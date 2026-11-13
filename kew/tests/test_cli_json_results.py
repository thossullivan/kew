"""``--json`` result summary for dispatch, run and review (#2505).

The fake runner is ``execute_dispatch`` / ``dispatch_review`` replaced at the CLI
boundary: it records a run the way the real one does and prints to both streams,
so every assertion reads the real summary path and the real stdout.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import typer
from typer.testing import CliRunner, Result

from kew.cli import app
from kew.command_outcome import CommandOutcome
from kew.config import DispatchConfig
from kew.labels import ClaimResult
from kew.overlap import DispatchOutcome
from kew.progress import _log, _warn
from kew.run_report_sink import RESULT_SUMMARY_KEYS, render_report
from kew.run_store import RunRecord

runner = CliRunner()

PR_URL = "https://github.com/owner/repo/pull/7"
SUMMARY_KEYS = {*RESULT_SUMMARY_KEYS, "reason"}
PROGRESS = "fake runner progress line"


@pytest.fixture
def boundary(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> DispatchConfig:
    """Real preflight and dispatch boundaries over local tool responses."""
    import subprocess

    config = DispatchConfig()
    config.project.repo = "owner/repo"
    config.logging.dir = str(tmp_path / "logs")
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("kew.cli.load_config", lambda **_: config)
    monkeypatch.setattr("kew.cli.preflight_tools", lambda: None)
    monkeypatch.setattr(
        "kew.cli.run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "", "")
    )
    monkeypatch.setattr(
        "kew.cli.select_next_issue",
        lambda **_: {"number": 42, "title": "Test issue", "body": "", "labels": []},
    )
    monkeypatch.setattr("kew.cli.remote_branch_exists", lambda _: False)
    monkeypatch.setattr("kew.cli.sync_outcomes", lambda *a, **k: None)
    return config


def _record(run_id: str = "run-json", **overrides: Any) -> RunRecord:
    fields: dict[str, Any] = {
        "run_id": run_id,
        "issue_num": 42,
        "item_id": "42",
        "branch": "agent/issue-42",
        "status": "success",
        "exit_code": 0,
        "pr_url": PR_URL,
        "estimated_cost_usd": 1.25,
        "duration_seconds": 61.5,
        "started_at": datetime.now(UTC),
    } | overrides
    return RunRecord(**fields)


def _noisy() -> None:
    """Write progress every way a runner can: stderr lines, echo, print."""
    _log(PROGRESS)
    typer.echo(f"{PROGRESS} (echo)")
    print(f"{PROGRESS} (print)")  # noqa: T201 — the leak under test


def _fake_execute(record: RunRecord, outcome: DispatchOutcome) -> Any:
    def execute(**kwargs: Any) -> DispatchOutcome:
        _noisy()
        kwargs["store"].save(record)
        return outcome

    return execute


def _invoke(command: list[str], execute: Any) -> Result:
    with (
        patch("kew.cli.try_claim_issue", return_value=ClaimResult.CLAIMED),
        patch("kew.cli.execute_dispatch", side_effect=execute),
    ):
        return runner.invoke(app, command)


def _summary(result: Result) -> dict[str, Any]:
    """Stdout must be exactly one JSON document."""
    lines = result.stdout.splitlines()
    assert len(lines) == 1, result.stdout
    document = json.loads(lines[0])
    assert set(document) == SUMMARY_KEYS
    return document


@pytest.mark.parametrize("command", [["dispatch", "42"], ["run"]])
def test_successful_dispatch_json_summary(
    command: list[str], boundary: DispatchConfig
) -> None:
    execute = _fake_execute(_record(), DispatchOutcome(exit_code=0, run_id="run-json"))
    result = _invoke([*command, "--json"], execute)

    assert result.exit_code == 0, result.output
    document = _summary(result)
    assert document["run_id"] == "run-json"
    assert document["outcome"] == "success"
    assert document["pr_url"] == PR_URL
    assert document["item_id"] == "42"
    assert document["cost_usd"] == 1.25
    assert document["duration_seconds"] == 61.5
    assert document["reason"] is None


def test_failed_dispatch_json_keeps_exit_code(boundary: DispatchConfig) -> None:
    record = _record(status="failure", exit_code=1, pr_url="", error_category="ci")
    execute = _fake_execute(record, DispatchOutcome(exit_code=1, run_id="run-json"))
    result = _invoke(["dispatch", "42", "--json"], execute)

    assert result.exit_code == 1, result.output
    document = _summary(result)
    assert document["outcome"] == "failure"
    assert document["error_category"] == "ci"
    assert document["pr_url"] is None
    assert "agent exited with code 1" in document["reason"]


@pytest.mark.parametrize(
    ("claim", "exit_code", "words"),
    [
        (ClaimResult.NOT_READY, 2, "claim failed (not_ready)"),
        (ClaimResult.ALREADY_CLAIMED, 2, "claim failed (already_claimed)"),
    ],
)
def test_refused_claim_json_summary(
    claim: ClaimResult, exit_code: int, words: str, boundary: DispatchConfig
) -> None:
    without_json = _claim_refusal(["dispatch", "42"], claim)
    result = _claim_refusal(["dispatch", "42", "--json"], claim)

    assert result.exit_code == without_json.exit_code == exit_code, result.output
    document = _summary(result)
    assert document["outcome"] == "refused"
    assert document["run_id"] is None
    assert document["reason"] == words


def _claim_refusal(command: list[str], claim: ClaimResult) -> Result:
    with (
        patch("kew.cli.try_claim_issue", return_value=claim),
        patch("kew.cli.execute_dispatch") as execute,
    ):
        result = runner.invoke(app, command)
    execute.assert_not_called()
    return result


def test_refused_pre_launch_json_names_category_and_reason(
    boundary: DispatchConfig,
) -> None:
    def execute(**kwargs: Any) -> DispatchOutcome:
        _warn("budget check failed")
        return DispatchOutcome(
            exit_code=1,
            category="budget_exceeded",
            reason="issue cap $5.00 reached",
            launched=False,
        )

    without_json = _invoke(["dispatch", "42"], execute)
    result = _invoke(["dispatch", "42", "--json"], execute)

    assert result.exit_code == without_json.exit_code == 2, result.output
    document = _summary(result)
    assert document["outcome"] == "refused"
    assert document["error_category"] == "budget_exceeded"
    assert document["reason"] == "issue cap $5.00 reached"


def test_refused_hold_json_keeps_exit_zero(boundary: DispatchConfig) -> None:
    hold = DispatchOutcome(
        exit_code=0,
        category="approval_hold",
        reason="awaiting approval",
        launched=False,
    )
    result = _invoke(["dispatch", "42", "--json"], lambda **_: hold)

    assert result.exit_code == 0, result.output
    document = _summary(result)
    assert document["outcome"] == "held"
    assert document["error_category"] == "approval_hold"
    assert document["reason"] == "awaiting approval"


def test_refused_before_selection_still_prints_the_summary(
    boundary: DispatchConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refusal raised as an exit, before any result exists, keeps its shape."""

    def stopped(config: DispatchConfig) -> None:
        typer.echo("[dispatch] Fleet is stopped", err=True)
        from kew.progress import _error

        _error("Fleet is stopped: kew resume to continue")
        raise typer.Exit(2)

    monkeypatch.setattr("kew.cli._refuse_if_fleet_stopped", stopped)
    result = runner.invoke(app, ["dispatch", "42", "--json"])

    assert result.exit_code == 2, result.output
    document = _summary(result)
    assert document["outcome"] == "refused"
    assert document["reason"] == "Fleet is stopped: kew resume to continue"


