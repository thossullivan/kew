"""Credential collisions and malformed protocol values cannot enter evidence."""

import base64
import json
from dataclasses import asdict
from io import StringIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.parse import quote

import pytest

from kew.config import DispatchConfig
from kew.orchestrator import _budget_tripped, _process_line, run_agent
from kew.runners import get_runner
from kew.sandbox import CONTAINER_PROXY_NAMES
from kew.tracking import DispatchResult
from kew.transcript_metrics import StreamDerivation, write_derived_evidence

SECRET = "ProxySecret123"
MODEL = "claude-sonnet-4-6"
MARKER = "<redacted-identity>"


@pytest.fixture(autouse=True)
def _clean_proxies(monkeypatch):
    for name in CONTAINER_PROXY_NAMES:
        monkeypatch.delenv(name, raising=False)


def _forms(secret):
    return {
        secret,
        quote(secret, safe=""),
        base64.b64encode(secret.encode()).decode(),
        base64.b64encode(secret.encode()).decode().rstrip("="),
        base64.urlsafe_b64encode(secret.encode()).decode().rstrip("="),
        secret.encode().hex(),
        secret.encode().hex().upper(),
        repr(secret)[1:-1],
        json.dumps(secret)[1:-1],
    }


def _assert_safe(text, secret):
    for form in _forms(secret):
        assert form not in text


@pytest.mark.parametrize("field", ["model", "service_tier"])
@pytest.mark.parametrize("encoding", ["raw", "base64", "hex", "embedded"])
@pytest.mark.parametrize("surface", ["record", "audit", "sidecar", "report"])
def test_reported_identity_retention_and_budget(
    monkeypatch, tmp_path, caplog, field, encoding, surface
):
    from test_dispatch_core import _profile_run

    from kew.dispatch_core import (
        _emit_completion_audit,
        _persist_completion,
        _RetryOutcome,
        _save_pending_run,
    )
    from kew.run_report_sink import export_run_report
    from kew.run_store import SqliteRunStore

    monkeypatch.setenv("HTTP_PROXY", f"http://user:{SECRET}@proxy.invalid")
    value = {
        "raw": SECRET,
        "base64": base64.b64encode(SECRET.encode()).decode().rstrip("="),
        "hex": SECRET.encode().hex(),
        "embedded": f"prefix-{SECRET}-suffix",
    }[encoding]
    payload = {
        "type": "turn.completed",
        field: value,
        "usage": {"input_tokens": 1000000, "output_tokens": 100000},
    }
    result = DispatchResult(exit_code=0, issue_num=1933, log_file=None)
    derivation = StreamDerivation()
    _, pricing_model = _process_line(
        json.dumps(payload),
        StringIO(),
        StringIO(),
        get_runner("codex"),
        result,
        MODEL,
        None,
        None,
        derivation,
    )
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        run = _profile_run(store, tmp_path, effective_model=MODEL)
        _save_pending_run(
            run, worker_cap=0, cap_exclude_issue=None, admit_budget_check=None
        )
        _persist_completion(
            run,
            result,
            _RetryOutcome(last_exit_code=0, total_attempts=1),
            result.resolved_model,
        )
        _emit_completion_audit(run, result, 0, 1, result.resolved_model)
        record = store.get(run.run_id)
        assert record is not None
        if surface == "record":
            retained = json.dumps(asdict(record), default=str)
        elif surface == "audit":
            retained = json.dumps(
                store.list_audit_events(event="dispatch_completed")[0].payload
            )
        elif surface == "sidecar":
            path = write_derived_evidence(tmp_path / "run.log", derivation.evidence())
            retained = path.read_text()
        else:
            run.config.run_reports.enabled = True
            run.config.run_reports.sink_path = str(tmp_path / "reports")
            path = export_run_report(record=record, config=run.config, store=store)
            assert path is not None
            retained = path.read_text()
    assert MARKER in retained
    _assert_safe(retained, SECRET)
    assert field in caplog.text
    _assert_safe(caplog.text, SECRET)
    proc = MagicMock()
    proc.poll.return_value = None
    assert _budget_tripped(result, pricing_model, None, lambda cost: cost > 0.1, proc)
    assert pricing_model == MODEL
    assert result.estimated_cost_usd == 4.5


