"""Tests for the CLI interface."""

import json as _json
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import typer
from typer.testing import CliRunner

from kew.canary import RunnerOutcome
from kew.cli import DispatchCommandResult, _dispatch_one, _preflight_runner, app
from kew.config import DispatchConfig
from kew.dispatch_core import check_pr_exists
from kew.health import CheckResult
from kew.labels import ClaimResult
from kew.outcomes import OrphanReconciliationResult, SyncResult
from kew.overlap import DispatchOutcome
from kew.prompt import slot_sentinel
from kew.report import ReportResult

runner = CliRunner()


@pytest.fixture
def dispatch_boundary(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Exercise real preflight and dispatch boundaries with local tool responses."""
    import subprocess

    config = DispatchConfig()
    config.project.repo = "owner/repo"
    config.logging.dir = str(tmp_path / "logs")
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
    return config


@pytest.mark.parametrize("command", [["run"], ["dispatch", "42"]])
@pytest.mark.parametrize("raw_code", [2, 3, 4, 127])
def test_dispatch_normalizes_runner_codes(
    command, raw_code, dispatch_boundary, tmp_path
):
    """Runner codes remain raw until the public boundary."""
    (tmp_path / ".git").mkdir(exist_ok=True)
    from datetime import UTC, datetime

    from kew.run_store import RunRecord, SqliteRunStore

    outcome = DispatchOutcome(exit_code=raw_code, run_id="raw-run")

    def execute_with_record(**kwargs):
        kwargs["store"].save(
            RunRecord(
                run_id="raw-run",
                issue_num=42,
                branch="agent/issue-42",
                status="failure",
                exit_code=raw_code,
                started_at=datetime.now(UTC),
            )
        )
        return outcome

    with (
        patch("kew.cli.try_claim_issue", return_value=ClaimResult.CLAIMED),
        patch("kew.cli.execute_dispatch", side_effect=execute_with_record),
        patch("kew.cli.sync_outcomes") as sync,
    ):
        result = runner.invoke(app, command)
    assert result.exit_code == 1, result.output
    assert f"agent exited with code {raw_code}" in result.output
    assert outcome.exit_code == raw_code
    assert sync.call_args.kwargs["run_id"] == "raw-run"
    with SqliteRunStore(Path(dispatch_boundary.logging.dir) / "dispatch.db") as store:
        record = store.get("raw-run")
        assert record is not None
        assert record.exit_code == raw_code


@pytest.mark.parametrize("command", [["run"], ["dispatch", "42"]])
@pytest.mark.parametrize(
    ("category", "raw_code"),
    [
        ("github_unreachable", 3),
        ("github_auth", 3),
        ("fleet_stopped", 1),
        ("requeued_over_cap", 1),
        ("exception", 1),
        ("malformed_issue", 1),
        ("credential_publication_refused", 1),
    ],
)
def test_dispatch_pre_launch_failures_exit_two(
    command, category, raw_code, dispatch_boundary, tmp_path
):
    """An outcome decided before any agent started is could-not-run (2)."""
    (tmp_path / ".git").mkdir(exist_ok=True)
    outcome = DispatchOutcome(
        exit_code=raw_code,
        category=category,
        reason="decided before launch",
        launched=False,
    )
    with (
        patch("kew.cli.try_claim_issue", return_value=ClaimResult.CLAIMED),
        patch("kew.cli.execute_dispatch", return_value=outcome),
        patch("kew.cli.sync_outcomes"),
    ):
        result = runner.invoke(app, command)
    assert result.exit_code == 2, result.output
    assert f"dispatch did not launch — {category}: decided before launch" in (
        result.output
    )
    assert "agent exited" not in result.output


@pytest.mark.parametrize("command", [["run"], ["dispatch", "42"]])
def test_dispatch_pre_launch_holds_keep_zero(command, dispatch_boundary, tmp_path):
    """An approval hold or a declined estimate ran nothing and is not a failure."""
    (tmp_path / ".git").mkdir(exist_ok=True)
    for category in ("approval_hold", "approval_declined"):
        outcome = DispatchOutcome(
            exit_code=0, category=category, reason="held", launched=False
        )
        with (
            patch("kew.cli.try_claim_issue", return_value=ClaimResult.CLAIMED),
            patch("kew.cli.execute_dispatch", return_value=outcome),
            patch("kew.cli.sync_outcomes"),
        ):
            result = runner.invoke(app, command)
        assert result.exit_code == 0, result.output


@pytest.mark.parametrize("command", [["run"], ["dispatch", "42"]])
def test_dispatch_preserves_every_claim_result(command, dispatch_boundary, tmp_path):
    (tmp_path / ".git").mkdir(exist_ok=True)
    # Refusing the one issue named on the command line is could-not-run (2);
    # the same refusal on a queue-selected issue is an empty cycle (#2058).
    refused = 2 if command[0] == "dispatch" else 0
    cases = [
        (ClaimResult.FETCH_FAILED, 2, "fetch_failed"),
        (ClaimResult.RACE_DETECTED, 2, "race_detected"),
        (ClaimResult.ALREADY_CLAIMED, refused, "another dispatch already claimed"),
        (ClaimResult.NOT_READY, refused, "is not labeled"),
        (ClaimResult.CLAIMED, 0, ""),
    ]
    observed = []
    with (
        patch("kew.cli.try_claim_issue") as claim,
        patch(
            "kew.cli.execute_dispatch", return_value=DispatchOutcome(exit_code=0)
        ) as execute,
    ):
        for value, _expected, message in cases:
            claim.return_value = value
            execute.reset_mock()
            result = runner.invoke(app, command)
            observed.append(result.exit_code)
            assert message in result.output
            assert execute.call_count == int(value is ClaimResult.CLAIMED)
    assert observed == [expected for _, expected, _ in cases]


@pytest.mark.parametrize("command", [["run"], ["dispatch", "42"]])
@pytest.mark.parametrize("verification", [None, {"ready"}, set()])
def test_dispatch_failed_claim_verification_is_loud(
    command, verification, dispatch_boundary, tmp_path
):
    (tmp_path / ".git").mkdir(exist_ok=True)
    config = dispatch_boundary
    with (
        patch("kew.labels._fetch_issue_labels", side_effect=[{"ready"}, verification]),
        patch("kew.labels.mark_in_progress") as mark,
        patch("kew.cli.execute_dispatch") as execute,
    ):
        result = runner.invoke(app, command)
    assert result.exit_code == 2, result.output
    assert "post-claim verification" in result.output
    assert config.labels.in_progress in result.output
    assert "No agent started" in result.output
    mark.assert_called_once_with(42, config)
    execute.assert_not_called()


@pytest.mark.parametrize("failure", ["auth", "repo", "runner", "tools"])
def test_dispatch_prerequisite_failure_exits_two(
    failure, dispatch_boundary, monkeypatch, tmp_path
):
    (tmp_path / ".git").mkdir(exist_ok=True)
    import subprocess

    from kew.subprocess_utils import preflight_tools

    config = dispatch_boundary
    if failure == "auth":
        monkeypatch.setattr(
            "kew.cli.run",
            lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "expired"),
        )
        message = "not authenticated"
    elif failure == "repo":
        config.project.repo = ""
        message = "Could not detect repo"
    elif failure == "runner":
        monkeypatch.setattr("kew.subprocess_utils.shutil.which", lambda _: None)
        message = "was not found"
    else:
        monkeypatch.setattr("kew.cli.preflight_tools", preflight_tools)
        monkeypatch.setattr("kew.subprocess_utils.shutil.which", lambda _: None)
        message = "not found. Install"
    with patch("kew.cli.execute_dispatch") as execute:
        result = runner.invoke(app, ["run"])
    assert result.exit_code == 2, result.output
    assert message in result.output
    execute.assert_not_called()


def test_nonexistent_project_exits_two(tmp_path):
    result = runner.invoke(app, ["--project", str(tmp_path / "missing"), "run"])
    assert result.exit_code == 2, result.output
    assert "is not a directory" in result.output


@pytest.mark.parametrize("command", [["run"], ["dispatch", "42"]])
def test_dispatch_public_exit_scheme(command: list[str], tmp_path: Path) -> None:
    """Exercise each outcome through selection and the public command boundary."""
    (tmp_path / ".git").mkdir(exist_ok=True)
    from kew.selector import GhQueryError

    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")
    issue = {"number": 42, "title": "Test issue", "body": "", "labels": []}
    with (
        patch("kew.cli.load_config", return_value=config),
        patch("kew.cli._preflight"),
        patch("kew.cli._preflight_runner"),
        patch("kew.cli._refuse_if_fleet_stopped"),
        patch("kew.cli._log_budget_banner"),
        patch("kew.cli.select_next_issue") as select,
        patch("kew.cli.remote_branch_exists", return_value=False),
        patch("kew.cli.try_claim_issue", return_value=ClaimResult.CLAIMED),
        patch("kew.cli.execute_dispatch") as execute,
    ):
        select.return_value = None
        empty = runner.invoke(app, command)
        # An empty queue ran nothing and is not a failure; an undispatchable
        # issue named on the command line is could-not-run (#2058).
        assert empty.exit_code == (2 if command[0] == "dispatch" else 0), empty.output
        execute.assert_not_called()

        select.return_value = issue
        execute.return_value = DispatchOutcome(exit_code=1)
        findings = runner.invoke(app, command)
        assert findings.exit_code == 1, findings.output
        execute.assert_called_once()

        execute.reset_mock()
        execute.return_value = DispatchOutcome(exit_code=0)
        success = runner.invoke(app, command)
        assert success.exit_code == 0, success.output
        execute.assert_called_once()

        execute.reset_mock()
        select.side_effect = GhQueryError("token expired")
        failed_query = runner.invoke(app, command)
        assert failed_query.exit_code == 2, failed_query.output
        assert "GitHub query failed" in failed_query.output
        execute.assert_not_called()


def test_fleet_loop_query_failure_exits_two(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir(exist_ok=True)
    from kew.selector import GhQueryError

    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")
    with (
        patch("kew.cli.load_config", return_value=config),
        patch("kew.cli._preflight"),
        patch("kew.cli._refuse_if_fleet_stopped"),
        patch("kew.cli._log_budget_banner"),
        patch("kew.cli.DispatchScheduler") as scheduler,
    ):
        scheduler.return_value.run_loop.side_effect = GhQueryError("token expired")
        result = runner.invoke(app, ["run", "--drain"])

    assert result.exit_code == 2, result.output
    assert "GitHub query failed" in result.output
    assert "stopping the drain" in result.output


@pytest.mark.parametrize("command", ["skills", "restore"])
def test_missing_command_prerequisite_exits_two(command: str, tmp_path: Path) -> None:
    args = (
        ["skills", "install"]
        if command == "skills"
        else ["restore", str(tmp_path / "missing.tar.gz")]
    )
    result = runner.invoke(app, args)
    assert result.exit_code == 2, result.output


@pytest.mark.parametrize("command", ["status", "doctor"])
def test_root_config_boundary_covers_direct_import_commands(
    command: str, tmp_path: Path
) -> None:
    config_path = tmp_path / "kew.toml"
    config_path.write_text("[agent]\ntimeout_minuttes = 60\n")

    result = runner.invoke(app, ["--project", str(tmp_path), command])

    assert result.exit_code == 2
    assert len(result.output.splitlines()) == 3
    assert str(config_path) in result.output
    assert "[agent].timeout_minuttes" in result.output
    assert "expected" in result.output
    assert "fix:" in result.output
    assert "Traceback" not in result.output


@pytest.mark.parametrize(
    ("case", "key", "expectation"),
    [
        ("unreadable", "-", "readable TOML file"),
        ("not_a_file", "-", "regular TOML file"),
        ("encoding", "-", "UTF-8 encoded TOML"),
        ("provenance", "[model_eol].snapshot_models", "snapshot provenance"),
    ],
)
def test_status_translates_non_pydantic_config_failures(
    case: str,
    key: str,
    expectation: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "kew.toml"
    _prepare_config_failure(case, config_path, monkeypatch)
    result = runner.invoke(app, ["--project", str(tmp_path), "status"])

    assert result.exit_code == 2
    assert len(result.output.splitlines()) <= 3
    assert str(config_path) in result.output
    assert f"key {key}:" in result.output
    assert expectation in result.output
    assert "fix:" in result.output
    assert "Traceback" not in result.output


def _prepare_config_failure(case, config_path, monkeypatch):
    import kew.config as config_module

    if case == "not_a_file":
        config_path.mkdir()
    elif case == "encoding":
        config_path.write_bytes(b"\xff")
    elif case == "provenance":
        config_path.write_text('[model_eol]\nsnapshot_models = ["manual"]\n')
    else:
        config_path.write_text("")
    if case == "unreadable":
        real_open = open

        def denied_open(path, *args, **kwargs):
            if Path(path) == config_path:
                raise PermissionError(path)
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(config_module, "open", denied_open, raising=False)


def test_run_dry_run_config_error_does_not_use_queue_empty_exit(
    tmp_path: Path,
) -> None:
    (tmp_path / "kew.toml").write_text("[agent]\ntimeout_minuttes = 60\n")

    result = runner.invoke(app, ["--project", str(tmp_path), "run", "--dry-run"])

    assert result.exit_code == 2
    assert result.exit_code != 0
    assert "Traceback" not in result.output


def test_status_env_override_without_config_names_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KEW_TIMEOUT", "abc")

    result = runner.invoke(app, ["--project", str(tmp_path), "status"])

    assert result.exit_code == 2
    assert result.output.splitlines()[0] == "KEW_TIMEOUT"
    assert "key KEW_TIMEOUT: expected an integer" in result.output
    assert "fix:" in result.output
    assert "'abc'" in result.output
    assert "kew.toml" not in result.output
    assert "Traceback" not in result.output


def test_status_translates_invalid_model_eol_snapshot(tmp_path: Path) -> None:
    (tmp_path / "kew.toml").write_text('[model_eol]\nsnapshot_path = "eol.json"\n')
    (tmp_path / "eol.json").write_text("not JSON")

    result = runner.invoke(app, ["--project", str(tmp_path), "status"])

    assert result.exit_code == 2
    assert "model_eol.snapshot_path" in result.output
    assert "a JSON snapshot written by kew model-eol" in result.output
    assert "fix:" in result.output
    assert "Traceback" not in result.output


@pytest.fixture(autouse=True)
def _round9_local_cli_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep project and ledger discovery inside this test's directory."""
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)


@pytest.fixture(autouse=True)
def _no_detect_repo(monkeypatch):
    """Prevent _detect_repo from shelling out to gh in unit tests.

    Tests that need to assert on _detect_repo calls can override this fixture
    by decorating with @patch("kew.cli._detect_repo") — the decorator takes
    precedence over the monkeypatch for the duration of that test.
    """
    monkeypatch.setattr("kew.cli._detect_repo", lambda *a, **k: None)


