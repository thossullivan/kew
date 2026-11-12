"""Unit tests for the pure dashboard aggregator (#700, Group 1).

Covers the DTO shape, the discriminated-union row types + additivity, per-source
fail-soft degradation (one flaky reader never sinks the payload), and the banner
truth table. The gh-backed ready queue is stubbed so no network is touched.
"""

from __future__ import annotations

import ast
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from support import source_ast
from support.dashboard_fakes import (
    FakeAuditEvent,
    FakeDriverState,
    FakeEpicTick,
    FakeRun,
    FakeStore,
)

from kew.config import (
    DispatchConfig,
    LabelConfig,
    ProjectConfig,
    RouteEntry,
    RoutingConfig,
)
from kew.epic_archive import EPIC_ARCHIVED_EVENT, EPIC_UNARCHIVED_EVENT
from kew.epic_holds import BUDGET_HOLD_PREFIX, CI_PENDING_HOLD_REASON
from kew.run_store import EpicHold, OrphanPRRecord
from kew.service import dashboard, needs_you
from kew.service.models import (
    DashboardDTO,
    DashboardRowDTO,
    QueueCandidateDTO,
)

NOW = datetime(2026, 7, 16, 12, 0, 0, tzinfo=UTC)


def _config() -> DispatchConfig:
    return DispatchConfig(
        project=ProjectConfig(repo=""),
        routing=RoutingConfig(
            default="claude-code",
            simple=RouteEntry(runner="claude-code", model="claude-haiku"),
        ),
    )


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Stub the gh-backed ready queue so unit tests never shell out, and start
    every test with empty read caches and no held failures."""
    monkeypatch.setattr(dashboard, "_ready_issues", lambda config: [])
    dashboard._ready_cache.clear()
    dashboard._candidates_cache.clear()
    dashboard._read_failures.clear()


def _wait_for(condition: Callable[[], bool]) -> None:
    """Block until ``condition()`` holds — a background refill landing. Counted
    in sleeps, not read off a clock: some tests here freeze ``time.monotonic``."""
    for _ in range(500):
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError("the background refill never landed")


def _rows_by_type(dto: DashboardDTO) -> dict[str, DashboardRowDTO]:
    return {r.type: r for r in dto.rows}


def test_empty_store_yields_well_formed_payload():
    dto = dashboard.assemble(FakeStore(), _config(), now=NOW)
    assert isinstance(dto, DashboardDTO)
    assert dto.rows == []
    assert dto.unavailable == []
    assert dto.banner.fleet_stopped is False
    assert dto.banner.no_live_driver is False
    # every source that ran successfully stamps as_of
    assert "approval_holds" in dto.as_of
    assert dto.glance.fleet is not None
    assert dto.glance.caps is not None


def test_all_five_row_types_present_with_consequence_classes():
    store = FakeStore(
        audit_events=[
            FakeAuditEvent("approval_hold", 41, {"estimate_usd": 3.0, "basis": "est"}),
        ],
        # An operator-clearable hold: ``ci_pending`` is the CI gate, which
        # ``kew.waits.classify_wait`` keeps OUT of the needs-you set (a person
        # cannot make a check finish), so it raises no budget_hold row (#2207).
        epic_holds=[EpicHold(42, "epic:x", f"{BUDGET_HOLD_PREFIX}: cap reached", NOW)],
        orphan_prs=[
            OrphanPRRecord(99, "http://pr/99", "agent/99", "no-record", NOW, None)
        ],
        syncable=[
            FakeRun("r1", 43, pr_state="OPEN", pr_url="http://pr/43", finished_at=NOW)
        ],
        epic_refs=["epic:x"],
        driver_states={
            "epic:x": FakeDriverState(
                "epic:x",
                last_tick_started_at=(NOW - timedelta(hours=1)).isoformat(),
                interval_s=60.0,
            )
        },
    )
    dto = dashboard.assemble(store, _config(), now=NOW)
    by_type = _rows_by_type(dto)
    assert set(by_type) == {
        "approval_hold",
        "budget_hold",
        "stalled_epic",
        "pr_review",
        "orphan_pr",
    }
    assert by_type["approval_hold"].consequence == "money"
    assert by_type["approval_hold"].estimate_usd == 3.0
    assert by_type["budget_hold"].consequence == "money"
    assert by_type["stalled_epic"].consequence == "epic"
    assert by_type["pr_review"].consequence == "review"
    assert by_type["pr_review"].url == "http://pr/43"
    assert by_type["orphan_pr"].consequence == "hygiene"
    assert by_type["orphan_pr"].pr_number == 99
    # each row carries a primary_action hint
    assert all(r.primary_action for r in dto.rows)


def test_a_ci_pending_hold_raises_no_budget_hold_row():
    """The CI gate is the one hold that is a wait, not somebody's move (#2207).

    A regression guard for this payload: the seed above used to be ``ci_pending``
    and asserted a ``budget_hold`` row off it, so the set's exclusion of the gate
    (``kew.waits.classify_wait``) and this module's own test disagreed.
    """
    store = FakeStore(epic_holds=[EpicHold(42, "epic:x", CI_PENDING_HOLD_REASON, NOW)])
    dto = dashboard.assemble(store, _config(), now=NOW)
    assert [r for r in dto.rows if r.type == "budget_hold"] == []
    assert "budget_holds" not in (dto.unavailable or [])


def test_rows_ordered_by_consequence_class():
    store = FakeStore(
        orphan_prs=[OrphanPRRecord(1, "u", "b", "no-record", NOW, None)],  # hygiene
        epic_holds=[EpicHold(2, "epic:x", "held", NOW)],  # money
    )
    dto = dashboard.assemble(store, _config(), now=NOW)
    order = [r.consequence for r in dto.rows]
    assert order == ["money", "hygiene"]


def test_additivity_unknown_row_type_round_trips():
    """A future row type serializes cleanly — clients ignore what they don't know."""
    row = DashboardRowDTO(type="future_type", consequence="money", reason="x")
    dumped = row.model_dump()
    assert dumped["type"] == "future_type"
    # re-parsing an unknown type does not raise (forward-compatible)
    assert DashboardRowDTO(**dumped).type == "future_type"


