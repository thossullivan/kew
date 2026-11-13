"""#2459 — the review stage launches detached, so a tick never waits on a review.

With a detacher wired, ``drive_review_stage`` reserves each due node under the
``review`` pseudo-epic, writes its ``review_stage`` run row and hands the action
to a child, then returns. The child takes the very action the synchronous stage
would have taken, under the tick's reservation. These tests stand the child up
on a thread (its own ledger handle, as a process would have) or as a real
process, and never sleep to prove a wait did not happen: the reviewer blocks on
an event the test only sets after the tick has returned.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from support.review_stream import stream_verdict

from kew.config import DispatchConfig, LoggingConfig, ReviewConfig
from kew.graph.domain import NodeStatus
from kew.graph.driver import review_detach
from kew.graph.driver.base_integration import BaseIntegrationOutcome
from kew.graph.driver.facts import NodeFacts
from kew.graph.driver.overlay import DriverStatus
from kew.graph.driver.review_detach import (
    DetachedReview,
    DetachedReviewSpawner,
    facts_from_json,
    facts_to_json,
)
from kew.graph.driver.review_stage import (
    RemediationContext,
    ReviewStageOutcome,
    drive_detached_node,
    drive_review_stage,
)
from kew.graph.driver.tick import _reconcile_reservations, default_run_liveness
from kew.pseudo_epics import REVIEW_EPIC_REF
from kew.review_dispatch import ReviewerLaunchContext
from kew.review_outcome import ReviewOutcome
from kew.run_store import (
    ADVERSARIAL_REVIEW_RUN_KIND,
    PR_SUBJECT_RUN_KINDS,
    REVIEW_STAGE_RUN_KIND,
    RunRecord,
    SqliteRunStore,
)
from kew.subprocess_utils import run

EPIC = "epic:x"
ITEM = "41"
NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
WAIT_S = 20
VERDICT = {
    "verdict": "NEEDS_CHANGES",
    "findings": [
        {
            "severity": "high",
            "location": "code.py:1",
            "summary": "unchecked path",
            "evidence": {"command": "uv run pytest -q", "output_digest": "sha256:x"},
        }
    ],
}


@pytest.fixture(autouse=True)
def _register_git(monkeypatch: pytest.MonkeyPatch) -> None:
    from kew.subprocess_utils import TOOL_PATHS

    git = shutil.which("git")
    assert git is not None, "git required for review-stage tests"
    monkeypatch.setitem(TOOL_PATHS, "git", git)


@pytest.fixture
def no_setpgid(monkeypatch: pytest.MonkeyPatch) -> None:
    """A thread child must not move pytest into its own process group.

    A real child claims the row with ``exclusive_owner_pid``; on a thread the
    claim keeps the same refusals (gone / not pending) and skips the setpgid.
    """

    def _claim(store, run_id: str) -> bool:
        row = store.get(run_id)
        return row is not None and row.status == "pending"

    monkeypatch.setattr(review_detach, "_claim_ownership", _claim)


def _make_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    run(["git", "config", "user.email", "t@t.t"], cwd=repo, check=True)
    run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "code.py").write_text("x = 1\n")
    run(["git", "add", "-A"], cwd=repo, check=True)
    run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)
    head = run(["git", "rev-parse", "HEAD"], cwd=repo, check=True).stdout.strip()
    return repo, head


def _config(tmp_path: Path) -> DispatchConfig:
    return DispatchConfig(
        review=ReviewConfig(
            adversarial=True,
            blocking=True,
            max_rounds=3,
            max_review_usd=10.0,
            publish_status=False,
            publish_comment=False,
        ),
        logging=LoggingConfig(dir=str(tmp_path / "logs")),
    )


def _facts(head: str, *, merge_state: str | None = None) -> NodeFacts:
    return NodeFacts(
        issue_num=int(ITEM),
        state="OPEN",
        state_reason=None,
        labels=frozenset({"b", "a"}),
        pr_number=141,
        pr_url="https://example.invalid/pr/141",
        review_decision=None,
        merge_state_status=merge_state,
        head_oid=head,
        pr_pushed_at=None,
    )


class _ThreadDetacher:
    """Runs each child on its own thread and its own ledger handle."""

    def __init__(self, store: SqliteRunStore, tmp_path: Path, repo: Path, **seams):
        self.store = store
        self.config = _config(tmp_path)
        self.repo = repo
        self.seams = seams
        self.threads: list[threading.Thread] = []
        self.spawned: list[DetachedReview] = []
        self.ran: list[bool] = []

    def __call__(self, review: DetachedReview) -> None:
        self.spawned.append(review)
        thread = threading.Thread(target=self._child, args=(review,))
        self.threads.append(thread)
        thread.start()

    def _child(self, review: DetachedReview) -> None:
        child_store = self.store.open_sibling()
        try:
            self.ran.append(
                drive_detached_node(
                    # Through the argv form the real spawner uses.
                    replace(review, facts=facts_from_json(facts_to_json(review.facts))),
                    config=self.config,
                    store=child_store,
                    repo="o/r",
                    repo_root=self.repo,
                    base="main",
                    now=NOW,
                    **self.seams,
                )
            )
        finally:
            child_store.close()

    def join(self) -> None:
        for thread in self.threads:
            thread.join(WAIT_S)
            assert not thread.is_alive(), "detached child never finished"


def _tick(
    store: SqliteRunStore,
    tmp_path: Path,
    repo: Path,
    facts: NodeFacts,
    *,
    status: DriverStatus = DriverStatus.ADVERSARIAL_REVIEW,
    detacher=None,
    reviewer_launcher=None,
    remediator=None,
    base_integrator=None,
) -> tuple[ReviewStageOutcome, dict[str, DriverStatus]]:
    statuses = {ITEM: status}
    outcome = drive_review_stage(
        statuses,
        {ITEM: facts},
        epic_ref=EPIC,
        config=_config(tmp_path),
        store=store,
        repo="o/r",
        repo_root=repo,
        base="main",
        reviewer_launcher=reviewer_launcher,
        remediator=remediator,
        base_integrator=base_integrator,
        worker_cap=4,
        max_concurrent=2,
        now=NOW,
        detacher=detacher,
    )
    return outcome, statuses


def _gated_reviewer(gate: threading.Event, started: threading.Event):
    def _launch(ctx: ReviewerLaunchContext):
        started.set()
        assert gate.wait(WAIT_S), "the test never released the reviewer"
        return stream_verdict(ctx, VERDICT)

    return _launch


def _stage_rows(store: SqliteRunStore) -> list[RunRecord]:
    return [r for r in store.list_by_item(ITEM) if r.run_kind == REVIEW_STAGE_RUN_KIND]


def test_tick_returns_while_the_reviewer_is_still_running(
    tmp_path: Path, no_setpgid: None
) -> None:
    """AC1: the tick starts the review and returns; the next tick reads it."""
    repo, head = _make_repo(tmp_path)
    gate, started = threading.Event(), threading.Event()
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        detacher = _ThreadDetacher(
            store,
            tmp_path,
            repo,
            reviewer_launcher=_gated_reviewer(gate, started),
        )
        try:
            outcome, statuses = _tick(
                store, tmp_path, repo, _facts(head), detacher=detacher
            )
            # The tick is back while the reviewer is provably mid-run.
            assert started.wait(WAIT_S)
            assert not gate.is_set()
            assert outcome.detached == {ITEM: review_detach.DETACHED_WAITING_REASON}
            assert outcome.verdicts == {} and outcome.dispatched == []
            assert statuses == {ITEM: DriverStatus.ADVERSARIAL_REVIEW}
            # The slot and the run row were written before the tick returned.
            [reservation] = store.list_epic_reservations(REVIEW_EPIC_REF)
            [stage] = _stage_rows(store)
            assert reservation.run_id == stage.run_id and stage.status == "pending"
            assert store.latest_review_round(ITEM) is None

            # A tick while the child is live neither waits nor double-starts it.
            again, _ = _tick(store, tmp_path, repo, _facts(head), detacher=detacher)
            # It still reports the node as worked on, so the tick is not stalled.
            assert len(detacher.spawned) == 1
            assert again.detached == {ITEM: review_detach.DETACHED_WAITING_REASON}
        finally:
            gate.set()
            detacher.join()

        assert detacher.ran == [True]
        latest = store.latest_review_round(ITEM)
        assert latest is not None
        assert (latest.verdict, latest.head_sha) == ("NEEDS_CHANGES", head)
        assert store.list_epic_reservations(REVIEW_EPIC_REF) == []
        [stage] = _stage_rows(store)
        assert stage.status == "success"
        assert stage.error_category == review_detach.STAGE_FINISHED
        # The next tick's reconcile finds nothing pinned, and the node is due its
        # remediation (NEEDS_CHANGES on this head) — the transition it reads.
        assert (
            _reconcile_reservations(REVIEW_EPIC_REF, store, _config(tmp_path), now=NOW)
            == set()
        )


def _round_scene(tmp_path: Path, *, detached: bool) -> tuple[object, ...]:
    repo, head = _make_repo(tmp_path)
    gate, started = threading.Event(), threading.Event()
    gate.set()
    launcher = _gated_reviewer(gate, started)
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        if detached:
            detacher = _ThreadDetacher(
                store, tmp_path, repo, reviewer_launcher=launcher
            )
            _tick(store, tmp_path, repo, _facts(head), detacher=detacher)
            detacher.join()
        else:
            _tick(store, tmp_path, repo, _facts(head), reviewer_launcher=launcher)
        latest = store.latest_review_round(ITEM)
        assert latest is not None
        sidecar = Path(latest.sidecar_path or "").read_bytes()
        reviewer_rows = [
            (r.status, r.error_category)
            for r in store.list_by_item(ITEM)
            if r.run_kind == ADVERSARIAL_REVIEW_RUN_KIND
        ]
        return (
            latest.round,
            latest.verdict,
            latest.head_sha == head,
            latest.sidecar_digest,
            latest.review_mode,
            latest.plan_sha,
            latest.criteria_json,
            latest.accepted_verdict,
            sidecar,
            reviewer_rows,
            sorted(h.reason for h in store.list_epic_holds(EPIC)),
            store.list_epic_reservations(REVIEW_EPIC_REF),
        )


def test_detached_round_is_identical_to_the_synchronous_round(
    tmp_path: Path, no_setpgid: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC2: same fake reviewer output, same recorded round."""
    # Both scenes commit the same tree at the same instant, so they review the
    # same head sha and the sidecar (which names it) digests identically.
    for name in ("GIT_AUTHOR_DATE", "GIT_COMMITTER_DATE"):
        monkeypatch.setenv(name, "2026-10-07T12:00:00+00:00")
    synchronous = _round_scene(tmp_path / "sync", detached=False)
    detached = _round_scene(tmp_path / "detached", detached=True)
    assert detached == synchronous
    assert synchronous[1] == "NEEDS_CHANGES" and synchronous[3]