class TestPublicationAttest:
    """`kew publication attest` binds an operator-pushed head (#2124)."""

    @patch("kew.cli.publication.attest_published_head")
    @patch("kew.cli._open_run_store")
    @patch("kew.cli.load_config")
    @patch("kew.cli.preflight_tools")
    def test_attest_routes_branch_head_and_item_to_the_verb(
        self,
        preflight: MagicMock,
        mock_config: MagicMock,
        mock_open_store: MagicMock,
        mock_verb: MagicMock,
        tmp_path: Path,
    ):
        cfg = DispatchConfig()
        cfg.logging.dir = str(tmp_path)
        cfg.branches.base = "main"
        mock_config.return_value = cfg
        store = MagicMock()
        mock_open_store.return_value.__enter__.return_value = store
        mock_verb.return_value = "run-producer"

        result = runner.invoke(
            app,
            ["publication", "attest", "agent/issue-7", "a" * 40, "--item", "7"],
        )

        assert result.exit_code == 0, result.output
        assert mock_verb.call_args.args == (store,)
        assert {
            key: value
            for key, value in mock_verb.call_args.kwargs.items()
            if key not in ("remote_head", "commit_parents", "commit_on_base")
        } == {
            "branch": "agent/issue-7",
            "head_sha": "a" * 40,
            "item_id": "7",
            "base_branch": "main",
        }
        # The hardened operator-origin reads are injected by this layer.
        assert callable(mock_verb.call_args.kwargs["remote_head"])
        assert callable(mock_verb.call_args.kwargs["commit_parents"])
        assert callable(mock_verb.call_args.kwargs["commit_on_base"])
        # The git reads need the allowlist registered first.
        preflight.assert_called_once()
        assert "run run-producer" in result.output

    @patch("kew.cli.publication.attest_published_head")
    @patch("kew.cli._open_run_store")
    @patch("kew.cli.load_config")
    @patch("kew.cli.preflight_tools")
    def test_a_refused_attestation_exits_two_and_says_why(
        self,
        preflight: MagicMock,
        mock_config: MagicMock,
        mock_open_store: MagicMock,
        mock_verb: MagicMock,
        tmp_path: Path,
    ):
        from kew.publication_attest import HeadAttestationRefused

        cfg = DispatchConfig()
        cfg.logging.dir = str(tmp_path)
        mock_config.return_value = cfg
        cfg.branches.base = "main"
        mock_open_store.return_value.__enter__.return_value = MagicMock()
        mock_verb.side_effect = HeadAttestationRefused("the parent is not bound")

        result = runner.invoke(
            app,
            ["publication", "attest", "agent/issue-7", "a" * 40, "--item", "7"],
        )

        assert result.exit_code == 2
        assert "the parent is not bound" in result.output

    @patch("kew.cli.publication.attest_published_head")
    @patch("kew.cli._open_run_store")
    @patch("kew.cli.load_config")
    @patch("kew.cli.preflight_tools")
    def test_a_publication_refusal_exits_two_and_never_tracebacks(
        self,
        preflight: MagicMock,
        mock_config: MagicMock,
        mock_open_store: MagicMock,
        mock_verb: MagicMock,
        tmp_path: Path,
    ):
        """The publication family documents codes 0 and 2, never 1 (#2124).

        An evidence refusal from the policy layer — a branch past the producer
        guard's window, unreadable claims — must not reach the operator as a
        traceback with exit code 1.
        """
        from kew.publication_credentials import CredentialPublicationRefused

        cfg = DispatchConfig()
        cfg.logging.dir = str(tmp_path)
        mock_config.return_value = cfg
        cfg.branches.base = "main"
        mock_open_store.return_value.__enter__.return_value = MagicMock()
        mock_verb.side_effect = CredentialPublicationRefused(
            "publication_lineage_too_deep"
        )

        result = runner.invoke(
            app,
            ["publication", "attest", "agent/issue-7", "a" * 40, "--item", "7"],
        )

        assert result.exit_code == 2
        assert result.exception is None or isinstance(result.exception, SystemExit)
        assert "bounded policy walk" in result.output

    @patch("kew.cli.publication.attest_published_head")
    @patch("kew.cli._open_run_store")
    @patch("kew.cli.load_config")
    @patch("kew.cli.preflight_tools")
    def test_an_unreadable_repository_root_exits_two_not_one(
        self,
        preflight: MagicMock,
        mock_config: MagicMock,
        mock_open_store: MagicMock,
        mock_verb: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """An unset `branches.base` reads the base from git, and that read can fail.

        With the base configured the name answers without a git call and the
        command already exited 2 on an unreadable repository; with it unset the
        eager read raised RuntimeError past every handler, so the family
        documented as 0 or 2 exited 1 with a traceback and said nothing (#2177).
        """
        cfg = DispatchConfig()
        cfg.logging.dir = str(tmp_path)
        cfg.branches.base = ""
        mock_config.return_value = cfg
        mock_open_store.return_value.__enter__.return_value = MagicMock()
        monkeypatch.setattr("kew.worktree._repo_root_or_none", lambda: None)

        result = runner.invoke(
            app,
            ["publication", "attest", "agent/issue-7", "a" * 40, "--item", "7"],
        )

        assert result.exit_code == 2
        assert result.exception is None or isinstance(result.exception, SystemExit)
        assert "the base branch of the operator repository could not be read" in (
            result.output
        )
        assert "Traceback" not in result.output
        # An unverified base is never guessed at: nothing was attested.
        mock_verb.assert_not_called()


class TestRunDryRun:
    @patch("kew.cli.service_verbs.finalize_publication_run")
    @patch("kew.cli._open_run_store")
    @patch("kew.cli.load_config")
    @patch("kew.cli.preflight_tools")
    def test_finalize_routes_run_id_to_repair_verb(
        self,
        preflight: MagicMock,
        mock_config: MagicMock,
        mock_open_store: MagicMock,
        mock_verb: MagicMock,
        tmp_path: Path,
    ):
        cfg = DispatchConfig()
        cfg.logging.dir = str(tmp_path)
        mock_config.return_value = cfg
        store = MagicMock()
        mock_open_store.return_value.__enter__.return_value = store
        mock_verb.return_value = MagicMock(run_id="run-publication")

        result = runner.invoke(app, ["finalize", "run-publication"])

        assert result.exit_code == 0, result.output
        assert mock_verb.call_args.args == (store,)
        assert mock_verb.call_args.kwargs["run_id"] == "run-publication"
        # The operator-origin read the verb names the published head from
        # is injected here; the service layer may not import it (#2124).
        assert callable(mock_verb.call_args.kwargs["remote_head"])
        assert "Finalized run run-publication" in result.output

    @patch("kew.cli.service_verbs.finalize_publication_run")
    @patch("kew.cli._open_run_store")
    @patch("kew.cli.load_config")
    @patch("kew.cli.preflight_tools")
    def test_finalize_registers_subprocess_tools(
        self,
        preflight: MagicMock,
        mock_config: MagicMock,
        mock_open_store: MagicMock,
        mock_verb: MagicMock,
        tmp_path: Path,
    ):
        """finalize shells out to gh (branch/PR evidence) — without
        preflight_tools() the allowlist is empty and resolve_cmd raises."""
        cfg = DispatchConfig()
        cfg.logging.dir = str(tmp_path)
        mock_config.return_value = cfg
        mock_open_store.return_value.__enter__.return_value = MagicMock()
        mock_verb.return_value = MagicMock(run_id="run-publication")

        result = runner.invoke(app, ["finalize", "run-publication"])

        assert result.exit_code == 0
        preflight.assert_called_once()

    @patch("kew.cli.remote_branch_exists", return_value=False)
    @patch("kew.cli._preflight")
    @patch("kew.cli.select_next_issue")
    @patch("kew.cli.load_config")
    def test_dry_run_shows_issue(
        self,
        mock_config: MagicMock,
        mock_select: MagicMock,
        mock_preflight: MagicMock,
        mock_branch: MagicMock,
        tmp_path: Path,
    ):
        (tmp_path / ".git").mkdir(exist_ok=True)
        from kew.config import DispatchConfig

        mock_config.return_value = DispatchConfig()
        mock_select.return_value = {
            "number": 42,
            "title": "Test",
            "body": "Body",
            "labels": [{"name": "ready"}],
        }
        result = runner.invoke(app, ["run", "--dry-run"])
        assert result.exit_code == 0

    @patch("kew.cli._preflight")
    @patch("kew.cli.select_next_issue")
    @patch("kew.cli.load_config")
    def test_dry_run_empty_queue(
        self,
        mock_config: MagicMock,
        mock_select: MagicMock,
        mock_preflight: MagicMock,
        tmp_path,
    ):
        (tmp_path / ".git").mkdir(exist_ok=True)
        from kew.config import DispatchConfig

        mock_config.return_value = DispatchConfig()
        mock_select.return_value = None
        result = runner.invoke(app, ["run", "--dry-run"])
        assert result.exit_code == 0


class TestPreflightRunner:
    """The configured runner's CLI + credentials are checked before dispatch."""

    @patch("kew.subprocess_utils.shutil.which", return_value=None)
    def test_missing_runner_binary_is_actionable_error(self, _which: MagicMock):
        config = DispatchConfig()
        config.agent.runner = "claude-code"
        with pytest.raises(typer.Exit) as exc:
            _preflight_runner(config)
        assert exc.value.exit_code == 2

    @patch.dict("os.environ", {}, clear=True)
    @patch("kew.subprocess_utils.shutil.which", return_value="/usr/bin/claude")
    def test_claude_without_credentials_warns_but_does_not_exit(
        self, _which: MagicMock, capsys: pytest.CaptureFixture[str]
    ):
        config = DispatchConfig()
        config.agent.runner = "claude-code"
        # No raise — a missing key is a warning, not a hard gate (Bedrock/Vertex
        # and token auth are valid and not detectable as ANTHROPIC_API_KEY).
        _preflight_runner(config)
        assert "No API-key env var found" in capsys.readouterr().err

    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test"})
    @patch("kew.subprocess_utils.shutil.which", return_value="/usr/bin/claude")
    def test_claude_with_binary_and_key_passes_quietly(
        self, _which: MagicMock, capsys: pytest.CaptureFixture[str]
    ):
        config = DispatchConfig()
        config.agent.runner = "claude-code"
        _preflight_runner(config)
        assert capsys.readouterr().err == ""

    @patch.dict("os.environ", {}, clear=True)
    @patch("kew.subprocess_utils.shutil.which", return_value="/usr/bin/opencode")
    def test_non_claude_runner_skips_credential_warning(
        self, _which: MagicMock, capsys: pytest.CaptureFixture[str]
    ):
        # OpenCode manages its own credentials — kew must not warn about keys.
        config = DispatchConfig()
        config.agent.runner = "opencode"
        _preflight_runner(config)
        assert capsys.readouterr().err == ""

    @pytest.mark.parametrize(
        ("version", "supported", "login", "expected"),
        [
            (None, False, True, "0.145.0"),
            ("0.144.9", False, True, "0.145.0"),
            ("0.145.0", True, False, "codex login"),
        ],
    )
    def test_codex_health_is_a_hard_preflight_gate(
        self, version, supported, login, expected
    ):
        from kew.subprocess_utils import RunnerReachability

        reach = RunnerReachability(
            "codex",
            "codex",
            True,
            login,
            version=version,
            version_supported=supported,
        )
        with (
            patch("kew.runner_preflight.inspect_runner", return_value=reach),
            patch("kew.cli._error") as error,
            pytest.raises(typer.Exit) as exc,
        ):
            _preflight_runner(DispatchConfig())
        assert exc.value.exit_code == 2
        assert expected in error.call_args.args[0]

    def test_healthy_codex_passes_quietly(self, capsys: pytest.CaptureFixture[str]):
        from kew.subprocess_utils import RunnerReachability

        reach = RunnerReachability(
            "codex",
            "codex",
            True,
            True,
            version="0.145.0",
            version_supported=True,
        )
        with patch("kew.runner_preflight.inspect_runner", return_value=reach):
            _preflight_runner(DispatchConfig())
        assert capsys.readouterr().err == ""

    def test_explicit_runner_name_reaches_runner_inspection(self):
        from kew.subprocess_utils import RunnerReachability

        config = DispatchConfig()
        reach = RunnerReachability(
            "codex",
            "codex",
            True,
            False,
            version="0.145.0",
            version_supported=True,
        )
        with (
            patch("kew.runner_preflight.inspect_runner", return_value=reach) as inspect,
            pytest.raises(typer.Exit),
        ):
            _preflight_runner(config, runner_name="codex")

        inspect.assert_called_once_with(config, runner_name="codex")

    def test_direct_dispatch_preflights_the_resolved_complex_route(self):
        from kew.config import RouteEntry, RoutingConfig

        config = DispatchConfig(
            routing=RoutingConfig(
                default=RouteEntry(runner="claude-code", model="claude-sonnet-4-6"),
                complex=RouteEntry(runner="codex", model="gpt-5.6-sol"),
            )
        )
        issue = {
            "number": 42,
            "title": "Complex issue",
            "body": "",
            "labels": [{"name": "complex"}],
        }
        with (
            patch("kew.cli.select_next_issue", return_value=issue),
            patch("kew.cli.build_prompt", return_value="prompt"),
            patch("kew.cli._preflight_runner", side_effect=typer.Exit(2)) as preflight,
            patch("kew.cli.remote_branch_exists") as remote_branch,
            patch("kew.cli.execute_dispatch") as launch,
            pytest.raises(typer.Exit) as exc,
        ):
            _dispatch_one(config, force_issue=42, dry_run=False)

        assert exc.value.exit_code == 2
        preflight.assert_called_once_with(config, runner_name="codex")
        remote_branch.assert_not_called()
        launch.assert_not_called()


class TestPreviewSkipsRunnerCheck:
    """Commands defer runner checks until their resolved launch boundary."""

    @patch("kew.cli._refuse_if_fleet_stopped")
    @patch(
        "kew.cli.dispatch_review",
        return_value=SimpleNamespace(kind="executed", exit_code=0),
    )
    @patch("kew.cli._preflight")
    @patch("kew.cli.load_config", return_value=DispatchConfig())
    def test_review_dry_run_skips_runner_check(
        self,
        _config: MagicMock,
        mock_preflight: MagicMock,
        _dispatch: MagicMock,
        _refuse: MagicMock,
        tmp_path,
    ):
        (tmp_path / ".git").mkdir(exist_ok=True)
        result = runner.invoke(app, ["review", "7", "--dry-run"])
        assert result.exit_code == 0
        assert "check_runner" not in mock_preflight.call_args.kwargs

    @patch("kew.cli._refuse_if_fleet_stopped")
    @patch(
        "kew.cli.dispatch_review",
        return_value=SimpleNamespace(kind="executed", exit_code=0),
    )
    @patch("kew.cli._preflight")
    @patch("kew.cli.load_config", return_value=DispatchConfig())
    @patch("kew.cli._open_run_store")
    def test_review_without_dry_run_defers_runner_check(
        self,
        mock_open_store: MagicMock,
        _config: MagicMock,
        mock_preflight: MagicMock,
        _dispatch: MagicMock,
        _refuse: MagicMock,
    ):
        mock_open_store.return_value.__enter__.return_value = MagicMock()
        result = runner.invoke(app, ["review", "7"])
        assert result.exit_code == 0
        assert "check_runner" not in mock_preflight.call_args.kwargs


class TestReviewForceBudget:
    """Tests for the --force-budget flag on the review command."""

    @patch("kew.cli._refuse_if_fleet_stopped")
    @patch("kew.cli._preflight")
    @patch("kew.cli.load_config")
    @patch(
        "kew.cli.dispatch_review",
        return_value=SimpleNamespace(kind="executed", exit_code=0),
    )
    def test_review_force_budget(
        self,
        mock_dispatch: MagicMock,
        mock_config: MagicMock,
        _preflight: MagicMock,
        _refuse: MagicMock,
        tmp_path,
    ):
        (tmp_path / ".git").mkdir(exist_ok=True)
        mock_config.return_value = DispatchConfig()

        result = runner.invoke(app, ["review", "42", "--force-budget"])
        assert result.exit_code == 0
        assert mock_dispatch.call_args.kwargs["force_budget"] is True

    @patch("kew.cli._refuse_if_fleet_stopped")
    @patch("kew.cli._preflight")
    @patch("kew.cli.load_config")
    @patch(
        "kew.cli.dispatch_review",
        return_value=SimpleNamespace(kind="executed", exit_code=0),
    )
    def test_review_without_force_budget(
        self,
        mock_dispatch: MagicMock,
        mock_config: MagicMock,
        _preflight: MagicMock,
        _refuse: MagicMock,
        tmp_path,
    ):
        (tmp_path / ".git").mkdir(exist_ok=True)
        mock_config.return_value = DispatchConfig()

        result = runner.invoke(app, ["review", "42"])
        assert result.exit_code == 0
        # Direct key access (not .get) locks the wiring contract: fails if
        # review() ever stops forwarding force_budget explicitly.
        assert mock_dispatch.call_args.kwargs["force_budget"] is False


class TestSetupRepoDetect:
    @patch("kew.cli._detect_repo")
    @patch("kew.cli.preflight_tools")
    @patch("kew.cli.load_config")
    def test_setup_undetected_repo_gives_actionable_error(
        self,
        mock_config: MagicMock,
        mock_preflight: MagicMock,
        mock_detect: MagicMock,
    ):
        # No repo configured and detection leaves it unset.
        mock_config.return_value = DispatchConfig()
        mock_detect.return_value = None
        result = runner.invoke(app, ["setup"])
        assert result.exit_code == 2
        # Must point the user at the fix, matching `kew run`'s message.
        assert "Set project.repo in kew.toml" in result.output


class TestSetupLabelFailure:
    """setup must exit 2 with actionable error when label creation fails."""

    @patch("kew.cli._detect_repo")
    @patch("kew.cli.preflight_tools")
    @patch("kew.cli.load_config")
    @patch("kew.labels.run")
    def test_setup_exits_2_on_repo_404(
        self,
        mock_run: MagicMock,
        mock_config: MagicMock,
        mock_preflight: MagicMock,
        mock_detect: MagicMock,
    ):
        """gh repo view 404 → exit 2, actionable error, no 'Labels created'."""
        cfg = DispatchConfig()
        cfg.project.repo = "owner/name"
        mock_config.return_value = cfg

        fake = MagicMock()
        fake.returncode = 1
        fake.stderr = "HTTP 404: Not Found"
        fake.stdout = ""
        mock_run.return_value = fake

        result = runner.invoke(app, ["setup"])
        assert result.exit_code == 2
        assert "Labels created" not in result.output
        assert "owner/name" in result.output

    @pytest.mark.parametrize(
        ("stderr", "gh_text"),
        [
            (
                "You are not logged into any GitHub hosts. "
                "To log in, run: gh auth login\n",
                (
                    "You are not logged into any GitHub hosts.",
                    "To log in, run: gh auth login",
                ),
            ),
            (
                "HTTP 401: Bad credentials (https://api.github.com/graphql)\n",
                ("HTTP 401: Bad credentials",),
            ),
        ],
        ids=["logged-out", "http-401"],
    )
    @patch("kew.cli._detect_repo")
    @patch("kew.cli.preflight_tools")
    @patch("kew.cli.load_config")
    @patch("kew.labels.run")
    def test_setup_gh_failure_keeps_advice_and_gh_text(
        self,
        mock_run: MagicMock,
        mock_config: MagicMock,
        mock_preflight: MagicMock,
        mock_detect: MagicMock,
        stderr: str,
        gh_text: tuple[str, ...],
    ):
        """F23: the auth advice and gh's own text both survive redaction."""
        cfg = DispatchConfig()
        cfg.project.repo = "owner/name"
        mock_config.return_value = cfg

        fake = MagicMock()
        fake.returncode = 4
        fake.stderr = stderr
        fake.stdout = ""
        mock_run.return_value = fake

        result = runner.invoke(app, ["setup"])
        assert result.exit_code == 2
        output = " ".join(result.output.split())
        assert "Check project.repo in kew.toml and run gh auth status." in output
        for text in gh_text:
            assert text in output

    @patch("kew.cli._detect_repo")
    @patch("kew.cli.preflight_tools")
    @patch("kew.cli.load_config")
    @patch("kew.labels.run")
    def test_setup_gh_failure_still_redacts_a_token_in_gh_output(
        self,
        mock_run: MagicMock,
        mock_config: MagicMock,
        mock_preflight: MagicMock,
        mock_detect: MagicMock,
    ):
        """F23: the reordering must not let a secret in gh's stderr through."""
        cfg = DispatchConfig()
        cfg.project.repo = "owner/name"
        mock_config.return_value = cfg

        secret = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
        fake = MagicMock()
        fake.returncode = 1
        fake.stderr = f"error: token: {secret}\n"
        fake.stdout = ""
        mock_run.return_value = fake

        result = runner.invoke(app, ["setup"])
        assert result.exit_code == 2
        assert secret not in result.output
        assert "<redacted>" in result.output
        assert "run gh auth status" in result.output

    @patch("kew.cli._detect_repo")
    @patch("kew.cli.preflight_tools")
    @patch("kew.cli.load_config")
    @patch("kew.labels.run")
    def test_setup_succeeds_when_labels_already_exist(
        self,
        mock_run: MagicMock,
        mock_config: MagicMock,
        mock_preflight: MagicMock,
        mock_detect: MagicMock,
        tmp_path: Path,
    ):
        """All labels already exist (create fails, edit succeeds) → exit 0."""
        cfg = DispatchConfig()
        cfg.project.repo = "owner/real-repo"
        cfg.logging.dir = str(tmp_path / "logs")
        cfg.branches.worktree_base = str(tmp_path / "wt")
        mock_config.return_value = cfg

        def side_effect(cmd, **kwargs):
            fake = MagicMock()
            if cmd[1] == "repo" and cmd[2] == "view":
                fake.returncode = 0
                fake.stdout = '{"name": "real-repo"}'
                fake.stderr = ""
            elif cmd[1] == "label" and cmd[2] == "create":
                fake.returncode = 1
                fake.stderr = "already exists"
                fake.stdout = ""
            elif cmd[1] == "label" and cmd[2] == "edit":
                fake.returncode = 0
                fake.stderr = ""
                fake.stdout = ""
            else:
                fake.returncode = 0
                fake.stderr = ""
                fake.stdout = ""
            return fake

        mock_run.side_effect = side_effect

        with (
            patch("kew.security.generate_security_hook"),
            patch("kew.security.wire_hook_into_settings"),
        ):
            result = runner.invoke(app, ["setup"])
        assert result.exit_code == 0
        assert "Setup complete" in result.output
        assert "Labels created" in result.output


class TestRunShowPrompt:
    @patch("kew.cli._preflight")
    @patch("kew.cli.select_next_issue")
    @patch("kew.cli.load_config")
    def test_show_prompt_prints_prompt(
        self,
        mock_config: MagicMock,
        mock_select: MagicMock,
        mock_preflight: MagicMock,
        tmp_path,
    ):
        (tmp_path / ".git").mkdir(exist_ok=True)
        from kew.config import DispatchConfig

        mock_config.return_value = DispatchConfig()
        mock_select.return_value = {
            "number": 42,
            "title": "Test Issue",
            "body": "Issue body",
            "labels": [{"name": "ready"}, {"name": "medium"}],
        }
        result = runner.invoke(app, ["run", "--dry-run", "--show-prompt"])
        assert result.exit_code == 0
        # The title renders fenced inline as data (#3348).
        token = slot_sentinel("Test Issue")
        assert f"## Issue #42: {token} Test Issue {token}" in result.output

    @patch("kew.cli._preflight")
    @patch("kew.cli.select_next_issue")
    @patch("kew.cli.load_config")
    def test_show_prompt_empty_queue(
        self,
        mock_config: MagicMock,
        mock_select: MagicMock,
        mock_preflight: MagicMock,
        tmp_path,
    ):
        (tmp_path / ".git").mkdir(exist_ok=True)
        from kew.config import DispatchConfig

        mock_config.return_value = DispatchConfig()
        mock_select.return_value = None
        result = runner.invoke(app, ["run", "--dry-run", "--show-prompt"])
        assert result.exit_code == 0


class TestRunWorkersFlag:
    """The --workers flag controls parallel dispatch."""

    @patch("kew.cli._preflight")
    @patch("kew.cli.DispatchScheduler")
    @patch("kew.cli.load_config")
    def test_workers_flag_sets_max_workers(
        self,
        mock_load: MagicMock,
        mock_scheduler_cls: MagicMock,
        mock_preflight: MagicMock,
        tmp_path,
    ):
        (tmp_path / ".git").mkdir(exist_ok=True)
        mock_config = MagicMock()
        mock_config.parallel.max_workers = 1
        # A bare mock here is not inert: MagicMock satisfies os.PathLike and
        # its __fspath__ returns a plausible RELATIVE path, so the run store
        # created real directories under the process CWD (#1464).
        mock_config.logging.dir = str(tmp_path / "logs")
        mock_load.return_value = mock_config

        mock_scheduler = MagicMock()
        mock_scheduler_cls.return_value = mock_scheduler

        def assert_store_is_open(**_kwargs):
            store = mock_scheduler_cls.call_args.kwargs["store"]
            assert store.list_pending() == []
            return SimpleNamespace(claim_failures=((42, ClaimResult.FETCH_FAILED),))

        mock_scheduler.run_loop.side_effect = assert_store_is_open

        result = runner.invoke(app, ["run", "--drain", "--workers", "3"])
        assert result.exit_code == 2
        # Config should have been updated before scheduler creation
        assert mock_config.parallel.max_workers == 3
        assert mock_scheduler_cls.call_args.kwargs["config"] is mock_config
        mock_scheduler.run_loop.assert_called_once()

    @patch("kew.cli._preflight")
    @patch("kew.cli._dispatch_one")
    @patch("kew.cli.load_config")
    def test_single_worker_no_loop_uses_dispatch_one(
        self,
        mock_load: MagicMock,
        mock_dispatch: MagicMock,
        mock_preflight: MagicMock,
        tmp_path,
    ):
        """Without --drain, single dispatch still uses _dispatch_one."""
        (tmp_path / ".git").mkdir(exist_ok=True)
        mock_config = MagicMock()
        mock_config.parallel.max_workers = 1
        mock_config.logging.dir = str(tmp_path / "logs")  # see #1464
        mock_load.return_value = mock_config
        mock_dispatch.return_value = DispatchCommandResult("executed")

        result = runner.invoke(app, ["run"])
        assert result.exception is None
        assert result.exit_code == 0
        mock_dispatch.assert_called_once()

    @patch("kew.cli._preflight")
    @patch("kew.cli.DispatchScheduler")
    @patch("kew.cli.load_config")
    def test_loop_opens_store_before_constructing_scheduler(
        self,
        mock_load: MagicMock,
        mock_scheduler_cls: MagicMock,
        mock_preflight: MagicMock,
        tmp_path: Path,
    ) -> None:
        (tmp_path / ".git").mkdir(exist_ok=True)
        config = DispatchConfig(
            agent={"max_budget_usd": 25.0}, budget={"per_issue_usd": 0.40}
        )
        config.logging.dir = str(tmp_path / "logs")
        mock_load.return_value = config

        mock_scheduler = mock_scheduler_cls.return_value

        def assert_store_is_open(**_kwargs):
            store = mock_scheduler_cls.call_args.kwargs["store"]
            assert store.list_pending() == []
            return SimpleNamespace(claim_failures=((42, ClaimResult.FETCH_FAILED),))

        mock_scheduler.run_loop.side_effect = assert_store_is_open

        result = runner.invoke(app, ["run", "--drain"])

        assert result.exit_code == 2
        # The cap-warning deferral itself is proven where the code runs:
        # tests/test_dispatch_core.py (fleet workers skip the warning) and
        # tests/test_scheduler.py (the parent warns once per selected issue).
        # With the scheduler mocked, an output assertion here proves nothing.
        assert mock_scheduler_cls.call_args.kwargs["config"] is config
        mock_scheduler.run_loop.assert_called_once()


class TestStatusCostGrouping:
    @patch("kew.cli._preflight")
    @patch("kew.cli.run_with_backoff")
    @patch("kew.cli.load_config")
    @patch("kew.cli.detect_orphan_prs", new=lambda *_: [])
    def test_cost_grouped_by_runner_when_routing_configured(
        self,
        mock_config: MagicMock,
        mock_run: MagicMock,
        mock_preflight: MagicMock,
        tmp_path,
    ):
        """When routing is active, --cost shows per-runner breakdown."""
        (tmp_path / ".git").mkdir(exist_ok=True)
        from datetime import UTC, datetime

        from kew.config import DispatchConfig, RouteEntry, RoutingConfig
        from kew.run_store import RunRecord, SqliteRunStore

        config = DispatchConfig(
            routing=RoutingConfig(
                default=RouteEntry(runner="claude-code", model="claude-sonnet-4"),
                simple=RouteEntry(runner="opencode", model="ollama/deepseek-coder"),
            ),
        )
        config.logging.dir = str(tmp_path / "logs")
        mock_config.return_value = config
        mock_run.return_value = MagicMock(returncode=0, stdout="0")

        # Seed the database
        db_path = tmp_path / "logs" / "dispatch.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        now = datetime.now(UTC)
        with SqliteRunStore(str(db_path)) as store:
            store.save(
                RunRecord(
                    run_id="r1",
                    issue_num=1,
                    branch="b1",
                    status="success",
                    exit_code=0,
                    started_at=now,
                    estimated_cost_usd=2.50,
                    runner_name="claude-code",
                )
            )
            store.save(
                RunRecord(
                    run_id="r2",
                    issue_num=2,
                    branch="b2",
                    status="success",
                    exit_code=0,
                    started_at=now,
                    estimated_cost_usd=0.05,
                    runner_name="opencode",
                )
            )
            store.save(
                RunRecord(
                    run_id="r3",
                    issue_num=3,
                    branch="b3",
                    status="success",
                    exit_code=0,
                    started_at=now,
                    estimated_cost_usd=1.80,
                    runner_name="claude-code",
                )
            )

        result = runner.invoke(app, ["status", "--cost"])
        assert result.exit_code == 0
        assert "Cost by runner:" in result.output
        assert "claude-code" in result.output
        assert "opencode" in result.output
        assert "$4.30" in result.output
        assert "$0.05" in result.output

    @patch("kew.cli._preflight")
    @patch("kew.cli.run_with_backoff")
    @patch("kew.cli.load_config")
    @patch("kew.cli.detect_orphan_prs", new=lambda *_: [])
    def test_no_runner_grouping_without_routing(
        self,
        mock_config: MagicMock,
        mock_run: MagicMock,
        mock_preflight: MagicMock,
        tmp_path,
    ):
        """Without routing, --cost shows flat totals without per-runner breakdown."""
        (tmp_path / ".git").mkdir(exist_ok=True)
        from kew.config import DispatchConfig

        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        mock_config.return_value = config
        mock_run.return_value = MagicMock(returncode=0, stdout="0")

        (tmp_path / "logs").mkdir(parents=True, exist_ok=True)

        result = runner.invoke(app, ["status", "--cost"])
        assert result.exit_code == 0
        assert "Cost by runner:" not in result.output


class TestStatus:
    @patch("kew.cli._preflight")
    @patch("kew.cli.run_with_backoff")
    @patch("kew.cli.load_config")
    def test_status_gh_failure_is_not_an_empty_queue(
        self,
        mock_config: MagicMock,
        mock_run: MagicMock,
        mock_preflight: MagicMock,
        tmp_path: Path,
    ):
        (tmp_path / ".git").mkdir(exist_ok=True)
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        mock_config.return_value = config
        mock_run.return_value = MagicMock(
            returncode=1, stdout="", stderr="authentication failed"
        )

        result = runner.invoke(app, ["status"])

        assert result.exit_code == 2
        assert "GitHub query failed" in result.output
        assert "Queue status" not in result.output

    @patch("kew.cli._preflight")
    @patch("kew.cli.run_with_backoff")
    @patch("kew.cli.load_config")
    def test_status_gh_nonzero_wins_over_valid_count(
        self,
        mock_config: MagicMock,
        mock_run: MagicMock,
        mock_preflight: MagicMock,
        tmp_path: Path,
    ):
        (tmp_path / ".git").mkdir(exist_ok=True)
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        mock_config.return_value = config
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="7",
            stderr="gh: error connecting",
        )

        result = runner.invoke(app, ["status"])

        assert result.exit_code == 2
        assert "GitHub query failed" in result.stderr
        assert "gh: error connecting" in result.stderr
        assert "Queue status" not in result.stdout

    @patch("kew.cli._preflight")
    @patch("kew.cli.run_with_backoff")
    @patch("kew.cli.load_config")
    def test_status_invalid_count_reports_parse_error(
        self,
        mock_config: MagicMock,
        mock_run: MagicMock,
        mock_preflight: MagicMock,
        tmp_path: Path,
    ):
        (tmp_path / ".git").mkdir(exist_ok=True)
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        mock_config.return_value = config
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="not json",
            stderr="",
        )

        result = runner.invoke(app, ["status"])

        assert result.exit_code == 2
        assert "GitHub query failed" in result.stderr
        assert "invalid literal" in result.stderr
        assert "not json" in result.stderr
        assert "Queue status" not in result.stdout

    @patch("kew.cli._preflight")
    @patch("kew.cli.detect_orphan_prs", return_value=[])
    @patch("kew.cli.run_with_backoff")
    @patch("kew.cli.load_config")
    def test_status_empty_queue_is_not_an_error(
        self,
        mock_config: MagicMock,
        mock_run: MagicMock,
        mock_orphans: MagicMock,
        mock_preflight: MagicMock,
        tmp_path: Path,
    ):
        (tmp_path / ".git").mkdir(exist_ok=True)
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        mock_config.return_value = config
        mock_run.return_value = MagicMock(returncode=0, stdout="0", stderr="")

        result = runner.invoke(app, ["status"])

        assert result.exit_code == 0
        assert "Queue status" in result.output
        assert result.output.count(" 0\n") == 5
        assert "GitHub query failed" not in result.output

    @patch("kew.cli._preflight")
    @patch("kew.cli.run_with_backoff")
    @patch("kew.cli.load_config")
    @patch("kew.cli.detect_orphan_prs", new=lambda *_: [])
    def test_status_output(
        self,
        mock_config: MagicMock,
        mock_run: MagicMock,
        mock_preflight: MagicMock,
        tmp_path,
    ):
        (tmp_path / ".git").mkdir(exist_ok=True)
        from kew.config import DispatchConfig

        mock_config.return_value = DispatchConfig()
        mock_run.return_value = MagicMock(returncode=0, stdout="0")
        result = runner.invoke(app, ["status"])
        assert result.exit_code == 0


class TestRetryEscalation:
    """The retry loop should resume, then fresh, then fail.

    Retry logic now lives in dispatch_core.execute_dispatch. These tests
    verify that _dispatch_one delegates correctly; retry internals are
    covered by test_dispatch_core.py.
    """

    @patch("kew.cli.execute_dispatch", return_value=DispatchOutcome(exit_code=0))
    @patch("kew.cli.remote_branch_exists", return_value=False)
    @patch("kew.cli.try_claim_issue", return_value=ClaimResult.CLAIMED)
    @patch("kew.cli._preflight")
    @patch("kew.cli.select_next_issue")
    @patch("kew.cli.load_config")
    def test_resume_on_transient_then_succeed(
        self,
        mock_config: MagicMock,
        mock_select: MagicMock,
        mock_preflight: MagicMock,
        mock_claim: MagicMock,
        mock_branch_exists: MagicMock,
        mock_execute_dispatch: MagicMock,
        tmp_path,
    ):
        (tmp_path / ".git").mkdir(exist_ok=True)
        from kew.config import DispatchConfig, RetryConfig

        config = DispatchConfig(
            retry=RetryConfig(max_attempts=3, max_resume_attempts=2)
        )
        mock_config.return_value = config
        mock_select.return_value = {
            "number": 1,
            "title": "Test",
            "body": "",
            "labels": [],
        }

        result = runner.invoke(app, ["run"])
        assert result.exit_code == 0
        mock_execute_dispatch.assert_called_once()

    @patch("kew.cli.execute_dispatch", return_value=DispatchOutcome(exit_code=1))
    @patch("kew.cli.remote_branch_exists", return_value=False)
    @patch("kew.cli.try_claim_issue", return_value=ClaimResult.CLAIMED)
    @patch("kew.cli._preflight")
    @patch("kew.cli.select_next_issue")
    @patch("kew.cli.load_config")
    def test_permanent_error_no_retry(
        self,
        mock_config: MagicMock,
        mock_select: MagicMock,
        mock_preflight: MagicMock,
        mock_claim: MagicMock,
        mock_branch_exists: MagicMock,
        mock_execute_dispatch: MagicMock,
        tmp_path,
    ):
        (tmp_path / ".git").mkdir(exist_ok=True)
        from kew.config import DispatchConfig, RetryConfig

        config = DispatchConfig(retry=RetryConfig(max_attempts=3))
        mock_config.return_value = config
        mock_select.return_value = {
            "number": 1,
            "title": "Test",
            "body": "",
            "labels": [],
        }

        invoke_result = runner.invoke(app, ["run"])
        # execute_dispatch returned 1 → exit code should be non-zero
        assert invoke_result.exit_code != 0
        mock_execute_dispatch.assert_called_once()


class TestStatusEnhanced:
    @patch("kew.cli.load_config")
    @patch("kew.cli._preflight")
    @patch("kew.cli.detect_orphan_prs", new=lambda *_: [])
    def test_status_history_flag(
        self, mock_preflight: MagicMock, mock_config: MagicMock, tmp_path: Path
    ):
        (tmp_path / ".git").mkdir(exist_ok=True)
        from datetime import UTC, datetime

        from kew.run_store import RunRecord, SqliteRunStore

        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        mock_config.return_value = config

        # Create store with sample data
        store_path = Path(config.logging.dir) / "dispatch.db"
        store_path.parent.mkdir(parents=True, exist_ok=True)
        with SqliteRunStore(str(store_path)) as store:
            store.save(
                RunRecord(
                    run_id="run-001",
                    issue_num=42,
                    branch="agent/issue-42",
                    status="success",
                    exit_code=0,
                    started_at=datetime(2026, 3, 17, 10, 0, 0, tzinfo=UTC),
                    finished_at=datetime(2026, 3, 17, 10, 15, 0, tzinfo=UTC),
                    duration_seconds=900.0,
                    input_tokens=5000,
                    output_tokens=2000,
                    estimated_cost_usd=0.45,
                )
            )

        with patch("kew.cli.run_with_backoff") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="0")
            result = runner.invoke(app, ["status", "--history"])
            assert result.exit_code == 0
            assert "#42" in result.output
            assert "$0.45" in result.output

    @patch("kew.cli.load_config")
    @patch("kew.cli._preflight")
    @patch("kew.cli.detect_orphan_prs", new=lambda *_: [])
    def test_status_cost_flag(
        self, mock_preflight: MagicMock, mock_config: MagicMock, tmp_path: Path
    ):
        (tmp_path / ".git").mkdir(exist_ok=True)
        from datetime import UTC, datetime, timedelta

        from kew.run_store import RunRecord, SqliteRunStore

        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        mock_config.return_value = config

        now = datetime.now(UTC)
        start_of_today = now.replace(hour=0, minute=0, second=0, microsecond=0)

        store_path = Path(config.logging.dir) / "dispatch.db"
        store_path.parent.mkdir(parents=True, exist_ok=True)
        with SqliteRunStore(str(store_path)) as store:
            # Today: should appear in "today" cost
            store.save(
                RunRecord(
                    run_id="run-today",
                    issue_num=10,
                    branch="agent/issue-10",
                    status="success",
                    exit_code=0,
                    started_at=start_of_today + timedelta(hours=1),
                    estimated_cost_usd=1.25,
                )
            )
            # Yesterday 23:59:59: should NOT appear in "today" but SHOULD in "this week"
            store.save(
                RunRecord(
                    run_id="run-yesterday",
                    issue_num=11,
                    branch="agent/issue-11",
                    status="success",
                    exit_code=0,
                    started_at=start_of_today - timedelta(seconds=1),
                    estimated_cost_usd=2.00,
                )
            )
            # 8 days ago: should NOT appear in either
            store.save(
                RunRecord(
                    run_id="run-old",
                    issue_num=12,
                    branch="agent/issue-12",
                    status="success",
                    exit_code=0,
                    started_at=start_of_today - timedelta(days=8),
                    estimated_cost_usd=5.00,
                )
            )

        with patch("kew.cli.run_with_backoff") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="0")
            result = runner.invoke(app, ["status", "--cost"])
            assert result.exit_code == 0
            assert "Cost:" in result.output
            # Today: only $1.25 (not $2.00 from yesterday)
            assert "$1.25 today" in result.output
            # This week: $1.25 + $2.00 = $3.25 (not $5.00 from 8 days ago)
            assert "$3.25 this week" in result.output

    @patch("kew.cli.load_config")
    @patch("kew.cli._preflight")
    def test_status_run_flag(
        self, mock_preflight: MagicMock, mock_config: MagicMock, tmp_path: Path
    ):
        (tmp_path / ".git").mkdir(exist_ok=True)
        from datetime import UTC, datetime

        from kew.run_store import RunRecord, SqliteRunStore

        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        mock_config.return_value = config

        store_path = Path(config.logging.dir) / "dispatch.db"
        store_path.parent.mkdir(parents=True, exist_ok=True)
        with SqliteRunStore(str(store_path)) as store:
            store.save(
                RunRecord(
                    run_id="run-xyz",
                    issue_num=7,
                    branch="agent/issue-7",
                    status="failure",
                    exit_code=1,
                    started_at=datetime(2026, 3, 17, 10, 0, 0, tzinfo=UTC),
                    duration_seconds=120.0,
                    estimated_cost_usd=0.30,
                    error_category="permanent",
                )
            )

        with patch("kew.cli.run_with_backoff") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="0")
            result = runner.invoke(app, ["status", "--run", "run-xyz"])
            assert result.exit_code == 0
            assert "run-xyz" in result.output
            assert "#7" in result.output
            assert "failure" in result.output

    @patch("kew.cli.load_config")
    @patch("kew.cli._preflight")
    def test_status_run_flag_not_found(
        self, mock_preflight: MagicMock, mock_config: MagicMock, tmp_path: Path
    ):
        (tmp_path / ".git").mkdir(exist_ok=True)
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        mock_config.return_value = config

        store_path = Path(config.logging.dir) / "dispatch.db"
        store_path.parent.mkdir(parents=True, exist_ok=True)
        from kew.run_store import SqliteRunStore

        with SqliteRunStore(str(store_path)) as _store:
            pass  # just create the db

        with patch("kew.cli.run_with_backoff") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="0")
            result = runner.invoke(app, ["status", "--run", "nonexistent"])
            assert result.exit_code == 2

    @patch("kew.cli.load_config")
    @patch("kew.cli._preflight")
    def test_status_export_json(
        self, mock_preflight: MagicMock, mock_config: MagicMock, tmp_path: Path
    ):
        (tmp_path / ".git").mkdir(exist_ok=True)
        import json
        from datetime import UTC, datetime

        from kew.run_store import RunRecord, SqliteRunStore

        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        mock_config.return_value = config

        store_path = Path(config.logging.dir) / "dispatch.db"
        store_path.parent.mkdir(parents=True, exist_ok=True)
        with SqliteRunStore(str(store_path)) as store:
            store.save(
                RunRecord(
                    run_id="run-j1",
                    issue_num=5,
                    branch="agent/issue-5",
                    status="success",
                    exit_code=0,
                    started_at=datetime(2026, 3, 17, 10, 0, 0, tzinfo=UTC),
                    estimated_cost_usd=0.50,
                )
            )

        with (
            patch("kew.cli.run_with_backoff") as mock_run,
            patch("kew.cli.list_ready_issues") as mock_lri,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="0")
            mock_lri.return_value = []
            result = runner.invoke(app, ["status", "--json"])
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert "runs" in data
            assert "queue" in data
            assert len(data["runs"]) == 1
            assert data["runs"][0]["issue_num"] == 5

    @patch("kew.cli.load_config")
    @patch("kew.cli._preflight")
    def test_status_export_json_empty(
        self, mock_preflight: MagicMock, mock_config: MagicMock, tmp_path: Path
    ):
        (tmp_path / ".git").mkdir(exist_ok=True)
        import json

        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        mock_config.return_value = config

        store_path = Path(config.logging.dir) / "dispatch.db"
        store_path.parent.mkdir(parents=True, exist_ok=True)
        from kew.run_store import SqliteRunStore

        with SqliteRunStore(str(store_path)) as _store:
            pass

        with (
            patch("kew.cli.run_with_backoff") as mock_run,
            patch("kew.cli.list_ready_issues") as mock_lri,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="0")
            mock_lri.return_value = []
            result = runner.invoke(app, ["status", "--json"])
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["runs"] == []

    @patch("kew.cli.load_config")
    @patch("kew.selector.run_with_backoff")
    @patch("kew.cli._preflight")
    def test_status_export_json_gh_failure_has_no_json(
        self,
        mock_preflight: MagicMock,
        mock_gh: MagicMock,
        mock_config: MagicMock,
        tmp_path: Path,
    ):
        (tmp_path / ".git").mkdir(exist_ok=True)
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        mock_config.return_value = config
        mock_gh.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="gh: error connecting",
        )

        result = runner.invoke(app, ["status", "--json"])

        assert result.exit_code == 2
        assert result.stdout == ""
        assert "GitHub query failed" in result.stderr
        assert "query_unavailable" in result.stderr

    @patch("kew.cli.load_config")
    @patch("kew.selector.run_with_backoff")
    @patch("kew.cli._preflight")
    def test_status_export_json_rc0_garbage_is_a_failure_not_an_empty_queue(
        self,
        mock_preflight: MagicMock,
        mock_gh: MagicMock,
        mock_config: MagicMock,
        tmp_path: Path,
    ):
        # gh exiting 0 with a non-JSON body used to print "count": 0, exit 0.
        (tmp_path / ".git").mkdir(exist_ok=True)
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        mock_config.return_value = config
        mock_gh.return_value = MagicMock(returncode=0, stdout="garbage", stderr="")

        result = runner.invoke(app, ["status", "--json"])

        assert result.exit_code == 2
        assert result.stdout == ""
        assert "GitHub query failed" in result.stderr
        assert "query_unavailable" in result.stderr

    @patch("kew.cli.load_config")
    @patch("kew.selector.run_with_backoff")
    @patch("kew.cli._preflight")
    def test_status_export_json_healthy_gh_has_real_queue_count(
        self,
        mock_preflight: MagicMock,
        mock_gh: MagicMock,
        mock_config: MagicMock,
        tmp_path: Path,
    ):
        (tmp_path / ".git").mkdir(exist_ok=True)
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        mock_config.return_value = config
        mock_gh.return_value = MagicMock(
            returncode=0,
            stdout=_json.dumps(
                [
                    {
                        "number": 17,
                        "title": "First",
                        "labels": [{"name": "ready"}],
                        "createdAt": "2026-08-27T10:00:00Z",
                    },
                    {
                        "number": 18,
                        "title": "Second",
                        "labels": [{"name": "ready"}],
                        "createdAt": "2026-08-28T10:00:00Z",
                    },
                ]
            ),
            stderr="",
        )

        result = runner.invoke(app, ["status", "--json"])

        assert result.exit_code == 0
        data = _json.loads(result.stdout)
        assert data["queue"]["count"] == 2
        assert [issue["number"] for issue in data["queue"]["issues"]] == [17, 18]

    @patch("kew.cli.load_config")
    @patch("kew.cli._preflight")
    def test_status_export_flag_is_removed(
        self, mock_preflight: MagicMock, mock_config: MagicMock, tmp_path: Path
    ):
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        mock_config.return_value = config

        result = runner.invoke(app, ["status", "--export", "json"])
        assert result.exit_code == 2
        assert "No such option '--export'" in _plain(result.output)