@pytest.mark.parametrize(
    "method,source_name,row_type",
    [
        ("list_all_epic_holds", "budget_holds", "budget_hold"),
        ("list_orphan_prs", "orphan_prs", "orphan_pr"),
        ("list_syncable_runs", "pr_reviews", "pr_review"),
    ],
)
def test_one_flaky_source_degrades_but_rest_intact(method, source_name, row_type):
    store = FakeStore(
        epic_holds=[EpicHold(2, "epic:x", "held", NOW)],
        orphan_prs=[OrphanPRRecord(1, "u", "b", "no-record", NOW, None)],
        syncable=[
            FakeRun("r1", 43, pr_state="OPEN", pr_url="http://pr/43", finished_at=NOW)
        ],
    )

    def boom(*a, **k):
        raise RuntimeError("reader down")

    setattr(store, method, boom)
    dto = dashboard.assemble(store, _config(), now=NOW)
    assert source_name in dto.unavailable
    assert source_name not in dto.as_of
    # the failed source contributes no rows, but the other two still do
    present = {r.type for r in dto.rows}
    assert row_type not in present
    assert len(present) == 2


def test_glance_source_failure_is_isolated():
    store = FakeStore()

    def boom(*a, **k):
        raise RuntimeError("fleet reader down")

    store.get_fleet_stopped = boom  # banner source
    store.list_pending = boom  # fleet glance source
    dto = dashboard.assemble(store, _config(), now=NOW)
    assert "fleet" in dto.unavailable
    assert dto.glance.fleet is None
    # the banner defaulted rather than 500-ing
    assert dto.banner.fleet_stopped is False


@pytest.mark.parametrize(
    "stopped,ref_present,driver_gone,expect_stopped,expect_no_driver",
    [
        (None, False, False, False, False),  # fresh repo: no alarm
        ("x", False, False, True, False),  # fleet stopped, no epics
        (None, True, False, False, False),  # epic with a live driver
        (None, True, True, False, True),  # epic present, driver gone
        ("x", True, True, True, True),  # both alarms
    ],
)
def test_banner_truth_table(
    stopped, ref_present, driver_gone, expect_stopped, expect_no_driver
):
    refs = ["epic:x"] if ref_present else []
    if ref_present:
        heartbeat = (NOW - timedelta(hours=1)) if driver_gone else NOW
        states = {
            "epic:x": FakeDriverState(
                "epic:x", last_tick_started_at=heartbeat.isoformat(), interval_s=60.0
            )
        }
    else:
        states = {}
    store = FakeStore(epic_refs=refs, driver_states=states, fleet_stopped=stopped)
    dto = dashboard.assemble(store, _config(), now=NOW)
    assert dto.banner.fleet_stopped is expect_stopped
    assert dto.banner.no_live_driver is expect_no_driver


