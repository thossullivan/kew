from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from kew.run_store import RunRecord, SqliteRunStore


class TestTuiState:
    def test_construction(self, tmp_path):
        from kew.config import DispatchConfig
        from kew.tui.state import TuiState

        store = SqliteRunStore(str(tmp_path / "runs.db"))
        config = DispatchConfig()
        state = TuiState(config=config, store=store)

        assert state.pending_runs == []
        assert state.recent_runs == []
        assert state.daily_spend == 0.0
        assert state.queue_ready == []
        assert state.queue_failed == []
        assert state.queue_backlog == []

    def test_refresh_local_loads_pending_and_recent(self, tmp_path):
        from kew.config import DispatchConfig
        from kew.tui.state import TuiState

        store = SqliteRunStore(str(tmp_path / "runs.db"))
        now = datetime.now(UTC)

        store.save(
            RunRecord(
                run_id="r1",
                issue_num=1,
                branch="b1",
                status="pending",
                exit_code=-1,
                started_at=now,
                runner_name="claude-code",
                model="claude-sonnet-4",
            )
        )
        store.save(
            RunRecord(
                run_id="r2",
                issue_num=2,
                branch="b2",
                status="success",
                exit_code=0,
                started_at=now,
                estimated_cost_usd=2.50,
                runner_name="claude-code",
                model="claude-sonnet-4",
            )
        )

        config = DispatchConfig()
        state = TuiState(config=config, store=store)
        state.refresh_local()

        assert len(state.pending_runs) == 1
        assert state.pending_runs[0].run_id == "r1"
        assert len(state.recent_runs) == 1
        assert state.recent_runs[0].run_id == "r2"
        assert state.daily_spend == pytest.approx(2.50)

    def test_progress_estimate_with_history(self, tmp_path):
        from kew.config import DispatchConfig
        from kew.tui.state import TuiState

        store = SqliteRunStore(str(tmp_path / "runs.db"))
        now = datetime.now(UTC)

        for i, tokens in enumerate([10000, 20000, 30000, 40000, 50000]):
            store.save(
                RunRecord(
                    run_id=f"r{i}",
                    issue_num=i,
                    branch=f"b{i}",
                    status="success",
                    exit_code=0,
                    started_at=now,
                    total_tokens=tokens,
                    runner_name="claude-code",
                    model="claude-sonnet-4",
                )
            )

        state = TuiState(config=DispatchConfig(), store=store)
        state.refresh_local()

        expected = state.expected_tokens("medium")
        assert expected == 40000

    def test_progress_estimate_no_history(self, tmp_path):
        from kew.config import DispatchConfig
        from kew.tui.state import TuiState

        store = SqliteRunStore(str(tmp_path / "runs.db"))
        state = TuiState(config=DispatchConfig(), store=store)
        state.refresh_local()

        assert state.expected_tokens("medium") == 100_000

    def test_recent_runs_sorted_by_most_recently_completed(self, tmp_path):
        """recent_runs sorted by finished_at desc (most recent first)."""
        from kew.config import DispatchConfig
        from kew.tui.state import TuiState

        store = SqliteRunStore(str(tmp_path / "runs.db"))
        # Freeze clock at noon UTC so all records stay within the same UTC day
        frozen = datetime(2025, 6, 15, 12, 0, 0, tzinfo=UTC)

        store.save(
            RunRecord(
                run_id="early",
                issue_num=1,
                branch="b1",
                status="success",
                exit_code=0,
                started_at=frozen - timedelta(minutes=12),
                finished_at=frozen - timedelta(minutes=8),
                duration_seconds=240,
                runner_name="claude-code",
                model="claude-sonnet-4",
            )
        )
        store.save(
            RunRecord(
                run_id="late",
                issue_num=2,
                branch="b2",
                status="error",
                exit_code=1,
                started_at=frozen - timedelta(minutes=8),
                finished_at=frozen - timedelta(minutes=2),
                duration_seconds=360,
                runner_name="claude-code",
                model="claude-sonnet-4",
            )
        )
        store.save(
            RunRecord(
                run_id="middle",
                issue_num=3,
                branch="b3",
                status="success",
                exit_code=0,
                started_at=frozen - timedelta(minutes=5),
                finished_at=frozen - timedelta(minutes=4),
                duration_seconds=60,
                runner_name="claude-code",
                model="claude-sonnet-4",
            )
        )

        with patch("kew.tui.state.datetime") as mock_dt:
            mock_dt.now.return_value = frozen
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            state = TuiState(config=DispatchConfig(), store=store)
            state.refresh_local()

        assert len(state.recent_runs) == 3
        assert state.recent_runs[0].run_id == "late"
        assert state.recent_runs[1].run_id == "middle"
        assert state.recent_runs[2].run_id == "early"


