"""Auth dependencies + Host-header middleware for the inbox.

stdlib + FastAPI/Starlette only — no kew.run_store / kew.audit / kew.orchestrator.
"""

from __future__ import annotations

import ipaddress
import secrets
from collections.abc import Callable, Iterable

from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import PlainTextResponse, Response

from kew.serve.auth import Session, SessionStore

_CSRF_HEADER = "x-kew-csrf"

# The `Sec-Fetch-Site` values :func:`offbox_read` treats as not-cross-site:
# the app's own pages, and a request the user made directly (address bar,
# bookmark), which carries "none".
_SAME_SITE_FETCH = frozenset({"same-origin", "none"})

# Applied to the one response that may carry exact prompt bytes. `private`
# keeps a shared cache out of it, `no-store` keeps every cache out of it, and
# `max-age=0` covers the heuristically-freshening implementations that treat a
# response with no explicit age as cacheable anyway.
_SENSITIVE_CACHE_CONTROL = "private, no-store, max-age=0"

# Baseline hardening headers applied to every response. The CSP is same-origin
# only ('self') — kew's SPA and API make no cross-origin requests — with inline
# script/style allowed because the built SvelteKit shell hydrates via inline
# tags. frame-ancestors + X-Frame-Options DENY block clickjacking; nosniff stops
# MIME confusion. These are static (independent of bind/expose), so the
# loopback-default response is byte-identical whether exposed or not.
_SECURITY_HEADERS = {
    "content-security-policy": (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "font-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "object-src 'none'"
    ),
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
}


def _host_variants(host: str, port: int) -> set[str]:
    return {f"{host}:{port}", host}


def _normalize_host(host: str) -> str:
    # Bracket a bare IPv6 literal so it forms a valid Host/Origin value
    # (e.g. ::1 -> [::1]); leave hostnames and IPv4 untouched.
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


def allowed_hosts(bind_host: str, port: int, expose: bool) -> set[str]:
    # localhost aliases PLUS the actual bind host — so any loopback bind in
    # 127.0.0.0/8 (or a concrete exposed address the client sends as Host) is
    # accepted, not just 127.0.0.1. Bare "::1" is omitted from the base since
    # bracketed "[::1]" covers it; bind_host is normalized the same way.
    base = {"localhost", "127.0.0.1", "[::1]"}
    hosts: set[str] = set()
    for h in base:
        hosts |= _host_variants(h, port)
    hosts |= _host_variants(_normalize_host(bind_host), port)
    return hosts


def allowed_origins(
    bind_host: str,
    port: int,
    expose: bool,
    extra_origins: Iterable[str] = (),
) -> set[str]:
    # Mirror allowed_hosts: localhost aliases plus the normalized bind host.
    base = ["127.0.0.1", "localhost", "[::1]"]
    origins = {f"http://{h}:{port}" for h in base}
    nb = _normalize_host(bind_host)
    origins.add(f"http://{nb}:{port}")
    if expose:
        origins.add(f"https://{nb}:{port}")  # exposed deployments may front with TLS
    # Operator-declared public origins for a TLS reverse proxy: the browser's
    # Origin (scheme + host, no internal port) never matches bind_host:port, so
    # without these, writes 403. Default () keeps the loopback allowlist unchanged.
    origins |= set(extra_origins)
    return origins


def expose_transport_refusal(trusted_proxies: Iterable[str]) -> str | None:
    """Why an exposed bind is refused, or ``None`` when its transport is declared.

    kew never terminates TLS (docs/security.md), so an exposed bind speaks
    plaintext HTTP: the admin token pasted into the sign-in form and the
    session cookie that follows it cross the network in the clear, and the
    cookie's ``Secure`` flag would be the only thing keeping the browser from
    replaying it there. The only transport that makes exposure safe is a TLS
    reverse proxy in front, and the only thing kew can check is that the
    operator named that proxy — which is the same declaration that decides
    whose ``X-Forwarded-*`` headers are believed. Undeclared, kew refuses
    rather than serving the token over plaintext.
    """
    if any(trusted_proxies):
        return None
    return (
        "exposing kew serve without a declared TLS terminator would send the admin "
        "token and session cookie over plaintext HTTP.\n"
        "       kew does not terminate TLS: front it with a TLS reverse proxy and "
        "name that proxy's address in kew.toml —\n"
        "         [flight_recorder]\n"
        '         trusted_proxies = ["127.0.0.1"]  # the only peer whose '
        "X-Forwarded-* kew believes\n"
        "       Without --expose (and without [flight_recorder].expose), kew stays "
        "on its loopback default and needs none of this."
    )


