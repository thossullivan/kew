"""Explicit mutation-response ambiguity regressions against b76a7d8a."""

from __future__ import annotations

import http.client
import json
import socket
import ssl
import urllib.error
from copy import deepcopy
from unittest.mock import Mock

import pytest
from dispatch_owner_fixture import admission_owner as admission_owner
from test_remediation_flow_gate import pinned_dispatch_scene as pinned_dispatch_scene
from test_round18_linear_compensation import linear_child as linear_child
from test_round21_admission_owner import ITEM
from test_round22_dispatcher_liveness import _claim_scene

from kew.linear.client import (
    LINEAR_GRAPHQL_URL,
    LinearApiError,
    LinearClient,
    LinearUnavailableError,
    _default_transport,
)
from kew.run_store import SqliteRunStore

REJECTION = {
    "errors": [
        {
            "message": "Invalid claim state",
            "extensions": {"code": "BAD_USER_INPUT"},
        }
    ]
}


class ClaimTransport:
    """Model remote commits independently from response delivery."""

    def __init__(self, outcomes, source=None):
        self.outcomes = list(outcomes)
        self.source = source
        self.mutations = 0
        self.verifications = 0
        self.issue = deepcopy(
            source.issue
            if source is not None
            else {"assignee": None, "state": {"id": "ready", "name": "Todo"}}
        )

    def __call__(self, _url, payload, _headers):
        request = json.loads(payload)
        query = request["query"]
        if "IssueUpdate" in query:
            return self._mutate(request["variables"]["input"])
        if "IssueClaimState" in query:
            self.verifications += 1
            return 200, json.dumps({"data": {"issue": self.issue}}).encode()
        return 200, json.dumps(
            {"data": self.source.execute(query, request["variables"])}
        ).encode()

    def _mutate(self, fields):
        if self.source is not None:
            self.source._observe_admission()
        self.mutations += 1
        outcome = self.outcomes[min(self.mutations - 1, len(self.outcomes) - 1)]
        if outcome == "success" or isinstance(outcome, ConnectionResetError):
            self.issue = {
                "identifier": ITEM,
                "assignee": {"id": fields["assigneeId"]},
                "state": {"id": fields["stateId"], "name": "In Progress"},
                "updatedAt": f"version-{self.mutations}",
            }
        if isinstance(outcome, Exception):
            raise outcome
        if outcome == "success":
            outcome = {"data": {"issueUpdate": {"success": True, "issue": self.issue}}}
        return 200, json.dumps(outcome).encode()


def _definite_failure(kind):
    if kind == "dns":
        return urllib.error.URLError(socket.gaierror("DNS lookup failed"))
    return REJECTION


@pytest.mark.parametrize("kind", ["dns", "graphql"])
def test_round23_definite_claim_failure_has_no_retention(
    admission_owner, tmp_path, kind
):
    transport = ClaimTransport([_definite_failure(kind)])
    original = deepcopy(transport.issue)
    with SqliteRunStore(tmp_path / "runs.db") as store:
        claim, client = _claim_scene(tmp_path, store, transport)
        with pytest.raises(LinearApiError) as raised:
            claim.admit("ambiguous")
        claim.retain(str(raised.value))
        assert not store.list_audit_events(event="linear_claim_outcome_unknown")
        assert not store.list_audit_events(event="linear_claim_retained")
        assert raised.value.ambiguous is False
        assert client.mutation_outcome_ambiguous is False
        assert claim.tracker.mutation_outcome_ambiguous is False
        assert not claim.outcome_unknown
        assert transport.issue == original
        # A pre-send DNS failure is a definite outage: retried within the
        # client's budget (#1117), never an unknown outcome.
        assert transport.mutations == (3 if kind == "dns" else 1)
        assert transport.verifications == 0


