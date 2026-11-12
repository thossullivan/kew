"""The drive yields to a running local merge verify (#3190).

Every lease here is held by a real helper process
(``tests/support/verify_lease_holder.py``) through the production
``hold_verify_lease``; the tests wait on the line it prints once it holds the
lease, never on a sleep.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from support import driver_fakes as _fx
from support.driver_fakes import (
    FakeFactsReader,
    FakeSink,
    FakeSource,
    _advance,
    _facts,
    _node,
)
from support.held_out_log import opened
from support.review_stream import stream_verdict
from support.verify_lease_holder import LeaseHolder, lease_held, probe_in_progress
from typer.testing import CliRunner

from kew import work_dirs
from kew.cli import app
from kew.command_outcome import CommandOutcome
from kew.config import (
    DispatchConfig,
    DriveConfig,
    LoggingConfig,
    ParallelConfig,
    ReviewConfig,
)
from kew.graph.driver.facts import NodeFacts
from kew.graph.driver.overlay import DriverStatus
from kew.graph.driver.review_stage import drive_review_stage
from kew.review_contract import CollectedSidecar
from kew.review_dispatch import ReviewerLaunchContext
from kew.run_store import SqliteRunStore
from kew.subprocess_utils import run
from kew.verify_lease import (
    LAUNCH_DEFERRED_EVENT,
    VERIFY_LAUNCHES_KEY,
    VerifyLeaseBusy,
    hold_verify_lease,
    verify_deferral,
    verify_running,
)

store = _fx.store
_no_real_git = _fx._no_real_git
_isolate_tool_paths = _fx._isolate_tool_paths

EPIC = "epic:x"
ITEM = "42"
REF = "refs/pull/7/merge"


def _payloads(store: SqliteRunStore) -> list[dict[str, Any]]:
    rows = store.list_audit_events(event=LAUNCH_DEFERRED_EVENT)
    out = []
    for row in rows:
        payload = row.payload
        out.append(payload if isinstance(payload, dict) else json.loads(payload))
    return out


@pytest.fixture(autouse=True)
def _register_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fixture repositories are built with git, the local host runs sh."""
    from kew.subprocess_utils import TOOL_PATHS

    for tool in ("git", "sh"):
        found = shutil.which(tool)
        assert found is not None, f"{tool} required"
        monkeypatch.setitem(TOOL_PATHS, tool, found)


# --- the lease itself ---


def test_a_held_lease_reads_its_record_and_a_released_one_reads_free() -> None:
    assert verify_running() is None
    with lease_held(REF) as holder:
        lease = verify_running()
        assert lease is not None
        assert lease.ref == REF
        assert lease.pid == holder.process.pid
        assert lease.token
        assert lease.deferral() == (
            f"deferred: verify running ({REF}, since {lease.started_at})"
        )
        holder.release()
        assert verify_running() is None


def test_a_killed_holder_leaves_the_lease_free_at_once() -> None:
    """SIGKILL runs no ``finally``: the kernel drops the lock, and the record it
    left behind is never read as a running verify."""
    with lease_held(REF) as holder:
        assert verify_running() is not None
        holder.kill()
        assert verify_running() is None
        # Nothing is held over: a new verify takes the lease without waiting.
        with hold_verify_lease("next") as lease:
            running = verify_running()  # another descriptor, so it reads held
            assert running is not None and running.token == lease.token


# --- the spawn guard ---


def _store(tmp_path: Path) -> SqliteRunStore:
    return SqliteRunStore(str(tmp_path / "run.db"))


def test_launches_during_verify_admits_that_many_then_defers(tmp_path: Path) -> None:
    config = DispatchConfig(drive=DriveConfig(launches_during_verify=1))
    with _store(tmp_path) as store, lease_held(REF):
        first = verify_deferral(store, config, item_id="1", kind="dispatch")
        second = verify_deferral(store, config, item_id="2", kind="dispatch")

        assert first is None
        assert second is not None and second.startswith("deferred: verify running")
        assert [p["launch"] for p in _payloads(store)] == ["dispatch"]
        assert [
            e.item_id for e in store.list_audit_events(event=LAUNCH_DEFERRED_EVENT)
        ] == ["2"]


