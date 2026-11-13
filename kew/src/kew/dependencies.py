"""Dependency parsing and auto-unblock logic for issue dispatch.

Parses ``<!-- kew:depends N M -->`` HTML comments from issue bodies
and provides helpers to check whether dependencies are resolved and
to find blocked issues that can be unblocked after a completion.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any, NamedTuple
from urllib.parse import quote, unquote

from kew.subprocess_utils import run

if TYPE_CHECKING:
    from kew.config import DispatchConfig

_logger = logging.getLogger(__name__)

EPIC_CONTAINER_MARKER = "kew:epic-container"
PLAN_NODE_MARKER = "kew:plan-node"
_DEPENDS_RE = re.compile(r"<!--\s*kew:depends\s+(.*?)-->")
_EPIC_CONTAINER_RE = re.compile(
    rf"<!--\s*{re.escape(EPIC_CONTAINER_MARKER)}\s+([^\s>]+)\s*-->"
)
_PLAN_NODE_RE = re.compile(
    rf"<!--\s*{re.escape(PLAN_NODE_MARKER)}"
    r"\s+([^\s>]+)\s+([^\s>]+)\s+([0-9a-f]+)\s*-->"
)
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
# An inline code span: a backtick run, content, then a run of the same length.
_CODE_SPAN_RE = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)")


def format_epic_container_marker(epic_name: str) -> str:
    """Return the durable HTML marker for one epic container issue."""
    return f"<!-- {EPIC_CONTAINER_MARKER} {epic_name} -->"


class PlanNodeIdentity(NamedTuple):
    """The plan node a plan-created issue was made for, read from its marker."""

    epic: str
    local_id: str
    # A digest of the draft's title and author-written body: the same local id
    # with a different digest names different work.
    digest: str


def format_plan_node_marker(epic_name: str, local_id: str, digest: str) -> str:
    """Return the durable HTML marker naming the plan node an issue was made for.

    The local id is the manifest author's string, so it is percent-encoded:
    whitespace or ``-->`` in it can neither split nor end the marker. *digest*
    fingerprints the draft the issue was made from (lowercase hex).
    """
    lid = quote(local_id, safe="")
    return f"<!-- {PLAN_NODE_MARKER} {epic_name} {lid} {digest} -->"


def _closes_fence(fence: re.Match[str] | None, char: str, length: int) -> bool:
    """Whether this line closes an open fence of ``char`` repeated ``length``."""
    if fence is None:
        return False
    delimiter = fence.group(1)
    return (
        delimiter[0] == char and len(delimiter) >= length and not fence.group(2).strip()
    )


def _opens_fence(fence: re.Match[str]) -> bool:
    """Whether this line opens a fence.

    A backtick fence's info string may not contain a backtick, so such a line
    is ordinary text. Treating it as a fence would hide an ACTIVE marker below.
    """
    delimiter = fence.group(1)
    return not (delimiter[0] == "`" and "`" in fence.group(2))


def _closed_openers(lines: list[str]) -> set[int]:
    """Indices of the fence openers that some later line closes.

    One backward pass tracks, per fence character, the longest bare closer
    still ahead: an opener is closed iff that run is at least its own length.
    """
    longest = {"`": 0, "~": 0}
    closed: set[int] = set()
    for index in range(len(lines) - 1, -1, -1):
        fence = _FENCE_RE.match(lines[index])
        if fence is None:
            continue
        delimiter = fence.group(1)
        if _opens_fence(fence) and longest[delimiter[0]] >= len(delimiter):
            closed.add(index)
        if not fence.group(2).strip():
            longest[delimiter[0]] = max(longest[delimiter[0]], len(delimiter))
    return closed


def _starts_fence(fence: re.Match[str], index: int, closed: set[int] | None) -> bool:
    """Whether line ``index`` opens a fence; ``closed=None`` keeps unclosed ones."""
    return _opens_fence(fence) and (closed is None or index in closed)


def _marker_candidate_lines(
    body: str, *, skip_indented: bool = True, unclosed_fence_is_code: bool = True
) -> Iterator[str]:
    """Yield the body's lines that can carry active metadata.

    Fenced blocks are skipped. Four-space-indented lines are treated as
    indented code: stricter than CommonMark, which also requires a preceding
    blank line, but the safe direction here. ``skip_indented=False`` keeps
    them, for a marker that has always been honoured inside a nested list.

    CommonMark runs an unclosed fence to the end of the document.
    ``unclosed_fence_is_code=False`` instead reads its opener as ordinary text
    and scans on from the next line, for a caller that must fail closed. The
    closed openers are found up front, so this stays one linear pass however
    many fences never close -- no rescan, no recursion per unclosed fence.
    """
    lines = body.splitlines()
    closed = None if unclosed_fence_is_code else _closed_openers(lines)
    char = ""
    length = 0
    for index, line in enumerate(lines):
        fence = _FENCE_RE.match(line)
        if char:
            if _closes_fence(fence, char, length):
                char = ""
                length = 0
            continue
        if fence is not None and _starts_fence(fence, index, closed):
            char = fence.group(1)[0]
            length = len(fence.group(1))
            continue
        # expandtabs first: CommonMark counts a tab as up to four columns, so
        # a tab-indented line is indented code even though it has no spaces.
        if not skip_indented or not line.expandtabs(4).startswith("    "):
            yield line


def epic_container_name(body: str | None) -> str | None:
    """Return the marked epic name, or None when the body is not a container.

    The marker counts only as a WHOLE line, outside fenced and indented blocks.
    The whole-line rule is what ends the code-span class: a marker quoted in
    prose, in an inline span, or in a multiline span sits beside other text on
    its line, so none of them can make a work issue vanish from membership.
    Tracking span state to reach the same answer was more code and less
    certain -- the fence tracker alone still lost to a multiline span.

    The residual hole is a marker alone on a line inside a multiline code span.
    `kew epic plan` writes the marker unindented and on its own line, so the
    canonical container always reads correctly.
    """
    if not body:
        return None
    for line in _marker_candidate_lines(body):
        stripped = line.strip()
        if stripped.startswith(">"):
            continue
        match = _EPIC_CONTAINER_RE.fullmatch(stripped)
        if match:
            return match.group(1)
    return None


def plan_node_identity(body: str | None) -> PlanNodeIdentity | None:
    """Return the ``(epic, local_id, digest)`` a plan-created issue was made for.

    Read like the container marker -- a whole line outside fenced and
    indented blocks -- except that a fence that never closes hides nothing:
    kew appends the marker after the author's text, and a stray fence above
    it must not make a re-applied manifest create the node a second time.
    """
    if not body:
        return None
    for line in _marker_candidate_lines(body, unclosed_fence_is_code=False):
        match = _PLAN_NODE_RE.fullmatch(line.strip())
        if match:
            return PlanNodeIdentity(
                match.group(1), unquote(match.group(2)), match.group(3)
            )
    return None


def _depends_markers(body: str | None) -> list[str]:
    """Return the token text of every active ``kew:depends`` comment.

    A comment inside a fenced block or an inline code span is documentation of
    the syntax, not a dependency, so it is skipped. A fence that never closes
    hides nothing: `kew epic plan` appends the marker after the author's text,
    and a stray fence above it must not drop the edge and open dispatch.
    """
    if not body:
        return []
    markers: list[str] = []
    for line in _marker_candidate_lines(
        body, skip_indented=False, unclosed_fence_is_code=False
    ):
        prose = _CODE_SPAN_RE.sub("", line)
        markers.extend(m.group(1) for m in _DEPENDS_RE.finditer(prose))
    return markers


def depends_marker_count(body: str | None) -> int:
    """How many active ``kew:depends`` comments the body carries.

    One is the documented form; more are still unioned, but ``kew epic
    validate`` warns so the author can merge them.
    """
    return len(_depends_markers(body))


def _normalize_depends_token(tok: str) -> str:
    """Read a bare ``#N`` or ``N`` token as issue ``N``; leave every other form.

    A zero-padded number reads as the member number, so ``#02`` and ``02``
    both match member ``2``. ``owner/repo#N`` and native ids such as
    ``ENG-123`` are not bare numbers, so they pass through unchanged.
    """
    digits = tok[1:] if tok.startswith("#") else tok
    if digits.isascii() and digits.isdigit():
        # lstrip, not int(): a 5000-digit token must not raise on CPython's
        # int-string conversion limit.
        return digits.lstrip("0") or "0"
    return tok


def parse_depends(body: str | None) -> list[int]:
    """Extract issue numbers from every ``<!-- kew:depends ... -->`` comment.

    Returns an empty list when no comment is found or the body is empty/None.
    A bare ``#N`` token reads as ``N``. Other non-numeric tokens in the
    comment are silently ignored.
    """
    result: list[int] = []
    for marker in _depends_markers(body):
        for tok in marker.split():
            try:
                result.append(int(_normalize_depends_token(tok)))
            except ValueError:
                continue
    return result


def parse_depends_ids(body: str | None) -> list[str]:
    """Extract dependency ids (provider-native tolerant) from every
    ``<!-- kew:depends ... -->`` comment.

    Unlike :func:`parse_depends` (which returns ints for the legacy one-hop
    unblock path), this returns raw string tokens so native ids like
    ``ENG-123`` survive. Deduplicates preserving first-seen order; drops empty
    tokens. A bare ``#N`` token reads as ``N``, so it matches member id ``N``.
    Used by the epic-graph adapter.
    """
    seen: dict[str, None] = {}
    for marker in _depends_markers(body):
        for raw in marker.split():
            tok = _normalize_depends_token(raw)
            if tok and tok not in seen:
                seen[tok] = None
    return list(seen)


def check_dependencies_resolved(issue_nums: list[int], repo: str) -> bool:
    """Return True if every issue in *issue_nums* is closed.

    Uses ``gh issue view`` for each dependency. Treats any API failure
    or parse error as "still open" (safe default).
    """
    if not issue_nums:
        return True
    for num in issue_nums:
        result = run(
            [
                "gh",
                "issue",
                "view",
                str(num),
                "--repo",
                repo,
                "--json",
                "state",
            ],
            check=False,
        )
        if result.returncode != 0:
            return False
        try:
            data = json.loads(result.stdout)
        except (json.JSONDecodeError, TypeError):
            return False
        if data.get("state") != "CLOSED":
            return False
    return True


def find_blocked_dependents(completed_issue: int, config: DispatchConfig) -> list[int]:
    """Find blocked issues that can be unblocked now that *completed_issue* is done.

    Fetches all open issues with the ``blocked`` label, parses their
    dependency comments, and returns issue numbers whose dependencies
    are all now closed.
    """
    result = run(
        [
            "gh",
            "issue",
            "list",
            "--repo",
            config.project.repo,
            "--label",
            config.labels.blocked,
            "--state",
            "open",
            "--json",
            "number,body,labels",
            "--limit",
            "1000",
        ],
        check=False,
    )
    if result.returncode != 0:
        _logger.warning("Failed to fetch blocked issues: rc=%d", result.returncode)
        return []
    try:
        issues: list[dict[str, Any]] = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(issues, list):
        return []

    to_unblock: list[int] = []
    for issue in issues:
        num = issue.get("number")
        if not isinstance(num, int):
            continue
        deps = parse_depends(issue.get("body"))
        if completed_issue not in deps:
            continue
        remaining_deps = [dep for dep in deps if dep != completed_issue]
        if check_dependencies_resolved(remaining_deps, config.project.repo):
            to_unblock.append(num)

    return to_unblock