class TestStatusRunWorkView:
    """`kew status --run` renders the canonical I19/I20 work projection.

    The run row alone knows its exit code and nothing about publication,
    judgment, source synchronization, review capacity or how old the evidence
    behind any of them is. These tests pin the CLI to the shared projection so
    that a scripted caller reads the SAME truth the console and API read:

    * an opaque tracker id survives (``SFD-12``, never ``#0``);
    * an unreadable forge renders as unavailable/stale and non-healthy rather
      than as a successful empty;
    * preserved work is offered finalization and never a retry;
    * ``view_revision``, the route claim, the age of each observation and the
      one safest action are all present in machine output.
    """

    @staticmethod
    def _config(tmp_path: Path) -> DispatchConfig:
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        config.project.repo = "acme/widgets"
        return config

    @staticmethod
    def _admit(
        config: DispatchConfig,
        *,
        run_id: str = "r1",
        item_id: str = "42",
        tracker: str = "github",
        issue_num: int = 42,
        status: str = "success",
        **overrides,
    ) -> None:
        """Seed one run through the identity-bearing admission path."""
        from datetime import UTC, datetime

        from kew.run_store import AdmissionIdentity, RunRecord, SqliteRunStore

        db = Path(config.logging.dir) / "dispatch.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        fields = {
            "run_id": run_id,
            "issue_num": issue_num,
            "item_id": item_id,
            "branch": f"agent/issue-{item_id.lower()}",
            "status": status,
            "exit_code": 0,
            "started_at": datetime(2026, 8, 18, 10, 0, tzinfo=UTC),
            "finished_at": datetime(2026, 8, 18, 10, 30, tzinfo=UTC),
            "runner_name": "claude-code",
            "model": "claude-opus-5",
            "attempt_number": 1,
        }
        fields.update(overrides)
        with SqliteRunStore(str(db)) as store:
            store.admit_run_with_identity(
                RunRecord(**fields),
                AdmissionIdentity(
                    run_id=run_id,
                    attempt_number=int(fields["attempt_number"]),
                    source_tracker=tracker,
                    item_id=item_id,
                    delivery_repo="acme/widgets",
                ),
            )

    def _invoke(self, config: DispatchConfig, *args: str):
        with (
            patch("kew.cli.load_config", return_value=config),
            patch("kew.cli._preflight"),
        ):
            return runner.invoke(app, ["status", *args])

    def _json(self, config: DispatchConfig, run_id: str = "r1") -> dict:
        result = self._invoke(config, "--run", run_id, "--json")
        assert result.exit_code == 0, result.output
        return _json.loads(result.output)

    # -- human render ------------------------------------------------------

    def test_run_without_posture_record_says_not_recorded(self, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir(exist_ok=True)
        config = self._config(tmp_path)
        self._admit(config)

        result = self._invoke(config, "--run", "r1")

        assert result.exit_code == 0
        assert "Posture:  not recorded" in result.output

    def test_run_prints_the_recorded_posture_line(self, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir(exist_ok=True)
        from kew.run_store import SqliteRunStore

        config = self._config(tmp_path)
        self._admit(config)
        db = Path(config.logging.dir) / "dispatch.db"
        with SqliteRunStore(str(db)) as store:
            store.append_audit_event(
                timestamp="2026-09-13T12:00:00Z",
                event="sandbox_resolved",
                run_id="r1",
                payload={
                    "posture": "native",
                    "filesystem_boundary": "runner-native-workspace-write",
                    "network_boundary": "not-applicable",
                    "credential_names": [],
                    "command_guard": "not-applicable",
                    "admission": "policy",
                    "reason": None,
                },
            )

        result = self._invoke(config, "--run", "r1")

        posture_lines = [
            line for line in result.output.splitlines() if line.startswith("Posture:")
        ]
        assert posture_lines == [
            "Posture:  native; filesystem_boundary=runner-native-workspace-write; "
            "network_boundary=not-applicable; egress_verification_basis=not recorded; "
            "credential_names=none; "
            "command_guard=not-applicable; admission=policy; reason=none"
        ]

    def test_active_run_shows_execution_state_and_recommends_waiting(
        self, tmp_path: Path
    ):
        """An in-flight GitHub item is in_flight and must not be re-dispatched."""
        from datetime import UTC, datetime, timedelta

        (tmp_path / ".git").mkdir(exist_ok=True)
        config = self._config(tmp_path)
        # Started minutes ago, against the wall clock `kew status` reads: a
        # pending row is a run in flight only while it is YOUNG
        # (``PENDING_RUN_MAX_AGE_S`` in kew/service/work.py), and the fixture's
        # August default is read — rightly — as a row a dead host left behind.
        self._admit(
            config,
            status="pending",
            started_at=datetime.now(UTC) - timedelta(minutes=5),
            finished_at=None,
        )

        result = self._invoke(config, "--run", "r1")

        assert result.exit_code == 0
        assert "#42" in result.output
        assert "in_flight" in result.output
        lines = result.output.splitlines()
        (nxt,) = [line for line in lines if line.startswith("Next:")]
        assert "observation: wait" in nxt
        # Nothing is offered while an attempt is running: the projection has
        # no action for "wait", and inventing one would duplicate the run.
        assert not [line for line in lines if line.startswith("Actions:")]

    def test_opaque_linear_id_is_rendered_bare_and_never_as_issue_zero(
        self, tmp_path: Path
    ):
        """SFD-12 keeps its own id — the numeric shim ``#0`` is a lie."""
        (tmp_path / ".git").mkdir(exist_ok=True)
        config = self._config(tmp_path)
        self._admit(config, item_id="SFD-12", tracker="linear", issue_num=0)

        result = self._invoke(config, "--run", "r1")

        assert result.exit_code == 0
        assert "SFD-12" in result.output
        assert "#0" not in result.output

    def test_preserved_work_is_offered_finalization_and_never_a_retry(
        self, tmp_path: Path
    ):
        """awaiting_publication: publish the SAME artifact, never run it again."""
        (tmp_path / ".git").mkdir(exist_ok=True)
        config = self._config(tmp_path)
        self._admit(
            config,
            item_id="SFD-12",
            tracker="linear",
            issue_num=0,
            status="awaiting_publication",
        )

        result = self._invoke(config, "--run", "r1")

        assert result.exit_code == 0
        assert "awaiting_publication" in result.output
        assert "finalize_publication" in result.output
        assert "publication-finalization" in result.output
        assert "dispatch" not in result.output
        assert "retry" not in result.output

    def test_route_claim_is_reported_as_declared_not_enforced(self, tmp_path: Path):
        """The lane is a claim: nothing came back to check means unverified."""
        (tmp_path / ".git").mkdir(exist_ok=True)
        config = self._config(tmp_path)
        self._admit(config, status="awaiting_publication")

        result = self._invoke(config, "--run", "r1")

        assert "claude-opus-5" in result.output
        assert "unverified" in result.output

    def test_substituted_route_claim_is_named_in_the_human_render(self, tmp_path: Path):
        (tmp_path / ".git").mkdir(exist_ok=True)
        config = self._config(tmp_path)
        self._admit(
            config,
            status="awaiting_publication",
            reported_model="claude-sonnet-5",
        )

        result = self._invoke(config, "--run", "r1")

        assert "substituted" in result.output

    def test_terminal_failure_with_no_artifact_asks_for_admission(self, tmp_path: Path):
        """Nothing was preserved, so admitting the item again is the safe move."""
        (tmp_path / ".git").mkdir(exist_ok=True)
        config = self._config(tmp_path)
        self._admit(config, status="failure")

        result = self._invoke(config, "--run", "r1")

        assert "terminal" in result.output
        # The source item could not be read, so a dispatch would be a guess.
        assert "observe_remote" in result.output or "dispatch" in result.output

    def test_unknown_run_still_exits_2_with_a_stderr_message(self, tmp_path: Path):
        (tmp_path / ".git").mkdir(exist_ok=True)
        config = self._config(tmp_path)
        self._admit(config)

        result = self._invoke(config, "--run", "nope")

        assert result.exit_code == 2
        assert "not found" in result.output

    def test_unknown_run_exits_2_in_machine_mode_too(self, tmp_path: Path):
        """Scriptable error semantics survive --json."""
        (tmp_path / ".git").mkdir(exist_ok=True)
        config = self._config(tmp_path)
        self._admit(config)

        result = self._invoke(config, "--run", "nope", "--json")

        assert result.exit_code == 2
        assert "not found" in result.output

    # -- machine render ----------------------------------------------------

    def test_machine_output_pins_the_view_revision(self, tmp_path: Path):
        (tmp_path / ".git").mkdir(exist_ok=True)
        config = self._config(tmp_path)
        self._admit(config, status="awaiting_publication")

        data = self._json(config)

        assert data["view_revision"].startswith("wv1:")
        assert data["work"]["view_revision"] == data["view_revision"]
        # Same facts, same revision — a caller may compare two reads directly.
        assert self._json(config)["view_revision"] == data["view_revision"]

    def test_machine_output_pins_the_route_claim(self, tmp_path: Path):
        (tmp_path / ".git").mkdir(exist_ok=True)
        config = self._config(tmp_path)
        self._admit(
            config, status="awaiting_publication", reported_model="claude-sonnet-5"
        )

        data = self._json(config)

        assert data["route"]["claim"] == "substituted"
        assert data["route"]["model"] == "claude-opus-5"
        assert data["route"]["reported_model"] == "claude-sonnet-5"
        assert data["route"]["provenance"] == "declared"

    def test_machine_output_pins_the_safest_action_and_its_stage(self, tmp_path: Path):
        (tmp_path / ".git").mkdir(exist_ok=True)
        config = self._config(tmp_path)
        self._admit(config, status="awaiting_publication")

        data = self._json(config)

        assert data["safest_action"]["action"] == "finalize_publication"
        assert data["safest_action"]["stage"] == "publication-finalization"
        assert data["safest_action"]["blocked_by"] is None
        assert [a["action"] for a in data["actions"]] == ["finalize_publication"]
        assert data["actions"][0]["stage"] == "publication-finalization"
        assert data["actions"][0]["target_kind"] == "run"
        assert data["actions"][0]["target_ref"] == "r1"

    def test_machine_output_ages_every_observation_it_rests_on(self, tmp_path: Path):
        """Evidence age is explicit: a reading with no timestamp says so."""
        (tmp_path / ".git").mkdir(exist_ok=True)
        config = self._config(tmp_path)
        self._admit(
            config,
            status="success",
            pr_url="https://example.invalid/pr/7",
            pr_state="OPEN",
        )

        data = self._json(config)

        evidence = {e["dimension"]: e for e in data["evidence"]}
        assert set(evidence) == {
            "pull_request",
            "tracker_item",
            "route_boundary",
            "review_capacity",
        }
        cached = evidence["pull_request"]
        assert cached["availability"] == "stale"
        assert cached["provenance"] == "cached"
        assert cached["age_seconds"] > 0
        # Never read, never aged — an absent reading has no age to report.
        assert evidence["tracker_item"]["availability"] == "unavailable"
        assert evidence["tracker_item"]["age_seconds"] is None

    def test_machine_output_carries_the_opaque_item_identity(self, tmp_path: Path):
        (tmp_path / ".git").mkdir(exist_ok=True)
        config = self._config(tmp_path)
        self._admit(config, item_id="SFD-12", tracker="linear", issue_num=0)

        data = self._json(config)

        assert data["item_id"] == "SFD-12"
        assert data["item_ref"] == "SFD-12"
        assert data["work"]["identity"]["source_tracker"] == "linear"
        assert data["run"]["run_id"] == "r1"

    def test_unreadable_forge_is_unavailable_and_non_healthy(self, tmp_path: Path):
        """A GitHub read that never happened is not a successful empty."""
        (tmp_path / ".git").mkdir(exist_ok=True)
        config = self._config(tmp_path)
        self._admit(config, status="success")

        data = self._json(config)

        assert data["healthy"] is False
        assert all(
            e["availability"] in ("stale", "unavailable") for e in data["evidence"]
        )

    def test_merged_artifact_with_an_unreadable_tracker_blocks_the_sync_claim(
        self, tmp_path: Path
    ):
        """Sync failure mode: never claim delivered off a reading nobody made."""
        (tmp_path / ".git").mkdir(exist_ok=True)
        config = self._config(tmp_path)
        self._admit(
            config,
            status="success",
            pr_url="https://example.invalid/pr/7",
            pr_state="MERGED",
        )

        data = self._json(config)

        assert data["healthy"] is False
        assert data["work"]["source"]["synchronization"] == "unknown"
        assert data["safest_action"]["action"] == "observe_remote"
        assert data["safest_action"]["blocked_by"] == "tracker_item"
        # Fail closed: nothing is offered off a fact nobody could read.
        assert data["actions"] == []

    def test_preserved_work_never_offers_a_redispatch_in_machine_output(
        self, tmp_path: Path
    ):
        (tmp_path / ".git").mkdir(exist_ok=True)
        config = self._config(tmp_path)
        self._admit(config, status="awaiting_publication")

        data = self._json(config)

        assert data["healthy"] is False
        assert "dispatch" not in [a["action"] for a in data["actions"]]
        assert data["work"]["artifact"]["state"] == "preserved"
        assert data["work"]["artifact"]["publication"] == "pending"

    def test_machine_output_is_valid_json_only(self, tmp_path: Path):
        """No banner, no log line — stdout stays parseable."""
        (tmp_path / ".git").mkdir(exist_ok=True)
        config = self._config(tmp_path)
        self._admit(config)

        result = self._invoke(config, "--run", "r1", "--json")

        assert result.exit_code == 0
        _json.loads(result.output)

    def test_machine_output_keeps_a_stable_top_level_shape(self, tmp_path: Path):
        (tmp_path / ".git").mkdir(exist_ok=True)
        config = self._config(tmp_path)
        self._admit(config)

        data = self._json(config)

        assert set(data) == {
            "run",
            "item_id",
            "item_ref",
            "view_revision",
            "composed_at",
            "healthy",
            "route",
            "safest_action",
            "actions",
            "evidence",
            "sandbox",
            "work",
        }


class TestWorkActionStages:
    """The CLI keeps five kinds of next step visibly apart.

    ``kew.service.work`` decides WHICH action is safe; this map only names the
    kind of decision it is, so a scripted caller never confuses admitting new
    work with releasing finished work.
    """

    @pytest.mark.parametrize(
        ("action", "stage"),
        [
            ("dispatch", "admission"),
            ("fix_checks", "independent-review"),
            ("resolve_conflict", "independent-review"),
            ("await_human_decision", "human-release"),
            ("confirm_publication_risk", "human-release"),
            ("confirm_no_change", "human-release"),
            ("finalize_publication", "publication-finalization"),
            ("address_review_feedback", "address-feedback"),
        ],
    )
    def test_each_canonical_action_declares_its_stage(self, action: str, stage: str):
        from kew.cli import _action_stage

        assert _action_stage(action) == stage

    def test_the_five_named_stages_are_distinct(self):
        from kew.cli import _action_stage

        stages = {
            _action_stage(a)
            for a in (
                "dispatch",
                "fix_checks",
                "await_human_decision",
                "finalize_publication",
                "address_review_feedback",
            )
        }
        assert len(stages) == 5

    def test_an_unmapped_action_is_named_rather_than_guessed(self):
        from kew.cli import _action_stage

        assert _action_stage("some_future_action") == "other"


class TestStatusRunDocsParity:
    """`docs/cli/status.md` must describe the command that actually ships.

    The published page is what an operator scripts against; a machine contract
    documented but not implemented — or implemented but not documented — is the
    same outage either way.
    """

    @staticmethod
    def _docs() -> str:
        root = Path(__file__).resolve().parents[2]
        return (root / "docs" / "cli" / "status.md").read_text()

    def test_docs_describe_the_machine_contract_keys(self):
        docs = self._docs()
        assert "kew status --run <run-id> --json" in docs
        for key in (
            "run",
            "item_id",
            "view_revision",
            "composed_at",
            "healthy",
            "route",
            "safest_action",
            "actions",
            "evidence",
            "work",
        ):
            assert f"`{key}`" in docs, key

    def test_docs_name_every_stage_the_cli_renders(self):
        docs = self._docs()
        for stage in (
            "admission",
            "independent-review",
            "human-release",
            "publication-finalization",
            "address-feedback",
        ):
            assert stage in docs, stage

    def test_docs_state_the_outage_and_exit_contract(self):
        docs = self._docs().lower()
        assert "stderr and exit code 2" in docs
        assert "stderr and exit code 1" not in docs
        assert "observe_remote" in docs
        assert "never a fresh dispatch" in docs


class TestStalePendingCleanupOnStartup:
    """run_cmd calls cleanup_stale_pending() when the store is opened."""

    @patch("kew.cli.build_notifier")
    @patch("kew.cli._preflight")
    @patch("kew.cli._dispatch_one")
    @patch("kew.cli.load_config")
    def test_run_cmd_calls_cleanup_stale_pending(
        self,
        mock_load: MagicMock,
        mock_dispatch: MagicMock,
        mock_preflight: MagicMock,
        mock_build_notifier: MagicMock,
        tmp_path: Path,
    ):
        """run_cmd must call cleanup_stale_pending() before dispatching."""
        from kew.config import DispatchConfig

        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        mock_load.return_value = config
        mock_dispatch.return_value = DispatchCommandResult("executed")
        mock_build_notifier.return_value = MagicMock()

        with patch("kew.cli._open_run_store") as mock_store_cls:
            mock_store = MagicMock()
            mock_store.cleanup_stale_pending.return_value = 0
            mock_store_cls.return_value.__enter__.return_value = mock_store

            result = runner.invoke(app, ["run"])

        assert result.exit_code == 0
        mock_store.cleanup_stale_pending.assert_called_once()


class TestRunStoreIntegration:
    @patch("kew.cli.execute_dispatch", return_value=DispatchOutcome(exit_code=0))
    @patch("kew.cli.remote_branch_exists", return_value=False)
    @patch("kew.cli.try_claim_issue", return_value=ClaimResult.CLAIMED)
    @patch("kew.cli.select_next_issue")
    def test_dispatch_passes_store_to_execute_dispatch(
        self,
        mock_select: MagicMock,
        mock_claim: MagicMock,
        mock_branch_exists: MagicMock,
        mock_execute_dispatch: MagicMock,
        tmp_path: Path,
    ):
        """_dispatch_one passes the store to execute_dispatch."""
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        mock_select.return_value = {
            "number": 42,
            "title": "Test issue",
            "body": "body",
            "labels": [],
        }

        from kew.run_store import SqliteRunStore

        db_path = tmp_path / "dispatch.db"
        with SqliteRunStore(str(db_path)) as store:
            rc = _dispatch_one(config, force_issue=42, dry_run=False, store=store)
            assert rc == DispatchCommandResult("executed", 0)

        call_kwargs = mock_execute_dispatch.call_args.kwargs
        assert call_kwargs["store"] is store

    @patch("kew.cli.execute_dispatch", return_value=DispatchOutcome(exit_code=1))
    @patch("kew.cli.remote_branch_exists", return_value=False)
    @patch("kew.cli.try_claim_issue", return_value=ClaimResult.CLAIMED)
    @patch("kew.cli.select_next_issue")
    def test_dispatch_returns_failure_code(
        self,
        mock_select: MagicMock,
        mock_claim: MagicMock,
        mock_branch_exists: MagicMock,
        mock_execute_dispatch: MagicMock,
        tmp_path: Path,
    ):
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        mock_select.return_value = {
            "number": 10,
            "title": "Failing issue",
            "body": "body",
            "labels": [],
        }

        from kew.run_store import SqliteRunStore

        db_path = tmp_path / "dispatch.db"
        with SqliteRunStore(str(db_path)) as store:
            rc = _dispatch_one(config, force_issue=10, dry_run=False, store=store)
            assert rc == DispatchCommandResult("executed", 1)

    @patch("kew.cli.select_next_issue")
    def test_dispatch_without_store_still_works(
        self,
        mock_select: MagicMock,
    ):
        """Backward compatibility: no store param means no tracking."""
        config = DispatchConfig()
        mock_select.return_value = None
        rc = _dispatch_one(config, force_issue=None, dry_run=False)
        assert rc == DispatchCommandResult("empty", 0)  # empty queue


class TestCheckPrExists:
    """check_pr_exists queries gh for a PR on the branch.

    check_pr_exists now lives in dispatch_core; these tests use the
    kew.dispatch_core.run patch target accordingly.
    """

    @patch("kew.dispatch_core.run")
    def test_returns_pr_number_when_found(self, mock_run: MagicMock):
        mock_run.return_value = MagicMock(returncode=0, stdout="123\n")
        config = DispatchConfig()
        config.project.repo = "owner/repo"
        assert check_pr_exists("agent/issue-42", config) == 123
        assert mock_run.call_args.args[0] == [
            "gh",
            "api",
            "--method",
            "GET",
            "repos/owner/repo/pulls",
            "-f",
            "head=owner:agent/issue-42",
            "-f",
            "state=all",
            "-f",
            "per_page=1",
            "--jq",
            ".[0].number // empty",
        ]

    @patch("kew.dispatch_core.run")
    def test_returns_none_when_no_pr(self, mock_run: MagicMock):
        mock_run.return_value = MagicMock(returncode=0, stdout="null\n")
        config = DispatchConfig()
        config.project.repo = "owner/repo"
        assert check_pr_exists("agent/issue-42", config) is None

    @patch("kew.dispatch_core.run")
    def test_raises_on_gh_failure(self, mock_run: MagicMock):
        import pytest

        mock_run.return_value = MagicMock(
            returncode=1, stdout="", stderr="gh: not found"
        )
        config = DispatchConfig()
        config.project.repo = "owner/repo"
        with pytest.raises(RuntimeError, match="GitHub REST PR lookup failed"):
            check_pr_exists("agent/issue-42", config)

    @patch("kew.dispatch_core.run")
    def test_rejects_missing_repo_before_building_rest_path(self, mock_run: MagicMock):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="no origin")
        with pytest.raises(RuntimeError, match="repository slug unavailable"):
            check_pr_exists("agent/issue-42", DispatchConfig())
        mock_run.assert_called_once()
        assert mock_run.call_args.args[0] == [
            "git",
            "remote",
            "get-url",
            "origin",
        ]


class TestNoChangesCompletion:
    """When agent exits 0 but no PR exists, use mark_completed_no_changes.

    Label transition logic (check_pr_exists, mark_completed_no_changes) now
    lives in dispatch_core.execute_dispatch. These tests verify that
    _dispatch_one delegates correctly to execute_dispatch.
    """

    @patch("kew.cli.execute_dispatch", return_value=DispatchOutcome(exit_code=0))
    @patch("kew.cli.remote_branch_exists", return_value=False)
    @patch("kew.cli.try_claim_issue", return_value=ClaimResult.CLAIMED)
    @patch("kew.cli.select_next_issue")
    def test_no_pr_calls_mark_completed_no_changes(
        self,
        mock_select: MagicMock,
        mock_claim: MagicMock,
        mock_branch_exists: MagicMock,
        mock_execute_dispatch: MagicMock,
        tmp_path: Path,
    ):
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        mock_select.return_value = {
            "number": 30,
            "title": "Verify Docker runner",
            "body": "",
            "labels": [],
        }

        rc = _dispatch_one(config, force_issue=30, dry_run=False)
        assert rc == DispatchCommandResult("executed", 0)
        mock_execute_dispatch.assert_called_once()

    @patch("kew.cli.execute_dispatch", return_value=DispatchOutcome(exit_code=0))
    @patch("kew.cli.remote_branch_exists", return_value=False)
    @patch("kew.cli.try_claim_issue", return_value=ClaimResult.CLAIMED)
    @patch("kew.cli.select_next_issue")
    def test_pr_exists_calls_mark_completed(
        self,
        mock_select: MagicMock,
        mock_claim: MagicMock,
        mock_branch_exists: MagicMock,
        mock_execute_dispatch: MagicMock,
        tmp_path: Path,
    ):
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        mock_select.return_value = {
            "number": 42,
            "title": "Add feature",
            "body": "",
            "labels": [],
        }

        rc = _dispatch_one(config, force_issue=42, dry_run=False)
        assert rc == DispatchCommandResult("executed", 0)
        mock_execute_dispatch.assert_called_once()


class TestCiFixCommand:
    """Tests for the ci-fix CLI command."""

    @patch("kew.cli.dispatch_ci_fix")
    @patch("kew.cli.build_notifier")
    @patch("kew.cli._preflight")
    @patch("kew.cli.load_config")
    def test_ci_fix_success(
        self,
        mock_config: MagicMock,
        mock_preflight: MagicMock,
        mock_build_notifier: MagicMock,
        mock_dispatch: MagicMock,
        tmp_path,
    ):
        (tmp_path / ".git").mkdir(exist_ok=True)
        mock_config.return_value = DispatchConfig()
        mock_build_notifier.return_value = MagicMock()
        mock_dispatch.return_value = 0

        result = runner.invoke(app, ["ci-fix", "12345"])
        assert result.exit_code == 0
        mock_dispatch.assert_called_once()
        call_kwargs = mock_dispatch.call_args[1]
        assert call_kwargs["run_id"] == "12345"

    @patch("kew.cli.dispatch_ci_fix")
    @patch("kew.cli.build_notifier")
    @patch("kew.cli._preflight")
    @patch("kew.cli.load_config")
    def test_ci_fix_failure_exits_1(
        self,
        mock_config: MagicMock,
        mock_preflight: MagicMock,
        mock_build_notifier: MagicMock,
        mock_dispatch: MagicMock,
    ):
        mock_config.return_value = DispatchConfig()
        mock_build_notifier.return_value = MagicMock()
        mock_dispatch.return_value = 1

        result = runner.invoke(app, ["ci-fix", "12345"])
        assert result.exit_code == 1

    @patch("kew.cli.dispatch_ci_fix")
    @patch("kew.cli.build_notifier")
    @patch("kew.cli._preflight")
    @patch("kew.cli.load_config")
    def test_ci_fix_skip_exits_0(
        self,
        mock_config: MagicMock,
        mock_preflight: MagicMock,
        mock_build_notifier: MagicMock,
        mock_dispatch: MagicMock,
        tmp_path,
    ):
        (tmp_path / ".git").mkdir(exist_ok=True)
        mock_config.return_value = DispatchConfig()
        mock_build_notifier.return_value = MagicMock()
        mock_dispatch.return_value = 2

        result = runner.invoke(app, ["ci-fix", "12345"])
        assert result.exit_code == 0

    @patch("kew.cli.dispatch_ci_fix")
    @patch("kew.cli.build_notifier")
    @patch("kew.cli._preflight")
    @patch("kew.cli.load_config")
    def test_ci_fix_query_failure_exits_2(
        self,
        mock_config: MagicMock,
        mock_preflight: MagicMock,
        mock_build_notifier: MagicMock,
        mock_dispatch: MagicMock,
        tmp_path,
    ):
        """A failed PR lookup query is could-not-run (2), never a clean skip."""
        (tmp_path / ".git").mkdir(exist_ok=True)
        from kew.ci_fix import CI_FIX_QUERY_FAILED

        mock_config.return_value = DispatchConfig()
        mock_build_notifier.return_value = MagicMock()
        mock_dispatch.return_value = CI_FIX_QUERY_FAILED

        result = runner.invoke(app, ["ci-fix", "12345"])
        assert result.exit_code == 2

    @patch("kew.cli.dispatch_ci_fix")
    @patch("kew.cli.build_notifier")
    @patch("kew.cli._preflight")
    @patch("kew.cli.load_config")
    def test_ci_fix_passes_notifier(
        self,
        mock_config: MagicMock,
        mock_preflight: MagicMock,
        mock_build_notifier: MagicMock,
        mock_dispatch: MagicMock,
        tmp_path,
    ):
        (tmp_path / ".git").mkdir(exist_ok=True)
        config = DispatchConfig()
        mock_config.return_value = config
        notifier = MagicMock()
        mock_build_notifier.return_value = notifier
        mock_dispatch.return_value = 0

        result = runner.invoke(app, ["ci-fix", "99999"])
        assert result.exit_code == 0
        mock_build_notifier.assert_called_once_with(config)
        call_kwargs = mock_dispatch.call_args[1]
        assert call_kwargs["notifier"] is notifier
        assert call_kwargs["config"] is config


class TestDuplicateDispatchGuard:
    """Guard against duplicate dispatch when triggered by both manual and CI."""

    @patch("kew.cli.execute_dispatch")
    @patch("kew.cli.remote_branch_exists", return_value=True)
    @patch("kew.cli.select_next_issue")
    def test_skips_when_remote_branch_exists(
        self,
        mock_select: MagicMock,
        mock_branch_exists: MagicMock,
        mock_execute_dispatch: MagicMock,
    ):
        config = DispatchConfig()
        mock_select.return_value = {
            "number": 42,
            "title": "Test",
            "body": "",
            "labels": [],
        }
        rc = _dispatch_one(config, force_issue=42, dry_run=False)
        # Named on the command line and not dispatched: nothing ran (#2058).
        assert rc == DispatchCommandResult("refused", 0)
        mock_execute_dispatch.assert_not_called()

    @patch("kew.cli.execute_dispatch")
    @patch("kew.cli.remote_branch_exists", return_value=False)
    @patch("kew.cli.try_claim_issue", return_value=ClaimResult.ALREADY_CLAIMED)
    @patch("kew.cli.select_next_issue")
    def test_skips_when_claim_fails(
        self,
        mock_select: MagicMock,
        mock_claim: MagicMock,
        mock_branch_exists: MagicMock,
        mock_execute_dispatch: MagicMock,
        capsys,
    ):
        config = DispatchConfig()
        mock_select.return_value = {
            "number": 42,
            "title": "Test",
            "body": "body",
            "labels": [],
        }
        rc = _dispatch_one(config, force_issue=42, dry_run=False)
        assert rc == DispatchCommandResult("refused", 0)
        mock_execute_dispatch.assert_not_called()
        captured = capsys.readouterr()
        assert "another dispatch already claimed it" in captured.err

    @patch("kew.cli.execute_dispatch")
    @patch("kew.cli.remote_branch_exists", return_value=False)
    @patch("kew.cli.try_claim_issue", return_value=ClaimResult.NOT_READY)
    @patch("kew.cli.select_next_issue")
    def test_not_ready_shows_label_hint(
        self,
        mock_select: MagicMock,
        mock_claim: MagicMock,
        mock_branch_exists: MagicMock,
        mock_execute_dispatch: MagicMock,
        capsys,
    ):
        config = DispatchConfig()
        mock_select.return_value = {
            "number": 42,
            "title": "Test",
            "body": "body",
            "labels": [{"name": "enhancement"}, {"name": "content-ops"}],
        }
        rc = _dispatch_one(config, force_issue=42, dry_run=False)
        assert rc == DispatchCommandResult("refused", 0)
        mock_execute_dispatch.assert_not_called()
        captured = capsys.readouterr()
        assert "not labeled 'ready'" in captured.err
        assert "content-ops, enhancement" in captured.err
        # --force never bypassed the ready label; no flag does, so none is hinted.
        assert "--force" not in captured.err


class TestBudgetExceeded:
    """Budget exhaustion is handled by execute_dispatch in dispatch_core.

    _dispatch_one is a thin wrapper: these tests verify it delegates
    correctly and returns the exit code from execute_dispatch.
    Detailed budget logic tests live in test_dispatch_core.py.
    """

    @patch("kew.cli.execute_dispatch", return_value=DispatchOutcome(exit_code=1))
    @patch("kew.cli.remote_branch_exists", return_value=False)
    @patch("kew.cli.try_claim_issue", return_value=ClaimResult.CLAIMED)
    @patch("kew.cli.select_next_issue")
    def test_budget_exceeded_with_uncommitted_work_preserves_worktree(
        self,
        mock_select: MagicMock,
        mock_claim: MagicMock,
        mock_branch_exists: MagicMock,
        mock_execute_dispatch: MagicMock,
        tmp_path: Path,
    ):
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        mock_select.return_value = {
            "number": 99,
            "title": "Budget test",
            "body": "",
            "labels": [],
        }

        rc = _dispatch_one(config, force_issue=99, dry_run=False)
        assert rc == DispatchCommandResult("executed", 1)
        mock_execute_dispatch.assert_called_once()

    @patch("kew.cli.execute_dispatch", return_value=DispatchOutcome(exit_code=0))
    @patch("kew.cli.remote_branch_exists", return_value=False)
    @patch("kew.cli.try_claim_issue", return_value=ClaimResult.CLAIMED)
    @patch("kew.cli.select_next_issue")
    def test_budget_exceeded_clean_worktree_marks_completed(
        self,
        mock_select: MagicMock,
        mock_claim: MagicMock,
        mock_branch_exists: MagicMock,
        mock_execute_dispatch: MagicMock,
        tmp_path: Path,
    ):
        """Budget exceeded but no uncommitted work — agent may have finished."""
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        mock_select.return_value = {
            "number": 100,
            "title": "Budget clean test",
            "body": "",
            "labels": [],
        }

        rc = _dispatch_one(config, force_issue=100, dry_run=False)
        assert rc == DispatchCommandResult("executed", 0)
        mock_execute_dispatch.assert_called_once()

    @patch("kew.cli.execute_dispatch", return_value=DispatchOutcome(exit_code=1))
    @patch("kew.cli.remote_branch_exists", return_value=False)
    @patch("kew.cli.try_claim_issue", return_value=ClaimResult.CLAIMED)
    @patch("kew.cli.select_next_issue")
    def test_budget_exceeded_does_not_mark_completed(
        self,
        mock_select: MagicMock,
        mock_claim: MagicMock,
        mock_branch_exists: MagicMock,
        mock_execute_dispatch: MagicMock,
        tmp_path: Path,
    ):
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        mock_select.return_value = {
            "number": 42,
            "title": "Test issue",
            "body": "body",
            "labels": [],
        }

        rc = _dispatch_one(config, force_issue=42, dry_run=False)
        assert rc == DispatchCommandResult("executed", 1)
        mock_execute_dispatch.assert_called_once()

    @patch("kew.cli.execute_dispatch", return_value=DispatchOutcome(exit_code=1))
    @patch("kew.cli.remote_branch_exists", return_value=False)
    @patch("kew.cli.try_claim_issue", return_value=ClaimResult.CLAIMED)
    @patch("kew.cli.select_next_issue")
    def test_budget_exceeded_preserves_worktree(
        self,
        mock_select: MagicMock,
        mock_claim: MagicMock,
        mock_branch_exists: MagicMock,
        mock_execute_dispatch: MagicMock,
        tmp_path: Path,
    ):
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        mock_select.return_value = {
            "number": 7,
            "title": "Budget test",
            "body": "",
            "labels": [],
        }

        rc = _dispatch_one(config, force_issue=7, dry_run=False)
        assert rc == DispatchCommandResult("executed", 1)
        mock_execute_dispatch.assert_called_once()

    @patch("kew.cli.execute_dispatch", return_value=DispatchOutcome(exit_code=1))
    @patch("kew.cli.remote_branch_exists", return_value=False)
    @patch("kew.cli.try_claim_issue", return_value=ClaimResult.CLAIMED)
    @patch("kew.cli.select_next_issue")
    def test_budget_exceeded_adds_budget_label(
        self,
        mock_select: MagicMock,
        mock_claim: MagicMock,
        mock_branch_exists: MagicMock,
        mock_execute_dispatch: MagicMock,
        tmp_path: Path,
    ):
        """Budget label handling is verified in test_dispatch_core.py.
        Here we verify _dispatch_one passes config (which has budget_exceeded label)
        through to execute_dispatch.
        """
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        mock_select.return_value = {
            "number": 42,
            "title": "Budget test",
            "body": "",
            "labels": [],
        }

        _dispatch_one(config, force_issue=42, dry_run=False)
        call_kwargs = mock_execute_dispatch.call_args.kwargs
        assert call_kwargs["config"] is config

    @patch("kew.cli.execute_dispatch", return_value=DispatchOutcome(exit_code=1))
    @patch("kew.cli.remote_branch_exists", return_value=False)
    @patch("kew.cli.try_claim_issue", return_value=ClaimResult.CLAIMED)
    @patch("kew.cli.select_next_issue")
    def test_budget_exceeded_records_failure_in_store(
        self,
        mock_select: MagicMock,
        mock_claim: MagicMock,
        mock_branch_exists: MagicMock,
        mock_execute_dispatch: MagicMock,
        tmp_path: Path,
    ):
        """Store recording is done inside execute_dispatch (dispatch_core).
        Verify _dispatch_one passes the store through.
        """
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        mock_select.return_value = {
            "number": 42,
            "title": "Budget test",
            "body": "",
            "labels": [],
        }

        from kew.run_store import SqliteRunStore

        db_path = tmp_path / "dispatch.db"
        with SqliteRunStore(str(db_path)) as store:
            _dispatch_one(config, force_issue=42, dry_run=False, store=store)

        call_kwargs = mock_execute_dispatch.call_args.kwargs
        assert call_kwargs["store"] is store


class TestNotifierIntegration:
    """Notifier is wired into _dispatch_one and run_cmd.

    Notification logic (event firing) now lives in dispatch_core.execute_dispatch.
    These tests verify _dispatch_one passes the notifier through correctly.
    """

    @patch("kew.cli.execute_dispatch", return_value=DispatchOutcome(exit_code=0))
    @patch("kew.cli.remote_branch_exists", return_value=False)
    @patch("kew.cli.try_claim_issue", return_value=ClaimResult.CLAIMED)
    @patch("kew.cli.select_next_issue")
    def test_dispatch_sends_success_notification(
        self,
        mock_select: MagicMock,
        mock_claim: MagicMock,
        mock_branch_exists: MagicMock,
        mock_execute_dispatch: MagicMock,
        tmp_path: Path,
    ):
        config = DispatchConfig()
        config.alerting.notify_on_success = True
        mock_select.return_value = {
            "number": 42,
            "title": "Test issue",
            "body": "body",
            "labels": [],
        }

        notifier = MagicMock()
        rc = _dispatch_one(config, force_issue=42, dry_run=False, notifier=notifier)
        assert rc == DispatchCommandResult("executed", 0)
        call_kwargs = mock_execute_dispatch.call_args.kwargs
        assert call_kwargs["notifier"] is notifier

    @patch("kew.cli.execute_dispatch", return_value=DispatchOutcome(exit_code=0))
    @patch("kew.cli.remote_branch_exists", return_value=False)
    @patch("kew.cli.try_claim_issue", return_value=ClaimResult.CLAIMED)
    @patch("kew.cli.select_next_issue")
    def test_dispatch_no_success_notification_when_disabled(
        self,
        mock_select: MagicMock,
        mock_claim: MagicMock,
        mock_branch_exists: MagicMock,
        mock_execute_dispatch: MagicMock,
        tmp_path: Path,
    ):
        config = DispatchConfig()
        config.alerting.notify_on_success = False
        mock_select.return_value = {
            "number": 42,
            "title": "Test issue",
            "body": "body",
            "labels": [],
        }

        notifier = MagicMock()
        rc = _dispatch_one(config, force_issue=42, dry_run=False, notifier=notifier)
        assert rc == DispatchCommandResult("executed", 0)
        # notifier is passed through; dispatch_core respects notify_on_success
        call_kwargs = mock_execute_dispatch.call_args.kwargs
        assert call_kwargs["notifier"] is notifier

    @patch("kew.cli.execute_dispatch", return_value=DispatchOutcome(exit_code=1))
    @patch("kew.cli.remote_branch_exists", return_value=False)
    @patch("kew.cli.try_claim_issue", return_value=ClaimResult.CLAIMED)
    @patch("kew.cli.select_next_issue")
    def test_dispatch_sends_failure_notification(
        self,
        mock_select: MagicMock,
        mock_claim: MagicMock,
        mock_branch_exists: MagicMock,
        mock_execute_dispatch: MagicMock,
        tmp_path: Path,
    ):
        config = DispatchConfig()
        config.alerting.notify_on_failure = True
        mock_select.return_value = {
            "number": 10,
            "title": "Failing issue",
            "body": "body",
            "labels": [],
        }

        notifier = MagicMock()
        rc = _dispatch_one(config, force_issue=10, dry_run=False, notifier=notifier)
        assert rc == DispatchCommandResult("executed", 1)
        call_kwargs = mock_execute_dispatch.call_args.kwargs
        assert call_kwargs["notifier"] is notifier

    @patch("kew.cli.execute_dispatch", return_value=DispatchOutcome(exit_code=1))
    @patch("kew.cli.remote_branch_exists", return_value=False)
    @patch("kew.cli.try_claim_issue", return_value=ClaimResult.CLAIMED)
    @patch("kew.cli.select_next_issue")
    def test_dispatch_no_failure_notification_when_disabled(
        self,
        mock_select: MagicMock,
        mock_claim: MagicMock,
        mock_branch_exists: MagicMock,
        mock_execute_dispatch: MagicMock,
        tmp_path: Path,
    ):
        config = DispatchConfig()
        config.alerting.notify_on_failure = False
        mock_select.return_value = {
            "number": 10,
            "title": "Failing issue",
            "body": "body",
            "labels": [],
        }

        notifier = MagicMock()
        rc = _dispatch_one(config, force_issue=10, dry_run=False, notifier=notifier)
        assert rc == DispatchCommandResult("executed", 1)
        # notifier is passed through; dispatch_core respects notify_on_failure
        call_kwargs = mock_execute_dispatch.call_args.kwargs
        assert call_kwargs["notifier"] is notifier

    @patch("kew.cli.execute_dispatch", return_value=DispatchOutcome(exit_code=0))
    @patch("kew.cli.remote_branch_exists", return_value=False)
    @patch("kew.cli.try_claim_issue", return_value=ClaimResult.CLAIMED)
    @patch("kew.cli.select_next_issue")
    def test_dispatch_no_notifier_still_works(
        self,
        mock_select: MagicMock,
        mock_claim: MagicMock,
        mock_branch_exists: MagicMock,
        mock_execute_dispatch: MagicMock,
        tmp_path: Path,
    ):
        """Backward compatibility: no notifier param means no notifications."""
        config = DispatchConfig()
        config.alerting.notify_on_success = True
        mock_select.return_value = {
            "number": 42,
            "title": "Test issue",
            "body": "body",
            "labels": [],
        }

        # No notifier passed — should not raise
        rc = _dispatch_one(config, force_issue=42, dry_run=False)
        assert rc == DispatchCommandResult("executed", 0)

    @patch("kew.cli.build_notifier")
    @patch("kew.cli._preflight")
    @patch("kew.cli._dispatch_one")
    @patch("kew.cli.load_config")
    def test_run_cmd_builds_and_passes_notifier(
        self,
        mock_load: MagicMock,
        mock_dispatch: MagicMock,
        mock_preflight: MagicMock,
        mock_build_notifier: MagicMock,
        tmp_path,
    ):
        (tmp_path / ".git").mkdir(exist_ok=True)
        config = DispatchConfig()
        mock_load.return_value = config
        mock_dispatch.return_value = DispatchCommandResult("executed")
        mock_notifier = MagicMock()
        mock_build_notifier.return_value = mock_notifier

        result = runner.invoke(app, ["run"])
        assert result.exit_code == 0
        mock_build_notifier.assert_called_once_with(config)
        # Verify notifier was passed to _dispatch_one
        call_args = mock_dispatch.call_args
        if "notifier" in call_args.kwargs:
            assert call_args.kwargs["notifier"] is mock_notifier
        else:
            assert mock_notifier in call_args.args


class TestTuiCommand:
    def test_tui_command_exists(self):
        from typer.testing import CliRunner

        from kew.cli import app

        runner = CliRunner()
        result = runner.invoke(app, ["tui", "--help"])
        assert result.exit_code == 0
        assert "tui" in result.output.lower() or "dashboard" in result.output.lower()

    def test_tui_launches_kew_app(self, tmp_path):
        """kew tui constructs KewApp with correct config and db_path."""
        (tmp_path / ".git").mkdir(exist_ok=True)
        from typer.testing import CliRunner

        from kew.cli import app

        runner = CliRunner()
        with (
            patch("kew.cli.load_config") as mock_config,
            patch("kew.cli._preflight"),
            patch("kew.tui.app.KewApp") as mock_app_cls,
        ):
            config = MagicMock()
            config.logging.dir = str(tmp_path)
            mock_config.return_value = config
            mock_app_instance = MagicMock()
            mock_app_cls.return_value = mock_app_instance

            runner.invoke(app, ["tui"])

        mock_app_cls.assert_called_once()
        call_kwargs = mock_app_cls.call_args[1]
        assert call_kwargs["config"] is config
        assert "dispatch.db" in call_kwargs["db_path"]
        mock_app_instance.run.assert_called_once()

    def test_tui_resolves_db_path(self, tmp_path, monkeypatch):
        """kew tui passes an absolute (resolved) db_path so the dashboard opens
        the same store regardless of cwd."""
        (tmp_path / ".git").mkdir(exist_ok=True)
        from typer.testing import CliRunner

        from kew.cli import app

        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        with (
            patch("kew.cli.load_config") as mock_config,
            patch("kew.cli._preflight"),
            patch("kew.tui.app.KewApp") as mock_app_cls,
        ):
            config = MagicMock()
            config.logging.dir = "./logs"  # relative — must be resolved
            mock_config.return_value = config
            runner.invoke(app, ["tui"])

        db_path = mock_app_cls.call_args[1]["db_path"]
        assert Path(db_path).is_absolute()


class TestAttachCommand:
    def test_attach_command_exists(self):
        from typer.testing import CliRunner

        from kew.cli import app

        runner = CliRunner()
        result = runner.invoke(app, ["attach", "--help"])
        assert result.exit_code == 0

    def test_attach_no_pending_run(self, tmp_path):
        from typer.testing import CliRunner

        from kew.cli import app

        runner = CliRunner()
        with (
            patch("kew.cli.load_config") as mock_config,
            patch("kew.cli._preflight"),
            patch("kew.cli._open_run_store") as mock_store,
        ):
            config = MagicMock()
            config.logging.dir = str(tmp_path)
            mock_config.return_value = config

            store = mock_store.return_value.__enter__.return_value
            store.list_pending.return_value = []

            result = runner.invoke(app, ["attach", "999"])
            assert result.exit_code == 2
            assert "no running dispatch" in result.output.lower()

    def test_attach_tails_events(self, tmp_path):
        """attach prints events from the event file and exits on finished."""

        from typer.testing import CliRunner

        from kew.cli import app

        # Create log file and event file
        log_file = tmp_path / "dispatch-42-test.log"
        log_file.touch()
        event_file = tmp_path / "dispatch-42-test.events.jsonl"
        event_file.touch()

        initial_events = [
            {"type": "started", "ts": 1.0, "runner": "claude-code", "model": "sonnet"},
            {"type": "activity", "ts": 2.0, "msg": "Reading file"},
        ]
        finished_event = {
            "type": "finished",
            "ts": 3.0,
            "exit_code": 0,
            "cost_usd": 1.50,
        }

        # tail_events: first call returns initial events (offset 0), second call
        # returns the finished event (polling loop), third+ calls return nothing
        tail_calls = []

        def fake_tail_events(path, offset=0):
            tail_calls.append(offset)
            if len(tail_calls) == 1:
                return initial_events, 100
            if len(tail_calls) == 2:
                return [finished_event], 200
            return [], 200

        run = MagicMock(
            run_id="r1",
            issue_num=42,
            runner_name="claude-code",
            model="sonnet",
            log_file=log_file,
        )

        runner = CliRunner()
        with (
            patch("kew.cli.load_config") as mock_config,
            patch("kew.cli._preflight"),
            patch("kew.cli._open_run_store") as mock_store,
            patch("kew.tui.state.tail_events", side_effect=fake_tail_events),
            patch("time.sleep"),
        ):
            config = MagicMock()
            config.logging.dir = str(tmp_path)
            mock_config.return_value = config
            store = mock_store.return_value.__enter__.return_value
            store.list_pending.return_value = [run]

            result = runner.invoke(app, ["attach", "42"])

        assert result.exit_code == 0
        assert "Reading file" in result.output
        assert "Finished" in result.output

    def test_attach_ctrl_c_detaches(self, tmp_path):
        """attach prints 'Detached.' on KeyboardInterrupt."""
        from typer.testing import CliRunner

        from kew.cli import app

        log_file = tmp_path / "dispatch-42-test.log"
        log_file.touch()
        event_file = tmp_path / "dispatch-42-test.events.jsonl"
        event_file.write_text("")  # Empty — will poll

        run = MagicMock(
            run_id="r1",
            issue_num=42,
            runner_name="claude-code",
            model="sonnet",
            log_file=log_file,
        )

        runner = CliRunner()
        with (
            patch("kew.cli.load_config") as mock_config,
            patch("kew.cli._preflight"),
            patch("kew.cli._open_run_store") as mock_store,
            patch("time.sleep", side_effect=KeyboardInterrupt),
        ):
            config = MagicMock()
            config.logging.dir = str(tmp_path)
            mock_config.return_value = config
            store = mock_store.return_value.__enter__.return_value
            store.list_pending.return_value = [run]

            result = runner.invoke(app, ["attach", "42"])

        assert "Detached" in result.output

    def test_attach_timeout_detaches(self, tmp_path):
        """attach detaches after 2-hour timeout."""
        from typer.testing import CliRunner

        from kew.cli import app

        log_file = tmp_path / "dispatch-42-test.log"
        log_file.touch()
        event_file = tmp_path / "dispatch-42-test.events.jsonl"
        event_file.write_text("")

        run = MagicMock(
            run_id="r1",
            issue_num=42,
            runner_name="claude-code",
            model="sonnet",
            log_file=log_file,
        )

        # Simulate time passing beyond 7200s
        call_count = 0
        start_time = 1000.0

        def mock_monotonic():
            nonlocal call_count
            call_count += 1
            # First call sets start, subsequent calls exceed timeout
            if call_count <= 1:
                return start_time
            return start_time + 7201

        runner = CliRunner()
        with (
            patch("kew.cli.load_config") as mock_config,
            patch("kew.cli._preflight"),
            patch("kew.cli._open_run_store") as mock_store,
            patch("time.sleep"),
            patch("time.monotonic", side_effect=mock_monotonic),
        ):
            config = MagicMock()
            config.logging.dir = str(tmp_path)
            mock_config.return_value = config
            store = mock_store.return_value.__enter__.return_value
            store.list_pending.return_value = [run]

            result = runner.invoke(app, ["attach", "42"])

        assert "timeout" in result.output.lower() or "Timeout" in result.output


class TestEnhancedJsonExport:
    def test_status_json_includes_queue_and_budget(self, tmp_path):
        """kew status --json includes queue, workers, budget."""
        (tmp_path / ".git").mkdir(exist_ok=True)
        import json

        from typer.testing import CliRunner

        from kew.cli import app

        runner = CliRunner()
        with (
            patch("kew.cli.load_config") as mock_config,
            patch("kew.cli._preflight"),
            patch("kew.cli.list_ready_issues") as mock_queue,
            patch("kew.cli.run_with_backoff") as mock_run,
        ):
            config = MagicMock()
            config.budget.daily_usd = 50.0
            config.budget.weekly_usd = 200.0
            config.budget.per_issue_usd = 15.0
            config.logging.dir = "/tmp/kew-test-export"
            mock_config.return_value = config
            mock_queue.return_value = [
                {
                    "number": 1,
                    "title": "Test",
                    "labels": [],
                    "createdAt": "2026-03-20T10:00:00Z",
                },
            ]
            mock_run.return_value = MagicMock(returncode=0, stdout="0")

            import tempfile

            with tempfile.TemporaryDirectory() as tmp_dir:
                config.logging.dir = tmp_dir
                result = runner.invoke(app, ["status", "--json"])
            assert result.exit_code == 0, f"status --json failed: {result.output}"
            data = json.loads(result.output)
            assert "queue" in data
            assert "budget" in data
            assert "workers" in data
            assert "runs" in data

    def test_status_json_queue_has_issues_list(self, tmp_path):
        """queue field contains count and issues list."""
        (tmp_path / ".git").mkdir(exist_ok=True)
        import json

        from typer.testing import CliRunner

        from kew.cli import app

        runner = CliRunner()
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        (tmp_path / "logs").mkdir(parents=True, exist_ok=True)

        with (
            patch("kew.cli.load_config") as mock_config,
            patch("kew.cli._preflight"),
            patch("kew.cli.list_ready_issues") as mock_queue,
            patch("kew.cli.run_with_backoff") as mock_run,
        ):
            mock_config.return_value = config
            mock_queue.return_value = [
                {
                    "number": 7,
                    "title": "Fix the bug",
                    "labels": [{"name": "ready"}, {"name": "medium"}],
                    "createdAt": "2026-03-20T10:00:00Z",
                },
            ]
            mock_run.return_value = MagicMock(returncode=0, stdout="0")

            result = runner.invoke(app, ["status", "--json"])
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["queue"]["count"] == 1
            issue = data["queue"]["issues"][0]
            assert issue["number"] == 7
            assert issue["title"] == "Fix the bug"
            assert "ready" in issue["labels"]

    def test_status_json_budget_fields(self, tmp_path):
        """budget field includes daily/weekly/per_issue limits."""
        (tmp_path / ".git").mkdir(exist_ok=True)
        import json

        from typer.testing import CliRunner

        from kew.cli import app
        from kew.config import BudgetConfig, DispatchConfig

        runner = CliRunner()
        config = DispatchConfig()
        config.budget = BudgetConfig(
            daily_usd=100.0, weekly_usd=500.0, per_issue_usd=20.0
        )
        config.logging.dir = str(tmp_path / "logs")
        (tmp_path / "logs").mkdir(parents=True, exist_ok=True)

        with (
            patch("kew.cli.load_config") as mock_config,
            patch("kew.cli._preflight"),
            patch("kew.cli.list_ready_issues") as mock_queue,
            patch("kew.cli.run_with_backoff") as mock_run,
        ):
            mock_config.return_value = config
            mock_queue.return_value = []
            mock_run.return_value = MagicMock(returncode=0, stdout="0")

            result = runner.invoke(app, ["status", "--json"])
            assert result.exit_code == 0
            data = json.loads(result.output)
            budget = data["budget"]
            assert budget["daily_limit"] == 100.0
            assert budget["weekly_limit"] == 500.0
            assert budget["per_issue_limit"] == 20.0


class TestRunModelFlag:
    """--model flag overrides the config model for that dispatch."""

    @pytest.mark.parametrize("command", [["run"], ["dispatch", "42"]])
    @patch("kew.cli._preflight")
    @patch("kew.cli.select_next_issue")
    @patch("kew.cli.try_claim_issue", return_value=ClaimResult.CLAIMED)
    @patch("kew.cli.remote_branch_exists", return_value=False)
    @patch("kew.cli.execute_dispatch", return_value=DispatchOutcome(exit_code=0))
    @patch("kew.cli.load_config")
    def test_model_flag_passed_to_execute_dispatch(
        self,
        mock_config: MagicMock,
        mock_execute: MagicMock,
        mock_branch: MagicMock,
        mock_claim: MagicMock,
        mock_select: MagicMock,
        mock_preflight: MagicMock,
        command: list[str],
        tmp_path: Path,
    ):
        (tmp_path / ".git").mkdir(exist_ok=True)
        from kew.config import DispatchConfig

        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        mock_config.return_value = config
        mock_select.return_value = {
            "number": 42,
            "title": "Test Issue",
            "body": "body",
            "labels": [{"name": "ready"}],
        }

        result = runner.invoke(app, [*command, "--model", "claude-opus-4-6"])
        assert result.exit_code == 0
        call_kwargs = mock_execute.call_args[1]
        assert call_kwargs["model_override"] == "claude-opus-4-6"

    @patch("kew.cli._preflight")
    @patch("kew.cli.load_config")
    def test_model_flag_rejected_with_drain(
        self,
        mock_config: MagicMock,
        mock_preflight: MagicMock,
        tmp_path: Path,
    ):
        from kew.config import DispatchConfig

        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        mock_config.return_value = config

        result = runner.invoke(app, ["run", "--drain", "--model", "claude-opus-4-6"])
        assert result.exit_code == 2
        assert "--model is not currently supported with --drain" in result.output


class TestCleanupCommand:
    """Tests for the kew cleanup command."""

    @patch("kew.cli._preflight")
    @patch("kew.cli.load_config")
    def test_no_orphaned_issues(
        self, mock_config: MagicMock, mock_preflight: MagicMock, tmp_path: Path
    ) -> None:
        """When no issues have agent-working label, reports clean state."""
        (tmp_path / ".git").mkdir(exist_ok=True)
        from kew.config import DispatchConfig

        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        mock_config.return_value = config

        with patch("kew.labels.fetch_agent_working_issues", return_value=[]):
            result = runner.invoke(app, ["cleanup"])

        assert result.exit_code == 0
        assert "No orphaned" in result.output

    @patch("kew.cli._preflight")
    @patch("kew.cli.load_config")
    def test_identifies_orphaned_issues(
        self, mock_config: MagicMock, mock_preflight: MagicMock, tmp_path: Path
    ) -> None:
        """Issues with agent-working but no pending run are reported as orphaned."""
        (tmp_path / ".git").mkdir(exist_ok=True)
        from kew.config import DispatchConfig

        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        mock_config.return_value = config

        with patch(
            "kew.labels.fetch_agent_working_issues",
            return_value=[
                {
                    "number": 99,
                    "title": "Orphaned issue",
                    "labels": [{"name": "agent-working"}, {"name": "in-progress"}],
                }
            ],
        ):
            result = runner.invoke(app, ["cleanup"])

        assert result.exit_code == 1
        assert "#99" in result.output
        assert "Orphaned issue" in result.output

    @patch("kew.cli._preflight")
    @patch("kew.cli.load_config")
    def test_active_runs_not_reported_as_orphaned(
        self, mock_config: MagicMock, mock_preflight: MagicMock, tmp_path: Path
    ) -> None:
        """Issues with agent-working AND a matching pending run are not orphaned."""
        (tmp_path / ".git").mkdir(exist_ok=True)
        from datetime import UTC, datetime

        from kew.config import DispatchConfig
        from kew.run_store import RunRecord, SqliteRunStore

        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        mock_config.return_value = config

        store_path = Path(config.logging.dir) / "dispatch.db"
        store_path.parent.mkdir(parents=True, exist_ok=True)
        with SqliteRunStore(str(store_path)) as store:
            store.save(
                RunRecord(
                    run_id="run-001",
                    issue_num=42,
                    branch="agent/issue-42",
                    status="pending",
                    exit_code=0,
                    started_at=datetime.now(UTC),
                )
            )

        with patch(
            "kew.labels.fetch_agent_working_issues",
            return_value=[
                {
                    "number": 42,
                    "title": "Active issue",
                    "labels": [{"name": "agent-working"}],
                }
            ],
        ):
            result = runner.invoke(app, ["cleanup"])

        assert result.exit_code == 0
        assert "No orphaned" in result.output

    @patch("kew.cli._preflight")
    @patch("kew.cli.load_config")
    def test_dry_run_shows_without_resetting(
        self, mock_config: MagicMock, mock_preflight: MagicMock, tmp_path: Path
    ) -> None:
        """Dry run reports what would change but does not call label updates."""
        (tmp_path / ".git").mkdir(exist_ok=True)
        from kew.config import DispatchConfig

        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        mock_config.return_value = config

        with (
            patch(
                "kew.labels.fetch_agent_working_issues",
                return_value=[
                    {
                        "number": 99,
                        "title": "Orphaned",
                        "labels": [{"name": "agent-working"}],
                    }
                ],
            ),
            patch("kew.labels.reset_orphaned_issue") as mock_reset,
        ):
            result = runner.invoke(app, ["cleanup", "--dry-run", "--reset-to", "ready"])

        assert result.exit_code == 1
        assert "DRY RUN" in result.output
        mock_reset.assert_not_called()

    @patch("kew.cli._preflight")
    @patch("kew.cli.load_config")
    def test_dry_run_previews_stale_sweep_without_queued_resets(
        self,
        mock_config: MagicMock,
        mock_preflight: MagicMock,
        tmp_path: Path,
    ) -> None:
        """F18: --dry-run changes nothing yet reports what the real run does.

        A crashed run leaves a stale pending row while its issue still carries
        the working labels. The real sweep marks the row failed, which makes
        the issue orphaned. With no stop-label reset queued, the preview must
        report the same orphan and exit code without writing the row or
        retrying label resets.
        """
        (tmp_path / ".git").mkdir(exist_ok=True)
        from datetime import UTC, datetime, timedelta

        from click.testing import Result

        from kew.config import DispatchConfig
        from kew.run_store import RunRecord, SqliteRunStore

        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        mock_config.return_value = config

        store_path = Path(config.logging.dir) / "dispatch.db"
        store_path.parent.mkdir(parents=True, exist_ok=True)
        with SqliteRunStore(str(store_path)) as store:
            store.save(
                RunRecord(
                    run_id="run-stale",
                    issue_num=77,
                    branch="agent/issue-77",
                    status="pending",
                    exit_code=0,
                    started_at=datetime.now(UTC) - timedelta(hours=48),
                    pgid=424242,
                )
            )
            before = store.get("run-stale")

        issue = {
            "number": 77,
            "title": "Crashed issue",
            "labels": [{"name": "agent-working"}, {"name": "in-progress"}],
        }

        def invoke(args: list[str]) -> tuple[Result, MagicMock]:
            with (
                patch("kew.run_store._stale_host_is_dead", return_value=True),
                patch.object(SqliteRunStore, "retry_stop_label_resets") as resets,
                patch("kew.labels.fetch_agent_working_issues", return_value=[issue]),
                patch("kew.labels.reset_orphaned_issue") as reset_issue,
            ):
                result = runner.invoke(app, args)
            reset_issue.assert_not_called()
            return result, resets

        preview, preview_resets = invoke(["cleanup", "--dry-run"])
        with SqliteRunStore(str(store_path)) as store:
            assert store.get("run-stale") == before
        preview_resets.assert_not_called()
        assert "Would clean up 1 stale pending record(s)" in preview.output
        assert "run-stale — #77" in preview.output
        assert "Cleaned up" not in preview.output

        real, real_resets = invoke(["cleanup"])
        with SqliteRunStore(str(store_path)) as store:
            assert store.get("run-stale").status == "failure"
        real_resets.assert_called()
        assert "Cleaned up 1 stale pending record(s)" in real.output

        for result in (preview, real):
            assert "#77" in result.output and "Crashed issue" in result.output
            assert "No orphaned" not in result.output
        # No stop-label reset is queued here (the outbox retry is patched),
        # so the preview matches the real run. Queued resets are not previewed.
        assert preview.exit_code == real.exit_code == 1, (
            preview.output,
            real.output,
        )

    @patch("kew.cli._preflight")
    @patch("kew.cli.load_config")
    def test_reset_to_ready(
        self, mock_config: MagicMock, mock_preflight: MagicMock, tmp_path: Path
    ) -> None:
        """--reset-to ready calls reset_orphaned_issue with target='ready'."""
        (tmp_path / ".git").mkdir(exist_ok=True)
        from kew.config import DispatchConfig

        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        mock_config.return_value = config

        with (
            patch(
                "kew.labels.fetch_agent_working_issues",
                return_value=[
                    {
                        "number": 99,
                        "title": "Orphaned",
                        "labels": [
                            {"name": "agent-working"},
                            {"name": "in-progress"},
                        ],
                    }
                ],
            ),
            patch("kew.labels.reset_orphaned_issue") as mock_reset,
        ):
            result = runner.invoke(app, ["cleanup", "--reset-to", "ready"])

        assert result.exit_code == 1
        mock_reset.assert_called_once_with(
            99,
            config,
            target="ready",
            current_labels=["agent-working", "in-progress"],
        )

    @patch("kew.cli._preflight")
    @patch("kew.cli.load_config")
    def test_reset_to_agent_failed(
        self, mock_config: MagicMock, mock_preflight: MagicMock, tmp_path: Path
    ) -> None:
        """--reset-to agent-failed resets orphaned issues to agent-failed state."""
        (tmp_path / ".git").mkdir(exist_ok=True)
        from kew.config import DispatchConfig

        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        mock_config.return_value = config

        with (
            patch(
                "kew.labels.fetch_agent_working_issues",
                return_value=[
                    {
                        "number": 99,
                        "title": "Orphaned",
                        "labels": [{"name": "agent-working"}],
                    }
                ],
            ),
            patch("kew.labels.reset_orphaned_issue") as mock_reset,
        ):
            result = runner.invoke(app, ["cleanup", "--reset-to", "agent-failed"])

        assert result.exit_code == 1
        mock_reset.assert_called_once_with(
            99,
            config,
            target="agent-failed",
            current_labels=["agent-working"],
        )

    @patch("kew.cli._preflight")
    @patch("kew.cli.load_config")
    def test_invalid_reset_to_exits_with_error(
        self, mock_config: MagicMock, mock_preflight: MagicMock, tmp_path: Path
    ) -> None:
        """An invalid --reset-to value prevents cleanup and exits with code 2."""
        from kew.config import DispatchConfig

        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        mock_config.return_value = config

        result = runner.invoke(app, ["cleanup", "--reset-to", "invalid"])
        assert result.exit_code == 2


class TestStatusAnalytics:
    """kew status --analytics outputs aggregate dispatch metrics."""

    @patch("kew.cli._preflight")
    @patch("kew.cli.load_config")
    def test_analytics_shows_success_rate(
        self, mock_config: MagicMock, mock_preflight: MagicMock, tmp_path: Path
    ) -> None:
        (tmp_path / ".git").mkdir(exist_ok=True)
        from datetime import UTC, datetime

        from kew.config import DispatchConfig
        from kew.run_store import RunRecord, SqliteRunStore

        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        mock_config.return_value = config

        # Populate real DB with test data
        log_dir = tmp_path / "logs"
        log_dir.mkdir(parents=True)
        db_path = str(log_dir / "dispatch.db")
        with SqliteRunStore(db_path) as store:
            store.save(
                RunRecord(
                    run_id="a1",
                    issue_num=1,
                    branch="b1",
                    status="success",
                    exit_code=0,
                    started_at=datetime.now(UTC),
                    runner_name="claude-code",
                    model="claude-sonnet-4-6",
                    estimated_cost_usd=0.10,
                    duration_seconds=60.0,
                )
            )
            store.save(
                RunRecord(
                    run_id="a2",
                    issue_num=2,
                    branch="b2",
                    status="failure",
                    exit_code=1,
                    started_at=datetime.now(UTC),
                    runner_name="claude-code",
                    model="claude-sonnet-4-6",
                    error_category="transient",
                )
            )

        result = runner.invoke(app, ["status", "--analytics"])
        assert result.exit_code == 0
        assert "1/2" in result.output
        assert "50" in result.output
        mock_preflight.assert_not_called()

    @patch("kew.cli._preflight")
    @patch("kew.cli.load_config")
    def test_analytics_shows_cost_by_runner(
        self, mock_config: MagicMock, mock_preflight: MagicMock, tmp_path: Path
    ) -> None:
        (tmp_path / ".git").mkdir(exist_ok=True)
        from datetime import UTC, datetime

        from kew.config import DispatchConfig
        from kew.run_store import RunRecord, SqliteRunStore

        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        mock_config.return_value = config

        log_dir = tmp_path / "logs"
        log_dir.mkdir(parents=True)
        db_path = str(log_dir / "dispatch.db")
        with SqliteRunStore(db_path) as store:
            store.save(
                RunRecord(
                    run_id="b1",
                    issue_num=10,
                    branch="b10",
                    status="success",
                    exit_code=0,
                    started_at=datetime.now(UTC),
                    runner_name="my-runner",
                    estimated_cost_usd=0.25,
                )
            )

        result = runner.invoke(app, ["status", "--analytics"])
        assert result.exit_code == 0
        assert "my-runner" in result.output
        mock_preflight.assert_not_called()

    @patch("kew.cli._preflight")
    @patch("kew.cli.load_config")
    def test_analytics_empty_store_does_not_crash(
        self, mock_config: MagicMock, mock_preflight: MagicMock, tmp_path: Path
    ) -> None:
        (tmp_path / ".git").mkdir(exist_ok=True)
        from kew.config import DispatchConfig
        from kew.run_store import SqliteRunStore

        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        mock_config.return_value = config

        log_dir = tmp_path / "logs"
        log_dir.mkdir(parents=True)
        # Create an empty DB so the existence check passes
        with SqliteRunStore(str(log_dir / "dispatch.db")):
            pass

        result = runner.invoke(app, ["status", "--analytics"])
        assert result.exit_code == 0

    @patch("kew.cli._preflight")
    @patch("kew.cli.load_config")
    def test_analytics_missing_db_exits_nonzero(
        self, mock_config: MagicMock, mock_preflight: MagicMock, tmp_path: Path
    ) -> None:
        from kew.config import DispatchConfig

        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        mock_config.return_value = config

        (tmp_path / "logs").mkdir(parents=True)
        # No dispatch.db created — should fail fast

        result = runner.invoke(app, ["status", "--analytics"])
        assert result.exit_code == 2

    @patch("kew.cli._preflight")
    @patch("kew.cli.load_config")
    def test_analytics_rejects_with_cost(
        self, mock_config: MagicMock, mock_preflight: MagicMock, tmp_path: Path
    ) -> None:
        from kew.config import DispatchConfig
        from kew.run_store import SqliteRunStore

        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        mock_config.return_value = config

        log_dir = tmp_path / "logs"
        log_dir.mkdir(parents=True)
        with SqliteRunStore(str(log_dir / "dispatch.db")):
            pass

        result = runner.invoke(app, ["status", "--analytics", "--cost"])
        assert result.exit_code == 2
        mock_preflight.assert_not_called()


class TestResolveCommand:
    """kew resolve CLI command."""

    @patch("kew.cli._preflight")
    @patch("kew.cli.load_config")
    @patch("kew.resolve.dispatch_resolve")
    def test_resolve_command_calls_dispatch(
        self, mock_dispatch, mock_config, mock_preflight, tmp_path
    ):
        (tmp_path / ".git").mkdir(exist_ok=True)
        mock_config.return_value = DispatchConfig()
        mock_dispatch.return_value = SimpleNamespace(kind="executed", exit_code=0)

        result = runner.invoke(app, ["resolve", "42"])
        assert result.exit_code == 0
        mock_dispatch.assert_called_once()
        assert mock_dispatch.call_args.kwargs["pr_number"] == 42

    @patch("kew.cli._preflight")
    @patch("kew.cli.load_config")
    @patch("kew.resolve.dispatch_resolve")
    def test_resolve_dry_run(
        self, mock_dispatch, mock_config, mock_preflight, tmp_path
    ):
        (tmp_path / ".git").mkdir(exist_ok=True)
        mock_config.return_value = DispatchConfig()
        mock_dispatch.return_value = SimpleNamespace(kind="executed", exit_code=0)

        result = runner.invoke(app, ["resolve", "42", "--dry-run"])
        assert result.exit_code == 0
        assert mock_dispatch.call_args.kwargs["dry_run"] is True

    @patch("kew.cli._preflight")
    @patch("kew.cli.load_config")
    @patch("kew.resolve.dispatch_resolve")
    def test_resolve_nonzero_exit(
        self, mock_dispatch, mock_config, mock_preflight, tmp_path
    ):
        (tmp_path / ".git").mkdir(exist_ok=True)
        mock_config.return_value = DispatchConfig()
        mock_dispatch.return_value = SimpleNamespace(kind="executed", exit_code=1)

        result = runner.invoke(app, ["resolve", "42"])
        assert result.exit_code == 1
        assert result.exception.code == 1

    @patch("kew.cli._preflight")
    @patch("kew.cli.load_config")
    @patch("kew.resolve.dispatch_resolve")
    def test_resolve_force_budget(
        self, mock_dispatch, mock_config, mock_preflight, tmp_path
    ):
        (tmp_path / ".git").mkdir(exist_ok=True)
        mock_config.return_value = DispatchConfig()
        mock_dispatch.return_value = SimpleNamespace(kind="executed", exit_code=0)

        result = runner.invoke(app, ["resolve", "42", "--force-budget"])
        assert result.exit_code == 0
        assert mock_dispatch.call_args.kwargs["force_budget"] is True

    def test_resolve_help_shows_usage(self):
        result = runner.invoke(app, ["resolve", "--help"])
        assert result.exit_code == 0
        assert "PR number" in result.output or "PR_NUMBER" in result.output


class TestExportCommand:
    """Tests for the `kew export` command."""

    @patch("kew.cli.load_config")
    def test_export_json_stdout(self, mock_config: MagicMock, tmp_path: Path):
        (tmp_path / ".git").mkdir(exist_ok=True)
        import json
        from datetime import UTC, datetime

        from kew.run_store import RunRecord, SqliteRunStore

        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        mock_config.return_value = config

        store_path = Path(config.logging.dir) / "dispatch.db"
        store_path.parent.mkdir(parents=True, exist_ok=True)
        with SqliteRunStore(str(store_path)) as store:
            store.save(
                RunRecord(
                    run_id="exp-1",
                    issue_num=10,
                    branch="agent/issue-10",
                    status="success",
                    exit_code=0,
                    started_at=datetime(2026, 3, 15, 10, 0, 0, tzinfo=UTC),
                    estimated_cost_usd=1.50,
                )
            )

        result = runner.invoke(app, ["export", "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data) == 1
        assert data[0]["run_id"] == "exp-1"
        assert data[0]["estimated_cost_usd"] == 1.50

    @patch("kew.cli.load_config")
    def test_export_csv_to_file(self, mock_config: MagicMock, tmp_path: Path):
        (tmp_path / ".git").mkdir(exist_ok=True)
        from datetime import UTC, datetime

        from kew.run_store import RunRecord, SqliteRunStore

        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        mock_config.return_value = config

        store_path = Path(config.logging.dir) / "dispatch.db"
        store_path.parent.mkdir(parents=True, exist_ok=True)
        with SqliteRunStore(str(store_path)) as store:
            store.save(
                RunRecord(
                    run_id="exp-csv-1",
                    issue_num=20,
                    branch="agent/issue-20",
                    status="failure",
                    exit_code=1,
                    started_at=datetime(2026, 3, 15, 10, 0, 0, tzinfo=UTC),
                )
            )

        out_file = tmp_path / "export.csv"
        result = runner.invoke(
            app, ["export", "--format", "csv", "--output", str(out_file)]
        )
        assert result.exit_code == 0
        content = out_file.read_text()
        assert "exp-csv-1" in content
        assert "run_id" in content  # header

    @patch("kew.cli.load_config")
    def test_export_no_db_exits_with_error(
        self, mock_config: MagicMock, tmp_path: Path
    ):
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "nonexistent")
        mock_config.return_value = config

        result = runner.invoke(app, ["export"])
        assert result.exit_code == 2
        assert "No dispatch database" in result.output

    @patch("kew.cli.load_config")
    def test_export_invalid_format(self, mock_config: MagicMock, tmp_path: Path):
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        mock_config.return_value = config

        result = runner.invoke(app, ["export", "--format", "yaml"])
        assert result.exit_code == 2
        assert "json" in result.output or "csv" in result.output

    @patch("kew.cli.project_root")
    @patch("kew.cli.load_config")
    def test_export_model_inventory_json(
        self, mock_config: MagicMock, mock_project_root: MagicMock, tmp_path: Path
    ):
        (tmp_path / ".git").mkdir(exist_ok=True)
        import json
        from datetime import UTC, datetime

        from kew.run_store import RunRecord

        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        mock_config.return_value = config
        mock_project_root.return_value = tmp_path
        store_path = Path(config.logging.dir) / "dispatch.db"
        store_path.parent.mkdir(parents=True, exist_ok=True)
        from kew.ledger_identity import open_managed_store

        with open_managed_store(
            project_root=tmp_path,
            db_path=store_path,
            anchors_path=tmp_path / "audit-anchors.jsonl",
        ) as store:
            store.save(
                RunRecord(
                    run_id="inventory-1",
                    issue_num=10,
                    branch="agent/issue-10",
                    status="success",
                    exit_code=0,
                    started_at=datetime(2026, 8, 1, tzinfo=UTC),
                    model="route/sonnet",
                    reported_model="claude-sonnet-4-6",
                )
            )

        result = runner.invoke(app, ["export", "--format", "model-inventory"])

        assert result.exit_code == 0, result.output
        inventory = json.loads(result.output)
        assert inventory["format"] == "model-eol"
        assert inventory["models"][0]["id"] == "claude-sonnet-4-6"
        assert inventory["models"][0]["source"] == "reported"

    @patch("kew.cli.load_config")
    def test_export_with_since_filter(self, mock_config: MagicMock, tmp_path: Path):
        (tmp_path / ".git").mkdir(exist_ok=True)
        import json
        from datetime import UTC, datetime

        from kew.run_store import RunRecord, SqliteRunStore

        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        mock_config.return_value = config

        store_path = Path(config.logging.dir) / "dispatch.db"
        store_path.parent.mkdir(parents=True, exist_ok=True)
        with SqliteRunStore(str(store_path)) as store:
            store.save(
                RunRecord(
                    run_id="old-1",
                    issue_num=1,
                    branch="agent/issue-1",
                    status="success",
                    exit_code=0,
                    started_at=datetime(2025, 1, 1, tzinfo=UTC),
                )
            )
            store.save(
                RunRecord(
                    run_id="new-1",
                    issue_num=2,
                    branch="agent/issue-2",
                    status="success",
                    exit_code=0,
                    started_at=datetime(2026, 3, 1, tzinfo=UTC),
                )
            )

        result = runner.invoke(app, ["export", "--since", "2026-01-01"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data) == 1
        assert data[0]["run_id"] == "new-1"

    @patch("kew.cli.load_config")
    def test_export_purge_without_retention_warns(
        self, mock_config: MagicMock, tmp_path: Path
    ):
        (tmp_path / ".git").mkdir(exist_ok=True)
        from datetime import UTC, datetime

        from kew.run_store import RunRecord, SqliteRunStore

        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        config.logging.retention_days = 0
        mock_config.return_value = config

        store_path = Path(config.logging.dir) / "dispatch.db"
        store_path.parent.mkdir(parents=True, exist_ok=True)
        with SqliteRunStore(str(store_path)) as store:
            store.save(
                RunRecord(
                    run_id="r1",
                    issue_num=1,
                    branch="b",
                    status="success",
                    exit_code=0,
                    started_at=datetime.now(UTC),
                )
            )

        result = runner.invoke(app, ["export", "--purge"])
        assert result.exit_code == 0
        assert "Cannot purge" in result.output

    @patch("kew.cli.load_config")
    def test_export_purge_with_retention(self, mock_config: MagicMock, tmp_path: Path):
        (tmp_path / ".git").mkdir(exist_ok=True)
        from datetime import UTC, datetime, timedelta

        from kew.run_store import RunRecord, SqliteRunStore

        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        config.logging.retention_days = 30
        mock_config.return_value = config

        store_path = Path(config.logging.dir) / "dispatch.db"
        store_path.parent.mkdir(parents=True, exist_ok=True)
        now = datetime.now(UTC)
        with SqliteRunStore(str(store_path)) as store:
            store.save(
                RunRecord(
                    run_id="old-purge",
                    issue_num=1,
                    branch="b",
                    status="success",
                    exit_code=0,
                    started_at=now - timedelta(days=60),
                )
            )
            store.save(
                RunRecord(
                    run_id="new-keep",
                    issue_num=2,
                    branch="b2",
                    status="success",
                    exit_code=0,
                    started_at=now,
                )
            )

        result = runner.invoke(app, ["export", "--purge"])
        assert result.exit_code == 0
        assert "Purged 1 record" in result.output

        # Verify old record is gone, new one remains
        with SqliteRunStore(str(store_path)) as store:
            assert store.get("old-purge") is None
            assert store.get("new-keep") is not None


class TestStopCommand:
    @patch("kew.cli._preflight")
    @patch("kew.cli.load_config")
    def test_stop_kills_and_latches(
        self, mock_config: MagicMock, mock_preflight: MagicMock, tmp_path: Path
    ):
        (tmp_path / ".git").mkdir(exist_ok=True)
        from kew.fleet import FleetStopReport, KilledRun

        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        mock_config.return_value = config

        report = FleetStopReport(
            reason="runaway",
            stopped_at="2026-06-09T10:00:00+00:00",
            already_stopped=False,
            killed=[KilledRun(run_id="abcd1234efgh", issue_num=42, cost_usd=1.5)],
        )
        with patch("kew.fleet.stop_fleet", return_value=report) as mock_stop:
            result = runner.invoke(app, ["stop", "--reason", "runaway"])

        assert result.exit_code == 0
        assert mock_stop.call_args.kwargs["reason"] == "runaway"
        assert "#42" in result.output
        assert "$1.50" in result.output
        assert "1 run(s) killed" in result.output
        assert "kew resume" in result.output

    @patch("kew.cli._preflight")
    @patch("kew.cli.load_config")
    def test_stop_with_nothing_in_flight(
        self, mock_config: MagicMock, mock_preflight: MagicMock, tmp_path: Path
    ):
        (tmp_path / ".git").mkdir(exist_ok=True)
        from kew.fleet import FleetStopReport

        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        mock_config.return_value = config

        report = FleetStopReport(
            reason="", stopped_at="t", already_stopped=False, killed=[]
        )
        with patch("kew.fleet.stop_fleet", return_value=report):
            result = runner.invoke(app, ["stop"])

        assert result.exit_code == 0
        assert "0 run(s) killed" in result.output

    @patch("kew.cli._preflight")
    @patch("kew.cli.load_config")
    def test_stop_twice_reports_already_stopped(
        self, mock_config: MagicMock, mock_preflight: MagicMock, tmp_path: Path
    ):
        (tmp_path / ".git").mkdir(exist_ok=True)
        from kew.fleet import FleetStopReport

        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        mock_config.return_value = config

        report = FleetStopReport(
            reason="first",
            stopped_at="2026-06-09T10:00:00+00:00",
            already_stopped=True,
            killed=[],
        )
        with patch("kew.fleet.stop_fleet", return_value=report):
            result = runner.invoke(app, ["stop", "--reason", "second"])

        assert result.exit_code == 0
        assert "already stopped" in result.output


class TestResumeCommand:
    @patch("kew.cli.load_config")
    def test_resume_clears_latch(self, mock_config: MagicMock, tmp_path: Path):
        (tmp_path / ".git").mkdir(exist_ok=True)
        from kew.run_store import SqliteRunStore

        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        mock_config.return_value = config
        (tmp_path / "logs").mkdir(parents=True)
        with SqliteRunStore(str(tmp_path / "logs" / "dispatch.db")) as store:
            store.set_fleet_stopped("halt")

        result = runner.invoke(app, ["resume"])

        assert result.exit_code == 0
        assert "resumed" in result.output.lower()
        with SqliteRunStore(str(tmp_path / "logs" / "dispatch.db")) as store:
            assert store.get_fleet_stopped() is None

    @patch("kew.cli.load_config")
    def test_resume_when_not_stopped(self, mock_config: MagicMock, tmp_path: Path):
        (tmp_path / ".git").mkdir(exist_ok=True)
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        mock_config.return_value = config

        result = runner.invoke(app, ["resume"])

        assert result.exit_code == 0
        assert "not stopped" in result.output.lower()


class TestFleetStoppedRefusal:
    """Dispatch-triggering commands refuse while the fleet is latched."""

    def _latched_config(self, tmp_path: Path) -> DispatchConfig:
        from kew.run_store import SqliteRunStore

        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        (tmp_path / "logs").mkdir(parents=True)
        with SqliteRunStore(str(tmp_path / "logs" / "dispatch.db")) as store:
            store.set_fleet_stopped("incident response")
        return config

    @patch("kew.cli._preflight")
    @patch("kew.cli.load_config")
    def test_run_refuses_while_latched(
        self, mock_config: MagicMock, mock_preflight: MagicMock, tmp_path: Path
    ):
        (tmp_path / ".git").mkdir(exist_ok=True)
        mock_config.return_value = self._latched_config(tmp_path)
        with patch("kew.cli.select_next_issue") as mock_select:
            result = runner.invoke(app, ["run"])
        assert result.exit_code == 2
        assert "FLEET STOPPED" in result.output
        assert "incident response" in result.output
        mock_select.assert_not_called()

    @patch("kew.cli._preflight")
    @patch("kew.cli.load_config")
    def test_review_refuses_while_latched(
        self, mock_config: MagicMock, mock_preflight: MagicMock, tmp_path: Path
    ):
        (tmp_path / ".git").mkdir(exist_ok=True)
        mock_config.return_value = self._latched_config(tmp_path)
        with patch("kew.cli.dispatch_review") as mock_review:
            result = runner.invoke(app, ["review", "7"])
        assert result.exit_code == 2
        assert "FLEET STOPPED" in result.output
        mock_review.assert_not_called()

    @patch("kew.cli._preflight")
    @patch("kew.cli.load_config")
    def test_resolve_refuses_while_latched(
        self, mock_config: MagicMock, mock_preflight: MagicMock, tmp_path: Path
    ):
        (tmp_path / ".git").mkdir(exist_ok=True)
        mock_config.return_value = self._latched_config(tmp_path)
        with patch("kew.resolve.dispatch_resolve") as mock_resolve:
            result = runner.invoke(app, ["resolve", "7"])
        assert result.exit_code == 2
        assert "FLEET STOPPED" in result.output
        mock_resolve.assert_not_called()

    @patch("kew.cli._preflight")
    @patch("kew.cli.load_config")
    def test_ci_fix_refuses_while_latched(
        self, mock_config: MagicMock, mock_preflight: MagicMock, tmp_path: Path
    ):
        (tmp_path / ".git").mkdir(exist_ok=True)
        mock_config.return_value = self._latched_config(tmp_path)
        with patch("kew.cli.dispatch_ci_fix") as mock_ci_fix:
            result = runner.invoke(app, ["ci-fix", "12345"])
        assert result.exit_code == 2
        assert "FLEET STOPPED" in result.output
        mock_ci_fix.assert_not_called()

    @patch("kew.cli._preflight")
    @patch("kew.cli.select_next_issue")
    @patch("kew.cli.load_config")
    def test_run_proceeds_when_not_latched(
        self,
        mock_config: MagicMock,
        mock_select: MagicMock,
        mock_preflight: MagicMock,
        tmp_path: Path,
    ):
        (tmp_path / ".git").mkdir(exist_ok=True)
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        mock_config.return_value = config
        mock_select.return_value = None  # empty queue — exits cleanly
        result = runner.invoke(app, ["run"])
        assert result.exit_code == 0


class TestStatusFleetBanner:
    @patch("kew.cli.run_with_backoff")
    @patch("kew.cli._preflight")
    @patch("kew.cli.load_config")
    @patch("kew.cli.detect_orphan_prs", new=lambda *_: [])
    def test_banner_shown_while_latched(
        self,
        mock_config: MagicMock,
        mock_preflight: MagicMock,
        mock_run: MagicMock,
        tmp_path: Path,
    ):
        (tmp_path / ".git").mkdir(exist_ok=True)
        from kew.run_store import SqliteRunStore

        config = DispatchConfig()
        config.project.repo = "owner/repo"
        config.logging.dir = str(tmp_path / "logs")
        mock_config.return_value = config
        mock_run.return_value = MagicMock(returncode=0, stdout="0")
        (tmp_path / "logs").mkdir(parents=True)
        with SqliteRunStore(str(tmp_path / "logs" / "dispatch.db")) as store:
            store.set_fleet_stopped("budget runaway")

        result = runner.invoke(app, ["status"])

        assert result.exit_code == 0
        assert "FLEET STOPPED" in result.output
        assert "budget runaway" in result.output

    @patch("kew.cli.run_with_backoff")
    @patch("kew.cli._preflight")
    @patch("kew.cli.load_config")
    @patch("kew.cli.detect_orphan_prs", new=lambda *_: [])
    def test_no_banner_when_not_latched(
        self,
        mock_config: MagicMock,
        mock_preflight: MagicMock,
        mock_run: MagicMock,
        tmp_path: Path,
    ):
        (tmp_path / ".git").mkdir(exist_ok=True)
        config = DispatchConfig()
        config.project.repo = "owner/repo"
        config.logging.dir = str(tmp_path / "logs")
        mock_config.return_value = config
        mock_run.return_value = MagicMock(returncode=0, stdout="0")

        result = runner.invoke(app, ["status"])

        assert result.exit_code == 0
        assert "FLEET STOPPED" not in result.output


class TestApproveFlag:
    """kew dispatch N --approve releases a held issue past the gate."""

    @patch("kew.cli._run_loop_dispatch")
    @patch("kew.cli._preflight")
    @patch("kew.cli.load_config")
    def test_approve_refused_with_loop(
        self, mock_config: MagicMock, mock_preflight: MagicMock, loop: MagicMock
    ):
        mock_config.return_value = DispatchConfig()
        result = runner.invoke(app, ["dispatch", "42", "--loop", "--approve"])
        assert result.exit_code == 2
        assert "--approve cannot be used with --loop" in result.output
        loop.assert_not_called()

    @patch("kew.cli._preflight")
    @patch("kew.cli._dispatch_one")
    @patch("kew.cli.load_config")
    def test_approve_passed_to_dispatch_one(
        self,
        mock_config: MagicMock,
        mock_dispatch: MagicMock,
        mock_preflight: MagicMock,
        tmp_path,
    ):
        (tmp_path / ".git").mkdir(exist_ok=True)
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        mock_config.return_value = config
        mock_dispatch.return_value = DispatchCommandResult("executed")

        result = runner.invoke(app, ["dispatch", "42", "--approve"])
        assert result.exit_code == 0
        assert mock_dispatch.call_args[1]["approve"] is True


class TestApproveReleasePath:
    """_dispatch_one restores 'ready' on a held issue before claiming.

    ``release_approval_hold`` runs for real; the forge boundary is the
    ``gh issue edit`` it shells, so each test reads the label edits off that.
    """

    def _held_issue(self):
        return {
            "number": 42,
            "title": "Held issue",
            "body": "",
            "labels": [{"name": "needs-approval"}, {"name": "medium"}],
        }

    @staticmethod
    def _label_edits(gh: MagicMock) -> list[list[str]]:
        return [
            c.args[0] for c in gh.call_args_list if c.args[0][1:3] == ["issue", "edit"]
        ]

    @patch("kew.labels.run", return_value=subprocess.CompletedProcess([], 0, "", ""))
    @patch("kew.cli.execute_dispatch")
    @patch("kew.cli.try_claim_issue", return_value=ClaimResult.CLAIMED)
    @patch("kew.cli.remote_branch_exists", return_value=False)
    @patch("kew.cli.select_next_issue")
    def test_approve_releases_held_issue_then_claims(
        self,
        mock_select: MagicMock,
        mock_remote: MagicMock,
        mock_claim: MagicMock,
        mock_execute: MagicMock,
        gh: MagicMock,
    ):
        config = DispatchConfig()
        config.project.repo = "owner/repo"
        mock_select.return_value = self._held_issue()
        mock_execute.return_value = DispatchOutcome(exit_code=0)

        rc = _dispatch_one(config, force_issue=42, dry_run=False, approve=True)

        assert rc == DispatchCommandResult("executed", 0)
        assert self._label_edits(gh) == [
            [
                "gh",
                "issue",
                "edit",
                "42",
                "--repo",
                "owner/repo",
                "--add-label",
                config.labels.ready,
                "--remove-label",
                config.approval.label,
            ]
        ]
        mock_claim.assert_called_once_with(42, config)
        kwargs = mock_execute.call_args[1]
        assert kwargs["approved"] is True
        assert kwargs["interactive"] is True

    @patch("kew.labels.run", return_value=subprocess.CompletedProcess([], 0, "", ""))
    @patch("kew.cli.execute_dispatch")
    @patch("kew.cli.try_claim_issue", return_value=ClaimResult.CLAIMED)
    @patch("kew.cli.remote_branch_exists", return_value=False)
    @patch("kew.cli.select_next_issue")
    def test_no_release_without_approve(
        self,
        mock_select: MagicMock,
        mock_remote: MagicMock,
        mock_claim: MagicMock,
        mock_execute: MagicMock,
        gh: MagicMock,
    ):
        config = DispatchConfig()
        mock_select.return_value = self._held_issue()
        mock_execute.return_value = DispatchOutcome(exit_code=0)

        rc = _dispatch_one(config, force_issue=42, dry_run=False)

        assert rc == DispatchCommandResult("executed", 0)
        assert self._label_edits(gh) == []
        assert mock_execute.call_args[1]["approved"] is False

    @patch("kew.labels.run", return_value=subprocess.CompletedProcess([], 0, "", ""))
    @patch("kew.cli.execute_dispatch")
    @patch("kew.cli.try_claim_issue", return_value=ClaimResult.CLAIMED)
    @patch("kew.cli.remote_branch_exists", return_value=False)
    @patch("kew.cli.select_next_issue")
    def test_approve_on_unheld_issue_skips_release(
        self,
        mock_select: MagicMock,
        mock_remote: MagicMock,
        mock_claim: MagicMock,
        mock_execute: MagicMock,
        gh: MagicMock,
    ):
        """--approve on an issue that isn't held: no label churn, still pre-approved."""
        config = DispatchConfig()
        mock_select.return_value = {
            "number": 42,
            "title": "Ready issue",
            "body": "",
            "labels": [{"name": "ready"}],
        }
        mock_execute.return_value = DispatchOutcome(exit_code=0)

        rc = _dispatch_one(config, force_issue=42, dry_run=False, approve=True)

        assert rc == DispatchCommandResult("executed", 0)
        assert self._label_edits(gh) == []
        assert mock_execute.call_args[1]["approved"] is True


class TestStatusHeldCount:
    """kew status shows the count of needs-approval (held) issues."""

    @patch("kew.cli._preflight")
    @patch("kew.cli.run_with_backoff")
    @patch("kew.cli.load_config")
    @patch("kew.cli.detect_orphan_prs", new=lambda *_: [])
    def test_status_shows_needs_approval_count(
        self,
        mock_config: MagicMock,
        mock_run: MagicMock,
        mock_preflight: MagicMock,
        tmp_path,
    ):
        (tmp_path / ".git").mkdir(exist_ok=True)
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        mock_config.return_value = config
        mock_run.return_value = MagicMock(returncode=0, stdout="3")

        result = runner.invoke(app, ["status"])

        assert result.exit_code == 0
        assert "needs-approval" in result.output
        # The held label was actually queried via gh issue list --label
        queried_labels = []
        for call in mock_run.call_args_list:
            cmd = call.args[0]
            if "--label" in cmd:
                queried_labels.append(cmd[cmd.index("--label") + 1])
        assert "needs-approval" in queried_labels


class TestVersionOption:
    """`kew --version` — eager option, importlib.metadata with fallback."""

    def test_version_exits_zero(self):
        result = runner.invoke(app, ["--version"])
        assert result.exit_code == 0

    def test_version_prints_installed_distribution_version(self):
        import importlib.metadata

        expected = importlib.metadata.version("kew-cli")
        result = runner.invoke(app, ["--version"])
        assert result.output.strip() == f"kew {expected}"

    def test_version_falls_back_to_source_checkout(self, monkeypatch):
        import importlib.metadata

        import kew
        import kew.cli as cli_module

        def raise_not_found(name: str) -> str:
            raise importlib.metadata.PackageNotFoundError(name)

        monkeypatch.setattr(cli_module.importlib.metadata, "version", raise_not_found)
        result = runner.invoke(app, ["--version"])
        assert result.exit_code == 0
        assert result.output.strip() == f"kew {kew.__version__}"

    def test_version_is_eager_and_short_circuits_commands(self):
        # Must exit before `run` executes (no mocks installed — `run`
        # would fail or produce dispatch output if it actually ran).
        result = runner.invoke(app, ["--version", "run"])
        assert result.exit_code == 0
        assert result.output.startswith("kew ")


class TestPreflightAuditContext:
    """_preflight seeds the process-wide audit repo default."""

    @patch("kew.cli.run")
    @patch("kew.cli.preflight_tools")
    def test_preflight_sets_audit_context(
        self, mock_tools, mock_run, tmp_path, monkeypatch
    ):
        import kew.audit as audit
        from kew.cli import _preflight
        from kew.config import DispatchConfig

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(audit, "_audit_repo", None)
        monkeypatch.setattr(audit, "_audit_policy_hash", None)
        monkeypatch.setattr(audit, "_audit_policy_snapshot", None)
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        config = DispatchConfig()
        config.project.repo = "octo/widgets"
        config.logging.dir = str(tmp_path / "logs")
        config.branches.worktree_base = str(tmp_path / "wt")

        _preflight(config)

        assert audit._audit_repo == "octo/widgets"


def test_preflight_runner_raises_when_binary_missing(monkeypatch):
    """The dispatch preflight still hard-fails when the runner CLI is absent."""
    import typer

    from kew.cli import _preflight_runner
    from kew.config import DispatchConfig
    from kew.subprocess_utils import RunnerReachability

    monkeypatch.setattr(
        "kew.runner_preflight.inspect_runner",
        lambda config: RunnerReachability(
            runner_name="claude-code",
            binary="claude",
            binary_present=False,
            credential_detected=True,
        ),
    )
    with pytest.raises(typer.Exit):
        _preflight_runner(DispatchConfig())


def test_health_command_exit_0_when_healthy(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir(exist_ok=True)
    import json as _json

    from kew.run_store import SqliteRunStore
    from kew.subprocess_utils import RunnerReachability

    (tmp_path / "logs").mkdir()
    (tmp_path / "kew.toml").write_text(
        '[project]\nrepo = "o/r"\n[logging]\ndir = "./logs"\n'
    )
    with SqliteRunStore(str(tmp_path / "logs" / "dispatch.db")):
        pass
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "kew.health.inspect_runners",
        lambda c: [RunnerReachability("claude-code", "claude", True, True)],
    )
    result = runner.invoke(app, ["health", "--json"])
    assert result.exit_code == 0
    payload = _json.loads(result.stdout)
    assert payload["healthy"] is True
    assert {c["name"] for c in payload["checks"]} >= {"meter_running", "audit_parses"}


def test_switchyard_health_json_exposes_sanitized_proxy_observation(
    tmp_path, monkeypatch
):
    (tmp_path / ".git").mkdir(exist_ok=True)
    from kew.run_store import SqliteRunStore
    from kew.subprocess_utils import RunnerReachability
    from kew.switchyard import SwitchyardProbeResult
    from kew.switchyard_metrics import parse_switchyard_metrics

    (tmp_path / "logs").mkdir()
    (tmp_path / "kew.toml").write_text(
        '[project]\nrepo = "o/r"\n'
        '[logging]\ndir = "./logs"\n'
        '[inference_transport]\nmode = "switchyard"\n'
        "[inference_transport.switchyard]\n"
        'endpoint = "https://switchyard.example"\n'
        'route = "switchyard/passthrough"\n'
        'expected_version = "0.1.0"\n'
    )
    with SqliteRunStore(str(tmp_path / "logs" / "dispatch.db")):
        pass
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "kew.health.inspect_runners",
        lambda c: [RunnerReachability("claude-code", "claude", True, True)],
    )
    monkeypatch.setattr(
        "kew.cli.probe_switchyard_endpoint",
        lambda **kwargs: SwitchyardProbeResult(
            admitted=True,
            reason="ready",
            endpoint="https://switchyard.example",
            route="switchyard/passthrough",
            observed_version="0.1.0",
        ),
    )
    monkeypatch.setattr(
        "kew.cli.observe_switchyard_metrics",
        lambda **kwargs: parse_switchyard_metrics(
            "switchyard_requests_total"
            '{route="switchyard/passthrough",api_key="secret"} 2\n'
        ),
    )

    result = runner.invoke(app, ["health", "--json"])

    assert result.exit_code == 0
    payload = _json.loads(result.stdout)
    assert set(payload) == {"healthy", "exit_code", "window_days", "checks"}
    _assert_switchyard_proxy_observation(payload)
    assert "secret" not in result.stdout


def _assert_switchyard_proxy_observation(payload):
    check = next(item for item in payload["checks"] if item["name"] == "switchyard")
    assert set(check) == {"name", "status", "summary", "detail"}
    assert check["status"] == "pass"
    _assert_proxy_observation(check)


def _assert_proxy_observation(check):
    observation = check["detail"]["proxy_observation"]
    assert observation["scope"] == "process_wide_proxy"
    assert observation["accounting_authority"] == "runner_reported_usage"
    assert observation["per_run_attribution"] is False


def test_switchyard_health_json_sanitizes_diagnostic_failures(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir(exist_ok=True)
    from kew.run_store import SqliteRunStore
    from kew.subprocess_utils import RunnerReachability
    from kew.switchyard import SwitchyardProbeResult
    from kew.switchyard_metrics import SwitchyardMetricsError

    (tmp_path / "logs").mkdir()
    (tmp_path / "kew.toml").write_text(
        '[project]\nrepo = "o/r"\n'
        '[logging]\ndir = "./logs"\n'
        '[inference_transport]\nmode = "switchyard"\n'
        "[inference_transport.switchyard]\n"
        'endpoint = "https://switchyard.example"\n'
        'route = "switchyard/passthrough"\n'
        'expected_version = "0.1.0"\n'
    )
    with SqliteRunStore(str(tmp_path / "logs" / "dispatch.db")):
        pass
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "kew.health.inspect_runners",
        lambda c: [RunnerReachability("claude-code", "claude", True, True)],
    )
    monkeypatch.setattr(
        "kew.cli.probe_switchyard_endpoint",
        lambda **kwargs: SwitchyardProbeResult(
            admitted=False,
            reason="unreachable",
            endpoint="https://switchyard.example",
            route="switchyard/passthrough",
        ),
    )

    def unavailable(**kwargs):
        raise SwitchyardMetricsError("unreachable")

    monkeypatch.setattr("kew.cli.observe_switchyard_metrics", unavailable)

    result = runner.invoke(app, ["health", "--json"])

    assert result.exit_code == 1
    payload = _json.loads(result.stdout)
    check = next(item for item in payload["checks"] if item["name"] == "switchyard")
    assert check["summary"] == "refused: unreachable"
    assert check["detail"]["proxy_observation"]["unavailable_reason"] == "unreachable"
    assert "Traceback" not in result.stdout


def test_switchyard_diagnostics_collect_probe_and_metrics_concurrently(monkeypatch):
    from threading import Barrier

    from kew.cli import _switchyard_diagnostics
    from kew.config import InferenceTransportConfig, SwitchyardConfig
    from kew.switchyard import SwitchyardProbeResult
    from kew.switchyard_metrics import parse_switchyard_metrics

    rendezvous = Barrier(2)

    def probe(**kwargs):
        rendezvous.wait(timeout=2.0)
        return SwitchyardProbeResult(
            admitted=True,
            reason="ready",
            endpoint="https://switchyard.example",
            route="switchyard/passthrough",
        )

    def observe(**kwargs):
        rendezvous.wait(timeout=2.0)
        return parse_switchyard_metrics("switchyard_requests_total 1\n")

    monkeypatch.setattr("kew.cli.probe_switchyard_endpoint", probe)
    monkeypatch.setattr("kew.cli.observe_switchyard_metrics", observe)
    config = DispatchConfig(
        inference_transport=InferenceTransportConfig(
            mode="switchyard",
            switchyard=SwitchyardConfig(
                endpoint="https://switchyard.example",
                route="switchyard/passthrough",
                expected_version="0.1.0",
            ),
        )
    )

    probe_result, observation = _switchyard_diagnostics(config)

    assert probe_result is not None and probe_result.admitted is True
    assert observation is not None and observation.samples[0].value == 1


def test_health_command_uses_db_budget_overrides(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir(exist_ok=True)
    from datetime import UTC, datetime

    from kew.run_store import RunRecord, SqliteRunStore
    from kew.subprocess_utils import RunnerReachability

    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (tmp_path / "kew.toml").write_text(
        '[project]\nrepo = "o/r"\n'
        "[budget]\ndaily_usd = 0.01\nweekly_usd = 0.01\n"
        '[logging]\ndir = "./logs"\n'
    )
    with SqliteRunStore(str(log_dir / "dispatch.db")) as store:
        store.save(
            RunRecord(
                run_id="health-override",
                issue_num=1,
                branch="b",
                status="success",
                exit_code=0,
                started_at=datetime.now(UTC),
                model="claude-sonnet-4-6",
                estimated_cost_usd=5.0,
                total_tokens=1000,
            )
        )
        for field in ("budget.daily_usd", "budget.weekly_usd"):
            store.set_config_override(
                field=field,
                value_json="10.0",
                file_value_json="0.01",
                set_at=datetime.now(UTC).isoformat(),
                set_by="test",
            )

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "kew.health.inspect_runners",
        lambda c: [RunnerReachability("claude-code", "claude", True, True)],
    )
    result = runner.invoke(app, ["health", "--json"])

    assert result.exit_code == 0
    payload = _json.loads(result.stdout)
    spend = next(c for c in payload["checks"] if c["name"] == "spend_within_limits")
    assert spend["status"] == "pass"
    assert spend["detail"]["daily_limit"] == 10.0
    assert spend["detail"]["weekly_limit"] == 10.0


def test_health_command_exit_2_when_db_missing(tmp_path, monkeypatch):
    # logging.dir points at a dir with no dispatch.db — the gate must alarm,
    # not silently auto-create an empty db and report green.
    (tmp_path / "kew.toml").write_text(
        '[project]\nrepo = "o/r"\n[logging]\ndir = "./no-such-dir"\n'
    )
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["health"])
    assert result.exit_code == 2


def test_health_command_exit_2_when_window_invalid(tmp_path, monkeypatch):
    # --window < 1 has no well-defined retrospective semantics — fail fast.
    (tmp_path / "kew.toml").write_text(
        '[project]\nrepo = "o/r"\n[logging]\ndir = "./logs"\n'
    )
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["health", "--window", "0"])
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# kew canary tests
# ---------------------------------------------------------------------------


