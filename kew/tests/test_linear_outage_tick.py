"""A Linear outage during an epic tick ends as the documented `linear-unavailable`
outcome, never a traceback (#1117 launch gate).

The go-live drive hit two real Linear network failures on 2026-09-14: a read
timeout in the facts reader and a DNS failure (``[Errno 8] nodename nor
servname``). Both ended the tick with a traceback at exit 1. The CLI tests
elsewhere raise ``LinearUnavailableError`` from a patched ``advance``; these
drive the REAL tick through the real Linear client, with only the socket layer
(``http.client.HTTPSConnection``) failing, so the transport-to-exit mapping is
what is under test.

Documented outcome (docs/cli/epic.md): the client retries with bounded
backoff, then `advance` and `run` exit 2 with the `linear-unavailable`
diagnostic, and nothing is admitted, reserved, claimed or journaled.
"""

from __future__ import annotations

import functools
import json
import socket
import ssl
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from kew.cli import epic_app
from kew.config import DispatchConfig
from kew.graph.driver.lock import lock_holder
from kew.linear import client as linear_client
from kew.run_store import SqliteRunStore

runner = CliRunner()
EPIC = "payments"
FAKE_KEY = "lin_api_outage_fixture_value"
_PROJECT = {"id": "prj-uuid-1", "name": EPIC}
_ISSUE_UUID = "00000000-0000-0000-0000-000000000001"


@pytest.fixture(autouse=True)
def _fixture_ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep ledger discovery and registries inside each test's own project."""
    from kew import ledger_identity

    find_git_entry = ledger_identity._find_git_entry

    def fixture_git_entry(start: Path) -> Path | None:
        entry = find_git_entry(start)
        return entry if entry is not None and entry.is_relative_to(tmp_path) else None

    monkeypatch.setattr(ledger_identity, "_find_git_entry", fixture_git_entry)
    (tmp_path / ".git").mkdir(exist_ok=True)
    monkeypatch.setattr("kew.cli.project_root", lambda: tmp_path)
    monkeypatch.setenv("LINEAR_API_KEY", FAKE_KEY)


def _config(tmp_path: Path) -> DispatchConfig:
    cfg = DispatchConfig()
    cfg.project.repo = "o/r"
    cfg.logging.dir = str(tmp_path)
    cfg.tracker.type = "linear"
    return cfg


def _graph_response(query: str) -> dict[str, Any]:
    """Answer the graph-build queries for a one-node armed Linear project."""
    if "query Projects" in query:
        return {"data": {"projects": {"nodes": [_PROJECT]}}}
    if "query ProjectIssues" in query:
        issue = {
            "id": _ISSUE_UUID,
            "identifier": "SFD-1",
            "title": "armed node",
            "url": "https://linear.app/kew/issue/SFD-1",
            "state": {"name": "Todo", "type": "unstarted"},
            "labels": {"nodes": [{"name": "kew"}, {"name": "kew:ready"}]},
            "children": {
                "nodes": [],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            },
        }
        page = {"hasNextPage": False, "endCursor": None}
        return {"data": {"project": {"issues": {"nodes": [issue], "pageInfo": page}}}}
    # The aliased by-UUID relations batch.
    return {
        "data": {
            "i0": {
                "identifier": "SFD-1",
                "relations": {"nodes": []},
                "inverseRelations": {"nodes": []},
            }
        }
    }


def _gql_errors(message: str, code: str | None = None) -> bytes:
    error: dict[str, Any] = {"message": message}
    if code:
        error["extensions"] = {"code": code}
    return json.dumps({"errors": [error]}).encode()


# Canned Linear/edge responses. Several echo the key to prove redaction.
_CANNED: dict[str, tuple[int, bytes]] = {
    "503": (503, b"<html>503 Service Temporarily Unavailable</html>"),
    "500": (500, f"<html>\n500 Internal Server Error {FAKE_KEY}\n</html>".encode()),
    "500json": (500, _gql_errors("Internal server error", "INTERNAL_SERVER_ERROR")),
    "520": (520, b"<html>error code: 520 Web server is returning an unknown error"),
    "429html": (429, b"<html><h1>429 Too Many Requests</h1></html>"),
    "401": (
        401,
        _gql_errors(f"Authentication required {FAKE_KEY}", "AUTHENTICATION_ERROR"),
    ),
    "unexpected": (200, _gql_errors("Cannot query field 'projectz' on type 'Query'")),
}


class _Response:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body


