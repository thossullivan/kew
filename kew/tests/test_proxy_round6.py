"""Proxy secrets cannot alter accounting or escape through retained reports."""

import base64
import json
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest

from kew.config import DispatchConfig
from kew.credential_surface import redact_credentials
from kew.dispatch_core import _post_agent_report
from kew.dispatch_sandbox import _accumulate_stream_line, _SandboxTelemetry
from kew.orchestrator import _process_line, _should_trip_budget
from kew.readiness_probe import resolve_probe_route
from kew.run_store import SqliteRunStore
from kew.runners import get_runner
from kew.sandbox import CONTAINER_PROXY_NAMES, _build_env
from kew.tracking import DispatchResult
from kew.transcript_metrics import (
    DerivedRunEvidence,
    StreamDerivation,
    write_derived_evidence,
)


def _accounting_lines(runner):
    usage = {"input_tokens": 100000, "output_tokens": 1000}
    message = {
        "role": "assistant",
        "content": [{"type": "text", "text": "Work finished."}],
        "usage": usage,
        "stopReason": "stop",
    }
    if runner == "claude-code":
        events = [
            {"type": "assistant", "message": message},
            {"type": "result", "usage": usage},
        ]
    elif runner == "codex":
        events = [
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "Done."},
            },
            {"type": "turn.completed", "usage": usage},
        ]
    elif runner == "opencode":
        events = [
            {"type": "text", "part": {"text": "Done."}},
            {
                "type": "step_finish",
                "part": {
                    "reason": "stop",
                    "tokens": {"input": 100000, "output": 1000},
                },
            },
        ]
    else:
        message["usage"] = {"input": 100000, "output": 1000}
        events = [{"type": "message_end", "message": message}]
    return [json.dumps(event) for event in events]


@pytest.mark.parametrize("policy", ["none", "inherit"])
@pytest.mark.parametrize(
    "runner_name", ["claude-code", "codex", "opencode", "pi", "lemonade"]
)
def test_protocol_passwords_preserve_accounting(monkeypatch, policy, runner_name):
    config = DispatchConfig()
    config.sandbox.proxy_environment = policy
    for name in CONTAINER_PROXY_NAMES:
        monkeypatch.delenv(name, raising=False)
    # Exact names and longer passwords with protocol prefixes must both work.
    for word in ("type", "assistant", "result", "content", "usage"):
        for password in (word, word + "-93LongSecret!"):
            value = f"http://user:{password}@proxy.invalid:3128"
            monkeypatch.setenv("HTTP_PROXY", value)
            assert _build_env(config.sandbox.proxy_environment)["HTTP_PROXY"] == (
                value if policy == "inherit" else ""
            )
            lines = _accounting_lines(runner_name)
            parser = get_runner(runner_name)
            _assert_accounting_events(parser, lines, password)
            _assert_protocol_consumers(lines, runner_name)


@pytest.mark.parametrize(
    "surface", ["display", "events", "json", "sidecar", "publication"]
)
@pytest.mark.parametrize("kind", ["proxy-header", "header", "userinfo", "password"])
def test_authorization_secrets_never_reach_retained_surfaces(
    tmp_path,
    monkeypatch,
    capsys,
    surface,
    kind,
):
    monkeypatch.setenv("HTTP_PROXY", "http://user:cat@proxy.invalid:3128")
    token = "dXNlcjpjYXQ=" if kind != "password" else "Y2F0"
    values = {
        "proxy-header": f"Proxy-Authorization: Basic {token}",
        "header": f"Authorization: Bearer {token}",
        "userinfo": token,
        "password": token,
    }
    report = f"## Dispatch Report\n{values[kind]}\nDone."
    if kind == "password":
        # The whitespace-joined run includes the heading and does not decode to cat.
        assert redact_credentials(report) == report
        report = f"## Dispatch Report\n'{values[kind]}'\nDone."
    parser = get_runner("claude-code")
    if surface == "display":
        parser.parse_line(json.dumps({"type": "result", "result": report}))
        retained = capsys.readouterr().err
    elif surface == "events":
        events = parser.parse_line(json.dumps({"type": "assistant", "message": report}))
        retained = " ".join(event.message for event in events)
    elif surface == "json":
        retained = json.dumps(DerivedRunEvidence(report=report).as_json())
    else:
        derivation = StreamDerivation()
        derivation.observe(json.dumps({"type": "result", "result": report}))
        log = tmp_path / "dispatch.log"
        path = write_derived_evidence(log, derivation.evidence())
        assert path is not None
        retained = path.read_text()
        if surface == "publication":
            with patch(
                "kew.dispatch_core.run", return_value=MagicMock(returncode=0)
            ) as publish:
                _post_agent_report(
                    "https://github.com/example/repo/pull/1941", log, DispatchConfig()
                )
            argv = publish.call_args.args[0]
            retained = argv[argv.index("--body") + 1]
    assert token not in retained
    assert "redacted" in retained.lower()


