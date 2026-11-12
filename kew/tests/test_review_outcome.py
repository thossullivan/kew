"""Tests for watcher-independent review outcome persistence + notification (#1291).

A review that ends with nobody attached must still leave a durable, queryable
record and fire a notification.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from support.gh_fakes import GhProcessBoundary, install_placeholder_forge
from support.host_git_boundary import record_git_calls

from kew import subprocess_utils
from kew.config import DispatchConfig
from kew.notifier import NotificationEvent
from kew.review import dispatch_review
from kew.review_checkout import CHECKOUT_BUSY, ReviewCheckoutError
from kew.review_outcome import (
    REVIEW_OUTCOME_EVENT,
    STAGE_PRE_LAUNCH,
    STAGE_RUNNER,
    STATUS_COMPLETED,
    STATUS_FAILED,
    ReviewOutcome,
    latest_review_outcome,
    list_review_outcomes,
    record_review_outcome,
)
from kew.run_store import SqliteRunStore
from kew.runners.claude_code import ClaudeCodeRunner
from kew.runners.codex import CodexRunner
from kew.runners.registry import ResolvedRoute
from kew.subprocess_utils import preflight_tools


@pytest.fixture(autouse=True)
def _placeholder_forge(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ``gh`` call reaches GitHub: the forge answers as ``o/r`` does (#3069)."""
    install_placeholder_forge(monkeypatch)


class _RecordingNotifier:
    def __init__(self) -> None:
        self.events: list[NotificationEvent] = []

    def notify(self, event: NotificationEvent) -> None:
        self.events.append(event)


class _BrokenNotifier:
    def notify(self, event: NotificationEvent) -> None:
        raise RuntimeError("webhook down")


def _config() -> DispatchConfig:
    config = DispatchConfig()
    config.project.repo = "owner/repo"
    return config


def _failure(pr_number: int = 1288) -> ReviewOutcome:
    return ReviewOutcome(
        pr_number=pr_number,
        status=STATUS_FAILED,
        stage=STAGE_PRE_LAUNCH,
        classification=CHECKOUT_BUSY,
        detail="review checkout is claimed by another reviewer",
        branch="agent/issue-42",
        head_sha="b" * 40,
        item_id="SFD-187",
    )


class TestPersistence:
    def test_outcome_survives_process_exit(self, tmp_path: Path) -> None:
        """The record outlives the writing store — no watcher required."""
        db = str(tmp_path / "d.db")
        with SqliteRunStore(db) as store:
            record_review_outcome(
                _failure(), store=store, config=_config(), notify=False
            )

        # A fresh process would open a fresh store; so does this.
        with SqliteRunStore(db) as reopened:
            outcome = latest_review_outcome(reopened, 1288)
        assert outcome is not None
        assert outcome.status == STATUS_FAILED
        assert outcome.stage == STAGE_PRE_LAUNCH
        assert outcome.classification == CHECKOUT_BUSY
        assert outcome.branch == "agent/issue-42"
        assert outcome.head_sha == "b" * 40
        assert outcome.item_id == "SFD-187"

    def test_outcome_is_queryable_through_the_audit_trail(self, tmp_path: Path) -> None:
        with SqliteRunStore(str(tmp_path / "d.db")) as store:
            record_review_outcome(
                _failure(), store=store, config=_config(), notify=False
            )
            events = store.list_audit_events(event=REVIEW_OUTCOME_EVENT)
        assert len(events) == 1
        assert events[0].payload is not None
        assert events[0].payload["classification"] == CHECKOUT_BUSY
        assert events[0].issue_num == 1288

    def test_latest_wins_and_history_is_kept(self, tmp_path: Path) -> None:
        with SqliteRunStore(str(tmp_path / "d.db")) as store:
            record_review_outcome(
                _failure(), store=store, config=_config(), notify=False
            )
            record_review_outcome(
                ReviewOutcome(
                    pr_number=1288,
                    status=STATUS_COMPLETED,
                    stage=STAGE_RUNNER,
                    classification="completed",
                ),
                store=store,
                config=_config(),
                notify=False,
            )
            latest = latest_review_outcome(store, 1288)
            history = list_review_outcomes(store, pr_number=1288)
        assert latest is not None and latest.ok
        assert [o.classification for o in history] == ["completed", CHECKOUT_BUSY]

    def test_outcomes_are_scoped_per_pr(self, tmp_path: Path) -> None:
        with SqliteRunStore(str(tmp_path / "d.db")) as store:
            record_review_outcome(
                _failure(1288), store=store, config=_config(), notify=False
            )
            record_review_outcome(
                _failure(1291), store=store, config=_config(), notify=False
            )
            assert latest_review_outcome(store, 1291) is not None
            assert len(list_review_outcomes(store, pr_number=1288)) == 1

    def test_missing_store_does_not_raise(self) -> None:
        record_review_outcome(_failure(), store=None, config=_config(), notify=False)


