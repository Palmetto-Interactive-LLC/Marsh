#!/usr/bin/env python3
"""Read-only Daytona snapshot inventory with Marsh fleet references.

The live mode performs only paginated ``GET /snapshots`` requests. It emits an
allowlisted JSON report and never includes credentials, response headers, or
provider error bodies. Use ``--input`` to audit a previously captured response
without any network access.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


PAGE_LIMIT = 200
MAX_PAGES = 1_000
DAYTONA_API_BASE = "https://app.daytona.io/api"


class AuditError(ValueError):
    """A safe-to-print snapshot audit failure."""


class RejectRedirects(urllib.request.HTTPRedirectHandler):
    """Never forward the Daytona bearer credential through an HTTP redirect."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        return None


def open_without_redirects(request: urllib.request.Request, timeout: float):
    return urllib.request.build_opener(RejectRedirects()).open(request, timeout=timeout)


@dataclass(frozen=True)
class ProfileReference:
    fleet: str
    profile: str
    size_class: str


@dataclass(frozen=True)
class Snapshot:
    snapshot_id: str | None
    name: str | None
    state: str | None
    size: int | float | None
    created_at: str | None
    image_name: str | None
    snapshot_ref: str | None
    cpu: int | float | None
    memory_gib: int | float | None
    disk_gib: int | float | None
    gpu: int | float | None
    sandbox_class: str | None
    region_ids: tuple[str, ...]


def repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def load_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise AuditError(f"could not read fleet configuration {path}: {exc}") from None
    if not isinstance(data, dict):
        raise AuditError(f"fleet configuration {path} must contain a TOML table")
    return data


def _profile_path(root: Path, relative: object) -> Path:
    if not isinstance(relative, str) or not relative:
        raise AuditError("fleet manifest profile paths must be non-empty strings")
    candidate = (root / relative).resolve()
    fleet_root = (root / "config" / "fleets").resolve()
    if candidate.parent != fleet_root and fleet_root not in candidate.parents:
        raise AuditError(f"fleet profile escapes config/fleets: {relative!r}")
    return candidate


def load_profile_references(root: Path) -> tuple[dict[str, list[ProfileReference]], str]:
    """Return configured snapshot references and the single Daytona API base."""
    manifest = load_toml(root / "config" / "fleets" / "manifest.toml")
    entries = manifest.get("fleet")
    if not isinstance(entries, list) or not entries:
        raise AuditError("fleet manifest must contain at least one [[fleet]]")

    references: dict[str, list[ProfileReference]] = defaultdict(list)
    api_bases: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise AuditError("every fleet manifest entry must be a TOML table")
        fleet = entry.get("name")
        profile_value = entry.get("profile")
        if not isinstance(fleet, str) or not fleet:
            raise AuditError("every fleet manifest entry must have a name")
        profile_path = _profile_path(root, profile_value)
        profile = load_toml(profile_path)
        relative_profile = profile_path.relative_to(root.resolve()).as_posix()

        daytona = profile.get("daytona")
        if not isinstance(daytona, dict):
            raise AuditError(f"{relative_profile}: missing [daytona]")
        api_base = daytona.get("api_base", "https://app.daytona.io/api")
        if not isinstance(api_base, str) or not api_base:
            raise AuditError(f"{relative_profile}: [daytona].api_base must be a string")
        api_bases.add(validate_api_base(api_base))

        classes = profile.get("size_class")
        if not isinstance(classes, list):
            raise AuditError(f"{relative_profile}: missing [[size_class]]")
        for size_class in classes:
            if not isinstance(size_class, dict):
                raise AuditError(f"{relative_profile}: invalid [[size_class]]")
            name = size_class.get("name")
            snapshot = size_class.get("snapshot")
            if not isinstance(name, str) or not isinstance(snapshot, str):
                raise AuditError(f"{relative_profile}: size classes require name and snapshot")
            references[snapshot].append(ProfileReference(fleet, relative_profile, name))

    if len(api_bases) != 1:
        raise AuditError("production fleet profiles must use exactly one Daytona API base")
    for snapshot_references in references.values():
        snapshot_references.sort(key=lambda item: (item.fleet, item.size_class, item.profile))
    return dict(references), next(iter(api_bases))


