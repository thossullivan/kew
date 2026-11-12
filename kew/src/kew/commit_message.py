"""The subject and body of the commit kew builds for an agent's work (#2062).

The host writes this commit whenever it publishes work the agent did not commit
itself — canonical publication, the #518 broker, the rescue path. It used to say
only ``complete work for #123 (committed by kew)``: a subject that names the
ceremony and not the change, so a merged history read as a wall of identical
titles while the PR (built from the issue title) said something useful.

The subject now describes the change — the agent's own one-line account of what
it did when its Dispatch Report carries one, otherwise the issue title — and the
provenance moves down into the body, where a trailer names the run.

Pure text construction: no config, no I/O, no git. ``dispatch_broker`` supplies
the rendered commit prefix and the run's report.
"""

from __future__ import annotations

import re

from kew.ids import item_ref

#: Conventional git subject width. The prefix counts against it.
SUBJECT_LIMIT = 72

#: However long the configured prefix is, the description keeps at least this
#: much room — a subject truncated to nothing describes even less than the
#: generic title it replaced.
MIN_DESCRIPTION = 24

#: Git trailer naming the run that produced the commit.
RUN_TRAILER = "Kew-Run-Id"

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_BULLET = re.compile(r"^(?:[-*+]|\d+[.)])(?:\s+|$)")

#: A forge closing keyword (its first letter, then the rest) and the reference
#: after it: an optional colon, then ``#N``, ``GH-N``, ``owner/repo#N`` or a
#: URL. In a commit or PR body that lands on the default branch it closes that
#: issue, whoever wrote the text. One definition, two uses: a commit subject
#: strips keyword and reference (:data:`CLOSING_REFERENCE`); a PR title breaks
#: each keyword (:data:`CLOSING_KEYWORD`, :func:`kew.pr_text.break_closing_references`).
_KEYWORD = r"\b(?P<first>[cfr])(?P<rest>lose[sd]?|ix(?:e[sd])?|esolve[sd]?)"
_REFERENCE = r":?\s*(?:#\d+|[\w.-]+/[\w.-]+#\d+|GH-\d+|https?://\S+)"
#: A keyword and the reference it closes, consumed together.
CLOSING_REFERENCE = re.compile(rf"{_KEYWORD}(?P<ref>{_REFERENCE})", re.IGNORECASE)
#: A keyword a reference follows, and only the keyword: a URL the reference
#: spans may hold a keyword of its own (``…/issues/1,fixes #2``), which must
#: match too.
CLOSING_KEYWORD = re.compile(rf"{_KEYWORD}(?={_REFERENCE})", re.IGNORECASE)


def sanitize_commit_subject(text: str) -> str:
    """One line of plain text: control characters and whitespace runs collapsed.

    Issue titles and agent reports are untrusted free text. A newline in a
    subject silently turns the rest of the title into a commit body, so
    everything folds to single spaces before it reaches the message.
    """
    return " ".join(_CONTROL.sub(" ", text).split())


def report_summary_line(report: str | None) -> str | None:
    """The agent's own one-line account of the change, or None.

    Reads the first item of the Dispatch Report's "What was done" section. An
    absent section, an empty one, or the template's unfilled ``- [List each
    ...]`` placeholder all mean the agent said nothing usable — the caller then
    falls back to the issue title.
    """
    return report_section_item(report, "what was done")


def report_section_item(report: str | None, section: str) -> str | None:
    """First item of the Dispatch Report's *section* (``### <section>``), or None."""
    if not report:
        return None
    heading = re.compile(rf"^#{{1,6}}\s*{re.escape(section)}\b.*$", re.IGNORECASE)
    lines = report.splitlines()
    for index, line in enumerate(lines):
        if heading.match(line.strip()):
            return _first_item(lines[index + 1 :])
    return None


def _first_item(lines: list[str]) -> str | None:
    """First non-empty line of a report section, stripped of its list marker."""
    for line in lines:
        text = line.strip()
        if not text:
            continue
        if text.startswith("#"):
            return None  # the section was empty; the next heading already began
        cleaned = sanitize_commit_subject(_BULLET.sub("", text, count=1))
        return None if not cleaned or cleaned.startswith("[") else cleaned
    return None


def strip_closing_references(text: str) -> str:
    """*text* without any issue-closing reference, whitespace re-collapsed.

    The description a subject is built from is an agent's report or an issue
    title; a ``Fixes #999`` in either must not close an issue when the commit
    merges. PR titles keep :func:`sanitize_commit_subject` alone. Removing one
    reference can join its neighbours into another (``Fixes Fixes #1 #999``), so
    the substitution repeats until nothing more matches.
    """
    stripped, count = CLOSING_REFERENCE.subn(" ", text)
    while count:
        stripped, count = CLOSING_REFERENCE.subn(" ", stripped)
    return " ".join(stripped.split())


def _capped(description: str, prefix: str) -> str:
    """*description*, trimmed on a word boundary so the subject stays readable."""
    return cap_on_word(description, max(SUBJECT_LIMIT - len(prefix), MIN_DESCRIPTION))


def cap_on_word(text: str, limit: int) -> str:
    """*text* in at most *limit* characters, cut on a word boundary with ``…``.

    The cut falls on the last space in the kept half or later; a single word
    longer than half the limit is cut inside it rather than lose the line.
    """
    if len(text) <= limit:
        return text
    head = text[: limit - 1].rstrip()
    space = head.rfind(" ")
    if space >= limit // 2:
        head = head[:space].rstrip()
    return f"{head}…"


def build_commit_message(
    *,
    item_id: str,
    prefix: str,
    title: str | None = None,
    run_id: str | None = None,
    report: str | None = None,
) -> str:
    """The full commit message for work kew commits on the agent's behalf.

    With a description available the subject is ``<prefix><description>`` and
    the body carries the attribution plus the run trailer. With neither a title
    nor a usable report there is nothing to describe, so the subject keeps the
    historical attribution line and only the trailer is added.
    """
    attribution = f"complete work for {item_ref(item_id)} (committed by kew)"
    description = strip_closing_references(
        report_summary_line(report) or sanitize_commit_subject(title or "")
    )
    subject = f"{prefix}{_capped(description, prefix) if description else attribution}"
    paragraphs = [subject]
    if description:
        paragraphs.append(attribution)
    if run_id:
        paragraphs.append(f"{RUN_TRAILER}: {run_id}")
    return "\n\n".join(paragraphs)
