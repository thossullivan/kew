"""Item pipeline projection — service composition and the serve route (FUI-11).

The payload behind the DWork swimlane: one lane (the base branch) over
implement / publish / review / merge. The fixture in :func:`_seed` is the
artboard's own item — run ``f5dcb6a5`` successful, scan clean over
``3670e43f``, review round 1 in flight, an open pull request whose decision
that round's verdict decides — so a regression in any stage shows up as the
reference picture changing, not as an abstract dict diff.

Guard coverage mirrors ``test_serve_api_dashboard``: the router carries
``read_guard``, so the read is session-gated under ``--expose`` and for every
non-loopback peer, and loopback-open otherwise.
"""

from __future__ import annotations

from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from support import route_deps

import kew.serve.api_work as api_work_module
import kew.service.dashboard as dashboard_module
from kew.audit import Actor, emit
from kew.config import BranchConfig, DispatchConfig, ProjectConfig
from kew.merge_verify import VERIFY_EVENT
from kew.publication_scan import ALREADY_PUBLISHED_EVENT, OUTPUT_SCAN_EVENT
from kew.review_audit import REVIEW_VERDICT_EVENT
from kew.run_store import (
    ADVERSARIAL_REVIEW_RUN_KIND,
    RunRecord,
    SqliteRunStore,
)
from kew.serve.app import create_app
from kew.serve.auth import SessionStore
from kew.serve.security import read_guard, require_write
from kew.service.models import ReadyIssueDTO
from kew.service.pipeline import (
    UNREADABLE_AUDIT,
    UNREADABLE_JUDGMENT,
    UNREADABLE_MERGE_VERIFY,
    UNREADABLE_ROUNDS,
    item_pipeline,
)
from kew.service.work import PENDING_RUN_MAX_AGE_S

ITEM = "2059"
PATH = f"/api/work/{ITEM}/pipeline"
IMPL_RUN = "f5dcb6a5"
HEAD_SHA = "3670e43f9a1b2c3d4e5f60718293a4b5c6d7e8f9"
#: A second push of the same item — the head a later, clean scan covers.
OTHER_HEAD = "9c8b7a6f5e4d3c2b1a0987654321fedcba098765"
PR_URL = "https://github.com/kew-dev/kew/pull/2059"


def _impl_run(**overrides: Any) -> RunRecord:
    fields: dict[str, Any] = {
        "run_id": IMPL_RUN,
        "issue_num": 2059,
        "item_id": ITEM,
        "branch": "agent/issue-2059",
        "status": "success",
        "exit_code": 0,
        "started_at": datetime(2026, 9, 24, tzinfo=UTC),
        "finished_at": datetime(2026, 9, 24, 1, tzinfo=UTC),
        "pr_url": PR_URL,
        "pr_state": "OPEN",
    }
    fields.update(overrides)
    return RunRecord(**fields)


def _review_run(**overrides: Any) -> RunRecord:
    fields: dict[str, Any] = {
        "run_id": "rev-1",
        "issue_num": 2059,
        "item_id": ITEM,
        "branch": "agent/issue-2059",
        "status": "pending",
        "exit_code": 0,
        # An hour ago, not a fixed date in the past: a pending review row is
        # read as a round still being read only while it is YOUNG
        # (``PENDING_RUN_MAX_AGE_S`` in kew/src/kew/service/work.py), and
        # these fixtures are composed against the wall clock. Still the newest
        # row on the item, which is the shadowing the projection scopes past.
        "started_at": datetime.now(UTC) - timedelta(hours=1),
        "run_kind": ADVERSARIAL_REVIEW_RUN_KIND,
    }
    fields.update(overrides)
    return RunRecord(**fields)


def _scan(
    store: SqliteRunStore,
    *,
    verdict: str,
    head: str = HEAD_SHA,
    item_id: str | None = ITEM,
    already_published: bool = False,
) -> None:
    """One recorded ``publication_output_scan``, as the host writes it.

    ``item_id=None`` is the pre-item_id row a legacy GitHub item carries: the
    key is on ``run_id`` alone, and the run that scanned is the one that pushed.
    """
    emit(
        store,
        OUTPUT_SCAN_EVENT,
        actor=Actor("policy", "host-publication"),
        run_id=IMPL_RUN,
        item_id=item_id,
        payload={
            "verdict": verdict,
            "head_sha": head,
            "already_published": already_published,
            "files_scanned": 7,
            "matches": {},
            "findings": [],
        },
    )


def _clean_scan(store: SqliteRunStore) -> None:
    _scan(store, verdict="clean")


def _seed(db: Path) -> None:
    """The artboard's item: implemented, published+scanned, round 1 reviewing."""
    store = SqliteRunStore(str(db))
    try:
        store.save(_impl_run())
        store.save(_review_run())
        _clean_scan(store)
    finally:
        store.close()


def _config() -> DispatchConfig:
    return DispatchConfig(project=ProjectConfig(repo="kew-dev/kew"))


@pytest.fixture
def seeded(tmp_path: Path) -> Path:
    db = tmp_path / "dispatch.db"
    _seed(db)
    return db


def _stages(dto: Any) -> dict[str, Any]:
    assert len(dto.lanes) == 1
    return {stage.stage: stage for stage in dto.lanes[0].stages}


# --------------------------------------------------------------------------- #
# The main lane, cell by cell.
# --------------------------------------------------------------------------- #


def test_the_fixture_item_reproduces_the_artboard_main_lane(seeded: Path) -> None:
    store = SqliteRunStore(str(seeded))
    try:
        dto = item_pipeline(store, ITEM, config=_config())
    finally:
        store.close()
    assert dto is not None
    assert dto.item_id == ITEM
    assert [lane.name for lane in dto.lanes] == ["main"]
    assert [s.stage for s in dto.lanes[0].stages] == [
        "implement",
        "publish",
        "review",
        "merge",
    ]
    assert dto.unavailable == []

    stages = _stages(dto)
    assert (stages["implement"].state, stages["implement"].label) == (
        "done",
        f"run {IMPL_RUN}",
    )
    assert stages["implement"].detail == "success"
    assert stages["implement"].evidence.kind == "run"
    assert stages["implement"].evidence.ref == IMPL_RUN

    assert (stages["publish"].state, stages["publish"].label) == ("done", "scan clean")
    assert stages["publish"].detail == HEAD_SHA[:8] == "3670e43f"
    assert stages["publish"].evidence.kind == "output_scan"
    assert stages["publish"].evidence.ref == HEAD_SHA

    assert (stages["review"].state, stages["review"].label) == ("working", "round 1")
    assert stages["review"].detail == "reviewing"
    assert stages["review"].evidence is None

    # NOT "needs you · merge decision": round 1 is reading this very pull
    # request two cells to the left (#2279/D8).
    assert (stages["merge"].state, stages["merge"].label) == ("pending", "after review")
    assert stages["merge"].detail == "the review verdict decides"
    assert stages["merge"].evidence.kind == "pull_request"
    assert stages["merge"].evidence.ref == PR_URL


