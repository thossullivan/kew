"""The root ``AGENTS.md`` is the one agent-instructions file (#2481).

Codex reads ``AGENTS.md`` from the repo root down; Claude Code reads
``CLAUDE.md``. The root ``CLAUDE.md`` only imports ``AGENTS.md``, so both
agents load the same conventions from one source.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

pytestmark = pytest.mark.policy

_SECTIONS = (
    "Tech Stack",
    "Conventions",
    "Trap Gates",
    "Writing tests",
    "TUI (Textual) Patterns",
    "Documentation Site",
)

# Records written before the move that quote the old path as history: a frozen
# eval case (a past merge commit message) and a lesson file, which the memory
# channel gate refuses to edit in the same change as work files.
_HISTORICAL = (":!kew/evals/triage/cases.jsonl", ":!.kew/memory/")


def test_root_claude_md_only_imports_agents_md() -> None:
    assert (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8").strip() == (
        "@AGENTS.md"
    )
    assert not (REPO_ROOT / "kew" / "CLAUDE.md").exists()


def test_agents_md_carries_every_section() -> None:
    text = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    headings = re.findall(r"^## (.+)$", text, flags=re.MULTILINE)
    for section in _SECTIONS:
        assert any(h.startswith(section) for h in headings), section


def test_writing_tests_section_states_the_seven_rules() -> None:
    text = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    section = text.split("## Writing tests", 1)[1].split("\n## ", 1)[0]
    rules = re.findall(r"^- ", section, flags=re.MULTILINE)
    assert len(rules) == 7
    for phrase in (
        "observable behavior",
        "assert_called",
        "module under test",
        "inspect.getsource",
        "tests/support/",
        "Behavior changes",
        "Characterization tests",
    ):
        assert phrase in section, phrase


def test_no_tracked_file_points_at_the_old_conventions_file() -> None:
    # Built from fragments so this file does not match its own search.
    old = "kew/" + "CLAUDE.md"
    result = subprocess.run(  # noqa: S603
        ["git", "grep", "-n", "-F", old, "--", ":!CHANGELOG.md", *_HISTORICAL],  # noqa: S607
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode in (0, 1), result.stderr
    assert result.stdout == ""
