"""Tests for run_epic_plan orchestration (agent dispatch mocked) + CLI wiring."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from support.gh_fakes import FakeGh

from kew import subprocess_utils
from kew.config import DispatchConfig
from kew.criteria import extract_criteria
from kew.graph import plan_run
from kew.graph.domain import Edge, Graph, Node, NodeStatus
from kew.graph.plan_run import run_epic_plan
from kew.worktree import is_linked_worktree


def _write_manifest(tmp_path: Path, issues: list[dict[str, Any]]) -> Path:
    p = tmp_path / "manifest.json"
    p.write_text(
        json.dumps({"epic": "demo", "plan_path": "docs/design/x.md", "issues": issues})
    )
    return p


def _criteria(lid: str) -> str:
    """Two item-specific criteria — the planning contract's minimum (#2292)."""
    return (
        "\n\n## Acceptance Criteria\n\n"
        f"- [ ] `step_{lid}()` returns the parsed row "
        f"(`uv run pytest tests/test_{lid}.py -k parsed`)\n"
        f"- [ ] `kew epic plan --dry-run` lists node {lid} in its wave "
        "(`uv run kew epic plan demo --dry-run`)\n"
    )


def _issue(lid: str, deps: list[str] | None = None) -> dict[str, Any]:
    return {
        "local_id": lid,
        "title": f"feat: {lid}",
        "body": f"**Plan file:** `docs/design/x.md`\n\n{lid}" + _criteria(lid),
        "priority": "p2-medium",
        "complexity": "simple",
        "type": "feature",
        "depends_on": deps or [],
    }


def _oversized_issue(lid: str) -> dict[str, Any]:
    issue = _issue(lid)
    issue["target_files"] = [f"src/f{i}.py" for i in range(11)]
    return issue


def _manifest_at(tmp_path: Path, name: str, issues: list[dict[str, Any]]) -> Path:
    p = tmp_path / f"{name}.json"
    p.write_text(
        json.dumps({"epic": "demo", "plan_path": "docs/design/x.md", "issues": issues})
    )
    return p


def _patch_plan_dispatch(
    monkeypatch: pytest.MonkeyPatch, checkout: Path, agent_result: Any
) -> None:
    resolved = SimpleNamespace(
        runner=SimpleNamespace(name="claude-code"), model="claude-sonnet-4-6"
    )
    containment = SimpleNamespace(
        sandboxed=True,
        image="agent-image:test",
        posture="sandboxed",
    )
    monkeypatch.setattr(plan_run, "_setup_plan_worktree", lambda *a: checkout)
    monkeypatch.setattr(plan_run, "_copy_plan_into", lambda *a: "plan.md")
    monkeypatch.setattr(plan_run, "build_epic_plan_prompt", lambda **k: "prompt")
    monkeypatch.setattr("kew.runners.resolve_runner", lambda *a, **k: resolved)
    monkeypatch.setattr(
        plan_run, "resolve_flow_containment", lambda *a, **k: containment
    )
    monkeypatch.setattr("kew.dispatch_core.run_tracked_agent", lambda **k: agent_result)


class _EmptySource:
    def list_epic_nodes(self, epic_ref: str) -> list[Node]:
        return []


class _BusySource:
    def list_epic_nodes(self, epic_ref: str) -> list[Node]:
        return [Node(id="1", title="old", status=NodeStatus.DONE, epic_ref=epic_ref)]


class _ExistingSource:
    def list_epic_nodes(self, epic_ref: str) -> list[Node]:
        return [
            Node(id=node_id, title="old", status=NodeStatus.TODO, epic_ref=epic_ref)
            for node_id in ("50", "51")
        ]


@pytest.fixture()
def config(tmp_path: Path) -> DispatchConfig:
    cfg = DispatchConfig()
    cfg.project.repo = "o/r"
    cfg.logging.dir = str(tmp_path / "logs")
    return cfg


@pytest.fixture()
def gh(monkeypatch: pytest.MonkeyPatch) -> FakeGh:
    fake = FakeGh()
    monkeypatch.setattr("kew.graph.plan.run", fake)
    # keep the orchestration module's own git calls (base-branch detection,
    # committed-plan check) hermetic too
    monkeypatch.setattr(plan_run, "run", fake)
    monkeypatch.setattr(plan_run, "detect_base_branch", lambda base: "main")
    return fake


def _patch_source(monkeypatch: pytest.MonkeyPatch, source: Any) -> None:
    monkeypatch.setattr(plan_run, "make_dependency_source", lambda *a, **k: source)


def _graph_of(count: int) -> Graph:
    return Graph(
        [
            Node(
                id=str(101 + i), title=f"n{i}", status=NodeStatus.TODO, epic_ref="demo"
            )
            for i in range(count)
        ],
        [],
    )


def test_plan_checkout_is_standalone_and_does_not_hardlink_git_objects(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "source"
    subprocess.run(["git", "init", "-q", "-b", "main", str(source)], check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=source,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=source, check=True)
    (source / "tracked.txt").write_text("base")
    subprocess.run(["git", "add", "tracked.txt"], cwd=source, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=source, check=True)
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", str(origin)], cwd=source, check=True
    )
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=source, check=True)

    config = DispatchConfig()
    config.branches.base = "main"
    config.branches.worktree_base = str(tmp_path / "checkouts")
    monkeypatch.chdir(source)
    git_path = shutil.which("git")
    assert git_path is not None
    monkeypatch.setitem(subprocess_utils.TOOL_PATHS, "git", git_path)

    checkout = plan_run._setup_plan_worktree("demo", config)

    assert (checkout / ".git").is_dir()
    assert not is_linked_worktree(checkout)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=source,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    # The checkout holds the commit, in whatever form the narrowed fetch stored
    # it: since #2442 the objects arrive as a pack, not as loose files, so the
    # property is checked over every object file rather than one loose path.
    subprocess.run(["git", "cat-file", "-e", sha], cwd=checkout, check=True)

    def object_identities(repo: Path) -> set[tuple[int, int]]:
        objects = repo / ".git" / "objects"
        return {
            (path.stat().st_dev, path.stat().st_ino)
            for path in objects.rglob("*")
            if path.is_file()
        }

    shared = object_identities(source) & object_identities(checkout)
    assert not shared, "the plan checkout hardlinks the operator's git objects"


