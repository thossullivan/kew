"""``kew epic send-back`` — an operator's NEEDS_CHANGES on a READY epic node (#3070).

kew's own reviewer can pass a head that a second review — the operator's own,
or another tool's — finds blockers on. Before this verb the only ways to get
those fixed ended the drive's ownership of the node: ``kew review <PR>``
records the PR number as the run's item, and an operator commit on the agent
branch trips the publication-ancestor refusal.

The send-back instead records the operator's blockers as a review round, the
same way the serial merge queue records a red combined tree (#2148): a
NEEDS_CHANGES round against the head the READY verdict stands on, whose stored
sidecar carries one ``high`` finding with the operator's reason. That head must
still be the pull request's current head, read from the forge: a round recorded
on a head no reviewer passed would become the base of the next delta review, so
the commits between the READY head and it (a merge-main step's conflict
resolution, say) would merge without ever being reviewed. A head that moved is
refused until the drive has reviewed it; a merged, closed or missing pull
request has nothing left to send back and is refused too. The overlay
then reads the node as remediating, and the drive's own review stage runs the
remediation under the node's item id — so ownership, the claim binding and the
run lineage stay intact. The round is attested as an
``adversarial_review_verdict`` with the human as its actor, and the reason
rides on an ``epic_node_send_back`` event beside it.

The write runs inside the item's review reservation, the per-item mutex the
reviewer and remediator take: a send-back can never land under a reviewer or a
remediation in flight, and every refusal is re-checked inside it. The merge pass
does not take that reservation; it re-reads the latest round immediately before
it merges instead (``kew.graph.driver.merge``), so a send-back recorded while
the merge queue verifies the node still stops the merge.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from kew import review_status
from kew.audit import emit, resolve_actor
from kew.epic_holds import safe_hold_reason
from kew.ids import GITHUB_ID_RE
from kew.pseudo_epics import REVIEW_EPIC_REF
from kew.review_audit import VERDICT_DEGRADED, emit_review_verdict
from kew.review_contract import operator_send_back_sidecar
from kew.review_delta import (
    REVIEW_MODE_OPERATOR_SEND_BACK,
    VERDICT_NEEDS_CHANGES,
    VERDICT_READY,
)
from kew.review_residuals import READY_WITH_RESIDUALS

if TYPE_CHECKING:
    from kew.config import DispatchConfig
    from kew.run_store import ReviewRound, RunStore

SEND_BACK_EVENT = "epic_node_send_back"

#: Longest reason accepted, in characters. The reason becomes one finding in
#: the remediation brief; a whole review pasted in belongs in the PR, linked.
MAX_REASON_CHARS = 16_000


class SendBackRefused(ValueError):
    """The send-back recorded nothing: the node is not in a state it applies to."""


@dataclass(frozen=True)
class PullRequestHead:
    """The node's pull request as the forge reports it now.

    ``number`` is None when the node has no linked pull request, or the read
    failed; ``state`` is the forge's ``OPEN``/``MERGED``/``CLOSED`` (anything
    else, ``None`` included, is an unreadable state); ``head_sha`` is None when
    the head could not be read.
    """

    number: int | None
    state: str | None
    head_sha: str | None


@dataclass(frozen=True)
class SendBack:
    """What a send-back recorded."""

    item_id: str
    head_sha: str
    round: int
    #: The pull request read MERGED right after the round was recorded: it
    #: merged before the send-back could stop it, and the round changes nothing.
    merged_meanwhile: bool = False


def _ready_round(
    store: RunStore, epic_ref: str, item_id: str, *, advisory: bool
) -> ReviewRound:
    """The READY round the send-back answers, or :class:`SendBackRefused`.

    READY, a NEEDS_CHANGES round the stop rule accepted as READY with
    residuals, or — under advisory review (``[review] blocking = false``) — a
    DEGRADED round: the verdicts that read IN_REVIEW, awaiting merge. Anything
    else is already a round the drive is acting on, and the refusal names the
    review mode it was read under.
    """
    holds = [h for h in store.list_epic_holds(epic_ref) if h.item_id == item_id]
    if holds:
        raise SendBackRefused(
            f"held ({safe_hold_reason(holds[0].reason)}): a send-back cannot "
            f"clear a hold — run kew epic unhold {epic_ref} {item_id} first"
        )
    latest = store.latest_review_round(item_id)
    if latest is None or latest.epic_ref != epic_ref:
        raise SendBackRefused(
            f"no review round of epic {epic_ref!r} is recorded for {item_id}"
        )
    if not _awaits_merge(latest, advisory=advisory):
        mode = "advisory" if advisory else "blocking"
        raise SendBackRefused(
            f"round {latest.round} stands at {latest.verdict} under {mode} "
            "review, not READY — the drive is already acting on it"
        )
    return latest


def _awaits_merge(latest: ReviewRound, *, advisory: bool) -> bool:
    """Whether *latest* is a verdict the drive reads as awaiting merge."""
    if latest.verdict == VERDICT_READY:
        return True
    if latest.accepted_verdict == READY_WITH_RESIDUALS:
        return True
    return advisory and latest.verdict == VERDICT_DEGRADED


def _open_head(pr: PullRequestHead, item_id: str) -> str:
    """The open pull request's current head, or :class:`SendBackRefused`."""
    if pr.number is None:
        raise SendBackRefused(
            f"no pull request is linked to {item_id} (or it could not be read)"
        )
    state = (pr.state or "").upper()
    if state in {"MERGED", "CLOSED"}:
        raise SendBackRefused(
            f"PR #{pr.number} is {state.lower()}: there is nothing left to send back"
        )
    if state != "OPEN":
        raise SendBackRefused(f"PR #{pr.number}'s state could not be read")
    if not pr.head_sha:
        raise SendBackRefused(f"PR #{pr.number}'s head could not be read")
    return pr.head_sha