class TestEventFileTailing:
    def test_read_worker_events(self, tmp_path):
        from kew.tui.state import read_worker_events

        event_file = tmp_path / "dispatch-42-20260321-120000.events.jsonl"
        event_file.write_text(
            '{"ts": 1.0, "type": "started",'
            ' "runner": "claude-code", "model": "claude-sonnet-4", "issue": 42}\n'
            '{"ts": 2.0, "type": "tokens",'
            ' "input": 1200, "output": 340, "cost_usd": 0.02}\n'
            '{"ts": 3.0, "type": "activity", "msg": "Reading config.py"}\n'
        )

        events = read_worker_events(event_file)
        assert len(events) == 3
        assert events[0]["type"] == "started"
        assert events[-1]["msg"] == "Reading config.py"

    def test_read_worker_events_missing_file(self, tmp_path):
        from kew.tui.state import read_worker_events

        result = read_worker_events(tmp_path / "nonexistent.events.jsonl")
        assert result == []

    def test_refresh_workers_populates_snapshots(self, tmp_path):
        from kew.config import DispatchConfig
        from kew.tui.state import TuiState

        store = SqliteRunStore(str(tmp_path / "runs.db"))
        now = datetime.now(UTC)

        log_file = tmp_path / "dispatch-99-20260322-090000.log"
        log_file.touch()
        event_file = log_file.with_suffix(".events.jsonl")
        event_file.write_text(
            '{"ts": 1.0, "type": "tokens",'
            ' "input": 800, "output": 200, "cost_usd": 0.05}\n'
            '{"ts": 2.0, "type": "activity", "msg": "Editing main.py"}\n'
        )

        store.save(
            RunRecord(
                run_id="run-99",
                issue_num=99,
                branch="fix/issue-99",
                status="pending",
                exit_code=-1,
                started_at=now,
                log_file=log_file,
                runner_name="claude-code",
                model="claude-sonnet-4",
            )
        )

        state = TuiState(config=DispatchConfig(), store=store)
        state.refresh_local()
        state.refresh_workers()

        assert "run-99" in state.worker_snapshots
        snap = state.worker_snapshots["run-99"]
        assert snap["input_tokens"] == 800
        assert snap["output_tokens"] == 200
        assert snap["total_tokens"] == 1000
        assert snap["cost_usd"] == pytest.approx(0.05)
        assert snap["last_activity"] == "Editing main.py"

    def test_worker_snapshot_from_events(self, tmp_path):
        from kew.tui.state import worker_snapshot_from_events

        events = [
            {
                "ts": 1.0,
                "type": "started",
                "runner": "claude-code",
                "model": "claude-sonnet-4",
                "issue": 42,
            },
            {
                "ts": 2.0,
                "type": "tokens",
                "input": 1200,
                "output": 340,
                "cost_usd": 0.02,
            },
            {"ts": 3.0, "type": "activity", "msg": "Reading config.py"},
            {
                "ts": 4.0,
                "type": "tokens",
                "input": 5000,
                "output": 1200,
                "cost_usd": 0.10,
            },
        ]
        snap = worker_snapshot_from_events(events)

        assert snap["input_tokens"] == 5000
        assert snap["output_tokens"] == 1200
        assert snap["cost_usd"] == pytest.approx(0.10)
        assert snap["last_activity"] == "Reading config.py"
        assert snap["total_tokens"] == 6200


def test_worker_snapshot_extracts_pid():
    from kew.tui.state import worker_snapshot_from_events

    events = [
        {"type": "started", "runner": "claude-code", "model": "sonnet"},
        {"type": "pid", "pid": 12345},
        {"type": "tokens", "input": 100, "output": 50, "cost_usd": 0.01},
    ]
    snap = worker_snapshot_from_events(events)
    assert snap["pid"] == 12345


def test_tui_state_has_filter_text():
    from kew.tui.state import TuiState

    state = TuiState(config=MagicMock(), store=MagicMock())
    assert state.filter_text == {}


def test_active_worker_cost_zero_when_no_snapshots():
    """active_worker_cost returns 0.0 when there are no worker snapshots."""
    from kew.tui.state import TuiState

    state = TuiState(config=MagicMock(), store=MagicMock())
    assert state.active_worker_cost() == 0.0


