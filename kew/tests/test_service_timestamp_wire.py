"""#2205 — one wire format for the timestamps the console renders as clocks.

The console turns a timestamp into a clock reading in the viewer's own zone,
which it can only do from an instant. JavaScript reads an offset-less ISO
date-time as the *viewer's* wall clock, so a timestamp that leaves the API
without an offset is read as a different instant by every reader at a
different offset — and reads as a confident time either way.

Current writers all stamp ``datetime.now(UTC)``, so this is about the rows
that predate that: ``_iso_to_dt`` in run_store.py still carries a
backward-compat path for rows written without tz info, and those rows reach
these DTOs unchanged. The normalization is declared on the field, so it holds
for every caller rather than for the one call site that remembered.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from kew.service.models import (
    AuditEventDTO,
    EpicHeartbeatDTO,
    EpicSnapshotDTO,
    EpicSummaryDTO,
    EpicTickDTO,
)

_HEARTBEAT: dict[str, object] = {
    "last_tick_started_at": None,
    "last_tick_ts": None,
    "last_tick_duration_s": 1.0,
    "tick_count": 1,
    "interval_s": 60.0,
    "last_signal": "PROGRESSING",
    "change_count": 1,
}


def _heartbeat(**over: object) -> EpicHeartbeatDTO:
    return EpicHeartbeatDTO.model_validate(_HEARTBEAT | over)


def test_a_naive_audit_timestamp_leaves_the_api_as_utc() -> None:
    dto = AuditEventDTO(id=1, timestamp="2026-09-24T15:46:58", event="dispatch_started")
    assert dto.timestamp == "2026-09-24T15:46:58+00:00"
    # The same instant the writer meant, not the reader's wall clock.
    assert datetime.fromisoformat(dto.timestamp) == datetime(
        2026, 9, 24, 15, 46, 58, tzinfo=UTC
    )


def test_an_offset_bearing_timestamp_keeps_its_instant() -> None:
    dto = AuditEventDTO(id=1, timestamp="2026-09-24T17:46:58+02:00", event="e")
    assert dto.timestamp == "2026-09-24T15:46:58+00:00"
    dto = AuditEventDTO(id=1, timestamp="2026-09-24T15:46:58Z", event="e")
    assert dto.timestamp == "2026-09-24T15:46:58+00:00"


def test_an_unparseable_timestamp_is_passed_through_not_dropped() -> None:
    # The surfaces that render it already answer "—" for a stamp they cannot
    # read; swallowing the field would make the row render as untimed instead.
    dto = AuditEventDTO(id=1, timestamp="not-a-date", event="e")
    assert dto.timestamp == "not-a-date"


@pytest.mark.parametrize("field", ["last_tick_ts", "last_tick_started_at"])
def test_the_epic_heartbeat_states_its_tick_in_utc(field: str) -> None:
    dto = _heartbeat(**{field: "2026-09-24T15:46:58"})
    assert getattr(dto, field) == "2026-09-24T15:46:58+00:00"


def test_a_heartbeat_that_never_ticked_stays_absent() -> None:
    # None is "no tick", a different fact from a tick at the epoch.
    dto = _heartbeat()
    assert dto.last_tick_ts is None
    assert dto.last_tick_started_at is None


def test_the_epic_list_and_the_replay_journal_use_the_same_wire_format() -> None:
    summary = EpicSummaryDTO(
        epic_ref="#1",
        signal="PROGRESSING",
        counts={},
        drain_total_usd=0.0,
        last_tick_ts="2026-09-24T15:46:58",
        interval_s=None,
        change_count=0,
    )
    assert summary.last_tick_ts == "2026-09-24T15:46:58+00:00"
    snapshot = EpicSnapshotDTO(
        epic_ref="#1",
        tick_id=1,
        tick_ts="2026-09-24T15:46:58",
        signal="PROGRESSING",
        counts={},
        frontier=[],
        nodes=[],
        edges=[],
    )
    assert snapshot.tick_ts == "2026-09-24T15:46:58+00:00"
    tick = EpicTickDTO(
        id=1, tick_ts="2026-09-24T15:46:58", snapshot=snapshot, actions={}
    )
    assert tick.tick_ts == "2026-09-24T15:46:58+00:00"
