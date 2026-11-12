"""Round-nine regressions at credential normalization and retention boundaries."""

import base64
import json
from dataclasses import asdict
from io import StringIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import typer
from support.agent_report import visible_text

from kew.config import DispatchConfig
from kew.orchestrator import _process_line
from kew.runner_fields import REDACTED_IDENTITY, exception_text, sanitize_exception_args
from kew.runners import get_runner
from kew.sandbox import CONTAINER_PROXY_NAMES
from kew.tracking import DispatchResult
from kew.transcript_metrics import (
    StreamDerivation,
    _safe_token,
    write_derived_evidence,
)

SECRET = "ProxySecret123"
MODEL = "claude-sonnet-4-6"


@pytest.fixture(autouse=True)
def _proxy_credentials(monkeypatch):
    for name in CONTAINER_PROXY_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTP_PROXY", f"http://user:{SECRET}@proxy.invalid")


def _assert_secret_free(text, secret=SECRET):
    for value in (secret, f"user:{secret}"):
        for form in (
            value,
            repr(value)[1:-1],
            json.dumps(value)[1:-1],
            base64.b64encode(value.encode()).decode().rstrip("="),
            base64.urlsafe_b64encode(value.encode()).decode().rstrip("="),
            value.encode().hex(),
        ):
            assert form not in text


@pytest.mark.parametrize("surface", ["serialized_report", "publication_body", "error"])
@pytest.mark.parametrize(
    "header",
    [
        "Proxy-Authorization:\n Basic dXNlcjpQcm94\n eVNlY3JldDEyMw==",
        "Proxy-Authorization:\r\n\tBasic \t\r\n\t dXNlcjpQcm94 \t\r\n eVNlY3JldDEyMw==",
        'Proxy-Authorization: Digest realm = "x" , nonce = "y"',
        'Proxy-Authorization:\n Digest\n realm\n =\n "x"\n ,\n nonce\n =\n "y"',
        'Proxy-Authorization: Digest realm\t=\t"x"\t,\tnonce\t=\t"y"',
        "Proxy-Authorization: Digest realm = x , nonce = y",
        'Proxy-Authorization: Digest realm = "Proxy" , nonce = "Secret123"',
        'Proxy-Authorization: Digest realm = "dXNlcjpQcm94" , '
        'nonce = "eVNlY3JldDEyMw=="',
    ],
    ids=[
        "fold-before-scheme",
        "crlf-and-token-padding",
        "spaced-digest",
        "folded-digest",
        "tabbed-digest",
        "unquoted-digest",
        "raw-credential-fragments",
        "base64-credential-fragments",
    ],
)
def test_header_value_is_removed_before_report_publication(tmp_path, header, surface):
    from kew.dispatch_core import _post_agent_report
    from kew.publication import sanitize_publication_error

    # Same-line text belongs to the protected header value in round ten.
    tail = "\ndiagnostic survives"
    report = f"## Dispatch Report\n{header}{tail}\nDone."
    derivation = StreamDerivation()
    derivation.observe(json.dumps({"type": "result", "result": report}))
    log = tmp_path / "dispatch.log"
    path = write_derived_evidence(log, derivation.evidence())
    if surface == "serialized_report":
        retained = json.loads(path.read_text())["report"]
    elif surface == "publication_body":
        with patch(
            "kew.dispatch_core.run", return_value=MagicMock(returncode=0)
        ) as post:
            _post_agent_report(
                "https://github.com/example/repo/pull/1941", log, DispatchConfig()
            )
        argv = post.call_args.args[0]
        retained = visible_text(argv[argv.index("--body") + 1])
    else:
        retained = sanitize_publication_error(header + tail + " " + "z" * 800)
        assert len(retained) == 500
    expected_tail = tail.replace("\n", " ") if surface == "error" else tail
    assert "<redacted>" + expected_tail in retained
    assert "dXNlcjpQcm94" not in retained
    assert "eVNlY3JldDEyMw" not in retained
    assert "realm" not in retained
    assert "nonce" not in retained
    _assert_secret_free(retained)
    _assert_secret_free(path.read_text())


@pytest.mark.parametrize("surface", ["operational_log", "event_json", "sidecar"])
def test_normalized_tool_never_reaches_retained_streams(tmp_path, caplog, surface):
    log, events = StringIO(), StringIO()
    derivation = StreamDerivation()
    _process_line(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "usage": {"input_tokens": 1},
                    "content": [
                        {"type": "tool_use", "name": "Proxy/Secret123", "input": {}}
                    ],
                },
            }
        ),
        log,
        events,
        get_runner("claude-code"),
        DispatchResult(exit_code=0, issue_num=1933, log_file=None),
        MODEL,
        None,
        None,
        derivation,
    )
    if surface == "operational_log":
        retained = log.getvalue()
    elif surface == "event_json":
        retained = events.getvalue()
        tool_events = [json.loads(line) for line in retained.splitlines()]
        assert any(event.get("tool") == REDACTED_IDENTITY for event in tool_events)
    else:
        # Serialization previously hid the earlier collision. Inspect retained
        # evidence before serialization, then inspect the actual sidecar too.
        assert derivation.evidence().tool_uses == ((REDACTED_IDENTITY, 1),)
        retained = write_derived_evidence(
            tmp_path / "run.log", derivation.evidence()
        ).read_text()
    assert REDACTED_IDENTITY in retained
    assert "tool_name" in caplog.text
    _assert_secret_free(retained + caplog.text)


