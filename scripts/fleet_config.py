#!/usr/bin/env python3
"""Validate and query credential-free Marsh fleet profiles.

The manifest deliberately holds only a fleet's scope, GitHub owner, profile
path, and the *name* of its installation-ID secret field. Runtime credentials
remain host-only. Two scopes are supported:

``organization``
    The established selected-repository organization runner group model.
``repository``
    A personal-account model. Every private repository is explicitly listed in
    the profile, and each JIT runner is registered through that repository's
    API endpoint. It has no organization runner group and no warm floor.
"""
from __future__ import annotations

import argparse
import ipaddress
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


FLEET_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")
INSTALLATION_KEY = re.compile(r"^installation_id_[a-z0-9_]+$")
OWNER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*$")
RUNNER_GROUP_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")
# ``.github`` is GitHub's special organization profile repository. Keep this
# exactly aligned with the runtime path validator; no other leading-dot name is
# accepted as a configured repository target.
REPOSITORY_NAME = re.compile(r"^(?:\.github|[A-Za-z0-9][A-Za-z0-9._-]*)$")
FORBIDDEN_CONFIG_KEYS = re.compile(r"(?:api[_-]?key|token|secret|password|private)", re.IGNORECASE)
ORGANIZATION_SCOPE = "organization"
REPOSITORY_SCOPE = "repository"
SCOPES = {ORGANIZATION_SCOPE, REPOSITORY_SCOPE}
DAYTONA_TARGET = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
NETWORK_POLICY = "deny-by-default"
DOMAIN_NAME = re.compile(
    r"^(?:\*\.)?(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$"
)

@dataclass(frozen=True)
class Fleet:
    name: str
    scope: str
    owner: str
    profile_rel: str
    installation_id_key: str
    profile: dict[str, Any]


def fail(message: str) -> None:
    raise ValueError(message)


def load_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            loaded = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        fail(f"{path}: {exc}")
    if not isinstance(loaded, dict):
        fail(f"{path}: expected a TOML table")
    return loaded


def profile_path(root: Path, name: str, profile_rel: str) -> Path:
    # Deriving the only accepted profile path from the already-validated fleet
    # name retains the path boundary without publishing an environment-specific
    # allowlist in source.
    expected = f"config/fleets/{name}/runners.toml"
    if profile_rel != expected:
        fail(f"fleet {name!r} must use its canonical profile path {expected!r}")
    return root / expected


