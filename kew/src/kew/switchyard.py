"""Bounded, fail-closed Switchyard compatibility probes."""

from __future__ import annotations

import json
import re
import ssl
from dataclasses import dataclass
from http.client import HTTPConnection, HTTPException, HTTPSConnection
from typing import Any, Protocol
from urllib.parse import urlsplit


@dataclass(frozen=True)
class ProbeResponse:
    """The small response surface accepted from an injected HTTP transport."""

    status_code: int
    body: Any


class SwitchyardEndpointTransport(Protocol):
    """Bounded HTTP boundary used for readiness and liveness checks."""

    def get_json(self, url: str, timeout_seconds: float) -> ProbeResponse: ...


class SwitchyardProbeTransport(SwitchyardEndpointTransport, Protocol):
    """Full compatibility boundary for static fakes and local probes."""

    def probe_version(self, timeout_seconds: float) -> str: ...

    def probe_dry_run(self, timeout_seconds: float) -> str: ...


@dataclass(frozen=True)
class SwitchyardProbeResult:
    """Uniform non-secret admission evidence returned by every probe path."""

    admitted: bool
    reason: str
    endpoint: str
    route: str
    observed_version: str | None = None


_VERSION = re.compile(r"^switchyard-server\s+([^\s]+)\s*$")
_MAX_PROBE_RESPONSE_BYTES = 1024 * 1024


class HttpSwitchyardEndpointTransport:
    """Small direct JSON client that neither proxies nor follows redirects."""

    def get_json(self, url: str, timeout_seconds: float) -> ProbeResponse:
        parts = urlsplit(url)
        host = parts.hostname
        if host is None or parts.scheme not in {"http", "https"}:
            raise ValueError("Switchyard probe URL is invalid")
        connection_type = HTTPSConnection if parts.scheme == "https" else HTTPConnection
        connection = connection_type(host, parts.port, timeout=timeout_seconds)
        try:
            connection.request("GET", parts.path)
            response = connection.getresponse()
            payload = response.read(_MAX_PROBE_RESPONSE_BYTES + 1)
            if len(payload) > _MAX_PROBE_RESPONSE_BYTES:
                raise ValueError("Switchyard probe response exceeds size limit")
            body = json.loads(payload) if response.status == 200 else None
            return ProbeResponse(status_code=response.status, body=body)
        finally:
            connection.close()


def _result(
    admitted: bool,
    reason: str,
    endpoint: str,
    route: str,
    observed_version: str | None = None,
) -> SwitchyardProbeResult:
    return SwitchyardProbeResult(
        admitted=admitted,
        reason=reason,
        endpoint=sanitize_endpoint(endpoint),
        route=route,
        observed_version=observed_version,
    )


def sanitize_endpoint(endpoint: str) -> str:
    """Retain only an origin, excluding credentials, paths, and query data."""
    try:
        parsed = urlsplit(endpoint)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return "invalid-endpoint"
    if parsed.scheme not in {"http", "https"} or host is None:
        return "invalid-endpoint"
    rendered_host = f"[{host}]" if ":" in host else host
    rendered_port = f":{port}" if port is not None else ""
    return f"{parsed.scheme}://{rendered_host}{rendered_port}"


def parse_switchyard_version(output: str) -> str | None:
    """Return the version token from the pinned server's exact CLI format."""
    match = _VERSION.fullmatch(output)
    return match.group(1) if match is not None else None


def _models_include_route(body: Any, route: str) -> bool | None:
    if not isinstance(body, dict) or "data" not in body:
        return None
    models = body["data"]
    if not isinstance(models, list):
        return None
    identifiers: list[str] = []
    for model in models:
        if not isinstance(model, dict) or not isinstance(model.get("id"), str):
            return None
        identifiers.append(model["id"])
    return route in identifiers


def _probe_local_contract(
    transport: SwitchyardProbeTransport,
    expected_version: str,
    timeout_seconds: float,
) -> tuple[str | None, str | None]:
    version_output = transport.probe_version(timeout_seconds)
    observed_version = parse_switchyard_version(version_output)
    if observed_version is None:
        return None, "malformed_response"
    if expected_version and observed_version != expected_version:
        return observed_version, "wrong_version"
    if transport.probe_dry_run(timeout_seconds).strip() != "ok":
        return observed_version, "dry_run_failed"
    return observed_version, None


def _probe_http_contract(
    transport: SwitchyardEndpointTransport,
    endpoint: str,
    route: str,
    timeout_seconds: float,
) -> str | None:
    base = endpoint.rstrip("/")
    health = transport.get_json(f"{base}/health", timeout_seconds)
    if health.status_code != 200:
        return "unhealthy"
    if not isinstance(health.body, dict) or health.body.get("status") not in {
        "ok",
        "healthy",
    }:
        return "malformed_response"

    models = transport.get_json(f"{base}/v1/models", timeout_seconds)
    if models.status_code != 200:
        return "models_unavailable"
    includes_route = _models_include_route(models.body, route)
    if includes_route is None:
        return "malformed_response"
    if not includes_route:
        return "missing_route"
    return None


def probe_switchyard_endpoint(
    *,
    endpoint: str,
    route: str,
    transport: SwitchyardEndpointTransport | None = None,
    timeout_seconds: float = 5.0,
) -> SwitchyardProbeResult:
    """Check only endpoint health and route presence after local validation."""
    active_transport = transport or HttpSwitchyardEndpointTransport()
    try:
        reason = _probe_http_contract(
            active_transport, endpoint, route, timeout_seconds
        )
        if reason is not None:
            return _result(False, reason, endpoint, route)
        return _result(True, "ready", endpoint, route)
    except ssl.SSLError:
        return _result(False, "tls_error", endpoint, route)
    except (OSError, TimeoutError, ValueError, TypeError, HTTPException, KeyError):
        return _result(False, "unreachable", endpoint, route)


def probe_switchyard(
    *,
    endpoint: str,
    route: str,
    expected_version: str,
    transport: SwitchyardProbeTransport,
    timeout_seconds: float = 5.0,
) -> SwitchyardProbeResult:
    """Run all compatibility checks, discarding raw failures and response bodies."""
    try:
        observed_version, reason = _probe_local_contract(
            transport, expected_version, timeout_seconds
        )
        if reason is not None:
            return _result(False, reason, endpoint, route, observed_version)
        reason = _probe_http_contract(transport, endpoint, route, timeout_seconds)
        if reason is not None:
            return _result(False, reason, endpoint, route, observed_version)
        return _result(True, "ready", endpoint, route, observed_version)
    except ssl.SSLError:
        return _result(False, "tls_error", endpoint, route)
    except (OSError, TimeoutError, ValueError, TypeError, HTTPException, KeyError):
        return _result(False, "unreachable", endpoint, route)
