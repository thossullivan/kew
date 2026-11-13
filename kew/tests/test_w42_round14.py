"""Round 14: exec failures, outcome queries, and partial setup findings."""

import json
import os
import subprocess
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from kew.config import DispatchConfig
from kew.outcomes import OrphanReconciliationResult
from kew.run_store import RunRecord, SqliteRunStore
from kew.tracking import DispatchResult


def streamed_attempt(
    tmp_path, code, *, event=False, path="dispatch", finalization_error=False
):
    from test_dispatch_sandbox import _round_nine_run_container
    from test_orchestrator import TestRunAgent

    from kew.sandbox import ContainerHandle

    nonce = "a" * 32
    lines = [f"KEW_AGENT_START {nonce}\n", "shell: agent exec diagnostic\n"]
    if event:
        lines.append('{"type":"system","subtype":"init","session_id":"s"}\n')
    proc = TestRunAgent._mock_popen(exit_code=code, lines=lines)
    # Completion is learned after buffered output, including an agent event.
    proc.poll.return_value = None

    def wait(**kwargs):
        proc.poll.return_value = code
        return code

    proc.wait.side_effect = wait
    argv = ["docker", "run", "-e", f"KEW_AGENT_START_NONCE={nonce}", "image"]
    result = DispatchResult(-1, 42, tmp_path / "run.log")
    with (
        patch("kew.subprocess_utils.popen", return_value=proc),
        patch("kew.orchestrator.popen", return_value=proc),
        patch(
            "kew.subprocess_utils.run",
            return_value=subprocess.CompletedProcess([], 0, "", ""),
        ),
        patch.object(ContainerHandle, "start_watchdog"),
        patch.object(ContainerHandle, "stop_watchdog"),
        patch(
            "kew.dispatch_sandbox.finalize_container_hard_stop",
            side_effect=OSError("finalization failed") if finalization_error else None,
        ),
        patch(
            "kew.orchestrator._record_container_hard_stop",
            side_effect=OSError("finalization failed") if finalization_error else None,
        ),
        patch("kew.orchestrator._remove_container_after_stop"),
    ):
        if finalization_error:
            with pytest.raises(OSError, match="finalization failed"):
                _round_nine_run_container(tmp_path, proc, argv, path, result)
        else:
            _round_nine_run_container(tmp_path, proc, argv, path, result)
    result.exit_code = code
    return result


@pytest.mark.parametrize(
    "code,event", [(126, False), (127, False), (126, True), (127, True), (78, False)]
)
@pytest.mark.parametrize("path", ["dispatch", "orchestrator"])
def test_round14_exec_failure_launch_state(tmp_path, code, event, path):
    from kew.cli import _command_exit_code
    from kew.tracking import command_outcome

    result = streamed_attempt(tmp_path, code, event=event, path=path)
    executed = event or code not in (126, 127)
    assert result.launched is executed
    assert _command_exit_code(command_outcome(result)) == (1 if executed else 2)
    assert (result.error_category == "sandbox_startup_refused") is (not executed)


@pytest.mark.parametrize("code", [126, 127])
@pytest.mark.parametrize("boundary", ["dispatch", "review", "resolve"])
def test_round14_exec_failure_public_boundary(tmp_path, code, boundary):

    refused = streamed_attempt(tmp_path, code)
    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")
    with (
        patch("kew.cli.load_config", return_value=config),
        patch("kew.cli._preflight"),
        patch("kew.cli.preflight_tools"),
        patch("kew.cli._detect_repo"),
        patch("kew.cli._refuse_if_fleet_stopped"),
        patch("kew.cli._open_run_store", return_value=nullcontext(None)),
    ):
        if boundary == "dispatch":
            _assert_dispatch_refusal(tmp_path, config, refused)
        elif boundary == "review":
            _assert_review_refusal(tmp_path, refused)
        else:
            _assert_resolve_refusal(tmp_path, refused)
    assert refused.launched is False  # Refusal rollback remains permitted.
    assert refused.error_category == "sandbox_startup_refused"


