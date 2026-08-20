# GPU runners rollout

**DRAFT — not deployed.** This document proposes the build, registration, and
fleet-profile steps to bring up a GPU size class. Nothing in it has been run:
no image has been built or pushed, no snapshot has been registered, no
Daytona API call has been made, and the fleet-profile block below has not
been committed anywhere. Every command is written so a human (or an agent
with the real credentials) can execute it directly.

## 1. Build and push the GPU runner image

Image: `runner-image/Dockerfile.gpu` (sibling of `runner-image/Dockerfile`;
see that file's header for how it preserves the runner contract on top of a
CUDA 12 base). This mirrors the login/build/push sequence in
`infra/snapshots/register-snapshot.sh`, split out here because that script
drives the two general-purpose (non-GPU) snapshots.

Prerequisite: `infra/snapshots/setup-registry.sh` has already connected GHCR
to Daytona for pulls (shared across all Marsh runner images; do not re-run
unless the connection is missing).

Secrets (env; never printed): `GHCR_USER`, `GHCR_TOKEN` (needs `write:packages`
to push).

```bash
: "${GHCR_USER:?}"; : "${GHCR_TOKEN:?}"

echo "==> docker login ghcr.io (token via stdin, never echoed)"
printf '%s' "$GHCR_TOKEN" | docker login ghcr.io -u "$GHCR_USER" --password-stdin

echo "==> build + push ghcr.io/palmetto-interactive-llc/marsh-runner-gpu:v1 (linux/amd64)"
docker buildx build --platform linux/amd64 \
  -t ghcr.io/palmetto-interactive-llc/marsh-runner-gpu:v1 \
  -f runner-image/Dockerfile.gpu . --push
```

Optional, same as the base runner build: pass
`--secret id=ghtoken,src=<token-file>` on the `buildx build` line to
authenticate the `seed-toolcache.sh` python-build-standalone API lookup
during the build (avoids unauthenticated GitHub API rate limits on the build
host). Never pass the token as a `--build-arg` or otherwise bake it into a
layer.

## 2. Register the GPU snapshot

`infra/snapshots/register-gpu-snapshot.sh` registers a new **immutable**
snapshot name — it refuses to replace an existing one (see
`docs/SNAPSHOT-GOVERNANCE.md`). GPU units, like CPU/memory/disk, are fixed at
registration.

Secret (env; never printed): `DAYTONA_API_KEY`.

```bash
: "${DAYTONA_API_KEY:?}"
infra/snapshots/register-gpu-snapshot.sh marsh-runner-gpu-v1 \
  ghcr.io/palmetto-interactive-llc/marsh-runner-gpu:v1 1 8 32 50
```

That is `<snapshot-name> <image-ref> [gpu] [cpu] [mem_gib] [disk_gib]` = 1 GPU
/ 8 vCPU / 32 GiB / 50 GiB disk, matching the `[[size_class]]` block below.

## 3. Fleet-profile `[[size_class]]` block (palmetto, private Marsh-ops repo)

This repo only holds the public, non-activated example profile
(`config/fleets/example/runners.toml`), which already carries this exact
block commented out as a template. The activated version — with the real
snapshot name — belongs in the **private** Marsh-ops repo's palmetto fleet
profile, alongside its existing `default` and `large` classes:

```toml
[[size_class]]
name = "gpu"
labels = ["self-hosted", "daytona", "gpu"]
snapshot = "marsh-runner-gpu-v1"
cpu = 8
memory_gib = 32
disk_gib = 50
gpu = 1
spot = true
min_idle = 0
max = 2
```

`min_idle = 0` is mandatory whenever `spot = true`: preemptible GPU capacity
can be reclaimed by Daytona without notice, so it cannot be used to hold a
warm floor of idle sandboxes.

### Verified against `scripts/fleet_config.py`

This block satisfies every rule the validator applies to a `[[size_class]]`
table (checked against `validate_profile` in `scripts/fleet_config.py`):

- `labels` includes `"self-hosted"` (required unconditionally) and
  `"daytona"` (required because this profile has no restrictive `[network]`/
  `[routing]` — palmetto is org-scoped).
- `snapshot` is a non-empty string.
- `min_idle` (0) and `max` (2) are non-negative integers with
  `min_idle <= max`.
- `warm_floor_reason` is correctly **omitted**: the validator requires it
  only when `min_idle > 0`, and forbids it when `min_idle == 0`.
- No key in the block matches the forbidden credential-like key pattern
  (`api_key|token|secret|password|private`) that the validator scans for
  recursively.
- The profile-level requirement that `default` and `large` size classes exist
  is unaffected — this is an additional class, not a replacement.
- `cpu`, `memory_gib`, `disk_gib`, `gpu`, and `spot` are not restricted by an
  explicit key allowlist on `[[size_class]]` in the validator, and `cpu`/
  `memory_gib`/`disk_gib`/`gpu` are the exact field names `orchestrator.py`
  reads off a size class (see `orchestrator.py:1285`); `spot` is the exact
  field name `orchestrator.py` forwards to the Daytona create call only when
  a class opts in (`orchestrator.py:825-827`).

No changes to the block were needed to pass; it is copied from the existing
commented-out template in `config/fleets/example/runners.toml` with the
placeholder snapshot name replaced by the real registered one.

## Prerequisites and open items

- **UNVERIFIED: Daytona GPU capacity/quota for this account.** Nothing in
  this draft calls the Daytona API to confirm GPU nodes are available or
  quota is provisioned. Confirm with Daytona (support or dashboard) before
  registering the snapshot for real.
- **Spot semantics:** GPU sandboxes here are requested as spot (preemptible)
  capacity. Daytona may terminate one without notice to reclaim the GPU for
  on-demand use. Only route jobs to the `gpu` label that are safely retryable
  (idempotent, checkpointed, or cheap to rerun) — a job with no retry story
  should not carry this label.
- **UNVERIFIED: exact UID/GID of the `daytona` user inside `daytonaio/sandbox:0.8.0`.**
  `Dockerfile.gpu` creates the user fresh (`useradd -m`) since the CUDA base
  doesn't ship it; the assigned UID may not match the base image used by
  `runner-image/Dockerfile`. Only matters if something depends on a stable
  UID across runner images (e.g. cache-volume file ownership) — worth
  confirming before this goes live if that turns out to matter.
- **UNVERIFIED: whether GPU CI jobs need `nvcc`/full CUDA toolchain.** The
  image uses the `-cudnn-runtime-` base, not `-devel`. If a workflow compiles
  CUDA kernels from source rather than installing prebuilt wheels, this base
  needs to change.
- **Not reproduced from `daytonaio/sandbox`:** `claude-code` is not baked
  into `Dockerfile.gpu`. Add it only if a GPU workflow actually needs it.
