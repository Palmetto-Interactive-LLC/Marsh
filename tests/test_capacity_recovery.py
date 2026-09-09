from __future__ import annotations

import json
import unittest
import urllib.error
from email.message import Message
from unittest.mock import patch

from tests.support import load_orchestrator


orch = load_orchestrator("marsh_capacity_recovery_test")

CAPACITY_REFUSAL = "Failed to create sandbox: Total CPU limit exceeded. Maximum allowed: 250."


class Stop:
    def is_set(self) -> bool:
        return False

    def wait(self, seconds: float) -> bool:
        return False


class BusyMap:
    def is_busy(self, runner) -> bool:
        return False


class Sandbox:
    def __init__(self, sandbox_id: str = "sandbox-recovered") -> None:
        self.id = sandbox_id
        self.deleted = False
        self.labels = {"role": "gha-runner", "org": "example-org", orch.CYCLE_LABEL: "cycle"}

    class Process:
        def delete_session(self, session_id: str) -> None:
            pass

    process = Process()

    def delete(self) -> None:
        self.deleted = True


class GitHub:
    def __init__(self) -> None:
        self.runner = orch.RunnerRef(7)
        self.minted_names: list[str | None] = []
        self.deleted: list[orch.RunnerRef] = []

    def mint_jit(self, group_id, labels, repository=None, name=None):
        self.minted_names.append(name)
        return self.runner, "jit"

    def runner_busy(self, runner):
        return False

    def delete_runner(self, runner) -> bool:
        self.deleted.append(runner)
        return True


def telemetry(lines: list[str]) -> dict:
    line = next(line for line in lines if orch.CYCLE_TELEMETRY_PREFIX in line)
    return json.loads(line.split(orch.CYCLE_TELEMETRY_PREFIX, 1)[1])


SIZE_CLASS = {
    "name": "large",
    "labels": ["self-hosted", "daytona", "large"],
    "snapshot": "example-runner-large",
    "cpu": 4,
    "memory_gib": 8,
    "disk_gib": 10,
}


class CreateFailureRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        orch.clear_provider_capacity_backoff()
        orch.ORPHAN_SWEEP_OK_AT = None
        with orch.REGISTRY_LOCK:
            orch.REGISTRY.clear()

    tearDown = setUp

    def register(self, cycle_id: str) -> None:
        with orch.REGISTRY_LOCK:
            orch.REGISTRY[cycle_id] = orch.Cycle(cls_name="large", state="SPAWNING", idle_deadline_secs=5)

    def run_cycle(self, cycle_id: str, gh: GitHub, dt) -> list[str]:
        with patch.object(orch, "_cycle_now", return_value=120.0), \
             self.assertLogs("marsh-orch", level="INFO") as logs:
            orch.cycle(cycle_id, SIZE_CLASS, gh, dt, 1, BusyMap(),
                       orch.Lifecycle(3600, 120, 300, 1800), Stop())
        return logs.output

    def test_refused_create_confirmed_absent_releases_slot_and_starts_backoff(self) -> None:
        class Daytona:
            base_labels = {"org": "example-org"}
            lookups: list[str] = []

            def create_sandbox(self, *args, **kwargs):
                raise RuntimeError(CAPACITY_REFUSAL)

            def find_cycle_sandbox(self, cycle_id: str):
                self.lookups.append(cycle_id)
                return None

        gh = GitHub()
        dt = Daytona()
        self.register("refused")
        lines = self.run_cycle("refused", gh, dt)

        event = telemetry(lines)
        self.assertEqual(event["outcome"], "failed")
        self.assertEqual(event["termination_reason"], "provider_capacity")
        self.assertEqual(event["cleanup_status"], "create_failed")
        self.assertIsNone(event["allocated_secs"])
        self.assertEqual(dt.lookups, ["refused"])
        self.assertEqual(gh.deleted, [gh.runner])
        self.assertEqual(gh.minted_names, ["marsh-refused"])
        self.assertGreater(orch.provider_capacity_backoff_remaining(), 0.0)
        self.assertNotIn("refused", orch.REGISTRY)
        self.assertFalse(any("Traceback" in line for line in lines))

    def test_failed_create_with_existing_sandbox_adopts_and_deletes_it(self) -> None:
        recovered = Sandbox()

        class Daytona:
            base_labels = {"org": "example-org"}

            def create_sandbox(self, *args, **kwargs):
                raise TimeoutError("Function 'create' exceeded timeout of 180 seconds")

            def find_cycle_sandbox(self, cycle_id: str):
                return recovered

        self.register("timed-out")
        lines = self.run_cycle("timed-out", GitHub(), Daytona())

        event = telemetry(lines)
        self.assertEqual(event["termination_reason"], "create_failed")
        self.assertEqual(event["cleanup_status"], "deleted")
        self.assertTrue(recovered.deleted)
        self.assertEqual(orch.provider_capacity_backoff_remaining(), 0.0)
        self.assertNotIn("timed-out", orch.REGISTRY)

    def test_unconfirmed_create_stays_pending_until_a_sweep_proves_it_clean(self) -> None:
        class Daytona:
            base_labels = {"org": "example-org"}

            def create_sandbox(self, *args, **kwargs):
                raise RuntimeError("provider unreachable")

        gh = GitHub()
        self.register("unknown")
        self.run_cycle("unknown", gh, Daytona())
        entry = orch.REGISTRY["unknown"]
        self.assertEqual(entry.state, "CLEANUP_PENDING")
        self.assertTrue(entry.sandbox_unconfirmed)
        self.assertFalse(entry.runner_unconfirmed)

        self.assertEqual(orch.resolve_cleanup_pending(gh, Daytona()), 0)
        self.assertIn("unknown", orch.REGISTRY)

        # A full sweep that started inside the grace window proves nothing.
        orch.ORPHAN_SWEEP_OK_AT = entry.pending_since + 10
        self.assertEqual(orch.resolve_cleanup_pending(gh, Daytona()), 0)
        # One that ran after the grace window would have reaped any leftover.
        orch.ORPHAN_SWEEP_OK_AT = entry.pending_since + orch.ORPHAN_SWEEP_GRACE_SECS + 1
        with self.assertLogs("marsh-orch", level="INFO") as logs:
            self.assertEqual(orch.resolve_cleanup_pending(gh, Daytona()), 1)
        self.assertNotIn("unknown", orch.REGISTRY)
        self.assertTrue(any("released 1 cycle" in line for line in logs.output))

    def test_resolver_retries_known_sandbox_and_runner_cleanup(self) -> None:
        class Daytona:
            base_labels = {"org": "example-org"}
            deleted: list[str] = []
            answers = iter((False, True))

            def delete_sandbox_id(self, sandbox_id: str) -> bool:
                self.deleted.append(sandbox_id)
                return next(self.answers)

        class GitHubFlaky(GitHub):
            answers = iter((False, True))

            def delete_runner(self, runner) -> bool:
                return next(self.answers)

        with orch.REGISTRY_LOCK:
            orch.REGISTRY["stuck"] = orch.Cycle(
                cls_name="large", state="CLEANUP_PENDING", idle_deadline_secs=5,
                runner=orch.RunnerRef(9), sandbox_id="sandbox-stuck",
                sandbox_unconfirmed=True, runner_unconfirmed=True, pending_since=1.0,
            )
        gh = GitHubFlaky()
        dt = Daytona()
        self.assertEqual(orch.resolve_cleanup_pending(gh, dt), 0)
        self.assertEqual(orch.REGISTRY["stuck"].state, "CLEANUP_PENDING")
        self.assertEqual(orch.resolve_cleanup_pending(gh, dt), 1)
        self.assertEqual(dt.deleted, ["sandbox-stuck", "sandbox-stuck"])
        self.assertNotIn("stuck", orch.REGISTRY)

    def test_github_error_response_on_mint_is_a_confirmed_non_registration(self) -> None:
        class GitHubRefuses(GitHub):
            def mint_jit(self, group_id, labels, repository=None, name=None):
                raise urllib.error.HTTPError("https://api.github.com/x", 422, "Unprocessable", Message(), None)

        class Daytona:
            base_labels = {"org": "example-org"}

            def create_sandbox(self, *args, **kwargs):
                raise AssertionError("create must not run after a refused mint")

        self.register("no-runner")
        lines = self.run_cycle("no-runner", GitHubRefuses(), Daytona())
        self.assertEqual(telemetry(lines)["cleanup_status"], "create_not_attempted")
        self.assertNotIn("no-runner", orch.REGISTRY)


class CapacityBackoffReconcileTests(unittest.TestCase):
    def setUp(self) -> None:
        orch.clear_provider_capacity_backoff()
        with orch.REGISTRY_LOCK:
            orch.REGISTRY.clear()

    tearDown = setUp

    def test_reconcile_defers_spawns_while_provider_capacity_backoff_is_active(self) -> None:
        class GitHubDemand:
            scope = orch.GITHUB_SCOPE_ORGANIZATION

            def current_group_id(self, name):
                return 4

            def runner_group_repos(self, group_id):
                return ["service"]

            def queued_jobs(self, repos):
                return [orch.QueuedJob("service", {"labels": ["self-hosted", "daytona", "large"]})
                        for _ in range(3)]

            def runners_busy_map(self):
                return {}

        spawned = []
        classes = [dict(SIZE_CLASS, min_idle=0, max=10)]
        lc = orch.Lifecycle(3600, 120, 300, 1800)
        with patch.object(orch, "spawn_cycle", lambda *args, **kwargs: spawned.append(args)):
            self.assertTrue(orch.note_provider_capacity_error(RuntimeError(CAPACITY_REFUSAL), backoff_secs=60))
            with self.assertLogs("marsh-orch", level="WARNING") as logs:
                orch._reconcile_organization(GitHubDemand(), "daytona", classes, orch.BusyMap(),
                                             object(), lc, Stop())
            self.assertEqual(spawned, [])
            self.assertTrue(any("deferring +3" in line for line in logs.output))

            orch.clear_provider_capacity_backoff()
            orch._reconcile_organization(GitHubDemand(), "daytona", classes, orch.BusyMap(),
                                         object(), lc, Stop())
            self.assertEqual(len(spawned), 3)

    def test_only_capacity_refusals_start_a_backoff(self) -> None:
        self.assertFalse(orch.note_provider_capacity_error(RuntimeError("connection reset")))
        self.assertEqual(orch.provider_capacity_backoff_remaining(), 0.0)
        self.assertTrue(orch.note_provider_capacity_error(RuntimeError(CAPACITY_REFUSAL), backoff_secs=5))
        self.assertLessEqual(orch.provider_capacity_backoff_remaining(), 5.0)


