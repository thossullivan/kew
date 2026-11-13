import time
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from support.tui_wait import (
    debounce_pending,
    quiesce,
    settle,
    wait_for,
)
from support.tui_wait import (
    no_ambient_polls as no_ambient_polls,
)

from kew.config import DispatchConfig
from kew.tui.app import KewApp

# --- Deterministic waits (#2178, #2342) --------------------------------------
# Every wait in this module goes through support/tui_wait.py, which carries a
# deadline and names what it waited for. A bare pilot pump would assert "the
# app got there within N idle points", which stops being true under load; that
# is what used to park this module in the serial phase.


class TestCockpitScreen:
    @pytest.mark.asyncio
    async def test_cockpit_renders_four_panels(self):
        app = KewApp(config=DispatchConfig(), db_path=":memory:")
        async with app.run_test() as _pilot:
            assert app.screen.query_one("#queue-panel") is not None
            assert app.screen.query_one("#workers-panel") is not None
            assert app.screen.query_one("#reviews-panel") is not None
            assert app.screen.query_one("#analytics-panel") is not None
            assert app.screen.query_one("#status-bar") is not None

    @pytest.mark.asyncio
    async def test_cockpit_shows_footer(self):
        app = KewApp(config=DispatchConfig(), db_path=":memory:")
        async with app.run_test() as _pilot:
            from textual.widgets import Footer

            footer = app.screen.query_one(Footer)
            assert footer is not None


class TestLiveUpdates:
    @pytest.mark.asyncio
    async def test_status_bar_updates_from_state(self, tmp_path):
        from kew.config import DispatchConfig, ProjectConfig
        from kew.run_store import RunRecord, SqliteRunStore
        from kew.tui.app import KewApp

        db_path = str(tmp_path / "runs.db")
        store = SqliteRunStore(db_path)
        now = datetime.now(UTC)
        store.save(
            RunRecord(
                run_id="r1",
                issue_num=1,
                branch="b1",
                status="success",
                exit_code=0,
                started_at=now,
                estimated_cost_usd=4.20,
            )
        )

        config = DispatchConfig(project=ProjectConfig(name="test-proj"))
        app = KewApp(config=config, db_path=db_path)
        async with app.run_test() as pilot:
            # Trigger a poll cycle
            app._poll_local()
            app._update_widgets()
            await quiesce(pilot)

            bar = app.screen.query_one("#status-bar")
            bar_text = str(bar.content)
            assert "test-proj" in bar_text
            assert "$4.20" in bar_text


