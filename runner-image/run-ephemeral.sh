#!/usr/bin/env bash
# Exec'd by the orchestrator inside a fresh Daytona sandbox. Runs the GitHub Actions
# runner for EXACTLY ONE job (JIT + ephemeral) and exits; the orchestrator deletes
# the sandbox afterward.
#
# Caching model (important): the Daytona cache Volume is mounted at $CACHE_VOL and
# is mountpoint-s3 — whole-file writes only, NO append/rename. So build caches point
# at LOCAL POSIX dirs, and GitHub Actions runner job-hooks tar them to/from the
# volume around each job (see hooks/cache-restore.sh + cache-save.sh). Transparent —
# no workflow edits.
#
# Required env (injected by the orchestrator via Daytona Secrets):
#   RUNNER_JITCONFIG   base64 JIT config from /orgs/{org}/actions/runners/generate-jitconfig
# Optional:
#   CACHE_VOL          default /cache   (the mounted S3-FUSE cache volume)
set -euo pipefail

export CACHE_VOL="${CACHE_VOL:-/cache}"
export CACHE_LOCAL="${CACHE_LOCAL:-/home/daytona/.marsh-cache}"

# Local POSIX cache dirs (restored/saved to the volume by the hooks).
export CARGO_HOME="${CACHE_LOCAL}/cargo"
export SCCACHE_DIR="${CACHE_LOCAL}/sccache"
export RUSTC_WRAPPER="sccache"
export GOMODCACHE="${CACHE_LOCAL}/go/mod"
export GOCACHE="${CACHE_LOCAL}/go/build"
export npm_config_cache="${CACHE_LOCAL}/npm"
export PIP_CACHE_DIR="${CACHE_LOCAL}/pip"
mkdir -p "$CARGO_HOME" "$SCCACHE_DIR" "$GOMODCACHE" "$GOCACHE" "$npm_config_cache" "$PIP_CACHE_DIR"

# Docker layer cache: use a registry (Harbor/GHCR) via --cache-to/--cache-from
# type=registry in the workflow — NOT the S3-FUSE volume. Baked buildx is ready.
export DOCKER_BUILDKIT=1

# Transparent per-job cache restore/save via runner hooks.
export ACTIONS_RUNNER_HOOK_JOB_STARTED=/opt/hooks/cache-restore.sh
export ACTIONS_RUNNER_HOOK_JOB_COMPLETED=/opt/hooks/cache-save.sh

export RUNNER_TOOL_CACHE="${RUNNER_TOOL_CACHE:-/opt/hostedtoolcache}"

if [[ -z "${RUNNER_JITCONFIG:-}" ]]; then
  echo "run-ephemeral: RUNNER_JITCONFIG is required" >&2
  exit 2
fi

# Start the Docker daemon (best-effort, non-fatal) so `docker build`/buildx and
# container-based actions work. Daytona sandboxes allow DinD; daytona has passwordless
# sudo and is in the docker group. A job that doesn't use docker is unaffected.
if command -v dockerd >/dev/null 2>&1 && [[ ! -S /var/run/docker.sock ]]; then
  sudo -n sh -c 'nohup dockerd >/var/log/dockerd.log 2>&1 &' 2>/dev/null || true
  for _ in $(seq 1 20); do
    if [[ -S /var/run/docker.sock ]]; then break; fi
    sleep 1
  done
  sudo -n chmod 666 /var/run/docker.sock 2>/dev/null || true
fi

# run.sh ships without the +x bit in the runner tarball; invoke via bash (absolute path).
cd /opt/actions-runner
exec bash /opt/actions-runner/run.sh --jitconfig "${RUNNER_JITCONFIG}"
