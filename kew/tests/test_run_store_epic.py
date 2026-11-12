import sqlite3
import threading
from datetime import UTC, datetime

import pytest
from support.busy_backoff import OneShotBackoff
from support.checkout_generations import (
    fixture_checkout_generation,
    fixture_claim_evidence,
)

from kew.audit import Actor, emit_continuation_evidence
from kew.run_store import (
    _NODE_PR_FACTS_OPEN,
    CURRENT_SCHEMA_VERSION,
    SETTLED_NODE_STATUSES,
    RunRecord,
    SchemaTooNewError,
    SqliteRunStore,
)


class CheckoutReplacedError(RuntimeError):
    """Test refusal raised when the captured checkout generation changes."""


def _pending_run(run_id: str, *, status="pending"):
    now = datetime.now(UTC)
    return RunRecord(
        run_id=run_id,
        issue_num=0,
        item_id="SFD-250",
        branch="agent/issue-sfd-250",
        status=status,
        exit_code=0,
        started_at=now,
        total_tokens=41,
        estimated_cost_usd=1.25,
        error_category="owner-detail",
    )


def _observe_managed_sql(store, monkeypatch):
    """Record completed managed SQL without using store calls in callbacks."""
    observed: list[str] = []
    original_execute = store._execute

    def execute(sql, params=None):
        cursor = original_execute(sql, params)
        normalized = " ".join(sql.split()).upper()
        if normalized.startswith("INSERT INTO EPIC_CONTINUATIONS"):
            observed.append("continuation-insert")
        elif normalized.startswith("SELECT EPIC_REF") and (
            "FROM EPIC_CONTINUATIONS" in normalized
        ):
            observed.append("continuation-readback")
        elif normalized.startswith("DELETE FROM EPIC_RESERVATIONS"):
            observed.append("reservation-delete")
        elif normalized.startswith("UPDATE EPIC_CONTINUATIONS SET"):
            observed.append("continuation-update")
        elif normalized.startswith("UPDATE RUNS SET STATUS = 'KILLED'"):
            observed.append("orphan-update")
        return cursor

    monkeypatch.setattr(store, "_execute", execute)
    return observed


@pytest.fixture
def store(tmp_path):
    s = SqliteRunStore(str(tmp_path / "dispatch.db"))
    yield s
    s.close()


def test_release_generation_conditional_spares_a_relive_reservation(store):
    """A generation-conditional release deletes only the run_id it names, so a
    reconciler holding a stale generation can't delete a live re-claimed one
    (#972 review Finding 2)."""
    assert store.reserve_epic_node_item("delegation", "SFD-1", "run-A") is True
    store.release_epic_node_item("SFD-1")  # owner path, unconditional
    assert store.reserve_epic_node_item("delegation", "SFD-1", "run-B") is True
    # A stale reconciler releasing run-A must not touch the live run-B.
    store.release_epic_node_item("SFD-1", run_id="run-A")
    assert [r.run_id for r in store.list_epic_reservations("delegation")] == ["run-B"]
    # The unconditional owner form still clears whatever generation is present.
    store.release_epic_node_item("SFD-1")
    assert store.list_epic_reservations("delegation") == []


def test_finalize_awaiting_publication_atomically_terminals_recovery_handle(
    store, tmp_path
):
    now = datetime.now(UTC)
    store.save(
        RunRecord(
            run_id="run-publication",
            issue_num=0,
            item_id="SFD-12",
            branch="agent/issue-sfd-12",
            status="awaiting_publication",
            exit_code=0,
            started_at=now,
            finished_at=now,
            error_category="publication_unavailable",
        )
    )
    store.record_continuation(
        "epic:x",
        "SFD-12",
        "original-implementation",
        "agent/issue-sfd-12",
        tmp_path / "preserved",
        checkout_generation=fixture_checkout_generation(tmp_path / "preserved"),
    )

    assert (
        store.finalize_awaiting_publication(
            "run-publication", pr_url="https://github.com/o/r/pull/77"
        )
        is True
    )

    run = store.get("run-publication")
    assert run is not None
    assert run.status == "success"
    assert run.error_category == ""
    assert run.pr_url == "https://github.com/o/r/pull/77"
    recovery = store.list_continuations("epic:x")
    assert len(recovery) == 1
    assert recovery[0].state == "terminal"
    assert recovery[0].terminal_at is not None
    assert store.finalize_awaiting_publication("run-publication") is False


def test_read_only_open_rejects_a_too_new_schema(tmp_path):
    """A read-only open (used by `kew doctor`) must reject a newer-schema DB via
    SchemaTooNewError instead of migrating it or crashing later (#972 review
    Finding 3)."""
    db = tmp_path / "dispatch.db"
    with SqliteRunStore(str(db)) as s:
        s._conn.execute(
            "UPDATE schema_version SET version = ? WHERE id = 1",
            (CURRENT_SCHEMA_VERSION + 1,),
        )
        s._conn.commit()
    with pytest.raises(SchemaTooNewError):
        SqliteRunStore(str(db), read_only=True)


def test_read_only_open_reads_without_migrating(tmp_path):
    """Read-only mode reads reservations and never writes (no schema mutation)."""
    db = tmp_path / "dispatch.db"
    with SqliteRunStore(str(db)) as s:
        s.reserve_epic_node_item("delegation", "SFD-9", "run-x")
    with SqliteRunStore(str(db), read_only=True) as ro:
        assert [r.item_id for r in ro.list_epic_reservations("delegation")] == ["SFD-9"]
        # A write against the read-only connection is refused by SQLite.
        import sqlite3

        with pytest.raises(sqlite3.OperationalError):
            ro._conn.execute("DELETE FROM epic_reservations")


def test_reserve_is_atomic_single_winner(store):
    assert store.reserve_epic_node("alpha", 42, "run-a") is True
    assert store.reserve_epic_node("alpha", 42, "run-b") is False  # second loses
    res = store.list_epic_reservations("alpha")
    assert [r.issue_num for r in res] == [42]
    assert res[0].run_id == "run-a"


def test_claim_continuation_under_cap_claims_record_and_reserves_slot(store, tmp_path):
    worktree = tmp_path / "preserved" / ".." / "preserved"
    store.record_continuation(
        "delegation",
        "SFD-1",
        "original-run",
        "agent/issue-sfd-1",
        worktree,
        checkout_generation=fixture_checkout_generation(worktree),
    )

    assert (
        store.claim_continuation_under_cap(
            "delegation",
            "SFD-1",
            "original-run",
            "agent/issue-sfd-1",
            worktree,
            "continuation-a",
            1,
            claim_evidence=fixture_claim_evidence(
                store,
                "delegation",
                "SFD-1",
                "original-run",
                "agent/issue-sfd-1",
                worktree,
                "continuation-a",
            ),
        )
        == "claimed"
    )

    claimed = store.get_continuation(
        "delegation",
        "SFD-1",
        "original-run",
        "agent/issue-sfd-1",
        worktree,
    )
    assert claimed is not None
    assert claimed.state == "continuing"
    assert claimed.generation == 1
    assert claimed.continuation_run_id == "continuation-a"
    assert [row.run_id for row in store.list_epic_reservations("delegation")] == [
        "continuation-a"
    ]


def test_continuation_cap_refusal_leaves_claim_unmodified(store, tmp_path):
    worktree = tmp_path / "preserved"
    store.record_continuation(
        "delegation",
        "SFD-1",
        "original-run",
        "agent/issue-sfd-1",
        worktree,
        checkout_generation=fixture_checkout_generation(worktree),
    )
    assert store.reserve_epic_node_item("delegation", "SFD-other", "other-run") is True

    assert (
        store.claim_continuation_under_cap(
            "delegation",
            "SFD-1",
            "original-run",
            "agent/issue-sfd-1",
            worktree,
            "continuation-a",
            1,
            claim_evidence=fixture_claim_evidence(
                store,
                "delegation",
                "SFD-1",
                "original-run",
                "agent/issue-sfd-1",
                worktree,
                "continuation-a",
            ),
        )
        == "fleet_full"
    )
    pending = store.get_continuation(
        "delegation",
        "SFD-1",
        "original-run",
        "agent/issue-sfd-1",
        worktree,
    )
    assert pending is not None
    assert pending.state == "awaiting_continuation"
    assert pending.generation == 0
    assert pending.continuation_run_id is None
    assert [row.item_id for row in store.list_epic_reservations("delegation")] == [
        "SFD-other"
    ]


