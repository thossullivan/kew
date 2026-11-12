#!/usr/bin/env bash
# Docs gate — run identically by CI (.github/workflows/docs.yml) and by
# kew/scripts/verify.sh, so the parity contract in
# kew/tests/test_verify_parity.py holds with ONE signature on both sides
# (same pattern as the www gate: put the gates behind one script so the
# signature cannot depend on how either side spells them).
#
# Six things are proven here:
#   1. the internal material deleted by the E01 curation (2026-08-26) and by
#      the launch cut (2026-09-22) stays deleted, `docs/design/` holds only
#      the allowlisted public pages, and nothing in the tip still points into
#      a deleted docs tree;
#   2. a deleted docs basename is not still cited in the public project tree;
#   3. built pages cannot link to excluded pages;
#   4. the docs still build with the pinned mkdocs/material versions;
#   5. no held internal working material reaches the built site — the durable pin
#      for the 2026-08-06 leak (PR #1407), where `not_in_nav` only suppressed
#      warnings and ~214 internal pages were published and sitemap-indexed;
#   6. no built page loads a resource from another origin, and mkdocs.yml
#      turns on no Material integration that would (#1168).
set -euo pipefail

cd "$(dirname "$0")/.."
# The exact mkdocs and mkdocs-material versions every build below runs, kept in one
# place with the other gate tools (tests/test_verify_parity.py holds it).
source kew/scripts/tool-pins.env

echo "==> Asserting the deleted internal docs trees have not returned…"
# E01 (2026-08-26, PR #1701) curated the ADR-grade specs and the still-live plans
# into `docs/design/` and deleted these six trees from the tip; the seventh,
# `docs/audit/`, kept the packs that named a still-open finding until the
# launch cut below removed it too. All 235 files are mirrored in the private
# docs archive at kew 4a387c0b (commit cf9e16da782d). Until then each tree
# had an `exclude_docs` entry in mkdocs.yml keeping it out of the built site.
# Those entries are gone with the trees, and the guarantee they provided —
# internal working material can never reach a published site — is enforced here
# instead, and more strongly: these prefixes may not exist under `docs/` at all,
# so a returning tree fails the gate before it can reach a build.
#
returned=0
for d in superpowers handoff spikes plans receipts launch-surface; do
    if [ -e "docs/${d}" ]; then
        echo "::error::docs/${d} is back in the tree — internal working material belongs in the private docs mirror, not the public tip" >&2
        returned=1
    fi
done

# The launch cut (2026-09-22, pre-launch re-audit findings F3 and F6) moved the
# rest of the internal material to the private docs archive at kew 1702613a:
# all of `docs/audit/`, every `docs/design/` page off the owner-approved
# allowlist below, `kew/docs/plans/`, `kew/docs/issues/`, `evidence-packs/`
# and the committed `.kew/epic-plan-*.json` files.
#
# `docs/` paths are checked in the working tree, like the six trees above:
# mkdocs builds untracked files too, so an uncommitted internal page would
# still reach a build. The other prefixes never reach the site; what matters
# is that they are not committed again, so they are checked in the index
# (tracked or staged), and local untracked scratch such as a fresh
# `kew report export` does not fail the gate.
if [ -e docs/audit ]; then
    echo "::error::docs/audit is back in the tree — audit packs belong in the private docs archive" >&2
    returned=1
fi
for d in kew/docs/plans kew/docs/issues evidence-packs; do
    if [ -n "$(git ls-files -- "${d}")" ]; then
        echo "::error::${d} is back in the tree — internal working material belongs in the private docs archive" >&2
        returned=1
    fi
done
# `.kew/epic-plan-manifest.json` is not an archived plan: it is the file
# `kew epic plan --from-manifest` reads at runtime (kew/src/kew/graph/plan.py),
# and the stale-reference scan below spares it for the same reason (#1993).
while IFS= read -r plan; do
    echo "::error::${plan} is internal — epic plans belong in the private docs archive" >&2
    returned=1
done < <(git ls-files -- '.kew/epic-plan-*' ':!.kew/epic-plan-manifest.json')

