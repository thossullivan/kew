# tests/test_canary.py
import json
import os
import re
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kew.canary import (
    CONTRACTS,
    CanaryReport,
    PassthroughCanaryEvidence,
    RunnerOutcome,
    canary_checks,
    passthrough_canary_checks,
    reduce_run,
)
from kew.config import DispatchConfig, InferenceTransportConfig, SwitchyardConfig
from kew.health import CheckResult
from kew.runners.claude_code import ClaudeCodeRunner
from kew.runners.codex import CodexRunner
from kew.runners.opencode import OpenCodeRunner
from kew.runners.protocol import ResultClassification, StreamEvent

_FIXTURES = Path(__file__).parent / "fixtures" / "canary"
_SYNTHETIC_CODEX_THREAD = "00000000-0000-4000-8000-000000000001"


def _replay(runner, fixture_name):
    config = DispatchConfig()
    lines = (_FIXTURES / fixture_name).read_text().splitlines()
    events = []
    for line in lines:
        events.extend(runner.parse_line(line))
    last_output = next((ln for ln in reversed(lines) if ln.strip()), None)
    classification = runner.classify_result(
        exit_code=0, last_output=last_output, config=config
    )
    return reduce_run(events, classification)


def test_golden_claude_all_pass():
    fields = _replay(ClaudeCodeRunner(), "claude_code.jsonl")
    checks = canary_checks(fields, CONTRACTS["claude-code"], model="")
    # Every check must be exactly "pass" — a skip/transient on the golden fixture
    # is itself a regression (the trivial prompt should fully attest).
    assert all(c.status == "pass" for c in checks), checks


def test_golden_opencode_all_pass():
    fields = _replay(OpenCodeRunner(), "opencode.jsonl")
    checks = canary_checks(
        fields, CONTRACTS["opencode"], model="anthropic/claude-sonnet-4-6"
    )
    assert all(c.status == "pass" for c in checks), checks


def test_codex_contract_uses_session_and_derived_cost():
    assert CONTRACTS["codex"].needs_session is True
    assert CONTRACTS["codex"].needs_native_cost is False
    fields = _fields(model=None, session_id="00000000-0000-4000-8000-000000000001")
    checks = canary_checks(fields, CONTRACTS["codex"], model="gpt-5.6-sol")
    assert all(check.status == "pass" for check in checks)
    session = _by_name(checks)["session_captured"]
    assert session.detail == {"present": True}
    assert fields.session_id not in session.summary


@pytest.mark.parametrize("fixture_name", ["codex.jsonl", "codex-resume.jsonl"])
def test_golden_codex_live_evidence_passes(fixture_name):
    fields = _replay(CodexRunner(), fixture_name)
    checks = canary_checks(fields, CONTRACTS["codex"], model="gpt-5.6-sol")
    assert fields.input_tokens > 0
    assert fields.output_tokens > 0
    assert fields.session_id == _SYNTHETIC_CODEX_THREAD
    assert fields.classification == "success"
    assert fields.saw_result_event is True
    assert all(check.status == "pass" for check in checks), checks


@pytest.mark.parametrize("fixture_name", ["codex.jsonl", "codex-resume.jsonl"])
def test_golden_codex_live_evidence_is_sanitized(fixture_name):
    text = (_FIXTURES / fixture_name).read_text()
    payloads = [json.loads(line) for line in text.splitlines()]
    assert all(isinstance(payload, dict) for payload in payloads)
    uuids = set(
        re.findall(
            r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
            r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b",
            text,
        )
    )
    assert uuids == {_SYNTHETIC_CODEX_THREAD}
    lowered = text.lower()
    for forbidden in (
        "/users/",
        "/var/folders/",
        "worktree",
        "authorization",
        "api_key",
        "access_token",
        "workspace",
        "request_id",
    ):
        assert forbidden not in lowered