def test_active_worker_cost_sums_snapshot_costs():
    """active_worker_cost sums cost_usd from all worker snapshots."""
    from kew.tui.state import TuiState

    state = TuiState(config=MagicMock(), store=MagicMock())
    state.worker_snapshots = {
        "run-1": {"total_tokens": 50000, "cost_usd": 1.20},
        "run-2": {"total_tokens": 30000, "cost_usd": 0.80},
    }
    assert state.active_worker_cost() == pytest.approx(2.00)


def test_filter_matches_case_insensitive():
    """Filter text matches case-insensitively against formatted row."""
    from kew.tui.state import matches_filter

    assert matches_filter("P0 #142 Fix auth regression", "auth") is True
    assert matches_filter("P0 #142 Fix auth regression", "AUTH") is True
    assert matches_filter("P0 #142 Fix auth regression", "deploy") is False


def _make_run(cost, started_at, status="success"):
    r = MagicMock()
    r.estimated_cost_usd = cost
    r.started_at = started_at
    r.status = status
    return r


def test_weekly_spend():
    from kew.tui.state import TuiState

    state = TuiState(config=MagicMock(), store=MagicMock())
    now = datetime.now(UTC)
    state._all_runs = [
        _make_run(5.0, now - timedelta(days=1)),
        _make_run(3.0, now - timedelta(days=5)),
        _make_run(10.0, now - timedelta(days=10)),  # outside 7-day window
    ]
    assert state.weekly_spend() == 8.0


def test_per_issue_avg_no_runs():
    from kew.tui.state import TuiState

    state = TuiState(config=MagicMock(), store=MagicMock())
    state.daily_spend = 0.0
    state.recent_runs = []
    assert state.per_issue_avg() == 0.0


def test_burn_rate(monkeypatch):
    from kew.tui.state import TuiState

    # Freeze time to noon UTC so burn_rate = daily_spend / 12 hours
    frozen = datetime(2026, 3, 22, 12, 0, 0, tzinfo=UTC)
    monkeypatch.setattr(
        "kew.tui.state.datetime",
        type(
            "dt",
            (),
            {
                "now": staticmethod(lambda tz=None: frozen),
            },
        ),
    )
    state = TuiState(config=MagicMock(), store=MagicMock())
    state.daily_spend = 10.0
    rate = state.burn_rate()
    assert abs(rate - 10.0 / 12.0) < 0.01


def test_refresh_remote_resolves_routing(monkeypatch):
    from kew.tui.state import TuiState

    state = TuiState(config=MagicMock(), store=MagicMock())
    mock_ready = [{"number": 1, "title": "Fix", "labels": []}]
    monkeypatch.setattr(
        "kew.tui.state.list_queue_issues", lambda config: (mock_ready, [], [])
    )
    monkeypatch.setattr("kew.tui.state.list_review_prs", lambda config: [])
    mock_route = MagicMock()
    mock_route.runner.name = "claude-code"
    mock_route.model = "claude-sonnet-4"
    monkeypatch.setattr(
        "kew.tui.state.resolve_runner", lambda config, complexity: mock_route
    )
    state.refresh_remote()
    assert state.queue_ready[0]["_routed_runner"] == "claude-code"
    assert state.queue_ready[0]["_routed_model"] == "claude-sonnet-4"


def test_tail_events_reads_from_offset(tmp_path):
    """tail_events reads only new events starting from offset."""
    from kew.tui.state import tail_events

    path = tmp_path / "test.events.jsonl"
    path.write_bytes(
        b'{"type": "started", "ts": 1.0}\n'
        b'{"type": "tokens", "ts": 2.0, "input": 100, "output": 50}\n'
    )
    # First read: gets both events
    events, offset = tail_events(path)
    assert len(events) == 2
    assert offset > 0

    # Append a new event
    with open(path, "ab") as f:
        f.write(b'{"type": "finished", "ts": 3.0}\n')

    # Second read from offset: gets only the new event
    new_events, new_offset = tail_events(path, offset)
    assert len(new_events) == 1
    assert new_events[0]["type"] == "finished"
    assert new_offset > offset


def test_tail_events_handles_missing_file(tmp_path):
    """tail_events returns empty list for missing file."""
    from kew.tui.state import tail_events

    path = tmp_path / "nonexistent.jsonl"
    events, offset = tail_events(path)
    assert events == []
    assert offset == 0


