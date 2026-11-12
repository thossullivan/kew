"""Unit tests for the artifact-freshness composition (#2303, design-c3).

AC1 of the node: one test per ``review_state`` and one per ``base.state``. The
composition is read-only and makes no GitHub call, so everything here runs off a
fake store — plus two end-to-end cases over a REAL ``SqliteRunStore``, because
the row set is produced by a ``json_each`` query and a SQL-side filter nobody can
assert from a fake.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from kew.config import DispatchConfig
from kew.run_store import (
    ADVERSARIAL_REVIEW_RUN_KIND,
    MAX_NODE_PR_FACTS_LIMIT,
    NodePrFacts,
    SqliteRunStore,
)
from kew.service.attention import (
    _MERGE_STATE_MAX_PAGES,
    _MERGE_STATE_PAGE,
    _cached_artifact,
    _journaled_merge_states,
    _JournaledMergeStates,
)
from kew.service.freshness import (
    MAX_LIMIT,
    NEEDS_A_PERSON_BASE_STATES,
    base_state,
    compose_freshness,
    pr_number,
)

NOW = datetime(2026, 10, 6, 12, 0, 0, tzinfo=UTC)
#: When the driver last READ a merge state — not the tick that reported it.
READ_AT = "2026-10-06T11:59:00+00:00"
EPIC = "design-c3"
PR_URL = "https://github.com/o/r/pull/2303"


@dataclass
class _Round:
    item_id: str
    round: int
    verdict: str
    head_sha: str
    recorded_at: datetime
    epic_ref: str = EPIC


@dataclass
class _Run:
    item_id: str
    run_kind: str = ADVERSARIAL_REVIEW_RUN_KIND
    status: str = "pending"
    started_at: datetime | None = None
    finished_at: datetime | None = None


class _FakeStore:
    def __init__(
        self,
        facts: list[NodePrFacts],
        rounds: list[_Round] | None = None,
        pending: list[_Run] | None = None,
    ) -> None:
        self._facts = facts
        self._rounds = rounds or []
        self._pending = pending or []
        self.fact_calls: list[dict[str, Any]] = []

    def latest_node_pr_facts(
        self,
        *,
        limit: int = 50,
        after: tuple[str, str] | None = None,
        epic_ref: str | None = None,
    ) -> list[NodePrFacts]:
        self.fact_calls.append({"limit": limit, "after": after, "epic_ref": epic_ref})
        rows = [
            f
            for f in self._facts
            if (after is None or (f.epic_ref, f.item_id) > (after[0], after[1]))
            and (epic_ref is None or f.epic_ref == epic_ref)
        ]
        return rows[:limit]

    def list_review_rounds_for_epic(self, epic_ref: str) -> list[_Round]:
        return [r for r in self._rounds if r.epic_ref == epic_ref]

    def list_pending(self) -> list[_Run]:
        return list(self._pending)


def _facts(**kwargs: Any) -> NodePrFacts:
    base: dict[str, Any] = {
        "epic_ref": EPIC,
        "item_id": "2303",
        "observed_at": NOW.isoformat(),
        "status": "in_review",
        "pr_url": PR_URL,
        "head_oid": "9f8e7d1",
        "merge_state": "CLEAN",
        "merge_state_since": "2026-10-05T10:00:00+00:00",
        "merge_state_at": READ_AT,
        "pr_pushed_at": "2026-10-06T09:00:00+00:00",
    }
    base.update(kwargs)
    return NodePrFacts(**base)


def _round(verdict: str = "READY", head: str = "9f8e7d1", n: int = 2) -> _Round:
    return _Round(
        item_id="2303",
        round=n,
        verdict=verdict,
        head_sha=head,
        recorded_at=NOW - timedelta(hours=3),
    )


def _one(store: _FakeStore) -> Any:
    page = compose_freshness(store, DispatchConfig(), now=NOW)
    assert len(page.rows) == 1, page
    return page.rows[0]


# --------------------------------------------------------------------------- #
# review_state — one test each (AC1)
# --------------------------------------------------------------------------- #


def test_review_state_no_round_when_nothing_has_reviewed_it() -> None:
    """ "Never reviewed" is not "not stale" — it gets its own word."""
    row = _one(_FakeStore([_facts()]))

    assert row.review_state == "no_round"
    assert row.last_verdict is None


def test_review_state_current_when_the_verdict_names_the_live_head() -> None:
    row = _one(_FakeStore([_facts(head_oid="9f8e7d1")], [_round(head="9f8e7d1")]))

    assert row.review_state == "current"
    assert row.last_verdict is not None
    assert row.last_verdict.head_sha == "9f8e7d1"
    assert row.last_verdict.round == 2


def test_review_state_stale_when_the_head_moved_after_the_verdict() -> None:
    """The node's own cause: the review is about a commit that is not the head."""
    row = _one(_FakeStore([_facts(head_oid="a1b2c3d")], [_round(head="9f8e7d1")]))

    assert row.review_state == "stale"
    assert row.latest_head.sha == "a1b2c3d"
    assert row.latest_head.source == "snapshot"
    assert row.latest_head.at == "2026-10-06T09:00:00+00:00"
    assert row.last_verdict is not None
    assert row.last_verdict.head_sha == "9f8e7d1"


