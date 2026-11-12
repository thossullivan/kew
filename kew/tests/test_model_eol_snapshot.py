"""Curated lifecycle registry parsing, hydration, and advisory warnings.

The model-eol feed is gone (#1490). What remains is a committed, strictly
parsed registry: reading it touches no socket and writes no byte, and only a
malformed document raises — lifecycle status itself is always advisory.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from kew import model_eol_snapshot
from kew.config import ConfigError, DispatchConfig, ModelEolConfig, load_config
from kew.dispatch_core import _preflight_pricing
from kew.model_catalog import load_catalog
from kew.model_eol_snapshot import (
    REGISTRY_SCHEMA,
    EvidenceRecord,
    LifecycleEntry,
    RegistryError,
    catalog_lifecycle_rows,
    check_lifecycle,
    parse_registry,
)
from kew.runners import resolve_runner

MODEL = "claude-opus-5"

# The routed, reviewer, and priced ids the curated registry covers (eleven
# since the 5.5 generation joined on 2026-10-08, #2531).
# `claude-haiku-4-5` is the disabled classifier model and is deliberately out.
# `gpt-5.6-sol` joined on 2026-09-01 when every delivery lane moved to it
# (#1796); the site build refuses a selected model with no curated row.


def _catalog_ids() -> tuple[str, ...]:
    """Every catalog id and declared alias: the packaged lifecycle rows."""
    return tuple(
        model_id for row in load_catalog().rows for model_id in (row.id, *row.aliases)
    )


# Every fetch, digest, staleness, and write helper deleted with the feed.
REMOVED_SYMBOLS = (
    "DEFAULT_FEED_URL",
    "DEFAULT_WARN_AGE_DAYS",
    "Snapshot",
    "fetch_feed",
    "load_snapshot",
    "parse_feed",
    "render_snapshot",
    "snapshot_age_days",
    "sync_models",
    "_check_staleness",
    "_digest_payload",
    "_feed_models",
)

# Tokens that would mean a network client, a writer, or a hasher came back.
BANNED_TOKENS = (
    "urllib",
    "urlopen",
    "httpx",
    "requests",
    "socket",
    "hashlib",
    "import warnings",
    "warnings.warn",
    "write_text",
    "write_bytes",
    "mkdir",
)


def _registry_payload(**overrides: Any) -> dict[str, Any]:
    """A minimal, valid curated-registry document."""
    payload: dict[str, Any] = {
        "schema": REGISTRY_SCHEMA,
        "reviewedAt": "2026-08-16",
        "models": [
            {
                "id": MODEL,
                "provider": "anthropic",
                "status": "ok",
                "shutdown": None,
                "replacement": None,
                "evidence": [
                    {
                        "kind": "provider-lifecycle-doc",
                        "url": "https://docs.example.invalid/deprecations",
                        "checkedAt": "2026-08-16",
                    }
                ],
            }
        ],
    }
    payload.update(overrides)
    return payload


def _row(payload: dict[str, Any]) -> dict[str, Any]:
    row: dict[str, Any] = payload["models"][0]
    return row


def _evidence(payload: dict[str, Any]) -> dict[str, Any]:
    record: dict[str, Any] = _row(payload)["evidence"][0]
    return record


def _extra_row(model_id: str, **overrides: Any) -> dict[str, Any]:
    row = dict(_row(_registry_payload()))
    row["id"] = model_id
    row["evidence"] = [dict(record) for record in row["evidence"]]
    row.update(overrides)
    return row


def _write_registry(directory: Path, payload: dict[str, Any]) -> Path:
    """A strict temporary registry fixture, replacing the deleted feed fixture."""
    path = directory / "lifecycle.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# The removed feed surface
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", REMOVED_SYMBOLS)
def test_feed_fetch_digest_and_staleness_helpers_are_gone(name: str) -> None:
    assert not hasattr(model_eol_snapshot, name)


@pytest.mark.parametrize(
    "field", ["feed_url", "warn_age_days", "snapshot_captured_at", "snapshot_sha256"]
)
def test_deleted_lifecycle_config_fields_are_rejected(field: str) -> None:
    assert field not in ModelEolConfig.model_fields

    with pytest.raises(ValueError):
        ModelEolConfig(**{field: "anything"})


def test_registry_loading_has_no_network_or_write_path(tmp_path: Path) -> None:
    """Loading and hydrating the registry opens no socket and writes no file."""
    source = Path(model_eol_snapshot.__file__).read_text(encoding="utf-8")
    for token in BANNED_TOKENS:
        assert token not in source, f"{token!r} is back on the lifecycle path"

    config_path = tmp_path / "kew.toml"
    config_path.write_text(f'[agent]\nmodel = "{MODEL}"\n', encoding="utf-8")
    before = sorted(path.name for path in tmp_path.iterdir())

    with (
        patch("socket.socket", side_effect=AssertionError("network")),
        patch("urllib.request.urlopen", side_effect=AssertionError("network")),
        patch("pathlib.Path.write_text", side_effect=AssertionError("write")),
        patch("pathlib.Path.write_bytes", side_effect=AssertionError("write")),
    ):
        rows = catalog_lifecycle_rows()
        config = load_config(config_path)

    assert rows
    assert config.model_eol.snapshot_models[MODEL].status == "ok"
    assert sorted(path.name for path in tmp_path.iterdir()) == before


# ---------------------------------------------------------------------------
# Default registry, explicit override
# ---------------------------------------------------------------------------


def test_packaged_registry_is_the_default(tmp_path: Path) -> None:
    """The wheel-shipped catalog — not a file beside kew.toml — is the source."""
    _write_registry(tmp_path, _registry_payload(models=[_extra_row("vendor/ignored")]))
    config_path = tmp_path / "kew.toml"
    config_path.write_text(f'[agent]\nmodel = "{MODEL}"\n', encoding="utf-8")

    config = load_config(config_path)

    assert tuple(config.model_eol.snapshot_models) == _catalog_ids()
    assert config.model_eol.snapshot_models[MODEL].status == "ok"


def test_explicit_snapshot_path_overrides_the_packaged_registry(
    tmp_path: Path,
) -> None:
    _write_registry(tmp_path, _registry_payload(models=[_extra_row("vendor/model-x")]))
    config_path = tmp_path / "kew.toml"
    config_path.write_text(
        '[model_eol]\nsnapshot_path = "lifecycle.json"\n', encoding="utf-8"
    )

    config = load_config(config_path)

    assert list(config.model_eol.snapshot_models) == ["vendor/model-x"]


def test_absolute_snapshot_path_overrides_the_packaged_registry(
    tmp_path: Path,
) -> None:
    override = _write_registry(
        tmp_path, _registry_payload(models=[_extra_row("vendor/absolute")])
    )
    config_path = tmp_path / "kew.toml"
    config_path.write_text(
        f'[model_eol]\nsnapshot_path = "{override}"\n', encoding="utf-8"
    )

    config = load_config(config_path)

    assert list(config.model_eol.snapshot_models) == ["vendor/absolute"]


def test_explicit_registry_that_is_missing_fails_loudly(tmp_path: Path) -> None:
    config_path = tmp_path / "kew.toml"
    config_path.write_text(
        '[model_eol]\nsnapshot_path = "nope.json"\n', encoding="utf-8"
    )

    # The registry failure is now the config-error envelope (W2.3) with the
    # RegistryError chained, so the CLI boundary renders it instead of a
    # traceback; the loud failure is unchanged.
    with pytest.raises(ConfigError) as excinfo:
        load_config(config_path)
    assert excinfo.value.kind == "snapshot"
    assert isinstance(excinfo.value.__cause__, RegistryError)
    assert "unreadable" in str(excinfo.value.__cause__)


def test_explicit_registry_that_is_malformed_fails_loudly(tmp_path: Path) -> None:
    _write_registry(tmp_path, _registry_payload(reviewedAt="whenever"))
    config_path = tmp_path / "kew.toml"
    config_path.write_text(
        '[model_eol]\nsnapshot_path = "lifecycle.json"\n', encoding="utf-8"
    )

    with pytest.raises(ConfigError) as excinfo:
        load_config(config_path)
    assert excinfo.value.kind == "snapshot"
    assert isinstance(excinfo.value.__cause__, RegistryError)
    assert "reviewedAt" in str(excinfo.value.__cause__)
    assert "reviewedAt" in excinfo.value.fix  # the reason reaches the operator


# ---------------------------------------------------------------------------
# Strict registry parsing
# ---------------------------------------------------------------------------


def test_lifecycle_rows_are_the_catalog_rows() -> None:
    """One lifecycle row per catalog row: a new row needs no lifecycle edit."""
    rows = catalog_lifecycle_rows()
    catalog = {row.id: row for row in load_catalog().rows}

    assert tuple(row.id for row in rows) == _catalog_ids()
    for row in rows:
        source = catalog[row.id]
        assert row.provider == source.provider
        assert (row.status, row.shutdown, row.replacement) == (
            source.lifecycle.status,
            source.lifecycle.shutdown,
            source.lifecycle.replacement,
        )
        assert all(isinstance(record, EvidenceRecord) for record in row.evidence)
        assert all(record.url.startswith("https://") for record in row.evidence)
        if row.status == "untracked":
            # Router/catalog evidence proves availability, never lifecycle.
            assert row.shutdown is None and row.replacement is None


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda p: p.pop("reviewedAt"), id="missing-reviewedAt"),
        pytest.param(
            lambda p: p.update(schema="kew/model-lifecycle-registry@2"),
            id="wrong-schema",
        ),
        pytest.param(
            lambda p: p.update(package="model-eol@0.2.3"), id="extra-top-level-field"
        ),
        pytest.param(lambda p: p.update(reviewedAt="16-08-2026"), id="non-iso-review"),
        pytest.param(
            lambda p: p.update(reviewedAt="2026-08-16T00:00:00Z"), id="review-timestamp"
        ),
        pytest.param(lambda p: p.update(reviewedAt="2026-W33-1"), id="iso-week-date"),
        pytest.param(lambda p: p.update(reviewedAt="2026-228"), id="iso-ordinal-date"),
        pytest.param(lambda p: p.update(models={}), id="models-not-a-list"),
        pytest.param(lambda p: p.update(models=[None]), id="row-not-an-object"),
        pytest.param(lambda p: _row(p).update(usage="gateway"), id="row-extra-usage"),
        pytest.param(lambda p: _row(p).pop("replacement"), id="row-missing-field"),
        pytest.param(
            lambda p: _row(p).update(status="deprecated"), id="unknown-status"
        ),
        pytest.param(lambda p: _row(p).update(status=None), id="status-wrong-type"),
        pytest.param(lambda p: _row(p).update(provider=7), id="provider-wrong-type"),
        pytest.param(lambda p: _row(p).update(id=""), id="empty-model-id"),
        pytest.param(lambda p: _row(p).update(shutdown="soon"), id="shutdown-not-date"),
        pytest.param(
            lambda p: _row(p).update(shutdown="2026-W33-1"), id="shutdown-week-date"
        ),
        pytest.param(
            lambda p: _row(p).update(replacement=3), id="replacement-wrong-type"
        ),
        pytest.param(lambda p: _row(p).update(evidence=[]), id="empty-evidence"),
        pytest.param(lambda p: _row(p).update(evidence={}), id="evidence-not-a-list"),
        pytest.param(
            lambda p: _evidence(p).update(url="http://insecure.invalid/x"),
            id="evidence-not-https",
        ),
        pytest.param(
            lambda p: _evidence(p).update(checkedAt="2026-8-16"),
            id="evidence-non-iso-checkedAt",
        ),
        pytest.param(
            lambda p: _evidence(p).update(checkedAt="2026-W33-1"),
            id="evidence-week-date",
        ),
        pytest.param(
            lambda p: _evidence(p).update(note="hand-checked"),
            id="evidence-extra-field",
        ),
        pytest.param(lambda p: _evidence(p).pop("kind"), id="evidence-missing-field"),
        pytest.param(
            lambda p: _evidence(p).update(kind="provider-lifecycle"),
            id="unknown-evidence-kind",
        ),
        pytest.param(
            lambda p: p["models"].append(_extra_row(MODEL)), id="duplicate-model-id"
        ),
        pytest.param(
            lambda p: p["models"].insert(0, _extra_row("zz-vendor/model")),
            id="unsorted-model-ids",
        ),
    ],
)
def test_registry_rejects_schema_date_https_and_extra_field_violations(
    mutate: Any,
) -> None:
    payload = _registry_payload()
    mutate(payload)

    with pytest.raises(RegistryError):
        parse_registry(payload)


def test_registry_document_must_be_a_json_object() -> None:
    with pytest.raises(RegistryError):
        parse_registry(["not", "an", "object"])


@pytest.mark.parametrize(
    "kind",
    [
        "provider-lifecycle-doc",
        "provider-announcement",
        "provider-catalog",
        "router-catalog",
    ],
)
def test_registry_accepts_allowed_evidence_kinds(kind: str) -> None:
    payload = _registry_payload()
    _evidence(payload)["kind"] = kind

    registry = parse_registry(payload)

    assert registry.rows[0].evidence[0].kind == kind


def test_registry_does_not_constrain_evidence_order_or_uniqueness() -> None:
    record = dict(_evidence(_registry_payload()))
    later = {**record, "kind": "provider-catalog", "checkedAt": "2026-08-01"}
    payload = _registry_payload(models=[_extra_row(MODEL, evidence=[record, later])])

    registry = parse_registry(payload)

    assert [item.checked_at for item in registry.rows[0].evidence] == [
        "2026-08-16",
        "2026-08-01",
    ]


# ---------------------------------------------------------------------------
# Advisory evaluation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["scheduled", "watch", "retiring", "retired"])
def test_actionable_lifecycle_status_warns(status: str) -> None:
    warnings = check_lifecycle([MODEL], {MODEL: LifecycleEntry(status=status)})

    assert len(warnings) == 1
    assert status in warnings[0]


def test_model_without_a_row_is_a_registry_gap_not_an_error() -> None:
    warnings = check_lifecycle(["unknown-model"], {})

    assert warnings == ["unknown-model: registry gap (no curated lifecycle row)"]


def test_ok_model_is_quiet() -> None:
    assert check_lifecycle([MODEL], {MODEL: LifecycleEntry(status="ok")}) == []


def test_registry_entries_preserve_advisory_lifecycle_semantics() -> None:
    payload = _registry_payload(
        models=[
            _extra_row(
                "vendor/retiring-model",
                status="retiring",
                shutdown="2026-12-01",
                replacement="vendor/next-model",
            )
        ]
    )

    warnings = check_lifecycle(
        ["vendor/retiring-model"], parse_registry(payload).entries
    )

    assert warnings == [
        "vendor/retiring-model: lifecycle warning (status=retiring, "
        "shutdown=2026-12-01, replacement=vendor/next-model)"
    ]


def test_untracked_registry_row_is_unverified_not_a_lifecycle_claim() -> None:
    entries = {
        row.id: LifecycleEntry(status=row.status, provider=row.provider)
        for row in catalog_lifecycle_rows()
    }

    warnings = check_lifecycle(["openrouter/z-ai/glm-5.2"], entries)

    assert warnings == [
        "openrouter/z-ai/glm-5.2: lifecycle unverified (untracked in the "
        "curated registry)"
    ]


def test_dispatch_preflight_warns_without_refusing() -> None:
    from types import SimpleNamespace

    config = DispatchConfig()
    config.agent.model = MODEL
    config.model_eol.snapshot_models = {
        MODEL: LifecycleEntry(status="retired", shutdown="2026-07-01")
    }
    ctx = SimpleNamespace(config=config, model_override=None)

    with patch("kew.dispatch_core._warn") as warn:
        _preflight_pricing(ctx, resolve_runner(config))

    assert any("retired" in call.args[0] for call in warn.call_args_list)
