"""LinearTracker: thin Phase-C write vertical over LinearClient.

Mocked transport only — no live API (mirrors test_linear_client.py). Covers the
Group L1 surface: list_ready / claim / comment / close, workflow-state
resolution cached once per process, the ``config.tracker.type == "linear"``
gate, and key redaction on every error path.

Spec: docs/superpowers/specs/2026-07-01-linear-surface-design.md@4a387c0b §Phase C.
"""

import json

import pytest

from kew.config import DispatchConfig, LinearTrackerConfig, TrackerConfig
from kew.linear.client import LinearApiError, LinearClient
from kew.linear.tracker import LinearTracker

KEY = "fake-test-key-not-a-secret"  # deliberately low-entropy: gitleaks

# A team that runs two `started` states — the disambiguation the in_review_state
# pattern exists to handle (config.py:39-47).
STATES = [
    {"id": "st-backlog", "name": "Backlog", "type": "backlog"},
    {"id": "st-todo", "name": "Todo", "type": "unstarted"},
    {"id": "st-progress", "name": "In Progress", "type": "started"},
    {"id": "st-review", "name": "In Review", "type": "started"},
    {"id": "st-done", "name": "Done", "type": "completed"},
    {"id": "st-canceled", "name": "Canceled", "type": "canceled"},
]


class FakeLinear:
    """Routes each GraphQL operation to a canned response by content.

    Unlike the client tests' scripted-list transport, the tracker fires several
    operations per public call (states + viewer + mutation), so routing by query
    text keeps the tests order-independent. ``requests`` records every call for
    assertion; ``force`` injects a raw (status, body) to exercise error paths.
    """

    def __init__(self, states=STATES, issues=None, force=None, issue=None):
        self.states = states
        self.issues = issues if issues is not None else []
        self.force = force
        self.issue = issue
        self.requests = []

    def __call__(self, url, payload, headers):
        doc = json.loads(payload)
        query = doc["query"]
        self.requests.append(doc)
        if self.force is not None:
            return self.force
        if "teams(" in query:
            team = {"id": "team-uuid", "states": {"nodes": self.states}}
            return _ok({"teams": {"nodes": [team]}})
        if "viewer" in query:
            return _ok({"viewer": {"id": "viewer-uuid"}})
        if "IssueForDispatch" in query:
            return _ok({"issue": self.issue})
        if "issues(" in query:
            return _ok({"issues": {"nodes": self.issues}})
        if "issueUpdate" in query:
            return _ok(
                {
                    "issueUpdate": {
                        "success": True,
                        "issue": {
                            "identifier": doc["variables"]["id"],
                            "state": {"name": "In Progress", "type": "started"},
                            "assignee": {"id": "viewer-uuid"},
                        },
                    }
                }
            )
        if "commentCreate" in query:
            return _ok({"commentCreate": {"success": True, "comment": {"id": "c1"}}})
        raise AssertionError(f"unrouted query: {query}")


def _ok(data):
    return 200, json.dumps({"data": data}).encode()


def _config(*, type="linear", team="SFD", in_review_state="In Review"):
    return DispatchConfig(
        tracker=TrackerConfig(
            type=type,
            linear=LinearTrackerConfig(team=team, in_review_state=in_review_state),
        )
    )


def _tracker(transport, config=None):
    client = LinearClient(KEY, transport=transport, sleep=lambda _s: None)
    return LinearTracker(config or _config(), client)


def test_construction_gated_on_linear_tracker_type():
    t = FakeLinear()
    client = LinearClient(KEY, transport=t, sleep=lambda _s: None)
    with pytest.raises(ValueError) as exc:
        LinearTracker(_config(type="github"), client)
    assert "linear" in str(exc.value).lower()
    assert not t.requests  # gate fires before any network call