class _Socket:
    """Stands in for ``http.client.HTTPSConnection``; ``fail`` decides, per
    GraphQL query, which failure (if any) the socket layer produces."""

    def __init__(self, fail: Any) -> None:
        self._fail = fail
        self.connects = 0
        self.queries: list[str] = []

    def __call__(self, host: str, timeout: float) -> _Socket:
        assert host == "api.linear.app"
        self._query = ""
        return self

    def connect(self) -> None:
        self.connects += 1
        outcome = self._fail("connect", None)
        if outcome == "tls":
            raise ssl.SSLError(
                1, "[SSL: SSLV3_ALERT_HANDSHAKE_FAILURE] handshake failure"
            )
        if outcome:
            raise socket.gaierror(8, "nodename nor servname provided, or not known")

    def request(self, method: str, path: str, body: bytes, headers: Any) -> None:
        self._query = json.loads(body)["query"]
        self.queries.append(self._query)

    def getresponse(self) -> _Response:
        outcome = self._fail("response", self._query)
        if outcome == "timeout":
            raise TimeoutError("The read operation timed out")
        if outcome in _CANNED:
            status, body = _CANNED[outcome]
            return _Response(status, body)
        return _Response(200, json.dumps(_graph_response(self._query)).encode())

    def close(self) -> None:
        return None


def _dns_down(phase: str, query: str | None) -> Any:
    return phase == "connect"


def _read_timeout(phase: str, query: str | None) -> Any:
    return "timeout" if phase == "response" else None


def _gateway_503(phase: str, query: str | None) -> Any:
    return "503" if phase == "response" else None


def _facts_read_timeout(phase: str, query: str | None) -> Any:
    """The 2026-09-14 shape: the graph builds, then the facts read times out."""
    if phase == "response" and query and "query IssueFacts" in query:
        return "timeout"
    return None


def _invoke(tmp_path: Path, fail: Any, args: list[str]) -> tuple[Any, _Socket, list]:
    cfg = _config(tmp_path)
    sock = _Socket(fail)
    sleeps: list[float] = []
    bounded_client = functools.partial(linear_client.LinearClient, sleep=sleeps.append)
    with (
        patch("kew.cli.load_config", return_value=cfg),
        patch("kew.cli.preflight_tools"),
        patch("kew.graph.driver.tick.preflight_tools"),
        patch("kew.cli.epic._driver_repo_host", return_value="github.com"),
        patch("kew.cli._sleep") as poll_sleep,
        patch.object(linear_client, "LinearClient", bounded_client),
        patch.object(linear_client.http.client, "HTTPSConnection", sock),
    ):
        invoked = runner.invoke(epic_app, args)
    assert poll_sleep.call_count == 0, "an outage must never enter the poll loop"
    return invoked, sock, sleeps


def _assert_clean_outage(
    invoked: Any, tmp_path: Path, *, exit_code: int, label: str = "linear-unavailable"
) -> None:
    output = invoked.output
    assert invoked.exit_code == exit_code, output
    # Clean exit: typer.Exit, not an escaped exception rendered as a traceback.
    assert invoked.exception is None or isinstance(invoked.exception, SystemExit), repr(
        invoked.exception
    )
    assert "Traceback" not in output
    assert f"epic {EPIC}: {label} — " in output
    # Whitespace-free, so console wrapping cannot split the key past the check.
    assert FAKE_KEY not in "".join(output.split())
    # Nothing admitted, reserved, claimed or journaled.
    with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
        assert store.count_epic_ticks(EPIC) == 0
        assert list(store.list_epic_reservations(EPIC)) == []
        assert list(store.list_by_item("SFD-1")) == []
    assert lock_holder(tmp_path / f"epic-driver-{EPIC}.lock") is None


@pytest.mark.parametrize(
    "fail",
    [
        pytest.param(_dns_down, id="dns-resolution-error"),
        pytest.param(_read_timeout, id="read-timeout"),
        pytest.param(_gateway_503, id="http-503"),
        pytest.param(_facts_read_timeout, id="facts-read-timeout-mid-tick"),
    ],
)
def test_advance_linear_outage_exits_2_classified(tmp_path: Path, fail: Any) -> None:
    invoked, sock, sleeps = _invoke(tmp_path, fail, ["advance", EPIC])

    _assert_clean_outage(invoked, tmp_path, exit_code=2)
    # No Linear write was attempted.
    assert not any(q.lstrip().startswith("mutation") for q in sock.queries)
    # Bounded retry: three attempts on the failing call, backoff between only.
    assert sleeps == [1.0, 2.0]
    assert sock.connects >= 3


def test_mid_tick_facts_timeout_retries_the_facts_read(tmp_path: Path) -> None:
    """The graph builds, then the facts read times out three times; the tick
    exits classified rather than inferring facts from the missing response."""
    invoked, sock, _sleeps = _invoke(tmp_path, _facts_read_timeout, ["advance", EPIC])

    _assert_clean_outage(invoked, tmp_path, exit_code=2)
    assert sum("query IssueFacts" in q for q in sock.queries) == 3
    assert any("query ProjectIssues" in q for q in sock.queries)


