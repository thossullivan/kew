"""Pre-transaction orphan recovery and generation-bound capacity exclusions."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Any

from kew import process_groups, process_termination, stop_recovery
from kew.run_store_runs import _OWNER_COLUMNS
from kew.runtime_names import OPENSHELL_RUNTIME

if TYPE_CHECKING:
    from kew.run_store import RunRecord, RunStore, SqliteRunStore

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeadPending:
    """The exact owner and reservation generation observed before probing."""

    record: RunRecord
    reservation_run_id: str | None


def prepare_admission(store: SqliteRunStore) -> list[DeadPending]:
    """Share one owner snapshot, keeping unknown survivors in the fleet count."""
    store.reap_cancelled_runs()
    with store._txn_lock:
        pending = store.list_pending()
        rows = list(
            store._execute("SELECT epic_ref, item_id, run_id FROM epic_reservations")
        )
    if not pending:
        return []
    by_item = {row["item_id"]: row["run_id"] for row in rows}
    generations = {(row["epic_ref"], row["item_id"], row["run_id"]) for row in rows}
    snapshot = process_groups.owner_snapshot()
    dead = []
    for record in pending:
        left = _left_for_host_restart_tick(store, record, generations)
        if left is not None:
            # The tick reaps it either way; only a gone container frees its slot.
            if _container_gone(store, record):
                dead.append(left)
            continue
        # A reservation without its run row remains a separate capacity claim.
        item_id = record.item_id or str(record.issue_num)
        reservation = by_item.get(item_id)
        proof = DeadPending(
            record, reservation if reservation == record.run_id else None
        )
        if _reap_orphan(store, proof, snapshot):
            dead.append(proof)
    return dead


def _reap_orphan(
    store: SqliteRunStore, proof: DeadPending, snapshot: process_groups.OwnerSnapshot
) -> bool:
    """Reap *proof*'s run when it never started, or its owner is proven dead."""
    if reap_dead_never_started(store, proof, snapshot=snapshot).reaped:
        return True
    if not _orphan_ready(store, proof.record, snapshot):
        return False
    _finalize_dead_pending(store, proof)
    return True


def _dispatch_binding_identity(event: Any) -> tuple[str, str, str] | None:
    """Return one complete reservation generation named by a binding event."""
    payload = event.payload or {}
    epic_ref = payload.get("epic_ref")
    item_id = payload.get("item_id")
    reservation_run_id = payload.get("reservation_run_id")
    if (
        not isinstance(epic_ref, str)
        or not isinstance(item_id, str)
        or not isinstance(reservation_run_id, str)
    ):
        return None
    return epic_ref, item_id, reservation_run_id


def _pinned_reservation_run_id(
    store: SqliteRunStore,
    record: RunRecord,
    reservations: set[tuple[str, str, str]],
) -> str | None:
    """Find the current reservation generation pinned to this child run."""
    bindings = store.list_audit_events(
        event="dispatch_reservation_bound", run_id=record.run_id, limit=None
    )
    identity = _unique_pinned_binding(bindings)
    item_id = record.item_id or str(record.issue_num)
    if identity is None or identity not in reservations or identity[1] != item_id:
        return None
    return identity[2]


def _unique_pinned_binding(bindings: list[Any]) -> tuple[str, str, str] | None:
    """Return one unambiguous identity when any matching event is pinned."""
    identities = {_dispatch_binding_identity(event) for event in bindings}
    if len(identities) != 1:
        return None
    identity = next(iter(identities))
    if identity is None:
        return None
    if not any((event.payload or {}).get("pinned") is True for event in bindings):
        return None
    return identity


def _left_for_host_restart_tick(
    store: SqliteRunStore,
    record: RunRecord,
    generations: set[tuple[str, str, str]],
) -> DeadPending | None:
    """The slot admission frees while leaving *record*'s reap to the tick.

    The epic tick owns host-restart classification and its atomic retry, so
    admission ignores this proven-dead generation (the run and the reservation
    its pinned binding names) for capacity, but leaves both for that
    reconciliation. A remote run's agent may outlive this machine's reboot,
    and so may an OpenShell agent, which lives in its gateway, so only a local
    placement's earlier boot proves its slot free. ``None`` leaves the run to
    the ordinary orphan path.

    A contained run is left to the tick too (#2971): the earlier boot already
    proves its client gone, so neither the owner probe nor the startup grace
    of :func:`_orphan_ready` has anything left to wait for. Its slot is freed
    only once the runtime says the recorded container is absent
    (:func:`_container_gone`); one that restarted with the daemon, one merely
    stopped, or one the runtime cannot answer for, keeps the run counted until
    the tick stops it.
    """
    if (
        stop_recovery.record_placed_remotely(record)
        or OPENSHELL_RUNTIME in (record.container_runtime, record.cleanup_runtime)
        or not process_groups.from_earlier_boot(record.owner_boot_id)
    ):
        return None
    reservation = _pinned_reservation_run_id(store, record, generations)
    return None if reservation is None else DeadPending(record, reservation)