def test_canary_runner_factory_supports_codex():
    from kew.cli import _canary_runner

    assert isinstance(_canary_runner("codex"), CodexRunner)


def test_codex_session_deletion_is_bounded_and_boolean_only():
    from kew.canary_live import delete_codex_session

    completed = subprocess.CompletedProcess(["codex"], 0, stdout="secret", stderr="")
    with patch("kew.canary_live.run", return_value=completed) as invoke:
        check = delete_codex_session("00000000-0000-4000-8000-000000000001")
    assert check.status == "pass"
    assert check.detail == {"deleted": True}
    assert "00000000" not in check.summary
    assert invoke.call_args.kwargs["timeout"] == 5
    assert invoke.call_args.kwargs["capture"] is True
    assert invoke.call_args.kwargs["check"] is False


def test_codex_session_deletion_timeout_is_sanitized_failure():
    from kew.canary_live import delete_codex_session

    with patch(
        "kew.canary_live.run",
        side_effect=subprocess.TimeoutExpired(["codex", "delete"], 5),
    ):
        check = delete_codex_session("00000000-0000-4000-8000-000000000001")
    assert check.status == "fail"
    assert check.detail == {"deleted": False}


def test_codex_resume_timeout_reaps_and_drains_stdout(tmp_path):
    from kew.canary_live import _run_resumed_codex

    proc = MagicMock()
    proc.communicate.side_effect = [
        subprocess.TimeoutExpired(["codex", "exec", "resume"], 120),
        ("partial output", None),
    ]
    with (
        patch("kew.canary_live.validate_codex_project_tree"),
        patch("kew.canary_live.popen", return_value=proc),
        patch("kew.canary_live._terminate_and_reap") as reap,
        pytest.raises(RuntimeError, match="codex resume timed out"),
    ):
        _run_resumed_codex(
            CodexRunner(),
            DispatchConfig(),
            "gpt-5.6-sol",
            "00000000-0000-4000-8000-000000000001",
            tmp_path,
            {},
        )
    reap.assert_called_once_with(proc)
    assert proc.communicate.call_count == 2


def test_codex_resume_capture_waits_for_turn_start():
    from kew.canary_live import _CaptureState, _consume_fresh_line

    state = _CaptureState(lines=[])
    runner = CodexRunner()
    _consume_fresh_line(
        state,
        '{"type":"thread.started","thread_id":"00000000-0000-4000-8000-000000000001"}',
        runner,
    )
    assert state.session_id is not None
    assert state.turn_started is False
    _consume_fresh_line(state, '{"type":"turn.started"}', runner)
    assert state.turn_started is True


def test_codex_resume_record_preserves_direct_mode_directory_creation(tmp_path):
    from kew.canary_live import _write_resume_record

    record_dir = tmp_path / "records"

    _write_resume_record(str(record_dir), ["direct-raw"])

    assert (record_dir / "codex-resume.jsonl").read_text() == "direct-raw\n"


def _pass(name="tokens_present"):
    return CheckResult(name, "pass", "", {})


def _fail(name="tokens_present"):
    return CheckResult(name, "fail", "", {})


def test_exit_all_pass_is_zero():
    r = CanaryReport(
        [
            RunnerOutcome("claude-code", True, [_pass()]),
            RunnerOutcome("opencode", True, [_pass()]),
        ]
    )
    assert r.exit_code == 0


def test_exit_drift_is_one_even_with_couldnt_run():
    r = CanaryReport(
        [
            RunnerOutcome("opencode", True, [_fail()]),
            RunnerOutcome("claude-code", False, [], "binary missing"),
        ]
    )
    assert r.exit_code == 1  # drift outranks incomplete


def test_couldnt_run_auth_failure():
    cls = ResultClassification(category="", session_id=None)
    fields = _fields(input_tokens=0, output_tokens=0, saw_result_event=True)
    from kew.cli import _canary_couldnt_run

    assert _canary_couldnt_run(exit_code=1, classification=cls, fields=fields)


