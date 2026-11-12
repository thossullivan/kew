"""Name a runner that died of a signal, and keep what it last said (#2887).

A sandbox that runs out of memory does not fail politely: the cgroup OOM
killer SIGKILLs a process (exit 137), or a runtime that cannot allocate
aborts (exit 134). Neither prints a protocol result, so the runner's own
classification saw a bare nonzero exit, called it ``permanent`` and recorded
no reason at all. This module gives such a run a reason — the signal and the
runner's last non-protocol output lines — and the retryable ``resource``
category: the same work under the same caps may fit on a second attempt (a
test pool sized by the run, a burst that will not recur), but not on a third.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Iterable
from typing import TYPE_CHECKING

from kew.credential_surface import redact_credentials

if TYPE_CHECKING:
    from kew.tracking import DispatchResult

#: Exit statuses that mean "killed by this signal" (128 + signal number).
SIGNAL_EXITS: dict[int, str] = {134: "SIGABRT", 137: "SIGKILL"}

#: Error category of a run that exhausted a resource: retried once, not twice.
RESOURCE_CATEGORY = "resource"

#: Categories a signal exit may replace. Anything else — a budget stop, a
#: needs-human verdict, a quota refusal — already names the real cause.
_REPLACEABLE = frozenset({"", "permanent", "transient", "watchdog_kill"})

#: Hard stops that already explain a 137: the deadline or a host cap kill.
_EXPLAINED_HARD_STOPS = frozenset({"wall_clock", "resource_cap"})

#: ``ResourceCapCode.OOM``'s value: the cap a confirmed OOM kill records. A
#: confirmed OOM is not explained by its cap — the memory ran out, which is
#: exactly a ``resource`` exit (#3191).
OOM_CAP_CODE = "oom"

#: Reasons a runner records when it saw no result at all. They say less than
#: the signal does, so a signal exit replaces them (#3191).
PLACEHOLDER_REASONS = frozenset({"", "incomplete"})

DIAGNOSTIC_TAIL_LINES = 5
_DIAGNOSTIC_LINE_CHARS = 240


class DiagnosticTail:
    """The last few non-protocol output lines of a run, redacted and bounded.

    Thread-safe: a separated stderr pipe is drained on its own thread. Each
    line is credential-redacted and truncated on the way in, so nothing held
    here is a raw provider line.
    """

    def __init__(self, maxlen: int = DIAGNOSTIC_TAIL_LINES) -> None:
        self._lines: deque[str] = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def add(self, line: str) -> None:
        text = _clean(line)
        if not text:
            return
        with self._lock:
            self._lines.append(text)

    def lines(self) -> list[str]:
        with self._lock:
            return list(self._lines)


def _clean(line: str) -> str:
    return redact_credentials(line).strip()[:_DIAGNOSTIC_LINE_CHARS]


def remember_line(lines: list[str], line: str) -> None:
    """Append one cleaned *line* to *lines*, keeping only the last few."""
    text = _clean(line)
    if not text:
        return
    lines.append(text)
    del lines[:-DIAGNOSTIC_TAIL_LINES]


def signal_exit_reason(
    exit_code: int, last_lines: Iterable[str], *, oom: bool = False
) -> str:
    """``killed by SIGKILL (exit 137); last output: a | b``, or ``""``.

    *oom* says the runtime confirmed an out-of-memory kill, and the reason
    says so too.
    """
    signal = SIGNAL_EXITS.get(exit_code)
    if signal is None:
        return ""
    cause = ", out of memory" if oom else ""
    reason = f"killed by {signal} (exit {exit_code}{cause})"
    tail = [line for line in last_lines if line]
    if tail:
        # Each line was redacted alone; a secret stderr wrapped across two
        # lines is only whole in the joined text, so redact that once more.
        joined = redact_credentials("\n".join(tail)).replace("\n", " | ")
        reason = f"{reason}; last output: {joined}"
    return reason


def is_resource_exit(
    *,
    exit_code: int,
    error_category: str,
    hard_stop: str,
    intentional: bool,
    resource_cap_code: str = "",
) -> bool:
    """Whether a run's exit reads as resource exhaustion, not a code failure.

    A confirmed OOM kill is one even though its cap stopped the run: the cap
    that fired is the memory running out, not a checkout or host limit.
    """
    explained = hard_stop in _EXPLAINED_HARD_STOPS and not (
        hard_stop == "resource_cap" and resource_cap_code == OOM_CAP_CODE
    )
    return (
        exit_code in SIGNAL_EXITS
        and not intentional
        and not explained
        and error_category in _REPLACEABLE
    )


def name_signal_exit(result: DispatchResult) -> str:
    """Give a 134/137 death its signal, last output and category (#2887).

    An OOM kill or abort prints no protocol result, so the adapter saw a bare
    nonzero exit: the run recorded no reason, or a placeholder one such as
    ``incomplete``, and read as ``permanent``. Every exit path calls this once
    its hard stop is classified — the host runner and the sandboxed container
    alike (#3191). Returns the signal reason when the run now reads as
    ``resource``, else ``""``.
    """
    oom = (
        result.hard_stop == "resource_cap" and result.resource_cap_code == OOM_CAP_CODE
    )
    reason = signal_exit_reason(result.exit_code, result.diagnostic_tail, oom=oom)
    if not reason:
        return ""
    if result.stop_reason.strip() in PLACEHOLDER_REASONS:
        result.stop_reason = reason
    if not is_resource_exit(
        exit_code=result.exit_code,
        error_category=result.error_category,
        hard_stop=result.hard_stop,
        intentional=bool(
            result.paused or result.budget_exceeded or result.breaker_stopped
        ),
        resource_cap_code=result.resource_cap_code,
    ):
        return ""
    result.error_category = RESOURCE_CATEGORY
    return reason
