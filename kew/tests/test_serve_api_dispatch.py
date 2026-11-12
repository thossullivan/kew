"""Serve coverage for GET /api/dispatch/resolve (#2302, AC1).

The four properties the endpoint promises, each as a test: it is behind
``read_guard``, it carries no credential value, it makes no ``gh`` call, and it
writes nothing at all — the store this app is given raises on every write, so a
single audit row would fail the request rather than quietly land in the ledger.

The per-layer provenance is tested against the service in
``tests/test_service_dispatch_plan.py``; what is checked here is the HTTP shape
and the guards.
"""

from __future__ import annotations

from contextlib import nullcontext, suppress
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from kew.config import AgentConfig, DispatchConfig, SandboxConfig
from kew.serve.app import create_app
from kew.service.health import ENV_SURFACE, HealthProbes, HealthReader

PATH = "/api/dispatch/resolve"

SENTINEL = "kew-sentinel-secret-6f2b9e"


class Wrote(BaseException):
    """A write reached the store from a read endpoint.

    Deliberately OFF the ``Exception`` tree. Every provenance read in
    ``service/dispatch_plan.py`` is wrapped in a broad ``except Exception`` on
    purpose — an unreadable override, a cold prefix cache or an unreachable
    governor must never fail a reading — and an ``AssertionError`` raised from
    inside one of those is swallowed: the request still answered 200 and this
    module's guard passed against the very write it exists to catch (reviewer
    P3). ``BaseException`` is not swallowed, and the write is recorded as well,
    so the guard holds even if a handler widens further.
    """


class NoWriteStore:
    """The plan's reads. Every write is a failure, not a no-op."""

    def __init__(self) -> None:
        #: Every write attempted, in order, named by method. Recorded as well
        #: as raised so the assertion does not depend on an exception
        #: surviving the plan's handlers.
        self.writes: list[str] = []

    def _wrote(self, what: str) -> None:
        self.writes.append(what)
        raise Wrote(f"a read endpoint wrote: {what}")

    def get_config_override(self, field: str) -> dict[str, Any] | None:
        return None

    def list_by_issue(self, issue_num: int) -> list[Any]:
        return []

    def list_by_item(self, item_id: str) -> list[Any]:
        return []

    def list_since(self, since: datetime) -> list[Any]:
        return []

    def warm_prefixes(self, provider: str, now: datetime) -> set[str]:
        return set()

    def lookup_readiness_probe(self, fingerprint: str, *, now: datetime | None = None):
        from kew.run_store_readiness import ProbeLookupResult, ProbeVerdict

        return ProbeLookupResult(verdict=ProbeVerdict.MISS, probe=None)

    def get_fleet_stopped(self) -> dict[str, str] | None:
        return None

    def list_audit_events(self, **kwargs: Any) -> list[Any]:
        return []

    # ── the writes, every one of them a test failure ────────────────────────

    def append_audit_event(self, **kwargs: Any) -> None:
        self._wrote(f"append_audit_event {kwargs.get('event', '?')}")

    def set_meta(self, key: str, value: str) -> None:
        self._wrote(f"set_meta {key!r}")

    def lease_readiness_probe(self, **kwargs: Any) -> None:
        self._wrote("lease_readiness_probe")

    def try_admit_run(self, **kwargs: Any) -> None:
        self._wrote("try_admit_run")


def _probes() -> HealthProbes:
    return HealthProbes(
        runtime_present=lambda _name: True,
        image_present=lambda _runtime, _ref: True,
        env_set=lambda name: name == "ANTHROPIC_API_KEY",
    )


def _config() -> DispatchConfig:
    return DispatchConfig(
        agent=AgentConfig(model="claude-opus-4-8"),
        sandbox=SandboxConfig(enabled=False, dangerously_skip_containment=True),
    )


def _mk_app(
    tmp_path: Path,
    *,
    expose: bool = False,
    store: NoWriteStore | None = None,
    config: DispatchConfig | None = None,
):
    bound = store if store is not None else NoWriteStore()
    return create_app(
        lambda: nullcontext(bound),
        repo_dir=tmp_path,
        expose=expose,
        config=config if config is not None else _config(),
        health_reader=HealthReader(probes=_probes()),
    )


