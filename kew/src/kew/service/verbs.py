"""Intent-first operator verbs: epic-retry, issue-queue, orphan-ack (#682).

The serve/TUI surfaces reach these mutating verbs only through here (kew.serve
may not import kew.audit / kew.run_store / kew.labels). Each verb follows the
Core security stance: **emit the chained audit event BEFORE the effector runs**
— attest, then act — exactly like ``apply_approval_decision`` and the config
override verbs (``kew.service.config``).

Partial-failure contract: because the event is appended first, an effector that
fails after the emit leaves the event standing and the error propagates to the
caller. This is deliberate: the operator's intent (retry this node, queue this
issue, ack this PR) is a fact the moment it is issued, independent of whether
the downstream label/store write then succeeded. A lost effector can be retried;
a lost intent record would silently erase who asked for what.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from kew.approval import _authorized
from kew.audit import (
    Actor,
    audit_fields,
    bind_policy_context,
    emit,
    record_current_policy,
    resolve_actor,
)
from kew.audit_identity import continuation_evidence
from kew.continuation_recovery import (
    continuation_histories,
    replacement_was_withdrawn,
    selectable_continuation,
)
from kew.epic_archive import (
    EPIC_ARCHIVED_EVENT,
    EPIC_UNARCHIVED_EVENT,
    ArchiveState,
    archive_states,
)
from kew.epic_holds import (
    holds_node_out_of_frontier,
    is_dispatch_refusal_hold,
    safe_hold_reason,
)
from kew.human_review import (
    ReviewPolicy,
    RiskConfirmation,
    authorize_approver,
    policy_from_config,
)
from kew.ids import GITHUB_ID_RE, provider_from_config, validate_item_id
from kew.labels import mark_ready, mark_ready_from_failed
from kew.no_change import (
    AUTHORIZED,
    COMPLETED,
    EFFECT_PENDING,
    LIFECYCLE,
    PENDING_CONFIRMATION,
    SYNC_PENDING,
    TERMINAL_STATES,
    NoChangeStateError,
    RepositoryAuthority,
    resolve_no_change_authority,
)
from kew.outcomes import (
    BaseComparison,
    compare_judged_base,
    import_review_obligations,
    judged_paths_of,
    name_paths,
    observe_forge_identity,
    observe_repository_authority,
    post_no_change_rejection,
    read_delivery_base,
    revalidate_no_change_source,
    synchronize_no_change,
)
from kew.publication_attest import (
    HeadAttestationRefused,
    head_unclaimable_for_run,
    record_head_attestation,
)
from kew.publication_job import ABANDONED as PUBLICATION_ABANDONED
from kew.publication_job import SLOT_RELEASED
from kew.run_liveness import continuation_owner_death_refusal
from kew.service.epics import driver_liveness
from kew.service.models import WorkActionDTO, WorkActionResultDTO, WorkViewDTO
from kew.service.send_back import PullRequestHead, SendBack, epic_send_back
from kew.service.work import (
    PullRequestFact,
    TrackerItemFact,
    get_work_view,
    pending_run_max_age_s,
)
from kew.subprocess_utils import run
from kew.waits import EPIC_ABANDON_COMMAND, EPIC_CONTINUE_COMMAND, fill_command

if TYPE_CHECKING:
    from kew.config import DispatchConfig
    from kew.run_store import ContinuationRecord, EpicHold, RunRecord, RunStore


class VerbDenied(Exception):
    """Actor is not in ``[approval].approvers`` for a spend-authorizing verb."""


def _live_publication_job(store: RunStore, record: RunRecord) -> Any:
    """The normalized job this run attempt still owns, or None for legacy rows."""
    reader = getattr(store, "publication_job_for_attempt", None)
    if reader is None:
        return None
    job = reader(run_id=record.run_id, attempt_number=record.attempt_number)
    return job if job is not None and job.active else None


def _finalize_publication_job(
    store: RunStore, record: RunRecord, job: Any
) -> RunRecord:
    """Abandon the job and hand back whatever review slot it was holding.

    Release first: a crash between the two leaves a settled-looking job still
    occupying capacity, and abandoning is the decision that makes the hold
    meaningless. Releasing an unheld job is a no-op, so the order is safe to
    retry from either side.
    """
    now = datetime.now(UTC)
    store.release_publication_capacity(  # type: ignore[attr-defined]
        job_id=job.job_id, disposition=SLOT_RELEASED, now=now
    )
    store.mark_publication_state(  # type: ignore[attr-defined]
        job_id=job.job_id,
        expected_state=job.state,
        state=PUBLICATION_ABANDONED,
        detail="finalized by an operator",
        now=now,
    )
    settled = store.get(record.run_id)
    if settled is None:  # pragma: no cover - the row was just read
        raise RuntimeError(f"finalized run {record.run_id!r} disappeared")
    return settled


def _finalized_head(
    store: RunStore,
    record: RunRecord,
    remote_head: Callable[[str], str] | None,
) -> tuple[str | None, str | None]:
    """The remote head this finalize binds to the run, or why there is none.

    The operator published this run's output by hand, so nothing recorded a
    ``publication_intent`` for the head and the next governed remediation on
    the branch would refuse it as unbound (#2124). Finalizing is the moment
    the operator says the remote holds this run's output, so it is the moment
    to name the head — but only a head actually read back from the origin, and
    only a head no other run has claimed already.

    That second condition is what keeps the repair from destroying a binding
    it did not make: two claims on one head refuse as ambiguous wherever the
    binding is read, and a chained claim cannot be withdrawn, so finalizing a
    second run stranded on the same branch must leave the first run's claim
    standing rather than add its own.

    The read itself is injected. This layer may not import the host's git
    surface (the serve/service boundary must not reach ``kew.graph`` through
    it), so ``kew finalize`` passes the operator-origin reader and a surface
    that supplies none names no head — and says so, rather than recording a
    clean absence it never checked.

    An unreachable origin never blocks the repair either: the reason is
    returned so it lands in the finalize event.
    """
    branch = getattr(record, "branch", None)
    if not branch:
        return None, "the run names no branch"
    if remote_head is None:
        return None, (
            "this surface reads no remote head; finalize from `kew finalize` "
            "to name the published head of this run"
        )
    try:
        head = remote_head(branch)
    except Exception as exc:
        # Any read failure is recorded as the reason, never defaulted through.
        return None, f"the remote head of {branch!r} could not be read: {exc}"
    unclaimable = head_unclaimable_for_run(
        store,
        branch=branch,
        head_sha=head,
        run_id=record.run_id,
        item_id=record.item_id,
    )
    return (None, unclaimable) if unclaimable is not None else (head, None)


def _claimed_finalized_head(
    store: RunStore,
    record: RunRecord,
    remote_head: Callable[[str], str] | None,
    *,
    actor: Actor,
    source: str,
) -> tuple[str | None, str | None]:
    """The head this finalize claimed for the run, or why it claimed none.

    The claim is written here, before the finalize event, because the append
    is what settles a race between two finalizers naming one head: only the
    writer that lost it knows it lost, and the finalize event has to say so
    rather than name a head this run does not own (#2124).

    Losing that race never fails the repair. Every reason a claim was withheld
    — an unreadable origin, a head another run holds, a claim that did not
    reach the chain — is returned for the event, exactly like the reasons the
    lookup ahead of it produces.
    """
    head, reason = _finalized_head(store, record, remote_head)
    if head is None:
        return None, reason
    try:
        record_head_attestation(
            store,
            run_id=record.run_id,
            item_id=record.item_id,
            branch=record.branch,
            commit_sha=head,
            parent_shas=(),
            actor=actor,
            source=source,
        )
    except HeadAttestationRefused as exc:
        return None, str(exc)
    return head, None


#: The events that claim a head for a run: the host publisher's own intent and
#: an operator's attestation (``kew.publication_credentials``).
_HEAD_CLAIM_EVENTS = frozenset({"publication_intent", "publication_head_attested"})


def _finalizable(store: RunStore, record: RunRecord) -> tuple[Any, bool]:
    """The live job a finalize settles, and whether the run is a reconciled one.

    Refuses every run that is neither awaiting publication, owned by a live
    job, nor reconciled from its PR with no head claimed yet.
    """
    job = _live_publication_job(store, record)
    reconciled = job is None and _reconciled_without_claim(store, record)
    if job is None and record.status != "awaiting_publication" and not reconciled:
        raise ValueError(f"run {record.run_id!r} is not awaiting publication")
    return job, reconciled


def _reconciled_without_claim(store: RunStore, record: RunRecord) -> bool:
    """Whether the tick settled this run from its PR and nobody claimed its head.

    ``reconcile_awaiting_publication`` settles a run once its PR is seen, but it
    claims no head: the driver never claims one on its own (#3193). The run is
    then ``success`` with an unbound head, and the next governed remediation on
    the branch is refused. ``kew finalize`` is the human claim for exactly that
    run, and only until a claim exists.
    """
    if record.status != "success":
        return False
    events = {
        event.event
        for event in store.list_audit_events(limit=None, run_id=record.run_id)
    }
    return "publication_reconciled" in events and not events & _HEAD_CLAIM_EVENTS


def _require_reconciled_claim(
    record: RunRecord, head: str | None, unattested: str | None
) -> None:
    """Refuse a reconciled run whose head was not claimed.

    Claiming the head is the whole repair for a reconciled run, so a claim that
    did not land fails it before any finalize event is written, and the run
    stays finalizable for another attempt.
    """
    if head is None:
        raise ValueError(
            f"run {record.run_id!r} was reconciled from its PR, but its head "
            f"was not claimed: {unattested}"
        )


def finalize_publication_run(
    store: RunStore,
    *,
    run_id: str,
    config: DispatchConfig,
    source: str = "cli",
    remote_head: Callable[[str], str] | None = None,
) -> RunRecord:
    """Operator repair for a run stranded in host-publication state.

    Validation precedes the intent event so a typo or an unrelated terminal run
    cannot pollute the chain.  Once validated, the operator intent is attested
    before the atomic run/continuation settlement, matching the other service
    verbs' partial-failure contract.

    I14 cuts the finalizer over to the normalized job. A run whose publication
    the job lifecycle owns is settled *there* — and settling it hands back the
    review slot it was holding, so operator repair can never leak the scarce
    thing. Runs with no job are the legacy ones, and keep the overloaded
    ``awaiting_publication`` status they were written under.

    The verified remote head is named as this run's published output, so the
    remediation leg can bind a head the operator pushed by hand (#2124).
    ``remote_head`` is the seam that reads it. A head another run already
    claims is left alone: the repair records why in its own event rather than
    adding a second claim, which would refuse the branch as ambiguous for good.
    The claim is made before the finalize event precisely so the event reports
    the claim that survived — including when a concurrent finalizer on the same
    branch took the head first, which only the losing append can discover.
    """
    record = store.get(run_id)
    if record is None:
        raise ValueError(f"run {run_id!r} was not found")
    job, reconciled = _finalizable(store, record)
    from kew.publication_credentials import ensure_publication_credentials

    ensure_publication_credentials(store, run_id=run_id)
    actor = resolve_actor()
    head, unattested = _claimed_finalized_head(
        store, record, remote_head, actor=actor, source=source
    )
    if reconciled:
        _require_reconciled_claim(record, head, unattested)
    emit(
        store,
        "publication_finalized",
        actor=actor,
        run_id=run_id,
        issue_num=record.issue_num,
        item_id=record.item_id,
        repo=config.project.repo or None,
        payload={
            "previous_status": record.status,
            "job_id": None if job is None else job.job_id,
            "source": source,
            "attested_head": head,
            "head_unattested_reason": unattested,
        },
    )
    if job is not None:
        return _finalize_publication_job(store, record, job)
    if reconciled:
        return record
    if not store.finalize_awaiting_publication(run_id):
        raise RuntimeError(
            f"run {run_id!r} changed state before publication could be finalized"
        )
    finalized = store.get(run_id)
    if finalized is None:  # pragma: no cover - guarded by the successful CAS
        raise RuntimeError(f"finalized run {run_id!r} disappeared")
    return finalized


class ConfirmRequired(Exception):
    """A spend-authorizing verb was invoked without explicit ``confirm: true``."""


def _safe_continuation_token(value: str) -> str:
    """Keep operator hints free of paths, control characters, and shell syntax."""
    safe = "".join(char if char.isalnum() or char in "._:-" else "_" for char in value)
    return safe or "_"


def _awaiting_continuation(store: RunStore, epic_ref: str, item_id: str) -> bool:
    """Return whether an epic item has a non-terminal continuation record."""
    return any(
        row.item_id == item_id and row.state != "terminal"
        for row in store.list_continuations(epic_ref)
    )


def _preserved_work_refusal(
    store: RunStore, epic_ref: str, item_id: str, item_hint: str
) -> str | None:
    """Why retry must not run past the item's preserved work (#2960).

    An ambiguous history names its original runs and no command: continue and
    abandon both refuse it. An incomplete, unabandoned terminal row keeps its
    preserved checkout; a retry would only record a marker, and the next tick
    finds that checkout as pre-existing work, shared by every run that owned
    it, and holds the node again without resuming the bytes. The commands are
    filled by :func:`fill_command`, so a printed one runs as printed or is
    left out.
    """
    history = continuation_histories(store, epic_ref).get(item_id)
    if history is None:
        return None
    if history.owned:
        # Continue refuses while another caller owns one: no command (#2960).
        return (
            f"node {item_hint} has a continuation in progress; retry and "
            "continue wait until its owner finishes"
        )
    if history.ambiguous:
        runs = ", ".join(
            _safe_continuation_token(row.original_run_id) for row in history.preserved
        )
        return (
            f"node {item_hint} keeps {len(history.preserved)} preserved "
            f"continuations (original runs {runs}); continue and abandon are "
            "fail-closed until the continuation history is unambiguous"
        )
    if not history.terminal or any(row.state != "terminal" for row in history.rows):
        # Nothing terminal to resume, or a live row the awaiting refusal names.
        return None
    message = f"node {item_hint} keeps preserved work that retry would not resume"
    continue_command = fill_command(EPIC_CONTINUE_COMMAND, epic=epic_ref, item=item_id)
    abandon_command = fill_command(EPIC_ABANDON_COMMAND, epic=epic_ref, item=item_id)
    if continue_command and abandon_command:
        message += (
            f"; continue it with `{continue_command}`, or discard it with "
            f"`{abandon_command} --reason TEXT`"
        )
    return message


class EpicRetryHeld(ValueError):
    """The node carries a hold the driver honors, so retry refused.

    Retry re-admits a FAILED node; a hold is the record that an operator must
    look at the node before it is re-admitted at all. Clearing one here would
    give every retry (CLI, TUI holds pane, web) the authority `kew epic unhold`
    exists to ceremonialize, so retry refuses instead and names the reason.
    """


def _wedging_epic_hold(store: RunStore, epic_ref: str, item_id: str) -> EpicHold | None:
    """The hold row that will outlive a retry, or None when retry can help."""
    return next(
        (
            row
            for row in store.list_epic_holds(epic_ref)
            if row.item_id == item_id and _outlives_retry(store, row)
        ),
        None,
    )


def _outlives_retry(store: RunStore, hold: EpicHold) -> bool:
    """Whether this hold still stands after a retry re-admits the node.

    Every reason the driver's overlay routes to HELD *by reason* does
    (:func:`kew.epic_holds.holds_node_out_of_frontier`) — retry restores
    admission state, and admission state is not what those gate.

    The driver's own dispatch refusals (``credential_issuer_refused:`` /
    ``preexisting_worktree_refused:``) are conditional, because what they cost
    a retry depends on where the node is. While the node still awaits dispatch
    they cost nothing: the overlay matches neither, the node keeps reading
    FAILED and dispatchable, and the dispatch that follows disproves the
    refusal and drops the row itself
    (``tick._clear_stale_dispatch_refusal_hold``), so retry is the node's
    documented recovery and refusing it would strand it behind a
    `kew epic unhold` it never needed. Once the node is PAST dispatch with the
    row still standing that recovery is gone (#2217): nothing will dispatch it
    again, so no dispatch will disprove the refusal, and a retry would emit its
    audit event and write its marker while the hold stayed — the #2154 "retry
    reported a recovery that did not happen" drift — with the marker resetting
    the review cycle out from under the open PR. So retry refuses there and
    names the `kew epic unhold` that does move the node (and, once the row is
    gone, retry is available again for the fresh cycle).
    """
    if holds_node_out_of_frontier(hold.reason):
        return True
    return is_dispatch_refusal_hold(hold.reason) and _past_dispatch_since(store, hold)


def _past_dispatch_since(store: RunStore, hold: EpicHold) -> bool:
    """Did this node reach the review lifecycle AFTER *hold* was written (#2217)?

    A reviewer round in the node's current cycle recorded at or after
    ``held_at`` is the evidence, and the only evidence this boundary may read:
    a round exists only for a PR a dispatch opened, so the node went past
    dispatch with the row still on it — the shape the driver's overlay holds
    through precedence 8a (``overlay._remediation_wedged``) once the review
    returns findings.

    Rounds recorded BEFORE the hold say the opposite, and the distinction is
    the whole point: a node whose PR was abandoned reads ``todo``, no hold is
    honored for it, and the frontier refusal that wrote this row happened while
    it was dispatchable. Refusing retry there would both strand the node behind
    an unhold it never needed AND leave its rounds standing — `kew epic unhold`
    is not a writer of the ``epic_retries`` marker, so only a retry restarts
    the cycle those abandoned rounds are still counted against
    (``review.max_rounds``). ``held_at`` is the hold's START and survives
    re-holds of the same row, so the comparison stays honest across them.
    """
    latest = store.latest_review_round(hold.item_id)
    return latest is not None and latest.recorded_at >= hold.held_at


def _refuse_unretryable(store: RunStore, epic_ref: str, item_id: str) -> None:
    """Raise when this node's state makes a re-admission a lie.

    Every refusal runs before the audit emit and every effector, so a refused
    retry records nothing at all: no marker, no label edit, no event.
    """
    epic_hint = _safe_continuation_token(epic_ref)
    item_hint = _safe_continuation_token(item_id)
    hold = _wedging_epic_hold(store, epic_ref, item_id)
    if hold is not None:
        raise EpicRetryHeld(
            f"node {item_hint} is held ({safe_hold_reason(hold.reason)}): retry "
            f"cannot clear a hold — run `kew epic unhold {epic_hint} {item_hint}`"
        )
    refusal = _preserved_work_refusal(store, epic_ref, item_id, item_hint)
    if refusal is not None:
        raise ValueError(refusal)
    if _awaiting_continuation(store, epic_ref, item_id):
        raise ValueError(
            f"node {item_hint} has preserved dirty work; continue it with "
            f"`kew epic continue {epic_hint} {item_hint}`"
        )


def _retry_payload(
    epic_ref: str, item_id: str, source: str, detail: Mapping[str, str] | None
) -> dict[str, str]:
    """The retry event's payload: *detail*, then the node it names."""
    return {**(detail or {}), "epic_ref": epic_ref, "node": item_id, "source": source}


def _refuse_retry(
    store: RunStore,
    epic_ref: str,
    item_id: str,
    precheck: Callable[[], None] | None,
) -> None:
    """Every refusal a retry's write re-runs under its lock."""
    _refuse_unretryable(store, epic_ref, item_id)
    if precheck is not None:
        precheck()


def epic_retry(
    store: RunStore,
    *,
    epic_ref: str,
    item_id: str,
    config: DispatchConfig,
    source: str = "cli",
    actor: Actor | None = None,
    detail: Mapping[str, str] | None = None,
    precheck: Callable[[], None] | None = None,
) -> None:
    """Re-admit a FAILED epic node — audit event first, then the effectors.

    Keyed on the durable ``item_id`` so a non-numeric tracker (Linear) node
    retries too, not just GitHub-numbered nodes. Emits ``epic_node_retry``
    (actor, node id, epic_ref) then runs the effectors:

    * GitHub node (``item_id`` matches :data:`~kew.ids.GITHUB_ID_RE`): clear the
      ``agent-failed`` label, release the stale reservation, and record the
      retry marker on the int issue number (its stringified id) — the exact
      prior ``kew epic retry`` semantics.
    * Non-numeric node: no GitHub label exists, so release the item-keyed
      reservation and record the item-keyed retry marker the driver overlay
      reads (mirrors the CLI's Linear branch).

    A node held for a reason the driver's overlay honors is refused with
    :class:`EpicRetryHeld` before the emit (#2154): retry restores admission
    state, and admission state is not what a hold gates — the node stayed HELD
    while retry reported success, so the operator learned nothing and ran
    `kew epic unhold` anyway. A hold the overlay ignores leaves the node FAILED
    and dispatchable, and retry still runs for it — until a reviewer round
    recorded after that row says the node went past dispatch carrying it, which
    is when nothing will dispatch it again (see :func:`_outlives_retry`).

    The event and the retry marker commit in one transaction that re-runs
    every refusal first (#2960), so a state change racing the first read
    cannot slip a retry past preserved work.

    Repo is read from ``config.project.repo`` (the CLI resolves it first). If an
    effector raises after the emit, the event stands and the error propagates
    (see the module docstring's partial-failure contract).

    The driver re-admits a node by this same path (#3191): *actor* names it
    instead of the acting human, *detail* rides in the event's payload (why,
    and at which head), and *precheck* is one more refusal the write re-runs.
    """
    _refuse_retry(store, epic_ref, item_id, precheck)
    is_github = GITHUB_ID_RE.fullmatch(item_id) is not None
    # The refusals run again inside the write: a continuation that turned
    # terminal after the read above must still stop the event and the marker,
    # which commit together or not at all (#2960).
    store.mark_epic_retry_item_attested(
        epic_ref,
        item_id,
        audit=audit_fields(
            actor=actor or resolve_actor(),
            # 0-sentinel issue_num for a non-numeric tracker; the durable id
            # rides in the payload's ``node`` field regardless of currency.
            issue_num=int(item_id) if is_github else 0,
            repo=config.project.repo or None,
            payload=_retry_payload(epic_ref, item_id, source, detail),
        ),
        precheck=partial(_refuse_retry, store, epic_ref, item_id, precheck),
    )
    reservation = next(
        (
            row
            for row in store.list_epic_reservations(epic_ref)
            if row.item_id == item_id
        ),
        None,
    )
    if reservation is not None:
        observed_run = store.get(reservation.run_id)
        # Missing run evidence is indeterminate; pending is live. Only an
        # observed terminal generation can be released, and the conditional
        # DELETE cannot remove a concurrent replacement generation.
        if observed_run is not None and observed_run.status != "pending":
            store.release_epic_node_item(item_id, run_id=reservation.run_id)
    if is_github:
        # One GitHub edit restores the configured admission predicate and
        # clears every mutually-exclusive failure label together.
        mark_ready_from_failed(int(item_id), config)


class EpicResumeDenied(ValueError):
    """The persisted continuation cannot be claimed by this caller."""


class ContinuationFleetFull(EpicResumeDenied):
    """The fleet worker cap refused the continuation's slot."""


class ContinuationAlreadyClaimed(EpicResumeDenied):
    """Another caller won the continuation-claim compare-and-set.

    ``winner`` is the record re-read after the loss: the claim that holds the
    continuation now, or None when it no longer reads as continuing.
    """

    def __init__(self, message: str, winner: ContinuationRecord | None) -> None:
        super().__init__(message)
        self.winner = winner


class EpicAbandonDenied(ValueError):
    """The operator verb ran, but the continuation cannot be abandoned."""


def _continuation_operator(config: DispatchConfig) -> Actor:
    """Use the same local operator identity and approver policy for both verbs."""
    actor = resolve_actor()
    approvers = config.epic.approvers or config.approval.approvers
    if actor.id == "unknown" or (approvers and actor.id not in approvers):
        raise VerbDenied(f"actor {actor.id!r} lacks epic operator authority")
    return actor


class ContinuationSelectionDenied(ValueError):
    """The continuation set is not in a state that can be selected."""


def _continuation_selection_error(
    records: list[ContinuationRecord],
    awaiting: list[ContinuationRecord],
    epic_ref: str,
) -> ContinuationSelectionDenied:
    """Build the caller-formatted error for a non-selectable record set."""
    if not records:
        epic_hint = repr(epic_ref).replace("{", "{{").replace("}", "}}")
        return ContinuationSelectionDenied(
            "no preserved continuation recorded for {item_id} in epic " + epic_hint
        )
    if any(record.state == "terminal" for record in records):
        if awaiting:
            return ContinuationSelectionDenied(
                "multiple continuation states recorded for {item_id}; "
                "resume is fail-closed until the identity is unambiguous"
            )
        return ContinuationSelectionDenied(
            "{item_id} has already reached a terminal state"
        )
    if len(awaiting) > 1:
        return ContinuationSelectionDenied(
            "multiple preserved continuations recorded for {item_id}; "
            "resume is fail-closed until the identity is unambiguous"
        )
    return ContinuationSelectionDenied("{item_id} has no resumable continuation")


def _selectable_continuation_records(
    store: RunStore, epic_ref: str, item_id: str
) -> list[ContinuationRecord]:
    """Explain a withdrawn replacement without restoring abandoned work."""
    item_records = [
        record
        for record in store.list_continuations(epic_ref)
        if record.item_id == item_id
    ]
    records = [
        record
        for record in item_records
        if not store.continuation_was_abandoned(record)
    ]
    if not records and any(replacement_was_withdrawn(store, r) for r in item_records):
        raise ContinuationSelectionDenied(
            "{item_id} replacement was withdrawn before launch and its checkout "
            "was removed; the old continuation remains abandoned; use fresh dispatch"
        )
    return records


def select_awaiting_continuation(
    store: RunStore, epic_ref: str, item_id: str
) -> ContinuationRecord:
    """Select the one unambiguous awaiting continuation for an epic item.

    The selector deliberately formats its error with an ``{item_id}``
    placeholder. CLI and service callers can then choose their own item label
    without duplicating the fail-closed state ladder.
    """
    records = _selectable_continuation_records(store, epic_ref, item_id)
    for record in records:
        if record.state == "continuing":
            raise ContinuationSelectionDenied(
                "{item_id} already has a continuation owned by another caller"
                f" (run {record.continuation_run_id!r}, generation {record.generation})"
            )
    selected = selectable_continuation(store, records)
    if selected is not None:
        return selected
    awaiting = [
        record for record in records if store.continuation_is_recoverable(record)
    ]
    raise _continuation_selection_error(records, awaiting, epic_ref)


def _same_continuation_identity(
    actual: ContinuationRecord, expected: ContinuationRecord
) -> bool:
    """Return whether two records name the same validated generation."""
    return (
        actual.original_run_id,
        actual.branch,
        actual.worktree_identity,
        actual.generation,
    ) == (
        expected.original_run_id,
        expected.branch,
        expected.worktree_identity,
        expected.generation,
    )


_CLAIM_EVIDENCE_TOKENS = (
    "epic_ref",
    "item_id",
    "original_run_id",
    "branch",
    "continuation_run_id",
)


def _claim_identity_refusal(
    store: RunStore, record: ContinuationRecord, run_id: str
) -> str | None:
    """Why this claim's identity cannot be recorded, else None.

    The claim evidence stores sanitized tokens; an identity the tokens do not
    reproduce would let two distinct claims share one evidence identity (and
    one dedup key), so it is refused before anything is claimed. The run id
    the service generated must also be new everywhere a run id can live.
    """
    evidence = continuation_evidence(
        phase="claim",
        epic_ref=record.epic_ref,
        item_id=record.item_id,
        original_run_id=record.original_run_id,
        continuation_run_id=run_id,
        branch=record.branch,
        worktree_identity=record.worktree_identity,
    )
    raw = {
        "epic_ref": record.epic_ref,
        "item_id": record.item_id,
        "original_run_id": record.original_run_id,
        "branch": record.branch,
        "continuation_run_id": run_id,
    }
    for field in _CLAIM_EVIDENCE_TOKENS:
        if evidence.get(field) != raw[field]:
            return (
                f"the continuation {field} {raw[field]!r} is not representable "
                "in claim evidence"
            )
    if store.continuation_run_id_in_use(run_id):
        return f"continuation run id {run_id!r} is already in use"
    return None


def _claim_winner(
    store: RunStore, record: ContinuationRecord
) -> ContinuationRecord | None:
    """Re-read the continuation a lost claim CAS left: the winner's claim, or
    None when it no longer reads as continuing."""
    current = store.get_continuation(
        record.epic_ref,
        record.item_id,
        record.original_run_id,
        record.branch,
        record.worktree_identity,
    )
    if current is None or current.state != "continuing":
        return None
    return current


def _claim_refusal(
    store: RunStore, record: ContinuationRecord, result: str
) -> EpicResumeDenied:
    """The typed refusal for a claim that did not succeed."""
    messages = {
        "already_claimed": "another caller claimed it first",
        "fleet_full": "the fleet worker cap is full",
        "lost_claim": "the continuation claim was lost to a concurrent writer",
        "not_found": "the continuation record disappeared",
        "terminal": "the continuation is already terminal",
    }
    detail = messages.get(result, "the continuation could not be claimed")
    message = f"cannot resume {record.item_id}: {detail}"
    if result == "already_claimed":
        return ContinuationAlreadyClaimed(message, _claim_winner(store, record))
    if result == "fleet_full":
        return ContinuationFleetFull(message)
    return EpicResumeDenied(message)


def claim_continuation(
    store: RunStore,
    *,
    record: ContinuationRecord,
    config: DispatchConfig,
    cap: int | None = None,
    actor_id: str = "epic_resume",
) -> ContinuationRecord:
    """Claim a selected continuation and verify the durable owner afterward.

    The continuation run id is always generated here: an id supplied from
    outside could name an existing run, owner, or claim, and the ancestry
    reader treats a run id as one claim. *cap* is the fleet worker cap the
    claim is admitted under (the configured one by default); the epic driver's
    automatic continuation (#2964) passes its own tick's cap and actor.
    """
    run_id = str(uuid.uuid4())
    refusal = _claim_identity_refusal(store, record, run_id)
    if refusal is not None:
        raise EpicResumeDenied(f"cannot resume {record.item_id}: {refusal}")
    try:
        store.require_continuation_history(record)
    except (RuntimeError, ValueError, OverflowError) as exc:
        raise EpicResumeDenied(f"cannot resume {record.item_id}: {exc}") from exc
    evidence = continuation_evidence(
        phase="claim",
        epic_ref=record.epic_ref,
        item_id=record.item_id,
        original_run_id=record.original_run_id,
        continuation_run_id=run_id,
        branch=record.branch,
        worktree_identity=record.worktree_identity,
        generation=record.generation + 1,
        state="continuing",
        outcome="continuing",
    )
    result = store.claim_continuation_under_cap(
        record.epic_ref,
        record.item_id,
        record.original_run_id,
        record.branch,
        record.worktree_identity,
        run_id,
        config.fleet_concurrency() if cap is None else cap,
        claim_evidence=evidence,
        actor_id=actor_id,
        repo=config.project.repo or None,
    )
    if result != "claimed":
        raise _claim_refusal(store, record, result)

    claimed = store.get_continuation(
        record.epic_ref,
        record.item_id,
        record.original_run_id,
        record.branch,
        record.worktree_identity,
    )
    if (
        claimed is None
        or claimed.state != "continuing"
        or claimed.continuation_run_id != run_id
    ):
        # The claim increments generation exactly once. If the post-claim read
        # cannot observe our owner, use that generation to release only this
        # claim; a concurrent/newer owner cannot satisfy the predicate.
        store.mark_continuation_awaiting(
            record.epic_ref,
            record.item_id,
            record.original_run_id,
            record.branch,
            record.worktree_identity,
            record.generation + 1,
            run_id,
            reason="claimed_owner_unreadable",
        )
        # This should be impossible because the claim operation and its read
        # use the same database identity. Refuse to execute if the invariant
        # cannot be observed rather than running without durable ownership.
        raise EpicResumeDenied(
            f"cannot resume {record.item_id}: claimed continuation ownership "
            "is unreadable"
        )
    return claimed


def epic_resume(
    store: RunStore,
    *,
    epic_ref: str,
    item_id: str,
    config: DispatchConfig,
    expect: ContinuationRecord | None = None,
) -> ContinuationRecord:
    """Atomically claim one preserved epic worktree for a continuation.

    Worktree and owner validation belongs to the CLI, where the configured
    filesystem and process probes are available. This service boundary owns
    the durable part of the operator verb: it selects exactly one awaiting
    record and folds its state transition together with the fleet reservation
    through RunStore.claim_continuation_under_cap. A caller that loses the
    generation race or the fleet cap receives an error without a write.
    """
    _continuation_operator(config)
    try:
        record = select_awaiting_continuation(store, epic_ref, item_id)
    except ContinuationSelectionDenied as exc:
        raise EpicResumeDenied(str(exc).format(item_id=item_id)) from None
    if expect is not None and not _same_continuation_identity(record, expect):
        raise EpicResumeDenied(
            f"cannot resume {item_id}: the validated continuation identity changed"
        )
    return claim_continuation(store, record=record, config=config)


def _require_known_epic_node(store: RunStore, epic_ref: str, item_id: str) -> None:
    """Resolve operator targets from local continuation and driver evidence."""
    records = store.list_continuations(epic_ref)
    if any(record.item_id == item_id for record in records):
        return
    if not records and epic_ref not in store.list_epic_refs():
        raise LookupError(f"unknown epic {epic_ref!r}")
    tick = store.latest_epic_tick(epic_ref)
    nodes = tick.snapshot.get("nodes", []) if tick is not None else []
    if any(node.get("id") == item_id for node in nodes):
        return
    raise LookupError(f"unknown node {item_id!r} in epic {epic_ref!r}")


def epic_abandon(
    store: RunStore,
    *,
    epic_ref: str,
    item_id: str,
    reason: str,
    config: DispatchConfig,
    checkout_guard: Callable[[Path], AbstractContextManager[None]] | None = None,
) -> None:
    """Abandon one awaiting continuation with complete owner history.

    The existing terminalization transaction appends the operator event,
    releases the exact reservation, and writes the driver's retry marker.
    No ancestry parsing or checkout-content validation is required.
    *checkout_guard* (``kew.worktree.checkout_claim_guard`` from the
    CLI) is required in every publication mode. Owner death is checked under
    this guard before terminalization; a busy checkout or unproven owner refuses.
    """
    actor = _continuation_operator(config)
    if not reason.strip():
        raise EpicAbandonDenied("cannot abandon: reason must be non-empty")
    _require_known_epic_node(store, epic_ref, item_id)
    try:
        record = select_awaiting_continuation(store, epic_ref, item_id)
    except ContinuationSelectionDenied as exc:
        raise EpicAbandonDenied(
            "not awaiting continuation: " + str(exc).format(item_id=item_id)
        ) from None
    # The checkout claim is injected by the caller (the CLI passes
    # kew.worktree.checkout_claim_guard): the service layer must not import
    # kew.worktree, which reaches kew.graph through dispatch_core and breaks
    # the import contracts (CI on #1952).
    worktree = Path(record.worktree_identity)
    if checkout_guard is None:
        raise EpicAbandonDenied("cannot abandon: checkout guard is required")
    guard = checkout_guard(worktree)
    try:
        guard.__enter__()
    except Exception as exc:  # noqa: BLE001 — the guard's own busy error
        raise EpicAbandonDenied(f"cannot abandon {item_id}: {exc}") from exc
    try:
        refusal = continuation_owner_death_refusal(store, record, config)
        if refusal is not None:
            raise EpicAbandonDenied(f"cannot abandon {item_id}: {refusal}")
        finalized = store.finalize_continuation(
            record.epic_ref,
            record.item_id,
            record.original_run_id,
            record.branch,
            record.worktree_identity,
            record.generation,
            record.continuation_run_id,
            abandon_reason=reason.strip(),
            actor_id=actor.id,
            repo=config.project.repo or None,
        )
        if not finalized:
            raise EpicAbandonDenied(
                f"cannot abandon {item_id}: the continuation state changed"
            )
    finally:
        guard.__exit__(None, None, None)


class EpicArchiveDenied(ValueError):
    """The archive decision would change nothing, so it is not recorded."""


def set_epic_archived(
    store: RunStore,
    *,
    epic_ref: str,
    archived: bool,
    reason: str,
    config: DispatchConfig,
    source: str = "cli",
) -> ArchiveState:
    """Retire an epic from the active views, or return it to them (#2206).

    The attested event IS the state — see :mod:`kew.epic_archive` — so unlike
    every other verb in this module there is no effector after the emit, and
    the write is ``strict``: a dropped event would mean a retirement the
    operator was told happened and that no reader can see.

    Refuses an epic the local ledger has never driven (``LookupError``): a
    typo'd ref would otherwise record a retirement that retires nothing.
    Refuses a no-op (:class:`EpicArchiveDenied`), because a second archive is
    not a second decision — attesting it would put a change on the chain that
    changed nothing.
    """
    if epic_ref not in set(store.list_epic_refs()):
        raise LookupError(f"unknown epic {epic_ref!r}: no tick for it in this ledger")
    current = archive_states(store).get(epic_ref)
    if (current is not None and current.archived) == archived:
        state = "already archived" if archived else "not archived"
        since = f" (since {current.at})" if current is not None else ""
        raise EpicArchiveDenied(f"{epic_ref} is {state}{since}")
    event = EPIC_ARCHIVED_EVENT if archived else EPIC_UNARCHIVED_EVENT
    emit(
        store,
        event,
        actor=resolve_actor(),
        repo=config.project.repo or None,
        payload={"epic_ref": epic_ref, "reason": reason.strip(), "source": source},
        strict=True,
    )
    # Read the decision back out of the chain rather than returning what we
    # meant to write. `strict` proves a row landed; this proves the row landed
    # as the state every OTHER reader derives — so the caller cannot report a
    # retirement that the Epics list and the needs-you count disagree with.
    recorded = archive_states(store).get(epic_ref)
    if recorded is None or recorded.archived != archived:
        raise RuntimeError(
            f"{event} was written for {epic_ref} but does not read back as the "
            "archive state; the ledger may be mid-write or corrupt"
        )
    return recorded


def surfaced_candidates(config: DispatchConfig) -> set[int]:
    """Open issue numbers of the pinned repo — the queue candidate set.

    Repo is pinned server-side to ``config.project.repo``; there is no
    client-supplied repo to honor. Fail-closed: a gh error (or unset repo)
    yields the empty set, so ``queue_issue`` rejects rather than queuing an
    issue against an unverifiable repo.
    """
    repo = config.project.repo
    if not repo:
        return set()
    try:
        result = run(
            [
                "gh",
                "issue",
                "list",
                "--repo",
                repo,
                "--state",
                "open",
                "--json",
                "number",
                "--limit",
                "1000",
            ],
            check=False,
        )
        if getattr(result, "returncode", 1) != 0:
            return set()
        rows = json.loads(result.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):  # fail-closed
        return set()
    if not isinstance(rows, list):
        return set()
    numbers: set[int] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw = row.get("number")
        if raw is None:
            continue
        try:
            numbers.add(int(raw))
        except (TypeError, ValueError):
            continue
    return numbers


def queue_issue(
    store: RunStore,
    *,
    issue: int,
    config: DispatchConfig,
    source: str = "cli",
) -> None:
    """Queue an issue for dispatch — validate, audit event first, then mark ready.

    The issue is validated against :func:`surfaced_candidates` (open issues of
    the pinned repo) *before* any event is emitted: an unknown issue is rejected
    with ``ValueError`` and produces no audit event and no label change. A valid
    issue emits ``issue_queued`` and then gains the ready label.
    """
    if issue not in surfaced_candidates(config):
        raise ValueError(
            f"issue #{issue} is not an open issue of {config.project.repo!r}"
        )
    emit(
        store,
        "issue_queued",
        actor=resolve_actor(),
        issue_num=issue,
        repo=config.project.repo or None,
        payload={"issue": issue, "source": source},
    )
    mark_ready(issue, config)


def ack_orphan_pr(
    store: RunStore,
    *,
    pr_number: int,
    config: DispatchConfig,
    source: str = "cli",
) -> bool:
    """Acknowledge an orphan PR — audit event first, then set ``acked_at``.

    Never closes the PR (acknowledging is a ledger-only act; the human decides).
    Idempotent: re-acking preserves the first ``acked_at`` at the store, and the
    intent event is emitted on each call. Returns whether a tracked orphan row
    exists for ``pr_number``.
    """
    emit(
        store,
        "orphan_pr_acked",
        actor=resolve_actor(),
        repo=config.project.repo or None,
        payload={"pr_number": pr_number, "source": source},
    )
    return store.ack_orphan_pr(pr_number)


# --------------------------------------------------------------------------- #
# Serve-facing wrappers (Group 4, #684): approver gate + explicit-confirm for
# the spend-authorizing verbs, plus the feedback contract — each returns the
# target's new authoritative state, and queue/resume the driver-staleness flag.
# The serve route stays a thin shim (kew.serve may not import audit/approval).
# --------------------------------------------------------------------------- #


def _approver_gate(store: RunStore, config: DispatchConfig) -> None:
    """Gate a spend-authorizing verb on ``[approval].approvers`` + bind policy.

    Mirrors ``kew.service.config._gate_and_bind``: a non-listed actor is
    rejected with :class:`VerbDenied`; a permitted actor binds the governance
    policy for this process so the verb's own audit event carries the
    ``policy_hash`` (attest under a bound policy, then act).
    """
    actor = resolve_actor()
    if not _authorized(config, actor.id):
        raise VerbDenied(f"actor {actor.id!r} is not in [approval].approvers")
    bind_policy_context(config)
    record_current_policy(store)


def _no_live_driver(store: RunStore, *, now: datetime | None = None) -> bool:
    """Whether NO epic driver is currently live ("no live driver").

    A driver is live when its heartbeat is present and not past the driver-gone
    threshold. With no known epic drivers at all, there is no live driver, so
    the flag is True. ``now`` is injectable for deterministic tests.
    """
    for epic_ref in store.list_epic_refs():
        liveness = driver_liveness(store, epic_ref, now=now)
        if liveness.present and not liveness.driver_gone:
            return False
    return True


def resume_via_serve(
    store: RunStore,
    *,
    config: DispatchConfig,
    confirm: bool,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Resume the fleet from the serve surface — gated + explicit-confirm.

    Returns the new authoritative state (``resumed``) and the driver-staleness
    flag. Raises :class:`ConfirmRequired` without ``confirm: true`` and
    :class:`VerbDenied` for a non-approver — both before any side-effect.
    """
    if confirm is not True:
        raise ConfirmRequired("resume requires explicit confirm: true")
    _approver_gate(store, config)
    # Through the module's own service wrapper (keyword-only config) — the
    # single service-boundary entry point for fleet resume (#685/#684 merge).
    resumed = resume_fleet(store, config=config)
    return {"resumed": resumed, "driver_stale": _no_live_driver(store, now=now)}


def queue_via_serve(
    store: RunStore,
    *,
    issue: int,
    config: DispatchConfig,
    confirm: bool,
    source: str = "web",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Queue an issue from the serve surface — gated + explicit-confirm.

    Returns the ready-labeled issue's new state and the driver-staleness flag.
    Raises :class:`ConfirmRequired` / :class:`VerbDenied` before any effect, and
    propagates ``ValueError`` for an issue outside the surfaced-candidate set.
    """
    if confirm is not True:
        raise ConfirmRequired("queue requires explicit confirm: true")
    _approver_gate(store, config)
    queue_issue(store, issue=issue, config=config, source=source)
    return {
        "issue": issue,
        "item_id": str(issue),
        "ready": True,
        "driver_stale": _no_live_driver(store, now=now),
    }


def epic_retry_gated(
    store: RunStore,
    *,
    epic_ref: str,
    item_id: str,
    config: DispatchConfig,
    source: str = "cli",
) -> str:
    """Retry an epic node behind the approver gate — every surface's entry (#2552).

    Re-admitting a node authorizes new agent spend, so the console, `kew epic
    retry` and the TUI all reach :func:`epic_retry` only through here: a
    non-approver gets :class:`VerbDenied` before any marker, event, reservation
    release or label edit, and an approver's ``epic_node_retry`` event carries
    the bound policy's hash. Each surface keeps its own confirm step. Returns
    the canonical ``item_id`` the retry ran on.
    """
    _approver_gate(store, config)
    item_id = validate_item_id(item_id, provider_from_config(config))
    epic_retry(store, epic_ref=epic_ref, item_id=item_id, config=config, source=source)
    return item_id


def epic_send_back_gated(
    store: RunStore,
    *,
    epic_ref: str,
    item_id: str,
    reason: str,
    config: DispatchConfig,
    read_pr: Callable[[], PullRequestHead],
    source: str = "cli",
) -> SendBack:
    """Send a READY epic node back for remediation behind the approver gate (#3070).

    The send-back authorizes a remediation run, so a non-approver gets
    :class:`VerbDenied` before anything is recorded, exactly as for a retry,
    and before *read_pr* reads the forge.
    """
    _approver_gate(store, config)
    item_id = validate_item_id(item_id, provider_from_config(config))
    return epic_send_back(
        store,
        epic_ref=epic_ref,
        item_id=item_id,
        reason=reason,
        config=config,
        read_pr=read_pr,
        source=source,
    )


def retry_via_serve(
    store: RunStore,
    *,
    epic_ref: str,
    item_id: str,
    config: DispatchConfig,
    confirm: bool,
    source: str = "web",
) -> dict[str, Any]:
    """Retry an epic node from the serve surface — gated + explicit-confirm.

    Re-admitting a node authorizes new agent spend just as queueing does, so it
    carries the same gate: :class:`ConfirmRequired` without ``confirm: true``
    and :class:`VerbDenied` for a non-approver, both before any effect.

    Accepts both id currencies: the ``item_id`` is validated against the
    configured tracker's canonical form (:class:`~kew.ids.InvalidItemIdError`
    surfaces as a 4xx at the route). Returns whether the node was re-admitted
    (its stale item-keyed reservation released), read back after the verb runs,
    plus both the legacy numeric ``issue`` and the durable ``item_id``.
    """
    if confirm is not True:
        raise ConfirmRequired("retry requires explicit confirm: true")
    item_id = epic_retry_gated(
        store, epic_ref=epic_ref, item_id=item_id, config=config, source=source
    )
    reserved = {
        str(getattr(r, "item_id", "") or "")
        for r in store.list_epic_reservations(epic_ref)
    }
    is_github = GITHUB_ID_RE.fullmatch(item_id) is not None
    return {
        "epic_ref": epic_ref,
        "issue": int(item_id) if is_github else 0,
        "item_id": item_id,
        "re_admitted": item_id not in reserved,
    }


def ack_via_serve(
    store: RunStore,
    *,
    pr_number: int,
    config: DispatchConfig,
    source: str = "web",
) -> dict[str, Any]:
    """Acknowledge an orphan PR from the serve surface (behind ``require_write``).

    Returns whether a tracked orphan row exists (``acked``) and its persisted
    ``acked_at`` read back from the store.
    """
    tracked = ack_orphan_pr(store, pr_number=pr_number, config=config, source=source)
    row = next(
        (
            o
            for o in store.list_orphan_prs(include_acked=True)
            if o.pr_number == pr_number
        ),
        None,
    )
    acked_at = row.acked_at.isoformat() if row and row.acked_at is not None else None
    return {"pr_number": pr_number, "acked": tracked, "acked_at": acked_at}


def resume_fleet(
    store: RunStore,
    *,
    config: DispatchConfig,
) -> bool:
    """Clear the fleet-stop latch through the service boundary (#685).

    Thin wrapper over :func:`kew.fleet.resume_fleet` so serve/TUI surfaces
    reach fleet resume without importing ``kew.fleet`` (or ``kew.run_store``)
    directly. Returns ``False`` when the latch was not set. The underlying
    call clears the latch and emits ``fleet_resume`` with the resolved actor.
    """
    from kew.fleet import resume_fleet as _resume_fleet

    return _resume_fleet(store, config)


__all__ = [
    "epic_retry",
    "epic_retry_gated",
    "epic_resume",
    "epic_abandon",
    "set_epic_archived",
    "EpicArchiveDenied",
    "queue_issue",
    "ack_orphan_pr",
    "resume_fleet",
    "surfaced_candidates",
    "resume_via_serve",
    "queue_via_serve",
    "retry_via_serve",
    "ack_via_serve",
    "VerbDenied",
    "ConfirmRequired",
    "EpicResumeDenied",
    "ContinuationSelectionDenied",
    "select_awaiting_continuation",
    "perform_work_action",
    "WorkActionUnavailable",
    "WorkActionStale",
]


# ---------------------------------------------------------------------------
# I14: the operator-facing end of the publication-capacity transaction.
#
# Both verbs follow this module's contract — attest, then act — and both keep
# the network call strictly outside the SQLite write transaction. The
# reservation verb reconciles the delivery repository first, persists what it
# saw, and only then asks the store to reserve against the generation it just
# wrote. A reservation therefore always rides on this caller's own look at the
# forge, never on one somebody else took an hour ago.
# ---------------------------------------------------------------------------


def _publication_review_or_raise(store: RunStore, job_id: str) -> Any:
    review = store.publication_review(job_id)  # type: ignore[attr-defined]
    if review is None:
        raise ValueError(f"publication job {job_id!r} was not found")
    return review


def confirm_publication_risk(
    store: RunStore,
    *,
    job_id: str,
    config: DispatchConfig,
    now: datetime,
    source: str = "cli",
) -> Any:
    """Release one risk hold, for the exact SHA the job is still publishing.

    The confirmation is built from the persisted row rather than from anything
    the caller supplies, so it cannot be given for a tree the job no longer
    names: remediation opens a successor job with a different tree, and the
    store's own binding check refuses yesterday's approval against it.
    """
    _approver_gate(store, config)
    actor = resolve_actor()
    review = _publication_review_or_raise(store, job_id)
    job = store.get_publication_job(job_id)
    if job is None:  # pragma: no cover - the review read already found the row
        raise ValueError(f"publication job {job_id!r} was not found")
    policy = _effective_policy(store, config, review, now=now)
    authorize_approver(policy, actor.id)
    confirmation = RiskConfirmation(
        job_id=job_id,
        delivery_repo=review.delivery_repo,
        base_sha=job.base_sha,
        tree_sha=job.tree_sha,
        risk_digest=review.risk_digest,
        policy_hash=review.policy_hash,
        policy_generation=review.policy_generation,
        actor=actor.id,
        confirmed_at=now.isoformat(),
    )
    emit(
        store,
        "publication_risk_confirmed",
        actor=actor,
        run_id=job.run_id,
        item_id=job.source_id,
        repo=review.delivery_repo or None,
        payload={
            "job_id": job_id,
            "base_sha": job.base_sha,
            "tree_sha": job.tree_sha,
            "risk_digest": review.risk_digest,
            "policy_generation": review.policy_generation,
            "source": source,
        },
    )
    return store.confirm_publication_review(  # type: ignore[attr-defined]
        job_id=job_id, confirmation=confirmation, now=now
    )


def _effective_policy(
    store: RunStore, config: DispatchConfig, review: Any, *, now: datetime
) -> ReviewPolicy:
    """The policy in force for the repository this job delivers to."""
    system = policy_from_config(config)
    if review.delivery_repo and review.delivery_repo != system.delivery_repo:
        raise ValueError(
            f"job delivers to {review.delivery_repo!r}, which this project does"
            " not govern"
        )
    resolved: ReviewPolicy = store.effective_human_review_policy(  # type: ignore[attr-defined]
        system=system, now=now
    )
    return resolved


def reserve_publication_capacity(
    store: RunStore,
    *,
    job_id: str,
    config: DispatchConfig,
    now: datetime,
    source: str = "cli",
) -> Any:
    """Reconcile the delivery repository, then reserve this job's review slot.

    The reconciliation is the network call and happens first, outside every
    transaction. The reservation that follows makes none, and is refused
    outright when the observation it would count from is unavailable or stale —
    an unread forge is never counted as an empty one.
    """
    actor = resolve_actor()
    review = _publication_review_or_raise(store, job_id)
    system = policy_from_config(config)
    if review.delivery_repo != system.delivery_repo:
        raise ValueError(
            f"job delivers to {review.delivery_repo!r}, which this project does"
            " not govern"
        )
    observation = import_review_obligations(store, config, now=now)
    emit(
        store,
        "publication_capacity_reserved",
        actor=actor,
        item_id=review.delivery_repo or None,
        repo=review.delivery_repo or None,
        payload={
            "job_id": job_id,
            "delivery_repo": review.delivery_repo,
            "observation_generation": observation.generation,
            "observed_obligations": len(observation.obligations),
            "source": source,
        },
    )
    return store.reserve_publication_capacity(  # type: ignore[attr-defined]
        job_id=job_id,
        system_policy=system,
        expected_generation=observation.generation,
        actor=actor.id,
        now=now,
    )


def release_publication_capacity(
    store: RunStore,
    *,
    job_id: str,
    disposition: str,
    config: DispatchConfig,
    now: datetime,
    source: str = "cli",
) -> Any:
    """Hand a review slot back once a terminal human decision has been observed.

    ``held`` is a legitimate answer and changes nothing — it is what an open
    pull request and an unreadable forge both resolve to.
    """
    actor = resolve_actor()
    review = _publication_review_or_raise(store, job_id)
    if disposition == SLOT_RELEASED and review.occupies_slot:
        emit(
            store,
            "publication_capacity_released",
            actor=actor,
            item_id=review.delivery_repo or None,
            repo=review.delivery_repo or None,
            payload={
                "job_id": job_id,
                "delivery_repo": review.delivery_repo,
                "source": source,
            },
        )
    return store.release_publication_capacity(  # type: ignore[attr-defined]
        job_id=job_id, disposition=disposition, now=now
    )


# --------------------------------------------------------------------------- #
# I11: the three no-change verbs — confirm, reject, retry.
#
# Every one of them is a decision about an ABSENCE. There is no diff to read
# afterwards, so the only durable account of why an item closed is what these
# verbs write: who decided, under which authority rule, against which evidence,
# and at what time. That is why all three funnel through here rather than
# living in a CLI branch and a route handler that each learned the rules
# separately, and why the audit event is emitted before the effect as
# everywhere else in this module.
#
# The authority split is deliberately stricter than the spend gate's:
#
# * a NONEMPTY ``[human_review].approvers`` allowlist requires membership, and
#   a repository admin who is not named is refused;
# * an EMPTY allowlist requires FRESH delivery-repository write/maintain/admin
#   evidence, read at the moment of the decision.
#
# Authorship of the item is not a third rule and never waives either.
# --------------------------------------------------------------------------- #


class OutcomeStale(ValueError):
    """The source or base moved after the judgment, so it is not decidable."""


class NoChangeNotFound(ValueError):
    """No judgment by that id — a missing target, not a bad request."""


def _no_change_or_raise(store: RunStore, judgment_id: str) -> Any:
    judgment = store.get_no_change_judgment(judgment_id)  # type: ignore[attr-defined]
    if judgment is None:
        raise NoChangeNotFound(f"no-change judgment {judgment_id!r} was not found")
    return judgment


#: What each verb may be asked of, by the state of the judgment it names. The
#: audit event is emitted BEFORE the effect everywhere in this module, so a
#: verb that only discovers its refusal in the effector has already chained an
#: event attesting a decision that never happened — a repeated ``confirm``
#: wrote a second ``no_change_confirmed`` and then failed, and a ``retry-sync``
#: on an undecided judgment attested a retry of a synchronization that did not
#: exist yet and reported success (#2463). The admitted states are the ones the
#: effector itself accepts: ``confirm`` moves a judgment that is waiting on a
#: person and nothing else, ``reject`` is reachable from every live state, and
#: a synchronization can only be re-run once a decision has been recorded —
#: sync jobs are enqueued by the confirmation, so before it there is nothing
#: for a retry to carry.
_CONFIRMABLE = frozenset({PENDING_CONFIRMATION})
_REJECTABLE = frozenset(LIFECYCLE) - TERMINAL_STATES
_RESYNCABLE = frozenset({AUTHORIZED, SYNC_PENDING, EFFECT_PENDING, COMPLETED})


def _admits(judgment: Any, *, states: frozenset[str], nothing: str) -> None:
    """Refuse a verb this judgment's state does not admit, before any write.

    Checked after the authority read and before everything else: an actor who
    may not decide this outcome is refused as one, whatever state the judgment
    is in, so a caller cannot read a judgment's state off the refusal it gets.
    """
    if judgment.state not in states:
        raise NoChangeStateError(
            f"{judgment.judgment_id} is {judgment.state}, so there is {nothing}"
        )


def _no_change_approvers(config: DispatchConfig) -> tuple[str, ...]:
    """The allowlist in force, with ``[approval].approvers`` as the fallback.

    The same resolution ``policy_from_config`` performs, read directly so a
    project whose ``[project].repo`` is unset still gets an authority answer
    (a refusal) rather than a policy-validation error.
    """
    return tuple(config.human_review.approvers) or tuple(config.approval.approvers)


@dataclass(frozen=True)
class _NoChangeAuthority:
    """One authority decision, carrying BOTH identities behind it.

    ``actor`` is the name the rule in force actually decided about — the forge
    login under the repository-permission rule, the operator identity under a
    named allowlist — so what lands on the judgment and in the tracker comment
    is a name a reader can look up. ``facts`` records the other one too: a
    refusal or a grant that named only half the identity left nobody able to
    tell "the wrong person asked" from "we asked about the wrong name" (#2463).
    """

    actor: str
    facts: dict[str, Any]


def _forge_authority(
    config: DispatchConfig, *, operator: str, now: datetime
) -> tuple[str, RepositoryAuthority]:
    """The forge login to judge, and what the delivery repository says about it.

    The OS username is not a forge identity. Permission is granted to a LOGIN,
    so the permission rule asks about the login ``gh`` is authenticated as —
    the same account the host publishes with. A login that cannot be read is
    refused exactly as an unread permission is: not knowing which account is
    asking is not evidence that it may close an item.
    """
    identity = observe_forge_identity()
    if not identity.available:
        return operator, RepositoryAuthority(
            delivery_repo=config.project.repo or "",
            actor=operator,
            permission="",
            observed_at=now.isoformat(),
            available=False,
            detail="the authenticated forge login could not be read: "
            + identity.detail,
        )
    return identity.login, observe_repository_authority(
        config, actor=identity.login, now=now
    )


def _authorize_no_change(
    store: RunStore, config: DispatchConfig, *, now: datetime
) -> _NoChangeAuthority:
    """Resolve this actor's authority over this outcome, or refuse it.

    The repository read happens ONLY when the allowlist is empty: with a named
    allowlist there is nothing a permission could add, and asking anyway would
    leak the impression that it might. That is also why the forge login is
    resolved only on the permission path — an allowlist names operators, and
    membership is not a question for the forge.
    """
    operator = resolve_actor().id
    approvers = _no_change_approvers(config)
    actor, authority = (
        (operator, None)
        if approvers
        else _forge_authority(config, operator=operator, now=now)
    )
    # ``item_author`` is deliberately not passed: authorship is not an input to
    # authority here, so an actor who filed the item is authorized by the rule
    # in force or refused by it, exactly like anyone else. The domain function
    # accepts an author only so that stance can be asserted directly.
    grant = resolve_no_change_authority(
        actor=actor,
        approvers=approvers,
        delivery_repo=config.project.repo or "",
        authority=authority,
        now=now,
    )
    if not grant.authorized:
        raise VerbDenied(grant.reason)
    bind_policy_context(config)
    record_current_policy(store)
    return _NoChangeAuthority(
        actor=grant.actor,
        facts={
            **grant.facts,
            "operator_actor": operator,
            "forge_actor": actor if authority is not None else "",
        },
    )


def _no_change_state(store: RunStore, judgment_id: str) -> dict[str, Any]:
    judgment = _no_change_or_raise(store, judgment_id)
    return {"judgment_id": judgment_id, "state": judgment.state}


def _sync_payload(results: Any) -> list[dict[str, Any]]:
    return [
        {
            "sync_job_id": r.sync_job_id,
            "target_kind": r.target_kind,
            "target_ref": r.target_ref,
            "state": r.state,
            "observation": r.observation_kind,
            "detail": r.detail,
        }
        for r in results
    ]


def confirm_no_change(
    store: RunStore,
    *,
    judgment_id: str,
    config: DispatchConfig,
    now: datetime | None = None,
    source: str = "cli",
) -> dict[str, Any]:
    """A person authorizes one no-change outcome, and kew synchronizes it.

    Order: authorize, admit the state, revalidate the base the judgment rests
    on, attest the intent, then act. The revalidation is what stops a
    confirmation given against a tree the repository has since moved past — a
    no-change is a claim about a commit, and the claim expires when the commit
    does. The admission precedes the attestation so a second confirmation of
    an outcome already authorized refuses without chaining an event for a
    decision nobody made.
    """
    stamp = now or datetime.now(UTC)
    judgment = _no_change_or_raise(store, judgment_id)
    grant = _authorize_no_change(store, config, now=stamp)
    _admits(judgment, states=_CONFIRMABLE, nothing="nothing to confirm")
    revalidation = revalidate_no_change_source(config, judgment=judgment)
    if not revalidation.valid:
        raise OutcomeStale(revalidation.detail)
    emit(
        store,
        "no_change_confirmed",
        actor=resolve_actor(),
        run_id=judgment.run_id,
        item_id=judgment.item_id or judgment.source_id,
        repo=config.project.repo or None,
        payload={
            "judgment_id": judgment_id,
            "generation": judgment.generation,
            "base_sha": judgment.base_sha,
            "observed_base_sha": revalidation.observed_base_sha,
            "source": source,
            **grant.facts,
        },
    )
    store.confirm_no_change(  # type: ignore[attr-defined]
        judgment_id=judgment_id, actor_id=grant.actor, now=stamp
    )
    store.enqueue_outcome_sync(  # type: ignore[attr-defined]
        judgment_id=judgment_id,
        targets=((judgment.source_kind, judgment.source_id),),
        now=stamp,
    )
    results = synchronize_no_change(store, config, judgment_id=judgment_id, now=stamp)
    return {**_no_change_state(store, judgment_id), "sync": _sync_payload(results)}


def reject_no_change(
    store: RunStore,
    *,
    judgment_id: str,
    reason: str,
    config: DispatchConfig,
    now: datetime | None = None,
    source: str = "cli",
) -> dict[str, Any]:
    """Refuse one no-change outcome. Closes nothing, unblocks nothing.

    The item is left open and unsatisfied on purpose: a refused no-change is
    not a failed run and must not arm a blind redispatch — somebody decided
    the absence was not the answer, and what to do instead is their next
    decision, not this verb's.

    The base is revalidated and RECORDED here, never enforced: a base that has
    moved is one of the strongest reasons to refuse a no-change, so refusing
    to accept the refusal would strand exactly the item that needs it.
    """
    stamp = now or datetime.now(UTC)
    if not reason.strip():
        raise ValueError("rejecting a no-change outcome requires a reason")
    judgment = _no_change_or_raise(store, judgment_id)
    grant = _authorize_no_change(store, config, now=stamp)
    _admits(judgment, states=_REJECTABLE, nothing="nothing left to refuse")
    revalidation = revalidate_no_change_source(config, judgment=judgment)
    emit(
        store,
        "no_change_rejected",
        actor=resolve_actor(),
        run_id=judgment.run_id,
        item_id=judgment.item_id or judgment.source_id,
        repo=config.project.repo or None,
        payload={
            "judgment_id": judgment_id,
            "generation": judgment.generation,
            "reason": reason,
            "base_still_valid": revalidation.valid,
            "observed_base_sha": revalidation.observed_base_sha,
            "source": source,
            **grant.facts,
        },
    )
    rejected = store.reject_no_change(  # type: ignore[attr-defined]
        judgment_id=judgment_id, actor_id=grant.actor, reason=reason, now=stamp
    )
    noted = post_no_change_rejection(
        config, judgment=rejected, reason=reason, actor=grant.actor
    )
    return {**_no_change_state(store, judgment_id), "disposition_noted": noted}


class RejudgeRefused(OutcomeStale):
    """Main moved in a way a rejudge may not record without a person saying so."""


def _rejudge_refusal(comparison: BaseComparison, paths: tuple[str, ...]) -> str:
    """Why this base move needs ``--accept-changes``, or ``""`` when it does not.

    A judgment with judged paths is about those files; one without is about the
    whole tree, so any file the commits since changed counts as touched.
    """
    if comparison.touched is None:
        return f"the commits since could not be compared: {comparison.unanswered}"
    if not comparison.touched:
        return ""
    scope = "judged paths" if paths else "files (it records no judged paths)"
    return f"the commits since touched {scope}: {name_paths(comparison.touched)}"


class RejudgeUnaudited(RuntimeError):
    """The base moved, and the audit row that records who moved it did not land."""


def _audit_rejudge(
    store: RunStore,
    judgment: Any,
    config: DispatchConfig,
    *,
    payload: dict[str, Any],
) -> None:
    """Chain the ``no_change_rejudged`` row, or say loudly that it is missing.

    Strict: for an ``--accept-changes`` override this row is the only record
    of who overrode the re-check and why, so a write that did not land is a
    failure of the command, never a warning beside a success.
    """
    try:
        emit(
            store,
            "no_change_rejudged",
            actor=resolve_actor(),
            run_id=judgment.run_id,
            item_id=judgment.item_id or judgment.source_id,
            repo=config.project.repo or None,
            payload=payload,
            strict=True,
        )
    except RuntimeError as exc:
        raise RejudgeUnaudited(
            f"{judgment.judgment_id} now stands on base"
            f" {payload['observed_base_sha']} (was {payload['base_sha']}), but"
            f" its no_change_rejudged audit row was not written: {exc}"
        ) from exc


def _require_acceptance_reason(accept_changes: bool, reason: str) -> None:
    """``--accept-changes`` and ``--reason`` come together, or not at all."""
    if accept_changes and not reason.strip():
        raise ValueError("--accept-changes requires a --reason saying why")
    if reason.strip() and not accept_changes:
        raise ValueError("--reason explains --accept-changes; pass both or neither")


def _rejudge_refused_text(item: str, previous: str, observed: str, why: str) -> str:
    return (
        f"main moved from {previous} to {observed} and {why}. Nothing was"
        " recorded. Look at those changes, then run `kew no-change rejudge"
        f' {item} --accept-changes --reason "…"` to record the new base'
        f' anyway, or `kew no-change reject {item} --reason "…"`'
    )


def rejudge_no_change(
    store: RunStore,
    *,
    judgment_id: str,
    config: DispatchConfig,
    accept_changes: bool = False,
    reason: str = "",
    now: datetime | None = None,
    source: str = "cli",
) -> dict[str, Any]:
    """Re-check one no-change against the current base, and record that base.

    The way forward from a stale confirmation (#2828). No agent is dispatched
    and no run is written. The re-check is real: when main does not descend
    from the judged base, the comparison cannot run, or the commits since
    touched a judged path (or, for a judgment that records none, any file),
    the base is NOT moved — :class:`RejudgeRefused` names what changed — unless
    a person passes *accept_changes* with a *reason*, which the
    ``no_change_rejudged`` event records with the actor. The event is chained
    only once the compare-and-set on the base has landed, so a lost race
    attests nothing. The judgment stays pending; confirming it is the next,
    separate decision.
    """
    stamp = now or datetime.now(UTC)
    _require_acceptance_reason(accept_changes, reason)
    judgment = _no_change_or_raise(store, judgment_id)
    grant = _authorize_no_change(store, config, now=stamp)
    _admits(judgment, states=_CONFIRMABLE, nothing="nothing to rejudge")
    observed, unread = read_delivery_base(config)
    if not observed:
        raise OutcomeStale(unread)
    previous = judgment.base_sha
    if observed == previous:
        return {**_no_change_state(store, judgment_id), "base_sha": previous}
    paths = judged_paths_of(judgment)
    comparison = compare_judged_base(previous, observed, paths)
    refusal = _rejudge_refusal(comparison, paths)
    item = judgment.item_id or judgment.source_id
    if refusal and not accept_changes:
        raise RejudgeRefused(_rejudge_refused_text(item, previous, observed, refusal))
    store.rebase_no_change_judgment(  # type: ignore[attr-defined]
        judgment_id=judgment_id,
        expected_base_sha=previous,
        base_sha=observed,
        now=stamp,
    )
    touched = None if comparison.touched is None else list(comparison.touched)
    _audit_rejudge(
        store,
        judgment,
        config,
        payload={
            "judgment_id": judgment_id,
            "generation": judgment.generation,
            "base_sha": previous,
            "observed_base_sha": observed,
            "judged_paths": list(paths),
            "touched_paths": touched,
            "accepted_changes": bool(refusal),
            "refusal": refusal,
            "reason": reason if refusal else "",
            "source": source,
            **grant.facts,
        },
    )
    return {
        **_no_change_state(store, judgment_id),
        "base_sha": observed,
        "previous_base_sha": previous,
        "touched_paths": touched,
        "accepted": refusal,
    }


def retry_no_change_sync(
    store: RunStore,
    *,
    judgment_id: str,
    config: DispatchConfig,
    now: datetime | None = None,
    source: str = "cli",
) -> dict[str, Any]:
    """Re-run a synchronization that held. Discovers its own prior write first.

    The retry is the recovery path for every hold — a tracker outage, an
    unconfirmed write, a base that had moved and has since been re-judged —
    and it is safe to press repeatedly because the reconciler looks for this
    judgment's own marker before writing anything.

    A judgment nobody has decided yet is refused rather than retried: its
    synchronization does not exist, so a pass over it would attest a retry and
    report success for work it never carried.
    """
    stamp = now or datetime.now(UTC)
    judgment = _no_change_or_raise(store, judgment_id)
    grant = _authorize_no_change(store, config, now=stamp)
    _admits(
        judgment,
        states=_RESYNCABLE,
        nothing="no synchronization to retry — nothing has been decided yet",
    )
    emit(
        store,
        "no_change_sync_retried",
        actor=resolve_actor(),
        run_id=judgment.run_id,
        item_id=judgment.item_id or judgment.source_id,
        repo=config.project.repo or None,
        payload={
            "judgment_id": judgment_id,
            "generation": judgment.generation,
            "state": judgment.state,
            "source": source,
            **grant.facts,
        },
    )
    results = synchronize_no_change(store, config, judgment_id=judgment_id, now=stamp)
    return {**_no_change_state(store, judgment_id), "sync": _sync_payload(results)}


# --------------------------------------------------------------------------- #
# I20 — the guarded work action.
#
# Every mutating verb above answers "do this specific thing to this specific
# row". This one answers the question a surface actually has: "the projection
# says X is the safest thing to do about this work — do it." That difference is
# the whole point, because it removes the step where a CLI, a web client or a
# TUI each decided for itself what X was.
#
# Five properties are non-negotiable, and each one is a bug this repository has
# already shipped at least once:
#
# 1. **Canonical identity.** The caller names the work by its opaque item id.
#    The id of the thing acted on — a run, a judgment, a publication job, an
#    orphan pull request — is read back out of the projection's own facts, so a
#    caller can never point the effect at something else.
# 2. **Expected revision.** The caller says which ``view_revision`` it decided
#    against. A different one now means the world moved under the decision, and
#    the request is refused rather than applied to facts nobody looked at.
# 3. **Audited intent before effect.** ``work_action_requested`` is chained
#    before the reconciler runs (the module contract), and
#    ``work_action_applied`` after it succeeds.
# 4. **Idempotent effect.** The applied event IS the dedupe key: replaying the
#    identical request returns the authoritative projection without running the
#    effect twice. A request that failed mid-effect wrote no applied event, so
#    it is still retryable — the two cases are deliberately not the same.
# 5. **Authoritative post-write truth.** The returned view is recomposed AFTER
#    the effect, so the caller's next decision is made against the world its
#    own write produced.
#
# There is no action resolver here. ``kew.service.work.offered_actions`` is the
# only thing that decides what may be requested, and this function's entire
# authority check on the *action* is "is it in that list".
# --------------------------------------------------------------------------- #

WORK_ACTION_REQUESTED = "work_action_requested"
WORK_ACTION_APPLIED = "work_action_applied"


class WorkActionUnavailable(ValueError):
    """The projection does not offer this action for this work."""


class WorkActionStale(ValueError):
    """The caller decided against a view revision that is no longer current."""


@dataclass(frozen=True)
class _WorkEffect:
    """Everything one delegated reconciler call needs, and nothing more."""

    store: RunStore
    config: DispatchConfig
    target: str
    reason: str
    now: datetime
    source: str


def _effect_finalize_publication(ctx: _WorkEffect) -> str:
    record = finalize_publication_run(
        ctx.store, run_id=ctx.target, config=ctx.config, source=ctx.source
    )
    return f"run {ctx.target} finalized to {record.status}"


def _effect_confirm_no_change(ctx: _WorkEffect) -> str:
    state = confirm_no_change(
        ctx.store,
        judgment_id=ctx.target,
        config=ctx.config,
        now=ctx.now,
        source=ctx.source,
    )
    return f"no-change judgment {ctx.target} is {state['state']}"


def _effect_reject_no_change(ctx: _WorkEffect) -> str:
    state = reject_no_change(
        ctx.store,
        judgment_id=ctx.target,
        reason=ctx.reason,
        config=ctx.config,
        now=ctx.now,
        source=ctx.source,
    )
    return f"no-change judgment {ctx.target} is {state['state']}"


def _effect_retry_no_change_sync(ctx: _WorkEffect) -> str:
    state = retry_no_change_sync(
        ctx.store,
        judgment_id=ctx.target,
        config=ctx.config,
        now=ctx.now,
        source=ctx.source,
    )
    return f"no-change judgment {ctx.target} is {state['state']}"


def _effect_confirm_publication_risk(ctx: _WorkEffect) -> str:
    confirm_publication_risk(
        ctx.store,
        job_id=ctx.target,
        config=ctx.config,
        now=ctx.now,
        source=ctx.source,
    )
    return f"publication job {ctx.target} released from its risk hold"


def _effect_acknowledge_orphan(ctx: _WorkEffect) -> str:
    ack_orphan_pr(
        ctx.store, pr_number=int(ctx.target), config=ctx.config, source=ctx.source
    )
    return f"orphan pull request {ctx.target} acknowledged"


def _effect_dispatch(ctx: _WorkEffect) -> str:
    queue_issue(ctx.store, issue=int(ctx.target), config=ctx.config, source=ctx.source)
    return f"item {ctx.target} queued for dispatch"


def _effect_resume_fleet(ctx: _WorkEffect) -> str:
    resumed = resume_fleet(ctx.store, config=ctx.config)
    return "fleet resumed" if resumed else "the fleet-stop latch was not set"


#: Action name -> the domain reconciler that owns it. Every entry delegates;
#: none of them re-derives whether the action was a good idea.
_WORK_EFFECTS: dict[str, Callable[[_WorkEffect], str]] = {
    "finalize_publication": _effect_finalize_publication,
    "confirm_no_change": _effect_confirm_no_change,
    "reject_no_change": _effect_reject_no_change,
    "retry_no_change_sync": _effect_retry_no_change_sync,
    "confirm_publication_risk": _effect_confirm_publication_risk,
    "acknowledge_orphan": _effect_acknowledge_orphan,
    "dispatch": _effect_dispatch,
    "resume_fleet": _effect_resume_fleet,
}


def _work_action_payload(
    offer: WorkActionDTO, *, expected_revision: str, source: str
) -> dict[str, Any]:
    return {
        "action": offer.action,
        "target_kind": offer.target_kind,
        "target_ref": offer.target_ref,
        "expected_revision": expected_revision,
        "source": source,
    }


def _emit_work_action(
    store: RunStore,
    event: str,
    *,
    view: WorkViewDTO,
    offer: WorkActionDTO,
    config: DispatchConfig,
    expected_revision: str,
    source: str,
    extra: dict[str, Any] | None = None,
) -> None:
    identity = view.identity
    emit(
        store,
        event,
        actor=resolve_actor(),
        run_id=identity.run_id,
        item_id=identity.item_id,
        repo=identity.delivery_repo or config.project.repo or None,
        payload={
            **_work_action_payload(
                offer, expected_revision=expected_revision, source=source
            ),
            **(extra or {}),
        },
    )


def _applied_request(
    store: RunStore, item_id: str, action: str, expected_revision: str
) -> dict[str, Any] | None:
    """The already-recorded application of THIS exact request, if there is one.

    The dedupe key is (item, action, expected revision): the same decision,
    made against the same facts. A different revision is a different decision
    and is never silently absorbed into this one.
    """
    for event in store.list_audit_events(
        event=WORK_ACTION_APPLIED, item_id=item_id, limit=None
    ):
        payload = dict(event.payload or {})
        if (payload.get("action"), payload.get("expected_revision")) == (
            action,
            expected_revision,
        ):
            return payload
    return None


def _offer_or_raise(view: WorkViewDTO, action: str) -> WorkActionDTO:
    offer = next((a for a in view.actions if a.action == action), None)
    if offer is None:
        raise WorkActionUnavailable(
            f"{action!r} is not offered for {view.identity.item_ref}: the "
            f"safest action is {view.safest_action.action!r} "
            f"({view.safest_action.reason})"
        )
    return offer


def _require_current(view: WorkViewDTO, expected_revision: str) -> None:
    if view.view_revision != expected_revision:
        raise WorkActionStale(
            f"{view.identity.item_ref} moved: the decision was made against "
            f"{expected_revision!r} and the current view is "
            f"{view.view_revision!r}"
        )


def _replayed(
    payload: dict[str, Any], *, action: str, expected_revision: str, view: WorkViewDTO
) -> WorkActionResultDTO:
    return WorkActionResultDTO(
        action=action,
        target_kind=str(payload.get("target_kind", "")),
        target_ref=str(payload.get("target_ref", "")),
        applied=False,
        idempotent=True,
        detail="this exact request already applied; the effect was not repeated",
        expected_revision=expected_revision,
        view=view,
    )


def _compose_work_view(
    store: RunStore, item_id: str, reads: dict[str, Any], moment: datetime
) -> WorkViewDTO:
    """One work view over this store. ``RunStore`` satisfies the composer's
    structural read surface; the protocol just does not spell it out."""
    return get_work_view(cast(Any, store), item_id, composed_at=moment, **reads)


def _apply_work_action(
    store: RunStore,
    *,
    view: WorkViewDTO,
    offer: WorkActionDTO,
    config: DispatchConfig,
    expected_revision: str,
    reason: str,
    now: datetime,
    source: str,
) -> str:
    """Attest the intent, run the owning reconciler, then record the effect.

    The two events are deliberately not one. A request that dies inside the
    reconciler leaves the intent standing and no applied record, which is what
    keeps it retryable rather than mistaken for a completed one.
    """
    attested = {
        "view": view,
        "offer": offer,
        "config": config,
        "expected_revision": expected_revision,
        "source": source,
    }
    _emit_work_action(store, WORK_ACTION_REQUESTED, **attested)  # type: ignore[arg-type]
    detail = _WORK_EFFECTS[offer.action](
        _WorkEffect(store, config, offer.target_ref, reason, now, source)
    )
    _emit_work_action(
        store,
        WORK_ACTION_APPLIED,
        extra={"detail": detail},
        **attested,  # type: ignore[arg-type]
    )
    return detail


def perform_work_action(
    store: RunStore,
    *,
    item_id: str,
    action: str,
    expected_revision: str,
    config: DispatchConfig,
    reason: str = "",
    pull_request: PullRequestFact | None = None,
    tracker_item: TrackerItemFact | None = None,
    delivery_repo: str | None = None,
    now: datetime | None = None,
    source: str = "cli",
) -> WorkActionResultDTO:
    """Perform ONE centrally derived safe action against ONE work item.

    Raises before any effect: :class:`VerbDenied` for an actor outside
    ``[approval].approvers``, :class:`WorkActionStale` when the caller's
    ``expected_revision`` is not the current one, :class:`WorkActionUnavailable`
    when the projection does not offer the action, and ``ValueError`` when a
    decision that must be explained arrives without a reason.
    """
    stamp = now or datetime.now(UTC)
    reads: dict[str, Any] = {
        "pull_request": pull_request,
        "tracker_item": tracker_item,
        "delivery_repo": delivery_repo,
        # The same in-flight bound the console's projection was composed under.
        # ``view_revision`` digests the composed view, so composing this one
        # against a different age would make every action on an item with a
        # pending row read as stale.
        "pending_max_age_s": pending_run_max_age_s(config),
    }
    before = _compose_work_view(store, item_id, reads, stamp)
    _approver_gate(store, config)
    replay = _applied_request(store, item_id, action, expected_revision)
    if replay is not None:
        return _replayed(
            replay, action=action, expected_revision=expected_revision, view=before
        )
    _require_current(before, expected_revision)
    offer = _offer_or_raise(before, action)
    if offer.requires_reason and not reason.strip():
        raise ValueError(f"{action!r} requires a reason")
    detail = _apply_work_action(
        store,
        view=before,
        offer=offer,
        config=config,
        expected_revision=expected_revision,
        reason=reason,
        now=stamp,
        source=source,
    )
    return WorkActionResultDTO(
        action=action,
        target_kind=offer.target_kind,
        target_ref=offer.target_ref,
        applied=True,
        detail=detail,
        expected_revision=expected_revision,
        view=_compose_work_view(store, item_id, reads, stamp),
    )
