"""Token resolution + in-process session store for the authenticated inbox.

stdlib only — must NOT import kew.run_store / kew.audit / kew.orchestrator
(import-linter contract on kew.serve).
"""

from __future__ import annotations

import os
import secrets
import stat
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

_DEFAULT_TTL_SECONDS = 12 * 60 * 60  # 12h fixed, no sliding


def _write_token_file(path: Path) -> str:
    token = secrets.token_urlsafe(32)
    # O_EXCL so the secret is never on disk world-readable for any window.
    fd = os.open(str(path), os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600)
    try:
        os.write(fd, token.encode())
    except BaseException:
        os.close(fd)
        path.unlink(missing_ok=True)
        raise
    os.close(fd)
    return token


def _read_existing_token(path: Path) -> str | None:
    """Return stripped token from *path*, or None if empty (triggers regen)."""
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        path.chmod(0o600)
    token = path.read_text().strip()
    return token if token else None


def resolve_token(
    token_path: Path, env_token: str | None, *, regenerate: bool = False
) -> str:
    """Active token: env override wins; else the 0600 file (created if absent)."""
    if env_token:
        return env_token
    if regenerate and token_path.exists():
        token_path.unlink()
    if token_path.exists():
        token = _read_existing_token(token_path)
        if token:
            return token
        # Empty file — treat as absent; unlink so O_EXCL create succeeds.
        token_path.unlink(missing_ok=True)
    return _write_token_file(token_path)


def verify_token(provided: str, actual: str) -> bool:
    if not actual:  # no token configured -> auth disabled, never accept
        return False
    return secrets.compare_digest(provided.encode(), actual.encode())


@dataclass(repr=False)
class Session:
    created: float
    expires: float
    csrf_token: str


class SessionStore:
    """Thread-safe in-process sessions. Cleared on restart (revocation path)."""

    def __init__(
        self,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._now = now
        self._lock = threading.Lock()
        self._sessions: dict[str, Session] = {}

    def create(self) -> tuple[str, str]:
        sid = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        t = self._now()
        with self._lock:
            self._sessions[sid] = Session(
                created=t, expires=t + self._ttl, csrf_token=csrf
            )
        return sid, csrf

    def get(self, sid: str | None) -> Session | None:
        if not sid:
            return None
        with self._lock:
            s = self._sessions.get(sid)
            if s is None:
                return None
            if self._now() >= s.expires:
                del self._sessions[sid]  # lazy eviction
                return None
            return s

    def delete(self, sid: str) -> None:
        with self._lock:
            self._sessions.pop(sid, None)
