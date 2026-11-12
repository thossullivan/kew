"""Item evidence chain — service composition and the serve route (FUI-12).

The payload behind the DWork evidence ledger and the DEpic Evidence column: the
four guarantees the item's route promises, in order, each either attested on the
hash chain or honestly not. The fixture in :func:`_seed` is the artboard's own
item — sandboxed, canonical publication, a clean scan of ``3670e43f``, and
round 1 still reviewing — so it reproduces the reference picture's "3 / 4
attested" with the verdict pending.

The load-bearing rule is negative: **no green check over a chain that does not
verify**. Two tests attack it from the two directions a false check could come
from — a tampered chain, and a fact recorded by something other than the host.
The chain verdict is ``audit_status``'s, TTL cache included, so one test drives
that cache deliberately (with the clock under its own control) rather than
leaving the window the cache opens to the autouse reset fixture below.

The other negative rule is **superseded is not evidence**: ``kew epic retry``
starts a fresh review cycle, and the ledger's Verdict row has to reset with the
swimlane's round count instead of re-attesting the prior cycle's answer.

Guard coverage mirrors ``test_serve_api_pipeline``: the router carries
``read_guard``, so the read is session-gated under ``--expose`` and for every
non-loopback peer, and loopback-open otherwise.
"""

from __future__ import annotations

from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import kew.service.audit as audit_svc
from kew.audit import Actor, emit
from kew.config import DispatchConfig, ProjectConfig
from kew.publication_scan import OUTPUT_SCAN_EVENT
from kew.review_audit import REVIEW_VERDICT_EVENT
from kew.run_store import ADVERSARIAL_REVIEW_RUN_KIND, RunRecord, SqliteRunStore
from kew.serve.app import create_app
from kew.serve.auth import SessionStore
from kew.service.evidence_chain import (
    GUARANTEES,
    NO_RUN_REASON,
    PUBLICATION_POSTURE_EVENT,
    SANDBOX_EVENT,
    TAMPERED_REASON,
    TOTAL,
    UNATTRIBUTED_REASON,
    UNREADABLE_AUDIT,
    UNREADABLE_CHAIN,
    UNREADABLE_CHAIN_REASON,
    UNREADABLE_CYCLE,
    UNREADABLE_FACT_REASON,
    item_evidence,
)
from kew.service.pipeline import item_pipeline

ITEM = "2059"
PATH = f"/api/work/{ITEM}/evidence"
IMPL_RUN = "f5dcb6a5"
HEAD_SHA = "3670e43f9a1b2c3d4e5f60718293a4b5c6d7e8f9"
PR_URL = "https://github.com/kew-dev/kew/pull/2059"

#: The operating human the host records a posture as (``resolve_actor``).
OPERATOR = Actor("human", "operator")
#: An agent's self-report — never the host's own attestation.
AGENT = Actor("agent", "claude")

#: The four labels, in the order both artboards number them.
LABELS = ["Sandboxed", "Canonical publication", "Scan clean", "Verdict"]
EVENTS = [
    SANDBOX_EVENT,
    PUBLICATION_POSTURE_EVENT,
    OUTPUT_SCAN_EVENT,
    REVIEW_VERDICT_EVENT,
]


@pytest.fixture(autouse=True)
def _reset_status_cache() -> Any:
    """Isolate the chain-verify cache between tests (order-independence)."""
    audit_svc._status_cache.clear()
    yield
    audit_svc._status_cache.clear()


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


def _sandbox(
    store: SqliteRunStore,
    *,
    posture: str = "sandboxed",
    actor: Actor = OPERATOR,
    item_id: str | None = ITEM,
) -> None:
    """The resolved containment posture, as ``emit_resolved_posture`` writes it.

    The posture row's actor is the operating human (``resolve_actor``), not a
    policy actor — the host process wrote it either way, which is exactly the
    distinction ``attested_by_host`` has to get right.
    """
    emit(
        store,
        SANDBOX_EVENT,
        actor=actor,
        run_id=IMPL_RUN,
        item_id=item_id,
        payload={
            "attempt_number": 1,
            "posture": posture,
            "runtime": "docker",
            "runner": "claude",
            "image": "kew-sandbox:latest",
            "reason": None,
            "egress_verification": "sidecar-certified",
        },
    )