class ConditionalRequestTests(unittest.TestCase):
    def test_conditional_get_replays_cached_body_on_304_without_charging_budget(self) -> None:
        client = orch.GitHub("example-org", "app", "installation", "/key")
        seen: list[dict[str, str]] = []

        class Response:
            def __init__(self, payload: dict, etag: str) -> None:
                self.payload = payload
                self.headers = {"ETag": etag}

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self) -> bytes:
                return json.dumps(self.payload).encode()

        def urlopen(request, timeout):
            seen.append(dict(request.header_items()))
            if len(seen) == 1:
                return Response({"workflow_runs": [{"id": 1}]}, 'W/"abc"')
            if len(seen) == 2:
                raise urllib.error.HTTPError(request.full_url, 304, "Not Modified", Message(), None)
            return Response({"workflow_runs": []}, 'W/"def"')

        with patch.object(orch.urllib.request, "urlopen", urlopen):
            first = client._api("GET", "/repos/example-org/service/actions/runs?status=queued", bearer="t")
            second = client._api("GET", "/repos/example-org/service/actions/runs?status=queued", bearer="t")
            third = client._api("GET", "/repos/example-org/service/actions/runs?status=queued", bearer="t")

        self.assertEqual(first, {"workflow_runs": [{"id": 1}]})
        self.assertEqual(second, first)
        self.assertEqual(third, {"workflow_runs": []})
        self.assertNotIn("If-none-match", seen[0])
        self.assertEqual(seen[1].get("If-none-match"), 'W/"abc"')
        self.assertEqual(seen[2].get("If-none-match"), 'W/"abc"')
        self.assertEqual(client._etag_cache[
            "https://api.github.com/repos/example-org/service/actions/runs?status=queued"][0], 'W/"def"')

    def test_non_get_requests_are_never_cached(self) -> None:
        client = orch.GitHub("example-org", "app", "installation", "/key")

        class Response:
            headers = {"ETag": 'W/"post"'}

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self) -> bytes:
                return b'{"ok": true}'

        with patch.object(orch.urllib.request, "urlopen", lambda request, timeout: Response()):
            client._api("POST", "/orgs/example-org/actions/runners/generate-jitconfig", {"name": "x"}, bearer="t")
        self.assertEqual(client._etag_cache, {})


class OrphanSweepToleranceTests(unittest.TestCase):
    def test_sweep_continues_past_already_deleted_sandboxes_and_records_success(self) -> None:
        class Gone(Sandbox):
            def delete(self) -> None:
                raise RuntimeError("Failed to delete sandbox: not found")

        survivors = [Gone("gone"), Sandbox("orphan")]
        for sb in survivors:
            sb.labels = {"role": "gha-runner", "org": "example-org"}
            sb.created_at = "2000-01-01T00:00:00Z"

        class Sdk:
            def list(self):
                return survivors

        class GitHubEmpty:
            def runners(self):
                return []

        orch.ORPHAN_SWEEP_OK_AT = None
        with patch.object(orch, "ORG_LABEL", "example-org"), \
             patch.object(orch, "GITHUB_SCOPE", orch.GITHUB_SCOPE_ORGANIZATION), \
             self.assertLogs("marsh-orch", level="INFO") as logs:
            orch.orphan_sweep(GitHubEmpty(), Sdk())
        self.assertTrue(survivors[1].deleted)
        self.assertIsNotNone(orch.ORPHAN_SWEEP_OK_AT)
        self.assertTrue(any("removed 2 untracked sandboxes" in line for line in logs.output))


class TickSpacingTests(unittest.TestCase):
    def test_min_tick_defaults_to_five_seconds_bounded_by_the_poll_interval(self) -> None:
        self.assertEqual(orch.resolve_min_tick_secs({}, 20), 5)
        self.assertEqual(orch.resolve_min_tick_secs({}, 2), 2)
        self.assertEqual(orch.resolve_min_tick_secs({"min_tick_secs": 10}, 20), 10)
        with self.assertRaises(ValueError):
            orch.resolve_min_tick_secs({"min_tick_secs": 30}, 20)
        with self.assertRaises(ValueError):
            orch.resolve_min_tick_secs({"min_tick_secs": 0}, 20)
        with self.assertRaises(ValueError):
            orch.resolve_min_tick_secs({"min_tick_secs": True}, 20)


if __name__ == "__main__":
    unittest.main()
