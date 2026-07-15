#!/usr/bin/env bash
# Reproducible Marsh runner pipeline: build the image once, push to GHCR, and register
# both sized snapshots FROM the registry image (Daytona pulls it via the connection
# from setup-registry.sh). Run this from a fast x86_64 Docker host or CI.
#
# Prereq: infra/snapshots/setup-registry.sh has connected GHCR to Daytona (pull).
# Secrets (env; never printed):
#   DAYTONA_API_KEY, GHCR_USER, GHCR_TOKEN (needs write:packages to push here)
# Optional non-secret configuration:
#   MARSH_RUNNER_DOCKERFILE (repository-relative; defaults to runner-image/Dockerfile)
# Usage: ./register-snapshot.sh <tag>          # e.g. v1
set -euo pipefail
TAG="${1:?usage: register-snapshot.sh <tag>}"
: "${DAYTONA_API_KEY:?}"; : "${GHCR_USER:?}"; : "${GHCR_TOKEN:?}"
IMAGE_OWNER="${GHCR_IMAGE_OWNER:-${GHCR_USER}}"
IMAGE_NAME="${GHCR_IMAGE_NAME:-marsh-runner}"
DEFAULT_SNAPSHOT="${MARSH_DEFAULT_SNAPSHOT:-marsh-runner-default}"
LARGE_SNAPSHOT="${MARSH_LARGE_SNAPSHOT:-marsh-runner-large}"
IMAGE="ghcr.io/${IMAGE_OWNER}/${IMAGE_NAME}:${TAG}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
DOCKERFILE_REL="${MARSH_RUNNER_DOCKERFILE:-runner-image/Dockerfile}"
case "$DOCKERFILE_REL" in
  /*|..|../*|*/../*|*//*)
    printf 'MARSH_RUNNER_DOCKERFILE must be a repository-relative path\n' >&2
    exit 2
    ;;
esac
DOCKERFILE="${ROOT}/${DOCKERFILE_REL}"
if [[ ! -f "$DOCKERFILE" ]]; then
  printf 'runner Dockerfile does not exist: %s\n' "$DOCKERFILE_REL" >&2
  exit 2
fi
export DAYTONA_API_KEY

echo "==> docker login ghcr.io (token via stdin, never echoed)"
printf '%s' "$GHCR_TOKEN" | docker login ghcr.io -u "$GHCR_USER" --password-stdin

echo "==> build + push ${IMAGE} from ${DOCKERFILE_REL} (linux/amd64)"
docker buildx build --platform linux/amd64 -t "${IMAGE}" \
  -f "${DOCKERFILE}" "${ROOT}" --push

# Register the one image as two sized snapshots (resources fixed at registration).
register() { # <snapshot-name> <cpu> <mem_gib> <disk_gib>
  local name="$1" cpu="$2" mem="$3" disk="$4"
  echo "==> register snapshot ${name} (${cpu}c/${mem}g/${disk}g) from ${IMAGE}"
  daytona snapshot delete "${name}" >/dev/null 2>&1 || true
  # wait for delete to settle (async), then create from the registry image
  for _ in $(seq 1 20); do
    curl -fsS "https://app.daytona.io/api/snapshots" -H "Authorization: Bearer $DAYTONA_API_KEY" \
      | jq -e --arg n "$name" '[(.items? // .)[]|select(.name==$n)]|length==0' >/dev/null && break
    sleep 3
  done
  daytona snapshot create "${name}" --image "${IMAGE}" --cpu "${cpu}" --memory "${mem}" --disk "${disk}"
}

register "$DEFAULT_SNAPSHOT" 2 4 10
register "$LARGE_SNAPSHOT"   4 8 10
echo "==> done. Snapshots point at ${IMAGE}; bump <tag> + rerun to roll a new image."
