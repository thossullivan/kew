"""Tests for kew.service.verbs — intent-first operator verbs (#682 Group 3).

Every verb emits its chained audit event BEFORE the effector runs, so an
effector that fails after the emit leaves the event standing (attest, then
act). Real SqliteRunStore; gh is mocked at the module boundary.
"""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from support.checkout_generations import (
    fixture_checkout_generation,
    fixture_claim_evidence,
)

from kew.audit_chain import verify_chain
from kew.config import DispatchConfig
from kew.run_store import AdmissionIdentity, RunRecord, SqliteRunStore
from kew.service import verbs
from kew.service.work import (
    RemoteObservation,
    TrackerItemFact,
    get_work_view,
)


@pytest.fixture
def store(tmp_path: Path) -> Any:
    s = SqliteRunStore(str(tmp_path / "dispatch.db"))
    yield s
    s.close()


def _config(repo: str = "o/r") -> DispatchConfig:
    config = DispatchConfig()
    config.project.repo = repo
    return config


def test_finalize_publication_run_is_chain_attested_and_settles_handle(
    store: Any, tmp_path: Path
) -> None:
    config = _config()
    now = datetime.now(UTC)
    store.save(
        RunRecord(
            run_id="run-publication",
            issue_num=42,
            item_id="42",
            branch="agent/issue-42",
            status="awaiting_publication",
            exit_code=0,
            started_at=now,
            finished_at=now,
            error_category="publication_unavailable",
        )
    )
    store.record_continuation(
        "epic:x",
        "42",
        "run-publication",
        "agent/issue-42",
        tmp_path / "preserved",
        checkout_generation=fixture_checkout_generation(tmp_path / "preserved"),
    )

    finalized = verbs.finalize_publication_run(
        store, run_id="run-publication", config=config
    )

    assert finalized.status == "success"
    event = store.list_audit_events(event="publication_finalized")
    assert len(event) == 1
    assert event[0].run_id == "run-publication"
    assert event[0].payload == {
        "previous_status": "awaiting_publication",
        "job_id": None,
        "source": "cli",
        # This caller supplies no origin reader, so nothing is bound and the
        # finalize event says why rather than reading as "no head".
        "attested_head": None,
        "head_unattested_reason": (
            "this surface reads no remote head; finalize from `kew finalize` "
            "to name the published head of this run"
        ),
    }
    from kew.audit_chain import verify_chain

    assert verify_chain(store.read_chain_rows()).ok
    assert store.list_continuations("epic:x")[0].state == "terminal"


def _awaiting_run(store: Any, *, run_id: str = "run-publication") -> None:
    from kew.readiness import (
        Containment,
        RealizedLaunchBoundary,
        compose_realized_profile,
        compose_route_profile,
    )

    now = datetime.now(UTC)
    store.save(
        RunRecord(
            run_id=run_id,
            issue_num=42,
            item_id="42",
            branch="agent/issue-42",
            status="awaiting_publication",
            exit_code=0,
            started_at=now,
            finished_at=now,
            error_category="publication_unavailable",
        )
    )
    store.record_route_profile(
        compose_realized_profile(
            run_id=run_id,
            attempt_number=1,
            route=compose_route_profile(
                runner="claude-code", requested_model="claude-opus-5"
            ),
            boundary=RealizedLaunchBoundary(
                containment=Containment.NATIVE, auth_path="cli-login"
            ),
        )
    )


def test_finalize_names_the_verified_remote_head_as_the_run_output(
    store: Any,
) -> None:
    """#2124: the operator published by hand, so finalize records the head.

    Without this the branch carries no claim at all and every governed
    remediation leg afterwards refuses it as unbound, permanently.
    """
    from kew.publication_credentials import publication_producer_for_head

    _awaiting_run(store)
    head = "a" * 40

    verbs.finalize_publication_run(
        store,
        run_id="run-publication",
        config=_config(),
        remote_head=lambda branch: head,
    )

    attested = store.list_audit_events(event="publication_head_attested")
    assert [(e.run_id, e.item_id, e.actor_type) for e in attested] == [
        ("run-publication", "42", "human")
    ]
    assert attested[0].payload["commit_sha"] == head
    assert attested[0].payload["branch"] == "agent/issue-42"
    assert (
        publication_producer_for_head(store, branch="agent/issue-42", head_sha=head)
        == "run-publication"
    )
    from kew.audit_chain import verify_chain

    assert verify_chain(store.read_chain_rows()).ok


def test_finalize_records_why_no_head_was_named_when_the_origin_is_unreadable(
    store: Any,
) -> None:
    """An unreachable origin never reads as "this run published nothing"."""

    def unavailable(branch: str) -> str:
        raise RuntimeError("operator origin query failed")

    _awaiting_run(store)

    finalized = verbs.finalize_publication_run(
        store,
        run_id="run-publication",
        config=_config(),
        remote_head=unavailable,
    )

    assert finalized.status == "success"
    payload = store.list_audit_events(event="publication_finalized")[0].payload
    assert payload["attested_head"] is None
    assert "operator origin query failed" in payload["head_unattested_reason"]
    assert not store.list_audit_events(event="publication_head_attested")


def test_finalize_leaves_a_head_claim_another_run_already_made_standing(
    store: Any,
) -> None:
    """A second stranded run on one branch must not break the first's binding.

    Two claims on one head refuse as ambiguous wherever the binding is read,
    and a chained claim cannot be withdrawn, so a blind append here would turn
    a working head binding into a permanent refusal (#2124).
    """
    from kew.publication_credentials import publication_producer_for_head

    head = "a" * 40
    _awaiting_run(store, run_id="run-one")
    _awaiting_run(store, run_id="run-two")

    verbs.finalize_publication_run(
        store, run_id="run-one", config=_config(), remote_head=lambda branch: head
    )
    finalized = verbs.finalize_publication_run(
        store, run_id="run-two", config=_config(), remote_head=lambda branch: head
    )

    # The repair still runs; only the claim is withheld, with its reason.
    assert finalized.status == "success"
    payload = [
        event.payload
        for event in store.list_audit_events(event="publication_finalized")
        if event.run_id == "run-two"
    ][0]
    assert payload["attested_head"] is None
    assert "already claimed by run-one" in payload["head_unattested_reason"]
    assert [
        event.run_id
        for event in store.list_audit_events(event="publication_head_attested")
    ] == ["run-one"]
    assert (
        publication_producer_for_head(store, branch="agent/issue-42", head_sha=head)
        == "run-one"
    )