def validate_api_base(value: str) -> str:
    parsed = urllib.parse.urlsplit(value.rstrip("/"))
    expected = urllib.parse.urlsplit(DAYTONA_API_BASE)
    if (
        parsed.scheme != expected.scheme
        or parsed.netloc != expected.netloc
        or parsed.path != expected.path
        or parsed.query
        or parsed.fragment
    ):
        # The live request attaches DAYTONA_API_KEY. Never let a checked-in
        # profile redirect that bearer credential to a different origin/path.
        raise AuditError(f"Daytona API base must be exactly {DAYTONA_API_BASE}")
    return DAYTONA_API_BASE


def _number(value: object) -> int | float | None:
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value < 0):
        return None
    return value


def _string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def normalize_snapshot(raw: object) -> Snapshot:
    if not isinstance(raw, dict):
        raise AuditError("every Daytona snapshot item must be a JSON object")
    resources = raw.get("resources")
    if not isinstance(resources, dict):
        resources = {}
    build_info = raw.get("buildInfo", raw.get("build_info"))
    if not isinstance(build_info, dict):
        build_info = {}
    regions = raw.get("regionIds", raw.get("region_ids", []))
    if not isinstance(regions, list):
        regions = []

    return Snapshot(
        snapshot_id=_string(raw.get("id")),
        name=_string(raw.get("name")),
        state=_string(raw.get("state")),
        size=_number(raw.get("size")),
        created_at=_string(raw.get("createdAt", raw.get("created_at"))),
        image_name=_string(raw.get("imageName", raw.get("image_name", raw.get("image")))),
        snapshot_ref=_string(raw.get("ref", build_info.get("snapshotRef", build_info.get("snapshot_ref")))),
        cpu=_number(raw.get("cpu", resources.get("cpu"))),
        memory_gib=_number(raw.get("mem", raw.get("memory", resources.get("memory")))),
        disk_gib=_number(raw.get("disk", resources.get("disk"))),
        gpu=_number(raw.get("gpu", resources.get("gpu"))),
        sandbox_class=_string(raw.get("sandboxClass", raw.get("sandbox_class"))),
        region_ids=tuple(sorted(
            region for region in regions if isinstance(region, str) and region
        )),
    )


def snapshots_from_payload(payload: object) -> list[Snapshot]:
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict) and isinstance(payload.get("items"), list):
        items = payload["items"]
    else:
        raise AuditError("snapshot response must be a JSON array or an object with an items array")
    return validate_snapshot_ids([normalize_snapshot(item) for item in items])


def validate_snapshot_ids(snapshots: list[Snapshot]) -> list[Snapshot]:
    seen: set[str] = set()
    for snapshot in snapshots:
        if snapshot.snapshot_id is None:
            continue
        if snapshot.snapshot_id in seen:
            raise AuditError(f"Daytona snapshot inventory repeated ID {snapshot.snapshot_id!r}")
        seen.add(snapshot.snapshot_id)
    return sorted(snapshots, key=lambda item: (item.name or "", item.snapshot_id or ""))


def _fetch_page(
    api_base: str,
    api_key: str,
    page: int,
    timeout: float,
    opener: Callable[..., Any],
) -> dict[str, Any]:
    query = urllib.parse.urlencode({
        "page": page,
        "limit": PAGE_LIMIT,
        "sort": "createdAt",
        "order": "asc",
    })
    request = urllib.request.Request(
        f"{api_base}/snapshots?{query}",
        headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
        method="GET",
    )
    try:
        with opener(request, timeout=timeout) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        status = exc.code
        exc.close()
        raise AuditError(f"Daytona snapshot inventory request failed with HTTP {status}") from None
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        raise AuditError("Daytona snapshot inventory request failed") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise AuditError("Daytona snapshot inventory returned an unexpected response shape")
    return payload


