"""A one-shot `kew epic watch` answers off the journal, not the forge (#2469).

`kew epic watch design-c3 --json` on a 23-node epic took 92.6 s wall for 3.8 s
of CPU: the read it shared with `kew epic status` re-queried GitHub per node, so
operator pollers that allowed it 240 s still timed out and the attention events
they exist to deliver arrived late or not at all.

These tests pin the two halves of the fix: the one-shot path makes no
subprocess call at all (so a slow forge, or a tick holding the driver lock and
the ledger, cannot make it slow), and the events it composes off the driver's
recorded observations are the ones a live read would have reported.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from kew import cli as _c
from kew.cli import epic_app
from kew.config import DispatchConfig
from kew.epic_holds import BUDGET_HOLD_PREFIX
from kew.graph.domain import Edge, Graph, Node, NodeStatus
from kew.graph.driver.attention import attention_events
from kew.graph.driver.attention_journal import (
    journaled_attention_events,
    read_journaled_observations,
)
from kew.graph.driver.facts import NodeFacts
from kew.graph.driver.overlay import DriverStatus
from kew.graph.driver.result import StuckNode, TerminalSignal, TickResult, TickSummary
from kew.graph.driver.tick import (
    _invalid_graph_result,
    _journal_invalid,
    _waiting_nodes,
    _write_journal,
)
from kew.graph.validate import validate
from kew.run_store import SqliteRunStore
from kew.waits import UNBUCKETED_HELD_HINT

EPIC = "design-c3"
TICK_TS = "2026-10-07T18:04:11+00:00"
runner = CliRunner()


def _config(tmp_path: Path) -> DispatchConfig:
    (tmp_path / ".git").mkdir(exist_ok=True)
    cfg = DispatchConfig()
    cfg.project.repo = "o/r"
    cfg.logging.dir = str(tmp_path)
    return cfg


def _node(nid: str, status: str, **extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": nid,
        "title": f"node {nid}",
        "status": status,
        "status_since": TICK_TS,
        "url": None,
        "pr_url": None,
        "external": False,
        "stuck": None,
    }
    base.update(extra)
    return base


def _journal(
    store: SqliteRunStore,
    nodes: list[dict[str, Any]],
    *,
    edges: list[list[str]] | None = None,
    signal: str = "stalled",
) -> None:
    store.register_epic(EPIC)
    store.append_epic_tick(
        epic_ref=EPIC,
        tick_ts=TICK_TS,
        snapshot={
            "schema": 1,
            "epic_ref": EPIC,
            "signal": signal,
            "counts": {},
            "frontier": [],
            "nodes": nodes,
            "edges": edges or [],
        },
        actions={"schema": 1},
        snapshot_hash="h1",
    )


@contextmanager
def _seeding_store(tmp_path: Path) -> Any:
    """A managed ledger at the path the CLI will reopen read-only.

    The `.git` marker comes first: `_open_run_store` canonicalizes the project
    root, so a seed taken before the marker exists registers the ledger under a
    DIFFERENT root than the invocation's and the read-only open then refuses it.
    """
    (tmp_path / ".git").mkdir(exist_ok=True)
    with _c._open_run_store(str(tmp_path / "dispatch.db")) as opened:
        yield opened


@pytest.fixture
def store(tmp_path: Path) -> Any:
    opened = SqliteRunStore(str(tmp_path / "dispatch.db"), project_root=tmp_path)
    yield opened
    opened.close()


# ---------------------------------------------------------------------------
# Composing events off the journal
# ---------------------------------------------------------------------------


def _events(store: Any) -> tuple[Any, ...]:
    observations = read_journaled_observations(store.latest_epic_tick(EPIC))
    assert observations is not None
    return journaled_attention_events(EPIC, observations, store=store)


def test_a_journaled_in_review_node_is_a_merge_ready_event(store: Any) -> None:
    """The reason names the PR the journal recorded, exactly as a live read's
    does — the PR number comes off the snapshot's own `pr_url`."""
    _journal(
        store,
        [_node("7", "in_review", pr_url="https://github.com/o/r/pull/41")],
    )

    (event,) = _events(store)

    assert event.kind.value == "merge_ready"
    assert event.item == "7"
    assert event.reason == "in_review — awaiting merge (PR #41)"
    assert event.action


