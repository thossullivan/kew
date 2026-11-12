"""Characterization (golden/snapshot) tests for dispatch_core boundaries.

Pins the observable behavior of execute_dispatch and run_tracked_agent at
their seams — labels written, worktree lifecycle, RunRecord fields — for
representative dispatch outcomes. Any future silent field-dropping or
label/worktree regression will appear as a diff in these assertions.

Source functions are NOT modified; this file is read-only characterization.
"""

from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest
from support.checkout_generations import (
    fixture_checkout_generation,
    fixture_claim_evidence,
)

from kew.config import DispatchConfig
from kew.tracking import DispatchResult

# Patch target prefix
_P = "kew.dispatch_core"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _launch_image_resolves(monkeypatch):
    """The fail-closed image gate (#1550 F1) refuses when no runtime resolves
    the tag; these tests run with detect_runtime_cached forced to None, so
    resolve to a stable fake ID by default."""
    monkeypatch.setattr(
        "kew.dispatch_sandbox._resolve_launch_image_id",
        lambda config, image, **_: "sha256:" + "f" * 64,
    )


@pytest.fixture(autouse=True)
def _mock_resolved_models_are_not_openrouter_routes(monkeypatch):
    """A patched resolve_runner yields a MagicMock model, which names no route.

    Governed review round 2 (2026-09-21): a direct OpenRouter route without a
    provisioning key is refused on the host too, so the flow tests here that
    never name a model must not read as OpenRouter routes.
    """
    from kew import run_credentials

    real = run_credentials.direct_openrouter_route

    def classify(model, credential_names, *, direct):
        return isinstance(model, str) and real(model, credential_names, direct=direct)

    monkeypatch.setattr(run_credentials, "direct_openrouter_route", classify)


@pytest.fixture
def _canonical_existing_pr_published(monkeypatch: pytest.MonkeyPatch) -> None:
    """Synthetic orchestration checkouts have no tree to publish.

    Keep canonical success routing while mocking its existing-PR publisher.
    Real publication coverage lives in test_publication_success_routing.
    """
    monkeypatch.setattr("kew.dispatch_core._publish_canonical_pr_update", MagicMock())


def _make_issue(
    num: int = 42,
    title: str = "Fix the widget",
    body: str = "It is broken",
    labels: list[dict] | None = None,
) -> dict:
    return {
        "number": num,
        "title": title,
        "body": body,
        "labels": labels or [{"name": "ready"}, {"name": "medium"}],
    }


def _ok_result(issue_num: int = 42, **kw) -> DispatchResult:
    defaults = dict(
        exit_code=0,
        issue_num=issue_num,
        log_file=Path("/tmp/log"),
        session_id="sess-ok",
        error_category="",
        total_tokens=1000,
        input_tokens=800,
        output_tokens=200,
        resolved_model="claude-sonnet-4-6",
    )
    defaults.update(kw)
    return DispatchResult(**defaults)


def _fail_result(issue_num: int = 42, **kw) -> DispatchResult:
    defaults = dict(
        exit_code=1,
        issue_num=issue_num,
        log_file=Path("/tmp/log"),
        session_id="sess-fail",
        error_category="transient",
    )
    defaults.update(kw)
    return DispatchResult(**defaults)


def _make_store(tmp_path: Path):
    from kew.run_store import SqliteRunStore

    return SqliteRunStore(str(tmp_path / "runs.db"))


def _make_config(tmp_path: Path) -> DispatchConfig:
    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")
    config.project.repo = "octo/widgets"
    config.retry.max_attempts = 1
    return config


# ---------------------------------------------------------------------------
# RunRecord field pinning — run_tracked_agent boundary
# ---------------------------------------------------------------------------


