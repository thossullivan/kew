"""The reviewer copy is found from the project, never from the cwd (#3108).

``kew prompts``, the API ``kew serve`` builds and a drive tick's review launch
all find the deployed reviewer copy from ``policy.repo_root(project_root())``.
Each test runs them with the cwd in an unrelated repository (repoB, which
deploys its own copy) and ``KEW_PROJECT`` at ``repoA/svc``: all three must
report and use repoA's top copy and its sha, so a preview of the merge gate
shows the copy the gate runs whatever directory kew is started from.

The other launches of the reviewer (``kew review --governed`` and the drive's
detached ``kew epic _review-node`` child) hand it the same copy, and the merge
pass (``kew merge queue run`` and the drive's) works in the same repository.
``kew serve`` opens the ledger the drive registered for the project.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from support import driver_fakes as _fx
from support.driver_fakes import EPIC, REPO, FakeFactsReader, FakeSink, FakeSource
from support.driver_fakes import _node as _driver_node
from support.kew_published import published_by_kew
from support.review_stream import stream_verdict
from support.reviewer_artifact import commit_reviewer_artifact, git_repo
from typer.testing import CliRunner

from kew.cli import app as cli_app
from kew.graph.driver.facts import NodeFacts
from kew.graph.driver.tick import advance
from kew.review_contract import CollectedSidecar
from kew.review_dispatch import ReviewerLaunchContext
from kew.run_store import SqliteRunStore
from kew.subprocess_utils import TOOL_PATHS

_isolate_tool_paths = _fx._isolate_tool_paths

A_TOP = "# Reviewer rubric at repoA's top\n\nThe rubric in force.\n"
A_SVC = "# Reviewer rubric under repoA/svc\n\nA copy nothing reads.\n"
B_TOP = "# Reviewer rubric of the unrelated repoB\n\nThe cwd's copy.\n"
NOW = datetime(2026, 10, 29, 12, 0, 0, tzinfo=UTC)
ITEM = "1"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.fixture(autouse=True)
def _tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The real git; a ``gh`` that answers nothing, so no tick reaches GitHub."""
    git = shutil.which("git")
    assert git is not None, "git required for the base-ref read"
    monkeypatch.setitem(TOOL_PATHS, "git", git)
    gh = tmp_path / "bin" / "gh"
    gh.parent.mkdir()
    gh.write_text("#!/bin/sh\nexit 1\n")
    gh.chmod(0o755)
    monkeypatch.setitem(TOOL_PATHS, "gh", str(gh))
    monkeypatch.setattr("kew.graph.driver.tick.preflight_tools", lambda: None)


def _two_repos(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, str]:
    """repoA deploying A_TOP at its top and A_SVC under ``svc/`` (a kew
    project), repoB deploying B_TOP; the cwd in repoB and ``KEW_PROJECT`` at
    ``repoA/svc``. Returns ``(repoA, svc, repoA's main)``."""
    repo_a = git_repo(tmp_path / "repoA")
    commit_reviewer_artifact(repo_a, A_TOP)
    main = commit_reviewer_artifact(repo_a, A_SVC, under="svc")
    svc = repo_a / "svc"
    (svc / "logs").mkdir(parents=True)
    (svc / "kew.toml").write_text('[logging]\ndir = "logs"\n')
    repo_b = git_repo(tmp_path / "repoB")
    commit_reviewer_artifact(repo_b, B_TOP)
    (repo_b / "work").mkdir()
    monkeypatch.chdir(repo_b / "work")
    monkeypatch.setenv("KEW_PROJECT", str(svc))
    return repo_a, svc, main


def _shown() -> dict[str, Any]:
    result = CliRunner().invoke(
        cli_app, ["prompts", "show", "adversarial_review", "--json"]
    )
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def _served(inbox_client: Any) -> dict[str, Any]:
    """``GET /api/prompts/adversarial_review`` from the app ``kew serve`` builds."""
    served: list = []
    with (
        patch("kew.cli.preflight_tools"),
        patch("uvicorn.run", side_effect=lambda app, **_: served.append(app)),
    ):
        result = CliRunner().invoke(cli_app, ["serve"])
    assert result.exit_code == 0, result.output
    client = inbox_client(served[0], loopback_peer=True)
    body = client.get("/api/prompts/adversarial_review")
    assert body.status_code == 200, body.text
    return body.json()


