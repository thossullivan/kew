"""Round 23 contract regressions against ad364859."""

import signal
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from test_w42_round17 import _config, _preflight_environment, _prepare_direct_command
from test_w42_round20 import RemoteLabels, _remote_scene
from test_w42_round21 import _receipt, _seed
from test_w42_round22 import HostLaunchScene

import kew.ci_fix as ci_fix
import kew.dispatch_core as core
import kew.resolve as resolve
import kew.review as review
from kew.command_outcome import CommandOutcome
from kew.dispatch_admission import PendingAdmission
from kew.graph.driver.tick import _reconcile_reservations
from kew.run_store import SqliteRunStore
from kew.tracking import DispatchResult


class PausedHostScene(HostLaunchScene):
    """Keep A inside the ownership handoff while B attempts the same PR."""

    def __init__(self, stack, tmp_path, monkeypatch, store, db):
        super().__init__(stack, tmp_path, monkeypatch, store)
        self.db = db
        self.observations = []
        self.competing = []
        self.config.parallel.max_workers = 3
        self.config.triggers.ci_failure_max_retries = 1
        self.config.publication.publish = "checkout"
        self.config.retry.max_attempts = 1
        self.gate = tmp_path / "settle-wrapper"
        self.argv = [
            sys.executable,
            "-c",
            "import pathlib, time; "
            f"p = pathlib.Path({str(self.gate)!r}); "
            "exec('while not p.exists(): time.sleep(0.01)'); "
            "raise SystemExit(127)",
        ]
        self.duration = "10s"

    def dispatch_b(self):
        with SqliteRunStore(str(self.db)) as store:
            return ci_fix.dispatch_ci_fix(
                run_id="actions-b", config=self.config, store=store
            )

    def own(self, admission, handle, result):
        super().own(admission, handle, result)
        if self.observations:
            return
        self.observations.append(_receipt(self.store, admission.run_id))
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                self.competing.append(pool.submit(self.dispatch_b).result(timeout=10))
            self.rows_during_pause = len(self.store.list_all())
            self.consumed_during_pause = self.store.count_launched_ci_fix_attempts(
                "o/r", 87
            )
        finally:
            self.gate.touch()


@pytest.mark.serial
def test_round23_pending_host_does_not_exhaust_concurrent_repair(tmp_path, monkeypatch):
    """Base stores started during the pause and B labels the PR exhausted."""
    db = tmp_path / "repairs.db"
    with SqliteRunStore(str(db)) as store, ExitStack() as stack:
        scene = PausedHostScene(stack, tmp_path, monkeypatch, store, db)
        remote = RemoteLabels()
        _remote_scene(stack, remote)
        first = scene.invoke()
        assert first.exit_code == 2, first.output
        assert scene.observations[0]["state"] == "pending", (
            "Host Popen must leave the receipt pending until workload settlement."
        )
        assert scene.observations[0]["launched_at"] is None
        assert scene.consumed_during_pause == 0
        assert scene.competing == [ci_fix.CI_FIX_CANNOT_RUN]
        assert scene.rows_during_pause == 1, "B cannot allocate another slot or row."
        assert "agent-needs-help" not in remote.labels
        assert _receipt(store, scene.results[0].run_id)["state"] == "refused"
        scene.stub(1, event=True)
        assert scene.dispatch_b() == 1
        assert len(store.list_all()) == 2
        assert _receipt(store, scene.results[-1].run_id)["ordinal"] == 1
        assert _receipt(store, scene.results[-1].run_id)["state"] == "started"
        assert store.count_launched_ci_fix_attempts("o/r", 87) == 1
        assert "agent-needs-help" not in remote.labels


@pytest.mark.serial
def test_round23_signal_only_receipt_is_unknown(tmp_path, monkeypatch):
    """Base returns unknown execution but leaves its receipt started."""
    with SqliteRunStore(":memory:") as store, ExitStack() as stack:
        scene = HostLaunchScene(stack, tmp_path, monkeypatch, store)
        scene.config.retry.max_attempts = 1
        scene.argv = [
            sys.executable,
            "-c",
            f"import os; os.kill(os.getpid(), {signal.SIGTERM})",
        ]
        assert scene.invoke().exit_code == 1
        result = scene.results[-1]
        assert result.launched is None
        assert _receipt(store, result.run_id)["state"] == "unknown", (
            "A signal without workload evidence must persist UNKNOWN."
        )
        assert store.count_launched_ci_fix_attempts("o/r", 87) == 1


class FailingFinalization:
    def __init__(self, store, failures):
        self.original = store.finalize_cancelled_run
        self.failures = failures
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        if self.calls <= self.failures:
            raise sqlite3.OperationalError("injected cleanup write failure")
        return self.original(*args, **kwargs)


def _reserve_cancelled(store):
    _seed(store, "cancelled", started=False)
    assert store.reserve_epic_node_item("round23", "42", "cancelled")


def test_round23_transient_finalization_retries_before_interrupt_escapes():
    """Base makes one call and leaves both the run and reservation pending."""
    with SqliteRunStore(":memory:") as store:
        _reserve_cancelled(store)
        finalizer = FailingFinalization(store, 1)
        interruption = KeyboardInterrupt("operator")
        with (
            patch.object(store, "finalize_cancelled_run", side_effect=finalizer),
            patch.object(
                store, "defer_cancelled_run", side_effect=OSError("journal unavailable")
            ),
            pytest.raises(KeyboardInterrupt) as caught,
            PendingAdmission(store, "cancelled"),
        ):
            raise interruption
        assert not store.list_pending(), "Retry the transient finalization failure."
        assert caught.value is interruption
        assert finalizer.calls == 2
        assert not store.list_epic_reservations("round23")
        assert store.live_run_count() == 0
        assert _receipt(store, "cancelled")["state"] == "refused"