def _owned_stage(
    store: SqliteRunStore, tmp_path: Path, pid: int, run_id: str
) -> RunRecord:
    """The stage row as a real child leaves it once it has claimed ownership."""
    from kew.process_groups import identity_at_admission

    row = store.get(run_id)
    assert row is not None
    owned = replace(row, pgid=pid, owner_pid=pid, **identity_at_admission(pid))
    store.save(owned, update_ownership=True)
    return owned


def test_a_child_that_dies_mid_review_is_reaped_killed_and_frees_its_slot(
    tmp_path: Path,
) -> None:
    """AC3: a dead child is reaped like a dead dispatch child; no round."""
    _, head = _make_repo(tmp_path)
    config = _config(tmp_path)
    child = subprocess.Popen(  # noqa: S603 — the test's own interpreter
        [sys.executable, "-c", "import time; time.sleep(120)"],
        start_new_session=True,
    )
    spawned: list[DetachedReview] = []
    try:
        with SqliteRunStore(str(tmp_path / "d.db")) as store:
            outcome, _ = _tick(
                store, tmp_path, tmp_path, _facts(head), detacher=spawned.append
            )
            assert list(outcome.detached) == [ITEM]
            [review] = spawned
            stage = _owned_stage(store, tmp_path, child.pid, review.run_id)
            # The reviewer the child started: its own row, owned by the child.
            store.save(
                RunRecord(
                    run_id="reviewer-run",
                    issue_num=int(ITEM),
                    item_id=ITEM,
                    branch="",
                    status="pending",
                    exit_code=0,
                    started_at=datetime.now(UTC),
                    run_kind=ADVERSARIAL_REVIEW_RUN_KIND,
                    pgid=stage.pgid,
                    owner_pid=stage.owner_pid,
                    owner_start_time=stage.owner_start_time,
                    owner_start_token=stage.owner_start_token,
                    owner_ppid=stage.owner_ppid,
                    owner_command=stage.owner_command,
                ),
                update_ownership=True,
            )
            live = _reconcile_reservations(
                REVIEW_EPIC_REF, store, config, now=datetime.now(UTC)
            )
            assert live == {ITEM}, "a live child must keep its slot"

            os.killpg(child.pid, signal.SIGKILL)
            child.wait(WAIT_S)

            live = _reconcile_reservations(
                REVIEW_EPIC_REF,
                store,
                config,
                now=datetime.now(UTC),
                liveness=default_run_liveness(config),
            )
            assert live == set()
            reaped = store.get(review.run_id)
            assert reaped is not None and reaped.status == "killed"
            reviewer = store.get("reviewer-run")
            assert reviewer is not None and reviewer.status == "killed"
            assert store.latest_review_round(ITEM) is None
    finally:
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait(WAIT_S)


