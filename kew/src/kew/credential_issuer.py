"""Host-only OpenRouter bounded-key issuance. Never retain response diagnostics."""

from __future__ import annotations

import json
import logging
import math
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.client import HTTPSConnection
from typing import Protocol
from urllib.parse import urlsplit

KEYS_ENDPOINT = "https://openrouter.ai/api/v1/keys"
AUTHORIZATION_HEADER = "Authorization"
PROVISIONING_ENV = "OPENROUTER_PROVISIONING_KEY"
MANAGEMENT_CREDENTIAL_ENVS = (PROVISIONING_ENV,)
INFERENCE_ENV = "OPENROUTER_API_KEY"
NAME_FIELD = "name"
RUN_KEY_NAME_PREFIX = "kew-run:"
LIMIT_FIELD = "limit"
RESET_FIELD = "limit_reset"
EXPIRY_FIELD = "expires_at"
BYOK_FIELD = "include_byok_in_limit"
KEY_FIELD = "key"
DATA_FIELD = "data"
HASH_FIELD = "hash"
HTTP_TIMEOUT_SECONDS = 15
# OpenRouter key identities are SHA-256 hashes, not opaque credential tokens.
_HASH_PATTERN = re.compile(r"[0-9a-f]{64}")
_LEDGER_ID_PATTERN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
_logger = logging.getLogger(__name__)


class IssuerError(RuntimeError):
    """A safe diagnostic containing no issuer response or credential."""

    reason = "issuer error"

    def __init__(self) -> None:
        super().__init__(self.reason)


class IssuerUnavailable(IssuerError):
    reason = "issuer unavailable"


class IssuerRefused(IssuerError):
    reason = "issuer refused"


class InvalidProvisioningCredential(IssuerRefused):
    reason = (
        "invalid provisioning credential; "
        "replace OPENROUTER_PROVISIONING_KEY on the host"
    )


class KeyAlreadyAbsent(IssuerError):
    reason = "already absent"


class MalformedIssuerReply(IssuerError):
    reason = "malformed issuer reply"


@dataclass(frozen=True)
class BoundedRunKey:
    secret: str = field(repr=False)
    key_hash: str
    limit_usd: float
    expires_at: str
    issuer: str = "openrouter"
    name: str | None = None

    @property
    def identity(self) -> dict[str, object]:
        return {
            "issuer": self.issuer,
            "key_hash": self.key_hash,
            "limit_usd": self.limit_usd,
            "expires_at": self.expires_at,
            "limit_reset": None,
            "name": self.name,
        }

    @property
    def auth_path(self) -> str:
        return f"issued:openrouter:{self.key_hash}"


class IssuerHTTP(Protocol):
    def __call__(
        self, method: str, url: str, *, headers: dict[str, str], body: bytes | None
    ) -> tuple[int, bytes]: ...


def _http(
    method: str, url: str, *, headers: dict[str, str], body: bytes | None
) -> tuple[int, bytes]:
    parsed = urlsplit(url)
    connection = HTTPSConnection(parsed.netloc, timeout=HTTP_TIMEOUT_SECONDS)
    try:
        target = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        connection.request(method, target, body=body, headers=headers)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def _request(
    method: str, url: str, body: dict[str, object] | None, http: IssuerHTTP
) -> bytes:
    provisioning_key = os.environ.get(PROVISIONING_ENV)
    if not provisioning_key:
        raise IssuerUnavailable()
    try:
        status, reply = http(
            method,
            url,
            headers={
                AUTHORIZATION_HEADER: f"Bearer {provisioning_key}",
                "Content-Type": "application/json",
            },
            body=None if body is None else json.dumps(body, allow_nan=False).encode(),
        )
    except Exception:
        raise IssuerUnavailable() from None
    return _checked_response(method, status, reply)


def _checked_response(method: str, status: int, reply: bytes) -> bytes:
    _check_lookup_status(method, status)
    if status >= 500 or status == 429:
        raise IssuerUnavailable()
    if method == "DELETE":
        _check_delete_status(status, reply)
    accepted = (200, 201) if method == "POST" else (200, 204)
    if status not in accepted:
        raise IssuerRefused()
    return b'{"deleted":true}' if method == "DELETE" and status == 204 else reply


