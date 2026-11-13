"""`/api/epics` and `kew epic status` tell one story about a finished epic (#2392).

Found 2026-10-06 on go-live-fixes — six issues, all closed in September:

    kew epic status go-live-fixes  ->  done=6 remaining=0, signal complete
    GET /api/epics                 ->  done=2 held=2 remediating=2, progressing

So the Epics list, and anything else reading that route, showed a finished epic
as active with two held nodes. Two readings of one fact: the CLI derives a
node's state live from the tracker, the console reads it off the newest
journaled snapshot — and that journal stopped before the merges landed, with
per-node ``held``/``remediating`` rows nothing ever cleared.

:mod:`kew.epic_node_states` now owns the one sentence both surfaces read — a
node whose tracker item is CLOSED behind a MERGED pull request is ``done`` — so
this module seeds exactly that ledger and asserts the two agree, counts and
signal, number for number. Nothing here asserts WHICH words they use; it
asserts they use the same ones.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from support import driver_fakes as _fx
from support.driver_fakes import (
    EPIC,
    NOW,
    FakeFactsReader,
    FakeSource,
    _config,
    _facts,
    _node,
)

from kew.epic_node_states import MERGED_NOT_CLOSED_REASON
from kew.graph.build import build_graph
from kew.graph.driver.result import TerminalSignal
from kew.graph.driver.tick import summarize
from kew.run_store import RunRecord, SqliteRunStore
from kew.service.epics import (
    get_epic_detail,
    list_epics,
    reconciled_epic_snapshots,
)
from kew.service.needs_you import needs_you_by_epic, needs_you_set

_no_real_git = _fx._no_real_git
_isolate_tool_paths = _fx._isolate_tool_paths

#: The incident's own shape: six member nodes, and a journal that froze with
#: two of them done, two held and two remediating.
_STALE_STATUSES = {
    "101": "done",
    "102": "done",
    "103": "held",
    "104": "held",
    "105": "remediating",
    "106": "remediating",
}

#: Which items the ledger proves landed. ``epic_merge`` is kew's own merge
#: attestation, and the ONLY evidence serve lands a node on: the merge pass
#: writes it after the pull request merged AND it read the tracker item CLOSED,
#: so it carries both halves of the rule (#2392 review).
_ATTESTED = ("103", "104", "105", "106")

#: The merged run rows the same merges leave in the run ledger. Seeded on two
#: of the four so the fixture carries both shapes the ledger really holds —
#: they prove the PR half and nothing about the tracker, so they land nothing
#: on their own (``test_a_merged_run_row_alone_never_lands_a_node``).
_MERGED_ROWS = ("105", "106")

_TICK_TS = "2026-09-30T00:00:00+00:00"


@pytest.fixture
def store(tmp_path: Path) -> Any:
    s = SqliteRunStore(str(tmp_path / "dispatch.db"))
    yield s
    s.close()


def _pr_number(item: str) -> int:
    """The pull request this node's work rides on.

    Deliberately NOT the item number: every evidence arm matches a merge
    against the pull request the journal is pointing at, and a fixture where
    the two numbers coincide cannot tell a matched PR from an unmatched one
    (#2392 review).
    """
    return 900 + int(item)


def _pr_url(item: str) -> str:
    return f"https://example.invalid/acme/app/pull/{_pr_number(item)}"


#: A pull request the journal is NOT pointing at: the one that merged back in
#: September, before the node was reopened for more work.
_OTHER_PR = "https://example.invalid/acme/app/pull/4242"


def _merged_run_row(store: Any, item: str, pr_url: str) -> None:
    """What a merge leaves in the RUN ledger: a row whose PR state is MERGED.

    The only thing any non-driver path persists about a merge (serve's own
    sweep, the outcome reconciliation). Nothing in it, or anywhere else serve
    can read, says whether the tracker item closed behind it.
    """
    store.save(
        RunRecord(
            run_id=f"run-{item}-{_pr_number_in(pr_url)}",
            issue_num=int(item),
            item_id=item,
            branch=f"issue-{item}",
            status="success",
            exit_code=0,
            started_at=datetime.fromisoformat(_TICK_TS),
            finished_at=datetime.fromisoformat(_TICK_TS),
            pr_url=pr_url,
            pr_state="MERGED",
            pr_merged_at=_TICK_TS,
        )
    )


def _pr_number_in(pr_url: str) -> str:
    return pr_url.rsplit("/", 1)[-1]


def _landed_facts(item: str) -> Any:
    """The tracker's reading of a node that landed: closed, behind a merge."""
    return _facts(
        int(item),
        state="CLOSED",
        state_reason="COMPLETED",
        pr_number=_pr_number(item),
        pr_url=_pr_url(item),
        pr_state="MERGED",
    )


#: October: the tick that records node 105 back at work, after September's
#: attested merge closed it.
_REOPENED_TS = "2026-10-05T00:00:00+00:00"


def _journal_node(
    item: str, status: str, *, pr_url: str | None, since: str
) -> dict[str, Any]:
    """One node as the driver journals it (``graph.driver.journal._node_entry``).

    ``status_since`` is the field the recency arm reads: the driver stamps it
    with the tick a node FIRST read this status and carries it forward over
    every later tick that reads the same one.
    """
    return {
        "id": item,
        "title": f"issue {item}",
        "status": status,
        "status_since": since,
        "url": None,
        "pr_url": pr_url,
        "external": False,
        "stuck": "review_rounds_exhausted" if status == "held" else None,
    }


def _append_tick(
    store: Any, nodes: list[dict[str, Any]], *, tick_ts: str, snapshot_hash: str
) -> None:
    counts: dict[str, int] = {}
    for node in nodes:
        counts[node["status"]] = counts.get(node["status"], 0) + 1
    store.append_epic_tick(
        epic_ref=EPIC,
        tick_ts=tick_ts,
        snapshot={
            "schema": 1,
            "epic_ref": EPIC,
            "signal": TerminalSignal.PROGRESSING.value,
            "counts": counts,
            "frontier": [],
            "nodes": nodes,
            "edges": [],
        },
        actions={"schema": 1, "dispatched": [], "merged": [], "held": [], "stuck": []},
        snapshot_hash=snapshot_hash,
    )


def _journal_stale_tick(store: Any, *, pr_urls: bool = True) -> None:
    """The last tick the driver ever wrote for this epic, held rows and all.

    ``pr_urls=False`` is the OTHER journal shape the driver writes: a node whose
    tracker read carried no pull request at all (the Linear adapter's, see
    ``epics._ledger_pr_urls``). Nothing in the snapshot then names a pull
    request for the node, which is the reading a merge-matching rule has least
    to go on.
    """
    _append_tick(
        store,
        [
            _journal_node(
                item,
                status,
                pr_url=_pr_url(item) if pr_urls else None,
                since=_TICK_TS,
            )
            for item, status in _STALE_STATUSES.items()
        ],
        tick_ts=_TICK_TS,
        snapshot_hash="stale",
    )


def _journal_reopened_tick(store: Any, item: str, *, pr_url: str | None) -> None:
    """A LATER tick: the driver put *item* back to work after the merge.

    The journal's newest reading of the node is ``todo`` as of October, after
    September's attested merge — the tracker item was reopened for more work.
    Every other node stays exactly as the stale tick left it, so the repair
    those attestations DO make is asserted alongside the one they must not.
    """
    _append_tick(
        store,
        [
            _journal_node(
                other,
                "todo" if other == item else status,
                pr_url=pr_url if other == item else _pr_url(other),
                since=_REOPENED_TS if other == item else _TICK_TS,
            )
            for other, status in _STALE_STATUSES.items()
        ],
        tick_ts=_REOPENED_TS,
        snapshot_hash="reopened",
    )


def _seed_ledger(store: Any) -> None:
    """The holds nobody cleared, plus the evidence that the work landed."""
    for item in ("103", "104"):
        store.hold_epic_node_item(EPIC, item, "review_rounds_exhausted")
    for item in _ATTESTED:
        store.append_audit_event(
            timestamp=_TICK_TS,
            event="epic_merge",
            issue_num=int(item),
            item_id=item,
            actor_type="human",
            actor_id="tom",
            payload={
                "trigger": "approval",
                "actor": "tom",
                "sha": "abc",
                "pr": _pr_number(item),
            },
        )
    for item in _MERGED_ROWS:
        _merged_run_row(store, item, _pr_url(item))


def _cli_summary(store: Any, *, live: str | None = None) -> Any:
    """What `kew epic status` reads — the tracker, through the tick's overlay.

    *live* names the one item the tracker still has OPEN with no pull request: a
    node reopened for more work after its earlier merge. Every other item reads
    closed behind a merge.
    """
    deps = FakeSource(nodes=[_node(item) for item in _STALE_STATUSES], edges=[])
    facts = FakeFactsReader(
        issue_facts_by_num={
            int(item): (_facts(int(item)) if item == live else _landed_facts(item))
            for item in _STALE_STATUSES
        }
    )
    graph = build_graph(deps, EPIC)
    return summarize(EPIC, graph, config=_config(), store=store, facts=facts, now=NOW)


@pytest.fixture
def finished_epic(store: Any) -> Any:
    _journal_stale_tick(store)
    _seed_ledger(store)
    return store


def test_the_cli_reads_a_landed_node_done_over_its_own_stale_hold(
    finished_epic: Any,
) -> None:
    """The hold row is still there; the tracker has closed the item behind a
    merged PR. A hold governs LIVE work, and nothing clears one once its node
    stops being dispatched, so the tracker decides."""
    summary = _cli_summary(finished_epic)

    assert summary.counts == {"done": 6}
    assert summary.remaining == 0
    assert summary.held == ()
    assert summary.signal is TerminalSignal.COMPLETE


def test_the_list_reads_the_same_counts_from_the_ledger(finished_epic: Any) -> None:
    """The journal still says held/remediating and no further tick will ever
    correct it. The ledger's own merge evidence is what the list reads."""
    rows = {row.epic_ref: row for row in list_epics(finished_epic)}

    assert rows[EPIC].counts == {"done": 6}
    assert rows[EPIC].signal == TerminalSignal.COMPLETE.value


def test_the_list_and_the_cli_report_the_same_counts_and_signal(
    finished_epic: Any,
) -> None:
    """The whole claim of #2392, as one assertion: one epic, one answer."""
    summary = _cli_summary(finished_epic)
    row = next(r for r in list_epics(finished_epic) if r.epic_ref == EPIC)

    assert row.counts == summary.counts
    assert row.signal == summary.signal.value


def test_the_detail_route_agrees_with_the_list_node_by_node(
    finished_epic: Any,
) -> None:
    """A fix that moves the disagreement from the list to the epic page is not
    a fix: the route the console deep-links to reads every landed node done,
    and drops the held reason the journal recorded with it."""
    detail = get_epic_detail(finished_epic, EPIC)

    assert detail is not None
    assert detail.snapshot is not None
    assert detail.snapshot.counts == {"done": 6}
    assert detail.snapshot.signal == TerminalSignal.COMPLETE.value
    assert {n.id: n.status for n in detail.snapshot.nodes} == dict.fromkeys(
        _STALE_STATUSES, "done"
    )
    assert all(n.stuck is None for n in detail.snapshot.nodes)


def test_a_journal_with_nothing_landed_is_served_verbatim(store: Any) -> None:
    """The reconciliation is a no-op without evidence — no ledger row, no
    repair. An epic mid-flight must keep reading exactly as the driver wrote
    it, held rows included, or this fix would retire live work."""
    _journal_stale_tick(store)

    row = next(r for r in list_epics(store) if r.epic_ref == EPIC)

    assert row.counts == {"done": 2, "held": 2, "remediating": 2}
    assert row.signal == TerminalSignal.PROGRESSING.value


@pytest.mark.parametrize(
    ("pr_urls", "merged_pr"),
    [(True, _pr_url("105")), (True, _OTHER_PR), (False, _pr_url("105"))],
    ids=["the-journal-s-own-pr", "a-superseded-pr", "no-journaled-pr"],
)
def test_a_merged_run_row_alone_never_lands_a_node(
    store: Any, pr_urls: bool, merged_pr: str
) -> None:
    """A merged pull request is not a closed tracker item (#2392 review).

    Serve holds no tracker read. A run row's ``pr_state`` MERGED proves the PR
    half of the rule and says nothing at all about the item, and kew's own merge
    pass is the proof the second does not follow from the first: it closes the
    item itself and re-reads the tracker (``merge._verify_closed``) precisely
    because a merge does not reliably close one — a missing closing keyword on
    GitHub, and on Linear no keyword closes anything.

    Promoted on that row alone, node 105's live remediation was served ``done``
    with its reason dropped, the epic went to ``complete``, and the retirement
    gate emptied its whole attention join — while the needs-you set, which reads
    the item's own work facts rather than this journal, went on filing the merge
    decision the promotion had just called finished. One payload, two answers.

    Run for all three shapes a journal can carry here, because matching the row
    against the journal's pull request is no defence: the row may name the very
    PR the node is pointing at, and a node the journal recorded without a PR
    (the Linear shape) matches anything the ledger holds.
    """
    _journal_stale_tick(store, pr_urls=pr_urls)
    _merged_run_row(store, "105", merged_pr)

    row = next(r for r in list_epics(store) if r.epic_ref == EPIC)
    detail = get_epic_detail(store, EPIC, config=_config())

    assert row.counts == {"done": 2, "held": 2, "remediating": 2}
    assert row.signal == TerminalSignal.PROGRESSING.value
    assert detail is not None and detail.snapshot is not None
    node = next(n for n in detail.snapshot.nodes if n.id == "105")
    assert node.status == "remediating"


def test_the_needs_you_set_keeps_a_node_no_merged_row_has_settled(
    store: Any,
) -> None:
    """The other half of the same invariant, from the queue's side.

    The epic page's diamonds are the node join and its legend is this set, so a
    promotion serve cannot prove does not merely mis-colour one node: it empties
    the join behind the retirement gate while every row the set files from the
    ITEM's facts stays, and the two surfaces of one payload disagree (#2207).
    """
    _journal_stale_tick(store)
    for item in ("103", "104"):
        store.hold_epic_node_item(EPIC, item, "review_rounds_exhausted")
    _merged_run_row(store, "103", _pr_url("103"))

    answer = needs_you_set(store, _config())
    detail = get_epic_detail(store, EPIC, config=_config())

    assert detail is not None and detail.snapshot is not None
    diamonds = sum(1 for n in detail.snapshot.nodes if n.attention is not None)
    assert [e.item_id for e in answer.entries if e.type == "budget_hold"] == [
        "103",
        "104",
    ]
    assert needs_you_by_epic(answer.entries).get(EPIC, 0) == diamonds


def test_a_superseded_attestation_does_not_retire_a_node_on_a_new_pr(
    store: Any,
) -> None:
    """The same rule on the OTHER evidence arm (#2392 review).

    ``epic_merge`` is the stronger evidence — kew merged that pull request and
    saw the item close — but it is evidence about THAT pull request. The arm
    applied no PR check at all and short-circuited ahead of the arm that does,
    so a node reopened onto a new, still-open PR after an earlier attested
    merge was promoted to ``done`` and its epic to ``complete``, contradicting
    ``kew epic status`` and dropping the live merge decision out of the
    needs-you set.
    """
    _journal_stale_tick(store)
    store.append_audit_event(
        timestamp=_TICK_TS,
        event="epic_merge",
        issue_num=105,
        item_id="105",
        actor_type="human",
        actor_id="tom",
        # The PR that merged in September, not the one node 105 now carries.
        payload={
            "trigger": "approval",
            "actor": "tom",
            "sha": "abc",
            "pr": int(_pr_number_in(_OTHER_PR)),
        },
    )

    row = next(r for r in list_epics(store) if r.epic_ref == EPIC)
    detail = get_epic_detail(store, EPIC)

    assert row.counts == {"done": 2, "held": 2, "remediating": 2}
    assert row.signal == TerminalSignal.PROGRESSING.value
    assert detail is not None
    assert detail.snapshot is not None
    assert {n.id: n.status for n in detail.snapshot.nodes}["105"] == "remediating"


def test_a_superseded_attestation_leaves_the_node_to_the_tracker(
    store: Any,
) -> None:
    """Refusing the stale attestation leaves the node where the journal has it.

    The node's new pull request may itself have merged behind a human pressing
    the button — and serve still cannot tell whether that closed the item, so
    the merged row it can see lands nothing (#2392 review). The journal's own
    reading stands, which is the reading serve already served; ``kew epic
    status``, which reads the tracker live, is the surface that can call this
    node done.
    """
    _journal_stale_tick(store)
    store.append_audit_event(
        timestamp=_TICK_TS,
        event="epic_merge",
        issue_num=105,
        item_id="105",
        actor_type="human",
        actor_id="tom",
        payload={
            "trigger": "approval",
            "actor": "tom",
            "sha": "abc",
            "pr": int(_pr_number_in(_OTHER_PR)),
        },
    )
    _merged_run_row(store, "105", _pr_url("105"))

    row = next(r for r in list_epics(store) if r.epic_ref == EPIC)

    assert row.counts == {"done": 2, "held": 2, "remediating": 2}
    assert row.signal == TerminalSignal.PROGRESSING.value


def test_an_attestation_with_no_recorded_pr_still_lands_the_node(
    store: Any,
) -> None:
    """Only a PROVEN mismatch refuses. A pre-``pr``-payload attestation names
    no second pull request, so it still says what it always said: kew merged
    this item's work and saw the tracker close it."""
    _journal_stale_tick(store)
    store.append_audit_event(
        timestamp=_TICK_TS,
        event="epic_merge",
        issue_num=105,
        item_id="105",
        actor_type="human",
        actor_id="tom",
        payload={"trigger": "approval", "actor": "tom"},
    )

    row = next(r for r in list_epics(store) if r.epic_ref == EPIC)

    assert row.counts == {"done": 3, "held": 2, "remediating": 1}


@pytest.mark.parametrize(
    "pr_url",
    [_pr_url("105"), _OTHER_PR, None],
    ids=["the-attested-pr", "a-new-pr", "no-pr-yet"],
)
def test_a_node_reopened_after_an_attested_merge_is_not_retired(
    store: Any, pr_url: str | None
) -> None:
    """An attestation is evidence about a merge at one TIME (#2392 review).

    Node 105's pull request merged in September and kew attested it. The item was
    then reopened for more work, and the journal's newest tick records it ``todo``
    since October. Matching the attestation's pull request cannot answer that on
    its own: the reopened node may still be pointing at the very PR that merged,
    and before its next one is opened it points at none — the "names no second
    pull request" reading that lands every closed Linear issue. With no recency
    test the node was served ``done`` and its epic ``complete``, dropped from the
    frontier and from the needs-you set, while ``kew epic status`` read it live as
    ``todo``.
    """
    _journal_stale_tick(store)
    _seed_ledger(store)
    _journal_reopened_tick(store, "105", pr_url=pr_url)

    row = next(r for r in list_epics(store) if r.epic_ref == EPIC)
    detail = get_epic_detail(store, EPIC, config=_config())

    assert row.counts == {"done": 5, "todo": 1}
    assert row.signal == TerminalSignal.PROGRESSING.value
    assert detail is not None and detail.snapshot is not None
    assert {n.id: n.status for n in detail.snapshot.nodes}["105"] == "todo"


def test_the_cli_and_the_list_agree_on_the_reopened_node(store: Any) -> None:
    """The parity claim on the reopened shape: the tracker has node 105 open with
    no pull request, and the console — reading a journal whose newest tick
    stamped that same reopen — must not count it done (#2392 review)."""
    _journal_stale_tick(store)
    _seed_ledger(store)
    _journal_reopened_tick(store, "105", pr_url=None)

    summary = _cli_summary(store, live="105")
    row = next(r for r in list_epics(store) if r.epic_ref == EPIC)

    assert row.counts == summary.counts
    assert row.signal == summary.signal.value


def test_the_reopened_node_still_reaches_the_needs_you_set(store: Any) -> None:
    """A wrong retirement does not merely mis-colour one node: it empties the
    epic's attention join behind the gate while the set goes on filing the item's
    own work facts, so the legend and the diamonds disagree (#2207)."""
    _journal_stale_tick(store)
    _seed_ledger(store)
    _journal_reopened_tick(store, "105", pr_url=None)
    store.hold_epic_node_item(EPIC, "105", "review_rounds_exhausted")

    answer = needs_you_set(store, _config())
    detail = get_epic_detail(store, EPIC, config=_config())

    assert [e.item_id for e in answer.entries if e.type == "budget_hold"] == ["105"]
    assert detail is not None and detail.snapshot is not None
    diamonds = sum(1 for n in detail.snapshot.nodes if n.attention is not None)
    assert needs_you_by_epic(answer.entries).get(EPIC, 0) == diamonds == 1


def test_an_attestation_written_after_the_reopen_lands_the_node(store: Any) -> None:
    """The recency arm reads the NEWEST attestation, not the first: a node
    reopened in October and merged again afterwards is landed by that second
    merge, exactly as the first one landed its first round."""
    _journal_stale_tick(store)
    _seed_ledger(store)
    _journal_reopened_tick(store, "105", pr_url=_OTHER_PR)
    store.append_audit_event(
        timestamp="2026-10-06T00:00:00+00:00",
        event="epic_merge",
        issue_num=105,
        item_id="105",
        actor_type="human",
        actor_id="tom",
        payload={
            "trigger": "approval",
            "actor": "tom",
            "sha": "def",
            "pr": int(_pr_number_in(_OTHER_PR)),
        },
    )

    row = next(r for r in list_epics(store) if r.epic_ref == EPIC)

    assert row.counts == {"done": 6}
    assert row.signal == TerminalSignal.COMPLETE.value


# --------------------------------------------------------------------------- #
# A settled node leaves every derived reading, the ready frontier included
# --------------------------------------------------------------------------- #

#: The journal shape this section needs: one node the driver left on its ready
#: frontier, and one still mid-flight so the epic is NOT retired — nothing
#: epic-wide can hide the frontier entry.
_READY = "110"
_LIVE = "111"


def _journal_with_a_frontier(store: Any) -> None:
    store.append_epic_tick(
        epic_ref=EPIC,
        tick_ts=_TICK_TS,
        snapshot={
            "schema": 1,
            "epic_ref": EPIC,
            "signal": TerminalSignal.PROGRESSING.value,
            "counts": {"todo": 1, "remediating": 1},
            "frontier": [_READY],
            "nodes": [
                {
                    "id": item,
                    "title": f"issue {item}",
                    "status": status,
                    "status_since": _TICK_TS,
                    "url": None,
                    # No journaled PR, the shape a Linear tracker read leaves:
                    # the attestation below names a pull request this snapshot
                    # does not contradict, so it stands.
                    "pr_url": None,
                    "external": False,
                    "stuck": None,
                }
                for item, status in ((_READY, "todo"), (_LIVE, "remediating"))
            ],
            "edges": [],
        },
        actions={"schema": 1, "dispatched": [], "merged": [], "held": [], "stuck": []},
        snapshot_hash="frontier",
    )


def test_a_settled_node_leaves_the_ready_frontier(store: Any) -> None:
    """Every reading derived from the node states is re-derived, or the repair
    serves a node two ways at once (#2392 review).

    ``frontier`` is "what the driver could dispatch NOW": the epic page rings
    each of its nodes and counts them under *next up*. Counts and signal were
    re-derived from the settled states and this list was passed through
    untouched, so one payload served node 110 ``status=done`` and ringed it as
    ready work in the same snapshot.
    """
    _journal_with_a_frontier(store)
    store.append_audit_event(
        timestamp=_TICK_TS,
        event="epic_merge",
        issue_num=int(_READY),
        item_id=_READY,
        actor_type="human",
        actor_id="tom",
        payload={
            "trigger": "approval",
            "actor": "tom",
            "sha": "abc",
            "pr": _pr_number(_READY),
        },
    )

    detail = get_epic_detail(store, EPIC, config=_config())

    assert detail is not None and detail.snapshot is not None
    assert detail.snapshot.counts == {"done": 1, "remediating": 1}
    assert {n.id: n.status for n in detail.snapshot.nodes}[_READY] == "done"
    assert detail.snapshot.frontier == []


def test_a_frontier_entry_nothing_landed_is_served_verbatim(store: Any) -> None:
    """And the pruning is the LEDGER's, not a blanket one: with no evidence the
    frontier the driver wrote is what a reader gets, ringed node and all."""
    _journal_with_a_frontier(store)

    detail = get_epic_detail(store, EPIC, config=_config())

    assert detail is not None and detail.snapshot is not None
    assert detail.snapshot.frontier == [_READY]


#: The stuck reason the tick journals on a node whose PR merged while its
#: tracker item is still open — ``tick._held_wait_reason``, precedence 3a.
_MERGED_UNCLOSED_STUCK = (
    "merged_unclosed — the merge pass closes the issue on this tick"
)

#: The merge pass's own reason for that same state: it merged the PR, then
#: could not verify the item closed (``merge._handle_merged``, ``merge._do_merge``).
#: The one that persists, because a failed close stays failed while the tick's
#: wait is recomputed from facts every tick (#2392 review).
_MERGE_PASS_STUCK = MERGED_NOT_CLOSED_REASON

#: Both spellings run every assertion below: one state, one answer, whichever
#: word the journal happens to carry.
_UNCLOSED_STUCK_REASONS = (_MERGED_UNCLOSED_STUCK, _MERGE_PASS_STUCK)

_UNCLOSED = "107"


def _journal_merged_unclosed(store: Any, stuck: str) -> None:
    """One node, journaled exactly as the merge pass mid-flight leaves it."""
    store.append_epic_tick(
        epic_ref=EPIC,
        tick_ts=_TICK_TS,
        snapshot={
            "schema": 1,
            "epic_ref": EPIC,
            "signal": TerminalSignal.PROGRESSING.value,
            "counts": {"held": 1},
            "frontier": [],
            "nodes": [
                {
                    "id": _UNCLOSED,
                    "title": f"issue {_UNCLOSED}",
                    "status": "held",
                    "status_since": _TICK_TS,
                    "url": None,
                    "pr_url": _pr_url(_UNCLOSED),
                    "external": False,
                    "stuck": stuck,
                }
            ],
            "edges": [],
        },
        actions={"schema": 1, "dispatched": [], "merged": [], "held": [], "stuck": []},
        snapshot_hash="unclosed",
    )


def _seed_merge_evidence(store: Any, item: str) -> None:
    """Both evidence arms for *item*: kew's attestation and a merged run row."""
    store.append_audit_event(
        timestamp=_TICK_TS,
        event="epic_merge",
        issue_num=int(item),
        item_id=item,
        actor_type="human",
        actor_id="tom",
        payload={
            "trigger": "approval",
            "actor": "tom",
            "sha": "abc",
            "pr": _pr_number(item),
        },
    )
    _merged_run_row(store, item, _pr_url(item))


@pytest.fixture(params=_UNCLOSED_STUCK_REASONS, ids=["tick-wait", "merge-pass"])
def unclosed_stuck(request: pytest.FixtureRequest) -> str:
    """Which of the two reasons the journal carries for this one state."""
    return str(request.param)


@pytest.fixture
def merged_unclosed_epic(store: Any, unclosed_stuck: str) -> Any:
    _journal_merged_unclosed(store, unclosed_stuck)
    _seed_merge_evidence(store, _UNCLOSED)
    return store


def _merged_unclosed_cli_summary(store: Any) -> Any:
    """`kew epic status` on the same node: PR merged, tracker item still OPEN."""
    deps = FakeSource(nodes=[_node(_UNCLOSED)], edges=[])
    facts = FakeFactsReader(
        issue_facts_by_num={
            int(_UNCLOSED): _facts(
                int(_UNCLOSED),
                state="OPEN",
                pr_number=int(_UNCLOSED) + 900,
                pr_url=_pr_url(_UNCLOSED),
                pr_state="MERGED",
            )
        }
    )
    return summarize(
        EPIC,
        build_graph(deps, EPIC),
        config=_config(),
        store=store,
        facts=facts,
        now=NOW,
    )


def test_the_cli_holds_a_merged_pr_whose_item_is_still_open(
    merged_unclosed_epic: Any,
) -> None:
    """Precedence 3a: a merged PR beside an OPEN item is the merge pass
    mid-flight, not a landing. Both halves of the rule are required."""
    summary = _merged_unclosed_cli_summary(merged_unclosed_epic)

    assert summary.counts == {"held": 1}
    assert summary.remaining == 1
    assert summary.signal is not TerminalSignal.COMPLETE


def test_the_list_holds_it_too_rather_than_reading_the_merge_as_landed(
    merged_unclosed_epic: Any,
) -> None:
    """The ledger's merge rows prove the PR half and nothing else, and the
    journal carries the other half NEGATED — the driver read the tracker, found
    the item open behind that very merge, and held. Serve must not promote it:
    a `done`/`complete` here is the CLI and the overlay contradicted (#2392
    review)."""
    row = next(r for r in list_epics(merged_unclosed_epic) if r.epic_ref == EPIC)

    assert row.counts == {"held": 1}
    assert row.signal == TerminalSignal.PROGRESSING.value


def test_the_epic_page_keeps_the_merged_unclosed_reason(
    merged_unclosed_epic: Any,
    unclosed_stuck: str,
) -> None:
    """And it keeps the reason WITH the status: the stuck reason is why a
    person can see the merge pass still owes this node a close."""
    detail = get_epic_detail(merged_unclosed_epic, EPIC)

    assert detail is not None
    assert detail.snapshot is not None
    assert detail.snapshot.counts == {"held": 1}
    assert detail.snapshot.signal == TerminalSignal.PROGRESSING.value
    node = next(n for n in detail.snapshot.nodes if n.id == _UNCLOSED)
    assert node.status == "held"
    assert node.stuck == unclosed_stuck


def test_the_list_and_the_cli_agree_on_the_merged_unclosed_node(
    merged_unclosed_epic: Any,
) -> None:
    summary = _merged_unclosed_cli_summary(merged_unclosed_epic)
    row = next(r for r in list_epics(merged_unclosed_epic) if r.epic_ref == EPIC)

    assert row.counts == summary.counts


def test_the_needs_you_walk_reads_the_held_node_the_routes_do(
    merged_unclosed_epic: Any,
    unclosed_stuck: str,
) -> None:
    """The judgment queue walks the SAME repair this route applies, so a
    promotion here would retire the node out of the queue too — and erase the
    reason that is the only sign the merge pass still owes this item a close."""
    snapshots = reconciled_epic_snapshots(
        merged_unclosed_epic,
        {EPIC: merged_unclosed_epic.latest_epic_tick(EPIC)},
    )

    snapshot = snapshots[EPIC]
    assert snapshot is not None
    assert snapshot["signal"] == TerminalSignal.PROGRESSING.value
    node = next(n for n in snapshot["nodes"] if n["id"] == _UNCLOSED)
    assert node["status"] == "held"
    assert node["stuck"] == unclosed_stuck


def test_the_needs_you_set_reads_the_same_repaired_journal(
    finished_epic: Any,
) -> None:
    """The judgment queue is composed from the SAME journal this route repairs.

    It read the raw journaled signal and the raw per-node rows while the Epics
    list and every epic page read them reconciled, so one payload reported the
    epic `done=6`/`complete` with no node attention while the queue beside it
    listed its nodes as waiting on a person. That is #2207's
    one-page-two-answers failure, and a fix to the epic routes alone
    reintroduces it (#2392 review).
    """
    row = next(r for r in list_epics(finished_epic) if r.epic_ref == EPIC)
    assert row.signal == TerminalSignal.COMPLETE.value

    answer = needs_you_set(finished_epic, _config())

    assert [e for e in answer.entries if e.epic_ref == EPIC] == []
    assert needs_you_by_epic(answer.entries).get(EPIC, 0) == 0


def test_a_mid_flight_epic_still_reaches_the_needs_you_set(store: Any) -> None:
    """The exclusion is the LEDGER's, not a blanket one: an epic with the same
    held rows and no merge evidence keeps every entry it had, or this fix would
    hide the holds somebody has to clear."""
    _journal_stale_tick(store)
    for item in ("103", "104"):
        store.hold_epic_node_item(EPIC, item, "review_rounds_exhausted")

    answer = needs_you_set(store, _config())

    assert [e for e in answer.entries if e.epic_ref == EPIC] != []


@pytest.fixture
def partly_landed_epic(store: Any) -> Any:
    """One node landed, one still held, the rest mid-flight.

    The epic is NOT retired, so neither the archive gate nor the ``complete``
    one takes its stale rows out — the node's own reading has to.
    """
    _journal_stale_tick(store)
    for item in ("103", "104"):
        store.hold_epic_node_item(EPIC, item, "review_rounds_exhausted")
    store.append_audit_event(
        timestamp=_TICK_TS,
        event="epic_merge",
        issue_num=103,
        item_id="103",
        actor_type="human",
        actor_id="tom",
        payload={
            "trigger": "approval",
            "actor": "tom",
            "sha": "abc",
            "pr": _pr_number("103"),
        },
    )
    return store


def test_a_landed_node_s_uncleared_hold_leaves_the_needs_you_set(
    partly_landed_epic: Any,
) -> None:
    """A hold nobody cleared is not a decision once its node has finished.

    Nothing clears a hold after the node it stands on stops being dispatched,
    and the epic-wide exclusions miss this one: the epic is still live, so one
    payload served node 103 as `status=done, stuck=None` while counting it as
    waiting on a person — and `kew epic status` reported no hold for it at all
    (#2392 review). The hold on 104, whose work has NOT landed, stays.
    """
    row = next(r for r in list_epics(partly_landed_epic) if r.epic_ref == EPIC)
    assert row.counts == {"done": 3, "held": 1, "remediating": 2}
    assert row.signal == TerminalSignal.PROGRESSING.value

    answer = needs_you_set(partly_landed_epic, _config())
    holds = [e for e in answer.entries if e.type == "budget_hold"]

    assert [e.item_id for e in holds] == ["104"]


def test_the_landed_node_s_legend_count_matches_its_own_diamonds(
    partly_landed_epic: Any,
) -> None:
    """The legend is the needs-you count and the diamonds are the node join, so
    both have to drop the same entry — a landed node lit `held` under a legend
    that no longer counted it is #2207's one-page-two-answers failure."""
    detail = get_epic_detail(partly_landed_epic, EPIC, config=_config())
    answer = needs_you_set(partly_landed_epic, _config())

    assert detail is not None
    assert detail.snapshot is not None
    landed = next(n for n in detail.snapshot.nodes if n.id == "103")
    assert landed.status == "done"
    assert landed.stuck is None
    assert landed.attention is None
    assert next(n for n in detail.snapshot.nodes if n.id == "104").attention is not None

    diamonds = sum(1 for n in detail.snapshot.nodes if n.attention is not None)
    assert needs_you_by_epic(answer.entries).get(EPIC, 0) == diamonds


def test_a_cancelled_node_s_hold_still_stands(store: Any) -> None:
    """The per-node exclusion is DONE, not every terminal state: the driver's
    own hold precedence exempts a landed node and nothing else
    (``overlay._hold_stands``), so a cancelled node's hold must still reach the
    operator who has to clear it."""
    snapshot = {
        "schema": 1,
        "epic_ref": EPIC,
        "signal": TerminalSignal.PROGRESSING.value,
        "counts": {"cancelled": 1, "remediating": 1},
        "frontier": [],
        "nodes": [
            {
                "id": item,
                "title": f"issue {item}",
                "status": status,
                "status_since": _TICK_TS,
                "url": None,
                "pr_url": _pr_url(item),
                "external": False,
                "stuck": None,
            }
            for item, status in (("108", "cancelled"), ("109", "remediating"))
        ],
        "edges": [],
    }
    store.append_epic_tick(
        epic_ref=EPIC,
        tick_ts=_TICK_TS,
        snapshot=snapshot,
        actions={"schema": 1, "dispatched": [], "merged": [], "held": [], "stuck": []},
        snapshot_hash="cancelled",
    )
    store.hold_epic_node_item(EPIC, "108", "review_rounds_exhausted")

    answer = needs_you_set(store, _config())

    assert [e.item_id for e in answer.entries if e.type == "budget_hold"] == ["108"]
