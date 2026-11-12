"""Outcomes per prompt version: first-pass approval, rounds to READY, verdicts (#3026).

Each run of an implementer role whose prompt reached an agent is charged with
the review rounds that follow its launch on its item, until the item's next
such launch of the role, under the profile version the run recorded (null is
the built-in prompt), and, for one version only, the item's merge, priced at
the whole item's cost (every run on it, whatever its kind); a pre-launch
refusal counts for nothing. The reviewer's rounds count under the
version recorded on each round. Every active or retired
version is tabled, at zero when nothing ran under it. Seeded through the
store's own writers in the order they happen (runs start on the wall clock the
rounds are stamped with), read back through the service and
``GET /api/prompts/{role}/outcomes``.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from kew.audit import Actor
from kew.config import DispatchConfig
from kew.prompt_refusals import record_prompt_refusal
from kew.review_dispatch import REVIEWER_PROFILE_ROLE
from kew.run_store import RunRecord, SqliteRunStore
from kew.run_store_prompt_profiles import NOT_LAUNCHED_CATEGORIES
from kew.serve.app import create_app
from kew.serve.auth import SessionStore
from kew.service import prompt_profiles as profiles
from kew.service.prompts import get_role

ALICE = Actor("human", "alice")
EPIC = "epic:demo"
NC = "NEEDS_CHANGES"
IMPLEMENTER_ROLES = ["dispatch", "review_remediation", "resolve", "ci_fix"]


@pytest.fixture
def config() -> DispatchConfig:
    cfg = DispatchConfig()
    cfg.approval.approvers = ["alice"]
    return cfg


@pytest.fixture
def db(tmp_path: Path) -> str:
    return str(tmp_path / "dispatch.db")


@pytest.fixture
def store(db: str) -> Any:
    with SqliteRunStore(db) as s:
        yield s


def _approve(store: SqliteRunStore, config: DispatchConfig, role: str) -> int:
    """Propose and approve *role*'s next version; its row id."""
    row = profiles.propose(
        store, role=role, body=f"note {role}", config=config, actor=ALICE
    )
    profiles.approve(
        store,
        role=role,
        version=row.version,
        generation=profiles.current_generation(store, role),
        config=config,
        actor=ALICE,
    )
    return row.id


_runs = iter(range(1, 10_000))
_heads = iter(range(1, 10_000))


def _run(
    store: SqliteRunStore,
    item: str,
    version_id: int | None,
    *,
    kind: str = "dispatch",
    cost: float = 0.0,
    merged: bool = False,
    merged_at: datetime | None = None,
    at: datetime | None = None,
    status: str = "success",
    error_category: str = "",
    repo: str | None = None,
) -> str:
    """Save one run whose agent ran: it carries the session it launched.

    It starts *at*, or now on the clock record_review_round stamps rounds
    with, so a run seeded after a round started after it. *merged* marks the
    PR merged by state alone; *merged_at* also dates the merge, written as
    GitHub's ``mergedAt`` is. Returns its run id.
    """
    n = next(_runs)
    started = at or datetime.now(UTC)
    run_id = f"run-{n}"
    store.save(
        RunRecord(
            run_id=run_id,
            repo=repo,
            issue_num=0,
            item_id=item,
            branch=f"agent/{item}",
            status=status,
            exit_code=0 if status == "success" else 1,
            started_at=started,
            estimated_cost_usd=cost,
            error_category=error_category,
            session_id=f"session-{n}",
            run_kind=kind,
            pr_state="MERGED" if merged or merged_at else None,
            pr_merged_at=(
                merged_at.strftime("%Y-%m-%dT%H:%M:%SZ") if merged_at else None
            ),
            prompt_profile_version_id=version_id,
        )
    )
    return run_id


def _rounds(store: SqliteRunStore, item: str, *verdicts: str) -> None:
    for verdict in verdicts:
        head = f"{item}-{next(_heads)}"
        store.record_review_round(EPIC, item, head_sha=head, verdict=verdict)


