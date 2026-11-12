"""Guard: runtime messages send users to PyPI and kew.dev, never to this repo.

The public launch ships from a new repository (#1994), so an install hint
naming a git install of the development repository, or a docs link on the
old thossullivan.github.io site, is broken for every public user (#2004).
"""

import re
from pathlib import Path

import pytest

from kew.links import DOCS_BASE_URL, UPGRADE_COMMAND, UPGRADE_HINT, docs_url

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src" / "kew"

_FORBIDDEN = (
    re.compile(r"thossullivan\.github\.io", re.IGNORECASE),
    re.compile(r"git\+(?:https?|ssh)://(?:git@)?github\.com[/:]thossullivan/kew", re.I),
    re.compile(r"pip install\s+[\"']?git\+", re.IGNORECASE),
)


def _source_texts() -> list[tuple[Path, str]]:
    texts: list[tuple[Path, str]] = []
    for path in sorted(SOURCE_ROOT.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        try:
            texts.append((path, path.read_text(encoding="utf-8")))
        except UnicodeDecodeError:
            continue  # fonts and other binary assets carry no user text
    return texts


@pytest.mark.policy
def test_no_source_file_points_users_at_a_git_install_or_the_old_docs_site():
    offenders = [
        f"{path.relative_to(SOURCE_ROOT)}:{text.count(chr(10), 0, match.start()) + 1}"
        f": {match.group(0)}"
        for path, text in _source_texts()
        for pattern in _FORBIDDEN
        for match in pattern.finditer(text)
    ]
    assert not offenders, "stale install or docs link in kew/src:\n" + "\n".join(
        offenders
    )


@pytest.mark.policy
def test_guard_scans_python_sources():
    scanned = {path.name for path, _ in _source_texts()}
    assert {"staleness.py", "links.py", "serve.py"} <= scanned


def test_public_links_name_pypi_and_kew_dev():
    assert UPGRADE_COMMAND == "uv tool install --upgrade kew-cli"
    assert DOCS_BASE_URL == "https://kew.dev/docs"
    assert docs_url("authentication") == "https://kew.dev/docs/authentication/"


def test_upgrade_hint_names_pypi_and_a_source_reinstall_without_a_git_url():
    # kew-cli is not on PyPI until launch; a source install must still get a
    # true instruction, and the development repo's git URL 404s publicly.
    assert UPGRADE_HINT == (
        "uv tool install --upgrade kew-cli "
        "(if you installed kew from a source checkout, reinstall it from that checkout)"
    )
    assert UPGRADE_HINT.startswith(UPGRADE_COMMAND)
    assert "\n" not in UPGRADE_HINT
    for forbidden in ("git+", "github.com", "http://", "https://"):
        assert forbidden not in UPGRADE_HINT
    assert not any(pattern.search(UPGRADE_HINT) for pattern in _FORBIDDEN)


@pytest.mark.policy
def test_user_facing_messages_print_the_hint_not_the_bare_command():
    bare = [
        f"{path.relative_to(SOURCE_ROOT)}:{text.count(chr(10), 0, match.start()) + 1}"
        for path, text in _source_texts()
        if path.suffix == ".py" and path.name != "links.py"
        for match in re.finditer(r"\{UPGRADE_COMMAND\}", text)
    ]
    assert not bare, "print UPGRADE_HINT so source installs see a true fix:\n" + (
        "\n".join(bare)
    )
