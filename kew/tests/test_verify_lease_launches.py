"""Every launch the drive makes yields to a running local merge verify (#3190).

The reviewer and frontier-dispatch guards are pinned in
``tests/test_verify_lease.py``. These cover the rest of the drive's launches:
a remediation, a merge-main step, the detached review stage (the production
path, which must reserve, record and spawn nothing), and a console run
request. Each one waits, unheld, with the deferral as its reason, records one
``launch_deferred`` event per verify, and launches on the first pass after the
verify ends. The lease is held by a real helper process
(``tests/support/verify_lease_holder.py``); nothing here sleeps.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from support.driver_fakes import REPO, FakeFactsReader, FakeSink, FakeSource, _node
from support.gh_fakes import install_placeholder_forge
from support.review_stream import stream_verdict
from support.verify_lease_holder import lease_held

from kew import run_requests as rr
from kew.config import DispatchConfig, LoggingConfig, ProjectConfig, ReviewConfig
from kew.drive import DriveLease, acquire_drive
from kew.drive_requests import RequestRunner
from kew.graph.build import build_graph
from kew.graph.driver.base_integration import (
    BaseIntegrationContext,
    BaseIntegrationOutcome,
)
from kew.graph.driver.facts import NodeFacts
from kew.graph.driver.overlay import DriverStatus
from kew.graph.driver.review_detach import DetachedReviewSpawner
from kew.graph.driver.review_stage import (
    RemediationContext,
    ReviewStageOutcome,
    drive_review_stage,
)
from kew.graph.driver.tick import advance, summarize
from kew.pseudo_epics import REVIEW_EPIC_REF
from kew.review_contract import CollectedSidecar
from kew.review_dispatch import ReviewerLaunchContext
from kew.run_store import REVIEW_STAGE_RUN_KIND, SqliteRunStore
from kew.subprocess_utils import run
from kew.verify_lease import LAUNCH_DEFERRED_EVENT

EPIC = "epic:x"
ITEM = "42"
REF = "refs/pull/7/merge"
DEFERRED = f"deferred: verify running ({REF}, since "


@pytest.fixture(autouse=True)
def _placeholder_forge(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ``gh`` call reaches GitHub (#3069)."""
    install_placeholder_forge(monkeypatch)


@pytest.fixture(autouse=True)
def _register_git(monkeypatch: pytest.MonkeyPatch) -> None:
    from kew.subprocess_utils import TOOL_PATHS

    git = shutil.which("git")
    assert git is not None, "git required"
    monkeypatch.setitem(TOOL_PATHS, "git", git)


def _deferrals(store: SqliteRunStore) -> list[tuple[str | None, str]]:
    return [
        (event.item_id, (event.payload or {})["launch"])
        for event in store.list_audit_events(event=LAUNCH_DEFERRED_EVENT)
    ]


def _make_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    run(["git", "config", "user.email", "t@t.t"], cwd=repo, check=True)
    run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "code.py").write_text("x = 1\n")
    run(["git", "add", "-A"], cwd=repo, check=True)
    run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)
    head = run(["git", "rev-parse", "HEAD"], cwd=repo, check=True).stdout.strip()
    return repo, head


def _facts(head: str, *, merge_state: str | None = None) -> NodeFacts:
    return NodeFacts(
        issue_num=int(ITEM),
        state="OPEN",
        state_reason=None,
        labels=frozenset(),
        pr_number=7,
        pr_url=None,
        review_decision=None,
        merge_state_status=merge_state,
        head_oid=head,
        pr_pushed_at=None,
    )


def _config(tmp_path: Path) -> DispatchConfig:
    return DispatchConfig(
        project=ProjectConfig(repo=REPO),
        review=ReviewConfig(
            adversarial=True,
            blocking=True,
            max_rounds=3,
            publish_status=False,
            publish_comment=False,
        ),
        logging=LoggingConfig(dir=str(tmp_path / "logs")),
    )


def _needs_changes(ctx: ReviewerLaunchContext) -> CollectedSidecar:
    finding = {
        "severity": "high",
        "location": "code.py:1",
        "summary": "unchecked path",
        "evidence": {"command": "uv run pytest -q", "output_digest": "sha256:x"},
    }
    return stream_verdict(ctx, {"verdict": "NEEDS_CHANGES", "findings": [finding]})


def _stage(
    store: SqliteRunStore,
    repo: Path,
    facts: NodeFacts,
    status: DriverStatus,
    *,
    config: DispatchConfig,
    **seams: Any,
) -> ReviewStageOutcome:
    return drive_review_stage(
        {ITEM: status},
        {ITEM: facts},
        epic_ref=EPIC,
        config=config,
        store=store,
        repo=REPO,
        repo_root=repo,
        base="main",
        reviewer_launcher=seams.pop("reviewer_launcher", None),
        worker_cap=4,
        **seams,
    )


