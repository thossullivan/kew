"""Tests for the plan→issues on-ramp manifest layer (kew epic plan)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from support.gh_fakes import FakeGh

from kew.config import DispatchConfig
from kew.dependencies import format_epic_container_marker
from kew.graph.adapters.github_convention import GitHubConventionSource
from kew.graph.adapters.github_native import (
    GitHubNativeSource,
    reset_linkage_probe_cache,
)
from kew.graph.plan import (
    MANIFEST_RELPATH,
    CreatedIssue,
    EpicPlanManifest,
    ManifestError,
    PartialCreationError,
    build_epic_plan_prompt,
    create_issues,
    draft_graph,
    ensure_labels,
    link_native_relations,
    load_manifest,
    validate_manifest,
)


def _criteria(lid: str) -> str:
    """Two item-specific criteria — the planning contract's minimum (#2292)."""
    return (
        "\n\n## Acceptance Criteria\n\n"
        f"- [ ] `step_{lid}()` returns the parsed row "
        f"(`uv run pytest tests/test_{lid}.py -k parsed`)\n"
        f"- [ ] `kew epic plan --dry-run` lists node {lid} in its wave "
        "(`uv run kew epic plan demo --dry-run`)\n"
    )


def _issue(lid: str, deps: list[str] | None = None, **over: Any) -> dict[str, Any]:
    d: dict[str, Any] = {
        "local_id": lid,
        "title": f"feat: part {lid} (Tasks 1-2)",
        "body": (
            f"## Plan Reference\n\n**Plan file:** `docs/design/x.md`\n\npart {lid}"
            + _criteria(lid)
        ),
        "priority": "p2-medium",
        "complexity": "simple",
        "type": "feature",
        "depends_on": deps or [],
    }
    d.update(over)
    return d


def _manifest(issues: list[dict[str, Any]]) -> dict[str, Any]:
    return {"epic": "demo", "plan_path": "docs/design/x.md", "issues": issues}


class TestManifestSchema:
    def test_load_valid_manifest(self, tmp_path: Path) -> None:
        p = tmp_path / "m.json"
        p.write_text(json.dumps(_manifest([_issue("a"), _issue("b", ["a"])])))
        m = load_manifest(p)
        assert isinstance(m, EpicPlanManifest)
        assert [i.local_id for i in m.issues] == ["a", "b"]
        assert m.issues[1].depends_on == ["a"]

    def test_bad_json_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "m.json"
        p.write_text("{not json")
        with pytest.raises(ManifestError):
            load_manifest(p)

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ManifestError):
            load_manifest(tmp_path / "absent.json")

    def test_unknown_field_rejected(self, tmp_path: Path) -> None:
        bad = _manifest([_issue("a")])
        bad["issues"][0]["labels"] = ["ready"]  # no free-form labels — by construction
        p = tmp_path / "m.json"
        p.write_text(json.dumps(bad))
        with pytest.raises(ManifestError):
            load_manifest(p)

    def test_bad_enum_rejected(self, tmp_path: Path) -> None:
        p = tmp_path / "m.json"
        p.write_text(json.dumps(_manifest([_issue("a", priority="urgent")])))
        with pytest.raises(ManifestError):
            load_manifest(p)

    def test_empty_issues_rejected(self, tmp_path: Path) -> None:
        p = tmp_path / "m.json"
        p.write_text(json.dumps(_manifest([])))
        with pytest.raises(ManifestError):
            load_manifest(p)