@pytest.mark.parametrize("verdict", ["NEEDS_CHANGES", "NEEDS_FULL"])
def test_review_state_remediating_when_kew_owes_the_next_round(verdict: str) -> None:
    """A head kew itself is moving is not a staleness a person has to read."""
    row = _one(
        _FakeStore([_facts(head_oid="a1b2c3d")], [_round(verdict=verdict, head="9f8")])
    )

    assert row.review_state == "remediating"


def test_review_state_reviewing_outranks_a_moved_head() -> None:
    """A round reading the artifact now is recomputing the very answer."""
    store = _FakeStore(
        [_facts(head_oid="a1b2c3d")],
        [_round(head="9f8e7d1")],
        [_Run(item_id="2303", started_at=NOW - timedelta(minutes=2))],
    )

    assert _one(store).review_state == "reviewing"


def test_a_pending_implementation_run_is_not_a_review_in_flight() -> None:
    """``reviewing`` is kew's own review lane, not any pending row."""
    store = _FakeStore(
        [_facts(head_oid="a1b2c3d")],
        [_round(head="9f8e7d1")],
        [_Run(item_id="2303", run_kind="dispatch", started_at=NOW)],
    )

    assert _one(store).review_state == "stale"


def test_an_unobserved_head_makes_no_staleness_claim() -> None:
    """No journaled head: the verdict's own sha is reported, and nothing is stale."""
    row = _one(_FakeStore([_facts(head_oid=None)], [_round(head="9f8e7d1")]))

    assert row.review_state == "current"
    assert row.latest_head.sha == "9f8e7d1"
    assert row.latest_head.source == "review_round"


def test_no_head_anywhere_reports_unobserved_rather_than_empty_truth() -> None:
    row = _one(_FakeStore([_facts(head_oid=None)]))

    assert row.latest_head.source == "unobserved"
    assert row.latest_head.sha == ""


# --------------------------------------------------------------------------- #
# base.state — one test each (AC1)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("merge_state", "expected"),
    [
        ("CLEAN", "clean"),
        ("HAS_HOOKS", "clean"),
        ("UNSTABLE", "clean"),
        ("BEHIND", "behind"),
        ("DIRTY", "conflicted"),
        ("BLOCKED", "blocked"),
        ("DRAFT", "blocked"),
    ],
)
def test_base_state_per_merge_state(merge_state: str, expected: str) -> None:
    row = _one(_FakeStore([_facts(merge_state=merge_state)]))

    assert row.base.state == expected
    assert row.base.reason != ""
    assert row.base.source == "snapshot"
    assert row.base.since == "2026-10-05T10:00:00+00:00"


def test_base_state_unknown_names_why_rather_than_reading_clean() -> None:
    """An unjournaled merge state is not a clean tree (rule 4: no clean default)."""
    row = _one(_FakeStore([_facts(merge_state=None, merge_state_since=None)]))

    assert row.base.state == "unknown"
    assert "has not journaled" in row.base.reason
    assert row.base.source == "unobserved"


def test_an_unrecognised_merge_state_is_unknown_and_names_the_word() -> None:
    """A merge state GitHub adds later must not silently read as clean."""
    state, reason = base_state("SOMETHING_NEW")

    assert state == "unknown"
    assert "SOMETHING_NEW" in reason


def test_only_a_conflict_is_a_persons_move() -> None:
    """DESIGN.md rations magenta: a rebase kew can do itself is not a needs-you."""
    assert set(NEEDS_A_PERSON_BASE_STATES) == {"conflicted"}


# --------------------------------------------------------------------------- #
# Page shape, bounds and silence-on-failure
# --------------------------------------------------------------------------- #