def test_a_live_reviewer_does_not_erase_the_published_artifact(seeded: Path) -> None:
    """Regression: the reviewer is the newest run on the item and carries no
    pull request, so a view composed with it as "latest" would report the
    artifact absent — the exact three-cell state the swimlane exists to draw."""
    store = SqliteRunStore(str(seeded))
    try:
        stages = _stages(item_pipeline(store, ITEM, config=_config()))
    finally:
        store.close()
    assert stages["publish"].state == "done"
    # Published, and the merge is ahead of us rather than owed by anybody: the
    # narrowing that keeps the artifact visible must not also hide the round
    # from the judgment (see the next test).
    assert stages["merge"].state == "pending"
    assert stages["merge"].evidence.ref == PR_URL


def test_the_swimlane_never_asks_for_a_decision_mid_round(seeded: Path) -> None:
    """One pull request, one owner (#2279/D8).

    The regression this closes is a narrowing, not a missing rule: the merge
    cell is worded from ``judgment.state``, which read ``under_review`` only
    while the facts it was composed from still held the review run — and
    ``_collect`` had just narrowed them to the implementation attempts, so the
    judgment answered "awaiting a person" for an item whose round two cells to
    the left was still reading the artifact. The two cells are now ONE reading
    of ``review_in_flight`` (``WorkFacts.runs_on_item``).
    """
    store = SqliteRunStore(str(seeded))
    try:
        dto = item_pipeline(store, ITEM, config=_config())
    finally:
        store.close()
    stages = _stages(dto)
    assert (stages["review"].state, stages["review"].detail) == ("working", "reviewing")
    assert stages["merge"].state != "needs_you"
    assert (stages["merge"].label, stages["merge"].detail) == (
        "after review",
        "the review verdict decides",
    )


def test_a_round_that_will_never_report_hands_the_decision_back(
    tmp_path: Path,
) -> None:
    """The other half: the bound cannot be "the merge cell is never needs-you".

    A pending review row past :data:`kew.service.work.PENDING_RUN_MAX_AGE_S`
    is the dead row it is, and the decision it was holding goes back to the
    person it was always waiting on — in the same cell, with the shipped words.
    """
    db = tmp_path / "dispatch.db"
    store = SqliteRunStore(str(db))
    try:
        store.save(_impl_run())
        store.save(
            _review_run(
                started_at=datetime.now(UTC)
                - timedelta(seconds=PENDING_RUN_MAX_AGE_S + 60)
            )
        )
        _clean_scan(store)
        stages = _stages(item_pipeline(store, ITEM, config=_config()))
    finally:
        store.close()
    assert (stages["merge"].state, stages["merge"].label) == ("needs_you", "needs you")
    assert stages["merge"].detail == "merge decision"
    # And the review cell agrees it is not reading anything: one predicate.
    assert stages["review"].state != "working"


def test_the_lane_is_named_for_the_configured_base_branch(seeded: Path) -> None:
    config = DispatchConfig(
        project=ProjectConfig(repo="kew-dev/kew"),
        branches=BranchConfig(base="release/2.0"),
    )
    store = SqliteRunStore(str(seeded))
    try:
        dto = item_pipeline(store, ITEM, config=config)
    finally:
        store.close()
    assert [lane.name for lane in dto.lanes] == ["release/2.0"]


@pytest.mark.parametrize(
    ("verdict", "state", "detail"),
    [
        ("READY", "done", "ready"),
        ("NEEDS_CHANGES", "not_ready", "needs changes"),
        ("NEEDS_FULL", "not_ready", "needs a full round"),
        ("UNPARSEABLE", "needs_you", "reviewer output unreadable"),
        ("DEGRADED", "done", "degraded"),
    ],
)
def test_a_finished_round_is_worded_from_its_recorded_verdict(
    tmp_path: Path, verdict: str, state: str, detail: str
) -> None:
    db = tmp_path / "dispatch.db"
    store = SqliteRunStore(str(db))
    try:
        store.save(_impl_run())
        _clean_scan(store)
        store.record_review_round(
            "epic:factory-ui", ITEM, head_sha=HEAD_SHA, verdict=verdict
        )
        stages = _stages(item_pipeline(store, ITEM, config=_config()))
    finally:
        store.close()
    assert (stages["review"].state, stages["review"].detail) == (state, detail)
    assert stages["review"].label == "round 1"
    assert stages["review"].evidence.kind == "review_verdict"
    assert stages["review"].evidence.ref == HEAD_SHA


def test_an_accepted_round_reads_ready_with_residuals(tmp_path: Path) -> None:
    """#2151: the swimlane words the round from the driver's acceptance, not
    from the reviewer's standing NEEDS_CHANGES, which read as "working" while
    the node was merge-ready."""
    db = tmp_path / "dispatch.db"
    store = SqliteRunStore(str(db))
    try:
        store.save(_impl_run())
        _clean_scan(store)
        store.record_review_round(
            "epic:factory-ui", ITEM, head_sha=HEAD_SHA, verdict="NEEDS_CHANGES"
        )
        assert store.accept_review_round(
            ITEM,
            round_num=1,
            head_sha=HEAD_SHA,
            sidecar_digest=None,
            verdict="READY_WITH_RESIDUALS",
        )
        stages = _stages(item_pipeline(store, ITEM, config=_config()))
    finally:
        store.close()
    assert (stages["review"].state, stages["review"].detail) == (
        "done",
        "ready with residuals",
    )


def test_a_second_round_in_flight_counts_past_the_recorded_one(
    tmp_path: Path,
) -> None:
    db = tmp_path / "dispatch.db"
    store = SqliteRunStore(str(db))
    try:
        store.save(_impl_run())
        store.save(_review_run())
        store.record_review_round(
            "epic:factory-ui", ITEM, head_sha=HEAD_SHA, verdict="NEEDS_CHANGES"
        )
        stages = _stages(item_pipeline(store, ITEM, config=_config()))
    finally:
        store.close()
    assert stages["review"].label == "round 2"
    assert stages["review"].detail == "reviewing"


def test_an_unpublished_item_has_nothing_to_review_or_merge(tmp_path: Path) -> None:
    db = tmp_path / "dispatch.db"
    store = SqliteRunStore(str(db))
    try:
        store.save(_impl_run(status="pending", pr_url=None, pr_state=None))
        stages = _stages(item_pipeline(store, ITEM, config=_config()))
    finally:
        store.close()
    assert (stages["implement"].state, stages["implement"].detail) == (
        "working",
        "in flight",
    )
    assert stages["publish"].state == "not_required"
    assert stages["review"].state == "pending"
    assert stages["merge"].state == "pending"


