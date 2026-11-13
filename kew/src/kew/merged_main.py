"""What actually landed on ``main``, read from the local clone (C3/#2301).

The ledger knows every PR kew touched. It cannot know the ones it never saw —
a hand-made PR, a hotfix, a dependency bump — and "merged by kew" means
nothing without that denominator. ``git log origin/main`` has it, locally, with
no GitHub call: a squash merge's subject ends in ``(#N)``, and GitHub writes
that subject itself.

The read is deliberately narrow:

* **origin/main as fetched, not as it is on GitHub.** Nothing here fetches. A
  stale clone reports a stale denominator, which is why the caller is told the
  tip it read (:attr:`MergedMain.head`) rather than left to assume freshness.
* **Bounded.** ``--since`` cuts the walk to the window and ``--max-count``
  caps it regardless; a repository with a million commits costs the same as
  one with a thousand.
* **Cached.** A read endpoint must not fork a subprocess per request: the
  result is memoized per (repo, since, ref) for :data:`CACHE_TTL_S`.
* **Never fatal.** No clone, no ``origin/main``, no git at all: the reader
  returns an unavailable result carrying the reason, and the surface says why
  its chart is empty instead of erroring.
"""

from __future__ import annotations

import logging
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from kew import subprocess_utils

_logger = logging.getLogger(__name__)

#: The ref the denominator is read from when the caller names none — a clone
#: whose ``branches.base`` is set is read at that branch's remote instead
#: (``kew.service.outcomes._base_ref``). ``origin/<branch>`` and not the local
#: branch: a working clone's own may sit behind or ahead of what was merged.
DEFAULT_REF = "origin/main"

#: Hard cap on the walk, whatever the window asks for.
MAX_COMMITS = 5000

#: How long a parsed walk is reused. Short enough that a merge shows up within
#: a poll or two, long enough that a dashboard refresh does not fork git.
CACHE_TTL_S = 60.0

#: Seconds before the walk is abandoned. A hung git read must not hold a
#: request open.
GIT_TIMEOUT_S = 20.0

#: ``...(#1234)`` at the very end of a squash-merge subject. Anchored at the
#: end so a subject that merely mentions an issue mid-sentence is not counted.
_PR_SUFFIX = re.compile(r"\(#(\d+)\)$")

_cache: dict[tuple[str, str, str], tuple[float, MergedMain]] = {}


def _now() -> float:
    """Monotonic clock for the cache TTL (patchable in tests)."""
    return time.monotonic()


@dataclass(frozen=True)
class MergedMain:
    """Distinct PR numbers squash-merged into the ref, bucketed by UTC week."""

    by_week: dict[str, int] = field(default_factory=dict)
    """``{Monday ISO date: distinct PRs}``. Empty when unavailable."""
    head: str | None = None
    """Short id of the ref's tip, so a reader can see what it read."""
    unavailable: str | None = None
    """Why the read produced nothing, in a sentence the console can print.
    None when the walk succeeded — including when it succeeded and found no
    merges, which is a real answer and not an error."""


def week_start(moment: datetime) -> str:
    """ISO date of the Monday of *moment*'s UTC week.

    The same bucket ``run_store_outcomes`` computes in SQL
    (``date(ts, 'weekday 0', '-6 days')``), so a week's ledger numbers and its
    git number are the same seven days.
    """
    utc = moment.astimezone(UTC)
    return (utc.date() - timedelta(days=utc.weekday())).isoformat()


def _parse(stdout: str) -> dict[str, set[int]]:
    """``{week: {pr numbers}}`` from ``%cI\\x09%s`` lines, skipping the rest."""
    found: dict[str, set[int]] = defaultdict(set)
    for line in stdout.splitlines():
        iso, _, subject = line.partition("\t")
        match = _PR_SUFFIX.search(subject.strip())
        if not match:
            continue
        try:
            moment = datetime.fromisoformat(iso.strip())
        except ValueError:
            continue
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        found[week_start(moment)].add(int(match.group(1)))
    return found


def read_merged_main(
    repo_dir: Path | str | None, *, since: datetime, ref: str = DEFAULT_REF
) -> MergedMain:
    """Distinct PRs merged into *ref* on or after *since*, per UTC week.

    Cached for :data:`CACHE_TTL_S` per (repo, since, ref). Any failure — a
    directory that is not a clone, a missing ref, a timeout — comes back as
    :attr:`MergedMain.unavailable` rather than an exception.
    """
    if repo_dir is None:
        return MergedMain(unavailable="no repository directory is configured")
    key = (str(repo_dir), since.astimezone(UTC).isoformat(), ref)
    entry = _cache.get(key)
    if entry is not None and _now() - entry[0] < CACHE_TTL_S:
        return entry[1]
    result = _walk(Path(repo_dir), since=since, ref=ref)
    _cache[key] = (_now(), result)
    return result


def _walk(repo_dir: Path, *, since: datetime, ref: str) -> MergedMain:
    """One ``git log`` walk, parsed. Failures become a printable reason."""
    # The allowlist is populated by ``preflight_tools``, which a serve process
    # runs at startup — but an embedded host that only ever reads need not
    # have, and preflight EXITS on a missing required tool. A report resolves
    # its own tool and prints a sentence instead.
    if subprocess_utils.ensure_tool("git") is None:
        return MergedMain(unavailable="git is not installed, so main cannot be read")
    try:
        completed = subprocess_utils.run(
            [
                "git",
                "log",
                ref,
                f"--since={since.astimezone(UTC).isoformat()}",
                f"--max-count={MAX_COMMITS}",
                "--format=%cI%x09%s",
            ],
            cwd=repo_dir,
            check=True,
            timeout=GIT_TIMEOUT_S,
        )
        head = subprocess_utils.run(
            ["git", "rev-parse", "--short", ref],
            cwd=repo_dir,
            check=True,
            timeout=GIT_TIMEOUT_S,
        )
    except Exception as exc:  # noqa: BLE001 - every failure is the same answer
        _logger.debug("merged-main read failed in %s: %s", repo_dir, exc)
        return MergedMain(
            unavailable=f"{ref} could not be read from the local clone",
        )
    return MergedMain(
        by_week={week: len(prs) for week, prs in _parse(completed.stdout).items()},
        head=head.stdout.strip() or None,
    )


def reset_cache() -> None:
    """Drop the memoized walks. For tests and for a repo-dir change."""
    _cache.clear()
