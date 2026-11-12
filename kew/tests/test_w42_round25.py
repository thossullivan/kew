"""Round 25 regressions against 29aa4a3c and explicit passing controls."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from test_driver_dispatch import _wire_dispatch_child
from test_w42_round17 import _record
from test_w42_round21 import _receipt, _seed
from typer.testing import CliRunner

import kew.cli as cli
import kew.cli.epic as epic
import kew.dispatch_core as core
from kew.config import DispatchConfig
from kew.dispatch_admission import PendingAdmission
from kew.graph.driver.tick import _reap_orphaned_run, reconcile_epic_reservations
from kew.overlap import DispatchOutcome
from kew.run_store import SqliteRunStore
from kew.run_store_launches import CiFixAttemptsExhausted


def _journal_refusal(store):
    _seed(store, "cancelled", started=False)
    assert store.reserve_epic_node_item("round25", "42", "cancelled")
    store.defer_cancelled_run("cancelled", startup_state="refused")


def _driver_recovery(store):
    record = store.get("cancelled")
    reservation = store.list_epic_reservations("round25")[0]
    with (
        patch("kew.graph.driver.tick._owner_may_be_alive", return_value=False),
        patch("kew.graph.driver.tick._kill_orphaned_attempt", return_value=True),
    ):
        _reap_orphaned_run(
            store, DispatchConfig(), reservation, record, now=datetime.now(UTC)
        )


def _admission_recovery(store):
    with (
        patch("kew.process_groups.owner_snapshot", return_value={}),
        patch("kew.process_groups.owner_alive", return_value=False),
        patch("kew.stop_recovery.pending_owner_is_dead", return_value=True),
    ):
        assert store.try_admit_run(_record("replacement", item="43"), cap=1)


@pytest.mark.parametrize("recover", [_driver_recovery, _admission_recovery])
def test_w42_round25_marker_precedes_dead_owner_recovery(tmp_path, recover):
    """Base leaves the receipt unknown after generic recovery beats replay."""
    db = tmp_path / "recovery.db"
    with SqliteRunStore(str(db)) as store:
        _journal_refusal(store)
    with SqliteRunStore(str(db)) as store:
        recover(store)
        store.reap_cancelled_runs()
        assert _receipt(store, "cancelled")["state"] == "refused", (
            "Replay settled refusal before generic dead-owner finalization."
        )
        assert store.get("cancelled").status == "refused"
        assert store.count_launched_ci_fix_attempts("o/r", 87) == 0
        assert not store.list_epic_reservations("round25")
        assert not store._cancellation_journal.pending()


def test_w42_round25_no_change_retains_marker(tmp_path):
    """Base removes a marker even when neither run nor receipt exists.

    The marker is retained while its run row may still commit; a request that
    can never settle expires instead (governed review round 2, 2026-09-21,
    tests/test_governed_round2_startup_evidence.py).
    """
    db = tmp_path / "no-change.db"
    with SqliteRunStore(str(db)) as store:
        store.defer_cancelled_run("missing", startup_state="refused")
        store.reap_cancelled_runs()
    with SqliteRunStore(str(db)) as store:
        assert store._cancellation_journal.pending() == [("missing", "refused")], (
            "Retain a marker when replay changes no row."
        )
        _seed(store, "missing", started=False)
        store.reap_cancelled_runs()
        assert _receipt(store, "missing")["state"] == "refused"
        assert not store._cancellation_journal.pending()


def test_w42_round25_late_marker_corrects_terminal_receipt():
    """Base cannot apply settled refusal after generic finalization commits."""
    with SqliteRunStore(":memory:") as store:
        _seed(store, "cancelled", started=False)
        assert store.finalize_orphaned_pending(
            "cancelled", error_category="owner_dead", finished_at=datetime.now(UTC)
        )
        assert _receipt(store, "cancelled")["state"] == "unknown"
        store.defer_cancelled_run("cancelled", startup_state="refused")
        store.reap_cancelled_runs()
        assert _receipt(store, "cancelled")["state"] == "refused", (
            "A late settled refusal must correct an unknown terminal receipt."
        )
        assert store.count_launched_ci_fix_attempts("o/r", 87) == 0
        assert store.get("cancelled").status == "killed"
        assert not store._cancellation_journal.pending()


@pytest.mark.parametrize("identity", ["owner", "attempt"])
def test_w42_round25_failed_receipt_survives_identity_refusal(tmp_path, identity):
    """Base reports persistence success but leaves the earlier execution refused."""
    db = tmp_path / "identity.db"
    with SqliteRunStore(str(db)) as store:
        _seed(store, "retry", started=False)
        admission = PendingAdmission(store, "retry")
        run = SimpleNamespace(
            store=store,
            run_id="retry",
            issue_num=42,
            item_id="42",
            log_file=None,
            launched=True,
            # #1941 validates the session handle on every exception result;
            # a run whose runner is unresolved keeps no session id.
            runner=None,
        )
        with patch.object(store, "record_launch", side_effect=OSError("disk full")):
            admission.take_ownership()
            admission.record_startup(settled=True)
            assert admission.startup_started and not admission.receipt_persisted
            record = store.get("retry")
            record.attempt_owner_verified = True
            with (
                patch(
                    "kew.process_groups.start_token_matches",
                    side_effect=[False] if identity == "owner" else [True, False],
                ),
                patch("kew.process_groups.owner_alive", return_value=True),
                pytest.raises(core.HostIdentityUnavailable) as caught,
            ):
                core._check_host_spawn_identity(record)
            with patch.object(core, "_emit_completion_audit"):
                core._host_identity_refusal(run, caught.value, 2)
            assert store.get("retry").status == "refused"
            assert _receipt(store, "retry")["state"] == "refused"
        admission.persist_receipt()
        assert admission.receipt_persisted
        assert _receipt(store, "retry")["state"] == "started", (
            "Retained settled execution must upgrade a terminal refused receipt."
        )
    with SqliteRunStore(str(db)) as store:
        assert store.count_launched_ci_fix_attempts("o/r", 87) == 1
        assert _receipt(store, "retry")["launched_at"] is not None
        excess = _record("excess", kind="ci_fix")
        excess.repo = "o/r"
        store.save(excess)
        with pytest.raises(CiFixAttemptsExhausted):
            store.register_ci_fix_attempt("excess", 87, max_attempts=1)


@pytest.mark.parametrize(
    "raw,launched,expected",
    [(127, True, 1), (2, False, 2), (127, False, 2), (0, True, 0)],
    ids=["executed-127", "refusal-2-control", "refusal-127", "success-control"],
)
def test_w42_round25_dispatch_node_exit(tmp_path, monkeypatch, raw, launched, expected):
    """Base exposes 127; successful execution and an existing code 2 are controls."""
    outcome = DispatchOutcome(raw, launched=launched)
    with SqliteRunStore(":memory:") as store:
        _wire_dispatch_child(
            monkeypatch, tmp_path, store, dispatch=Mock(return_value=outcome)
        )
        result = CliRunner().invoke(epic.epic_app, ["_dispatch-node", "round25", "42"])
        assert result.exit_code == expected, result.output
        assert outcome.exit_code == raw, "Keep the raw outcome for the ledger."


def test_w42_round25_dispatch_node_preflight_refusal(tmp_path, monkeypatch):
    """Base exits 1 although runner preflight prevents execution."""
    dispatch = Mock()
    with SqliteRunStore(":memory:") as store:
        _wire_dispatch_child(monkeypatch, tmp_path, store, dispatch=dispatch)
        monkeypatch.setattr(
            cli, "runner_preflight_failure", Mock(return_value="runner unavailable")
        )
        result = CliRunner().invoke(epic.epic_app, ["_dispatch-node", "round25", "42"])
        assert result.exit_code == 2, result.output
        dispatch.assert_not_called()
        assert store.list_by_item("42")[0].status == "refused"


def test_w42_round25_reaper_order_control():
    """Control: reservation reconciliation already prioritizes marker replay."""
    with SqliteRunStore(":memory:") as store:
        _journal_refusal(store)
        assert not reconcile_epic_reservations(
            "round25", store, DispatchConfig(), now=datetime.now(UTC)
        )
        assert _receipt(store, "cancelled")["state"] == "refused"
        assert store.count_launched_ci_fix_attempts("o/r", 87) == 0


@pytest.mark.parametrize("journal", [False, True])
def test_w42_round25_later_refusal_preserves_started_receipt(journal):
    """Base lets either refusal writer erase previously persisted execution."""
    with SqliteRunStore(":memory:") as store:
        _seed(store, "started", started=False)
        store.record_launch("started")
        if journal:
            store.defer_cancelled_run("started", startup_state="refused")
            store.reap_cancelled_runs()
        else:
            store.refuse_launch("started")
        assert _receipt(store, "started")["state"] == "started", (
            "A later refusal must preserve an earlier started receipt."
        )
        assert store.count_launched_ci_fix_attempts("o/r", 87) == 1


def test_w42_round25_recovered_start_preserves_slot_allocation():
    """Base ignores late execution after another repair reuses its refused slot."""
    with SqliteRunStore(":memory:") as store:
        _seed(store, "late", started=False)
        admission = PendingAdmission(store, "late")
        with patch.object(store, "record_launch", side_effect=OSError("disk full")):
            admission.record_startup(settled=True)
        record = store.get("late")
        record.status = "refused"
        store.save(record)
        _seed(store, "replacement")
        admission.persist_receipt()
        assert _receipt(store, "late")["state"] == "started", (
            "Recover settled execution after another repair reuses the released slot."
        )
        assert store.count_launched_ci_fix_attempts("o/r", 87) == 2
        next_record = _record("next", kind="ci_fix")
        next_record.repo = "o/r"
        store.save(next_record)
        store.register_ci_fix_attempt("next", 87, max_attempts=3)
        ordinals = {
            _receipt(store, run_id)["ordinal"]
            for run_id in ("late", "replacement", "next")
        }
        assert ordinals == {1, 2, 3}, (
            "Recovered starts must not collide with reused slots."
        )
