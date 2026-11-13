"""Quoted authorization fields and classification precede credential redaction."""

import json
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from support.agent_report import visible_text

from kew.config import DispatchConfig
from kew.orchestrator import _finalize_agent_result
from kew.progress import display_event, summarize_tool_input
from kew.publication import sanitize_publication_evidence
from kew.runner_fields import exception_text, runner_text, sanitize_exception_args
from kew.runners.claude_code import ClaudeCodeRunner
from kew.runners.codex import CodexRunner
from kew.runners.lemonade import LemonadeRunner
from kew.runners.opencode import OpenCodeRunner
from kew.sandbox import CONTAINER_PROXY_NAMES
from kew.tracking import DispatchResult
from kew.transcript_metrics import StreamDerivation, write_derived_evidence


@pytest.fixture(autouse=True)
def _clean_proxies(monkeypatch):
    for name in CONTAINER_PROXY_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTP_PROXY", "http://user:ProxySecret123@proxy.invalid")


@pytest.mark.parametrize(
    "header",
    [
        r'{"Proxy-Authorization": "Basic dXNlcjpQcm94\r\n eVNlY3JldDEyMw=="}',
        '{"Proxy-Authorization": "Basic dXNlcjpQcm94\r\n eVNlY3JldDEyMw=="}',
        r"""{'authorization' : 'Basic dXNlcjpQcm94\r\n eVNlY3JldDEyMw=='}""",
        r'{"X-Authorization": "Digest nonce=\"dXNlcjpQcm94\\\r\n eVNlY3JldDEyMw==\""}',
        '{"Authorization"\r\n :\n "Basic dXNlcjpQcm94\r\n eVNlY3JldDEyMw=="}',
    ],
    ids=[
        "json-escaped-fold",
        "json-raw-fold",
        "single-quoted",
        "escaped-value",
        "json-whitespace",
    ],
)
@pytest.mark.parametrize("surface", ["report", "publication", "display"])
def test_quoted_headers_never_reach_retained_surfaces(
    tmp_path, capsys, header, surface
):
    from kew.dispatch_core import _post_agent_report

    report = (
        f'## Dispatch Report\n{header}\n"Authorization: Bearer opaque-token"\nDone.'
    )
    log = tmp_path / "dispatch.log"
    if surface == "display":
        display_event({"type": "result", "result": report})
        retained = capsys.readouterr().err
    else:
        derivation = StreamDerivation()
        derivation.observe(json.dumps({"type": "result", "result": report}))
        path = write_derived_evidence(log, derivation.evidence())
        if surface == "report":
            retained = path.read_text()
        else:
            with patch(
                "kew.dispatch_core.run", return_value=MagicMock(returncode=0)
            ) as post:
                _post_agent_report(
                    "https://github.com/example/repo/pull/1941", log, DispatchConfig()
                )
            argv = post.call_args.args[0]
            retained = visible_text(argv[argv.index("--body") + 1])
    assert "dXNlcjpQcm94" not in retained
    assert "eVNlY3JldDEyMw" not in retained
    assert "ProxySecret123" not in retained
    assert "user:ProxySecret123" not in retained
    assert "opaque-token" not in retained
    assert "<redacted>" in retained
    assert "Done." in retained


@pytest.mark.parametrize(
    "key", ["Authorization", "pRoXy-AuThOrIzAtIoN", "X-Authorization"]
)
@pytest.mark.parametrize(
    "surface", ["tool", "exception", "exception_args", "publication", "evidence"]
)
def test_structured_header_values_are_opaque(tmp_path, key, surface):
    value = {
        "nested": [{key: ["opaque-one", {"value": "opaque-two"}]}],
        "keep": "public",
    }
    if surface == "tool":
        retained = summarize_tool_input(value)
    elif surface == "exception":
        retained = exception_text(ValueError(value))
    elif surface == "exception_args":
        error = ValueError(value)
        sanitize_exception_args(error)
        retained = str(error)
    elif surface == "publication":
        retained = sanitize_publication_evidence(value)
    else:
        # Exercise the serialization boundary independently of schema filtering.
        evidence = SimpleNamespace(as_json=lambda: value)
        retained = write_derived_evidence(
            tmp_path / "dispatch.log", evidence
        ).read_text()
    assert "opaque-one" not in retained
    assert "opaque-two" not in retained
    assert key in retained
    assert "<redacted>" in retained
    assert "public" in retained
    assert value["nested"][0][key][0] == "opaque-one"


