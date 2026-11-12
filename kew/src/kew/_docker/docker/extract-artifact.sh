#!/usr/bin/env bash
# =============================================================================
# kew — extract the agent's work into an artifact, INSIDE the container.
#
# The host must never point git at a directory an agent can write: reading a
# repository runs code from it (core.fsmonitor, filter.*.clean, and a repo-local
# .git/config that cannot be suppressed). So extraction happens here, where
# untrusted code already runs and this grants an attacker nothing new, and the
# host reads the result as opaque bytes.
#
#   usage: extract-artifact.sh <base-commit> <output-dir>
#
# Produces, per docs/sandbox-entrypoint-contract.md element 6:
#
#   <output-dir>/manifest.json   {"scope","base","entries":[{path,mode,sha256}],"deletions":[]}
#   <output-dir>/blobs/<sha256>  content, addressed by its own hash
#
# ## The package is the WHOLE workspace, not the commits
#
# An agent that edited files and never ran `git commit` has still done the work.
# Extracting `base..HEAD` called that a no-change publication — the single
# failure this producer exists to prevent, because "nothing to publish" is
# indistinguishable from "nothing was done" by the time the host sees it. So the
# package spans committed, staged, unstaged and untracked work as one state:
# the diff is taken from the trusted base to the WORKING TREE, and untracked
# files are enumerated separately.
#
# `scope` states that in the manifest. The host refuses an EMPTY package that
# does not declare it, so a producer that only ever knew about commits cannot
# have its silence read as a verified no-change.
#
# Ignored files (.gitignore, core.excludesFile, .git/info/exclude) are the one
# deliberate omission: they are build output and local secrets, they are what
# the agent itself declared is not work, and no ordinary git publication would
# carry them either.
#
# ## Nothing is followed, and nothing is skipped
#
# Every mode comes from git's own record of the object — never from a guess
# about the filesystem — and any path that is a symlink or a gitlink on either
# side refuses the WHOLE extraction. Untracked content is the exception that
# proves it: git has no object for it, so the path is walked component by
# component and refused if any of them is a link, then refused again if the
# result is not a regular file. A FIFO would otherwise block the read forever
# rather than be carried.
#
# Enumeration output is written to a file and its exit status checked before
# anything is parsed. `while read ... done < <(git ...)` throws that status
# away, so a git that died halfway produced a SHORT list and a manifest that
# quietly called the missing work no-change.
#
# Only git, jq and coreutils are used — the kew-agent image has no python3, and
# jq is what makes the JSON escaping correct for paths this script must not try
# to quote itself.
# =============================================================================
set -euo pipefail

# Pin the C locale so every byte-wise operation is collation- and
# encoding-independent — this script is a determinism contract. Without it a
# dictionary-collation locale (en_US.UTF-8) makes `[a-z]` also match uppercase
# A-Y, so the ordinary 'H' cached-index tag matches the assume-unchanged reject
# glob below and every tracked file is refused. The container runs C by default;
# pinning makes that true off the container too (the host test gate, a CI box).
export LC_ALL=C

#: Declares what the manifest covers. The host keys its empty-package rule on
#: this exact string, so changing it is a contract change, not a rename.
SCOPE="workspace"

if [ "$#" -ne 2 ]; then
    echo "usage: $0 <base-commit> <output-dir>" >&2
    exit 2
fi

base="$1"
out="$2"

command -v git >/dev/null || { echo "extract: git not found" >&2; exit 3; }
command -v jq >/dev/null || { echo "extract: jq not found" >&2; exit 3; }
command -v sha256sum >/dev/null || { echo "extract: sha256sum not found" >&2; exit 3; }
command -v stat >/dev/null || { echo "extract: stat not found" >&2; exit 3; }

