"""LinearSource: DependencySource over Linear Projects + native blocks relations.

Spec: docs/superpowers/specs/2026-07-01-linear-surface-design.md@4a387c0b §Epics+graph.
Contract mirrors GitHubConventionSource: provider-raw statuses only (never
READY — the driver frontier needs raw TODO), all six workflow-state types
mapped, includeArchived on every query, canceled never satisfies dependents.

Relation fetching is a SEPARATE aliased by-UUID batch, not nested under the
project query: the live API (dogfood 2026-07-01) returns EMPTY relations from
the project-nested and identifier-shorthand projections even when the relation
exists — only `issue(id: <UUID>)` resolves relations reliably.
"""

import json

import pytest

from kew.graph.adapters.linear_native import LinearSource, is_linear_epic_admitted
from kew.graph.build import build_graph
from kew.graph.domain import NodeStatus
from kew.graph.export import render
from kew.graph.validate import validate
from kew.linear.client import LinearApiError, LinearClient


def _state(name, stype):
    return {"name": name, "type": stype}


def test_linear_epic_ownership_requires_project_and_both_admission_markers():
    state = _state("Todo", "unstarted")
    labels = {"kew", "kew:ready"}
    assert is_linear_epic_admitted(state, labels, project_id="project-1")
    assert not is_linear_epic_admitted(state, labels, project_id=None)
    assert not is_linear_epic_admitted(state, {"kew:ready"}, project_id="project-1")
    assert not is_linear_epic_admitted(
        _state("In Progress", "started"), labels, project_id="project-1"
    )


def _uuid(identifier):
    # Shaped like a Linear UUID so the adapter's embed-validation accepts it.
    return f"00000000-0000-0000-0000-{abs(hash(identifier)) % 10**12:012d}"


def _issue(identifier, title, state, labels=()):
    return {
        "id": _uuid(identifier),
        "identifier": identifier,
        "title": title,
        "url": f"https://linear.app/kew/issue/{identifier}",
        "state": state,
        "labels": {"nodes": [{"name": name} for name in labels]},
    }


def _rels(issues, rels=None):
    """Aliased relations-batch response for `issues` (in page order).

    rels: {identifier: (blocks, blocked_by)} — issues absent default to none.
    """
    rels = rels or {}
    data = {}
    for i, it in enumerate(issues):
        blocks, blocked_by = rels.get(it["identifier"], ((), ()))
        data[f"i{i}"] = {
            "identifier": it["identifier"],
            "relations": {
                "nodes": [
                    {"type": "blocks", "relatedIssue": {"identifier": b}}
                    for b in blocks
                ]
            },
            "inverseRelations": {
                "nodes": [
                    {"type": "blocks", "issue": {"identifier": b}} for b in blocked_by
                ]
            },
        }
    return {"data": data}


class ScriptedTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, url, payload, headers):
        self.requests.append(json.loads(payload))
        return 200, json.dumps(self.responses.pop(0)).encode()


def _source(responses, **kw):
    t = ScriptedTransport(responses)
    client = LinearClient("k", transport=t, sleep=lambda _s: None)
    return LinearSource(client, **kw), t


def _project_lookup(projects):
    return {"data": {"projects": {"nodes": projects}}}


