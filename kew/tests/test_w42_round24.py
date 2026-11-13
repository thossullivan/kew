"""Round 24 cancellation regressions against bcf6b016."""

import logging
import signal
import sqlite3
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock, patch

import pytest
from test_w42_round17 import _config, _record
from test_w42_round21 import _receipt

from kew.dispatch_admission import PendingAdmission, execution_cancellation
from kew.graph.driver.tick import _reconcile_reservations
from kew.run_store import SqliteRunStore


class FailingWrite:
    """Exercise real persistence after a controlled series of failed writes."""

    def __init__(self, original, name, events, failures, error, *, interrupt=False):
        self.original = original
        self.name = name
        self.events = events
        self.failures = failures
        self.error = error
        self.interrupt = interrupt
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        self.events.append(self.name)
        if self.calls <= self.failures:
            if self.interrupt:
                signal.raise_signal(signal.SIGINT)
            raise self.error
        return self.original(*args, **kwargs)


def _reserve(store, *, kind="ci_fix"):
    assert store.reserve_epic_node_item("round24", "42", "round24-cancelled")
    record = _record("round24-cancelled", kind=kind, item="42")
    record.repo = "o/r"
    record.attempt_pgid = 424242
    store.save(record)
    store.register_ci_fix_attempt(record.run_id, 87)


def _interrupt(admission, interruption):
    with pytest.raises(BaseException) as caught, admission:
        raise interruption
    return caught.value


def _reap(store, config, *, dead=False):
    with patch("kew.process_groups.group_alive", return_value=False if dead else None):
        assert not _reconcile_reservations(
            "round24",
            store,
            config,
            now=datetime.now(UTC) + timedelta(hours=1),
            liveness=Mock(return_value=False if dead else None),
        )
    assert not store.list_pending()
    assert not store.list_epic_reservations("round24")
    assert store.live_run_count() == 0


@pytest.mark.serial
@pytest.mark.parametrize("repeated_sigint", [False, True])
def test_round24_double_failure_preserves_interruption_and_reaps(
    tmp_path, monkeypatch, caplog, repeated_sigint
):
    """Base replaces KeyboardInterrupt with the first journal OSError."""
    config = _config(tmp_path, monkeypatch)
    db = tmp_path / "cancelled.db"
    events = []
    interruption = KeyboardInterrupt("operator")
    with SqliteRunStore(str(db)) as store:
        _reserve(store, kind="dispatch")
        finalizer = FailingWrite(
            store.finalize_cancelled_run,
            "finalize",
            events,
            99,
            sqlite3.OperationalError("disk full"),
        )
        journal = FailingWrite(
            store.defer_cancelled_run,
            "journal",
            events,
            99,
            OSError("journal disk full"),
            interrupt=repeated_sigint,
        )
        with (
            patch.object(store, "finalize_cancelled_run", side_effect=finalizer),
            patch.object(store, "defer_cancelled_run", side_effect=journal),
            patch("kew.dispatch_admission.time.sleep") as sleep,
        ):
            caught = _interrupt(
                PendingAdmission(store, "round24-cancelled"), interruption
            )
        assert caught is interruption, "Keep the original interruption primary."
        assert caught.__cause__ is journal.error
        assert events == ["finalize", "journal"] * 6
        assert [call.args[0] for call in sleep.call_args_list] == [
            0.05,
            0.1,
            0.2,
            0.4,
            0.8,
        ]
        errors = "\n".join(
            r.message for r in caplog.records if r.levelno == logging.ERROR
        )
        assert "run round24-cancelled" in errors
        assert "reservation run_id=round24-cancelled" in errors
        assert "OperationalError('disk full')" in errors
        assert "OSError('journal disk full')" in errors
        assert store.list_pending()
        assert store.list_epic_reservations("round24")
        assert not list(tmp_path.glob("cancelled.db.cancelled/*.json"))
    with SqliteRunStore(str(db)) as store:
        _reap(store, config, dead=True)
        assert (
            store.get("round24-cancelled").error_category == "orphaned_pending_reaped"
        )


def test_round24_transient_journal_failure_retries_and_reaps(tmp_path, monkeypatch):
    """Base propagates OSError and never commits a recovery marker."""
    config = _config(tmp_path, monkeypatch)
    db = tmp_path / "cancelled.db"
    events = []
    interruption = KeyboardInterrupt("operator")
    with SqliteRunStore(str(db)) as store:
        _reserve(store)
        finalizer = FailingWrite(
            store.finalize_cancelled_run,
            "finalize",
            events,
            99,
            sqlite3.OperationalError("disk full"),
        )
        journal = FailingWrite(
            store.defer_cancelled_run,
            "journal",
            events,
            1,
            OSError("journal disk full"),
        )
        with (
            patch.object(store, "finalize_cancelled_run", side_effect=finalizer),
            patch.object(store, "defer_cancelled_run", side_effect=journal),
            patch("kew.dispatch_admission.time.sleep") as sleep,
        ):
            caught = _interrupt(
                PendingAdmission(store, "round24-cancelled"), interruption
            )
        assert caught is interruption, "Journal retries must preserve cancellation."
        assert events == ["finalize", "journal"] * 2
        sleep.assert_called_once_with(0.05)
        assert len(list(tmp_path.glob("cancelled.db.cancelled/*.json"))) == 1
    with SqliteRunStore(str(db)) as store:
        _reap(store, config)
        assert _receipt(store, "round24-cancelled")["state"] == "refused"
        assert store.count_launched_ci_fix_attempts("o/r", 87) == 0
        assert not list(tmp_path.glob("cancelled.db.cancelled/*.json"))


