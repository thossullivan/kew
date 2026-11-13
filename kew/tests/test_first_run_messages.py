"""First-run messages tell the operator the truth.

A ``gh`` that was never logged in is an auth failure that names
``gh auth login``; a ``budget`` or ``breaker`` drift warning does not tell the
operator to commit a value that already governs this machine; and ``kew
status`` counts are not silently capped at gh's default page of 30.

Every ``gh`` here is a fake executable on ``PATH`` (the process boundary), and
the drift checks read a real git repository's ``origin`` ref.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from support.fake_gcloud import launchable
from typer.testing import CliRunner

from kew.cli import app
from kew.config import DispatchConfig
from kew.dispatch_core import (
    GitHubAuthError,
    _check_github_connectivity,
    execute_dispatch,
)
from kew.resolve import _preflight_resolve
from kew.review import dispatch_review
from kew.review_contract import Finding
from kew.review_outcome import ReviewOutcome
from kew.run_store import SqliteRunStore
from kew.staleness import check_governance_config_drift
from kew.subprocess_utils import TOOL_PATHS

#: What gh 2.x prints on stderr, with exit 4, before anyone ran ``gh auth login``.
_NEVER_LOGGED_IN = (
    "To get started with GitHub CLI, please run:  gh auth login\n"
    "Alternatively, populate the GH_TOKEN environment variable with a GitHub "
    "API authentication token.\n"
)

#: What ``gh issue list`` prints, with exit 1, when its token is revoked.
_REFUSED_TOKEN_WITH_LOGIN_HINT = (
    "HTTP 401: Bad credentials (https://api.github.com/graphql)\n"
    "Try authenticating with:  gh auth login -h github.com\n"
)

#: The variables gh reads a token from ahead of its stored login.
_TOKEN_ENVS = ("GH_TOKEN", "GITHUB_TOKEN")


@pytest.fixture
def bin_dir(tmp_path: Path) -> Iterator[Path]:
    """A directory a fake ``gh`` can be launched from.

    Some sandboxes mount the pytest temporary filesystem ``noexec``; the fake
    then lives under ``HOME`` and is removed on teardown.
    """
    if launchable(tmp_path):
        yield tmp_path / "bin"
        return
    fallback = tempfile.mkdtemp(prefix="kew-fake-gh-", dir=Path.home())
    try:
        yield Path(fallback) / "bin"
    finally:
        shutil.rmtree(fallback, ignore_errors=True)


def _fake_gh(bin_dir: Path, *, stderr: str, exit_code: int) -> Path:
    """Write a ``gh`` that prints *stderr* and exits *exit_code* for every call."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        f"sys.stderr.write({stderr!r})\n"
        f"sys.exit({exit_code})\n"
    )
    gh.chmod(0o755)
    return gh


