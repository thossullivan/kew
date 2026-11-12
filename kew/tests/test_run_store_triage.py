"""The ``triage_decisions`` ledger: a proposal, and who resolved it (#2293).

The schema's two load-bearing properties are asserted here rather than
described:

* **A proposal is not a decision.** A recorded row's ``resolution`` columns are
  NULL, and the only way they ever fill is
  :meth:`~kew.run_store_triage.TriageDecisionStoreMixin.resolve_triage_decision`
  — which refuses an empty actor and refuses to rewrite an already-resolved row.
* **An unavailable row is still a row.** A classification that never happened is
  recorded with a NULL ``type``, so "triage could not read this item" is a fact
  rather than an absence that reads like "nobody has triaged it".
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from kew.run_store import (
    CURRENT_SCHEMA_VERSION,
    TRIAGE_OUTCOMES,
    SqliteRunStore,
)
from kew.run_store_triage import TRIAGE_RESOLUTIONS
from kew.triage.policy import OUTCOMES


def _record(store: Any, item_id: str, digest: str, **extra: Any) -> Any:
    fields: dict[str, Any] = {
        "body_digest": digest,
        "backend": "local",
        "model": "local-7b",
        "prompt_sha": "prompt-sha",
        "outcome": "propose_dispatch",
        "decision_json": '{"type": "feature"}',
        "decision_type": "feature",
        "priority": "p2",
        "complexity": "medium",
        "confidence": 0.86,
        "actionable": True,
        "needs_info": False,
    }
    fields.update(extra)
    return store.record_triage_decision(item_id, **fields)


@pytest.fixture
def store(tmp_path: Path) -> Any:
    with SqliteRunStore(str(tmp_path / "dispatch.db")) as opened:
        yield opened


class TestTheOutcomeVocabularyIsOne:
    def test_the_store_and_the_policy_name_the_same_outcomes(self) -> None:
        """``run_store`` restates the vocabulary because it sits below the policy."""
        assert TRIAGE_OUTCOMES == OUTCOMES

    def test_an_unknown_outcome_is_refused(self, store: Any) -> None:
        with pytest.raises(ValueError, match="outcome must be one of"):
            _record(store, "7", "digest-a", outcome="auto_close")


class TestRecordingAndSupersession:
    def test_a_recorded_row_is_a_proposal_with_no_resolution(self, store: Any) -> None:
        write = _record(store, "7", "digest-a", issue_num=7)
        row = write.current
        assert write.recorded is True
        assert (row.resolution, row.resolved_by, row.resolved_at) == (None, None, None)
        assert row.unresolved is True
        assert (row.item_id, row.issue_num, row.outcome) == (
            "7",
            7,
            "propose_dispatch",
        )
        assert (row.backend, row.model, row.prompt_sha) == (
            "local",
            "local-7b",
            "prompt-sha",
        )

    def test_the_same_body_records_nothing_new(self, store: Any) -> None:
        first = _record(store, "7", "digest-a")
        again = _record(store, "7", "digest-a")
        assert again.recorded is False
        assert again.superseded is None
        assert again.current.id == first.current.id
        assert len(store.list_triage_decisions("7")) == 1

    def test_an_edited_body_supersedes_the_previous_proposal(self, store: Any) -> None:
        first = _record(store, "7", "digest-a")
        second = _record(store, "7", "digest-b", outcome="ask")
        assert second.recorded is True
        assert second.superseded is not None
        assert second.superseded.id == first.current.id
        assert second.superseded.superseded_at is not None
        assert store.current_triage_decision("7").id == second.current.id
        assert [r.id for r in store.list_triage_decisions("7")] == [
            first.current.id,
            second.current.id,
        ]

    def test_a_never_triaged_item_has_no_proposal(self, store: Any) -> None:
        assert store.current_triage_decision("nobody") is None
        assert store.list_triage_decisions("nobody") == []


class TestUnavailableIsStillARow:
    def test_an_unavailable_row_carries_no_decision_fields(self, store: Any) -> None:
        write = store.record_triage_decision(
            "7",
            body_digest="digest-a",
            backend="local",
            model="",
            prompt_sha="prompt-sha",
            outcome="unavailable",
            decision_json="{}",
        )
        row = write.current
        assert row.outcome == "unavailable"
        assert (row.type, row.confidence, row.actionable) == (None, None, None)
        assert row.unresolved is True

    def test_an_unavailable_row_reaches_the_unresolved_queue(self, store: Any) -> None:
        store.record_triage_decision(
            "7",
            body_digest="digest-a",
            backend="local",
            model="",
            prompt_sha="prompt-sha",
            outcome="unavailable",
            decision_json="{}",
        )
        assert [r.item_id for r in store.list_unresolved_triage_decisions()] == ["7"]


class TestOnlyAPersonResolves:
    @pytest.mark.parametrize("resolution", sorted(TRIAGE_RESOLUTIONS))
    def test_each_resolution_records_the_person_who_chose_it(
        self, store: Any, resolution: str
    ) -> None:
        write = _record(store, "7", "digest-a")
        row = store.resolve_triage_decision(
            write.current.id, resolution=resolution, resolved_by="tom"
        )
        assert (row.resolution, row.resolved_by) == (resolution, "tom")
        assert row.resolved_at is not None
        assert row.unresolved is False

    def test_a_resolution_with_no_actor_is_refused(self, store: Any) -> None:
        write = _record(store, "7", "digest-a")
        with pytest.raises(ValueError, match="only a person resolves"):
            store.resolve_triage_decision(
                write.current.id, resolution="dispatch", resolved_by="  "
            )
        assert store.current_triage_decision("7").resolution is None

    def test_an_unknown_resolution_is_refused(self, store: Any) -> None:
        write = _record(store, "7", "digest-a")
        with pytest.raises(ValueError, match="resolution must be one of"):
            store.resolve_triage_decision(
                write.current.id, resolution="merge", resolved_by="tom"
            )

    def test_a_second_decision_cannot_rewrite_the_first(self, store: Any) -> None:
        write = _record(store, "7", "digest-a")
        store.resolve_triage_decision(
            write.current.id, resolution="decline", resolved_by="tom"
        )
        again = store.resolve_triage_decision(
            write.current.id, resolution="dispatch", resolved_by="someone-else"
        )
        assert (again.resolution, again.resolved_by) == ("decline", "tom")

    def test_resolving_a_row_that_does_not_exist_answers_none(self, store: Any) -> None:
        assert (
            store.resolve_triage_decision(999, resolution="ask", resolved_by="tom")
            is None
        )


class TestTheUnresolvedQueue:
    def test_a_resolved_row_leaves_the_queue(self, store: Any) -> None:
        write = _record(store, "7", "digest-a")
        store.resolve_triage_decision(
            write.current.id, resolution="dispatch", resolved_by="tom"
        )
        assert store.list_unresolved_triage_decisions() == []

    def test_a_superseded_row_leaves_the_queue(self, store: Any) -> None:
        """Nobody can act on a proposal about a body that is no longer there."""
        _record(store, "7", "digest-a")
        second = _record(store, "7", "digest-b")
        queued = store.list_unresolved_triage_decisions()
        assert [row.id for row in queued] == [second.current.id]

    def test_the_queue_is_oldest_first_across_items(self, store: Any) -> None:
        _record(store, "7", "digest-a")
        _record(store, "8", "digest-b")
        _record(store, "9", "digest-c")
        assert [r.item_id for r in store.list_unresolved_triage_decisions()] == [
            "7",
            "8",
            "9",
        ]


def test_v61_adds_triage_decisions_and_keeps_existing_rows(tmp_path: Path) -> None:
    """A pre-v61 ledger gains ``triage_decisions`` and keeps every row it had."""
    db_path = str(tmp_path / "dispatch.db")
    with SqliteRunStore(db_path) as opened:
        opened.record_item_plan(
            "7",
            body_digest="digest-a",
            source="extracted",
            plan_sha="plan-sha",
            plan_json="{}",
            criteria_count=2,
        )
        opened._conn.execute("DROP TABLE triage_decisions")
        opened._conn.execute("UPDATE schema_version SET version = 60 WHERE id = 1")
        opened._conn.commit()

    with SqliteRunStore(db_path) as opened:
        version = opened._conn.execute(
            "SELECT version FROM schema_version WHERE id = 1"
        ).fetchone()["version"]
        assert version == CURRENT_SCHEMA_VERSION == 77
        plan = opened.current_item_plan("7")
        assert plan is not None and plan.criteria_count == 2
        assert opened.current_triage_decision("7") is None
        assert _record(opened, "7", "digest-a").recorded is True


def test_a_pre_v61_ledger_cannot_be_read_for_triage_without_migrating(
    tmp_path: Path,
) -> None:
    """A read-only open of an old ledger refuses loudly, not emptily.

    ``kew triage queue`` opens read-only (it must not migrate a ledger as a
    side effect of a read), so "this ledger predates triage" has to arrive as
    an error rather than as an empty queue.
    """
    db_path = tmp_path / "dispatch.db"
    with SqliteRunStore(str(db_path)) as opened:
        opened._conn.execute("DROP TABLE triage_decisions")
        opened._conn.execute("UPDATE schema_version SET version = 60 WHERE id = 1")
        opened._conn.commit()
    with (
        SqliteRunStore(str(db_path), read_only=True) as opened,
        pytest.raises(sqlite3.DatabaseError),
    ):
        opened.list_unresolved_triage_decisions()