def _assert_dispatch_refusal(tmp_path, config, refused):
    from kew.claim_retention import RefusalDelivery
    from kew.cli import _dispatch_exit_code, _execute_and_capture_outcome
    from kew.dispatch_core import _launch_runner

    # The caller's refusal callback is delivered once, as in production.
    on_refuse = MagicMock()
    ctx = SimpleNamespace(
        config=config,
        complexity="medium",
        model_override=None,
        issue_num=42,
        store=None,
        worktree_dir=tmp_path,
        item_id="42",
        run_id="run-42",
        start_monotonic=0,
        epic_ref=None,
        refuse_rollback=RefusalDelivery(on_refuse),
        expected_reservation_run_id=None,
        expected_reservation_generation=None,
        admitted_run_id=None,
        on_admitted=None,
        claim_launch_started=False,
        container_cleanup_pending=False,
    )

    def workspace(_ctx, setup, **kwargs):
        setup.worktree_dir = tmp_path
        setup.run_sandboxed = True
        from kew.tracking import preserve_dispatch_result

        with preserve_dispatch_result(refused):
            raise OSError("finalization failed after stream completion")

    with (
        patch("kew.dispatch_core.resolve_runner"),
        patch("kew.dispatch_core._preflight_pricing"),
        patch("kew.dispatch_core._require_dispatch_transport_admission"),
        patch("kew.dispatch_core._launch_workspace", side_effect=workspace),
        patch("kew.dispatch_core.mark_failed"),
        patch("kew.dispatch_core._emit_early_exit"),
        patch("kew.dispatch_core.cleanup_worktree"),
    ):
        launch = _launch_runner(ctx, None)
    on_refuse.assert_called_once_with("finalization failed after stream completion")
    with patch("kew.cli.execute_dispatch", return_value=launch.early):
        command = _execute_and_capture_outcome(
            issue={"number": 42},
            config=config,
            store=None,
            notifier=None,
            force_budget=False,
            approve=False,
            model=None,
        )
    assert _dispatch_exit_code(command) == 2


def _assert_review_refusal(tmp_path, refused):
    from test_review import _GOVERNED_META, _fake_checkout, _make_review_comment

    from kew.cli import app

    with (
        patch("kew.review._check_github_connectivity", return_value=True),
        patch("kew.review._get_pr_metadata", return_value=_GOVERNED_META),
        patch(
            "kew.review.fetch_review_comments", return_value=[_make_review_comment()]
        ),
        patch(
            "kew.review.materialize_review_checkout",
            return_value=_fake_checkout(tmp_path),
        ),
        patch("kew.review.resolve_runner"),
        patch("kew.review.run_tracked_agent", return_value=refused) as launch,
        patch("kew.review.record_review_outcome"),
        patch("kew.review._canonical_review_publication") as publish,
        patch("kew.review.publish_existing_pr_branch") as push,
        patch("kew.review.discard_checkout"),
    ):
        result = CliRunner().invoke(app, ["review", "42"])
    launch.assert_called_once()
    assert result.exit_code == 2, result.output
    assert refused.stop_reason in result.output
    publish.assert_not_called()
    push.assert_not_called()


