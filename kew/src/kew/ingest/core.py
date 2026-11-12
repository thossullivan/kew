"""Substrate-agnostic ingest core (#256): document -> store, shared by the
file command and the receiver."""

from __future__ import annotations

import json
from dataclasses import dataclass

from kew.ingest.claude_code import assemble
from kew.ingest.otlp import walk_metrics
from kew.run_store import RunStore, TrackerEvent


@dataclass
class IngestSummary:
    ingested: int
    deduped: int
    skipped: int


def ingest_document(
    doc: object, *, ingest_source: str, store: RunStore
) -> IngestSummary:
    result = assemble(walk_metrics(doc), ingest_source=ingest_source)
    ingested = 0
    deduped = 0
    for record in result.records:
        if store.record_interactive_usage(record):
            ingested += 1
        else:
            deduped += 1
    return IngestSummary(ingested=ingested, deduped=deduped, skipped=result.skipped)


# Linear webhook entity types this receiver normalizes (W1/#893). Other event
# types are acknowledged and ignored so Linear does not retry / disable them.
SUPPORTED_TRACKER_EVENTS = frozenset({"Issue", "Comment", "Project"})


@dataclass
class TrackerIngestResult:
    """Outcome of parsing one tracker webhook delivery."""

    status: str  # "recorded" | "deduped" | "ignored"
    event_type: str | None


def ingest_tracker_event(
    doc: object, *, delivery_id: str, source: str, store: RunStore
) -> TrackerIngestResult:
    """Normalize one tracker webhook payload into tracker_events (W1/#893).

    Raises ValueError on a structurally malformed body (not a JSON object, or
    missing a string ``type``); the caller maps that to 400. A well-formed but
    unsupported event type is acknowledged as ``ignored`` (no persistence),
    since a non-2xx would make the tracker retry and eventually disable us.
    """
    if not isinstance(doc, dict):
        raise ValueError("tracker webhook body must be a JSON object")
    event_type = doc.get("type")
    if not isinstance(event_type, str):
        raise ValueError("tracker webhook missing string 'type'")
    if event_type not in SUPPORTED_TRACKER_EVENTS:
        return TrackerIngestResult(status="ignored", event_type=event_type)

    action = doc.get("action")
    data = doc.get("data")
    entity_id = data.get("id") if isinstance(data, dict) else None
    webhook_ts = doc.get("webhookTimestamp")
    event = TrackerEvent(
        delivery_id=delivery_id,
        source=source,
        event_type=event_type,
        action=action if isinstance(action, str) else None,
        entity_id=entity_id if isinstance(entity_id, str) else None,
        webhook_timestamp=(
            webhook_ts
            if isinstance(webhook_ts, int) and not isinstance(webhook_ts, bool)
            else None
        ),
        payload=json.dumps(doc, separators=(",", ":"), sort_keys=True),
    )
    recorded = store.record_tracker_event(event)
    return TrackerIngestResult(
        status="recorded" if recorded else "deduped", event_type=event_type
    )