def test_a_journaled_hold_carries_the_ledger_hold_reason(store: Any) -> None:
    """A held node's reason is NOT in the snapshot — it is a row in
    `epic_holds`, which is a ledger read. Reading it is what keeps the kind
    (`budget_exhausted`, not the generic held fallback) the same as live."""
    store.hold_epic_node_item(EPIC, "9", f"{BUDGET_HOLD_PREFIX} $4.00 over")
    _journal(store, [_node("9", "held")])

    (event,) = _events(store)

    assert event.kind.value == "budget_exhausted"
    assert event.reason.startswith(BUDGET_HOLD_PREFIX)


def test_a_held_node_with_no_hold_row_reads_as_merged_unclosed(store: Any) -> None:
    """The one HELD state with no `epic_holds` row is a merged PR whose issue is
    still open, which the next tick closes — nobody's job, so no event."""
    _journal(store, [_node("3", "held")])

    assert _events(store) == ()


def test_blast_radius_comes_off_the_journaled_edges(store: Any) -> None:
    """Transitive dependents, the closure `Graph.transitive_dependents` walks,
    computed from the snapshot's `[src, dst]` edge list."""
    _journal(
        store,
        [
            _node("1", "in_review", pr_url="https://github.com/o/r/pull/1"),
            _node("2", "todo"),
            _node("3", "todo"),
        ],
        edges=[["1", "2"], ["2", "3"]],
    )

    (event,) = _events(store)

    assert event.blast_radius == ("2", "3")


def test_a_complete_epic_reports_its_completion_and_nothing_else(store: Any) -> None:
    _journal(
        store,
        [_node("1", "in_review", pr_url="https://github.com/o/r/pull/1")],
        signal="complete",
    )

    (event,) = _events(store)

    assert event.kind.value == "epic_complete"
    assert event.item == EPIC


def test_progressing_statuses_and_external_nodes_need_nobody(store: Any) -> None:
    """todo/in_progress/done are work in motion, and an external boundary node
    is not an epic member — none of them is a person's job."""
    _journal(
        store,
        [
            _node("1", "todo"),
            _node("2", "in_progress"),
            _node("3", "done"),
            {"id": "4", "status": "in_review", "external": True},
        ],
    )

    assert _events(store) == ()


def test_a_status_word_this_build_has_no_member_for_refuses_the_snapshot(
    store: Any,
) -> None:
    """A snapshot is JSON written by whichever kew ran the tick, so a NEWER
    one's status word is never a crash — and never a row to drop either.

    Dropping it is the invalid-graph failure one node down (#2476 review): the
    epic's only waiting node is the unreadable one, so the watch reports that
    nobody is waiting while the hold behind it stands. `None` is the caller's
    signal to pay for the live read, which computes the overlay itself.
    """
    _journal(store, [_node("1", "teleported"), _node("2", "held")])
    store.hold_epic_node_item(EPIC, "2", f"{BUDGET_HOLD_PREFIX} over")

    assert read_journaled_observations(store.latest_epic_tick(EPIC)) is None


def test_a_recorded_stuck_reason_wins_over_the_status_word(store: Any) -> None:
    """The driver wrote the richer line down; the journal hands it back."""
    _journal(
        store,
        [
            _node(
                "5",
                "in_review",
                pr_url="https://github.com/o/r/pull/5",
                stuck="review-stalled",
            )
        ],
    )

    (event,) = _events(store)

    assert event.reason == "review-stalled"
    assert event.kind.value == "merge_ready"