def test_a_preserved_artifact_is_publishing(tmp_path: Path) -> None:
    db = tmp_path / "dispatch.db"
    store = SqliteRunStore(str(db))
    try:
        store.save(_impl_run(status="awaiting_publication", pr_url=None, pr_state=None))
        stages = _stages(item_pipeline(store, ITEM, config=_config()))
    finally:
        store.close()
    assert stages["implement"].state == "done"
    assert (stages["publish"].state, stages["publish"].label) == (
        "working",
        "publishing",
    )
    assert stages["publish"].detail == "agent/issue-2059"


def test_a_failed_run_is_a_failed_implement_cell(tmp_path: Path) -> None:
    db = tmp_path / "dispatch.db"
    store = SqliteRunStore(str(db))
    try:
        store.save(_impl_run(status="failure", pr_url=None, pr_state=None))
        stages = _stages(item_pipeline(store, ITEM, config=_config()))
    finally:
        store.close()
    assert (stages["implement"].state, stages["implement"].detail) == (
        "failed",
        "failure",
    )


def test_a_credential_in_the_push_makes_publish_need_a_person(
    tmp_path: Path,
) -> None:
    db = tmp_path / "dispatch.db"
    store = SqliteRunStore(str(db))
    try:
        store.save(_impl_run())
        _scan(store, verdict="found")
        stages = _stages(item_pipeline(store, ITEM, config=_config()))
    finally:
        store.close()
    assert (stages["publish"].state, stages["publish"].label) == (
        "needs_you",
        "scan found",
    )
    assert stages["publish"].evidence.ref == HEAD_SHA


def test_a_legacy_scan_is_found_past_a_newer_run_of_another_kind(
    tmp_path: Path,
) -> None:
    """Regression: a pre-item_id row is keyed on run_id alone, and the run that
    recorded the scan is the one that PUSHED — never the reviewer that started
    after it. Joining the fallback to the newest row on the item hid a recorded
    ``found`` behind a green "published / no scan recorded"."""
    db = tmp_path / "dispatch.db"
    store = SqliteRunStore(str(db))
    try:
        store.save(_impl_run())
        store.save(_review_run())  # the newest run on the item, and not a push
        _scan(store, verdict="found", item_id=None)
        stages = _stages(item_pipeline(store, ITEM, config=_config()))
    finally:
        store.close()
    assert (stages["publish"].state, stages["publish"].label) == (
        "needs_you",
        "scan found",
    )
    assert stages["publish"].evidence.ref == HEAD_SHA


def test_a_legacy_review_verdict_is_found_past_a_newer_implementation_run(
    tmp_path: Path,
) -> None:
    """The same walk, from the other end: the verdict is recorded under the
    REVIEWER's run, so a newer implementation attempt must not hide it."""
    db = tmp_path / "dispatch.db"
    store = SqliteRunStore(str(db))
    try:
        # An explicitly OLD round: this test is about a verdict recorded
        # under the reviewer's run being found past a NEWER implementation
        # attempt, so the reviewer has to predate that attempt.
        store.save(
            _review_run(
                status="success", started_at=datetime(2026, 9, 24, 2, tzinfo=UTC)
            )
        )
        store.save(_impl_run(started_at=datetime(2026, 9, 24, 3, tzinfo=UTC)))
        # A pre-v32 round row: no head of its own, so the evidence ref can
        # only come from the attested event the run walk reaches.
        store.record_review_round("epic:factory-ui", ITEM, head_sha="", verdict="READY")
        emit(
            store,
            REVIEW_VERDICT_EVENT,
            actor=Actor("policy", "driver"),
            run_id="rev-1",
            item_id=None,
            payload={"verdict": "READY", "head_sha": HEAD_SHA},
        )
        stages = _stages(item_pipeline(store, ITEM, config=_config()))
    finally:
        store.close()
    assert stages["review"].evidence is not None
    assert stages["review"].evidence.ref == HEAD_SHA


def test_a_later_clean_scan_does_not_clear_a_published_credential(
    tmp_path: Path,
) -> None:
    """Regression: ``credential_already_published`` says the value reached the
    remote branch, which a clean scan of a LATER head does not undo — only a
    person rotating the credential does."""
    db = tmp_path / "dispatch.db"
    store = SqliteRunStore(str(db))
    try:
        store.save(_impl_run())
        _scan(store, verdict="found", already_published=True)
        emit(
            store,
            ALREADY_PUBLISHED_EVENT,
            actor=Actor("policy", "host-publication"),
            run_id=IMPL_RUN,
            item_id=ITEM,
            payload={
                "head_sha": HEAD_SHA,
                "findings": [],
                "operator_action": "rotate the credential; remove the commit",
            },
        )
        _scan(store, verdict="clean", head=OTHER_HEAD)
        stages = _stages(item_pipeline(store, ITEM, config=_config()))
    finally:
        store.close()
    assert (stages["publish"].state, stages["publish"].label) == (
        "needs_you",
        "scan found",
    )
    assert stages["publish"].detail == "a credential reached the remote branch"
    assert stages["publish"].evidence.ref == HEAD_SHA


def test_a_rescan_of_the_same_head_cannot_clear_a_found_verdict(
    tmp_path: Path,
) -> None:
    """Regression: the push gate's own reader answers ``found`` for a head any
    scan found a credential in, "earlier or later" — so a later clean record of
    that same head must not turn the cell green while the host refuses."""
    db = tmp_path / "dispatch.db"
    store = SqliteRunStore(str(db))
    try:
        _scan(store, verdict="found")
        _scan(store, verdict="clean")
        store.save(_impl_run())
        stages = _stages(item_pipeline(store, ITEM, config=_config()))
    finally:
        store.close()
    assert (stages["publish"].state, stages["publish"].label) == (
        "needs_you",
        "scan found",
    )


def test_a_scan_verdict_this_projection_cannot_word_is_unknown(
    tmp_path: Path,
) -> None:
    """Regression: only ``found`` and ``clean`` are rendered. A verdict from a
    newer host is a fact that was read but not understood, and "not found" is
    not the same as "clean" — the host's own reader answers ``missing`` there
    and refuses the push."""
    db = tmp_path / "dispatch.db"
    store = SqliteRunStore(str(db))
    try:
        store.save(_impl_run())
        _scan(store, verdict="quarantined")
        dto = item_pipeline(store, ITEM, config=_config())
    finally:
        store.close()
    publish = _stages(dto)["publish"]
    assert (publish.state, publish.label) == ("unknown", "unknown")
    assert publish.detail == "the scan verdict could not be read"
    assert UNREADABLE_AUDIT in dto.unavailable


