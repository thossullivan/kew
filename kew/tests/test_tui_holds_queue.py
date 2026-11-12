"""Holds & Queue TUI pane + verbs (#685, Group 5).

Proves: the pane renders epic holds / ready-queue issues / orphan PRs from
store fixtures; each verb (retry-node, queue-issue, ack-orphan, resume-fleet)
reaches ``kew.service`` and never touches ``run_store``/gh directly.
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from rich.text import Text
from support.tui_wait import no_ambient_polls as no_ambient_polls
from support.tui_wait import quiesce, wait_for


def _hold_row(issue_num: int, epic_ref: str, reason: str, item_id: str | None = None):
    """One ``epic_holds`` row as ``TuiState.epic_holds`` carries it.

    ``item_id`` defaults to the numeric id; pass it (with ``issue_num=0``) for a
    non-numeric tracker's row, which is exactly how a Linear hold is stored.
    """
    from kew.run_store import EpicHold

    return EpicHold(
        issue_num=issue_num,
        epic_ref=epic_ref,
        reason=reason,
        held_at=datetime(2026, 1, 1, tzinfo=UTC),
        item_id=item_id if item_id is not None else str(issue_num),
    )


def _seed_store(db_path: str) -> None:
    """Insert one epic hold + one orphan PR into a store at db_path."""
    from kew.run_store import SqliteRunStore

    store = SqliteRunStore(db_path)
    store.hold_epic_node("epic-1", 101, "ci_pending")
    store.sync_orphan_prs([(55, "https://example/55", "agent/issue-55", "no-record")])
    store.close()


class TestBuildItems:
    def test_three_sections_from_state(self):
        from kew.run_store import EpicHold, OrphanPRRecord
        from kew.tui.screens.holds_queue import build_holds_queue_items
        from kew.tui.selection import (
            EpicHoldListItem,
            OrphanListItem,
            ReadyQueueListItem,
        )

        state = MagicMock()
        state.work_view.return_value = None  # projection not read in this fixture
        state.work_unreadable.return_value = []
        state.epic_holds = [
            EpicHold(
                issue_num=101,
                epic_ref="epic-1",
                reason="ci_pending",
                held_at=datetime.now(UTC),
            )
        ]
        state.queue_ready = [{"number": 7, "title": "Do the thing"}]
        state.orphan_prs = [
            OrphanPRRecord(
                pr_number=55,
                url="u",
                branch="agent/issue-55",
                reason="no-record",
                detected_at=datetime.now(UTC),
                acked_at=None,
            )
        ]

        items = build_holds_queue_items(state)
        assert any(
            isinstance(i, EpicHoldListItem)
            and i.issue_num == 101
            and i.epic_ref == "epic-1"
            for i in items
        )
        assert any(
            isinstance(i, ReadyQueueListItem) and i.issue_num == 7 for i in items
        )
        assert any(isinstance(i, OrphanListItem) and i.pr_number == 55 for i in items)

    def test_none_state_renders_headers_only(self):
        from kew.tui.screens.holds_queue import build_holds_queue_items
        from kew.tui.selection import (
            EpicHoldListItem,
            OrphanListItem,
            ReadyQueueListItem,
        )

        items = build_holds_queue_items(None)
        # Three section headers + three placeholders; no data rows.
        assert len(items) >= 3
        assert not any(
            isinstance(i, (EpicHoldListItem, ReadyQueueListItem, OrphanListItem))
            for i in items
        )


class TestPaneRender:
    @pytest.mark.asyncio
    async def test_pane_renders_sections_from_store(self, tmp_path):
        from textual.widgets import ListView

        from kew.config import DispatchConfig
        from kew.tui.app import KewApp
        from kew.tui.screens.holds_queue import HoldsQueueScreen
        from kew.tui.selection import (
            EpicHoldListItem,
            OrphanListItem,
            ReadyQueueListItem,
        )

        db_path = str(tmp_path / "runs.db")
        _seed_store(db_path)

        app = KewApp(config=DispatchConfig(), db_path=db_path)
        async with app.run_test() as pilot:
            app._poll_local()  # fills epic_holds + orphan_prs from the store
            app.state.queue_ready = [{"number": 7, "title": "Do the thing"}]
            await pilot.press("5")
            await wait_for(
                pilot,
                lambda: isinstance(app.screen, HoldsQueueScreen),
                "the holds & queue screen to become active",
            )
            await quiesce(pilot)

            assert isinstance(app.screen, HoldsQueueScreen)
            lv = app.screen.query_one("#holds-queue-panel", ListView)
            children = list(lv.children)
            assert any(
                isinstance(c, EpicHoldListItem) and c.issue_num == 101 for c in children
            )
            assert any(
                isinstance(c, ReadyQueueListItem) and c.issue_num == 7 for c in children
            )
            assert any(
                isinstance(c, OrphanListItem) and c.pr_number == 55 for c in children
            )

    @pytest.mark.asyncio
    async def test_escape_returns_to_cockpit(self, tmp_path):
        from kew.config import DispatchConfig
        from kew.tui.app import KewApp
        from kew.tui.screens.cockpit import CockpitScreen
        from kew.tui.screens.holds_queue import HoldsQueueScreen

        db_path = str(tmp_path / "runs.db")
        _seed_store(db_path)
        app = KewApp(config=DispatchConfig(), db_path=db_path)
        async with app.run_test() as pilot:
            await pilot.press("5")
            await wait_for(
                pilot,
                lambda: isinstance(app.screen, HoldsQueueScreen),
                "the holds & queue screen to become active",
            )
            await pilot.press("escape")
            await wait_for(
                pilot,
                lambda: isinstance(app.screen, CockpitScreen),
                "the cockpit to become active again",
            )
            assert app.screen.query_one("#queue-panel") is not None


def _dispatchable_view(item_id: str):
    """A composed view whose derived safe action is a first dispatch."""
    from types import SimpleNamespace

    return SimpleNamespace(
        identity=SimpleNamespace(
            item_id=item_id, item_ref=f"#{item_id}", source_tracker="github"
        ),
        exception=SimpleNamespace(state="not_started", detail="", blocking=False),
        safest_action=SimpleNamespace(
            action="dispatch", reason="nothing has been attempted yet"
        ),
        actions=[
            SimpleNamespace(
                action="dispatch",
                intent="queue this item for its first attempt",
                target_kind="item",
                target_ref=item_id,
                safest=True,
                requires_reason=False,
            )
        ],
        view_revision="wv1:abc",
    )


class TestVerbWiring:
    """Each verb pushes a ConfirmDialog for the highlighted target."""

    def test_retry_node_pushes_confirm(self):
        from kew.tui.actions import handle_retry_node

        app = MagicMock()
        app.screen = MagicMock()
        # Held for a reason the driver's overlay does NOT route to HELD: the
        # node still reads FAILED, so retry is its recovery path.
        app.state.epic_holds = [
            _hold_row(101, "epic-1", "credential_issuer_refused: unsupported route")
        ]
        with patch(
            "kew.tui.actions.get_selected_epic_hold", return_value=(101, "epic-1")
        ):
            handle_retry_node(app)
        app.push_screen.assert_called_once()
        assert "101" in app.push_screen.call_args[0][0].prompt

    def test_retry_node_no_selection_is_noop(self):
        from kew.tui.actions import handle_retry_node

        app = MagicMock()
        app.screen = MagicMock()
        with patch("kew.tui.actions.get_selected_epic_hold", return_value=None):
            handle_retry_node(app)
        app.push_screen.assert_not_called()

    def test_retry_node_on_a_driver_honored_hold_names_unhold_instead(self):
        """#2154 review: the pane's only rows are holds, so `r` must not
        push a dialog whose worker can only report a refusal the row already
        knew about — say so up front, naming the command that does release it."""
        from kew.tui.actions import handle_retry_node

        app = MagicMock()
        app.screen = MagicMock()
        app.state.epic_holds = [_hold_row(101, "epic-1", "review_unparseable")]
        with patch(
            "kew.tui.actions.get_selected_epic_hold", return_value=(101, "epic-1")
        ):
            handle_retry_node(app)
        app.push_screen.assert_not_called()
        app.notify.assert_called_once()
        message = app.notify.call_args[0][0]
        assert "review_unparseable" in message
        assert "kew epic unhold epic-1 101" in message
        assert app.notify.call_args[1]["severity"] == "warning"

    def test_retry_precheck_keys_on_the_item_id_not_the_zero_sentinel(self):
        """#2154 review: every Linear hold in an epic stores ``issue_num=0``,
        so a pre-check matched on that number reads the FIRST row's reason for
        whichever node the operator actually selected — here it would refuse a
        retry the selected node is entitled to."""
        from kew.tui.actions import handle_retry_node
        from kew.tui.selection import WorkItemRef

        app = MagicMock()
        app.screen = MagicMock()
        app.state.epic_holds = [
            _hold_row(0, "epic-1", "review_unparseable", item_id="SFD-1"),
            _hold_row(0, "epic-1", "credential_issuer_refused: x", item_id="SFD-2"),
        ]
        with (
            patch("kew.tui.actions.get_selected_epic_hold", return_value=(0, "epic-1")),
            patch(
                "kew.tui.actions.get_selected_work_ref",
                return_value=WorkItemRef(item_id="SFD-2", tracker="linear"),
            ),
        ):
            handle_retry_node(app)
        app.notify.assert_not_called()
        app.push_screen.assert_called_once()

    def test_retry_refusal_names_the_selected_linear_item_not_hash_zero(self):
        """The refusal has to name an id `kew epic unhold` accepts: ``#0`` is
        neither this node nor a real GitHub issue."""
        from kew.tui.actions import handle_retry_node
        from kew.tui.selection import WorkItemRef

        app = MagicMock()
        app.screen = MagicMock()
        app.state.epic_holds = [
            _hold_row(0, "epic-1", "credential_issuer_refused: x", item_id="SFD-1"),
            _hold_row(0, "epic-1", "review_rounds_exhausted", item_id="SFD-2"),
        ]
        with (
            patch("kew.tui.actions.get_selected_epic_hold", return_value=(0, "epic-1")),
            patch(
                "kew.tui.actions.get_selected_work_ref",
                return_value=WorkItemRef(item_id="SFD-2", tracker="linear"),
            ),
        ):
            handle_retry_node(app)
        app.push_screen.assert_not_called()
        message = app.notify.call_args[0][0]
        assert "review_rounds_exhausted" in message
        assert "kew epic unhold epic-1 SFD-2" in message
        assert "#0" not in message

    def test_retry_node_refusal_toast_escapes_an_untrusted_reason(self):
        """An ``admission_refused`` reason carries a detached child's stderr
        tail; ``notify`` renders Rich markup, so it must not reach it raw."""
        from kew.tui.actions import handle_retry_node

        app = MagicMock()
        app.screen = MagicMock()
        app.state.epic_holds = [
            _hold_row(101, "epic-1", "admission_refused: [bold]boom\nsecond line")
        ]
        with patch(
            "kew.tui.actions.get_selected_epic_hold", return_value=(101, "epic-1")
        ):
            handle_retry_node(app)
        message = app.notify.call_args[0][0]
        assert "\n" not in message
        # Rendering the toast must yield the literal reason, not bold markup.
        rendered = Text.from_markup(message)
        assert "[bold]boom second line" in rendered.plain
        assert rendered.spans == []

    def test_queue_issue_pushes_confirm(self):
        from kew.tui.actions import handle_queue_issue
        from kew.tui.selection import WorkItemRef

        app = MagicMock()
        app.screen = MagicMock()
        app.state.work_view.return_value = _dispatchable_view("7")
        with patch(
            "kew.tui.actions.get_selected_work_ref",
            return_value=WorkItemRef(item_id="7", issue_num=7),
        ):
            handle_queue_issue(app)
        app.push_screen.assert_called_once()
        assert "7" in app.push_screen.call_args[0][0].prompt

    def test_queue_issue_no_selection_is_noop(self):
        from kew.tui.actions import handle_queue_issue

        app = MagicMock()
        app.screen = MagicMock()
        with patch("kew.tui.actions.get_selected_work_ref", return_value=None):
            handle_queue_issue(app)
        app.push_screen.assert_not_called()

    def test_ack_orphan_pushes_confirm(self):
        from kew.tui.actions import handle_ack_orphan

        app = MagicMock()
        app.screen = MagicMock()
        with patch("kew.tui.actions.get_selected_orphan", return_value=55):
            handle_ack_orphan(app)
        app.push_screen.assert_called_once()
        assert "55" in app.push_screen.call_args[0][0].prompt

    def test_ack_orphan_no_selection_is_noop(self):
        from kew.tui.actions import handle_ack_orphan

        app = MagicMock()
        app.screen = MagicMock()
        with patch("kew.tui.actions.get_selected_orphan", return_value=None):
            handle_ack_orphan(app)
        app.push_screen.assert_not_called()

    def test_resume_fleet_pushes_confirm(self):
        from kew.tui.actions import handle_resume_fleet

        app = MagicMock()
        app.screen = MagicMock()
        handle_resume_fleet(app)
        app.push_screen.assert_called_once()


class TestVerbsHitServiceLayer:
    """The background coroutines reach kew.service only — never run_store/gh."""

    def test_retry_node_calls_service(self):
        from kew.tui.actions import _do_retry_node

        app = MagicMock()
        app.config = MagicMock()
        with patch("kew.service.verbs.epic_retry_gated") as mock_verb:
            asyncio.run(_do_retry_node(app, "epic-1", 101))
        mock_verb.assert_called_once()
        assert mock_verb.call_args[0][0] is app._store
        assert mock_verb.call_args[1]["epic_ref"] == "epic-1"
        assert mock_verb.call_args[1]["item_id"] == "101"
        assert mock_verb.call_args[1]["source"] == "tui"
        # No direct store access — the store is only passed through to the verb.
        assert app._store.method_calls == []
        app.notify.assert_called_once()

    def test_retry_node_worker_reports_a_real_refusal_unmocked(self, tmp_path):
        """#2154 review: the mocked test above cannot see a verb that now
        refuses. Drive the worker through the REAL verb and a real store so a
        retry the holds pane can never complete goes red here."""
        from kew.config import DispatchConfig
        from kew.run_store import SqliteRunStore
        from kew.tui.actions import _do_retry_node

        db = str(tmp_path / "dispatch.db")
        config = DispatchConfig()
        config.project.repo = "o/r"
        app = MagicMock()
        app.config = config
        # The TUI opens its store exactly this way (kew.tui.app), because the
        # verb workers run on ``asyncio.to_thread``.
        with SqliteRunStore(db, check_same_thread=False) as store:
            store.hold_epic_node_item("epic-1", "101", "review_unparseable")
            app._store = store
            with patch("kew.labels.run") as gh:
                gh.return_value = MagicMock(returncode=0, stdout="", stderr="")
                asyncio.run(_do_retry_node(app, "epic-1", 101))
            gh.assert_not_called()
            assert store.list_epic_retries("epic-1") == []

        app.notify.assert_called_once()
        assert app.notify.call_args[1]["severity"] == "error"
        assert "review_unparseable" in app.notify.call_args[0][0]

    def test_retry_node_worker_failure_toast_escapes_the_reason(self, tmp_path):
        """#2154 review: the refusal the worker reports carries the same
        untrusted hold reason the pre-check escapes — a bracketed stderr tail
        reaching ``notify`` raw raises MarkupError inside the toast's render,
        so the operator loses the message the refusal exists to deliver."""
        from kew.config import DispatchConfig
        from kew.run_store import SqliteRunStore
        from kew.tui.actions import _do_retry_node

        db = str(tmp_path / "dispatch.db")
        config = DispatchConfig()
        config.project.repo = "o/r"
        app = MagicMock()
        app.config = config
        with SqliteRunStore(db, check_same_thread=False) as store:
            # A detached child's stderr tail, verbatim: `[/bold]` is a closing
            # tag with nothing to close, which is a MarkupError, not a style.
            store.hold_epic_node_item("epic-1", "101", "admission_refused: [/bold]x")
            app._store = store
            asyncio.run(_do_retry_node(app, "epic-1", 101))

        assert app.notify.call_args[1]["severity"] == "error"
        message = app.notify.call_args[0][0]
        # Rendering the toast must yield the literal reason, not raise on it.
        rendered = Text.from_markup(message)
        assert "admission_refused: [/bold]x" in rendered.plain
        assert rendered.spans == []

    def test_retry_node_worker_still_retries_an_unhonored_hold_unmocked(self, tmp_path):
        """A ``credential_issuer_refused`` row is not a HELD node: the overlay
        ignores it, so the pane's retry must still reach the store."""
        from kew.config import DispatchConfig
        from kew.run_store import SqliteRunStore
        from kew.tui.actions import _do_retry_node

        db = str(tmp_path / "dispatch.db")
        config = DispatchConfig()
        config.project.repo = "o/r"
        app = MagicMock()
        app.config = config
        with SqliteRunStore(db, check_same_thread=False) as store:
            store.hold_epic_node_item("epic-1", "101", "credential_issuer_refused: x")
            app._store = store
            with patch("kew.labels.run") as gh:
                gh.return_value = MagicMock(returncode=0, stdout="", stderr="")
                asyncio.run(_do_retry_node(app, "epic-1", 101))
            assert [r.item_id for r in store.list_epic_retries("epic-1")] == ["101"]

        assert app.notify.call_args[0][0] == "Retried epic node #101"

    def test_retry_node_worker_refuses_an_excluded_actor_unmocked(
        self, tmp_path, monkeypatch
    ):
        """#2552: the TUI retry binds the console's approver gate. An actor
        outside ``[approval].approvers`` is refused after the confirm with no
        marker, event, reservation release or label edit."""
        from kew.config import DispatchConfig
        from kew.run_store import SqliteRunStore
        from kew.tui.actions import _do_retry_node

        monkeypatch.setenv("KEW_ACTOR", "mallory")
        config = DispatchConfig()
        config.project.repo = "o/r"
        config.approval.approvers = ["ada"]
        app = MagicMock()
        app.config = config
        with SqliteRunStore(
            str(tmp_path / "dispatch.db"), check_same_thread=False
        ) as store:
            store.reserve_epic_node_item("epic-1", "101", "run-fail-1")
            app._store = store
            with patch("kew.labels.run") as gh:
                asyncio.run(_do_retry_node(app, "epic-1", 101))
            gh.assert_not_called()
            assert store.list_epic_retries("epic-1") == []
            assert [r.item_id for r in store.list_epic_reservations("epic-1")] == [
                "101"
            ]
            assert store.list_audit_events(event="epic_node_retry") == []

        assert app.notify.call_args[1]["severity"] == "error"
        rendered = Text.from_markup(app.notify.call_args[0][0]).plain
        assert "'mallory' is not in [approval].approvers" in rendered

    def test_retry_node_worker_binds_the_policy_for_an_approver(
        self, tmp_path, monkeypatch
    ):
        """#2552: an approver's TUI retry event carries the bound policy hash."""
        from kew.config import DispatchConfig
        from kew.run_store import SqliteRunStore
        from kew.tui.actions import _do_retry_node

        monkeypatch.setenv("KEW_ACTOR", "ada")
        config = DispatchConfig()
        config.project.repo = "o/r"
        config.approval.approvers = ["ada"]
        app = MagicMock()
        app.config = config
        with SqliteRunStore(
            str(tmp_path / "dispatch.db"), check_same_thread=False
        ) as store:
            app._store = store
            with patch("kew.labels.run") as gh:
                gh.return_value = MagicMock(returncode=0, stdout="", stderr="")
                asyncio.run(_do_retry_node(app, "epic-1", 101))
            events = store.list_audit_events(event="epic_node_retry")

        assert app.notify.call_args[0][0] == "Retried epic node #101"
        assert len(events) == 1
        assert events[0].actor_id == "ada"
        assert events[0].policy_hash

    def test_queue_issue_calls_the_guarded_work_action(self):
        """Queueing carries the item id and the revision it was decided on."""
        from kew.tui.actions import _do_work_action

        app = MagicMock()
        app.config = MagicMock()
        with patch("kew.service.verbs.perform_work_action") as mock_verb:
            asyncio.run(
                _do_work_action(
                    app,
                    item_id="7",
                    action="dispatch",
                    expected_revision="wv1:abc",
                )
            )
        mock_verb.assert_called_once()
        assert mock_verb.call_args[0][0] is app._store
        assert mock_verb.call_args[1]["item_id"] == "7"
        assert mock_verb.call_args[1]["action"] == "dispatch"
        assert mock_verb.call_args[1]["expected_revision"] == "wv1:abc"
        assert mock_verb.call_args[1]["source"] == "tui"
        assert app._store.method_calls == []

    def test_ack_orphan_calls_service(self):
        from kew.tui.actions import _do_ack_orphan

        app = MagicMock()
        app.config = MagicMock()
        with patch("kew.service.verbs.ack_orphan_pr", return_value=True) as mock_verb:
            asyncio.run(_do_ack_orphan(app, 55))
        mock_verb.assert_called_once()
        assert mock_verb.call_args[0][0] is app._store
        assert mock_verb.call_args[1]["pr_number"] == 55
        assert mock_verb.call_args[1]["source"] == "tui"
        assert app._store.method_calls == []

    def test_resume_fleet_calls_service(self):
        from kew.tui.actions import _do_resume_fleet

        app = MagicMock()
        app.config = MagicMock()
        with patch("kew.service.verbs.resume_fleet", return_value=True) as mock_verb:
            asyncio.run(_do_resume_fleet(app))
        mock_verb.assert_called_once()
        assert mock_verb.call_args[0][0] is app._store
        assert mock_verb.call_args[1]["config"] is app.config
        assert app._store.method_calls == []

    def test_ack_orphan_untracked_warns(self):
        from kew.tui.actions import _do_ack_orphan

        app = MagicMock()
        app.config = MagicMock()
        with patch("kew.service.verbs.ack_orphan_pr", return_value=False):
            asyncio.run(_do_ack_orphan(app, 999))
        assert app.notify.call_args[1]["severity"] == "warning"


