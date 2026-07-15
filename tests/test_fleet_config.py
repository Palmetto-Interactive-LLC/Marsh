from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path

from tests.support import ROOT, load_module


fleet_config = load_module("marsh_fleet_config_test", "scripts/fleet_config.py")


def repository_profile(min_idle: int = 0, repositories: str = '"alpha", "beta"') -> str:
    return textwrap.dedent(
        f"""
        [github]
        scope = "repository"
        owner = "personal-owner"
        runner_group_id = 1
        repositories = [{repositories}]

        [daytona]
        target = "us"

        [[size_class]]
        name = "default"
        labels = ["self-hosted", "daytona"]
        snapshot = "default"
        min_idle = {min_idle}
        max = 2

        [[size_class]]
        name = "large"
        labels = ["self-hosted", "daytona", "large"]
        snapshot = "large"
        min_idle = 0
        max = 1
        """
    ).strip() + "\n"


def organization_profile(min_idle: int = 0, warm_floor_reason: str | None = None) -> str:
    profile = textwrap.dedent(
        f"""
        [github]
        org = "example-org"
        runner_group = "daytona"

        [daytona]
        target = "us"

        [[size_class]]
        name = "default"
        labels = ["self-hosted", "daytona"]
        snapshot = "default"
        min_idle = {min_idle}
        max = 2

        [[size_class]]
        name = "large"
        labels = ["self-hosted", "daytona", "large"]
        snapshot = "large"
        min_idle = 0
        max = 1
        """
    ).strip() + "\n"
    if warm_floor_reason is not None:
        profile = profile.replace(
            f"min_idle = {min_idle}\nmax = 2",
            f'min_idle = {min_idle}\nwarm_floor_reason = "{warm_floor_reason}"\nmax = 2',
            1,
        )
    return profile


def restricted_network_profile() -> str:
    profile = repository_profile()
    profile = profile.replace('runner_group_id = 1', 'runner_group = "restricted-runner"')
    profile = profile.replace('target = "us"', 'target = "private-target"')
    profile = profile.replace('["self-hosted", "daytona", "large"]', '["self-hosted", "restricted-runner", "large"]')
    profile = profile.replace('["self-hosted", "daytona"]', '["self-hosted", "restricted-runner"]')
    return profile + textwrap.dedent(
        """
        [network]
        policy = "deny-by-default"
        cidr_allow_list = ["10.0.0.10/32", "10.0.0.11/32"]
        domain_allow_list = ["github.com", "*.github.com"]

        [routing]
        required_labels = ["restricted-runner"]
        """
    ).lstrip()