def test_round24_finalization_recovers_after_journal_failure():
    """Base waits for finalization without attempting the alternate durable path."""
    events = []
    with SqliteRunStore(":memory:") as store:
        _reserve(store)
        finalizer = FailingWrite(
            store.finalize_cancelled_run,
            "finalize",
            events,
            1,
            sqlite3.OperationalError("disk full"),
        )
        journal = FailingWrite(
            store.defer_cancelled_run,
            "journal",
            events,
            99,
            OSError("journal disk full"),
        )
        with (
            patch.object(store, "finalize_cancelled_run", side_effect=finalizer),
            patch.object(store, "defer_cancelled_run", side_effect=journal),
            patch("kew.dispatch_admission.time.sleep"),
        ):
            interruption = KeyboardInterrupt("operator")
            caught = _interrupt(
                PendingAdmission(store, "round24-cancelled"), interruption
            )
        assert events == ["finalize", "journal", "finalize"], (
            "Alternate finalization and journal attempts before backoff."
        )
        assert caught is interruption
        assert not store.list_pending()
        assert not store.list_epic_reservations("round24")
        assert store.live_run_count() == 0


@pytest.mark.parametrize("failures", [1, 99])
def test_round24_settled_refusal_survives_receipt_write_failure(failures):
    """Base writes unknown and consumes one attempt after settled refusal."""
    with SqliteRunStore(":memory:") as store:
        _reserve(store)
        admission = PendingAdmission(store, "round24-cancelled")
        interruption = KeyboardInterrupt("operator")
        errors = [interruption] + [
            sqlite3.OperationalError("receipt disk full")
        ] * failures
        with (
            patch.object(store, "refuse_launch", side_effect=errors) as refuse,
            pytest.raises(KeyboardInterrupt) as caught,
            admission,
            execution_cancellation(admission),
        ):
            admission.take_ownership()
            admission.record_startup(refused=True, settled=True)
        assert caught.value is interruption
        assert refuse.call_count == 2
        receipt = _receipt(store, "round24-cancelled")
        assert receipt["state"] == "refused", (
            "Settled refusal must release the repair slot."
        )
        assert receipt["ordinal"] is None
        assert receipt["launched_at"] is None
        assert store.count_launched_ci_fix_attempts("o/r", 87) == 0
        assert not store.list_pending()
        assert not store.list_epic_reservations("round24")
        assert store.live_run_count() == 0
        replacement = _record("replacement", kind="ci_fix", item="43")
        replacement.repo = "o/r"
        store.save(replacement)
        store.register_ci_fix_attempt("replacement", 87, max_attempts=1)
        assert _receipt(store, "replacement")["ordinal"] == 1


@pytest.mark.parametrize("startup_state", ["refused", "started"])
def test_round24_journal_retains_settled_evidence(tmp_path, monkeypatch, startup_state):
    """Base journals ownership alone and replays settled evidence as unknown."""
    config = _config(tmp_path, monkeypatch)
    db = tmp_path / "cancelled.db"
    with SqliteRunStore(str(db)) as store:
        _reserve(store)
        admission = PendingAdmission(store, "round24-cancelled")
        interruption = KeyboardInterrupt("operator")
        with (
            patch.object(
                store,
                "refuse_launch" if startup_state == "refused" else "record_launch",
                side_effect=sqlite3.OperationalError("receipt disk full"),
            ),
            patch.object(
                store,
                "finalize_cancelled_run",
                side_effect=sqlite3.OperationalError("disk full"),
            ),
            patch("kew.dispatch_admission.time.sleep"),
            pytest.raises(KeyboardInterrupt) as caught,
            admission,
            execution_cancellation(admission),
        ):
            admission.take_ownership()
            admission.record_startup(refused=startup_state == "refused", settled=True)
            raise interruption
        assert caught.value is interruption
    with SqliteRunStore(str(db)) as store:
        _reap(store, config)
        assert _receipt(store, "round24-cancelled")["state"] == startup_state, (
            "Journal replay must retain settled evidence independently of ownership."
        )
        assert store.count_launched_ci_fix_attempts("o/r", 87) == (
            startup_state == "started"
        )