# --- a remediation (F1) ---


def test_a_due_remediation_waits_for_the_verify_then_launches(tmp_path: Path) -> None:
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path)
    remediations: list[RemediationContext] = []

    def remediator(ctx: RemediationContext) -> None:
        remediations.append(ctx)

    def remediate() -> ReviewStageOutcome:
        return _stage(
            store,
            repo,
            _facts(head),
            DriverStatus.REMEDIATING,
            config=config,
            remediator=remediator,
        )

    with SqliteRunStore(str(tmp_path / "run.db")) as store:
        _stage(
            store,
            repo,
            _facts(head),
            DriverStatus.ADVERSARIAL_REVIEW,
            config=config,
            reviewer_launcher=_needs_changes,
        )
        latest = store.latest_review_round(ITEM)
        assert latest is not None and latest.verdict == "NEEDS_CHANGES"

        with lease_held(REF) as holder:
            first = remediate()
            remediate()
            assert remediations == []
            assert first.refused[ITEM].startswith(DEFERRED)
            assert first.remediating == [] and first.held == []
            assert store.list_epic_holds(EPIC) == []
            assert _deferrals(store) == [(ITEM, "remediation")]
            holder.release()

        after = remediate()

    assert [c.head_sha for c in remediations] == [head]
    assert after.remediating == [ITEM] and ITEM not in after.refused


# --- a merge-main step (F1) ---


def test_a_due_merge_main_step_waits_for_the_verify_then_runs(tmp_path: Path) -> None:
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path)
    steps: list[BaseIntegrationContext] = []

    def integrator(ctx: BaseIntegrationContext) -> BaseIntegrationOutcome:
        steps.append(ctx)
        return BaseIntegrationOutcome("integrated")

    def integrate() -> ReviewStageOutcome:
        return _stage(
            store,
            repo,
            _facts(head, merge_state="DIRTY"),
            DriverStatus.ADVERSARIAL_REVIEW,
            config=config,
            reviewer_launcher=_needs_changes,
            base_integrator=integrator,
        )

    with SqliteRunStore(str(tmp_path / "run.db")) as store:
        with lease_held(REF) as holder:
            first = integrate()
            assert steps == []
            assert first.refused[ITEM].startswith(DEFERRED)
            assert first.integrated == {} and first.held == []
            assert first.dispatched == []  # nor a reviewer in its place
            assert store.latest_review_round(ITEM) is None
            assert store.list_epic_holds(EPIC) == []
            assert _deferrals(store) == [(ITEM, "merge-main")]
            holder.release()

        after = integrate()

    assert [c.head_sha for c in steps] == [head]
    assert ITEM in after.integrated


# --- the detached review stage, through a real tick (F2) ---


class _Children:
    """The process boundary of the detached stage: every ``kew epic
    _review-node`` the spawner starts, never run."""

    def __init__(self) -> None:
        self.started: list[list[str]] = []

    def __call__(self, cmd: list[str], **_kwargs: object) -> Any:
        self.started.append(cmd)
        return self

    def poll(self) -> None:
        return None


def _tick(
    store: SqliteRunStore,
    tmp_path: Path,
    head: str,
    *,
    config: DispatchConfig,
    children: _Children,
) -> Any:
    return advance(
        EPIC,
        config=config,
        store=store,
        deps=FakeSource(nodes=[_node(ITEM)], edges=[]),
        facts=_reader(head),
        dispatch_sink=FakeSink(),
        max_parallel=3,
        now=datetime(2026, 10, 26, 12, 0, tzinfo=UTC),
        lock_path=tmp_path / "epic.lock",
        approvers=["human"],
        repo=REPO,
        review_detacher=DetachedReviewSpawner(config, base="main", popen=children),
    )


def _reader(head: str) -> FakeFactsReader:
    return FakeFactsReader(
        issue_facts_by_num={int(ITEM): _facts(head, merge_state="CLEAN")},
        pr_facts_by_num={
            7: {
                "number": 7,
                "state": "OPEN",
                "headRefOid": head,
                "mergeStateStatus": "CLEAN",
            }
        },
    )


def _stage_rows(store: SqliteRunStore) -> list[Any]:
    return [r for r in store.list_by_item(ITEM) if r.run_kind == REVIEW_STAGE_RUN_KIND]