def test_tail_events_handles_partial_line(tmp_path):
    """tail_events does not advance past a partial (unparseable) line."""
    from kew.tui.state import tail_events

    path = tmp_path / "test.events.jsonl"
    # Write one complete line and one partial line (no trailing newline)
    path.write_bytes(
        b'{"type": "started", "ts": 1.0}\n{"type": "tok'  # partial — writer mid-flush
    )
    events, offset = tail_events(path)
    assert len(events) == 1
    assert events[0]["type"] == "started"

    # Simulate writer completing the line
    with open(path, "ab") as f:
        f.write(b'ens", "ts": 2.0, "input": 100}\n')

    # Next read picks up the completed line
    new_events, new_offset = tail_events(path, offset)
    assert len(new_events) == 1
    assert new_events[0]["type"] == "tokens"


def test_tail_events_empty_file(tmp_path):
    """tail_events handles an empty file."""
    from kew.tui.state import tail_events

    path = tmp_path / "empty.jsonl"
    path.write_bytes(b"")
    events, offset = tail_events(path)
    assert events == []
    assert offset == 0


def test_tail_events_skips_malformed_complete_line(tmp_path):
    """tail_events skips a malformed but newline-terminated line and continues."""
    from kew.tui.state import tail_events

    path = tmp_path / "test.events.jsonl"
    path.write_bytes(
        b'{"type": "started", "ts": 1.0}\n'
        b"not valid json\n"
        b'{"type": "finished", "ts": 3.0}\n'
    )
    events, offset = tail_events(path)
    assert len(events) == 2
    assert events[0]["type"] == "started"
    assert events[1]["type"] == "finished"

    # Offset should be past all three lines — no re-read loop
    new_events, new_offset = tail_events(path, offset)
    assert new_events == []
    assert new_offset == offset


class TestTuiStateReviewPrs:
    def test_review_prs_initialized_empty(self, tmp_path):
        from kew.config import DispatchConfig
        from kew.tui.state import TuiState

        store = SqliteRunStore(str(tmp_path / "runs.db"))
        state = TuiState(config=DispatchConfig(), store=store)
        assert state.review_prs == []

    @patch("kew.tui.state.list_review_prs")
    @patch("kew.tui.state.list_queue_issues", return_value=([], [], []))
    def test_refresh_remote_populates_review_prs(
        self, mock_queue: MagicMock, mock_review: MagicMock, tmp_path
    ):
        from kew.config import DispatchConfig
        from kew.tui.state import TuiState

        mock_review.return_value = [
            {
                "number": 87,
                "title": "Fix widget",
                "author": {"login": "alice"},
                "createdAt": "2026-03-22T10:00:00Z",
                "comments": [],
            },
        ]
        store = SqliteRunStore(str(tmp_path / "runs.db"))
        state = TuiState(config=DispatchConfig(), store=store)
        state.refresh_remote()

        assert len(state.review_prs) == 1
        assert state.review_prs[0]["number"] == 87


def test_refresh_local_populates_queue_held(tmp_path):
    from datetime import UTC, datetime

    from kew.config import DispatchConfig, LoggingConfig, ProjectConfig
    from kew.run_store import SqliteRunStore
    from kew.tui.state import TuiState

    config = DispatchConfig(
        project=ProjectConfig(repo="o/r"), logging=LoggingConfig(dir=str(tmp_path))
    )
    db = tmp_path / "d.db"
    with SqliteRunStore(str(db)) as store:
        store.append_audit_event(
            timestamp=datetime.now(UTC).isoformat(),
            event="approval_hold",
            issue_num=42,
            payload={"estimate_usd": 3.1, "basis": "median of last-10"},
        )
        state = TuiState(config=config, store=store)
        state.refresh_local()
        assert [h["number"] for h in state.queue_held] == [42]
        assert state.queue_held[0]["estimate_usd"] == 3.1
        assert state.queue_held[0]["basis"] == "median of last-10"


class TestTuiStateFleetStopped:
    def test_refresh_local_picks_up_latch(self, tmp_path):
        from kew.config import DispatchConfig
        from kew.run_store import SqliteRunStore
        from kew.tui.state import TuiState

        config = DispatchConfig()
        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            state = TuiState(config=config, store=store)
            assert state.fleet_stopped is None
            state.refresh_local()
            assert state.fleet_stopped is None

            store.set_fleet_stopped("halt")
            state.refresh_local()
            assert state.fleet_stopped is not None
            assert state.fleet_stopped["reason"] == "halt"

            store.clear_fleet_stopped()
            state.refresh_local()
            assert state.fleet_stopped is None


