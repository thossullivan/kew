"""The title and body of the pull request kew opens for an agent's work (#3187).

The PR title used to be the issue title — the bug statement — and the squash
landed that on main, so the history read as a list of complaints. The title now
states what the product does after the merge, in the agent's own words: the
Dispatch Report's ``PR title:`` line, or else the first sentence of the
report's summary paragraph, or else ``Fix: <issue title>``. Never a "What was
done" bullet: those name code paths. The text is stripped of markdown, folded
to one line, capped on a word boundary, and a closing keyword followed by a
reference (``fixes #77``) gets a zero-width space after its first letter, so
the squash subject on main closes nothing.

The body carries the closing reference, a short cause, change and proof, and
the agent's **Behavior changes** lines verbatim under **Proposed behavior
changes**, inside a code fence. That heading is not the one the merge's
held-out gate reads (:func:`kew.merge_verify.behavior_change_lines`), so an
agent's own line declares nothing and cannot excuse its own test edits. A line
becomes a declaration only through the reviewer-requested path (#2872):
:mod:`kew.behavior_declarations` rebuilds it as ``- <requested id> — <reason>``,
the id verbatim and only the reason escaped (:func:`escape_text`), and adds it
under **Behavior changes**.

The body stays under GitHub's 65,536-character limit (:data:`MAX_BODY_CHARS`):
prose lines are dropped first, whole, and a proposed line is never cut. When
the proposed lines alone do not fit, :class:`PrBodyTooLong` is raised before
anything is pushed.

Pure text construction: no config, no I/O.
"""

from __future__ import annotations

import html
import re
from collections.abc import Collection

from kew.commit_message import (
    CLOSING_KEYWORD,
    cap_on_word,
    report_section_item,
    report_summary_line,
    sanitize_commit_subject,
)
from kew.ids import closes_marker, item_ref
from kew.merge_verify import behavior_change_lines

#: Where the agent's own Behavior changes lines go: a heading the merge's
#: held-out gate does not read, so they are proposals, never declarations.
PROPOSED_HEADING = "**Proposed behavior changes**"
_PROPOSED_NOTE = (
    "The agent's own lines, verbatim. They declare nothing: kew's reviewer rules "
    "on each, and kew adds the ones it accepts under Behavior changes."
)
#: GitHub refuses a body over 65,536 characters; the margin leaves room for the
#: Behavior changes lines the reviewer-requested path adds later.
MAX_BODY_CHARS = 60_000
#: Under GitHub's 256-character title limit, with room for the squash's
#: `` (#<pr>)`` suffix.
MAX_TITLE_CHARS = 200

#: Numeric entities only: a named one (``&amp;``) would put letters into a
#: reason, and the merge reads two letters as a stated reason.
_ENTITIES = {"&": "&#38;", "<": "&#60;", ">": "&#62;", "@": "&#64;", "*": "&#42;"}
_ESCAPED = re.compile("[&<>@*]")
#: The break inside a keyword in rendered markdown: GitHub no longer reads a
#: keyword, the reader still sees one. :func:`unescape_text` removes this exact
#: entity first; a literal ``&`` is always escaped, so agent text never spells it.
_ENTITY_BREAK = "&#8203;"
#: The same break in plain text (a title, a commit subject), where no entity is
#: decoded.
ZERO_WIDTH_SPACE = "​"

_PR_TITLE_LINE = re.compile(
    r"^\s*(?:[-*+]\s+)?[*_]{0,2}\s*pr title\s*[*_]{0,2}\s*:\s*[*_]{0,2}(?P<title>.*)$",
    re.IGNORECASE,
)
_REPORT_HEADING = re.compile(r"^#{1,6}\s*dispatch report\b", re.IGNORECASE)
#: A report metadata line: ``**Issue:** #12 — ...``, ``**Complexity:** simple``.
_META_LINE = re.compile(r"^(?:\*\*|__)[^*_]+?(?::(?:\*\*|__)|(?:\*\*|__):)")
_LIST_ITEM = re.compile(r"^(?:[-*+]|\d+[.)])\s")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_TAG = re.compile(r"<[^>]*>")
_MARKUP = re.compile(r"`+|\*+|~~|(?<!\w)_{1,2}|_{1,2}(?!\w)")
_NONE_LINE = re.compile(r"^(?:[-*+]\s*)?(?:none|n/?a)\.?$", re.IGNORECASE)


#: C0 control characters and DEL, except tab and newline: ``gh`` cannot pass a
#: NUL in an argument, and none of them belongs in a PR's text.
_CONTROLS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
#: The longest body GitHub accepts.
GITHUB_BODY_LIMIT = 65_536


class PrBodyTooLong(ValueError):
    """The proposed Behavior changes lines alone exceed :data:`MAX_BODY_CHARS`."""