class TestRunTrackedAgentRunRecordFields:
    """Pin all RunRecord fields written by run_tracked_agent for each outcome."""

    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.resolve_runner")
    def test_success_record_fields_pinned(
        self,
        mock_resolve_runner: MagicMock,
        mock_run_agent: MagicMock,
        tmp_path: Path,
    ):
        """Success outcome: pin every field that run_tracked_agent must populate."""
        from kew.dispatch_core import run_tracked_agent

        config = _make_config(tmp_path)
        mock_resolve_runner.return_value.runner.name = "claude-code"
        mock_resolve_runner.return_value.model = "claude-sonnet-4-6"

        ok = _ok_result(
            total_tokens=1500,
            input_tokens=1200,
            output_tokens=300,
            cache_creation_tokens=50,
            cache_read_tokens=100,
            resolved_model="claude-sonnet-4-6",
            reported_cost_usd=0.042,
        )
        mock_run_agent.return_value = ok

        with _make_store(tmp_path) as store:
            result = run_tracked_agent(
                allow_unrecorded=True,
                prompt="fix the widget",
                worktree_dir=tmp_path,
                config=config,
                issue_num=42,
                store=store,
                title="Fix the widget",
                branch="agent/issue-42",
                model="claude-sonnet-4-6",
                complexity="medium",
            )

            assert result.exit_code == 0

            records = store.list_by_issue(42)
            assert len(records) == 1
            rec = records[0]

        # Core outcome fields
        assert rec.status == "success"
        assert rec.exit_code == 0
        assert rec.error_category == ""
        assert rec.attempt_number == 1
        assert rec.finished_at is not None
        assert rec.duration_seconds >= 0.0

        # Identity / routing fields — these were previously found under-populated
        assert rec.branch == "agent/issue-42"
        assert rec.complexity == "medium"
        assert rec.runner_name == "claude-code"
        assert rec.model == "claude-sonnet-4-6"
        assert rec.title == "Fix the widget"
        assert rec.issue_num == 42

        # Token / cost fields
        assert rec.total_tokens == 1500
        assert rec.input_tokens == 1200
        assert rec.output_tokens == 300
        assert rec.cache_creation_tokens == 50
        assert rec.cache_read_tokens == 100
        assert rec.estimated_cost_usd == 0.042

        # Runner-reported model (#251) — normalised and stored
        assert rec.reported_model == "claude-sonnet-4-6"

        # run_id is a non-empty UUID string
        assert rec.run_id and len(rec.run_id) == 36

    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.resolve_runner")
    def test_runner_failure_record_fields_pinned(
        self,
        mock_resolve_runner: MagicMock,
        mock_run_agent: MagicMock,
        tmp_path: Path,
    ):
        """Runner failure: pin status, exit_code, error_category, branch, complexity."""
        from kew.dispatch_core import run_tracked_agent

        config = _make_config(tmp_path)
        mock_resolve_runner.return_value.runner.name = "claude-code"
        mock_resolve_runner.return_value.model = "claude-sonnet-4-6"
        mock_run_agent.return_value = _fail_result(
            error_category="permanent", exit_code=1
        )

        with _make_store(tmp_path) as store:
            result = run_tracked_agent(
                allow_unrecorded=True,
                prompt="fix the widget",
                worktree_dir=tmp_path,
                config=config,
                issue_num=42,
                store=store,
                title="Fix the widget",
                branch="agent/issue-42",
                model="claude-sonnet-4-6",
                complexity="complex",
            )

            assert result.exit_code == 1

            records = store.list_by_issue(42)
            assert len(records) == 1
            rec = records[0]

        assert rec.status == "failure"
        assert rec.exit_code == 1
        assert rec.error_category == "permanent"
        assert rec.attempt_number == 1
        assert rec.finished_at is not None

        # Fields that were previously found under-populated
        assert rec.branch == "agent/issue-42"
        assert rec.complexity == "complex"
        assert rec.runner_name == "claude-code"
        assert rec.model == "claude-sonnet-4-6"
        assert rec.title == "Fix the widget"

    @pytest.mark.parametrize("category", ["permanent", "needs_human"])
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.resolve_runner")
    def test_exit_zero_failure_category_is_not_stored_success(
        self,
        mock_resolve_runner: MagicMock,
        mock_run_agent: MagicMock,
        tmp_path: Path,
        category: str,
    ):
        """Hermes exit 0 with permanent error_category must not be stored as success.

        When hermes exits 0 without a usage file (guardrail 403 / credential
        failure), classify_result returns ``permanent``.  _apply_completion_status
        must map this to ``failure``, not ``success``, even though the exit code
        is 0.  Refs #1104.
        """
        from kew.dispatch_core import run_tracked_agent

        config = _make_config(tmp_path)
        # The classification under test needs a launched run: a direct
        # OpenRouter route without provisioning is refused before launch on
        # the host (governed review round 2, 2026-09-21), so the route here is
        # one that launches.
        mock_resolve_runner.return_value.runner.name = "hermes"
        mock_resolve_runner.return_value.model = "claude-sonnet-4-6"
        # Hermes exit 0 + no usage file → classify_result → permanent (#1104 bug)
        mock_run_agent.return_value = _ok_result(
            error_category=category,
            exit_code=0,
        )

        with _make_store(tmp_path) as store:
            result = run_tracked_agent(
                allow_unrecorded=True,
                prompt="fix the widget",
                worktree_dir=tmp_path,
                config=config,
                issue_num=42,
                store=store,
                title="Fix the widget",
                branch="agent/issue-42",
                model="claude-sonnet-4-6",
                complexity="complex",
            )

            assert result.exit_code == 0
            assert result.error_category == category

            records = store.list_by_issue(42)
            assert len(records) == 1
            rec = records[0]

        # Must NOT be "success" — permanent is a failure regardless of exit code
        assert rec.status == "failure"
        assert rec.exit_code == 0
        assert rec.error_category == category
        assert rec.attempt_number == 1

    @patch(f"{_P}.time.sleep")
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.resolve_runner")
    def test_retried_then_success_record_fields_pinned(
        self,
        mock_resolve_runner: MagicMock,
        mock_run_agent: MagicMock,
        mock_sleep: MagicMock,
        tmp_path: Path,
    ):
        """Retried-then-success: attempt_number reflects total attempts."""
        from kew.dispatch_core import run_tracked_agent

        config = _make_config(tmp_path)
        config.retry.max_attempts = 2
        config.retry.backoff_base_seconds = 0
        mock_resolve_runner.return_value.runner.name = "claude-code"
        mock_resolve_runner.return_value.model = "claude-sonnet-4-6"
        mock_resolve_runner.return_value.supports_resume.return_value = False

        # Both attempts have nonzero tokens so the merged total is exact.
        # merge_usage adds prev attempt's tokens into the current result:
        # input = 700 + 80 = 780, output = 200 + 20 = 220, total = 1000.
        transient = _fail_result(
            error_category="transient",
            exit_code=1,
            total_tokens=100,
            input_tokens=80,
            output_tokens=20,
            reported_cost_usd=0.001,
        )
        success = _ok_result(
            total_tokens=900,
            input_tokens=700,
            output_tokens=200,
            reported_cost_usd=0.041,
        )
        mock_run_agent.side_effect = [transient, success]

        with _make_store(tmp_path) as store:
            result = run_tracked_agent(
                allow_unrecorded=True,
                prompt="fix the widget",
                worktree_dir=tmp_path,
                config=config,
                issue_num=42,
                store=store,
                title="Fix the widget",
                branch="agent/issue-42",
                model="claude-sonnet-4-6",
                complexity="medium",
            )

            assert result.exit_code == 0

            records = store.list_by_issue(42)
            assert len(records) == 1
            rec = records[0]

        assert rec.status == "success"
        assert rec.exit_code == 0
        assert rec.attempt_number == 2
        # Branch and complexity still populated despite retry
        assert rec.branch == "agent/issue-42"
        assert rec.complexity == "medium"
        assert rec.runner_name == "claude-code"
        # Exact accumulated totals across both attempts
        assert rec.total_tokens == 1000
        assert rec.input_tokens == 780
        assert rec.output_tokens == 220
        assert rec.estimated_cost_usd == 0.042

    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.resolve_runner")
    def test_budget_exceeded_record_fields_pinned(
        self,
        mock_resolve_runner: MagicMock,
        mock_run_agent: MagicMock,
        tmp_path: Path,
    ):
        """Budget-exceeded outcome: status='budget_exceeded', error_category matches.

        Note: the watchdog path (DispatchResult.budget_exceeded=True) overrides
        error_category to 'budget_stopped' inside run_tracked_agent. This test
        exercises the agent-reported budget-exceeded path (error_category set by
        the runner, not the watchdog), which produces status='budget_exceeded'.
        """
        from kew.dispatch_core import run_tracked_agent

        config = _make_config(tmp_path)
        mock_resolve_runner.return_value.runner.name = "claude-code"
        mock_resolve_runner.return_value.model = "claude-sonnet-4-6"

        # exit_code=0 so the retry loop exits via the success-break, but
        # error_category signals budget exhaustion to the outcome path.
        budget_result = DispatchResult(
            exit_code=0,
            issue_num=42,
            log_file=tmp_path / "log",
            error_category="budget_exceeded",
        )
        mock_run_agent.return_value = budget_result

        with _make_store(tmp_path) as store:
            run_tracked_agent(
                allow_unrecorded=True,
                prompt="fix the widget",
                worktree_dir=tmp_path,
                config=config,
                issue_num=42,
                store=store,
                title="Fix the widget",
                branch="agent/issue-42",
                model="claude-sonnet-4-6",
                complexity="medium",
            )

            records = store.list_by_issue(42)
            assert len(records) == 1
            rec = records[0]

        assert rec.status == "budget_exceeded"
        assert rec.error_category == "budget_exceeded"
        assert rec.branch == "agent/issue-42"
        assert rec.complexity == "medium"
        assert rec.finished_at is not None


