"""#2110 — the review stage runs due reviews and remediations concurrently.

Five NEEDS_CHANGES nodes once took an hour of serial remediation because the
stage launched each node's (minutes-long) review or remediation one after
another inside the tick. The stage now runs up to ``max_concurrent`` nodes at
once, each worker on its own ledger handle, and merges results in node order.

Overlap is proved with barriers, never with sleeps: a barrier of N parties only
opens when N launches are live at the same time.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from support.review_stream import stream_verdict

from kew.config import DispatchConfig, LoggingConfig, ReviewConfig
from kew.graph.driver import review_stage
from kew.graph.driver.facts import NodeFacts
from kew.graph.driver.overlay import DriverStatus
from kew.graph.driver.review_launcher import StoreBound
from kew.graph.driver.review_stage import (
    RemediationContext,
    ReviewStageOutcome,
    drive_review_stage,
)
from kew.graph.driver.tick import _review_concurrency
from kew.progress import _log, line_scope
from kew.pseudo_epics import REVIEW_EPIC_REF
from kew.review_contract import CollectedSidecar
from kew.review_dispatch import ReviewerLaunchContext
from kew.run_store import (
    ADVERSARIAL_REVIEW_RUN_KIND,
    RunRecord,
    RunStore,
    SqliteRunStore,
)
from kew.subprocess_utils import run

EPIC = "epic:x"
ITEMS = ("41", "42", "43")
NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
# Generous: a barrier only waits this long when the stage fails to overlap.
BARRIER_TIMEOUT_S = 20


@pytest.fixture(autouse=True)
def _register_git(monkeypatch):
    from kew.subprocess_utils import TOOL_PATHS

    git = shutil.which("git")
    assert git is not None, "git required for review-stage tests"
    monkeypatch.setitem(TOOL_PATHS, "git", git)


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


def _config(tmp_path: Path, *, max_review_usd: float = 10.0) -> DispatchConfig:
    return DispatchConfig(
        review=ReviewConfig(
            adversarial=True,
            blocking=True,
            max_rounds=3,
            max_review_usd=max_review_usd,
            publish_status=False,
            publish_comment=False,
        ),
        logging=LoggingConfig(dir=str(tmp_path / "logs")),
    )


def _facts(item: str, head: str) -> NodeFacts:
    return NodeFacts(
        issue_num=int(item),
        state="OPEN",
        state_reason=None,
        labels=frozenset(),
        pr_number=100 + int(item),
        pr_url=None,
        review_decision=None,
        merge_state_status=None,
        head_oid=head,
        pr_pushed_at=None,
    )


def _seed_needs_changes(store: RunStore, head: str) -> None:
    for item in ITEMS:
        store.record_review_round(
            EPIC, item, head_sha=head, verdict="NEEDS_CHANGES", sidecar_digest="d"
        )


def _drive(
    store: SqliteRunStore,
    tmp_path: Path,
    head: str,
    *,
    status: DriverStatus,
    max_concurrent: int,
    remediator=None,
    reviewer_launcher=None,
    config: DispatchConfig | None = None,
    repo_root: Path | None = None,
) -> tuple[ReviewStageOutcome, dict[str, DriverStatus]]:
    statuses = dict.fromkeys(ITEMS, status)
    outcome = drive_review_stage(
        statuses,
        {item: _facts(item, head) for item in ITEMS},
        epic_ref=EPIC,
        config=config or _config(tmp_path),
        store=store,
        repo="o/r",
        repo_root=repo_root or tmp_path,
        base="main",
        reviewer_launcher=reviewer_launcher,
        remediator=remediator,
        max_concurrent=max_concurrent,
        now=NOW,
    )
    return outcome, statuses


class _Recorder:
    """Records each remediation's thread and the peak number live at once."""

    def __init__(self, barrier: threading.Barrier | None = None) -> None:
        self.barrier = barrier
        self.lock = threading.Lock()
        self.live = 0
        self.peak = 0
        self.items: list[str] = []
        self.threads: set[int] = set()
        self.stores: list[RunStore] = []

    def build(self, store: RunStore):
        def _remediate(ctx: RemediationContext) -> None:
            # The handle this seam was bound to must be usable on this thread:
            # a thread-affine SQLite handle from another thread would raise.
            assert store.latest_review_round(ctx.item_id) is not None
            with self.lock:
                self.live += 1
                self.peak = max(self.peak, self.live)
                self.items.append(ctx.item_id)
                self.threads.add(threading.get_ident())
                self.stores.append(store)
            try:
                if self.barrier is not None:
                    self.barrier.wait()
            finally:
                with self.lock:
                    self.live -= 1

        return _remediate