def test_an_all_archived_roster_is_not_a_no_live_driver_alarm():
    """#2206: the alarm's own rule is "epics exist that should be driven, and
    none is" — a fresh repo with no epics is exempt because it has nothing to
    drive. A roster whose every epic an operator retired has nothing to drive
    either, so it is exempt for exactly the same reason."""
    store = FakeStore(
        epic_refs=["epic:a"],
        driver_states={
            "epic:a": FakeDriverState(
                "epic:a",
                last_tick_started_at=(NOW - timedelta(days=60)).isoformat(),
                interval_s=60.0,
            )
        },
        audit_events=[
            FakeAuditEvent(EPIC_ARCHIVED_EVENT, None, {"epic_ref": "epic:a"}, id=1)
        ],
    )
    dto = dashboard.assemble(store, _config(), now=NOW)
    assert dto.banner.no_live_driver is False
    assert "banner" not in (dto.unavailable or [])


def test_routing_table_reports_file_and_default_provenance():
    dto = dashboard.assemble(FakeStore(), _config(), now=NOW)
    routing = {r.complexity: r for r in dto.glance.routing}
    assert routing["default"].provenance == "file"
    assert routing["simple"].provenance == "file"  # explicit entry in _config
    assert routing["medium"].provenance == "default"  # inherits default
    assert routing["medium"].route == routing["default"].route


def test_pr_review_linear_run_renders_item_ref_not_hash_zero():
    """A Linear syncable run (issue_num=0 sentinel, item_id set) renders its
    bare item id, never ``#0``, and carries item_id in the row."""
    store = FakeStore(
        syncable=[
            FakeRun(
                "r1",
                0,
                item_id="SFD-12",
                pr_state="OPEN",
                pr_url="http://pr/1",
                finished_at=NOW,
            )
        ],
    )
    dto = dashboard.assemble(store, _config(), now=NOW)
    row = _rows_by_type(dto)["pr_review"]
    assert "#0" not in row.reason
    assert "SFD-12" in row.reason
    assert row.item_id == "SFD-12"


def test_pr_review_github_run_keeps_hash_number():
    """A GitHub syncable run still renders ``#N`` (byte-identical output)."""
    store = FakeStore(
        syncable=[
            FakeRun("r1", 43, pr_state="OPEN", pr_url="http://pr/43", finished_at=NOW)
        ],
    )
    dto = dashboard.assemble(store, _config(), now=NOW)
    row = _rows_by_type(dto)["pr_review"]
    assert row.reason == "PR open for #43 — awaiting review"
    assert row.item_id == "43"


def test_recent_completion_carries_item_id():
    store = FakeStore(
        all_runs=[FakeRun("r1", 0, item_id="SFD-9", finished_at=NOW)],
    )
    dto = dashboard.assemble(store, _config(), now=NOW)
    assert dto.glance.recent_completions[0].item_id == "SFD-9"


def test_recent_completions_newest_first_and_capped():
    runs = [
        FakeRun(
            f"r{i}",
            i,
            finished_at=NOW - timedelta(hours=i),
            estimated_cost_usd=float(i),
        )
        for i in range(1, 8)
    ]
    store = FakeStore(all_runs=runs)
    dto = dashboard.assemble(store, _config(), now=NOW)
    comps = dto.glance.recent_completions
    assert len(comps) == 5  # capped
    assert comps[0].run_id == "r1"  # newest finished_at first
    assert comps[0].cost_usd == 1.0


# --------------------------------------------------------------------------- #
# queue_candidates glance field: additive, populated, fail-soft (#729)
# --------------------------------------------------------------------------- #


@dataclass
class FakeProc:
    """Minimal CompletedProcess-like for stubbing ``dashboard.run``."""

    returncode: int
    stdout: str
    stderr: str = ""


def test_queue_candidates_field_defaults_empty():
    """The additive field is present and defaults to an empty list."""
    dto = dashboard.assemble(FakeStore(), _config(), now=NOW)
    assert dto.glance.queue_candidates == []
    assert "queue_candidates" in dto.as_of


def test_queue_candidates_populated_from_source(monkeypatch):
    monkeypatch.setattr(
        dashboard,
        "_queue_candidates",
        lambda config: [QueueCandidateDTO(issue_num=7, title="fix widget")],
    )
    dto = dashboard.assemble(FakeStore(), _config(), now=NOW)
    assert dto.glance.queue_candidates == [
        QueueCandidateDTO(issue_num=7, title="fix widget")
    ]
    assert "queue_candidates" not in dto.unavailable


