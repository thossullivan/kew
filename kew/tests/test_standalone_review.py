"""#2556 — `kew review --governed`: one governed review round on a standalone PR.

The epic review stage's own per-node action runs against a PR no epic drives,
with the item read off the PR's head branch. A PR whose head kew did not
publish is refused before any reviewer runs.
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from support.review_stream import reviewer_stream, stream_lines, stream_verdict
from typer.testing import CliRunner

from kew import standalone_review
from kew.audit import Actor
from kew.cli import app
from kew.config import DispatchConfig, LoggingConfig, ReviewConfig
from kew.pseudo_epics import REVIEW_EPIC_REF
from kew.publication_attest import record_head_attestation
from kew.readiness import (
    Containment,
    RealizedLaunchBoundary,
    compose_realized_profile,
    compose_route_profile,
)
from kew.review_audit import REVIEW_VERDICT_EVENT
from kew.review_dispatch import ReviewerLaunchContext
from kew.run_store import RunRecord, SqliteRunStore
from kew.standalone_review import (
    STANDALONE_REVIEW_EPIC_REF,
    PrUnreadable,
    read_item_labels,
    read_pr,
    run_standalone_review,
)
from kew.subprocess_utils import run

ITEM = "42"
BRANCH = "agent/issue-42"
PR = 7
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _register_git(monkeypatch):
    from kew.subprocess_utils import TOOL_PATHS

    git = shutil.which("git")
    assert git is not None, "git required for review tests"
    monkeypatch.setitem(TOOL_PATHS, "git", git)


@pytest.fixture
def surfaces():
    """The PR-thread and commit-status surfaces the verdict is published to."""
    with (
        patch("kew.graph.driver.review_stage.review_comment") as comment,
        patch("kew.graph.driver.review_stage.review_status") as status,
    ):
        yield comment, status


def _make_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    run(["git", "config", "user.email", "t@t.t"], cwd=repo, check=True)
    run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "code.py").write_text("x = 1\n")
    run(["git", "add", "-A"], cwd=repo, check=True)
    run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)
    head = run(["git", "rev-parse", "HEAD"], cwd=repo, check=True).stdout.strip()
    return repo, head


def _config(tmp_path: Path) -> DispatchConfig:
    return DispatchConfig(
        review=ReviewConfig(adversarial=True, blocking=True, max_rounds=2),
        logging=LoggingConfig(dir=str(tmp_path / "logs")),
    )


def _pr(head: str, *, branch: str = BRANCH, state: str = "OPEN") -> dict[str, Any]:
    return {
        "number": PR,
        "state": state,
        "headRefName": branch,
        "headRefOid": head,
        "baseRefName": "main",
        "mergeStateStatus": "CLEAN",
        "reviewDecision": None,
        "url": f"https://github.com/o/r/pull/{PR}",
    }


def _published_by_kew(store: SqliteRunStore, head: str) -> None:
    """The implementation run on the branch, and the claim binding its head."""
    store.save(
        RunRecord(
            run_id="producer",
            issue_num=int(ITEM),
            item_id=ITEM,
            branch=BRANCH,
            status="success",
            exit_code=0,
            started_at=datetime(2026, 10, 9, 1, tzinfo=UTC),
            finished_at=datetime(2026, 10, 9, 2, tzinfo=UTC),
            attempt_number=1,
            container_name="kew-42",
            runner_name="claude-code",
            model="claude-opus-5",
        )
    )
    store.record_route_profile(
        compose_realized_profile(
            run_id="producer",
            attempt_number=1,
            route=compose_route_profile(
                runner="claude-code", requested_model="claude-opus-5"
            ),
            boundary=RealizedLaunchBoundary(
                containment=Containment.CONTAINED,
                image_ref="kew-agent:latest",
                image_id="sha256:" + "a" * 64,
                auth_path="env:SYNTHETIC_PROVIDER_KEY",
            ),
        )
    )
    record_head_attestation(
        store,
        run_id="producer",
        item_id=ITEM,
        branch=BRANCH,
        commit_sha=head,
        parent_shas=("b" * 40,),
        actor=Actor("human", "owner"),
    )


def _ready_launcher(ctx: ReviewerLaunchContext):
    evidence = {"command": "uv run pytest -q", "output_digest": "sha256:beef"}
    return stream_verdict(
        ctx, {"verdict": "READY", "findings": [], "acceptance_evidence": [evidence]}
    )


def _finding(summary: str) -> dict[str, Any]:
    return {
        "severity": "high",
        "location": "code.py:1",
        "summary": summary,
        "evidence": {"command": "uv run pytest -q", "output_digest": "sha256:cafe"},
    }


def _needs_changes_launcher(ctx: ReviewerLaunchContext):
    findings = [_finding("unchecked path"), _finding("missing guard")]
    return stream_verdict(ctx, {"verdict": "NEEDS_CHANGES", "findings": findings})


def _review(store, config, repo, head, launcher, *, pr=None, remediator=None, **kwargs):
    kwargs.setdefault("labels_reader", lambda _item, _repo: [])
    return run_standalone_review(
        PR,
        config=config,
        store=store,
        repo="o/r",
        repo_root=repo,
        reviewer_launcher=launcher,
        remediator=remediator,
        pr_reader=lambda number, _repo: pr if pr is not None else _pr(head),
        now=NOW,
        **kwargs,
    )


def _verdict_events(store: SqliteRunStore) -> list[Any]:
    return [
        e
        for e in store.list_audit_events(item_id=ITEM)
        if e.event == REVIEW_VERDICT_EVENT
    ]


def test_ready_records_one_round_and_posts_one_verdict(tmp_path: Path, surfaces):
    comment, _status = surfaces
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path)
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _published_by_kew(store, head)

        result = _review(store, config, repo, head, _ready_launcher)

        assert result.exit_code == 0, result.message
        assert "READY" in result.message
        [round_] = store.list_review_rounds(ITEM)
        assert (round_.verdict, round_.head_sha, round_.epic_ref) == (
            "READY",
            head,
            STANDALONE_REVIEW_EPIC_REF,
        )
        [event] = _verdict_events(store)
        assert event.payload["verdict"] == "READY"
        comment.post_verdict_comment.assert_called_once()
        assert comment.post_verdict_comment.call_args.args == ("o/r", PR)

        # A second run on the same head pays for no second reviewer.
        launcher = MagicMock(side_effect=AssertionError("reviewer ran"))
        again = _review(store, config, repo, head, launcher)
        assert again.exit_code == 0
        assert "already stands" in again.message
        assert len(store.list_review_rounds(ITEM)) == 1


def _answered_twice_launcher(ctx: ReviewerLaunchContext):
    """A reviewer whose runner wrote its one verdict twice (#3174)."""
    evidence = {"command": "uv run pytest -q", "output_digest": "sha256:beef"}
    ready = {"verdict": "READY", "findings": [], "acceptance_evidence": [evidence]}
    genuine = reviewer_stream(ready, ctx.head_sha)
    return stream_lines(ctx, [*genuine, genuine[-1]])


def test_a_retried_round_tells_the_operator_to_run_it_again(tmp_path: Path, surfaces):
    """#3186: the drive re-runs a retried round on its next tick; ``kew review
    --governed`` has no next tick, so it says to run it again instead."""
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path)
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _published_by_kew(store, head)

        result = _review(store, config, repo, head, _answered_twice_launcher)

        assert result.exit_code != 0
        assert "2 result events" in result.message
        assert f"run `kew review --governed {PR}` again" in result.message
        assert "next tick" not in result.message
        assert store.list_review_rounds(ITEM) == []


