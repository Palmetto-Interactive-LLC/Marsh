#!/usr/bin/env bash
# Connect GHCR to Daytona so it can pull the Marsh runner image.
#
# Discovered API: POST /api/docker-registry {name,url,username,password}. Daytona
# has no registry CLI command, so we use the API directly. Idempotent.
#
# Secrets (env; never printed/committed):
#   DAYTONA_API_KEY   the Daytona account key
#   GHCR_USER         a GitHub username/bot with read:packages on the org's GHCR
#   GHCR_TOKEN        GitHub PAT with read:packages (Daytona pull). Same token may
#                     carry write:packages if you also push with it (see register-snapshot.sh).
set -euo pipefail
: "${DAYTONA_API_KEY:?}"; : "${GHCR_USER:?}"; : "${GHCR_TOKEN:?}"
API="${DAYTONA_API_BASE:-https://app.daytona.io/api}"
NAME="${GHCR_REGISTRY_NAME:-ghcr-marsh}"

echo "==> ensure GHCR registry connection '${NAME}' in Daytona"
existing=$(curl -fsS "$API/docker-registry" -H "Authorization: Bearer $DAYTONA_API_KEY" \
           | jq -r --arg n "$NAME" '(.items? // .)[]? | select(.name==$n) | .id' | head -1)
if [ -n "$existing" ]; then
  echo "   already connected (id=$existing)"
else
  code=$(curl -sS -o /tmp/reg.json -w '%{http_code}' -X POST "$API/docker-registry" \
    -H "Authorization: Bearer $DAYTONA_API_KEY" -H "Content-Type: application/json" \
    -d "$(jq -n --arg n "$NAME" --arg u "$GHCR_USER" --arg p "$GHCR_TOKEN" \
          '{name:$n, url:"https://ghcr.io", username:$u, password:$p}')")
  if [ "$code" -lt 300 ]; then
    echo "   connected (id=$(jq -r '.id' /tmp/reg.json))"
  else
    echo "   FAILED ($code): $(jq -r '.message // .' /tmp/reg.json)"
    exit 1
  fi
fi
echo "==> done. Now build+push the image and register snapshots: infra/snapshots/register-snapshot.sh <tag>"