def test_queue_candidates_fail_soft(monkeypatch):
    """A raising reader degrades to unavailable without sinking the payload."""

    def boom(config):
        raise RuntimeError("gh down")

    monkeypatch.setattr(dashboard, "_queue_candidates", boom)
    dto = dashboard.assemble(FakeStore(), _config(), now=NOW)
    assert "queue_candidates" in dto.unavailable
    assert "queue_candidates" not in dto.as_of
    assert dto.glance.queue_candidates == []
    # the rest of the payload is intact
    assert isinstance(dto, DashboardDTO)
    assert dto.glance.fleet is not None


def test_queue_candidates_reader_drops_ready_labeled(monkeypatch):
    """The reader returns open, un-ready issues only (number + title)."""
    payload = json.dumps(
        [
            {"number": 1, "title": "un-ready one", "labels": []},
            {"number": 2, "title": "already ready", "labels": [{"name": "ready"}]},
            {"number": 3, "title": "un-ready two", "labels": [{"name": "bug"}]},
        ]
    )
    monkeypatch.setattr(
        dashboard, "run", lambda *a, **k: FakeProc(returncode=0, stdout=payload)
    )
    config = DispatchConfig(project=ProjectConfig(repo="owner/repo"))
    candidates = dashboard._queue_candidates(config)
    assert candidates == [
        QueueCandidateDTO(issue_num=1, item_id="1", title="un-ready one"),
        QueueCandidateDTO(issue_num=3, item_id="3", title="un-ready two"),
    ]


def test_queue_candidates_reader_raises_on_gh_error(monkeypatch):
    """A gh failure raises so ``source`` marks it unavailable (never empty=healthy)."""
    monkeypatch.setattr(
        dashboard,
        "run",
        lambda *a, **k: FakeProc(returncode=1, stdout="", stderr="boom"),
    )
    config = DispatchConfig(project=ProjectConfig(repo="owner/repo"))
    with pytest.raises(RuntimeError):
        dashboard._queue_candidates(config)


def test_2264_queue_candidates_are_read_once_per_ttl(monkeypatch):
    """Many polls inside the TTL cost one gh read; the cache expires (#2264)."""
    dashboard._candidates_cache.clear()
    calls = []

    def reader(config):
        calls.append(1)
        return [QueueCandidateDTO(issue_num=len(calls), title="t")]

    clock = [1000.0]
    monkeypatch.setattr(dashboard, "_queue_candidates", reader)
    monkeypatch.setattr(dashboard.time, "monotonic", lambda: clock[0])
    config = _config()
    config.project.repo = "o/r"
    for _ in range(20):
        assert dashboard._queue_candidates_cached(config)[0].issue_num == 1
    assert len(calls) == 1
    clock[0] += dashboard._CANDIDATES_CACHE_TTL_S + 1
    # An expired entry is answered as it stands while one refill runs off the
    # request path (#2789, K8); the poll after it lands reads the new value.
    assert dashboard._queue_candidates_cached(config)[0].issue_num == 1
    _wait_for(lambda: dashboard._candidates_cache["o/r"][1][0].issue_num == 2)
    assert dashboard._queue_candidates_cached(config)[0].issue_num == 2
    assert len(calls) == 2
    dashboard._candidates_cache.clear()


def test_c6_a_failed_read_is_held_for_the_backoff_then_retried(monkeypatch):
    """A failing gh is not asked again on every poll (design review c1, C6):
    inside the backoff the failure re-raises from memory, never as an empty
    healthy answer; after it, gh is tried once more and a success is cached."""
    attempts = []

    def flaky(config):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("gh rate limited")
        return []

    clock = [1000.0]
    monkeypatch.setattr(dashboard, "_queue_candidates", flaky)
    monkeypatch.setattr(dashboard.time, "monotonic", lambda: clock[0])
    config = _config()
    config.project.repo = "o/r"
    with pytest.raises(RuntimeError, match="gh rate limited"):
        dashboard._queue_candidates_cached(config)
    for _ in range(10):
        with pytest.raises(RuntimeError, match="failed recently"):
            dashboard._queue_candidates_cached(config)
    assert len(attempts) == 1
    clock[0] += dashboard._FAILURE_BACKOFF_S + 1
    assert dashboard._queue_candidates_cached(config) == []
    assert dashboard._queue_candidates_cached(config) == []
    assert len(attempts) == 2


