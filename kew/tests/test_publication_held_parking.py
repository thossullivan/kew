"""Held sandbox publications persist their pending status before returning."""

from __future__ import annotations

import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from support.host_git_boundary import record_git_calls

from kew import dispatch_core, dispatch_sandbox, subprocess_utils, worktree
from kew.config import DispatchConfig
from kew.publication_canonical import host_commit_identity, publish_checkout
from kew.run_store import RunRecord, SqliteRunStore
from kew.tracking import DispatchResult


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603  # nosec B603, B607 — local fixture setup
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.mark.parametrize("interruption", ["none", "after_save", "mint_failure", "job"])
def test_held_publication_parks_before_host_collection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interruption: str
) -> None:
    git = shutil.which("git")
    assert git is not None
    monkeypatch.setitem(subprocess_utils.TOOL_PATHS, "git", git)
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", "-q", "-b", "main", str(origin))
    seed = tmp_path / "seed"
    _git(tmp_path, "clone", "-q", str(origin), str(seed))
    _git(seed, "config", "user.name", "Test")
    _git(seed, "config", "user.email", "test@example.invalid")
    (seed / "readme.txt").write_text("base\n")
    _git(seed, "add", "readme.txt")
    _git(seed, "commit", "-qm", "base")
    _git(seed, "push", "-q", "origin", "main")
    base = _git(seed, "rev-parse", "HEAD")
    checkout = tmp_path / "checkout"
    _git(tmp_path, "clone", "-q", str(origin), str(checkout))
    worktree.write_checkout_generation(checkout)
    workflow = checkout / ".github" / "workflows" / "check.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("name: check\n")
    monkeypatch.chdir(seed)

    def _publish(*_args, **_kwargs):
        return publish_checkout(
            checkout,
            remote=str(origin),
            branch="agent/issue-7",
            parents=(base,),
            base_sha=base,
            message="held work",
            identity=host_commit_identity(),
            output_guard=None,
            scratch=tmp_path / "publication",
        )

    monkeypatch.setattr(dispatch_core, "_push_and_open_pr", _publish)
    if interruption == "job":
        monkeypatch.setattr(
            dispatch_core,
            "_normalized_publication_job",
            lambda *_args: SimpleNamespace(active=True),
        )
    if interruption == "mint_failure":

        def _cannot_mint(*_args, **_kwargs):
            raise RuntimeError("continuation storage unavailable")

        monkeypatch.setattr(
            dispatch_core, "_mint_publication_continuation", _cannot_mint
        )

    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        store.save(
            RunRecord(
                run_id="held-run",
                issue_num=7,
                item_id="7",
                branch="agent/issue-7",
                status="running",
                exit_code=0,
                started_at=datetime.now(UTC),
            )
        )
        saved_statuses: list[str] = []

        def _finalize(code: int, error_category: str = "", status=None) -> None:
            dispatch_sandbox._finalize_sandbox_run(
                store,
                "held-run",
                exit_code=code,
                error_category=error_category,
                status=status,
            )
            row = store.get("held-run")
            assert row is not None
            saved_statuses.append(row.status)
            if interruption == "after_save":
                raise SystemExit("host stopped after finalization")

        def _complete():
            return dispatch_sandbox._publish_completed_sandbox(
                DispatchResult(
                    exit_code=0,
                    issue_num=7,
                    run_id="held-run",
                    log_file=tmp_path / "run.log",
                ),
                finalize=_finalize,
                checkout_dir=checkout,
                branch="agent/issue-7",
                item_id="7",
                title="held work",
                config=DispatchConfig(),
                store=store,
                run_id="held-run",
                publication_base_sha=base,
                image_id="unused",
                log_file=tmp_path / "run.log",
                issue_num=7,
                epic_ref="alpha",
            )

        with record_git_calls() as calls:
            if interruption == "after_save":
                with pytest.raises(SystemExit, match="host stopped after finalization"):
                    _complete()
            else:
                result = _complete()
                assert result.error_category == "publication_held"
                assert result.subtype == (
                    ".github/workflows/check.yml: "
                    "CI workflow code, which runs with repository secrets"
                )
        git_calls = [call for call in calls if call.is_git]
        assert git_calls
        assert not [
            call.describe()
            for call in git_calls
            if call.repository().resolves_inside(checkout)
        ]
        assert _git(origin, "branch", "--list", "agent/issue-7") == ""
        assert workflow.read_text() == "name: check\n"
        row = store.get("held-run")
        assert row is not None
        expected_status = "success" if interruption == "job" else "awaiting_publication"
        assert saved_statuses == [expected_status]
        assert row.status == expected_status
        if interruption != "job":
            assert row.error_category == "publication_review_required"
        if interruption == "none":
            [handle] = store.list_continuations("alpha")
            assert handle.original_run_id == "held-run"
            assert (
                dispatch_core._recorded_publication_base(
                    store,
                    run_id="held-run",
                    branch="agent/issue-7",
                    checkout=checkout,
                )
                == base
            )
            dispatch_core._mint_publication_continuation(
                store,
                epic_ref="alpha",
                item_id="7",
                branch="agent/issue-7",
                issue_num=7,
                repo=None,
                publish="canonical",
                base=base,
                run_id="held-run",
                worktree_dir=checkout,
            )
            assert len(store.list_continuations("alpha")) == 1
        else:
            assert store.list_continuations("alpha") == []
