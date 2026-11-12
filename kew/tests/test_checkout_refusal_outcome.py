"""A checkout-identity refusal reports could-not-run and keeps an admitted row.

The refusal can arrive after admission: the run's pending row already names
its log file and start time. The refusal must finish that row, never replace it
with a fresh pre-admission refusal row, and the outcome must say no worker
launched (#2013).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from kew import dispatch_core
from kew.config import DispatchConfig
from kew.dispatch_sandbox import _finalize_sandbox_run
from kew.run_store import RunRecord, SqliteRunStore

EPIC = "epic:checkout-refusal"


@pytest.fixture
def store(tmp_path: Path):
    with SqliteRunStore(str(tmp_path / "runs.db")) as opened:
        yield opened


def _context(store: SqliteRunStore, *, run_id: str) -> dispatch_core._DispatchContext:
    return dispatch_core._DispatchContext(
        config=DispatchConfig(),
        store=store,
        notifier=None,
        force_budget=False,
        approved=True,
        interactive=False,
        model_override=None,
        merge_context=None,
        epic_ref=EPIC,
        provider="github",
        issue_num=42,
        issue_title="checkout refusal",
        issue_body="",
        complexity="simple",
        item_id="42",
        branch="agent/issue-42",
        start_monotonic=0.0,
        refuse_rollback=Mock(),
        terminal_failure=None,
        run_kind="dispatch",
        run_id=run_id,
    )


def _refuse(ctx: dispatch_core._DispatchContext) -> dispatch_core.DispatchOutcome:
    refusal = dispatch_core._CheckoutIdentityRefused(
        "unknown", "checkout identity changed during handoff"
    )
    launch = dispatch_core._launch_refused_checkout(ctx, None, False, refusal)
    assert launch.early is not None
    return launch.early


def _admit(store: SqliteRunStore, run_id: str, log_file: Path) -> datetime:
    started = datetime.now(UTC) - timedelta(minutes=3)
    assert store.try_admit_run(
        RunRecord(
            run_id=run_id,
            issue_num=42,
            item_id="42",
            branch="agent/issue-42",
            status="pending",
            exit_code=-1,
            started_at=started,
            log_file=log_file,
            runner_name="claude",
            model="opus",
            container_name="kew-42",
        ),
        cap=0,
    )
    return started


def test_checkout_refusal_reports_not_launched(store: SqliteRunStore) -> None:
    outcome = _refuse(_context(store, run_id="refused-run"))

    assert outcome.launched is False
    assert outcome.exit_code == 2
    assert outcome.category == "checkout_identity_changed"
    row = store.get("refused-run")
    assert row is not None
    assert row.status == dispatch_core._REFUSAL_STATUS


def test_checkout_refusal_after_admission_keeps_the_admitted_row(
    store: SqliteRunStore, tmp_path: Path
) -> None:
    started = datetime.now(UTC) - timedelta(minutes=3)
    log_file = tmp_path / "admitted-run.log"
    assert store.try_admit_run(
        RunRecord(
            run_id="admitted-run",
            issue_num=42,
            item_id="42",
            branch="agent/issue-42",
            status="pending",
            exit_code=-1,
            started_at=started,
            log_file=log_file,
        ),
        cap=0,
    )
    ctx = _context(store, run_id="admitted-run")
    ctx.on_admitted = Mock()
    ctx.admitted_run_id = "admitted-run"

    outcome = _refuse(ctx)

    assert outcome.launched is False
    row = store.get("admitted-run")
    assert row is not None
    assert row.log_file == log_file
    assert row.started_at == started
    assert row.pre_admission_refusal is False
    assert row.status != "pending"
    assert row.error_category == "checkout_identity_changed"
    assert row.finished_at is not None


def test_refusal_leaves_a_row_the_sandbox_already_finalized_unchanged(
    store: SqliteRunStore, tmp_path: Path
) -> None:
    """A contained GitHub epic node has no admission callback, so
    ``admitted_run_id`` stays None. The sandbox's checkout boundary has already
    finalized the admitted row as refused before it raised; the refusal handler
    must not replace that row with a pre-admission refusal row."""
    _admit(store, "sandboxed-run", tmp_path / "sandboxed-run.log")
    _finalize_sandbox_run(
        store,
        "sandboxed-run",
        exit_code=-1,
        error_category="checkout_identity_changed",
        status="refused",
    )
    finalized = store.get("sandboxed-run")
    assert finalized is not None and finalized.status == "refused"
    ctx = _context(store, run_id="sandboxed-run")
    assert ctx.on_admitted is None and ctx.admitted_run_id is None

    outcome = _refuse(ctx)

    assert outcome.launched is False
    assert store.get("sandboxed-run") == finalized


def test_refusal_without_an_admission_callback_finishes_the_pending_row(
    store: SqliteRunStore, tmp_path: Path
) -> None:
    log_file = tmp_path / "pending-run.log"
    started = _admit(store, "pending-run", log_file)
    ctx = _context(store, run_id="pending-run")
    assert ctx.on_admitted is None and ctx.admitted_run_id is None

    _refuse(ctx)

    row = store.get("pending-run")
    assert row is not None
    assert row.status == dispatch_core._REFUSAL_STATUS
    assert row.exit_code == 1
    assert row.error_category == "checkout_identity_changed"
    assert row.finished_at is not None
    assert row.log_file == log_file
    assert row.started_at == started
    assert (row.runner_name, row.model) == ("claude", "opus")
    assert row.pre_admission_refusal is False


class _TerminalWriteFirst:
    """The writer connection, with another actor's terminal write ordered first.

    Just before the first statement that writes ``runs`` on this connection,
    *finish* runs once: another handle terminalizes the row between the
    refusal handler's decision and its write.
    """

    def __init__(self, connection: Any, finish: Any) -> None:
        self._connection = connection
        self._finish = finish

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)

    def execute(self, sql: str, *params: Any) -> Any:
        statement = sql.lstrip().upper()
        if statement.startswith(("INSERT INTO RUNS", "UPDATE RUNS")) and self._finish:
            finish, self._finish = self._finish, None
            finish()
        return self._connection.execute(sql, *params)


def test_refusal_never_overwrites_a_terminal_write_that_landed_first(
    store: SqliteRunStore, tmp_path: Path
) -> None:
    """The pending-row finish is one conditional write, not a read and then a
    full-row save: a reaper that terminalized the row first keeps its outcome."""
    _admit(store, "reaped-run", tmp_path / "reaped-run.log")
    sibling = store.open_sibling()

    def _reaper_lands_first() -> None:
        pending = sibling.get("reaped-run")
        assert pending is not None and pending.status == "pending"
        sibling.save(
            replace(
                pending,
                status="failed",
                exit_code=137,
                error_category="dispatch_child_died",
                finished_at=datetime.now(UTC),
            )
        )

    try:
        store._conn = _TerminalWriteFirst(store._conn, _reaper_lands_first)  # type: ignore[assignment]
        _refuse(_context(store, run_id="reaped-run"))
    finally:
        sibling.close()

    row = store.get("reaped-run")
    assert row is not None
    assert (row.status, row.exit_code, row.error_category) == (
        "failed",
        137,
        "dispatch_child_died",
    )
    assert row.pre_admission_refusal is False


def _driver_declared_dead(store: SqliteRunStore, run_id: str) -> RunRecord:
    """The driver's verdict on a reservation whose child wrote no row in time."""
    from kew.graph.driver.tick import _record_dispatch_child_died
    from kew.run_store import EpicReservation

    reserved_at = datetime.now(UTC) - timedelta(minutes=11)
    _record_dispatch_child_died(
        store,
        DispatchConfig(),
        EpicReservation(
            issue_num=42, epic_ref=EPIC, run_id=run_id, reserved_at=reserved_at
        ),
        now=datetime.now(UTC),
    )
    verdict = store.get(run_id)
    assert verdict is not None and verdict.error_category == "dispatch_child_died"
    return verdict


