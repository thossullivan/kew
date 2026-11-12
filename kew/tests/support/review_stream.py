"""Doubles that deliver a reviewer verdict the way production does (#2021).

The verdict travels in the reviewer's own terminal ``result`` event in the
runner stream; nothing reads a verdict file. These helpers build that stream so
a fake launcher or a fake container exercises the real capture, store, digest
and parse path.
"""

from __future__ import annotations

import io
import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any

from kew.review_contract import (
    CollectedSidecar,
    collect_stream_verdict,
    criteria_contract_in_force,
)
from kew.review_dispatch import ReviewerLaunchContext
from kew.review_stream import VERDICT_BEGIN, VERDICT_END, ReviewResultCapture
from kew.sandbox import _readline_bytes, is_startup_ack

DEFAULT_COVERAGE_ENTRY: dict[str, Any] = {
    "target": "src/kew/review_contract.py:1",
    "tried": "ran the acceptance tests",
    "how": "executed",
    "evidence": {"command": "uv run pytest", "output_digest": "abc123"},
}
"""The one coverage entry a cooperative READY carries when a test names none."""


def verdict_message(verdict: dict[str, Any] | str, head_sha: str | None) -> str:
    """A final message carrying *verdict* in one delimited block.

    A dict without ``head_sha`` gets *head_sha* filled in, as a cooperative
    reviewer would write it; pass a dict that names one to test a mismatch.
    The coverage contract (#3419) is filled the same way: an empty
    ``not_checked`` on every verdict and one executed ``coverage`` entry on a
    READY, unless the dict names its own.
    """
    if isinstance(verdict, dict):
        body = dict(verdict)
        if head_sha is not None:
            body.setdefault("head_sha", head_sha)
        body.setdefault("not_checked", [])
        if body.get("verdict") == "READY":
            body.setdefault("coverage", [DEFAULT_COVERAGE_ENTRY])
        text = json.dumps(body)
    else:
        text = verdict
    return f"Review complete.\n{VERDICT_BEGIN}\n{text}\n{VERDICT_END}\n"