def test_a_child_that_never_started_is_reaped_after_the_startup_grace(
    tmp_path: Path,
) -> None:
    """The row the tick wrote has no owner until the child claims it."""
    _, head = _make_repo(tmp_path)
    config = _config(tmp_path)
    spawned: list[DetachedReview] = []
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _tick(store, tmp_path, tmp_path, _facts(head), detacher=spawned.append)
        start = datetime.now(UTC)
        assert _reconcile_reservations(
            REVIEW_EPIC_REF, store, config, now=start + timedelta(minutes=5)
        ) == {ITEM}
        assert (
            _reconcile_reservations(
                REVIEW_EPIC_REF, store, config, now=start + timedelta(minutes=11)
            )
            == set()
        )
        [stage] = _stage_rows(store)
        assert stage.status == "killed"


def test_a_child_whose_reservation_was_reaped_does_nothing(
    tmp_path: Path, no_setpgid: None
) -> None:
    repo, head = _make_repo(tmp_path)
    launched: list[str] = []
    spawned: list[DetachedReview] = []
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _tick(store, tmp_path, repo, _facts(head), detacher=spawned.append)
        [review] = spawned
        store.release_epic_node_item(ITEM, run_id=review.run_id)
        ran = drive_detached_node(
            review,
            config=_config(tmp_path),
            store=store,
            repo="o/r",
            repo_root=repo,
            base="main",
            reviewer_launcher=lambda ctx: launched.append(ctx.run_id),
            now=NOW,
        )
        assert ran is False and launched == []
        assert store.latest_review_round(ITEM) is None


