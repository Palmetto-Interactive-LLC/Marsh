#!/usr/bin/env python3
"""Marsh orchestrator -- demand-driven reconciler for Daytona-hosted GitHub runners.

GitHub is the controller (queues + dispatches jobs by label). This orchestrator watches
GitHub's own queue and keeps just enough ephemeral `[self-hosted, marsh]` runners
registered to match it, plus a `min_idle` floor per size class for instant pickup of the
common single-job case. The old static warm pool is gone — `max` (a real burst ceiling)
and `large` (spawn-from-zero) are both now live instead of dead config.

Model — a reconciler ("poller") ticks every `[poller].interval_secs`:
    dynamic repo list (the runner group's selected repos, via GitHub API — never
    hardcoded) -> queued job demand per size class -> busy-runner map -> per class:
    spawn max(demand deficit, floor deficit, 0) new "cycles", never letting live cycles
    exceed `max`.
Each cycle is its own thread, tracked in a lock-guarded in-memory registry ({class,
state SPAWNING|IDLE|BUSY, runner_id, sandbox_id, spawned_at, busy_at,
idle_deadline_secs}), and runs exactly once (no internal retry loop — a failed/expired
cycle just drops out of the registry and the next tick's deficit recalculation spawns
a fresh one; this is what makes the reconciler self-correcting without per-job
bookkeeping):
    mint JIT (Marsh App)  ->  Daytona SDK: create sandbox from the sized snapshot
    (+ cache volume; auto_stop_interval=[lifecycle].auto_stop_minutes as an orphan
    safety net for a crashed orchestrator)  ->  start the runner as a background
    SESSION command (process.exec's request/response is capped at ~3600s regardless
    of the timeout= passed to it; a session command returns its cmd_id immediately
    and is polled separately,
    so no single HTTP request stays open for the runner's lifetime)  ->  poll every
    ~15s for job pickup (busy map) / natural exit / idle+job deadlines  ->  delete
    sandbox, deregister runner.
An orphan sweep (~every 10min, from the main loop) generalizes the startup reap()
using the registry to avoid touching anything still tracked.

Uses the Daytona Python SDK (version-matched to the API — the CLI release lagged and
its exec of long-running processes was unreliable). App JWT signed via `openssl`
(key never leaves the host); GitHub REST via stdlib. Run it under a host service
manager with a bounded shutdown deadline.

Env: DAYTONA_API_KEY, GH_APP_ID, GH_APP_INSTALLATION_ID, GH_APP_KEY_PATH,
     MARSH_RUNNER_CONFIG (default /etc/marsh/runners.toml).
"""
from __future__ import annotations

import base64
import ipaddress
import json
import logging
import math
import os
import re
import shlex
import signal
import stat
import subprocess
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from daytona_sdk import (
    CreateSandboxFromSnapshotParams,
    Daytona as DaytonaSDK,
    DaytonaConfig,
    SessionExecuteRequest,
    VolumeMount,
)

log = logging.getLogger("marsh-orch")


