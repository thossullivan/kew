"""The code a running ``kew drive`` was started on, and when it moved (#3197).

A long-lived drive keeps the modules it loaded at start while the checkout or
install under it moves on: it then lazily imports new modules beside the old
ones, and its ``--kew-binary`` children run the new code (and may migrate the
ledger past the schema the drive knows). The drive records its identity at
start and reads it again before each launch; a change stops admission, lets
running work settle and restarts the drive on the new code.

The identity is the kew version plus the source tree's git head when the
package is a checkout's source tree, else a content hash of the installed
package. Neither reading is a subprocess.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path

import kew
from kew.progress import _warn

#: The exit code of a drive that had to restart and could not: the scheme's
#: "could not run" (``docs/cli/exit-codes.md`` allows no public code above
#: 2), non-zero so launchd's ``KeepAlive {SuccessfulExit: false}`` restarts
#: it. The exit it records tells it apart from a lost lock.
DRIVE_RESTART_EXIT = 2

#: The env var a re-exec'd drive reads to take its own lease back (#3197).
DRIVE_RESUME_ENV = "KEW_DRIVE_RESUME_TOKEN"

#: The refusal record beside the ledger (#3284): launchd restarts any
#: non-zero exit, and the restarted drive fails at the ledger open before it
#: can write to the ledger, so the refusal that stopped it lives outside it.
DRIVE_REFUSAL = "drive-refusal.json"

#: A start that repeats a refusal recorded this recently, on the same code,
#: exits 0 so launchd leaves the drive down.
REFUSAL_REPEAT_S = 600

_SKIP_DIRS = frozenset({"__pycache__"})


def package_dir() -> Path:
    """The directory the running kew package was imported from."""
    return Path(kew.__file__).resolve().parent


def _version() -> str:
    for dist in ("kew-cli", "kew"):
        try:
            return metadata.version(dist)
        except metadata.PackageNotFoundError:
            continue
    return "unknown"


def _git_dir(source: Path) -> Path | None:
    """The git directory of the checkout ``source`` lives in, if any. An
    installed package (under ``site-packages``) never counts: a venv inside a
    checkout would otherwise follow the checkout's head, not the install."""
    if "site-packages" in source.parts:
        return None
    for parent in (source, *source.parents):
        dot = parent / ".git"
        if dot.is_dir():
            return dot
        if dot.is_file():
            text = dot.read_text(encoding="utf-8").strip()
            if text.startswith("gitdir:"):
                return (parent / text.removeprefix("gitdir:").strip()).resolve()
            return None
    return None


def _ref(git: Path, ref: str) -> str | None:
    common = git
    commondir = git / "commondir"
    if commondir.is_file():
        common = (git / commondir.read_text(encoding="utf-8").strip()).resolve()
    for base in (git, common):
        loose = base / ref
        if loose.is_file():
            return loose.read_text(encoding="utf-8").strip()
    packed = common / "packed-refs"
    if packed.is_file():
        for line in packed.read_text(encoding="utf-8").splitlines():
            sha, _, name = line.partition(" ")
            if name.strip() == ref:
                return sha
    return None


def _git_head(source: Path) -> str | None:
    """The commit the checkout holding ``source`` is at; None when there is
    no checkout or its head cannot be read."""
    try:
        git = _git_dir(source)
        if git is None:
            return None
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref:"):
            return _ref(git, head.removeprefix("ref:").strip())
        return head or None
    except OSError as exc:
        _warn(
            f"kew drive: cannot read the git head over {source} ({exc}); "
            "identifying the kew there by its content."
        )
        return None


def _package_files(source: Path) -> list[tuple[str, Path]]:
    files = []
    for path in sorted(source.rglob("*")):
        rel = path.relative_to(source)
        if _SKIP_DIRS.intersection(rel.parts) or path.suffix == ".pyc":
            continue
        if path.is_file():
            files.append((rel.as_posix(), path))
    return files


def _stat_print(files: list[tuple[str, Path]]) -> tuple[tuple[str, int, int], ...]:
    """Each file's size and mtime: what any rewrite of the package changes."""
    prints = []
    for rel, path in files:
        try:
            st = path.stat()
        except OSError:
            prints.append((rel, -1, -1))
            continue
        prints.append((rel, st.st_size, st.st_mtime_ns))
    return tuple(prints)


# The last content hash per package, keyed by its stat print: the drive reads
# its identity before every launch, and hashing the whole package each time
# would cost a noticeable share of a core.
_HASHED: dict[Path, tuple[tuple[tuple[str, int, int], ...], str]] = {}


