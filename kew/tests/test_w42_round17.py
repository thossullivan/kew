"""Round 17: exact absence, global admission, and unavailable report outcomes."""

import json
import subprocess
from contextlib import ExitStack, nullcontext
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from support.dispatch_identity import install_dispatch_identity
from typer.testing import CliRunner

import kew.cli as cli
from kew.config import DispatchConfig
from kew.run_store import RunRecord, SqliteRunStore
from kew.tracking import DispatchResult

PRIVATE = "private remote detail: token=secret\x1b[31m"
ABSENCE = 'no pull requests found for branch "agent/42"'
CAP_MESSAGE = "Worker cap reached — cannot start another run."
PROCESS_ERRORS = [OSError, ValueError, subprocess.SubprocessError]


def _reply(stdout="", *, code=0, stderr=""):
    return subprocess.CompletedProcess([], code, stdout, stderr)


def _config(tmp_path, monkeypatch):
    install_dispatch_identity(monkeypatch)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".git").mkdir(exist_ok=True)
    config = DispatchConfig()
    config.project.repo = "o/r"
    config.logging.dir = str(tmp_path / "logs")
    config.branches.prefix = "agent/"
    config.sandbox.dangerously_skip_containment = True
    return config


def _preflight_environment(stack):
    for name in (
        "preflight_tools",
        "_emit_stray_ledger_warnings",
        "_emit_orchestrator_skew_warnings",
        "_emit_governance_drift_warnings",
    ):
        stack.enter_context(patch(f"kew.cli.{name}"))


@pytest.mark.parametrize(
    "code,diagnostic,expected",
    [
        (1, f"HTTP 502 proxy: {ABSENCE}", 2),
        (1, f"{ABSENCE}: HTTP 502 proxy", 2),
        (1, ABSENCE, 0),
        (1, f" \n{ABSENCE}\n ", 0),
        (2, ABSENCE, 2),
        (1, ABSENCE.replace("agent/42", "agent/43"), 2),
    ],
)
def test_round17_ci_fix_absence_at_cli(
    tmp_path, monkeypatch, code, diagnostic, expected
):
    config = _config(tmp_path, monkeypatch)
    with (
        ExitStack() as stack,
        SqliteRunStore(":memory:") as store,
        patch("kew.cli.load_config", return_value=config),
        patch("kew.cli.run", return_value=_reply()),
        patch("kew.cli._open_run_store", return_value=nullcontext(store)),
        patch(
            "kew.ci_fix.run",
            side_effect=[
                _reply(json.dumps({"headBranch": "agent/42"})),
                _reply(code=code, stderr=diagnostic),
            ],
        ),
        patch("kew.ci_fix.run_tracked_agent") as launch,
    ):
        _preflight_environment(stack)
        result = CliRunner().invoke(cli.app, ["ci-fix", "123"])
    assert result.exit_code == expected, result.output
    assert ("skipping" in result.output) is (expected == 0)
    launch.assert_not_called()


@pytest.mark.parametrize(
    "command,seam",
    [
        (["status"], "auth"),
        (["status", "--json"], "auth"),
        (["status"], "repo"),
        (["status", "--json"], "repo"),
        (["report", "sync"], "repo"),
    ],
)
@pytest.mark.parametrize("error", PROCESS_ERRORS)
def test_round17_preflight_process_failure(tmp_path, monkeypatch, command, error, seam):
    config = _config(tmp_path, monkeypatch)
    config.project.repo = ""
    seen = []

    def spawn(cmd, **kwargs):
        seen.append(cmd[1])
        if cmd[1] == seam:
            raise error(PRIVATE)
        return _reply()

    with (
        ExitStack() as stack,
        patch("kew.cli.load_config", return_value=config),
        patch("kew.cli.run", side_effect=spawn),
        patch("kew.cli._open_run_store") as open_store,
    ):
        _preflight_environment(stack)
        result = CliRunner().invoke(cli.app, command)
    assert result.exit_code == 2, result.output
    message = {
        "auth": "GitHub authentication check unavailable — cannot run command.",
        "repo": "Repository detection unavailable — cannot run GitHub query.",
    }[seam]
    assert message in result.stderr
    assert seen[-1] == seam
    assert "private remote detail" not in result.output
    assert "Traceback" not in result.output
    open_store.assert_not_called()


def _record(run_id, *, kind="dispatch", item="900", status="pending", branch=None):
    return RunRecord(
        run_id=run_id,
        issue_num=int(item),
        item_id=item,
        branch=branch or f"agent/{item}",
        run_kind=kind,
        status=status,
        exit_code=-1 if status == "pending" else 0,
        started_at=datetime.now(UTC),
        estimated_cost_usd=3.0,
        model="claude-sonnet-4-6",
    )