def test_concurrent_continuation_claims_have_one_winner(tmp_path):
    db_path = str(tmp_path / "dispatch.db")
    worktree = tmp_path / "preserved"
    with SqliteRunStore(db_path) as seeded:
        seeded.record_continuation(
            "delegation",
            "SFD-1",
            "original-run",
            "agent/issue-sfd-1",
            worktree,
            checkout_generation=fixture_checkout_generation(worktree),
        )

    barrier = threading.Barrier(2)
    outcomes: list[str | Exception] = []
    outcomes_lock = threading.Lock()

    def worker(run_id: str) -> None:
        with SqliteRunStore(db_path) as store:
            try:
                barrier.wait(timeout=5)
                outcome = store.claim_continuation_under_cap(
                    "delegation",
                    "SFD-1",
                    "original-run",
                    "agent/issue-sfd-1",
                    worktree,
                    run_id,
                    1,
                    claim_evidence=fixture_claim_evidence(
                        store,
                        "delegation",
                        "SFD-1",
                        "original-run",
                        "agent/issue-sfd-1",
                        worktree,
                        run_id,
                    ),
                )
            except Exception as exc:  # pragma: no cover - diagnostic capture
                outcome = exc
            with outcomes_lock:
                outcomes.append(outcome)

    threads = [
        threading.Thread(target=worker, args=("continuation-a",)),
        threading.Thread(target=worker, args=("continuation-b",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert outcomes.count("claimed") == 1
    assert sum(outcome == "already_claimed" for outcome in outcomes) == 1
    with SqliteRunStore(db_path) as store:
        claimed = store.get_continuation(
            "delegation",
            "SFD-1",
            "original-run",
            "agent/issue-sfd-1",
            worktree,
        )
        assert claimed is not None
        assert claimed.state == "continuing"
        assert len(store.list_epic_reservations("delegation")) == 1


def test_concurrent_continuation_registration_is_idempotent(tmp_path):
    db_path = str(tmp_path / "dispatch.db")
    worktree = tmp_path / "preserved"
    SqliteRunStore(db_path).close()

    barrier = threading.Barrier(2)
    errors: list[Exception] = []

    def worker() -> None:
        try:
            with SqliteRunStore(db_path) as store:
                barrier.wait(timeout=5)
                store.record_continuation(
                    "delegation",
                    "SFD-1",
                    "original-run",
                    "agent/issue-sfd-1",
                    worktree,
                    checkout_generation=fixture_checkout_generation(worktree),
                )
        except Exception as exc:  # pragma: no cover - diagnostic capture
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    with SqliteRunStore(db_path) as store:
        records = store.list_continuations("delegation")
    assert len(records) == 1
    assert records[0].state == "awaiting_continuation"
    assert records[0].generation == 0


def test_continuation_registration_crash_rolls_back_both_writes(
    store, tmp_path, monkeypatch
):
    """A crash before commit leaves both the marker and temp reservation intact."""
    worktree = tmp_path / "preserved"
    assert store.reserve_epic_node_item("delegation", "SFD-1", "temporary") is True
    original_execute = store._execute

    def crash_after_release(sql, params=None):
        cursor = original_execute(sql, params)
        if sql.startswith("DELETE FROM epic_reservations"):
            raise KeyboardInterrupt("simulated process crash")
        return cursor

    monkeypatch.setattr(store, "_execute", crash_after_release)
    with pytest.raises(KeyboardInterrupt, match="simulated process crash"):
        store.register_continuation_and_release_reservation(
            "delegation",
            "SFD-1",
            "original-run",
            "agent/issue-sfd-1",
            worktree,
            "temporary",
            checkout_generation=fixture_checkout_generation(worktree),
        )

    assert store.list_continuations("delegation") == []
    assert [row.run_id for row in store.list_epic_reservations("delegation")] == [
        "temporary"
    ]


def test_concurrent_continuation_registration_releases_only_one_generation(
    tmp_path,
):
    """Interleaved registrars have one winner and cannot double-release."""
    db_path = str(tmp_path / "dispatch.db")
    worktree = tmp_path / "preserved"
    with SqliteRunStore(db_path) as seeded:
        assert seeded.reserve_epic_node_item("delegation", "SFD-1", "temporary") is True

    barrier = threading.Barrier(2)
    outcomes = []
    errors = []
    outcomes_lock = threading.Lock()

    def worker() -> None:
        try:
            with SqliteRunStore(db_path) as local:
                barrier.wait(timeout=5)
                outcome = local.register_continuation_and_release_reservation(
                    "delegation",
                    "SFD-1",
                    "original-run",
                    "agent/issue-sfd-1",
                    worktree,
                    "temporary",
                    checkout_generation=fixture_checkout_generation(worktree),
                )
            with outcomes_lock:
                outcomes.append(outcome)
        except Exception as exc:  # pragma: no cover - diagnostic capture
            with outcomes_lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    assert sum(outcome is not None for outcome in outcomes) == 1
    assert sum(outcome is None for outcome in outcomes) == 1
    with SqliteRunStore(db_path) as store:
        assert len(store.list_continuations("delegation")) == 1
        assert store.list_epic_reservations("delegation") == []


def test_stale_continuation_registration_cannot_release_reclaimed_generation(
    store, tmp_path
):
    """A stale registration rolls back and leaves a newer reservation live."""
    worktree = tmp_path / "preserved"
    assert store.reserve_epic_node_item("delegation", "SFD-1", "old") is True
    store.release_epic_node_item("SFD-1", run_id="old")
    assert store.reserve_epic_node_item("delegation", "SFD-1", "new") is True

    assert (
        store.register_continuation_and_release_reservation(
            "delegation",
            "SFD-1",
            "original-run",
            "agent/issue-sfd-1",
            worktree,
            "old",
            checkout_generation=fixture_checkout_generation(worktree),
        )
        is None
    )
    assert store.list_continuations("delegation") == []
    assert [row.run_id for row in store.list_epic_reservations("delegation")] == ["new"]


def _assert_continuation_version(row, state, generation, run_id):
    assert row is not None
    assert (row.state, row.generation, row.continuation_run_id) == (
        state,
        generation,
        run_id,
    )


def test_stale_continuation_writer_cannot_finalize_newer_generation(store, tmp_path):
    worktree = tmp_path / "preserved"
    store.record_continuation(
        "delegation",
        "SFD-1",
        "original-run",
        "agent/issue-sfd-1",
        worktree,
        checkout_generation=fixture_checkout_generation(worktree),
    )
    assert (
        store.claim_continuation_under_cap(
            "delegation",
            "SFD-1",
            "original-run",
            "agent/issue-sfd-1",
            worktree,
            "continuation-a",
            1,
            claim_evidence=fixture_claim_evidence(
                store,
                "delegation",
                "SFD-1",
                "original-run",
                "agent/issue-sfd-1",
                worktree,
                "continuation-a",
            ),
        )
        == "claimed"
    )
    first = store.get_continuation(
        "delegation",
        "SFD-1",
        "original-run",
        "agent/issue-sfd-1",
        worktree,
    )
    assert first is not None

    assert (
        store.mark_continuation_awaiting(
            "delegation",
            "SFD-1",
            "original-run",
            "agent/issue-sfd-1",
            worktree,
            first.generation,
            "continuation-a",
        )
        is True
    )
    assert (
        store.claim_continuation_under_cap(
            "delegation",
            "SFD-1",
            "original-run",
            "agent/issue-sfd-1",
            worktree,
            "continuation-b",
            1,
            claim_evidence=fixture_claim_evidence(
                store,
                "delegation",
                "SFD-1",
                "original-run",
                "agent/issue-sfd-1",
                worktree,
                "continuation-b",
            ),
        )
        == "claimed"
    )
    second = store.get_continuation(
        "delegation",
        "SFD-1",
        "original-run",
        "agent/issue-sfd-1",
        worktree,
    )
    assert second is not None
    assert second.generation > first.generation

    assert (
        store.finalize_continuation(
            "delegation",
            "SFD-1",
            "original-run",
            "agent/issue-sfd-1",
            worktree,
            first.generation,
            "continuation-a",
        )
        is False
    )
    current = store.get_continuation(
        "delegation",
        "SFD-1",
        "original-run",
        "agent/issue-sfd-1",
        worktree,
    )
    _assert_continuation_version(
        current, "continuing", second.generation, "continuation-b"
    )
    assert [row.run_id for row in store.list_epic_reservations("delegation")] == [
        "continuation-b"
    ]

    _finalize_successor_continuation(store, worktree, second)


def _finalize_successor_continuation(store, worktree, second):
    assert (
        store.finalize_continuation(
            "delegation",
            "SFD-1",
            "original-run",
            "agent/issue-sfd-1",
            worktree,
            second.generation,
            "continuation-b",
        )
        is True
    )
    terminal = store.get_continuation(
        "delegation",
        "SFD-1",
        "original-run",
        "agent/issue-sfd-1",
        worktree,
    )
    assert terminal is not None
    assert terminal.state == "terminal"
    assert store.list_epic_reservations("delegation") == []


def test_release_frees_the_node(store):
    store.reserve_epic_node("alpha", 42, "run-a")
    store.release_epic_node(42, run_id="run-a")
    assert store.list_epic_reservations("alpha") == []
    assert store.reserve_epic_node("alpha", 42, "run-c") is True  # reservable again


def test_hold_upsert_and_clear(store):
    store.hold_epic_node("alpha", 7, "ci_pending")
    first_held_at = store.list_epic_holds("alpha")[0].held_at
    store.hold_epic_node("alpha", 7, "ci_pending")  # idempotent upsert
    holds = store.list_epic_holds("alpha")
    assert [(h.issue_num, h.reason) for h in holds] == [(7, "ci_pending")]
    # held_at means "when the hold STARTED" -- a repeat call for the same
    # still-held node must preserve it, not refresh it (see
    # test_hold_epic_node_preserves_held_at_on_repeat_call for the
    # staleness-clock rationale).
    assert holds[0].held_at == first_held_at
    store.clear_epic_hold(7)
    assert store.list_epic_holds("alpha") == []


def test_remove_epic_hold_returns_and_deletes_atomically(store):
    store.hold_epic_node("alpha", 7, "ci_pending")
    removed = store.remove_epic_hold("alpha", "7")
    assert removed is not None
    assert (removed.issue_num, removed.reason, removed.item_id) == (
        7,
        "ci_pending",
        "7",
    )
    assert store.list_epic_holds("alpha") == []


def test_remove_epic_hold_restarts_the_review_cycle_for_a_named_reason(store):
    """#2154: releasing the round cap has to restart round numbering with it.

    The cap is read from the count, ``kew epic retry`` is the marker's only
    other writer, and retry refuses a held node — so a release that left the
    count standing would be re-HELD for the same reason by the driver's next
    review pass, in a race no second call can win. The reset therefore rides in
    the release's own transaction.
    """
    from kew.epic_holds import REVIEW_CYCLE_RESET_HOLD_REASONS

    store.hold_epic_node("alpha", 7, "review_rounds_exhausted")
    store.record_review_round("alpha", "7", head_sha="a" * 40, verdict="NEEDS_CHANGES")
    assert store.review_round_count("7") == 1

    removed = store.remove_epic_hold(
        "alpha", "7", fresh_review_cycle_reasons=REVIEW_CYCLE_RESET_HOLD_REASONS
    )

    assert removed is not None and removed.reason == "review_rounds_exhausted"
    assert store.review_round_count("7") == 0
    assert [r.item_id for r in store.list_epic_retries("alpha")] == ["7"]
    # Append-only history is untouched: the restart is a cycle boundary, not a
    # delete (same contract a retry's marker has).
    assert [r.round for r in store.list_review_rounds("7")] == [1]


def test_remove_epic_hold_leaves_the_cycle_alone_for_other_reasons(store):
    """The reset is opt-in per reason, and empty by default — the driver's own
    ``ci_pending`` release must stay a plain delete."""
    from kew.epic_holds import REVIEW_CYCLE_RESET_HOLD_REASONS

    store.hold_epic_node("alpha", 7, "ci_pending")
    store.record_review_round("alpha", "7", head_sha="a" * 40, verdict="NEEDS_CHANGES")

    assert (
        store.remove_epic_hold(
            "alpha", "7", fresh_review_cycle_reasons=REVIEW_CYCLE_RESET_HOLD_REASONS
        )
        is not None
    )
    assert store.review_round_count("7") == 1

    store.hold_epic_node("alpha", 7, "review_rounds_exhausted")
    assert store.remove_epic_hold("alpha", "7") is not None
    assert store.review_round_count("7") == 1
    assert store.list_epic_retries("alpha") == []


def test_remove_epic_hold_records_a_release_receipt_for_a_named_prefix(store):
    """#2192: a DERIVED hold needs its release recorded, for the same reason the
    round cap needs its reset — the pass that derived it runs again.

    The merge queue re-intersects the PR's unchanged diff with
    ``merge_gate_paths`` on every tick, so the delete alone is re-held before
    any separate write could land. The receipt is the released reason itself,
    written in the same transaction, and the head it carries is what the queue
    matches the PR against."""
    from kew.epic_holds import MERGE_GATE_TOUCHED_HOLD_PREFIX, merge_gate_ack_key

    reason = f"merge_gate_touched@{'a' * 40}: scripts/verify.sh"
    key = merge_gate_ack_key("alpha", "7")
    store.hold_epic_node("alpha", 7, reason)

    removed = store.remove_epic_hold(
        "alpha",
        "7",
        receipt_key=key,
        receipt_reason_prefixes=(MERGE_GATE_TOUCHED_HOLD_PREFIX,),
    )

    assert removed is not None and removed.reason == reason
    assert store.get_meta(key) == reason


def test_remove_epic_hold_records_no_receipt_for_another_reason(store):
    """Opt-in per reason and empty by default: no other release may leave the
    receipt that lets a gate-editing PR be verified."""
    from kew.epic_holds import MERGE_GATE_TOUCHED_HOLD_PREFIX, merge_gate_ack_key

    key = merge_gate_ack_key("alpha", "7")
    store.hold_epic_node("alpha", 7, "ci_pending")
    assert (
        store.remove_epic_hold(
            "alpha",
            "7",
            receipt_key=key,
            receipt_reason_prefixes=(MERGE_GATE_TOUCHED_HOLD_PREFIX,),
        )
        is not None
    )
    assert store.get_meta(key) is None

    # And the default vocabulary is empty, so an unnamed release records nothing.
    store.hold_epic_node("alpha", 7, f"merge_gate_touched@{'b' * 40}: kew.toml")
    assert store.remove_epic_hold("alpha", "7") is not None
    assert store.get_meta(key) is None


def test_remove_epic_hold_missing_returns_none(store):
    # No row for this epic/item, and a wrong-epic hold is not a match either.
    store.hold_epic_node("beta", 7, "ci_pending")
    assert store.remove_epic_hold("alpha", "7") is None
    assert [h.epic_ref for h in store.list_all_epic_holds()] == ["beta"]


def test_hold_epic_node_unless_writes_over_a_hold_keep_admits(store):
    """#2869: the conditional upsert writes a fresh hold and replaces one the
    writer owns, keeping when that hold started."""

    def other_writers(reason):
        return not reason.startswith("ci_pending")

    assert store.hold_epic_node_unless("alpha", "7", "ci_pending", keep=other_writers)
    started = store.list_epic_holds("alpha")[0].held_at

    assert store.hold_epic_node_unless(
        "alpha", "7", "ci_pending:dirty", keep=other_writers
    )

    (hold,) = store.list_epic_holds("alpha")
    assert (hold.reason, hold.held_at) == ("ci_pending:dirty", started)


def test_hold_epic_node_unless_never_replaces_a_hold_keep_names(store):
    store.hold_epic_node("alpha", 7, "base_integration_conflict: a.py")

    written = store.hold_epic_node_unless(
        "alpha", "7", "ci_pending:dirty", keep=lambda reason: True
    )

    assert written is False
    assert [h.reason for h in store.list_epic_holds("alpha")] == [
        "base_integration_conflict: a.py"
    ]


def test_hold_epic_node_unless_writes_its_meta_record_only_with_the_hold(store):
    """#3191: the record of why a hold was written stands or falls with it."""
    store.hold_epic_node("alpha", 7, "base_integration_conflict: a.py")

    assert not store.hold_epic_node_unless(
        "alpha", "7", "resource_retries_spent: x", keep=lambda r: True, meta=("k", "v")
    )
    assert store.get_meta("k") is None

    assert store.hold_epic_node_unless(
        "alpha", "8", "resource_retries_spent: x", keep=lambda r: True, meta=("k", "v")
    )
    assert store.get_meta("k") == "v"


def test_hold_epic_node_preserves_held_at_on_repeat_call(store):
    """held_at means "when the hold STARTED", not "last refreshed". The
    epic driver re-issues hold_epic_node every tick a still-CI-red PR is
    reclassified -- if held_at reset each time, a HELD node could never
    age past `stall_days` into "stuck" (the held-forever regression)."""
    store.hold_epic_node("alpha", 7, "ci_pending")
    original_held_at = store.list_epic_holds("alpha")[0].held_at

    # Same reason, later tick -- held_at must NOT move.
    store.hold_epic_node("alpha", 7, "ci_pending")
    holds = store.list_epic_holds("alpha")
    assert holds[0].held_at == original_held_at
    assert holds[0].reason == "ci_pending"

    # Reason changes (e.g. mergeability now known) -- reason updates but
    # held_at, the hold-start clock, still must NOT move.
    store.hold_epic_node("alpha", 7, "mergeability-unknown")
    holds = store.list_epic_holds("alpha")
    assert holds[0].held_at == original_held_at
    assert holds[0].reason == "mergeability-unknown"

    # A genuinely NEW hold, after the old one is cleared, gets a fresh
    # held_at via the plain INSERT path.
    store.clear_epic_hold(7)
    store.hold_epic_node("alpha", 7, "ci_pending")
    holds = store.list_epic_holds("alpha")
    assert holds[0].held_at != original_held_at


def test_mark_epic_retry_inserts_and_lists(store):
    store.mark_epic_retry("alpha", 42)
    retries = store.list_epic_retries("alpha")
    assert [r.issue_num for r in retries] == [42]
    assert retries[0].epic_ref == "alpha"
    assert retries[0].retried_at is not None


def test_mark_epic_retry_repeat_call_updates_retried_at(store):
    store.mark_epic_retry("alpha", 42)
    first_retried_at = store.list_epic_retries("alpha")[0].retried_at

    store.mark_epic_retry("alpha", 42)
    retries = store.list_epic_retries("alpha")

    # A later retry is a newer supersession point for the overlay, so
    # (unlike hold_epic_node's held_at) retried_at SHOULD move forward on
    # a repeat call, not stay pinned to the first retry.
    assert len(retries) == 1
    assert retries[0].retried_at >= first_retried_at


def test_precommit_callbacks_guard_all_four_successful_writes(store, tmp_path):
    """Every approved writer validates after its final mutation."""
    calls: list[str] = []
    worktree = tmp_path / "preserved"

    recorded = store.record_continuation(
        "delegation",
        "SFD-1",
        "original-record",
        "agent/issue-sfd-1",
        worktree,
        precommit_check=lambda: calls.append("record"),
        checkout_generation=fixture_checkout_generation(worktree),
    )
    assert recorded.state == "awaiting_continuation"

    assert store.reserve_epic_node_item("delegation", "SFD-2", "reservation")
    registered = store.register_continuation_and_release_reservation(
        "delegation",
        "SFD-2",
        "original-register",
        "agent/issue-sfd-2",
        worktree,
        "reservation",
        precommit_check=lambda: calls.append("register"),
        checkout_generation=fixture_checkout_generation(worktree),
    )
    assert registered is not None

    assert (
        store.claim_continuation_under_cap(
            "delegation",
            "SFD-1",
            "original-record",
            "agent/issue-sfd-1",
            worktree,
            "continuation",
            0,
            claim_evidence=fixture_claim_evidence(
                store,
                "delegation",
                "SFD-1",
                "original-record",
                "agent/issue-sfd-1",
                worktree,
                "continuation",
            ),
        )
        == "claimed"
    )
    assert store.mark_continuation_awaiting(
        "delegation",
        "SFD-1",
        "original-record",
        "agent/issue-sfd-1",
        worktree,
        1,
        "continuation",
        precommit_check=lambda: calls.append("mark"),
    )

    store.save(_pending_run("pending-success"))
    assert store.finalize_orphaned_pending(
        "pending-success",
        error_category="orphaned",
        finished_at=datetime.now(UTC),
        precommit_check=lambda: calls.append("finalize"),
    )
    assert calls == ["record", "register", "mark", "finalize"]


def test_record_continuation_refusal_rolls_back_last_mutation(
    store, tmp_path, monkeypatch
):
    worktree = tmp_path / "record-refusal"
    observed = _observe_managed_sql(store, monkeypatch)

    def refuse() -> None:
        assert observed == ["continuation-insert", "continuation-readback"]
        raise CheckoutReplacedError("checkout replaced")

    with pytest.raises(CheckoutReplacedError, match="checkout replaced"):
        store.record_continuation(
            "delegation",
            "SFD-3",
            "original",
            "agent/issue-sfd-3",
            worktree,
            precommit_check=refuse,
            checkout_generation=fixture_checkout_generation(worktree),
        )
    assert store.list_continuations("delegation") == []


def test_registration_refusal_restores_reservation_and_continuation(
    store, tmp_path, monkeypatch
):
    worktree = tmp_path / "registration-refusal"
    assert store.reserve_epic_node_item("delegation", "SFD-4", "reservation")
    observed = _observe_managed_sql(store, monkeypatch)

    def refuse() -> None:
        assert observed == [
            "continuation-insert",
            "continuation-readback",
            "reservation-delete",
        ]
        raise CheckoutReplacedError("checkout replaced")

    with pytest.raises(CheckoutReplacedError):
        store.register_continuation_and_release_reservation(
            "delegation",
            "SFD-4",
            "original",
            "agent/issue-sfd-4",
            worktree,
            "reservation",
            precommit_check=refuse,
            checkout_generation=fixture_checkout_generation(worktree),
        )
    assert store.list_continuations("delegation") == []
    assert [r.run_id for r in store.list_epic_reservations("delegation")] == [
        "reservation"
    ]


def test_mark_awaiting_refusal_restores_owned_generation(store, tmp_path, monkeypatch):
    worktree = tmp_path / "mark-refusal"
    store.record_continuation(
        "delegation",
        "SFD-5",
        "original",
        "agent/issue-sfd-5",
        worktree,
        checkout_generation=fixture_checkout_generation(worktree),
    )
    assert (
        store.claim_continuation_under_cap(
            "delegation",
            "SFD-5",
            "original",
            "agent/issue-sfd-5",
            worktree,
            "continuation",
            0,
            claim_evidence=fixture_claim_evidence(
                store,
                "delegation",
                "SFD-5",
                "original",
                "agent/issue-sfd-5",
                worktree,
                "continuation",
            ),
        )
        == "claimed"
    )
    observed = _observe_managed_sql(store, monkeypatch)

    def refuse() -> None:
        assert observed == ["continuation-update", "reservation-delete"]
        raise CheckoutReplacedError("checkout replaced")

    with pytest.raises(CheckoutReplacedError):
        store.mark_continuation_awaiting(
            "delegation",
            "SFD-5",
            "original",
            "agent/issue-sfd-5",
            worktree,
            1,
            "continuation",
            precommit_check=refuse,
        )
    current = store.get_continuation(
        "delegation", "SFD-5", "original", "agent/issue-sfd-5", worktree
    )
    assert current is not None and current.state == "continuing"
    assert current.continuation_run_id == "continuation"
    assert [r.run_id for r in store.list_epic_reservations("delegation")] == [
        "continuation"
    ]


def test_orphan_refusal_restores_pending_fields_and_telemetry(store, monkeypatch):
    store.save(_pending_run("pending-refusal"))
    observed = _observe_managed_sql(store, monkeypatch)

    def refuse() -> None:
        assert observed == ["orphan-update"]
        raise CheckoutReplacedError("checkout replaced")

    with pytest.raises(CheckoutReplacedError):
        store.finalize_orphaned_pending(
            "pending-refusal",
            error_category="orphaned",
            finished_at=datetime.now(UTC),
            duration_seconds=9.0,
            precommit_check=refuse,
        )
    restored = store.get("pending-refusal")
    assert restored is not None
    assert restored.status == "pending"
    assert restored.error_category == "owner-detail"
    assert restored.finished_at is None
    assert restored.duration_seconds == 0.0
    assert restored.total_tokens == 41
    assert restored.estimated_cost_usd == 1.25


def test_lost_registration_validates_and_preserves_new_owner(store, tmp_path):
    worktree = tmp_path / "lost-registration"
    assert store.reserve_epic_node_item("delegation", "SFD-6", "new-owner")
    calls = 0

    def stable() -> None:
        nonlocal calls
        calls += 1

    assert (
        store.register_continuation_and_release_reservation(
            "delegation",
            "SFD-6",
            "original",
            "agent/issue-sfd-6",
            worktree,
            "old-owner",
            precommit_check=stable,
            checkout_generation=fixture_checkout_generation(worktree),
        )
        is None
    )
    assert calls == 1
    assert store.list_continuations("delegation") == []
    assert [r.run_id for r in store.list_epic_reservations("delegation")] == [
        "new-owner"
    ]

    with pytest.raises(CheckoutReplacedError):
        store.register_continuation_and_release_reservation(
            "delegation",
            "SFD-6",
            "original",
            "agent/issue-sfd-6",
            worktree,
            "old-owner",
            precommit_check=lambda: (_ for _ in ()).throw(
                CheckoutReplacedError("checkout replaced")
            ),
            checkout_generation=fixture_checkout_generation(worktree),
        )
    assert store.list_continuations("delegation") == []
    assert [r.run_id for r in store.list_epic_reservations("delegation")] == [
        "new-owner"
    ]


def test_lost_mark_validates_and_preserves_new_generation(store, tmp_path):
    worktree = tmp_path / "lost-mark"
    store.record_continuation(
        "delegation",
        "SFD-7",
        "original",
        "agent/issue-sfd-7",
        worktree,
        checkout_generation=fixture_checkout_generation(worktree),
    )
    assert (
        store.claim_continuation_under_cap(
            "delegation",
            "SFD-7",
            "original",
            "agent/issue-sfd-7",
            worktree,
            "first",
            0,
            claim_evidence=fixture_claim_evidence(
                store,
                "delegation",
                "SFD-7",
                "original",
                "agent/issue-sfd-7",
                worktree,
                "first",
            ),
        )
        == "claimed"
    )
    assert store.mark_continuation_awaiting(
        "delegation",
        "SFD-7",
        "original",
        "agent/issue-sfd-7",
        worktree,
        1,
        "first",
    )
    assert (
        store.claim_continuation_under_cap(
            "delegation",
            "SFD-7",
            "original",
            "agent/issue-sfd-7",
            worktree,
            "second",
            0,
            claim_evidence=fixture_claim_evidence(
                store,
                "delegation",
                "SFD-7",
                "original",
                "agent/issue-sfd-7",
                worktree,
                "second",
            ),
        )
        == "claimed"
    )
    calls = 0

    def stable() -> None:
        nonlocal calls
        calls += 1

    assert not store.mark_continuation_awaiting(
        "delegation",
        "SFD-7",
        "original",
        "agent/issue-sfd-7",
        worktree,
        1,
        "first",
        precommit_check=stable,
    )
    assert calls == 1
    with pytest.raises(CheckoutReplacedError):
        store.mark_continuation_awaiting(
            "delegation",
            "SFD-7",
            "original",
            "agent/issue-sfd-7",
            worktree,
            1,
            "first",
            precommit_check=lambda: (_ for _ in ()).throw(
                CheckoutReplacedError("checkout replaced")
            ),
        )
    current = store.get_continuation(
        "delegation", "SFD-7", "original", "agent/issue-sfd-7", worktree
    )
    _assert_continuation_version(current, "continuing", 2, "second")
    assert [r.run_id for r in store.list_epic_reservations("delegation")] == ["second"]


def test_lost_orphan_finalization_validates_and_preserves_winner(tmp_path):
    db = tmp_path / "dispatch.db"
    with SqliteRunStore(str(db)) as stale, SqliteRunStore(str(db)) as winner:
        stale.save(_pending_run("pending-lost"))
        assert winner.transition_run_status(
            "pending-lost",
            expected_status="pending",
            new_status="success",
            error_category="winner-detail",
            pr_url="https://example.test/pr/1",
        )
        calls = 0

        def stable() -> None:
            nonlocal calls
            calls += 1

        assert not stale.finalize_orphaned_pending(
            "pending-lost",
            error_category="orphaned",
            finished_at=datetime.now(UTC),
            precommit_check=stable,
        )
        assert calls == 1
        with pytest.raises(CheckoutReplacedError):
            stale.finalize_orphaned_pending(
                "pending-lost",
                error_category="orphaned",
                finished_at=datetime.now(UTC),
                precommit_check=lambda: (_ for _ in ()).throw(
                    CheckoutReplacedError("checkout replaced")
                ),
            )
        preserved = winner.get("pending-lost")
        assert preserved is not None
        assert preserved.status == "success"
        assert preserved.error_category == "winner-detail"
        assert preserved.pr_url == "https://example.test/pr/1"
        assert preserved.total_tokens == 41


def test_idempotent_and_terminal_continuations_still_validate(store, tmp_path):
    worktree = tmp_path / "idempotent"
    store.save(_pending_run("successful-original", status="success"))
    calls = 0

    def stable() -> None:
        nonlocal calls
        calls += 1

    first = store.record_continuation(
        "delegation",
        "SFD-8",
        "successful-original",
        "agent/issue-sfd-8",
        worktree,
        precommit_check=stable,
        checkout_generation=fixture_checkout_generation(worktree),
    )
    second = store.record_continuation(
        "delegation",
        "SFD-8",
        "successful-original",
        "agent/issue-sfd-8",
        worktree,
        precommit_check=stable,
        checkout_generation=fixture_checkout_generation(worktree),
    )
    assert first == second
    assert first.state == "terminal"
    assert calls == 2


def test_idempotent_awaiting_and_continuing_rows_still_validate(store, tmp_path):
    worktree = tmp_path / "idempotent-active"
    calls: list[str] = []
    store.record_continuation(
        "delegation",
        "SFD-81",
        "original",
        "agent/issue-sfd-81",
        worktree,
        checkout_generation=fixture_checkout_generation(worktree),
    )
    awaiting = store.record_continuation(
        "delegation",
        "SFD-81",
        "original",
        "agent/issue-sfd-81",
        worktree,
        precommit_check=lambda: calls.append("awaiting"),
        checkout_generation=fixture_checkout_generation(worktree),
    )
    assert awaiting.state == "awaiting_continuation"
    assert (
        store.claim_continuation_under_cap(
            "delegation",
            "SFD-81",
            "original",
            "agent/issue-sfd-81",
            worktree,
            "continuation",
            0,
            claim_evidence=fixture_claim_evidence(
                store,
                "delegation",
                "SFD-81",
                "original",
                "agent/issue-sfd-81",
                worktree,
                "continuation",
            ),
        )
        == "claimed"
    )
    continuing = store.record_continuation(
        "delegation",
        "SFD-81",
        "original",
        "agent/issue-sfd-81",
        worktree,
        precommit_check=lambda: calls.append("continuing"),
        checkout_generation=fixture_checkout_generation(worktree),
    )
    assert continuing.state == "continuing"
    assert continuing.generation == 1
    assert continuing.continuation_run_id == "continuation"
    assert calls == ["awaiting", "continuing"]


@pytest.mark.parametrize("error", [RuntimeError("ordinary"), KeyboardInterrupt()])
def test_callback_baseexceptions_rollback_and_leave_connection_usable(
    store, tmp_path, error
):
    with pytest.raises(type(error), match=str(error) or None):
        store.record_continuation(
            "delegation",
            "SFD-9",
            "original",
            "agent/issue-sfd-9",
            tmp_path / "interrupt",
            precommit_check=lambda: (_ for _ in ()).throw(error),
            checkout_generation=fixture_checkout_generation(tmp_path / "interrupt"),
        )
    assert store.list_continuations("delegation") == []
    recovered = store.record_continuation(
        "delegation",
        "SFD-10",
        "original",
        "agent/issue-sfd-10",
        tmp_path / "usable",
        checkout_generation=fixture_checkout_generation(tmp_path / "usable"),
    )
    assert recovered.item_id == "SFD-10"


def test_callback_sqlite_locked_error_is_not_retried(store, tmp_path):
    calls = 0

    def refuse() -> None:
        nonlocal calls
        calls += 1
        raise sqlite3.OperationalError("database is locked by checkout validation")

    with pytest.raises(sqlite3.OperationalError, match="checkout validation"):
        store.record_continuation(
            "delegation",
            "SFD-11",
            "original",
            "agent/issue-sfd-11",
            tmp_path / "sqlite-looking",
            precommit_check=refuse,
            checkout_generation=fixture_checkout_generation(
                tmp_path / "sqlite-looking"
            ),
        )
    assert calls == 1
    assert store.list_continuations("delegation") == []


class _CommitOnceLockedConnection:
    def __init__(self, connection):
        self._connection = connection
        self.remaining_failures = 1

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def commit(self):
        if self.remaining_failures:
            self.remaining_failures -= 1
            raise sqlite3.OperationalError("database is locked")
        return self._connection.commit()


def test_busy_retry_revalidates_after_validated_attempt_loses_transaction(
    store, tmp_path, monkeypatch
):
    real_connection = store._conn
    store._conn = _CommitOnceLockedConnection(real_connection)
    calls = 0

    def stable() -> None:
        nonlocal calls
        calls += 1

    def txn_lock_released() -> bool:
        acquired = store._txn_lock.acquire(blocking=False)
        if acquired:
            store._txn_lock.release()
        return acquired

    backoff = OneShotBackoff(txn_lock_released)
    monkeypatch.setattr(store, "busy_sleep", backoff)

    recorded = store.record_continuation(
        "delegation",
        "SFD-12",
        "original",
        "agent/issue-sfd-12",
        tmp_path / "retry",
        precommit_check=stable,
        checkout_generation=fixture_checkout_generation(tmp_path / "retry"),
    )
    assert recorded.item_id == "SFD-12"
    assert calls == 2
    assert backoff.outcomes == [True]


def test_replacement_between_busy_attempts_refuses_second_attempt(store, tmp_path):
    real_connection = store._conn
    store._conn = _CommitOnceLockedConnection(real_connection)
    calls = 0

    def replace_on_retry() -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise CheckoutReplacedError("checkout replaced between attempts")

    with pytest.raises(CheckoutReplacedError, match="between attempts"):
        store.record_continuation(
            "delegation",
            "SFD-13",
            "original",
            "agent/issue-sfd-13",
            tmp_path / "retry-refusal",
            precommit_check=replace_on_retry,
            checkout_generation=fixture_checkout_generation(tmp_path / "retry-refusal"),
        )
    assert calls == 2
    assert store.list_continuations("delegation") == []


def test_retry_rereads_ownership_and_preserves_new_generation(
    store, tmp_path, monkeypatch
):
    worktree = tmp_path / "ownership-retry"
    store.record_continuation(
        "delegation",
        "SFD-131",
        "original",
        "agent/issue-sfd-131",
        worktree,
        checkout_generation=fixture_checkout_generation(worktree),
    )
    assert (
        store.claim_continuation_under_cap(
            "delegation",
            "SFD-131",
            "original",
            "agent/issue-sfd-131",
            worktree,
            "first",
            0,
            claim_evidence=fixture_claim_evidence(
                store,
                "delegation",
                "SFD-131",
                "original",
                "agent/issue-sfd-131",
                worktree,
                "first",
            ),
        )
        == "claimed"
    )
    real_connection = store._conn
    store._conn = _CommitOnceLockedConnection(real_connection)
    calls = 0

    def stable() -> None:
        nonlocal calls
        calls += 1

    def claim_new_generation() -> tuple[bool, str]:
        with SqliteRunStore(store._db_path) as actor:
            released = actor.mark_continuation_awaiting(
                "delegation",
                "SFD-131",
                "original",
                "agent/issue-sfd-131",
                worktree,
                1,
                "first",
            )
            claimed = actor.claim_continuation_under_cap(
                "delegation",
                "SFD-131",
                "original",
                "agent/issue-sfd-131",
                worktree,
                "second",
                0,
                claim_evidence=fixture_claim_evidence(
                    actor,
                    "delegation",
                    "SFD-131",
                    "original",
                    "agent/issue-sfd-131",
                    worktree,
                    "second",
                ),
            )
            return released, claimed

    backoff = OneShotBackoff(claim_new_generation)
    monkeypatch.setattr(store, "busy_sleep", backoff)
    assert not store.mark_continuation_awaiting(
        "delegation",
        "SFD-131",
        "original",
        "agent/issue-sfd-131",
        worktree,
        1,
        "first",
        precommit_check=stable,
    )
    assert backoff.outcomes == [(True, "claimed")]
    assert len(backoff.delays) == 1
    assert calls == 2
    current = store.get_continuation(
        "delegation", "SFD-131", "original", "agent/issue-sfd-131", worktree
    )
    assert current is not None
    assert current.generation == 2
    assert current.continuation_run_id == "second"
    assert [r.run_id for r in store.list_epic_reservations("delegation")] == ["second"]


#: How long a wait between threads may take before the test calls it a hang.
#: Only a bound on a test that would otherwise never end: no assert below
#: reads how long anything took.
_HANG_GUARD_SECONDS = 60


def test_real_connection_contention_precedes_validation(tmp_path, monkeypatch):
    db = tmp_path / "dispatch.db"
    with (
        SqliteRunStore(str(db)) as blocker,
        SqliteRunStore(str(db), check_same_thread=False) as writer,
    ):
        blocker._conn.execute("BEGIN IMMEDIATE")
        writer._conn.execute("PRAGMA busy_timeout=0")
        callback_called = threading.Event()
        busy_observed = threading.Event()
        retry_allowed = threading.Event()
        errors: list[BaseException] = []

        def observe_backoff() -> tuple[bool, bool]:
            # What the back-off saw: had validation run before the contention
            # was met, and was the retry let go (False only on the hang guard).
            validated_first = callback_called.is_set()
            busy_observed.set()
            return validated_first, retry_allowed.wait(_HANG_GUARD_SECONDS)

        backoff = OneShotBackoff(observe_backoff)
        monkeypatch.setattr(writer, "busy_sleep", backoff)

        def write() -> None:
            try:
                writer.record_continuation(
                    "delegation",
                    "SFD-14",
                    "original",
                    "agent/issue-sfd-14",
                    tmp_path / "contended",
                    precommit_check=callback_called.set,
                    checkout_generation=fixture_checkout_generation(
                        tmp_path / "contended"
                    ),
                )
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=write, daemon=True)
        thread.start()
        assert busy_observed.wait(_HANG_GUARD_SECONDS), (
            "hang guard: the writer never backed off from the held lock"
        )
        blocker._conn.rollback()
        retry_allowed.set()
        thread.join(_HANG_GUARD_SECONDS)
        assert not thread.is_alive(), "hang guard: the retried write never returned"
        assert errors == []
        assert backoff.outcomes == [(False, True)]
        assert len(backoff.delays) == 1
        assert callback_called.is_set()


class _RollbackFailConnection:
    def __init__(self, connection):
        self._connection = connection
        self.rollback_calls = 0
        self.begin_calls = 0

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def execute(self, sql, params=()):
        if sql == "BEGIN IMMEDIATE":
            self.begin_calls += 1
        return self._connection.execute(sql, params)

    def rollback(self):
        self.rollback_calls += 1
        raise sqlite3.OperationalError("database is locked during rollback")


def test_callback_rollback_failure_is_explicit(store, tmp_path):
    from kew import run_store

    original = CheckoutReplacedError("checkout replaced")
    failing_connection = _RollbackFailConnection(store._conn)
    store._conn = failing_connection
    with pytest.raises(run_store._PrecommitRollbackError) as caught:
        store.record_continuation(
            "delegation",
            "SFD-15",
            "original",
            "agent/issue-sfd-15",
            tmp_path / "rollback-failure",
            precommit_check=lambda: (_ for _ in ()).throw(original),
            checkout_generation=fixture_checkout_generation(
                tmp_path / "rollback-failure"
            ),
        )
    assert caught.value.__cause__ is original
    assert "locked during rollback" in str(caught.value)
    assert failing_connection.rollback_calls == 1
    assert failing_connection.begin_calls == 1


def _assert_lost_rollback_failure(caught, connection) -> None:
    from kew import run_store

    assert isinstance(caught.value.__cause__, run_store._PrecommitRollbackError)
    assert isinstance(caught.value.__cause__.__cause__, sqlite3.OperationalError)
    assert "locked" in str(caught.value.__cause__.__cause__)
    assert connection.begin_calls == 1


def test_lost_registration_rollback_failure_is_not_retried(store, tmp_path):
    from kew import run_store

    assert store.reserve_epic_node_item("delegation", "SFD-16", "new-owner")
    connection = _RollbackFailConnection(store._conn)
    store._conn = connection
    with pytest.raises(run_store._PrecommitRollbackError) as caught:
        store.register_continuation_and_release_reservation(
            "delegation",
            "SFD-16",
            "original",
            "agent/issue-sfd-16",
            tmp_path / "lost-registration-rollback",
            "old-owner",
            checkout_generation=fixture_checkout_generation(
                tmp_path / "lost-registration-rollback"
            ),
        )
    _assert_lost_rollback_failure(caught, connection)


def test_lost_mark_rollback_failure_is_not_retried(store, tmp_path):
    from kew import run_store

    worktree = tmp_path / "lost-mark-rollback"
    store.record_continuation(
        "delegation",
        "SFD-17",
        "original",
        "agent/issue-sfd-17",
        worktree,
        checkout_generation=fixture_checkout_generation(worktree),
    )
    connection = _RollbackFailConnection(store._conn)
    store._conn = connection
    with pytest.raises(run_store._PrecommitRollbackError) as caught:
        store.mark_continuation_awaiting(
            "delegation",
            "SFD-17",
            "original",
            "agent/issue-sfd-17",
            worktree,
            99,
            "lost-owner",
        )
    _assert_lost_rollback_failure(caught, connection)


def test_lost_orphan_rollback_failure_is_not_retried(store):
    from kew import run_store

    store.save(_pending_run("terminal-before-reap", status="success"))
    connection = _RollbackFailConnection(store._conn)
    store._conn = connection
    with pytest.raises(run_store._PrecommitRollbackError) as caught:
        store.finalize_orphaned_pending(
            "terminal-before-reap",
            error_category="orphaned",
            finished_at=datetime.now(UTC),
        )
    _assert_lost_rollback_failure(caught, connection)


def test_continuation_run_id_in_use_checks_every_identity_location(store, tmp_path):
    """A run row, a mutable owner, or claim evidence each make an id used."""
    assert store.continuation_run_id_in_use("fresh") is False

    store.save(
        RunRecord(
            run_id="row",
            issue_num=1,
            item_id="1",
            branch="agent/issue-1",
            status="success",
            exit_code=0,
            started_at=datetime.now(UTC),
        )
    )
    assert store.continuation_run_id_in_use("row") is True

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
            "owner",
            10,
            claim_evidence=fixture_claim_evidence(
                store,
                record.epic_ref,
                record.item_id,
                record.original_run_id,
                record.branch,
                record.worktree_identity,
                "owner",
            ),
        )
        == "claimed"
    )
    assert store.continuation_run_id_in_use("owner") is True

    store.reserve_epic_node_item("epic:y", "7", "reserved")
    assert store.continuation_run_id_in_use("reserved") is True

    emit_continuation_evidence(
        store,
        "continuation_claimed",
        actor=Actor("policy", "test"),
        phase="claim",
        epic_ref="epic:x",
        item_id="43",
        original_run_id="original-43",
        continuation_run_id="claimed",
        branch="agent/issue-43",
        worktree_identity=tmp_path / "other",
        generation=1,
        state="continuing",
        outcome="continuing",
        run_id="claimed",
    )
    assert store.continuation_run_id_in_use("claimed") is True
    assert store.continuation_run_id_in_use("fresh") is False


