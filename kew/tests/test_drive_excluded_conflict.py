"""An excluded epic's base conflict is held inside its tick (#2869).

End to end through one ``kew drive`` round in production order: the epic's own
tick (the real ``advance``), then the standing merge rule's pass. Only the
forge (``gh``) and the merge-main seam are faked.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from support import driver_fakes as _fx
from support.driver_fakes import (
    EPIC,
    NOW,
    REPO,
    FakeFactsReader,
    FakeSink,
    FakeSource,
    _node,
)

import kew.cli as cli
import kew.cli.drive as drive_cli
from kew.config import DispatchConfig, DriveMergeConfig, ProjectConfig
from kew.drive import current_holder, request_stop
from kew.drive_merge import DriveMerger
from kew.graph.domain import NodeStatus
from kew.graph.driver.base_integration import (
    BaseIntegrationContext,
    BaseIntegrationOutcome,
    base_integration_exhausted_hold_reason,
)
from kew.graph.driver.excluded_conflict import hold_excluded_conflicts
from kew.graph.driver.facts import NodeFacts
from kew.graph.driver.overlay import DriverStatus
from kew.graph.driver.review_detach import DetachedReview
from kew.graph.driver.review_stage import drive_review_stage
from kew.graph.driver.tick import advance
from kew.outcomes import ForgeIdentity
from kew.pseudo_epics import REVIEW_EPIC_REF
from kew.run_store import SqliteRunStore

_no_real_git = _fx._no_real_git
_isolate_tool_paths = _fx._isolate_tool_paths

ITEM = "1"
PR = 7
HEAD = "a" * 40


def _config(tmp_path: Path) -> DispatchConfig:
    config = DispatchConfig(project=ProjectConfig(repo=REPO))
    config.logging.dir = str(tmp_path / "logs")
    config.drive.merge = DriveMergeConfig(
        enabled=True, signer="tom", exclude_epics=[EPIC]
    )
    config.review.adversarial = True
    config.review.blocking = True
    return config


def _dirty_facts() -> FakeFactsReader:
    """GitHub's read of the READY head: open, and no longer merging."""
    return FakeFactsReader(
        issue_facts_by_num={
            int(ITEM): NodeFacts(
                issue_num=int(ITEM),
                state="OPEN",
                state_reason=None,
                labels=frozenset(),
                pr_number=PR,
                pr_url=f"https://github.com/{REPO}/pull/{PR}",
                review_decision=None,
                merge_state_status="DIRTY",
                head_oid=HEAD,
                pr_pushed_at=None,
                pr_state="OPEN",
            )
        },
        pr_facts_by_num={
            PR: {
                "number": PR,
                "state": "OPEN",
                "headRefOid": HEAD,
                "mergeStateStatus": "DIRTY",
            }
        },
    )


