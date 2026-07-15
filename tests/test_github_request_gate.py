from __future__ import annotations

import json
import subprocess
import threading
import unittest
from email.message import Message
from unittest.mock import patch
from urllib.error import HTTPError

from tests.support import load_orchestrator


orch = load_orchestrator("marsh_orchestrator_request_gate_test")


class Response:
    def __init__(self, payload: dict):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()


class Clock:
    def __init__(self, wall_time: float = 1_700_000_000.0):
        self.now = 0.0
        self.wall_time = wall_time
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return self.wall_time + self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class RunnerBootstrapTests(unittest.TestCase):
    def test_runner_bootstrap_waits_for_docker_and_disables_shared_pip_cache(self) -> None:
        command = orch.RUNNER_CMD
        self.assertTrue(command.startswith("bash -c '"))
        self.assertTrue(command.endswith("'"))
        script = command[len("bash -c '"):-1]

        parsed = subprocess.run(
            ["bash", "-n"],
            input=script,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(parsed.returncode, 0, parsed.stderr)
        self.assertIn("docker info", script)
        self.assertIn("runner bootstrap: Docker daemon did not become ready", script)
        self.assertIn("exec bash /usr/local/bin/run-ephemeral.sh", script)
        self.assertEqual(orch.RUNNER_ENV["PIP_NO_CACHE_DIR"], "1")


def rate_error(code: int, headers: dict[str, str] | None = None) -> HTTPError:
    message = Message()
    for key, value in (headers or {}).items():
        message[key] = value
    # The response body intentionally contains no test assertion target: the
    # controller must make its backoff decision from status/headers alone.
    return HTTPError("https://api.github.com/test", code, "limited", message, None)


class GitHubRequestGateTests(unittest.TestCase):
    def github(self, spacing: float = 0, stop_event=None):
        client = orch.GitHub("example-org", "app", "installation", "/key",
                             request_spacing_secs=spacing, stop_event=stop_event)
        client._tok = ("test-token", float("inf"))
        return client

    def test_zero_spacing_preserves_immediate_sequential_requests(self) -> None:
        clock = Clock()
        starts: list[float] = []

        def urlopen(request, timeout: int):
            starts.append(clock.monotonic())
            return Response({})

        client = self.github()
        with patch.object(orch.time, "monotonic", clock.monotonic), \
             patch.object(orch.time, "time", clock.time), \
             patch.object(orch.time, "sleep", clock.sleep), \
             patch.object(orch.urllib.request, "urlopen", urlopen):
            client._api("GET", "/first")
            client._api("GET", "/second")

        self.assertEqual(starts, [0.0, 0.0])
        self.assertEqual(clock.sleeps, [])

    def test_spacing_delays_request_starts(self) -> None:
        clock = Clock()
        starts: list[float] = []

        def urlopen(request, timeout: int):
            starts.append(clock.monotonic())
            return Response({})

        client = self.github(spacing=1)
        with patch.object(orch.time, "monotonic", clock.monotonic), \
             patch.object(orch.time, "time", clock.time), \
             patch.object(orch.time, "sleep", clock.sleep), \
             patch.object(orch.urllib.request, "urlopen", urlopen):
            client._api("GET", "/first")
            client._api("GET", "/second")

        self.assertEqual(starts, [0.0, 1.0])
        self.assertEqual(clock.sleeps, [1.0])

    def test_fallback_cooldown_is_shared_and_escalates(self) -> None:
        clock = Clock()
        starts: list[float] = []
        outcomes: list[Response | HTTPError] = [rate_error(403), rate_error(429), Response({})]

        def urlopen(request, timeout: int):
            starts.append(clock.monotonic())
            outcome = outcomes.pop(0)
            if isinstance(outcome, HTTPError):
                raise outcome
            return outcome

        client = self.github()
        with patch.object(orch.time, "monotonic", clock.monotonic), \
             patch.object(orch.time, "time", clock.time), \
             patch.object(orch.time, "sleep", clock.sleep), \
             patch.object(orch.urllib.request, "urlopen", urlopen):
            with self.assertRaises(HTTPError) as first_error:
                client._api("GET", "/first")
            first_error.exception.close()
            with self.assertRaises(HTTPError) as second_error:
                client._api("GET", "/second")
            second_error.exception.close()
            client._api("GET", "/third")

        self.assertEqual(starts, [0.0, 60.0, 180.0])
        self.assertEqual(clock.sleeps, [60.0, 120.0])

    def test_fallback_cooldown_is_bounded(self) -> None:
        clock = Clock()
        outcomes: list[Response | HTTPError] = [rate_error(403) for _ in range(6)] + [Response({})]

        def urlopen(request, timeout: int):
            outcome = outcomes.pop(0)
            if isinstance(outcome, HTTPError):
                raise outcome
            return outcome

        client = self.github()
        with patch.object(orch.time, "monotonic", clock.monotonic), \
             patch.object(orch.time, "time", clock.time), \
             patch.object(orch.time, "sleep", clock.sleep), \
             patch.object(orch.urllib.request, "urlopen", urlopen):
            for index in range(6):
                with self.assertRaises(HTTPError) as error:
                    client._api("GET", f"/limited-{index}")
                error.exception.close()
            client._api("GET", "/recovered")

        self.assertEqual(clock.sleeps, [60.0, 120.0, 240.0, 480.0, 900.0, 900.0])

    def test_retry_after_header_is_honored(self) -> None:
        clock = Clock()
        outcomes: list[Response | HTTPError] = [rate_error(403, {"Retry-After": "7"}), Response({})]

        def urlopen(request, timeout: int):
            outcome = outcomes.pop(0)
            if isinstance(outcome, HTTPError):
                raise outcome
            return outcome

        client = self.github()
        with patch.object(orch.time, "monotonic", clock.monotonic), \
             patch.object(orch.time, "time", clock.time), \
             patch.object(orch.time, "sleep", clock.sleep), \
             patch.object(orch.urllib.request, "urlopen", urlopen):
            with self.assertRaises(HTTPError) as error:
                client._api("GET", "/first")
            error.exception.close()
            client._api("GET", "/second")

        self.assertEqual(clock.sleeps, [7.0])

    def test_rate_limit_reset_header_is_honored(self) -> None:
        clock = Clock()
        reset_at = str(int(clock.wall_time + 9))
        outcomes: list[Response | HTTPError] = [rate_error(429, {
            "X-RateLimit-Reset": reset_at,
            "X-RateLimit-Remaining": "0",
        }), Response({})]

        def urlopen(request, timeout: int):
            outcome = outcomes.pop(0)
            if isinstance(outcome, HTTPError):
                raise outcome
            return outcome

        client = self.github()
        with patch.object(orch.time, "monotonic", clock.monotonic), \
             patch.object(orch.time, "time", clock.time), \
             patch.object(orch.time, "sleep", clock.sleep), \
             patch.object(orch.urllib.request, "urlopen", urlopen):
            with self.assertRaises(HTTPError) as error:
                client._api("GET", "/first")
            error.exception.close()
            client._api("GET", "/second")

        self.assertEqual(clock.sleeps, [9.0])

    def test_nonexhausted_primary_reset_uses_secondary_fallback(self) -> None:
        clock = Clock()
        reset_at = str(int(clock.wall_time + 9))
        outcomes: list[Response | HTTPError] = [rate_error(403, {
            "X-RateLimit-Reset": reset_at,
            "X-RateLimit-Remaining": "4876",
        }), Response({})]

        def urlopen(request, timeout: int):
            outcome = outcomes.pop(0)
            if isinstance(outcome, HTTPError):
                raise outcome
            return outcome

        client = self.github()
        with patch.object(orch.time, "monotonic", clock.monotonic), \
             patch.object(orch.time, "time", clock.time), \
             patch.object(orch.time, "sleep", clock.sleep), \
             patch.object(orch.urllib.request, "urlopen", urlopen):
            with self.assertRaises(HTTPError) as error:
                client._api("GET", "/first")
            error.exception.close()
            client._api("GET", "/second")

        self.assertEqual(clock.sleeps, [60.0])

    def test_successful_partial_scan_calls_do_not_reset_backoff(self) -> None:
        clock = Clock()
        outcomes: list[Response | HTTPError] = [
            Response({}), rate_error(403), Response({}), rate_error(403), Response({}),
        ]

        def urlopen(request, timeout: int):
            outcome = outcomes.pop(0)
            if isinstance(outcome, HTTPError):
                raise outcome
            return outcome

        client = self.github()
        with patch.object(orch.time, "monotonic", clock.monotonic), \
             patch.object(orch.time, "time", clock.time), \
             patch.object(orch.time, "sleep", clock.sleep), \
             patch.object(orch.urllib.request, "urlopen", urlopen):
            client._api("GET", "/group")
            with self.assertRaises(HTTPError) as first_error:
                client._api("GET", "/actions")
            first_error.exception.close()
            client._api("GET", "/group")
            with self.assertRaises(HTTPError) as second_error:
                client._api("GET", "/actions")
            second_error.exception.close()
            client._api("GET", "/recovered")

        self.assertEqual(clock.sleeps, [60.0, 120.0])

    def test_full_poller_recovery_resets_backoff(self) -> None:
        client = self.github()
        client._fallback_rate_limit_backoff_secs = 120.0
        client.current_group_id = lambda name: 23  # type: ignore[method-assign]
        client.runner_group_repos = lambda group_id: ["one"]  # type: ignore[method-assign]
        client.queued_jobs = lambda repositories: []  # type: ignore[method-assign]
        client.runners_busy_map = lambda: {}  # type: ignore[method-assign]

        class Stop:
            def wait(self, seconds: float) -> bool:
                return False

        stop = Stop()
        classes = [{"name": "default", "labels": ["self-hosted", "daytona"], "min_idle": 0, "max": 1}]
        self.assertTrue(orch.poller_tick(
            client, "daytona", classes, orch.BusyMap(), object(), orch.Lifecycle(60, 1, 1, 1), stop,
        ))
        self.assertEqual(client._fallback_rate_limit_backoff_secs, 60.0)

        client._fallback_rate_limit_backoff_secs = 120.0
        limited = rate_error(403)
        client.queued_jobs = lambda repositories: (_ for _ in ()).throw(limited)  # type: ignore[method-assign]
        try:
            self.assertFalse(orch.poller_tick(
                client, "daytona", classes, orch.BusyMap(), object(), orch.Lifecycle(60, 1, 1, 1), stop,
            ))
        finally:
            limited.close()
        self.assertEqual(client._fallback_rate_limit_backoff_secs, 120.0)

    def test_shutdown_interrupts_cooldown_without_an_api_call(self) -> None:
        clock = Clock()

        class StopEvent:
            def __init__(self) -> None:
                self.waits: list[float] = []

            def is_set(self) -> bool:
                return False

            def wait(self, seconds: float) -> bool:
                self.waits.append(seconds)
                return True

        stop_event = StopEvent()
        client = self.github(stop_event=stop_event)
        client._next_request_at = 60.0

        def urlopen(request, timeout: int):
            raise AssertionError("a shutdown-interrupted cooldown must not call GitHub")

        with patch.object(orch.time, "monotonic", clock.monotonic), \
             patch.object(orch.time, "time", clock.time), \
             patch.object(orch.time, "sleep", clock.sleep), \
             patch.object(orch.urllib.request, "urlopen", urlopen):
            with self.assertRaises(orch.GitHubRequestCancelled):
                client._api("GET", "/blocked")

        self.assertEqual(stop_event.waits, [60.0])
        self.assertEqual(clock.sleeps, [])

    def test_runner_busy_propagates_shutdown_cancellation(self) -> None:
        client = self.github()
        client._api = lambda *args, **kwargs: (_ for _ in ()).throw(  # type: ignore[method-assign]
            orch.GitHubRequestCancelled("shutdown")
        )

        with self.assertRaises(orch.GitHubRequestCancelled):
            client.runner_busy(orch.RunnerRef(7))

    def test_app_jwt_is_minted_after_the_gate_wait(self) -> None:
        clock = Clock()
        minted_at: list[float] = []
        calls: list[tuple[str, str | None, float]] = []
        client = orch.GitHub("example-org", "app", "installation", "/key")
        client._next_request_at = 60.0
        client._app_jwt = lambda: minted_at.append(clock.monotonic()) or "fresh-jwt"  # type: ignore[method-assign]

        def urlopen(request, timeout: int):
            calls.append((request.full_url, request.get_header("Authorization"), clock.monotonic()))
            if request.full_url.endswith("/access_tokens"):
                return Response({"token": "installation-token"})
            return Response({})

        with patch.object(orch.time, "monotonic", clock.monotonic), \
             patch.object(orch.time, "time", clock.time), \
             patch.object(orch.time, "sleep", clock.sleep), \
             patch.object(orch.urllib.request, "urlopen", urlopen):
            client._api("GET", "/target")

        self.assertEqual(minted_at, [60.0])
        self.assertEqual(calls, [
            ("https://api.github.com/app/installations/installation/access_tokens", "Bearer fresh-jwt", 60.0),
            ("https://api.github.com/target", "Bearer installation-token", 60.0),
        ])

    def test_app_metadata_uses_app_jwt_after_the_gate_wait(self) -> None:
        clock = Clock()
        minted_at: list[float] = []
        calls: list[tuple[str, str | None, float]] = []
        client = orch.GitHub("example-org", "app", "installation", "/key")
        client._next_request_at = 60.0
        client._app_jwt = lambda: minted_at.append(clock.monotonic()) or "fresh-jwt"  # type: ignore[method-assign]

        def urlopen(request, timeout: int):
            calls.append((request.full_url, request.get_header("Authorization"), clock.monotonic()))
            return Response({})

        with patch.object(orch.time, "monotonic", clock.monotonic), \
             patch.object(orch.time, "time", clock.time), \
             patch.object(orch.time, "sleep", clock.sleep), \
             patch.object(orch.urllib.request, "urlopen", urlopen):
            client._app_api("GET", "/app/installations/installation")

        self.assertEqual(minted_at, [60.0])
        self.assertEqual(calls, [
            ("https://api.github.com/app/installations/installation", "Bearer fresh-jwt", 60.0),
        ])

    def test_organization_installation_scope_cache_requires_complete_matching_metadata(self) -> None:
        client = orch.GitHub("example-org", "app", "123", "/key")
        calls: list[str] = []

        def app_api(method: str, path: str, body=None):
            calls.append(path)
            return {
                "id": 123,
                "target_type": "Organization",
                "account": {"login": "example-org"},
                "repository_selection": "all",
            }

        client._app_api = app_api  # type: ignore[method-assign]
        client.require_organization_installation_all_repositories()
        client.require_organization_installation_all_repositories()
        self.assertEqual(calls, ["/app/installations/123"])
        client.invalidate_reconciliation_cache()
        client.require_organization_installation_all_repositories()
        self.assertEqual(calls, ["/app/installations/123", "/app/installations/123"])

    def test_organization_installation_scope_refetches_after_an_inflight_invalidation(self) -> None:
        client = orch.GitHub("example-org", "app", "123", "/key")
        calls: list[str] = []

        def app_api(method: str, path: str, body=None):
            calls.append(path)
            if len(calls) == 1:
                client.invalidate_reconciliation_cache()
            return {
                "id": 123,
                "target_type": "Organization",
                "account": {"login": "example-org"},
                "repository_selection": "all",
            }

        client._app_api = app_api  # type: ignore[method-assign]
        client.require_organization_installation_all_repositories()
        self.assertEqual(calls, ["/app/installations/123", "/app/installations/123"])

    def test_organization_installation_scope_rejects_incomplete_or_mismatched_metadata(self) -> None:
        cases = [
            ({
                "id": 123,
                "target_type": "Organization",
                "account": {"login": "example-org"},
                "repository_selection": "selected",
            }, "all repositories"),
            ({
                "id": 123,
                "target_type": "Organization",
                "account": {"login": "other-org"},
                "repository_selection": "all",
            }, "does not match"),
            ({
                "id": 123,
                "target_type": "User",
                "account": {"login": "example-org"},
                "repository_selection": "all",
            }, "not scoped"),
            ({
                "id": 456,
                "target_type": "Organization",
                "account": {"login": "example-org"},
                "repository_selection": "all",
            }, "did not match"),
            ({
                "id": 123,
                "target_type": "Organization",
                "account": "malformed",
                "repository_selection": "all",
            }, "does not match"),
        ]
        for metadata, message in cases:
            with self.subTest(metadata=metadata):
                client = orch.GitHub("example-org", "app", "123", "/key")
                client._app_api = lambda method, path, body=None: metadata  # type: ignore[method-assign]
                with self.assertRaisesRegex(RuntimeError, message):
                    client.require_organization_installation_all_repositories()
                self.assertIsNone(client._organization_installation_cache)

    def test_selected_app_installation_never_mints_an_idle_floor_runner(self) -> None:
        client = self.github()
        # Simulate an already-running controller: an old valid group cache
        # must not bypass a newly restricted App installation.
        client._group_cache = (23, orch.time.time())
        client._app_api = lambda method, path, body=None: {  # type: ignore[method-assign]
            "id": "installation",
            "target_type": "Organization",
            "account": {"login": "example-org"},
            "repository_selection": "selected",
        }
        client._api = lambda *args, **kwargs: (_ for _ in ()).throw(  # type: ignore[method-assign]
            AssertionError("runner-group access must not be queried after incomplete App scope")
        )
        spawned: list[object] = []
        original_spawn = orch.spawn_cycle
        orch.spawn_cycle = lambda *args, **kwargs: spawned.append(args)  # type: ignore[assignment]
        try:
            self.assertFalse(orch.poller_tick(
                client,
                "daytona",
                [{"name": "default", "labels": ["self-hosted", "daytona"], "min_idle": 1, "max": 1}],
                orch.BusyMap(),
                object(),
                orch.Lifecycle(60, 1, 1, 1),
                threading.Event(),
            ))
        finally:
            orch.spawn_cycle = original_spawn

        self.assertEqual(spawned, [])
        self.assertEqual(client._group_cache[0], 23)
        self.assertIsNone(client._organization_installation_cache)

    def test_group_pagination_failures_never_cache_partial_repositories(self) -> None:
        for failure_page in (1, 2):
            with self.subTest(failure_page=failure_page):
                client = self.github()
                first_page = {"repositories": [{"name": f"repo-{index}"} for index in range(100)]}
                limited = rate_error(403)

                def api(method: str, path: str, body=None, bearer=None):
                    if path.endswith("/runner-groups/23"):
                        return {"visibility": "selected", "allows_public_repositories": False}
                    if path.endswith("&page=1"):
                        if failure_page == 1:
                            raise limited
                        return first_page
                    if path.endswith("&page=2"):
                        raise limited
                    raise AssertionError(f"unexpected path {path}")

                client._api = api  # type: ignore[method-assign]
                try:
                    with self.assertRaises(HTTPError):
                        client.runner_group_repos(23)
                finally:
                    limited.close()
                self.assertIsNone(client._repo_cache)

    def test_group_pagination_cap_fails_closed_without_caching_partial_selection(self) -> None:
        client = self.github()
        pages: list[str] = []

        def api(method: str, path: str, body=None, bearer=None):
            if path.endswith("/runner-groups/23"):
                return {"visibility": "selected", "allows_public_repositories": False}
            if "/runner-groups/23/repositories?" in path:
                pages.append(path)
                return {"repositories": [{"name": "repo"}] * 100}
            raise AssertionError(f"unexpected path {path}")

        client._api = api  # type: ignore[method-assign]
        with self.assertRaisesRegex(RuntimeError, "pagination limit reached"):
            client.runner_group_repos(23)
        self.assertEqual(len(pages), 50)
        self.assertIsNone(client._repo_cache)

    def test_installation_pagination_cap_fails_closed(self) -> None:
        client = self.github()
        pages: list[str] = []

        def api(method: str, path: str, body=None, bearer=None):
            if path.startswith("/installation/repositories?"):
                pages.append(path)
                return {"repositories": [{"name": "repo"}] * 100}
            raise AssertionError(f"unexpected path {path}")

        client._api = api  # type: ignore[method-assign]
        with self.assertRaisesRegex(RuntimeError, "pagination limit reached"):
            client.installation_repositories()
        self.assertEqual(len(pages), 20)

    def test_cache_refresh_never_falls_back_to_default_runner_group(self) -> None:
        client = self.github()
        client._group_cache = (1, orch.time.time())
        client.invalidate_reconciliation_cache()
        client.require_organization_installation_all_repositories = lambda ttl=600: None  # type: ignore[method-assign]

        def api(method: str, path: str, body=None, bearer=None):
            if path == "/orgs/example-org/actions/runner-groups":
                return {"runner_groups": [{"id": 1, "name": "Default", "visibility": "all"}]}
            raise AssertionError(f"unexpected path {path}")

        client._api = api  # type: ignore[method-assign]
        spawned: list[object] = []
        original_spawn = orch.spawn_cycle
        orch.spawn_cycle = lambda *args, **kwargs: spawned.append(args)  # type: ignore[assignment]
        try:
            self.assertFalse(orch.poller_tick(
                client,
                "daytona",
                [{"name": "default", "labels": ["self-hosted", "daytona"], "min_idle": 0, "max": 1}],
                orch.BusyMap(),
                object(),
                orch.Lifecycle(60, 1, 1, 1),
                threading.Event(),
            ))
        finally:
            orch.spawn_cycle = original_spawn

        self.assertEqual(spawned, [])
        self.assertIsNone(client._group_cache)

    def test_public_enabled_runner_group_never_reconciles_after_cache_refresh(self) -> None:
        client = self.github()
        client._group_cache = (7, orch.time.time())
        client.invalidate_reconciliation_cache()
        client.require_organization_installation_all_repositories = lambda ttl=600: None  # type: ignore[method-assign]

        def api(method: str, path: str, body=None, bearer=None):
            if path == "/orgs/example-org/actions/runner-groups":
                return {"runner_groups": [{
                    "id": 7,
                    "name": "daytona",
                    "visibility": "selected",
                    "allows_public_repositories": True,
                }]}
            raise AssertionError(f"unexpected path {path}")

        client._api = api  # type: ignore[method-assign]
        spawned: list[object] = []
        original_spawn = orch.spawn_cycle
        orch.spawn_cycle = lambda *args, **kwargs: spawned.append(args)  # type: ignore[assignment]
        try:
            self.assertFalse(orch.poller_tick(
                client,
                "daytona",
                [{"name": "default", "labels": ["self-hosted", "daytona"], "min_idle": 0, "max": 1}],
                orch.BusyMap(),
                object(),
                orch.Lifecycle(60, 1, 1, 1),
                threading.Event(),
            ))
        finally:
            orch.spawn_cycle = original_spawn

        self.assertEqual(spawned, [])
        self.assertIsNone(client._group_cache)

    def test_public_enabled_group_detail_never_mints_an_idle_floor_runner(self) -> None:
        client = self.github()
        client.current_group_id = lambda name: 23  # type: ignore[method-assign]

        def api(method: str, path: str, body=None, bearer=None):
            if path.endswith("/runner-groups/23"):
                return {"visibility": "selected", "allows_public_repositories": True}
            raise AssertionError(f"unexpected path {path}")

        client._api = api  # type: ignore[method-assign]
        spawned: list[object] = []
        original_spawn = orch.spawn_cycle
        orch.spawn_cycle = lambda *args, **kwargs: spawned.append(args)  # type: ignore[assignment]
        try:
            self.assertFalse(orch.poller_tick(
                client,
                "daytona",
                [{"name": "default", "labels": ["self-hosted", "daytona"], "min_idle": 1, "max": 1}],
                orch.BusyMap(),
                object(),
                orch.Lifecycle(60, 1, 1, 1),
                threading.Event(),
            ))
        finally:
            orch.spawn_cycle = original_spawn

        self.assertEqual(spawned, [])
        self.assertIsNone(client._repo_cache)

    def test_group_page_two_failure_aborts_reconciliation_before_spawn(self) -> None:
        client = self.github()
        client.current_group_id = lambda name: 23  # type: ignore[method-assign]
        first_page = {"repositories": [{"name": f"repo-{index}"} for index in range(100)]}
        limited = rate_error(403)

        def api(method: str, path: str, body=None, bearer=None):
            if path.endswith("/runner-groups/23"):
                return {"visibility": "selected", "allows_public_repositories": False}
            if path.endswith("&page=1"):
                return first_page
            if path.endswith("&page=2"):
                raise limited
            raise AssertionError(f"unexpected path {path}")

        client._api = api  # type: ignore[method-assign]

        class Stop:
            def __init__(self) -> None:
                self.waits: list[float] = []

            def wait(self, seconds: float) -> bool:
                self.waits.append(seconds)
                return False

        stop = Stop()
        spawned: list[object] = []
        original_spawn = orch.spawn_cycle
        orch.spawn_cycle = lambda *args, **kwargs: spawned.append(args)  # type: ignore[assignment]
        try:
            orch.poller_tick(
                client,
                "daytona",
                [{"name": "default", "labels": ["self-hosted", "daytona"], "min_idle": 1, "max": 1}],
                orch.BusyMap(),
                object(),
                orch.Lifecycle(60, 1, 1, 1),
                stop,
            )
        finally:
            orch.spawn_cycle = original_spawn
            limited.close()

        self.assertEqual(spawned, [])
        self.assertEqual(stop.waits, [30])
        self.assertIsNone(client._repo_cache)

    def test_shutdown_cancelled_busy_confirmation_never_tears_down_before_confirmation(self) -> None:
        """A stop-path gate cancellation is not evidence that an idle runner is safe to kill.

        The next deterministic busy-state answer is false so this single-threaded
        lifecycle can finish its ordinary cleanup. The event order proves the
        cancellation itself causes no destructive action; under real shutdown the
        unchanged systemd stop boundary owns the conservative hold instead.
        """
        events: list[str] = []

        class Stop:
            def is_set(self) -> bool:
                return True

        class Sandbox:
            id = "sandbox-1"

            class Process:
                def delete_session(self, session_id: str) -> None:
                    events.append("session-delete")

            process = Process()

            def delete(self) -> None:
                events.append("sandbox-delete")

        class Daytona:
            def create_sandbox(self, *args, **kwargs):
                events.append("sandbox-create")
                return Sandbox()

            def start_runner(self, sandbox, jit: str) -> str:
                events.append("runner-start")
                return "command-1"

            def session_exit_code(self, sandbox, command_id: str) -> None:
                return None

        class GitHub:
            def __init__(self) -> None:
                self.busy_calls = 0

            def mint_jit(self, group_id: int, labels: list[str], repository=None):
                events.append("jit-mint")
                return orch.RunnerRef(7), "jit"

            def runner_busy(self, runner: orch.RunnerRef) -> bool:
                self.busy_calls += 1
                if self.busy_calls == 1:
                    events.append("busy-cancelled")
                    raise orch.GitHubRequestCancelled("shutdown")
                events.append("busy-confirmed-idle")
                return False

            def delete_runner(self, runner: orch.RunnerRef) -> None:
                events.append("runner-delete")

        cycle_id = "shutdown-cancelled-busy-check"
        cls = {"name": "default", "labels": ["self-hosted", "daytona"], "snapshot": "snap"}
        with orch.REGISTRY_LOCK:
            orch.REGISTRY.clear()
            orch.REGISTRY[cycle_id] = orch.Cycle("default", "SPAWNING", idle_deadline_secs=1)
        try:
            with patch.object(orch.time, "sleep", lambda seconds: None), \
                 self.assertLogs("marsh-orch", level="INFO") as logs:
                orch.cycle(cycle_id, cls, GitHub(), Daytona(), 23, orch.BusyMap(),
                           orch.Lifecycle(60, 1, 1, 1), Stop())
        finally:
            with orch.REGISTRY_LOCK:
                orch.REGISTRY.clear()

        cancelled = events.index("busy-cancelled")
        confirmed = events.index("busy-confirmed-idle")
        self.assertLess(cancelled, confirmed)
        for action in ("session-delete", "sandbox-delete", "runner-delete"):
            self.assertGreater(events.index(action), confirmed)
        self.assertTrue(any("retaining runner and sandbox without teardown" in line for line in logs.output))

    def test_request_gate_serializes_cycle_threads(self) -> None:
        first_opened = threading.Event()
        release_first = threading.Event()
        second_attempted = threading.Event()
        calls: list[str] = []
        calls_lock = threading.Lock()
        errors: list[BaseException] = []

        class RecordingLock:
            def __init__(self) -> None:
                self.lock = threading.Lock()
                self.attempts = 0
                self.attempts_lock = threading.Lock()

            def __enter__(self):
                with self.attempts_lock:
                    self.attempts += 1
                    if self.attempts == 2:
                        second_attempted.set()
                self.lock.acquire()
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                self.lock.release()
                return False

        def urlopen(request, timeout: int):
            with calls_lock:
                calls.append(request.full_url)
            if request.full_url.endswith("/first"):
                first_opened.set()
                if not release_first.wait(timeout=2):
                    raise TimeoutError("test did not release first request")
            return Response({})

        client = self.github()
        client._request_lock = RecordingLock()  # type: ignore[assignment]

        def call(path: str) -> None:
            try:
                client._api("GET", path)
            except BaseException as exc:  # surface worker errors in the test thread
                errors.append(exc)

        first = threading.Thread(target=call, args=("/first",))
        second = threading.Thread(target=call, args=("/second",))
        with patch.object(orch.urllib.request, "urlopen", urlopen):
            first.start()
            self.assertTrue(first_opened.wait(timeout=1))
            second.start()
            self.assertTrue(second_attempted.wait(timeout=1))
            self.assertEqual(calls, ["https://api.github.com/first"])
            release_first.set()
            first.join(timeout=1)
            second.join(timeout=1)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(calls, ["https://api.github.com/first", "https://api.github.com/second"])

    def test_rate_limited_queue_scan_never_spawns_from_partial_data(self) -> None:
        client = self.github()
        client.current_group_id = lambda name: 23  # type: ignore[method-assign]
        client.runner_group_repos = lambda group_id: ["one"]  # type: ignore[method-assign]

        limited = rate_error(403)

        def queued_jobs(repositories):
            raise limited

        client.queued_jobs = queued_jobs  # type: ignore[method-assign]

        class Stop:
            def __init__(self) -> None:
                self.waits: list[float] = []

            def wait(self, seconds: float) -> bool:
                self.waits.append(seconds)
                return False

        stop = Stop()
        spawned: list[object] = []
        original_spawn = orch.spawn_cycle
        orch.spawn_cycle = lambda *args, **kwargs: spawned.append(args)  # type: ignore[assignment]
        try:
            orch.poller_tick(
                client,
                "daytona",
                [{"name": "default", "labels": ["self-hosted", "daytona"], "min_idle": 0, "max": 1}],
                orch.BusyMap(),
                object(),
                orch.Lifecycle(60, 1, 1, 1),
                stop,
            )
        finally:
            orch.spawn_cycle = original_spawn
            limited.close()

        self.assertEqual(spawned, [])
        self.assertEqual(stop.waits, [30])


if __name__ == "__main__":
    unittest.main()