def test_the_audit_run_id_index_is_additive_and_used(store):
    """An index over an existing column needs no schema version: it is created
    on every open, so an already-migrated ledger gains it too."""
    from kew.run_store import CURRENT_SCHEMA_VERSION

    # The index itself never moves the schema version. 73 = v58 review_mode
    # (#2111), v59 accepted_verdict (#2151), v60 item_plans (#2290),
    # v61 triage_decisions (#2293), v62 the driver's expected cadence, v63
    # its lock snapshot (#2297), v64 the epic_tick_log (#2304), v65 the prompt's
    # self-check section (#2454), v66 a run's fleet placement (#2398), v67
    # the acceptance_contracts table (#2726), v68 its plan binding (#2774) and
    # v69 the boot a run's owner ran under (#2812), v70 the
    # epic_merge_state_reads table (#2879), and v71 the merge_queue table
    # (#2814). v72 binds a host-restart retry marker to its run (#2969).
    # v73 adds the queue stop's kind and failure (#2883).
    # v74 adds prompt profiles (#3020).
    # v75 marks the journaled tick attempt (#2949); v76 adds merge claims (#3198).
    assert CURRENT_SCHEMA_VERSION == 77
    names = {
        row["name"] for row in store._conn.execute("PRAGMA index_list('audit_events')")
    }
    assert "idx_audit_events_run_id" in names
    plan = store._conn.execute(
        "EXPLAIN QUERY PLAN SELECT 1 FROM audit_events"
        " WHERE event = 'continuation_claimed' AND run_id = ? LIMIT 1",
        ("x",),
    ).fetchall()
    assert any("idx_audit_events_run_id" in str(tuple(row)) for row in plan), plan


