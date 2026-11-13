"""Recording an item's extracted plan for the run that dispatches it (#2290).

The host writes the plan; the chain attests it. An unchanged body adds nothing,
an edited body attests both the supersession and the new plan, and a ledger that
cannot be written leaves a reason rather than a silent "no criteria".

A failed attestation is recoverable: the row is committed and never rewritten,
so the next observation of the same body re-attempts the append under the same
dedup key rather than leaving the row off the chain forever.
"""

from __future__ import annotations

import logging

import pytest

from kew.audit_chain import verify_chain
from kew.run_store import SqliteRunStore
from kew.triage.plan import body_digest, extract_plan, plan_sha
from kew.triage.record import (
    PLAN_RECORDED_EVENT,
    PLAN_SUPERSEDED_EVENT,
    record_extracted_plan,
)

_BODY = """### Why

Nothing records the criteria.

### Acceptance

- The extractor reads this section.
- `kew plan show` prints it.
"""

_EDITED = _BODY + "- A third criterion arrived.\n"

_EDITED_AGAIN = _EDITED + "- And a fourth.\n"


@pytest.fixture
def store(tmp_path):
    with SqliteRunStore(str(tmp_path / "dispatch.db")) as opened:
        yield opened


def _events(store, event):
    return store.list_audit_events(limit=None, event=event)


def test_a_dispatch_records_the_extracted_criteria_and_attests_them(store):
    write = record_extracted_plan(
        store, item_id="2290", body=_BODY, run_id="run-1", issue_num=2290
    )

    assert write is not None and write.recorded is True
    row = store.current_item_plan("2290")
    assert row is not None
    assert (row.source, row.criteria_count, row.run_id) == ("extracted", 2, "run-1")
    assert row.body_digest == body_digest(_BODY)
    assert row.plan_sha == plan_sha(extract_plan(_BODY))
    assert row.approved_by is None

    [recorded] = _events(store, PLAN_RECORDED_EVENT)
    assert (recorded.actor_type, recorded.actor_id) == ("policy", "triage-plan")
    assert recorded.payload["plan_sha"] == row.plan_sha
    assert recorded.payload["criteria_count"] == 2
    assert recorded.payload["criteria_ids"] == ["AC1", "AC2"]
    assert recorded.payload["source"] == "extracted"
    assert _events(store, PLAN_SUPERSEDED_EVENT) == []


def test_the_same_body_on_a_later_tick_attests_nothing_new(store):
    record_extracted_plan(store, item_id="2290", body=_BODY, run_id="run-1")
    again = record_extracted_plan(store, item_id="2290", body=_BODY, run_id="run-2")

    assert again is not None and again.recorded is False
    assert len(_events(store, PLAN_RECORDED_EVENT)) == 1
    assert len(store.list_item_plans("2290")) == 1


def test_an_edited_body_attests_the_supersession_then_the_new_plan(store):
    first = record_extracted_plan(store, item_id="2290", body=_BODY)
    second = record_extracted_plan(store, item_id="2290", body=_EDITED)

    assert first is not None and second is not None
    assert second.superseded is not None

    [superseded] = _events(store, PLAN_SUPERSEDED_EVENT)
    assert superseded.payload["body_digest"] == body_digest(_BODY)
    assert superseded.payload["superseded_by_body_digest"] == body_digest(_EDITED)
    assert superseded.payload["criteria_count"] == 2

    recorded = _events(store, PLAN_RECORDED_EVENT)
    assert [event.payload["criteria_count"] for event in recorded] == [3, 2]
    # Supersession is attested before the plan that caused it.
    assert superseded.id < max(event.id for event in recorded)

    current = store.current_item_plan("2290")
    assert current is not None and current.criteria_count == 3


def test_a_body_stating_no_criteria_records_an_empty_plan(store):
    write = record_extracted_plan(store, item_id="2290", body="Just prose.")

    assert write is not None and write.current.criteria_count == 0
    [recorded] = _events(store, PLAN_RECORDED_EVENT)
    assert recorded.payload["criteria_ids"] == []


def test_no_store_records_nothing_without_raising():
    assert record_extracted_plan(None, item_id="2290", body=_BODY) is None


