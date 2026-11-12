import pytest
from fastapi import Depends, FastAPI, Request

from kew.serve.auth import SessionStore
from kew.serve.security import (
    HostHeaderMiddleware,
    SecurityHeadersMiddleware,
    allowed_hosts,
    allowed_origins,
    expose_transport_refusal,
    forwarded_header_trust,
    offbox_read,
    read_guard,
    require_write,
)

# `inbox_client` is the conftest fixture (factory) from Global Constraints.


def _app(expose: bool = False) -> FastAPI:
    app = FastAPI()
    app.add_middleware(HostHeaderMiddleware)
    app.state.sessions = SessionStore()
    app.state.expose = expose
    app.state.allowed_hosts = allowed_hosts("127.0.0.1", 8765, expose)
    app.state.allowed_origins = allowed_origins("127.0.0.1", 8765, expose)

    @app.post("/login")
    def login(request: Request):
        sid, csrf = request.app.state.sessions.create()
        resp = {"csrf": csrf}
        from fastapi.responses import JSONResponse

        r = JSONResponse(resp)
        r.set_cookie("kew_session", sid, httponly=True, samesite="strict")
        return r

    @app.post("/write")
    def write(request: Request, _: object = Depends(require_write)):
        return {"ok": True}

    @app.get("/read")
    def read(request: Request, _: object = Depends(read_guard)):
        return {"ok": True}

    @app.get("/offbox")
    def offbox(request: Request, _: object = Depends(offbox_read)):
        return {"ok": True}

    return app


def test_write_requires_session(inbox_client):
    c = inbox_client(_app())
    assert c.post("/write").status_code == 401


def test_write_requires_csrf_header(inbox_client):
    c = inbox_client(_app())
    csrf = c.post("/login").json()["csrf"]
    # session cookie now set on the client; omit the CSRF header
    assert c.post("/write").status_code == 403
    # wrong header
    assert c.post("/write", headers={"X-Kew-CSRF": "nope"}).status_code == 403
    # correct header
    assert c.post("/write", headers={"X-Kew-CSRF": csrf}).status_code == 200


def test_write_origin_allowlist(inbox_client):
    c = inbox_client(_app())
    csrf = c.post("/login").json()["csrf"]
    ok = {"X-Kew-CSRF": csrf, "Origin": "http://127.0.0.1:8765"}
    bad = {"X-Kew-CSRF": csrf, "Origin": "http://evil.example"}
    assert c.post("/write", headers=ok).status_code == 200
    assert c.post("/write", headers=bad).status_code == 403


def test_host_header_rejected(inbox_client):
    c = inbox_client(_app(), loopback_peer=True)
    # base_url sets Host: 127.0.0.1:8765 (allowed); an explicit bad Host 403s.
    assert c.get("/read", headers={"Host": "evil.example"}).status_code == 403
    assert c.get("/read").status_code == 200


def test_read_open_on_loopback_when_not_exposed(inbox_client):
    # Unauthenticated read must 200 — requires an explicit loopback peer (C2).
    c = inbox_client(_app(expose=False), loopback_peer=True)
    assert c.get("/read").status_code == 200


def test_read_requires_session_when_exposed(inbox_client):
    # Even from a loopback peer, exposure forces auth on reads.
    c = inbox_client(_app(expose=True), loopback_peer=True)
    assert c.get("/read").status_code == 401
    c.post("/login")
    assert c.get("/read").status_code == 200


def test_read_requires_session_when_not_exposed_and_not_loopback(inbox_client):
    # expose=False but a non-loopback peer must still 401 without a session.
    c = inbox_client(_app(expose=False), loopback_peer=False)
    assert c.get("/read").status_code == 401


@pytest.mark.parametrize(
    "header",
    [
        "Forwarded",
        "X-Forwarded-For",
        "X-Forwarded-Host",
        "X-Forwarded-Proto",
        "X-Real-IP",
        "Via",
        "X-Forwarded-Server",
        "X-Forwarded-Port",
    ],
)
def test_a_loopback_read_carrying_forwarded_headers_needs_a_session(
    inbox_client, header
):
    # A same-host reverse proxy reaches kew from loopback; the header it adds
    # says the reader is somewhere else, so the loopback free read is off.
    c = inbox_client(_app(expose=False), loopback_peer=True)
    assert c.get("/read").status_code == 200
    assert c.get("/read", headers={header: "203.0.113.9"}).status_code == 401
    assert c.get("/offbox", headers={header: "203.0.113.9"}).status_code == 401
    c.post("/login")
    assert c.get("/read", headers={header: "203.0.113.9"}).status_code == 200


