#!/usr/bin/env python3
"""Verify that one host-rendered Marsh fleet can safely start.

Organization profiles retain the selected ``daytona`` runner-group check.
Repository profiles are for personal accounts: every named private repository
must be visible to the App installation and expose the configured repository
runner group. The preflight never mints a JIT runner, so activation has no
temporary registrations or sandboxes.
"""
from __future__ import annotations

import argparse
import json
import sys
import tomllib
import urllib.error
import urllib.request
from pathlib import Path


DAYTONA_API_BASE = "https://app.daytona.io/api"


class RejectRedirects(urllib.request.HTTPRedirectHandler):
    """Never forward the Daytona bearer credential through a redirect."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        return None


def open_without_redirects(request: urllib.request.Request, timeout: float):
    return urllib.request.build_opener(RejectRedirects()).open(request, timeout=timeout)


def load_environment(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return values


def daytona_target_preflight(
    environment: dict[str, str],
    config: dict,
    opener=open_without_redirects,
) -> str:
    """Fail closed unless the profile's exact Daytona target is available.

    The Daytona API key is read from the rendered host environment and is only
    attached to the fixed Daytona API origin.  Responses and provider errors
    are never emitted, so neither credentials nor provider response details
    can appear in deployment logs.
    """
    daytona_config = config.get("daytona")
    if not isinstance(daytona_config, dict):
        raise RuntimeError("fleet profile is missing [daytona]")
    target = daytona_config.get("target")
    if not isinstance(target, str) or not target:
        raise RuntimeError("fleet profile is missing [daytona].target")
    api_key = environment.get("DAYTONA_API_KEY")
    if not api_key or "://" in api_key:
        raise RuntimeError("rendered environment is missing a resolved DAYTONA_API_KEY")

    request = urllib.request.Request(
        f"{DAYTONA_API_BASE}/regions",
        headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
        method="GET",
    )
    try:
        with opener(request, timeout=15) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        status = exc.code
        exc.close()
        raise RuntimeError(f"Daytona target inventory request failed with HTTP {status}") from None
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        raise RuntimeError("Daytona target inventory request failed") from None

    regions = payload.get("items") if isinstance(payload, dict) else payload
    if not isinstance(regions, list):
        raise RuntimeError("Daytona target inventory returned an unexpected response shape")
    if not any(
        isinstance(region, dict)
        and (region.get("id") == target or region.get("name") == target)
        for region in regions
    ):
        raise RuntimeError(f"configured Daytona target {target!r} is not available to this API key")
    return f"preflight passed: Daytona target {target!r} is available"


def repositories_visible_to_app(client: object, owner: str) -> dict[str, tuple[str, bool]]:
    """Return repository names and privacy from the configured App's scope.

    Organization callers first verify that the App installation covers all
    repositories, making this the complete organization roster. They then
    compare it with a selected runner group without printing credentials or
    mutating GitHub.
    """
    visible: dict[str, tuple[str, bool]] = {}
    for item in client.installation_repositories():  # type: ignore[attr-defined]
        if (not isinstance(item, dict) or not isinstance(item.get("private"), bool)
                or not isinstance(item.get("name"), str)
                or not isinstance(item.get("owner"), dict)
                or item["owner"].get("login", "").lower() != owner.lower()):
            continue
        name = item["name"]
        visible[name.lower()] = (name, item.get("private") is True)
    return visible


def organization_preflight(github: object, config: dict, client_type: type) -> str:
    github_config = config.get("github")
    if not isinstance(github_config, dict):
        raise RuntimeError("fleet profile is missing [github]")
    organization = github_config.get("org")
    group_name = github_config.get("runner_group")
    if not isinstance(organization, str) or group_name != "daytona":
        raise RuntimeError("organization profile must declare org and runner_group=daytona")
    client = client_type(organization, github["GH_APP_ID"], github["GH_APP_INSTALLATION_ID"], github["GH_APP_KEY_PATH"])
    groups = client._api("GET", f"/orgs/{organization}/actions/runner-groups").get("runner_groups", [])
    group = next((item for item in groups if item.get("name") == group_name), None)
    if (not isinstance(group, dict) or group.get("visibility") != "selected"
            or group.get("allows_public_repositories") is not False):
        raise RuntimeError("required Daytona runner group is absent, not selected-repository scoped, or allows public repositories")
    repositories = client.runner_group_repos(int(group["id"]))
    client.require_organization_installation_all_repositories()  # type: ignore[attr-defined]
    if not repositories:
        raise RuntimeError("Daytona runner group has no selected repositories")
    selected = {repository.lower(): repository for repository in repositories if isinstance(repository, str)}
    visible = repositories_visible_to_app(client, organization)
    selected_not_visible = sorted(name for key, name in selected.items() if key not in visible)
    if selected_not_visible:
        raise RuntimeError(
            "Daytona runner group selects repository/repositories not visible to the Marsh App: "
            + ", ".join(selected_not_visible[:10])
        )
    visible_private = {key: name for key, (name, private) in visible.items() if private}
    missing = sorted(name for key, name in visible_private.items() if key not in selected)
    if missing:
        raise RuntimeError(
            "Daytona runner group does not cover every private organization repository: "
            + ", ".join(missing[:10])
        )
    return (
        f"preflight passed: {organization} runner group {group_name!r} covers all "
        f"{len(visible_private)} private organization repository/repositories"
    )


def repository_preflight(github: object, config: dict, client_type: type) -> str:
    github_config = config.get("github")
    if not isinstance(github_config, dict):
        raise RuntimeError("fleet profile is missing [github]")
    owner = github_config.get("owner")
    repositories = github_config.get("repositories")
    runner_group_id = github_config.get("runner_group_id")
    runner_group_name = github_config.get("runner_group")
    if not isinstance(owner, str) or not isinstance(repositories, list) or not repositories:
        raise RuntimeError("repository profile must declare owner and non-empty repositories")
    if (runner_group_id is None) == (runner_group_name is None):
        raise RuntimeError("repository profile must declare exactly one of runner_group_id or runner_group")
    if runner_group_id is not None and (not isinstance(runner_group_id, int) or runner_group_id < 1):
        raise RuntimeError("repository profile runner_group_id must be a positive integer")
    if runner_group_name is not None and (not isinstance(runner_group_name, str) or not runner_group_name):
        raise RuntimeError("repository profile runner_group must be a non-empty string")
    if not all(isinstance(repository, str) and repository for repository in repositories):
        raise RuntimeError("repository profile contains an invalid repository name")

    client = client_type(owner, github["GH_APP_ID"], github["GH_APP_INSTALLATION_ID"], github["GH_APP_KEY_PATH"],
                         scope="repository", repositories=repositories)
    visible = {
        item["name"].lower(): item
        for item in client.installation_repositories()
        if isinstance(item.get("name"), str)
        and isinstance(item.get("owner"), dict)
        and item["owner"].get("login", "").lower() == owner.lower()
    }
    resolved_group_ids: set[int] = set()
    for repository in repositories:
        installation_repo = visible.get(repository.lower())
        if not installation_repo:
            raise RuntimeError(f"configured private repository {owner}/{repository} is not visible to this App installation")
        if installation_repo.get("private") is not True:
            raise RuntimeError(f"configured repository {owner}/{repository} is not private")
        groups = client.repository_runner_groups(repository)
        if runner_group_name is not None:
            matching = [item for item in groups if item.get("name") == runner_group_name and isinstance(item.get("id"), int)]
            if len(matching) != 1:
                raise RuntimeError(
                    f"configured runner_group {runner_group_name!r} is unavailable to {owner}/{repository}"
                )
            resolved_group_ids.add(int(matching[0]["id"]))
        elif not any(item.get("id") == runner_group_id for item in groups):
            raise RuntimeError(
                f"configured runner_group_id {runner_group_id} is unavailable to {owner}/{repository}"
            )
    if runner_group_name is not None and len(resolved_group_ids) != 1:
        raise RuntimeError(f"configured runner_group {runner_group_name!r} has inconsistent IDs across repositories")
    group_description = repr(runner_group_name) if runner_group_name is not None else str(runner_group_id)
    return (
        f"preflight passed: {owner} repository scope has {len(repositories)} private repository/repositories "
        f"with runner group {group_description}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--env-file", required=True, type=Path)
    args = parser.parse_args()

    sys.path.insert(0, str(args.source_root / "orchestrator"))
    from orchestrator import GitHub  # noqa: PLC0415

    try:
        with args.config.open("rb") as handle:
            config = tomllib.load(handle)
        environment = load_environment(args.env_file)
        for required in ("GH_APP_ID", "GH_APP_INSTALLATION_ID", "GH_APP_KEY_PATH"):
            if not environment.get(required):
                raise RuntimeError(f"rendered environment is missing {required}")
        github_config = config.get("github")
        if not isinstance(github_config, dict):
            raise RuntimeError("fleet profile is missing [github]")
        scope = github_config.get("scope", "organization")
        if scope == "organization":
            result = organization_preflight(environment, config, GitHub)
        elif scope == "repository":
            result = repository_preflight(environment, config, GitHub)
        else:
            raise RuntimeError(f"unsupported GitHub scope {scope!r}")
        target_result = daytona_target_preflight(environment, config)
    except Exception as exc:  # noqa: BLE001 - all failures must stop activation
        print(f"fleet preflight failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    print(result)
    print(target_result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