def test_a_failed_spawn_gives_the_slot_back(tmp_path: Path) -> None:
    _, head = _make_repo(tmp_path)

    def _refuse(review: DetachedReview) -> None:
        raise OSError("no kew on PATH")

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        outcome, _ = _tick(store, tmp_path, tmp_path, _facts(head), detacher=_refuse)
        assert outcome.failed == [ITEM] and outcome.detached == {}
        assert store.list_epic_reservations(REVIEW_EPIC_REF) == []
        [stage] = _stage_rows(store)
        assert (stage.status, stage.error_category) == (
            "failure",
            review_detach.SPAWN_FAILED,
        )


def test_width_and_fleet_cap_bound_the_children_started(tmp_path: Path) -> None:
    _, head = _make_repo(tmp_path)
    spawned: list[DetachedReview] = []
    items = ("41", "42", "43")
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        facts = {i: replace(_facts(head), issue_num=int(i)) for i in items}
        outcome = drive_review_stage(
            dict.fromkeys(items, DriverStatus.ADVERSARIAL_REVIEW),
            facts,
            epic_ref=EPIC,
            config=_config(tmp_path),
            store=store,
            repo="o/r",
            repo_root=tmp_path,
            base="main",
            reviewer_launcher=None,
            worker_cap=4,
            max_concurrent=2,
            now=NOW,
            detacher=spawned.append,
        )
        assert [r.item_id for r in spawned] == ["41", "42"]
        assert list(outcome.detached) == ["41", "42"]
    # The fleet cap: one slot left for three due nodes.
    with SqliteRunStore(str(tmp_path / "capped.db")) as store:
        store.reserve_epic_node_item("epic:other", "99", "busy-1")
        store.reserve_epic_node_item("epic:other", "98", "busy-2")
        spawned.clear()
        drive_review_stage(
            dict.fromkeys(items, DriverStatus.ADVERSARIAL_REVIEW),
            facts,
            epic_ref=EPIC,
            config=_config(tmp_path),
            store=store,
            repo="o/r",
            repo_root=tmp_path,
            base="main",
            reviewer_launcher=None,
            worker_cap=3,
            max_concurrent=3,
            now=NOW,
            detacher=spawned.append,
        )
        assert [r.item_id for r in spawned] == ["41"]


