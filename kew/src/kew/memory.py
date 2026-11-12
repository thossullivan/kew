"""`.kew/memory/` channel guard — the lesson/work change-set separation (spec D8).

Only the classifier survives here. The lessons loader and fenced prompt
injection (spec D6/D7) were never wired into a dispatch path and were deleted
(#1647); the D8 channel separation is enforced in CI by
``scripts/check_memory_channel.py``, which imports :func:`classify_memory_channel`
from this module so the rule has one definition.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

# Directory (repo-relative) holding committed lesson files.
LESSONS_DIR = ".kew/memory"


@dataclass(frozen=True)
class ChannelVerdict:
    """Whether a change set respects the lesson-channel separation (spec D8)."""

    ok: bool
    memory_paths: tuple[str, ...]
    other_paths: tuple[str, ...]
    reason: str


def _under_lessons_dir(path: str) -> bool:
    """True if *path* (repo-relative POSIX) is the lessons dir or a file in it."""
    norm = path.strip().replace("\\", "/")
    while norm.startswith("./"):
        norm = norm[2:]
    return norm == LESSONS_DIR or norm.startswith(f"{LESSONS_DIR}/")


def classify_memory_channel(paths: Iterable[str]) -> ChannelVerdict:
    """Enforce the lesson-channel separation ahead of any distiller (spec D8).

    A change set that edits both ``.kew/memory/`` **and** ordinary work files in
    one shot is the inline-smuggling channel D8 closes: it lets code review of a
    work PR carry a lesson-corpus edit past the reviewer's attention. So a change
    set is valid only when it is *pure* — either every path is under
    ``.kew/memory/`` (a lesson change) or none is (an ordinary work change).

    This is a pure classifier over an already-computed path list; the git plumbing
    that derives per-commit path lists lives in ``scripts/check_memory_channel.py``.
    """
    memory: list[str] = []
    other: list[str] = []
    for raw in paths:
        path = raw.strip().replace("\\", "/")
        if not path:
            continue
        (memory if _under_lessons_dir(path) else other).append(path)
    memory_t, other_t = tuple(memory), tuple(other)
    if memory_t and other_t:
        return ChannelVerdict(
            ok=False,
            memory_paths=memory_t,
            other_paths=other_t,
            reason=(
                "change set mixes lesson files with work files — the "
                "inline-smuggling channel D8 forbids. Split into a lesson-only "
                "change and a work-only change.\n"
                f"  lesson files: {', '.join(memory_t)}\n"
                f"  work files:   {', '.join(other_t)}"
            ),
        )
    kind = "lesson-only" if memory_t else "work-only"
    return ChannelVerdict(
        ok=True,
        memory_paths=memory_t,
        other_paths=other_t,
        reason=f"pure {kind} change set",
    )