class TestExpandedScreen:
    @pytest.mark.asyncio
    async def test_press_2_opens_logs_screen(self):
        from kew.config import DispatchConfig
        from kew.tui.app import KewApp
        from kew.tui.screens.logs import LogsScreen

        app = KewApp(config=DispatchConfig(), db_path=":memory:")
        async with app.run_test() as pilot:
            await pilot.press("2")
            await wait_for(
                pilot,
                lambda: isinstance(app.screen, LogsScreen),
                "the logs screen to become active",
            )
            assert isinstance(app.screen, LogsScreen)
            assert app.screen.query_one("#logs-breadcrumb") is not None
            await settle(pilot)

    @pytest.mark.asyncio
    async def test_esc_returns_to_cockpit(self):
        from kew.config import DispatchConfig
        from kew.tui.app import KewApp
        from kew.tui.screens.cockpit import CockpitScreen
        from kew.tui.screens.logs import LogsScreen

        app = KewApp(config=DispatchConfig(), db_path=":memory:")
        async with app.run_test() as pilot:
            await pilot.press("2")
            await wait_for(
                pilot,
                lambda: isinstance(app.screen, LogsScreen),
                "the logs screen to become active",
            )
            await pilot.press("escape")
            await wait_for(
                pilot,
                lambda: isinstance(app.screen, CockpitScreen),
                "the cockpit to become active again",
            )
            assert isinstance(app.screen, CockpitScreen)
            assert app.screen.query_one("#queue-panel") is not None
            await settle(pilot)

    @pytest.mark.asyncio
    async def test_logs_screen_populates_with_data(self, tmp_path):
        """LogsScreen (press 2) shows pending run data in its run list."""
        from kew.config import DispatchConfig, ProjectConfig
        from kew.run_store import RunRecord, SqliteRunStore
        from kew.tui.app import KewApp

        db_path = str(tmp_path / "runs.db")
        store = SqliteRunStore(db_path)
        now = datetime.now(UTC)
        store.save(
            RunRecord(
                run_id="w1",
                issue_num=200,
                branch="b1",
                status="pending",
                exit_code=-1,
                started_at=now,
                model="claude-sonnet-4",
            )
        )
        store.close()

        config = DispatchConfig(project=ProjectConfig(name="test-proj"))
        app = KewApp(config=config, db_path=db_path)
        async with app.run_test() as pilot:
            from textual.widgets import ListView

            from kew.tui.screens.logs import LogsScreen

            await pilot.press("2")
            await wait_for(
                pilot,
                lambda: isinstance(app.screen, LogsScreen),
                "the logs screen to become active",
            )
            app._poll_local()
            await wait_for(
                pilot,
                lambda: len(app.screen.query_one("#logs-list", ListView)._nodes) > 0,
                "the logs run list to populate",
            )
            await settle(pilot)
            logs_lv = app.screen.query_one("#logs-list", ListView)
            assert len(logs_lv._nodes) > 0

    @pytest.mark.asyncio
    async def test_press_3_opens_expanded_reviews(self, tmp_path):
        """Pressing 3 opens ExpandedScreen for reviews."""
        from kew.config import DispatchConfig, ProjectConfig
        from kew.tui.app import KewApp
        from kew.tui.screens.expanded import ExpandedScreen

        config = DispatchConfig(project=ProjectConfig(name="test-proj"))
        app = KewApp(config=config, db_path=":memory:")
        async with app.run_test() as pilot:
            await pilot.press("3")
            await wait_for(
                pilot,
                lambda: (
                    isinstance(app.screen, ExpandedScreen)
                    and app.screen.panel == "reviews"
                ),
                "the expanded reviews screen to become active",
            )
            assert isinstance(app.screen, ExpandedScreen)
            assert app.screen.panel == "reviews"
            await settle(pilot)

    @pytest.mark.asyncio
    async def test_reviews_listview_focused_on_mount(self):
        """Expanded reviews screen focuses the ListView on mount."""
        from textual.widgets import ListView

        from kew.tui.screens.expanded import ExpandedScreen

        app = KewApp(config=DispatchConfig(), db_path=":memory:")
        async with app.run_test() as pilot:
            await app.push_screen(ExpandedScreen(panel="reviews"))
            await wait_for(
                pilot,
                lambda: isinstance(app.screen.focused, ListView),
                "the reviews ListView to take focus on mount",
            )
            focused = app.screen.focused
            assert isinstance(focused, ListView)
            assert focused.id == "reviews-panel"
            await settle(pilot)

    @pytest.mark.asyncio
    async def test_cursor_down_single_review_triggers_detail_update(self):
        """j/k on a single-item reviews list triggers detail pane update."""
        from textual.widgets import ListView

        from kew.tui.screens.expanded import ExpandedScreen
        from kew.tui.selection import ReviewListItem

        app = KewApp(config=DispatchConfig(), db_path=":memory:")
        async with app.run_test() as pilot:
            await app.push_screen(ExpandedScreen(panel="reviews"))
            await wait_for(
                pilot,
                lambda: (
                    isinstance(app.screen, ExpandedScreen)
                    and app.screen.panel == "reviews"
                ),
                "the expanded reviews screen to mount",
            )
            # Drive the row through app state rather than appending to the
            # widget: the 2s local poll rebuilds #reviews-panel from state, and
            # would replace a hand-appended item with the "no pending reviews"
            # placeholder at a moment this test does not control.
            app.state.review_prs = [
                {
                    "number": 99,
                    "title": "PR #99",
                    "author": {"login": "kew"},
                    "createdAt": "",
                    "statusCheckRollup": [],
                }
            ]
            # Seed the PR detail cache so the refresh resolves from cache rather
            # than spawning a `gh pr view` worker whose timing this test neither
            # controls nor asserts on.
            app._pr_detail_cache[99] = {
                "number": 99,
                "title": "PR #99",
                "body": "",
                "labels": [],
                "assignees": [],
                "createdAt": "",
            }
            app._pr_cache_ts[99] = time.monotonic()
            app._update_widgets()

            def _review_row_is_live() -> bool:
                lv = app.screen.query_one("#reviews-panel", ListView)
                return (
                    isinstance(lv.highlighted_child, ReviewListItem)
                    and lv.highlighted_child.pr_number == 99
                    and app._detail_pr_num == 99
                    and not debounce_pending(app)
                )

            await wait_for(
                pilot, _review_row_is_live, "the review row to reach the detail pane"
            )

            # Track calls to _update_detail_pane
            original = app._update_detail_pane
            call_count = {"n": 0}

            def tracking_update():
                call_count["n"] += 1
                original()

            app._update_detail_pane = tracking_update

            # Cursor can't move (single item), so the screen must still ask for
            # a refresh — and that refresh must land before the harness unwinds
            # the screen stack, or it fires against an empty stack (#2178).
            for key in ("j", "k"):
                call_count["n"] = 0
                await pilot.press(key)
                await wait_for(
                    pilot,
                    lambda: call_count["n"] >= 1 and not debounce_pending(app),
                    "the keypress to run a debounced detail-pane refresh",
                )
                assert call_count["n"] >= 1

    @pytest.mark.asyncio
    async def test_cursor_refocuses_listview_when_detail_focused(self):
        """j/k refocuses the list panel when focus is elsewhere."""
        from textual.widgets import ListView

        from kew.tui.screens.expanded import ExpandedScreen

        app = KewApp(config=DispatchConfig(), db_path=":memory:")
        async with app.run_test() as pilot:
            await app.push_screen(ExpandedScreen(panel="reviews"))
            await wait_for(
                pilot,
                lambda: (
                    isinstance(app.screen, ExpandedScreen)
                    and app.screen.panel == "reviews"
                ),
                "the expanded reviews screen to mount",
            )

            # Move focus away from ListView to the detail pane
            detail = app.screen.query_one("#detail-pane")
            detail.focus()
            await wait_for(
                pilot,
                lambda: not isinstance(app.screen.focused, ListView),
                "focus to leave the reviews ListView",
            )
            assert not isinstance(app.screen.focused, ListView)

            # Press j — should refocus the ListView
            await pilot.press("j")
            await wait_for(
                pilot,
                lambda: isinstance(app.screen.focused, ListView),
                "the 'j' press to refocus the reviews ListView",
            )
            focused = app.screen.focused
            assert isinstance(focused, ListView)
            assert focused.id == "reviews-panel"
            await settle(pilot)