class _FakeProc:
    """Minimal popen stand-in: communicate() returns canned stdout + an exit code."""

    def __init__(self, lines: list[str], code: int = 0):
        self._stdout = "".join(lines)
        self.returncode = code
        self.pid = 1234

    def communicate(self, timeout: float | None = None) -> tuple[str, None]:
        return self._stdout, None


_CLAUDE_OK = [
    '{"type":"system","subtype":"init","model":"claude-sonnet-4-6"}\n',
    '{"type":"assistant","message":{"content":[{"type":"text","text":"OK"}],'
    '"usage":{"input_tokens":12,"output_tokens":3,"cache_creation_input_tokens":0,'
    '"cache_read_input_tokens":0}}}\n',
    '{"type":"result","subtype":"success","session_id":"sess_ok","stop_reason":"end_turn"}\n',
]
_CLAUDE_DRIFT = [  # usage present but output_tokens dropped to 0 (vendor broke a key)
    '{"type":"system","subtype":"init","model":"claude-sonnet-4-6"}\n',
    '{"type":"assistant","message":{"content":[{"type":"text","text":"OK"}],'
    '"usage":{"input_tokens":12,"output_tokens":0,"cache_creation_input_tokens":0,'
    '"cache_read_input_tokens":0}}}\n',
    '{"type":"result","subtype":"success","session_id":"sess_ok","stop_reason":"end_turn"}\n',
]


