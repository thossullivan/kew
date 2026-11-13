"""Canonical success requires publication evidence before checkout cleanup."""

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import test_publication_round3 as publication_fixtures
from support.host_git_boundary import record_git_calls

from kew import dispatch_core
from kew.config import DispatchConfig
from kew.publication import PublicationError
from kew.run_store import RunRecord, SqliteRunStore
from kew.tracking import DispatchResult
from kew.worktree import write_checkout_generation

_git = publication_fixtures._git
_git_on_allowlist = publication_fixtures._git_on_allowlist
scene = publication_fixtures.scene


def _context(config, checkout, *, continued=False, store=None, base="a" * 40):
    return SimpleNamespace(
        config=config,
        issue_num=42,
        item_id="42",
        issue_title="Repair the widget",
        complexity="simple",
        branch="agent/issue-42",
        worktree_dir=checkout if continued else None,
        publication_style="agent",
        publication_base_sha=base,
        store=store,
        run_id="run-42",
        notifier=None,
    )


def _result(tmp_path):
    return DispatchResult(
        exit_code=0, issue_num=42, log_file=tmp_path / "run.log", run_id="run-42"
    )


@pytest.mark.parametrize("continued", [False, True])
def test_existing_pr_publication_failure_defers_without_cleanup(tmp_path, continued):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "work.txt").write_text("unpublished work\n")
    ctx = _context(DispatchConfig(), checkout, continued=continued)
    deferred = dispatch_core.DispatchOutcome(exit_code=1, changed_files=[])
    with (
        patch.object(dispatch_core, "check_pr_exists", return_value=99),
        patch.object(
            dispatch_core,
            "_git_push_branch",
            side_effect=PublicationError("push", "branch moved"),
        ) as publish,
        patch.object(
            dispatch_core, "_defer_publication", return_value=deferred
        ) as defer,
        patch.object(dispatch_core, "_mark_success_pr") as complete,
        patch.object(dispatch_core, "_get_changed_files", return_value=[]),
        patch.object(dispatch_core, "_cleanup_dispatch_dir") as cleanup,
    ):
        outcome = dispatch_core._collect_success(
            ctx, _result(tmp_path), checkout, False
        )
    assert outcome is deferred
    publish.assert_called_once()
    assert publish.call_args.kwargs["base_sha"] == ctx.publication_base_sha
    assert publish.call_args.kwargs["run_id"] == "run-42"
    defer.assert_called_once()
    complete.assert_not_called()
    cleanup.assert_not_called()
    assert (checkout / "work.txt").read_text() == "unpublished work\n"


def test_canonical_agent_style_lookup_failure_brokers_before_cleanup(tmp_path):
    checkout = tmp_path / "checkout"
    ctx = _context(DispatchConfig(), checkout)
    deferred = dispatch_core.DispatchOutcome(exit_code=1, changed_files=[])
    with (
        patch.object(
            dispatch_core,
            "check_pr_exists",
            side_effect=RuntimeError("lookup unavailable"),
        ),
        patch.object(
            dispatch_core, "_broker_or_complete", return_value=deferred
        ) as broker,
        patch.object(dispatch_core, "_mark_success_pr") as complete,
        patch.object(dispatch_core, "_get_changed_files", return_value=[]),
        patch.object(dispatch_core, "_cleanup_dispatch_dir") as cleanup,
    ):
        outcome = dispatch_core._collect_success(
            ctx, _result(tmp_path), checkout, False
        )
    assert outcome is deferred
    broker.assert_called_once()
    assert broker.call_args.args[0] is ctx
    complete.assert_not_called()
    cleanup.assert_not_called()


def test_continuation_refuses_foreign_remote_head_and_preserves_tree(scene, tmp_path):
    checkout = Path(scene["checkout"])
    origin = Path(scene["origin"])
    seed = Path(scene["seed"])
    branch = "agent/issue-42"
    _git(seed, "checkout", "-qb", branch)
    (seed / "foreign.txt").write_text("another run's work\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-qm", "foreign work")
    _git(seed, "push", "-q", "origin", branch)
    foreign = _git(origin, "rev-parse", f"refs/heads/{branch}")
    (checkout / "edit.txt").write_text("preserved continuation work\n")
    config = DispatchConfig()
    config.project.repo = str(origin)
    store = SqliteRunStore(":memory:")
    store.save(
        RunRecord(
            run_id="run-42",
            issue_num=42,
            branch=branch,
            status="success",
            exit_code=0,
            started_at=datetime.now(UTC),
        )
    )
    ctx = _context(
        config, checkout, continued=True, store=store, base=str(scene["base"])
    )
    deferred = dispatch_core.DispatchOutcome(exit_code=1, changed_files=[])
    with (
        patch.object(dispatch_core, "check_pr_exists", return_value=99),
        patch.object(
            dispatch_core, "_defer_publication", return_value=deferred
        ) as defer,
        patch.object(dispatch_core, "_mark_success_pr") as complete,
        patch.object(dispatch_core, "_cleanup_dispatch_dir") as cleanup,
        record_git_calls() as calls,
    ):
        outcome = dispatch_core._collect_success(
            ctx, _result(tmp_path), checkout, False
        )
    assert outcome is deferred
    defer.assert_called_once()
    assert "intent" in str(defer.call_args.args[3]).lower()
    complete.assert_not_called()
    cleanup.assert_not_called()
    assert calls
    assert not [
        call.describe() for call in calls if call.repository().resolves_inside(checkout)
    ]
    assert _git(origin, "rev-parse", f"refs/heads/{branch}") == foreign
    assert (checkout / "edit.txt").read_text() == "preserved continuation work\n"


@pytest.mark.parametrize("continued", [False, True])
def test_existing_pr_publishes_owned_work_before_cleanup(scene, tmp_path, continued):
    checkout = Path(scene["checkout"])
    origin = Path(scene["origin"])
    config = DispatchConfig()
    config.project.repo = str(origin)
    store = SqliteRunStore(":memory:")
    branch = "agent/issue-42"
    store.save(
        RunRecord(
            run_id="run-42",
            issue_num=42,
            branch=branch,
            status="success",
            exit_code=0,
            started_at=datetime.now(UTC),
        )
    )
    write_checkout_generation(checkout)
    ctx = _context(
        config, checkout, continued=continued, store=store, base=str(scene["base"])
    )
    (checkout / "edit.txt").write_text("first published edit\n")
    first = dispatch_core._git_push_branch(
        branch,
        checkout,
        repo=str(origin),
        base_sha=str(scene["base"]),
        store=store,
        run_id="run-42",
        item_id="42",
    )
    (checkout / "edit.txt").write_text("latest uncommitted edit\n")

    def assert_published_before_cleanup(*args, **kwargs):
        assert (
            _git(origin, "show", f"refs/heads/{branch}:edit.txt")
            == "latest uncommitted edit"
        )

    with (
        patch.object(dispatch_core, "check_pr_exists", return_value=99),
        patch.object(dispatch_core, "_mark_success_pr") as complete,
        patch.object(
            dispatch_core,
            "_cleanup_dispatch_dir",
            side_effect=assert_published_before_cleanup,
        ) as cleanup,
        record_git_calls() as calls,
    ):
        outcome = dispatch_core._collect_success(
            ctx, _result(tmp_path), checkout, False
        )
    assert outcome.exit_code == 0
    complete.assert_called_once()
    cleanup.assert_called_once()
    assert calls
    assert not [
        call.describe() for call in calls if call.repository().resolves_inside(checkout)
    ]
    assert _git(origin, "rev-parse", f"refs/heads/{branch}^") == first.commit_sha
