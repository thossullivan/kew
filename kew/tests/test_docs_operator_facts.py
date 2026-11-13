"""The operator docs pages state what kew ships, read off the shipped values.

Each test reads the fact from kew itself — the labels ``setup_labels``
creates, the skills manifest, the public command tree, the ``mkdocs.yml``
nav, what ``kew stop`` does when its preflight fails — and checks the page
against it, so a page goes red when the product moves and the page does not.
"""

from __future__ import annotations

import json
import re
import subprocess
from importlib.resources import files
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import typer
from typer.testing import CliRunner

import kew
import kew.cli
from kew.config import DispatchConfig
from kew.labels import setup_labels
from kew.run_store import SqliteRunStore

REPO_ROOT = Path(__file__).parent.parent.parent
DOCS = REPO_ROOT / "docs"
CLI_DOCS = DOCS / "cli"
SETUP_PAGE = CLI_DOCS / "setup.md"
MANIFEST = files("kew") / "_skills" / "manifest.json"

# Wording that once announced, as still to come, a feature that has shipped:
# the epic abandon verb and the committed pricing snapshot.
_STILL_COMING = (
    "an abandon verb is being added",
    "feature in progress",
    "until the snapshot ships",
)


def _labels_setup_creates() -> list[str]:
    """Run ``setup_labels`` with default config against a gh that accepts all."""
    with patch("kew.labels.run", return_value=MagicMock(returncode=0, stderr="")):
        result = setup_labels(DispatchConfig(project={"repo": "owner/repo"}))
    return result.names["created"]


def test_setup_page_states_the_label_count_kew_setup_creates() -> None:
    created = _labels_setup_creates()
    text = SETUP_PAGE.read_text()

    stated = re.findall(r"\*\*(\d+) GitHub labels\*\*", text)

    assert stated == [str(len(created))], (
        f"setup.md states {stated} labels; kew setup creates {len(created)}"
    )


def test_setup_page_states_the_label_breakdown_kew_setup_creates() -> None:
    labels = DispatchConfig().labels
    priority, complexity = len(labels.priorities), len(labels.complexity)
    status = len(_labels_setup_creates()) - priority - complexity
    text = " ".join(SETUP_PAGE.read_text().split())

    stated = re.findall(
        r"(\d+) status, (\d+) priority and (\d+) complexity labels", text
    )

    assert stated == [(str(status), str(priority), str(complexity))], (
        f"setup.md states {stated}; kew setup creates {status} status, "
        f"{priority} priority and {complexity} complexity labels"
    )


def test_setup_page_names_every_shipped_skill() -> None:
    artifacts = json.loads(MANIFEST.read_text())["artifacts"]
    text = SETUP_PAGE.read_text()
    with_skills = text.split("## `--with-skills`", 1)[1].split("\n## ", 1)[0]

    # Every single-word code span in the section is an artifact name; the
    # commands it mentions (`kew skills install --all`) carry spaces.
    named = set(re.findall(r"`([a-z][a-z0-9-]*)`", with_skills))
    shipped = {artifact["name"] for artifact in artifacts}

    assert named == shipped, (
        f"setup.md --with-skills omits {sorted(shipped - named)}, "
        f"names unshipped {sorted(named - shipped)}"
    )


def _public_top_level_commands() -> set[str]:
    command = typer.main.get_command(kew.cli.app)
    return {
        name
        for name, child in getattr(command, "commands", {}).items()
        if not getattr(child, "hidden", False)
    }


def test_cli_index_has_a_row_for_every_public_top_level_command() -> None:
    text = (CLI_DOCS / "index.md").read_text()

    rows = re.findall(r"^\| \[`kew ([a-z][a-z0-9-]*)[^`]*`\]\(", text, re.MULTILINE)

    assert set(rows) == _public_top_level_commands(), (
        f"missing rows {sorted(_public_top_level_commands() - set(rows))}, "
        f"stale rows {sorted(set(rows) - _public_top_level_commands())}"
    )


def test_stop_preflight_fails_before_the_latch_is_touched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """stop.md: a failed ``gh auth status`` ends ``kew stop`` before the latch."""
    (tmp_path / "kew.toml").write_text('[logging]\ndir = "logs"\n')
    monkeypatch.setenv("KEW_PROJECT", str(tmp_path))
    monkeypatch.delenv("KEW_LOG_DIR", raising=False)
    db = tmp_path / "logs" / "dispatch.db"
    db.parent.mkdir()
    with SqliteRunStore(str(db)) as store:
        assert store.get_fleet_stopped() is None

    def gh(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        assert cmd[:3] == ["gh", "auth", "status"], cmd
        return subprocess.CompletedProcess(cmd, 1, "", "You are not logged in")

    with (
        patch("kew.subprocess_utils.shutil.which", side_effect=lambda t: f"/bin/{t}"),
        patch("kew.cli.run", side_effect=gh),
    ):
        result = CliRunner().invoke(kew.cli.app, ["stop", "--reason", "halt"])

    assert result.exit_code == 2, result.output
    assert "gh CLI not authenticated. Run: gh auth login" in result.output
    with SqliteRunStore(str(db)) as store:
        assert store.get_fleet_stopped() is None


def _cli_pages_in_nav(mkdocs_yml: str) -> set[str]:
    """CLI pages named by an uncommented ``- Title: cli/page.md`` nav item."""
    return set(
        re.findall(
            r"^\s*-\s[^:#\n]+:\s*cli/([A-Za-z0-9_-]+\.md)\s*$",
            mkdocs_yml,
            re.MULTILINE,
        )
    )


def test_a_commented_out_nav_entry_does_not_count_as_in_the_nav() -> None:
    mkdocs_yml = (
        "nav:\n"
        "  - CLI:\n"
        "    - run: cli/run.md\n"
        "#    - model-eol: cli/model-eol.md\n"
        "    # - stop: cli/stop.md\n"
    )

    assert _cli_pages_in_nav(mkdocs_yml) == {"run.md"}


@pytest.mark.policy
def test_every_cli_reference_page_is_in_the_nav() -> None:
    in_nav = _cli_pages_in_nav((REPO_ROOT / "mkdocs.yml").read_text())
    pages = {page.name for page in CLI_DOCS.glob("*.md")}

    assert pages <= in_nav, f"CLI pages missing from the nav: {sorted(pages - in_nav)}"


@pytest.mark.policy
def test_no_page_says_a_shipped_feature_is_still_coming() -> None:
    offenders = []
    for page in sorted(DOCS.rglob("*.md")):
        flat = " ".join(page.read_text().split()).lower()
        offenders.extend(
            f"{page.relative_to(REPO_ROOT)}: {phrase!r}"
            for phrase in _STILL_COMING
            if phrase in flat
        )

    assert not offenders, "\n".join(offenders)