def test_manifest_export_rejects_symlink_to_host_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    checkout = tmp_path / "checkout"
    host_file = tmp_path / "operator-data.json"
    host_file.write_text('{"protected": true}')
    source = checkout / plan_run.MANIFEST_RELPATH
    source.parent.mkdir(parents=True)
    source.symlink_to(host_file)
    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")
    _patch_plan_dispatch(
        monkeypatch, checkout, SimpleNamespace(exit_code=0, launched=True)
    )

    result = plan_run._dispatch_plan_agent(
        name="demo",
        plan_path=tmp_path / "plan.md",
        config=config,
        store=None,
        item_id="plan:demo",
    )

    assert result.manifest_path is None
    assert result.failure is not None
    assert checkout.is_dir()
    assert host_file.read_text() == '{"protected": true}'
    assert "securely open" in capsys.readouterr().err


def test_manifest_export_rejects_fifo_without_blocking(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    checkout = tmp_path / "checkout"
    source = checkout / plan_run.MANIFEST_RELPATH
    source.parent.mkdir(parents=True)
    os.mkfifo(source)
    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")
    _patch_plan_dispatch(
        monkeypatch, checkout, SimpleNamespace(exit_code=0, launched=True)
    )

    result = plan_run._dispatch_plan_agent(
        name="demo",
        plan_path=tmp_path / "plan.md",
        config=config,
        store=None,
        item_id="plan:demo",
    )

    assert result.manifest_path is None
    assert result.failure is not None
    assert checkout.is_dir()
    assert "regular file" in capsys.readouterr().err


def test_manifest_export_rejects_oversized_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    checkout = tmp_path / "checkout"
    source = checkout / plan_run.MANIFEST_RELPATH
    source.parent.mkdir(parents=True)
    source.write_bytes(b"x" * (plan_run.MAX_MANIFEST_EXPORT_BYTES + 1))
    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")
    _patch_plan_dispatch(
        monkeypatch, checkout, SimpleNamespace(exit_code=0, launched=True)
    )

    result = plan_run._dispatch_plan_agent(
        name="demo",
        plan_path=tmp_path / "plan.md",
        config=config,
        store=None,
        item_id="plan:demo",
    )

    assert result.manifest_path is None
    assert result.failure is not None
    assert checkout.is_dir()
    assert "size limit" in capsys.readouterr().err


def test_manifest_export_is_skipped_when_agent_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    checkout = tmp_path / "checkout"
    manifest = checkout / plan_run.MANIFEST_RELPATH
    manifest.parent.mkdir(parents=True)
    manifest.write_text('{"epic": "demo"}')
    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")
    _patch_plan_dispatch(
        monkeypatch,
        checkout,
        SimpleNamespace(exit_code=1, launched=True),
    )
    export = MagicMock()
    monkeypatch.setattr(plan_run, "_export_plan_manifest", export)

    result = plan_run._dispatch_plan_agent(
        name="demo",
        plan_path=tmp_path / "plan.md",
        config=config,
        store=None,
        item_id="plan:demo",
    )

    assert result.manifest_path is None
    assert result.failure is not None
    export.assert_not_called()
    assert checkout.is_dir()


def test_successful_plan_cleanup_preserves_same_named_operator_branch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from kew.tracking import DispatchResult

    operator = tmp_path / "operator"
    subprocess.run(["git", "init", "-q", "-b", "main", str(operator)], check=True)
    _git_config = ["git", "config"]
    subprocess.run(
        [*_git_config, "user.email", "test@example.invalid"],
        cwd=operator,
        check=True,
    )
    subprocess.run([*_git_config, "user.name", "Test User"], cwd=operator, check=True)
    (operator / "tracked.txt").write_text("base")
    subprocess.run(["git", "add", "tracked.txt"], cwd=operator, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=operator, check=True)
    branch = plan_run._plan_branch("demo")
    subprocess.run(["git", "branch", branch], cwd=operator, check=True)

    checkout = tmp_path / "checkout"
    manifest = checkout / plan_run.MANIFEST_RELPATH
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps({"epic": "demo", "plan_path": "docs/design/x.md", "issues": []})
    )
    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")
    _patch_plan_dispatch(
        monkeypatch,
        checkout,
        DispatchResult(exit_code=0, issue_num=0, log_file=tmp_path / "run.log"),
    )
    git_path = shutil.which("git")
    assert git_path is not None
    monkeypatch.setitem(subprocess_utils.TOOL_PATHS, "git", git_path)
    monkeypatch.chdir(operator)

    result = plan_run._dispatch_plan_agent(
        name="demo",
        plan_path=tmp_path / "plan.md",
        config=config,
        store=None,
        item_id="plan:demo",
    )

    assert result.manifest_path is not None
    assert not checkout.exists()
    subprocess.run(
        ["git", "show-ref", "--verify", f"refs/heads/{branch}"],
        cwd=operator,
        check=True,
    )


def test_planning_preflights_the_forced_complex_runner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from kew.config import RouteEntry, RoutingConfig

    config = DispatchConfig(
        routing=RoutingConfig(
            default=RouteEntry(runner="claude-code", model="claude-sonnet-4-6"),
            complex=RouteEntry(runner="codex", model="gpt-5.6-sol"),
        )
    )
    worktree = tmp_path / "worktree"
    refusal = "Runner 'codex' is not logged in. Authenticate with: codex login"
    preflighted: list[str | None] = []
    launched: list[str] = []
    monkeypatch.setattr(plan_run, "_setup_plan_worktree", lambda *a: worktree)
    monkeypatch.setattr(plan_run, "_copy_plan_into", lambda *a: Path("plan.md"))
    monkeypatch.setattr(plan_run, "build_epic_plan_prompt", lambda **k: "prompt")
    monkeypatch.setattr(
        "kew.runner_preflight.runner_preflight_failure",
        lambda _config, *, runner_name=None: preflighted.append(runner_name) or refusal,
    )
    monkeypatch.setattr(
        "kew.dispatch_core.run_tracked_agent",
        lambda **k: launched.append("launch"),
    )

    result = plan_run._dispatch_plan_agent(
        name="demo",
        plan_path=tmp_path / "plan.md",
        config=config,
        store=None,
        item_id="plan:demo",
    )

    assert result.manifest_path is None
    assert result.failure is not None
    assert preflighted == ["codex"]
    assert launched == []


