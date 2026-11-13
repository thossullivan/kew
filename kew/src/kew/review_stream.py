"""The governed reviewer's verdict channel: its own final result event (#2021).

The contained reviewer runs the code under review (its tests, as acceptance
evidence) in the same container, as the same user, over the same ``/work``
mount. Any file in that checkout is therefore writable by the thing being
reviewed, and a verdict read from one can be forged with the right head SHA.

The verdict is instead taken from the runner's event stream as the HOST reads
it: the container's stdout, folded line by line in
``dispatch_sandbox.run_container_and_stream``. Only the runner's typed terminal
record counts — for claude-code the top-level ``{"type": "result"}`` event —
never free text, a tool result, or an assistant turn. A child process's
ordinary stdout reaches the stream only as the content of a ``tool_result``
inside a ``user`` event, which is never read here.

That stream is the container's stdout ALONE: a review launch gives the runtime
client a second pipe for the container's stderr (#2053), which the client would
otherwise merge into the same one. The reviewer's own channel is therefore the
only thing counted here, and a stray non-event line — a runtime warning from
the reviewed code, a daemon notice — no longer fails a genuine round. The
separated lines are drained and counted by ``sandbox.ContainerStderrDrain``,
never folded into a verdict.

A same-user child can still write a raw line into the agent's own stdout
through ``/proc/<pid>/fd/1``. That line cannot displace the agent's genuine
result: the capture requires EXACTLY ONE result event and requires it to be the
stream's last event other than the runner's own routine notices (``system``,
``rate_limit_event``), so an injected record next to the real one makes the
round ambiguous (failed). Every line that is not a
typed event (blank lines aside) also ends a result's claim to be last, and so
does a line the host reader split: a newline-free write glued onto the agent's
genuine result makes that line unparseable, so the forged record before it is
not the last event either.
A forged record can stand alone only if the reviewed code stops the agent
before its own result; a killed agent exits non-zero, which the collector
fails on its own. See ``docs/security.md`` for the residual boundary (an agent
stopped by a catchable signal).

What persists is a closed capture beside the host log: counts, the terminal
flags, and the text between the verdict delimiters — not the reviewer's prose.
It is written owner-only with ``O_NOFOLLOW`` into the host log directory, which
is never mounted into the container.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kew.sandbox import MAX_LINE_BYTES

_logger = logging.getLogger(__name__)

VERDICT_BEGIN = "<<<KEW-REVIEW-VERDICT-BEGIN>>>"
"""Opening delimiter of the verdict block in the reviewer's final message."""

VERDICT_END = "<<<KEW-REVIEW-VERDICT-END>>>"
"""Closing delimiter of the verdict block in the reviewer's final message."""

STREAM_VERDICT_RUNNERS: frozenset[str] = frozenset({"claude-code"})
"""Review runners whose typed terminal record kew can bind a verdict to.

claude-code ends ``--print --output-format stream-json`` with exactly one
top-level ``result`` event. Other runners have no single typed final record kew
reads here (opencode streams ``text`` parts; codex's containment lane has no
reviewer image), so the reviewer launcher refuses them before any worktree or
container access rather than reading a verdict from somewhere forgeable.
"""

VERDICT_BYTE_CAP = MAX_LINE_BYTES - 64 * 1024
"""Largest reviewer final message kew will hold (S3, #1431).

Nearly a megabyte, orders of magnitude above any real verdict. Over-cap is
REFUSED, never truncated-and-parsed: a truncated verdict either fails to decode
or decodes to a shorter findings list than the reviewer wrote. The cap sits
64 KiB under the host reader's line limit (``sandbox.MAX_LINE_BYTES``), so a
result line just over it still reaches the capture whole, with room for the
event's other fields and JSON escaping, and gets this cap's refusal. A line
over the reader's limit is cut before the capture sees it and is refused as a
split line.
"""

CAPTURE_SUFFIX = ".review-result.json"
"""Capture file suffix, keyed off the reviewer run's host log path."""

_RESULT_EVENT_TYPE = "result"
# Claude Code keeps emitting lifecycle notices after its result while
# background tasks settle (task_updated, background_tasks_changed, ...).
_SYSTEM_EVENT_TYPE = "system"

#: The runner's rate-limit notice (#2219). The runner emits one whenever its
#: rate-limit info CHANGES, so most of them say the run is fine; only the
#: ``rejected`` ones below are the provider refusing to serve the reviewer, and
#: only those are counted. Counted, never trusted: the count is how a reviewer
#: run a usage limit CUT OFF is told apart from a reviewer that ran to
#: completion and wrote a bad verdict — the first is infrastructure to retry,
#: the second is a verdict defect a human reads.
_RATE_LIMIT_EVENT_TYPE = "rate_limit_event"