def test_two_finalizers_racing_for_one_head_record_only_one_claim(
    store: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two runs stranded on one branch, finalized at once, claim it once.

    The guard reads the recorded claims and the writer appends in a separate
    transaction, so both finalizers pass the guard — this pins the window
    itself, with both of them held past their check before either appends.
    Only the append settles who owns the head: two claims would refuse the
    branch as ``publication_lineage_ambiguous`` permanently, which is the
    deadlock this whole repair exists to clear (#2124).
    """
    from kew.publication_attest import head_unclaimable_for_run
    from kew.publication_credentials import publication_producer_for_head

    head = "a" * 40
    _awaiting_run(store, run_id="run-one")
    _awaiting_run(store, run_id="run-two")
    db_path = str(tmp_path / "dispatch.db")
    past_the_guard = threading.Barrier(2, timeout=10)

    def guard_then_race(*args: Any, **kwargs: Any) -> Any:
        unclaimable = head_unclaimable_for_run(*args, **kwargs)
        past_the_guard.wait()
        return unclaimable

    monkeypatch.setattr(verbs, "head_unclaimable_for_run", guard_then_race)
    errors: list[Exception] = []
    collected = threading.Lock()

    def finalize(run_id: str) -> None:
        try:
            with SqliteRunStore(db_path) as finalizer:
                verbs.finalize_publication_run(
                    finalizer,
                    run_id=run_id,
                    config=_config(),
                    remote_head=lambda branch: head,
                )
        except Exception as exc:  # pragma: no cover - diagnostic capture
            with collected:
                errors.append(exc)

    finalizers = [
        threading.Thread(target=finalize, args=(run_id,))
        for run_id in ("run-one", "run-two")
    ]
    for thread in finalizers:
        thread.start()
    for thread in finalizers:
        thread.join(timeout=20)

    assert not any(thread.is_alive() for thread in finalizers)
    assert errors == []
    # Both repairs ran: losing the head race never strands a run.
    assert {store.get(run_id).status for run_id in ("run-one", "run-two")} == {
        "success"
    }
    claims = store.list_audit_events(event="publication_head_attested")
    assert len(claims) == 1
    winner = claims[0].run_id
    reasons = {
        event.run_id: event.payload["head_unattested_reason"]
        for event in store.list_audit_events(event="publication_finalized")
    }
    heads = {
        event.run_id: event.payload["attested_head"]
        for event in store.list_audit_events(event="publication_finalized")
    }
    loser = "run-two" if winner == "run-one" else "run-one"
    assert heads == {winner: head, loser: None}
    assert reasons[winner] is None
    assert "already claimed by " + winner in reasons[loser]
    assert (
        publication_producer_for_head(store, branch="agent/issue-42", head_sha=head)
        == winner
    )
    assert verify_chain(store.read_chain_rows()).ok


def test_finalize_records_why_an_unusable_head_was_not_named(store: Any) -> None:
    """A head that is not a sha is a reason in the event, never a failed repair."""
    _awaiting_run(store)

    finalized = verbs.finalize_publication_run(
        store,
        run_id="run-publication",
        config=_config(),
        remote_head=lambda branch: "refs/heads/agent/issue-42",
    )

    assert finalized.status == "success"
    payload = store.list_audit_events(event="publication_finalized")[0].payload
    assert payload["attested_head"] is None
    assert "40-character commit sha" in payload["head_unattested_reason"]
    assert not store.list_audit_events(event="publication_head_attested")


def test_finalize_publication_run_rejects_non_awaiting_state(store: Any) -> None:
    now = datetime.now(UTC)
    store.save(
        RunRecord(
            run_id="run-failed",
            issue_num=42,
            branch="agent/issue-42",
            status="failure",
            exit_code=1,
            started_at=now,
        )
    )

    with pytest.raises(ValueError, match="not awaiting publication"):
        verbs.finalize_publication_run(store, run_id="run-failed", config=_config())
    assert store.list_audit_events(event="publication_finalized") == []


def test_finalize_cannot_attest_readiness_for_protected_legacy_output(
    store: Any,
) -> None:
    from kew.publication_credentials import CredentialPublicationRefused

    store.save(
        RunRecord(
            run_id="protected-publication",
            issue_num=42,
            item_id="42",
            branch="agent/issue-42",
            status="awaiting_publication",
            exit_code=0,
            started_at=datetime.now(UTC),
            container_name="kew-42",
        )
    )

    with pytest.raises(CredentialPublicationRefused):
        verbs.finalize_publication_run(
            store, run_id="protected-publication", config=_config()
        )

    assert store.get("protected-publication").status == "awaiting_publication"
    assert store.list_audit_events(event="publication_finalized") == []


def test_finalize_interleaved_with_defer_cannot_park_finished_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finalization between defer's two writes must settle the late handle."""
    from kew.dispatch_core import _defer_publication, _DispatchContext
    from kew.run_store import SqliteRunStore
    from kew.tracking import DispatchResult

    db_path = str(tmp_path / "dispatch.db")
    worktree = tmp_path / "preserved"
    (worktree / ".git").mkdir(parents=True)
    # A checkout kew created carries its identity (review round 17 of #1440).
    from kew.worktree import write_checkout_generation

    write_checkout_generation(worktree)
    now = datetime.now(UTC)
    record_started = threading.Event()
    finalized = threading.Event()
    finalizer_errors: list[Exception] = []

    with SqliteRunStore(db_path) as deferrer:
        deferrer.save(
            RunRecord(
                run_id="run-race",
                issue_num=42,
                item_id="42",
                branch="agent/issue-42",
                status="success",
                exit_code=0,
                started_at=now,
                finished_at=now,
            )
        )
        original_record = deferrer.record_continuation

        def record_after_finalize(*args: object, **kwargs: object):
            record_started.set()
            assert finalized.wait(timeout=5)
            return original_record(*args, **kwargs)

        monkeypatch.setattr(deferrer, "record_continuation", record_after_finalize)

        def finalize() -> None:
            try:
                assert record_started.wait(timeout=5)
                with SqliteRunStore(db_path) as finalizer:
                    verbs.finalize_publication_run(
                        finalizer, run_id="run-race", config=_config()
                    )
            except Exception as exc:  # pragma: no cover - diagnostic capture
                finalizer_errors.append(exc)
            finally:
                finalized.set()

        ctx = MagicMock(spec=_DispatchContext)
        ctx.checkout_generation = ""
        ctx.config = _config()
        ctx.store = deferrer
        ctx.epic_ref = "epic:x"
        ctx.issue_num = 42
        ctx.item_id = "42"
        ctx.branch = "agent/issue-42"
        # Canonical minting binds the run's trusted base (review round 15 of
        # #1440); without one nothing is minted.
        ctx.publication_base_sha = "c" * 40
        result = DispatchResult(
            exit_code=0,
            issue_num=42,
            log_file=tmp_path / "run.log",
            session_id="session",
            error_category="",
            run_id="run-race",
        )

        finalizer = threading.Thread(target=finalize)
        finalizer.start()
        _defer_publication(ctx, result, worktree, RuntimeError("rate limit exceeded"))
        finalizer.join(timeout=10)

        assert not finalizer.is_alive()
        assert finalizer_errors == []
        current = deferrer.get("run-race")
        assert current is not None
        assert current.status == "success"
        continuations = deferrer.list_continuations("epic:x")
        assert len(continuations) == 1
        assert continuations[0].state == "terminal"


def test_defer_committed_before_finalize_still_settles_the_handle(
    tmp_path: Path,
) -> None:
    """The reverse interleaving of the race above: the continuation insert wins.

    Ordering A (finalize lands between defer's two writes) is covered by the
    thread test above and converges via the insert's success-run predicate.
    This pins ordering B — the continuation is already committed and active
    when finalization runs — which must converge through the OTHER guard, the
    same-transaction terminalization inside ``finalize_awaiting_publication``.
    Both orderings must end with a successful run and no active handle."""
    now = datetime.now(UTC)

    with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
        store.save(
            RunRecord(
                run_id="run-order-b",
                issue_num=42,
                item_id="42",
                branch="agent/issue-42",
                status="awaiting_publication",
                exit_code=0,
                started_at=now,
                finished_at=now,
                error_category="publication_unavailable",
            )
        )
        recorded = store.record_continuation(
            "epic:x",
            "42",
            "run-order-b",
            "agent/issue-42",
            tmp_path / "preserved",
            checkout_generation=fixture_checkout_generation(tmp_path / "preserved"),
        )
        assert recorded.state == "awaiting_continuation"

        assert store.finalize_awaiting_publication("run-order-b") is True

        current = store.get("run-order-b")
        assert current is not None
        assert current.status == "success"
        continuations = store.list_continuations("epic:x")
        assert len(continuations) == 1
        assert continuations[0].state == "terminal"


# --------------------------------------------------------------------------- #
# Task 1 — epic_retry
# --------------------------------------------------------------------------- #


def test_epic_retry_happy_path_emits_then_effects(store: Any) -> None:
    config = _config()
    store.save(
        RunRecord(
            run_id="run-a",
            issue_num=12,
            branch="agent/issue-12",
            status="failure",
            exit_code=1,
            started_at=datetime.now(UTC),
        )
    )
    store.reserve_epic_node("epic:x", 12, "run-a")
    with patch("kew.labels.run") as gh:
        gh.return_value = MagicMock(returncode=0, stdout="", stderr="")
        verbs.epic_retry(store, epic_ref="epic:x", item_id="12", config=config)

    events = store.list_audit_events(event="epic_node_retry")
    assert len(events) == 1
    assert events[0].payload["epic_ref"] == "epic:x"
    assert events[0].payload["node"] == "12"
    # effectors ran: terminal generation released, retry marker recorded, and
    # configured ready/failure labels changed in one GitHub edit.
    assert store.list_epic_reservations("epic:x") == []
    assert [r.issue_num for r in store.list_epic_retries("epic:x")] == [12]
    gh.assert_called_once()
    argv = gh.call_args.args[0]
    assert config.labels.ready in argv
    assert config.labels.agent_failed in argv


def test_epic_retry_linear_node_is_item_keyed(store: Any) -> None:
    """A non-numeric tracker node retries via the item-keyed store methods —
    no GitHub label, reservation released on the durable id."""
    config = _config()
    config.tracker.type = "linear"
    store.reserve_epic_node_item("epic:x", "SFD-12", "run-a")
    with patch("kew.labels.run") as gh:
        gh.return_value = MagicMock(returncode=0, stdout="", stderr="")
        verbs.epic_retry(store, epic_ref="epic:x", item_id="SFD-12", config=config)
        # No GitHub label round-trip for a Linear node.
        gh.assert_not_called()

    events = store.list_audit_events(event="epic_node_retry")
    assert len(events) == 1
    assert events[0].payload["node"] == "SFD-12"
    assert events[0].issue_num == 0  # 0-sentinel for a non-numeric tracker
    # Missing run evidence is indeterminate and must survive retry.
    assert [r.run_id for r in store.list_epic_reservations("epic:x")] == ["run-a"]
    assert [r.item_id for r in store.list_epic_retries("epic:x")] == ["SFD-12"]


def test_retry_via_serve_rejects_non_canonical_id(store: Any) -> None:
    """An id that is not canonical for the configured tracker is rejected
    before any effect (the route maps this to a 4xx)."""
    config = _config()  # github provider
    with pytest.raises(ValueError, match="canonical"):
        verbs.retry_via_serve(
            store, epic_ref="epic:x", item_id="SFD-12", config=config, confirm=True
        )
    assert store.list_audit_events(event="epic_node_retry") == []


def test_epic_retry_event_stands_when_effector_fails(store: Any) -> None:
    """Effector failure AFTER the emit → event persists, error propagates.

    The retry marker commits with the event (#2960), so the effector that can
    still fail after it is the GitHub label edit.
    """
    config = _config()

    failing_edit = RuntimeError("label edit blew up")
    with (
        patch("kew.labels.run", side_effect=failing_edit),
        pytest.raises(RuntimeError, match="label edit blew up"),
    ):
        verbs.epic_retry(store, epic_ref="epic:x", item_id="5", config=config)

    # The intent event and its marker were committed before the failing effector.
    assert len(store.list_audit_events(event="epic_node_retry")) == 1
    assert [row.item_id for row in store.list_epic_retries("epic:x")] == ["5"]


def test_epic_retry_does_not_release_live_reservation(store: Any) -> None:
    config = _config()
    store.save(
        RunRecord(
            run_id="run-live",
            issue_num=15,
            branch="agent/issue-15",
            status="pending",
            exit_code=0,
            started_at=datetime.now(UTC),
        )
    )
    store.reserve_epic_node("epic:x", 15, "run-live")

    with patch("kew.labels.run") as gh:
        gh.return_value = MagicMock(returncode=0, stdout="", stderr="")
        verbs.epic_retry(store, epic_ref="epic:x", item_id="15", config=config)

    assert [r.run_id for r in store.list_epic_reservations("epic:x")] == ["run-live"]


def test_epic_resume_rejects_an_unvalidated_continuation_identity(
    store: Any, tmp_path: Path
) -> None:
    worktree = tmp_path / "preserved"
    record = store.record_continuation(
        "epic:x",
        "42",
        "original-run",
        "agent/issue-42",
        worktree,
        checkout_generation=fixture_checkout_generation(worktree),
    )
    expected = replace(record, branch="agent/issue-other")

    with pytest.raises(verbs.EpicResumeDenied, match="identity changed"):
        verbs.epic_resume(
            store,
            epic_ref="epic:x",
            item_id="42",
            config=_config(),
            expect=expected,
        )

    current = store.get_continuation(
        "epic:x",
        "42",
        "original-run",
        "agent/issue-42",
        worktree,
    )
    assert current is not None
    assert current.state == "awaiting_continuation"
    assert store.list_epic_reservations("epic:x") == []


def test_epic_resume_accepts_the_validated_continuation_identity(
    store: Any, tmp_path: Path
) -> None:
    worktree = tmp_path / "preserved"
    record = store.record_continuation(
        "epic:x",
        "42",
        "original-run",
        "agent/issue-42",
        worktree,
        checkout_generation=fixture_checkout_generation(worktree),
    )

    claimed = verbs.epic_resume(
        store,
        epic_ref="epic:x",
        item_id="42",
        config=_config(),
        expect=record,
    )

    assert claimed.state == "continuing"
    assert claimed.generation == record.generation + 1


def test_epic_resume_records_the_claim_evidence_the_launch_check_reads(
    store: Any, tmp_path: Path
) -> None:
    """The launch credential check binds a continuation to its family through
    the immutable ``continuation_claimed`` event, not the mutable claim row;
    a resume that claims without recording it is refused as a requeue that
    lost its evidence (go-live drive, SFD-232 run 59a3d7e2)."""
    worktree = tmp_path / "preserved"
    record = store.record_continuation(
        "epic:x",
        "42",
        "original-run",
        "agent/issue-42",
        worktree,
        checkout_generation=fixture_checkout_generation(worktree),
    )

    claimed = verbs.epic_resume(
        store, epic_ref="epic:x", item_id="42", config=_config(), expect=record
    )

    run_id = claimed.continuation_run_id
    assert run_id
    events = [
        e
        for e in store.list_audit_events(limit=None, event="continuation_claimed")
        if e.run_id == run_id
    ]
    assert len(events) == 1
    payload = events[0].payload
    assert (payload["epic_ref"], payload["item_id"], payload["original_run_id"]) == (
        "epic:x",
        "42",
        "original-run",
    )
    assert payload["continuation_run_id"] == run_id
    assert payload["branch"] == "agent/issue-42"
    assert payload["generation"] == claimed.generation
    # What the launch check asks: the continuation's retained origin.
    assert store.continuation_origins(run_id) == ["original-run"]


def test_epic_resume_refuses_and_releases_when_the_claim_evidence_is_not_recorded(
    store: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dropped audit insertion rolls back the complete claim transaction."""

    worktree = tmp_path / "preserved"
    record = store.record_continuation(
        "epic:x",
        "42",
        "original-run",
        "agent/issue-42",
        worktree,
        checkout_generation=fixture_checkout_generation(worktree),
    )
    monkeypatch.setattr(store, "_chained_insert_in_transaction", lambda *_a, **_k: None)

    with pytest.raises(RuntimeError, match="not durably recorded"):
        verbs.epic_resume(
            store, epic_ref="epic:x", item_id="42", config=_config(), expect=record
        )

    released = store.get_continuation(
        "epic:x", "42", "original-run", "agent/issue-42", worktree
    )
    assert released is not None
    assert released.state == "awaiting_continuation"
    assert released.continuation_run_id is None
    assert released.generation == record.generation, "the claim is undone entirely"
    assert store.list_audit_events(limit=None, event="continuation_claimed") == []

    # With the audit append working again, the retry takes the same
    # generation, records it, and its ancestry is complete: what the launch
    # check reads.
    monkeypatch.undo()
    retried = verbs.epic_resume(
        store, epic_ref="epic:x", item_id="42", config=_config()
    )
    assert retried.generation == record.generation + 1
    assert retried.continuation_run_id
    assert store.continuation_origins(retried.continuation_run_id) == ["original-run"]
    claims = store.list_audit_events(limit=None, event="continuation_claimed")
    assert [e.payload["generation"] for e in claims] == [retried.generation]


def _fixed_run_id(monkeypatch: pytest.MonkeyPatch, run_id: str) -> None:
    from kew.service import verbs as verbs_module

    monkeypatch.setattr(verbs_module.uuid, "uuid4", lambda: run_id)


def test_epic_resume_generates_the_continuation_run_id_itself(store: Any) -> None:
    """No caller may name the run id: an outside id could name an existing
    run, owner, or claim, and the ancestry reader treats an id as one claim."""
    with pytest.raises(TypeError):
        verbs.epic_resume(
            store,
            epic_ref="epic:x",
            item_id="42",
            config=_config(),
            continuation_run_id="chosen",  # type: ignore[call-arg]
        )


@pytest.mark.parametrize(
    "where", ["run-row", "reservation", "mutable-owner", "claim-evidence"]
)
def test_epic_resume_refuses_a_run_id_that_anything_already_names(
    store: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, where: str
) -> None:
    """The generated id must be new in every place a run id can live."""
    worktree = tmp_path / "preserved"
    record = store.record_continuation(
        "epic:x",
        "42",
        "original-run",
        "agent/issue-42",
        worktree,
        checkout_generation=fixture_checkout_generation(worktree),
    )
    if where == "run-row":
        store.save(
            RunRecord(
                run_id="taken",
                issue_num=99,
                item_id="99",
                branch="agent/issue-99",
                status="success",
                exit_code=0,
                started_at=datetime.now(UTC),
            )
        )
    elif where == "reservation":
        store.reserve_epic_node_item("epic:y", "43", "taken")
    elif where == "mutable-owner":
        other = store.record_continuation(
            "epic:x",
            "43",
            "original-43",
            "agent/issue-43",
            tmp_path / "other",
            checkout_generation=fixture_checkout_generation(tmp_path / "other"),
        )
        assert (
            store.claim_continuation_under_cap(
                other.epic_ref,
                other.item_id,
                other.original_run_id,
                other.branch,
                other.worktree_identity,
                "taken",
                10,
                claim_evidence=fixture_claim_evidence(
                    store,
                    other.epic_ref,
                    other.item_id,
                    other.original_run_id,
                    other.branch,
                    other.worktree_identity,
                    "taken",
                ),
            )
            == "claimed"
        )
    else:
        _fixed_run_id(monkeypatch, "taken")
        first = verbs.epic_resume(
            store, epic_ref="epic:x", item_id="42", config=_config(), expect=record
        )
        assert first.continuation_run_id == "taken"
        assert store.mark_continuation_awaiting(
            "epic:x", "42", "original-run", "agent/issue-42", worktree, 1, "taken"
        )
    _fixed_run_id(monkeypatch, "taken")
    roomy = _config()
    roomy.parallel.max_workers = 4  # capacity for both owners: the identity refuses

    with pytest.raises(verbs.EpicResumeDenied, match="already in use"):
        verbs.epic_resume(store, epic_ref="epic:x", item_id="42", config=roomy)

    untouched = store.get_continuation(
        "epic:x", "42", "original-run", "agent/issue-42", worktree
    )
    assert untouched is not None
    assert (untouched.state, untouched.continuation_run_id) == (
        "awaiting_continuation",
        None,
    )
    claims = store.list_audit_events(
        limit=None, event="continuation_claimed", run_id="taken"
    )
    assert len(claims) == (1 if where in {"claim-evidence", "mutable-owner"} else 0)


def test_epic_resume_refuses_an_identity_the_evidence_cannot_reproduce(
    store: Any, tmp_path: Path
) -> None:
    """Evidence tokens are sanitized; two records whose tokens collide would
    share one evidence identity, so a lossy identity is refused up front."""
    worktree = tmp_path / "preserved"
    store.record_continuation(
        "epic:x",
        "item+a",
        "original-run",
        "agent/issue-42",
        worktree,
        checkout_generation=fixture_checkout_generation(worktree),
    )

    with pytest.raises(verbs.EpicResumeDenied, match="not representable"):
        verbs.epic_resume(store, epic_ref="epic:x", item_id="item+a", config=_config())

    untouched = store.get_continuation(
        "epic:x", "item+a", "original-run", "agent/issue-42", worktree
    )
    assert untouched is not None
    assert untouched.state == "awaiting_continuation"
    assert store.list_audit_events(limit=None, event="continuation_claimed") == []


def _tick_then_drop_claim(store, config, *_args, **_kwargs):
    from kew.graph.driver import tick

    tick._attest_continuation_states(store, config, "epic:x")


def test_a_tick_observing_the_claim_before_its_evidence_cannot_poison_the_undo(
    store: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A same-connection observer cannot attest a claim before insertion fails.

    The transaction rolls back, so the retry safely claims the same generation.
    """

    worktree = tmp_path / "preserved"
    record = store.record_continuation(
        "epic:x",
        "42",
        "original-run",
        "agent/issue-42",
        worktree,
        checkout_generation=fixture_checkout_generation(worktree),
    )
    config = _config()

    monkeypatch.setattr(
        store,
        "_chained_insert_in_transaction",
        partial(_tick_then_drop_claim, store, config),
    )
    with pytest.raises(RuntimeError, match="not durably recorded"):
        verbs.epic_resume(
            store, epic_ref="epic:x", item_id="42", config=config, expect=record
        )
    assert store.list_audit_events(limit=None, event="continuation_claimed") == [], (
        "the tick attested a claim that had no run yet"
    )
    monkeypatch.undo()

    retried = verbs.epic_resume(store, epic_ref="epic:x", item_id="42", config=config)
    assert retried.generation == record.generation + 1
    assert retried.continuation_run_id
    assert store.continuation_origins(retried.continuation_run_id) == ["original-run"]
    claims = store.list_audit_events(limit=None, event="continuation_claimed")
    assert [(e.run_id, e.payload["generation"]) for e in claims] == [
        (retried.continuation_run_id, retried.generation)
    ]


def test_the_tick_attests_a_continuing_claim_once_its_run_exists(
    store: Any, tmp_path: Path
) -> None:
    from kew.graph.driver import tick as tick_module

    record = store.record_continuation(
        "epic:x",
        "42",
        "original-run",
        "agent/issue-42",
        tmp_path / "wt",
        checkout_generation=fixture_checkout_generation(tmp_path / "wt"),
    )
    assert (
        store.claim_continuation_under_cap(
            record.epic_ref,
            record.item_id,
            record.original_run_id,
            record.branch,
            record.worktree_identity,
            "c1",
            10,
            claim_evidence=fixture_claim_evidence(
                store,
                record.epic_ref,
                record.item_id,
                record.original_run_id,
                record.branch,
                record.worktree_identity,
                "c1",
            ),
        )
        == "claimed"
    )
    config = _config()
    tick_module._attest_continuation_states(store, config, "epic:x")
    assert len(store.list_audit_events(limit=None, event="continuation_claimed")) == 1

    store.save(
        RunRecord(
            run_id="c1",
            issue_num=42,
            item_id="42",
            branch="agent/issue-42",
            status="success",
            exit_code=0,
            started_at=datetime.now(UTC),
        )
    )
    tick_module._attest_continuation_states(store, config, "epic:x")
    tick_module._attest_continuation_states(store, config, "epic:x")
    claims = store.list_audit_events(
        limit=None, event="continuation_claimed", run_id="c1"
    )
    assert [e.payload["generation"] for e in claims] == [record.generation + 1]


def _seed_original_run(store: Any, tmp_path: Path):
    """An original run with the route evidence the launch check needs."""
    from kew.readiness import (
        Containment,
        RealizedLaunchBoundary,
        compose_realized_profile,
        compose_route_profile,
    )

    store.save(
        RunRecord(
            run_id="original-run",
            issue_num=42,
            item_id="42",
            branch="agent/issue-42",
            status="failure",
            exit_code=1,
            started_at=datetime.now(UTC),
        )
    )
    store.record_route_profile(
        compose_realized_profile(
            run_id="original-run",
            attempt_number=1,
            route=compose_route_profile(
                runner="claude-code", requested_model="claude-opus-5"
            ),
            boundary=RealizedLaunchBoundary(
                containment=Containment.NATIVE, auth_path="cli-login"
            ),
        )
    )
    return store.record_continuation(
        "epic:x",
        "42",
        "original-run",
        "agent/issue-42",
        tmp_path / "preserved",
        checkout_generation=fixture_checkout_generation(tmp_path / "preserved"),
    )


def _launch_allowed(store: Any, run_id: str) -> None:
    from kew.publication_credentials import require_publication_launch_credentials

    require_publication_launch_credentials(
        store, run_id=run_id, run_kind="dispatch", attempt_number=1
    )


def test_a_tick_after_the_empty_readback_cannot_poison_the_undo(
    store: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tick after insertion rollback cannot invent evidence for the failed claim.

    The retry must pass the real launch credential check.
    """
    from kew.graph.driver import tick as tick_module

    record = _seed_original_run(store, tmp_path)
    config = _config()
    monkeypatch.setattr(store, "_chained_insert_in_transaction", lambda *_a, **_k: None)
    with pytest.raises(RuntimeError, match="not durably recorded"):
        verbs.epic_resume(
            store, epic_ref="epic:x", item_id="42", config=config, expect=record
        )
    monkeypatch.undo()
    tick_module._attest_continuation_states(store, config, "epic:x")
    assert store.list_audit_events(limit=None, event="continuation_claimed") == []

    retried = verbs.epic_resume(store, epic_ref="epic:x", item_id="42", config=config)
    assert retried.continuation_run_id
    _launch_allowed(store, retried.continuation_run_id)


def _capture_then_drop_claim(store, captured, *_args, **_kwargs):
    captured.extend(store.list_continuations("epic:x"))


def test_a_record_captured_before_the_undo_attests_nothing_after_the_retry(
    store: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tick that captured the continuing row before the undo and attests it
    only after the retry launched names the undone run id, which has no run,
    so it records nothing; the retry's launch stays allowed."""
    from kew.graph.driver import tick as tick_module

    record = _seed_original_run(store, tmp_path)
    config = _config()
    captured: list[Any] = []

    monkeypatch.setattr(
        store,
        "_chained_insert_in_transaction",
        partial(_capture_then_drop_claim, store, captured),
    )
    with pytest.raises(RuntimeError, match="not durably recorded"):
        verbs.epic_resume(
            store, epic_ref="epic:x", item_id="42", config=config, expect=record
        )
    monkeypatch.undo()
    assert captured and captured[0].state == "continuing"

    retried = verbs.epic_resume(store, epic_ref="epic:x", item_id="42", config=config)
    assert retried.continuation_run_id
    store.save(
        RunRecord(
            run_id=retried.continuation_run_id,
            issue_num=42,
            item_id="42",
            branch="agent/issue-42",
            status="success",
            exit_code=0,
            started_at=datetime.now(UTC),
        )
    )
    tick_module._attest_continuing_continuation(store, config, captured[0])
    claims = store.list_audit_events(limit=None, event="continuation_claimed")
    assert [(e.run_id, e.payload["generation"]) for e in claims] == [
        (retried.continuation_run_id, retried.generation)
    ]
    _launch_allowed(store, retried.continuation_run_id)


def _wrong_claim_generation(**kwargs):
    from kew.audit_identity import continuation_evidence

    payload = continuation_evidence(**kwargs)
    if kwargs.get("generation") is not None:
        payload["generation"] += 7
    return payload


def test_epic_resume_readback_requires_the_exact_claim_identity(
    store: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The store rejects another generation's evidence before committing a claim."""
    from kew.service import verbs as verbs_module

    worktree = tmp_path / "preserved"
    record = store.record_continuation(
        "epic:x",
        "42",
        "original-run",
        "agent/issue-42",
        worktree,
        checkout_generation=fixture_checkout_generation(worktree),
    )
    monkeypatch.setattr(verbs_module, "continuation_evidence", _wrong_claim_generation)
    _fixed_run_id(monkeypatch, "claim-1")
    with pytest.raises(ValueError, match="evidence does not match"):
        verbs.epic_resume(
            store, epic_ref="epic:x", item_id="42", config=_config(), expect=record
        )

    released = store.get_continuation(
        "epic:x", "42", "original-run", "agent/issue-42", worktree
    )
    assert released is not None
    assert released.state == "awaiting_continuation"
    assert released.continuation_run_id is None
    assert released.generation == record.generation, "the claim is undone entirely"
    stale = store.list_audit_events(
        limit=None, event="continuation_claimed", run_id="claim-1"
    )
    assert stale == [], "invalid evidence must roll back with the claim"


def test_epic_resume_denial_escapes_braces_in_epic_ref(store: Any) -> None:
    with pytest.raises(verbs.EpicResumeDenied) as exc:
        verbs.epic_resume(
            store,
            epic_ref="epic:{operator}",
            item_id="42",
            config=_config(),
        )

    assert str(exc.value) == (
        "no preserved continuation recorded for 42 in epic 'epic:{operator}'"
    )


def test_epic_retry_refuses_awaiting_continuation_with_sanitized_hint(
    store: Any,
) -> None:
    config = _config()
    store.record_continuation(
        "epic:x",
        "12",
        "original-run",
        "agent/issue-12",
        "/private/preserved/worktree",
        checkout_generation=fixture_checkout_generation("/private/preserved/worktree"),
    )

    with patch("kew.labels.run") as gh:
        gh.return_value = MagicMock(returncode=0, stdout="", stderr="")
        with pytest.raises(ValueError, match=r"kew epic continue epic:x 12") as exc:
            verbs.epic_retry(store, epic_ref="epic:x", item_id="12", config=config)

    assert "/private/preserved/worktree" not in str(exc.value)
    assert store.list_epic_retries("epic:x") == []
    assert store.list_audit_events(event="epic_node_retry") == []
    gh.assert_not_called()


def test_epic_retry_refuses_a_review_held_node_and_leaves_it_held(
    store: Any,
) -> None:
    """#2154: retry claimed success while the node stayed HELD."""
    config = _config()
    store.hold_epic_node_item("epic:x", "12", "review_unparseable")

    with patch("kew.labels.run") as gh:
        gh.return_value = MagicMock(returncode=0, stdout="", stderr="")
        with pytest.raises(verbs.EpicRetryHeld) as exc:
            verbs.epic_retry(store, epic_ref="epic:x", item_id="12", config=config)

    assert "held (review_unparseable)" in str(exc.value)
    assert "kew epic unhold epic:x 12" in str(exc.value)
    held = store.list_epic_holds("epic:x")
    assert [row.item_id for row in held] == ["12"], "the hold must survive a refusal"
    assert store.list_epic_retries("epic:x") == []
    assert store.list_audit_events(event="epic_node_retry") == []
    gh.assert_not_called()


def test_epic_retry_refusal_flattens_an_untrusted_hold_reason(store: Any) -> None:
    """An admission-refusal reason carries a detached child's stderr tail."""
    config = _config()
    store.hold_epic_node_item(
        "epic:x",
        "12",
        "admission_refused: \x07bad\nline two " + "x" * 400,
    )

    with pytest.raises(verbs.EpicRetryHeld) as exc:
        verbs.epic_retry(store, epic_ref="epic:x", item_id="12", config=config)

    message = str(exc.value)
    assert "\n" not in message and "\x07" not in message
    assert "admission_refused: bad line two" in message
    assert len(message) < 320, message


@pytest.mark.parametrize(
    "reason",
    [
        "credential_issuer_refused: issuer route unsupported",
        "preexisting_worktree_refused: dirty worktree on disk",
    ],
)
def test_epic_retry_runs_for_a_hold_the_driver_overlay_does_not_honor(
    store: Any, reason: str
) -> None:
    """A hold row is not automatically a HELD node (#2154 review).

    ``tick._hold_refused_frontier_node`` writes these two reasons, and
    ``overlay_status`` matches neither: the node keeps reading FAILED and stays
    in the dispatch frontier. Refusing retry on them would take away the very
    recovery path `docs/cli/epic.md` documents for a failed node and leave the
    operator a `kew epic unhold` they never needed.
    """
    from kew.epic_holds import holds_node_out_of_frontier

    assert not holds_node_out_of_frontier(reason)
    config = _config()
    store.hold_epic_node_item("epic:x", "12", reason)

    with patch("kew.labels.run") as gh:
        gh.return_value = MagicMock(returncode=0, stdout="", stderr="")
        verbs.epic_retry(store, epic_ref="epic:x", item_id="12", config=config)

    assert [row.item_id for row in store.list_epic_retries("epic:x")] == ["12"]
    assert store.list_audit_events(event="epic_node_retry") != []


def test_epic_retry_refuses_exactly_the_reasons_the_predicate_names(
    store: Any,
) -> None:
    """The refusal is the shared predicate, node by node — nothing wider.

    Refuse more than the overlay holds and a dispatchable node loses retry;
    refuse less and retry reports a recovery the hold outlives — the bug #2154
    fixed. ``tests/test_driver_overlay.py`` pins the other half: that the same
    predicate is exactly what ``overlay_status`` routes to HELD.
    """
    from kew.epic_holds import holds_node_out_of_frontier

    reasons = [
        "ci_pending",
        "review_unparseable",
        "review_budget: pool $40.00 spent $43.97",
        "budget_preflight: over cap",
        "sandbox_containment: no mapped image",
        "admission_refused: gh auth failed",
        "credential_issuer_refused: issuer route unsupported",
        "preexisting_worktree_refused: dirty worktree on disk",
        "something_nobody_writes",
    ]
    config = _config()
    for index, reason in enumerate(reasons):
        item_id = str(100 + index)
        store.hold_epic_node_item("epic:p", item_id, reason)
        with patch("kew.labels.run") as gh:
            gh.return_value = MagicMock(returncode=0, stdout="", stderr="")
            try:
                verbs.epic_retry(
                    store, epic_ref="epic:p", item_id=item_id, config=config
                )
                refused = False
            except verbs.EpicRetryHeld:
                refused = True
        assert refused is holds_node_out_of_frontier(reason), reason


def test_epic_retry_still_runs_for_an_unheld_node_in_a_held_epic(store: Any) -> None:
    """The refusal is keyed on the node, not on the epic."""
    config = _config()
    store.hold_epic_node_item("epic:x", "13", "review_unparseable")

    with patch("kew.labels.run") as gh:
        gh.return_value = MagicMock(returncode=0, stdout="", stderr="")
        verbs.epic_retry(store, epic_ref="epic:x", item_id="12", config=config)

    assert [row.item_id for row in store.list_epic_retries("epic:x")] == ["12"]
    gh.assert_called()


# --------------------------------------------------------------------------- #
# Task 2 — queue_issue
# --------------------------------------------------------------------------- #


def _gh_issue_list(numbers: list[int]) -> MagicMock:
    import json

    return MagicMock(
        returncode=0, stdout=json.dumps([{"number": n} for n in numbers]), stderr=""
    )


def test_queue_issue_pins_repo_and_marks_ready(store: Any) -> None:
    config = _config("o/pinned")
    with (
        patch("kew.service.verbs.run") as gh_list,
        patch("kew.labels.run") as gh_edit,
    ):
        gh_list.return_value = _gh_issue_list([42])
        gh_edit.return_value = MagicMock(returncode=0, stdout="", stderr="")
        verbs.queue_issue(store, issue=42, config=config)

    # Candidate query pinned to config.project.repo (no client repo honored).
    argv = gh_list.call_args.args[0]
    assert "o/pinned" in argv and "--state" in argv and "open" in argv
    events = store.list_audit_events(event="issue_queued")
    assert len(events) == 1 and events[0].issue_num == 42
    # mark_ready ran (added the ready label via gh issue edit).
    gh_edit.assert_called_once()


def test_queue_issue_rejects_unknown_issue(store: Any) -> None:
    config = _config()
    with (
        patch("kew.service.verbs.run") as gh_list,
        patch("kew.labels.run") as gh_edit,
    ):
        gh_list.return_value = _gh_issue_list([42])
        with pytest.raises(ValueError, match="not an open issue"):
            verbs.queue_issue(store, issue=99, config=config)

    # Rejected before any event or effector.
    assert store.list_audit_events(event="issue_queued") == []
    gh_edit.assert_not_called()


def test_queue_issue_event_stands_when_mark_ready_fails(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config()

    def _boom(_issue: int, _config: Any) -> None:
        raise RuntimeError("mark_ready blew up")

    monkeypatch.setattr(verbs, "mark_ready", _boom)
    with patch("kew.service.verbs.run") as gh_list:
        gh_list.return_value = _gh_issue_list([42])
        with pytest.raises(RuntimeError, match="mark_ready blew up"):
            verbs.queue_issue(store, issue=42, config=config)

    assert len(store.list_audit_events(event="issue_queued")) == 1


def test_surfaced_candidates_failclosed_on_gh_error(store: Any) -> None:
    config = _config()
    with patch("kew.service.verbs.run") as gh_list:
        gh_list.return_value = MagicMock(returncode=1, stdout="", stderr="boom")
        assert verbs.surfaced_candidates(config) == set()


# --------------------------------------------------------------------------- #
# Task 3 — ack_orphan_pr
# --------------------------------------------------------------------------- #


def _seed_orphan(store: Any, pr_number: int = 7) -> None:
    store.sync_orphan_prs(
        [(pr_number, f"https://x/pull/{pr_number}", "agent/issue-1", "no-record")]
    )


def test_ack_orphan_pr_sets_acked_at_and_emits(store: Any) -> None:
    config = _config()
    _seed_orphan(store, 7)
    with patch("kew.service.verbs.run") as gh:
        result = verbs.ack_orphan_pr(store, pr_number=7, config=config)

    assert result is True
    # Never touched the PR: no gh interaction from the ack verb.
    gh.assert_not_called()
    events = store.list_audit_events(event="orphan_pr_acked")
    assert len(events) == 1 and events[0].payload["pr_number"] == 7
    # acked row is hidden from the default (unacked) listing.
    assert store.list_orphan_prs(include_acked=False) == []
    acked = store.list_orphan_prs(include_acked=True)
    assert len(acked) == 1 and acked[0].acked_at is not None


def test_ack_orphan_pr_idempotent(store: Any) -> None:
    config = _config()
    _seed_orphan(store, 7)
    verbs.ack_orphan_pr(store, pr_number=7, config=config)
    first_acked_at = store.list_orphan_prs(include_acked=True)[0].acked_at

    # Second ack: no error, acked_at preserved (first ack wins).
    assert verbs.ack_orphan_pr(store, pr_number=7, config=config) is True
    assert store.list_orphan_prs(include_acked=True)[0].acked_at == first_acked_at


def test_ack_orphan_pr_unknown_returns_false(store: Any) -> None:
    config = _config()
    assert verbs.ack_orphan_pr(store, pr_number=999, config=config) is False


# ---------------------------------------------------------------------------
# I14: the operator end of the publication-capacity transaction.
# ---------------------------------------------------------------------------

_BASE = "b" * 40
_TREE = "c" * 40
_PACKAGE = "sha256:" + "a" * 64
_STAMP = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)


def _cleared_job(
    store: Any, *, repo: str = "o/r", run_id: str = "run-pub", policy: Any = None
) -> Any:
    """A run, its sealed receipt, its publication job, and a measured review."""
    from kew.human_review import (
        PathChange,
        ReviewPolicy,
        decide_publication,
        measure_risk,
    )
    from kew.publication_job import PublicationIntent
    from kew.publication_repository import VERIFICATION_EXACT_TREE
    from kew.run_store import ArtifactLineage

    policy = policy or ReviewPolicy(delivery_repo=repo)
    store.save(
        RunRecord(
            run_id=run_id,
            issue_num=42,
            item_id="42",
            branch=f"agent/{run_id}",
            status="success",
            exit_code=0,
            started_at=_STAMP,
            finished_at=_STAMP,
        )
    )
    receipt = store.seal_artifact_receipt(
        lineage=ArtifactLineage(
            source_kind="github",
            source_id=repo,
            run_id=run_id,
            attempt_number=1,
            base_sha=_BASE,
            package_digest=_PACKAGE,
            tree_sha=_TREE,
            verification=VERIFICATION_EXACT_TREE,
            locator="packages/" + "a" * 64,
        ),
        sealed_at=_STAMP,
    )
    job = store.open_publication_job(
        intent=PublicationIntent(
            source_kind="github",
            source_id=repo,
            run_id=run_id,
            attempt_number=1,
            branch=f"agent/{run_id}",
            base_sha=_BASE,
            tree_sha=_TREE,
            receipt_digest=receipt.receipt_digest,
            message="feat: the change",
        ),
        now=_STAMP,
    )
    risk = measure_risk(
        base_sha=_BASE, tree_sha=_TREE, changes=[PathChange("src/a.py", added=1)]
    )
    decision = decide_publication(policy=policy, risk=risk, pending_reviews=0)
    store.record_publication_review(
        job_id=job.job_id, policy=policy, risk=risk, decision=decision, now=_STAMP
    )
    return job


def _pr_list(payload: str = "[]") -> Any:
    return MagicMock(returncode=0, stdout=payload)


def test_reserve_publication_capacity_reconciles_then_reserves(store: Any) -> None:
    job = _cleared_job(store)

    with patch("kew.outcomes.run") as gh:
        gh.return_value = _pr_list()
        reservation = verbs.reserve_publication_capacity(
            store, job_id=job.job_id, config=_config(), now=_STAMP
        )

    assert reservation.review.occupies_slot
    assert reservation.occupancy == 1
    # The reconciliation the reservation counted from is the one it just took.
    assert store.review_observation("o/r").generation == 0
    event = store.list_audit_events(event="publication_capacity_reserved")
    assert len(event) == 1
    assert event[0].payload["job_id"] == job.job_id
    assert event[0].payload["observation_generation"] == 0

    from kew.audit_chain import verify_chain

    assert verify_chain(store.read_chain_rows()).ok


def test_reserve_publication_capacity_holds_when_the_forge_is_unreadable(
    store: Any,
) -> None:
    from kew.publication_job import PublicationCapacityUnknownError

    job = _cleared_job(store)

    with patch("kew.outcomes.run") as gh:
        gh.return_value = MagicMock(returncode=1, stdout="")
        with pytest.raises(PublicationCapacityUnknownError, match="not an empty one"):
            verbs.reserve_publication_capacity(
                store, job_id=job.job_id, config=_config(), now=_STAMP
            )

    assert store.pending_review_count("o/r") == 0


def test_reserve_publication_capacity_counts_the_repos_own_open_reviews(
    store: Any,
) -> None:
    from kew.human_review import ReviewCapacityError, ReviewPolicy

    policy = ReviewPolicy(delivery_repo="o/r", max_pending_reviews=1)
    job = _cleared_job(store, policy=policy)
    config = _config()
    config.human_review.max_pending_reviews = 1

    with patch("kew.outcomes.run") as gh:
        gh.return_value = _pr_list(
            json.dumps(
                [
                    {
                        "number": 3,
                        "url": "https://github.com/o/r/pull/3",
                        "headRefName": "someone/else",
                        "body": "",
                        "state": "OPEN",
                    }
                ]
            )
        )
        with pytest.raises(ReviewCapacityError, match="holding 1 of 1"):
            verbs.reserve_publication_capacity(
                store, job_id=job.job_id, config=config, now=_STAMP
            )

    assert store.pending_review_count("o/r") == 0


def test_reserve_publication_capacity_refuses_a_foreign_delivery_repository(
    store: Any,
) -> None:
    job = _cleared_job(store)

    with pytest.raises(ValueError, match="does not govern"):
        verbs.reserve_publication_capacity(
            store, job_id=job.job_id, config=_config("other/repo"), now=_STAMP
        )


def test_release_publication_capacity_is_attested_only_when_it_frees_a_slot(
    store: Any,
) -> None:
    from kew.publication_job import SLOT_HELD, SLOT_RELEASED

    job = _cleared_job(store)
    config = _config()
    with patch("kew.outcomes.run") as gh:
        gh.return_value = _pr_list()
        verbs.reserve_publication_capacity(
            store, job_id=job.job_id, config=config, now=_STAMP
        )

    verbs.release_publication_capacity(
        store, job_id=job.job_id, disposition=SLOT_HELD, config=config, now=_STAMP
    )
    assert store.pending_review_count("o/r") == 1
    assert store.list_audit_events(event="publication_capacity_released") == []

    verbs.release_publication_capacity(
        store, job_id=job.job_id, disposition=SLOT_RELEASED, config=config, now=_STAMP
    )
    assert store.pending_review_count("o/r") == 0
    assert len(store.list_audit_events(event="publication_capacity_released")) == 1


def test_confirm_publication_risk_is_bound_to_the_sha_the_job_still_names(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kew.human_review import ReviewPolicy

    monkeypatch.setenv("KEW_ACTOR", "ada")
    policy = ReviewPolicy(delivery_repo="o/r", max_diff_lines=0, approvers=("ada",))
    config = _config()
    config.human_review.max_diff_lines = 0
    config.human_review.approvers = ["ada"]
    config.approval.approvers = ["ada"]
    job = _cleared_job(store, policy=policy)
    store.reserve_publication_review(
        job_id=job.job_id, policy=policy, actor="ada", now=_STAMP
    )

    review = verbs.confirm_publication_risk(
        store, job_id=job.job_id, config=config, now=_STAMP
    )

    assert review.review_state == "confirmed"
    assert review.confirmed_by == "ada"
    event = store.list_audit_events(event="publication_risk_confirmed")
    assert len(event) == 1
    assert event[0].payload["tree_sha"] == _TREE


def test_finalize_publication_run_settles_the_job_and_frees_its_slot(
    store: Any,
) -> None:
    """The finalization writer is cut over: the job settles, not the status."""
    job = _cleared_job(store)
    config = _config()
    with patch("kew.outcomes.run") as gh:
        gh.return_value = _pr_list()
        verbs.reserve_publication_capacity(
            store, job_id=job.job_id, config=config, now=_STAMP
        )

    finalized = verbs.finalize_publication_run(store, run_id="run-pub", config=config)

    assert finalized.status == "success"
    assert store.get_publication_job(job.job_id).state == "abandoned"
    assert store.pending_review_count("o/r") == 0
    event = store.list_audit_events(event="publication_finalized")
    assert event[0].payload["job_id"] == job.job_id


# ---------------------------------------------------------------------------
# I11 (Tasks 46-48): the audited no-change verbs.
#
# These three verbs are the ONLY way a no-change outcome may be confirmed,
# rejected or retried, so what is asserted here is not just "the store moved"
# but "the ledger says who moved it, under which authority rule, and against
# what evidence" — a closed item with no diff leaves nothing else to read.
# ---------------------------------------------------------------------------

from kew.no_change import (  # noqa: E402
    AUTHORITY_RULE_ALLOWLIST,
    AUTHORITY_RULE_REPOSITORY,
    COMPLETED,
    ITEM_CLOSED,
    ITEM_OPEN,
    PENDING_CONFIRMATION,
    REJECTED,
    SYNC_ACKED,
    SYNC_INTENT,
    SYNC_PENDING,
    ItemObservation,
    NamedCheck,
    NoChangeEvidence,
    NoChangeIntent,
    NoChangeStateError,
    RepositoryAuthority,
)

NC_BASE = "a" * 40
NC_MOVED = "b" * 40
NC_PKG = "sha256:" + "c" * 64
NC_CHECK = "sha256:" + "d" * 64
NC_NOW = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)


class _NoChangeTracker:
    """A tracker that says yes, and remembers exactly what it was asked."""

    def __init__(self, *, state: str = ITEM_OPEN, marker_found: bool = False) -> None:
        self.state = state
        self.marker_found = marker_found
        self.writes: list[dict[str, Any]] = []

    def observe(self, *, marker: str) -> ItemObservation:
        return ItemObservation(
            available=True, state=self.state, marker_found=self.marker_found
        )

    def apply(self, *, body: str, close: bool) -> bool:
        self.writes.append({"body": body, "close": close})
        self.state = ITEM_CLOSED if close else self.state
        self.marker_found = self.marker_found or close
        return True


def _pending_no_change(store: Any, *, item: str = "1533") -> Any:
    """One attested candidate waiting for a person, exactly as I10B leaves it."""
    judgment = store.open_no_change_judgment(
        intent=NoChangeIntent(
            source_kind="github",
            source_id=item,
            run_id="run-" + item,
            attempt_number=1,
            base_sha=NC_BASE,
            item_id=item,
        ),
        now=NC_NOW,
    )
    store.attest_no_change(
        judgment_id=judgment.judgment_id,
        evidence=NoChangeEvidence(
            trusted_base_sha=NC_BASE,
            observed_base_sha=NC_BASE,
            package_digest=NC_PKG,
            package_scope="workspace",
            package_entry_count=0,
            package_deletion_count=0,
            checks=(NamedCheck(name="pytest", outcome="passed", digest=NC_CHECK),),
        ),
        now=NC_NOW,
    )
    return store.decide_no_change(judgment_id=judgment.judgment_id, now=NC_NOW)


@contextmanager
def _remote(*, base: str = NC_BASE, permission: str = "write", available=True):
    """The remote reads the verbs make, answered without a network.

    ``ada`` is both the operator and the forge login here, so the permission
    evidence this hands back describes the same name the verb resolves from
    ``gh api user`` — the identity split itself is exercised by ``_fake_gh``.
    """
    authority = RepositoryAuthority(
        delivery_repo="o/r",
        actor="ada",
        permission=permission,
        observed_at=NC_NOW.isoformat(),
        available=available,
        detail="" if available else "gh could not be reached",
    )
    with (
        patch("kew.service.verbs.observe_repository_authority", return_value=authority),
        patch("kew.outcomes.run") as gh,
    ):
        gh.side_effect = lambda argv, **_: MagicMock(
            returncode=0,
            stdout="ada" if argv[:3] == ["gh", "api", "user"] else base,
            stderr="",
        )
        yield gh


def _payload(store: Any, event: str) -> dict[str, Any]:
    events = store.list_audit_events(event=event)
    assert len(events) == 1, f"expected exactly one {event} event, got {len(events)}"
    return dict(events[0].payload)


@contextmanager
def _fake_gh(
    *,
    login: str = "owner",
    permission: str = "admin",
    base: str = NC_BASE,
    login_rc: int = 0,
):
    """A ``gh`` that answers by argv, so the verbs resolve a REAL login path.

    ``observe_repository_authority`` is deliberately NOT patched here: the bug
    in #2463 was the argument it was called with, so a test that stubs the
    whole read cannot see it. The collaborators route answers ``none`` for any
    name other than *login*, exactly as the forge does for an OS username that
    is not an account.
    """

    def _answer(argv, **_: Any) -> MagicMock:
        if argv[:3] == ["gh", "api", "user"]:
            return MagicMock(
                returncode=login_rc,
                stdout=login if login_rc == 0 else "",
                stderr="gh: not authenticated",
            )
        route = argv[2] if len(argv) > 2 else ""
        if "/collaborators/" in route:
            asked = route.split("/collaborators/")[1].split("/")[0]
            return MagicMock(
                returncode=0,
                stdout=permission if asked == login else "none",
                stderr="",
            )
        return MagicMock(returncode=0, stdout=base, stderr="")

    with patch("kew.outcomes.run", side_effect=_answer) as gh:
        yield gh


def _asked_about(gh: Any) -> list[str]:
    """Every login the delivery repository's permission was read for."""
    return [
        call.args[0][2].split("/collaborators/")[1].split("/")[0]
        for call in gh.call_args_list
        if len(call.args[0]) > 2 and "/collaborators/" in call.args[0][2]
    ]


class TestNoChangeAuthorityAtTheVerb:
    def test_no_change_confirm_requires_allowlist_membership(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KEW_ACTOR", "mallory")
        config = _config()
        config.human_review.approvers = ["ada"]
        judgment = _pending_no_change(store)
        with _remote(permission="admin"), pytest.raises(verbs.VerbDenied) as exc:
            verbs.confirm_no_change(
                store, judgment_id=judgment.judgment_id, config=config, now=NC_NOW
            )
        assert "not in the human_review allowlist" in str(exc.value)
        # Refused before any effect: the judgment is untouched and unsynced.
        assert store.get_no_change_judgment(judgment.judgment_id).state == (
            PENDING_CONFIRMATION
        )
        assert store.list_audit_events(event="no_change_confirmed") == []

    def test_no_change_confirm_by_a_named_approver_records_the_allowlist_rule(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KEW_ACTOR", "ada")
        config = _config()
        config.human_review.approvers = ["ada"]
        judgment = _pending_no_change(store)
        tracker = _NoChangeTracker()
        with (
            _remote(),
            patch("kew.outcomes.no_change_item_adapter", return_value=tracker),
        ):
            verbs.confirm_no_change(
                store, judgment_id=judgment.judgment_id, config=config, now=NC_NOW
            )
        payload = _payload(store, "no_change_confirmed")
        assert payload["authority_rule"] == AUTHORITY_RULE_ALLOWLIST
        assert payload["authorized"] is True
        assert payload["authority_decided_at"] == NC_NOW.isoformat()

    def test_no_change_confirm_with_an_empty_allowlist_needs_repository_authority(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KEW_ACTOR", "ada")
        config = _config()
        config.human_review.approvers = []
        config.approval.approvers = []
        judgment = _pending_no_change(store)
        with _remote(permission="read"), pytest.raises(verbs.VerbDenied) as exc:
            verbs.confirm_no_change(
                store, judgment_id=judgment.judgment_id, config=config, now=NC_NOW
            )
        assert "not write, maintain or admin" in str(exc.value)

    def test_no_change_confirm_with_an_empty_allowlist_accepts_write_evidence(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KEW_ACTOR", "ada")
        config = _config()
        config.human_review.approvers = []
        config.approval.approvers = []
        judgment = _pending_no_change(store)
        tracker = _NoChangeTracker()
        with (
            _remote(),
            patch("kew.outcomes.no_change_item_adapter", return_value=tracker),
        ):
            verbs.confirm_no_change(
                store, judgment_id=judgment.judgment_id, config=config, now=NC_NOW
            )
        assert _payload(store, "no_change_confirmed")["authority_rule"] == (
            AUTHORITY_RULE_REPOSITORY
        )

    def test_no_change_confirm_refuses_when_the_authority_read_is_unavailable(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KEW_ACTOR", "ada")
        config = _config()
        config.human_review.approvers = []
        config.approval.approvers = []
        judgment = _pending_no_change(store)
        with (
            _remote(permission="", available=False),
            pytest.raises(verbs.VerbDenied),
        ):
            verbs.confirm_no_change(
                store, judgment_id=judgment.judgment_id, config=config, now=NC_NOW
            )

    def test_no_change_authority_is_not_asked_of_the_forge_when_named(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A named allowlist decides alone — no permission read is even made."""
        monkeypatch.setenv("KEW_ACTOR", "ada")
        config = _config()
        config.human_review.approvers = ["ada"]
        judgment = _pending_no_change(store)
        tracker = _NoChangeTracker()
        with (
            patch("kew.service.verbs.observe_repository_authority") as observer,
            patch("kew.outcomes.run") as gh,
            patch("kew.outcomes.no_change_item_adapter", return_value=tracker),
        ):
            gh.return_value = MagicMock(returncode=0, stdout=NC_BASE, stderr="")
            verbs.confirm_no_change(
                store, judgment_id=judgment.judgment_id, config=config, now=NC_NOW
            )
        observer.assert_not_called()


class TestNoChangeForgeIdentity:
    """#2463: the permission rule asks the forge about a forge login.

    The bug this class exists for refused the repository's own owner on his own
    repository: with no approvers configured, authority was read for the LOCAL
    OS username, the forge answered ``none`` for a name that is not an account,
    and every console confirm/reject was a 403. So what is asserted here is
    *which name the forge was asked about*, not merely that a grant happened.
    """

    def test_the_permission_rule_asks_about_the_forge_login(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KEW_ACTOR", "localuser")
        config = _config()
        config.human_review.approvers = []
        config.approval.approvers = []
        judgment = _pending_no_change(store)
        tracker = _NoChangeTracker()
        with (
            _fake_gh(login="thossullivan", permission="admin") as gh,
            patch("kew.outcomes.no_change_item_adapter", return_value=tracker),
        ):
            verbs.confirm_no_change(
                store, judgment_id=judgment.judgment_id, config=config, now=NC_NOW
            )
        assert _asked_about(gh) == ["thossullivan"], (
            "the permission read must name the forge login, not the OS username"
        )
        payload = _payload(store, "no_change_confirmed")
        assert payload["authority_rule"] == AUTHORITY_RULE_REPOSITORY
        assert payload["authorized"] is True
        # Both identities, so a reader can tell the two names apart later.
        assert payload["authority_actor"] == "thossullivan"
        assert payload["forge_actor"] == "thossullivan"
        assert payload["operator_actor"] == "localuser"
        assert store.get_no_change_judgment(judgment.judgment_id).decided_by == (
            "thossullivan"
        )

    def test_a_read_only_forge_login_is_refused_by_that_login(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KEW_ACTOR", "localuser")
        config = _config()
        config.human_review.approvers = []
        config.approval.approvers = []
        judgment = _pending_no_change(store)
        with (
            _fake_gh(login="drive-by", permission="read"),
            pytest.raises(verbs.VerbDenied) as exc,
        ):
            verbs.confirm_no_change(
                store, judgment_id=judgment.judgment_id, config=config, now=NC_NOW
            )
        assert "drive-by has 'read' on o/r" in str(exc.value)
        assert "not write, maintain or admin" in str(exc.value)
        assert store.get_no_change_judgment(judgment.judgment_id).state == (
            PENDING_CONFIRMATION
        )

    def test_an_unreadable_forge_login_is_refused_without_a_permission_read(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Not knowing who is asking is not evidence that they may close it."""
        monkeypatch.setenv("KEW_ACTOR", "localuser")
        config = _config()
        config.human_review.approvers = []
        config.approval.approvers = []
        judgment = _pending_no_change(store)
        with (
            _fake_gh(login_rc=1) as gh,
            pytest.raises(verbs.VerbDenied) as exc,
        ):
            verbs.confirm_no_change(
                store, judgment_id=judgment.judgment_id, config=config, now=NC_NOW
            )
        assert "authenticated forge login could not be read" in str(exc.value)
        assert _asked_about(gh) == [], (
            "an unknown login must not be substituted by the OS username"
        )

    def test_a_named_allowlist_never_resolves_a_forge_login(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An allowlist names operators; membership is not the forge's question."""
        monkeypatch.setenv("KEW_ACTOR", "ada")
        config = _config()
        config.human_review.approvers = ["ada"]
        judgment = _pending_no_change(store)
        tracker = _NoChangeTracker()
        with (
            _remote(),
            patch("kew.service.verbs.observe_forge_identity") as identity,
            patch("kew.outcomes.no_change_item_adapter", return_value=tracker),
        ):
            verbs.confirm_no_change(
                store, judgment_id=judgment.judgment_id, config=config, now=NC_NOW
            )
        identity.assert_not_called()
        payload = _payload(store, "no_change_confirmed")
        assert payload["authority_rule"] == AUTHORITY_RULE_ALLOWLIST
        assert payload["operator_actor"] == "ada"
        assert payload["forge_actor"] == ""

    def test_the_cli_reject_records_the_reason_and_leaves_the_item_open(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The forge identity carries through to the name posted on the item."""
        monkeypatch.setenv("KEW_ACTOR", "localuser")
        config = _config()
        config.human_review.approvers = []
        config.approval.approvers = []
        judgment = _pending_no_change(store)
        tracker = _NoChangeTracker()
        with (
            _fake_gh(login="thossullivan", permission="admin"),
            patch("kew.outcomes.no_change_item_adapter", return_value=tracker),
        ):
            verbs.reject_no_change(
                store,
                judgment_id=judgment.judgment_id,
                reason="the work was never attempted",
                config=config,
                now=NC_NOW,
            )
        rejected = store.get_no_change_judgment(judgment.judgment_id)
        assert rejected.state == REJECTED
        assert rejected.decided_by == "thossullivan"
        assert tracker.state == ITEM_OPEN
        assert "thossullivan" in tracker.writes[-1]["body"]


class TestConfirmNoChange:
    def test_no_change_confirm_synchronizes_and_completes(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KEW_ACTOR", "ada")
        config = _config()
        config.human_review.approvers = ["ada"]
        judgment = _pending_no_change(store)
        tracker = _NoChangeTracker()
        with (
            _remote(),
            patch("kew.outcomes.no_change_item_adapter", return_value=tracker),
        ):
            result = verbs.confirm_no_change(
                store, judgment_id=judgment.judgment_id, config=config, now=NC_NOW
            )
        assert [job["state"] for job in result["sync"]] == [SYNC_ACKED]
        assert len(tracker.writes) == 1 and tracker.writes[0]["close"] is True
        settled = store.get_no_change_judgment(judgment.judgment_id)
        assert settled.decided_by == "ada"
        from kew.audit_chain import verify_chain

        assert verify_chain(store.read_chain_rows()).ok

    def test_no_change_confirm_refuses_a_moved_base_before_any_effect(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KEW_ACTOR", "ada")
        config = _config()
        config.human_review.approvers = ["ada"]
        judgment = _pending_no_change(store)
        with _remote(base=NC_MOVED), pytest.raises(verbs.OutcomeStale) as exc:
            verbs.confirm_no_change(
                store, judgment_id=judgment.judgment_id, config=config, now=NC_NOW
            )
        assert "stale" in str(exc.value)
        assert store.get_no_change_judgment(judgment.judgment_id).state == (
            PENDING_CONFIRMATION
        )
        assert store.list_audit_events(event="no_change_confirmed") == []

    def test_no_change_confirm_refuses_a_second_confirmer(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KEW_ACTOR", "ada")
        config = _config()
        config.human_review.approvers = ["ada", "bob"]
        judgment = _pending_no_change(store)
        tracker = _NoChangeTracker()
        with (
            _remote(),
            patch("kew.outcomes.no_change_item_adapter", return_value=tracker),
        ):
            verbs.confirm_no_change(
                store, judgment_id=judgment.judgment_id, config=config, now=NC_NOW
            )
            monkeypatch.setenv("KEW_ACTOR", "bob")
            with pytest.raises(NoChangeStateError):
                verbs.confirm_no_change(
                    store, judgment_id=judgment.judgment_id, config=config, now=NC_NOW
                )
        assert len(tracker.writes) == 1  # the item was not commented on twice
        assert store.get_no_change_judgment(judgment.judgment_id).decided_by == "ada"

    def test_no_change_confirm_holds_sync_pending_when_the_tracker_is_out(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KEW_ACTOR", "ada")
        config = _config()
        config.human_review.approvers = ["ada"]
        judgment = _pending_no_change(store)
        with _remote(), patch("kew.outcomes.no_change_item_adapter", return_value=None):
            result = verbs.confirm_no_change(
                store, judgment_id=judgment.judgment_id, config=config, now=NC_NOW
            )
        assert result["state"] == SYNC_PENDING
        assert [job["state"] for job in result["sync"]] == [SYNC_INTENT]
        # Authorized locally, unsynchronized remotely: nothing downstream may run.
        assert store.list_outcome_sync_jobs(judgment.judgment_id)[0].acked_at is None

    def test_no_change_confirm_refuses_an_unknown_judgment(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KEW_ACTOR", "ada")
        config = _config()
        config.human_review.approvers = ["ada"]
        with pytest.raises(ValueError):
            verbs.confirm_no_change(
                store, judgment_id="nc-missing", config=config, now=NC_NOW
            )


class TestRejectNoChange:
    def test_no_change_reject_leaves_the_item_open_and_unsatisfied(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KEW_ACTOR", "ada")
        config = _config()
        config.human_review.approvers = ["ada"]
        judgment = _pending_no_change(store)
        tracker = _NoChangeTracker()
        with (
            _remote(),
            patch("kew.outcomes.no_change_item_adapter", return_value=tracker),
        ):
            result = verbs.reject_no_change(
                store,
                judgment_id=judgment.judgment_id,
                reason="the named check never ran",
                config=config,
                now=NC_NOW,
            )
        assert result["state"] == REJECTED
        assert result["disposition_noted"] is True
        assert tracker.writes[0]["close"] is False  # never closes
        assert tracker.state == ITEM_OPEN
        assert store.list_outcome_sync_jobs(judgment.judgment_id) == ()
        assert store.list_dependency_effects(judgment.judgment_id) == ()
        payload = _payload(store, "no_change_rejected")
        assert payload["reason"] == "the named check never ran"

    def test_no_change_reject_requires_a_reason(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KEW_ACTOR", "ada")
        config = _config()
        config.human_review.approvers = ["ada"]
        judgment = _pending_no_change(store)
        with pytest.raises(ValueError):
            verbs.reject_no_change(
                store,
                judgment_id=judgment.judgment_id,
                reason="   ",
                config=config,
                now=NC_NOW,
            )
        assert store.get_no_change_judgment(judgment.judgment_id).state == (
            PENDING_CONFIRMATION
        )

    def test_no_change_reject_is_refused_for_an_unauthorized_actor(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KEW_ACTOR", "mallory")
        config = _config()
        config.human_review.approvers = ["ada"]
        judgment = _pending_no_change(store)
        with _remote(), pytest.raises(verbs.VerbDenied):
            verbs.reject_no_change(
                store,
                judgment_id=judgment.judgment_id,
                reason="no",
                config=config,
                now=NC_NOW,
            )
        assert store.get_no_change_judgment(judgment.judgment_id).state == (
            PENDING_CONFIRMATION
        )

    def test_no_change_reject_attests_nothing_for_a_settled_judgment(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A completed outcome is nobody's decision to refuse a second time.

        The web route addresses a judgment by id, so this verb is reachable
        over one that has already synchronized and closed its item. The store
        refuses the move — but the event is emitted before the effector, so
        without the state admission the refusal left a ``no_change_rejected``
        event standing over an item that was closed, not refused.
        """
        monkeypatch.setenv("KEW_ACTOR", "ada")
        config = _config()
        config.human_review.approvers = ["ada"]
        judgment = _pending_no_change(store)
        tracker = _NoChangeTracker()
        with (
            _remote(),
            patch("kew.outcomes.no_change_item_adapter", return_value=tracker),
        ):
            verbs.confirm_no_change(
                store, judgment_id=judgment.judgment_id, config=config, now=NC_NOW
            )
            with pytest.raises(NoChangeStateError) as exc:
                verbs.reject_no_change(
                    store,
                    judgment_id=judgment.judgment_id,
                    reason="on reflection, no",
                    config=config,
                    now=NC_NOW,
                )
        assert "nothing left to refuse" in str(exc.value)
        assert store.list_audit_events(event="no_change_rejected") == []
        assert len(tracker.writes) == 1  # the close, and no refusal note after it
        assert store.get_no_change_judgment(judgment.judgment_id).state == COMPLETED

    def test_no_change_reject_survives_a_tracker_that_cannot_be_noted(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KEW_ACTOR", "ada")
        config = _config()
        config.human_review.approvers = ["ada"]
        judgment = _pending_no_change(store)
        with _remote(), patch("kew.outcomes.no_change_item_adapter", return_value=None):
            result = verbs.reject_no_change(
                store,
                judgment_id=judgment.judgment_id,
                reason="unattested",
                config=config,
                now=NC_NOW,
            )
        assert result["state"] == REJECTED and result["disposition_noted"] is False


class TestRetryNoChangeSync:
    def test_no_change_retry_resumes_a_held_synchronization(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KEW_ACTOR", "ada")
        config = _config()
        config.human_review.approvers = ["ada"]
        judgment = _pending_no_change(store)
        with _remote(), patch("kew.outcomes.no_change_item_adapter", return_value=None):
            verbs.confirm_no_change(
                store, judgment_id=judgment.judgment_id, config=config, now=NC_NOW
            )
        tracker = _NoChangeTracker()
        with (
            _remote(),
            patch("kew.outcomes.no_change_item_adapter", return_value=tracker),
        ):
            result = verbs.retry_no_change_sync(
                store, judgment_id=judgment.judgment_id, config=config, now=NC_NOW
            )
        assert [job["state"] for job in result["sync"]] == [SYNC_ACKED]
        assert len(tracker.writes) == 1
        assert _payload(store, "no_change_sync_retried")["state"] == SYNC_PENDING

    def test_no_change_retry_is_refused_for_an_unauthorized_actor(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KEW_ACTOR", "mallory")
        config = _config()
        config.human_review.approvers = ["ada"]
        judgment = _pending_no_change(store)
        with _remote(), pytest.raises(verbs.VerbDenied):
            verbs.retry_no_change_sync(
                store, judgment_id=judgment.judgment_id, config=config, now=NC_NOW
            )
        assert store.list_audit_events(event="no_change_sync_retried") == []

    def test_no_change_retry_writes_nothing_twice(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KEW_ACTOR", "ada")
        config = _config()
        config.human_review.approvers = ["ada"]
        judgment = _pending_no_change(store)
        tracker = _NoChangeTracker()
        with (
            _remote(),
            patch("kew.outcomes.no_change_item_adapter", return_value=tracker),
        ):
            verbs.confirm_no_change(
                store, judgment_id=judgment.judgment_id, config=config, now=NC_NOW
            )
            result = verbs.retry_no_change_sync(
                store, judgment_id=judgment.judgment_id, config=config, now=NC_NOW
            )
        assert len(tracker.writes) == 1
        assert [job["state"] for job in result["sync"]] == [SYNC_ACKED]


# --------------------------------------------------------------------------- #
# I20 — the guarded work action.
#
# Each test below asks the same question from a different angle: can a caller
# make something happen that the projection did not itself derive as safe?
# --------------------------------------------------------------------------- #

WA_NOW = datetime(2026, 8, 22, 9, 0, tzinfo=UTC)


def _wa_config(actor: str = "ada") -> DispatchConfig:
    config = _config()
    config.approval.approvers = [actor]
    config.human_review.approvers = [actor]
    return config


def _preserved(
    store: Any, *, item_id: str = "42", run_id: str = "run-preserved"
) -> None:
    """One item whose implementation is preserved and unpublished."""
    store.admit_run_with_identity(
        RunRecord(
            run_id=run_id,
            issue_num=int(item_id) if item_id.isdigit() else 0,
            item_id=item_id,
            branch=f"agent/issue-{item_id.lower()}",
            status="awaiting_publication",
            exit_code=0,
            started_at=WA_NOW,
            finished_at=WA_NOW,
            attempt_number=1,
        ),
        AdmissionIdentity(
            run_id=run_id,
            attempt_number=1,
            source_tracker="github" if item_id.isdigit() else "linear",
            item_id=item_id,
            delivery_repo="o/r",
        ),
    )


def _tracker_item_open() -> TrackerItemFact:
    """One real reading of an open source item, so dispatch is not blocked."""
    return TrackerItemFact(
        observation=RemoteObservation(
            dimension="tracker_item",
            availability="available",
            provenance="observed",
            observed_at=WA_NOW,
        ),
        state="open",
    )


def _open_pr() -> Any:
    """One real reading of an open pull request on the forge."""
    from kew.service.work import PullRequestFact

    return PullRequestFact(
        observation=RemoteObservation(
            dimension="pull_request",
            availability="available",
            provenance="observed",
            observed_at=WA_NOW,
        ),
        url="https://x/pull/7",
        state="open",
        checks="passing",
        mergeability="clean",
    )


def _wa_view(store: Any, item_id: str = "42") -> Any:
    return get_work_view(store, item_id, composed_at=WA_NOW)


def _wa_events(store: Any, event: str) -> list[Any]:
    return store.list_audit_events(event=event, limit=None)


class TestPerformWorkAction:
    def test_preserved_work_finalizes_the_same_artifact_and_returns_the_new_truth(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The whole contract in one pass: derived action, canonical target
        read server-side, intent attested before the effect, and the view that
        comes back is the post-write one."""
        monkeypatch.setenv("KEW_ACTOR", "ada")
        _preserved(store)
        before = _wa_view(store)

        result = verbs.perform_work_action(
            store,
            item_id="42",
            action="finalize_publication",
            expected_revision=before.view_revision,
            config=_wa_config(),
            now=WA_NOW,
        )

        assert result.applied is True and result.idempotent is False
        # The caller never named the run; the projection's own facts did.
        assert (result.target_kind, result.target_ref) == ("run", "run-preserved")
        assert store.get("run-preserved").status == "success"
        # Post-write truth, not the world the decision was made against.
        assert result.view.view_revision != before.view_revision
        assert result.view.artifact.publication != "pending"
        assert result.view.actions == []
        intent = _wa_events(store, verbs.WORK_ACTION_REQUESTED)
        assert [e.payload["action"] for e in intent] == ["finalize_publication"]
        assert intent[0].payload["expected_revision"] == before.view_revision
        assert intent[0].item_id == "42"
        assert verify_chain(store.read_chain_rows()).ok

    def test_the_intent_event_precedes_the_effect(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Attest, then act — asserted by making the effect fail."""
        monkeypatch.setenv("KEW_ACTOR", "ada")
        _preserved(store)
        before = _wa_view(store)

        with (
            patch.object(
                verbs,
                "finalize_publication_run",
                side_effect=RuntimeError("forge down"),
            ),
            pytest.raises(RuntimeError, match="forge down"),
        ):
            verbs.perform_work_action(
                store,
                item_id="42",
                action="finalize_publication",
                expected_revision=before.view_revision,
                config=_wa_config(),
                now=WA_NOW,
            )

        # The intent stands (a lost intent record would erase who asked what)…
        assert len(_wa_events(store, verbs.WORK_ACTION_REQUESTED)) == 1
        # …and nothing claims the effect happened, so it stays retryable.
        assert _wa_events(store, verbs.WORK_ACTION_APPLIED) == []
        assert store.get("run-preserved").status == "awaiting_publication"

    def test_a_failed_effect_can_be_retried_at_the_same_revision(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The dedupe key is the APPLIED event, so a half-run request is not
        mistaken for a completed one."""
        monkeypatch.setenv("KEW_ACTOR", "ada")
        _preserved(store)
        before = _wa_view(store)
        request = {
            "item_id": "42",
            "action": "finalize_publication",
            "expected_revision": before.view_revision,
            "config": _wa_config(),
            "now": WA_NOW,
        }
        with (
            patch.object(verbs, "finalize_publication_run", side_effect=RuntimeError),
            pytest.raises(RuntimeError),
        ):
            verbs.perform_work_action(store, **request)

        result = verbs.perform_work_action(store, **request)

        assert result.applied is True
        assert store.get("run-preserved").status == "success"

    def test_a_duplicate_request_never_applies_the_effect_twice(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KEW_ACTOR", "ada")
        _preserved(store)
        before = _wa_view(store)
        request = {
            "item_id": "42",
            "action": "finalize_publication",
            "expected_revision": before.view_revision,
            "config": _wa_config(),
            "now": WA_NOW,
        }
        first = verbs.perform_work_action(store, **request)

        second = verbs.perform_work_action(store, **request)

        assert first.applied is True
        assert second.applied is False and second.idempotent is True
        assert second.target_ref == "run-preserved"
        # One effect, one applied record — and the replay still hands back the
        # authoritative current projection rather than an error.
        assert len(_wa_events(store, verbs.WORK_ACTION_APPLIED)) == 1
        assert len(_wa_events(store, "publication_finalized")) == 1
        assert second.view.view_revision == first.view.view_revision

    def test_a_stale_revision_is_refused_before_any_effect(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A decision made against facts that have since moved is not applied
        to the facts nobody looked at."""
        monkeypatch.setenv("KEW_ACTOR", "ada")
        _preserved(store)

        with pytest.raises(verbs.WorkActionStale) as exc:
            verbs.perform_work_action(
                store,
                item_id="42",
                action="finalize_publication",
                expected_revision="wv1:0000000000000000",
                config=_wa_config(),
                now=WA_NOW,
            )

        assert "moved" in str(exc.value)
        assert store.get("run-preserved").status == "awaiting_publication"
        assert _wa_events(store, verbs.WORK_ACTION_REQUESTED) == []

    def test_an_action_the_projection_does_not_offer_is_refused(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The guardrail, stated as a test: preserved work cannot be
        redispatched through this surface at any revision."""
        monkeypatch.setenv("KEW_ACTOR", "ada")
        _preserved(store)
        before = _wa_view(store)

        with pytest.raises(verbs.WorkActionUnavailable) as exc:
            verbs.perform_work_action(
                store,
                item_id="42",
                action="dispatch",
                expected_revision=before.view_revision,
                config=_wa_config(),
                now=WA_NOW,
            )

        assert "'dispatch' is not offered" in str(exc.value)
        assert "finalize_publication" in str(exc.value)
        assert store.get("run-preserved").status == "awaiting_publication"
        assert _wa_events(store, verbs.WORK_ACTION_REQUESTED) == []

    def test_an_unlisted_actor_is_refused_before_anything_is_read_back(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KEW_ACTOR", "mallory")
        _preserved(store)
        before = _wa_view(store)

        with pytest.raises(verbs.VerbDenied):
            verbs.perform_work_action(
                store,
                item_id="42",
                action="finalize_publication",
                expected_revision=before.view_revision,
                config=_wa_config("ada"),
                now=WA_NOW,
            )

        assert store.get("run-preserved").status == "awaiting_publication"
        assert _wa_events(store, verbs.WORK_ACTION_REQUESTED) == []

    def test_unreadable_facts_offer_nothing_so_every_action_fails_closed(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An item with no attempt and no readable source: the safest move is
        to look, and there is nothing to request."""
        monkeypatch.setenv("KEW_ACTOR", "ada")
        before = _wa_view(store, "77")
        assert before.safest_action.blocked_by == "tracker_item"

        with pytest.raises(verbs.WorkActionUnavailable):
            verbs.perform_work_action(
                store,
                item_id="77",
                action="dispatch",
                expected_revision=before.view_revision,
                config=_wa_config(),
                now=WA_NOW,
            )

    def test_an_opaque_item_id_reaches_its_own_run_unchanged(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KEW_ACTOR", "ada")
        _preserved(store, item_id="SFD-12", run_id="run-sfd")
        before = _wa_view(store, "SFD-12")

        result = verbs.perform_work_action(
            store,
            item_id="SFD-12",
            action="finalize_publication",
            expected_revision=before.view_revision,
            config=_wa_config(),
            now=WA_NOW,
        )

        assert result.target_ref == "run-sfd"
        assert result.view.identity.item_id == "SFD-12"
        assert _wa_events(store, verbs.WORK_ACTION_APPLIED)[0].item_id == "SFD-12"
        assert store.get("run-sfd").status == "success"

    def test_refusing_a_no_change_outcome_requires_a_reason(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KEW_ACTOR", "ada")
        judgment = _pending_no_change(store, item="42")
        before = _wa_view(store)
        assert [a.action for a in before.actions] == [
            "confirm_no_change",
            "reject_no_change",
        ]

        with pytest.raises(ValueError, match="requires a reason"):
            verbs.perform_work_action(
                store,
                item_id="42",
                action="reject_no_change",
                expected_revision=before.view_revision,
                config=_wa_config(),
                now=WA_NOW,
            )

        assert store.get_no_change_judgment(judgment.judgment_id).state == (
            PENDING_CONFIRMATION
        )
        assert _wa_events(store, verbs.WORK_ACTION_REQUESTED) == []

    def test_a_no_change_decision_delegates_to_the_owning_reconciler(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The work action adds a gate and an audit envelope; the decision
        itself is still made by the I11 verb, authority rules and all."""
        monkeypatch.setenv("KEW_ACTOR", "ada")
        judgment = _pending_no_change(store, item="42")
        before = _wa_view(store)

        with (
            _remote(),
            patch(
                "kew.outcomes.no_change_item_adapter",
                return_value=_NoChangeTracker(),
            ),
        ):
            result = verbs.perform_work_action(
                store,
                item_id="42",
                action="reject_no_change",
                expected_revision=before.view_revision,
                reason="the absence is not the answer here",
                config=_wa_config(),
                now=WA_NOW,
            )

        assert result.applied is True
        assert result.target_ref == judgment.judgment_id
        assert store.get_no_change_judgment(judgment.judgment_id).state == REJECTED
        assert len(_wa_events(store, "no_change_rejected")) == 1
        # Refusing a zero-diff outcome must not arm a redispatch.
        assert "dispatch" not in [a.action for a in result.view.actions]

    def test_a_stopped_fleet_offers_resume_instead_of_a_dispatch_that_cannot_run(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KEW_ACTOR", "ada")
        store.save(
            RunRecord(
                run_id="run-failed",
                issue_num=42,
                item_id="42",
                branch="agent/issue-42",
                status="failure",
                exit_code=1,
                started_at=WA_NOW,
                finished_at=WA_NOW,
            )
        )
        store.set_fleet_stopped(reason="budget breach")
        item = _tracker_item_open()
        before = get_work_view(store, "42", tracker_item=item, composed_at=WA_NOW)
        assert [a.action for a in before.actions] == ["resume_fleet"]

        result = verbs.perform_work_action(
            store,
            item_id="42",
            action="resume_fleet",
            expected_revision=before.view_revision,
            config=_wa_config(),
            tracker_item=item,
            now=WA_NOW,
        )

        assert result.applied is True
        assert store.get_fleet_stopped() is None
        # Post-write truth: with the latch cleared the retry is on offer again.
        assert [a.action for a in result.view.actions] == ["dispatch"]

    def test_confirming_a_zero_diff_outcome_runs_the_i11_reconciler(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KEW_ACTOR", "ada")
        judgment = _pending_no_change(store, item="42")
        before = _wa_view(store)
        tracker = _NoChangeTracker()

        with (
            _remote(),
            patch("kew.outcomes.no_change_item_adapter", return_value=tracker),
        ):
            result = verbs.perform_work_action(
                store,
                item_id="42",
                action="confirm_no_change",
                expected_revision=before.view_revision,
                config=_wa_config(),
                now=WA_NOW,
            )

        assert result.applied is True
        assert tracker.writes[0]["close"] is True
        assert len(_wa_events(store, "no_change_confirmed")) == 1
        assert judgment.judgment_id in result.detail

    def test_a_held_source_write_is_resynchronized_not_redecided(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The authorized-but-unsynchronized case: the item is open against a
        decision already made, so the offer is to finish it, never to rejudge."""
        monkeypatch.setenv("KEW_ACTOR", "ada")
        judgment = _pending_no_change(store, item="42")
        with _remote(), patch("kew.outcomes.no_change_item_adapter", return_value=None):
            verbs.confirm_no_change(
                store, judgment_id=judgment.judgment_id, config=_wa_config(), now=WA_NOW
            )
        before = _wa_view(store)
        assert before.exception.state == "no_change_authorized"
        assert [a.action for a in before.actions] == ["retry_no_change_sync"]
        tracker = _NoChangeTracker()

        with (
            _remote(),
            patch("kew.outcomes.no_change_item_adapter", return_value=tracker),
        ):
            result = verbs.perform_work_action(
                store,
                item_id="42",
                action="retry_no_change_sync",
                expected_revision=before.view_revision,
                config=_wa_config(),
                now=WA_NOW,
            )

        assert result.applied is True
        assert len(tracker.writes) == 1
        assert len(_wa_events(store, "no_change_sync_retried")) == 1

    def test_acknowledging_an_orphan_artifact_never_closes_it(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KEW_ACTOR", "ada")
        store.save(
            RunRecord(
                run_id="run-orphan",
                issue_num=42,
                item_id="42",
                branch="agent/issue-42",
                status="failure",
                exit_code=1,
                started_at=WA_NOW,
                finished_at=WA_NOW,
            )
        )
        store.sync_orphan_prs([(7, "https://x/pull/7", "agent/issue-42", "no-run")])
        reads = {"pull_request": _open_pr(), "tracker_item": _tracker_item_open()}
        before = get_work_view(store, "42", composed_at=WA_NOW, **reads)
        assert [a.action for a in before.actions] == ["acknowledge_orphan"]

        result = verbs.perform_work_action(
            store,
            item_id="42",
            action="acknowledge_orphan",
            expected_revision=before.view_revision,
            config=_wa_config(),
            now=WA_NOW,
            **reads,
        )

        assert result.applied is True and result.target_ref == "7"
        acked = store.list_orphan_prs(include_acked=True)[0]
        assert acked.acked_at is not None
        assert len(_wa_events(store, "orphan_pr_acked")) == 1

    def test_dispatching_an_unstarted_item_goes_through_the_queue_verb(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KEW_ACTOR", "ada")
        item = _tracker_item_open()
        before = get_work_view(store, "42", tracker_item=item, composed_at=WA_NOW)
        assert [a.action for a in before.actions] == ["dispatch"]

        with (
            patch("kew.service.verbs.run") as gh_list,
            patch("kew.service.verbs.mark_ready") as ready,
        ):
            gh_list.return_value = MagicMock(
                returncode=0, stdout=json.dumps([{"number": 42}]), stderr=""
            )
            result = verbs.perform_work_action(
                store,
                item_id="42",
                action="dispatch",
                expected_revision=before.view_revision,
                config=_wa_config(),
                tracker_item=item,
                now=WA_NOW,
            )

        assert result.applied is True
        ready.assert_called_once()
        assert len(_wa_events(store, "issue_queued")) == 1

    def test_a_publication_risk_hold_is_confirmed_for_the_job_it_names(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The confirmation is addressed to the job the projection read, so it
        cannot be given for a tree the job no longer publishes."""
        from kew.human_review import ReviewPolicy

        monkeypatch.setenv("KEW_ACTOR", "ada")
        policy = ReviewPolicy(delivery_repo="o/r", max_diff_lines=0, approvers=("ada",))
        config = _wa_config()
        config.human_review.max_diff_lines = 0
        # ``repo`` here is the LINEAGE source id — the item the job publishes
        # for — while the policy still governs the o/r delivery repository.
        job = _cleared_job(store, repo="42", policy=policy)
        store.reserve_publication_review(
            job_id=job.job_id, policy=policy, actor="ada", now=WA_NOW
        )
        before = _wa_view(store)
        assert before.exception.state == "publication_risk_hold"
        assert [(a.action, a.target_ref) for a in before.actions] == [
            ("confirm_publication_risk", job.job_id)
        ]

        result = verbs.perform_work_action(
            store,
            item_id="42",
            action="confirm_publication_risk",
            expected_revision=before.view_revision,
            config=config,
            now=WA_NOW,
        )

        assert result.applied is True
        assert store.publication_review(job.job_id).review_state == "confirmed"
        assert len(_wa_events(store, "publication_risk_confirmed")) == 1


# Issue #1931: immutable recovery transitions and operator abandonment.


def _abandon_verb():
    verb = getattr(verbs, "epic_abandon", None)
    assert callable(verb), "epic_abandon must exist at the service boundary"
    return verb


def test_unreadable_continuation_owner_records_release(store, tmp_path, monkeypatch):
    record = store.record_continuation(
        "epic:x",
        "42",
        "original-run",
        "agent/issue-42",
        tmp_path / "preserved",
        checkout_generation=fixture_checkout_generation(tmp_path / "preserved"),
    )
    before = store.read_chain_rows()
    with monkeypatch.context() as scoped:
        scoped.setattr(store, "get_continuation", lambda *args: None)
        with pytest.raises(verbs.EpicResumeDenied, match="ownership is unreadable"):
            verbs.epic_resume(store, epic_ref="epic:x", item_id="42", config=_config())
    releases = store.list_audit_events(event="continuation_released")
    assert len(releases) == 1
    payload = releases[0].payload
    assert payload["reason"] == "claimed_owner_unreadable"
    assert payload["generation"] == record.generation + 1
    assert payload["item_id"] == payload["node"] == "42"
    assert payload["epic_ref"] == "epic:x"
    assert payload["original_run_id"] == "original-run"
    assert payload["continuation_run_id"] == releases[0].run_id
    assert payload["worktree_identity"].startswith("sha256:")
    assert store.read_chain_rows()[: len(before)] == before
    assert verify_chain(store.read_chain_rows()).ok
    assert store.list_epic_reservations("epic:x") == []
    retried = verbs.epic_resume(
        store, epic_ref="epic:x", item_id="42", config=_config()
    )
    assert store.continuation_origins(retried.continuation_run_id) == ["original-run"]


def test_service_claim_unwind_records_matching_release(store, tmp_path):
    from kew.graph.driver.dispatch import resume_continuation
    from kew.overlap import DispatchOutcome

    store.record_continuation(
        "epic:x",
        "42",
        "original-run",
        "agent/issue-42",
        tmp_path / "preserved",
        checkout_generation=fixture_checkout_generation(tmp_path / "preserved"),
    )
    claimed = verbs.epic_resume(
        store, epic_ref="epic:x", item_id="42", config=_config()
    )
    claim = store.list_audit_events(event="continuation_claimed")[0]
    # This failure follows run creation and consumes its generation.
    store.save(
        RunRecord(
            run_id=claimed.continuation_run_id,
            issue_num=42,
            branch=claimed.branch,
            status="pending",
            exit_code=0,
            started_at=datetime.now(UTC),
        )
    )
    resume_continuation(
        claimed, store=store, dispatch=lambda *args: DispatchOutcome(exit_code=1)
    )
    releases = store.list_audit_events(event="continuation_released")
    assert len(releases) == 1
    for field in (
        "epic_ref",
        "item_id",
        "original_run_id",
        "generation",
        "branch",
        "worktree_identity",
        "continuation_run_id",
    ):
        assert releases[0].payload[field] == claim.payload[field]
    assert releases[0].payload["reason"] == "dispatch_exit_1"
    assert verify_chain(store.read_chain_rows()).ok


def test_unevidenced_claim_undo_also_attests_release(store, tmp_path, monkeypatch):
    store.record_continuation(
        "epic:x",
        "42",
        "original-run",
        "agent/issue-42",
        tmp_path / "preserved",
        checkout_generation=fixture_checkout_generation(tmp_path / "preserved"),
    )
    with monkeypatch.context() as legacy:
        legacy.setattr(store, "_attest_continuation_claim", lambda *args, **kw: None)
        claimed = verbs.epic_resume(
            store, epic_ref="epic:x", item_id="42", config=_config()
        )
    assert store.release_unevidenced_claim(
        claimed.epic_ref,
        claimed.item_id,
        claimed.original_run_id,
        claimed.branch,
        claimed.worktree_identity,
        claimed.generation,
        claimed.continuation_run_id,
    )
    releases = store.list_audit_events(event="continuation_released")
    assert len(releases) == 1
    assert releases[0].payload["outcome"] == "claim_undone"
    assert releases[0].payload["reason"] == "claim_evidence_not_recorded"
    monkeypatch.undo()
    retried = verbs.epic_resume(
        store, epic_ref="epic:x", item_id="42", config=_config()
    )
    assert store.continuation_origins(retried.continuation_run_id) == ["original-run"]
    # Late execution evidence for the undone owner must remain fail-closed,
    # even after another run has claimed the same restored generation.
    store.save(
        RunRecord(
            run_id=releases[0].run_id,
            issue_num=42,
            item_id="42",
            branch="agent/issue-42",
            status="failure",
            exit_code=1,
            started_at=datetime.now(UTC),
        )
    )
    with pytest.raises(RuntimeError, match="launched run"):
        store.continuation_origins(retried.continuation_run_id)


@pytest.mark.parametrize("item_id", ["42", "SFD-12"])
def test_epic_abandon_attests_and_releases_without_erasing_history(
    store, tmp_path, monkeypatch, item_id
):
    abandon = _abandon_verb()
    import kew.worktree as worktree_module
    from kew.worktree import claim_checkout, release_checkout_claim

    config = _config()
    monkeypatch.setenv("KEW_ACTOR", "ada")
    config.epic.approvers = ["ada"]
    worktree = tmp_path / "preserved"
    (worktree / ".git").mkdir(parents=True)
    (worktree / "edits.txt").write_text("preserved edits")
    store.save(
        RunRecord(
            run_id="original",
            issue_num=42,
            item_id=item_id,
            branch="agent/issue-42",
            status="failure",
            exit_code=1,
            started_at=datetime.now(UTC),
        )
    )
    store.record_continuation(
        "epic:x",
        item_id,
        "original",
        "agent/issue-42",
        worktree,
        checkout_generation=fixture_checkout_generation(worktree),
    )
    assert store.reserve_epic_node_item("epic:x", item_id, "original")
    before = store.read_chain_rows()
    claim_checkout(worktree)
    with patch.object(
        worktree_module, "release_checkout_claim", wraps=release_checkout_claim
    ) as release:
        abandon(
            store,
            epic_ref="epic:x",
            item_id=item_id,
            reason="cost breaker",
            config=config,
            # The CLI injects the claim guard; the service layer never imports
            # kew.worktree (import contract).
            checkout_guard=worktree_module.checkout_claim_guard,
        )
        release.assert_called_once_with(worktree)
    terminal = store.list_continuations("epic:x")[0]
    assert terminal.state == "terminal"
    assert terminal.generation == 0
    assert store.list_epic_reservations("epic:x") == []
    assert [row.item_id for row in store.list_epic_retries("epic:x")] == [item_id]
    assert (worktree / "edits.txt").read_text() == "preserved edits"
    events = store.list_audit_events(event="continuation_abandoned")
    assert len(events) == 1
    event = events[0]
    assert (event.actor_type, event.actor_id) == ("human", "ada")
    assert event.run_id == "original"
    assert event.payload["reason"] == "cost breaker"
    assert event.payload["generation"] == 0
    assert event.payload["item_id"] == event.payload["node"] == item_id
    assert event.payload["continuation_run_id"] is None
    assert event.payload["worktree_identity"].startswith("sha256:")
    assert store.continuation_was_abandoned(terminal)
    assert store.read_chain_rows()[: len(before)] == before
    assert verify_chain(store.read_chain_rows()).ok


@pytest.mark.parametrize("state", ["continuing", "terminal", "absent"])
def test_epic_abandon_refuses_nonawaiting_without_writes(store, tmp_path, state):
    abandon = _abandon_verb()
    args = ("epic:x", "42", "original", "agent/issue-42", tmp_path / "preserved")
    if state == "absent":
        store.append_epic_tick(
            epic_ref="epic:x",
            tick_ts=datetime.now(UTC).isoformat(),
            snapshot={"nodes": [{"id": "42"}]},
            actions={},
            snapshot_hash="known",
        )
    else:
        store.record_continuation(
            *args,
            checkout_generation=fixture_checkout_generation(args[4]),
        )
        assert (
            store.claim_continuation_under_cap(
                *args,
                "owner",
                0,
                claim_evidence=fixture_claim_evidence(store, *args, "owner"),
            )
            == "claimed"
        )
        if state == "terminal":
            assert store.finalize_continuation(*args, 1, "owner")
    before = (
        store.list_continuations("epic:x"),
        store.read_chain_rows(),
        store.list_epic_reservations("epic:x"),
    )
    with pytest.raises(verbs.EpicAbandonDenied):
        abandon(store, epic_ref="epic:x", item_id="42", reason="stop", config=_config())
    assert before == (
        store.list_continuations("epic:x"),
        store.read_chain_rows(),
        store.list_epic_reservations("epic:x"),
    )
    assert store.list_epic_retries("epic:x") == []


@pytest.mark.parametrize("reason", ["", " \n\t"])
def test_epic_abandon_refuses_empty_reason(store, reason):
    abandon = _abandon_verb()
    with pytest.raises(verbs.EpicAbandonDenied, match="non-empty"):
        abandon(store, epic_ref="epic:x", item_id="42", reason=reason, config=_config())
    assert store.read_chain_rows() == []


@pytest.mark.parametrize("target", ["epic", "node"])
def test_epic_abandon_rejects_unknown_target(store, tmp_path, target):
    abandon = _abandon_verb()
    store.record_continuation(
        "epic:x",
        "42",
        "original",
        "branch",
        tmp_path / "work",
        checkout_generation=fixture_checkout_generation(tmp_path / "work"),
    )
    before = store.read_chain_rows()
    with pytest.raises(LookupError, match=f"unknown {target}"):
        abandon(
            store,
            epic_ref="missing" if target == "epic" else "epic:x",
            item_id="99",
            reason="stop",
            config=_config(),
        )
    assert store.read_chain_rows() == before


@pytest.mark.parametrize("verb_name", ["epic_resume", "epic_abandon"])
@pytest.mark.parametrize("policy", ["epic", "approval"])
def test_continuation_verbs_require_operator_authority(
    store, tmp_path, monkeypatch, verb_name, policy
):
    verb = getattr(verbs, verb_name, None)
    assert callable(verb), f"{verb_name} must exist"
    monkeypatch.setenv("KEW_ACTOR", "unlisted")
    config = _config()
    getattr(config, policy).approvers = ["ada"]
    record = store.record_continuation(
        "epic:x",
        "42",
        "original",
        "branch",
        tmp_path / "work",
        checkout_generation=fixture_checkout_generation(tmp_path / "work"),
    )
    kwargs = {"reason": "stop"} if verb_name == "epic_abandon" else {}
    before = store.read_chain_rows()
    with pytest.raises(verbs.VerbDenied, match="authority"):
        verb(store, epic_ref="epic:x", item_id="42", config=config, **kwargs)
    assert store.list_continuations("epic:x") == [record]
    assert store.read_chain_rows() == before


def test_claim_recorder_refuses_run_id_reuse_before_writing(store, tmp_path):
    store.record_continuation(
        "epic:x",
        "42",
        "original",
        "branch",
        tmp_path / "work",
        checkout_generation=fixture_checkout_generation(tmp_path / "work"),
    )
    claimed = verbs.epic_resume(
        store, epic_ref="epic:x", item_id="42", config=_config()
    )
    before = store.read_chain_rows()
    refusal = verbs._claim_identity_refusal(
        store, replace(claimed, generation=2), claimed.continuation_run_id
    )
    assert "already in use" in refusal
    assert store.read_chain_rows() == before


def test_epic_abandon_event_failure_rolls_back_terminalization(
    store, tmp_path, monkeypatch
):
    abandon = _abandon_verb()
    from kew.worktree import checkout_claim_guard

    store.save(
        RunRecord(
            run_id="original",
            issue_num=42,
            branch="branch",
            status="failure",
            exit_code=1,
            started_at=datetime.now(UTC),
        )
    )
    record = store.record_continuation(
        "epic:x",
        "42",
        "original",
        "branch",
        tmp_path / "work",
        checkout_generation=fixture_checkout_generation(tmp_path / "work"),
    )
    store.reserve_epic_node_item("epic:x", "42", "original")

    def fail(*args, **kwargs):
        raise OSError("audit unavailable")

    monkeypatch.setattr(store, "_chained_insert_in_transaction", fail)
    before = store.read_chain_rows()
    with pytest.raises(OSError, match="audit unavailable"):
        abandon(
            store,
            epic_ref="epic:x",
            item_id="42",
            reason="stop",
            config=_config(),
            checkout_guard=checkout_claim_guard,
        )
    assert store.list_continuations("epic:x") == [record]
    assert len(store.list_epic_reservations("epic:x")) == 1
    assert store.list_epic_retries("epic:x") == []
    assert store.read_chain_rows() == before


@pytest.mark.parametrize("publication", ["canonical", "checkout"])
def test_epic_abandon_requires_checkout_guard(store, tmp_path, publication):
    config = _config()
    config.publication.publish = publication
    store.record_continuation(
        "epic:x",
        "42",
        "original",
        "branch",
        tmp_path / "work",
        checkout_generation=fixture_checkout_generation(tmp_path / "work"),
    )
    before = (store.list_continuations("epic:x"), store.read_chain_rows())
    with pytest.raises(verbs.EpicAbandonDenied, match="checkout guard is required"):
        verbs.epic_abandon(
            store, epic_ref="epic:x", item_id="42", reason="stop", config=config
        )
    assert before == (store.list_continuations("epic:x"), store.read_chain_rows())


def test_epic_abandon_probes_dead_owner_under_guard(store, tmp_path, monkeypatch):
    calls = []
    store.save(
        RunRecord(
            run_id="original",
            issue_num=42,
            branch="branch",
            status="failure",
            exit_code=1,
            started_at=datetime.now(UTC),
            pgid=12345,
        )
    )
    worktree = tmp_path / "work"
    store.record_continuation(
        "epic:x",
        "42",
        "original",
        "branch",
        worktree,
        checkout_generation=fixture_checkout_generation(worktree),
    )
    store.reserve_epic_node_item("epic:x", "42", "original")

    @contextmanager
    def guard(path):
        assert path == worktree
        calls.append("entered")
        try:
            yield
        finally:
            calls.append("exited")

    def dead(pgid, *identity):
        assert pgid == 12345
        assert calls == ["entered"]
        calls.append("probed")
        return False

    finalize = store.finalize_continuation

    def guarded_finalize(*args, **kwargs):
        calls.append("finalized")
        return finalize(*args, **kwargs)

    monkeypatch.setattr("kew.process_groups.owner_alive", dead)
    monkeypatch.setattr("kew.process_termination.attempt_is_dead", lambda _: True)
    monkeypatch.setattr(store, "finalize_continuation", guarded_finalize)
    verbs.epic_abandon(
        store,
        epic_ref="epic:x",
        item_id="42",
        reason="stop",
        config=_config(),
        checkout_guard=guard,
    )
    assert calls == ["entered", "probed", "finalized", "exited"]
    assert store.list_continuations("epic:x")[0].state == "terminal"
    assert store.list_epic_reservations("epic:x") == []


@pytest.mark.parametrize("publication", ["canonical", "checkout"])
def test_abandon_checks_every_released_child_owner(
    store, tmp_path, monkeypatch, publication
):
    import subprocess
    import sys
    from dataclasses import replace

    from support.process_identity import fixture_child_identity

    from kew import process_groups, worktree
    from kew.graph.driver.dispatch import resume_continuation
    from kew.overlap import DispatchOutcome

    config = _config()
    config.publication.publish = publication
    config.parallel.max_workers = 4
    checkout = tmp_path / "preserved"
    checkout.mkdir()
    (checkout / "edits").write_text("keep these bytes")
    original = RunRecord(
        run_id="original",
        issue_num=42,
        item_id="42",
        branch="agent/issue-42",
        status="failure",
        exit_code=1,
        started_at=datetime.now(UTC),
    )
    store.save(original)
    store.record_continuation(
        "epic:x",
        "42",
        "original",
        original.branch,
        checkout,
        checkout_generation=fixture_checkout_generation(checkout),
    )
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )
    fixture_child_identity(monkeypatch, process)
    try:
        for generation in range(2):
            claimed = verbs.epic_resume(
                store, epic_ref="epic:x", item_id="42", config=config
            )
            store.save(
                replace(
                    original,
                    run_id=claimed.continuation_run_id,
                    status="pending" if generation == 0 else "failure",
                    pgid=process.pid if generation == 0 else None,
                    owner_pid=process.pid if generation == 0 else None,
                    attempt_pgid=process.pid if generation == 0 else None,
                    **(
                        process_groups.identity_at_admission(process.pid)
                        if generation == 0
                        else {}
                    ),
                )
            )
            resume_continuation(
                claimed,
                store=store,
                dispatch=lambda *args: DispatchOutcome(exit_code=1),
            )
        record = store.list_continuations("epic:x")[0]
        assert record.continuation_run_id is None
        assert record.generation == 2
        before = (store.list_continuations("epic:x"), store.read_chain_rows())
        actual_probe = process_groups.owner_alive
        probes = []

        def probe(pgid, *identity, **kwargs):
            assert checkout.absolute() in worktree._checkout_claims
            probes.append(pgid)
            return actual_probe(pgid, *identity, **kwargs)

        monkeypatch.setattr(process_groups, "owner_alive", probe)
        with pytest.raises(verbs.EpicAbandonDenied, match="live or unproven owner"):
            verbs.epic_abandon(
                store,
                epic_ref="epic:x",
                item_id="42",
                reason="stop",
                config=config,
                checkout_guard=worktree.checkout_claim_guard,
            )
        assert probes == [process.pid]
        assert before == (store.list_continuations("epic:x"), store.read_chain_rows())
        process.terminate()
        process.wait(timeout=5)
        verbs.epic_abandon(
            store,
            epic_ref="epic:x",
            item_id="42",
            reason="stop",
            config=config,
            checkout_guard=worktree.checkout_claim_guard,
        )
        assert store.list_continuations("epic:x")[0].state == "terminal"
        assert (checkout / "edits").read_text() == "keep these bytes"
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)


def test_finalize_claims_the_head_of_a_run_the_tick_reconciled(store: Any) -> None:
    """A run the drive settled from its open PR can still be finalized (#3193).

    ``reconcile_awaiting_publication`` settles the run and claims no head (the
    drive never claims one), and finalize then refused the run as no longer
    awaiting publication — leaving the PR's head unbound for good. Finalize
    now makes the human claim for it, once.
    """
    from kew.graph.driver.facts import reconcile_awaiting_publication
    from kew.publication_credentials import recorded_head_producer

    _awaiting_run(store)
    head = "a" * 40
    assert reconcile_awaiting_publication(
        store,
        item_id="42",
        repo="o/r",
        pr_url="https://github.com/o/r/pull/70",
        observation="open_pr",
    )
    assert store.get("run-publication").status == "success"

    finalized = verbs.finalize_publication_run(
        store,
        run_id="run-publication",
        config=_config(),
        remote_head=lambda branch: head,
    )

    assert finalized.status == "success"
    assert (
        recorded_head_producer(store, branch="agent/issue-42", head_sha=head)
        == "run-publication"
    )
    attested = store.list_audit_events(event="publication_head_attested")
    assert [(e.run_id, e.actor_type) for e in attested] == [
        ("run-publication", "human")
    ]
    # Claimed once: the run is no longer finalizable.
    with pytest.raises(ValueError, match="not awaiting publication"):
        verbs.finalize_publication_run(
            store,
            run_id="run-publication",
            config=_config(),
            remote_head=lambda branch: head,
        )
    assert verify_chain(store.read_chain_rows()).ok


def test_finalize_of_a_reconciled_run_fails_when_no_head_was_claimed(
    store: Any,
) -> None:
    """Claiming is the whole repair for a reconciled run, so no claim fails it."""
    from kew.graph.driver.facts import reconcile_awaiting_publication

    _awaiting_run(store)
    reconcile_awaiting_publication(
        store, item_id="42", repo="o/r", pr_url=None, observation="open_pr"
    )

    def unavailable(branch: str) -> str:
        raise RuntimeError("origin unreachable")

    with pytest.raises(ValueError, match="origin unreachable"):
        verbs.finalize_publication_run(
            store, run_id="run-publication", config=_config(), remote_head=unavailable
        )
    assert store.list_audit_events(event="publication_head_attested") == []
    # A refused finalize is not a finalize: it leaves no event saying it was.
    assert store.list_audit_events(event="publication_finalized") == []

    # The run stays finalizable, and the next attempt that claims the head is
    # the one finalize event.
    verbs.finalize_publication_run(
        store,
        run_id="run-publication",
        config=_config(),
        remote_head=lambda branch: "a" * 40,
    )
    finalized = store.list_audit_events(event="publication_finalized")
    assert [e.payload["attested_head"] for e in finalized] == ["a" * 40]


def test_finalize_still_refuses_a_success_run_nothing_reconciled(store: Any) -> None:
    _awaiting_run(store)
    store.finalize_awaiting_publication("run-publication")

    with pytest.raises(ValueError, match="not awaiting publication"):
        verbs.finalize_publication_run(
            store,
            run_id="run-publication",
            config=_config(),
            remote_head=lambda branch: "a" * 40,
        )