def test_couldnt_run_nonzero_exit_with_tokens_not_couldnt_run():
    cls = ResultClassification(category="", session_id=None)
    fields = _fields(input_tokens=12, output_tokens=3, saw_result_event=True)
    from kew.cli import _canary_couldnt_run

    assert not _canary_couldnt_run(exit_code=1, classification=cls, fields=fields)


def test_exit_partial_couldnt_run_is_two():
    r = CanaryReport(
        [
            RunnerOutcome("claude-code", True, [_pass()]),
            RunnerOutcome("opencode", False, [], "unauthed"),
        ]
    )
    assert r.exit_code == 2


def test_skip_checks_do_not_fail():
    r = CanaryReport(
        [
            RunnerOutcome(
                "claude-code",
                True,
                [_pass(), CheckResult("cost_captured", "skip", "", {})],
            )
        ]
    )
    assert r.exit_code == 0


def _claude_events():
    return [
        StreamEvent(
            raw_line="", event_type="init", metadata={"model": "claude-sonnet-4-6"}
        ),
        StreamEvent(raw_line="", event_type="usage", input_tokens=12, output_tokens=3),
        StreamEvent(raw_line="", event_type="step", input_tokens=12, output_tokens=3),
        StreamEvent(
            raw_line="",
            event_type="result",
            metadata={"session_id": "sess_abc", "stop_reason": "end_turn"},
        ),
    ]


def test_reduce_run_claude_shape():
    cls = ResultClassification(category="", session_id="sess_abc")
    f = reduce_run(_claude_events(), cls)
    assert f.input_tokens == 12 and f.output_tokens == 3  # step not double-counted
    assert f.session_id == "sess_abc"
    assert f.classification == "success"  # "" -> success
    assert f.model == "claude-sonnet-4-6"  # from init event
    assert f.native_cost_usd is None  # claude has no native cost
    assert f.saw_result_event is True


def test_reduce_run_opencode_native_cost():
    events = [
        StreamEvent(raw_line="", event_type="usage", input_tokens=20, output_tokens=5),
        StreamEvent(raw_line="", event_type="step", input_tokens=20, output_tokens=5),
        StreamEvent(
            raw_line="",
            event_type="result",
            metadata={
                "session_id": "ses_xyz",
                "stop_reason": "stop",
                "cost_usd": 0.0012,
            },
        ),
    ]
    cls = ResultClassification(category="", session_id="ses_xyz")
    f = reduce_run(events, cls)
    assert f.native_cost_usd == 0.0012
    assert f.model is None  # opencode emits no init/model
    assert f.classification == "success"


def test_reduce_run_preserves_transient():
    cls = ResultClassification(category="transient", session_id=None)
    f = reduce_run(
        [StreamEvent(raw_line="", event_type="usage", input_tokens=1, output_tokens=1)],
        cls,
    )
    assert f.classification == "transient"
    assert f.saw_result_event is False


def _fields(**kw):
    base = dict(
        input_tokens=12,
        output_tokens=3,
        cache_creation_tokens=0,
        cache_read_tokens=0,
        session_id="s1",
        native_cost_usd=None,
        classification="success",
        model="claude-sonnet-4-6",
        saw_result_event=True,
    )
    base.update(kw)
    from kew.canary import GovernanceFields

    return GovernanceFields(**base)


def _by_name(checks):
    return {c.name: c for c in checks}


def test_checks_all_pass_claude():
    checks = _by_name(
        canary_checks(_fields(), CONTRACTS["claude-code"], model="claude-sonnet-4-6")
    )
    assert checks["tokens_present"].status == "pass"
    assert checks["session_captured"].status == "pass"
    assert checks["cost_captured"].status == "pass"  # derived cost > 0
    assert checks["result_classified"].status == "pass"


