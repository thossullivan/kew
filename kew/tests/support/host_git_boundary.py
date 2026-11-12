"""Subprocess-level detector for the W1.1 host/agent git boundary.

Acceptance criterion 1 of
``docs/design/2026-08-06-host-owned-publication-broker.md`` is:

    No host git process — read or write — resolves a repository inside an
    agent-writable path. Enforced by a subprocess-level assertion, not review
    of known call sites.

"Not review of known call sites" is the load-bearing clause. A hand-maintained
list of ``git push`` sites is exactly what missed the read-side RCE: the
dangerous invocations are ``git diff``/``git status``/``git rev-parse``, which
no writer-focused inventory flags. So this module observes the process boundary
instead:

* :func:`resolve_repository` reimplements git's own repository discovery —
  ``-C``, ``--git-dir``, ``--work-tree``, ``GIT_DIR``, ``GIT_WORK_TREE``,
  gitfile redirection, and the walk-up — so a recorded invocation can be mapped
  to the repository it would actually operate on. Where that answer lands is
  the whole question; ``cwd`` alone does not answer it.
* :func:`record_git_calls` patches the ``subprocess`` module *as seen by*
  ``kew.subprocess_utils``, which is the single chokepoint every kew subprocess
  is required to pass through. Calls are recorded and then delegated, so flows
  under test run for real.

The recorder is only as complete as that chokepoint, which is why
``tests/test_host_git_boundary.py`` also asserts the chokepoint holds.
"""

from __future__ import annotations

import os
import subprocess
import unicodedata
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import kew.subprocess_utils

__all__ = [
    "GitInvocation",
    "ResolvedRepository",
    "record_git_calls",
    "resolve_repository",
]

# git stops parsing its own options at the first non-option token (the
# subcommand). These are the ones that relocate the repository.
_RELOCATING_OPTIONS = frozenset({"-C", "--git-dir", "--work-tree", "--namespace"})

# Global options that take a separate value. Their value must be *consumed*
# even when it does not relocate anything, or parsing mistakes it for the
# subcommand and stops early — so ``git -c a.b=c -C <agent path> status`` would
# resolve to the process cwd and the agent path would go unseen. A false
# negative in this detector is the failure mode that matters.
_VALUE_OPTIONS = _RELOCATING_OPTIONS | frozenset(
    {"-c", "--config-env", "--exec-path", "--super-prefix"}
)


@dataclass(frozen=True)
class ResolvedRepository:
    """Where a git invocation would actually operate.

    ``work_tree`` is the working directory git would act on — the discovered
    toplevel, or an explicit ``--work-tree``/``GIT_WORK_TREE``. ``git_dir`` is
    the metadata directory, which for a linked worktree lives elsewhere
    entirely. Both are answers to "what did this process get access to", so
    both are checked.
    """

    git_dir: Path | None
    work_tree: Path | None
    discovery_start: Path
    discovered: bool

    def paths(self) -> tuple[Path, ...]:
        """Every filesystem location this invocation binds itself to."""
        return tuple(p for p in (self.work_tree, self.git_dir) if p is not None)

    def resolves_inside(self, root: Path) -> bool:
        """True when the repository — tree or metadata — lies under *root*."""
        return any(_is_within(path, root) for path in self.paths())


