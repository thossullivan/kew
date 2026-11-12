"""Tests for scripts/verify.sh as the single source of truth for the verify gate."""

import os
import subprocess
import tomllib
from pathlib import Path

import pytest
from support.verify_switches import SUITE_JOB_SWITCHES, environ_without_switches

_KEW_DIR = Path(__file__).parent.parent
_REPO_ROOT = _KEW_DIR.parent


def test_kew_toml_verify_invokes_script():
    """commands.verify in kew.toml must reference scripts/verify.sh."""
    with open(_REPO_ROOT / "kew.toml", "rb") as f:
        config = tomllib.load(f)
    verify_cmd = config["commands"]["verify"]
    assert "scripts/verify.sh" in verify_cmd, (
        f"kew.toml commands.verify must reference scripts/verify.sh;"
        f" got: {verify_cmd!r}"
    )


def test_verify_script_exists_and_is_executable():
    """scripts/verify.sh must exist and be marked executable."""
    script = _KEW_DIR / "scripts" / "verify.sh"
    assert script.exists(), "scripts/verify.sh does not exist"
    assert script.stat().st_mode & 0o111, "scripts/verify.sh is not executable"


def test_verify_script_has_fail_closed_guard():
    """verify.sh must declare set -euo pipefail so any gate failure halts the script."""
    script = _KEW_DIR / "scripts" / "verify.sh"
    content = script.read_text()
    assert "set -euo pipefail" in content, "verify.sh must contain 'set -euo pipefail'"


def test_verify_script_scope_parity_with_ci():
    """verify.sh must cover the same checks and scopes as the CI parallel jobs."""
    script = _KEW_DIR / "scripts" / "verify.sh"
    content = script.read_text()

    # lint job: whole-tree ruff (not just src/)
    assert "ruff check ." in content, (
        "verify.sh must run 'ruff check .' (whole tree, matching CI)"
    )
    assert "ruff format --check ." in content, (
        "verify.sh must run 'ruff format --check .' (whole tree, matching CI)"
    )

    # lint job: mypy src/
    assert "mypy src/" in content, "verify.sh must run 'mypy src/' (matching CI)"

    # lint job: bandit with pyproject.toml config
    assert "bandit" in content, "verify.sh must run bandit (matching CI)"
    assert "pyproject.toml" in content, (
        "verify.sh must pass -c pyproject.toml to bandit (matching CI)"
    )

    # complexity job: xenon with .xenon-excludes
    assert "xenon" in content, "verify.sh must run xenon (matching CI)"
    assert ".xenon-excludes" in content, (
        "verify.sh must pass .xenon-excludes to xenon (matching CI)"
    )

    # sast job: semgrep pinned to the vendored ruleset with the jinja2 exclude-rule
    assert "semgrep" in content, "verify.sh must run semgrep (matching CI)"
    assert ".semgrep/pinned.yml" in content, (
        "verify.sh must scan against the pinned ruleset, not --config auto "
        "(matching CI)"
    )
    assert "incorrect-autoescape-disabled" in content, (
        "verify.sh must pass the jinja2 exclude-rule to semgrep (matching CI)"
    )

    # test job: pytest
    assert "pytest" in content, "verify.sh must run pytest (matching CI)"


def test_verify_script_fails_closed(tmp_path):
    """verify.sh must exit nonzero when any gate fails."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    # A uv that always fails simulates a failing gate
    fake_uv = fake_bin / "uv"
    fake_uv.write_text("#!/bin/sh\nexit 1\n")
    fake_uv.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"

    result = subprocess.run(
        ["bash", "scripts/verify.sh"],
        cwd=_KEW_DIR,
        env=env,
        capture_output=True,
    )
    assert result.returncode != 0, (
        "verify.sh must exit nonzero when a gate fails (set -euo pipefail)"
    )


@pytest.mark.parametrize(
    "ambient", [{}, SUITE_JOB_SWITCHES], ids=["clean", "inside-a-suite-job"]
)
def test_verify_script_fails_closed_on_xenon_error_with_zero_exit(
    tmp_path, monkeypatch, ambient
):
    """A reported Xenon violation must not be treated as a green gate.

    Xenon normally returns 1 for infractions, but the verification contract is
    fail-closed even if a wrapper reports the diagnostics and exits 0.  Stub
    every later tool so this test specifically exercises the Xenon guard.
    The verify switches of the suite job this test may run under do not
    reach the script it runs.
    """
    for name, value in ambient.items():
        monkeypatch.setenv(name, value)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()

    fake_uv = fake_bin / "uv"
    fake_uv.write_text(
        "#!/bin/sh\n"
        'for arg in "$@"; do\n'
        '    if [ "$arg" = xenon ]; then\n'
        "        echo 'ERROR:xenon:block has a rank of C' >&2\n"
        "        exit 0\n"
        "    fi\n"
        "done\n"
        "exit 0\n"
    )
    fake_uv.chmod(0o755)

    for command in ("uvx", "gitleaks", "npm"):
        fake_tool = fake_bin / command
        fake_tool.write_text("#!/bin/sh\nexit 0\n")
        fake_tool.chmod(0o755)

    env = environ_without_switches()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"

    result = subprocess.run(
        ["bash", "scripts/verify.sh"],
        cwd=_KEW_DIR,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, (
        "verify.sh must fail when Xenon reports an ERROR, even if the Xenon "
        "process exits 0"
    )
