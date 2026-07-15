#!/usr/bin/env bash
# ACTIONS_RUNNER_HOOK_JOB_STARTED — runs before every job, transparently (no
# workflow edits). Restores per-repo build caches from the Daytona Volume
# (mountpoint-s3) into LOCAL POSIX dirs. The volume only supports whole-file
# writes (no rename/append), so caches live there as tarballs and are extracted
# to local disk here; cache-save.sh tars them back on completion.
set -uo pipefail
CACHE_VOL="${CACHE_VOL:-/cache}"                 # mounted S3-FUSE volume
LOCAL="${CACHE_LOCAL:-/home/daytona/.marsh-cache}"  # local POSIX cache root
repo="${GITHUB_REPOSITORY:-unknown}"; repo="${repo//\//_}"
base="${CACHE_VOL}/${repo}"

restore() { # <name> <local-dir>
  local name="$1" dir="$2" tgz="${base}/${1}.tgz"
  mkdir -p "$dir"
  if [ -f "$tgz" ]; then
    tar -xzf "$tgz" -C "$dir" 2>/dev/null && echo "cache-restore: hit ${name}" || echo "cache-restore: bad ${name}"
  else
    echo "cache-restore: miss ${name}"
  fi
}

# Local dirs match the env exported in run-ephemeral.sh.
restore cargo   "${CARGO_HOME:-$LOCAL/cargo}"
restore go-mod  "${GOMODCACHE:-$LOCAL/go/mod}"
restore go-build "${GOCACHE:-$LOCAL/go/build}"
restore npm     "${npm_config_cache:-$LOCAL/npm}"
restore pip     "${PIP_CACHE_DIR:-$LOCAL/pip}"
restore sccache "${SCCACHE_DIR:-$LOCAL/sccache}"
exit 0
