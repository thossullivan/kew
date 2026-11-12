"""An operator's merge verify holds the PR head it tests (#3198).

The drive pushes nothing onto a head a live merge claim holds — no
remediation, merge-main step or ci-fix, and a run already going waits to
publish — so a merge no longer fails on a head the drive moved under its
verify. The claim lives one TTL past its last renewal: a running verify renews
it, a killed one frees the head within one TTL. Times are pinned clocks except
where the CLI reads the wall clock itself.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from support.driver_fakes import EPIC as FAKE_EPIC
from support.driver_fakes import FakeFactsReader, FakeSource, _node
from support.held_out_log import opened
from typer.testing import CliRunner

from kew import merge_claim as merge_claim_module
from kew import review as review_module
from kew.cli import app
from kew.config import (
    BudgetConfig,
    DispatchConfig,
    EpicConfig,
    LoggingConfig,
    ProjectConfig,
    ReviewConfig,
)
from kew.graph.build import build_graph
from kew.graph.domain import Node, NodeStatus
from kew.graph.driver.base_integration import (
    BaseIntegrationContext,
    BaseIntegrationOutcome,
)
from kew.graph.driver.ci_recovery import ci_recovery_pass
from kew.graph.driver.facts import NodeFacts
from kew.graph.driver.overlay import DriverStatus
from kew.graph.driver.review_detach import DetachedReviewSpawner
from kew.graph.driver.review_stage import (
    RemediationContext,
    ReviewStageOutcome,
    drive_review_stage,
)
from kew.graph.driver.tick import summarize
from kew.hosts import Job, LocalProvider
from kew.hosts.errors import HostTimeout
from kew.merge_claim import (
    ClaimHeartbeat,
    held_merge_claim,
    utc_now,
    wait_out_merge_claim,
)
from kew.merge_verify import VERIFY_EVENT
from kew.pseudo_epics import REVIEW_EPIC_REF
from kew.run_store import (
    CI_FIX_RUN_KIND,
    CURRENT_SCHEMA_VERSION,
    REVIEW_STAGE_RUN_KIND,
    RunRecord,
    SqliteRunStore,
)
from kew.run_store_merge_claims import (
    MERGE_CLAIM_RENEW_EVERY,
    MERGE_CLAIM_TTL,
    MergeClaimRefused,
)

EPIC = "epic:x"
ITEM = "42"
PR = 7
HEAD = "ab" * 20
NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)

runner = CliRunner()


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SqliteRunStore]:
    with SqliteRunStore(str(tmp_path / "d.db")) as opened_store:
        yield opened_store


# --- the claim table ---


def test_a_second_take_of_a_live_claim_is_refused_and_a_lapsed_one_replaced(
    store: SqliteRunStore,
) -> None:
    (claim,) = store.take_merge_claim(PR, (HEAD,), owner="verify-a", now=NOW)
    assert (claim.owner, claim.expires_at) == ("verify-a", NOW + MERGE_CLAIM_TTL)

    for owner in ("verify-b", "verify-a"):
        with pytest.raises(MergeClaimRefused, match="already claimed by verify-a"):
            store.take_merge_claim(PR, (HEAD,), owner=owner, now=NOW)
    # A take is all or nothing: one live head refuses the other with it.
    with pytest.raises(MergeClaimRefused):
        store.take_merge_claim(PR, ("cd" * 20, HEAD), owner="verify-b", now=NOW)
    assert [c.owner for c in store.list_merge_claims()] == ["verify-a"]

    lapsed = NOW + MERGE_CLAIM_TTL
    assert store.live_merge_claim(PR, HEAD, now=lapsed) is None
    (retaken,) = store.take_merge_claim(PR, (HEAD,), owner="verify-b", now=lapsed)
    assert retaken.owner == "verify-b"
    assert [c.owner for c in store.list_merge_claims()] == ["verify-b"]


def test_renew_and_release_act_only_for_the_owner_of_a_live_claim(
    store: SqliteRunStore,
) -> None:
    store.take_merge_claim(PR, (HEAD,), owner="verify-a", now=NOW)
    later = NOW + MERGE_CLAIM_RENEW_EVERY

    assert not store.renew_merge_claim(PR, (HEAD,), owner="verify-b", now=later)
    assert store.release_merge_claim(PR, (HEAD,), owner="verify-b") == 0
    assert store.renew_merge_claim(PR, (HEAD,), owner="verify-a", now=later)
    live = store.live_merge_claim(PR, HEAD, now=later)
    assert live is not None and live.expires_at == later + MERGE_CLAIM_TTL

    # A lapsed claim is never revived: the drive may have acted in the gap.
    gone = later + MERGE_CLAIM_TTL
    assert not store.renew_merge_claim(PR, (HEAD,), owner="verify-a", now=gone)
    assert store.release_merge_claim(PR, (HEAD,), owner="verify-a") == 1
    assert store.list_merge_claims() == []


def test_a_take_drops_the_prs_lapsed_claims(store: SqliteRunStore) -> None:
    other = "cd" * 20
    store.take_merge_claim(PR, (HEAD,), owner="verify-a", now=NOW)
    store.take_merge_claim(PR + 1, (HEAD,), owner="verify-a", now=NOW)
    later = NOW + MERGE_CLAIM_TTL

    store.take_merge_claim(PR, (other,), owner="verify-b", now=later)

    # PR's lapsed head went with the take; another PR's row is not its business.
    assert [(c.pr, c.head) for c in store.list_merge_claims()] == [
        (PR + 1, HEAD),
        (PR, other),
    ]


def _v75_database(path: str) -> None:
    """A ledger exactly as main's v75 left it, holding one run.

    v76 creates the one ``merge_claims`` table and touches nothing else, so a
    current ledger with that table dropped and its version set back is the v75
    shape — built with plain SQL, not with a kew told to skip a step.
    """
    with SqliteRunStore(path) as current:
        current.save(
            RunRecord(
                run_id="old-run",
                issue_num=7,
                branch="agent/issue-7",
                status="success",
                exit_code=0,
                started_at=NOW,
            )
        )
    conn = sqlite3.connect(path)
    with conn:
        conn.execute("DROP TABLE merge_claims")
        conn.execute("UPDATE schema_version SET version = 75 WHERE id = 1")
    conn.close()


def test_the_migration_upgrades_a_v75_database_with_its_rows_intact(
    tmp_path: Path,
) -> None:
    path = str(tmp_path / "migration.db")
    _v75_database(path)
    for stale in tmp_path.glob("migration.db.bak-*"):
        stale.unlink()

    with SqliteRunStore(path) as migrated:
        version = migrated._conn.execute("SELECT version FROM schema_version")
        assert version.fetchone()[0] == CURRENT_SCHEMA_VERSION == 77
        saved = migrated.get("old-run")
        assert saved is not None and saved.branch == "agent/issue-7"
        assert migrated.list_merge_claims() == []
        migrated.take_merge_claim(PR, (HEAD,), owner="verify-a", now=NOW)
        assert migrated.live_merge_claim(PR, HEAD, now=NOW) is not None
    assert list(tmp_path.glob("migration.db.bak-pre-v77-*"))


# --- the drive ---


def _facts(merge_state_status: str | None = None) -> NodeFacts:
    return NodeFacts(
        issue_num=int(ITEM),
        state="OPEN",
        state_reason=None,
        labels=frozenset(),
        pr_number=PR,
        pr_url=None,
        review_decision=None,
        merge_state_status=merge_state_status,
        head_oid=HEAD,
        pr_pushed_at=None,
    )


def _no_reviewer(ctx: Any) -> Any:
    raise AssertionError("a standing NEEDS_CHANGES round needs no reviewer")


class _Remediator:
    def __init__(self) -> None:
        self.launched: list[RemediationContext] = []

    def __call__(self, ctx: RemediationContext) -> None:
        self.launched.append(ctx)


def _tick(
    store: SqliteRunStore,
    tmp_path: Path,
    remediator: _Remediator,
    now: datetime,
    *,
    status: DriverStatus = DriverStatus.REMEDIATING,
    facts: NodeFacts | None = None,
    base_integrator: Any = None,
) -> ReviewStageOutcome:
    config = DispatchConfig(
        review=ReviewConfig(adversarial=True, blocking=True, max_rounds=2),
        logging=LoggingConfig(dir=str(tmp_path / "logs")),
    )
    return drive_review_stage(
        {ITEM: status},
        {ITEM: facts or _facts()},
        epic_ref=EPIC,
        config=config,
        store=store,
        repo="o/r",
        repo_root=tmp_path,
        base="main",
        reviewer_launcher=_no_reviewer,
        remediator=remediator,
        base_integrator=base_integrator,
        now=now,
    )


def _needs_changes(store: SqliteRunStore) -> None:
    store.record_review_round(
        EPIC, ITEM, head_sha=HEAD, verdict="NEEDS_CHANGES", sidecar_digest="d"
    )


def test_a_live_claim_holds_back_remediation_until_it_expires(
    store: SqliteRunStore, tmp_path: Path
) -> None:
    _needs_changes(store)
    store.take_merge_claim(PR, (HEAD,), owner="kew verify alice:1", now=NOW)
    remediator = _Remediator()

    held = _tick(store, tmp_path, remediator, NOW + timedelta(minutes=1))

    assert remediator.launched == []
    assert held.remediating == []
    assert held.held == []
    assert held.refused[ITEM].startswith("merge_claimed — kew verify alice:1")
    # The findings stay recorded: the round is the NEEDS_CHANGES verdict.
    latest = store.latest_review_round(ITEM)
    assert latest is not None and latest.verdict == "NEEDS_CHANGES"
    assert store.review_round_count(ITEM) == 1

    freed = _tick(store, tmp_path, remediator, NOW + MERGE_CLAIM_TTL)

    assert [ctx.head_sha for ctx in remediator.launched] == [HEAD]
    assert freed.remediating == [ITEM]
    assert ITEM not in freed.refused


def test_a_claim_on_another_head_or_pr_holds_nothing_back(
    store: SqliteRunStore, tmp_path: Path
) -> None:
    _needs_changes(store)
    store.take_merge_claim(PR, ("cd" * 20,), owner="verify-a", now=NOW)
    store.take_merge_claim(PR + 1, (HEAD,), owner="verify-a", now=NOW)
    remediator = _Remediator()

    _tick(store, tmp_path, remediator, NOW)

    assert len(remediator.launched) == 1


class _Integrator:
    """The merge-main seam: in production ``kew resolve`` pushes a merge
    commit onto the PR head."""

    def __init__(self) -> None:
        self.ran_on: list[str] = []

    def __call__(self, ctx: BaseIntegrationContext) -> BaseIntegrationOutcome:
        self.ran_on.append(ctx.head_sha)
        return BaseIntegrationOutcome("integrated")


@pytest.mark.parametrize(
    "status", [DriverStatus.IN_REVIEW, DriverStatus.REMEDIATING], ids=str
)
def test_a_live_claim_holds_back_the_merge_main_step_until_released(
    store: SqliteRunStore, tmp_path: Path, status: DriverStatus
) -> None:
    if status is DriverStatus.REMEDIATING:
        _needs_changes(store)
    store.take_merge_claim(PR, (HEAD,), owner="kew verify alice:1", now=NOW)
    integrator = _Integrator()
    remediator = _Remediator()
    dirty = _facts(merge_state_status="DIRTY")

    held = _tick(
        store,
        tmp_path,
        remediator,
        NOW + timedelta(minutes=1),
        status=status,
        facts=dirty,
        base_integrator=integrator,
    )

    assert integrator.ran_on == []
    assert remediator.launched == []
    assert held.held == []
    assert held.refused[ITEM].startswith("merge_claimed — kew verify alice:1")
    assert "merge-main step" in held.refused[ITEM]

    store.release_merge_claim(PR, (HEAD,), owner="kew verify alice:1")
    freed = _tick(
        store,
        tmp_path,
        remediator,
        NOW + timedelta(minutes=2),
        status=status,
        facts=dirty,
        base_integrator=integrator,
    )

    assert integrator.ran_on == [HEAD]
    assert ITEM not in freed.refused


def test_a_live_claim_holds_back_a_ci_fix(
    store: SqliteRunStore, tmp_path: Path
) -> None:
    store.take_merge_claim(PR, (HEAD,), owner="kew verify alice:1", now=NOW)
    classified: list[NodeFacts] = []
    dispatched: list[NodeFacts] = []

    def classify(facts: NodeFacts) -> Any:
        classified.append(facts)
        return "code_failure"

    def dispatch_fix(facts: NodeFacts) -> bool:
        dispatched.append(facts)
        return True

    def recover(now: datetime) -> Any:
        return ci_recovery_pass(
            [ITEM],
            graph=build_graph(FakeSource(nodes=[_node(ITEM)], edges=[]), FAKE_EPIC),
            node_facts={ITEM: _facts()},
            config=DispatchConfig(
                epic=EpicConfig(auto_ci_fix=True),
                budget=BudgetConfig(per_issue_usd=1000.0),
                logging=LoggingConfig(dir=str(tmp_path / "logs")),
            ),
            store=store,
            now=now,
            classify=classify,
            dispatch_fix=dispatch_fix,
        )

    held = recover(NOW + timedelta(minutes=1))

    assert (classified, dispatched, held.dispatched) == ([], [], [])
    assert held.claimed[ITEM].startswith("merge_claimed — kew verify alice:1")

    freed = recover(NOW + MERGE_CLAIM_TTL)

    assert freed.dispatched == [ITEM] and freed.claimed == {}


class _Child:
    def poll(self) -> None:
        return None


def _review_config(tmp_path: Path, *, auto_ci_fix: bool = False) -> DispatchConfig:
    return DispatchConfig(
        review=ReviewConfig(
            adversarial=True,
            blocking=True,
            max_rounds=2,
            publish_status=False,
            publish_comment=False,
        ),
        epic=EpicConfig(auto_ci_fix=auto_ci_fix),
        logging=LoggingConfig(dir=str(tmp_path / "logs")),
    )


def _status(
    store: SqliteRunStore, config: DispatchConfig, facts: NodeFacts, now: datetime
) -> Any:
    """What ``kew epic status`` reports for the one node: its waiting line."""
    summary = summarize(
        EPIC,
        build_graph(
            FakeSource(
                nodes=[Node(id=ITEM, title="t", status=NodeStatus.TODO, epic_ref=EPIC)],
                edges=[],
            ),
            EPIC,
        ),
        config=config,
        store=store,
        facts=FakeFactsReader(issue_facts_by_num={int(ITEM): facts}),
        now=now,
    )
    return {w.node_id: w.reason for w in summary.waiting}.get(ITEM)


@pytest.mark.parametrize(
    ("status", "merge_state", "action"),
    [
        pytest.param(DriverStatus.REMEDIATING, None, "remediation", id="remediation"),
        pytest.param(DriverStatus.IN_REVIEW, "DIRTY", "merge-main step", id="merge"),
    ],
)
def test_a_detached_stage_starts_nothing_while_a_claim_holds_its_push(
    store: SqliteRunStore,
    tmp_path: Path,
    status: DriverStatus,
    merge_state: str | None,
    action: str,
) -> None:
    """The production path: with review on, the stage always runs detached."""
    if status is DriverStatus.REMEDIATING:
        _needs_changes(store)
    store.take_merge_claim(PR, (HEAD,), owner="kew verify alice:1", now=NOW)
    config = _review_config(tmp_path)
    facts = _facts(merge_state_status=merge_state)
    spawned: list[list[str]] = []

    def popen(cmd: list[str], **_kwargs: object) -> _Child:
        spawned.append(cmd)
        return _Child()

    spawner = DetachedReviewSpawner(config, base="main", popen=popen)

    def tick(now: datetime) -> ReviewStageOutcome:
        return drive_review_stage(
            {ITEM: status},
            {ITEM: facts},
            epic_ref=EPIC,
            config=config,
            store=store,
            repo="o/r",
            repo_root=tmp_path,
            base="main",
            reviewer_launcher=None,
            now=now,
            detacher=spawner,
            max_concurrent=2,
            worker_cap=4,
        )

    outcomes = [tick(NOW + timedelta(minutes=m)) for m in (1, 2, 3)]

    assert spawned == []
    assert store.list_epic_reservations(REVIEW_EPIC_REF) == []
    assert [
        r for r in store.list_by_item(ITEM) if r.run_kind == REVIEW_STAGE_RUN_KIND
    ] == []
    for outcome in outcomes:
        assert outcome.detached == {}
        assert outcome.refused[ITEM].startswith("merge_claimed — kew verify alice:1")
        assert f"the {action} runs once" in outcome.refused[ITEM]
    reason = _status(store, config, facts, NOW + timedelta(minutes=3))
    assert reason is not None and reason.startswith("merge_claimed — kew verify")

    store.release_merge_claim(PR, (HEAD,), owner="kew verify alice:1")
    freed = tick(NOW + timedelta(minutes=4))

    assert len(spawned) == 1 and ITEM in freed.detached
    assert ITEM not in freed.refused


def test_a_claimed_head_is_still_reviewed(
    store: SqliteRunStore, tmp_path: Path
) -> None:
    """A reviewer round pushes nothing, so the claim does not hold it back."""
    store.take_merge_claim(PR, (HEAD,), owner="kew verify alice:1", now=NOW)
    spawned: list[list[str]] = []

    def popen(cmd: list[str], **_kwargs: object) -> _Child:
        spawned.append(cmd)
        return _Child()

    outcome = drive_review_stage(
        {ITEM: DriverStatus.ADVERSARIAL_REVIEW},
        {ITEM: _facts()},
        epic_ref=EPIC,
        config=_review_config(tmp_path),
        store=store,
        repo="o/r",
        repo_root=tmp_path,
        base="main",
        reviewer_launcher=None,
        now=NOW + timedelta(minutes=1),
        detacher=DetachedReviewSpawner(
            _review_config(tmp_path), base="main", popen=popen
        ),
        max_concurrent=2,
        worker_cap=4,
    )

    assert len(spawned) == 1 and ITEM in outcome.detached


def test_status_names_a_ci_fix_a_claim_holds_back(
    store: SqliteRunStore, tmp_path: Path
) -> None:
    """Not the bare ``ci_pending`` hold the node sits on."""
    store.hold_epic_node_item(EPIC, ITEM, "ci_pending")
    config = _review_config(tmp_path, auto_ci_fix=True)
    assert _status(store, config, _facts(), NOW) == "ci_pending"

    store.take_merge_claim(PR, (HEAD,), owner="kew verify alice:1", now=NOW)
    reason = _status(store, config, _facts(), NOW + timedelta(minutes=1))

    assert reason is not None and reason.startswith("merge_claimed — kew verify")
    assert "the ci-fix runs once" in reason


def test_a_ci_fix_that_finds_the_head_claimed_publishes_nothing_and_returns(
    store: SqliteRunStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """It runs inside the tick: it defers, never waits, and keeps its worktree."""
    from kew import ci_fix
    from kew.tracking import DispatchResult

    store.take_merge_claim(PR, (HEAD,), owner="kew verify alice:1", now=utc_now())
    pushed: list[Any] = []
    cleaned: list[Any] = []
    worktree = tmp_path / "worktree"
    pr_meta = {
        "number": PR,
        "headRefName": "agent/issue-42",
        "baseRefName": "main",
        "body": "Closes #42",
        "title": "Fix widget",
        "headRefOid": HEAD,
        "labels": [{"name": "agent-pr"}],
    }
    # The forge, the checkout, the runner and the push: the run's boundaries.
    monkeypatch.setattr(ci_fix, "project_root", lambda: tmp_path)

    def gh(argv: list[str], **_kwargs: Any) -> Any:
        if argv[:3] == ["gh", "run", "view"] and "--log-failed" in argv:
            out = "FAILED test_foo\n"
        elif argv[:3] == ["gh", "run", "view"]:
            out = json.dumps({"headBranch": "agent/issue-42"})
        elif argv[:3] == ["gh", "pr", "view"]:
            out = json.dumps(pr_meta)
        else:
            pytest.fail(f"unexpected command {argv}")
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    monkeypatch.setattr(ci_fix, "run", gh)
    monkeypatch.setattr(ci_fix, "update_pr_labels", lambda *_a, **_k: True)
    monkeypatch.setattr(
        ci_fix, "checkout_existing_worktree", lambda **_k: (worktree, None)
    )
    monkeypatch.setattr(ci_fix, "require_pinned_pr_head", lambda *_a: None)
    monkeypatch.setattr(
        ci_fix,
        "run_tracked_agent",
        lambda *_a, **_k: DispatchResult(
            exit_code=0, issue_num=int(ITEM), log_file=tmp_path / "fix.log"
        ),
    )
    monkeypatch.setattr(ci_fix, "cleanup_worktree", lambda *a: cleaned.append(a))
    monkeypatch.setattr(
        ci_fix, "publish_existing_pr_branch", lambda *a, **k: pushed.append(a)
    )
    monkeypatch.setattr(
        merge_claim_module,
        "time",
        SimpleNamespace(sleep=lambda _s: pytest.fail("a ci-fix must not wait")),
    )
    config = DispatchConfig(
        project=ProjectConfig(repo="o/r"),
        logging=LoggingConfig(dir=str(tmp_path / "logs")),
    )

    result = ci_fix.dispatch_ci_fix(run_id="123", config=config, store=store)

    assert result == 1
    assert pushed == [] and cleaned == []
    said = capsys.readouterr()
    assert "CI fix not published: kew verify alice:1 claims PR #7" in (
        said.out + said.err
    )
    assert str(worktree) in said.out + said.err


def _opener(path: Path) -> Any:
    return lambda: SqliteRunStore(str(path))


def test_a_renewing_verify_keeps_its_claim_for_four_ttls(
    store: SqliteRunStore, tmp_path: Path
) -> None:
    _needs_changes(store)
    clock = [NOW]
    store.take_merge_claim(PR, (HEAD,), owner="verify-a", now=clock[0])
    heartbeat = ClaimHeartbeat(
        _opener(tmp_path / "d.db"),
        PR,
        (HEAD,),
        owner="verify-a",
        clock=lambda: clock[0],
    )
    remediator = _Remediator()

    while clock[0] < NOW + 4 * MERGE_CLAIM_TTL:
        clock[0] += MERGE_CLAIM_RENEW_EVERY
        assert heartbeat.tick()
        # Just before the next renewal is due, the claim still holds.
        almost = clock[0] + MERGE_CLAIM_RENEW_EVERY - timedelta(seconds=1)
        assert store.live_merge_claim(PR, HEAD, now=almost) is not None
        _tick(store, tmp_path, remediator, almost)

    assert remediator.launched == []
    assert not heartbeat.lost


def test_a_killed_verify_frees_the_head_one_ttl_after_its_last_renewal(
    store: SqliteRunStore, tmp_path: Path
) -> None:
    _needs_changes(store)
    clock = [NOW]
    store.take_merge_claim(PR, (HEAD,), owner="verify-a", now=clock[0])
    heartbeat = ClaimHeartbeat(
        _opener(tmp_path / "d.db"),
        PR,
        (HEAD,),
        owner="verify-a",
        clock=lambda: clock[0],
    )
    for _ in range(3):
        clock[0] += MERGE_CLAIM_RENEW_EVERY
        assert heartbeat.tick()
    last_renewal = clock[0]
    remediator = _Remediator()

    # The verify is killed here: nothing renews, nothing releases.
    _tick(
        store,
        tmp_path,
        remediator,
        last_renewal + MERGE_CLAIM_TTL - timedelta(seconds=1),
    )
    assert remediator.launched == []

    _tick(store, tmp_path, remediator, last_renewal + MERGE_CLAIM_TTL)
    assert len(remediator.launched) == 1

    # A verify that was only paused finds its claim gone, and says so.
    clock[0] = last_renewal + MERGE_CLAIM_TTL
    assert not heartbeat.tick()
    assert heartbeat.lost


def test_the_heartbeat_thread_renews_until_stopped(tmp_path: Path) -> None:
    path = tmp_path / "d.db"
    renewed_at = NOW + MERGE_CLAIM_RENEW_EVERY
    with SqliteRunStore(str(path)) as store:
        store.take_merge_claim(PR, (HEAD,), owner="verify-a", now=NOW)
    renewed = threading.Event()

    def opener() -> Any:
        renewed.set()
        return SqliteRunStore(str(path))

    heartbeat = ClaimHeartbeat(
        opener,
        PR,
        (HEAD,),
        owner="verify-a",
        clock=lambda: renewed_at,
        every=timedelta(0),
    )
    heartbeat.start()
    assert renewed.wait(timeout=30)
    heartbeat.stop()

    with SqliteRunStore(str(path)) as store:
        (claim,) = store.list_merge_claims()
    assert claim.expires_at == renewed_at + MERGE_CLAIM_TTL


def test_a_claim_the_wall_clock_outran_reads_as_lapsed(tmp_path: Path) -> None:
    """A Mac asleep past one TTL: the heartbeat's monotonic wait never woke,
    and the claim expired by the wall clock all the same."""
    path = tmp_path / "d.db"
    clock = [NOW]
    with SqliteRunStore(str(path)) as store:
        with held_merge_claim(
            store, _opener(path), PR, (HEAD,), owner="verify-a", clock=lambda: clock[0]
        ) as slept:
            assert not slept.lapsed()
            clock[0] = NOW + MERGE_CLAIM_TTL
        with held_merge_claim(
            store, _opener(path), PR, (HEAD,), owner="verify-b", clock=lambda: clock[0]
        ) as awake:
            clock[0] += MERGE_CLAIM_RENEW_EVERY
        assert store.list_merge_claims() == []

    # Read again after the release, each answer stands: a released claim is
    # not a lapsed one.
    assert slept.lapsed()
    assert not awake.lapsed()


class _ReleaseFails(SqliteRunStore):
    """A ledger that refuses the release (locked, say) — the store boundary."""

    def release_merge_claim(self, pr: int, heads: Any, *, owner: str) -> int:
        raise sqlite3.OperationalError("database is locked")


def test_a_release_that_fails_never_hides_the_verifys_own_result(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "d.db"
    with _ReleaseFails(str(path)) as store:
        with (
            pytest.raises(ValueError, match="the gate failed"),
            held_merge_claim(
                store, _opener(path), PR, (HEAD,), owner="verify-a", clock=lambda: NOW
            ),
        ):
            raise ValueError("the gate failed")
        with held_merge_claim(
            store, _opener(path), PR + 1, (HEAD,), owner="verify-a", clock=lambda: NOW
        ) as held:
            pass

    assert held.pr == PR + 1
    assert caplog.text.count("could not be released") == 2


# --- a run already going when the claim was taken ---


def test_a_publish_waits_out_a_claim_and_then_goes_ahead(
    store: SqliteRunStore,
) -> None:
    store.take_merge_claim(PR, (HEAD,), owner="kew verify alice:1", now=NOW)
    clock = [NOW + timedelta(minutes=1)]
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock[0] += timedelta(seconds=seconds)
        if len(slept) == 2:
            store.release_merge_claim(PR, (HEAD,), owner="kew verify alice:1")

    waited = wait_out_merge_claim(
        store, PR, HEAD, flow="review remediation", clock=lambda: clock[0], sleep=sleep
    )

    assert waited is not None and waited.owner == "kew verify alice:1"
    assert slept == [30.0, 30.0]
    assert store.live_merge_claim(PR, HEAD, now=clock[0]) is None
    # Nothing claims another head, so nothing waits on it.
    assert wait_out_merge_claim(store, PR, "cd" * 20, flow="x", sleep=sleep) is None
    assert len(slept) == 2


def _review_flow(store: SqliteRunStore, tmp_path: Path) -> Any:
    """A remediation whose agent finished, at the step that publishes it."""
    flow = object.__new__(review_module._ReviewDispatch)
    flow.config = DispatchConfig(project=ProjectConfig(repo="o/r"))
    flow.store = store
    flow.producer_run_id = None
    flow.result = SimpleNamespace(run_id="")
    flow.publication_mode = "checkout"
    flow.worktree_dir = tmp_path
    flow.branch_name = "agent/issue-42"
    flow.checkout = SimpleNamespace(head_sha=HEAD, integrated_base_sha="")
    flow.published_sha = HEAD
    flow.pr_number = PR
    flow.host_publishes = True
    flow.item_id = ITEM
    flow.base_branch = "main"
    return flow


def test_a_remediation_finishing_under_a_claim_publishes_after_it_ends(
    store: SqliteRunStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The claim was taken after the remediation launched: its push waits."""
    store.take_merge_claim(PR, (HEAD,), owner="kew verify alice:1", now=utc_now())
    events: list[tuple[str, Any]] = []

    def sleep(seconds: float) -> None:
        events.append(("sleep", seconds))
        store.release_merge_claim(PR, (HEAD,), owner="kew verify alice:1")

    def publish(*_args: Any, **kwargs: Any) -> bool:
        events.append(("push", store.live_merge_claim(PR, HEAD, now=utc_now())))
        return True

    monkeypatch.setattr(merge_claim_module, "time", SimpleNamespace(sleep=sleep))
    monkeypatch.setattr(review_module, "publish_existing_pr_branch", publish)

    _review_flow(store, tmp_path)._publish()

    assert [kind for kind, _ in events] == ["sleep", "push"]
    assert events[-1] == ("push", None)