class TestServiceResumeFleet:
    def test_delegates_to_fleet(self):
        from kew.service.verbs import resume_fleet

        store = MagicMock()
        config = MagicMock()
        with patch("kew.fleet.resume_fleet", return_value=True) as mock_resume:
            assert resume_fleet(store, config=config) is True
        mock_resume.assert_called_once_with(store, config)


class TestStateHoldsAndOrphans:
    def test_refresh_local_populates(self, tmp_path):
        from kew.config import DispatchConfig
        from kew.run_store import SqliteRunStore
        from kew.tui.state import TuiState

        db_path = str(tmp_path / "runs.db")
        _seed_store(db_path)
        store = SqliteRunStore(db_path)
        state = TuiState(config=DispatchConfig(), store=store)
        state.refresh_local()
        assert any(h.issue_num == 101 for h in state.epic_holds)
        assert any(o.pr_number == 55 for o in state.orphan_prs)
        store.close()


# --- I22: the pane renders canonical work truth, not numeric shims ----------


def _hold(issue_num: int, item_id: str = "0"):
    from kew.run_store import EpicHold

    return EpicHold(
        issue_num=issue_num,
        epic_ref="epic-1",
        reason="ci_pending",
        held_at=datetime.now(UTC),
        item_id=item_id,
    )


