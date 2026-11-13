"""A running item is never needs-you, and an epic-less wait has a home (#2784).

Two readings from design review c5, each against a REAL ledger so the run rows,
the epic journal and the orphan table are read by the store's own queries:

* **N5.** #2728 wore the needs-you diamond beside WORKING. A run executing on
  an item is the item's next move already being made, so the set drops it —
  and keeps it the moment that run stops reading as in flight, because a dead
  ``pending`` row must not hide a real wait for good.
* **D2.** The header read 11 over an Epics list totalling 10; the eleventh was
  an epic-less PR wait with no visible home. The served payload now counts
  those entries beside the per-epic map, so the header is the map's sum plus
  exactly that number.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from kew.config import DispatchConfig, ProjectConfig, RouteEntry, RoutingConfig
from kew.run_store import (
    ADVERSARIAL_REVIEW_RUN_KIND,
    CI_FIX_RUN_KIND,
    RESOLVE_RUN_KIND,
    REVIEW_REMEDIATION_RUN_KIND,
    REVIEW_STAGE_RUN_KIND,
    RunRecord,
    SqliteRunStore,
)
from kew.service.attention import compose_node_attention
from kew.service.dashboard import assemble
from kew.service.needs_you import (
    needs_you_by_epic,
    needs_you_set,
    needs_you_without_epic,
)
from kew.service.work import pending_run_max_age_s

NOW = datetime(2026, 10, 12, 12, 0, 0, tzinfo=UTC)
EPIC = "test-integrity"
ITEM = "2728"
ORPHAN_PR = 2608


def _config() -> DispatchConfig:
    return DispatchConfig(
        project=ProjectConfig(repo=""),
        routing=RoutingConfig(
            default="claude-code",
            simple=RouteEntry(runner="claude-code", model="claude-haiku"),
        ),
    )


def _node(node_id: str, status: str, stuck: str | None) -> dict[str, Any]:
    return {
        "id": node_id,
        "title": f"node {node_id}",
        "status": status,
        "status_since": "2026-10-12T11:00:00+00:00",
        "url": None,
        "pr_url": None,
        "external": False,
        "stuck": stuck,
    }


def _ledger(*nodes: dict[str, Any]) -> SqliteRunStore:
    """A ledger whose one live epic journals *nodes*."""
    store = SqliteRunStore(":memory:")
    store.append_epic_tick(
        epic_ref=EPIC,
        tick_ts=(NOW - timedelta(minutes=1)).isoformat(),
        snapshot={
            "schema": 1,
            "epic_ref": EPIC,
            "signal": "progressing",
            "counts": {},
            "frontier": [],
            "nodes": list(nodes),
            "edges": [],
        },
        actions={},
        snapshot_hash="h1",
    )
    return store


def _awaiting_continuation() -> dict[str, Any]:
    return _node(ITEM, "held", "awaiting_continuation — run kew epic resume")


def _pending_run(store: SqliteRunStore, started_at: datetime) -> None:
    store.save(
        RunRecord(
            run_id=f"run-{ITEM}",
            issue_num=int(ITEM),
            item_id=ITEM,
            branch=f"agent/issue-{ITEM}",
            status="pending",
            exit_code=-1,
            started_at=started_at,
        )
    )


def _items(store: SqliteRunStore) -> list[str]:
    return [e.item_id for e in needs_you_set(store, _config(), now=NOW).entries]


def test_a_waiting_node_is_in_the_set_while_nothing_runs_on_it() -> None:
    """The control: without a run, the journaled wait IS a member — so the
    exclusion below is the run's doing and not a wait the set never held."""
    assert _items(_ledger(_awaiting_continuation())) == [ITEM]


def test_an_item_with_an_executing_run_is_not_in_the_set() -> None:
    store = _ledger(_awaiting_continuation())
    _pending_run(store, NOW - timedelta(minutes=5))

    answer = needs_you_set(store, _config(), now=NOW)

    assert [e.item_id for e in answer.entries] == []
    assert "executing_runs" not in answer.unavailable


