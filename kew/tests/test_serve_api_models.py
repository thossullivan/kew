"""``GET /api/models`` serves the model catalog to the console (#2542)."""

from __future__ import annotations

import copy
import json
from contextlib import contextmanager
from pathlib import Path

import pytest

from kew.config import DispatchConfig, RouteEntry, RoutingConfig, load_config
from kew.model_catalog import PACKAGED_CATALOG_PATH, load_catalog, parse_catalog
from kew.run_store import SqliteRunStore
from kew.runners.registry import resolve_runner
from kew.serve import api_models
from kew.serve.app import create_app
from kew.serve.auth import SessionStore

RETIRED = "claude-opus-4"


@contextmanager
def _store_factory(db_path: str):
    store = SqliteRunStore(db_path)
    try:
        yield store
    finally:
        store.close()


def _catalog_with_retired_row():
    """The packaged catalog with one row marked retired."""
    payload = json.loads(PACKAGED_CATALOG_PATH.read_text(encoding="utf-8"))
    payload = copy.deepcopy(payload)
    for row in payload["models"]:
        if row["id"] == RETIRED:
            row["lifecycle"]["status"] = "retired"
            row["lifecycle"]["shutdown"] = "2026-01-05"
            row["lifecycle"]["replacement"] = "claude-opus-5-5"
    return parse_catalog(payload)


@pytest.fixture
def client(tmp_path: Path, inbox_client, monkeypatch: pytest.MonkeyPatch):
    db = str(tmp_path / "kew.db")
    SqliteRunStore(db).close()
    catalog = _catalog_with_retired_row()
    monkeypatch.setattr(api_models, "load_catalog", lambda: catalog)
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
    return inbox_client(app, loopback_peer=True), catalog


def _ids(response) -> list[str]:
    assert response.status_code == 200
    return [row["id"] for row in response.json()["models"]]


def test_every_live_catalog_row_is_served_and_the_retired_one_is_not(client):
    c, catalog = client
    ids = _ids(c.get("/api/models"))
    assert RETIRED not in ids
    assert ids == [row.id for row in catalog.rows if row.id != RETIRED]


def test_the_packaged_catalog_is_served_whole(tmp_path: Path, inbox_client):
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
    )
    ids = _ids(inbox_client(app, loopback_peer=True).get("/api/models"))
    catalog = load_catalog()
    assert ids == [row.id for row in catalog.rows if row.lifecycle.status != "retired"]


def test_a_row_carries_what_the_picker_shows(client):
    c, _ = client
    rows = {row["id"]: row for row in c.get("/api/models").json()["models"]}
    opus = rows["claude-opus-5-5"]
    assert opus["display_name"] == "Opus 5.5"
    assert opus["runner"] == "claude-code"
    assert set(opus["price"]) == {"input_per_mtok", "output_per_mtok"}
    assert opus["lifecycle"]["status"] == "ok"
    assert opus["configured"] is False
    default = [row["id"] for row in rows.values() if "default" in row["default_lanes"]]
    assert default == [load_catalog().lane_default("default")]


def test_a_retired_model_still_configured_is_served_and_flagged(client):
    c, _ = client
    login = c.post("/api/auth/login", json={"token": "TESTTOKEN"})
    headers = {"X-Kew-CSRF": login.json()["csrf_token"]}
    written = c.post(
        "/api/config/agent.model",
        json={"value": RETIRED, "reason": "pinned"},
        headers=headers,
    )
    assert written.status_code == 200
    rows = {row["id"]: row for row in c.get("/api/models").json()["models"]}
    assert rows[RETIRED]["configured"] is True
    assert rows[RETIRED]["lifecycle"]["status"] == "retired"
    assert rows[RETIRED]["lifecycle"]["replacement"] == "claude-opus-5-5"


def test_configured_names_cover_agent_routing_and_reviewer():
    config = DispatchConfig(
        routing=RoutingConfig(
            default=RouteEntry(runner="claude-code", model="claude-sonnet-5-5"),
            simple="codex",
            complex=RouteEntry(runner="claude-code", model="claude-opus-5"),
        )
    )
    config.agent.model = "claude-fable-5"
    config.review.model = "claude-haiku-4-5"
    assert api_models.configured_model_names(config) == {
        "claude-sonnet-5-5",
        "claude-opus-5",
        "claude-fable-5",
        "claude-haiku-4-5",
    }


def test_a_retired_model_named_by_routing_or_reviewer_is_kept():
    catalog = _catalog_with_retired_row()
    for configured in ({RETIRED}, set()):
        ids = [
            row["id"]
            for row in api_models.models_payload(catalog, configured)["models"]
        ]
        assert (RETIRED in ids) is bool(configured)