def _finalize(runner, last_output, exit_code):
    result = DispatchResult(exit_code=exit_code, issue_num=1933, log_file=None)
    _finalize_agent_result(
        result,
        runner=runner,
        config=DispatchConfig(),
        last_output=last_output,
        inference_transport=None,
        health_check=None,
        evidence=None,
        classifier_records=(),
        timeout_override_minutes=None,
    )
    return result


@pytest.mark.parametrize("surface", ["runner", "orchestrator", "publication"])
def test_codex_intervention_collision_blocks_success(monkeypatch, tmp_path, surface):
    from kew.dispatch_core import _collect_result

    monkeypatch.setenv("HTTP_PROXY", "http://user:KEW_NEEDS_HUMAN@proxy.invalid")
    runner = CodexRunner()
    events = runner.parse_line(
        json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "text": "KEW_NEEDS_HUMAN: decide KEW_NEEDS_HUMAN policy",
                },
            }
        )
    )
    final = json.dumps({"type": "turn.completed", "usage": {}})
    runner.parse_line(final)
    classified = runner.classify_result(
        exit_code=0, last_output=final, config=DispatchConfig()
    )
    if surface == "runner":
        assert classified.category == "needs_human"
    else:
        result = _finalize(runner, final, 0)
        if surface == "orchestrator":
            assert result.error_category == "needs_human"
        else:
            with (
                patch(
                    "kew.dispatch_core._root_checkout_integrity_check",
                    return_value=None,
                ),
                patch("kew.dispatch_core._collect_special_category", return_value=None),
                patch("kew.dispatch_core._collect_needs_human") as human,
                patch("kew.dispatch_core._collect_success") as publish,
            ):
                # Main's checkout generation binding (#1952) reads this field;
                # an unbound checkout skips it, as a fresh MagicMock cannot.
                ctx = MagicMock()
                ctx.checkout_generation = ""
                _collect_result(
                    ctx,
                    result=result,
                    worktree_dir=tmp_path,
                    run_sandboxed_path=False,
                )
            publish.assert_not_called()
            human.assert_called_once()
    assert "KEW_NEEDS_HUMAN" not in classified.subtype
    assert "KEW_NEEDS_HUMAN" not in runner._final_message
    assert "KEW_NEEDS_HUMAN" not in events[0].message
    assert "<HTTP_PROXY redacted>" in events[0].message
    # A new attempt must clear both the flag and the retained reason.
    runner.build_command(
        prompt="task", config=DispatchConfig(), env={}, model="gpt-5.6-sol"
    )
    runner.parse_line(final)
    assert (
        runner.classify_result(
            exit_code=0, last_output=final, config=DispatchConfig()
        ).category
        == ""
    )


@pytest.mark.parametrize("surface", ["runner", "orchestrator"])
def test_codex_permanence_precedes_redaction(monkeypatch, surface):
    monkeypatch.setenv("HTTP_PROXY", "http://user:config@proxy.invalid")
    runner = CodexRunner()
    final = json.dumps({"type": "turn.failed", "error": {"message": "config invalid"}})
    events = runner.parse_line(final)
    if surface == "runner":
        category = runner.classify_result(
            exit_code=1, last_output=final, config=DispatchConfig()
        ).category
    else:
        category = _finalize(runner, final, 1).error_category
    assert category == "permanent"
    assert "config" not in runner._last_error + events[0].message
    assert "<HTTP_PROXY redacted>" in events[0].message


@pytest.mark.parametrize(
    "runner_type, event, password, category, field",
    [
        (
            ClaudeCodeRunner,
            {"type": "result", "subtype": "custom_budget_refusal"},
            "budget",
            "budget_exceeded",
            "subtype",
        ),
        (
            OpenCodeRunner,
            {"type": "step_finish", "part": {"reason": "private-stop"}},
            "private-stop",
            "",
            "stop_reason",
        ),
        (
            LemonadeRunner,
            {
                "type": "message_end",
                "message": {"role": "assistant", "stopReason": "private-stop"},
            },
            "private-stop",
            "permanent",
            "stop_reason",
        ),
    ],
    ids=["claude-code", "opencode", "lemonade"],
)
def test_adapters_classify_original_then_redact_retained_fields(
    monkeypatch, runner_type, event, password, category, field
):
    monkeypatch.setenv("HTTP_PROXY", f"http://user:{password}@proxy.invalid")
    runner = runner_type()
    final = json.dumps(event)
    runner.parse_line(final)
    classified = runner.classify_result(
        exit_code=0, last_output=final, config=DispatchConfig()
    )
    assert classified.category == category
    assert password not in getattr(classified, field)
    assert "redacted" in getattr(classified, field)
    result = _finalize(runner, final, 0)
    assert result.error_category == category
    assert password not in runner_text(asdict(classified))