def test_planning_launches_the_preflighted_runner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from kew.config import RouteEntry, RoutingConfig
    from kew.tracking import DispatchResult

    config = DispatchConfig(
        routing=RoutingConfig(
            default=RouteEntry(runner="claude-code", model="claude-sonnet-4-6"),
            complex=RouteEntry(runner="codex", model="gpt-5.6-sol"),
        )
    )
    worktree = tmp_path / "worktree"
    preflighted: list[str | None] = []
    launch = MagicMock(
        return_value=DispatchResult(
            exit_code=1,
            issue_num=0,
            log_file=tmp_path / "run.log",
        )
    )
    monkeypatch.setattr(plan_run, "_setup_plan_worktree", lambda *a: worktree)
    monkeypatch.setattr(plan_run, "_copy_plan_into", lambda *a: Path("plan.md"))
    monkeypatch.setattr(plan_run, "build_epic_plan_prompt", lambda **k: "prompt")
    monkeypatch.setattr(
        "kew.runner_preflight.runner_preflight_failure",
        lambda _config, *, runner_name=None: preflighted.append(runner_name) or None,
    )
    monkeypatch.setattr("kew.dispatch_core.run_tracked_agent", launch)

    result = plan_run._dispatch_plan_agent(
        name="demo",
        plan_path=tmp_path / "plan.md",
        config=config,
        store=None,
        item_id="plan:demo",
    )

    assert result.manifest_path is None
    assert result.failure is not None
    launched_runner = launch.call_args.kwargs["runner"]
    assert preflighted == [launched_runner.name]


def test_planning_attests_and_forwards_container_before_launch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from kew.sandbox import SandboxDecision
    from kew.tracking import DispatchResult

    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    resolved = SimpleNamespace(
        runner=SimpleNamespace(name="claude-code"), model="claude-sonnet-4-6"
    )
    decision = SandboxDecision(
        sandboxed=True,
        runtime="docker",
        posture="sandboxed",
        notice="Sandbox: on (docker)",
        image="agent-image:test",
    )
    order: list[tuple[str, Any]] = []

    monkeypatch.setattr(plan_run, "_setup_plan_worktree", lambda *a: checkout)
    monkeypatch.setattr(plan_run, "_copy_plan_into", lambda *a: Path("plan.md"))
    monkeypatch.setattr(plan_run, "build_epic_plan_prompt", lambda **k: "prompt")
    monkeypatch.setattr("kew.runners.resolve_runner", lambda *a, **k: resolved)
    monkeypatch.setattr(
        "kew.sandbox.resolve_effective_sandbox", lambda *a, **k: decision
    )
    monkeypatch.setattr(
        "kew.runner_preflight.runner_preflight_failure",
        lambda *a, **k: "host runner is unavailable",
    )

    def _emit(_store: Any, event: str, **kwargs: Any) -> None:
        order.append((event, kwargs))

    def _launch(**kwargs: Any) -> DispatchResult:
        order.append(("launch", kwargs))
        return DispatchResult(exit_code=1, issue_num=0, log_file=tmp_path / "run.log")

    monkeypatch.setattr("kew.audit.emit", _emit)
    monkeypatch.setattr("kew.dispatch_core.run_tracked_agent", _launch)

    result = plan_run._dispatch_plan_agent(
        name="demo",
        plan_path=tmp_path / "plan.md",
        config=config,
        store=MagicMock(),
        item_id="plan:demo",
    )

    assert result.manifest_path is None
    assert result.failure is not None
    assert [name for name, _ in order[:2]] == ["sandbox_resolved", "launch"]
    event = order[0][1]
    assert event["payload"]["posture"] == "sandboxed"
    assert event["payload"]["image"] == "agent-image:test"
    launch = order[1][1]
    assert event["run_id"] == launch["run_id"]
    assert launch["contained_image"] == "agent-image:test"
    assert launch["sandbox_posture"] == "sandboxed"


def test_planning_refuses_host_execution_without_escape_hatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from kew.config import SandboxConfig
    from kew.tracking import DispatchResult

    config = DispatchConfig(sandbox=SandboxConfig(enabled=False))
    checkout = tmp_path / "checkout"
    resolved = SimpleNamespace(
        runner=SimpleNamespace(name="claude-code"), model="claude-sonnet-4-6"
    )
    events: list[dict[str, Any]] = []
    launch = MagicMock(
        return_value=DispatchResult(
            exit_code=1, issue_num=0, log_file=tmp_path / "run.log"
        )
    )

    monkeypatch.setattr(plan_run, "_setup_plan_worktree", lambda *a: checkout)
    monkeypatch.setattr(plan_run, "_copy_plan_into", lambda *a: Path("plan.md"))
    monkeypatch.setattr(plan_run, "build_epic_plan_prompt", lambda **k: "prompt")
    monkeypatch.setattr("kew.runners.resolve_runner", lambda *a, **k: resolved)
    monkeypatch.setattr(
        "kew.runner_preflight.runner_preflight_failure", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "kew.audit.emit",
        lambda _store, event, **kwargs: events.append({"event": event, **kwargs}),
    )
    monkeypatch.setattr("kew.dispatch_core.run_tracked_agent", launch)

    result = plan_run._dispatch_plan_agent(
        name="demo",
        plan_path=tmp_path / "plan.md",
        config=config,
        store=MagicMock(),
        item_id="plan:demo",
    )

    assert result.manifest_path is None
    assert result.failure is not None
    launch.assert_not_called()
    assert events[0]["event"] == "sandbox_resolved"
    assert events[0]["payload"]["posture"] == "refused"


def _patch_real_graph(monkeypatch: pytest.MonkeyPatch, count: int) -> None:
    monkeypatch.setattr(plan_run, "build_graph", lambda *a, **k: _graph_of(count))


