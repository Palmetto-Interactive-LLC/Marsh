from __future__ import annotations

import json
import os
import stat
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
from unittest.mock import patch

from tests.support import load_orchestrator


orch = load_orchestrator("marsh_cycle_telemetry_test")


class Stop:
    def __init__(self, draining: bool = False) -> None:
        self.draining = draining

    def is_set(self) -> bool:
        return self.draining


class BusyMap:
    def __init__(self, busy: bool) -> None:
        self.busy = busy

    def is_busy(self, runner) -> bool:
        return self.busy


class Sandbox:
    id = "sandbox-test"

    class Process:
        def delete_session(self, session_id: str) -> None:
            pass

    process = Process()

    def delete(self) -> None:
        pass


class GitHub:
    def __init__(self, repository: str | None = None) -> None:
        self.runner = orch.RunnerRef(7, repository)

    def mint_jit(self, group_id: int, labels: list[str], repository: str | None = None):
        return self.runner, "jit"

    def runner_busy(self, runner) -> bool:
        return False

    def delete_runner(self, runner) -> None:
        pass


def telemetry_record(lines: list[str]) -> dict:
    line = next(line for line in lines if orch.CYCLE_TELEMETRY_PREFIX in line)
    return json.loads(line.split(orch.CYCLE_TELEMETRY_PREFIX, 1)[1])