def test_no_journaled_tick_reads_as_no_observations(store: Any) -> None:
    assert read_journaled_observations(store.latest_epic_tick(EPIC)) is None


# ---------------------------------------------------------------------------
# The one-shot CLI path: no subprocess, no lock, bounded time
# ---------------------------------------------------------------------------


def _invoke_watch(tmp_path: Path, *args: str, delay: float = 0.0) -> Any:
    """Drive `kew epic watch` with a fake forge that DELAYS every call.

    `kew.subprocess_utils.run` is the allowlist choke point every `gh` read
    resolves through at call time (``run_with_backoff`` reads it off the
    module), so a counting fake here sees the per-node calls the one-shot path
    must not make.
    """
    calls: list[list[str]] = []

    def _fake_run(cmd: list[str], **kwargs: Any) -> Any:
        calls.append(list(cmd))
        if delay:
            time.sleep(delay)
        raise AssertionError(f"the one-shot watch shelled out: {cmd}")

    with (
        patch("kew.cli.load_config", return_value=_config(tmp_path)),
        patch("kew.subprocess_utils.run", side_effect=_fake_run),
        patch("kew.cli.summarize", side_effect=AssertionError("live read")),
        patch("kew.cli.preflight_tools", side_effect=AssertionError("preflight")),
    ):
        started = time.monotonic()
        invoked = runner.invoke(epic_app, ["watch", EPIC, *args])
    invoked.elapsed = time.monotonic() - started  # type: ignore[attr-defined]
    invoked.gh_calls = calls  # type: ignore[attr-defined]
    return invoked


def test_one_shot_watch_of_a_23_node_epic_makes_no_per_node_call(
    tmp_path: Path,
) -> None:
    """The proof #2469 asked for: 23 nodes, a forge that costs a second per
    call, a tick holding the driver lock — and the watch still answers off the
    journal in well under five seconds, with the events it owes the poller."""
    nodes = [_node(str(n), "todo") for n in range(1, 22)]
    nodes.append(_node("22", "in_review", pr_url="https://github.com/o/r/pull/22"))
    nodes.append(_node("23", "held"))
    with _seeding_store(tmp_path) as seeded:
        seeded.hold_epic_node_item(
            EPIC, "23", f"{BUDGET_HOLD_PREFIX} $9.00 over the cap"
        )
        _journal(seeded, nodes)
    # The lock a mid-drive tick would be holding. The watch is read-only, so it
    # must never wait on it — nor even look.
    (tmp_path / "epic-lock.json").write_text(json.dumps({"pid": 1}), encoding="utf-8")

    invoked = _invoke_watch(tmp_path, "--json", delay=1.0)

    assert invoked.gh_calls == [], invoked.output
    assert invoked.elapsed < 5.0
    assert invoked.exit_code == 1, invoked.output
    events = [json.loads(line) for line in invoked.stdout.splitlines() if line.strip()]
    assert [e["kind"] for e in events] == ["merge_ready", "budget_exhausted"]
    assert [e["item"] for e in events] == ["22", "23"]


def test_every_json_event_names_when_its_observations_were_recorded(
    tmp_path: Path,
) -> None:
    """A cheap answer must be an honest one: the watch bounds its own staleness
    with a field rather than blocking until the facts are current."""
    with _seeding_store(tmp_path) as seeded:
        _journal(
            seeded,
            [_node("4", "in_review", pr_url="https://github.com/o/r/pull/4")],
        )

    invoked = _invoke_watch(tmp_path, "--json")

    events = [json.loads(line) for line in invoked.stdout.splitlines() if line.strip()]
    assert [e["observations_stale_since"] for e in events] == [TICK_TS]


def test_the_human_line_says_how_stale_the_observations_are(tmp_path: Path) -> None:
    with _seeding_store(tmp_path) as seeded:
        _journal(
            seeded,
            [_node("4", "in_review", pr_url="https://github.com/o/r/pull/4")],
        )

    invoked = _invoke_watch(tmp_path)

    assert f"observations stale since {TICK_TS}" in invoked.output
    assert "merge_ready 4" in invoked.output


