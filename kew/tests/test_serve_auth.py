import stat
from pathlib import Path

from kew.serve.auth import Session, SessionStore, resolve_token, verify_token


def test_token_generated_0600_and_stable(tmp_path: Path):
    p = tmp_path / "serve_token"
    t1 = resolve_token(p, None)
    assert t1 and len(t1) >= 32
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert resolve_token(p, None) == t1  # stable across calls


def test_env_token_wins_over_file(tmp_path: Path):
    p = tmp_path / "serve_token"
    resolve_token(p, None)
    assert resolve_token(p, "ENVTOKEN") == "ENVTOKEN"


def test_regenerate_replaces_file(tmp_path: Path):
    p = tmp_path / "serve_token"
    t1 = resolve_token(p, None)
    t2 = resolve_token(p, None, regenerate=True)
    assert t1 != t2
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_verify_token_rejects_empty_configured_token():
    assert verify_token("", "") is False
    assert verify_token("anything", "") is False


def test_verify_token_constant_time_semantics():
    assert verify_token("abc", "abc") is True
    assert verify_token("abc", "abd") is False
    assert verify_token("", "abc") is False


def test_session_create_get_delete_and_expiry():
    clock = {"t": 1000.0}
    store = SessionStore(ttl_seconds=100.0, now=lambda: clock["t"])
    sid, csrf = store.create()
    assert csrf
    s = store.get(sid)
    assert s is not None and s.csrf_token == csrf
    clock["t"] = 1101.0  # past ttl
    assert store.get(sid) is None  # expired + evicted
    sid2, _ = store.create()
    store.delete(sid2)
    assert store.get(sid2) is None
    assert store.get(None) is None


def test_resolve_token_regenerates_empty_file(tmp_path: Path):
    p = tmp_path / "serve_token"
    p.write_text("")
    p.chmod(0o600)
    token = resolve_token(p, None)
    assert token and len(token) >= 32
    assert p.read_text().strip() == token
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_session_repr_does_not_leak_csrf():
    s = Session(created=0.0, expires=9999.0, csrf_token="SUPER_SECRET_CSRF")
    assert "SUPER_SECRET_CSRF" not in repr(s)