class CycleTelemetryTests(unittest.TestCase):
    def test_volume_lookup_never_forwards_daytona_credential_on_redirect(self) -> None:
        requests = []

        def redirected(request, timeout):
            requests.append(request)
            raise urllib.error.HTTPError(
                request.full_url, 302, "redirect refused",
                {"Location": "https://attacker.example/collect"}, None,
            )

        with patch.object(orch, "open_without_redirects", side_effect=redirected):
            with self.assertRaisesRegex(RuntimeError, "HTTP 302"):
                orch.resolve_volume_id("injected-secret", "cache")

        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].full_url, "https://app.daytona.io/api/volumes")
        self.assertEqual(requests[0].get_header("Authorization"), "Bearer injected-secret")
        self.assertIsNone(
            orch.RejectRedirects().redirect_request(
                requests[0], None, 302, "redirect refused",
                {"Location": "https://attacker.example/collect"},
                "https://attacker.example/collect",
            )
        )

    def tearDown(self) -> None:
        with orch.REGISTRY_LOCK:
            orch.REGISTRY.clear()

    def register(self, cycle_id: str, repository: str | None = None) -> None:
        with orch.REGISTRY_LOCK:
            orch.REGISTRY.clear()
            orch.REGISTRY[cycle_id] = orch.Cycle(
                cls_name="default",
                state="SPAWNING",
                idle_deadline_secs=5,
                repository=repository,
                spawned_at=100.0,
            )

    def test_job_completion_emits_snapshot_profile_repository_and_durations(self) -> None:
        class Daytona:
            base_labels = {"org": "example-org"}

            def __init__(self) -> None:
                self.exit_codes = iter((None, 0))

            def create_sandbox(self, *args, **kwargs):
                return Sandbox()

            def start_runner(self, sandbox, jit: str) -> str:
                return "command-test"

            def session_exit_code(self, sandbox, command_id: str) -> int | None:
                return next(self.exit_codes)

        cycle_id = "job-cycle"
        self.register(cycle_id, "alpha")
        size_class = {
            "name": "default",
            "labels": ["self-hosted", "daytona"],
            "snapshot": "example-runner-default",
            "cpu": 2,
            "memory_gib": 4,
            "disk_gib": 10,
        }

        # Clock marks: jit start/end, create start/ready, runner start, busy, complete, teardown.
        with patch.object(orch.time, "sleep", lambda seconds: None), \
             patch.object(orch, "_cycle_now", side_effect=(
                 101.0, 103.0, 105.0, 108.0, 110.0, 120.0, 140.0, 145.0,
             )), \
             self.assertLogs("marsh-orch", level="INFO") as logs:
            orch.cycle(
                cycle_id, size_class, GitHub("alpha"), Daytona(), 1, BusyMap(True),
                orch.Lifecycle(3600, 120, 300, 1800), Stop(), "alpha",
            )

        event = telemetry_record(logs.output)
        self.assertEqual(event["event"], "runner_cycle_complete")
        self.assertEqual(event["schema_version"], 1)
        self.assertEqual(event["profile"], "example-org")
        self.assertEqual(event["scope"], "organization")
        self.assertEqual(event["size_class"], "default")
        self.assertEqual(event["snapshot"], "example-runner-default")
        self.assertEqual(event["repository"], "alpha")
        self.assertEqual(event["outcome"], "job")
        self.assertEqual(event["termination_reason"], "runner_exit")
        self.assertEqual(event["runner_exit_code"], 0)
        self.assertEqual(event["total_secs"], 40.0)
        self.assertEqual(event["allocated_secs"], 40.0)
        self.assertEqual(event["cleanup_status"], "deleted")
        self.assertEqual(event["jit_mint_secs"], 2.0)
        self.assertEqual(event["sandbox_create_secs"], 3.0)
        self.assertEqual(event["runner_start_secs"], 2.0)
        self.assertEqual(event["launch_secs"], 5.0)
        self.assertEqual(event["idle_secs"], 10.0)
        self.assertEqual(event["busy_secs"], 20.0)
        self.assertEqual(event["teardown_secs"], 5.0)
        self.assertTrue(event["job_phase_observed"])
        self.assertEqual(event["declared_resources"], {"cpu": 2, "disk_gib": 10, "memory_gib": 4})
        self.assertEqual(event["started_at"], "1970-01-01T00:01:40.000Z")
        self.assertEqual(event["jit_started_at"], "1970-01-01T00:01:41.000Z")
        self.assertEqual(event["jit_completed_at"], "1970-01-01T00:01:43.000Z")
        self.assertEqual(event["sandbox_started_at"], "1970-01-01T00:01:45.000Z")
        self.assertEqual(event["sandbox_ready_at"], "1970-01-01T00:01:48.000Z")
        self.assertEqual(event["runner_command_started_at"], "1970-01-01T00:01:50.000Z")
        self.assertEqual(event["busy_at"], "1970-01-01T00:02:00.000Z")
        self.assertEqual(event["completed_at"], "1970-01-01T00:02:20.000Z")
        self.assertEqual(event["allocation_completed_at"], "1970-01-01T00:02:25.000Z")

    def test_idle_deadline_is_distinct_from_job_outcome(self) -> None:
        class Daytona:
            base_labels = {"scope": "repository", "fleet": "personal-fleet"}

            def create_sandbox(self, *args, **kwargs):
                return Sandbox()

            def start_runner(self, sandbox, jit: str) -> str:
                return "command-test"

            def session_exit_code(self, sandbox, command_id: str) -> None:
                return None

        cycle_id = "idle-cycle"
        self.register(cycle_id, "alpha")
        size_class = {
            "name": "large",
            "labels": ["self-hosted", "daytona", "large"],
            "snapshot": "example-runner-large",
        }

        with patch.object(orch.time, "sleep", lambda seconds: None), \
             patch.object(orch, "_cycle_now", side_effect=(
                 101.0, 103.0, 105.0, 107.0, 110.0, 120.0, 130.0, 135.0,
             )), \
             self.assertLogs("marsh-orch", level="INFO") as logs:
            orch.cycle(
                cycle_id, size_class, GitHub("alpha"), Daytona(), 1, BusyMap(False),
                orch.Lifecycle(3600, 120, 300, 1800), Stop(), "alpha",
            )

        event = telemetry_record(logs.output)
        self.assertEqual(event["profile"], "personal-fleet")
        self.assertEqual(event["scope"], "repository")
        self.assertEqual(event["outcome"], "idle")
        self.assertEqual(event["termination_reason"], "idle_deadline")
        self.assertIsNone(event["busy_at"])
        self.assertIsNone(event["busy_secs"])
        self.assertEqual(event["jit_mint_secs"], 2.0)
        self.assertEqual(event["sandbox_create_secs"], 2.0)
        self.assertEqual(event["runner_start_secs"], 3.0)
        self.assertEqual(event["idle_secs"], 20.0)
        self.assertEqual(event["teardown_secs"], 5.0)
        self.assertEqual(event["allocated_secs"], 30.0)
        self.assertEqual(event["cleanup_status"], "deleted")
        self.assertEqual(event["declared_resources"], {})

    def test_failed_sandbox_delete_never_claims_a_complete_allocation(self) -> None:
        class DeleteFails(Sandbox):
            def delete(self) -> None:
                raise RuntimeError("provider still owns sandbox")

        class Daytona:
            base_labels = {"org": "another-example-org"}

            def create_sandbox(self, *args, **kwargs):
                return DeleteFails()

            def start_runner(self, sandbox, jit: str) -> str:
                return "command-test"

            def session_exit_code(self, sandbox, command_id: str) -> int:
                return 0

        cycle_id = "delete-failed-cycle"
        self.register(cycle_id)
        size_class = {
            "name": "default",
            "labels": ["self-hosted", "daytona"],
            "snapshot": "another-example-runner-default",
            "cpu": 2,
            "memory_gib": 4,
            "disk_gib": 10,
        }

        with patch.object(orch.time, "sleep", lambda seconds: None), \
             patch.object(orch, "_cycle_now", side_effect=(
                 101.0, 103.0, 105.0, 107.0, 110.0, 120.0,
             )), \
             self.assertLogs("marsh-orch", level="INFO") as logs:
            orch.cycle(
                cycle_id, size_class, GitHub(), Daytona(), 1, BusyMap(False),
                orch.Lifecycle(3600, 120, 300, 1800), Stop(),
            )

        event = telemetry_record(logs.output)
        self.assertEqual(event["cleanup_status"], "delete_failed")
        self.assertIsNone(event["allocation_completed_at"])
        self.assertIsNone(event["allocated_secs"])
        self.assertIsNone(event["teardown_secs"])
        self.assertEqual(event["sandbox_create_secs"], 2.0)
        with orch.REGISTRY_LOCK:
            self.assertEqual(orch.REGISTRY[cycle_id].state, "CLEANUP_PENDING")

    def test_unconfirmed_runner_delete_keeps_cycle_visible_for_safe_drain(self) -> None:
        class DeleteFailsGitHub(GitHub):
            def delete_runner(self, runner) -> bool:
                return False

        class Daytona:
            base_labels = {"org": "another-example-org"}

            def create_sandbox(self, *args, **kwargs):
                return Sandbox()

            def start_runner(self, sandbox, jit: str) -> str:
                return "command-test"

            def session_exit_code(self, sandbox, command_id: str) -> int:
                return 0

        cycle_id = "runner-delete-failed-cycle"
        self.register(cycle_id)
        size_class = {
            "name": "default",
            "labels": ["self-hosted", "daytona"],
            "snapshot": "another-example-runner-default",
        }

        with patch.object(orch.time, "sleep", lambda seconds: None), \
             patch.object(orch, "_cycle_now", side_effect=(
                 101.0, 103.0, 105.0, 107.0, 110.0, 120.0, 125.0,
             )), \
             self.assertLogs("marsh-orch", level="WARNING") as logs:
            orch.cycle(
                cycle_id, size_class, DeleteFailsGitHub(), Daytona(), 1, BusyMap(False),
                orch.Lifecycle(3600, 120, 300, 1800), Stop(),
            )

        with orch.REGISTRY_LOCK:
            self.assertEqual(orch.REGISTRY[cycle_id].state, "CLEANUP_PENDING")
        self.assertTrue(any("runner deregistration was not confirmed" in line for line in logs.output))

    def test_fast_job_does_not_report_unobserved_job_time_as_idle(self) -> None:
        class Daytona:
            base_labels = {"org": "example-org"}

            def create_sandbox(self, *args, **kwargs):
                return Sandbox()

            def start_runner(self, sandbox, jit: str) -> str:
                return "command-test"

            def session_exit_code(self, sandbox, command_id: str) -> int:
                return 0

        cycle_id = "fast-job-cycle"
        self.register(cycle_id)
        size_class = {
            "name": "default",
            "labels": ["self-hosted", "daytona"],
            "snapshot": "example-runner-default",
            "cpu": 2,
            "memory_gib": 4,
            "disk_gib": 10,
        }

        with patch.object(orch.time, "sleep", lambda seconds: None), \
             patch.object(orch, "_cycle_now", side_effect=(
                 101.0, 103.0, 105.0, 107.0, 110.0, 120.0, 125.0,
             )), \
             self.assertLogs("marsh-orch", level="INFO") as logs:
            orch.cycle(
                cycle_id, size_class, GitHub(), Daytona(), 1, BusyMap(True),
                orch.Lifecycle(3600, 120, 300, 1800), Stop(),
            )

        event = telemetry_record(logs.output)
        self.assertEqual(event["outcome"], "job")
        self.assertFalse(event["job_phase_observed"])
        self.assertIsNone(event["busy_at"])
        self.assertIsNone(event["busy_secs"])
        self.assertIsNone(event["idle_secs"])
        self.assertEqual(event["jit_mint_secs"], 2.0)
        self.assertEqual(event["sandbox_create_secs"], 2.0)
        self.assertEqual(event["runner_start_secs"], 3.0)
        self.assertEqual(event["teardown_secs"], 5.0)
        self.assertEqual(event["allocated_secs"], 20.0)

    def test_cycle_failure_record_excludes_exception_and_invalid_resource_values(self) -> None:
        class Daytona:
            base_labels = {"org": "another-example-org"}

            def create_sandbox(self, *args, **kwargs):
                raise RuntimeError("sensitive vendor response")

        cycle_id = "failed-cycle"
        self.register(cycle_id)
        size_class = {
            "name": "default",
            "labels": ["self-hosted", "daytona"],
            "snapshot": "another-example-runner-default",
            "cpu": True,
            "memory_gib": float("nan"),
            "disk_gib": "10",
        }

        with patch.object(orch, "_cycle_now", return_value=120.0), \
             self.assertLogs("marsh-orch", level="INFO") as logs:
            orch.cycle(
                cycle_id, size_class, GitHub(), Daytona(), 1, BusyMap(False),
                orch.Lifecycle(3600, 120, 300, 1800), Stop(),
            )

        event = telemetry_record(logs.output)
        self.assertEqual(event["outcome"], "failed")
        self.assertEqual(event["termination_reason"], "cycle_failed")
        self.assertIsNone(event["sandbox_started_at"])
        self.assertIsNone(event["sandbox_ready_at"])
        self.assertIsNone(event["sandbox_create_secs"])
        self.assertIsNone(event["allocated_secs"])
        # JIT completed; create was attempted and failed before ready.
        self.assertEqual(event["jit_mint_secs"], 0.0)
        self.assertIsNone(event["runner_start_secs"])
        self.assertEqual(event["cleanup_status"], "create_unconfirmed")
        self.assertEqual(event["declared_resources"], {})
        self.assertNotIn("exception", event)
        self.assertNotIn("error", event)
        with orch.REGISTRY_LOCK:
            self.assertEqual(orch.REGISTRY[cycle_id].state, "CLEANUP_PENDING")

    def test_jit_failure_is_known_zero_allocation_before_create_attempt(self) -> None:
        class GitHubFails(GitHub):
            def mint_jit(self, group_id: int, labels: list[str], repository: str | None = None):
                raise RuntimeError("GitHub unavailable")

        class Daytona:
            base_labels = {"org": "another-example-org"}

            def create_sandbox(self, *args, **kwargs):
                raise AssertionError("sandbox create must not be attempted")

        cycle_id = "jit-failed-cycle"
        self.register(cycle_id)
        size_class = {
            "name": "default",
            "labels": ["self-hosted", "daytona"],
            "snapshot": "another-example-runner-default",
            "cpu": 2,
            "memory_gib": 4,
            "disk_gib": 10,
        }

        with patch.object(orch, "_cycle_now", return_value=120.0), \
             self.assertLogs("marsh-orch", level="INFO") as logs:
            orch.cycle(
                cycle_id, size_class, GitHubFails(), Daytona(), 1, BusyMap(False),
                orch.Lifecycle(3600, 120, 300, 1800), Stop(),
            )

        event = telemetry_record(logs.output)
        self.assertEqual(event["cleanup_status"], "create_not_attempted")
        self.assertIsNone(event["allocated_secs"])
        # JIT start was observed; mint raised before completion.
        self.assertIsNotNone(event["jit_started_at"])
        self.assertIsNone(event["jit_completed_at"])
        self.assertIsNone(event["jit_mint_secs"])
        with orch.REGISTRY_LOCK:
            self.assertEqual(orch.REGISTRY[cycle_id].state, "CLEANUP_PENDING")

    def test_sigusr_quiesce_is_independent_from_termination_and_blocks_new_cycles(self) -> None:
        control = orch.RuntimeControl()
        control.quiesce()

        self.assertFalse(control.stop.is_set())
        self.assertFalse(control.admission.is_set())
        self.assertTrue(control.wake.is_set())

        size_class = {"name": "default", "labels": ["self-hosted", "daytona"]}
        orch.spawn_cycle(
            size_class, object(), object(), 1, BusyMap(False),
            orch.Lifecycle(3600, 120, 300, 1800), 300, control.stop,
            admission=control.admission,
        )
        with orch.REGISTRY_LOCK:
            self.assertEqual(orch.REGISTRY, {})

        control.wake.clear()
        control.resume()
        self.assertFalse(control.stop.is_set())
        self.assertTrue(control.admission.is_set())
        self.assertTrue(control.wake.is_set())
        self.assertTrue(control.refresh.is_set())

        terminating = orch.RuntimeControl()
        terminating.request_stop()
        self.assertTrue(terminating.stop.is_set())
        self.assertTrue(terminating.admission.is_set())

    def test_quiesce_drains_idle_cycle_with_a_distinct_telemetry_reason(self) -> None:
        control = orch.RuntimeControl()

        class Daytona:
            base_labels = {"org": "another-example-org"}

            def create_sandbox(self, *args, **kwargs):
                return Sandbox()

            def start_runner(self, sandbox, jit: str) -> str:
                return "command-test"

            def session_exit_code(self, sandbox, command_id: str) -> None:
                control.quiesce()
                return None

        cycle_id = "quiesced-idle-cycle"
        self.register(cycle_id)
        size_class = {
            "name": "default",
            "labels": ["self-hosted", "daytona"],
            "snapshot": "another-example-runner-default",
        }

        with patch.object(orch.time, "sleep", lambda seconds: None), \
             patch.object(orch, "_cycle_now", side_effect=(
                 101.0, 103.0, 105.0, 107.0, 110.0, 120.0, 125.0, 130.0,
             )), \
             self.assertLogs("marsh-orch", level="INFO") as logs:
            # SIGUSR1 arrives after this runner has started; new cycles are
            # separately rejected by spawn_cycle above.
            orch.cycle(
                cycle_id, size_class, GitHub(), Daytona(), 1, BusyMap(False),
                orch.Lifecycle(3600, 120, 300, 1800), control.stop,
                admission=control.admission,
            )

        event = telemetry_record(logs.output)
        self.assertEqual(event["outcome"], "idle")
        self.assertEqual(event["termination_reason"], "quiesce")
        self.assertEqual(event["cleanup_status"], "deleted")

    def test_quiesce_never_interrupts_a_busy_cycle(self) -> None:
        control = orch.RuntimeControl()

        class Daytona:
            base_labels = {"org": "another-example-org"}

            def __init__(self) -> None:
                self.exit_codes = iter((None, 0))

            def create_sandbox(self, *args, **kwargs):
                return Sandbox()

            def start_runner(self, sandbox, jit: str) -> str:
                return "command-test"

            def session_exit_code(self, sandbox, command_id: str) -> int | None:
                code = next(self.exit_codes)
                if code is None:
                    control.quiesce()
                return code

        cycle_id = "quiesced-busy-cycle"
        self.register(cycle_id)
        size_class = {
            "name": "default",
            "labels": ["self-hosted", "daytona"],
            "snapshot": "another-example-runner-default",
        }

        with patch.object(orch.time, "sleep", lambda seconds: None), \
             patch.object(orch, "_cycle_now", side_effect=(
                 101.0, 103.0, 105.0, 107.0, 110.0, 120.0, 130.0, 140.0, 145.0,
             )), \
             self.assertLogs("marsh-orch", level="INFO") as logs:
            orch.cycle(
                cycle_id, size_class, GitHub(), Daytona(), 1, BusyMap(True),
                orch.Lifecycle(3600, 120, 300, 1800), control.stop,
                admission=control.admission,
            )

        event = telemetry_record(logs.output)
        self.assertFalse(control.admission.is_set())
        self.assertEqual(event["outcome"], "job")
        self.assertEqual(event["termination_reason"], "runner_exit")
        self.assertTrue(event["job_phase_observed"])

    def test_runtime_status_uses_the_exact_atomic_non_secret_fleet_contract(self) -> None:
        control = orch.RuntimeControl()
        control.quiesce()
        ready = threading.Event()
        with orch.REGISTRY_LOCK:
            orch.REGISTRY.clear()
            orch.REGISTRY["idle"] = orch.Cycle("default", "IDLE", idle_deadline_secs=1)
            orch.REGISTRY["busy"] = orch.Cycle("large", "BUSY", idle_deadline_secs=1)

        with tempfile.TemporaryDirectory() as directory, \
             patch.object(orch, "RUNTIME_STATUS_DIR", directory), \
             patch.object(orch, "_runtime_status_timestamp", return_value="2026-07-14T12:00:00.000Z"):
            status_path = Path(directory) / "example-fleet.json"
            outside_path = Path(directory) / "outside"
            outside_path.write_text("unchanged", encoding="utf-8")
            # An old target symlink is atomically replaced, never followed;
            # it is neither parsed nor trusted as input to the status record.
            status_path.symlink_to(outside_path)
            orch.write_runtime_status("example-fleet", control.admission, ready)

            payload = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertFalse(status_path.is_symlink())
            self.assertEqual(outside_path.read_text(encoding="utf-8"), "unchanged")
            self.assertEqual(set(payload), {"schema", "pid", "admission", "ready", "total", "busy", "updated_at"})
            self.assertEqual(payload["schema"], 1)
            self.assertEqual(payload["admission"], False)
            self.assertEqual(payload["ready"], False)
            self.assertEqual(payload["total"], 2)
            self.assertEqual(payload["busy"], 1)
            self.assertEqual(payload["updated_at"], "2026-07-14T12:00:00.000Z")
            self.assertEqual(payload["pid"], os.getpid())
            self.assertEqual(status_path.name, "example-fleet.json")
            self.assertEqual(status_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list(Path(directory).glob(".*.tmp")), [])

            ready.set()
            orch.write_runtime_status("example-fleet", control.admission, ready)
            self.assertTrue(json.loads(status_path.read_text(encoding="utf-8"))["ready"])

    def test_runtime_status_rejects_a_group_writable_runtime_directory(self) -> None:
        control = orch.RuntimeControl()
        with tempfile.TemporaryDirectory() as directory:
            info = os.stat(directory)
            unsafe_info = os.stat_result((
                info.st_mode | stat.S_IWGRP | stat.S_IWOTH,
                *info[1:],
            ))
            with patch.object(orch, "RUNTIME_STATUS_DIR", directory), \
                 patch.object(orch.os, "fstat", return_value=unsafe_info):
                with self.assertRaisesRegex(RuntimeError, "group/world writable"):
                    orch.write_runtime_status("example-fleet", control.admission)

    def test_runtime_status_rejects_pathlike_fleet_names(self) -> None:
        control = orch.RuntimeControl()
        self.assertEqual(orch._status_filename("example-fleet"), "example-fleet.json")
        for fleet in ("", "../outside", "/tmp/outside", "example-fleet/next", "example-fleet.json"):
            with self.subTest(fleet=fleet), self.assertRaisesRegex(ValueError, "invalid Marsh fleet name"):
                orch.runtime_status(fleet, control.admission)


