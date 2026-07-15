from __future__ import annotations

import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.support import load_module, load_orchestrator


orch = load_orchestrator("orchestrator")
preflight = load_module("marsh_preflight_test", "infra/preflight-fleet.py")
watchdog = load_module("marsh_watchdog_test", "orchestrator/watchdog.py")


REPOSITORY_CONFIG = {
    "github": {
        "scope": "repository",
        "owner": "personal-owner",
        "repositories": ["alpha"],
        "runner_group_id": 1,
    },
    "daytona": {"target": "us"},
    "size_class": [{"name": "default", "labels": ["self-hosted", "daytona"]}],
}


class PreflightAndWatchdogTests(unittest.TestCase):
    def test_daytona_target_preflight_requires_exact_visible_target(self) -> None:
        requests = []

        class Response(io.StringIO):
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                self.close()
                return False

        def opener(request, timeout):
            requests.append((request, timeout))
            return Response(json.dumps([
                {"id": "us", "name": "us", "regionType": "shared"},
                {"id": "private-target", "name": "private-target", "regionType": "custom"},
            ]))

        config = {**REPOSITORY_CONFIG, "daytona": {"target": "private-target"}}
        result = preflight.daytona_target_preflight(
            {"DAYTONA_API_KEY": "injected-secret"}, config, opener,
        )

        self.assertEqual(result, "preflight passed: Daytona target 'private-target' is available")
        self.assertEqual(len(requests), 1)
        request, timeout = requests[0]
        self.assertEqual(timeout, 15)
        self.assertEqual(request.full_url, "https://app.daytona.io/api/regions")
        self.assertEqual(request.get_header("Authorization"), "Bearer injected-secret")

    def test_daytona_target_preflight_rejects_missing_target_without_provider_detail(self) -> None:
        class Response(io.StringIO):
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                self.close()
                return False

        def opener(request, timeout):
            return Response(json.dumps([{"id": "us", "name": "us"}]))

        config = {**REPOSITORY_CONFIG, "daytona": {"target": "private-target"}}
        with self.assertRaisesRegex(RuntimeError, "target 'private-target' is not available"):
            preflight.daytona_target_preflight(
                {"DAYTONA_API_KEY": "injected-secret"}, config, opener,
            )

    def test_daytona_target_preflight_rejects_redirects_without_forwarding_key(self) -> None:
        requests = []

        def redirected(request, timeout):
            requests.append(request)
            raise urllib.error.HTTPError(
                request.full_url, 302, "redirect refused",
                {"Location": "https://attacker.example/collect"}, None,
            )

        with self.assertRaisesRegex(RuntimeError, "HTTP 302"):
            preflight.daytona_target_preflight(
                {"DAYTONA_API_KEY": "injected-secret"},
                REPOSITORY_CONFIG,
                redirected,
            )

        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].get_header("Authorization"), "Bearer injected-secret")
        self.assertIsNone(
            preflight.RejectRedirects().redirect_request(
                requests[0], None, 302, "redirect refused",
                {"Location": "https://attacker.example/collect"},
                "https://attacker.example/collect",
            )
        )

    def test_daytona_target_preflight_rejects_unresolved_secret_uri_without_request(self) -> None:
        def opener(request, timeout):
            raise AssertionError("unresolved credentials must not reach the provider")

        with self.assertRaisesRegex(RuntimeError, "missing a resolved DAYTONA_API_KEY"):
            preflight.daytona_target_preflight(
                {"DAYTONA_API_KEY": "secret://unresolved"},
                REPOSITORY_CONFIG,
                opener,
            )

    def test_notification_token_is_never_forwarded_on_redirect(self) -> None:
        requests = []

        def redirected(request, timeout):
            requests.append(request)
            raise urllib.error.HTTPError(
                request.full_url, 302, "redirect refused",
                {"Location": "https://attacker.example/collect"}, None,
            )

        cfg = {"notify": {
            "url": "https://notify.example/topic",
            "token_env": "MARSH_NOTIFY_TOKEN",
        }}
        with mock.patch.dict("os.environ", {"MARSH_NOTIFY_TOKEN": "injected-secret"}), \
             mock.patch.object(watchdog, "open_without_redirects", side_effect=redirected), \
             mock.patch("builtins.print"):
            watchdog.notify(cfg, "title", "body")

        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].get_header("Authorization"), "Bearer injected-secret")
        self.assertIsNone(
            watchdog.RejectRedirects().redirect_request(
                requests[0], None, 302, "redirect refused",
                {"Location": "https://attacker.example/collect"},
                "https://attacker.example/collect",
            )
        )

    def test_usage_report_groups_structured_cycles_by_snapshot(self) -> None:
        self.assertEqual(watchdog.CYCLE_TELEMETRY_PREFIX, orch.CYCLE_TELEMETRY_PREFIX)
        base = {
            "event": "runner_cycle_complete",
            "schema_version": 1,
            "snapshot": "example-runner-default",
            "size_class": "default",
            "cleanup_status": "deleted",
            "declared_resources": {"cpu": 2, "memory_gib": 4, "disk_gib": 10},
        }
        job = {**base, "outcome": "job", "total_secs": 60, "allocated_secs": 60}
        idle = {**base, "outcome": "idle", "total_secs": 180, "allocated_secs": 180}
        journal = [
            "unrelated log message",
            f"INFO {orch.CYCLE_TELEMETRY_PREFIX}{json.dumps(job)}",
            f"{orch.CYCLE_TELEMETRY_PREFIX}not-json",
            f"{orch.CYCLE_TELEMETRY_PREFIX}{json.dumps(idle)}",
            f"{orch.CYCLE_TELEMETRY_PREFIX}{json.dumps({**base, 'schema_version': 2})}",
        ]

        events = watchdog.parse_cycle_telemetry(journal)
        self.assertEqual(events, [job, idle])
        self.assertEqual(
            watchdog.summarize_cycle_telemetry(events),
            [
                "example-runner-default: n=2 job=1 idle=1 failed=0 unknown=0 "
                "avg=2.00m p95=3.00m; allocated=0.13 CPU-h/0.27 GiB-h RAM/"
                "0.67 GiB-h disk"
            ],
        )

    def test_usage_report_does_not_invent_missing_allocation(self) -> None:
        lines = watchdog.summarize_cycle_telemetry([{
            "event": "runner_cycle_complete",
            "schema_version": 1,
            "snapshot": "partial-snapshot",
            "outcome": "unexpected",
            "total_secs": None,
            "declared_resources": {"cpu": True, "memory_gib": 4},
        }])

        self.assertEqual(
            lines,
            [
                "partial-snapshot: n=1 job=0 idle=0 failed=0 unknown=1 "
                "duration unavailable (coverage=0/1); allocation unavailable (coverage=0/1)"
            ],
        )

        partial_duration = watchdog.summarize_cycle_telemetry([
            {
                "snapshot": "duration-partial",
                "outcome": "failed",
                "total_secs": 60,
                "cleanup_status": "create_not_attempted",
            },
            {
                "snapshot": "duration-partial",
                "outcome": "failed",
                "total_secs": None,
                "cleanup_status": "create_not_attempted",
            },
        ])
        self.assertIn("duration unavailable (coverage=1/2)", partial_duration[0])
        self.assertIn("allocated=0.00 CPU-h", partial_duration[0])

    def test_usage_report_marks_partial_allocation_and_mixed_rollout_window(self) -> None:
        complete = {
            "event": "runner_cycle_complete",
            "schema_version": 1,
            "snapshot": "runner-v7",
            "outcome": "job",
            "total_secs": 3600,
            "allocated_secs": 3600,
            "cleanup_status": "deleted",
            "declared_resources": {"cpu": 2, "memory_gib": 4, "disk_gib": 10},
        }
        incomplete = {
            **complete,
            "outcome": "failed",
            "cleanup_status": "delete_failed",
            "allocated_secs": None,
        }
        journal = "\n".join((
            "[default] runner up: sandbox=one runner_id=1 idle_deadline=300s",
            f"{watchdog.CYCLE_TELEMETRY_PREFIX}{json.dumps(complete)}",
            f"{watchdog.CYCLE_TELEMETRY_PREFIX}{json.dumps(incomplete)}",
        ))
        cfg = {"report": {"since": "-72h"}, "notify": {}, "instance": [{
            "name": "example-fleet", "unit": "marsh-orchestrator@example-fleet.service",
        }]}

        with mock.patch.object(
            watchdog.subprocess, "run",
            return_value=SimpleNamespace(returncode=0, stdout=journal),
        ), mock.patch.object(watchdog, "notify") as notify, \
             mock.patch("builtins.print") as output:
            self.assertEqual(watchdog.cmd_usage_report(cfg), 0)

        body = output.call_args.args[0]
        self.assertIn("structured completions=2; runner-up lines=1", body)
        self.assertIn("allocation unavailable (coverage=1/2)", body)
        self.assertEqual(notify.call_args.args[2], body)

    def test_organization_preflight_keeps_selected_runner_group_contract(self) -> None:
        class Client:
            def __init__(self, *args, **kwargs):
                pass

            def require_organization_installation_all_repositories(self):
                return None

            def _api(self, method: str, path: str):
                self.last_path = path
                return {"runner_groups": [{"id": 7, "name": "daytona", "visibility": "selected", "allows_public_repositories": False}]}

            def runner_group_repos(self, group_id: int):
                if group_id != 7:
                    raise AssertionError(f"unexpected group {group_id}")
                return ["private-repo"]

            def installation_repositories(self):
                return [{"name": "private-repo", "private": True, "owner": {"login": "existing-org"}}]

        config = {"github": {"org": "existing-org", "runner_group": "daytona"}}
        result = preflight.organization_preflight(
            {"GH_APP_ID": "id", "GH_APP_INSTALLATION_ID": "installation", "GH_APP_KEY_PATH": "/key"},
            config,
            Client,
        )
        self.assertIn("covers all 1 private organization", result)

    def test_organization_preflight_rejects_a_private_visible_repository_outside_the_runner_group(self) -> None:
        class Client:
            def __init__(self, *args, **kwargs):
                pass

            def require_organization_installation_all_repositories(self):
                return None

            def _api(self, method: str, path: str):
                return {"runner_groups": [{"id": 7, "name": "daytona", "visibility": "selected", "allows_public_repositories": False}]}

            def runner_group_repos(self, group_id: int):
                return ["covered"]

            def installation_repositories(self):
                return [
                    {"name": "covered", "private": True, "owner": {"login": "existing-org"}},
                    {"name": "uncovered", "private": True, "owner": {"login": "existing-org"}},
                ]

        with self.assertRaisesRegex(RuntimeError, "does not cover every private organization repository"):
            preflight.organization_preflight(
                {"GH_APP_ID": "id", "GH_APP_INSTALLATION_ID": "installation", "GH_APP_KEY_PATH": "/key"},
                {"github": {"org": "existing-org", "runner_group": "daytona"}},
                Client,
            )

    def test_organization_preflight_rejects_group_repository_outside_app_visibility(self) -> None:
        class Client:
            def __init__(self, *args, **kwargs):
                pass

            def require_organization_installation_all_repositories(self):
                return None

            def _api(self, method: str, path: str):
                return {"runner_groups": [{"id": 7, "name": "daytona", "visibility": "selected", "allows_public_repositories": False}]}

            def runner_group_repos(self, group_id: int):
                return ["visible", "not-visible"]

            def installation_repositories(self):
                return [{"name": "visible", "private": True, "owner": {"login": "existing-org"}}]

        with self.assertRaisesRegex(RuntimeError, "not visible to the Marsh App"):
            preflight.organization_preflight(
                {"GH_APP_ID": "id", "GH_APP_INSTALLATION_ID": "installation", "GH_APP_KEY_PATH": "/key"},
                {"github": {"org": "existing-org", "runner_group": "daytona"}},
                Client,
            )

    def test_organization_preflight_requires_an_all_repository_app_installation(self) -> None:
        class Client:
            def __init__(self, *args, **kwargs):
                pass

            def _api(self, method: str, path: str):
                return {"runner_groups": [{
                    "id": 7,
                    "name": "daytona",
                    "visibility": "selected",
                    "allows_public_repositories": False,
                }]}

            def runner_group_repos(self, group_id: int):
                return ["private-repo"]

            def require_organization_installation_all_repositories(self):
                raise RuntimeError("GitHub App installation must cover all repositories before organization runners can reconcile")

        with self.assertRaisesRegex(RuntimeError, "all repositories"):
            preflight.organization_preflight(
                {"GH_APP_ID": "id", "GH_APP_INSTALLATION_ID": "installation", "GH_APP_KEY_PATH": "/key"},
                {"github": {"org": "existing-org", "runner_group": "daytona"}},
                Client,
            )

    def test_organization_preflight_rejects_a_group_that_allows_public_repositories(self) -> None:
        class Client:
            def __init__(self, *args, **kwargs):
                pass

            def _api(self, method: str, path: str):
                return {"runner_groups": [{
                    "id": 7,
                    "name": "daytona",
                    "visibility": "selected",
                    "allows_public_repositories": True,
                }]}

        with self.assertRaisesRegex(RuntimeError, "allows public repositories"):
            preflight.organization_preflight(
                {"GH_APP_ID": "id", "GH_APP_INSTALLATION_ID": "installation", "GH_APP_KEY_PATH": "/key"},
                {"github": {"org": "existing-org", "runner_group": "daytona"}},
                Client,
            )

    def test_repository_preflight_requires_private_visible_target_and_runner_group(self) -> None:
        class Client:
            def __init__(self, *args, **kwargs):
                pass

            def installation_repositories(self):
                return [{"name": "alpha", "private": True, "owner": {"login": "personal-owner"}}]

            def repository_runner_groups(self, repository: str):
                if repository != "alpha":
                    raise AssertionError(f"unexpected repository {repository}")
                return [{"id": 1, "name": "Default"}]

        result = preflight.repository_preflight(
            {"GH_APP_ID": "id", "GH_APP_INSTALLATION_ID": "installation", "GH_APP_KEY_PATH": "/key"},
            REPOSITORY_CONFIG,
            Client,
        )
        self.assertIn("repository scope has 1 private", result)

    def test_repository_preflight_resolves_a_named_group(self) -> None:
        class Client:
            def __init__(self, *args, **kwargs):
                pass

            def installation_repositories(self):
                return [{"name": "alpha", "private": True, "owner": {"login": "personal-owner"}}]

            def repository_runner_groups(self, repository: str):
                if repository != "alpha":
                    raise AssertionError(f"unexpected repository {repository}")
                return [{"id": 44, "name": "restricted-runner"}]

        config = {
            **REPOSITORY_CONFIG,
            "github": {
                "scope": "repository",
                "owner": "personal-owner",
                "repositories": ["alpha"],
                "runner_group": "restricted-runner",
            },
        }
        result = preflight.repository_preflight(
            {"GH_APP_ID": "id", "GH_APP_INSTALLATION_ID": "installation", "GH_APP_KEY_PATH": "/key"},
            config,
            Client,
        )
        self.assertIn("'restricted-runner'", result)

    def test_repository_preflight_rejects_public_or_inaccessible_target(self) -> None:
        class PublicClient:
            def __init__(self, *args, **kwargs):
                pass

            def installation_repositories(self):
                return [{"name": "alpha", "private": False, "owner": {"login": "personal-owner"}}]

            def repository_runner_groups(self, repository: str):
                return [{"id": 1}]

        with self.assertRaisesRegex(RuntimeError, "not private"):
            preflight.repository_preflight(
                {"GH_APP_ID": "id", "GH_APP_INSTALLATION_ID": "installation", "GH_APP_KEY_PATH": "/key"},
                REPOSITORY_CONFIG,
                PublicClient,
            )

    def test_watchdog_repository_scope_reports_coverage_and_queue_without_org_group_warning(self) -> None:
        class Client:
            def __init__(self, owner, *args, **kwargs):
                self.owner = owner

            def installation_repositories(self):
                return [
                    {"name": "alpha", "private": True, "owner": {"login": "personal-owner"}},
                    {"name": "new-private", "private": True, "owner": {"login": "personal-owner"}},
                ]

            def repository_runner_groups(self, repository: str):
                if repository != "alpha":
                    raise AssertionError(f"unexpected repository {repository}")
                return [{"id": 1, "name": "Default"}]

            def queued_jobs(self, repositories):
                if repositories != ["alpha"]:
                    raise AssertionError(f"unexpected repositories {repositories}")
                return [orch.QueuedJob("alpha", {
                    "labels": ["self-hosted", "daytona"],
                    "created_at": "2020-01-01T00:00:00Z",
                    "html_url": "https://github.example/personal-owner/alpha/actions/runs/1/job/2",
                })]

        original_active, original_github, original_daytona = watchdog.unit_active, watchdog.GitHub, watchdog.Daytona
        watchdog.unit_active = lambda unit: True
        watchdog.GitHub = Client
        watchdog.Daytona = lambda *args, **kwargs: SimpleNamespace(sdk=SimpleNamespace(list=lambda: []))
        try:
            with tempfile.TemporaryDirectory() as directory:
                config_path = Path(directory) / "runners.toml"
                config_path.write_text(
                    """
[github]
scope = "repository"
owner = "personal-owner"
runner_group_id = 1
repositories = ["alpha"]
[daytona]
target = "us"
[[size_class]]
name = "default"
labels = ["self-hosted", "daytona"]
""".strip() + "\n",
                    encoding="utf-8",
                )
                env_path = Path(directory) / "orchestrator.env"
                env_path.write_text(
                    "GH_APP_ID=id\nGH_APP_INSTALLATION_ID=installation\nGH_APP_KEY_PATH=/key\nMARSH_FLEET_NAME=personal-fleet\n",
                    encoding="utf-8",
                )
                findings: list[str] = []
                watchdog.check_instance(
                    {"name": "personal-fleet", "unit": "unit.service", "env_file": str(env_path), "config": str(config_path)},
                    {"checks": {"stuck_queue_minutes": 1, "max_sandboxes": 0, "orphan_sandbox_minutes": 0}},
                    findings,
                )
        finally:
            watchdog.unit_active, watchdog.GitHub, watchdog.Daytona = original_active, original_github, original_daytona
        self.assertTrue(any("not covered by this repository profile" in finding for finding in findings))
        self.assertTrue(any("personal-owner/alpha: job queued" in finding for finding in findings))
        self.assertFalse(any("runner group" in finding for finding in findings))

    def test_watchdog_can_skip_queue_scans_while_keeping_runner_group_coverage(self) -> None:
        github_calls: list[str] = []
        daytona_calls: list[tuple] = []
        original_active, original_github, original_daytona = watchdog.unit_active, watchdog.GitHub, watchdog.Daytona
        watchdog.unit_active = lambda unit: True

        class Client:
            def __init__(self, owner, *args, **kwargs):
                self.owner = owner
                github_calls.append("init")

            def require_organization_installation_all_repositories(self):
                github_calls.append("installation-scope")

            def _api(self, method: str, path: str):
                github_calls.append("runner-groups")
                return {"runner_groups": [{"id": 7, "name": "daytona", "visibility": "selected", "allows_public_repositories": False}]}

            def runner_group_repos(self, group_id: int):
                github_calls.append("group-repos")
                return ["private-repository"]

            def installation_repositories(self):
                github_calls.append("installation-repos")
                return [{"name": "private-repository", "private": True, "owner": {"login": "example-org"}}]

            def queued_jobs(self, repositories):
                github_calls.append("queued-jobs")
                raise AssertionError("watchdog GitHub queue scan must be disabled")

        def daytona(*args, **kwargs):
            daytona_calls.append(args)
            return SimpleNamespace(sdk=SimpleNamespace(list=lambda: []))

        watchdog.GitHub = Client
        watchdog.Daytona = daytona
        try:
            with tempfile.TemporaryDirectory() as directory:
                config_path = Path(directory) / "runners.toml"
                config_path.write_text(
                    """
[github]
org = "example-org"
runner_group = "daytona"
[daytona]
target = "us"
[watchdog]
github_queue_scan = false
[[size_class]]
name = "default"
labels = ["self-hosted", "daytona"]
""".strip() + "\n",
                    encoding="utf-8",
                )
                env_path = Path(directory) / "orchestrator.env"
                env_path.write_text(
                    "GH_APP_ID=id\nGH_APP_INSTALLATION_ID=installation\nGH_APP_KEY_PATH=/key\nDAYTONA_API_KEY=id\n",
                    encoding="utf-8",
                )
                findings: list[str] = []
                watchdog.check_instance(
                    {"name": "example-fleet", "unit": "unit.service", "env_file": str(env_path), "config": str(config_path)},
                    {"checks": {"stuck_queue_minutes": 1, "max_sandboxes": 1, "orphan_sandbox_minutes": 0}},
                    findings,
                )
        finally:
            watchdog.unit_active, watchdog.GitHub, watchdog.Daytona = original_active, original_github, original_daytona

        self.assertEqual(github_calls, ["init", "installation-scope", "runner-groups", "group-repos", "installation-repos"])
        self.assertEqual(len(daytona_calls), 1)
        self.assertEqual(findings, [])

    def test_watchdog_defaults_to_github_queue_scan(self) -> None:
        calls: list[str] = []

        class Client:
            def __init__(self, owner, *args, **kwargs):
                self.owner = owner
                calls.append("init")

            def require_organization_installation_all_repositories(self):
                calls.append("installation-scope")

            def _api(self, method: str, path: str):
                calls.append("runner-groups")
                return {"runner_groups": [{"id": 7, "name": "daytona", "visibility": "selected", "allows_public_repositories": False}]}

            def runner_group_repos(self, group_id: int):
                calls.append("group-repos")
                return ["private-repo"]

            def installation_repositories(self):
                calls.append("installation-repos")
                return [{"name": "private-repo", "private": True, "owner": {"login": "existing-org"}}]

            def queued_jobs(self, repositories):
                calls.append("queued-jobs")
                return []

        original_active, original_github = watchdog.unit_active, watchdog.GitHub
        watchdog.unit_active = lambda unit: True
        watchdog.GitHub = Client
        try:
            with tempfile.TemporaryDirectory() as directory:
                config_path = Path(directory) / "runners.toml"
                config_path.write_text(
                    """
[github]
org = "existing-org"
runner_group = "daytona"
[daytona]
target = "us"
[[size_class]]
name = "default"
labels = ["self-hosted", "daytona"]
""".strip() + "\n",
                    encoding="utf-8",
                )
                env_path = Path(directory) / "orchestrator.env"
                env_path.write_text(
                    "GH_APP_ID=id\nGH_APP_INSTALLATION_ID=installation\nGH_APP_KEY_PATH=/key\n",
                    encoding="utf-8",
                )
                findings: list[str] = []
                watchdog.check_instance(
                    {"name": "existing-org", "unit": "unit.service", "env_file": str(env_path), "config": str(config_path)},
                    {"checks": {"stuck_queue_minutes": 1, "max_sandboxes": 0, "orphan_sandbox_minutes": 0}},
                    findings,
                )
        finally:
            watchdog.unit_active, watchdog.GitHub = original_active, original_github

        self.assertEqual(calls, ["init", "installation-scope", "runner-groups", "group-repos", "installation-repos", "queued-jobs"])
        self.assertEqual(findings, [])

    def test_watchdog_reports_incomplete_app_installation_and_continues_coverage_checks(self) -> None:
        calls: list[str] = []

        class Client:
            def __init__(self, owner, *args, **kwargs):
                self.owner = owner
                calls.append("init")

            def require_organization_installation_all_repositories(self):
                calls.append("installation-scope")
                raise RuntimeError(
                    "GitHub App installation must cover all repositories before organization runners can reconcile"
                )

            def _api(self, method: str, path: str):
                calls.append("runner-groups")
                return {"runner_groups": [{
                    "id": 7,
                    "name": "daytona",
                    "visibility": "selected",
                    "allows_public_repositories": False,
                }]}

            def runner_group_repos(self, group_id: int):
                calls.append("group-repos")
                return ["private-repo"]

            def installation_repositories(self):
                calls.append("installation-repos")
                return [{"name": "private-repo", "private": True, "owner": {"login": "existing-org"}}]

            def queued_jobs(self, repositories):
                calls.append("queued-jobs")
                return []

        original_active, original_github = watchdog.unit_active, watchdog.GitHub
        watchdog.unit_active = lambda unit: True
        watchdog.GitHub = Client
        try:
            with tempfile.TemporaryDirectory() as directory:
                config_path = Path(directory) / "runners.toml"
                config_path.write_text(
                    """
[github]
org = "existing-org"
runner_group = "daytona"
[daytona]
target = "us"
[[size_class]]
name = "default"
labels = ["self-hosted", "daytona"]
""".strip() + "\n",
                    encoding="utf-8",
                )
                env_path = Path(directory) / "orchestrator.env"
                env_path.write_text(
                    "GH_APP_ID=id\nGH_APP_INSTALLATION_ID=installation\nGH_APP_KEY_PATH=/key\n",
                    encoding="utf-8",
                )
                findings: list[str] = []
                watchdog.check_instance(
                    {"name": "existing-org", "unit": "unit.service", "env_file": str(env_path), "config": str(config_path)},
                    {"checks": {"stuck_queue_minutes": 1, "max_sandboxes": 0, "orphan_sandbox_minutes": 0}},
                    findings,
                )
        finally:
            watchdog.unit_active, watchdog.GitHub = original_active, original_github

        self.assertEqual(calls, ["init", "installation-scope", "runner-groups", "group-repos", "installation-repos", "queued-jobs"])
        self.assertEqual(len(findings), 1)
        self.assertIn("all repositories", findings[0])

    def test_watchdog_reports_private_repository_without_runner_group_access(self) -> None:
        class Client:
            def __init__(self, owner, *args, **kwargs):
                self.owner = owner

            def require_organization_installation_all_repositories(self):
                return None

            def _api(self, method: str, path: str):
                return {"runner_groups": [{"id": 7, "name": "daytona", "visibility": "selected", "allows_public_repositories": False}]}

            def runner_group_repos(self, group_id: int):
                return ["covered"]

            def installation_repositories(self):
                return [
                    {"name": "covered", "private": True, "owner": {"login": "existing-org"}},
                    {"name": "uncovered", "private": True, "owner": {"login": "existing-org"}},
                ]

            def queued_jobs(self, repositories):
                return []

        original_active, original_github = watchdog.unit_active, watchdog.GitHub
        watchdog.unit_active = lambda unit: True
        watchdog.GitHub = Client
        try:
            with tempfile.TemporaryDirectory() as directory:
                config_path = Path(directory) / "runners.toml"
                config_path.write_text(
                    """
[github]
org = "existing-org"
runner_group = "daytona"
[daytona]
target = "us"
[[size_class]]
name = "default"
labels = ["self-hosted", "daytona"]
""".strip() + "\n",
                    encoding="utf-8",
                )
                env_path = Path(directory) / "orchestrator.env"
                env_path.write_text(
                    "GH_APP_ID=id\nGH_APP_INSTALLATION_ID=installation\nGH_APP_KEY_PATH=/key\n",
                    encoding="utf-8",
                )
                findings: list[str] = []
                watchdog.check_instance(
                    {"name": "existing-org", "unit": "unit.service", "env_file": str(env_path), "config": str(config_path)},
                    {"checks": {"stuck_queue_minutes": 1, "max_sandboxes": 0, "orphan_sandbox_minutes": 0}},
                    findings,
                )
        finally:
            watchdog.unit_active, watchdog.GitHub = original_active, original_github

        self.assertTrue(any("uncovered" in finding and "cannot reach runner group" in finding for finding in findings))

    def test_watchdog_reports_group_repository_outside_app_visibility(self) -> None:
        class Client:
            def __init__(self, owner, *args, **kwargs):
                self.owner = owner

            def require_organization_installation_all_repositories(self):
                return None

            def _api(self, method: str, path: str):
                return {"runner_groups": [{"id": 7, "name": "daytona", "visibility": "selected", "allows_public_repositories": False}]}

            def runner_group_repos(self, group_id: int):
                return ["visible", "not-visible"]

            def installation_repositories(self):
                return [{"name": "visible", "private": True, "owner": {"login": "existing-org"}}]

            def queued_jobs(self, repositories):
                return []

        original_active, original_github = watchdog.unit_active, watchdog.GitHub
        watchdog.unit_active = lambda unit: True
        watchdog.GitHub = Client
        try:
            with tempfile.TemporaryDirectory() as directory:
                config_path = Path(directory) / "runners.toml"
                config_path.write_text(
                    """
[github]
org = "existing-org"
runner_group = "daytona"
[daytona]
target = "us"
[[size_class]]
name = "default"
labels = ["self-hosted", "daytona"]
""".strip() + "\n",
                    encoding="utf-8",
                )
                env_path = Path(directory) / "orchestrator.env"
                env_path.write_text(
                    "GH_APP_ID=id\nGH_APP_INSTALLATION_ID=installation\nGH_APP_KEY_PATH=/key\n",
                    encoding="utf-8",
                )
                findings: list[str] = []
                watchdog.check_instance(
                    {"name": "existing-org", "unit": "unit.service", "env_file": str(env_path), "config": str(config_path)},
                    {"checks": {"stuck_queue_minutes": 1, "max_sandboxes": 0, "orphan_sandbox_minutes": 0}},
                    findings,
                )
        finally:
            watchdog.unit_active, watchdog.GitHub = original_active, original_github

        self.assertTrue(any("not-visible" in finding and "not visible to the Marsh App" in finding for finding in findings))

    def test_watchdog_strict_group_lookup_rejects_public_repository_access(self) -> None:
        client = SimpleNamespace(
            owner="existing-org",
            _api=lambda method, path: {"runner_groups": [{
                "id": 7,
                "name": "daytona",
                "visibility": "selected",
                "allows_public_repositories": True,
            }]},
        )
        with self.assertRaisesRegex(RuntimeError, "not selected private-only"):
            watchdog.selected_runner_group_id(client, "daytona")

    def test_watchdog_sandbox_filter_requires_exact_repository_fleet(self) -> None:
        self.assertTrue(watchdog.fleet_sandbox(
            {"role": "gha-runner", "scope": "repository", "fleet": "personal-fleet"},
            "repository", "personal-owner", "personal-fleet",
        ))
        self.assertFalse(watchdog.fleet_sandbox(
            {"role": "gha-runner", "scope": "repository", "fleet": "other-fleet"},
            "repository", "personal-owner", "personal-fleet",
        ))


if __name__ == "__main__":
    unittest.main()
