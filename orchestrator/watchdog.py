"""Marsh fleet watchdog and usage reporter.

The orchestrator is deliberately silent when healthy — which means a dead
control-plane host is also silent. This sidecar closes that gap from the same
box, with two oneshot subcommands driven by systemd timers:

    watchdog.py check         # every few minutes: alert on unhealthy fleet
    watchdog.py usage-report  # daily: spawn-count summary per size class

Checks, per configured [[instance]] (one orchestrator process/org):
  * unit          -- the orchestrator's systemd unit is active
  * stuck queue   -- a queued job with this fleet's labels has waited longer
                     than `stuck_queue_minutes`. Scanned over the App
                     INSTALLATION's repos, not just the runner group's, so a
                     repo that was never added to the runner group (jobs queue
                     forever, invisible to the orchestrator's own demand scan)
                     is caught and called out explicitly.
  * sandboxes     -- org-labeled sandbox count exceeds `max_sandboxes`, or a
                     sandbox has been alive longer than `orphan_sandbox_minutes`

Alerts POST to [notify].url (ntfy-style headers or plain JSON), deduplicate via
a state file so a persistent failure re-pages on `realert_minutes` instead of
every timer tick, and send a recovery notice when a condition clears. A
successful all-clear pass can GET [notify].heartbeat_url as a dead-man's
switch: if this watchdog (or the whole host) dies, the missing heartbeat is
itself the page.

Configuration: TOML file (default /etc/marsh/watchdog.toml, see
config/watchdog.example.toml). Reuses the orchestrator's GitHub App and
Daytona clients — run it from the same directory/venv as orchestrator.py.
No secrets in the config: credentials come from each instance's env_file and
an optional bearer token from the env var named by [notify].token_env.
"""

from __future__ import annotations

import argparse
import calendar
import json
import math
import os
import subprocess
import sys
import time
import tomllib
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from orchestrator import Daytona, GitHub, _sandbox_age_secs, match_class, routing_required_labels  # noqa: E402

# Keep the parser compatible with deploy-watchdog.sh upgrading watchdog.py on
# a host whose orchestrator.py has not yet been restarted/upgraded.
CYCLE_TELEMETRY_PREFIX = "cycle_telemetry "

# Fixed paths (systemd's StateDirectory=marsh provides /var/lib/marsh):
# deliberately not env-configurable so no runtime input ever shapes a filesystem path.
STATE_DIR = "/var/lib/marsh"
STATE_PATH = "/var/lib/marsh/watchdog-state.json"

# --config must resolve under one of these bases (or the working directory,
# for development runs) — an allowlist rather than an open path.
CONFIG_BASES = ("/etc/marsh",)