def test_c6_a_failed_read_still_marks_the_field_unavailable(monkeypatch):
    def down(config):
        raise RuntimeError("gh down")

    monkeypatch.setattr(dashboard, "_queue_candidates", down)
    config = _config()
    config.project.repo = "o/r"
    for _ in range(2):  # the live failure, then the held one
        dto = dashboard.assemble(FakeStore(), config, now=NOW)
        assert "queue_candidates" in dto.unavailable
        assert dto.glance.queue_candidates == []


def test_c6_concurrent_polls_share_one_refill(monkeypatch):
    import threading

    started = threading.Event()
    release = threading.Event()
    calls = []

    def slow(config):
        calls.append(1)
        started.set()
        release.wait(5)
        return [QueueCandidateDTO(issue_num=1, title="t")]

    monkeypatch.setattr(dashboard, "_queue_candidates", slow)
    config = _config()
    config.project.repo = "o/r"
    results = []

    def poll() -> None:
        results.append(dashboard._queue_candidates_cached(config))

    threads = [threading.Thread(target=poll) for _ in range(5)]
    for t in threads:
        t.start()
    started.wait(5)
    release.set()
    for t in threads:
        t.join(5)
    assert len(calls) == 1
    assert len(results) == 5


_SCOPES = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
# Nodes an argv literal can sit inside on its way to a name or a call:
# ``["gh", ...] + extra``, ``[*["gh", ...], x]``.
_ARGV_WRAPPERS = (ast.BinOp, ast.List, ast.Tuple, ast.Starred)


def _gh_argv_runs(tree: ast.Module) -> list[tuple[ast.expr, str, list[ast.Call]]]:
    """Each ``["gh", ...]`` literal in *tree*, its scope's name, and the calls
    that run it — passed inline, or through the name it is assigned to.

    Any def, async def or lambda, and an argv held in a variable first, so a
    reader written differently from today's two is still followed. An argv
    the walk cannot follow to a call comes back with no calls.
    """
    parent = {
        child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)
    }
    found = []
    for literal in ast.walk(tree):
        if not (
            isinstance(literal, ast.List | ast.Tuple)
            and literal.elts
            and source_ast.template(literal.elts[0]) == "gh"
        ):
            continue
        scope: ast.AST = parent[literal]
        while not isinstance(scope, _SCOPES):
            scope = parent[scope]
        outer: ast.AST = literal
        while isinstance(parent[outer], _ARGV_WRAPPERS):
            outer = parent[outer]
        holder = parent[outer]
        names: set[str] = set()
        if isinstance(holder, ast.Assign | ast.AnnAssign | ast.NamedExpr):
            targets = (
                holder.targets if isinstance(holder, ast.Assign) else [holder.target]
            )
            names = {t.id for t in targets if isinstance(t, ast.Name)}

        consumers = [
            call
            for call in source_ast.calls(scope)
            if any(
                arg is outer or (isinstance(arg, ast.Name) and arg.id in names)
                for arg in [*call.args, *(kw.value for kw in call.keywords)]
            )
        ]
        found.append((literal, getattr(scope, "name", type(scope).__name__), consumers))
    return found


@pytest.mark.policy
def test_every_gh_read_has_a_timeout():
    """Every ``gh`` argv the dashboard runs carries ``timeout=_GH_TIMEOUT_S``.

    Read off the module's syntax tree, not the readers: the autouse fixture
    stubs one of them, so no call through it could show the deadline. Each
    ``gh`` argv literal is followed to the call that runs it, so an argv built
    in a variable, or a reader that is ``async``, is held to the same rule.
    """
    gh_argvs = _gh_argv_runs(source_ast.module_tree(dashboard))
    readers = {name for _, name, _ in gh_argvs}
    assert readers >= {"_ready_issues", "_queue_candidates"}, readers
    for literal, name, consumers in gh_argvs:
        assert consumers, f"{name}:{literal.lineno}: gh argv not followed to a call"
        for call in consumers:
            timeout = source_ast.keyword(call, "timeout")
            assert isinstance(timeout, ast.Name), (name, call.lineno)
            assert timeout.id == "_GH_TIMEOUT_S", (name, call.lineno)


