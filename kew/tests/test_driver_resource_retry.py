"""A node whose run ran out of a resource is retried once per head (#3191).

A sandboxed run killed by SIGABRT (134) or the OOM killer (137) reads as
``resource``: the same work may fit on a second attempt, not a third. The
driver re-admits the node once for the commit its run started from — the
dispatch sink is what shows the relaunch — and a second ``resource`` failure
from that commit holds the node for a person; the run keeps the signal and
the runner's last output as its reason. A node gets at most two such retries
in total, across heads: the next ``resource`` failure holds it the same way.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from support.driver_fakes import (
    EPIC,
    NOW,
    FakeFactsReader,
    FakeSink,
    FakeSource,
    _advance,
    _config,
    _facts,
    _node,
)
from support.driver_fakes import _isolate_tool_paths as _isolate_tool_paths
from support.driver_fakes import _no_real_git as _no_real_git
from support.driver_fakes import store as store

from kew.audit import Actor
from kew.epic_holds import (
    CONTAINMENT_HOLD_PREFIX,
    RESOURCE_RETRIES_SPENT_HOLD_PREFIX,
)
from kew.evidence import (
    TRUSTED_BASE_FACT,
    EvidenceKind,
    Producer,
    ProducerKind,
    record_evidence,
)
from kew.graph.driver.result import TickResult
from kew.run_store import RunRecord
from kew.service.verbs import epic_retry

_ABORT = "killed by SIGABRT (exit 134); last output: FATAL ERROR: heap limit"


@pytest.fixture(autouse=True)
def _forge_labels() -> Any:
    """The retry's label edit is GitHub's; record it instead of sending it."""
    with patch("kew.labels._update_labels") as edits:
        yield edits


def _resource_failure(
    store: Any, run_id: str, *, started_at: Any, base: str | None = None
) -> None:
    """A contained run that died of SIGABRT, as dispatch finalizes one."""
    store.save(
        RunRecord(
            run_id=run_id,
            issue_num=1,
            branch="agent/issue-1",
            status="failure",
            exit_code=134,
            error_category="resource",
            started_at=started_at,
            finished_at=started_at,
            item_id="1",
        )
    )
    store.append_audit_event(
        timestamp=started_at.isoformat(),
        event="sandbox_execution_failed",
        run_id=run_id,
        issue_num=1,
        actor_type="policy",
        actor_id="sandbox",
        payload={"exit_code": 134, "error_category": "resource", "reason": _ABORT},
    )
    if base is not None:
        record_evidence(
            store,
            EvidenceKind.RECEIPT_FACT,
            actor=Actor("policy", "sandbox_launch"),
            producer=Producer(ProducerKind.HOST, "kew", "test"),
            facts={"fact": TRUSTED_BASE_FACT, "value": base, "attempt_number": 1},
            run_id=run_id,
            issue_num=1,
            item_id="1",
        )


def _tick(store: Any, sink: FakeSink, tmp_path: Path) -> TickResult:
    return _advance(
        deps=FakeSource(nodes=[_node("1")], edges=[]),
        facts=FakeFactsReader(issue_facts_by_num={1: _facts(1, state="OPEN")}),
        store=store,
        sink=sink,
        lock_path=tmp_path / "epic.lock",
    )


def _relaunch_start(store: Any) -> datetime:
    """When the relaunch the last tick dispatched started.

    After that tick finished — the driver stamped its retry marker and its
    dispatch then — and before the next tick stamps anything.
    """
    started = datetime.now(UTC)
    assert all(r.retried_at < started for r in store.list_epic_retries(EPIC))
    return started


def test_a_resource_failure_retries_once_then_holds_at_the_same_head(
    tmp_path: Path, store: Any, _forge_labels: Any
) -> None:
    head = "a" * 40
    _resource_failure(store, "run-1", started_at=NOW, base=head)
    sink = FakeSink()

    first = _tick(store, sink, tmp_path)

    assert first.dispatched == ("1",)
    assert sink.calls == ["1"]
    [retry] = store.list_audit_events(event="epic_node_retry")
    assert (retry.actor_type, retry.actor_id) == ("policy", "driver_resource_retry")
    assert retry.payload["reason"] == "resource"
    assert retry.payload["head"] == head
    assert retry.payload["run_id"] == "run-1"
    # The retry clears the failure label exactly as `kew epic retry` does.
    assert _forge_labels.call_count == 1

    _resource_failure(store, "run-2", started_at=_relaunch_start(store), base=head)

    second = _tick(store, sink, tmp_path)

    assert second.dispatched == ()
    assert sink.calls == ["1"]  # no second relaunch
    assert second.failed == ()
    reason = _held_reason(second)
    assert reason == _hold_reason(store)
    assert reason.startswith(f"{RESOURCE_RETRIES_SPENT_HOLD_PREFIX}:")
    assert f"at {head[:12]}, after its automatic retry there" in reason
    assert "kew epic unhold" in reason and "kew epic retry" in reason
    assert len(store.list_audit_events(event="epic_node_retry")) == 1


def test_a_spent_resource_retry_says_why_the_node_holds(
    tmp_path: Path, store: Any, caplog: pytest.LogCaptureFixture
) -> None:
    head = "a" * 40
    _resource_failure(store, "run-1", started_at=NOW, base=head)
    sink = FakeSink()
    _tick(store, sink, tmp_path)
    _resource_failure(store, "run-2", started_at=_relaunch_start(store), base=head)

    with caplog.at_level("WARNING", logger="kew.graph.driver.resource_retry"):
        result = _tick(store, sink, tmp_path)

    assert (
        f"1 not re-admitted: 1: resource retry spent at {head} — held for a person"
        in caplog.text
    )
    assert _held_reason(result).startswith(RESOURCE_RETRIES_SPENT_HOLD_PREFIX)


def test_a_released_same_head_hold_leaves_the_node_failed_for_a_retry_by_hand(
    tmp_path: Path, store: Any
) -> None:
    head = "a" * 40
    _resource_failure(store, "run-1", started_at=NOW, base=head)
    sink = FakeSink()
    _tick(store, sink, tmp_path)
    _resource_failure(store, "run-2", started_at=_relaunch_start(store), base=head)
    _tick(store, sink, tmp_path)
    assert store.remove_epic_hold(EPIC, "1") is not None  # `kew epic unhold`

    released = _tick(store, sink, tmp_path)

    # Not held again for the same run; its signal and output are the reason.
    assert released.failed == ("1",)
    assert _hold_reason(store) is None
    [stuck] = released.stuck
    assert stuck.reason == f"failed-terminal (exit 134, resource): {_ABORT}"
    assert sink.calls == ["1"]

    epic_retry(
        store,
        epic_ref=EPIC,
        item_id="1",
        config=_config(),
        actor=Actor("human", "operator"),
    )
    _tick(store, sink, tmp_path)

    assert sink.calls == ["1", "1"]


def test_a_crash_writing_the_hold_record_leaves_no_hold_behind(
    tmp_path: Path, store: Any
) -> None:
    """The hold and the record of why it was written stand or fall together.

    The crash is injected at the database: a trigger aborts the record's write
    inside the hold's transaction, as a process dying mid-write would leave it.
    """
    head = "a" * 40
    _resource_failure(store, "run-1", started_at=NOW, base=head)
    sink = FakeSink()
    _tick(store, sink, tmp_path)
    _resource_failure(store, "run-2", started_at=_relaunch_start(store), base=head)
    with sqlite3.connect(tmp_path / "run.db") as db:
        db.execute(
            "CREATE TRIGGER crash_on_record BEFORE INSERT ON meta"
            " WHEN NEW.key LIKE 'resource_retry_decision:%'"
            " BEGIN SELECT RAISE(ABORT, 'crash'); END"
        )

    with pytest.raises(sqlite3.Error, match="crash"):
        _tick(store, sink, tmp_path)

    assert _hold_reason(store) is None

    with sqlite3.connect(tmp_path / "run.db") as db:
        db.execute("DROP TRIGGER crash_on_record")
    held = _tick(store, sink, tmp_path)

    assert _held_reason(held).startswith(RESOURCE_RETRIES_SPENT_HOLD_PREFIX)
    # Its release is final: the record written with the hold stops a re-hold.
    store.remove_epic_hold(EPIC, "1")
    released = _tick(store, sink, tmp_path)

    assert released.failed == ("1",)
    assert _hold_reason(store) is None


def test_a_resource_failure_from_a_new_head_earns_its_own_retry(
    tmp_path: Path, store: Any
) -> None:
    _resource_failure(store, "run-1", started_at=NOW, base="a" * 40)
    sink = FakeSink()
    _tick(store, sink, tmp_path)
    _resource_failure(store, "run-2", started_at=_relaunch_start(store), base="b" * 40)

    result = _tick(store, sink, tmp_path)

    assert result.dispatched == ("1",)
    assert sink.calls == ["1", "1"]
    heads = [
        e.payload["head"] for e in store.list_audit_events(event="epic_node_retry")
    ]
    assert sorted(heads) == ["a" * 40, "b" * 40]


def test_an_ordinary_failure_is_not_retried(tmp_path: Path, store: Any) -> None:
    store.save(
        RunRecord(
            run_id="run-1",
            issue_num=1,
            branch="agent/issue-1",
            status="failure",
            exit_code=1,
            error_category="permanent",
            started_at=NOW,
            finished_at=NOW,
            item_id="1",
        )
    )
    sink = FakeSink()

    result = _tick(store, sink, tmp_path)

    assert result.failed == ("1",)
    assert sink.calls == []
    assert store.list_audit_events(event="epic_node_retry") == []


def test_a_held_node_is_not_retried(tmp_path: Path, store: Any) -> None:
    _resource_failure(store, "run-1", started_at=NOW)
    store.hold_epic_node_item(EPIC, "1", f"{CONTAINMENT_HOLD_PREFIX}: refused")
    sink = FakeSink()

    _tick(store, sink, tmp_path)

    assert sink.calls == []
    assert store.list_audit_events(event="epic_node_retry") == []


def test_a_failed_label_edit_does_not_end_the_tick(
    tmp_path: Path, store: Any, _forge_labels: Any
) -> None:
    _resource_failure(store, "run-1", started_at=NOW)
    _forge_labels.side_effect = RuntimeError("gh issue edit failed (exit 1)")
    sink = FakeSink()

    result = _tick(store, sink, tmp_path)

    assert result.dispatched == ("1",)
    assert len(store.list_audit_events(event="epic_node_retry")) == 1


def test_a_run_that_spent_its_retry_in_process_is_not_retried_again(
    tmp_path: Path, store: Any
) -> None:
    """An uncontained dispatch retries a ``resource`` death itself, once."""
    _resource_failure(store, "run-1", started_at=NOW)
    run = store.get("run-1")
    run.attempt_number = 2
    store.save(run)
    sink = FakeSink()

    result = _tick(store, sink, tmp_path)

    assert result.failed == ("1",)
    assert sink.calls == []


def _driver_retries(store: Any) -> list[Any]:
    return [
        e
        for e in store.list_audit_events(event="epic_node_retry")
        if e.actor_id == "driver_resource_retry"
    ]


def _hold_reason(store: Any) -> str | None:
    return next((h.reason for h in store.list_epic_holds(EPIC)), None)


def _held_reason(result: TickResult) -> str:
    """The reason the tick reports node 1 as waiting on a person for."""
    [reason] = [w.reason for w in result.waiting if w.node_id == "1"]
    return reason


def test_one_resource_failure_gets_exactly_one_retry(
    tmp_path: Path, store: Any
) -> None:
    _resource_failure(store, "run-1", started_at=NOW, base="a" * 40)
    sink = FakeSink()

    _tick(store, sink, tmp_path)
    _tick(store, sink, tmp_path)

    assert sink.calls == ["1"]
    assert len(_driver_retries(store)) == 1
    assert _hold_reason(store) is None


def test_a_node_gets_two_automatic_resource_retries_in_total_then_holds(
    tmp_path: Path, store: Any
) -> None:
    """Main moves between relaunches, so each failure is from a new head."""
    sink = FakeSink()
    _resource_failure(store, "run-a", started_at=NOW, base="a" * 40)
    _tick(store, sink, tmp_path)
    _resource_failure(store, "run-b", started_at=_relaunch_start(store), base="b" * 40)
    _tick(store, sink, tmp_path)
    _resource_failure(store, "run-c", started_at=_relaunch_start(store), base="c" * 40)

    held = _tick(store, sink, tmp_path)

    assert sink.calls == ["1", "1"]
    assert sorted(e.payload["head"] for e in _driver_retries(store)) == [
        "a" * 40,
        "b" * 40,
    ]
    assert held.dispatched == ()
    assert held.failed == ()
    assert _held_reason(held).startswith(RESOURCE_RETRIES_SPENT_HOLD_PREFIX)
    reason = _hold_reason(store)
    assert reason is not None
    assert reason.startswith(f"{RESOURCE_RETRIES_SPENT_HOLD_PREFIX}:")
    assert "after 2 automatic retries" in reason
    assert "kew epic unhold" in reason and "kew epic retry" in reason

    # Another tick, with main moved on again, changes nothing while it holds.
    again = _tick(store, sink, tmp_path)

    assert _held_reason(again).startswith(RESOURCE_RETRIES_SPENT_HOLD_PREFIX)
    assert sink.calls == ["1", "1"]
    assert len(_driver_retries(store)) == 2


def test_a_released_cap_hold_leaves_the_node_failed_for_a_retry_by_hand(
    tmp_path: Path, store: Any
) -> None:
    sink = FakeSink()
    _resource_failure(store, "run-a", started_at=NOW, base="a" * 40)
    _tick(store, sink, tmp_path)
    _resource_failure(store, "run-b", started_at=_relaunch_start(store), base="b" * 40)
    _tick(store, sink, tmp_path)
    _resource_failure(store, "run-c", started_at=_relaunch_start(store), base="c" * 40)
    _tick(store, sink, tmp_path)
    assert store.remove_epic_hold(EPIC, "1") is not None  # `kew epic unhold`

    released = _tick(store, sink, tmp_path)

    # Not held again for the same run, and not relaunched either.
    assert released.failed == ("1",)
    assert _hold_reason(store) is None
    assert sink.calls == ["1", "1"]

    # The person relaunches it by hand; its next resource failure, at a
    # fourth head, holds it again without a third automatic retry.
    epic_retry(
        store,
        epic_ref=EPIC,
        item_id="1",
        config=_config(),
        actor=Actor("human", "operator"),
    )
    _tick(store, sink, tmp_path)
    assert sink.calls == ["1", "1", "1"]
    _resource_failure(store, "run-d", started_at=_relaunch_start(store), base="d" * 40)

    held = _tick(store, sink, tmp_path)

    assert _held_reason(held).startswith(RESOURCE_RETRIES_SPENT_HOLD_PREFIX)
    assert sink.calls == ["1", "1", "1"]
    assert len(_driver_retries(store)) == 2


def test_a_spent_retry_is_logged_once(
    tmp_path: Path, store: Any, caplog: pytest.LogCaptureFixture
) -> None:
    head = "a" * 40
    sink = FakeSink()
    _resource_failure(store, "run-1", started_at=NOW, base=head)
    _tick(store, sink, tmp_path)
    _resource_failure(store, "run-2", started_at=_relaunch_start(store), base=head)

    with caplog.at_level("WARNING", logger="kew.graph.driver.resource_retry"):
        for _ in range(3):
            _tick(store, sink, tmp_path)

    assert caplog.text.count("not re-admitted") == 1


def test_a_cap_hold_is_logged_once(
    tmp_path: Path, store: Any, caplog: pytest.LogCaptureFixture
) -> None:
    sink = FakeSink()
    _resource_failure(store, "run-a", started_at=NOW, base="a" * 40)
    _tick(store, sink, tmp_path)
    _resource_failure(store, "run-b", started_at=_relaunch_start(store), base="b" * 40)
    _tick(store, sink, tmp_path)
    _resource_failure(store, "run-c", started_at=_relaunch_start(store), base="c" * 40)

    with caplog.at_level("WARNING", logger="kew.graph.driver.resource_retry"):
        for _ in range(3):
            _tick(store, sink, tmp_path)

    assert caplog.text.count("held for a person") == 1


def test_a_refused_retry_is_logged_once_and_stays_open(
    tmp_path: Path, store: Any, caplog: pytest.LogCaptureFixture
) -> None:
    _resource_failure(store, "run-1", started_at=NOW, base="a" * 40)
    store.hold_epic_node_item(EPIC, "1", f"{CONTAINMENT_HOLD_PREFIX}: refused")
    sink = FakeSink()

    with caplog.at_level("WARNING", logger="kew.graph.driver.resource_retry"):
        for _ in range(3):
            _tick(store, sink, tmp_path)

    assert caplog.text.count("not re-admitted") == 1
    assert sink.calls == []

    # A refusal is not final: once the hold is released, this head's one
    # retry is still the node's to take.
    store.remove_epic_hold(EPIC, "1")
    _tick(store, sink, tmp_path)

    assert sink.calls == ["1"]
    assert len(_driver_retries(store)) == 1


def test_a_newer_run_in_flight_stands_in_front_of_an_older_resource_failure(
    tmp_path: Path, store: Any
) -> None:
    """A dispatch by hand after the failure leaves no retry marker."""
    _resource_failure(store, "run-1", started_at=NOW, base="a" * 40)
    store.save(
        RunRecord(
            run_id="run-2",
            issue_num=1,
            branch="agent/issue-1",
            status="pending",
            exit_code=0,
            started_at=datetime(2026, 7, 1, 1, tzinfo=UTC),
            item_id="1",
        )
    )
    sink = FakeSink()

    _tick(store, sink, tmp_path)

    assert _driver_retries(store) == []
    assert sink.calls == []