def _publication(
    store: SqliteRunStore, *, posture: str = "canonical-publication"
) -> None:
    emit(
        store,
        PUBLICATION_POSTURE_EVENT,
        actor=Actor("policy", "host-publication"),
        run_id=IMPL_RUN,
        item_id=ITEM,
        payload={"branch": "agent/issue-2059", "posture": posture},
    )


def _scan(store: SqliteRunStore, *, verdict: str = "clean") -> None:
    emit(
        store,
        OUTPUT_SCAN_EVENT,
        actor=Actor("policy", "host-publication"),
        run_id=IMPL_RUN,
        item_id=ITEM,
        payload={
            "verdict": verdict,
            "head_sha": HEAD_SHA,
            "already_published": False,
            "files_scanned": 7,
            "matches": {},
            "findings": [],
        },
    )


def _verdict(store: SqliteRunStore, *, verdict: str = "READY") -> None:
    emit(
        store,
        REVIEW_VERDICT_EVENT,
        actor=Actor("policy", "epic-driver"),
        run_id="rev-1",
        item_id=ITEM,
        payload={"verdict": verdict, "head_sha": HEAD_SHA, "round": 1},
    )


def _seed(db: Path) -> None:
    """The artboard's item: sandboxed, canonical, scanned clean, reviewing."""
    store = SqliteRunStore(str(db))
    try:
        store.save(_impl_run())
        store.save(_review_run())
        _sandbox(store)
        _publication(store)
        _scan(store)
    finally:
        store.close()


def _config() -> DispatchConfig:
    return DispatchConfig(project=ProjectConfig(repo="kew-dev/kew"))


@pytest.fixture
def seeded(tmp_path: Path) -> Path:
    db = tmp_path / "dispatch.db"
    _seed(db)
    return db


def _evidence(db: Path, item_id: str = ITEM, **kwargs: Any) -> Any:
    store = SqliteRunStore(str(db))
    try:
        return item_evidence(store, item_id, config=_config(), **kwargs)
    finally:
        store.close()


def _states(dto: Any) -> list[str]:
    return [row.state for row in dto.rows]


# --------------------------------------------------------------------------- #
# The checklist itself.
# --------------------------------------------------------------------------- #


def test_the_fixture_item_is_three_of_four_attested_with_the_verdict_pending(
    seeded: Path,
) -> None:
    dto = _evidence(seeded)
    assert [row.n for row in dto.rows] == [1, 2, 3, 4]
    assert [row.label for row in dto.rows] == LABELS
    assert [row.event for row in dto.rows] == EVENTS
    assert _states(dto) == ["attested", "attested", "attested", "pending"]
    assert (dto.attested_count, dto.total) == (3, 4)
    assert dto.chain_verified is True
    assert dto.reason == ""
    assert dto.unavailable == []


def test_each_attested_row_carries_the_words_and_the_time_of_its_fact(
    seeded: Path,
) -> None:
    rows = {row.label: row for row in _evidence(seeded).rows}
    assert rows["Sandboxed"].summary == "sandboxed"
    # The artboard's word: the recorded posture is "canonical-publication".
    assert rows["Canonical publication"].summary == "canonical"
    assert rows["Scan clean"].summary == "clean · 3670e43f"
    for label in ("Sandboxed", "Canonical publication", "Scan clean"):
        # The ISO timestamp of the row itself; the surface formats it.
        assert rows[label].ts.startswith("20"), label
    # Nothing recorded yet, so no time to show — never a borrowed one.
    assert rows["Verdict"].ts == ""


def test_a_pending_row_is_worded_from_the_stage_it_waits_on(seeded: Path) -> None:
    """The ledger and the swimlane must tell one story: the pending verdict row
    reads in the review stage's own words ("round 1 · reviewing")."""
    rows = {row.label: row for row in _evidence(seeded).rows}
    assert rows["Verdict"].summary == "round 1 · reviewing"


def test_a_recorded_verdict_completes_the_checklist(tmp_path: Path) -> None:
    db = tmp_path / "dispatch.db"
    _seed(db)
    store = SqliteRunStore(str(db))
    try:
        store.record_review_round(
            "epic:factory-ui", ITEM, head_sha=HEAD_SHA, verdict="READY"
        )
        _verdict(store)
    finally:
        store.close()
    dto = _evidence(db)
    assert _states(dto) == ["attested"] * 4
    assert (dto.attested_count, dto.total) == (4, 4)
    assert dto.rows[3].summary == "ready"