def test_tokens_fail_on_zero_output():
    checks = _by_name(
        canary_checks(
            _fields(output_tokens=0),
            CONTRACTS["claude-code"],
            model="claude-sonnet-4-6",
        )
    )
    assert checks["tokens_present"].status == "fail"


def test_session_fail_on_empty():
    checks = _by_name(
        canary_checks(
            _fields(session_id=None),
            CONTRACTS["claude-code"],
            model="claude-sonnet-4-6",
        )
    )
    assert checks["session_captured"].status == "fail"


def test_opencode_cost_uses_native():
    f = _fields(model=None, native_cost_usd=0.0012)
    checks = _by_name(
        canary_checks(f, CONTRACTS["opencode"], model="anthropic/claude-sonnet-4-6")
    )
    assert checks["cost_captured"].status == "pass"
    f0 = _fields(model=None, native_cost_usd=0.0)
    checks0 = _by_name(
        canary_checks(f0, CONTRACTS["opencode"], model="anthropic/claude-sonnet-4-6")
    )
    assert checks0["cost_captured"].status == "fail"  # vendor dropped 'cost'


def test_cost_skip_on_free_model():
    checks = _by_name(
        canary_checks(
            _fields(model="ollama/qwen3"),
            CONTRACTS["claude-code"],
            model="ollama/qwen3",
        )
    )
    assert checks["cost_captured"].status == "skip"


def test_result_classified_fail_on_permanent():
    checks = _by_name(
        canary_checks(
            _fields(classification="permanent"),
            CONTRACTS["claude-code"],
            model="claude-sonnet-4-6",
        )
    )
    assert checks["result_classified"].status == "fail"


def test_result_classified_skip_on_transient():
    checks = _by_name(
        canary_checks(
            _fields(classification="transient"),
            CONTRACTS["claude-code"],
            model="claude-sonnet-4-6",
        )
    )
    assert checks["result_classified"].status == "skip"


def test_switchyard_passthrough_checks_cover_required_evidence():
    fields = _fields(tool_uses=1)
    checks = passthrough_canary_checks(
        fields,
        CONTRACTS["codex"],
        evidence=PassthroughCanaryEvidence(
            proxy_healthy=True,
            cancellation_observed=True,
            resume_completed=True,
            cleanup_complete=True,
        ),
    )

    assert {check.name for check in checks} == {
        "tokens_present",
        "session_captured",
        "result_classified",
        "tools_exercised",
        "proxy_healthy",
        "cancellation_observed",
        "resume_completed",
        "cleanup_complete",
    }
    assert all(check.status == "pass" for check in checks)
    assert "cost_captured" not in {check.name for check in checks}


@pytest.mark.parametrize(
    ("field", "check_name"),
    [
        ("proxy_healthy", "proxy_healthy"),
        ("cancellation_observed", "cancellation_observed"),
        ("resume_completed", "resume_completed"),
        ("cleanup_complete", "cleanup_complete"),
    ],
)
def test_switchyard_passthrough_lifecycle_failures_are_drift(field, check_name):
    evidence = {
        "proxy_healthy": True,
        "cancellation_observed": True,
        "resume_completed": True,
        "cleanup_complete": True,
    }
    evidence[field] = False

    checks = passthrough_canary_checks(
        _fields(tool_uses=1),
        CONTRACTS["codex"],
        evidence=PassthroughCanaryEvidence(**evidence),
    )

    assert _by_name(checks)[check_name].status == "fail"


def test_switchyard_passthrough_requires_tool_evidence():
    checks = passthrough_canary_checks(
        _fields(tool_uses=0),
        CONTRACTS["codex"],
        evidence=PassthroughCanaryEvidence.all_pass(),
    )

    assert _by_name(checks)["tools_exercised"].status == "fail"