def test_an_unticked_epic_still_pays_for_a_live_read(tmp_path: Path) -> None:
    """Nothing recorded is not the same as nothing waiting. With no journaled
    tick the watch falls back to the full live read rather than reporting a
    just-created epic as quiet."""
    with (
        patch("kew.cli.load_config", return_value=_config(tmp_path)),
        patch("kew.cli.preflight_tools", side_effect=AssertionError("live read")),
    ):
        invoked = runner.invoke(epic_app, ["watch", EPIC, "--json"])

    assert isinstance(invoked.exception, AssertionError)


def test_a_held_ledger_write_transaction_does_not_stall_the_watch(
    tmp_path: Path,
) -> None:
    """Suspect two in #2469: the watch waiting on the ledger a 30-70 minute
    tick is holding. It opens read-only — no migration, no `journal_mode`
    pragma, nothing that wants the write lock — so a tick mid-transaction
    cannot delay it."""
    with _seeding_store(tmp_path) as seeded:
        _journal(
            seeded,
            [_node("8", "in_review", pr_url="https://github.com/o/r/pull/8")],
        )
    writer = sqlite3.connect(str(tmp_path / "dispatch.db"))
    writer.execute("BEGIN IMMEDIATE")
    try:
        invoked = _invoke_watch(tmp_path, "--json")
    finally:
        writer.rollback()
        writer.close()

    assert invoked.exit_code == 1, invoked.output
    assert invoked.elapsed < 5.0
    assert [json.loads(line)["item"] for line in invoked.stdout.splitlines()] == ["8"]


# ---------------------------------------------------------------------------
# What the snapshot's `stuck` map actually holds (#2476 review)
# ---------------------------------------------------------------------------

#: A hold reason no `STALL_BUCKETS` prefix names. The vocabulary is additive, so
#: every hold family arrives here before it gets a bucket (`kew.waits` documents
#: three that did), and `UNBUCKETED_HELD_HINT` is what carries it meanwhile.
UNBUCKETED_HOLD = "weather_hold: the runner host is under water"


def test_a_tick_journals_its_waiting_reasons_in_the_stuck_map(store: Any) -> None:
    """`_write_journal` folds `result.waiting` AND `result.stuck` into the one
    snapshot `nodes[].stuck` map (#661, so the epics DTO's `stuck` field
    carries the held/in_review/blocked lines too).

    Pinned here because the journaled reader is built on top of it: read that
    map as "the stuck ones" and every WAITING node looks stuck.
    """
    graph = Graph(
        [
            Node(id="1", title="t1", status=NodeStatus.TODO, epic_ref=EPIC),
            Node(id="2", title="t2", status=NodeStatus.TODO, epic_ref=EPIC),
        ],
        [],
    )
    store.register_epic(EPIC)
    _write_journal(
        EPIC,
        graph,
        TickResult(
            dispatched=(),
            merged=(),
            held=("1",),
            in_flight=(),
            failed=(),
            cancelled=(),
            stuck=(StuckNode("2", "failed-terminal (exit 1)", ()),),
            done=(),
            remaining=2,
            signal=TerminalSignal.STALLED,
            waiting=(StuckNode("1", UNBUCKETED_HOLD, ()),),
        ),
        store=store,
        statuses={"1": DriverStatus.HELD, "2": DriverStatus.FAILED},
        node_facts={},
        frontier=[],
    )

    tick = store.latest_epic_tick(EPIC)
    assert tick is not None
    by_id = {node["id"]: node for node in tick.snapshot["nodes"]}
    # The WAITING node's reason is in `stuck` beside the stuck one's.
    assert by_id["1"]["stuck"] == UNBUCKETED_HOLD
    assert by_id["2"]["stuck"] == "failed-terminal (exit 1)"

    observations = read_journaled_observations(tick)
    assert observations is not None
    assert observations.reasons["1"] == UNBUCKETED_HOLD


