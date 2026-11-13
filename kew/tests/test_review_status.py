"""Unit tests for the ``kew-review`` GitHub commit-status publisher (#1056).

The publisher mirrors the adversarial-review verdict onto the PR head SHA as a
``kew-review`` commit status branch protection on ``main`` can require. These
tests pin the gh argv (context/state keyed to the exact head SHA) and the
best-effort contract: a resolve/transport error or a non-zero ``gh`` exit is
swallowed, never raised, so a status hiccup can never crash the driver tick.
"""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

from kew import review_status


def _record_run(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Capture argv passed to ``subprocess_utils.run`` (returns a 0-exit result)."""
    calls: list[list[str]] = []

    def _fake_run(cmd: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(review_status.subprocess_utils, "run", _fake_run)
    return calls


def test_publish_pending_posts_pending_keyed_to_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record_run(monkeypatch)
    review_status.publish_pending("o/r", "deadbeef")
    assert calls == [
        [
            "gh",
            "api",
            "-X",
            "POST",
            "repos/o/r/statuses/deadbeef",
            "-f",
            "state=pending",
            "-f",
            "context=kew-review",
            "-f",
            "description=adversarial review in progress",
        ]
    ]


def test_publish_verdict_ready_is_success(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _record_run(monkeypatch)
    review_status.publish_verdict("o/r", "sha1", "READY")
    (cmd,) = calls
    assert cmd[4] == "repos/o/r/statuses/sha1"
    assert "state=success" in cmd


@pytest.mark.parametrize("verdict", ["NEEDS_CHANGES", "DEGRADED"])
def test_publish_verdict_non_ready_is_failure(
    monkeypatch: pytest.MonkeyPatch, verdict: str
) -> None:
    calls = _record_run(monkeypatch)
    review_status.publish_verdict("o/r", "sha2", verdict)
    (cmd,) = calls
    assert "state=failure" in cmd
    assert f"description=adversarial review: {verdict}" in cmd


def test_publish_advisory_degraded_posts_truthful_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record_run(monkeypatch)
    review_status.publish_advisory_degraded("o/r", "sha2")
    (cmd,) = calls
    assert "context=kew-review" in cmd
    assert "state=success" in cmd
    assert "description=adversarial review: DEGRADED (advisory)" in cmd


def test_publish_failure_holds_the_check_red(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _record_run(monkeypatch)
    review_status.publish_failure("o/r", "sha3", "rounds exhausted")
    (cmd,) = calls
    assert "state=failure" in cmd
    assert "description=adversarial review held: rounds exhausted" in cmd


def test_non_zero_gh_exit_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed ``gh`` POST must never raise — the tick keeps going."""

    def _fail(cmd: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="HTTP 422")

    monkeypatch.setattr(review_status.subprocess_utils, "run", _fail)
    review_status.publish_pending("o/r", "sha")  # does not raise


def test_resolve_error_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """gh absent from the allowlist (ValueError from resolve_cmd) is swallowed."""

    def _raise(cmd: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        raise ValueError("Unknown executable 'gh'")

    monkeypatch.setattr(review_status.subprocess_utils, "run", _raise)
    review_status.publish_verdict("o/r", "sha", "READY")  # does not raise
