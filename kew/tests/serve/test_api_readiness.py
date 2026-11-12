"""Serve coverage for GET /api/readiness (#2514).

The route serves ``kew doctor``'s own checks rather than re-checking, so the
parity test drives BOTH surfaces through the real ``run_checks`` with one faked
set of check groups: the CLI's ``--json`` document and the API's must name the
same ids with the same statuses, in the same order.
"""

from __future__ import annotations

import json
import subprocess
from contextlib import nullcontext
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

import kew.cli.doctor as doctor_module
from kew.cli import app as cli_app
from kew.config import load_config
from kew.readiness import CheckResult, CheckStatus
from kew.serve.api_health import READINESS_TTL_SECONDS, ReadinessCache
from kew.serve.app import create_app
from kew.serve.auth import SessionStore

PATH = "/api/readiness"


def _app(*, expose: bool = False, config: Any = None, cache: Any = None):
    application = create_app(
        lambda: nullcontext(None),
        repo_dir=Path("."),
        config=config,
        expose=expose,
        sessions=SessionStore(),
    )
    if cache is not None:
        application.state.readiness_cache = cache
    return application


class _CountingRunner:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> list[CheckResult]:
        self.calls += 1
        return [CheckResult("required-tools", CheckStatus.OK, f"run {self.calls}")]


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _fixture_groups(_config: Any) -> list[tuple[tuple[str, ...], Any]]:
    """Deterministic stand-ins for every real check group.

    One passes, one fails with a fix, one warns, one raises (the guard turns
    that into a FAIL) — and every other id goes unreported, which ``run_checks``
    fills in as a SKIP.
    """

    def boom() -> None:
        raise RuntimeError("probe exploded")

    return [
        (
            ("required-tools",),
            lambda: doctor_module._record(
                "required-tools", CheckStatus.OK, "every tool resolved"
            ),
        ),
        (
            ("allowlist-network",),
            lambda: doctor_module._record(
                "allowlist-network",
                CheckStatus.FAIL,
                "'kew-allowlist' is not present",
                fix="kew sandbox network",
            ),
        ),
        (
            ("anchor-age",),
            lambda: doctor_module._record(
                "anchor-age", CheckStatus.WARN, "newest anchor is 9.0 day(s) old"
            ),
        ),
        (("model-eol",), boom),
    ]