def _state_with(view, *, holds=(), ready=(), unreadable=()):
    state = MagicMock()
    state.epic_holds = list(holds)
    state.queue_ready = list(ready)
    state.orphan_prs = []
    state.work_view.return_value = view
    state.work_unreadable.return_value = list(unreadable)
    state.work_revision.return_value = "wp1:abcdef0123456789"
    return state


def _fake_view(**kwargs):
    """A minimal stand-in for a composed WorkViewDTO."""
    from types import SimpleNamespace

    defaults = {
        "identity": SimpleNamespace(
            item_id="SFD-12", item_ref="SFD-12", source_tracker="linear"
        ),
        "execution": SimpleNamespace(state="completed"),
        "outcome": SimpleNamespace(state="in_progress"),
        "exception": SimpleNamespace(
            state="awaiting_publication",
            detail="implementation preserved on agent/issue-sfd-12",
            blocking=False,
        ),
        "safest_action": SimpleNamespace(
            action="finalize_publication", reason="publish that same artifact"
        ),
        "observations": [],
        "view_revision": "wv1:deadbeef",
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


class TestCanonicalWorkRows:
    def test_a_linear_hold_shows_its_opaque_id_and_never_hash_zero(self):
        from kew.tui.screens.holds_queue import build_holds_queue_items
        from kew.tui.selection import EpicHoldListItem

        state = _state_with(_fake_view(), holds=[_hold(0, "SFD-12")])
        rows = [
            i for i in build_holds_queue_items(state) if isinstance(i, EpicHoldListItem)
        ]

        assert len(rows) == 1
        assert rows[0].work_ref.item_id == "SFD-12"
        assert rows[0].issue_num == 0  # the legacy verb path keeps its number
        text = _row_text(rows[0])
        assert "SFD-12" in text
        assert "#0" not in text

    def test_a_held_row_shows_the_canonical_safest_action(self):
        from kew.tui.screens.holds_queue import build_holds_queue_items
        from kew.tui.selection import EpicHoldListItem

        state = _state_with(_fake_view(), holds=[_hold(101, "101")])
        row = next(
            i for i in build_holds_queue_items(state) if isinstance(i, EpicHoldListItem)
        )
        text = _row_text(row)
        assert "finalize_publication" in text
        assert "ERR" not in text  # preserved work is not an error

    def test_a_ready_row_shows_what_the_projection_says_about_it(self):
        from types import SimpleNamespace

        from kew.tui.screens.holds_queue import build_holds_queue_items
        from kew.tui.selection import ReadyQueueListItem

        view = _fake_view(
            identity=SimpleNamespace(
                item_id="7", item_ref="#7", source_tracker="github"
            ),
            execution=SimpleNamespace(state="not_started"),
            exception=SimpleNamespace(
                state="fleet_stopped", detail="the fleet is stopped", blocking=True
            ),
            safest_action=SimpleNamespace(action="none", reason="the fleet is stopped"),
        )
        state = _state_with(view, ready=[{"number": 7, "title": "Do the thing"}])
        row = next(
            i
            for i in build_holds_queue_items(state)
            if isinstance(i, ReadyQueueListItem)
        )
        assert row.work_ref.item_id == "7"
        text = _row_text(row)
        assert "#7" in text
        assert "fleet_stopped" in text or "none" in text

    def test_a_row_with_no_canonical_view_says_it_was_not_read(self):
        from kew.tui.screens.holds_queue import build_holds_queue_items
        from kew.tui.selection import EpicHoldListItem
        from kew.tui.state import UNREAD_BADGE

        state = _state_with(None, holds=[_hold(101, "101")])
        row = next(
            i for i in build_holds_queue_items(state) if isinstance(i, EpicHoldListItem)
        )
        text = _row_text(row)
        assert UNREAD_BADGE in text
        assert "OK" not in text

    def test_items_the_projection_could_not_read_are_named_not_dropped(self):
        from kew.tui.screens.holds_queue import build_holds_queue_items

        state = _state_with(_fake_view(), holds=[_hold(101, "101")], unreadable=["9"])
        text = " ".join(_row_text(i) for i in build_holds_queue_items(state))
        assert "#9" in text
        assert "not be read" in text

    def test_the_pane_publishes_the_projection_revision_it_is_showing(self):
        from kew.tui.screens.holds_queue import holds_queue_revision_line

        state = _state_with(_fake_view())
        assert "wp1:abcdef" in holds_queue_revision_line(state)
        assert "not read" in holds_queue_revision_line(None)


class TestNoCompetingStateMap:
    """Task 84 — the screens must not re-derive work state from a RunStatus."""

    def test_screens_read_the_shared_mapper_and_keep_no_status_table(self):
        from pathlib import Path

        import kew.tui.screens.holds_queue as holds_queue_mod
        import kew.tui.screens.logs as logs_mod

        for module in (holds_queue_mod, logs_mod):
            source = Path(module.__file__).read_text()
            assert "work_display" in source, module.__name__
            assert 'status == "success"' not in source, module.__name__
            assert '"ERR"' not in source, module.__name__
            assert '"OK"' not in source, module.__name__


def _row_text(item) -> str:
    """The markup a row was built with."""
    return str(item.row_text)


class TestOpaqueIdNavigation:
    """Task 84 — selection in the live pane is keyed on the canonical id."""

    @pytest.mark.asyncio
    async def test_navigating_to_a_linear_hold_selects_its_opaque_id(self, tmp_path):
        from textual.widgets import ListView

        from kew.config import DispatchConfig
        from kew.tui.app import KewApp
        from kew.tui.selection import (
            EpicHoldListItem,
            get_selected_work_ref,
        )

        db_path = str(tmp_path / "runs.db")
        from kew.run_store import SqliteRunStore

        with SqliteRunStore(db_path) as store:
            store.hold_epic_node_item("epic-1", "SFD-12", "ci_pending")

        app = KewApp(config=DispatchConfig(), db_path=db_path)
        async with app.run_test() as pilot:
            app._poll_local()
            await pilot.press("5")
            await wait_for(
                pilot,
                lambda: len(app.screen.query("#holds-queue-panel")) == 1,
                "the holds & queue pane to mount",
            )
            await quiesce(pilot)

            lv = app.screen.query_one("#holds-queue-panel", ListView)
            index = next(
                i
                for i, child in enumerate(lv.children)
                if isinstance(child, EpicHoldListItem)
            )
            lv.index = index
            await wait_for(
                pilot,
                lambda: lv.highlighted_child is lv.children[index],
                "the hold row to become the highlighted child",
            )

            ref = get_selected_work_ref(app.screen)
            assert ref is not None
            assert ref.item_id == "SFD-12"
            assert ref.display == "SFD-12"
            rendered = " ".join(str(getattr(c, "row_text", "")) for c in lv.children)
            # "#0" as a work reference, not as the start of a palette hex.
            assert re.search(r"#0(?![0-9a-fA-F])", rendered) is None


# --- I23: the pane's verbs are the projection's offers, not the pane's ------


class TestCanonicalActionBindings:
    """Task 85/86 — the pane binds the derived safe action and its counterpart."""

    def test_the_screen_binds_the_safe_action_and_its_counterpart(self):
        from kew.tui.screens.holds_queue import HoldsQueueScreen

        keys = {b[0]: b[1] for b in HoldsQueueScreen.BINDINGS}
        assert keys["w"] == "work_action"
        assert keys["W"] == "work_counterpart"

    def test_the_safe_action_key_delegates_to_the_canonical_handler(self):
        from unittest.mock import PropertyMock

        from kew.tui.screens.holds_queue import HoldsQueueScreen

        screen = HoldsQueueScreen()
        app = MagicMock()
        with (
            patch.object(
                type(screen), "app", new_callable=PropertyMock, return_value=app
            ),
            patch("kew.tui.actions.handle_work_action") as handler,
        ):
            screen.action_work_action()
            handler.assert_called_once_with(app)
            handler.reset_mock()
            screen.action_work_counterpart()
            handler.assert_called_once_with(app, counterpart=True)

    def test_queueing_goes_through_the_derived_dispatch_offer(self):
        from kew.tui.actions import handle_queue_issue

        app = MagicMock()
        with patch("kew.tui.actions.handle_work_action") as handler:
            handle_queue_issue(app)
        handler.assert_called_once_with(app, action="dispatch")

    def test_no_blind_queue_path_survives_in_the_action_module(self):
        """The old ``_do_queue_issue`` called the domain with no revision."""
        import kew.tui.actions as actions_mod

        assert not hasattr(actions_mod, "_do_queue_issue")