def test_a_refused_posture_is_attested_because_that_is_what_the_host_recorded(
    tmp_path: Path,
) -> None:
    """``state`` is an attestation vocabulary, not a verdict on the content: the
    row says the host DID record a posture, and the summary says which one.
    Reading "refused" as unattested would hide the fact it is evidence of."""
    db = tmp_path / "dispatch.db"
    store = SqliteRunStore(str(db))
    try:
        store.save(_impl_run(status="failure", pr_url=None, pr_state=None))
        _sandbox(store, posture="refused")
    finally:
        store.close()
    rows = {row.label: row for row in _evidence(db).rows}
    assert (rows["Sandboxed"].state, rows["Sandboxed"].summary) == (
        "attested",
        "refused",
    )


def test_an_uncontained_publication_is_attested_and_says_so(tmp_path: Path) -> None:
    db = tmp_path / "dispatch.db"
    store = SqliteRunStore(str(db))
    try:
        store.save(_impl_run())
        _publication(store, posture="uncontained-publication")
    finally:
        store.close()
    rows = {row.label: row for row in _evidence(db).rows}
    assert rows["Canonical publication"].state == "attested"
    assert rows["Canonical publication"].summary == "uncontained"


def test_a_passed_stage_with_no_recorded_fact_is_missing_not_pending(
    tmp_path: Path,
) -> None:
    """The item published without a scan: the stage is over, so the promised
    evidence is MISSING. Calling that "pending" would imply it is still coming."""
    db = tmp_path / "dispatch.db"
    store = SqliteRunStore(str(db))
    try:
        store.save(_impl_run())  # published, nothing scanned it
    finally:
        store.close()
    rows = {row.label: row for row in _evidence(db).rows}
    assert rows["Sandboxed"].state == "missing"
    assert rows["Canonical publication"].state == "missing"
    assert rows["Scan clean"].state == "missing"
    # Review has not started, so its verdict is genuinely still ahead of us.
    assert rows["Verdict"].state == "pending"


def test_a_not_ready_round_with_no_recorded_verdict_is_missing(
    tmp_path: Path,
) -> None:
    """A NEEDS_CHANGES round is over, though the pipeline draws it amber
    rather than done: a verdict nobody recorded for it is missing."""
    db = tmp_path / "dispatch.db"
    store = SqliteRunStore(str(db))
    try:
        store.save(_impl_run())
        _scan(store)
        store.record_review_round(
            "epic:factory-ui", ITEM, head_sha=HEAD_SHA, verdict="NEEDS_CHANGES"
        )
    finally:
        store.close()
    rows = {row.label: row for row in _evidence(db).rows}
    assert rows["Verdict"].state == "missing"
    assert rows["Verdict"].summary == "round 1 · needs changes"


def test_an_unpublished_item_does_not_accuse_the_host_of_losing_evidence(
    tmp_path: Path,
) -> None:
    """A run still in flight has published nothing, so the publication rows are
    ``pending`` — the four-state vocabulary has no "does not apply", and
    ``missing`` would be an accusation about a promise never made."""
    db = tmp_path / "dispatch.db"
    store = SqliteRunStore(str(db))
    try:
        store.save(_impl_run(status="pending", pr_url=None, pr_state=None))
        _sandbox(store)
    finally:
        store.close()
    rows = {row.label: row for row in _evidence(db).rows}
    assert rows["Sandboxed"].state == "attested"
    assert rows["Canonical publication"].state == "pending"
    assert rows["Scan clean"].state == "pending"
    assert rows["Verdict"].state == "pending"


def test_an_absent_run_is_four_pending_rows_and_a_reason(seeded: Path) -> None:
    """Not a 404: the guarantees a route promises exist before the work does,
    and the Epics rail selects nodes nothing has been dispatched for."""
    dto = _evidence(seeded, "9999")
    assert dto.item_id == "9999"
    assert _states(dto) == ["pending"] * 4
    assert [row.label for row in dto.rows] == LABELS
    assert [row.summary for row in dto.rows] == [""] * 4
    assert (dto.attested_count, dto.total) == (0, TOTAL)
    assert dto.reason == NO_RUN_REASON
    assert dto.chain_verified is True


