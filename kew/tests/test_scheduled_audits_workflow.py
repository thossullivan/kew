from pathlib import Path

import yaml

WORKFLOW = (
    Path(__file__).resolve().parents[2]
    / ".github"
    / "workflows"
    / "scheduled-audits.yml"
)


def test_runner_liveness_audit_is_independent_of_self_hosted_runners() -> None:
    data = yaml.safe_load(WORKFLOW.read_text())
    job = data["jobs"]["runner-liveness"]

    assert job["runs-on"] == "ubuntu-latest"
    assert job["permissions"]["actions"] == "read"


def test_runner_liveness_audit_fails_for_offline_runners() -> None:
    text = WORKFLOW.read_text()

    assert "listSelfHostedRunnersForRepo" in text
    assert "status === 'offline'" in text
    assert "core.setFailed" in text