def test_pr_number_comes_off_the_url_never_from_a_slug() -> None:
    assert pr_number(PR_URL) == 2303
    assert pr_number("https://example.invalid/not-a-pr") is None
    assert pr_number(None) is None


def test_a_full_page_carries_a_cursor_and_the_cursor_is_honoured() -> None:
    rows = [
        _facts(item_id="2301", pr_url="https://github.com/o/r/pull/1"),
        _facts(item_id="2302", pr_url="https://github.com/o/r/pull/2"),
    ]
    store = _FakeStore(rows)

    first = compose_freshness(store, DispatchConfig(), limit=1, now=NOW)
    assert [r.item_id for r in first.rows] == ["2301"]
    assert first.next_cursor == f"{EPIC}|2301"

    second = compose_freshness(
        store, DispatchConfig(), limit=1, cursor=first.next_cursor, now=NOW
    )
    assert [r.item_id for r in second.rows] == ["2302"]


def test_a_short_page_carries_no_cursor() -> None:
    page = compose_freshness(_FakeStore([_facts()]), DispatchConfig(), limit=5, now=NOW)

    assert page.next_cursor is None


def test_the_epic_filter_reaches_the_store_query() -> None:
    """One epic's page, bounded on its own (#2303).

    A global page can fill before reaching the epic a surface is drawing, and
    the resulting silence is indistinguishable from "nothing here is stale" —
    so the ref is pushed into the query rather than applied to a page that was
    already truncated.
    """
    store = _FakeStore([_facts(), _facts(epic_ref="other", item_id="9")])

    mine = compose_freshness(store, DispatchConfig(), epic=EPIC, limit=1, now=NOW)
    theirs = compose_freshness(store, DispatchConfig(), epic="other", limit=1, now=NOW)

    assert store.fact_calls[0]["epic_ref"] == EPIC
    assert [r.item_id for r in mine.rows] == ["2303"]
    assert [r.item_id for r in theirs.rows] == ["9"]


def test_no_epic_asked_for_still_reads_every_epic() -> None:
    store = _FakeStore([_facts()])

    compose_freshness(store, DispatchConfig(), now=NOW)

    assert store.fact_calls[-1]["epic_ref"] is None


def test_the_base_read_instant_is_carried_through_to_the_row() -> None:
    """``base.at`` is when the merge state was READ; ``observed_at`` is the tick
    that reported it, and the driver carries a state across a tick that read
    nothing — so the two are deliberately different fields."""
    row = _one(_FakeStore([_facts(merge_state_at="2026-10-06T06:00:00+00:00")]))

    assert row.base.at == "2026-10-06T06:00:00+00:00"
    assert row.observed_at == NOW.isoformat()


def test_a_base_nobody_read_carries_no_instant() -> None:
    row = _one(_FakeStore([_facts(merge_state=None, merge_state_at=None)]))

    assert row.base.state == "unknown"
    assert row.base.at is None
    assert row.base.source == "unobserved"


def test_the_limit_is_clamped_to_the_ceiling() -> None:
    store = _FakeStore([_facts()])

    compose_freshness(store, DispatchConfig(), limit=10_000, now=NOW)

    assert store.fact_calls[-1]["limit"] == MAX_LIMIT


def test_an_unparseable_cursor_is_named_not_silently_page_one() -> None:
    page = compose_freshness(_FakeStore([_facts()]), DispatchConfig(), cursor="junk")

    assert any("cursor" in line for line in page.unavailable)


def test_a_refused_round_read_is_named_rather_than_read_as_no_round() -> None:
    class _Refusing(_FakeStore):
        def list_review_rounds_for_epic(self, epic_ref: str) -> list[_Round]:
            raise RuntimeError("review_rounds is locked")

    page = compose_freshness(_Refusing([_facts()]), DispatchConfig(), now=NOW)

    assert any("review_rounds" in line for line in page.unavailable)


def test_a_refused_fact_read_is_named_rather_than_an_empty_page() -> None:
    class _Refusing(_FakeStore):
        def latest_node_pr_facts(self, **kwargs: Any) -> list[NodePrFacts]:
            raise RuntimeError("the snapshot could not be read")

    page = compose_freshness(_Refusing([]), DispatchConfig(), now=NOW)

    assert page.rows == []
    assert any("snapshot_pr_facts" in line for line in page.unavailable)


# --------------------------------------------------------------------------- #
# End to end over the real store: the SQL filter and the journaled snapshot
# --------------------------------------------------------------------------- #


