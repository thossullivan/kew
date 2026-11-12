"""The reviewer's model as a console override (#2741).

``review.model`` decides which model judges agent PRs, so the
``/api/config/review.model`` write is held to the catalog: a routable,
unretired catalog id on the reviewer's runner is attested and stored; anything
else is refused before an audit row or an override row is written. And the
verdict a governed review attests names the model the reviewer ran on, not
whatever the config says by the time the verdict lands.
"""

from __future__ import annotations

import copy
import json
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import kew.model_catalog as model_catalog
from kew.config import DispatchConfig, load_config
from kew.graph.driver.review_stage import _reviewer_model_used
from kew.model_catalog import PACKAGED_CATALOG_PATH, parse_catalog
from kew.run_store import RunRecord, SqliteRunStore
from kew.serve.app import create_app
from kew.serve.auth import SessionStore

FIELD = "review.model"
ROUTABLE = "claude-sonnet-5-5"
RETIRED = "claude-opus-4-8"


@contextmanager
def _store_factory(db_path: str) -> Any:
    store = SqliteRunStore(db_path)
    try:
        yield store
    finally:
        store.close()


@pytest.fixture
def app_db(tmp_path: Path, inbox_client: Any) -> tuple[Any, str]:
    db = str(tmp_path / "kew.db")
    SqliteRunStore(db).close()
    app = create_app(
        lambda: _store_factory(db),
        repo_dir=tmp_path,
        config=load_config(),
        token="TESTTOKEN",
        sessions=SessionStore(),
        bind_host="127.0.0.1",
        bind_port=8765,
        expose=False,
    )
    return inbox_client(app, loopback_peer=True), db


def _login(client: Any) -> dict[str, str]:
    r = client.post("/api/auth/login", json={"token": "TESTTOKEN"})
    assert r.status_code == 200
    return {"X-Kew-CSRF": r.json()["csrf_token"]}


def _written(db: str) -> tuple[list[Any], Any]:
    with _store_factory(db) as store:
        events = store.list_audit_events(event="config_override_set", limit=None)
        return events, store.get_config_override(FIELD)


def test_a_catalog_id_is_accepted_audited_and_in_force(app_db: Any) -> None:
    client, db = app_db
    r = client.post(
        f"/api/config/{FIELD}",
        json={"value": ROUTABLE, "reason": "sonnet reviews this week"},
        headers=_login(client),
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"field": FIELD, "effective": ROUTABLE}

    events, override = _written(db)
    assert len(events) == 1
    payload = events[0].payload
    assert payload["field"] == FIELD
    assert payload["new_value"] == ROUTABLE
    assert payload["reason"] == "sonnet reviews this week"
    assert payload["source"] == "web"
    assert override is not None
    assert override["audit_event_id"] == events[0].id
    # The override is what the reviewer resolves from now on.
    with _store_factory(db) as store:
        assert load_config(store=store).review.model == ROUTABLE


def _retire(monkeypatch: pytest.MonkeyPatch, model_id: str) -> None:
    payload = copy.deepcopy(json.loads(PACKAGED_CATALOG_PATH.read_text("utf-8")))
    for row in payload["models"]:
        if row["id"] == model_id:
            row["lifecycle"]["status"] = "retired"
            row["lifecycle"]["shutdown"] = "2026-01-05"
            row["lifecycle"]["replacement"] = "claude-opus-5-5"
    catalog = parse_catalog(payload)
    monkeypatch.setattr(model_catalog, "load_catalog", lambda: catalog)


@pytest.mark.parametrize(
    ("value", "why"),
    [
        ("claude-nonesuch-9", "not a model id in the catalog"),
        (RETIRED, "retired"),
        # In the catalog, but the router may not emit it.
        ("claude-opus-4-7", "not routable"),
        # Routable, but on a runner the reviewer does not run.
        ("gpt-5.6-sol", "runs on codex"),
    ],
)
def test_anything_else_is_refused_before_a_row_is_written(
    app_db: Any, monkeypatch: pytest.MonkeyPatch, value: str, why: str
) -> None:
    _retire(monkeypatch, RETIRED)
    client, db = app_db
    r = client.post(
        f"/api/config/{FIELD}", json={"value": value}, headers=_login(client)
    )
    assert r.status_code == 400
    assert why in r.json()["detail"]
    events, override = _written(db)
    assert events == []
    assert override is None


def test_the_write_requires_the_operator_write_auth(app_db: Any) -> None:
    client, db = app_db
    r = client.post(f"/api/config/{FIELD}", json={"value": ROUTABLE})
    assert r.status_code in (401, 403)
    assert _written(db) == ([], None)


def test_a_stored_override_survives_its_row_retiring(
    app_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the write reads the catalog: a later retirement must not silently
    drop the override and move the reviewer onto another model."""
    client, db = app_db
    r = client.post(
        f"/api/config/{FIELD}", json={"value": ROUTABLE}, headers=_login(client)
    )
    assert r.status_code == 200
    _retire(monkeypatch, ROUTABLE)
    with _store_factory(db) as store:
        assert load_config(store=store).review.model == ROUTABLE


def _review_row(store: SqliteRunStore, run_id: str, model: str) -> None:
    now = datetime.now(UTC)
    store.save(
        RunRecord(
            run_id=run_id,
            issue_num=7,
            branch="kew/7",
            status="success",
            exit_code=0,
            started_at=now,
            finished_at=now,
            model=model,
        )
    )


def test_the_verdict_names_the_model_the_reviewer_ran_on(tmp_path: Path) -> None:
    """The override can change while a review runs; the run row is the record."""
    config = DispatchConfig()
    config.review.model = "claude-opus-5-5"  # set after the reviewer launched
    with SqliteRunStore(str(tmp_path / "kew.db")) as store:
        _review_row(store, "rv-1", "claude-sonnet-5-5")
        assert _reviewer_model_used(store, "rv-1", config) == "claude-sonnet-5-5"
        # No row (the raise landed before launch): the launcher's resolution,
        # which falls back to the agent model rather than reading as unset.
        config.review.model = ""
        config.agent.model = "claude-opus-5"
        assert _reviewer_model_used(store, "rv-missing", config) == "claude-opus-5"
        assert _reviewer_model_used(store, None, DispatchConfig()) is None