def test_an_unbucketed_hold_reason_still_names_a_person(store: Any) -> None:
    """A journaled HELD node is HELD, and an honored hold no bucket names must
    not go silent (`kew.waits.UNBUCKETED_HELD_HINT`).

    Reading "has a recorded reason" as "is stuck, so not held" is what took the
    fallback away: every waiting reason is in that map, so the test collapsed to
    `holds_node_out_of_frontier`, which an unbucketed reason fails by
    definition. The overlay status is the hold table on this path.
    """
    store.hold_epic_node_item(EPIC, "9", UNBUCKETED_HOLD)
    _journal(store, [_node("9", "held", stuck=UNBUCKETED_HOLD)])

    (event,) = _events(store)

    assert event.kind.value == "held"
    assert event.reason == UNBUCKETED_HOLD
    assert event.action == UNBUCKETED_HELD_HINT


def test_a_waiting_node_with_its_reason_recorded_is_not_read_as_stuck(
    store: Any,
) -> None:
    """The same snapshot shape a real tick writes: every waiting node carries
    its reason. The in_review node is still a merge_ready item, and the held
    one still a hold — neither is relabelled by sitting in that map."""
    _journal(
        store,
        [
            _node(
                "4",
                "in_review",
                pr_url="https://github.com/o/r/pull/4",
                stuck="in_review — awaiting merge (PR #4)",
            ),
            _node("5", "held", stuck=f"{BUDGET_HOLD_PREFIX} $4.00 over"),
        ],
    )
    store.hold_epic_node_item(EPIC, "5", f"{BUDGET_HOLD_PREFIX} $4.00 over")

    assert [(e.item, e.kind.value) for e in _events(store)] == [
        ("4", "merge_ready"),
        ("5", "budget_exhausted"),
    ]


def _in_review_facts(pr: int) -> NodeFacts:
    """The one fact an IN_REVIEW node's waiting reason is composed from."""
    return NodeFacts(
        issue_num=0,
        state="OPEN",
        state_reason=None,
        labels=frozenset(),
        pr_number=pr,
        pr_url=f"https://github.com/o/r/pull/{pr}",
        review_decision=None,
        merge_state_status=None,
        head_oid=None,
        pr_pushed_at=None,
    )


def _live_event_order(store: Any, ids: list[str]) -> list[str]:
    """The items a LIVE read emits for the same in-review nodes.

    The rows come from `tick._waiting_nodes` itself, not from a restatement of
    the sort on its last line, so what this compares against is the driver and
    not a copy of it.
    """
    graph = Graph(
        [
            Node(id=nid, title=f"node {nid}", status=NodeStatus.TODO, epic_ref=EPIC)
            for nid in ids
        ],
        [],
    )
    waiting = _waiting_nodes(
        graph,
        {nid: DriverStatus.IN_REVIEW for nid in ids},
        {nid: _in_review_facts(pr) for pr, nid in enumerate(ids, start=1)},
        store=store,
        holds_by_item={},
        continuation_reason_by_item={},
        frontier_status={},
        stuck_ids=set(),
    )
    summary = TickSummary(
        counts={},
        frontier=(),
        in_flight=(),
        held=(),
        failed=(),
        cancelled=(),
        stuck=(),
        done=(),
        remaining=len(ids),
        signal=TerminalSignal.STALLED,
        waiting=tuple(waiting),
    )
    return [event.item for event in attention_events(EPIC, summary)]


