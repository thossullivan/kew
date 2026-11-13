"""Tests for the unified logs screen."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from conftest import (
    make_finished_event as _finished_event,
)
from conftest import (
    make_started_event as _started_event,
)
from conftest import (
    make_tokens_event as _tokens_event,
)
from support.tui_wait import quiesce

from kew.tui.palette import ATTESTED, JUDGMENT, MONEY
from kew.tui.screens.logs import LogsScreen


def _no_highlight(screen, event) -> None:
    """A LogsScreen highlight handler that arms no debounced load."""


def test_logs_screen_constructs():
    screen = LogsScreen()
    assert screen.lookback == "today"
    assert screen.pre_select_run_id is None


def test_logs_screen_with_pre_selection():
    screen = LogsScreen(pre_select_run_id="abc-123")
    assert screen.pre_select_run_id == "abc-123"


def test_logs_screen_lookback_cycle():
    screen = LogsScreen()
    assert screen.lookback == "today"
    # Verify the cycle list is accessible
    from kew.tui.screens.logs import _LOOKBACK_CYCLE

    assert _LOOKBACK_CYCLE == ["today", "7d", "30d", "all"]


@pytest.mark.asyncio
async def test_logs_stream_richlog_has_markup_enabled():
    """RichLog must have markup=True so Rich tags render
    instead of showing as literal text."""
    from textual.app import App, ComposeResult
    from textual.widgets import RichLog

    class _TestApp(App):  # type: ignore[type-arg]
        state = None  # satisfies LogsScreen._update_run_list guard

        def compose(self) -> ComposeResult:
            yield LogsScreen()

    app = _TestApp()
    async with app.run_test() as _pilot:
        log = app.screen.query_one("#logs-stream", RichLog)
        assert log.markup is True, (
            "RichLog#logs-stream must have markup=True so Rich tags render correctly"
        )


# -- Summary header tests --


class TestBuildCompletedSummary:
    """_build_completed_summary should show exit status, tokens, cost, duration."""

    def test_build_completed_summary_success(self):
        """Completed run with exit 0 shows COMPLETE, exit:0 in green,
        tokens, cost, duration."""
        events = [
            _started_event(ts=1711000000.0),
            _tokens_event(inp=40000, out=8200, cost=1.64),
            _finished_event(exit_code=0, cost=1.64, ts=1711000618.0),
        ]
        result = LogsScreen._build_completed_summary(events)
        assert "COMPLETE" in result
        assert "exit:0" in result
        assert f"[{ATTESTED}]" in result
        assert "48,200" in result or "48.2k" in result  # token count
        assert "$1.64" in result
        assert "10m 18s" in result

    def test_build_completed_summary_failure(self):
        """Completed run with non-zero exit shows ERR color and exit code."""
        events = [
            _started_event(ts=1711000000.0),
            _tokens_event(inp=5000, out=1000, cost=0.30),
            _finished_event(exit_code=1, cost=0.30, ts=1711000120.0),
        ]
        result = LogsScreen._build_completed_summary(events)
        assert "COMPLETE" in result
        assert "exit:1" in result
        assert f"[{JUDGMENT}]" in result  # failure color
        assert "6,000" in result  # 5000 + 1000
        assert "$0.30" in result
        assert "2m" in result

    def test_build_completed_summary_no_events(self):
        """Empty events produce the exact fallback string."""
        result = LogsScreen._build_completed_summary([])
        assert result == "[dim]Replay[/dim]"


class TestBuildLiveSummaryWithElapsed:
    """_build_live_summary should show live indicator, tokens, cost, elapsed time."""

    def test_build_live_summary_with_elapsed(self, monkeypatch):
        """Live run shows LIVE indicator, tokens, cost, and elapsed time."""
        start_ts = 1711000000.0
        monkeypatch.setattr("kew.tui.screens.logs.time.time", lambda: start_ts + 300)
        events = [
            _started_event(ts=start_ts),
            _tokens_event(inp=40000, out=8200, cost=1.64, ts=start_ts + 151),
        ]
        result = LogsScreen._build_live_summary(events)
        assert "LIVE" in result
        assert "48,200" in result  # 40000 + 8200
        assert "$1.64" in result
        assert "5m" in result

    def test_build_live_summary_no_started_event(self):
        """Live summary without a started event still shows tokens/cost."""
        events = [
            _tokens_event(inp=1000, out=200, cost=0.05),
        ]
        result = LogsScreen._build_live_summary(events)
        assert "LIVE" in result
        assert "1,200" in result
        assert "$0.05" in result


class TestBuildCompletedSummaryNoFinished:
    """_build_completed_summary without a finished event should show exit:?."""

    def test_no_finished_event_shows_exit_unknown(self):
        """When there's no finished event, exit code should show as '?'."""
        events = [
            _started_event(ts=1711000000.0),
            _tokens_event(inp=2000, out=500, cost=0.10),
        ]
        result = LogsScreen._build_completed_summary(events)
        assert "COMPLETE" in result
        assert "exit:?" in result
        assert "2,500" in result
        assert "$0.10" in result

    def test_no_finished_event_no_duration(self):
        """Without a finished event, there should be no duration displayed."""
        events = [
            _started_event(ts=1711000000.0),
            _tokens_event(inp=1000, out=200, cost=0.05),
        ]
        result = LogsScreen._build_completed_summary(events)
        assert "exit:?" in result
        assert "[dim]" in result  # the exit:? is wrapped in dim
        # Duration is appended after the cost; verify nothing follows it
        after_cost = result.split(f"[/{MONEY}]")[-1]
        assert after_cost.strip() == "", (
            f"Expected no duration after cost, got: {after_cost!r}"
        )