def test_switchyard_metrics_fixture_is_offline_proxy_evidence():
    from kew.switchyard_metrics import parse_switchyard_metrics

    fixture = Path(__file__).parent / "fixtures" / "switchyard" / "metrics.txt"
    observation = parse_switchyard_metrics(fixture.read_text())

    assert observation.scope == "process_wide_proxy"
    assert {sample.family for sample in observation.samples} >= {
        "requests",
        "model_selection",
        "tokens",
    }


def test_switchyard_record_directory_must_preexist_and_be_owner_only(tmp_path):
    from kew.canary_live import CanaryRecordError, validate_canary_record_directory

    missing = tmp_path / "missing"
    with pytest.raises(CanaryRecordError, match="must already exist"):
        validate_canary_record_directory(str(missing))

    record_dir = tmp_path / "records"
    record_dir.mkdir(mode=0o700)
    assert validate_canary_record_directory(str(record_dir)) == record_dir.resolve()

    os.chmod(record_dir, 0o750)
    with pytest.raises(CanaryRecordError, match="owner-only"):
        validate_canary_record_directory(str(record_dir))


def test_switchyard_record_writer_keeps_raw_bytes_private(tmp_path):
    from kew.canary_live import write_canary_record

    record_dir = tmp_path / "records"
    record_dir.mkdir(mode=0o700)

    target = write_canary_record(record_dir, "codex-resume.jsonl", ["raw-secret"])

    assert target.read_text() == "raw-secret\n"
    assert target.stat().st_mode & 0o777 == 0o600


def test_switchyard_record_writer_does_not_reclose_owned_descriptor(tmp_path):
    from kew.canary_live import write_canary_record

    record_dir = tmp_path / "records"
    record_dir.mkdir(mode=0o700)
    temporary = record_dir / ".record.tmp"
    temporary.touch()
    output = MagicMock()
    output.__enter__.return_value = output
    output.write.side_effect = OSError("write failed")

    with (
        patch(
            "kew.canary_live.tempfile.mkstemp",
            return_value=(123, str(temporary)),
        ),
        patch("kew.canary_live.os.fdopen", return_value=output),
        patch("kew.canary_live.os.close") as close,
        pytest.raises(OSError, match="write failed"),
    ):
        write_canary_record(record_dir, "codex-resume.jsonl", ["raw-secret"])

    close.assert_not_called()
    assert not temporary.exists()


def test_switchyard_record_writer_closes_descriptor_when_fdopen_fails(tmp_path):
    from kew.canary_live import write_canary_record

    record_dir = tmp_path / "records"
    record_dir.mkdir(mode=0o700)
    temporary = record_dir / ".record.tmp"
    temporary.touch()

    with (
        patch(
            "kew.canary_live.tempfile.mkstemp",
            return_value=(123, str(temporary)),
        ),
        patch("kew.canary_live.os.fdopen", side_effect=OSError("fdopen failed")),
        patch("kew.canary_live.os.close") as close,
        pytest.raises(OSError, match="fdopen failed"),
    ):
        write_canary_record(record_dir, "codex-resume.jsonl", ["raw-secret"])

    close.assert_called_once_with(123)
    assert not temporary.exists()


def test_switchyard_raw_evidence_parsing_is_silent(capsys):
    from kew.canary_live import _CaptureState, _consume_fresh_line

    raw_line = json.dumps(
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "private-output"}]},
        }
    )
    state = _CaptureState(lines=[])

    _consume_fresh_line(state, raw_line, ClaudeCodeRunner())

    assert state.lines == [raw_line]
    output = capsys.readouterr()
    assert "private-output" not in output.err
    assert "private-output" not in output.out


def _switchyard_canary_config() -> DispatchConfig:
    return DispatchConfig(
        inference_transport=InferenceTransportConfig(
            mode="switchyard",
            switchyard=SwitchyardConfig(
                endpoint="http://127.0.0.1:4000",
                route="switchyard/passthrough",
                expected_version="6ff491acd7eb1ec239525a1baa015730c3d3564a",
            ),
        )
    )