def _assert_resolve_refusal(tmp_path, refused):
    from test_resolve import ConflictInfo

    from kew.cli import app

    with (
        patch("kew.resolve._check_github_connectivity", return_value=True),
        patch(
            "kew.resolve._get_pr_metadata",
            return_value={
                "headRefName": "agent/42",
                "baseRefName": "main",
                "mergeable": "CONFLICTING",
            },
        ),
        patch(
            "kew.resolve.checkout_existing_worktree",
            return_value=(tmp_path, ConflictInfo("merge", ["f.txt"], "main")),
        ),
        patch(
            "kew.resolve.resolve_remediation_containment",
            return_value=SimpleNamespace(
                contained=False,
                posture="uncontained_flow",
                image=None,
                reason="test",
            ),
        ),
        patch("kew.resolve.attest_flow_posture"),
        patch("kew.resolve.require_pinned_pr_head"),
        patch("kew.runner_preflight.runner_preflight_failure", return_value=None),
        patch("kew.resolve.run_tracked_agent", return_value=refused) as launch,
        patch("kew.resolve._publish_resolved_branch") as publish,
        patch("kew.resolve._verify_resolved_branch") as verify,
        patch("kew.resolve.cleanup_worktree"),
    ):
        result = CliRunner().invoke(app, ["resolve", "42"])
    launch.assert_called_once()
    assert result.exit_code == 2, result.output
    assert refused.stop_reason in result.output
    publish.assert_not_called()
    verify.assert_not_called()


