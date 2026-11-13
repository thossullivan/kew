"""W1.1 node 3d-3: review and resolve actually run their agent in a container.

The seam and the decision landed separately (#1461, #1463). This is the wiring,
and the properties worth testing are the ones the wiring could quietly lose:
the image reaching the container, the cost cap surviving the move, and the
publication head-binding staying on the HOST side of the boundary.
"""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from support.source_ast import (
    called,
    calls,
    definition,
    dotted,
    keyword_text,
    module_tree,
    reachable,
)

from kew.config import DispatchConfig
from kew.dispatch_core import budget_watchdog
from kew.tracking import DispatchResult

IMAGE = "ghcr.io/example/agent:latest"


@pytest.fixture(autouse=True)
def _launch_image_resolves(monkeypatch):
    """The fail-closed image gate (#1550 F1) refuses when no runtime resolves
    the tag; these tests run with detect_runtime_cached forced to None, so
    resolve to a stable fake ID by default."""
    monkeypatch.setattr(
        "kew.dispatch_sandbox._resolve_launch_image_id",
        lambda config, image, **_: "sha256:" + "f" * 64,
    )


def _ok() -> DispatchResult:
    return DispatchResult(exit_code=0, issue_num=42, log_file=Path("/tmp/log"))


class TestTheImageReachesTheContainer:
    @patch("kew.dispatch_core.check_pr_exists", return_value=None)
    @patch("kew.dispatch_core.run_agent")
    def test_a_contained_run_hands_run_agent_that_image(
        self, mock_run_agent, _pr, tmp_path: Path
    ):
        from kew.dispatch_core import run_tracked_agent

        mock_run_agent.return_value = _ok()
        runner = MagicMock()
        runner.name = "claude-code"
        runner.container_args.return_value = ["--model", "claude-opus-5"]

        with patch(
            "kew.dispatch_core._require_contained_image_id",
            return_value="sha256:" + "f" * 64,
        ):
            run_tracked_agent(
                allow_unrecorded=True,
                prompt="p",
                worktree_dir=tmp_path,
                config=DispatchConfig(),
                issue_num=42,
                runner=runner,
                model="claude-opus-5",
                contained_image=IMAGE,
            )

        contained = mock_run_agent.call_args.kwargs["contained"]
        assert contained is not None
        # The argv launches by the RESOLVED immutable ID, never the tag
        # (#1550 review F1) — what the profile captures is what runs.
        assert "sha256:" + "f" * 64 in contained.argv
        assert IMAGE not in contained.argv
        # The container is named for the work item, and that name is what a
        # budget or breaker kill uses to reach it.
        assert contained.name == "kew-42"

    @patch("kew.dispatch_core.check_pr_exists", return_value=None)
    @patch("kew.dispatch_core.run_agent")
    def test_no_image_means_host_execution(self, mock_run_agent, _pr, tmp_path: Path):
        from kew.dispatch_core import run_tracked_agent

        mock_run_agent.return_value = _ok()
        runner = MagicMock()
        runner.name = "claude-code"

        run_tracked_agent(
            allow_unrecorded=True,
            prompt="p",
            worktree_dir=tmp_path,
            config=DispatchConfig(),
            issue_num=42,
            runner=runner,
            model="m",
        )

        assert mock_run_agent.call_args.kwargs["contained"] is None
        runner.container_args.assert_not_called()


class TestTheCostCapSurvivesContainment:
    """#1462. The regression this wiring would otherwise have shipped.

    ``max_remediation_usd`` (#993) reaches claude-code as an argv flag, and the
    container command deliberately omits that flag. Moving these flows into a
    container without a kew-side watchdog would have removed the cap entirely
    while looking like a pure relocation.
    """

    def test_claude_code_on_the_host_still_self_caps(self):
        """Unchanged: kew does not double-enforce what the agent already does."""
        assert budget_watchdog(40.0, runner_name="claude-code", contained=False) is None

    def test_claude_code_in_a_container_is_watched_by_kew(self):
        check = budget_watchdog(40.0, runner_name="claude-code", contained=True)
        assert check is not None
        assert check(40.01) is True
        assert check(39.99) is False

    def test_a_runner_that_never_gets_the_flag_is_watched_either_way(self):
        """The flag is claude-code-only, so every other runner needs kew."""
        for contained in (True, False):
            check = budget_watchdog(25.0, runner_name="opencode", contained=contained)
            assert check is not None, contained
            assert check(25.5) is True, contained

    @pytest.mark.parametrize("cap", [None, 0.0])
    def test_no_cap_means_no_watchdog(self, cap):
        """A zero cap is 'unset', not 'kill immediately'."""
        assert budget_watchdog(cap, runner_name="opencode", contained=True) is None