@pytest.mark.parametrize(
    "runner_name, payload, diagnostic",
    [
        (
            "opencode",
            {"type": "step_finish", "part": {"tokens": {"input": "SECRET"}}},
            "tokens.input is not an integer",
        ),
        (
            "pi",
            {
                "type": "message_end",
                "message": {"role": "assistant", "usage": {"input": "SECRET"}},
            },
            "usage.input is not an integer",
        ),
        (
            "lemonade",
            {
                "type": "message_end",
                "message": {"role": "assistant", "usage": {"output": "SECRET"}},
            },
            "usage.output is not an integer",
        ),
        (
            "pi",
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "usage": {"input": 1, "cost": {"total": "SECRET"}},
                },
            },
            "usage.cost.total is not a number",
        ),
    ],
)
def test_numeric_protocol_failure_is_value_free(
    monkeypatch, tmp_path, runner_name, payload, diagnostic
):
    proxy = f"http://user:{SECRET}@proxy.invalid"
    monkeypatch.setenv("HTTP_PROXY", proxy)
    line = json.dumps(payload).replace("SECRET", proxy)
    runner = get_runner(runner_name)

    def stream(*args, **kwargs):
        runner.parse_line(line)

    log = tmp_path / "run.log"
    with (
        patch("kew.orchestrator._run_process", side_effect=stream),
        pytest.raises(ValueError) as failure,
    ):
        run_agent(
            runner=runner,
            prompt="task",
            worktree_dir=tmp_path,
            config=DispatchConfig(),
            issue_num=1933,
            model=MODEL,
            log_file_path=log,
            emit_events=True,
        )
    failed = json.loads(log.with_suffix(".events.jsonl").read_text().splitlines()[-1])
    assert failed["error"] == diagnostic
    _assert_safe(json.dumps(failed), SECRET)
    assert str(failure.value) == diagnostic


@pytest.mark.parametrize("shape", ["nested", "python_escaped", "json_escaped"])
@pytest.mark.parametrize(
    "surface", ["event", "retry_progress", "launch_reason", "smoke"]
)
def test_nested_claude_exception_is_sanitized_before_formatting(
    monkeypatch, tmp_path, capsys, surface, shape
):
    from test_dispatch_core import _profile_run

    from kew.dispatch_core import _launch_failed, _retry_exception_outcome

    password = "ab'cd\"ef\\gh"
    monkeypatch.setenv(
        "HTTP_PROXY", f"http://user:{quote(password, safe='')}@proxy.invalid"
    )
    error = {"message": password, "detail": [password]}
    exc = ValueError(
        {
            "nested": error,
            "python_escaped": repr(error),
            "json_escaped": json.dumps(error),
        }[shape]
    )
    if surface == "event":
        runner = get_runner("claude-code")
        runner.parse_line(json.dumps({"type": "error", "error": error}))
        log = tmp_path / "run.log"
        with (
            patch("kew.orchestrator._run_process", side_effect=exc),
            pytest.raises(ValueError),
        ):
            run_agent(
                runner=runner,
                prompt="task",
                worktree_dir=tmp_path,
                config=DispatchConfig(),
                issue_num=1933,
                model=MODEL,
                log_file_path=log,
                emit_events=True,
            )
        retained = json.loads(
            log.with_suffix(".events.jsonl").read_text().splitlines()[-1]
        )["error"]
    elif surface == "retry_progress":
        from kew.dispatch_core import _save_pending_run
        from kew.run_store import SqliteRunStore

        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            run = _profile_run(store, tmp_path)
            _save_pending_run(
                run, worker_cap=0, cap_exclude_issue=None, admit_budget_check=None
            )
            _retry_exception_outcome(run, exc, 1)
            retained = json.dumps(asdict(store.get(run.run_id)), default=str)
            retained += json.dumps(
                store.list_audit_events(event="dispatch_completed")[0].payload
            )
            retained += capsys.readouterr().err
    elif surface == "launch_reason":
        # Main's refusal delivery (#1943/#1952) hands the reason to the claim
        # callback and reads the reservation binding from the context.
        rollback_reasons: list[str] = []
        ctx = SimpleNamespace(
            config=DispatchConfig(),
            issue_num=1933,
            refuse_rollback=rollback_reasons.append,
            expected_reservation_run_id=None,
            admitted_run_id=None,
        )
        with (
            patch("kew.dispatch_core.mark_failed"),
            patch("kew.dispatch_core._emit_early_exit"),
        ):
            retained = _launch_failed(ctx, exc, SimpleNamespace()).early.reason
        # The retained-claim reason is a persisted payload as well.
        assert rollback_reasons
        for reason in rollback_reasons:
            _assert_safe(reason, password)
    else:
        from kew.sandbox import run_smoke

        with (
            patch("kew.sandbox.detect_runtime", return_value="/fake/runtime"),
            patch("kew.sandbox._ensure_smoke_image", return_value=None),
            patch("kew.sandbox.launch_container", side_effect=exc),
        ):
            retained = run_smoke(DispatchConfig().sandbox).detail
    _assert_safe(retained, password)
    assert "redacted" in retained


