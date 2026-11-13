"""Serve coverage for GET /api/health, /api/health/env and /api/config/effective.

Every host probe is faked here — docker (the runtime and image probes), gh (the
rate-limit reading the sweep caches in the ledger) and the environment — so the
whole surface is exercised with no container runtime, no network and no real
credential (#2298, AC1).

The payload rule has its own test: a sentinel secret is exported into the
process environment and the ENTIRE serialized response is grepped for it (AC2).
That is deliberately a grep rather than a field-by-field assertion — a future
field that serialises a value would pass a shape check and fail this one.
"""

from __future__ import annotations

import json
import subprocess
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from kew.config import _ENV_OVERRIDES, DispatchConfig, SandboxConfig
from kew.serve.app import create_app
from kew.service.health import ENV_SURFACE, HealthProbes, HealthReader

PATH = "/api/health"
ENV_PATH = "/api/health/env"
EFFECTIVE_PATH = "/api/config/effective"

#: The value no response may contain. Deliberately unmistakable: if it appears
#: anywhere in a payload, something serialised an environment variable's value.
SENTINEL = "kew-sentinel-secret-6f2b9e"


class FakeStore:
    """The ledger reads a health assembly makes, and nothing else."""

    def __init__(self, *, rate_limit: str | None = None, runtimes: dict | None = None):
        self._meta = {"github_rate_limit": rate_limit} if rate_limit else {}
        self._runtimes = runtimes or {}

    def get_meta(self, key: str) -> str | None:
        return self._meta.get(key)

    def count_container_runtimes(self, since: datetime) -> dict[str, int]:
        return dict(self._runtimes)

    def list_epic_refs(self) -> list[str]:
        return []

    def get_epic_driver_state(self, epic_ref: str) -> Any:
        return None

    def get_fleet_stopped(self) -> Any:
        return None

    def list_pending(self) -> list[Any]:
        return []

    def list_loops(self, status: str | None = None) -> list[Any]:
        return []

    def list_config_overrides(self) -> list[dict[str, Any]]:
        return []


def fake_probes(
    *,
    runtimes: dict[str, bool | None] | None = None,
    images: dict[str, bool | None] | None = None,
    env: set[str] | None = None,
) -> HealthProbes:
    """Probes that answer from committed dictionaries — no docker, no env read."""
    runtime_map = {"docker": True, "podman": False, "openshell": None}
    runtime_map.update(runtimes or {})
    image_map = dict(images or {})
    env_set = set(env or ())
    return HealthProbes(
        runtime_present=lambda name: runtime_map.get(name),
        image_present=lambda _runtime, ref: image_map.get(ref, False),
        env_set=lambda name: name in env_set,
    )


def _config(**sandbox: Any) -> DispatchConfig:
    settings = {"images": {"claude-code": "kew-agent:test"}, **sandbox}
    return DispatchConfig(sandbox=SandboxConfig(**settings))


def _mk_app(
    tmp_path: Path,
    *,
    expose: bool = False,
    store: FakeStore | None = None,
    probes: HealthProbes | None = None,
    config: DispatchConfig | None = None,
):
    return create_app(
        lambda: nullcontext(store or FakeStore()),
        repo_dir=tmp_path,
        expose=expose,
        config=config or _config(),
        health_reader=HealthReader(probes=probes or fake_probes()),
    )


def _read(tmp_path: Path, inbox_client, path: str = PATH, **kwargs) -> dict[str, Any]:
    client = inbox_client(_mk_app(tmp_path, **kwargs), loopback_peer=True)
    response = client.get(path)
    assert response.status_code == 200, response.text
    return response.json()


# ── the guard (no unguarded routes, ever) ───────────────────────────────────


@pytest.mark.parametrize("path", [PATH, ENV_PATH, EFFECTIVE_PATH])
def test_exposed_reads_require_a_session(tmp_path: Path, inbox_client, path: str):
    client = inbox_client(_mk_app(tmp_path, expose=True), loopback_peer=True)
    assert client.get(path).status_code == 401