class TestReviewWiring:
    """The flow-level assertions: what dispatch_review hands the tracked agent."""

    def _run(
        self,
        containment,
        tmp_path: Path,
        *,
        item_id: str | None = None,
        ci: str = "",
    ):
        from kew.review import dispatch_review

        # Route resolution hashes the validated inference transport config.
        config = DispatchConfig()
        config.project.repo = "owner/repo"
        config.review.include_resolved = False
        config.review.auto_reply = False
        config.review.max_remediation_usd = 40.0
        config.agent.max_budget_usd = 25.0
        config.branches.base = "main"
        config.commands.ci = ci
        config.commands.format = ""
        config.publication.publish = "canonical"
        config.commands.test_targeted = "uv run pytest {paths}"
        config.commands.test_policy_mark = "policy"

        runner = MagicMock()
        runner.name = "claude-code"
        runner.publication_style = "agent"

        with (
            patch("kew.review._get_pr_metadata") as meta,
            patch("kew.review.fetch_review_comments") as fetch,
            patch("kew.review.materialize_review_checkout") as checkout,
            patch("kew.review.resolve_runner") as resolve,
            patch("kew.review.run_tracked_agent") as tracked,
            patch("kew.review.attest_flow_posture"),
            patch(
                "kew.review.resolve_remediation_containment", return_value=containment
            ),
            patch("kew.review.discard_checkout"),
            patch("kew.review.detect_base_branch", return_value="main"),
            patch("kew.review._check_github_connectivity", return_value=True),
            patch("kew.review.check_budget"),
        ):
            meta.return_value = {
                "headRefName": "fix/branch",
                "baseRefName": "main",
                "body": "Closes #99",
                "title": "Fix thing",
            }
            fetch.return_value = [MagicMock(path="f.py", body="fix this")]
            checkout.return_value = MagicMock(
                path=tmp_path, head_sha="a" * 40, detached=False, conflict=None
            )
            resolve.return_value = MagicMock(runner=runner, model="claude-opus-5")
            tracked.return_value = _ok()

            store = MagicMock()
            # Explicit None: the default mock return gets compared numerically
            # (record.issue_num > 0) and a mock is not inert (#1464).
            store.find_run_by_branch.return_value = None
            store.publication_jobs_for_head.return_value = []
            store.normalized_jobs_for_head.return_value = []
            store.list_runs_by_branch_after.return_value = ([], None)

            dispatch_review(pr_number=10, config=config, store=store, item_id=item_id)

        return tracked.call_args.kwargs

    def test_a_contained_decision_reaches_the_tracked_agent(self, tmp_path: Path):
        from kew.remediation_containment import RemediationContainment

        kwargs = self._run(
            RemediationContainment(posture="sandboxed", image=IMAGE), tmp_path
        )

        assert kwargs["contained_image"] == IMAGE
        # And the cap is now kew's to enforce, because the container command
        # does not carry claude-code's own budget flag.
        assert kwargs["budget_check"] is not None

    def test_an_uncontained_decision_runs_on_the_host_unchanged(self, tmp_path: Path):
        from kew.remediation_containment import RemediationContainment

        kwargs = self._run(
            RemediationContainment(posture="uncontained_flow", reason="no runtime"),
            tmp_path,
        )

        assert kwargs["contained_image"] is None
        # claude-code on the host self-caps; kew must not start double-killing.
        assert kwargs["budget_check"] is None

    def test_a_contained_governed_round_leaves_the_policy_gates_to_the_host(
        self, tmp_path: Path
    ):
        """#2984: the round runs under the sandbox's 8 GB cap, so its brief
        does not send it to the repo-wide policy gates."""
        from kew.remediation_containment import RemediationContainment

        kwargs = self._run(
            RemediationContainment(posture="sandboxed", image=IMAGE),
            tmp_path,
            item_id="item-1",
        )

        assert "uv run pytest -m policy" not in kwargs["prompt"]
        assert "8 GB memory cap" in kwargs["prompt"]

    def test_governed_uncontained_remediation_keeps_host_policy_gates(
        self, tmp_path: Path
    ):
        """#2984: a governed round on the host has no sandbox cap to respect,
        so its brief keeps the policy gates and claims no 8 GB sandbox."""
        from kew.remediation_containment import RemediationContainment

        kwargs = self._run(
            RemediationContainment(posture="uncontained_flow", reason="no runtime"),
            tmp_path,
            item_id="item-1",
        )

        assert "Run the gates with: `uv run pytest -m policy`" in kwargs["prompt"]
        assert "8 GB" not in kwargs["prompt"]

    def test_governed_native_remediation_keeps_host_policy_gates(self, tmp_path: Path):
        """#2984: a ``native`` runner isolates itself on the host, outside the
        container's memory cap, so its brief keeps the policy gates and claims
        no 8 GB sandbox."""
        from kew.remediation_containment import RemediationContainment

        kwargs = self._run(
            RemediationContainment(posture="native"), tmp_path, item_id="item-1"
        )

        assert "Run the gates with: `uv run pytest -m policy`" in kwargs["prompt"]
        assert "8 GB" not in kwargs["prompt"]

    def test_a_contained_standalone_review_runs_no_full_suite(self, tmp_path: Path):
        """#2984: `kew review` without an item still runs under the 8 GB cap
        when it is contained, so its brief names targeted tests and leaves the
        configured CI command and the policy gates to the verify host."""
        from kew.remediation_containment import RemediationContainment

        kwargs = self._run(
            RemediationContainment(posture="sandboxed", image=IMAGE),
            tmp_path,
            ci="uv run pytest -v",
        )

        prompt = kwargs["prompt"]
        assert "Run the full test suite" not in prompt
        assert "uv run pytest -v" not in prompt
        assert "-m policy" not in prompt
        assert "Targeted tests only" in prompt
        assert "8 GB memory cap" in prompt