def result_line(
    message: str, *, is_error: bool = False, subtype: str = "success"
) -> str:
    """One claude-code stream-json terminal ``result`` event, as a stream line."""
    event = {
        "type": "result",
        "subtype": subtype,
        "is_error": is_error,
        "result": message,
        "session_id": "s-1",
        "total_cost_usd": 0.01,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    return json.dumps(event) + "\n"


def reviewer_stream(
    verdict: dict[str, Any] | str | None, head_sha: str | None
) -> list[str]:
    """A plausible reviewer stream ending in one verdict-bearing result.

    Every line is newline-terminated, as the host reader delivers it.
    """
    init = {"type": "system", "subtype": "init", "session_id": "s-1"}
    turn = {
        "type": "assistant",
        "message": {
            "content": [{"type": "text", "text": "reviewing"}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    }
    lines = [json.dumps(init) + "\n", json.dumps(turn) + "\n"]
    if verdict is not None:
        lines.append(result_line(verdict_message(verdict, head_sha)))
    return lines


def rate_limit_notice(status: str = "rejected") -> str:
    """One runner ``rate_limit_event`` line, in the runner's own shape (#2219).

    claude-code emits this whenever its rate-limit info changes and carries the
    window's state in ``rate_limit_info.status`` — one of ``allowed``,
    ``allowed_warning``, ``rejected``. Only ``rejected`` is the provider
    refusing to serve the run; the other two are a window filling up, which a
    long run reports routinely. Building the event in any other shape would
    test a notice the runner never writes.
    """
    info: dict[str, Any] = {"status": status, "rateLimitType": "five_hour"}
    if status != "allowed":
        info["resetsAt"] = 1_760_000_000
    event = {
        "type": "rate_limit_event",
        "rate_limit_info": info,
        "uuid": "00000000-0000-4000-8000-000000000001",
        "session_id": "s-1",
    }
    return json.dumps(event) + "\n"


def rate_limited_stream(*, killed: bool = False, status: str = "rejected") -> list[str]:
    """The stream of a reviewer a provider usage limit cut off (#2219).

    The incident's shape: the provider's rejection notices arrive mid-run, then
    the run ends on an error result carrying no verdict block at all.

    ``killed`` is the other shape the same limit leaves — the run dies
    mid-stream, so there is no result event at all and the capture never records
    an ``is_error`` either way.

    ``status`` lowers the notices to a benign one (``allowed_warning``) to build
    the near miss: a run that only ever reported a window filling up and then
    failed for its own reasons.
    """
    init = {"type": "system", "subtype": "init", "session_id": "s-1"}
    lines = [json.dumps(init) + "\n"]
    lines.extend(rate_limit_notice(status) for _ in range(3))
    if killed:
        return lines
    lines.append(
        result_line(
            "Claude AI usage limit reached",
            is_error=True,
            subtype="error_during_execution",
        )
    )
    return lines


def stream_rate_limited(
    ctx: ReviewerLaunchContext,
    *,
    exit_code: int = 1,
    killed: bool = False,
    status: str = "rejected",
) -> CollectedSidecar:
    """Collect a usage-limited reviewer run as a launcher double (#2219)."""
    return collect_stream_verdict(
        capture_of(rate_limited_stream(killed=killed, status=status)),
        head_sha=ctx.head_sha,
        exit_code=exit_code,
        dest_dir=ctx.log_dir,
        run_id=ctx.run_id,
    )


def capture_of(lines: Iterable[str]) -> dict[str, Any]:
    """The host capture record the real tap would persist for *lines*."""
    capture = ReviewResultCapture()
    for line in lines:
        capture.observe(line)
    return capture.as_json()


def capture_through_reader(stdout: bytes) -> dict[str, Any]:
    """The capture of raw container *stdout*, read the way the host reads it.

    Each line comes from ``sandbox._readline_bytes`` (bounded, so an over-long
    line is cut), and whole startup acknowledgements are dropped, as
    ``ContainerHandle`` does before the review tap sees the stream.
    """
    stream = io.TextIOWrapper(io.BytesIO(stdout), encoding="utf-8")
    capture = ReviewResultCapture()
    while line := _readline_bytes(stream):
        if not is_startup_ack(line):
            capture.observe(line)
    return capture.as_json()


def stream_verdict(
    ctx: ReviewerLaunchContext,
    verdict: dict[str, Any] | str | None,
    *,
    exit_code: int = 0,
) -> CollectedSidecar:
    """Collect *verdict* as a launcher double: through the real stream path.

    ``None`` is a reviewer that ended without a result event (the crash path).

    The criterion ids ride off the launch context, exactly as the production
    launcher takes them, so a double exercises the real #2291 criterion rules
    whenever the dispatch rendered a plan section — and the #2732 contract-test
    rules whenever that section named a contract test. The #3419 coverage rules
    apply, as in production, when the context names the pinned prompt, and the
    #3516 declared tests ride off it the same way.
    """
    return collect_stream_verdict(
        capture_of(reviewer_stream(verdict, ctx.head_sha)),
        head_sha=ctx.head_sha,
        exit_code=exit_code,
        dest_dir=ctx.log_dir,
        run_id=ctx.run_id,
        plan_criterion_ids=ctx.criterion_ids,
        contract_test_ids=ctx.contract_test_ids,
        require_coverage=criteria_contract_in_force(ctx.prompt_artifact_sha),
        declared_tests=ctx.declared_tests,
    )


@dataclass
class FakeReviewerContainer:
    """A container handle that streams fixed lines and exits with a code."""

    lines: list[str]
    exit_code: int = 0

    def __iter__(self) -> Iterator[str]:
        yield from self.lines

    def wait(self) -> int:
        return self.exit_code


def draining_stream(exit_code: int | None = None):  # noqa: ANN201
    """A ``_stream_sandbox_log`` double that consumes the handle's lines.

    The review capture taps the stream the consumer iterates, so a double must
    iterate it for the reviewer's result event to be seen. Returns the
    handle's own exit code unless *exit_code* overrides it.
    """

    def _stream(handle, log_file, st, run_id, *, model, overrides, runner) -> int:  # noqa: ANN001
        for _line in handle:
            pass
        return handle.wait() if exit_code is None else exit_code

    return _stream


def stream_lines(
    ctx: ReviewerLaunchContext, lines: Iterable[str], *, exit_code: int = 0
) -> CollectedSidecar:
    """Collect an arbitrary reviewer stream as a launcher double, through the
    real capture and collector."""
    return collect_stream_verdict(
        capture_of(lines),
        head_sha=ctx.head_sha,
        exit_code=exit_code,
        dest_dir=ctx.log_dir,
        run_id=ctx.run_id,
    )
