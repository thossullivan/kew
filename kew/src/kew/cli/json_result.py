"""``--json`` result summary for ``dispatch``, ``run`` and ``review`` (#2505).

In JSON mode a command's progress still streams for people, on stderr, and its
stdout carries exactly one JSON document once it ends: the run's id, item,
outcome, error category, PR URL, cost and duration under the run-report
frontmatter's own key names (:data:`kew.run_report_sink.RESULT_SUMMARY_KEYS`),
plus ``reason`` — why it refused, held or failed, ``null`` on success. A command
that refuses before any run still prints the same shape. Exit codes are the
command's own; JSON mode never changes them.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from contextlib import contextmanager, redirect_stdout
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

import typer

from kew.command_outcome import COULD_NOT_RUN_EXIT_CODE
from kew.progress import capture_problems
from kew.run_report_sink import RESULT_SUMMARY_KEYS, result_summary_fields
from kew.run_store import RunStore

#: The one key a summary carries beyond the run-report frontmatter's.
REASON_KEY = "reason"
#: Outcomes whose summary names a reason even when the command exits 0.
_UNSUCCESSFUL = frozenset({"refused", "query_failed", "failure"})


@dataclass
class JsonResult:
    """What the command learned about its outcome before it ended."""

    run_id: str | None = None
    outcome: str | None = None
    error_category: str | None = None
    reason: str | None = None
    record: dict[str, Any] = field(default_factory=dict)

    def document(self, exit_code: int, problems: list[str]) -> dict[str, Any]:
        """The summary: the run record's fields, then what the command noted."""
        doc: dict[str, Any] = dict.fromkeys(RESULT_SUMMARY_KEYS)
        doc.update(self.record)
        noted = {
            "run_id": self.run_id,
            "outcome": self.outcome,
            "error_category": self.error_category,
        }
        doc.update({key: value for key, value in noted.items() if value})
        doc["outcome"] = doc["outcome"] or _fallback_outcome(exit_code)
        doc[REASON_KEY] = self.reason or _fallback_reason(
            exit_code, doc["outcome"], problems
        )
        return doc


def _fallback_reason(exit_code: int, outcome: str, problems: list[str]) -> str | None:
    """Why an unsuccessful command ended, when it noted no reason itself: its
    last warning or error line, else its exit code."""
    if exit_code == 0 and outcome not in _UNSUCCESSFUL:
        return None
    return problems[-1] if problems else f"exited with code {exit_code}"


def _fallback_outcome(exit_code: int) -> str:
    """The outcome of a command that ended before it noted one."""
    if exit_code == 0:
        return "success"
    return "refused" if exit_code == COULD_NOT_RUN_EXIT_CODE else "failure"


_active: ContextVar[JsonResult | None] = ContextVar("kew_json_result", default=None)


def active() -> bool:
    """Whether a ``--json`` summary is being collected."""
    return _active.get() is not None


def note(
    *,
    run_id: str | None = None,
    outcome: str | None = None,
    error_category: str | None = None,
    reason: str | None = None,
    store: RunStore | None = None,
) -> None:
    """Record what the command knows about its outcome; a no-op outside JSON mode.

    With a *store* and a *run_id*, the run's recorded fields (outcome, PR URL,
    cost, duration, ...) fill the summary; an explicit *outcome* still wins, as
    the command's own verdict on a run that never launched.
    """
    summary = _active.get()
    if summary is None:
        return
    if run_id:
        summary.run_id = run_id
        record = store.get(run_id) if store is not None else None
        if record is not None:
            summary.record = result_summary_fields(record)
    summary.outcome = outcome or summary.outcome
    summary.error_category = error_category or summary.error_category
    summary.reason = reason or summary.reason


def _exit_code(exc: BaseException) -> int:
    if isinstance(exc, typer.Exit):
        return exc.exit_code
    if isinstance(exc, SystemExit):
        return exc.code if isinstance(exc.code, int) else 1
    return 1


@contextmanager
def json_result(enabled: bool) -> Iterator[None]:
    """Run a command body in JSON mode when *enabled*.

    Everything the body writes to stdout goes to stderr instead, so stdout
    carries nothing but the one summary written as the body ends — however it
    ends, an exit or an exception included.
    """
    if not enabled:
        yield
        return
    summary = JsonResult()
    token = _active.set(summary)
    stdout = sys.stdout
    exit_code = 0
    try:
        with capture_problems() as problems, redirect_stdout(sys.stderr):
            try:
                yield
            except BaseException as exc:
                exit_code = _exit_code(exc)
                raise
            finally:
                document = summary.document(exit_code, list(problems))
    finally:
        _active.reset(token)
        stdout.write(json.dumps(document) + "\n")
        stdout.flush()
