"""Tests for governed-review round-counter persistence (F8/#977, Task 2).

Review state (round counter + per-round {head_sha, verdict, sidecar_digest})
lives in the run store so a driver restart mid-review resumes the round count
instead of restarting it, and `kew epic retry` resets the counter for a fresh
review allowance while the prior rounds stay visible in the persisted history
(F15). The reset boundary is the existing `epic_retries` marker — the same
supersession model `overlay_status` uses for failure runs.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from kew.run_store import (
    CURRENT_SCHEMA_VERSION,
    ReviewRound,
    RunRecord,
    SqliteRunStore,
)


def test_record_review_round_increments_within_cycle(tmp_path):
    db_path = str(tmp_path / "dispatch.db")
    with SqliteRunStore(db_path) as store:
        r1 = store.record_review_round(
            "epic:x", "42", head_sha="aaa", verdict="NEEDS_CHANGES", sidecar_digest="d1"
        )
        r2 = store.record_review_round(
            "epic:x", "42", head_sha="bbb", verdict="READY", sidecar_digest="d2"
        )
        assert (r1, r2) == (1, 2)
        assert store.review_round_count("42") == 2


def test_review_round_count_survives_reopen(tmp_path):
    # Driver-restart test: the round counter survives process death.
    db_path = str(tmp_path / "dispatch.db")
    with SqliteRunStore(db_path) as store:
        store.record_review_round(
            "epic:x", "42", head_sha="aaa", verdict="NEEDS_CHANGES"
        )
        store.record_review_round("epic:x", "42", head_sha="bbb", verdict="READY")

    # Fresh process/store over the same on-disk DB.
    with SqliteRunStore(db_path) as store:
        assert store.review_round_count("42") == 2
        latest = store.latest_review_round("42")
        assert latest is not None
        assert (latest.round, latest.head_sha, latest.verdict) == (2, "bbb", "READY")


def test_epic_retry_resets_counter_but_preserves_history(tmp_path):
    db_path = str(tmp_path / "dispatch.db")
    with SqliteRunStore(db_path) as store:
        store.record_review_round(
            "epic:x", "42", head_sha="aaa", verdict="NEEDS_CHANGES"
        )
        store.record_review_round("epic:x", "42", head_sha="bbb", verdict="READY")
        assert store.review_round_count("42") == 2

        # `kew epic retry` records the marker (fresh implementation ⇒ fresh
        # review allowance).
        store.mark_epic_retry_item("epic:x", "42")

        # Counter reset for the new cycle...
        assert store.review_round_count("42") == 0
        assert store.latest_review_round("42") is None
        # ...but the prior rounds remain in the persisted history (F15).
        history = store.list_review_rounds("42")
        assert [rr.head_sha for rr in history] == ["aaa", "bbb"]

        # A round recorded after the retry starts the new cycle at round 1.
        r = store.record_review_round(
            "epic:x", "42", head_sha="ccc", verdict="NEEDS_CHANGES"
        )
        assert r == 1
        assert store.review_round_count("42") == 1
        assert [rr.head_sha for rr in store.list_review_rounds("42")] == [
            "aaa",
            "bbb",
            "ccc",
        ]


def test_review_rounds_isolated_per_item(tmp_path):
    db_path = str(tmp_path / "dispatch.db")
    with SqliteRunStore(db_path) as store:
        store.record_review_round("epic:x", "1", head_sha="a", verdict="READY")
        store.record_review_round("epic:x", "2", head_sha="b", verdict="READY")
        store.record_review_round("epic:x", "2", head_sha="c", verdict="READY")
        assert store.review_round_count("1") == 1
        assert store.review_round_count("2") == 2


def test_review_round_dataclass_roundtrip(tmp_path):
    db_path = str(tmp_path / "dispatch.db")
    with SqliteRunStore(db_path) as store:
        store.record_review_round(
            "epic:x", "9", head_sha="sha9", verdict="READY", sidecar_digest="dig9"
        )
        rows = store.list_review_rounds("9")
        assert len(rows) == 1
        rr = rows[0]
        assert isinstance(rr, ReviewRound)
        assert rr.epic_ref == "epic:x"
        assert rr.item_id == "9"
        assert rr.round == 1
        assert rr.head_sha == "sha9"
        assert rr.verdict == "READY"
        assert rr.sidecar_digest == "dig9"
        assert isinstance(rr.recorded_at, datetime)


def test_v29_creates_review_rounds_preserves_data(tmp_path):
    """Reopening a pre-v29 DB creates the off-chain review_rounds table
    without disturbing existing rows (F8/#977)."""
    db_path = str(tmp_path / "dispatch.db")
    with SqliteRunStore(db_path) as store:
        store.save(
            RunRecord(
                run_id="r-keep",
                issue_num=7,
                branch="agent/issue-7",
                status="success",
                exit_code=0,
                started_at=datetime(2026, 1, 1, tzinfo=UTC),
                model="claude-opus-4-8",
                complexity="medium",
                estimated_cost_usd=0.25,
            )
        )
        store._conn.execute("DROP TABLE IF EXISTS review_rounds")
        store._conn.execute("UPDATE schema_version SET version = 28 WHERE id = 1")
        store._conn.commit()

    conn = sqlite3.connect(db_path)
    got = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='review_rounds'"
    ).fetchone()
    conn.close()
    assert got is None

    with SqliteRunStore(db_path) as store:
        assert store.get("r-keep") is not None
        ver = store._conn.execute(
            "SELECT version FROM schema_version WHERE id = 1"
        ).fetchone()["version"]
        assert ver == CURRENT_SCHEMA_VERSION
        assert store.review_round_count("7") == 0
        store.record_review_round("epic:x", "7", head_sha="z", verdict="READY")
        assert store.review_round_count("7") == 1