@pytest.mark.parametrize("header", ["Authorization", "pRoXy-AuThOrIzAtIoN"])
def test_header_redaction_does_not_require_host_credentials(monkeypatch, header):
    for name in CONTAINER_PROXY_NAMES:
        monkeypatch.delenv(name, raising=False)
    for scheme in ("Basic", "Bearer", "Negotiate"):
        # Diagnostics outside a header need their own non-continuation line.
        text = f"error: {header}:\t{scheme} unknown+/token==\nrequest failed"
        retained = redact_credentials(text)
        assert "unknown+/token==" not in retained
        assert "request failed" in retained
        assert "redacted" in retained


def test_proxy_encodings_follow_current_host_values(monkeypatch):
    for value, userinfo, password in (
        ("http://user:cat@proxy.invalid", "user:cat", "cat"),
        ("user:p%3F%3F@proxy.invalid", "user:p??", "p??"),
    ):
        monkeypatch.setenv("HTTP_PROXY", value)
        for secret in (userinfo, password):
            for encode in (base64.b64encode, base64.urlsafe_b64encode):
                encoded = encode(secret.encode()).decode()
                for printed in (encoded, encoded.rstrip("=")):
                    if len(secret) < 6:
                        assert (
                            redact_credentials(f"echo {printed}") == f"echo {printed}"
                        )
                    else:
                        assert printed not in redact_credentials(f"echo {printed}")
                    assert printed not in redact_credentials(f"echo '{printed}'")
        assert password not in redact_credentials(password)
    monkeypatch.setenv("HTTP_PROXY", "http://user:dog@proxy.invalid")
    assert redact_credentials("Y2F0") == "Y2F0"


def test_unparseable_proxy_value_is_redacted_whole(monkeypatch):
    """A proxy value urlsplit rejects still carries its secret in free text; the
    whole value is redacted because its password cannot be located."""
    value = "http://user:sup3rsecret@[bad"
    monkeypatch.setenv("HTTP_PROXY", value)
    retained = redact_credentials(f"proxy configured as {value} for the run")
    assert "sup3rsecret" not in retained
    assert value not in retained
    encoded = base64.b64encode(value.encode()).decode("ascii")
    assert encoded not in redact_credentials(f"echo {encoded}")


@pytest.mark.parametrize(
    "runner_name", ["claude-code", "codex", "opencode", "pi", "lemonade", "hermes"]
)
def test_adapter_redacts_encoding_before_display_cutoff(monkeypatch, runner_name):
    monkeypatch.setenv("HTTP_PROXY", "http://user:cat@proxy.invalid")
    text = "x" * 195 + "dXNlcjpjYXQ="
    payloads = {
        "claude-code": {"type": "assistant", "message": text},
        "codex": {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": text},
        },
        "opencode": {"type": "text", "part": {"text": text}},
        "pi": {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": text}],
            },
        },
    }
    payloads["lemonade"] = payloads["pi"]
    line = text if runner_name == "hermes" else json.dumps(payloads[runner_name])
    events = get_runner(runner_name).parse_line(line)
    messages = [event.message for event in events if event.event_type == "activity"]
    assert messages
    assert "dXNl" not in " ".join(messages)
    assert messages[0].startswith("x" * 195 + "<HTTP")


def test_readiness_hmac_key_is_private_and_changes_fingerprints(tmp_path, monkeypatch):
    key_path = tmp_path / "config" / "readiness-hmac.key"
    monkeypatch.setenv("KEW_READINESS_HMAC_KEY_PATH", str(key_path))
    monkeypatch.setenv("HTTP_PROXY", "http://user:cat@proxy.invalid")
    config = DispatchConfig()
    config.sandbox.enabled = False
    config.sandbox.dangerously_skip_containment = True
    config.sandbox.proxy_environment = "inherit"
    db = tmp_path / "dispatch.db"
    with SqliteRunStore(str(db), project_root=tmp_path) as store:
        first = resolve_probe_route(
            config, complexity="complex", store=store, create_key=True
        )
        key = store.readiness_proxy_key()
        dump = "\n".join(store._conn.iterdump())
        _assert_private_readiness_key(key_path, key, dump, first)
    with SqliteRunStore(str(db), read_only=True) as store:
        assert store.readiness_proxy_key() == key
        assert (
            resolve_probe_route(config, complexity="complex", store=store).fingerprint
            == first.fingerprint
        )
        second_path = tmp_path / "other.key"
        second_path.write_text("ab" * 32)
        second_path.chmod(0o600)
        monkeypatch.setenv("KEW_READINESS_HMAC_KEY_PATH", str(second_path))
        second = resolve_probe_route(config, complexity="complex", store=store)
        # The key salts the attested proxy digest. Probe identity stays the
        # realized profile's fingerprint, which the readiness consumer can
        # reconstruct (governed review round 4, 2026-09-21; kew issue #1973).
        assert second.inherited_proxy_digest != first.inherited_proxy_digest
        assert second.proxy_material != first.proxy_material
        assert second.fingerprint == first.fingerprint == first.profile.fingerprint


