"""Immutable admission identity: the durable join from a run to its work.

Before this module a governed run could only be tied back to the work it was
admitted for by *guessing*: parse the title, read the branch/worktree path,
read ``runs.issue_num`` (a ``0`` sentinel for every non-numeric tracker), or —
worst — read ``runs.provider``, which names the INFERENCE provider and has
nothing to do with the work tracker. This module persists the composite
identity once, at admission, in its own normalized table:

    source tracker + opaque item id + delivery repository + epic/node + run
    + attempt

Design rules this module enforces:

* **Tracker is not a provider.** ``source_tracker`` accepts only a known work
  tracker (:data:`KNOWN_TRACKERS`). An inference-provider name (``anthropic``,
  ``openrouter``, ...) or a runner name is rejected with
  :class:`AdmissionIdentityError` rather than silently persisted — the field
  confusion this table exists to end must not be re-introduced through it.
* **Opaque ids stay opaque.** ``item_id`` is validated against the tracker's
  canonical form (:func:`kew.ids.validate_item_id`); it is never derived from a
  numeric shim and never rewritten into one.
* **Synthetic runs stay nullable.** A run with no work item (``epic plan``, a
  review pass) records ``item_id = NULL``. ``runs.item_id`` backfills such a
  row with its ``run_id`` so the fleet key stays distinct; copying that shim in
  here would assert a work item that does not exist.
* **Identity is immutable.** ``(run_id, attempt_number)`` is the primary key.
  Re-recording byte-identical values is an idempotent no-op; recording
  *different* values raises :class:`AdmissionIdentityConflictError`.

The write primitive (:meth:`AdmissionIdentityStoreMixin.admit_run_with_identity`)
is the ONE path both admission writers use — host dispatch
(``dispatch_core._save_pending_run``) and contained dispatch
(``dispatch_sandbox._record_sandbox_start``) — so the two can never drift.
It also exposes one bounded transactional enricher hook: later attempt-scoped
facts join the same transaction, and any enricher failure rolls the run row and
its identity back together.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import kew.run_store
from kew.ids import InvalidItemIdError, validate_item_id

if TYPE_CHECKING:
    from kew.run_store import EpicReservation, RunRecord, RunStore


DISPATCH_RESERVATION_BOUND = "dispatch_reservation_bound"


def capture_dispatch_reservation(
    store: RunStore | None,
    epic_ref: str | None,
    item_id: str,
    run_kind: str = "dispatch",
    *,
    expected_reservation_run_id: str | None = None,
    expected_reservation_generation: str | None = None,
) -> EpicReservation | None:
    """Capture the parent's reservation; reject a missing or replaced generation."""
    if (
        store is None
        or epic_ref is None
        or run_kind not in {"dispatch", "delegation", "loop"}
    ):
        if expected_reservation_run_id is not None:
            raise DispatchReservationReplaced
        return None
    reservation = next(
        (
            row
            for row in store.list_epic_reservations(epic_ref)
            if row.item_id == item_id
        ),
        None,
    )

    if expected_reservation_run_id is not None:
        _require_expected_reservation(
            reservation, expected_reservation_run_id, expected_reservation_generation
        )
    return reservation


def _require_expected_reservation(
    reservation: EpicReservation | None, run_id: str, generation: str | None
) -> None:
    # Epic reservations use their unique run_id as the generation token.
    if reservation is None or reservation.run_id != run_id:
        raise DispatchReservationReplaced
    if generation is not None and reservation.run_id != generation:
        raise DispatchReservationReplaced


KNOWN_TRACKERS = frozenset({"github", "linear"})
"""Work trackers a source identity may name — mirrors ``TrackerConfig.type``.

Deliberately a closed set: the whole point of the column is that it answers
"which tracker owns this id", so an unknown value would make the answer a
guess again.
"""

