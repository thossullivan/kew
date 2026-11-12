"""Where kew keeps the trees it makes, and when it lets them go.

Every long-lived tree kew creates — a local verify checkout, a sandbox clone,
a reviewer checkout, a switchyard or canary scratch directory — lives under
one root, :func:`work_root`. It used to be the operating system's temp
directory, which macOS cleans on its own schedule: a verify that ran across
that cleanup lost files out of its own virtualenv and reported hundreds of
test failures nothing in the candidate caused (#3171). The default root is
``~/.local/state/kew/tmp.noindex``; Spotlight skips a ``.noindex`` folder, so
a checkout under it is not indexed while a suite runs in it.

The root is ``[paths] work_root`` in kew.toml, or ``KEW_WORK_ROOT``, which
wins as every ``KEW_*`` override does. ``load_config`` hands the configured
value and the project's ledger to :func:`configure`; a caller that never
loaded a config still gets the environment variable or the default.

The default root is shared by every project on the machine, so each tree is
made inside a subdirectory named for the ledger in force: ``<root>/<ledger
id>/``, where the id is the ledger's own identity (the UUID its registry and
its ``ledger_identity`` row carry). A process with no registered ledger in
reach makes its trees under ``<root>/unscoped/``, which no sweep enters.

Retention is kew's own, not the operating system's (:func:`sweep_work_root`).
A sweep only ever lists its own ledger's subdirectory, so another project's
ledger, or a new or replaced one (a new id), never sees a tree it did not
make. A tree the ledger still references in a live or held state is never
removed; any other ``kew-*`` entry is removed once nothing in it has changed
for :data:`RETENTION`. A ledger that cannot be read keeps everything, and so
does a root kew did not create itself (:func:`root_refusal`). Trees made
before this module existed are not moved: they are read from the paths their
run evidence recorded.

This is the one module that may name ``mkdtemp``, ``TemporaryDirectory`` or
``gettempdir`` — ``tests/test_work_dirs_policy.py`` refuses them elsewhere,
apart from a short list of call-local uses.
"""

from __future__ import annotations

import contextlib
import fcntl
import logging
import os
import shutil
import tempfile
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from kew.run_store import AuditEvent, ContinuationRecord, RunRecord

logger = logging.getLogger(__name__)

#: The environment variable that names the work root, over any kew.toml value.
WORK_ROOT_ENV = "KEW_WORK_ROOT"

#: Where trees live when nothing names a root.
DEFAULT_WORK_ROOT = "~/.local/state/kew/tmp.noindex"

#: How long an entry no live ledger row references is kept.
RETENTION = timedelta(days=7)

#: A subdirectory the sweep never enters: lock files whose inode is the lock.
LOCKS_DIR = "locks"

#: The file kew writes into a root it created itself. A sweep refuses any
#: root without it, so a root an operator pointed at their own directory is
#: never swept.
ROOT_MARKER = ".kew-work-root"

#: The subdirectory for trees made with no registered ledger in reach. No
#: sweep enters it: no ledger can say whether its trees are still held.
UNSCOPED = "unscoped"

#: The local merge verify's lease (#3190), directly in the work root, where no
#: sweep looks.
VERIFY_LEASE = "verify.lease"

#: The name every entry kew makes starts with. The sweep removes nothing else.
KEW_PREFIX = "kew-"

#: The suffix of a checkout's claim lock, beside the checkout it claims.
LOCK_SUFFIX = ".lock"

#: Run statuses that hold the tree they reference.
HOLDING_STATUSES: frozenset[str] = frozenset(
    {"pending", "awaiting_publication", "awaiting_continuation"}
)

#: Run error categories that name a held run, whatever its status says.
HOLDING_CATEGORIES: frozenset[str] = frozenset(
    {
        "awaiting_publication",
        "publication_review_required",
        "held_publication",
        "awaiting_continuation",
    }
)

#: The audit events a dispatch binds its checkout path with.
CHECKOUT_EVENTS: tuple[str, ...] = ("dispatch_checkout_bound", "publication_base")

_configured: str | None = None

#: The project root and ledger path :func:`load_config` resolved, if any.
_ledger: tuple[Path, Path] | None = None