def test_offbox_read_is_open_to_the_app_itself(inbox_client):
    # A same-origin GET: browsers send no Origin, and Sec-Fetch-Site says the
    # app's own page asked. Nothing about the loopback read path changes.
    c = inbox_client(_app(), loopback_peer=True)
    assert c.get("/offbox").status_code == 200
    assert (
        c.get("/offbox", headers={"Sec-Fetch-Site": "same-origin"}).status_code == 200
    )
    # Address bar / bookmark: no initiating site at all.
    assert c.get("/offbox", headers={"Sec-Fetch-Site": "none"}).status_code == 200
    ok = {"Origin": "http://127.0.0.1:8765", "Sec-Fetch-Site": "same-origin"}
    assert c.get("/offbox", headers=ok).status_code == 200


def test_offbox_read_refuses_a_cross_origin_fetch(inbox_client):
    """A page the operator merely visits must not be able to drive a read whose
    cost lands off-box, even though CORS keeps it from seeing the answer."""
    c = inbox_client(_app(), loopback_peer=True)
    evil = {"Origin": "http://evil.example", "Sec-Fetch-Site": "cross-site"}
    assert c.get("/offbox", headers=evil).status_code == 403
    # The Origin alone is enough, for a client that sends no Sec-Fetch-Site.
    origin_only = {"Origin": "http://evil.example"}
    assert c.get("/offbox", headers=origin_only).status_code == 403
    # The ordinary read on the same peer is untouched by the stricter guard.
    assert c.get("/read", headers=evil).status_code == 200


def test_offbox_read_lets_a_declared_reverse_proxy_origin_through(inbox_client):
    """An operator-declared origin is cross-origin and legitimate: the SPA on a
    TLS proxy's public host is a different origin (and, for a sibling host, a
    same-SITE one), so the Origin allowlist has to decide those, not the
    initiator header."""
    app = _app()
    app.state.allowed_origins = allowed_origins(
        "127.0.0.1", 8765, False, extra_origins=["https://kew.example.com"]
    )
    c = inbox_client(app, loopback_peer=True)
    declared = {"Origin": "https://kew.example.com", "Sec-Fetch-Site": "same-site"}
    assert c.get("/offbox", headers=declared).status_code == 200


def test_offbox_read_refuses_a_cross_site_subresource(inbox_client):
    """An `<img src>` or `<script src>` on another site carries no Origin, so
    Sec-Fetch-Site is the only thing that tells kew who started the request."""
    c = inbox_client(_app(), loopback_peer=True)
    for site in ("cross-site", "same-site"):
        assert c.get("/offbox", headers={"Sec-Fetch-Site": site}).status_code == 403


def test_offbox_read_still_requires_a_session_where_read_guard_does(inbox_client):
    # It is read_guard PLUS a cross-site refusal, never instead of it.
    exposed = inbox_client(_app(expose=True), loopback_peer=True)
    assert exposed.get("/offbox").status_code == 401
    remote = inbox_client(_app(expose=False), loopback_peer=False)
    assert remote.get("/offbox").status_code == 401


def test_write_non_ascii_csrf_header_returns_403_not_500(inbox_client):
    # A non-ASCII X-Kew-CSRF value must never produce a 500; it must 403.
    # Pass the value as raw bytes (b"caf\xe9") — httpx rejects non-ASCII str
    # header values at the client level, but a real HTTP client can send raw bytes.
    c = inbox_client(_app())
    c.post("/login")  # establishes session; CSRF value is on the app side
    assert c.post("/write", headers={"X-Kew-CSRF": b"caf\xe9"}).status_code == 403


def test_allowlist_covers_non_127_0_0_1_loopback_bind():
    # A valid loopback bind outside 127.0.0.1 must be accepted as Host/Origin.
    hosts = allowed_hosts("127.0.0.2", 8765, False)
    assert "127.0.0.2:8765" in hosts
    origins = allowed_origins("127.0.0.2", 8765, False)
    assert "http://127.0.0.2:8765" in origins


def test_allowlist_brackets_ipv6_bind_and_omits_malformed():
    # A bare ::1 bind normalizes to [::1]:port; the malformed ::1:port never appears.
    hosts = allowed_hosts("::1", 8765, False)
    assert "[::1]:8765" in hosts
    assert "::1:8765" not in hosts


