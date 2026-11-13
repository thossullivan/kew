"""The docs nav lists each page once, and every docs page is in it or excluded.

A page listed twice shows twice in the sidebar with one of the entries never
marked active. A page that is neither in the nav nor matched by
``exclude_docs`` is still built, indexed and served, but no reader can reach it
from the navigation (the ten design records were in that state).
"""

from __future__ import annotations

import posixpath
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]

# Every test here exercises the tree-wide nav scan (the real tree, or the same
# scan over a synthetic tree), so the whole module runs under `-m policy`.
pytestmark = pytest.mark.policy


def _nav_files(node: Any) -> list[str]:
    """Every docs file a nav names, normalised, in order, duplicates kept.

    ``./cli/loop.md`` and ``cli/loop.md`` are one file to MkDocs, so they are
    one file here.
    """
    if isinstance(node, str):
        if "://" in node:
            return []
        return [posixpath.normpath(node).lstrip("/")]
    if isinstance(node, list):
        return [path for item in node for path in _nav_files(item)]
    if isinstance(node, dict):
        return [path for value in node.values() for path in _nav_files(value)]
    return []


def _exclude_patterns(config: dict[str, Any]) -> list[str]:
    raw = config.get("exclude_docs") or ""
    patterns = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # This reader implements the gitignore rules mkdocs.yml uses: `*` and
        # `?` stay inside one path segment, `**` crosses segments, and a
        # leading or inner `/` anchors at the docs root. A negation or a
        # character class would need more; refuse it rather than misread it.
        assert not line.startswith("!") and "[" not in line, (
            f"unsupported exclude_docs pattern {line!r}"
        )
        patterns.append(line)
    return patterns


def _glob_regex(glob: str) -> str:
    """The regex for one gitignore glob, with `*` kept inside a segment."""
    out = []
    i = 0
    while i < len(glob):
        if glob.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif glob.startswith("**", i):
            out.append(".*")
            i += 2
        elif glob[i] == "*":
            out.append("[^/]*")
            i += 1
        elif glob[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(glob[i]))
            i += 1
    return "".join(out)


def _excluded(page: str, patterns: list[str]) -> bool:
    """Whether a gitignore pattern matches the page or a directory above it."""
    parts = page.split("/")
    # "design", "design/record.md": a pattern naming a directory excludes its pages.
    prefixes = ["/".join(parts[:end]) for end in range(1, len(parts) + 1)]
    for pattern in patterns:
        directory_only = pattern.endswith("/")
        glob = pattern.rstrip("/")
        anchored = glob.startswith("/") or "/" in glob
        regex = _glob_regex(glob.lstrip("/"))
        if not anchored:
            regex = f"(?:.*/)?{regex}"
        candidates = prefixes[:-1] if directory_only else prefixes
        if any(re.fullmatch(regex, candidate) for candidate in candidates):
            return True
    return False


def nav_problems(config_text: str, docs_dir: Path) -> list[str]:
    """The duplicates, missing files and unreachable pages of one mkdocs config."""
    config = yaml.safe_load(config_text)
    listed = _nav_files(config.get("nav") or [])
    problems = [
        f"{page} appears {listed.count(page)} times in the nav"
        for page in sorted(set(listed))
        if listed.count(page) > 1
    ]
    problems += [
        f"{page} is in the nav but has no file"
        for page in sorted(set(listed))
        if not (docs_dir / page).is_file()
    ]
    patterns = _exclude_patterns(config)
    for path in sorted(docs_dir.rglob("*.md")):
        page = path.relative_to(docs_dir).as_posix()
        if page not in listed and not _excluded(page, patterns):
            problems.append(f"{page} is neither in the nav nor excluded")
    return problems


def _docs_tree(tmp_path: Path, *pages: str) -> Path:
    docs = tmp_path / "docs"
    for page in pages:
        (docs / page).parent.mkdir(parents=True, exist_ok=True)
        (docs / page).write_text("# page\n")
    return docs


def test_a_page_listed_twice_is_reported(tmp_path: Path) -> None:
    docs = _docs_tree(tmp_path, "index.md", "cli/loop.md")
    config = (
        "nav:\n"
        "  - Intro: index.md\n"
        "  - CLI:\n"
        "    - loop pause: cli/loop.md\n"
        "    - loop resume: cli/loop.md\n"
    )

    assert nav_problems(config, docs) == ["cli/loop.md appears 2 times in the nav"]


def test_a_page_outside_the_nav_is_reported_unless_excluded(tmp_path: Path) -> None:
    docs = _docs_tree(
        tmp_path,
        "index.md",
        "design/record.md",
        "audit/pack.md",
        "vhs/notes.md",
        "brand-guidelines.md",
    )
    config = (
        "nav:\n"
        "  - Intro: index.md\n"
        "exclude_docs: |\n"
        "  /audit/**\n"
        "  /vhs/**\n"
        "  /brand-guidelines.md\n"
    )

    assert nav_problems(config, docs) == [
        "design/record.md is neither in the nav nor excluded"
    ]


def test_a_star_in_exclude_docs_does_not_cross_a_slash(tmp_path: Path) -> None:
    # MkDocs reads `/d*.md` with gitignore rules: it excludes `docs.md` at the
    # root and still builds `design/record.md`.
    docs = _docs_tree(tmp_path, "index.md", "docs.md", "design/record.md")
    config = "nav:\n  - Intro: index.md\nexclude_docs: |\n  /d*.md\n"

    assert nav_problems(config, docs) == [
        "design/record.md is neither in the nav nor excluded"
    ]


def test_two_spellings_of_one_page_are_one_duplicate(tmp_path: Path) -> None:
    docs = _docs_tree(tmp_path, "index.md", "cli/loop.md")
    config = (
        "nav:\n"
        "  - Intro: index.md\n"
        "  - loop pause: cli/loop.md\n"
        "  - loop resume: ./cli/loop.md\n"
    )

    assert nav_problems(config, docs) == ["cli/loop.md appears 2 times in the nav"]


def test_a_nav_entry_without_a_file_is_reported(tmp_path: Path) -> None:
    docs = _docs_tree(tmp_path, "index.md")
    config = "nav:\n  - Intro: index.md\n  - Gone: cli/removed.md\n"

    assert nav_problems(config, docs) == [
        "cli/removed.md is in the nav but has no file"
    ]


def test_an_external_nav_link_is_not_a_page(tmp_path: Path) -> None:
    docs = _docs_tree(tmp_path, "index.md")
    config = (
        "nav:\n"
        "  - Intro: index.md\n"
        "  - Home: https://kew.dev/\n"
        "  - Again: https://kew.dev/\n"
    )

    assert nav_problems(config, docs) == []


def test_the_docs_nav_lists_every_page_once() -> None:
    config = (REPO_ROOT / "mkdocs.yml").read_text(encoding="utf-8")

    assert nav_problems(config, REPO_ROOT / "docs") == []