class TestRunEpicPlan:
    def test_refuses_when_epic_already_has_issues(
        self, config: DispatchConfig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _patch_source(monkeypatch, _BusySource())
        manifest = _write_manifest(tmp_path, [_issue("a")])
        rc = run_epic_plan(
            name="demo",
            config=config,
            plan_path=None,
            manifest_path=manifest,
            dry_run=False,
            store=None,
        )
        assert rc == 2

    def test_from_manifest_creates_and_validates(
        self,
        config: DispatchConfig,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        gh: FakeGh,
    ) -> None:
        _patch_source(monkeypatch, _EmptySource())
        _patch_real_graph(monkeypatch, 2)
        manifest = _write_manifest(tmp_path, [_issue("a"), _issue("b", ["a"])])
        rc = run_epic_plan(
            name="demo",
            config=config,
            plan_path=None,
            manifest_path=manifest,
            dry_run=False,
            store=None,
        )
        assert rc == 0
        assert len(gh.creates()) == 2
        # label ensured before creation
        verbs = [c[:3] for c in gh.calls]
        assert verbs.index(["gh", "label", "create"]) < verbs.index(
            ["gh", "issue", "create"]
        )

    def test_creation_registers_epic_for_run_all(
        self,
        config: DispatchConfig,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        gh: FakeGh,
    ) -> None:
        # Planning an epic must seed a driver-state row so `epic run --all`
        # (which enumerates store.list_epic_refs()) sees it before its first
        # tick (#740).
        from kew.run_store import SqliteRunStore

        _patch_source(monkeypatch, _EmptySource())
        _patch_real_graph(monkeypatch, 2)
        manifest = _write_manifest(tmp_path, [_issue("a"), _issue("b", ["a"])])
        store = SqliteRunStore(str(tmp_path / "dispatch.db"))
        try:
            assert store.list_epic_refs() == []
            rc = run_epic_plan(
                name="demo",
                config=config,
                plan_path=None,
                manifest_path=manifest,
                dry_run=False,
                store=store,
            )
            assert rc == 0
            assert store.list_epic_refs() == ["demo"]
        finally:
            store.close()

    def test_dry_run_registers_nothing(
        self,
        config: DispatchConfig,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        gh: FakeGh,
    ) -> None:
        # Dry-run creates no issues, so it must not register the epic either.
        from kew.run_store import SqliteRunStore

        _patch_source(monkeypatch, _EmptySource())
        manifest = _write_manifest(tmp_path, [_issue("a")])
        store = SqliteRunStore(str(tmp_path / "dispatch.db"))
        try:
            rc = run_epic_plan(
                name="demo",
                config=config,
                plan_path=None,
                manifest_path=manifest,
                dry_run=True,
                store=store,
            )
            assert rc == 0
            assert store.list_epic_refs() == []
        finally:
            store.close()

    def test_dry_run_creates_nothing(
        self,
        config: DispatchConfig,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        gh: FakeGh,
    ) -> None:
        _patch_source(monkeypatch, _EmptySource())
        manifest = _write_manifest(tmp_path, [_issue("a"), _issue("b", ["a"])])
        rc = run_epic_plan(
            name="demo",
            config=config,
            plan_path=None,
            manifest_path=manifest,
            dry_run=True,
            store=None,
        )
        assert rc == 0
        assert gh.creates() == []

    def test_dry_run_previews_each_node_criteria(
        self,
        config: DispatchConfig,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        gh: FakeGh,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # The dry run is the only look a human gets at the draft before the
        # issues exist, so it shows what the work will be judged against
        # (#2292) — not just the titles and the wave order.
        _patch_source(monkeypatch, _EmptySource())
        manifest = _write_manifest(tmp_path, [_issue("a"), _issue("b", ["a"])])
        rc = run_epic_plan(
            name="demo",
            config=config,
            plan_path=None,
            manifest_path=manifest,
            dry_run=True,
            store=None,
        )
        assert rc == 0
        out = capsys.readouterr().err
        for lid in ("a", "b"):
            for criterion in extract_criteria(_issue(lid)["body"]):
                assert criterion in out
        assert "All new and existing tests pass" not in out
        assert gh.creates() == []

    def test_invalid_manifest_creates_nothing(
        self,
        config: DispatchConfig,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        gh: FakeGh,
    ) -> None:
        _patch_source(monkeypatch, _EmptySource())
        manifest = _write_manifest(
            tmp_path,
            [_issue("a", ["b"]), _issue("b", ["a"])],  # cycle
        )
        rc = run_epic_plan(
            name="demo",
            config=config,
            plan_path=None,
            manifest_path=manifest,
            dry_run=False,
            store=None,
        )
        assert rc == 1
        assert gh.creates() == []

    def test_agent_path_dispatches_then_creates(
        self,
        config: DispatchConfig,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        gh: FakeGh,
    ) -> None:
        _patch_source(monkeypatch, _EmptySource())
        _patch_real_graph(monkeypatch, 1)
        manifest = _write_manifest(tmp_path, [_issue("a")])
        plan_file = tmp_path / "plan.md"
        plan_file.write_text("# plan")
        seen: dict[str, Any] = {}

        def fake_dispatch(**kwargs: Any) -> plan_run.PlanDispatchOutcome:
            seen.update(kwargs)
            return plan_run.PlanDispatchOutcome(True, manifest)

        monkeypatch.setattr(plan_run, "_dispatch_plan_agent", fake_dispatch)
        rc = run_epic_plan(
            name="demo",
            config=config,
            plan_path=plan_file,
            manifest_path=None,
            dry_run=False,
            store=None,
        )
        assert rc == 0
        assert seen["name"] == "demo"
        assert len(gh.creates()) == 1

    def test_agent_failure_stops_flow(
        self,
        config: DispatchConfig,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        gh: FakeGh,
    ) -> None:
        _patch_source(monkeypatch, _EmptySource())
        plan_file = tmp_path / "plan.md"
        plan_file.write_text("# plan")
        monkeypatch.setattr(
            plan_run,
            "_dispatch_plan_agent",
            lambda **k: plan_run.PlanDispatchOutcome(True, failure="agent failed"),
        )
        rc = run_epic_plan(
            name="demo",
            config=config,
            plan_path=plan_file,
            manifest_path=None,
            dry_run=False,
            store=None,
        )
        assert rc == 1
        assert gh.creates() == []

    def test_missing_plan_file_is_precondition_error(
        self,
        config: DispatchConfig,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        gh: FakeGh,
    ) -> None:
        _patch_source(monkeypatch, _EmptySource())
        rc = run_epic_plan(
            name="demo",
            config=config,
            plan_path=tmp_path / "absent.md",
            manifest_path=None,
            dry_run=False,
            store=None,
        )
        assert rc == 2


class TestResplitGate:
    """T4: an oversized draft node is fed back to the planning agent for one
    re-split retry, then hard-fails the plan if it is still oversized. No issues
    are created until that decision is final (validate-before-create)."""

    def test_oversized_draft_resplits_then_creates(
        self,
        config: DispatchConfig,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        gh: FakeGh,
    ) -> None:
        _patch_source(monkeypatch, _EmptySource())
        _patch_real_graph(monkeypatch, 2)
        oversized = _manifest_at(tmp_path, "over", [_oversized_issue("a")])
        resplit = _manifest_at(tmp_path, "split", [_issue("a"), _issue("b", ["a"])])
        plan_file = tmp_path / "plan.md"
        plan_file.write_text("# plan")
        paths = iter([oversized, resplit])
        calls: dict[str, Any] = {"n": 0, "extra": []}

        def fake_dispatch(**kwargs: Any) -> plan_run.PlanDispatchOutcome:
            calls["n"] += 1
            calls["extra"].append(kwargs.get("extra_instruction"))
            return plan_run.PlanDispatchOutcome(True, next(paths))

        monkeypatch.setattr(plan_run, "_dispatch_plan_agent", fake_dispatch)
        rc = run_epic_plan(
            name="demo",
            config=config,
            plan_path=plan_file,
            manifest_path=None,
            dry_run=False,
            store=None,
        )
        assert rc == 0
        # exactly one retry: first pass got no instruction, the retry did
        assert calls["n"] == 2
        assert calls["extra"][0] is None
        assert calls["extra"][1] is not None
        assert "re-split" in calls["extra"][1].lower()
        assert "'a'" in calls["extra"][1]
        # the re-split (in-limit) draft is what actually gets created
        assert len(gh.creates()) == 2

    def test_still_oversized_after_retry_hard_fails(
        self,
        config: DispatchConfig,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        gh: FakeGh,
    ) -> None:
        _patch_source(monkeypatch, _EmptySource())
        first = _manifest_at(tmp_path, "over1", [_oversized_issue("a")])
        second = _manifest_at(tmp_path, "over2", [_oversized_issue("a")])
        plan_file = tmp_path / "plan.md"
        plan_file.write_text("# plan")
        paths = iter([first, second])
        calls = {"n": 0}

        def fake_dispatch(**kwargs: Any) -> plan_run.PlanDispatchOutcome:
            calls["n"] += 1
            return plan_run.PlanDispatchOutcome(True, next(paths))

        monkeypatch.setattr(plan_run, "_dispatch_plan_agent", fake_dispatch)
        rc = run_epic_plan(
            name="demo",
            config=config,
            plan_path=plan_file,
            manifest_path=None,
            dry_run=False,
            store=None,
        )
        assert rc == 1
        # one initial dispatch + one retry, no third attempt
        assert calls["n"] == 2
        # gate is validate-before-create: nothing reached GitHub
        assert gh.creates() == []


class TestGuardrails:
    def test_invalid_epic_name_refused(
        self, config: DispatchConfig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _patch_source(monkeypatch, _EmptySource())
        manifest = _write_manifest(tmp_path, [_issue("a")])
        for bad in ("my epic", "a..b", "x.lock", "-lead", "a~b"):
            rc = run_epic_plan(
                name=bad,
                config=config,
                plan_path=None,
                manifest_path=manifest,
                dry_run=True,
                store=None,
            )
            assert rc == 2, bad

    def test_uncommitted_plan_warns(
        self,
        config: DispatchConfig,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        gh: FakeGh,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        import subprocess as sp

        def git_run(cmd: list[str], **kw: Any) -> sp.CompletedProcess[str]:
            if cmd[:3] == ["git", "cat-file", "-e"]:
                return sp.CompletedProcess(cmd, 1, stdout="", stderr="")
            return sp.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(plan_run, "run", git_run)
        _patch_source(monkeypatch, _EmptySource())
        _patch_real_graph(monkeypatch, 1)
        manifest = _write_manifest(tmp_path, [_issue("a")])
        rc = run_epic_plan(
            name="demo",
            config=config,
            plan_path=None,
            manifest_path=manifest,
            dry_run=False,
            store=None,
        )
        assert rc == 0
        out = capsys.readouterr()
        assert "commit and push" in out.out + out.err


class TestPostCreationGate:
    """GitHub's label index lags fresh issues — the gate must retry, and must
    never pass vacuously on an empty round-trip graph (the onramp-smoke
    dogfood bug)."""

    def test_retries_until_graph_complete(
        self,
        config: DispatchConfig,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        gh: FakeGh,
    ) -> None:
        _patch_source(monkeypatch, _EmptySource())
        monkeypatch.setattr(plan_run.time, "sleep", lambda s: None)
        graphs = iter([_graph_of(0), _graph_of(0), _graph_of(2)])
        calls = {"n": 0}

        def lagging_build(*a: Any, **k: Any) -> Graph:
            calls["n"] += 1
            return next(graphs)

        monkeypatch.setattr(plan_run, "build_graph", lagging_build)
        manifest = _write_manifest(tmp_path, [_issue("a"), _issue("b", ["a"])])
        rc = run_epic_plan(
            name="demo",
            config=config,
            plan_path=None,
            manifest_path=manifest,
            dry_run=False,
            store=None,
        )
        assert rc == 0
        assert calls["n"] == 3

    def test_incomplete_graph_warns_without_vacuous_validate(
        self,
        config: DispatchConfig,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        gh: FakeGh,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _patch_source(monkeypatch, _EmptySource())
        monkeypatch.setattr(plan_run.time, "sleep", lambda s: None)
        _patch_real_graph(monkeypatch, 0)  # never catches up
        manifest = _write_manifest(tmp_path, [_issue("a"), _issue("b", ["a"])])
        rc = run_epic_plan(
            name="demo",
            config=config,
            plan_path=None,
            manifest_path=manifest,
            dry_run=False,
            store=None,
        )
        assert rc == 1
        output = capsys.readouterr().err
        assert "validation deferred" in output
        assert "#101, #102" in output
        # An empty read is index lag, not a lost node: it must not send the
        # operator to restore issues that are fine.
        assert "Restore" not in output

    def test_provider_read_failure_is_not_reported_as_index_lag(
        self,
        config: DispatchConfig,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        gh: FakeGh,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _patch_source(monkeypatch, _EmptySource())
        monkeypatch.setattr(plan_run.time, "sleep", lambda s: None)

        def raising_build(*a: Any, **k: Any) -> Graph:
            raise RuntimeError("gh issue list failed (rc=1)")

        monkeypatch.setattr(plan_run, "build_graph", raising_build)
        manifest = _write_manifest(tmp_path, [_issue("a")])
        rc = run_epic_plan(
            name="demo",
            config=config,
            plan_path=None,
            manifest_path=manifest,
            dry_run=False,
            store=None,
        )
        assert rc == 2
        output = capsys.readouterr().err
        assert output.count("Round-trip read failed") == 3
        assert "read could not be performed" in output
        assert "credential or connectivity" in output
        assert "kew epic validate demo" in output
        assert "hasn't indexed" not in output
        assert "validation deferred" not in output

    def test_partial_adoption_exits_nonzero_and_names_only_the_lost_node(
        self,
        config: DispatchConfig,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        gh: FakeGh,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _patch_source(monkeypatch, _ExistingSource())
        monkeypatch.setattr(plan_run.time, "sleep", lambda seconds: None)
        monkeypatch.setattr(
            plan_run,
            "link_native_relations",
            lambda *args, **kwargs: [
                "#51: could not be added as a sub-issue of #102 (no access)"
            ],
        )
        monkeypatch.setattr(
            plan_run,
            "build_graph",
            lambda *args, **kwargs: Graph(
                [
                    Node(
                        id=node_id,
                        title="node",
                        status=NodeStatus.TODO,
                        epic_ref="demo",
                    )
                    for node_id in ("50", "101")
                ],
                [],
            ),
        )
        manifest = _write_manifest(tmp_path, [_issue("a")])

        rc = run_epic_plan(
            name="demo",
            config=config,
            plan_path=None,
            manifest_path=manifest,
            dry_run=False,
            store=None,
            amend=True,
        )

        output = capsys.readouterr().err
        assert rc == 1
        missing_line = next(
            line for line in output.splitlines() if "missing expected epic node" in line
        )
        assert "#51" in missing_line
        assert "#50" not in missing_line
        assert "#101" not in missing_line


class TestEpicPlanCli:
    def _invoke(
        self,
        args: list[str],
        monkeypatch: pytest.MonkeyPatch,
        config: DispatchConfig,
        rc: int = 0,
    ) -> tuple[Any, dict[str, Any]]:
        from typer.testing import CliRunner

        import kew.cli as cli_mod

        seen: dict[str, Any] = {}

        def fake_run_epic_plan(**kwargs: Any) -> int:
            seen.update(kwargs)
            return rc

        monkeypatch.setattr(cli_mod, "load_config", lambda: config)
        monkeypatch.setattr(cli_mod, "_preflight", lambda c, **kw: None)
        monkeypatch.setattr("kew.graph.plan_run.run_epic_plan", fake_run_epic_plan)
        runner = CliRunner()
        result = runner.invoke(cli_mod.app, ["epic", "plan", *args])
        return result, seen

    def test_requires_exactly_one_source(
        self, monkeypatch: pytest.MonkeyPatch, config: DispatchConfig, tmp_path: Path
    ) -> None:
        result, _ = self._invoke(["demo"], monkeypatch, config)
        assert result.exit_code == 2
        plan = tmp_path / "p.md"
        plan.write_text("x")
        mani = tmp_path / "m.json"
        mani.write_text("{}")
        result, _ = self._invoke(
            ["demo", "--from", str(plan), "--from-manifest", str(mani)],
            monkeypatch,
            config,
        )
        assert result.exit_code == 2

    def test_happy_path_passes_args(
        self, monkeypatch: pytest.MonkeyPatch, config: DispatchConfig, tmp_path: Path
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".git").mkdir()
        plan = tmp_path / "p.md"
        plan.write_text("x")
        result, seen = self._invoke(
            ["demo", "--from", str(plan), "--dry-run"], monkeypatch, config
        )
        assert result.exit_code == 0
        assert seen["name"] == "demo"
        assert seen["plan_path"] == plan
        assert seen["dry_run"] is True

    def test_nonzero_rc_propagates(
        self, monkeypatch: pytest.MonkeyPatch, config: DispatchConfig, tmp_path: Path
    ) -> None:
        plan = tmp_path / "p.md"
        plan.write_text("x")
        result, _ = self._invoke(
            ["demo", "--from", str(plan)], monkeypatch, config, rc=1
        )
        assert result.exit_code == 1

    def test_non_github_tracker_refused(
        self, monkeypatch: pytest.MonkeyPatch, config: DispatchConfig, tmp_path: Path
    ) -> None:
        config.tracker.type = "linear"
        plan = tmp_path / "p.md"
        plan.write_text("x")
        result, _ = self._invoke(["demo", "--from", str(plan)], monkeypatch, config)
        assert result.exit_code == 2


class TestPlanBudgetBucket:
    """#1038: each `epic plan` invocation gets a distinct per-run budget bucket
    (durable ``item_id``) so planning spend is governed by the per-issue cap for
    one run, instead of accumulating without bound onto the shared ``#0``
    sentinel and eventually blocking every future planning dispatch."""

    def test_bucket_is_distinct_per_call_and_names_the_epic(self) -> None:
        from kew.ids import item_ref

        a = plan_run._plan_budget_bucket("demo")
        b = plan_run._plan_budget_bucket("demo")
        assert a != b
        assert a.startswith("epic-plan:demo:")
        # non-numeric id => rendered bare, never as a cross-linking `#0`
        assert item_ref(a) == a

    def test_two_invocations_do_not_share_a_bucket(
        self,
        config: DispatchConfig,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        gh: FakeGh,
    ) -> None:
        _patch_source(monkeypatch, _EmptySource())
        _patch_real_graph(monkeypatch, 2)
        manifest = _write_manifest(tmp_path, [_issue("a")])
        plan_file = tmp_path / "plan.md"
        plan_file.write_text("# plan")
        buckets: list[str] = []

        def fake_dispatch(**kwargs: Any) -> plan_run.PlanDispatchOutcome:
            buckets.append(kwargs["item_id"])
            return plan_run.PlanDispatchOutcome(True, manifest)

        monkeypatch.setattr(plan_run, "_dispatch_plan_agent", fake_dispatch)
        for _ in range(2):
            rc = run_epic_plan(
                name="demo",
                config=config,
                plan_path=plan_file,
                manifest_path=None,
                dry_run=False,
                store=None,
            )
            assert rc == 0
        assert len(buckets) == 2
        assert buckets[0] != buckets[1]

    def test_resplit_retry_shares_the_invocation_bucket(
        self,
        config: DispatchConfig,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        gh: FakeGh,
    ) -> None:
        _patch_source(monkeypatch, _EmptySource())
        _patch_real_graph(monkeypatch, 2)
        oversized = _manifest_at(tmp_path, "over", [_oversized_issue("a")])
        resplit = _manifest_at(tmp_path, "split", [_issue("a"), _issue("b", ["a"])])
        plan_file = tmp_path / "plan.md"
        plan_file.write_text("# plan")
        paths = iter([oversized, resplit])
        buckets: list[str] = []

        def fake_dispatch(**kwargs: Any) -> plan_run.PlanDispatchOutcome:
            buckets.append(kwargs["item_id"])
            return plan_run.PlanDispatchOutcome(True, next(paths))

        monkeypatch.setattr(plan_run, "_dispatch_plan_agent", fake_dispatch)
        rc = run_epic_plan(
            name="demo",
            config=config,
            plan_path=plan_file,
            manifest_path=None,
            dry_run=False,
            store=None,
        )
        assert rc == 0
        # the re-split retry is part of the same invocation → same bucket
        assert len(buckets) == 2
        assert buckets[0] == buckets[1]

    def test_distinct_buckets_keep_each_run_under_the_per_issue_cap(self) -> None:
        """End-to-end acceptance: two successive planning invocations, each
        costing over half the cap, do NOT sum onto one capped item."""
        from datetime import UTC, datetime

        from kew.budget import effective_budget_view, per_issue_overspend_note
        from kew.run_store import RunRecord, SqliteRunStore

        cfg = DispatchConfig()
        cfg.budget.per_issue_usd = 45.0
        now = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
        b1, b2 = "epic-plan:demo:1:aaaaaaaa", "epic-plan:demo:2:bbbbbbbb"
        with SqliteRunStore(":memory:") as store:
            for i, bucket in enumerate((b1, b2)):
                store.save(
                    RunRecord(
                        run_id=f"r{i}",
                        issue_num=0,
                        branch="",
                        status="success",
                        exit_code=0,
                        started_at=now,
                        estimated_cost_usd=30.0,
                        item_id=bucket,
                        run_kind="planning",
                    )
                )
            # Each invocation's bucket sees only its own $30 run — under the cap.
            for bucket in (b1, b2):
                view = effective_budget_view(
                    cfg, store, issue=0, item_id=bucket, now=now
                )
                assert view.issue_spent_usd == 30.0
                assert per_issue_overspend_note(view) is None
            # Regression guard: the OLD behavior (keyed on the `#0` sentinel)
            # summed both runs to $60 and would trip the $45 per-issue cap.
            sentinel = effective_budget_view(cfg, store, issue=0, item_id=None, now=now)
            assert sentinel.issue_spent_usd == 60.0


class TestReadBackFailureModes:
    """The read-back gate must name the case it actually hit (#1782 round 3)."""

    def _run(self, config: DispatchConfig, tmp_path: Path) -> int:
        manifest = _write_manifest(tmp_path, [_issue("a")])
        return run_epic_plan(
            name="demo",
            config=config,
            plan_path=None,
            manifest_path=manifest,
            dry_run=False,
            store=None,
        )

    def test_a_success_then_failures_is_a_read_failure_not_index_lag(
        self,
        config: DispatchConfig,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        gh: FakeGh,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Attempt 1 succeeds and returns nothing; 2 and 3 raise. Aggregating
        # "any attempt succeeded" reported index lag and told the operator to
        # wait, when the credential is what needs fixing.
        _patch_source(monkeypatch, _EmptySource())
        monkeypatch.setattr(plan_run.time, "sleep", lambda s: None)
        calls = {"n": 0}

        def build(*a: Any, **k: Any) -> Graph:
            calls["n"] += 1
            if calls["n"] == 1:
                return Graph([], [])
            raise RuntimeError("auth denied")

        monkeypatch.setattr(plan_run, "build_graph", build)

        rc = self._run(config, tmp_path)

        assert rc == 2
        output = capsys.readouterr().err
        assert "read could not be performed" in output
        assert "indexed" not in output

    def test_a_non_runtime_read_error_returns_through_the_gate(
        self,
        config: DispatchConfig,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        gh: FakeGh,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # The read reaches gh through subprocess, so a missing binary is an
        # OSError. It must return 2 through this gate, not escape it.
        _patch_source(monkeypatch, _EmptySource())
        monkeypatch.setattr(plan_run.time, "sleep", lambda s: None)

        def build(*a: Any, **k: Any) -> Graph:
            raise FileNotFoundError("gh missing")

        monkeypatch.setattr(plan_run, "build_graph", build)

        rc = self._run(config, tmp_path)

        assert rc == 2
        assert "read could not be performed" in capsys.readouterr().err


class TestExactGateMembership:
    """The gate counts members, never external boundary nodes (#1782 round 4)."""

    def test_an_external_boundary_node_does_not_satisfy_the_gate(
        self,
        config: DispatchConfig,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        gh: FakeGh,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Membership lost #101, but #102 still depends on it, so build_graph
        # resolves #101 as an EXTERNAL node. Counting it let the gate pass
        # while the driver could never dispatch #101.
        _patch_source(monkeypatch, _EmptySource())
        monkeypatch.setattr(plan_run.time, "sleep", lambda s: None)
        graph = Graph(
            [
                Node(
                    id="101",
                    title="lost",
                    status=NodeStatus.TODO,
                    epic_ref="demo",
                    external=True,
                ),
                Node(id="102", title="kept", status=NodeStatus.TODO, epic_ref="demo"),
            ],
            [Edge(src="101", dst="102")],
        )
        monkeypatch.setattr(plan_run, "build_graph", lambda *a, **k: graph)
        manifest = _write_manifest(tmp_path, [_issue("a"), _issue("b", ["a"])])

        rc = run_epic_plan(
            name="demo",
            config=config,
            plan_path=None,
            manifest_path=manifest,
            dry_run=False,
            store=None,
        )

        assert rc == 1
        assert "#101" in capsys.readouterr().err

    def test_a_number_too_large_for_int_returns_through_the_gate(
        self,
        config: DispatchConfig,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        gh: FakeGh,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # json.loads("1e309") is inf, and int(inf) raises OverflowError in the
        # adapter. It must return 2 through the gate, not escape it.
        _patch_source(monkeypatch, _EmptySource())
        monkeypatch.setattr(plan_run.time, "sleep", lambda s: None)

        def build(*a: Any, **k: Any) -> Graph:
            raise OverflowError("cannot convert float infinity to integer")

        monkeypatch.setattr(plan_run, "build_graph", build)
        manifest = _write_manifest(tmp_path, [_issue("a")])

        rc = run_epic_plan(
            name="demo",
            config=config,
            plan_path=None,
            manifest_path=manifest,
            dry_run=False,
            store=None,
        )

        assert rc == 2
        assert "read could not be performed" in capsys.readouterr().err

    def test_a_malformed_provider_payload_returns_through_the_gate(
        self,
        config: DispatchConfig,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        gh: FakeGh,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # A sub-issue object without "number" makes the adapter raise KeyError.
        # It must return 2 through the gate, not escape it and skip the retries.
        _patch_source(monkeypatch, _EmptySource())
        monkeypatch.setattr(plan_run.time, "sleep", lambda s: None)

        def build(*a: Any, **k: Any) -> Graph:
            raise KeyError("number")

        monkeypatch.setattr(plan_run, "build_graph", build)
        manifest = _write_manifest(tmp_path, [_issue("a")])

        rc = run_epic_plan(
            name="demo",
            config=config,
            plan_path=None,
            manifest_path=manifest,
            dry_run=False,
            store=None,
        )

        assert rc == 2
        assert "read could not be performed" in capsys.readouterr().err


class TestOpenShellHasNoPlanningLaunchPath:
    """Epic planning runs its agent as a container argv, so it has no gateway.

    `resolve_flow_containment` resolved with the dispatch path's
    `gateway_launch=True` default, so this flow attested `posture=sandboxed`
    on the audit chain for `runtime = "openshell"` and was refused only
    afterwards, where `sandbox.build_run_argv` declines to spell a docker
    command line at a CLI with no `run` subcommand (PR #2401 review).
    """

    def test_planning_refuses_before_attesting_a_posture(
        self,
        config: DispatchConfig,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        from kew.config import SandboxConfig
        from kew.openshell_broker import ProviderAdmission
        from kew.run_store import SqliteRunStore
        from kew.sandbox import SandboxRuntimeUnavailable
        from kew.tracking import LaunchState

        config.sandbox = SandboxConfig(
            enabled=True,
            runtime="openshell",
            openshell={"providers": {"claude-code": "kew-claude-code"}},  # type: ignore[arg-type]
        )
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        resolved = SimpleNamespace(
            runner=SimpleNamespace(name="claude-code"), model="claude-sonnet-4-6"
        )
        monkeypatch.setattr(plan_run, "_setup_plan_worktree", lambda *a: checkout)
        monkeypatch.setattr(plan_run, "_copy_plan_into", lambda *a: "plan.md")
        monkeypatch.setattr(plan_run, "build_epic_plan_prompt", lambda **k: "prompt")
        monkeypatch.setattr("kew.runners.resolve_runner", lambda *a, **k: resolved)
        # A REACHABLE gateway holding this runner's provider is what made the
        # bug: every prerequisite but the flow's own launch shape is satisfied.
        monkeypatch.setattr(
            "kew.sandbox.detect_runtime_cached", lambda _config: "/usr/bin/openshell"
        )
        monkeypatch.setattr(
            "kew.openshell_broker.brokered_admission",
            lambda *a, **k: ProviderAdmission("kew-claude-code", True, "present"),
        )
        # The egress-certification gate (#2319) runs in front of the gate
        # under test; opened so this asserts the launch path, not the proof.
        monkeypatch.setattr(
            "kew.sandbox.require_certified_boundary",
            lambda config, *, runner, store: None,
        )

        def _never(**kwargs: Any) -> Any:
            raise AssertionError("planning launched on a runtime it cannot launch into")

        monkeypatch.setattr("kew.dispatch_core.run_tracked_agent", _never)

        store = SqliteRunStore(str(tmp_path / "dispatch.db"))
        try:
            with pytest.raises(SandboxRuntimeUnavailable) as refused:
                plan_run._run_plan_agent(
                    name="demo",
                    plan_path=tmp_path / "plan.md",
                    config=config,
                    store=store,
                    item_id="epic-plan:demo",
                    launch_state=LaunchState(),
                )
            assert refused.value.reason == "openshell-runtime-unavailable"
            assert "command line" in str(refused.value)
            postures = [
                event.payload["posture"]
                for event in store.list_audit_events(event="sandbox_resolved")
            ]
            assert postures == ["refused"]
        finally:
            store.close()