def _absent_reply(reply: bytes) -> bool:
    """Accept explicit target-key absence without retaining issuer diagnostics."""
    try:
        raw = json.loads(reply)
    except (ValueError, TypeError):
        raise MalformedIssuerReply() from None
    if not isinstance(raw, dict):
        return False
    error = raw.get("error", raw)
    if not isinstance(error, dict):
        return False
    message = error.get("message")
    if not isinstance(message, str):
        return False
    return message.strip().rstrip(".").casefold() in (
        "key not found",
        "api key not found",
        "key does not exist",
        "api key does not exist",
        "key no longer exists",
        "api key no longer exists",
        "key already deleted",
        "api key already deleted",
    )


def _check_delete_status(status: int, reply: bytes) -> None:
    if status in (401, 403):
        raise InvalidProvisioningCredential()
    if status == 404 or (status in (200, 400, 410) and _absent_reply(reply)):
        raise KeyAlreadyAbsent()


def _utc_expiry(expires_at: datetime) -> str:
    if expires_at.tzinfo is None or expires_at.utcoffset() is None:
        raise IssuerRefused()
    expiry = expires_at.astimezone(UTC)
    if expiry <= datetime.now(UTC):
        raise IssuerRefused()
    return expiry.isoformat().replace("+00:00", "Z")


def _valid_hash(value: object, issued_key: object = None) -> bool:
    """Reject credentials independently of reply validation before URL assembly."""
    known_credentials = (
        issued_key,
        os.environ.get(INFERENCE_ENV),
        *(os.environ.get(name) for name in MANAGEMENT_CREDENTIAL_ENVS),
    )
    return (
        isinstance(value, str)
        and _HASH_PATTERN.fullmatch(value) is not None
        and value not in known_credentials
    )


def _parse_reply(
    reply: bytes, limit: float, expiry: str, expected_name: str
) -> BoundedRunKey:
    try:
        raw = json.loads(reply)
        data = raw[DATA_FIELD]
        secret, key_hash = raw[KEY_FIELD], data[HASH_FIELD]
        valid = (
            isinstance(secret, str)
            and bool(secret)
            and _valid_hash(key_hash, secret)
            and data.get(NAME_FIELD) == expected_name
            and os.environ.get(PROVISIONING_ENV) not in (secret, key_hash)
            and _confirmed_bounds(data, limit, expiry)
        )
        if valid:
            return BoundedRunKey(secret, key_hash, limit, expiry, name=expected_name)
    except (ValueError, TypeError, KeyError, OverflowError):
        raise MalformedIssuerReply() from None
    raise MalformedIssuerReply()


def parse_run_key_name(name: str) -> tuple[str, str] | None:
    """Accept only a canonical ledger UUID and one nonempty run segment."""
    parts = name.split(":")
    if len(parts) != 3 or parts[0] + ":" != RUN_KEY_NAME_PREFIX or not parts[2]:
        return None
    if _LEDGER_ID_PATTERN.fullmatch(parts[1]) is None:
        return None
    return parts[1], parts[2]


def run_key_name(ledger_id: str, run_id: str) -> str:
    """Reserve a deterministic issuer namespace for one durable ledger."""
    name = f"{RUN_KEY_NAME_PREFIX}{ledger_id}:{run_id}"
    if parse_run_key_name(name) is None:
        raise IssuerRefused()
    return name


def mint_bounded_run_key(
    run_id: str,
    limit_usd: float,
    expires_at: datetime,
    *,
    ledger_id: str,
    http: IssuerHTTP = _http,
    on_malformed: Callable[[BoundedRunKey], None] | None = None,
) -> BoundedRunKey:
    """Issue one finite, non-resetting dollar ceiling with a UTC expiry."""
    if (
        type(limit_usd) not in (int, float)
        or not math.isfinite(limit_usd)
        or limit_usd <= 0
    ):
        raise IssuerRefused()
    if not run_id:
        raise IssuerRefused()
    expiry = _utc_expiry(expires_at)
    name = run_key_name(ledger_id, run_id)
    body = {
        NAME_FIELD: name,
        LIMIT_FIELD: float(limit_usd),
        RESET_FIELD: None,
        EXPIRY_FIELD: expiry,
        BYOK_FIELD: True,
    }
    reply = _request("POST", KEYS_ENDPOINT, body, http)
    try:
        return _parse_reply(reply, float(limit_usd), expiry, name)
    except MalformedIssuerReply:
        _revoke_malformed_reply(reply, http, float(limit_usd), expiry, on_malformed)
        raise


