"""Offline model lifecycle facts.

The governed path is the ``lifecycle`` of each row in the model catalog
(``models.json``, shipped in the wheel): it is the runtime default, and
``[model_eol].snapshot_path`` overrides it with a curated registry document
only when a project sets that key explicitly.  Parsing is strict — a malformed
document fails loudly rather than degrading into a silently empty lifecycle
view.

There is no feed, no fetch, and no write path (#1490): every function here
reads committed bytes and evaluates them.  Lifecycle status itself stays
advisory and never refuses a route; only a malformed registry raises.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

REGISTRY_SCHEMA = "kew/model-lifecycle-registry@1"
# Strict calendar dates only: `date.fromisoformat` also accepts ISO week and
# ordinal forms (`2026-W33-1`, `2026-228`), which the registry contract does not.
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_REGISTRY_KEYS = frozenset({"schema", "reviewedAt", "models"})
_ROW_KEYS = frozenset(
    {"id", "provider", "status", "shutdown", "replacement", "evidence"}
)
_EVIDENCE_KEYS = frozenset({"kind", "url", "checkedAt"})
_ALLOWED_EVIDENCE_KINDS = frozenset(
    {
        "provider-lifecycle-doc",
        "provider-announcement",
        "provider-catalog",
        "router-catalog",
    }
)
_KNOWN_STATUSES = frozenset(
    {"ok", "untracked", "watch", "scheduled", "retiring", "retired"}
)
_ACTIONABLE_STATUSES = frozenset({"scheduled", "watch", "retiring", "retired"})


@dataclass(frozen=True)
class LifecycleEntry:
    """Lifecycle facts for one curated registry row."""

    status: str = "untracked"
    provider: str = ""
    shutdown: str | None = None
    replacement: str | None = None


class RegistryError(ValueError):
    """A curated lifecycle registry document is not usable as written."""


@dataclass(frozen=True)
class EvidenceRecord:
    """One reviewed source backing a registry row, checked on ``checked_at``."""

    kind: str
    url: str
    checked_at: str


@dataclass(frozen=True)
class RegistryRow:
    """One curated model's lifecycle facts and the evidence behind them."""

    id: str
    provider: str
    status: str
    shutdown: str | None
    replacement: str | None
    evidence: tuple[EvidenceRecord, ...]


@dataclass(frozen=True)
class LifecycleRegistry:
    """The curated registry: reviewed on ``reviewed_at``, sorted by model id."""

    schema: str
    reviewed_at: str
    rows: tuple[RegistryRow, ...]

    @property
    def entries(self) -> dict[str, LifecycleEntry]:
        """Registry rows as the lifecycle mapping every consumer already reads."""
        return {
            row.id: LifecycleEntry(
                status=row.status,
                provider=row.provider,
                shutdown=row.shutdown,
                replacement=row.replacement,
            )
            for row in self.rows
        }


