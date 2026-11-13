"""Round 15: retain unavailable capacity, remote reads, and setup preflight."""

import subprocess
from concurrent.futures import Future
from contextlib import nullcontext
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

import kew.cli as cli
from kew.command_outcome import CommandOutcome
from kew.config import DispatchConfig
from kew.labels import ClaimResult
from kew.outcomes import _QueryFailure, reconcile_orphan_prs
from kew.overlap import DispatchOutcome
from kew.run_store import SqliteRunStore
from kew.scheduler import DispatchScheduler
from kew.selector import GhQueryError


def _scheduler():
    config = DispatchConfig()
    config.parallel.max_workers = 3
    config.overlap.strategy = "off"
    config.resolve.auto = True
    store = MagicMock()
    store.list_pending.side_effect = OSError("ledger unavailable")
    store.live_run_count.return_value = 0
    scheduler = DispatchScheduler(config, store=store)
    scheduler._CAPACITY_WAIT_CHECKS = 2
    return scheduler


def test_round15_unreadable_capacity_is_zero():
    scheduler = _scheduler()
    assert scheduler.available_slots == 3
    assert scheduler._fleet_available_slots() == 0
    assert scheduler._required_failures == [
        ("Fleet worker capacity", "ledger unavailable")
    ]


@pytest.mark.parametrize("executed", [False, True])
def test_round15_capacity_failure_drains_without_new_claims(executed):
    scheduler = _scheduler()
    dispatch, resolve = Future(), Future()
    original = scheduler._reap_loop_workers
    seeded = False

    def reap(futures, resolve_futures):
        nonlocal seeded
        if not seeded:
            seeded = True
            futures[dispatch] = 40
            resolve_futures[resolve] = 41
            scheduler.in_flight.add(40)
            scheduler.resolve_in_flight.add(41)
        return original(futures, resolve_futures)

    def finish(_seconds):
        # End this pass with active children; run_loop's exit context drains them.
        scheduler._shutdown = True
        dispatch.set_result(DispatchOutcome(0, launched=executed))
        resolve.set_result(CommandOutcome("executed" if executed else "not_run", 0))

    with (
        patch("kew.scheduler.ProcessPoolExecutor") as executor,
        patch.object(scheduler, "_reap_loop_workers", side_effect=reap),
        patch.object(scheduler, "_fleet_stop_requested", return_value=False),
        patch.object(scheduler, "_check_fleet_breaker"),
        patch.object(scheduler, "_cleanup_stale_pending_if_due", return_value=0),
        patch.object(scheduler, "_poll_conflicting_prs", return_value=[]) as conflicts,
        patch.object(scheduler, "select_available_issues", return_value=[]) as select,
        patch("kew.scheduler.try_claim_issue") as claim,
        patch("kew.scheduler.time.sleep", side_effect=finish),
    ):
        drain = scheduler.run_loop()

    assert drain.required_failures == (("Fleet worker capacity", "ledger unavailable"),)
    assert drain.dispatch_outcomes == ((40, dispatch.result()),)
    assert drain.resolve_outcomes == ((41, resolve.result()),)
    assert not scheduler.in_flight and not scheduler.resolve_in_flight
    select.assert_not_called()
    conflicts.assert_not_called()
    claim.assert_not_called()
    executor.return_value.submit.assert_not_called()
    assert cli._drain_exit_code(drain) == (1 if executed else 2)


def test_round15_persistent_capacity_failure_exits_two():
    scheduler = _scheduler()
    with (
        patch("kew.scheduler.ProcessPoolExecutor"),
        patch.object(scheduler, "_fleet_stop_requested", return_value=False),
        patch.object(scheduler, "_cleanup_stale_pending_if_due", return_value=0),
        patch.object(scheduler, "_poll_conflicting_prs", return_value=[]),
        patch.object(scheduler, "select_available_issues", return_value=[]) as select,
        patch("kew.scheduler.time.sleep"),
    ):
        drain = scheduler.run_loop()
    assert cli._drain_exit_code(drain) == 2
    select.assert_not_called()
    assert len(drain.required_failures) == 2