def test_needs_changes_records_round_and_exits_with_finding_count(
    tmp_path: Path, surfaces
):
    comment, _status = surfaces
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path)
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _published_by_kew(store, head)

        result = _review(store, config, repo, head, _needs_changes_launcher)

        assert result.exit_code == 1
        assert "NEEDS_CHANGES with 2 finding(s)" in result.message
        [round_] = store.list_review_rounds(ITEM)
        assert round_.verdict == "NEEDS_CHANGES"
        assert round_.sidecar_path is not None
        comment.post_verdict_comment.assert_called_once()

        # The next run on the unchanged head remediates the standing findings
        # instead of paying for another reviewer.
        remediator = MagicMock(return_value=None)
        launcher = MagicMock(side_effect=AssertionError("reviewer ran"))
        second = _review(store, config, repo, head, launcher, remediator=remediator)
        remediator.assert_called_once()
        assert remediator.call_args.args[0].pr_number == PR
        assert "remediation dispatched" in second.message
        assert len(store.list_review_rounds(ITEM)) == 1


def test_round_cap_holds_under_the_standalone_ref(tmp_path: Path, surfaces):
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path)
    config.review.max_rounds = 1
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _published_by_kew(store, head)
        _review(store, config, repo, head, _needs_changes_launcher)
        with patch("kew.graph.driver.review_stage._remediation_rounds", return_value=1):
            held = _review(store, config, repo, head, None)
        assert held.exit_code == 1
        assert "HELD" in held.message
        [hold] = store.list_epic_holds(STANDALONE_REVIEW_EPIC_REF)
        assert hold.item_id == ITEM

        # A held item is refused before anything runs, naming the release.
        launcher = MagicMock()
        refused = _review(store, config, repo, head, launcher)
        assert refused.exit_code == 2
        assert f"kew epic unhold {STANDALONE_REVIEW_EPIC_REF} {ITEM}" in (
            refused.message
        )
        launcher.assert_not_called()