class TestBuildCompletedSummaryNoTokens:
    """When no token events exist, omit '0 tokens' and use finished cost."""

    def test_no_token_events_omits_zero_tokens(self):
        """Should not show '0 tokens' when there are no token events."""
        events = [
            _started_event(ts=1711000000.0),
            _finished_event(exit_code=0, cost=1.50, ts=1711000600.0),
        ]
        result = LogsScreen._build_completed_summary(events)
        assert "COMPLETE" in result
        assert "0 tokens" not in result
        assert "$1.50" in result
        assert "10m" in result

    def test_finished_cost_used_when_no_token_events(self):
        """Cost should come from finished event when no tokens events."""
        events = [
            _started_event(ts=1711000000.0),
            _finished_event(exit_code=0, cost=2.75, ts=1711000060.0),
        ]
        result = LogsScreen._build_completed_summary(events)
        assert "$2.75" in result


class TestBuildLiveSummaryWithKwargs:
    """_build_live_summary with pre-computed keyword arguments (no events)."""

    def test_kwargs_only_tokens_and_cost(self):
        """Passing tokens/cost as kwargs without events should work."""
        result = LogsScreen._build_live_summary(
            tokens=15000,
            cost=0.75,
        )
        assert "LIVE" in result
        assert "15,000" in result
        assert "$0.75" in result

    def test_kwargs_with_start_ts(self, monkeypatch):
        """Passing start_ts as kwarg should show elapsed time."""
        monkeypatch.setattr("kew.tui.screens.logs.time.time", lambda: 1711000120.0)
        result = LogsScreen._build_live_summary(
            tokens=5000,
            cost=0.30,
            start_ts=1711000000.0,
        )
        assert "LIVE" in result
        assert "5,000" in result
        assert "$0.30" in result
        assert "2m" in result

    def test_kwargs_zero_start_ts_no_elapsed(self):
        """start_ts=0 should not show any elapsed time."""
        result = LogsScreen._build_live_summary(
            tokens=1000,
            cost=0.05,
            start_ts=0.0,
        )
        assert "LIVE" in result
        assert "1,000" in result
        assert "$0.05" in result

    def test_none_events_uses_kwargs(self):
        """Passing events=None explicitly should use kwargs only."""
        result = LogsScreen._build_live_summary(
            None,
            tokens=8000,
            cost=0.40,
        )
        assert "LIVE" in result
        assert "8,000" in result
        assert "$0.40" in result


def _make_mock_run(
    run_id: str,
    status: str = "success",
    started_at: datetime | None = None,
) -> MagicMock:
    """Create a mock run object for testing _get_completed_runs."""
    run = MagicMock()
    run.run_id = run_id
    run.status = status
    run.started_at = started_at
    return run


