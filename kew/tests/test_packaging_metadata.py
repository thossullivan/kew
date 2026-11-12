"""Guards on the kew-cli distribution metadata in pyproject.toml."""

import re
import subprocess
import tomllib
from pathlib import Path
from typing import Any

PYPROJECT_PATH = Path(__file__).resolve().parents[1] / "pyproject.toml"
REPO_ROOT = PYPROJECT_PATH.parents[1]


def _project() -> dict[str, Any]:
    with PYPROJECT_PATH.open("rb") as f:
        data: dict[str, Any] = tomllib.load(f)
    return data["project"]


def test_author_has_contact_email() -> None:
    authors = _project()["authors"]
    assert {"name": "Tom Sullivan", "email": "tom@tomsullivan.dev"} in authors


def test_documentation_and_changelog_urls() -> None:
    urls = _project()["urls"]
    assert urls["Documentation"] == "https://kew.dev/docs/"
    # The changelog lives at the REPO ROOT, where the Release workflow reads
    # each version's notes from, not at kew/CHANGELOG.md.
    assert urls["Changelog"] == (
        "https://github.com/thossullivan/kew/blob/main/CHANGELOG.md"
    )


def test_environment_console_classifier() -> None:
    assert "Environment :: Console" in _project()["classifiers"]


def test_generated_template_runner_catalog_and_codex_route() -> None:
    from kew.runners.registry import _get_runner_factories

    template = (REPO_ROOT / "create-kew/templates/kew.toml").read_text(encoding="utf-8")
    catalog_match = re.search(r"Force a runner \(([^)]+)\)", template)
    assert catalog_match is not None
    catalog = set(re.findall(r'"([^"]+)"', catalog_match.group(1)))
    assert catalog == set(_get_runner_factories())
    assert '# simple = { runner = "codex", model = "gpt-5.6-sol" }' in template
    assert "[agent.codex]" in template
    assert 'reasoning_effort = "high"' in template
    assert 'service_tier = "fast"' in template
    # Native Codex contains itself (sandbox.py checks that before `enabled`), so
    # the template must not tell Codex users to turn the container sandbox off.
    assert "# [sandbox]\n# enabled = false" not in template
    assert 'default = "claude-code"' in template


def test_docs_landing_page_is_runner_neutral() -> None:
    text = (REPO_ROOT / "docs/index.md").read_text(encoding="utf-8").lower()
    # The landing page names the work generically ("a coding agent"), never one
    # runner as the thing kew runs.
    assert "runs a coding agent" in text
    assert "claude code" not in text


def test_uv_lock_records_the_committed_version() -> None:
    """uv.lock's own kew-cli version must match pyproject.toml's.

    A release bumps ``project.version`` in a pull request. When the release
    tooling once bumped it without re-locking, main carried a lock recording a
    version two releases old, and the first ``uv run`` in a fresh clone
    rewrote it, handing every contributor an uncommitted diff they did not make
    on their very first command.

    This is the loud half: a drifted lock now fails a test instead of quietly
    dirtying trees. If it fires on a version bump, the bump's pull request
    left out ``uv lock``.

    **Reads the COMMITTED blob, not the working tree, and that is load-bearing.**
    CI invokes ``uv run pytest``, which syncs the project — and syncing rewrites
    a stale ``uv.lock`` before any test executes. Asserting on the file on disk
    would therefore always pass in CI while the committed lock stayed wrong:
    a check that cannot fail. Verified by mutation: planting a stale version and
    running through ``uv run`` does NOT trip this; running the venv's pytest
    directly does.
    """
    committed = subprocess.run(
        ["git", "show", "HEAD:kew/uv.lock"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert committed.returncode == 0, (
        "could not read kew/uv.lock from HEAD — this test must inspect the "
        f"committed blob, not the working tree: {committed.stderr.strip()}"
    )
    lock = tomllib.loads(committed.stdout)

    locked = [p for p in lock["package"] if p["name"] == "kew-cli"]
    assert len(locked) == 1, "expected exactly one kew-cli entry in uv.lock"

    assert locked[0]["version"] == _project()["version"], (
        f"uv.lock records kew-cli {locked[0]['version']!r} but pyproject.toml "
        f"declares {_project()['version']!r}. Run `uv lock` in kew/ and commit "
        "the result with the version bump (docs/operations.md, "
        '"Cutting a release").'
    )