@pytest.mark.parametrize("field", ["model", "service_tier"])
def test_contained_identity_sidecar_is_safe(monkeypatch, tmp_path, field):
    from kew.dispatch_sandbox import (
        _accumulate_stream_line,
        _runner_totals,
        _SandboxTelemetry,
    )

    monkeypatch.setenv("HTTP_PROXY", f"http://user:{SECRET}@proxy.invalid")
    live = _SandboxTelemetry()
    _accumulate_stream_line(
        live,
        json.dumps(
            {"type": "turn.completed", field: SECRET, "usage": {"input_tokens": 99}}
        ),
        get_runner("codex"),
    )
    derivation = StreamDerivation()
    derivation.record_totals(_runner_totals(live))
    path = write_derived_evidence(tmp_path / "run.log", derivation.evidence())
    text = path.read_text()
    assert MARKER in text
    _assert_safe(text, SECRET)
    assert json.loads(text)["totals"]["input_tokens"] == 99


def test_provider_collision_does_not_enter_reconstructed_model(monkeypatch, caplog):
    monkeypatch.setenv("HTTP_PROXY", "http://user:openrouter@proxy.invalid")
    result = DispatchResult(exit_code=0, issue_num=1933, log_file=None)
    get_runner("hermes")._apply_usage(
        result, {"provider": "openrouter", "model": "vendor/model"}
    )
    assert result.resolved_model == MARKER
    assert "provider" in caplog.text
    assert "openrouter" not in caplog.text


def test_session_collision_records_field_warning_without_a_resume_handle(
    monkeypatch, caplog
):
    from kew.session_ids import capture_session_id

    session = "12345678-1234-1234-1234-123456789abc"
    monkeypatch.setenv("HTTP_PROXY", f"http://user:{session}@proxy.invalid")
    result = DispatchResult(exit_code=0, issue_num=1933, log_file=None)
    capture_session_id(result, session, "codex")
    assert MARKER in getattr(result, "session_id_warning", "")
    assert result.session_id is None
    assert "session_id" in caplog.text
    _assert_safe(result.session_id_rejection + caplog.text, session)


@pytest.mark.parametrize(
    "field", ["tool_name", "protocol_identifier", "subtype", "stop_reason"]
)
def test_other_retained_identifiers_are_safe(monkeypatch, caplog, field):
    from kew.orchestrator import _apply_classification
    from kew.runners.protocol import ResultClassification

    monkeypatch.setenv("HTTP_PROXY", f"http://user:{SECRET}@proxy.invalid")
    if field in ("subtype", "stop_reason"):
        result = DispatchResult(exit_code=1, issue_num=1933, log_file=None)
        _apply_classification(
            result, ResultClassification(category="permanent", **{field: SECRET})
        )
        retained = getattr(result, field)
    else:
        derivation = StreamDerivation()
        payload = (
            {"type": SECRET}
            if field == "protocol_identifier"
            else {
                "type": "assistant",
                "message": {
                    "usage": {"input_tokens": 1},
                    "content": [{"type": "tool_use", "name": SECRET}],
                },
            }
        )
        derivation.observe(json.dumps(payload))
        retained = json.dumps(derivation.evidence().as_json())
    assert MARKER in retained
    assert field in caplog.text
    _assert_safe(retained + caplog.text, SECRET)


@pytest.mark.parametrize("field", ["model", "provider", "tier"])
def test_proxy_metric_identifiers_do_not_bypass_retention_check(
    monkeypatch, caplog, field
):
    from kew.switchyard_metrics import parse_switchyard_metrics

    monkeypatch.setenv("HTTP_PROXY", f"http://user:{SECRET}@proxy.invalid")
    observation = parse_switchyard_metrics(
        f'switchyard_requests_total{{{field}="{SECRET}"}} 1\n'
    )
    retained = json.dumps(observation.to_json())
    assert MARKER in retained
    assert field in caplog.text
    _assert_safe(retained + caplog.text, SECRET)