def test_children_still_running_from_earlier_ticks_count_against_the_width(
    tmp_path: Path,
) -> None:
    """A live child from an earlier tick holds a width slot (#2459 review).

    The synchronous stage never ran more than ``max_concurrent`` reviews at
    once; a detached child that outlives its tick must not let the next tick
    start another one on top of it.
    """
    _, head = _make_repo(tmp_path)
    spawned: list[DetachedReview] = []
    items = ("41", "42", "43")
    facts = {i: replace(_facts(head), issue_num=int(i)) for i in items}

    def _tick_once(store: SqliteRunStore) -> ReviewStageOutcome:
        return drive_review_stage(
            dict.fromkeys(items, DriverStatus.ADVERSARIAL_REVIEW),
            facts,
            epic_ref=EPIC,
            config=_config(tmp_path),
            store=store,
            repo="o/r",
            repo_root=tmp_path,
            base="main",
            reviewer_launcher=None,
            worker_cap=8,
            max_concurrent=1,
            now=NOW,
            # Never runs the child: its reservation stays live across ticks.
            detacher=spawned.append,
        )

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _tick_once(store)
        assert [r.item_id for r in spawned] == ["41"]
        for _ in range(3):
            outcome = _tick_once(store)
            assert [r.item_id for r in spawned] == ["41"]
            assert list(outcome.detached) == ["41"]
        assert [r.item_id for r in store.list_epic_reservations(REVIEW_EPIC_REF)] == [
            "41"
        ]


class _Remediator:
    """Reads the reservations from its own handle, as the child's thread must."""

    def __init__(self, db: Path) -> None:
        self.db = db
        self.contexts: list[RemediationContext] = []
        self.reservations: list[list[str]] = []

    def __call__(self, ctx: RemediationContext) -> ReviewOutcome | None:
        self.contexts.append(ctx)
        with SqliteRunStore(str(self.db)) as store:
            self.reservations.append(
                [r.run_id for r in store.list_epic_reservations(REVIEW_EPIC_REF)]
            )
        return None