class TestNotification:
    def test_failure_is_notified(self, tmp_path: Path) -> None:
        notifier = _RecordingNotifier()
        with SqliteRunStore(str(tmp_path / "d.db")) as store:
            record_review_outcome(
                _failure(), store=store, config=_config(), notifier=notifier
            )
        assert len(notifier.events) == 1
        event = notifier.events[0]
        assert event.severity == "error"
        assert event.pr_number == 1288
        assert CHECKOUT_BUSY in event.message
        assert event.metadata is not None
        assert event.metadata["stage"] == STAGE_PRE_LAUNCH

    def test_completion_is_notified(self, tmp_path: Path) -> None:
        notifier = _RecordingNotifier()
        with SqliteRunStore(str(tmp_path / "d.db")) as store:
            record_review_outcome(
                ReviewOutcome(
                    pr_number=1288,
                    status=STATUS_COMPLETED,
                    stage=STAGE_RUNNER,
                    classification="completed",
                ),
                store=store,
                config=_config(),
                notifier=notifier,
            )
        assert notifier.events[0].severity == "info"
        assert "completed" in notifier.events[0].message

    def test_foreign_publication_move_explains_that_review_was_completed(
        self, tmp_path: Path
    ) -> None:
        notifier = _RecordingNotifier()
        outcome = ReviewOutcome(
            pr_number=1288,
            status=STATUS_FAILED,
            stage="publication",
            classification="publication_head_moved",
            detail="branch moved (abc123 -> def456)",
        )
        with SqliteRunStore(str(tmp_path / "d.db")) as store:
            record_review_outcome(
                outcome, store=store, config=_config(), notifier=notifier
            )
        assert notifier.events[0].severity == "error"
        assert "completed but was not published because the branch moved" in (
            notifier.events[0].message
        )
        assert "failed at publication" not in notifier.events[0].message

    def test_broken_notifier_never_costs_the_record(self, tmp_path: Path) -> None:
        """State first, best-effort I/O second."""
        with SqliteRunStore(str(tmp_path / "d.db")) as store:
            record_review_outcome(
                _failure(), store=store, config=_config(), notifier=_BrokenNotifier()
            )
            assert latest_review_outcome(store, 1288) is not None