def test_release_unevidenced_claim_undoes_exactly_one_claim(store, tmp_path):
    record = store.record_continuation(
        "epic:x",
        "42",
        "original-run",
        "agent/issue-42",
        tmp_path / "wt",
        checkout_generation=fixture_checkout_generation(tmp_path / "wt"),
    )
    args = (record.epic_ref, record.item_id, record.original_run_id, record.branch)
    assert (
        store.claim_continuation_under_cap(
            *args,
            record.worktree_identity,
            "c1",
            10,
            claim_evidence=fixture_claim_evidence(
                store, *args, record.worktree_identity, "c1"
            ),
        )
        == "claimed"
    )
    claimed = store.get_continuation(*args, record.worktree_identity)
    assert claimed is not None and claimed.generation == record.generation + 1

    # Wrong generation or wrong owner: nothing changes.
    assert (
        store.release_unevidenced_claim(
            *args, record.worktree_identity, claimed.generation + 1, "c1"
        )
        is False
    )
    assert (
        store.release_unevidenced_claim(
            *args, record.worktree_identity, claimed.generation, "other"
        )
        is False
    )
    unchanged = store.get_continuation(*args, record.worktree_identity)
    _assert_continuation_version(unchanged, "continuing", claimed.generation, "c1")

    assert (
        store.release_unevidenced_claim(
            *args, record.worktree_identity, claimed.generation, "c1"
        )
        is True
    )
    undone = store.get_continuation(*args, record.worktree_identity)
    assert undone is not None
    assert (undone.state, undone.generation, undone.continuation_run_id) == (
        "awaiting_continuation",
        record.generation,
        None,
    )
    assert store.continuation_run_id_in_use("c1") is True, (
        "immutable claim evidence remains"
    )
    assert store.list_epic_reservations("epic:x") == []
    # Released once; a second release finds no owner.
    assert (
        store.release_unevidenced_claim(
            *args, record.worktree_identity, claimed.generation, "c1"
        )
        is False
    )