def test_each_verify_gets_its_own_launch_allowance(tmp_path: Path) -> None:
    config = DispatchConfig(drive=DriveConfig(launches_during_verify=1))
    with _store(tmp_path) as store:
        with lease_held(REF) as holder:
            assert verify_deferral(store, config, item_id="1", kind="review") is None
            assert verify_deferral(store, config, item_id="2", kind="review")
            holder.release()
        with lease_held("refs/pull/8/merge"):
            assert verify_deferral(store, config, item_id="2", kind="review") is None


def test_a_free_lease_defers_nothing_and_records_nothing(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        assert (
            verify_deferral(store, DispatchConfig(), item_id="1", kind="review") is None
        )
        assert store.list_audit_events(event=LAUNCH_DEFERRED_EVENT) == []


def test_an_unreadable_launch_count_warns_and_counts_afresh(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    config = DispatchConfig(drive=DriveConfig(launches_during_verify=1))
    with _store(tmp_path) as store, lease_held(REF):
        store.set_meta(VERIFY_LAUNCHES_KEY, "{not json")
        with caplog.at_level(logging.WARNING, logger="kew.verify_lease"):
            first = verify_deferral(store, config, item_id="1", kind="dispatch")
        second = verify_deferral(store, config, item_id="2", kind="dispatch")

        assert first is None
        assert second is not None
        assert f"unreadable {VERIFY_LAUNCHES_KEY} row" in caplog.text


def test_a_missing_lease_file_reads_free_without_creating_it(tmp_path: Path) -> None:
    lease = tmp_path / "root" / "verify.lease"
    assert verify_running(lease) is None
    assert not lease.parent.exists()


def test_two_probes_at_once_with_no_verify_both_read_free(tmp_path: Path) -> None:
    """A probe caught mid-probe in another process holds the probe's lock; a
    second probe at that instant still reads no verify, and records nothing."""
    lease = work_dirs.verify_lease_path()
    lease.touch()
    with _store(tmp_path) as store, probe_in_progress(lease):
        assert verify_running() is None
        assert (
            verify_deferral(store, DispatchConfig(), item_id="1", kind="dispatch")
            is None
        )
        assert store.list_audit_events(event=LAUNCH_DEFERRED_EVENT) == []
    with lease_held(REF):  # the probe still sees a real verify
        assert verify_running() is not None


def _within(seconds: float, call: Callable[[], Any]) -> Any:
    """Run *call* on a thread and fail if it has not returned in *seconds*:
    a lease taken with a blocking wait would never return here."""
    box: dict[str, Any] = {}

    def _run() -> None:
        try:
            box["value"] = call()
        except BaseException as exc:  # noqa: BLE001 - re-raised on the test thread
            box["error"] = exc

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(seconds)
    assert not thread.is_alive(), f"still waiting after {seconds}s: it blocked"
    if "error" in box:
        raise box["error"]
    return box.get("value")


def test_a_second_lease_is_refused_at_once_naming_the_running_verify() -> None:
    with lease_held(REF) as holder:
        running = verify_running()
        assert running is not None

        def _second() -> None:
            with hold_verify_lease("second"):
                raise AssertionError("a second verify took a held lease")

        with pytest.raises(VerifyLeaseBusy) as refused:
            _within(30, _second)

    message = str(refused.value)
    assert f"pid {holder.process.pid}" in message
    assert f"{REF}, since {running.started_at}" in message


# --- a due review defers while the lease is held ---


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


def _review_facts(head: str) -> NodeFacts:
    return NodeFacts(
        issue_num=int(ITEM),
        state="OPEN",
        state_reason=None,
        labels=frozenset(),
        pr_number=7,
        pr_url=None,
        review_decision=None,
        merge_state_status=None,
        head_oid=head,
        pr_pushed_at=None,
    )


class _Reviewer:
    """The fake reviewer: an evidenced READY verdict, counted per call."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, ctx: ReviewerLaunchContext) -> CollectedSidecar:
        self.calls += 1
        evidence = {"command": "uv run pytest -q", "output_digest": "sha256:beef"}
        sidecar = {
            "verdict": "READY",
            "findings": [],
            "acceptance_evidence": [evidence],
        }
        return stream_verdict(ctx, sidecar)


def _review_tick(
    store: SqliteRunStore,
    config: DispatchConfig,
    repo: Path,
    head: str,
    reviewer: _Reviewer,
) -> Any:
    return drive_review_stage(
        {ITEM: DriverStatus.ADVERSARIAL_REVIEW},
        {ITEM: _review_facts(head)},
        epic_ref=EPIC,
        config=config,
        store=store,
        repo="o/r",
        repo_root=repo,
        base="main",
        reviewer_launcher=reviewer,
    )


def test_a_due_review_defers_once_per_verify_then_launches_after_it(
    tmp_path: Path,
) -> None:
    repo, head = _make_repo(tmp_path)
    config = DispatchConfig(
        review=ReviewConfig(adversarial=True, blocking=True),
        logging=LoggingConfig(dir=str(tmp_path / "logs")),
    )
    reviewer = _Reviewer()
    with _store(tmp_path) as store:
        with lease_held(REF) as holder:
            first = _review_tick(store, config, repo, head, reviewer)
            assert reviewer.calls == 0
            assert first.refused[ITEM].startswith(f"deferred: verify running ({REF}")
            events = store.list_audit_events(event=LAUNCH_DEFERRED_EVENT)
            assert [e.item_id for e in events] == [ITEM]
            assert _payloads(store)[0]["reason"] == first.refused[ITEM]

            for _ in range(2):
                _review_tick(store, config, repo, head, reviewer)
            assert reviewer.calls == 0
            assert len(store.list_audit_events(event=LAUNCH_DEFERRED_EVENT)) == 1
            holder.release()

        after = _review_tick(store, config, repo, head, reviewer)

        assert reviewer.calls == 1
        assert ITEM not in after.refused
        latest = store.latest_review_round(ITEM)
        assert latest is not None and latest.verdict == "READY"
        assert len(store.list_audit_events(event=LAUNCH_DEFERRED_EVENT)) == 1


# --- a dispatch the lease was taken for after admission ---


class _LeaseTakingSink(FakeSink):
    """Dispatching the first node starts a verify: the lease is taken after
    the tick admitted, before the next node's spawn."""

    def __init__(self) -> None:
        super().__init__()
        self.holder: LeaseHolder | None = None

    def dispatch(self, item_id: str) -> bool:
        if self.holder is None:
            self.holder = LeaseHolder(REF)
        return super().dispatch(item_id)


def test_a_verify_that_starts_mid_tick_defers_the_launches_after_it(
    tmp_path: Path, store: Any
) -> None:
    deps = FakeSource(nodes=[_node("1"), _node("2")], edges=[])
    facts = FakeFactsReader(
        issue_facts_by_num={1: _facts(1, state="OPEN"), 2: _facts(2, state="OPEN")}
    )
    sink = _LeaseTakingSink()
    try:
        result = _advance(
            deps=deps,
            facts=facts,
            store=store,
            sink=sink,
            lock_path=tmp_path / "l",
            config=DispatchConfig(parallel=ParallelConfig(max_workers=5)),
        )
        assert sink.calls == ["1"]
        assert result.dispatched == ("1",)
        events = store.list_audit_events(event=LAUNCH_DEFERRED_EVENT)
        assert [e.item_id for e in events] == ["2"]
        assert _payloads(store)[0]["launch"] == "dispatch"
        waiting = {w.node_id: w.reason for w in result.waiting}
        assert "deferred: verify running" in waiting.get("2", "")
    finally:
        if sink.holder is not None:
            sink.holder.release()


# --- a local verify holds the lease for its whole job ---


_GIT = ["git", "-c", "user.email=t@example.com", "-c", "user.name=t"]
_HELD_OUT_PASSED = opened("kew-held-out: exit 0 seconds 1\n" * 2)


def _project(tmp_path: Path, *, here: bool = False) -> Path:
    """A candidate branch over main; *here* builds it in *tmp_path* itself,
    where the CLI's project root points in tests."""
    repo = tmp_path if here else tmp_path / "project"
    (repo / "kew").mkdir(parents=True)
    for args in (
        ["init", "--quiet", "-b", "main", "."],
        ["commit", "--quiet", "--allow-empty", "-m", "base"],
        ["checkout", "--quiet", "-b", "candidate"],
    ):
        subprocess.run([*_GIT, *args], cwd=repo, check=True, capture_output=True)
    (repo / "kew" / "module.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run([*_GIT, "add", "--all"], cwd=repo, check=True)
    subprocess.run(
        [*_GIT, "commit", "--quiet", "-m", "candidate"], cwd=repo, check=True
    )
    return repo


@pytest.mark.parametrize(
    ("answer", "outcome"),
    [
        ((0, "gate passed\n" + _HELD_OUT_PASSED), "passed"),
        ((3, "gate failed\n"), "failed"),
        (TimeoutError("the job ran past its timeout"), "raised"),
    ],
    ids=["success", "failure", "timeout"],
)
def test_a_local_verify_holds_the_lease_for_its_job_and_releases_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    answer: tuple[int, str] | Exception,
    outcome: str,
) -> None:
    from kew.config import VerifyConfig
    from kew.hosts import Job, LocalProvider
    from kew.merge_verify import run_verify

    repo = _project(tmp_path)
    config = DispatchConfig()
    config.project.repo = "o/r"
    config.logging.dir = str(tmp_path / "logs")
    config.verify = VerifyConfig(host="local")
    seen: list[Any] = []

    def _execute(self: LocalProvider, job: Job) -> tuple[int, str]:
        # Every job — the gate job and, behind a clean one, the suite — runs here.
        seen.append(verify_running())
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(LocalProvider, "_execute", _execute)
    with _store(tmp_path) as store:
        try:
            _, result = run_verify(config, store=store, repo_root=repo)
        except TimeoutError:
            assert outcome == "raised"
        else:
            assert result.outcome == outcome

    # A clean gate job is followed by the suite job, under the same lease.
    assert len(seen) == (2 if outcome == "passed" else 1)
    assert all(lease is not None and lease == seen[0] for lease in seen)
    assert seen[0].ref.startswith("HEAD (")
    assert verify_running() is None
    assert work_dirs.verify_lease_path().exists()


def test_a_local_verify_beside_a_running_one_is_refused_before_any_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No wait behind the running verify: the drive's merge pass runs verifies
    in its own process, and a wait would freeze it. The refusal comes before
    the job, so nothing runs and no verify event is recorded."""
    from kew.config import VerifyConfig
    from kew.hosts import Job, LocalProvider
    from kew.merge_verify import VERIFY_EVENT, VerifyRefused, run_verify

    repo = _project(tmp_path)
    config = DispatchConfig()
    config.project.repo = "o/r"
    config.logging.dir = str(tmp_path / "logs")
    config.verify = VerifyConfig(host="local")
    jobs: list[Job] = []

    def _execute(self: LocalProvider, job: Job) -> tuple[int, str]:
        jobs.append(job)
        return 0, "gate passed\n" + _HELD_OUT_PASSED

    monkeypatch.setattr(LocalProvider, "_execute", _execute)
    with _store(tmp_path) as store, lease_held(REF) as holder:
        running = verify_running()
        assert running is not None
        with pytest.raises(VerifyRefused) as refused:
            _within(30, lambda: run_verify(config, store=store, repo_root=repo))
        assert store.list_audit_events(event=VERIFY_EVENT) == []

    assert jobs == []
    assert f"pid {holder.process.pid}" in str(refused.value)
    assert f"{REF}, since {running.started_at}" in str(refused.value)


def test_a_hand_verify_beside_a_running_one_exits_as_never_run_naming_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _project(tmp_path, here=True)
    (repo / "kew.toml").write_text(
        '[project]\nrepo = "o/r"\n'
        f"[logging]\ndir = {str(repo / 'logs')!r}\n"
        '[verify]\nhost = "local"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("KEW_PROJECT", str(repo))
    monkeypatch.chdir(repo)

    with lease_held(REF) as holder:
        running = verify_running()
        assert running is not None
        result = _within(
            30, lambda: CliRunner().invoke(app, ["verify", "--host", "local"])
        )

    assert result.exit_code == 2, result.output
    assert "a local merge verify is already running" in result.output
    assert f"pid {holder.process.pid}" in result.output
    assert f"{REF}, since {running.started_at}" in result.output


# --- a hand-started review is never blocked ---


def test_a_hand_review_launches_and_names_the_running_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "kew.toml").write_text(
        f'[project]\nrepo = "owner/repo"\n\n[logging]\ndir = "{tmp_path / "logs"}"\n',
        encoding="utf-8",
    )
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("kew.cli.preflight_tools", lambda: None)
    monkeypatch.setattr(  # the gh auth probe answers authenticated
        "kew.cli.run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "", "")
    )
    launched: list[int] = []

    def _review(**kwargs: Any) -> CommandOutcome:
        launched.append(kwargs["pr_number"])
        return CommandOutcome("executed", 0, run_id="review-run")

    with lease_held(REF), patch("kew.cli.dispatch_review", side_effect=_review):
        result = CliRunner().invoke(app, ["review", "7"])

    assert launched == [7], result.output
    assert f"a local merge verify is running ({REF}, since" in result.output
