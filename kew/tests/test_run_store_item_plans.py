"""Recorded acceptance-criteria plans in the ledger (#2290).

The plan is a ledger row bound to the digest of the body it was read from, and
the host writes it. An unchanged body records nothing new; a changed body
supersedes the row the old body produced, and the superseded row stays in the
history.
"""

from __future__ import annotations

import pytest

from kew.run_store import CURRENT_SCHEMA_VERSION, SqliteRunStore

_PLAN_JSON = '{"acceptance_criteria":[{"id":"AC1","statement":"It works"}]}'


def _record(store: SqliteRunStore, item_id: str, digest: str, **kwargs):
    return store.record_item_plan(
        item_id,
        body_digest=digest,
        source="extracted",
        plan_sha=f"sha-of-{digest}",
        plan_json=_PLAN_JSON,
        criteria_count=1,
        **kwargs,
    )


def test_the_same_body_twice_records_one_row(tmp_path):
    with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
        first = _record(store, "2278", "digest-a", run_id="run-1")
        second = _record(store, "2278", "digest-a", run_id="run-2")

        assert first.recorded is True
        assert first.superseded is None
        assert second.recorded is False
        assert second.superseded is None
        # The second observation reads back the row the first one wrote,
        # including its original run_id — the plan is keyed on the body.
        assert second.current.id == first.current.id
        assert second.current.run_id == "run-1"
        assert len(store.list_item_plans("2278")) == 1


def test_an_edited_body_supersedes_the_old_plan(tmp_path):
    with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
        first = _record(store, "2278", "digest-a")
        second = _record(store, "2278", "digest-b")

        assert second.recorded is True
        assert second.superseded is not None
        assert second.superseded.id == first.current.id
        assert second.superseded.body_digest == "digest-a"
        assert second.superseded.superseded_at is not None

        current = store.current_item_plan("2278")
        assert current is not None
        assert (current.body_digest, current.superseded_at) == ("digest-b", None)

        history = store.list_item_plans("2278")
        assert [row.body_digest for row in history] == ["digest-a", "digest-b"]


def test_a_reverted_body_records_a_fresh_row_rather_than_reviving(tmp_path):
    with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
        _record(store, "2278", "digest-a")
        _record(store, "2278", "digest-b")
        back = _record(store, "2278", "digest-a")

        assert back.recorded is True
        assert [row.body_digest for row in store.list_item_plans("2278")] == [
            "digest-a",
            "digest-b",
            "digest-a",
        ]


def test_plans_are_scoped_per_item(tmp_path):
    with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
        _record(store, "2278", "digest-a")
        _record(store, "SFD-9", "digest-a")

        assert store.current_item_plan("2278") is not None
        assert store.current_item_plan("SFD-9") is not None
        assert store.current_item_plan("missing") is None
        assert store.list_item_plans("missing") == []


def test_fields_round_trip_including_approval_and_count(tmp_path):
    with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
        write = store.record_item_plan(
            "SFD-9",
            body_digest="digest-a",
            source="human",
            plan_sha="plan-sha",
            plan_json=_PLAN_JSON,
            criteria_count=3,
            run_id="run-1",
            approved_by="tom",
        )

        row = write.current
        assert (row.item_id, row.run_id, row.source) == ("SFD-9", "run-1", "human")
        assert (row.plan_sha, row.plan_json) == ("plan-sha", _PLAN_JSON)
        assert (row.criteria_count, row.approved_by) == (3, "tom")
        assert row.recorded_at.tzinfo is not None


def test_a_body_with_no_criteria_still_records_a_row(tmp_path):
    with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
        write = store.record_item_plan(
            "2278",
            body_digest="digest-empty",
            source="extracted",
            plan_sha="plan-sha",
            plan_json='{"acceptance_criteria":[]}',
            criteria_count=0,
        )

        assert write.recorded is True
        assert write.current.criteria_count == 0


def test_v60_adds_item_plans_and_review_round_plan_binding(tmp_path):
    """A pre-v60 ledger gains ``item_plans`` plus the two nullable
    ``review_rounds`` columns, and keeps every row it already had."""
    db_path = str(tmp_path / "dispatch.db")
    with SqliteRunStore(db_path) as store:
        store.record_review_round(
            "epic:x", "7", head_sha="old", verdict="READY", review_mode="full"
        )
        store._conn.execute("DROP TABLE item_plans")
        store._conn.execute("ALTER TABLE review_rounds DROP COLUMN plan_sha")
        store._conn.execute("ALTER TABLE review_rounds DROP COLUMN criteria_json")
        store._conn.execute("UPDATE schema_version SET version = 59 WHERE id = 1")
        store._conn.commit()

    with SqliteRunStore(db_path) as store:
        version = store._conn.execute(
            "SELECT version FROM schema_version WHERE id = 1"
        ).fetchone()["version"]
        assert version == CURRENT_SCHEMA_VERSION == 77

        [legacy] = store.list_review_rounds("7")
        assert (legacy.head_sha, legacy.review_mode) == ("old", "full")
        assert (legacy.plan_sha, legacy.criteria_json) == (None, None)

        assert store.current_item_plan("7") is None
        assert _record(store, "7", "digest-a").recorded is True

        indexes = {
            row["name"]
            for row in store._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
                " AND tbl_name = 'item_plans'"
            )
        }
        assert "idx_item_plans_item" in indexes


def test_a_review_round_can_bind_the_plan_it_was_judged_against(tmp_path):
    with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
        store.record_review_round(
            "epic:x",
            "7",
            head_sha="head",
            verdict="READY",
            plan_sha="plan-sha",
            criteria_json=_PLAN_JSON,
        )

        latest = store.latest_review_round("7")
        assert latest is not None
        assert (latest.plan_sha, latest.criteria_json) == ("plan-sha", _PLAN_JSON)


def test_v60_tolerates_a_ledger_without_review_rounds(tmp_path):
    """The v60 step must not assume ``review_rounds`` exists: a hand-built
    fixture that never had the table still has to reach v60 and gain
    ``item_plans``."""
    db_path = str(tmp_path / "no-rounds.db")
    with SqliteRunStore(db_path) as store:
        store._conn.execute("DROP TABLE item_plans")
        store._conn.execute("DROP TABLE review_rounds")
        store._conn.execute("UPDATE schema_version SET version = 59 WHERE id = 1")
        store._conn.commit()

    with SqliteRunStore(db_path) as store:
        version = store._conn.execute(
            "SELECT version FROM schema_version WHERE id = 1"
        ).fetchone()["version"]
        assert version == CURRENT_SCHEMA_VERSION
        assert _record(store, "7", "digest-a").recorded is True


@pytest.mark.parametrize("count", [-1, "three"])
def test_a_non_countable_criteria_count_is_refused(tmp_path, count):
    with (
        SqliteRunStore(str(tmp_path / "dispatch.db")) as store,
        pytest.raises(ValueError, match="criteria_count"),
    ):
        store.record_item_plan(
            "7",
            body_digest="digest-a",
            source="extracted",
            plan_sha="plan-sha",
            plan_json=_PLAN_JSON,
            criteria_count=count,
        )


def test_an_unknown_plan_source_is_refused(tmp_path):
    with (
        SqliteRunStore(str(tmp_path / "dispatch.db")) as store,
        pytest.raises(ValueError, match="source"),
    ):
        store.record_item_plan(
            "7",
            body_digest="digest-a",
            source="agent",
            plan_sha="plan-sha",
            plan_json=_PLAN_JSON,
            criteria_count=1,
        )
