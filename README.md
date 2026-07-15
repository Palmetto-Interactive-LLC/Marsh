# Marsh

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

Marsh is an open-source runner platform for GitHub Actions. It keeps GitHub as
the scheduler, then creates short-lived self-hosted runners inside Daytona
sandboxes when queued jobs need capacity.

The design is intentionally small:

- GitHub Actions remains the queue, dispatcher, and source of truth.
- A stateless orchestrator polls queued workflow jobs and mints GitHub JIT runner
  configs.
- Daytona creates a fresh sandbox from a prebuilt runner snapshot.
- The runner executes exactly one job, exits, and the sandbox is deleted.
- Cache data is restored and saved through job hooks so runner sandboxes stay
  disposable.

Marsh is MIT licensed. It is built for teams that want elastic self-hosted
runner labels without owning a long-lived runner fleet.

## Architecture

```text
pull request / push
        |
        v
GitHub Actions queue
        |
        | queued jobs, runner labels
        v
marsh orchestrator
        |
        | GitHub App: generate JIT runner config
        | Daytona API: create sandbox from snapshot
        v
Daytona sandbox
        |
        | actions/runner --jitconfig ... --ephemeral
        v
one workflow job, then teardown
```

The default example runner labels are:

```yaml
runs-on: [self-hosted, daytona, marsh]
```

Use the large class for heavier jobs:

```yaml
runs-on: [self-hosted, daytona, marsh, large]
```

## Why Marsh

Self-hosted runner systems usually fail in one of three ways: stale runners,
dirty workspaces, or idle capacity that costs money while doing nothing. Marsh
pushes those problems to the edges:

| Concern | Marsh approach |
| --- | --- |
| Scheduling | GitHub owns the queue; Marsh only provisions capacity. |
| Isolation | Every job starts in a fresh Daytona sandbox. |
| Runner lifecycle | JIT + ephemeral runner; one job per sandbox. |
| Scaling | Poll queued demand; enable a measured warm floor only when pickup latency requires it. |
| Caching | Bake stable tools into the snapshot; store mutable caches as tarballs. |
| Failure cleanup | Auto-stop, registry-aware orphan sweep, and startup reap. |
| Multi-tenancy | Sandboxes are labeled by org; sweeps never touch another org's runners. |

## Repository Layout

```text
orchestrator/              Python demand reconciler + fleet watchdog
runner-image/              universal GitHub Actions runner image
runner-image/hooks/        cache restore/save hooks for job start and completion
config/runners.toml        example runner classes and lifecycle settings
config/fleets/             credential-free example fleet profiles
infra/snapshots/           GHCR registry setup and snapshot registration scripts
infra/terraform/           runner group, cache volume, and registry IaC
scripts/                   verification and migration helpers
docs/                      security, release, setup, and public-release notes
```

## Quick Start

Marsh needs three external systems:

1. A GitHub App with administration access to the intended GitHub Actions
   runners. Organization fleets use a selected runner group; personal-account
   fleets use explicit repository-scoped JIT registrations.
2. A Daytona account with API access.
3. A container registry, typically GHCR, that Daytona can pull the runner image
   from.

Create a runner group, registry connection, cache volumes, and snapshots with
Terraform:

```bash
cd infra/terraform
terraform init

export TF_VAR_github_token="<github app or PAT with runner-group admin scope>"
export TF_VAR_daytona_api_key="<daytona api key>"
export TF_VAR_ghcr_user="<github user or bot>"
export TF_VAR_ghcr_token="<ghcr token with packages read access>"

terraform plan
terraform apply
```

Build and register a runner image:

```bash
export DAYTONA_API_KEY="<daytona api key>"
export GHCR_USER="<github user or bot>"
export GHCR_TOKEN="<ghcr token with packages write/read access>"
export GHCR_IMAGE_OWNER="your-github-org-or-user"

infra/snapshots/setup-registry.sh
infra/snapshots/register-snapshot.sh v1
```

Run the orchestrator through your own reviewed deployment system. For a
production installation, keep activated fleet profiles, host inventory,
network policy, and deployment procedures in a private operations source.
This repository documents the public contract and uses synthetic examples; it
must not contain a live fleet roster, host identity, private route, or
provider-specific production endpoint. For production, inject secrets through
your own secret manager. Do not commit tokens, private keys, `.env` files, or
generated Terraform state.

## Configuration

The orchestrator reads `config/runners.toml`. The important fields are:

- `[github].scope`: `organization` (the default) or `repository`.
- Organization scope: `[github].org` and selected `runner_group` used for JIT
  registrations.
- Repository scope: `[github].owner`, an explicit private `repositories`
  allowlist, and `runner_group_id` used by each repository JIT request.
- `[cache].volume`: Daytona volume mounted at `/cache`.
- `[[size_class]]`: labels, snapshot name, resources, warm floor, and burst cap.
- `[lifecycle]`: job deadline, idle refresh, demand idle timeout, and orphan
  safety settings.