def test_a_ledger_failure_is_reported_rather_than_read_as_no_criteria(
    store, monkeypatch, caplog
):
    def _boom(*args, **kwargs):
        raise RuntimeError("ledger is gone")

    monkeypatch.setattr(store, "record_item_plan", _boom)
    with caplog.at_level(logging.WARNING):
        assert record_extracted_plan(store, item_id="2290", body=_BODY) is None

    assert "Could not record the acceptance criteria for 2290" in caplog.text
    assert "ledger is gone" in caplog.text


class TestAttestationIsRecoverable:
    """#2309 P4: audit.emit is fail-soft, so a dropped append must be retried.

    The ledger row is already committed when the append runs, and the next tick
    sees the same body and writes nothing — so without a retry the row would
    stay off the chain permanently.
    """

    @staticmethod
    def _drop_appends(monkeypatch, store):
        """Make every chain append fail the way a locked ledger would."""

        def _boom(*args, **kwargs):
            raise RuntimeError("chain is unavailable")

        monkeypatch.setattr(store, "append_audit_event", _boom)
        monkeypatch.setattr(store, "append_audit_event_once", _boom)

    def test_a_later_tick_re_attests_a_row_the_chain_never_got(
        self, store, monkeypatch, caplog
    ):
        with monkeypatch.context() as dropped:
            self._drop_appends(dropped, store)
            with caplog.at_level(logging.WARNING):
                first = record_extracted_plan(store, item_id="2290", body=_BODY)

        assert first is not None and first.recorded is True
        assert _events(store, PLAN_RECORDED_EVENT) == []

        again = record_extracted_plan(store, item_id="2290", body=_BODY)

        assert again is not None and again.recorded is False
        [recovered] = _events(store, PLAN_RECORDED_EVENT)
        assert recovered.payload["body_digest"] == body_digest(_BODY)
        assert len(store.list_item_plans("2290")) == 1

    def test_the_recovered_event_names_the_run_that_recorded_the_row(
        self, store, monkeypatch
    ):
        with monkeypatch.context() as dropped:
            self._drop_appends(dropped, store)
            record_extracted_plan(store, item_id="2290", body=_BODY, run_id="run-1")

        record_extracted_plan(store, item_id="2290", body=_BODY, run_id="run-2")

        [recovered] = _events(store, PLAN_RECORDED_EVENT)
        assert recovered.run_id == "run-1"

    def test_a_later_tick_re_attests_a_lost_supersession(self, store, monkeypatch):
        record_extracted_plan(store, item_id="2290", body=_BODY)
        with monkeypatch.context() as dropped:
            self._drop_appends(dropped, store)
            record_extracted_plan(store, item_id="2290", body=_EDITED)

        assert _events(store, PLAN_SUPERSEDED_EVENT) == []

        record_extracted_plan(store, item_id="2290", body=_EDITED)

        [superseded] = _events(store, PLAN_SUPERSEDED_EVENT)
        assert superseded.payload["body_digest"] == body_digest(_BODY)
        assert superseded.payload["superseded_by_body_digest"] == body_digest(_EDITED)

    def test_repeated_ticks_never_double_record_an_attested_row(self, store):
        for _ in range(3):
            record_extracted_plan(store, item_id="2290", body=_BODY)
        record_extracted_plan(store, item_id="2290", body=_EDITED)
        for _ in range(3):
            record_extracted_plan(store, item_id="2290", body=_EDITED)

        assert len(_events(store, PLAN_RECORDED_EVENT)) == 2
        assert len(_events(store, PLAN_SUPERSEDED_EVENT)) == 1

    def test_the_chain_still_verifies_after_a_recovered_append(
        self, store, monkeypatch
    ):
        with monkeypatch.context() as dropped:
            self._drop_appends(dropped, store)
            record_extracted_plan(store, item_id="2290", body=_BODY)
        record_extracted_plan(store, item_id="2290", body=_BODY)

        assert verify_chain(store.read_chain_rows()).ok is True

    def test_a_row_superseded_before_its_append_landed_is_still_recovered(
        self, store, monkeypatch
    ):
        """#2309 P6: the row an edit displaces gets no second write of its own.

        Its ``plan_recorded`` is therefore only ever retried by a walk of the
        whole history — the displacing write names the row it superseded, not
        the attestation that row never got.
        """
        with monkeypatch.context() as dropped:
            self._drop_appends(dropped, store)
            record_extracted_plan(store, item_id="2290", body=_BODY)

        record_extracted_plan(store, item_id="2290", body=_EDITED)

        recorded = _events(store, PLAN_RECORDED_EVENT)
        assert {event.payload["body_digest"] for event in recorded} == {
            body_digest(_BODY),
            body_digest(_EDITED),
        }

    def test_a_middle_row_the_chain_never_got_is_recovered_by_a_later_tick(
        self, store, monkeypatch
    ):
        """The gap is two events wide: the lost row AND the supersession it
        caused, neither of which the next write would ever name."""
        record_extracted_plan(store, item_id="2290", body=_BODY)
        with monkeypatch.context() as dropped:
            self._drop_appends(dropped, store)
            record_extracted_plan(store, item_id="2290", body=_EDITED)

        record_extracted_plan(store, item_id="2290", body=_EDITED_AGAIN)

        rows = store.list_item_plans("2290")
        assert [
            event.payload["item_plan_id"]
            for event in _events(store, PLAN_RECORDED_EVENT)
        ] == [row.id for row in reversed(rows)]
        assert [
            event.payload["item_plan_id"]
            for event in _events(store, PLAN_SUPERSEDED_EVENT)
        ] == [rows[1].id, rows[0].id]
        assert verify_chain(store.read_chain_rows()).ok is True

    def test_a_recovered_supersession_names_the_run_that_caused_it(
        self, store, monkeypatch
    ):
        record_extracted_plan(store, item_id="2290", body=_BODY, run_id="run-1")
        with monkeypatch.context() as dropped:
            self._drop_appends(dropped, store)
            record_extracted_plan(store, item_id="2290", body=_EDITED, run_id="run-2")

        record_extracted_plan(store, item_id="2290", body=_EDITED, run_id="run-3")

        [superseded] = _events(store, PLAN_SUPERSEDED_EVENT)
        assert superseded.run_id == "run-2"

    def test_an_unreadable_history_still_attests_the_row_just_written(
        self, store, monkeypatch, caplog
    ):
        def _boom(*args, **kwargs):
            raise RuntimeError("history is unreadable")

        monkeypatch.setattr(store, "list_item_plans", _boom)
        with caplog.at_level(logging.WARNING):
            write = record_extracted_plan(store, item_id="2290", body=_BODY)

        assert write is not None and write.recorded is True
        [recorded] = _events(store, PLAN_RECORDED_EVENT)
        assert recorded.payload["body_digest"] == body_digest(_BODY)
        assert "plan history" in caplog.text