def test_three_remediations_overlap_under_max_concurrent_three(tmp_path: Path):
    _, head = _make_repo(tmp_path)
    recorder = _Recorder(threading.Barrier(3, timeout=BARRIER_TIMEOUT_S))
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _seed_needs_changes(store, head)
        outcome, statuses = _drive(
            store,
            tmp_path,
            head,
            status=DriverStatus.REMEDIATING,
            max_concurrent=3,
            remediator=StoreBound(recorder.build, store),
        )
    # All three reached the 3-party barrier together: they ran concurrently.
    assert recorder.peak == 3
    assert sorted(recorder.items) == list(ITEMS)
    assert len(recorder.threads) == 3
    assert threading.get_ident() not in recorder.threads
    # Each worker ran over its own handle, never the tick's.
    assert all(s is not store for s in recorder.stores)
    assert len({id(s) for s in recorder.stores}) == 3
    # Merged in node order, exactly as the serial path reports it.
    assert outcome.remediating == list(ITEMS)
    assert outcome.failed == []
    assert statuses == dict.fromkeys(ITEMS, DriverStatus.REMEDIATING)


def test_max_concurrent_one_stays_serial_on_the_tick_thread(tmp_path: Path):
    _, head = _make_repo(tmp_path)
    recorder = _Recorder()
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _seed_needs_changes(store, head)
        outcome, _ = _drive(
            store,
            tmp_path,
            head,
            status=DriverStatus.REMEDIATING,
            max_concurrent=1,
            remediator=StoreBound(recorder.build, store),
        )
        assert recorder.stores == [store, store, store]
    assert recorder.peak == 1
    assert recorder.items == list(ITEMS)
    assert recorder.threads == {threading.get_ident()}
    assert outcome.remediating == list(ITEMS)


def test_in_memory_ledger_keeps_the_serial_path(tmp_path: Path):
    _, head = _make_repo(tmp_path)
    recorder = _Recorder()
    with SqliteRunStore(":memory:") as store:
        _seed_needs_changes(store, head)
        outcome, _ = _drive(
            store,
            tmp_path,
            head,
            status=DriverStatus.REMEDIATING,
            max_concurrent=3,
            remediator=StoreBound(recorder.build, store),
        )
    assert recorder.threads == {threading.get_ident()}
    assert outcome.remediating == list(ITEMS)


