from __future__ import annotations

from kew.tui.selection import (
    QueueListItem,
    RecentListItem,
    ReviewListItem,
    WorkerListItem,
)


def test_get_selected_issue_recognizes_held_item():
    # drive the get_selected_issue isinstance branch via a fake screen (no mount)
    from unittest.mock import MagicMock

    from kew.tui.selection import HeldListItem, get_selected_issue, is_selected_held

    screen = MagicMock()
    lv = MagicMock()
    lv.highlighted_child = HeldListItem(issue_num=42, text="#42")
    screen.query_one.return_value = lv
    assert get_selected_issue(screen) == 42
    assert is_selected_held(screen) is True


def test_queue_list_item_stores_issue_num():
    item = QueueListItem(issue_num=142, text="test")
    assert item.issue_num == 142


def test_worker_list_item_stores_run_id():
    item = WorkerListItem(run_id="abc123", text="test")
    assert item.run_id == "abc123"


def test_recent_list_item_stores_run_id():
    item = RecentListItem(run_id="def456", text="test")
    assert item.run_id == "def456"


def test_review_list_item_stores_pr_number():
    item = ReviewListItem(pr_number=87, text="PR #87")
    assert item.pr_number == 87


# --- I22: rows are keyed on the canonical work item, not a numeric shim -----


def test_work_item_ref_renders_github_numerically_and_linear_bare():
    from kew.tui.selection import WorkItemRef

    assert WorkItemRef(item_id="42").display == "#42"
    assert WorkItemRef(item_id="SFD-12", tracker="linear").display == "SFD-12"


def test_work_item_ref_never_renders_the_zero_sentinel():
    """A Linear item carries ``issue_num=0`` in a NOT NULL column. That
    sentinel is storage, never identity — it must not reach a screen."""
    from kew.run_store import EpicHold
    from kew.tui.selection import WorkItemRef

    hold = EpicHold(
        issue_num=0,
        epic_ref="epic-1",
        reason="ci_pending",
        held_at=None,
        item_id="SFD-12",
    )
    ref = WorkItemRef.from_record(hold)
    assert ref.item_id == "SFD-12"
    assert ref.display == "SFD-12"
    assert "#0" not in ref.display


def test_work_item_ref_falls_back_to_the_legacy_numeric_id():
    from types import SimpleNamespace

    from kew.tui.selection import WorkItemRef

    legacy = SimpleNamespace(item_id=None, issue_num=42)
    assert WorkItemRef.from_record(legacy).display == "#42"


def test_work_item_ref_with_no_identity_says_so_rather_than_inventing_one():
    from types import SimpleNamespace

    from kew.tui.selection import WorkItemRef

    unidentified = WorkItemRef.from_record(SimpleNamespace(item_id="0", issue_num=0))
    assert unidentified.display != "#0"
    assert "0" not in unidentified.display


def test_work_item_ref_from_a_canonical_view():
    from types import SimpleNamespace

    from kew.tui.selection import WorkItemRef

    view = SimpleNamespace(
        identity=SimpleNamespace(
            item_id="SFD-12", item_ref="SFD-12", source_tracker="linear"
        )
    )
    ref = WorkItemRef.from_view(view)
    assert (ref.item_id, ref.tracker, ref.display) == ("SFD-12", "linear", "SFD-12")


def test_work_rows_carry_both_the_canonical_ref_and_the_legacy_number():
    from kew.tui.selection import (
        EpicHoldListItem,
        ReadyQueueListItem,
        WorkItemRef,
    )

    ref = WorkItemRef(item_id="SFD-12", tracker="linear", issue_num=0)
    hold_row = EpicHoldListItem(issue_num=0, epic_ref="epic-1", text="x", work_ref=ref)
    assert hold_row.work_ref.item_id == "SFD-12"
    assert hold_row.issue_num == 0  # the legacy verb path still has its number
    assert hold_row.epic_ref == "epic-1"

    queue_row = ReadyQueueListItem(issue_num=7, text="x")
    assert queue_row.work_ref.item_id == "7"


def test_get_selected_work_ref_returns_the_opaque_id_of_the_highlighted_row():
    from unittest.mock import MagicMock

    from kew.tui.selection import (
        ReadyQueueListItem,
        WorkItemRef,
        get_selected_work_ref,
    )

    screen = MagicMock()
    lv = MagicMock()
    lv.highlighted_child = ReadyQueueListItem(
        issue_num=0, text="SFD-12", work_ref=WorkItemRef(item_id="SFD-12")
    )
    screen.query_one.return_value = lv

    ref = get_selected_work_ref(screen, "holds-queue-panel")
    assert ref is not None
    assert ref.item_id == "SFD-12"


def test_get_selected_work_ref_is_none_for_a_row_that_names_no_work_item():
    from unittest.mock import MagicMock

    from kew.tui.selection import (
        OrphanListItem,
        SectionHeaderListItem,
        get_selected_work_ref,
    )

    screen = MagicMock()
    lv = MagicMock()
    for child in (
        SectionHeaderListItem(text="hdr"),
        OrphanListItem(pr_number=5, text="x"),
    ):
        lv.highlighted_child = child
        screen.query_one.return_value = lv
        assert get_selected_work_ref(screen, "holds-queue-panel") is None


def test_run_rows_carry_the_work_item_they_belong_to():
    """A recorder row is selected by run, but a *work* verb needs the item."""
    from kew.tui.selection import RecentListItem, WorkItemRef

    row = RecentListItem(
        run_id="r1", text="x", work_ref=WorkItemRef(item_id="SFD-12", issue_num=0)
    )
    assert row.run_id == "r1"
    assert row.work_ref is not None
    assert row.work_ref.display == "SFD-12"


def test_get_selected_work_ref_reads_a_run_row_too():
    from unittest.mock import MagicMock

    from kew.tui.selection import RecentListItem, WorkItemRef, get_selected_work_ref

    screen = MagicMock()
    lv = MagicMock()
    lv.highlighted_child = RecentListItem(
        run_id="r1", text="x", work_ref=WorkItemRef(item_id="42")
    )
    screen.query_one.return_value = lv
    ref = get_selected_work_ref(screen, "logs-list")
    assert ref is not None and ref.item_id == "42"


def test_get_selected_work_ref_is_none_when_a_run_row_has_no_work_item():
    from unittest.mock import MagicMock

    from kew.tui.selection import RecentListItem, get_selected_work_ref

    screen = MagicMock()
    lv = MagicMock()
    lv.highlighted_child = RecentListItem(run_id="r1", text="x")
    screen.query_one.return_value = lv
    assert get_selected_work_ref(screen, "logs-list") is None