class TestActions:
    @pytest.mark.asyncio
    async def test_d_key_noop_with_empty_queue(self):
        from kew.config import DispatchConfig
        from kew.tui.app import KewApp

        app = KewApp(config=DispatchConfig(), db_path=":memory:")
        async with app.run_test() as pilot:
            await pilot.press("d")
            await quiesce(pilot)
            # Should not crash with empty queue
            assert app.screen.query_one("#queue-panel") is not None


@pytest.mark.asyncio
async def test_expanded_screen_has_listview():
    from kew.tui.screens.expanded import ExpandedScreen

    app = KewApp(config=DispatchConfig(), db_path=":memory:")
    async with app.run_test() as pilot:
        await app.push_screen(ExpandedScreen(panel="queue"))
        await wait_for(
            pilot,
            lambda: (
                isinstance(app.screen, ExpandedScreen) and app.screen.panel == "queue"
            ),
            "the expanded queue screen to mount",
        )
        # Queue panel should exist in the DOM (nested inside Horizontal)
        assert app.screen.query_one("#queue-panel") is not None
        # Detail pane should also be present for queue panel
        assert app.screen.query_one("#detail-pane") is not None
        await settle(pilot)


def test_cockpit_bindings_include_j_and_k():
    from kew.tui.screens.cockpit import CockpitScreen

    screen = CockpitScreen()
    keys = [b[0] for b in screen.BINDINGS]
    assert "j" in keys
    assert "k" in keys
    # k is now purely cursor_up; kill is on x
    k_actions = [b[1] for b in screen.BINDINGS if b[0] == "k"]
    assert "cursor_up" in k_actions
    x_actions = [b[1] for b in screen.BINDINGS if b[0] == "x"]
    assert "kill_worker" in x_actions


