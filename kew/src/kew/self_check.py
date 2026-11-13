"""Pre-publish implementer self-check: the reviewer's rubric, before round 1.

The review loop is where the drive's agent time goes — roughly 3.6 adversarial
rounds per implementation, most of them ending NEEDS_CHANGES. Re-review is
already incremental (:mod:`kew.review_delta`); what was missing is the cheap
step BEFORE round 1, where the implementer reads its own diff the way the
reviewer will.

So the dispatch prompt grows one bounded section, rendered HERE from the pinned
rubric artifact the review stage loads, never from a second hand-written copy
that could drift from it. It reads the operator checkout's copy
(:func:`kew.review_contract.load_reviewer_prompt`), where the review stage
reads the base ref's tree (#3087):

- :func:`rubric_checklist` keeps the rubric's review-focus sections and drops
  its OUTPUT CONTRACT — the verdict block, the sidecar schema, the
  criteria-results rules. The implementer must not produce a verdict or a
  sidecar; only the reviewer's lenses carry over.
- :func:`self_check_section` wraps that checklist in host-authored instructions:
  one pass, pinned to a ``<base>...HEAD`` diff, the recorded criterion ids to
  confirm, and an explicit refusal to record findings.

Trust: the rubric text is interpolated as INSTRUCTIONS, not fenced as data —
deliberately, because the point is that the implementer applies it. That is the
same trust the review stage already gives the artifact: a sha-pinned file whose
deployed copy is only reachable by a PR that merged into the base branch.

Which makes WHERE the deployed copy is read from load-bearing. It is read from
*rubric_root*, and the only admissible value is a root the run under dispatch
cannot write: the operator's own checkout, the repository the review stage
reads its base ref from. An agent checkout is NOT admissible — a retry, a
continuation or a loop resume materializes it from the prior attempt's branch,
so a rubric read
there is the previous agent's text, promoted into the next prompt as
instructions. The launch paths therefore pass
:func:`kew.worktree.operator_repo_root`, never the checkout they are dispatching
(#2454 round 2).

A rubric whose focus sections are all output contract renders NO section and
says so in the log (never a silent empty self-check), and the run then records
``self_check`` false — which is the truth about that prompt.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from pathlib import Path

from kew.review_contract import load_reviewer_prompt

_logger = logging.getLogger(__name__)

SELF_CHECK_HEADING = "## Pre-Publish Self-Check"
"""Heading of the rendered section. Also how a run reports that it rendered."""

# The rubric's output contract, matched by vocabulary rather than by heading
# name so a tuned rubric cannot smuggle verdict/sidecar instructions into the
# implementer's prompt by renaming a section. The verdict tokens are
# case-sensitive on purpose: a case-insensitive ``READY`` also matches the
# "already" in the rubric's own prose.
_OUTPUT_CONTRACT = re.compile(
    r"(?i:\b(?:verdict|sidecar|unverifiable|criteria_results|acceptance_executed"
    r"|acceptance_evidence|acceptance_reason|output_digest|head_sha)\b)"
    r"|\b(?:READY|NEEDS_CHANGES)\b"
)

# One fence boundary of :func:`kew.prompt.make_sentinel`, anchored to a whole
# line because that is the only shape the renderers emit: every fence kew builds
# is ``\n<sentinel>\n``. A token quoted MID-line is therefore not a boundary and
# cannot move a span — which is what an issue TITLE, interpolated unfenced into
# a markdown heading, was able to do (#2454 round 4). Spelled here rather than
# imported because ``kew.prompt`` imports this module; ``test_self_check`` pins a
# freshly made sentinel against it so the two cannot drift.
_SENTINEL = re.compile(r"^<<<KEW-UNTRUSTED-[0-9a-f]{32}>>>$", re.MULTILINE)
_HEADING_LINE = re.compile(rf"^{re.escape(SELF_CHECK_HEADING)}\s*$", re.MULTILINE)

_HEADING = re.compile(r"^##\s+(?P<title>.+?)\s*$")
_ITEM = re.compile(r"^(?:[-*]|\d+\.)\s")
_FENCE = re.compile(r"^\s*```")

_PREAMBLE = """{heading}