# An allowlist, not a denylist: a new public design page needs a deliberate
# edit here (and in PUBLIC_DESIGN_PAGES in kew/tests/test_docs_truthfulness.py,
# which pins the two lists and the tree together).
PUBLIC_DESIGN_PAGES=(
    2026-06-25-audit-chain-design.md
    2026-06-28-cache-aware-routing-design.md
    2026-07-01-epic-dag-design.md
    2026-07-10-launch-hardening-design.md
    2026-07-10-sandboxed-dispatch-design.md
    2026-07-23-adversarial-review-stage.md
    2026-08-06-host-owned-publication-broker.md
    2026-08-17-kew-product-charter.md
    2026-08-17-production-certified-customer-journeys.md
    2026-08-21-run-report-sink-design.md
)
if [ -d docs/design ]; then
    while IFS= read -r -d '' page; do
        name=${page#docs/design/}
        allowed=0
        for public in "${PUBLIC_DESIGN_PAGES[@]}"; do
            if [ "$name" = "$public" ]; then
                allowed=1
            fi
        done
        if [ "$allowed" -ne 1 ]; then
            echo "::error::${page} is not on the public design allowlist — internal design notes belong in the private docs archive" >&2
            returned=1
        fi
    done < <(find docs/design -mindepth 1 -print0)
fi

if [ "$returned" -ne 0 ]; then
    echo "docs gate FAILED: a deleted internal docs tree returned to the tip." >&2
    exit 1
fi

echo "==> Asserting nothing in the tip still points into a deleted tree…"
# mkdocs --strict only checks markdown links; code-span paths, README, source
# docstrings and workflows are invisible to it. A reference written as
# `path@4a387c0b` or `path@1702613a` is a deliberate pointer into git history
# (the E01 and launch-cut mirror commits), not a dangling path.
# Text files only (-I): pytest writes .pyc files whose docstrings carry these
# paths, and verify.sh runs the gate after pytest.
SCAN="README.md docs kew/src kew/tests kew.toml .github mkdocs.yml scripts www .claude AGENTS.md CLAUDE.md .dockerignore"
# `docs/design/` is scanned: since the launch cut it holds only the ten
# published pages, and a published page citing archived material must use
# a history pointer like any other public file.
#
# `kew/tests/.test_durations.json` is the one exempt FILE (#2344). It is the
# recorded cost of every test, keyed by pytest node id, that `--shard=K/N`
# balances the verify VMs with (`kew/tests/support/shard.py`), and a
# parametrised node id quotes its parameters — so the ids of
# `kew/tests/test_docs_truthfulness.py`'s own refusal cases contain the deleted
# prefixes below, for the same reason that file synthesises them from fragments
# rather than writing them out. It is exempt on the same ground as this script:
# naming a prefix in order to refuse it is not a citation of it, and every id in
# the recording comes from a test module that IS scanned, so a real reference
# still fails the gate. `kew/tests/test_shard.py` pins the name against the one
# `shard.py` reads, so a renamed recording cannot quietly fall out of step.
GENERATED="--exclude=.test_durations.json"
EXCL="-I --exclude-dir=__pycache__ --exclude-dir=.venv --exclude-dir=site --exclude-dir=node_modules --exclude-dir=worktrees $GENERATED"
# The launch cut (1702613a) added `docs/audit/`, `kew/docs/plans/` (already
# covered by `docs/plans/`), `kew/docs/issues/`, `evidence-packs/` and the
# committed `.kew/epic-plan-<name>.json` files. `docs/audit/` needs a following
# path character: the site URL `/docs/audit/` is the public audit-log page
# (docs/audit.md), not the archived packs.
DELETED_TREES='docs/(superpowers|handoff|spikes|plans|issues|receipts|launch-surface)/|docs/audit/[A-Za-z0-9_.-]|evidence-packs/|\.kew/epic-plan-'
# `.kew/docs/` is the INTERNAL docs directory the skills write to (docs/skills.md)
# — a different root that happens to share the `docs/` segment and the
# `plans/`/`handoff/` leaf names. Rewriting that root away before re-applying the
# pattern is what keeps `.kew/docs/plans/` from reading as a pointer into the
# deleted public `docs/plans/`. ERE has no lookbehind and `grep -P` is not
# portable, hence the rewrite-then-rematch rather than one clever regex; dropping
# the whole line instead would blind the gate to a real hit sharing that line.
# The same rewrite spares `.kew/epic-plan-manifest.json`, the file
# `kew epic plan --from-manifest` reads at runtime, which was never archived.
# This gate and its own trigger list (the docs workflow's `paths:` filters)
# must name the prefixes they guard, so both are exempt; only the filter lines
# of the workflow are, so a real citation elsewhere in it still fails.
# shellcheck disable=SC2086
stale=$(grep -rnE "$DELETED_TREES" $SCAN $EXCL 2>/dev/null | sed -e 's|\.kew/docs/|.kew/<internal-docs>/|g' -e 's|\.kew/epic-plan-manifest\.json|.kew/<plan-manifest>|g' | grep -E "$DELETED_TREES" | grep -v -e 4a387c0b -e 1702613a | grep -v -e 'scripts/check-docs.sh' | grep -vE "^\.github/workflows/docs\.yml:[0-9]+:      - '[^']+'\$" || true)
# R15 removed the blanket `_skills`/`skills`/`docs/skills.md` exclusions: the
# shipped skills and docs/skills.md now name `.kew/docs/<kind>/`, the internal
# docs directory, so they are scanned like everything else and a regression in
# them fails this gate.
#
# No audit directory is excluded: the held packs are gone, and a basename-style
# directory exclusion matches at any depth — the old audit one silently hid
# `.claude/skills/audit/SKILL.md` (review of #1744). The deployed copies under
# `.claude/skills/` are scanned like everything else; when a shipped skill
# changes, an operator re-renders them with `kew skills update <name> --force`
# (the security guard forbids a dispatched agent from writing under `.claude/`).
if [ -n "$stale" ]; then
    echo "::error::references into a deleted internal docs tree remain:" >&2
    echo "$stale" >&2
    echo "docs gate FAILED: point them at docs/design/ or at git history (4a387c0b, 1702613a)." >&2
    exit 1
fi

echo "==> Asserting no archived design page or audit pack is still cited…"
# `docs/design/` is a live tree, so the pages the launch cut archived from it
# cannot be a DELETED_TREES prefix. This is their sibling list: the 31 design
# basenames archived at 1702613a (29 from the tip plus the two unmerged
# learnings pages). The deletion-diff basename stage below only sees them
# while the cut is the change under review; this stage keeps refusing them
# once it is on main. The design pages themselves are scanned too (mkdocs
# checks only their markdown links). Only the full-path history pointer
# `docs/design/<basename>@<sha>` is exempt, because the pointer stage can
# validate it; a bare `<basename>@<sha>` still counts as a citation here.
ARCHIVED_DESIGN_PAGES=(
    2026-03-17-autonomous-flow-roadmap-design.md
    2026-03-17-autonomous-flow-wave-1.md
    2026-06-11-pricing-freshness-design.md
    2026-06-23-provide-vs-delegate.md
    2026-06-25-kew-health-design.md
    2026-07-01-epic-dag-live-view-design.md
    2026-07-01-epic-dag-phase-2-driver-design.md
    2026-07-17-linear-drive-slice-a.md
    2026-07-23-context-memory-layers-design.md
    2026-07-26-containment-epic.md
    2026-07-27-codex-runner-design.md
    2026-07-27-codex-runner.md
    2026-07-28-codex-runner-implementation.md
    2026-08-01-switchyard-compatibility.md
    2026-08-03-switchyard-passthrough-uat.md
    2026-08-06-pre-release-program.md
    2026-08-07-pass-3-simplicity-brief.md
    2026-08-16-publication-dogfood.md
    2026-08-17-product-coherence-release.md
    2026-08-18-product-coherence-implementation.md
    2026-08-24-oss-cut-epic.md
    2026-08-24-oss-cut-list.md
    2026-08-24-production-certified-program.md
    2026-08-25-publication-broker-open-vs-pro-boundary.md
    2026-08-27-github-native-linkage.md
    2026-09-03-s1-r2-canonical-publication.md
    2026-09-20-go-live-drive-learnings.md
    2026-09-20-sandbox-unit-design.md
    launch-runbook.md
    release-uat.md
    uat-plan.md
)
# Strip full-path history pointers for $1 from stdin, then keep the lines
# that still cite its basename (rewrite-then-rematch, as above): a pointer
# never hides a bare citation sharing its line.
drop_pointers() {
    local esc
    # Repository paths hold only [A-Za-z0-9._/-]: '.' is the one ERE
    # metacharacter to escape, and '#' is a safe delimiter.
    esc=$(printf '%s' "$1" | sed 's#\.#\\.#g')
    # Only the two validated mirrors are pointers; any other hex suffix is a
    # citation like any other.
    sed -E "s#${esc}@(4a387c0b|1702613a)[0-9a-f]*##g" | grep -F -- "${1##*/}" || true
}

# The audit packs archived at 1702613a, refused the same way (#1993):
# DELETED_TREES only matches the full `docs/audit/<pack>` path, so a pack
# cited by its site-relative path (`audit/<pack>.md`) or by its bare
# basename slipped through. Only `docs/audit/<pack>@<sha>` is exempt.
ARCHIVED_AUDIT_PACKS=(
    2026-03-25-codebase-audit.md
    2026-07-25-openrouter-parity-o1.md
    2026-08-03-adaptive-window-2.md
    2026-08-03-adaptive-window-e.md
    2026-08-04-linear-epic-driver-uat.md
    2026-08-06-feature-census.md
    2026-08-06-reference-gap-audit.md
    2026-08-29-agent-image-cve-triage.md
    2026-09-13-linear-lifecycle-supplement.md
)
bad_design=0
for archived in "${ARCHIVED_AUDIT_PACKS[@]}"; do
    # shellcheck disable=SC2086
    hits=$(grep -rnF $GENERATED -I --exclude-dir=__pycache__ --exclude-dir=.venv --exclude-dir=site --exclude-dir=node_modules --exclude-dir=worktrees -- "$archived" $SCAN 2>/dev/null \
        | drop_pointers "docs/audit/${archived}" | grep -v '^scripts/check-docs.sh:' || true)
    if [ -n "$hits" ]; then
        echo "::error::archived audit pack '${archived}' is still cited:" >&2
        echo "$hits" >&2
        bad_design=1
    fi
done
for archived in "${ARCHIVED_DESIGN_PAGES[@]}"; do
    # shellcheck disable=SC2086
    # $GENERATED goes before `--`: everything after it is an operand, so the
    # flags trailing the file list here have never been read as options. Left
    # as they were — making them effective would change what this stage scans,
    # which is not this change's business — but the exclusion has to work.
    hits=$(grep -rnF $GENERATED -- "$archived" $SCAN -I --exclude-dir=__pycache__ --exclude-dir=.venv --exclude-dir=site --exclude-dir=node_modules --exclude-dir=worktrees 2>/dev/null \
        | drop_pointers "docs/design/${archived}" | grep -v '^scripts/check-docs.sh:' || true)
    if [ -n "$hits" ]; then
        echo "::error::archived design page '${archived}' is still cited:" >&2
        echo "$hits" >&2
        bad_design=1
    fi
done
if [ "$bad_design" -ne 0 ]; then
    echo "docs gate FAILED: point design and audit references at a public page or at git history (1702613a)." >&2
    exit 1
fi

echo "==> Asserting deleted docs basenames have no tree-wide citations…"
# A path grep misses references that use only a basename. Compare the current
# checkout, including staged and unstaged changes, with the PR base. On main,
# where origin/main equals HEAD, compare with HEAD^ so push-triggered docs gates
# still inspect the change that just landed. Tests can pin an exact base through
# CHECK_DOCS_DIFF_BASE.
diff_base="${CHECK_DOCS_DIFF_BASE:-}"
if [ -z "$diff_base" ] && git cat-file -e 'origin/main^{commit}' 2>/dev/null; then
    diff_base=$(git merge-base HEAD origin/main)
    if [ "$diff_base" = "$(git rev-parse HEAD)" ] && \
        [ "${GITHUB_EVENT_NAME:-}" = "push" ]; then
        diff_base="HEAD^"
    fi
fi
if [ -z "$diff_base" ] && git rev-parse --verify HEAD^ >/dev/null 2>&1; then
    diff_base="HEAD^"
fi
# A root commit has no HEAD^. The public repository starts as one commit (a
# `git archive` snapshot), so its first push to main would fail on the base
# below. Compare a root commit with the empty tree instead: every file is
# added and none is deleted. A shallow clone's oldest commit also lacks HEAD^
# without being a root, so a shallow clone keeps HEAD^ and still fails loud.
empty_tree=$(git hash-object -t tree /dev/null)
if { [ "$diff_base" = "HEAD^" ] || [ -z "$diff_base" ]; } && \
    [ "$(git rev-parse --is-shallow-repository)" = "false" ] && \
    [ "$(git rev-list --count HEAD)" = "1" ]; then
    diff_base=$empty_tree
    echo "    (HEAD is a root commit: comparing with the empty tree)"
fi
# A base that does not resolve would make the basename stage pass vacuously
# (the process substitution's failure is invisible to set -e). Fail instead.
if [ -n "$diff_base" ] && [ "$diff_base" != "$empty_tree" ] && \
    ! git rev-parse --verify --quiet "${diff_base}^{commit}" >/dev/null; then
    echo "::error::docs gate: diff base '${diff_base}' does not resolve to a commit" >&2
    exit 1
fi

bad_basename=0
if [ -n "$diff_base" ]; then
    BASENAME_SCAN=(README.md mkdocs.yml docs kew/src kew/tests kew/scripts AGENTS.md CLAUDE.md .claude scripts .github www kew.toml create-kew)
    while IFS= read -r -d '' deleted_path; do
        basename=${deleted_path##*/}
        # Search text files only. The deleted path itself is absent, so every
        # hit is a surviving citation, including on the published design
        # pages. Only the full-path pointer `<deleted path>@<sha>` is exempt
        # (the pointer stage validates it), and this gate's own archived-page
        # list is the refusal, not a citation.
        hits=$(grep -rIFn \
            --exclude-dir=__pycache__ --exclude-dir=.venv \
            --exclude-dir=site --exclude-dir=node_modules "$GENERATED" \
            -- "$basename" "${BASENAME_SCAN[@]}" 2>/dev/null \
            | drop_pointers "$deleted_path" | grep -v '^scripts/check-docs.sh:' || true)
        if [ -n "$hits" ]; then
            echo "::error::deleted docs basename '${basename}' is still cited:" >&2
            echo "$hits" >&2
            bad_basename=1
        fi
    done < <(git diff --no-renames --diff-filter=D --name-only -z "$diff_base" -- docs)
fi
if [ "$bad_basename" -ne 0 ]; then
    echo "docs gate FAILED: remove or update citations to deleted docs files." >&2
    exit 1
fi

echo "==> Asserting every history pointer resolves at its mirror commit and not at the tip…"
# A `path@<mirror>` token must name a file that existed at the mirror commit and
# is gone from the tip — otherwise it is either a typo or a pointer that should
# be a live path. 4a387c0b is the E01 mirror, 1702613a the launch-cut mirror.
# Needs the commit locally; a shallow clone (CI) skips it and says so,
# verify.sh runs it on the full history.
badptr=0
# A pointer must carry the full repository path, or no stage could check it:
# a bare `<file>.<ext>@<sha>` (typo'd or not) fails here, with or without
# the mirror commit present.
# shellcheck disable=SC2086
bare=$(grep -rnoE '(^|[^/A-Za-z0-9_.-])[A-Za-z0-9_.-]+\.[A-Za-z0-9]+@(4a387c0b|1702613a)' $SCAN -I --exclude-dir=__pycache__ --exclude-dir=.venv --exclude-dir=site --exclude-dir=node_modules $GENERATED 2>/dev/null | grep -v '^scripts/check-docs.sh:' || true)
if [ -n "$bare" ]; then
    echo "::error::history pointers must name the full repository path (docs/..., kew/docs/..., evidence-packs/..., .kew/epic-plan-...):" >&2
    echo "$bare" >&2
    badptr=1
fi
for mirror in 4a387c0b 1702613a; do
    if ! git cat-file -e "${mirror}^{commit}" 2>/dev/null; then
        echo "    (${mirror} not present in this clone — history-pointer check skipped)"
        continue
    fi
    # shellcheck disable=SC2086
    for tok in $(grep -rhoE "(\.kew/epic-plan-|kew/docs/|docs/|evidence-packs/)[A-Za-z0-9_./-]+@${mirror}" $SCAN -I --exclude-dir=__pycache__ --exclude-dir=.venv --exclude-dir=site --exclude-dir=node_modules $GENERATED 2>/dev/null | sort -u); do
        p="${tok%@"${mirror}"}"
        if ! git cat-file -e "${mirror}:${p}" 2>/dev/null; then
            echo "::error::${tok}: no such file at ${mirror}" >&2; badptr=1
        elif [ -e "$p" ]; then
            echo "::error::${tok}: the file exists at the tip — use the live path" >&2; badptr=1
        fi
    done
done
if [ "$badptr" -ne 0 ]; then
    echo "docs gate FAILED: a history pointer does not resolve." >&2
    exit 1
fi

echo "==> Asserting mkdocs.yml turns on no off-origin integration…"
# Material builds these URLs inside JavaScript, never in a src the built-page
# stage below could read: `repo_url` makes every page's source widget call the
# GitHub API, and `extra.analytics` makes an inline script load Google Tag
# Manager. Read through mkdocs' own loader, so any YAML spelling counts.
integrations=$(uvx --from mkdocs=="${MKDOCS_VERSION}" --with mkdocs-material=="${MKDOCS_MATERIAL_VERSION}" python -I -B -c '
import sys
from mkdocs.config import load_config
config = load_config(sys.argv[1])
if config.repo_url:
    print(f"mkdocs.yml sets repo_url ({config.repo_url}): the source widget calls the GitHub API")
if (config.extra or {}).get("analytics"):
    print("mkdocs.yml sets extra.analytics: the analytics script loads from Google Tag Manager")
' mkdocs.yml)
if [ -n "$integrations" ]; then
    echo "::error::mkdocs.yml turns on an off-origin integration:" >&2
    echo "$integrations" >&2
    echo "docs gate FAILED: drop the setting (the site CSP is default-src 'self')." >&2
    exit 1
fi

echo "==> Building docs (mkdocs ${MKDOCS_VERSION} / material ${MKDOCS_MATERIAL_VERSION})…"
# mkdocs 1.6 reports a link from a built page to an EXCLUDED page, and an
# absolute link, at INFO level only — `--strict` never fails on them
# (mkdocs/structure/pages.py caps that message at INFO). Capture the build log
# and fail on those two messages ourselves, so an excluded internal page can
# never become a dangling link on a public page.
build_log=$(mktemp)
trap 'rm -f "$build_log"' EXIT
# `-d site`: every stage below reads `site/`, whatever `site_dir` says.
uvx --from mkdocs=="${MKDOCS_VERSION}" --with mkdocs-material=="${MKDOCS_MATERIAL_VERSION}" mkdocs build --strict -d site 2>&1 | tee "$build_log"
excluded_links=$(grep -E "which is excluded from the built site|contains an absolute link" "$build_log" || true)
rm -f "$build_log"
if [ -n "$excluded_links" ]; then
    echo "::error::a built page links to an excluded page or uses an absolute link:" >&2
    echo "$excluded_links" >&2
    echo "docs gate FAILED: link to a built page, or drop the link." >&2
    exit 1
fi
# The stages below scan `site/`; with nothing there, each would pass.
if [ ! -f site/index.html ]; then
    echo "::error::the build left no site/index.html — the built-site stages would check nothing" >&2
    echo "docs gate FAILED: the docs did not build into site/." >&2
    exit 1
fi

echo "==> Asserting no built page carries a raw href into an excluded path…"
# mkdocs validates markdown links only; a raw link into a held source is
# published verbatim as a dangling link. Scan built HTML for held prefixes;
# `audit/2026-` and `evidence-packs/` name material archived at 1702613a.
raw_hrefs=$(grep -rhoE 'href="[^"]*"' site 2>/dev/null | grep -E 'href="(\.\./)*(docs/)?(vhs|brand-guidelines|audit/2026-)|evidence-packs/' || true)
if [ -n "$raw_hrefs" ]; then
    echo "::error::built pages carry hrefs into an excluded path:" >&2
    echo "$raw_hrefs" | sort -u >&2
    echo "docs gate FAILED: drop the raw link or point it at a built page." >&2
    exit 1
fi

echo "==> Asserting no built page loads an off-origin resource…"
# The site CSP (www/Caddyfile) is `default-src 'self'`, so anything a page
# fetches from another origin is a console error and a hole in the
# "self-contained" claim (#1168): Material's Google Fonts link and its GitHub
# source widget were both. Fail on any built element (but a hyperlink) that
# names an absolute, protocol-relative or `http(s):` URL, on such a URL in CSS
# (stylesheets, `<style>` and `style=`) or in an inline script, and on markup
# that makes Material's bundle fetch one. A `<link>` whose every rel token is
# a hyperlink (`canonical`, `alternate`, `next`, `prev`, `me`, `author`,
# `license`, `help`) names a URL without fetching it; `rel="alternate
# stylesheet"` fetches.
# Every built script and SVG is scanned too: an `extra_javascript` file or an
# SVG opened as a document fetches what it names. A script is exempt only when
# it is byte for byte the file the pinned mkdocs-material package ships at the
# same path (its bundle, search worker and lunr files): an author's file in
# `assets/javascripts/`, or one overriding Material's, is scanned. An `on*` or
# `srcdoc` attribute is refused outright (Material's templates emit none): it
# runs script or markup the scan cannot read.
# Material's JS bundle itself always carries three third-party URLs and
# fetches each only on a trigger. The source widget calls
# api.github.com for `data-md-component="source"` markup (from `repo_url`,
# refused above, or raw HTML, refused here); mermaid loads from unpkg.com for
# any `pre.mermaid` (refused here: any element with the `mermaid` class); and a
# ResizeObserver polyfill loads from unpkg.com only in a browser with no native
# ResizeObserver, which no page can turn off (the CSP blocks it there).
# Parsed, not grepped: raw HTML in a page reaches the build as written — any
# case, any quoting, a tag split across lines, several srcset candidates.
# The scan runs in the build's own pinned environment, so it compares built
# scripts with the very mkdocs-material package that built them.
off_origin=$(uvx --from mkdocs=="${MKDOCS_VERSION}" --with mkdocs-material=="${MKDOCS_MATERIAL_VERSION}" python -I -B - site <<'PY'
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

import material

# The URL parser drops tab, LF and CR anywhere in a URL, and C0 controls and
# spaces at either end, so `ht&#9;tps://` is `https://` to the browser.
DROPPED = re.compile(r"[\t\n\r]")
EDGES = "".join(chr(code) for code in range(0x21))
# `scheme://`, `//`, or any http(s) URL: `http:host/x` on an https page is
# another origin whether or not it has slashes.
OFF_ORIGIN = re.compile(r"(?i)^(?:https?:|[a-z][a-z0-9+.-]*:[/\\]{2}|[/\\]{2})")
NAVIGATIONAL = {"canonical", "alternate", "next", "prev", "me", "author",
                "license", "help"}
HYPERLINKS = {"a", "area"}
URL_ATTRS = ("href", "src", "data", "poster", "xlink:href")
SRCSET_ATTRS = ("srcset", "imagesrcset")
CSS_URL = re.compile(r"""(?i)url\(\s*(?:"([^"]*)"|'([^']*)'|([^)\s]*))""")
CSS_IMPORT = re.compile(r"""(?i)@import\s*(?:"([^"]*)"|'([^']*)')""")
CSS_IMAGE_SET = re.compile(r"""(?i)image-set\(((?:[^()]|\([^()]*\))*)\)""")
CSS_STRING = re.compile(r""""([^"]*)"|'([^']*)'""")
SCRIPT_URL = re.compile(r"""(?i)https?:[^\s"'`<>)]*""")


def clean(url):
    return DROPPED.sub("", url).strip(EDGES)


def off_origin_css(text):
    found = [m.groups() for m in CSS_URL.finditer(text)]
    found += [m.groups() for m in CSS_IMPORT.finditer(text)]
    for image_set in CSS_IMAGE_SET.finditer(text):
        found += [m.groups() for m in CSS_STRING.finditer(image_set.group(1))]
    urls = [clean("".join(g or "" for g in groups)) for groups in found]
    return [url for url in urls if OFF_ORIGIN.match(url)]


class Page(HTMLParser):
    def __init__(self, page):
        super().__init__(convert_charrefs=True)
        self.page = page
        self.inline = None
        self.text = []

    def report(self, what):
        print(f"{self.page}: {what}")

    def handle_starttag(self, tag, attrs):
        values = {name: (value or "") for name, value in attrs}
        self.check(tag, values)
        if tag in ("script", "style") and not values.get("src"):
            self.inline, self.text = tag, []

    def handle_startendtag(self, tag, attrs):
        self.check(tag, {name: (value or "") for name, value in attrs})

    def check(self, tag, values):
        for name in values:
            if name.startswith("on") or name == "srcdoc":
                self.report(f"<{tag} {name}> runs script or markup the scan cannot read")
        if "mermaid" in values.get("class", "").split():
            self.report(f"<{tag} class=mermaid> makes Material load mermaid from unpkg.com")
        if values.get("data-md-component") == "source":
            self.report(f"<{tag}> is Material's source widget, which calls the GitHub API")
        if tag == "script" and values.get("id") == "__analytics":
            self.report("<script id=__analytics> is Material's Google Tag Manager loader")
        for url in off_origin_css(values.get("style", "")):
            self.report(f"<{tag} style> {url}")
        rel = set(values.get("rel", "").lower().split())
        if tag in HYPERLINKS or (tag == "link" and rel and rel <= NAVIGATIONAL):
            return
        urls = [values.get(name, "") for name in URL_ATTRS]
        for name in SRCSET_ATTRS:
            urls += [c.split()[0] for c in values.get(name, "").split(",") if c.split()]
        for url in map(clean, urls):
            if OFF_ORIGIN.match(url):
                self.report(f"<{tag}> {url}")

    def handle_data(self, data):
        if self.inline:
            self.text.append(data)

    def handle_endtag(self, tag):
        if tag != self.inline:
            return
        text = "".join(self.text)
        if tag == "style":
            urls = off_origin_css(text)
        else:
            urls = SCRIPT_URL.findall(DROPPED.sub("", text))
        for url in urls:
            self.report(f"<{tag}> (inline) {url}")
        self.inline = None


def read(path):
    return path.read_text(encoding="utf-8", errors="replace")


def materials_own(script):
    shipped = shipped_root / script.relative_to(site)
    return shipped.is_file() and shipped.read_bytes() == script.read_bytes()


site = Path(sys.argv[1])
shipped_root = Path(material.__file__).parent / "templates"
if not (shipped_root / "assets" / "javascripts").is_dir():
    sys.exit(
        f"mkdocs-material's shipped scripts not found at {shipped_root}: the script"
        " scan cannot tell Material's own files from an author's"
    )
pages = sorted(site.rglob("*.html"))
if not pages:
    sys.exit(f"no built page under {site}/ to check")
for page in pages:
    Page(page).feed(read(page))
# An SVG opened as a document (`<object>`, `<embed>`, a visit) loads what it
# names, like a page. An internal DTD entity can spell a URL the attribute
# scan never sees, so an SVG may declare none.
for drawing in sorted(site.rglob("*.svg")):
    Page(drawing).feed(read(drawing))
    if re.search(r"(?i)<!ENTITY", read(drawing)):
        print(f"{drawing}: <!ENTITY> spells text the scan cannot read")
for sheet in sorted(site.rglob("*.css")):
    for url in off_origin_css(read(sheet)):
        print(f"{sheet}: url {url}")
scripts = sorted([*site.rglob("*.js"), *site.rglob("*.mjs")])
for script in scripts:
    if materials_own(script):
        continue
    for url in SCRIPT_URL.findall(DROPPED.sub("", read(script))):
        print(f"{script}: script {url}")
PY
)
if [ -n "$off_origin" ]; then
    echo "::error::built pages load an off-origin resource:" >&2
    echo "$off_origin" | sort -u >&2
    echo "docs gate FAILED: serve the resource from the site itself (the CSP is default-src 'self')." >&2
    exit 1
fi

echo "==> Asserting internal working docs are excluded from the built site…"
leaked=0
# Each pattern names a held source that still exists in the tree and that
# mkdocs.yml's `exclude_docs` must keep out of the build. The audit packs are
# no longer held here: the first stage refuses `docs/audit/` outright.
for p in vhs/ brand-guidelines; do
    if find site -path "*${p}*" | grep -q .; then
        echo "::error::internal path '${p}' present in the built site — exclude_docs regressed" >&2
        leaked=1
    fi
done

if [ "$leaked" -ne 0 ]; then
    echo "docs gate FAILED: internal material would be published." >&2
    exit 1
fi

echo "✓ no deleted internal tree returned; no stale references or basenames; strict docs build clean; no internal paths in the built site; no off-origin resource."