def _pool_widths(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Every ``max_workers`` the stage hands its pool, in submission order.

    Observed overlap alone cannot tell "two ran together" from "three were
    allowed and the third happened to start late", so the ceiling itself is
    asserted here as well (#2114 review).
    """
    widths: list[int] = []
    real_pool = review_stage.ThreadPoolExecutor

    def _spy(*args, **kwargs):
        widths.append(kwargs["max_workers"])
        return real_pool(*args, **kwargs)

    monkeypatch.setattr(review_stage, "ThreadPoolExecutor", _spy)
    return widths


def test_width_bounds_live_launches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """With a width of two, three due nodes never run three at once."""
    _, head = _make_repo(tmp_path)
    recorder = _Recorder()
    release = threading.Event()
    widths = _pool_widths(monkeypatch)

    def _build(store: RunStore):
        inner = recorder.build(store)

        def _remediate(ctx: RemediationContext) -> None:
            inner(ctx)
            with recorder.lock:
                recorder.live += 1  # hold the slot until released
                recorder.peak = max(recorder.peak, recorder.live)
                if recorder.live == 2:
                    release.set()
            assert release.wait(BARRIER_TIMEOUT_S), "two launches never overlapped"
            with recorder.lock:
                recorder.live -= 1

        return _remediate

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _seed_needs_changes(store, head)
        outcome, _ = _drive(
            store,
            tmp_path,
            head,
            status=DriverStatus.REMEDIATING,
            max_concurrent=2,
            remediator=StoreBound(_build, store),
        )
    # The ceiling handed to the pool was the width, not the due count.
    assert widths == [2]
    # Two nodes held their slots until released, so a third worker thread would
    # have had to be spawned for the third node: the pool never spawned one.
    assert recorder.peak == 2
    assert len(recorder.threads) == 2
    assert outcome.remediating == list(ITEMS)


def test_width_never_exceeds_the_due_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A width above the number of due nodes still sizes the pool to the nodes."""
    _, head = _make_repo(tmp_path)
    recorder = _Recorder()
    widths = _pool_widths(monkeypatch)
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _seed_needs_changes(store, head)
        outcome, _ = _drive(
            store,
            tmp_path,
            head,
            status=DriverStatus.REMEDIATING,
            max_concurrent=8,
            remediator=StoreBound(recorder.build, store),
        )
    assert widths == [len(ITEMS)]
    assert outcome.remediating == list(ITEMS)


def test_review_concurrency_honours_max_parallel_and_fleet_cap(tmp_path: Path):
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        assert (
            _review_concurrency(store, epic_ref=EPIC, max_parallel=1, fleet_cap=8) == 1
        )
        assert (
            _review_concurrency(store, epic_ref=EPIC, max_parallel=3, fleet_cap=0) == 3
        )
        assert (
            _review_concurrency(store, epic_ref=EPIC, max_parallel=3, fleet_cap=8) == 3
        )
        # Another epic holds one slot of a two-slot fleet: one slot is free and
        # this epic's fair share is one, so the stage runs one at a time.
        assert store.reserve_epic_node_item("epic:y", "7", "run-7")
        assert (
            _review_concurrency(store, epic_ref=EPIC, max_parallel=3, fleet_cap=2) == 1
        )
        # A full fleet still floors at one: the reviewer's own atomic under-cap
        # reservation refuses it, as it did before.
        assert store.reserve_epic_node_item("epic:y", "8", "run-8")
        assert (
            _review_concurrency(store, epic_ref=EPIC, max_parallel=3, fleet_cap=2) == 1
        )


def test_one_raising_launch_does_not_stop_the_others(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    _, head = _make_repo(tmp_path)
    recorder = _Recorder(threading.Barrier(2, timeout=BARRIER_TIMEOUT_S))

    def _build(store: RunStore):
        inner = recorder.build(store)

        def _remediate(ctx: RemediationContext) -> None:
            if ctx.item_id == "42":
                raise RuntimeError("remediation infra exploded")
            inner(ctx)

        return _remediate

    caplog.set_level(logging.ERROR, logger="kew.graph.driver.review_stage")
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _seed_needs_changes(store, head)
        with pytest.raises(RuntimeError, match="remediation infra exploded"):
            _drive(
                store,
                tmp_path,
                head,
                status=DriverStatus.REMEDIATING,
                max_concurrent=3,
                remediator=StoreBound(_build, store),
            )
    # 41 and 43 still met at the barrier and finished.
    assert sorted(recorder.items) == ["41", "43"]
    assert recorder.peak == 2
    assert "42 review/remediation raised" in caplog.text


def test_a_locked_sibling_handle_degrades_only_its_own_node(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
):
    """One worker's handle failing to open must not abort the whole tick.

    Opening the handle runs before the per-node fail-closed boundary inside the
    drive, so a transient ledger write lock there once took every other due node
    down with it (#2114 review).
    """
    _, head = _make_repo(tmp_path)
    recorder = _Recorder(threading.Barrier(2, timeout=BARRIER_TIMEOUT_S))
    caplog.set_level(logging.ERROR, logger="kew.graph.driver.review_stage")
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _seed_needs_changes(store, head)
        real_open = store.open_sibling
        lock = threading.Lock()
        opened = 0

        def _open_sibling():
            nonlocal opened
            with lock:
                opened += 1
                first = opened == 1
            if first:
                raise sqlite3.OperationalError("database is locked")
            return real_open()

        monkeypatch.setattr(store, "open_sibling", _open_sibling)
        outcome, statuses = _drive(
            store,
            tmp_path,
            head,
            status=DriverStatus.REMEDIATING,
            max_concurrent=3,
            remediator=StoreBound(recorder.build, store),
        )
    # The one node without a handle stood still; the other two met at the barrier.
    assert len(outcome.failed) == 1
    assert sorted(outcome.failed + outcome.remediating) == list(ITEMS)
    assert recorder.peak == 2
    assert sorted(recorder.items) == sorted(outcome.remediating)
    # Nothing moved for the degraded node — the next tick retries it.
    assert statuses == dict.fromkeys(ITEMS, DriverStatus.REMEDIATING)
    assert "could not open its own ledger handle" in caplog.text


def _ready_after(barrier: threading.Barrier | None):
    def _launch(ctx: ReviewerLaunchContext) -> CollectedSidecar:
        if barrier is not None:
            barrier.wait()
        return stream_verdict(
            ctx,
            {
                "verdict": "READY",
                "findings": [],
                "acceptance_evidence": [
                    {"command": "uv run pytest -q", "output_digest": "sha256:x"}
                ],
            },
        )

    return _launch


def _seed_spent_review(store: RunStore, item: str) -> None:
    store.save(
        RunRecord(
            run_id=f"spent-{item}",
            issue_num=int(item),
            item_id=item,
            branch=f"kew/{item}",
            status="success",
            exit_code=0,
            started_at=NOW - timedelta(hours=1),
            finished_at=NOW - timedelta(minutes=50),
            estimated_cost_usd=5.0,
            run_kind=ADVERSARIAL_REVIEW_RUN_KIND,
        )
    )


def _ledger(store: RunStore, head: str) -> dict[str, object]:
    """The ledger rows the stage writes, stripped of run ids and timestamps
    (and of the scene's head sha, which differs between two scene repos)."""
    return {
        "runs": sorted(
            (r.item_id, r.status, r.error_category, r.run_kind)
            for item in ITEMS
            for r in store.list_by_item(item)
        ),
        "rounds": sorted(
            (item, r.verdict, r.head_sha == head)
            for item in ITEMS
            if (r := store.latest_review_round(item)) is not None
        ),
        "holds": sorted((h.item_id, h.reason) for h in store.list_epic_holds(EPIC)),
        "events": sorted(
            (e.event, e.item_id or "", str(e.payload.get("verdict", "")))
            for item in ITEMS
            for e in store.list_audit_events(item_id=item)
        ),
        "reservations": store.list_epic_reservations(REVIEW_EPIC_REF),
    }


def _budget_scene(
    tmp_path: Path, *, max_concurrent: int, barrier: threading.Barrier | None
) -> tuple[ReviewStageOutcome, dict[str, DriverStatus], dict[str, object]]:
    repo, head = _make_repo(tmp_path)
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        # 42 already spent past its review pool; 41 and 43 have spent nothing.
        _seed_spent_review(store, "42")
        outcome, statuses = _drive(
            store,
            tmp_path,
            head,
            status=DriverStatus.ADVERSARIAL_REVIEW,
            max_concurrent=max_concurrent,
            reviewer_launcher=_ready_after(barrier),
            config=_config(tmp_path, max_review_usd=1.0),
            repo_root=repo,
        )
        return outcome, statuses, _ledger(store, head)


def test_budget_refusal_refuses_only_its_node_and_matches_the_serial_ledger(
    tmp_path: Path,
):
    concurrent = _budget_scene(
        tmp_path / "concurrent",
        max_concurrent=3,
        # Only the two admitted reviewers launch; they must overlap.
        barrier=threading.Barrier(2, timeout=BARRIER_TIMEOUT_S),
    )
    serial = _budget_scene(tmp_path / "serial", max_concurrent=1, barrier=None)

    outcome, statuses, ledger = concurrent
    assert outcome.verdicts == {"41": "READY", "43": "READY"}
    assert outcome.dispatched == ["41", "43"]
    assert outcome.held == ["42"]
    assert statuses["42"] is DriverStatus.HELD
    assert statuses["41"] is statuses["43"] is DriverStatus.ADVERSARIAL_REVIEW
    [(held_item, reason)] = ledger["holds"]  # type: ignore[misc]
    assert held_item == "42" and reason.startswith("review_budget")
    assert ledger["reservations"] == []

    serial_outcome, serial_statuses, serial_ledger = serial
    assert outcome == serial_outcome
    assert statuses == serial_statuses
    assert ledger == serial_ledger


def test_line_scope_prefixes_only_its_own_thread(capsys: pytest.CaptureFixture):
    """The tick thread's line stays unprefixed WHILE a worker holds a scope.

    The scope has to be live on the other thread when the tick thread logs —
    with the worker joined first, a process-global would pass too (#2114 review).
    """
    scoped = threading.Event()
    tick_logged = threading.Event()

    def _worker() -> None:
        with line_scope("42"):
            scoped.set()
            assert tick_logged.wait(BARRIER_TIMEOUT_S), "tick line never printed"
            _log("Dispatching review for PR #142...")

    thread = threading.Thread(target=_worker)
    thread.start()
    try:
        assert scoped.wait(BARRIER_TIMEOUT_S), "worker never entered its scope"
        _log("tick line")
    finally:
        tick_logged.set()
        thread.join()
    err = capsys.readouterr().err.splitlines()
    assert err[0].endswith("] tick line") and "[42]" not in err[0]
    assert err[1].endswith("[42] Dispatching review for PR #142...")
