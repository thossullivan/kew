"""Complete proxy encodings and fixed vocabulary survive the correct boundaries."""

import base64
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.parse import quote, quote_plus, unquote, unquote_plus

import pytest
from support.agent_report import visible_text

from kew.config import DispatchConfig
from kew.orchestrator import _apply_classification
from kew.progress import _dim, _error, _log, _success, _warn, display_event
from kew.runner_fields import (
    REDACTED_IDENTITY,
    reported_identity,
    reported_model_identity,
    require_public_model,
    runner_text,
    service_tier_token,
)
from kew.runners.protocol import ResultClassification
from kew.sandbox import CONTAINER_PROXY_NAMES, OOMProbeStatus, ResourceCapCode
from kew.tracking import DispatchResult
from kew.transcript_metrics import StreamDerivation, write_derived_evidence

PROXY = "HTTP://user:Proxy%2BSecret+123@PROXY.invalid/~?q=~~"
NORMALIZED_PROXY = "http://user:Proxy%2BSecret+123@proxy.invalid/~?q=~~"
MARKER = "<HTTP_PROXY redacted>"


def _value_marker(value):
    return MARKER


@pytest.fixture(autouse=True)
def _clean_proxies(monkeypatch):
    for name in CONTAINER_PROXY_NAMES:
        monkeypatch.delenv(name, raising=False)


def _encode(value, encoding):
    raw = value.encode()
    return {
        "base64": base64.b64encode(raw).decode(),
        "base64-unpadded": base64.b64encode(raw).decode().rstrip("="),
        "urlsafe": base64.urlsafe_b64encode(raw).decode(),
        "urlsafe-unpadded": base64.urlsafe_b64encode(raw).decode().rstrip("="),
        "hex": raw.hex(),
        "HEX": raw.hex().upper(),
        "percent": quote(value, safe=""),
        "plus": quote_plus(value, safe=""),
    }[encoding]


@pytest.mark.parametrize(
    "value",
    [PROXY, unquote(PROXY), unquote_plus(PROXY), NORMALIZED_PROXY],
    ids=["configured", "percent-decoded", "plus-decoded", "normalized-url"],
)
@pytest.mark.parametrize(
    "encoding",
    [
        "base64",
        "base64-unpadded",
        "urlsafe",
        "urlsafe-unpadded",
        "hex",
        "HEX",
        "percent",
        "plus",
    ],
)
@pytest.mark.parametrize(
    "surface", ["serialized_report", "publication_body", "live_display"]
)
def test_complete_proxy_encoding_is_removed(
    monkeypatch, tmp_path, capsys, value, encoding, surface
):
    from kew.dispatch_core import _post_agent_report

    monkeypatch.setenv("HTTP_PROXY", PROXY)
    encoded = _encode(value, encoding)
    report = f"## Dispatch Report\n{encoded}\nDone."
    expected = f"## Dispatch Report\n{MARKER}\nDone."
    config = DispatchConfig()
    config.sandbox.proxy_environment = "inherit"
    log = tmp_path / "dispatch.log"
    if surface == "live_display":
        display_event({"type": "result", "result": report})
        retained = capsys.readouterr().err
        expected = expected.replace("\n", " ")
    else:
        derivation = StreamDerivation()
        derivation.observe(json.dumps({"type": "result", "result": report}))
        path = write_derived_evidence(log, derivation.evidence())
        if surface == "serialized_report":
            retained = json.loads(path.read_text())["report"]
        else:
            with patch(
                "kew.dispatch_core.run", return_value=MagicMock(returncode=0)
            ) as post:
                _post_agent_report(
                    "https://github.com/example/repo/pull/1941", log, config
                )
            argv = post.call_args.args[0]
            retained = visible_text(argv[argv.index("--body") + 1])
    # Whole-value replacement also rejects hex/percent fragments left around
    # a password-only replacement, and checks redaction before display limits.
    assert expected in retained
    assert encoded not in retained
    assert "Proxy" not in retained