def strip_controls(text: str) -> str:
    """*text* without C0 control characters or DEL; tab and newline stay."""
    return _CONTROLS.sub("", text)


def unsendable(title: str, body: str) -> str | None:
    """Why GitHub's PR create would refuse *title* and *body*, or None.

    Checked before anything is pushed, so a refusal never leaves a branch
    with no PR (#3187).
    """
    for name, text in (("title", title), ("body", body)):
        found = _CONTROLS.search(text) or (name == "title" and "\n" in text)
        if found:
            return f"the PR {name} holds a control character or line break"
        try:
            text.encode("utf-8")
        except UnicodeEncodeError as exc:
            return f"the PR {name} is not valid UTF-8 text ({exc.reason})"
    if not title.strip():
        return "the PR title is empty"
    if len(body) > GITHUB_BODY_LIMIT:
        return (
            f"the PR body is {len(body)} characters; GitHub takes {GITHUB_BODY_LIMIT}"
        )
    return None


def escape_text(text: str) -> str:
    """*text* safe to render in a PR body as the characters it holds.

    ``<``, ``>``, ``&``, ``@`` and ``*`` become numeric entities, so nothing
    hides text (``<!--``), pings someone or restyles it; a closing keyword
    followed by a reference gets an invisible break, so merging closes nothing
    it names. :func:`unescape_text` reverses both byte for byte.
    """
    escaped = _ESCAPED.sub(lambda m: _ENTITIES[m.group(0)], text)
    return break_closing_references(escaped, _ENTITY_BREAK)


def unescape_text(text: str) -> str:
    """The text :func:`escape_text` was given."""
    return html.unescape(text.replace(_ENTITY_BREAK, ""))


def break_closing_references(text: str, mark: str = ZERO_WIDTH_SPACE) -> str:
    """*text* with *mark* after the first letter of each closing keyword that a
    reference follows: ``fixes #77`` no longer closes issue 77 (#3187)."""
    return CLOSING_KEYWORD.sub(lambda m: f"{m['first']}{mark}{m['rest']}", text)


def _plain(text: str) -> str | None:
    """*text* as one plain line: no markdown, links or HTML; None when empty
    or the template's unfilled ``[...]`` placeholder."""
    stripped = text.strip()
    if stripped.startswith("[") and stripped.endswith("]"):
        return None
    plain = _MARKUP.sub("", _TAG.sub("", _LINK.sub(r"\1", stripped)))
    plain = sanitize_commit_subject(plain).lstrip("#").strip().strip("\"'")
    plain = plain.removesuffix(".").rstrip()
    return plain or None


def _title_line(report: str) -> str | None:
    """The report's ``PR title:`` line, as plain text."""
    for line in report.splitlines():
        match = _PR_TITLE_LINE.match(line)
        if match:
            return _plain(match.group("title"))
    return None


def _summary_lines(report: str) -> list[str]:
    """The report's summary paragraph: the prose before its first section."""
    lines = report.splitlines()
    start = next((n + 1 for n, ln in enumerate(lines) if _REPORT_HEADING.match(ln)), 0)
    paragraph: list[str] = []
    for line in lines[start:]:
        text = line.strip()
        if text.startswith("#"):
            break
        skipped = not text or _META_LINE.match(text) or _LIST_ITEM.match(text)
        if skipped or _PR_TITLE_LINE.match(text):
            if paragraph:
                break
            continue
        paragraph.append(text)
    return paragraph


def _summary_sentence(report: str) -> str | None:
    """The first sentence of the report's summary paragraph, as plain text."""
    paragraph = " ".join(_summary_lines(report))
    return _plain(_SENTENCE_END.split(paragraph, maxsplit=1)[0]) if paragraph else None


def agent_pr_title(item_id: str, title: str, report: str | None) -> str:
    """What the product does once the PR merges, in one plain line."""
    described = (_title_line(report) or _summary_sentence(report)) if report else None
    issue = sanitize_commit_subject(title)
    if not described and not issue:
        return f"Fix {item_ref(item_id)}"  # names the node's own issue, nothing else
    text = described or f"Fix: {issue}"
    return break_closing_references(cap_on_word(text, MAX_TITLE_CHARS))


def proposed_lines(report: str | None) -> list[str]:
    """The report's **Behavior changes** lines as written, without the blank
    lines around them; none when the section only says "None"."""
    # Stripped before the section is read: a line that is a heading only once
    # its controls go would otherwise ride into the body as a real one.
    lines = behavior_change_lines(strip_controls(report or ""))
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if all(_NONE_LINE.match(ln.strip()) for ln in lines if ln.strip()):
        return []
    return lines


