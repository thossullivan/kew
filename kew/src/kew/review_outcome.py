"""Persist and notify the terminal outcome of a review dispatch (#1291).

A review used to report itself only to whoever was watching: an exit code and
some progress lines. Run ``kew review <PR>`` from a cron slot, a service unit,
or any other non-interactive context and a completion — or, worse, a *failure
before launch* — left no durable trace: nothing to query afterwards, nobody
notified. The outcome existed only for as long as a watcher was attached.

This module makes the outcome a watcher-independent fact. Every terminal path
of :func:`kew.review.dispatch_review` records a :class:`ReviewOutcome`, which is

1. appended to the audit trail, so it survives process exit and stays queryable
   (``kew audit tail --event review.outcome``, or :func:`list_review_outcomes`);
2. pushed to the configured notifier — best-effort, never fatal.

State first, best-effort I/O second: the outcome is persisted before anything is
notified, so a notifier outage can never cost the durable record.

The new attested fact binds through the audit ``payload`` dict — never a new
column or ``entry_hash`` preimage slot (``audit-chain-change`` skill).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from kew.audit import Actor, emit
from kew.notifier import NotificationEvent, Notifier, build_notifier

if TYPE_CHECKING:
    from kew.config import DispatchConfig
    from kew.run_store import RunStore

_logger = logging.getLogger(__name__)

#: Audit event type carrying every review outcome.
REVIEW_OUTCOME_EVENT = "review.outcome"

#: The host dispatcher itself is the actor — not the reviewing agent.
_ACTOR = Actor("policy", "review-dispatch")

STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
#: Stopped before launch by the operator's own configuration (#3023): nothing
#: failed, and only the operator's change resumes it.
STATUS_REFUSED = "refused"

STAGE_PRE_LAUNCH = "pre_launch"
STAGE_RUNNER = "runner"
STAGE_PUBLICATION = "publication"


@dataclass(frozen=True)
class ReviewOutcome:
    """One terminal review result, durable and queryable after the fact."""

    pr_number: int
    status: str
    stage: str
    classification: str
    detail: str = ""
    branch: str = ""
    head_sha: str = ""
    item_id: str | None = None
    exit_code: int = 0
    cut_head_sha: str = ""
    """The head a completed run's checkout was cut at, beside the *head_sha* it
    published (#3276). Empty when not recorded. Only the trees below tell an
    agent's change from kew's own merge of the base; outcomes recorded before
    them are judged on these two commit ids."""
    integrated_tree: str = ""
    """The tree kew's own base integration left in the checkout, before the
    agent ran. Empty when not recorded, or when the merge stopped on conflicts
    the agent had to finish."""
    published_tree: str = ""
    """The tree of the *head_sha* a completed run published. Equal to
    *integrated_tree* when the agent changed nothing, however far kew's merge
    of the base moved the commit (#3276). Empty when not recorded."""

    @property
    def ok(self) -> bool:
        """True when the review reached a successful completion."""
        return self.status == STATUS_COMPLETED

    def as_payload(self) -> dict[str, Any]:
        """The audit payload — the only place new review facts may bind."""
        return {
            "pr_number": self.pr_number,
            "status": self.status,
            "stage": self.stage,
            "classification": self.classification,
            "detail": self.detail,
            "branch": self.branch,
            "head_sha": self.head_sha,
            "item_id": self.item_id,
            "exit_code": self.exit_code,
            "cut_head_sha": self.cut_head_sha,
            "integrated_tree": self.integrated_tree,
            "published_tree": self.published_tree,
        }

    def message(self) -> str:
        """A one-line human summary for the notification body."""
        head = f" @{self.head_sha[:12]}" if self.head_sha else ""
        if self.ok:
            return f"Review of PR #{self.pr_number}{head} completed."
        detail = f": {self.detail}" if self.detail else ""
        if self.status == STATUS_REFUSED:
            return (
                f"Review of PR #{self.pr_number}{head} was refused before launch "
                f"({self.classification}){detail}"
            )
        if self.classification == "publication_head_moved":
            return (
                f"Review of PR #{self.pr_number}{head} completed but was not "
                f"published because the branch moved{detail}."
            )
        return (
            f"Review of PR #{self.pr_number}{head} failed at {self.stage} "
            f"({self.classification}){detail}"
        )


def _payload_to_outcome(payload: dict[str, Any]) -> ReviewOutcome | None:
    """Rebuild a :class:`ReviewOutcome` from a stored audit payload."""
    try:
        return ReviewOutcome(
            pr_number=int(payload["pr_number"]),
            status=str(payload["status"]),
            stage=str(payload["stage"]),
            classification=str(payload["classification"]),
            detail=str(payload.get("detail") or ""),
            branch=str(payload.get("branch") or ""),
            head_sha=str(payload.get("head_sha") or ""),
            item_id=payload.get("item_id"),
            exit_code=int(payload.get("exit_code") or 0),
            cut_head_sha=str(payload.get("cut_head_sha") or ""),
            integrated_tree=str(payload.get("integrated_tree") or ""),
            published_tree=str(payload.get("published_tree") or ""),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _severity(outcome: ReviewOutcome) -> str:
    """A refusal is the operator's own configuration, never an error."""
    if outcome.ok:
        return "info"
    return "warning" if outcome.status == STATUS_REFUSED else "error"


def _notify(
    outcome: ReviewOutcome,
    *,
    config: DispatchConfig,
    notifier: Notifier | None,
) -> None:
    """Push the outcome to the configured sink — never raises."""
    try:
        sink = notifier if notifier is not None else build_notifier(config)
        sink.notify(
            NotificationEvent(
                event_type=REVIEW_OUTCOME_EVENT,
                severity=_severity(outcome),
                message=outcome.message(),
                pr_number=outcome.pr_number,
                metadata=outcome.as_payload(),
            )
        )
    except Exception as exc:  # noqa: BLE001 — notification is strictly best-effort
        _logger.warning(
            "review-outcome: could not notify PR #%d (%s): %s",
            outcome.pr_number,
            outcome.classification,
            exc,
        )


def record_review_outcome(
    outcome: ReviewOutcome,
    *,
    store: RunStore | None,
    config: DispatchConfig,
    notifier: Notifier | None = None,
    notify: bool = True,
) -> None:
    """Persist *outcome* to the audit trail, then notify it.

    Ordering is load-bearing: the durable record is written first so a failing
    notifier can never cost the record. Both halves are best-effort at the
    transport level — recording an outcome must not be able to crash a review.
    """
    try:
        emit(
            store,
            REVIEW_OUTCOME_EVENT,
            actor=_ACTOR,
            issue_num=outcome.pr_number,
            item_id=outcome.item_id,
            repo=config.project.repo or None,
            payload=outcome.as_payload(),
            strict=True,
        )
    except Exception as exc:
        _logger.error(
            "Failed to persist %s for PR #%d (%s): %s",
            REVIEW_OUTCOME_EVENT,
            outcome.pr_number,
            outcome.classification,
            exc,
        )
    if notify:
        _notify(outcome, config=config, notifier=notifier)


def list_review_outcomes(
    store: RunStore,
    *,
    pr_number: int | None = None,
    limit: int = 100,
) -> list[ReviewOutcome]:
    """Recorded review outcomes, newest first (optionally for one PR)."""
    events = store.list_audit_events(
        limit=limit,
        event=REVIEW_OUTCOME_EVENT,
        issue_num=pr_number,
    )
    outcomes = []
    for event in events:
        outcome = _payload_to_outcome(event.payload or {})
        if outcome is not None:
            outcomes.append(outcome)
    return outcomes


def latest_review_outcome(store: RunStore, pr_number: int) -> ReviewOutcome | None:
    """The most recent recorded outcome for *pr_number*, or ``None``."""
    outcomes = list_review_outcomes(store, pr_number=pr_number, limit=1)
    return outcomes[0] if outcomes else None