def test_review_mode_roundtrips_and_rounds_in_cycle_scope_to_retry(tmp_path):
    """#2111: a round records full/delta, and the cycle reader stops at the
    latest retry marker like the round counter does."""
    db_path = str(tmp_path / "dispatch.db")
    with SqliteRunStore(db_path) as store:
        store.record_review_round(
            "epic:x", "42", head_sha="a", verdict="NEEDS_CHANGES", review_mode="full"
        )
        store.mark_epic_retry_item("epic:x", "42")
        store.record_review_round(
            "epic:x", "42", head_sha="b", verdict="NEEDS_CHANGES", review_mode="full"
        )
        store.record_review_round(
            "epic:x", "42", head_sha="c", verdict="READY", review_mode="delta"
        )
        cycle = store.review_rounds_in_cycle("42")
        assert [(r.head_sha, r.review_mode) for r in cycle] == [
            ("b", "full"),
            ("c", "delta"),
        ]
        assert len(store.list_review_rounds("42")) == 3


def test_v58_adds_review_mode_and_preserves_rounds(tmp_path):
    """Reopening a v57 DB adds ``review_mode`` without touching existing rounds;
    the migrated rows read NULL (legacy rounds, all full reviews)."""
    db_path = str(tmp_path / "dispatch.db")
    with SqliteRunStore(db_path) as store:
        store.record_review_round(
            "epic:x", "7", head_sha="old", verdict="READY", sidecar_path="/s.json"
        )
        store._conn.execute("ALTER TABLE review_rounds DROP COLUMN review_mode")
        store._conn.execute("UPDATE schema_version SET version = 57 WHERE id = 1")
        store._conn.commit()

    with SqliteRunStore(db_path) as store:
        ver = store._conn.execute(
            "SELECT version FROM schema_version WHERE id = 1"
        ).fetchone()["version"]
        assert ver == CURRENT_SCHEMA_VERSION
        [legacy] = store.list_review_rounds("7")
        assert (legacy.head_sha, legacy.sidecar_path) == ("old", "/s.json")
        assert legacy.review_mode is None
        store.record_review_round(
            "epic:x", "7", head_sha="new", verdict="READY", review_mode="delta"
        )
        latest = store.latest_review_round("7")
        assert latest is not None
        assert latest.review_mode == "delta"


def test_accept_review_round_records_merge_readiness_in_the_ledger(tmp_path):
    """#2151: the stop rule's acceptance is a ROUND fact, not only a chain fact.

    A watcher polling recorded verdicts saw ``NEEDS_CHANGES`` for a round the
    driver had already accepted, so an accepted PR sat unmerged for hours. The
    acceptance is recorded beside the reviewer's verdict — which is never
    rewritten — and the merge-ready query returns the round.
    """
    db_path = str(tmp_path / "dispatch.db")
    with SqliteRunStore(db_path) as store:
        store.record_review_round(
            "epic:x",
            "42",
            head_sha="aaa",
            verdict="NEEDS_CHANGES",
            sidecar_digest="d1",
        )
        assert store.merge_ready_review_rounds("42") == []
        assert store.accept_review_round(
            "42",
            round_num=1,
            head_sha="aaa",
            sidecar_digest="d1",
            verdict="READY_WITH_RESIDUALS",
        )

    # A fresh process over the same DB: every reader sees the merge-ready state.
    with SqliteRunStore(db_path) as store:
        latest = store.latest_review_round("42")
        assert latest is not None
        assert latest.verdict == "NEEDS_CHANGES"
        assert latest.accepted_verdict == "READY_WITH_RESIDUALS"
        assert latest.effective_verdict == "READY_WITH_RESIDUALS"
        [ready] = store.merge_ready_review_rounds("42")
        assert (ready.round, ready.effective_verdict) == (1, "READY_WITH_RESIDUALS")