class TestDispatchReviewRecordsOutcomes:
    """dispatch_review persists + notifies on every terminal path."""

    @patch("kew.review.materialize_review_checkout")
    @patch("kew.review.detect_base_branch", return_value="main")
    @patch("kew.review.fetch_review_comments")
    @patch("kew.review._get_pr_metadata")
    @patch("kew.review._check_github_connectivity", return_value=True)
    def test_pre_launch_checkout_failure_is_persisted_and_notified(
        self,
        _conn: MagicMock,
        meta: MagicMock,
        fetch: MagicMock,
        _base: MagicMock,
        checkout: MagicMock,
        tmp_path: Path,
    ) -> None:
        from kew.review import ReviewComment

        meta.return_value = {
            "number": 1288,
            "headRefName": "agent/issue-42",
            "headRefOid": "c" * 40,
            "body": "Closes #42",
            "title": "Fix widget",
        }
        fetch.return_value = [
            ReviewComment(1, "r", "fix", "src/a.py", 1, "COMMENTED", None, False)
        ]
        checkout.side_effect = ReviewCheckoutError(
            CHECKOUT_BUSY, "review checkout /wt/review/pr-1288 is claimed"
        )
        notifier = _RecordingNotifier()

        db = str(tmp_path / "d.db")
        with (
            SqliteRunStore(db) as store,
            patch("kew.review_outcome.build_notifier", return_value=notifier),
        ):
            exit_code = dispatch_review(
                pr_number=1288, config=_config(), store=store, force_budget=True
            )

        assert getattr(exit_code, "kind", None) == "not_run"
        assert exit_code.exit_code == 1
        with SqliteRunStore(db) as reopened:
            outcome = latest_review_outcome(reopened, 1288)
        assert outcome is not None
        assert outcome.status == STATUS_FAILED
        assert outcome.stage == STAGE_PRE_LAUNCH
        assert outcome.classification == CHECKOUT_BUSY
        assert outcome.branch == "agent/issue-42"
        assert notifier.events and notifier.events[0].severity == "error"

    @patch("kew.review.discard_checkout")
    @patch(
        "kew.review._canonical_review_publication",
        return_value=("d" * 40, False),
    )
    @patch("kew.review.run_tracked_agent")
    @patch("kew.review.resolve_runner")
    @patch("kew.review.materialize_review_checkout")
    @patch("kew.review.detect_base_branch", return_value="main")
    @patch("kew.review.fetch_review_comments")
    @patch("kew.review._get_pr_metadata")
    @patch("kew.review._check_github_connectivity", return_value=True)
    def test_completion_is_persisted_and_the_claim_released(
        self,
        _conn: MagicMock,
        meta: MagicMock,
        fetch: MagicMock,
        _base: MagicMock,
        checkout: MagicMock,
        runner: MagicMock,
        tracked: MagicMock,
        _publication: MagicMock,
        _cleanup: MagicMock,
        tmp_path: Path,
    ) -> None:
        from kew.review import ReviewComment
        from kew.review_checkout import ReviewCheckout
        from kew.tracking import DispatchResult

        meta.return_value = {
            "number": 1288,
            "headRefName": "agent/issue-42",
            "body": "Closes #42",
            "title": "Fix widget",
        }
        fetch.return_value = [
            ReviewComment(1, "r", "fix", "src/a.py", 1, "COMMENTED", None, False)
        ]
        materialized = ReviewCheckout(
            path=tmp_path / "review" / "pr-1288",
            head_sha="d" * 40,
            branch="agent/issue-42",
            detached=False,
            impl_worktree=None,
            conflict=None,
            claim=None,
        )
        checkout.return_value = materialized
        runner.return_value = ResolvedRoute(ClaudeCodeRunner(), "claude-opus-5")
        tracked.return_value = DispatchResult(
            exit_code=0, issue_num=1288, log_file=Path("/tmp/log")
        )

        db = str(tmp_path / "d.db")
        with (
            SqliteRunStore(db) as store,
            patch(
                "kew.review_outcome.build_notifier",
                return_value=_RecordingNotifier(),
            ),
        ):
            exit_code = dispatch_review(
                pr_number=1288, config=_config(), store=store, force_budget=True
            )

        assert getattr(exit_code, "kind", None) == "executed"
        assert exit_code.exit_code == 0
        with SqliteRunStore(db) as reopened:
            outcome = latest_review_outcome(reopened, 1288)
        assert outcome is not None
        assert outcome.ok
        assert outcome.head_sha == "d" * 40

    def test_publication_mode_refusal_releases_claim_and_reservation(
        self, tmp_path: Path
    ) -> None:
        from kew.remediation_containment import RemediationContainment
        from kew.review import ReviewComment
        from kew.review_checkout import ReviewCheckout

        config = _config()
        config.publication.publish = "checkout"
        claim = MagicMock()
        checkout = ReviewCheckout(
            path=tmp_path / "review" / "pr-1288",
            head_sha="d" * 40,
            branch="agent/issue-42",
            detached=False,
            impl_worktree=None,
            conflict=None,
            claim=claim,
        )
        db = str(tmp_path / "d.db")
        with (
            patch("kew.review._check_github_connectivity", return_value=True),
            patch(
                "kew.review._get_pr_metadata",
                return_value={
                    "number": 1288,
                    "headRefName": "agent/issue-42",
                    "headRefOid": "d" * 40,
                    "baseRefName": "main",
                    "body": "Closes #42",
                    "title": "Fix widget",
                },
            ),
            patch(
                "kew.review.fetch_review_comments",
                return_value=[
                    ReviewComment(
                        1,
                        "r",
                        "fix",
                        "src/a.py",
                        1,
                        "COMMENTED",
                        None,
                        False,
                    )
                ],
            ),
            patch("kew.review.materialize_review_checkout", return_value=checkout),
            patch(
                "kew.review.resolve_runner",
                return_value=ResolvedRoute(ClaudeCodeRunner(), "claude-opus-5"),
            ),
            patch(
                "kew.review.resolve_remediation_containment",
                return_value=RemediationContainment(
                    posture="uncontained_flow", reason="test"
                ),
            ),
            patch("kew.review.attest_flow_posture"),
            patch("kew.review.run_tracked_agent") as launch,
            SqliteRunStore(db) as store,
        ):
            exit_code = dispatch_review(
                pr_number=1288,
                config=config,
                store=store,
                force_budget=True,
                item_id="SFD-230",
            )
            reservations = store.list_epic_reservations("review")
            runs = store.list_by_item("SFD-230")

        assert getattr(exit_code, "kind", None) == "not_run"
        assert exit_code.exit_code == 1
        launch.assert_not_called()
        claim.close.assert_called_once_with()
        assert reservations == []
        assert runs == []
        with SqliteRunStore(db) as reopened:
            outcome = latest_review_outcome(reopened, 1288)
        assert outcome is not None
        assert outcome.status == STATUS_FAILED
        assert outcome.stage == STAGE_PRE_LAUNCH
        assert outcome.classification == "publication_configuration_refused"

    @patch("kew.review.discard_checkout")
    @patch(
        "kew.review._canonical_review_publication",
        return_value=("f" * 40, True),
    )
    @patch("kew.review.run_tracked_agent")
    @patch("kew.review.resolve_runner")
    @patch("kew.review.materialize_review_checkout")
    @patch("kew.review.detect_base_branch", return_value="main")
    @patch("kew.review.fetch_review_comments")
    @patch("kew.review._get_pr_metadata")
    @patch("kew.review._check_github_connectivity", return_value=True)
    def test_self_pushed_review_is_completed_with_the_pushed_sha(
        self,
        _conn: MagicMock,
        meta: MagicMock,
        fetch: MagicMock,
        _base: MagicMock,
        checkout: MagicMock,
        runner: MagicMock,
        tracked: MagicMock,
        _publication: MagicMock,
        _cleanup: MagicMock,
        tmp_path: Path,
    ) -> None:
        from kew.review import ReviewComment
        from kew.review_checkout import ReviewCheckout
        from kew.tracking import DispatchResult

        meta.return_value = {
            "number": 1288,
            "headRefName": "agent/issue-42",
            "body": "Closes #42",
            "title": "Fix widget",
        }
        fetch.return_value = [
            ReviewComment(1, "r", "fix", "src/a.py", 1, "COMMENTED", None, False)
        ]
        checkout.return_value = ReviewCheckout(
            path=tmp_path / "review" / "pr-1288",
            head_sha="d" * 40,
            branch="agent/issue-42",
            detached=False,
            impl_worktree=None,
            conflict=None,
            claim=None,
        )
        runner.return_value = ResolvedRoute(CodexRunner(), "gpt-5.6-sol")
        tracked.return_value = DispatchResult(
            exit_code=0, issue_num=1288, log_file=Path("/tmp/log")
        )

        db = str(tmp_path / "d.db")
        notifier = _RecordingNotifier()
        from kew.publication_canonical import CanonicalPublication

        republished = CanonicalPublication(
            commit_sha="e" * 40,
            tree_sha="t" * 40,
            parent_shas=("d" * 40,),
            changed_paths=(),
            flags=(),
        )
        with (
            SqliteRunStore(db) as store,
            patch("kew.review_outcome.build_notifier", return_value=notifier),
            # The agent pushed f...; the host republishes that tree as its own
            # commit e... on the reviewed head (review round 4).
            patch("kew.review.republish_agent_head", return_value=republished),
        ):
            exit_code = dispatch_review(
                pr_number=1288, config=_config(), store=store, force_budget=True
            )

        assert getattr(exit_code, "kind", None) == "executed"
        assert exit_code.exit_code == 0
        with SqliteRunStore(db) as reopened:
            outcome = latest_review_outcome(reopened, 1288)
        assert outcome is not None
        assert outcome.ok
        assert outcome.stage == "publication"
        assert outcome.classification == "already_published"
        assert outcome.head_sha == "f" * 40
        assert notifier.events[0].severity == "info"

    @patch("kew.review.discard_checkout")
    @patch("kew.review.run_tracked_agent")
    @patch("kew.review.resolve_runner")
    @patch("kew.review.materialize_review_checkout")
    @patch("kew.review.detect_base_branch", return_value="main")
    @patch("kew.review.fetch_review_comments")
    @patch("kew.review._get_pr_metadata")
    @patch("kew.review._check_github_connectivity", return_value=True)
    def test_runner_failure_is_persisted_with_its_exit_code(
        self,
        _conn: MagicMock,
        meta: MagicMock,
        fetch: MagicMock,
        _base: MagicMock,
        checkout: MagicMock,
        runner: MagicMock,
        tracked: MagicMock,
        _cleanup: MagicMock,
        tmp_path: Path,
    ) -> None:
        from kew.review import ReviewComment
        from kew.review_checkout import ReviewCheckout
        from kew.tracking import DispatchResult

        meta.return_value = {
            "number": 1288,
            "headRefName": "agent/issue-42",
            "body": "Closes #42",
            "title": "Fix widget",
        }
        fetch.return_value = [
            ReviewComment(1, "r", "fix", "src/a.py", 1, "COMMENTED", None, False)
        ]
        checkout.return_value = ReviewCheckout(
            path=tmp_path / "review" / "pr-1288",
            head_sha="e" * 40,
            branch="agent/issue-42",
            detached=False,
            impl_worktree=None,
            conflict=None,
            claim=None,
        )
        runner.return_value = ResolvedRoute(ClaudeCodeRunner(), "claude-opus-5")
        tracked.return_value = DispatchResult(
            exit_code=3, issue_num=1288, log_file=Path("/tmp/log"), launched=True
        )

        db = str(tmp_path / "d.db")
        with (
            SqliteRunStore(db) as store,
            patch(
                "kew.review_outcome.build_notifier",
                return_value=_RecordingNotifier(),
            ),
        ):
            exit_code = dispatch_review(
                pr_number=1288, config=_config(), store=store, force_budget=True
            )

        assert getattr(exit_code, "kind", None) == "executed"
        assert exit_code.exit_code == 3
        with SqliteRunStore(db) as reopened:
            outcome = latest_review_outcome(reopened, 1288)
        assert outcome is not None
        assert outcome.status == STATUS_FAILED
        assert outcome.stage == STAGE_RUNNER
        assert outcome.classification == "runner_failed"
        assert outcome.exit_code == 3


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_canonical_review_binds_empty_local_commit_to_remote_head(
    tmp_path: Path,
) -> None:
    """An unpushed empty commit is not an already-published review."""
    from kew.remediation_containment import RemediationContainment
    from kew.review import ReviewComment
    from kew.review_checkout import ReviewCheckout
    from kew.tracking import DispatchResult

    preflight_tools()
    origin = tmp_path / "origin.git"
    checkout = tmp_path / "review"
    _git("init", "--bare", "-b", "main", str(origin), cwd=tmp_path)
    _git("clone", str(origin), str(checkout), cwd=tmp_path)
    _git("config", "user.name", "review test", cwd=checkout)
    _git("config", "user.email", "review@example.invalid", cwd=checkout)
    (checkout / "tracked.txt").write_text("base\n")
    _git("add", "tracked.txt", cwd=checkout)
    _git("commit", "-m", "base", cwd=checkout)
    _git("checkout", "-b", "agent/issue-42", cwd=checkout)
    _git("push", "-u", "origin", "agent/issue-42", cwd=checkout)
    expected_head = _git("rev-parse", "HEAD", cwd=checkout)

    materialized = ReviewCheckout(
        path=checkout,
        head_sha=expected_head,
        branch="agent/issue-42",
        detached=False,
        impl_worktree=None,
        conflict=None,
        claim=None,
    )

    def create_unpushed_empty_commit(**_kwargs: object) -> DispatchResult:
        _git("commit", "--allow-empty", "-m", "empty review", cwd=checkout)
        return DispatchResult(exit_code=0, issue_num=1288, log_file=tmp_path / "run")

    config = _config()
    config.project.repo = str(origin)
    db = str(tmp_path / "dispatch.db")
    with (
        patch("kew.review._check_github_connectivity", return_value=True),
        patch(
            "kew.review._get_pr_metadata",
            return_value={
                "number": 1288,
                "headRefName": "agent/issue-42",
                "body": "Closes #42",
                "title": "Fix widget",
            },
        ),
        patch(
            "kew.review.fetch_review_comments",
            return_value=[
                ReviewComment(1, "r", "fix", "tracked.txt", 1, "COMMENTED", None, False)
            ],
        ),
        patch("kew.review.detect_base_branch", return_value="main"),
        patch("kew.review.materialize_review_checkout", return_value=materialized),
        patch(
            "kew.review.resolve_runner",
            return_value=ResolvedRoute(ClaudeCodeRunner(), "claude-opus-5"),
        ),
        patch(
            "kew.review.resolve_remediation_containment",
            return_value=RemediationContainment(
                posture="uncontained_flow", reason="test"
            ),
        ),
        patch("kew.review.run_tracked_agent", side_effect=create_unpushed_empty_commit),
        patch("kew.review.attest_flow_posture"),
        patch("kew.review.discard_checkout"),
        SqliteRunStore(db) as store,
        record_git_calls() as calls,
    ):
        outcome = dispatch_review(
            pr_number=1288, config=config, store=store, force_budget=True
        )
        assert getattr(outcome, "kind", None) == "executed"
        assert outcome.exit_code == 0

    local_empty = _git("rev-parse", "HEAD", cwd=checkout)
    assert local_empty != expected_head
    assert _git("rev-parse", "agent/issue-42", cwd=origin) == expected_head
    offenders = [
        call
        for call in calls
        if call.is_git and call.repository().resolves_inside(checkout)
    ]
    assert offenders == []
    with SqliteRunStore(db) as reopened:
        outcome = latest_review_outcome(reopened, 1288)
    assert outcome is not None
    assert outcome.classification == "completed"
    assert outcome.head_sha == expected_head


