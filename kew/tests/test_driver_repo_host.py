"""Project-host invariants for GitHub and GitHub Enterprise."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from kew.repo_host import repo_host


def test_repo_host_uses_the_origin_remote_host(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(
            command, 0, "git@ghe.example.test:owner/repo.git\n", ""
        )

    assert repo_host(tmp_path, run_fn=run) == "ghe.example.test"
    assert repo_host(tmp_path, run_fn=run) == "ghe.example.test"
    assert calls == [["git", "remote", "get-url", "origin"]]


@pytest.mark.policy
def test_project_github_urls_use_repo_host_instead_of_literals() -> None:
    src = Path(__file__).parents[1] / "src" / "kew"
    self_service_files = {
        src / "staleness.py",
        src / "cli" / "__init__.py",
        src / "cli" / "serve.py",
        src / "subprocess_utils.py",
    }
    violations: list[str] = []

    for path in src.rglob("*.py"):
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if "github.com" not in line:
                continue
            if path in self_service_files:
                continue
            if path.name == "repo_host.py" and line.strip() == (
                '_DEFAULT_PROJECT_HOST = "github.com"'
            ):
                continue
            violations.append(f"{path.relative_to(src)}:{line_number}: {line.strip()}")

    assert violations == []
