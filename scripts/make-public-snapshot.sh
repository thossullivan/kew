#!/usr/bin/env bash
# Make the public snapshot of kew: the tree of `main` as `git archive` exports
# it, checked before anyone pushes it. It never pushes, never creates a remote
# repository and never changes this checkout.
#
# Usage: bash scripts/make-public-snapshot.sh <output-dir>
#
# Refuses unless this checkout is on `main` with no uncommitted change to a
# tracked file, and unless <output-dir> is missing or empty. Then:
#
#   1. `git archive` of HEAD into <output-dir>. Paths marked `export-ignore` in
#      .gitattributes are left out; each line there carries its reason.
#   2. Lists every tracked file left out, and refuses an `export-ignore` line
#      that leaves nothing out: a misspelt path would leak the file it meant
#      to drop.
#   3. Commits the snapshot as ONE commit in a throwaway repository inside
#      <output-dir> (the shape the public repository starts with), then runs
#      against it: THIS checkout's private-name scans ($PRIVATE_SCANS, which
#      the snapshot does not carry), pointed at the snapshot through
#      KEW_TREE_SCAN_ROOT; the snapshot's own kew/tests/test_tracked_tree.py;
#      and the pinned gitleaks with the snapshot's .gitleaks.toml.
#   4. Removes the throwaway repository and checks that <output-dir> holds
#      exactly the files the archive wrote, nothing the checks left behind.
#
# docs/operations.md ("Making the public snapshot") is the procedure around it.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
# The scans that must not ship; .gitattributes marks the file export-ignore.
PRIVATE_SCANS="kew/tests/test_private_names.py"

die() {
    echo "make-public-snapshot: $*" >&2
    exit 1
}

[ -f "$ROOT/$PRIVATE_SCANS" ] \
    || die "refusing: $PRIVATE_SCANS is missing; run this from the private checkout"

[ "$#" -eq 1 ] || die "usage: bash scripts/make-public-snapshot.sh <output-dir>"
out="$1"

branch="$(git -C "$ROOT" symbolic-ref --quiet --short HEAD || true)"
[ "$branch" = "main" ] || die "refusing: the checkout is on '${branch:-a detached HEAD}', not main"
git -C "$ROOT" diff --quiet HEAD -- \
    || die "refusing: tracked files differ from HEAD; commit or discard them first"

