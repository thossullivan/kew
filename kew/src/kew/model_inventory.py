"""Produce the stdlib-only model-eol inventory format.

The inventory is intentionally a projection of run history, not a second
ledger. A reported model is the served-identity evidence for a run; the
requested route is used only when that evidence is absent.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from kew.run_store import RunRecord

MODEL_EOL_VERSION = "0.2.0"
_MODEL_EOL_FORMAT = "model-eol"
_SOURCES = frozenset({"reported", "requested"})


def _rfc3339(value: datetime) -> str:
    """Render a ledger timestamp in the format's UTC RFC3339 profile."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def build_model_inventory(
    records: list[RunRecord], *, generated_at: datetime | None = None
) -> dict[str, Any]:
    """Build a model-eol v0.2 inventory from run records.

    Each run contributes once to the identity it actually served when
    ``reported_model`` is present, otherwise to its requested ``model``. A
    source is retained in the inventory key so a requested identity is never
    silently presented as served evidence.
    """
    observations: dict[tuple[str, str], list[datetime]] = defaultdict(list)
    for record in records:
        reported = (record.reported_model or "").strip()
        requested = (record.model or "").strip()
        if reported:
            observations[(reported, "reported")].append(record.started_at)
        elif requested:
            observations[(requested, "requested")].append(record.started_at)

    models: list[dict[str, Any]] = []
    for (model_id, source), timestamps in sorted(
        observations.items(), key=lambda item: (item[0][0], item[0][1] != "reported")
    ):
        models.append(
            {
                "id": model_id,
                "source": source,
                "usage": {
                    "count": len(timestamps),
                    "first_seen": _rfc3339(min(timestamps)),
                    "last_seen": _rfc3339(max(timestamps)),
                },
            }
        )

    return {
        "format": _MODEL_EOL_FORMAT,
        "version": MODEL_EOL_VERSION,
        "generated_at": _rfc3339(generated_at or datetime.now(UTC)),
        "models": models,
    }


def render_model_inventory(inventory: dict[str, Any]) -> str:
    """Serialize a validated model-eol inventory as human-readable JSON."""
    parse_model_inventory(inventory)
    return json.dumps(inventory, indent=2) + "\n"


def _parse_timestamp(value: Any, field: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"model-eol {field} must be an RFC3339 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"model-eol {field} must be an RFC3339 string") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"model-eol {field} must include a timezone")


def _decode_inventory(value: str | bytes | dict[str, Any]) -> Any:
    if isinstance(value, (str, bytes)):
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("model-eol inventory is not valid JSON") from exc
    return value


def _validate_inventory_header(decoded: dict[str, Any]) -> None:
    if set(decoded) != {"format", "version", "generated_at", "models"}:
        raise ValueError("model-eol inventory has unexpected fields")
    if decoded["format"] != _MODEL_EOL_FORMAT:
        raise ValueError("model-eol inventory format must be 'model-eol'")
    if decoded["version"] != MODEL_EOL_VERSION:
        raise ValueError(f"unsupported model-eol version: {decoded['version']!r}")
    _parse_timestamp(decoded["generated_at"], "generated_at")


def _validate_model_entry(model: Any) -> None:
    if not isinstance(model, dict) or set(model) != {"id", "source", "usage"}:
        raise ValueError("model-eol model entry has unexpected fields")
    if not isinstance(model["id"], str) or not model["id"]:
        raise ValueError("model-eol model id must be a non-empty string")
    if model["source"] not in _SOURCES:
        raise ValueError("model-eol model source must be reported or requested")
    usage = model["usage"]
    if not isinstance(usage, dict) or set(usage) != {
        "count",
        "first_seen",
        "last_seen",
    }:
        raise ValueError("model-eol usage has unexpected fields")
    if type(usage["count"]) is not int or usage["count"] < 1:
        raise ValueError("model-eol usage count must be a positive integer")
    _parse_timestamp(usage["first_seen"], "usage.first_seen")
    _parse_timestamp(usage["last_seen"], "usage.last_seen")


def parse_model_inventory(value: str | bytes | dict[str, Any]) -> dict[str, Any]:
    """Parse and validate the documented model-eol v0.2 inventory shape."""
    decoded = _decode_inventory(value)
    if not isinstance(decoded, dict):
        raise ValueError("model-eol inventory must be a JSON object")
    _validate_inventory_header(decoded)
    models = decoded["models"]
    if not isinstance(models, list):
        raise ValueError("model-eol models must be an array")
    for model in models:
        _validate_model_entry(model)
    return decoded