def require_string(table: dict[str, Any], key: str, context: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value.strip():
        fail(f"{context}: {key} must be a non-empty string")
    return value


def require_only(table: dict[str, Any], allowed: set[str], context: str) -> None:
    unknown = set(table) - allowed
    if unknown:
        fail(f"{context}: unsupported keys {sorted(unknown)}")


def validate_repositories(value: object, context: str) -> list[str]:
    if not isinstance(value, list) or not value:
        fail(f"{context}: repositories must be a non-empty array")
    repositories: list[str] = []
    seen: set[str] = set()
    for repository in value:
        if not isinstance(repository, str) or not REPOSITORY_NAME.fullmatch(repository):
            fail(f"{context}: invalid repository name {repository!r}")
        lowered = repository.lower()
        if lowered in seen:
            fail(f"{context}: duplicate repository {repository!r}")
        seen.add(lowered)
        repositories.append(repository)
    return repositories


def validate_network(profile_file: Path, profile: dict[str, Any], scope: str) -> set[str]:
    """Validate a restrictive sandbox egress policy and return its route labels."""
    network = profile.get("network")
    routing = profile.get("routing")
    if network is None:
        if routing is not None:
            fail(f"{profile_file}: [routing] requires a restrictive [network] profile")
        return set()
    if scope != REPOSITORY_SCOPE:
        fail(f"{profile_file}: [network] is allowed only for repository-scoped fleets")
    if not isinstance(network, dict):
        fail(f"{profile_file}: [network] must be a TOML table")
    require_only(network, {"policy", "cidr_allow_list", "domain_allow_list"}, f"{profile_file}: [network]")
    if network.get("policy") != NETWORK_POLICY:
        fail(f"{profile_file}: [network].policy must be {NETWORK_POLICY!r}")

    cidrs = network.get("cidr_allow_list")
    if not isinstance(cidrs, list) or not cidrs or len(cidrs) > 5:
        fail(f"{profile_file}: [network].cidr_allow_list must contain 1-5 IPv4 CIDRs")
    seen_cidrs: set[str] = set()
    for cidr in cidrs:
        if not isinstance(cidr, str):
            fail(f"{profile_file}: [network].cidr_allow_list contains a non-string value")
        try:
            parsed = ipaddress.ip_network(cidr, strict=True)
        except ValueError as exc:
            fail(f"{profile_file}: invalid network CIDR {cidr!r}: {exc}")
        if parsed.version != 4 or not parsed.is_private:
            fail(f"{profile_file}: network CIDR {cidr!r} must be private IPv4 space")
        if str(parsed) in seen_cidrs:
            fail(f"{profile_file}: duplicate network CIDR {cidr!r}")
        seen_cidrs.add(str(parsed))

    domains = network.get("domain_allow_list")
    if not isinstance(domains, list) or not domains:
        fail(f"{profile_file}: [network].domain_allow_list must be a non-empty array")
    seen_domains: set[str] = set()
    for domain in domains:
        if not isinstance(domain, str) or not DOMAIN_NAME.fullmatch(domain):
            fail(f"{profile_file}: invalid network allowlist domain {domain!r}")
        lowered = domain.lower()
        if lowered in seen_domains:
            fail(f"{profile_file}: duplicate network allowlist domain {domain!r}")
        seen_domains.add(lowered)

    if not isinstance(routing, dict):
        fail(f"{profile_file}: restrictive [network] requires a [routing] table")
    require_only(routing, {"required_labels"}, f"{profile_file}: [routing]")
    labels = routing.get("required_labels")
    if not isinstance(labels, list) or not labels:
        fail(f"{profile_file}: [routing].required_labels must be a non-empty array")
    required: set[str] = set()
    for label in labels:
        if not isinstance(label, str) or not RUNNER_GROUP_NAME.fullmatch(label):
            fail(f"{profile_file}: invalid required routing label {label!r}")
        if label in {"self-hosted", "daytona", "marsh", "large"}:
            fail(f"{profile_file}: [routing] label {label!r} is not an exclusive selector")
        if label in required:
            fail(f"{profile_file}: duplicate required routing label {label!r}")
        required.add(label)
    return required


def validate_profile(profile_file: Path, scope: str, owner: str) -> dict[str, Any]:
    profile = load_toml(profile_file)
    github = profile.get("github")
    if not isinstance(github, dict):
        fail(f"{profile_file}: missing [github]")
    runner_group: str | None = None
    profile_scope = github.get("scope", ORGANIZATION_SCOPE)
    if profile_scope != scope:
        fail(f"{profile_file}: [github].scope must match manifest scope {scope!r}")

    if scope == ORGANIZATION_SCOPE:
        require_only(github, {"scope", "org", "runner_group"}, f"{profile_file}: [github]")
        if require_string(github, "org", str(profile_file)) != owner:
            fail(f"{profile_file}: [github].org must match the manifest organization")
        if github.get("runner_group") != "daytona":
            fail(f"{profile_file}: [github].runner_group must be exactly 'daytona'")
    elif scope == REPOSITORY_SCOPE:
        require_only(github, {"scope", "owner", "runner_group_id", "runner_group", "repositories"}, f"{profile_file}: [github]")
        if require_string(github, "owner", str(profile_file)) != owner:
            fail(f"{profile_file}: [github].owner must match the manifest owner")
        validate_repositories(github.get("repositories"), f"{profile_file}: [github]")
        runner_group_id = github.get("runner_group_id")
        runner_group = github.get("runner_group")
        if (runner_group_id is None) == (runner_group is None):
            fail(f"{profile_file}: [github] must declare exactly one of runner_group_id or runner_group")
        if runner_group_id is not None and (not isinstance(runner_group_id, int) or runner_group_id < 1):
            fail(f"{profile_file}: [github].runner_group_id must be a positive integer")
        if runner_group is not None and (not isinstance(runner_group, str) or not RUNNER_GROUP_NAME.fullmatch(runner_group)):
            fail(f"{profile_file}: [github].runner_group must be a safe non-empty group name")
    else:  # defensive even though load_fleets validates scopes before calling us
        fail(f"{profile_file}: unsupported GitHub scope {scope!r}")

    daytona = profile.get("daytona")
    if not isinstance(daytona, dict):
        fail(f"{profile_file}: missing [daytona].target")
    target = require_string(daytona, "target", str(profile_file))
    if not DAYTONA_TARGET.fullmatch(target):
        fail(f"{profile_file}: [daytona].target must be a safe target name")

    routing_labels = validate_network(profile_file, profile, scope)
    if scope == REPOSITORY_SCOPE:
        if routing_labels:
            if runner_group is None:
                fail(f"{profile_file}: restrictive [network] requires a named [github].runner_group")
            if routing_labels != {runner_group}:
                fail(f"{profile_file}: [github].runner_group must match exactly the exclusive routing label")
        elif runner_group is not None:
            fail(f"{profile_file}: [github].runner_group is reserved for restrictive [network] profiles")

    cache = profile.get("cache")
    if cache is not None:
        if not isinstance(cache, dict) or not require_string(cache, "volume", str(profile_file)):
            fail(f"{profile_file}: [cache].volume must be a non-empty string when [cache] exists")

    poller = profile.get("poller")
    if poller is not None:
        if not isinstance(poller, dict):
            fail(f"{profile_file}: [poller] must be a TOML table")
        require_only(poller, {"interval_secs", "request_spacing_secs"}, f"{profile_file}: [poller]")
        interval = poller.get("interval_secs")
        if not isinstance(interval, int) or interval < 1:
            fail(f"{profile_file}: [poller].interval_secs must be a positive integer")
        if scope == REPOSITORY_SCOPE and interval < 60:
            fail(f"{profile_file}: repository-scoped [poller].interval_secs must be at least 60")
        request_spacing = poller.get("request_spacing_secs", 0)
        if (not isinstance(request_spacing, int) or isinstance(request_spacing, bool)
                or request_spacing < 0):
            fail(f"{profile_file}: [poller].request_spacing_secs must be a non-negative integer")

    webhook = profile.get("webhook")
    if webhook is not None:
        if not isinstance(webhook, dict):
            fail(f"{profile_file}: [webhook] must be a TOML table")
        # hmac_env names the host env var that holds the GitHub webhook HMAC secret.
        # The secret value itself must never appear in the profile.
        require_only(webhook, {"listen", "hmac_env"}, f"{profile_file}: [webhook]")
        listen = webhook.get("listen")
        if not isinstance(listen, str) or ":" not in listen:
            fail(f"{profile_file}: [webhook].listen must be host:port")
        host, _, port_s = listen.rpartition(":")
        host = host.strip().lower()
        if host not in {"127.0.0.1", "0.0.0.0", "::1", "localhost"}:
            fail(f"{profile_file}: [webhook].listen host must be loopback or 0.0.0.0")
        try:
            port = int(port_s)
        except (TypeError, ValueError):
            fail(f"{profile_file}: [webhook].listen port must be an integer")
        if not (1 <= port <= 65535):
            fail(f"{profile_file}: [webhook].listen port out of range")
        hmac_env = webhook.get("hmac_env", "MARSH_WEBHOOK_HMAC")
        if not isinstance(hmac_env, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", hmac_env):
            fail(f"{profile_file}: [webhook].hmac_env must be a safe env var name")

    lifecycle = profile.get("lifecycle")
    if lifecycle is not None:
        if not isinstance(lifecycle, dict):
            fail(f"{profile_file}: [lifecycle] must be a TOML table")
        require_only(lifecycle, {
            "ephemeral", "auto_stop_minutes", "auto_delete_interval",
            "idle_refresh_secs", "demand_idle_secs", "job_max_secs",
            "hold_on_failure_secs", "idle_poll_secs", "fast_idle_poll_secs",
            "fast_idle_window_secs", "busy_poll_secs",
        }, f"{profile_file}: [lifecycle]")
        for key in ("hold_on_failure_secs", "idle_poll_secs", "fast_idle_poll_secs",
                    "fast_idle_window_secs", "busy_poll_secs", "job_max_secs",
                    "idle_refresh_secs", "demand_idle_secs", "auto_stop_minutes"):
            if key not in lifecycle:
                continue
            value = lifecycle[key]
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                fail(f"{profile_file}: [lifecycle].{key} must be a non-negative integer")
            if key in {"idle_poll_secs", "fast_idle_poll_secs", "busy_poll_secs"} and value < 1:
                fail(f"{profile_file}: [lifecycle].{key} must be at least 1")

    watchdog = profile.get("watchdog")
    if watchdog is not None:
        if not isinstance(watchdog, dict):
            fail(f"{profile_file}: [watchdog] must be a TOML table")
        require_only(watchdog, {"github_queue_scan"}, f"{profile_file}: [watchdog]")
        if not isinstance(watchdog.get("github_queue_scan", True), bool):
            fail(f"{profile_file}: [watchdog].github_queue_scan must be a boolean")

    classes = profile.get("size_class")
    if not isinstance(classes, list) or not classes:
        fail(f"{profile_file}: at least one [[size_class]] is required")
    by_name: dict[str, dict[str, Any]] = {}
    for item in classes:
        if not isinstance(item, dict):
            fail(f"{profile_file}: every [[size_class]] must be a table")
        name = require_string(item, "name", str(profile_file))
        if name in by_name:
            fail(f"{profile_file}: duplicate size class {name!r}")
        labels = item.get("labels")
        if not isinstance(labels, list) or not all(isinstance(label, str) and label for label in labels):
            fail(f"{profile_file}: size class {name!r} must define string labels")
        if "self-hosted" not in labels:
            fail(f"{profile_file}: size class {name!r} must include the self-hosted label")
        if routing_labels:
            if "daytona" in labels or "marsh" in labels:
                fail(f"{profile_file}: restricted size class {name!r} must not share daytona or marsh labels")
            if not routing_labels.issubset(labels):
                fail(f"{profile_file}: restricted size class {name!r} must include every required routing label")
        elif "daytona" not in labels:
            fail(f"{profile_file}: size class {name!r} must include the daytona label")
        require_string(item, "snapshot", str(profile_file))
        for numeric in ("min_idle", "max"):
            numeric_value = item.get(numeric)
            if (not isinstance(numeric_value, int) or isinstance(numeric_value, bool)
                    or numeric_value < 0):
                fail(f"{profile_file}: size class {name!r} has invalid {numeric}")
        min_idle = item["min_idle"]
        if min_idle > item["max"]:
            fail(f"{profile_file}: size class {name!r} min_idle cannot exceed max")
        if scope == REPOSITORY_SCOPE and min_idle != 0:
            fail(f"{profile_file}: repository-scoped size class {name!r} must set min_idle = 0")
        warm_floor_reason = item.get("warm_floor_reason")
        if min_idle > 0:
            if not isinstance(warm_floor_reason, str) or not warm_floor_reason.strip():
                fail(
                    f"{profile_file}: size class {name!r} with nonzero min_idle "
                    "must define a non-empty warm_floor_reason"
                )
        elif warm_floor_reason is not None:
            fail(
                f"{profile_file}: size class {name!r} may define warm_floor_reason "
                "only when min_idle is nonzero"
            )
        by_name[name] = item

    for required in ("default", "large"):
        if required not in by_name:
            fail(f"{profile_file}: required size class {required!r} is missing")
    if "large" not in by_name["large"]["labels"]:
        fail(f"{profile_file}: large size class must include the large label")

    def walk(value: Any, path: str = "") -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                child_path = f"{path}.{key}" if path else str(key)
                if FORBIDDEN_CONFIG_KEYS.search(str(key)):
                    fail(f"{profile_file}: forbidden credential-like key {child_path!r}")
                walk(child, child_path)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{path}[{index}]")

    walk(profile)
    return profile


def load_fleets(root: Path) -> list[Fleet]:
    manifest_path = root / "config" / "fleets" / "manifest.toml"
    manifest = load_toml(manifest_path)
    metadata = manifest.get("manifest")
    if not isinstance(metadata, dict) or metadata.get("version") != 1:
        fail(f"{manifest_path}: [manifest].version must be 1")
    entries = manifest.get("fleet")
    if not isinstance(entries, list) or not entries:
        fail(f"{manifest_path}: at least one [[fleet]] is required")

    fleets: list[Fleet] = []
    names: set[str] = set()
    owner_scopes: set[tuple[str, str]] = set()
    for raw in entries:
        if not isinstance(raw, dict):
            fail(f"{manifest_path}: every [[fleet]] must be a table")
        scope = raw.get("scope", ORGANIZATION_SCOPE)
        if scope not in SCOPES:
            fail(f"{manifest_path}: unsupported fleet scope {scope!r}")
        allowed = {"name", "scope", "profile", "installation_id_key"}
        owner_key = "organization" if scope == ORGANIZATION_SCOPE else "owner"
        allowed.add(owner_key)
        require_only(raw, allowed, str(manifest_path))

        name = require_string(raw, "name", str(manifest_path))
        if not FLEET_NAME.fullmatch(name):
            fail(f"{manifest_path}: invalid fleet name {name!r}")
        if name in names:
            fail(f"{manifest_path}: duplicate fleet {name!r}")
        owner = require_string(raw, owner_key, str(manifest_path))
        if not OWNER_NAME.fullmatch(owner):
            fail(f"{manifest_path}: invalid {owner_key} {owner!r}")
        owner_identity = (scope, owner.lower())
        if owner_identity in owner_scopes:
            fail(f"{manifest_path}: duplicate GitHub owner {owner!r} in {scope!r} scope")
        profile_rel = require_string(raw, "profile", str(manifest_path))
        installation_id_key = require_string(raw, "installation_id_key", str(manifest_path))
        if not INSTALLATION_KEY.fullmatch(installation_id_key):
            fail(f"{manifest_path}: invalid installation_id_key for fleet {name!r}")
        profile = validate_profile(profile_path(root, name, profile_rel), scope, owner)
        fleets.append(Fleet(name, scope, owner, profile_rel, installation_id_key, profile))
        names.add(name)
        owner_scopes.add(owner_identity)
    return fleets


def root_path() -> Path:
    root = Path(__file__).resolve().parents[1]
    if not (root / "config" / "fleets" / "manifest.toml").is_file():
        fail(f"{root}: missing config/fleets/manifest.toml")
    return root


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("validate", help="validate the manifest and every fleet profile")
    get = subcommands.add_parser("get", help="print one safe manifest value")
    get.add_argument("--fleet", required=True)
    get.add_argument("--field", required=True,
                     choices=("profile", "organization", "owner", "scope", "installation-id-key"))
    args = parser.parse_args()

    try:
        fleets = load_fleets(root_path())
        if args.command == "validate":
            print(f"validated {len(fleets)} Marsh fleet profile(s)")
            return 0
        fleet = next((item for item in fleets if item.name == args.fleet), None)
        if fleet is None:
            fail(f"unknown fleet {args.fleet!r}")
        if args.field == "organization" and fleet.scope != ORGANIZATION_SCOPE:
            fail(f"fleet {fleet.name!r} is repository-scoped and has no organization runner-group owner")
        values = {
            "profile": fleet.profile_rel,
            "organization": fleet.owner,
            "owner": fleet.owner,
            "scope": fleet.scope,
            "installation-id-key": fleet.installation_id_key,
        }
        print(values[args.field])
        return 0
    except ValueError as exc:
        print(f"fleet configuration error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
