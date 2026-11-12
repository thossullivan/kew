"""Evaluate a workflow job's agent PR gate (#2033) over every PR event shape.

While the repository is private, a job that runs pull-request code on the
self-hosted runner must skip an agent-authored PR until a human adds the
approval label. The checks here evaluate the job's ``if:`` with
:mod:`support.workflow_runners` over every action, label set, head branch and
visibility, rather than matching its text. They live in a support module so
more than one test module can apply them without importing a test module.
"""

from __future__ import annotations

import itertools
import tomllib
from typing import Any

from kew.config import BranchConfig, LabelConfig
from support.workflow_runners import (
    MISSING,
    NON_PRIVATE_VISIBILITIES,
    REPO_ROOT,
    evaluate,
)

# Every event that carries a pull request. The gate must hold for each, so
# a new PR-shaped trigger cannot slip past a clause that names only one.
PR_EVENTS = ("pull_request", "pull_request_target")


def _configured(section: str, key: str, default: str) -> str:
    data = tomllib.loads((REPO_ROOT / "kew.toml").read_text(encoding="utf-8"))
    return str(data.get(section, {}).get(key, default))


AGENT_LABEL = _configured("labels", "agent_pr", LabelConfig().agent_pr)
APPROVAL_LABEL = _configured("labels", "ci_approved", LabelConfig().ci_approved)
AGENT_BRANCH_PREFIX = _configured("branches", "prefix", BranchConfig().prefix)
OTHER_LABEL = "pr-pending-review"

ACTIONS = ("opened", "synchronize", "reopened", "labeled")
LABEL_SETS = tuple(
    tuple(combo)
    for size in range(4)
    for combo in itertools.combinations(
        (AGENT_LABEL, APPROVAL_LABEL, OTHER_LABEL), size
    )
)
HEAD_REFS = (f"{AGENT_BRANCH_PREFIX}2033", "feature/human-work")


def event_context(
    *,
    event: str = "pull_request",
    action: str | None = "synchronize",
    labels: tuple[str, ...] = (),
    head_ref: str = "feature/human-work",
    visibility: object = "private",
    added_label: str | None = None,
) -> dict[str, Any]:
    """The ``github`` context one event gives a job-level ``if:``."""
    payload: dict[str, Any] = {}
    if visibility is not MISSING:
        payload["repository"] = {"visibility": visibility}
    if action is not None:
        payload["action"] = action
    if added_label is not None:
        payload["label"] = {"name": added_label}
    if event in PR_EVENTS:
        payload["pull_request"] = {
            "labels": [{"name": label} for label in labels],
            "head": {"ref": head_ref},
        }
    return {"github": {"event_name": event, "event": payload}}


def runs(job: dict, context: dict[str, Any]) -> bool:
    """Whether GitHub would start *job* for *context*."""
    condition = job.get("if")
    if condition is None:
        return True
    value = evaluate(str(condition), context)
    return value not in (None, False, "", 0)


def pr_cases() -> list[dict[str, Any]]:
    """Every private-repository PR event shape the gate must decide."""
    cases = []
    for event, action, labels, head_ref in itertools.product(
        PR_EVENTS, ACTIONS, LABEL_SETS, HEAD_REFS
    ):
        added = [None] if action != "labeled" else list(labels)
        for added_label in added:
            cases.append(
                {
                    "event": event,
                    "action": action,
                    "labels": labels,
                    "head_ref": head_ref,
                    "added_label": added_label,
                }
            )
    return cases


def agent_authored(case: dict[str, Any]) -> bool:
    return AGENT_LABEL in case["labels"] or case["head_ref"].startswith(
        AGENT_BRANCH_PREFIX
    )


def assert_job_gated(job: dict, where: str) -> None:
    """No unapproved agent PR event starts *job* in a private repository."""
    assert "if" in job, (
        f"{where} runs pull-request code on the self-hosted runner and has no "
        "agent PR gate."
    )
    for case in pr_cases():
        if not runs(job, event_context(**case)):
            continue
        assert (not agent_authored(case)) or APPROVAL_LABEL in case["labels"], (
            f"{where} runs an agent PR without {APPROVAL_LABEL!r} on the "
            f"self-hosted runner: {case}"
        )


def assert_job_gate_stays_live(job: dict, where: str) -> None:
    """The gate blocks only what it must: human, approved, main and hosted runs."""
    for action in ("opened", "synchronize", "reopened"):
        assert runs(job, event_context(action=action)), f"{where} skips a human PR"
    assert runs(
        job,
        event_context(
            action="labeled",
            labels=(AGENT_LABEL, APPROVAL_LABEL),
            head_ref=f"{AGENT_BRANCH_PREFIX}7",
            added_label=APPROVAL_LABEL,
        ),
    ), f"{where} does not start when a human approves an agent PR"
    assert runs(
        job,
        event_context(
            action="synchronize",
            labels=(AGENT_LABEL, APPROVAL_LABEL),
            head_ref=f"{AGENT_BRANCH_PREFIX}7",
        ),
    ), f"{where} skips an approved agent PR head"
    assert runs(job, event_context(event="push", action=None)), (
        f"{where} skips a push to main"
    )
    for visibility in NON_PRIVATE_VISIBILITIES:
        assert runs(
            job,
            event_context(
                labels=(AGENT_LABEL,),
                head_ref=f"{AGENT_BRANCH_PREFIX}7",
                visibility=visibility,
            ),
        ), f"{where} changed the hosted path for visibility {visibility!r}"