Your last step, and ONE bounded pass: no loop, no second opinion, and no
substitute for a review round — the adversarial review still runs after you.

Read your own change the way that reviewer will read it, pinned to the merge
base:

```
git diff {base_ref}...HEAD
```

That three-dot diff IS the change under review. A diff of the working tree
alone shows only what you have not committed yet, so it is not what the
reviewer sees; if `{base_ref}` is absent here, this checkout has no `origin`
remote and the bare branch name is the base.

Check that diff against the rubric below, fix what fails, then re-run the
verification commands above. Then stop: do NOT write a judgement, a findings
list, or any machine-readable result file. Recording findings is the review
stage's job, and a judgement of your own is not an input to it."""

_CRITERIA = """### Acceptance criteria to confirm in the diff

For each id, confirm the diff meets it AND carries a test that proves it:

{ids}"""

_CHECKLIST = """### The reviewer's own checklist

Quoted from the pinned reviewer rubric (sha {sha}) — the rubric this diff will
actually be reviewed against, with its judgement and reporting contract
removed, because none of that is yours to produce:

{checklist}"""


def _host_authored(prompt: str) -> str:
    """*prompt* with every sentinel-fenced untrusted span removed.

    The renderer opens a fence with one sentinel token ON ITS OWN LINE and
    closes it with the SAME token, so that — not the parity of the boundaries —
    is what this walks. Parity alone was forgeable in both directions: a single
    sentinel-SHAPED token typed into quoted content added a boundary, shifting
    every span after it, which both claimed a self-check for a prompt that
    rendered none and denied one for a prompt that rendered it (#2454 round 3).
    ``kew.prompt`` now strips that vocabulary out of every value it interpolates,
    fenced or not; matching the opening token, and only where the renderer can
    put one, is the layer that does not depend on it.

    A fence left open at the end of the prompt cannot be produced by a content
    author either, and dropping its span anyway errs toward reporting no
    self-check rather than claiming one.
    """
    host: list[str] = []
    opened: str | None = None
    cursor = 0
    for match in _SENTINEL.finditer(prompt):
        if opened is None:
            host.append(prompt[cursor : match.start()])
            opened = match.group()
        elif match.group() == opened:
            opened = None
        cursor = match.end()
    if opened is None:
        host.append(prompt[cursor:])
    return "".join(host)


def _split_blocks(lines: Sequence[str]) -> list[tuple[str, list[str]]]:
    """Section body as ``(separator_before, lines)`` blocks.

    A block is one paragraph, one list item with its continuation lines, or a
    whole fenced span. ``separator_before`` records the blank line that stood
    between this block and the previous one, so dropping a block never reflows
    the ones that survive it.
    """
    blocks: list[tuple[str, list[str]]] = []
    current: list[str] | None = None
    sep = ""
    fenced = False
    for line in lines:
        if fenced and current is not None:
            current.append(line)
            fenced = not _FENCE.match(line)
            continue
        if not line.strip():
            current, sep = None, "\n\n"
            continue
        if current is None or _ITEM.match(line) or _FENCE.match(line):
            current = []
            blocks.append((sep, current))
            sep = "\n"
        current.append(line)
        fenced = bool(_FENCE.match(line))
    return blocks


def _render_section(title: str, body: Sequence[str]) -> str:
    """One rubric section as the implementer reads it, output contract removed.

    Empty when every block in it was output contract: a bare heading would
    claim a focus area the rubric no longer states.
    """
    kept = [
        (sep, lines)
        for sep, lines in _split_blocks(body)
        if not _OUTPUT_CONTRACT.search("\n".join(lines))
    ]
    if not kept:
        return ""
    # Demoted one level: the rubric's own ``##`` sections nest under the
    # self-check's heading in the dispatch prompt.
    out = f"### {title}"
    for index, (sep, lines) in enumerate(kept):
        out += ("\n\n" if index == 0 else sep or "\n\n") + "\n".join(lines)
    return out


