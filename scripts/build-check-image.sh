#!/usr/bin/env bash
# Build kew's check image (#3281): the approved agent image plus kew's locked
# Python environment, the frontend's node_modules, the ui-checks' Chromium and
# semgrep (scripts/check-image.Dockerfile). `[sandbox] check_image` names the
# tag this prints, and kew's pre-review gate and `[resolve]` checks then run in
# it with no network.
#
#   bash scripts/build-check-image.sh --print-tag   # the tag, no build
#   bash scripts/build-check-image.sh               # build it under that tag
#
# The tag is `kew-check:<12 hex>` over the Dockerfile, kew/pyproject.toml,
# kew/uv.lock, frontend/package.json, frontend/package-lock.json, the base
# agent pin and the semgrep and setuptools pins, so a lock that moves names an
# image that is not built yet rather than reusing a stale one.
#
# Lives outside docker/ on purpose: every file there feeds the agent images'
# digest pin and ships in the package.
set -euo pipefail

# Called from the root or from kew/; resolve rather than assume.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
# The container runtime, `docker` unless KEW_CHECK_RUNTIME names another. A
# path (`./podman`) is relative to the caller, so it is made absolute before
# the cd below; a bare name is looked up on PATH as it is.
RUNTIME="${KEW_CHECK_RUNTIME:-docker}"
case "$RUNTIME" in
    */*) RUNTIME="$(cd "$(dirname "$RUNTIME")" && pwd -P)/$(basename "$RUNTIME")" ;;
esac
cd "$REPO_ROOT"
DOCKERFILE="scripts/check-image.Dockerfile"
INPUTS=(
    "$DOCKERFILE"
    kew/pyproject.toml
    kew/uv.lock
    frontend/package.json
    frontend/package-lock.json
)
# The exact semgrep and setuptools the image installs: read from the pin file
# every gate reads them from, and written nowhere else, here or in the
# Dockerfile (tests/test_verify_parity.py holds both to it).
TOOL_PINS="kew/scripts/tool-pins.env"

usage() {
    echo "usage: bash scripts/build-check-image.sh [--print-tag]" >&2
    exit 2
}

sha256() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum | cut -d' ' -f1
    else
        shasum -a 256 | cut -d' ' -f1
    fi
}

# The `[sandbox.images] claude-code` pin in kew.toml: the image this one is FROM.
agent_pin() {
    awk '
        /^\[/ { section = $0; next }
        section == "[sandbox.images]" && $1 == "claude-code" {
            value = $0
            sub(/^[^=]*=[ \t]*"/, "", value)
            sub(/".*$/, "", value)
            print value
            exit
        }
    ' "$REPO_ROOT/kew.toml"
}

check_tag() {
    local base="$1" input
    {
        for input in "${INPUTS[@]}"; do
            printf '%s %s\n' "$input" "$(sha256 < "$REPO_ROOT/$input")"
        done
        printf 'base %s\n' "$base"
        printf 'semgrep %s\n' "$SEMGREP_VERSION"
        printf 'setuptools %s\n' "$SETUPTOOLS_VERSION"
    } | sha256 | cut -c1-12 | sed 's/^/kew-check:/'
}

main() {
    local print_only=0
    case "${1:-}" in
        "") ;;
        --print-tag) print_only=1 ;;
        *) usage ;;
    esac
    [ "$#" -le 1 ] || usage

    local input
    for input in "${INPUTS[@]}" kew.toml "$TOOL_PINS"; do
        if [ ! -f "$REPO_ROOT/$input" ]; then
            echo "build-check-image.sh: $input is missing, so there is no check image to name." >&2
            exit 1
        fi
    done
    local base pin tag
    base="$(agent_pin)"
    if [ -z "$base" ]; then
        echo "build-check-image.sh: kew.toml pins no [sandbox.images] claude-code image to build FROM." >&2
        exit 1
    fi
    SEMGREP_VERSION="$(awk -F= '$1 == "SEMGREP_VERSION" { print $2 }' kew/scripts/tool-pins.env)"
    SETUPTOOLS_VERSION="$(awk -F= '$1 == "SETUPTOOLS_VERSION" { print $2 }' kew/scripts/tool-pins.env)"
    for pin in SEMGREP_VERSION SETUPTOOLS_VERSION; do
        if [ -z "${!pin}" ]; then
            echo "build-check-image.sh: $TOOL_PINS pins no $pin, so there is no check image to name." >&2
            exit 1
        fi
    done
    tag="$(check_tag "$base")"
    if [ "$print_only" -eq 1 ]; then
        echo "$tag"
        return 0
    fi

    # The context holds only the inputs the Dockerfile copies, not the
    # checkout's own .venv and node_modules.
    # Global, not local: the EXIT trap runs after main returns, where a local
    # is unset and `set -u` would fail a build that succeeded.
    context="$(mktemp -d)"
    trap 'rm -rf "$context"' EXIT
    mkdir -p "$context/kew" "$context/frontend"
    cp "$REPO_ROOT/kew/pyproject.toml" "$REPO_ROOT/kew/uv.lock" "$context/kew/"
    cp "$REPO_ROOT/frontend/package.json" "$REPO_ROOT/frontend/package-lock.json" "$context/frontend/"

    echo "build-check-image.sh: building $tag FROM $base" >&2
    "$RUNTIME" build \
        --file "$REPO_ROOT/$DOCKERFILE" \
        --build-arg "AGENT_IMAGE=$base" \
        --build-arg "SEMGREP_VERSION=${SEMGREP_VERSION}" \
        --build-arg "SETUPTOOLS_VERSION=${SETUPTOOLS_VERSION}" \
        --tag "$tag" \
        "$context"
    echo "$tag"
}

main "$@"
