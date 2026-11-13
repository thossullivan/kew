"""Every run kind kew defines meets the daily and weekly caps at launch (#3205).

A passed cap means kew starts no new paid run. ``LAUNCH_GATES`` maps each kind
in ``KNOWN_RUN_KINDS`` to the proof that its launch path refuses once a window
is past its cap; a kind added without one fails
:func:`test_every_run_kind_has_a_window_gated_launch_path` until its launch is
gated and listed here.

Fakes sit at the process boundary (``run_agent``, the ``gh``/``git`` processes
behind ``subprocess_utils``) and the budget clock; the store is real.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from kew.budget import BudgetExceeded, check_window_budget
from kew.config import AgentConfig, DispatchConfig, ProjectConfig, SandboxConfig
from kew.run_store import (
    ADVERSARIAL_REVIEW_RUN_KIND,
    KNOWN_RUN_KINDS,
    READINESS_PROBE_RUN_KIND,
    REVIEW_STAGE_RUN_KIND,
    RunRecord,
    SqliteRunStore,
)
from kew.tracking import DispatchResult

NOW = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)


def _process_succeeds(cmd, **kwargs) -> subprocess.CompletedProcess[str]:
    """Every allowlisted process exits 0 with no output: ``gh`` is authed and
    the repo reachable, and a PR lookup finds no PR."""
    return subprocess.CompletedProcess(cmd, 0, "", "")


class _BudgetClock(datetime):
    """The budget module's clock, pinned to ``NOW``."""

    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        return NOW if tz is None else NOW.astimezone(tz)


@pytest.fixture(autouse=True)
def _pinned_budget_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("kew.budget.datetime", _BudgetClock)


def _config(tmp_path: Path) -> DispatchConfig:
    config = DispatchConfig(
        project=ProjectConfig(repo="o/r"),
        agent=AgentConfig(model="claude-opus-5"),
        sandbox=SandboxConfig(enabled=False, dangerously_skip_containment=True),
    )
    config.logging.dir = str(tmp_path / "logs")
    config.retry.max_attempts = 1
    config.budget.daily_usd = 10.0
    return config


def _spend_past_the_daily_cap(store: SqliteRunStore) -> None:
    store.save(
        RunRecord(
            run_id="settled",
            issue_num=99,
            item_id="99",
            branch="agent/issue-99",
            status="success",
            exit_code=0,
            started_at=NOW,
            finished_at=NOW,
            estimated_cost_usd=12.5,
            run_kind="dispatch",
        )
    )


def _tracked_launch(
    run_kind: str,
    tmp_path: Path,
    store: SqliteRunStore,
    *,
    force_budget: bool = False,
) -> MagicMock:
    """Launch *run_kind* through ``run_tracked_agent``; return the fake process."""
    from kew.dispatch_core import run_tracked_agent

    runner = MagicMock()
    runner.name = "fake"
    runner.supports_resume.return_value = False
    spawn = MagicMock(
        return_value=DispatchResult(
            exit_code=0, issue_num=42, log_file=tmp_path / "x.log"
        )
    )
    with (
        patch("kew.dispatch_core.run_agent", spawn),
        patch("kew.dispatch_core.run", _process_succeeds),
    ):
        run_tracked_agent(
            prompt="p",
            worktree_dir=tmp_path,
            config=_config(tmp_path),
            issue_num=42,
            item_id="42",
            store=store,
            runner=runner,
            branch="agent/issue-42",
            model="test",
            run_kind=run_kind,
            run_id=f"run-{run_kind}",
            validate_continuation=False,
            force_budget=force_budget,
        )
    return spawn


def _refused_by_run_tracked_agent(
    run_kind: str, tmp_path: Path, store: SqliteRunStore
) -> None:
    with pytest.raises(BudgetExceeded) as refused:
        _tracked_launch(run_kind, tmp_path, store)
    assert refused.value.which_limit == "daily"
    assert store.get(f"run-{run_kind}") is None


def _refused_at_reviewer_admission(
    run_kind: str, tmp_path: Path, store: SqliteRunStore
) -> None:
    """The reviewer's pending row is admitted by the review launcher."""
    from kew.graph.driver.review_launcher import _record_pending

    ctx = SimpleNamespace(run_id="run-reviewer", item_id="42")
    with pytest.raises(BudgetExceeded) as refused:
        _record_pending(
            store, ctx, _config(tmp_path), tmp_path / "r.log", "test", "claude-code"
        )
    assert refused.value.which_limit == "daily"
    assert store.get("run-reviewer") is None


def _refused_before_the_probe_lease(
    run_kind: str, tmp_path: Path, store: SqliteRunStore
) -> None:
    from kew.readiness_probe import LiveStatus, confirm_route_live

    with (
        patch("kew.readiness_probe.subprocess_utils.run", _process_succeeds),
        patch("kew.readiness_probe.subprocess_utils.popen") as provider_call,
    ):
        result = confirm_route_live(
            store, _config(tmp_path), complexity="complex", now=NOW
        )
    assert result.status is LiveStatus.REFUSED
    assert result.detail.startswith("budget: daily cap $10.00 reached")
    provider_call.assert_not_called()
    assert store.list_readiness_probes() == []


