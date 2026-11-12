"""Tests for the dispatch scheduler module."""

import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kew.config import DispatchConfig, ParallelConfig
from kew.labels import ClaimResult
from kew.overlap import DispatchOutcome, OverlapCoordinator
from kew.scheduler import DispatchScheduler


@pytest.fixture(autouse=True)
def _isolate_scheduler_ledger_registry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Keep managed scheduler ledger state inside each test's temp project."""
    # TMPDIR can be inside a worktree. Bound Git discovery to this fake project.
    (tmp_path / ".git").mkdir(exist_ok=True)
    monkeypatch.setattr("kew.scheduler.project_root", lambda: tmp_path)


class TestDispatchSchedulerInit:
    def test_creates_with_config(self) -> None:
        config = DispatchConfig(parallel=ParallelConfig(max_workers=3))
        scheduler = DispatchScheduler(config)
        assert scheduler.max_workers == 3
        assert scheduler.in_flight == set()

    def test_default_single_worker(self) -> None:
        config = DispatchConfig()
        scheduler = DispatchScheduler(config)
        assert scheduler.max_workers == 1


def test_worker_dispatch_refuses_unavailable_final_runner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    (tmp_path / ".git").mkdir(exist_ok=True)
    from pathlib import Path

    from kew.config import SandboxConfig
    from kew.run_store import SqliteRunStore
    from kew.runners.codex import CodexRunner
    from kew.runners.registry import ResolvedRoute
    from kew.scheduler import _worker_dispatch

    config = DispatchConfig(
        sandbox=SandboxConfig(enabled=False, dangerously_skip_containment=True)
    )
    config.logging.dir = str(tmp_path / "logs")
    config.project.repo = "owner/repo"
    resolved = ResolvedRoute(CodexRunner(), "gpt-5.6-sol")
    refusal = "Runner 'codex' is not logged in. Authenticate with: codex login"
    launch = MagicMock()

    monkeypatch.setattr(
        "kew.dispatch_core._check_github_connectivity", lambda _config: True
    )
    monkeypatch.setattr("kew.dispatch_core.resolve_runner", lambda *a, **k: resolved)
    monkeypatch.setattr(
        "kew.dispatch_core.setup_worktree", lambda **_kwargs: Path(tmp_path / "wt")
    )
    monkeypatch.setattr("kew.dispatch_core.build_prompt", lambda **_kwargs: "prompt")
    monkeypatch.setattr("kew.dispatch_core.generate_repo_map", lambda *a, **k: "")
    monkeypatch.setattr("kew.dispatch_core.run_tracked_agent", launch)
    monkeypatch.setattr("kew.dispatch_core.mark_failed", lambda *a, **k: None)
    monkeypatch.setattr("kew.dispatch_core._cleanup_dispatch_dir", lambda *a, **k: None)
    monkeypatch.setattr(
        "kew.runner_preflight.runner_preflight_failure",
        lambda _config, *, runner_name=None: refusal,
    )

    issue = {"number": 42, "title": "Test", "body": "", "labels": []}
    outcome = _worker_dispatch(issue, config, {}, merge_context=None)

    assert outcome.exit_code == 1
    assert outcome.changed_files == []
    assert outcome.category == "runner_preflight_refused"
    assert outcome.reason == refusal
    launch.assert_not_called()
    with SqliteRunStore(str(tmp_path / "logs" / "dispatch.db")) as store:
        event = store.list_audit_events(event="dispatch_completed")[0]
    assert event.payload["runner"] == "codex"
    assert event.payload["transport_admission_reason"] == refusal


class TestInFlightTracking:
    def test_has_capacity_when_empty(self) -> None:
        config = DispatchConfig(parallel=ParallelConfig(max_workers=2))
        scheduler = DispatchScheduler(config)
        assert scheduler.has_capacity() is True

    def test_no_capacity_when_full(self) -> None:
        config = DispatchConfig(parallel=ParallelConfig(max_workers=1))
        scheduler = DispatchScheduler(config)
        scheduler.in_flight.add(42)
        assert scheduler.has_capacity() is False

    def test_available_slots(self) -> None:
        config = DispatchConfig(parallel=ParallelConfig(max_workers=3))
        scheduler = DispatchScheduler(config)
        scheduler.in_flight.add(42)
        assert scheduler.available_slots == 2

    def test_is_in_flight(self) -> None:
        config = DispatchConfig(parallel=ParallelConfig(max_workers=2))
        scheduler = DispatchScheduler(config)
        scheduler.in_flight.add(42)
        assert scheduler.is_in_flight(42) is True
        assert scheduler.is_in_flight(43) is False