@pytest.mark.parametrize(
    "field, value",
    [
        ("error_category", "permanent"),
        ("error_category", "transient"),
        ("error_category", "budget_exceeded"),
        ("error_category", "watchdog_kill"),
        ("error_category", "transport_unavailable"),
        ("error_category", "needs_human"),
        ("error_category", "circuit_breaker"),
        ("error_category", "budget_stop"),
        ("error_category", "budget_stopped"),
        ("error_category", "killed"),
        ("error_category", "exception"),
        ("subtype", "success"),
        ("subtype", "error_max_budget_usd"),
        ("subtype", "proxy_lost"),
        ("stop_reason", "aborted"),
        ("stop_reason", "end_turn"),
        ("stop_reason", "completed"),
        ("stop_reason", "toolUse"),
        ("runner", "codex"),
        ("runner", "claude-code"),
        ("model", "claude-sonnet-4-6"),
        ("model", "gpt-5.6-sol"),
        ("service_tier", "fast"),
    ],
)
def test_fixed_identity_is_internal_but_display_is_redacted(monkeypatch, field, value):
    monkeypatch.setenv("HTTP_PROXY", f"http://user:{value}@proxy.invalid")
    _assert_identity_boundary(value, field)
    if field in ("error_category", "subtype", "stop_reason"):
        result = DispatchResult(exit_code=2, issue_num=1933, log_file=None)
        kwargs = {"category" if field == "error_category" else field: value}
        _apply_classification(
            result, ResultClassification(**{"category": "permanent", **kwargs})
        )
        assert getattr(result, field) == value
    elif field == "model":
        assert reported_model_identity(value) == REDACTED_IDENTITY
        with pytest.raises(ValueError, match="model identity conflicts"):
            require_public_model(value)
    elif field == "service_tier":
        assert service_tier_token(value) == value


@pytest.mark.parametrize("emit", [_log, _warn, _error, _success, _dim])
def test_fixed_category_display_boundary(monkeypatch, capsys, emit):
    monkeypatch.setenv("HTTP_PROXY", "http://user:permanent@proxy.invalid")
    emit("category: permanent")
    retained = capsys.readouterr().err
    assert "permanent" not in retained
    assert MARKER in retained


@pytest.mark.parametrize("surface", ["retry", "failure_record", "terminal_outcome"])
def test_terminal_category_collision_does_not_retry(
    monkeypatch, tmp_path, capsys, surface
):
    from test_dispatch_core import _profile_run

    from kew.dispatch_core import (
        _finalize_result,
        _run_retry_loop,
        _save_pending_run,
        _terminal_outcome,
    )
    from kew.run_store import SqliteRunStore

    monkeypatch.setenv("HTTP_PROXY", "http://user:permanent@proxy.invalid")
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        run = _profile_run(store, tmp_path)
        run.config.retry.max_attempts = 3
        run.config.sandbox.proxy_environment = "inherit"
        _save_pending_run(
            run, worker_cap=0, cap_exclude_issue=None, admit_budget_check=None
        )
        with (
            patch(
                "kew.orchestrator._run_process",
                return_value=(None, run.effective_model, 2),
            ) as launch,
            patch("kew.dispatch_core._check_existing_pr", return_value=(False, None)),
            patch("kew.dispatch_core._admit_attempt_route_profile"),
            patch("kew.dispatch_core.time.sleep"),
        ):
            outcome = _run_retry_loop(
                run,
                prompt="task",
                worktree_dir=tmp_path,
                budget_override_usd=None,
                timeout_override_minutes=None,
                should_pause=None,
                budget_check=None,
                anomaly_threshold_usd=None,
                anomaly_compaction_allowance_usd=0.0,
            )
        result = _finalize_result(run, outcome)
        record = store.get(run.run_id)
        terminal = _terminal_outcome(result, exit_code=1)
        if surface == "retry":
            assert launch.call_count == 1
        elif surface == "failure_record":
            assert record.error_category == "permanent"
        else:
            assert terminal.category == "permanent"
        _assert_terminal_failure(result, outcome, record, terminal)
        assert "permanent" not in capsys.readouterr().err + terminal.reason