# Issue #1931: released and never-launched generations are transparent.


def _recovery_family(store, tmp_path):
    args = ("epic:x", "42", "original", "agent/issue-42", tmp_path / "preserved")
    store.record_continuation(
        *args,
        checkout_generation=fixture_checkout_generation(args[4]),
    )
    return args


def _recovery_claim(store, args, run_id, *, attest=True):
    from unittest.mock import patch

    # Explicitly model pre-atomic writers for legacy evidence-gap coverage.
    if not attest:
        with patch.object(store, "_attest_continuation_claim"):
            assert (
                store.claim_continuation_under_cap(
                    *args,
                    run_id,
                    0,
                    claim_evidence=fixture_claim_evidence(store, *args, run_id),
                )
                == "claimed"
            )
    else:
        assert (
            store.claim_continuation_under_cap(
                *args,
                run_id,
                0,
                claim_evidence=fixture_claim_evidence(store, *args, run_id),
            )
            == "claimed"
        )
    record = store.get_continuation(*args)
    if attest:
        emit_continuation_evidence(
            store,
            "continuation_claimed",
            actor=Actor("policy", "test"),
            phase="claim",
            epic_ref=record.epic_ref,
            item_id=record.item_id,
            original_run_id=record.original_run_id,
            branch=record.branch,
            worktree_identity=record.worktree_identity,
            generation=record.generation,
            continuation_run_id=run_id,
            run_id=run_id,
            state="continuing",
            outcome="continuing",
        )
    return record


