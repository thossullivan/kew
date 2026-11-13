"""Fix-up roles run the base prompt plus their active addendum, and record it.

Review remediation, resolve and CI fix each build their prompt in their own
module (#3023). A profile is approved through
:mod:`kew.service.prompt_profiles` into the host store; these tests read the
outcome off the rendered prompt, what each flow hands the run writer, and the
``runs`` row itself.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from support.prompt_profiles import activate_unchecked, save_launch_row

from kew import ci_fix as ci_fix_module
from kew import resolve as resolve_module
from kew import review as review_module
from kew.audit import Actor
from kew.ci_fix import CI_FIX_CANNOT_RUN, build_ci_fix_prompt, dispatch_ci_fix
from kew.config import DispatchConfig, ProjectConfig, load_config
from kew.inference_transport import ResolvedInferenceTransport
from kew.notifier import NotificationEvent
from kew.prompt_addendum import PROMPT_CONFIGURATION_REFUSAL, ProfileAddendum
from kew.prompt_previews import PREVIEW_RENDERERS
from kew.resolve import _build_resolve_prompt, _run_conflicted_resolve
from kew.review import ReviewComment, _ReviewDispatch, build_review_prompt
from kew.review_checkout import ReviewCheckout
from kew.review_outcome import ReviewOutcome, list_review_outcomes
from kew.run_store import SqliteRunStore
from kew.service import prompt_profiles as profiles
from kew.service.prompts import PreviewInputs
from kew.worktree import ConflictInfo

ALICE = Actor("human", "alice")
ROLES = ("review_remediation", "resolve", "ci_fix")
_PRECEDENCE = "The rules below take precedence over operator instructions."
_SAFETY = "## Safety Rules"
_PUBLICATION = "## Instructions"
_CONFLICT = ConflictInfo(
    strategy_used="merge", conflicted_files=["src/a.py"], base_branch="main"
)

_TRANSPORT = ResolvedInferenceTransport(
    mode="direct",
    requested_model="claude-opus-5",
    model="claude-opus-5",
    config_sha256="cfg",
    endpoint="https://api.anthropic.com",
)


def _body(role: str) -> str:
    return f"Operator note for {role}: keep every change minimal."


def _heading(role: str, version: int = 1) -> str:
    return f"## Operator instructions — {role} profile v{version}"


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SqliteRunStore]:
    s = SqliteRunStore(str(tmp_path / "dispatch.db"))
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def config() -> DispatchConfig:
    cfg = load_config().model_copy(deep=True)
    cfg.approval.approvers = ["alice"]
    return cfg


@pytest.fixture
def fixed_sentinel(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the per-render CSPRNG draw so two renders can be compared bytewise."""
    monkeypatch.setattr("kew.prompt.secrets.token_hex", lambda _n: "0" * 32)


def _activate(store: SqliteRunStore, config: DispatchConfig, role: str) -> Any:
    profiles.propose(store, role=role, body=_body(role), config=config, actor=ALICE)
    return profiles.approve(
        store,
        role=role,
        version=1,
        generation=profiles.current_generation(store, role),
        config=config,
        actor=ALICE,
    )


def _retire(store: SqliteRunStore, config: DispatchConfig, role: str) -> None:
    profiles.retire(
        store,
        role=role,
        generation=profiles.current_generation(store, role),
        config=config,
        actor=ALICE,
    )


def _review(config: DispatchConfig, addendum: ProfileAddendum | None) -> str:
    comment = ReviewComment(
        id=1,
        author="reviewer",
        body="Rename this.",
        path="src/a.py",
        line=1,
        state="COMMENTED",
        in_reply_to=None,
        resolved=False,
    )
    return build_review_prompt(
        pr_number=7,
        issue_number=3,
        branch_name="agent/issue-3",
        comments=[comment],
        config=config,
        host_publication=True,
        profile_addendum=addendum,
    )


