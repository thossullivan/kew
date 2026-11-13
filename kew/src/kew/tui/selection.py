"""Selection helpers — typed ListItems and selected-item extraction.

Rows that name a work item carry a :class:`WorkItemRef`: the canonical opaque
id the rest of kew joins on (``SFD-12``, ``42``), not the ``issue_num`` column
a non-numeric tracker has to write as ``0``. The legacy number rides along
beside it for the verbs that still take one — it is never what gets displayed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from textual.css.query import NoMatches
from textual.screen import Screen
from textual.widgets import ListItem, ListView, Static

from kew.ids import item_ref

#: What a row shows when its record carries no usable identity at all (a
#: legacy ``0``-sentinel row with no ``item_id``). Never ``#0``: that reads as
#: a real GitHub issue and is the exact confusion this slice removes.
UNIDENTIFIED = "(no item id)"

_SENTINELS = ("", "0", "None")


@dataclass(frozen=True)
class WorkItemRef:
    """The canonical identity of one work item as a TUI row holds it."""

    item_id: str
    tracker: str = "github"
    #: The legacy numeric id, when the record had one. Storage, not identity.
    issue_num: int | None = None

    @property
    def display(self) -> str:
        """``#42`` for a GitHub id, ``SFD-12`` bare, and never ``#0``."""
        if self.item_id in _SENTINELS:
            return UNIDENTIFIED
        return item_ref(self.item_id)

    @classmethod
    def from_record(cls, record: Any, *, tracker: str = "github") -> WorkItemRef:
        """From any record carrying ``item_id``/``issue_num`` (run, hold)."""
        raw = str(getattr(record, "item_id", "") or "")
        issue_num = getattr(record, "issue_num", None)
        if raw in _SENTINELS:
            raw = str(issue_num or "")
        return cls(item_id=raw, tracker=tracker, issue_num=issue_num)

    @classmethod
    def from_view(cls, view: Any) -> WorkItemRef:
        """From a canonical :class:`WorkViewDTO` — its identity, verbatim."""
        identity = view.identity
        return cls(
            item_id=identity.item_id or "",
            tracker=identity.source_tracker or "github",
        )

    @classmethod
    def from_issue(cls, issue: Mapping[str, Any]) -> WorkItemRef:
        """From a queue issue dict, preferring an opaque id when one is set."""
        raw = str(issue.get("item_id") or "")
        number = issue.get("number")
        if raw in _SENTINELS:
            raw = str(number or "")
        return cls(
            item_id=raw,
            tracker=str(issue.get("tracker") or "github"),
            issue_num=number if isinstance(number, int) else None,
        )


class WorkListItem(ListItem):
    """A row naming one work item, keyed on its canonical ref.

    ``issue_num`` stays public because the shipped verbs still take a number;
    ``work_ref`` is what a surface displays and what I23's canonical actions
    are keyed on.
    """

    def __init__(
        self, *, issue_num: int, text: str, work_ref: WorkItemRef | None = None
    ) -> None:
        super().__init__(Static(text))
        #: The row's markup, retained so a surface (and a test) can read what
        #: was rendered without mounting the widget.
        self.row_text = text
        self.issue_num = issue_num
        self.work_ref = work_ref or WorkItemRef(
            item_id=str(issue_num), issue_num=issue_num
        )


class QueueListItem(WorkListItem):
    """ListItem carrying a ready queue issue."""


class BacklogListItem(WorkListItem):
    """ListItem carrying a backlog (non-ready) issue."""


class FailedListItem(WorkListItem):
    """ListItem carrying a failed (agent-failed) issue."""


class WorkerListItem(ListItem):
    """ListItem carrying a run_id, and the work item that run belongs to."""

    def __init__(
        self, *, run_id: str, text: str, work_ref: WorkItemRef | None = None
    ) -> None:
        super().__init__(Static(text))
        self.row_text = text
        self.run_id = run_id
        #: None only for a row built before its record was resolved — a run
        #: row is selected by ``run_id``; this is what a *work* verb needs.
        self.work_ref = work_ref


class RecentListItem(ListItem):
    """ListItem carrying a run_id, and the work item that run belongs to."""

    def __init__(
        self, *, run_id: str, text: str, work_ref: WorkItemRef | None = None
    ) -> None:
        super().__init__(Static(text))
        self.row_text = text
        self.run_id = run_id
        #: None only for a row built before its record was resolved — a run
        #: row is selected by ``run_id``; this is what a *work* verb needs.
        self.work_ref = work_ref


class ReviewListItem(ListItem):
    """ListItem carrying a PR number."""

    def __init__(self, *, pr_number: int, text: str) -> None:
        super().__init__(Static(text))
        self.pr_number = pr_number


class HeldListItem(WorkListItem):
    """ListItem carrying a needs-approval held issue."""


class SectionHeaderListItem(ListItem):
    """A non-selectable section header row in the Holds & Queue pane (#685)."""

    def __init__(self, *, text: str) -> None:
        super().__init__(Static(text), disabled=True)
        self.row_text = text


