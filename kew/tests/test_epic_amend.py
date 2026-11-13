"""Tests for the `kew epic plan --amend` re-plan path (Node I6).

Amend appends new nodes to an existing epic through the same governed pipeline
as a fresh plan: reject local_id collisions with existing nodes, enforce I5's
<=10-target_files gate, and wire depends-on markers for both intra-manifest and
cross-existing-node edges. Bare `epic plan` (no --amend) must still refuse an
existing epic.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
from support.gh_fakes import FakeGh

from kew.config import DispatchConfig
from kew.dependencies import format_plan_node_marker, parse_depends_ids
from kew.graph import plan_run
from kew.graph.domain import Graph, Node, NodeStatus
from kew.graph.plan_run import run_epic_plan


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


def _issue(
    lid: str,
    deps: list[str] | None = None,
    target_files: list[str] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "local_id": lid,
        "title": f"feat: {lid}",
        "body": f"**Plan file:** `docs/design/x.md`\n\n{lid}" + _criteria(lid),
        "priority": "p2-medium",
        "complexity": "simple",
        "type": "feature",
        "depends_on": deps or [],
    }
    if target_files is not None:
        out["target_files"] = target_files
    return out


class _ExistingSource:
    """An epic that already has nodes at the given ids."""

    def __init__(self, ids: list[str]) -> None:
        self._ids = ids

    def list_epic_nodes(self, epic_ref: str) -> list[Node]:
        return [
            Node(id=i, title=f"old-{i}", status=NodeStatus.DONE, epic_ref=epic_ref)
            for i in self._ids
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
    monkeypatch.setattr(plan_run, "run", fake)
    monkeypatch.setattr(plan_run, "detect_base_branch", lambda base: "main")
    return fake


def _patch_source(monkeypatch: pytest.MonkeyPatch, source: Any) -> None:
    monkeypatch.setattr(plan_run, "make_dependency_source", lambda *a, **k: source)


def _patch_real_graph(monkeypatch: pytest.MonkeyPatch, node_ids: list[str]) -> None:
    """Fake the post-creation round-trip read.

    The ids must be the ones that really exist after the run — the epic's
    pre-existing nodes plus the issues just created. The gate checks exact
    membership, so a fake naming unrelated ids would assert nothing.
    """
    graph = Graph(
        [
            Node(id=node_id, title=f"n{i}", status=NodeStatus.TODO, epic_ref="demo")
            for i, node_id in enumerate(node_ids)
        ],
        [],
    )
    monkeypatch.setattr(plan_run, "build_graph", lambda *a, **k: graph)


def _create_body(cmd: list[str]) -> str:
    return cmd[cmd.index("--body") + 1]


def test_bare_epic_plan_still_refuses_existing_epic(
    config: DispatchConfig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_source(monkeypatch, _ExistingSource(["501"]))
    manifest = _write_manifest(tmp_path, [_issue("a")])
    rc = run_epic_plan(
        name="demo",
        config=config,
        plan_path=None,
        manifest_path=manifest,
        dry_run=False,
        amend=False,
        store=None,
    )
    assert rc == 2


def test_epic_amend_adds_new_nodes(
    config: DispatchConfig,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    gh: FakeGh,
) -> None:
    # Existing epic has #501; two new nodes: I6b depends on the new I6a
    # (intra-manifest) and on the existing #501 (cross-existing-node).
    _patch_source(monkeypatch, _ExistingSource(["501"]))
    _patch_real_graph(monkeypatch, ["101", "102", "501"])
    manifest = _write_manifest(tmp_path, [_issue("I6a"), _issue("I6b", ["I6a", "501"])])
    rc = run_epic_plan(
        name="demo",
        config=config,
        plan_path=None,
        manifest_path=manifest,
        dry_run=False,
        amend=True,
        store=None,
    )
    assert rc == 0
    creates = gh.creates()
    assert len(creates) == 2
    # I6a is created first (dependency); its number is the first minted (101).
    # I6b's body must carry a kew:depends marker referencing BOTH the new I6a
    # number (101) and the existing node #501.
    i6b = next(c for c in creates if "feat: I6b" in c)
    body = _create_body(i6b)
    assert "kew:depends" in body
    assert "101" in body  # intra-manifest edge -> new issue number
    assert "501" in body  # cross-existing-node edge


def test_epic_amend_rejects_local_id_collision(
    config: DispatchConfig,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    gh: FakeGh,
) -> None:
    # New manifest reuses "501" as a local_id, colliding with the existing node.
    _patch_source(monkeypatch, _ExistingSource(["501"]))
    manifest = _write_manifest(tmp_path, [_issue("501"), _issue("I6b")])
    rc = run_epic_plan(
        name="demo",
        config=config,
        plan_path=None,
        manifest_path=manifest,
        dry_run=False,
        amend=True,
        store=None,
    )
    assert rc == 1
    assert gh.creates() == []


def test_epic_amend_enforces_target_files_gate(
    config: DispatchConfig,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    gh: FakeGh,
) -> None:
    # I5's <=10 target_files gate must still fire on amend manifests.
    _patch_source(monkeypatch, _ExistingSource(["501"]))
    manifest = _write_manifest(
        tmp_path, [_issue("I6a", target_files=[f"f{i}.py" for i in range(11)])]
    )
    rc = run_epic_plan(
        name="demo",
        config=config,
        plan_path=None,
        manifest_path=manifest,
        dry_run=False,
        amend=True,
        store=None,
    )
    assert rc == 1
    assert gh.creates() == []


def test_epic_amend_on_empty_epic_behaves_like_fresh(
    config: DispatchConfig,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    gh: FakeGh,
) -> None:
    # --amend on an epic with no nodes yet is allowed (nothing to collide with).
    _patch_source(monkeypatch, _ExistingSource([]))
    _patch_real_graph(monkeypatch, ["101"])
    manifest = _write_manifest(tmp_path, [_issue("I6a")])
    rc = run_epic_plan(
        name="demo",
        config=config,
        plan_path=None,
        manifest_path=manifest,
        dry_run=False,
        amend=True,
        store=None,
    )
    assert rc == 0
    assert len(gh.creates()) == 1


class _ForgeGh(FakeGh):
    """FakeGh that also lists the issues it created, as `gh issue list` would.

    Every issue it made is authored by :attr:`login`, the account ``gh api
    user`` names, and listed open, newest first. ``overrides`` replaces fields
    of one made issue (its state, say); ``foreign`` holds issues someone else
    put on the label.
    """

    login = "kew-operator"

    def __init__(self, fail_on_create_number: int | None = None) -> None:
        super().__init__(fail_on_create_number)
        self.overrides: dict[int, dict[str, Any]] = {}
        self.foreign: list[dict[str, Any]] = []

    def __call__(
        self, cmd: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        if cmd[:3] == ["gh", "issue", "list"]:
            self.calls.append(cmd)
            label = cmd[cmd.index("--label") + 1]
            listed = [
                {
                    "number": number,
                    "body": _create_body(create),
                    "author": {"login": self.login},
                    "state": "OPEN",
                    "stateReason": None,
                    **self.overrides.get(number, {}),
                }
                for number, create in self.made()
                if label in create
            ] + self.foreign
            listed.sort(key=lambda issue: -int(issue["number"]))
            return subprocess.CompletedProcess(cmd, 0, json.dumps(listed), "")
        if cmd[:3] == ["gh", "api", "user"]:
            self.calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, f"{self.login}\n", "")
        return super().__call__(cmd, **kwargs)

    def made(self) -> list[tuple[int, list[str]]]:
        """(number, argv) of every create that succeeded, in creation order."""
        out: list[tuple[int, list[str]]] = []
        for index, create in enumerate(self.creates(), start=1):
            if self._fail_on != index:
                out.append((100 + index, create))
        return out


@pytest.fixture()
def forge(monkeypatch: pytest.MonkeyPatch) -> _ForgeGh:
    fake = _ForgeGh()
    monkeypatch.setattr("kew.graph.plan.run", fake)
    monkeypatch.setattr(plan_run, "run", fake)
    monkeypatch.setattr(plan_run, "detect_base_branch", lambda base: "main")
    return fake


def _amend(config: DispatchConfig, manifest: Path) -> int:
    return run_epic_plan(
        name="demo",
        config=config,
        plan_path=None,
        manifest_path=manifest,
        dry_run=False,
        amend=True,
        store=None,
    )


def test_reapplying_the_same_amend_manifest_creates_no_second_issue(
    config: DispatchConfig,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    forge: _ForgeGh,
) -> None:
    manifest = _write_manifest(
        tmp_path, [_issue("same-local-id"), _issue("next", ["same-local-id"])]
    )
    _patch_source(monkeypatch, _ExistingSource(["501"]))
    _patch_real_graph(monkeypatch, ["101", "102", "501"])
    assert _amend(config, manifest) == 0
    assert len(forge.creates()) == 2

    _patch_source(monkeypatch, _ExistingSource(["501", "101", "102"]))
    assert _amend(config, manifest) == 0
    assert len(forge.creates()) == 2


def test_rerun_after_a_partial_failure_creates_only_the_missing_nodes(
    config: DispatchConfig,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    gh = _ForgeGh(fail_on_create_number=2)
    monkeypatch.setattr("kew.graph.plan.run", gh)
    monkeypatch.setattr(plan_run, "run", gh)
    monkeypatch.setattr(plan_run, "detect_base_branch", lambda base: "main")
    manifest = _write_manifest(tmp_path, [_issue("A"), _issue("B", ["A", "501"])])
    _patch_source(monkeypatch, _ExistingSource(["501"]))
    assert _amend(config, manifest) == 1
    assert [number for number, _ in gh.made()] == [101]
    out = capsys.readouterr()
    advice = out.out + out.err
    assert "Re-run the same command" in advice
    assert "Clean up" not in advice

    # The first run failed before any membership write: A is on the label only.
    _patch_source(monkeypatch, _ExistingSource(["501"]))
    _patch_real_graph(monkeypatch, ["101", "103", "501"])
    assert _amend(config, manifest) == 0
    made = gh.made()
    assert [number for number, _ in made] == [101, 103]
    b_create = made[1][1]
    assert "feat: B" in b_create
    assert parse_depends_ids(_create_body(b_create)) == ["101", "501"]


def test_bare_rerun_after_a_partial_failure_refuses_and_creates_nothing(
    config: DispatchConfig,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    gh = _ForgeGh(fail_on_create_number=2)
    monkeypatch.setattr("kew.graph.plan.run", gh)
    monkeypatch.setattr(plan_run, "run", gh)
    monkeypatch.setattr(plan_run, "detect_base_branch", lambda base: "main")
    manifest = _write_manifest(tmp_path, [_issue("A"), _issue("B", ["A"])])
    # The first run fails before any membership write, so the epic reads empty.
    _patch_source(monkeypatch, _ExistingSource([]))

    def bare() -> int:
        return run_epic_plan(
            name="demo",
            config=config,
            plan_path=None,
            manifest_path=manifest,
            dry_run=False,
            amend=False,
            store=None,
        )

    assert bare() == 1
    assert [number for number, _ in gh.made()] == [101]
    capsys.readouterr()

    assert bare() == 2
    assert [number for number, _ in gh.made()] == [101]
    out = capsys.readouterr()
    assert "--amend" in out.out + out.err


def test_a_draft_body_carrying_the_plan_node_marker_is_refused(
    config: DispatchConfig,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    forge: _ForgeGh,
) -> None:
    claimed = _issue("B")
    claimed["body"] += "\n" + format_plan_node_marker("demo", "A", "0123abcd") + "\n"
    manifest = _write_manifest(tmp_path, [_issue("A"), claimed])
    _patch_source(monkeypatch, _ExistingSource(["501"]))
    assert _amend(config, manifest) == 1
    assert forge.creates() == []


def test_a_plan_node_of_another_epic_is_not_reused(
    config: DispatchConfig,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    forge: _ForgeGh,
) -> None:
    def listed(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if cmd[:3] == ["gh", "issue", "list"]:
            body = "x\n\n" + format_plan_node_marker("other", "A", "0123abcd") + "\n"
            issues = [
                {
                    "number": 77,
                    "body": body,
                    "author": {"login": forge.login},
                    "state": "OPEN",
                    "stateReason": None,
                }
            ]
            return subprocess.CompletedProcess(cmd, 0, json.dumps(issues), "")
        return forge(cmd, **kwargs)

    monkeypatch.setattr("kew.graph.plan.run", listed)
    _patch_source(monkeypatch, _ExistingSource(["77"]))
    _patch_real_graph(monkeypatch, ["77", "101"])
    manifest = _write_manifest(tmp_path, [_issue("A")])
    assert _amend(config, manifest) == 0
    assert len(forge.creates()) == 1


def test_an_unreadable_epic_label_creates_nothing(
    config: DispatchConfig,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    forge: _ForgeGh,
) -> None:
    def failing(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if cmd[:3] == ["gh", "issue", "list"]:
            return subprocess.CompletedProcess(cmd, 1, "", "rate limited")
        return forge(cmd, **kwargs)

    monkeypatch.setattr("kew.graph.plan.run", failing)
    _patch_source(monkeypatch, _ExistingSource(["501"]))
    manifest = _write_manifest(tmp_path, [_issue("A")])
    assert _amend(config, manifest) == 2
    assert forge.creates() == []


def _output(capsys: pytest.CaptureFixture[str]) -> str:
    out = capsys.readouterr()
    return out.out + out.err


def test_a_later_manifest_reusing_a_local_id_is_refused_and_creates_nothing(
    config: DispatchConfig,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    forge: _ForgeGh,
    capsys: pytest.CaptureFixture[str],
) -> None:
    first = _write_manifest(tmp_path, [_issue("auth-core")])
    _patch_source(monkeypatch, _ExistingSource(["501"]))
    _patch_real_graph(monkeypatch, ["101", "501"])
    assert _amend(config, first) == 0
    assert len(forge.creates()) == 1
    capsys.readouterr()

    # Weeks later a DIFFERENT amendment happens to pick the same slug.
    (tmp_path / "later").mkdir()
    new_work = _issue("auth-core")
    new_work["title"] = "feat: totally new work"
    later = _write_manifest(tmp_path / "later", [new_work, _issue("ui", ["auth-core"])])
    _patch_source(monkeypatch, _ExistingSource(["501", "101"]))
    assert _amend(config, later) == 2
    assert len(forge.creates()) == 1
    advice = _output(capsys)
    assert "'auth-core'" in advice
    assert "#101" in advice
    assert "fresh id" in advice


def test_a_changed_body_under_a_created_local_id_is_refused(
    config: DispatchConfig,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    forge: _ForgeGh,
) -> None:
    _patch_source(monkeypatch, _ExistingSource(["501"]))
    _patch_real_graph(monkeypatch, ["101", "501"])
    assert _amend(config, _write_manifest(tmp_path, [_issue("A")])) == 0

    edited = _issue("A")
    edited["body"] += "\nOne more paragraph.\n"
    (tmp_path / "edited").mkdir()
    _patch_source(monkeypatch, _ExistingSource(["501", "101"]))
    assert _amend(config, _write_manifest(tmp_path / "edited", [edited])) == 2
    assert len(forge.creates()) == 1


def _first_run_bodies(
    config: DispatchConfig,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    issues: list[dict[str, Any]],
) -> list[str]:
    """The exact bodies kew writes for *issues*, from a run on a scratch forge."""
    scratch = _ForgeGh()
    monkeypatch.setattr("kew.graph.plan.run", scratch)
    monkeypatch.setattr(plan_run, "run", scratch)
    _patch_source(monkeypatch, _ExistingSource(["501"]))
    _patch_real_graph(monkeypatch, ["501", *[str(101 + i) for i in range(len(issues))]])
    (tmp_path / "scratch").mkdir()
    assert _amend(config, _write_manifest(tmp_path / "scratch", issues)) == 0
    return [_create_body(create) for _, create in scratch.made()]


def test_a_forged_marker_on_an_issue_another_login_wrote_is_not_reused(
    config: DispatchConfig,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    issues = [_issue("A"), _issue("B", ["A"])]
    (copied_a, _) = _first_run_bodies(config, monkeypatch, tmp_path, issues)
    gh = _ForgeGh()
    # An older issue, by someone else, carrying kew's exact body for node A.
    gh.foreign.append(
        {
            "number": 7,
            "body": copied_a,
            "author": {"login": "mallory"},
            "state": "OPEN",
            "stateReason": None,
        }
    )
    monkeypatch.setattr("kew.graph.plan.run", gh)
    monkeypatch.setattr(plan_run, "run", gh)
    _patch_source(monkeypatch, _ExistingSource(["501"]))
    _patch_real_graph(monkeypatch, ["7", "101", "102", "501"])
    assert _amend(config, _write_manifest(tmp_path, issues)) == 0
    made = gh.made()
    assert [number for number, _ in made] == [101, 102]
    assert parse_depends_ids(_create_body(made[1][1])) == ["101"]


def _foreign_copy(number: int, body: str, login: str) -> dict[str, Any]:
    return {
        "number": number,
        "body": body,
        "author": {"login": login},
        "state": "OPEN",
        "stateReason": None,
    }


def test_a_node_another_login_made_from_this_manifest_is_named_in_a_warning(
    config: DispatchConfig,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    issues = [_issue("A"), _issue("B", ["A"])]
    (copied_a, _) = _first_run_bodies(config, monkeypatch, tmp_path, issues)
    capsys.readouterr()
    gh = _ForgeGh()
    # The earlier run was made under another gh login (a re-auth since).
    gh.foreign.append(_foreign_copy(7, copied_a, "old-login"))
    monkeypatch.setattr("kew.graph.plan.run", gh)
    monkeypatch.setattr(plan_run, "run", gh)
    _patch_source(monkeypatch, _ExistingSource(["501"]))
    _patch_real_graph(monkeypatch, ["7", "101", "102", "501"])
    assert _amend(config, _write_manifest(tmp_path, issues)) == 0
    assert [number for number, _ in gh.made()] == [101, 102]
    advice = _output(capsys)
    assert re.search(r"#7\b", advice)
    assert "old-login" in advice
    assert gh.login in advice


def test_a_foreign_marker_for_different_work_is_ignored_without_a_warning(
    config: DispatchConfig,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    other = _issue("A")
    other["title"] = "feat: unrelated work"
    (copied, _) = _first_run_bodies(config, monkeypatch, tmp_path, [other, _issue("Z")])
    capsys.readouterr()
    gh = _ForgeGh()
    gh.foreign.append(_foreign_copy(7, copied, "old-login"))
    monkeypatch.setattr("kew.graph.plan.run", gh)
    monkeypatch.setattr(plan_run, "run", gh)
    _patch_source(monkeypatch, _ExistingSource(["501"]))
    _patch_real_graph(monkeypatch, ["7", "101", "501"])
    assert _amend(config, _write_manifest(tmp_path, [_issue("A")])) == 0
    assert [number for number, _ in gh.made()] == [101]
    assert "old-login" not in _output(capsys)


def test_a_duplicate_closed_as_not_planned_leaves_the_live_issue_reused(
    config: DispatchConfig,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    gh = _ForgeGh(fail_on_create_number=2)
    monkeypatch.setattr("kew.graph.plan.run", gh)
    monkeypatch.setattr(plan_run, "run", gh)
    monkeypatch.setattr(plan_run, "detect_base_branch", lambda base: "main")
    manifest = _write_manifest(tmp_path, [_issue("A"), _issue("B", ["A"])])
    _patch_source(monkeypatch, _ExistingSource(["501"]))
    assert _amend(config, manifest) == 1
    (a_number, a_create) = gh.made()[0]
    # A second copy of A was made, then closed as the refusal advises.
    duplicate = _foreign_copy(150, _create_body(a_create), gh.login)
    duplicate.update(state="CLOSED", stateReason="NOT_PLANNED")
    gh.foreign.append(duplicate)

    _patch_real_graph(monkeypatch, ["101", "103", "501"])
    assert _amend(config, manifest) == 0
    made = gh.made()
    assert [number for number, _ in made] == [a_number, 103]
    assert parse_depends_ids(_create_body(made[1][1])) == [str(a_number)]


def test_two_issues_kew_made_for_one_local_id_are_refused(
    config: DispatchConfig,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    forge: _ForgeGh,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest = _write_manifest(tmp_path, [_issue("A"), _issue("B", ["A"])])
    _patch_source(monkeypatch, _ExistingSource(["501"]))
    _patch_real_graph(monkeypatch, ["101", "102", "501"])
    assert _amend(config, manifest) == 0
    (a_number, a_create) = forge.made()[0]
    forge.foreign.append(
        {
            "number": 150,
            "body": _create_body(a_create),
            "author": {"login": forge.login},
            "state": "OPEN",
            "stateReason": None,
        }
    )
    capsys.readouterr()

    _patch_source(monkeypatch, _ExistingSource(["501", "101", "102"]))
    assert _amend(config, manifest) == 2
    assert len(forge.creates()) == 2
    advice = _output(capsys)
    # The refusal names the issue to keep (the oldest) and the one to close.
    assert f"keep #{a_number}" in advice
    assert "close #150" in advice


def test_a_live_copy_kept_after_the_oldest_was_closed_is_refused(
    config: DispatchConfig,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    gh = _ForgeGh(fail_on_create_number=2)
    monkeypatch.setattr("kew.graph.plan.run", gh)
    monkeypatch.setattr(plan_run, "run", gh)
    monkeypatch.setattr(plan_run, "detect_base_branch", lambda base: "main")
    manifest = _write_manifest(tmp_path, [_issue("A"), _issue("B", ["A"])])
    _patch_source(monkeypatch, _ExistingSource(["501"]))
    assert _amend(config, manifest) == 1
    (a_number, a_create) = gh.made()[0]
    # The operator closed the ORIGINAL (the lower number) and kept a copy:
    # dependents made from the original may still name it.
    gh.overrides[a_number] = {"state": "CLOSED", "stateReason": "NOT_PLANNED"}
    gh.foreign.append(_foreign_copy(150, _create_body(a_create), gh.login))
    capsys.readouterr()

    _patch_real_graph(monkeypatch, ["101", "103", "150", "501"])
    assert _amend(config, manifest) == 2
    assert [number for number, _ in gh.made()] == [a_number]
    advice = _output(capsys)
    assert re.search(rf"#{a_number}\b", advice)
    assert re.search(r"#150\b", advice)


def test_a_closed_node_with_no_reason_is_refused_for_an_unknown_reason(
    config: DispatchConfig,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    forge: _ForgeGh,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest = _write_manifest(tmp_path, [_issue("A")])
    _patch_source(monkeypatch, _ExistingSource(["501"]))
    _patch_real_graph(monkeypatch, ["101", "501"])
    assert _amend(config, manifest) == 0
    forge.overrides[101] = {"state": "CLOSED", "stateReason": None}
    capsys.readouterr()

    _patch_source(monkeypatch, _ExistingSource(["501", "101"]))
    assert _amend(config, manifest) == 2
    assert "#101 (closed for an unknown reason)" in _output(capsys)


def test_a_bare_plan_over_markers_another_login_wrote_is_refused_before_dispatch(
    config: DispatchConfig,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (copied_a,) = _first_run_bodies(config, monkeypatch, tmp_path, [_issue("A")])
    capsys.readouterr()
    gh = _ForgeGh()
    # The earlier `epic plan` ran under another gh login, so the epic reads
    # empty and none of its plan-node issues is kew's own under this login.
    gh.foreign.append(_foreign_copy(7, copied_a, "old-login"))
    monkeypatch.setattr("kew.graph.plan.run", gh)
    monkeypatch.setattr(plan_run, "run", gh)
    _patch_source(monkeypatch, _ExistingSource([]))
    rc = run_epic_plan(
        name="demo",
        config=config,
        plan_path=tmp_path / "no-such-plan.md",
        manifest_path=None,
        dry_run=False,
        amend=False,
        store=None,
    )
    assert rc == 2
    assert gh.creates() == []
    advice = _output(capsys)
    # Refused at the precondition, before the plan file (and any agent) is used.
    assert "Plan file not found" not in advice
    assert re.search(r"#7\b", advice)
    assert "old-login" in advice
    assert "--amend" in advice


def test_a_node_closed_as_not_planned_is_refused_by_number(
    config: DispatchConfig,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    forge: _ForgeGh,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest = _write_manifest(tmp_path, [_issue("A"), _issue("B", ["A"])])
    _patch_source(monkeypatch, _ExistingSource(["501"]))
    _patch_real_graph(monkeypatch, ["101", "102", "501"])
    assert _amend(config, manifest) == 0
    forge.overrides[101] = {"state": "CLOSED", "stateReason": "NOT_PLANNED"}
    capsys.readouterr()

    _patch_source(monkeypatch, _ExistingSource(["501", "101", "102"]))
    assert _amend(config, manifest) == 2
    assert len(forge.creates()) == 2
    advice = _output(capsys)
    assert "#101" in advice
    assert "NOT_PLANNED" in advice


def test_a_node_closed_as_completed_is_reused(
    config: DispatchConfig,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    forge: _ForgeGh,
) -> None:
    manifest = _write_manifest(tmp_path, [_issue("A"), _issue("B", ["A"])])
    _patch_source(monkeypatch, _ExistingSource(["501"]))
    _patch_real_graph(monkeypatch, ["101", "102", "501"])
    assert _amend(config, manifest) == 0
    forge.overrides[101] = {"state": "CLOSED", "stateReason": "COMPLETED"}

    _patch_source(monkeypatch, _ExistingSource(["501", "101", "102"]))
    assert _amend(config, manifest) == 0
    assert len(forge.creates()) == 2


def test_plan_nodes_are_not_trusted_when_the_login_cannot_be_read(
    config: DispatchConfig,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    forge: _ForgeGh,
) -> None:
    manifest = _write_manifest(tmp_path, [_issue("A"), _issue("B", ["A"])])
    _patch_source(monkeypatch, _ExistingSource(["501"]))
    _patch_real_graph(monkeypatch, ["101", "102", "501"])
    assert _amend(config, manifest) == 0

    def no_login(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if cmd[:3] == ["gh", "api", "user"]:
            return subprocess.CompletedProcess(cmd, 1, "", "HTTP 401")
        return forge(cmd, **kwargs)

    monkeypatch.setattr("kew.graph.plan.run", no_login)
    monkeypatch.setattr(plan_run, "run", no_login)
    _patch_source(monkeypatch, _ExistingSource(["501", "101", "102"]))
    assert _amend(config, manifest) == 2
    assert len(forge.creates()) == 2


def test_an_epic_label_listing_at_the_read_limit_creates_nothing(
    config: DispatchConfig,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    forge: _ForgeGh,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def full(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if cmd[:3] == ["gh", "issue", "list"]:
            limit = int(cmd[cmd.index("--limit") + 1])
            issues = [
                {
                    "number": 5000 - i,
                    "body": "old work",
                    "author": {"login": forge.login},
                    "state": "OPEN",
                    "stateReason": None,
                }
                for i in range(limit)
            ]
            return subprocess.CompletedProcess(cmd, 0, json.dumps(issues), "")
        return forge(cmd, **kwargs)

    monkeypatch.setattr("kew.graph.plan.run", full)
    monkeypatch.setattr(plan_run, "run", full)
    _patch_source(monkeypatch, _ExistingSource(["501"]))
    _patch_real_graph(monkeypatch, ["101", "501"])
    assert _amend(config, _write_manifest(tmp_path, [_issue("A")])) == 2
    assert forge.creates() == []
    assert "epic:demo" in _output(capsys)


def test_an_empty_local_id_is_refused(
    config: DispatchConfig,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    forge: _ForgeGh,
) -> None:
    _patch_source(monkeypatch, _ExistingSource(["501"]))
    _patch_real_graph(monkeypatch, ["101", "501"])
    assert _amend(config, _write_manifest(tmp_path, [_issue("")])) == 1
    assert forge.creates() == []


class TestAmendCli:
    def _invoke(
        self,
        args: list[str],
        monkeypatch: pytest.MonkeyPatch,
        config: DispatchConfig,
    ) -> dict[str, Any]:
        from typer.testing import CliRunner

        import kew.cli as cli_mod

        seen: dict[str, Any] = {}

        def fake_run_epic_plan(**kwargs: Any) -> int:
            seen.update(kwargs)
            return 0

        monkeypatch.setattr(cli_mod, "load_config", lambda: config)
        monkeypatch.setattr(cli_mod, "_preflight", lambda c, **kw: None)
        monkeypatch.setattr("kew.graph.plan_run.run_epic_plan", fake_run_epic_plan)
        runner = CliRunner()
        result = runner.invoke(cli_mod.app, ["epic", "plan", *args])
        assert result.exit_code == 0, result.output
        return seen

    def test_amend_flag_threads_through(
        self, monkeypatch: pytest.MonkeyPatch, config: DispatchConfig, tmp_path: Path
    ) -> None:
        mani = tmp_path / "m.json"
        mani.write_text("{}")
        seen = self._invoke(
            ["demo", "--from-manifest", str(mani), "--amend"], monkeypatch, config
        )
        assert seen["amend"] is True

    def test_amend_defaults_false(
        self, monkeypatch: pytest.MonkeyPatch, config: DispatchConfig, tmp_path: Path
    ) -> None:
        mani = tmp_path / "m.json"
        mani.write_text("{}")
        seen = self._invoke(["demo", "--from-manifest", str(mani)], monkeypatch, config)
        assert seen["amend"] is False