def _sections(prompt_text: str) -> list[tuple[str, list[str]]]:
    """The rubric's ``##`` sections as ``(title, body lines)``.

    The frontmatter and the ``#`` preamble are deliberately dropped: they are
    the reviewer's role framing ("you did NOT write this code"), which is false
    for the implementer, not a review focus area.
    """
    sections: list[tuple[str, list[str]]] = []
    body: list[str] = []
    title: str | None = None
    for line in prompt_text.splitlines():
        heading = _HEADING.match(line)
        if heading:
            if title is not None:
                sections.append((title, body))
            title, body = heading.group("title"), []
        elif title is not None:
            body.append(line)
    if title is not None:
        sections.append((title, body))
    return sections


def rubric_checklist(prompt_text: str) -> str:
    """The rubric's review-focus sections, with its output contract removed."""
    rendered = (
        _render_section(title, body)
        for title, body in _sections(prompt_text)
        if not _OUTPUT_CONTRACT.search(title)
    )
    return "\n\n".join(section for section in rendered if section)


def self_check_section(
    *,
    base_ref: str,
    criterion_ids: Sequence[str] = (),
    rubric_root: Path | None = None,
) -> str:
    """The dispatch prompt's pre-publish self-check section, or ``""``.

    *base_ref* is the ref the diff is pinned to; *criterion_ids* are the ids of
    the plan kew recorded for the item (empty when it recorded none — the
    section still renders, without a criteria list).

    *rubric_root* is the checkout the rubric is read from, working tree and
    uncommitted bytes included (:func:`kew.review_contract.load_reviewer_prompt`):
    a copy deployed there wins over the packaged default. That is not the
    review stage's read, which takes the deployed copy out of the base ref's
    tree (:func:`kew.review_contract.resolve_reviewer_prompt`, #3087), so the
    implementer and the reviewer see the same rubric only while the checkout's
    copy matches the base ref's. It MUST name an operator-controlled checkout — the
    quoted text becomes instructions, so a root the dispatched run can write
    would let the prior attempt's branch author the next prompt. ``None``
    ships the packaged artifact.

    Empty only when the loaded rubric states no review focus at all; that is
    logged, never silently rendered as an empty self-check.
    """
    prompt_text, sha = load_reviewer_prompt(base_ref_root=rubric_root)
    checklist = rubric_checklist(prompt_text)
    if not checklist:
        _logger.warning(
            "self-check: the reviewer rubric (sha %s) states no review focus "
            "once its verdict and reporting contract are removed, so this "
            "dispatch prompt carries no pre-publish self-check",
            sha[:12],
        )
        return ""
    parts = [_PREAMBLE.format(heading=SELF_CHECK_HEADING, base_ref=base_ref)]
    if criterion_ids:
        parts.append(_CRITERIA.format(ids="\n".join(f"- {i}" for i in criterion_ids)))
    parts.append(_CHECKLIST.format(sha=sha[:12], checklist=checklist))
    return "\n\n".join(parts)


def rendered_self_check(prompt: str) -> bool:
    """Whether *prompt* — the exact bytes a run was dispatched with — carries it.

    Read off the rendered prompt rather than tracked alongside it, so the
    ``self_check`` fact on the run cannot claim a section the agent never saw.

    Only a HOST-authored heading counts. The prompt also carries the issue
    body and the acceptance criteria quoted verbatim inside sentinel-fenced
    untrusted blocks, so a plain substring test let an issue that merely typed
    the heading record ``self_check`` true for a prompt with no section in it
    (#2454 round 2). The fenced spans are dropped first — the sentinel is drawn
    per render from a CSPRNG, :func:`kew.prompt.guard_untrusted` rejects a body
    that contains it, ``kew.prompt`` strips a sentinel-shaped token of its own
    out of every value it interpolates (an unfenceable issue title as much as a
    fenced body), and :func:`_host_authored` closes a fence only on the token
    that opened it and only where the renderer puts one, so a content author can
    neither forge nor close a fence — and the heading is then matched as a whole
    line, which is the only shape the renderer emits.
    """
    return bool(_HEADING_LINE.search(_host_authored(prompt)))