@pytest.mark.parametrize(
    "kind,message", [("dns", "DNS lookup failed"), ("graphql", "Invalid claim state")]
)
def test_round23_definite_refusal_preserves_remote_item(
    admission_owner, linear_child, monkeypatch, kind, message
):
    child = linear_child
    transport = ClaimTransport([_definite_failure(kind)], source=child.client)
    original = deepcopy(transport.issue)
    client = LinearClient("round23-key", transport=transport, sleep=Mock())
    monkeypatch.setattr("kew.linear.client.LinearClient", Mock(return_value=client))
    result = child.invoke()
    # A definite claim failure never launched: public exit 2 (could not run).
    assert result.exit_code == 2
    assert message in result.output
    assert not child.store.list_audit_events(event="linear_claim_outcome_unknown")
    assert not child.store.list_audit_events(event="linear_claim_retained")
    assert "claim retained" not in result.output
    assert transport.issue == original
    assert transport.verifications == 0
    assert client.mutation_outcome_ambiguous is False


def test_round23_lost_responses_verify_and_retain(
    admission_owner, tmp_path, monkeypatch
):
    transport = ClaimTransport([ConnectionResetError("mutation response lost")])
    warnings = []
    monkeypatch.setattr("kew.cli.epic._warn", warnings.append)
    with SqliteRunStore(tmp_path / "runs.db") as store:
        claim, client = _claim_scene(tmp_path, store, transport)
        with pytest.raises(LinearUnavailableError) as raised:
            claim.admit("ambiguous")
        assert getattr(raised.value, "ambiguous", None) is True
        assert client.mutation_outcome_ambiguous is True
        assert claim.tracker.mutation_outcome_ambiguous is True
        assert claim.outcome_unknown
        assert transport.mutations == 3
        assert len(store.list_audit_events(event="linear_claim_outcome_unknown")) == 1
        claim.retain("hook failed")
        assert transport.verifications == 1
        assert not claim.outcome_unknown
        [completed] = store.list_audit_events(event="linear_claim_completed")
        assert completed.payload["source"] == "verification"
        [retained] = store.list_audit_events(event="linear_claim_retained")
        assert retained.payload["reason"] == "hook failed"
        assert warnings and "claim retained" in warnings[0]
        # Verification resets the client, but must not erase mutation evidence.
        assert client.mutation_outcome_ambiguous is False
        assert claim.tracker.mutation_outcome_ambiguous is True


def test_round23_success_resolves_earlier_ambiguity(admission_owner, tmp_path):
    transport = ClaimTransport([ConnectionResetError("lost"), "success"])
    with SqliteRunStore(tmp_path / "runs.db") as store:
        claim, client = _claim_scene(tmp_path, store, transport)
        claim.admit("ambiguous")
        assert getattr(client, "mutation_outcome_ambiguous", None) is False
        assert claim.tracker.mutation_outcome_ambiguous is False
        assert not claim.outcome_unknown
        assert transport.mutations == 2
        assert claim.snapshot == transport.issue
        assert len(store.list_audit_events(event="linear_claim_completed")) == 1
        assert not store.list_audit_events(event="linear_claim_outcome_unknown")
        assert not store.list_audit_events(event="linear_claim_retained")


def test_round23_rejection_preserves_earlier_ambiguity(
    admission_owner, tmp_path, monkeypatch
):
    transport = ClaimTransport([ConnectionResetError("lost"), REJECTION])
    monkeypatch.setattr("kew.cli.epic._warn", Mock())
    with SqliteRunStore(tmp_path / "runs.db") as store:
        claim, client = _claim_scene(tmp_path, store, transport)
        with pytest.raises(LinearApiError, match="Invalid claim state") as raised:
            claim.admit("ambiguous")
        assert getattr(raised.value, "ambiguous", None) is True
        assert client.mutation_outcome_ambiguous is True
        assert claim.tracker.mutation_outcome_ambiguous is True
        assert transport.mutations == 2
        assert len(store.list_audit_events(event="linear_claim_outcome_unknown")) == 1
        claim.retain(str(raised.value))
        assert transport.verifications == 1
        assert len(store.list_audit_events(event="linear_claim_retained")) == 1