def test_the_credential_issuer_lifecycle_rows_do_not_answer_for_the_posture(
    tmp_path: Path,
) -> None:
    """Regression: ``sandbox_resolved`` also carries the issuer's key lifecycle
    rows. The newest row of that event is often a revocation; the guarantee's
    row is the newest one that actually records a posture."""
    db = tmp_path / "dispatch.db"
    store = SqliteRunStore(str(db))
    try:
        store.save(_impl_run())
        _sandbox(store)
        emit(
            store,
            SANDBOX_EVENT,
            actor=Actor("policy", "credential-issuer"),
            run_id=IMPL_RUN,
            item_id=ITEM,
            payload={"phase": "revocation", "credential_issuer": None},
        )
    finally:
        store.close()
    rows = {row.label: row for row in _evidence(db).rows}
    assert (rows["Sandboxed"].state, rows["Sandboxed"].summary) == (
        "attested",
        "sandboxed",
    )


def test_a_legacy_row_keyed_only_on_its_run_is_still_found(tmp_path: Path) -> None:
    """A pre-item_id row carries its key on ``run_id`` alone. Missing it would
    report a recorded guarantee as ``missing`` — an accusation from a join that
    was too narrow."""
    db = tmp_path / "dispatch.db"
    store = SqliteRunStore(str(db))
    try:
        store.save(_impl_run())
        store.save(_review_run())  # the newest run, and not the one that ran
        _sandbox(store, item_id=None)
    finally:
        store.close()
    rows = {row.label: row for row in _evidence(db).rows}
    assert rows["Sandboxed"].state == "attested"


# --------------------------------------------------------------------------- #
# No green check over a chain that does not verify.
# --------------------------------------------------------------------------- #


def test_a_tampered_chain_turns_every_row_unverified(tmp_path: Path) -> None:
    db = tmp_path / "dispatch.db"
    _seed(db)
    store = SqliteRunStore(str(db))
    try:
        store._conn.execute("UPDATE audit_events SET actor_id='mallory' WHERE id=1")
        store._conn.commit()
    finally:
        store.close()
    dto = _evidence(db)
    assert _states(dto) == ["unverified", "unverified", "unverified", "pending"]
    assert dto.attested_count == 0
    assert dto.chain_verified is False
    assert dto.reason == TAMPERED_REASON
    # An unrecorded fact is still unrecorded: a broken chain does not invent one.
    assert dto.rows[3].state == "pending"


def test_a_lagging_anchor_is_enough_to_withhold_every_check(tmp_path: Path) -> None:
    """The verdict is ``/api/audit/status``'s, anchors included, so an anchor
    that does not classify withholds the checks exactly as the CLI would."""
    db = tmp_path / "dispatch.db"
    _seed(db)
    (tmp_path / "audit-anchors.jsonl").write_text(
        '{"id": 9999, "entry_hash": "' + "f" * 64 + '", "count": 9999,'
        ' "timestamp": "2026-09-24T00:00:00+00:00"}\n',
        encoding="utf-8",
    )
    dto = _evidence(db, repo_dir=tmp_path)
    assert dto.chain_verified is False
    assert "attested" not in _states(dto)


def test_a_fact_an_agent_recorded_is_never_attested(tmp_path: Path) -> None:
    """The other direction a false check could come from: the chain verifies,
    but the row is a self-report rather than the host's own attestation."""
    db = tmp_path / "dispatch.db"
    store = SqliteRunStore(str(db))
    try:
        store.save(_impl_run())
        _sandbox(store, actor=AGENT)
        _publication(store)
        _scan(store)
    finally:
        store.close()
    dto = _evidence(db)
    rows = {row.label: row for row in dto.rows}
    assert rows["Sandboxed"].state == "unverified"
    # Its words still show: an unverified row is readable, just not vouched for.
    assert rows["Sandboxed"].summary == "sandboxed"
    assert rows["Canonical publication"].state == "attested"
    assert dto.chain_verified is True
    assert dto.reason == UNATTRIBUTED_REASON


