"""Publish the adversarial-review verdict as an edit-in-place PR comment (#1046).

One machine comment per PR per verdict: kew searches for its sentinel comment,
patches it when found, and creates it when not.  A second verdict round updates
the same comment instead of appending — the PR thread stays clean.

GitHub comments are DISPLAY only — kew parses only host-stored sidecar
artifacts (F10 / spec §Reviewer contract).  The comment cites the sidecar
digest so a reader can locate the host-stored copy.

Best-effort: any ``gh`` or GitHub failure is logged and swallowed so a comment
hiccup never crashes the driver tick (same fail-safe contract as review_status).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from kew import subprocess_utils
from kew.publication_credentials import BRANCH_RUNS_CAP, BRANCH_RUNS_WARNING
from kew.review_contract import (
    CoverageEntry,
    CriterionResult,
    Finding,
    NotCheckedEntry,
)
from kew.review_criteria import UnjudgedCriteria

_logger = logging.getLogger(__name__)

NO_CRITERIA_NOTE = "no acceptance criteria recorded for this item"
"""What the comment says when the item has no recorded plan (#2291).

Stated rather than omitted: an absent criteria table is indistinguishable from
a reviewer that ignored the criteria, and the two mean opposite things to
whoever reads the verdict before approving the merge.
"""

UNJUDGED_CRITERIA_NOTE = (
    "⚠ **{count} recorded acceptance criteri{plural} for this item "
    "(plan `{plan_sha}`) {verb} NOT judged in this round** — {reason}. This "
    "verdict covers the diff only; it says nothing about whether the item does "
    "what the issue asked for."
)
"""What the comment says when the LEDGER records criteria this round skipped.

The opposite of :data:`NO_CRITERIA_NOTE` and never interchangeable with it: one
says the issue asked for nothing checkable, the other says kew holds a contract
it failed to put in front of the reviewer. Printing the first for the second is
how a host-side gap gets approved as a met plan.
"""

OMITTED_CRITERIA_NOTE = (
    "⚠ **{count} further criteri{plural} of plan `{plan_sha}` {verb} not judged** "
    "— {ids} did not fit the reviewer prompt, so the table above is not the whole "
    "plan."
)
"""What the comment says when the plan section could not carry every criterion.

The rows are a judged SUBSET; without this the table reads as the full plan
(#2291).
"""

GATE_SKIPPED_NOTE = (
    "> ⚠ **The pre-review gate did not check this head** — {reason}. The merge "
    "gate's static checks were not run on it before this review."
)
"""What the comment says when the pre-review gate skipped the reviewed head
(#3280): a refused launch, a timeout, a tool the check image lacks, a red base.

Stated rather than omitted, as :data:`UNJUDGED_CRITERIA_NOTE` is: a verdict
with no gate line otherwise reads as a head the gate passed. A head the gate
passed, or a repository with no gate configured, gets no line.
"""

_CRITERION_ICONS = {"pass": "✓", "fail": "✗", "unverifiable": "?"}  # nosec B105 — criterion outcomes, not a password

# HTML sentinel embedded in every kew verdict comment so subsequent rounds can
# find and patch the same comment rather than appending.
_COMMENT_SENTINEL = "<!-- kew-review-verdict -->"

_VERDICT_READY = "READY"
_VERDICT_NEEDS_CHANGES = "NEEDS_CHANGES"
_VERDICT_NEEDS_FULL = "NEEDS_FULL"

_GATE_PATHS_NAMED = 20
"""The most touched gate paths the second-review note names (#3422)."""

_GATE_PATH_CHARS = 200
"""The most characters of one named gate path: a file name is the PR's to
choose, and this note sits in the part of the comment that is never cut."""

_BACKTICK_RUN = re.compile(r"`+")

COMMENT_BODY_CAP = 60_000
"""The most UTF-8 bytes a verdict comment body carries. GitHub refuses a body
over 65,536 characters, and a refused PATCH is logged and swallowed — the PR
would keep the PREVIOUS round's comment under this round's status. Bytes are
never fewer than characters, so the cap holds however GitHub counts."""

_COMMENT_LINE_CAP = 4_000
"""The most characters one reviewer-derived line keeps, so one oversized
finding cannot push every row after it out of the comment."""

_TRUNCATED_NOTE = (
    "> ⚠ **comment truncated** — {omitted} more line{plural} did not fit "
    "GitHub's comment size limit; the full verdict is in the host-stored "
    "sidecar."
)


def _append_acceptance_status(
    lines: list[str],
    acceptance_evidence: list[dict[str, str]],
    acceptance_executed: bool | None,
    acceptance_reason: str | None,
) -> None:
    """Render executed acceptance evidence or an explicit unavailable result."""
    if acceptance_evidence:
        lines.append("")
        lines.append("<details><summary>Acceptance evidence</summary>")
        lines.append("")
        for ev in acceptance_evidence:
            cmd = ev.get("command", "")
            digest = ev.get("output_digest", "")
            digest_short = digest[:20] + "…" if len(digest) > 20 else digest
            lines.append(f"- `{cmd}` → `{digest_short}`")
        lines.append("")
        lines.append("</details>")
    elif acceptance_executed is False:
        lines.append("")
        lines.append("**Acceptance execution: not executed** — degraded confidence")
        if acceptance_reason:
            lines.append(f"> {acceptance_reason}")


COVERAGE_LIST_CAP = 30
"""The most coverage or not-checked entries the comment lists; the rest are
counted as ``(+N more)`` — the full lists stay in the stored sidecar (#3419)."""


def _more_line(lines: list[str], total: int) -> None:
    if total > COVERAGE_LIST_CAP:
        lines.append(f"- (+{total - COVERAGE_LIST_CAP} more)")


def _inline(text: str) -> str:
    """Reviewer-authored text as ONE line of a list item, outside any table.

    A newline would end the item, so whitespace collapses. A pipe is left as
    it is: outside a table it is plain text, and ``\\|`` would show its
    backslash.
    """
    return " ".join(text.split()) or "—"


def _code(text: str) -> str:
    """Reviewer-authored text as ONE inline code span, outside any table.

    The fence is one backtick longer than the longest backtick run inside, so
    a command quoting ``date`` in backticks cannot close the span early; a
    space pads an edge backtick, and Markdown strips it again.
    """
    inner = _inline(text)
    longest = max((len(run) for run in _BACKTICK_RUN.findall(inner)), default=0)
    fence = "`" * (longest + 1)
    pad = " " if inner.startswith("`") or inner.endswith("`") else ""
    return f"{fence}{pad}{inner}{pad}{fence}"


def _coverage_line(entry: CoverageEntry) -> str:
    line = f"- {_code(entry.target)} — {entry.how}: {_inline(entry.tried)}"
    if entry.evidence_command:
        line += f" ({_code(entry.evidence_command)})"
    return line


def _dropped_line(dropped: int) -> str:
    noun = "entry" if dropped == 1 else "entries"
    return (
        f"- ⚠ {dropped} malformed not-checked {noun} dropped (no check, reason "
        "or kind) — the stored sidecar has them"
    )


def _append_coverage(
    lines: list[str],
    verdict: str,
    coverage: list[CoverageEntry],
    not_checked: list[NotCheckedEntry] | None,
    not_checked_dropped: int = 0,
) -> None:
    """What the reviewer checked and cleared, and what it could not check (#3419).

    ``not_checked`` is ``None`` when the round was not held to the coverage
    contract (an older prompt), and then neither list is shown; an empty list
    is shown as such, so a reader sees the reviewer claimed nothing unchecked
    — unless entries were dropped as malformed, which is said instead. Every
    entry is reviewer prose and goes through ``_inline`` or ``_code``.
    """
    if verdict == _VERDICT_READY and coverage:
        lines.append("")
        lines.append("### Checked and cleared")
        lines.append("")
        lines.extend(_coverage_line(e) for e in coverage[:COVERAGE_LIST_CAP])
        _more_line(lines, len(coverage))
    if not_checked is None:
        return
    lines.append("")
    lines.append("### Not checked in the review sandbox")
    lines.append("")
    if not not_checked and not not_checked_dropped:
        lines.append("- none reported")
    lines.extend(
        f"- **{_inline(e.kind)}** — {_inline(e.check)}: {_inline(e.reason)}"
        for e in not_checked[:COVERAGE_LIST_CAP]
    )
    _more_line(lines, len(not_checked))
    if not_checked_dropped:
        lines.append(_dropped_line(not_checked_dropped))


def _cell(text: str) -> str:
    """Reviewer-authored text as ONE markdown table cell.

    A newline would split the row and a bare pipe would add a column, so a
    reason the reviewer wrote across two lines would garble the table a human
    reads before approving the merge — and a crafted one would forge whole rows
    in it. Both are neutralized here rather than trusted: the text is already
    credential-redacted, but it is still the reviewer's prose. Every cell of
    every table in this comment goes through here.
    """
    return " ".join(text.replace("|", "\\|").split()) or "—"


def _criterion_row(result: CriterionResult) -> str:
    """One per-criterion table row: id, outcome, evidence, the stated reason."""
    icon = _CRITERION_ICONS.get(result.result, "")
    digest = result.evidence_output_digest
    short = f"{digest[:20]}…" if len(digest) > 20 else digest
    evidence = (
        f"`{_cell(result.evidence_command)}` → `{_cell(short)}`"
        if result.evidence_command
        else "—"
    )
    return (
        f"| {result.id} | {result.result} {icon} | {evidence} "
        f"| {_cell(result.reason)} |"
    )


def _plural(count: int) -> tuple[str, str]:
    """``(criteri-suffix, verb)`` so one criterion never reads "1 criteria were"."""
    return ("on", "was") if count == 1 else ("a", "were")


def _no_plan_note(unjudged: UnjudgedCriteria | None) -> str:
    """The sentence that stands in for a table there was no plan section for."""
    if unjudged is None:
        return f"_{NO_CRITERIA_NOTE}._"
    plural, verb = _plural(unjudged.criteria_count)
    return UNJUDGED_CRITERIA_NOTE.format(
        count=unjudged.criteria_count,
        plural=plural,
        verb=verb,
        plan_sha=unjudged.plan_sha[:12],
        reason=_cell(unjudged.reason),
    )


def _omitted_note(plan_sha: str, omitted: tuple[str, ...]) -> str:
    """The sentence that keeps a partial table from reading as the whole plan."""
    plural, verb = _plural(len(omitted))
    return OMITTED_CRITERIA_NOTE.format(
        count=len(omitted),
        plural=plural,
        verb=verb,
        plan_sha=plan_sha[:12],
        ids=_cell(", ".join(omitted)),
    )


def _append_criteria_table(
    lines: list[str],
    plan_sha: str | None,
    criteria_results: list[CriterionResult],
    omitted_criterion_ids: tuple[str, ...] = (),
    unjudged: UnjudgedCriteria | None = None,
) -> None:
    """Render the per-criterion verdict, and whatever it does NOT cover (#2291).

    ``plan_sha`` is the host's fact about whether a plan section was rendered
    at all — never the length of *criteria_results*, which a reviewer could
    make empty. No plan sha and no *unjudged* plan means the item has no
    recorded criteria, and the note says exactly that. No plan sha WITH an
    *unjudged* plan means kew holds criteria it could not put in front of this
    round's reviewer: a different sentence, because reading it as the first one
    approves a host-side gap as an issue that asked for nothing. And a rendered
    table names the ids the prompt could not carry, so its rows are never
    mistaken for the whole plan.
    """
    lines.append("")
    if plan_sha is None:
        lines.append(_no_plan_note(unjudged))
        return
    lines.append(f"### Acceptance criteria — plan `{plan_sha[:12]}`")
    lines.append("")
    lines.append("| Criterion | Result | Evidence | Reason |")
    lines.append("|---|---|---|---|")
    lines.extend(_criterion_row(result) for result in criteria_results)
    if omitted_criterion_ids:
        lines.append("")
        lines.append(_omitted_note(plan_sha, omitted_criterion_ids))


def _append_run_cap_warning(lines: list[str], branch_runs: int) -> None:
    """Warn before the branch reaches the producer guard's run cap (#3193).

    The cap is hard: at it, kew cannot bind another head it pushes to the
    branch, and the next send-back is held for a person. Saying so from
    ``BRANCH_RUNS_WARNING`` runs on gives the operator time to finish the PR.
    """
    if branch_runs < BRANCH_RUNS_WARNING:
        return
    lines.append("")
    lines.append(
        f"> ⚠ **Run cap** — this branch carries {branch_runs} of the "
        f"{BRANCH_RUNS_CAP} runs kew's publication guard reads. At "
        f"{BRANCH_RUNS_CAP} kew cannot bind another head it pushes here, and "
        "the next send-back is held for you: finish this PR soon."
    )


def _append_gate_paths_note(
    lines: list[str], verdict: str, gate_paths: tuple[str, ...], *, residuals: bool
) -> None:
    """Say why a full round awaits a second full review (#3422).

    Only for a round recorded ``NEEDS_FULL`` with gate paths: the reviewer
    returned READY, or (*residuals*) only findings the stop rule would accept,
    and the diff changes what the merge gates are made of.
    """
    if verdict != _VERDICT_NEEDS_FULL or not gate_paths:
        return
    returned = "only findings kew would accept as residuals" if residuals else "READY"
    lines.append("")
    lines.append(
        f"> **Second review required** — the reviewer returned {returned}, but "
        "this diff touches merge gate paths, so a second, independent full "
        "review must confirm it on this head before it stands. Gate paths "
        "touched:"
    )
    for path in gate_paths[:_GATE_PATHS_NAMED]:
        shown = path if len(path) <= _GATE_PATH_CHARS else path[:_GATE_PATH_CHARS] + "…"
        lines.append(f"> - {_code(shown)}")
    if len(gate_paths) > _GATE_PATHS_NAMED:
        lines.append(f"> - (+{len(gate_paths) - _GATE_PATHS_NAMED} more)")


def _append_excused(lines: list[str], excused_tests: tuple[str, ...]) -> None:
    """The tests kew took off the reviewer's declaration findings (#3516): the
    verdict can read READY over a sidecar that says NEEDS_CHANGES, and this
    line is why."""
    if excused_tests:
        lines.append("")
        lines.append(
            "excused as declared: " + ", ".join(_code(t) for t in excused_tests)
        )


def _build_body(
    *,
    verdict: str,
    head_sha: str,
    current_head_sha: str,
    review_round: int,
    sidecar_id: str | None,
    findings: list[Finding],
    acceptance_evidence: list[dict[str, str]],
    acceptance_executed: bool | None = None,
    acceptance_reason: str | None = None,
    plan_sha: str | None = None,
    criteria_results: list[CriterionResult] | None = None,
    omitted_criterion_ids: tuple[str, ...] = (),
    unjudged_criteria: UnjudgedCriteria | None = None,
    coverage: list[CoverageEntry] | None = None,
    not_checked: list[NotCheckedEntry] | None = None,
    not_checked_dropped: int = 0,
    blocking: bool,
    branch_runs: int = 0,
    gate_skipped: str | None = None,
    gate_paths: tuple[str, ...] = (),
    excused_tests: tuple[str, ...] = (),
) -> str:
    """Build the comment body for the given verdict.

    The sentinel must be the FIRST line so ``_find_existing_comment`` matches it
    reliably without reading the full body structure.
    """
    lines: list[str] = [_COMMENT_SENTINEL]

    sidecar_note = f"sidecar: `{sidecar_id[:16]}…`" if sidecar_id else "no sidecar"
    lines.append(
        f"**kew adversarial review** — machine-posted · {sidecar_note} "
        f"(host-stored; source of truth)"
    )
    lines.append("")

    icon = "✓" if verdict == _VERDICT_READY else "✗"
    lines.append(
        f"**Verdict: {verdict} {icon}** — round {review_round} · head `{head_sha[:12]}`"
    )

    # Stale-verdict marker: the verdict's head differs from the current PR head.
    if head_sha != current_head_sha:
        lines.append("")
        lines.append(
            f"> ⚠ **Stale** — head has since moved to `{current_head_sha[:12]}` "
            f"(this verdict no longer applies to the current code)"
        )

    _append_run_cap_warning(lines, branch_runs)
    _append_gate_paths_note(lines, verdict, gate_paths, residuals=bool(findings))
    # Everything above is host-written and short; everything below carries
    # reviewer text and is what ``_bounded`` may cut.
    head_end = len(lines)

    if gate_skipped:
        lines.append("")
        lines.append(GATE_SKIPPED_NOTE.format(reason=_inline(gate_skipped)))

    # The per-criterion verdict against the item's recorded plan (#2291) — what
    # the work was FOR, above the defects found in how it was done.
    _append_criteria_table(
        lines,
        plan_sha,
        criteria_results or [],
        omitted_criterion_ids,
        unjudged_criteria,
    )

    # NEEDS_CHANGES: emit the evidenced findings table. Every cell is reviewer
    # prose, so every cell goes through ``_cell``: a summary carrying a newline
    # and pipes would otherwise forge extra rows in the table a human reads
    # before approving the merge.
    if findings:
        lines.append("")
        lines.append("### Findings")
        lines.append("")
        lines.append("| Severity | Location | Summary |")
        lines.append("|---|---|---|")
        for finding in findings:
            lines.append(
                f"| {_cell(finding.severity)} | `{_cell(finding.location)}` "
                f"| {_cell(finding.summary)} |"
            )

    _append_excused(lines, excused_tests)

    # READY: emit acceptance-evidence commands + digests, or the explicit
    # unavailable result.
    _append_acceptance_status(
        lines, acceptance_evidence, acceptance_executed, acceptance_reason
    )

    _append_coverage(lines, verdict, coverage or [], not_checked, not_checked_dropped)

    # Blocking mode: state the merge invariant so a human knows why merge is held.
    foot: list[str] = []
    if blocking:
        foot.append("")
        foot.append(
            "_Blocking: merge requires a READY verdict bound to the current head SHA "
            "— this is the kew merge-pass invariant_"
        )

    return _bounded(lines[:head_end], lines[head_end:], foot)


def _size(lines: list[str]) -> int:
    """UTF-8 bytes of *lines* joined by newlines, counting a trailing one."""
    return sum(len(line.encode()) + 1 for line in lines)


def _bounded(head: list[str], content: list[str], foot: list[str]) -> str:
    """The comment body, cut to :data:`COMMENT_BODY_CAP` bytes (#3419).

    *head* (the sentinel and the verdict line) and *foot* (the blocking
    invariant) are always kept. *content* comes in priority order — criteria,
    findings, acceptance, then the coverage lists — so cutting its tail drops
    the lowest-value sections first. A cut is never silent: a visible note
    says how many lines went, and an acceptance ``<details>`` block it split is
    closed so the note is not folded inside it.
    """
    content = [
        line if len(line) <= _COMMENT_LINE_CAP else line[: _COMMENT_LINE_CAP - 1] + "…"
        for line in content
    ]
    if _size(head) + _size(content) + _size(foot) <= COMMENT_BODY_CAP:
        return "\n".join(head + content + foot)
    budget = COMMENT_BODY_CAP - _size(head) - _size(foot) - 512
    kept: list[str] = []
    for line in content:
        budget -= len(line.encode()) + 1
        if budget < 0:
            break
        kept.append(line)
    omitted = len(content) - len(kept)
    if kept.count("</details>") < sum(line.startswith("<details>") for line in kept):
        kept.append("</details>")
    note = _TRUNCATED_NOTE.format(omitted=omitted, plural="" if omitted == 1 else "s")
    return "\n".join([*head, *kept, "", note, *foot])


def _find_existing_comment(repo: str, pr_number: int) -> int | None:
    """Return the GitHub comment ID of the existing kew verdict comment, or None.

    Lists issue comments on the PR and scans for the sentinel.  Returns None on
    any ``gh`` failure — the caller will POST a new comment instead.
    """
    try:
        result = subprocess_utils.run(
            [
                "gh",
                "api",
                f"repos/{repo}/issues/{pr_number}/comments",
                "--paginate",
                "--slurp",
            ],
            capture=True,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort; failure → create new
        _logger.warning(
            "review-comment: could not list comments for PR #%d: %s", pr_number, exc
        )
        return None
    if result.returncode != 0:
        return None
    return _scan_comments_for_sentinel(result.stdout, pr_number)


def _flatten_comment_pages(data: Any) -> list[dict[str, Any]]:
    """``--paginate --slurp`` emits a list of pages; flatten to comment dicts."""
    comments: list[dict[str, Any]] = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, list):
                comments.extend(c for c in item if isinstance(c, dict))
            elif isinstance(item, dict):
                comments.append(item)
    return comments


def _scan_comments_for_sentinel(stdout: str, pr_number: int) -> int | None:
    """Comment ID of the sentinel-bearing comment in a gh api payload, or None."""
    try:
        for comment in _flatten_comment_pages(json.loads(stdout)):
            body: str = comment.get("body", "") or ""
            if _COMMENT_SENTINEL in body:
                return int(comment["id"])
    except Exception as exc:  # noqa: BLE001
        _logger.warning(
            "review-comment: could not parse comment list for PR #%d: %s",
            pr_number,
            exc,
        )
    return None


def post_verdict_comment(
    repo: str,
    pr_number: int | None,
    *,
    verdict: str,
    head_sha: str,
    current_head_sha: str,
    review_round: int,
    sidecar_id: str | None,
    findings: list[Finding],
    acceptance_evidence: list[dict[str, str]],
    acceptance_executed: bool | None = None,
    acceptance_reason: str | None = None,
    plan_sha: str | None = None,
    criteria_results: list[CriterionResult] | None = None,
    omitted_criterion_ids: tuple[str, ...] = (),
    unjudged_criteria: UnjudgedCriteria | None = None,
    coverage: list[CoverageEntry] | None = None,
    not_checked: list[NotCheckedEntry] | None = None,
    not_checked_dropped: int = 0,
    blocking: bool,
    branch_runs: int = 0,
    gate_skipped: str | None = None,
    gate_paths: tuple[str, ...] = (),
    excused_tests: tuple[str, ...] = (),
) -> None:
    """Post or update the machine verdict comment on the PR — best-effort.

    A no-op when ``pr_number`` is None (no PR to comment on).  Any ``gh`` or
    GitHub failure is logged and swallowed so a comment hiccup never crashes
    the driver tick.
    """
    if pr_number is None:
        return
    body = _build_body(
        verdict=verdict,
        head_sha=head_sha,
        current_head_sha=current_head_sha,
        review_round=review_round,
        sidecar_id=sidecar_id,
        findings=findings,
        acceptance_evidence=acceptance_evidence,
        acceptance_executed=acceptance_executed,
        acceptance_reason=acceptance_reason,
        plan_sha=plan_sha,
        criteria_results=criteria_results,
        omitted_criterion_ids=omitted_criterion_ids,
        unjudged_criteria=unjudged_criteria,
        coverage=coverage,
        not_checked=not_checked,
        not_checked_dropped=not_checked_dropped,
        blocking=blocking,
        branch_runs=branch_runs,
        gate_skipped=gate_skipped,
        gate_paths=gate_paths,
        excused_tests=excused_tests,
    )
    existing_id = _find_existing_comment(repo, pr_number)
    try:
        if existing_id is not None:
            result = subprocess_utils.run(
                [
                    "gh",
                    "api",
                    "-X",
                    "PATCH",
                    f"repos/{repo}/issues/comments/{existing_id}",
                    "-f",
                    f"body={body}",
                ],
                capture=True,
                check=False,
            )
        else:
            result = subprocess_utils.run(
                [
                    "gh",
                    "api",
                    "-X",
                    "POST",
                    f"repos/{repo}/issues/{pr_number}/comments",
                    "-f",
                    f"body={body}",
                ],
                capture=True,
                check=False,
            )
    except Exception as exc:  # noqa: BLE001 — best-effort; a publish failure never crashes the tick
        _logger.warning(
            "review-comment: could not post verdict comment for PR #%d: %s",
            pr_number,
            exc,
        )
        return
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()[:200]
        _logger.warning(
            "review-comment: gh POST/PATCH for PR #%d failed (rc=%d): %s",
            pr_number,
            result.returncode,
            stderr,
        )