def test_list_ready_filters_by_resolved_unstarted_state():
    issues = [
        {"id": "u1", "identifier": "SFD-1", "title": "one"},
        {"id": "u2", "identifier": "SFD-2", "title": "two"},
    ]
    t = FakeLinear(issues=issues)
    tr = _tracker(t)
    ready = tr.list_ready()
    assert [i["identifier"] for i in ready] == ["SFD-1", "SFD-2"]
    # The issues query filters on the resolved ready (unstarted) state id.
    issue_req = next(d for d in t.requests if "issues(" in d["query"])
    assert issue_req["variables"]["stateId"] == "st-todo"
    assert issue_req["variables"]["key"] == "SFD"


def test_claim_assigns_viewer_and_transitions_to_started_not_review():
    t = FakeLinear()
    tr = _tracker(t)
    tr.claim("SFD-1")
    upd = next(d for d in t.requests if "issueUpdate" in d["query"])
    inp = upd["variables"]["input"]
    assert upd["variables"]["id"] == "SFD-1"
    assert inp["assigneeId"] == "viewer-uuid"
    # `In Progress`, never the `In Review` started state (disambiguation).
    assert inp["stateId"] == "st-progress"


def test_claim_disambiguation_honours_in_review_state_override():
    # If the team names its review state differently, the OTHER started state is
    # still the claim target — resolution is by name-within-type, not position.
    states = [
        {"id": "st-todo", "name": "Todo", "type": "unstarted"},
        {"id": "st-qa", "name": "QA", "type": "started"},
        {"id": "st-doing", "name": "Doing", "type": "started"},
        {"id": "st-done", "name": "Done", "type": "completed"},
    ]
    t = FakeLinear(states=states)
    tr = _tracker(t, _config(in_review_state="QA"))
    tr.claim("SFD-9")
    upd = next(d for d in t.requests if "issueUpdate" in d["query"])
    assert upd["variables"]["input"]["stateId"] == "st-doing"


def test_comment_posts_body_to_issue():
    t = FakeLinear()
    tr = _tracker(t)
    tr.comment("SFD-3", "ci is green")
    req = next(d for d in t.requests if "commentCreate" in d["query"])
    assert req["variables"]["input"] == {"issueId": "SFD-3", "body": "ci is green"}


def test_close_transitions_to_completed_state():
    t = FakeLinear()
    tr = _tracker(t)
    tr.close("SFD-4")
    upd = next(d for d in t.requests if "issueUpdate" in d["query"])
    assert upd["variables"]["id"] == "SFD-4"
    assert upd["variables"]["input"] == {"stateId": "st-done"}


def test_reset_to_ready_unassigns_and_transitions_to_unstarted():
    t = FakeLinear()
    tr = _tracker(t)
    tr.reset_to_ready("SFD-6")
    upd = next(d for d in t.requests if "issueUpdate" in d["query"])
    assert upd["variables"]["id"] == "SFD-6"
    # Inverse of claim: back to the ready (unstarted) state and unassigned.
    assert upd["variables"]["input"] == {"stateId": "st-todo", "assigneeId": None}


def test_workflow_states_resolved_once_per_process():
    t = FakeLinear()
    tr = _tracker(t)
    tr.claim("SFD-1")
    tr.close("SFD-1")
    tr.comment("SFD-1", "done")
    # States (teams) fetched once; viewer fetched once — cached across calls.
    assert sum("teams(" in d["query"] for d in t.requests) == 1
    assert sum("viewer" in d["query"] for d in t.requests) == 1


def test_missing_completed_state_raises_clearly():
    states = [
        {"id": "st-todo", "name": "Todo", "type": "unstarted"},
        {"id": "st-doing", "name": "Doing", "type": "started"},
    ]
    t = FakeLinear(states=states)
    tr = _tracker(t)
    with pytest.raises(LinearApiError) as exc:
        tr.close("SFD-5")
    assert "completed" in str(exc.value)


def test_unknown_team_raises():
    t = FakeLinear()
    t.states = []  # teams query returns no node

    def transport(url, payload, headers):
        doc = json.loads(payload)
        if "teams(" in doc["query"]:
            return _ok({"teams": {"nodes": []}})
        return t(url, payload, headers)

    tr = _tracker(transport)
    with pytest.raises(LinearApiError) as exc:
        tr.list_ready()
    assert "SFD" in str(exc.value)