class EpicHoldListItem(WorkListItem):
    """ListItem carrying an epic-DAG node hold (work item + epic ref)."""

    def __init__(
        self,
        *,
        issue_num: int,
        epic_ref: str,
        text: str,
        work_ref: WorkItemRef | None = None,
    ) -> None:
        super().__init__(issue_num=issue_num, text=text, work_ref=work_ref)
        self.epic_ref = epic_ref


class ReadyQueueListItem(WorkListItem):
    """ListItem carrying a ready-queue work item in the Holds & Queue pane."""


class OrphanListItem(ListItem):
    """ListItem carrying an orphan PR number in the Holds & Queue pane."""

    def __init__(self, *, pr_number: int, text: str) -> None:
        super().__init__(Static(text))
        self.row_text = text
        self.pr_number = pr_number


_HOLDS_QUEUE_PANEL = "#holds-queue-panel"


def get_selected_epic_hold(screen: Screen) -> tuple[int, str] | None:  # type: ignore[type-arg]
    """Return ``(issue_num, epic_ref)`` of the highlighted epic-hold row, or None."""
    try:
        lv = screen.query_one(_HOLDS_QUEUE_PANEL, ListView)
    except NoMatches:
        return None
    item = lv.highlighted_child
    if isinstance(item, EpicHoldListItem):
        return item.issue_num, item.epic_ref
    return None


def get_selected_ready_issue(screen: Screen) -> int | None:  # type: ignore[type-arg]
    """Return the issue number of the highlighted ready-queue row, or None."""
    try:
        lv = screen.query_one(_HOLDS_QUEUE_PANEL, ListView)
    except NoMatches:
        return None
    item = lv.highlighted_child
    if isinstance(item, ReadyQueueListItem):
        return item.issue_num
    return None


def get_selected_orphan(screen: Screen) -> int | None:  # type: ignore[type-arg]
    """Return the PR number of the highlighted orphan-PR row, or None."""
    try:
        lv = screen.query_one(_HOLDS_QUEUE_PANEL, ListView)
    except NoMatches:
        return None
    item = lv.highlighted_child
    if isinstance(item, OrphanListItem):
        return item.pr_number
    return None


def get_selected_work_ref(
    screen: Screen,  # type: ignore[type-arg]
    panel_id: str = "holds-queue-panel",
) -> WorkItemRef | None:
    """Return the canonical :class:`WorkItemRef` of the highlighted row.

    None for a row that names no work item (a section header, an orphan PR):
    a surface must not substitute a neighbouring row's identity for a missing
    one, which is what reading ``issue_num`` off anything used to allow.
    """
    try:
        lv = screen.query_one(f"#{panel_id}", ListView)
    except NoMatches:
        return None
    ref = getattr(lv.highlighted_child, "work_ref", None)
    return ref if isinstance(ref, WorkItemRef) else None


def get_selected_issue(screen: Screen) -> int | None:  # type: ignore[type-arg]
    """Return the issue number of the highlighted queue item, or None."""
    try:
        lv = screen.query_one("#queue-panel", ListView)
    except NoMatches:
        return None
    item = lv.highlighted_child
    if item is None:
        return None
    if isinstance(item, QueueListItem):
        return item.issue_num
    if isinstance(item, BacklogListItem):
        return item.issue_num
    if isinstance(item, FailedListItem):
        return item.issue_num
    if isinstance(item, HeldListItem):
        return item.issue_num
    return None


def is_selected_ready(screen: Screen) -> bool:  # type: ignore[type-arg]
    """Return True if the highlighted queue item is a ready (not backlog) issue."""
    try:
        lv = screen.query_one("#queue-panel", ListView)
    except NoMatches:
        return False
    item = lv.highlighted_child
    return isinstance(item, QueueListItem)


def is_selected_failed(screen: Screen) -> bool:  # type: ignore[type-arg]
    """Return True if the highlighted queue item is a failed (agent-failed) issue."""
    try:
        lv = screen.query_one("#queue-panel", ListView)
    except NoMatches:
        return False
    item = lv.highlighted_child
    return isinstance(item, FailedListItem)


def is_selected_held(screen: Screen) -> bool:  # type: ignore[type-arg]
    """Return True if the highlighted queue item is a needs-approval held issue."""
    try:
        lv = screen.query_one("#queue-panel", ListView)
    except NoMatches:
        return False
    return isinstance(lv.highlighted_child, HeldListItem)


def get_selected_run(screen: Screen, panel_id: str) -> str | None:  # type: ignore[type-arg]
    """Return the run_id of the highlighted worker/recent item, or None."""
    try:
        lv = screen.query_one(f"#{panel_id}", ListView)
    except NoMatches:
        return None
    item = lv.highlighted_child
    if item is None or not hasattr(item, "run_id"):
        return None
    return str(item.run_id)


def get_selected_pr(screen: Screen, panel_id: str = "reviews-panel") -> int | None:  # type: ignore[type-arg]
    """Return the PR number of the highlighted review item, or None."""
    try:
        lv = screen.query_one(f"#{panel_id}", ListView)
    except NoMatches:
        return None
    item = lv.highlighted_child
    if item is None or not isinstance(item, ReviewListItem):
        return None
    return item.pr_number
