"""Characterization tests for KewApp._update_widgets.

Golden/snapshot tests that pin the current rendering behavior exactly.
If behavior looks wrong, capture it as-is and leave a comment — do not fix.

One test per panel pins that panel's full composition: the queue sections in
``test_mixed_state_ready_failed_backlog``, the workers divider in
``test_worker_plus_recent_run_renders_divider``. The status-bar content and the
workers in-place-update path are pinned at this same layer by
``tests/test_tui_app.py`` (``TestLiveUpdates``, ``TestSnapshotCaching``); the
single-section near-copies that re-pinned those were removed in #2343.

The one case a full-composition pin cannot reach is a section separator
rendered with no ready items above it — ``test_mixed_state_ready_failed_backlog``
always populates ``queue_ready``. ``test_section_separator_independent_of_ready_items``
covers that input class for both separators.
"""

from datetime import UTC, datetime

import pytest
from support.tui_wait import no_ambient_polls as no_ambient_polls
from support.tui_wait import quiesce

from kew.config import DispatchConfig
from kew.run_store import RunRecord
from kew.tui.app import KewApp
from kew.tui.selection import (
    BacklogListItem,
    FailedListItem,
    HeldListItem,
    QueueListItem,
    RecentListItem,
    ReviewListItem,
    WorkerListItem,
)


def _make_run(
    run_id: str,
    *,
    status: str = "pending",
    issue_num: int = 1,
    title: str = "",
    estimated_cost_usd: float = 0.0,
) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        issue_num=issue_num,
        branch=f"branch-{run_id}",
        status=status,
        exit_code=0 if status == "success" else -1,
        started_at=datetime.now(UTC),
        title=title,
        estimated_cost_usd=estimated_cost_usd,
    )


def _make_issue(num: int, *, title: str = "Test Issue") -> dict:
    return {
        "number": num,
        "title": title,
        "labels": [],
        "createdAt": None,
        "_routed_runner": "",
        "_routed_model": "",
    }


def _make_pr(num: int, *, title: str = "PR title") -> dict:
    return {
        "number": num,
        "title": title,
        "labels": [],
        "statusCheckRollup": [],
    }