@pytest.mark.parametrize("path", [PATH, ENV_PATH, EFFECTIVE_PATH])
def test_non_loopback_reads_require_a_session(tmp_path: Path, inbox_client, path: str):
    client = inbox_client(_mk_app(tmp_path, expose=False), loopback_peer=False)
    assert client.get(path).status_code == 401


# ── no value of any environment variable, anywhere ──────────────────────────


CREDENTIAL_ENVS = (
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "ANTHROPIC_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "LINEAR_API_KEY",
    "OPENROUTER_API_KEY",
)


@pytest.mark.parametrize("path", [PATH, ENV_PATH, EFFECTIVE_PATH])
def test_no_response_carries_a_credential_value(
    tmp_path: Path, inbox_client, monkeypatch: pytest.MonkeyPatch, path: str
):
    """A sentinel secret in the environment reaches no byte of any payload."""
    for name in CREDENTIAL_ENVS:
        monkeypatch.setenv(name, SENTINEL)
    client = inbox_client(_mk_app(tmp_path), loopback_peer=True)
    response = client.get(path)
    assert response.status_code == 200, response.text
    assert SENTINEL not in response.text


@pytest.mark.parametrize("path", [PATH, ENV_PATH])
def test_the_health_reads_carry_no_env_value_at_all(
    tmp_path: Path, inbox_client, monkeypatch: pytest.MonkeyPatch, path: str
):
    """Not just credentials: these two reads carry NO environment value.

    Every name the surface knows about is set to the sentinel, including the
    operational ``KEW_*`` selectors, and the whole serialized payload is grepped.
    A field-by-field assertion would pass a future field that serialised a value;
    this will not.
    """
    for name, _purpose, _required_by in ENV_SURFACE:
        monkeypatch.setenv(name, SENTINEL)
    client = inbox_client(_mk_app(tmp_path), loopback_peer=True)
    response = client.get(path)
    assert response.status_code == 200, response.text
    assert SENTINEL not in response.text


def test_the_only_env_values_config_effective_can_echo_are_the_selectors(
    tmp_path: Path, inbox_client
):
    """``/api/config/effective`` reports effective CONFIG, which is the point.

    Several of its values legitimately come from the environment — the repo, the
    log directory, the worker count, the fleet cap — so it cannot promise "no env
    value ever". What it can promise is that the env layer is the closed
    ``_ENV_OVERRIDES`` allowlist and that no entry in it is a credential, which
    is what the two tests above then enforce for the surfaces that make the
    stronger claim. The count is pinned so a new selector is a deliberate edit
    here rather than a quiet widening of what the surface may echo.
    """
    names = {name for name in _ENV_OVERRIDES}
    assert len(names) == 10, sorted(names)
    assert not names & set(CREDENTIAL_ENVS)


def test_env_report_lists_names_with_a_purpose_and_whether_they_are_set(
    tmp_path: Path, inbox_client
):
    body = _read(
        tmp_path, inbox_client, ENV_PATH, probes=fake_probes(env={"GITHUB_TOKEN"})
    )
    assert body["as_seen_by"] == "the serve process"
    by_name = {entry["name"]: entry for entry in body["vars"]}
    assert by_name["GITHUB_TOKEN"]["set"] is True
    assert by_name["LINEAR_API_KEY"]["set"] is False
    assert by_name["GITHUB_TOKEN"]["purpose"]
    assert by_name["GITHUB_TOKEN"]["required_by"]
    # The shape is exactly four keys: a fifth would be the place a value lands.
    assert set(by_name["GITHUB_TOKEN"]) == {"name", "purpose", "required_by", "set"}


# ── runtimes, images, posture ───────────────────────────────────────────────


def test_runtimes_are_a_list_and_an_unknown_presence_is_not_a_failure(
    tmp_path: Path, inbox_client
):
    """``openshell`` is accepted by config and backed since epic node N8.

    It must appear in the list with its backend acknowledged, and a presence
    the probe could not establish stays ``null``: unknown is not a failure.
    """
    body = _read(tmp_path, inbox_client)
    assert isinstance(body["runtimes"], list)
    by_name = {entry["name"]: entry for entry in body["runtimes"]}
    assert by_name["docker"] == {
        "name": "docker",
        "configured": True,
        "backed": True,
        "present": True,
    }
    assert by_name["openshell"]["backed"] is True
    assert by_name["openshell"]["present"] is None