#: The ``rate_limit_info.status`` that means the provider REFUSED the request.
#: Its siblings — ``allowed`` and ``allowed_warning`` — are a usage window
#: filling up, which a long run reports routinely and no run is ended by. They
#: never touch a verdict; an ``allowed_warning`` is recorded against the
#: reviewer's agent token by the contained stream fold this capture taps
#: (``dispatch_sandbox.sandbox_log_fold``, #3329).
_REJECTED_STATUS = "rejected"


def _at_reader_limit(line: str) -> bool:
    """True for a newline-free line as long as the host reader's cut.

    The reader decodes with ``errors="replace"``, which never shortens the
    bytes it read, so a cut line is at least ``MAX_LINE_BYTES`` long here.
    """
    if line.endswith("\n"):
        return False
    return len(line.encode("utf-8", "surrogatepass")) >= MAX_LINE_BYTES


def capture_path_for(log_file: Path) -> Path:
    """The host capture path beside the reviewer run's log file."""
    return log_file.with_name(log_file.stem + CAPTURE_SUFFIX)


def _strip_fence(text: str) -> str:
    """Drop one wrapping Markdown code fence, if the block is fenced."""
    lines = text.strip().splitlines()
    if len(lines) >= 2 and lines[0].startswith("```") and lines[-1].strip() == "```":
        lines = lines[1:-1]
    return "\n".join(lines).strip()


def extract_verdict_block(text: str) -> tuple[str | None, str | None]:
    """Return ``(block, None)`` or ``(None, reason)`` for one final message.

    Strict: exactly one opening and one closing delimiter, opening first. Two
    blocks are ambiguous, not "take the last one" — a message that carries a
    quoted or planted block beside the real one must not choose between them.
    """
    begins = text.count(VERDICT_BEGIN)
    ends = text.count(VERDICT_END)
    if begins == 0 and ends == 0:
        return None, "the reviewer's final result carries no verdict block"
    if begins != 1 or ends != 1:
        return None, (
            f"the reviewer's final result carries {begins} verdict opening and "
            f"{ends} closing delimiters; exactly one block is required"
        )
    start = text.index(VERDICT_BEGIN) + len(VERDICT_BEGIN)
    end = text.index(VERDICT_END)
    if end < start:
        return None, "the verdict block closes before it opens"
    return _strip_fence(text[start:end]), None