def test_allowlist_covers_concrete_exposed_bind():
    hosts = allowed_hosts("192.168.1.5", 8765, True)
    origins = allowed_origins("192.168.1.5", 8765, True)
    assert "192.168.1.5:8765" in hosts
    assert "http://192.168.1.5:8765" in origins
    assert "https://192.168.1.5:8765" in origins


def test_extra_origins_added_without_touching_loopback_default():
    # The loopback-default set (no extra_origins) is unchanged...
    default = allowed_origins("127.0.0.1", 8765, False)
    assert default == allowed_origins("127.0.0.1", 8765, False, ())
    # ...and a declared public origin is added on top.
    with_proxy = allowed_origins("127.0.0.1", 8765, False, ["https://kew.example.com"])
    assert with_proxy == default | {"https://kew.example.com"}


def test_extra_origins_lets_reverse_proxy_origin_write(inbox_client):
    # A TLS reverse proxy sends the public Origin (no port); with it declared,
    # the write passes the Origin allowlist instead of 403ing.
    app = _app()
    app.state.allowed_origins = allowed_origins(
        "127.0.0.1", 8765, False, ["https://kew.example.com"]
    )
    c = inbox_client(app)
    csrf = c.post("/login").json()["csrf"]
    headers = {"X-Kew-CSRF": csrf, "Origin": "https://kew.example.com"}
    assert c.post("/write", headers=headers).status_code == 200


def test_security_headers_on_every_response(inbox_client):
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/x")
    def x():
        return {"ok": True}

    resp = inbox_client(app).get("/x")
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["x-frame-options"] == "DENY"
    assert resp.headers["referrer-policy"] == "no-referrer"
    csp = resp.headers["content-security-policy"]
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "object-src 'none'" in csp


def test_security_headers_wrap_host_rejection(inbox_client):
    # Even a HostHeaderMiddleware 403 carries the hardening headers.
    app = _app()
    app.add_middleware(SecurityHeadersMiddleware)
    c = inbox_client(app, loopback_peer=True)
    resp = c.get("/read", headers={"Host": "evil.example"})
    assert resp.status_code == 403
    assert resp.headers["x-content-type-options"] == "nosniff"


# ── W1.6: an exposed bind must declare its transport ────────────────


def test_expose_without_a_declared_trusted_proxy_is_refused():
    """kew serves plaintext HTTP, so exposing without a TLS terminator in
    front puts the admin token and session cookie on the wire in the clear.
    The refusal must name the key that fixes it, not just say 'no'."""
    refusal = expose_transport_refusal([])
    assert refusal is not None
    assert "trusted_proxies" in refusal
    assert "flight_recorder" in refusal
    assert "TLS" in refusal


def test_expose_with_a_declared_trusted_proxy_is_allowed():
    # The declared proxy IS the TLS terminator: declaring it is the operator
    # saying the network hop in front of kew is not plaintext.
    assert expose_transport_refusal(["127.0.0.1"]) is None


def test_forwarded_headers_are_trusted_from_nobody_by_default():
    # Not even uvicorn's own 127.0.0.1 default: an undeclared proxy must not
    # be able to rewrite the peer address the read guard reads.
    assert forwarded_header_trust([]) == (False, [])


def test_forwarded_headers_are_trusted_only_from_configured_proxies():
    assert forwarded_header_trust(["10.0.0.5", "192.168.1.0/24"]) == (
        True,
        ["10.0.0.5", "192.168.1.0/24"],
    )


def _login_cookies(tmp_path, inbox_client, *, expose: bool) -> dict[str, str]:
    """Log in over plaintext http and return the raw Set-Cookie strings."""
    from contextlib import nullcontext

    from kew.config import DispatchConfig
    from kew.serve.app import create_app
    from kew.serve.auth import SessionStore

    app = create_app(
        lambda: nullcontext(object()),
        repo_dir=tmp_path,
        config=DispatchConfig(),
        token="TESTTOKEN",
        sessions=SessionStore(),
        bind_host="127.0.0.1",
        bind_port=8765,
        expose=expose,
    )
    client = inbox_client(app, loopback_peer=True)
    response = client.post("/api/auth/login", json={"token": "TESTTOKEN"})
    assert response.status_code == 200, response.text
    raw = response.headers.get_list("set-cookie")
    return {header.split("=", 1)[0]: header for header in raw}