def _capturing_launcher() -> tuple[
    Callable[[ReviewerLaunchContext], CollectedSidecar], list[ReviewerLaunchContext]
]:
    """The reviewer container, faked: it records what it is handed and answers
    READY. Returns ``(launcher, launched)``."""
    launched: list[ReviewerLaunchContext] = []

    def launcher(ctx: ReviewerLaunchContext) -> CollectedSidecar:
        launched.append(ctx)
        return stream_verdict(
            ctx,
            {
                "verdict": "READY",
                "findings": [],
                "acceptance_evidence": [
                    {"command": "pytest -q", "output_digest": "sha256:x"}
                ],
            },
        )

    return launcher, launched


def _launched(tmp_path: Path, head: str) -> ReviewerLaunchContext:
    """What one production ``advance()`` tick hands the review launch for an
    open PR at *head*, the reviewer container faked."""
    launcher, launched = _capturing_launcher()
    facts = FakeFactsReader(
        issue_facts_by_num={
            int(ITEM): NodeFacts(
                issue_num=int(ITEM),
                state="OPEN",
                state_reason=None,
                labels=frozenset(),
                pr_number=7,
                pr_url="https://github.com/x/y/pull/7",
                review_decision=None,
                merge_state_status=None,
                head_oid=head,
                pr_pushed_at=None,
            )
        },
        pr_facts_by_num={7: {"number": 7, "headRefOid": head}},
    )
    config = _fx._config()
    config.review.adversarial = True
    config.review.blocking = False
    config.review.max_rounds = 2
    config.review.publish_status = False
    config.review.publish_comment = False
    config.logging.dir = str(tmp_path / "logs")
    store = SqliteRunStore(str(tmp_path / "run.db"), project_root=tmp_path)
    try:
        advance(
            EPIC,
            config=config,
            store=store,
            deps=FakeSource(nodes=[_driver_node(ITEM)], edges=[]),
            facts=facts,
            dispatch_sink=FakeSink(),
            max_parallel=3,
            now=NOW,
            lock_path=tmp_path / "epic.lock",
            approvers=["human"],
            repo=REPO,
            reviewer_launcher=launcher,
        )
    finally:
        store.close()
    (ctx,) = launched
    return ctx


def _assert_all_name_a_top(
    shown: dict[str, Any], served: dict[str, Any], ctx: ReviewerLaunchContext
) -> None:
    assert shown["source"]["kind"] == "deployed-artifact"
    assert shown["source"]["sha256"] == _sha(A_TOP)
    assert shown["base_prompt"].startswith(A_TOP)
    assert served["source"]["sha256"] == _sha(A_TOP)
    assert served["preview"].startswith(A_TOP)
    assert ctx.prompt.startswith(A_TOP)
    assert ctx.prompt_artifact_sha == _sha(A_TOP)
    for text in (shown["base_prompt"], served["preview"], ctx.prompt):
        assert "The cwd's copy." not in text
        assert "A copy nothing reads." not in text


def test_prompts_serve_and_a_drive_tick_read_the_projects_copy_from_an_unrelated_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, inbox_client: Any
) -> None:
    _repo_a, _svc, main = _two_repos(tmp_path, monkeypatch)

    _assert_all_name_a_top(_shown(), _served(inbox_client), _launched(tmp_path, main))


def test_an_empty_git_dir_under_the_project_does_not_split_the_three_surfaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, inbox_client: Any
) -> None:
    """A leftover empty ``svc/.git/`` is no repository to git, so none of the
    three surfaces stops there and reads ``svc/.claude/kew/``."""
    _repo_a, svc, main = _two_repos(tmp_path, monkeypatch)
    (svc / ".git").mkdir()

    _assert_all_name_a_top(_shown(), _served(inbox_client), _launched(tmp_path, main))


PR = 7
BRANCH = f"agent/issue-{ITEM}"


def _review_project(svc: Path) -> None:
    """``svc``'s kew.toml with the governed review on and nothing published."""
    (svc / "kew.toml").write_text(
        f'[project]\nrepo = "{REPO}"\n\n[logging]\ndir = "logs"\n\n'
        "[review]\nadversarial = true\nblocking = false\nmax_rounds = 2\n"
        "publish_status = false\npublish_comment = false\n"
    )