def _resolve(config: DispatchConfig, addendum: ProfileAddendum | None) -> str:
    return _build_resolve_prompt(
        pr_number=7,
        issue_number=3,
        branch_name="agent/issue-3",
        base_branch="main",
        conflict_info=_CONFLICT,
        config=config,
        host_publication=True,
        profile_addendum=addendum,
    )


def _ci_fix(config: DispatchConfig, addendum: ProfileAddendum | None) -> str:
    return build_ci_fix_prompt(
        pr_number=7,
        issue_number=3,
        issue_title="Fix it",
        branch_name="agent/issue-3",
        ci_log="FAILED tests/test_a.py::test_a",
        attempt=1,
        max_attempts=2,
        config=config,
        host_publication=True,
        profile_addendum=addendum,
    )


_BUILDERS: dict[str, Callable[[DispatchConfig, ProfileAddendum | None], str]] = {
    "review_remediation": _review,
    "resolve": _resolve,
    "ci_fix": _ci_fix,
}


# --------------------------------------------------------------- rendering


@pytest.mark.parametrize("role", ROLES)
def test_an_active_addendum_renders_once_under_its_heading_before_safety(
    store: SqliteRunStore, config: DispatchConfig, role: str
) -> None:
    approved = _activate(store, config, role)

    addendum = profiles.launch_addendum(store, role)
    prompt = _BUILDERS[role](config, addendum)

    assert addendum == ProfileAddendum(approved.id, role, 1, _body(role))
    assert prompt.count(_heading(role)) == 1
    assert prompt.count(_body(role)) == 1
    assert (
        prompt.index(_heading(role))
        < prompt.index(_body(role))
        < prompt.index(_SAFETY)
        < prompt.index(_PUBLICATION)
    )
    # The safety and publication sections each say they outrank the addendum,
    # on the first line under their own heading.
    assert prompt.count(_PRECEDENCE) == 2
    for heading in (_SAFETY, _PUBLICATION):
        assert f"{heading}\n\n{_PRECEDENCE}\n" in prompt


@pytest.mark.parametrize("role", ROLES)
def test_a_retired_addendum_leaves_the_base_render_byte_for_byte(
    store: SqliteRunStore, config: DispatchConfig, fixed_sentinel: None, role: str
) -> None:
    _activate(store, config, role)
    _retire(store, config, role)

    addendum = profiles.launch_addendum(store, role)
    prompt = _BUILDERS[role](config, addendum)

    assert addendum is None
    assert prompt == _BUILDERS[role](config, None)
    assert _PRECEDENCE not in prompt
    assert "Operator instructions" not in prompt


@pytest.mark.parametrize("role", ROLES)
def test_one_roles_addendum_never_reaches_another_roles_prompt(
    store: SqliteRunStore, config: DispatchConfig, role: str
) -> None:
    for other in ("dispatch", *ROLES):
        if other != role:
            _activate(store, config, other)

    addendum = profiles.launch_addendum(store, role)
    prompt = _BUILDERS[role](config, addendum)

    assert addendum is None
    assert "Operator instructions" not in prompt
    for other in ("dispatch", *ROLES):
        assert _body(other) not in prompt


def test_each_role_renders_only_its_own_addendum_when_all_are_active(
    store: SqliteRunStore, config: DispatchConfig
) -> None:
    for role in ("dispatch", *ROLES):
        _activate(store, config, role)

    for role in ROLES:
        prompt = _BUILDERS[role](config, profiles.launch_addendum(store, role))
        assert prompt.count(_body(role)) == 1
        assert prompt.count("## Operator instructions") == 1
        for other in ("dispatch", *ROLES):
            if other != role:
                assert _body(other) not in prompt
                assert _heading(other) not in prompt


