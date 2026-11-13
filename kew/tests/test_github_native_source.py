"""GitHub-native linkage: sub-issue membership, blocked_by edges, the probe.

Every `gh api graphql` call is faked. The fake dispatches on the query text so
a test states which *key* it is exercising (sub-issues, the label fallback, the
capability probe) rather than the order the adapter happens to call them in.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

import pytest

from kew.dependencies import format_epic_container_marker
from kew.graph.adapters import make_github_source
from kew.graph.adapters.github_convention import GitHubConventionSource
from kew.graph.adapters.github_native import (
    GitHubNativeSource,
    probe_native_linkage,
)
from kew.graph.build import build_graph
from kew.graph.domain import NodeStatus
from kew.graph.driver.facts import GitHubFactsReader, prime_issue_snapshots_from
from kew.graph.driver.frontier import compute_frontier
from kew.graph.driver.tick import _invalid_graph_reason
from kew.graph.validate import validate


def _issue(
    number: int,
    *,
    state: str = "OPEN",
    state_reason: str | None = None,
    labels: list[str] | None = None,
    blocked_by: list[int | tuple[int, str]] | None = None,
    body: str = "",
    closing: list[dict[str, Any]] | None = None,
    parent: int | None = None,
    parent_body: str = "",
    parent_repository: str | None = None,
    sub_issues: int = 0,
    repository: str = "o/r",
) -> dict[str, Any]:
    blockers = [
        {
            "number": blocker[0] if isinstance(blocker, tuple) else blocker,
            "repository": {
                "nameWithOwner": blocker[1]
                if isinstance(blocker, tuple)
                else repository
            },
        }
        for blocker in blocked_by or []
    ]
    return {
        "number": number,
        "title": f"node {number}",
        "url": f"https://github.com/o/r/issues/{number}",
        "state": state,
        "stateReason": state_reason,
        "body": body,
        "labels": {"nodes": [{"name": n} for n in labels or []]},
        "repository": {"nameWithOwner": repository},
        "blockedBy": {"nodes": blockers},
        "closedByPullRequestsReferences": {"nodes": closing or []},
        "parent": (
            {
                "number": parent,
                "body": parent_body,
                "repository": {"nameWithOwner": parent_repository or repository},
            }
            if parent is not None
            else None
        ),
        "subIssues": {"totalCount": sub_issues},
    }


class FakeGh:
    """Answers `gh api graphql` from canned payloads, keyed by query text."""

    def __init__(
        self,
        *,
        fields: list[str] | None = None,
        sub_issues: list[dict[str, Any]] | None = None,
        labelled: list[dict[str, Any]] | None = None,
        probe_rc: int = 0,
    ) -> None:
        self.calls: list[list[str]] = []
        self._fields = ["number", "title", "subIssues", "blockedBy"]
        if fields is not None:
            self._fields = fields
        self._sub_issues = sub_issues or []
        self._labelled = labelled or []
        self._probe_rc = probe_rc

    @property
    def graphql_calls(self) -> list[list[str]]:
        return [c for c in self.calls if c[:3] == ["gh", "api", "graphql"]]

    def _payload(self, query: str) -> dict[str, Any]:
        if "blockedBy(first: 1)" in query:
            missing = [
                field
                for field in ("blockedBy", "subIssues")
                if field not in self._fields
            ]
            if missing:
                return {
                    "errors": [
                        {"message": f"Field '{field}' doesn't exist on type 'Issue'"}
                        for field in missing
                    ]
                }
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
        if "subIssues(first: 100" in query:
            return {
                "data": {
                    "repository": {
                        "issue": {
                            "subIssues": {
                                "nodes": self._sub_issues,
                                "pageInfo": {"hasNextPage": False},
                            }
                        }
                    }
                }
            }
        return {
            "data": {
                "repository": {
                    "issues": {
                        "nodes": self._labelled,
                        "pageInfo": {"hasNextPage": False},
                    }
                }
            }
        }

    def __call__(
        self, cmd: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(cmd)
        if cmd[:3] == ["gh", "issue", "list"]:
            issues = []
            for issue in self._labelled:
                copied = dict(issue)
                copied["labels"] = (issue.get("labels") or {}).get("nodes", [])
                issues.append(copied)
            return subprocess.CompletedProcess(cmd, 0, json.dumps(issues), "")
        query = next((a[6:] for a in cmd if a.startswith("query=")), "")
        if "blockedBy(first: 1)" in query and self._probe_rc != 0:
            return subprocess.CompletedProcess(cmd, self._probe_rc, "", "gh: 404")
        return subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps(self._payload(query)), stderr=""
        )


def _source(fake: FakeGh) -> GitHubNativeSource:
    return GitHubNativeSource(repo="o/r", run=fake)


class TestCapabilityProbe:
    def test_probe_reports_available_when_both_fields_exist(self) -> None:
        probe = probe_native_linkage("o/r", 11, run=FakeGh())
        assert probe.available
        assert "subIssues" in probe.describe()

    def test_probe_names_the_missing_field_on_an_older_api(self) -> None:
        fake = FakeGh(fields=["number", "title"])
        probe = probe_native_linkage("o/r", 11, run=fake)
        assert not probe.available
        assert "blockedBy" in probe.reason and "subIssues" in probe.reason
        # The fallback line says the key AND why, never just the key.
        assert "epic label + kew:depends marker" in probe.describe()
        assert probe.reason in probe.describe()

    def test_transport_failure_raises_instead_of_falling_back(self) -> None:
        with pytest.raises(RuntimeError, match="linkage probe failed"):
            probe_native_linkage("o/r", 11, run=FakeGh(probe_rc=1))

    @pytest.mark.parametrize(
        ("error", "match"),
        [
            (
                {
                    "type": "RATE_LIMITED",
                    "message": "Rate limit telemetry mentions blockedBy in prose",
                },
                "RATE_LIMITED",
            ),
            (
                {
                    "type": "INSUFFICIENT_SCOPES",
                    "message": "Token cannot read blockedBy for this repository",
                },
                "INSUFFICIENT_SCOPES",
            ),
            (
                {"type": "NOT_FOUND", "message": "Could not resolve to an Issue"},
                "NOT_FOUND",
            ),
        ],
    )
    def test_non_field_graphql_probe_errors_raise(
        self, error: dict[str, str], match: str
    ) -> None:
        def failing(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            payload = json.dumps({"errors": [error]})
            return subprocess.CompletedProcess(cmd, 0, payload, "")

        with pytest.raises(RuntimeError, match=match):
            probe_native_linkage("o/r", 11, run=failing)

    def test_the_real_undefined_field_shape_selects_fallback(self) -> None:
        # Verified live: GitHub returns no `type`; the signal is
        # extensions.code == undefinedField with extensions.fieldName.
        def missing(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            payload = json.dumps(
                {
                    "errors": [
                        {
                            "message": "unexpected wording",
                            "extensions": {
                                "code": "undefinedField",
                                "typeName": "Issue",
                                "fieldName": "subIssues",
                            },
                        }
                    ]
                }
            )
            return subprocess.CompletedProcess(cmd, 0, payload, "")

        probe = probe_native_linkage("o/r", 11, run=missing)
        assert not probe.available
        assert "subIssues" in probe.reason

    def test_a_fabricated_field_error_type_does_not_fall_back(self) -> None:
        # A `type: FIELD_ERROR` with an unrelated message is not a missing
        # linkage field; falling back on it would hide a real failure.
        def other(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            payload = json.dumps(
                {"errors": [{"type": "FIELD_ERROR", "message": "something else"}]}
            )
            return subprocess.CompletedProcess(cmd, 0, payload, "")

        with pytest.raises(RuntimeError, match="FIELD_ERROR"):
            probe_native_linkage("o/r", 11, run=other)

    def test_an_undefined_field_outside_the_linkage_pair_raises(self) -> None:
        # The probe query also selects totalCount; an undefinedField on it is
        # a real schema failure, not "this repo has no linkage fields".
        def other(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            payload = json.dumps(
                {
                    "errors": [
                        {
                            "message": "unexpected wording",
                            "extensions": {
                                "code": "undefinedField",
                                "typeName": "IssueConnection",
                                "fieldName": "totalCount",
                            },
                        }
                    ]
                }
            )
            return subprocess.CompletedProcess(cmd, 0, payload, "")

        with pytest.raises(RuntimeError, match="unexpected wording"):
            probe_native_linkage("o/r", 11, run=other)

    def test_a_relation_without_a_repository_field_is_local(self) -> None:
        # repository.nameWithOwner is non-null in GitHub's schema and _graphql
        # raises on errors[], so a missing field only happens in hand-built
        # payloads; treating it as local is the documented choice.
        fake = FakeGh(sub_issues=[_issue(11), _issue(12, blocked_by=[11])])
        for issue in fake._sub_issues:
            issue["repository"] = None
            for blocker in issue["blockedBy"]["nodes"]:
                blocker.pop("repository", None)
        source = _source(fake)
        nodes = source.list_epic_nodes("7")
        assert sorted(n.id for n in nodes) == ["11", "12"]
        assert [(e.src, e.dst) for e in source.list_edges(nodes)] == [("11", "12")]

    def test_repository_names_compare_case_insensitively(self) -> None:
        # GitHub repository names are case-insensitive; a hand-typed
        # project.repo must not turn every node into a cross-repo one.
        fake = FakeGh(
            sub_issues=[
                _issue(11, repository="O/R"),
                _issue(12, blocked_by=[11], repository="O/R"),
            ]
        )
        source = _source(fake)
        nodes = source.list_epic_nodes("7")
        assert sorted(n.id for n in nodes) == ["11", "12"]
        assert [(e.src, e.dst) for e in source.list_edges(nodes)] == [("11", "12")]

    def test_exact_github_missing_field_message_selects_fallback(self) -> None:
        def missing(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            payload = json.dumps(
                {
                    "errors": [
                        {"message": ("Field 'blockedBy' doesn't exist on type 'Issue'")}
                    ]
                }
            )
            return subprocess.CompletedProcess(cmd, 0, payload, "")

        probe = probe_native_linkage("o/r", 11, run=missing)
        assert not probe.available
        assert "blockedBy" in probe.reason

    def test_auth_failure_with_empty_stdout_raises(self) -> None:
        def auth(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(cmd, 1, "", "gh auth login")

        with pytest.raises(RuntimeError, match="gh auth login"):
            probe_native_linkage("o/r", 11, run=auth)

    def test_probe_that_cannot_run_at_all_raises(self) -> None:
        def boom(cmd: list[str], **kwargs: Any) -> Any:
            raise FileNotFoundError("gh not on PATH")

        with pytest.raises(FileNotFoundError, match="gh not on PATH"):
            probe_native_linkage("o/r", 11, run=boom)

    def test_probe_runs_once_per_driver_run(self) -> None:
        fake = FakeGh()
        probe_native_linkage("o/r", 11, run=fake)
        probe_native_linkage("o/r", 12, run=fake)
        assert len(fake.graphql_calls) == 1

    def test_30_node_cached_probe_numeric_ref_uses_1_call_name_ref_uses_2(
        self,
    ) -> None:
        probe_native_linkage("o/r", 7, run=FakeGh())
        issues = [_issue(number) for number in range(1, 31)]

        numeric_fake = FakeGh(sub_issues=issues)
        numeric = GitHubNativeSource(repo="o/r", run=numeric_fake, probe_on_read=True)
        assert len(numeric.list_epic_nodes("7")) == 30
        assert len(numeric_fake.calls) == 1

        name_fake = FakeGh(labelled=[_issue(7, sub_issues=30)], sub_issues=issues)
        named = GitHubNativeSource(repo="o/r", run=name_fake, probe_on_read=True)
        assert len(named.list_epic_nodes("demo")) == 30
        assert len(name_fake.calls) == 2

    def test_probe_uses_the_first_issue_not_schema_introspection(self) -> None:
        fake = FakeGh()
        probe_native_linkage("o/r", 42, run=fake)
        query = fake.graphql_calls[0][4]
        assert "issue(number: $number)" in query
        assert "blockedBy(first: 1)" in query
        assert "subIssues(first: 1)" in query
        assert "number=42" in fake.graphql_calls[0]
        assert "__type" not in query

    def test_factory_picks_native_when_the_probe_passes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeGh()
        monkeypatch.setattr("kew.graph.adapters.github_native._default_run", fake)
        source = make_github_source(repo="o/r")
        assert isinstance(source, GitHubNativeSource)
        source.list_epic_nodes("7")
        assert source.membership_key == "sub-issues of #7"

    def test_factory_falls_back_to_convention_without_the_fields(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeGh(fields=["number"], labelled=[_issue(11)])
        monkeypatch.setattr("kew.graph.adapters.github_native._default_run", fake)
        source = make_github_source(repo="o/r", epic_prefix="epic:")
        assert [node.id for node in source.list_epic_nodes("demo")] == ["11"]
        assert source.membership_key == "label epic:demo"

    def test_factory_native_label_fallback_excludes_a_marked_container(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeGh(
            labelled=[
                _issue(101, labels=["epic:demo"]),
                _issue(
                    103,
                    labels=["epic:demo"],
                    body=format_epic_container_marker("demo"),
                    sub_issues=0,
                ),
            ]
        )
        monkeypatch.setattr("kew.graph.adapters.github_native._default_run", fake)

        source = make_github_source(repo="o/r")

        assert isinstance(source, GitHubNativeSource)
        assert [node.id for node in source.list_epic_nodes("demo")] == ["101"]
        assert source.membership_key == "label epic:demo"


class TestMembership:
    def test_a_nested_container_is_not_a_sub_issue_node(self) -> None:
        # A container can itself be someone's sub-issue. The label fallback is
        # not the only read path, so the marker filter cannot live only there.
        fake = FakeGh(
            sub_issues=[
                _issue(11),
                _issue(12, body=format_epic_container_marker("other")),
            ]
        )
        source = _source(fake)

        assert [node.id for node in source.list_epic_nodes("7")] == ["11"]

    def test_a_container_marked_for_another_epic_is_not_this_epics_parent(
        self,
    ) -> None:
        # #103 is `other`'s container. Carrying this epic's label and having
        # children must not let it capture `demo`'s membership.
        fake = FakeGh(
            labelled=[
                _issue(101, labels=["epic:demo"]),
                _issue(
                    103,
                    labels=["epic:demo"],
                    body=format_epic_container_marker("other"),
                    sub_issues=2,
                ),
            ]
        )
        source = _source(fake)

        assert source.epic_parent("demo") is None

    def test_numeric_epic_ref_reads_the_parents_sub_issues(self) -> None:
        fake = FakeGh(sub_issues=[_issue(11), _issue(12)])
        source = _source(fake)
        nodes = source.list_epic_nodes("7")
        assert [n.id for n in nodes] == ["11", "12"]
        assert source.membership_key == "sub-issues of #7"

    def test_one_query_supplies_every_node_fact(self) -> None:
        fake = FakeGh(
            sub_issues=[
                _issue(
                    11,
                    state="CLOSED",
                    state_reason="COMPLETED",
                    labels=["epic:demo"],
                    blocked_by=[10],
                    closing=[{"number": 5, "url": "u", "state": "MERGED"}],
                )
            ]
        )
        source = _source(fake)
        nodes = source.list_epic_nodes("7")
        assert len(fake.graphql_calls) == 1
        assert nodes[0].status is NodeStatus.DONE
        snap = source.issue_snapshots[11]
        assert snap["state"] == "CLOSED"
        assert snap["stateReason"] == "COMPLETED"
        assert snap["labels"] == [{"name": "epic:demo"}]
        assert snap["repository"] == "o/r"
        assert snap["blockedBy"] == [{"number": 10, "repository": "o/r"}]
        assert snap["closedByPullRequestsReferences"][0]["number"] == 5

    def test_a_label_that_names_one_parent_resolves_to_its_sub_issues(self) -> None:
        fake = FakeGh(
            labelled=[_issue(7, sub_issues=3), _issue(11, parent=7)],
            sub_issues=[_issue(11, parent=7), _issue(12, parent=7)],
        )
        source = _source(fake)
        nodes = source.list_epic_nodes("demo")
        # The parent itself is not a node — membership is its sub-issues.
        assert [n.id for n in nodes] == ["11", "12"]
        assert source.membership_key == "sub-issues of #7"

    def test_a_label_with_no_parent_falls_back_to_the_label_query(self) -> None:
        fake = FakeGh(labelled=[_issue(11), _issue(12)])
        source = _source(fake)
        nodes = source.list_epic_nodes("demo")
        assert [n.id for n in nodes] == ["11", "12"]
        assert source.membership_key == "label epic:demo"

    def test_a_foreign_parent_raises_with_both_issue_refs(self) -> None:
        fake = FakeGh(
            labelled=[
                _issue(
                    11,
                    parent=7,
                    parent_body=format_epic_container_marker("demo"),
                    parent_repository="other/repo",
                )
            ]
        )

        with pytest.raises(RuntimeError, match=r"issue #11.*other/repo#7"):
            _source(fake).epic_parent("demo")

    def test_a_marked_local_parent_is_the_epic_parent(self) -> None:
        fake = FakeGh(
            labelled=[
                _issue(
                    11,
                    parent=7,
                    parent_body=format_epic_container_marker("demo"),
                )
            ]
        )

        assert _source(fake).epic_parent("demo") == 7

    def test_foreign_sub_issues_are_skipped_once_per_read(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        fake = FakeGh(sub_issues=[_issue(11), _issue(12, repository="other/repo")])
        nodes = _source(fake).list_epic_nodes("7")
        captured = capsys.readouterr()

        assert [node.id for node in nodes] == ["11"]
        assert captured.err.count("skipped 1 cross-repo sub-issues of #7") == 1

    def test_two_parents_are_ambiguous_and_raise(self) -> None:
        fake = FakeGh(labelled=[_issue(7, sub_issues=2), _issue(8, sub_issues=2)])
        with pytest.raises(RuntimeError, match="Ambiguous epic ref"):
            _source(fake).list_epic_nodes("demo")

    def test_a_gh_failure_is_an_error_not_an_empty_epic(self) -> None:
        def failing(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="rate limited")

        with pytest.raises(RuntimeError, match="rate limited"):
            GitHubNativeSource(repo="o/r", run=failing).list_epic_nodes("7")

    def test_graphql_errors_are_an_error_not_an_empty_epic(self) -> None:
        def erroring(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            body = json.dumps({"errors": [{"message": "Field 'blockedBy' missing"}]})
            return subprocess.CompletedProcess(cmd, 0, stdout=body, stderr="")

        with pytest.raises(RuntimeError, match="blockedBy"):
            GitHubNativeSource(repo="o/r", run=erroring).list_epic_nodes("7")

    def test_a_truncated_page_run_refuses_rather_than_reading_a_partial_epic(
        self,
    ) -> None:
        def always_more(
            cmd: list[str], **kwargs: Any
        ) -> subprocess.CompletedProcess[str]:
            body = json.dumps(
                {
                    "data": {
                        "repository": {
                            "issue": {
                                "subIssues": {
                                    "nodes": [_issue(11)],
                                    "pageInfo": {
                                        "hasNextPage": True,
                                        "endCursor": "c",
                                    },
                                }
                            }
                        }
                    }
                }
            )
            return subprocess.CompletedProcess(cmd, 0, stdout=body, stderr="")

        with pytest.raises(RuntimeError, match="exceeded"):
            GitHubNativeSource(repo="o/r", run=always_more).list_epic_nodes("7")

    def test_two_pages_of_sub_issues_are_joined_not_truncated(self) -> None:
        # 150 sub-issues over two pages: the second call carries the first
        # page's cursor and the epic has all 150 nodes. A read that silently
        # stopped at the 100-row page would still pass the truncation test.
        cursors: list[str | None] = []

        def two_pages(
            cmd: list[str], **kwargs: Any
        ) -> subprocess.CompletedProcess[str]:
            after = None
            for i, arg in enumerate(cmd):
                if arg == "-F" and cmd[i + 1].startswith("after="):
                    after = cmd[i + 1].removeprefix("after=")
                if arg == "-f" and cmd[i + 1].startswith("after="):
                    after = cmd[i + 1].removeprefix("after=")
            cursors.append(after)
            if after in (None, "", "null"):
                nodes = [_issue(n) for n in range(1, 101)]
                info = {"hasNextPage": True, "endCursor": "page-2"}
            else:
                nodes = [_issue(n) for n in range(101, 151)]
                info = {"hasNextPage": False, "endCursor": None}
            body = json.dumps(
                {
                    "data": {
                        "repository": {
                            "issue": {"subIssues": {"nodes": nodes, "pageInfo": info}}
                        }
                    }
                }
            )
            return subprocess.CompletedProcess(cmd, 0, stdout=body, stderr="")

        nodes = GitHubNativeSource(repo="o/r", run=two_pages).list_epic_nodes("7")
        assert len(nodes) == 150
        assert sorted(int(n.id) for n in nodes) == list(range(1, 151))
        assert len(cursors) == 2 and cursors[1] == "page-2"


class TestEdges:
    def test_blocked_by_relations_are_the_edges(self) -> None:
        fake = FakeGh(sub_issues=[_issue(11), _issue(12, blocked_by=[11])])
        source = _source(fake)
        edges = source.list_edges(source.list_epic_nodes("7"))
        assert [(e.src, e.dst) for e in edges] == [("11", "12")]

    def test_native_and_marker_edges_are_unioned_during_overlap(self) -> None:
        fake = FakeGh(
            sub_issues=[
                _issue(11),
                _issue(12),
                _issue(
                    13,
                    blocked_by=[11],
                    body="<!-- kew:depends 11 12 -->",
                ),
            ]
        )
        source = _source(fake)
        edges = source.list_edges(source.list_epic_nodes("7"))
        assert sorted((e.src, e.dst) for e in edges) == [("11", "13"), ("12", "13")]

    def test_a_relation_outside_the_epic_fails_closed(self) -> None:
        fake = FakeGh(sub_issues=[_issue(12, blocked_by=[900])])
        source = _source(fake)

        # An out-of-epic blocker that cannot be resolved stays an edge, so the
        # frontier refuses to dispatch the node rather than silently dropping
        # the dependency and calling it ready.
        def unresolvable(node_id: str) -> None:
            return None

        source.status_of = unresolvable  # type: ignore[method-assign]
        graph = build_graph(source, "7")
        assert [(e.src, e.dst) for e in graph.edges] == [("900", "12")]
        statuses = {nid: n.status for nid, n in graph.nodes.items()}
        assert compute_frontier(graph, status=statuses, excluded=set()) == []

    def test_a_foreign_blocker_is_named_and_fails_closed(self) -> None:
        fake = FakeGh(sub_issues=[_issue(13, blocked_by=[(12, "other/repo")])])
        graph = build_graph(_source(fake), "7")

        assert [(e.src, e.dst) for e in graph.edges] == [("other/repo#12", "13")]
        statuses = {nid: node.status for nid, node in graph.nodes.items()}
        assert compute_frontier(graph, status=statuses, excluded=set()) == []
        assert "other/repo#12" in _invalid_graph_reason(graph, validate(graph))

    def test_list_edges_before_list_epic_nodes_is_loud(self) -> None:
        fake = FakeGh(sub_issues=[_issue(11)])
        source = _source(fake)
        nodes = source.list_epic_nodes("7")
        other = _source(FakeGh())
        with pytest.raises(RuntimeError, match="no cached snapshot"):
            other.list_edges(nodes)

    def test_status_of_delegates_to_the_convention_read(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def view(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            assert cmd[:3] == ["gh", "issue", "view"]
            return subprocess.CompletedProcess(
                cmd, 0, stdout=json.dumps({"state": "CLOSED"}), stderr=""
            )

        source = GitHubNativeSource(repo="o/r", run=FakeGh())
        source._fallback.repo = "o/r"
        import kew.graph.adapters.github_convention as convention

        monkeypatch.setattr(convention, "run", view)
        assert source.status_of("900") is NodeStatus.DONE

    def test_second_read_after_empty_fallback_keeps_native_blocked_by_edges(
        self,
    ) -> None:
        fake = FakeGh(sub_issues=[_issue(12, blocked_by=[11])])
        source = GitHubNativeSource(repo="o/r", run=fake, probe_on_read=True)

        assert source.list_epic_nodes("missing") == []
        nodes = source.list_epic_nodes("7")
        assert [(edge.src, edge.dst) for edge in source.list_edges(nodes)] == [
            ("11", "12")
        ]


class TestSnapshotHandover:
    def test_the_snapshot_answers_the_first_issue_read_without_gh(self) -> None:
        fake = FakeGh(sub_issues=[_issue(11, labels=["ready"])])
        source = _source(fake)
        source.list_epic_nodes("7")

        def never(cmd: list[str], **kwargs: Any) -> Any:
            raise AssertionError(f"unexpected gh call: {cmd}")

        reader = GitHubFactsReader(repo="o/r", run=never, host="github.com")
        assert prime_issue_snapshots_from(source, reader) is True
        issue = reader.fetch_issue_or_none(11)
        assert issue is not None
        assert issue["state"] == "OPEN"
        assert issue["labels"] == [{"name": "ready"}]

    def test_a_second_read_of_the_same_issue_goes_live(self) -> None:
        fake = FakeGh(sub_issues=[_issue(11)])
        source = _source(fake)
        source.list_epic_nodes("7")
        seen: list[list[str]] = []

        def view(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            seen.append(cmd)
            return subprocess.CompletedProcess(
                cmd, 0, stdout=json.dumps({"state": "CLOSED"}), stderr=""
            )

        reader = GitHubFactsReader(repo="o/r", run=view, host="github.com")
        prime_issue_snapshots_from(source, reader)
        assert (reader.fetch_issue_or_none(11) or {})["state"] == "OPEN"
        # The merge pass re-reads after `gh issue close`; that read must be
        # live, never the snapshot taken before the close.
        assert (reader.fetch_issue_or_none(11) or {})["state"] == "CLOSED"
        assert len(seen) == 1

    def test_a_source_without_a_snapshot_hands_over_nothing(self) -> None:
        reader = GitHubFactsReader(repo="o/r", run=FakeGh(), host="github.com")
        convention = GitHubConventionSource(repo="o/r")
        assert prime_issue_snapshots_from(convention, reader) is False
