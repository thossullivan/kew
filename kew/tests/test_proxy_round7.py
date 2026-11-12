"""Regression proofs for nested text, pricing identity, smoke, and headers."""

import ast
import base64
import json
from io import StringIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.parse import quote

import pytest
from typer.testing import CliRunner

from kew.cli import app
from kew.config import DispatchConfig
from kew.orchestrator import _budget_tripped, _process_line, run_agent
from kew.progress import display_event
from kew.runners import get_runner
from kew.sandbox import CONTAINER_PROXY_NAMES, _build_env
from kew.tracking import DispatchResult
from kew.transcript_metrics import DerivedRunEvidence, StreamDerivation


@pytest.fixture(autouse=True)
def _clean_proxy_environment(monkeypatch):
    for name in CONTAINER_PROXY_NAMES:
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("password", ["ab'cd\"ef", "ab'cd\"ef\\gh"])
def test_nested_claude_error_is_not_literal_recoverable(monkeypatch, capsys, password):
    monkeypatch.setenv("HTTP_PROXY", f"http://user:{quote(password)}@proxy.invalid")
    get_runner("claude-code").parse_line(
        json.dumps({"type": "error", "error": {"message": password}})
    )
    stderr = capsys.readouterr().err
    literal = stderr.split("[error] ", 1)[1].strip()
    assert ast.literal_eval(literal)["message"] != password
    assert password not in stderr
    assert repr(password)[1:-1] not in stderr
    assert json.dumps(password)[1:-1] not in stderr
    assert "redacted" in stderr


@pytest.mark.parametrize("runner_name", ["claude-code", "opencode", "pi", "lemonade"])
@pytest.mark.parametrize("surface", ["text", "tool"])
def test_nested_adapter_fields_redact_before_conversion(
    monkeypatch, runner_name, surface
):
    password = "ab'cd\"ef\\gh"
    monkeypatch.setenv("HTTP_PROXY", f"http://user:{quote(password)}@proxy.invalid")
    nested = {"message": [password, {password: password}]}
    text_events = {
        "claude-code": {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": nested}]},
        },
        "opencode": {"type": "text", "part": {"text": nested}},
        "pi": {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": nested}],
            },
        },
    }
    text_events["lemonade"] = text_events["pi"]
    tool_events = {
        "claude-code": {
            "type": "tool_use",
            "tool": "Bash",
            "input": {"command": nested},
        },
        "opencode": {
            "type": "tool_use",
            "part": {
                "tool": "bash",
                "state": {"status": "completed", "input": {"command": nested}},
            },
        },
        "pi": {
            "type": "tool_execution_start",
            "toolName": "bash",
            "args": {"command": nested},
        },
    }
    tool_events["lemonade"] = tool_events["pi"]
    payload = (text_events if surface == "text" else tool_events)[runner_name]
    events = get_runner(runner_name).parse_line(json.dumps(payload))
    text = " ".join(
        event.message + str(event.metadata.get("target", "")) for event in events
    )
    assert repr(password)[1:-1] not in text
    assert "redacted" in text


def test_nested_usage_display_redacts_before_formatting(monkeypatch, capsys):
    password = "ab'cd\"ef\\gh"
    monkeypatch.setenv("HTTP_PROXY", f"http://user:{quote(password)}@proxy.invalid")
    display_event(
        {
            "type": "system",
            "subtype": "task_notification",
            "usage": {"total_tokens": 10, "tool_uses": {"message": password}},
        }
    )
    stderr = capsys.readouterr().err
    assert repr(password)[1:-1] not in stderr
    assert "10 tokens" in stderr