def test_canary_exit_0_when_healthy(monkeypatch, tmp_path):
    (tmp_path / "kew.toml").write_text('[project]\nrepo = "o/r"\n')
    monkeypatch.chdir(tmp_path)
    with (
        patch("kew.cli.preflight_tools"),
        patch("kew.cli.popen", return_value=_FakeProc(_CLAUDE_OK)),
    ):
        result = runner.invoke(app, ["canary", "--runner", "claude-code", "--json"])
    assert result.exit_code == 0
    payload = _json.loads(result.stdout)
    assert payload["exit_code"] == 0
    assert payload["runners"][0]["runner"] == "claude-code"
    assert payload["runners"][0]["status"] == "pass"


def test_canary_exit_1_on_drift(monkeypatch, tmp_path):
    (tmp_path / "kew.toml").write_text('[project]\nrepo = "o/r"\n')
    monkeypatch.chdir(tmp_path)
    with (
        patch("kew.cli.preflight_tools"),
        patch("kew.cli.popen", return_value=_FakeProc(_CLAUDE_DRIFT)),
    ):
        result = runner.invoke(app, ["canary", "--runner", "claude-code", "--json"])
    assert result.exit_code == 1
    payload = _json.loads(result.stdout)
    assert payload["runners"][0]["status"] == "fail"