def _snapshot(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema": 1,
        "epic_ref": EPIC,
        "signal": "progressing",
        "counts": {},
        "frontier": [],
        "nodes": nodes,
        "edges": [],
    }


def test_the_store_reads_only_open_nodes_with_a_linked_pr(tmp_path: Path) -> None:
    with SqliteRunStore(tmp_path / "runs.db") as store:
        store.append_epic_tick(
            epic_ref=EPIC,
            tick_ts=NOW.isoformat(),
            snapshot=_snapshot(
                [
                    {"id": "2301", "status": "in_review", "pr_url": PR_URL},
                    {"id": "2302", "status": "done", "pr_url": PR_URL},
                    {"id": "2303", "status": "cancelled", "pr_url": PR_URL},
                    {"id": "2304", "status": "todo", "pr_url": None},
                    {"id": "2305", "status": "todo", "external": True},
                ]
            ),
            actions={},
            snapshot_hash="h1",
        )

        assert [f.item_id for f in store.latest_node_pr_facts()] == ["2301"]


def test_a_moved_head_reads_stale_end_to_end(tmp_path: Path) -> None:
    """The node's browser case, as data: a PR whose head moved after a READY."""
    with SqliteRunStore(tmp_path / "runs.db") as store:
        store.append_epic_tick(
            epic_ref=EPIC,
            tick_ts=NOW.isoformat(),
            snapshot=_snapshot(
                [
                    {
                        "id": "2303",
                        "status": "in_review",
                        "pr_url": PR_URL,
                        "head_oid": "a1b2c3d",
                        "merge_state": "DIRTY",
                        "merge_state_since": "2026-10-05T10:00:00+00:00",
                        "pr_pushed_at": "2026-10-06T09:00:00+00:00",
                    }
                ]
            ),
            actions={},
            snapshot_hash="h1",
        )
        store.record_review_round(
            EPIC,
            "2303",
            head_sha="9f8e7d1",
            verdict="READY",
        )

        page = compose_freshness(store, DispatchConfig(), now=NOW)

    assert len(page.rows) == 1
    row = page.rows[0]
    assert row.pr_number == 2303
    assert row.review_state == "stale"
    assert row.latest_head.sha == "a1b2c3d"
    assert row.last_verdict is not None
    assert row.last_verdict.head_sha == "9f8e7d1"
    assert row.base.state == "conflicted"
    assert page.unavailable == []


def test_a_snapshot_row_that_is_not_json_is_skipped_never_repaired(
    tmp_path: Path,
) -> None:
    """Operator rule: tolerate a bad historical row, never rewrite it."""
    with SqliteRunStore(tmp_path / "runs.db") as store:
        store.append_epic_tick(
            epic_ref=EPIC,
            tick_ts=NOW.isoformat(),
            snapshot=_snapshot([{"id": "2303", "status": "todo", "pr_url": PR_URL}]),
            actions={},
            snapshot_hash="h1",
        )
        store._execute("UPDATE epic_ticks SET snapshot = 'not json'")
        store._conn.commit()

        assert store.latest_node_pr_facts() == []


# --------------------------------------------------------------------------- #
# The same journaled fact is what finally wires serve's merge_conflict verdict
# --------------------------------------------------------------------------- #


def test_the_projection_reads_the_journaled_merge_states() -> None:
    """The state AND the instant it was read at: a state nobody can age is not
    something ``cached_pull_request`` will raise a blocking verdict from."""
    states = _journaled_merge_states(
        _FakeStore(
            [
                _facts(merge_state="DIRTY", merge_state_at=READ_AT),
                _facts(item_id="2304", merge_state_at=READ_AT),
            ]
        ),
        None,
    )

    assert states.by_item == {"2303": ("DIRTY", READ_AT), "2304": ("CLEAN", READ_AT)}
    assert states.unread is None


def test_a_merge_state_with_no_reading_instant_carries_none() -> None:
    """A pre-``merge_state_at`` snapshot: the state is there, its age is not."""
    states = _journaled_merge_states(_FakeStore([_facts(merge_state_at=None)]), None)

    assert states.by_item == {"2303": ("CLEAN", None)}


def test_a_store_too_old_to_answer_leaves_the_dimension_unknown() -> None:
    """Not a claim that nothing conflicts — the reading serve already had."""

    class _Old:
        pass

    states = _journaled_merge_states(_Old(), None)

    assert states.by_item == {}
    assert states.unread is not None  # and it SAYS nobody could look