# ─────────────────────────── GitHub (controller) ───────────────────────────
GITHUB_SCOPE_ORGANIZATION = "organization"
GITHUB_SCOPE_REPOSITORY = "repository"
# ``.github`` is GitHub's special organization profile repository.  It is the
# sole leading-dot exception; accepting a general leading dot would weaken the
# one-path-component boundary enforced before a name reaches an API path.
REPOSITORY_NAME = re.compile(r"^(?:\.github|[A-Za-z0-9][A-Za-z0-9._-]*)$")
FLEET_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")
NETWORK_DOMAIN = re.compile(r"^(?:\*\.)?(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
RUNNER_FLEET_LABEL_PREFIX = "marsh-fleet-"
RATE_LIMIT_BACKOFF_INITIAL_SECS = 60.0
RATE_LIMIT_BACKOFF_MAX_SECS = 900.0
START_QUIESCED_ENV = "MARSH_START_QUIESCED"


class GitHubRequestCancelled(RuntimeError):
    """The orchestrator is stopping while a rate-limited request is waiting."""


@dataclass(frozen=True)
class RunnerRef:
    """A GitHub runner registration and the repository that owns it.

    Organization runners use ``repository=None``. A repository-scoped runner
    ID must always keep its repository: the status and delete endpoints are
    repository-local, and treating an ID as global could tear down a runner
    from another personal repository.
    """

    runner_id: int
    repository: str | None = None


@dataclass(frozen=True)
class QueuedJob:
    """A queued GitHub job together with the repository that queued it."""

    repository: str
    job: dict


class GitHub:
    def __init__(self, owner: str, app_id: str, installation_id: str, key_path: str,
                 scope: str = GITHUB_SCOPE_ORGANIZATION, repositories: list[str] | None = None,
                 fleet_name: str | None = None, request_spacing_secs: float = 0.0,
                 stop_event: threading.Event | None = None):
        if scope not in (GITHUB_SCOPE_ORGANIZATION, GITHUB_SCOPE_REPOSITORY):
            raise ValueError(f"unsupported GitHub scope {scope!r}")
        if scope == GITHUB_SCOPE_REPOSITORY and not repositories:
            raise ValueError("repository-scoped GitHub client requires configured repositories")
        if fleet_name is not None and not FLEET_NAME.fullmatch(fleet_name):
            raise ValueError(f"invalid Marsh fleet name {fleet_name!r}")
        if (not isinstance(request_spacing_secs, (int, float))
                or isinstance(request_spacing_secs, bool)
                or not math.isfinite(request_spacing_secs)
                or request_spacing_secs < 0):
            raise ValueError("GitHub request spacing must be a non-negative finite number")
        self.owner, self.org = owner, owner  # ``org`` remains the legacy public attribute for org callers.
        self.scope = scope
        self.repositories = tuple(repositories or ())
        self.fleet_name = fleet_name
        self.request_spacing_secs = float(request_spacing_secs)
        self.app_id, self.installation_id, self.key_path = app_id, installation_id, key_path
        self._stop_event = stop_event
        self._tok: tuple[str, float] | None = None
        self._lock = threading.Lock()
        # All GitHub REST calls made by one fleet share this gate. The controller has
        # cycle threads as well as its poller, so per-call sleeps would still permit
        # a burst unless the wait, request, and cooldown state are serialized together.
        self._request_lock = threading.Lock()
        self._next_request_at = 0.0
        self._fallback_rate_limit_backoff_secs = RATE_LIMIT_BACKOFF_INITIAL_SECS
        self._rate_limit_generation = 0
        self._repo_cache: tuple[list[str], float] | None = None
        self._repo_lock = threading.Lock()
        self._group_cache: tuple[int, float] | None = None
        self._group_lock = threading.Lock()
        # A selected-repository App installation can hide an organization
        # repository from both the normal installation roster and a selected
        # runner group. Keep a short-lived proof that the organization App is
        # installed for *all* repositories, and clear it with the ordinary
        # reconciliation caches when an administrator restores access.
        self._organization_installation_cache: float | None = None
        self._organization_installation_lock = threading.Lock()
        self._organization_installation_generation = 0

    @staticmethod
    def _b64url(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    def _app_jwt(self) -> str:
        now = int(time.time())
        header = self._b64url(b'{"alg":"RS256","typ":"JWT"}')
        payload = self._b64url(json.dumps({"iat": now - 60, "exp": now + 540, "iss": self.app_id}).encode())
        si = f"{header}.{payload}".encode()
        sig = subprocess.run(["openssl", "dgst", "-sha256", "-sign", self.key_path],
                             input=si, capture_output=True, check=True).stdout
        return f"{header}.{payload}.{self._b64url(sig)}"

    @staticmethod
    def _header_delay(error: urllib.error.HTTPError) -> tuple[float, str] | None:
        """Return a server-directed rate-limit delay without reading an error body."""
        headers = error.headers
        if headers is None:
            return None

        retry_after = headers.get("Retry-After")
        if retry_after is not None:
            value = retry_after.strip()
            try:
                delay = float(value)
            except ValueError:
                try:
                    retry_at = parsedate_to_datetime(value)
                    if retry_at.tzinfo is None:
                        retry_at = retry_at.replace(tzinfo=timezone.utc)
                    delay = retry_at.timestamp() - time.time()
                except (TypeError, ValueError, OverflowError, IndexError):
                    delay = float("nan")
            if math.isfinite(delay) and delay >= 0:
                return delay, "retry-after"

        reset = headers.get("X-RateLimit-Reset")
        remaining = headers.get("X-RateLimit-Remaining")
        if reset is not None and remaining is not None and remaining.strip() == "0":
            try:
                delay = float(reset.strip()) - time.time()
            except ValueError:
                delay = float("nan")
            # A reset timestamp in the past is not useful guidance for a new
            # 403, particularly a secondary-rate-limit response.
            if math.isfinite(delay) and delay > 0:
                return delay, "rate-limit-reset"
        return None

    def _rate_limit_cooldown(self, error: urllib.error.HTTPError) -> tuple[float, str]:
        """Choose a shared cooldown for a 403/429 while retaining a safe fallback.

        GitHub may report a secondary limit while the primary limit remains high.
        When it provides Retry-After or a future primary reset, honor that response.
        Otherwise use an exponentially escalating, bounded floor. A later successful
        *full reconciliation tick* resets that fallback. Individual successful calls
        cannot do so: a partial scan can succeed before its later Actions request is
        rate limited, and must still escalate on the next failed tick.
        """
        directed = self._header_delay(error)
        if directed is not None:
            return directed
        delay = self._fallback_rate_limit_backoff_secs
        self._fallback_rate_limit_backoff_secs = min(
            self._fallback_rate_limit_backoff_secs * 2,
            RATE_LIMIT_BACKOFF_MAX_SECS,
        )
        return delay, "exponential-fallback"

    def _wait_for_request_slot_locked(self) -> None:
        """Wait for the shared slot without making service shutdown wait for cooldown.

        The lock remains held so no cycle thread can slip an API call through the
        gap. ``Event.wait`` wakes immediately on SIGTERM/SIGINT, releasing the gate
        before systemd's shutdown timeout. Clients without an event (preflight and
        one-shot tooling) retain the ordinary bounded sleep behavior.
        """
        while True:
            if self._stop_event is not None and self._stop_event.is_set():
                raise GitHubRequestCancelled("GitHub request cancelled during service shutdown")
            wait_secs = self._next_request_at - time.monotonic()
            if wait_secs <= 0:
                return
            if self._stop_event is None:
                time.sleep(wait_secs)
            elif self._stop_event.wait(wait_secs):
                raise GitHubRequestCancelled("GitHub request cancelled during rate-limit cooldown")

    @staticmethod
    def _request(method: str, path: str, body: dict | None, bearer: str) -> urllib.request.Request:
        req = urllib.request.Request(f"https://api.github.com{path}",
                                     data=json.dumps(body).encode() if body is not None else None, method=method)
        req.add_header("Authorization", f"Bearer {bearer}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        return req

    def _send_request_locked(self, req: urllib.request.Request) -> dict:
        self._wait_for_request_slot_locked()
        request_started_at = time.monotonic()
        try:
            # URL is https://api.github.com + an internal path constant — never caller input.
            with urllib.request.urlopen(req, timeout=30) as r:  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
                result = json.loads(r.read() or "{}")
        except urllib.error.HTTPError as error:
            if error.code in (403, 429):
                cooldown_secs, source = self._rate_limit_cooldown(error)
                self._rate_limit_generation += 1
                self._next_request_at = max(
                    self._next_request_at,
                    request_started_at + self.request_spacing_secs,
                    time.monotonic() + cooldown_secs,
                )
                # Deliberately no response body, URL, or authorization material in logs.
                log.warning("GitHub API status=%s; applying shared %.1fs cooldown (%s)",
                            error.code, cooldown_secs, source)
            raise
        # Space request starts, rather than adding the full delay after a slow
        # response. A zero value preserves the historical no-pacing behavior.
        self._next_request_at = request_started_at + self.request_spacing_secs
        return result

    def _installation_token_locked(self) -> str:
        """Return an installation token while the request gate is held.

        A fresh App JWT is signed only after any pending cooldown has elapsed. If
        a token refresh is needed, that refresh itself is rate-paced before the
        eventual caller request, so neither credential expires while waiting.
        """
        with self._lock:
            if self._tok and self._tok[1] > time.time() + 120:
                return self._tok[0]
            self._wait_for_request_slot_locked()
            # Repository-scoped profiles run a separate JIT controller beside
            # an organization-wide fleet. Mint the installation token narrowly
            # rather than merely relying on our caller-side endpoint checks.
            # GitHub rejects names outside the installation, so this can only
            # reduce privilege.
            token_scope = {"repositories": list(self.repositories)} if self.scope == GITHUB_SCOPE_REPOSITORY else {}
            req = self._request("POST", f"/app/installations/{self.installation_id}/access_tokens",
                                token_scope, self._app_jwt())
            out = self._send_request_locked(req)
            self._tok = (out["token"], time.time() + 3000)
            return self._tok[0]

    def _installation_token(self) -> str:
        with self._request_lock:
            return self._installation_token_locked()

    def _api(self, method: str, path: str, body: dict | None = None, bearer: str | None = None) -> dict:
        # Keep this lock through token minting and the response: otherwise cycle
        # threads can overlap a poller request and defeat spacing/cooldown state.
        with self._request_lock:
            self._wait_for_request_slot_locked()
            token = bearer or self._installation_token_locked()
            return self._send_request_locked(self._request(method, path, body, token))

    def _app_api(self, method: str, path: str, body: dict | None = None) -> dict:
        """Call an App-only endpoint without reusing an installation token.

        GitHub's installation-metadata endpoint accepts an App JWT, rather
        than the narrower installation token used by normal runner APIs. Mint
        that JWT after the shared rate gate opens so it cannot age out during a
        cooldown. The request lock keeps this call inside the same per-fleet
        rate-limit discipline as every other GitHub call.
        """
        with self._request_lock:
            self._wait_for_request_slot_locked()
            return self._send_request_locked(self._request(method, path, body, self._app_jwt()))

    def rate_limit_checkpoint(self) -> int:
        """Snapshot the shared rate-limit state before a reconciliation tick."""
        with self._request_lock:
            return self._rate_limit_generation

    def reset_rate_limit_backoff(self, checkpoint: int) -> None:
        """Mark a complete, rate-error-free reconciliation as recovery.

        Cycle threads share the same client. The generation guard prevents a
        concurrent cycle's 403/429 from being erased merely because the poller
        happened to complete its own scan at the same moment.
        """
        with self._request_lock:
            if self._rate_limit_generation == checkpoint:
                self._fallback_rate_limit_backoff_secs = RATE_LIMIT_BACKOFF_INITIAL_SECS

    def _repository_path(self, repository: str) -> str:
        """Return a safe repository API prefix for a configured target.

        Repository scope accepts only the static profile allowlist. Organization
        scope also permits runner-group discoveries, but still rejects a slash
        or otherwise malformed name before it reaches an HTTP path.
        """
        if not REPOSITORY_NAME.fullmatch(repository):
            raise ValueError(f"invalid repository name {repository!r}")
        if self.scope == GITHUB_SCOPE_REPOSITORY and repository not in self.repositories:
            raise ValueError(f"repository {repository!r} is outside this fleet's configured scope")
        return f"/repos/{urllib.parse.quote(self.owner, safe='')}/{urllib.parse.quote(repository, safe='')}"

    def _runners_path(self, repository: str | None = None) -> str:
        if self.scope == GITHUB_SCOPE_ORGANIZATION:
            if repository is not None:
                raise ValueError("organization runner endpoint does not take a repository")
            return f"/orgs/{urllib.parse.quote(self.owner, safe='')}/actions/runners"
        if repository is None:
            raise ValueError("repository runner endpoint requires a repository")
        return f"{self._repository_path(repository)}/actions/runners"

    def configured_repositories(self) -> list[str]:
        if self.scope != GITHUB_SCOPE_REPOSITORY:
            raise ValueError("organization scope discovers repositories from its runner group")
        return list(self.repositories)

    def fleet_runner_label(self) -> str:
        """The exact GitHub label that scopes repository-JIT cleanup.

        Sandbox labels alone are insufficient: GitHub runner registrations
        outlive a failed sandbox briefly, and a sibling profile could otherwise
        reap every offline ``marsh-*`` runner in the same repository.
        """
        if self.scope != GITHUB_SCOPE_REPOSITORY or not self.fleet_name:
            raise ValueError("repository runner cleanup requires an exact Marsh fleet name")
        return f"{RUNNER_FLEET_LABEL_PREFIX}{self.fleet_name}"

    def runner_group_id(self, name: str) -> int:
        if self.scope != GITHUB_SCOPE_ORGANIZATION:
            raise ValueError("repository scope has no organization runner-group lookup")
        groups = self._api("GET", f"/orgs/{self.org}/actions/runner-groups").get("runner_groups", [])
        matches = [
            group for group in groups
            if isinstance(group, dict)
            and group.get("name") == name
            and isinstance(group.get("id"), int)
            and group.get("visibility") == "selected"
            and group.get("allows_public_repositories") is False
        ]
        if len(matches) != 1:
            raise RuntimeError(
                f"configured runner group {name!r} is absent, ambiguous, not selected, or allows public repositories"
            )
        return int(matches[0]["id"])

    def current_group_id(self, name: str, ttl: int = 600) -> int:
        """Resolve the configured selected runner group on a bounded cadence.

        A missing, ambiguous, or non-selected group raises rather than falling
        back to Default, so a cache refresh cannot register runners into a
        broader unrelated pool. Callers retry on a later poll after the
        configuration is repaired.
        """
        if self.scope != GITHUB_SCOPE_ORGANIZATION:
            raise ValueError("repository scope uses its profile's runner_group_id")
        self.require_organization_installation_all_repositories(ttl=ttl)
        with self._group_lock:
            if self._group_cache and time.time() - self._group_cache[1] < ttl:
                return self._group_cache[0]
        gid = self.runner_group_id(name)
        with self._group_lock:
            self._group_cache = (gid, time.time())
        return gid

    def require_organization_installation_all_repositories(self, ttl: int = 600) -> None:
        """Fail closed unless this organization App installation covers every repo.

        Comparing a runner group's selected repositories with
        ``/installation/repositories`` is only complete after GitHub confirms
        the App is installed for all organization repositories. Otherwise a
        private repository omitted from both lists can silently lose runner
        access. Only a successful, verified App-metadata response is cached.
        """
        if self.scope != GITHUB_SCOPE_ORGANIZATION:
            raise ValueError("repository scope does not require organization-wide App coverage")
        while True:
            with self._organization_installation_lock:
                if (self._organization_installation_cache is not None
                        and time.time() - self._organization_installation_cache < ttl):
                    return
                generation = self._organization_installation_generation

            metadata = self._app_api("GET", f"/app/installations/{self.installation_id}")
            account = metadata.get("account")
            account_login = account.get("login") if isinstance(account, dict) else None
            if str(metadata.get("id")) != self.installation_id:
                raise RuntimeError("GitHub App installation metadata did not match the configured installation")
            if metadata.get("target_type") != "Organization":
                raise RuntimeError("GitHub App installation is not scoped to the configured organization")
            if not isinstance(account_login, str) or account_login.lower() != self.owner.lower():
                raise RuntimeError("GitHub App installation does not match the configured organization")
            if metadata.get("repository_selection") != "all":
                raise RuntimeError(
                    "GitHub App installation must cover all repositories before organization runners can reconcile"
                )
            with self._organization_installation_lock:
                if generation == self._organization_installation_generation:
                    self._organization_installation_cache = time.time()
                    return
            # SIGUSR2 invalidated the proof while the App request was in
            # flight. Recheck against the newly restored configuration rather
            # than letting this reconciliation use a bounded stale response.

    def runner_group_repos(self, group_id: int, ttl: int = 600) -> list[str]:
        """Repos selected into the runner group — fetched dynamically (never hardcoded
        in runners.toml) and cached ~10min, so onboarding a new repo needs zero
        orchestrator change or restart.

        A selected-group pagination/API failure is intentionally propagated. Returning
        a partial page list could make the reconciler spawn from incomplete demand;
        caching it would repeat that unsafe snapshot for ten minutes. A group that
        is non-selected or permits public repositories is a trust-boundary failure,
        so it raises before any idle floor can mint a runner into that group."""
        if self.scope != GITHUB_SCOPE_ORGANIZATION:
            raise ValueError("repository scope uses its profile's configured repository allowlist")
        with self._repo_lock:
            if self._repo_cache and time.time() - self._repo_cache[1] < ttl:
                return self._repo_cache[0]
        repos: list[str] = []
        group = self._api("GET", f"/orgs/{self.org}/actions/runner-groups/{group_id}")
        if (group.get("visibility") != "selected"
                or group.get("allows_public_repositories") is not False):
            raise RuntimeError(
                f"runner group {group_id} is not selected private-only; refusing to reconcile or cache demand"
            )
        else:
            for p in range(1, 51):
                d = self._api("GET", f"/orgs/{self.org}/actions/runner-groups/{group_id}/repositories"
                                     f"?per_page=100&page={p}")
                page = d.get("repositories", [])
                if not isinstance(page, list):
                    raise RuntimeError("runner-group repository response was not a list")
                repos.extend(r["name"] for r in page)
                if len(page) < 100:
                    break
            else:
                raise RuntimeError(
                    "runner-group repository pagination limit reached; refusing incomplete repository selection"
                )
        with self._repo_lock:
            self._repo_cache = (repos, time.time())
        return repos

    def queued_jobs(self, repos: list[str]) -> list[QueuedJob]:
        """Queued jobs across repos. Scans queued+in_progress runs (a matrix run holds
        queued jobs while the run itself is in_progress) and returns each queued job's
        raw dict (only `labels` is used, by the caller). Both listing calls are
        paginated like every other listing in this file -- an under-count here (more
        than 100 org-wide queued+in_progress runs, or more than 100 jobs in one run)
        would silently hide demand, which is exactly the failure this reconciler
        exists to prevent."""
        jobs: list[QueuedJob] = []
        for repo in repos:
            repo_path = self._repository_path(repo)
            run_ids: list[int] = []
            for status in ("queued", "in_progress"):
                for p in range(1, 25):
                    d = self._api("GET",
                                 f"{repo_path}/actions/runs?status={status}&per_page=100&page={p}")
                    page = d.get("workflow_runs", [])
                    run_ids.extend(r["id"] for r in page)
                    if len(page) < 100:
                        break
            for run_id in run_ids:
                for p in range(1, 25):
                    d = self._api("GET",
                                 f"{repo_path}/actions/runs/{run_id}/jobs?per_page=100&page={p}")
                    page = d.get("jobs", [])
                    jobs.extend(QueuedJob(repo, j) for j in page if j.get("status") == "queued")
                    if len(page) < 100:
                        break
        return jobs

    def runners(self) -> list[tuple[RunnerRef, dict]]:
        """Every runner visible to this fleet, with its owning scope retained."""
        rows: list[tuple[RunnerRef, dict]] = []
        repositories: list[str | None] = [None]
        if self.scope == GITHUB_SCOPE_REPOSITORY:
            repositories = self.configured_repositories()
        for repository in repositories:
            base = self._runners_path(repository)
            for p in range(1, 25):
                d = self._api("GET", f"{base}?per_page=100&page={p}")
                rs = d.get("runners", [])
                for runner in rs:
                    rows.append((RunnerRef(int(runner["id"]), repository), runner))
                if len(rs) < 100:
                    break
        return rows

    def runners_busy_map(self) -> dict[RunnerRef, bool]:
        """``{RunnerRef: busy}`` for every registration visible to this fleet."""
        return {ref: bool(runner.get("busy")) for ref, runner in self.runners()}

    def runner_busy(self, runner: RunnerRef) -> bool | None:
        """Fresh, point-in-time busy state for ONE runner. Returns None if it can't be
        determined (network error, or a 404 = runner already gone). Used to gate an
        idle-teardown against the race where GitHub assigns a job to a runner in the same
        window the tick-cached busy_map would call it idle: callers treat None as "not
        confirmed idle" and DECLINE to tear down, so a job is never killed on a stale read."""
        try:
            r = self._api("GET", f"{self._runners_path(runner.repository)}/{runner.runner_id}")
            return bool(r.get("busy"))
        except GitHubRequestCancelled:
            # A shutdown cancellation is distinct from an ordinary unavailable
            # busy-state lookup. The cycle needs to retain its resources explicitly
            # rather than accidentally treating this as an ordinary transient.
            raise
        except Exception:  # noqa: BLE001 — 404 (gone) or transient; caller must not tear down on None
            return None

    def mint_jit(self, group_id: int, labels: list[str], repository: str | None = None) -> tuple[RunnerRef, str]:
        # generate-jitconfig REGISTERS the runner now (returns its id) and yields the
        # encoded config the runner boots with. Return the id so we can deregister on
        # cleanup — a JIT runner killed before completing a job would otherwise linger
        # offline forever (GitHub only auto-removes ephemerals that finish a job).
        jit_labels = list(labels)
        if self.scope == GITHUB_SCOPE_REPOSITORY:
            fleet_label = self.fleet_runner_label()
            if fleet_label not in jit_labels:
                jit_labels.append(fleet_label)
        out = self._api("POST", f"{self._runners_path(repository)}/generate-jitconfig",
                       body={"name": f"marsh-{uuid.uuid4().hex[:12]}", "runner_group_id": group_id,
                             "labels": jit_labels, "work_folder": "_work"})
        return RunnerRef(int(out["runner"]["id"]), repository), out["encoded_jit_config"]

    def delete_runner(self, runner: RunnerRef) -> bool:
        """Delete a runner registration and report whether cleanup is proven.

        A 404 means GitHub already removed the ephemeral registration and is
        equivalent to a successful delete. Other failures stay visible to a
        draining controller: claiming a cycle is gone when its runner may
        still be online would make a deployment's empty-status proof unsound.
        Existing best-effort callers may ignore the boolean.
        """
        try:
            self._api("DELETE", f"{self._runners_path(runner.repository)}/{runner.runner_id}")
        except urllib.error.HTTPError as error:
            error.close()
            return error.code == 404
        except Exception:  # noqa: BLE001 — network/API failure is not a confirmed deletion
            return False
        return True

    def invalidate_reconciliation_cache(self) -> None:
        """Force the next poller pass to observe current runner-group access.

        An administrator restores selected repositories after a fenced cohort
        rollout. SIGUSR2 calls this through ``main`` so controllers need not
        be restarted just to discard their bounded empty-group cache.
        """
        with self._group_lock:
            self._group_cache = None
        with self._repo_lock:
            self._repo_cache = None
        with self._organization_installation_lock:
            self._organization_installation_cache = None
            self._organization_installation_generation += 1

    def repository_runner_groups(self, repository: str) -> list[dict]:
        """Runner groups available to one repository, for repo-scope preflight."""
        if self.scope != GITHUB_SCOPE_REPOSITORY:
            raise ValueError("repository runner groups are only meaningful in repository scope")
        return self._api("GET", f"{self._repository_path(repository)}/actions/runner-groups").get("runner_groups", [])

    def current_repository_group_id(self, name: str, ttl: int = 600) -> int:
        """Resolve a named repository-visible group identically across every target.

        A named private group avoids a stale numeric identifier in source. The
        cache follows the organization group's ten-minute resolution cadence;
        if an administrator removes the group, the next refresh fails closed
        before another JIT registration can be minted.
        """
        if self.scope != GITHUB_SCOPE_REPOSITORY:
            raise ValueError("repository runner-group lookup requires repository scope")
        with self._group_lock:
            if self._group_cache and time.time() - self._group_cache[1] < ttl:
                return self._group_cache[0]
        identifiers: set[int] = set()
        for repository in self.repositories:
            matches = [group for group in self.repository_runner_groups(repository)
                       if group.get("name") == name and isinstance(group.get("id"), int)]
            if len(matches) != 1:
                raise RuntimeError(
                    f"repository runner group {name!r} is not uniquely available to {self.owner}/{repository}"
                )
            identifiers.add(int(matches[0]["id"]))
        if len(identifiers) != 1:
            raise RuntimeError(f"repository runner group {name!r} has inconsistent IDs across this fleet")
        group_id = identifiers.pop()
        with self._group_lock:
            self._group_cache = (group_id, time.time())
        return group_id

    def installation_repositories(self) -> list[dict]:
        """Repositories visible to the App installation; no credential is logged."""
        repos: list[dict] = []
        for page in range(1, 21):
            data = self._api("GET", f"/installation/repositories?per_page=100&page={page}")
            batch = data.get("repositories", [])
            if not isinstance(batch, list):
                raise RuntimeError("installation repository response was not a list")
            repos.extend(item for item in batch if isinstance(item, dict))
            if len(batch) < 100:
                break
        else:
            raise RuntimeError(
                "installation repository pagination limit reached; refusing incomplete App-visible roster"
            )
        return repos


def match_class(job_labels: set[str], classes: list[dict], required_labels: set[str] | frozenset[str] = frozenset()) -> dict | None:
    """Smallest size class whose label set is a superset of the job's requested labels
    (this mirrors GitHub's own runner-selection rule). None if no class qualifies —
    e.g. `ubuntu-latest` (GitHub-hosted) or a differently-pooled self-hosted label like
    `marsh`; those are just ignored, not an error."""
    if not set(required_labels) <= job_labels:
        return None
    candidates = [c for c in classes if job_labels <= set(c["labels"])]
    return min(candidates, key=lambda c: len(c["labels"])) if candidates else None


@dataclass(frozen=True)
class NetworkPolicy:
    """Per-sandbox outbound policy. Empty keeps standard fleet behavior unchanged."""

    network_allow_list: str | None = None
    domain_allow_list: str | None = None

    @property
    def restricted(self) -> bool:
        return self.network_allow_list is not None or self.domain_allow_list is not None

    def create_parameters(self) -> dict[str, str]:
        parameters: dict[str, str] = {}
        if self.network_allow_list is not None:
            parameters["network_allow_list"] = self.network_allow_list
        if self.domain_allow_list is not None:
            parameters["domain_allow_list"] = self.domain_allow_list
        return parameters


def _string_list(value: object, context: str) -> list[str]:
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
        raise ValueError(f"{context} must be a non-empty list of strings")
    return list(value)


def network_policy_from_config(cfg: dict) -> NetworkPolicy:
    """Parse the source-validated restrictive networking settings defensively."""
    network = cfg.get("network")
    if network is None:
        return NetworkPolicy()
    if not isinstance(network, dict) or network.get("policy") != "deny-by-default":
        raise ValueError("restricted runner networking must use policy=deny-by-default")
    cidrs = _string_list(network.get("cidr_allow_list"), "[network].cidr_allow_list")
    if len(cidrs) > 5:
        raise ValueError("[network].cidr_allow_list may contain at most five CIDRs")
    parsed_cidrs: list[str] = []
    for cidr in cidrs:
        try:
            parsed = ipaddress.ip_network(cidr, strict=True)
        except ValueError as exc:
            raise ValueError(f"invalid private network CIDR {cidr!r}") from exc
        if parsed.version != 4 or not parsed.is_private:
            raise ValueError(f"network CIDR {cidr!r} must be private IPv4 space")
        parsed_cidrs.append(str(parsed))
    if len(set(parsed_cidrs)) != len(parsed_cidrs):
        raise ValueError("[network].cidr_allow_list contains duplicate CIDRs")
    domains = _string_list(network.get("domain_allow_list"), "[network].domain_allow_list")
    lowered_domains = [domain.lower() for domain in domains]
    if len(set(lowered_domains)) != len(lowered_domains) or not all(NETWORK_DOMAIN.fullmatch(domain) for domain in lowered_domains):
        raise ValueError("[network].domain_allow_list contains an invalid or duplicate domain")
    return NetworkPolicy(
        network_allow_list=",".join(parsed_cidrs),
        domain_allow_list=",".join(lowered_domains),
    )


def routing_required_labels(cfg: dict) -> frozenset[str]:
    """Return labels that must appear on a queued job before this fleet mints JIT."""
    routing = cfg.get("routing")
    if routing is None:
        return frozenset()
    if not isinstance(routing, dict):
        raise ValueError("[routing] must be a TOML table")
    labels = _string_list(routing.get("required_labels"), "[routing].required_labels")
    return frozenset(labels)


# ─────────────────────────── Daytona (via SDK) ─────────────────────────────
# run-ephemeral.sh (baked in the image) starts dockerd, sets the cache env + job
# hooks, then execs the runner for exactly one job.  A Docker socket can appear
# before the daemon is ready to serve requests, though; container actions then
# fail intermittently at job setup.  Confirm the daemon with `docker info`
# before registering the runner, so GitHub cannot assign a job into that race.
#
# The pip HTTP cache is restored from a shared, whole-file volume.  A partially
# fetched package-index response can be syntactically valid inside its tarball
# but fail pip's JSON parser later.  Disable pip's cache for ephemeral runners
# until the cache format has an integrity-aware repair path.
RUNNER_CMD = r"""bash -c '
if command -v dockerd >/dev/null 2>&1 && command -v docker >/dev/null 2>&1; then
  if ! docker info >/dev/null 2>&1; then
    sudo -n sh -c "nohup dockerd >/var/log/dockerd.log 2>&1 &" 2>/dev/null || true
    for _ in $(seq 1 60); do
      if docker info >/dev/null 2>&1; then
        break
      fi
      sleep 1
    done
  fi
  docker info >/dev/null 2>&1 || {
    echo "runner bootstrap: Docker daemon did not become ready" >&2
    exit 1
  }
fi
exec bash /usr/local/bin/run-ephemeral.sh
'"""
RUNNER_ENV = {"CACHE_VOL": "/cache", "PIP_NO_CACHE_DIR": "1"}
SESSION_ID = "runner"  # sandboxes are single-purpose (one session each); no collision risk
# Consecutive session-read failures (each ~15s apart) WITH GitHub confirming the runner idle
# before a cycle concludes its sandbox is gone. Guards a running job against a transient
# Daytona-side read blip being mistaken for "session ended". ~45s of tolerance.
SESSION_READ_MAX_ERRS = 3


class Daytona:
    def __init__(self, api_key: str, target: str, volume_id: str | None, base_labels: dict | None = None,
                 network_policy: NetworkPolicy | None = None):
        self.sdk = DaytonaSDK(DaytonaConfig(api_key=api_key, target=target))
        self.volume_id = volume_id
        # Cost attribution: every sandbox carries org + size_class labels so
        # Daytona usage can be split per org (multi-instance/single-account deployments).
        self.base_labels = dict(base_labels or {})
        self.network_policy = network_policy or NetworkPolicy()

    def create_sandbox(self, snapshot: str, auto_stop_minutes: int, size_class: str = "",
                       repository: str | None = None):
        """New sandbox for one runner cycle. auto_stop_interval is minutes (verified
        against the installed SDK's CreateSandboxBaseParams field docstring, which
        matches the API's own field description) and is purely an orphan safety net:
        if the orchestrator crashes before this cycle tears the sandbox down, Daytona
        force-stops it on its own within auto_stop_minutes instead of it running
        (and billing) forever."""
        volumes = [VolumeMount(volume_id=self.volume_id, mount_path="/cache")] if self.volume_id else []
        labels = {"role": "gha-runner", **self.base_labels}
        if size_class:
            labels["size_class"] = size_class
        if repository:
            # Repository scope supplies this from the static profile allowlist;
            # keeping it on the sandbox makes cost and orphan evidence auditable.
            labels["repository"] = repository
        sandbox = self.sdk.create(CreateSandboxFromSnapshotParams(
            snapshot=snapshot, labels=labels, volumes=volumes,
            auto_stop_interval=auto_stop_minutes, auto_delete_interval=0,
            **self.network_policy.create_parameters()), timeout=180)
        self.sdk.get(sandbox.id)  # ensure started
        return sandbox

    def start_runner(self, sandbox, jit: str) -> str:
        """Launch the runner as a session command instead of process.exec: exec's
        request/response is capped at ~3600s regardless of timeout= (root-caused in
        the Daytona proxy); a session command with run_async=True returns
        its cmd_id immediately, so no single HTTP request stays open for the runner's
        lifetime — we poll get_session_command separately instead. SessionExecuteRequest
        has no env= param (unlike exec), so env vars are set via a shell prefix."""
        sandbox.process.create_session(SESSION_ID)
        env = {"RUNNER_JITCONFIG": jit, **RUNNER_ENV}
        prefix = " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items())
        resp = sandbox.process.execute_session_command(
            SESSION_ID, SessionExecuteRequest(command=f"{prefix} {RUNNER_CMD}", run_async=True), timeout=60)
        return resp.cmd_id

    def session_exit_code(self, sandbox, cmd_id: str) -> int | None:
        return sandbox.process.get_session_command(SESSION_ID, cmd_id).exit_code


class RejectRedirects(urllib.request.HTTPRedirectHandler):
    """Never forward provider bearer credentials through an HTTP redirect."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        return None


def open_without_redirects(request: urllib.request.Request, timeout: float):
    return urllib.request.build_opener(RejectRedirects()).open(request, timeout=timeout)


# ─────────────────────────── state registry ─────────────────────────────────
@dataclass
class Cycle:
    cls_name: str
    state: str  # SPAWNING | IDLE | BUSY
    idle_deadline_secs: int
    repository: str | None = None
    spawned_at: float = field(default_factory=time.time)
    busy_at: float | None = None
    runner: RunnerRef | None = None
    sandbox_id: str | None = None


REGISTRY: dict[str, Cycle] = {}
REGISTRY_LOCK = threading.Lock()


# This file is deliberately a tiny, non-secret contract for the deployment
# controller.  It is keyed by fleet rather than PID so a restart atomically
# replaces the previous process's view at the stable path
# ``/run/marsh/<fleet>.json``.
RUNTIME_STATUS_DIR = "/run/marsh"
RUNTIME_STATUS_SCHEMA = 1


class RuntimeControl:
    """Separate graceful admission control from service termination.

    ``stop`` retains the historical SIGTERM/SIGINT semantics: it asks every
    cycle to drain and causes ``main`` to leave its service loop.  ``admission``
    is intentionally independent: SIGUSR1 closes it without terminating the
    process, so existing busy cycles can finish and SIGUSR2 can reopen it.
    ``wake`` gives those signals prompt effect even while the main loop is
    waiting for its ordinary poll interval.
    """

    def __init__(self, *, start_quiesced: bool = False) -> None:
        self.stop = threading.Event()
        self.admission = threading.Event()
        if not start_quiesced:
            self.admission.set()
        self.wake = threading.Event()
        self.refresh = threading.Event()

    def request_stop(self) -> None:
        self.stop.set()
        self.wake.set()

    def quiesce(self) -> None:
        self.admission.clear()
        self.wake.set()

    def resume(self) -> None:
        self.admission.set()
        self.refresh.set()
        self.wake.set()


def _runtime_status_timestamp() -> str:
    """Return a stable UTC timestamp with a dedicated test seam."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _status_filename(fleet: str) -> str:
    """Return a single safe filename for a configured fleet.

    The strict fleet-name grammar prevents a configuration value from escaping
    the root-owned runtime directory.  Keep the fleet out of the JSON payload:
    the filename is its authority/key and the payload remains a small fixed
    non-secret schema.
    """
    if not FLEET_NAME.fullmatch(fleet):
        raise ValueError(f"invalid Marsh fleet name for runtime status {fleet!r}")
    root = os.path.normpath(RUNTIME_STATUS_DIR)
    if not os.path.isabs(root):
        raise RuntimeError("runtime status directory must be absolute")
    candidate = os.path.normpath(os.path.join(root, f"{fleet}.json"))
    prefix = f"{root}{os.sep}"
    if not candidate.startswith(prefix):
        raise ValueError("runtime status filename escapes its configured directory")
    return candidate[len(prefix):]


def _open_runtime_status_dir() -> int:
    """Open ``/run/marsh`` without following links or trusting an unsafe owner.

    The process normally runs as root; checking ownership against the effective
    UID also keeps local, unprivileged test invocations safe.  A group/world
    writable directory is rejected rather than becoming a root file-write
    primitive.  Callers receive an fd so the subsequent temporary write and
    rename stay beneath this verified directory even if its pathname changes.
    """
    try:
        os.mkdir(RUNTIME_STATUS_DIR, 0o755)
    except FileExistsError:
        pass

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    directory_fd = os.open(RUNTIME_STATUS_DIR, flags)
    try:
        info = os.fstat(directory_fd)
        if not stat.S_ISDIR(info.st_mode):
            raise RuntimeError("runtime status path is not a directory")
        if info.st_uid != os.geteuid():
            raise RuntimeError("runtime status directory is not owned by this service user")
        if stat.S_IMODE(info.st_mode) & 0o022:
            raise RuntimeError("runtime status directory must not be group/world writable")
    except Exception:
        os.close(directory_fd)
        raise
    return directory_fd


def runtime_status(fleet: str, admission: threading.Event,
                   ready: threading.Event | None = None) -> dict[str, int | bool | str]:
    """Build the fixed, secret-free status contract for one controller process.

    ``ready`` is deliberately false until startup cleanup and one complete
    reconciliation pass have both succeeded.  The one exception is an
    explicitly quiesced bootstrap process: it has published a valid status
    contract, has no active cycles, and cannot receive work until ``SIGUSR2``
    opens admission.  A systemd ``active`` state alone is never enough evidence
    that a replacement controller can safely receive work.
    """
    _status_filename(fleet)  # validate before producing a keyed status record
    with REGISTRY_LOCK:
        total = len(REGISTRY)
        busy = sum(1 for cycle_item in REGISTRY.values() if cycle_item.state == "BUSY")
    return {
        "schema": RUNTIME_STATUS_SCHEMA,
        "pid": os.getpid(),
        "admission": admission.is_set(),
        "ready": ready.is_set() if ready is not None else False,
        "total": total,
        "busy": busy,
        "updated_at": _runtime_status_timestamp(),
    }


def write_runtime_status(fleet: str, admission: threading.Event,
                         ready: threading.Event | None = None) -> None:
    """Atomically publish ``/run/marsh/<fleet>.json`` with no secret material.

    A unique O_EXCL temporary file is written and fsynced through a verified
    directory fd, then atomically renamed over the stable fleet filename.  An
    existing symlink at the target is replaced rather than followed.
    """
    filename = _status_filename(fleet)
    payload = json.dumps(runtime_status(fleet, admission, ready), sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8") + b"\n"
    directory_fd = _open_runtime_status_dir()
    temporary_name = f".status.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    temporary_fd: int | None = None
    renamed = False
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        temporary_fd = os.open(temporary_name, flags, 0o600, dir_fd=directory_fd)
        # Deployment and controller processes run under the service user, so
        # keep the status contract owner-readable without exposing it broadly.
        os.fchmod(temporary_fd, 0o600)
        with os.fdopen(temporary_fd, "wb", closefd=True) as temporary_file:
            temporary_fd = None
            temporary_file.write(payload)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_name, filename, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        renamed = True
        os.fsync(directory_fd)
    finally:
        if temporary_fd is not None:
            os.close(temporary_fd)
        if not renamed:
            try:
                os.unlink(temporary_name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
        os.close(directory_fd)


def _wake_status(status_wake: threading.Event | None) -> None:
    """Prompt the main loop to republish when a cycle changes visible state."""
    if status_wake is not None:
        status_wake.set()


def _runner_delete_confirmed(gh: GitHub, runner: RunnerRef) -> bool:
    """Accept historical mock/adapter ``None`` as success, never an explicit false.

    The concrete GitHub client returns a boolean. Keeping ``None`` compatible
    with existing local adapters avoids changing their public contract while
    retaining a fail-closed path for the real client and any adapter that can
    prove a cleanup failure.
    """
    return gh.delete_runner(runner) is not False


def _admission_open(admission: threading.Event | None) -> bool:
    """Treat omitted admission controls as open for legacy/direct callers."""
    return admission is None or admission.is_set()


class BusyMap:
    """Shared ``{RunnerRef: busy}``, refreshed wholesale once per poller tick and read by
    every cycle thread. A lock guards the swap/read pair — cheap since it's a single
    dict-reference assignment, not a per-key mutation."""

    def __init__(self) -> None:
        self._map: dict[RunnerRef, bool] = {}
        self._lock = threading.Lock()

    def refresh(self, new_map: dict[RunnerRef, bool]) -> None:
        with self._lock:
            self._map = new_map

    def is_busy(self, runner: RunnerRef | None) -> bool:
        with self._lock:
            return self._map.get(runner, False)


@dataclass(frozen=True)
class Lifecycle:
    job_max_secs: int
    auto_stop_minutes: int
    demand_idle_secs: int
    idle_refresh_secs: int


# ─────────────────────────── worker + pool ─────────────────────────────────
CYCLE_TELEMETRY_PREFIX = "cycle_telemetry "


def _cycle_now() -> float:
    """Clock seam for lifecycle telemetry tests without patching global time."""
    return time.time()


def _telemetry_timestamp(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _declared_resources(cls: dict) -> dict[str, float | int]:
    """Return finite numeric resource declarations from the static profile."""
    resources: dict[str, float | int] = {}
    for field_name in ("cpu", "memory_gib", "disk_gib"):
        value = cls.get(field_name)
        if (not isinstance(value, (int, float)) or isinstance(value, bool)
                or not math.isfinite(value) or value < 0):
            continue
        resources[field_name] = value
    return resources


def _log_cycle_telemetry(cycle_id: str, cls: dict, dt: Daytona, cycle_state: Cycle,
                         sandbox_started_at: float | None, runner_command_started_at: float | None,
                         completed_at: float, allocation_completed_at: float | None,
                         cleanup_status: str, outcome: str,
                         termination_reason: str, exit_code: int | None) -> None:
    """Emit one stable, secret-free JSON record after a runner cycle finishes.

    Journald already durably retains the orchestrator's stdout/stderr. The declared
    resources describe intended snapshot capacity, not runtime utilization.
    """
    labels = getattr(dt, "base_labels", {})
    if not isinstance(labels, dict):
        labels = {}
    profile = labels.get("fleet") or labels.get("org") or FLEET_LABEL or ORG_LABEL or "unknown"
    busy_at = cycle_state.busy_at
    payload = {
        "event": "runner_cycle_complete",
        "schema_version": 1,
        "cycle_id": cycle_id,
        "profile": str(profile),
        "scope": str(labels.get("scope") or GITHUB_SCOPE),
        "size_class": str(cls.get("name", "unknown")),
        "snapshot": str(cls.get("snapshot", "unknown")),
        "repository": cycle_state.repository,
        "outcome": outcome,
        "termination_reason": termination_reason,
        "started_at": _telemetry_timestamp(cycle_state.spawned_at),
        "sandbox_started_at": _telemetry_timestamp(sandbox_started_at),
        "runner_command_started_at": _telemetry_timestamp(runner_command_started_at),
        "busy_at": _telemetry_timestamp(busy_at),
        "completed_at": _telemetry_timestamp(completed_at),
        "allocation_completed_at": _telemetry_timestamp(allocation_completed_at),
        "cleanup_status": cleanup_status,
        "total_secs": round(max(completed_at - cycle_state.spawned_at, 0.0), 3),
        "allocated_secs": round(max(allocation_completed_at - sandbox_started_at, 0.0), 3)
        if sandbox_started_at is not None and allocation_completed_at is not None else None,
        "launch_secs": round(max(runner_command_started_at - sandbox_started_at, 0.0), 3)
        if runner_command_started_at is not None and sandbox_started_at is not None else None,
        "idle_secs": round(max((busy_at or completed_at) - runner_command_started_at, 0.0), 3)
        if (runner_command_started_at is not None
            and (outcome == "idle" or busy_at is not None)) else None,
        "busy_secs": round(max(completed_at - busy_at, 0.0), 3) if busy_at is not None else None,
        "job_phase_observed": busy_at is not None if outcome == "job" else None,
        "runner_exit_code": exit_code,
        "declared_resources": _declared_resources(cls),
    }
    log.info("%s%s", CYCLE_TELEMETRY_PREFIX,
             json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False))


def resolve_volume_id(api_key: str, name: str) -> str | None:
    req = urllib.request.Request("https://app.daytona.io/api/volumes")
    req.add_header("Authorization", f"Bearer {api_key}")
    # Fixed literal URL (Daytona API) — no dynamic scheme/host.
    try:
        with open_without_redirects(req, timeout=30) as response:
            volumes = json.load(response)
    except urllib.error.HTTPError as exc:
        status = exc.code
        exc.close()
        raise RuntimeError(f"Daytona volume inventory failed with HTTP {status}") from None
    for v in volumes:
        if v.get("name") == name:
            return v.get("id")
    log.warning("cache volume %r not found; runners will have no shared cache", name)
    return None


def is_fleet_sandbox(labels: object) -> bool:
    """Whether a Daytona sandbox belongs to this controller instance.

    Organization fleets retain the established owner label and deliberately
    ignore repository-scoped sandboxes. Repository profiles additionally carry
    an exact fleet label, so two profiles for one personal account cannot reap
    one another's work.
    """
    if not isinstance(labels, dict) or labels.get("role") != "gha-runner":
        return False
    if GITHUB_SCOPE == GITHUB_SCOPE_REPOSITORY:
        return labels.get("scope") == GITHUB_SCOPE_REPOSITORY and labels.get("fleet") == FLEET_LABEL
    return labels.get("org") == ORG_LABEL and labels.get("scope") != GITHUB_SCOPE_REPOSITORY


def is_fleet_runner(gh: GitHub, item: dict) -> bool:
    """Whether an offline registration is safe for this fleet to delete."""
    if gh.scope != GITHUB_SCOPE_REPOSITORY:
        return True  # preserves the organization fleet contract
    labels = item.get("labels", [])
    return any(
        isinstance(label, dict) and label.get("name") == gh.fleet_runner_label()
        for label in labels
    )


def reap(gh: GitHub, sdk: DaytonaSDK) -> None:
    """Clean stale registrations and only this fleet's leftover sandboxes.

    Repository scope enumerates just its configured repositories. It cannot
    list or delete any other repository's registrations.
    """
    dr = 0
    while True:
        found = 0
        for runner, item in gh.runners():
            if (item["status"] == "offline" and item["name"].startswith("marsh-")
                    and any(label["name"] == "daytona" for label in item["labels"])
                    and is_fleet_runner(gh, item)):
                if not _runner_delete_confirmed(gh, runner):
                    raise RuntimeError("could not confirm stale GitHub runner deregistration")
                dr += 1
                found += 1
        if found == 0:
            break
    ds = 0
    for sb in list(sdk.list()):
        if is_fleet_sandbox(getattr(sb, "labels", None) or {}):
            try:
                sb.delete()
                ds += 1
            except Exception:  # noqa: BLE001
                raise RuntimeError("could not confirm stale Daytona sandbox deletion") from None
    log.info("reap: removed %d offline daytona runners, %d leftover gha-runner sandboxes", dr, ds)


ORG_LABEL = ""  # set in main() from [github].org/owner; scopes organization cleanup.
FLEET_LABEL = ""  # set in main() for repository scope; scopes same-owner profiles exactly.
GITHUB_SCOPE = GITHUB_SCOPE_ORGANIZATION
ORPHAN_SWEEP_GRACE_SECS = 180  # skip sandboxes younger than this -- closes the TOCTOU
                                # window between create_sandbox() returning and the
                                # cycle thread's REGISTRY_LOCK-protected sandbox_id write


def _sandbox_age_secs(sb) -> float | None:
    """Best-effort sandbox age from its `created_at` field (an ISO 8601 string per the
    SDK's own field description; exact wire format not confirmed against a live
    sandbox, so this is deliberately defensive). Returns None -- "unknown age" -- if
    the field is absent or doesn't parse, so callers fail open to their prior
    (registry-membership-only) behavior rather than mis-skipping or mis-deleting."""
    ts = getattr(sb, "created_at", None)
    if not ts:
        return None
    try:
        created = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - created).total_seconds()
    except (ValueError, TypeError):
        return None


def orphan_sweep(gh: GitHub, sdk: DaytonaSDK) -> None:
    """Runs every ~10min from the main loop. Registry-aware generalization of reap():
    catches sandboxes/registrations that leaked past a cycle thread's best-effort
    `finally` cleanup (e.g. a hard process kill mid-cycle) without ever touching a
    sandbox_id/runner_id the registry still tracks. Also skips brand-new gha-runner
    sandboxes (< ORPHAN_SWEEP_GRACE_SECS old) regardless of registry membership: a
    sweep can run in the sub-millisecond gap between a cycle's create_sandbox() call
    returning and its REGISTRY_LOCK-protected write of sandbox_id, which would
    otherwise look identical to a genuine orphan for one sweep pass."""
    with REGISTRY_LOCK:
        live_sandboxes = {c.sandbox_id for c in REGISTRY.values() if c.sandbox_id}
        live_runners = {c.runner for c in REGISTRY.values() if c.runner}

    ds = 0
    for sb in list(sdk.list()):
        lbl = getattr(sb, "labels", None) or {}
        if not (is_fleet_sandbox(lbl) and sb.id not in live_sandboxes):
            continue
        age = _sandbox_age_secs(sb)
        if age is not None and age < ORPHAN_SWEEP_GRACE_SECS:
            continue  # too young to be a confirmed orphan -- a cycle may still be registering it
        try:
            sb.delete()
            ds += 1
        except Exception:  # noqa: BLE001
            raise RuntimeError("could not confirm orphan Daytona sandbox deletion") from None

    dr = 0
    while True:
        found = 0
        for runner, item in gh.runners():
            if (item["status"] == "offline" and item["name"].startswith("marsh-")
                    and any(label["name"] == "daytona" for label in item["labels"])
                    and is_fleet_runner(gh, item)
                    and runner not in live_runners):
                if not _runner_delete_confirmed(gh, runner):
                    raise RuntimeError("could not confirm orphan GitHub runner deregistration")
                dr += 1
                found += 1
        if found == 0:
            break

    if ds or dr:
        log.info("orphan sweep: removed %d untracked sandboxes, %d untracked offline runners", ds, dr)


def cycle(cycle_id: str, cls: dict, gh: GitHub, dt: Daytona, group_id: int,
          busy_map: BusyMap, lc: Lifecycle, stop: threading.Event,
          repository: str | None = None, admission: threading.Event | None = None,
          status_wake: threading.Event | None = None) -> None:
    """One runner's full life, run exactly once: mint -> sandbox -> session-exec ->
    poll (job pickup / natural exit / idle+job deadlines) -> teardown. This thread is
    the sole writer of its own registry entry (the poller only reads entries and
    inserts new SPAWNING ones), so no cross-cycle locking races beyond REGISTRY_LOCK
    itself. Never retries internally — a failed or expired cycle just removes itself;
    the next poller tick sees the resulting deficit and spawns a fresh one."""
    runner = None
    sandbox = None
    jit_mint_attempted = False
    sandbox_create_attempted = False
    sandbox_started_at = None
    runner_command_started_at = None
    exit_code = None
    outcome = "failed"
    termination_reason = "cycle_failed"
    shutdown_busy_confirmation_logged = False

    with REGISTRY_LOCK:
        initial_state = REGISTRY.get(cycle_id)
    if initial_state is None:
        # Defensive only: spawn_cycle inserts the entry before starting this thread.
        initial_state = Cycle(cls_name=str(cls.get("name", "unknown")), state="SPAWNING",
                              idle_deadline_secs=0, repository=repository)

    def hold_for_cancelled_busy_confirmation() -> None:
        """Leave the cycle intact when shutdown interrupts its last safety check.

        Destroying a sandbox without a fresh busy-state confirmation could kill a
        job GitHub assigned in the idle-teardown race window. Keep the registration
        and sandbox intact instead; the existing systemd TimeoutStopSec remains the
        stop-path boundary, and startup recovery owns any subsequent cleanup.
        """
        nonlocal shutdown_busy_confirmation_logged
        if not shutdown_busy_confirmation_logged:
            log.info("[%s] shutdown cancelled GitHub busy-state confirmation; retaining "
                     "runner and sandbox without teardown until systemd's stop boundary",
                     cls["name"])
            shutdown_busy_confirmation_logged = True

    try:
        # A cycle inserted immediately before SIGUSR1 may not have minted a JIT
        # registration yet.  It is safe to discard that unstarted admission;
        # once JIT minting begins the finally block owns cleanup instead.
        if not _admission_open(admission):
            outcome = "idle"
            termination_reason = "quiesce"
            log.info("[%s] quiesce hit before JIT mint; discarding unstarted cycle", cls["name"])
            return
        jit_mint_attempted = True
        runner, jit = gh.mint_jit(group_id, cls["labels"], repository)
        with REGISTRY_LOCK:
            REGISTRY[cycle_id].runner = runner
        _wake_status(status_wake)

        # Do not create provider capacity after admission has closed.  The JIT
        # registration above is safe to delete in finally and avoids a runner
        # that can accept new work while the fleet is quiesced.
        if not _admission_open(admission):
            outcome = "idle"
            termination_reason = "quiesce"
            log.info("[%s] quiesce hit after JIT mint; deleting registration without sandbox", cls["name"])
            return

        sandbox_create_started_at = _cycle_now()
        sandbox_create_attempted = True
        sandbox = dt.create_sandbox(cls["snapshot"], lc.auto_stop_minutes, cls.get("name", ""), repository)
        # Count allocation from the request that successfully produced a
        # sandbox. A failed create or earlier GitHub JIT work must never be
        # multiplied into provider resource-hours.
        sandbox_started_at = sandbox_create_started_at
        with REGISTRY_LOCK:
            REGISTRY[cycle_id].sandbox_id = sandbox.id
        _wake_status(status_wake)

        # A create request already in flight when SIGUSR1 arrives cannot be
        # cancelled safely, but it must not be turned into a runner afterward.
        # Delete this just-created, still-unadmitted sandbox in finally.
        if not _admission_open(admission):
            outcome = "idle"
            termination_reason = "quiesce"
            log.info("[%s] quiesce hit after sandbox create; deleting unstarted sandbox", cls["name"])
            return

        cmd_id = dt.start_runner(sandbox, jit)
        with REGISTRY_LOCK:
            REGISTRY[cycle_id].state = "IDLE"
        _wake_status(status_wake)
        # This is command-launch completion, not proof that GitHub already sees
        # the runner online. Keep the telemetry name explicit about that limit.
        runner_command_started_at = _cycle_now()
        target = f" repository={repository}" if repository else ""
        log.info("[%s] runner up: sandbox=%s runner_id=%s%s idle_deadline=%ds",
                 cls["name"], sandbox.id, runner.runner_id, target, REGISTRY[cycle_id].idle_deadline_secs)

        session_read_errs = 0
        while True:
            time.sleep(15)
            try:
                exit_code = dt.session_exit_code(sandbox, cmd_id)
                session_read_errs = 0
            except Exception:  # noqa: BLE001 — could be "session genuinely gone" OR a
                # transient Daytona-side blip while a job is still running. Deleting the
                # sandbox on a transient read error would fail that job — the same race the
                # GitHub-side gate below fixes, on the Daytona side. So NEVER tear down here
                # while the job may be live: only advance toward "gone" when GitHub EXPLICITLY
                # confirms the runner is idle (busy is False). busy or unconfirmed -> hold and
                # retry. A truly-gone sandbox keeps failing with GitHub idle -> torn down after
                # SESSION_READ_MAX_ERRS. If GitHub is ALSO unreachable (runner_busy None
                # forever) this gate holds indefinitely — BILLING is still bounded by Daytona's
                # server-side auto_stop_minutes, and the stale thread/registration converge once
                # GitHub recovers or the next reap()/orphan_sweep runs.
                try:
                    fresh_busy = gh.runner_busy(runner)
                except GitHubRequestCancelled:
                    hold_for_cancelled_busy_confirmation()
                    continue
                if fresh_busy is not False:
                    session_read_errs = 0
                    continue
                session_read_errs += 1
                if session_read_errs < SESSION_READ_MAX_ERRS:
                    continue
                with REGISTRY_LOCK:
                    observed_busy = REGISTRY[cycle_id].busy_at is not None
                outcome = "job" if observed_busy else "unknown"
                termination_reason = "session_unreadable"
                log.warning("[%s] session unreadable %dx and runner idle per GitHub; treating "
                           "as gone", cls["name"], session_read_errs)
                break
            if exit_code is not None:
                with REGISTRY_LOCK:
                    observed_busy = REGISTRY[cycle_id].busy_at is not None
                outcome = "job" if observed_busy or exit_code == 0 else "failed"
                termination_reason = "runner_exit"
                log.info("[%s] runner exited (code=%s)", cls["name"], exit_code)
                break

            with REGISTRY_LOCK:
                c = REGISTRY[cycle_id]
                now = _cycle_now()
                if c.state == "IDLE" and busy_map.is_busy(runner):
                    c.state, c.busy_at = "BUSY", now
                    _wake_status(status_wake)
                state, busy_at, spawned_at, idle_deadline = c.state, c.busy_at, c.spawned_at, c.idle_deadline_secs

            if state == "BUSY":
                # A running job is never cut short for drain — only the hard ceiling
                # (or the job finishing, caught above) ends it.
                if busy_at is not None and now - busy_at > lc.job_max_secs:
                    outcome = "job"
                    termination_reason = "job_max_secs"
                    log.warning("[%s] job_max_secs (%ds) exceeded; hard teardown",
                               cls["name"], lc.job_max_secs)
                    break
                continue

            # state == IDLE. Two conditions want this runner torn down: a drain signal or
            # the idle deadline. NEITHER may fire on a runner GitHub has just handed a job
            # but whose tick-cached busy_map hasn't caught yet — deleting its sandbox would
            # fail that job. Gate teardown on a FRESH single-runner busy check:
            #   busy  -> it just claimed a job in the race window; promote to BUSY, keep it.
            #   None  -> can't confirm idle (transient API error / TLS timeout / 404); do
            #            NOT tear down — hold and re-check next poll. A truly-stuck runner
            #            is bounded by auto_stop_minutes and (on drain) systemd's SIGKILL.
            #   False -> confirmed idle; safe to tear down.
            quiescing = not _admission_open(admission)
            draining = stop.is_set() or quiescing
            if not (draining or now - spawned_at > idle_deadline):
                continue
            reason = "drain" if stop.is_set() else (
                "quiesce" if quiescing else f"idle deadline ({idle_deadline}s)"
            )
            try:
                fresh_busy = gh.runner_busy(runner)
            except GitHubRequestCancelled:
                hold_for_cancelled_busy_confirmation()
                continue
            if fresh_busy is None:
                log.warning("[%s] %s teardown wanted but busy state unconfirmed; holding",
                           cls["name"], reason)
                continue
            if fresh_busy:
                with REGISTRY_LOCK:
                    c2 = REGISTRY.get(cycle_id)
                    if c2 is not None:
                        c2.state, c2.busy_at = "BUSY", _cycle_now()
                _wake_status(status_wake)
                log.info("[%s] %s teardown averted: runner claimed a job just now; running it",
                        cls["name"], reason)
                continue
            outcome = "idle"
            termination_reason = "drain" if stop.is_set() else (
                "quiesce" if quiescing else "idle_deadline"
            )
            log.info("[%s] %s hit; tearing down (confirmed idle)", cls["name"], reason)
            break
    except Exception:  # noqa: BLE001
        log.exception("[%s] cycle failed", cls["name"])
    finally:
        completed_at = _cycle_now()
        with REGISTRY_LOCK:
            completed_state = REGISTRY.get(cycle_id, initial_state)
        allocation_completed_at = None
        cleanup_status = "create_unconfirmed" if sandbox_create_attempted else "create_not_attempted"
        if sandbox is not None:
            try:
                sandbox.process.delete_session(SESSION_ID)
            except Exception:  # noqa: BLE001
                pass
            try:
                sandbox.delete()
                allocation_completed_at = _cycle_now()
                cleanup_status = "deleted"
            except Exception as e:  # noqa: BLE001
                cleanup_status = "delete_failed"
                log.warning("[%s] sandbox delete failed: %s", cls["name"], str(e)[:80])
        try:
            _log_cycle_telemetry(
                cycle_id, cls, dt, completed_state, sandbox_started_at, runner_command_started_at,
                completed_at, allocation_completed_at, cleanup_status, outcome,
                termination_reason, exit_code,
            )
        except Exception:  # noqa: BLE001 — telemetry must never prevent resource cleanup
            log.warning("[%s] cycle telemetry serialization failed", cls["name"])
        # A provider request may fail after the remote side effect succeeds.
        # Keep that cycle visible until an operator can reconcile it instead of
        # reporting a false empty cohort during a drain.
        cleanup_complete = (
            not (jit_mint_attempted and runner is None)
            and not (sandbox_create_attempted and sandbox is None)
            and cleanup_status != "delete_failed"
        )
        if not cleanup_complete and (
                (jit_mint_attempted and runner is None)
                or (sandbox_create_attempted and sandbox is None)):
            log.warning("[%s] provider side effect was not confirmed; retaining cycle as cleanup pending",
                        cls["name"])
        if runner is not None and not _runner_delete_confirmed(gh, runner):
            cleanup_complete = False
            log.warning("[%s] runner deregistration was not confirmed; retaining cycle as cleanup pending",
                        cls["name"])
        if cleanup_complete:
            with REGISTRY_LOCK:
                REGISTRY.pop(cycle_id, None)
        else:
            # This deliberately remains counted by runtime status. A cohort
            # rollout must not report total=0 while an old sandbox or runner
            # could still exist. Reconciliation treats this non-supply state
            # as a capacity loss rather than masking it with a fresh runner.
            with REGISTRY_LOCK:
                pending = REGISTRY.get(cycle_id)
                if pending is not None:
                    pending.state = "CLEANUP_PENDING"
            log.warning("[%s] cleanup is incomplete; cycle remains visible and blocks a safe drain",
                        cls["name"])
        _wake_status(status_wake)


def spawn_cycle(cls: dict, gh: GitHub, dt: Daytona, group_id: int, busy_map: BusyMap,
                lc: Lifecycle, idle_deadline_secs: int, stop: threading.Event,
                repository: str | None = None, admission: threading.Event | None = None,
                status_wake: threading.Event | None = None) -> None:
    if not _admission_open(admission):
        return
    cycle_id = uuid.uuid4().hex
    with REGISTRY_LOCK:
        # Recheck while creating visible supply so a quiesce that arrives
        # between reconciliation and this call cannot leave a new cycle behind.
        if not _admission_open(admission):
            return
        REGISTRY[cycle_id] = Cycle(cls_name=cls["name"], state="SPAWNING",
                                   idle_deadline_secs=idle_deadline_secs, repository=repository)
    _wake_status(status_wake)
    t = threading.Thread(target=cycle,
                         args=(cycle_id, cls, gh, dt, group_id, busy_map, lc, stop, repository,
                               admission, status_wake),
                         daemon=True, name=f"{cls['name']}-{cycle_id[:8]}")
    try:
        t.start()
    except Exception:  # noqa: BLE001 — e.g. can't start new thread (resource limits)
        # Without this, a phantom SPAWNING entry with no thread behind it would live in
        # REGISTRY forever: permanently over-counting supply (reconcile never spawns a
        # real replacement) and never draining (main()'s tail now blocks until REGISTRY
        # is empty, so this alone would force every shutdown to ride out the full 120s
        # SIGKILL instead of exiting promptly).
        log.warning("[%s] failed to start cycle thread; discarding registry entry", cls["name"])
        with REGISTRY_LOCK:
            REGISTRY.pop(cycle_id, None)
        _wake_status(status_wake)


def _reconcile_organization(gh: GitHub, group_name: str, classes: list[dict], busy_map: BusyMap,
                            dt: Daytona, lc: Lifecycle, stop: threading.Event,
                            required_labels: set[str] | frozenset[str] = frozenset(),
                            admission: threading.Event | None = None,
                            status_wake: threading.Event | None = None) -> None:
    """Existing selected-runner-group reconciliation, unchanged in behavior."""
    if not _admission_open(admission):
        log.info("reconcile skipped: runner admission is quiesced")
        return
    group_id = gh.current_group_id(group_name)
    repos = gh.runner_group_repos(group_id)
    jobs = gh.queued_jobs(repos)
    busy_map.refresh(gh.runners_busy_map())

    demand: dict[str, int] = {c["name"]: 0 for c in classes}
    for queued in jobs:
        job_labels = set(queued.job.get("labels", []))
        if not job_labels:
            continue  # a job with no labels is a subset of every class -- not a real match
        matched = match_class(job_labels, classes, required_labels)
        if matched:
            demand[matched["name"]] += 1

    with REGISTRY_LOCK:
        snapshot = list(REGISTRY.values())

    for cls in classes:
        cycles = [cycle_item for cycle_item in snapshot if cycle_item.cls_name == cls["name"]]
        # Count busy_map (just refreshed above), not just each cycle's own possibly-
        # stale `.state`: a cycle GitHub already handed a job to, but whose thread
        # hasn't noticed yet via its own ~15s poll, would otherwise still count as
        # supply against a *different*, newly queued job and delay its pickup by
        # up to another poll interval or two.
        supply = sum(1 for cycle_item in cycles if cycle_item.state == "SPAWNING"
                     or (cycle_item.state == "IDLE" and not busy_map.is_busy(cycle_item.runner)))
        live = len(cycles)
        min_idle = int(cls.get("min_idle", 0))
        max_live = int(cls.get("max", 10**9))
        queued_n = demand[cls["name"]]

        demand_deficit = queued_n - supply
        floor_deficit = min_idle - supply
        to_spawn = max(demand_deficit, floor_deficit, 0)
        to_spawn = min(to_spawn, max(max_live - live, 0))
        floor_slots = max(min(floor_deficit, to_spawn), 0)

        for index in range(to_spawn):
            if not _admission_open(admission):
                return
            idle_deadline = lc.idle_refresh_secs if index < floor_slots else lc.demand_idle_secs
            spawn_cycle(cls, gh, dt, group_id, busy_map, lc, idle_deadline, stop,
                        admission=admission, status_wake=status_wake)
        if to_spawn:
            log.info("[%s] reconcile: queued=%d supply=%d live=%d max=%d -> +%d (floor=%d demand=%d)",
                     cls["name"], queued_n, supply, live, max_live, to_spawn,
                     floor_slots, to_spawn - floor_slots)


def _reconcile_repositories(gh: GitHub, group_id: int, classes: list[dict], busy_map: BusyMap,
                            dt: Daytona, lc: Lifecycle, stop: threading.Event,
                            required_labels: set[str] | frozenset[str] = frozenset(),
                            admission: threading.Event | None = None,
                            status_wake: threading.Event | None = None) -> None:
    """Reconcile repository JIT demand without ever sharing supply across repos.

    A JIT registration minted through ``/repos/<owner>/<repo>`` can only run that
    repository's work. Demand and idle supply are therefore keyed by
    ``(repository, size class)``. ``max`` remains a fleet-wide class ceiling,
    preventing a busy personal account from multiplying capacity by its number
    of repositories.
    """
    if not _admission_open(admission):
        log.info("reconcile skipped: runner admission is quiesced")
        return
    repositories = gh.configured_repositories()
    jobs = gh.queued_jobs(repositories)
    busy_map.refresh(gh.runners_busy_map())

    demand: dict[tuple[str, str], int] = {
        (repository, cls["name"]): 0 for repository in repositories for cls in classes
    }
    for queued in jobs:
        job_labels = set(queued.job.get("labels", []))
        if not job_labels:
            continue
        matched = match_class(job_labels, classes, required_labels)
        if matched:
            demand[(queued.repository, matched["name"])] += 1

    with REGISTRY_LOCK:
        snapshot = list(REGISTRY.values())

    for cls in classes:
        if int(cls.get("min_idle", 0)) != 0:
            raise ValueError("repository-scoped profiles must set min_idle = 0")
        class_name = cls["name"]
        max_live = int(cls.get("max", 10**9))
        live = sum(1 for cycle_item in snapshot if cycle_item.cls_name == class_name)
        available = max(max_live - live, 0)
        for repository in repositories:
            cycles = [cycle_item for cycle_item in snapshot
                      if cycle_item.cls_name == class_name and cycle_item.repository == repository]
            supply = sum(1 for cycle_item in cycles if cycle_item.state == "SPAWNING"
                         or (cycle_item.state == "IDLE" and not busy_map.is_busy(cycle_item.runner)))
            queued_n = demand[(repository, class_name)]
            to_spawn = min(max(queued_n - supply, 0), available)
            for _ in range(to_spawn):
                if not _admission_open(admission):
                    return
                spawn_cycle(cls, gh, dt, group_id, busy_map, lc, lc.demand_idle_secs, stop,
                            repository, admission=admission, status_wake=status_wake)
            available -= to_spawn
            live += to_spawn
            if to_spawn:
                log.info("[%s] repository=%s reconcile: queued=%d supply=%d live=%d max=%d -> +%d",
                         class_name, repository, queued_n, supply, live, max_live, to_spawn)


def poller_tick(gh: GitHub, runner_group: str | int, classes: list[dict], busy_map: BusyMap,
                dt: Daytona, lc: Lifecycle, stop: threading.Event,
                required_labels: set[str] | frozenset[str] = frozenset(),
                admission: threading.Event | None = None,
                status_wake: threading.Event | None = None) -> bool:
    """One reconcile pass, returning whether it completed successfully.

    Both scopes fail closed on API errors and retry later.  The return value is
    intentionally narrow: it is used only to publish process readiness after
    startup, not to infer that demand was nonzero or that a runner was spawned.
    """
    if not _admission_open(admission):
        log.info("poller tick skipped: runner admission is quiesced")
        return False
    try:
        checkpoint = gh.rate_limit_checkpoint()
        if gh.scope == GITHUB_SCOPE_ORGANIZATION:
            if not isinstance(runner_group, str):
                raise ValueError("organization scope requires a runner-group name")
            _reconcile_organization(gh, runner_group, classes, busy_map, dt, lc, stop,
                                    required_labels, admission, status_wake)
        else:
            group_id = (runner_group if isinstance(runner_group, int)
                        else gh.current_repository_group_id(runner_group))
            _reconcile_repositories(gh, group_id, classes, busy_map, dt, lc, stop,
                                    required_labels, admission, status_wake)
        gh.reset_rate_limit_backoff(checkpoint)
        return True
    except GitHubRequestCancelled:
        log.info("poller tick cancelled during service shutdown")
    except urllib.error.HTTPError as e:
        if e.code in (403, 429):
            log.warning("poller tick: GitHub rate/permission limited (%s); backing off 30s", e.code)
            stop.wait(30)
        else:
            log.exception("poller tick: GitHub API error")
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        # Transient network/TLS blips (e.g. the ~2.5%/tick TLS handshake timeout to
        # api.github.com): the tick is simply skipped and retried next interval, with no
        # effect on running jobs or existing runners. Log ONE line, not a traceback —
        # a full stack trace here made routine idle nights read as dozens of "errors".
        log.warning("poller tick: transient network error (%s); skipping this tick",
                   str(getattr(e, "reason", e))[:100])
    except Exception:  # noqa: BLE001
        log.exception("poller tick failed")
    return False


def start_quiesced_from_environment() -> bool:
    """Return the explicit initial-admission mode without accepting truthy guesses.

    This is intentionally a narrow one-time bootstrap control.  An unset value
    preserves ordinary controller behavior; an invalid value fails the service
    before it can make provider or GitHub calls.  Operators must use ``1`` to
    request a closed start and ``0`` to opt out explicitly.
    """
    value = os.environ.get(START_QUIESCED_ENV)
    if value is None or value == "0":
        return False
    if value == "1":
        return True
    raise RuntimeError(f"{START_QUIESCED_ENV} must be exactly '0' or '1'")


def _runtime_fleet_name(owner: str, configured_fleet_name: str, *, require_explicit: bool = False) -> str:
    """Select the validated filename key for this one fleet process.

    Fleet renderer environments set ``MARSH_FLEET_NAME`` for every profile,
    including organization-scoped ones.  Falling back to the validated owner
    keeps hand-run organization profiles observable without accepting a path
    component from configuration.
    """
    explicit_fleet = os.environ.get("MARSH_FLEET_NAME", "").strip()
    if require_explicit and not explicit_fleet:
        raise RuntimeError("MARSH_FLEET_NAME is required when MARSH_START_QUIESCED=1")
    fleet = explicit_fleet or configured_fleet_name or owner.lower()
    if not FLEET_NAME.fullmatch(fleet):
        raise RuntimeError("MARSH_FLEET_NAME (or fleet owner fallback) must be a valid fleet name")
    return fleet


def initialize_provider_runtime(api_key: str, cfg: dict, gh: GitHub, base_labels: dict,
                                network_policy: NetworkPolicy) -> Daytona:
    """Perform the first provider-touching startup work after admission is open.

    A controller started with ``MARSH_START_QUIESCED=1`` must not resolve a
    Daytona volume, reap an orphan, poll GitHub, or create a Daytona client
    until a later ``SIGUSR2`` explicitly opens admission.  Keep every startup
    provider interaction in this one boundary so that property is auditable and
    unit-testable.
    """
    vol_id = resolve_volume_id(api_key, cfg.get("cache", {}).get("volume", "")) if cfg.get("cache") else None
    reap(gh, DaytonaSDK(DaytonaConfig(api_key=api_key, target=cfg["daytona"]["target"])))
    return Daytona(api_key, cfg["daytona"]["target"], vol_id, base_labels=base_labels,
                   network_policy=network_policy)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    with open(os.environ.get("MARSH_RUNNER_CONFIG", "/etc/marsh/runners.toml"), "rb") as fh:
        cfg = tomllib.load(fh)

    github_cfg = cfg["github"]
    scope = github_cfg.get("scope", GITHUB_SCOPE_ORGANIZATION)
    if scope == GITHUB_SCOPE_ORGANIZATION:
        owner = github_cfg["org"]
        repositories: list[str] | None = None
        runner_group: str | int = github_cfg.get("runner_group", "daytona")
        fleet_name = ""
    elif scope == GITHUB_SCOPE_REPOSITORY:
        owner = github_cfg["owner"]
        repositories = list(github_cfg["repositories"])
        if "runner_group" in github_cfg:
            runner_group = str(github_cfg["runner_group"])
        else:
            runner_group = int(github_cfg["runner_group_id"])
        fleet_name = os.environ.get("MARSH_FLEET_NAME", "").strip()
        if not FLEET_NAME.fullmatch(fleet_name):
            raise RuntimeError("MARSH_FLEET_NAME is required and must be a valid fleet name for repository-scoped fleets")
    else:
        raise ValueError(f"unsupported GitHub scope {scope!r}")

    api_key = os.environ["DAYTONA_API_KEY"]
    poller_cfg = cfg.get("poller", {})
    request_spacing_secs = poller_cfg.get("request_spacing_secs", 0)
    start_quiesced = start_quiesced_from_environment()
    control = RuntimeControl(start_quiesced=start_quiesced)
    signal.signal(signal.SIGTERM, lambda *_: control.request_stop())
    signal.signal(signal.SIGINT, lambda *_: control.request_stop())
    signal.signal(signal.SIGUSR1, lambda *_: control.quiesce())
    signal.signal(signal.SIGUSR2, lambda *_: control.resume())
    gh = GitHub(owner, os.environ["GH_APP_ID"], os.environ["GH_APP_INSTALLATION_ID"],
                os.environ["GH_APP_KEY_PATH"], scope, repositories, fleet_name or None,
                request_spacing_secs=request_spacing_secs, stop_event=control.stop)

    lcfg = cfg.get("lifecycle", {})
    lc = Lifecycle(
        job_max_secs=int(lcfg.get("job_max_secs", 3600)),
        auto_stop_minutes=int(lcfg.get("auto_stop_minutes", 120)),
        demand_idle_secs=int(lcfg.get("demand_idle_secs", 300)),
        idle_refresh_secs=int(lcfg.get("idle_refresh_secs", 1800)),
    )
    poll_default = 60 if scope == GITHUB_SCOPE_REPOSITORY else 20
    poll_interval = int(poller_cfg.get("interval_secs", poll_default))
    classes = cfg["size_class"]
    required_labels = routing_required_labels(cfg)
    network_policy = network_policy_from_config(cfg)

    global ORG_LABEL, FLEET_LABEL, GITHUB_SCOPE
    ORG_LABEL = owner.lower()
    GITHUB_SCOPE = scope
    if scope == GITHUB_SCOPE_REPOSITORY:
        FLEET_LABEL = fleet_name
    else:
        FLEET_LABEL = ""
    runtime_fleet = _runtime_fleet_name(owner, fleet_name, require_explicit=start_quiesced)
    ready = threading.Event()
    cleanup_healthy = threading.Event()
    if start_quiesced:
        # The process has parsed its profile, installed its signal handlers,
        # and opened no provider/GitHub control path.  It is safe to hand it
        # off only as a closed controller until SIGUSR2 explicitly resumes it.
        ready.set()

    # Make the runtime contract available before startup reconciliation performs
    # any provider work.  A bad/malicious status path therefore fails service
    # startup rather than silently defeating drain-aware deployment control.
    write_runtime_status(runtime_fleet, control.admission, ready)

    def publish_runtime_status() -> None:
        try:
            write_runtime_status(runtime_fleet, control.admission, ready)
        except (OSError, RuntimeError, ValueError):
            # The status payload has no dynamic strings, but do not log an OS
            # error path/value here either: observability must never become a
            # route for configuration or credential disclosure.
            log.error("runtime status write failed")

    base_labels = {"org": ORG_LABEL}
    if scope == GITHUB_SCOPE_REPOSITORY:
        base_labels.update({"scope": GITHUB_SCOPE_REPOSITORY, "fleet": FLEET_LABEL})
    if network_policy.restricted:
        base_labels["network_policy"] = "deny-by-default"
    busy_map = BusyMap()
    dt: Daytona | None = None
    provider_initialized = False

    def initialize_provider() -> bool:
        nonlocal dt, provider_initialized
        if provider_initialized:
            return True
        try:
            dt = initialize_provider_runtime(api_key, cfg, gh, base_labels, network_policy)
        except GitHubRequestCancelled:
            log.info("startup reap cancelled during service shutdown")
            return False
        cleanup_healthy.set()
        provider_initialized = True
        log.info("orchestrator provider runtime initialized: scope=%s owner=%s "
                 "runner_group=%s classes=%s poll=%ds request_spacing=%.1fs network=%s",
                 scope, owner, runner_group, [c["name"] for c in classes], poll_interval,
                 gh.request_spacing_secs, "restricted" if network_policy.restricted else "standard")
        return True

    if not start_quiesced and not initialize_provider():
        return
    if start_quiesced:
        log.info("orchestrator started quiesced; waiting for SIGUSR2 before provider initialization")

    last_sweep = 0.0
    reconciled = False
    while not control.stop.is_set():
        if control.refresh.is_set():
            # Clear before the provider call so a SIGUSR2 arriving while it is
            # in flight schedules another fresh pass instead of being lost.
            control.refresh.clear()
            gh.invalidate_reconciliation_cache()
        if control.admission.is_set() and not provider_initialized:
            if not initialize_provider():
                break
        if control.admission.is_set():
            if dt is None:
                raise RuntimeError("provider runtime was not initialized after admission opened")
            if poller_tick(gh, runner_group, classes, busy_map, dt, lc, control.stop,
                           required_labels, control.admission, control.wake):
                reconciled = True
        if (not control.stop.is_set() and control.admission.is_set() and dt is not None
                and time.time() - last_sweep > 600):
            try:
                orphan_sweep(gh, dt.sdk)
                cleanup_healthy.set()
            except GitHubRequestCancelled:
                log.info("orphan sweep cancelled during service shutdown")
            except Exception:  # noqa: BLE001
                cleanup_healthy.clear()
                ready.clear()
                log.exception("orphan sweep failed")
            last_sweep = time.time()
        if reconciled and cleanup_healthy.is_set():
            ready.set()
        publish_runtime_status()
        control.wake.wait(poll_interval)
        control.wake.clear()

    # Drain: no new spawns happen once the loop above exits. IDLE/SPAWNING cycles
    # notice `stop` on their own next ~15s poll and tear themselves down; BUSY cycles
    # deliberately ignore `stop` and keep running until job_max_secs, natural exit, or
    # systemd's TimeoutStopSec=120 SIGKILLs the process. That SIGKILL is the *only*
    # cutoff here on purpose: cycle threads are daemon=True, so their `finally` cleanup
    # (sandbox delete, runner deregister) never runs if this function returns and the
    # interpreter exits while they're still alive -- returning early would silently
    # orphan every still-running sandbox with zero cleanup attempt, worse than doing
    # nothing. So block on the registry actually draining to empty, with no artificial
    # deadline of our own; systemd killing the whole process is what bounds this.
    log.info("draining: no new spawns; blocking until every cycle clears (idle/spawning "
             "ones self-teardown within ~15s; busy ones run until job completion, "
             "job_max_secs, or systemd's SIGKILL at TimeoutStopSec=120 -- that kill is "
             "the only cutoff, there is no timeout in this loop)")
    last_log = time.time()
    while True:
        with REGISTRY_LOCK:
            remaining = len(REGISTRY)
            busy = sum(1 for c in REGISTRY.values() if c.state == "BUSY")
        if remaining == 0:
            break
        if time.time() - last_log > 30:
            log.info("draining: %d cycle(s) still up (%d busy)", remaining, busy)
            last_log = time.time()
        publish_runtime_status()
        control.wake.wait(2)
        control.wake.clear()
    publish_runtime_status()
    log.info("drain complete; registry empty")


if __name__ == "__main__":
    main()