def test_readiness_matches_doctor_json_for_one_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, inbox_client
) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    (tmp_path / "kew.toml").write_text(
        f'[project]\nrepo = "o/r"\n[agent]\nrunner = "codex"\n'
        f'[logging]\ndir = "{logs}"\n'
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(doctor_module, "_check_groups", _fixture_groups)

    cli = CliRunner().invoke(cli_app, ["doctor", "--json"])
    config = load_config()
    application = create_app(
        lambda: nullcontext(None),
        repo_dir=tmp_path,
        config=config,
        readiness_checks=lambda: doctor_module.run_checks(config),
    )
    response = inbox_client(application, loopback_peer=True).get(PATH)

    assert cli.exit_code == 1, cli.output
    assert response.status_code == 200
    doctor = json.loads(cli.stdout)
    served = response.json()

    def verdicts(report: dict[str, Any]) -> list[tuple[str, str]]:
        return [(check["id"], check["status"]) for check in report["checks"]]

    assert verdicts(served) == verdicts(doctor)
    assert len(verdicts(served)) == len(doctor_module.CHECK_IDS)
    statuses = dict(verdicts(served))
    assert statuses["required-tools"] == "ok"
    assert statuses["allowlist-network"] == "fail"
    assert statuses["anchor-age"] == "warn"
    assert statuses["model-eol"] == "fail"
    assert statuses["route-live"] == "skip"
    assert served["schema"] == doctor["schema"]
    assert served["ok"] is doctor["ok"] is False
    # The fix is reported, never executed: it is a string in the payload.
    network = {c["id"]: c for c in served["checks"]}["allowlist-network"]
    assert network["fix"] == "kew sandbox network"


def test_readiness_cache_two_calls_within_ttl_run_the_checks_once(
    inbox_client,
) -> None:
    runner = _CountingRunner()
    clock = _Clock()
    client = inbox_client(
        _app(cache=ReadinessCache(runner, clock=clock)), loopback_peer=True
    )

    first = client.get(PATH).json()
    clock.now += READINESS_TTL_SECONDS - 1
    second = client.get(PATH).json()

    assert runner.calls == 1
    assert first == second


def test_readiness_cache_reruns_the_checks_after_the_ttl(inbox_client) -> None:
    runner = _CountingRunner()
    clock = _Clock()
    client = inbox_client(
        _app(cache=ReadinessCache(runner, clock=clock)), loopback_peer=True
    )

    client.get(PATH)
    clock.now += READINESS_TTL_SECONDS
    second = client.get(PATH).json()

    assert runner.calls == 2
    assert second["checks"][0]["summary"] == "run 2"


def test_readiness_cache_is_built_from_the_injected_checks(inbox_client) -> None:
    runner = _CountingRunner()
    application = create_app(
        lambda: nullcontext(None), repo_dir=Path("."), readiness_checks=runner
    )
    client = inbox_client(application, loopback_peer=True)

    assert client.get(PATH).status_code == 200
    assert client.get(PATH).status_code == 200
    assert runner.calls == 1


def test_readiness_answers_503_when_no_checks_are_wired(inbox_client) -> None:
    client = inbox_client(_app(), loopback_peer=True)
    assert client.get(PATH).status_code == 503


def test_kew_serve_wires_doctor_run_checks_with_its_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``kew serve`` hands the app the doctor's own checks, bound to its config."""
    from kew.config import DispatchConfig, LoggingConfig

    monkeypatch.chdir(tmp_path)
    config = DispatchConfig(logging=LoggingConfig(dir=str(tmp_path)))
    seen: list[object] = []
    monkeypatch.setattr(
        doctor_module,
        "run_checks",
        lambda cfg: seen.append(cfg) or [],
    )
    with (
        patch("kew.cli.load_config", return_value=config),
        patch("kew.cli.preflight_tools"),
        patch("kew.serve.app.create_app", return_value=MagicMock()) as create,
        patch("uvicorn.run"),
        patch(
            "kew.subprocess_utils.run",
            return_value=subprocess.CompletedProcess(args=[], returncode=1),
        ),
    ):
        result = CliRunner().invoke(cli_app, ["serve"])

    assert result.exit_code == 0, result.output
    create.call_args.kwargs["readiness_checks"]()
    assert seen == [config]


def test_readiness_cache_does_not_keep_a_run_that_raised() -> None:
    attempts = 0

    def flaky() -> list[CheckResult]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("first run failed")
        return [CheckResult("required-tools", CheckStatus.OK, "fine")]

    cache = ReadinessCache(flaky)
    with pytest.raises(RuntimeError):
        cache.read()

    assert cache.read()["checks"][0]["status"] == "ok"
    assert attempts == 2


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_readiness_auth_rejects_non_get_methods(inbox_client, method: str) -> None:
    runner = _CountingRunner()
    client = inbox_client(_app(cache=ReadinessCache(runner)), loopback_peer=True)

    assert client.request(method, PATH).status_code == 405
    assert runner.calls == 0


def test_readiness_auth_loopback_reads_without_session_when_not_exposed(
    inbox_client,
) -> None:
    client = inbox_client(
        _app(cache=ReadinessCache(_CountingRunner())), loopback_peer=True
    )
    assert client.get(PATH).status_code == 200


def test_readiness_auth_non_loopback_peer_needs_a_session(inbox_client) -> None:
    runner = _CountingRunner()
    client = inbox_client(_app(cache=ReadinessCache(runner)), loopback_peer=False)

    assert client.get(PATH).status_code == 401
    assert runner.calls == 0


def test_readiness_auth_exposed_needs_a_session_even_from_loopback(
    inbox_client,
) -> None:
    runner = _CountingRunner()
    application = _app(expose=True, cache=ReadinessCache(runner))
    client = inbox_client(application, loopback_peer=True)

    assert client.get(PATH).status_code == 401
    assert runner.calls == 0

    sid, _ = application.state.sessions.create()
    client.cookies.set("kew_session", sid)
    assert client.get(PATH).status_code == 200
    assert runner.calls == 1