def test_canary_exit_2_when_binary_absent(monkeypatch, tmp_path):
    (tmp_path / "kew.toml").write_text('[project]\nrepo = "o/r"\n')
    monkeypatch.chdir(tmp_path)
    with (
        patch("kew.cli.preflight_tools"),
        patch("time.sleep"),
        patch("kew.cli.popen", side_effect=ValueError("Unknown executable 'claude'")),
    ):
        result = runner.invoke(app, ["canary", "--runner", "claude-code", "--json"])
    assert result.exit_code == 2
    payload = _json.loads(result.stdout)
    assert payload["runners"][0]["status"] == "skip"


def test_canary_exit_2_on_timeout(monkeypatch, tmp_path):
    """A hung runner is killed on the deadline → couldn't-run skip, not a hang."""
    import subprocess

    (tmp_path / "kew.toml").write_text('[project]\nrepo = "o/r"\n')
    monkeypatch.chdir(tmp_path)

    class _HangProc:
        pid = 4321
        returncode = -9

        def communicate(self, timeout: float | None = None) -> tuple[str, None]:
            raise subprocess.TimeoutExpired(cmd="claude", timeout=timeout)

        def wait(self) -> int:
            return -9

    with (
        patch("kew.cli.preflight_tools"),
        patch("time.sleep"),
        patch("os.getpgid", return_value=4321),
        patch("os.killpg"),
        patch("kew.cli.popen", return_value=_HangProc()),
    ):
        result = runner.invoke(app, ["canary", "--runner", "claude-code", "--json"])
    assert result.exit_code == 2
    payload = _json.loads(result.stdout)
    assert payload["runners"][0]["status"] == "skip"
    assert "timed out" in payload["runners"][0]["reason"]


def test_canary_codex_model_override_is_single_runner_only():
    result = runner.invoke(
        app,
        [
            "canary",
            "--runner",
            "claude-code",
            "--runner",
            "codex",
            "--model",
            "gpt-5.6-sol",
        ],
    )
    assert result.exit_code == 2
    assert "exactly one runner" in result.output


def test_canary_exercise_resume_requires_one_resumable_runner():
    result = runner.invoke(
        app,
        ["canary", "--runner", "claude-code", "--exercise-resume"],
    )
    assert result.exit_code == 2
    assert "resume" in result.output.lower()


def test_canary_codex_unhealthy_never_launches(monkeypatch, tmp_path):
    from kew.subprocess_utils import RunnerReachability

    (tmp_path / "kew.toml").write_text('[project]\nrepo = "o/r"\n')
    monkeypatch.chdir(tmp_path)
    with (
        patch("kew.cli.preflight_tools"),
        patch(
            "kew.cli.inspect_runner",
            return_value=RunnerReachability(
                "codex",
                "codex",
                True,
                False,
                version="0.145.0",
                version_supported=True,
            ),
        ),
        patch("kew.cli.popen") as launch,
    ):
        result = runner.invoke(
            app,
            [
                "canary",
                "--runner",
                "codex",
                "--model",
                "gpt-5.6-sol",
                "--json",
            ],
        )
    assert result.exit_code == 2
    assert "codex login unavailable" in result.output
    launch.assert_not_called()


def test_canary_model_opencode_from_routing():
    """opencode canary leg uses model from routing.simple when configured."""
    from kew.cli import _canary_model
    from kew.config import RouteEntry, RoutingConfig

    config = DispatchConfig(
        routing=RoutingConfig(
            default="claude-code",
            simple=RouteEntry(
                runner="opencode", model="openrouter/deepseek/deepseek-v4-flash"
            ),
        ),
        agent={"model": "claude-opus-5"},
    )
    assert _canary_model("opencode", config) == "openrouter/deepseek/deepseek-v4-flash"


def test_canary_model_opencode_fallback_no_routing():
    """opencode canary leg falls back to default when routing is absent."""
    from kew.cli import _CANARY_DEFAULT_MODELS, _canary_model

    config = DispatchConfig(agent={"model": "claude-opus-5"})
    assert _canary_model("opencode", config) == _CANARY_DEFAULT_MODELS["opencode"]


def test_canary_model_opencode_fallback_routing_no_model():
    """opencode canary leg falls back to default when routing.simple lacks a model."""
    from kew.cli import _CANARY_DEFAULT_MODELS, _canary_model
    from kew.config import RouteEntry, RoutingConfig

    config = DispatchConfig(
        routing=RoutingConfig(
            default="claude-code",
            simple=RouteEntry(runner="opencode", model=""),
        ),
        agent={"model": "claude-opus-5"},
    )
    assert _canary_model("opencode", config) == _CANARY_DEFAULT_MODELS["opencode"]


def test_canary_model_opencode_fallback_to_default():
    """opencode canary leg uses per-runner default when routing.simple is a string."""
    from kew.cli import _CANARY_DEFAULT_MODELS, _canary_model
    from kew.config import RoutingConfig

    config = DispatchConfig(
        routing=RoutingConfig(default="claude-code", simple="opencode"),
        agent={"model": "claude-opus-5"},
    )
    assert _canary_model("opencode", config) == _CANARY_DEFAULT_MODELS["opencode"]


def test_canary_model_claude_unchanged():
    """claude-code canary leg still uses agent.model or its own default."""
    from kew.cli import _CANARY_DEFAULT_MODELS, _canary_model

    config = DispatchConfig(agent={"model": "claude-opus-5"})
    assert _canary_model("claude-code", config) == "claude-opus-5"

    config_no_model = DispatchConfig(agent={"model": ""})
    assert (
        _canary_model("claude-code", config_no_model)
        == _CANARY_DEFAULT_MODELS["claude-code"]
    )


def _write_switchyard_canary_config(tmp_path, *, route="switchyard/passthrough"):
    (tmp_path / "kew.toml").write_text(
        '[project]\nrepo = "o/r"\n'
        '[inference_transport]\nmode = "switchyard"\n'
        "[inference_transport.switchyard]\n"
        'endpoint = "http://127.0.0.1:4000"\n'
        f'route = "{route}"\n'
        'expected_version = "6ff491acd7eb1ec239525a1baa015730c3d3564a"\n'
    )


def test_switchyard_canary_requires_private_record_directory_before_launch(
    monkeypatch, tmp_path
):
    _write_switchyard_canary_config(tmp_path)
    monkeypatch.chdir(tmp_path)

    with (
        patch("kew.cli.preflight_tools"),
        patch("kew.cli.popen") as launch,
    ):
        result = runner.invoke(app, ["canary", "--runner", "claude-code", "--json"])

    assert result.exit_code == 2
    assert "--record" in result.output
    launch.assert_not_called()


def test_switchyard_canary_dispatches_explicit_passthrough_harness(
    monkeypatch, tmp_path
):
    _write_switchyard_canary_config(tmp_path)
    record_dir = tmp_path / "records"
    record_dir.mkdir(mode=0o700)
    monkeypatch.chdir(tmp_path)
    outcome = RunnerOutcome(
        "claude-code",
        True,
        [CheckResult("proxy_healthy", "pass", "proxy healthy", {})],
    )

    with (
        patch("kew.cli.preflight_tools"),
        patch(
            "kew.canary_live.run_switchyard_passthrough_canary",
            return_value=outcome,
        ) as live,
        patch("kew.cli.popen") as standard_launch,
    ):
        result = runner.invoke(
            app,
            [
                "canary",
                "--runner",
                "claude-code",
                "--record",
                str(record_dir),
                "--json",
            ],
        )

    assert result.exit_code == 0
    live.assert_called_once()
    assert live.call_args.args[0].name == "claude-code"
    assert live.call_args.args[2] == "switchyard/passthrough"
    assert live.call_args.kwargs["record_dir"] == str(record_dir)
    standard_launch.assert_not_called()


def test_switchyard_canary_refuses_opencode_before_launch(monkeypatch, tmp_path):
    _write_switchyard_canary_config(tmp_path)
    record_dir = tmp_path / "records"
    record_dir.mkdir(mode=0o700)
    monkeypatch.chdir(tmp_path)

    with (
        patch("kew.cli.preflight_tools"),
        patch("kew.canary_live.popen") as launch,
    ):
        result = runner.invoke(
            app,
            [
                "canary",
                "--runner",
                "opencode",
                "--record",
                str(record_dir),
                "--json",
            ],
        )

    assert result.exit_code == 2
    payload = _json.loads(result.stdout)
    assert payload["runners"][0]["runner"] == "opencode"
    assert payload["runners"][0]["status"] == "skip"
    assert "no proven Switchyard" in payload["runners"][0]["reason"]
    launch.assert_not_called()


def test_switchyard_canary_rejects_explicit_resume_flag(monkeypatch, tmp_path):
    _write_switchyard_canary_config(tmp_path)
    record_dir = tmp_path / "records"
    record_dir.mkdir(mode=0o700)
    monkeypatch.chdir(tmp_path)

    with (
        patch("kew.cli.preflight_tools"),
        patch("kew.canary_live.popen") as launch,
    ):
        result = runner.invoke(
            app,
            [
                "canary",
                "--runner",
                "codex",
                "--record",
                str(record_dir),
                "--exercise-resume",
            ],
        )

    assert result.exit_code == 2
    assert "always exercises resume" in result.output
    launch.assert_not_called()


def test_switchyard_canary_refuses_non_passthrough_route_before_launch(
    monkeypatch, tmp_path
):
    _write_switchyard_canary_config(tmp_path, route="switchyard/random")
    record_dir = tmp_path / "records"
    record_dir.mkdir(mode=0o700)
    monkeypatch.chdir(tmp_path)

    with (
        patch("kew.cli.preflight_tools"),
        patch("kew.cli.popen") as launch,
    ):
        result = runner.invoke(
            app,
            [
                "canary",
                "--runner",
                "claude-code",
                "--record",
                str(record_dir),
                "--json",
            ],
        )

    assert result.exit_code == 2
    assert "passthrough" in result.output.lower()
    launch.assert_not_called()


