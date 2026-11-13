"""Round 23: verification policy wiring and bounded nested proxy decoding."""

import base64
import json
import subprocess
from unittest.mock import Mock

import pytest
from test_docs_truthfulness import _assert_proxy_redaction_configuration, _read
from test_loops_verification import _check_contained_published
from test_loops_verification import contained_command as contained_command
from test_proxy_round20 import _agent_launch, _container_launch
from test_proxy_round21 import _retain
from test_proxy_round22 import TEXT_SURFACES

from kew import sandbox, subprocess_utils
from kew.config import SandboxConfig
from kew.loops import verification

MARKER = "<HTTP_PROXY redacted>"
CONTROLS = "## Dispatch Report\nerror_category: transient\nexit code 7\n"


@pytest.fixture(autouse=True)
def _clean_proxies(monkeypatch):
    for name in sandbox._PROXY_BUILD_NAMES:
        monkeypatch.delenv(name, raising=False)


def _nested_percent(value, depth):
    encoded = "".join(f"%{byte:02x}" for byte in value.encode())
    for _ in range(depth - 1):
        encoded = encoded.replace("%", "%25")
    return encoded


def _prepare_contained(gate, policy):
    gate.prepare_verification(
        SandboxConfig(
            enabled=True,
            images={"opencode": "approved:tag"},
            proxy_environment=policy,
        ),
        runner="opencode",
    )


def _launch_environment(calls):
    [argv] = [argv for argv, _ in calls if argv[:2] == ["docker", "run"]]
    return [argv[index + 1] for index, arg in enumerate(argv) if arg == "-e"]


@pytest.mark.parametrize("policy", ["inherit", "none"])
def test_round23_contained_verification_uses_admitted_policy(
    contained_command, tmp_path, monkeypatch, policy
):
    condition, gate, calls, completed, host = contained_command
    monkeypatch.setenv("HTTP_PROXY", "https://proxy.invalid/gateway")
    _prepare_contained(gate, policy)
    completed.code = 7
    completed.stdout = "gateway failure"
    completed.stderr = "exit code 7"
    result = _check_contained_published(condition, tmp_path)
    environment = _launch_environment(calls)
    # Contained verification carries --network none, so the admitted policy is
    # attested but never reaches its argv: every name is blanked, whatever the
    # operator configured (governed review round 4, 2026-09-21).
    assert "HTTP_PROXY=" in environment
    assert "HTTP_PROXY" not in environment
    # The admitted policy still scopes redaction, so an inherited proxy's path
    # component is replaced in retained evidence; only the argv stopped
    # carrying the value.
    expected = f"{MARKER} failure" if policy == "inherit" else "gateway failure"
    assert expected in result.evidence
    assert not result.met
    for name in sandbox._PROXY_BUILD_NAMES:
        assert f"{name}=" in environment
        assert name not in environment
    assert len(environment) == 11
    assert condition.admission.proxy_environment == policy
    assert (
        condition.admission.record_fields()["verification_proxy_environment"] == policy
    )
    host.assert_not_called()
    assert sandbox._LAUNCH_PROXY_VALUES.get() is None


@pytest.mark.parametrize("policy", ["inherit", "none"])
@pytest.mark.parametrize("published", [False, True], ids=["checkout", "canonical"])
def test_round23_host_verification_keeps_inherited_policy(
    tmp_path, monkeypatch, policy, published
):
    monkeypatch.setenv("HTTP_PROXY", "https://proxy.invalid/gateway")
    monkeypatch.setattr(sandbox, "detect_runtime_cached", Mock(return_value="docker"))
    monkeypatch.setattr(
        verification,
        "resolve_runner_image",
        Mock(side_effect=sandbox.SandboxImageUnmapped("fixture image missing")),
    )
    condition = verification.CommandCondition("./verify.sh")
    gate = verification.GateEvaluator([condition], guarded_paths=[])
    gate.prepare_verification(
        SandboxConfig(
            enabled=False,
            dangerously_skip_containment=True,
            proxy_environment=policy,
        ),
        runner="opencode",
    )
    run = Mock(return_value=subprocess.CompletedProcess([], 7, "gateway failure", ""))
    monkeypatch.setattr(subprocess_utils, "run", run)
    if published:
        monkeypatch.setattr(
            verification, "provision", Mock(return_value=Mock(path=tmp_path))
        )
        monkeypatch.setattr(verification, "fetch_commit", Mock())
        monkeypatch.setattr(
            verification,
            "_materialize_verification_checkout",
            Mock(return_value=subprocess.CompletedProcess([], 0, "", "")),
        )
        result = _check_contained_published(condition, tmp_path)
    else:
        result = condition.check(worktree=tmp_path)
    assert f"{MARKER} failure" in result.evidence
    assert not result.met
    assert run.call_args.args[0] == ["sh", "-c", "./verify.sh"]
    assert "env" not in run.call_args.kwargs
    assert condition.admission.record_fields().get(
        "verification_proxy_environment"
    ) == ("inherit")
    assert sandbox._LAUNCH_PROXY_VALUES.get() is None


@pytest.mark.parametrize("surface", TEXT_SURFACES)
@pytest.mark.parametrize("depth", [2, 3])
def test_round23_nested_components_leave_no_retained_spelling(
    monkeypatch, tmp_path, capsys, caplog, surface, depth
):
    spellings = ["s3cr3t", *(_nested_percent("s3cr3t", n) for n in range(1, depth + 1))]
    monkeypatch.setenv("HTTP_PROXY", f"https://proxy.invalid/{spellings[-1]}")
    report = CONTROLS + "\n".join(f"proxy echoed: {value}" for value in spellings)
    with sandbox.proxy_redaction_scope("inherit"):
        retained = _retain(report, surface, tmp_path, capsys, caplog)
    assert retained == CONTROLS + "\n".join(
        f"proxy echoed: {MARKER}" for _ in spellings
    )
    assert not any(value in retained for value in spellings)