def _priced_stream(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://user:sonnet@proxy.invalid")
    result = DispatchResult(exit_code=0, issue_num=1933, log_file=None)
    parser = get_runner("claude-code")
    model = "claude-sonnet-4-6"
    for event in (
        {"type": "system", "subtype": "init", "model": model},
        {
            "type": "assistant",
            "message": {"usage": {"input_tokens": 1000000, "output_tokens": 100000}},
        },
    ):
        _, model = _process_line(
            json.dumps(event),
            StringIO(),
            None,
            parser,
            result,
            model,
            None,
            None,
            StreamDerivation(),
        )
    return result, model


@pytest.mark.parametrize("policy", ["none", "inherit"])
def test_budget_uses_original_pricing_identity(monkeypatch, capsys, policy):
    result, model = _priced_stream(monkeypatch)
    assert bool(_build_env(policy)["HTTP_PROXY"]) == (policy == "inherit")
    proc = MagicMock()
    proc.poll.return_value = None
    assert _budget_tripped(result, model, None, lambda cost: cost > 0.1, proc)
    assert result.estimated_cost_usd == 4.5
    assert result.resolved_model == "<redacted-identity>"
    assert model == "claude-sonnet-4-6"
    assert "sonnet" not in capsys.readouterr().err


def test_record_keeps_configured_identity_and_redacts_reported_collision(
    monkeypatch, tmp_path
):
    from test_dispatch_core import _profile_run

    from kew.dispatch_core import _persist_completion, _RetryOutcome, _save_pending_run
    from kew.run_store import SqliteRunStore

    result, model = _priced_stream(monkeypatch)
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        run = _profile_run(store, tmp_path, effective_model=model)
        _save_pending_run(
            run, worker_cap=0, cap_exclude_issue=None, admit_budget_check=None
        )
        _persist_completion(
            run,
            result,
            _RetryOutcome(last_exit_code=0, total_attempts=1),
            result.resolved_model,
        )
        record = store.get(run.run_id)
        assert record.model == "claude-sonnet-4-6"
        assert record.reported_model == "<redacted-identity>"


@pytest.mark.parametrize("password", ["sonnet", "claude-sonnet-4-6"])
@pytest.mark.parametrize("policy", ["none", "inherit"])
def test_model_credential_collision_refused_before_launch(
    monkeypatch, tmp_path, password, policy
):
    config = DispatchConfig()
    config.sandbox.proxy_environment = policy
    monkeypatch.setenv("HTTP_PROXY", f"http://user:{password}@proxy.invalid")
    with patch(
        "kew.orchestrator._run_process", return_value=(None, "claude-sonnet-4-6", 0)
    ) as launch:
        with pytest.raises(
            ValueError, match="model identity conflicts with host credentials"
        ):
            run_agent(
                runner=get_runner("claude-code"),
                prompt="task",
                worktree_dir=tmp_path,
                config=config,
                issue_num=1933,
                model="claude-sonnet-4-6",
            )
        launch.assert_not_called()
    assert not list(tmp_path.rglob("*.log"))


@pytest.mark.parametrize(
    "runner_name", ["claude-code", "codex", "pi", "lemonade", "hermes"]
)
def test_adapter_reported_identity_redacts_collision(monkeypatch, runner_name):
    monkeypatch.setenv("HTTP_PROXY", "http://user:sonnet@proxy.invalid")
    model = "claude-sonnet-4-6"
    payloads = {
        "claude-code": {"type": "system", "subtype": "init", "model": model},
        "codex": {"type": "turn.completed", "model": model, "usage": {}},
        "pi": {"type": "message_end", "message": {"role": "assistant", "model": model}},
    }
    payloads["lemonade"] = payloads["pi"]
    parser = get_runner(runner_name)
    if runner_name == "hermes":
        result = DispatchResult(exit_code=0, issue_num=1933, log_file=None)
        parser._apply_usage(result, {"model": model, "provider": "openrouter"})
        assert result.resolved_model == "<redacted-identity>"
    else:
        events = parser.parse_line(json.dumps(payloads[runner_name]))
        assert (
            next(
                event.metadata["model"] for event in events if "model" in event.metadata
            )
            == "<redacted-identity>"
        )


@pytest.mark.parametrize("mode", ["exit", "cutoff", "launch"])
def test_smoke_cli_redacts_container_diagnostics(monkeypatch, mode):
    value = "http://user:cat@proxy.invalid:3128"
    monkeypatch.setenv("HTTP_PROXY", value)
    config = DispatchConfig()
    config.sandbox.proxy_environment = "inherit"
    output = value + " dXNlcjpjYXQ= Y2F0"
    if mode == "cutoff":
        output = "x" * 195 + "user:cat " + output
    handle = MagicMock()
    handle.__iter__.return_value = iter([output])
    handle.wait.return_value = 1
    with (
        patch("kew.cli.load_config", return_value=config),
        patch("kew.sandbox.detect_runtime", return_value="/fake/runtime"),
        patch("kew.sandbox._ensure_smoke_image", return_value=None),
        patch(
            "kew.sandbox.launch_container",
            return_value=handle,
            side_effect=OSError(output) if mode == "launch" else None,
        ),
    ):
        result = CliRunner().invoke(app, ["sandbox", "smoke", "--no-build"])
    assert result.exit_code == 1
    assert "user:" not in result.stderr
    for secret in ("cat", "dXNlcjpjYXQ=", "Y2F0"):
        assert secret not in result.stderr
    # The cutoff may truncate a replacement marker, but never expose the secret.
    assert "<HTTP" in result.stderr
    assert "sandbox smoke FAILED" in result.stderr


@pytest.mark.parametrize("ok", [True, False])
def test_smoke_cli_redacts_detail_at_print_boundary(monkeypatch, ok):
    monkeypatch.setenv("HTTP_PROXY", "http://user:cat@proxy.invalid")
    with (
        patch("kew.cli.load_config", return_value=DispatchConfig()),
        patch(
            "kew.sandbox.run_smoke",
            return_value=SimpleNamespace(
                ok=ok, layer="execution", detail="user:cat Y2F0"
            ),
        ),
    ):
        result = CliRunner().invoke(app, ["sandbox", "smoke"])
    assert "user:cat" not in result.stderr
    assert "Y2F0" not in result.stderr


@pytest.mark.parametrize(
    "header",
    [
        "Proxy-Authorization: Basic dXNlcjpj\r\n YXQ=",
        "Authorization: Basic unknown\n\twrapped-token",
        'Proxy-Authorization: Digest username="user", '
        'response="opaque-response", nonce=opaque-nonce',
        'Authorization: Custom key=value, signature="opaque-response"',
    ],
)
def test_retained_report_redacts_complete_header(header):
    report = DerivedRunEvidence(
        report=f"## Dispatch Report\n{header}\nDone."
    ).as_json()["report"]
    assert report == f"## Dispatch Report\n{header.split(':', 1)[0]}: <redacted>\nDone."


@pytest.mark.parametrize(
    "encoding",
    [
        "base64",
        "base64-unpadded",
        "urlsafe",
        "urlsafe-unpadded",
        "hex",
        "upper-hex",
        "decoded-base64",
    ],
)
def test_short_malformed_proxy_encodings_redact(monkeypatch, encoding):
    value = "u:p@[" if encoding != "decoded-base64" else ":%70["
    monkeypatch.setenv("HTTP_PROXY", value)
    encoded = {
        "base64": base64.b64encode(value.encode()).decode(),
        "base64-unpadded": base64.b64encode(value.encode()).decode().rstrip("="),
        "urlsafe": base64.urlsafe_b64encode(value.encode()).decode(),
        "urlsafe-unpadded": base64.urlsafe_b64encode(value.encode())
        .decode()
        .rstrip("="),
        "hex": value.encode().hex(),
        "upper-hex": value.encode().hex().upper(),
        "decoded-base64": "OnBb",
    }[encoding]
    original = f"## Dispatch Report\n{encoded}"
    if encoding not in {"hex", "upper-hex"}:
        assert DerivedRunEvidence(report=original).as_json()["report"] == original
    report = DerivedRunEvidence(report=f"## Dispatch Report\n'{encoded}'").as_json()[
        "report"
    ]
    assert encoded not in report
    assert "redacted" in report.lower()


def test_result_totals_and_sidecar_redact_reported_collision(monkeypatch):
    from kew.dispatch_sandbox import (
        _accumulate_stream_line,
        _runner_totals,
        _SandboxTelemetry,
    )
    from kew.transcript_metrics import RunnerTotals

    monkeypatch.setenv("HTTP_PROXY", "http://user:sonnet@proxy.invalid")
    model = "claude-sonnet-4-6"
    live = _SandboxTelemetry()
    line = json.dumps(
        {"type": "result", "modelUsage": {model: {"inputTokens": 1000000}}}
    )
    _accumulate_stream_line(live, line, get_runner("claude-code"))
    assert live.reported_model == "<redacted-identity>"
    derivation = StreamDerivation()
    derivation.record_totals(_runner_totals(live))
    serialized = derivation.evidence().as_json()["totals"]
    assert serialized["reported_model"] == "<redacted-identity>"
    assert RunnerTotals.from_json(serialized).reported_model == "<redacted-identity>"


@pytest.mark.parametrize("model", ["bad model", "x" * 257, "ok\nmodel"])
def test_protocol_model_grammar_rejects_complete_invalid_value(model):
    for name in ("claude-code", "codex", "pi", "lemonade"):
        payloads = {
            "claude-code": {"type": "system", "subtype": "init", "model": model},
            "codex": {"type": "turn.completed", "model": model, "usage": {}},
            "pi": {
                "type": "message_end",
                "message": {"role": "assistant", "model": model},
            },
        }
        payloads["lemonade"] = payloads["pi"]
        events = get_runner(name).parse_line(json.dumps(payloads[name]))
        assert not any(event.metadata.get("model") for event in events)


def test_pricing_preflight_refuses_collision_before_registry(monkeypatch):
    from kew.dispatch_core import _preflight_pricing

    monkeypatch.setenv("HTTP_PROXY", "http://user:sonnet@proxy.invalid")
    ctx = SimpleNamespace(config=DispatchConfig(), model_override="")
    with patch("kew.dispatch_core.check_lifecycle", return_value=[]) as lifecycle:
        with pytest.raises(
            ValueError, match="model identity conflicts with host credentials"
        ):
            _preflight_pricing(ctx, SimpleNamespace(model="claude-sonnet-4-6"))
        lifecycle.assert_not_called()