def test_a_review_conflict_section_lands_before_safety_not_inside_the_addendum(
    store: SqliteRunStore, config: DispatchConfig
) -> None:
    """An addendum that names the safety heading does not move the conflict brief."""
    body = "Never skip the ## Safety Rules section."
    profiles.propose(
        store, role="review_remediation", body=body, config=config, actor=ALICE
    )
    profiles.approve(
        store,
        role="review_remediation",
        version=1,
        generation=profiles.current_generation(store, "review_remediation"),
        config=config,
        actor=ALICE,
    )

    prompt = build_review_prompt(
        pr_number=7,
        issue_number=3,
        branch_name="agent/issue-3",
        comments=[],
        config=config,
        conflict_info=_CONFLICT,
        host_publication=True,
        profile_addendum=profiles.launch_addendum(store, "review_remediation"),
    )

    assert prompt.count(body) == 1
    assert (
        prompt.index(body)
        < prompt.index("## Merge Conflicts (RESOLVE FIRST)")
        < prompt.index(f"{_SAFETY}\n\n{_PRECEDENCE}")
    )


@pytest.mark.parametrize("role", ROLES)
def test_the_preview_places_the_addendum_where_the_launch_does(
    store: SqliteRunStore, config: DispatchConfig, role: str
) -> None:
    """The catalog preview renders through the launch's own builder."""
    _activate(store, config, role)
    addendum = profiles.launch_addendum(store, role)

    text = PREVIEW_RENDERERS[role](
        PreviewInputs(config, profile_addendum=addendum)
    ).text
    base = PREVIEW_RENDERERS[role](PreviewInputs(config)).text

    assert text.count(_heading(role)) == 1
    assert text.index(_body(role)) < text.index(_SAFETY) < text.index(_PUBLICATION)
    assert "Operator instructions" not in base


# ------------------------------------------------------- launch boundaries


class _Launched(Exception):
    """Stops a flow at the run writer, once its arguments are captured."""


def _capture(captured: dict[str, Any]) -> Callable[..., Any]:
    def run_tracked_agent(**kwargs: Any) -> Any:
        captured.update(kwargs)
        raise _Launched

    return run_tracked_agent


