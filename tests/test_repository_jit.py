from __future__ import annotations

import json
import threading
import unittest
from types import SimpleNamespace

from tests.support import load_orchestrator


orch = load_orchestrator()


class RepositoryJitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gh = orch.GitHub(
            "personal-owner", "app", "installation", "/key", scope="repository", repositories=["alpha", "beta"],
            fleet_name="personal-fleet",
        )
        self.calls: list[tuple[str, str, dict | None]] = []

        def api(method: str, path: str, body: dict | None = None, bearer: str | None = None) -> dict:
            self.calls.append((method, path, body))
            if method == "POST" and path.endswith("generate-jitconfig"):
                return {"runner": {"id": 41}, "encoded_jit_config": "jit"}
            if method == "GET" and path.endswith("/41"):
                return {"busy": False}
            if method == "GET" and "actions/runs" in path:
                if "/runs/7/jobs" in path:
                    return {"jobs": [{"status": "queued", "labels": ["self-hosted", "daytona"]}]}
                if "status=queued" in path:
                    return {"workflow_runs": [{"id": 7}]}
                return {"workflow_runs": []}
            if method == "GET" and "actions/runners" in path:
                return {"runners": []}
            return {}

        self.gh._api = api  # type: ignore[method-assign]

    def test_repo_jit_lifecycle_uses_only_repository_endpoints(self) -> None:
        runner, jit = self.gh.mint_jit(1, ["self-hosted", "daytona"], "alpha")
        self.assertEqual(jit, "jit")
        self.assertEqual(runner, orch.RunnerRef(41, "alpha"))
        self.assertTrue(self.gh.runner_busy(runner) is False)
        self.gh.delete_runner(runner)

        paths = [path for _, path, _ in self.calls]
        self.assertIn("marsh-fleet-personal-fleet", self.calls[0][2]["labels"])
        self.assertIn("/repos/personal-owner/alpha/actions/runners/generate-jitconfig", paths)
        self.assertIn("/repos/personal-owner/alpha/actions/runners/41", paths)
        self.assertFalse(any(path.startswith("/orgs/") for path in paths))

    def test_repository_scope_downscopes_the_installation_token_to_its_allowlist(self) -> None:
        captured: list[dict] = []
        self.gh._app_jwt = lambda: "jwt"  # type: ignore[method-assign]

        def send(request):
            captured.append(json.loads(request.data.decode()))
            return {"token": "scoped"}

        self.gh._send_request_locked = send  # type: ignore[method-assign]
        self.assertEqual(self.gh._installation_token_locked(), "scoped")
        self.assertEqual(captured, [{"repositories": ["alpha", "beta"]}])

    def test_organization_scope_keeps_its_existing_unrestricted_installation_token_contract(self) -> None:
        gh = orch.GitHub("existing-org", "app", "installation", "/key")
        captured: list[dict] = []
        gh._app_jwt = lambda: "jwt"  # type: ignore[method-assign]

        def send(request):
            captured.append(json.loads(request.data.decode()))
            return {"token": "organization"}

        gh._send_request_locked = send  # type: ignore[method-assign]
        self.assertEqual(gh._installation_token_locked(), "organization")
        self.assertEqual(captured, [{}])

    def test_named_repository_group_and_private_routing_label_fail_closed(self) -> None:
        self.gh.repository_runner_groups = lambda repository: [  # type: ignore[method-assign]
            {"id": 44, "name": "restricted-runner"}
        ]
        self.assertEqual(self.gh.current_repository_group_id("restricted-runner"), 44)
        classes = [{"name": "private", "labels": ["self-hosted", "restricted-runner"]}]
        self.assertIsNone(orch.match_class({"self-hosted"}, classes, {"restricted-runner"}))
        self.assertEqual(
            orch.match_class({"self-hosted", "restricted-runner"}, classes, {"restricted-runner"}),
            classes[0],
        )

    def test_restricted_network_policy_is_passed_to_daytona_sandbox_create(self) -> None:
        original_sdk = orch.DaytonaSDK

        class SDK:
            def __init__(self, *args, **kwargs):
                self.params = None

            def create(self, params, timeout):
                self.params = params
                return type("Sandbox", (), {"id": "sandbox"})()

            def get(self, sandbox_id):
                self.sandbox_id = sandbox_id

        orch.DaytonaSDK = SDK
        try:
            policy = orch.network_policy_from_config({
                "network": {
                    "policy": "deny-by-default",
                    "cidr_allow_list": ["10.0.0.10/32", "10.0.0.11/32"],
                    "domain_allow_list": ["github.com", "*.github.com"],
                },
            })
            daytona = orch.Daytona("api-key", "private-target", None, network_policy=policy)
            daytona.create_sandbox("snapshot", 60, "default", "private-repository")
        finally:
            orch.DaytonaSDK = original_sdk

        self.assertEqual(daytona.sdk.params.kwargs["network_allow_list"], "10.0.0.10/32,10.0.0.11/32")
        self.assertEqual(daytona.sdk.params.kwargs["domain_allow_list"], "github.com,*.github.com")

    def test_organization_lifecycle_keeps_organization_endpoint_contract(self) -> None:
        gh = orch.GitHub("existing-org", "app", "installation", "/key")
        calls: list[str] = []

        def api(method: str, path: str, body=None, bearer=None):
            calls.append(path)
            if method == "POST":
                return {"runner": {"id": 9}, "encoded_jit_config": "jit"}
            if method == "GET":
                return {"busy": False}
            return {}

        gh._api = api  # type: ignore[method-assign]
        runner, _ = gh.mint_jit(5, ["self-hosted", "daytona"])
        self.assertFalse(gh.runner_busy(runner))
        gh.delete_runner(runner)
        self.assertEqual(calls, [
            "/orgs/existing-org/actions/runners/generate-jitconfig",
            "/orgs/existing-org/actions/runners/9",
            "/orgs/existing-org/actions/runners/9",
        ])

    def test_queued_jobs_retain_owning_repository(self) -> None:
        jobs = self.gh.queued_jobs(["alpha"])
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].repository, "alpha")
        self.assertEqual(jobs[0].job["status"], "queued")

    def test_dotgithub_repository_is_the_only_leading_dot_path_exception(self) -> None:
        organization_client = orch.GitHub("existing-org", "app", "installation", "/key")
        self.assertEqual(
            organization_client._repository_path(".github"),
            "/repos/existing-org/.github",
        )
        for invalid in (".github-backup", ".config", "../github", "alpha/beta", "alpha%2Fbeta"):
            with self.subTest(repository=invalid):
                with self.assertRaisesRegex(ValueError, "invalid repository name"):
                    organization_client._repository_path(invalid)

    def test_repo_a_supply_does_not_hide_repo_b_demand(self) -> None:
        classes = [{"name": "default", "labels": ["self-hosted", "daytona"], "min_idle": 0, "max": 2}]
        queued = [orch.QueuedJob("beta", {"labels": ["self-hosted", "daytona"]})]
        self.gh.queued_jobs = lambda repositories: queued  # type: ignore[method-assign]
        self.gh.runners_busy_map = lambda: {}  # type: ignore[method-assign]
        spawned: list[str | None] = []
        original_spawn = orch.spawn_cycle
        orch.REGISTRY.clear()
        orch.REGISTRY["alpha-idle"] = orch.Cycle(
            cls_name="default", state="IDLE", idle_deadline_secs=1,
            repository="alpha", runner=orch.RunnerRef(1, "alpha"),
        )
        orch.spawn_cycle = lambda *args, **kwargs: spawned.append(args[-1])  # type: ignore[assignment]
        try:
            orch._reconcile_repositories(
                self.gh, 1, classes, orch.BusyMap(), SimpleNamespace(),
                orch.Lifecycle(60, 1, 1, 1), threading.Event(),
            )
        finally:
            orch.spawn_cycle = original_spawn
            orch.REGISTRY.clear()
        self.assertEqual(spawned, ["beta"])

    def test_global_class_cap_is_shared_across_repository_demand(self) -> None:
        classes = [{"name": "default", "labels": ["self-hosted", "daytona"], "min_idle": 0, "max": 1}]
        self.gh.queued_jobs = lambda repositories: [  # type: ignore[method-assign]
            orch.QueuedJob("alpha", {"labels": ["self-hosted", "daytona"]}),
            orch.QueuedJob("beta", {"labels": ["self-hosted", "daytona"]}),
        ]
        self.gh.runners_busy_map = lambda: {}  # type: ignore[method-assign]
        spawned: list[str | None] = []
        original_spawn = orch.spawn_cycle
        orch.REGISTRY.clear()
        orch.spawn_cycle = lambda *args, **kwargs: spawned.append(args[-1])  # type: ignore[assignment]
        try:
            orch._reconcile_repositories(
                self.gh, 1, classes, orch.BusyMap(), SimpleNamespace(),
                orch.Lifecycle(60, 1, 1, 1), threading.Event(),
            )
        finally:
            orch.spawn_cycle = original_spawn
        self.assertEqual(spawned, ["alpha"])

    def test_reap_only_deletes_exact_repository_fleet_sandboxes(self) -> None:
        class Sandbox:
            def __init__(self, labels: dict):
                self.labels = labels
                self.deleted = False

            def delete(self) -> None:
                self.deleted = True

        ours = Sandbox({"role": "gha-runner", "scope": "repository", "fleet": "personal-fleet"})
        other = Sandbox({"role": "gha-runner", "scope": "repository", "fleet": "other-fleet"})
        self.gh.runners = lambda: []  # type: ignore[method-assign]
        original_scope, original_fleet = orch.GITHUB_SCOPE, orch.FLEET_LABEL
        orch.GITHUB_SCOPE, orch.FLEET_LABEL = "repository", "personal-fleet"
        try:
            orch.reap(self.gh, SimpleNamespace(list=lambda: [ours, other]))
        finally:
            orch.GITHUB_SCOPE, orch.FLEET_LABEL = original_scope, original_fleet
        self.assertTrue(ours.deleted)
        self.assertFalse(other.deleted)

    def test_reap_keeps_other_or_untagged_repository_runner_registrations(self) -> None:
        def runner(runner_id: int, labels: list[str]):
            return orch.RunnerRef(runner_id, "alpha"), {
                "id": runner_id,
                "status": "offline",
                "name": f"marsh-{runner_id}",
                "labels": [{"name": label} for label in labels],
            }

        rows = [
            runner(1, ["daytona", "marsh-fleet-personal-fleet"]),
            runner(2, ["daytona", "marsh-fleet-other-fleet"]),
            runner(3, ["daytona"]),
        ]
        deleted: list[orch.RunnerRef] = []
        self.gh.runners = lambda: list(rows)  # type: ignore[method-assign]

        def delete(ref: orch.RunnerRef) -> None:
            deleted.append(ref)
            rows[:] = [item for item in rows if item[0] != ref]

        self.gh.delete_runner = delete  # type: ignore[method-assign]
        original_scope, original_fleet = orch.GITHUB_SCOPE, orch.FLEET_LABEL
        orch.GITHUB_SCOPE, orch.FLEET_LABEL = "repository", "personal-fleet"
        try:
            orch.reap(self.gh, SimpleNamespace(list=lambda: []))
        finally:
            orch.GITHUB_SCOPE, orch.FLEET_LABEL = original_scope, original_fleet
        self.assertEqual(deleted, [orch.RunnerRef(1, "alpha")])

    def test_reap_fails_closed_when_runner_deregistration_is_unconfirmed(self) -> None:
        row = (orch.RunnerRef(1, "alpha"), {
            "id": 1,
            "status": "offline",
            "name": "marsh-1",
            "labels": [
                {"name": "daytona"},
                {"name": "marsh-fleet-personal-fleet"},
            ],
        })
        self.gh.runners = lambda: [row]  # type: ignore[method-assign]
        self.gh.delete_runner = lambda runner: False  # type: ignore[method-assign]
        original_scope, original_fleet = orch.GITHUB_SCOPE, orch.FLEET_LABEL
        orch.GITHUB_SCOPE, orch.FLEET_LABEL = "repository", "personal-fleet"
        try:
            with self.assertRaisesRegex(RuntimeError, "could not confirm stale GitHub runner"):
                orch.reap(self.gh, SimpleNamespace(list=lambda: []))
        finally:
            orch.GITHUB_SCOPE, orch.FLEET_LABEL = original_scope, original_fleet


if __name__ == "__main__":
    unittest.main()