def configure(root: str | None, *, ledger: tuple[Path, Path] | None = None) -> None:
    """Record ``[paths] work_root`` and the ``(project root, ledger path)``."""
    global _configured, _ledger
    _configured = root or None
    _ledger = ledger


def configured_root() -> Path:
    """The work root as configured, expanded, without creating it."""
    raw = os.environ.get(WORK_ROOT_ENV) or _configured or DEFAULT_WORK_ROOT
    return Path(raw).expanduser()


def work_root() -> Path:
    """The work root, created private to this user when it is missing.

    Only a root this call creates gets :data:`ROOT_MARKER`; a directory that
    already existed is used as it is, and never swept.
    """
    root = configured_root()
    try:
        root.mkdir(mode=0o700, parents=True)
    except FileExistsError:
        return root
    (root / ROOT_MARKER).write_text("made by kew; trees under it may be swept\n")
    return root


def ledger_scope() -> str:
    """The subdirectory name this process makes its trees under.

    The registered ledger's id when the configured project has a registry
    naming the configured ledger; :data:`UNSCOPED` otherwise.
    """
    if _ledger is None:
        return UNSCOPED
    from kew.ledger_identity import LedgerIdentityError, RegistryRecord, registry_paths

    project, db_path = _ledger
    try:
        registry = registry_paths(project).registry
        if not registry.is_file():
            return UNSCOPED
        record = RegistryRecord.from_json(registry.read_text(encoding="utf-8"))
    except (OSError, ValueError, LedgerIdentityError) as exc:
        logger.warning("work trees are unscoped: unreadable ledger registry: %s", exc)
        return UNSCOPED
    if record.db_path != db_path.resolve():
        return UNSCOPED
    return record.ledger_id


class UnsafeWorkScope(OSError):
    """A ledger scope that is not a real directory directly in the work root."""


def scope_dir() -> Path:
    """This process's ledger subdirectory of the work root, created if missing.

    Refuses (:class:`UnsafeWorkScope`), never replaces, a scope that is a
    symlink or does not resolve directly inside the root: a tree made through
    it would land wherever the link points.
    """
    root = work_root()
    scoped = root / ledger_scope()
    with contextlib.suppress(FileExistsError):
        scoped.mkdir(mode=0o700)
    problem = scope_problem(root, scoped)
    if problem is not None:
        raise UnsafeWorkScope(f"refusing to make a work tree: {problem}")
    return scoped


def scope_problem(root: Path, scoped: Path) -> str | None:
    """Why *scoped* is not a safe ledger scope of *root*, or ``None``."""
    if scoped.is_symlink():
        return f"{scoped} is a symlink"
    if not scoped.is_dir():
        return f"{scoped} is not a directory"
    if _resolved(scoped).parent != _resolved(root):
        return f"{scoped} does not resolve directly inside {root}"
    return None


def make_work_dir(prefix: str) -> Path:
    """A fresh directory under this ledger's scope that its caller must remove."""
    return Path(tempfile.mkdtemp(prefix=prefix, dir=scope_dir()))


@contextmanager
def work_tempdir(prefix: str) -> Iterator[str]:
    """A call-local directory under this ledger's scope, removed on exit."""
    with tempfile.TemporaryDirectory(prefix=prefix, dir=scope_dir()) as directory:
        yield directory


def lock_path(name: str) -> Path:
    """A lock file's path, shared by every project; the sweep never enters it."""
    locks = work_root() / LOCKS_DIR
    locks.mkdir(mode=0o700, exist_ok=True)
    return locks / name


def verify_lease_path() -> Path:
    """The lease every local merge verify holds while it runs (#3190)."""
    return work_root() / VERIFY_LEASE


def os_temp_dir() -> Path:
    """The operating system's temp directory, for diagnostics that name it."""
    return Path(tempfile.gettempdir())


class TreeLedger(Protocol):
    """What the keep rule reads from the run ledger."""

    def read_ledger_id(self) -> str | None: ...

    def list_all(self) -> list[RunRecord]: ...

    def list_audit_events(
        self, limit: int | None = 100, *, event: str | None = None
    ) -> list[AuditEvent]: ...

    def list_all_continuations(self) -> list[ContinuationRecord]: ...

    def continuation_was_abandoned(self, record: ContinuationRecord) -> bool: ...


