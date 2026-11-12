"""An operator secret for readiness HMACs, outside every exported ledger."""

from __future__ import annotations

import contextlib
import os
import secrets
import stat
import tempfile
from pathlib import Path


def _key_path() -> Path:
    override = os.environ.get("KEW_READINESS_HMAC_KEY_PATH")
    if override:
        return Path(override).expanduser()
    config = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser()
    return config / "kew" / "readiness-hmac.key"


def _read_key(path: Path) -> str:
    """Reject symlinks, nonregular files, other owners and permissive modes."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "r", encoding="ascii") as handle:
        info = os.fstat(handle.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise ValueError("Readiness HMAC key requires an owner-only regular file")
        key = handle.read(65)
        if len(key) != 64 or any(char not in "0123456789abcdef" for char in key):
            raise ValueError("Readiness HMAC key requires 32 bytes encoded as hex")
        return key


def _create_key(path: Path) -> None:
    """Publish complete key bytes atomically; concurrent creators keep the winner."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".readiness-key-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(secrets.token_hex(32))
            handle.flush()
            os.fsync(handle.fileno())
        with contextlib.suppress(FileExistsError):
            os.link(temporary, path)
    finally:
        os.unlink(temporary)


def existing_readiness_hmac_key() -> str | None:
    """Load the host key when it exists; never create it.

    For read-only callers such as ``kew doctor``: a diagnostic must not write
    a secret into the operator's config directory as a side effect.
    """
    try:
        return _read_key(_key_path())
    except FileNotFoundError:
        return None


def readiness_hmac_key() -> str:
    """Load the stable host key, creating it with mode 0600 on first use."""
    path = _key_path()
    try:
        return _read_key(path)
    except FileNotFoundError:
        _create_key(path)
        return _read_key(path)
