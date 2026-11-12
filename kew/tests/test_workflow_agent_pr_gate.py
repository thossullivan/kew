"""Self-hosted CI never runs an agent PR's code before a human approves it.

While the repository is private, pull-request CI runs on the operator's Mac
with a secret in the environment (#2033, audit F1). An agent-authored PR can
add an ordinary test file, so every job that runs pull-request code on the
self-hosted runner carries a job-level ``if:`` that GitHub evaluates before
any runner picks the job up. For an agent PR the job runs only while the
human approval label (kew's ``labels.ci_approved``) is present; kew removes
that label before it pushes new agent commits.

The ``if:`` is evaluated, not matched as text, over every combination of
event action, labels, head branch and visibility, so a rewrite that lets one
unapproved agent PR through fails even when it still reads plausibly.
"""

from __future__ import annotations

import copy

import pytest
import yaml
from support.agent_pr_gate import (
    AGENT_BRANCH_PREFIX,
    AGENT_LABEL,
    APPROVAL_LABEL,
    OTHER_LABEL,
    PR_EVENTS,
    assert_job_gate_stays_live,
    assert_job_gated,
)
from support.agent_pr_gate import event_context as _context
from support.agent_pr_gate import runs as _runs
from support.workflow_runners import (
    REPO_ROOT,
    SELF_HOSTED,
    evaluate,
    resolve_runner,
    workflow_paths,
)

# Every job that runs pull-request code on the self-hosted runner.
EXPECTED_GATED_JOBS = {("ci.yml", "test"), ("ci.yml", "agent-image-scan")}


def _triggers(data: dict) -> dict:
    # PyYAML reads the bare key `on` as the boolean True.
    triggers = data.get("on", data.get(True))
    if isinstance(triggers, str):
        return {triggers: None}
    if isinstance(triggers, list):
        return dict.fromkeys(triggers)
    return triggers or {}


def _workflows() -> dict[str, dict]:
    return {
        path.name: yaml.safe_load(path.read_text(encoding="utf-8"))
        for path in workflow_paths()
    }


def _pr_triggered(data: dict) -> list[str]:
    return [event for event in PR_EVENTS if event in _triggers(data)]


def _self_hosted_pr_jobs(workflows: dict[str, dict]) -> dict[tuple[str, str], dict]:
    return {
        (name, job_name): job
        for name, data in workflows.items()
        if _pr_triggered(data)
        for job_name, job in data["jobs"].items()
        if SELF_HOSTED in resolve_runner(job, "private")
    }


def _assert_gated(workflows: dict[str, dict]) -> None:
    """No unapproved agent PR event reaches a self-hosted job in private."""
    for (workflow_name, job_name), job in _self_hosted_pr_jobs(workflows).items():
        assert_job_gated(job, f"{workflow_name} job {job_name!r}")


def _assert_gate_stays_live(workflows: dict[str, dict]) -> None:
    """The gate blocks only what it must: approved, human and main runs stay."""
    for (workflow_name, job_name), job in _self_hosted_pr_jobs(workflows).items():
        assert_job_gate_stays_live(job, f"{workflow_name} job {job_name!r}")


def test_every_self_hosted_pr_job_is_known() -> None:
    assert set(_self_hosted_pr_jobs(_workflows())) == EXPECTED_GATED_JOBS


def test_self_hosted_pr_jobs_require_approval_for_an_agent_pr() -> None:
    _assert_gated(_workflows())


def test_the_gate_keeps_human_approved_and_hosted_runs() -> None:
    _assert_gate_stays_live(_workflows())


def test_unapproved_agent_pr_events_are_skipped() -> None:
    for job in _self_hosted_pr_jobs(_workflows()).values():
        for action in ("opened", "synchronize", "reopened"):
            # Labelled, and the `opened` race: the host labels after creation.
            assert not _runs(
                job,
                _context(action=action, labels=(AGENT_LABEL,), head_ref="feature/x"),
            )
            assert not _runs(
                job, _context(action=action, head_ref=f"{AGENT_BRANCH_PREFIX}9")
            )
        # A human push to an agent PR is still gated: the agent label stays.
        assert not _runs(
            job,
            _context(
                action="synchronize",
                labels=(AGENT_LABEL, OTHER_LABEL),
                head_ref=f"{AGENT_BRANCH_PREFIX}9",
            ),
        )


def test_only_push_runs_self_hosted_jobs_without_the_agent_check() -> None:
    """The gate allow-lists the non-PR events ci.yml uses; every other event,
    pull_request_target included, must pass the agent/approval check."""
    data = _workflows()["ci.yml"]
    assert set(_triggers(data)) == {"push", "pull_request"}
    for job in _self_hosted_pr_jobs({"ci.yml": data}).values():
        condition = " ".join(str(job["if"]).split())
        assert "github.event_name == 'push'" in condition
        assert "github.event_name != 'pull_request'" not in condition
        for event in PR_EVENTS:
            assert not _runs(
                job,
                _context(event=event, labels=(AGENT_LABEL,), head_ref="feature/x"),
            ), f"an unapproved agent PR runs on a {event} event"
        # An unknown event that carries no PR is not waved through either.
        assert not _runs(job, _context(event="workflow_run", action=None))