def test_mutation_failure_raises_api_error():
    def transport(url, payload, headers):
        doc = json.loads(payload)
        if "issueUpdate" in doc["query"]:
            return _ok({"issueUpdate": {"success": False, "issue": None}})
        return FakeLinear()(url, payload, headers)

    tr = _tracker(transport)
    with pytest.raises(LinearApiError) as exc:
        tr.claim("SFD-6")
    assert "SFD-6" in str(exc.value)


def test_error_path_redacts_key():
    # A forced server error echoing the key must not surface it (delegates to
    # the client redactor — F-L6 parity for the write vertical).
    t = FakeLinear(force=(500, f"boom {KEY} boom".encode()))
    tr = _tracker(t)
    with pytest.raises(LinearApiError) as exc:
        tr.claim("SFD-7")
    assert KEY not in str(exc.value)


def test_fetch_issue_builds_dispatch_dict():
    # Shapes the item like ``select_next_issue``'s dict so ``execute_dispatch``
    # consumes both without a branch: ``number`` is the 0 sentinel (the durable
    # id travels beside it as ``item_id``, L3), and Linear labels map to the
    # ``[{"name": ...}]`` shape complexity routing reads.
    issue = {
        "identifier": "SFD-133",
        "title": "Wire the vertical",
        "description": "the seeded spec",
        "url": "https://linear.app/x/issue/SFD-133",
        "labels": {"nodes": [{"name": "complex"}, {"name": "backend"}]},
    }
    tr = _tracker(FakeLinear(issue=issue))
    result = tr.fetch_issue("SFD-133")
    assert result == {
        "number": 0,
        "title": "Wire the vertical",
        "body": "the seeded spec",
        "url": "https://linear.app/x/issue/SFD-133",
        "labels": [{"name": "complex"}, {"name": "backend"}],
    }


def test_fetch_issue_missing_labels_and_description_degrade():
    # An item with no labels / no description resolves to an empty list / empty
    # body — routing falls back to the default complexity, never crashes.
    issue = {
        "identifier": "SFD-9",
        "title": "T",
        "description": None,
        "url": "u",
        "labels": {"nodes": []},
    }
    result = _tracker(FakeLinear(issue=issue)).fetch_issue("SFD-9")
    assert result["body"] == ""
    assert result["labels"] == []


def test_fetch_issue_not_found_raises():
    with pytest.raises(LinearApiError) as exc:
        _tracker(FakeLinear(issue=None)).fetch_issue("SFD-404")
    assert "SFD-404" in str(exc.value)


# --- the no-change outcome adapter (I11) -------------------------------------
#
# Same contract as the GitHub dialect in kew.labels: one look that reports open
# vs closed and marked vs unmarked, and one write that is True only if Linear
# confirmed it. Every refusal comes back as a value — an outage must leave the
# outcome sync-pending, never raise into the reconciler.

MARKER = "<!-- kew-no-change:deadbeef -->"


def _outcome_transport(*, state="started", comments=(), fail=False):
    def transport(url, payload, headers):
        doc = json.loads(payload)
        if "IssueOutcome" in doc["query"]:
            if fail:
                return 500, b"upstream is unwell"
            return _ok(
                {
                    "issue": {
                        "identifier": doc["variables"]["id"],
                        "url": "https://linear.app/x/SFD-9",
                        "state": {"type": state},
                        "comments": {"nodes": [{"body": b} for b in comments]},
                    }
                }
            )
        return FakeLinear()(url, payload, headers)

    return transport


def test_observe_no_change_item_reports_an_open_unmarked_issue():
    tr = _tracker(_outcome_transport())
    seen = tr.observe_no_change_item("SFD-9", marker=MARKER)
    assert seen.available and seen.state == "open"
    assert not seen.marker_found
    assert seen.ref.endswith("SFD-9")