class TestValidateManifest:
    def test_valid_manifest_no_problems(self) -> None:
        m = EpicPlanManifest.model_validate(
            _manifest([_issue("a"), _issue("b", ["a"])])
        )
        assert validate_manifest(m) == []

    def test_cycle_detected(self) -> None:
        m = EpicPlanManifest.model_validate(
            _manifest([_issue("a", ["b"]), _issue("b", ["a"])])
        )
        assert any("cycle" in p for p in validate_manifest(m))

    def test_dangling_dep_detected(self) -> None:
        m = EpicPlanManifest.model_validate(_manifest([_issue("a", ["ghost"])]))
        assert any("ghost" in p for p in validate_manifest(m))

    def test_self_dep_detected(self) -> None:
        m = EpicPlanManifest.model_validate(_manifest([_issue("a", ["a"])]))
        assert any("self" in p for p in validate_manifest(m))

    def test_duplicate_local_id_detected(self) -> None:
        m = EpicPlanManifest.model_validate(_manifest([_issue("a"), _issue("a")]))
        assert any("duplicate" in p for p in validate_manifest(m))

    def test_body_missing_plan_path_detected(self) -> None:
        m = EpicPlanManifest.model_validate(
            _manifest([_issue("a", body="no plan reference here")])
        )
        assert any("plan file" in p for p in validate_manifest(m))

    def test_agent_written_depends_marker_rejected(self) -> None:
        # parse_depends_ids unions every marker in a body, so an
        # agent-written one would add edges beside kew's appended marker
        body = "**Plan file:** `docs/design/x.md`\n\n<!-- kew:depends 999 -->"
        m = EpicPlanManifest.model_validate(_manifest([_issue("a", body=body)]))
        assert any("kew:depends" in p for p in validate_manifest(m))


class TestDraftGraph:
    def test_edges_point_dep_to_dependent(self) -> None:
        m = EpicPlanManifest.model_validate(
            _manifest([_issue("a"), _issue("b", ["a"])])
        )
        g = draft_graph(m)
        assert set(g.nodes) == {"a", "b"}
        assert [(e.src, e.dst) for e in g.edges] == [("a", "b")]