def test_merge_ready_rounds_read_reviewer_ready_and_scope_to_the_cycle(tmp_path):
    """A reviewer READY is merge-ready too, and a retry supersedes both."""
    db_path = str(tmp_path / "dispatch.db")
    with SqliteRunStore(db_path) as store:
        store.record_review_round("epic:x", "42", head_sha="aaa", verdict="READY")
        assert [r.round for r in store.merge_ready_review_rounds("42")] == [1]
        store.mark_epic_retry_item("epic:x", "42")
        assert store.merge_ready_review_rounds("42") == []
        store.record_review_round(
            "epic:x", "42", head_sha="bbb", verdict="NEEDS_CHANGES"
        )
        store.record_review_round("epic:x", "42", head_sha="ccc", verdict="NEEDS_FULL")
        assert store.merge_ready_review_rounds("42") == []
        store.accept_review_round(
            "42",
            round_num=1,
            head_sha="bbb",
            sidecar_digest=None,
            verdict="READY_WITH_RESIDUALS",
        )
        [ready] = store.merge_ready_review_rounds("42")
        assert (ready.round, ready.head_sha) == (1, "bbb")


def test_accept_review_round_refuses_a_binding_that_does_not_match(tmp_path):
    """Acceptance binds to round, head and sidecar digest: a mismatch marks
    nothing, so a stale acceptance can never make some other round read ready."""
    db_path = str(tmp_path / "dispatch.db")
    with SqliteRunStore(db_path) as store:
        store.record_review_round(
            "epic:x",
            "42",
            head_sha="aaa",
            verdict="NEEDS_CHANGES",
            sidecar_digest="d1",
        )
        for kwargs in (
            {"round_num": 2, "head_sha": "aaa", "sidecar_digest": "d1"},
            {"round_num": 1, "head_sha": "bbb", "sidecar_digest": "d1"},
            {"round_num": 1, "head_sha": "aaa", "sidecar_digest": "d2"},
            {"round_num": 1, "head_sha": "aaa", "sidecar_digest": None},
        ):
            assert not store.accept_review_round(
                "42", verdict="READY_WITH_RESIDUALS", **kwargs
            )
        assert store.merge_ready_review_rounds("42") == []
        latest = store.latest_review_round("42")
        assert latest is not None and latest.accepted_verdict is None


def test_v59_adds_accepted_verdict_and_preserves_rounds(tmp_path):
    """Reopening a v58 DB adds ``accepted_verdict`` without touching existing
    rounds; migrated rows read None — a pre-v59 acceptance is on the chain
    only, which is the bug #2151 fixes going forward."""
    db_path = str(tmp_path / "dispatch.db")
    with SqliteRunStore(db_path) as store:
        store.record_review_round(
            "epic:x", "7", head_sha="old", verdict="READY", review_mode="full"
        )
        store._conn.execute("ALTER TABLE review_rounds DROP COLUMN accepted_verdict")
        store._conn.execute("UPDATE schema_version SET version = 58 WHERE id = 1")
        store._conn.commit()

    with SqliteRunStore(db_path) as store:
        ver = store._conn.execute(
            "SELECT version FROM schema_version WHERE id = 1"
        ).fetchone()["version"]
        assert ver == CURRENT_SCHEMA_VERSION == 77
        [legacy] = store.list_review_rounds("7")
        assert (legacy.head_sha, legacy.review_mode) == ("old", "full")
        assert legacy.accepted_verdict is None
        assert legacy.effective_verdict == "READY"
        store.record_review_round(
            "epic:x", "7", head_sha="new", verdict="NEEDS_CHANGES"
        )
        assert store.accept_review_round(
            "7",
            round_num=2,
            head_sha="new",
            sidecar_digest=None,
            verdict="READY_WITH_RESIDUALS",
        )
        # The legacy READY round reads ready too; the accepted one joins it.
        ready = store.merge_ready_review_rounds("7")
        assert [(r.head_sha, r.effective_verdict) for r in ready] == [
            ("old", "READY"),
            ("new", "READY_WITH_RESIDUALS"),
        ]


def test_review_cycle_start_publishes_the_boundary_the_cycle_readers_use(tmp_path):
    """The public boundary reader (#2075): a fact recorded OUTSIDE this table —
    an ``adversarial_review_verdict`` row on the audit chain — has to be placed
    in the current cycle or in the superseded history, and it must use the same
    marker ``review_rounds_in_cycle`` scopes to, not a second rule."""
    db_path = str(tmp_path / "dispatch.db")
    with SqliteRunStore(db_path) as store:
        # Never retried: the whole history is the current cycle.
        assert store.review_cycle_start("42") is None
        store.record_review_round("epic:x", "42", head_sha="a", verdict="READY")
        assert store.review_cycle_start("42") is None

        store.mark_epic_retry_item("epic:x", "42")
        marker = store.review_cycle_start("42")
        assert marker == store._review_cycle_start("42")
        assert marker is not None
        # The boundary is orderable against a recorded stamp, and the round it
        # supersedes is the one the cycle reader has already dropped.
        [superseded] = store.list_review_rounds("42")
        assert datetime.fromisoformat(marker) > superseded.recorded_at
        assert store.review_rounds_in_cycle("42") == []