def test_a_detached_review_stage_starts_nothing_while_a_verify_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The production path: no reservation, no ``review_stage`` row, no log
    and no child while the lease is held; the tick and ``kew epic status`` both
    name the deferral; the first tick after the verify starts the child."""
    repo, head = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    config = _config(tmp_path)
    children = _Children()
    log_dir = Path(config.logging.dir)

    with SqliteRunStore(str(tmp_path / "d.db"), project_root=tmp_path) as store:
        with lease_held(REF) as holder:
            for _ in range(2):
                result = _tick(store, tmp_path, head, config=config, children=children)
                waiting = {w.node_id: w.reason for w in result.waiting}
                assert waiting.get(ITEM, "").startswith(DEFERRED), waiting
            assert children.started == []
            assert store.list_epic_reservations(REVIEW_EPIC_REF) == []
            assert _stage_rows(store) == []
            assert list(log_dir.glob("review-stage-*.log")) == []
            assert _deferrals(store) == [(ITEM, "review")]

            status = summarize(
                EPIC,
                build_graph(FakeSource(nodes=[_node(ITEM)], edges=[]), EPIC),
                config=config,
                store=store,
                facts=_reader(head),
            )
            shown = {w.node_id: w.reason for w in status.waiting}
            assert shown.get(ITEM, "").startswith(DEFERRED), shown
            holder.release()

        _tick(store, tmp_path, head, config=config, children=children)

        assert len(children.started) == 1
        assert children.started[0][:3] == ["kew", "epic", "_review-node"]
        [row] = _stage_rows(store)
        assert row.status == "pending"
        assert [r.item_id for r in store.list_epic_reservations(REVIEW_EPIC_REF)] == [
            ITEM
        ]


# --- a console run request (F5) ---


def _requests(
    store: SqliteRunStore,
    config: DispatchConfig,
    spawned: list[list[str]],
    clock: Callable[[], datetime] | None = None,
) -> RequestRunner:
    class _Exited:
        pid = 4242

        def poll(self) -> int:
            return 0

    def spawn(argv: list[str], *_args: Any) -> Any:
        spawned.append(argv)
        return _Exited()

    lease = acquire_drive(store)
    assert isinstance(lease, DriveLease)
    runner = RequestRunner(
        store, lease, config, Path(config.logging.dir), spawn=spawn, say=lambda _m: None
    )
    if clock is not None:
        runner.clock = clock
    return runner


class _Clock:
    """The drive's clock, moved by the test: never a sleep."""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def test_time_deferred_behind_a_verify_does_not_age_a_request_past_its_ttl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dispatch queued, then deferred for three hours behind a running
    verify, still launches after it: the queue TTL counts only the time it
    was queued and not waiting. Time queued without a wait still counts, to
    the second."""
    monkeypatch.setenv("KEW_ACTOR", "tom")
    config = _config(tmp_path)
    Path(config.logging.dir).mkdir(parents=True)
    spawned: list[list[str]] = []
    start = datetime.now(UTC).replace(microsecond=0)
    clock = _Clock(start)

    with SqliteRunStore(str(tmp_path / "run.db")) as store:
        request, _ = rr.queue(
            store, "kew dispatch 7", operator="tom", source="web", now=start
        )
        runner = _requests(store, config, spawned, clock)
        with lease_held(REF) as holder:
            clock.now = start + timedelta(minutes=10)  # queued, not yet waiting
            runner()
            while clock.now < start + rr.QUEUE_TTL * 3:
                clock.now += rr.WAIT_CREDIT_STEP
                runner()
            waiting = rr.get_request(store, request.id)
            assert waiting is not None and waiting.state == rr.QUEUED, waiting
            assert spawned == []
            holder.release()

        waited = timedelta(seconds=waiting.waited_seconds)
        assert waited == clock.now - (start + timedelta(minutes=10))
        boundary = start + rr.QUEUE_TTL + waited
        assert not waiting.stale(boundary)
        assert waiting.stale(boundary + timedelta(seconds=1))

        clock.now += timedelta(seconds=30)
        runner()
        started = rr.get_request(store, request.id)

    assert len(spawned) == 1
    assert started is not None and started.state == rr.RUNNING


def _host_reading(load: float) -> Callable[..., Any]:
    from kew.drive_host_load import HostReading

    return lambda *, swap: HostReading(load=load, cpus=8, swap=0.0)


def _queue_and_wait(
    store: SqliteRunStore,
    runner: RequestRunner,
    clock: _Clock,
    start: datetime,
) -> rr.RunRequest:
    """Queue a dispatch at *start*, then run a pass every credit step until
    five minutes past the TTL; the request as it stands then."""
    request, _ = rr.queue(
        store, "kew dispatch 7", operator="tom", source="web", now=start
    )
    while clock.now < start + rr.QUEUE_TTL + timedelta(minutes=5):
        clock.now += rr.WAIT_CREDIT_STEP
        runner()
    found = rr.get_request(store, request.id)
    assert found is not None
    return found


def test_a_host_load_wait_still_ages_a_request_past_its_ttl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The host-load gate is #2864's to decide, not #3190's: a click the load
    holds for over an hour is refused as stale, as on main."""
    from kew.drive import host_gate

    monkeypatch.setenv("KEW_ACTOR", "tom")
    config = _config(tmp_path)
    config.drive.max_load_per_cpu = 4
    Path(config.logging.dir).mkdir(parents=True)
    spawned: list[list[str]] = []
    start = datetime.now(UTC).replace(microsecond=0)
    clock = _Clock(start)

    with SqliteRunStore(str(tmp_path / "run.db")) as store:
        host_gate(store, config, _host_reading(98.0))
        runner = _requests(store, config, spawned, clock)
        refused = _queue_and_wait(store, runner, clock, start)

    assert refused.state == rr.REFUSED, refused
    assert "never started" in (refused.detail or "")
    assert refused.waited_seconds == 0.0
    assert spawned == []