def test_readiness_key_creation_is_atomic(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    path = tmp_path / "readiness-hmac.key"
    monkeypatch.setenv("KEW_READINESS_HMAC_KEY_PATH", str(path))

    # Independent stores represent concurrent dispatch processes on one host.
    def read_key(index):
        with SqliteRunStore(str(tmp_path / f"{index}.db")) as store:
            return store.readiness_proxy_key()

    with ThreadPoolExecutor(max_workers=8) as pool:
        keys = list(pool.map(read_key, range(16)))
    assert len(set(keys)) == 1
    assert path.read_text() == keys[0]
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("unsafe", ["symlink", "permissions", "empty"])
def test_readiness_rejects_unsafe_key_files(tmp_path, monkeypatch, unsafe):
    path = tmp_path / "readiness-hmac.key"
    target = tmp_path / "target"
    target.write_text("cd" * 32)
    target.chmod(0o600)
    if unsafe == "symlink":
        path.symlink_to(target)
    else:
        path.write_text("" if unsafe == "empty" else "cd" * 32)
        path.chmod(0o600 if unsafe == "empty" else 0o644)
    monkeypatch.setenv("KEW_READINESS_HMAC_KEY_PATH", str(path))
    with (
        SqliteRunStore(str(tmp_path / "dispatch.db")) as store,
        pytest.raises((OSError, ValueError)),
    ):
        store.readiness_proxy_key()
    assert target.read_text() == "cd" * 32


def test_readiness_key_uses_operator_config(tmp_path, monkeypatch):
    monkeypatch.delenv("KEW_READINESS_HMAC_KEY_PATH", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
        key = store.readiness_proxy_key()
    path = tmp_path / "config" / "kew" / "readiness-hmac.key"
    assert path.is_file()
    assert path.read_text() == key
    assert path.stat().st_mode & 0o777 == 0o600


def _assert_accounting_events(parser, lines, password):
    events = [event for line in lines for event in parser.parse_line(line)]
    assert [event.event_type for event in events] == [
        "activity",
        "usage",
        "step",
        "result",
    ], password
    usage = [event for event in events if event.event_type == "usage"]
    assert [(event.input_tokens, event.output_tokens) for event in usage] == [
        (100000, 1000),
    ]


def _assert_private_readiness_key(key_path, key, dump, first):
    assert key not in dump
    assert key_path.is_file()
    assert key_path.stat().st_mode & 0o777 == 0o600
    assert key_path.read_text() == key
    assert "readiness_proxy_salt" not in dump
    assert "cat" not in first.inherited_proxy_digest


def _assert_protocol_consumers(lines, runner_name):
    # Exercise both production consumers with the original bytes.
    result = DispatchResult(exit_code=0, issue_num=1933, log_file=None)
    live = _SandboxTelemetry()
    host_runner, sandbox_runner = (
        get_runner(runner_name),
        get_runner(runner_name),
    )
    derivation = StreamDerivation()
    contributed = []
    for line in lines:
        _process_line(
            line,
            StringIO(),
            None,
            host_runner,
            result,
            "claude-sonnet-4-6",
            None,
            None,
            derivation,
        )
        contributed.append(_accumulate_stream_line(live, line, sandbox_runner))
    assert any(contributed)
    assert (result.input_tokens, result.output_tokens) == (100000, 1000)
    assert (live.input_tokens, live.output_tokens) == (100000, 1000)
    assert derivation.lines_observed == len(lines)
    expected_type = json.loads(lines[-1])["type"]
    assert derivation.evidence().as_json()["terminal_type"] == expected_type
    if runner_name in {"claude-code", "codex"}:
        result.compute_cost("claude-sonnet-4-6")
        assert _should_trip_budget(
            result.estimated_cost_usd, lambda cost: cost > 0.1, None, result
        )
        assert result.budget_exceeded