def _seed_dispatch(store: SqliteRunStore, config: DispatchConfig) -> int:
    """Seed two dispatch versions and built-in items; v2's row id."""
    v1 = _approve(store, config, "dispatch")
    v2 = _approve(store, config, "dispatch")
    # Built-in: two items, both merged, one READY at round 1. b1's remediation
    # run does not end its dispatch's window: only another dispatch would.
    _run(store, "b1", None, cost=1.0, merged=True)
    _rounds(store, "b1", NC)
    _run(store, "b1", None, kind="review_remediation", cost=0.5)
    _rounds(store, "b1", "READY")
    _run(store, "b2", None, cost=3.0, merged=True)
    _rounds(store, "b2", "READY")
    # v1: four items, two READY at round 1, one at round 3, one never.
    for item, verdicts in {
        "a1": ("READY",),
        "a2": ("READY",),
        "a3": (NC, NC, "READY"),
        "a4": (NC,),
    }.items():
        _run(store, item, v1)
        _rounds(store, item, *verdicts)
    # v2: three items, two READY at round 1, one at round 2; one never reviewed.
    for item, verdicts in {
        "c1": ("READY",),
        "c2": (NC, "READY"),
        "c3": ("READY",),
        "c4": (),
    }.items():
        _run(store, item, v2)
        _rounds(store, item, *verdicts)
    # A run of another role's kind never puts an item in this table.
    _run(store, "r1", v2, kind="review_remediation")
    _rounds(store, "r1", NC)
    return v2


def _rows(dto: profiles.PromptOutcomesDTO) -> dict[str, dict[str, Any]]:
    return {row.label: row.model_dump() for row in dto.versions}


def test_dispatch_versions_give_their_first_pass_rates_and_mean_rounds(
    store: SqliteRunStore, config: DispatchConfig
) -> None:
    _seed_dispatch(store, config)

    dto = profiles.outcomes(store, "dispatch")

    assert dto.kind == "implementer"
    assert [row.label for row in dto.versions] == ["v2", "v1", "built-in"]
    rows = _rows(dto)
    assert rows["v2"]["state"] == "active"
    assert rows["v1"]["state"] == "retired"
    assert (rows["v1"]["n"], rows["v1"]["round_one_items"]) == (4, 4)
    assert rows["v1"]["first_pass_rate"] == pytest.approx(2 / 4)
    assert rows["v1"]["mean_rounds_to_ready"] == pytest.approx((1 + 1 + 3) / 3)
    assert (rows["v2"]["n"], rows["v2"]["round_one_items"]) == (4, 3)
    assert rows["v2"]["first_pass_rate"] == pytest.approx(2 / 3)
    assert rows["v2"]["mean_rounds_to_ready"] == pytest.approx((1 + 2 + 1) / 3)
    assert rows["v2"]["median_cost_usd"] is None


def test_the_null_version_is_reported_as_built_in(
    store: SqliteRunStore, config: DispatchConfig
) -> None:
    _seed_dispatch(store, config)

    built_in = _rows(profiles.outcomes(store, "dispatch"))["built-in"]

    assert built_in["version"] is None
    assert built_in["state"] is None
    assert built_in["n"] == 2
    assert built_in["first_pass_rate"] == pytest.approx(1 / 2)
    assert built_in["mean_rounds_to_ready"] == pytest.approx((2 + 1) / 2)
    # b1 cost 1.5 in all (its dispatch's 1.0 and its remediation run's 0.5),
    # b2 3.0: the median of 1.5 and 3.0.
    assert built_in["merged_items"] == 2
    assert built_in["median_cost_usd"] == pytest.approx(2.25)


def test_another_implementer_role_reads_only_its_own_run_kinds(
    store: SqliteRunStore, config: DispatchConfig
) -> None:
    dispatch_v2 = _seed_dispatch(store, config)

    rows = _rows(profiles.outcomes(store, "review_remediation"))

    # b1's remediation run is built-in; r1's names a dispatch row id, which
    # the remediation history does not hold, so it is never folded into built-in.
    # No remediation version is stored, so no row is tabled at zero.
    assert {label: row["n"] for label, row in rows.items()} == {
        f"profile #{dispatch_v2}": 1,
        "built-in": 1,
    }