@pytest.mark.parametrize(
    "command",
    [
        ["run", "--drain", "--json"],
        ["run", "--show-prompt", "--json"],
        ["dispatch", "42", "--loop", "--json"],
        ["dispatch", "42", "--show-prompt", "--json"],
    ],
)
def test_refused_option_combinations_print_the_summary(
    command: list[str], boundary: DispatchConfig
) -> None:
    result = runner.invoke(app, command)

    assert result.exit_code == 2, result.output
    document = _summary(result)
    assert document["outcome"] == "refused"
    assert "cannot be used with --json" in document["reason"]


def test_empty_queue_run_json(
    boundary: DispatchConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("kew.cli.select_next_issue", lambda **_: None)
    result = runner.invoke(app, ["run", "--json"])

    assert result.exit_code == 0, result.output
    document = _summary(result)
    assert document["outcome"] == "empty"
    assert document["reason"]


def test_a_free_run_keeps_its_zero_cost(boundary: DispatchConfig) -> None:
    """Zero is a recorded cost, not an absence (#1565): it is not nulled."""
    record = _record(estimated_cost_usd=0.0)
    execute = _fake_execute(record, DispatchOutcome(exit_code=0, run_id="run-json"))
    document = _summary(_invoke(["dispatch", "42", "--json"], execute))

    assert document["cost_usd"] == 0.0


def test_summary_field_names_match_run_report_frontmatter() -> None:
    """Every summary key but ``reason`` is a frontmatter key, under that name."""
    record = _record(error_category="ci")
    report = render_report(record, "owner/repo", [])
    frontmatter = report.split("---\n")[1]
    keys = {line.split(":", 1)[0] for line in frontmatter.splitlines() if ":" in line}

    assert set(RESULT_SUMMARY_KEYS) <= keys
    assert SUMMARY_KEYS - set(RESULT_SUMMARY_KEYS) == {"reason"}


def test_field_names_of_a_live_summary(boundary: DispatchConfig) -> None:
    execute = _fake_execute(
        _record(error_category="ci"), DispatchOutcome(exit_code=0, run_id="run-json")
    )
    document = _summary(_invoke(["dispatch", "42", "--json"], execute))
    report = render_report(_record(error_category="ci"), "owner/repo", [])

    for key in RESULT_SUMMARY_KEYS:
        assert f"\n{key}:" in report, key
        assert key in document


@pytest.mark.parametrize("command", [["dispatch", "42"], ["run"]])
def test_stdout_clean_in_json_mode(
    command: list[str], boundary: DispatchConfig
) -> None:
    execute = _fake_execute(_record(), DispatchOutcome(exit_code=0, run_id="run-json"))
    result = _invoke([*command, "--json"], execute)

    assert result.exit_code == 0, result.output
    _summary(result)
    assert PROGRESS not in result.stdout
    assert "[dispatch]" not in result.stdout
    # Progress still reaches people, on stderr.
    assert f"{PROGRESS} (echo)" in result.stderr
    assert f"{PROGRESS} (print)" in result.stderr
    assert "Selected: #42" in result.stderr


def test_stdout_clean_without_json_is_unchanged(boundary: DispatchConfig) -> None:
    """Without --json nothing is redirected and no summary is printed."""
    execute = _fake_execute(_record(), DispatchOutcome(exit_code=0, run_id="run-json"))
    result = _invoke(["dispatch", "42"], execute)

    assert result.exit_code == 0, result.output
    assert f"{PROGRESS} (echo)" in result.stdout
    assert "{" not in result.stdout


def _fake_review(outcome: CommandOutcome, record: RunRecord | None) -> Any:
    def review(**kwargs: Any) -> CommandOutcome:
        _noisy()
        if record is not None:
            kwargs["store"].save(record)
        return outcome

    return review


def test_review_json_summary(boundary: DispatchConfig) -> None:
    record = _record("review-run", run_kind="review")
    fake = _fake_review(CommandOutcome("executed", 0, run_id="review-run"), record)
    with patch("kew.cli.dispatch_review", side_effect=fake):
        result = runner.invoke(app, ["review", "7", "--json"])

    assert result.exit_code == 0, result.output
    document = _summary(result)
    assert document["run_id"] == "review-run"
    assert document["outcome"] == "success"
    assert document["pr_url"] == PR_URL
    assert PROGRESS not in result.stdout


def test_refused_review_json_keeps_exit_code(boundary: DispatchConfig) -> None:
    def fake(**kwargs: Any) -> CommandOutcome:
        _warn("PR #7 has no head branch")
        return CommandOutcome("not_run", 1)

    with patch("kew.cli.dispatch_review", side_effect=fake):
        without_json = runner.invoke(app, ["review", "7"])
        result = runner.invoke(app, ["review", "7", "--json"])

    assert result.exit_code == without_json.exit_code == 2, result.output
    document = _summary(result)
    assert document["outcome"] == "refused"
    assert document["run_id"] is None
    assert document["reason"] == "PR #7 has no head branch"
