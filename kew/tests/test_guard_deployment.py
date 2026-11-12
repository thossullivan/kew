"""Regression pin: the committed security guard must not drift from its template.

Finding N2 (delta audit 2026-07-22): the committed
``.claude/hooks/security-guard.sh`` had gone stale — it was a *pre-hardening*
copy while the source of truth (``kew/src/kew/_templates/security-guard.sh``)
had been hardened. Nothing caught the drift because nothing compared the two.
Fixed directly in ``8a459252``; this test makes that drift class impossible to
reintroduce silently.

The committed guard is the raw template plus exactly ONE documented addition:
the Claude Code auto-memory allowlist block (so agents working in *this* repo
may write project memory notes under ``~/.claude/projects/*/memory/`` while
every other path under ``.claude/`` stays protected). The template itself has
no such block because kew ships it to *user* projects, which have no such
allowance. At generate-time kew also injects the operator's custom
``# EXTRA_*_PATTERNS`` — but the committed copy keeps those literal markers, so
comparing against the template text (not a rendered instance) is correct here.

This pin asserts the delta is EXACTLY that block: nothing removed, and the only
lines added are the documented allowlist. Any other divergence — a hardening
that reached the template but not the committed hook, or vice versa — fails
here. Changing the allowlist is then a conscious single-point update to
``AUTO_MEMORY_ALLOWLIST_BLOCK`` below.
"""

from __future__ import annotations

import difflib
from pathlib import Path

KEW_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = KEW_DIR.parent
COMMITTED_GUARD = REPO_ROOT / ".claude" / "hooks" / "security-guard.sh"
TEMPLATE_GUARD = KEW_DIR / "src" / "kew" / "_templates" / "security-guard.sh"

# The one documented, deliberate addition the committed guard carries over the
# template. Byte-for-byte (indentation and the em-dash included) — this IS the
# spec, so an intentional change is a single conscious edit here.
AUTO_MEMORY_ALLOWLIST_BLOCK = [
    "        # Allow Claude Code auto-memory (project memory notes) — nothing else",
    "        # under .claude/ is writable (hooks and settings stay protected).",
    '        case "$CANON_PATH" in',
    '            "$HOME"/.claude/projects/*/memory/*)',
    "                exit 0",
    "                ;;",
    "        esac",
]


def _delta(template: list[str], committed: list[str]) -> tuple[list[str], list[str]]:
    """Return (removed-from-template, added-in-committed) line lists."""
    matcher = difflib.SequenceMatcher(a=template, b=committed, autojunk=False)
    removed: list[str] = []
    added: list[str] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("delete", "replace"):
            removed.extend(template[i1:i2])
        if tag in ("insert", "replace"):
            added.extend(committed[j1:j2])
    return removed, added


def test_guard_files_exist() -> None:
    """Sanity: both the committed hook and its template are present."""
    assert COMMITTED_GUARD.exists(), f"missing committed guard: {COMMITTED_GUARD}"
    assert TEMPLATE_GUARD.exists(), f"missing guard template: {TEMPLATE_GUARD}"


def test_committed_guard_matches_template_modulo_auto_memory_block() -> None:
    """Committed guard == template + EXACTLY the auto-memory allowlist block.

    Guards finding N2: a stale committed copy diverging from the hardened
    template. The only permitted difference is the documented allowlist.
    """
    template = TEMPLATE_GUARD.read_text().splitlines()
    committed = COMMITTED_GUARD.read_text().splitlines()

    removed, added = _delta(template, committed)

    assert removed == [], (
        "The committed .claude/hooks/security-guard.sh is MISSING lines that "
        "the template has — a hardening reached the template but not the "
        "committed hook (the N2 drift class). Regenerate the committed guard "
        "from the template (keeping the auto-memory block). Lines dropped:\n"
        + "\n".join(f"  - {line}" for line in removed)
    )
    assert added == AUTO_MEMORY_ALLOWLIST_BLOCK, (
        "The committed guard diverges from the template by something other "
        "than the documented auto-memory allowlist block. Either an unexpected "
        "change slipped into the committed hook, or the allowlist itself "
        "changed — if intentional, update AUTO_MEMORY_ALLOWLIST_BLOCK. "
        "Actual added lines:\n" + "\n".join(f"  + {line}" for line in added)
    )