def _answering_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, head: str) -> None:
    """A ``gh`` that is logged in, shows PR 7 open at *head* on a kew branch and
    gives its item no labels; anything else fails."""
    pr = {
        "number": PR,
        "state": "OPEN",
        "headRefName": BRANCH,
        "headRefOid": head,
        "baseRefName": "main",
        "mergeStateStatus": "CLEAN",
        "reviewDecision": None,
        "url": f"https://github.com/{REPO}/pull/{PR}",
    }
    answers = tmp_path / "gh-answers"
    answers.mkdir()
    (answers / "pr.json").write_text(json.dumps(pr))
    gh = tmp_path / "bin" / "gh-answering"
    gh.write_text(
        "#!/bin/sh\n"
        'case "$1 $2" in\n'
        '  "auth status") exit 0 ;;\n'
        f'  "pr view") cat "{answers / "pr.json"}"; exit 0 ;;\n'
        '  "issue view") echo \'{"labels": []}\'; exit 0 ;;\n'
        "esac\n"
        "exit 1\n"
    )
    gh.chmod(0o755)
    monkeypatch.setitem(TOOL_PATHS, "gh", str(gh))


def test_a_governed_review_reads_the_projects_copy_from_an_unrelated_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``kew review 7 --governed`` hands the reviewer repoA's top copy, the one
    ``kew prompts show`` previews, not the copy the cwd's repository deploys."""
    _repo_a, svc, main = _two_repos(tmp_path, monkeypatch)
    _review_project(svc)
    _answering_gh(tmp_path, monkeypatch, main)
    with SqliteRunStore(str(svc / "logs" / "dispatch.db")) as store:
        published_by_kew(store, item=ITEM, branch=BRANCH, head=main)
    launcher, launched = _capturing_launcher()

    with (
        patch("kew.cli.preflight_tools"),
        patch("kew.cli.sandbox_reviewer_launcher", return_value=launcher),
    ):
        result = CliRunner().invoke(cli_app, ["review", str(PR), "--governed"])

    assert result.exit_code == 0, result.output
    (ctx,) = launched
    assert ctx.prompt.startswith(A_TOP)
    assert ctx.prompt_artifact_sha == _sha(A_TOP) == _shown()["source"]["sha256"]
    assert "The cwd's copy." not in ctx.prompt