def test_exposed_login_cookies_are_pinned_secure_over_plaintext(tmp_path, inbox_client):
    """Cookie flags under --expose, pinned: the request kew itself sees is
    plaintext http (the TLS hop ends at the proxy), and the cookies must
    still carry Secure so a browser never replays them over http."""
    cookies = _login_cookies(tmp_path, inbox_client, expose=True)
    session, csrf = cookies["kew_session"], cookies["kew_csrf"]
    for header in (session, csrf):
        assert "; Secure" in header
        assert "SameSite=strict" in header
        assert "Path=/" in header
        assert "Max-Age=43200" in header
    assert "HttpOnly" in session
    # The CSRF cookie is the double-submit half the SPA must read.
    assert "HttpOnly" not in csrf


def test_loopback_login_cookies_keep_their_flags_and_stay_non_secure(
    tmp_path, inbox_client
):
    # The loopback default is unchanged: Secure over plain http there would
    # make the local inbox unusable, and nothing crosses a network.
    cookies = _login_cookies(tmp_path, inbox_client, expose=False)
    session, csrf = cookies["kew_session"], cookies["kew_csrf"]
    for header in (session, csrf):
        assert "; Secure" not in header
        assert "SameSite=strict" in header
        assert "Path=/" in header
    assert "HttpOnly" in session
    assert "HttpOnly" not in csrf


def test_forwarded_proto_is_honoured_only_through_uvicorn_from_a_listed_proxy(
    tmp_path, inbox_client
):
    """End to end through uvicorn's ProxyHeadersMiddleware, which never runs
    under a bare TestClient: a listed loopback proxy's X-Forwarded-Proto makes
    the request https (so the Secure flag reflects it); an unlisted peer's
    header is ignored. Pins the trust rule the config validator protects."""
    from contextlib import nullcontext

    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    from kew.config import DispatchConfig
    from kew.serve.app import create_app
    from kew.serve.auth import SessionStore

    def scheme_seen(trusted: list[str]) -> str:
        app = create_app(
            lambda: nullcontext(object()),
            repo_dir=tmp_path,
            config=DispatchConfig(),
            token="TESTTOKEN",
            sessions=SessionStore(),
            bind_host="0.0.0.0",
            bind_port=8765,
            expose=True,
        )
        seen: dict[str, str] = {}

        async def probe(scope, receive, send):
            if scope["type"] == "http":
                seen["scheme"] = scope["scheme"]
            await app(scope, receive, send)

        wrapped = ProxyHeadersMiddleware(probe, trusted_hosts=trusted)
        client = inbox_client(wrapped, loopback_peer=True)
        client.get("/api/auth/session", headers={"X-Forwarded-Proto": "https"})
        return seen.get("scheme", "")

    assert scheme_seen(["127.0.0.0/24"]) == "https"
    assert scheme_seen([]) == "http"


_SCHEMA_AND_DOCS_PATHS = ("/openapi.json", "/docs", "/redoc", "/docs/oauth2-redirect")


def _schema_app(tmp_path, static_dir=None):
    from contextlib import nullcontext

    from kew.config import DispatchConfig
    from kew.serve.app import create_app

    return create_app(
        lambda: nullcontext(object()),
        repo_dir=tmp_path,
        static_dir=static_dir,
        config=DispatchConfig(),
    )


def _assert_neither_schema_nor_docs(response, path):
    assert response.headers.get("content-type", "").startswith("text/html"), path
    body = response.text.lower()
    for marker in ('"openapi"', "swagger", "redoc"):
        assert marker not in body, (path, marker)


def test_the_api_schema_and_docs_pages_are_not_served(tmp_path, inbox_client):
    # No bundle built: every non-API GET gets the missing-build hint page.
    c = inbox_client(_schema_app(tmp_path), loopback_peer=True)
    for path in _SCHEMA_AND_DOCS_PATHS:
        _assert_neither_schema_nor_docs(c.get(path), path)


def test_the_spa_shell_answers_the_schema_and_docs_paths(tmp_path, inbox_client):
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<!doctype html><title>kew shell</title>")
    c = inbox_client(_schema_app(tmp_path, static_dir=web), loopback_peer=True)
    for path in _SCHEMA_AND_DOCS_PATHS:
        response = c.get(path)
        assert "kew shell" in response.text, path
        _assert_neither_schema_nor_docs(response, path)


def test_the_app_still_builds_its_schema_for_the_drift_gate(tmp_path):
    schema = _schema_app(tmp_path).openapi()
    assert schema["info"]["title"] == "kew"
    assert any(path.startswith("/api/") for path in schema["paths"])