@pytest.mark.parametrize(
    "reader",
    [
        "def _r(config):\n"
        '    argv = ["gh", "issue", "list"]\n'
        "    return subprocess_utils.run(argv, capture_output=True)\n",
        'async def _r(config):\n    return run(["gh", "issue", "list"], check=False)\n',
        "def _r(config):\n"
        '    argv = ["gh", "issue", "list"] + extra\n'
        "    return run(args=argv, timeout=5)\n",
        'def _r(config):\n    return helper(lambda: ["gh", "issue", "list"])\n',
    ],
)
def test_gh_timeout_check_follows_every_argv_shape(reader):
    """The policy walk above sees a timeout-less gh read however it is spelled:
    argv in a variable, an async reader, ``args=`` with the wrong deadline, or
    an argv it cannot trace to a call at all."""
    [(_, _, consumers)] = _gh_argv_runs(ast.parse(reader))
    assert not consumers or any(
        not isinstance(t := source_ast.keyword(c, "timeout"), ast.Name)
        or t.id != "_GH_TIMEOUT_S"
        for c in consumers
    )


def test_queue_candidates_reader_empty_when_repo_unset():
    """No pinned repo → nothing to read (empty), not a failure."""
    assert dashboard._queue_candidates(_config()) == []


# --------------------------------------------------------------------------- #
# Stalled epic row: filtering, humanized age, epic ref in reason (#709)
# --------------------------------------------------------------------------- #


def _stalled_driver_state(ref: str, age: timedelta) -> FakeDriverState:
    """Return a driver state that will be flagged as gone (age >> 2× interval)."""
    return FakeDriverState(
        ref,
        last_tick_started_at=(NOW - age).isoformat(),
        interval_s=60.0,
    )


def test_complete_signal_epic_produces_no_stalled_row():
    """An epic whose last snapshot signal is 'complete' must not appear in the queue."""
    ref = "epic:coldstart"
    store = FakeStore(
        epic_refs=[ref],
        driver_states={ref: _stalled_driver_state(ref, timedelta(days=7))},
        epic_ticks={ref: FakeEpicTick(snapshot={"signal": "complete"})},
    )
    dto = dashboard.assemble(store, _config(), now=NOW)
    stalled = [r for r in dto.rows if r.type == "stalled_epic"]
    assert stalled == []


def test_stalled_epic_without_complete_signal_still_produces_row():
    """A genuinely stalled epic (no terminal signal) still surfaces in the queue."""
    ref = "epic:active"
    store = FakeStore(
        epic_refs=[ref],
        driver_states={ref: _stalled_driver_state(ref, timedelta(hours=3))},
    )
    dto = dashboard.assemble(store, _config(), now=NOW)
    stalled = [r for r in dto.rows if r.type == "stalled_epic"]
    assert len(stalled) == 1
    assert stalled[0].epic_ref == ref


def test_an_archived_epic_produces_no_stalled_row():
    """#2206: an archived epic is retired, not ignored — its drain stays in the
    ledger, but it stops counting against the header's "needs you". Sixty-six
    epics put twelve dead drains in that count with no way to retire one."""
    ref = "epic:abandoned"
    store = FakeStore(
        epic_refs=[ref],
        driver_states={ref: _stalled_driver_state(ref, timedelta(days=60))},
        audit_events=[
            FakeAuditEvent(
                EPIC_ARCHIVED_EVENT, None, {"epic_ref": ref, "reason": "dropped"}, id=1
            )
        ],
    )
    dto = dashboard.assemble(store, _config(), now=NOW)
    assert [r for r in dto.rows if r.type == "stalled_epic"] == []
    assert "stalled_epics" not in (dto.unavailable or [])


def test_an_unarchived_epic_counts_again():
    """Unarchive is a new fact that outranks the archive, so the row returns."""
    ref = "epic:revived"
    store = FakeStore(
        epic_refs=[ref],
        driver_states={ref: _stalled_driver_state(ref, timedelta(days=60))},
        audit_events=[
            FakeAuditEvent(EPIC_ARCHIVED_EVENT, None, {"epic_ref": ref}, id=1),
            FakeAuditEvent(EPIC_UNARCHIVED_EVENT, None, {"epic_ref": ref}, id=2),
        ],
    )
    dto = dashboard.assemble(store, _config(), now=NOW)
    stalled = [r for r in dto.rows if r.type == "stalled_epic"]
    assert len(stalled) == 1
    assert stalled[0].epic_ref == ref


def test_stalled_row_reason_humanizes_age():
    """Raw seconds are replaced by a human-legible duration (e.g. '6d 8h')."""
    ref = "epic:active"
    # 549972 seconds ≈ 6d 8h 46m → expect '6d 8h'
    store = FakeStore(
        epic_refs=[ref],
        driver_states={ref: _stalled_driver_state(ref, timedelta(seconds=549972))},
    )
    dto = dashboard.assemble(store, _config(), now=NOW)
    stalled = [r for r in dto.rows if r.type == "stalled_epic"]
    assert len(stalled) == 1
    reason = stalled[0].reason
    assert "549972s" not in reason
    assert "6d" in reason