@dataclass(frozen=True)
class GitInvocation:
    """One observed subprocess launch, with the repository it resolved to.

    ``env=None`` means the child inherited the parent environment (no ``env=``
    was passed); ``resolved`` is the repository as it stood *at launch time*.
    """

    argv: tuple[str, ...]
    cwd: Path
    env: Mapping[str, str] | None = None
    resolved: ResolvedRepository | None = None

    @property
    def effective_env(self) -> Mapping[str, str]:
        """What the child actually saw, inheritance included."""
        return os.environ if self.env is None else self.env

    @property
    def is_git(self) -> bool:
        """True when argv[0] is the git binary (absolute after allowlisting)."""
        if not self.argv:
            return False
        return Path(self.argv[0]).name in {"git", "git.exe"}

    @property
    def subcommand(self) -> str | None:
        """The git subcommand, skipping git's own global options."""
        _, rest = _split_global_options(self.argv[1:])
        return rest[0] if rest else None

    def repository(self) -> ResolvedRepository:
        """The repository this call bound itself to, as of launch time.

        Resolution is a filesystem walk, so it must be snapshotted when the
        process starts. Resolving lazily lets a flow that removes the agent's
        worktree on its way out — ``cleanup_worktree`` does exactly that —
        erase its own violations: the walk finds nothing afterwards and the
        call reports clean.
        """
        if self.resolved is not None:
            return self.resolved
        return resolve_repository(self.argv, self.cwd, self.env)

    def describe(self) -> str:
        """One line naming the call and where it landed — for failure output."""
        repo = self.repository()
        where = ", ".join(str(p) for p in repo.paths()) or "<no repository>"
        return f"{' '.join(self.argv[1:])!r} (cwd={self.cwd}) -> {where}"


def _normalize(name: str) -> str:
    return unicodedata.normalize("NFC", name)


def _canonical(path: Path) -> Path:
    """Resolve symlinks and ``..`` so path comparison is by real location.

    Unicode normalization matches the filter rules in D4: a decomposed path
    name is the same path.
    """
    try:
        return Path(_normalize(str(path))).resolve()
    except OSError:  # pragma: no cover — resolve() on a broken mount
        return path


def _is_within(candidate: Path, root: Path) -> bool:
    """True when *candidate* is *root* or lies beneath it.

    Both sides are canonicalized, so a symlinked or ``..``-laden path cannot
    claim to be outside a root it actually reaches.
    """
    resolved = _canonical(candidate)
    base = _canonical(root)
    return resolved == base or base in resolved.parents


def _split_global_options(
    args: Sequence[str],
) -> tuple[list[tuple[str, str]], list[str]]:
    """Split git's global options from the subcommand and its arguments.

    Returns ``([(option, value), ...], remaining)`` where the options are only
    the ones that relocate the repository. Mirrors git: option parsing stops at
    the first token that is not an option. Every value-bearing option consumes
    its value, so a later ``-C`` is still seen.
    """
    options: list[tuple[str, str]] = []
    index = 0
    while index < len(args):
        token = args[index]
        if not token.startswith("-"):
            break
        name, sep, inline = token.partition("=")
        if name not in _VALUE_OPTIONS:
            index += 1
            continue
        if sep:
            value, index = inline, index + 1
        elif index + 1 < len(args):
            value, index = args[index + 1], index + 2
        else:  # trailing option with no value — git errors; record nothing
            index += 1
            continue
        if name in _RELOCATING_OPTIONS:
            options.append((name, value))
    return options, list(args[index:])


def _read_gitfile(gitfile: Path) -> Path | None:
    """Follow a ``.git`` *file* (linked worktree / submodule) to its gitdir."""
    try:
        text = gitfile.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        prefix, sep, target = line.partition(":")
        if sep and prefix.strip() == "gitdir":
            candidate = Path(target.strip())
            if not candidate.is_absolute():
                candidate = gitfile.parent / candidate
            return candidate
    return None


def _discover(start: Path) -> tuple[Path | None, Path | None]:
    """Walk up from *start* the way git does. Returns ``(git_dir, work_tree)``."""
    current = start
    for directory in (current, *current.parents):
        dot_git = directory / ".git"
        if dot_git.is_dir():
            return dot_git, directory
        if dot_git.is_file():
            return _read_gitfile(dot_git), directory
    return None, None