def test_events_come_out_in_the_live_reads_own_node_order(store: Any) -> None:
    """One epic must not be two stories about what is waiting FIRST.

    A snapshot watch and the `--follow`/fallback live read list the same
    states, so they list them in the same order: `tick._waiting_nodes` sorts
    its rows by the plain `node_id` string, and the journaled read sorts the
    same way. Keying it on the natural `item_id_sort_key` instead put node 9
    ahead of node 10 on one path and behind it on the other (#2476 review).
    """
    ids = ["10", "9", "SFD-2"]
    _journal(
        store,
        [
            _node(nid, "in_review", pr_url=f"https://github.com/o/r/pull/{nid}")
            for nid in ids
        ],
    )

    journaled = [e.item for e in _events(store)]

    assert journaled == _live_event_order(store, ids)
    assert journaled == ["10", "9", "SFD-2"]


def test_a_legacy_unregistered_ledger_pays_for_the_live_read(tmp_path: Path) -> None:
    """A ledger with no registry entry and no identity binding is refused by
    the READ-ONLY open alone (`ledger_identity.open_managed_store`): adopting
    and registering one is a write, so the read-write open the live read uses
    does it and carries on. Routing that refusal through `_c._open_run_store`
    made a snapshot watch report `REGISTERED-LEDGER-MISSING` and exit 2 on a
    ledger every other `kew` command still reads (#2476 review).
    """
    seeded = SqliteRunStore(str(tmp_path / "dispatch.db"), project_root=tmp_path)
    try:
        _journal(
            seeded,
            [_node("4", "in_review", pr_url="https://github.com/o/r/pull/4")],
        )
    finally:
        seeded.close()
    legacy = sqlite3.connect(str(tmp_path / "dispatch.db"))
    legacy.execute("DROP TABLE ledger_identity")
    legacy.commit()
    legacy.close()

    with (
        patch("kew.cli.load_config", return_value=_config(tmp_path)),
        patch("kew.cli.preflight_tools", side_effect=AssertionError("live read")),
    ):
        invoked = runner.invoke(epic_app, ["watch", EPIC, "--json"])

    assert isinstance(invoked.exception, AssertionError), invoked.output
    assert "REGISTERED-LEDGER-MISSING" not in invoked.output


def test_a_ledger_with_no_epic_ticks_table_pays_for_the_live_read(
    tmp_path: Path,
) -> None:
    """The read-only open SKIPS `_migrate` by design — a reader must not bump
    the schema under a live older process — so the ledger it reads can be one
    whose schema predates a table this build queries, `epic_ticks` itself on a
    pre-journal one. An unhandled `sqlite3.OperationalError` there exits 1,
    which is the code this command documents as "somebody is waiting on you".
    """
    with _seeding_store(tmp_path) as seeded:
        _journal(
            seeded,
            [_node("4", "in_review", pr_url="https://github.com/o/r/pull/4")],
        )
    older = sqlite3.connect(str(tmp_path / "dispatch.db"))
    older.execute("DROP TABLE epic_ticks")
    older.commit()
    older.close()

    with (
        patch("kew.cli.load_config", return_value=_config(tmp_path)),
        patch("kew.cli.preflight_tools", side_effect=AssertionError("live read")),
    ):
        invoked = runner.invoke(epic_app, ["watch", EPIC, "--json"])

    assert isinstance(invoked.exception, AssertionError), invoked.output


# ---------------------------------------------------------------------------
# The snapshot an invalid-graph tick wrote records no observations (#2476 review)
# ---------------------------------------------------------------------------


def _journal_an_invalid_graph(store: Any, *, held: str | None = None) -> None:
    """Journal exactly what a tick that found a cyclic graph journals.

    `tick.run_tick` validates before it reads any overlay or facts, so the
    branch it takes there writes a snapshot with NO statuses and NO node facts
    (`_journal_invalid`): every node lands with its raw tracker status and the
    one `invalid-graph:` reason. The hold is real and predates the cycle — it
    is what a person is still waiting to clear.
    """
    graph = Graph(
        [
            Node(id="1", title="t1", status=NodeStatus.TODO, epic_ref=EPIC),
            Node(id="2", title="t2", status=NodeStatus.TODO, epic_ref=EPIC),
        ],
        [Edge(src="1", dst="2"), Edge(src="2", dst="1")],
    )
    report = validate(graph)
    assert not report.ok
    store.register_epic(EPIC)
    if held is not None:
        store.hold_epic_node_item(EPIC, "1", held)
    _journal_invalid(EPIC, graph, _invalid_graph_result(graph, report), store=store)