def test_released_claimless_generation_is_transparent(store, tmp_path):
    args = _recovery_family(store, tmp_path)
    _recovery_claim(store, args, "never-created", attest=False)
    assert store.mark_continuation_awaiting(*args, 1, "never-created")
    _recovery_claim(store, args, "second")
    assert store.continuation_origins("second") == ["original"]
    assert [
        e.payload["generation"]
        for e in store.list_audit_events(event="continuation_released")
    ] == [1]


@pytest.mark.parametrize("initial_row", [False, True])
def test_never_launched_claim_is_not_a_publication_ancestor(
    store, tmp_path, initial_row
):
    from kew.audit import emit

    args = _recovery_family(store, tmp_path)
    _recovery_claim(store, args, "first")
    if initial_row:
        store.save(
            RunRecord(
                run_id="first",
                item_id="42",
                issue_num=42,
                branch="agent/issue-42",
                status="pending",
                exit_code=0,
                started_at=datetime.now(UTC),
            )
        )
    assert store.mark_continuation_awaiting(*args, 1, "first")
    _recovery_claim(store, args, "second")
    assert store.continuation_origins("second") == ["original"]
    # Positive execution evidence must restore the ancestor, including an
    # event written after the unwind's run-row observation.
    emit(store, "dispatch_started", actor=Actor("policy", "test"), run_id="first")
    assert store.continuation_origins("second") == ["first"]


