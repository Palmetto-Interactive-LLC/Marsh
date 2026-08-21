#!/usr/bin/env bash
# Register a GPU runner snapshot from an already-pushed registry image.
#
# Daytona fixes resources (including GPU units) at snapshot registration, so a
# GPU size class needs its own snapshot. Sandboxes created from it may then be
# requested as spot capacity by the orchestrator (`spot = true` on the size
# class): preemptible, terminated without notice when on-demand GPU demand
# needs the capacity. Bake CUDA/tooling into the image; keep jobs retryable.
#
# This script only registers a NEW immutable name (see
# docs/SNAPSHOT-GOVERNANCE.md); it refuses to replace an existing snapshot.
#
# Secrets (env; never printed): DAYTONA_API_KEY
# Usage:
#   register-gpu-snapshot.sh <snapshot-name> <image-ref> [gpu] [cpu] [mem_gib] [disk_gib]
# Example:
#   register-gpu-snapshot.sh marsh-runner-gpu-v1 ghcr.io/org/marsh-runner-gpu:v1 1 8 32 50
set -euo pipefail
NAME="${1:?usage: register-gpu-snapshot.sh <snapshot-name> <image-ref> [gpu] [cpu] [mem_gib] [disk_gib]}"
IMAGE="${2:?image reference required (e.g. ghcr.io/org/marsh-runner-gpu:v1)}"
GPU="${3:-1}"; CPU="${4:-8}"; MEM="${5:-32}"; DISK="${6:-50}"
: "${DAYTONA_API_KEY:?}"
API_BASE="${DAYTONA_API_BASE:-https://app.daytona.io/api}"

case "$IMAGE" in
  *:latest) echo "refusing ':latest'; use an immutable tag or digest" >&2; exit 2 ;;
  *@sha256:*) ;;
  *:*) ;;
  *) echo "image reference must carry a tag or digest" >&2; exit 2 ;;
esac

existing=$(curl -fsS "${API_BASE}/snapshots?limit=200" -H "Authorization: Bearer $DAYTONA_API_KEY" \
  | jq -r --arg n "$NAME" '[(.items? // .)[] | select(.name==$n)] | length')
if [ "$existing" != "0" ]; then
  echo "snapshot ${NAME} already exists; immutable names are never replaced (bump the version)" >&2
  exit 3
fi

echo "==> register GPU snapshot ${NAME} (${GPU} gpu / ${CPU}c / ${MEM}g / ${DISK}g) from ${IMAGE}"
jq -n --arg name "$NAME" --arg image "$IMAGE" \
      --argjson gpu "$GPU" --argjson cpu "$CPU" --argjson mem "$MEM" --argjson disk "$DISK" \
      '{name:$name, imageName:$image, gpu:$gpu, cpu:$cpu, memory:$mem, disk:$disk}' \
  | curl -fsS -X POST "${API_BASE}/snapshots" \
      -H "Authorization: Bearer $DAYTONA_API_KEY" -H "Content-Type: application/json" \
      -d @- \
  | jq '{id, name, state, gpu, cpu, mem, disk}'
echo "==> done. Reference ${NAME} from a [[size_class]] with gpu labels (and optionally spot = true)."