TRACKER_WORK_RUN_KINDS = frozenset(
    {"dispatch", "loop", "delegation", "review_remediation", "ci_fix", "resolve"}
)
"""Run kinds admitted on behalf of a concrete tracker work item.

All other kinds are synthetic unless they are deliberately added here.  In
particular, PR review, adversarial review, planning, and canary activity can
carry numeric or fleet-budget keys without those keys becoming source items.
"""

_ADMISSION_COLS = (
    "run_id",
    "attempt_number",
    "source_tracker",
    "item_id",
    "delivery_repo",
    "epic_ref",
    "node_id",
    "recorded_at",
)

_ADMISSION_SELECT = f"SELECT {', '.join(_ADMISSION_COLS)} FROM run_admission_identity"  # nosec B608 — column names are a module constant


class AdmissionIdentityError(ValueError):
    """An admission identity is not internally honest (bad tracker/id/attempt)."""


class AdmissionIdentityConflictError(RuntimeError):
    """A run's persisted admission identity was already written differently."""


class DispatchReservationReplaced(AdmissionIdentityConflictError):
    """The child no longer owns the reservation supplied by its parent."""

    def __init__(self) -> None:
        super().__init__("dispatch reservation changed before admission")


class ReadSnapshotWriteError(sqlite3.OperationalError):
    """A write ran inside ``read_snapshot()``, which reads through a read-only
    connection. An ``OperationalError`` so existing handlers still match it."""

    def __init__(self) -> None:
        super().__init__(
            "write attempted inside read_snapshot(): the block reads through a"
            " read-only connection; write after the block ends"
        )


def _is_snapshot_write(exc: sqlite3.OperationalError) -> bool:
    """A write, or a write transaction's BEGIN, reached a snapshot connection."""
    code = getattr(exc, "sqlite_errorcode", 0)
    if code & 0xFF == sqlite3.SQLITE_READONLY:
        return True
    return "cannot start a transaction within a transaction" in str(exc)


def _pin_snapshot(connection: sqlite3.Connection) -> sqlite3.Connection:
    """Begin the snapshot's read transaction and pin its image now, not at
    whatever the caller reads first. Closes *connection* if that fails."""
    try:
        connection.execute("BEGIN")
        connection.execute("SELECT 1 FROM sqlite_schema LIMIT 1")
    except BaseException:
        connection.close()
        raise
    return connection


@dataclass(frozen=True)
class AdmissionIdentity:
    """The immutable composite identity of one admitted attempt.

    Every field except ``run_id`` is nullable and means "not applicable or not
    known" — never a placeholder. ``node_id`` is the epic DAG node key (the
    item id the reservation was taken on), kept alongside ``epic_ref`` so a run
    can be joined to its position in an epic without re-deriving it from a
    branch name.
    """

    run_id: str
    attempt_number: int = 1
    source_tracker: str | None = None
    item_id: str | None = None
    delivery_repo: str | None = None
    epic_ref: str | None = None
    node_id: str | None = None

    @property
    def is_synthetic(self) -> bool:
        """True when this run was admitted for no tracker work item at all."""
        return self.item_id is None


