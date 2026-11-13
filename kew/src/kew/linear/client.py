"""Minimal stdlib GraphQL client for the Linear API.

Deliberately dependency-free (the #256 ingestion precedent). The endpoint is
hardcoded — never config-driven (no SSRF surface) — and TLS verification is
HTTPSConnection's default. Linear signals throttling as a GraphQL ``RATELIMITED`` error
at HTTP 400, NOT HTTP 429, so backoff keys on the error body. The API key must
never appear in anything this module raises or logs: every outbound message
passes through ``_redact``.

Transient failures — a DNS or connect failure, a timeout, a reset connection,
any HTTP 5xx (including a gateway or Cloudflare 52x page) or a 429 that is not
Linear's JSON rate-limit error — are retried with the same bounded exponential
backoff as RATELIMITED, then surfaced as ``LinearUnavailableError`` so a caller
(the epic driver tick) can classify a Linear outage instead of dumping a
traceback. A TLS failure, an auth failure (401/403), any other 4xx or a GraphQL
error stays fail-fast: retrying a client error just wastes time.

Mutations differ once bytes are sent: a 5xx or edge-429 reply to a mutation
means the write may have committed behind the error, so it is raised at once
as an ambiguous (unknown-outcome) ``LinearUnavailableError`` and never retried.
Connect-phase failures, before anything is sent, stay definite and retried.

Spec: docs/superpowers/specs/2026-07-01-linear-surface-design.md@4a387c0b §Phase C.
"""

from __future__ import annotations

import http.client
import json
import ssl
import time
import urllib.error
from collections.abc import Callable
from contextlib import suppress
from functools import partial
from typing import Any

LINEAR_GRAPHQL_URL = "https://api.linear.app/graphql"

#: Socket timeout of the default transport, in seconds. This is a background
#: tick's budget; a caller answering a browser request passes its own (see
#: :mod:`kew.service.description`), because a read nobody is waiting on and a
#: read a panel is waiting on do not deserve the same patience.
DEFAULT_TIMEOUT_S = 30.0

_REDACTED = "<redacted:LINEAR_API_KEY>"
_NOT_FOUND_MARKERS = ("entity not found", "could not find")
# Statuses that fail fast as authentication errors whatever the body says.
_AUTH_HTTP_STATUS = frozenset({401, 403})


def _is_transient_status(status: int) -> bool:
    """Every 5xx (including Cloudflare 52x) and a 429 that is not Linear's own
    JSON ``RATELIMITED`` error: the server side or an edge in front of Linear
    failed, so the request is retried and then classified as an outage."""
    return status == 429 or 500 <= status <= 599


def _is_auth_failure(status: int, codes: set[str], messages: str) -> bool:
    """An auth status, Linear's AUTHENTICATION_ERROR code, or its message."""
    return (
        status in _AUTH_HTTP_STATUS
        or "AUTHENTICATION_ERROR" in codes
        or "authentication" in messages.lower()
    )


# transport(url, payload_bytes, headers) -> (http_status, body_bytes)
Transport = Callable[[str, bytes, dict[str, str]], tuple[int, bytes]]
_TRANSPORT_ERRORS = (
    urllib.error.URLError,
    ConnectionResetError,
    TimeoutError,
    http.client.RemoteDisconnected,
    http.client.IncompleteRead,
    http.client.BadStatusLine,
)


class LinearApiError(RuntimeError):
    """A Linear API call failed. ``not_found`` marks entity-not-found errors so
    callers can distinguish a dangling reference from a real failure."""

    def __init__(
        self, message: str, *, not_found: bool = False, ambiguous: bool = False
    ) -> None:
        super().__init__(message)
        self.not_found = not_found
        self.ambiguous = ambiguous
        # Set by ``LinearClient.execute``: a MUTATION whose result is unknown.
        self.write_outcome_unknown = False


class LinearAuthError(LinearApiError):
    """Authentication failed (bad/expired/missing key)."""


class LinearUnavailableError(LinearApiError):
    """Linear could not be reached or returned a transient server-side failure
    — a DNS or connect failure, a timeout, a reset connection, any 5xx or an
    edge 429 page — that did not clear within the retry budget.
    Distinct from ``LinearApiError`` so the epic driver tick can classify it as
    ``linear-unavailable`` (an infrastructure blip) rather than a hard error."""