@pytest.mark.parametrize(
    ("pr_kwargs", "attest", "expected"),
    [
        ({}, False, "no publication attestation"),
        ({"branch": "feature/hand-made"}, True, "not a kew branch"),
        ({"state": "MERGED"}, True, "not open"),
    ],
)
def test_refused_before_any_reviewer_runs(
    tmp_path: Path, surfaces, pr_kwargs, attest, expected
):
    comment, status = surfaces
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path)
    launcher = MagicMock()
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        if attest:
            _published_by_kew(store, head)
        result = _review(store, config, repo, head, launcher, pr=_pr(head, **pr_kwargs))
        assert result.exit_code == 2
        assert expected in result.message
        launcher.assert_not_called()
        assert store.list_review_rounds(ITEM) == []
        # No reviewer run row and no review reservation was ever taken.
        rows = [r.run_id for r in store.list_by_item(ITEM)]
        assert rows == (["producer"] if attest else [])
        assert store.list_epic_reservations(REVIEW_EPIC_REF) == []
        comment.post_verdict_comment.assert_not_called()
        status.publish_pending.assert_not_called()


def test_an_unreadable_pr_is_refused_with_the_reason(tmp_path: Path, surfaces):
    """A PR GitHub cannot give is a refusal that says why, never an empty PR."""
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path)
    launcher = MagicMock()
    unreadable = PrUnreadable("CalledProcessError: HTTP 502")
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _published_by_kew(store, head)
        result = _review(store, config, repo, head, launcher, pr=unreadable)
    assert result.exit_code == 2
    assert "could not read the PR from GitHub (CalledProcessError: HTTP 502)" in (
        result.message
    )
    launcher.assert_not_called()


def test_read_pr_carries_the_gh_error_class_and_first_line(monkeypatch):
    def failing(cmd, **_kwargs):
        raise subprocess.CalledProcessError(
            1, cmd, stderr="HTTP 404: Not Found\nmore detail"
        )

    monkeypatch.setattr(standalone_review, "subprocess_run", failing)
    assert read_pr(PR, "o/r") == PrUnreadable("CalledProcessError: HTTP 404: Not Found")


def test_read_pr_refuses_json_that_is_not_a_pr_object(monkeypatch):
    monkeypatch.setattr(
        standalone_review,
        "subprocess_run",
        lambda cmd, **_kw: subprocess.CompletedProcess(cmd, 0, stdout="[]"),
    )
    assert read_pr(PR, "o/r") == PrUnreadable("gh returned list, not a PR object")


@pytest.mark.parametrize(
    ("branch", "rule"),
    [
        ("feature/hand-made", "does not start with"),
        ("agent/issue-not-an-item", "not an item id"),
    ],
)
def test_a_non_kew_branch_refusal_names_the_rule(
    tmp_path: Path, surfaces, branch, rule
):
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path)
    launcher = MagicMock()
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        result = _review(
            store, config, repo, head, launcher, pr=_pr(head, branch=branch)
        )
    assert result.exit_code == 2
    assert "not a kew branch" in result.message
    assert rule in result.message
    launcher.assert_not_called()