def _clean(value: str | None) -> str | None:
    """Empty/whitespace-only strings are "not known", not a value."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def normalize_admission_identity(identity: AdmissionIdentity) -> AdmissionIdentity:
    """Return *identity* with blanks nulled, or raise if it is dishonest.

    Rejects, in order: an empty ``run_id``, an attempt number below 1, a
    ``source_tracker`` that is not a known work tracker (this is where an
    inference-provider or runner name gets caught), an ``item_id`` with no
    tracker to interpret it, and an ``item_id`` that is not canonical for its
    tracker.
    """
    run_id = _clean(identity.run_id)
    if run_id is None:
        raise AdmissionIdentityError("admission identity requires a run_id")
    if identity.attempt_number < 1:
        raise AdmissionIdentityError(
            f"attempt_number must be >= 1, got {identity.attempt_number}"
        )
    tracker = _clean(identity.source_tracker)
    if tracker is not None and tracker not in KNOWN_TRACKERS:
        raise AdmissionIdentityError(
            f"source_tracker {tracker!r} is not a work tracker "
            f"(expected one of {sorted(KNOWN_TRACKERS)}); the inference "
            "provider belongs in RunRecord.provider, not here"
        )
    item_id = _clean(identity.item_id)
    if item_id is not None:
        if tracker is None:
            raise AdmissionIdentityError(
                f"item_id {item_id!r} has no source_tracker to interpret it"
            )
        try:
            validate_item_id(item_id, tracker)
        except InvalidItemIdError as exc:
            raise AdmissionIdentityError(str(exc)) from exc
    return replace(
        identity,
        run_id=run_id,
        source_tracker=tracker,
        item_id=item_id,
        delivery_repo=_clean(identity.delivery_repo),
        epic_ref=_clean(identity.epic_ref),
        node_id=_clean(identity.node_id),
    )


def _row_to_identity(row: sqlite3.Row) -> AdmissionIdentity:
    return AdmissionIdentity(
        run_id=row["run_id"],
        attempt_number=row["attempt_number"],
        source_tracker=row["source_tracker"],
        item_id=row["item_id"],
        delivery_repo=row["delivery_repo"],
        epic_ref=row["epic_ref"],
        node_id=row["node_id"],
    )


AdmissionEnricher = Callable[[sqlite3.Cursor], None]
"""Bounded hook that joins the admission transaction to add attempt facts.

