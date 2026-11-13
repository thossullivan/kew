"""The free GitHub rate-limit reading (#2297).

``gh api -i rate_limit`` is the one GitHub endpoint that spends no quota, so the
serve sweep calls it once per cycle and keeps the answer: the three buckets a
dispatch run can exhaust (core, graphql, search), the token's scopes, and when
the reading was taken. It doubles as the auth probe — a 401 is recorded as a
reading that is not ``ok`` rather than raising.

These tests parse a RECORDED response (headers + body exactly as `gh api -i`
prints them), so the parser is pinned to gh's real output shape without a
network call.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from kew.github_rate_limit import RateLimitReading, parse_rate_limit, read_rate_limit

# A real `gh api -i rate_limit` response, trimmed to the headers that matter.
RECORDED_OK = """HTTP/2.0 200 OK
Access-Control-Allow-Origin: *
Content-Type: application/json; charset=utf-8
Date: Tue, 06 Oct 2026 12:00:00 GMT
X-Accepted-Oauth-Scopes:\x20
X-Github-Api-Version-Selected: 2022-11-28
X-Oauth-Scopes: gist, read:org, repo, workflow
X-Ratelimit-Limit: 5000
X-Ratelimit-Remaining: 4987
X-Ratelimit-Reset: 1791374400
X-Ratelimit-Resource: core
X-Ratelimit-Used: 13

{"resources":{"core":{"limit":5000,"used":13,"remaining":4987,"reset":1791374400},\
"search":{"limit":30,"used":0,"remaining":30,"reset":1791370860},\
"graphql":{"limit":5000,"used":120,"remaining":4880,"reset":1791373200},\
"integration_manifest":{"limit":5000,"used":0,"remaining":5000,"reset":1791374400}},\
"rate":{"limit":5000,"used":13,"remaining":4987,"reset":1791374400}}
"""

RECORDED_401 = """HTTP/2.0 401 Unauthorized
Content-Type: application/json; charset=utf-8
X-Github-Media-Type: github.v3; format=json

{"message":"Bad credentials","documentation_url":"https://docs.github.com/rest"}
"""

NOW = datetime(2026, 10, 6, 12, 0, 1, tzinfo=UTC)


def test_parses_a_recorded_gh_api_i_response() -> None:
    reading = parse_rate_limit(RECORDED_OK, now=NOW)

    assert reading.ok is True
    assert reading.status == 200
    assert reading.observed_at == "2026-10-06T12:00:01+00:00"
    assert reading.core.limit == 5000
    assert reading.core.remaining == 4987
    assert reading.core.reset_at == "2026-10-07T12:00:00+00:00"
    assert reading.graphql.remaining == 4880
    assert reading.graphql.reset_at == "2026-10-07T11:40:00+00:00"
    assert reading.search.limit == 30
    assert reading.search.reset_at == "2026-10-07T11:01:00+00:00"
    assert reading.scopes == ("gist", "read:org", "repo", "workflow")


def test_the_reading_serializes_to_the_recorded_shape() -> None:
    payload = parse_rate_limit(RECORDED_OK, now=NOW).as_dict()

    assert payload["core"] == {
        "limit": 5000,
        "remaining": 4987,
        "reset_at": "2026-10-07T12:00:00+00:00",
    }
    assert set(payload) == {
        "core",
        "graphql",
        "search",
        "scopes",
        "observed_at",
        "ok",
        "status",
        "reason",
    }
    # No token, no header dump: scopes are names, never a credential.
    assert payload["scopes"] == ["gist", "read:org", "repo", "workflow"]


def test_a_401_is_a_not_ok_reading_not_a_raise() -> None:
    reading = parse_rate_limit(RECORDED_401, now=NOW)

    assert reading.ok is False
    assert reading.status == 401
    assert reading.reason == "unauthorized"
    assert reading.core.limit is None
    assert reading.scopes == ()


def test_unparseable_output_is_a_not_ok_reading() -> None:
    reading = parse_rate_limit("not an http response at all", now=NOW)

    assert reading.ok is False
    assert reading.reason == "unparseable"
    assert reading.observed_at == "2026-10-06T12:00:01+00:00"


def test_read_rate_limit_parses_the_subprocess_output(monkeypatch: Any) -> None:
    calls: list[list[str]] = []

    class _Result:
        returncode = 0
        stdout = RECORDED_OK
        stderr = ""

    def _run(cmd: list[str], **kwargs: Any) -> Any:
        calls.append(cmd)
        return _Result()

    monkeypatch.setattr("kew.github_rate_limit.run", _run)

    reading = read_rate_limit(now=NOW)

    assert calls == [["gh", "api", "-i", "rate_limit"]]
    assert reading.core.remaining == 4987


def test_read_rate_limit_is_fail_soft_when_gh_cannot_run(monkeypatch: Any) -> None:
    def _run(cmd: list[str], **kwargs: Any) -> Any:
        raise OSError("gh: not found")

    monkeypatch.setattr("kew.github_rate_limit.run", _run)

    reading = read_rate_limit(now=NOW)

    assert isinstance(reading, RateLimitReading)
    assert reading.ok is False
    assert reading.reason == "unavailable"
