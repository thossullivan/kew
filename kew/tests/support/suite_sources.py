"""The text of the suite's own Python files, for rules read off their syntax tree.

A policy test that lints how other tests are written (which hook a
``monkeypatch.setattr`` installs, say) parses those modules rather than
running them. Reading them here keeps that one I/O step out of the test
bodies, which then assert on what the parsed tree holds.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent.parent


def suite_source(relative: str) -> str:
    """The text of one file under ``tests/``, named relative to it."""
    return (TESTS_DIR / relative).read_text(encoding="utf-8")


def suite_sources() -> Iterator[tuple[str, str]]:
    """``(relative path, text)`` for every ``.py`` file under ``tests/``, sorted."""
    for path in sorted(TESTS_DIR.rglob("*.py")):
        yield path.relative_to(TESTS_DIR).as_posix(), path.read_text(encoding="utf-8")