def test_refusal_replaces_the_drivers_dead_child_verdict(
    store: SqliteRunStore,
) -> None:
    """A child that is alive to refuse proves the driver's dead-child verdict
    wrong: its refusal is the node's true outcome and takes the row."""
    _driver_declared_dead(store, "late-child")

    outcome = _refuse(_context(store, run_id="late-child"))

    assert outcome.launched is False
    row = store.get("late-child")
    assert row is not None
    assert (row.status, row.exit_code, row.error_category) == (
        dispatch_core._REFUSAL_STATUS,
        1,
        "checkout_identity_changed",
    )
    assert row.pre_admission_refusal is True


class _InsertAfter:
    """The writer connection, with another actor's write ordered just before
    the first ``INSERT INTO runs``: it lands after any read the refusal handler
    made and before its write."""

    def __init__(self, connection: Any, land: Any) -> None:
        self._connection = connection
        self._land = land

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)

    def execute(self, sql: str, *params: Any) -> Any:
        if sql.lstrip().upper().startswith("INSERT INTO RUNS") and self._land:
            land, self._land = self._land, None
            land()
        return self._connection.execute(sql, *params)


@pytest.mark.parametrize(
    ("status", "error_category"),
    [
        ("pending", ""),
        ("failure", "agent_error"),
        ("success", ""),
    ],
)
def test_refusal_row_never_replaces_a_row_that_landed_before_it(
    store: SqliteRunStore,
    tmp_path: Path,
    status: str,
    error_category: str,
) -> None:
    """The refusal row is one conditional write, not a read and then a save:
    any row other than the driver's dead-child verdict that appears under the
    run id first is kept whole."""
    sibling = store.open_sibling()
    landed: list[RunRecord] = []

    def _other_writer_lands_first() -> None:
        sibling.save(
            RunRecord(
                run_id="raced-run",
                issue_num=42,
                item_id="42",
                branch="agent/issue-42",
                status=status,
                exit_code=-1 if status == "pending" else 0,
                started_at=datetime.now(UTC) - timedelta(minutes=1),
                error_category=error_category,
                log_file=tmp_path / "raced-run.log",
            )
        )
        stored = sibling.get("raced-run")
        assert stored is not None
        landed.append(stored)

    try:
        store._conn = _InsertAfter(store._conn, _other_writer_lands_first)  # type: ignore[assignment]
        _refuse(_context(store, run_id="raced-run"))
    finally:
        sibling.close()

    assert landed, "the other writer never ran"
    assert store.get("raced-run") == landed[0]


@pytest.mark.parametrize("admission_callback", [True, False])
def test_refusal_keeps_an_operator_stop_request(
    store: SqliteRunStore, tmp_path: Path, admission_callback: bool
) -> None:
    """A stop request leaves the row ``pending`` with ``error_category`` killed
    until the stop is proven; a refusal landing in between must not turn it
    into a re-dispatchable refusal. The row stays exactly as the stop left it."""
    _admit(store, "stopped-run", tmp_path / "stopped-run.log")
    store.request_run_stop("stopped-run")
    stopped = store.get("stopped-run")
    assert stopped is not None
    assert (stopped.status, stopped.error_category) == ("pending", "killed")
    ctx = _context(store, run_id="stopped-run")
    if admission_callback:
        ctx.on_admitted = Mock()
        ctx.admitted_run_id = "stopped-run"

    outcome = _refuse(ctx)

    assert outcome.launched is False
    assert store.get("stopped-run") == stopped
    assert [r.run_id for r in store.list_pending()] == ["stopped-run"]