def fetch_snapshots(
    api_base: str,
    api_key: str,
    timeout: float = 20.0,
    opener: Callable[..., Any] | None = None,
) -> list[Snapshot]:
    """Fetch all pages using only Daytona's read-only snapshot list endpoint."""
    request_opener = opener or open_without_redirects
    snapshots: list[Snapshot] = []
    page = 1
    expected_total_pages: int | None = None
    while page <= MAX_PAGES:
        payload = _fetch_page(api_base, api_key, page, timeout, request_opener)
        snapshots.extend(normalize_snapshot(item) for item in payload["items"])
        total_pages = payload.get("totalPages", payload.get("total_pages", 1))
        if (not isinstance(total_pages, int) or isinstance(total_pages, bool)
                or total_pages < 1 or total_pages > MAX_PAGES):
            raise AuditError("Daytona snapshot inventory returned invalid pagination metadata")
        if expected_total_pages is None:
            expected_total_pages = total_pages
        elif total_pages != expected_total_pages:
            raise AuditError("Daytona snapshot inventory pagination changed between pages")
        returned_page = payload.get("page")
        if (returned_page is not None
                and (not isinstance(returned_page, int) or isinstance(returned_page, bool)
                     or returned_page != page)):
            raise AuditError("Daytona snapshot inventory returned an unexpected page")
        if page >= expected_total_pages:
            return validate_snapshot_ids(snapshots)
        page += 1
    raise AuditError("Daytona snapshot inventory exceeded the pagination safety limit")


def _candidate_key(snapshot: Snapshot) -> tuple[object, ...] | None:
    if (not snapshot.image_name
            or any(value is None for value in (
                snapshot.cpu, snapshot.memory_gib, snapshot.disk_gib,
                snapshot.gpu, snapshot.sandbox_class,
            ))
            or not snapshot.region_ids):
        return None
    return (
        snapshot.image_name,
        snapshot.cpu,
        snapshot.memory_gib,
        snapshot.disk_gib,
        snapshot.gpu,
        snapshot.sandbox_class,
        snapshot.region_ids,
    )


def _references_json(references: list[ProfileReference]) -> list[dict[str, str]]:
    return [asdict(reference) for reference in references]


