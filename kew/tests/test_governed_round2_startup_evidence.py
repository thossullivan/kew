"""Governed review round 2 (2026-09-21) for PR #1943.

1. Every contained agent is exec'd through the entrypoint, so its acknowledgement
   is the startup evidence; a container or exec that merely started is not.
2. A cancellation journaled for a run whose admission rolled back is dropped
   instead of being replayed on every admission and tick forever.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from kew.run_store import SqliteRunStore
from kew.sandbox import (
    STARTUP_ACK_PREFIX,
    STARTUP_NONCE_ENV,
    build_worker_agent_exec_argv,
)

AGENT_ENTRYPOINT_PATH = "/usr/local/bin/kew-agent-entrypoint"


def test_governed_round2_every_worker_exec_runs_the_entrypoint():
    """The exec argv carries the nonce as an option pair and runs the
    entrypoint, so the acknowledgement line always exists. A direct agent exec
    would have no acknowledgement and no startup evidence at all, so this
    contract fails rather than letting one ship."""
    argv = build_worker_agent_exec_argv(
        runtime="docker", worker_id="a" * 64, command=["claude", "--print"]
    )
    assert argv[:3] == ["docker", "exec", "-e"]
    assert argv[3].startswith(f"{STARTUP_NONCE_ENV}=")
    assert argv[4] == "a" * 64
    # The command the daemon runs begins with the entrypoint, never the agent.
    assert argv[5] == AGENT_ENTRYPOINT_PATH
    assert argv[6:] == ["claude", "--print"]


class _Proc:
    def __init__(self, code: int | None = None) -> None:
        self._code = code

    def poll(self) -> int | None:
        return self._code


def _handle(tmp_path: Path, nonce: str = "receipt-nonce"):
    from kew.sandbox import ContainerHandle

    return ContainerHandle(
        _Proc(),
        "kew-42",
        "docker",
        checkout_dir=tmp_path,
        cidfile=None,
        container_id="c" * 64,
        launch_argv=build_worker_agent_exec_argv(
            runtime="docker", worker_id="c" * 64, command=["claude", "--print"]
        ),
        startup_nonce=nonce,
    )


def test_governed_round2_a_started_exec_is_not_agent_startup(tmp_path):
    """Round 2: the daemon exec_start half of this contract was unreachable, so
    it is gone; the acknowledgement is the evidence and nothing else latches."""
    handle = _handle(tmp_path)
    with patch("kew.subprocess_utils.run") as runtime_call:
        assert handle.observe_workload_start() is False
        # No daemon probe is made: a started exec proves the wrapper, not the
        # agent, so there is nothing to ask the runtime about.
        runtime_call.assert_not_called()
    assert handle.container_id == "c" * 64

    handle.observe_workload_output(f"{STARTUP_ACK_PREFIX}receipt-nonce\n")
    assert handle.observe_workload_start() is True


def test_governed_round2_a_cancellation_that_can_never_settle_stops_replaying(
    tmp_path,
):
    """A marker recorded before its run row commits settles on a later pass, so
    a replay that changes nothing is kept. Past the bound it is dropped however
    it became unsettleable -- the row never committed, or another actor
    terminalized it -- instead of costing every later admission and tick
    another transaction (governed review rounds 2 and 3, 2026-09-21)."""
    import os
    import time

    from kew.run_store_launches import CANCELLATION_REPLAY_LIMIT_SECONDS

    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        journal = store._cancellation_journal
        store.defer_cancelled_run("never-admitted", startup_state="pending")
        assert [row[0] for row in journal.pending()] == ["never-admitted"]

        # A fresh request is kept: its run row may still be committing.
        store.reap_cancelled_runs()
        assert [row[0] for row in journal.pending()] == ["never-admitted"]

        aged = time.time() - CANCELLATION_REPLAY_LIMIT_SECONDS - 1
        os.utime(journal._path("never-admitted"), (aged, aged))
        store.reap_cancelled_runs()

        assert list(journal.pending()) == []
        store.reap_cancelled_runs()
        assert list(journal.pending()) == []


def test_governed_round3_a_terminalized_run_also_stops_replaying(tmp_path):
    """A marker whose run row another actor terminalized settles nothing
    either: the run update matches only a pending row. Keeping it while the
    row merely exists replayed it forever (governed review round 3)."""
    import os
    import time
    from datetime import UTC, datetime

    from kew.run_store import RunRecord
    from kew.run_store_launches import CANCELLATION_REPLAY_LIMIT_SECONDS

    now = datetime.now(UTC)
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        journal = store._cancellation_journal
        store.save(
            RunRecord(
                run_id="terminalized",
                issue_num=42,
                item_id="42",
                branch="agent/issue-42",
                status="failure",
                exit_code=1,
                started_at=now,
                finished_at=now,
            )
        )
        # The first request settles the receipt and clears itself.
        store.defer_cancelled_run("terminalized", startup_state="refused")
        store.reap_cancelled_runs()
        assert list(journal.pending()) == []

        # A second request for the same run can change nothing: the row is not
        # pending and its receipt already carries this state.
        store.defer_cancelled_run("terminalized", startup_state="refused")
        store.reap_cancelled_runs()
        assert [row[0] for row in journal.pending()] == ["terminalized"]

        aged = time.time() - CANCELLATION_REPLAY_LIMIT_SECONDS - 1
        os.utime(journal._path("terminalized"), (aged, aged))
        store.reap_cancelled_runs()

        assert list(journal.pending()) == []
        assert store.get("terminalized").status == "failure"


def test_governed_round4_one_unreadable_request_never_stops_the_sweep(tmp_path):
    """pending() runs inside every admission and every driver tick, so one
    corrupt request file must not stop kew recovering the others (governed
    review round 4, 2026-09-21)."""
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        journal = store._cancellation_journal
        store.defer_cancelled_run("readable", startup_state="refused")
        (journal.directory / "corrupt.json").write_text("{not json")
        (journal.directory / "incomplete.json").write_text('{"run_id": "x"}')

        assert [run_id for run_id, _state in journal.pending()] == ["readable"]

        # The unreadable files stay for an operator to look at.
        assert (journal.directory / "corrupt.json").exists()
        assert (journal.directory / "incomplete.json").exists()
        store.reap_cancelled_runs()


def test_governed_round6_a_container_that_always_refuses_escalates(tmp_path):
    """A refusal releases its launch slot, so a transient one never spends the
    repair budget. A container that refuses every time would therefore repair
    forever, because the escalation counted launches only (governed review
    round 6, 2026-09-22)."""
    from datetime import UTC, datetime

    from kew.run_store import RunRecord

    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        now = datetime.now(UTC)
        for index in range(3):
            run_id = f"refused-{index}"
            store.save(
                RunRecord(
                    run_id=run_id,
                    issue_num=87,
                    item_id="87",
                    branch="agent/issue-87",
                    status="failure",
                    exit_code=2,
                    started_at=now,
                    finished_at=now,
                    repo="o/r",
                )
            )
            store.register_ci_fix_attempt(run_id, 87)
            store.defer_cancelled_run(run_id, startup_state="pending")
            store.reap_cancelled_runs()

        # None of them launched, so the launch budget is untouched: at a limit
        # of one, a single refusal is still followed by a real repair (round
        # 22). Refusals past that budget cannot be transient, so they escalate.
        assert store.count_launched_ci_fix_attempts("o/r", 87) == 0
        assert store.count_refused_ci_fix_attempts("o/r", 87) == 3


def test_governed_round6_an_absent_pr_never_erases_a_recorded_one(tmp_path):
    """A run's PR state only advances: a branch whose PR is not visible right
    now must not clear the url, review cycles and diff counters an OPEN
    outcome already carries (governed review round 6, 2026-09-22)."""
    from datetime import UTC, datetime
    from unittest.mock import patch

    from kew.config import DispatchConfig
    from kew.outcomes import sync_outcomes
    from kew.run_store import RunRecord

    now = datetime.now(UTC)
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        store.save(
            RunRecord(
                run_id="open-run",
                issue_num=7,
                item_id="7",
                branch="agent/issue-7",
                status="success",
                exit_code=0,
                started_at=now,
                finished_at=now,
                repo="o/r",
            )
        )
        store.set_outcome(
            "open-run",
            pr_url="https://example.invalid/pr/7",
            pr_state="OPEN",
            pr_merged_at=None,
            review_cycles=2,
            diff_additions=10,
            diff_deletions=3,
            diff_files=1,
        )
        with patch("kew.outcomes._fetch_pr_outcome") as fetch:
            from kew.outcomes import _AbsentPR

            fetch.return_value = _AbsentPR()
            config = DispatchConfig()
            config.project.repo = "o/r"
            sync_outcomes(store, config)

        run = store.get("open-run")
        assert run.pr_state == "OPEN"
        assert run.pr_url == "https://example.invalid/pr/7"
        assert run.review_cycles == 2