def _legacy_gap(store, args, monkeypatch):
    _recovery_claim(store, args, "legacy", attest=False)
    # Model the old writer without editing or deleting any audit rows.
    with monkeypatch.context() as scoped:
        scoped.setattr(
            store, "_attest_continuation_transition", lambda **kw: None, raising=False
        )
        assert store.mark_continuation_awaiting(*args, 1, "legacy")
    assert store.list_audit_events(event="continuation_claimed") == []
    assert store.list_audit_events(event="continuation_released") == []


def test_legacy_gap_refuses_fresh_claim_with_abandon_recovery(
    store, tmp_path, monkeypatch
):
    from kew.config import DispatchConfig
    from kew.service import verbs

    args = _recovery_family(store, tmp_path)
    _legacy_gap(store, args, monkeypatch)
    before = store.get_continuation(*args)
    with pytest.raises(verbs.EpicResumeDenied, match="kew epic abandon"):
        verbs.epic_resume(
            store, epic_ref="epic:x", item_id="42", config=DispatchConfig()
        )
    assert store.get_continuation(*args) == before
    assert store.list_audit_events(event="continuation_claimed") == []
    # The parser also names the recovery when a lower-level caller claims.
    _recovery_claim(store, args, "second")
    with pytest.raises(RuntimeError, match="kew epic abandon"):
        store.continuation_origins("second")


