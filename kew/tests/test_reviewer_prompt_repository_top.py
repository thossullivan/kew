"""Every reviewer surface reads the deployed copy at the top of the repository.

The drive passes every review launch the git toplevel (``policy.repo_root``,
``kew.graph.driver.tick``), because the reviewer checkout is cloned from it;
``kew serve`` serves the toplevel too. So when the kew project sits in a
subdirectory (``svc/``) of its repository, the deployed reviewer copy in force
is ``<top>/.claude/kew/adversarial-reviewer.md`` on the base ref, and
``kew prompts``, the served API and the launch must all name that one copy:
a preview of the merge gate that disagrees with the gate is worse than none.
Each test commits a different copy under ``svc/.claude/kew/`` that none of
them may use (#3087).
"""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from support import driver_fakes as _fx
from support.driver_fakes import EPIC, REPO, FakeFactsReader, FakeSink, FakeSource
from support.driver_fakes import _node as _driver_node
from support.review_stream import stream_verdict
from support.reviewer_artifact import commit_reviewer_artifact, git_repo, lose_blob
from typer.testing import CliRunner

from kew.cli import app as cli_app
from kew.config import DispatchConfig
from kew.graph.driver.facts import NodeFacts
from kew.graph.driver.tick import advance
from kew.review_contract import (
    CollectedSidecar,
    resolve_reviewer_prompt,
    reviewer_prompt_artifact,
)
from kew.review_dispatch import ReviewerLaunchContext
from kew.run_store import SqliteRunStore
from kew.subprocess_utils import TOOL_PATHS

_isolate_tool_paths = _fx._isolate_tool_paths

TOP_COPY = "# Reviewer rubric at the repository top\n\nThe rubric in force.\n"
SUB_COPY = "# Reviewer rubric under svc\n\nA copy nothing reads.\n"
NOW = datetime(2026, 7, 23, 12, 0, 0, tzinfo=UTC)
ITEM = "1"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.fixture(autouse=True)
def _tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The real git; a ``gh`` that answers nothing, so no tick reaches GitHub.

    ``advance()`` runs the tool preflight, which would re-resolve the real
    ``gh`` over the fake, so the tick's preflight is skipped and the
    allowlist set here is the one it runs with."""
    git = shutil.which("git")
    assert git is not None, "git required for the base-ref read"
    monkeypatch.setitem(TOOL_PATHS, "git", git)
    gh = tmp_path / "bin" / "gh"
    gh.parent.mkdir()
    gh.write_text("#!/bin/sh\nexit 1\n")
    gh.chmod(0o755)
    monkeypatch.setitem(TOOL_PATHS, "gh", str(gh))
    monkeypatch.setattr("kew.graph.driver.tick.preflight_tools", lambda: None)


def _svc_project(tmp_path: Path, *, top: str | None) -> tuple[Path, str]:
    """A repository whose ``main`` deploys *top* at its top (none when None)
    and SUB_COPY under ``svc/``, with a kew project at ``svc/``. Returns
    ``(svc, main)``."""
    repo = git_repo(tmp_path / "repo")
    if top is not None:
        commit_reviewer_artifact(repo, top)
    main = commit_reviewer_artifact(repo, SUB_COPY, under="svc")
    svc = repo / "svc"
    (svc / "logs").mkdir(parents=True)
    (svc / "kew.toml").write_text('[logging]\ndir = "logs"\n')
    return svc, main


def _cli(svc: Path, monkeypatch: pytest.MonkeyPatch, *args: str) -> dict[str, Any]:
    monkeypatch.setenv("KEW_PROJECT", str(svc))
    result = CliRunner().invoke(cli_app, ["prompts", *args, "--json"])
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def _served(svc: Path, monkeypatch: pytest.MonkeyPatch, inbox_client) -> Any:
    """The app ``kew serve`` builds when started from *svc*."""
    monkeypatch.chdir(svc)
    served: list = []
    with (
        patch("kew.cli.preflight_tools"),
        patch("uvicorn.run", side_effect=lambda app, **_: served.append(app)),
    ):
        result = CliRunner().invoke(cli_app, ["serve"])
    assert result.exit_code == 0, result.output
    return inbox_client(served[0], loopback_peer=True)


def _review_config(tmp_path: Path, *, blocking: bool) -> DispatchConfig:
    config = _fx._config()
    config.review.adversarial = True
    config.review.blocking = blocking
    config.review.max_rounds = 2
    config.review.publish_status = False
    config.review.publish_comment = False
    config.logging.dir = str(tmp_path / "logs")
    return config


def _drive_review(
    tmp_path: Path,
    svc: Path,
    head: str,
    monkeypatch: pytest.MonkeyPatch,
    *,
    blocking: bool = False,
) -> tuple[list[ReviewerLaunchContext], SqliteRunStore]:
    """One production ``advance()`` tick from *svc* over an open PR at *head*,
    the reviewer container faked; returns what the launcher was handed and
    the (open) store."""
    monkeypatch.chdir(svc)
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
    store = SqliteRunStore(str(tmp_path / "run.db"), project_root=tmp_path)
    advance(
        EPIC,
        config=_review_config(tmp_path, blocking=blocking),
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
    return launched, store


def test_kew_prompts_shows_the_repository_tops_copy_for_a_project_in_a_subdirectory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc, _main = _svc_project(tmp_path, top=TOP_COPY)

    shown = _cli(svc, monkeypatch, "show", "adversarial_review")

    assert shown["source"]["kind"] == "deployed-artifact"
    assert shown["source"]["sha256"] == _sha(TOP_COPY)
    assert shown["base_prompt"].startswith(TOP_COPY)
    assert "A copy nothing reads." not in shown["base_prompt"]


def test_kew_serve_serves_the_repository_tops_copy_for_a_project_in_a_subdirectory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, inbox_client
) -> None:
    svc, _main = _svc_project(tmp_path, top=TOP_COPY)
    client = _served(svc, monkeypatch, inbox_client)

    listed = client.get("/api/prompts")
    body = client.get("/api/prompts/adversarial_review")

    assert listed.status_code == 200, listed.text
    source = {r["role"]: r for r in listed.json()}["adversarial_review"]["source"]
    assert source["sha256"] == _sha(TOP_COPY)
    assert body.status_code == 200, body.text
    assert body.json()["source"] == source
    assert body.json()["preview"].startswith(TOP_COPY)


def test_the_drive_launches_the_review_under_the_repository_tops_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The drive's own tick, run from the project's subdirectory, launches the
    reviewer with the top copy and attests its sha — the copy the two
    previews above name."""
    svc, main = _svc_project(tmp_path, top=TOP_COPY)

    launched, store = _drive_review(tmp_path, svc, main, monkeypatch)
    store.close()

    (ctx,) = launched
    assert ctx.prompt.startswith(TOP_COPY)
    assert "A copy nothing reads." not in ctx.prompt
    assert ctx.prompt_artifact_sha == _sha(TOP_COPY)


