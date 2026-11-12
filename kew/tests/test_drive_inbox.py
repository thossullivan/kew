"""The drive's attention inbox (#2620).

The acceptance: each attention event notifies exactly once across drive
restarts (a cursor in the ledger); routine events never notify; a repeated
outage notifies once. Every test runs on a real ledger with journaled ticks;
only the desktop notifier is replaced by a list.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import kew.cli as cli
import kew.cli.drive as drive_cli
import kew.drive_inbox as inbox
from kew.config import DispatchConfig, ProjectConfig
from kew.drive import current_holder, request_stop
from kew.drive_inbox import (
    DRIVE_ATTENTION_KEY,
    Notice,
    Owner,
    deliver_attention,
    desktop_notifier,
)
from kew.run_store import SqliteRunStore

EPIC = "epic:a"
PR = "https://github.com/o/r/pull/41"
OUTAGE = "verify host unreachable: ssh exited 255"


def _node(nid: str, status: str, **extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": nid,
        "title": f"node {nid}",
        "status": status,
        "url": None,
        "pr_url": None,
        "external": False,
        "stuck": None,
    }
    base.update(extra)
    return base


_TICKS = iter(range(1, 10_000))


def _journal(
    store: Any, nodes: list[dict[str, Any]], *, epic: str = EPIC, signal: str = ""
) -> None:
    n = next(_TICKS)
    store.register_epic(epic)
    store.append_epic_tick(
        epic_ref=epic,
        tick_ts=f"2026-10-07T18:{n // 60 % 60:02d}:{n % 60:02d}+00:00",
        snapshot={
            "schema": 1,
            "epic_ref": epic,
            "signal": signal or "awaiting_merges",
            "counts": {},
            "frontier": [],
            "nodes": nodes,
            "edges": [],
        },
        actions={"schema": 1},
        snapshot_hash=f"h{n}",
    )


@pytest.fixture
def store(tmp_path: Path) -> Any:
    opened = SqliteRunStore(str(tmp_path / "dispatch.db"), project_root=tmp_path)
    yield opened
    opened.close()


class _Sink:
    def __init__(self) -> None:
        self.notices: list[Notice] = []
        self.log: list[str] = []
        self.warn: list[str] = []

    def __call__(self, notice: Notice) -> None:
        self.notices.append(notice)

    def deliver(self, store: Any, epics: tuple[str, ...] = (EPIC,)) -> list[Notice]:
        return deliver_attention(
            store, epics, notify=self, log=self.log.append, warn=self.warn.append
        )


def _held(store: Any, nid: str, reason: str) -> dict[str, Any]:
    store.register_epic(EPIC)
    store.hold_epic_node(EPIC, nid, reason)
    return _node(nid, "held")


def test_a_new_event_notifies_its_owner_once(store: Any) -> None:
    _journal(store, [_node("7", "in_review", pr_url=PR)])
    sink = _Sink()
    first = sink.deliver(store)
    assert [(n.owner, n.event.item) for n in first] == [(Owner.MAINTAINER, "7")]
    assert sink.notices == first
    assert any("needs the maintainer" in line for line in sink.warn)
    # The next round sees the same state: nothing new.
    assert sink.deliver(store) == []
    assert len(sink.notices) == 1


def test_the_cursor_survives_a_restart(store: Any, tmp_path: Path) -> None:
    """A second drive on the same ledger — a new connection, new process state —
    does not re-notify what the first already delivered."""
    _journal(store, [_node("7", "in_review", pr_url=PR)])
    _Sink().deliver(store)
    assert store.get_meta(DRIVE_ATTENTION_KEY)
    with SqliteRunStore(
        str(tmp_path / "dispatch.db"), project_root=tmp_path
    ) as reopened:
        restarted = _Sink()
        assert restarted.deliver(reopened) == []
        assert restarted.notices == []


def test_a_held_node_is_the_operators(store: Any) -> None:
    _journal(store, [_held(store, "3", OUTAGE)])
    sink = _Sink()
    assert [(n.owner, n.event.kind.value) for n in sink.deliver(store)] == [
        (Owner.OPERATOR, "held")
    ]


def test_a_repeated_outage_notifies_once_until_it_changes(store: Any) -> None:
    sink = _Sink()
    _journal(store, [_held(store, "3", OUTAGE)])
    sink.deliver(store)
    # Still down across three more ticks (one journals a changed snapshot
    # elsewhere in the epic): no second notification.
    for _ in range(2):
        sink.deliver(store)
    _journal(store, [_node("3", "held"), _node("4", "todo")])
    sink.deliver(store)
    assert len(sink.notices) == 1
    # The outage clears ...
    store.clear_epic_hold("3")
    _journal(store, [_node("3", "todo"), _node("4", "todo")])
    assert sink.deliver(store) == []
    # ... and comes back: that is news again.
    _journal(store, [_held(store, "3", OUTAGE), _node("4", "todo")])
    assert len(sink.deliver(store)) == 1
    assert len(sink.notices) == 2


def test_a_routine_event_is_logged_and_never_notified(store: Any) -> None:
    _journal(store, [_node("7", "done")], signal="complete")
    sink = _Sink()
    assert sink.deliver(store) == []
    assert sink.notices == []
    assert any("epic_complete" in line for line in sink.log)


def test_an_unreadable_journal_keeps_the_cursor(store: Any) -> None:
    """A snapshot the journal refuses is "could not tell", not "cleared": the
    standing event must not re-notify once the journal is readable again."""
    sink = _Sink()
    _journal(store, [_node("7", "in_review", pr_url=PR)])
    sink.deliver(store)
    _journal(store, [_node("7", "some_future_status", pr_url=PR)])
    sink.deliver(store)
    _journal(store, [_node("7", "in_review", pr_url=PR)])
    sink.deliver(store)
    assert len(sink.notices) == 1


def test_an_unreadable_cursor_starts_over_and_renotifies(store: Any) -> None:
    _journal(store, [_node("7", "in_review", pr_url=PR)])
    sink = _Sink()
    sink.deliver(store)
    store.set_meta(DRIVE_ATTENTION_KEY, "{not json")
    sink.deliver(store)
    assert len(sink.notices) == 2


def test_a_failing_notifier_never_stops_delivery(store: Any) -> None:
    _journal(store, [_node("7", "in_review", pr_url=PR)])
    warned: list[str] = []

    def boom(_notice: Notice) -> None:
        raise OSError("no display")

    delivered = deliver_attention(
        store, (EPIC,), notify=boom, log=lambda _l: None, warn=warned.append
    )
    assert len(delivered) == 1
    assert any("could not notify" in line for line in warned)


def test_an_unknown_kind_is_an_operators_job() -> None:
    event = inbox.AttentionEvent(
        kind="something_new",  # type: ignore[arg-type]
        epic=EPIC,
        item="1",
        reason="r",
        action="a",
    )
    assert inbox.owner_for(event) is Owner.OPERATOR


def test_an_epic_that_left_the_rotation_drops_out_of_the_cursor(store: Any) -> None:
    _journal(store, [_node("7", "in_review", pr_url=PR)])
    _journal(store, [_node("8", "in_review", pr_url=PR)], epic="epic:b")
    sink = _Sink()
    sink.deliver(store, (EPIC, "epic:b"))
    sink.deliver(store, (EPIC,))
    assert "epic:b" not in store.get_meta(DRIVE_ATTENTION_KEY)


def test_no_desktop_notifier_off_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(inbox.sys, "platform", "linux")
    assert desktop_notifier() is None


def test_the_macos_notifier_passes_text_as_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reason is journaled data: it travels as an argument, never inside the
    AppleScript source, so a quote in it cannot end a string literal."""
    import kew.subprocess_utils as su

    calls: list[list[str]] = []
    monkeypatch.setattr(inbox.sys, "platform", "darwin")
    monkeypatch.setattr(su, "ensure_tool", lambda _t: "/usr/bin/osascript")
    monkeypatch.setattr(su, "run", lambda cmd, **_kw: calls.append(cmd))
    notify = desktop_notifier()
    assert notify is not None
    event = inbox.AttentionEvent(
        kind=inbox.AttentionKind.HELD,
        epic=EPIC,
        item="3",
        reason='he said "end tell"',
        action="kew epic unhold",
    )
    notify(Notice(Owner.OPERATOR, event))
    (cmd,) = calls
    assert cmd[0] == "osascript"
    assert not any("end tell" in part for part in cmd[1:7])
    assert cmd[-2] == event.as_line()


def test_the_drive_delivers_after_each_round_and_once_across_restarts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end through `run_drive`: two drives in a row on one ledger
    deliver a standing merge-ready event once between them."""
    monkeypatch.setattr(cli, "_sleep", lambda _s: None)
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    db = log_dir / "dispatch.db"
    with SqliteRunStore(str(db)) as s:
        _journal(s, [_node("7", "in_review", pr_url=PR)])
    cfg = DispatchConfig(project=ProjectConfig(repo="o/r"))
    cfg.logging.dir = str(log_dir)
    sink = _Sink()

    def tick(_name: str, _admit_cap: int) -> int:
        with SqliteRunStore(str(db)) as s:
            holder = current_holder(s)
            assert holder is not None
            request_stop(s, holder, None)
        return 10

    for _ in range(2):
        drive_cli.run_drive(cfg, tick, 1, notify=sink)
    assert [n.event.item for n in sink.notices] == ["7"]