def _issues_page(issues, has_next=False, cursor=None):
    return {
        "data": {
            "project": {
                "issues": {
                    "nodes": issues,
                    "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                }
            }
        }
    }


PROJ = [{"id": "prj-uuid-1", "name": "payments"}]


class TestListEpicNodes:
    def test_nodes_from_project_with_status_mapping(self):
        issues = [
            _issue("ENG-1", "done one", _state("Done", "completed")),
            _issue(
                "ENG-2",
                "todo one",
                _state("Todo", "unstarted"),
                labels=("kew", "kew:ready"),
            ),
            _issue("ENG-3", "reviewing", _state("In Review", "started")),
            _issue("ENG-4", "working", _state("In Progress", "started")),
            _issue("ENG-5", "backlog", _state("Backlog", "backlog")),
            _issue("ENG-6", "triaging", _state("Triage", "triage")),
            _issue("ENG-7", "nope", _state("Canceled", "canceled")),
        ]
        src, _ = _source([_project_lookup(PROJ), _issues_page(issues), _rels(issues)])
        nodes = {n.id: n for n in src.list_epic_nodes("payments")}
        assert nodes["ENG-1"].status is NodeStatus.DONE
        assert nodes["ENG-2"].status is NodeStatus.TODO
        assert nodes["ENG-3"].status is NodeStatus.IN_REVIEW
        # Started-but-not-review is known work but not a dispatchable Todo;
        # IN_PROGRESS remains kew-run-derived in the driver overlay.
        assert nodes["ENG-4"].status is NodeStatus.QUEUED
        assert nodes["ENG-5"].status is NodeStatus.QUEUED
        assert nodes["ENG-6"].status is NodeStatus.QUEUED
        # Canceled must NEVER satisfy dependents (adversarial graph-M6):
        # mapped BLOCKED (fails closed) until a first-class CANCELLED exists.
        assert nodes["ENG-7"].status is NodeStatus.BLOCKED
        assert nodes["ENG-2"].epic_ref == "payments"
        assert nodes["ENG-2"].url.endswith("ENG-2")

    def test_linear_admission_requires_membership_and_ready_markers(self):
        issues = [
            _issue(
                "ENG-1",
                "backlog",
                _state("Backlog", "backlog"),
                labels=("kew", "kew:ready"),
            ),
            _issue(
                "ENG-2",
                "triage",
                _state("Triage", "triage"),
                labels=("kew", "kew:ready"),
            ),
            _issue("ENG-3", "unarmed", _state("Todo", "unstarted"), labels=("kew",)),
            _issue(
                "ENG-4",
                "armed",
                _state("Todo", "unstarted"),
                labels=("kew", "kew:ready"),
            ),
            _issue(
                "ENG-5",
                "missing membership",
                _state("Todo", "unstarted"),
                labels=("kew:ready",),
            ),
        ]
        src, _ = _source([_project_lookup(PROJ), _issues_page(issues), _rels(issues)])

        statuses = {node.id: node.status for node in src.list_epic_nodes("payments")}

        assert statuses == {
            "ENG-1": NodeStatus.QUEUED,
            "ENG-2": NodeStatus.QUEUED,
            "ENG-3": NodeStatus.QUEUED,
            "ENG-4": NodeStatus.TODO,
            "ENG-5": NodeStatus.QUEUED,
        }

    def test_clearing_ready_withdraws_admission(self):
        armed = _issue(
            "ENG-1",
            "armed",
            _state("Todo", "unstarted"),
            labels=("kew", "kew:ready"),
        )
        unarmed = _issue(
            "ENG-1", "unarmed", _state("Todo", "unstarted"), labels=("kew",)
        )
        src, _ = _source(
            [
                _project_lookup(PROJ),
                _issues_page([armed]),
                _rels([armed]),
                _project_lookup(PROJ),
                _issues_page([unarmed]),
                _rels([unarmed]),
            ]
        )

        assert src.list_epic_nodes("payments")[0].status is NodeStatus.TODO
        assert src.list_epic_nodes("payments")[0].status is NodeStatus.QUEUED

    def test_linear_api_failure_never_produces_admitted_nodes(self):
        src, _ = _source([{"errors": [{"message": "linear unavailable"}]}])

        with pytest.raises(LinearApiError, match="linear unavailable"):
            src.list_epic_nodes("payments")

    def test_adapter_never_emits_ready(self):
        # READY is graph-derived; an adapter emitting it empties the driver
        # frontier (adversarial graph-M8).
        issues = [_issue("ENG-2", "t", _state("Todo", "unstarted"))]
        src, _ = _source([_project_lookup(PROJ), _issues_page(issues), _rels(issues)])
        assert all(
            n.status is not NodeStatus.READY for n in src.list_epic_nodes("payments")
        )

    def test_queries_include_archived(self):
        # Linear auto-archives completed issues; without includeArchived a
        # mature project's satisfied deps read as dangling (adversarial API-F3).
        src, t = _source([_project_lookup(PROJ), _issues_page([])])
        assert src.list_epic_nodes("payments") == []  # empty guard is the CLI's
        assert all("includeArchived: true" in r["query"] for r in t.requests)

    def test_pagination_follows_cursor(self):
        page1 = [_issue("ENG-1", "a", _state("Todo", "unstarted"))]
        page2 = [_issue("ENG-2", "b", _state("Todo", "unstarted"))]
        src, t = _source(
            [
                _project_lookup(PROJ),
                _issues_page(page1, has_next=True, cursor="c1"),
                _issues_page(page2),
                _rels(page1 + page2),
            ]
        )
        ids = [n.id for n in src.list_epic_nodes("payments")]
        assert ids == ["ENG-1", "ENG-2"]
        assert t.requests[2]["variables"]["after"] == "c1"

    def test_parent_containers_are_excluded_but_retained_as_metadata(self):
        parent = _issue("SFD-184", "container", _state("Todo", "unstarted"))
        parent["children"] = {
            "nodes": [{"identifier": f"SFD-{n}"} for n in range(50)],
            "pageInfo": {"hasNextPage": True, "endCursor": "children-next"},
        }
        leaf = _issue("SFD-185", "leaf", _state("Todo", "unstarted"))
        src, _ = _source(
            [
                _project_lookup(PROJ),
                _issues_page([parent, leaf], has_next=True, cursor="project-next"),
                _issues_page([_issue("SFD-186", "later", _state("Todo", "unstarted"))]),
                _rels([leaf, _issue("SFD-186", "later", _state("Todo", "unstarted"))]),
            ]
        )

        nodes = src.list_epic_nodes("payments")

        assert [node.id for node in nodes] == ["SFD-185", "SFD-186"]
        container = src.excluded_containers["SFD-184"]
        assert container.title == "container"
        assert container.child_ids == tuple(f"SFD-{n}" for n in range(50))
        assert container.children_truncated is True

    def test_filtered_container_does_not_become_dangling_or_frontier(self):
        parent = _issue("SFD-184", "container", _state("Todo", "unstarted"))
        parent["children"] = {
            "nodes": [{"identifier": "SFD-185"}],
            "pageInfo": {"hasNextPage": False, "endCursor": None},
        }
        blocker = _issue("SFD-186", "blocker", _state("Done", "completed"))
        child = _issue("SFD-185", "child", _state("Todo", "unstarted"))
        disconnected = _issue("SFD-187", "disconnected", _state("Todo", "unstarted"))
        relations = {
            "SFD-185": ((), ("SFD-184", "SFD-186", "EXT-1")),
            "SFD-186": (("SFD-185",), ()),
        }
        src, _ = _source(
            [
                _project_lookup(PROJ),
                _issues_page([parent, blocker, child, disconnected]),
                _rels([blocker, child, disconnected], relations),
                {
                    "data": {
                        "issue": {
                            "identifier": "EXT-1",
                            "state": _state("Done", "completed"),
                        }
                    }
                },
            ]
        )

        graph = build_graph(src, "payments")
        report = validate(graph)

        assert "SFD-184" not in graph.nodes
        assert "SFD-184" not in render(graph, "mermaid")
        assert [(edge.src, edge.dst) for edge in graph.edges] == [
            ("SFD-186", "SFD-185"),
            ("EXT-1", "SFD-185"),
        ]
        assert graph.nodes["EXT-1"].external is True
        assert report.dangling == []
        assert report.orphans == ["SFD-187"]

    def test_unknown_project_raises(self):
        src, _ = _source([_project_lookup([])])
        with pytest.raises(RuntimeError, match="[Nn]o Linear project"):
            src.list_epic_nodes("ghost")

    def test_ambiguous_project_raises(self):
        src, _ = _source(
            [_project_lookup([{"id": "1", "name": "pay"}, {"id": "2", "name": "pay"}])]
        )
        with pytest.raises(RuntimeError, match="[Aa]mbiguous"):
            src.list_epic_nodes("pay")

    def test_custom_in_review_state_name(self):
        issues = [_issue("ENG-3", "r", _state("In QA", "started"))]
        src, _ = _source(
            [_project_lookup(PROJ), _issues_page(issues), _rels(issues)],
            in_review_state="In QA",
        )
        (node,) = src.list_epic_nodes("payments")
        assert node.status is NodeStatus.IN_REVIEW

    def test_relations_fetched_by_uuid_not_identifier(self):
        # The live API resolves relations ONLY on issue(id: <UUID>); the
        # identifier shorthand and project-nested projections return empty
        # (2026-07-01 dogfood). Guard: the batch query embeds UUIDs.
        issues = [_issue("ENG-1", "a", _state("Todo", "unstarted"))]
        src, t = _source([_project_lookup(PROJ), _issues_page(issues), _rels(issues)])
        src.list_epic_nodes("payments")
        batch_query = t.requests[2]["query"]
        assert _uuid("ENG-1") in batch_query
        assert '"ENG-1"' not in batch_query

    def test_malformed_uuid_rejected_before_embedding(self):
        bad = _issue("ENG-1", "a", _state("Todo", "unstarted"))
        bad["id"] = 'x") { } evil: __typename # '
        src, _ = _source([_project_lookup(PROJ), _issues_page([bad])])
        with pytest.raises(RuntimeError, match="[Uu]nexpected issue id"):
            src.list_epic_nodes("payments")


class TestListEdges:
    def _nodes_with_edges(self):
        issues = [
            _issue("ENG-1", "a", _state("Done", "completed")),
            _issue("ENG-2", "b", _state("Todo", "unstarted")),
            _issue("ENG-3", "c", _state("Todo", "unstarted")),
        ]
        # Realistic payload: the SAME "ENG-1 blocks ENG-2" relation appears on
        # BOTH endpoints (forward on ENG-1, inverse on ENG-2), as the live API
        # returns it; plus an inverse-only edge onto ENG-3.
        rels = {
            "ENG-1": (("ENG-2",), ()),
            "ENG-2": ((), ("ENG-1",)),
            "ENG-3": ((), ("ENG-2",)),
        }
        return _source(
            [_project_lookup(PROJ), _issues_page(issues), _rels(issues, rels)]
        )

    def test_edges_from_blocks_relations_both_directions(self):
        src, _ = self._nodes_with_edges()
        nodes = src.list_epic_nodes("payments")
        edges = src.list_edges(nodes)
        pairs = [(e.src, e.dst) for e in edges]
        # Edge(src=dependency, dst=dependent): blocker is the dependency.
        assert ("ENG-1", "ENG-2") in pairs
        assert ("ENG-2", "ENG-3") in pairs
        # The bidirectionally-reported pair must yield exactly ONE edge.
        assert len(pairs) == len(set(pairs)) == 2

    def test_non_blocks_relations_ignored(self):
        issues = [_issue("ENG-1", "a", _state("Todo", "unstarted"))]
        batch = _rels(issues)
        batch["data"]["i0"]["relations"]["nodes"].append(
            {"type": "related", "relatedIssue": {"identifier": "ENG-9"}}
        )
        src, _ = _source([_project_lookup(PROJ), _issues_page(issues), batch])
        nodes = src.list_epic_nodes("payments")
        assert src.list_edges(nodes) == []

    def test_list_edges_requires_prior_node_fetch(self):
        # Mirror the GitHub adapter's loud ordering contract.
        src, _ = _source([])
        from kew.graph.domain import Node

        stray = Node(id="ENG-1", title="", status=NodeStatus.TODO, epic_ref="payments")
        with pytest.raises(RuntimeError, match="list_epic_nodes"):
            src.list_edges([stray])


class TestStatusOf:
    def test_resolves_out_of_epic_id(self):
        src, _ = _source(
            [
                {
                    "data": {
                        "issue": {
                            "identifier": "OPS-9",
                            "state": _state("Done", "completed"),
                        }
                    }
                }
            ]
        )
        assert src.status_of("OPS-9") is NodeStatus.DONE

    def test_not_found_returns_none(self):
        src, _ = _source(
            [{"errors": [{"message": "Entity not found: Issue - could not find"}]}]
        )
        assert src.status_of("GHOST-1") is None

    def test_other_api_errors_propagate(self):
        # An auth/network failure must NOT read as "dangling dependency".
        src, _ = _source([{"errors": [{"message": "internal server error"}]}])
        with pytest.raises(Exception, match="internal server error"):
            src.status_of("ENG-1")


def test_query_page_bounds_stay_under_complexity_cap():
    """Nested-connection page sizes multiply GraphQL complexity (10k cap per
    query). 50 x (50+50) blew the cap on a real workspace — live dogfood
    2026-07-01. Guard all bounds, including the relations-batch template."""
    import re

    from kew.graph.adapters import linear_native

    text = linear_native._ISSUES_QUERY + linear_native._RELATIONS_FRAGMENT
    bounds = [int(n) for n in re.findall(r"first: (\d+)", text)]
    assert bounds, "expected explicit first: bounds on every connection"
    assert all(b <= 50 for b in bounds), bounds
    # batch_size x per-issue relation bounds must stay well under the cap
    assert linear_native._RELATIONS_BATCH_SIZE * 30 <= 3000


def _members(members):
    return {"data": {"users": {"nodes": members}}}


def test_resolve_member_id_returns_the_member_uuid():
    member_id = "11111111-2222-3333-4444-555555555555"
    src, t = _source([_members([{"id": member_id, "name": "kew"}])])
    assert src.resolve_member_id("kew") == member_id
    # The name is passed as a GraphQL variable, not string-interpolated.
    assert t.requests[0]["variables"] == {"name": "kew"}


def test_resolve_member_id_raises_when_no_member_matches():
    src, _ = _source([_members([])])
    with pytest.raises(RuntimeError, match="No Linear member named 'ghost'"):
        src.resolve_member_id("ghost")


def test_resolve_member_id_raises_on_ambiguous_match():
    src, _ = _source(
        [_members([{"id": "a", "name": "kew"}, {"id": "b", "name": "kew"}])]
    )
    with pytest.raises(RuntimeError, match="Ambiguous member name 'kew'"):
        src.resolve_member_id("kew")


def _labels(labels):
    return {"data": {"issueLabels": {"nodes": labels}}}


def test_resolve_label_id_returns_the_label_uuid():
    label_id = "label-0000-1111-2222-3333"
    src, t = _source([_labels([{"id": label_id, "name": "kew:ready"}])])
    assert src.resolve_label_id("kew:ready") == label_id
    # The name is a GraphQL variable, not string-interpolated.
    assert t.requests[0]["variables"] == {"name": "kew:ready"}


def test_resolve_label_id_returns_none_when_no_label_matches():
    src, _ = _source([_labels([])])
    assert src.resolve_label_id("ghost") is None


def test_resolve_label_id_returns_none_on_ambiguous_match():
    # Ambiguous -> None so the watcher falls back to the conservative filter
    # rather than crashing at startup on a duplicated label name across teams.
    src, _ = _source(
        [_labels([{"id": "a", "name": "kew:ready"}, {"id": "b", "name": "kew:ready"}])]
    )
    assert src.resolve_label_id("kew:ready") is None


def _delegated_page(identifiers, has_next=False, cursor=None):
    return {
        "data": {
            "issues": {
                "nodes": [{"identifier": identifier} for identifier in identifiers],
                "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
            }
        }
    }


def test_list_delegated_returns_canonical_identifiers_with_variables():
    member_id = "11111111-2222-3333-4444-555555555555"
    src, t = _source([_delegated_page(["SFD-1", "SFD-2"])])
    assert src.list_delegated(member_id, "kew:ready") == ["SFD-1", "SFD-2"]
    # assignee/label are GraphQL variables, never string-interpolated.
    assert t.requests[0]["variables"] == {
        "assigneeId": member_id,
        "label": "kew:ready",
        "after": None,
    }


def test_list_delegated_paginates():
    src, t = _source(
        [
            _delegated_page(["SFD-1"], has_next=True, cursor="CUR"),
            _delegated_page(["SFD-2"]),
        ]
    )
    assert src.list_delegated("m", "kew:ready") == ["SFD-1", "SFD-2"]
    assert t.requests[1]["variables"]["after"] == "CUR"


def test_list_delegated_returns_empty_when_nothing_delegated():
    src, _ = _source([_delegated_page([])])
    assert src.list_delegated("m", "kew:ready") == []
