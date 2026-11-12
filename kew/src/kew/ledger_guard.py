"""Detect forked dispatch ledgers under one project root (#1156).

A relative ``logging.dir`` used to resolve against the cwd, so ``cd``-ing into a
subdirectory silently minted a second ``logs/dispatch/dispatch.db``: two live
audit chains, a false ``COUNT-SHRINK`` whenever ``kew audit verify`` picked the
wrong one, and a daily budget cap you could reset by changing directory. The
path bug itself is fixed in :func:`kew.config.project_root`; this module reports
the ledgers that fork already left behind, loudly, so an operator merges or
archives them instead of one going on being silently ignored.
"""

from collections.abc import Iterator
from pathlib import Path

DB_NAME = "dispatch.db"

# The forks seen in the wild sit one level down (``kew/``, ``www/``). Three is
# generous while keeping the scan cheap enough to run on every preflight.
MAX_DEPTH = 3

# Never descend into these: huge, and no operator keeps a ledger there.
_PRUNED = frozenset(
    {"node_modules", "__pycache__", "site-packages", "dist", "build", "target"}
)


def _child_dirs(directory: Path) -> Iterator[Path]:
    """Real subdirectories worth descending into (no dotdirs, no symlinks)."""
    try:
        entries = sorted(directory.iterdir())
    except OSError:  # unreadable dir — never break a command over a scan
        return
    for entry in entries:
        if entry.name.startswith(".") or entry.name in _PRUNED:
            continue
        if entry.is_dir() and not entry.is_symlink():
            yield entry


def _is_own_project(directory: Path, root: Path) -> bool:
    """True for a nested directory that carries its own kew.toml.

    Such a directory is its own project (a monorepo with per-service control
    planes), so its ledger is deliberate, not a fork.

    Never the root itself: the root always has the config, and a leftover
    ``root/logs/dispatch`` beside an absolute ``logging.dir`` is a real fork.
    """
    if directory == root:
        return False
    return (directory / "kew.toml").is_file()


def find_stray_ledgers(
    root: Path, canonical: Path, *, max_depth: int = MAX_DEPTH
) -> list[Path]:
    """Every ``logs/dispatch/dispatch.db`` under *root* that is not *canonical*.

    Breadth-first to *max_depth* levels below *root*, so the common one-level
    forks are found without walking the whole tree. Nested directories that are
    their own kew project are skipped — their ledger is intentional.
    """
    target = canonical.resolve()
    resolved_root = root.resolve()
    strays: list[Path] = []
    frontier = [resolved_root]
    for _ in range(max_depth + 1):
        if not frontier:
            break
        next_frontier: list[Path] = []
        for directory in frontier:
            candidate = directory / "logs" / "dispatch" / DB_NAME
            if (
                candidate.is_file()
                and candidate.resolve() != target
                and not _is_own_project(directory, resolved_root)
            ):
                strays.append(candidate)
            next_frontier.extend(_child_dirs(directory))
        frontier = next_frontier
    return strays


def stray_ledger_warning(
    root: Path, canonical: Path, *, max_depth: int = MAX_DEPTH
) -> str | None:
    """A one-shot operator warning naming the forked ledgers, or None when clean."""
    strays = find_stray_ledgers(root, canonical, max_depth=max_depth)
    if not strays:
        return None
    listed = ", ".join(str(path) for path in strays)
    return (
        f"Forked dispatch ledger(s) found under {root}: {listed}. "
        f"kew is reading and writing {canonical} only — runs, spend and audit "
        "events in the other ledger(s) are NOT counted here, and verifying the "
        "chain against them reports a false COUNT-SHRINK. Merge or archive them."
    )
