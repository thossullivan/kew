"""Group A9 exit gate — the work-item id sweep must not regress.

Slice A′ de-ints work-item currency so a non-numeric Linear id (e.g.
``SFD-12``) survives dispatch → PR → merge → close. A bare ``int()`` coercion
on an item id raises ``ValueError`` on a Linear id, so the sweep forbids it
OUTSIDE these surfaces:

  * the GitHub-native tracker-role modules (``selector.py`` / ``labels.py`` /
    ``dependencies.py`` / ``facts.py``) — they stay int internally; Phase B
    extracts them (plan Conventions);
  * the ``graph/driver/`` subset L3 converted, which keeps GitHub-numeric
    coercions behind explicit ``GITHUB_ID_RE`` / ``provider == "github"``
    guards (plan Group L3);
  * a small, enumerated set of already-swept call sites where the coercion
    is provably reachable only for a GitHub-numeric value — each pinned by
    its exact source line so editing it re-triggers review;
  * the consumer surfaces explicitly DEFERRED to the Task-8 sweep (``service/``
    — verbs/dashboard/epics), tracked but not yet converted (plan Sequencing
    "Deferred wave").

Any NEW unguarded coercion — in any other module — trips this gate, forcing a
guard or a documented exemption below.
"""

from __future__ import annotations

import pathlib
import re

import pytest

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "kew"

# The amended sweep pattern (plan §Group A9): the reference plan's
# ``(issue_num|item_id|num)`` widened with ``nid`` and ``number``.
_PATTERN = re.compile(r"int\((issue_num|item_id|num|nid|number)\b")

# GitHub-native tracker-role modules — keep int internals (plan Conventions).
_ALLOWED_NAMES = {"selector.py", "labels.py", "dependencies.py", "facts.py"}

# Consumer surfaces deferred to the Task-8 sweep (plan "Deferred wave").
_DEFERRED_PREFIXES = ("service/",)

# Guarded coercions the broad regex still flags: each is reachable only for a
# GitHub-numeric value. Pinned by exact stripped source line (tight — a new,
# differently-shaped coercion in the same file still trips the gate; a moved
# or deleted line trips ``test_guarded_exemptions_are_live`` below).
_GUARDED_LINES = {
    # run_store.py — inline GITHUB_ID_RE guard (0-sentinel for Linear rows).
    "return int(item_id) if GITHUB_ID_RE.fullmatch(item_id) else 0",
    # review_audit.py — same GITHUB_ID_RE guard emitting the review-verdict
    # audit envelope (0-sentinel for Linear rows), mirroring the driver sites.
    "issue_num=int(item_id) if GITHUB_ID_RE.fullmatch(item_id) else 0,",
    # ids.py — numeric-only branch of the sort key (GITHUB_ID_RE guard above).
    "return (0, int(item_id))",
    # cli/epic.py — inside the `if provider == "github":` block (epic unhold label
    # strip). Routed through `_c.` so the moved command stays patchable via kew.cli.
    "_c.clear_pending_review_label(int(item_id), config)",
    # cli/epic.py — reached only after the `provider != "github"` early-return
    # (dispatch of a non-GitHub node is rejected before this line). Routed via `_c.`.
    "issue_dict = _c.select_next_issue(config=config, force_issue=int(item_id))",
    # scheduler.py — GitHub single-dispatch show-prompt; `num` is the gh issue
    # number (item_id == str(num) on the preceding line).
    "issue_num=int(num),",
    # graph/adapters/linear_facts.py — a GitHub PR number (PRs are always
    # GitHub-native ints even when the tracker is Linear).
    "pr_number = int(number)",
}


def _swept_modules() -> list[pathlib.Path]:
    """Every ``src/kew`` module in the sweep's scope (allowlist + driver
    subset + deferred surfaces excluded)."""
    out = []
    for p in sorted(SRC.rglob("*.py")):
        rel = p.relative_to(SRC).as_posix()
        if p.name in _ALLOWED_NAMES:
            continue
        if "driver" in rel:  # L3's converted graph/driver/ subset
            continue
        if rel.startswith(_DEFERRED_PREFIXES):
            continue
        out.append(p)
    return out


@pytest.mark.policy
def test_no_unguarded_int_coercion_on_item_ids() -> None:
    offenders = [
        f"{p.relative_to(SRC).as_posix()}:{i}: {line.strip()}"
        for p in _swept_modules()
        for i, line in enumerate(p.read_text().splitlines(), 1)
        if _PATTERN.search(line) and line.strip() not in _GUARDED_LINES
    ]
    assert offenders == [], offenders


@pytest.mark.policy
def test_guarded_exemptions_are_live() -> None:
    """Every documented exemption must still exist verbatim in the tree.

    A stale entry (code moved, or the deferred sweep finally landed) forces
    the list to be trimmed instead of silently masking a future regression.
    """
    present = {
        line.strip()
        for p in SRC.rglob("*.py")
        for line in p.read_text().splitlines()
        if line.strip() in _GUARDED_LINES
    }
    assert _GUARDED_LINES - present == set(), _GUARDED_LINES - present


def test_gate_has_teeth() -> None:
    """Positive control: the pattern really would flag an unguarded coercion
    (so a broken regex can't make the gate vacuously green)."""
    assert _PATTERN.search("x = int(item_id)")
    assert _PATTERN.search("        issue_num=int(num),")
    assert _PATTERN.search("int(nid)")
    assert _PATTERN.search("int(number)")
    # Coercions on unrelated names — must not match.
    assert not _PATTERN.search("int(total_cost)")
    assert not _PATTERN.search("str(item_id)")