@pytest.fixture
def never_logged_in_gh(bin_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    gh = _fake_gh(bin_dir, stderr=_NEVER_LOGGED_IN, exit_code=4)
    monkeypatch.setenv("PATH", f"{gh.parent}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setitem(TOOL_PATHS, "gh", str(gh))
    return gh


def test_gh_never_logged_in_is_an_auth_failure(never_logged_in_gh: Path) -> None:
    with pytest.raises(GitHubAuthError, match="gh auth login"):
        _check_github_connectivity(DispatchConfig())


def test_expired_gh_token_is_still_an_auth_failure(
    bin_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in _TOKEN_ENVS:
        monkeypatch.delenv(name, raising=False)
    gh = _fake_gh(
        bin_dir,
        stderr="HTTP 401: Bad credentials (https://api.github.com/rate_limit)\n",
        exit_code=1,
    )
    monkeypatch.setitem(TOOL_PATHS, "gh", str(gh))

    with pytest.raises(GitHubAuthError) as raised:
        _check_github_connectivity(DispatchConfig())

    assert "check your token" in str(raised.value)
    assert "not logged in" not in str(raised.value)


def test_a_refused_token_with_a_login_hint_says_check_the_token(
    bin_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gh prints a login hint beside a 401; the 401 is the cause, not a login."""
    for name in _TOKEN_ENVS:
        monkeypatch.delenv(name, raising=False)
    gh = _fake_gh(bin_dir, stderr=_REFUSED_TOKEN_WITH_LOGIN_HINT, exit_code=1)
    monkeypatch.setitem(TOOL_PATHS, "gh", str(gh))

    with pytest.raises(GitHubAuthError) as raised:
        _check_github_connectivity(DispatchConfig())

    assert "check your token" in str(raised.value)
    assert "not logged in" not in str(raised.value)


@pytest.mark.parametrize("token_env", _TOKEN_ENVS)
def test_a_refused_token_from_the_environment_names_its_variable(
    token_env: str, bin_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``gh auth login`` refuses while a token variable is set, so name the
    variable instead of suggesting it, and never print the value."""
    for name in _TOKEN_ENVS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(token_env, "sentinel-value-not-a-token")
    gh = _fake_gh(bin_dir, stderr=_REFUSED_TOKEN_WITH_LOGIN_HINT, exit_code=1)
    monkeypatch.setitem(TOOL_PATHS, "gh", str(gh))

    with pytest.raises(GitHubAuthError) as raised:
        _check_github_connectivity(DispatchConfig())

    message = str(raised.value)
    assert token_env in message
    assert "sentinel-value-not-a-token" not in message
    assert "gh auth login" not in message
    assert "not logged in" not in message


def test_a_network_failure_is_not_an_auth_failure(
    bin_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gh = _fake_gh(
        bin_dir,
        stderr="error connecting to api.github.com\n",
        exit_code=1,
    )
    monkeypatch.setitem(TOOL_PATHS, "gh", str(gh))

    assert _check_github_connectivity(DispatchConfig()) is False


def test_dispatch_with_gh_never_logged_in_says_run_gh_auth_login(
    never_logged_in_gh: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")
    issue = {"number": 42, "title": "Fix", "body": "b", "labels": [{"name": "ready"}]}

    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        outcome = execute_dispatch(
            allow_unrecorded=True, issue=issue, config=config, store=store
        )
        events = store.list_audit_events(event="dispatch_completed")

    assert outcome.exit_code == 3
    assert outcome.category == "github_auth"
    assert events[0].payload["error_category"] == "github_auth"
    captured = capsys.readouterr()
    lines = (captured.out + captured.err).splitlines()
    assert any("not logged in" in ln and "gh auth login" in ln for ln in lines)


# ---------------------------------------------------------------------------
# Governance drift: budget/breaker govern locally, security reaches agents
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _repo_with_committed_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, committed: str
) -> Path:
    """A real repository whose ``origin/main`` holds *committed* kew.toml."""
    git = shutil.which("git")
    assert git is not None
    monkeypatch.setitem(TOOL_PATHS, "git", git)
    remote = tmp_path / "remote.git"
    repo = tmp_path / "repo"
    subprocess.run(
        ["git", "init", "--bare", str(remote)], check=True, capture_output=True
    )
    repo.mkdir()
    _git(repo, "-c", "init.defaultBranch=main", "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "checkout", "-B", "main")
    (repo / "kew.toml").write_text(committed)
    _git(repo, "add", "kew.toml")
    _git(repo, "commit", "-m", "config")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "origin", "main")
    return repo


@pytest.mark.parametrize(
    ("committed", "local", "field"),
    [
        ("[budget]\ndaily_usd = 50.0\n", "[budget]\ndaily_usd = 100.0\n", "daily_usd"),
        (
            "[breaker]\ncost_anomaly_factor = 3.0\n",
            "[breaker]\ncost_anomaly_factor = 5.0\n",
            "cost_anomaly_factor",
        ),
    ],
)
def test_budget_and_breaker_drift_says_the_local_value_governs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    committed: str,
    local: str,
    field: str,
) -> None:
    repo = _repo_with_committed_config(tmp_path, monkeypatch, committed)
    (repo / "kew.toml").write_text(local)

    warnings = check_governance_config_drift(repo / "kew.toml")

    assert len(warnings) == 1
    w = warnings[0]
    assert field in w
    assert "already governs this machine's dispatches" in w
    assert "other machines and CI use the committed" in w
    assert "Commit kew.toml to apply" not in w
    assert "dispatch worktrees use the committed file" not in w


def test_security_drift_still_says_commit_to_reach_dispatched_agents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo_with_committed_config(
        tmp_path, monkeypatch, "[security]\nallow_network = false\n"
    )
    (repo / "kew.toml").write_text("[security]\nallow_network = true\n")

    warnings = check_governance_config_drift(repo / "kew.toml")

    assert warnings == [
        "local kew.toml differs from committed config — "
        "dispatch worktrees use the committed file "
        "(security.allow_network: local True / committed False). "
        "Commit kew.toml to apply this change to dispatched agents."
    ]


# ---------------------------------------------------------------------------
# kew status: true queue counts
# ---------------------------------------------------------------------------

#: gh's own default page for ``gh issue list`` when no ``--limit`` is given.
_GH_DEFAULT_LIMIT = 30


def _counting_gh(bin_dir: Path, open_issues: dict[str, int]) -> Path:
    """A ``gh`` that answers ``issue list --label L`` the way gh does: at most
    ``--limit`` issues (30 when absent), counted by ``--jq length``."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        f"counts = {json.dumps(open_issues)}\n"
        "argv = sys.argv[1:]\n"
        "def opt(name, default=None):\n"
        "    return argv[argv.index(name) + 1] if name in argv else default\n"
        "if argv[:2] == ['auth', 'status']:\n"
        "    sys.exit(0)\n"
        "if argv[:2] == ['issue', 'list']:\n"
        f"    limit = int(opt('--limit', {_GH_DEFAULT_LIMIT}))\n"
        "    print(min(limit, counts.get(opt('--label'), 0)))\n"
        "    sys.exit(0)\n"
        "print('[]')\n"
    )
    gh.chmod(0o755)
    return gh


def _status(
    tmp_path: Path,
    bin_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    open_issues: dict[str, int],
) -> str:
    gh = _counting_gh(bin_dir, open_issues)
    monkeypatch.setenv("PATH", f"{gh.parent}{os.pathsep}{os.environ['PATH']}")
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    (project / "kew.toml").write_text(
        f'[project]\nrepo = "o/r"\n\n[logging]\ndir = "{project / "logs"}"\n'
    )
    monkeypatch.chdir(project)
    result = CliRunner().invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    return result.output


def _count_line(output: str, label: str) -> str:
    for line in output.splitlines():
        parts = line.split()
        if parts and parts[0] == label:
            return parts[1]
    raise AssertionError(f"no count line for {label!r} in:\n{output}")


def test_status_counts_past_thirty_open_issues(
    tmp_path: Path, bin_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    labels = DispatchConfig().labels
    output = _status(
        tmp_path, bin_dir, monkeypatch, {labels.ready: 31, labels.agent_failed: 2}
    )

    assert _count_line(output, labels.ready) == "31"
    assert _count_line(output, labels.agent_failed) == "2"


def test_status_says_when_a_count_reaches_the_query_limit(
    tmp_path: Path, bin_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    labels = DispatchConfig().labels
    output = _status(
        tmp_path, bin_dir, monkeypatch, {labels.ready: 5000, labels.in_progress: 3}
    )

    assert _count_line(output, labels.ready) == "1000+"
    assert _count_line(output, labels.in_progress) == "3"
    assert "at least" in output


def test_resolve_with_gh_never_logged_in_is_skipped_with_the_login_hint(
    never_logged_in_gh: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = _preflight_resolve(
        7, DispatchConfig(), store=None, force_budget=False, issue_number=42
    )

    assert rc == 1
    captured = capsys.readouterr()
    lines = (captured.out + captured.err).splitlines()
    assert any("Resolve skipped" in ln and "gh auth login" in ln for ln in lines)


def test_review_with_gh_never_logged_in_is_not_run_with_the_login_hint(
    bin_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    pr = {
        "number": 7,
        "headRefName": "agent/issue-42",
        "headRefOid": "a" * 40,
        "baseRefName": "main",
        "body": "Closes #42",
        "title": "Fix",
    }
    bin_dir.mkdir(parents=True, exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "if sys.argv[1:3] == ['pr', 'view']:\n"
        f"    print({json.dumps(pr)!r})\n"
        "    sys.exit(0)\n"
        f"sys.stderr.write({_NEVER_LOGGED_IN!r})\n"
        "sys.exit(4)\n"
    )
    gh.chmod(0o755)
    monkeypatch.setitem(TOOL_PATHS, "gh", str(gh))
    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")
    finding = Finding(
        severity="low",
        location="kew/src/kew/x.py:1",
        summary="s",
        evidence_command="true",
        evidence_output_digest="0" * 64,
    )
    outcomes: list[ReviewOutcome] = []

    outcome = dispatch_review(
        pr_number=7, config=config, findings=[finding], on_outcome=outcomes.append
    )

    assert outcome.exit_code == 1
    assert [o.classification for o in outcomes] == ["github_auth"]
    assert "gh auth login" in outcomes[0].detail
    captured = capsys.readouterr()
    lines = (captured.out + captured.err).splitlines()
    assert any("Review skipped" in ln and "gh auth login" in ln for ln in lines)
