from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
import urllib.error
import urllib.parse
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from tests.support import ROOT, load_module


audit = load_module("marsh_snapshot_audit_test", "infra/snapshots/audit-snapshots.py")


def snapshot(
    snapshot_id: str,
    name: str,
    *,
    image: str = "ghcr.io/example/marsh-runner:v7",
    cpu: int = 2,
    memory: int = 4,
    disk: int = 10,
    size: int = 100,
    state: str = "active",
) -> dict[str, object]:
    return {
        "id": snapshot_id,
        "name": name,
        "state": state,
        "size": size,
        "createdAt": "2026-07-13T20:00:00.000Z",
        "imageName": image,
        "cpu": cpu,
        "mem": memory,
        "disk": disk,
        "gpu": 0,
        "sandboxClass": "container",
        "regionIds": ["us"],
        "ref": f"snapshot-ref-{snapshot_id}",
    }


class FakeResponse(io.StringIO):
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()
        return False


class SnapshotAuditTests(unittest.TestCase):
    def test_api_base_cannot_redirect_the_injected_credential(self) -> None:
        self.assertEqual(
            audit.validate_api_base("https://app.daytona.io/api/"),
            "https://app.daytona.io/api",
        )
        for unsafe in (
            "https://attacker.example/api",
            "https://app.daytona.io/other",
            "https://app.daytona.io:8443/api",
            "https://user@app.daytona.io/api",
        ):
            with self.subTest(unsafe=unsafe):
                with self.assertRaisesRegex(audit.AuditError, "must be exactly"):
                    audit.validate_api_base(unsafe)

    def test_loads_exact_references_from_the_public_example_profile(self) -> None:
        references, api_base = audit.load_profile_references(ROOT)

        self.assertEqual(api_base, "https://app.daytona.io/api")
        self.assertEqual(
            [(item.fleet, item.profile, item.size_class) for item in references["marsh-runner-default-example"]],
            [
                ("example", "config/fleets/example/runners.toml", "default"),
            ],
        )
        self.assertEqual(
            [(item.fleet, item.size_class) for item in references["marsh-runner-large-example"]],
            [("example", "large")],
        )

    def test_normalizes_current_daytona_fields_without_guessing_missing_values(self) -> None:
        normalized = audit.normalize_snapshot(snapshot("snap-1", "runner-default-v7"))

        self.assertEqual(normalized.snapshot_id, "snap-1")
        self.assertEqual(normalized.name, "runner-default-v7")
        self.assertEqual(normalized.state, "active")
        self.assertEqual(normalized.size, 100)
        self.assertEqual(normalized.created_at, "2026-07-13T20:00:00.000Z")
        self.assertEqual(normalized.image_name, "ghcr.io/example/marsh-runner:v7")
        self.assertEqual((normalized.cpu, normalized.memory_gib, normalized.disk_gib), (2, 4, 10))
        self.assertEqual(normalized.region_ids, ("us",))

        incomplete = audit.normalize_snapshot({"name": "partial"})
        self.assertIsNone(incomplete.snapshot_id)
        self.assertIsNone(incomplete.size)
        self.assertIsNone(incomplete.created_at)

    def test_report_marks_cross_fleet_shared_candidates_but_never_actions_them(self) -> None:
        references = {
            "alpha-runner-default-v7": [
                audit.ProfileReference("alpha", "config/fleets/alpha/runners.toml", "default")
            ],
            "beta-runner-default-v7": [
                audit.ProfileReference("beta", "config/fleets/beta/runners.toml", "default")
            ],
            "missing-large-v7": [
                audit.ProfileReference("gamma", "config/fleets/gamma/runners.toml", "large")
            ],
        }
        snapshots = audit.snapshots_from_payload({
            "items": [
                snapshot("snap-alpha", "alpha-runner-default-v7"),
                snapshot("snap-beta", "beta-runner-default-v7"),
                snapshot("snap-old", "unreferenced-default-v6", image="ghcr.io/example/marsh-runner:v6"),
            ]
        })

        report = audit.build_report(
            snapshots,
            references,
            "offline-json",
            generated_at="2026-07-14T00:00:00Z",
        )

        self.assertEqual(report["destructive_actions"], [])
        self.assertEqual(report["summary"], {
            "snapshot_count": 3,
            "referenced_snapshot_count": 2,
            "ambiguous_snapshot_count": 0,
            "unreferenced_snapshot_count": 1,
            "unresolved_profile_reference_count": 1,
            "name_collision_count": 0,
            "duplicate_candidate_count": 1,
            "shared_candidate_count": 1,
        })
        candidate = report["duplicate_candidates"][0]
        self.assertTrue(candidate["shared_candidate"])
        self.assertTrue(candidate["all_active"])
        self.assertEqual(candidate["fleets"], ["alpha", "beta"])
        self.assertEqual(candidate["snapshot_ids"], ["snap-alpha", "snap-beta"])
        self.assertEqual(candidate["potential_reclaimable_size"], 100)
        self.assertIn("immutable image digest", candidate["proof_required"])
        self.assertEqual(
            report["unresolved_profile_references"][0]["snapshot_name"],
            "missing-large-v7",
        )
        self.assertEqual(
            report["unreferenced_snapshots"],
            [{"id": "snap-old", "name": "unreferenced-default-v6"}],
        )

    def test_does_not_claim_duplicates_without_image_and_resource_evidence(self) -> None:
        snapshots = [
            audit.normalize_snapshot({
                "id": "one", "name": "one", "state": "active",
                "imageName": "same", "cpu": 2, "mem": 4, "disk": 10,
            }),
            audit.normalize_snapshot({
                "id": "two", "name": "two", "state": "active",
                "imageName": "same", "cpu": 2, "mem": 4, "disk": 10,
            }),
        ]

        report = audit.build_report(snapshots, {}, "offline-json", generated_at="now")

        self.assertEqual(report["duplicate_candidates"], [])
        self.assertEqual(report["summary"]["duplicate_candidate_count"], 0)

        invalid = audit.snapshots_from_payload({"items": [
            snapshot("invalid-one", "invalid-one", cpu=-2),
            snapshot("invalid-two", "invalid-two", cpu=-2),
            {**snapshot("region-one", "region-one"), "regionIds": [""]},
            {**snapshot("region-two", "region-two"), "regionIds": [""]},
        ]})
        invalid_report = audit.build_report(invalid, {}, "offline-json", generated_at="now")
        self.assertEqual(invalid_report["duplicate_candidates"], [])

    def test_duplicate_immutable_names_are_reported_as_ambiguous(self) -> None:
        name = "shared-runner-default-v7"
        references = {
            name: [audit.ProfileReference("alpha", "config/fleets/alpha/runners.toml", "default")]
        }
        report = audit.build_report(
            audit.snapshots_from_payload({"items": [
                snapshot("snapshot-one", name),
                snapshot("snapshot-two", name),
            ]}),
            references,
            "offline-json",
            generated_at="now",
        )

        self.assertEqual(report["summary"]["name_collision_count"], 1)
        self.assertEqual(report["summary"]["referenced_snapshot_count"], 0)
        self.assertEqual(report["summary"]["ambiguous_snapshot_count"], 2)
        self.assertEqual(report["name_collisions"][0]["snapshot_ids"], ["snapshot-one", "snapshot-two"])
        self.assertTrue(all(
            item["reference_status"] == "ambiguous_name_collision"
            for item in report["snapshots"]
        ))

        missing_id = audit.build_report(
            [
                audit.normalize_snapshot(snapshot("snapshot-one", name)),
                audit.normalize_snapshot({"name": name}),
            ],
            references,
            "offline-json",
            generated_at="now",
        )
        self.assertEqual(missing_id["summary"]["name_collision_count"], 1)
        self.assertEqual(missing_id["name_collisions"][0]["snapshot_ids"], [None, "snapshot-one"])

    def test_inventory_rejects_duplicate_ids_and_invalid_pagination(self) -> None:
        with self.assertRaisesRegex(audit.AuditError, "repeated ID"):
            audit.snapshots_from_payload({"items": [
                snapshot("same-id", "one"),
                snapshot("same-id", "two"),
            ]})

        def opener(request, timeout):
            return FakeResponse(json.dumps({"items": [], "page": 1, "totalPages": 1.5}))

        with self.assertRaisesRegex(audit.AuditError, "invalid pagination"):
            audit.fetch_snapshots("https://app.daytona.io/api", "injected-secret", opener=opener)

        responses = iter((
            {"items": [snapshot("one", "one")], "page": 1, "totalPages": 3},
            {"items": [snapshot("two", "two")], "page": 2, "totalPages": 2},
        ))

        def drifting_opener(request, timeout):
            return FakeResponse(json.dumps(next(responses)))

        with self.assertRaisesRegex(audit.AuditError, "changed between pages"):
            audit.fetch_snapshots(
                "https://app.daytona.io/api", "injected-secret", opener=drifting_opener
            )

    def test_live_inventory_uses_get_and_reads_every_page(self) -> None:
        requests = []
        responses = [
            {"items": [snapshot("one", "one")], "page": 1, "totalPages": 2, "total": 2},
            {"items": [snapshot("two", "two")], "page": 2, "totalPages": 2, "total": 2},
        ]

        def opener(request, timeout):
            requests.append((request, timeout))
            return FakeResponse(json.dumps(responses[len(requests) - 1]))

        snapshots = audit.fetch_snapshots(
            "https://app.daytona.io/api", "injected-secret", timeout=3.0, opener=opener
        )

        self.assertEqual([item.snapshot_id for item in snapshots], ["one", "two"])
        self.assertEqual(len(requests), 2)
        for page, (request, timeout) in enumerate(requests, start=1):
            self.assertEqual(request.get_method(), "GET")
            self.assertEqual(timeout, 3.0)
            self.assertEqual(request.get_header("Authorization"), "Bearer injected-secret")
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)
            self.assertEqual(query["page"], [str(page)])
            self.assertEqual(query["limit"], [str(audit.PAGE_LIMIT)])

    def test_provider_errors_are_sanitized(self) -> None:
        secret = "must-not-appear"

        def opener(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 401, secret, {}, None)

        with self.assertRaises(audit.AuditError) as raised:
            audit.fetch_snapshots("https://app.daytona.io/api", secret, opener=opener)

        self.assertEqual(str(raised.exception), "Daytona snapshot inventory request failed with HTTP 401")
        self.assertNotIn(secret, str(raised.exception))

    def test_live_inventory_rejects_redirects_without_a_second_request(self) -> None:
        requests = []

        def redirected(request, timeout):
            requests.append(request)
            raise urllib.error.HTTPError(
                request.full_url,
                302,
                "redirect refused",
                {"Location": "https://attacker.example/collect"},
                None,
            )

        with mock.patch.object(audit, "open_without_redirects", side_effect=redirected):
            with self.assertRaisesRegex(audit.AuditError, "HTTP 302"):
                audit.fetch_snapshots("https://app.daytona.io/api", "injected-secret")

        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].full_url.split("?", 1)[0], "https://app.daytona.io/api/snapshots")
        self.assertEqual(requests[0].get_header("Authorization"), "Bearer injected-secret")
        self.assertIsNone(
            audit.RejectRedirects().redirect_request(
                requests[0], None, 302, "redirect refused",
                {"Location": "https://attacker.example/collect"},
                "https://attacker.example/collect",
            )
        )

    def test_cli_requires_injected_credential_but_offline_mode_does_not(self) -> None:
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=True), redirect_stderr(stderr):
            result = audit.main([])
        self.assertEqual(result, 2)
        self.assertIn("DAYTONA_API_KEY is missing", stderr.getvalue())

        stderr = io.StringIO()
        with mock.patch.dict(
            os.environ,
            {"DAYTONA_API_KEY": "secret://unresolved"},
            clear=True,
        ), redirect_stderr(stderr):
            result = audit.main([])
        self.assertEqual(result, 2)
        self.assertIn("DAYTONA_API_KEY is unresolved", stderr.getvalue())

        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "snapshots.json"
            input_path.write_text(json.dumps({"items": []}), encoding="utf-8")
            stdout = io.StringIO()
            with mock.patch.dict(os.environ, {}, clear=True), redirect_stdout(stdout):
                result = audit.main(["--input", str(input_path), "--compact"])
        self.assertEqual(result, 0)
        report = json.loads(stdout.getvalue())
        self.assertEqual(report["source"], "offline-json")
        self.assertEqual(report["destructive_actions"], [])


if __name__ == "__main__":
    unittest.main()