@pytest.mark.parametrize("preview", [False, True])
def test_round15_capacity_recovery_resumes_claiming(preview):
    scheduler = _scheduler()
    scheduler.store.list_pending.side_effect = [OSError("ledger unavailable"), []]
    pool = MagicMock()
    with (
        patch.object(scheduler, "_log_fleet_capacity"),
        patch.object(scheduler, "_dispatch_conflicting_resolves") as dispatch,
        patch("kew.scheduler.time.sleep"),
    ):
        first = scheduler._poll_dispatch_slots(pool, {}, preview=preview)
        assert first is None if preview else first == 0
        dispatch.assert_not_called()
        assert scheduler._poll_dispatch_slots(pool, {}, preview=preview) == 3
    dispatch.assert_called_once_with(pool, {}, max_count=3)
    assert scheduler._required_failures
    assert scheduler._durable_full_checks == 0


def test_round15_recovered_loop_claims_on_the_next_pass():
    scheduler = _scheduler()
    scheduler.store.list_pending.side_effect = [OSError("ledger unavailable"), []]
    issue = {"number": 42, "title": "ready", "labels": [], "body": ""}
    future = Future()
    future.set_result(DispatchOutcome(0, launched=True))
    passes = []

    def claim(*args):
        passes.append(scheduler.store.list_pending.call_count)
        scheduler._shutdown = True
        return ClaimResult.CLAIMED

    with (
        patch("kew.scheduler.ProcessPoolExecutor") as executor,
        patch.object(scheduler, "_fleet_stop_requested", return_value=False),
        patch.object(scheduler, "_check_fleet_breaker"),
        patch.object(scheduler, "_cleanup_stale_pending_if_due", return_value=0),
        patch.object(scheduler, "_log_fleet_capacity"),
        patch.object(scheduler, "_poll_conflicting_prs", return_value=[]),
        patch.object(scheduler, "select_available_issues", return_value=[issue]),
        patch.object(scheduler, "_build_merge_context", return_value=None),
        patch("kew.scheduler.remote_branch_exists", return_value=False),
        patch("kew.scheduler.try_claim_issue", side_effect=claim),
        patch("kew.dispatch_core._warn_if_cap_conflict"),
        patch("kew.scheduler.time.sleep"),
    ):
        executor.return_value.submit.return_value = future
        drain = scheduler.run_loop()
    assert passes == [2]
    assert drain.required_failures == (("Fleet worker capacity", "ledger unavailable"),)
    assert drain.dispatch_outcomes == ((42, future.result()),)
    assert cli._drain_exit_code(drain) == 1


def test_round15_resolves_respect_durable_capacity():
    scheduler = _scheduler()
    scheduler.store.list_pending.side_effect = None
    scheduler.store.list_pending.return_value = []
    scheduler.store.live_run_count.return_value = 2
    pool = MagicMock()
    prs = [{"number": n, "body": ""} for n in (40, 41)]
    with (
        patch.object(scheduler, "_log_fleet_capacity"),
        patch.object(scheduler, "_poll_conflicting_prs", return_value=prs),
    ):
        assert scheduler._poll_dispatch_slots(pool, {}) == 0
    assert pool.submit.call_count == 1
    assert scheduler.resolve_in_flight == {40}


def _invoke_remote_command(tmp_path, monkeypatch, command, *, repo="o/r", reply=None):
    monkeypatch.chdir(tmp_path)
    config = DispatchConfig()
    config.project.repo = repo
    config.logging.dir = str(tmp_path / "logs")
    store = SqliteRunStore(":memory:")
    store.sync_orphan_prs(
        [(7, "https://github.com/o/r/pull/7", "agent/7", "no-record")]
    )
    with (
        patch("kew.cli.load_config", return_value=config),
        patch("kew.cli.preflight_tools"),
        patch("kew.cli._preflight"),
        patch("kew.cli._detect_repo"),
        patch("kew.cli._open_run_store", return_value=nullcontext(store)),
        patch(
            "kew.cli.run_with_backoff",
            return_value=subprocess.CompletedProcess([], 0, "0", ""),
        ),
        patch("kew.outcomes.run", side_effect=reply) as remote,
    ):
        result = CliRunner().invoke(cli.app, command.split())
    saved = store.list_orphan_prs()
    store.close()
    return result, saved, remote


def test_round15_sync_requires_repository(tmp_path, monkeypatch):
    result, _, remote = _invoke_remote_command(
        tmp_path, monkeypatch, "report sync", repo=""
    )
    assert result.exit_code == 2, result.output
    assert "Sync repository unavailable" in result.output
    remote.assert_not_called()


