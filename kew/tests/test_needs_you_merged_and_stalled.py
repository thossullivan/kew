"""The needs-you set, wrong in both directions on the live ledger (#2538).

Two readings from design review c4, each with the source the row came from:

* **A stale member.** #2469 read ``IN REVIEW · NEEDS YOU · Awaiting merge (PR
  #2476)`` on Work, the epic card and the rail for twenty minutes after PR
  #2476 merged. The row was an ``epic_wait`` (``_epic_wait_rows``), read off
  the epic's JOURNALED snapshot node — ``status: in_review``, ``stuck: in_review
  — awaiting merge (PR #2476)`` — and the rail's diamond was the same node
  through the node join (``compose_node_attention`` -> ``wait_attention``).
  The journal is only rewritten by a tick, while the merge was already in the
  run row's ``pr_state`` (the rail's own MERGE column read ``merged``).
* **Missing members.** go-live-2026-09 was ``stalled`` and its banner listed
  five ``waiting for human arm`` nodes, each a person's move, over a legend
  reading ``needs you 0``. That reason had no ``kew.waits`` bucket, so
  ``node_wait`` classified it to nobody and neither reader filed a row.

The fixtures here are those two worlds, through the same fakes the rest of the
needs-you suite uses — and for the merge, through a REAL ``SqliteRunStore`` for
the run rows, so the PR state is read by the store's own batched query rather
than by a fake that answers whatever the test wanted.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from test_needs_you_commands import _cli_command_paths, _names_a_real_command
from test_service_dashboard import (
    FakeDriverState,
    FakeEpicTick,
    FakeRun,
    FakeStore,
    _config,
)

from kew.graph.adapters.linear_native import _LINEAR_READY_LABEL
from kew.graph.driver.overlay import DriverStatus
from kew.graph.driver.result import TerminalSignal
from kew.graph.driver.tick import _FIXED_WAITING_REASONS
from kew.run_store import RunRecord, SqliteRunStore
from kew.service import dashboard, needs_you
from kew.service.attention import compose_node_attention, merged_pr_waits
from kew.waits import ARM_LABEL, ARM_WAIT_REASON, STALLED_SIGNAL

NOW = datetime(2026, 10, 8, 22, 41, 0, tzinfo=UTC)
EPIC = "console-qol"
ITEM = "2469"
PR_URL = "https://github.com/o/r/pull/2476"
GO_LIVE = "go-live-2026-09"
FRONTEND = Path(__file__).resolve().parents[2] / "frontend"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the gh-backed ready queue so unit tests never shell out."""
    monkeypatch.setattr(dashboard, "_ready_issues", lambda config: [])


def _node(node_id: str, status: str, **extra: Any) -> dict[str, Any]:
    node = {
        "id": node_id,
        "title": f"node {node_id}",
        "status": status,
        "status_since": "2026-10-08T21:00:00+00:00",
        "url": None,
        "pr_url": None,
        "external": False,
        "stuck": None,
    }
    node.update(extra)
    return node


def _in_review(node_id: str = ITEM, pr_url: str = PR_URL) -> dict[str, Any]:
    return _node(
        node_id,
        "in_review",
        pr_url=pr_url,
        stuck=f"in_review — awaiting merge (PR #{pr_url.rsplit('/', 1)[-1]})",
    )


def _snapshot(epic: str, signal: str, *nodes: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": 1,
        "epic_ref": epic,
        "signal": signal,
        "counts": {},
        "frontier": [],
        "nodes": list(nodes),
        "edges": [],
    }