# Absolute BEFORE the cd below, or a relative output directory would be created
# somewhere other than where the caller named.
case "$out" in
    /*) ;;
    *) out="$PWD/$out" ;;
esac

git rev-parse --verify --quiet "$base^{commit}" >/dev/null || {
    echo "extract: base '$base' is not a commit in this checkout" >&2
    exit 4
}

# `git ls-files --others` lists paths RELATIVE TO THE CWD and only below it,
# while `git diff` reports from the repository root. Run from the root so the
# two enumerations describe the same namespace — and so a producer invoked in a
# subdirectory cannot silently omit everything above it.
toplevel="$(git rev-parse --show-toplevel)" || {
    echo "extract: not inside a git working tree" >&2
    exit 9
}
cd "$toplevel"

# The output directory must not be part of what is being captured. Inside the
# worktree, every blob written would be enumerated as untracked work by the
# loop still running — the artifact would start eating itself.
case "$out/" in
    "$toplevel"/*)
        echo "extract: output directory '$out' is inside the checkout it must capture" >&2
        exit 11
        ;;
esac

# A fresh location per artifact. Re-running into a populated directory leaves
# blobs the new manifest does not reference, and the host refuses unreferenced
# content — so the second run would produce an artifact that cannot be read,
# for a reason nowhere near the cause.
if [ -e "$out" ] && [ -n "$(ls -A "$out" 2>/dev/null)" ]; then
    echo "extract: output directory '$out' is not empty" >&2
    exit 7
fi
mkdir -p "$out/blobs"
entries="$(mktemp)"
deletions="$(mktemp)"
tracked="$(mktemp)"
staged="$(mktemp)"
marks="$(mktemp)"
untracked="$(mktemp)"
# Staged beside blobs/ rather than in /tmp: /tmp is a small tmpfs in the
# extraction container, and a large file would fail there for a reason that
# has nothing to do with the work.
staging="$out/.incoming"
trap 'rm -f "$entries" "$deletions" "$tracked" "$staged" "$marks" "$untracked" "$staging"' EXIT
: >"$entries"
: >"$deletions"

refuse_link() {
    echo "extract: refusing '$1' (mode $2): symlinks and gitlinks cannot be carried" >&2
    exit 8
}

# Refuse a symlink or gitlink on EITHER side of a change. The base side matters
# as much as the worktree side: replacing a symlink with a regular file would
# otherwise carry the link's removal without the refusal a plain deletion of it
# gets — a one-file bypass of the rule. Refused by explicit mode rather than by
# allow-list, because 000000 (absent) and 040000 (a tree) are the legitimate
# shape changes.
reject_uncarryable_mode() {
    case "$2" in
        120000 | 160000) refuse_link "$1" "$2" ;;
    esac
}

# Every component, not just the last. A tracked or untracked path may sit under
# a directory that is itself a symlink, and opening `d/f` where `d` points at
# /etc reads whatever the link chose — the one way content from outside the
# checkout can enter the artifact.
reject_link_components() {
    local rest="$1" walked="" component
    while [ -n "$rest" ]; do
        component="${rest%%/*}"
        if [ "$component" = "$rest" ]; then
            rest=""
        else
            rest="${rest#*/}"
        fi
        [ -n "$component" ] || continue
        walked="${walked:+$walked/}$component"
        [ -L "$walked" ] && refuse_link "$walked" 120000
    done
    return 0
}

# Prove from the filesystem what git has no object to prove. `-f` is checked
# only AFTER the link walk, and before any read: a FIFO is not a symlink, and
# opening one for reading blocks until a writer appears — the extraction would
# hang rather than refuse.
require_regular_file() {
    reject_link_components "$1"
    if [ ! -f "$1" ]; then
        echo "extract: refusing '$1': not a regular file that publication can carry" >&2
        exit 10
    fi
}

#: Set by `stage_blob` to the digest of what it just staged.
staged_digest=""

# The content is read ONCE, into the staging file, and the digest is taken from
# the COPY. Hashing the source and then copying it separately are two reads of
# the same bytes, and a blob named after bytes it does not hold is exactly what
# the host's re-hash refuses.
stage_blob() {
    staged_digest="$(sha256sum <"$staging" | cut -d' ' -f1)"
    if [ -e "$out/blobs/$staged_digest" ]; then
        # Two paths sharing content share a blob, which is the point of
        # addressing by digest.
        rm -f "$staging"
    else
        mv "$staging" "$out/blobs/$staged_digest"
    fi
}

record_entry() {
    jq -Rn \
        --arg p "$1" --arg m "$2" --arg s "$3" \
        '{path: $p, mode: $m, sha256: $s}' >>"$entries"
}

