#!/usr/bin/env bash
# Rebuild the IBM Plex arrow subsets in src/lib/fonts/ (#3478).
#
# Fontsource's Plex faces carry no ← (U+2190), → (U+2192) or ↗ (U+2197), so
# the browser drew them from a platform fallback whose width differs between
# macOS and Linux (#3460). Upstream Plex has them; this cuts U+2190, U+2192 and
# U+2194-2199 out of IBM's complete woff2 files at Regular, Medium and SemiBold
# for Mono and Sans. U+2191 and U+2193 are left out: fontsource's latin face
# already serves them, and two faces for one code point would make the order
# of the @font-face rules matter.
#
# Every input is pinned (the npm tarballs by version and sha256, fontTools and
# brotli by version), so a rerun writes the same bytes:
#   bash scripts/plex-arrows.sh && git diff --exit-code -- src/lib/fonts/
# Needs network (npm, PyPI) and uvx. Run from anywhere.
set -euo pipefail

FONTTOOLS='fonttools[woff]==4.60.1'
BROTLI='brotli==1.2.1'
UNICODES='U+2190,U+2192,U+2194-2199'

# Lookups are `case` functions, not associative arrays: macOS's /bin/bash is
# 3.2, which has no `declare -A`.
sha256_of() {
  case "$1" in
    mono) echo 744157918f504d2ec10ef2a26b7ec080e362743dda99618c8ff547e946e40d45 ;;
    sans) echo c3818979c2a2c82927ea3e7c485e389a18de0b7462887dd4f7acc9a525568962 ;;
  esac
}
file_prefix_of() {
  case "$1" in
    mono) echo IBMPlexMono ;;
    sans) echo IBMPlexSans ;;
  esac
}
weight_name_of() {
  case "$1" in
    400) echo Regular ;;
    500) echo Medium ;;
    600) echo SemiBold ;;
  esac
}

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
out="$here/../src/lib/fonts"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
mkdir -p "$out"

for family in mono sans; do
  pkg="@ibm/plex-$family@1.1.0"
  tarball="$(cd "$work" && npm pack --silent "$pkg")"
  echo "$(sha256_of "$family")  $work/$tarball" | sha256sum --check --quiet -
  mkdir -p "$work/$family"
  tar -xzf "$work/$tarball" -C "$work/$family"
  fonts="$work/$family/package/fonts/complete/woff2"
  for weight in 400 500 600; do
    uvx --quiet --with "$BROTLI" --from "$FONTTOOLS" pyftsubset \
      "$fonts/$(file_prefix_of "$family")-$(weight_name_of "$weight").woff2" \
      --unicodes="$UNICODES" --flavor=woff2 \
      --output-file="$out/ibm-plex-$family-arrows-$weight-normal.woff2"
  done
done

# Both packages ship the same OFL text; one copy covers all six files.
cmp "$work/mono/package/fonts/complete/woff2/license.txt" "$work/sans/package/fonts/complete/woff2/license.txt"
cp "$work/mono/package/fonts/complete/woff2/license.txt" "$out/OFL.txt"