def _container_gone(store: SqliteRunStore, record: RunRecord) -> bool:
    """Whether *record*'s container, if it had one, is proved absent.

    A ``stopped`` container still exists, holding its name and its checkout,
    so it frees no slot on its own (#3009 F2): only the runtime saying it is
    gone does, or kew removing it itself (:func:`reap_never_started`,
    :func:`stopped_container_remover`). The runtime saying so counts only
    from the daemon the run's launch recorded.
    """
    if not record.container_name and not record.container_runtime:
        return True
    runtime = record.cleanup_runtime or record.container_runtime
    if runtime is None:
        return False
    return (
        stop_recovery.record_container_state(runtime, record) == "absent"
        and stop_recovery.absence_refusal(runtime, recorded_daemon(store, record))
        is None
    )


def recorded_daemon(store: RunStore, record: RunRecord) -> str | None:
    """The daemon *record*'s launch created its container on, if it said.

    ``None`` for a run launched before kew recorded it (or on a runtime with
    no daemon to name); :func:`kew.stop_recovery.absence_refusal` then keeps
    the behavior such a run always had.
    """
    key = stop_recovery.container_daemon_key(record.run_id)
    return _locked_read(store, lambda: store.get_meta(key))


def _locked_read[T](store: RunStore, read: Callable[[], T]) -> T:
    """Run one store read under the store's lock.

    Admission runs these reads before its transaction, on a connection a TUI
    or a server may share across threads (``check_same_thread=False``). The
    store's plain readers (``get_meta``, ``read_ledger_id``,
    ``list_audit_events``) take no lock of their own, and one used while
    another thread is mid-statement on the same connection fails with
    sqlite3's "bad parameter or other API misuse". A store with no lock (a
    test double) is read as it is.
    """
    lock = getattr(store, "_txn_lock", None)
    with lock if lock is not None else nullcontext():
        return read()


def _orphan_ready(
    store: SqliteRunStore, record: RunRecord, snapshot: process_groups.OwnerSnapshot
) -> bool:
    alive = process_groups.owner_alive(
        record.pgid or 0,
        record.owner_pid,
        record.owner_start_time,
        record.owner_ppid,
        record.owner_command,
        record.owner_start_token,
        snapshot=snapshot,
    )
    if alive is not False:
        return False
    if stop_recovery.pending_owner_is_dead(
        record,
        datetime.now(UTC),
        snapshot=snapshot,
        remove_stopped=stopped_container_remover(store),
        daemon=recorded_daemon(store, record),
    ):
        return True
    # Container recovery still requires independent container death evidence.
    if record.container_name:
        return _contained_stop_ready(store, record)
    terminated = process_termination.stop_orphaned_attempt(record, store)
    store.append_audit_event(
        timestamp=datetime.now(UTC).isoformat(),
        event="orphaned_attempt_terminated"
        if terminated
        else "orphaned_attempt_retained",
        run_id=record.run_id,
        issue_num=record.issue_num,
        payload={"attempt_pgid": record.attempt_pgid, "reason": "admission_owner_dead"},
    )
    return terminated


def _contained_stop_ready(store: SqliteRunStore, record: RunRecord) -> bool:
    """An explicit completed fleet stop does not need the startup grace."""
    if record.error_category != "killed":
        return False
    with store._txn_lock:
        tasks = store.list_audit_events(
            event="stop_label_reset_requested", run_id=record.run_id
        )
    if not tasks:
        return False
    runtime = str((tasks[0].payload or {}).get("runtime", ""))
    return bool(
        stop_recovery.contained_stop_reason(
            record,
            datetime.now(UTC),
            partial(stop_recovery.record_container_state, runtime, record),
        )
    )