carry_worktree_file() {
    require_regular_file "$1"
    cat <"$1" >"$staging"
    stage_blob
    record_entry "$1" "$2" "$staged_digest"
}

carry_git_object() {
    git cat-file blob "$3" >"$staging" || {
        echo "extract: no tree entry for $1" >&2
        exit 5
    }
    stage_blob
    record_entry "$1" "$2" "$staged_digest"
}

# One `--raw` record: ":<srcmode> <dstmode> <srcoid> <dstoid> <status>" and the
# path. The modes are git's, so a symlink is refused without the filesystem
# being consulted at all.
handle_tracked() {
    local meta="${1#:}" path="$2"
    local srcmode dstmode dstoid status
    srcmode="${meta%% *}"; meta="${meta#* }"
    dstmode="${meta%% *}"; meta="${meta#* }"
    meta="${meta#* }" # the source object id, which nothing here needs
    dstoid="${meta%% *}"; status="${meta#* }"

    reject_uncarryable_mode "$path" "$srcmode"
    reject_uncarryable_mode "$path" "$dstmode"

    case "$status" in
        D)
            # A deletion's mode came from the base tree above, which is the
            # only place it still exists.
            [ "$srcmode" != "000000" ] || {
                echo "extract: no tree entry for $path" >&2
                exit 5
            }
            jq -Rn --arg p "$path" '$p' >>"$deletions"
            ;;
        A | M | T)
            case "$dstmode" in
                100644 | 100755) ;;
                *) refuse_link "$path" "$dstmode" ;;
            esac
            # A destination object id means git already has these bytes: use
            # them, so committed and staged content is carried exactly as git
            # recorded it rather than as a checkout filter left it on disk. The
            # id is all zeroes when the change is only in the working tree, and
            # then the file itself is the only copy there is.
            case "$dstoid" in
                *[!0]*) carry_git_object "$path" "$dstmode" "$dstoid" ;;
                *) carry_worktree_file "$path" "$dstmode" ;;
            esac
            ;;
        U)
            # An unmerged path has no single state to carry. Publishing either
            # side, or the conflicted file, would be a guess.
            echo "extract: refusing '$path': the path is unmerged" >&2
            exit 6
            ;;
        *)
            echo "extract: unexpected status '$status' for $path" >&2
            exit 6
            ;;
    esac
}

# The INDEX side of the change, judged for carryability only — content still
# comes from the working tree below.
#
# A worktree diff cannot see a gitlink whose directory is absent: git reports
# nothing at all for it, so a committed submodule would have left the package
# silently, which is the omission this producer exists to make impossible. The
# index remembers it. Only paths the index CHANGED relative to the base reach
# here, so a symlink that predates the work and was never touched still does
# not poison an unrelated edit.
reject_staged_shape() {
    local meta="${1#:}" path="$2" srcmode dstmode
    srcmode="${meta%% *}"; meta="${meta#* }"
    dstmode="${meta%% *}"
    reject_uncarryable_mode "$path" "$srcmode"
    reject_uncarryable_mode "$path" "$dstmode"
}

# `git ls-files -v` tags each index entry, and two tags mean the package cannot
# be honest about that path:
#
# * a lowercase tag or `S` is `--assume-unchanged` / `--skip-worktree`. An
#   agent that sets either and then edits the file has made a change that NO
#   enumeration reports — the working-tree diff is empty, the index blob is
#   unchanged, and the file is not untracked. The package would be a verified
#   no-change over work sitting on disk.
# * `M` is unmerged. A conflicted path has no single state, so carrying one
#   side, or the file with its markers, is a guess. The working-tree diff calls
#   it a plain modification, so this is where it has to be caught.
reject_unusable_index_entry() {
    local tag="${1%% *}" path="${1#* }"
    case "$tag" in
        [a-z] | S)
            echo "extract: refusing '$path': its index entry is marked '$tag', so changes to it cannot be enumerated" >&2
            exit 9
            ;;
        M)
            echo "extract: refusing '$path': the path is unmerged" >&2
            exit 6
            ;;
    esac
}

# Untracked files have no index entry, so the executable bit comes from the
# filesystem — but only where git would honour it. With core.fileMode off, a
# tracked file's mode comes from the index and an untracked one from the disk,
# and the two would disagree about the same repository.
filemode="$(git config --get core.fileMode || echo true)"