def test_a_routed_image_and_an_unrouted_one_are_both_listed_and_distinguished(
    tmp_path: Path, inbox_client
):
    """The fact the header badge counts on: ``routed``.

    ``opencode`` has a mapped image that no lane routes to, so its absence is
    not a factory fault; ``claude-code`` is the default route's runner, so its
    absence is.
    """
    config = _config(images={"claude-code": "kew-agent:test", "opencode": "oc:test"})
    body = _read(
        tmp_path,
        inbox_client,
        config=config,
        probes=fake_probes(images={"kew-agent:test": False, "oc:test": False}),
    )
    by_runner = {entry["runner"]: entry for entry in body["images"]}
    assert by_runner["claude-code"]["routed"] is True
    assert by_runner["claude-code"]["present"] is False
    assert by_runner["claude-code"]["source"] == "images.claude-code"
    assert by_runner["opencode"]["routed"] is False
    assert by_runner["opencode"]["present"] is False


def test_an_unmapped_runner_image_reads_unmapped_rather_than_raising(
    tmp_path: Path, inbox_client
):
    """No ``images`` map and no legacy ``image``: nothing to launch, said plainly
    rather than raised at a reader."""
    body = _read(tmp_path, inbox_client, config=_config(images={}, image=""))
    by_runner = {entry["runner"]: entry for entry in body["images"]}
    assert by_runner["claude-code"]["source"] == "unmapped"
    assert by_runner["claude-code"]["ref"] is None
    assert by_runner["claude-code"]["present"] is None


def test_posture_covers_the_review_lane_and_names_the_credential_mode(
    tmp_path: Path, inbox_client
):
    """The review lane is a lane. It has its own runner pin and its own image,
    and a reachability reading that skipped it said nothing about the stage that
    holds every PR."""
    body = _read(tmp_path, inbox_client, probes=fake_probes(env=set()))
    lanes = {entry["lane"]: entry for entry in body["posture"]}
    assert "review" in lanes
    assert lanes["review"]["runner"] == "claude-code"
    # No credential in this environment: a contained launch would authenticate
    # as nothing, which is the `placeholder` reading.
    assert lanes["review"]["credential_mode"] == "placeholder"
    assert lanes["review"]["dangerously_skip_containment"] is False


def test_a_credential_the_runner_declares_makes_the_lane_real(
    tmp_path: Path, inbox_client
):
    """The subscription path counts.

    ``CLAUDE_CODE_OAUTH_TOKEN`` is absent from ``CLAUDE_AUTH_ENVS``, so the old
    reachability check reported an operator authenticated the common way as
    having no credential at all. The runner's own ``required_credentials`` is
    the list asked here.
    """
    body = _read(
        tmp_path, inbox_client, probes=fake_probes(env={"CLAUDE_CODE_OAUTH_TOKEN"})
    )
    lanes = {entry["lane"]: entry for entry in body["posture"]}
    assert lanes["default"]["credential_mode"] == "real"
    runners = {entry["name"]: entry for entry in body["runners"]}
    names = {c["name"]: c["set"] for c in runners["claude-code"]["credential_names"]}
    assert names == {"ANTHROPIC_API_KEY": False, "CLAUDE_CODE_OAUTH_TOKEN": True}
    assert runners["claude-code"]["review_lane"] is True
    assert "default" in runners["claude-code"]["routed_tiers"]


def test_observed_runtimes_come_from_the_ledger_not_the_config(
    tmp_path: Path, inbox_client
):
    """What ran, beside what is configured. A run with no recorded runtime is
    counted under the empty string rather than dropped."""
    store = FakeStore(runtimes={"docker": 12, "": 3})
    body = _read(tmp_path, inbox_client, store=store)
    assert body["observed_7d"] == {"docker": 12, "": 3}
    assert body["observed_window_days"] == 7