@pytest.mark.parametrize("command", ["report sync", "status"])
@pytest.mark.parametrize(
    "reply,category",
    [
        (
            lambda *a, **k: subprocess.CompletedProcess([], 1, "", "private error"),
            "github_query_failed",
        ),
        (OSError("private error"), "query_unavailable"),
        (subprocess.SubprocessError("private error"), "query_unavailable"),
        (
            lambda *a, **k: subprocess.CompletedProcess([], 0, "invalid", ""),
            "invalid_response",
        ),
        (
            lambda *a, **k: subprocess.CompletedProcess([], 0, "[{}]", ""),
            "invalid_response",
        ),
    ],
)
def test_round15_orphan_query_failure_is_visible(
    tmp_path, monkeypatch, command, reply, category
):
    result, saved, remote = _invoke_remote_command(
        tmp_path, monkeypatch, command, reply=reply
    )
    assert result.exit_code == 2, result.output
    assert f"Orphan PR query failed: {category}" in result.output
    assert "private error" not in result.output
    assert "no orphan" not in result.output.lower()
    if command == "report sync":
        assert "synced 0, no PR 0, query failures 1" in result.output
    assert [row.pr_number for row in saved] == [7]
    assert remote.call_count == 1
    assert remote.call_args.args[0][:3] == ["gh", "pr", "list"]


def test_round15_successful_empty_orphan_query_clears_stale_rows(tmp_path, monkeypatch):
    result, saved, _ = _invoke_remote_command(
        tmp_path,
        monkeypatch,
        "report sync",
        reply=lambda *a, **k: subprocess.CompletedProcess([], 0, "[]", ""),
    )
    assert result.exit_code == 0, result.output
    assert "query failures 0" in result.output
    assert saved == []


def test_round15_orphan_ledger_failure_preserves_rows():
    config = DispatchConfig()
    config.project.repo = "o/r"
    store = MagicMock()
    store.list_all.side_effect = OSError("ledger unavailable")
    payload = '[{"number":7,"url":"u","headRefName":"agent/issue-7"}]'
    with patch(
        "kew.outcomes.run", return_value=subprocess.CompletedProcess([], 0, payload)
    ):
        result = reconcile_orphan_prs(store, config)
    assert result == _QueryFailure("ledger_unavailable")
    store.sync_orphan_prs.assert_not_called()


@pytest.mark.parametrize(
    "error",
    [
        OSError("spawn failed"),
        subprocess.SubprocessError("spawn failed"),
        GhQueryError("gh unavailable"),
        ValueError("gh unavailable"),
    ],
)
def test_round15_setup_preflight_cannot_run(tmp_path, monkeypatch, error):
    monkeypatch.chdir(tmp_path)
    config = DispatchConfig()
    config.project.repo = "o/r"
    with (
        patch("kew.cli.load_config", return_value=config),
        patch("kew.cli.preflight_tools"),
        patch("kew.cli._detect_repo"),
        patch("kew.labels.run", side_effect=error) as remote,
        patch("kew.security.generate_security_hook") as repair,
    ):
        result = CliRunner().invoke(cli.app, ["setup"])
    assert result.exit_code == 2, result.output
    assert "Setup label preflight unavailable" in result.output
    assert "Setup complete!" not in result.output
    assert str(error) not in result.output
    assert remote.call_count == 1
    assert remote.call_args.args[0][:3] == ["gh", "repo", "view"]
    repair.assert_not_called()


def test_round15_setup_failure_after_repair_keeps_one(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = DispatchConfig()
    config.project.repo = "o/r"
    replies = [
        subprocess.CompletedProcess([], 0, '{"name":"r"}', ""),
        subprocess.CompletedProcess([], 0, "", ""),
        OSError("spawn failed after a label repair"),
    ]
    with (
        patch("kew.cli.load_config", return_value=config),
        patch("kew.cli.preflight_tools"),
        patch("kew.cli._detect_repo"),
        patch("kew.labels.run", side_effect=replies) as remote,
    ):
        result = CliRunner().invoke(cli.app, ["setup"])
    assert result.exit_code == 1, result.output
    assert remote.call_count == 3
    assert remote.call_args_list[1].args[0][:3] == ["gh", "label", "create"]
    assert "Setup complete!" not in result.output