It receives a cursor on the open ``BEGIN IMMEDIATE`` transaction *after* the
run row and its identity are inserted and before COMMIT. Raising rolls back the
run and its identity together — there is deliberately no way to keep half.
"""


class AdmissionIdentityStoreMixin:
    """The *work* slice of ``SqliteRunStore``: admission identity + read snapshot.

    Mixed into ``SqliteRunStore``; relies on the composition root for the
    connection, the execute wrapper, and the shared admission transaction
    (``_admit_run_txn`` — the same cap-count-then-insert body ``try_admit_run``
    uses), declared below.

    Besides the write primitive it owns the one *read* primitive the customer
    work projection needs: :meth:`read_snapshot`. Both live here because both
    answer "what work is this run", from the two ends — the write that records
    it and the untorn read that composes it.
    """

    # Thread-LOCAL snapshot marker, not a shared owner id: a numeric
    # thread-id comparison can alias when the OS reuses an id after a thread
    # dies inside an unbalanced snapshot, letting the impostor reenter a stale
    # transaction. A thread-local dies with its thread, so it cannot lie.
    _read_snapshot_local: threading.local
    """Thread owning this connection's open read snapshot, if any.

    A class-level default so the mixin needs no ``__init__`` of its own; each
    instance shadows it while a snapshot is open.
    """

    if TYPE_CHECKING:
        # Provided by the composed SqliteRunStore — declared here so the mixin
        # body type-checks in isolation (mypy strict).
        _conn: sqlite3.Connection
        _db_path: str
        _txn_lock: threading.Lock
        _write_lock: threading.RLock

        _read_only: bool

        def _open_snapshot_connection(self) -> sqlite3.Connection: ...

        def _execute(
            self, sql: str, params: sqlite3._Parameters | None = None
        ) -> sqlite3.Cursor: ...

        def _chained_insert_in_transaction(
            self, values: tuple[Any, ...], *, dedup_key: str | None = None
        ) -> int | None: ...

        def _admit_run_txn(
            self,
            operation: str,
            record: RunRecord,
            cap: int,
            *,
            exclude_issue: int | None = None,
            exclude_item: str | None = None,
            exclude_run: str | None = None,
            on_admit: Callable[[], None] | None = None,
            in_txn: Callable[[], None] | None = None,
        ) -> bool: ...

    @contextmanager
    def read_snapshot(self) -> Iterator[None]:
        """Hold ONE consistent read snapshot for the body of the block.

        ``WorkView`` composition reads several tables (runs, admission
        identity, epic reservations, tick journal). Read individually, a
        concurrent reconciliation — a publication finalize, a driver tick —
        can land between two of those reads, and the composed view then shows
        half of one world and half of the next. That torn view is what this
        primitive makes impossible: under WAL an open read transaction pins
        the database image it first saw, so every read inside the block agrees.

        Read-only by construction: the block reads through its own read-only
        connection and always ends in ROLLBACK, so it can never become an
        accidental writer (a write in it raises ``ReadSnapshotWriteError``),
        and a raise inside the body releases the transaction. Writers on other
        threads keep the writer connection to themselves: their commits neither
        end this snapshot nor are undone by it (#1958). Without WAL their
        commits wait for the block to end instead. Only a memory ledger, which
        cannot be reopened, snapshots on the writer connection.

        Nesting is safe and deliberate: an inner block joins the outer
        snapshot instead of opening a second one, so composers are free to call
        each other without silently re-reading a *newer* image halfway through.
        """
        local = self.__dict__.setdefault("_read_snapshot_local", threading.local())
        if getattr(local, "inside", False):
            yield
            return
        if self._db_path == ":memory:":
            with self._writer_connection_snapshot(local):
                yield
            return
        # The snapshot owns a private read-only connection in every journal
        # mode. The writer connection is shared across threads
        # (``check_same_thread=False`` for the TUI and serve), so a
        # BEGIN/ROLLBACK there would swallow another thread's uncommitted save,
        # and that thread's COMMIT would end this snapshot early (#1958). This
        # thread's store reads go through it.
        connection = self._open_pinned_snapshot()
        try:
            local.connection = connection
            local.inside = True
            try:
                yield
            except sqlite3.OperationalError as exc:
                if self._read_only or not _is_snapshot_write(exc):
                    raise
                raise ReadSnapshotWriteError() from exc
            finally:
                local.inside = False
                local.connection = None
        finally:
            try:
                connection.rollback()
            finally:
                connection.close()

    def _open_pinned_snapshot(self) -> sqlite3.Connection:
        """Open the snapshot connection with its read transaction pinned.

        Without WAL a writer process that died mid-commit leaves a hot rollback
        journal, which a read-only connection cannot roll back
        (``SQLITE_READONLY_ROLLBACK``). One read on the writer connection rolls
        it back, then the snapshot opens again.
        """
        try:
            return _pin_snapshot(self._open_snapshot_connection())
        except sqlite3.OperationalError as exc:
            code = getattr(exc, "sqlite_errorcode", 0)
            if self._read_only or code != sqlite3.SQLITE_READONLY_ROLLBACK:
                raise
        with self._write_lock:
            self._conn.execute("SELECT 1 FROM sqlite_schema LIMIT 1")
        return _pin_snapshot(self._open_snapshot_connection())

    @contextmanager
    def _writer_connection_snapshot(self, local: threading.local) -> Iterator[None]:
        """Memory databases cannot reopen their data: snapshot on the writer.

        The lock is the same one every BEGIN IMMEDIATE section takes, because
        SQLite cannot nest two transactions on one connection.
        """
        with self._txn_lock:
            local.inside = True
            try:
                self._conn.execute("BEGIN DEFERRED")
                self._conn.execute("SELECT 1 FROM sqlite_schema LIMIT 1")
                yield
            finally:
                try:
                    self._conn.rollback()
                finally:
                    local.inside = False

    def admit_run_with_identity(
        self,
        record: RunRecord,
        identity: AdmissionIdentity,
        *,
        cap: int = 0,
        exclude_issue: int | None = None,
        exclude_item: str | None = None,
        exclude_run: str | None = None,
        on_admit: Callable[[], None] | None = None,
        enrich: AdmissionEnricher | None = None,
        reservation: EpicReservation | None = None,
        reservation_pinned: bool = False,
    ) -> bool:
        """Admit *record* and persist its immutable *identity* in one transaction.

        The single primitive both admission writers use. ``cap`` > 0 makes the
        pending row an atomic worker-slot claim exactly as
        :meth:`RunStore.try_admit_run` does (count and insert under one
        ``BEGIN IMMEDIATE``, returning ``False`` without writing when the fleet
        is full); ``cap`` <= 0 always inserts. Either way the identity row is
        written in the same transaction as the run row, so a run can never
        exist without its identity.

        Order: run row, budget gate, identity, dispatcher identity,
        optional reservation binding, then ``enrich``.
        Any failure rolls back the entire admission.
        A supplied reservation must still own its captured generation.
        Its audit binding survives later terminal updates to the child row.

        Recording an identity that already exists byte-for-byte is an
        idempotent no-op; recording a *different* one for the same
        ``(run_id, attempt_number)`` raises
        :class:`AdmissionIdentityConflictError` — the identity is immutable.
        """
        checked = normalize_admission_identity(identity)

        def _bind_identity() -> None:
            self._write_identity(checked)
            self._record_dispatch_owner(record)
            if reservation is not None:
                self._bind_dispatch_reservation(
                    record, reservation, pinned=reservation_pinned
                )
            if enrich is not None:
                enrich(self._conn.cursor())

        return self._admit_run_txn(
            "admit_run_with_identity",
            record,
            cap,
            exclude_issue=exclude_issue,
            exclude_item=exclude_item,
            exclude_run=exclude_run,
            on_admit=on_admit,
            in_txn=_bind_identity,
        )

    def _record_dispatch_owner(self, record: RunRecord) -> None:
        """Attest the stored dispatch identity in the admission transaction."""
        if record.owner_pid is None:
            return
        self._chained_insert_in_transaction(
            (
                datetime.now(UTC).isoformat(),
                "dispatch_owner_recorded",
                record.run_id,
                record.issue_num,
                record.repo,
                "policy",
                "dispatch-admission",
                None,
                record.policy_hash,
                json.dumps(
                    {
                        "owner_pid": record.owner_pid,
                        "owner_start_time": record.owner_start_time,
                        "owner_start_token": record.owner_start_token,
                        "owner_ppid": record.owner_ppid,
                        "owner_command": record.owner_command,
                        "pgid": record.pgid,
                    }
                ),
                record.item_id,
            )
        )

    def _bind_dispatch_reservation(
        self, record: RunRecord, reservation: EpicReservation, *, pinned: bool
    ) -> None:
        """Bind the child in the admission transaction; reject a replaced owner."""
        current = self._conn.execute(
            "SELECT run_id FROM epic_reservations WHERE epic_ref = ? AND item_id = ?",
            (reservation.epic_ref, reservation.item_id),
        ).fetchone()
        if (
            current is None
            or current["run_id"] != reservation.run_id
            or (record.item_id or str(record.issue_num)) != reservation.item_id
        ):
            raise DispatchReservationReplaced
        self._chained_insert_in_transaction(
            (
                datetime.now(UTC).isoformat(),
                DISPATCH_RESERVATION_BOUND,
                record.run_id,
                record.issue_num,
                record.repo,
                "policy",
                "dispatch-admission",
                None,
                record.policy_hash,
                json.dumps(
                    {
                        "reservation_run_id": reservation.run_id,
                        "pinned": pinned,
                        "epic_ref": reservation.epic_ref,
                        "item_id": reservation.item_id,
                    }
                ),
                reservation.item_id,
            )
        )

    def _write_identity(self, identity: AdmissionIdentity) -> None:
        """Insert *identity*, tolerating an identical re-write only.

        Must run inside the caller's open transaction: a conflicting row is
        detected by reading back within it, so the raise rolls the run row back
        too rather than leaving a run bound to someone else's work.
        """
        existing = self._conn.execute(
            f"{_ADMISSION_SELECT} WHERE run_id = ? AND attempt_number = ?",
            (identity.run_id, identity.attempt_number),
        ).fetchone()
        if existing is not None:
            if _row_to_identity(existing) != identity:
                raise AdmissionIdentityConflictError(
                    f"run {identity.run_id} attempt {identity.attempt_number} "
                    "already recorded a different admission identity"
                )
            return
        self._execute(
            # nosec B608 — column list is a module constant; values are bound
            "INSERT INTO run_admission_identity"  # nosec B608
            f" ({', '.join(_ADMISSION_COLS)})"
            f" VALUES ({', '.join('?' * len(_ADMISSION_COLS))})",
            (
                identity.run_id,
                identity.attempt_number,
                identity.source_tracker,
                identity.item_id,
                identity.delivery_repo,
                identity.epic_ref,
                identity.node_id,
                kew.run_store._dt_to_iso(datetime.now(UTC)),
            ),
        )

    def build_admission_identity(
        self,
        run_id: str,
        *,
        tracker: str,
        item_id: str | None,
        repo: str | None,
        attempt_number: int = 1,
    ) -> AdmissionIdentity:
        """Compose one admission identity, joining the epic node from the store.

        ``epic_ref``/``node_id`` are read from ``epic_reservations`` rather than
        threaded down from the driver: the reservation is the durable record
        that this item is a node of that epic, and it is written before the
        child dispatch starts. A run with no reservation is simply not epic
        work, and both fields stay NULL — the honest answer, not a guess from
        the branch name.

        A run with no work item (``item_id is None`` — an ``epic plan`` run, a
        review pass) records neither an item nor a tracker: naming a tracker
        for no item would imply a work item that does not exist.
        """
        if item_id is None:
            # Synthetic, but the delivery repo is still honest fact — the v42
            # legacy backfill keeps it for the same reason.
            return AdmissionIdentity(
                run_id=run_id, attempt_number=attempt_number, delivery_repo=repo
            )
        row = self._conn.execute(
            "SELECT epic_ref, item_id FROM epic_reservations WHERE item_id = ?",
            (item_id,),
        ).fetchone()
        return AdmissionIdentity(
            run_id=run_id,
            attempt_number=attempt_number,
            source_tracker=tracker,
            item_id=item_id,
            delivery_repo=repo,
            epic_ref=None if row is None else row["epic_ref"],
            node_id=None if row is None else row["item_id"],
        )

    def get_admission_identity(self, run_id: str) -> AdmissionIdentity | None:
        """The latest recorded admission identity for *run_id*, or ``None``."""
        row = self._conn.execute(
            f"{_ADMISSION_SELECT} WHERE run_id = ?"
            " ORDER BY attempt_number DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        return None if row is None else _row_to_identity(row)

    def list_admission_identities(self, run_id: str) -> list[AdmissionIdentity]:
        """Every recorded attempt identity for *run_id*, oldest attempt first."""
        rows = self._conn.execute(
            f"{_ADMISSION_SELECT} WHERE run_id = ? ORDER BY attempt_number",
            (run_id,),
        ).fetchall()
        return [_row_to_identity(row) for row in rows]

    def find_admissions_for_item(
        self, source_tracker: str, item_id: str
    ) -> list[AdmissionIdentity]:
        """Every admission recorded for one tracker's opaque item id.

        The join the numeric ``issue_num`` shim could never express: a Linear
        item and a GitHub issue that happen to stringify alike stay distinct
        because the tracker is part of the key.
        """
        rows = self._conn.execute(
            f"{_ADMISSION_SELECT} WHERE source_tracker = ? AND item_id = ?"
            " ORDER BY run_id, attempt_number",
            (source_tracker, item_id),
        ).fetchall()
        return [_row_to_identity(row) for row in rows]