# ---------------------------------------------------------------------------
# kew approve / kew deny / kew report --notify  (Task 4 — #236)
# ---------------------------------------------------------------------------


def _approval_repo(tmp_path):
    # a real dispatch.db (with schema) so the missing-store guard passes
    (tmp_path / "kew.toml").write_text(
        '[project]\nrepo = "o/r"\n[logging]\ndir = "./logs"\n'
    )
    (tmp_path / "logs").mkdir()
    from kew.run_store import SqliteRunStore

    with SqliteRunStore(str(tmp_path / "logs" / "dispatch.db")):
        pass


@pytest.mark.parametrize("command", ["approve", "deny"])
def test_remote_decision_query_failure_exits_two(command, monkeypatch, tmp_path):
    _approval_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    with (
        patch("kew.cli.preflight_tools"),
        patch("kew.approval.apply_approval_decision", return_value="gh_error"),
    ):
        result = runner.invoke(app, [command, "42"])
    assert result.exit_code == 2, result.output
    assert "couldn't reach GitHub" in result.output


def test_approve_command_released(monkeypatch, tmp_path):
    (tmp_path / ".git").mkdir(exist_ok=True)
    _approval_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    with (
        patch("kew.cli.preflight_tools"),
        patch("kew.approval.remote_approve", return_value="released") as ra,
    ):
        result = runner.invoke(app, ["approve", "42", "--reason", "ok"])
    assert result.exit_code == 0
    assert ra.call_args.kwargs["issue_num"] == 42


def test_approve_command_not_held_exits_0(monkeypatch, tmp_path):
    (tmp_path / ".git").mkdir(exist_ok=True)
    _approval_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    with (
        patch("kew.cli.preflight_tools"),
        patch("kew.approval.remote_approve", return_value="not_held"),
    ):
        result = runner.invoke(app, ["approve", "42"])
    assert result.exit_code == 0  # idempotent no-op


def test_deny_command_denied(monkeypatch, tmp_path):
    (tmp_path / ".git").mkdir(exist_ok=True)
    _approval_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    with (
        patch("kew.cli.preflight_tools"),
        patch("kew.approval.remote_deny", return_value="denied") as rd,
    ):
        result = runner.invoke(app, ["deny", "42", "--reason", "too costly"])
    assert result.exit_code == 0
    assert rd.call_args.kwargs["issue_num"] == 42
    assert rd.call_args.kwargs["reason"] == "too costly"


def test_approve_command_unauthorized_exits_2(monkeypatch, tmp_path):
    (tmp_path / ".git").mkdir(exist_ok=True)
    _approval_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    with (
        patch("kew.cli.preflight_tools"),
        patch("kew.approval.remote_approve", return_value="unauthorized"),
    ):
        result = runner.invoke(app, ["approve", "42"])
    assert result.exit_code == 2


def test_approve_command_missing_store_exits_2(monkeypatch, tmp_path):
    (tmp_path / "kew.toml").write_text(
        '[project]\nrepo = "o/r"\n[logging]\ndir = "./nope"\n'
    )
    monkeypatch.chdir(tmp_path)
    with patch("kew.cli.preflight_tools"):
        result = runner.invoke(app, ["approve", "42"])
    assert result.exit_code == 2  # no audit store -> fail loud, no silent no-op


def test_report_notify_posts(monkeypatch, tmp_path):
    (tmp_path / "kew.toml").write_text('[project]\nrepo = "o/r"\n')
    monkeypatch.chdir(tmp_path)
    with (
        patch("kew.cli.preflight_tools"),
        patch("kew.cli._detect_repo"),
        patch("kew.cli.build_report_result", return_value=ReportResult("# spend")),
        patch("kew.cli.deliver_report", return_value=True) as dr,
    ):
        result = runner.invoke(app, ["report", "--notify"])
    assert result.exit_code == 0
    dr.assert_called_once()


def test_sync_detects_repo_before_syncing(monkeypatch, tmp_path):
    """kew report sync must auto-detect the repo (parity with report/dispatch) — else
    it silently syncs 0 when [project] repo is unset (#626)."""
    (tmp_path / ".git").mkdir(exist_ok=True)
    (tmp_path / "kew.toml").write_text('[project]\nname = "kew"\n')  # no repo
    monkeypatch.chdir(tmp_path)

    def fake_detect(cfg):
        cfg.project.repo = "o/r"  # stand in for `gh repo view`

    with (
        patch("kew.cli.preflight_tools"),
        patch("kew.cli._detect_repo", side_effect=fake_detect) as detect,
        patch("kew.cli.sync_outcomes", return_value=SyncResult(synced=3)) as sync,
        patch(
            "kew.cli.reconcile_orphan_prs", return_value=OrphanReconciliationResult()
        ),
    ):
        result = runner.invoke(app, ["report", "sync"])
    assert result.exit_code == 0
    detect.assert_called_once()
    sync.assert_called_once()
    assert "synced 3" in result.output


def test_sync_warns_and_skips_when_repo_undetectable(monkeypatch, tmp_path):
    """An unavailable repository prevents synchronization and returns exit 2."""
    (tmp_path / "kew.toml").write_text('[project]\nname = "kew"\n')
    monkeypatch.chdir(tmp_path)
    with (
        patch("kew.cli.preflight_tools"),
        patch("kew.cli._detect_repo"),  # no-op -> repo stays empty
        patch("kew.cli.sync_outcomes", return_value=0) as sync,
    ):
        result = runner.invoke(app, ["report", "sync"])
    assert result.exit_code == 2
    assert "repo" in result.output.lower()
    sync.assert_not_called()


def test_report_notify_no_webhook_exits_2(monkeypatch, tmp_path):
    (tmp_path / "kew.toml").write_text('[project]\nrepo = "o/r"\n')
    monkeypatch.chdir(tmp_path)
    with (
        patch("kew.cli.preflight_tools"),
        patch("kew.cli._detect_repo"),
        patch("kew.cli.build_report_result", return_value=ReportResult("# spend")),
        patch("kew.cli.deliver_report", return_value=False),
    ):
        result = runner.invoke(app, ["report", "--notify"])
    assert result.exit_code == 2


def test_report_notify_delivery_failure_exits_2(monkeypatch, tmp_path):
    # A configured webhook whose POST fails surfaces as an error, NOT a silent
    # "delivered" success. The delivery could not run, so it exits 2 (#1943).
    from kew.notifier import ReportDeliveryError

    (tmp_path / "kew.toml").write_text('[project]\nrepo = "o/r"\n')
    monkeypatch.chdir(tmp_path)
    with (
        patch("kew.cli.preflight_tools"),
        patch("kew.cli._detect_repo"),
        patch("kew.cli.build_report_result", return_value=ReportResult("# spend")),
        patch("kew.cli.deliver_report", side_effect=ReportDeliveryError("POST failed")),
    ):
        result = runner.invoke(app, ["report", "--notify"])
    assert result.exit_code == 2
    assert "POST failed" in result.output


def test_preflight_binds_policy_context(tmp_path, monkeypatch):
    import subprocess

    import kew.audit as audit
    import kew.cli as cli
    from kew.config import DispatchConfig

    config = DispatchConfig(project={"repo": "o/r"})
    # _preflight shells out to `gh auth status` (cli.run) and calls
    # preflight_tools() BEFORE binding — stub both so the test is hermetic and
    # does not depend on gh being authenticated in CI.
    monkeypatch.setattr(cli, "_detect_repo", lambda _c: None)
    monkeypatch.setattr(cli, "preflight_tools", lambda *a, **k: None)
    monkeypatch.setattr(
        cli, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "", "")
    )
    monkeypatch.chdir(tmp_path)  # mkdir of log/worktree dirs lands in tmp
    try:
        cli._preflight(config)
        assert audit.current_policy_hash() is not None
    finally:
        audit.set_audit_context(repo=None)