@pytest.mark.parametrize("role", IMPLEMENTER_ROLES)
def test_each_version_is_charged_only_with_the_rounds_after_its_own_runs(
    store: SqliteRunStore, config: DispatchConfig, role: str
) -> None:
    kinds = get_role(role).run_kinds
    v1 = _approve(store, config, role)
    v2 = _approve(store, config, role)
    if role != "dispatch":
        _run(store, "w1", None, kind="dispatch")
    # Recorded before the role's first run on the item: no version's round.
    _rounds(store, "w1", NC)
    _run(store, "w1", v1, kind=kinds[0])
    _rounds(store, "w1", NC, NC)
    # The role's next run on the item (any of its kinds) closes v1's window.
    _run(store, "w1", v2, kind=kinds[-1])
    _rounds(store, "w1", "READY")

    dto = profiles.outcomes(store, role)

    assert [row.label for row in dto.versions] == ["v2", "v1", "built-in"]
    rows = _rows(dto)
    assert (rows["v1"]["n"], rows["v1"]["round_one_items"]) == (1, 1)
    assert rows["v1"]["first_pass_rate"] == pytest.approx(0.0)
    assert (rows["v1"]["ready_items"], rows["v1"]["mean_rounds_to_ready"]) == (
        0,
        None,
    )
    assert (rows["v2"]["n"], rows["v2"]["round_one_items"]) == (1, 1)
    assert rows["v2"]["first_pass_rate"] == pytest.approx(1.0)
    assert rows["v2"]["mean_rounds_to_ready"] == pytest.approx(1.0)
    assert (rows["built-in"]["n"], rows["built-in"]["round_one_items"]) == (0, 0)
    assert v1 != v2


def test_one_versions_runs_on_an_item_join_their_windows(
    store: SqliteRunStore,
) -> None:
    _run(store, "j1", None)
    _rounds(store, "j1", NC)
    _run(store, "j1", None, kind="loop")
    _rounds(store, "j1", "READY")

    (row,) = profiles.outcomes(store, "dispatch").versions

    assert (row.n, row.round_one_items, row.ready_items) == (1, 1, 1)
    assert row.first_pass_rate == pytest.approx(0.0)
    assert row.mean_rounds_to_ready == pytest.approx(2.0)


def test_a_round_accepted_with_residuals_counts_as_ready(
    store: SqliteRunStore,
) -> None:
    _run(store, "h1", None)
    store.record_review_round(EPIC, "h1", head_sha="h1-head", verdict=NC)
    assert store.accept_review_round(
        "h1",
        round_num=1,
        head_sha="h1-head",
        sidecar_digest=None,
        verdict="READY_WITH_RESIDUALS",
    )

    (row,) = profiles.outcomes(store, "dispatch").versions
    (reviewer,) = profiles.outcomes(store, REVIEWER_PROFILE_ROLE).versions

    assert (row.round_one_items, row.ready_items) == (1, 1)
    assert row.first_pass_rate == pytest.approx(1.0)
    assert row.mean_rounds_to_ready == pytest.approx(1.0)
    # The reviewer is still read by its own verdict, not the driver's acceptance.
    assert reviewer.verdicts == {NC: 1}