class TestTheEventsBindTheItemTheyDescribe:
    """#2309 P7: only ``issue_num`` enters the ``entry_hash`` preimage.

    A tracker riding the ``issue_num = 0`` sentinel identifies the item through
    ``item_id`` alone, and that column is unchained — so the item-to-plan
    binding has to ride in the payload, which IS hashed.
    """

    def test_both_events_name_the_item_and_the_row_in_the_payload(self, store):
        record_extracted_plan(store, item_id="SFD-12", body=_BODY, issue_num=0)
        record_extracted_plan(store, item_id="SFD-12", body=_EDITED, issue_num=0)
        rows = store.list_item_plans("SFD-12")

        recorded = _events(store, PLAN_RECORDED_EVENT)
        assert {
            (event.payload["item_id"], event.payload["item_plan_id"])
            for event in recorded
        } == {("SFD-12", row.id) for row in rows}
        [superseded] = _events(store, PLAN_SUPERSEDED_EVENT)
        assert superseded.payload["item_id"] == "SFD-12"
        assert superseded.payload["item_plan_id"] == rows[0].id

    def test_repointing_a_row_at_another_item_contradicts_the_chain(self, store):
        """The ledger rewrite leaves the chain intact — and detectable."""
        record_extracted_plan(store, item_id="SFD-12", body=_BODY, issue_num=0)
        record_extracted_plan(store, item_id="SFD-99", body=_EDITED, issue_num=0)
        attested = {
            event.payload["item_plan_id"]: event.payload["item_id"]
            for event in _events(store, PLAN_RECORDED_EVENT)
        }

        store._conn.execute(
            "UPDATE item_plans SET item_id ="
            " CASE item_id WHEN 'SFD-12' THEN 'SFD-99' ELSE 'SFD-12' END"
        )
        store._conn.commit()

        assert verify_chain(store.read_chain_rows()).ok is True
        swapped = {
            row.id: row.item_id
            for item in ("SFD-12", "SFD-99")
            for row in store.list_item_plans(item)
        }
        assert swapped != attested