def test_cli_backup_then_restore_roundtrip(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir(exist_ok=True)
    from datetime import UTC, datetime

    from kew.run_store import RunRecord, SqliteRunStore

    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    monkeypatch.setenv("KEW_LOG_DIR", str(log_dir))
    with SqliteRunStore(str(log_dir / "dispatch.db")) as store:
        store.save(
            RunRecord(
                run_id="r1",
                issue_num=1,
                branch="b",
                status="success",
                exit_code=0,
                started_at=datetime.now(UTC),
            )
        )

    out = tmp_path / "backups"
    res = runner.invoke(
        app, ["--project", str(tmp_path), "backup", "--output", str(out)]
    )
    assert res.exit_code == 0, res.output
    archive = next(out.glob("*.tar.gz"))

    # wipe and restore
    (log_dir / "dispatch.db").unlink()
    res2 = runner.invoke(app, ["--project", str(tmp_path), "restore", str(archive)])
    assert res2.exit_code == 0, res2.output
    with SqliteRunStore(str(log_dir / "dispatch.db")) as store:
        assert store.get("r1") is not None


@pytest.mark.parametrize("failure", ["existing_db", "unreadable"])
def test_restore_prerequisite_errors_exit_two(tmp_path, monkeypatch, failure):
    (tmp_path / ".git").mkdir(exist_ok=True)
    from kew.backup import create_backup
    from kew.run_store import SqliteRunStore

    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    monkeypatch.setenv("KEW_LOG_DIR", str(log_dir))
    with SqliteRunStore(log_dir / "dispatch.db"):
        pass
    archive = tmp_path / "backup.tar.gz"
    create_backup(log_dir / "dispatch.db", None, archive)
    message = "pass --force"
    if failure == "unreadable":
        monkeypatch.setattr(
            "kew.backup.tarfile.open", MagicMock(side_effect=PermissionError("denied"))
        )
        message = "not a readable kew backup archive"

    result = runner.invoke(app, ["--project", str(tmp_path), "restore", str(archive)])

    assert result.exit_code == 2, result.output
    assert message in result.output
    assert (log_dir / "dispatch.db").is_file()


def test_cli_backup_no_db_exits_nonzero(tmp_path, monkeypatch):
    monkeypatch.setenv("KEW_LOG_DIR", str(tmp_path / "empty"))
    res = runner.invoke(app, ["backup"])
    assert res.exit_code == 2


class TestFormatRouteLine:
    """The dispatch-banner Route line surfaces route + provenance (#659)."""

    def _config(self, tmp_path, body: str):
        from kew.config import load_config

        toml = tmp_path / "kew.toml"
        toml.write_text(body)
        return load_config(config_path=toml)

    def test_file_provenance_for_routing(self, tmp_path):
        from kew.cli import _format_route_line

        cfg = self._config(
            tmp_path,
            "[routing]\n"
            'default = { runner = "claude-code", model = "claude-sonnet-4" }\n'
            'medium = { runner = "claude-code", model = "claude-opus-4-8" }\n',
        )
        line = _format_route_line(cfg, "medium")
        assert line == ("Route: claude-code / claude-opus-4-8 (routing.medium, file)")

    def test_agent_model_file_provenance(self, tmp_path):
        from kew.cli import _format_route_line

        cfg = self._config(tmp_path, '[agent]\nmodel = "claude-opus-4-8"\n')
        line = _format_route_line(cfg, "medium")
        assert line == ("Route: claude-code / claude-opus-4-8 (agent.model, file)")

    def test_override_provenance_names_actor_and_reason(self, tmp_path):
        from kew.cli import _format_route_line
        from kew.config import load_config
        from kew.run_store import SqliteRunStore

        toml = tmp_path / "kew.toml"
        toml.write_text('[agent]\nmodel = "claude-sonnet-4"\n')
        with SqliteRunStore(str(tmp_path / "d.db")) as store:
            store.set_config_override(
                field="agent.model",
                value_json='"claude-opus-4-8"',
                file_value_json='"claude-sonnet-4"',
                set_at="2026-07-16T00:00:00+00:00",
                set_by="tom",
                reason="opus race",
            )
            cfg = load_config(config_path=toml, store=store)
            line = _format_route_line(cfg, "medium", store=store)
        assert line == (
            "Route: claude-code / claude-opus-4-8 "
            "(agent.model, override by tom: opus race)"
        )


class TestEpicRetryDelegates:
    """`kew epic retry` delegates to service.verbs.epic_retry; output unchanged."""

    @patch("kew.cli.service_verbs.epic_retry")
    @patch("kew.cli._open_run_store")
    @patch("kew.cli._epic_repo", return_value="o/r")
    @patch("kew.cli.preflight_tools")
    @patch("kew.cli.load_config")
    def test_retry_calls_service_verb(
        self, load_cfg, _preflight, _repo, open_store, verb, tmp_path
    ):
        cfg = DispatchConfig()
        cfg.project.repo = "o/r"
        cfg.logging.dir = str(tmp_path / "logs")
        load_cfg.return_value = cfg
        open_store.return_value.__enter__.return_value = MagicMock()

        result = runner.invoke(app, ["epic", "retry", "epic:x", "42"])

        assert result.exit_code == 1, result.output
        assert (
            "no live driver — relaunch required: kew epic run epic:x" in result.output
        )
        verb.assert_called_once()
        kwargs = verb.call_args.kwargs
        assert kwargs["epic_ref"] == "epic:x"
        assert kwargs["item_id"] == "42"
        assert "restored 'ready' and cleared failure labels" in result.output
        assert "recorded a retry marker and restored admission state" in result.output

    @patch("kew.cli.load_config")
    def test_retry_rejects_non_integer_node(self, load_cfg):
        # Under a GitHub tracker a non-numeric node is not a canonical item id.
        cfg = DispatchConfig()
        load_cfg.return_value = cfg
        result = runner.invoke(app, ["epic", "retry", "epic:x", "notanint"])
        assert result.exit_code == 2
        assert "must be a valid github item id" in result.output

    @patch("kew.cli._epic_repo", return_value="o/r")
    @patch("kew.cli.preflight_tools")
    @patch("kew.cli.load_config")
    def test_retry_refuses_a_held_node_and_leaves_the_hold(
        self, load_cfg, _preflight, _repo, tmp_path
    ):
        """#2154: retry printed success while the node stayed HELD."""
        from kew.run_store import SqliteRunStore

        cfg = DispatchConfig()
        cfg.project.repo = "o/r"
        log_dir = tmp_path / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        cfg.logging.dir = str(log_dir)
        load_cfg.return_value = cfg
        with SqliteRunStore(str(log_dir / "dispatch.db")) as store:
            store.hold_epic_node_item("epic:x", "42", "review_unparseable")

        with patch("kew.labels.run") as gh:
            gh.return_value = MagicMock(returncode=0, stdout="", stderr="")
            result = runner.invoke(app, ["epic", "retry", "epic:x", "42"])

        assert result.exit_code == 2, result.output
        assert "held (review_unparseable)" in result.output
        assert "kew epic unhold epic:x 42" in result.output
        assert "restored admission" not in result.output
        gh.assert_not_called()
        with SqliteRunStore(str(log_dir / "dispatch.db")) as store:
            assert [h.item_id for h in store.list_epic_holds("epic:x")] == ["42"]
            assert store.list_epic_retries("epic:x") == []


class TestEpicUnhold:
    """`kew epic unhold` releases a driver hold; github also strips the label."""

    def _seed_hold(self, tmp_path, epic_ref, item_id, reason):
        from kew.run_store import SqliteRunStore

        log_dir = tmp_path / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        with SqliteRunStore(str(log_dir / "dispatch.db")) as store:
            store.hold_epic_node_item(epic_ref, item_id, reason)

    @patch("kew.cli.clear_pending_review_label")
    @patch("kew.cli._epic_repo", return_value="o/r")
    @patch("kew.cli.preflight_tools")
    @patch("kew.cli.load_config")
    def test_github_clears_hold_and_strips_label(
        self, load_cfg, _preflight, _repo, strip, tmp_path
    ):
        (tmp_path / ".git").mkdir(exist_ok=True)
        self._seed_hold(tmp_path, "epic:x", "42", "ci_pending")
        cfg = DispatchConfig()
        cfg.project.repo = "o/r"
        cfg.logging.dir = str(tmp_path / "logs")
        load_cfg.return_value = cfg

        result = runner.invoke(app, ["epic", "unhold", "epic:x", "42"])

        assert result.exit_code == 0, result.output
        # Names the cleared reason in the output.
        assert "ci_pending" in result.output
        assert "#42" in result.output
        # Label strip called for github, with the issue number.
        strip.assert_called_once()
        assert strip.call_args.args[0] == 42
        # Hold row is gone.
        from kew.run_store import SqliteRunStore

        with SqliteRunStore(str(tmp_path / "logs" / "dispatch.db")) as store:
            assert store.list_epic_holds("epic:x") == []

    @patch("kew.cli.clear_pending_review_label")
    @patch("kew.cli._epic_repo", return_value="o/r")
    @patch("kew.cli.preflight_tools")
    @patch("kew.cli.load_config")
    def test_releasing_a_gate_hold_records_the_head_the_human_read(
        self, load_cfg, _preflight, _repo, strip, tmp_path
    ):
        """#2192: the merge queue re-derives `merge_gate_touched` from the PR's
        unchanged diff every pass, so clearing the row releases nothing on its
        own. The release writes the receipt the queue reads back — and the
        queue's half of it reads the head out of exactly this string."""
        from kew.epic_holds import merge_gate_ack_key, merge_gate_reviewed_head
        from kew.run_store import SqliteRunStore

        head = "a" * 40
        reason = f"merge_gate_touched@{head}: scripts/verify.sh"
        (tmp_path / ".git").mkdir(exist_ok=True)
        self._seed_hold(tmp_path, "epic:x", "42", reason)
        cfg = DispatchConfig()
        cfg.project.repo = "o/r"
        cfg.logging.dir = str(tmp_path / "logs")
        load_cfg.return_value = cfg

        result = runner.invoke(app, ["epic", "unhold", "epic:x", "42"])

        assert result.exit_code == 0, result.output
        assert "reviewed head recorded" in result.output
        with SqliteRunStore(str(tmp_path / "logs" / "dispatch.db")) as store:
            receipt = store.get_meta(merge_gate_ack_key("epic:x", "42"))
            assert merge_gate_reviewed_head(receipt) == head
            events = store.list_audit_events(event="epic_node_unhold")
            assert events[0].payload["merge_gate_head_reviewed"] == head

    @patch("kew.cli.clear_pending_review_label")
    @patch("kew.cli._epic_repo", return_value="o/r")
    @patch("kew.cli.preflight_tools")
    @patch("kew.cli.load_config")
    def test_releasing_any_other_hold_records_no_gate_receipt(
        self, load_cfg, _preflight, _repo, strip, tmp_path
    ):
        """The receipt is the one thing that lets a PR which edits the gates be
        verified, so no other release may leave one."""
        from kew.epic_holds import merge_gate_ack_key
        from kew.run_store import SqliteRunStore

        (tmp_path / ".git").mkdir(exist_ok=True)
        self._seed_hold(tmp_path, "epic:x", "42", "ci_pending")
        cfg = DispatchConfig()
        cfg.project.repo = "o/r"
        cfg.logging.dir = str(tmp_path / "logs")
        load_cfg.return_value = cfg

        result = runner.invoke(app, ["epic", "unhold", "epic:x", "42"])

        assert result.exit_code == 0, result.output
        assert "reviewed head recorded" not in result.output
        with SqliteRunStore(str(tmp_path / "logs" / "dispatch.db")) as store:
            assert store.get_meta(merge_gate_ack_key("epic:x", "42")) is None
            events = store.list_audit_events(event="epic_node_unhold")
            assert events[0].payload["merge_gate_head_reviewed"] is None

    @patch("kew.cli.clear_pending_review_label")
    @patch("kew.cli._epic_repo", return_value="o/r")
    @patch("kew.cli.preflight_tools")
    @patch("kew.cli.load_config")
    def test_linear_clears_hold_without_label_strip(
        self, load_cfg, _preflight, _repo, strip, tmp_path
    ):
        (tmp_path / ".git").mkdir(exist_ok=True)
        self._seed_hold(tmp_path, "epic:x", "SFD-12", "ci_pending")
        cfg = DispatchConfig()
        cfg.tracker.type = "linear"
        cfg.project.repo = "o/r"
        cfg.logging.dir = str(tmp_path / "logs")
        load_cfg.return_value = cfg

        result = runner.invoke(app, ["epic", "unhold", "epic:x", "SFD-12"])

        assert result.exit_code == 0, result.output
        assert "SFD-12" in result.output
        # A Linear item has no GitHub label to strip.
        strip.assert_not_called()
        from kew.run_store import SqliteRunStore

        with SqliteRunStore(str(tmp_path / "logs" / "dispatch.db")) as store:
            assert store.list_epic_holds("epic:x") == []

    @patch("kew.cli.clear_pending_review_label")
    @patch("kew.cli._epic_repo", return_value="o/r")
    @patch("kew.cli.preflight_tools")
    @patch("kew.cli.load_config")
    def test_round_cap_release_restarts_the_review_cycle(
        self, load_cfg, _preflight, _repo, strip, tmp_path
    ):
        """#2154: unhold is now the only way back from a round-cap hold.

        `kew epic retry` writes the marker that restarts round numbering and
        refuses a held node, so a release that left the count standing would be
        re-HELD on it by the next review pass with no recovery left.
        """
        from kew.run_store import SqliteRunStore

        (tmp_path / ".git").mkdir(exist_ok=True)
        self._seed_hold(tmp_path, "epic:x", "42", "review_rounds_exhausted")
        cfg = DispatchConfig()
        cfg.project.repo = "o/r"
        cfg.logging.dir = str(tmp_path / "logs")
        load_cfg.return_value = cfg
        db = str(tmp_path / "logs" / "dispatch.db")
        with SqliteRunStore(db) as store:
            store.record_review_round(
                "epic:x", "42", head_sha="a" * 40, verdict="NEEDS_CHANGES"
            )
            assert store.review_round_count("42") == 1

        result = runner.invoke(app, ["epic", "unhold", "epic:x", "42"])

        assert result.exit_code == 0, result.output
        assert "review cycle restarted" in result.output
        with SqliteRunStore(db) as store:
            assert store.list_epic_holds("epic:x") == []
            assert store.review_round_count("42") == 0
            # The full history survives the restart, exactly as a retry's does.
            assert len(store.list_review_rounds("42")) == 1

    @patch("kew.cli.clear_pending_review_label")
    @patch("kew.cli._epic_repo", return_value="o/r")
    @patch("kew.cli.preflight_tools")
    @patch("kew.cli.load_config")
    def test_other_reasons_keep_their_round_count(
        self, load_cfg, _preflight, _repo, strip, tmp_path
    ):
        """Only the round cap resets: a budget hold clears against a raised cap,
        and its rounds are the history that cap is measured over."""
        from kew.run_store import SqliteRunStore

        (tmp_path / ".git").mkdir(exist_ok=True)
        self._seed_hold(tmp_path, "epic:x", "42", "review_budget: $43.97 of $40.00")
        cfg = DispatchConfig()
        cfg.project.repo = "o/r"
        cfg.logging.dir = str(tmp_path / "logs")
        load_cfg.return_value = cfg
        db = str(tmp_path / "logs" / "dispatch.db")
        with SqliteRunStore(db) as store:
            store.record_review_round(
                "epic:x", "42", head_sha="a" * 40, verdict="NEEDS_CHANGES"
            )

        result = runner.invoke(app, ["epic", "unhold", "epic:x", "42"])

        assert result.exit_code == 0, result.output
        assert "review cycle restarted" not in result.output
        with SqliteRunStore(db) as store:
            assert store.review_round_count("42") == 1
            assert store.list_epic_retries("epic:x") == []

    @patch("kew.cli.clear_pending_review_label")
    @patch("kew.cli._epic_repo", return_value="o/r")
    @patch("kew.cli.preflight_tools")
    @patch("kew.cli.load_config")
    def test_release_is_attested_on_the_audit_chain(
        self, load_cfg, _preflight, _repo, strip, tmp_path
    ):
        """#2154: the round-cap release writes the `epic_retries` marker that
        moves `review_cycle_start`, so it must leave the same kind of trace
        `kew epic retry` (the marker's only other writer) does."""
        from kew.audit_chain import verify_chain
        from kew.run_store import SqliteRunStore

        (tmp_path / ".git").mkdir(exist_ok=True)
        self._seed_hold(tmp_path, "epic:x", "42", "review_rounds_exhausted")
        cfg = DispatchConfig()
        cfg.project.repo = "o/r"
        cfg.logging.dir = str(tmp_path / "logs")
        load_cfg.return_value = cfg
        db = str(tmp_path / "logs" / "dispatch.db")

        result = runner.invoke(app, ["epic", "unhold", "epic:x", "42"])

        assert result.exit_code == 0, result.output
        with SqliteRunStore(db) as store:
            events = store.list_audit_events(event="epic_node_unhold")
            assert len(events) == 1
            assert events[0].payload["epic_ref"] == "epic:x"
            assert events[0].payload["node"] == "42"
            assert events[0].payload["reason"] == "review_rounds_exhausted"
            # The extra reviewer rounds this release granted are the fact the
            # chain has to carry — a bare release grants none.
            assert events[0].payload["review_cycle_restarted"] is True
            assert events[0].issue_num == 42
            assert events[0].item_id == "42"
            assert verify_chain(store.read_chain_rows()).ok

    @patch("kew.cli.clear_pending_review_label")
    @patch("kew.cli._epic_repo", return_value="o/r")
    @patch("kew.cli.preflight_tools")
    @patch("kew.cli.load_config")
    def test_attested_release_flags_an_unrestarted_cycle_and_flattens_the_reason(
        self, load_cfg, _preflight, _repo, strip, tmp_path
    ):
        """Every release is attested, not just the round cap — and the reason
        is untrusted text on an `admission_refused` hold (a detached child's
        stderr tail), so it is flattened before it reaches the chain."""
        from kew.run_store import SqliteRunStore

        (tmp_path / ".git").mkdir(exist_ok=True)
        cfg = DispatchConfig()
        cfg.tracker.type = "linear"
        cfg.project.repo = "o/r"
        cfg.logging.dir = str(tmp_path / "logs")
        load_cfg.return_value = cfg
        self._seed_hold(
            tmp_path,
            "epic:x",
            "SFD-12",
            "admission_refused: boom\nsecond line\x07",
        )
        db = str(tmp_path / "logs" / "dispatch.db")

        result = runner.invoke(app, ["epic", "unhold", "epic:x", "SFD-12"])

        assert result.exit_code == 0, result.output
        with SqliteRunStore(db) as store:
            events = store.list_audit_events(event="epic_node_unhold")
            assert len(events) == 1
            assert events[0].payload["review_cycle_restarted"] is False
            assert events[0].payload["reason"] == (
                "admission_refused: boom second line"
            )
            assert events[0].payload["node"] == "SFD-12"
            # 0-sentinel issue_num for a non-numeric tracker, as retry records.
            assert events[0].issue_num == 0
            assert events[0].item_id == "SFD-12"

    @patch("kew.cli.clear_pending_review_label")
    @patch("kew.cli._epic_repo", return_value="o/r")
    @patch("kew.cli.preflight_tools")
    @patch("kew.cli.load_config")
    def test_exit_2_when_no_hold(self, load_cfg, _preflight, _repo, strip, tmp_path):
        (tmp_path / ".git").mkdir(exist_ok=True)
        (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
        cfg = DispatchConfig()
        cfg.project.repo = "o/r"
        cfg.logging.dir = str(tmp_path / "logs")
        load_cfg.return_value = cfg

        result = runner.invoke(app, ["epic", "unhold", "epic:x", "42"])

        assert result.exit_code == 2
        assert "no hold" in result.output
        strip.assert_not_called()

    @patch("kew.cli.load_config")
    def test_rejects_invalid_item_id(self, load_cfg):
        cfg = DispatchConfig()
        load_cfg.return_value = cfg
        result = runner.invoke(app, ["epic", "unhold", "epic:x", "notanint"])
        assert result.exit_code == 2
        assert "must be a valid github item id" in result.output

    @patch("kew.cli.load_config")
    def test_help_text_meaningful(self, load_cfg):
        result = runner.invoke(app, ["epic", "unhold", "--help"])
        assert result.exit_code == 0
        assert "hold" in result.output.lower()


class TestSandboxSmoke:
    @patch("kew.sandbox.run_smoke")
    @patch("kew.cli.load_config")
    def test_smoke_exit_0_when_ok(self, load_cfg, mock_smoke):
        from kew.sandbox import SmokeResult

        load_cfg.return_value = DispatchConfig()
        mock_smoke.return_value = SmokeResult(ok=True, layer="ok", detail="all OK")

        result = runner.invoke(app, ["sandbox", "smoke"])

        assert result.exit_code == 0, result.output
        assert "OK" in result.output

    @patch("kew.sandbox.run_smoke")
    @patch("kew.cli.load_config")
    def test_smoke_exit_1_names_failure_layer(self, load_cfg, mock_smoke):
        from kew.sandbox import SmokeResult

        load_cfg.return_value = DispatchConfig()
        mock_smoke.return_value = SmokeResult(
            ok=False, layer="image", detail="local build failed"
        )

        result = runner.invoke(app, ["sandbox", "smoke"])

        assert result.exit_code == 1
        assert "image" in result.output

    @patch("kew.sandbox.run_smoke")
    @patch("kew.cli.load_config")
    def test_smoke_no_build_flag_threads_through(self, load_cfg, mock_smoke):
        from kew.sandbox import SmokeResult

        load_cfg.return_value = DispatchConfig()
        mock_smoke.return_value = SmokeResult(ok=True, layer="ok", detail="ok")

        result = runner.invoke(app, ["sandbox", "smoke", "--no-build"])

        assert result.exit_code == 0, result.output
        assert mock_smoke.call_args.kwargs["build_if_missing"] is False

    @patch("kew.sandbox.run_smoke")
    @patch("kew.cli.load_config")
    def test_smoke_defaults_to_the_claude_runner(self, load_cfg, mock_smoke):
        from kew.sandbox import SmokeResult

        load_cfg.return_value = DispatchConfig()
        mock_smoke.return_value = SmokeResult(ok=True, layer="ok", detail="ok")

        result = runner.invoke(app, ["sandbox", "smoke"])

        assert result.exit_code == 0, result.output
        assert mock_smoke.call_args.kwargs["runner"] == "claude-code"

    @patch("kew.sandbox.run_smoke")
    @patch("kew.cli.load_config")
    def test_smoke_runner_flag_threads_through(self, load_cfg, mock_smoke):
        # #1144: an operator must be able to prove the image a NON-claude route
        # will actually launch, before routing work at it.
        from kew.sandbox import SmokeResult

        load_cfg.return_value = DispatchConfig()
        mock_smoke.return_value = SmokeResult(ok=True, layer="ok", detail="ok")

        result = runner.invoke(app, ["sandbox", "smoke", "--runner", "opencode"])

        assert result.exit_code == 0, result.output
        assert mock_smoke.call_args.kwargs["runner"] == "opencode"

    @patch("kew.sandbox.run_smoke")
    @patch("kew.cli.load_config")
    def test_smoke_rejects_an_unknown_runner(self, load_cfg, mock_smoke):
        # Named as a bad runner, not reported as a missing image mapping for a
        # runner nothing could ever resolve to.
        load_cfg.return_value = DispatchConfig()

        result = runner.invoke(app, ["sandbox", "smoke", "--runner", "opencde"])

        assert result.exit_code == 2
        assert "unknown runner" in result.output
        mock_smoke.assert_not_called()


class TestSandboxBuild:
    @patch("kew.sandbox.build_sandbox_image")
    @patch("kew.cli.load_config")
    def test_build_prints_effective_tag_image_id_and_existing_network_notice(
        self, load_cfg, mock_build
    ):
        from kew.sandbox import SandboxBuildResult

        load_cfg.return_value = DispatchConfig()
        mock_build.return_value = SandboxBuildResult(
            tag="kew-agent:docker-123456789abc",
            image_id="sha256:" + "a" * 64,
            allowlist_network_exists=True,
            pinned=True,
        )

        result = runner.invoke(app, ["sandbox", "build"])

        assert result.exit_code == 0, result.output
        assert "kew-agent:docker-123456789abc" in result.output
        assert "sha256:" + "a" * 64 in result.output
        assert "kew-allowlist exists" in result.output
        assert "did not modify it" in result.output
        assert mock_build.call_args.kwargs == {
            "runner": "claude-code",
            "tag": None,
            "unpinned": False,
        }

    @patch("kew.sandbox.build_sandbox_image")
    @patch("kew.cli.load_config")
    def test_build_reports_open_egress_instead_of_a_fail_closed_claim(
        self, load_cfg, mock_build
    ):
        # With network = "open", dispatch never consults kew-allowlist, so a
        # "dispatch will refuse until it exists" line would be a false promise.
        from kew.sandbox import SandboxBuildResult

        load_cfg.return_value = DispatchConfig(sandbox={"network": "open"})
        mock_build.return_value = SandboxBuildResult(
            tag="kew-agent:docker-123456789abc",
            image_id="sha256:" + "c" * 64,
            allowlist_network_exists=False,
            pinned=True,
            network_name=None,
        )

        result = runner.invoke(app, ["sandbox", "build"])

        assert result.exit_code == 0, result.output
        assert 'network = "open"' in result.output
        assert "full egress" in result.output
        assert "Dispatch will refuse" not in result.output

    @pytest.mark.parametrize("exists", [True, False])
    @patch("kew.sandbox.build_sandbox_image")
    @patch("kew.cli.load_config")
    def test_build_reports_the_custom_network_by_name(
        self, load_cfg, mock_build, exists
    ):
        from kew.sandbox import SandboxBuildResult

        load_cfg.return_value = DispatchConfig(sandbox={"network": "corp-egress"})
        mock_build.return_value = SandboxBuildResult(
            tag="kew-agent:docker-123456789abc",
            image_id="sha256:" + "d" * 64,
            allowlist_network_exists=exists,
            pinned=True,
            network_name="corp-egress",
        )

        result = runner.invoke(app, ["sandbox", "build"])

        assert result.exit_code == 0, result.output
        assert "kew-allowlist" not in result.output
        if exists:
            assert "corp-egress exists" in result.output
        else:
            assert "corp-egress is absent" in result.output
            assert "network named corp-egress" in result.output

    @patch("kew.sandbox.build_sandbox_image")
    @patch("kew.cli.load_config")
    def test_build_threads_runner_and_tag(self, load_cfg, mock_build):
        from kew.sandbox import SandboxBuildResult

        load_cfg.return_value = DispatchConfig(
            sandbox={"images": {"opencode": "kew-agent-opencode:latest"}}
        )
        mock_build.return_value = SandboxBuildResult(
            tag="local/opencode:test",
            image_id="sha256:" + "b" * 64,
            allowlist_network_exists=False,
            pinned=False,
        )

        result = runner.invoke(
            app,
            [
                "sandbox",
                "build",
                "--runner",
                "opencode",
                "--tag",
                "local/opencode:test",
                "--unpinned",
            ],
        )

        assert result.exit_code == 0, result.output
        assert "not the packaged input pin" in result.output
        # OpenCode is configured through images.opencode only.
        assert "[sandbox] images.opencode" in result.output
        assert "[sandbox] image (" not in result.output
        assert "kew-allowlist is absent" in result.output
        assert "Dispatch will refuse until it exists" in result.output
        assert "Provision a restricted network" in result.output
        assert "plain bridge permits unrestricted egress" in result.output
        assert mock_build.call_args.kwargs == {
            "runner": "opencode",
            "tag": "local/opencode:test",
            "unpinned": True,
        }

    @patch("kew.sandbox.build_sandbox_image")
    @patch("kew.cli.load_config")
    def test_build_rejects_a_runner_with_no_packaged_image(self, load_cfg, mock_build):
        """`hermes`, not `codex`: codex HAS a packaged image now (#2317).

        A runner kew knows but ships no Dockerfile for is the case this refusal
        exists for, so the name must be one of those or the test proves nothing.
        """
        load_cfg.return_value = DispatchConfig()

        result = runner.invoke(app, ["sandbox", "build", "--runner", "hermes"])

        assert result.exit_code == 2
        assert "claude-code, codex, opencode" in result.output
        mock_build.assert_not_called()

    @patch("kew.sandbox.build_sandbox_image")
    @patch("kew.cli.load_config")
    def test_build_reports_refusal_without_a_traceback(self, load_cfg, mock_build):
        load_cfg.return_value = DispatchConfig()
        mock_build.side_effect = ValueError(
            "configured tag kew-agent:docker-old; expected kew-agent:docker-new"
        )

        result = runner.invoke(app, ["sandbox", "build"])

        assert result.exit_code == 2
        assert "configured tag" in result.output
        assert "expected" in result.output
        assert "Traceback" not in result.output


@pytest.mark.parametrize("claim", [ClaimResult.FETCH_FAILED, ClaimResult.RACE_DETECTED])
def test_fleet_claim_failure_exits_two(claim, dispatch_boundary, tmp_path):
    (tmp_path / ".git").mkdir(exist_ok=True)
    config = dispatch_boundary
    config.overlap.strategy = "off"
    issue = {"number": 42, "title": "test", "body": "", "labels": []}
    with (
        patch("kew.scheduler.ProcessPoolExecutor") as pool,
        patch("kew.scheduler.DispatchScheduler._slots_for_pass", return_value=1),
        patch(
            "kew.scheduler.select_next_issue", side_effect=[issue, None, None, None]
        ) as select,
        patch("kew.scheduler.remote_branch_exists", return_value=False),
        patch("kew.scheduler.try_claim_issue", return_value=claim) as claims,
        patch("kew.scheduler.rollback_claim") as rollback,
        patch("kew.scheduler.time.sleep"),
    ):
        result = runner.invoke(app, ["run", "--drain"])
        assert result.exit_code == 2, result.output
        assert f"Potentially stranded claim: issue #42 ({claim.value})" in result.output
        for contention in (ClaimResult.NOT_READY, ClaimResult.ALREADY_CLAIMED):
            select.side_effect = [issue, None, None, None]
            claims.return_value = contention
            result = runner.invoke(app, ["run", "--drain"])
            assert result.exit_code == 0, result.output
            assert "Potentially stranded" not in result.output
    pool.return_value.submit.assert_not_called()
    rollback.assert_not_called()


@pytest.mark.parametrize("command", ["review", "resolve"])
@pytest.mark.parametrize("raw_code", [0, 1, 2, 3, 127])
def test_remediation_cli_normalizes_executed_codes(
    command, raw_code, dispatch_boundary, tmp_path
):
    (tmp_path / ".git").mkdir(exist_ok=True)
    target = (
        "kew.cli.dispatch_review"
        if command == "review"
        else "kew.resolve.dispatch_resolve"
    )
    with patch(
        target, return_value=SimpleNamespace(kind="executed", exit_code=raw_code)
    ):
        result = runner.invoke(app, [command, "42"])
    assert result.exit_code == int(raw_code != 0), result.output
    # Click must receive an integer, never the tagged result or raw runner code.
    assert result.exception is None or result.exception.code == 1


@pytest.mark.parametrize("command", ["review", "resolve"])
@pytest.mark.parametrize("metadata", [None, {"headRefName": ""}])
def test_remediation_metadata_failure_could_not_run(
    command, metadata, dispatch_boundary, tmp_path
):
    (tmp_path / ".git").mkdir(exist_ok=True)
    with (
        patch(f"kew.{command}._get_pr_metadata", return_value=metadata),
        patch(f"kew.{command}.run_tracked_agent") as launch,
    ):
        result = runner.invoke(app, [command, "42"])
    assert result.exit_code == 2, result.output
    launch.assert_not_called()


@pytest.mark.parametrize("command", ["review", "resolve"])
@pytest.mark.parametrize("raw_code", [1, 2, 3, 127])
def test_remediation_cli_refusal_could_not_run(
    command, raw_code, dispatch_boundary, tmp_path
):
    (tmp_path / ".git").mkdir(exist_ok=True)
    target = (
        "kew.cli.dispatch_review"
        if command == "review"
        else "kew.resolve.dispatch_resolve"
    )
    with patch(
        target, return_value=SimpleNamespace(kind="not_run", exit_code=raw_code)
    ):
        result = runner.invoke(app, [command, "42"])
    assert result.exit_code == 2, result.output


@pytest.mark.parametrize("command", [["run"], ["dispatch", "42"]])
@pytest.mark.parametrize(
    "category",
    [
        "budget_exceeded",
        "containment_refused",
        "publication_configuration_refused",
        "runner_preflight_refused",
        "credential_publication_refused",
        "transport_admission_refused",
        "github_auth",
        "github_unreachable",
        "fleet_stopped",
        "requeued_over_cap",
        "exception",
        "malformed_issue",
    ],
)
def test_dispatch_additional_pre_launch_categories(
    command, category, dispatch_boundary, tmp_path
):
    (tmp_path / ".git").mkdir(exist_ok=True)
    outcome = DispatchOutcome(
        exit_code=1, category=category, reason="refused before launch"
    )
    # Assignment keeps the regression executable against the old dataclass.
    outcome.launched = False
    with (
        patch("kew.cli.try_claim_issue", return_value=ClaimResult.CLAIMED),
        patch("kew.cli.execute_dispatch", return_value=outcome) as launch,
        patch("kew.cli.sync_outcomes"),
    ):
        result = runner.invoke(app, command)
    assert result.exit_code == 2, result.output
    assert (
        f"dispatch did not launch — {category}: refused before launch" in result.output
    )
    assert "agent exited" not in result.output
    launch.assert_called_once()


@pytest.mark.parametrize(
    "dispatch_code,launched,resolve_kind,resolve_code,query_error,expected",
    [
        (137, True, "executed", 0, False, 1),
        (1, False, "executed", 0, False, 2),
        (0, True, "not_run", 1, False, 2),
        (0, True, "executed", 127, False, 1),
        (0, True, "executed", 0, True, 2),
        (137, True, "not_run", 1, True, 2),
    ],
)
def test_round_four_fleet_retains_worker_failures(
    dispatch_boundary,
    dispatch_code,
    launched,
    resolve_kind,
    resolve_code,
    query_error,
    expected,
    tmp_path,
):
    (tmp_path / ".git").mkdir(exist_ok=True)
    import subprocess
    from concurrent.futures import Future

    from kew.command_outcome import CommandOutcome
    from kew.overlap import DispatchOutcome
    from kew.scheduler import DispatchScheduler

    config = dispatch_boundary
    config.overlap.strategy = "off"
    config.resolve.auto = True
    scheduler = DispatchScheduler(config)
    dispatch = Future()
    dispatch.set_result(DispatchOutcome(dispatch_code, launched=launched))
    resolve = Future()
    resolve.set_result(CommandOutcome(resolve_kind, resolve_code))

    def slots(pool, futures, resolve_futures, **kwargs):
        futures[dispatch] = 42
        resolve_futures[resolve] = 87
        if query_error:
            scheduler._poll_conflicting_prs()
        return None

    with (
        patch("kew.cli.DispatchScheduler", return_value=scheduler),
        patch.object(scheduler, "_slots_for_pass", side_effect=slots),
        patch("kew.scheduler.ProcessPoolExecutor"),
        patch(
            "kew.scheduler.subprocess_run",
            side_effect=subprocess.CalledProcessError(1, "gh"),
        ),
    ):
        result = runner.invoke(app, ["run", "--drain"])
    assert result.exit_code == expected, result.output
    if dispatch_code:
        assert "Drain issue #42:" in result.output
    if resolve_code:
        assert "Drain resolve PR #87:" in result.output
    if query_error:
        assert "Drain Conflict PR query: could not run" in result.output


def test_round_four_smoke_missing_docker_cannot_run(monkeypatch):
    monkeypatch.setattr("kew.cli.load_config", lambda: DispatchConfig())
    monkeypatch.setattr("kew.sandbox.detect_runtime", lambda config: None)
    result = runner.invoke(app, ["sandbox", "smoke"])
    assert result.exit_code == 2, result.output
    assert "install Docker/OrbStack" in result.output


def test_round_four_ci_log_query_cannot_run(dispatch_boundary, tmp_path):
    (tmp_path / ".git").mkdir(exist_ok=True)
    import kew.ci_fix as ci_fix

    metadata = {
        "number": 87,
        "headRefName": "agent/42",
        "labels": [{"name": dispatch_boundary.labels.agent_pr}],
    }
    with (
        patch("kew.ci_fix.get_pr_for_run", return_value=metadata),
        patch(
            "kew.ci_fix.run",
            return_value=SimpleNamespace(returncode=1, stdout="", stderr="denied"),
        ) as query,
        patch("kew.ci_fix.run_tracked_agent") as launch,
    ):
        result = runner.invoke(app, ["ci-fix", "123"])
    assert result.exit_code == 2, result.output
    assert "Could not extract CI log" in result.output
    assert "--log-failed" in query.call_args.args[0]
    assert ci_fix.CI_FIX_CANNOT_RUN not in (0, 1, 2, ci_fix.CI_FIX_QUERY_FAILED)
    launch.assert_not_called()


def test_loop_command_containment_refusal_exits_two(monkeypatch, tmp_path):
    from types import SimpleNamespace

    import typer

    from kew import cli

    config = DispatchConfig(sandbox={"enabled": True})
    config.logging.dir = str(tmp_path / "logs")
    monkeypatch.setattr(cli, "load_config", lambda **kwargs: config)
    monkeypatch.setattr(cli, "_detect_repo", lambda config: None)
    monkeypatch.setattr(
        cli,
        "select_next_issue",
        lambda **kwargs: {"number": 1946, "title": "verify", "body": "", "labels": []},
    )
    monkeypatch.setattr(
        cli,
        "_resolve_loop_route",
        lambda *a: SimpleNamespace(
            runner=SimpleNamespace(name="claude-code"), model="claude-sonnet-4-6"
        ),
    )
    monkeypatch.setattr(cli, "_log_budget_banner", lambda *a, **k: None)
    with pytest.raises(typer.Exit) as raised:
        cli._run_loop_dispatch(config, 1946)
    assert raised.value.exit_code == 2


# ── W4.3 (#2000): removed CLI spellings fail with Click's usage error ──────


def _plain(output: str) -> str:
    """Rich renders usage errors with ANSI styling; compare the plain text."""
    return re.sub(r"\x1b\[[0-9;]*m", "", output)


def _assert_no_such_option(result, option: str) -> None:
    assert result.exit_code == 2, result.output
    assert f"No such option '{option}'" in _plain(result.output)


def test_w43_report_export_out_is_removed(tmp_path):
    """Row 1: ``--out`` is gone; ``--output``/``-o`` is the spelling."""
    target = tmp_path / "pack.json"
    _assert_no_such_option(
        runner.invoke(app, ["report", "export", "--out", str(target)]), "--out"
    )
    assert not target.exists()


@pytest.mark.parametrize("flag", ["--output", "-o"])
def test_w43_report_export_output_writes_the_pack(flag, monkeypatch, tmp_path):
    from kew.outcomes import SyncResult

    (tmp_path / "kew.toml").write_text('[project]\nrepo = "o/r"\n')
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "pack.json"
    with (
        patch("kew.cli.preflight_tools"),
        patch("kew.cli._detect_repo"),
        patch("kew.report_export.sync_outcomes", return_value=SyncResult()),
    ):
        result = runner.invoke(
            app, ["report", "export", flag, str(target), "--format", "json"]
        )
    assert result.exit_code == 0, result.output
    assert _json.loads(target.read_text())["kind"] == "kew-evidence-pack"


def test_w43_run_force_is_removed():
    """Row 2: the budget bypass is ``--force-budget``; ``--force`` is gone."""
    with patch("kew.cli._run_dispatch_command") as dispatch:
        result = runner.invoke(app, ["run", "--force"])
    _assert_no_such_option(result, "--force")
    dispatch.assert_not_called()


def test_w43_run_force_budget_bypasses_the_budget():
    with patch("kew.cli._run_dispatch_command") as dispatch:
        result = runner.invoke(app, ["run", "--force-budget"])
    assert result.exit_code == 0, result.output
    assert dispatch.call_args.kwargs["force"] is True


@pytest.mark.parametrize("flag", ["--force", "--no-force", "--no-force-probe"])
def test_w43_route_live_confirm_old_force_flags_are_removed(flag):
    """Row 3: only ``--force-probe`` exists, with no negative form."""
    with patch("kew.cli.doctor.confirm_route_live") as spend:
        result = runner.invoke(app, ["doctor", "route-live-confirm", flag])
    _assert_no_such_option(result, flag)
    spend.assert_not_called()


def test_w43_route_live_confirm_force_probe_spends_despite_fresh_evidence(
    tmp_path, monkeypatch
):
    from kew.readiness import ProbeCost
    from kew.readiness_probe import LiveConfirmation, LiveStatus

    logs = tmp_path / "logs"
    logs.mkdir()
    (tmp_path / "kew.toml").write_text(
        '[project]\nrepo = "o/r"\n'
        "[sandbox]\nenabled = false\ndangerously_skip_containment = true\n"
        f'[logging]\ndir = "{logs}"\n'
    )
    (logs / "dispatch.db").touch()
    monkeypatch.chdir(tmp_path)
    confirmed = LiveConfirmation(
        status=LiveStatus.CONFIRMED,
        fingerprint="f" * 64,
        runner="claude-code",
        requested_model="claude-opus-5",
        detail="detail",
        cost=ProbeCost.observed(0.004, "claude-code"),
        spent=True,
        probe_id="p-1",
    )
    with (
        patch("kew.cli.doctor.register_tools", return_value={}),
        patch("kew.cli.doctor.confirm_route_live", return_value=confirmed) as spend,
        patch(
            "kew.cli.doctor.route_live_status",
            return_value=(LiveStatus.CONFIRMED, None),
        ),
    ):
        result = runner.invoke(
            app, ["doctor", "route-live-confirm", "--force-probe", "--yes"]
        )
    assert result.exit_code == 0, result.output
    assert "not spending" not in result.output
    assert spend.call_args.kwargs["force"] is True


def test_w43_canary_comma_runner_list_is_refused():
    """Row 4: ``--runner a,b`` is one (unknown) runner name, not a list."""
    with patch("kew.cli._run_requested_canary") as run_one:
        result = runner.invoke(app, ["canary", "--runner", "claude-code,opencode"])
    assert result.exit_code == 2, result.output
    assert "unsupported runner 'claude-code,opencode'" in result.output
    run_one.assert_not_called()


def test_w43_canary_runner_is_repeatable(monkeypatch, tmp_path):
    (tmp_path / "kew.toml").write_text('[project]\nrepo = "o/r"\n')
    monkeypatch.chdir(tmp_path)
    with (
        patch("kew.cli.preflight_tools"),
        patch("kew.cli._run_requested_canary") as run_one,
        patch("kew.cli.CanaryReport") as report,
        patch("kew.cli._render_canary"),
    ):
        report.return_value.exit_code = 0
        result = runner.invoke(
            app, ["canary", "--runner", "claude-code", "--runner", "opencode"]
        )
    assert result.exit_code == 0, result.output
    assert [c.args[0] for c in run_one.call_args_list] == ["claude-code", "opencode"]


def test_w43_canary_default_runs_both_runners(monkeypatch, tmp_path):
    (tmp_path / "kew.toml").write_text('[project]\nrepo = "o/r"\n')
    monkeypatch.chdir(tmp_path)
    with (
        patch("kew.cli.preflight_tools"),
        patch("kew.cli._run_requested_canary") as run_one,
        patch("kew.cli.CanaryReport") as report,
        patch("kew.cli._render_canary"),
    ):
        report.return_value.exit_code = 0
        result = runner.invoke(app, ["canary"])
    assert result.exit_code == 0, result.output
    assert [c.args[0] for c in run_one.call_args_list] == ["claude-code", "opencode"]


@pytest.mark.parametrize("command", ["approve", "deny"])
def test_w43_remote_decision_issue_option_is_removed(command):
    """Rows 5-6: the issue is positional; ``--issue`` is gone."""
    with patch("kew.cli._apply_remote_decision") as decide:
        result = runner.invoke(app, [command, "--issue", "42"])
    _assert_no_such_option(result, "--issue")
    decide.assert_not_called()


@pytest.mark.parametrize("command", ["approve", "deny"])
def test_w43_remote_decision_takes_a_positional_issue(command):
    with patch("kew.cli._apply_remote_decision") as decide:
        result = runner.invoke(app, [command, "42", "--reason", "why"])
    assert result.exit_code == 0, result.output
    decide.assert_called_once_with(command, 42, "why")


def test_w43_remote_decision_requires_the_issue(tmp_path):
    with patch("kew.cli._apply_remote_decision") as decide:
        result = runner.invoke(app, ["approve"])
    assert result.exit_code == 2, result.output
    assert "Missing argument 'ISSUE'" in _plain(result.output)
    decide.assert_not_called()


def test_w43_lone_dispatch_toml_refuses_before_any_ledger(monkeypatch, tmp_path):
    """Row 19: exit 2 naming the file; no work, no ledger directory."""
    (tmp_path / ".git").mkdir(exist_ok=True)
    (tmp_path / "dispatch.toml").write_text("[budget]\nper_issue_usd = 3.0\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("KEW_PROJECT", raising=False)
    monkeypatch.delenv("KEW_LOG_DIR", raising=False)
    with patch("kew.cli._open_run_store") as open_store:
        result = runner.invoke(app, ["status"])
    assert result.exit_code == 2, result.output
    plain = _plain(result.output)
    assert str(tmp_path / "dispatch.toml") in plain
    assert "kew does not read dispatch.toml" in plain
    open_store.assert_not_called()
    assert not (tmp_path / "logs").exists()


# ── W4.4 (#2001): one spelling per operation; removed spellings fail ────────


def _assert_no_such_command(result, command: str) -> None:
    assert result.exit_code == 2, result.output
    assert f"No such command '{command}'" in _plain(result.output)


def test_w44_run_finalize_is_removed():
    """Q1: the repair verb is top-level ``kew finalize``; ``run`` takes no ACTION."""
    with patch("kew.cli.service_verbs.finalize_publication_run") as verb:
        result = runner.invoke(app, ["run", "finalize", "run-1"])
    assert result.exit_code == 2, result.output
    assert "unexpected extra argument" in _plain(result.output)
    verb.assert_not_called()


@pytest.mark.parametrize(
    ("error", "code"), [(ValueError("unknown run"), 2), (RuntimeError("raced"), 1)]
)
def test_w44_finalize_exit_codes(error, code, tmp_path):
    cfg = DispatchConfig()
    cfg.logging.dir = str(tmp_path)
    with (
        patch("kew.cli.preflight_tools"),
        patch("kew.cli.load_config", return_value=cfg),
        patch("kew.cli._open_run_store"),
        patch(
            "kew.cli.service_verbs.finalize_publication_run", side_effect=error
        ) as verb,
    ):
        result = runner.invoke(app, ["finalize", "run-1"])
    assert result.exit_code == code, result.output
    assert f"kew finalize: {error}" in _plain(result.output)
    assert verb.call_args.kwargs["run_id"] == "run-1"


def test_w44_finalize_requires_the_run_id():
    with patch("kew.cli.service_verbs.finalize_publication_run") as verb:
        result = runner.invoke(app, ["finalize"])
    assert result.exit_code == 2, result.output
    assert "Missing argument 'RUN_ID'" in _plain(result.output)
    verb.assert_not_called()


@pytest.mark.parametrize(
    ("argv", "option"),
    [
        (["run", "--issue", "42"], "--issue"),
        (["run", "--approve"], "--approve"),
        (["run", "--issue", "42", "--approve"], "--issue"),
        (["run", "--loop"], "--loop"),
        (["run", "--loop", "--workers", "3"], "--loop"),
    ],
)
def test_w44_run_queue_only_options_are_removed(argv, option):
    """Q2/Q3: ``run`` drains the queue; a named issue is ``dispatch <n>``."""
    with patch("kew.cli._run_dispatch_command") as dispatch:
        result = runner.invoke(app, argv)
    _assert_no_such_option(result, option)
    dispatch.assert_not_called()


def test_w44_run_drain_drains_the_queue():
    with patch("kew.cli._run_dispatch_command") as dispatch:
        result = runner.invoke(app, ["run", "--drain", "--workers", "3"])
    assert result.exit_code == 0, result.output
    assert dispatch.call_args.kwargs["drain"] is True
    assert dispatch.call_args.kwargs["workers"] == 3


def test_w44_run_help_names_the_configured_runner():
    result = runner.invoke(app, ["run", "--help"])
    assert result.exit_code == 0, result.output
    text = " ".join(_plain(result.output).split())
    assert "configured runner" in text
    assert "Claude Code" not in text
    assert "--drain" in text
    for removed in ("--issue", "--approve", "--loop", "ACTION", "TARGET_RUN_ID"):
        assert removed not in text, removed


def test_w44_dispatch_takes_the_single_dispatch_options(tmp_path):
    """Q2: moved from ``run``, and passed to the same single-dispatch path."""
    cfg = DispatchConfig()
    cfg.logging.dir = str(tmp_path / "logs")
    with (
        patch("kew.cli.load_config", return_value=cfg),
        patch("kew.cli._preflight"),
        patch("kew.cli._refuse_if_fleet_stopped"),
        patch("kew.cli._log_budget_banner"),
        patch(
            "kew.cli._dispatch_one", return_value=DispatchCommandResult("executed")
        ) as one,
    ):
        result = runner.invoke(
            app,
            [
                "dispatch",
                "42",
                "--dry-run",
                "--show-prompt",
                "--model",
                "claude-opus-5",
                "--approve",
                "--force-budget",
            ],
        )
    assert result.exit_code == 0, result.output
    kwargs = one.call_args.kwargs
    assert kwargs["force_issue"] == 42
    assert kwargs["dry_run"] is True
    assert kwargs["show_prompt"] is True
    assert kwargs["model"] == "claude-opus-5"
    assert kwargs["approve"] is True
    assert kwargs["force_budget"] is True


@pytest.mark.parametrize(
    ("extra", "flag"),
    [
        (["--dry-run"], "--dry-run"),
        (["--show-prompt"], "--show-prompt"),
        (["--model", "claude-opus-5"], "--model"),
        (["--approve"], "--approve"),
        (["--force-budget"], "--force-budget"),
    ],
)
def test_w44_dispatch_loop_refuses_single_dispatch_options(extra, flag):
    with (
        patch("kew.cli.load_config", return_value=DispatchConfig()),
        patch("kew.cli._preflight"),
        patch("kew.cli._refuse_if_fleet_stopped"),
        patch("kew.cli._run_loop_dispatch") as loop,
        patch("kew.cli._dispatch_one") as one,
    ):
        result = runner.invoke(app, ["dispatch", "42", "--loop", *extra])
    assert result.exit_code == 2, result.output
    assert f"{flag} cannot be used with --loop" in _plain(result.output)
    loop.assert_not_called()
    one.assert_not_called()


def test_w44_epic_resume_is_removed():
    """Q4: ``epic continue`` adopts an orphaned worktree; ``epic resume`` is gone."""
    with patch("kew.cli.epic._prepare_resume_record") as prepare:
        result = runner.invoke(app, ["epic", "resume", "epic:x", "42"])
    _assert_no_such_command(result, "resume")
    prepare.assert_not_called()
    help_text = _plain(runner.invoke(app, ["epic", "--help"]).output)
    assert "continue" in help_text


def test_w44_top_level_sync_is_removed():
    """Q5: outcome backfill is ``kew report sync``."""
    with patch("kew.cli.sync_outcomes") as sync:
        result = runner.invoke(app, ["sync"])
    _assert_no_such_command(result, "sync")
    sync.assert_not_called()


@pytest.mark.parametrize("flag", ["--help", "--version"])
def test_python_dash_m_kew_runs_the_cli(flag: str, tmp_path: Path) -> None:
    """#2003: ``python -m kew`` runs the same Typer app as the kew script."""
    import os
    import subprocess
    import sys

    source = Path(__file__).resolve().parents[1] / "src"
    env = {**os.environ, "PYTHONPATH": str(source), "NO_COLOR": "1", "TERM": "dumb"}
    result = subprocess.run(  # noqa: S603 - fixed interpreter and arguments
        [sys.executable, "-m", "kew", flag],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    if flag == "--help":
        assert "Usage: kew " in result.stdout
    else:
        assert result.stdout.startswith("kew ")