@pytest.mark.parametrize(
    "reason,retryable",
    [
        # #1117: socket-level connect failures are outages (retryable and
        # classified linear-unavailable); TLS stays fail-fast. All definite.
        (socket.gaierror("DNS failed"), True),
        (ConnectionRefusedError("refused"), True),
        (ssl.SSLError("TLS handshake failed"), False),
        (ConnectionResetError("connect reset"), True),
        (TimeoutError("connect timeout"), True),
    ],
    ids=["dns", "refused", "tls", "reset", "timeout"],
)
def test_round23_default_transport_connect_failure_is_definite(
    monkeypatch, reason, retryable
):
    connection = Mock()
    connection.connect.side_effect = reason
    monkeypatch.setattr("http.client.HTTPSConnection", Mock(return_value=connection))
    with pytest.raises(LinearApiError) as raised:
        _default_transport(LINEAR_GRAPHQL_URL, b"{}", {})
    assert getattr(raised.value, "ambiguous", None) is False
    assert isinstance(raised.value, LinearUnavailableError) is retryable


@pytest.mark.parametrize("phase", ["request", "getresponse", "read", "http_error_read"])
@pytest.mark.parametrize(
    "error",
    [
        ConnectionResetError("response reset"),
        TimeoutError("response timeout"),
        http.client.RemoteDisconnected("response disconnected"),
        http.client.IncompleteRead(b"partial"),
        http.client.BadStatusLine("partial status"),
    ],
    ids=["reset", "timeout", "disconnected", "incomplete", "bad_status"],
)
def test_round23_default_transport_response_loss_is_ambiguous(
    monkeypatch, error, phase
):
    response = Mock(status=400 if phase == "http_error_read" else 200)
    connection = Mock()
    connection.getresponse.return_value = response
    if phase in {"read", "http_error_read"}:
        response.read.side_effect = error
    else:
        getattr(connection, phase).side_effect = error
    monkeypatch.setattr("http.client.HTTPSConnection", Mock(return_value=connection))
    with pytest.raises(LinearUnavailableError) as raised:
        _default_transport(LINEAR_GRAPHQL_URL, b"{}", {})
    assert getattr(raised.value, "ambiguous", None) is True


def test_round23_complete_http_error_is_definite(monkeypatch):
    body = json.dumps(REJECTION).encode()
    response = Mock(status=400)
    response.read.return_value = body
    connection = Mock()
    connection.getresponse.return_value = response
    monkeypatch.setattr("http.client.HTTPSConnection", Mock(return_value=connection))
    assert _default_transport(LINEAR_GRAPHQL_URL, b"{}", {}) == (400, body)
    client = LinearClient("round23-key", transport=Mock(return_value=(400, body)))
    with pytest.raises(LinearApiError, match="Invalid claim state") as raised:
        client.execute("mutation IssueUpdate { issueUpdate { success } }")
    assert getattr(raised.value, "ambiguous", None) is False
    assert client.mutation_outcome_ambiguous is False


def test_round23_new_execution_clears_previous_ambiguity():
    transport = Mock(
        side_effect=[ConnectionResetError("lost"), _definite_failure("dns")]
    )
    client = LinearClient("round23-key", transport=transport, max_retries=1)
    with pytest.raises(LinearUnavailableError) as raised:
        client.execute("mutation { claim }")
    assert getattr(raised.value, "ambiguous", None) is True
    with pytest.raises(LinearApiError, match="DNS lookup failed") as definite:
        client.execute("mutation { claim }")
    assert definite.value.ambiguous is False
    assert client.mutation_outcome_ambiguous is False


def test_round23_connect_reset_retries_without_ambiguity():
    transport = Mock(side_effect=urllib.error.URLError(ConnectionResetError("connect")))
    sleep = Mock()
    client = LinearClient("round23-key", transport=transport, sleep=sleep)
    with pytest.raises(LinearUnavailableError) as raised:
        client.execute("mutation { claim }")
    assert getattr(raised.value, "ambiguous", None) is False
    assert client.mutation_outcome_ambiguous is False
    assert transport.call_count == 3
    assert [call.args for call in sleep.call_args_list] == [(1.0,), (2.0,)]