inside_checkout() {
    case "$1/" in
        "$ROOT"/*) die "refusing: $1 is inside the checkout" ;;
    esac
}
case "$out" in
    /*) inside_checkout "$out" ;;
    *) inside_checkout "$PWD/$out" ;;
esac
if [ -e "$out" ]; then
    [ -d "$out" ] || die "refusing: $out exists and is not a directory"
    [ -z "$(ls -A "$out")" ] || die "refusing: $out is not empty"
fi
mkdir -p "$out"
out="$(cd "$out" && pwd -P)"
inside_checkout "$out"

# The gitleaks build is part of the gate: CI and verify.sh pin one version,
# and a different build reads .gitleaks.toml differently. Read the pin from
# verify.sh rather than copying it here.
pinned_gitleaks="$(awk -F'"' '/^GITLEAKS_VERSION="/ { print $2; exit }' "$ROOT/kew/scripts/verify.sh")"
[ -n "$pinned_gitleaks" ] || die "kew/scripts/verify.sh pins no GITLEAKS_VERSION"
command -v gitleaks >/dev/null 2>&1 || die "gitleaks is not installed (want $pinned_gitleaks)"
found_gitleaks="$(gitleaks version)"
[ "$found_gitleaks" = "$pinned_gitleaks" ] \
    || die "gitleaks is $found_gitleaks, not the pinned $pinned_gitleaks"

commit="$(git -C "$ROOT" rev-parse HEAD)"
work="$(mktemp -d)"
cleanup() {
    rm -rf "$work" "$out/.git"
}
trap cleanup EXIT

echo "==> Exporting main at ${commit} to ${out}"
git -C "$ROOT" archive --format=tar -o "$work/snapshot.tar" "$commit"
tar -x -f "$work/snapshot.tar" -C "$out"
tar -t -f "$work/snapshot.tar" | grep -v '/$' | LC_ALL=C sort > "$work/exported"
git -C "$ROOT" ls-tree -r --name-only "$commit" | LC_ALL=C sort > "$work/tracked"
LC_ALL=C comm -23 "$work/tracked" "$work/exported" > "$work/left-out"

echo "==> Files left out of the snapshot (export-ignore in .gitattributes):"
sed 's/^/    /' "$work/left-out"
echo "    ($(wc -l < "$work/left-out" | tr -d ' ') files)"

# Every export-ignore line must leave at least one tracked file out.
git -C "$ROOT" show "${commit}:.gitattributes" \
    | awk '!/^[[:space:]]*#/ && / export-ignore/ { print $1 }' > "$work/patterns"
while IFS= read -r pattern; do
    if ! awk -v path="$pattern" \
        '$0 == path || index($0, path "/") == 1 { found = 1 } END { exit !found }' \
        "$work/left-out"; then
        die "the export-ignore line for '$pattern' leaves no tracked file out"
    fi
done < "$work/patterns"
if grep -qvxF -f "$work/tracked" "$work/exported"; then
    die "the archive holds a file HEAD does not track"
fi
if grep -qxF -- "$PRIVATE_SCANS" "$work/exported"; then
    die "the archive holds $PRIVATE_SCANS; it must be export-ignore"
fi

echo "==> Committing the snapshot as one commit in a throwaway repository"
git -C "$out" init -q -b main
git -C "$out" add -A --force
git -C "$out" -c user.name=snapshot -c user.email=snapshot@example.invalid \
    -c commit.gpgsign=false -c core.hooksPath=/dev/null \
    commit -q -m "kew public snapshot of ${commit}"

echo "==> Private-name scans and tree rules against the snapshot"
# Run from this checkout's environment. KEW_TREE_SCAN_ROOT points this
# checkout's private-name scans at the snapshot (tests/support/tree_scan.py);
# the snapshot's own test_tracked_tree.py reads its own tree with no
# override. KEW_TREE_SCAN_EXPECT_FILES is the
# number of files the archive wrote, which the identifier scan must read. No
# bytecode or cache is written into the snapshot.
expect_files="$(wc -l < "$work/exported" | tr -d ' ')"
(
    cd "$ROOT/kew"
    # Two runs: each test file loads its own tree's conftest and support/.
    KEW_TREE_SCAN_ROOT="$out" KEW_TREE_SCAN_EXPECT_FILES="$expect_files" \
        PYTHONDONTWRITEBYTECODE=1 \
        uv run --frozen pytest -q -p no:cacheprovider "$ROOT/$PRIVATE_SCANS"
    # The snapshot's own copy reads its own tree, the checkout it sits in, so
    # it needs (and refuses) no override.
    PYTHONDONTWRITEBYTECODE=1 \
        uv run --frozen pytest -q -p no:cacheprovider \
        "$out/kew/tests/test_tracked_tree.py"
)

echo "==> gitleaks ${found_gitleaks} over the snapshot commit"
gitleaks detect --source "$out" --config "$out/.gitleaks.toml" --redact --no-banner

rm -rf "$out/.git"
( cd "$out" && find . \( -type f -o -type l \) | sed 's|^\./||' | LC_ALL=C sort ) > "$work/after"
if ! cmp -s "$work/exported" "$work/after"; then
    echo "make-public-snapshot: the checks changed $out:" >&2
    diff "$work/exported" "$work/after" >&2 || true
    exit 1
fi

echo "==> Snapshot of ${commit} is in ${out}: $(wc -l < "$work/exported" | tr -d ' ') files, checks passed."
echo "    Nothing was pushed. docs/operations.md, \"Making the public snapshot\", has the next steps."