def test_a_copy_only_under_the_subdirectory_deploys_nothing_on_any_surface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, inbox_client
) -> None:
    """With no copy at the top, the launch runs the packaged default, so
    ``kew prompts list``, ``show``, the served API and the launch all say so —
    none of them picks up the copy under ``svc/``."""
    svc, main = _svc_project(tmp_path, top=None)
    packaged = resolve_reviewer_prompt().text  # no repository: the packaged default
    target = reviewer_prompt_artifact().target

    listed = _cli(svc, monkeypatch, "list")
    shown = _cli(svc, monkeypatch, "show", "adversarial_review")
    served = _served(svc, monkeypatch, inbox_client).get("/api/prompts")
    launched, store = _drive_review(tmp_path, svc, main, monkeypatch)
    store.close()

    reviewer = next(r for r in listed if r["role"] == "adversarial_review")
    assert reviewer["source"] != f"deployed-artifact:{target}"
    assert shown["source"]["kind"] == "pinned-artifact"
    assert shown["source"]["sha256"] == _sha(packaged)
    api = {r["role"]: r for r in served.json()}["adversarial_review"]["source"]
    assert api["kind"] == "pinned-artifact"
    (ctx,) = launched
    assert ctx.prompt.startswith(packaged)
    assert ctx.prompt_artifact_sha == _sha(packaged)


def test_an_unreadable_deployed_copy_holds_the_node_and_launches_no_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The base ref lists a copy Git cannot read. The drive's launch raises
    before any reviewer checkout or container, so no review runs under another
    prompt, and the blocking review stage holds the node instead."""
    svc, main = _svc_project(tmp_path, top=TOP_COPY)
    lose_blob(svc.parent, TOP_COPY)

    launched, store = _drive_review(tmp_path, svc, main, monkeypatch, blocking=True)
    try:
        holds = store.list_epic_holds(EPIC)
        latest = store.latest_review_round(ITEM)
    finally:
        store.close()

    assert launched == []
    (hold,) = holds
    assert hold.item_id == ITEM
    assert latest is None or latest.verdict != "READY"