def test_round23_persistent_finalization_journal_reaped_next_tick(
    tmp_path, monkeypatch, caplog
):
    """Base leaves no durable recovery marker after its single failed call."""
    config = _config(tmp_path, monkeypatch)
    db = tmp_path / "cancelled.db"
    with SqliteRunStore(str(db)) as store:
        _reserve_cancelled(store)
        finalizer = FailingFinalization(store, 99)
        with (
            patch.object(store, "finalize_cancelled_run", side_effect=finalizer),
            patch("time.sleep") as sleep,
            pytest.raises(KeyboardInterrupt),
            PendingAdmission(store, "cancelled"),
        ):
            raise KeyboardInterrupt("operator")
        markers = list(tmp_path.glob("cancelled.db.cancelled/*.json"))
        assert len(markers) == 1, "Persist recovery outside the failed database write."
        assert finalizer.calls == 1
        sleep.assert_not_called()
        assert "reservation run_id=cancelled" in caplog.text
    # Reopen the database to prove the next tick uses durable recovery evidence.
    with SqliteRunStore(str(db)) as store:
        assert (
            _reconcile_reservations(
                "round23",
                store,
                config,
                now=datetime.now(UTC),
                liveness=Mock(return_value=None),
            )
            == set()
        )
        assert not store.list_pending()
        assert store.get("cancelled").status == "refused"
        assert store.live_run_count() == 0
        assert not markers[0].exists()
        assert _receipt(store, "cancelled")["state"] == "refused"


def test_round23_resolve_cleanup_preserves_executed_outcome(
    tmp_path, monkeypatch, caplog
):
    """Base changes completed executed 1 into not_run 1 (public exit 2)."""
    config = _config(tmp_path, monkeypatch)
    with ExitStack() as stack:
        _prepare_direct_command(stack, tmp_path, "resolve", config)
        _preflight_environment(stack)
        stack.enter_context(
            patch.object(
                resolve, "_run_resolve", return_value=CommandOutcome("executed", 1)
            )
        )
        cleanup = stack.enter_context(
            patch.object(
                resolve, "release_checkout_claims", side_effect=OSError("lease failure")
            )
        )
        outcome = resolve.dispatch_resolve(pr_number=87, config=config)
    assert outcome == CommandOutcome("executed", 1), "Cleanup cannot erase execution."
    cleanup.assert_called_once()
    assert "resolve checkout cleanup failed" in caplog.text


def test_round23_ci_fix_cleanup_preserves_executed_outcome(
    tmp_path, monkeypatch, caplog
):
    """Base raises OSError instead of returning the completed CI-fix exit 1."""
    config = _config(tmp_path, monkeypatch)
    with (
        patch.object(ci_fix, "_dispatch_ci_fix", return_value=1),
        patch.object(
            ci_fix, "release_checkout_claims", side_effect=OSError("lease failure")
        ) as cleanup,
    ):
        assert ci_fix.dispatch_ci_fix(run_id="actions", config=config) == 1
    cleanup.assert_called_once()
    assert "ci-fix checkout cleanup failed" in caplog.text


def test_round23_review_cleanup_preserves_executed_outcome(
    tmp_path, monkeypatch, caplog
):
    """Base lets review checkout release replace completed execution."""
    config = _config(tmp_path, monkeypatch)
    with ExitStack() as stack:
        _prepare_direct_command(stack, tmp_path, "review", config)
        _preflight_environment(stack)
        stack.enter_context(
            patch.object(
                review,
                "run_tracked_agent",
                return_value=DispatchResult(1, 42, None, launched=True),
            )
        )
        cleanup = stack.enter_context(
            patch(
                "kew.review_checkout.ReviewCheckout.release",
                side_effect=OSError("lease failure"),
            )
        )
        outcome = review.dispatch_review(pr_number=87, config=config)
    # The executed run's id rides along since #2505; cleanup must not erase it.
    assert (outcome.kind, outcome.exit_code) == ("executed", 1)
    assert outcome.run_id
    cleanup.assert_called_once()
    assert "review PR #87 checkout cleanup failed" in caplog.text


def test_round23_dispatch_cleanup_preserves_completed_outcome(caplog):
    """Base raises its release error instead of returning the dispatch outcome."""
    expected = core.DispatchOutcome(1, [], launched=True)
    context = SimpleNamespace(
        item_id="42",
        launch_state=Mock(),
        admitted_run_id=None,
        claim_launch_started=False,
        container_cleanup_pending=False,
    )
    with (
        patch.object(core, "_prepare_dispatch_context", return_value=context),
        patch.object(core, "_run_dispatch_pipeline", return_value=expected),
        patch.object(
            core, "release_checkout_claims", side_effect=OSError("lease failure")
        ),
    ):
        outcome = core.execute_dispatch(issue=Mock(), config=Mock())
    assert outcome is expected
    assert "dispatch checkout cleanup failed" in caplog.text