Daytona fixes CPU, memory, and disk size at snapshot registration time. Marsh
therefore uses one image registered as multiple pre-sized snapshots.
Inventory, shared-candidate review, immutable naming, rollback, and retirement
gates are defined in [docs/SNAPSHOT-GOVERNANCE.md](docs/SNAPSHOT-GOVERNANCE.md).
Production profiles default to `min_idle = 0`. Any future nonzero warm floor
must include `warm_floor_reason` so the latency/cost exception is explicit and
reviewable.

### Multi-Instance / Single Daytona Account

Running one orchestrator instance per GitHub organization or personal account
against a single shared Daytona account is safe. Organization sandboxes retain
their existing `org` + size-class labels. Repository-scope sandboxes also carry
an exact fleet label and repository name, so a controller can reap only its
own profile's sandboxes. A repository JIT runner is never counted as supply
for another repository. Sandboxes that predate this labeling (or were created
out-of-band) are silently skipped by scoped sweeps; drain those manually once
after upgrading a pre-existing deployment.

`config/fleets/` contains credential-free examples for reviewing the profile
shape. Keep the activated fleet inventory, repository roster, runner-group
identifiers, host mapping, and secret-manager bindings in a private operations
source. A public release must remain reproducible from the generic examples
without disclosing a deployment's trust boundary.

## Fleet Watchdog

The orchestrator is silent when healthy, which means a dead orchestrator host
is silent too. `orchestrator/watchdog.py` is a timer-driven sidecar for the
same host that alerts when the fleet stops working:

- the orchestrator process is no longer active;
- a queued job carrying this fleet's labels has waited too long — including
  the invisible failure where a repository was never added to the runner
  group, so its jobs can never be picked up (the alert names the repo);
- every active organization fleet's App installation covers all repositories
  and its private repository roster still has runner-group access, even when a
  rate-sensitive fleet opts out of the watchdog's queued-job scan;
- org-labeled sandboxes exceed a cap or look orphaned.

Alerts POST to any webhook (ntfy-style or JSON), deduplicate against a state
file so persistent failures re-page on a configurable cadence instead of every
tick, and a recovery notice is sent when a finding clears. An optional
heartbeat URL is fetched on every all-clear pass as a dead-man's switch — if
the watchdog or the whole host dies, the missing heartbeat is the page. A
second subcommand, `usage-report`, posts per-snapshot cycle counts, job/idle
outcomes, average and p95 durations, and declared allocation-hours from the
orchestrator journals. The resource totals match reserved CPU/RAM/disk
capacity only when every cycle has a confirmed cleanup boundary; incomplete
windows are labeled with coverage instead of emitting partial totals. These
are controller observations, not measured utilization or provider billing.
Use `usage-report --since=-72h` for an ad hoc multi-day sample.

Configure the watchdog through the same private operations source that deploys
the orchestrator. Keep its active instance list, alert destination, and host
configuration private; public examples should describe only the schema and
non-secret behavior.

## Cache Model

Daytona volumes are S3-backed FUSE mounts. Whole-file writes work, but append and
rename are not POSIX-equivalent. Marsh avoids mounting build-tool cache
directories directly on the volume.

Instead:

- stable tools live in the runner snapshot;
- job hooks restore cache tarballs from the volume to local disk at job start;
- job hooks save tarballs back to the volume at job completion;
- Docker layer cache should use a registry-backed cache, not the FUSE volume.

This keeps the runner filesystem normal while preserving cache reuse across
ephemeral sandboxes.

## Security Model

Marsh is designed around short-lived credentials and short-lived compute:

- GitHub runner registration uses JIT config.
- Runner sandboxes are ephemeral and execute one job.
- The runner image must not contain GitHub, Daytona, model, cloud, or registry
  credentials.
- Secrets are injected at runtime by the deployment environment.
- The repo's own public CI should run on GitHub-hosted runners; use Marsh labels
  in your downstream repositories after you have configured trust boundaries.
- Public repositories should be allowed onto a Marsh runner group only after you
  have reviewed the workflows that can run on that group.

See [docs/SECURITY-MODEL.md](docs/SECURITY-MODEL.md) and
[SECURITY.md](SECURITY.md).

## Development

Run the local verification gate:

```bash
make verify
```

Run the release-readiness scans before making the repository public or cutting a
release:

```bash
gitleaks detect --no-git --redact --source .
make verify
```

The scanner output should be attached to the release notes or public-release
review record. See [docs/PUBLIC_RELEASE_REVIEW.md](docs/PUBLIC_RELEASE_REVIEW.md).

## Public Release Boundary

Marsh is a reusable open-source runner platform. Public releases include the
orchestrator, runner image, Terraform examples, deployment-neutral
configuration, verification gate, documentation, and MIT license. They do not
include activated fleet profiles, downstream repository rosters, private
network routes, host inventory, or production deployment records.

Before publishing, run the release checks in [RELEASING.md](RELEASING.md) and
the current-tree review in [docs/PUBLIC_RELEASE_REVIEW.md](docs/PUBLIC_RELEASE_REVIEW.md).

## License

MIT. See [LICENSE](LICENSE).