def _prepare_review(stack, tmp_path, meta):
    from test_review import _fake_checkout, _make_review_comment

    stack.enter_context(
        patch("kew.review._check_github_connectivity", return_value=True)
    )
    stack.enter_context(patch("kew.review._get_pr_metadata", return_value=meta))
    stack.enter_context(
        patch("kew.review.fetch_review_comments", return_value=[_make_review_comment()])
    )
    stack.enter_context(
        patch(
            "kew.review.materialize_review_checkout",
            return_value=_fake_checkout(tmp_path),
        )
    )
    stack.enter_context(patch("kew.review.record_review_outcome"))


def _prepare_resolve(stack, tmp_path, meta):
    from kew.worktree import ConflictInfo

    stack.enter_context(
        patch("kew.resolve._check_github_connectivity", return_value=True)
    )
    stack.enter_context(patch("kew.resolve._get_pr_metadata", return_value=meta))
    stack.enter_context(
        patch(
            "kew.resolve.checkout_existing_worktree",
            return_value=(tmp_path, ConflictInfo("merge", ["f.txt"], "main")),
        )
    )
    stack.enter_context(patch("kew.resolve.require_pinned_pr_head"))


def _prepare_ci_fix(stack, tmp_path, meta):
    stack.enter_context(patch("kew.ci_fix.get_pr_for_run", return_value=meta))
    stack.enter_context(patch("kew.ci_fix.extract_ci_log", return_value="test failed"))
    stack.enter_context(patch("kew.ci_fix.update_pr_labels"))
    stack.enter_context(
        patch("kew.ci_fix.checkout_existing_worktree", return_value=(tmp_path, None))
    )
    stack.enter_context(patch("kew.ci_fix.require_pinned_pr_head"))


def _prepare_direct_command(stack, tmp_path, command, config):
    containment = SimpleNamespace(
        contained=False, sandboxed=False, image=None, posture="native", reason="test"
    )
    meta = {
        "number": 87,
        "headRefName": "agent/42",
        "headRefOid": "a" * 40,
        "baseRefName": "main",
        "body": "Closes #42",
        "title": "Repair",
        "mergeable": "CONFLICTING",
        "labels": [{"name": config.labels.agent_pr}],
    }
    module = command.replace("-", "_")
    for name in ("review", "resolve"):
        stack.enter_context(
            patch(
                f"kew.{name}.resolve_remediation_containment", return_value=containment
            )
        )
        stack.enter_context(patch(f"kew.{name}.attest_flow_posture"))
    stack.enter_context(
        patch("kew.ci_fix.resolve_flow_containment", return_value=containment)
    )
    stack.enter_context(
        patch("kew.runner_preflight.runner_preflight_failure", return_value=None)
    )
    {
        "review": _prepare_review,
        "resolve": _prepare_resolve,
        "ci_fix": _prepare_ci_fix,
    }[module](stack, tmp_path, meta)


def _admitted_execution(store, launched):
    from kew.dispatch_core import _RetryOutcome

    def execute(run, **kwargs):
        launched.append(store.list_pending())
        row = store.get(run.run_id)
        assert row is not None
        row.status = "failed"
        row.exit_code = 1
        store.save(row)
        # A failed execution avoids testing unrelated publication in this test.
        return _RetryOutcome(
            early_return=DispatchResult(1, run.issue_num, run.log_file, launched=True)
        )

    return execute


@pytest.mark.parametrize("command", ["review", "resolve", "ci-fix"])
@pytest.mark.parametrize("kind", ["dispatch", "review", "resolve", "ci_fix"])
@pytest.mark.parametrize("cap,own_item", [(1, False), (2, False), (1, True)])
def test_round17_direct_command_worker_cap(
    tmp_path, monkeypatch, command, kind, cap, own_item
):
    config = _config(tmp_path, monkeypatch)
    config.parallel.max_workers = cap
    launched = []
    # The cap counts runs, not items (#2628): a pending run of the SAME item is
    # a second run, so at cap 1 it refuses exactly like another item's run.
    refused = cap == 1
    target_item = "87" if command == "review" else "42"
    with SqliteRunStore(":memory:") as store, ExitStack() as stack:
        store.save(
            _record(
                "existing",
                kind=kind,
                item=target_item if own_item else "900",
                branch="agent/existing",
            )
        )
        _prepare_direct_command(stack, tmp_path, command, config)
        _preflight_environment(stack)
        stack.enter_context(patch("kew.cli.load_config", return_value=config))
        stack.enter_context(patch("kew.cli.run", return_value=_reply()))
        stack.enter_context(
            patch("kew.cli._open_run_store", return_value=nullcontext(store))
        )
        execute = stack.enter_context(
            patch(
                "kew.dispatch_core._run_retry_loop",
                side_effect=_admitted_execution(store, launched),
            )
        )
        result = CliRunner().invoke(cli.app, [command, "87"])
        _assert_cap_result(result, store, execute, launched, refused)