@pytest.mark.parametrize("role", ["dispatch", REVIEWER_PROFILE_ROLE])
def test_an_approved_version_with_nothing_recorded_has_a_zero_row(
    store: SqliteRunStore, config: DispatchConfig, role: str
) -> None:
    _approve(store, config, role)
    declined = profiles.propose(
        store, role=role, body="declined", config=config, actor=ALICE
    )
    profiles.decline(
        store, role=role, version=declined.version, config=config, actor=ALICE
    )
    profiles.propose(store, role=role, body="pending", config=config, actor=ALICE)

    dto = profiles.outcomes(store, role)

    # The proposed and the declined addenda never ran: no row for either.
    assert [row.label for row in dto.versions] == ["v1", "built-in"]
    rows = _rows(dto)
    assert (rows["v1"]["state"], rows["v1"]["n"]) == ("active", 0)
    assert rows["v1"]["comparable"] is False
    assert rows["built-in"]["n"] == 0
    if role == REVIEWER_PROFILE_ROLE:
        assert rows["v1"]["verdicts"] == {}
    else:
        assert rows["v1"]["round_one_items"] == 0
        assert rows["v1"]["first_pass_rate"] is None
        assert rows["v1"]["median_cost_usd"] is None


def _refuse_prompt_configuration(
    store: SqliteRunStore, config: DispatchConfig, item: str, version_id: int
) -> None:
    # The fix-up flows' writer (#3023): it records no profile version.
    record_prompt_refusal(
        store,
        config,
        run_id=f"refused-{next(_runs)}",
        run_kind="dispatch",
        issue_num=0,
        branch=f"agent/{item}",
        item_id=item,
        runner="claude",
    )


def _refuse_before_admission(
    store: SqliteRunStore, config: DispatchConfig, item: str, version_id: int
) -> None:
    # Dispatch's shared pre-admission writer, on a row naming the version.
    # Imported where dispatch imports it: the module loads after dispatch_core.
    import kew.dispatch_core  # noqa: F401
    from kew.dispatch_preflight import record_owned_refusal

    now = datetime.now(UTC)
    record_owned_refusal(
        store,
        RunRecord(
            run_id=f"refused-{next(_runs)}",
            issue_num=0,
            item_id=item,
            branch=f"agent/{item}",
            status="refused",
            exit_code=1,
            started_at=now,
            finished_at=now,
            error_category="budget_exceeded",
            run_kind="dispatch",
            prompt_profile_version_id=version_id,
        ),
        epic_ref=EPIC,
        expected_reservation_run_id=None,
    )


def _never_launch(
    store: SqliteRunStore, config: DispatchConfig, item: str, version_id: int
) -> None:
    # Unmarked, but no agent ever started: no handle, transcript, time or spend.
    store.save(
        RunRecord(
            run_id=f"unlaunched-{next(_runs)}",
            issue_num=0,
            item_id=item,
            branch=f"agent/{item}",
            status="failure",
            exit_code=1,
            started_at=datetime.now(UTC),
            run_kind="loop",
            prompt_profile_version_id=version_id,
        )
    )


@pytest.mark.parametrize(
    "refuse", [_refuse_prompt_configuration, _refuse_before_admission, _never_launch]
)
def test_prelaunch_refusals_do_not_count_or_close_review_windows(
    store: SqliteRunStore, config: DispatchConfig, refuse: Any
) -> None:
    v1 = _approve(store, config, "dispatch")
    v2 = _approve(store, config, "dispatch")
    _run(store, "p1", v1)
    _rounds(store, "p1", NC)
    refuse(store, config, "p1", v2)
    _rounds(store, "p1", "READY")
    _run(store, "p2", v1)
    refuse(store, config, "p3", v2)

    rows = _rows(profiles.outcomes(store, "dispatch"))

    # Neither p3, refused alone, nor p1's refusal is an item of any version.
    assert {label: row["n"] for label, row in rows.items()} == {
        "v2": 0,
        "v1": 2,
        "built-in": 0,
    }
    # And p1's READY stays in v1's window: two rounds to ready, not one.
    assert (rows["v1"]["round_one_items"], rows["v1"]["ready_items"]) == (1, 1)
    assert rows["v1"]["first_pass_rate"] == pytest.approx(0.0)
    assert rows["v1"]["mean_rounds_to_ready"] == pytest.approx(2.0)