def _require_object(value: Any, keys: frozenset[str], what: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RegistryError(f"{what} must be a JSON object")
    missing = sorted(keys - set(value))
    unexpected = sorted(set(value) - keys)
    if missing or unexpected:
        raise RegistryError(
            f"{what} has missing={missing} unexpected={unexpected} fields"
        )
    return value


def _require_text(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value:
        raise RegistryError(f"{what} must be a nonempty string")
    return value


def _require_date(value: Any, what: str) -> str:
    text = _require_text(value, what)
    try:
        if not _ISO_DATE.fullmatch(text):
            raise ValueError(text)
        date.fromisoformat(text)
    except ValueError as exc:
        raise RegistryError(f"{what} must be an ISO YYYY-MM-DD date: {text!r}") from exc
    return text


def _optional(value: Any, what: str, require: Any) -> str | None:
    return None if value is None else str(require(value, what))


def _parse_evidence(value: Any, model_id: str) -> EvidenceRecord:
    fields = _require_object(value, _EVIDENCE_KEYS, f"{model_id} evidence record")
    kind = _require_text(fields["kind"], f"{model_id} evidence kind")
    if kind not in _ALLOWED_EVIDENCE_KINDS:
        raise RegistryError(
            f"{model_id} has unknown evidence kind {kind!r}; "
            f"allowed kinds: {sorted(_ALLOWED_EVIDENCE_KINDS)}"
        )
    url = _require_text(fields["url"], f"{model_id} evidence url")
    if not url.startswith("https://"):
        raise RegistryError(f"{model_id} evidence url must be https: {url!r}")
    return EvidenceRecord(
        kind=kind,
        url=url,
        checked_at=_require_date(fields["checkedAt"], f"{model_id} evidence checkedAt"),
    )


def _parse_row(value: Any) -> RegistryRow:
    fields = _require_object(value, _ROW_KEYS, "registry model row")
    model_id = _require_text(fields["id"], "registry model id")
    status = _require_text(fields["status"], f"{model_id} status")
    if status not in _KNOWN_STATUSES:
        raise RegistryError(f"{model_id} has unknown lifecycle status {status!r}")
    evidence = fields["evidence"]
    if not isinstance(evidence, list) or not evidence:
        raise RegistryError(f"{model_id} needs a nonempty evidence list")
    return RegistryRow(
        id=model_id,
        provider=_require_text(fields["provider"], f"{model_id} provider"),
        status=status,
        shutdown=_optional(fields["shutdown"], f"{model_id} shutdown", _require_date),
        replacement=_optional(
            fields["replacement"], f"{model_id} replacement", _require_text
        ),
        evidence=tuple(_parse_evidence(item, model_id) for item in evidence),
    )


def _require_sorted_unique(ids: list[str]) -> None:
    duplicates = sorted({value for value in ids if ids.count(value) > 1})
    if duplicates:
        raise RegistryError(f"registry has duplicate model ids: {duplicates}")
    if ids != sorted(ids):
        raise RegistryError("registry model ids must be sorted by id")


def parse_registry(payload: Any) -> LifecycleRegistry:
    """Parse a curated registry document, refusing anything off-contract."""
    fields = _require_object(payload, _REGISTRY_KEYS, "lifecycle registry")
    schema = fields["schema"]
    if schema != REGISTRY_SCHEMA:
        raise RegistryError(
            f"lifecycle registry schema must be {REGISTRY_SCHEMA!r}, got {schema!r}"
        )
    models = fields["models"]
    if not isinstance(models, list):
        raise RegistryError("lifecycle registry models must be a JSON array")
    rows = tuple(_parse_row(item) for item in models)
    _require_sorted_unique([row.id for row in rows])
    reviewed_at = _require_date(fields["reviewedAt"], "registry reviewedAt")
    return LifecycleRegistry(schema=REGISTRY_SCHEMA, reviewed_at=reviewed_at, rows=rows)


def catalog_lifecycle_rows() -> tuple[RegistryRow, ...]:
    """The packaged lifecycle rows: one per catalog id and declared alias."""
    from kew.model_catalog import load_catalog

    return tuple(
        RegistryRow(
            id=model_id,
            provider=row.provider,
            status=row.lifecycle.status,
            shutdown=row.lifecycle.shutdown,
            replacement=row.lifecycle.replacement,
            evidence=tuple(
                EvidenceRecord(kind=e.kind, url=e.url, checked_at=e.checked_at)
                for e in row.lifecycle.evidence
            ),
        )
        for row in load_catalog().rows
        for model_id in (row.id, *row.aliases)
    )


def load_registry(source: Path) -> LifecycleRegistry:
    """Load the registry document a project names in ``snapshot_path``."""
    try:
        raw = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise RegistryError(f"lifecycle registry is unreadable: {source}") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RegistryError(f"lifecycle registry is not valid JSON: {source}") from exc
    try:
        return parse_registry(payload)
    except RegistryError as exc:
        raise RegistryError(f"{source}: {exc}") from exc


def _override_path(lifecycle_raw: Mapping[str, Any], config_dir: Path) -> Path | None:
    """Resolve ``snapshot_path`` only when the project set it explicitly."""
    configured = lifecycle_raw.get("snapshot_path")
    if not configured:
        return None
    path = Path(str(configured))
    return path if path.is_absolute() else config_dir / path


def merge_snapshot_into_raw(raw: dict[str, Any], config_dir: Path) -> None:
    """Hydrate lifecycle rows from the model catalog or an explicit override."""
    lifecycle_raw = raw.get("model_eol", {})
    if not isinstance(lifecycle_raw, dict):
        return
    override = _override_path(lifecycle_raw, config_dir)
    rows = (
        catalog_lifecycle_rows() if override is None else load_registry(override).rows
    )
    lifecycle_raw["snapshot_models"] = {
        row.id: {
            "status": row.status,
            "provider": row.provider,
            "shutdown": row.shutdown,
            "replacement": row.replacement,
        }
        for row in rows
    }
    raw["model_eol"] = lifecycle_raw


def _advisory_for_entry(model: str, entry: LifecycleEntry | None) -> str | None:
    if entry is None:
        return f"{model}: registry gap (no curated lifecycle row)"
    if entry.status == "untracked":
        return f"{model}: lifecycle unverified (untracked in the curated registry)"
    if entry.status not in _KNOWN_STATUSES:
        return f"{model}: registry gap (unknown status={entry.status})"
    if entry.status not in _ACTIONABLE_STATUSES:
        return None
    detail = f"status={entry.status}"
    if entry.shutdown:
        detail += f", shutdown={entry.shutdown}"
    if entry.replacement:
        detail += f", replacement={entry.replacement}"
    return f"{model}: lifecycle warning ({detail})"


def check_lifecycle(
    models: list[str], entries: Mapping[str, LifecycleEntry]
) -> list[str]:
    """Return operator-facing advisories for resolved or pinned model ids."""
    advisories: list[str] = []
    for model in sorted({model for model in models if model}):
        advisory = _advisory_for_entry(model, entries.get(model))
        if advisory:
            advisories.append(advisory)
    return advisories


def missing_registry_rows(
    models: list[str], entries: Mapping[str, LifecycleEntry]
) -> list[str]:
    """Model ids with no curated row at all — the only exit-3 condition."""
    return sorted({model for model in models if model and model not in entries})


__all__ = [
    "REGISTRY_SCHEMA",
    "EvidenceRecord",
    "LifecycleEntry",
    "LifecycleRegistry",
    "RegistryError",
    "RegistryRow",
    "catalog_lifecycle_rows",
    "check_lifecycle",
    "load_registry",
    "merge_snapshot_into_raw",
    "missing_registry_rows",
    "parse_registry",
]