class TestSelectExcludesInFlight:
    @patch("kew.scheduler.select_next_issue")
    def test_passes_in_flight_as_exclude(self, mock_select: MagicMock) -> None:
        """Verify in-flight issues are passed in the exclude kwarg."""
        config = DispatchConfig()
        scheduler = DispatchScheduler(config)
        scheduler.in_flight.add(10)
        scheduler.in_flight.add(20)

        mock_select.return_value = None

        scheduler.select_available_issues(max_count=1)

        mock_select.assert_called_once()
        kwargs = mock_select.call_args.kwargs
        assert kwargs["exclude"] == {10, 20}

    @patch("kew.scheduler.select_next_issue")
    def test_returns_empty_when_no_issues(self, mock_select: MagicMock) -> None:
        config = DispatchConfig()
        scheduler = DispatchScheduler(config)
        mock_select.return_value = None

        result = scheduler.select_available_issues(max_count=3)
        assert result == []

    @patch("kew.scheduler.select_next_issue")
    def test_selects_up_to_max_count(self, mock_select: MagicMock) -> None:
        config = DispatchConfig()
        scheduler = DispatchScheduler(config)

        issue1: dict[str, Any] = {"number": 1, "title": "a", "labels": [], "body": ""}
        issue2: dict[str, Any] = {"number": 2, "title": "b", "labels": [], "body": ""}

        mock_select.side_effect = [issue1, issue2, None]

        result = scheduler.select_available_issues(max_count=3)
        assert len(result) == 2
        assert result[0]["number"] == 1
        assert result[1]["number"] == 2

    @patch("kew.scheduler.select_next_issue")
    def test_accumulates_exclude_across_selections(
        self, mock_select: MagicMock
    ) -> None:
        """Each selected issue is added to the exclude set for subsequent calls."""
        config = DispatchConfig()
        scheduler = DispatchScheduler(config)

        issue1: dict[str, Any] = {"number": 5, "title": "a", "labels": [], "body": ""}
        issue2: dict[str, Any] = {"number": 7, "title": "b", "labels": [], "body": ""}

        mock_select.side_effect = [issue1, issue2, None]

        scheduler.select_available_issues(max_count=3)

        # First call: exclude is None (empty set is falsy, passed as None)
        first_call_exclude = mock_select.call_args_list[0].kwargs["exclude"]
        assert first_call_exclude is None

        # Second call: should include issue 5 in exclude
        second_call_exclude = mock_select.call_args_list[1].kwargs["exclude"]
        assert 5 in second_call_exclude

        # Third call: should include issues 5 and 7 in exclude
        third_call_exclude = mock_select.call_args_list[2].kwargs["exclude"]
        assert 5 in third_call_exclude
        assert 7 in third_call_exclude

    def test_two_worker_loop_excludes_a_pending_issue_and_uses_one_slot(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A durable live run consumes one slot and cannot be selected again."""
        from datetime import UTC, datetime

        from kew.run_store import RunRecord, SqliteRunStore

        config = DispatchConfig(parallel=ParallelConfig(max_workers=2))
        config.logging.dir = str(tmp_path / "logs")
        config.overlap.strategy = "off"
        (tmp_path / "logs").mkdir(parents=True)
        with SqliteRunStore(str(tmp_path / "logs" / "dispatch.db")) as store:
            store.save(
                RunRecord(
                    run_id="run-10",
                    issue_num=10,
                    item_id="10",
                    branch="agent/issue-10",
                    status="pending",
                    exit_code=-1,
                    started_at=datetime.now(UTC),
                )
            )

            issue_20: dict[str, Any] = {
                "number": 20,
                "title": "second",
                "labels": [],
                "body": "",
            }

            def select_issue(**kwargs: Any) -> dict[str, Any] | None:
                excluded = kwargs.get("exclude") or set()
                assert 10 in excluded
                return issue_20 if 20 not in excluded else None

            monkeypatch.setattr(
                "kew.graph.driver.tick.default_run_liveness",
                lambda _config: lambda _run: True,
            )
            scheduler = DispatchScheduler(config, store=store)
            with (
                patch("kew.scheduler.ProcessPoolExecutor"),
                patch(
                    "kew.scheduler.select_next_issue", side_effect=select_issue
                ) as pick,
                patch("kew.scheduler.time.sleep"),
            ):
                scheduler.run_loop(dry_run=True)

        assert pick.call_count == 1

    def test_dead_pending_issue_is_selectable_and_does_not_use_a_slot(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A provably dead pending row cannot hide its issue or consume capacity."""
        from datetime import UTC, datetime

        from kew.run_store import RunRecord, SqliteRunStore

        config = DispatchConfig(parallel=ParallelConfig(max_workers=1))
        config.logging.dir = str(tmp_path / "logs")
        (tmp_path / "logs").mkdir(parents=True)
        with SqliteRunStore(str(tmp_path / "logs" / "dispatch.db")) as store:
            store.save(
                RunRecord(
                    run_id="run-dead",
                    issue_num=10,
                    item_id="10",
                    branch="agent/issue-10",
                    status="pending",
                    exit_code=-1,
                    started_at=datetime.now(UTC),
                )
            )
            monkeypatch.setattr(
                "kew.graph.driver.tick.default_run_liveness",
                lambda _config: lambda _run: False,
            )
            scheduler = DispatchScheduler(config, store=store)

            assert scheduler._pending_issue_numbers() == set()
            assert scheduler._fleet_available_slots() == 1
            dead_issue: dict[str, Any] = {
                "number": 10,
                "title": "retry crashed dispatch",
                "labels": [],
                "body": "",
            }
            with patch(
                "kew.scheduler.select_next_issue", return_value=dead_issue
            ) as pick:
                assert scheduler.select_available_issues(max_count=1) == [dead_issue]
            assert pick.call_args.kwargs["exclude"] is None

    @pytest.mark.parametrize("probe_result", [True, False, None])
    @pytest.mark.parametrize("has_reservation", [False, True])
    def test_parent_and_child_share_three_state_fleet_capacity(
        self,
        tmp_path: Any,
        monkeypatch: pytest.MonkeyPatch,
        probe_result: bool | None,
        has_reservation: bool,
    ) -> None:
        """Parent selection and child preflight use one liveness predicate."""
        from datetime import UTC, datetime

        from kew.dispatch_core import _worker_cap_reached
        from kew.run_store import RunRecord, SqliteRunStore

        config = DispatchConfig(parallel=ParallelConfig(max_workers=1))
        config.logging.dir = str(tmp_path / "logs")
        (tmp_path / "logs").mkdir(parents=True)
        with SqliteRunStore(str(tmp_path / "logs" / "dispatch.db")) as store:
            if has_reservation:
                assert store.reserve_epic_node("epic-x", 10, "run-10") is True
            store.save(
                RunRecord(
                    run_id="run-10",
                    issue_num=10,
                    item_id="10",
                    branch="agent/issue-10",
                    status="pending",
                    exit_code=-1,
                    started_at=datetime.now(UTC),
                )
            )
            monkeypatch.setattr(
                "kew.graph.driver.tick.default_run_liveness",
                lambda _config: lambda _run: probe_result,
            )
            scheduler = DispatchScheduler(config, store=store)

            parent_slots = scheduler._fleet_available_slots()
            child_reached, child_live, cap = _worker_cap_reached(
                store, config, 20, "20"
            )
            expected_live = 0 if probe_result is False else 1
            expected_pending = set() if probe_result is False else {10}
            assert scheduler._pending_issue_numbers() == expected_pending
            assert child_live == expected_live
            assert parent_slots == cap - expected_live
            assert child_reached is (parent_slots == 0)

    def test_scheduler_preserves_aged_unprobeable_and_dead_pending(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Scheduler sweeps preserve pending rows and reservations under 24 hours."""
        from datetime import UTC, datetime, timedelta

        from kew.run_store import RunRecord, SqliteRunStore

        config = DispatchConfig(parallel=ParallelConfig(max_workers=3))
        config.logging.dir = str(tmp_path / "logs")
        config.overlap.strategy = "off"
        with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
            for number in (10, 20):
                assert store.reserve_epic_node("epic-x", number, f"run-{number}")
                store.save(
                    RunRecord(
                        run_id=f"run-{number}",
                        issue_num=number,
                        item_id=str(number),
                        branch=f"agent/issue-{number}",
                        status="pending",
                        exit_code=-1,
                        pgid=12345 if number == 20 else None,
                        started_at=datetime.now(UTC) - timedelta(hours=2),
                    )
                )
            monkeypatch.setattr(
                "kew.graph.driver.tick.default_run_liveness",
                lambda _config: lambda run: None if run.issue_num == 10 else False,
            )
            scheduler = DispatchScheduler(config, store=store)
            with (
                patch("kew.scheduler.ProcessPoolExecutor"),
                patch("kew.scheduler.select_next_issue", return_value=None) as pick,
                patch("kew.scheduler.time.sleep"),
                patch(
                    "kew.scheduler.time.monotonic", side_effect=[0, 0, 60, 60, 120, 120]
                ),
                patch.object(
                    store, "cleanup_stale_pending", wraps=store.cleanup_stale_pending
                ) as cleanup,
            ):
                scheduler.run_loop()

            assert pick.call_count == 3
            assert cleanup.call_count == 3
            assert {run.run_id for run in store.list_pending()} == {"run-10", "run-20"}
            assert len(store.list_epic_reservations("epic-x")) == 2
            assert store.list_audit_events(event="orphaned_run_reaped") == []

    def test_pending_probe_runs_once_per_row_per_loop_pass(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Counting and issue exclusions share one three-state result per pass."""
        from collections import Counter
        from datetime import UTC, datetime

        from kew.run_store import RunRecord, SqliteRunStore

        config = DispatchConfig(parallel=ParallelConfig(max_workers=4))
        config.logging.dir = str(tmp_path / "logs")
        config.overlap.strategy = "off"
        verdicts = {"run-10": True, "run-20": None, "run-30": False}
        pass_number = 0
        probes: Counter[tuple[int, str]] = Counter()

        def probe(run: RunRecord) -> bool | None:
            probes[pass_number, run.run_id] += 1
            return verdicts[run.run_id]

        def next_pass(_seconds: float) -> None:
            nonlocal pass_number
            pass_number += 1
            assert pass_number < 3

        monkeypatch.setattr(
            "kew.graph.driver.tick.default_run_liveness", lambda _config: probe
        )
        with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
            for number in (10, 20, 30):
                store.save(
                    RunRecord(
                        run_id=f"run-{number}",
                        issue_num=number,
                        item_id=str(number),
                        branch=f"agent/issue-{number}",
                        status="pending",
                        exit_code=-1,
                        started_at=datetime.now(UTC),
                    )
                )
            scheduler = DispatchScheduler(config, store=store)
            with (
                patch("kew.scheduler.ProcessPoolExecutor"),
                patch("kew.scheduler.select_next_issue", return_value=None) as pick,
                patch("kew.scheduler.time.sleep", side_effect=next_pass),
            ):
                scheduler.run_loop()

        assert probes == Counter(
            {(iteration, run_id): 1 for iteration in range(3) for run_id in verdicts}
        )

        assert [call.kwargs["exclude"] for call in pick.call_args_list] == [
            {10, 20}
        ] * 3

    @pytest.mark.parametrize("resolving", [False, True])
    def test_local_children_fill_capacity_without_store_access(
        self, tmp_path: Any, resolving: bool
    ) -> None:
        """Local capacity waits neither read pending rows nor probe them.

        The fleet-stop latch and the breaker are still checked on every pass
        (see ``test_local_capacity_waits_still_honor_fleet_stop_and_breaker``).
        """
        from datetime import UTC, datetime

        from kew.run_store import RunRecord, SqliteRunStore

        config = DispatchConfig(parallel=ParallelConfig(max_workers=1))
        config.logging.dir = str(tmp_path)
        config.overlap.strategy = "off"
        with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
            store.save(
                RunRecord(
                    run_id="run-10",
                    issue_num=10,
                    branch="agent/issue-10",
                    status="pending",
                    exit_code=-1,
                    started_at=datetime.now(UTC),
                )
            )
            scheduler = DispatchScheduler(config, store=store)
            probe = MagicMock(return_value=None)
            children = scheduler.resolve_in_flight if resolving else scheduler.in_flight
            children.add(10)

            def stop_after_three_waits(_seconds: float) -> None:
                if sleep.call_count == 3:
                    scheduler._shutdown = True

            with (
                patch("kew.scheduler.ProcessPoolExecutor"),
                patch(
                    "kew.scheduler.time.sleep", side_effect=stop_after_three_waits
                ) as sleep,
                patch(
                    "kew.graph.driver.tick.default_run_liveness", return_value=probe
                ) as probe_factory,
                patch("kew.scheduler._open_scheduler_store") as open_store,
                patch.object(
                    store, "cleanup_stale_pending", wraps=store.cleanup_stale_pending
                ) as cleanup,
                patch.object(
                    store, "list_pending", wraps=store.list_pending
                ) as pending,
                patch.object(
                    store, "live_run_count", wraps=store.live_run_count
                ) as count,
                patch.object(scheduler, "select_available_issues") as select,
            ):
                open_store.return_value.__enter__.return_value = store
                scheduler.run_loop()

            assert [call.args[0] for call in sleep.call_args_list] == [2, 2, 2]
            # The age-gated stale sweep runs once on the first pass (the
            # pre-loop sweep's contract); the 2s waits add no store reads.
            assert cleanup.call_count == 1
            for operation in (
                probe,
                probe_factory,
                open_store,
                pending,
                count,
                select,
            ):
                operation.assert_not_called()

    def test_local_capacity_waits_still_honor_fleet_stop_and_breaker(
        self, tmp_path: Any
    ) -> None:
        """A fleet full of the parent's own children still obeys kew stop.

        The latch and the breaker run at the top of every pass, before the
        2s local-capacity wait; a latch engaged during the wait ends the loop
        on the next pass instead of being ignored until a child exits.
        """
        from kew.run_store import SqliteRunStore

        config = DispatchConfig(parallel=ParallelConfig(max_workers=1))
        config.logging.dir = str(tmp_path)
        config.overlap.strategy = "off"
        with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
            scheduler = DispatchScheduler(config, store=store)
            scheduler.in_flight.add(10)

            def stop_after_three_waits(_seconds: float) -> None:
                if sleep.call_count == 3:
                    scheduler._shutdown = True

            with (
                patch("kew.scheduler.ProcessPoolExecutor"),
                patch(
                    "kew.scheduler.time.sleep", side_effect=stop_after_three_waits
                ) as sleep,
                patch.object(
                    scheduler, "_fleet_latched", side_effect=[False, False, True]
                ) as latched,
                patch.object(scheduler, "_check_fleet_breaker") as breaker,
                patch.object(scheduler, "select_available_issues") as select,
                patch("kew.scheduler._warn") as warn,
            ):
                scheduler.run_loop()

            assert [call.args[0] for call in sleep.call_args_list] == [2]
            assert latched.call_count == 3
            assert breaker.call_count == 1
            assert any(
                "Fleet stopped" in str(call.args[0]) for call in warn.call_args_list
            )
            select.assert_not_called()

    def test_preview_reaches_selection_when_durable_fleet_is_full(
        self, tmp_path: Any
    ) -> None:
        """--dry-run and --show-prompt never wait on the durable fleet count."""
        from datetime import UTC, datetime

        from kew.run_store import RunRecord, SqliteRunStore

        config = DispatchConfig(parallel=ParallelConfig(max_workers=1))
        config.logging.dir = str(tmp_path)
        config.overlap.strategy = "off"
        ready_issue: dict[str, Any] = {
            "number": 20,
            "title": "Preview me",
            "labels": [],
            "body": "",
        }
        with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
            store.save(
                RunRecord(
                    run_id="run-10",
                    issue_num=10,
                    branch="agent/issue-10",
                    status="pending",
                    exit_code=-1,
                    started_at=datetime.now(UTC),
                )
            )
            scheduler = DispatchScheduler(config, store=store)

            def stop_after_three_waits(_seconds: float) -> None:
                if sleep.call_count == 3:
                    scheduler._shutdown = True

            with (
                patch("kew.scheduler.ProcessPoolExecutor"),
                patch.object(scheduler, "_fleet_latched", return_value=False),
                patch.object(scheduler, "_check_fleet_breaker"),
                patch(
                    "kew.graph.driver.tick.default_run_liveness",
                    return_value=lambda _run: True,
                ),
                patch("kew.scheduler.select_next_issue", return_value=ready_issue),
                patch.object(scheduler, "_dispatch_batch") as dispatch,
                patch.object(
                    scheduler,
                    "_fleet_available_slots",
                    wraps=scheduler._fleet_available_slots,
                ) as durable,
                patch("kew.scheduler._log") as log,
                patch(
                    "kew.scheduler.time.sleep", side_effect=stop_after_three_waits
                ) as sleep,
            ):
                scheduler.run_loop(dry_run=True)

            # The durable count still runs (a live run consumes its slot in the
            # preview too), but a full fleet never waits: the batch is shown
            # from the local count and the loop exits.
            assert durable.call_count == 1
            assert dispatch.call_count == 1
            assert dispatch.call_args.kwargs["dry_run"] is True
            sleep.assert_not_called()
            assert not any(
                "Fleet capacity full" in str(call.args[0])
                for call in log.call_args_list
            )

    def test_capacity_log_failure_keeps_fleet_full_verdict(self, tmp_path: Any) -> None:
        """A failure describing the holders never turns 'full' into local capacity."""
        from datetime import UTC, datetime

        from kew.run_store import RunRecord, SqliteRunStore

        config = DispatchConfig(parallel=ParallelConfig(max_workers=1))
        config.logging.dir = str(tmp_path)
        with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
            store.save(
                RunRecord(
                    run_id="run-10",
                    issue_num=10,
                    branch="agent/issue-10",
                    status="pending",
                    exit_code=-1,
                    started_at=datetime.now(UTC),
                )
            )
            scheduler = DispatchScheduler(config, store=store)
            with (
                patch(
                    "kew.graph.driver.tick.default_run_liveness",
                    return_value=lambda _run: True,
                ),
                patch.object(
                    scheduler,
                    "_log_fleet_capacity",
                    side_effect=RuntimeError("holders unavailable"),
                ),
                patch("kew.scheduler._warn") as warn,
            ):
                assert scheduler._fleet_available_slots() == 0

            assert any(
                "describe fleet capacity holders" in str(call.args[0])
                for call in warn.call_args_list
            )

    def test_issue_hidden_by_pending_run_is_reported_as_held(
        self, tmp_path: Any
    ) -> None:
        """A ready issue hidden by an unprobeable pending row is never 'empty'."""
        from datetime import UTC, datetime

        from kew.run_store import RunRecord, SqliteRunStore

        config = DispatchConfig(parallel=ParallelConfig(max_workers=2))
        config.logging.dir = str(tmp_path)
        config.overlap.strategy = "off"
        ready_issue: dict[str, Any] = {
            "number": 20,
            "title": "Held",
            "labels": [],
            "body": "",
        }

        def select_issue(**kwargs: Any) -> dict[str, Any] | None:
            return None if kwargs.get("exclude") else ready_issue

        with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
            store.save(
                RunRecord(
                    run_id="run-20",
                    issue_num=20,
                    branch="agent/issue-20",
                    status="pending",
                    exit_code=-1,
                    started_at=datetime.now(UTC),
                )
            )
            scheduler = DispatchScheduler(config, store=store)
            with (
                patch("kew.scheduler.ProcessPoolExecutor"),
                patch.object(scheduler, "_fleet_latched", return_value=False),
                patch.object(scheduler, "_check_fleet_breaker"),
                patch(
                    "kew.graph.driver.tick.default_run_liveness",
                    return_value=lambda _run: None,
                ),
                patch("kew.scheduler.select_next_issue", side_effect=select_issue),
                patch(
                    "kew.scheduler.list_ready_issues", return_value=[ready_issue]
                ) as candidates,
                patch("kew.scheduler.empty_queue_message") as teaching,
                patch("kew.scheduler._log") as log,
                patch("kew.scheduler.time.sleep"),
            ):
                scheduler.run_loop()

            messages = [str(call.args[0]) for call in log.call_args_list]
            teaching.assert_not_called()
            # One candidate read per no-selection pass, never more.
            assert candidates.call_count == 3
            assert (
                "No selectable issue (held: #20 (pending run)). "
                "Empty 1/3. Retrying in 60s..."
            ) in messages
            assert messages[-1] == (
                "No selectable issue after 3 checks (held: #20 (pending run)). Exiting."
            )

    def test_over_cap_wait_follows_fleet_stop_check(self) -> None:
        """An over-cap refusal never delays the fleet-stop latch check."""
        config = DispatchConfig(parallel=ParallelConfig(max_workers=1))
        config.overlap.strategy = "off"
        scheduler = DispatchScheduler(config)
        with (
            patch("kew.scheduler.ProcessPoolExecutor"),
            patch.object(scheduler, "_reap_completed", return_value=True),
            patch.object(scheduler, "_fleet_latched", return_value=True),
            patch("kew.scheduler._warn") as warn,
            patch("kew.scheduler.time.sleep") as sleep,
        ):
            scheduler.run_loop()

        sleep.assert_not_called()
        assert any("Fleet stopped" in str(call.args[0]) for call in warn.call_args_list)

    def test_held_issue_named_once_when_child_also_owns_pending_row(self) -> None:
        """A running child that owns a live pending row is listed once."""
        config = DispatchConfig(parallel=ParallelConfig(max_workers=2))
        scheduler = DispatchScheduler(config)
        scheduler._last_exclusions = {
            "live child": {20},
            "skip backoff": {30},
            "pending run": {20, 40},
        }
        ready = [
            {"number": n, "title": "", "labels": [], "body": ""} for n in (20, 30, 40)
        ]

        with patch("kew.scheduler.list_ready_issues", return_value=ready):
            assert scheduler._held_issue_detail() == (
                "#20 (live child), #30 (skip backoff), #40 (pending run)"
            )

    def test_teaching_message_still_prints_after_a_held_first_pass(self) -> None:
        """A streak that starts held still teaches once when the queue empties."""
        config = DispatchConfig(parallel=ParallelConfig(max_workers=1))
        config.overlap.strategy = "off"
        scheduler = DispatchScheduler(config)
        scheduler._skip_backoff[20] = time.monotonic() + 1000.0

        def select_issue(**kwargs: Any) -> None:
            # The skip expires after the first pass: later passes are empty.
            scheduler._skip_backoff.clear()
            return None

        ready = [{"number": 20, "title": "", "labels": [], "body": ""}]

        with (
            patch("kew.scheduler.ProcessPoolExecutor"),
            patch.object(scheduler, "_fleet_latched", return_value=False),
            patch("kew.scheduler.select_next_issue", side_effect=select_issue),
            patch("kew.scheduler.list_ready_issues", return_value=ready),
            patch("kew.scheduler.empty_queue_message", return_value="TEACH"),
            patch("kew.scheduler._log") as log,
            patch("kew.scheduler.time.sleep"),
        ):
            scheduler.run_loop()

        messages = [str(call.args[0]) for call in log.call_args_list]
        assert messages[0].startswith("No selectable issue (held: #20 (skip backoff))")
        assert messages.count("TEACH") == 1
        assert messages[-1] == "Queue empty after 3 checks. Exiting."

    def test_pending_row_for_unready_issue_is_not_reported_as_held(
        self, tmp_path: Any
    ) -> None:
        """An exclusion that hides no ready candidate leaves the queue 'empty'."""
        from datetime import UTC, datetime

        from kew.run_store import RunRecord, SqliteRunStore

        config = DispatchConfig(parallel=ParallelConfig(max_workers=2))
        config.logging.dir = str(tmp_path)
        config.overlap.strategy = "off"
        with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
            # A pending row for #99, which is not labeled ready.
            store.save(
                RunRecord(
                    run_id="run-99",
                    issue_num=99,
                    branch="agent/issue-99",
                    status="pending",
                    exit_code=-1,
                    started_at=datetime.now(UTC),
                )
            )
            scheduler = DispatchScheduler(config, store=store)
            with (
                patch("kew.scheduler.ProcessPoolExecutor"),
                patch.object(scheduler, "_fleet_latched", return_value=False),
                patch.object(scheduler, "_check_fleet_breaker"),
                patch(
                    "kew.graph.driver.tick.default_run_liveness",
                    return_value=lambda _run: None,
                ),
                patch("kew.scheduler.select_next_issue", return_value=None),
                patch("kew.scheduler.list_ready_issues", return_value=[]),
                patch("kew.scheduler.empty_queue_message", return_value="TEACH"),
                patch("kew.scheduler._log") as log,
                patch("kew.scheduler.time.sleep"),
            ):
                scheduler.run_loop()

            messages = [str(call.args[0]) for call in log.call_args_list]
            assert messages.count("TEACH") == 1
            assert not any("held" in message for message in messages)
            assert messages[-1] == "Queue empty after 3 checks. Exiting."

    @pytest.mark.parametrize("row_exists", [True, False])
    def test_resolve_worker_occupies_one_fleet_slot(
        self, tmp_path: Any, row_exists: bool
    ) -> None:
        """A resolve child counts once: its pending row, or the starting child."""
        from datetime import UTC, datetime

        from kew.run_store import RunRecord, SqliteRunStore

        config = DispatchConfig(parallel=ParallelConfig(max_workers=2))
        config.logging.dir = str(tmp_path)
        with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
            if row_exists:
                # The resolve worker for PR #77 runs under its issue, #10.
                store.save(
                    RunRecord(
                        run_id="run-resolve-10",
                        issue_num=10,
                        item_id="10",
                        branch="agent/issue-10",
                        status="pending",
                        exit_code=-1,
                        started_at=datetime.now(UTC),
                    )
                )
            scheduler = DispatchScheduler(config, store=store)
            scheduler.resolve_in_flight.add(77)
            scheduler._resolve_items[77] = "10"
            with (
                patch(
                    "kew.graph.driver.tick.default_run_liveness",
                    return_value=lambda _run: True,
                ),
                patch("kew.scheduler._log") as log,
            ):
                assert scheduler._fleet_available_slots() == 1
                scheduler.resolve_in_flight.add(78)
                scheduler._resolve_items[78] = "11"
                assert scheduler._fleet_available_slots() == 0

            holders = [
                str(call.args[0])
                for call in log.call_args_list
                if "Fleet capacity full" in str(call.args[0])
            ]
            assert len(holders) == 1
            assert "PR #78 (live)" in holders[0]
            if row_exists:
                # The PR whose row is pending is described by that row, once.
                assert holders[0].count("10 (live)") == 1
                assert "PR #77" not in holders[0]
            else:
                assert "PR #77 (live)" in holders[0]
                assert "10 (live)" not in holders[0]

    def test_dispatching_a_resolve_records_its_item_key_until_reaped(
        self, tmp_path: Any
    ) -> None:
        from concurrent.futures import Future

        config = DispatchConfig(parallel=ParallelConfig(max_workers=2))
        config.logging.dir = str(tmp_path)
        config.resolve.auto = True
        scheduler = DispatchScheduler(config)
        pr = {"number": 77, "title": "t", "body": "", "headRefName": "agent/issue-10"}
        future: Future[SimpleNamespace] = Future()
        pool = MagicMock()
        pool.submit.return_value = future
        futures: dict[Future[SimpleNamespace], int] = {}

        with patch.object(scheduler, "_poll_conflicting_prs", return_value=[pr]):
            scheduler._dispatch_conflicting_resolves(pool, futures)

        assert scheduler._resolve_items == {77: "10"}
        future.set_result(SimpleNamespace(kind="executed", exit_code=0))
        with patch("kew.scheduler._success") as success:
            scheduler._reap_resolve_completed(futures)
        success.assert_called_once_with("Resolve worker for PR #77 completed.")
        assert scheduler._resolve_items == {}
        assert scheduler.resolve_in_flight == set()

    def test_loop_exits_after_bounded_wait_on_a_fleet_held_by_others(
        self, tmp_path: Any
    ) -> None:
        """Another actor's live run holding the last slot cannot idle the loop."""
        from datetime import UTC, datetime

        from kew.run_store import RunRecord, SqliteRunStore

        config = DispatchConfig(parallel=ParallelConfig(max_workers=1))
        config.logging.dir = str(tmp_path)
        config.overlap.strategy = "off"
        with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
            store.save(
                RunRecord(
                    run_id="run-10",
                    issue_num=10,
                    item_id="10",
                    branch="agent/issue-10",
                    status="pending",
                    exit_code=-1,
                    started_at=datetime.now(UTC),
                )
            )
            scheduler = DispatchScheduler(config, store=store)
            with (
                patch("kew.scheduler.ProcessPoolExecutor"),
                patch.object(scheduler, "_fleet_latched", return_value=False),
                patch.object(scheduler, "_check_fleet_breaker"),
                patch(
                    "kew.graph.driver.tick.default_run_liveness",
                    return_value=lambda _run: True,
                ),
                patch("kew.scheduler.select_next_issue") as pick,
                patch("kew.scheduler._log") as log,
                patch("kew.scheduler.time.sleep") as sleep,
            ):
                scheduler.run_loop()

            waits = [call.args[0] for call in sleep.call_args_list]
            assert waits == [60] * (DispatchScheduler._CAPACITY_WAIT_CHECKS - 1)
            pick.assert_not_called()
            messages = [str(call.args[0]) for call in log.call_args_list]
            assert messages[-1] == (
                f"Fleet capacity full for {DispatchScheduler._CAPACITY_WAIT_CHECKS} "
                "checks (held by: 10 (live)). Exiting; rerun when a slot frees."
            )

    def test_a_pass_with_capacity_resets_the_bounded_wait(self, tmp_path: Any) -> None:
        config = DispatchConfig(parallel=ParallelConfig(max_workers=1))
        config.logging.dir = str(tmp_path)
        scheduler = DispatchScheduler(config)
        scheduler._durable_full_checks = DispatchScheduler._CAPACITY_WAIT_CHECKS - 1
        with (
            patch.object(scheduler, "_dispatch_conflicting_resolves"),
            patch.object(scheduler, "_fleet_available_slots", return_value=1),
        ):
            assert scheduler._poll_dispatch_slots(MagicMock(), {}) == 1
        assert scheduler._durable_full_checks == 0

    def test_child_with_dead_row_is_named_as_a_starting_holder(
        self, tmp_path: Any
    ) -> None:
        """A child whose own row probes dead still holds a slot and is named."""
        from datetime import UTC, datetime

        from kew.run_store import RunRecord, SqliteRunStore

        config = DispatchConfig(parallel=ParallelConfig(max_workers=1))
        config.logging.dir = str(tmp_path)
        with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
            store.save(
                RunRecord(
                    run_id="run-20",
                    issue_num=20,
                    item_id="20",
                    branch="agent/issue-20",
                    status="pending",
                    exit_code=-1,
                    started_at=datetime.now(UTC),
                )
            )
            scheduler = DispatchScheduler(config, store=store)
            scheduler.in_flight.add(20)
            with (
                patch(
                    "kew.graph.driver.tick.default_run_liveness",
                    return_value=lambda _run: False,
                ),
                patch("kew.scheduler._log") as log,
            ):
                assert scheduler._fleet_available_slots() == 0

            messages = [str(call.args[0]) for call in log.call_args_list]
            assert any("Holders: 20 (live)" in message for message in messages)
            assert not any("Holders: none" in message for message in messages)

    def test_bounded_capacity_exit_waits_while_own_children_run(
        self, tmp_path: Any
    ) -> None:
        """A parent with a live child never quits the loop for fleet capacity."""
        config = DispatchConfig(parallel=ParallelConfig(max_workers=2))
        config.logging.dir = str(tmp_path)
        scheduler = DispatchScheduler(config)
        scheduler.in_flight.add(10)
        scheduler._durable_full_checks = DispatchScheduler._CAPACITY_WAIT_CHECKS + 3
        with (
            patch.object(scheduler, "_dispatch_conflicting_resolves"),
            patch.object(scheduler, "_fleet_available_slots", return_value=0),
            patch("kew.scheduler._log") as log,
            patch("kew.scheduler.time.sleep") as sleep,
        ):
            assert scheduler._poll_dispatch_slots(MagicMock(), {}) == 0
            scheduler.in_flight.clear()
            assert scheduler._poll_dispatch_slots(MagicMock(), {}) is None

        sleep.assert_called_once_with(60)
        messages = [str(call.args[0]) for call in log.call_args_list]
        assert len(messages) == 1
        assert messages[0].startswith("Fleet capacity full for ")
        assert "Exiting" in messages[0]

    def test_durable_full_waits_log_holders_once_and_preserve_empty_streak(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two full passes suspend an existing streak and log both verdicts once."""
        from datetime import UTC, datetime

        from kew.run_store import RunRecord, SqliteRunStore

        config = DispatchConfig(parallel=ParallelConfig(max_workers=2))
        config.logging.dir = str(tmp_path / "logs")
        config.overlap.strategy = "off"
        monkeypatch.setattr(
            "kew.graph.driver.tick.default_run_liveness",
            lambda _config: lambda run: True if run.issue_num == 10 else None,
        )
        with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
            scheduler = DispatchScheduler(config, store=store)

            def update_holders(_seconds: float) -> None:
                assert sleep.call_count < 6
                if sleep.call_count == 1:
                    for number in (10, 20):
                        store.save(
                            RunRecord(
                                run_id=f"run-{number}",
                                issue_num=number,
                                item_id=str(number),
                                branch=f"agent/issue-{number}",
                                status="pending",
                                exit_code=-1,
                                started_at=datetime.now(UTC),
                            )
                        )
                elif sleep.call_count == 3:
                    store.delete("run-10")
                    store.delete("run-20")

            with (
                patch("kew.scheduler.ProcessPoolExecutor"),
                patch("kew.scheduler.select_next_issue", return_value=None) as pick,
                patch("kew.scheduler.list_ready_issues", return_value=[]),
                patch("kew.scheduler.time.sleep", side_effect=update_holders) as sleep,
                patch.object(scheduler, "_log_empty_queue") as empty_log,
                patch("kew.scheduler._log") as log,
            ):
                scheduler.run_loop()

        assert [call.args[0] for call in sleep.call_args_list] == [60, 60, 60, 60]
        assert [call.args[0] for call in empty_log.call_args_list] == [1, 2]
        assert pick.call_count == 3
        messages = [str(call.args[0]) for call in log.call_args_list]
        capacity_messages = [
            message for message in messages if "Fleet capacity full" in message
        ]
        assert len(capacity_messages) == 1
        assert "10 (live)" in capacity_messages[0]
        assert "20 (unprobeable)" in capacity_messages[0]
        assert "24-hour stale-pending backstop reclaims it" in capacity_messages[0]
        assert "frees nothing younger than 24 hours" in capacity_messages[0]
        assert messages[-1] == "Queue empty after 3 checks. Exiting."

    def test_capacity_log_tracks_reservations_and_verdict_changes(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reservations and changed verdicts produce fresh capacity logs."""
        from datetime import UTC, datetime

        from kew.run_store import RunRecord, SqliteRunStore

        config = DispatchConfig(parallel=ParallelConfig(max_workers=1))
        verdict: bool | None = None
        monkeypatch.setattr(
            "kew.graph.driver.tick.default_run_liveness",
            lambda _config: lambda _run: verdict,
        )
        with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
            assert store.reserve_epic_node_item("epic-x", "SFD-20", "run-20")
            scheduler = DispatchScheduler(config, store=store)
            with patch("kew.scheduler._log") as log:
                assert scheduler._fleet_available_slots() == 0
                assert scheduler._fleet_available_slots() == 0
                store.save(
                    RunRecord(
                        run_id="run-20",
                        issue_num=0,
                        item_id="SFD-20",
                        branch="agent/issue-sfd-20",
                        status="pending",
                        exit_code=-1,
                        started_at=datetime.now(UTC),
                    )
                )
                assert scheduler._fleet_available_slots() == 0
                assert log.call_count == 1
                verdict = True
                assert scheduler._fleet_available_slots() == 0
                assert log.call_count == 2
                verdict = False
                assert scheduler._fleet_available_slots() == 1
                verdict = True
                assert scheduler._fleet_available_slots() == 0

        messages = [str(call.args[0]) for call in log.call_args_list]
        assert len(messages) == 3
        assert "SFD-20 (unprobeable)" in messages[0]
        assert all("SFD-20 (live)" in message for message in messages[1:])

    def test_young_unprobeable_pending_remains_live(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unprobeable startup row retains its row, exclusion, and slot."""
        from datetime import UTC, datetime

        from kew.run_store import RunRecord, SqliteRunStore

        config = DispatchConfig(parallel=ParallelConfig(max_workers=1))
        config.logging.dir = str(tmp_path / "logs")
        (tmp_path / "logs").mkdir(parents=True)
        with SqliteRunStore(str(tmp_path / "logs" / "dispatch.db")) as store:
            store.save(
                RunRecord(
                    run_id="run-unknown",
                    issue_num=10,
                    item_id="10",
                    branch="agent/issue-10",
                    status="pending",
                    exit_code=-1,
                    started_at=datetime.now(UTC),
                )
            )
            monkeypatch.setattr(
                "kew.graph.driver.tick.default_run_liveness",
                lambda _config: lambda _run: None,
            )
            scheduler = DispatchScheduler(config, store=store)

            scheduler._cleanup_stale_pending()
            record = store.get("run-unknown")

            assert record is not None
            assert record.status == "pending"
            assert scheduler._pending_issue_numbers() == {10}
            assert scheduler._fleet_available_slots() == 0


class TestFleetWorkerOutcomes:
    @staticmethod
    def _config(tmp_path: Any) -> DispatchConfig:
        config = DispatchConfig(parallel=ParallelConfig(max_workers=1))
        config.logging.dir = str(tmp_path / "logs")
        config.overlap.strategy = "off"
        (tmp_path / "logs").mkdir(parents=True)
        return config

    @staticmethod
    def _save_pending(config: DispatchConfig) -> None:
        from datetime import UTC, datetime

        from kew.run_store import RunRecord, SqliteRunStore

        with SqliteRunStore(str(Path(config.logging.dir) / "dispatch.db")) as store:
            store.save(
                RunRecord(
                    run_id="run-10",
                    issue_num=10,
                    item_id="10",
                    branch="agent/issue-10",
                    status="pending",
                    exit_code=-1,
                    started_at=datetime.now(UTC),
                )
            )

    @staticmethod
    def _issue() -> dict[str, Any]:
        return {"number": 20, "title": "second", "labels": [], "body": ""}

    def test_child_over_cap_refusal_restores_ready_without_parent_reap(
        self, tmp_path: Any
    ) -> None:
        (tmp_path / ".git").mkdir(exist_ok=True)
        from kew.scheduler import _worker_dispatch

        config = self._config(tmp_path)
        self._save_pending(config)
        with (
            patch("kew.dispatch_core._capture_root_checkout_state", return_value=None),
            patch("kew.dispatch_core.reset_issue_to_ready") as reset_ready,
        ):
            outcome = _worker_dispatch(self._issue(), config, {})

        reset_ready.assert_called_once_with(20, config)
        assert outcome.category == "requeued_over_cap"
        assert "1/1 live" in outcome.reason

    def test_child_over_cap_refusal_leaves_audit_evidence(self, tmp_path: Any) -> None:
        (tmp_path / ".git").mkdir(exist_ok=True)
        from kew.run_store import SqliteRunStore
        from kew.scheduler import _worker_dispatch

        config = self._config(tmp_path)
        self._save_pending(config)
        with (
            patch("kew.dispatch_core._capture_root_checkout_state", return_value=None),
            patch("kew.dispatch_core.reset_issue_to_ready"),
        ):
            _worker_dispatch(self._issue(), config, {})

        with SqliteRunStore(str(Path(config.logging.dir) / "dispatch.db")) as store:
            events = store.list_audit_events(event="dispatch_completed")
            refused_runs = store.list_by_issue(20)
        assert len(events) == 1
        assert refused_runs == []
        assert events[0].issue_num == 20
        assert events[0].payload["error_category"] == "requeued_over_cap"
        assert "1/1 live" in events[0].payload["reason"]

    def test_two_child_cycles_suppress_parent_owned_cap_warning(
        self, tmp_path: Any
    ) -> None:
        (tmp_path / ".git").mkdir(exist_ok=True)
        from kew.scheduler import _worker_dispatch

        config = self._config(tmp_path)
        config.agent.max_budget_usd = 25.0
        config.budget.per_issue_usd = 0.40
        config.budget.model_fields_set.add("per_issue_usd")
        self._save_pending(config)
        with (
            patch("kew.dispatch_core._capture_root_checkout_state", return_value=None),
            patch("kew.dispatch_core.reset_issue_to_ready"),
            patch("kew.dispatch_core._warn_if_cap_conflict") as cap_warning,
        ):
            _worker_dispatch(self._issue(), config, {})
            _worker_dispatch(self._issue(), config, {})

        cap_warning.assert_not_called()

    def test_parent_warning_uses_selected_issue_spend(self, tmp_path: Any) -> None:
        from concurrent.futures import Future
        from datetime import UTC, datetime

        from kew.run_store import RunRecord, SqliteRunStore

        config = self._config(tmp_path)
        config.agent.max_budget_usd = 0.30
        config.agent.model_fields_set.add("max_budget_usd")
        config.budget.per_issue_usd = 0.40
        config.budget.model_fields_set.add("per_issue_usd")
        with SqliteRunStore(str(Path(config.logging.dir) / "dispatch.db")) as store:
            store.save(
                RunRecord(
                    run_id="spent-20",
                    issue_num=20,
                    item_id="20",
                    branch="agent/issue-20",
                    status="success",
                    exit_code=0,
                    started_at=datetime.now(UTC),
                    estimated_cost_usd=0.20,
                )
            )
            scheduler = DispatchScheduler(config, store=store)
            pool = MagicMock()
            future: Future[Any] = Future()
            pool.submit.return_value = future
            futures: dict[Future[Any], int] = {}

            with (
                patch("kew.scheduler.remote_branch_exists", return_value=False),
                patch("kew.scheduler.try_claim_issue", return_value=True),
                patch("kew.dispatch_core._warn") as warning,
            ):
                scheduler._dispatch_issue(
                    self._issue(),
                    pool,
                    futures,
                    source_files=[],
                    identifier_index={},
                    module_index={},
                )

        warnings = [str(call.args[0]) for call in warning.call_args_list]
        assert any("Runtime cap $0.30" in message for message in warnings)
        assert any("headroom $0.20" in message for message in warnings)

    def test_parent_reports_exit_category_and_reason(self) -> None:
        from concurrent.futures import Future
        from types import SimpleNamespace

        config = DispatchConfig(parallel=ParallelConfig(max_workers=1))
        scheduler = DispatchScheduler(config)
        future: Future[Any] = Future()
        future.set_result(
            SimpleNamespace(
                exit_code=1,
                changed_files=[],
                category="runner_preflight_refused",
                reason="runner authentication is missing",
            )
        )
        scheduler.in_flight.add(20)

        with (
            patch("kew.scheduler._warn") as warning,
            patch("kew.scheduler.mark_failed"),
        ):
            scheduler._reap_completed({future: 20})

        warning.assert_called_once_with(
            "Worker #20 exited with code 1: runner_preflight_refused "
            "— runner authentication is missing"
        )

    def test_parent_retries_over_cap_issue_without_skip_backoff(self) -> None:
        from concurrent.futures import Future
        from types import SimpleNamespace

        config = DispatchConfig(parallel=ParallelConfig(max_workers=1))
        scheduler = DispatchScheduler(config)
        future: Future[Any] = Future()
        future.set_result(
            SimpleNamespace(
                exit_code=1,
                changed_files=[],
                category="requeued_over_cap",
                reason="worker cap reached",
            )
        )
        scheduler.in_flight.add(20)

        with (
            patch("kew.scheduler.rollback_claim") as restore_ready,
            patch("kew.scheduler._warn"),
            patch("kew.scheduler.mark_failed"),
        ):
            scheduler._reap_completed({future: 20})

        assert 20 not in scheduler._active_skips()
        restore_ready.assert_not_called()

    def test_over_cap_refusal_is_reselected_without_empty_queue_exit(self) -> None:
        """A child capacity refusal stays selectable on the next loop pass."""
        from concurrent.futures import Future

        config = DispatchConfig(parallel=ParallelConfig(max_workers=1))
        config.overlap.strategy = "off"
        scheduler = DispatchScheduler(config)
        issue = self._issue()
        dispatch_passes = 0
        selections_at_sleep: list[int] = []

        def select_issue(**kwargs: Any) -> dict[str, Any]:
            assert 20 not in (kwargs.get("exclude") or set())
            return issue

        def dispatch_batch(
            _issues: list[dict[str, Any]],
            _pool: Any,
            futures: dict[Future[DispatchOutcome], int],
            **_kwargs: Any,
        ) -> None:
            nonlocal dispatch_passes
            dispatch_passes += 1
            if dispatch_passes == 1:
                future: Future[DispatchOutcome] = Future()
                future.set_result(
                    DispatchOutcome(
                        exit_code=1,
                        category="requeued_over_cap",
                        reason="worker cap reached",
                    )
                )
                futures[future] = 20
                scheduler.in_flight.add(20)
            else:
                scheduler._shutdown = True

        with (
            patch("kew.scheduler.ProcessPoolExecutor"),
            patch.object(scheduler, "_fleet_latched", return_value=False),
            patch.object(scheduler, "_fleet_available_slots", return_value=1),
            patch("kew.scheduler.select_next_issue", side_effect=select_issue) as pick,
            patch.object(scheduler, "_dispatch_batch", side_effect=dispatch_batch),
            patch("kew.scheduler._log") as log,
            patch(
                "kew.scheduler.time.sleep",
                side_effect=lambda _seconds: selections_at_sleep.append(
                    pick.call_count
                ),
            ) as sleep,
        ):
            scheduler.run_loop()

        assert pick.call_count == 2
        sleep.assert_called_once_with(60)
        assert selections_at_sleep == [1]
        assert dispatch_passes == 2
        assert 20 not in scheduler._active_skips()
        messages = [str(call.args[0]) for call in log.call_args_list]
        assert not any("Queue empty" in message for message in messages)


class TestSchedulerDuplicateGuard:
    """Scheduler skips issues that are already being dispatched elsewhere."""

    @patch("kew.scheduler.time.sleep")
    @patch("kew.scheduler.try_claim_issue", return_value=True)
    @patch("kew.scheduler.remote_branch_exists", return_value=True)
    @patch("kew.scheduler.select_next_issue")
    def test_skips_issue_when_remote_branch_exists(
        self,
        mock_select: MagicMock,
        mock_branch: MagicMock,
        mock_claim: MagicMock,
        mock_sleep: MagicMock,
    ) -> None:
        config = DispatchConfig(parallel=ParallelConfig(max_workers=1))
        scheduler = DispatchScheduler(config)

        issue: dict[str, Any] = {
            "number": 42,
            "title": "Test",
            "labels": [],
            "body": "",
        }
        mock_select.side_effect = [issue, None, issue]
        mock_sleep.side_effect = lambda _seconds: setattr(scheduler, "_shutdown", True)

        scheduler.run_loop(dry_run=False)

        mock_claim.assert_not_called()

    @patch("kew.scheduler.time.sleep")
    @patch("kew.scheduler.remote_branch_exists", return_value=False)
    @patch(
        "kew.scheduler.try_claim_issue",
        return_value=ClaimResult.ALREADY_CLAIMED,
    )
    @patch("kew.scheduler.select_next_issue")
    def test_skips_issue_when_claim_fails(
        self,
        mock_select: MagicMock,
        mock_claim: MagicMock,
        mock_branch: MagicMock,
        mock_sleep: MagicMock,
    ) -> None:
        config = DispatchConfig(parallel=ParallelConfig(max_workers=1))
        scheduler = DispatchScheduler(config)

        issue: dict[str, Any] = {
            "number": 42,
            "title": "Test",
            "labels": [],
            "body": "",
        }
        mock_select.side_effect = [issue, None, None, None]

        scheduler.run_loop(dry_run=False)

        # Verify claim was attempted but the issue was not dispatched
        mock_claim.assert_called_once_with(42, config)


class TestSchedulerSkipBackoff:
    """Permanently-skippable issues are held out of selection with backoff.

    Without skip memory the loop re-selects the same undispatchable issue (e.g.
    one whose remote branch already exists) every iteration with no sleep,
    hammering the gh API (#SFD-151, I9).
    """

    def _issue(self, num: int) -> dict[str, Any]:
        return {"number": num, "title": "t", "labels": [], "body": ""}

    @patch("kew.scheduler.remote_branch_exists", return_value=True)
    def test_dispatch_records_skip_on_stale_remote_branch(
        self, mock_branch: MagicMock
    ) -> None:
        config = DispatchConfig(parallel=ParallelConfig(max_workers=1))
        config.overlap.strategy = "off"
        scheduler = DispatchScheduler(config)

        scheduler._dispatch_issue(
            self._issue(42),
            MagicMock(),
            {},
            source_files=[],
            identifier_index={},
            module_index={},
        )

        assert 42 in scheduler._skip_backoff
        assert 42 in scheduler._active_skips()

    @patch("kew.scheduler.select_next_issue", return_value=None)
    def test_active_skips_are_excluded_from_selection(
        self, mock_select: MagicMock
    ) -> None:
        import time

        config = DispatchConfig(parallel=ParallelConfig(max_workers=1))
        scheduler = DispatchScheduler(config)
        scheduler._skip_backoff[99] = time.monotonic() + 1000.0

        scheduler.select_available_issues(max_count=1)

        assert 99 in mock_select.call_args.kwargs["exclude"]

    def test_expired_skips_are_pruned_and_retried(self) -> None:
        import time

        config = DispatchConfig(parallel=ParallelConfig(max_workers=1))
        scheduler = DispatchScheduler(config)
        scheduler._skip_backoff[7] = time.monotonic() - 1.0  # already expired

        assert scheduler._active_skips() == set()
        assert 7 not in scheduler._skip_backoff

    def test_skip_backoff_advances_empty_streak_and_exits(self) -> None:
        """A hidden ready issue still advances the three-check exit streak."""
        config = DispatchConfig(parallel=ParallelConfig(max_workers=1))
        config.overlap.strategy = "off"
        scheduler = DispatchScheduler(config)
        scheduler._skip_backoff[20] = time.monotonic() + 1000.0
        ready_issue = self._issue(20)

        def select_issue(**kwargs: Any) -> dict[str, Any] | None:
            return None if kwargs.get("exclude") else ready_issue

        def stop_loop(_seconds: float) -> None:
            assert sleep.call_count <= 2, "Hidden candidates must not keep polling"

        with (
            patch("kew.scheduler.ProcessPoolExecutor"),
            patch.object(scheduler, "_fleet_latched", return_value=False),
            patch("kew.scheduler.select_next_issue", side_effect=select_issue) as pick,
            patch("kew.scheduler.list_ready_issues", return_value=[ready_issue]),
            patch.object(scheduler, "_log_empty_queue") as empty_log,
            patch("kew.scheduler._log") as log,
            patch("kew.scheduler.time.sleep", side_effect=stop_loop) as sleep,
        ):
            scheduler.run_loop()

        assert [call.args[0] for call in empty_log.call_args_list] == [1, 2]
        assert pick.call_count == 3
        log.assert_called_with(
            "No selectable issue after 3 checks (held: #20 (skip backoff)). Exiting."
        )


class TestSchedulerShowPrompt:
    """Show-prompt preview threads item_id into build_prompt (A5a)."""

    @patch("kew.scheduler.build_prompt", return_value="PROMPT")
    def test_handle_preview_passes_item_id(self, mock_build: MagicMock) -> None:
        config = DispatchConfig(parallel=ParallelConfig(max_workers=1))
        scheduler = DispatchScheduler(config)
        issue: dict[str, Any] = {
            "number": 42,
            "title": "Test",
            "labels": [],
            "body": "",
        }

        handled = scheduler._handle_preview(issue, show_prompt=True, dry_run=False)

        assert handled is True
        kwargs = mock_build.call_args.kwargs
        assert kwargs["issue_num"] == 42
        assert kwargs["item_id"] == "42"

    def test_codex_preview_is_host_publication(self, capsys) -> None:
        config = DispatchConfig(parallel=ParallelConfig(max_workers=1))
        config.agent.runner = "codex"
        config.agent.model = "gpt-5.6-sol"
        scheduler = DispatchScheduler(config)
        issue: dict[str, Any] = {
            "number": 42,
            "title": "Codex preview",
            "labels": [],
            "body": "",
        }
        assert scheduler._handle_preview(issue, show_prompt=True, dry_run=False)
        prompt = capsys.readouterr().out
        assert "Only edit files and run verification" in prompt
        assert "gh pr create" not in prompt


class TestSchedulerOverlapIntegration:
    def test_scheduler_creates_coordinator(self):
        config = DispatchConfig()
        scheduler = DispatchScheduler(config)
        assert isinstance(scheduler.coordinator, OverlapCoordinator)


class TestSchedulerClaimRollback:
    """A failed pool.submit must release the claim, not strand the issue (#272).

    try_claim_issue swaps the issue to in-progress on GitHub; if the
    subsequent pool.submit raises (executor shutting down) the issue would
    otherwise be left in-progress with no worker until `kew cleanup` repairs it.
    """

    def _scheduler(self, tmp_path: Any) -> DispatchScheduler:
        config = DispatchConfig(parallel=ParallelConfig(max_workers=1))
        config.overlap.strategy = "off"
        config.logging.dir = str(tmp_path / "logs")
        (tmp_path / "logs").mkdir(parents=True)
        return DispatchScheduler(config)

    @patch("kew.scheduler.rollback_claim")
    @patch("kew.scheduler.remote_branch_exists", return_value=False)
    @patch("kew.scheduler.try_claim_issue", return_value=True)
    def test_submit_failure_restores_ready_label(
        self,
        mock_claim: MagicMock,
        mock_branch: MagicMock,
        mock_rollback: MagicMock,
        tmp_path: Any,
    ) -> None:
        scheduler = self._scheduler(tmp_path)
        issue: dict[str, Any] = {"number": 42, "title": "T", "labels": [], "body": ""}

        pool = MagicMock()
        pool.submit.side_effect = RuntimeError("cannot schedule new futures")
        futures: dict[Any, int] = {}

        scheduler._dispatch_issue(
            issue,
            pool,
            futures,
            source_files=[],
            identifier_index={},
            module_index={},
        )

        mock_rollback.assert_called_once_with(42, scheduler.config)
        assert futures == {}
        assert 42 not in scheduler.in_flight

    @patch("kew.scheduler.rollback_claim")
    @patch("kew.scheduler.remote_branch_exists", return_value=False)
    @patch("kew.scheduler.try_claim_issue", return_value=True)
    def test_submit_failure_emits_audit_event(
        self,
        mock_claim: MagicMock,
        mock_branch: MagicMock,
        mock_rollback: MagicMock,
        tmp_path: Any,
    ) -> None:
        (tmp_path / ".git").mkdir(exist_ok=True)
        from kew.run_store import SqliteRunStore

        scheduler = self._scheduler(tmp_path)
        issue: dict[str, Any] = {"number": 42, "title": "T", "labels": [], "body": ""}

        pool = MagicMock()
        pool.submit.side_effect = RuntimeError("cannot schedule new futures")

        scheduler._dispatch_issue(
            issue,
            pool,
            {},
            source_files=[],
            identifier_index={},
            module_index={},
        )

        with SqliteRunStore(str(tmp_path / "logs" / "dispatch.db")) as store:
            events = store.list_audit_events(event="claim_rollback")
        assert len(events) == 1
        assert events[0].actor_type == "policy"
        assert events[0].issue_num == 42


class TestSchedulerStalePendingCleanup:
    """run_loop reaps PENDING rows stranded by a crashed dispatch (#SFD-152)."""

    def _make_stale_pending(self, tmp_path):
        from datetime import UTC, datetime, timedelta

        from kew.run_store import RunRecord, SqliteRunStore

        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        config.overlap.strategy = "off"
        (tmp_path / "logs").mkdir(parents=True)
        # Latch the fleet so the loop reaps stale pending, then exits before
        # claiming any work.
        with SqliteRunStore(str(tmp_path / "logs" / "dispatch.db")) as store:
            store.save(
                RunRecord(
                    run_id="run-stale",
                    issue_num=1,
                    branch="agent/issue-1",
                    status="pending",
                    exit_code=-1,
                    started_at=datetime.now(UTC) - timedelta(hours=25),
                    pgid=2**31 - 1,
                    owner_pid=2**31 - 1,
                    attempt_pgid=2**31 - 2,
                )
            )
            store.set_fleet_stopped("halt")
        return config

    @patch("kew.scheduler.ProcessPoolExecutor")
    def test_run_loop_cleans_pending_older_than_24_hours_on_startup(
        self, mock_pool, tmp_path
    ):
        (tmp_path / ".git").mkdir(exist_ok=True)
        from kew.run_store import SqliteRunStore

        config = self._make_stale_pending(tmp_path)
        scheduler = DispatchScheduler(config=config)
        scheduler.run_loop()

        with SqliteRunStore(str(tmp_path / "logs" / "dispatch.db")) as store:
            record = store.get("run-stale")
        assert record is not None
        assert record.status == "failure"
        assert record.error_category == "stale_pending"

    def test_cleanup_stale_pending_no_db_is_noop(self, tmp_path):
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "no-such-dir")
        scheduler = DispatchScheduler(config=config)
        # Must not raise or create the DB when there is nothing to clean.
        scheduler._cleanup_stale_pending()
        assert not (tmp_path / "no-such-dir" / "dispatch.db").exists()

    @patch("kew.scheduler.ProcessPoolExecutor")
    def test_run_loop_repeats_stale_pending_cleanup(self, mock_pool, tmp_path):
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        config.overlap.strategy = "off"
        scheduler = DispatchScheduler(config=config)

        with (
            patch.object(scheduler, "_cleanup_stale_pending") as cleanup,
            patch.object(scheduler, "_fleet_latched", return_value=False),
            patch.object(scheduler, "_check_fleet_breaker"),
            patch.object(scheduler, "select_available_issues", return_value=[]),
            patch("kew.scheduler.time.sleep"),
            patch("kew.scheduler.time.monotonic", side_effect=[0.0, 60.0, 120.0]),
        ):
            scheduler.run_loop()

        assert cleanup.call_count == 3


class TestSchedulerFleetLatch:
    """The scheduler stops claiming issues when the fleet is latched."""

    def test_fleet_latched_true_when_latch_set(self, tmp_path):
        (tmp_path / ".git").mkdir(exist_ok=True)
        from kew.run_store import SqliteRunStore

        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        (tmp_path / "logs").mkdir(parents=True)
        with SqliteRunStore(str(tmp_path / "logs" / "dispatch.db")) as store:
            store.set_fleet_stopped("halt")
        scheduler = DispatchScheduler(config=config)
        assert scheduler._fleet_latched() is True

    def test_fleet_latched_false_without_db(self, tmp_path):
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "no-such-dir")
        scheduler = DispatchScheduler(config=config)
        assert scheduler._fleet_latched() is False

    def test_fleet_latched_false_when_not_set(self, tmp_path):
        (tmp_path / ".git").mkdir(exist_ok=True)
        from kew.run_store import SqliteRunStore

        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        (tmp_path / "logs").mkdir(parents=True)
        with SqliteRunStore(str(tmp_path / "logs" / "dispatch.db")):
            pass
        scheduler = DispatchScheduler(config=config)
        assert scheduler._fleet_latched() is False

    @patch("kew.scheduler.ProcessPoolExecutor")
    def test_run_loop_exits_when_latched(self, mock_pool, tmp_path):
        (tmp_path / ".git").mkdir(exist_ok=True)
        from kew.run_store import SqliteRunStore

        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        config.overlap.strategy = "off"
        (tmp_path / "logs").mkdir(parents=True)
        with SqliteRunStore(str(tmp_path / "logs" / "dispatch.db")) as store:
            store.set_fleet_stopped("halt")

        scheduler = DispatchScheduler(config=config)
        with patch.object(scheduler, "select_available_issues") as mock_select:
            scheduler.run_loop()  # must exit promptly without selecting

        mock_select.assert_not_called()

    @patch("kew.scheduler.ProcessPoolExecutor")
    def test_run_loop_breaker_trip_stops_dispatch_same_iteration(
        self, mock_pool, tmp_path
    ):
        """When _check_fleet_breaker latches mid-iteration, no new work is claimed
        in that same pass — resolve/selection/dispatch are all skipped (#245)."""
        (tmp_path / ".git").mkdir(exist_ok=True)
        from kew.run_store import SqliteRunStore

        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        config.overlap.strategy = "off"
        (tmp_path / "logs").mkdir(parents=True)
        db = str(tmp_path / "logs" / "dispatch.db")
        with SqliteRunStore(db):  # create the db so _fleet_latched can read it
            pass

        scheduler = DispatchScheduler(config=config)

        def latch_the_fleet():
            with SqliteRunStore(db) as store:
                store.set_fleet_stopped("circuit breaker: daily spend exceeded")

        with (
            patch.object(
                scheduler, "_check_fleet_breaker", side_effect=latch_the_fleet
            ),
            patch.object(scheduler, "_dispatch_conflicting_resolves") as mock_resolve,
            patch.object(scheduler, "select_available_issues") as mock_select,
            patch.object(scheduler, "_dispatch_batch") as mock_batch,
        ):
            scheduler.run_loop()

        mock_resolve.assert_not_called()
        mock_select.assert_not_called()
        mock_batch.assert_not_called()


class TestSchedulerFleetBreaker:
    """A live daily/weekly spend breach engages the fleet stop (#245)."""

    def _scheduler_with_logs(self, tmp_path):
        from kew.config import DispatchConfig, ParallelConfig
        from kew.scheduler import DispatchScheduler

        config = DispatchConfig(parallel=ParallelConfig(max_workers=1))
        config.logging.dir = str(tmp_path / "logs")
        (tmp_path / "logs").mkdir(parents=True)
        return DispatchScheduler(config)

    def test_fleet_breaker_latches_on_daily_breach(self, tmp_path):
        (tmp_path / ".git").mkdir(exist_ok=True)
        from datetime import UTC, datetime

        from kew.run_store import RunRecord, SqliteRunStore

        scheduler = self._scheduler_with_logs(tmp_path)
        db = str(tmp_path / "logs" / "dispatch.db")
        with SqliteRunStore(db) as store:
            # daily_usd default is 50.0; persist $60 of completed spend today
            store.save(
                RunRecord(
                    run_id="r1",
                    issue_num=1,
                    branch="b",
                    status="success",
                    exit_code=0,
                    started_at=datetime.now(UTC),
                    estimated_cost_usd=60.0,
                )
            )

        scheduler._check_fleet_breaker()

        with SqliteRunStore(db) as store:
            assert store.get_fleet_stopped() is not None
            events = store.list_audit_events(event="circuit_breaker")
        assert len(events) == 1
        assert events[0].payload["scope"] == "fleet"
        assert events[0].payload["rule"] == "daily_breach"
        assert events[0].actor_type == "policy"

    def test_fleet_breaker_no_latch_when_within_budget(self, tmp_path):
        (tmp_path / ".git").mkdir(exist_ok=True)
        from datetime import UTC, datetime

        from kew.run_store import RunRecord, SqliteRunStore

        scheduler = self._scheduler_with_logs(tmp_path)
        db = str(tmp_path / "logs" / "dispatch.db")
        with SqliteRunStore(db) as store:
            store.save(
                RunRecord(
                    run_id="r1",
                    issue_num=1,
                    branch="b",
                    status="success",
                    exit_code=0,
                    started_at=datetime.now(UTC),
                    estimated_cost_usd=5.0,
                )
            )

        scheduler._check_fleet_breaker()

        with SqliteRunStore(db) as store:
            assert store.get_fleet_stopped() is None


class TestBuildOverlapIndexes:
    """_build_overlap_indexes uses git ls-files, not a filesystem walk."""

    def _fake_git_ls_files(self, stdout: str):
        """Return a mock subprocess result for git ls-files."""
        result = MagicMock(spec=subprocess.CompletedProcess)
        result.stdout = stdout
        result.returncode = 0
        return result

    def test_excludes_mypy_cache_includes_source(self):
        git_output = "src/kew/config.py\nsrc/kew/scheduler.py\npyproject.toml\n"
        with patch(
            "kew.subprocess_utils.run", return_value=self._fake_git_ls_files(git_output)
        ):
            files, mod_idx, ident_idx = DispatchScheduler._build_overlap_indexes()

        assert "src/kew/config.py" in files
        assert "src/kew/scheduler.py" in files
        assert "pyproject.toml" in files
        # .mypy_cache files never appear because git ls-files doesn't list them
        for f in files:
            assert ".mypy_cache" not in f

    def test_filters_by_suffix(self):
        git_output = "src/kew/config.py\nREADME.md\nlogo.png\npackage-lock.json\n"
        with patch(
            "kew.subprocess_utils.run", return_value=self._fake_git_ls_files(git_output)
        ):
            files, _, _ = DispatchScheduler._build_overlap_indexes()

        assert "src/kew/config.py" in files
        assert "README.md" in files
        assert "package-lock.json" in files
        assert "logo.png" not in files

    def test_fallback_on_git_failure(self):
        with patch("kew.subprocess_utils.run", side_effect=Exception("not a git repo")):
            files, mod_idx, ident_idx = DispatchScheduler._build_overlap_indexes()

        assert files == []
        assert mod_idx == {}
        assert ident_idx == {}


@pytest.mark.parametrize("claim", [ClaimResult.FETCH_FAILED, ClaimResult.RACE_DETECTED])
def test_drain_retains_potentially_stranded_claims(claim, capsys):
    config = DispatchConfig()
    config.overlap.strategy = "off"
    scheduler = DispatchScheduler(config)
    issues = [
        {"number": n, "title": "test", "body": "", "labels": []} for n in (42, 43)
    ]
    with (
        patch("kew.scheduler.ProcessPoolExecutor") as pool,
        patch.object(scheduler, "_slots_for_pass", return_value=2),
        patch.object(
            scheduler, "select_available_issues", side_effect=[issues, [], [], []]
        ),
        patch("kew.scheduler.remote_branch_exists", return_value=False),
        patch("kew.scheduler.try_claim_issue", return_value=claim),
        patch("kew.scheduler.rollback_claim") as rollback,
        patch("kew.scheduler._update_labels") as labels,
        patch("kew.scheduler.time.sleep"),
    ):
        drain = scheduler.run_loop()
    assert getattr(drain, "claim_failures", None) == ((42, claim), (43, claim))
    output = capsys.readouterr().err
    for number in (42, 43):
        assert f"Potentially stranded claim: issue #{number} ({claim.value})" in output
    assert "Queue empty after 3 checks" not in output
    pool.return_value.submit.assert_not_called()
    rollback.assert_not_called()
    labels.assert_not_called()


def test_round_four_clean_drain_retains_both_worker_results():
    from concurrent.futures import Future

    from kew.command_outcome import CommandOutcome

    config = DispatchConfig()
    config.overlap.strategy = "off"
    scheduler = DispatchScheduler(config)
    dispatch = Future()
    dispatch_result = DispatchOutcome(0)
    dispatch.set_result(dispatch_result)
    resolve = Future()
    resolve_result = CommandOutcome("executed", 0)
    resolve.set_result(resolve_result)

    def slots(pool, futures, resolve_futures, **kwargs):
        futures[dispatch] = 42
        resolve_futures[resolve] = 87
        return None

    with (
        patch.object(scheduler, "_slots_for_pass", side_effect=slots),
        patch("kew.scheduler.ProcessPoolExecutor"),
    ):
        drain = scheduler.run_loop()
    assert getattr(drain, "dispatch_outcomes", None) == ((42, dispatch_result),)
    assert drain.resolve_outcomes == ((87, resolve_result),)
    assert drain.required_failures == ()


def test_round_five_claim_failure_stops_reselection():
    from kew.cli import _drain_exit_code

    config = DispatchConfig()
    config.overlap.strategy = "off"
    scheduler = DispatchScheduler(config)
    issue = {"number": 42, "title": "ready", "body": "", "labels": []}

    def claim(*args):
        # Bound the broken implementation so the regression cannot hang.
        if claims.call_count >= 5:
            scheduler._shutdown = True
        return ClaimResult.FETCH_FAILED

    with (
        patch("kew.scheduler.ProcessPoolExecutor"),
        patch.object(scheduler, "_slots_for_pass", return_value=1),
        patch.object(scheduler, "select_available_issues", return_value=[issue]),
        patch("kew.scheduler.remote_branch_exists", return_value=False),
        patch("kew.scheduler.try_claim_issue", side_effect=claim) as claims,
        patch("kew.scheduler.time.sleep"),
    ):
        drain = scheduler.run_loop()
    assert claims.call_count <= 2
    assert _drain_exit_code(drain) == 2
    assert drain.claim_failures[0] == (42, ClaimResult.FETCH_FAILED)


def test_round_five_hold_release_failure_is_retained():
    from concurrent.futures import Future

    from kew.cli import _drain_exit_code

    config = DispatchConfig()
    config.overlap.strategy = "off"
    scheduler = DispatchScheduler(config)
    completed = Future()
    completed.set_result(DispatchOutcome(0))
    scheduler.coordinator.add_hold(43, blocking_issues={42}, overlap_files=set())

    def slots(pool, futures, resolve_futures, **kwargs):
        futures[completed] = 42
        return None

    with (
        patch("kew.scheduler.ProcessPoolExecutor"),
        patch.object(scheduler, "_slots_for_pass", side_effect=slots),
        patch("kew.scheduler._update_labels", side_effect=RuntimeError("HTTP 403")),
        patch.object(scheduler, "_check_fleet_breaker"),
    ):
        drain = scheduler.run_loop()
    assert _drain_exit_code(drain) == 2
    assert drain.required_failures == (("Release overlap hold #43", "HTTP 403"),)
    assert drain.dispatch_outcomes == ((42, completed.result()),)


@pytest.mark.parametrize("worker", ["dispatch", "resolve"])
def test_round_five_unfinished_workers_fail_drain(worker):
    from concurrent.futures import Future

    from kew.cli import _drain_exit_code

    config = DispatchConfig()
    config.overlap.strategy = "off"
    config.parallel.shutdown_grace_seconds = 0
    scheduler = DispatchScheduler(config)
    pending = Future()

    def slots(pool, futures, resolve_futures, **kwargs):
        target = futures if worker == "dispatch" else resolve_futures
        target[pending] = 42
        return None

    with (
        patch("kew.scheduler.ProcessPoolExecutor"),
        patch.object(scheduler, "_slots_for_pass", side_effect=slots),
    ):
        drain = scheduler.run_loop()
    assert _drain_exit_code(drain) == 2
    label = "Issue #42" if worker == "dispatch" else "Resolve PR #42"
    assert drain.required_failures == ((label, "worker exceeded shutdown grace"),)


def test_round_five_hold_application_failure_is_retained():
    from kew.cli import _drain_exit_code
    from kew.overlap import OverlapDecision

    config = DispatchConfig()
    config.overlap.strategy = "off"
    scheduler = DispatchScheduler(config)

    def slots(pool, futures, resolve_futures, **kwargs):
        scheduler._apply_overlap_hold(
            43, OverlapDecision(action="hold", reason="overlap")
        )
        return None

    with (
        patch("kew.scheduler.ProcessPoolExecutor"),
        patch.object(scheduler, "_slots_for_pass", side_effect=slots),
        patch("kew.scheduler._update_labels", side_effect=RuntimeError("HTTP 403")),
    ):
        drain = scheduler.run_loop()
    assert _drain_exit_code(drain) == 2
    assert drain.required_failures == (("Apply overlap hold #43", "HTTP 403"),)


@pytest.mark.parametrize("failure", ["exception", "returncode", "query"])
def test_round_six_startup_hold_cleanup_retains_failures(failure):
    from kew.cli import _drain_exit_code
    from kew.selector import GhQueryError

    for failing in (True, False):
        config = DispatchConfig()
        config.overlap.strategy = "serialize"
        scheduler = DispatchScheduler(config)
        with (
            patch("kew.scheduler.ProcessPoolExecutor"),
            patch.object(scheduler, "_slots_for_pass", return_value=1),
            patch.object(scheduler, "select_available_issues", return_value=[]),
            patch.object(
                scheduler, "_build_overlap_indexes", return_value=([], {}, {})
            ),
            patch("kew.scheduler.time.sleep"),
            patch("kew.overlap._run_gh_json", return_value=[{"number": 42}]) as query,
            patch("kew.overlap.run", return_value=MagicMock(returncode=0)) as remove,
        ):
            if failing:
                if failure == "query":
                    query.side_effect = GhQueryError("HTTP 503")
                elif failure == "exception":
                    remove.side_effect = GhQueryError("HTTP 403")
                else:
                    remove.return_value = MagicMock(returncode=1, stderr="HTTP 403")
            drain = scheduler.run_loop()
        label = (
            "List stale overlap holds"
            if failure == "query"
            else "Strip stale overlap hold #42"
        )
        assert tuple(name for name, _ in drain.required_failures) == (
            (label,) if failing else ()
        )
        if failing:
            assert "HTTP" in drain.required_failures[0][1]
        assert _drain_exit_code(drain) == (2 if failing else 0)


@pytest.mark.parametrize("failure", ["resolve", "dispatch", "rollback", "audit"])
def test_round_seven_submission_failures_survive_drain(failure):
    from concurrent.futures import Future

    from kew.cli import _drain_exit_code
    from kew.dispatch_core import DispatchOutcome

    config = DispatchConfig()
    config.overlap.strategy = "off"
    config.resolve.auto = True
    scheduler = DispatchScheduler(config, store=MagicMock())
    completed = Future()
    completed.set_result(DispatchOutcome(0, launched=True))
    selected = {"number": 42, "title": "ready", "body": "", "labels": []}

    def slots(pool, futures, resolve_futures, **kwargs):
        # A tracked worker must still drain after another submission fails.
        futures[completed] = 41
        if failure == "resolve":
            scheduler._dispatch_conflicting_resolves(pool, resolve_futures)
        return 1

    with (
        patch("kew.scheduler.ProcessPoolExecutor") as executor,
        patch.object(scheduler, "_slots_for_pass", side_effect=slots),
        patch.object(scheduler, "has_capacity", return_value=True),
        patch.object(scheduler, "_poll_conflicting_prs", return_value=[selected]),
        patch.object(
            scheduler, "select_available_issues", side_effect=[[selected], [], [], []]
        ),
        patch.object(scheduler, "_build_merge_context", return_value=None),
        patch("kew.scheduler.remote_branch_exists", return_value=False),
        patch("kew.scheduler.try_claim_issue", return_value=ClaimResult.CLAIMED),
        patch(
            "kew.scheduler.rollback_claim",
            side_effect=RuntimeError("rollback failed")
            if failure == "rollback"
            else None,
        ),
        patch(
            "kew.scheduler.audit.emit",
            side_effect=RuntimeError("audit failed") if failure == "audit" else None,
        ),
        patch("kew.scheduler.time.sleep"),
    ):
        executor.return_value.submit.side_effect = RuntimeError("pool unavailable")
        drain = scheduler.run_loop()
    names = [name for name, detail in drain.required_failures]
    assert (
        "Submit resolve PR #42" if failure == "resolve" else "Submit issue #42"
    ) in names
    if failure in {"rollback", "audit"}:
        label = (
            "Rollback claim #42"
            if failure == "rollback"
            else "Record claim rollback #42"
        )
        assert label in names
    assert _drain_exit_code(drain) == 2
    assert (41, completed.result()) in drain.dispatch_outcomes


@pytest.mark.parametrize("submission", ["dispatch", "resolve"])
@pytest.mark.parametrize(
    "error", [OSError(24, "Too many open files"), ValueError("executor refused")]
)
def test_round_nine_submission_exceptions_drain_both_workers(submission, error):
    from concurrent.futures import Future

    from kew.cli import _drain_exit_code
    from kew.command_outcome import CommandOutcome
    from kew.dispatch_core import DispatchOutcome

    config = DispatchConfig()
    config.overlap.strategy = "off"
    config.resolve.auto = True
    scheduler = DispatchScheduler(config, store=MagicMock())
    dispatch = Future()
    dispatch.set_result(DispatchOutcome(1, launched=True))
    resolve = Future()
    resolve.set_result(CommandOutcome("executed", 1))
    selected = {"number": 42, "title": "ready", "body": "", "labels": []}

    def slots(pool, futures, resolve_futures, **kwargs):
        futures[dispatch] = 40
        resolve_futures[resolve] = 41
        scheduler.in_flight.add(40)
        scheduler.resolve_in_flight.add(41)
        if submission == "resolve":
            scheduler._dispatch_conflicting_resolves(pool, resolve_futures)
            return None
        return 1

    escaped = None
    with (
        patch("kew.scheduler.ProcessPoolExecutor") as executor,
        patch.object(scheduler, "_slots_for_pass", side_effect=slots),
        patch.object(scheduler, "has_capacity", return_value=True),
        patch.object(scheduler, "_poll_conflicting_prs", return_value=[selected]),
        patch.object(scheduler, "select_available_issues", return_value=[selected]),
        patch.object(scheduler, "_build_merge_context", return_value=None),
        patch.object(scheduler, "_check_fleet_breaker"),
        patch("kew.scheduler.remote_branch_exists", return_value=False),
        patch(
            "kew.scheduler.try_claim_issue", return_value=ClaimResult.CLAIMED
        ) as claim,
        patch("kew.scheduler.rollback_claim") as rollback,
        patch("kew.scheduler.audit.emit"),
    ):
        executor.return_value.submit.side_effect = error
        try:
            drain = scheduler.run_loop()
        except Exception as exc:
            escaped = exc
    assert escaped is None, f"submission exception escaped: {escaped}"
    label = "Submit issue #42" if submission == "dispatch" else "Submit resolve PR #42"
    assert drain.required_failures == ((label, str(error)),)
    assert drain.dispatch_outcomes == ((40, dispatch.result()),)
    assert drain.resolve_outcomes == ((41, resolve.result()),)
    assert not scheduler.in_flight
    assert not scheduler.resolve_in_flight
    assert _drain_exit_code(drain) == 2
    if submission == "dispatch":
        claim.assert_called_once_with(42, config)
        rollback.assert_called_once_with(42, config)
    else:
        claim.assert_not_called()
        rollback.assert_not_called()


def test_round_ten_selection_failure_drains_active_workers():
    from concurrent.futures import Future

    from kew.cli import _drain_exit_code
    from kew.command_outcome import CommandOutcome
    from kew.dispatch_core import DispatchOutcome
    from kew.selector import GhQueryError

    config = DispatchConfig()
    config.overlap.strategy = "off"
    scheduler = DispatchScheduler(config, store=MagicMock())
    dispatch, resolve = Future(), Future()
    selected = {"number": 42, "title": "ready", "body": "", "labels": []}
    passes = [0]

    def slots(pool, futures, resolve_futures, **kwargs):
        passes[0] += 1
        if passes[0] == 1:
            resolve_futures[resolve] = 43
            scheduler.resolve_in_flight.add(43)
        return 1

    def selection(**kwargs):
        if passes[0] == 1:
            return [selected]
        raise GhQueryError("selection unavailable")

    def finish(_seconds):
        dispatch.set_result(
            DispatchOutcome(1, changed_files=["src/a.py"], launched=True)
        )
        resolve.set_result(CommandOutcome("executed", 1))

    escaped = None
    with (
        patch("kew.scheduler.ProcessPoolExecutor") as executor,
        patch.object(scheduler, "_slots_for_pass", side_effect=slots),
        patch.object(scheduler, "select_available_issues", side_effect=selection),
        patch.object(scheduler, "has_capacity", return_value=True),
        patch.object(scheduler, "_check_fleet_breaker"),
        patch.object(scheduler, "_build_merge_context", return_value=None),
        patch("kew.scheduler.remote_branch_exists", return_value=False),
        patch("kew.scheduler.try_claim_issue", return_value=ClaimResult.CLAIMED),
        patch.object(scheduler.coordinator, "on_complete") as release,
        patch("kew.scheduler.time.sleep", side_effect=finish) as sleep,
    ):
        executor.return_value.submit.return_value = dispatch
        try:
            drain = scheduler.run_loop()
        except GhQueryError as exc:
            escaped = exc
    assert escaped is None, str(escaped)
    assert drain.required_failures == (("Selection query", "selection unavailable"),)
    assert drain.dispatch_outcomes == ((42, dispatch.result()),)
    assert drain.resolve_outcomes == ((43, resolve.result()),)
    release.assert_called_once_with(42, ["src/a.py"])
    assert sleep.called
    assert not scheduler.in_flight and not scheduler.resolve_in_flight
    assert _drain_exit_code(drain) == 2
