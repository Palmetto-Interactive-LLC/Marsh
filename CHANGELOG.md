# Changelog

All notable changes to Marsh are documented in this file.

The format is based on Keep a Changelog, and this project uses Calendar
Versioning: `YYYY.M.PATCH`.

## [2026.7.2] - 2026-07-14

Public-release hygiene documentation update. A release is not ready until the
current-tree boundary review has evidence for the exact tag commit.

### Changed

- Added public deployment, setup, security, and release guidance for synthetic
  fleet examples and an explicit private-operations boundary.
- Documented signed, annotated CalVer source-release tags without a `v` prefix.

### Security

- Added review criteria that reject activated fleet profiles, repository
  rosters, host inventory, and private-network topology alongside credentials
  and local artifacts.

## [2026.7.1] - 2026-07-12

Reliability and operability release: multi-instance safety fixes for the
reconciler plus a fleet watchdog so a dead control-plane host can no longer fail
silently.

### Added

- Fleet watchdog (`orchestrator/watchdog.py`) with two timer-driven subcommands:
  `check` (orchestrator units active; queued jobs with fleet labels waiting too
  long, including the case where a repository was never added to the runner
  group; sandbox count/age sanity) and `usage-report` (daily spawn counts per
  size class). Alerts POST to any webhook (ntfy headers or JSON) with state-file
  dedup, recovery notices, and an optional heartbeat dead-man's switch.
- Watchdog configuration examples and lifecycle guidance.

### Fixed

- `reap()` and `orphan_sweep()` now require a sandbox's `org` label to match the
  orchestrator's own org before deleting it, so one org's instance can no longer
  tear down a sibling org's runners in a single-account, multi-instance
  deployment.
- `reap()` and `orphan_sweep()` deregister only runner registrations named
  `marsh-*`, so non-Marsh self-hosted runners that intentionally share the
  runner labels are never removed.

## [2026.7.0] - 2026-07-05

Initial public release.

### Added

- Marsh README with architecture, quick start, cache model, security model, and
  release status.
- MIT license.
- Daytona-backed GitHub Actions runner orchestrator.
- Universal runner image with GitHub Actions runner, common build toolchains,
  Docker/buildx support, and cache hooks.
- Terraform examples for runner group, Daytona cache volumes, registry setup,
  and snapshot registration.
- Reusable orchestrator configuration and verification guidance.
- Public release documentation, release checklist, security policy,
  contributing guide, and code of conduct.

### Changed

- Rebranded the Daytona runner repository as Marsh.
- Made default examples generic rather than deployment-specific.
- Moved this repository's own CI/security workflows to GitHub-hosted runners so
  public pull requests do not depend on private self-hosted runner access.