def test_remediation_runs_detached_under_the_stage_reservation(
    tmp_path: Path, no_setpgid: None
) -> None:
    """AC4: remediation is detached and runs under the tick's reservation."""
    repo, head = _make_repo(tmp_path)
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        store.record_review_round(
            EPIC, ITEM, head_sha=head, verdict="NEEDS_CHANGES", sidecar_digest="d"
        )
        remediator = _Remediator(tmp_path / "d.db")
        detacher = _ThreadDetacher(store, tmp_path, repo, remediator=remediator)
        outcome, _ = _tick(
            store,
            tmp_path,
            repo,
            _facts(head),
            status=DriverStatus.REMEDIATING,
            detacher=detacher,
        )
        detacher.join()
        [review] = detacher.spawned
        assert list(outcome.detached) == [ITEM]
        [ctx] = remediator.contexts
        assert ctx.reservation_held is True
        assert remediator.reservations == [[review.run_id]]
        assert store.list_epic_reservations(REVIEW_EPIC_REF) == []


def test_base_integration_runs_detached_and_records_its_step(
    tmp_path: Path, no_setpgid: None
) -> None:
    """AC4: the merge-main step is detached; its marker row is written as before."""
    repo, head = _make_repo(tmp_path)
    calls: list[object] = []

    def _integrate(ctx) -> BaseIntegrationOutcome:
        calls.append(ctx)
        return BaseIntegrationOutcome("integrated")

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        detacher = _ThreadDetacher(store, tmp_path, repo, base_integrator=_integrate)
        outcome, _ = _tick(
            store,
            tmp_path,
            repo,
            _facts(head, merge_state="DIRTY"),
            status=DriverStatus.IN_REVIEW,
            detacher=detacher,
        )
        detacher.join()
        assert list(outcome.detached) == [ITEM]
        assert detacher.spawned[0].integration_only is True
        assert len(calls) == 1
        kinds = {r.run_kind for r in store.list_by_item(ITEM)}
        assert len(kinds - {REVIEW_STAGE_RUN_KIND}) == 1, kinds
        assert store.list_epic_reservations(REVIEW_EPIC_REF) == []


def test_held_reservation_skips_the_remediation_reserve(tmp_path: Path) -> None:
    from kew.review import _ReviewDispatch

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        store.reserve_epic_node_item(REVIEW_EPIC_REF, ITEM, "stage-run")
        review = _ReviewDispatch(
            pr_number=141,
            config=_config(tmp_path),
            author_filter=None,
            store=store,
            force_budget=False,
            item_id=ITEM,
            findings=[],
            reservation_held=True,
        )
        assert review._reserve() is None
        assert review.effective_run_kind == "review_remediation"
        # Nothing taken, so nothing of ours to release: the holder's row stands.
        review._release_reservation()
        [held] = store.list_epic_reservations(REVIEW_EPIC_REF)
        assert held.run_id == "stage-run"


def test_facts_survive_the_argv_round_trip() -> None:
    facts = replace(
        _facts("abc"), admission_status=NodeStatus.QUEUED, pr_has_closing_ref=True
    )
    assert facts_from_json(facts_to_json(facts)) == facts


def test_review_stage_rows_never_derive_node_status() -> None:
    assert REVIEW_STAGE_RUN_KIND in PR_SUBJECT_RUN_KINDS


def test_spawner_starts_a_detached_review_node_child(tmp_path: Path) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    class _Child:
        def poll(self) -> None:
            return None

    def _popen(cmd: list[str], **kwargs: object) -> _Child:
        calls.append((cmd, kwargs))
        return _Child()

    spawner = DetachedReviewSpawner(_config(tmp_path), base="main", popen=_popen)
    review = DetachedReview(EPIC, ITEM, ITEM, "run-1", _facts("abc"), True)
    spawner(review)
    [(cmd, kwargs)] = calls
    assert cmd[:4] == ["kew", "epic", "_review-node", EPIC]
    assert cmd[4] == ITEM and "--integration-only" in cmd
    assert cmd[cmd.index("--run-id") + 1] == "run-1"
    assert facts_from_json(cmd[cmd.index("--facts") + 1]) == review.facts
    assert kwargs["start_new_session"] is True
    assert kwargs["stdout"] == subprocess.DEVNULL