class TestGetCompletedRuns:
    """_get_completed_runs should filter runs by lookback window."""

    def _make_app(
        self,
        recent_runs: list,
        all_runs: list,
    ) -> MagicMock:
        """Create a mock app with state.recent_runs and state.store.list_all()."""
        app = MagicMock()
        app.state.recent_runs = recent_runs
        app.state.store.list_all.return_value = all_runs
        return app

    def test_lookback_today_returns_recent_runs(self):
        """'today' lookback should return app.state.recent_runs directly."""
        now = datetime.now(UTC)
        recent = [_make_mock_run("r1", started_at=now)]
        old = [_make_mock_run("r2", started_at=now - timedelta(days=10))]

        screen = LogsScreen()
        screen.lookback = "today"
        app = self._make_app(recent_runs=recent, all_runs=recent + old)
        result = screen._get_completed_runs(app)
        assert len(result) == 1
        assert result[0].run_id == "r1"

    def test_lookback_7d_filters_by_seven_days(self):
        """'7d' lookback should return runs from the last 7 days."""
        now = datetime.now(UTC)
        r_3d = _make_mock_run("r1", started_at=now - timedelta(days=3))
        r_10d = _make_mock_run("r2", started_at=now - timedelta(days=10))
        r_1d = _make_mock_run("r3", started_at=now - timedelta(days=1))

        screen = LogsScreen()
        screen.lookback = "7d"
        app = self._make_app(
            recent_runs=[],
            all_runs=[r_3d, r_10d, r_1d],
        )
        result = screen._get_completed_runs(app)
        run_ids = {r.run_id for r in result}
        assert "r1" in run_ids  # 3 days ago
        assert "r3" in run_ids  # 1 day ago
        assert "r2" not in run_ids  # 10 days ago

    def test_lookback_30d_filters_by_thirty_days(self):
        """'30d' lookback should return runs from the last 30 days."""
        now = datetime.now(UTC)
        r_10d = _make_mock_run("r1", started_at=now - timedelta(days=10))
        r_45d = _make_mock_run("r2", started_at=now - timedelta(days=45))
        r_25d = _make_mock_run("r3", started_at=now - timedelta(days=25))

        screen = LogsScreen()
        screen.lookback = "30d"
        app = self._make_app(
            recent_runs=[],
            all_runs=[r_10d, r_45d, r_25d],
        )
        result = screen._get_completed_runs(app)
        run_ids = {r.run_id for r in result}
        assert "r1" in run_ids  # 10 days ago
        assert "r3" in run_ids  # 25 days ago
        assert "r2" not in run_ids  # 45 days ago

    def test_lookback_all_returns_all_completed(self):
        """'all' lookback should return every non-pending run."""
        now = datetime.now(UTC)
        r_old = _make_mock_run("r1", started_at=now - timedelta(days=365))
        r_recent = _make_mock_run("r2", started_at=now - timedelta(days=1))
        r_pending = _make_mock_run("r3", status="pending", started_at=now)

        screen = LogsScreen()
        screen.lookback = "all"
        app = self._make_app(
            recent_runs=[],
            all_runs=[r_old, r_recent, r_pending],
        )
        result = screen._get_completed_runs(app)
        run_ids = {r.run_id for r in result}
        assert "r1" in run_ids
        assert "r2" in run_ids
        assert "r3" not in run_ids  # pending excluded

    def test_lookback_7d_excludes_pending_runs(self):
        """Pending runs should be excluded from completed results."""
        now = datetime.now(UTC)
        r_ok = _make_mock_run("r1", started_at=now - timedelta(days=2))
        r_pending = _make_mock_run("r2", status="pending", started_at=now)

        screen = LogsScreen()
        screen.lookback = "7d"
        app = self._make_app(
            recent_runs=[],
            all_runs=[r_ok, r_pending],
        )
        result = screen._get_completed_runs(app)
        run_ids = {r.run_id for r in result}
        assert "r1" in run_ids
        assert "r2" not in run_ids

    def test_lookback_7d_excludes_runs_with_no_started_at(self):
        """Runs without started_at should be excluded from date-filtered results."""
        r_none = _make_mock_run("r1", started_at=None)

        screen = LogsScreen()
        screen.lookback = "7d"
        app = self._make_app(
            recent_runs=[],
            all_runs=[r_none],
        )
        result = screen._get_completed_runs(app)
        assert len(result) == 0