class _RetryableReply(Exception):
    """Internal: a complete reply that the request loop should retry."""

    def __init__(self, error: LinearApiError) -> None:
        super().__init__(str(error))
        self.error = error


def _transport_error(
    exc: Exception, *, ambiguous: bool | None = None
) -> LinearApiError:
    """Classify response loss separately from retryable connection failures."""
    if isinstance(exc, urllib.error.URLError):
        message = f"Linear API connection failed: {exc.reason}"
        # A socket-level failure (DNS, refused, unreachable, connect timeout,
        # reset) is an outage to retry and classify; a TLS failure is not.
        reason = exc.reason
        if isinstance(reason, OSError) and not isinstance(reason, ssl.SSLError):
            return LinearUnavailableError(message, ambiguous=bool(ambiguous))
        return LinearApiError(message, ambiguous=bool(ambiguous))
    if isinstance(exc, ConnectionResetError):
        message = f"Linear API connection reset: {exc}"
    else:
        message = f"Linear API response incomplete: {exc}"
    error_type = (
        LinearUnavailableError if isinstance(exc, _TRANSPORT_ERRORS) else LinearApiError
    )
    return error_type(message, ambiguous=True if ambiguous is None else ambiguous)


def _default_transport(
    url: str,
    payload: bytes,
    headers: dict[str, str],
    *,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> tuple[int, bytes]:
    # The URL is the module-level https constant, never caller-supplied; the
    # guard makes that a hard invariant here (no file:// etc.) rather than a
    # convention the SAST tools must take on faith.
    if url != LINEAR_GRAPHQL_URL:
        raise LinearApiError(f"refusing non-Linear endpoint: {url!r}")
    connection = http.client.HTTPSConnection("api.linear.app", timeout=timeout)
    ambiguous = False
    try:
        connection.connect()
        # request() transmits bytes, so any failure from here may follow a commit.
        ambiguous = True
        connection.request("POST", "/graphql", body=payload, headers=headers)
        response = connection.getresponse()
        return response.status, response.read()
    except Exception as exc:
        # Preserve connect-phase retry policy independently from outcome certainty.
        failure = exc if ambiguous else urllib.error.URLError(exc)
        raise _transport_error(failure, ambiguous=ambiguous) from None
    finally:
        # Cleanup cannot turn a complete response into an unknown outcome.
        with suppress(Exception):
            connection.close()


def _issue_update_succeeded(data: Any) -> bool:
    """A complete data response can still reject or omit the mutation."""
    if not isinstance(data, dict):
        return False
    operation = data.get("issueUpdate")
    return isinstance(operation, dict) and operation.get("success") is True


class LinearClient:
    def __init__(
        self,
        api_key: str,
        *,
        transport: Transport | None = None,
        max_retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        if not api_key:
            raise LinearAuthError(
                "no Linear API key provided; set the LINEAR_API_KEY env variable"
            )
        self._api_key = api_key
        # ``timeout`` bounds each socket operation of the default transport; an
        # injected transport carries its own budget.
        self._transport = transport or partial(_default_transport, timeout=timeout)
        self._max_retries = max_retries
        self._sleep = sleep
        self.mutation_outcome_ambiguous = False

    def _redact(self, text: str) -> str:
        return text.replace(self._api_key, _REDACTED)

    def execute(
        self, query: str, variables: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Run a GraphQL operation; return the ``data`` object.

        Retries RATELIMITED and transient failures (DNS/connect failure,
        timeout, reset, any 5xx or a non-RATELIMITED 429) with exponential
        backoff; raises LinearAuthError on auth failures (401/403),
        LinearUnavailableError when a transient outage does not clear, and
        LinearApiError otherwise (key always redacted). TLS, other 4xx and
        GraphQL errors fail fast — retrying a client error only wastes time.
        A 5xx or edge-429 reply to a mutation is not retried: it raises an
        ambiguous LinearUnavailableError (the write may have committed).
        """
        self.mutation_outcome_ambiguous = False
        payload = json.dumps({"query": query, "variables": variables or {}}).encode()
        headers = {
            # Personal API keys go in Authorization verbatim — no Bearer prefix.
            "Authorization": self._api_key,
            "Content-Type": "application/json",
        }
        mutation = query.lstrip().startswith("mutation")
        try:
            return self._request_loop(payload, headers, mutation=mutation)
        except LinearApiError as exc:
            self.mutation_outcome_ambiguous |= exc.ambiguous
            exc.ambiguous = self.mutation_outcome_ambiguous
            exc.write_outcome_unknown = mutation and exc.ambiguous
            exc.args = (self._redact(str(exc)),)
            raise

    def _send(self, payload: bytes, headers: dict[str, str]) -> tuple[int, bytes]:
        """Apply the same failure contract to injected transports."""
        try:
            return self._transport(LINEAR_GRAPHQL_URL, payload, headers)
        except _TRANSPORT_ERRORS as exc:
            raise _transport_error(exc) from None

    def _request_loop(
        self, payload: bytes, headers: dict[str, str], *, mutation: bool
    ) -> dict[str, Any]:
        last: LinearApiError = LinearApiError("Linear API request did not succeed")
        for attempt in range(self._max_retries):
            try:
                status, body = self._send(payload, headers)
            except LinearUnavailableError as exc:
                # Transport outage: back off and retry, keeping it as the
                # error to raise if the retry budget runs out.
                last = exc
                self.mutation_outcome_ambiguous |= exc.ambiguous
            else:
                try:
                    return self._reply_data(status, body, mutation=mutation)
                except _RetryableReply as retry:
                    last = retry.error
            # Back off only between retries — no wait after the final attempt.
            if attempt < self._max_retries - 1:
                self._sleep(2.0**attempt)
        raise last

    def _reply_data(
        self, status: int, body: bytes, *, mutation: bool
    ) -> dict[str, Any]:
        """Return a reply's ``data``; raise ``_RetryableReply`` to retry.

        A 5xx or edge-429 reply to a query is an outage to retry. The same reply
        to a mutation is an unknown outcome: the write may have committed behind
        the error, so it is never retried (a retried comment would duplicate).
        """
        try:
            doc = self._parse_body(status, body)
            if not doc.get("errors") and "data" in doc:
                # Only a successful operation resolves earlier response loss.
                if _issue_update_succeeded(doc["data"]):
                    self.mutation_outcome_ambiguous = False
                return doc["data"]  # type: ignore[no-any-return]
            # Raises for anything non-retryable; returns the rate-limit text.
            message = self._classify_errors(status, doc.get("errors") or [])
        except LinearUnavailableError as exc:
            if mutation:
                raise LinearUnavailableError(
                    f"{exc} (the write's outcome is unknown; not retried)",
                    ambiguous=True,
                ) from None
            raise _RetryableReply(exc) from None
        raise _RetryableReply(
            LinearApiError(f"Linear API rate limit not clearing: {message}")
        )

    def _parse_body(self, status: int, body: bytes) -> dict[str, Any]:
        try:
            return json.loads(body.decode("utf-8", "replace"))  # type: ignore[no-any-return]
        except json.JSONDecodeError:
            detail = (
                f"Linear API returned non-JSON (HTTP {status}): "
                f"{self._redact(body.decode('utf-8', 'replace'))[:300]}"
            )
            # A 5xx or edge 429 page is transient (retry); an auth status or any
            # other non-JSON body is a hard error (fail fast).
            if _is_transient_status(status):
                raise LinearUnavailableError(detail) from None
            if status in _AUTH_HTTP_STATUS:
                raise LinearAuthError(
                    "Linear authentication failed — check the LINEAR_API_KEY "
                    f"environment variable (HTTP {status})"
                ) from None
            raise LinearApiError(detail) from None

    def _classify_errors(self, status: int, errors: list[dict[str, Any]]) -> str:
        """Raise for auth/hard errors; return the message for RATELIMITED."""
        messages = self._redact("; ".join(str(e.get("message", "")) for e in errors))
        codes = {str(e.get("extensions", {}).get("code", "")).upper() for e in errors}
        if _is_auth_failure(status, codes, messages):
            raise LinearAuthError(
                "Linear authentication failed — check the LINEAR_API_KEY "
                f"environment variable ({messages})"
            )
        if "RATELIMITED" in codes:
            return messages or "rate limited"
        if _is_transient_status(status):
            # A JSON 5xx (Linear's INTERNAL_SERVER_ERROR) is a server outage.
            raise LinearUnavailableError(
                f"Linear API server error (HTTP {status}): {messages or 'no detail'}"
            )
        raise LinearApiError(
            f"Linear API error: {messages or f'HTTP {status}'}",
            not_found=any(m in messages.lower() for m in _NOT_FOUND_MARKERS),
        )