def test_observe_no_change_item_finds_this_judgments_marker():
    tr = _tracker(_outcome_transport(comments=("earlier note", "closed " + MARKER)))
    seen = tr.observe_no_change_item("SFD-9", marker=MARKER)
    assert seen.marker_found


def test_observe_no_change_item_reads_a_completed_state_as_closed():
    tr = _tracker(_outcome_transport(state="completed"))
    assert tr.observe_no_change_item("SFD-9", marker=MARKER).state == "closed"


def test_observe_no_change_item_reads_a_cancelled_state_as_closed():
    # A cancelled item is not one kew may claim it closed as a no-change.
    tr = _tracker(_outcome_transport(state="canceled"))
    assert tr.observe_no_change_item("SFD-9", marker=MARKER).state == "closed"


def test_observe_no_change_item_is_unavailable_on_an_api_error():
    tr = _tracker(_outcome_transport(fail=True))
    seen = tr.observe_no_change_item("SFD-9", marker=MARKER)
    assert not seen.available and seen.state == "unknown"


def test_observe_no_change_item_is_unavailable_for_a_missing_issue():
    def transport(url, payload, headers):
        doc = json.loads(payload)
        if "IssueOutcome" in doc["query"]:
            return _ok({"issue": None})
        return FakeLinear()(url, payload, headers)

    seen = _tracker(transport).observe_no_change_item("SFD-404", marker=MARKER)
    assert not seen.available


def test_observe_no_change_item_error_redacts_the_key():
    tr = _tracker(FakeLinear(force=(500, f"boom {KEY} boom".encode())))
    seen = tr.observe_no_change_item("SFD-9", marker=MARKER)
    assert not seen.available and KEY not in seen.detail


def test_apply_no_change_outcome_comments_then_closes():
    t = FakeLinear()
    tr = _tracker(t)
    assert tr.apply_no_change_outcome("SFD-9", body="note " + MARKER, close=True)
    comment = next(d for d in t.requests if "commentCreate" in d["query"])
    assert comment["variables"]["input"]["issueId"] == "SFD-9"
    assert MARKER in comment["variables"]["input"]["body"]
    update = next(d for d in t.requests if "issueUpdate" in d["query"])
    assert update["variables"]["input"] == {"stateId": "st-done"}


def test_apply_no_change_outcome_leaves_the_item_open_when_asked():
    t = FakeLinear()
    tr = _tracker(t)
    assert tr.apply_no_change_outcome("SFD-9", body="rejected", close=False)
    assert not [d for d in t.requests if "issueUpdate" in d["query"]]


def test_apply_no_change_outcome_closes_without_re_noting():
    # A resumed pass finishing a closure whose comment already landed.
    t = FakeLinear()
    tr = _tracker(t)
    assert tr.apply_no_change_outcome("SFD-9", body=None, close=True)
    assert not [d for d in t.requests if "commentCreate" in d["query"]]
    update = next(d for d in t.requests if "issueUpdate" in d["query"])
    assert update["variables"]["input"] == {"stateId": "st-done"}


def test_apply_no_change_outcome_treats_an_unsuccessful_reply_as_unconfirmed():
    """`success: false` is a warning, not an acknowledgement."""

    def transport(url, payload, headers):
        doc = json.loads(payload)
        if "commentCreate" in doc["query"]:
            return _ok({"commentCreate": {"success": False, "comment": None}})
        return FakeLinear()(url, payload, headers)

    assert not _tracker(transport).apply_no_change_outcome(
        "SFD-9", body="note", close=True
    )


def test_apply_no_change_outcome_is_unconfirmed_when_the_close_fails():
    def transport(url, payload, headers):
        doc = json.loads(payload)
        if "issueUpdate" in doc["query"]:
            return _ok({"issueUpdate": {"success": False, "issue": None}})
        return FakeLinear()(url, payload, headers)

    assert not _tracker(transport).apply_no_change_outcome(
        "SFD-9", body="note", close=True
    )