# --- I22: the recorder shows canonical work truth, never a derived status ---


def _display(**kwargs):
    from kew.tui.palette import WARN
    from kew.tui.state import WorkDisplay

    defaults = {
        "badge": "WAIT",
        "color": WARN,
        "ref": "SFD-12",
        "exception": "awaiting_publication",
        "detail": "implementation preserved on agent/issue-sfd-12",
        "action": "finalize_publication",
        "evidence": "stale",
        "evidence_detail": "pull_request:stale",
        "revision": "wv1:deadbeefcafe",
        "read": True,
    }
    defaults.update(kwargs)
    return WorkDisplay(**defaults)


def _run(**kwargs):
    from types import SimpleNamespace

    defaults = {
        "run_id": "r1",
        "issue_num": 0,
        "item_id": "SFD-12",
        "title": "Do the thing",
        "status": "awaiting_publication",
        "started_at": datetime.now(UTC) - timedelta(minutes=3),
        "duration_seconds": 600,
        "estimated_cost_usd": 1.5,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


class TestRunRows:
    def test_a_preserved_run_is_not_rendered_as_an_error(self):
        from kew.tui.screens.logs import completed_row_text

        text = completed_row_text(_run(), _display())
        assert "ERR" not in text
        assert "WAIT" in text

    def test_a_linear_run_row_shows_its_opaque_id_never_hash_zero(self):
        from kew.tui.screens.logs import completed_row_text, pending_row_text

        for text in (
            completed_row_text(_run(), _display()),
            pending_row_text(_run(status="pending"), _display(badge="LIVE")),
        ):
            assert "SFD-12" in text
            assert "#0" not in text

    def test_a_row_with_no_projection_says_nothing_rather_than_guessing(self):
        from kew.tui.screens.logs import completed_row_text
        from kew.tui.state import UNREAD_BADGE, work_display

        text = completed_row_text(_run(), work_display(None))
        assert UNREAD_BADGE in text
        # the identity still comes off the record, so the row is still joinable
        assert "SFD-12" in text

    def test_a_github_row_keeps_its_numeric_reference(self):
        from kew.tui.screens.logs import completed_row_text

        text = completed_row_text(_run(issue_num=42, item_id="42"), _display(ref="#42"))
        assert "#42" in text


class TestWorkDetailLine:
    """The recorder's one canonical line — and its route to web detail."""

    def test_it_shows_the_exception_action_evidence_and_revision(self):
        from kew.tui.screens.logs import work_detail_line

        line = work_detail_line(_display(), "run-7")
        assert "awaiting_publication" in line
        assert "finalize_publication" in line
        assert "stale" in line
        assert "wv1:deadbeefcafe" in line

    def test_it_routes_to_the_canonical_web_detail_instead_of_a_scrubber(self):
        from kew.tui.screens.logs import work_detail_line

        line = work_detail_line(_display(), "run-7")
        assert "/runs/run-7" in line
        assert "kew serve" in line

    def test_an_unread_projection_line_claims_nothing(self):
        from kew.tui.screens.logs import work_detail_line
        from kew.tui.state import work_display

        line = work_detail_line(work_display(None), "run-7")
        assert "not read" in line
        assert "OK" not in line

    def test_an_unavailable_remote_is_named_with_the_dimensions_it_lost(self):
        from kew.tui.screens.logs import work_detail_line

        line = work_detail_line(
            _display(
                evidence="unavailable", evidence_detail="tracker_item:unavailable"
            ),
            "run-7",
        )
        assert "unavailable" in line
        assert "tracker_item" in line


def test_posture_detail_line_renders_the_service_record() -> None:
    import kew.service.models as models
    from kew.tui.screens import logs

    dto_type = getattr(models, "SandboxPostureDTO", None)
    render = getattr(logs, "posture_detail_line", None)
    assert dto_type is not None, "the service must own the posture DTO"
    assert callable(render), "the TUI run view must render recorded posture"
    line = render(
        dto_type(
            posture="native",
            filesystem_boundary="runner-native-workspace-write",
            network_boundary="not-applicable",
            credential_names=[],
            command_guard="not-applicable",
            admission="policy",
        )
    )
    assert "posture: native" in line
    assert "filesystem_boundary=runner-native-workspace-write" in line


@pytest.mark.asyncio
async def test_logs_screen_publishes_the_canonical_work_line():
    """The recorder mounts one work line, fed by the shared projection."""
    from textual.app import App, ComposeResult
    from textual.widgets import Static

    class _TestApp(App):  # type: ignore[type-arg]
        state = None

        def compose(self) -> ComposeResult:
            yield LogsScreen()

    app = _TestApp()
    async with app.run_test() as _pilot:
        line = app.screen.query_one("#logs-work", Static)
        assert "not read" in str(line.content)


def test_the_recorder_follows_the_projection_across_revisions(tmp_path):
    """Task 84 — a row re-reads the shared projection on every refresh, so a
    run that moves from in-flight to preserved changes badge without this
    screen ever consulting a RunStatus."""
    from kew.config import DispatchConfig, ProjectConfig
    from kew.run_store import AdmissionIdentity, RunRecord, SqliteRunStore
    from kew.tui.screens.logs import completed_row_text, pending_row_text
    from kew.tui.state import TuiState

    started = datetime.now(UTC)
    record = RunRecord(
        run_id="r1",
        issue_num=0,
        item_id="SFD-12",
        branch="agent/issue-sfd-12",
        status="pending",
        exit_code=-1,
        started_at=started,
        runner_name="claude-code",
        model="claude-opus-5",
        attempt_number=1,
    )
    with SqliteRunStore(str(tmp_path / "t.db")) as store:
        store.admit_run_with_identity(
            record,
            AdmissionIdentity(
                run_id="r1",
                attempt_number=1,
                source_tracker="linear",
                item_id="SFD-12",
                delivery_repo="acme/widgets",
            ),
        )
        state = TuiState(
            config=DispatchConfig(project=ProjectConfig(repo="acme/widgets")),
            store=store,
        )
        state.refresh_local()
        in_flight_revision = state.work_revision()
        run = state.pending_runs[0]
        assert "LIVE" in pending_row_text(run, state.work_display_for_run(run))

        store.save(
            RunRecord(
                **{
                    **record.__dict__,
                    "status": "awaiting_publication",
                    "exit_code": 0,
                    "finished_at": datetime.now(UTC),
                }
            )
        )
        state.refresh_local()

        assert state.work_revision() != in_flight_revision
        display = state.work_display_for_run(record)
        assert display.exception == "awaiting_publication"
        assert display.action == "finalize_publication"
        text = completed_row_text(record, display)
        assert "ERR" not in text
        assert "SFD-12" in text


@pytest.mark.parametrize("retry", [False, True], ids=["promotion", "retry-reset"])
@pytest.mark.asyncio
async def test_selected_posture_refreshes_without_selection_change(retry):
    from types import SimpleNamespace

    from textual.app import App, ComposeResult
    from textual.widgets import Static

    record = SimpleNamespace(run_id="r1", attempt_number=1)
    payload = {
        "attempt_number": 1,
        "posture": "sandboxed",
        "network_boundary": "sidecar-certified" if retry else "unavailable",
        "egress_verification_basis": "sidecar-admission"
        if retry
        else "pending-sidecar",
    }
    events = [SimpleNamespace(run_id="r1", payload=payload)]
    store = SimpleNamespace(
        list_all=lambda: [record], list_audit_events=lambda **k: events
    )

    class TestApp(App):
        state = None

        def compose(self) -> ComposeResult:
            yield LogsScreen()

    app = TestApp()
    async with app.run_test():
        screen = app.screen.query_one(LogsScreen)
        screen._selected_run_id = "r1"
        from kew.tui.state import work_display

        app.state = SimpleNamespace(
            store=store, work_display_for_run=lambda run: work_display(None)
        )
        line = screen.query_one("#logs-posture", Static)
        from kew.service.runs import get_run_posture
        from kew.tui.screens.logs import posture_detail_line

        line.update(posture_detail_line(get_run_posture(store, "r1")))
        if retry:
            record.attempt_number = 2
        else:
            payload.update(
                network_boundary="sidecar-certified",
                egress_verification_basis="sidecar-admission",
            )
        screen._tail_events()
        expected = "pending-sidecar" if retry else "sidecar-admission"
        assert f"egress_verification_basis={expected}" in str(line.content)
        assert screen._selected_run_id == "r1"


@pytest.mark.parametrize("phase", ["load", "refresh"])
@pytest.mark.asyncio
async def test_round9_logs_uses_matched_attempt(tmp_path, monkeypatch, phase):
    import json
    import re
    from types import SimpleNamespace

    from test_service_runs import _record_sidecar_posture
    from textual.app import App, ComposeResult
    from textual.widgets import ListView, Static

    from kew.run_store import RunRecord, SqliteRunStore
    from kew.tui.selection import WorkerListItem
    from kew.tui.state import work_display

    class TestApp(App):
        state = None

        def compose(self) -> ComposeResult:
            yield LogsScreen()

    record = RunRecord(
        run_id="r1",
        issue_num=1945,
        branch="attempt-2",
        status="pending",
        exit_code=-1,
        attempt_number=2,
        started_at=datetime.now(UTC),
        log_file=tmp_path / "attempt-2.log",
    )
    for attempt in (2, 3):
        (tmp_path / f"attempt-{attempt}.events.jsonl").write_text(
            json.dumps({"type": "tokens", "input": attempt}) + "\n"
        )
    with (
        SqliteRunStore(str(tmp_path / "runs.db")) as store,
        SqliteRunStore(str(tmp_path / "runs.db")) as writer,
    ):
        store.save(record)
        _record_sidecar_posture(store, 2, admitted=True)
        raced = False

        def commit_retry():
            nonlocal raced
            if not raced:
                raced = True
                writer.save(
                    RunRecord(
                        **{
                            **record.__dict__,
                            "attempt_number": 3,
                            "log_file": tmp_path / "attempt-3.log",
                        }
                    )
                )
                _record_sidecar_posture(writer, 3, admitted=True)
                assert not writer._conn.in_transaction

        def display_for_run(view):
            if phase == "load":
                commit_retry()
            return work_display(view)

        monkeypatch.setattr("kew.tui.screens.logs.work_display", display_for_run)

        app = TestApp()
        async with app.run_test() as pilot:
            screen = app.screen.query_one(LogsScreen)
            screen._tail_timer.stop()
            app.state = SimpleNamespace(
                store=store,
                pending_runs=[record],
            )
            lv = screen.query_one("#logs-list", ListView)
            await lv.append(WorkerListItem(run_id="r1", text="attempt 2"))
            lv.index = 0
            # This test drives the single load itself, so disarm the screen's
            # 0.15s highlight debounce the same way it stops the tail timer
            # above: left armed, it loads a second time whenever the pump is
            # slower than the debounce, which no assertion here is about.
            monkeypatch.setattr(LogsScreen, "on_list_view_highlighted", _no_highlight)
            await quiesce(pilot)
            screen._load_selected_run()
            if phase == "refresh":
                commit_retry()
                screen._tail_events()
            line = str(screen.query_one("#logs-posture", Static).content)
            match = re.search(r"reason=attempt (\d+)", line)
            assert match is not None
            attempt = int(match[1])
            assert raced
            assert screen._tail_path == tmp_path / f"attempt-{attempt}.events.jsonl"
            assert f"attempt={attempt}" in line
            summary = str(screen.query_one("#logs-summary", Static).content)
            assert f"{attempt:,} tokens" in summary


def _assert_round10_logs(screen, views, tmp_path):
    from textual.widgets import Static

    assert views and views[-1] is not None
    identity = views[-1].identity
    assert identity.run_id == "r1"
    assert identity.attempt_number == screen._selected_record_key[0] == 2
    posture = str(screen.query_one("#logs-posture", Static).content)
    assert f"reason=attempt {identity.attempt_number}" in posture
    assert f"attempt={identity.attempt_number}" in posture
    assert screen._tail_path == tmp_path / "attempt-2.events.jsonl"
    summary = str(screen.query_one("#logs-summary", Static).content)
    assert "2 tokens" in summary
    work = str(screen.query_one("#logs-work", Static).content)
    assert views[-1].view_revision in work
    assert "evidence" in work and "kew serve" in work


@pytest.mark.parametrize("storage", ["file", "memory"])
@pytest.mark.parametrize("phase", ["load", "refresh"])
@pytest.mark.parametrize("race", [True, False], ids=["retry", "stable"])
@pytest.mark.asyncio
async def test_round10_logs_composes_one_attempt(
    tmp_path, monkeypatch, storage, phase, race
):
    import json

    from test_cli_status import _round10_retry, _round10_stores
    from textual.app import App, ComposeResult
    from textual.widgets import ListView

    from kew.config import DispatchConfig, ProjectConfig
    from kew.service import runs
    from kew.tui import state as state_module
    from kew.tui.screens import logs
    from kew.tui.selection import WorkerListItem

    class TestApp(App):
        state = None

        def compose(self) -> ComposeResult:
            yield LogsScreen()

    views = []
    original_display = state_module.work_display

    def display(view):
        views.append(view)
        return original_display(view)

    monkeypatch.setattr(state_module, "work_display", display)
    monkeypatch.setattr(logs, "work_display", display)
    with _round10_stores(tmp_path, storage) as (store, writer):
        record = store.get("r1")
        record.status = "pending"
        record.log_file = tmp_path / "attempt-2.log"
        store.save(record)
        record.log_file.with_suffix(".events.jsonl").write_text(
            json.dumps({"type": "tokens", "input": 2}) + "\n"
        )
        state = state_module.TuiState(
            config=DispatchConfig(project=ProjectConfig(repo="acme/widgets")),
            store=store,
        )
        state._all_runs = [record]
        state.refresh_work()
        app = TestApp()
        async with app.run_test() as pilot:
            screen = app.screen.query_one(LogsScreen)
            screen._tail_timer.stop()
            app.state = state
            lv = screen.query_one("#logs-list", ListView)
            await lv.append(WorkerListItem(run_id="r1", text="attempt 2"))
            lv.index = 0
            # This test drives the single load itself, so disarm the screen's
            # 0.15s highlight debounce the same way it stops the tail timer
            # above: left armed, it loads a second time whenever the pump is
            # slower than the debounce, which no assertion here is about.
            monkeypatch.setattr(LogsScreen, "on_list_view_highlighted", _no_highlight)
            await quiesce(pilot)
            original = runs._verify_posture
            reads = []

            def after_posture(*args, **kwargs):
                selected = original(*args, **kwargs)
                reads.append(selected[0])
                if race:
                    _round10_retry(writer)
                    state.refresh_work()
                return selected

            monkeypatch.setattr(runs, "_verify_posture", after_posture)
            if phase == "load":
                screen._selected_run_id = None
                screen._load_selected_run()
            else:
                screen._selected_run_id = "r1"
                screen._selected_record_key = None
                screen._tail_events()
            assert reads == [2]
            _assert_round10_logs(screen, views, tmp_path)


def test_the_selected_run_is_composed_under_the_host_s_in_flight_bound(
    tmp_path, monkeypatch
):
    """The logs screen reads a pending row the way the dashboard does (#2279/D8).

    ``pending_run_max_age_s`` is twice the operator's ``agent.timeout_minutes``,
    floored at four hours, and the dashboard's projection — the same screen's
    row badges — is composed under it. Selecting a run composed against the
    bare floor made the detail pane call a five-hour run on a six-hour agent
    abandoned while the row above it still read LIVE.
    """
    from types import SimpleNamespace

    from kew.config import AgentConfig, DispatchConfig, ProjectConfig
    from kew.run_store import AdmissionIdentity, RunRecord, SqliteRunStore

    config = DispatchConfig(
        project=ProjectConfig(repo="acme/widgets"),
        agent=AgentConfig(timeout_minutes=360),
    )
    with SqliteRunStore(str(tmp_path / "logs.db")) as store:
        store.admit_run_with_identity(
            RunRecord(
                run_id="r1",
                issue_num=42,
                item_id="42",
                branch="agent/issue-42",
                status="pending",
                exit_code=-1,
                started_at=datetime.now(UTC) - timedelta(hours=5),
                attempt_number=1,
            ),
            AdmissionIdentity(
                run_id="r1",
                attempt_number=1,
                source_tracker="github",
                item_id="42",
                delivery_repo="acme/widgets",
            ),
        )
        screen = LogsScreen()
        app = SimpleNamespace(state=SimpleNamespace(store=store, config=config))
        monkeypatch.setattr(LogsScreen, "app", property(lambda self: app))

        detail = screen._select_detail("r1")

    assert detail.work is not None
    assert detail.work.exception.state == "in_flight"
    assert detail.work.safest_action.action == "wait"