def test_effective_lanes_follow_routing_fallbacks():
    config = DispatchConfig(
        routing=RoutingConfig(
            default=RouteEntry(runner="claude-code", model="claude-opus-5-5"),
            simple="codex",
            complex=RouteEntry(runner="claude-code", model="claude-sonnet-4-6"),
        )
    )
    config.agent.model = "claude-haiku-4-5"
    assert api_models.effective_lane_models(config) == {
        "default": "claude-opus-5-5",
        # A runner-only route inherits agent.model, as resolve_runner does.
        "simple": "claude-haiku-4-5",
        "medium": "claude-opus-5-5",
        "complex": "claude-sonnet-4-6",
    }


def _lane_matrix_config(routing: RoutingConfig | None, agent_model: str):
    config = DispatchConfig(routing=routing)
    config.agent.model = agent_model
    return config


_ROUTINGS = {
    "no-routing": None,
    "default-only": RoutingConfig(
        default=RouteEntry(runner="claude-code", model="claude-opus-5-5")
    ),
    "runner-only-tier": RoutingConfig(
        default=RouteEntry(runner="claude-code", model="claude-opus-5-5"),
        simple="codex",
    ),
    "tier-with-own-model": RoutingConfig(
        default=RouteEntry(runner="claude-code", model="claude-opus-5-5"),
        complex=RouteEntry(runner="claude-code", model="claude-sonnet-4-6"),
    ),
    "runner-only-default": RoutingConfig(default="codex"),
    "model-less-entry": RoutingConfig(
        default=RouteEntry(runner="claude-code"),
        medium=RouteEntry(model="claude-sonnet-4-6"),
    ),
}


@pytest.mark.parametrize("agent_model", ["claude-haiku-4-5", ""])
@pytest.mark.parametrize("routing", list(_ROUTINGS.values()), ids=list(_ROUTINGS))
def test_every_lane_mark_is_the_model_resolve_runner_dispatches(
    routing: RoutingConfig | None, agent_model: str
):
    config = _lane_matrix_config(routing, agent_model)
    lanes = api_models.effective_lane_models(config)
    for lane in ("default", "simple", "medium", "complex"):
        tier = None if lane == "default" else lane
        # A lane resolving to no model is left out: the runner picks it.
        assert lanes.get(lane, "") == resolve_runner(config, tier).model


def test_a_lane_whose_runner_cannot_resolve_is_left_unmarked():
    config = _lane_matrix_config(
        RoutingConfig(
            default=RouteEntry(runner="claude-code", model="claude-opus-5-5"),
            simple=RouteEntry(runner="no-such-runner", model="claude-sonnet-4-6"),
        ),
        "",
    )
    with pytest.raises(ValueError, match="Unknown runner"):
        resolve_runner(config, "simple")
    assert api_models.effective_lane_models(config) == {
        "default": "claude-opus-5-5",
        "medium": "claude-opus-5-5",
        "complex": "claude-opus-5-5",
    }


def test_no_lane_is_marked_when_nothing_names_a_model():
    assert api_models.effective_lane_models(DispatchConfig()) == {}


def test_lane_marks_are_the_projects_routing_not_kews_shipped_default():
    catalog = load_catalog()
    shipped = catalog.lane_default("default")
    assert shipped is not None and shipped != "claude-opus-5-5"
    config = DispatchConfig(
        routing=RoutingConfig(
            default=RouteEntry(runner="claude-code", model="claude-opus-5-5")
        )
    )
    rows = {
        row["id"]: row
        for row in api_models.models_payload(
            catalog,
            api_models.configured_model_names(config),
            api_models.effective_lane_models(config),
        )["models"]
    }
    assert rows["claude-opus-5-5"]["routed_lanes"] == [
        "default",
        "simple",
        "medium",
        "complex",
    ]
    assert rows[shipped]["routed_lanes"] == []
    # kew's shipped default is still reported, under its own key.
    assert "default" in rows[shipped]["default_lanes"]


def test_an_alias_route_marks_the_row_it_belongs_to():
    payload = json.loads(PACKAGED_CATALOG_PATH.read_text(encoding="utf-8"))
    for row in payload["models"]:
        if row["id"] == "claude-opus-5-5":
            row["aliases"] = ["opus-latest"]
    catalog = parse_catalog(payload)
    config = DispatchConfig(
        routing=RoutingConfig(
            default=RouteEntry(runner="claude-code", model="opus-latest")
        )
    )
    served = api_models.models_payload(
        catalog, set(), api_models.effective_lane_models(config)
    )
    marked = {r["id"] for r in served["models"] if r["routed_lanes"]}
    assert marked == {"claude-opus-5-5"}