def test_approval_gated_checks_match_the_gated_jobs() -> None:
    """ci.approval_gated_checks names exactly the ci.yml jobs whose `if:`
    carries the ci-approved agent check, by the check-run name GitHub shows
    (the job's `name:` when set, else its id)."""
    import tomllib

    from kew.config import CiConfig

    agent_check = (
        f"contains(github.event.pull_request.labels.*.name, '{APPROVAL_LABEL}')"
    )
    gated_jobs = {
        str(job.get("name") or job_name)
        for data in _workflows().values()
        for job_name, job in data["jobs"].items()
        if agent_check in " ".join(str(job.get("if", "")).split())
    }
    toml = tomllib.loads((REPO_ROOT / "kew.toml").read_text(encoding="utf-8"))
    configured = toml.get("ci", {}).get(
        "approval_gated_checks", CiConfig().approval_gated_checks
    )
    assert gated_jobs == set(configured)
    assert set(CiConfig().approval_gated_checks) == gated_jobs


def test_labeled_is_among_the_pull_request_types() -> None:
    for name, data in _workflows().items():
        if not any(key[0] == name for key in _self_hosted_pr_jobs({name: data})):
            continue
        for event in _pr_triggered(data):
            types = (_triggers(data)[event] or {}).get("types")
            assert types is not None, f"{name} {event} lost its default types"
            assert {"opened", "synchronize", "reopened", "labeled"} <= set(types), (
                f"{name} {event} types {types!r}: `labeled` starts CI when a "
                "human approves, and the default three must stay."
            )


def test_other_label_events_run_no_job_and_cancel_nothing() -> None:
    data = _workflows()["ci.yml"]
    for job_name, job in data["jobs"].items():
        assert not _runs(
            job,
            _context(action="labeled", labels=(OTHER_LABEL,), added_label=OTHER_LABEL),
        ), f"ci.yml job {job_name!r} reruns for an unrelated label"
        assert _runs(job, _context(action="opened")), job_name
    group = str(data["concurrency"]["group"])
    assert "github.event.action == 'labeled'" in group
    assert f"github.event.label.name != '{APPROVAL_LABEL}'" in group
    assert "github.run_id" in group


def test_workflow_label_names_match_kew_config() -> None:
    text = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    for name in (AGENT_LABEL, APPROVAL_LABEL, AGENT_BRANCH_PREFIX):
        assert f"'{name}'" in text, f"ci.yml does not name {name!r} from kew.toml"


def _mutated(mutate) -> dict[str, dict]:
    workflows = copy.deepcopy(_workflows())
    mutate(workflows)
    return workflows


def _set_if(value: str | None):
    def mutate(workflows: dict[str, dict]) -> None:
        job = workflows["ci.yml"]["jobs"]["test"]
        if value is None:
            job.pop("if", None)
        else:
            job["if"] = value

    return mutate


def test_gate_assertions_fail_red_for_weakened_gates() -> None:
    weakened = (
        None,
        "true",
        # A deny-list of one PR event lets pull_request_target straight through.
        f"github.event_name != 'pull_request' || "
        f"contains(github.event.pull_request.labels.*.name, '{APPROVAL_LABEL}') || "
        f"(!contains(github.event.pull_request.labels.*.name, '{AGENT_LABEL}') && "
        f"!startsWith(github.event.pull_request.head.ref, '{AGENT_BRANCH_PREFIX}'))",
        # Approval ignored for a branch-only agent PR (the `opened` race).
        f"!contains(github.event.pull_request.labels.*.name, '{AGENT_LABEL}') "
        f"|| contains(github.event.pull_request.labels.*.name, '{APPROVAL_LABEL}')",
        # Any label event, not only approval, starts an unapproved agent PR.
        f"github.event.action == 'labeled' || "
        f"contains(github.event.pull_request.labels.*.name, '{APPROVAL_LABEL}')",
    )
    for value in weakened:
        with pytest.raises(AssertionError):
            _assert_gated(_mutated(_set_if(value)))


def test_liveness_assertions_fail_red_for_a_gate_that_blocks_everything() -> None:
    with pytest.raises(AssertionError):
        _assert_gate_stays_live(_mutated(_set_if("false")))
    no_public_path = (
        f"contains(github.event.pull_request.labels.*.name, '{APPROVAL_LABEL}') "
        f"|| github.event_name != 'pull_request'"
    )
    with pytest.raises(AssertionError):
        _assert_gate_stays_live(_mutated(_set_if(no_public_path)))


def test_new_self_hosted_pr_job_without_a_gate_fails() -> None:
    def add_job(workflows: dict[str, dict]) -> None:
        jobs = workflows["ci.yml"]["jobs"]
        jobs["extra"] = {
            "runs-on": jobs["test"]["runs-on"],
            "steps": [{"run": "true"}],
        }

    with pytest.raises(AssertionError, match="no agent PR gate"):
        _assert_gated(_mutated(add_job))


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("contains(github.event.pull_request.labels.*.name, 'CI-Approved')", True),
        ("contains(github.event.pull_request.labels.*.name, 'agent-pr')", False),
        ("startsWith(github.event.pull_request.head.ref, 'AGENT/')", True),
        ("startsWith(github.event.pull_request.head.ref, 'feature/')", False),
        ("contains(github.event.missing.*.name, 'x')", False),
    ],
)
def test_expression_evaluator_models_contains_and_starts_with(
    expression: str, expected: bool
) -> None:
    context = _context(labels=("ci-approved",), head_ref="agent/issue-1")
    assert evaluate(expression, context) is expected
