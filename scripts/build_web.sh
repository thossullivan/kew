#!/usr/bin/env bash
# Build the SvelteKit SPA and copy it into the Python package so the wheel
# can serve it. Run from anywhere; paths resolve to the repo root.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

cd "$ROOT/frontend"
npm ci
npm run build

DEST="$ROOT/kew/src/kew/serve/web"
rm -rf "$DEST"
mkdir -p "$DEST"
cp -R build/. "$DEST/"

# Freshness stamp: git sha + build time, read back by `kew serve` for the
# footer badge and the "source newer than bundle" staleness warning.
GIT_SHA="$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
BUILT_AT="$(date +%s)"
printf '{"git_sha": "%s", "built_at": %s}\n' "$GIT_SHA" "$BUILT_AT" \
    > "$DEST/build-stamp.json"

echo "web bundle -> $DEST (git_sha=$GIT_SHA)"