def test_switchyard_resumed_raw_evidence_parsing_is_silent(capsys, tmp_path):
    from kew.canary_live import _run_resumed_passthrough

    raw_lines = [
        json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "private-output"}]},
            }
        ),
        json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "session_id": "s1",
                "stop_reason": "end_turn",
                "result": "private-result",
                "usage": {"input_tokens": 12, "output_tokens": 3},
            }
        ),
    ]
    proc = MagicMock(returncode=0)
    proc.communicate.return_value = ("\n".join(raw_lines), None)

    with patch("kew.canary_live.popen", return_value=proc):
        _, recorded, resumed = _run_resumed_passthrough(
            ClaudeCodeRunner(),
            _switchyard_canary_config(),
            "switchyard/passthrough",
            "s1",
            tmp_path,
            {},
        )

    assert recorded == raw_lines
    assert resumed is True
    output = capsys.readouterr()
    assert "private-output" not in output.err
    assert "private-result" not in output.err
    assert "private-output" not in output.out
    assert "private-result" not in output.out


@pytest.mark.parametrize("runner", [ClaudeCodeRunner(), OpenCodeRunner()])
def test_switchyard_process_env_filters_credentials_for_every_runner(
    monkeypatch, runner
):
    from kew.canary_live import _passthrough_process_env

    monkeypatch.setenv("ANTHROPIC_API_KEY", "private-provider-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "private-cloud-key")
    monkeypatch.setenv("KEW_CANARY_SAFE_SETTING", "preserved")

    environment = _passthrough_process_env(runner)

    assert "ANTHROPIC_API_KEY" not in environment
    assert "AWS_SECRET_ACCESS_KEY" not in environment
    assert environment["KEW_CANARY_SAFE_SETTING"] == "preserved"


def test_switchyard_cleanup_skips_record_without_raw_evidence(tmp_path):
    from kew.canary_live import _cleanup_passthrough, _PassthroughState

    record_dir = tmp_path / "records"
    record_dir.mkdir(mode=0o700)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    state = _PassthroughState(temp_dir=checkout, raw_lines=[])

    with patch("kew.canary_live.write_canary_record") as write_record:
        _cleanup_passthrough(state, ClaudeCodeRunner(), record_dir)

    write_record.assert_not_called()
    assert state.cleanup_complete is True
    assert not checkout.exists()


def test_switchyard_live_harness_reduces_only_sanitized_offline_evidence(tmp_path):
    from kew.canary_live import run_switchyard_passthrough_canary
    from kew.subprocess_utils import RunnerReachability

    record_dir = tmp_path / "records"
    record_dir.mkdir(mode=0o700)
    reachability = RunnerReachability(
        "codex", "codex", True, True, version="0.145.0", version_supported=True
    )
    with (
        patch("kew.canary_live.inspect_runner", return_value=reachability),
        patch("kew.canary_live._proxy_observable", side_effect=[True, True]),
        patch("kew.canary_live._initialize_resume_repo"),
        patch(
            "kew.canary_live._interrupt_passthrough_runner",
            return_value=("s1", ["fresh-private"], True),
        ),
        patch(
            "kew.canary_live._run_resumed_passthrough",
            return_value=(_fields(tool_uses=1), ["resume-private"], True),
        ),
        patch(
            "kew.canary_live.delete_codex_session",
            return_value=CheckResult("session_deleted", "pass", "deleted", {}),
        ),
    ):
        outcome = run_switchyard_passthrough_canary(
            CodexRunner(),
            _switchyard_canary_config(),
            "gpt-5.6-sol",
            record_dir=str(record_dir),
        )

    assert outcome.ran is True
    assert all(check.status == "pass" for check in outcome.checks)
    assert "private" not in repr(outcome)
    record = record_dir / "codex-passthrough.jsonl"
    assert record.read_text() == "fresh-private\nresume-private\n"
    assert record.stat().st_mode & 0o777 == 0o600
