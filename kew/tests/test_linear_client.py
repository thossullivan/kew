"""LinearClient: stdlib GraphQL transport with RATELIMITED backoff + redaction.

Spec: docs/superpowers/specs/2026-07-01-linear-surface-design.md@4a387c0b §Phase C
(API client). Throttling is a GraphQL error code at HTTP 400, NOT HTTP 429;
the key must never appear in any raised message (F-L6).
"""

import json
import socket
import ssl
import urllib.error
from unittest.mock import Mock

import pytest

from kew.linear.client import (
    LINEAR_GRAPHQL_URL,
    LinearApiError,
    LinearAuthError,
    LinearClient,
    LinearUnavailableError,
)

KEY = "fake-test-key-not-a-secret"  # deliberately low-entropy: gitleaks


def _ok_body(data):
    return 200, json.dumps({"data": data}).encode()


def _error_body(message, code=None, status=200):
    err = {"message": message}
    if code:
        err["extensions"] = {"code": code}
    return status, json.dumps({"errors": [err]}).encode()


class FakeTransport:
    """Records requests; replays a scripted list of (status, body) responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, url, payload, headers):
        self.requests.append((url, json.loads(payload), headers))
        return self.responses.pop(0)


def _client(responses, **kw):
    t = FakeTransport(responses)
    return LinearClient(KEY, transport=t, sleep=lambda _s: None, **kw), t


def test_posts_to_hardcoded_endpoint_with_raw_key_auth():
    c, t = _client([_ok_body({"viewer": {"id": "u1"}})])
    data = c.execute("query { viewer { id } }")
    assert data == {"viewer": {"id": "u1"}}
    url, payload, headers = t.requests[0]
    assert url == LINEAR_GRAPHQL_URL == "https://api.linear.app/graphql"
    # Personal API keys go in Authorization verbatim — no Bearer prefix.
    assert headers["Authorization"] == KEY
    assert payload["query"] == "query { viewer { id } }"


def test_variables_are_sent():
    c, t = _client([_ok_body({"issue": None})])
    c.execute("query($id: String!) { issue(id: $id) { id } }", {"id": "ENG-1"})
    assert t.requests[0][1]["variables"] == {"id": "ENG-1"}


def test_ratelimited_graphql_error_retries_then_succeeds():
    # Linear signals throttling in the GraphQL error body (HTTP 400) — a
    # 429-keyed backoff would never fire (adversarial API-F1).
    c, t = _client(
        [
            _error_body("rate limited", code="RATELIMITED", status=400),
            _ok_body({"viewer": {"id": "u1"}}),
        ]
    )
    assert c.execute("query { viewer { id } }") == {"viewer": {"id": "u1"}}
    assert len(t.requests) == 2


def test_ratelimited_exhaustion_raises_without_key(monkeypatch):
    c, _ = _client([_error_body("rate limited", code="RATELIMITED", status=400)] * 3)
    with pytest.raises(LinearApiError) as exc:
        c.execute("query { viewer { id } }")
    assert KEY not in str(exc.value)


def test_auth_error_names_env_var_and_redacts_key():
    c, _ = _client([_error_body(f"authentication failed for {KEY}", status=401)])
    with pytest.raises(LinearAuthError) as exc:
        c.execute("query { viewer { id } }")
    msg = str(exc.value)
    assert "LINEAR_API_KEY" in msg
    assert KEY not in msg


def test_error_bodies_containing_key_are_redacted():
    # F-L6: a forced server error echoing the key must not surface it.
    c, _ = _client([(500, f"boom {KEY} boom".encode())] * 3)
    with pytest.raises(LinearApiError) as exc:
        c.execute("query { viewer { id } }")
    assert KEY not in str(exc.value)


@pytest.mark.parametrize(
    "response",
    [
        pytest.param((500, b"<html>500 Internal Server Error</html>"), id="500-html"),
        pytest.param(
            _error_body("Internal server error", "INTERNAL_SERVER_ERROR", 500),
            id="500-json",
        ),
        pytest.param((520, b"error code: 520"), id="cloudflare-520"),
        pytest.param((522, b"error code: 522"), id="cloudflare-522"),
        pytest.param((429, b"<html>429 Too Many Requests</html>"), id="429-html"),
    ],
)
def test_server_and_edge_failures_are_unavailable_and_retried(response):
    # #2020: every 5xx and an edge 429 page are outages, not client errors.
    sleeps = []
    t = FakeTransport([response] * 3)
    c = LinearClient(KEY, transport=t, sleep=sleeps.append)
    with pytest.raises(LinearUnavailableError) as exc:
        c.execute("query { viewer { id } }")
    assert exc.value.ambiguous is False
    assert sleeps == [1.0, 2.0]
    assert len(t.requests) == 3


@pytest.mark.parametrize(
    "response",
    [
        pytest.param((500, b"<html>500</html>"), id="500-html"),
        pytest.param(
            _error_body("Internal server error", "INTERNAL_SERVER_ERROR", 500),
            id="500-json",
        ),
        pytest.param((502, b"<html>bad gateway</html>"), id="502"),
        pytest.param((503, b"<html>unavailable</html>"), id="503"),
        pytest.param((504, b"<html>gateway timeout</html>"), id="504"),
        pytest.param((520, b"error code: 520"), id="cloudflare-520"),
        pytest.param((429, b"<html>429 Too Many Requests</html>"), id="429-html"),
    ],
)
def test_mutation_server_reply_is_an_unknown_outcome_and_not_retried(response):
    # The write may have committed behind the error: retrying a comment or a
    # claim could duplicate it, so the client stops and reports it unknown.
    sleeps = []
    t = FakeTransport([response] * 3)
    c = LinearClient(KEY, transport=t, sleep=sleeps.append)
    with pytest.raises(LinearUnavailableError) as exc:
        c.execute("mutation IssueUpdate { issueUpdate { success } }")
    assert exc.value.ambiguous is True
    assert exc.value.write_outcome_unknown is True
    assert c.mutation_outcome_ambiguous is True
    assert "outcome is unknown" in str(exc.value)
    assert len(t.requests) == 1
    assert sleeps == []


def test_query_server_reply_is_retried_and_not_a_write_outcome():
    sleeps = []
    t = FakeTransport([(500, b"<html>500</html>")] * 3)
    c = LinearClient(KEY, transport=t, sleep=sleeps.append)
    with pytest.raises(LinearUnavailableError) as exc:
        c.execute("query { viewer { id } }")
    assert exc.value.write_outcome_unknown is False
    assert len(t.requests) == 3
    assert sleeps == [1.0, 2.0]


def test_mutation_connect_failure_stays_definite_and_retried():
    sleeps = []
    t = Mock(side_effect=urllib.error.URLError(socket.gaierror(8, "DNS failed")))
    c = LinearClient(KEY, transport=t, sleep=sleeps.append)
    with pytest.raises(LinearUnavailableError) as exc:
        c.execute("mutation IssueUpdate { issueUpdate { success } }")
    assert exc.value.ambiguous is False
    assert exc.value.write_outcome_unknown is False
    assert t.call_count == 3


def test_mutation_json_rate_limit_is_definite_and_retried():
    c, t = _client([_error_body("slow down", code="RATELIMITED", status=429)] * 3)
    with pytest.raises(LinearApiError) as exc:
        c.execute("mutation IssueUpdate { issueUpdate { success } }")
    assert exc.value.write_outcome_unknown is False
    assert len(t.requests) == 3


def test_linear_json_429_rate_limit_stays_on_the_rate_limit_path():
    c, t = _client([_error_body("slow down", code="RATELIMITED", status=429)] * 3)
    with pytest.raises(LinearApiError) as exc:
        c.execute("query { viewer { id } }")
    assert not isinstance(exc.value, LinearUnavailableError)
    assert "rate limit not clearing" in str(exc.value)
    assert len(t.requests) == 3


@pytest.mark.parametrize("status", [401, 403])
def test_auth_status_with_html_body_fails_fast_as_auth_error(status):
    c, t = _client([(status, f"<html>denied {KEY}</html>".encode())])
    with pytest.raises(LinearAuthError) as exc:
        c.execute("query { viewer { id } }")
    assert KEY not in str(exc.value)
    assert len(t.requests) == 1


def test_graphql_errors_raise_api_error():
    c, _ = _client([_error_body("Entity not found: Issue")])
    with pytest.raises(LinearApiError) as exc:
        c.execute("query { issue { id } }")
    assert exc.value.not_found is True


def test_non_not_found_error_flagged():
    c, _ = _client([_error_body("something else broke")])
    with pytest.raises(LinearApiError) as exc:
        c.execute("query { x }")
    assert exc.value.not_found is False


def test_linear_unavailable_is_a_linear_api_error():
    # A tick catching LinearApiError still traps the transient case; callers
    # that care can narrow to LinearUnavailableError for `linear-unavailable`.
    assert issubclass(LinearUnavailableError, LinearApiError)


def test_transient_5xx_non_json_retries_then_succeeds():
    # 502/503/504 gateway pages are HTML, not JSON — a transient blip in front
    # of Linear, not a client error. Retry until it clears.
    c, t = _client(
        [
            (503, b"<html>503 Service Unavailable</html>"),
            _ok_body({"viewer": {"id": "u1"}}),
        ]
    )
    assert c.execute("query { viewer { id } }") == {"viewer": {"id": "u1"}}
    assert len(t.requests) == 2


def test_transient_5xx_exhaustion_raises_unavailable_without_key():
    c, _ = _client([(502, f"<html>bad gateway {KEY}</html>".encode())] * 3)
    with pytest.raises(LinearUnavailableError) as exc:
        c.execute("query { viewer { id } }")
    assert KEY not in str(exc.value)


def test_transient_backoff_is_exponential_between_retries_only():
    # Backoff fires only *between* attempts — no wait after the final failed
    # attempt (3 attempts => 2 sleeps), so an outage doesn't cost a dead 4s wait.
    sleeps = []
    t = FakeTransport([(504, b"<html>gateway timeout</html>")] * 3)
    c = LinearClient(KEY, transport=t, sleep=sleeps.append)
    with pytest.raises(LinearUnavailableError):
        c.execute("query { viewer { id } }")
    assert sleeps == [1.0, 2.0]


def test_connection_reset_retries_then_succeeds():
    # A reset surfaces as LinearUnavailableError from the transport; the retry
    # loop treats it exactly like a transient 5xx.
    calls = {"n": 0}

    def transport(url, payload, headers):
        calls["n"] += 1
        if calls["n"] == 1:
            raise LinearUnavailableError("Linear API connection reset: [Errno 104]")
        return _ok_body({"viewer": {"id": "u1"}})

    c = LinearClient(KEY, transport=transport, sleep=lambda _s: None)
    assert c.execute("query { viewer { id } }") == {"viewer": {"id": "u1"}}
    assert calls["n"] == 2


def test_4xx_non_json_fails_fast_without_retry():
    # A 4xx with a non-JSON body is a client-side error, not a transient blip:
    # fail fast, no backoff (retrying a 400 just wastes time).
    c, t = _client([(400, b"<html>bad request</html>")])
    with pytest.raises(LinearApiError) as exc:
        c.execute("query { viewer { id } }")
    assert not isinstance(exc.value, LinearUnavailableError)
    assert len(t.requests) == 1


def test_default_transport_classifies_connection_reset_as_unavailable(monkeypatch):
    connection = Mock()
    connection.request.side_effect = ConnectionResetError(
        104, "Connection reset by peer"
    )
    monkeypatch.setattr("http.client.HTTPSConnection", Mock(return_value=connection))
    c = LinearClient(KEY, sleep=Mock())
    with pytest.raises(LinearUnavailableError) as exc:
        c.execute("query { viewer { id } }")
    assert exc.value.ambiguous is True
    assert KEY not in str(exc.value)


def test_default_transport_classifies_connect_reset_as_unavailable(monkeypatch):
    connection = Mock()
    connection.connect.side_effect = ConnectionResetError(
        104, "Connection reset by peer"
    )
    monkeypatch.setattr("http.client.HTTPSConnection", Mock(return_value=connection))
    c = LinearClient(KEY, sleep=Mock())
    with pytest.raises(LinearUnavailableError) as exc:
        c.execute("query { viewer { id } }")
    assert exc.value.ambiguous is False
    assert "connection failed" in str(exc.value).lower()
    assert KEY not in str(exc.value)


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(socket.gaierror(8, "nodename nor servname provided"), id="dns"),
        pytest.param(ConnectionRefusedError(61, "Connection refused"), id="refused"),
        pytest.param(TimeoutError("timed out"), id="connect-timeout"),
        pytest.param(OSError(51, "Network is unreachable"), id="unreachable"),
    ],
)
def test_connect_phase_network_errors_are_unavailable_and_retried(monkeypatch, failure):
    # #1117: a DNS failure during the go-live drive surfaced as a hard
    # LinearApiError and crashed the tick; it is an outage, not a client error.
    connection = Mock()
    connection.connect.side_effect = failure
    monkeypatch.setattr("http.client.HTTPSConnection", Mock(return_value=connection))
    sleeps = []
    c = LinearClient(KEY, sleep=sleeps.append)
    with pytest.raises(LinearUnavailableError) as exc:
        c.execute("query { viewer { id } }")
    assert "connection" in str(exc.value).lower()
    # Nothing was sent, so even a mutation's outcome is definite.
    assert exc.value.ambiguous is False
    assert connection.request.call_count == 0
    assert sleeps == [1.0, 2.0]
    assert KEY not in str(exc.value)


def test_tls_failure_stays_fail_fast(monkeypatch):
    connection = Mock()
    connection.connect.side_effect = ssl.SSLCertVerificationError(
        1, "certificate verify failed"
    )
    monkeypatch.setattr("http.client.HTTPSConnection", Mock(return_value=connection))
    sleeps = []
    c = LinearClient(KEY, sleep=sleeps.append)
    with pytest.raises(LinearApiError) as exc:
        c.execute("query { viewer { id } }")
    assert not isinstance(exc.value, LinearUnavailableError)
    assert sleeps == []