def revoke_run_key(
    key_hash: str, *, issued_key: str | None = None, http: IssuerHTTP = _http
) -> str | None:
    """Revoke by issuer identity. Never place credential values in the URL."""
    if not _valid_hash(key_hash, issued_key):
        _logger.warning("revocation skipped: invalid issuer identity")
        raise IssuerRefused()
    try:
        reply = _request("DELETE", f"{KEYS_ENDPOINT}/{key_hash}", None, http)
    except KeyAlreadyAbsent:
        return "already absent"
    try:
        raw = json.loads(reply)
    except (ValueError, TypeError):
        raise MalformedIssuerReply() from None
    if not isinstance(raw, dict) or raw.get("deleted") is not True:
        raise MalformedIssuerReply()
    return None


def _confirmed_bounds(data: dict[str, object], limit: float, expiry: str) -> bool:
    observed_expiry = data[EXPIRY_FIELD]
    if not isinstance(observed_expiry, str):
        return False
    return (
        type(data[LIMIT_FIELD]) in (int, float)
        and data[LIMIT_FIELD] == limit
        and datetime.fromisoformat(observed_expiry) == datetime.fromisoformat(expiry)
        and data[RESET_FIELD] is None
        and data[BYOK_FIELD] is True
    )


def has_provisioning_key() -> bool:
    """Expose presence without exposing management authority to route callers."""
    return bool(os.environ.get(PROVISIONING_ENV))


def _revoke_malformed_reply(
    reply: bytes,
    http: IssuerHTTP,
    limit: float,
    expiry: str,
    on_malformed: Callable[[BoundedRunKey], None] | None,
) -> None:
    """Clean up a validated hash when the reply cannot confirm name or bounds."""
    try:
        raw = json.loads(reply)
        key_hash = raw[DATA_FIELD][HASH_FIELD]
        issued_key = raw.get(KEY_FIELD)
    except (ValueError, TypeError, KeyError):
        _logger.warning("revocation skipped: invalid issuer identity")
        return
    if not _valid_hash(key_hash, issued_key):
        _logger.warning("revocation skipped: invalid issuer identity")
        return
    try:
        if on_malformed is None:
            revoke_run_key(key_hash, issued_key=issued_key, http=http)
        else:
            on_malformed(BoundedRunKey("", key_hash, limit, expiry))
    except Exception:
        _logger.warning("revocation outcome unconfirmed")


class KeyLookupUnsupported(IssuerError):
    reason = "issuer lookup unsupported"


def _key_list_page(reply: bytes) -> list[dict[str, object]]:
    try:
        data = json.loads(reply)[DATA_FIELD]
    except (ValueError, TypeError, KeyError):
        raise MalformedIssuerReply() from None
    if not isinstance(data, list) or any(
        not isinstance(item, dict)
        or not isinstance(item.get(NAME_FIELD), str)
        or not _valid_hash(item.get(HASH_FIELD))
        for item in data
    ):
        raise MalformedIssuerReply()
    return data


def _list_keys(*, http: IssuerHTTP = _http) -> list[dict[str, object]]:
    """Read the complete issuer list before any recovery mutates its pages."""
    found: list[dict[str, object]] = []
    offset = 0
    # Finite work per recovery attempt; an incomplete list never proves absence.
    for _ in range(100):
        reply = _request(
            "GET", f"{KEYS_ENDPOINT}?include_disabled=true&offset={offset}", None, http
        )
        page = _key_list_page(reply)
        found.extend(page)
        if len(page) < 100:
            return found
        offset += len(page)
    raise IssuerUnavailable()


def list_run_keys(*, http: IssuerHTTP = _http) -> list[dict[str, object]]:
    """List kew's deterministic namespace, including disabled entries."""
    return [
        item
        for item in _list_keys(http=http)
        if str(item[NAME_FIELD]).startswith(RUN_KEY_NAME_PREFIX)
    ]


def lookup_run_key_hashes(name: str, *, http: IssuerHTTP = _http) -> list[str]:
    """Match the exact persisted name, including disabled entries."""
    return sorted(
        {
            str(item[HASH_FIELD])
            for item in _list_keys(http=http)
            if item[NAME_FIELD] == name
        }
    )


def _check_lookup_status(method: str, status: int) -> None:
    if method == "GET" and status in (404, 405, 501):
        raise KeyLookupUnsupported()