def _content_hash(source: Path) -> str:
    """A hash over every file of the package, by relative path and content;
    re-read only when a file's size or mtime moved."""
    files = _package_files(source)
    stats = _stat_print(files)
    cached = _HASHED.get(source)
    if cached is not None and cached[0] == stats:
        return cached[1]
    digest = hashlib.sha256()
    for rel_name, path in files:
        digest.update(rel_name.encode())
        digest.update(b"\0")
        try:
            digest.update(path.read_bytes())
        except OSError as exc:
            digest.update(f"unreadable: {exc.strerror}".encode())
        digest.update(b"\0")
    hashed = digest.hexdigest()[:16]
    _HASHED[source] = (stats, hashed)
    return hashed


def code_identity(source: Path | None = None) -> str:
    """The identity of the kew code at ``source`` (default: the running
    package): ``<version>+git:<head>``, or ``<version>+sha256:<hash>`` when it
    is not a checkout's source tree."""
    root = package_dir() if source is None else source
    head = _git_head(root)
    if head is not None:
        return f"{_version()}+git:{head}"
    return f"{_version()}+sha256:{_content_hash(root)}"


@dataclass(frozen=True)
class SchemaRefusal:
    """A drive refused a ledger whose schema is newer than its own."""

    reason: str
    ledger_schema: int
    drive_schema: int
    #: The drive's code identity: a new kew is a different refusal.
    identity: str
    at: datetime

    def repeats(self, earlier: SchemaRefusal) -> bool:
        """True when ``earlier`` is this same refusal, recorded within
        ``REFUSAL_REPEAT_S`` of it."""
        age = (self.at - earlier.at).total_seconds()
        return (earlier.ledger_schema, earlier.drive_schema, earlier.identity) == (
            self.ledger_schema,
            self.drive_schema,
            self.identity,
        ) and 0 <= age <= REFUSAL_REPEAT_S

    def describe(self) -> str:
        return (
            f"{self.reason} (ledger schema {self.ledger_schema}, drive schema "
            f"{self.drive_schema}, kew {self.identity})"
        )


def read_refusal(directory: Path) -> SchemaRefusal | None:
    """The refusal recorded in ``directory``, or None (an unreadable record
    reads as none: it can only cost one more restart)."""
    try:
        data = json.loads((directory / DRIVE_REFUSAL).read_text())
        return SchemaRefusal(
            reason=str(data["reason"]),
            ledger_schema=int(data["ledger_schema"]),
            drive_schema=int(data["drive_schema"]),
            identity=str(data["identity"]),
            at=datetime.fromisoformat(data["at"]),
        )
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError) as exc:
        _warn(f"kew drive: ignoring an unreadable refusal record ({exc}).")
        return None


def write_refusal(directory: Path, refusal: SchemaRefusal) -> None:
    """Record ``refusal`` in ``directory``, replacing any earlier one."""
    record = {
        "reason": refusal.reason,
        "ledger_schema": refusal.ledger_schema,
        "drive_schema": refusal.drive_schema,
        "identity": refusal.identity,
        "at": refusal.at.isoformat(),
    }
    path = directory / DRIVE_REFUSAL
    partial = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        partial.write_text(json.dumps(record))
        os.replace(partial, path)
    except OSError as exc:
        partial.unlink(missing_ok=True)
        _warn(f"kew drive: could not record the refusal ({exc}).")


def clear_refusal(directory: Path) -> None:
    """Forget the recorded refusal: the drive's kew changed or was reinstalled."""
    try:
        (directory / DRIVE_REFUSAL).unlink(missing_ok=True)
    except OSError as exc:
        _warn(f"kew drive: could not clear the refusal record ({exc}).")


@dataclass(frozen=True)
class CodeMove:
    """Why the drive must stop launching, and whether a re-exec can cure it."""

    reason: str
    #: False when only the ledger moved: this drive's own kew is unchanged, so
    #: a re-exec would load the same code and refuse the same ledger.
    reexec: bool
    #: Set when ``reexec`` is False: the refusal to record beside the ledger.
    refusal: SchemaRefusal | None = None


class CodeWatch:
    """The drive's code identity at start, and the first sign it moved."""

    def __init__(
        self,
        schema_now: Callable[[], int],
        schema: int,
        source: Path | None = None,
    ) -> None:
        self._schema_now, self._schema, self._source = schema_now, schema, source
        self.started = code_identity(source)

    def moved(self) -> CodeMove | None:
        """Why the drive must restart, or None while its code still stands. A
        ledger a newer kew migrated counts: a pinned drive keeps its own
        identity, yet can no longer open the ledger its children write."""
        changed = code_identity(self._source) != self.started
        schema = self._schema_now()
        if schema > self._schema:
            newer = f"ledger schema {schema} is newer than this drive's {self._schema}"
            if changed:
                return CodeMove(f"{newer}: restarting", reexec=True)
            reason = (
                f"{newer} and the drive's own kew has not changed: "
                "upgrade the kew the drive runs"
            )
            refusal = SchemaRefusal(
                reason, schema, self._schema, self.started, datetime.now(UTC)
            )
            return CodeMove(reason, reexec=False, refusal=refusal)
        if changed:
            return CodeMove("kew changed under the drive: restarting", reexec=True)
        return None