def test_an_invalid_graph_snapshot_records_no_observations(store: Any) -> None:
    """Nothing in that snapshot can name a person, so it is not read at all.

    Its statuses are the TRACKER's, not the overlay's — the node held on a
    budget preflight reads `todo` — and its one reason is unbucketed, so every
    node would classify as nobody's job and the watch would report a frozen
    epic as QUIET. `None` is the caller's signal to pay for the live read.
    """
    _journal_an_invalid_graph(store, held=f"{BUDGET_HOLD_PREFIX} $7.00 over")

    tick = store.latest_epic_tick(EPIC)
    assert tick is not None
    # The shape being refused, pinned so a writer-side change is caught here.
    assert [n["status"] for n in tick.snapshot["nodes"]] == ["todo", "todo"]
    assert all(n["stuck"].startswith("invalid-graph:") for n in tick.snapshot["nodes"])

    assert read_journaled_observations(tick) is None


def test_a_frozen_epic_still_pays_for_a_live_read(tmp_path: Path) -> None:
    """The CLI end of it: a watch whose last journaled tick is an invalid-graph
    one falls back to the live read instead of emitting zero events and exiting
    0 while a real hold waits.

    The live read is also the one that can answer: it rebuilds the graph from
    the tracker, so the instant an operator breaks the cycle it reports the
    holds and merge-ready PRs this snapshot never recorded.
    """
    with _seeding_store(tmp_path) as seeded:
        _journal_an_invalid_graph(seeded, held=f"{BUDGET_HOLD_PREFIX} $7.00 over")

    with (
        patch("kew.cli.load_config", return_value=_config(tmp_path)),
        patch("kew.cli.preflight_tools", side_effect=AssertionError("live read")),
    ):
        invoked = runner.invoke(epic_app, ["watch", EPIC, "--json"])

    assert isinstance(invoked.exception, AssertionError)


# ---------------------------------------------------------------------------
# Dating the observations: the heartbeat, not the dedup'd row (#2476 review)
# ---------------------------------------------------------------------------

#: A later finished tick than the one that journaled `TICK_TS`.
LATER_TICK_TS = "2026-10-07T18:44:11+00:00"
#: Earlier than it — a heartbeat that cannot date these observations.
EARLIER_TICK_TS = "2026-10-07T17:04:11+00:00"


def _finished_tick(store: Any, ts: str) -> None:
    """The heartbeat every FINISHED tick writes beside the journal.

    `tick._finish_heartbeat`'s own two calls, so what is read back is the row a
    real drive leaves — including on the nine ticks in ten that change no state
    and therefore append no journal row at all.
    """
    store.begin_epic_tick(EPIC, ts)
    store.finish_epic_tick(
        EPIC,
        tick_ts=ts,
        duration_s=0.5,
        interval_s=None,
        signal=TerminalSignal.STALLED.value,
        tick_id=None,
    )


def _observations(store: Any) -> Any:
    """Read the journal the way the CLI does: the tick AND the heartbeat row."""
    return read_journaled_observations(
        store.latest_epic_tick(EPIC),
        driver_state=store.get_epic_driver_state(EPIC),
    )