#: Set by `set_untracked_mode`, never returned through `$(...)`: a refusal
#: inside a command substitution exits only the SUBSHELL, so the caller would
#: carry on with an empty mode and write a manifest instead of refusing.
untracked_mode=""

# `test -x` asks whether this process could EXECUTE the file, which is not the
# question: on a noexec mount it answers no for a file whose owner-execute bit
# is plainly set, and the package would publish a script that cannot be run.
# `stat` reports the bit itself. S_IXUSR alone, because that is the only bit
# git looks at when it decides between 100644 and 100755.
set_untracked_mode() {
    local hexmode octperm ixusr
    # GNU stat prints the raw mode in hex (-c '%f'); BSD/macOS stat has no -c
    # and prints the permission bits in octal with -f '%Lp'. The container ships
    # GNU coreutils, so try it first and fall back to BSD for the host test gate.
    # Either way we mask S_IXUSR (0100) — the only bit git reads to choose
    # between 100644 and 100755.
    if hexmode="$(stat -c '%f' -- "$1" 2>/dev/null)" && [ -n "$hexmode" ]; then
        ixusr=$((0x$hexmode & 0100))
    elif octperm="$(stat -f '%Lp' -- "$1" 2>/dev/null)" && [ -n "$octperm" ]; then
        ixusr=$((8#$octperm & 0100))
    else
        echo "extract: refusing '$1': its mode could not be read" >&2
        exit 10
    fi
    if [ "$filemode" != "false" ] && [ "$ixusr" -ne 0 ]; then
        untracked_mode=100755
    else
        untracked_mode=100644
    fi
}

handle_untracked() {
    require_regular_file "$1"
    set_untracked_mode "$1"
    carry_worktree_file "$1" "$untracked_mode"
}

# --no-renames so a rename is a delete plus an add. A rename record would need
# the host to trust a pairing it cannot verify; two independent operations are
# checked by the same rules as any other.
#
# -z because a path may contain anything except NUL. Without it git quotes
# unusual names and this script would have to unquote them — writing a second,
# subtly different parser for the one input that is fully attacker-controlled.
#
# --abbrev=40 because `git diff --raw` shortens object ids by default, and a
# shortened id is one collision away from naming a different object.
git diff --raw --abbrev=40 --no-renames --ignore-submodules=none -z "$base" -- \
    >"$tracked" || {
    echo "extract: could not enumerate tracked changes against '$base'" >&2
    exit 9
}
git diff --raw --abbrev=40 --no-renames --ignore-submodules=none --cached -z \
    "$base" -- >"$staged" || {
    echo "extract: could not enumerate staged changes against '$base'" >&2
    exit 9
}
git ls-files -v -z >"$marks" || {
    echo "extract: could not enumerate the index" >&2
    exit 9
}
git ls-files --others --exclude-standard -z >"$untracked" || {
    echo "extract: could not enumerate untracked files" >&2
    exit 9
}

while IFS= read -r -d '' record; do
    reject_unusable_index_entry "$record"
done <"$marks"

while IFS= read -r -d '' meta && IFS= read -r -d '' path; do
    reject_staged_shape "$meta" "$path"
done <"$staged"

while IFS= read -r -d '' meta && IFS= read -r -d '' path; do
    handle_tracked "$meta" "$path"
done <"$tracked"

while IFS= read -r -d '' path; do
    handle_untracked "$path"
done <"$untracked"

# Sorted, so the same workspace produces the same manifest bytes twice —
# git's enumeration order is not part of the contract, and a package that
# only compares equal by luck cannot be used as lineage evidence.
#
# A path that is both deleted and present is present: dropping a file from the
# index while leaving it on disk reports a deletion against the base AND an
# untracked add, and the workspace plainly still holds the file.
jq -n \
    --arg scope "$SCOPE" \
    --arg base "$(git rev-parse "$base")" \
    --slurpfile entries "$entries" \
    --slurpfile deletions "$deletions" \
    '{scope: $scope,
      base: $base,
      entries: ($entries | sort_by(.path)),
      deletions: (($deletions - ($entries | map(.path))) | sort)}' \
    >"$out/manifest.json"