def resolve_repository(
    argv: Sequence[str],
    cwd: Path | str | None,
    env: Mapping[str, str] | None = None,
) -> ResolvedRepository:
    """Resolve the repository a git invocation would operate on.

    Precedence follows git: ``-C`` relocates the starting directory (and is
    cumulative), explicit ``--git-dir``/``--work-tree`` and their ``GIT_*``
    environment equivalents override discovery, and otherwise git walks up from
    the effective directory looking for ``.git``.

    ``env=None`` means the process **inherits** the current environment, which
    is what a ``subprocess`` call with no ``env=`` does. Pass ``{}`` to model a
    genuinely empty environment. Recording ``None`` as empty would hide an
    inherited ``GIT_DIR``.
    """
    env = os.environ if env is None else env
    start = Path(cwd) if cwd is not None else Path.cwd()
    options, _ = _split_global_options(list(argv[1:]))

    git_dir_opt: str | None = None
    work_tree_opt: str | None = None
    for name, value in options:
        if name == "-C":
            start = start / value if not Path(value).is_absolute() else Path(value)
            # git chdir's on each -C, so each hop resolves through whatever
            # symlinks it lands on. Canonicalize as we go: discovery walks
            # ``.parents``, which is lexical, and would otherwise walk the
            # symlink's *textual* ancestors and find the wrong repository.
            start = _canonical(start)
        elif name == "--git-dir":
            git_dir_opt = value
        elif name == "--work-tree":
            work_tree_opt = value

    def _anchor(value: str | None) -> Path | None:
        if value is None:
            return None
        path = Path(value)
        return path if path.is_absolute() else start / path

    # argv wins over the environment, as it does in git.
    git_dir = _anchor(git_dir_opt) or _anchor(env.get("GIT_DIR"))
    work_tree = _anchor(work_tree_opt) or _anchor(env.get("GIT_WORK_TREE"))

    if git_dir is not None or work_tree is not None:
        # An explicit --git-dir with no --work-tree makes cwd the work tree.
        return ResolvedRepository(
            git_dir=git_dir,
            work_tree=work_tree if work_tree is not None else start,
            discovery_start=start,
            discovered=False,
        )

    found_git_dir, found_work_tree = _discover(start)
    return ResolvedRepository(
        git_dir=found_git_dir,
        work_tree=found_work_tree,
        discovery_start=start,
        discovered=found_work_tree is not None,
    )


class _RecordingSubprocess:
    """Stand-in for the ``subprocess`` module inside ``kew.subprocess_utils``.

    Records every launch, then delegates to what it replaced: the real module,
    or a fake a test already installed there (a forge that answers ``gh``).
    Attribute access falls through, so exception types and constants keep
    working.
    """

    def __init__(self, calls: list[GitInvocation], inner: Any = subprocess) -> None:
        self._calls = calls
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def _record(self, args: Any, kwargs: dict[str, Any]) -> None:
        argv = args[0] if args else kwargs.get("args")
        if isinstance(argv, str):  # shell=True — one string, not a vector
            argv = [argv]
        if not isinstance(argv, (list, tuple)):
            return
        cwd = kwargs.get("cwd")
        env = kwargs.get("env")
        call = GitInvocation(
            argv=tuple(str(part) for part in argv),
            cwd=Path(cwd) if cwd is not None else Path.cwd(),
            # None is meaningful: the child inherits this process's environment.
            env=dict(env) if env is not None else None,
        )
        if call.is_git:
            # Snapshot before the process runs. Afterwards the repository may
            # not exist to resolve.
            call = replace(call, resolved=call.repository())
        self._calls.append(call)

    def run(self, *args: Any, **kwargs: Any) -> Any:
        self._record(args, kwargs)
        return self._inner.run(*args, **kwargs)  # nosec B603 — delegating recorder

    def Popen(self, *args: Any, **kwargs: Any) -> Any:  # noqa: N802 — mirrors API
        self._record(args, kwargs)
        return self._inner.Popen(*args, **kwargs)  # nosec B603 — delegating recorder


@contextmanager
def record_git_calls() -> Iterator[list[GitInvocation]]:
    """Record every subprocess kew launches while the block runs.

    Patches the ``subprocess`` reference held by ``kew.subprocess_utils``, the
    chokepoint every kew subprocess is required to pass through. Calls still
    execute, so flows under test do real work against real repositories.
    """
    calls: list[GitInvocation] = []
    original = kew.subprocess_utils.subprocess
    kew.subprocess_utils.subprocess = _RecordingSubprocess(calls, original)  # type: ignore[assignment]
    try:
        yield calls
    finally:
        kew.subprocess_utils.subprocess = original  # type: ignore[assignment]