@pytest.mark.parametrize("depth", [2, 3])
@pytest.mark.parametrize(
    "launch", [_agent_launch, _container_launch], ids=["host", "container"]
)
def test_round23_nested_component_launch_persists_sanitized_evidence(
    tmp_path, monkeypatch, depth, launch
):
    monkeypatch.setenv(
        "HTTP_PROXY", f"https://proxy.invalid/{_nested_percent('s3cr3t', depth)}"
    )
    launch(tmp_path, monkeypatch, "exit code 7\nproxy echoed: s3cr3t")
    [path] = tmp_path.glob("*.evidence.json")
    report = json.loads(path.read_text())["report"]
    assert report == (f"## Dispatch Report\nexit code 7\nproxy echoed: {MARKER}\nDone.")
    assert "s3cr3t" not in path.read_text()


@pytest.mark.parametrize("depth", [2, 3])
def test_round23_nested_component_contained_verification(
    contained_command, tmp_path, monkeypatch, depth
):
    condition, gate, calls, completed, host = contained_command
    monkeypatch.setenv(
        "HTTP_PROXY", f"https://proxy.invalid/{_nested_percent('s3cr3t', depth)}"
    )
    _prepare_contained(gate, "inherit")
    completed.stdout = "proxy echoed: s3cr3t"
    result = _check_contained_published(condition, tmp_path)
    assert "s3cr3t" not in result.evidence
    assert MARKER in result.evidence
    environment = _launch_environment(calls)
    assert "HTTP_PROXY=" in environment
    assert "HTTP_PROXY" not in environment
    host.assert_not_called()


@pytest.mark.parametrize("depth", [2, 3])
@pytest.mark.parametrize(
    "location", ["query", "fragment", "path-assignment", "path-adjacent"]
)
def test_round23_nested_credential_name_selects_numeric_value(
    monkeypatch, depth, location
):
    key = _nested_percent("token", depth)
    suffix = {
        "query": f"/?{key}=1234",
        "fragment": f"/#{key}=1234",
        "path-assignment": f"/{key}=1234",
        "path-adjacent": f"/{key}/1234",
    }[location]
    monkeypatch.setenv("HTTP_PROXY", f"https://proxy.invalid{suffix}")
    with sandbox.proxy_redaction_scope("inherit"):
        retained = sandbox.redact_credentials("label token; value 1234; exit 7")
    label = MARKER if location == "path-adjacent" else "token"
    assert retained == f"label {label}; value {MARKER}; exit 7"


@pytest.mark.parametrize("depth", [2, 3])
def test_round23_nested_credential_name_selects_unscoped_value(monkeypatch, depth):
    key = _nested_percent("token", depth)
    monkeypatch.setenv("HTTP_PROXY", f"https://proxy.invalid/?{key}=s3cr3t")
    assert sandbox.redact_credentials("label token; value s3cr3t; exit 7") == (
        f"label token; value {MARKER}; exit 7"
    )


def test_round23_decoding_retains_intermediate_forms_and_stops_at_four():
    original = _nested_percent("s3cr3t", 5)
    stages = [original, *(_nested_percent("s3cr3t", n) for n in range(4, 0, -1))]
    engine = sandbox.credential_redaction
    forms = [("HTTP_PROXY", form) for form in engine.source_forms(original)]
    for stage in stages:
        for spelling in (
            stage,
            base64.b64encode(stage.encode()).decode(),
            stage.encode().hex(),
        ):
            assert engine.contains(spelling, forms)
    assert not engine.contains("s3cr3t", forms)
    inner = [("HTTP_PROXY", form) for form in engine.source_forms(stages[1])]
    assert engine.contains("s3cr3t", inner)


@pytest.mark.parametrize("depth", [2, 3])
def test_round23_nested_short_forms_preserve_boundaries_and_numeric_controls(
    monkeypatch, depth
):
    short = _nested_percent("ab", depth)
    numeric = _nested_percent("2", depth)
    monkeypatch.setenv("HTTP_PROXY", f"https://proxy.invalid/{short}/{numeric}")
    report = f"standalone ab; embedded X{short}Y; exit code 2; abandon"
    with sandbox.proxy_redaction_scope("inherit"):
        retained = sandbox.redact_credentials(report)
    assert retained == report.replace("standalone ab", f"standalone {MARKER}")


def test_round23_configuration_docs_bind_current_redaction_rules():
    _assert_proxy_redaction_configuration(_read("docs/configuration.md"))


@pytest.mark.parametrize(
    "claim",
    [
        "Matching proxy forms shorter than six characters replace the complete field.",
        "Any credential form appearing more than eight times "
        "also replaces the complete field.",
        "If a credential matches the fixed marker, the field becomes empty.",
        "Other matches use one substitution pass and retain surrounding text.",
    ],
)
def test_round23_configuration_docs_reject_restored_whole_field_claim(claim):
    text = _read("docs/configuration.md")
    _assert_proxy_redaction_configuration(text)
    mutated = text.replace(
        "<!-- proxy-redaction-rules:end -->",
        f"{claim}\n    <!-- proxy-redaction-rules:end -->",
    )
    assert mutated != text
    with pytest.raises(AssertionError, match="obsolete whole-field claim"):
        _assert_proxy_redaction_configuration(mutated)