class _PullRequestForge:
    """GitHub for PR #1288: open on ``agent/issue-42`` with one review asking for
    a fix. Any other ``gh`` argv answers as a missing repository does."""

    def __call__(self, cmd: list[str]) -> subprocess.CompletedProcess[str]:
        args = cmd[1:]
        out: str | None = None
        if args[:2] == ["api", "rate_limit"]:
            out = "{}"
        elif args[:2] == ["pr", "view"]:
            out = json.dumps(
                {
                    "number": 1288,
                    "headRefName": "agent/issue-42",
                    "body": "Closes #42",
                    "title": "Fix widget",
                }
            )
        elif args[:1] == ["api"] and args[1].endswith("/reviews"):
            review = {"id": 1, "body": "fix tracked.txt", "user": {"login": "r"}}
            out = json.dumps([[review]])
        elif args[:1] == ["api"] and args[1].endswith("/comments"):
            out = json.dumps([[]])
        if out is None:
            return subprocess.CompletedProcess(cmd, 1, "", "gh: Not Found (HTTP 404)")
        return subprocess.CompletedProcess(cmd, 0, out, "")


@pytest.mark.parametrize("agent_edits", [False, True], ids=["idle", "edits"])
def test_a_completed_outcome_records_the_integrated_and_published_trees(
    tmp_path: Path, agent_edits: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """kew merged a moved main into the checkout before the agent ran, so the
    published commit moves whether or not the agent did anything. The outcome
    records the tree that merge left beside the tree it published: equal when
    the agent changed nothing, different when it edited (#3276)."""
    from kew.remediation_containment import RemediationContainment
    from kew.review_checkout import ReviewCheckout
    from kew.tracking import DispatchResult

    preflight_tools()
    origin = tmp_path / "origin.git"
    checkout = tmp_path / "review"
    _git("init", "--bare", "-b", "main", str(origin), cwd=tmp_path)
    _git("clone", str(origin), str(checkout), cwd=tmp_path)
    _git("config", "user.name", "review test", cwd=checkout)
    _git("config", "user.email", "review@example.invalid", cwd=checkout)
    (checkout / "tracked.txt").write_text("base\n")
    _git("add", "tracked.txt", cwd=checkout)
    _git("commit", "-m", "base", cwd=checkout)
    _git("push", "origin", "main", cwd=checkout)
    _git("checkout", "-b", "agent/issue-42", cwd=checkout)
    (checkout / "pr.txt").write_text("pr work\n")
    _git("add", "pr.txt", cwd=checkout)
    _git("commit", "-m", "pr work", cwd=checkout)
    _git("push", "-u", "origin", "agent/issue-42", cwd=checkout)
    cut_head = _git("rev-parse", "HEAD", cwd=checkout)
    # main moves on, and kew's base integration merges it in before launch.
    _git("checkout", "main", cwd=checkout)
    (checkout / "base.txt").write_text("newer base\n")
    _git("add", "base.txt", cwd=checkout)
    _git("commit", "-m", "base moved", cwd=checkout)
    _git("push", "origin", "main", cwd=checkout)
    _git("checkout", "agent/issue-42", cwd=checkout)
    _git("merge", "--no-edit", "main", cwd=checkout)
    integrated_tree = _git("rev-parse", "HEAD^{tree}", cwd=checkout)

    materialized = ReviewCheckout(
        path=checkout,
        head_sha=cut_head,
        branch="agent/issue-42",
        detached=False,
        impl_worktree=None,
        conflict=None,
        integrated_tree=integrated_tree,
        claim=None,
    )

    def agent(**_kwargs: object) -> DispatchResult:
        if agent_edits:
            (checkout / "tracked.txt").write_text("fixed\n")
            _git("commit", "-am", "fix the finding", cwd=checkout)
        return DispatchResult(exit_code=0, issue_num=1288, log_file=tmp_path / "run")

    config = _config()
    config.project.repo = str(origin)
    db = str(tmp_path / "dispatch.db")
    monkeypatch.setattr(
        subprocess_utils,
        "subprocess",
        GhProcessBoundary(_PullRequestForge(), tool_paths=subprocess_utils.TOOL_PATHS),
    )
    with (
        patch("kew.review.detect_base_branch", return_value="main"),
        patch("kew.review.materialize_review_checkout", return_value=materialized),
        patch(
            "kew.review.resolve_runner",
            return_value=ResolvedRoute(ClaudeCodeRunner(), "claude-opus-5"),
        ),
        patch(
            "kew.review.resolve_remediation_containment",
            return_value=RemediationContainment(
                posture="uncontained_flow", reason="test"
            ),
        ),
        patch("kew.review.run_tracked_agent", side_effect=agent),
        patch("kew.review.attest_flow_posture"),
        patch("kew.review.discard_checkout"),
        SqliteRunStore(db) as store,
    ):
        result = dispatch_review(
            pr_number=1288, config=config, store=store, force_budget=True
        )
        assert getattr(result, "kind", None) == "executed"
        assert result.exit_code == 0

    with SqliteRunStore(db) as reopened:
        outcome = latest_review_outcome(reopened, 1288)
    assert outcome is not None
    assert outcome.status == STATUS_COMPLETED
    assert outcome.cut_head_sha == cut_head
    assert outcome.head_sha != cut_head, "the base merge alone moves the commit"
    assert outcome.head_sha == _git("rev-parse", "agent/issue-42", cwd=origin)
    assert outcome.integrated_tree == integrated_tree
    published_tree = _git("rev-parse", f"{outcome.head_sha}^{{tree}}", cwd=origin)
    assert outcome.published_tree == published_tree
    assert (outcome.published_tree != integrated_tree) is agent_edits