def _rolled_back_claim(
    store: SqliteRunStore, item: str, version_id: int, category: str
) -> None:
    """The row an admitted claim that ended before launch leaves.

    Dispatch's pending row carries the owner's pgid and pid, the log path and
    the container name it allocated at admission; ``_rollback_admitted_claim``
    then saves it as a failure, and nothing ever started.
    """
    n = next(_runs)
    admitted = datetime.now(UTC)
    store.save(
        RunRecord(
            run_id=f"rolled-back-{n}",
            issue_num=0,
            item_id=item,
            branch=f"agent/{item}",
            status="failure",
            exit_code=1,
            started_at=admitted,
            finished_at=admitted,
            error_category=category,
            pgid=40_000 + n,
            owner_pid=40_000 + n,
            log_file=f"logs/rolled-back-{n}.log",
            container_name=f"kew-rolled-back-{n}",
            runner_name="claude",
            model="claude-opus-5-5",
            run_kind="dispatch",
            prompt_profile_version_id=version_id,
        )
    )


# "" is the same row with no category kew knows: the preallocated fields
# alone still prove no launch.
@pytest.mark.parametrize("category", ["dispatch_child_refused", ""])
def test_unlaunched_pending_row_with_preallocated_pgid_and_log_does_not_count_or_close_window(  # noqa: E501
    store: SqliteRunStore, config: DispatchConfig, category: str
) -> None:
    v1 = _approve(store, config, "dispatch")
    v2 = _approve(store, config, "dispatch")
    _run(store, "u1", v1)
    _rounds(store, "u1", NC)
    _rolled_back_claim(store, "u1", v2, category)
    _rounds(store, "u1", "READY")

    rows = _rows(profiles.outcomes(store, "dispatch"))

    assert (rows["v2"]["n"], rows["v1"]["n"]) == (0, 1)
    assert rows["v1"]["mean_rounds_to_ready"] == pytest.approx(2.0)
    assert rows["v1"]["first_pass_rate"] == pytest.approx(0.0)


def _zero_output_failure(store: SqliteRunStore, item: str, version_id: int) -> str:
    """A run whose agent started and died at once: a duration, nothing else."""
    n = next(_runs)
    started = datetime.now(UTC)
    run_id = f"crashed-{n}"
    store.save(
        RunRecord(
            run_id=run_id,
            issue_num=0,
            item_id=item,
            branch=f"agent/{item}",
            status="failure",
            exit_code=1,
            started_at=started,
            finished_at=started + timedelta(seconds=4),
            duration_seconds=4.0,
            error_category="exception",
            pgid=50_000 + n,
            log_file=f"logs/crashed-{n}.log",
            run_kind="dispatch",
            prompt_profile_version_id=version_id,
        )
    )
    return run_id


def test_started_zero_output_failure_counts_as_executed_and_closes_prior_window(
    store: SqliteRunStore, config: DispatchConfig
) -> None:
    v1 = _approve(store, config, "dispatch")
    v2 = _approve(store, config, "dispatch")
    _run(store, "z1", v1)
    _rounds(store, "z1", NC)
    crashed = _zero_output_failure(store, "z1", v2)
    # The host's launch boundary writes the receipt, as dispatch admission does.
    store.record_launch(crashed)
    _rounds(store, "z1", "READY")

    rows = _rows(profiles.outcomes(store, "dispatch"))

    assert (rows["v2"]["n"], rows["v2"]["round_one_items"]) == (1, 1)
    assert rows["v2"]["first_pass_rate"] == pytest.approx(1.0)
    # v1's window closed at the crash: its NC is its only round.
    assert (rows["v1"]["n"], rows["v1"]["round_one_items"]) == (1, 1)
    assert (rows["v1"]["ready_items"], rows["v1"]["first_pass_rate"]) == (0, 0.0)