#: The one event a never-started run's reap writes, on every path (#3124).
NEVER_STARTED_REAPED = "never_started_run_reaped"
#: Written BEFORE kew removes a dead run's container, naming it, so a removal
#: whose later audit is lost is still on the chain (the #2973 pattern).
CONTAINER_REMOVAL_INTENDED = "dead_run_container_removal_intended"
#: Written once the removal is done, when no reap event says so in its place.
CONTAINER_REMOVED = "dead_run_container_removed"

#: Which rule a removal was made under.
NEVER_STARTED_RULE = "never_started"
STOPPED_RULE = "stopped_after_attempt"

LIVENESS_ACTOR = ("policy", "liveness-reconcile")


@dataclass(frozen=True)
class NeverStartedReap:
    """What one application of the never-started rule did.

    ``reaped`` is True only when the run was finalized. ``detail`` is why it
    was retained, or what was done. ``removed`` is the container id kew
    removed, which a run that was not finalized can still carry.
    """

    reaped: bool
    detail: str
    category: str | None = None
    removed: str | None = None


def never_started_category(store: RunStore, record: RunRecord) -> str:
    """What a never-started run is finalized as.

    A run already stopped keeps ``killed``: its container not starting is how
    the stop landed. A stop is ``killed`` intent that ``kew kill`` or
    ``kew stop`` recorded with its label reset, or that a stop latch recorded
    alone. The driver's own orphan-cleanup intent also writes ``killed``, but
    with a cleanup runtime and no label reset; that run is a container start
    failure like any other.
    """
    if record.error_category != "killed":
        return stop_recovery.CONTAINER_START_FAILED
    if record.cleanup_runtime is None or _locked_read(
        store,
        lambda: store.list_audit_events(
            1, event="stop_label_reset_requested", run_id=record.run_id
        ),
    ):
        return "killed"
    return stop_recovery.CONTAINER_START_FAILED


def reap_never_started(
    store: RunStore,
    record: RunRecord,
    now: datetime,
    *,
    finalize: Callable[[str], bool],
    actor: tuple[str, str],
    epic_ref: str | None,
    snapshot: process_groups.OwnerSnapshot | None = None,
    repo: str | None = None,
    guard: Callable[[], str | None] | None = None,
) -> NeverStartedReap:
    """Free the slot of a dead run whose container never started (#3124).

    The rule is :func:`kew.stop_recovery.never_started_verdict`. When it holds,
    the intent to remove the container is recorded, the container is removed
    by id, and *finalize* is called with the category; it finalizes the row
    against the exact owner that was probed. Only then is the one
    :data:`NEVER_STARTED_REAPED` event written. A container removed for a run
    *finalize* then refused is recorded as removed, and the result says so.

    The owner, grace and attempt checks come first and read no store, so a
    live or unprovable row (most of them, on every admission) costs nothing
    more. *guard*, the caller's own refusal, runs next. Every store read
    after that holds the store's lock (:func:`_locked_read`).
    """
    refused = stop_recovery.never_started_refusal(record, now, snapshot)
    if refused is None and guard is not None:
        refused = guard()
    if refused is not None:
        return NeverStartedReap(False, refused)
    verdict = stop_recovery.never_started_container_verdict(
        record,
        ledger_id=_locked_read(store, store.read_ledger_id),
        daemon=recorded_daemon(store, record),
    )
    if isinstance(verdict, str):
        return NeverStartedReap(False, verdict)
    audit = _removal_audit(record, verdict, NEVER_STARTED_RULE, actor, repo)
    if not remove_audited(store, audit, verdict):
        return NeverStartedReap(False, "the runtime did not remove the container")
    category = never_started_category(store, record)
    if not finalize(category):
        return _removed_but_not_reaped(store, audit, verdict)
    reason = (
        "Stopped before its container started; owner dead past the startup "
        "grace; container removed and slot freed"
        if category == "killed"
        else "Owner dead past the startup grace and its container never "
        "started; container removed and slot freed"
    )
    store.append_audit_event(
        timestamp=now.isoformat(),
        event=NEVER_STARTED_REAPED,
        **_audit_envelope(audit),
        payload={
            **audit["payload"],
            "reason": reason,
            "error_category": category,
            "epic_ref": epic_ref,
        },
    )
    return NeverStartedReap(True, reason, category, verdict.container_id)