def run_holds_tree(record: RunRecord) -> bool:
    """Whether *record* is in a state that keeps the tree it references."""
    return (
        record.status in HOLDING_STATUSES or record.error_category in HOLDING_CATEGORIES
    )


def referenced_trees(store: TreeLedger) -> set[Path]:
    """Every path a holding run or an unabandoned continuation references."""
    paths = _held_checkouts(store) | {
        continuation.worktree_identity
        for continuation in store.list_all_continuations()
        if not store.continuation_was_abandoned(continuation)
    }
    return {_resolved(Path(path)) for path in paths}


def _held_checkouts(store: TreeLedger) -> set[str]:
    """The checkout paths bound by runs in a holding state."""
    holding = {record.run_id for record in store.list_all() if run_holds_tree(record)}
    found: set[str] = set()
    for event in CHECKOUT_EVENTS:
        for recorded in store.list_audit_events(limit=None, event=event):
            checkout = (recorded.payload or {}).get("checkout")
            if recorded.run_id in holding and isinstance(checkout, str) and checkout:
                found.add(checkout)
    return found


def _resolved(path: Path) -> Path:
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        return path.absolute()


def root_refusal(root: Path, project: Path) -> str | None:
    """Why a sweep must not touch *root*, or ``None`` when it may.

    Refused, whatever its marker says: a relative root, the filesystem root,
    the home directory or any ancestor of it, and *project* (the checkout) or
    any ancestor of it. Otherwise refused unless kew created the root itself
    and left :data:`ROOT_MARKER` in it.
    """
    if not root.is_absolute():
        return f"{root} is a relative path"
    resolved = _resolved(root)
    if resolved == Path(resolved.anchor):
        return f"{root} is the filesystem root"
    if _resolved(Path.home()).is_relative_to(resolved):
        return f"{root} is the home directory or one of its ancestors"
    if _resolved(project).is_relative_to(resolved):
        return f"{root} is the project checkout or one of its ancestors"
    if not (resolved / ROOT_MARKER).is_file():
        return f"{root} has no {ROOT_MARKER}: kew did not create it"
    return None


def _changed_since(entry: Path, limit: float) -> bool:
    """Whether *entry*, or anything anywhere under it, changed at or after *limit*.

    Walks the whole tree without following a symlink, and stops at the first
    entry that is new enough.
    """
    if entry.lstat().st_mtime >= limit:
        return True
    if entry.is_symlink() or not entry.is_dir():
        return False
    pending = [entry]
    while pending:
        with os.scandir(pending.pop()) as children:
            for child in children:
                if child.stat(follow_symlinks=False).st_mtime >= limit:
                    return True
                if child.is_dir(follow_symlinks=False):
                    pending.append(Path(child.path))
    return False


def _referenced(entry: Path, referenced: Iterable[Path]) -> bool:
    resolved = _resolved(entry)
    return any(path == resolved or path.is_relative_to(resolved) for path in referenced)


def sweep_work_root(
    store: TreeLedger | None,
    *,
    project: Path,
    now: float | None = None,
    retention: timedelta = RETENTION,
    root: Path | None = None,
) -> list[Path]:
    """Remove every expired ``kew-*`` entry of *store*'s scope no live row holds.

    *project* is the checkout the sweep runs for; a root that is it or one of
    its ancestors is refused (:func:`root_refusal`). Returns what was
    removed. With no ledger, a ledger with no identity or one that cannot be
    read, or a refused root, nothing is removed: a tree kew cannot prove
    unreferenced is kept.
    """
    base = configured_root() if root is None else root
    if store is None or not base.is_dir():
        return []
    refusal = root_refusal(base, project)
    if refusal is not None:
        logger.warning("work root sweep refused: %s", refusal)
        return []
    try:
        ledger_id = store.read_ledger_id()
        referenced = referenced_trees(store)
    except Exception as exc:  # noqa: BLE001 — fail closed on any ledger fault
        logger.warning("work root sweep skipped: the ledger is unreadable: %s", exc)
        return []
    if not ledger_id:
        logger.warning("work root sweep skipped: the ledger has no identity")
        return []
    scoped = base / ledger_id
    if not os.path.lexists(scoped):
        return []
    problem = scope_problem(base, scoped)
    if problem is not None:
        logger.warning("work root sweep refused: %s", problem)
        return []
    limit = (time.time() if now is None else now) - retention.total_seconds()
    return _sweep_scope(scoped, limit, referenced)