def test_advance_dns_error_is_retried_without_sending(tmp_path: Path) -> None:
    """A DNS failure happens before any byte is sent, so no query reaches
    Linear and the outcome is definite (not an ambiguous mutation)."""
    invoked, sock, sleeps = _invoke(tmp_path, _dns_down, ["advance", EPIC])

    _assert_clean_outage(invoked, tmp_path, exit_code=2)
    assert sock.queries == []
    assert sleeps == [1.0, 2.0]
    assert "nodename nor servname" in invoked.output


@pytest.mark.parametrize(
    "fail",
    [
        pytest.param(_dns_down, id="dns-resolution-error"),
        pytest.param(_read_timeout, id="read-timeout"),
        pytest.param(_gateway_503, id="http-503"),
    ],
)
def test_run_linear_outage_exits_2_without_polling(tmp_path: Path, fail: Any) -> None:
    invoked, _sock, _sleeps = _invoke(tmp_path, fail, ["run", EPIC])

    _assert_clean_outage(invoked, tmp_path, exit_code=2)


@pytest.mark.parametrize(
    "fail",
    [
        pytest.param(_dns_down, id="dns-resolution-error"),
        pytest.param(_gateway_503, id="http-503"),
        pytest.param(_facts_read_timeout, id="facts-read-timeout"),
    ],
)
def test_status_linear_outage_exits_2_classified(tmp_path: Path, fail: Any) -> None:
    """`kew epic status` reads the same sources; an outage there is the same
    classified exit, not a traceback."""
    invoked, sock, _sleeps = _invoke(tmp_path, fail, ["status", EPIC])

    assert invoked.exit_code == 2, invoked.output
    assert invoked.exception is None or isinstance(invoked.exception, SystemExit), repr(
        invoked.exception
    )
    assert "Traceback" not in invoked.output
    assert f"epic {EPIC}: linear-unavailable" in invoked.output
    assert FAKE_KEY not in invoked.output
    assert not any(q.lstrip().startswith("mutation") for q in sock.queries)


def _connect_fails(outcome: str) -> Any:
    def fail(phase: str, query: str | None) -> Any:
        return outcome if phase == "connect" else None

    return fail


def _every_response(outcome: str) -> Any:
    def fail(phase: str, query: str | None) -> Any:
        return outcome if phase == "response" else None

    return fail


# (failure, diagnostic label, retried within the client's budget?)
_CLASSIFIED = [
    pytest.param(_every_response("500"), "linear-unavailable", True, id="http-500"),
    pytest.param(
        _every_response("500json"), "linear-unavailable", True, id="http-500-json"
    ),
    pytest.param(
        _every_response("520"), "linear-unavailable", True, id="cloudflare-520"
    ),
    pytest.param(
        _every_response("429html"), "linear-unavailable", True, id="edge-429-html"
    ),
    pytest.param(_connect_fails("tls"), "linear-error", False, id="tls-handshake"),
    pytest.param(_every_response("401"), "linear-auth", False, id="http-401"),
    pytest.param(
        _every_response("unexpected"), "linear-error", False, id="unexpected-api-error"
    ),
]


@pytest.mark.parametrize("command", ["advance", "run", "status"])
@pytest.mark.parametrize(("fail", "label", "retried"), _CLASSIFIED)
def test_every_linear_failure_is_a_classified_exit_2(
    tmp_path: Path, command: str, fail: Any, label: str, retried: bool
) -> None:
    """#2020/#1117: no Linear failure reaches the operator as a traceback.
    Outages are retried then `linear-unavailable`; TLS, auth and other hard API
    errors fail fast with a one-line `linear-auth` / `linear-error`."""
    invoked, sock, sleeps = _invoke(tmp_path, fail, [command, EPIC])

    _assert_clean_outage(invoked, tmp_path, exit_code=2, label=label)
    assert not any(q.lstrip().startswith("mutation") for q in sock.queries)
    assert sleeps == ([1.0, 2.0] if retried else [])
    assert sock.connects == (3 if retried else 1)


def test_linear_rate_limit_json_429_keeps_rate_limit_handling(tmp_path: Path) -> None:
    """Linear's own JSON RATELIMITED 429 stays on the rate-limit path, not the
    outage classification, and still ends as a classified exit."""
    _CANNED["429json"] = (429, _gql_errors("Rate limit exceeded", "RATELIMITED"))
    try:
        invoked, _sock, sleeps = _invoke(
            tmp_path, _every_response("429json"), ["advance", EPIC]
        )
    finally:
        del _CANNED["429json"]

    _assert_clean_outage(invoked, tmp_path, exit_code=2, label="linear-error")
    assert "rate limit not clearing" in invoked.output
    assert sleeps == [1.0, 2.0]