class RejectRedirects(urllib.request.HTTPRedirectHandler):
    """Never forward a configured notification bearer token on redirect."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        return None


def open_without_redirects(request: urllib.request.Request, timeout: float):
    return urllib.request.build_opener(RejectRedirects()).open(request, timeout=timeout)


def resolve_config_path(arg: str) -> str:
    path = os.path.realpath(arg)
    for base in (*CONFIG_BASES, os.getcwd()):
        base = os.path.realpath(base) + os.sep
        if path.startswith(base):
            return path
    raise SystemExit(f"config path must live under {' or '.join(CONFIG_BASES)} (or the working directory) — got {path}")


# --------------------------------------------------------------------------
# config / state


def load_config(path: str) -> dict:
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    if not cfg.get("instance"):
        raise SystemExit(f"{path}: at least one [[instance]] block is required")
    return cfg


def load_env_file(path: str) -> dict[str, str]:
    """KEY=VALUE lines, as written by the deploy scripts. Values never logged."""
    env: dict[str, str] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k] = v
    return env


def load_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def save_state(state: dict) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_PATH)


# --------------------------------------------------------------------------
# notification


def notify(cfg: dict, title: str, body: str, priority: str = "high") -> None:
    n = cfg.get("notify", {})
    url = n.get("url", "")
    if not url:
        print(f"[notify disabled] {title}: {body}")
        return
    token = os.environ.get(n.get("token_env", "MARSH_NOTIFY_TOKEN") or "", "")
    if n.get("format", "ntfy") == "json":
        data = json.dumps({"title": title, "message": body, "priority": priority}).encode()
        headers = {"Content-Type": "application/json"}
    else:  # ntfy
        data = body.encode()
        headers = {"Title": title, "Priority": priority, "Tags": "rotating_light" if priority == "high" else "white_check_mark"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if not url.startswith(("https://", "http://")):
        print(f"[notify skipped] non-http(s) notify url", file=sys.stderr)
        return
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        # Operator-configured webhook URL, scheme-checked above — never request input.
        with open_without_redirects(req, timeout=15) as response:
            response.read()
    except urllib.error.HTTPError as e:
        status = e.code
        e.close()
        print(f"[notify failed] HTTP {status}: {title}", file=sys.stderr)
    except OSError as e:  # alerting must never crash the check pass
        print(f"[notify failed] {e}: {title}", file=sys.stderr)


def heartbeat(cfg: dict) -> None:
    url = cfg.get("notify", {}).get("heartbeat_url", "")
    if not url.startswith(("https://", "http://")):
        return
    try:
        # Operator-configured heartbeat URL, scheme-checked above — never request input.
        urllib.request.urlopen(url, timeout=15).read()  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
    except OSError as e:
        print(f"[heartbeat failed] {e}", file=sys.stderr)


# --------------------------------------------------------------------------
# checks


def unit_active(unit: str) -> bool:
    r = subprocess.run(["systemctl", "is-active", "--quiet", unit], check=False)
    return r.returncode == 0


def installation_repository_records(gh: GitHub) -> list[dict]:
    """Normalized installation records for this fleet owner.

    Fetch once per watchdog pass so coverage and queued-job checks use the
    same App-visible roster without multiplying paginated GitHub calls.
    """
    records: list[dict] = []
    for item in gh.installation_repositories():
        if not isinstance(item, dict):
            continue
        owner = item.get("owner")
        name = item.get("name")
        if not isinstance(owner, dict) or not isinstance(name, str):
            continue
        if owner.get("login", "").lower() != gh.owner.lower():
            continue
        records.append(item)
    return records


def installation_repos(gh: GitHub, private_only: bool = False) -> list[str]:
    """Repository names visible to this App for its configured owner."""
    return [
        item["name"]
        for item in installation_repository_records(gh)
        if not private_only or item.get("private") is True
    ]


def selected_runner_group_id(gh: GitHub, group_name: str) -> int:
    """Resolve a selected organization runner group without Default fallback."""
    groups = gh._api("GET", f"/orgs/{gh.owner}/actions/runner-groups").get("runner_groups", [])
    matches = [
        group for group in groups
        if isinstance(group, dict) and group.get("name") == group_name
    ]
    if len(matches) != 1 or not isinstance(matches[0].get("id"), int):
        raise RuntimeError(f"runner group {group_name!r} is not uniquely available")
    group = matches[0]
    if group.get("visibility") != "selected" or group.get("allows_public_repositories") is not False:
        raise RuntimeError(f"runner group {group_name!r} is not selected private-only scoped")
    return int(group["id"])


def fleet_sandbox(labels: object, scope: str, owner: str, fleet: str) -> bool:
    if not isinstance(labels, dict) or labels.get("role") != "gha-runner":
        return False
    if scope == "repository":
        return labels.get("scope") == "repository" and labels.get("fleet") == fleet
    return labels.get("org") == owner.lower() and labels.get("scope") != "repository"


def check_instance(inst: dict, cfg: dict, findings: list[str]) -> None:
    name = inst.get("name", "unnamed")
    checks = cfg.get("checks", {})

    unit = inst.get("unit", "")
    if unit and not unit_active(unit):
        findings.append(f"[{name}] systemd unit {unit} is NOT active")
        return  # credentials/env may be the reason the unit is down; skip API checks

    env = load_env_file(inst["env_file"])
    with open(inst["config"], "rb") as f:
        rcfg = tomllib.load(f)
    github_config = rcfg["github"]
    scope = github_config.get("scope", "organization")
    if scope == "organization":
        owner = github_config["org"]
        repositories: list[str] | None = None
    elif scope == "repository":
        owner = github_config["owner"]
        repositories = list(github_config["repositories"])
    else:
        raise RuntimeError(f"unsupported GitHub scope {scope!r}")
    classes = rcfg.get("size_class", [])
    required_labels = routing_required_labels(rcfg)
    group_name = github_config.get("runner_group", "daytona")
    watchdog_config = rcfg.get("watchdog", {})
    if not isinstance(watchdog_config, dict):
        raise RuntimeError("[watchdog] must be a TOML table")
    github_queue_scan = watchdog_config.get("github_queue_scan", True)
    if not isinstance(github_queue_scan, bool):
        raise RuntimeError("[watchdog].github_queue_scan must be a boolean")

    # -- runner access coverage / stuck queue ------------------------------
    # Coverage always runs for an active fleet, including when its expensive
    # queued-job scan is intentionally disabled. This keeps a repository
    # selection change from silently stranding work between deployments.
    stuck_after = int(checks.get("stuck_queue_minutes", 10)) * 60
    queue_scan = github_queue_scan and stuck_after > 0
    poller_config = rcfg.get("poller", {})
    if not isinstance(poller_config, dict):
        raise RuntimeError("[poller] must be a TOML table")
    request_spacing_secs = poller_config.get("request_spacing_secs", 0)
    gh = GitHub(owner, env["GH_APP_ID"], env["GH_APP_INSTALLATION_ID"], env["GH_APP_KEY_PATH"],
                scope=scope, repositories=repositories, request_spacing_secs=request_spacing_secs)
    group_repos: dict[str, str] = {}
    if scope == "organization":
        if not isinstance(group_name, str) or not group_name:
            raise RuntimeError("organization profile has an invalid runner_group")
        try:
            gh.require_organization_installation_all_repositories()
        except RuntimeError as error:
            # Keep the remaining checks running: an installation downgraded to
            # selected repositories is itself actionable, but so is a private
            # repository that has already lost runner-group access.
            findings.append(f"[{name}] {error}")
        group_repos = {
            repository.lower(): repository
            for repository in gh.runner_group_repos(selected_runner_group_id(gh, group_name))
        }
        records = installation_repository_records(gh)
        queue_repos = [item["name"] for item in records]
        visible = {item["name"].lower(): item["name"] for item in records}
        selected_not_visible = sorted(
            repository for key, repository in group_repos.items() if key not in visible
        )
        if selected_not_visible:
            findings.append(
                f"[{name}] runner group '{group_name}' selects repository/repositories not visible to the Marsh App: "
                f"{', '.join(selected_not_visible[:10])}"
            )
        visible_private = {
            item["name"].lower(): item["name"]
            for item in records if item.get("private") is True
        }
        uncovered = sorted(name for key, name in visible_private.items() if key not in group_repos)
        if uncovered:
            findings.append(
                f"[{name}] private organization repository/repositories cannot reach runner group "
                f"'{group_name}': {', '.join(uncovered[:10])}"
            )
    else:
        assert repositories is not None
        records = installation_repository_records(gh)
        installation_private = {
            item["name"].lower(): item["name"]
            for item in records if item.get("private") is True
        }
        configured = {repository.lower(): repository for repository in repositories}
        missing = sorted(configured[key] for key in configured.keys() - installation_private.keys())
        unconfigured = sorted(installation_private[key] for key in installation_private.keys() - configured.keys())
        if missing:
            findings.append(
                f"[{name}] repository profile includes private App repository/repositories no longer visible: "
                f"{', '.join(missing[:10])}"
            )
        if unconfigured:
            findings.append(
                f"[{name}] private App repository/repositories are not covered by this repository profile: "
                f"{', '.join(unconfigured[:10])}"
            )
        configured_group_name = github_config.get("runner_group")
        configured_group_id = github_config.get("runner_group_id")
        observed_group_ids: set[int] = set()
        for repository in repositories:
            groups = gh.repository_runner_groups(repository)
            if isinstance(configured_group_name, str) and configured_group_name:
                matches = [
                    group for group in groups
                    if group.get("name") == configured_group_name and isinstance(group.get("id"), int)
                ]
                if len(matches) != 1:
                    findings.append(
                        f"[{name}] configured runner group '{configured_group_name}' is unavailable to "
                        f"{owner}/{repository}"
                    )
                    continue
                observed_group_ids.add(int(matches[0]["id"]))
            elif isinstance(configured_group_id, int) and not isinstance(configured_group_id, bool):
                if not any(group.get("id") == configured_group_id for group in groups):
                    findings.append(
                        f"[{name}] configured runner group {configured_group_id} is unavailable to "
                        f"{owner}/{repository}"
                    )
            else:
                raise RuntimeError("repository profile has an invalid runner group")
        if isinstance(configured_group_name, str) and len(observed_group_ids) > 1:
            findings.append(
                f"[{name}] configured runner group '{configured_group_name}' has inconsistent IDs across repositories"
            )
        queue_repos = repositories

    if queue_scan:
        now = time.time()
        for queued in gh.queued_jobs(queue_repos):
            job = queued.job
            labels = set(job.get("labels", []))
            if not labels or not match_class(labels, classes, required_labels):
                continue  # not this fleet's job
            created = job.get("created_at", "")
            try:
                waited = now - calendar.timegm(time.strptime(created, "%Y-%m-%dT%H:%M:%SZ"))
            except ValueError:
                continue
            if waited < stuck_after:
                continue
            repo = queued.repository
            mins = int(waited // 60)
            if scope == "organization" and repo.lower() not in group_repos:
                findings.append(
                    f"[{name}] {owner}/{repo}: job queued {mins}m but the repo is NOT in "
                    f"runner group '{group_name}' — jobs will queue forever until it is added"
                )
            else:
                findings.append(f"[{name}] {owner}/{repo}: job queued {mins}m — {job.get('html_url', '')}")

    # -- daytona sandbox sanity -------------------------------------------
    max_sb = int(checks.get("max_sandboxes", 0))
    orphan_after = int(checks.get("orphan_sandbox_minutes", 0)) * 60
    if max_sb or orphan_after:
        dt = Daytona(env["DAYTONA_API_KEY"], rcfg["daytona"].get("target", "us"), None)
        fleet_name = env.get("MARSH_FLEET_NAME", name)
        mine = [
            sb for sb in dt.sdk.list()
            if fleet_sandbox(getattr(sb, "labels", None), scope, owner, fleet_name)
        ]
        if max_sb and len(mine) > max_sb:
            findings.append(f"[{name}] {len(mine)} fleet-labeled sandboxes exceed the configured max of {max_sb}")
        if orphan_after:
            old = [sb for sb in mine if (_sandbox_age_secs(sb) or 0) > orphan_after]
            if old:
                findings.append(
                    f"[{name}] {len(old)} sandbox(es) older than {orphan_after // 60}m still "
                    f"running — possible orphans: {', '.join(sb.id[:8] for sb in old[:5])}"
                )


def cmd_check(cfg: dict) -> int:
    findings: list[str] = []
    for inst in cfg["instance"]:
        try:
            check_instance(inst, cfg, findings)
        except Exception as e:  # noqa: BLE001 — one broken instance must not mask the rest
            findings.append(f"[{inst.get('name', 'unnamed')}] watchdog check errored: {type(e).__name__}: {e}")

    state = load_state()
    active: dict[str, float] = state.get("active", {})
    now = time.time()
    realert = int(cfg.get("checks", {}).get("realert_minutes", 60)) * 60

    current = {f: active.get(f, 0.0) for f in findings}
    fresh = [f for f in findings if now - current[f] >= realert]
    recovered = [f for f in active if f not in current]

    if fresh:
        notify(cfg, f"Marsh fleet: {len(findings)} problem(s)", "\n".join(findings)[:3500], "high")
        for f in fresh:
            current[f] = now
    if recovered:
        notify(cfg, "Marsh fleet: recovered", "\n".join(recovered)[:3500], "default")

    save_state({"active": current})
    for f in findings:
        print(f)
    if not findings:
        print("all clear")
        heartbeat(cfg)
    return 1 if findings else 0


# --------------------------------------------------------------------------
# usage report


def parse_cycle_telemetry(lines: list[str]) -> list[dict]:
    """Extract complete v1 cycle records from journal message text.

    Malformed or future-schema records are ignored so a single partial journal
    write cannot break the daily report. The producer validates and redacts the
    payload; this parser still accepts only the exact event contract it knows.
    """
    events: list[dict] = []
    for line in lines:
        marker = line.find(CYCLE_TELEMETRY_PREFIX)
        if marker < 0:
            continue
        try:
            record = json.loads(line[marker + len(CYCLE_TELEMETRY_PREFIX):])
        except (json.JSONDecodeError, TypeError):
            continue
        if (isinstance(record, dict)
                and record.get("event") == "runner_cycle_complete"
                and record.get("schema_version") == 1):
            events.append(record)
    return events


def _finite_nonnegative(value: object) -> float | None:
    if (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value >= 0):
        return float(value)
    return None


def _nearest_rank(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(percentile * len(ordered)) - 1)]


def _stage_p95_summary(records: list[dict], field: str) -> str | None:
    """p95 for a stage field when every record in the cohort has it."""
    values = [
        value for record in records
        if (value := _finite_nonnegative(record.get(field))) is not None
    ]
    if len(values) != len(records) or not values:
        return None
    return f"{field.removesuffix('_secs')}={_nearest_rank(values, 0.95):.2f}s"


def summarize_cycle_telemetry(events: list[dict]) -> list[str]:
    """Summarize reserved allocation and duration by immutable snapshot name.

    CPU/RAM/disk totals are controller-observed declared allocation-hours from
    successful sandbox creation through confirmed deletion. They are not peak
    utilization or provider billing truth. Stage p95 figures (jit/create/start/
    idle/busy/teardown) are included only when every record in the cohort has
    that stage sample so mixed pre-rollout windows stay honest.
    """
    grouped: dict[str, list[dict]] = {}
    for event in events:
        snapshot = event.get("snapshot")
        if isinstance(snapshot, str) and snapshot:
            grouped.setdefault(snapshot, []).append(event)

    output: list[str] = []
    for snapshot, records in sorted(grouped.items()):
        durations = [
            value for record in records
            if (value := _finite_nonnegative(record.get("total_secs"))) is not None
        ]
        outcomes = {name: 0 for name in ("job", "idle", "failed", "unknown")}
        cpu_hours = ram_hours = disk_hours = 0.0
        allocated_records = 0
        for record in records:
            outcome = record.get("outcome")
            outcomes[outcome if outcome in outcomes else "unknown"] += 1
            cleanup_status = record.get("cleanup_status")
            if cleanup_status == "create_not_attempted":
                # No Daytona allocation existed, so this is a known zero rather
                # than a missing sample.
                allocated_records += 1
                continue
            if cleanup_status != "deleted":
                continue
            duration = _finite_nonnegative(record.get("allocated_secs"))
            resources = record.get("declared_resources")
            if duration is None or not isinstance(resources, dict):
                continue
            cpu = _finite_nonnegative(resources.get("cpu"))
            ram = _finite_nonnegative(resources.get("memory_gib"))
            disk = _finite_nonnegative(resources.get("disk_gib"))
            if None in (cpu, ram, disk):
                continue
            hours = duration / 3600
            cpu_hours += cpu * hours  # type: ignore[operator]
            ram_hours += ram * hours  # type: ignore[operator]
            disk_hours += disk * hours  # type: ignore[operator]
            allocated_records += 1

        duration_summary = f"duration unavailable (coverage={len(durations)}/{len(records)})"
        if len(durations) == len(records):
            duration_summary = (
                f"avg={sum(durations) / len(durations) / 60:.2f}m "
                f"p95={_nearest_rank(durations, 0.95) / 60:.2f}m"
            )
        stage_parts = [
            part for field in (
                "jit_mint_secs", "sandbox_create_secs", "runner_start_secs",
                "idle_secs", "busy_secs", "teardown_secs",
            )
            if (part := _stage_p95_summary(records, field)) is not None
        ]
        if stage_parts:
            duration_summary = f"{duration_summary}; stages p95 {'/'.join(stage_parts)}"
        allocation_summary = "allocation unavailable"
        if allocated_records == len(records):
            allocation_summary = (
                f"allocated={cpu_hours:.2f} CPU-h/{ram_hours:.2f} GiB-h RAM/"
                f"{disk_hours:.2f} GiB-h disk"
            )
        else:
            allocation_summary = (
                f"allocation unavailable (coverage={allocated_records}/{len(records)})"
            )
        output.append(
            f"{snapshot}: n={len(records)} job={outcomes['job']} idle={outcomes['idle']} "
            f"failed={outcomes['failed']} unknown={outcomes['unknown']} "
            f"{duration_summary}; {allocation_summary}"
        )
    return output


def cmd_usage_report(cfg: dict, since_override: str | None = None) -> int:
    """Report per-snapshot lifecycle averages from structured journal events.

    Old journal windows without structured completion events retain the legacy
    spawn-count fallback. Billing truth still lives with the provider.
    """
    since = since_override or cfg.get("report", {}).get("since", "-24h")
    lines_out: list[str] = []
    for inst in cfg["instance"]:
        unit = inst.get("unit", "")
        if not unit:
            continue
        r = subprocess.run(
            ["journalctl", "-u", unit, "--since", since, "--no-pager", "-o", "cat"],
            capture_output=True, text=True, check=False,
        )
        name = inst.get("name", unit)
        if r.returncode != 0:
            lines_out.append(f"{name}: journal unavailable (exit {r.returncode})")
            continue
        journal_lines = r.stdout.splitlines()
        events = parse_cycle_telemetry(journal_lines)
        summaries = summarize_cycle_telemetry(events)
        legacy_spawns = sum("runner up:" in line and "[" in line for line in journal_lines)
        if summaries:
            lines_out.append(
                f"{name}: structured completions={len(events)}; runner-up lines={legacy_spawns}; "
                "window may span the telemetry rollout"
            )
            lines_out.extend(f"  {summary}" for summary in summaries)
            continue

        # Pre-upgrade compatibility: report the historical spawn count until
        # the selected window contains structured cycle-completion records.
        counts: dict[str, int] = {}
        for line in journal_lines:
            if "runner up:" in line and "[" in line:
                cls = line.split("[", 1)[1].split("]", 1)[0]
                counts[cls] = counts.get(cls, 0) + 1
        summary = ", ".join(f"{v} {k}" for k, v in sorted(counts.items())) or "no spawns"
        lines_out.append(f"{name}: legacy window; {summary}")
    body = "\n".join(lines_out)
    print(body)
    notify(cfg, f"Marsh usage ({since})", body, "default")
    return 0


# --------------------------------------------------------------------------


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("command", choices=["check", "usage-report"])
    p.add_argument("--config", default="/etc/marsh/watchdog.toml")
    p.add_argument("--since", help="override the usage-report journal window; use --since=-72h")
    args = p.parse_args()
    cfg = load_config(resolve_config_path(args.config))
    sys.exit(cmd_check(cfg) if args.command == "check" else cmd_usage_report(cfg, args.since))


if __name__ == "__main__":
    main()
