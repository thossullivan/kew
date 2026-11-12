"""The review stage, launched detached so a tick never waits on a review (#2459).

Implementation runs already leave the tick through ``DetachedDispatchSink``; the
review stage did not. A reviewer, a remediation and the merge-main step each ran
inside the tick, so a tick lasted as long as its slowest review (30-70 minutes
in the October drive) and everything that needed the driver between ticks
waited with it.

With a :data:`ReviewDetacher` wired, the stage does for each due node what the
dispatch path does for a ready one, and returns:

1. reserve the node under the ``review`` pseudo-epic, under the fleet cap — the
   same atomic row a synchronous reviewer took, so the slot is held exactly as
   long as the work is in flight;
2. write the ``review_stage`` run row keyed to that reservation;
3. spawn ``kew epic _review-node`` in its own session.

A running local merge verify defers a due node before step 1 (#3190): nothing
is reserved, recorded or spawned for it, and it waits with the deferral as
its reason until the verify ends.

The child (:func:`run_detached_review`) takes the node's ONE action through the
very code the synchronous path runs (``review_stage._drive_one_node``), so the
verdict, sidecar, review-round row, holds, round numbering and publication are
recorded exactly as before. It runs that action under the reservation the tick
took — nothing inside it re-reserves — then finalizes its run row and releases
the reservation. A child that dies leaves a ``pending`` row whose owner is
provably gone; the next tick's ``_reconcile_reservations`` reaps it ``killed``
and frees the slot, as it does for a dead dispatch child. No round is recorded
for it: rounds are only written by a child that collected a verdict.

The child is spawned with its output on ``/dev/null``, so it keeps its own log
(#2656): ``review-stage-<item>-<run8>.log`` next to the dispatch logs, whose
path the tick writes on the stage row. A raise in the action lands there with
its traceback, and its type and message ride on the row's ``error_category``
after :data:`STAGE_RAISED` — the evidence an operator reads without the log.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import uuid
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from kew.graph.domain import NodeStatus
from kew.graph.driver.facts import NodeFacts
from kew.pseudo_epics import REVIEW_EPIC_REF
from kew.run_store import REVIEW_STAGE_RUN_KIND, RunRecord
from kew.verify_lease import verify_deferral

if TYPE_CHECKING:
    from kew.config import DispatchConfig
    from kew.graph.driver.review_stage import ReviewStageOutcome, _DueNode
    from kew.run_store import RunStatus, RunStore

_logger = logging.getLogger(__name__)

#: What a node whose review action is running in a detached child reports.
DETACHED_WAITING_REASON = "review stage running detached — collected next tick"

#: The terminal category of a stage row whose child could not be spawned.
SPAWN_FAILED = "review_stage_spawn_failed"
#: The terminal category of a stage row whose child finished its action.
STAGE_FINISHED = "review_stage_finished"
#: The terminal category of a stage row whose action raised in the child; the
#: row carries it as ``review_stage_raised: <Type>: <message>``.
STAGE_RAISED = "review_stage_raised"

#: How much of a raised exception's ``<Type>: <message>`` the row keeps.
_RAISED_EVIDENCE_CHARS = 300

_HELD_RESERVATION: ContextVar[str | None] = ContextVar(
    "kew_review_stage_reservation", default=None
)


def held_reservation() -> str | None:
    """The run id of the review reservation this action already holds, if any.

    Set only inside a detached child for the duration of its one action. Every
    reserve the action would otherwise take for the node — the reviewer's, the
    remediation's, the residual filing's — is already held, so it is skipped:
    ``item_id`` is unique across ``epic_reservations``, and re-reserving would
    refuse the child's own work.
    """
    return _HELD_RESERVATION.get()


@contextmanager
def holding_reservation(run_id: str | None) -> Iterator[None]:
    """Run the block as the holder of review reservation *run_id*.

    A hand-run ``kew review`` holds the item's reservation too (#3208): its
    agent's admission then drops that reservation from the fleet count, as a
    detached child's does, instead of counting its own slot twice.
    """
    token = _HELD_RESERVATION.set(run_id)
    try:
        yield
    finally:
        _HELD_RESERVATION.reset(token)


@dataclass(frozen=True)
class DetachedReview:
    """Everything a detached child needs to take one node's review action."""

    epic_ref: str
    node_id: str
    item_id: str
    run_id: str
    facts: NodeFacts
    integration_only: bool


#: Spawns the child for one reserved node and returns; raises when it cannot.
ReviewDetacher = Callable[[DetachedReview], None]