def test_a_published_artifact_nobody_scanned_says_so(tmp_path: Path) -> None:
    db = tmp_path / "dispatch.db"
    store = SqliteRunStore(str(db))
    try:
        store.save(_impl_run())
        stages = _stages(item_pipeline(store, ITEM, config=_config()))
    finally:
        store.close()
    # Not the green ``done``: nothing vouches for this push (D25). The PR is
    # up all the same, and ``passed`` says so in a field, not in the label.
    assert (stages["publish"].state, stages["publish"].label) == (
        "pending",
        "published",
    )
    assert stages["publish"].passed is True
    assert [name for name, stage in stages.items() if stage.passed] == ["publish"]
    assert stages["publish"].detail == "no scan recorded"
    assert stages["publish"].evidence.kind == "pull_request"


def test_a_merged_pull_request_settles_the_merge_cell(tmp_path: Path) -> None:
    db = tmp_path / "dispatch.db"
    store = SqliteRunStore(str(db))
    try:
        store.save(_impl_run(pr_state="MERGED"))
        stages = _stages(item_pipeline(store, ITEM, config=_config()))
    finally:
        store.close()
    assert (stages["merge"].state, stages["merge"].label) == ("done", "merged")


def test_an_unobserved_pull_request_is_unknown_and_named(tmp_path: Path) -> None:
    """A published artifact nobody could read is not a clean "pending"."""
    db = tmp_path / "dispatch.db"
    store = SqliteRunStore(str(db))
    try:
        store.save(_impl_run(pr_state=None))
        dto = item_pipeline(store, ITEM, config=_config())
    finally:
        store.close()
    assert _stages(dto)["merge"].state == "unknown"
    assert UNREADABLE_JUDGMENT in dto.unavailable


def test_an_item_with_no_runs_has_no_pipeline(seeded: Path) -> None:
    store = SqliteRunStore(str(seeded))
    try:
        assert item_pipeline(store, "9999", config=_config()) is None
    finally:
        store.close()


def test_no_config_still_projects_against_the_default_lane(seeded: Path) -> None:
    store = SqliteRunStore(str(seeded))
    try:
        dto = item_pipeline(store, ITEM)
    finally:
        store.close()
    assert [lane.name for lane in dto.lanes] == ["main"]


# --------------------------------------------------------------------------- #
# Degradation: a store that cannot answer must say so, not answer cleanly.
# --------------------------------------------------------------------------- #


class _NoReviewStore:
    """A store predating the review-rounds table (or a double that never had
    it): every other read works, so only the review cell may degrade."""

    def __init__(self, runs: list[RunRecord]) -> None:
        self._runs = runs

    def read_snapshot(self) -> Any:
        return nullcontext()

    def list_by_item(self, item_id: str) -> list[RunRecord]:
        return [r for r in self._runs if r.item_id == item_id]

    def get_admission_identity(self, run_id: str) -> None:
        return None

    def list_orphan_prs(self, include_acked: bool = False) -> list[Any]:
        return []

    def get_fleet_stopped(self) -> None:
        return None

    def list_audit_events(self, **kwargs: Any) -> list[Any]:
        return []


def test_a_store_without_review_rounds_is_unknown_with_the_reason_listed() -> None:
    dto = item_pipeline(_NoReviewStore([_impl_run()]), ITEM, config=_config())
    assert dto is not None
    review = _stages(dto)["review"]
    assert review.state == "unknown"
    assert review.detail == "the review rounds could not be read"
    assert UNREADABLE_ROUNDS in dto.unavailable
    # The other cells still answer: one unreadable table is not a blackout.
    assert _stages(dto)["implement"].state == "done"


class _UnreadableRow:
    """A recorded audit row whose payload did not parse: ``list_audit_events``
    coerces it to ``None`` rather than dropping the row."""

    payload = None


def test_an_unreadable_audit_table_makes_publish_unknown_not_published() -> None:
    """Regression: naming UNREADABLE_AUDIT while the cell still asserts
    done/"no scan recorded" is the exact contradiction the module forbids —
    "we could not look" is not "we looked, nothing scanned it"."""
    store = _NoReviewStore([_impl_run()])

    def _boom(**kwargs: Any) -> list[Any]:
        raise RuntimeError("audit_events is gone")

    store.list_audit_events = _boom  # type: ignore[method-assign]
    dto = item_pipeline(store, ITEM, config=_config())
    publish = _stages(dto)["publish"]
    assert (publish.state, publish.label) == ("unknown", "unknown")
    assert publish.detail == "the scan verdict could not be read"
    assert UNREADABLE_AUDIT in dto.unavailable


def test_a_scan_row_whose_payload_cannot_be_read_is_unknown() -> None:
    """Regression: an unparseable payload arrives as ``None``, which must not
    fall through to a green "scan clean" cell."""
    store = _NoReviewStore([_impl_run()])

    def _rows(**kwargs: Any) -> list[Any]:
        return [_UnreadableRow()] if kwargs.get("event") == OUTPUT_SCAN_EVENT else []

    store.list_audit_events = _rows  # type: ignore[method-assign]
    dto = item_pipeline(store, ITEM, config=_config())
    assert _stages(dto)["publish"].state == "unknown"
    assert UNREADABLE_AUDIT in dto.unavailable


def test_a_raising_review_reader_degrades_the_same_way() -> None:
    store = _NoReviewStore([_impl_run()])

    def _boom(item_id: str) -> list[Any]:
        raise RuntimeError("review_rounds is gone")

    store.review_rounds_in_cycle = _boom  # type: ignore[attr-defined]
    dto = item_pipeline(store, ITEM, config=_config())
    assert _stages(dto)["review"].state == "unknown"
    assert UNREADABLE_ROUNDS in dto.unavailable


# --------------------------------------------------------------------------- #
# The serve route.
# --------------------------------------------------------------------------- #


def _mk_app(db: Path, tmp_path: Path, *, expose: bool = False) -> Any:
    return create_app(
        lambda: SqliteRunStore(str(db)),
        repo_dir=tmp_path,
        token="TESTTOKEN",  # noqa: S106 — test-only session token
        sessions=SessionStore(),
        expose=expose,
        config=_config(),
    )


def test_route_returns_the_projection(
    seeded: Path, tmp_path: Path, inbox_client: Any
) -> None:
    client = inbox_client(_mk_app(seeded, tmp_path), loopback_peer=True)
    response = client.get(PATH)
    assert response.status_code == 200
    body = response.json()
    assert set(body) >= {"item_id", "lanes", "unavailable", "composed_at"}
    cells = {s["stage"]: s for s in body["lanes"][0]["stages"]}
    assert cells["implement"]["label"] == f"run {IMPL_RUN}"
    assert cells["publish"]["label"] == "scan clean"
    assert cells["review"]["label"] == "round 1"
    # The fixture's round is still reading the pull request, so the merge is
    # ahead of the verdict rather than owed by a person (#2279/D8).
    assert cells["merge"]["state"] == "pending"
    assert cells["merge"]["label"] == "after review"