class TestPublicationStaysOnTheHost:
    """The #1290 head-binding must not move inside the container's blast radius.

    This is the whole reason execution and publication were kept apart. A test
    that only checked "the agent ran in a container" would pass just as happily
    with the push folded in.
    """

    _PUBLISHERS = frozenset(
        {"publish_existing_pr_branch", "_canonical_review_publication"}
    )

    @staticmethod
    def _stage_calls(stages, method: str) -> set[str]:
        """Every callee the stage *method* reaches through ``self`` calls."""
        cls = definition(stages)
        methods = {
            node.name: node for node in cls.body if isinstance(node, ast.FunctionDef)
        }
        return {
            name.rsplit(".", 1)[-1]
            for reached in reachable(cls, method)
            for name in called(methods[reached])
        } | reachable(cls, method)

    @pytest.mark.policy
    def test_dispatch_review_completes_only_after_the_launch_stage(self):
        """Publication runs from ``_complete``, which runs after every stage."""
        import kew.review as review

        dispatch = definition(review.dispatch_review)
        launch = [
            node
            for node in ast.walk(dispatch)
            if isinstance(node, ast.Attribute) and node.attr == "_launch"
        ]
        complete = [
            call for call in calls(dispatch) if dotted(call.func) == "review._complete"
        ]
        assert len(launch) == 1 and len(complete) == 1
        # The stage loop that runs ``_launch`` returns on any refusal, so the one
        # ``_complete()`` it falls through to is the only way past it.
        loop = next(
            node
            for node in ast.walk(dispatch)
            if isinstance(node, ast.For) and launch[0] in ast.walk(node.iter)
        )
        assert (complete[0].lineno, complete[0].col_offset) > (
            loop.end_lineno,
            loop.end_col_offset,
        )

    @pytest.mark.policy
    def test_review_publishes_after_the_agent_returns(self):
        import kew.review as review

        stages = review._ReviewDispatch
        launched = self._stage_calls(stages, "_launch")
        completed = self._stage_calls(stages, "_complete")
        # The agent runs from the launch stage, and nothing it reaches publishes.
        assert "run_tracked_agent" in launched
        assert not launched & (self._PUBLISHERS | {"_publish"}), launched
        # Publication is the completion stage's, after the agent returned.
        assert "_publish" in completed
        assert "publish_existing_pr_branch" in completed
        assert "run_tracked_agent" not in completed

    @pytest.mark.policy
    def test_the_host_push_is_bound_to_the_reviewed_head(self):
        """The #1290 head-binding: the push names the head the review was cut at."""
        import kew.review as review

        publish = definition(review._ReviewDispatch._publish)
        pushes = [
            call
            for call in calls(publish)
            if dotted(call.func) == "publish_existing_pr_branch"
        ]
        assert pushes
        for push in pushes:
            assert keyword_text(push, "expected_remote_sha") == "self.checkout.head_sha"

    @pytest.mark.policy
    def test_the_decision_module_does_not_publish(self):
        """It used to carry a launcher too; it must not regrow one that pushes."""
        import kew.remediation_containment as module

        tree = module_tree(module)
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        }
        assert not any(name.startswith("publish") for name in imported)
        assert "run_container_and_stream" not in imported