def _removed_but_not_reaped(
    store: RunStore, audit: dict[str, Any], container: stop_recovery.DeadContainer
) -> NeverStartedReap:
    """The removal is done but the row moved: say exactly that, on the chain too."""
    detail = "the run was no longer pending under the owner that was probed"
    if container.container_id is None:
        return NeverStartedReap(False, detail)
    _append_removal(store, CONTAINER_REMOVED, audit, run_finalized=False)
    return NeverStartedReap(
        False,
        f"container {container.container_id} was removed, but {detail}; "
        "the run was left as it was",
        removed=container.container_id,
    )


def _removal_audit(
    record: RunRecord,
    container: stop_recovery.DeadContainer,
    rule: str,
    actor: tuple[str, str],
    repo: str | None,
) -> dict[str, Any]:
    """The fields every event about one container removal carries."""
    return {
        "run_id": record.run_id,
        "issue_num": record.issue_num,
        "item_id": record.item_id,
        "repo": repo,
        "actor_type": actor[0],
        "actor_id": actor[1],
        "payload": {
            "container_name": record.container_name,
            "container_id": container.container_id,
            "runtime": container.runtime,
            "state": container.state,
            "rule": rule,
        },
    }


def _audit_envelope(audit: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in audit.items() if key != "payload"}


def _append_removal(
    store: RunStore, event: str, audit: dict[str, Any], **extra: Any
) -> int | None:
    """Write one removal event once per run and container id."""
    payload = audit["payload"]
    return store.append_audit_event_once(
        dedup_key=f"{event}:{audit['run_id']}:{payload['container_id']}",
        timestamp=datetime.now(UTC).isoformat(),
        event=event,
        **_audit_envelope(audit),
        payload={**payload, **extra},
    )


def remove_audited(
    store: RunStore, audit: dict[str, Any], container: stop_recovery.DeadContainer
) -> bool:
    """Record the intent to remove *container*, then remove it by its full id.

    Nothing is removed under an intent that could not be written. A container
    the runtime already reported absent needs no removal and no intent.
    """
    identity = container.container_id
    if identity is None:
        return True
    try:
        _append_removal(store, CONTAINER_REMOVAL_INTENDED, audit)
    except Exception as exc:  # the container is kept: nothing was removed
        _logger.warning(
            "container %s of run %s kept: its removal could not be audited: %s",
            identity,
            audit["run_id"],
            exc,
        )
        return False
    return stop_recovery.remove_stopped_container(container.runtime, identity)


def stopped_container_remover(
    store: SqliteRunStore, actor: tuple[str, str] = LIVENESS_ACTOR
) -> stop_recovery.StoppedRemoval:
    """Remove a dead run's stopped container, audited write-ahead (#3009 F2).

    Handed to :func:`kew.stop_recovery.pending_owner_is_dead`, which has
    already proven the run's owner and its registered client dead past the
    startup grace. The container is read by id again, with its state; the
    intent naming it is recorded; it is removed without force; and the
    removal is recorded. True only once the container is gone. A run that
    owns a live continuation keeps its container (:func:`continuation_refusal`).
    """

    def remove(runtime: str, record: RunRecord) -> bool:
        owned = continuation_refusal(store, record)
        if owned is not None:
            _logger.info("run %s: container kept: %s", record.run_id, owned)
            return False
        container = stop_recovery.read_stopped_container(
            runtime, record, ledger_id=_locked_read(store, store.read_ledger_id)
        )
        if isinstance(container, str):
            _logger.info("run %s: container kept: %s", record.run_id, container)
            return False
        audit = _removal_audit(record, container, STOPPED_RULE, actor, None)
        if not remove_audited(store, audit, container):
            return False
        try:
            _append_removal(store, CONTAINER_REMOVED, audit)
        except Exception as exc:  # the removal stands; its intent is on the chain
            _logger.warning(
                "removal of container %s awaits its audit: %s",
                container.container_id,
                exc,
            )
        return True

    return remove