# --- I22: the TUI consumes the canonical work projection --------------------
#
# Before this slice the TUI decided what a work item *was* from
# ``RunRecord.status`` and displayed it as ``#{issue_num}``. Both are lies for
# any item the numeric shim cannot name (SFD-12 -> "#0") and for any run whose
# implementation is preserved but unpublished ("awaiting_publication" -> ERR,
# retry). ``TuiState`` now retains ``kew.service.work``'s revisions and every
# terminal surface renders them through the ONE mapper below.


def _admit_run(store, *, run_id, item_id, issue_num, tracker="github", **overrides):
    """Admit one run through the identity-bearing admission path."""
    from kew.run_store import AdmissionIdentity, RunRecord

    fields = {
        "run_id": run_id,
        "issue_num": issue_num,
        "item_id": item_id,
        "branch": f"agent/issue-{item_id.lower()}",
        "status": "success",
        "exit_code": 0,
        "started_at": datetime(2026, 8, 18, 10, 0, tzinfo=UTC),
        "finished_at": datetime(2026, 8, 18, 10, 30, tzinfo=UTC),
        "runner_name": "claude-code",
        "model": "claude-opus-5",
        "attempt_number": 1,
    }
    fields.update(overrides)
    store.admit_run_with_identity(
        RunRecord(**fields),
        AdmissionIdentity(
            run_id=run_id,
            attempt_number=1,
            source_tracker=tracker,
            item_id=item_id,
            delivery_repo="acme/widgets",
        ),
    )


def _state(store, **kwargs):
    from kew.config import DispatchConfig, ProjectConfig
    from kew.tui.state import TuiState

    config = DispatchConfig(project=ProjectConfig(repo="acme/widgets"))
    state = TuiState(config=config, store=store, **kwargs)
    return state