def forwarded_header_trust(trusted_proxies: Iterable[str]) -> tuple[bool, list[str]]:
    """ASGI-server ``(proxy_headers, forwarded_allow_ips)`` for these proxies.

    uvicorn trusts ``X-Forwarded-For`` / ``-Proto`` from ``127.0.0.1`` by
    default, so any local process could rewrite the peer address
    :func:`read_guard` reads and the scheme the cookie flags read. kew trusts
    those headers from nobody unless the operator listed the proxy.
    """
    proxies = list(trusted_proxies)
    return bool(proxies), proxies


def cookie_secure(request: Request) -> bool:
    """Whether the session cookies must carry ``Secure``.

    Forced on under ``--expose`` regardless of the scheme kew itself sees: an
    exposed deployment is behind a TLS proxy by construction (see
    :func:`expose_transport_refusal`), and that proxy's plaintext hop to kew
    must never be what downgrades the cookie to one a browser will replay
    over HTTP.
    """
    return request.url.scheme == "https" or bool(request.app.state.expose)


def _peer_is_loopback(request: Request) -> bool:
    client = request.client
    if client is None:
        return False
    try:
        return ipaddress.ip_address(client.host).is_loopback
    except ValueError:
        return False


def require_session(request: Request) -> Session:
    sessions: SessionStore = request.app.state.sessions
    session = sessions.get(request.cookies.get("kew_session"))
    if session is None:
        raise HTTPException(status_code=401, detail="authentication required")
    return session


def _check_csrf(request: Request, session: Session) -> None:
    header = request.headers.get(_CSRF_HEADER)
    # Compare on bytes so non-ASCII header values (e.g. "café") are safely handled
    # instead of raising TypeError from secrets.compare_digest.  encode("utf-8",
    # "ignore") strips any non-ASCII chars, which will never match the token → 403.
    if not header or not secrets.compare_digest(
        header.encode("utf-8", "ignore"), session.csrf_token.encode()
    ):
        raise HTTPException(status_code=403, detail="invalid or missing CSRF token")
    _check_origin(request)


def _check_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin is not None and origin not in request.app.state.allowed_origins:
        raise HTTPException(status_code=403, detail="origin not allowed")


def require_write(request: Request) -> Session:
    session = require_session(request)
    _check_csrf(request, session)
    return session


# Headers a reverse proxy adds to say who the reader really is, or that it
# relayed the request. kew trusts none of them for the peer address (see
# :func:`forwarded_header_trust`), but their presence on a loopback request
# says a proxy on this host relayed it. A browser cannot set ``Via``. A proxy
# that adds none of these looks like a local reader: ``expose`` is the control
# for that.
_FORWARDED_HEADERS = (
    "forwarded",
    "via",
    "x-forwarded-for",
    "x-forwarded-host",
    "x-forwarded-port",
    "x-forwarded-proto",
    "x-forwarded-server",
    "x-real-ip",
)


def _is_forwarded(request: Request) -> bool:
    return any(name in request.headers for name in _FORWARDED_HEADERS)


def needs_session(request: Request) -> bool:
    """Whether a read must carry a live session, at entry and on every poll.

    Always under ``--expose``. Otherwise only a loopback peer reads
    session-free, and not when a forwarded header says a proxy on this host
    relayed someone else's request.
    """
    if request.app.state.expose:
        return True
    return not _peer_is_loopback(request) or _is_forwarded(request)


def read_guard(request: Request) -> None:
    """Reads need a session whenever :func:`needs_session` says so."""
    if needs_session(request):
        require_session(request)