def test_legacy_gap_refuses_abandonment_without_erasing_history(
    store, tmp_path, monkeypatch
):
    from kew.config import DispatchConfig
    from kew.service import verbs

    abandon = getattr(verbs, "epic_abandon", None)
    assert callable(abandon), "epic_abandon must report owner-history gaps"
    args = _recovery_family(store, tmp_path)
    _legacy_gap(store, args, monkeypatch)
    from kew.worktree import checkout_claim_guard

    store.save(
        RunRecord(
            run_id="original",
            issue_num=42,
            branch="agent/issue-42",
            status="failure",
            exit_code=1,
            started_at=datetime.now(UTC),
        )
    )
    before = store.read_chain_rows()
    with pytest.raises(verbs.EpicAbandonDenied, match="generation 1.*claim count 0"):
        abandon(
            store,
            epic_ref="epic:x",
            item_id="42",
            reason="legacy gap",
            config=DispatchConfig(),
            checkout_guard=checkout_claim_guard,
        )
    assert store.get_continuation(*args).state == "awaiting_continuation"
    assert store.get_continuation(*args).generation == 1
    assert store.read_chain_rows() == before


# --------------------------------------------------------------------------- #
# The journaled PR facts read back (#2303)
# --------------------------------------------------------------------------- #


def test_settled_statuses_are_the_driver_statuses_they_name() -> None:
    """``run_store`` may not import ``kew.graph``, so the words are duplicated.

    A duplicated vocabulary drifts silently: a status renamed on the driver side
    would leave this reader quietly including merged nodes in a staleness page.
    """
    from kew.graph.driver.overlay import DriverStatus

    assert set(SETTLED_NODE_STATUSES) == {
        DriverStatus.DONE.value,
        DriverStatus.CANCELLED.value,
    }


def test_the_open_only_predicate_binds_one_placeholder_per_status() -> None:
    """The fragment is literal SQL (no interpolation), so the arity is a claim.

    One placeholder too few and the query raises; one too many and it silently
    compares a status against the page limit.
    """
    assert _NODE_PR_FACTS_OPEN.count("?") == len(SETTLED_NODE_STATUSES)


def _snapshot(nodes: list[dict]) -> dict:
    return {
        "schema": 1,
        "epic_ref": "e",
        "signal": "progressing",
        "counts": {},
        "frontier": [],
        "nodes": nodes,
        "edges": [],
    }


def test_latest_node_pr_facts_reads_only_the_newest_tick(store) -> None:
    """A head that moved is the NEWEST journaled one, not the first recorded."""
    pr = "https://github.com/o/r/pull/9"
    for tick, head in (
        ("2026-10-05T00:00:00+00:00", "aaa"),
        ("2026-10-06T00:00:00+00:00", "bbb"),
    ):
        store.append_epic_tick(
            epic_ref="e",
            tick_ts=tick,
            snapshot=_snapshot(
                [{"id": "42", "status": "in_review", "pr_url": pr, "head_oid": head}]
            ),
            actions={},
            snapshot_hash=head,
        )

    rows = store.latest_node_pr_facts()

    assert [(r.item_id, r.head_oid, r.observed_at) for r in rows] == [
        ("42", "bbb", "2026-10-06T00:00:00+00:00")
    ]


def test_latest_node_pr_facts_pages_across_epics_in_one_order(store) -> None:
    pr = "https://github.com/o/r/pull/9"
    for ref, item in (("a", "1"), ("b", "2"), ("c", "3")):
        store.append_epic_tick(
            epic_ref=ref,
            tick_ts="2026-10-06T00:00:00+00:00",
            snapshot=_snapshot([{"id": item, "status": "todo", "pr_url": pr}]),
            actions={},
            snapshot_hash=f"h{ref}",
        )

    first = store.latest_node_pr_facts(limit=2)
    second = store.latest_node_pr_facts(
        limit=2, after=(first[-1].epic_ref, first[-1].item_id)
    )

    assert [(r.epic_ref, r.item_id) for r in first] == [("a", "1"), ("b", "2")]
    assert [(r.epic_ref, r.item_id) for r in second] == [("c", "3")]


def test_latest_node_pr_facts_reads_one_epic_when_asked(store) -> None:
    """The filter a single-epic surface needs, SQL-side (#2303).

    A page bounded by ``limit`` over EVERY epic's newest snapshot can fill
    before it reaches the epic on screen, and the console then draws no badge
    on any of its nodes — which reads exactly like "nothing here is stale".
    Narrowing after the page was bounded would not fix that, so the ref is a
    predicate in the query: asking for the last epic alphabetically must find
    it even at ``limit=1``.
    """
    pr = "https://github.com/o/r/pull/9"
    for ref, item in (("a", "1"), ("b", "2"), ("c", "3")):
        store.append_epic_tick(
            epic_ref=ref,
            tick_ts="2026-10-06T00:00:00+00:00",
            snapshot=_snapshot([{"id": item, "status": "todo", "pr_url": pr}]),
            actions={},
            snapshot_hash=f"h{ref}",
        )

    only_c = store.latest_node_pr_facts(epic_ref="c", limit=1)
    nobody = store.latest_node_pr_facts(epic_ref="never-driven")
    everything = store.latest_node_pr_facts()

    assert [(r.epic_ref, r.item_id) for r in only_c] == [("c", "3")]
    assert nobody == []
    assert [r.epic_ref for r in everything] == ["a", "b", "c"]


def test_latest_node_pr_facts_reads_back_when_the_merge_state_was_read(store) -> None:
    """``merge_state_at``, not the tick: the driver carries a merge state
    forward over a tick GitHub answered UNKNOWN on, so a reader that aged the
    value by ``observed_at`` would read it as fresher than it is."""
    store.append_epic_tick(
        epic_ref="e",
        tick_ts="2026-10-06T00:00:00+00:00",
        snapshot=_snapshot(
            [
                {
                    "id": "42",
                    "status": "in_review",
                    "pr_url": "https://github.com/o/r/pull/9",
                    "merge_state": "DIRTY",
                    "merge_state_since": "2026-10-04T00:00:00+00:00",
                    "merge_state_at": "2026-10-05T00:00:00+00:00",
                }
            ]
        ),
        actions={},
        snapshot_hash="h",
    )

    (row,) = store.latest_node_pr_facts()

    assert row.merge_state == "DIRTY"
    assert row.merge_state_since == "2026-10-04T00:00:00+00:00"
    assert row.merge_state_at == "2026-10-05T00:00:00+00:00"
    assert row.observed_at == "2026-10-06T00:00:00+00:00"


def test_latest_node_pr_facts_clamps_an_unbounded_limit(store) -> None:
    pr = "https://github.com/o/r/pull/9"
    store.append_epic_tick(
        epic_ref="e",
        tick_ts="2026-10-06T00:00:00+00:00",
        snapshot=_snapshot([{"id": "1", "status": "todo", "pr_url": pr}]),
        actions={},
        snapshot_hash="h",
    )

    # Both ends: an ask of zero is a page nobody can read, and an unbounded ask
    # is one request that scales with the whole journal.
    assert len(store.latest_node_pr_facts(limit=0)) == 1
    assert len(store.latest_node_pr_facts(limit=10_000_000)) == 1


def test_the_audit_item_index_exists_on_every_open(store) -> None:
    """``audit_events(item_id, event)``: every per-item audit read filters on
    both, and that pair had no index at all. Leading with ``item_id`` keeps the
    index out of the #2129 ``event``-prefix plan, which ``idx_audit_events_event_ts``
    serves together with its ``ORDER BY timestamp``."""
    names = {
        str(row["name"])
        for row in store._execute("SELECT name FROM sqlite_master WHERE type = 'index'")
    }

    assert "idx_audit_events_item_event" in names