def test_route_404s_on_an_unknown_item(
    seeded: Path, tmp_path: Path, inbox_client: Any
) -> None:
    client = inbox_client(_mk_app(seeded, tmp_path), loopback_peer=True)
    assert client.get("/api/work/9999/pipeline").status_code == 404


@pytest.fixture
def ready_1177(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ready queue as the dashboard last read it: #1177, idle, never run."""
    issue = ReadyIssueDTO(issue_num=1177, item_id="1177", title="MODEL_PRICING drift")
    # Expired on purpose: a row the console drew from a stale read is still an
    # item someone can select, and the pipeline route must never refill it.
    monkeypatch.setitem(dashboard_module._ready_cache, "kew-dev/kew", (0.0, [issue]))


def _assert_no_run_pipeline(body: dict[str, Any], item_id: str) -> None:
    assert body["item_id"] == item_id
    assert body["unavailable"] == []
    assert [lane["name"] for lane in body["lanes"]] == ["main"]
    stages = body["lanes"][0]["stages"]
    assert [s["stage"] for s in stages] == ["implement", "publish", "review", "merge"]
    assert {s["state"] for s in stages} == {"pending"}
    assert all(s["evidence"] is None for s in stages)


def test_route_answers_four_pending_stages_for_a_ready_item_with_no_runs(
    seeded: Path, tmp_path: Path, inbox_client: Any, ready_1177: None
) -> None:
    """#2572: an item with no runs has an empty pipeline, not a missing one."""
    client = inbox_client(_mk_app(seeded, tmp_path), loopback_peer=True)
    response = client.get("/api/work/1177/pipeline")
    assert response.status_code == 200
    _assert_no_run_pipeline(response.json(), "1177")
    # Knowing #1177 does not make every id known.
    assert client.get("/api/work/9999/pipeline").status_code == 404


def test_route_answers_four_pending_stages_for_an_unstarted_epic_node(
    seeded: Path, tmp_path: Path, inbox_client: Any
) -> None:
    store = SqliteRunStore(str(seeded))
    try:
        store.append_epic_tick(
            epic_ref="console-qol",
            tick_ts="2026-10-08T00:00:00+00:00",
            snapshot={"nodes": [{"id": "SFD-12"}, {"id": 4242}]},
            actions={},
            snapshot_hash="h1",
        )
    finally:
        store.close()
    client = inbox_client(_mk_app(seeded, tmp_path), loopback_peer=True)
    for item_id in ("SFD-12", "4242"):
        response = client.get(f"/api/work/{item_id}/pipeline")
        assert response.status_code == 200
        _assert_no_run_pipeline(response.json(), item_id)
    assert client.get("/api/work/9999/pipeline").status_code == 404


def test_the_evidence_chain_still_reads_no_runs_as_no_pipeline(
    seeded: Path, ready_1177: None
) -> None:
    """The empty pipeline is the route's answer; ``item_pipeline`` keeps its
    ``None``, which the evidence chain reads as "nothing ran"."""
    store = SqliteRunStore(str(seeded))
    try:
        assert item_pipeline(store, "1177", config=_config()) is None
    finally:
        store.close()


def test_route_never_emits_a_reproduce_stage_or_a_backport_lane(
    seeded: Path, tmp_path: Path, inbox_client: Any
) -> None:
    """kew serves no Reproduce stage and no release/[branch] lane."""
    client = inbox_client(_mk_app(seeded, tmp_path), loopback_peer=True)
    body = client.get(PATH).json()
    assert [lane["name"] for lane in body["lanes"]] == ["main"]
    assert "reproduce" not in {s["stage"] for s in body["lanes"][0]["stages"]}


def test_exposed_read_requires_a_session(
    seeded: Path, tmp_path: Path, inbox_client: Any
) -> None:
    client = inbox_client(_mk_app(seeded, tmp_path, expose=True), loopback_peer=True)
    assert client.get(PATH).status_code == 401


def test_non_loopback_read_requires_a_session(
    seeded: Path, tmp_path: Path, inbox_client: Any
) -> None:
    client = inbox_client(_mk_app(seeded, tmp_path), loopback_peer=False)
    assert client.get(PATH).status_code == 401


def test_the_404_is_also_behind_the_guard(
    seeded: Path, tmp_path: Path, inbox_client: Any
) -> None:
    """An unauthenticated caller must not learn which item ids exist."""
    client = inbox_client(_mk_app(seeded, tmp_path, expose=True), loopback_peer=True)
    assert client.get("/api/work/9999/pipeline").status_code == 401


def test_the_router_is_read_only_and_guarded() -> None:
    """Every route resolves ``read_guard`` and none resolves ``require_write``."""
    for route in route_deps.api_routes(api_work_module.work_router):
        resolved = route_deps.dependency_calls(route)
        assert read_guard in resolved, route.path
        assert require_write not in resolved, route.path
        assert set(route.methods) == {"GET"}, route.path


# --------------------------------------------------------------------------- #
# The merge verify reading (#3140).
# --------------------------------------------------------------------------- #

FAILED_TESTS = [
    "tests/test_store.py::test_reservations_expire",
    "tests/test_store.py::test_claim_is_exclusive",
]


#: Main, as the drive fetched it: the second parent of every merge it verifies.
MAIN_SHA = "0123456789abcdef0123456789abcdef01234567"
#: A head of an earlier review cycle, and a merge commit made on it.
OLD_CYCLE_HEAD = "5555555555555555555555555555555555555555"
#: The head the drive's newest snapshot holds, newer than every recorded one.
SNAPSHOT_HEAD = "7777777777777777777777777777777777777777"
EPIC = "epic:factory-ui"


def _merge_of(head: str) -> str:
    """The sha of a merge commit the drive made of main on *head* (synthetic)."""
    return head[::-1]


def _verify(
    store: SqliteRunStore,
    *,
    ref_sha: str,
    outcome: Any,
    recorded_at: str,
    ref_parents: Any = None,
    held_out: dict[str, Any] | None = None,
    error_category: str | None = None,
) -> None:
    """One ``verify_completed`` row, shaped as ``record_verify_event`` writes it.

    ``ref_parents=None`` leaves the key out: the shape of an event recorded
    before the producer wrote the parents.
    """
    payload: dict[str, Any] = {
        "ref": ref_sha,
        "ref_sha": ref_sha,
        "base_ref": "main",
        "base_sha": MAIN_SHA,
        "outcome": outcome,
        "error": "free text the work page must not re-print",
        "error_category": error_category,
        "recorded_at": recorded_at,
        "held_out": held_out,
    }
    if ref_parents is not None:
        payload["ref_parents"] = ref_parents
    emit(
        store,
        VERIFY_EVENT,
        actor=Actor("human", "operator"),
        repo="kew-dev/kew",
        payload=payload,
    )


def _verify_merge_of(
    store: SqliteRunStore,
    head: str,
    *,
    outcome: Any,
    recorded_at: str,
    head_first: bool = True,
    **kw: Any,
) -> None:
    """A verify of a merge commit of main and *head*.

    ``head_first`` is the drive's: a ``git merge --no-ff main`` made ON the
    head. Without it, the head is the second parent, as when an operator's ad
    hoc merge checks out main and merges the PR in.
    """
    _verify(
        store,
        ref_sha=_merge_of(head),
        ref_parents=[head, MAIN_SHA] if head_first else [MAIN_SHA, head],
        outcome=outcome,
        recorded_at=recorded_at,
        **kw,
    )


def _pipeline_of(db: Path) -> Any:
    store = SqliteRunStore(str(db))
    try:
        return item_pipeline(store, ITEM, config=_config())
    finally:
        store.close()


def test_a_failed_verify_serves_the_held_out_tests_that_failed(seeded: Path) -> None:
    """The drive verified a merge commit whose first parent is the PR head."""
    store = SqliteRunStore(str(seeded))
    try:
        _verify_merge_of(
            store,
            HEAD_SHA,
            outcome="failed",
            recorded_at="2026-09-24T02:00:00+00:00",
            held_out={
                "outcome": "failed",
                "failed": [*FAILED_TESTS, "tests/test_store.py::test_renamed"],
                "declared": ["tests/test_store.py::test_renamed"],
                "undeclared": FAILED_TESTS,
                "seconds": 41,
                "error": "2 undeclared held-out failures",
            },
        )
        # Another PR's verify, newer, on the same main: sharing the base parent
        # is not sharing a head, so it is never this item's.
        _verify_merge_of(
            store,
            "f" * 40,
            outcome="passed",
            recorded_at="2026-09-24T03:00:00+00:00",
        )
    finally:
        store.close()
    verify = _pipeline_of(seeded).merge_verify
    assert verify is not None
    assert (verify.outcome, verify.head, verify.is_current_head) == (
        "failed",
        HEAD_SHA,
        True,
    )
    # A held-out refusal is no host failure: kew records no category for it.
    assert verify.error_category is None
    assert verify.held_out is not None
    assert verify.held_out.outcome == "failed"
    assert verify.held_out.undeclared == FAILED_TESTS
    assert verify.held_out.declared == ["tests/test_store.py::test_renamed"]
    assert len(verify.held_out.failed) == 3
    dumped = verify.model_dump_json()
    assert "free text" not in dumped
    # The parents feed the join only; they never cross into the DTO.
    assert MAIN_SHA not in dumped
    assert "ref_parents" not in dumped


def test_a_verify_recorded_before_parents_joins_on_the_bare_head(
    seeded: Path,
) -> None:
    """An older event carries no ``ref_parents``: it links when it verified the
    head itself, and a merge commit's sha alone links nothing."""
    store = SqliteRunStore(str(seeded))
    try:
        _verify(
            store,
            ref_sha=HEAD_SHA,
            outcome="passed",
            recorded_at="2026-09-24T02:00:00+00:00",
        )
        _verify(
            store,
            ref_sha=_merge_of(HEAD_SHA),
            outcome="failed",
            recorded_at="2026-09-24T03:00:00+00:00",
        )
    finally:
        store.close()
    verify = _pipeline_of(seeded).merge_verify
    assert verify is not None
    assert (verify.outcome, verify.head) == ("passed", HEAD_SHA)


def test_the_newest_verify_of_any_reviewed_head_wins(seeded: Path) -> None:
    """A round's head joins too, and ``recorded_at`` — not row order — decides."""
    store = SqliteRunStore(str(seeded))
    try:
        store.record_review_round(EPIC, ITEM, head_sha=OTHER_HEAD, verdict="READY")
        _verify_merge_of(
            store,
            OTHER_HEAD,
            outcome="passed",
            recorded_at="2026-09-25T00:00:00+00:00",
        )
        _verify_merge_of(
            store,
            HEAD_SHA,
            outcome="failed",
            recorded_at="2026-09-24T00:00:00+00:00",
        )
    finally:
        store.close()
    verify = _pipeline_of(seeded).merge_verify
    assert verify is not None
    assert (verify.outcome, verify.head) == ("passed", OTHER_HEAD)
    # The newest round's head is the current one when no snapshot names one.
    assert verify.is_current_head is True
    assert verify.held_out is None


def test_a_verify_that_did_not_finish_does_not_hide_the_verdict_at_its_head(
    seeded: Path,
) -> None:
    """A re-verify of the same head that ended with no verdict is newer, but
    the failure before it is what is known about that head."""
    store = SqliteRunStore(str(seeded))
    try:
        _verify_merge_of(
            store,
            HEAD_SHA,
            outcome="failed",
            recorded_at="2026-09-24T02:00:00+00:00",
            held_out={
                "outcome": "failed",
                "failed": FAILED_TESTS,
                "declared": [],
                "undeclared": FAILED_TESTS,
            },
        )
        _verify_merge_of(
            store,
            HEAD_SHA,
            outcome="incomplete",
            recorded_at="2026-09-24T03:00:00+00:00",
            error_category="host_error",
        )
    finally:
        store.close()
    verify = _pipeline_of(seeded).merge_verify
    assert verify is not None
    assert (verify.outcome, verify.head, verify.is_current_head) == (
        "failed",
        HEAD_SHA,
        True,
    )
    assert verify.recorded_at == "2026-09-24T02:00:00+00:00"
    assert verify.held_out is not None
    assert verify.held_out.undeclared == FAILED_TESTS


def test_a_pass_stands_when_a_later_verify_of_its_head_has_no_verdict(
    seeded: Path,
) -> None:
    """A re-verify of a passed head that did not finish knows nothing new about
    it: the pass is still the reading."""
    store = SqliteRunStore(str(seeded))
    try:
        _verify_merge_of(
            store,
            HEAD_SHA,
            outcome="passed",
            recorded_at="2026-09-24T11:00:00+00:00",
        )
        _verify_merge_of(
            store,
            HEAD_SHA,
            outcome="incomplete",
            recorded_at="2026-09-24T12:00:00+00:00",
            error_category="host_error",
        )
    finally:
        store.close()
    verify = _pipeline_of(seeded).merge_verify
    assert verify is not None
    assert (verify.outcome, verify.head) == ("passed", HEAD_SHA)
    assert verify.recorded_at == "2026-09-24T11:00:00+00:00"
    assert verify.error_category is None


def test_a_later_row_whose_outcome_is_not_a_string_leaves_the_failure_served(
    seeded: Path,
) -> None:
    """A malformed outcome is no verdict, and never a raise out of the join."""
    store = SqliteRunStore(str(seeded))
    try:
        _verify_merge_of(
            store,
            HEAD_SHA,
            outcome="failed",
            recorded_at="2026-09-24T11:00:00+00:00",
        )
        _verify_merge_of(
            store,
            HEAD_SHA,
            outcome=["failed"],
            recorded_at="2026-09-24T11:30:00+00:00",
        )
    finally:
        store.close()
    verify = _pipeline_of(seeded).merge_verify
    assert verify is not None
    assert (verify.outcome, verify.head) == ("failed", HEAD_SHA)
    assert verify.recorded_at == "2026-09-24T11:00:00+00:00"


def test_a_head_with_no_verdict_serves_its_unfinished_verify(seeded: Path) -> None:
    """A pass at an older head is no verdict on the newer one: the newer head's
    unfinished verify is served."""
    store = SqliteRunStore(str(seeded))
    try:
        store.record_review_round(EPIC, ITEM, head_sha=OTHER_HEAD, verdict="READY")
        _verify_merge_of(
            store,
            HEAD_SHA,
            outcome="passed",
            recorded_at="2026-09-24T00:00:00+00:00",
        )
        _verify_merge_of(
            store,
            OTHER_HEAD,
            outcome="incomplete",
            recorded_at="2026-09-25T00:00:00+00:00",
            error_category="host_error",
        )
    finally:
        store.close()
    verify = _pipeline_of(seeded).merge_verify
    assert verify is not None
    assert (verify.outcome, verify.head, verify.is_current_head) == (
        "incomplete",
        OTHER_HEAD,
        True,
    )
    assert verify.error_category == "host_error"


def test_a_verify_of_an_older_head_is_marked_not_current(seeded: Path) -> None:
    """The drive's snapshot holds a newer head than the one that was verified."""
    store = SqliteRunStore(str(seeded))
    try:
        store.record_review_round(EPIC, ITEM, head_sha=HEAD_SHA, verdict="READY")
        store.append_epic_tick(
            epic_ref=EPIC,
            tick_ts="2026-09-25T00:00:00+00:00",
            snapshot={
                "epic_ref": EPIC,
                "signal": "progressing",
                "counts": {},
                "nodes": [
                    {
                        "id": ITEM,
                        "status": "in_review",
                        "pr_url": PR_URL,
                        "head_oid": SNAPSHOT_HEAD,
                    }
                ],
            },
            actions={},
            snapshot_hash="h-snapshot",
        )
        _verify_merge_of(
            store,
            HEAD_SHA,
            outcome="passed",
            recorded_at="2026-09-24T05:00:00+00:00",
        )
    finally:
        store.close()
    verify = _pipeline_of(seeded).merge_verify
    assert verify is not None
    assert (verify.head, verify.is_current_head) == (HEAD_SHA, False)


def _review_verdict(store: SqliteRunStore, head: str, verdict: str) -> None:
    """The attested verdict the driver records beside each round."""
    emit(
        store,
        REVIEW_VERDICT_EVENT,
        actor=Actor("policy", "driver"),
        run_id="rev-1",
        item_id=ITEM,
        payload={"verdict": verdict, "head_sha": head},
    )


def test_a_previous_cycles_head_does_not_join_once_the_new_cycle_is_reviewed(
    tmp_path: Path,
) -> None:
    """``kew epic retry`` starts a new cycle: its rounds' heads are history.

    The ledger is written in the order the factory writes it: the old head is
    pushed (scanned), reviewed and given a verdict; the retry; then the new head
    is pushed, reviewed and given the new cycle's first verdict. From then on
    no recorded fact of the new cycle names the old head, so a verify of it is
    not this pull request's.
    """
    db = tmp_path / "dispatch.db"
    store = SqliteRunStore(str(db))
    try:
        store.save(_impl_run())
        _scan(store, verdict="clean", head=OLD_CYCLE_HEAD)
        store.record_review_round(
            EPIC, ITEM, head_sha=OLD_CYCLE_HEAD, verdict="NEEDS_CHANGES"
        )
        _review_verdict(store, OLD_CYCLE_HEAD, "NEEDS_CHANGES")
        _verify_merge_of(
            store,
            OLD_CYCLE_HEAD,
            outcome="failed",
            recorded_at="2026-09-24T05:00:00+00:00",
        )
        store.mark_epic_retry_item(EPIC, ITEM)
        _scan(store, verdict="clean", head=HEAD_SHA)
        store.record_review_round(EPIC, ITEM, head_sha=HEAD_SHA, verdict="READY")
        _review_verdict(store, HEAD_SHA, "READY")
        assert [r.head_sha for r in store.list_review_rounds(ITEM)] == [
            OLD_CYCLE_HEAD,
            HEAD_SHA,
        ]
        assert [r.head_sha for r in store.review_rounds_in_cycle(ITEM)] == [HEAD_SHA]
    finally:
        store.close()
    assert _pipeline_of(db).merge_verify is None


def test_a_stacked_prs_bare_verify_does_not_join_the_pr_below_it(seeded: Path) -> None:
    """A PR stacked on this one has this one's head as its only parent; its
    verify is of the stacked PR, not of the PR below it."""
    store = SqliteRunStore(str(seeded))
    try:
        _verify(
            store,
            ref_sha=OTHER_HEAD,
            ref_parents=[HEAD_SHA],
            outcome="passed",
            recorded_at="2026-09-24T05:00:00+00:00",
        )
    finally:
        store.close()
    assert _pipeline_of(seeded).merge_verify is None


@pytest.mark.parametrize("head_first", [True, False], ids=["drive", "ad-hoc"])
def test_a_merge_of_the_base_joins_through_its_other_parent(
    seeded: Path, head_first: bool
) -> None:
    """The drive merges main into the head (head first); an operator's ad hoc
    merge checks out main and merges the PR in (head second)."""
    store = SqliteRunStore(str(seeded))
    try:
        _verify_merge_of(
            store,
            HEAD_SHA,
            head_first=head_first,
            outcome="passed",
            recorded_at="2026-09-24T05:00:00+00:00",
        )
    finally:
        store.close()
    verify = _pipeline_of(seeded).merge_verify
    assert verify is not None
    assert (verify.outcome, verify.head) == ("passed", HEAD_SHA)


@pytest.mark.parametrize(
    "parents",
    [[HEAD_SHA, OTHER_HEAD], [OTHER_HEAD, HEAD_SHA]],
    ids=["head-first", "head-second"],
)
def test_a_merge_neither_of_whose_parents_is_the_base_does_not_join(
    seeded: Path, parents: list[str]
) -> None:
    """Two branches merged together, with the verify's base in neither parent:
    that is no merge of this PR onto the base, whichever parent names it."""
    store = SqliteRunStore(str(seeded))
    try:
        _verify(
            store,
            ref_sha="e" * 40,
            ref_parents=parents,
            outcome="passed",
            recorded_at="2026-09-24T05:00:00+00:00",
        )
    finally:
        store.close()
    assert _pipeline_of(seeded).merge_verify is None


def test_unread_head_events_never_read_as_no_verify_linked() -> None:
    """The scan and verdict rows name heads; without them the set is partial."""
    store = _NoReviewStore([_impl_run()])
    store.review_rounds_in_cycle = lambda item_id: []  # type: ignore[attr-defined]

    def _rows(**kwargs: Any) -> list[Any]:
        if kwargs.get("event") == VERIFY_EVENT:
            return []
        raise RuntimeError("audit_events is gone")

    store.list_audit_events = _rows  # type: ignore[method-assign]
    dto = item_pipeline(store, ITEM, config=_config())
    assert dto is not None
    assert UNREADABLE_ROUNDS not in dto.unavailable
    assert UNREADABLE_AUDIT in dto.unavailable
    assert dto.merge_verify is None
    assert UNREADABLE_MERGE_VERIFY in dto.unavailable


def test_held_out_lists_are_capped_and_long_ids_clipped(seeded: Path) -> None:
    long_id = "tests/test_params.py::test_input[" + "x" * 400 + "]"
    failed = [f"tests/test_many.py::test_{n}" for n in range(70)]
    store = SqliteRunStore(str(seeded))
    try:
        _verify_merge_of(
            store,
            HEAD_SHA,
            outcome="failed",
            recorded_at="2026-09-24T05:00:00+00:00",
            held_out={
                "outcome": "failed",
                "failed": [long_id, *failed],
                "declared": [long_id],
                "undeclared": failed,
            },
        )
    finally:
        store.close()
    verify = _pipeline_of(seeded).merge_verify
    assert verify is not None and verify.held_out is not None
    held = verify.held_out
    assert (len(held.failed), held.failed_more) == (50, 21)
    assert (len(held.undeclared), held.undeclared_more) == (50, 20)
    assert (held.declared_more, len(held.declared)) == (0, 1)
    clipped = held.failed[0]
    assert len(clipped) == 300
    kept, digest = clipped.rsplit("…", 1)
    assert long_id.startswith(kept) and len(digest) == 8
    assert clipped == held.declared[0]


def test_two_long_ids_with_one_head_stay_apart_when_clipped(seeded: Path) -> None:
    """Ids that share their first 300 characters must not merge into one.

    The page keys its list by id and marks a test excused by matching its id
    against the declared list, so a collision would hide a test or excuse the
    wrong one.
    """
    shared = "tests/test_params.py::test_input[" + "y" * 400
    excused, refused = shared + "a]", shared + "b]"
    store = SqliteRunStore(str(seeded))
    try:
        _verify_merge_of(
            store,
            HEAD_SHA,
            outcome="failed",
            recorded_at="2026-09-24T05:00:00+00:00",
            held_out={
                "outcome": "failed",
                "failed": [excused, refused],
                "declared": [excused],
                "undeclared": [refused],
            },
        )
    finally:
        store.close()
    verify = _pipeline_of(seeded).merge_verify
    assert verify is not None and verify.held_out is not None
    held = verify.held_out
    assert len(set(held.failed)) == 2
    assert held.declared == [held.failed[0]]
    assert held.undeclared == [held.failed[1]]
    assert held.declared != held.undeclared


@pytest.mark.parametrize(
    "malformed",
    [
        {"ref_sha": 7, "ref_parents": [HEAD_SHA]},
        {"ref_sha": "abc", "ref_parents": HEAD_SHA},
        {"ref_sha": "abc", "ref_parents": [HEAD_SHA, 7]},
        {"ref_sha": ["abc"], "ref_parents": None},
    ],
    ids=["number-ref", "string-parents", "number-parent", "list-ref"],
)
def test_a_malformed_verify_row_is_skipped_and_the_read_still_answers(
    seeded: Path, tmp_path: Path, inbox_client: Any, malformed: dict[str, Any]
) -> None:
    store = SqliteRunStore(str(seeded))
    try:
        _verify_merge_of(
            store,
            HEAD_SHA,
            outcome="failed",
            recorded_at="2026-09-24T01:00:00+00:00",
        )
        emit(
            store,
            VERIFY_EVENT,
            actor=Actor("human", "operator"),
            payload={
                "outcome": "passed",
                "recorded_at": "2026-09-24T09:00:00+00:00",
                **malformed,
            },
        )
    finally:
        store.close()
    client = inbox_client(_mk_app(seeded, tmp_path), loopback_peer=True)
    response = client.get(PATH)
    assert response.status_code == 200
    body = response.json()
    # The newer, malformed row is passed over; the well-formed one answers.
    assert body["merge_verify"]["outcome"] == "failed"
    assert body["merge_verify"]["head"] == HEAD_SHA
    assert UNREADABLE_MERGE_VERIFY not in body["unavailable"]


def test_no_verify_recorded_serves_an_explicit_none(
    seeded: Path, tmp_path: Path, inbox_client: Any
) -> None:
    assert _pipeline_of(seeded).merge_verify is None
    client = inbox_client(_mk_app(seeded, tmp_path), loopback_peer=True)
    body = client.get(PATH).json()
    assert "merge_verify" in body
    assert body["merge_verify"] is None


def test_an_unreadable_verify_read_is_named_not_a_clean_none() -> None:
    store = _NoReviewStore([_impl_run()])

    class _Scan:
        payload = {"verdict": "clean", "head_sha": HEAD_SHA}
        run_id = IMPL_RUN

    def _rows(**kwargs: Any) -> list[Any]:
        if kwargs.get("event") == VERIFY_EVENT:
            raise RuntimeError("audit_events is gone")
        return [_Scan()] if kwargs.get("event") == OUTPUT_SCAN_EVENT else []

    store.list_audit_events = _rows  # type: ignore[method-assign]
    dto = item_pipeline(store, ITEM, config=_config())
    assert dto is not None
    assert dto.merge_verify is None
    assert UNREADABLE_MERGE_VERIFY in dto.unavailable
    # The scan still answered, so the publish cell is not dragged down with it.
    assert _stages(dto)["publish"].label == "scan clean"


def test_unread_rounds_never_read_as_no_verify_linked() -> None:
    """Without the rounds the head set is partial, so "none linked" is named."""
    dto = item_pipeline(_NoReviewStore([_impl_run()]), ITEM, config=_config())
    assert dto is not None
    assert dto.merge_verify is None
    assert UNREADABLE_ROUNDS in dto.unavailable
    assert UNREADABLE_MERGE_VERIFY in dto.unavailable