def build_report(
    snapshots: list[Snapshot],
    references: dict[str, list[ProfileReference]],
    source: str,
    generated_at: str | None = None,
) -> dict[str, Any]:
    snapshots_by_name: dict[str, list[Snapshot]] = defaultdict(list)
    for snapshot in snapshots:
        if snapshot.name:
            snapshots_by_name[snapshot.name].append(snapshot)
    name_collisions = [
        {
            "snapshot_name": name,
            "snapshot_ids": sorted(
                (snapshot.snapshot_id for snapshot in members),
                key=lambda value: value or "",
            ),
            "profile_references": _references_json(references.get(name, [])),
            "proof_required": "resolve duplicate immutable name to exactly one provider snapshot ID",
        }
        for name, members in sorted(snapshots_by_name.items())
        if len(members) > 1
    ]
    collision_names = {item["snapshot_name"] for item in name_collisions}

    groups: dict[tuple[object, ...], list[Snapshot]] = defaultdict(list)
    for snapshot in snapshots:
        key = _candidate_key(snapshot)
        if key is not None:
            groups[key].append(snapshot)

    duplicate_groups = [
        (key, members)
        for key, members in groups.items()
        if len({member.name for member in members if member.name}) > 1
    ]
    duplicate_groups.sort(key=lambda item: tuple(str(value) for value in item[0]))
    group_by_snapshot: dict[tuple[str | None, str | None], str] = {}
    candidates: list[dict[str, Any]] = []
    for index, (key, members) in enumerate(duplicate_groups, start=1):
        candidate_id = f"candidate-{index:03d}"
        members = sorted(members, key=lambda item: (item.name or "", item.snapshot_id or ""))
        member_references = [
            reference
            for member in members
            for reference in references.get(member.name or "", [])
        ]
        fleets = sorted({reference.fleet for reference in member_references})
        sizes = [member.size for member in members]
        reclaimable_size: int | float | None = None
        if sizes and all(size is not None and size == sizes[0] for size in sizes):
            reclaimable_size = sizes[0] * (len(sizes) - 1)  # type: ignore[operator]
        for member in members:
            group_by_snapshot[(member.snapshot_id, member.name)] = candidate_id
        image_name, cpu, memory, disk, gpu, sandbox_class, region_ids = key
        candidates.append({
            "candidate_id": candidate_id,
            "basis": {
                "image_name": image_name,
                "cpu": cpu,
                "memory_gib": memory,
                "disk_gib": disk,
                "gpu": gpu,
                "sandbox_class": sandbox_class,
                "region_ids": list(region_ids),
            },
            "snapshot_ids": [member.snapshot_id for member in members],
            "snapshot_names": [member.name for member in members],
            "profile_references": _references_json(sorted(
                member_references,
                key=lambda item: (item.fleet, item.size_class, item.profile),
            )),
            "all_active": all((member.state or "").lower() == "active" for member in members),
            "shared_candidate": len(fleets) > 1,
            "fleets": fleets,
            "potential_reclaimable_size": reclaimable_size,
            "proof_required": "confirm immutable image digest and runner contract before consolidation",
        })

    snapshot_rows: list[dict[str, Any]] = []
    present_names: set[str] = set()
    for snapshot in sorted(snapshots, key=lambda item: (item.name or "", item.snapshot_id or "")):
        if snapshot.name:
            present_names.add(snapshot.name)
        snapshot_references = references.get(snapshot.name or "", [])
        if snapshot.name in collision_names:
            reference_status = "ambiguous_name_collision"
        elif snapshot_references:
            reference_status = "referenced"
        else:
            reference_status = "unreferenced"
        snapshot_rows.append({
            "id": snapshot.snapshot_id,
            "name": snapshot.name,
            "state": snapshot.state,
            "size": snapshot.size,
            "created_at": snapshot.created_at,
            "image_name": snapshot.image_name,
            "snapshot_ref": snapshot.snapshot_ref,
            "resources": {
                "cpu": snapshot.cpu,
                "memory_gib": snapshot.memory_gib,
                "disk_gib": snapshot.disk_gib,
                "gpu": snapshot.gpu,
            },
            "sandbox_class": snapshot.sandbox_class,
            "region_ids": list(snapshot.region_ids),
            "profile_references": _references_json(snapshot_references),
            "reference_status": reference_status,
            "duplicate_candidate": group_by_snapshot.get((snapshot.snapshot_id, snapshot.name)),
        })

    unresolved = [
        {
            "snapshot_name": name,
            "profile_references": _references_json(snapshot_references),
        }
        for name, snapshot_references in sorted(references.items())
        if name not in present_names
    ]
    unreferenced = [
        {"id": snapshot.snapshot_id, "name": snapshot.name}
        for snapshot in snapshots
        if not references.get(snapshot.name or "")
    ]
    timestamp = generated_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "schema_version": 1,
        "generated_at": timestamp,
        "source": source,
        "summary": {
            "snapshot_count": len(snapshots),
            "referenced_snapshot_count": sum(
                bool(references.get(item.name or "")) and item.name not in collision_names
                for item in snapshots
            ),
            "ambiguous_snapshot_count": sum(item.name in collision_names for item in snapshots),
            "unreferenced_snapshot_count": len(unreferenced),
            "unresolved_profile_reference_count": len(unresolved),
            "name_collision_count": len(name_collisions),
            "duplicate_candidate_count": len(candidates),
            "shared_candidate_count": sum(bool(item["shared_candidate"]) for item in candidates),
        },
        "snapshots": snapshot_rows,
        "duplicate_candidates": candidates,
        "name_collisions": name_collisions,
        "unresolved_profile_references": unresolved,
        "unreferenced_snapshots": unreferenced,
        "destructive_actions": [],
    }


def load_json(path: Path) -> object:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise AuditError(f"could not read snapshot input {path}: {exc}") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, help="offline Daytona snapshot-list JSON response")
    parser.add_argument("--root", type=Path, default=repository_root(), help=argparse.SUPPRESS)
    parser.add_argument("--compact", action="store_true", help="emit compact JSON")
    parser.add_argument("--timeout", type=float, default=20.0, help="live request timeout in seconds")
    args = parser.parse_args(argv)
    try:
        references, api_base = load_profile_references(args.root.resolve())
        if args.input:
            snapshots = snapshots_from_payload(load_json(args.input))
            source = "offline-json"
        else:
            api_key = os.environ.get("DAYTONA_API_KEY")
            if not api_key:
                raise AuditError(
                    "DAYTONA_API_KEY is missing; provide it through your secret manager or use --input"
                )
            if "://" in api_key:
                raise AuditError(
                    "DAYTONA_API_KEY is unresolved; resolve it through your secret manager or use --input"
                )
            snapshots = fetch_snapshots(api_base, api_key, args.timeout)
            source = "daytona-api"
        report = build_report(snapshots, references, source)
        json.dump(report, sys.stdout, indent=None if args.compact else 2, sort_keys=True)
        sys.stdout.write("\n")
        return 0
    except AuditError as exc:
        print(f"snapshot audit error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
