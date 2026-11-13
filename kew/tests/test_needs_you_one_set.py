"""ONE needs-you set, and the three things that are not in it (#2279).

The design review of 2026-10-03 photographed four disagreements in one console:

* **D6** — the header said ``1 needs you`` over an epic legend saying 1 and
  another saying 2, and the Epics list showed none at all. Four readings of one
  phrase again (#2207 closed the first four), because each surface filtered for
  itself: the header counted ``rows``, a legend counted the diamonds its own
  node join had drawn, and the list counted nothing. So the set is filtered
  ONCE, on the server — :func:`kew.service.needs_you.needs_you_by_epic`, served
  as ``DashboardDTO.needs_you_by_epic`` — and this module holds the header's
  count to the sum of those buckets.
* **D28** — #2272 counted in the header's "2 needs you" as
  ``in flight: run pending`` while kew itself was running it.
* **D8** — #2055 read magenta "Merge decision" with a merge button while review
  round 1 was still reading the pull request.
* **D7** — #2053 was drawn three ways at once (magenta "manual" on Work, violet
  BLOCKED on Monitor, truncated on the DAG) and counted nowhere.

The last three are one rule with three cases: a thing kew is doing right now,
and a failure already drawn as a failure, are not things a person has been
asked for. :data:`kew.service.attention.QUIET_EXCEPTIONS` is where the line is
drawn, and BOTH readers of the work projection read it — the set's own rows and
the node join behind the DAG's diamonds — so a magenta diamond and a judgment
row cannot disagree about whether anybody was asked.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from test_service_dashboard import (
    FakeAuditEvent,
    FakeEpicTick,
    FakeRun,
    FakeStore,
    _config,
)

from kew.epic_archive import EPIC_ARCHIVED_EVENT
from kew.run_store import ADVERSARIAL_REVIEW_RUN_KIND, OrphanPRRecord
from kew.service import dashboard, needs_you
from kew.service.attention import (
    QUIET_EXCEPTIONS,
    compose_node_attention,
    needs_a_person,
    work_projection,
)
from kew.service.work import (
    PENDING_RUN_MAX_AGE_S,
    RemoteObservation,
    TrackerItemFact,
    cached_pull_request,
    collect_work_facts,
    compose_work_view,
    pending_run_max_age_s,
    review_in_flight,
)

NOW = datetime(2026, 10, 3, 12, 0, 0, tzinfo=UTC)
EPIC = "launch-fixes"
OTHER = "go-live"
PR = "https://example.invalid/pr/2059"


@dataclass
class _DriverState:
    """A live heartbeat, so no epic in the fixture reads as stalled."""

    epic_ref: str
    last_tick_started_at: str | None
    last_tick_ts: str | None = None
    last_tick_duration_s: float | None = 1.0
    tick_count: int = 12
    interval_s: float | None = 60.0
    last_signal: str = "progressing"


def _node(node_id: str, status: str, **extra: Any) -> dict[str, Any]:
    node = {
        "id": node_id,
        "title": f"node {node_id}",
        "status": status,
        "status_since": "2026-10-02T09:00:00+00:00",
        "url": None,
        "pr_url": None,
        "external": False,
        "stuck": None,
    }
    node.update(extra)
    return node


def _snapshot(epic_ref: str, *nodes: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": 1,
        "epic_ref": epic_ref,
        "signal": "progressing",
        "counts": {},
        "frontier": [],
        "nodes": list(nodes),
        "edges": [],
    }


def _run(item_id: str, **extra: Any) -> FakeRun:
    defaults: dict[str, Any] = {
        "run_id": f"r-{item_id}",
        "issue_num": int(item_id),
        "item_id": item_id,
        "status": "success",
        "started_at": datetime(2026, 10, 2, 8, 0, 0, tzinfo=UTC),
        "finished_at": datetime(2026, 10, 2, 9, 0, 0, tzinfo=UTC),
    }
    defaults.update(extra)
    return FakeRun(**defaults)


def _review_run(item_id: str, *, started_at: datetime | None = None) -> FakeRun:
    """A governed review round kew is reading the artifact with RIGHT NOW.

    A recorded ``review_rounds`` row is a FINISHED round, so a pending run of a
    review kind is the only fact that says round N is being read — see
    :func:`kew.service.work.review_in_flight`. Started after the
    implementation run, which is what makes it the newest row on the item, and
    two hours before ``NOW``: the predicate has an age in it, so a round that
    is still being read has to be a YOUNG pending row and not merely a pending
    one. ``started_at`` is for the test that makes it old.
    """
    return FakeRun(
        run_id=f"rev-{item_id}",
        issue_num=int(item_id),
        item_id=item_id,
        status="pending",
        run_kind=ADVERSARIAL_REVIEW_RUN_KIND,
        started_at=started_at or (NOW - timedelta(hours=2)),
    )


def _store(
    *,
    nodes: tuple[dict[str, Any], ...] = (),
    other_nodes: tuple[dict[str, Any], ...] = (),
    syncable: tuple[FakeRun, ...] = (),
    runs: tuple[FakeRun, ...] = (),
    orphans: bool = False,
    approval: bool = False,
    holds: tuple[Any, ...] = (),
    rounds: tuple[Any, ...] = (),
) -> FakeStore:
    """Two live epics, plus the waits that belong to no epic at all.

    ``holds`` are the driver's open node holds and ``rounds`` the recorded
    governed review rounds — the two facts a between-rounds item is read from
    (#2328, in ``test_needs_you_between_rounds.py``).
    """
    return FakeStore(
        audit_events=(
            [
                FakeAuditEvent(
                    event="approval_hold",
                    issue_num=2090,
                    payload={"estimate_usd": 12.5, "basis": "over the per-issue cap"},
                    id=1,
                    timestamp="2026-10-03T10:00:00+00:00",
                )
            ]
            if approval
            else []
        ),
        orphan_prs=(
            [
                OrphanPRRecord(
                    77,
                    "https://example.invalid/pr/77",
                    "agent/77",
                    "branch has no run",
                    datetime(2026, 10, 2, 9, 0, 0, tzinfo=UTC),
                    None,
                )
            ]
            if orphans
            else []
        ),
        syncable=list(syncable),
        all_runs=list(runs),
        epic_holds=list(holds),
        review_rounds=list(rounds),
        epic_refs=[EPIC, OTHER],
        driver_states={
            ref: _DriverState(
                epic_ref=ref,
                last_tick_started_at=NOW.isoformat(),
                last_tick_ts=NOW.isoformat(),
            )
            for ref in (EPIC, OTHER)
        },
        epic_ticks={
            EPIC: FakeEpicTick(snapshot=_snapshot(EPIC, *nodes)),
            OTHER: FakeEpicTick(snapshot=_snapshot(OTHER, *other_nodes)),
        },
    )


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the gh-backed ready queue so nothing here shells out."""
    monkeypatch.setattr(dashboard, "_ready_issues", lambda config: [])
    dashboard._ready_cache.clear()
    dashboard._candidates_cache.clear()
    dashboard._read_failures.clear()


def _join(
    store: FakeStore,
    epic_ref: str,
    nodes: tuple[dict[str, Any], ...],
    *,
    config: Any = None,
) -> Any:
    """The node join the epic's own legend and DAG diamonds are drawn from."""
    return compose_node_attention(
        store,
        config or _config(),
        [str(n["id"]) for n in nodes],
        nodes=nodes,
        epic_ref=epic_ref,
        signal="progressing",
        now=NOW,
    )


def _observed_item(state: str = "open") -> TrackerItemFact:
    """A tracker item somebody actually read. Without one, every verdict that
    would recommend a dispatch degrades to ``remote_unavailable`` first
    (``_source_blocker``), which is a different — and perfectly real —
    exception, and not the one these tests are about."""
    return TrackerItemFact(
        observation=RemoteObservation(
            dimension="tracker_item",
            availability="available",
            provenance="observed",
            observed_at=NOW,
        ),
        state=state,
    )


def _facts(store: FakeStore, item_id: str, **overrides: Any) -> Any:
    """One item's facts, with its own run's cached pull request bound in.

    The projection reads the PR off the run row (``cached_pull_request``), so a
    fact set composed without it has an UNOBSERVED artifact and every judgment
    verdict degrades to "go and look" — honest, and not the state under test.
    """
    pr = next(
        (cached_pull_request(r) for r in store.list_by_item(item_id) if r.pr_url), None
    )
    facts = collect_work_facts(
        store, item_id, pull_request=pr, tracker_item=_observed_item(), now=NOW
    )
    return replace(facts, **overrides) if overrides else facts


def _view(store: FakeStore, item_id: str) -> Any:
    """One item's canonical work view, composed the way every surface reads it.

    Through ``work_projection`` rather than ``compose_work_projection``: that
    is the snapshot-bound reader the needs-you set and the node join both go
    through, and it is what binds the run's cached pull request into the view.
    """
    projection = work_projection(store, _config(), item_ids=[item_id], now=NOW)
    return next(v for v in projection.views if v.identity.item_id == item_id)


# --------------------------------------------------------------------------- #
# D6 — one set, filtered once
# --------------------------------------------------------------------------- #


def test_the_header_count_is_the_sum_of_the_per_epic_counts() -> None:
    """The acceptance: on one seeded store, the three readings are arithmetic.

    The sum is over the buckets PLUS the entries that name no epic, because an
    open spend approval on a loose issue and an orphan PR belong to the fleet
    and not to a drain — and a map that pretended otherwise would file them
    under whichever epic happened to be walked first.
    """
    nodes = (
        _node("2001", "in_review", pr_url=PR),
        _node("2002", "awaiting_continuation"),
    )
    other = (_node("2003", "awaiting_publication"),)
    store = _store(
        nodes=nodes,
        other_nodes=other,
        syncable=(_run("2001", pr_state="open", pr_url=PR),),
        orphans=True,
        approval=True,
    )

    payload = dashboard.assemble(store, _config(), now=NOW)

    assert payload.unavailable == []
    by_epic = payload.needs_you_by_epic
    fleet = [row for row in payload.rows if not row.epic_ref]
    assert len(payload.rows) == sum(by_epic.values()) + len(fleet)
    # And the buckets are the epics' own slices, not a reshuffle of them.
    assert by_epic == {EPIC: 2, OTHER: 1}
    for ref, count in by_epic.items():
        assert len(needs_you.epic_entries(payload.rows, ref)) == count
    # The two fleet-wide entries are the approval and the orphan PR, and
    # nothing else loses its epic on the way through.
    assert sorted(row.type for row in fleet) == ["approval_hold", "orphan_pr"]


def test_each_epic_legend_counts_what_its_bucket_counts() -> None:
    """The legend's diamonds and the epic's bucket are one number (#2279/D6).

    Before this, the DAG's diamonds came from the node join and the bucket did
    not exist, so the only way to compare them was to read two screens.
    """
    nodes = (
        _node("2001", "in_review", pr_url=PR),
        _node("2002", "awaiting_continuation"),
        _node("2010", "in_progress"),
        _node("2011", "done"),
    )
    store = _store(nodes=nodes, syncable=(_run("2001", pr_state="open", pr_url=PR),))

    payload = dashboard.assemble(store, _config(), now=NOW)
    join = _join(store, EPIC, nodes)

    assert join.unavailable == []
    assert len(join.by_node) == payload.needs_you_by_epic[EPIC] == 2
    assert set(join.by_node) == {"2001", "2002"}


def test_a_work_exception_reaches_the_set_carrying_its_node_s_epic() -> None:
    """The entry an epic legend could not show (#2279/D6).

    An approval hold is keyed by issue number and the work projection by item;
    neither knows an epic, so a merge decision on an epic node used to reach
    the set with ``epic_ref`` empty — counted by the header, invisible to every
    filter. The epic walk places it, and ``_collapse``'s fold is not what does
    it: this node carries no journaled wait of its own to inherit one from.
    """
    nodes = (_node("2001", "in_progress", pr_url=PR),)
    store = _store(
        nodes=nodes,
        syncable=(_run("2001", pr_state="open", pr_url=PR),),
        approval=True,
    )

    composed = needs_you.needs_you_set(store, _config(), now=NOW)

    merge = next(e for e in composed.entries if e.item_id == "2001")
    assert (merge.type, merge.epic_ref) == ("pr_review", EPIC)
    # The approval names an issue nobody's epic holds, so it stays fleet-wide
    # rather than being filed under the epic that was walked first.
    approval = next(e for e in composed.entries if e.type == "approval_hold")
    assert approval.epic_ref is None


def test_an_unreadable_walk_leaves_every_entry_where_its_source_put_it() -> None:
    """The placement is fail-soft, and in the direction that keeps the count.

    A failed epic walk cannot say which epic an item belongs to. Serving the
    entry anyway, unplaced, keeps the header's count honest and leaves the
    legend to confess through ``unavailable``; dropping it would make a read
    that did not happen look like a world with less in it.
    """

    class Broken(FakeStore):
        def latest_epic_tick(self, epic_ref: str) -> Any:
            raise RuntimeError("ticks unreadable")

    store = Broken(
        syncable=[_run("2001", pr_state="open", pr_url=PR)],
        epic_refs=[EPIC],
        driver_states={
            EPIC: _DriverState(
                epic_ref=EPIC,
                last_tick_started_at=NOW.isoformat(),
                last_tick_ts=NOW.isoformat(),
            )
        },
    )

    composed = needs_you.needs_you_set(store, _config(), now=NOW)

    assert "node_epics" in composed.unavailable
    merge = next(e for e in composed.entries if e.item_id == "2001")
    assert merge.epic_ref is None


# --------------------------------------------------------------------------- #
# D28 — a run kew is executing
# --------------------------------------------------------------------------- #


def test_an_in_flight_run_is_never_a_needs_you() -> None:
    """D28, reproduced: #2272 counted as "in flight: run pending".

    Both readers of the projection are asserted, because the diamond and the
    row are what disagreed before #2207 and the point of one table is that they
    cannot: the node shows as working, with no diamond and no judgment row.
    """
    nodes = (_node("2272", "in_progress"),)
    # The item reaches the projection through its open pull request (the work
    # projection composes a view per item with a live delivery artifact), and
    # the newest row on it is the run kew is executing right now — a ci-fix, a
    # redispatch, the second attempt. That is the state #2272 was counted in.
    # Started two hours ago, because every pending row now has an age bound on
    # it (``PENDING_RUN_MAX_AGE_S``): a run kew is executing is a YOUNG pending
    # row, and an old one is a row a dead host left behind
    # (``test_an_abandoned_run_is_on_the_needs_you_surfaces_again``).
    store = _store(
        nodes=nodes,
        syncable=(_run("2272", pr_state="open", pr_url=PR),),
        runs=(
            _run(
                "2272",
                run_id="r-2272-b",
                status="pending",
                branch="agent/2272",
                pr_url=None,
                started_at=NOW - timedelta(hours=2),
                finished_at=None,
            ),
        ),
    )

    view = _view(store, "2272")
    composed = needs_you.needs_you_set(store, _config(), now=NOW)
    join = _join(store, EPIC, nodes)

    assert (view.execution.state, view.exception.state) == ("in_flight", "in_flight")
    assert view.exception.state in QUIET_EXCEPTIONS
    assert [e for e in composed.entries if e.item_id == "2272"] == []
    assert join.by_node == {}


# --------------------------------------------------------------------------- #
# D8 — a review round kew is running
# --------------------------------------------------------------------------- #


def test_a_running_review_round_is_not_a_merge_decision() -> None:
    """D8, reproduced: #2055 read "Merge decision" mid-round.

    The judgment is what the Work screen's needs-you band and the pipeline's
    merge cell read — the band raises on ``awaiting_human_decision`` whatever
    the exception says — so the fix has to be in that field and not only in the
    exception.
    """
    # `adversarial_review`, which is what the driver journals while its own
    # reviewer runs (``DriverStatus`` in graph/driver/overlay.py) — and which
    # `_WAIT_STATUSES` deliberately leaves out, so the journal already agrees
    # that nobody is waited on. `in_review` is the status AFTER the verdict,
    # and the test below is that one.
    nodes = (_node("2055", "adversarial_review", pr_url=PR),)
    store = _store(
        nodes=nodes,
        syncable=(_run("2055", pr_state="open", pr_url=PR),),
        runs=(_review_run("2055"),),
    )

    view = _view(store, "2055")
    composed = needs_you.needs_you_set(store, _config(), now=NOW)
    join = _join(store, EPIC, nodes)

    assert view.exception.state == "review_in_flight"
    assert view.safest_action.action == "wait"
    # It used to read `in_flight` / "run pending" — the same words a run kew is
    # implementing with gets, which is the wrong word for a review AND the row
    # D28 was counted as.
    assert not needs_a_person(view)
    assert [e for e in composed.entries if e.item_id == "2055"] == []
    assert join.by_node == {}


def test_a_published_artifact_under_review_is_not_awaiting_a_person() -> None:
    """The judgment half of D8, where the artifact is published either way.

    The Work screen's needs-you band raises on
    ``judgment.state == "awaiting_human_decision"`` WHATEVER the exception says
    (``needsYouBand`` in frontend/src/lib/work/evidence.ts), and so does the
    pipeline's merge cell — so the fix has to reach that field and not only the
    exception. Composed from facts rather than from a store because this is
    about one field's rule and not about which run row happens to be newest.
    """
    store = _store(syncable=(_run("2055", pr_state="open", pr_url=PR),))
    facts = _facts(store, "2055")
    assert facts.pull_request.state == "open"

    reviewing = replace(facts, runs=(*facts.runs, _review_run("2055")))
    view = compose_work_view(reviewing, composed_at=NOW)

    assert view.artifact.state == "published"
    assert view.judgment.state == "under_review"
    assert view.outcome.state == "in_progress"
    assert view.exception.state == "review_in_flight"
    assert not needs_a_person(view)


def test_the_verdict_landing_is_what_raises_the_needs_you() -> None:
    """The same store with the round finished: the decision is a person's.

    Without this half the fix could be "never a merge decision", which is the
    same defect pointing the other way — a pull request nobody is told to look
    at.
    """
    nodes = (_node("2055", "in_review", pr_url=PR),)
    store = _store(
        nodes=nodes,
        syncable=(_run("2055", pr_state="open", pr_url=PR),),
    )

    view = _view(store, "2055")
    composed = needs_you.needs_you_set(store, _config(), now=NOW)
    join = _join(store, EPIC, nodes)

    assert view.judgment.state == "awaiting_human_decision"
    assert view.exception.state == "awaiting_human_decision"
    merge = next(e for e in composed.entries if e.item_id == "2055")
    assert (merge.type, merge.attention_kind) == ("pr_review", "merge_decision")
    assert join.by_node["2055"].kind == "merge_decision"


def test_a_review_round_that_will_never_report_stops_hiding_the_decision() -> None:
    """The bound on D8: a pending review row is in flight only while it is young.

    A pending row is the ONLY fact that says "round N is being read right now",
    and nothing reliably finalizes one whose run died with its host: the
    store's ``cleanup_stale_pending`` backstop waits 24h AND only flips a row
    whose host it can prove dead, so a row with an unknown owner stays pending
    for good. Unbounded, that one row held the artifact in ``under_review``
    indefinitely — and the Work screen's needs-you band raises on
    ``judgment.state`` whatever the exception says, so the merge decision was
    off that surface for a round nobody was running.

    So the predicate has an age in it
    (:data:`kew.service.work.PENDING_RUN_MAX_AGE_S`), measured against the
    snapshot's own moment (``WorkFacts.as_of``, so the view stays a function of
    its facts): a round older than it is read as the dead row it is, and the
    decision comes back to the person it was always waiting on.
    """
    store = _store(syncable=(_run("2055", pr_state="open", pr_url=PR),))
    facts = _facts(store, "2055")
    stale = _review_run(
        "2055", started_at=NOW - timedelta(seconds=PENDING_RUN_MAX_AGE_S + 1)
    )

    abandoned = compose_work_view(
        replace(facts, runs=(*facts.runs, stale)), composed_at=NOW
    )

    assert abandoned.artifact.state == "published"
    assert abandoned.judgment.state == "awaiting_human_decision"
    # The swimlane reads the same predicate for its review cell, so it cannot
    # still be drawing round 1 as `working` beside a merge cell that has gone
    # back to needs-you (#2279/D8 is one reading, bound included).
    assert not review_in_flight((stale,), now=NOW)
    # The other half, so the bound cannot be "never in flight": the same row
    # inside the window is still a round being read.
    fresh = replace(facts, runs=(*facts.runs, _review_run("2055")))
    assert compose_work_view(fresh, composed_at=NOW).judgment.state == "under_review"
    assert review_in_flight((_review_run("2055"),), now=NOW)


def test_an_abandoned_round_is_on_the_needs_you_surfaces_again() -> None:
    """The bound's whole purpose, read off the surfaces it was added for.

    The test above composes the facts by hand, which puts the implementation
    run first and leaves the judgment door open. Production does not: the
    store's own order is newest-first, so the pending review row IS the latest
    run — the artifact reads ``absent`` off a row that never had a pull request
    and the judgment is never consulted at all. The bound then only reached
    ``judgment.state``, and the exception fell through to the plain
    ``in_flight``, which is QUIET — so the dead row went on hiding the item
    from the header's count, the epic legend and the DAG's diamonds exactly as
    it had before the bound existed (#2279, round 2).

    ``review_abandoned`` is the answer, and it is not quiet: a row nothing will
    finalize is not a run kew is executing, and the next move — another round,
    or the decision as the artifact stands — is a person's.
    """
    nodes = (_node("2055", "adversarial_review", pr_url=PR),)
    stale = _review_run(
        "2055", started_at=NOW - timedelta(seconds=PENDING_RUN_MAX_AGE_S + 1)
    )
    store = _store(
        nodes=nodes,
        syncable=(_run("2055", pr_state="open", pr_url=PR),),
        runs=(stale,),
    )

    view = _view(store, "2055")
    composed = needs_you.needs_you_set(store, _config(), now=NOW)
    join = _join(store, EPIC, nodes)

    # The row that makes the execution read in_flight is the dead one, which is
    # exactly why the word cannot come from the row's status alone.
    assert view.execution.state == "in_flight"
    assert view.exception.state == "review_abandoned"
    assert view.exception.state not in QUIET_EXCEPTIONS
    assert needs_a_person(view)
    assert view.safest_action.action == "inspect"
    entry = next(e for e in composed.entries if e.item_id == "2055")
    assert entry.type == "work_exception"
    assert entry.epic_ref == EPIC
    assert needs_you.needs_you_by_epic(composed.entries)[EPIC] >= 1
    assert join.by_node["2055"].kind == "work_exception"
    # And the same store inside the window is still kew's own move, so this is
    # the bound talking and not a new reading of a pending review row.
    fresh = _store(
        nodes=nodes,
        syncable=(_run("2055", pr_state="open", pr_url=PR),),
        runs=(_review_run("2055"),),
    )
    assert _view(fresh, "2055").exception.state == "review_in_flight"


def test_an_age_that_cannot_be_read_leaves_the_round_in_flight() -> None:
    """A row with no readable start is not evidence of a stale one.

    The bound exists so a dead row cannot silence a merge decision; a bound
    that guessed the other way — reading an unreadable age as old — would
    invent the decision instead, which is the same defect wearing the other
    hat.
    """
    store = _store(syncable=(_run("2055", pr_state="open", pr_url=PR),))
    facts = _facts(store, "2055")
    undated = replace(_review_run("2055"), started_at=None)

    assert review_in_flight((undated,), now=NOW)
    reviewing = replace(facts, runs=(*facts.runs, undated))
    view = compose_work_view(reviewing, composed_at=NOW)
    assert view.judgment.state == "under_review"


def test_the_view_measures_the_round_against_its_own_snapshot() -> None:
    """``as_of`` and not the wall clock, which is what keeps the view pure.

    ``compose_work_view`` promises "same facts in, same view out", and a rule
    with an age in it breaks that promise the moment it reads the clock
    itself: one snapshot would compose two different judgments depending on
    when somebody asked. The moment travels on the facts instead.
    """
    store = _store(syncable=(_run("2055", pr_state="open", pr_url=PR),))
    facts = _facts(store, "2055")
    assert facts.as_of == NOW

    reviewed = replace(facts, runs=(*facts.runs, _review_run("2055")))
    later = replace(reviewed, as_of=NOW + timedelta(days=1))

    assert compose_work_view(reviewed, composed_at=NOW).judgment.state == "under_review"
    assert (
        compose_work_view(later, composed_at=NOW).judgment.state
        == "awaiting_human_decision"
    )


def test_a_remediation_in_flight_is_kew_s_move_not_the_reviewer_s() -> None:
    """The round-trip leg counts as the cycle too.

    ``REVIEW_RUN_KINDS`` holds the remediation run as well as the reviewer's,
    and while kew is fixing what a reviewer flagged the next move is kew's —
    which is why the check runs AHEAD of ``changes_requested`` rather than
    beside it.
    """
    from kew.run_store import REVIEW_REMEDIATION_RUN_KIND

    remediation = FakeRun(
        run_id="rem-2055",
        issue_num=2055,
        item_id="2055",
        status="pending",
        run_kind=REVIEW_REMEDIATION_RUN_KIND,
        started_at=NOW - timedelta(hours=2),
    )
    store = _store(syncable=(_run("2055", pr_state="open", pr_url=PR),))
    facts = _facts(store, "2055")
    # A reviewer who asked for changes AND a remediation in flight: the reading
    # that must NOT win here is `changes_requested`, whose action is a person's.
    flagged = replace(
        facts,
        runs=(*facts.runs, remediation),
        pull_request=replace(facts.pull_request, review_decision="changes_requested"),
    )

    view = compose_work_view(flagged, composed_at=NOW)

    assert view.judgment.state == "under_review"
    assert view.exception.state == "review_in_flight"
    assert not needs_a_person(view)


# --------------------------------------------------------------------------- #
# The bound is the OPERATOR's, and it is on every pending row
# --------------------------------------------------------------------------- #


def _dead_run(item_id: str, **extra: Any) -> FakeRun:
    """A pending IMPLEMENTATION row no host is behind any more.

    Started a day before ``NOW``, which is past any bound this host could set,
    and carrying no pull request — the shape a run leaves when its host dies
    mid-attempt. ``cleanup_stale_pending`` will not finalize it: that backstop
    waits 24h AND only flips a row whose host it can prove dead, so a row with
    an unknown owner stays pending for good.
    """
    defaults: dict[str, Any] = {
        "run_id": f"dead-{item_id}",
        "issue_num": int(item_id),
        "item_id": item_id,
        "status": "pending",
        "pr_url": None,
        "started_at": NOW - timedelta(days=1),
        "finished_at": None,
    }
    defaults.update(extra)
    return FakeRun(**defaults)


def test_an_abandoned_run_is_on_the_needs_you_surfaces_again() -> None:
    """The bound is on EVERY pending row, not only a review one.

    ``review_abandoned`` closed the hole for rows of a review kind. The hole
    itself was never review's: ``in_flight`` is quiet for every pending row, so
    an implementation attempt whose host died left the identical row and took
    the item off the header's count, the epic legend and the DAG's diamonds for
    good — the store's own backstop will not finalize a row whose owner it
    cannot prove dead.

    So the bound is read off the row and only the WORDING turns on the kind:
    ``run_abandoned``, whose next move is a person looking at what happened to
    the host, not a second attempt the row's own reservation would refuse.
    """
    nodes = (_node("2272", "in_progress"),)
    store = _store(
        nodes=nodes,
        syncable=(_run("2272", pr_state="open", pr_url=PR),),
        runs=(_dead_run("2272"),),
    )

    view = _view(store, "2272")
    composed = needs_you.needs_you_set(store, _config(), now=NOW)
    join = _join(store, EPIC, nodes)

    assert view.execution.state == "in_flight"
    assert view.exception.state == "run_abandoned"
    assert view.exception.state not in QUIET_EXCEPTIONS
    assert needs_a_person(view)
    assert view.safest_action.action == "inspect"
    entry = next(e for e in composed.entries if e.item_id == "2272")
    assert entry.type == "work_exception"
    assert entry.epic_ref == EPIC
    assert needs_you.needs_you_by_epic(composed.entries)[EPIC] >= 1
    assert join.by_node["2272"].kind == "work_exception"


def _budget(minutes: int) -> Any:
    """``_config()`` with an operator-set agent budget."""
    from kew.config import AgentConfig

    return _config().model_copy(update={"agent": AgentConfig(timeout_minutes=minutes)})


def test_the_bound_follows_the_operator_s_agent_budget() -> None:
    """A raised ``agent.timeout_minutes`` raises the bound with it.

    Four hours is justified as twice the DEFAULT budget (120 minutes), and read
    as a bare constant that justification expires the moment an operator raises
    the budget: a six-hour agent would have its LIVE review round called
    abandoned at four hours, which is #2279/D8 pointing the other way — a merge
    decision handed to a person while kew is still reading the artifact.
    """
    nodes = (_node("2055", "adversarial_review", pr_url=PR),)
    running = _review_run("2055", started_at=NOW - timedelta(hours=5))
    store = _store(
        nodes=nodes,
        syncable=(_run("2055", pr_state="open", pr_url=PR),),
        runs=(running,),
    )

    # Under the default budget the five-hour row is past the floor and dead...
    assert pending_run_max_age_s(_config()) == PENDING_RUN_MAX_AGE_S
    projection = work_projection(store, _config(), item_ids=["2055"], now=NOW)
    assert projection.views[0].exception.state == "review_abandoned"

    # ...and under a six-hour agent the same row is a round still being read.
    raised = _budget(360)
    assert pending_run_max_age_s(raised) == 12 * 60 * 60
    live = work_projection(store, raised, item_ids=["2055"], now=NOW)
    assert live.views[0].exception.state == "review_in_flight"
    assert not needs_a_person(live.views[0])
    assert needs_you.needs_you_set(store, raised, now=NOW).entries == []
    assert _join(store, EPIC, nodes, config=raised).by_node == {}


def test_a_lowered_budget_never_shortens_the_bound() -> None:
    """The constant is a FLOOR. A host that lowers ``timeout_minutes`` has not
    asked for a twitchier definition of "dead row", and a bound of twenty
    minutes would read every ordinary pending row as abandoned."""
    assert pending_run_max_age_s(_budget(10)) == PENDING_RUN_MAX_AGE_S
    # No config at all is the same floor — a caller composing facts by hand.
    assert pending_run_max_age_s(None) == PENDING_RUN_MAX_AGE_S


# --------------------------------------------------------------------------- #
# One epic's legend and its own DAG
# --------------------------------------------------------------------------- #


def test_a_boundary_copy_raises_no_diamond_on_the_epic_that_borrowed_it() -> None:
    """An external node is the OWNER's to ask about, on both surfaces.

    ``build_graph`` copies a resolved out-of-epic dependency into every epic
    that depends on it, flagged ``external``. Every source that starts from the
    epic already filed the node under its owner — the journaled waits, the epic
    holds, and the set's own ``_epic_by_item``, which is what the legend counts.
    The two sources that start from the ITEM did not, because neither has ever
    heard of an epic: the work projection and the approval holds are keyed by
    item id, so a merge decision on somebody else's node drew a magenta diamond
    on the borrowing epic's DAG while that epic's legend read 0.
    """
    owner_nodes = (_node("2055", "in_review", pr_url=PR),)
    borrowed = (_node("2055", "in_review", pr_url=PR, external=True),)
    store = _store(
        nodes=owner_nodes,
        other_nodes=borrowed,
        syncable=(_run("2055", pr_state="open", pr_url=PR),),
    )

    composed = needs_you.needs_you_set(store, _config(), now=NOW)
    counts = needs_you.needs_you_by_epic(composed.entries)

    # The row is filed under the epic that owns the node, and only there.
    assert counts.get(EPIC) == 1
    assert counts.get(OTHER) is None
    # And the diamonds match, legend for legend: the owner draws one, the epic
    # holding the boundary copy draws none.
    assert set(_join(store, EPIC, owner_nodes).by_node) == {"2055"}
    assert _join(store, OTHER, borrowed).by_node == {}


def test_a_retired_epic_s_legend_and_dag_both_read_nobody() -> None:
    """An archived epic asks nobody for anything — on every one of its surfaces.

    The legend counts the epic's bucket of the set, and no entry can be filed
    under an epic ``_epic_roster`` calls retired, so the bucket is empty. The
    node join used to keep the item-level readings anyway (the work projection
    and the approval holds reach ``rows`` naming no epic, so the header counts
    them either way) — which left the legend printing a confident ``0 needs
    you`` over a DAG still drawing the magenta diamond. One page, two answers.

    The item-level wait is still real and the header still counts it. What it no
    longer does is name an epic nobody intends to resume.
    """
    nodes = (_node("2055", "in_review", pr_url=PR),)
    store = _store(nodes=nodes, syncable=(_run("2055", pr_state="open", pr_url=PR),))
    store._audit = [
        FakeAuditEvent(
            event=EPIC_ARCHIVED_EVENT,
            issue_num=None,
            payload={"epic_ref": EPIC, "reason": "abandoned"},
            id=9,
            timestamp="2026-10-03T09:00:00+00:00",
        )
    ]

    composed = needs_you.needs_you_set(store, _config(), now=NOW)
    join = _join(store, EPIC, nodes)

    assert needs_you.epic_entries(composed.entries, EPIC) == []
    assert needs_you.needs_you_by_epic(composed.entries).get(EPIC, 0) == 0
    assert join.by_node == {}
    assert join.unavailable == []
    # The invariant both halves exist to keep: a node carries attention exactly
    # when the set files a row for THIS epic about it.
    assert len(join.by_node) == len(needs_you.epic_entries(composed.entries, EPIC))
    # The wait itself did not evaporate — it is counted where it actually is.
    assert "2055" in {e.item_id for e in composed.entries}


# --------------------------------------------------------------------------- #
# D7 — a failure, read as a failure
# --------------------------------------------------------------------------- #


def test_a_terminal_failure_is_not_a_needs_you_on_any_surface() -> None:
    """D7's chosen reading: the failure keeps its own mark, not the magenta.

    Four surfaces already said so — ``kew.waits``'s ``failed-terminal`` bucket
    names no attention kind, ``graph/driver/attention.py`` drops terminal nodes
    outright, the epic legend draws no diamond for one, and the DAG paints the
    node's own ``failed``/``blocked`` mark. The set's own row and Work's magenta
    "manual" were the two that disagreed, and #2053 was drawn three ways at
    once because of it. Both are quiet now, and the word Work prints is the
    failure word (``EXCEPTION_STATE_WORD`` in frontend/src/lib/work/list.ts,
    anchored by tests/test_factory_exception_parity.py).
    """
    nodes = (_node("2053", "blocked", stuck="needs a design decision"),)
    store = _store(
        nodes=nodes,
        syncable=(_run("2053", status="failure", branch="agent/2053"),),
    )
    view = compose_work_view(_facts(store, "2053"), composed_at=NOW)

    assert (view.execution.state, view.exception.state) == (
        "terminal",
        "terminal_failure",
    )
    # The one gate both readers of the projection go through, so this is the
    # whole claim: no judgment row, and no diamond.
    assert not needs_a_person(view)
    # And the node is drawn by its own journaled status, which is what the DAG
    # and Monitor already read: blocked, in the blocked treatment, with the
    # driver's recorded reason — and no diamond over it.
    join = _join(store, EPIC, nodes)
    assert join.unavailable == []
    assert join.by_node == {}


def test_a_read_failure_is_not_a_needs_you_on_any_surface() -> None:
    """A merged item whose tracker could not be read is kew's outage (#2893).

    Live #2870 sat in needs-you as "remote unavailable: the source item's
    current state could not be read". Nobody can act on that: the next poll
    reads the tracker again on its own. So the set serves no row for it and
    the node join draws no diamond, while the view itself still says it could
    not read — which is what the item's own detail shows.
    """
    pr = "https://example.invalid/pr/2870"
    nodes = (_node("2870", "in_review", pr_url=pr),)
    store = _store(
        nodes=nodes,
        syncable=(_run("2870", pr_state="merged", pr_url=pr),),
    )

    view = _view(store, "2870")
    composed = needs_you.needs_you_set(store, _config(), now=NOW)
    join = _join(store, EPIC, nodes)

    assert (view.exception.state, view.safest_action.action) == (
        "remote_unavailable",
        "observe_remote",
    )
    assert [e.reason for e in composed.entries if e.item_id == "2870"] == []
    assert join.by_node == {}


def test_the_quiet_exceptions_are_the_only_ones_both_readers_drop() -> None:
    """One table, two readers — the invariant the four-surface defect needs.

    ``needs_a_person`` is the whole of the rule, and it is read by the set's
    rows and by the node join. A reader growing a filter of its own is how the
    header came to count a diamond no legend drew, so the table is asserted
    here rather than each caller's behaviour being asserted twice.
    """
    assert set(QUIET_EXCEPTIONS) == {
        "none",
        "in_flight",
        "review_in_flight",
        # The same cycle BETWEEN two of its rounds (#2328) — see
        # ``test_needs_you_between_rounds.py``, which owns their behaviour. They
        # are listed here because this assertion is the table itself: a reader
        # growing a filter of its own is what it exists to catch.
        "review_remediation_due",
        "review_round_due",
        "terminal_failure",
        # A read failure is not a person's move (#2893) — see
        # ``test_a_read_failure_is_not_a_needs_you_on_any_surface``.
        "remote_unavailable",
    }