# ---------------------------------------------------------------------------
# Label transition pinning — execute_dispatch boundary
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("_canonical_existing_pr_published")
class TestExecuteDispatchLabelTransitions:
    """Pin which label functions are called and in what order for each outcome."""

    def _resolve_runner(self, mock_resolve_runner: MagicMock) -> None:
        mock_resolve_runner.return_value.runner.name = "claude-code"
        mock_resolve_runner.return_value.model = "claude-sonnet-4-6"

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.cleanup_worktree")
    @patch(f"{_P}.check_pr_exists", return_value=99)
    @patch(f"{_P}.update_pr_labels", return_value=True)
    @patch(f"{_P}.mark_completed")
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    @patch(f"{_P}.setup_worktree", return_value=Path("/tmp/wt"))
    @patch(f"{_P}.resolve_runner")
    def test_success_with_pr_calls_mark_completed(
        self,
        mock_resolve_runner: MagicMock,
        mock_setup_wt: MagicMock,
        mock_prompt: MagicMock,
        mock_run_agent: MagicMock,
        mock_mark_completed: MagicMock,
        mock_pr_labels: MagicMock,
        mock_pr_exists: MagicMock,
        mock_cleanup: MagicMock,
        mock_connectivity: MagicMock,
        tmp_path: Path,
    ):
        """Success + PR exists: update_pr_labels → mark_completed → cleanup."""
        from kew.dispatch_core import execute_dispatch

        self._resolve_runner(mock_resolve_runner)
        config = _make_config(tmp_path)
        mock_run_agent.return_value = _ok_result()

        mgr = MagicMock()
        mgr.attach_mock(mock_pr_labels, "update_pr_labels")
        mgr.attach_mock(mock_mark_completed, "mark_completed")
        mgr.attach_mock(mock_cleanup, "cleanup_worktree")

        rc = execute_dispatch(allow_unrecorded=True, issue=_make_issue(), config=config)

        assert rc.exit_code == 0
        assert mgr.mock_calls == [
            call.update_pr_labels(99, config, add=[config.labels.pr_pending_review]),
            call.mark_completed(42, config),
            call.cleanup_worktree(Path("/tmp/wt"), "agent/issue-42"),
        ]

    @patch(f"{_P}._broker_agent_work", return_value=False)
    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.cleanup_worktree")
    @patch(f"{_P}.check_pr_exists", return_value=None)
    @patch(f"{_P}.update_pr_labels", return_value=True)
    @patch(f"{_P}._complete_no_changes")
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    @patch(f"{_P}.setup_worktree", return_value=Path("/tmp/wt"))
    @patch(f"{_P}.resolve_runner")
    def test_success_no_pr_records_a_no_change_candidate(
        self,
        mock_resolve_runner: MagicMock,
        mock_setup_wt: MagicMock,
        mock_prompt: MagicMock,
        mock_run_agent: MagicMock,
        mock_no_change: MagicMock,
        mock_pr_labels: MagicMock,
        mock_pr_exists: MagicMock,
        mock_cleanup: MagicMock,
        mock_connectivity: MagicMock,
        mock_broker: MagicMock,
        tmp_path: Path,
    ):
        """Success but no PR: the outcome is recorded as a no-change candidate
        before cleanup (I10B), and no PR label transition happens."""
        from kew.dispatch_core import execute_dispatch

        self._resolve_runner(mock_resolve_runner)
        config = _make_config(tmp_path)
        mock_run_agent.return_value = _ok_result()

        mgr = MagicMock()
        mgr.attach_mock(mock_no_change, "_complete_no_changes")
        mgr.attach_mock(mock_cleanup, "cleanup_worktree")

        rc = execute_dispatch(allow_unrecorded=True, issue=_make_issue(), config=config)

        assert rc.exit_code == 0
        assert [c[0] for c in mgr.mock_calls] == [
            "_complete_no_changes",
            "cleanup_worktree",
        ]
        assert mock_no_change.call_args.kwargs["item_id"] == "42"
        mock_pr_labels.assert_not_called()

    @patch(f"{_P}._broker_agent_work")
    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.cleanup_worktree")
    @patch(f"{_P}.check_pr_exists", side_effect=RuntimeError("graphql exhausted"))
    @patch(f"{_P}.mark_failed")
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    @patch(f"{_P}.setup_worktree", return_value=Path("/tmp/wt"))
    @patch(f"{_P}.resolve_runner")
    def test_publication_outage_preserves_completed_work_without_redispatch(
        self,
        mock_resolve_runner: MagicMock,
        mock_setup_wt: MagicMock,
        mock_prompt: MagicMock,
        mock_run_agent: MagicMock,
        mock_mark_failed: MagicMock,
        mock_pr_exists: MagicMock,
        mock_cleanup: MagicMock,
        mock_connectivity: MagicMock,
        mock_broker: MagicMock,
        tmp_path: Path,
    ) -> None:
        """A provider outage is publication work, never a new implementation run."""
        from kew.dispatch_core import execute_dispatch
        from kew.publication import PublicationError

        self._resolve_runner(mock_resolve_runner)
        mock_resolve_runner.return_value.runner.publication_style = "host"
        config = _make_config(tmp_path)
        publication_log = tmp_path / "publication.log"
        publication_log.write_text("agent output\n")
        mock_run_agent.return_value = _ok_result(log_file=publication_log)
        mock_broker.side_effect = PublicationError(
            "pr_create",
            "rate limit exceeded at "
            "https://alice:supersecret@github.com/octo/widgets?token=ghp_bad",
        )

        with _make_store(tmp_path) as store:
            outcome = execute_dispatch(
                allow_unrecorded=True, issue=_make_issue(), config=config, store=store
            )
            record = store.list_by_issue(42)[0]
            deferred = store.list_audit_events(event="publication_deferred")

        assert outcome.exit_code == 1
        assert record.status == "awaiting_publication"
        assert record.error_category == "publication_rate_limited"
        assert len(deferred) == 1
        assert deferred[0].run_id == record.run_id
        assert deferred[0].payload == {
            "branch": "agent/issue-42",
            "category": "publication_rate_limited",
            "step": "pr_create",
            "error": "rate limit exceeded at https://github.com/octo/widgets",
            "worktree_preserved": True,
        }
        log_text = publication_log.read_text()
        assert "publication failed" in log_text
        assert "step=pr_create" in log_text
        assert "publication_rate_limited" in log_text
        assert "supersecret" not in log_text
        assert "ghp_bad" not in log_text
        mock_broker.assert_called_once()
        mock_mark_failed.assert_not_called()
        mock_cleanup.assert_not_called()

    @patch(f"{_P}._broker_agent_work", side_effect=RuntimeError("rate limit exceeded"))
    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.check_pr_exists", side_effect=RuntimeError("graphql exhausted"))
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    @patch(f"{_P}.setup_worktree")
    @patch(f"{_P}.resolve_runner")
    def test_publication_deferral_registers_epic_continuation_handle(
        self,
        mock_resolve_runner: MagicMock,
        mock_setup_wt: MagicMock,
        mock_prompt: MagicMock,
        mock_run_agent: MagicMock,
        mock_pr_exists: MagicMock,
        mock_connectivity: MagicMock,
        mock_broker: MagicMock,
        tmp_path: Path,
    ) -> None:
        from kew.dispatch_core import execute_dispatch
        from kew.worktree import write_checkout_generation

        self._resolve_runner(mock_resolve_runner)
        mock_resolve_runner.return_value.runner.publication_style = "host"
        worktree = tmp_path / "preserved"
        (worktree / ".git").mkdir(parents=True)
        write_checkout_generation(worktree)
        mock_setup_wt.return_value = worktree
        mock_run_agent.return_value = _ok_result()
        config = _make_config(tmp_path)
        # The escape hatch: canonical minting needs a trusted base, which a
        # mocked checkout has none of (review round 15 of #1440).
        config.publication.publish = "checkout"
        config.sandbox.dangerously_skip_containment = True

        with _make_store(tmp_path) as store:
            execute_dispatch(
                allow_unrecorded=True,
                issue=_make_issue(),
                config=config,
                store=store,
                epic_ref="epic:x",
            )
            continuations = store.list_continuations("epic:x")
            detected = store.list_audit_events(event="continuation_detected")

        assert len(continuations) == 1
        assert continuations[0].item_id == "42"
        assert continuations[0].original_run_id
        assert continuations[0].branch == "agent/issue-42"
        assert continuations[0].worktree_identity == str(worktree.resolve())
        assert continuations[0].state == "awaiting_continuation"
        assert len(detected) == 1
        assert detected[0].payload["outcome"] == "publication_deferred"

    def test_publication_deferral_does_not_overwrite_concurrent_terminal_write(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stale publication failure cannot replace a newer terminal state."""
        from datetime import UTC, datetime

        from kew.dispatch_core import _defer_publication, _DispatchContext
        from kew.run_store import RunRecord

        config = _make_config(tmp_path)
        with _make_store(tmp_path) as store:
            now = datetime.now(UTC)
            store.save(
                RunRecord(
                    run_id="run-race",
                    issue_num=42,
                    item_id="42",
                    branch="agent/issue-42",
                    status="success",
                    exit_code=0,
                    started_at=now,
                    finished_at=now,
                )
            )
            transition = store.transition_run_status

            def terminalize_before_compare(run_id: str, **kwargs: object) -> bool:
                current = store.get(run_id)
                assert current is not None
                current.status = "killed"
                current.exit_code = 137
                current.error_category = "killed"
                store.save(current)
                return transition(run_id, **kwargs)

            monkeypatch.setattr(
                store, "transition_run_status", terminalize_before_compare
            )
            ctx = MagicMock(spec=_DispatchContext)
            ctx.config = config
            ctx.store = store
            ctx.issue_num = 42
            ctx.item_id = "42"
            ctx.branch = "agent/issue-42"

            outcome = _defer_publication(
                ctx,
                _ok_result(run_id="run-race"),
                tmp_path / "preserved",
                RuntimeError("rate limit exceeded"),
            )

            current = store.get("run-race")
            assert outcome.exit_code == 1
            assert current is not None
            assert current.status == "killed"
            assert current.error_category == "killed"
            assert store.list_audit_events(event="publication_deferred") == []

    def test_publication_deferral_reuses_claimed_continuation_handle(
        self, tmp_path: Path
    ) -> None:
        from datetime import UTC, datetime

        from kew.dispatch_core import _defer_publication, _DispatchContext
        from kew.run_store import RunRecord

        config = _make_config(tmp_path)
        worktree = tmp_path / "preserved"
        worktree.mkdir()
        with _make_store(tmp_path) as store:
            original = store.record_continuation(
                "epic:x",
                "42",
                "original-run",
                "agent/issue-42",
                worktree,
                checkout_generation=fixture_checkout_generation(worktree),
            )
            assert (
                store.claim_continuation_under_cap(
                    original.epic_ref,
                    original.item_id,
                    original.original_run_id,
                    original.branch,
                    original.worktree_identity,
                    "continuation-run",
                    2,
                    claim_evidence=fixture_claim_evidence(
                        store,
                        original.epic_ref,
                        original.item_id,
                        original.original_run_id,
                        original.branch,
                        original.worktree_identity,
                        "continuation-run",
                    ),
                )
                == "claimed"
            )
            now = datetime.now(UTC)
            store.save(
                RunRecord(
                    run_id="continuation-run",
                    issue_num=42,
                    item_id="42",
                    branch="agent/issue-42",
                    status="success",
                    exit_code=0,
                    started_at=now,
                    finished_at=now,
                )
            )
            ctx = MagicMock(spec=_DispatchContext)
            ctx.config = config
            ctx.store = store
            ctx.epic_ref = "epic:x"
            ctx.issue_num = 42
            ctx.item_id = "42"
            ctx.branch = "agent/issue-42"

            _defer_publication(
                ctx,
                _ok_result(run_id="continuation-run"),
                worktree,
                RuntimeError("rate limit exceeded"),
            )

            continuations = store.list_continuations("epic:x")
            assert len(continuations) == 1
            assert continuations[0].original_run_id == "original-run"
            assert continuations[0].state == "continuing"

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.mark_failed")
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    @patch(f"{_P}.setup_worktree", return_value=Path("/tmp/wt"))
    @patch(f"{_P}.resolve_runner")
    def test_runner_failure_calls_mark_failed(
        self,
        mock_resolve_runner: MagicMock,
        mock_setup_wt: MagicMock,
        mock_prompt: MagicMock,
        mock_run_agent: MagicMock,
        mock_mark_failed: MagicMock,
        mock_connectivity: MagicMock,
        tmp_path: Path,
    ):
        """Runner failure: mark_failed called with exit code info."""
        from kew.dispatch_core import execute_dispatch

        self._resolve_runner(mock_resolve_runner)
        config = _make_config(tmp_path)
        mock_run_agent.return_value = _fail_result(error_category="permanent")

        rc = execute_dispatch(allow_unrecorded=True, issue=_make_issue(), config=config)

        assert rc.exit_code == 1
        mock_mark_failed.assert_called_once()
        call_args = mock_mark_failed.call_args
        assert call_args[0][0] == 42  # issue_num
        assert "exit code 1" in call_args[0][2]  # reason string

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.cleanup_worktree")
    @patch(f"{_P}._checkout_has_work", return_value=False)
    @patch(f"{_P}.mark_completed")
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    @patch(f"{_P}.setup_worktree", return_value=Path("/tmp/wt"))
    @patch(f"{_P}.resolve_runner")
    def test_budget_exceeded_clean_worktree_calls_mark_completed(
        self,
        mock_resolve_runner: MagicMock,
        mock_setup_wt: MagicMock,
        mock_prompt: MagicMock,
        mock_run_agent: MagicMock,
        mock_mark_completed: MagicMock,
        mock_uncommitted: MagicMock,
        mock_cleanup: MagicMock,
        mock_connectivity: MagicMock,
        tmp_path: Path,
    ):
        """Budget-exceeded (agent-reported) + clean worktree: mark_completed + cleanup.

        Uses error_category='budget_exceeded' without the watchdog flag (which
        would write 'budget_stopped'). Clean worktree -> mark_completed.
        """
        from kew.dispatch_core import execute_dispatch

        self._resolve_runner(mock_resolve_runner)
        config = _make_config(tmp_path)
        # The escape hatch: canonical mode records a no-change candidate for a
        # clean budget stop instead of completing (review round 11 of #1440).
        config.publication.publish = "checkout"
        config.sandbox.dangerously_skip_containment = True

        budget_result = DispatchResult(
            exit_code=0,
            issue_num=42,
            log_file=Path("/tmp/log"),
            error_category="budget_exceeded",
        )
        mock_run_agent.return_value = budget_result

        mgr = MagicMock()
        mgr.attach_mock(mock_mark_completed, "mark_completed")
        mgr.attach_mock(mock_cleanup, "cleanup_worktree")

        rc = execute_dispatch(allow_unrecorded=True, issue=_make_issue(), config=config)

        assert rc.exit_code == 0
        assert mgr.mock_calls == [
            call.mark_completed(42, config),
            call.cleanup_worktree(Path("/tmp/wt"), "agent/issue-42"),
        ]

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.cleanup_worktree")
    @patch(f"{_P}._checkout_has_work", return_value=True)
    @patch(f"{_P}.mark_failed")
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    @patch(f"{_P}.setup_worktree", return_value=Path("/tmp/wt"))
    @patch(f"{_P}.resolve_runner")
    def test_budget_exceeded_dirty_worktree_calls_mark_failed(
        self,
        mock_resolve_runner: MagicMock,
        mock_setup_wt: MagicMock,
        mock_prompt: MagicMock,
        mock_run_agent: MagicMock,
        mock_mark_failed: MagicMock,
        mock_uncommitted: MagicMock,
        mock_cleanup: MagicMock,
        mock_connectivity: MagicMock,
        tmp_path: Path,
    ):
        """Budget-exceeded (agent-reported) + dirty worktree: mark_failed with label.

        Uses error_category='budget_exceeded' without the watchdog flag so
        execute_dispatch reaches the budget_exceeded branch (not budget_stopped).
        """
        from kew.dispatch_core import execute_dispatch

        self._resolve_runner(mock_resolve_runner)
        config = _make_config(tmp_path)
        # The escape hatch: canonical mode records a no-change candidate for a
        # clean budget stop instead of completing (review round 11 of #1440).
        config.publication.publish = "checkout"
        config.sandbox.dangerously_skip_containment = True

        budget_result = DispatchResult(
            exit_code=0,
            issue_num=42,
            log_file=Path("/tmp/log"),
            error_category="budget_exceeded",
        )
        mock_run_agent.return_value = budget_result

        rc = execute_dispatch(allow_unrecorded=True, issue=_make_issue(), config=config)

        assert rc.exit_code == 1
        mock_mark_failed.assert_called_once()
        call_args = mock_mark_failed.call_args
        assert call_args[1].get("extra_labels") == [config.labels.budget_exceeded]
        # Worktree preserved (not cleaned up)
        mock_cleanup.assert_not_called()

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.cleanup_worktree")
    @patch(f"{_P}.mark_failed")
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    @patch(f"{_P}.setup_worktree", return_value=Path("/tmp/wt"))
    @patch(f"{_P}.resolve_runner")
    def test_budget_stopped_calls_mark_failed_with_budget_stop_label(
        self,
        mock_resolve_runner: MagicMock,
        mock_setup_wt: MagicMock,
        mock_prompt: MagicMock,
        mock_run_agent: MagicMock,
        mock_mark_failed: MagicMock,
        mock_cleanup: MagicMock,
        mock_connectivity: MagicMock,
        tmp_path: Path,
    ):
        """Live watchdog (budget_exceeded): mark_failed + graceful budget_stop label.

        Sets budget_exceeded=True so run_tracked_agent's watchdog branch executes
        (dispatch_core.py: ``if result.budget_exceeded: result.error_category =
        'budget_stop'``). This exercises the actual trigger, not a
        directly-set error_category (#859).
        """
        from kew.dispatch_core import execute_dispatch

        self._resolve_runner(mock_resolve_runner)
        config = _make_config(tmp_path)

        # Watchdog trigger: budget_exceeded=True causes run_tracked_agent to set
        # error_category="budget_stop" before returning to execute_dispatch.
        stopped_result = DispatchResult(
            exit_code=1,
            issue_num=42,
            log_file=Path("/tmp/log"),
            budget_exceeded=True,
        )
        mock_run_agent.return_value = stopped_result

        mgr = MagicMock()
        mgr.attach_mock(mock_mark_failed, "mark_failed")

        rc = execute_dispatch(allow_unrecorded=True, issue=_make_issue(), config=config)

        assert rc.exit_code == 1
        assert len(mgr.mock_calls) == 1
        assert mgr.mock_calls[0].kwargs.get("extra_labels") == [
            config.labels.budget_stop
        ]
        # Worktree preserved for graceful budget stop
        mock_cleanup.assert_not_called()

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.cleanup_worktree")
    @patch(f"{_P}.reset_issue_to_ready")
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    @patch(f"{_P}.setup_worktree", return_value=Path("/tmp/wt"))
    @patch(f"{_P}.resolve_runner")
    def test_killed_calls_reset_to_ready_and_preserves_worktree(
        self,
        mock_resolve_runner: MagicMock,
        mock_setup_wt: MagicMock,
        mock_prompt: MagicMock,
        mock_run_agent: MagicMock,
        mock_reset: MagicMock,
        mock_cleanup: MagicMock,
        mock_connectivity: MagicMock,
        tmp_path: Path,
    ):
        """Operator kill: reset_issue_to_ready called, worktree preserved."""
        from kew.dispatch_core import execute_dispatch

        self._resolve_runner(mock_resolve_runner)
        config = _make_config(tmp_path)

        killed_result = DispatchResult(
            exit_code=1,
            issue_num=42,
            log_file=Path("/tmp/log"),
            error_category="killed",
        )
        mock_run_agent.return_value = killed_result

        rc = execute_dispatch(allow_unrecorded=True, issue=_make_issue(), config=config)

        assert rc.exit_code == 1
        mock_reset.assert_called_once_with(42, config)
        mock_cleanup.assert_not_called()


# ---------------------------------------------------------------------------
# Worktree lifecycle pinning — execute_dispatch boundary
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("_canonical_existing_pr_published")
class TestExecuteDispatchWorktreeLifecycle:
    """Pin worktree create/cleanup lifecycle for each dispatch outcome.

    The runner-failure case (worktree preserved, mark_failed called) is pinned
    at this same layer by tests/test_dispatch_core.py::TestExecuteDispatchFailed
    ::test_failure_preserves_worktree; the near-copy here was removed in #2343.
    """

    def _resolve_runner(self, mock_resolve_runner: MagicMock) -> None:
        mock_resolve_runner.return_value.runner.name = "claude-code"
        mock_resolve_runner.return_value.model = "claude-sonnet-4-6"

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.cleanup_worktree")
    @patch(f"{_P}.check_pr_exists", return_value=99)
    @patch(f"{_P}.update_pr_labels", return_value=True)
    @patch(f"{_P}.mark_completed")
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    @patch(f"{_P}.setup_worktree", return_value=Path("/tmp/wt"))
    @patch(f"{_P}.resolve_runner")
    def test_success_creates_and_cleans_up_worktree(
        self,
        mock_resolve_runner: MagicMock,
        mock_setup_wt: MagicMock,
        mock_prompt: MagicMock,
        mock_run_agent: MagicMock,
        mock_mark_completed: MagicMock,
        mock_pr_labels: MagicMock,
        mock_pr_exists: MagicMock,
        mock_cleanup: MagicMock,
        mock_connectivity: MagicMock,
        tmp_path: Path,
    ):
        """Success: setup_worktree called once, cleanup_worktree called once."""
        from kew.dispatch_core import execute_dispatch

        self._resolve_runner(mock_resolve_runner)
        config = _make_config(tmp_path)
        mock_run_agent.return_value = _ok_result()

        rc = execute_dispatch(allow_unrecorded=True, issue=_make_issue(), config=config)

        assert rc.exit_code == 0
        mock_setup_wt.assert_called_once()
        # The branch is threaded from the dispatch context, never read back out
        # of the agent's directory (#1445).
        mock_cleanup.assert_called_once_with(Path("/tmp/wt"), "agent/issue-42")

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.mark_failed")
    @patch(f"{_P}.setup_worktree")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    def test_setup_worktree_exception_does_not_call_cleanup(
        self,
        mock_prompt: MagicMock,
        mock_setup_wt: MagicMock,
        mock_mark_failed: MagicMock,
        mock_connectivity: MagicMock,
        tmp_path: Path,
    ):
        """Exception from setup_worktree: mark_failed called, cleanup never called."""
        from kew.dispatch_core import execute_dispatch

        config = _make_config(tmp_path)
        mock_setup_wt.side_effect = RuntimeError("disk full")

        with (
            patch(f"{_P}.cleanup_worktree") as mock_cleanup,
            patch(f"{_P}.resolve_runner") as mock_resolve_runner,
        ):
            self._resolve_runner(mock_resolve_runner)
            rc = execute_dispatch(
                allow_unrecorded=True, issue=_make_issue(), config=config
            )

        assert rc.exit_code == 1
        mock_mark_failed.assert_called_once()
        mock_cleanup.assert_not_called()


# ---------------------------------------------------------------------------
# RunRecord field pinning via execute_dispatch (integration slice)
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("_canonical_existing_pr_published")
class TestExecuteDispatchRunRecordFields:
    """Pin RunRecord fields written through the full execute_dispatch path."""

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.cleanup_worktree")
    @patch(f"{_P}.check_pr_exists", return_value=77)
    @patch(f"{_P}.update_pr_labels", return_value=True)
    @patch(f"{_P}.mark_completed")
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    @patch(f"{_P}.setup_worktree", return_value=Path("/tmp/wt"))
    @patch(f"{_P}.resolve_runner")
    def test_success_run_record_carries_branch_and_complexity(
        self,
        mock_resolve_runner: MagicMock,
        mock_setup_wt: MagicMock,
        mock_prompt: MagicMock,
        mock_run_agent: MagicMock,
        mock_mark_completed: MagicMock,
        mock_pr_labels: MagicMock,
        mock_pr_exists: MagicMock,
        mock_cleanup: MagicMock,
        mock_connectivity: MagicMock,
        tmp_path: Path,
    ):
        """Branch and complexity populated in RunRecord (were under-populated)."""
        from kew.dispatch_core import execute_dispatch

        mock_resolve_runner.return_value.runner.name = "claude-code"
        mock_resolve_runner.return_value.model = "claude-sonnet-4-6"

        config = _make_config(tmp_path)
        ok = _ok_result(total_tokens=500, input_tokens=400, output_tokens=100)
        ok.resolved_model = "claude-sonnet-4-6"
        mock_run_agent.return_value = ok

        with _make_store(tmp_path) as store:
            rc = execute_dispatch(
                allow_unrecorded=True,
                issue=_make_issue(
                    num=55,
                    title="Refactor the gizmo",
                    labels=[{"name": "ready"}, {"name": "complex"}],
                ),
                config=config,
                store=store,
            )

        assert rc.exit_code == 0

        with _make_store(tmp_path) as store:
            records = store.list_by_issue(55)

        assert len(records) == 1
        rec = records[0]

        # The two fields found under-populated in the dogfood (#468 context)
        assert rec.branch == f"{config.branches.prefix}55"
        assert rec.complexity == "complex"

        # Other identity fields must also be set
        assert rec.title == "Refactor the gizmo"
        assert rec.runner_name == "claude-code"
        assert rec.model == "claude-sonnet-4-6"
        assert rec.issue_num == 55
        assert rec.status == "success"

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.cleanup_worktree")
    @patch(f"{_P}.mark_failed")
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    @patch(f"{_P}.setup_worktree", return_value=Path("/tmp/wt"))
    @patch(f"{_P}.resolve_runner")
    def test_failure_run_record_carries_branch_and_complexity(
        self,
        mock_resolve_runner: MagicMock,
        mock_setup_wt: MagicMock,
        mock_prompt: MagicMock,
        mock_run_agent: MagicMock,
        mock_mark_failed: MagicMock,
        mock_cleanup: MagicMock,
        mock_connectivity: MagicMock,
        tmp_path: Path,
    ):
        """Failure outcome: branch and complexity still populated in RunRecord."""
        from kew.dispatch_core import execute_dispatch

        mock_resolve_runner.return_value.runner.name = "claude-code"
        mock_resolve_runner.return_value.model = "claude-sonnet-4-6"

        config = _make_config(tmp_path)
        mock_run_agent.return_value = _fail_result(
            issue_num=33, error_category="permanent"
        )

        with _make_store(tmp_path) as store:
            rc = execute_dispatch(
                allow_unrecorded=True,
                issue=_make_issue(
                    num=33,
                    title="Broken thing",
                    labels=[{"name": "ready"}, {"name": "simple"}],
                ),
                config=config,
                store=store,
            )

        assert rc.exit_code == 1

        with _make_store(tmp_path) as store:
            records = store.list_by_issue(33)

        assert len(records) == 1
        rec = records[0]

        assert rec.branch == f"{config.branches.prefix}33"
        assert rec.complexity == "simple"
        assert rec.title == "Broken thing"
        assert rec.status == "failure"
