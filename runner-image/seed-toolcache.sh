#!/usr/bin/env bash
# Pre-seed RUNNER_TOOL_CACHE so actions/setup-python|node|go are instant cache hits
# on this (Debian) image — the fix for pi-template-54g ("no hosted tool cache").
# Uses the exact hostedtoolcache layout setup-* expects: <tool>/<ver>/<arch>/ + a
# <tool>/<ver>/<arch>.complete marker. Best-effort; the volume overlay catches any
# version not seeded here. Add versions to the arrays as our repos need them.
set -uo pipefail
TC="${RUNNER_TOOL_CACHE:-/opt/hostedtoolcache}"
ARCH=x64
mkdir -p "$TC"

# ── Python (astral python-build-standalone; Ubuntu-only setup-python can't
#    self-provision on Debian). Seed the latest patch of each series so a
#    workflow asking for "3.12"/"3.11" is an instant hit. ──
# Authenticate the API lookup if a BuildKit secret token is mounted (unauth API
# rate-limits per build host).
AUTH=()
if [ -f /run/secrets/ghtoken ]; then AUTH=(-H "Authorization: Bearer $(cat /run/secrets/ghtoken)"); fi
REL_JSON=$(curl -fsSL "${AUTH[@]}" "https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest")
for series in 3.12 3.11; do
  # Match on .name (has a literal '+'); browser_download_url URL-encodes it as %2B,
  # which is why matching the URL returned nothing. Emit the (encoded) url to fetch.
  url=$(printf '%s' "$REL_JSON" | jq -r --arg s "$series" '
        [ .assets[]
          | select(.name | test("cpython-" + ($s|gsub("\\.";"[.]")) + "[.][0-9]+.*x86_64-unknown-linux-gnu-install_only[.]tar[.]gz$"))
          | .browser_download_url ]
        | sort | last // empty')
  [ -n "$url" ] || { echo "seed: no python $series asset in latest release"; continue; }
  ver=$(printf '%s' "$url" | grep -oE 'cpython-[0-9]+\.[0-9]+\.[0-9]+' | head -1 | cut -d- -f2)
  [ -n "$ver" ] || { echo "seed: could not parse python $series version"; continue; }
  [ -f "$TC/Python/$ver/$ARCH.complete" ] && { echo "seed: python $ver present"; continue; }
  tmp=$(mktemp -d)
  if curl -fsSL "$url" -o "$tmp/py.tgz" && tar -xzf "$tmp/py.tgz" -C "$tmp"; then
    mkdir -p "$TC/Python/$ver"; rm -rf "$TC/Python/$ver/$ARCH"; mv "$tmp/python" "$TC/Python/$ver/$ARCH"
    touch "$TC/Python/$ver/$ARCH.complete"
    echo "seed: python $ver ($series)"
  fi
  rm -rf "$tmp"
done

# ── Node (setup-node layout) ────────────────────────────────────────────────
NODE_VERSIONS=("20.18.1" "22.12.0")
for ver in "${NODE_VERSIONS[@]}"; do
  dest="$TC/node/$ver/$ARCH"
  [ -f "$TC/node/$ver/$ARCH.complete" ] && continue
  tmp=$(mktemp -d)
  if curl -fsSL "https://nodejs.org/dist/v${ver}/node-v${ver}-linux-x64.tar.gz" -o "$tmp/n.tgz" && tar -xzf "$tmp/n.tgz" -C "$tmp"; then
    mkdir -p "$dest"; cp -a "$tmp/node-v${ver}-linux-x64/." "$dest/"
    touch "$TC/node/$ver/$ARCH.complete"
    echo "seed: node $ver"
  fi
  rm -rf "$tmp"
done

chown -R daytona:daytona "$TC" 2>/dev/null || true
echo "seed-toolcache: done"
