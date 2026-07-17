"""Tests for webhook fast-path, signature checks, and hold_on_failure."""
from __future__ import annotations

import hashlib
import hmac
import json
import threading
import unittest
from unittest.mock import patch

import urllib.error

from tests.support import load_module, load_orchestrator


orch = load_orchestrator("marsh_webhook_debug_test")
watchdog = load_module("marsh_webhook_watchdog_test", "orchestrator/watchdog.py")


class WebhookSignatureTests(unittest.TestCase):
    def test_valid_signature_accepts(self) -> None:
        secret = b"test-hmac"
        body = b'{"action":"queued"}'
        sig = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
        self.assertTrue(orch.verify_github_webhook_signature(secret, body, sig))

    def test_invalid_signature_rejects(self) -> None:
        secret = b"test-hmac"
        body = b'{"action":"queued"}'
        self.assertFalse(orch.verify_github_webhook_signature(secret, body, "sha256=deadbeef"))
        self.assertFalse(orch.verify_github_webhook_signature(secret, body, None))
        self.assertFalse(orch.verify_github_webhook_signature(secret, body, "sha1=abc"))


class WebhookLabelMatchTests(unittest.TestCase):
    def test_superset_of_size_class_matches(self) -> None:
        fleet = [{"self-hosted", "daytona", "marsh"}, {"self-hosted", "daytona", "marsh", "large"}]
        self.assertTrue(orch.webhook_labels_match(
            ["self-hosted", "daytona", "marsh"], fleet))
        self.assertTrue(orch.webhook_labels_match(
            ["self-hosted", "daytona", "marsh", "large", "extra"], fleet))
        self.assertFalse(orch.webhook_labels_match(["ubuntu-latest"], fleet))
        self.assertFalse(orch.webhook_labels_match([], fleet))
        self.assertFalse(orch.webhook_labels_match(None, fleet))


class WebhookHandlerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.secret = b"unit-test-hmac"
        self.wake = threading.Event()
        self.matches: list[int] = []
        handler_cls = orch._make_webhook_handler(
            self.secret, self.wake,
            [{"self-hosted", "daytona", "marsh"}],
            on_match=lambda: self.matches.append(1),
        )
        self.server = orch.ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def _post(self, body: bytes, event: str = "workflow_job", signed: bool = True) -> int:
        import urllib.request
        headers = {"Content-Type": "application/json", "X-GitHub-Event": event}
        if signed:
            headers["X-Hub-Signature-256"] = (
                "sha256=" + hmac.new(self.secret, body, hashlib.sha256).hexdigest()
            )
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/github", data=body, headers=headers, method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=2) as resp:
                return resp.status
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            return code

    def test_matched_queued_job_wakes_poller(self) -> None:
        body = json.dumps({
            "action": "queued",
            "workflow_job": {"labels": ["self-hosted", "daytona", "marsh"]},
        }).encode()
        status = self._post(body)
        self.assertEqual(status, 202)
        self.assertTrue(self.wake.is_set())
        self.assertEqual(self.matches, [1])

    def test_unmatched_labels_do_not_wake(self) -> None:
        body = json.dumps({
            "action": "queued",
            "workflow_job": {"labels": ["ubuntu-latest"]},
        }).encode()
        status = self._post(body)
        self.assertEqual(status, 202)
        self.assertFalse(self.wake.is_set())

    def test_bad_signature_is_401(self) -> None:
        body = b'{"action":"queued"}'
        status = self._post(body, signed=False)
        self.assertEqual(status, 401)
        self.assertFalse(self.wake.is_set())

    def test_ping_is_200(self) -> None:
        body = b'{"zen":"design for failure"}'
        status = self._post(body, event="ping")
        self.assertEqual(status, 200)