@pytest.mark.asyncio
async def test_action_quit_closes_store():
    """action_quit closes the SqliteRunStore before quitting."""
    config = MagicMock(spec=DispatchConfig)
    app = KewApp(config=config, db_path="")
    mock_store = MagicMock()
    app._store = mock_store
    with patch.object(type(app), "exit") as mock_exit:
        await app.action_quit()
    mock_store.close.assert_called_once()
    mock_exit.assert_called_once()


@pytest.mark.asyncio
async def test_action_quit_handles_no_store():
    """action_quit works when no store is opened."""
    config = MagicMock(spec=DispatchConfig)
    app = KewApp(config=config, db_path="")
    # _store is None by default — should not raise
    with patch.object(type(app), "exit") as mock_exit:
        await app.action_quit()
    mock_exit.assert_called_once()


class TestPollingCycle:
    @pytest.mark.asyncio
    async def test_workers_panel_renders_pending_runs(self, tmp_path):
        """After _poll_local, workers panel shows pending run data."""
        from kew.config import DispatchConfig, ProjectConfig
        from kew.run_store import RunRecord, SqliteRunStore
        from kew.tui.app import KewApp

        db_path = str(tmp_path / "runs.db")
        store = SqliteRunStore(db_path)
        now = datetime.now(UTC)
        store.save(
            RunRecord(
                run_id="w1",
                issue_num=135,
                branch="b1",
                status="pending",
                exit_code=-1,
                started_at=now,
                estimated_cost_usd=0.0,
                model="claude-sonnet-4",
            )
        )
        store.close()

        config = DispatchConfig(project=ProjectConfig(name="test-proj"))
        app = KewApp(config=config, db_path=db_path)
        async with app.run_test() as pilot:
            app._poll_local()
            await quiesce(pilot)
            # Check state directly since ListView.render() doesn't expose item text
            assert any(r.issue_num == 135 for r in app.state.pending_runs)
            # Also verify the panel has items
            from textual.widgets import ListView

            workers_lv = app.screen.query_one("#workers-panel", ListView)
            assert len(workers_lv._nodes) > 0

    @pytest.mark.asyncio
    async def test_state_tracks_completed_runs(self, tmp_path):
        """After _poll_local, state tracks today's completed runs."""
        from kew.config import DispatchConfig, ProjectConfig
        from kew.run_store import RunRecord, SqliteRunStore
        from kew.tui.app import KewApp

        db_path = str(tmp_path / "runs.db")
        store = SqliteRunStore(db_path)
        now = datetime.now(UTC)
        store.save(
            RunRecord(
                run_id="c1",
                issue_num=134,
                branch="b1",
                status="success",
                exit_code=0,
                started_at=now,
                estimated_cost_usd=2.10,
                pr_url="https://github.com/test/repo/pull/156",
            )
        )
        store.close()

        config = DispatchConfig(project=ProjectConfig(name="test-proj"))
        app = KewApp(config=config, db_path=db_path)
        async with app.run_test() as pilot:
            app._poll_local()
            await quiesce(pilot)
            # Check state directly
            assert any(r.issue_num == 134 for r in app.state.recent_runs)

    @pytest.mark.asyncio
    async def test_empty_state_renders_without_error(self, tmp_path):
        """Panels render correctly with no data."""
        from kew.config import DispatchConfig, ProjectConfig
        from kew.run_store import SqliteRunStore
        from kew.tui.app import KewApp

        db_path = str(tmp_path / "runs.db")
        SqliteRunStore(db_path).close()

        config = DispatchConfig(project=ProjectConfig(name="test-proj"))
        app = KewApp(config=config, db_path=db_path)
        async with app.run_test() as pilot:
            app._poll_local()
            await quiesce(pilot)
            assert app.screen.query_one("#queue-panel") is not None
            assert app.screen.query_one("#workers-panel") is not None
            assert app.screen.query_one("#reviews-panel") is not None

    @pytest.mark.asyncio
    async def test_queue_panel_renders_from_remote_poll(self, tmp_path):
        """After refresh_remote, queue panel shows issues."""
        from kew.config import DispatchConfig, ProjectConfig
        from kew.run_store import SqliteRunStore
        from kew.tui.app import KewApp

        db_path = str(tmp_path / "runs.db")
        SqliteRunStore(db_path).close()

        config = DispatchConfig(project=ProjectConfig(name="test-proj"))
        app = KewApp(config=config, db_path=db_path)
        async with app.run_test() as pilot:
            app.state.queue_ready = [
                {
                    "number": 142,
                    "title": "Fix auth regression",
                    "labels": [{"name": "P0"}],
                },
                {"number": 143, "title": "Add logging", "labels": [{"name": "P2"}]},
            ]
            app._update_widgets()
            await quiesce(pilot)
            # Check state directly since ListView.render() doesn't expose item text
            assert any(i["number"] == 142 for i in app.state.queue_ready)
            assert any(i["number"] == 143 for i in app.state.queue_ready)
            # Also verify the panel has items
            from textual.widgets import ListView

            queue_lv = app.screen.query_one("#queue-panel", ListView)
            assert len(queue_lv._nodes) == 2


