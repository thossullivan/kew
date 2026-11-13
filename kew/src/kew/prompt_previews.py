"""Each agent role's base-prompt builder, bound over the sample item (#3018).

The prompt catalog (:mod:`kew.service.prompts`) owns the role list and the
sample item, but it may not import the builders: they reach ``kew.graph``
through the dispatch stack. This module binds them, outside ``kew.service``,
and ``kew serve`` hands :data:`PREVIEW_RENDERERS` to ``create_app``.

Every renderer calls the builder the role runs, with sample arguments only. It
reads no store, tracker or checkout, so no real item reaches a preview. What
it does read is the served config and the project root (for a deployed
reviewer copy). The dispatch preview takes the containment the runner's code
and the config declare, in the admission resolver's order; it never asks the
resolver itself, which probes the host and reads the launching process's
credentials — that admission is decided at launch.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime

from kew.acceptance_author import author_prompt
from kew.ci_fix import build_ci_fix_prompt
from kew.config import DispatchConfig
from kew.dispatch_core import dispatch_publication_style
from kew.graph.plan import build_epic_plan_prompt
from kew.loops.delegate import _build_continuation_prompt
from kew.probe_route import PROBE_PROMPT
from kew.prompt import build_prompt
from kew.resolve import _build_resolve_prompt
from kew.review import ReviewComment, build_review_prompt
from kew.review_contract import resolve_reviewer_prompt, reviewer_base_ref
from kew.review_dispatch import assemble_reviewer_prompt, reviewer_prompt_head
from kew.run_store import ItemPlanRow
from kew.runners.registry import resolve_runner
from kew.sandbox import (
    declared_offered_runtime,
    runner_native_containment,
    runner_uncontainable,
    uncontainable_refusal,
)
from kew.service.prompts import (
    SAMPLE_BODY,
    SAMPLE_ISSUE_NUM,
    SAMPLE_TITLE,
    PreviewInputs,
    PromptRenderer,
    RenderedPrompt,
)
from kew.test_integrity import TestIntegrityReport
from kew.triage.classify import render_triage_prompt
from kew.triage.schema import AcceptanceCriterion, ItemPlan
from kew.triage.signals import IssueSignals, body_features
from kew.worktree import ConflictInfo

_SAMPLE_PR = 2
_SAMPLE_BRANCH = "agent/issue-1"
_SAMPLE_ITEM_ID = "1"
_SAMPLE_FILE = "src/sample.py"
_SAMPLE_CI_LOG = "FAILED tests/test_sample.py::test_sample - AssertionError"
_SAMPLE_EPIC = "sample"
_SAMPLE_PLAN_PATH = "sample-plan.md"
_SAMPLE_COMPLEXITY = "medium"

#: The sample item's one criterion, as its body states it.
SAMPLE_CRITERION = AcceptanceCriterion(
    id="AC1", statement="The sample behaves as described."
)
_SAMPLE_PLAN_JSON = ItemPlan(acceptance_criteria=[SAMPLE_CRITERION]).model_dump_json()

#: The sample item's recorded plan, as the ledger would hold it: what the
#: reviewer preview's criteria section is rendered from.
SAMPLE_PLAN_ROW = ItemPlanRow(
    id=1,
    item_id=_SAMPLE_ITEM_ID,
    run_id=None,
    body_digest=hashlib.sha256(SAMPLE_BODY.encode("utf-8")).hexdigest(),
    source="extracted",
    plan_sha=hashlib.sha256(_SAMPLE_PLAN_JSON.encode("utf-8")).hexdigest(),
    plan_json=_SAMPLE_PLAN_JSON,
    criteria_count=1,
    approved_by=None,
    recorded_at=datetime(2026, 1, 1, tzinfo=UTC),
    superseded_at=None,
)

#: The sample round's test-integrity report: clean, no flags to answer.
SAMPLE_INTEGRITY = TestIntegrityReport()


#: Said by every dispatch variant: the preview shows what the config declares,
#: and only the launch asks the runtime, credentials, certified boundary,
#: transport and gateway whether it may start.
ADMISSION_AT_LAUNCH = "decided at launch"
REFUSED_SANDBOX_OFF = (
    "The sandbox is off and containment is not skipped, so a dispatch is refused."
)


def _declared_containment(config: DispatchConfig, runner: str) -> dict[str, str | bool]:
    """The posture the admission resolver's code-and-config arms give ``runner``.

    Asked in the resolver's order: the runner's own boundary (on the runtime
    a launch would offer it), a runner that can never be contained, the
    sandbox switch, then kew's container. Every answer here is a declaration
    in runner code or config; host detection and credentials are the launch's.
    """
    sandbox = config.sandbox
    transport = config.inference_transport
    sidecar = transport.switchyard.sidecar
    offered = declared_offered_runtime(
        sandbox,
        runner,
        sidecar=bool(sidecar and sidecar.enabled),
        gateway_launch=True,
        direct=transport.mode == "direct",
    )
    native = runner_native_containment(runner, runtime=offered)
    if native is not None:
        return {"posture": native, "contained": True}
    uncontainable = runner_uncontainable(runner)
    if uncontainable is not None or sandbox.enabled is False:
        if sandbox.dangerously_skip_containment:
            return {"posture": "uncontained", "contained": False}
        refusal = (
            REFUSED_SANDBOX_OFF
            if uncontainable is None
            else uncontainable_refusal(uncontainable)
        )
        return {"posture": "refused", "refusal": refusal}
    return {"posture": "contained", "contained": True, "runtime": sandbox.runtime}


def _dispatch(inputs: PreviewInputs) -> RenderedPrompt:
    # The shape the config DECLARES: the resolved default runner, the
    # containment its code and the config declare, and the publication style
    # its launch site derives from both. No host probe or credential is asked —
    # that admission is the launching process's. No repo map — that is the
    # checkout's, not the base.
    config = inputs.config
    runner = resolve_runner(config, _SAMPLE_COMPLEXITY).runner
    variant: dict[str, str | bool] = {
        "runner": runner.name,
        "admission": ADMISSION_AT_LAUNCH,
        **_declared_containment(config, runner.name),
    }
    if variant["posture"] == "refused":
        # The launch refuses before it builds a prompt, so there is none.
        return RenderedPrompt("", variant)
    sandboxed = variant["posture"] == "contained"
    publication = dispatch_publication_style(config, runner, sandboxed=sandboxed)
    variant["publication"] = publication
    skills = (
        inputs.repo_root is not None
        and (inputs.repo_root / ".claude" / "skills").is_dir()
    )
    text = build_prompt(
        issue_num=SAMPLE_ISSUE_NUM,
        issue_title=SAMPLE_TITLE,
        issue_body=SAMPLE_BODY,
        complexity=_SAMPLE_COMPLEXITY,
        config=config,
        has_coderabbit=False,
        sandboxed=sandboxed,
        publication_style=publication,
        has_repo_skills=runner.name == "claude-code" and skills,
        runner_name=runner.name,
        rubric_root=inputs.repo_root,
        profile_addendum=inputs.profile_addendum,
    )
    return RenderedPrompt(text, variant)


def _adversarial_review(inputs: PreviewInputs) -> RenderedPrompt:
    # ``review_dispatch``'s own assembly over the sample's recorded plan: a
    # first, full round with no frontend lockfile and a clean integrity report.
    # The artifact loads as at launch, so a deployed copy in the project wins;
    # the catalog hands in the copy it resolved for ``source``, so the two agree.
    loaded = inputs.reviewer_prompt or resolve_reviewer_prompt(
        inputs.repo_root, reviewer_base_ref(inputs.config.branches.base)
    )
    build = reviewer_prompt_head(
        loaded.text,
        loaded.sha256,
        SAMPLE_PLAN_ROW,
        item_id=_SAMPLE_ITEM_ID,
        max_review_usd=inputs.config.review.max_review_usd,
        addendum=inputs.profile_addendum,
    )
    text = assemble_reviewer_prompt(
        build, mode_section="", frontend_section="", integrity=SAMPLE_INTEGRITY
    )
    # An addendum renders before the artifact (#3088), so the verbatim head
    # runs through the artifact's end: addendum, precedence sentence, rubric.
    artifact_at = text.find(loaded.text)
    verbatim = text[: artifact_at + len(loaded.text)] if artifact_at >= 0 else ""
    return RenderedPrompt(
        text,
        {
            "artifact": "pinned" if loaded.deployed is None else "deployed",
            "review_mode": "full",
            "criteria": build.plan is not None,
            "frontend": False,
            "integrity": "clean",
        },
        verbatim_prefix=verbatim,
    )


def _review_remediation(inputs: PreviewInputs) -> RenderedPrompt:
    comment = ReviewComment(
        id=1,
        author="reviewer",
        body="Sample review comment.",
        path=_SAMPLE_FILE,
        line=1,
        state="COMMENTED",
        in_reply_to=None,
        resolved=False,
    )
    text = build_review_prompt(
        pr_number=_SAMPLE_PR,
        issue_number=SAMPLE_ISSUE_NUM,
        branch_name=_SAMPLE_BRANCH,
        comments=[comment],
        config=inputs.config,
        host_publication=True,
        governed=True,
        profile_addendum=inputs.profile_addendum,
    )
    return RenderedPrompt(text)


def _resolve(inputs: PreviewInputs) -> RenderedPrompt:
    text = _build_resolve_prompt(
        pr_number=_SAMPLE_PR,
        issue_number=SAMPLE_ISSUE_NUM,
        branch_name=_SAMPLE_BRANCH,
        base_branch="main",
        conflict_info=ConflictInfo(
            strategy_used="merge", conflicted_files=[_SAMPLE_FILE], base_branch="main"
        ),
        config=inputs.config,
        host_publication=True,
        profile_addendum=inputs.profile_addendum,
    )
    return RenderedPrompt(text)


def _ci_fix(inputs: PreviewInputs) -> RenderedPrompt:
    config = inputs.config
    text = build_ci_fix_prompt(
        pr_number=_SAMPLE_PR,
        issue_number=SAMPLE_ISSUE_NUM,
        issue_title=SAMPLE_TITLE,
        branch_name=_SAMPLE_BRANCH,
        ci_log=_SAMPLE_CI_LOG,
        attempt=1,
        max_attempts=max(1, config.triggers.ci_failure_max_retries),
        config=config,
        host_publication=True,
        profile_addendum=inputs.profile_addendum,
    )
    return RenderedPrompt(text)


def _triage(config: DispatchConfig) -> str:
    signals = IssueSignals(
        item_id=_SAMPLE_ITEM_ID,
        body_digest=hashlib.sha256(SAMPLE_BODY.encode("utf-8")).hexdigest(),
        features=body_features(SAMPLE_BODY),
        issue_num=SAMPLE_ISSUE_NUM,
    )
    return render_triage_prompt(
        item_id=_SAMPLE_ITEM_ID,
        title=SAMPLE_TITLE,
        body=SAMPLE_BODY,
        signals=signals,
        config=config,
    )


def _planning(config: DispatchConfig) -> str:
    return build_epic_plan_prompt(
        epic_name=_SAMPLE_EPIC, plan_path=_SAMPLE_PLAN_PATH, config=config
    )


def _acceptance_author(_config: DispatchConfig) -> str:
    return author_prompt(
        issue_num=SAMPLE_ISSUE_NUM,
        issue_body=f"# {SAMPLE_TITLE}\n\n{SAMPLE_BODY}",
        criteria=[SAMPLE_CRITERION],
    )


def _loop_continuation(_config: DispatchConfig) -> str:
    return _build_continuation_prompt(
        f"{SAMPLE_TITLE}\n\n{SAMPLE_BODY}", "sample stop reason", "Sample guidance."
    )


def _readiness_probe(_config: DispatchConfig) -> str:
    return PROBE_PROMPT


def _one_shape(build: Callable[[DispatchConfig], str]) -> PromptRenderer:
    """A renderer for a role with one prompt shape, built from config alone."""

    def render(inputs: PreviewInputs) -> RenderedPrompt:
        return RenderedPrompt(build(inputs.config))

    return render


#: One renderer per ``kew.service.prompts.ROLES`` id.
PREVIEW_RENDERERS: dict[str, PromptRenderer] = {
    "dispatch": _dispatch,
    "adversarial_review": _adversarial_review,
    "review_remediation": _review_remediation,
    "resolve": _resolve,
    "ci_fix": _ci_fix,
    "triage": _one_shape(_triage),
    "planning": _one_shape(_planning),
    "acceptance_author": _one_shape(_acceptance_author),
    "loop_continuation": _one_shape(_loop_continuation),
    "readiness_probe": _one_shape(_readiness_probe),
}