def test_a_refused_launch_receipt_never_counts(
    store: SqliteRunStore, config: DispatchConfig
) -> None:
    # The receipt outranks the fallback fields: a refused startup is no
    # outcome even on a row that carries a session id. A CI fix's admission
    # takes a repair slot (a pending receipt) that a refused startup settles.
    v1 = _approve(store, config, "ci_fix")
    v2 = _approve(store, config, "ci_fix")
    _run(store, "y1", v1, kind="ci_fix", repo="acme/app")
    _rounds(store, "y1", NC)
    refused = _run(store, "y1", v2, kind="ci_fix", repo="acme/app", status="pending")
    store.register_ci_fix_attempt(refused, 7)
    store.refuse_launch(refused)
    store.save(replace(store.get(refused), status="failure", exit_code=1))
    _rounds(store, "y1", "READY")

    rows = _rows(profiles.outcomes(store, "ci_fix"))

    assert (rows["v2"]["n"], rows["v1"]["n"]) == (0, 1)
    assert rows["v1"]["mean_rounds_to_ready"] == pytest.approx(2.0)


def test_every_pre_launch_hold_category_is_read_as_not_launched() -> None:
    """The store's list covers the review driver's pre-launch table and the
    dispatch rollback's category, so a new one cannot count as an outcome."""
    from kew.graph.driver.review_stage import _PRE_LAUNCH_HOLDS

    assert set(_PRE_LAUNCH_HOLDS) | {"dispatch_child_refused"} <= (
        NOT_LAUNCHED_CATEGORIES
    )


def test_a_run_refused_after_its_agent_ran_still_counts(
    store: SqliteRunStore,
) -> None:
    # Publication refused the push, but the agent ran: its status decides nothing.
    _run(store, "q1", None, status="refused", error_category="publication_refused")
    _rounds(store, "q1", "READY")

    (row,) = profiles.outcomes(store, "dispatch").versions

    assert (row.n, row.round_one_items) == (1, 1)
    assert row.first_pass_rate == pytest.approx(1.0)


def test_one_item_two_versions_attributes_merge_and_cost_once(
    store: SqliteRunStore, config: DispatchConfig
) -> None:
    v1 = _approve(store, config, "dispatch")
    v2 = _approve(store, config, "dispatch")
    # m1: v1 spends 100.0, v2 spends 1.0 and the item merges after v2 ran,
    # known only by its PR state: the merge, and the item's 101.0, is v2's.
    _run(store, "m1", v1, cost=100.0)
    _rounds(store, "m1", NC)
    _run(store, "m1", v2, cost=1.0, merged=True)
    _rounds(store, "m1", "READY")
    # m2: v2 spends 5.0 and merges at a dated moment; a v1 run started after
    # the merge spends 7.0 and is credited no merge, but the item cost 12.0.
    t = datetime.now(UTC) - timedelta(hours=3)
    _run(store, "m2", v2, cost=5.0, at=t, merged_at=t + timedelta(hours=1))
    _run(store, "m2", v1, cost=7.0, at=t + timedelta(hours=2))

    rows = _rows(profiles.outcomes(store, "dispatch"))

    assert (rows["v2"]["n"], rows["v2"]["merged_items"]) == (2, 2)
    assert rows["v2"]["median_cost_usd"] == pytest.approx((101.0 + 12.0) / 2)
    assert (rows["v1"]["n"], rows["v1"]["merged_items"]) == (2, 0)
    assert rows["v1"]["median_cost_usd"] is None


def test_a_merged_items_cost_sums_every_run_kind_once(
    store: SqliteRunStore, config: DispatchConfig
) -> None:
    v1 = _approve(store, config, "dispatch")
    _run(store, "k1", v1, cost=1.0)
    _rounds(store, "k1", NC)
    _run(store, "k1", None, kind="review_remediation", cost=9.0)
    _rounds(store, "k1", "READY")
    _run(store, "k1", None, kind="ci_fix", cost=0.5, merged=True)

    dispatch = _rows(profiles.outcomes(store, "dispatch"))
    remediation = _rows(profiles.outcomes(store, "review_remediation"))

    assert dispatch["v1"]["merged_items"] == 1
    assert dispatch["v1"]["median_cost_usd"] == pytest.approx(10.5)
    # Another role's table reads the same whole-item cost.
    assert remediation["built-in"]["median_cost_usd"] == pytest.approx(10.5)