def _same_head(pr: PullRequestHead, ready: ReviewRound, item_id: str) -> None:
    """Refuse unless the open PR still stands at the head *ready* passed.

    A send-back round on any other head would be the base the next delta review
    diffs from, so the commits between the READY head and it would merge
    without a reviewer ever reading them.
    """
    head = _open_head(pr, item_id)
    if head != ready.head_sha:
        raise SendBackRefused(
            f"PR #{pr.number}'s head moved since its READY review "
            f"({ready.head_sha[:12]} → {head[:12]}); wait for the drive to "
            "review the new head, then send it back"
        )


def _checked_reason(reason: str) -> str:
    text = reason.strip()
    if not text:
        raise SendBackRefused("the reason is empty")
    if len(text) > MAX_REASON_CHARS:
        raise SendBackRefused(
            f"the reason is {len(text)} characters; the limit is {MAX_REASON_CHARS}"
        )
    return text


def epic_send_back(
    store: RunStore,
    *,
    epic_ref: str,
    item_id: str,
    reason: str,
    config: DispatchConfig,
    read_pr: Callable[[], PullRequestHead],
    source: str = "cli",
) -> SendBack:
    """Record an operator NEEDS_CHANGES against the head *item_id*'s READY stands on.

    *read_pr* reads the node's pull request from the forge: once after the
    cheap refusals, to prove the READY head is still the PR's head, and once
    after the round is recorded, to report a PR that merged in between
    (:attr:`SendBack.merged_meanwhile`).

    Raises :class:`SendBackRefused`, having recorded nothing, when the reason is
    empty or too long, the node is held, its latest round is not READY, it has
    no linked pull request, its pull request is merged, closed or unreadable,
    its head moved since the READY review, or a reviewer, remediation or
    dispatch holds the node's reservation.
    """
    text = _checked_reason(reason)
    advisory = not config.review.blocking
    seen = _ready_round(store, epic_ref, item_id, advisory=advisory)
    _same_head(read_pr(), seen, item_id)
    mutex = str(uuid.uuid4())
    if not store.reserve_epic_node_item(REVIEW_EPIC_REF, item_id, mutex):
        raise SendBackRefused(
            f"a run holds {item_id}'s reservation — send it back once that run ends"
        )
    try:
        latest = _ready_round(store, epic_ref, item_id, advisory=advisory)
        if latest.round != seen.round:
            raise SendBackRefused(
                f"round {latest.round} was recorded while the send-back ran — "
                "re-read the node and send it back again"
            )
        sent = _record(
            store,
            epic_ref=epic_ref,
            latest=latest,
            reason=text,
            config=config,
            source=source,
        )
    finally:
        store.release_epic_node_item(item_id, run_id=mutex)
    # Advisory only: the round is recorded, so a forge read that fails here must
    # not turn the send-back into an error.
    try:
        after = read_pr()
    except Exception:  # noqa: BLE001 — any forge failure leaves the report off
        return sent
    if (after.state or "").upper() == "MERGED":
        return replace(sent, merged_meanwhile=True)
    return sent


def _record(
    store: RunStore,
    *,
    epic_ref: str,
    latest: ReviewRound,
    reason: str,
    config: DispatchConfig,
    source: str,
) -> SendBack:
    """Write the sidecar and the round, then attest both (merge queue order)."""
    actor = resolve_actor()
    item_id, head = latest.item_id, latest.head_sha
    sidecar = operator_send_back_sidecar(
        Path(config.logging.dir).resolve(),
        f"send-back-{item_id.replace('/', '-')}-{head[:12]}-{uuid.uuid4().hex[:8]}",
        head_sha=head,
        actor=actor.id,
        reason=reason,
    )
    round_num = store.record_review_round(
        epic_ref,
        item_id,
        head_sha=head,
        verdict=VERDICT_NEEDS_CHANGES,
        sidecar_digest=sidecar.digest,
        sidecar_path=str(sidecar.stored_path),
        review_mode=REVIEW_MODE_OPERATOR_SEND_BACK,
    )
    repo = config.project.repo or None
    emit_review_verdict(
        store,
        config.review,
        pr_number=None,
        item_id=item_id,
        head_sha=head,
        review_round=round_num,
        verdict=VERDICT_NEEDS_CHANGES,
        sidecar_digest=sidecar.digest,
        review_mode=REVIEW_MODE_OPERATOR_SEND_BACK,
        repo=repo,
        actor=actor,
    )
    emit(
        store,
        SEND_BACK_EVENT,
        actor=actor,
        issue_num=int(item_id) if GITHUB_ID_RE.fullmatch(item_id) else 0,
        item_id=item_id,
        repo=repo,
        payload={
            "epic_ref": epic_ref,
            "node": item_id,
            "head_sha": head,
            "round": round_num,
            "ready_round": latest.round,
            "reason": reason,
            "reason_sha256": hashlib.sha256(reason.encode()).hexdigest(),
            "sidecar_digest": sidecar.digest,
            "source": source,
        },
    )
    if repo is not None and config.review.publish_status:
        review_status.publish_verdict(repo, head, VERDICT_NEEDS_CHANGES)
    return SendBack(item_id=item_id, head_sha=head, round=round_num)