def reap_dead_never_started(
    store: SqliteRunStore,
    proof: DeadPending,
    *,
    snapshot: process_groups.OwnerSnapshot | None = None,
    actor: tuple[str, str] = LIVENESS_ACTOR,
) -> NeverStartedReap:
    """:func:`reap_never_started` for admission and ``kew settle``.

    The row is finalized against the owner that was probed, and the
    reservation generation it held, if any, released. A run that owns a live
    continuation is never reaped here: the epic tick owns that recovery, and
    the operator moves it with ``kew epic continue`` or ``kew epic abandon``.
    """
    record = proof.record
    now = datetime.now(UTC)

    def guard() -> str | None:
        owned = continuation_refusal(store, record)
        if owned is not None:
            _logger.info("run %s retained: %s", record.run_id, owned)
        return owned

    def finalize(category: str) -> bool:
        if not store.finalize_orphaned_pending(
            record.run_id,
            error_category=category,
            finished_at=now,
            expected_owner=record,
        ):
            return False
        if proof.reservation_run_id is not None:
            store.release_epic_node_item(
                record.item_id or str(record.issue_num),
                run_id=proof.reservation_run_id,
            )
        return True

    return reap_never_started(
        store,
        record,
        now,
        finalize=finalize,
        actor=actor,
        epic_ref=_reserved_epic(store, proof),
        snapshot=snapshot,
        guard=guard,
    )


def continuation_refusal(store: SqliteRunStore, record: RunRecord) -> str | None:
    """Why *record* may not be reaped here: the live continuation it owns.

    The tick's own test (``_continuation_owned_by_run``) widened to every
    epic: a continuation record that is not ``terminal`` and names this run as
    its continuation run. ``None`` when it owns none.
    """
    with store._txn_lock:
        row = store._execute(
            "SELECT epic_ref, item_id, state FROM epic_continuations"
            " WHERE continuation_run_id = ? AND state != 'terminal'"
            " ORDER BY epic_ref, item_id LIMIT 1",
            (record.run_id,),
        ).fetchone()
    if row is None:
        return None
    epic, item = row["epic_ref"], row["item_id"]
    return (
        f"the run owns continuation {epic}/{item} ({row['state']}); move it "
        f"with `kew epic continue {epic} {item}` or "
        f"`kew epic abandon {epic} {item} --reason ...` first"
    )


def _reserved_epic(store: SqliteRunStore, proof: DeadPending) -> str | None:
    """The epic whose reservation generation *proof* holds, if it holds one."""
    if proof.reservation_run_id is None:
        return None
    with store._txn_lock:
        row = store._execute(
            "SELECT epic_ref FROM epic_reservations WHERE item_id = ? AND run_id = ?",
            (
                proof.record.item_id or str(proof.record.issue_num),
                proof.reservation_run_id,
            ),
        ).fetchone()
    return None if row is None else str(row["epic_ref"])


def reservation_generation(store: SqliteRunStore, record: RunRecord) -> DeadPending:
    """*record* with the reservation generation it holds, if it holds one."""
    item_id = record.item_id or str(record.issue_num)
    with store._txn_lock:
        row = store._execute(
            "SELECT run_id FROM epic_reservations WHERE item_id = ?", (item_id,)
        ).fetchone()
    held = None if row is None else row["run_id"]
    return DeadPending(record, held if held == record.run_id else None)


def _finalize_dead_pending(store: SqliteRunStore, proof: DeadPending) -> None:
    record = proof.record
    now = datetime.now(UTC)
    if not store.finalize_orphaned_pending(
        record.run_id,
        error_category="orphaned_pending_reaped",
        finished_at=now,
        expected_owner=record,
    ):
        return
    if proof.reservation_run_id is not None:
        store.release_epic_node_item(
            record.item_id or str(record.issue_num), run_id=proof.reservation_run_id
        )
    if record.error_category == "killed" and not record.container_name:
        store.append_audit_event(
            timestamp=now.isoformat(),
            event="host_stop_reconciled",
            run_id=record.run_id,
            issue_num=record.issue_num,
            payload={
                "reason": "Fleet stop completed: owner and attempt groups are dead"
            },
        )
    store.append_audit_event(
        timestamp=now.isoformat(),
        event="orphaned_run_reaped",
        run_id=record.run_id,
        issue_num=record.issue_num,
        payload={
            "reason": "Owner dead; authenticated attempts terminated before admission"
        },
    )


def _proof_json(proofs: list[DeadPending]) -> str:
    rows = []
    for proof in proofs:
        record = proof.record
        rows.append(
            {
                **{column: getattr(record, column) for column in _OWNER_COLUMNS},
                "run_id": record.run_id,
                "item_id": record.item_id or str(record.issue_num),
                "started_at": record.started_at.astimezone(UTC).isoformat(),
                "reservation_run_id": proof.reservation_run_id,
            }
        )
    return json.dumps(rows)