def _proposed_section(lines: list[str]) -> str:
    """*lines* verbatim in a code fence under :data:`PROPOSED_HEADING`.

    The fence is one backtick longer than any run inside, so no line ends it.
    """
    if not lines:
        return ""
    runs = [len(run) for ln in lines for run in re.findall("`+", ln)]
    fence = "`" * max(3, max(runs, default=0) + 1)
    return "\n".join([PROPOSED_HEADING, "", _PROPOSED_NOTE, "", fence, *lines, fence])


_FENCE = re.compile(r"^\s*(`{3,})\s*$")


def _proposed_span(lines: list[str]) -> tuple[int, int, int] | None:
    """Where *lines* hold the proposed section: its heading, its opening
    fence and its closing fence (``len(lines)`` when it never closes)."""
    heading = next(
        (n for n, ln in enumerate(lines) if ln.strip() == PROPOSED_HEADING), None
    )
    if heading is None:
        return None
    for n in range(heading + 1, len(lines)):
        opened = _FENCE.match(lines[n])
        if opened is None:
            continue
        close = next(
            (m for m in range(n + 1, len(lines)) if lines[m].strip() == opened[1]),
            len(lines),
        )
        return heading, n, close
    return None


def proposed_section_lines(body: str | None) -> list[str]:
    """The lines inside a PR body's **Proposed behavior changes** block, as
    written there; none when the body has no such block."""
    lines = (body or "").splitlines()
    span = _proposed_span(lines)
    return [] if span is None else lines[span[1] + 1 : span[2]]


def with_proposed_lines(body: str, lines: list[str]) -> str:
    """*body* with its proposed section holding *lines*, then every earlier
    proposed line that names no test *lines* name (#3475).

    A later run's line for a test replaces the earlier one; the section is
    appended at the end of a body that has none.
    """
    out = body.splitlines()
    span = _proposed_span(out)
    earlier = [] if span is None else out[span[1] + 1 : span[2]]
    named = set().union(*(_named_tests(ln) for ln in lines))
    kept = [ln for ln in earlier if ln.strip() and not _named_tests(ln) & named]
    section = _proposed_section([*lines, *kept]).splitlines()
    if span is None:
        return f"{body.rstrip()}\n\n" + "\n".join(section) + "\n"
    out[span[0] : span[2] + 1] = section
    return "\n".join(out) + ("\n" if body.endswith("\n") else "")


def proposed_block_closed(body: str | None) -> bool:
    """Whether *body*'s proposed block, when it has one, ends at a closing
    fence: an unclosed one runs to the end of the body, over lines that are
    not proposals at all."""
    lines = (body or "").splitlines()
    span = _proposed_span(lines)
    return span is None or span[2] < len(lines)


def without_proposed_tests(body: str, node_ids: Collection[str]) -> str:
    """*body* with every proposed line naming one of *node_ids* removed (#3500).

    The reviewer ruled those proposals unneeded, so they leave the block. The
    heading and fences stay even when no line does: the heading ends any
    section above it, so removing it would widen that section.
    """
    out = body.splitlines()
    span = _proposed_span(out)
    if span is None:
        return body
    earlier = out[span[1] + 1 : span[2]]
    kept = [ln for ln in earlier if not _named_tests(ln) & set(node_ids)]
    if kept == earlier:
        return body
    out[span[1] + 1 : span[2]] = kept
    return "\n".join(out) + ("\n" if body.endswith("\n") else "")


def _named_tests(line: str) -> set[str]:
    """The first token of a proposed line: the test it names."""
    words = re.sub(r"^\s*[-*+]\s+", "", line).split()
    return {words[0].strip("`")} if words else set()


def agent_pr_body(item_id: str, title: str, report: str | None) -> str:
    """The closing reference, then cause, change, proof and proposed lines.

    With no report there is nothing to describe: the closing reference alone.
    Raises :class:`PrBodyTooLong` when the proposed lines alone do not fit.
    """
    marker = closes_marker(item_id)
    if not report:
        return marker
    facts = (
        ("Cause", sanitize_commit_subject(title)),
        ("Change", report_summary_line(report)),
        ("Proof", report_section_item(report, "testing")),
    )
    prose = [f"**{name}:** {escape_text(text)}" for name, text in facts if text]
    lines = proposed_lines(report)
    proposed = _proposed_section(lines)
    while True:
        parts = [marker, "\n".join(prose), proposed]
        body = strip_controls("\n\n".join(part for part in parts if part) + "\n")
        if len(body) <= MAX_BODY_CHARS:
            return body
        if not prose:
            raise PrBodyTooLong(
                f"the {len(lines)} proposed Behavior changes lines need a "
                f"{len(body)}-character PR body; kew keeps it under "
                f"{MAX_BODY_CHARS} (GitHub refuses one over 65536)"
            )
        prose.pop()