@pytest.mark.parametrize("value", [kind.value for kind in OOMProbeStatus])
def test_oom_status_collision_keeps_structured_evidence(monkeypatch, value):
    monkeypatch.setenv("HTTP_PROXY", f"http://user:{value}@proxy.invalid")
    derivation = StreamDerivation()
    derivation.record_container_evidence(
        oom_probe_status=value,
        oom_probe_diagnostic=value,
        resource_cap_observations=None,
        resource_cap_observations_truncated=False,
    )
    evidence = derivation.evidence().as_json()
    assert evidence["oom_probe_status"] == value
    assert evidence["oom_probe_diagnostic"] == _value_marker(value)


@pytest.mark.parametrize("value", [kind.value for kind in ResourceCapCode])
def test_resource_code_collision_keeps_structured_evidence(monkeypatch, value):
    from kew.sandbox import redact_container_observations

    monkeypatch.setenv("HTTP_PROXY", f"http://user:{value}@proxy.invalid")
    observations = redact_container_observations([{"code": value, "reason": value}])
    derivation = StreamDerivation()
    derivation.record_container_evidence(
        oom_probe_status=None,
        oom_probe_diagnostic=None,
        resource_cap_observations=observations,
        resource_cap_observations_truncated=False,
    )
    evidence = derivation.evidence().as_json()
    assert evidence["resource_cap_observations"] == [
        {"code": value, "reason": _value_marker(value)}
    ]


@pytest.mark.parametrize(
    "category, status",
    [
        ("permanent", "failure"),
        ("transient", "failure"),
        ("needs_human", "failure"),
        ("budget_exceeded", "budget_exceeded"),
    ],
)
def test_category_collision_preserves_terminal_status(monkeypatch, category, status):
    from kew.dispatch_core import _apply_completion_status

    monkeypatch.setenv("HTTP_PROXY", f"http://user:{category}@proxy.invalid")
    result = DispatchResult(exit_code=0, issue_num=1933, log_file=None)
    _apply_classification(result, ResultClassification(category=category))
    stored = SimpleNamespace(status="running", error_category=None)
    _apply_completion_status(stored, result, 0)
    assert stored.status == status


@pytest.mark.parametrize(
    "category, status",
    [("permanent", "needs_human"), ("budget_exceeded", "budget_limited")],
)
def test_loop_failure_reason_redacts_internal_category(monkeypatch, category, status):
    from kew.loops.delegate import _pre_gate_status

    monkeypatch.setenv("HTTP_PROXY", f"http://user:{category}@proxy.invalid")
    result = DispatchResult(exit_code=2, issue_num=1933, log_file=None)
    _apply_classification(result, ResultClassification(category=category))
    outcome = _pre_gate_status(SimpleNamespace(max_steps=5), result, [])
    assert outcome == (status, f"run ended: {MARKER}")


def _assert_identity_boundary(value, field):
    assert reported_identity(value, field) == value
    assert runner_text(value) == _value_marker(value)
    # Exact membership is field-specific, not a general credential exemption.
    assert reported_identity(value, "tool_name") == REDACTED_IDENTITY
    assert reported_identity(f"prefix-{value}-suffix", field) == REDACTED_IDENTITY
    assert (
        reported_identity(base64.b64encode(value.encode()).decode(), field)
        == REDACTED_IDENTITY
    )


def _assert_terminal_failure(result, outcome, record, terminal):
    assert result.error_category == "permanent"
    assert outcome.last_exit_code == result.exit_code == record.exit_code == 2
    assert terminal.exit_code == 1
    assert outcome.total_attempts == record.attempt_number == 1
    assert record.status == "failure"