def test_a_held_epics_stamp_advances_with_the_tick_that_changed_nothing(
    store: Any,
) -> None:
    """`epic_ticks` is a CHANGE journal: `append_epic_tick` dedups on the
    snapshot hash, so a held epic's latest row freezes at the instant its state
    first appeared (the merge-path epic ran 476 ticks and journaled 18).

    Dated by that row alone, `observations_stale_since` cannot tell a driver
    that ticked ten minutes ago from one that died days ago — the exact mistake
    the field exists to prevent. `epic_driver_state.last_tick_ts` is written by
    every finished tick, one point read away on the same connection.
    """
    _journal(store, [_node("9", "held", stuck=f"{BUDGET_HOLD_PREFIX} $4.00 over")])
    store.hold_epic_node_item(EPIC, "9", f"{BUDGET_HOLD_PREFIX} $4.00 over")
    # Ten more ticks that each looked and found the same state.
    for _ in range(10):
        _finished_tick(store, LATER_TICK_TS)

    # The row behind the read did NOT move — that is the whole problem.
    assert store.count_epic_ticks(EPIC) == 1
    assert store.latest_epic_tick(EPIC).tick_ts == TICK_TS

    observations = _observations(store)
    assert observations is not None
    assert observations.recorded_at == LATER_TICK_TS
    # Still the snapshot's own observations: only their date came off the row.
    assert [e.item for e in _events(store)] == ["9"]


@pytest.mark.parametrize(
    "heartbeat",
    [None, EARLIER_TICK_TS, "not-a-timestamp"],
    ids=["never-finished-a-tick", "older-than-the-row", "unparseable"],
)
def test_the_stamp_never_predates_the_journal_row(
    store: Any, heartbeat: str | None
) -> None:
    """The heartbeat only ever moves the stamp FORWARD.

    A tick that journaled and then died before its heartbeat landed, a row no
    tick has finished yet (`register_epic` seeds one with a NULL stamp), and a
    stamp nobody can parse all leave the journal row's own instant standing: a
    date that is too old is readable, and one invented from an absence is not.
    """
    _journal(store, [_node("4", "in_review", pr_url="https://github.com/o/r/pull/4")])
    if heartbeat is not None:
        _finished_tick(store, heartbeat)

    observations = _observations(store)
    assert observations is not None
    assert observations.recorded_at == TICK_TS


def test_the_json_stamp_is_the_drivers_last_finished_tick(tmp_path: Path) -> None:
    """End to end: the field a poller ages the events by is the last time
    anything LOOKED at the epic, not the tick its state first appeared on."""
    with _seeding_store(tmp_path) as seeded:
        _journal(
            seeded,
            [_node("4", "in_review", pr_url="https://github.com/o/r/pull/4")],
        )
        _finished_tick(seeded, LATER_TICK_TS)
        assert seeded.count_epic_ticks(EPIC) == 1

    invoked = _invoke_watch(tmp_path, "--json")

    events = [json.loads(line) for line in invoked.stdout.splitlines() if line.strip()]
    assert [e["observations_stale_since"] for e in events] == [LATER_TICK_TS]


def test_a_status_word_only_a_newer_kew_knows_pays_for_a_live_read(
    tmp_path: Path,
) -> None:
    """The CLI end of the whole-snapshot refusal: the epic's only waiting node
    carries a status word this build has no `DriverStatus` member for, and a
    real `epic_holds` row stands behind it.

    Dropping that node emitted nothing and exited 0 — "nobody is waiting" — on
    an epic with somebody waiting (#2476 review). The live read computes the
    overlay itself, so it is the read that can name the hold.
    """
    with _seeding_store(tmp_path) as seeded:
        seeded.hold_epic_node_item(EPIC, "1", f"{BUDGET_HOLD_PREFIX} $7.00 over")
        _journal(seeded, [_node("1", "teleported")])

    with (
        patch("kew.cli.load_config", return_value=_config(tmp_path)),
        patch("kew.cli.preflight_tools", side_effect=AssertionError("live read")),
    ):
        invoked = runner.invoke(epic_app, ["watch", EPIC, "--json"])

    assert isinstance(invoked.exception, AssertionError), invoked.output
