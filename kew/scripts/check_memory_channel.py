"""Lesson-channel guard (spec D8) — reject PRs that mix `.kew/memory/` with work.

The dispatch prompt injects the committed ``.kew/memory/`` lesson corpus as
*untrusted data*. A change set that edits a lesson file **and** ordinary work
files in one shot is the inline-smuggling channel D8 closes: a work PR could
carry a lesson-corpus edit past a reviewer focused on the code. This gate fails
any commit whose changed-file set mixes the two.

Enforcement is **per commit**, not per aggregate diff: a PR may carry a work
commit *and* a lesson commit as long as each commit is pure. That is exactly how
Task 8 bootstraps — the guard's own machinery lands in a work commit and the
first lesson lands in a separate lesson-only commit within the same PR, each
passing this gate.

Usage:
  * ``check_memory_channel.py <path> [<path> ...]`` — classify an explicit path
    list (used by tests and for manual checks); exits non-zero on a mixed set.
  * ``check_memory_channel.py`` (no args) — derive the per-commit changed-file
    lists for every commit in ``<base>..HEAD`` and classify each. The base ref is
    ``$MEMORY_GUARD_BASE`` if set, else the first of ``origin/main`` / ``main``
    that resolves. No commits ahead of base (e.g. on ``main`` itself) => pass.

Kept stdlib-only and dependency-light so it runs in a bare CI step.
"""

from __future__ import annotations

import os
import subprocess
import sys

from kew.memory import classify_memory_channel
from kew.subprocess_utils import preflight_tools, run

# The allowlist is process-local and empty until resolved; standalone scripts
# must populate it before any run() call (kew's CLI does this at startup). The
# guard only runs git, so a host without the GitHub CLI runs it too.
preflight_tools(required=("git",))

_BASE_CANDIDATES = ("origin/main", "main")


def _git(*args: str) -> str:
    """Run ``git`` via the repo's allowlisted subprocess helper."""
    return run(["git", *args], capture=True, check=True).stdout.strip()


def _ref_exists(ref: str) -> bool:
    try:
        _git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
        return True
    except subprocess.CalledProcessError:
        return False


def _resolve_base() -> str | None:
    env = os.environ.get("MEMORY_GUARD_BASE")
    if env:
        if _ref_exists(env):
            return env
        # An explicitly configured base that doesn't resolve must fail loudly:
        # silently returning None here would report "nothing to check" and turn
        # a typo'd MEMORY_GUARD_BASE into a disabled guard (fail-open).
        print(
            f"memory-channel guard: configured MEMORY_GUARD_BASE {env!r}"
            " does not resolve to a commit",
            file=sys.stderr,
        )
        raise SystemExit(2)
    for cand in _BASE_CANDIDATES:
        if _ref_exists(cand):
            return cand
    return None


def _commits_ahead(base: str) -> list[str]:
    merge_base = _git("merge-base", base, "HEAD")
    out = _git("rev-list", f"{merge_base}..HEAD")
    return [line for line in out.splitlines() if line]


def _files_in_commit(sha: str) -> list[str]:
    out = _git("diff-tree", "--no-commit-id", "--name-only", "-r", sha)
    return [line for line in out.splitlines() if line]


def _check_explicit(paths: list[str]) -> int:
    verdict = classify_memory_channel(paths)
    if not verdict.ok:
        print(f"memory-channel guard: {verdict.reason}", file=sys.stderr)
        return 1
    print(f"memory-channel guard: OK ({verdict.reason})")
    return 0


def _check_history() -> int:
    base = _resolve_base()
    if base is None:
        print("memory-channel guard: no base ref (origin/main/main) — nothing to check")
        return 0
    commits = _commits_ahead(base)
    if not commits:
        print(f"memory-channel guard: no commits ahead of {base} — OK")
        return 0
    failures: list[str] = []
    for sha in commits:
        verdict = classify_memory_channel(_files_in_commit(sha))
        if not verdict.ok:
            failures.append(f"commit {sha[:12]}: {verdict.reason}")
    if failures:
        print("memory-channel guard: mixed commit(s) found:", file=sys.stderr)
        for line in failures:
            print(f"  {line}", file=sys.stderr)
        return 1
    print(f"memory-channel guard: {len(commits)} commit(s) ahead of {base} — all pure")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    return _check_explicit(args) if args else _check_history()


if __name__ == "__main__":
    raise SystemExit(main())