def _read(
    tmp_path: Path, inbox_client, query: str = "", **kwargs: Any
) -> dict[str, Any]:
    client = inbox_client(_mk_app(tmp_path, **kwargs), loopback_peer=True)
    response = client.get(f"{PATH}{query}")
    assert response.status_code == 200, response.text
    return response.json()


# ── the guard ───────────────────────────────────────────────────────────────


def test_exposed_reads_require_a_session(tmp_path: Path, inbox_client):
    client = inbox_client(_mk_app(tmp_path, expose=True), loopback_peer=True)
    assert client.get(PATH).status_code == 401


def test_non_loopback_reads_require_a_session(tmp_path: Path, inbox_client):
    client = inbox_client(_mk_app(tmp_path, expose=False), loopback_peer=False)
    assert client.get(PATH).status_code == 401


# ── it writes nothing ───────────────────────────────────────────────────────


def test_a_resolve_writes_no_audit_row(tmp_path: Path, inbox_client):
    """AC1's other half, at the HTTP boundary.

    ``NoWriteStore`` records and raises on ``append_audit_event``, so a
    ``routing_decision`` emitted from the plan path — the defect this issue
    names in ``kew dispatch --dry-run`` — fails here. The RECORD is what the
    assertion reads: the raise alone is swallowed wherever the plan wraps a
    provenance read in ``except Exception`` (reviewer P3).
    """
    store = NoWriteStore()
    plan = _read(tmp_path, inbox_client, "?item=2302&complexity=medium", store=store)
    assert store.writes == []
    assert plan["governor"]["reason"]


@pytest.mark.parametrize(
    "read",
    [
        "get_config_override",
        "warm_prefixes",
        "list_audit_events",
        "get_fleet_stopped",
        "lookup_readiness_probe",
    ],
)
def test_the_no_write_guard_survives_the_plans_own_exception_handlers(
    tmp_path: Path, inbox_client, read: str
):
    """The guard above is only worth having if it can FAIL (reviewer P3).

    Each of these reads sits inside a broad ``except Exception`` in
    ``service/dispatch_plan.py`` — ``_override_source``, ``_warm_prefixes``,
    ``_open_hold``, ``_admission``, ``_live_status`` — which is where a
    re-added ``append_audit_event`` would most plausibly land, and where an
    ``AssertionError``-based guard was swallowed into a 200 with a truthy
    ``governor.reason`` of "governor_error". A write from any of them now fails
    the request AND leaves a record.
    """
    store = NoWriteStore()

    def writing(*_args: Any, **_kwargs: Any):
        store.append_audit_event(event="routing_decision", issue_num=2302)

    setattr(store, read, writing)
    client = inbox_client(_mk_app(tmp_path, store=store), loopback_peer=True)
    status: int | None = None
    # `Wrote` leaves the ASGI stack inside anyio's exception group, so the
    # shape it arrives in is not the assertion — "the row was written and the
    # request did not answer 200" is.
    with suppress(BaseException):
        status = client.get(f"{PATH}?item=2302&complexity=medium").status_code
    assert store.writes == ["append_audit_event routing_decision"]
    assert status != 200


# ── no credential value, no GitHub call ─────────────────────────────────────


def test_no_response_carries_an_env_value(
    tmp_path: Path, inbox_client, monkeypatch: pytest.MonkeyPatch
):
    """Every name the surface knows is set to one sentinel, and the whole
    payload is grepped — a future field that serialised a value fails here."""
    for name, _purpose, _required_by in ENV_SURFACE:
        monkeypatch.setenv(name, SENTINEL)
    client = inbox_client(_mk_app(tmp_path), loopback_peer=True)
    response = client.get(f"{PATH}?item=2302")
    assert response.status_code == 200, response.text
    assert SENTINEL not in response.text


def test_the_credential_rows_are_names_and_a_bit(tmp_path: Path, inbox_client):
    plan = _read(tmp_path, inbox_client, "?item=2302")
    rows = plan["containment"]["credential_names_present"]
    assert rows
    assert all(set(row) == {"name", "set"} for row in rows)


def test_no_request_makes_a_gh_call(
    tmp_path: Path, inbox_client, monkeypatch: pytest.MonkeyPatch
):
    """The tracker is not consulted: the complexity is the caller's to supply."""
    import kew.subprocess_utils as subprocess_utils

    real = subprocess_utils.run

    def guarded(argv: list[str], *args: Any, **kwargs: Any):
        assert argv and argv[0] != "gh", f"a read endpoint called gh: {argv}"
        return real(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess_utils, "run", guarded)
    plan = _read(tmp_path, inbox_client, "?item=2302&complexity=simple")
    assert plan["complexity"]["value"] == "simple"