class TestUpdateWidgetsQueuePanel:
    @pytest.mark.asyncio
    async def test_empty_queue_shows_placeholder(self):
        """Empty queue renders the 'NO TARGETS IN QUEUE' placeholder."""
        app = KewApp(config=DispatchConfig(), db_path=":memory:")
        async with app.run_test() as pilot:
            app.state.queue_ready = []
            app.state.queue_failed = []
            app.state.queue_backlog = []
            app.state.queue_held = []
            app._update_widgets()
            await quiesce(pilot)

            from textual.widgets import ListView

            queue_lv = app.screen.query_one("#queue-panel", ListView)
            assert len(queue_lv._nodes) == 1
            assert queue_lv._nodes[0].disabled
            from textual.widgets import Static

            assert "NO TARGETS IN QUEUE" in str(
                queue_lv._nodes[0].query_one(Static).render()
            )

    @pytest.mark.asyncio
    async def test_held_issue_adds_approval_header_and_held_item(self):
        """Held issue renders: NEEDS APPROVAL header + HeldListItem."""
        app = KewApp(config=DispatchConfig(), db_path=":memory:")
        async with app.run_test() as pilot:
            app.state.queue_ready = []
            app.state.queue_failed = []
            app.state.queue_backlog = []
            app.state.queue_held = [
                {"number": 7, "estimate_usd": 1.50, "basis": "medium"}
            ]
            app._update_widgets()
            await quiesce(pilot)

            from textual.widgets import ListView

            queue_lv = app.screen.query_one("#queue-panel", ListView)
            # NEEDS APPROVAL header + 1 HeldListItem
            assert len(queue_lv._nodes) == 2
            assert queue_lv._nodes[0].disabled
            from textual.widgets import Static

            assert "NEEDS APPROVAL" in str(
                queue_lv._nodes[0].query_one(Static).render()
            )
            assert isinstance(queue_lv._nodes[1], HeldListItem)
            assert queue_lv._nodes[1].issue_num == 7

    @pytest.mark.asyncio
    async def test_multiple_ready_issues_order_preserved(self):
        """Multiple ready issues appear in order with no separator between them."""
        app = KewApp(config=DispatchConfig(), db_path=":memory:")
        async with app.run_test() as pilot:
            app.state.queue_ready = [
                _make_issue(1),
                _make_issue(2),
                _make_issue(3),
            ]
            app.state.queue_failed = []
            app.state.queue_backlog = []
            app.state.queue_held = []
            app._update_widgets()
            await quiesce(pilot)

            from textual.widgets import ListView

            queue_lv = app.screen.query_one("#queue-panel", ListView)
            assert len(queue_lv._nodes) == 3
            assert all(isinstance(n, QueueListItem) for n in queue_lv._nodes)
            assert [n.issue_num for n in queue_lv._nodes] == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_filter_hides_non_matching_items(self):
        """A filter on queue-panel hides items whose text does not match."""
        app = KewApp(config=DispatchConfig(), db_path=":memory:")
        async with app.run_test() as pilot:
            app.state.queue_ready = [
                _make_issue(1, title="auth bug"),
                _make_issue(2, title="fix rendering"),
            ]
            app.state.queue_failed = []
            app.state.queue_backlog = []
            app.state.queue_held = []
            app.state.filter_text["queue-panel"] = "auth"
            app._update_widgets()
            await quiesce(pilot)

            from textual.widgets import ListView

            queue_lv = app.screen.query_one("#queue-panel", ListView)
            assert len(queue_lv._nodes) == 1
            assert isinstance(queue_lv._nodes[0], QueueListItem)
            assert queue_lv._nodes[0].issue_num == 1

    @pytest.mark.asyncio
    async def test_mixed_state_ready_failed_backlog(self):
        """Ready + failed + backlog renders all sections in order.

        The one pin for queue composition: every section's separator, item type
        and item identity (#2343).
        """
        app = KewApp(config=DispatchConfig(), db_path=":memory:")
        async with app.run_test() as pilot:
            app.state.queue_ready = [_make_issue(1)]
            app.state.queue_failed = [_make_issue(2)]
            app.state.queue_backlog = [_make_issue(3)]
            app.state.queue_held = []
            app._update_widgets()
            await quiesce(pilot)

            from textual.widgets import ListView

            queue_lv = app.screen.query_one("#queue-panel", ListView)
            # ready | failed-sep | failed | backlog-sep | backlog = 5 nodes
            assert len(queue_lv._nodes) == 5
            assert isinstance(queue_lv._nodes[0], QueueListItem)
            assert queue_lv._nodes[0].issue_num == 1
            assert queue_lv._nodes[1].disabled  # failed separator
            from textual.widgets import Static

            assert "failed" in str(queue_lv._nodes[1].query_one(Static).render())
            assert isinstance(queue_lv._nodes[2], FailedListItem)
            assert queue_lv._nodes[2].issue_num == 2
            assert queue_lv._nodes[3].disabled  # backlog separator
            assert "backlog" in str(queue_lv._nodes[3].query_one(Static).render())
            assert isinstance(queue_lv._nodes[4], BacklogListItem)
            assert queue_lv._nodes[4].issue_num == 3

    @pytest.mark.parametrize(
        ("section", "item_type", "separator_word"),
        [
            ("queue_failed", FailedListItem, "failed"),
            ("queue_backlog", BacklogListItem, "backlog"),
        ],
    )
    @pytest.mark.asyncio
    async def test_section_separator_independent_of_ready_items(
        self, section, item_type, separator_word
    ):
        """A section's separator comes from that section alone, not from ready.

        ``test_mixed_state_ready_failed_backlog`` always populates
        ``queue_ready``, so it cannot tell a separator guarded on its own
        section from one additionally guarded on ``queue_ready``. This pins the
        isolated-section case for both separators (#2343).
        """
        app = KewApp(config=DispatchConfig(), db_path=":memory:")
        async with app.run_test() as pilot:
            app.state.queue_ready = []
            app.state.queue_failed = []
            app.state.queue_backlog = []
            app.state.queue_held = []
            setattr(app.state, section, [_make_issue(10)])
            app._update_widgets()
            await quiesce(pilot)

            from textual.widgets import ListView, Static

            queue_lv = app.screen.query_one("#queue-panel", ListView)
            # separator (disabled) + the single section item, nothing above it
            assert len(queue_lv._nodes) == 2
            assert queue_lv._nodes[0].disabled
            assert separator_word in str(queue_lv._nodes[0].query_one(Static).render())
            assert isinstance(queue_lv._nodes[1], item_type)
            assert queue_lv._nodes[1].issue_num == 10