class HoldOnFailureTests(unittest.TestCase):
    def tearDown(self) -> None:
        with orch.REGISTRY_LOCK:
            orch.REGISTRY.clear()

    def test_failed_job_holds_before_delete(self) -> None:
        deleted: list[str] = []
        held: list[float] = []

        class Sandbox:
            id = "sandbox-hold"

            class Process:
                def delete_session(self, session_id: str) -> None:
                    pass

            process = Process()

            def delete(self) -> None:
                deleted.append(self.id)

        class Daytona:
            base_labels = {"org": "example-org"}

            def create_sandbox(self, *args, **kwargs):
                return Sandbox()

            def start_runner(self, sandbox, jit: str) -> str:
                return "cmd"

            def session_exit_code(self, sandbox, command_id: str) -> int:
                return 1

        class GitHub:
            def mint_jit(self, group_id, labels, repository=None):
                return orch.RunnerRef(9), "jit"

            def runner_busy(self, runner) -> bool:
                return False

            def delete_runner(self, runner) -> None:
                pass

        class BusyMap:
            def is_busy(self, runner) -> bool:
                return True

        class Stop:
            def is_set(self) -> bool:
                return False

        cycle_id = "hold-cycle"
        with orch.REGISTRY_LOCK:
            orch.REGISTRY[cycle_id] = orch.Cycle(
                "default", "SPAWNING", idle_deadline_secs=5, spawned_at=100.0)

        size_class = {
            "name": "default",
            "labels": ["self-hosted", "daytona"],
            "snapshot": "snap",
            "cpu": 2, "memory_gib": 4, "disk_gib": 10,
        }
        lc = orch.Lifecycle(
            3600, 120, 300, 1800, hold_on_failure_secs=42,
        )

        def fake_sleep(secs):
            held.append(secs)

        with patch.object(orch.time, "sleep", side_effect=fake_sleep), \
             patch.object(orch, "_cycle_now", side_effect=(
                 101.0, 102.0, 103.0, 104.0, 105.0, 110.0, 115.0, 120.0,
             )), \
             self.assertLogs("marsh-orch", level="WARNING") as logs:
            orch.cycle(
                cycle_id, size_class, GitHub(), Daytona(), 1, BusyMap(),
                lc, Stop(),
            )

        self.assertIn(42, held)  # adaptive idle poll may sleep first; hold is required
        self.assertEqual(deleted, ["sandbox-hold"])
        self.assertTrue(any("hold_on_failure" in line for line in logs.output))


class WebhookConfigTests(unittest.TestCase):
    def test_parses_listen_and_hmac_env(self) -> None:
        cfg = orch.webhook_config_from_profile({
            "webhook": {"listen": "127.0.0.1:8787", "hmac_env": "MARSH_WEBHOOK_HMAC"},
        })
        self.assertIsNotNone(cfg)
        assert cfg is not None
        self.assertEqual(cfg.host, "127.0.0.1")
        self.assertEqual(cfg.port, 8787)
        self.assertEqual(cfg.hmac_env, "MARSH_WEBHOOK_HMAC")

    def test_missing_section_is_none(self) -> None:
        self.assertIsNone(orch.webhook_config_from_profile({}))

    def test_rejects_public_bind_host(self) -> None:
        with self.assertRaisesRegex(ValueError, "loopback"):
            orch.webhook_config_from_profile({
                "webhook": {"listen": "203.0.113.5:8787"},
            })


class StageSummaryTests(unittest.TestCase):
    def test_usage_report_includes_stage_p95_when_complete(self) -> None:
        events = [{
            "event": "runner_cycle_complete",
            "schema_version": 1,
            "snapshot": "snap-a",
            "outcome": "job",
            "total_secs": 60,
            "allocated_secs": 60,
            "cleanup_status": "deleted",
            "declared_resources": {"cpu": 2, "memory_gib": 4, "disk_gib": 10},
            "jit_mint_secs": 1.0,
            "sandbox_create_secs": 4.0,
            "runner_start_secs": 1.0,
            "idle_secs": 5.0,
            "busy_secs": 40.0,
            "teardown_secs": 2.0,
        }, {
            "event": "runner_cycle_complete",
            "schema_version": 1,
            "snapshot": "snap-a",
            "outcome": "job",
            "total_secs": 80,
            "allocated_secs": 80,
            "cleanup_status": "deleted",
            "declared_resources": {"cpu": 2, "memory_gib": 4, "disk_gib": 10},
            "jit_mint_secs": 2.0,
            "sandbox_create_secs": 6.0,
            "runner_start_secs": 1.5,
            "idle_secs": 8.0,
            "busy_secs": 50.0,
            "teardown_secs": 3.0,
        }]
        lines = watchdog.summarize_cycle_telemetry(events)
        self.assertEqual(len(lines), 1)
        self.assertIn("stages p95", lines[0])
        self.assertIn("jit_mint=", lines[0])
        self.assertIn("sandbox_create=", lines[0])


if __name__ == "__main__":
    unittest.main()