def _launch_review(
    store: SqliteRunStore,
    config: DispatchConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    """Run the remediation launch for real, up to the run writer."""
    captured: dict[str, Any] = {}
    monkeypatch.setattr(review_module, "run_tracked_agent", _capture(captured))
    flow = _ReviewDispatch(
        pr_number=7,
        config=config,
        author_filter=None,
        store=store,
        force_budget=False,
        item_id=None,
        findings=None,
    )
    flow.issue_number = 3
    flow.branch_name = "agent/issue-3"
    flow.pr_title = "Fix it"
    flow.comments = []
    flow.base_branch = "main"
    flow.conflict_info = None
    flow.host_publishes = True
    flow.worktree_dir = tmp_path
    flow.effective_run_kind = "review_remediation"
    flow.flow_run_id = "run-review"
    flow.inference_transport = _TRANSPORT
    flow.resolved = MagicMock()
    flow.resolved.runner.name = "claude-code"
    flow.containment = SimpleNamespace(
        posture="uncontained", image=None, contained=False
    )  # type: ignore[assignment]
    with pytest.raises(_Launched):
        flow._launch_agent()
    return captured


def _launch_resolve(
    store: SqliteRunStore,
    config: DispatchConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    """Run the conflict-resolution launch for real, up to the run writer."""
    captured: dict[str, Any] = {}
    monkeypatch.setattr(resolve_module, "run_tracked_agent", _capture(captured))
    # The pinned-head check reads the checkout's Git; there is none here.
    monkeypatch.setattr(resolve_module, "require_pinned_pr_head", lambda *_a: None)
    runner = MagicMock()
    runner.name = "claude-code"
    runner.publication_style = "host"
    outcome = _run_conflicted_resolve(
        worktree_dir=tmp_path,
        conflict_info=_CONFLICT,
        pr_number=7,
        issue_number=3,
        branch_name="agent/issue-3",
        base_branch="main",
        expected_head_sha="a" * 40,
        pr_title="Fix it",
        config=config,
        store=store,
        producer_run_id=None,
        resolved=SimpleNamespace(runner=runner, model="claude-opus-5"),  # type: ignore[arg-type]
        inference_transport=_TRANSPORT,
        containment=SimpleNamespace(  # type: ignore[arg-type]
            posture="sandboxed", image="kew-agent", contained=True, reason=None
        ),
        flow_run_id="run-resolve",
    )
    assert outcome.exit_code == 1  # stopped at the run writer
    return captured


def _gh(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
    """The ``gh`` process boundary for one failed CI run on an agent PR."""
    out = ""
    if cmd[:3] == ["gh", "run", "view"] and "headBranch" in cmd:
        out = json.dumps({"headBranch": "agent/issue-3"})
    elif cmd[:3] == ["gh", "pr", "view"]:
        out = json.dumps(
            {
                "number": 7,
                "headRefName": "agent/issue-3",
                "headRefOid": "a" * 40,
                "baseRefName": "main",
                "body": "Closes #3",
                "title": "Fix it",
                "labels": [{"name": "agent-pr"}],
            }
        )
    elif "--log-failed" in cmd:
        out = "FAILED tests/test_a.py::test_a"
    return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")


def _launch_ci_fix(
    store: SqliteRunStore,
    config: DispatchConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    """Run the CI-fix launch for real, up to the run writer."""
    captured: dict[str, Any] = {}
    monkeypatch.setattr(ci_fix_module, "run", _gh)
    monkeypatch.setattr(ci_fix_module, "project_root", lambda: tmp_path)
    monkeypatch.setattr(ci_fix_module, "run_tracked_agent", _capture(captured))
    monkeypatch.setattr(ci_fix_module, "require_pinned_pr_head", lambda *_a: None)
    monkeypatch.setattr(
        ci_fix_module,
        "checkout_existing_worktree",
        lambda **_kw: (tmp_path / "worktree", None),
    )
    # The route is the forge-free stand-in the CI-fix suite uses: no runner
    # binary is probed, so the launch reaches the run writer on any host.
    monkeypatch.setattr(ci_fix_module, "resolve_runner", lambda *_a, **_k: MagicMock())
    config.project = ProjectConfig(repo="o/r")
    config.logging.dir = str(tmp_path / "logs")
    config.triggers.ci_failure_max_retries = 2
    dispatch_ci_fix(run_id="123", config=config, store=store)
    return captured


_LAUNCHES = {
    "review_remediation": _launch_review,
    "resolve": _launch_resolve,
    "ci_fix": _launch_ci_fix,
}


@pytest.mark.parametrize("role", ROLES)
def test_the_launch_renders_and_records_the_approved_version(
    store: SqliteRunStore,
    config: DispatchConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    role: str,
) -> None:
    approved = _activate(store, config, role)

    launched = _LAUNCHES[role](store, config, tmp_path, monkeypatch)

    assert launched["prompt"].count(_heading(role)) == 1
    assert launched["prompt"].count(_body(role)) == 1
    assert launched.get("prompt_profile_version_id", "unset") == approved.id
    run_id = save_launch_row(store, launched)
    assert store.run_prompt_profile_version_id(run_id) == approved.id
    stored = store.get(run_id)
    assert stored is not None
    assert stored.prompt_profile_version_id == approved.id


@pytest.mark.parametrize("role", ROLES)
def test_a_launch_under_no_profile_records_null(
    store: SqliteRunStore,
    config: DispatchConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    role: str,
) -> None:
    # Another role's profile is active; this role's launch still runs bare.
    _activate(store, config, "dispatch")

    launched = _LAUNCHES[role](store, config, tmp_path, monkeypatch)

    assert "Operator instructions" not in launched["prompt"]
    assert launched.get("prompt_profile_version_id", "unset") is None
    run_id = save_launch_row(store, launched)
    assert store.run_prompt_profile_version_id(run_id) is None


# ------------------------------------------------- refused active profiles


def _refuse_review(
    store: SqliteRunStore,
    config: DispatchConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    """Run the remediation launch, preparation included, up to the run writer."""
    captured: dict[str, Any] = {}
    monkeypatch.setattr(review_module, "run_tracked_agent", _capture(captured))
    outcomes: list[ReviewOutcome] = []
    flow = _ReviewDispatch(
        pr_number=7,
        config=config,
        author_filter=None,
        store=store,
        force_budget=False,
        item_id="ITEM-3",
        findings=None,
        on_outcome=outcomes.append,
    )
    flow.issue_number = 3
    flow.branch_name = "agent/issue-3"
    flow.pr_title = "Fix it"
    flow.comments = []
    flow.base_branch = "main"
    flow.conflict_info = None
    flow.worktree_dir = tmp_path
    flow.effective_run_kind = "review_remediation"
    flow.flow_run_id = "run-review"
    flow.inference_transport = _TRANSPORT
    flow.resolved = MagicMock()
    flow.resolved.runner.name = "claude-code"
    flow.resolved.runner.publication_style = "host"
    # A sandboxed route asks no host runner binary, so this runs on any host.
    flow.containment = SimpleNamespace(  # type: ignore[assignment]
        posture="sandboxed", image="kew-agent", contained=True, reason=None
    )
    flow.checkout = ReviewCheckout(
        path=tmp_path,
        head_sha="a" * 40,
        branch="agent/issue-3",
        detached=False,
        impl_worktree=None,
        conflict=None,
        claim=None,
    )
    outcome = flow._launch()
    assert outcome is not None
    [recorded] = outcomes
    return {
        "captured": captured,
        "kind": outcome.kind,
        "exit_code": outcome.exit_code,
        "classification": recorded.classification,
        "detail": recorded.detail,
    }


def _refuse_resolve(
    store: SqliteRunStore,
    config: DispatchConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(resolve_module, "run_tracked_agent", _capture(captured))
    monkeypatch.setattr(resolve_module, "require_pinned_pr_head", lambda *_a: None)
    runner = MagicMock()
    runner.name = "claude-code"
    runner.publication_style = "host"
    outcome = _run_conflicted_resolve(
        worktree_dir=tmp_path / "worktree",
        conflict_info=_CONFLICT,
        pr_number=7,
        issue_number=3,
        branch_name="agent/issue-3",
        base_branch="main",
        expected_head_sha="a" * 40,
        pr_title="Fix it",
        config=config,
        store=store,
        producer_run_id=None,
        resolved=SimpleNamespace(runner=runner, model="claude-opus-5"),  # type: ignore[arg-type]
        inference_transport=_TRANSPORT,
        containment=SimpleNamespace(  # type: ignore[arg-type]
            posture="sandboxed", image="kew-agent", contained=True, reason=None
        ),
        flow_run_id="run-resolve",
    )
    return {
        "captured": captured,
        "kind": outcome.kind,
        "exit_code": outcome.exit_code,
        "category": outcome.category,
        "detail": outcome.reason,
    }


def _refuse_ci_fix(
    store: SqliteRunStore,
    config: DispatchConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    checkouts: list[Any] = []
    monkeypatch.setattr(
        ci_fix_module,
        "checkout_existing_worktree",
        lambda **kw: checkouts.append(kw) or (tmp_path / "worktree", None),
    )
    captured: dict[str, Any] = {}
    monkeypatch.setattr(ci_fix_module, "run", _gh)
    monkeypatch.setattr(ci_fix_module, "project_root", lambda: tmp_path)
    monkeypatch.setattr(ci_fix_module, "run_tracked_agent", _capture(captured))
    config.project = ProjectConfig(repo="o/r")
    config.logging.dir = str(tmp_path / "logs")
    config.triggers.ci_failure_max_retries = 2
    exit_code = dispatch_ci_fix(run_id="123", config=config, store=store)
    assert exit_code == CI_FIX_CANNOT_RUN
    # Refused before the checkout, so none is left behind.
    assert checkouts == []
    return {"captured": captured, "kind": "not_run"}


_REFUSALS = {
    "review_remediation": _refuse_review,
    "resolve": _refuse_resolve,
    "ci_fix": _refuse_ci_fix,
}


@pytest.mark.parametrize("role", ROLES)
def test_a_refused_active_profile_starts_no_runner_and_records_the_category(
    store: SqliteRunStore,
    config: DispatchConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    role: str,
) -> None:
    activate_unchecked(store, _body(role), role, body_sha256="0" * 64)

    refused = _REFUSALS[role](store, config, tmp_path, monkeypatch)

    assert refused["captured"] == {}  # no runner was started
    assert refused["kind"] == "not_run"
    [row] = store.list_runs_by_branch("agent/issue-3")
    assert row.status == "refused"
    assert row.error_category == PROMPT_CONFIGURATION_REFUSAL
    assert row.prompt_profile_version_id is None
    if role == "review_remediation":
        assert refused["classification"] == PROMPT_CONFIGURATION_REFUSAL
    if role == "resolve":
        # The raw exit the refused row records, and the typed category.
        assert refused["exit_code"] == row.exit_code == 2
        assert refused["category"] == PROMPT_CONFIGURATION_REFUSAL
    if "detail" in refused:
        assert "does not match the approved digest" in refused["detail"]


def test_refused_review_profile_records_no_failed_outcome_or_error_notification(
    store: SqliteRunStore,
    config: DispatchConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[NotificationEvent] = []
    monkeypatch.setattr(
        "kew.review_outcome.build_notifier",
        lambda _config: SimpleNamespace(notify=sent.append),
    )
    activate_unchecked(
        store, _body("review_remediation"), "review_remediation", body_sha256="0" * 64
    )

    refused = _refuse_review(store, config, tmp_path, monkeypatch)

    [outcome] = list_review_outcomes(store, pr_number=7)
    assert outcome.status == "refused"
    assert outcome.classification == PROMPT_CONFIGURATION_REFUSAL
    [event] = sent
    assert event.severity != "error"
    assert "failed" not in event.message
    assert "refused before launch" in event.message
    # The returned outcome, the run row and the review outcome agree on exit 2.
    [row] = store.list_runs_by_branch("agent/issue-3")
    assert refused["exit_code"] == row.exit_code == outcome.exit_code == 2


@pytest.mark.parametrize("run_kind", ["review_remediation", "resolve", "ci_fix"])
def test_a_prompt_refusal_never_becomes_the_branch_producer(
    tmp_path: Path, run_kind: str
) -> None:
    """A refused fix-up launch started nothing, so publication lineage must skip
    it: after the operator fixes the profile, the relaunch binds to the run that
    pushed the head, not to the refusal (which carries no launch evidence)."""
    from datetime import UTC, datetime, timedelta

    from kew.prompt_refusals import record_prompt_refusal
    from kew.publication_credentials import publication_producer_for_head
    from kew.run_store import RunRecord

    branch = "agent/issue-5"
    pushed = datetime.now(UTC) - timedelta(hours=1)
    with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
        store.save(
            RunRecord(
                run_id="producer-1",
                issue_num=5,
                item_id="5",
                branch=branch,
                runner_name="claude-code",
                run_kind="dispatch",
                status="success",
                exit_code=0,
                started_at=pushed,
                finished_at=pushed + timedelta(minutes=5),
            )
        )
        record_prompt_refusal(
            store,
            DispatchConfig(),
            run_id="refused-1",
            run_kind=run_kind,
            issue_num=5,
            branch=branch,
            item_id="5",
            runner="claude-code",
        )

        producer = publication_producer_for_head(
            store, branch=branch, head_sha="c" * 40
        )

    assert producer == "producer-1"