class TestUpdateWidgetsWorkersPanel:
    @pytest.mark.asyncio
    async def test_empty_workers_shows_placeholder(self):
        """Empty workers panel renders the 'DECK CLEAR' placeholder."""
        app = KewApp(config=DispatchConfig(), db_path=":memory:")
        async with app.run_test() as pilot:
            app.state.pending_runs = []
            app.state.recent_runs = []
            app._update_widgets()
            await quiesce(pilot)

            from textual.widgets import ListView

            workers_lv = app.screen.query_one("#workers-panel", ListView)
            assert len(workers_lv._nodes) == 1
            assert workers_lv._nodes[0].disabled
            from textual.widgets import Static

            assert "DECK CLEAR" in str(workers_lv._nodes[0].query_one(Static).render())

    @pytest.mark.asyncio
    async def test_recent_run_only_shows_no_divider(self):
        """A completed run with no active workers appears without a divider."""
        app = KewApp(config=DispatchConfig(), db_path=":memory:")
        async with app.run_test() as pilot:
            run = _make_run("r1", status="success", issue_num=10)
            app.state.pending_runs = []
            app.state.recent_runs = [run]
            app._update_widgets()
            await quiesce(pilot)

            from textual.widgets import ListView

            workers_lv = app.screen.query_one("#workers-panel", ListView)
            # No divider — just the RecentListItem
            assert len(workers_lv._nodes) == 1
            assert isinstance(workers_lv._nodes[0], RecentListItem)
            assert workers_lv._nodes[0].run_id == "r1"

    @pytest.mark.asyncio
    async def test_worker_plus_recent_run_renders_divider(self):
        """Active worker + recent run: WorkerListItem + divider + RecentListItem."""
        app = KewApp(config=DispatchConfig(), db_path=":memory:")
        async with app.run_test() as pilot:
            active = _make_run("w1", status="pending", issue_num=1)
            recent = _make_run("r1", status="success", issue_num=2)
            app.state.pending_runs = [active]
            app.state.recent_runs = [recent]
            app._update_widgets()
            await quiesce(pilot)

            from textual.widgets import ListView

            workers_lv = app.screen.query_one("#workers-panel", ListView)
            # WorkerListItem + disabled divider + RecentListItem
            assert len(workers_lv._nodes) == 3
            assert isinstance(workers_lv._nodes[0], WorkerListItem)
            assert workers_lv._nodes[0].run_id == "w1"
            assert workers_lv._nodes[1].disabled
            from textual.widgets import Static

            assert "completed" in str(workers_lv._nodes[1].query_one(Static).render())
            assert isinstance(workers_lv._nodes[2], RecentListItem)
            assert workers_lv._nodes[2].run_id == "r1"

    @pytest.mark.asyncio
    async def test_recent_runs_capped_at_ten(self):
        """Workers panel shows at most 10 recent runs."""
        app = KewApp(config=DispatchConfig(), db_path=":memory:")
        async with app.run_test() as pilot:
            app.state.pending_runs = []
            app.state.recent_runs = [
                _make_run(f"r{i}", status="success", issue_num=i) for i in range(15)
            ]
            app._update_widgets()
            await quiesce(pilot)

            from textual.widgets import ListView

            workers_lv = app.screen.query_one("#workers-panel", ListView)
            assert len(workers_lv._nodes) == 10


