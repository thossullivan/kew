"""The free GitHub rate-limit reading (#2297).

``rate_limit`` is the one GitHub REST endpoint that costs no quota, so kew can
read it on a schedule and know — before a dispatch burns an agent run — how much
API budget is left in each bucket a drive actually spends: ``core`` (every
``gh pr``/``gh issue`` call), ``graphql`` (the project/epic reads) and ``search``.

``gh api -i`` prints the response headers as well as the body, which is what
makes this worth one call instead of two: the body carries the three buckets and
the headers carry ``x-oauth-scopes``, so the same reading doubles as the auth
probe (``ok`` is False with ``reason="unauthorized"`` on a 401). Nothing here
raises: a reading that could not be taken is a reading that says so, because the
caller is a background sweep that must never die of a bad network.

The reading holds no credential. ``scopes`` are scope NAMES, never the token.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from kew.subprocess_utils import run

_logger = logging.getLogger(__name__)

#: The buckets kept out of the response. GitHub reports a dozen (integration
#: manifest, source imports, code scanning …) that no kew call spends.
BUCKETS = ("core", "graphql", "search")

#: Seconds a rate-limit read may take before it is treated as unavailable. The
#: sweep that calls it has a whole cycle to finish, but a hung `gh` must not
#: hold the cycle open.
READ_TIMEOUT_S = 10


@dataclass(frozen=True)
class RateLimitBucket:
    """One bucket's ceiling, what is left, and when the window resets."""

    limit: int | None = None
    remaining: int | None = None
    reset_at: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "limit": self.limit,
            "remaining": self.remaining,
            "reset_at": self.reset_at,
        }


@dataclass(frozen=True)
class RateLimitReading:
    """One ``gh api -i rate_limit`` observation.

    ``ok`` means the endpoint answered 200 and the buckets were parsed.
    ``reason`` names why not otherwise: ``unauthorized`` (the auth probe
    failing), ``http_<status>``, ``unparseable`` or ``unavailable``.
    """

    observed_at: str
    core: RateLimitBucket = RateLimitBucket()
    graphql: RateLimitBucket = RateLimitBucket()
    search: RateLimitBucket = RateLimitBucket()
    scopes: tuple[str, ...] = field(default=())
    ok: bool = False
    status: int | None = None
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        """The cacheable shape a read-only reader serves (no GitHub call)."""
        payload: dict[str, Any] = {
            bucket: getattr(self, bucket).as_dict() for bucket in BUCKETS
        }
        payload["scopes"] = list(self.scopes)
        payload["observed_at"] = self.observed_at
        payload["ok"] = self.ok
        payload["status"] = self.status
        payload["reason"] = self.reason
        return payload


def _split_response(raw: str) -> tuple[str, dict[str, str], str]:
    """``gh api -i`` output -> (status line, lowercased headers, body).

    Raises ValueError when the text is not an HTTP response at all.
    """
    head, _, body = raw.replace("\r\n", "\n").partition("\n\n")
    lines = [line for line in head.split("\n") if line.strip()]
    if not lines or not lines[0].upper().startswith("HTTP/"):
        raise ValueError("not an HTTP response")
    headers = {}
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if sep:
            headers[name.strip().lower()] = value.strip()
    return lines[0], headers, body


def _status_of(status_line: str) -> int | None:
    parts = status_line.split()
    if len(parts) < 2 or not parts[1].isdigit():
        return None
    return int(parts[1])


def _scopes_of(headers: dict[str, str]) -> tuple[str, ...]:
    raw = headers.get("x-oauth-scopes", "")
    return tuple(scope.strip() for scope in raw.split(",") if scope.strip())


def _bucket_of(resources: Any, name: str) -> RateLimitBucket:
    entry = resources.get(name) if isinstance(resources, dict) else None
    if not isinstance(entry, dict):
        return RateLimitBucket()
    reset = entry.get("reset")
    reset_at = None
    if isinstance(reset, int | float):
        reset_at = datetime.fromtimestamp(float(reset), UTC).isoformat()
    limit = entry.get("limit")
    remaining = entry.get("remaining")
    return RateLimitBucket(
        limit=int(limit) if isinstance(limit, int) else None,
        remaining=int(remaining) if isinstance(remaining, int) else None,
        reset_at=reset_at,
    )


def _failed(
    observed_at: str, reason: str, status: int | None = None
) -> RateLimitReading:
    return RateLimitReading(
        observed_at=observed_at, ok=False, status=status, reason=reason
    )


def parse_rate_limit(raw: str, *, now: datetime | None = None) -> RateLimitReading:
    """Parse a recorded ``gh api -i rate_limit`` response. Never raises."""
    observed_at = (now or datetime.now(UTC)).isoformat()
    try:
        status_line, headers, body = _split_response(raw)
    except ValueError:
        return _failed(observed_at, "unparseable")
    status = _status_of(status_line)
    if status == 401:
        return _failed(observed_at, "unauthorized", status)
    if status != 200:
        return _failed(observed_at, f"http_{status}", status)
    try:
        payload = json.loads(body)
    except ValueError:
        return _failed(observed_at, "unparseable", status)
    resources = payload.get("resources") if isinstance(payload, dict) else None
    return RateLimitReading(
        observed_at=observed_at,
        core=_bucket_of(resources, "core"),
        graphql=_bucket_of(resources, "graphql"),
        search=_bucket_of(resources, "search"),
        scopes=_scopes_of(headers),
        ok=True,
        status=status,
    )


def read_rate_limit(*, now: datetime | None = None) -> RateLimitReading:
    """Call ``gh api -i rate_limit`` once and parse it. Never raises.

    The endpoint spends no quota, so this is safe on a schedule — but it is a
    subprocess and a network call, so it belongs in the serve sweep, never on a
    request path.
    """
    observed_at = (now or datetime.now(UTC)).isoformat()
    try:
        result = run(
            ["gh", "api", "-i", "rate_limit"], check=False, timeout=READ_TIMEOUT_S
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        _logger.debug("rate-limit read unavailable: %s", exc)
        return _failed(observed_at, "unavailable")
    if getattr(result, "returncode", 1) != 0 and not (result.stdout or ""):
        return _failed(observed_at, "unavailable")
    return parse_rate_limit(result.stdout or "", now=now)