def test_stalled_row_reason_includes_epic_ref():
    """The stalled-epic reason string must contain the epic ref so it is actionable."""
    ref = "epic:my-project"
    store = FakeStore(
        epic_refs=[ref],
        driver_states={ref: _stalled_driver_state(ref, timedelta(hours=2))},
    )
    dto = dashboard.assemble(store, _config(), now=NOW)
    stalled = [r for r in dto.rows if r.type == "stalled_epic"]
    assert len(stalled) == 1
    assert ref in stalled[0].reason


@pytest.mark.parametrize(
    "seconds,expected",
    [
        (30, "30s"),
        (90, "1m 30s"),
        (3661, "1h 1m"),
        (86400, "1d 0h"),
        (549972, "6d 8h"),
    ],
)
def test_humanize_age(seconds, expected):
    """The helper the stalled_epic reason is worded with — it lives in
    ``needs_you`` with the row it words, not in ``dashboard`` (#2207)."""
    assert needs_you._humanize_age(seconds) == expected


# ===========================================================================
# I19 — the console reads the canonical work projection, not raw statuses.
# ===========================================================================


class CapacityStore(FakeStore):
    """A store that HAS reconciled a delivery repository's review capacity."""

    def __init__(self, *, occupancy: int, available: bool = True, **kwargs: Any):
        super().__init__(**kwargs)
        self._occupancy = occupancy
        self._available = available

    def review_observation(self, delivery_repo: str):
        from kew.publication_job import RemoteReviewObservation

        return RemoteReviewObservation(
            delivery_repo=delivery_repo,
            availability="available" if self._available else "unavailable",
            observed_at=NOW.isoformat(),
            generation=0,
            detail="" if self._available else "gh could not be reached",
        )

    def review_capacity_occupancy(self, *, delivery_repo: str, observation: Any) -> int:
        return self._occupancy

    def get_human_review_policy(self, delivery_repo: str):
        return None

    def list_publication_jobs(self):
        return []


def _repo_config() -> DispatchConfig:
    return DispatchConfig(project=ProjectConfig(repo="acme/widgets"))


def test_glance_carries_the_review_pressure_read_with_the_rows():
    store = CapacityStore(occupancy=2)

    pressure = dashboard.assemble(store, _repo_config(), now=NOW).glance.pressure

    assert pressure is not None
    assert (pressure.used, pressure.total) == (2, 3)
    assert pressure.countable is True
    assert pressure.saturated is False


def test_unreadable_review_capacity_is_saturated_not_a_healthy_zero():
    """The one place an empty answer is most dangerous: a forge nobody read."""
    store = CapacityStore(occupancy=0, available=False)

    pressure = dashboard.assemble(store, _repo_config(), now=NOW).glance.pressure

    assert pressure is not None
    assert pressure.countable is False
    assert pressure.saturated is True
    assert pressure.reasons


def test_configured_review_policy_reports_itself_inactive_pre_broker():
    store = CapacityStore(occupancy=1)

    pressure = dashboard.assemble(store, _repo_config(), now=NOW).glance.pressure

    assert pressure is not None
    assert pressure.enforcement == "inactive"
    assert "not active" in pressure.enforcement_detail


def test_pressure_is_none_when_the_projection_itself_is_unreadable():
    """Not a zeroed reading — a zeroed reading would look like an empty queue."""
    store = FakeStore()
    store.list_syncable_runs = _boom  # type: ignore[method-assign]

    dto = dashboard.assemble(store, _repo_config(), now=NOW)

    assert dto.glance.pressure is None
    assert "pr_reviews" in dto.unavailable


def _boom(*_a: Any, **_k: Any) -> Any:
    raise RuntimeError("reader down")


def test_preserved_publication_is_a_row_the_console_could_not_show_before():
    """``awaiting_publication`` is preserved work, and its console action is
    to publish that same artifact — never to run the work again."""
    store = FakeStore(
        syncable=[
            FakeRun(
                "r1",
                43,
                status="awaiting_publication",
                branch="agent/issue-43",
                pr_url="http://pr/43",
                pr_state="OPEN",
                finished_at=NOW,
            )
        ]
    )

    row = _rows_by_type(dashboard.assemble(store, _config(), now=NOW))["work_exception"]

    assert "awaiting publication" in row.reason
    assert row.primary_action == "publish"
    assert row.primary_action != "dispatch"