# No codex: docker/agent-codex.Dockerfile is OpenShell-only and ships no
# entrypoint (#2317). The same refusal on that path is
# tests/test_openshell_launcher.py's
# test_launcher_refuses_a_missing_prompt_and_a_missing_runner.
@pytest.mark.parametrize("agent", ["claude", "opencode"])
@pytest.mark.parametrize("binary", ["missing", "not-executable", "directory"])
@pytest.mark.parametrize("packaged", [False, True])
def test_round14_entrypoint_refuses_unusable_agent(tmp_path, agent, binary, packaged):
    root = Path(__file__).resolve().parents[2]
    base = root / ("kew/src/kew/_docker/docker" if packaged else "docker")
    name = (
        "agent-entrypoint.sh" if agent == "claude" else f"agent-{agent}-entrypoint.sh"
    )
    bins = tmp_path / "bin"
    bins.mkdir()
    for command in ("git", "mkdir", "cp"):
        p = bins / command
        p.write_text("#!/bin/sh\nexit 0\n")
        p.chmod(0o755)
    if binary == "directory":
        (bins / agent).mkdir()
    elif binary == "not-executable":
        (bins / agent).write_text("#!/bin/sh\nexit 0\n")
        (bins / agent).chmod(0o644)
    prompt = tmp_path / "prompt"
    prompt.write_text("do work\n")
    reply = subprocess.run(
        ["/bin/bash", str(base / name)],
        env={
            **os.environ,
            "PATH": str(bins),
            "KEW_PROMPT_FILE": str(prompt),
            "KEW_AGENT_START_NONCE": "a" * 32,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert reply.returncode == 78
    assert (
        reply.stderr.strip() == "ERROR: kew agent executable missing or not executable"
    )
    assert "KEW_AGENT_START" not in reply.stdout


def sync_command(tmp_path, monkeypatch, states):
    import kew.cli as cli

    monkeypatch.chdir(tmp_path)
    cfg = DispatchConfig()
    cfg.project.repo = "o/r"
    cfg.logging.dir = str(tmp_path / "logs")
    store = SqliteRunStore(":memory:")
    for index, _state in enumerate(states):
        store.save(
            RunRecord(
                run_id=f"r{index}",
                issue_num=index + 1,
                branch=f"agent/{index}",
                status="success",
                exit_code=0,
                started_at=datetime.now(UTC),
            )
        )

    def gh(cmd, **kwargs):
        branch = cmd[3]
        state = states[int(branch.split("/")[-1])]
        if state == "auth":
            return subprocess.CompletedProcess(
                cmd, 1, "", "HTTP 401: authentication required"
            )
        if state == "absent":
            return subprocess.CompletedProcess(
                cmd, 1, "", f'no pull requests found for branch "{branch}"\n'
            )
        return subprocess.CompletedProcess(
            cmd,
            0,
            json.dumps(
                {
                    "url": "https://github.com/o/r/pull/7",
                    "state": "MERGED",
                    "mergedAt": "2026-09-01T00:00:00Z",
                    "reviews": [],
                    "additions": 1,
                    "deletions": 0,
                    "changedFiles": 1,
                }
            ),
            "",
        )

    with (
        patch("kew.cli.load_config", return_value=cfg),
        patch("kew.cli.preflight_tools"),
        patch("kew.cli._detect_repo"),
        patch("kew.cli._open_run_store", return_value=nullcontext(store)),
        patch(
            "kew.cli.reconcile_orphan_prs", return_value=OrphanReconciliationResult()
        ),
        patch("kew.outcomes.run", side_effect=gh),
    ):
        result = CliRunner().invoke(cli.app, ["report", "sync"])
    records = [store.get(f"r{i}") for i in range(len(states))]
    store.close()
    return result, records


@pytest.mark.parametrize(
    "states,code,summary",
    [
        (["auth"], 2, "synced 0, no PR 0, query failures 1"),
        (["absent"], 0, "synced 0, no PR 1, query failures 0"),
        (["merged", "absent", "auth"], 2, "synced 1, no PR 1, query failures 1"),
        ([], 0, "synced 0, no PR 0, query failures 0"),
    ],
)
def test_round14_sync_preserves_partial_progress(
    tmp_path, monkeypatch, states, code, summary
):
    result, records = sync_command(tmp_path, monkeypatch, states)
    assert result.exit_code == code, result.output
    assert summary in result.output
    assert [record.pr_state for record in records] == [
        {"auth": None, "absent": "ABSENT", "merged": "MERGED"}[state]
        for state in states
    ]


def setup_command(tmp_path, monkeypatch, *, modified=False):
    import kew.cli as cli
    from kew.skills_catalog import apply_artifact, load_manifest

    monkeypatch.chdir(tmp_path)
    cfg = DispatchConfig()
    cfg.project.repo = "o/r"
    cfg.logging.dir = str(tmp_path / "logs")
    cfg.branches.worktree_base = str(tmp_path / "worktrees")
    if modified:
        artifact = next(iter(load_manifest()))
        apply_artifact(artifact, tmp_path, cfg)
        (tmp_path / artifact.target).write_text("local customization\n")

    def gh(cmd, **kwargs):
        failed = not modified and cmd[1] == "label"
        return subprocess.CompletedProcess(
            cmd, int(failed), '{"name":"r"}', "permission denied" if failed else ""
        )

    with (
        patch("kew.cli.load_config", return_value=cfg),
        patch("kew.cli.preflight_tools"),
        patch("kew.cli._detect_repo"),
        patch("kew.labels.run", side_effect=gh),
    ):
        result = CliRunner().invoke(
            cli.app, ["setup", "--with-skills"] if modified else ["setup"]
        )
    assert (tmp_path / "logs").is_dir()
    assert (tmp_path / "worktrees").is_dir()
    assert (tmp_path / ".claude/hooks/security-guard.sh").is_file()
    if modified:
        assert (tmp_path / artifact.target).read_text() == "local customization\n"
    return result


@pytest.mark.parametrize("modified", [False, True])
def test_round14_partial_setup_returns_findings(tmp_path, monkeypatch, modified):
    result = setup_command(tmp_path, monkeypatch, modified=modified)
    assert result.exit_code == 1, result.output
    assert "Setup findings:" in result.output
    assert (
        "1 artifact(s) skipped" if modified else "0 artifact(s) skipped"
    ) in result.output
    assert "Setup complete!" not in result.output


@pytest.mark.parametrize("code", [126, 127])
@pytest.mark.parametrize("path", ["dispatch", "orchestrator"])
def test_round14_exec_refusal_survives_finalization_failure(tmp_path, code, path):
    from kew.cli import _command_exit_code
    from kew.tracking import command_outcome

    result = streamed_attempt(tmp_path, code, path=path, finalization_error=True)
    assert result.launched is False
    assert result.error_category == "sandbox_startup_refused"
    assert _command_exit_code(command_outcome(result)) == 2