# --- kew verify --pr ---

_GIT = ["git", "-c", "user.email=t@example.com", "-c", "user.name=t"]
_HELD_OUT_PASSED = opened("kew-held-out: exit 0 seconds 1\n" * 2)


def _git(args: list[str], cwd: Path) -> str:
    done = subprocess.run(
        [*_GIT, *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return done.stdout.strip()


def _project(repo: Path) -> str:
    """A repo whose HEAD is one candidate commit ahead of ``main``."""
    (repo / "kew").mkdir(parents=True, exist_ok=True)
    _git(["init", "--quiet", "-b", "main", "."], repo)
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(["add", "--all"], repo)
    _git(["commit", "--quiet", "-m", "base"], repo)
    _git(["checkout", "--quiet", "-b", "candidate"], repo)
    (repo / "kew" / "module.py").write_text("x = 1\n", encoding="utf-8")
    _git(["add", "--all"], repo)
    _git(["commit", "--quiet", "-m", "candidate"], repo)
    return _git(["rev-parse", "HEAD"], repo)


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """The candidate head, in a project whose own ``kew.toml`` the CLI loads."""
    from kew import subprocess_utils

    for tool in ("git", "sh"):
        subprocess_utils._register_optional_tool(tool)
    head = _project(tmp_path)
    monkeypatch.setenv("KEW_PROJECT", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "kew.toml").write_text(
        '[project]\nrepo = "o/r"\n\n'
        f'[logging]\ndir = "{tmp_path / "logs"}"\n\n'
        '[verify]\nhost = "local"\n',
        encoding="utf-8",
    )
    return head


def _claims(tmp_path: Path) -> list[Any]:
    with SqliteRunStore(str(tmp_path / "logs" / "dispatch.db")) as store:
        return store.list_merge_claims()


def _claim_seen_by(tmp_path: Path, head: str) -> Any:
    with SqliteRunStore(str(tmp_path / "logs" / "dispatch.db")) as store:
        return store.live_merge_claim(PR, head, now=datetime.now(UTC))


@pytest.mark.parametrize(
    ("answer", "exit_code"),
    [
        pytest.param((0, "gate passed\n" + _HELD_OUT_PASSED), 0, id="success"),
        pytest.param((3, "gate failed\n"), 3, id="failure"),
        pytest.param(
            HostTimeout("the job outran its 14400 s ceiling"), 2, id="timeout"
        ),
    ],
)
def test_verify_pr_claims_the_head_while_it_runs_and_releases_it(
    project: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    answer: Any,
    exit_code: int,
) -> None:
    during: list[Any] = []

    def _execute(self: LocalProvider, job: Job) -> tuple[int, str]:
        during.append(_claim_seen_by(tmp_path, project))
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(LocalProvider, "_execute", _execute)

    result = runner.invoke(app, ["verify", "--pr", str(PR)])

    assert result.exit_code == exit_code, result.output
    assert f"claiming PR #{PR} at {project[:12]}" in result.output
    # One claim, held for every job: the gate job and the suite behind it.
    claim = during[0]
    assert all(seen == claim for seen in during)
    assert claim is not None and claim.owner.startswith("kew verify ")
    assert _claims(tmp_path) == []


def test_verify_pr_refuses_a_head_another_claim_holds(
    project: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "logs").mkdir()
    with SqliteRunStore(str(tmp_path / "logs" / "dispatch.db")) as store:
        store.take_merge_claim(PR, (project,), owner="bob:9", now=datetime.now(UTC))
    ran: list[Job] = []
    monkeypatch.setattr(
        LocalProvider, "_execute", lambda _self, job: ran.append(job) or (0, "")
    )

    result = runner.invoke(app, ["verify", "--pr", str(PR)])

    assert result.exit_code == 2, result.output
    assert "already claimed by bob:9" in result.output
    assert ran == []
    assert [c.owner for c in _claims(tmp_path)] == ["bob:9"]


def _ad_hoc_merge(repo: Path) -> tuple[str, str]:
    """Main checked out, moved on, and the PR head merged in second: the
    merge and main's own commit."""
    _git(["checkout", "--quiet", "main"], repo)
    (repo / "MAIN.md").write_text("main moved\n", encoding="utf-8")
    _git(["add", "--all"], repo)
    _git(["commit", "--quiet", "-m", "main moved"], repo)
    main = _git(["rev-parse", "HEAD"], repo)
    _git(["merge", "--quiet", "--no-ff", "--no-edit", "candidate"], repo)
    return _git(["rev-parse", "HEAD"], repo), main


def _claim_line(output: str) -> str:
    (line,) = [ln for ln in output.splitlines() if "claiming PR" in ln]
    return line


def test_verify_of_a_merge_claims_both_parents(
    project: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    merge, main = _ad_hoc_merge(tmp_path)
    during: list[Any] = []

    def _execute(self: LocalProvider, job: Job) -> tuple[int, str]:
        during.append(_claim_seen_by(tmp_path, project))
        return 0, "gate passed\n" + _HELD_OUT_PASSED

    monkeypatch.setattr(LocalProvider, "_execute", _execute)

    result = runner.invoke(app, ["verify", "--pr", str(PR)])

    assert result.exit_code == 0, result.output
    assert during[0] is not None
    assert _claims(tmp_path) == []
    # The ledger cannot place the merge, so the line names both parents.
    line = _claim_line(result.output)
    assert f"at {main[:12]} or {project[:12]}" in line and merge[:12] in line


def _journal_pr(tmp_path: Path, head: str, *, item: str = ITEM) -> None:
    """The drive's newest snapshot links *item* to PR at *head*."""
    (tmp_path / "logs").mkdir(exist_ok=True)
    with SqliteRunStore(str(tmp_path / "logs" / "dispatch.db")) as store:
        store.append_epic_tick(
            epic_ref=EPIC,
            tick_ts="2026-10-25T12:00:00+00:00",
            snapshot={
                "schema": 1,
                "epic_ref": EPIC,
                "signal": "progressing",
                "counts": {},
                "frontier": [],
                "nodes": [
                    {
                        "id": item,
                        "status": "in_review",
                        "pr_url": f"https://github.com/o/r/pull/{PR}",
                        "head_oid": head,
                    }
                ],
                "edges": [],
            },
            actions={},
            snapshot_hash=head,
        )


def test_the_claim_line_names_the_pr_head_not_the_merge(
    project: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    merge, _main = _ad_hoc_merge(tmp_path)
    _journal_pr(tmp_path, project)
    monkeypatch.setattr(
        LocalProvider, "_execute", lambda _s, _j: (0, "gate\n" + _HELD_OUT_PASSED)
    )

    result = runner.invoke(app, ["verify", "--pr", str(PR)])

    assert result.exit_code == 0, result.output
    line = _claim_line(result.output)
    assert f"claiming PR #{PR} at {project[:12]} while it runs" in line
    assert merge[:12] not in line


def test_verify_item_claims_the_pr_the_ledger_links_to_it(
    project: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _journal_pr(tmp_path, project)
    during: list[Any] = []

    def _execute(self: LocalProvider, job: Job) -> tuple[int, str]:
        during.append(_claim_seen_by(tmp_path, project))
        return 0, "gate passed\n" + _HELD_OUT_PASSED

    monkeypatch.setattr(LocalProvider, "_execute", _execute)

    result = runner.invoke(app, ["verify", "--item", ITEM])

    assert result.exit_code == 0, result.output
    assert f"claiming PR #{PR} at {project[:12]}" in result.output
    # One claim, held for every job: the gate job and the suite behind it.
    claim = during[0]
    assert all(seen == claim for seen in during)
    assert claim is not None and claim.pr == PR
    assert _claims(tmp_path) == []


def _reserve_review(tmp_path: Path, run_id: str) -> None:
    """A remediation of the PR's node is running: it holds the review slot."""
    with SqliteRunStore(str(tmp_path / "logs" / "dispatch.db")) as store:
        assert store.reserve_epic_node_item(REVIEW_EPIC_REF, ITEM, run_id)


def test_verify_refuses_to_claim_under_a_running_remediation_unless_forced(
    project: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _journal_pr(tmp_path, project)
    _reserve_review(tmp_path, "remediation-run-1")
    ran: list[Any] = []

    def _execute(self: LocalProvider, job: Job) -> tuple[int, str]:
        ran.append(_claim_seen_by(tmp_path, project))
        return 0, "gate passed\n" + _HELD_OUT_PASSED

    monkeypatch.setattr(LocalProvider, "_execute", _execute)

    refused = runner.invoke(app, ["verify", "--pr", str(PR)])

    assert refused.exit_code == 2, refused.output
    assert f"review action of item {ITEM} (run remediat" in refused.output
    assert "--force-claim" in refused.output
    assert ran == [] and _claims(tmp_path) == []

    unexplained = runner.invoke(app, ["verify", "--pr", str(PR), "--force-claim"])

    assert unexplained.exit_code == 2, unexplained.output
    assert "--force-claim-reason" in unexplained.output
    assert ran == [] and _forced_events(tmp_path) == []

    forced = runner.invoke(
        app,
        [
            "verify",
            "--pr",
            str(PR),
            "--force-claim",
            "--force-claim-reason",
            "the remediation is a stale retry",
        ],
    )

    assert forced.exit_code == 0, forced.output
    # One claim, held for every job: the gate job and the suite behind it.
    claim = ran[0]
    assert all(seen == claim for seen in ran)
    assert claim is not None
    (event,) = _forced_events(tmp_path)
    assert event.payload is not None
    assert event.payload["pr"] == PR
    assert event.payload["reason"] == "the remediation is a stale retry"
    assert event.payload["runs"] == [
        {"kind": "review action", "item_id": ITEM, "run_id": "remediation-run-1"}
    ]


def _forced_events(tmp_path: Path) -> list[Any]:
    with SqliteRunStore(str(tmp_path / "logs" / "dispatch.db")) as store:
        return store.list_audit_events(event="merge_claim_forced")


def test_a_forced_take_that_is_refused_records_no_forced_claim(
    project: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _journal_pr(tmp_path, project)
    _reserve_review(tmp_path, "remediation-run-1")
    with SqliteRunStore(str(tmp_path / "logs" / "dispatch.db")) as store:
        store.take_merge_claim(PR, (project,), owner="bob:9", now=utc_now())
    monkeypatch.setattr(
        LocalProvider, "_execute", lambda _s, _j: pytest.fail("nothing may run")
    )

    result = runner.invoke(
        app,
        [
            "verify",
            "--pr",
            str(PR),
            "--force-claim",
            "--force-claim-reason",
            "stale remediation",
        ],
    )

    assert result.exit_code == 2, result.output
    assert "already claimed by bob:9" in result.output
    assert _forced_events(tmp_path) == []


def test_verify_refuses_to_claim_under_a_running_ci_fix(
    project: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _journal_pr(tmp_path, project)
    with SqliteRunStore(str(tmp_path / "logs" / "dispatch.db")) as store:
        store.save(
            RunRecord(
                run_id="ci-fix-run-1",
                issue_num=int(ITEM),
                branch="agent/issue-42",
                status="pending",
                exit_code=0,
                started_at=utc_now(),
                run_kind=CI_FIX_RUN_KIND,
                item_id=ITEM,
            )
        )
    monkeypatch.setattr(
        LocalProvider, "_execute", lambda _s, _j: pytest.fail("nothing may run")
    )

    result = runner.invoke(app, ["verify", "--pr", str(PR)])

    assert result.exit_code == 2, result.output
    assert f"ci-fix of item {ITEM} (run ci-fix-r" in result.output
    assert _claims(tmp_path) == []


def test_a_claim_that_lapses_mid_verify_is_reported_and_recorded(
    project: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _execute(self: LocalProvider, job: Job) -> tuple[int, str]:
        # The machine sleeps past the TTL mid-job: by the wall clock the
        # claim expired, and the heartbeat never woke to renew it.
        conn = sqlite3.connect(tmp_path / "logs" / "dispatch.db")
        with conn:
            conn.execute(
                "UPDATE merge_claims SET expires_at = ?",
                ((datetime.now(UTC) - timedelta(minutes=1)).isoformat(),),
            )
        conn.close()
        return 0, "gate passed\n" + _HELD_OUT_PASSED

    monkeypatch.setattr(LocalProvider, "_execute", _execute)

    result = runner.invoke(app, ["verify", "--pr", str(PR)])

    assert result.exit_code == 0, result.output
    assert f"merge claim on PR #{PR} lapsed mid-verify" in result.output
    with SqliteRunStore(str(tmp_path / "logs" / "dispatch.db")) as store:
        (event,) = store.list_audit_events(event=VERIFY_EVENT)
    assert event.payload is not None
    assert event.payload["merge_claim"] == {
        "pr": PR,
        "heads": [project],
        "lapsed": True,
    }


def test_a_claim_that_held_is_recorded_unlapsed(
    project: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        LocalProvider, "_execute", lambda _s, _j: (0, "gate\n" + _HELD_OUT_PASSED)
    )

    result = runner.invoke(app, ["verify", "--pr", str(PR)])

    assert result.exit_code == 0, result.output
    assert "lapsed" not in result.output
    with SqliteRunStore(str(tmp_path / "logs" / "dispatch.db")) as store:
        (event,) = store.list_audit_events(event=VERIFY_EVENT)
    assert event.payload is not None
    assert event.payload["merge_claim"]["lapsed"] is False


# --- kew merge claim ---


def test_merge_claim_take_renew_and_release_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "kew.toml").write_text(
        f'[project]\nrepo = "o/r"\n\n[logging]\ndir = "{tmp_path}"\n'
    )
    monkeypatch.setenv("KEW_SESSION", "alice-hand-flow")

    def kew(*args: str) -> Any:
        return runner.invoke(app, ["--project", str(project), "merge", "claim", *args])

    taken = kew("take", str(PR), "--head", HEAD)
    again = kew("take", str(PR), "--head", HEAD, "--owner", "bob")
    listed = kew("list")
    stolen = kew("release", str(PR), "--head", HEAD, "--owner", "bob")
    renewed = kew("renew", str(PR), "--head", HEAD)
    released = kew("release", str(PR), "--head", HEAD)
    after = kew("renew", str(PR), "--head", HEAD)
    empty = kew("list")
    short = kew("take", str(PR), "--head", "abc")

    assert taken.exit_code == 0, taken.output
    assert f"Claimed PR #{PR} at {HEAD[:12]} for alice-hand-flow" in taken.output
    assert again.exit_code == 1 and "already claimed by alice-hand-flow" in again.output
    assert listed.exit_code == 0 and f"#{PR}  {HEAD[:12]}  alice-hand-flow" in (
        listed.output
    )
    assert stolen.exit_code == 1, stolen.output
    assert renewed.exit_code == 0 and "Renewed" in renewed.output
    assert released.exit_code == 0 and "Released" in released.output
    assert after.exit_code == 1 and "holds no live claim" in after.output
    assert empty.exit_code == 0 and "No PR head is claimed." in empty.output
    assert short.exit_code == 2, short.output
    with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
        assert store.list_merge_claims() == []


def test_without_kew_session_renew_and_release_need_the_owner_take_printed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``take``'s ``user:pid`` fallback names the calling shell, which is a
    different process under ``uv run kew`` or a new shell on every call."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "kew.toml").write_text(
        f'[project]\nrepo = "o/r"\n\n[logging]\ndir = "{tmp_path}"\n'
    )
    monkeypatch.delenv("KEW_SESSION", raising=False)

    def kew(*args: str) -> Any:
        return runner.invoke(app, ["--project", str(project), "merge", "claim", *args])

    taken = kew("take", str(PR), "--head", HEAD)
    (hint,) = [ln for ln in taken.output.splitlines() if "--owner" in ln]
    owner = hint.split("--owner ", 1)[1].rstrip(".").strip("'")
    bare_renew = kew("renew", str(PR), "--head", HEAD)
    bare_release = kew("release", str(PR), "--head", HEAD)
    renewed = kew("renew", str(PR), "--head", HEAD, "--owner", owner)
    released = kew("release", str(PR), "--head", HEAD, "--owner", owner)

    assert taken.exit_code == 0, taken.output
    assert f"for {owner} until" in taken.output
    for bare in (bare_renew, bare_release):
        assert bare.exit_code == 2, bare.output
        assert "pass --owner" in bare.output and "KEW_SESSION" in bare.output
    assert renewed.exit_code == 0, renewed.output
    assert released.exit_code == 0, released.output
    with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
        assert store.list_merge_claims() == []