def test_a_wedged_container_runtime_still_answers_200(
    tmp_path: Path, inbox_client, monkeypatch: pytest.MonkeyPatch
):
    """The one test here that runs the PRODUCTION image probe.

    Everything above fakes the probes, which cannot see what the real one does
    with a docker daemon that has stopped answering: ``run`` raises
    ``subprocess.TimeoutExpired`` when its 15s bound expires, that is a
    ``SubprocessError`` rather than an ``OSError``, and it escaped the probe's
    handler to turn this read into a 500. The documented reading for a host that
    cannot answer is ``unknown``, so the route must stay 200.
    """

    def wedged(cmd: list[str], **_kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(cmd, 15)

    monkeypatch.setattr("kew.subprocess_utils.run", wedged)
    client = inbox_client(
        _mk_app(
            tmp_path,
            probes=HealthProbes(),
            config=_config(runtime="docker"),
        ),
        loopback_peer=True,
    )
    response = client.get(PATH)
    assert response.status_code == 200, response.text
    images = {entry["ref"]: entry for entry in response.json()["images"]}
    assert images["kew-agent:test"]["present"] is None


# ── trackers (gh is faked through the cached reading) ────────────────────────


def _rate_limit(**fields: Any) -> str:
    payload = {
        "ok": True,
        "observed_at": datetime.now(UTC).isoformat(),
        "core": {"limit": 5000, "remaining": 4987, "reset_at": ""},
        "scopes": ["repo", "workflow"],
        "reason": "",
    }
    payload.update(fields)
    return json.dumps(payload)


def test_github_reading_comes_from_the_cached_sweep_payload(
    tmp_path: Path, inbox_client
):
    body = _read(tmp_path, inbox_client, store=FakeStore(rate_limit=_rate_limit()))
    github = body["trackers"]["github"]
    assert github["reachable"] is True
    assert github["auth_ok"] is True
    assert github["scopes"] == ["repo", "workflow"]
    assert github["rate"]["remaining"] == 4987
    assert github["observed_age_s"] is not None


def test_a_401_reading_is_reachable_but_not_authorized(tmp_path: Path, inbox_client):
    """Two different repairs, so two different fields."""
    store = FakeStore(rate_limit=_rate_limit(ok=False, reason="unauthorized"))
    github = _read(tmp_path, inbox_client, store=store)["trackers"]["github"]
    assert github["reachable"] is True
    assert github["auth_ok"] is False


def test_a_reading_that_got_no_answer_is_unreachable_with_no_auth_verdict(
    tmp_path: Path, inbox_client
):
    """``unavailable`` means gh could not be run, or ran and said nothing.

    For a project whose work lives on GitHub that is a real outage — so
    ``reachable`` is False — but it says nothing about the credential, so
    ``auth_ok`` stays unknown rather than being blamed.
    """
    store = FakeStore(rate_limit=_rate_limit(ok=False, reason="unavailable"))
    github = _read(tmp_path, inbox_client, store=store)["trackers"]["github"]
    assert github["reachable"] is False
    assert github["auth_ok"] is None


def test_an_http_error_reading_is_reachable_with_no_auth_verdict(
    tmp_path: Path, inbox_client
):
    store = FakeStore(rate_limit=_rate_limit(ok=False, reason="http_503", status=503))
    github = _read(tmp_path, inbox_client, store=store)["trackers"]["github"]
    assert github["reachable"] is True
    assert github["auth_ok"] is None


def test_no_cached_reading_is_unknown_rather_than_broken(tmp_path: Path, inbox_client):
    github = _read(tmp_path, inbox_client)["trackers"]["github"]
    assert github["reachable"] is None
    assert github["auth_ok"] is None
    assert github["reason"] == "no_reading"


def test_an_unparseable_cached_reading_says_so_and_is_never_rewritten(
    tmp_path: Path, inbox_client
):
    """A bad historical row is tolerated by the reader, never repaired."""
    store = FakeStore(rate_limit="{not json")
    github = _read(tmp_path, inbox_client, store=store)["trackers"]["github"]
    assert github["reason"] == "unparseable"
    assert store.get_meta("github_rate_limit") == "{not json"


def test_a_github_project_reads_linear_as_not_configured_never_as_down(
    tmp_path: Path, inbox_client
):
    linear = _read(tmp_path, inbox_client)["trackers"]["linear"]
    assert linear["configured"] is False
    assert linear["reachable"] is None


# ── daemons ─────────────────────────────────────────────────────────────────


def test_a_serve_without_a_sweep_reports_absence_not_failure(
    tmp_path: Path, inbox_client
):
    body = _read(tmp_path, inbox_client)
    assert body["sweep"]["present"] is False
    assert body["sweep"]["heartbeat_at"] is None
    assert body["sweep"]["heartbeat_age_s"] is None


def test_a_stored_heartbeat_is_aged_even_with_no_sweep_in_this_process(
    tmp_path: Path, inbox_client
):
    """The heartbeat is the only evidence a sweep ever ran here."""
    stamp = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    store = FakeStore()
    store._meta["scheduler_heartbeat"] = stamp
    body = _read(tmp_path, inbox_client, store=store)
    assert body["sweep"]["heartbeat_at"] == stamp
    assert 250 < body["sweep"]["heartbeat_age_s"] < 400


def test_a_watcher_that_is_off_by_design_is_not_a_failure(tmp_path: Path, inbox_client):
    watchers = {w["name"]: w for w in _read(tmp_path, inbox_client)["watchers"]}
    assert watchers["delegation"]["enabled"] is False
    # Never False: nothing proves the watcher is down, and a red × for a watcher
    # nobody asked for is the two-state mistake this field exists to avoid.
    assert watchers["delegation"]["running"] is None


def test_the_reading_carries_the_fleet_latch_and_the_loop_counts(
    tmp_path: Path, inbox_client
):
    body = _read(tmp_path, inbox_client)
    assert body["fleet"] == {"stopped": False, "pending": 0, "reason": ""}
    assert body["loops"] == {"pursuing": 0, "stale": 0}
    assert body["drivers"] == []
    assert body["checked_at"]


# ── /api/config/effective ───────────────────────────────────────────────────


def test_effective_config_names_the_winning_layer_for_every_field(
    tmp_path: Path, inbox_client, monkeypatch: pytest.MonkeyPatch
):
    """The four layers, read off one project: env beats file, file beats default.

    ``KEW_MAX_WORKERS`` also sits in ``kew.toml`` here, which is the case the
    old ``file_value`` got wrong — the env number was reported as the file's.
    """
    (tmp_path / "kew.toml").write_text(
        "[parallel]\nmax_workers = 2\n\n[review]\nmax_rounds = 5\n"
    )
    monkeypatch.setenv("KEW_PROJECT", str(tmp_path))
    monkeypatch.setenv("KEW_MAX_WORKERS", "4")
    fields = _read(tmp_path, inbox_client, EFFECTIVE_PATH)["fields"]
    assert fields["parallel.max_workers"] == {
        "value": 4,
        "source": "env (KEW_MAX_WORKERS)",
    }
    assert fields["review.max_rounds"] == {"value": 5, "source": "kew.toml"}
    assert fields["budget.daily_usd"]["source"] == "default"


def test_effective_config_says_why_it_has_no_human_review_policy(
    tmp_path: Path, inbox_client
):
    """A project with no canonical delivery repository has no policy to read.

    That is a legitimate state, and it must not read as "the policy happens to
    be empty" — the refusal's own sentence travels in the response, so a reader
    of the thin-looking ``human_review.*`` rows can tell which of the two it is.
    """
    body = _read(tmp_path, inbox_client, EFFECTIVE_PATH)
    assert body["human_review_unavailable"]
    assert "human_review.max_pending_reviews" in body["fields"]


def test_effective_config_shows_the_caps_the_override_page_omits(
    tmp_path: Path, inbox_client
):
    """None of these is DB-overridable, so the override page never listed them —
    and a cap nobody can see is the one that surprises an operator mid-epic."""
    fields = _read(tmp_path, inbox_client, EFFECTIVE_PATH)["fields"]
    for field in (
        "review.max_rounds",
        "review.max_review_usd",
        "review.max_remediation_usd",
        "review.stop_rule_after_rounds",
        "parallel.max_workers",
        "epic.max_parallel",
        "retry.max_attempts",
        "agent.timeout_minutes",
    ):
        assert field in fields, field
        assert set(fields[field]) >= {"value", "source"}