def test_the_cli_wires_the_detached_stage_whenever_review_is_on(
    tmp_path: Path,
) -> None:
    """The default `kew epic advance` / `run` path is the detached one."""
    from kew.cli.epic import _review_detacher

    assert isinstance(_review_detacher(_config(tmp_path)), DetachedReviewSpawner)
    off = _config(tmp_path).model_copy(update={"review": ReviewConfig()})
    assert off.review.adversarial is False
    assert _review_detacher(off) is None


def test_a_child_that_cannot_claim_its_row_gives_the_slot_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, head = _make_repo(tmp_path)
    spawned: list[DetachedReview] = []

    def _refuse(store, run_id: str) -> bool:
        raise RuntimeError("Cannot establish an exclusive process group")

    monkeypatch.setattr(review_detach, "_claim_ownership", _refuse)
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _tick(store, tmp_path, tmp_path, _facts(head), detacher=spawned.append)
        [review] = spawned
        with pytest.raises(RuntimeError, match="exclusive process group"):
            drive_detached_node(
                review,
                config=_config(tmp_path),
                store=store,
                repo="o/r",
                repo_root=tmp_path,
                base="main",
                now=NOW,
            )
        assert store.list_epic_reservations(REVIEW_EPIC_REF) == []
        [stage] = _stage_rows(store)
        assert stage.status == "failure"


def _spawned_review(store: SqliteRunStore, tmp_path: Path) -> DetachedReview:
    _, head = _make_repo(tmp_path)
    spawned: list[DetachedReview] = []
    _tick(store, tmp_path, tmp_path, _facts(head), detacher=spawned.append)
    [review] = spawned
    return review


def test_a_raised_stage_leaves_its_exception_on_the_row_and_its_log(
    tmp_path: Path, no_setpgid: None
) -> None:
    """#2656: the child's output is /dev/null, so it keeps its own evidence."""

    def _raising_stage(*_args: object, **_kwargs: object) -> None:
        raise ValueError("base integration\nlost its worktree")

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        review = _spawned_review(store, tmp_path)
        [pending] = _stage_rows(store)
        expected_log = (
            tmp_path / "logs" / f"review-stage-{ITEM}-{review.run_id[:8]}.log"
        ).resolve()
        # The tick records the path before the child exists, so a child that
        # dies before writing a line still names where its log would be.
        assert pending.log_file == expected_log
        with pytest.raises(ValueError, match="lost its worktree"):
            review_detach.run_detached_review(review, drive=_raising_stage, store=store)
        [stage] = _stage_rows(store)
        assert stage.status == "failure"
        assert stage.error_category == (
            f"{review_detach.STAGE_RAISED}: "
            "ValueError: base integration lost its worktree"
        )
        assert stage.log_file == expected_log
        log = expected_log.read_text()
        assert "Traceback (most recent call last)" in log
        assert "_raising_stage" in log
        assert "ValueError: base integration" in log
        assert store.list_epic_reservations(REVIEW_EPIC_REF) == []


def test_a_clean_stage_leaves_a_short_log(tmp_path: Path, no_setpgid: None) -> None:
    ran: list[str] = []

    def _clean_stage(node_id: str, *_args: object, **_kwargs: object) -> None:
        ran.append(node_id)

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        review = _spawned_review(store, tmp_path)
        assert review_detach.run_detached_review(
            review, drive=_clean_stage, store=store
        )
        [stage] = _stage_rows(store)
        assert ran == [ITEM]
        assert (stage.status, stage.error_category) == (
            "success",
            review_detach.STAGE_FINISHED,
        )
        assert stage.log_file is not None
        lines = stage.log_file.read_text().splitlines()
        assert len(lines) == 2, lines
        assert "started" in lines[0] and "finished" in lines[1]
        assert "Traceback" not in "\n".join(lines)


def test_the_stage_log_name_is_sanitized_like_a_dispatch_log(tmp_path: Path) -> None:
    path = review_detach.stage_log_path(
        _config(tmp_path), "../SFD 12", "0123456789abcdef"
    )
    assert path.parent == (tmp_path / "logs").resolve()
    assert path.name == "review-stage-.._SFD_12-01234567.log"
