"""Publish the adversarial-review verdict as a GitHub commit status (#1056).

kew's blocking merge-pass invariant (#988) gates kew's OWN merge; it cannot
reach the GitHub *merge button*. A human — or an autonomous flow — can click
"Merge" on GitHub before the reviewer finishes, and the verdict then lands on an
already-merged PR (live incident #1013 / PR #1049: merged 4 minutes before the
reviewer returned NEEDS_CHANGES). This module mirrors the SAME verdict onto the
PR head SHA as a ``kew-review`` commit status that a branch-protection rule on
``main`` can REQUIRE — the GitHub-native gate that actually blocks the button,
which code-side ``blocking`` cannot.

The status is keyed to the exact head SHA the verdict attests, so a stale review
of an old head can never satisfy it (mirrors the merge-pass F1 head-sha binding).
A post-verdict force-push moves the head, and the next reviewer round re-posts
``pending`` on the new SHA — the protection follows the code, not a frozen review.

Posting is BEST-EFFORT: any ``gh`` / GitHub failure is logged and swallowed,
never raised, so a status hiccup can never crash the driver tick (the same
fail-safe contract the rest of the review stage honors). This is an ADDITIONAL
gate; kew's own merge-pass invariant remains the source of truth for the verdict.

Lifecycle of the ``kew-review`` commit status: (1) ``pending`` — posted when a
reviewer round is dispatched (fresh PR or re-review after a force-push); (2)
``success`` — posted when ``READY`` is recorded for the current head SHA, or
when the stop rule accepts a late low/medium round with its residuals filed; (3)
``failure`` — posted for any non-READY verdict (``NEEDS_CHANGES``, or crash-path
``HELD``/``DEGRADED``); (4) back to ``pending`` — a post-verdict force-push moves
the head and the next reviewer round re-posts ``pending`` on the new SHA, so the
protection always tracks the live code.
"""

from __future__ import annotations

import logging

from kew import subprocess_utils

_logger = logging.getLogger(__name__)

# The commit-status context branch protection requires on ``main``.
KEW_REVIEW_CONTEXT = "kew-review"

# GitHub commit-status states (POST .../statuses/{sha}).
_STATE_PENDING = "pending"
_STATE_SUCCESS = "success"
_STATE_FAILURE = "failure"

_VERDICT_READY = "READY"


def _post(repo: str, sha: str, *, state: str, description: str) -> None:
    """POST a ``kew-review`` commit status for ``sha`` — best-effort (never raises).

    Uses a fixed argv through the subprocess allowlist (no shell, no agent input;
    ``gh`` is already a required tool). A resolve/transport error or a non-zero
    ``gh`` exit is logged and swallowed so the driver tick is never crashed by a
    status-publish failure.
    """
    try:
        result = subprocess_utils.run(
            [
                "gh",
                "api",
                "-X",
                "POST",
                f"repos/{repo}/statuses/{sha}",
                "-f",
                f"state={state}",
                "-f",
                f"context={KEW_REVIEW_CONTEXT}",
                "-f",
                f"description={description}",
            ],
            capture=True,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001 — status publish is strictly best-effort
        _logger.warning(
            "review-status: could not POST %s for %s@%s (swallowed): %s",
            state,
            repo,
            sha[:12],
            exc,
        )
        return
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()[:200]
        _logger.warning(
            "review-status: gh POST %s for %s@%s failed (rc=%d): %s",
            state,
            repo,
            sha[:12],
            result.returncode,
            stderr,
        )


def publish_pending(repo: str, sha: str) -> None:
    """Adversarial review dispatched / in progress on ``sha`` → pending.

    Posted the moment a reviewer round is due (a fresh PR, or a re-review after a
    force-push moved the head), so the ``kew-review`` check is red-until-decided
    and the GitHub merge button is gated while the reviewer runs.
    """
    _post(
        repo,
        sha,
        state=_STATE_PENDING,
        description="adversarial review in progress",
    )


def publish_verdict(repo: str, sha: str, verdict: str) -> None:
    """Flip the ``kew-review`` status to mirror a recorded verdict on ``sha``.

    READY → success; every other verdict — NEEDS_CHANGES, or the advisory
    crash-path DEGRADED — → failure, and it stays failure until a later round on
    the CURRENT head records READY (or a force-push returns it to pending).
    """
    if verdict == _VERDICT_READY:
        _post(
            repo,
            sha,
            state=_STATE_SUCCESS,
            description="adversarial review passed",
        )
        return
    _post(
        repo,
        sha,
        state=_STATE_FAILURE,
        description=f"adversarial review: {verdict}",
    )


def publish_advisory_degraded(repo: str, sha: str) -> None:
    """Attest an advisory DEGRADED verdict without blocking delivery.

    The successful status is intentionally limited to this dedicated path:
    GitHub can compute a green aggregate only when all other required checks
    pass, while the description preserves the non-READY shadow verdict.
    """
    _post(
        repo,
        sha,
        state=_STATE_SUCCESS,
        description="adversarial review: DEGRADED (advisory)",
    )


def publish_residuals_accepted(repo: str, sha: str, issue: int) -> None:
    """The stop rule accepted a late low/medium round on ``sha`` → success.

    The acceptance is on record with its filed residual issue (#2112), so the
    merge gate reads the round as READY; the description names the issue so
    the green check never reads as a clean review.
    """
    _post(
        repo,
        sha,
        state=_STATE_SUCCESS,
        description=f"adversarial review: READY with residuals (#{issue})",
    )


def publish_failure(repo: str, sha: str, reason: str) -> None:
    """Force the ``kew-review`` status to failure on ``sha`` for a crash-path HELD.

    The blocking round-cap (rounds exhausted) and unparseable/infra crash paths
    HELD the node without recording a READY, so there is no verdict to mirror —
    surface the block to GitHub directly so the required check stays red.
    """
    _post(
        repo,
        sha,
        state=_STATE_FAILURE,
        description=f"adversarial review held: {reason}",
    )