def _assert_cap_result(result, store, execute, launched, refused):
    assert result.exit_code == (2 if refused else 1), result.output
    assert len(store.list_all()) == (1 if refused else 2)
    assert [row.run_id for row in store.list_pending()] == ["existing"]
    assert execute.call_count == (0 if refused else 1)
    if refused:
        assert CAP_MESSAGE in result.stderr
    else:
        assert len(launched[0]) == 2


def report_command(
    tmp_path, monkeypatch, *, mode="stdout", failures=1, delivery_failure=False
):
    from kew.notifier import ReportDeliveryError

    config = _config(tmp_path, monkeypatch)
    db_path = Path(config.logging.dir) / "dispatch.db"
    db_path.parent.mkdir(exist_ok=True)
    db_path.touch()
    output = tmp_path / "report.md"
    delivered = []

    def deliver(content, config):
        delivered.append(content)
        if delivery_failure:
            raise ReportDeliveryError("POST failed")
        return True

    flags = {
        "stdout": [],
        "file": ["--output", str(output)],
        "models": ["--models"],
        "notify": ["--notify"],
    }
    with (
        SqliteRunStore(":memory:") as store,
        patch("kew.cli.load_config", return_value=config),
        patch("kew.cli.preflight_tools"),
        patch("kew.report._open_report_store", return_value=nullcontext(store)),
        patch("kew.cli.deliver_report", side_effect=deliver),
        patch(
            "kew.outcomes.run", return_value=_reply(code=1, stderr=PRIVATE)
        ) as remote,
    ):
        for index in range(failures):
            row = _record(f"stale-{index}", item=str(42 + index), status="success")
            row.pr_url = f"https://github.com/o/r/pull/{42 + index}"
            row.pr_state = "OPEN"
            store.save(row)
        result = CliRunner().invoke(cli.app, ["report", *flags[mode]])
        assert remote.call_count == failures
        assert all(row.pr_state == "OPEN" for row in store.list_all())
    content = output.read_text() if mode == "file" else result.stdout
    if mode == "notify":
        assert len(delivered) == 1
        content = delivered[0]
    return result, content


@pytest.mark.parametrize("mode", ["stdout", "file", "models", "notify"])
@pytest.mark.parametrize("failures", [0, 1, 2])
def test_round17_report_retains_partial_output(tmp_path, monkeypatch, mode, failures):
    result, content = report_command(
        tmp_path, monkeypatch, mode=mode, failures=failures
    )
    assert result.exit_code == (2 if failures else 0), result.output
    assert (
        "# Model scorecard" if mode == "models" else "# kew spend report"
    ) in content
    assert "private remote detail" not in result.output
    if failures:
        assert (
            f"Report outcome sync incomplete: query failures {failures}."
            in result.stderr
        )


def test_round17_report_query_failure_overrides_delivery_failure(tmp_path, monkeypatch):
    result, content = report_command(
        tmp_path, monkeypatch, mode="notify", delivery_failure=True
    )
    assert result.exit_code == 2, result.output
    assert "# kew spend report" in content
    assert "Report outcome sync incomplete: query failures 1." in result.stderr


def test_round17_report_absence_is_available(tmp_path, monkeypatch):
    from kew.outcomes import _AbsentPR

    with patch("kew.outcomes._fetch_pr_outcome", return_value=_AbsentPR()):
        # Seed a stale outcome but expect confirmed absence to preserve availability.
        config = _config(tmp_path, monkeypatch)
        path = Path(config.logging.dir) / "dispatch.db"
        path.parent.mkdir()
        path.touch()
        with (
            SqliteRunStore(":memory:") as store,
            patch("kew.cli.load_config", return_value=config),
            patch("kew.cli.preflight_tools"),
            patch("kew.report._open_report_store", return_value=nullcontext(store)),
        ):
            store.save(_record("absent", status="success"))
            result = CliRunner().invoke(cli.app, ["report"])
    assert result.exit_code == 0, result.output
    assert "query failures" not in result.output