def test_a_pending_row_past_the_in_flight_bound_does_not_hide_the_wait() -> None:
    """A dead ``pending`` row is not a run executing: past the projection's own
    bound the item is back in the set rather than silenced for good."""
    store = _ledger(_awaiting_continuation())
    bound = pending_run_max_age_s(_config())
    _pending_run(store, NOW - timedelta(seconds=bound + 60))

    assert ITEM in _items(store)


def test_an_epicless_wait_is_in_the_set_and_counted_in_its_own_bucket() -> None:
    store = _ledger(_awaiting_continuation())
    store.sync_orphan_prs(
        [
            (
                ORPHAN_PR,
                f"https://github.com/o/r/pull/{ORPHAN_PR}",
                "feature/x",
                "no-record",
            )
        ]
    )

    entries = needs_you_set(store, _config(), now=NOW).entries

    orphan = [row for row in entries if row.pr_number == ORPHAN_PR]
    assert len(orphan) == 1
    assert orphan[0].epic_ref is None
    by_epic = needs_you_by_epic(entries)
    assert by_epic == {EPIC: 1}
    assert needs_you_without_epic(entries) == 1
    assert len(entries) == sum(by_epic.values()) + needs_you_without_epic(entries)


def test_the_dashboard_serves_the_epicless_bucket_beside_the_map() -> None:
    """What the header, the Epics list's "no epic" row and its epic rows read:
    one payload whose rows are the map's sum plus the served epic-less count."""
    store = _ledger(_awaiting_continuation())
    store.sync_orphan_prs(
        [(ORPHAN_PR, f"https://github.com/o/r/pull/{ORPHAN_PR}", "x", "no-record")]
    )

    served = assemble(store, _config())

    assert served.needs_you_by_epic == {EPIC: 1}
    assert served.needs_you_no_epic == 1
    assert len(served.rows) == 2


def test_the_dag_draws_no_diamond_on_a_running_node_the_legend_does_not_count() -> None:
    """The node join reads the same executing set, so the DAG's diamond and the
    legend's bucket move together: drawn while nothing runs, gone while a run
    executes on the node."""
    node = _awaiting_continuation()
    store = _ledger(node)

    def drawn() -> bool:
        join = compose_node_attention(
            store, _config(), [ITEM], nodes=[node], epic_ref=EPIC, now=NOW
        )
        return ITEM in join.by_node

    assert drawn()
    _pending_run(store, NOW - timedelta(minutes=5))
    assert not drawn()
    assert needs_you_by_epic(needs_you_set(store, _config(), now=NOW).entries) == {}


def _run_of_kind(store: SqliteRunStore, kind: str, status: str) -> None:
    """One run of *kind* on the held node's PR branch (#2842)."""
    store.save(
        RunRecord(
            run_id=f"{kind}-{ITEM}",
            issue_num=int(ITEM),
            item_id=ITEM,
            branch=f"agent/issue-{ITEM}",
            status=status,
            exit_code=-1 if status == "pending" else 1,
            started_at=NOW - timedelta(minutes=9),
            run_kind=kind,
        )
    )


@pytest.mark.parametrize(
    "kind",
    [
        "dispatch",
        ADVERSARIAL_REVIEW_RUN_KIND,
        REVIEW_REMEDIATION_RUN_KIND,
        REVIEW_STAGE_RUN_KIND,
        RESOLVE_RUN_KIND,
        CI_FIX_RUN_KIND,
    ],
)
def test_a_conflict_hold_leaves_the_set_while_any_run_executes_on_it(kind: str) -> None:
    """N5 (#2842): #2814 sat in the list as "held 9m" while a conflict-resolve
    run worked on it. A run of ANY kind executing on the item is the item's
    next move being made — and the wait comes back the moment that run ends."""
    store = _ledger(_node(ITEM, "held", "base_integration_conflict"))
    store.hold_epic_node_item(
        EPIC, ITEM, "base_integration_conflict: main no longer merges into this PR"
    )
    assert ITEM in _items(store)

    _run_of_kind(store, kind, "pending")
    answer = needs_you_set(store, _config(), now=NOW)
    assert [e.item_id for e in answer.entries] == []
    assert "executing_runs" not in answer.unavailable

    _run_of_kind(store, kind, "failure")
    assert ITEM in _items(store)