class TestUpdateWidgetsReviewsPanel:
    @pytest.mark.asyncio
    async def test_empty_reviews_shows_placeholder(self):
        """Empty reviews panel renders the 'NO PENDING REVIEWS' placeholder."""
        app = KewApp(config=DispatchConfig(), db_path=":memory:")
        async with app.run_test() as pilot:
            app.state.review_prs = []
            app._update_widgets()
            await quiesce(pilot)

            from textual.widgets import ListView

            reviews_lv = app.screen.query_one("#reviews-panel", ListView)
            assert len(reviews_lv._nodes) == 1
            assert reviews_lv._nodes[0].disabled
            from textual.widgets import Static

            assert "NO PENDING REVIEWS" in str(
                reviews_lv._nodes[0].query_one(Static).render()
            )

    @pytest.mark.asyncio
    async def test_single_review_pr_adds_review_list_item(self):
        """One review PR produces exactly one ReviewListItem."""
        app = KewApp(config=DispatchConfig(), db_path=":memory:")
        async with app.run_test() as pilot:
            app.state.review_prs = [_make_pr(101)]
            app._update_widgets()
            await quiesce(pilot)

            from textual.widgets import ListView

            reviews_lv = app.screen.query_one("#reviews-panel", ListView)
            assert len(reviews_lv._nodes) == 1
            assert isinstance(reviews_lv._nodes[0], ReviewListItem)
            assert reviews_lv._nodes[0].pr_number == 101

    @pytest.mark.asyncio
    async def test_reviews_capped_at_twenty(self):
        """Reviews panel shows at most 20 items even when more PRs exist."""
        app = KewApp(config=DispatchConfig(), db_path=":memory:")
        async with app.run_test() as pilot:
            app.state.review_prs = [_make_pr(i) for i in range(25)]
            app._update_widgets()
            await quiesce(pilot)

            from textual.widgets import ListView

            reviews_lv = app.screen.query_one("#reviews-panel", ListView)
            assert len(reviews_lv._nodes) == 20


class TestUpdateWidgetsStatusBar:
    @pytest.mark.asyncio
    async def test_status_bar_fleet_stopped_flag(self):
        """Status bar reflects fleet_stopped state when the latch is set."""
        app = KewApp(config=DispatchConfig(), db_path=":memory:")
        async with app.run_test() as pilot:
            app.state.fleet_stopped = {"reason": "manual stop", "by": "user"}
            app._update_widgets()
            await quiesce(pilot)

            bar = app.screen.query_one("#status-bar")
            # format_status_bar must incorporate the stopped latch visually
            bar_text = str(bar.content)
            assert "FLEET STOPPED" in bar_text
            assert "manual stop" in bar_text


class TestUpdateWidgetsSnapshotCache:
    @pytest.mark.asyncio
    async def test_same_queue_data_skips_rebuild(self):
        """Calling _update_widgets twice with identical data skips a rebuild."""
        app = KewApp(config=DispatchConfig(), db_path=":memory:")
        async with app.run_test() as pilot:
            app.state.queue_ready = [_make_issue(1)]
            app.state.queue_failed = []
            app.state.queue_backlog = []
            app.state.queue_held = []
            app._update_widgets()
            await quiesce(pilot)

            from textual.widgets import ListView

            queue_lv = app.screen.query_one("#queue-panel", ListView)
            node_id_before = id(queue_lv._nodes[0])

            app._update_widgets()
            await quiesce(pilot)

            queue_lv2 = app.screen.query_one("#queue-panel", ListView)
            node_id_after = id(queue_lv2._nodes[0])
            # Same node object — list was not cleared and rebuilt
            assert node_id_before == node_id_after

    @pytest.mark.asyncio
    async def test_changed_queue_data_rebuilds_list(self):
        """Adding a new issue to queue_ready triggers a list rebuild."""
        app = KewApp(config=DispatchConfig(), db_path=":memory:")
        async with app.run_test() as pilot:
            app.state.queue_ready = [_make_issue(1)]
            app.state.queue_failed = []
            app.state.queue_backlog = []
            app.state.queue_held = []
            app._update_widgets()
            await quiesce(pilot)

            from textual.widgets import ListView

            queue_lv = app.screen.query_one("#queue-panel", ListView)
            assert len(queue_lv._nodes) == 1
            node_before = queue_lv._nodes[0]

            app.state.queue_ready = [_make_issue(1), _make_issue(2)]
            app._update_widgets()
            await quiesce(pilot)

            queue_lv2 = app.screen.query_one("#queue-panel", ListView)
            assert len(queue_lv2._nodes) == 2
            # Rebuilt: new node objects with correct content and order
            assert id(queue_lv2._nodes[0]) != id(node_before)
            assert isinstance(queue_lv2._nodes[0], QueueListItem)
            assert queue_lv2._nodes[0].issue_num == 1
            assert isinstance(queue_lv2._nodes[1], QueueListItem)
            assert queue_lv2._nodes[1].issue_num == 2
