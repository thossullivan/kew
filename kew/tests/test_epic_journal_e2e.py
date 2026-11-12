"""End-to-end: scripted drain -> journal -> replay sequence (#454)."""

from pathlib import Path

from support.checkout_generations import (
    fixture_checkout_generation,
    fixture_claim_evidence,
)
from support.epic_fixture import run_scripted_drain

from kew.audit import Actor, emit
from kew.audit_chain import verify_chain
from kew.run_store import SqliteRunStore
from kew.service.epics import list_ticks


def test_scripted_drain_journals_the_demo_sequence(tmp_path: Path, monkeypatch):
    db = tmp_path / "dispatch.db"
    ticks = run_scripted_drain(db, epic_ref="demo-epic", monkeypatch=monkeypatch)

    def status_of(tick, nid):
        return {n["id"]: n["status"] for n in tick.snapshot["nodes"]}[nid]

    assert status_of(ticks[0], "101") == "in_progress"
    assert "101" in ticks[0].actions["dispatched"]
    assert status_of(ticks[1], "101") == "in_review"
    assert status_of(ticks[2], "101") == "held"
    held_node = [n for n in ticks[2].snapshot["nodes"] if n["id"] == "101"][0]
    assert held_node["pr_url"]  # PR link present for HELD pane
    merged_tick = next(t for t in ticks if "101" in t.actions["merged"])
    assert status_of(merged_tick, "101") == "done"  # frame never contradicts actions
    assert "105" in merged_tick.actions["dispatched"] or any(
        "105" in t.actions["dispatched"] for t in ticks
    )  # dependent unblocked
    failed_tick = next(t for t in ticks if status_of(t, "105") == "failed")
    fnode = [n for n in failed_tick.snapshot["nodes"] if n["id"] == "105"][0]
    # The stall reason now carries the failing run's exit code (#661).
    assert fnode["stuck"] == "failed-terminal (exit 1)"
    assert ticks[-1].snapshot["signal"] == "stalled"  # FAILED branch keeps it honest
    # journal is changed-only: every consecutive pair differs
    hashes = [t.snapshot_hash for t in ticks]
    assert len(hashes) == len(set(hashes))
    # economics source exists: fixture wrote finished run rows with costs
    store = SqliteRunStore(str(db))
    assert any(r.estimated_cost_usd > 0 for r in store.list_all())
    store.close()


def test_replay_frames_carry_economics(tmp_path: Path, monkeypatch):
    """Regression for the fixture's economics-window bug (#454 Phase B
    finding): run `finished_at` timestamps must land inside the window of
    the replay frame that observes them, not minutes in the future relative
    to the (real wall-clock) tick_ts each frame is journaled with — else
    every frame's drain_total_usd reads back as 0.0 forever.

    Drives the same fixture through the actual read-service layer
    (`list_ticks`) rather than asserting on raw store rows, so this proves
    the bug as it is actually observed by the API/serve layer.
    """
    db = tmp_path / "dispatch.db"
    run_scripted_drain(db, epic_ref="demo-epic", monkeypatch=monkeypatch)

    store = SqliteRunStore(str(db))
    try:
        frames = list_ticks(store, "demo-epic", after_id=0, limit=1000)
    finally:
        store.close()

    assert frames, "expected at least one replay frame"
    assert frames[-1].economics.drain_total_usd > 0

    drains = [f.economics.drain_total_usd for f in frames]
    assert drains == sorted(drains)  # monotonically non-decreasing over time


def test_continuation_lineage_survives_publication_and_terminal_crash_edges(
    tmp_path: Path,
) -> None:
    db = tmp_path / "dispatch.db"
    worktree = tmp_path / "preserved" / "agent" / "issue-SFD-174"

    with SqliteRunStore(str(db)) as store:
        record = store.record_continuation(
            "epic/recovery",
            "SFD-174",
            "original-run",
            "agent/issue-SFD-174",
            worktree,
            checkout_generation=fixture_checkout_generation(worktree),
        )
        assert (
            store.claim_continuation_under_cap(
                record.epic_ref,
                record.item_id,
                record.original_run_id,
                record.branch,
                record.worktree_identity,
                "continuation-run",
                5,
                claim_evidence=fixture_claim_evidence(
                    store,
                    record.epic_ref,
                    record.item_id,
                    record.original_run_id,
                    record.branch,
                    record.worktree_identity,
                    "continuation-run",
                ),
            )
            == "claimed"
        )
        record = store.get_continuation(
            record.epic_ref,
            record.item_id,
            record.original_run_id,
            record.branch,
            record.worktree_identity,
        )
        assert record is not None
        # The claim transaction already committed its single lineage event.
        emit(
            store,
            "publication_deferred",
            actor=Actor("policy", "host-publication"),
            run_id="continuation-run",
            item_id="SFD-174",
            payload={"category": "publication_unavailable"},
        )
        emit(
            store,
            "dispatch_completed",
            actor=Actor("agent", "codex"),
            run_id="continuation-run",
            item_id="SFD-174",
            payload={"exit_code": 0},
        )
        assert (
            store.finalize_continuation(
                record.epic_ref,
                record.item_id,
                record.original_run_id,
                record.branch,
                record.worktree_identity,
                record.generation,
                "continuation-run",
            )
            is True
        )
        events = store.list_audit_events(limit=None)
        assert verify_chain(store.read_chain_rows()).ok

    assert [event.event for event in reversed(events)] == [
        "continuation_checkout_bound",
        "continuation_claimed",
        "publication_deferred",
        "dispatch_completed",
    ]
    for event in events:
        if event.event == "continuation_checkout_bound":
            assert event.payload["checkout_id"] == ""
            continue
        assert event.payload["original_run_id"] == "original-run"
        assert event.payload["continuation_run_id"] == "continuation-run"
        assert event.payload["item_id"] == "SFD-174"
        assert event.payload["branch"] == "agent/issue-SFD-174"
        assert event.payload["worktree_identity"].startswith("sha256:")