class _LedgerRuns(FakeStore):
    """The suite's fake for everything epic-shaped, with the RUN ROWS read
    from a real ledger — the batched ``list_by_items`` the set calls."""

    def __init__(self, ledger: SqliteRunStore, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._ledger = ledger

    def list_by_items(self, item_ids: list[str]) -> dict[str, list[RunRecord]]:
        return self._ledger.list_by_items(item_ids)


def _ledger_with(pr_state: str, *, pr_url: str = PR_URL) -> SqliteRunStore:
    """A copy-of-the-ledger stand-in: one successful run whose outcome sync
    recorded *pr_state* for *pr_url*."""
    ledger = SqliteRunStore(":memory:")
    ledger.save(
        RunRecord(
            run_id="run-2469",
            issue_num=int(ITEM),
            item_id=ITEM,
            branch=f"agent/issue-{ITEM}",
            status="success",
            exit_code=0,
            started_at=NOW - timedelta(hours=2),
        )
    )
    ledger.set_outcome(
        "run-2469",
        pr_url=pr_url,
        pr_state=pr_state,
        pr_merged_at="2026-10-08T22:21:00Z" if pr_state == "MERGED" else None,
        review_cycles=None,
        diff_additions=None,
        diff_deletions=None,
        diff_files=None,
    )
    return ledger


def _store(
    ledger: SqliteRunStore,
    *nodes: dict[str, Any],
    epic: str = EPIC,
    signal: str = "awaiting_merges",
    ticking: bool = True,
) -> _LedgerRuns:
    beat = NOW if ticking else NOW - timedelta(days=1)
    return _LedgerRuns(
        ledger,
        epic_refs=[epic],
        driver_states={
            epic: FakeDriverState(epic_ref=epic, last_tick_started_at=beat.isoformat())
        },
        epic_ticks={epic: FakeEpicTick(snapshot=_snapshot(epic, signal, *nodes))},
    )


def _set(store: Any) -> needs_you.NeedsYouSet:
    return needs_you.needs_you_set(store, _config(), now=NOW)


def _join(store: Any, *nodes: dict[str, Any], epic: str = EPIC, signal: str) -> Any:
    return compose_node_attention(
        store,
        _config(),
        [str(n["id"]) for n in nodes],
        nodes=list(nodes),
        epic_ref=epic,
        signal=signal,
        now=NOW,
    )


# --------------------------------------------------------------------------- #
# AC1 — a wait whose PR merged leaves the set at read time
# --------------------------------------------------------------------------- #


def test_an_in_review_wait_whose_pr_merged_yields_no_needs_you_row() -> None:
    """The #2469 reading. The journal still says "awaiting merge"; the run row
    says MERGED; the set believes the run row, with no tick in between."""
    store = _store(_ledger_with("MERGED"), _in_review())

    result = _set(store)

    assert [e for e in result.entries if e.item_id == ITEM] == []
    assert needs_you.needs_you_by_epic(result.entries).get(EPIC, 0) == 0
    assert result.unavailable == []


def test_the_same_wait_with_its_pr_still_open_is_a_row() -> None:
    """The control: the fixture above is a merge decision until it merges, so
    the test is about the merge and not about a node the set never counted."""
    store = _store(_ledger_with("OPEN"), _in_review())

    rows = [e for e in _set(store).entries if e.item_id == ITEM]

    assert [(r.type, r.attention_kind) for r in rows] == [("epic_wait", "merge_ready")]


def test_an_older_merged_pr_does_not_clear_the_node_s_current_one() -> None:
    """A node reopened onto a second pull request is waiting on THAT one. The
    merge recorded for its first PR is history, not an answer."""
    store = _store(
        _ledger_with("MERGED", pr_url="https://github.com/o/r/pull/2400"),
        _in_review(),
    )

    assert [e.item_id for e in _set(store).entries] == [ITEM]


def test_a_merged_pr_does_not_clear_a_wait_that_is_not_about_it() -> None:
    """Only a wait ON the pull request is answered by its merge. A preserved
    continuation on the same node is somebody else's move entirely."""
    node = _node(ITEM, "awaiting_continuation", pr_url=PR_URL)
    store = _store(_ledger_with("MERGED"), node)

    rows = [e for e in _set(store).entries if e.item_id == ITEM]

    assert [r.command for r in rows] == [f"kew epic continue {EPIC} {ITEM}"]


def test_the_node_join_drops_the_merged_wait_too() -> None:
    """The rail and the epic card read the join, the legend reads the set —
    one invariant, so the diamond goes with the row."""
    merged = _store(_ledger_with("MERGED"), _in_review())
    still_open = _store(_ledger_with("OPEN"), _in_review())

    assert _join(merged, _in_review(), signal="awaiting_merges").by_node == {}
    assert ITEM in _join(still_open, _in_review(), signal="awaiting_merges").by_node


def test_an_unreadable_run_ledger_leaves_the_journaled_wait_standing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A read that did not happen cannot prove a merge, so it suppresses
    nothing — and the log says the waits stood because of it."""

    class _Refusing(FakeStore):
        def list_by_items(self, item_ids: list[str]) -> Any:
            raise RuntimeError("database is locked")

    with caplog.at_level("WARNING"):
        assert merged_pr_waits(_Refusing(), [_in_review()]) == frozenset()

    assert "journaled merge waits stand" in caplog.text


def test_a_merge_any_run_recorded_clears_the_wait_over_a_newer_stale_row() -> None:
    """One rule with every report (``kew.pr_states``): a PR is merged when any
    of its runs recorded the merge, even when a newer row still says OPEN or
    the merged row carries only the merge time."""
    ledger = SqliteRunStore(":memory:")
    for run_id, hours, merged_at in (
        ("writer", 3, "2026-10-08T22:21:00Z"),
        ("stale", 1, None),
    ):
        ledger.save(
            RunRecord(
                run_id=run_id,
                issue_num=int(ITEM),
                item_id=ITEM,
                branch=f"agent/issue-{ITEM}",
                status="success",
                exit_code=0,
                started_at=NOW - timedelta(hours=hours),
                pr_url=PR_URL,
                pr_state="OPEN",
                pr_merged_at=merged_at,
            )
        )

    assert [e for e in _set(_store(ledger, _in_review())).entries] == []


def test_a_store_without_the_batched_read_is_read_per_item() -> None:
    """The suite's plain fake has only ``list_by_item``; the read still works."""
    store = FakeStore(
        all_runs=[
            FakeRun(
                run_id="r",
                issue_num=int(ITEM),
                item_id=ITEM,
                pr_state="MERGED",
                pr_url=PR_URL,
            )
        ]
    )

    assert merged_pr_waits(store, [_in_review()]) == frozenset({(ITEM, "2476")})


# --------------------------------------------------------------------------- #
# AC2 — a stalled epic's person actions are members, each with a command
# --------------------------------------------------------------------------- #


def _go_live_stalled() -> tuple[dict[str, Any], ...]:
    """The go-live banner: two arm waits and the SFD-232 continuation."""
    return (
        _node("SFD-223", "queued", stuck=ARM_WAIT_REASON),
        _node("SFD-228", "queued", stuck=ARM_WAIT_REASON),
        _node(
            "SFD-232",
            "held",
            stuck="awaiting_continuation — preserved worktree; "
            f"resume with `kew epic resume {GO_LIVE} SFD-232`",
        ),
        _node("SFD-201", "done"),
    )


def test_a_stalled_epic_s_two_arm_waits_are_two_rows_with_a_command() -> None:
    store = _store(
        SqliteRunStore(":memory:"),
        *_go_live_stalled(),
        epic=GO_LIVE,
        signal=STALLED_SIGNAL,
        ticking=False,
    )

    entries = _set(store).entries
    arms = [e for e in entries if e.item_id in {"SFD-223", "SFD-228"}]

    assert sorted(e.item_id for e in arms) == ["SFD-223", "SFD-228"]
    known = _cli_command_paths()
    for row in arms:
        assert _names_a_real_command(row.command, known), row.command
        assert row.type == "epic_wait"
        assert row.epic_ref == GO_LIVE
        assert row.label == "awaiting a human arm"
        assert row.command == f"kew epic run {GO_LIVE}"
        assert ARM_LABEL in row.primary_action


def test_every_person_action_on_the_stalled_banner_is_counted() -> None:
    """Arm, arm, continue — and the stalled driver itself — all in go-live's
    bucket, which is what its legend and the Epics list read."""
    store = _store(
        SqliteRunStore(":memory:"),
        *_go_live_stalled(),
        epic=GO_LIVE,
        signal=STALLED_SIGNAL,
        ticking=False,
    )

    entries = _set(store).entries
    mine = needs_you.epic_entries(entries, GO_LIVE)

    assert sorted((e.type, e.item_id or "") for e in mine) == [
        ("epic_wait", "SFD-223"),
        ("epic_wait", "SFD-228"),
        ("epic_wait", "SFD-232"),
        ("stalled_epic", ""),
    ]
    assert all(e.command for e in mine)
    assert needs_you.needs_you_by_epic(entries)[GO_LIVE] == len(mine) == len(entries)


def test_an_arm_wait_on_a_running_epic_stays_quiet() -> None:
    """The driver is still ticking, so an unarmed node is the operator's queue
    to release when they choose — #2209 drew it without a diamond on purpose,
    and the set agrees with the graph: no row, and no join entry."""
    node = _node("SFD-223", "queued", stuck=ARM_WAIT_REASON)
    store = _store(SqliteRunStore(":memory:"), node, epic=GO_LIVE, signal="progressing")

    assert _set(store).entries == []
    assert _join(store, node, epic=GO_LIVE, signal="progressing").by_node == {}


def test_the_node_join_serves_the_stalled_arm_the_same_command() -> None:
    nodes = _go_live_stalled()
    store = _store(
        SqliteRunStore(":memory:"),
        *nodes,
        epic=GO_LIVE,
        signal=STALLED_SIGNAL,
        ticking=False,
    )

    by_node = _join(store, *nodes, epic=GO_LIVE, signal=STALLED_SIGNAL).by_node

    assert by_node["SFD-223"].command == f"kew epic run {GO_LIVE}"
    assert by_node["SFD-232"].command == f"kew epic continue {GO_LIVE} SFD-232"


# --------------------------------------------------------------------------- #
# The strings three layers share
# --------------------------------------------------------------------------- #


def test_the_arm_vocabulary_is_one_string_across_driver_adapter_and_console() -> None:
    assert _FIXED_WAITING_REASONS[DriverStatus.QUEUED] == ARM_WAIT_REASON
    assert _LINEAR_READY_LABEL == ARM_LABEL
    assert TerminalSignal.STALLED.value == STALLED_SIGNAL
    waiting_ts = (FRONTEND / "src/lib/epic/waiting.ts").read_text(encoding="utf-8")
    assert re.search(rf"ARM_WAIT_REASON = '{re.escape(ARM_WAIT_REASON)}'", waiting_ts)
    assert re.search(rf"ARM_LABEL = '{re.escape(ARM_LABEL)}'", waiting_ts)
