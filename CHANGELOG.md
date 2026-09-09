# Changelog

All notable changes to Marsh are documented in this file.

The format is based on Keep a Changelog, and this project uses Calendar
Versioning: `YYYY.M.PATCH`.

## [Unreleased]

Runner-boundary hardening from OPP-524 (Quarry-IO/quarry). Independently
implementable now; the remaining OPP-524 scope (pre-token BuildKit source-cache
boundary, fd-sealed JIT handoff, exact-host JIT broker) is gated on OPP-525 and
on an explicit Marsh deployment-authority grant -- see that issue.

### Changed

- `[lifecycle].job_max_secs` is now a hard 7200s ceiling, enforced fail-closed
  both at fleet-profile validation (`scripts/fleet_config.py`) and at
  orchestrator startup (`orchestrator/orchestrator.py`), not merely a default.
  A profile requesting more no longer silently runs longer; it refuses to
  start/validate.
- The runner image no longer widens `/var/run/docker.sock` to world-writable
  `0666`. `daytona`'s docker-group membership already grants it access at
  dockerd's own default `0660` mode.
- Replaced the runner image's inherited blanket passwordless sudo with an
  exact-command NOPASSWD rule scoped to one fixed root-owned launcher script
  (`runner-image/marsh-start-dockerd.sh`), the only thing `daytona` may run as
  root. Docker-group/DinD access itself is unchanged (still load-bearing for
  build jobs; narrowing that is separate, larger scope tracked under OPP-524/
  OPP-525).

## [2026.7.3] - 2026-07-17

Pickup latency, debug, and observability improvements for self-hosted runners.
GitHub remains the scheduler; Marsh stays a thin reconciler on Daytona.

### Added

- GitHub `workflow_job` webhook fast path: optional `[webhook]` listener verifies
  `X-Hub-Signature-256` and wakes the poller when a matching job is queued
  (poller remains reconciliation truth). See [docs/ORCHESTRATOR.md](docs/ORCHESTRATOR.md).
- Adaptive cycle polling: fast idle poll window after runner online, then settle
  (`fast_idle_poll_secs` / `idle_poll_secs` / `busy_poll_secs`).
- `hold_on_failure_secs`: keep the Daytona sandbox briefly after a failed job for
  operator SSH debug before teardown.
- Stage-level cycle telemetry (`jit_mint_secs`, `sandbox_create_secs`,
  `runner_start_secs`, `teardown_secs`) and usage-report stage p95 when coverage
  is complete.
- Adopter documentation for orchestrator control loop, webhooks, and telemetry.

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
  dedup and recovery notices.
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
