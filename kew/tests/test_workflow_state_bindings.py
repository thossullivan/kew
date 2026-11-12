"""Pin runner and state bindings for the staged hosted-runner migration."""

import shutil
from pathlib import Path

import pytest
import yaml
from support.workflow_runners import (
    RUNNER_BY_EVENT_VISIBILITY,
    SELF_HOSTED,
    WORKFLOWS_DIR,
    resolve_runner,
    workflow_paths,
)

# The jobs that use the self-hosted runner while the repository is private.
# Outside a private repository every job resolves to a hosted runner
# (tests/test_workflow_runner_visibility.py).
EXPECTED_SELF_HOSTED_JOBS = {
    "approval.yml": {"decide"},
    "audit-anchor.yml": {"anchor"},
    "canary.yml": {"canary"},
    "ci.yml": {"test", "agent-image-scan"},
    "release.yml": {"serial-tests"},
}


def _load_workflows(directory: Path = WORKFLOWS_DIR) -> dict[str, dict]:
    return {
        path.name: yaml.safe_load(path.read_text(encoding="utf-8"))
        for path in workflow_paths(directory)
    }


def _self_hosted_jobs(workflows: dict[str, dict]) -> dict[str, set[str]]:
    found = {}
    for workflow_name, data in workflows.items():
        jobs = {
            job_name
            for job_name, job in data["jobs"].items()
            if SELF_HOSTED in resolve_runner(job, "private")
        }
        if jobs:
            found[workflow_name] = jobs
    return found


def _assert_self_hosted_inventory(workflows: dict[str, dict]) -> None:
    """Pin every job that can reach the self-hosted runner."""
    self_hosted = _self_hosted_jobs(workflows)
    assert self_hosted == EXPECTED_SELF_HOSTED_JOBS, (
        f"self-hosted jobs changed: {self_hosted!r} vs {EXPECTED_SELF_HOSTED_JOBS!r}"
    )


def _named_step(job: dict, name: str) -> dict:
    return next(step for step in job["steps"] if step.get("name") == name)


def _steps_using(job: dict, variable: str) -> list[dict]:
    return [step for step in job["steps"] if variable in str(step)]


def test_approval_requires_its_database_and_derives_the_log_directory() -> None:
    data = _load_workflows()["approval.yml"]
    job = data["jobs"]["decide"]
    step = _named_step(job, "Apply decision")
    run = step["run"]

    assert job["runs-on"] == RUNNER_BY_EVENT_VISIBILITY
    assert step["env"]["KEW_DISPATCH_DB"] == "${{ vars.KEW_DISPATCH_DB }}"
    required = (
        ': "${KEW_DISPATCH_DB:?set the KEW_DISPATCH_DB repo variable to the '
        'run-store db path}"'
    )
    derived = 'export KEW_LOG_DIR="$(dirname "$KEW_DISPATCH_DB")"'
    assert required in run
    assert derived in run
    assert run.index(required) < run.index(derived) < run.index("uv run kew")
    assert _steps_using(job, "KEW_DISPATCH_DB") == [step]
    assert _steps_using(job, "KEW_LOG_DIR") == [step]


def test_pricing_database_binding_is_optional_on_the_hosted_runner() -> None:
    data = _load_workflows()["pricing-freshness.yml"]
    job = data["jobs"]["pricing-drift"]
    step = _named_step(job, "Audit the model catalog for drift")
    run = step["run"]

    assert job["runs-on"] == "ubuntu-latest"
    assert step["env"] == {"KEW_DISPATCH_DB": "${{ vars.KEW_DISPATCH_DB }}"}
    assert 'if [ -n "$KEW_DISPATCH_DB" ] && [ -f "$KEW_DISPATCH_DB" ]; then' in run
    assert 'args+=(--run-store "$KEW_DISPATCH_DB")' in run
    assert "${KEW_DISPATCH_DB:?" not in run
    assert _steps_using(job, "KEW_DISPATCH_DB") == [step]


def test_every_self_hosted_job_is_listed() -> None:
    _assert_self_hosted_inventory(_load_workflows())


def test_runner_mutations_fail_in_temporary_copies(tmp_path: Path) -> None:
    runner_copy = tmp_path / "runner" / "workflows"
    shutil.copytree(WORKFLOWS_DIR, runner_copy)

    approval = runner_copy / "approval.yml"
    approval.write_text(
        approval.read_text(encoding="utf-8").replace(
            f"runs-on: {RUNNER_BY_EVENT_VISIBILITY}", "runs-on: ubuntu-latest", 1
        ),
        encoding="utf-8",
    )
    with pytest.raises(AssertionError, match="self-hosted jobs changed"):
        _assert_self_hosted_inventory(_load_workflows(runner_copy))


def test_approval_passes_the_issue_positionally() -> None:
    """W4.3 rows 5-6: ``kew approve``/``kew deny`` take the issue as an argument."""
    job = _load_workflows()["approval.yml"]["jobs"]["decide"]
    run = _named_step(job, "Apply decision")["run"]
    assert 'uv run kew "${cmd#/}" "$ISSUE" --reason "$reason"' in run
    assert "--issue" not in run
