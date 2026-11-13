"""A run started under a detached review stage's held slot fits under the cap.

#2628 makes the fleet cap count runs, so a pending ``review_stage`` row (#2459)
holds a slot of its own. The remediation that stage starts runs under that very
reservation; counting the stage row against it would mean that under a small
cap a review stage could never start its own work. These pin the exclusion at
the store and at both admission seams, and that it is the held slot alone that
is excused — an unrelated pending run of the same item still counts.
"""

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from test_dispatch_core import _profile_run

from kew.config import DispatchConfig
from kew.dispatch_core import WorkerCapReached, _save_pending_run
from kew.dispatch_sandbox import _record_sandbox_start
from kew.graph.driver.review_detach import _stage_row, holding_reservation
from kew.pseudo_epics import REVIEW_EPIC_REF
from kew.run_store import RunRecord, SqliteRunStore
from kew.run_store_liveness import DeadPending, count_admission_runs

STAGE = "stage-run"


def _held_stage(store, tmp_path, item_id="42"):
    """What ``launch_detached`` leaves behind: the reservation and its row."""
    config = DispatchConfig()
    config.logging.dir = str(tmp_path)
    assert store.reserve_epic_node_item(REVIEW_EPIC_REF, item_id, STAGE)
    store.save(_stage_row(item_id, STAGE, config))


def _run(run_id, item_id="42"):
    return RunRecord(
        run_id=run_id,
        issue_num=int(item_id) if item_id.isdigit() else 0,
        item_id=item_id,
        branch=f"agent/{item_id}",
        status="pending",
        exit_code=-1,
        started_at=datetime.now(UTC),
    )


def test_store_admission_excuses_only_the_held_slot(tmp_path):
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        _held_stage(store, tmp_path)
        # #2628 unchanged: without the held slot named, the stage row counts.
        assert store.try_admit_run(_run("plain"), 1, exclude_item="42") is False
        assert store.try_admit_run(
            _run("remediation"), 1, exclude_item="42", exclude_run=STAGE
        )
        # The held slot is excused, the remediation it started is not.
        assert (
            store.try_admit_run(
                _run("another"), 1, exclude_item="42", exclude_run=STAGE
            )
            is False
        )


def test_dead_proof_admission_excuses_the_held_slot(tmp_path):
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        _held_stage(store, tmp_path)
        store.save(_run("dead", item_id="SFD-9"))
        proof = DeadPending(store.get("dead"), reservation_run_id=None)
        assert count_admission_runs(store, "42", [proof]) == 1
        assert count_admission_runs(store, "42", [proof], exclude_run=STAGE) == 0


def test_host_remediation_admits_under_the_held_stage_at_cap_one(tmp_path):
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        _held_stage(store, tmp_path)
        run = _profile_run(store, tmp_path, run_id="remediation")
        with pytest.raises(WorkerCapReached):
            _save_pending_run(
                run, worker_cap=1, cap_exclude_issue=None, admit_budget_check=None
            )
        with holding_reservation(STAGE):
            _save_pending_run(
                run, worker_cap=1, cap_exclude_issue=None, admit_budget_check=None
            )
        assert store.get("remediation") is not None


def test_sandbox_remediation_admits_under_the_held_stage_at_cap_one(tmp_path):
    def admit(run_id):
        return _record_sandbox_start(
            store,
            run_id=run_id,
            issue_num=42,
            item_id="42",
            branch="agent/42",
            title="remediate",
            model="test",
            runner_name="claude-code",
            log_file=tmp_path / f"{run_id}.log",
            worker_cap=1,
        )

    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        _held_stage(store, tmp_path)
        with pytest.raises(WorkerCapReached):
            admit("outside")
        with holding_reservation(STAGE):
            assert admit("remediation") == "remediation"
        assert store.get("outside") is None


# --- #2697: a detached review holds ONE slot, not one per row ---------------


@pytest.fixture(autouse=True)
def _owners_alive(monkeypatch):
    """The fake owner pids below are not real processes: keep admission's
    orphan sweep from reaping them, as it would not reap a live child."""
    monkeypatch.setattr("kew.run_store_liveness._orphan_ready", lambda *_a: False)


_OWNER = {"pgid": 4242, "owner_pid": 4242, "owner_start_time": "1700000000.0"}


def _claimed_stage(store, tmp_path, item_id="42"):
    """The held stage after its child claimed the row (``_claim_ownership``)."""
    _held_stage(store, tmp_path, item_id)
    stage = store.get(STAGE)
    store.save(replace(stage, **_OWNER), update_ownership=True)


def _reviewer(run_id="reviewer", item_id="42", **owner):
    """The run the stage's child starts: same process, so same owner identity."""
    return replace(
        _run(run_id, item_id), run_kind="adversarial_review", **(owner or _OWNER)
    )


def _counts(store):
    """The cap count on every path: plain SQL, liveness-aware, dead-proof SQL."""
    return (
        store.live_run_count(),
        store.live_run_count(liveness=lambda _record: True),
        count_admission_runs(store, None, []),
    )


def test_a_review_with_its_reviewer_running_counts_one(tmp_path):
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        _claimed_stage(store, tmp_path)
        assert store.try_admit_run(_reviewer(), 1, exclude_item="42", exclude_run=STAGE)
        assert _counts(store) == (1, 1, 1)


def test_a_stage_that_has_not_started_its_reviewer_counts_one(tmp_path):
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        _claimed_stage(store, tmp_path)
        assert _counts(store) == (1, 1, 1)


def test_a_dispatch_beside_a_running_review_counts_one_more(tmp_path):
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        _claimed_stage(store, tmp_path)
        store.save(_reviewer())
        assert store.try_admit_run(_run("dispatch", item_id="7"), 2, exclude_item="7")
        assert _counts(store) == (2, 2, 2)
        # The cap still holds: a third piece of work does not fit under two.
        assert (
            store.try_admit_run(_run("third", item_id="8"), 2, exclude_item="8")
            is False
        )


@pytest.mark.parametrize(
    "owner",
    [
        {"pgid": 5151, "owner_pid": 5151, "owner_start_time": "1700000000.0"},
        {"pgid": 4242, "owner_pid": 4242, "owner_start_time": "1800000000.0"},
        {"pgid": 4242, "owner_pid": 4242, "owner_start_time": None},
    ],
    ids=["other-pid", "reused-pid", "no-start-time"],
)
def test_only_the_stages_own_child_shadows_it(tmp_path, owner):
    """Another process's run of the same item is not the stage's work."""
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        _claimed_stage(store, tmp_path)
        store.save(replace(_run("other"), run_kind="adversarial_review", **owner))
        assert _counts(store) == (2, 2, 2)


def test_an_unclaimed_stage_is_never_shadowed(tmp_path):
    """No owner recorded yet: nothing can prove a run is the stage's child."""
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        _held_stage(store, tmp_path)
        store.save(replace(_run("other"), run_kind="adversarial_review"))
        assert _counts(store) == (2, 2, 2)


def test_a_dead_reviewer_does_not_shadow_its_stage(tmp_path):
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        _claimed_stage(store, tmp_path)
        store.save(_reviewer())
        proof = DeadPending(store.get("reviewer"), reservation_run_id=None)
        assert count_admission_runs(store, None, [proof]) == 1


def test_a_finished_reviewer_does_not_shadow_its_stage(tmp_path):
    """The child moved on (or is between actions): the stage is its only row."""
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        _claimed_stage(store, tmp_path)
        store.save(replace(_reviewer(), status="success", exit_code=0))
        store.save(_run("dispatch", item_id="7"))
        assert _counts(store) == (2, 2, 2)