def test_a_head_the_claim_does_not_name_is_refused(tmp_path: Path, surfaces):
    """The claim binds one head; a later push nobody claimed is not kew's."""
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path)
    launcher = MagicMock()
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _published_by_kew(store, head)
        other = "c" * 40
        result = _review(store, config, repo, other, launcher, pr=_pr(other))
        assert result.exit_code == 2
        launcher.assert_not_called()


runner = CliRunner()


def test_cli_governed_refuses_comment_review_flags():
    result = runner.invoke(app, ["review", "--governed", "--dry-run", "7"])
    assert result.exit_code == 2
    assert "--governed takes no" in result.output


@patch("kew.cli._detect_repo")
@patch("kew.cli._refuse_if_fleet_stopped")
@patch("kew.cli._preflight")
@patch("kew.cli.load_config")
def test_cli_governed_runs_the_standalone_review(
    mock_config, _preflight, _fleet, _detect, tmp_path: Path
):
    config = _config(tmp_path)
    config.project.repo = "o/r"
    mock_config.return_value = config
    fake = MagicMock(return_value=MagicMock(exit_code=1, message="NEEDS_CHANGES"))
    with patch("kew.standalone_review.run_standalone_review", fake):
        result = runner.invoke(app, ["review", "--governed", "7"])
    assert result.exit_code == 1
    assert fake.call_args.args == (7,)
    assert fake.call_args.kwargs["repo"] == "o/r"
    # The production launcher, which records the reviewer's run row as an
    # `adversarial_review` run — the kind `kew report` breaks spend out by.
    launcher = fake.call_args.kwargs["reviewer_launcher"]
    assert launcher.__class__.__name__ == "StoreBound"


def test_a_hold_under_any_epic_is_refused_before_any_reviewer_runs(
    tmp_path: Path, surfaces
):
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path)
    launcher = MagicMock()
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _published_by_kew(store, head)
        store.hold_epic_node("epic:x", ITEM, "operator_hold")
        result = _review(store, config, repo, head, launcher)
        assert result.exit_code == 2
        assert f"kew epic unhold epic:x {ITEM}" in result.message
        launcher.assert_not_called()


def _drive_epic(store: SqliteRunStore, epic_ref: str, signal: str) -> None:
    store.begin_epic_tick(epic_ref, NOW.isoformat())
    store.finish_epic_tick(
        epic_ref,
        tick_ts=NOW.isoformat(),
        duration_s=1.0,
        interval_s=None,
        signal=signal,
        tick_id=None,
    )


def _labelled(*labels: str):
    return lambda _item, _repo: list(labels)


def test_an_item_a_running_epic_drives_is_refused_and_names_the_epic(
    tmp_path: Path, surfaces
):
    """#2584: a standalone round between an epic's ticks would record the
    item's rounds under two epic refs."""
    comment, status = surfaces
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path)
    launcher = MagicMock()
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _published_by_kew(store, head)
        _drive_epic(store, "auth", "progressing")
        result = _review(
            store,
            config,
            repo,
            head,
            launcher,
            labels_reader=_labelled("bug", "epic:auth"),
        )
        assert result.exit_code == 2
        assert "driven by epic 'auth'" in result.message
        assert "--force" in result.message
        launcher.assert_not_called()
        assert [r.run_id for r in store.list_by_item(ITEM)] == ["producer"]
        assert store.list_epic_reservations(REVIEW_EPIC_REF) == []
        status.publish_pending.assert_not_called()


def test_force_reviews_an_item_a_running_epic_drives(tmp_path: Path, surfaces):
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path)
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _published_by_kew(store, head)
        _drive_epic(store, "auth", "progressing")
        result = _review(
            store,
            config,
            repo,
            head,
            _ready_launcher,
            labels_reader=_labelled("epic:auth"),
            force=True,
        )
        assert result.exit_code == 0, result.message
        assert "READY" in result.message