def test_a_verify_wait_does_not_age_a_request_past_its_ttl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same hour and five minutes spent behind a running verify: still
    queued, and it launches once the verify ends."""
    monkeypatch.setenv("KEW_ACTOR", "tom")
    config = _config(tmp_path)
    Path(config.logging.dir).mkdir(parents=True)
    spawned: list[list[str]] = []
    start = datetime.now(UTC).replace(microsecond=0)
    clock = _Clock(start)

    with SqliteRunStore(str(tmp_path / "run.db")) as store:
        runner = _requests(store, config, spawned, clock)
        with lease_held(REF):
            waiting = _queue_and_wait(store, runner, clock, start)
        assert waiting.state == rr.QUEUED, waiting
        assert spawned == []

        clock.now += timedelta(seconds=30)
        runner()
        started = rr.get_request(store, waiting.id)

    assert started is not None and started.state == rr.RUNNING
    assert len(spawned) == 1


def test_a_request_never_deferred_is_still_refused_past_its_ttl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KEW_ACTOR", "tom")
    config = _config(tmp_path)
    Path(config.logging.dir).mkdir(parents=True)
    spawned: list[list[str]] = []
    start = datetime.now(UTC).replace(microsecond=0)
    clock = _Clock(start + rr.QUEUE_TTL + timedelta(seconds=1))

    with SqliteRunStore(str(tmp_path / "run.db")) as store:
        request, _ = rr.queue(
            store, "kew dispatch 7", operator="tom", source="web", now=start
        )
        _requests(store, config, spawned, clock)()
        refused = rr.get_request(store, request.id)

    assert spawned == []
    assert refused is not None and refused.state == rr.REFUSED


@pytest.mark.parametrize(
    "command",
    ["kew dispatch 7", "kew epic continue epic:a 7"],
    ids=["dispatch", "continue"],
)
def test_a_launching_run_request_stays_queued_while_a_verify_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    monkeypatch.setenv("KEW_ACTOR", "tom")
    config = _config(tmp_path)
    Path(config.logging.dir).mkdir(parents=True)
    spawned: list[list[str]] = []

    with SqliteRunStore(str(tmp_path / "run.db")) as store:
        request, _ = rr.queue(store, command, operator="tom", source="web")
        runner = _requests(store, config, spawned)
        with lease_held(REF) as holder:
            runner()
            runner()
            waiting = rr.get_request(store, request.id)
            assert waiting is not None and waiting.state == rr.QUEUED
            assert (waiting.detail or "").startswith(f"waiting: {DEFERRED}")
            assert spawned == []
            assert [launch for _, launch in _deferrals(store)] == ["run-request"]
            holder.release()

        runner()
        started = rr.get_request(store, request.id)

    assert len(spawned) == 1
    assert started is not None and started.state != rr.QUEUED


def test_a_ledger_only_run_request_runs_beside_a_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unhold launches nothing itself: the tick it lets launch yields."""
    monkeypatch.setenv("KEW_ACTOR", "tom")
    config = _config(tmp_path)
    Path(config.logging.dir).mkdir(parents=True)
    spawned: list[list[str]] = []

    with SqliteRunStore(str(tmp_path / "run.db")) as store:
        rr.queue(store, "kew epic unhold epic:a 7", operator="tom", source="web")
        runner = _requests(store, config, spawned)
        with lease_held(REF):
            runner()
        assert _deferrals(store) == []

    assert len(spawned) == 1