class TestSnapshotCaching:
    @pytest.mark.asyncio
    async def test_workers_panel_inplace_update(self, tmp_path):
        """Workers panel should update text in-place when worker set is unchanged."""
        from kew.config import DispatchConfig, ProjectConfig
        from kew.run_store import RunRecord, SqliteRunStore
        from kew.tui.app import KewApp

        db_path = str(tmp_path / "runs.db")
        store = SqliteRunStore(db_path)
        now = datetime.now(UTC)
        store.save(
            RunRecord(
                run_id="w1",
                issue_num=200,
                branch="b1",
                status="pending",
                exit_code=-1,
                started_at=now,
                model="claude-sonnet-4",
            )
        )
        store.close()

        config = DispatchConfig(project=ProjectConfig(name="test-proj"))
        app = KewApp(config=config, db_path=db_path)
        async with app.run_test() as pilot:
            app._poll_local()
            await quiesce(pilot)

            # Snapshot should be populated
            assert app._workers_snapshot == [("w1", "worker")]

            from textual.widgets import ListView

            workers_lv = app.screen.query_one("#workers-panel", ListView)
            first_count = len(workers_lv._nodes)
            assert first_count > 0
            # Capture child widget identity to verify in-place update (not rebuild)
            first_child = workers_lv._nodes[0]
            first_child_id = id(first_child)

            # Spy on clear() to prove it's not called during in-place update
            clear_called = []
            original_clear = workers_lv.clear
            workers_lv.clear = lambda: clear_called.append(True) or original_clear()

            # Second poll — same worker set, in-place update
            app._poll_local()
            await quiesce(pilot)

            # clear() must not have been called
            assert not clear_called, "clear() called during in-place"
            # Same ListItem object proves items were reused, not recreated
            assert id(workers_lv._nodes[0]) == first_child_id
            assert app._workers_snapshot == [("w1", "worker")]

    @pytest.mark.asyncio
    async def test_workers_panel_rebuilds_on_new_worker(self, tmp_path):
        """Workers panel should full rebuild when a new worker appears."""
        from kew.config import DispatchConfig, ProjectConfig
        from kew.run_store import RunRecord, SqliteRunStore
        from kew.tui.app import KewApp

        db_path = str(tmp_path / "runs.db")
        store = SqliteRunStore(db_path)
        now = datetime.now(UTC)
        store.save(
            RunRecord(
                run_id="w1",
                issue_num=200,
                branch="b1",
                status="pending",
                exit_code=-1,
                started_at=now,
                model="claude-sonnet-4",
            )
        )

        config = DispatchConfig(project=ProjectConfig(name="test-proj"))
        app = KewApp(config=config, db_path=db_path)
        try:
            async with app.run_test() as pilot:
                app._poll_local()
                await quiesce(pilot)
                assert app._workers_snapshot == [("w1", "worker")]

                # Add a second worker
                store.save(
                    RunRecord(
                        run_id="w2",
                        issue_num=201,
                        branch="b2",
                        status="pending",
                        exit_code=-1,
                        started_at=now,
                        model="claude-sonnet-4",
                    )
                )
                app._poll_local()
                await quiesce(pilot)

                # Snapshot should now include both
                expected = {("w1", "worker"), ("w2", "worker")}
                assert set(app._workers_snapshot) == expected

                from textual.widgets import ListView

                workers_lv = app.screen.query_one("#workers-panel", ListView)
                assert len(workers_lv._nodes) == 2
        finally:
            store.close()