@pytest.mark.parametrize("field", ["tool_name", "model", "service_tier"])
def test_each_normalized_identifier_is_checked_before_retention(
    tmp_path, caplog, field
):
    from test_dispatch_core import _profile_run

    from kew.dispatch_core import _persist_completion, _RetryOutcome, _save_pending_run
    from kew.run_store import SqliteRunStore

    # The shared normalizer also serves identifiers from custom adapters.
    # Native model/tier validators keep their existing, stricter grammars.
    normalized = _safe_token("Proxy/Secret123", field)
    assert normalized == REDACTED_IDENTITY
    assert f"reported {field} conflicts" in caplog.text
    payload = (
        {
            "type": "assistant",
            "message": {
                "usage": {"input_tokens": 1},
                "content": [{"type": "tool_use", "name": normalized, "input": {}}],
            },
        }
        if field == "tool_name"
        else {"type": "turn.completed", field: normalized, "usage": {"input_tokens": 1}}
    )
    log, events = StringIO(), StringIO()
    derivation = StreamDerivation()
    result = DispatchResult(exit_code=0, issue_num=1933, log_file=None)
    _process_line(
        json.dumps(payload),
        log,
        events,
        get_runner("claude-code" if field == "tool_name" else "codex"),
        result,
        MODEL,
        None,
        None,
        derivation,
    )
    _assert_secret_free(log.getvalue())
    _assert_secret_free(events.getvalue())
    sidecar = write_derived_evidence(
        tmp_path / "run.log", derivation.evidence()
    ).read_text()
    assert REDACTED_IDENTITY in sidecar
    _assert_secret_free(sidecar)
    if field != "tool_name":
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
            record = store.get(run.run_id)
            assert record is not None
            retained = json.dumps(asdict(record), default=str)
        assert REDACTED_IDENTITY in retained
        _assert_secret_free(retained)
    _assert_secret_free(caplog.text)


def test_normalized_served_model_is_checked_before_selection(caplog):
    from kew.switchyard_metrics import parse_switchyard_metrics

    selected = parse_switchyard_metrics("").selection_evidence(
        served_model="Proxy\nSecret123"
    )
    assert selected.selected_model == REDACTED_IDENTITY
    assert "reported model conflicts" in caplog.text
    _assert_secret_free(json.dumps(selected.to_json()))
    _assert_secret_free(caplog.text)


@pytest.mark.parametrize("surface", ["preflight_result", "cli_message", "hold_reason"])
def test_binary_decode_failure_never_reaches_preflight_surfaces(
    real_runner_preflight, monkeypatch, tmp_path, capsys, caplog, surface
):
    from kew.cli import _preflight_runner
    from kew.graph.driver.review_launcher import sandbox_remediator
    from kew.graph.driver.review_stage import RemediationContext
    from kew.run_store import SqliteRunStore
    from kew.runner_preflight import runner_preflight_result

    buffer = b"proxy error: ProxySecret123\xff"
    exc = UnicodeDecodeError(
        "utf-8", buffer, len(buffer) - 1, len(buffer), "invalid start byte"
    )
    # Exercise the real text-mode probe, injecting its actual decoding error.
    monkeypatch.setattr(
        "kew.subprocess_utils.shutil.which", lambda _: "/synthetic/codex"
    )
    probe = MagicMock(side_effect=exc)
    monkeypatch.setattr("kew.subprocess_utils.subprocess.run", probe)
    config = DispatchConfig()
    config.agent.runner = "codex"
    if surface == "preflight_result":
        retained, warn = runner_preflight_result(config, runner_name="codex")
        assert retained is not None
        assert warn is False
    elif surface == "cli_message":
        with pytest.raises(typer.Exit) as refusal:
            _preflight_runner(config, runner_name="codex")
        # Main's two-axis exit scheme (#1943) reports a pre-launch refusal as
        # "did not run", exit 2; this test pins only the redaction.
        assert refusal.value.exit_code == 2
        retained = capsys.readouterr().err
    else:
        monkeypatch.setattr(
            "kew.graph.driver.review_launcher._load_sidecar_findings",
            # (sidecar_path, criterion_ids) since #2291.
            lambda *_: [SimpleNamespace()],
        )
        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            sandbox_remediator(config, store, epic_ref="review-1933")(
                RemediationContext(
                    item_id="1933",
                    issue_num=1933,
                    pr_number=1941,
                    head_sha="a" * 40,
                    review_round=1,
                    sidecar_path=None,
                )
            )
            [hold] = store.list_epic_holds("review-1933")
            retained = hold.reason
            assert retained.startswith("admission_refused:")
    _assert_secret_free(retained)
    _assert_secret_free(caplog.text)
    assert "invalid start byte" in retained
    assert probe.call_args.kwargs["text"] is True


@pytest.mark.parametrize("binary_type", [bytes, bytearray, memoryview])
@pytest.mark.parametrize("shape", ["argument", "nested", "key"])
@pytest.mark.parametrize("operation", ["format", "propagate"])
def test_binary_exception_arguments_are_sanitized(
    monkeypatch, binary_type, shape, operation
):
    password = "ab'cd\"ef\\gh"
    from urllib.parse import quote

    monkeypatch.setenv(
        "HTTP_PROXY", f"http://user:{quote(password, safe='')}@proxy.invalid"
    )
    value = binary_type(f"proxy error: {password}".encode() + b"\xff")
    # bytearray cannot be a dictionary key. Its bytes counterpart covers keys;
    # the original bytearray still appears as a nested value in that case.
    key = bytes(value) if binary_type is bytearray else value
    argument = {
        "argument": value,
        "nested": {"detail": [value, (value, ValueError(value))]},
        "key": {(key,): [value], "set": frozenset({key})},
    }[shape]
    exc = ValueError(argument)
    if operation == "format":
        retained = exception_text(exc)
    else:
        sanitize_exception_args(exc)
        retained = str(exc)
    _assert_secret_free(retained, password)
    assert "<HTTP_PROXY redacted>" in retained
    assert "proxy error:" in retained