class TestCreateIssues:
    def _patch(self, monkeypatch: pytest.MonkeyPatch, fake: FakeGh) -> None:
        monkeypatch.setattr("kew.graph.plan.run", fake)

    def test_topo_order_and_dep_rewrite(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = FakeGh()
        self._patch(monkeypatch, fake)
        m = EpicPlanManifest.model_validate(
            _manifest([_issue("b", ["a"]), _issue("a"), _issue("c", ["a", "b"])])
        )
        created = create_issues(m, epic_name="demo", repo="o/r")
        # topo order: a first, then b, then c
        assert [c.local_id for c in created] == ["a", "b", "c"]
        assert [c.number for c in created] == [101, 102, 103]
        assert all(isinstance(c, CreatedIssue) for c in created)
        # dependent bodies carry kew:depends with REAL numbers, appended by kew
        create_cmds = fake.creates()
        body_b = create_cmds[1][create_cmds[1].index("--body") + 1]
        assert "<!-- kew:depends 101 -->" in body_b
        body_c = create_cmds[2][create_cmds[2].index("--body") + 1]
        assert "<!-- kew:depends 101 102 -->" in body_c
        # wave-1 issue has no depends block
        body_a = create_cmds[0][create_cmds[0].index("--body") + 1]
        assert "kew:depends" not in body_a

    def test_labels_applied_and_never_ready(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeGh()
        self._patch(monkeypatch, fake)
        m = EpicPlanManifest.model_validate(_manifest([_issue("a")]))
        create_issues(m, epic_name="demo", repo="o/r")
        cmd = fake.creates()[0]
        labels = [cmd[i + 1] for i, tok in enumerate(cmd) if tok == "--label"]
        assert labels == ["epic:demo", "p2-medium", "simple", "feature"]
        assert "ready" not in labels

    def test_partial_failure_reports_created(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeGh(fail_on_create_number=2)
        self._patch(monkeypatch, fake)
        m = EpicPlanManifest.model_validate(
            _manifest([_issue("a"), _issue("b", ["a"])])
        )
        with pytest.raises(PartialCreationError) as exc_info:
            create_issues(m, epic_name="demo", repo="o/r")
        err = exc_info.value
        assert [c.number for c in err.created] == [101]
        assert err.failed_local_id == "b"
        assert "boom" in err.cause

    def test_invalid_manifest_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = FakeGh()
        self._patch(monkeypatch, fake)
        m = EpicPlanManifest.model_validate(_manifest([_issue("a", ["a"])]))
        with pytest.raises(ValueError, match="validate_manifest"):
            create_issues(m, epic_name="demo", repo="o/r")
        assert fake.creates() == []

    def test_empty_gh_stdout_is_partial_creation_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import subprocess

        def silent_ok(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr("kew.graph.plan.run", silent_ok)
        m = EpicPlanManifest.model_validate(_manifest([_issue("a")]))
        with pytest.raises(PartialCreationError):
            create_issues(m, epic_name="demo", repo="o/r")


class TestEnsureLabels:
    def test_creates_epic_and_taxonomy_labels(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeGh()
        monkeypatch.setattr("kew.graph.plan.run", fake)
        m = EpicPlanManifest.model_validate(
            _manifest(
                [
                    _issue("a", priority="p1-high", type="test"),
                    _issue("b", ["a"], complexity="complex", type="docs"),
                ]
            )
        )
        ensure_labels(m, epic_name="demo", repo="o/r")
        created = [c[3] for c in fake.calls if c[:3] == ["gh", "label", "create"]]
        # epic label plus every label the manifest will apply — the repo may
        # not have the type taxonomy at all (kew setup doesn't create it)
        assert "epic:demo" in created
        for label in ("p1-high", "p2-medium", "simple", "complex", "test", "docs"):
            assert label in created

    def test_existing_label_tolerated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import subprocess

        def exists(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                cmd, 1, stdout="", stderr="label with name already exists"
            )

        monkeypatch.setattr("kew.graph.plan.run", exists)
        m = EpicPlanManifest.model_validate(_manifest([_issue("a")]))
        ensure_labels(m, epic_name="demo", repo="o/r")  # must not raise

    def test_failure_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import subprocess

        def fail(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="denied")

        monkeypatch.setattr("kew.graph.plan.run", fail)
        m = EpicPlanManifest.model_validate(_manifest([_issue("a")]))
        with pytest.raises(RuntimeError, match="denied"):
            ensure_labels(m, epic_name="demo", repo="o/r")


class TestEpicPlanPrompt:
    def test_prompt_contains_contract(self) -> None:
        config = DispatchConfig()
        prompt = build_epic_plan_prompt(
            epic_name="demo", plan_path="docs/design/x.md", config=config
        )
        assert "docs/design/x.md" in prompt
        assert MANIFEST_RELPATH in prompt
        assert "depends_on" in prompt
        # agent must NOT do the guardrailed things itself
        assert "Do NOT create GitHub issues" in prompt
        assert "kew:depends" in prompt  # told not to hand-write markers
        assert "p0-critical" in prompt and "p3-low" in prompt  # enum surface


# --- GitHub-native linkage (W2.8b write side) -------------------------------

_DB = 9000  # database id offset: issue #N has database id 9000+N


def _ok(cmd: list[str], stdout: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")


class FakeRepo:
    """One repository, stateful across both seams the write side touches.

    `gh` answers `plan.py` (issue creation and the two REST relation
    endpoints); `graphql` answers the native adapter reading the same
    repository back. The amend criteria are about what a *later* read sees,
    not only about which calls were made, so the fake has to remember.
    """

    def __init__(
        self,
        *,
        labelled: list[int] | None = None,
        parent_of: dict[int, int] | None = None,
        parent_bodies: dict[int, str] | None = None,
        parent_repositories: dict[int, str] | None = None,
        available: bool = True,
        refuse: str = "",
    ) -> None:
        self.calls: list[list[str]] = []
        self.labelled: list[int] = list(labelled or [])
        self.available = available
        self.refuse = refuse
        self.parent_of: dict[int, int] = dict(parent_of or {})
        self.bodies: dict[int, str] = dict(parent_bodies or {})
        self.parent_repositories: dict[int, str] = dict(parent_repositories or {})
        self.sub_issues: list[tuple[int, int]] = [
            (parent, child) for child, parent in self.parent_of.items()
        ]
        self.blocked_by: list[tuple[int, int]] = []
        self._next = 100

    # -- the `gh` seam (kew.graph.plan.run) ----------------------------

    def gh(self, cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(cmd)
        if cmd[:3] == ["gh", "issue", "create"]:
            return self._create(cmd)
        if cmd[:3] == ["gh", "issue", "list"]:
            return _ok(cmd, json.dumps([self._rest_node(n) for n in self.labelled]))
        if cmd[:2] == ["gh", "api"]:
            return self._api(cmd)
        return _ok(cmd, "")

    def _create(self, cmd: list[str]) -> subprocess.CompletedProcess[str]:
        self._next += 1
        self.bodies[self._next] = cmd[cmd.index("--body") + 1]
        # Only a node carries the membership label; the container must not.
        if "epic:demo" in cmd:
            self.labelled.append(self._next)
        return _ok(cmd, f"https://github.com/o/r/issues/{self._next}\n")

    def _api(self, cmd: list[str]) -> subprocess.CompletedProcess[str]:
        path = next(a for a in cmd if a.startswith("repos/"))
        if self.refuse and self.refuse in path:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="no access")
        number = int(path.split("/issues/")[1].split("/")[0])
        if "--method" not in cmd:  # the database-id read
            return _ok(cmd, json.dumps({"id": _DB + number}))
        other = int(cmd[-1].split("=")[1]) - _DB
        if path.endswith("sub_issues"):
            self.sub_issues.append((number, other))
            self.parent_of[other] = number
        else:
            self.blocked_by.append((number, other))
        return _ok(cmd, "{}")

    # -- the `gh api graphql` seam (kew.graph.adapters.github_native) ---

    def graphql(self, cmd: list[str], **kwargs: Any) -> Any:
        self.calls.append(cmd)
        query = next((a[6:] for a in cmd if a.startswith("query=")), "")
        if "blockedBy(first: 1)" in query:
            return _ok(cmd, json.dumps(self._probe()))
        if "subIssues(first: 100" in query:
            parent = int(next(a for a in cmd if a.startswith("number=")).split("=")[1])
            children = [c for p, c in self.sub_issues if p == parent]
            return _ok(cmd, self._connection("issue", "subIssues", children))
        return _ok(cmd, self._connection("issues", None, self.labelled))

    def _probe(self) -> dict[str, Any]:
        if self.available:
            return {
                "data": {
                    "repository": {
                        "issue": {
                            "blockedBy": {"totalCount": 0},
                            "subIssues": {"totalCount": 0},
                        }
                    }
                }
            }
        return {
            "errors": [
                {
                    "message": "Field 'subIssues' doesn't exist on type 'Issue'",
                    "extensions": {"code": "undefinedField", "fieldName": "subIssues"},
                }
            ]
        }

    def _connection(self, key: str, inner: str | None, numbers: list[int]) -> str:
        page = {
            "nodes": [self._node(n) for n in numbers],
            "pageInfo": {"hasNextPage": False},
        }
        conn: dict[str, Any] = {inner: page} if inner else page
        return json.dumps({"data": {"repository": {key: conn}}})

    def _rest_node(self, number: int) -> dict[str, Any]:
        return {
            "number": number,
            "title": f"node {number}",
            "url": f"https://github.com/o/r/issues/{number}",
            "state": "OPEN",
            "stateReason": None,
            "body": self.bodies.get(number, ""),
            "labels": [{"name": "epic:demo"}],
        }

    def _node(self, number: int) -> dict[str, Any]:
        parent = self.parent_of.get(number)
        return {
            "number": number,
            "title": f"node {number}",
            "url": f"https://github.com/o/r/issues/{number}",
            "state": "OPEN",
            "stateReason": None,
            "body": self.bodies.get(number, ""),
            "repository": {"nameWithOwner": "o/r"},
            "labels": {"nodes": [{"name": "epic:demo"}]},
            "blockedBy": {"nodes": []},
            "closedByPullRequestsReferences": {"nodes": []},
            "parent": (
                {
                    "number": parent,
                    "body": self.bodies.get(parent, ""),
                    "repository": {
                        "nameWithOwner": self.parent_repositories.get(parent, "o/r")
                    },
                }
                if parent is not None
                else None
            ),
            "subIssues": {
                "totalCount": len([c for p, c in self.sub_issues if p == number])
            },
        }

    # -- assertions helpers --------------------------------------------

    def creates(self) -> list[list[str]]:
        return [c for c in self.calls if c[:3] == ["gh", "issue", "create"]]

    def children_of(self, parent: int) -> list[int]:
        return [c for p, c in self.sub_issues if p == parent]


class TestNativeRelations:
    def _wire(self, monkeypatch: pytest.MonkeyPatch, repo: FakeRepo) -> None:
        reset_linkage_probe_cache()
        monkeypatch.setattr("kew.graph.plan.run", repo.gh)
        monkeypatch.setattr(
            "kew.graph.adapters.github_native._default_run", repo.graphql
        )

    def _created(self, deps: bool = True) -> list[CreatedIssue]:
        issues = [_issue("a"), _issue("b", ["a"])] if deps else [_issue("a")]
        m = EpicPlanManifest.model_validate(_manifest(issues))
        return create_issues(m, epic_name="demo", repo="o/r")

    def test_typed_parent_failure_warns_and_creates_untyped(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from kew.graph import plan

        calls: list[list[str]] = []

        def create(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            calls.append(cmd)
            if "--type" in cmd:
                return subprocess.CompletedProcess(
                    cmd, 1, stdout="", stderr="unknown flag: --type\n"
                )
            return _ok(cmd, "https://github.com/o/r/issues/101\n")

        monkeypatch.setattr(plan, "run", create)

        number = plan._create_epic_parent(
            epic_name="demo", repo="o/r", plan_path="docs/design/x.md"
        )

        captured = capsys.readouterr().err
        assert number == 101
        assert len(calls) == 2
        assert captured.count("created untyped") == 1
        assert "unknown flag: --type" in captured

    def test_typed_parent_error_is_bounded_and_sanitized(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from kew.graph import plan

        def create(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            if "--type" in cmd:
                error = "secret=TOPSECRET " + ("x" * 1000) + "\nsecret=SECOND"
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr=error)
            return _ok(cmd, "https://github.com/o/r/issues/101\n")

        monkeypatch.setattr(plan, "run", create)

        number = plan._create_epic_parent(
            epic_name="demo", repo="o/r", plan_path="docs/design/x.md"
        )

        warning = capsys.readouterr().err.strip()
        assert number == 101
        assert "created untyped" in warning
        assert "secret=<redacted>" in warning
        assert "TOPSECRET" not in warning
        assert "SECOND" not in warning
        assert len(warning) <= 350

    def test_both_parent_create_failures_name_both_sanitized_errors(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from kew.graph import plan

        def create(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            error = (
                "typed denied secret=TYPED_SECRET"
                if "--type" in cmd
                else "untyped denied token=UNTYPED_SECRET"
            )
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr=error)

        monkeypatch.setattr(plan, "run", create)

        number = plan._create_epic_parent(
            epic_name="demo", repo="o/r", plan_path="docs/design/x.md"
        )

        warning = capsys.readouterr().err
        assert number is None
        assert "typed create error: typed denied secret=<redacted>" in warning
        assert "untyped create error: untyped denied token=<redacted>" in warning
        assert "TYPED_SECRET" not in warning
        assert "UNTYPED_SECRET" not in warning

    def test_plan_writes_membership_and_edges_natively(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = FakeRepo()
        self._wire(monkeypatch, repo)
        created = self._created()
        # The marker stays for one release: both keys describe the same edge.
        body_b = repo.creates()[1][repo.creates()[1].index("--body") + 1]
        assert "<!-- kew:depends 101 -->" in body_b

        assert link_native_relations(created, epic_name="demo", repo="o/r") == []

        parent_create = repo.creates()[-1]
        assert "--type" in parent_create and "Epic" in parent_create
        assert parent_create[parent_create.index("--title") + 1] == "epic: demo"
        parent_body = parent_create[parent_create.index("--body") + 1]
        assert format_epic_container_marker("demo") in parent_body
        assert "reserved kew metadata" in parent_body
        assert repo.children_of(103) == [101, 102]
        assert repo.blocked_by == [(102, 101)]  # b depends on a

    def test_the_parent_is_never_a_dispatchable_node(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A convention read (probe failure, GHES, older kew) must not list
        the container as a TODO node."""
        repo = FakeRepo()
        self._wire(monkeypatch, repo)
        created = self._created()

        assert link_native_relations(created, epic_name="demo", repo="o/r") == []

        parent_create = repo.creates()[-1]
        assert "epic:demo" not in parent_create
        repo.labelled.append(103)  # a human adds the membership label later
        convention = GitHubConventionSource(repo="o/r", run_fn=repo.gh)
        assert [n.id for n in convention.list_epic_nodes("demo")] == ["101", "102"]

    def test_amend_of_a_label_only_epic_adopts_every_existing_node(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = FakeRepo(labelled=[50, 51])
        self._wire(monkeypatch, repo)
        created = self._created(deps=False)

        warnings = link_native_relations(
            created, epic_name="demo", repo="o/r", existing_ids={"50", "51"}
        )

        assert warnings == []
        # Every pre-existing node joins the new parent, and before the new one.
        assert repo.children_of(102) == [50, 51, 101]
        source = GitHubNativeSource(repo="o/r", run=repo.graphql)
        assert [n.id for n in source.list_epic_nodes("demo")] == ["50", "51", "101"]
        assert source.membership_key == "sub-issues of #102"

    def test_amend_with_an_existing_parent_adds_only_the_new_nodes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = FakeRepo(
            labelled=[50, 51],
            parent_of={50: 7, 51: 7},
            parent_bodies={7: format_epic_container_marker("demo")},
        )
        self._wire(monkeypatch, repo)
        created = self._created(deps=False)
        before = len(repo.creates())

        assert (
            link_native_relations(
                created, epic_name="demo", repo="o/r", existing_ids={"50", "51"}
            )
            == []
        )
        assert len(repo.creates()) == before  # no second parent issue
        assert repo.children_of(7) == [50, 51, 101]
        assert repo.sub_issues[-1] == (7, 101)  # only the new node was added

    def test_amend_ignores_an_unrelated_parent_and_creates_its_own(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = FakeRepo(labelled=[50], parent_of={50: 7})
        self._wire(monkeypatch, repo)
        created = self._created(deps=False)

        warnings = link_native_relations(
            created, epic_name="demo", repo="o/r", existing_ids={"50"}
        )

        assert warnings == []
        assert repo.creates()[-1][repo.creates()[-1].index("--title") + 1] == (
            "epic: demo"
        )
        assert repo.children_of(102) == [50, 101]

    def test_an_api_without_the_relations_skips_loudly(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = FakeRepo(available=False)
        self._wire(monkeypatch, repo)
        created = self._created()

        warnings = link_native_relations(created, epic_name="demo", repo="o/r")
        assert len(warnings) == 1
        assert "epic:demo label" in warnings[0]
        assert "kew:depends" in warnings[0]
        assert repo.sub_issues == []

    def test_a_refused_relation_is_reported_not_raised(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = FakeRepo(refuse="dependencies/blocked_by")
        self._wire(monkeypatch, repo)
        created = self._created()

        warnings = link_native_relations(created, epic_name="demo", repo="o/r")
        assert len(warnings) == 1
        assert "blocked_by #101" in warnings[0]
        assert "kew:depends marker" in warnings[0]
        # Membership still got written; only the refused edge is reported.
        assert repo.children_of(103) == [101, 102]

    def test_an_unusable_gh_is_reported_not_raised(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The issues already exist; a linkage failure must not abort the plan."""
        repo = FakeRepo()
        self._wire(monkeypatch, repo)
        created = self._created()

        def unusable(cmd: list[str], **kwargs: Any) -> Any:
            raise ValueError("Unknown executable 'gh' — not in allowlist: []")

        monkeypatch.setattr("kew.graph.adapters.github_native._default_run", unusable)
        warnings = link_native_relations(created, epic_name="demo", repo="o/r")

        assert len(warnings) == 1
        assert "not in allowlist" in warnings[0]
        assert repo.sub_issues == []

    def test_an_ambiguous_epic_parent_is_reported_not_raised(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = FakeRepo(
            labelled=[50, 51],
            parent_of={50: 7, 51: 8},
            parent_bodies={
                7: format_epic_container_marker("demo"),
                8: format_epic_container_marker("demo"),
            },
        )
        self._wire(monkeypatch, repo)
        created = self._created(deps=False)

        warnings = link_native_relations(created, epic_name="demo", repo="o/r")
        assert len(warnings) == 1
        assert "Ambiguous epic ref" in warnings[0]
        assert repo.sub_issues == [(7, 50), (8, 51)]