class TestCanonicalWorkProjection:
    """``refresh_local`` retains WorkView revisions, not derived statuses."""

    def test_refresh_retains_the_projection_and_its_revision(self, tmp_path):
        from kew.run_store import SqliteRunStore

        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            _admit_run(store, run_id="r1", item_id="42", issue_num=42)
            state = _state(store)
            assert state.work_revision() == ""  # never read is not "healthy"

            state.refresh_local()

            assert state.work_revision().startswith("wp1:")
            view = state.work_view("42")
            assert view is not None
            assert view.identity.item_id == "42"
            assert view.view_revision.startswith("wv1:")

    def test_the_linear_item_keeps_its_opaque_id_and_never_becomes_zero(self, tmp_path):
        from kew.run_store import SqliteRunStore
        from kew.tui.state import work_display

        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            _admit_run(
                store,
                run_id="r-sfd",
                item_id="SFD-12",
                issue_num=0,
                tracker="linear",
                status="awaiting_publication",
            )
            state = _state(store)
            state.refresh_local()

            view = state.work_view("SFD-12")
            assert view is not None
            display = work_display(view)
            assert display.ref == "SFD-12"
            assert "#0" not in display.ref

    def test_preserved_work_is_not_an_error_and_never_asks_for_a_redispatch(
        self, tmp_path
    ):
        from kew.run_store import SqliteRunStore
        from kew.tui.state import work_display

        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            _admit_run(
                store,
                run_id="r1",
                item_id="42",
                issue_num=42,
                status="awaiting_publication",
            )
            state = _state(store)
            state.refresh_local()

            display = work_display(state.work_view("42"))
            assert display.badge != "ERR"
            assert display.exception == "awaiting_publication"
            assert display.action == "finalize_publication"
            assert "dispatch" not in display.action

    def test_an_in_flight_run_reads_live_and_waits(self, tmp_path):
        from kew.run_store import SqliteRunStore
        from kew.tui.palette import MACHINE
        from kew.tui.state import work_display

        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            _admit_run(
                store,
                run_id="r1",
                item_id="42",
                issue_num=42,
                status="pending",
                # Started minutes ago, against the wall clock the TUI's own
                # refresh reads: a pending row is a run in flight only while it
                # is YOUNG (``PENDING_RUN_MAX_AGE_S`` in kew/service/work.py),
                # and the fixture's August default would be read — rightly — as
                # a row a dead host left behind.
                started_at=datetime.now(UTC) - timedelta(minutes=5),
                finished_at=None,
            )
            state = _state(store)
            state.refresh_local()

            display = work_display(state.work_view("42"))
            assert display.badge == "LIVE"
            assert display.color == MACHINE
            assert display.action == "wait"

    def test_a_merged_artifact_is_delivered_from_the_cached_forge_reading(
        self, tmp_path
    ):
        """The persisted pr_state rides along as the cached reading it is, so a
        TUI with no network still shows a truthful judgment."""
        from kew.run_store import SqliteRunStore
        from kew.tui.palette import ATTESTED
        from kew.tui.state import work_display

        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            _admit_run(
                store,
                run_id="r1",
                item_id="42",
                issue_num=42,
                pr_url="https://example.invalid/pr/7",
                pr_state="merged",
            )
            state = _state(store)
            state.refresh_local()

            display = work_display(state.work_view("42"))
            assert display.badge == "OK"
            assert display.color == ATTESTED
            # The cached forge reading is labelled stale, and the whole row
            # still reports the worst gap it rests on — the tracker was never
            # read, and an unread remote is not a successful empty.
            assert "pull_request:stale" in display.evidence_detail
            assert display.evidence == "unavailable"
            assert "tracker_item:unavailable" in display.evidence_detail

    def test_a_terminal_failure_is_the_only_thing_that_reads_as_an_error(
        self, tmp_path
    ):
        from kew.run_store import SqliteRunStore
        from kew.tui.state import work_display

        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            _admit_run(store, run_id="r1", item_id="42", issue_num=42, status="failed")
            state = _state(store)
            state.refresh_local()

            assert work_display(state.work_view("42")).badge == "ERR"

    def test_a_projection_that_could_not_be_composed_is_never_a_healthy_empty(self):
        from kew.tui.state import work_display

        store = MagicMock()
        store.read_snapshot.side_effect = RuntimeError("db is gone")
        store.list_pending.return_value = []
        store.list_all.return_value = []
        store.get_fleet_stopped.return_value = None
        store.list_all_epic_holds.return_value = []
        store.list_orphan_prs.return_value = []
        state = _state(store)

        with patch("kew.report.open_approval_holds", return_value=[]):
            state.refresh_local()

        assert state.work_projection is None
        assert state.work_revision() == ""
        assert state.work_view("42") is None
        display = work_display(state.work_view("42"))
        assert display.read is False
        assert display.evidence == "unread"
        assert display.badge not in ("OK", "ERR")

    def test_the_queue_is_projected_too_so_a_never_started_item_is_canonical(
        self, tmp_path
    ):
        from kew.run_store import SqliteRunStore

        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            state = _state(store)
            state.queue_ready = [{"number": 7, "title": "Do the thing"}]
            state.refresh_local()

            view = state.work_view("7")
            assert view is not None
            assert view.execution.state == "not_started"

    def test_an_unreadable_item_is_named_rather_than_dropped(self, tmp_path):
        from kew.run_store import SqliteRunStore

        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            _admit_run(store, run_id="r1", item_id="42", issue_num=42)
            state = _state(store)
            state.refresh_local()
            assert state.work_unreadable() == []

    def test_the_revision_is_stable_across_polls_and_moves_when_facts_do(
        self, tmp_path
    ):
        from kew.run_store import SqliteRunStore

        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            _admit_run(store, run_id="r1", item_id="42", issue_num=42)
            state = _state(store)
            state.refresh_local()
            first = state.work_revision()
            state.refresh_local()
            assert state.work_revision() == first

            _admit_run(store, run_id="r2", item_id="43", issue_num=43)
            state.refresh_local()
            assert state.work_revision() != first

    def test_the_display_carries_the_view_revision_an_action_is_decided_against(
        self, tmp_path
    ):
        from kew.run_store import SqliteRunStore
        from kew.tui.state import work_display

        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            _admit_run(store, run_id="r1", item_id="42", issue_num=42)
            state = _state(store)
            state.refresh_local()

            view = state.work_view("42")
            assert work_display(view).revision == view.view_revision

    def test_a_record_without_an_item_id_still_resolves_by_its_numeric_id(
        self, tmp_path
    ):
        from kew.run_store import SqliteRunStore
        from kew.tui.state import work_item_id

        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            store.save(
                RunRecord(
                    run_id="legacy",
                    issue_num=99,
                    branch="b",
                    status="success",
                    exit_code=0,
                    started_at=datetime.now(UTC),
                    runner_name="claude-code",
                    model="m",
                )
            )
            state = _state(store)
            state.refresh_local()
            rec = state.recent_runs[0]
            assert work_item_id(rec) == "99"
            assert state.work_view_for_run(rec) is not None
