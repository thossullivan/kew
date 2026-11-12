"""The host clears human CI approval before it pushes new agent code (#2033).

Private-repository CI runs an agent PR on the self-hosted runner only while
the ``labels.ci_approved`` label is present (tests/test_workflow_agent_pr_gate.py).
These tests pin the host half: every host push path that can update an
existing agent PR removes that label first, fails closed when it cannot, and a
first publication never adds it.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kew import dispatch_core, resolve, review
from kew.config import DispatchConfig, LabelConfig, ProjectConfig
from kew.loops.delegate import DelegateDriver
from kew.publication import PublicationError
from kew.publication_canonical import CanonicalNoChange, CanonicalPublication

pytestmark = pytest.mark.usefixtures("real_ci_approval_revoke")

SHA = "a" * 40
PUBLICATION = CanonicalPublication(
    commit_sha="b" * 40,
    tree_sha="c" * 40,
    parent_shas=(SHA,),
    changed_paths=("src/x.py",),
    flags=(),
)


def _config(**labels: str) -> DispatchConfig:
    return DispatchConfig(
        project=ProjectConfig(repo="owner/repo"), labels=LabelConfig(**labels)
    )


class _GitHub:
    """A fake ``gh`` that records every command, in order, with the pushes."""

    def __init__(self, prs: list[dict[str, Any]], *, fail: str = "") -> None:
        self.prs = prs
        self.fail = fail
        self.calls: list[list[str]] = []

    def run(self, cmd: list[str], **_kwargs: Any) -> SimpleNamespace:
        self.calls.append(list(cmd))
        method = cmd[cmd.index("--method") + 1] if "--method" in cmd else ""
        if self.fail == method:
            return SimpleNamespace(returncode=1, stdout="", stderr="HTTP 502")
        if method == "GET":
            return SimpleNamespace(returncode=0, stdout=json.dumps(self.prs), stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def push(self, *_args: Any, **_kwargs: Any) -> CanonicalPublication:
        self.calls.append(["git", "push"])
        return PUBLICATION

    def methods(self) -> list[str]:
        return [
            cmd[cmd.index("--method") + 1] if "--method" in cmd else " ".join(cmd)
            for cmd in self.calls
        ]


def _pr(number: int, *labels: str) -> dict[str, Any]:
    return {"number": number, "labels": [{"name": label} for label in labels]}


# --- the helper -------------------------------------------------------------


def test_removes_the_approval_label_from_the_open_pr_on_the_branch() -> None:
    github = _GitHub([_pr(12, "agent-pr", "ci-approved")])
    with patch.object(dispatch_core, "run", github.run):
        cleared = dispatch_core.revoke_ci_approval("agent/issue-5", _config())
    assert cleared == (12,)
    lookup, delete = github.calls
    assert "repos/owner/repo/pulls" in lookup
    assert "head=owner:agent/issue-5" in lookup and "state=open" in lookup
    assert delete[-1] == "repos/owner/repo/issues/12/labels/ci-approved"
    assert delete[delete.index("--method") + 1] == "DELETE"


def test_uses_the_configured_label_name() -> None:
    github = _GitHub([_pr(3, "agent-pr", "ci-approved", "run ci")])
    with patch.object(dispatch_core, "run", github.run):
        cleared = dispatch_core.revoke_ci_approval("b", _config(ci_approved="run ci"))
    assert cleared == (3,)
    assert github.calls[-1][-1] == "repos/owner/repo/issues/3/labels/run%20ci"


def test_an_unapproved_pr_or_no_pr_is_left_alone() -> None:
    for prs in ([], [_pr(12, "agent-pr")]):
        github = _GitHub(prs)
        with patch.object(dispatch_core, "run", github.run):
            assert dispatch_core.revoke_ci_approval("b", _config()) == ()
        assert github.methods() == ["GET"]


@pytest.mark.parametrize("failing", ["GET", "DELETE"])
def test_fails_closed_when_github_cannot_answer(failing: str) -> None:
    github = _GitHub([_pr(12, "ci-approved")], fail=failing)
    with (
        patch.object(dispatch_core, "run", github.run),
        pytest.raises(PublicationError),
    ):
        dispatch_core.revoke_ci_approval("b", _config())


def test_a_malformed_lookup_fails_closed() -> None:
    def run(cmd: list[str], **_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(returncode=0, stdout='{"message": "x"}', stderr="")

    with patch.object(dispatch_core, "run", run), pytest.raises(PublicationError):
        dispatch_core.revoke_ci_approval("b", _config())


# --- dispatch publication ---------------------------------------------------

NO_CHANGE = CanonicalNoChange(artifact=MagicMock(), base_sha=SHA, tree_sha="d" * 40)


class _Publisher:
    """A publication step that honours ``before_push`` like the real ones.

    With a new head it runs the hook, then pushes. With no change it returns
    no-change evidence and never runs the hook: that is the contract of
    ``publish_checkout`` and the checkout escape hatch (#2033).
    """

    def __init__(self, github: _GitHub, *, changed: bool, result: Any = None) -> None:
        self.github = github
        self.changed = changed
        self.result = result
        self.hooks: list[Any] = []

    def __call__(self, *_args: Any, **kwargs: Any) -> Any:
        hook = kwargs.get("before_push")
        self.hooks.append(hook)
        if not self.changed:
            return NO_CHANGE if self.result is None else self.result
        if hook is not None:
            hook()
        self.github.calls.append(["git", "push"])
        return PUBLICATION if self.result is None else self.result


def _push_and_open_pr(github: _GitHub, publisher: _Publisher) -> str | None:
    with (
        patch.object(dispatch_core, "run", github.run),
        patch.object(
            dispatch_core, "select_publication_mode", return_value="canonical"
        ),
        patch.object(dispatch_core, "_git_push_branch", publisher),
        patch.object(
            dispatch_core,
            "_create_pull_request",
            return_value=("https://github.com/owner/repo/pull/12", 12),
        ),
    ):
        return dispatch_core._push_and_open_pr(
            Path("/tmp/checkout"), "agent/issue-5", "5", "Fix", _config()
        )


def test_a_first_publication_never_adds_the_approval_label() -> None:
    github = _GitHub([])
    assert _push_and_open_pr(github, _Publisher(github, changed=True))
    assert github.methods() == ["GET", "git push", "POST"]
    label_call = github.calls[-1]
    assert "labels[]=agent-pr" in label_call
    assert not any("ci-approved" in part for cmd in github.calls for part in cmd)


def test_a_republish_onto_an_approved_pr_clears_approval_before_the_push() -> None:
    github = _GitHub([_pr(12, "agent-pr", "ci-approved")])
    assert _push_and_open_pr(github, _Publisher(github, changed=True))
    assert github.methods()[:3] == ["GET", "DELETE", "git push"]


def test_no_push_when_approval_cannot_be_cleared() -> None:
    github = _GitHub([_pr(12, "agent-pr", "ci-approved")], fail="DELETE")
    with pytest.raises(PublicationError):
        _push_and_open_pr(github, _Publisher(github, changed=True))
    assert "git push" not in github.methods()


def test_a_no_change_republish_keeps_the_approval() -> None:
    github = _GitHub([_pr(12, "agent-pr", "ci-approved")])
    _push_and_open_pr(github, _Publisher(github, changed=False))
    assert "DELETE" not in github.methods()


def _continuation(
    github: _GitHub, publisher: _Publisher, tmp_path: Path, *, canonical: bool
) -> Any:
    config = _config()
    if not canonical:
        config.publication.publish = "checkout"
    ctx = SimpleNamespace(
        config=config,
        worktree_dir=tmp_path,
        branch="agent/issue-5",
        publication_base_sha=SHA,
        item_id="5",
        issue_num=5,
        issue_title="Fix the widget",
        store=None,
        run_id="run-1",
    )
    with (
        patch.object(dispatch_core, "run", github.run),
        patch.object(dispatch_core, "_git_push_branch", publisher),
        patch.object(dispatch_core, "select_publication_mode"),
        patch.object(dispatch_core.worktree, "publish_existing_pr_branch", publisher),
        patch.object(dispatch_core, "_defer_publication", return_value="deferred"),
    ):
        return dispatch_core._publish_existing_pr_continuation(
            ctx, SimpleNamespace(run_id="run-1", log_file=None), tmp_path, False
        )


@pytest.mark.parametrize("canonical", [True, False])
def test_existing_pr_continuation_clears_approval_before_the_push(
    tmp_path: Path, canonical: bool
) -> None:
    github = _GitHub([_pr(12, "agent-pr", "ci-approved")])
    outcome = _continuation(
        github, _Publisher(github, changed=True), tmp_path, canonical=canonical
    )
    assert outcome is None
    assert github.methods() == ["GET", "DELETE", "git push"]


@pytest.mark.parametrize("canonical", [True, False])
def test_a_no_change_continuation_keeps_the_approval(
    tmp_path: Path, canonical: bool
) -> None:
    github = _GitHub([_pr(12, "agent-pr", "ci-approved")])
    publisher = _Publisher(github, changed=False, result=NO_CHANGE)
    outcome = _continuation(github, publisher, tmp_path, canonical=canonical)
    assert outcome is None
    assert github.methods() == []
    assert publisher.hooks and publisher.hooks[0] is not None


def test_a_continuation_whose_approval_cannot_be_cleared_defers(
    tmp_path: Path,
) -> None:
    github = _GitHub([_pr(12, "ci-approved")], fail="GET")
    outcome = _continuation(
        github, _Publisher(github, changed=True), tmp_path, canonical=True
    )
    assert outcome == "deferred"
    assert "git push" not in github.methods()


def _loop_iteration(github: _GitHub, publisher: _Publisher, tmp_path: Path) -> Any:
    driver = SimpleNamespace(
        _config=_config(),
        _store=None,
        _loop_has_pr=lambda loop, branch: True,
        _record_publication_fact=lambda *args: None,
    )
    binding = SimpleNamespace(
        require_generation=lambda *args, **kwargs: None,
        lineage_run_id="run-1",
        run_id="run-1",
        base_sha=SHA,
    )
    with (
        patch.object(dispatch_core, "run", github.run),
        patch.object(dispatch_core, "_git_push_branch", publisher),
    ):
        return DelegateDriver._publish_iteration(
            driver,  # type: ignore[arg-type]
            SimpleNamespace(issue_num=5),  # type: ignore[arg-type]
            tmp_path,
            binding,  # type: ignore[arg-type]
        )


def test_a_loop_iteration_onto_its_pr_clears_approval_before_the_push(
    tmp_path: Path,
) -> None:
    github = _GitHub([_pr(12, "agent-pr", "ci-approved")])
    assert _loop_iteration(github, _Publisher(github, changed=True), tmp_path) == (
        PUBLICATION
    )
    assert github.methods() == ["GET", "DELETE", "git push"]


def test_a_no_change_loop_iteration_keeps_the_approval(tmp_path: Path) -> None:
    github = _GitHub([_pr(12, "agent-pr", "ci-approved")])
    assert _loop_iteration(github, _Publisher(github, changed=False), tmp_path) == (
        NO_CHANGE
    )
    assert github.methods() == []


# --- remediation flows -------------------------------------------------------


def _review_flow(tmp_path: Path, mode: str) -> Any:
    flow = object.__new__(review._ReviewDispatch)
    flow.config = _config()
    flow.store = None
    flow.producer_run_id = None
    flow.result = SimpleNamespace(run_id="run-1")
    flow.publication_mode = mode
    flow.worktree_dir = tmp_path
    flow.branch_name = "agent/issue-5"
    # ``integrated_base_sha`` is the base commit kew's own checkout integration
    # merged, which canonical publication reads to tell that merge's content
    # from a copy of a base head it never merged (#2457).
    flow.checkout = SimpleNamespace(head_sha=SHA, integrated_base_sha="e" * 40)
    flow.pr_number = 12
    flow.host_publishes = True
    flow.item_id = None
    # The PR's base, which canonical publication reads for the protected-path
    # review flags (#2362).
    flow.base_branch = "main"
    return flow


@pytest.mark.parametrize("mode", ["canonical", "checkout"])
@pytest.mark.parametrize("changed", [True, False])
def test_review_remediation_clears_approval_only_for_a_new_head(
    tmp_path: Path, mode: str, changed: bool
) -> None:
    github = _GitHub([_pr(12, "agent-pr", "ci-approved")])
    publisher = _Publisher(github, changed=changed)
    flow = _review_flow(tmp_path, mode)
    with (
        patch.object(dispatch_core, "run", github.run),
        patch.object(review, "publish_existing_pr_branch", publisher),
        patch.object(review, "_worktree_head_sha", return_value="b" * 40),
    ):
        flow._publish()
    expected = ["GET", "DELETE", "git push"] if changed else []
    assert github.methods() == expected


def _conflict_resolution(publisher: _Publisher, tmp_path: Path) -> Any:
    with (
        patch.object(resolve, "_require_remediation_publication"),
        patch.object(
            resolve, "_publication_extraction_boundary", return_value=(None, None)
        ),
        patch.object(resolve, "_publish_resolved_branch", publisher),
        patch.object(resolve, "cleanup_worktree"),
    ):
        return resolve._finish_conflict_resolution(
            result=SimpleNamespace(exit_code=0, stop_reason="", run_id="run-1"),
            containment=None,
            host_publication=True,
            publication_mode="canonical",
            worktree_dir=tmp_path,
            conflict_info=SimpleNamespace(base_sha=SHA, strategy_used="merge"),
            pr_number=12,
            issue_number=5,
            branch_name="agent/issue-5",
            base_branch="main",
            expected_head_sha=SHA,
            config=_config(),
            store=None,
        )


@pytest.mark.parametrize("changed", [True, False])
def test_conflict_resolution_clears_approval_only_for_a_new_head(
    tmp_path: Path, changed: bool
) -> None:
    github = _GitHub([_pr(12, "agent-pr", "ci-approved")])
    with patch.object(dispatch_core, "run", github.run):
        outcome = _conflict_resolution(_Publisher(github, changed=changed), tmp_path)
    assert outcome.exit_code == 0
    assert github.methods() == (["GET", "DELETE", "git push"] if changed else [])


@pytest.mark.parametrize("changed", [True, False])
def test_clean_integration_clears_approval_only_for_a_new_head(
    tmp_path: Path, changed: bool
) -> None:
    github = _GitHub([_pr(12, "agent-pr", "ci-approved")])
    publisher = _Publisher(
        github, changed=changed, result="pushed" if changed else "up_to_date"
    )
    with (
        patch.object(dispatch_core, "run", github.run),
        patch.object(resolve, "select_publication_mode"),
        patch.object(resolve, "_push_clean_integration", publisher),
        patch.object(resolve, "cleanup_worktree"),
    ):
        outcome = resolve._finish_clean_resolution(
            worktree_dir=tmp_path,
            pr_number=12,
            issue_number=5,
            branch_name="agent/issue-5",
            base_branch="main",
            expected_head_sha=SHA,
            integrated_base_sha=SHA,
            config=_config(),
            store=None,
            producer_run_id=None,
        )
    assert outcome.exit_code == 0
    assert github.methods() == (["GET", "DELETE", "git push"] if changed else [])


# --- the hook reaches the step that pushes -----------------------------------


def _forwarded(call: Any, target: tuple[Any, str], **patches: Any) -> Any:
    """Run *call* with *target* faked, and return the hook it received."""
    seen: dict[str, Any] = {}

    def fake(*_args: Any, **kwargs: Any) -> Any:
        seen["hook"] = kwargs.get("before_push")
        return patches.pop("_result", PUBLICATION)

    module, name = target
    with patch.object(module, name, fake):
        call()
    return seen.get("hook")


def test_every_layer_forwards_the_hook_to_the_push(tmp_path: Path) -> None:
    from kew import worktree

    hook = MagicMock()
    # dispatch: _git_push_branch -> canonical and checkout pushes.
    with patch.object(
        dispatch_core, "select_publication_mode", return_value="canonical"
    ):
        assert (
            _forwarded(
                lambda: dispatch_core._git_push_branch(
                    "b", tmp_path, base_sha=SHA, before_push=hook
                ),
                (dispatch_core, "_canonical_push_branch"),
            )
            is hook
        )
    with patch.object(
        dispatch_core, "select_publication_mode", return_value="checkout"
    ):
        assert (
            _forwarded(
                lambda: dispatch_core._git_push_branch("b", tmp_path, before_push=hook),
                (dispatch_core, "_checkout_push_branch"),
            )
            is hook
        )
    with (
        patch.object(dispatch_core, "publication_remote", return_value="/r"),
        patch.object(dispatch_core, "remote_branch_sha", return_value=""),
        patch.object(dispatch_core, "_publication_owner"),
        patch.object(dispatch_core, "_deliver_dispatch_no_change"),
    ):
        assert (
            _forwarded(
                lambda: dispatch_core._canonical_push_branch(
                    "b",
                    tmp_path,
                    repo=None,
                    base_sha=SHA,
                    message="m",
                    runtime=None,
                    image=None,
                    proxy_environment="none",
                    max_attempts=1,
                    delays=(),
                    sleep=lambda _s: None,
                    on_no_change=None,
                    before_push=hook,
                ),
                (dispatch_core, "publish_checkout"),
            )
            is hook
        )
    # worktree: publish_existing_pr_branch -> publish_checkout.
    with patch.object(worktree, "select_publication_mode", return_value="canonical"):
        assert (
            _forwarded(
                lambda: worktree.publish_existing_pr_branch(
                    tmp_path, "b", commit_message="m", before_push=hook
                ),
                (worktree, "_publish_existing_pr_branch_canonical"),
            )
            is hook
        )
    with (
        patch.object(worktree, "publication_remote", return_value="/r"),
        patch.object(worktree, "remote_branch_sha", return_value=SHA),
    ):
        assert (
            _forwarded(
                lambda: worktree._publish_existing_pr_branch_canonical(
                    tmp_path,
                    "b",
                    commit_message="m",
                    expected_remote_sha=SHA,
                    repo=None,
                    before_push=hook,
                ),
                (worktree, "publish_checkout"),
            )
            is hook
        )
    # review: the canonical host path.
    assert (
        _forwarded(
            lambda: review._canonical_review_publication(
                worktree_dir=tmp_path,
                branch_name="b",
                expected_head_sha=SHA,
                pr_number=12,
                repo=None,
                host_publishes=True,
                before_push=hook,
            ),
            (review, "publish_existing_pr_branch"),
        )
        is hook
    )
    # resolve: conflicted and clean, both modes.
    resolved = {
        "worktree_dir": tmp_path,
        "branch_name": "b",
        "expected_head_sha": SHA,
        "base_sha": SHA,
        "base_branch": "main",
        "repo": "owner/repo",
        "commit_message": "m",
        "force_with_lease": False,
        "before_push": hook,
    }
    with patch.object(resolve, "select_publication_mode", return_value="canonical"):
        assert (
            _forwarded(
                lambda: resolve._publish_resolved_branch(**resolved),
                (resolve, "_publish_resolved_branch_canonical"),
            )
            is hook
        )
    with (
        patch.object(resolve, "select_publication_mode", return_value="checkout"),
        patch.object(resolve, "_assert_resolved_worktree"),
        patch.object(resolve, "_commit_publication_worktree"),
        patch.object(resolve, "_worktree_head_sha", return_value="b" * 40),
        patch.object(resolve, "_assert_resolution_ancestry"),
        patch.object(resolve, "_verify_resolved_branch"),
    ):
        assert (
            _forwarded(
                lambda: resolve._publish_resolved_branch(**resolved),
                (resolve, "publish_existing_pr_branch"),
            )
            is hook
        )
    canonical_scene = (
        patch.object(resolve, "publication_remote", return_value="/r"),
        patch.object(
            resolve,
            "remote_branch_sha",
            side_effect=lambda remote, branch, scratch: SHA,
        ),
        patch.object(resolve, "remote_commit_is_ancestor", return_value=False),
    )
    with canonical_scene[0], canonical_scene[1], canonical_scene[2]:
        assert (
            _forwarded(
                lambda: resolve._publish_resolved_branch_canonical(
                    worktree_dir=tmp_path,
                    branch_name="b",
                    expected_head_sha=SHA,
                    base_sha=SHA,
                    base_branch="main",
                    repo="owner/repo",
                    commit_message="m",
                    runtime=None,
                    image=None,
                    proxy_environment="none",
                    before_push=hook,
                ),
                (resolve, "publish_checkout"),
            )
            is hook
        )
    with canonical_scene[0], canonical_scene[1], canonical_scene[2]:
        assert (
            _forwarded(
                lambda: resolve._push_clean_integration(
                    worktree_dir=tmp_path,
                    branch_name="b",
                    base_branch="main",
                    strategy="merge",
                    expected_head_sha=SHA,
                    base_sha=SHA,
                    repo="owner/repo",
                    before_push=hook,
                ),
                (resolve, "publish_checkout"),
            )
            is hook
        )
    with (
        patch.object(resolve, "select_publication_mode", return_value="checkout"),
        patch.object(resolve, "has_uncommitted_changes", return_value=False),
        patch.object(resolve, "publication_remote_sha", return_value=SHA),
        patch.object(resolve, "_worktree_head_sha", return_value="b" * 40),
        patch.object(
            resolve, "subprocess_run", return_value=SimpleNamespace(returncode=0)
        ),
    ):
        assert (
            _forwarded(
                lambda: resolve._push_clean_integration(
                    worktree_dir=tmp_path,
                    branch_name="b",
                    base_branch="main",
                    strategy="merge",
                    expected_head_sha=SHA,
                    base_sha=SHA,
                    repo="owner/repo",
                    publish="checkout",
                    before_push=hook,
                ),
                (resolve, "publish_existing_pr_branch"),
            )
            is hook
        )
    hook.assert_not_called()