class FleetConfigTests(unittest.TestCase):
    def write_profile(self, content: str) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "runners.toml"
        path.write_text(content, encoding="utf-8")
        return path

    def test_public_example_profile_validates_with_zero_warm_floor(self) -> None:
        fleets = fleet_config.load_fleets(ROOT)
        self.assertEqual([fleet.name for fleet in fleets], ["example"])
        self.assertTrue(all(fleet.scope == "organization" for fleet in fleets))
        self.assertTrue(
            all(
                size_class["min_idle"] == 0
                for fleet in fleets
                for size_class in fleet.profile["size_class"]
            )
        )
        example = fleets[0]
        self.assertEqual(example.owner, "example-org")
        self.assertEqual(example.profile["poller"], {
            "interval_secs": 20,
            "request_spacing_secs": 1,
        })
        self.assertEqual(
            [size_class["labels"] for size_class in example.profile["size_class"]],
            [
                ["self-hosted", "daytona", "marsh"],
                ["self-hosted", "daytona", "marsh", "large"],
            ],
        )
        self.assertEqual(example.profile["daytona"]["target"], "example-target")

    def test_repository_profile_requires_explicit_safe_scope(self) -> None:
        profile = fleet_config.validate_profile(
            self.write_profile(repository_profile()), "repository", "personal-owner"
        )
        self.assertEqual(profile["github"]["repositories"], ["alpha", "beta"])

    def test_repository_profile_allows_only_the_dotgithub_leading_dot_exception(self) -> None:
        profile = fleet_config.validate_profile(
            self.write_profile(repository_profile(repositories='".github"')), "repository", "personal-owner"
        )
        self.assertEqual(profile["github"]["repositories"], [".github"])
        for invalid in ('".github-backup"', '"../github"', '"alpha/beta"'):
            with self.subTest(repositories=invalid):
                with self.assertRaisesRegex(ValueError, "invalid repository name"):
                    fleet_config.validate_profile(
                        self.write_profile(repository_profile(repositories=invalid)),
                        "repository",
                        "personal-owner",
                    )

    def test_repository_profile_rejects_warm_floor_and_duplicate_targets(self) -> None:
        with self.assertRaisesRegex(ValueError, "min_idle = 0"):
            fleet_config.validate_profile(
                self.write_profile(repository_profile(min_idle=1)), "repository", "personal-owner"
            )
        with self.assertRaisesRegex(ValueError, "duplicate repository"):
            fleet_config.validate_profile(
                self.write_profile(repository_profile(repositories='"alpha", "ALPHA"')),
                "repository",
                "personal-owner",
            )

    def test_organization_warm_floor_requires_an_auditable_reason(self) -> None:
        for profile in (
            organization_profile(min_idle=1),
            organization_profile(min_idle=1, warm_floor_reason="   "),
        ):
            with self.subTest(profile=profile):
                with self.assertRaisesRegex(ValueError, "must define a non-empty warm_floor_reason"):
                    fleet_config.validate_profile(
                        self.write_profile(profile),
                        "organization",
                        "example-org",
                    )

        profile = fleet_config.validate_profile(
            self.write_profile(
                organization_profile(min_idle=1, warm_floor_reason="Measured pickup-latency SLO")
            ),
            "organization",
            "example-org",
        )
        self.assertEqual(profile["size_class"][0]["min_idle"], 1)
        self.assertEqual(
            profile["size_class"][0]["warm_floor_reason"],
            "Measured pickup-latency SLO",
        )

    def test_size_class_rejects_boolean_floor(self) -> None:
        profile = organization_profile().replace("min_idle = 0", "min_idle = true", 1)
        with self.assertRaisesRegex(ValueError, "invalid min_idle"):
            fleet_config.validate_profile(
                self.write_profile(profile), "organization", "example-org"
            )

    def test_size_class_rejects_floor_above_capacity(self) -> None:
        profile = organization_profile(min_idle=3, warm_floor_reason="Measured SLO")
        with self.assertRaisesRegex(ValueError, "min_idle cannot exceed max"):
            fleet_config.validate_profile(
                self.write_profile(profile), "organization", "example-org"
            )

    def test_zero_warm_floor_rejects_stale_reason(self) -> None:
        with self.assertRaisesRegex(ValueError, "only when min_idle is nonzero"):
            fleet_config.validate_profile(
                self.write_profile(
                    organization_profile(min_idle=0, warm_floor_reason="No longer needed")
                ),
                "organization",
                "example-org",
            )

    def test_repository_profile_rejects_excessive_poll_frequency(self) -> None:
        too_fast = repository_profile() + "\n[poller]\ninterval_secs = 20\n"
        with self.assertRaisesRegex(ValueError, "at least 60"):
            fleet_config.validate_profile(self.write_profile(too_fast), "repository", "personal-owner")

    def test_poller_request_spacing_is_non_negative_and_optional(self) -> None:
        zero_spacing = repository_profile() + "\n[poller]\ninterval_secs = 60\nrequest_spacing_secs = 0\n"
        profile = fleet_config.validate_profile(self.write_profile(zero_spacing), "repository", "personal-owner")
        self.assertEqual(profile["poller"]["request_spacing_secs"], 0)

        negative_spacing = repository_profile() + "\n[poller]\ninterval_secs = 60\nrequest_spacing_secs = -1\n"
        with self.assertRaisesRegex(ValueError, "request_spacing_secs"):
            fleet_config.validate_profile(self.write_profile(negative_spacing), "repository", "personal-owner")

        boolean_spacing = repository_profile() + "\n[poller]\ninterval_secs = 60\nrequest_spacing_secs = true\n"
        with self.assertRaisesRegex(ValueError, "request_spacing_secs"):
            fleet_config.validate_profile(self.write_profile(boolean_spacing), "repository", "personal-owner")

    def test_repository_profile_reserves_named_runner_group_for_restricted_profile(self) -> None:
        invalid = repository_profile().replace("runner_group_id = 1", 'runner_group = "daytona"')
        with self.assertRaisesRegex(ValueError, "reserved for restrictive"):
            fleet_config.validate_profile(self.write_profile(invalid), "repository", "personal-owner")

    def test_restricted_network_profile_requires_private_cidrs_and_exclusive_selector(self) -> None:
        profile = fleet_config.validate_profile(
            self.write_profile(restricted_network_profile()), "repository", "personal-owner"
        )
        self.assertEqual(profile["github"]["runner_group"], "restricted-runner")

        public_cidr = restricted_network_profile().replace("10.0.0.10/32", "8.8.8.8/32")
        with self.assertRaisesRegex(ValueError, "must be private IPv4"):
            fleet_config.validate_profile(self.write_profile(public_cidr), "repository", "personal-owner")

        shared_label = restricted_network_profile().replace('"restricted-runner"]', '"restricted-runner", "daytona"]', 1)
        with self.assertRaisesRegex(ValueError, "must not share daytona"):
            fleet_config.validate_profile(self.write_profile(shared_label), "repository", "personal-owner")

        numeric_group = restricted_network_profile().replace('runner_group = "restricted-runner"', "runner_group_id = 1")
        with self.assertRaisesRegex(ValueError, "requires a named"):
            fleet_config.validate_profile(self.write_profile(numeric_group), "repository", "personal-owner")

        mismatched_group = restricted_network_profile().replace('runner_group = "restricted-runner"', 'runner_group = "other-group"')
        with self.assertRaisesRegex(ValueError, "must match exactly"):
            fleet_config.validate_profile(self.write_profile(mismatched_group), "repository", "personal-owner")

    def test_manifest_allows_one_organization_fleet_and_one_repository_fleet_for_same_owner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config/fleets/org").mkdir(parents=True)
            (root / "config/fleets/repo").mkdir(parents=True)
            (root / "config/fleets/manifest.toml").write_text(textwrap.dedent("""
                [manifest]
                version = 1

                [[fleet]]
                name = "org"
                scope = "organization"
                organization = "shared-owner"
                profile = "config/fleets/org/runners.toml"
                installation_id_key = "installation_id_org"

                [[fleet]]
                name = "repo"
                scope = "repository"
                owner = "shared-owner"
                profile = "config/fleets/repo/runners.toml"
                installation_id_key = "installation_id_repo"
            """).strip() + "\n", encoding="utf-8")
            (root / "config/fleets/org/runners.toml").write_text(textwrap.dedent("""
                [github]
                org = "shared-owner"
                runner_group = "daytona"
                [daytona]
                target = "us"
                [[size_class]]
                name = "default"
                labels = ["self-hosted", "daytona"]
                snapshot = "default"
                min_idle = 0
                max = 1
                [[size_class]]
                name = "large"
                labels = ["self-hosted", "daytona", "large"]
                snapshot = "large"
                min_idle = 0
                max = 1
            """).strip() + "\n", encoding="utf-8")
            (root / "config/fleets/repo/runners.toml").write_text(
                repository_profile().replace("personal-owner", "shared-owner"), encoding="utf-8"
            )
            fleets = fleet_config.load_fleets(root)
        self.assertEqual([(fleet.name, fleet.scope) for fleet in fleets], [("org", "organization"), ("repo", "repository")])

    def test_manifest_rejects_same_owner_reused_within_one_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config/fleets/org").mkdir(parents=True)
            (root / "config/fleets/manifest.toml").write_text(textwrap.dedent("""
                [manifest]
                version = 1

                [[fleet]]
                name = "org"
                organization = "shared-owner"
                profile = "config/fleets/org/runners.toml"
                installation_id_key = "installation_id_org"

                [[fleet]]
                name = "org-two"
                organization = "shared-owner"
                profile = "config/fleets/org-two/runners.toml"
                installation_id_key = "installation_id_org_two"
            """).strip() + "\n", encoding="utf-8")
            (root / "config/fleets/org/runners.toml").write_text(textwrap.dedent("""
                [github]
                org = "shared-owner"
                runner_group = "daytona"
                [daytona]
                target = "us"
                [[size_class]]
                name = "default"
                labels = ["self-hosted", "daytona"]
                snapshot = "default"
                min_idle = 0
                max = 1
                [[size_class]]
                name = "large"
                labels = ["self-hosted", "daytona", "large"]
                snapshot = "large"
                min_idle = 0
                max = 1
            """).strip() + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate GitHub owner .* in 'organization' scope"):
                fleet_config.load_fleets(root)


if __name__ == "__main__":
    unittest.main()