def _git(cwd: Path, *args: str) -> str:
    git = TOOL_PATHS["git"]
    done = subprocess.run(  # noqa: S603 — test fixture over tmp repositories
        [git, "-C", str(cwd), *args], capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


def _queue_on_a_red_main(repo_a: Path, svc: Path, main: str) -> None:
    """repoA with an ``origin`` carrying its main, and the project's ledger
    holding the merge queue on that main as red, so a pass fetches main and
    stops there: no verify, no forge."""
    origin = repo_a.parent / "originA.git"
    _git(repo_a.parent, "clone", "-q", "--bare", str(repo_a), str(origin))
    _git(repo_a, "remote", "add", "origin", str(origin))
    with SqliteRunStore(str(svc / "logs" / "dispatch.db")) as store:
        store.suspect_merge_queue_main(REPO, "a red main", red_on=main)


def _fetched_base(repo: Path) -> str | None:
    """The main a merge-queue pass fetched into *repo*, if one fetched there."""
    done = subprocess.run(  # noqa: S603 — test fixture over a tmp repository
        [
            TOOL_PATHS["git"],
            "-C",
            str(repo),
            "rev-parse",
            "--verify",
            "-q",
            "refs/kew/merge-queue/base",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return done.stdout.strip() or None


def test_merge_queue_run_works_in_the_projects_repository_from_an_unrelated_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``kew merge queue run`` fetches and builds in repoA, the repository the
    drive's merge verifier and ``kew epic merge`` use, not the cwd's repoB."""
    repo_a, svc, main = _two_repos(tmp_path, monkeypatch)
    _review_project(svc)
    _queue_on_a_red_main(repo_a, svc, main)

    result = CliRunner().invoke(cli_app, ["merge", "queue", "run"])

    assert result.exit_code == 2, result.output
    assert f"main {main[:12]} is red; waiting for it to move" in result.output
    assert _fetched_base(repo_a) == main
    assert _fetched_base(tmp_path / "repoB") is None


def test_the_drives_merge_pass_works_in_the_projects_repository_from_an_unrelated_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The interactive drive's merge pass builds its queue on repoA, as the
    merge queue's own command does. ``[drive.merge]`` stays off here, so the
    standing rule (which shares the same root) is not exercised."""
    from kew.cli.drive import _merge_factory
    from kew.config import load_config

    repo_a, svc, main = _two_repos(tmp_path, monkeypatch)
    _review_project(svc)
    _queue_on_a_red_main(repo_a, svc, main)
    config = load_config()

    with SqliteRunStore(str(svc / "logs" / "dispatch.db")) as store:
        merge_pass = _merge_factory(config, REPO, "epic:")(store)
        assert merge_pass is not None
        merge_pass([])
        assert store.merge_queue_suspect(REPO) == ("a red main", main)

    assert _fetched_base(repo_a) == main
    assert _fetched_base(tmp_path / "repoB") is None


def test_a_detached_review_node_reads_the_projects_copy_from_an_unrelated_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The drive's ``kew epic _review-node`` child, started in repoB, hands the
    reviewer repoA's top copy, the one ``kew prompts show`` previews."""
    from kew.config import load_config
    from kew.graph.driver.overlay import DriverStatus
    from kew.graph.driver.review_detach import DetachedReview, facts_to_json
    from kew.graph.driver.review_stage import drive_review_stage

    repo_a, svc, main = _two_repos(tmp_path, monkeypatch)
    _review_project(svc)
    facts = NodeFacts(
        issue_num=int(ITEM),
        state="OPEN",
        state_reason=None,
        labels=frozenset(),
        pr_number=PR,
        pr_url=f"https://github.com/{REPO}/pull/{PR}",
        review_decision=None,
        merge_state_status=None,
        head_oid=main,
        pr_pushed_at=None,
    )
    spawned: list[DetachedReview] = []
    with SqliteRunStore(str(svc / "logs" / "dispatch.db")) as store:
        # The tick's side: reserve the node and write the child's stage row.
        drive_review_stage(
            {ITEM: DriverStatus.ADVERSARIAL_REVIEW},
            {ITEM: facts},
            epic_ref=EPIC,
            config=load_config(),
            store=store,
            repo=REPO,
            repo_root=repo_a,
            base="main",
            reviewer_launcher=None,
            worker_cap=4,
            now=NOW,
            detacher=spawned.append,
        )
    (review,) = spawned
    launcher, launched = _capturing_launcher()

    with (
        patch("kew.cli.preflight_tools"),
        patch("kew.cli.epic.sandbox_reviewer_launcher", return_value=launcher),
        # The child makes itself a process-group leader; pytest must not.
        patch("kew.process_groups.exclusive_owner_pid", os.getpid),
    ):
        result = CliRunner().invoke(
            cli_app,
            [
                "epic",
                "_review-node",
                EPIC,
                ITEM,
                "--run-id",
                review.run_id,
                "--facts",
                facts_to_json(review.facts),
            ],
        )

    assert result.exit_code == 0, result.output
    (ctx,) = launched
    assert ctx.prompt.startswith(A_TOP)
    assert ctx.prompt_artifact_sha == _sha(A_TOP) == _shown()["source"]["sha256"]
    assert "The cwd's copy." not in ctx.prompt


def test_serve_from_an_unrelated_cwd_opens_the_ledger_the_drive_registered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, inbox_client: Any
) -> None:
    """``kew serve`` started in repoB opens repoA/svc's ledger through the
    registry the drive keeps for it (``canonical_project_root(project_root())``),
    serving the drive's runs, and registers nothing for repoB."""
    from kew.audit import DEFAULT_ANCHORS_PATH
    from kew.ledger_identity import (
        RegistryRecord,
        canonical_project_root,
        open_managed_store,
        registry_paths,
    )
    from kew.run_store import RunRecord

    _repo_a, svc, _main = _two_repos(tmp_path, monkeypatch)
    db_path = svc / "logs" / "dispatch.db"
    root = canonical_project_root(svc)
    # The drive's own open of the project's ledger, and one run on it.
    store = open_managed_store(
        project_root=root, db_path=db_path, anchors_path=root / DEFAULT_ANCHORS_PATH
    )
    try:
        store.save(
            RunRecord(
                run_id="drive-run",
                issue_num=int(ITEM),
                item_id=ITEM,
                branch=BRANCH,
                status="success",
                exit_code=0,
                started_at=NOW,
                finished_at=NOW,
            )
        )
    finally:
        store.close()
    served: list = []

    with (
        patch("kew.cli.preflight_tools"),
        patch("uvicorn.run", side_effect=lambda app, **_: served.append(app)),
    ):
        result = CliRunner().invoke(cli_app, ["serve"])
    assert result.exit_code == 0, result.output
    runs = inbox_client(served[0], loopback_peer=True).get("/api/runs")

    assert runs.status_code == 200, runs.text
    assert [run["run_id"] for run in runs.json()] == ["drive-run"]
    registry = RegistryRecord.from_json(registry_paths(svc).registry.read_text())
    assert (registry.project_root, registry.db_path) == (root, db_path.resolve())
    assert not registry_paths(tmp_path / "repoB").registry.exists()
