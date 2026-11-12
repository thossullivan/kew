"""Tests for the model-eol inventory producer."""

import json
from datetime import UTC, datetime

from kew.model_inventory import (
    MODEL_EOL_VERSION,
    build_model_inventory,
    parse_model_inventory,
    render_model_inventory,
)
from kew.run_store import RunRecord


def _record(
    run_id: str,
    model: str,
    started_at: datetime,
    *,
    reported_model: str | None = None,
) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        issue_num=1,
        branch="agent/issue-1",
        status="success",
        exit_code=0,
        started_at=started_at,
        model=model,
        reported_model=reported_model,
    )


def test_inventory_uses_reported_identity_and_marks_requested_fallback() -> None:
    records = [
        _record(
            "reported",
            "route/sonnet",
            datetime(2026, 8, 1, 10, tzinfo=UTC),
            reported_model="claude-sonnet-4-6",
        ),
        _record(
            "fallback",
            "claude-opus-4-8",
            datetime(2026, 8, 2, 11, tzinfo=UTC),
        ),
    ]

    inventory = build_model_inventory(
        records, generated_at=datetime(2026, 8, 3, 12, tzinfo=UTC)
    )

    assert inventory == {
        "format": "model-eol",
        "version": MODEL_EOL_VERSION,
        "generated_at": "2026-08-03T12:00:00Z",
        "models": [
            {
                "id": "claude-opus-4-8",
                "source": "requested",
                "usage": {
                    "count": 1,
                    "first_seen": "2026-08-02T11:00:00Z",
                    "last_seen": "2026-08-02T11:00:00Z",
                },
            },
            {
                "id": "claude-sonnet-4-6",
                "source": "reported",
                "usage": {
                    "count": 1,
                    "first_seen": "2026-08-01T10:00:00Z",
                    "last_seen": "2026-08-01T10:00:00Z",
                },
            },
        ],
    }


def test_inventory_aggregates_usage_by_identity_and_source() -> None:
    records = [
        _record("one", "route/sonnet", datetime(2026, 8, 1, tzinfo=UTC)),
        _record("two", "route/sonnet", datetime(2026, 8, 3, tzinfo=UTC)),
        _record(
            "three",
            "route/sonnet",
            datetime(2026, 8, 4, tzinfo=UTC),
            reported_model="claude-sonnet-4-6",
        ),
    ]

    models = build_model_inventory(records)["models"]

    requested = next(model for model in models if model["source"] == "requested")
    reported = next(model for model in models if model["source"] == "reported")
    assert requested["usage"] == {
        "count": 2,
        "first_seen": "2026-08-01T00:00:00Z",
        "last_seen": "2026-08-03T00:00:00Z",
    }
    assert reported["usage"]["count"] == 1


def test_inventory_round_trips_through_documented_parser() -> None:
    encoded = render_model_inventory(
        build_model_inventory(
            [_record("one", "gpt-5", datetime(2026, 8, 1, tzinfo=UTC))],
            generated_at=datetime(2026, 8, 3, tzinfo=UTC),
        )
    )

    assert parse_model_inventory(encoded) == json.loads(encoded)