def stage_shadowed(row: str, siblings: str) -> str:
    """SQL true when *row* is a ``review_stage`` row whose child's run is in *siblings*.

    A detached review (#2459) writes two pending rows for one piece of work:
    the stage row, and the reviewer or remediation run its child starts under
    the stage's held slot. The child claims the stage row's ownership before it
    starts anything, so the run it starts carries the same owner pid and start
    time; while such a run is pending the stage row is the same work and counts
    no slot of its own (#2697). Only the owner identity links them: another
    process's run of the same item never shadows the stage, and a row with no
    recorded owner identity shadows nothing, so this can only ever drop a
    double count, never a run. *row* and *siblings* are constant SQL names.
    """
    # Local import: run_store imports this module.
    from kew.run_store import REVIEW_STAGE_RUN_KIND

    return (
        f"({row}.run_kind = '{REVIEW_STAGE_RUN_KIND}'"  # nosec B608
        f" AND {row}.owner_pid IS NOT NULL"
        f" AND {row}.owner_start_time IS NOT NULL"
        f" AND EXISTS (SELECT 1 FROM {siblings} AS started"
        f" WHERE started.status = 'pending'"
        f" AND started.run_id != {row}.run_id"
        f" AND started.run_kind IS NOT '{REVIEW_STAGE_RUN_KIND}'"
        f" AND COALESCE(started.item_id, CAST(started.issue_num AS TEXT))"
        f" = COALESCE({row}.item_id, CAST({row}.issue_num AS TEXT))"
        f" AND started.owner_pid = {row}.owner_pid"
        f" AND started.owner_start_time = {row}.owner_start_time))"
    )


def count_admission_runs(
    store: SqliteRunStore,
    exclude_item: str | None,
    proofs: list[DeadPending],
    *,
    exclude_run: str | None = None,
) -> int:
    """Recheck every captured generation using SQL alone under BEGIN IMMEDIATE.

    ``exclude_run`` is the held slot the admitting run was started under: its
    pending row and its reservation do not count (#2459, #2628).
    """
    matches = " AND ".join(
        f"runs.{column} IS json_extract(proof.value, '$.{column}')"
        for column in (*_OWNER_COLUMNS, "run_id", "started_at")
    )
    # Only constant column names enter matches; every observed value is bound.
    sql = (
        "WITH proven_dead AS ("  # nosec B608
        " SELECT runs.run_id, json_extract(proof.value, '$.item_id') AS item_id,"
        " json_extract(proof.value, '$.reservation_run_id') AS reservation_run_id"
        " FROM runs JOIN json_each(?) AS proof ON "
        + matches
        + " AND COALESCE(runs.item_id, CAST(runs.issue_num AS TEXT))"  # nosec B608
        " = json_extract(proof.value, '$.item_id')), "
        "live_pending AS ("
        " SELECT *, COALESCE(item_id, CAST(issue_num AS TEXT)) AS live_id FROM runs"
        " WHERE status = 'pending' AND run_id IS NOT ?"
        " AND run_id NOT IN (SELECT run_id FROM proven_dead)) "
        # Every live pending run holds its own slot (#2628), but a review
        # stage whose child's run is live is that run's slot (#2697); a
        # reservation adds one only while its item has no live pending run,
        # and the admitting item's own reservation never counts against it.
        "SELECT (SELECT COUNT(*) FROM live_pending AS pending"
        " WHERE NOT " + stage_shadowed("pending", "live_pending") + ")"
        " + (SELECT COUNT(DISTINCT reservation.item_id)"
        " FROM epic_reservations AS reservation"
        " WHERE (? IS NULL OR reservation.item_id != ?)"
        " AND reservation.run_id IS NOT ?"
        " AND reservation.item_id NOT IN (SELECT live_id FROM live_pending)"
        " AND NOT EXISTS (SELECT 1 FROM proven_dead AS dead"
        " WHERE dead.item_id = reservation.item_id"
        " AND dead.reservation_run_id = reservation.run_id))"
    )
    row = store._execute(
        sql,
        (_proof_json(proofs), exclude_run, exclude_item, exclude_item, exclude_run),
    ).fetchone()
    return int(row[0])