def test_a_refused_merge_state_read_is_logged_not_read_as_clean(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _Refusing(_FakeStore):
        def latest_node_pr_facts(self, **kwargs: Any) -> list[NodePrFacts]:
            raise RuntimeError("the snapshot could not be read")

    with caplog.at_level("WARNING"):
        states = _journaled_merge_states(_Refusing([]), None)

    assert states.by_item == {}
    assert "the snapshot could not be read" in (states.unread or "")
    assert "journaled merge states" in caplog.text


# --------------------------------------------------------------------------- #
# The merge-state read is PAGED: one page of it read truncation as "clean"
# --------------------------------------------------------------------------- #


def test_the_merge_state_read_follows_the_cursor_past_the_first_page() -> None:
    """The reviewer finding (#2303): ``latest_node_pr_facts`` is a bounded,
    cursored read, so a single call left every artifact past the page with no
    tree state — and on the path that raises the BLOCKING ``merge_conflict``
    that reads exactly like "nothing conflicts"."""
    rows = [
        _facts(item_id=f"{3000 + n}", merge_state="CLEAN", merge_state_at=READ_AT)
        for n in range(_MERGE_STATE_PAGE + 1)
    ]
    conflicted = _facts(item_id="9999", merge_state="DIRTY", merge_state_at=READ_AT)
    store = _FakeStore([*rows, conflicted])

    states = _journaled_merge_states(store, None)

    # The conflicted tree is the LAST row of the second page: one page would
    # never have reached it.
    assert states.by_item["9999"] == ("DIRTY", READ_AT)
    assert len(states.by_item) == len(rows) + 1
    assert states.unread is None
    assert [c["after"] for c in store.fact_calls] == [None, (EPIC, "3999")]


def test_a_walk_that_hits_the_page_cap_says_so() -> None:
    """A journal that keeps answering with a full page must neither spin the
    poll forever nor let the rows it never reached look clean."""

    class _Endless(_FakeStore):
        def latest_node_pr_facts(self, **kwargs: Any) -> list[NodePrFacts]:
            self.fact_calls.append(dict(kwargs))
            return [
                _facts(item_id=f"{len(self.fact_calls)}-{n}", merge_state="CLEAN")
                for n in range(_MERGE_STATE_PAGE)
            ]

    store = _Endless([])
    states = _journaled_merge_states(store, None)

    assert len(store.fact_calls) == _MERGE_STATE_MAX_PAGES
    assert states.unread is not None


def test_a_scoped_walk_stops_once_every_item_it_asked_about_answered() -> None:
    """The node join reads a handful of items on a 1s poll (#2096): once all of
    them have a state, another page cannot change one."""
    store = _FakeStore(
        [
            _facts(item_id="2303", merge_state="DIRTY", merge_state_at=READ_AT),
            _facts(item_id="2304", merge_state="CLEAN", merge_state_at=READ_AT),
        ]
    )

    states = _journaled_merge_states(store, ["2303"])

    assert states.by_item == {"2303": ("DIRTY", READ_AT)}  # scope filters the rows
    assert len(store.fact_calls) == 1
    assert states.unread is None


def test_the_page_asked_for_is_one_the_store_will_serve_whole() -> None:
    """A page above the store's own ceiling would be silently clamped, and the
    walk would then read a short page as the end of the journal."""
    assert _MERGE_STATE_PAGE <= MAX_NODE_PR_FACTS_LIMIT


def test_an_item_whose_state_was_never_reached_says_so_in_its_detail() -> None:
    """The confession the finding asked for: an artifact the merge-state read
    never covered must not be composed as one with nothing journaled."""

    class _Record:
        pr_url = PR_URL
        pr_state = "OPEN"
        item_id = "2303"
        finished_at = NOW

    fact = _cached_artifact(
        _Record(),
        _JournaledMergeStates({}, "the journaled merge-state read refused: boom"),
        now=NOW,
    )

    assert fact.mergeability is None
    assert "could not be read" in (fact.observation.detail or "")
    assert "boom" in (fact.observation.detail or "")


def test_an_item_whose_state_WAS_read_keeps_the_ordinary_detail() -> None:
    """A per-item confession for an item that was answered would be noise."""

    class _Record:
        pr_url = PR_URL
        pr_state = "OPEN"
        item_id = "2303"
        finished_at = NOW

    fact = _cached_artifact(
        _Record(),
        _JournaledMergeStates({"2303": ("DIRTY", NOW.isoformat())}, "truncated"),
        now=NOW,
    )

    assert fact.mergeability == "conflicted"
    assert "could not be read" not in (fact.observation.detail or "")