def test_a_window_starts_at_launch_not_admission(
    store: SqliteRunStore, config: DispatchConfig
) -> None:
    v1 = _approve(store, config, "dispatch")
    v2 = _approve(store, config, "dispatch")
    t = datetime.now(UTC) - timedelta(hours=3)
    # v1 ran (no receipt: its window starts at started_at); v2 is admitted
    # an hour later but its agent launches only after v1's READY round and
    # the item's merge, so neither is v2's.
    _run(store, "l1", v1, cost=1.0, at=t, merged_at=t + timedelta(hours=2))
    _rounds(store, "l1", NC)
    late = _run(store, "l1", v2, cost=9.0, at=t + timedelta(hours=1))
    _rounds(store, "l1", "READY")
    store.record_launch(late)
    _rounds(store, "l1", NC)

    rows = _rows(profiles.outcomes(store, "dispatch"))

    assert (rows["v1"]["first_pass_rate"], rows["v1"]["ready_items"]) == (0.0, 1)
    assert rows["v1"]["mean_rounds_to_ready"] == pytest.approx(2.0)
    assert rows["v1"]["merged_items"] == 1
    assert rows["v1"]["median_cost_usd"] == pytest.approx(10.0)
    assert (rows["v2"]["round_one_items"], rows["v2"]["ready_items"]) == (1, 0)
    assert rows["v2"]["first_pass_rate"] == pytest.approx(0.0)
    assert rows["v2"]["merged_items"] == 0


def test_a_retried_item_counts_its_superseded_rounds_to_ready(
    store: SqliteRunStore,
) -> None:
    # A retry restarts ``round`` at 1; the rounds before it still cost the item.
    _run(store, "t1", None)
    _rounds(store, "t1", NC, NC, NC)
    store.mark_epic_retry_item(EPIC, "t1")
    _rounds(store, "t1", "READY")

    (row,) = profiles.outcomes(store, "dispatch").versions

    assert row.first_pass_rate == pytest.approx(0.0)
    assert row.mean_rounds_to_ready == pytest.approx(4.0)


@pytest.mark.parametrize(("items", "comparable"), [(9, False), (10, True)])
def test_a_version_is_comparable_from_ten_items(
    store: SqliteRunStore, items: int, comparable: bool
) -> None:
    for i in range(items):
        _run(store, f"i{i}", None)

    (row,) = profiles.outcomes(store, "dispatch").versions

    assert (row.n, row.comparable) == (items, comparable)


def test_reviewer_outcomes_group_rounds_by_the_version_on_the_round(
    store: SqliteRunStore, config: DispatchConfig
) -> None:
    assert profiles.REVIEWER_ROLE == REVIEWER_PROFILE_ROLE
    v1 = _approve(store, config, REVIEWER_PROFILE_ROLE)
    for item, verdict, version_id in [
        ("x1", "READY", None),
        ("x2", "READY", None),
        ("x3", NC, None),
        ("x1", NC, v1),
        ("x2", NC, v1),
        ("x3", "READY", v1),
    ]:
        # The item's dispatch run names no version: the round's own does.
        _run(store, item, None)
        store.record_review_round(
            EPIC,
            item,
            head_sha=f"{item}-{verdict}",
            verdict=verdict,
            prompt_profile_version_id=version_id,
        )

    dto = profiles.outcomes(store, REVIEWER_PROFILE_ROLE)

    assert dto.kind == "reviewer"
    assert [row.label for row in dto.versions] == ["v1", "built-in"]
    rows = _rows(dto)
    assert rows["v1"]["verdicts"] == {NC: 2, "READY": 1}
    assert rows["built-in"]["verdicts"] == {NC: 1, "READY": 2}
    assert (rows["v1"]["n"], rows["v1"]["comparable"]) == (3, False)