def test_an_evidence_row_that_disclaims_host_attestation_is_believed(
    tmp_path: Path,
) -> None:
    """A structured evidence row marks a runner-produced fact
    ``attested_by_host: False`` inside its payload. That answer wins over the
    actor on the envelope."""
    db = tmp_path / "dispatch.db"
    store = SqliteRunStore(str(db))
    try:
        store.save(_impl_run())
        emit(
            store,
            OUTPUT_SCAN_EVENT,
            actor=Actor("policy", "host-publication"),
            run_id=IMPL_RUN,
            item_id=ITEM,
            payload={
                "attested_by_host": False,
                "verdict": "clean",
                "head_sha": HEAD_SHA,
            },
        )
    finally:
        store.close()
    rows = {row.label: row for row in _evidence(db).rows}
    assert rows["Scan clean"].state == "unverified"


def test_a_row_that_could_not_be_read_is_not_blamed_on_its_producer(
    tmp_path: Path,
) -> None:
    """``unverified`` has two causes and they are not interchangeable. Here the
    only ``sandbox_resolved`` row records no posture, so nobody could read the
    guarantee — saying "not the host's own attestation" would point the reader
    at a producer when the truth is that the fact is unreadable."""
    db = tmp_path / "dispatch.db"
    store = SqliteRunStore(str(db))
    try:
        store.save(_impl_run())
        emit(
            store,
            SANDBOX_EVENT,
            actor=Actor("policy", "credential-issuer"),
            run_id=IMPL_RUN,
            item_id=ITEM,
            payload={"phase": "revocation", "credential_issuer": None},
        )
    finally:
        store.close()
    dto = _evidence(db)
    rows = {row.label: row for row in dto.rows}
    assert rows["Sandboxed"].state == "unverified"
    assert rows["Sandboxed"].summary == ""
    assert dto.reason == UNREADABLE_FACT_REASON
    assert UNREADABLE_AUDIT in dto.unavailable