def facts_to_json(facts: NodeFacts) -> str:
    """The tick's view of the node, carried to the child verbatim."""
    payload = asdict(facts)
    payload["labels"] = sorted(facts.labels)
    payload["admission_status"] = facts.admission_status.value
    return json.dumps(payload, sort_keys=True)


def facts_from_json(text: str) -> NodeFacts:
    """Inverse of :func:`facts_to_json`."""
    payload = json.loads(text)
    payload["labels"] = frozenset(payload["labels"])
    payload["admission_status"] = NodeStatus(payload["admission_status"])
    return NodeFacts(**payload)


def stage_log_path(config: DispatchConfig, item_id: str, run_id: str) -> Path:
    """Where the child for this stage row writes its log, next to dispatch logs.

    The item part is sanitized exactly as a dispatch log's is, so an opaque
    tracker id can neither escape the log directory nor collide on ``0``.
    """
    from kew.dispatch_core import _dispatch_log_item_id

    issue_num = int(item_id) if item_id.isdigit() else 0
    safe_item = _dispatch_log_item_id(issue_num, item_id)
    log_dir = Path(config.logging.dir).resolve()
    return log_dir / f"review-stage-{safe_item}-{run_id[:8]}.log"


def _stage_row(item_id: str, run_id: str, config: DispatchConfig) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        issue_num=int(item_id) if item_id.isdigit() else 0,
        item_id=item_id,
        branch="",
        status="pending",
        exit_code=0,
        started_at=datetime.now(UTC),
        run_kind=REVIEW_STAGE_RUN_KIND,
        repo=config.project.repo or None,
        log_file=stage_log_path(config, item_id, run_id),
    )


def launch_detached(
    due: list[_DueNode],
    detacher: ReviewDetacher,
    *,
    outcome: ReviewStageOutcome,
    epic_ref: str,
    config: DispatchConfig,
    store: RunStore,
    worker_cap: int,
    max_concurrent: int,
    in_flight: int = 0,
    new_run_id: Callable[[], str] = lambda: str(uuid.uuid4()),
) -> None:
    """Reserve, record and spawn each due node's action; never wait on one.

    *in_flight* counts the children earlier ticks started that are still live
    (:func:`report_in_flight`). They hold width as surely as the ones started
    now, so at most ``max_concurrent`` children run at once across ticks — the
    width the synchronous stage ran under — and each holds an under-cap
    reservation, so the fleet cap holds too. A full fleet stops the pass — the
    rest are due again next tick, exactly as a synchronous reviewer's
    ``fleet_full``.
    """
    launched = in_flight
    for node in due:
        if launched >= max_concurrent:
            return
        if _defers_for_verify(node, outcome=outcome, config=config, store=store):
            continue
        run_id = new_run_id()
        reserved = store.reserve_epic_node_item_under_cap(
            REVIEW_EPIC_REF, node.item_id, run_id, worker_cap
        )
        if reserved == "fleet_full":
            return
        if reserved != "reserved":
            continue
        try:
            store.save(_stage_row(node.item_id, run_id, config))
            detacher(
                DetachedReview(
                    epic_ref=epic_ref,
                    node_id=node.node_id,
                    item_id=node.item_id,
                    run_id=run_id,
                    facts=node.facts,
                    integration_only=node.integration_only,
                )
            )
        except Exception:
            _logger.exception(
                "review: could not start the detached review stage for %s — "
                "slot released, retried next tick",
                node.item_id,
            )
            _abandon(store, node.item_id, run_id)
            outcome.failed.append(node.node_id)
            continue
        launched += 1
        outcome.detached[node.node_id] = DETACHED_WAITING_REASON


def _defers_for_verify(
    node: _DueNode,
    *,
    outcome: ReviewStageOutcome,
    config: DispatchConfig,
    store: RunStore,
) -> bool:
    """Does a running local merge verify defer this node's detached stage?

    Read before the slot is reserved (#3190): a deferred node gets no
    reservation, no ``review_stage`` row, no log and no child, and the tick
    names the deferral as its waiting reason. The child reads the lease again
    at its own launch, for a verify that starts after this spawn; a node
    admitted here is not charged twice for one verify.
    """
    reason = verify_deferral(store, config, item_id=node.item_id, kind="review")
    if reason is None:
        return False
    outcome.refused[node.node_id] = reason
    return True


