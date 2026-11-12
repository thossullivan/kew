"""Wheels repacked from this interpreter's own installed distributions (#3194).

A test that runs the real ``uv`` against a tiny project needs the project's
dependencies from somewhere other than the network. Every installed
distribution carries its files and ``RECORD``, so each is packed back into a
``py3-none-any`` wheel in a local find-links directory that ``uv lock`` and
``uv sync`` read with no index.
"""

from __future__ import annotations

import base64
import hashlib
import zipfile
from collections.abc import Callable
from importlib.metadata import distribution
from pathlib import Path

#: Metadata files an installer writes, which no wheel carries.
_INSTALLER_FILES = frozenset({"RECORD", "INSTALLER", "REQUESTED", "direct_url.json"})


def _digest(data: bytes) -> str:
    raw = hashlib.sha256(data).digest()
    return "sha256=" + base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def repack(
    name: str,
    dest: Path,
    *,
    edit: Callable[[str, bytes], bytes] | None = None,
) -> Path:
    """Pack installed *name* into a wheel in *dest*; *edit* may rewrite a file.

    *edit* gets each file's path inside the wheel and its bytes, and returns
    the bytes to pack — the way a test builds a tampered copy of a real one.
    """
    dist = distribution(name)
    info = next(
        p.parts[0] for p in dist.files or () if p.parts[0].endswith(".dist-info")
    )
    stem = info.removesuffix(".dist-info")
    wheel = dest / f"{stem}-py3-none-any.whl"
    dest.mkdir(parents=True, exist_ok=True)
    rows: list[str] = []
    with zipfile.ZipFile(wheel, "w") as archive:
        for record in dist.files or ():
            path = record.as_posix()
            if path.startswith("..") or "__pycache__" in record.parts:
                continue
            if record.parts[0] == info and record.name in _INSTALLER_FILES:
                continue
            data = record.locate().read_bytes()
            if edit is not None:
                data = edit(path, data)
            archive.writestr(path, data)
            rows.append(f"{path},{_digest(data)},{len(data)}")
        rows.append(f"{info}/RECORD,,")
        archive.writestr(f"{info}/RECORD", "\n".join(rows) + "\n")
    return wheel


def wheelhouse(dest: Path, *names: str) -> Path:
    """A find-links directory holding a wheel of each installed *names*."""
    for name in names:
        repack(name, dest)
    return dest
