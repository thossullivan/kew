from pathlib import Path

import yaml

from kew.runners.codex import CODEX_CREDENTIAL_ENV_NAMES

WORKFLOW = Path(__file__).parent.parent.parent / ".github/workflows/canary.yml"


def _steps() -> list[dict]:
    document = yaml.safe_load(WORKFLOW.read_text())
    return document["jobs"]["canary"]["steps"]


def _step(name: str) -> dict:
    return next(step for step in _steps() if step.get("name") == name)


def test_checkout_does_not_persist_credentials():
    checkout = next(
        step
        for step in _steps()
        if step.get("uses", "").startswith("actions/checkout@")
    )
    assert checkout["with"]["persist-credentials"] is False


def test_codex_standard_and_resume_are_isolated_without_recording():
    standard = _step("Run Codex canary")
    resume = _step("Run Codex resume canary")
    assert "--runner codex" in standard["run"]
    assert "--exercise-resume" not in standard["run"]
    assert "--runner codex" in resume["run"]
    assert "--exercise-resume" in resume["run"]
    for step in (standard, resume):
        assert "--record" not in step["run"]
        assert "login status" not in step["run"]
        assert ".codex" not in step["run"]
        assert "source " not in step["run"]
        assert set(step["env"]) >= set(CODEX_CREDENTIAL_ENV_NAMES)
        assert all(step["env"][name] == "" for name in CODEX_CREDENTIAL_ENV_NAMES)


def test_codex_model_is_explicit_and_reports_are_aggregated():
    standard = _step("Run Codex canary")
    resume = _step("Run Codex resume canary")
    for step in (standard, resume):
        assert '--model "$KEW_CODEX_CANARY_MODEL"' in step["run"]
    evaluator = _step("Evaluate canary report")["with"]["script"]
    assert "codex.json" in evaluator
    assert "codex-resume.json" in evaluator
    assert "drift" in evaluator
    assert "incomplete" in evaluator


def test_live_canary_names_each_runner_with_its_own_flag():
    """W4.3 row 4: ``--runner`` repeats; a comma list is one unknown runner."""
    run = _step("Run live canary")["run"]
    assert "--runner claude-code --runner opencode" in run
    assert "claude-code,opencode" not in run