@dataclass
class ReviewResultCapture:
    """Folds a reviewer's raw stream lines into the facts the verdict needs.

    Never raises on a line: the container's stdout is untrusted output, and a
    malformed line is simply not a result event. A result counts only as the
    stream's last event other than the runner's own routine notices —
    ``system`` and ``rate_limit_event``: every line that is not a typed event
    (blank lines aside), and every typed event that is none of those three,
    clears ``terminal_is_result``.

    Lines arrive as the host reader returns them (``sandbox._readline_bytes``):
    newline-terminated except the final line at EOF, and cut at
    ``MAX_LINE_BYTES``. A newline-free line followed by another line was either
    cut by the reader or was a newline-free write the next line got glued onto.
    Both pieces count in ``split_lines`` and are treated as unparseable.
    """

    result_events: int = 0
    terminal_is_result: bool = False
    is_error: bool | None = None
    subtype: str | None = None
    text: str | None = None
    oversize: bool = False
    unparsed_lines: int = 0
    split_lines: int = 0
    rate_limit_rejections: int = 0
    _open_line: bool = False
    _verdict_blocks: set[str] = field(default_factory=set)

    def observe(self, line: str) -> None:
        continues_open_line = self._open_line
        self._open_line = bool(line) and not line.endswith("\n")
        if continues_open_line or _at_reader_limit(line):
            self.split_lines += 1
            self._record_unparseable()
            return
        if not line.strip():
            return
        event = self._parse_event(line)
        if event is None:
            return
        if event["type"] == _SYSTEM_EVENT_TYPE:
            # Neither sets nor clears the flag: a system notice cannot make a
            # result final, and a genuine result stays final behind one. A
            # second result still fails on the result count, and a notice a
            # newline-free write was glued onto does not parse as an event, so
            # it is counted unparsed — and the collector refuses any unparsed
            # line.
            return
        if event["type"] == _RATE_LIMIT_EVENT_TYPE:
            # Counted, and — like a ``system`` notice — neither sets nor clears
            # the flag. The runner forwards this asynchronously off the
            # provider's response headers, so one can land after the result;
            # clearing the flag there turned a reviewer's valid READY verdict
            # into an unparseable one over a notice the reviewer did not write.
            self._record_rate_limit(event)
            return
        self.terminal_is_result = event["type"] == _RESULT_EVENT_TYPE
        if self.terminal_is_result:
            self._record_result(event)

    def _record_rate_limit(self, event: dict[str, Any]) -> None:
        """Count one notice, and only where the provider REFUSED the request.

        The runner emits ``rate_limit_event`` on every change to its rate-limit
        info, ``allowed`` and ``allowed_warning`` included — routine traffic a
        run nowhere near a limit still carries. Counting those read any failed
        round as a usage limit, which replaced the needs-human hold a bad
        verdict earns with hours of silent relaunching and then a hold telling
        the operator to raise a limit that was never reached.

        The status lives in ``rate_limit_info``; a notice in any other shape is
        one this host does not recognise, and an unrecognised notice is never
        evidence that a limit ended the run.
        """
        info = event.get("rate_limit_info")
        if isinstance(info, dict) and info.get("status") == _REJECTED_STATUS:
            self.rate_limit_rejections += 1

    def _record_result(self, event: dict[str, Any]) -> None:
        """Fold one result event; the count, not this record, decides ambiguity."""
        self.result_events += 1
        self.is_error = event.get("is_error") is True
        subtype = event.get("subtype")
        self.subtype = subtype if isinstance(subtype, str) else None
        text = event.get("result")
        self.text = None
        self.oversize = False
        if not isinstance(text, str):
            return
        if len(text.encode("utf-8", "surrogatepass")) > VERDICT_BYTE_CAP:
            self.oversize = True
            return
        self.text = text
        # Every result's block is kept apart from the last one's, so a stream
        # whose results disagree on the verdict is told from one that repeats
        # or drops it (#3174).
        block, _ = extract_verdict_block(text)
        if block is not None:
            self._verdict_blocks.add(block)

    def _parse_event(self, line: str) -> dict[str, Any] | None:
        """One typed JSON event (an object with a string ``type``), or None.

        Every None is first recorded as an unparseable line, so a line that is
        not a typed event is counted and is never the terminal.
        """
        try:
            event = json.loads(line)
        except (ValueError, RecursionError):
            self._record_unparseable()
            return None
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            self._record_unparseable()
            return None
        return event

    def _record_unparseable(self) -> None:
        """A line that is not a typed event: counted, and never the terminal."""
        self.unparsed_lines += 1
        self.terminal_is_result = False

    def tap(self, stream: Any) -> ReviewResultTap:
        """Wrap *stream* so every line it yields is observed first."""
        return ReviewResultTap(stream, self)

    def as_json(self) -> dict[str, Any]:
        """The closed record kew persists — the block, never the prose."""
        block: str | None = None
        block_error: str | None = None
        if self.text is not None:
            block, block_error = extract_verdict_block(self.text)
        return {
            "result_events": self.result_events,
            "terminal_is_result": self.terminal_is_result,
            "is_error": self.is_error,
            "subtype": self.subtype,
            "oversize": self.oversize,
            "unparsed_lines": self.unparsed_lines,
            "split_lines": self.split_lines,
            "rate_limit_rejections": self.rate_limit_rejections,
            "distinct_verdict_blocks": len(self._verdict_blocks),
            "verdict_block": block,
            "block_error": block_error,
        }

    def write(self, log_file: Path) -> Path:
        """Persist the capture owner-only, refusing to follow a planted link."""
        path = capture_path_for(log_file)
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
        fd = os.open(path, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(self.as_json(), handle, sort_keys=True)
        return path


@dataclass
class ReviewResultTap:
    """A stream wrapper feeding each line to a capture before the consumer.

    Keeps the stream consumer's contract (iterate, then ``wait``) unchanged.
    """

    stream: Any
    capture: ReviewResultCapture

    def __iter__(self) -> Iterator[str]:
        for line in self.stream:
            self.capture.observe(line)
            yield line

    def wait(self) -> int:
        return int(self.stream.wait())


def discard_capture(log_file: Path) -> None:
    """Remove any capture left at this run's path before a new launch."""
    capture_path_for(log_file).unlink(missing_ok=True)


def read_capture(log_file: Path) -> Mapping[str, Any] | None:
    """The host capture for *log_file*, or None when none was written."""
    path = capture_path_for(log_file)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        # No verdict follows: collect_stream_verdict fails the round on None.
        _logger.warning("review: no stream capture at %s (%s)", path, exc)
        return None
    try:
        with os.fdopen(fd, "rb") as handle:
            raw = handle.read()
        data = json.loads(raw)
    except (OSError, ValueError) as exc:
        _logger.warning("review: unreadable stream capture at %s (%s)", path, exc)
        return None
    return data if isinstance(data, dict) else None
