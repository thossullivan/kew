import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from kew.ingest.core import (
    IngestSummary,
    TrackerIngestResult,
    ingest_document,
    ingest_tracker_event,
)
from kew.run_store import SqliteRunStore


def _fixture():
    return json.loads(
        (Path(__file__).parent / "fixtures" / "claude_code_otel.json").read_text()
    )


def test_ingest_document_writes_and_dedups(tmp_path):
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        first = ingest_document(_fixture(), ingest_source="file:f", store=store)
        assert isinstance(first, IngestSummary)
        assert first.ingested == 3
        assert first.deduped == 0
        # second ingest of the same export dedups
        second = ingest_document(_fixture(), ingest_source="file:f", store=store)
        assert second.ingested == 0
        assert second.deduped == 3
        rows = store.list_interactive_usage_since(datetime(2024, 1, 1, tzinfo=UTC))
        assert len(rows) == 3


def _linear_issue(delivery_action="create"):
    return {
        "action": delivery_action,
        "type": "Issue",
        "data": {"id": "issue-abc", "title": "Ship W1"},
        "webhookTimestamp": 1_700_000_000_000,
    }


def test_ingest_tracker_event_records_and_dedups(tmp_path):
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        first = ingest_tracker_event(
            _linear_issue(), delivery_id="d1", source="linear", store=store
        )
        assert isinstance(first, TrackerIngestResult)
        assert first.status == "recorded"
        assert first.event_type == "Issue"
        # same delivery id → deduped, no second row
        second = ingest_tracker_event(
            _linear_issue(), delivery_id="d1", source="linear", store=store
        )
        assert second.status == "deduped"

        rows = store.list_tracker_events_since(datetime(2024, 1, 1, tzinfo=UTC))
        assert len(rows) == 1
        row = rows[0]
        assert row.source == "linear"
        assert row.event_type == "Issue"
        assert row.action == "create"
        assert row.entity_id == "issue-abc"
        assert row.webhook_timestamp == 1_700_000_000_000
        assert json.loads(row.payload)["data"]["title"] == "Ship W1"
        assert row.received_at is not None


def test_ingest_tracker_event_parses_comment_and_project(tmp_path):
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        for i, etype in enumerate(("Comment", "Project")):
            doc = {"action": "update", "type": etype, "data": {"id": f"e{i}"}}
            res = ingest_tracker_event(
                doc, delivery_id=f"d{i}", source="linear", store=store
            )
            assert res.status == "recorded"
            assert res.event_type == etype
        assert (
            len(store.list_tracker_events_since(datetime(2024, 1, 1, tzinfo=UTC))) == 2
        )


def test_ingest_tracker_event_ignores_unsupported_type(tmp_path):
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        res = ingest_tracker_event(
            {"type": "Cycle", "action": "create", "data": {"id": "x"}},
            delivery_id="d1",
            source="linear",
            store=store,
        )
        assert res.status == "ignored"
        assert res.event_type == "Cycle"
        assert store.list_tracker_events_since(datetime(2024, 1, 1, tzinfo=UTC)) == []


def test_ingest_tracker_event_rejects_malformed_body(tmp_path):
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        with pytest.raises(ValueError):
            ingest_tracker_event(
                ["not", "a", "dict"], delivery_id="d1", source="linear", store=store
            )
        with pytest.raises(ValueError):
            ingest_tracker_event(
                {"action": "create"}, delivery_id="d1", source="linear", store=store
            )