@pytest.mark.parametrize(
    ("labels", "driven"),
    [
        (("epic:auth",), None),  # labelled for an epic kew never drove
        (("epic:auth",), "complete"),  # the epic finished
        (("epic-ish", "other:auth"), "progressing"),  # not an epic label
    ],
)
def test_an_epic_label_no_running_epic_owns_is_reviewed(
    tmp_path: Path, surfaces, labels, driven
):
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path)
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _published_by_kew(store, head)
        if driven is not None:
            _drive_epic(store, "auth", driven)
        result = _review(
            store, config, repo, head, _ready_launcher, labels_reader=_labelled(*labels)
        )
        assert result.exit_code == 0, result.message


def test_unreadable_labels_fail_closed(tmp_path: Path, surfaces):
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path)
    launcher = MagicMock()
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _published_by_kew(store, head)
        result = _review(
            store,
            config,
            repo,
            head,
            launcher,
            labels_reader=lambda _i, _r: PrUnreadable("CalledProcessError: HTTP 502"),
        )
    assert result.exit_code == 2
    assert "check epic membership (CalledProcessError: HTTP 502)" in result.message
    launcher.assert_not_called()


def test_read_item_labels_reads_the_issue_labels(monkeypatch):
    calls = []

    def fake(cmd, **_kwargs):
        calls.append(cmd)
        return MagicMock(stdout='{"labels": [{"name": "epic:auth"}, {"name": "x"}]}')

    monkeypatch.setattr(standalone_review, "subprocess_run", fake)
    assert read_item_labels("42", "o/r") == ["epic:auth", "x"]
    assert calls[0][:3] == ["gh", "issue", "view"]
    # Only a GitHub issue carries the label convention; nothing is read for others.
    assert read_item_labels("ENG-7", "o/r") == []
    assert len(calls) == 1


def test_read_item_labels_carries_a_malformed_reply_as_unreadable(monkeypatch):
    monkeypatch.setattr(
        standalone_review, "subprocess_run", lambda *_a, **_k: MagicMock(stdout="[]")
    )
    assert isinstance(read_item_labels("42", "o/r"), PrUnreadable)


def test_a_spent_review_pool_is_refused_before_any_container_starts(
    tmp_path: Path, surfaces
):
    """#2584: the pool is checked before the stage publishes a pending status,
    takes a reservation or materializes a reviewer checkout."""
    comment, status = surfaces
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path)
    config.review.max_review_usd = 5.0
    launcher = MagicMock()
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _published_by_kew(store, head)
        store.save(
            RunRecord(
                run_id="earlier-review",
                issue_num=int(ITEM),
                item_id=ITEM,
                branch=BRANCH,
                status="success",
                exit_code=0,
                started_at=datetime(2026, 10, 9, 3, tzinfo=UTC),
                finished_at=datetime(2026, 10, 9, 4, tzinfo=UTC),
                estimated_cost_usd=6.0,
                run_kind="adversarial_review",
            )
        )
        with patch("kew.review_dispatch.materialize_reviewer_worktree") as materialize:
            result = _review(store, config, repo, head, launcher)
        assert result.exit_code == 2
        assert "review pool $5.00 is spent ($6.00)" in result.message
        assert "[review].max_review_usd" in result.message
        launcher.assert_not_called()
        materialize.assert_not_called()
        status.publish_pending.assert_not_called()
        assert store.list_epic_reservations(REVIEW_EPIC_REF) == []
        assert store.list_all_epic_holds() == []
        rows = sorted(r.run_id for r in store.list_by_item(ITEM))
        assert rows == ["earlier-review", "producer"]


def test_cli_force_needs_governed():
    result = runner.invoke(app, ["review", "--force", "7"])
    assert result.exit_code == 2
    assert "--force only applies with --governed" in result.output


@patch("kew.cli._detect_repo")
@patch("kew.cli._refuse_if_fleet_stopped")
@patch("kew.cli._preflight")
@patch("kew.cli.load_config")
def test_cli_governed_passes_force(
    mock_config, _preflight, _fleet, _detect, tmp_path: Path
):
    config = _config(tmp_path)
    config.project.repo = "o/r"
    mock_config.return_value = config
    fake = MagicMock(return_value=MagicMock(exit_code=0, message="READY"))
    with patch("kew.standalone_review.run_standalone_review", fake):
        result = runner.invoke(app, ["review", "--governed", "--force", "7"])
    assert result.exit_code == 0, result.output
    assert fake.call_args.kwargs["force"] is True