class _InterleavingStore(SqliteRunStore):
    """A store that commits a decision and a launch right after the outcomes
    read takes a role's version names, before it takes the metrics: what a
    second connection approving v2 and dispatching under it would commit
    between those two queries. *times* is how many name reads are
    interrupted."""

    def __init__(
        self, db_path: str, config: DispatchConfig, role: str, times: int
    ) -> None:
        super().__init__(db_path)
        self._config = config
        self._role = role
        self._left = times
        self._busy = False

    def list_prompt_profile_versions(self, role: str) -> list[Any]:
        rows = super().list_prompt_profile_versions(role)
        if self._left and not self._busy:
            self._left -= 1
            self._busy = True
            try:
                pending = [r for r in rows if r.state == "proposed"]
                if pending:
                    version_id = pending[0].id
                    profiles.approve(
                        self,
                        role=self._role,
                        version=pending[0].version,
                        generation=profiles.current_generation(self, self._role),
                        config=self._config,
                        actor=ALICE,
                    )
                else:
                    version_id = _approve(self, self._config, self._role)
                _run(self, f"late-{version_id}", version_id)
            finally:
                self._busy = False
        return rows


def test_outcomes_stays_generation_consistent_when_approval_and_launch_interleave(
    db: str, config: DispatchConfig
) -> None:
    with _InterleavingStore(db, config, "dispatch", times=1) as store:
        _approve(store, config, "dispatch")
        profiles.propose(store, role="dispatch", body="v2", config=config, actor=ALICE)

        dto = profiles.outcomes(store, "dispatch")

        # The approval of v2 and its run landed between the names and the
        # metrics: the read is the view after both, never v2 proposed with a
        # run under it beside v1 still active.
        rows = _rows(dto)
        assert (rows["v2"]["state"], rows["v2"]["n"]) == ("active", 1)
        assert (rows["v1"]["state"], rows["v1"]["n"]) == ("retired", 0)
        assert dto.generation == profiles.current_generation(store, "dispatch")


def test_outcomes_that_never_settle_name_no_generation(
    db: str, config: DispatchConfig
) -> None:
    with _InterleavingStore(db, config, "dispatch", times=10) as store:
        _approve(store, config, "dispatch")

        dto = profiles.outcomes(store, "dispatch")

        # A decision landed inside every attempt: no generation the console
        # could match the table to.
        assert dto.generation is None


def test_outcomes_name_the_generation_their_versions_read_serves(
    store: SqliteRunStore, config: DispatchConfig
) -> None:
    assert profiles.outcomes(store, "dispatch").generation == 0
    _approve(store, config, "dispatch")
    assert profiles.outcomes(store, "dispatch").generation == 1


def test_an_empty_store_has_no_rows(store: SqliteRunStore) -> None:
    assert profiles.outcomes(store, "dispatch").versions == []
    assert profiles.outcomes(store, REVIEWER_PROFILE_ROLE).versions == []


@contextmanager
def _store_factory(db_path: str):
    store = SqliteRunStore(db_path)
    try:
        yield store
    finally:
        store.close()


@pytest.fixture
def client(tmp_path: Path, db: str, config: DispatchConfig, inbox_client) -> Any:
    app = create_app(
        lambda: _store_factory(db),
        config=config,
        repo_dir=tmp_path,
        token="TESTTOKEN",
        sessions=SessionStore(),
    )
    return inbox_client(app, loopback_peer=True)


def test_the_outcomes_route_serves_the_service_read(
    client: Any, store: SqliteRunStore, config: DispatchConfig
) -> None:
    _seed_dispatch(store, config)

    response = client.get("/api/prompts/dispatch/outcomes")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["role"] == "dispatch"
    assert body["min_items"] == 10
    assert body["generation"] == 2
    assert [row["label"] for row in body["versions"]] == ["v2", "v1", "built-in"]
    assert body["versions"][1]["first_pass_rate"] == pytest.approx(0.5)


@pytest.mark.parametrize("role", ["triage", "no-such-role"])
def test_the_outcomes_route_404s_a_role_no_profile_applies_to(
    client: Any, role: str
) -> None:
    assert client.get(f"/api/prompts/{role}/outcomes").status_code == 404