def report_in_flight(
    items: Iterable[tuple[str, str]],
    *,
    outcome: ReviewStageOutcome,
    store: RunStore,
) -> int:
    """Report the review-stage nodes a child an EARLIER tick started still holds.

    *items* is ``(node_id, item_id)`` for the tick's review-stage nodes. Such a
    node is not due (its live reservation keeps it out of the pass), so without
    this nothing in the tick would say the stage is working on it — a node
    waiting on a minutes-long merge-main step would read as an idle IN_REVIEW
    node, and the tick as stalled.

    Returns how many there are: each is a child still running, so it counts
    against the stage's width (:func:`launch_detached`).
    """
    live = {
        res.item_id: res.run_id for res in store.list_epic_reservations(REVIEW_EPIC_REF)
    }
    count = 0
    for node_id, item_id in items:
        run_id = live.get(item_id)
        row = store.get(run_id) if run_id is not None else None
        if row is not None and row.run_kind == REVIEW_STAGE_RUN_KIND:
            outcome.detached[node_id] = DETACHED_WAITING_REASON
            count += 1
    return count


def _abandon(store: RunStore, item_id: str, run_id: str) -> None:
    """Give back the slot first, then close the row (state before evidence)."""
    try:
        store.release_epic_node_item(item_id, run_id=run_id)
    finally:
        _finish(store, run_id, status="failure", category=SPAWN_FAILED)


def _owns(store: RunStore, item_id: str, run_id: str) -> bool:
    """Whether the reservation the tick took for this child is still its own."""
    return any(
        res.item_id == item_id and res.run_id == run_id
        for res in store.list_epic_reservations(REVIEW_EPIC_REF)
    )


def _claim_ownership(store: RunStore, run_id: str) -> bool:
    """Record this process as the stage row's owner, so its death is provable.

    Refuses when the row is gone or no longer ``pending``: the reconciler
    already decided this generation, and running now would act on a slot the
    driver may have handed to someone else.
    """
    from kew.process_groups import exclusive_owner_pid, identity_at_admission

    row = store.get(run_id)
    if row is None or row.status != "pending":
        return False
    owner = exclusive_owner_pid()
    owned = replace(
        row,
        pgid=owner,
        owner_pid=owner,
        **identity_at_admission(owner),
    )
    store.save(owned, update_ownership=True)
    return True


def run_detached_review(
    review: DetachedReview,
    *,
    drive: Callable[..., None],
    store: RunStore,
) -> bool:
    """The child's side: take the node's one action under the tick's reservation.

    *drive* is ``review_stage._drive_one_node`` with everything but the node
    bound. Returns False when this generation was no longer the child's to run
    (reaped, released or re-reserved since the tick spawned it) — then nothing
    is done and nothing is released. Otherwise the run row is finalized and the
    reservation released on every path out, a raise included.
    """
    if not _owns(store, review.item_id, review.run_id):
        claimed = False
    else:
        try:
            claimed = _claim_ownership(store, review.run_id)
        except Exception:
            # Still ours, but this process cannot be recorded as its owner, so
            # its death could never be proven: give the slot back now rather
            # than leave it pinned until the startup grace runs out.
            _abandon(store, review.item_id, review.run_id)
            raise
    if not claimed:
        _logger.warning(
            "review: detached stage %s for %s no longer owns its reservation — "
            "nothing run",
            review.run_id,
            review.item_id,
        )
        return False
    row = store.get(review.run_id)
    with _stage_log(row.log_file if row is not None else None):
        _run_action(review, drive=drive, store=store)
    return True


def _run_action(
    review: DetachedReview,
    *,
    drive: Callable[..., None],
    store: RunStore,
) -> None:
    """Take the claimed action, then release the slot and close the row."""
    from kew.graph.driver.overlay import DriverStatus
    from kew.graph.driver.review_stage import ReviewStageOutcome

    _logger.info(
        "review: detached stage %s started for %s (integration_only=%s)",
        review.run_id,
        review.item_id,
        review.integration_only,
    )
    category = STAGE_RAISED
    try:
        with holding_reservation(review.run_id):
            drive(
                review.node_id,
                review.item_id,
                review.facts,
                statuses={review.node_id: DriverStatus.ADVERSARIAL_REVIEW},
                outcome=ReviewStageOutcome(),
                integration_only=review.integration_only,
            )
        category = STAGE_FINISHED
    except Exception as exc:
        _logger.exception(
            "review: detached stage %s for %s raised", review.run_id, review.item_id
        )
        category = _raised_category(exc)
        raise
    finally:
        try:
            store.release_epic_node_item(review.item_id, run_id=review.run_id)
        finally:
            _finish(
                store,
                review.run_id,
                status="success" if category == STAGE_FINISHED else "failure",
                category=category,
            )
    _logger.info(
        "review: detached stage %s for %s finished", review.run_id, review.item_id
    )


