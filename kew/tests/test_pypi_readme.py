"""Guards for the PyPI landing page (kew/README.md, [project] readme)."""

import re
import tomllib
from pathlib import Path

README_PATH = Path(__file__).resolve().parents[1] / "README.md"
PYPROJECT_PATH = README_PATH.parent / "pyproject.toml"
ROOT_LICENSE_PATH = README_PATH.parents[1] / "LICENSE"
SHIPPED_RUNNERS = {
    "claude-code",
    "codex",
    "opencode",
    "pi",
    "hermes",
    "lemonade",
}

# Verbatim from the packaging spec — do not reword.
NAMING_CAVEAT = (
    "`kew-cli` installs the `kew` command. The unrelated `kew` package "
    "on PyPI is a different project."
)


def _readme() -> str:
    return README_PATH.read_text(encoding="utf-8")


def test_naming_caveat_verbatim() -> None:
    assert NAMING_CAVEAT in _readme()


def test_install_command_is_pipx() -> None:
    assert "pipx install kew-cli" in _readme()


def test_all_markdown_links_are_absolute() -> None:
    """Relative links break when rendered on pypi.org."""
    links = re.findall(r"\[[^\]]*\]\(([^)]+)\)", _readme())
    relative = [
        target
        for target in links
        if not target.startswith(("https://", "http://", "#"))
    ]
    assert not relative, f"relative links would break on PyPI: {relative}"


def test_stub_repo_root_link_removed() -> None:
    assert "../README.md" not in _readme()


def test_license_matches_metadata_and_root_license() -> None:
    with PYPROJECT_PATH.open("rb") as f:
        license_name = tomllib.load(f)["project"]["license"]
    assert license_name == "Apache-2.0"
    assert "Apache License" in ROOT_LICENSE_PATH.read_text(encoding="utf-8")
    assert "Apache-2.0 licensed." in _readme()


def test_all_shipped_runners_are_named() -> None:
    text = _readme().lower()
    assert all(runner in text for runner in SHIPPED_RUNNERS)


def test_documentation_link_is_canonical() -> None:
    assert "[Documentation](https://kew.dev/docs/)" in _readme()