class QuiescedBootstrapTests(unittest.TestCase):
    def tearDown(self) -> None:
        with orch.REGISTRY_LOCK:
            orch.REGISTRY.clear()

    def test_quiesced_control_publishes_closed_ready_pid_status_and_resumes(self) -> None:
        control = orch.RuntimeControl(start_quiesced=True)
        ready = threading.Event()
        ready.set()

        status = orch.runtime_status("bootstrap-test", control.admission, ready)
        self.assertEqual(status["pid"], os.getpid())
        self.assertFalse(status["admission"])
        self.assertTrue(status["ready"])
        self.assertEqual(status["total"], 0)
        self.assertEqual(status["busy"], 0)

        control.resume()
        self.assertTrue(control.admission.is_set())
        self.assertTrue(control.refresh.is_set())

    def test_start_quiesced_parser_and_explicit_fleet_key_fail_closed(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(orch.start_quiesced_from_environment())
            with self.assertRaisesRegex(RuntimeError, "MARSH_FLEET_NAME is required"):
                orch._runtime_fleet_name("example-org", "", require_explicit=True)

        with patch.dict(os.environ, {"MARSH_START_QUIESCED": "0"}, clear=True):
            self.assertFalse(orch.start_quiesced_from_environment())

        with patch.dict(os.environ, {"MARSH_START_QUIESCED": "1", "MARSH_FLEET_NAME": "example-fleet"}, clear=True):
            self.assertTrue(orch.start_quiesced_from_environment())
            self.assertEqual(
                orch._runtime_fleet_name("example-org", "", require_explicit=True),
                "example-fleet",
            )

        for value in ("", "true", "yes", "2", " 1"):
            with self.subTest(value=value), patch.dict(os.environ, {"MARSH_START_QUIESCED": value}, clear=True):
                with self.assertRaisesRegex(RuntimeError, "exactly '0' or '1'"):
                    orch.start_quiesced_from_environment()

        with patch.dict(os.environ, {"MARSH_FLEET_NAME": "../outside"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "must be a valid fleet name"):
                orch._runtime_fleet_name("example-org", "", require_explicit=True)

    def test_quiesced_main_defers_all_provider_work_until_sigusr2(self) -> None:
        timeline: list[str] = []
        statuses: list[tuple[str, bool, bool, tuple[str, ...]]] = []
        signal_handlers = {}

        def register_signal(signum, handler) -> None:
            signal_handlers[signum] = handler

        class Wake:
            def __init__(self, control) -> None:
                self.control = control
                self.calls = 0

            def set(self) -> None:
                pass

            def clear(self) -> None:
                pass

            def wait(self, timeout: float) -> bool:
                self.calls += 1
                if self.calls == 1:
                    timeline.append("sigusr2")
                    signal_handlers[orch.signal.SIGUSR2](orch.signal.SIGUSR2, None)
                elif self.calls == 2:
                    self.control.request_stop()
                return True

        class TestControl(orch.RuntimeControl):
            instance = None

            def __init__(self, *, start_quiesced: bool = False) -> None:
                super().__init__(start_quiesced=start_quiesced)
                self.wake = Wake(self)
                TestControl.instance = self

        class FakeGitHub:
            def __init__(self, *args, **kwargs) -> None:
                self.request_spacing_secs = 0.0

            def invalidate_reconciliation_cache(self) -> None:
                timeline.append("github-cache-reset")

        class FakeDaytona:
            def __init__(self, *args, **kwargs) -> None:
                timeline.append("daytona-client")
                self.sdk = object()

        config = """
[github]
org = "example-org"
runner_group = "daytona"

[daytona]
target = "us"

[cache]
volume = "bootstrap-cache"

[[size_class]]
name = "default"
labels = ["self-hosted", "daytona"]
snapshot = "example-default"
min_idle = 0
max = 1

[lifecycle]
job_max_secs = 3600

[poller]
interval_secs = 1
"""

        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "runners.toml"
            config_path.write_text(config, encoding="utf-8")
            environment = {
                "MARSH_RUNNER_CONFIG": str(config_path),
                "MARSH_START_QUIESCED": "1",
                "MARSH_FLEET_NAME": "bootstrap-test",
                "DAYTONA_API_KEY": "test-only-placeholder",
                "GH_APP_ID": "test-app",
                "GH_APP_INSTALLATION_ID": "test-installation",
                "GH_APP_KEY_PATH": "/tmp/test-key",
            }

            def write_status(fleet: str, admission, ready) -> None:
                statuses.append((fleet, admission.is_set(), ready.is_set(), tuple(timeline)))

            with self.assertLogs("marsh-orch", level="INFO"), \
                 patch.dict(os.environ, environment, clear=True), \
                 patch.object(orch, "RuntimeControl", TestControl), \
                 patch.object(orch.signal, "signal", side_effect=register_signal), \
                 patch.object(orch, "GitHub", FakeGitHub), \
                 patch.object(orch, "Daytona", FakeDaytona), \
                 patch.object(orch, "write_runtime_status", side_effect=write_status), \
                 patch.object(
                     orch,
                     "resolve_volume_id",
                     side_effect=lambda *_: timeline.append("volume") or "volume-id",
                 ), \
                 patch.object(orch, "reap", side_effect=lambda *_: timeline.append("reap")), \
                 patch.object(orch, "poller_tick", side_effect=lambda *_: timeline.append("poller") or True), \
                 patch.object(orch, "orphan_sweep", side_effect=lambda *_: timeline.append("orphan-sweep")), \
                 patch.object(orch.time, "time", return_value=1_000.0):
                orch.main()

        self.assertGreaterEqual(len(statuses), 3)
        self.assertEqual(statuses[0], ("bootstrap-test", False, True, ()))
        self.assertEqual(statuses[1], ("bootstrap-test", False, True, ()))
        self.assertLess(timeline.index("sigusr2"), timeline.index("volume"))
        self.assertLess(timeline.index("sigusr2"), timeline.index("reap"))
        self.assertLess(timeline.index("sigusr2"), timeline.index("poller"))
        self.assertLess(timeline.index("sigusr2"), timeline.index("orphan-sweep"))
        self.assertEqual(
            timeline,
            ["sigusr2", "github-cache-reset", "volume", "reap", "daytona-client", "poller", "orphan-sweep"],
        )
        self.assertTrue(any(admission and ready for _, admission, ready, _ in statuses))
        self.assertIsNotNone(TestControl.instance)
        self.assertTrue(TestControl.instance.admission.is_set())


if __name__ == "__main__":
    unittest.main()
