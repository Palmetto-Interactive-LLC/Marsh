#!/usr/bin/env bash
# ACTIONS_RUNNER_HOOK_JOB_COMPLETED — runs after every job. Tars the LOCAL POSIX
# cache dirs and writes them as whole-file objects to the Daytona Volume
# (mountpoint-s3 supports whole-file writes; append/rename it does not). Idempotent
# rolling cache per repo; keep it simple (latest wins). Never fail the job.
set -uo pipefail
CACHE_VOL="${CACHE_VOL:-/cache}"
LOCAL="${CACHE_LOCAL:-/home/daytona/.marsh-cache}"
repo="${GITHUB_REPOSITORY:-unknown}"; repo="${repo//\//_}"
base="${CACHE_VOL}/${repo}"
mkdir -p "$base" 2>/dev/null || true

save() { # <name> <local-dir>
  local name="$1" dir="$2" tmp="/tmp/${1}.tgz" tgz="${base}/${1}.tgz"
  [ -d "$dir" ] || { echo "cache-save: skip ${name} (no dir)"; return 0; }
  # tar to local tmp first (POSIX), then copy the whole file onto the S3 volume.
  if tar -czf "$tmp" -C "$dir" . 2>/dev/null; then
    cp -f "$tmp" "$tgz" 2>/dev/null && echo "cache-save: saved ${name} ($(du -h "$tmp" | cut -f1))" \
      || echo "cache-save: write failed ${name}"
    rm -f "$tmp"
  fi
}

save cargo    "${CARGO_HOME:-$LOCAL/cargo}"
save go-mod   "${GOMODCACHE:-$LOCAL/go/mod}"
save go-build "${GOCACHE:-$LOCAL/go/build}"
save npm      "${npm_config_cache:-$LOCAL/npm}"
save pip      "${PIP_CACHE_DIR:-$LOCAL/pip}"
save sccache  "${SCCACHE_DIR:-$LOCAL/sccache}"
exit 0