def stream_authorization(request: Request) -> Callable[[], bool]:
    """The per-poll re-check for a long-lived stream that passed read_guard.

    A session can expire or be revoked during a connection that may last half
    an hour, so a stream whose reader needed a session at entry re-checks the
    cookie on every poll rather than trusting the opening gate. A reader that
    needed none at entry still needs none.
    """
    if not needs_session(request):
        return _always_authorized
    sessions: SessionStore = request.app.state.sessions
    cookie = request.cookies.get("kew_session")

    def _is_authorized() -> bool:
        return sessions.get(cookie) is not None

    return _is_authorized


def _always_authorized() -> bool:
    return True


def offbox_read(request: Request) -> None:
    """The guard for a read whose SIDE EFFECT leaves the box (FUI-31).

    :func:`read_guard` gates reads composed from the local store. Those are
    cheap and self-contained: a cross-origin page can make the operator's
    browser issue one, but CORS keeps it from reading the answer back, and all
    it spends is a SQLite query. This gates a read that spends something else —
    a ``gh`` subprocess and a request against the operator's own GitHub
    credentials — where the drive-by matters even though the answer stays
    unreadable, because the cost is off-box and shared with the dispatch driver.

    So it is ``read_guard`` plus a cross-site refusal, on the two headers a
    browser sets and script cannot forge:

    * ``Origin``, which is attached to every cross-origin ``fetch`` — including
      a simple GET that triggers no preflight, i.e. exactly the request a page
      would use to drive this read. It is the stronger signal and it decides
      alone: an allowlisted origin passes (a reverse-proxy deployment whose SPA
      sits on a declared ``extra_origins`` host is cross-origin and legitimate),
      anything else is the same 403 a write gets.
    * ``Sec-Fetch-Site``, consulted only for the requests that carry no
      ``Origin`` — an ``<img src>``, a ``<script src>``, a form navigation —
      which are drivable from another page and describe no origin at all.

    Both are absent from a same-origin GET (browsers omit ``Origin`` there) and
    from a non-browser client, so absence is allowed: this refuses a *declared*
    cross-site driver, and the session/loopback decision in ``read_guard`` above
    is what governs everything that declares nothing. What bounds the spend a
    permitted caller can drive is the service's own read budget, not this guard.
    """
    read_guard(request)
    _check_origin(request)
    if request.headers.get("origin") is not None:
        return  # allowlisted by _check_origin, whatever the initiator was
    site = request.headers.get("sec-fetch-site")
    if site is not None and site not in _SAME_SITE_FETCH:
        raise HTTPException(status_code=403, detail="cross-site read not allowed")


def sensitive_read(request: Request, response: Response) -> Session:
    """The guard for a response whose BODY is the secret (I17).

    :func:`read_guard` gates projections of what a run did. This gates the one
    route that hands back the exact compiled prompt — the attacker-reachable
    spec, the repo map and whatever the operator appended, verbatim — so it is
    stricter in both directions:

    * **No loopback exemption.** A real session is required even from
      127.0.0.1. The exemption that keeps ordinary local reads friction-free
      is not extended to a route that returns the bytes themselves; any local
      process that can reach the port would otherwise be able to read them.
    * **Off under ``--expose``.** An exposed deployment refuses outright
      rather than merely authenticating. Sending raw prompt bytes across a
      network is not something kew does, and "disabled" is a boundary an
      operator can reason about where "authenticated" is a judgement call.

    It also stamps the response ``private, no-store`` so a shared cache, a
    browser disk cache or a back-button never re-serves bytes the operator
    deliberately asked for exactly once.
    """
    if request.app.state.expose:
        raise HTTPException(
            status_code=403,
            detail="protected prompt reads are disabled while serving exposed",
        )
    session = require_session(request)
    response.headers["cache-control"] = _SENSITIVE_CACHE_CONTROL
    response.headers["pragma"] = "no-cache"
    return session


class HostHeaderMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        host = request.headers.get("host", "")
        if host not in request.app.state.allowed_hosts:
            return PlainTextResponse("host not allowed", status_code=403)
        return await call_next(request)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Attach baseline hardening headers to every response.

    Uses setdefault so a route that sets its own value (none do today) is never
    clobbered, and so an already-present header survives.
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        response = await call_next(request)
        for name, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response
