"""Tests for `kew loop resume` and `kew loop pause` CLI commands.

Covers #290 Task 4 (resume) and #303 Task 5 (pause).
"""

from datetime import UTC, datetime

import pytest
from support.gh_fakes import install_placeholder_forge
from typer.testing import CliRunner

from kew import subprocess_utils
from kew.cli import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _placeholder_forge(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ``gh`` call reaches GitHub: the forge answers as ``o/r`` does (#3069)."""
    install_placeholder_forge(monkeypatch)


@pytest.mark.parametrize(
    "status,launched,reason,expected",
    [
        ("needs_human", True, "runner exited 137", 1),
        ("budget_limited", True, "budget exceeded", 1),
        ("needs_human", True, "verification failed", 1),
        ("needs_human", False, "no verify command", 2),
        ("budget_limited", False, "budget admission refused", 2),
    ],
)
def test_round_five_resume_terminal_exit(
    monkeypatch, tmp_path, status, launched, reason, expected
):
    _round_five_resume_setup(monkeypatch, tmp_path)

    def resume(self, loop, guidance, **kwargs):
        self.launched = launched
        loop.status = status
        loop.status_reason = reason
        return loop

    monkeypatch.setattr("kew.loops.delegate.DelegateDriver.resume", resume)
    result = runner.invoke(app, ["loop", "resume", "resume-test", "--guidance", "go"])
    assert result.exit_code == expected, result.output
    assert status in result.output
    assert reason in result.output


def test_round_five_resume_exception_exit(monkeypatch, tmp_path):
    from kew.selector import GhQueryError

    _round_five_resume_setup(monkeypatch, tmp_path)

    for failure in ("runtime_after", "query_before", "query_after"):

        def resume(self, loop, guidance, failure=failure, **kwargs):
            self.launched = failure.endswith("after")
            if failure.startswith("query"):
                raise GhQueryError("query unavailable")
            raise RuntimeError("stream failed")

        monkeypatch.setattr("kew.loops.delegate.DelegateDriver.resume", resume)
        result = runner.invoke(
            app, ["loop", "resume", "resume-test", "--guidance", "go"]
        )
        assert result.exit_code == (2 if failure.startswith("query") else 1)
        assert (
            "query unavailable" if failure.startswith("query") else "stream failed"
        ) in result.output


def _round_five_resume_setup(monkeypatch, tmp_path):
    from kew.config import DispatchConfig
    from kew.run_store import SqliteRunStore

    config = DispatchConfig()
    config.logging.dir = str(tmp_path)
    with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
        store.save_loop(_make_loop("resume-test"))
    monkeypatch.setattr("kew.cli._open_run_store", SqliteRunStore)
    monkeypatch.setattr("kew.cli.load_config", lambda *a, **k: config)
    monkeypatch.setattr("kew.cli._detect_repo", lambda *a, **k: None)
    monkeypatch.setattr("kew.cli.preflight_tools", lambda: None)


@pytest.fixture(autouse=True)
def _isolate_tool_paths(monkeypatch, tmp_path):
    # Keep managed ledger discovery inside this module's local project: both
    # sides isolated it (branch via KEW_PROJECT, main via chdir); one .git.
    (tmp_path / ".git").mkdir()
    monkeypatch.setenv("KEW_PROJECT", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    # loop_resume calls preflight_tools(), which mutates the global
    # subprocess_utils.TOOL_PATHS with no cleanup. Save/restore so these CLI
    # tests don't leak tool registrations into other test modules (e.g.
    # test_subprocess_utils' unknown-executable assertion).
    saved = subprocess_utils.TOOL_PATHS.copy()
    monkeypatch.setattr(
        "kew.runner_preflight.runner_preflight_failure",
        lambda _config, *, runner_name=None: None,
    )
    yield
    subprocess_utils.TOOL_PATHS.clear()
    subprocess_utils.TOOL_PATHS.update(saved)


def _make_loop(loop_id: str, status: str = "needs_human"):
    from kew.run_store import LoopRecord

    return LoopRecord(
        loop_id=loop_id,
        source="issue",
        goal_text="fix it",
        runner_name="claude-code",
        created_at=datetime.now(UTC),
        issue_num=7,
        status=status,
        budget_usd=10.0,
    )


def test_loop_resume_unknown_loop(monkeypatch, tmp_path):
    from kew.config import DispatchConfig

    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr("kew.cli.load_config", lambda *a, **k: config)

    result = runner.invoke(app, ["loop", "resume", "missing-id", "--guidance", "go"])
    assert result.exit_code != 0
    assert "not found" in result.output.lower()


def test_loop_resume_requires_guidance():
    result = runner.invoke(app, ["loop", "resume", "some-id"])
    assert result.exit_code != 0  # missing required --guidance


def test_loop_resume_happy_path(monkeypatch, tmp_path):
    from kew.config import DispatchConfig
    from kew.run_store import SqliteRunStore

    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    # Seed a needs_human loop in the store
    loop = _make_loop("loop-abc", status="needs_human")
    db_path = log_dir / "dispatch.db"
    with SqliteRunStore(str(db_path)) as store:
        store.save_loop(loop)

    monkeypatch.setattr("kew.cli.load_config", lambda *a, **k: config)
    monkeypatch.setattr("kew.cli._detect_repo", lambda *a, **k: None)

    captured: dict = {}

    def _fake_resume(
        self,
        loop,
        guidance,
        *,
        runner_name=None,
        model=None,
        budget_usd=None,
        max_steps=None,
        timeout_minutes=None,
    ):
        captured["loop"] = loop
        captured["guidance"] = guidance
        captured["budget_usd"] = budget_usd
        captured["max_steps"] = max_steps
        loop.status = "achieved"
        return loop

    monkeypatch.setattr("kew.loops.delegate.DelegateDriver.resume", _fake_resume)

    result = runner.invoke(
        app,
        ["loop", "resume", "loop-abc", "--guidance", "fix it", "--budget", "25"],
    )
    assert result.exit_code == 0, result.output
    assert "achieved" in result.output.lower()
    assert captured.get("guidance") == "fix it"
    assert captured.get("budget_usd") == 25.0


def test_loop_resume_runtime_error_exits_cleanly(monkeypatch, tmp_path):
    """A RuntimeError from resume (e.g. unrestorable worktree) must surface as a
    controlled CLI failure (exit != 0 + message), not an escaped traceback."""
    from kew.config import DispatchConfig
    from kew.run_store import SqliteRunStore

    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    loop = _make_loop("loop-abc", status="needs_human")
    with SqliteRunStore(str(log_dir / "dispatch.db")) as store:
        store.save_loop(loop)

    monkeypatch.setattr("kew.cli.load_config", lambda *a, **k: config)
    monkeypatch.setattr("kew.cli._detect_repo", lambda *a, **k: None)

    def _boom_resume(self, loop, guidance, **kwargs):
        raise RuntimeError("worktree gone")

    monkeypatch.setattr("kew.loops.delegate.DelegateDriver.resume", _boom_resume)

    result = runner.invoke(app, ["loop", "resume", "loop-abc", "--guidance", "go"])
    assert result.exit_code != 0
    assert "worktree gone" in result.output


def test_loop_resume_calls_preflight_tools(monkeypatch, tmp_path):
    """Regression (found by a live dogfood): resume shells out to git via
    reuse_or_restore_worktree, so the subprocess allowlist MUST be populated.
    loop_resume previously skipped preflight_tools() -> 'Unknown executable git'.
    """
    from kew.config import DispatchConfig
    from kew.run_store import SqliteRunStore

    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    with SqliteRunStore(str(log_dir / "dispatch.db")) as store:
        store.save_loop(_make_loop("loop-abc", status="paused"))
    monkeypatch.setattr("kew.cli.load_config", lambda *a, **k: config)

    called = {"n": 0}
    monkeypatch.setattr(
        "kew.cli.preflight_tools", lambda: called.__setitem__("n", called["n"] + 1)
    )
    monkeypatch.setattr("kew.cli._detect_repo", lambda *a, **k: None)
    # Stub resume so the test isolates the preflight wiring.
    monkeypatch.setattr(
        "kew.loops.delegate.DelegateDriver.resume",
        lambda self, loop, guidance, **kw: setattr(loop, "status", "achieved") or loop,
    )

    result = runner.invoke(app, ["loop", "resume", "loop-abc", "--guidance", "go"])
    assert result.exit_code == 0, result.output
    assert called["n"] == 1  # preflight_tools() ran before resume's git operations


def test_loop_resume_canonical_host_publication_reaches_login_remedy(
    monkeypatch, tmp_path
):
    from kew.config import DispatchConfig, RouteEntry, RoutingConfig
    from kew.run_store import SqliteRunStore

    config = DispatchConfig(
        routing=RoutingConfig(
            default=RouteEntry(runner="claude-code", model="claude-sonnet-4-6")
        )
    )
    config.logging.dir = str(tmp_path / "logs")
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    loop = _make_loop("loop-abc", status="paused")
    loop.runner_name = "codex"
    loop.model = "gpt-5.6-sol"
    with SqliteRunStore(str(log_dir / "dispatch.db")) as store:
        store.save_loop(loop)

    preflighted: list[str | None] = []
    monkeypatch.setattr("kew.cli.load_config", lambda *a, **k: config)
    monkeypatch.setattr("kew.cli.preflight_tools", lambda: None)
    monkeypatch.setattr("kew.cli._detect_repo", lambda *a, **k: None)
    monkeypatch.setattr(
        "kew.runner_preflight.runner_preflight_failure",
        lambda _config, *, runner_name=None: (
            preflighted.append(runner_name)
            or "Runner 'codex' is not logged in. Authenticate with: codex login"
        ),
    )

    result = runner.invoke(app, ["loop", "resume", "loop-abc", "--guidance", "go"])

    assert result.exit_code == 2
    assert "host-publication runners" not in result.output
    assert "codex login" in result.output
    assert preflighted == ["codex"]


# ---------------------------------------------------------------------------
# kew loop pause (#303 Task 5)
# ---------------------------------------------------------------------------


def test_loop_pause_running_loop(monkeypatch, tmp_path):
    """Pausing a pursuing loop sets pause_requested and exits 0."""
    from kew.config import DispatchConfig
    from kew.run_store import SqliteRunStore

    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    loop = _make_loop("loop-xyz", status="pursuing")
    db_path = log_dir / "dispatch.db"
    with SqliteRunStore(str(db_path)) as store:
        store.save_loop(loop)

    monkeypatch.setattr("kew.cli.load_config", lambda *a, **k: config)

    result = runner.invoke(app, ["loop", "pause", "loop-xyz"])
    assert result.exit_code == 0, result.output
    assert (
        "resume" in result.output.lower()
    )  # success message guides the user to resume

    # Confirm store has pause_requested set
    with SqliteRunStore(str(db_path)) as store:
        refreshed = store.get_loop("loop-xyz")
    assert refreshed is not None
    assert refreshed.pause_requested is True


def test_loop_pause_non_running_loop(monkeypatch, tmp_path):
    """Pausing a non-pursuing loop (e.g. needs_human) exits non-zero."""
    from kew.config import DispatchConfig
    from kew.run_store import SqliteRunStore

    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    loop = _make_loop("loop-nhr", status="needs_human")
    db_path = log_dir / "dispatch.db"
    with SqliteRunStore(str(db_path)) as store:
        store.save_loop(loop)

    monkeypatch.setattr("kew.cli.load_config", lambda *a, **k: config)

    result = runner.invoke(app, ["loop", "pause", "loop-nhr"])
    assert result.exit_code != 0
    assert "not running" in result.output.lower()

    # Flag must remain unset
    with SqliteRunStore(str(db_path)) as store:
        refreshed = store.get_loop("loop-nhr")
    assert refreshed is not None
    assert refreshed.pause_requested is False


def test_loop_pause_unknown_loop(monkeypatch, tmp_path):
    """Pausing an unknown loop exits non-zero."""
    from kew.config import DispatchConfig

    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr("kew.cli.load_config", lambda *a, **k: config)

    result = runner.invoke(app, ["loop", "pause", "missing"])
    assert result.exit_code != 0
    assert (
        "not running" in result.output.lower()
    )  # failed BECAUSE the loop isn't running


# ---------------------------------------------------------------------------
# kew loop resume --runner/--model (#304 Task 2)
# ---------------------------------------------------------------------------


def test_loop_resume_runner_override_passthrough(monkeypatch, tmp_path):
    from kew.config import DispatchConfig
    from kew.run_store import SqliteRunStore

    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    with SqliteRunStore(str(log_dir / "dispatch.db")) as store:
        store.save_loop(_make_loop("loop-abc", status="paused"))
    monkeypatch.setattr("kew.cli.load_config", lambda *a, **k: config)
    monkeypatch.setattr("kew.cli._detect_repo", lambda *a, **k: None)

    captured: dict = {}

    def _fake_resume(
        self,
        loop,
        guidance,
        *,
        runner_name=None,
        model=None,
        budget_usd=None,
        max_steps=None,
        timeout_minutes=None,
    ):
        captured.update(runner_name=runner_name, model=model)
        loop.status = "achieved"
        return loop

    monkeypatch.setattr("kew.loops.delegate.DelegateDriver.resume", _fake_resume)

    result = runner.invoke(
        app,
        [
            "loop",
            "resume",
            "loop-abc",
            "--guidance",
            "go",
            "--runner",
            "opencode",
            "--model",
            "claude-sonnet-4-6",
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured["runner_name"] == "opencode"
    assert captured["model"] == "claude-sonnet-4-6"


def test_loop_resume_invalid_runner_exits_nonzero(monkeypatch, tmp_path):
    from kew.config import DispatchConfig
    from kew.run_store import SqliteRunStore

    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    with SqliteRunStore(str(log_dir / "dispatch.db")) as store:
        store.save_loop(_make_loop("loop-abc", status="paused"))
    monkeypatch.setattr("kew.cli.load_config", lambda *a, **k: config)
    monkeypatch.setattr("kew.cli._detect_repo", lambda *a, **k: None)

    result = runner.invoke(
        app,
        [
            "loop",
            "resume",
            "loop-abc",
            "--guidance",
            "go",
            "--runner",
            "nope",
        ],
    )
    assert result.exit_code != 0
    assert "unknown runner" in result.output.lower()


def test_round_ten_resume_loop_teardown_exit(monkeypatch, tmp_path):
    from test_loops_delegate import _round_ten_teardown_launch

    _round_five_resume_setup(monkeypatch, tmp_path)
    for started in (True, False):

        def resume(self, loop, guidance, started=started, **kwargs):
            return _round_ten_teardown_launch(self, loop, tmp_path, started)

        monkeypatch.setattr("kew.loops.delegate.DelegateDriver.resume", resume)
        result = runner.invoke(
            app, ["loop", "resume", "resume-test", "--guidance", "go"]
        )
        assert result.exit_code == (1 if started else 2), result.output
        assert (
            "sidecar teardown failed" if started else "pre-launch failure"
        ) in result.output


def test_loop_resume_containment_refusal_exits_two(monkeypatch, tmp_path):
    from pathlib import Path

    from kew.config import DispatchConfig
    from kew.run_store import SqliteRunStore

    config = DispatchConfig(sandbox={"enabled": True})
    config.logging.dir = str(tmp_path / "logs")
    Path(config.logging.dir).mkdir()
    with SqliteRunStore(str(Path(config.logging.dir) / "dispatch.db")) as store:
        store.save_loop(_make_loop("loop-1946", status="paused"))
    monkeypatch.setattr("kew.cli.load_config", lambda *a, **k: config)
    monkeypatch.setattr("kew.cli._detect_repo", lambda *a, **k: None)
    monkeypatch.setattr("kew.loops.delegate._validate_resume", lambda *a, **k: None)
    from types import SimpleNamespace

    monkeypatch.setattr(
        "kew.loops.delegate._resolve_resume_route",
        lambda *a, **k: SimpleNamespace(runner=SimpleNamespace(name="claude-code")),
    )
    result = runner.invoke(app, ["loop", "resume", "loop-1946", "--guidance", "retry"])
    assert result.exit_code == 2, result.output
    assert "#1946 refusal:" in result.output


@pytest.mark.parametrize(
    "reason,code",
    [
        ("gate_failed: command exited 7: stdout: failed; stderr: ", 1),
        ("#1946 refusal: local image unavailable", 2),
    ],
)
def test_loop_resume_reports_returned_gate_failure(monkeypatch, tmp_path, reason, code):
    from kew.config import DispatchConfig
    from kew.run_store import SqliteRunStore

    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")
    (tmp_path / "logs").mkdir()
    loop = _make_loop("loop-1946")
    loop.status_reason = reason
    with SqliteRunStore(str(tmp_path / "logs" / "dispatch.db")) as store:
        store.save_loop(loop)
    monkeypatch.setattr("kew.cli.load_config", lambda *a, **k: config)
    monkeypatch.setattr("kew.cli._detect_repo", lambda *a, **k: None)
    monkeypatch.setattr("kew.cli.preflight_tools", lambda: None)
    monkeypatch.setattr(
        "kew.loops.delegate.DelegateDriver.resume", lambda *a, **k: loop
    )
    result = runner.invoke(app, ["loop", "resume", loop.loop_id, "--guidance", "retry"])
    assert result.exit_code == code, result.output
    assert reason in result.output