def _forge(cmd: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
    """``gh pr view`` for the drive's mergeability read; nothing else runs."""
    if cmd[:3] == ["gh", "pr", "view"]:
        out = json.dumps(
            {
                "mergeable": "CONFLICTING",
                "mergeStateStatus": "DIRTY",
                "headRefOid": HEAD,
            }
        )
        return subprocess.CompletedProcess(cmd, 0, out, "")
    raise AssertionError(f"unexpected command {cmd}")


def test_an_excluded_epics_conflict_is_held_and_resolved_in_the_same_round(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """The tick holds the conflict before its review stage, so the resolve
    starts this round and the journaled tick already reads it held — not
    ``in_review — awaiting merge`` until the drive's pass, a round later."""
    monkeypatch.setattr(cli, "_sleep", lambda _s: None)
    config = _config(tmp_path)
    db = Path(config.logging.dir) / "dispatch.db"
    db.parent.mkdir(parents=True)
    with SqliteRunStore(str(db)) as ledger:
        ledger.register_epic(EPIC)
        ledger.record_review_round(
            EPIC, ITEM, head_sha=HEAD, verdict="READY", review_mode="full"
        )
    facts = _dirty_facts()
    resolved: list[tuple[int, str, list[str]]] = []
    passes: list[list[str]] = []

    def integrate(ctx: BaseIntegrationContext) -> BaseIntegrationOutcome:
        with SqliteRunStore(str(db)) as ledger:
            held = [h.reason for h in ledger.list_epic_holds(EPIC)]
        resolved.append((ctx.pr_number, ctx.head_sha, held))
        return BaseIntegrationOutcome("integrated")

    def tick(name: str, _admit_cap: int) -> int:
        with SqliteRunStore(str(db)) as store:
            advance(
                name,
                config=config,
                store=store,
                deps=FakeSource(
                    nodes=[_node(ITEM, status=NodeStatus.IN_REVIEW)], edges=[]
                ),
                facts=facts,  # type: ignore[arg-type]
                dispatch_sink=FakeSink(),  # type: ignore[arg-type]
                max_parallel=5,
                now=NOW,
                lock_path=tmp_path / "epic.lock",
                approvers=[],
                repo=REPO,
                base_integrator=integrate,
            )
        return 10

    def merge(store: Any) -> Any:
        merger = DriveMerger(
            config,
            store,
            repo=REPO,
            repo_root=tmp_path,
            reader=lambda: facts,  # type: ignore[arg-type,return-value]
            provider="github",
            identity=lambda: ForgeIdentity(login="tom"),
            run=_forge,
        )

        def merge_pass(due: Any) -> None:
            passes.append(list(due))
            merger(due)
            holder = current_holder(store)
            assert holder is not None
            request_stop(store, holder, None)

        return merge_pass

    drive_cli.run_drive(config, tick, interval=1, merge=merge)

    assert passes == [[EPIC]], "one round: the epic's tick, then the merge pass"
    assert resolved == [(PR, HEAD, ["ci_pending:dirty"])], (
        "the resolve ran in this round's tick, on the conflict hold"
    )
    with SqliteRunStore(str(db)) as ledger:
        journaled = ledger.latest_epic_tick(EPIC)
    assert journaled is not None
    (node,) = journaled.snapshot["nodes"]
    assert node["stuck"].startswith("integrating main"), node


def test_a_conflict_on_a_head_the_ready_round_did_not_review_is_not_held(
    tmp_path: Path,
) -> None:
    """The tick holds the same set the drive's own read does: a READY verdict
    at the head GitHub reports conflicting, nothing else."""
    statuses = {ITEM: DriverStatus.IN_REVIEW}
    facts = _dirty_facts().issue_facts(int(ITEM))
    with SqliteRunStore(str(tmp_path / "run.db")) as store:
        store.record_review_round(
            EPIC, ITEM, head_sha="f" * 40, verdict="READY", review_mode="full"
        )

        held = hold_excluded_conflicts(
            statuses, {ITEM: facts}, epic_ref=EPIC, store=store
        )

        assert held == [] and store.list_epic_holds(EPIC) == []
    assert statuses == {ITEM: DriverStatus.IN_REVIEW}


EXHAUSTED = base_integration_exhausted_hold_reason(
    base="main", attempts=3, head_sha=HEAD
)


class _LateResolver:
    """The ledger, with an earlier tick's detached resolver that finishes the
    moment this tick goes to write the conflict hold: it writes its retry-bound
    hold, then gives its review reservation back — after the overlays read it
    live and the node IN_REVIEW."""

    def __init__(self, store: SqliteRunStore, run_id: str) -> None:
        self._store = store
        self._run_id = run_id

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)

    def hold_epic_node_unless(self, *args: Any, **kwargs: Any) -> bool:
        self._store.hold_epic_node(EPIC, ITEM, EXHAUSTED)
        self._store.release_epic_node_item(ITEM, run_id=self._run_id)
        return self._store.hold_epic_node_unless(*args, **kwargs)


def test_late_resolver_hold_blocks_a_second_detached_resolve(
    tmp_path: Path,
) -> None:
    """A resolver hold written after the overlays were built reads HELD in
    this tick, so the review stage launches no second resolve past the bound."""
    config = _config(tmp_path)
    statuses = {ITEM: DriverStatus.IN_REVIEW}
    facts = _dirty_facts().issue_facts(int(ITEM))
    spawned: list[DetachedReview] = []
    with SqliteRunStore(str(tmp_path / "run.db")) as ledger:
        ledger.record_review_round(
            EPIC, ITEM, head_sha=HEAD, verdict="READY", review_mode="full"
        )
        ledger.reserve_epic_node_item(REVIEW_EPIC_REF, ITEM, "resolver-run")
        store = _LateResolver(ledger, "resolver-run")

        held = hold_excluded_conflicts(
            statuses,
            {ITEM: facts},
            epic_ref=EPIC,
            store=store,  # type: ignore[arg-type]
        )
        outcome = drive_review_stage(
            statuses,
            {ITEM: facts},
            epic_ref=EPIC,
            config=config,
            store=ledger,
            repo=REPO,
            repo_root=tmp_path,
            base="main",
            reviewer_launcher=None,
            worker_cap=4,
            max_concurrent=2,
            now=NOW,
            detacher=spawned.append,
        )

        assert ledger.list_epic_reservations(REVIEW_EPIC_REF) == []
        assert [h.reason for h in ledger.list_epic_holds(EPIC)] == [EXHAUSTED]
    assert held == []
    assert statuses == {ITEM: DriverStatus.HELD}
    assert spawned == [] and outcome.detached == {}


def test_a_resolver_hold_refuses_the_resolve_whatever_the_overlay_says(
    tmp_path: Path,
) -> None:
    """The review stage's due gate reads the store's hold, not only the status
    overlaid earlier in the tick: an IN_REVIEW node under a resolver hold owes
    no merge-main step."""
    spawned: list[DetachedReview] = []
    with SqliteRunStore(str(tmp_path / "run.db")) as store:
        store.hold_epic_node(EPIC, ITEM, EXHAUSTED)

        drive_review_stage(
            {ITEM: DriverStatus.IN_REVIEW},
            {ITEM: _dirty_facts().issue_facts(int(ITEM))},
            epic_ref=EPIC,
            config=_config(tmp_path),
            store=store,
            repo=REPO,
            repo_root=tmp_path,
            base="main",
            reviewer_launcher=None,
            worker_cap=4,
            max_concurrent=2,
            now=NOW,
            detacher=spawned.append,
        )

        assert store.list_epic_reservations(REVIEW_EPIC_REF) == []
    assert spawned == []


def test_excluded_epics_without_adversarial_review_are_refused() -> None:
    """Only the review stage resolves an excluded epic's conflict, so a config
    that excludes an epic with that stage off has no path to resolve it."""
    rule = {"merge": {"enabled": True, "signer": "tom", "exclude_epics": [EPIC]}}

    with pytest.raises(ValueError, match=r"exclude_epics requires \[review\]"):
        DispatchConfig.model_validate({"drive": rule})

    on = DispatchConfig.model_validate({"drive": rule, "review": {"adversarial": True}})
    assert on.drive.merge.exclude_epics == [EPIC]