def test_stale_pr_with_no_persisted_state_asks_for_a_refresh_not_a_dispatch():
    """A published artifact whose forge state was never read is unknown — the
    projection asks to look, and never proposes work on top of it. The read
    failure is not a person's move, so the console serves no needs-you row for
    it (#2893): the view keeps saying it could not read."""
    store = FakeStore(
        syncable=[FakeRun("r1", 43, pr_url="http://pr/43", finished_at=NOW)]
    )

    dto = dashboard.assemble(store, _config(), now=NOW)
    view = needs_you.work_projection(store, _config(), now=NOW).views[0]

    assert (view.exception.state, view.safest_action.action) == (
        "remote_unavailable",
        "observe_remote",
    )
    assert [r.item_id for r in dto.rows if r.item_id == "43"] == []


def test_an_item_whose_facts_cannot_be_read_is_named_not_dropped():
    store = FakeStore(
        syncable=[
            FakeRun("r1", 43, pr_state="OPEN", pr_url="http://pr/43", finished_at=NOW)
        ]
    )
    store.list_orphan_prs = _boom  # type: ignore[method-assign]

    dto = dashboard.assemble(store, _config(), now=NOW)
    row = _rows_by_type(dto)["work_unreadable"]

    assert row.item_id == "43"
    assert "not an item with nothing wrong" in row.reason


def test_merged_work_asks_whether_the_source_item_was_closed():
    """The console used to drop a merged run the moment its PR left ``OPEN``.
    That is exactly the point at which the source item may still be open, so
    the projection keeps it visible — as a tracker read to make, not a review
    to do, and never as work to dispatch again. An unread tracker is a read
    failure, not a person's move (#2893), so it is the view that says so, and
    the needs-you rows carry nothing for it."""
    store = FakeStore(
        syncable=[
            FakeRun("r1", 43, pr_state="MERGED", pr_url="http://pr/43", finished_at=NOW)
        ]
    )

    dto = dashboard.assemble(store, _config(), now=NOW)
    view = needs_you.work_projection(store, _config(), now=NOW).views[0]

    assert "pr_review" not in {r.type for r in dto.rows}
    assert [r.item_id for r in dto.rows if r.item_id == "43"] == []
    assert view.safest_action.action == "observe_remote"
    assert "source item" in view.exception.detail


def test_work_projection_binds_every_row_to_one_revision():
    store = FakeStore(
        syncable=[
            FakeRun("r1", 43, pr_state="OPEN", pr_url="http://pr/43", finished_at=NOW),
            FakeRun("r2", 44, pr_state="OPEN", pr_url="http://pr/44", finished_at=NOW),
        ]
    )

    projection = needs_you.work_projection(store, _config(), now=NOW)

    assert projection.projection_revision.startswith("wp1:")
    assert len(projection.views) == 2
    assert projection.unreadable == []


# --- #2297: the ready queue reads the CONFIGURED label ----------------------


def test_ready_reader_passes_the_configured_ready_label_to_gh(monkeypatch):
    """Hardcoding "ready" showed an empty queue to any project that renamed the
    label, while the dispatcher read the right one."""
    seen = []

    def fake_run(cmd, **kwargs):
        seen.append(cmd)
        return FakeProc(returncode=0, stdout="[]")

    # The module's autouse fixture stubs this very reader out; drop that so the
    # real one runs against a stubbed `run`.
    monkeypatch.undo()
    monkeypatch.setattr(dashboard, "run", fake_run)
    config = DispatchConfig(
        project=ProjectConfig(repo="owner/repo"), labels=LabelConfig(ready="queued")
    )

    assert dashboard._ready_issues(config) == []
    assert "queued" in seen[0]
    assert "ready" not in seen[0]


def test_queue_candidates_drop_the_configured_ready_label(monkeypatch):
    payload = json.dumps(
        [
            {"number": 1, "title": "waiting", "labels": [{"name": "ready"}]},
            {"number": 2, "title": "queued already", "labels": [{"name": "queued"}]},
        ]
    )
    monkeypatch.setattr(
        dashboard, "run", lambda *a, **k: FakeProc(returncode=0, stdout=payload)
    )
    config = DispatchConfig(
        project=ProjectConfig(repo="owner/repo"), labels=LabelConfig(ready="queued")
    )

    candidates = dashboard._queue_candidates(config)

    # Only the issue carrying the CONFIGURED ready label is already queued.
    assert [c.issue_num for c in candidates] == [1]