# ── the shape, and the bounds on what a caller may ask ──────────────────────


def test_the_payload_names_a_source_for_every_field(tmp_path: Path, inbox_client):
    plan = _read(tmp_path, inbox_client, "?item=2302&complexity=medium")
    assert plan["runner"]["value"] == "claude-code"
    assert plan["runner"]["source"] == "inferred from [agent].model"
    for key in ("lane", "complexity", "runner", "model", "transport", "containment"):
        assert plan[key]["source"]


def test_an_absent_complexity_is_answered_not_refused(tmp_path: Path, inbox_client):
    plan = _read(tmp_path, inbox_client, "?item=2302")
    assert plan["complexity"]["known"] is False


@pytest.mark.parametrize("lane", ["implement", "review", "remediation"])
def test_each_lane_resolves(tmp_path: Path, inbox_client, lane: str):
    plan = _read(tmp_path, inbox_client, f"?item=2302&lane={lane}")
    assert plan["lane"]["value"] == lane


def test_an_unknown_lane_is_refused_by_the_route(tmp_path: Path, inbox_client):
    """A typo must not silently resolve the implement lane's route."""
    client = inbox_client(_mk_app(tmp_path), loopback_peer=True)
    assert client.get(f"{PATH}?lane=production").status_code == 422


def test_an_unknown_complexity_is_refused_by_the_route(tmp_path: Path, inbox_client):
    client = inbox_client(_mk_app(tmp_path), loopback_peer=True)
    assert client.get(f"{PATH}?complexity=gigantic").status_code == 422


def test_an_unbounded_item_reference_is_refused(tmp_path: Path, inbox_client):
    client = inbox_client(_mk_app(tmp_path), loopback_peer=True)
    assert client.get(f"{PATH}?item={'9' * 200}").status_code == 422


@pytest.mark.parametrize("item", ["\u00b2", "\u0660", "\u2081"])
def test_a_non_ascii_digit_item_is_answered_not_a_500(
    tmp_path: Path, inbox_client, item: str
):
    """``str.isdigit`` accepts more than ``int`` does (reviewer P4).

    ``?item=%C2%B2`` raised an unhandled ``ValueError`` out of a read endpoint
    whose own docstring says nothing here raises; ``?item=%D9%A0`` was quieter
    and worse, resolving to issue 0 — the sentinel bucket per-item keys are
    kept out of.
    """
    plan = _read(tmp_path, inbox_client, f"?item={item}")
    assert plan["issue_num"] is None
    assert plan["item"] == item


def test_a_config_naming_an_unknown_runner_is_answered_not_a_500(
    tmp_path: Path, inbox_client
):
    """An unresolvable RUNNER reads like every other unresolvable route.

    No config validator checks ``[agent] runner`` against the registry, so the
    name loads and raises at the resolution — and that raise was taken outside
    the plan's refusal guard, so this endpoint answered 500 rather than the
    ``posture: refused`` reading it documents (reviewer finding). `_read`
    asserts the 200.
    """
    absent = "not-a-runner"
    plan = _read(
        tmp_path,
        inbox_client,
        config=DispatchConfig(
            agent=AgentConfig(runner=absent, model="claude-opus-4-8"),
            sandbox=SandboxConfig(enabled=False, dangerously_skip_containment=True),
        ),
    )
    assert plan["containment"]["posture"] == "refused"
    assert absent in plan["containment"]["refusal_reason"]
    assert plan["runner"]["value"] == ""


def test_the_resolver_is_shared_across_requests(tmp_path: Path, inbox_client):
    """One resolver on app state, so the TTL survives between reads."""
    app = _mk_app(tmp_path)
    client = inbox_client(app, loopback_peer=True)
    assert client.get(PATH).status_code == 200
    first = app.state.dispatch_resolver
    assert client.get(PATH).status_code == 200
    assert app.state.dispatch_resolver is first
    # And it reads the health node's probe cache, so both pages answer "is this
    # image here" from one place.
    assert first.cache is app.state.health_reader.cache