def _sweep_scope(scoped: Path, limit: float, referenced: set[Path]) -> list[Path]:
    """Remove the expired, unreferenced trees of one scope, then their locks."""
    trees, locks = _kew_entries(scoped)
    expired = [e for e in trees if _sweepable(e, limit, referenced)]
    removed = [e for e in expired if _remove_unclaimed(e)]
    gone = set(removed)
    released = [e for e in locks if _release(e, limit, referenced, gone)]
    return removed + released


def _kew_entries(scoped: Path) -> tuple[list[Path], list[Path]]:
    """The ``kew-*`` entries of a scope: trees and files, then claim locks."""
    entries = [e for e in sorted(scoped.iterdir()) if e.name.startswith(KEW_PREFIX)]
    locks = [e for e in entries if _is_lock(e)]
    return [e for e in entries if e not in locks], locks


def _sweepable(entry: Path, limit: float, referenced: set[Path]) -> bool:
    """Unreferenced first, so a held tree is never walked; then expired."""
    return not _referenced(entry, referenced) and _expired(entry, limit)


def _remove_unclaimed(entry: Path) -> bool:
    """Remove *entry* while holding its claim lock, if it has one.

    A tree whose ``<tree>.lock`` another process holds is a live claim, so the
    tree is kept. The lock is held through the removal, so no claimer can take
    the tree while it is being deleted.
    """
    lock = entry.with_name(entry.name + LOCK_SUFFIX)
    if not os.path.lexists(lock):
        _remove(entry)
        return True
    descriptor = _flocked(lock)
    if descriptor is None:
        return False
    try:
        _remove(entry)
    finally:
        os.close(descriptor)
    return True


def _flocked(lock: Path) -> int | None:
    """A descriptor holding *lock*'s ``flock``, or ``None`` when it cannot.

    A lock another process holds, or one that cannot be opened (a symlink is
    never followed), gives ``None``, and the log says why.
    """
    try:
        descriptor = os.open(lock, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        logger.warning("work root sweep kept %s: cannot open it: %s", lock, exc)
        return None
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        logger.warning("work root sweep kept %s: it is held: %s", lock, exc)
        os.close(descriptor)
        return None
    return descriptor


def _release(lock: Path, limit: float, referenced: set[Path], gone: set[Path]) -> bool:
    return _lock_is_free(lock, limit, referenced, gone) and _remove_lock(lock)


def _is_lock(entry: Path) -> bool:
    return entry.name.endswith(LOCK_SUFFIX) and not entry.is_dir()


def _lock_is_free(
    lock: Path, limit: float, referenced: set[Path], gone: set[Path]
) -> bool:
    """Whether a claim lock's checkout no longer needs it.

    A lock is kept while its checkout is referenced, or still there and not
    removed by this pass; and while it is younger than the retention.
    """
    checkout = lock.with_name(lock.name.removesuffix(LOCK_SUFFIX))
    if _referenced(checkout, referenced):
        return False
    if checkout.exists() and checkout not in gone:
        return False
    return _expired(lock, limit)


def _remove_lock(lock: Path) -> bool:
    """Unlink *lock* only while holding it: a lock another process holds stays.

    A claimer that opened the file just before the unlink notices on its own
    and reopens (``kew.worktree.claim_checkout``).
    """
    descriptor = _flocked(lock)
    if descriptor is None:
        return False
    try:
        lock.unlink(missing_ok=True)
    finally:
        os.close(descriptor)
    return True


def _expired(entry: Path, limit: float) -> bool:
    """Whether nothing in *entry* has changed since the *limit* timestamp."""
    try:
        return not _changed_since(entry, limit)
    except OSError as exc:
        # Fail closed: an entry kew cannot stat is kept, and the log says why.
        logger.warning("work root sweep kept %s: cannot read its age: %s", entry, exc)
        return False


def _remove(entry: Path) -> None:
    if entry.is_dir() and not entry.is_symlink():
        shutil.rmtree(entry, ignore_errors=True)
    else:
        entry.unlink(missing_ok=True)