def test_a_warm_status_cache_bounds_how_long_a_tampered_row_can_read_attested(
    seeded: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The chain verdict is ``/api/audit/status``'s, and its TTL cache comes
    with it: keyed on (db path, row count, tail ``entry_hash``), which an
    in-place edit of a NON-tail row leaves unchanged. So ``attested`` means "on
    a chain that verified at most ``_STATUS_CACHE_TTL_S`` ago", and the window
    is asserted here — both halves — rather than hidden by the autouse reset:
    a warm verdict masks the tamper, and the TTL is what ends the masking.
    """
    clock = [1_000.0]
    monkeypatch.setattr(audit_svc, "_now", lambda: clock[0])
    assert _states(_evidence(seeded))[0] == "attested"  # warms the verdict

    store = SqliteRunStore(str(seeded))
    try:
        store._conn.execute("UPDATE audit_events SET actor_id='mallory' WHERE id=1")
        store._conn.commit()
    finally:
        store.close()

    within = _evidence(seeded)
    assert within.chain_verified is True
    assert "attested" in _states(within)

    clock[0] += audit_svc._STATUS_CACHE_TTL_S
    after = _evidence(seeded)
    assert after.chain_verified is False
    assert after.reason == TAMPERED_REASON
    assert "attested" not in _states(after)


# --------------------------------------------------------------------------- #
# Superseded is not evidence: the verdict row belongs to the current cycle.
# --------------------------------------------------------------------------- #


def _verdict_recorded(db: Path) -> None:
    """Round 1 of the current cycle, and the attested verdict behind it."""
    store = SqliteRunStore(str(db))
    try:
        store.record_review_round(
            "epic:factory-ui", ITEM, head_sha=HEAD_SHA, verdict="READY"
        )
        _verdict(store)
    finally:
        store.close()


def _retry(db: Path) -> None:
    """``kew epic retry``'s marker: a fresh review allowance for the node."""
    store = SqliteRunStore(str(db))
    try:
        store.mark_epic_retry_item("epic:factory-ui", ITEM)
    finally:
        store.close()


def _reviewed(tmp_path: Path) -> Path:
    """The artboard's item with review round 1 finished READY, nothing live."""
    db = tmp_path / "dispatch.db"
    store = SqliteRunStore(str(db))
    try:
        store.save(_impl_run())
        _sandbox(store)
        _publication(store)
        _scan(store)
    finally:
        store.close()
    _verdict_recorded(db)
    return db


def _review_stage(db: Path) -> Any:
    """The swimlane's review cell for the same item (FUI-11) — the projection
    the ledger must not contradict."""
    store = SqliteRunStore(str(db))
    try:
        pipeline = item_pipeline(store, ITEM, config=_config())
    finally:
        store.close()
    assert pipeline is not None
    return next(
        stage
        for lane in pipeline.lanes
        for stage in lane.stages
        if stage.stage == "review"
    )


def test_a_retry_supersedes_the_previous_cycles_verdict(tmp_path: Path) -> None:
    """Regression (#2075): the verdict row joined the newest recorded verdict
    for the item with no cycle scope, so after ``kew epic retry`` the ledger
    read "4 / 4 attested · ready" beside a swimlane that said review had not
    started. A superseded verdict is history, not this route's evidence."""
    db = _reviewed(tmp_path)
    assert _states(_evidence(db)) == ["attested"] * 4

    _retry(db)

    dto = _evidence(db)
    rows = {row.label: row for row in dto.rows}
    assert rows["Verdict"].state == "pending"
    # Worded from the stage, so the two surfaces tell one story.
    assert _review_stage(db).state == "pending"
    assert rows["Verdict"].summary == "not started"
    assert rows["Verdict"].ts == ""
    assert (dto.attested_count, dto.total) == (3, 4)
    # Nothing is wrong with the chain, and nobody is accused of anything.
    assert dto.chain_verified is True
    assert dto.reason == ""
    assert dto.unavailable == []


def test_the_fresh_cycles_own_verdict_is_attested_again(tmp_path: Path) -> None:
    """The scope narrows to the current cycle; it does not hide it. Round 1 of
    the cycle the retry opened completes the checklist exactly as before."""
    db = _reviewed(tmp_path)
    _retry(db)
    _verdict_recorded(db)

    dto = _evidence(db)
    assert _states(dto) == ["attested"] * 4
    assert dto.rows[3].summary == "ready"
    assert _review_stage(db).label == "round 1"


def test_an_unreadable_review_cycle_never_vouches_for_a_recorded_verdict(
    tmp_path: Path,
) -> None:
    """Which cycle a recorded verdict belongs to could not be told, so it can
    be neither vouched for nor called absent — and the reason must not blame
    its producer for what is really an unreadable fact."""
    db = _reviewed(tmp_path)

    def _boom(item_id: str) -> str | None:
        raise RuntimeError("epic_retries is gone")

    store = SqliteRunStore(str(db))
    try:
        store.review_cycle_start = _boom  # type: ignore[method-assign]
        dto = item_evidence(store, ITEM, config=_config())
    finally:
        store.close()
    rows = {row.label: row for row in dto.rows}
    assert rows["Verdict"].state == "unverified"
    assert rows["Verdict"].summary == ""
    assert rows["Verdict"].ts == ""
    assert dto.attested_count == 3
    assert dto.chain_verified is True
    assert UNREADABLE_CYCLE in dto.unavailable
    assert dto.reason == UNREADABLE_FACT_REASON


# --------------------------------------------------------------------------- #
# Degradation: a store that cannot answer must say so, not answer cleanly.
# --------------------------------------------------------------------------- #


class _DuckStore:
    """A store with the work reads and nothing else — no chain, no audit."""

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


def test_a_store_that_cannot_be_verified_never_reports_an_attested_row() -> None:
    dto = item_evidence(_DuckStore([_impl_run()]), ITEM, config=_config())
    assert "attested" not in _states(dto)
    assert dto.chain_verified is False
    assert dto.reason == UNREADABLE_CHAIN_REASON
    assert UNREADABLE_CHAIN in dto.unavailable


def test_an_unreadable_audit_table_is_unverified_not_a_clean_absence() -> None:
    """ "We could not look" is not "we looked and nothing is there": a row whose
    event table did not answer must not read as ``pending`` or ``missing``."""
    store = _DuckStore([_impl_run()])

    def _boom(**kwargs: Any) -> list[Any]:
        raise RuntimeError("audit_events is gone")

    store.list_audit_events = _boom  # type: ignore[method-assign]
    dto = item_evidence(store, ITEM, config=_config())
    assert _states(dto) == ["unverified"] * 4
    assert UNREADABLE_AUDIT in dto.unavailable


class _UnreadableRow:
    """A recorded row whose payload did not parse: ``list_audit_events``
    coerces it to ``None`` rather than dropping the row."""

    payload = None
    timestamp = "2026-09-24T15:24:15+00:00"
    actor_type = "policy"


def test_a_row_nobody_can_parse_is_unverified_never_attested() -> None:
    store = _DuckStore([_impl_run()])

    def _rows(**kwargs: Any) -> list[Any]:
        return [_UnreadableRow()] if kwargs.get("event") == SANDBOX_EVENT else []

    store.list_audit_events = _rows  # type: ignore[method-assign]
    dto = item_evidence(store, ITEM, config=_config())
    rows = {row.label: row for row in dto.rows}
    assert rows["Sandboxed"].state == "unverified"
    assert rows["Sandboxed"].summary == ""
    assert UNREADABLE_AUDIT in dto.unavailable


def test_an_unreadable_stage_does_not_become_a_missing_promise() -> None:
    """A stage nobody could read has not been shown to have passed, so its
    unrecorded fact stays ``pending`` — and the pipeline's own reason is
    carried through rather than dropped."""
    store = _DuckStore([_impl_run()])

    def _boom(item_id: str) -> list[Any]:
        raise RuntimeError("review_rounds is gone")

    store.review_rounds_in_cycle = _boom  # type: ignore[attr-defined]
    dto = item_evidence(store, ITEM, config=_config())
    rows = {row.label: row for row in dto.rows}
    assert rows["Verdict"].state == "pending"
    assert "pipeline:review_rounds" in dto.unavailable


# --------------------------------------------------------------------------- #
# Composition properties.
# --------------------------------------------------------------------------- #


def test_the_guarantee_table_is_the_only_source_of_the_row_order() -> None:
    assert [g.label for g in GUARANTEES] == LABELS
    assert [g.event for g in GUARANTEES] == EVENTS
    assert TOTAL == 4


def test_composed_at_is_recorded_in_utc_iso(seeded: Path) -> None:
    moment = datetime(2026, 9, 24, 18, 12, 4, tzinfo=UTC)
    assert _evidence(seeded, now=moment).composed_at == moment.isoformat()


def test_recomposing_the_same_facts_gives_the_same_answer(seeded: Path) -> None:
    moment = datetime(2026, 9, 24, 18, 12, 4, tzinfo=UTC)
    first = _evidence(seeded, now=moment)
    second = _evidence(seeded, now=moment)
    assert first.model_dump() == second.model_dump()


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


def test_route_returns_the_checklist(
    seeded: Path, tmp_path: Path, inbox_client: Any
) -> None:
    client = inbox_client(_mk_app(seeded, tmp_path), loopback_peer=True)
    response = client.get(PATH)
    assert response.status_code == 200
    body = response.json()
    assert set(body) >= {
        "item_id",
        "rows",
        "attested_count",
        "total",
        "chain_verified",
        "reason",
        "unavailable",
        "composed_at",
    }
    assert [row["label"] for row in body["rows"]] == LABELS
    assert [row["state"] for row in body["rows"]] == [
        "attested",
        "attested",
        "attested",
        "pending",
    ]
    assert (body["attested_count"], body["total"]) == (3, 4)
    assert body["chain_verified"] is True


def test_route_answers_for_an_item_with_no_runs(
    seeded: Path, tmp_path: Path, inbox_client: Any
) -> None:
    client = inbox_client(_mk_app(seeded, tmp_path), loopback_peer=True)
    response = client.get("/api/work/9999/evidence")
    assert response.status_code == 200
    body = response.json()
    assert [row["state"] for row in body["rows"]] == ["pending"] * 4
    assert body["reason"] == NO_RUN_REASON


def test_route_carries_an_opaque_id_through_the_path(
    seeded: Path, tmp_path: Path, inbox_client: Any
) -> None:
    """A Linear key is never parsed: it reaches the store as its own key."""
    client = inbox_client(_mk_app(seeded, tmp_path), loopback_peer=True)
    body = client.get("/api/work/KEW-31/evidence").json()
    assert body["item_id"] == "KEW-31"


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