def _raised_category(exc: Exception) -> str:
    """``review_stage_raised: <Type>: <message>``, one line, redacted, bounded."""
    from kew.credential_surface import redact_credentials

    detail = " ".join(f"{type(exc).__name__}: {exc}".split())
    detail = redact_credentials(detail)[:_RAISED_EVIDENCE_CHARS]
    return f"{STAGE_RAISED}: {detail}"


class _RedactingFormatter(logging.Formatter):
    """Format a record, traceback included, then redact host credentials."""

    def format(self, record: logging.LogRecord) -> str:
        from kew.credential_surface import redact_credentials

        return redact_credentials(super().format(record))


@contextmanager
def _stage_log(path: Path | None) -> Iterator[None]:
    """Route the ``kew`` loggers' INFO and above to the stage's own log file.

    A row without a path (written before #2656) or a log that cannot be opened
    runs the action unlogged rather than not at all.
    """
    if path is None:
        yield
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(path, encoding="utf-8")
    except OSError:
        _logger.warning("review: could not open the stage log %s", path)
        yield
        return
    handler.setLevel(logging.INFO)
    handler.setFormatter(
        _RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    kew_logger = logging.getLogger("kew")
    previous = kew_logger.level
    kew_logger.addHandler(handler)
    if kew_logger.getEffectiveLevel() > logging.INFO:
        kew_logger.setLevel(logging.INFO)
    try:
        yield
    finally:
        kew_logger.removeHandler(handler)
        kew_logger.setLevel(previous)
        handler.close()


def _finish(store: RunStore, run_id: str, *, status: RunStatus, category: str) -> None:
    """Close the stage row, unless something already decided it (a reap, a kill).

    A terminal row is never rewritten: a reconciler that proved this child dead,
    or a race handler that killed its reviewer, keeps its own outcome.
    """
    row = store.get(run_id)
    if row is None or row.status != "pending":
        return
    now = datetime.now(UTC)
    store.save(
        replace(
            row,
            status=status,
            error_category=category,
            exit_code=0 if status == "success" else 1,
            finished_at=now,
            duration_seconds=(now - row.started_at).total_seconds(),
        )
    )


class DetachedReviewSpawner:
    """Spawn ``kew epic _review-node`` in its own session, never waiting on it.

    The child inherits the driver's environment minus management authority (the
    ``popen`` boundary strips it), keeping only the credentials the review and
    remediation runners declare — the ones the synchronous stage used in-tick.
    """

    def __init__(
        self,
        config: DispatchConfig,
        *,
        base: str,
        popen: Callable[..., subprocess.Popen[str]] | None = None,
    ) -> None:
        self.config = config
        self.base = base
        self._popen = popen
        self._children: list[subprocess.Popen[str]] = []

    def reap(self) -> None:
        """Collect exited children without waiting for running ones."""
        self._children = [c for c in self._children if c.poll() is None]

    def _retained(self) -> tuple[str, ...]:
        from kew.graph.driver.review_launcher import review_runner
        from kew.runners import resolve_runner
        from kew.runners.registry import runner_required_credentials

        names = set(runner_required_credentials(review_runner(self.config)))
        names |= set(
            runner_required_credentials(resolve_runner(self.config).runner.name)
        )
        return tuple(sorted(names))

    def __call__(self, review: DetachedReview) -> None:
        from kew.graph.driver.dispatch import resolve_kew_console_script
        from kew.subprocess_utils import TOOL_PATHS
        from kew.subprocess_utils import popen as default_popen

        kew_path = resolve_kew_console_script()
        if kew_path is None:
            raise RuntimeError(
                "kew CLI not found — the detached review stage spawns "
                "`kew epic _review-node`, which requires the `kew` console script"
            )
        TOOL_PATHS["kew"] = kew_path
        cmd = [
            "kew",
            "epic",
            "_review-node",
            review.epic_ref,
            review.node_id,
            "--run-id",
            review.run_id,
            "--base",
            self.base,
            "--facts",
            facts_to_json(review.facts),
        ]
        if review.integration_only:
            cmd.append("--integration-only")
        launch = self._popen or default_popen
        self.reap()
        child = launch(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=dict(os.environ),
            start_new_session=True,
            retain_credentials=self._retained(),
        )
        self._children.append(child)


def reap_review_children(detacher: ReviewDetacher | None) -> None:
    """Collect exited review children; a simulated detacher has none."""
    if isinstance(detacher, DetachedReviewSpawner):
        detacher.reap()