def _pays_nothing_itself(run_kind: str, tmp_path: Path, store: SqliteRunStore) -> None:
    """A detached review stage's row is bookkeeping: the reviewer and the
    remediation it starts are gated under their own kinds, and its in-flight
    row reserves nothing in the windows."""
    store.save(
        RunRecord(
            run_id="stage",
            issue_num=42,
            item_id="42",
            branch="agent/issue-42",
            status="pending",
            exit_code=-1,
            started_at=NOW,
            run_kind=run_kind,
        )
    )
    config = _config(tmp_path)
    # Under the cap once the stage's row is not valued at a reservation:
    # $12.50 settled + one $5 launch would pass a $20 cap only if it were.
    config.budget.daily_usd = 20.0
    check_window_budget(
        store, config.budget, reservation_usd=5.0, count_admitting_run=True
    )
    _refused_at_reviewer_admission(ADVERSARIAL_REVIEW_RUN_KIND, tmp_path, store)


_Proof = Callable[[str, Path, SqliteRunStore], None]

#: Each run kind's launch path, and the proof it refuses past a cap. ``triage``
#: has no launch of its own yet; a triage run started through the shared
#: tracked-run core is refused like every other kind there.
LAUNCH_GATES: dict[str, _Proof] = {
    "dispatch": _refused_by_run_tracked_agent,
    "loop": _refused_by_run_tracked_agent,
    "delegation": _refused_by_run_tracked_agent,
    "review": _refused_by_run_tracked_agent,
    "planning": _refused_by_run_tracked_agent,
    "review_remediation": _refused_by_run_tracked_agent,
    "ci_fix": _refused_by_run_tracked_agent,
    "resolve": _refused_by_run_tracked_agent,
    "triage": _refused_by_run_tracked_agent,
    "acceptance_author": _refused_by_run_tracked_agent,
    ADVERSARIAL_REVIEW_RUN_KIND: _refused_at_reviewer_admission,
    READINESS_PROBE_RUN_KIND: _refused_before_the_probe_lease,
    REVIEW_STAGE_RUN_KIND: _pays_nothing_itself,
}


def test_every_run_kind_has_a_window_gated_launch_path() -> None:
    assert set(LAUNCH_GATES) == set(KNOWN_RUN_KINDS), (
        "a run kind with no daily/weekly cap proof: gate its launch with "
        "kew.budget.check_window_budget and list it in LAUNCH_GATES"
    )


@pytest.mark.parametrize("run_kind", sorted(KNOWN_RUN_KINDS))
def test_each_run_kind_is_refused_past_the_daily_cap(
    run_kind: str, tmp_path: Path
) -> None:
    proof = LAUNCH_GATES.get(run_kind)
    assert proof is not None, f"{run_kind!r} has no window-gated launch path"
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _spend_past_the_daily_cap(store)
        proof(run_kind, tmp_path, store)


def test_force_budget_still_launches_past_the_cap(tmp_path: Path) -> None:
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _spend_past_the_daily_cap(store)

        spawn = _tracked_launch("review", tmp_path, store, force_budget=True)

        spawn.assert_called_once()
        assert store.get("run-review") is not None


def test_a_refusal_at_admission_names_spend_and_others_reservations_apart(
    tmp_path: Path,
) -> None:
    """Inside the admission transaction the refused reviewer's own row is in
    the window; the hold reason still leaves its ceiling out."""
    from kew.budget import window_budget_hold_reason
    from kew.graph.driver.review_launcher import _record_pending

    config = _config(tmp_path)
    config.budget.daily_usd = 20.0
    config.review.max_review_usd = 10.0
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        store.save(
            RunRecord(
                run_id="settled",
                issue_num=99,
                item_id="99",
                branch="agent/issue-99",
                status="success",
                exit_code=0,
                started_at=NOW,
                finished_at=NOW,
                estimated_cost_usd=5.0,
                run_kind="dispatch",
            )
        )
        store.save(
            RunRecord(
                run_id="in-flight",
                issue_num=98,
                item_id="98",
                branch="agent/issue-98",
                status="pending",
                exit_code=-1,
                started_at=NOW,
                estimated_cost_usd=1.5,
                run_kind="dispatch",
            )
        )
        ctx = SimpleNamespace(run_id="run-reviewer", item_id="42")

        with pytest.raises(BudgetExceeded) as refused:
            _record_pending(store, ctx, config, tmp_path / "r.log", "test", "claude")

        assert window_budget_hold_reason(refused.value) == (
            "budget: daily cap $20.00 reached "
            "($6.50 spent, $8.50 reserved by running or starting runs)"
        )
        assert store.get("run-reviewer") is None


def test_a_launch_under_the_caps_is_admitted(tmp_path: Path) -> None:
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        spawn = _tracked_launch("loop", tmp_path, store)

        spawn.assert_called_once()
        assert store.get("run-loop") is not None
