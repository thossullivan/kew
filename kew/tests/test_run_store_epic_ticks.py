"""epic_ticks journal + epic_driver_state heartbeat storage (#454 Phase A)."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from kew.run_store import (
    CURRENT_SCHEMA_VERSION,
    EPIC_TICK_LOG_CAP,
    RunRecord,
    SqliteRunStore,
)
from kew.service.epics import driver_liveness


@pytest.fixture
def store(tmp_path: Path) -> SqliteRunStore:
    s = SqliteRunStore(str(tmp_path / "dispatch.db"))
    yield s
    s.close()


SNAP_A = {
    "schema": 1,
    "epic_ref": "e",
    "signal": "progressing",
    "counts": {"todo": 1},
    "frontier": ["1"],
    "nodes": [
        {
            "id": "1",
            "title": "t",
            "status": "todo",
            "status_since": "2026-07-02T00:00:00+00:00",
            "url": None,
            "pr_url": None,
            "external": False,
            "stuck": None,
        }
    ],
    "edges": [],
}
ACTIONS = {"schema": 1, "dispatched": [], "merged": [], "held": [], "stuck": []}


def test_migration_reaches_v31(store: SqliteRunStore) -> None:
    # No hard-coded version pin: the meaningful check is that a fresh store
    # lands exactly on CURRENT_SCHEMA_VERSION, whatever that is. A pin here
    # forces this file into the scope of every schema-bumping change.
    row = store._conn.execute("SELECT version FROM schema_version").fetchone()
    assert row["version"] == CURRENT_SCHEMA_VERSION
    # V33: durable tracker column present on epic_driver_state (#927/N3)
    driver_cols = {
        r["name"] for r in store._conn.execute("PRAGMA table_info(epic_driver_state)")
    }
    assert "tracker" in driver_cols
    tables = {
        r["name"]
        for r in store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {"epic_ticks", "epic_driver_state"}.issubset(tables)
    # V28: epic_graph_cache table present (W2/#894)
    assert "epic_graph_cache" in tables
    # V29: review_rounds table present (F8/#977)
    assert "review_rounds" in tables
    # V22: container_name column present on runs table
    cols = {r["name"] for r in store._conn.execute("PRAGMA table_info(runs)")}
    assert "container_name" in cols
    # V30: first-edit metric columns present on runs table (D2)
    assert "steps_to_first_edit" in cols
    assert "tokens_to_first_edit" in cols
    # V24: item_id column present on runs table
    assert "item_id" in cols
    # V25: item_id column present on the epic tables
    for tbl in ("epic_reservations", "epic_holds", "epic_retries"):
        epic_cols = {
            r["name"] for r in store._conn.execute(f"PRAGMA table_info({tbl})")
        }
        assert "item_id" in epic_cols, tbl


def test_v20_db_migrates_up(tmp_path: Path) -> None:
    db = tmp_path / "dispatch.db"
    s = SqliteRunStore(str(db))
    s._conn.execute("UPDATE schema_version SET version = 20")
    s._conn.commit()
    s.close()
    s2 = SqliteRunStore(str(db))
    row = s2._conn.execute("SELECT version FROM schema_version").fetchone()
    assert row["version"] == CURRENT_SCHEMA_VERSION
    s2.close()


def test_append_and_dedup(store: SqliteRunStore) -> None:
    tid1, appended1 = store.append_epic_tick(
        epic_ref="e",
        tick_ts="2026-07-02T00:00:00+00:00",
        snapshot=SNAP_A,
        actions=ACTIONS,
        snapshot_hash="h1",
    )
    assert appended1 is True
    tid2, appended2 = store.append_epic_tick(
        epic_ref="e",
        tick_ts="2026-07-02T00:01:00+00:00",
        snapshot=SNAP_A,
        actions=ACTIONS,
        snapshot_hash="h1",  # same hash as latest → dedup
    )
    assert (tid2, appended2) == (tid1, False)
    assert store.count_epic_ticks("e") == 1  # per-epic: count is 1, not 2

    tid3, appended3 = store.append_epic_tick(
        epic_ref="e",
        tick_ts="2026-07-02T00:02:00+00:00",
        snapshot=SNAP_A,
        actions=ACTIONS,
        snapshot_hash="h2",  # different hash → append
    )
    assert appended3 is True
    assert tid3 > tid1
    assert store.count_epic_ticks("e") == 2

    _, appended4 = store.append_epic_tick(
        epic_ref="other",
        tick_ts="2026-07-02T00:03:00+00:00",
        snapshot=SNAP_A,
        actions=ACTIONS,
        snapshot_hash="h1",  # dedup is per-epic: appends on another epic_ref
    )
    assert appended4 is True
    assert store.count_epic_ticks("e") == 2  # other epic's tick doesn't affect "e"


def test_dedup_only_against_latest_row(store: SqliteRunStore) -> None:
    """A recurring hash after intervening state changes appends a NEW row."""
    tid1, _ = store.append_epic_tick(
        epic_ref="e",
        tick_ts="2026-07-02T00:00:00+00:00",
        snapshot=SNAP_A,
        actions=ACTIONS,
        snapshot_hash="hA",
    )
    tid2, _ = store.append_epic_tick(
        epic_ref="e",
        tick_ts="2026-07-02T00:01:00+00:00",
        snapshot=SNAP_A,
        actions=ACTIONS,
        snapshot_hash="hB",
    )
    tid3, appended3 = store.append_epic_tick(
        epic_ref="e",
        tick_ts="2026-07-02T00:02:00+00:00",
        snapshot=SNAP_A,
        actions=ACTIONS,
        snapshot_hash="hA",  # recurs, but latest row is hB → append
    )
    assert appended3 is True
    assert tid3 > tid2 > tid1
    assert store.count_epic_ticks("e") == 3


def test_latest_and_list_epic_tick(store: SqliteRunStore) -> None:
    store.append_epic_tick(
        epic_ref="e",
        tick_ts="t1",
        snapshot=SNAP_A,
        actions=ACTIONS,
        snapshot_hash="h1",
    )
    store.append_epic_tick(
        epic_ref="e",
        tick_ts="t2",
        snapshot=SNAP_A,
        actions=ACTIONS,
        snapshot_hash="h2",
    )
    latest = store.latest_epic_tick("e")
    assert latest is not None
    assert latest.tick_ts == "t2"
    assert latest.snapshot == SNAP_A  # JSON round-trips dicts
    assert latest.actions == ACTIONS
    ticks = store.list_epic_ticks("e")
    assert [t.tick_ts for t in ticks] == ["t1", "t2"]
    assert store.list_epic_ticks("e", after_id=ticks[0].id) == [ticks[1]]
    assert store.first_epic_tick_ts("e") == "t1"
    assert store.first_epic_tick_ts("nope") is None
    assert store.latest_epic_tick("nope") is None


def test_heartbeat_two_phase(store: SqliteRunStore) -> None:
    store.begin_epic_tick("e", "2026-07-02T00:00:00+00:00")
    st = store.get_epic_driver_state("e")
    assert st is not None
    assert st.tick_count == 1
    assert st.last_tick_started_at == "2026-07-02T00:00:00+00:00"
    assert st.last_tick_ts is None  # phase 2 not written yet
    store.finish_epic_tick(
        "e",
        tick_ts="2026-07-02T00:00:30+00:00",
        duration_s=30.0,
        interval_s=60.0,
        signal="progressing",
        tick_id=7,
    )
    st = store.get_epic_driver_state("e")
    assert st.last_tick_ts == "2026-07-02T00:00:30+00:00"
    assert st.last_tick_duration_s == 30.0
    assert st.interval_s == 60.0
    assert st.last_signal == "progressing"
    assert st.last_tick_id == 7
    store.begin_epic_tick("e", "2026-07-02T00:01:00+00:00")
    assert store.get_epic_driver_state("e").tick_count == 2
    # one-shot NULL interval_s scenario
    store.begin_epic_tick("o", "2026-07-02T00:00:00+00:00")
    store.finish_epic_tick(
        "o",
        tick_ts="2026-07-02T00:00:01+00:00",
        duration_s=1.0,
        interval_s=None,
        signal="stalled",
        tick_id=None,
    )
    assert store.get_epic_driver_state("o").interval_s is None


def test_list_epic_refs(store: SqliteRunStore) -> None:
    store.append_epic_tick(
        epic_ref="journal-only",
        snapshot=SNAP_A,
        actions=ACTIONS,
        tick_ts="t",
        snapshot_hash="h",
    )
    store.begin_epic_tick("heartbeat-only", "2026-07-02T00:00:00+00:00")
    assert store.list_epic_refs() == ["heartbeat-only", "journal-only"]


def test_list_epic_refs_reaches_an_attempt_only_epic(store: SqliteRunStore) -> None:
    # #2304 review round 4: every tick of this epic was refused before
    # `begin_epic_tick` — failed preflight, locked out — so it has attempt rows
    # and NOTHING else. Enumerating only the heartbeat and the change journal
    # left those rows with no reader: the roster dropped the epic and
    # /api/epics/<ref>/tick-strip 404'd on it through `known_epic_refs`, which
    # is this list.
    row_id = store.begin_epic_tick_log("attempt-only", "2026-07-02T00:00:00+00:00")
    store.finish_epic_tick_log(
        row_id,
        ts_finished="2026-07-02T00:00:01+00:00",
        outcome="locked_out",
    )
    assert store.get_epic_driver_state("attempt-only") is None
    assert store.count_epic_ticks("attempt-only") == 0
    assert store.list_epic_refs() == ["attempt-only"]
    # And the strip the ref now reaches is the attempt that was refused.
    assert [e.outcome for e in store.list_epic_tick_log_tail("attempt-only")] == [
        "locked_out"
    ]


def test_an_empty_graph_only_ref_never_becomes_an_epic(store: SqliteRunStore) -> None:
    """Reviewer finding (PR #2438). `advance` raises `EmptyEpicGraphError` for
    exactly two things — a typo'd epic name and a wrong-tracker cwd — so an
    attempt row is NOT evidence the ref names an epic. Admitting it made one
    mistyped `kew epic advance` a permanent member of the `kew epic run --all`
    roster (this list), which re-raised its empty graph as STALLED and a
    non-zero exit on every later run, with nothing able to take it back off:
    no code path deletes an epic's attempt rows.
    """
    store.register_epic("real-epic")
    typo = store.begin_epic_tick_log("typoo", "2026-07-02T00:00:00+00:00")
    store.finish_epic_tick_log(
        typo,
        ts_finished="2026-07-02T00:00:01+00:00",
        outcome="empty_graph",
        error_class="EmptyEpicGraphError",
    )
    assert store.list_epic_refs() == ["real-epic"]
    # A second mistyped attempt does not accumulate into one either.
    again = store.begin_epic_tick_log("typoo", "2026-07-02T00:01:00+00:00")
    store.finish_epic_tick_log(
        again,
        ts_finished="2026-07-02T00:01:01+00:00",
        outcome="empty_graph",
        error_class="EmptyEpicGraphError",
    )
    assert store.list_epic_refs() == ["real-epic"]
    # The rows are still THERE — the exclusion is a reading of them, not a
    # deletion, so the attempts remain readable by ref for forensics.
    assert len(store.list_epic_tick_log_tail("typoo")) == 2

    # The exclusion is per-ROW, not per-ref: one attempt of any other kind is a
    # real epic being refused, which is what the attempt arm exists to reach.
    refused = store.begin_epic_tick_log("typoo", "2026-07-02T00:02:00+00:00")
    store.finish_epic_tick_log(
        refused, ts_finished="2026-07-02T00:02:01+00:00", outcome="locked_out"
    )
    assert store.list_epic_refs() == ["real-epic", "typoo"]


def test_an_open_attempt_row_is_an_epic_mid_tick(store: SqliteRunStore) -> None:
    """The other side of the empty-graph exclusion: an OPEN row (no outcome
    yet) is a tick in flight, which is the one state the strip draws as live —
    so it stays admitted, and only the closed `empty_graph` reading retires it.
    """
    store.begin_epic_tick_log("in-flight", "2026-07-02T00:00:00+00:00")
    assert store.list_epic_refs() == ["in-flight"]


def test_register_epic_makes_unstarted_epic_visible(store: SqliteRunStore) -> None:
    # A planned-but-never-driven epic (zero ticks, zero driver history) is
    # invisible to list_epic_refs until registered (#740).
    assert store.list_epic_refs() == []
    store.register_epic("stitches")
    assert store.list_epic_refs() == ["stitches"]
    state = store.get_epic_driver_state("stitches")
    assert state is not None
    assert state.tick_count == 0
    assert state.last_tick_started_at is None


def test_register_epic_never_clobbers_live_state(store: SqliteRunStore) -> None:
    # Registering an already-driven epic is a no-op: it must not reset the
    # heartbeat's tick_count or started_at (#740).
    store.begin_epic_tick("stitches", "2026-07-02T00:00:00+00:00")
    store.register_epic("stitches")
    state = store.get_epic_driver_state("stitches")
    assert state.tick_count == 1
    assert state.last_tick_started_at == "2026-07-02T00:00:00+00:00"


def test_epic_trackers_infers_tracker_from_item_id_shape(
    store: SqliteRunStore,
) -> None:
    # `epic run --all` scopes itself to the active project's tracker (#882/B1).
    # An epic's tracker is inferred from the shape of its recorded work-item
    # ids: a numeric id ("882") is GitHub, an opaque key ("SFD-12") is Linear,
    # and an epic with no items yet resolves to None (drive under the active
    # tracker rather than guess).
    store.register_epic("gh-epic")
    store.register_epic("lin-epic")
    store.register_epic("fresh-epic")
    store.reserve_epic_node_item("gh-epic", "882", "run-gh")
    store.reserve_epic_node_item("lin-epic", "SFD-12", "run-lin")

    trackers = store.epic_trackers()

    assert trackers["gh-epic"] == "github"
    assert trackers["lin-epic"] == "linear"
    assert trackers["fresh-epic"] is None


def _save_run(store: SqliteRunStore, run_id: str, item_id: str) -> None:
    store.save(
        RunRecord(
            run_id=run_id,
            issue_num=0,
            branch=f"agent/{item_id}",
            status="success",
            exit_code=0,
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
            item_id=item_id,
        )
    )


def _insert_pre_recording_reservation(
    store: SqliteRunStore, *, issue_num: int, epic_ref: str, run_id: str
) -> None:
    """Insert a pre-v25 epic_reservations row: it has a ``run_id`` but its
    ``item_id`` column is NULL (the durable id was not recorded on the epic
    tables until v25)."""
    store._conn.execute(
        "INSERT INTO epic_reservations (issue_num, epic_ref, run_id, reserved_at)"
        " VALUES (?, ?, ?, ?)",
        (issue_num, epic_ref, run_id, datetime.now(UTC).isoformat()),
    )
    store._conn.commit()


def test_epic_trackers_backfills_tracker_from_run_item_id(
    store: SqliteRunStore,
) -> None:
    # #877/R2: an epic driven BEFORE the epic tables recorded item_id (v25) has
    # a reservation row whose own item_id is NULL, so the #887 heuristic saw an
    # empty set and resolved None — driving it under the active tracker and
    # misclassifying a complete Linear epic as STALLED. The durable id still
    # lives on the run the reservation points at (runs.item_id, recorded since
    # v24), so inference must join through run_id to recover it.
    store.register_epic("lin-old")
    _save_run(store, "run-old", "SFD-7")
    _insert_pre_recording_reservation(
        store, issue_num=7, epic_ref="lin-old", run_id="run-old"
    )

    assert store.epic_trackers()["lin-old"] == "linear"


def test_epic_trackers_still_none_for_never_driven_epic(store: SqliteRunStore) -> None:
    # The backfill must NOT resolve a tracker for a genuinely never-driven epic
    # (no reservation, no run): empty set ⇒ None still means "drive under the
    # active tracker rather than guess" (#877/R2).
    store.register_epic("fresh-old")

    assert store.epic_trackers()["fresh-old"] is None


def test_epic_trackers_prefers_recorded_tracker_over_numeric_item_ids(
    store: SqliteRunStore,
) -> None:
    # #927/N3 (third leak of #877): a complete Linear epic whose runs carry
    # NUMERIC GitHub-twin item ids (a pre-SFD recording era) would infer
    # "github" from shape alone — then `epic run --all` probes it against GitHub
    # labels, false-STALLS it, and poisons the fleet exit code. The durable
    # tracker recorded at drive time is ground truth and must WIN over the
    # misleading numeric shape.
    store.register_epic("lin-twin")
    store.reserve_epic_node_item("lin-twin", "784", "run-twin")  # numeric twin
    assert store.epic_trackers()["lin-twin"] == "github"  # shape-only: wrong

    store.record_epic_tracker("lin-twin", "linear")

    assert store.epic_trackers()["lin-twin"] == "linear"


def test_record_epic_tracker_is_authoritative_and_survives_ticks(
    store: SqliteRunStore,
) -> None:
    # Recording is an upsert that never clobbers an in-flight heartbeat's
    # tick_count, and re-recording the (ground-truth) active tracker is
    # idempotent (#927/N3).
    store.begin_epic_tick("e", "2026-07-02T00:00:00+00:00")
    store.record_epic_tracker("e", "linear")
    store.record_epic_tracker("e", "linear")

    state = store.get_epic_driver_state("e")
    assert state is not None
    assert state.tick_count == 1
    assert state.last_tick_started_at == "2026-07-02T00:00:00+00:00"
    assert store.epic_trackers()["e"] == "linear"


def test_record_epic_tracker_seeds_row_for_unregistered_epic(
    store: SqliteRunStore,
) -> None:
    # Recording before the first begin_epic_tick still creates a durable,
    # visible row (upsert path), so classification never races the heartbeat.
    store.record_epic_tracker("pre-seed", "linear")

    assert "pre-seed" in store.list_epic_refs()
    assert store.epic_trackers()["pre-seed"] == "linear"


def _drive(
    store: SqliteRunStore,
    epic_ref: str,
    *,
    began: str,
    ended: str,
    duration_s: float,
    interval_s: float | None,
    expected_interval_s: float | None = None,
) -> None:
    """One complete drive of *epic_ref*: both heartbeat phases, in the order
    graph.driver.tick.advance runs them (begin stamps the start, finish the
    end)."""
    store.begin_epic_tick(epic_ref, began)
    store.finish_epic_tick(
        epic_ref,
        tick_ts=ended,
        duration_s=duration_s,
        interval_s=interval_s,
        expected_interval_s=expected_interval_s,
        signal="progressing",
        tick_id=None,
    )


def test_a_one_shot_drive_retires_the_dead_pollers_recorded_cadence(
    store: SqliteRunStore,
) -> None:
    """#2297 review: COALESCE(?, interval_s) made a retired poller's measured
    cadence permanently unclearable, and _cadence always prefers it over the
    expectation — so an epic moved from "epic run --interval 60" to a 5-minute
    cron "epic advance" read gone two minutes after a SUCCESSFUL advance,
    forever, and [epic] driver_expected_interval_seconds never took effect.

    The cron's own 5-minute gap is what proves the 60s cadence dead: nothing
    polled at it while the cron was away.
    """
    _drive(
        store,
        "e",
        began="2026-07-02T00:00:00+00:00",
        ended="2026-07-02T00:00:30+00:00",
        duration_s=30.0,
        interval_s=60.0,
    )
    # The poller is gone; a 5-minute cron now drives the epic.
    _drive(
        store,
        "e",
        began="2026-07-02T00:05:30+00:00",
        ended="2026-07-02T00:05:31+00:00",
        duration_s=1.0,
        interval_s=None,
        expected_interval_s=300.0,
    )
    state = store.get_epic_driver_state("e")
    assert state is not None
    assert state.interval_s is None
    assert state.expected_interval_s == 300.0

    # ... and the liveness reading thresholds the cron's cadence, so a
    # successful advance two minutes ago is live rather than two-intervals gone.
    reading = driver_liveness(
        store, "e", now=datetime(2026, 7, 2, 0, 7, 31, tzinfo=UTC)
    )
    assert (reading.interval_s, reading.cadence_source) == (300.0, "expected")
    assert reading.status == "live"


def test_a_one_shot_drive_in_a_live_pollers_gap_keeps_its_cadence(
    store: SqliteRunStore,
) -> None:
    """#2297 review round 2: the unconditional interval_s write was the
    mirror-image bug. The driver lock is held for the duration of a tick, not
    between ticks, so a one-shot "kew epic advance" in a LIVE "epic run
    --interval 1800" poller's inter-tick gap is NOT locked out — it reached this
    write and blanked the poller's cadence, and the console then read gone
    (expectation 300s, poller legitimately silent for up to 1800s) until the
    poller's next tick.

    The one-shot observed a 10-minute gap, which a 30-minute poller honours, so
    the recorded cadence stays and the reading stays live.
    """
    _drive(
        store,
        "e",
        began="2026-07-02T00:00:00+00:00",
        ended="2026-07-02T00:00:10+00:00",
        duration_s=10.0,
        interval_s=1800.0,
    )
    _drive(
        store,
        "e",
        began="2026-07-02T00:10:00+00:00",
        ended="2026-07-02T00:10:01+00:00",
        duration_s=1.0,
        interval_s=None,
        expected_interval_s=300.0,
    )
    state = store.get_epic_driver_state("e")
    assert state is not None
    assert (state.interval_s, state.expected_interval_s) == (1800.0, 300.0)

    # 15 minutes on, the poller is still asleep inside its own interval: the
    # reading thresholds the cadence it kept, not the one-shot's expectation.
    reading = driver_liveness(
        store, "e", now=datetime(2026, 7, 2, 0, 25, 1, tzinfo=UTC)
    )
    assert (reading.interval_s, reading.cadence_source) == (1800.0, "recorded")
    assert reading.status == "live"


def test_the_recorded_cadence_survives_one_ambiguous_gap_then_retires(
    store: SqliteRunStore,
) -> None:
    """The retirement bound is the reader's own gone threshold, so writer and
    reader agree: a gap a 60s poller could still have honoured (one missed tick
    of jitter) leaves the cadence alone, and the next drive that proves a real
    lapse retires it. The tolerance defers the retirement, it never freezes."""
    _drive(
        store,
        "e",
        began="2026-07-02T00:00:00+00:00",
        ended="2026-07-02T00:00:30+00:00",
        duration_s=30.0,
        interval_s=60.0,
    )
    _drive(
        store,
        "e",
        began="2026-07-02T00:01:30+00:00",
        ended="2026-07-02T00:01:31+00:00",
        duration_s=1.0,
        interval_s=None,
        expected_interval_s=300.0,
    )
    kept = store.get_epic_driver_state("e")
    assert kept is not None and kept.interval_s == 60.0

    _drive(
        store,
        "e",
        began="2026-07-02T00:06:30+00:00",
        ended="2026-07-02T00:06:31+00:00",
        duration_s=1.0,
        interval_s=None,
        expected_interval_s=300.0,
    )
    retired = store.get_epic_driver_state("e")
    assert retired is not None and retired.interval_s is None


def test_an_unreadable_previous_stamp_leaves_the_recorded_cadence_alone(
    store: SqliteRunStore,
) -> None:
    """A stamp that cannot be parsed proves no lapse, so it never retires a
    cadence: history is tolerated, never repaired. The drive that reads it
    writes a readable one, so the retirement is deferred, not lost."""
    _drive(
        store,
        "e",
        began="2026-07-02T00:00:00+00:00",
        ended="2026-07-02T00:00:30+00:00",
        duration_s=30.0,
        interval_s=60.0,
    )
    store._conn.execute(
        "UPDATE epic_driver_state SET last_tick_ts='not-a-timestamp' WHERE epic_ref='e'"
    )
    store._conn.commit()

    _drive(
        store,
        "e",
        began="2026-07-02T01:00:00+00:00",
        ended="2026-07-02T01:00:01+00:00",
        duration_s=1.0,
        interval_s=None,
        expected_interval_s=300.0,
    )

    state = store.get_epic_driver_state("e")
    assert state is not None and state.interval_s == 60.0


def test_the_expected_cadence_lands_in_its_own_column(store: SqliteRunStore) -> None:
    """v62: the expectation is recorded apart from a measured cadence, so a
    reader can tell the driver's own interval from an operator's guess."""
    store.begin_epic_tick("o", "2026-07-02T00:00:00+00:00")
    store.finish_epic_tick(
        "o",
        tick_ts="2026-07-02T00:00:01+00:00",
        duration_s=1.0,
        interval_s=None,
        expected_interval_s=300.0,
        signal="stalled",
        tick_id=None,
    )
    state = store.get_epic_driver_state("o")
    assert state.interval_s is None
    assert state.expected_interval_s == 300.0


def test_a_later_drive_refreshes_the_recorded_expectation(
    store: SqliteRunStore,
) -> None:
    """#2297 review: COALESCE(?, interval_s, ?) froze the FIRST expectation
    forever — a later [epic] driver_expected_interval_seconds was inert, so an
    epic driven slower than that first guess read gone permanently."""
    for ordinal, expected in ((0, 300.0), (1, 3600.0)):
        store.begin_epic_tick("o", f"2026-07-02T00:0{ordinal}:00+00:00")
        store.finish_epic_tick(
            "o",
            tick_ts=f"2026-07-02T00:0{ordinal}:01+00:00",
            duration_s=1.0,
            interval_s=None,
            expected_interval_s=expected,
            signal="progressing",
            tick_id=None,
        )
    state = store.get_epic_driver_state("o")
    assert state.expected_interval_s == 3600.0
    assert state.interval_s is None


def test_a_drive_without_an_expectation_keeps_the_recorded_one(
    store: SqliteRunStore,
) -> None:
    store.begin_epic_tick("o", "2026-07-02T00:00:00+00:00")
    store.finish_epic_tick(
        "o",
        tick_ts="2026-07-02T00:00:01+00:00",
        duration_s=1.0,
        interval_s=None,
        expected_interval_s=300.0,
        signal="progressing",
        tick_id=None,
    )
    store.begin_epic_tick("o", "2026-07-02T00:05:00+00:00")
    store.finish_epic_tick(
        "o",
        tick_ts="2026-07-02T00:05:01+00:00",
        duration_s=1.0,
        interval_s=60.0,
        signal="progressing",
        tick_id=None,
    )
    state = store.get_epic_driver_state("o")
    assert (state.interval_s, state.expected_interval_s) == (60.0, 300.0)


def test_v62_adds_the_expected_cadence_column_without_losing_the_row(
    tmp_path: Path,
) -> None:
    """A pre-v62 heartbeat row survives the migration and reads as
    "no expectation recorded" — history is tolerated, never repaired."""
    path = str(tmp_path / "pre-v62.db")
    with SqliteRunStore(path) as store:
        store.begin_epic_tick("e", "2026-07-02T00:00:00+00:00")
        store.finish_epic_tick(
            "e",
            tick_ts="2026-07-02T00:00:30+00:00",
            duration_s=30.0,
            interval_s=60.0,
            signal="progressing",
            tick_id=None,
        )
        store._conn.execute(
            "ALTER TABLE epic_driver_state DROP COLUMN expected_interval_s"
        )
        store._conn.execute("UPDATE schema_version SET version = 61 WHERE id = 1")
        store._conn.commit()

    with SqliteRunStore(path) as store:
        columns = {
            row["name"]
            for row in store._conn.execute("PRAGMA table_info(epic_driver_state)")
        }
        assert "expected_interval_s" in columns
        assert (
            store._conn.execute("SELECT version FROM schema_version").fetchone()[0]
            == CURRENT_SCHEMA_VERSION
        )
        state = store.get_epic_driver_state("e")
        assert state is not None
        assert state.interval_s == 60.0
        assert state.expected_interval_s is None
        assert state.last_tick_ts == "2026-07-02T00:00:30+00:00"


def test_driver_state_carries_the_tracker_column(store: SqliteRunStore) -> None:
    store.begin_epic_tick("e", "2026-07-02T00:00:00+00:00")
    assert store.get_epic_driver_state("e").tracker is None
    store.record_epic_tracker("e", "linear")
    assert store.get_epic_driver_state("e").tracker == "linear"


def test_v63_adds_the_lock_snapshot_columns_without_losing_the_row(
    tmp_path: Path,
) -> None:
    """A pre-v63 heartbeat row survives the migration and reads as "no lock has
    been observed" — history is tolerated, never repaired. All-None must not read
    as a dead holder: that would be a conclusive gone verdict invented from an
    absence."""
    path = str(tmp_path / "pre-v63.db")
    with SqliteRunStore(path) as store:
        store.begin_epic_tick("e", "2026-07-02T00:00:00+00:00")
        store.finish_epic_tick(
            "e",
            tick_ts="2026-07-02T00:00:30+00:00",
            duration_s=30.0,
            interval_s=60.0,
            signal="progressing",
            tick_id=None,
        )
        for column in (
            "lock_pid",
            "lock_pid_alive",
            "lock_heartbeat_at",
            "lock_observed_at",
        ):
            store._conn.execute(
                f"ALTER TABLE epic_driver_state DROP COLUMN {column}"  # noqa: S608
            )
        store._conn.execute("UPDATE schema_version SET version = 62 WHERE id = 1")
        store._conn.commit()

    with SqliteRunStore(path) as store:
        columns = {
            row["name"]
            for row in store._conn.execute("PRAGMA table_info(epic_driver_state)")
        }
        assert {
            "lock_pid",
            "lock_pid_alive",
            "lock_heartbeat_at",
            "lock_observed_at",
        } <= columns
        assert (
            store._conn.execute("SELECT version FROM schema_version").fetchone()[0]
            == CURRENT_SCHEMA_VERSION
        )
        state = store.get_epic_driver_state("e")
        assert state is not None
        assert state.interval_s == 60.0
        assert state.last_tick_ts == "2026-07-02T00:00:30+00:00"
        assert (state.lock_pid, state.lock_pid_alive) == (None, None)
        assert (state.lock_heartbeat_at, state.lock_observed_at) == (None, None)
        # The absence settles nothing, so the reading still comes off the beat.
        assert (
            driver_liveness(
                store, "e", now=datetime(2026, 7, 2, 0, 1, 0, tzinfo=UTC)
            ).status
            == "live"
        )


def test_an_observed_lock_can_be_recorded_before_the_first_tick(
    store: SqliteRunStore,
) -> None:
    # The sweep can look at an epic the driver has never ticked (registered, or
    # mid first tick): the snapshot upserts rather than silently dropping.
    store.register_epic("fresh")
    store.record_epic_driver_lock(
        "fresh",
        pid=7,
        pid_alive=True,
        heartbeat_at="2026-07-02T00:00:00+00:00",
        observed_at="2026-07-02T00:00:01+00:00",
    )
    state = store.get_epic_driver_state("fresh")
    assert state is not None
    assert (state.lock_pid, state.lock_pid_alive) == (7, True)
    assert state.tick_count == 0


def test_v64_adds_epic_tick_log_and_preserves_pre_v64_rows(tmp_path: Path) -> None:
    """AC1 (#2304). A pre-v64 DB migrates without losing its journal rows.

    Hand-built at v63 with one `epic_ticks` row, reopened through
    `SqliteRunStore`: the row survives, the new table exists with every column
    the strip reads, and the DB lands on `CURRENT_SCHEMA_VERSION`.
    """
    import sqlite3

    db_path = str(tmp_path / "v63.db")
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE schema_version (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            version INTEGER NOT NULL
        );
        CREATE TABLE epic_ticks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            epic_ref TEXT NOT NULL,
            tick_ts TEXT NOT NULL,
            snapshot TEXT NOT NULL,
            actions TEXT NOT NULL,
            snapshot_hash TEXT NOT NULL
        );
    """)
    conn.execute("INSERT INTO schema_version (id, version) VALUES (1, 63)")
    conn.execute(
        "INSERT INTO epic_ticks"
        " (epic_ref, tick_ts, snapshot, actions, snapshot_hash)"
        " VALUES ('e', '2026-10-06T00:00:00+00:00', '{}', '{}', 'deadbeef')"
    )
    conn.commit()
    conn.close()

    with SqliteRunStore(db_path) as migrated:
        version = migrated._conn.execute(
            "SELECT version FROM schema_version"
        ).fetchone()["version"]
        assert version == CURRENT_SCHEMA_VERSION
        # The pre-migration journal row is preserved, never rewritten.
        assert migrated.count_epic_ticks("e") == 1
        assert migrated.latest_epic_tick("e").snapshot_hash == "deadbeef"

        cols = {
            r["name"]
            for r in migrated._conn.execute("PRAGMA table_info(epic_tick_log)")
        }
        assert cols == {
            "id",
            "epic_ref",
            "ts_started",
            "ts_finished",
            "outcome",
            "signal",
            "n_dispatched",
            "n_merged",
            "n_held",
            "n_stuck",
            "journal_tick_id",
            "error_class",
            "journaled",
        }
        indexes = {
            r["name"]
            for r in migrated._conn.execute("PRAGMA index_list(epic_tick_log)")
        }
        assert "idx_epic_tick_log_ref_id" in indexes
        # An empty log on a migrated DB, not a backfill: the ticks that ran
        # before this table existed cannot be reconstructed, and inventing
        # rows for them would be repairing history.
        assert migrated.list_epic_tick_log_tail("e") == []

        row_id = migrated.begin_epic_tick_log("e", "2026-10-07T00:00:00+00:00")
        migrated.finish_epic_tick_log(
            row_id,
            ts_finished="2026-10-07T00:00:01+00:00",
            outcome="failed",
            error_class="BrokenPipeError",
            journaled=False,
        )
        logged = migrated.list_epic_tick_log_tail("e")
        assert [(r.outcome, r.error_class) for r in logged] == [
            ("failed", "BrokenPipeError")
        ]


def test_v70_adds_merge_state_reads_and_preserves_pre_v70_rows(
    tmp_path: Path,
) -> None:
    """#2879. A pre-v70 DB migrates without losing its journal rows, and gains
    the mutable table a quiet tick writes its merge-state read time to."""
    import sqlite3

    db_path = str(tmp_path / "v68.db")
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE schema_version (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            version INTEGER NOT NULL
        );
        CREATE TABLE epic_ticks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            epic_ref TEXT NOT NULL,
            tick_ts TEXT NOT NULL,
            snapshot TEXT NOT NULL,
            actions TEXT NOT NULL,
            snapshot_hash TEXT NOT NULL
        );
    """)
    conn.execute("INSERT INTO schema_version (id, version) VALUES (1, 68)")
    conn.execute(
        "INSERT INTO epic_ticks"
        " (epic_ref, tick_ts, snapshot, actions, snapshot_hash)"
        " VALUES ('e', '2026-10-06T00:00:00+00:00', '{}', '{}', 'deadbeef')"
    )
    conn.commit()
    conn.close()

    with SqliteRunStore(db_path) as migrated:
        version = migrated._conn.execute(
            "SELECT version FROM schema_version"
        ).fetchone()["version"]
        assert version == CURRENT_SCHEMA_VERSION
        assert migrated.count_epic_ticks("e") == 1
        assert migrated.latest_epic_tick("e").snapshot_hash == "deadbeef"
        cols = {
            r["name"]
            for r in migrated._conn.execute("PRAGMA table_info(epic_merge_state_reads)")
        }
        assert cols == {
            "epic_ref",
            "node_id",
            "head_oid",
            "merge_state",
            "merge_state_at",
        }
        # Empty on a migrated DB: readers fall back to the snapshot's stamp.
        assert (
            migrated._conn.execute(
                "SELECT COUNT(*) FROM epic_merge_state_reads"
            ).fetchone()[0]
            == 0
        )


def test_an_open_row_is_abandoned_only_by_a_later_row_of_its_own_epic(
    store: SqliteRunStore,
) -> None:
    """Reviewer finding (PR #2438): nothing closed the row of a driver that
    died mid-tick, so every strip painted that attempt as a tick still running,
    permanently. The epic's next tick closes it — bounded by row id, which is
    the only bound that needs no clock.
    """
    dead = store.begin_epic_tick_log("e", "2026-10-07T00:00:00+00:00")
    done = store.begin_epic_tick_log("e", "2026-10-07T00:00:30+00:00")
    store.finish_epic_tick_log(
        done, ts_finished="2026-10-07T00:00:31+00:00", outcome="dispatched"
    )
    elsewhere = store.begin_epic_tick_log("other", "2026-10-07T00:00:40+00:00")
    mine = store.begin_epic_tick_log("e", "2026-10-07T00:01:00+00:00")

    closed = store.abandon_open_epic_tick_logs(
        "e", before_id=mine, ts_finished="2026-10-07T00:01:00+00:00"
    )
    assert closed == 1

    rows = {r.id: r for r in store.list_epic_tick_log_tail("e", limit=10)}
    assert (rows[dead].outcome, rows[dead].ts_finished) == (
        "abandoned",
        "2026-10-07T00:01:00+00:00",
    )
    # The sweeper's OWN row is still open — it is the tick doing the sweeping.
    assert rows[mine].outcome is None
    assert rows[mine].ts_finished is None
    # A row that closed itself keeps the outcome it reported.
    assert rows[done].outcome == "dispatched"
    # Another epic's open row is another driver's business: this one holds only
    # its own epic's lock, so it may say nothing about that row.
    other = store.list_epic_tick_log_tail("other", limit=10)
    assert [(r.id, r.outcome) for r in other] == [(elsewhere, None)]

    # Idempotent: a second sweep finds nothing left to close.
    assert (
        store.abandon_open_epic_tick_logs(
            "e", before_id=mine, ts_finished="2026-10-07T00:02:00+00:00"
        )
        == 0
    )


def test_reached_tick_marks_only_an_open_attempt_and_survives_a_bare_close(
    store: SqliteRunStore,
) -> None:
    """#2882: the mark ``begin_epic_tick`` leaves on an open attempt. It moves
    the strip stamp (it lands between the open and the close), is never served
    as a signal, survives a close that records no signal (a tick that raised
    after it began) and the abandoned sweep, and is replaced by the real
    signal of a tick that finished. A closed row cannot be marked after the
    fact."""
    stamp = store.epic_tick_log_stamp
    raised = store.begin_epic_tick_log("e", "2026-10-07T00:00:00+00:00")
    before = stamp("e")
    store.mark_epic_tick_log_reached(raised)
    assert stamp("e") != before
    store.finish_epic_tick_log(
        raised,
        ts_finished="2026-10-07T00:00:01+00:00",
        outcome="failed",
        error_class="RuntimeError",
    )

    refused = store.begin_epic_tick_log("e", "2026-10-07T00:01:00+00:00")
    store.finish_epic_tick_log(
        refused, ts_finished="2026-10-07T00:01:01+00:00", outcome="locked_out"
    )
    store.mark_epic_tick_log_reached(refused)

    finished = store.begin_epic_tick_log("e", "2026-10-07T00:02:00+00:00")
    store.mark_epic_tick_log_reached(finished)
    store.finish_epic_tick_log(
        finished,
        ts_finished="2026-10-07T00:02:01+00:00",
        outcome="nothing",
        signal="progressing",
    )

    dead = store.begin_epic_tick_log("e", "2026-10-07T00:03:00+00:00")
    store.mark_epic_tick_log_reached(dead)
    blocked = store.begin_epic_tick_log("e", "2026-10-07T00:04:00+00:00")
    store.abandon_open_epic_tick_logs(
        "e", before_id=blocked, ts_finished="2026-10-07T00:04:01+00:00"
    )

    rows = {r.id: r for r in store.list_epic_tick_log_tail("e", limit=10)}
    assert {i: (r.outcome, r.reached_tick, r.signal) for i, r in rows.items()} == {
        raised: ("failed", True, None),
        refused: ("locked_out", False, None),
        finished: ("nothing", True, "progressing"),
        dead: ("abandoned", True, None),
        blocked: (None, False, None),
    }
    assert store.latest_epic_tick_attempts(["e"])["e"].reached_tick is False
    assert store.epic_tick_log_stamps(["e"])["e"] == stamp("e")


def test_attempt_stamp_moves_for_every_attempt_a_heartbeat_cannot_see(
    store: SqliteRunStore,
) -> None:
    """Reviewer finding (PR #2438, round 3). The strip readers re-read a strip
    when a stamp moves, and the only stamps on offer were written by
    ``begin_epic_tick`` — the far side of the lock, the preflight and the
    non-empty-graph guard. This is the stamp that moves for the attempts those
    guards turn away, which is every attempt a strip exists to show.
    """
    assert store.epic_tick_log_stamp("e") == ""
    assert store.epic_tick_log_stamps(["e"]) == {}

    stamp = store.epic_tick_log_stamp
    first = store.begin_epic_tick_log("e", "2026-10-07T00:00:00+00:00")
    after_open = stamp("e")
    assert after_open != ""

    # Closing the row moves it again: the slot the strip painted live now has
    # an outcome, and nothing about the row's id changed.
    store.finish_epic_tick_log(
        first, ts_finished="2026-10-07T00:00:01+00:00", outcome="locked_out"
    )
    after_close = stamp("e")
    assert after_close != after_open

    # A second attempt — and the whole point: NOTHING on epic_driver_state has
    # moved through any of this, because no tick got past the lock.
    second = store.begin_epic_tick_log("e", "2026-10-07T00:00:30+00:00")
    assert stamp("e") not in (after_open, after_close)
    assert store.get_epic_driver_state("e") is None

    # The abandoned sweep moves it too, and that one moves no row id at all.
    third = store.begin_epic_tick_log("e", "2026-10-07T00:01:00+00:00")
    before_sweep = stamp("e")
    assert (
        store.abandon_open_epic_tick_logs(
            "e", before_id=third, ts_finished="2026-10-07T00:01:01+00:00"
        )
        == 1
    )
    assert stamp("e") != before_sweep
    assert second != third

    # One epic's attempts never stamp another's.
    store.begin_epic_tick_log("other", "2026-10-07T00:02:00+00:00")
    batched = store.epic_tick_log_stamps(["e", "other"])
    assert batched["e"] == stamp("e")
    assert batched["other"] == stamp("other")
    assert batched["e"] != batched["other"]


def test_migration_step_count_matches_the_version_constant() -> None:
    """The three edit points of a migration stay in lockstep."""
    steps = [
        name
        for name in dir(SqliteRunStore)
        if name.startswith("_apply_v") and name[len("_apply_v") :].isdigit()
    ]
    assert len(steps) == CURRENT_SCHEMA_VERSION


def _append_change(store: SqliteRunStore, tick_ts: str, status: str) -> int:
    snapshot = {**SNAP_A, "nodes": [{**SNAP_A["nodes"][0], "status": status}]}
    actions = {**ACTIONS, "dispatched": ["1"]}
    tick_id, inserted = store.append_epic_tick(
        epic_ref="e",
        tick_ts=tick_ts,
        snapshot=snapshot,
        actions=actions,
        snapshot_hash=f"hash-{status}",
    )
    assert inserted
    return tick_id


def _closed_attempt(
    store: SqliteRunStore, stamp: str, *, journal_tick_id: int | None = None
) -> int:
    row = store.begin_epic_tick_log("e", stamp)
    store.finish_epic_tick_log(
        row,
        ts_finished=stamp,
        outcome="nothing",
        signal="progressing",
        journal_tick_id=journal_tick_id,
    )
    return row


def test_a_change_whose_tick_log_begin_failed_still_draws_one_mark(
    store: SqliteRunStore,
) -> None:
    """#2882: the journal append is committed, the tick-log writes around it
    are best-effort. A tick whose ``begin_epic_tick_log`` failed leaves no
    attempt row at all, yet CHANGE counts its journal row — so the change strip
    reads the journal, and draws that change as one closed mark."""
    tick_id = _append_change(store, "2026-07-02T00:00:10+00:00", "in_progress")
    # The next tick changed nothing and carries the same journal id.
    _closed_attempt(store, "2026-07-02T00:01:00+00:00", journal_tick_id=tick_id)

    marks = store.list_epic_tick_log_marks("e")
    assert [(m.journal_tick_id, m.outcome) for m in marks] == [(tick_id, "dispatched")]
    (mark,) = marks
    assert mark.ts_started == mark.ts_finished == "2026-07-02T00:00:10+00:00"
    assert mark.n_dispatched == 1
    assert mark.id < 0  # never an attempt row's id


def test_a_change_whose_tick_log_finish_failed_still_draws_one_mark(
    store: SqliteRunStore,
) -> None:
    """#2882: the attempt row opened, the journal appended, and the finish
    write failed — so the row carries no journal id. Once the next tick sweeps
    it as abandoned it is no mark of its own; the journal row is the one."""
    opened = store.begin_epic_tick_log("e", "2026-07-02T00:00:00+00:00")
    tick_id = _append_change(store, "2026-07-02T00:00:10+00:00", "in_progress")
    later = _closed_attempt(store, "2026-07-02T00:01:00+00:00", journal_tick_id=tick_id)
    store.abandon_open_epic_tick_logs(
        "e", before_id=later, ts_finished="2026-07-02T00:01:00+00:00"
    )

    marks = store.list_epic_tick_log_marks("e")
    assert [(m.journal_tick_id, m.ts_started) for m in marks] == [
        (tick_id, "2026-07-02T00:00:10+00:00")
    ]
    assert opened not in {m.id for m in marks}


def test_a_change_the_log_closed_is_drawn_as_its_own_attempt(
    store: SqliteRunStore,
) -> None:
    """Where the attempt that appended the change closed normally, the mark is
    that attempt — one per change, merged in time order with a failed tick."""
    row = store.begin_epic_tick_log("e", "2026-07-02T00:00:00+00:00")
    tick_id = _append_change(store, "2026-07-02T00:00:10+00:00", "in_progress")
    store.finish_epic_tick_log(
        row,
        ts_finished="2026-07-02T00:00:20+00:00",
        outcome="dispatched",
        signal="progressing",
        n_dispatched=1,
        journal_tick_id=tick_id,
        journaled=True,
    )
    failed = store.begin_epic_tick_log("e", "2026-07-02T00:01:00+00:00")
    store.finish_epic_tick_log(
        failed,
        ts_finished="2026-07-02T00:01:01+00:00",
        outcome="failed",
        journaled=False,
    )
    begin_lost = _append_change(store, "2026-07-02T00:02:10+00:00", "done")

    marks = store.list_epic_tick_log_marks("e")
    assert [(m.id, m.journal_tick_id, m.outcome) for m in marks] == [
        (row, tick_id, "dispatched"),
        (failed, None, "failed"),
        (-begin_lost, begin_lost, "dispatched"),
    ]
    assert [
        m.journal_tick_id for m in store.list_epic_tick_log_marks("e", limit=1)
    ] == [begin_lost]


def test_a_malformed_journal_row_is_drawn_quietly_not_raised(
    store: SqliteRunStore,
) -> None:
    """A historical journal row is read as stored: actions that are not JSON
    count nothing and an unreadable stamp sorts oldest — the read never
    raises, and the row still draws its one mark."""
    store._conn.execute(
        "INSERT INTO epic_ticks (epic_ref, tick_ts, snapshot, actions, snapshot_hash)"
        " VALUES ('e', 'not a stamp', 'nope', '{broken', 'h')"
    )
    store._conn.commit()
    tick_id = _append_change(store, "2026-07-02T00:00:10+00:00", "in_progress")

    marks = store.list_epic_tick_log_marks("e")
    assert [(m.ts_started, m.outcome, m.signal) for m in marks] == [
        ("not a stamp", "nothing", None),
        ("2026-07-02T00:00:10+00:00", "dispatched", "progressing"),
    ]
    assert marks[1].journal_tick_id == tick_id


def test_latest_attempts_read_only_the_refs_passed(store: SqliteRunStore) -> None:
    """#2900 C4: the roster's attempts read is bounded to the roster. An
    archived epic's newest attempt is made unreadable — a count that is not a
    number — so reading it at all would raise; the read for the live refs
    neither raises nor returns it, and still answers each ref with its own
    newest attempt."""
    store.begin_epic_tick_log("live", "2026-10-07T00:00:00+00:00")
    newest = store.begin_epic_tick_log("live", "2026-10-07T00:01:00+00:00")
    other = store.begin_epic_tick_log("other", "2026-10-07T00:02:00+00:00")
    archived = store.begin_epic_tick_log("archived", "2026-10-07T00:03:00+00:00")
    store._conn.execute(
        "UPDATE epic_tick_log SET n_dispatched = 'not a number' WHERE id = ?",
        (archived,),
    )

    attempts = store.latest_epic_tick_attempts(["live", "other", "never-ticked"])

    assert {ref: entry.id for ref, entry in attempts.items()} == {
        "live": newest,
        "other": other,
    }
    assert store.latest_epic_tick_attempts([]) == {}
    with pytest.raises(ValueError, match="not a number"):
        store.latest_epic_tick_attempts(["archived"])


def test_attempt_stamps_read_only_the_refs_passed(store: SqliteRunStore) -> None:
    """#2900 C4: the roster's stamp read is bounded to the roster, the same
    way its attempts read is — a ref not passed is not stamped, and an empty
    roster asks nothing."""
    store.begin_epic_tick_log("live", "2026-10-07T00:00:00+00:00")
    store.begin_epic_tick_log("archived", "2026-10-07T00:01:00+00:00")

    stamps = store.epic_tick_log_stamps(["live", "never-ticked"])

    assert stamps == {"live": store.epic_tick_log_stamp("live")}
    assert store.epic_tick_log_stamps([]) == {}


@pytest.mark.parametrize(
    "quiet_stamp",
    ["2026-07-02T01:00:00+00:00", "not a stamp"],
    ids=["readable", "unreadable"],
)
def test_pruning_the_change_attempt_never_promotes_a_quiet_tick(
    store: SqliteRunStore, quiet_stamp: str
) -> None:
    """#2949: every quiet tick after a change carries the change's journal id.
    Once the cap prunes the attempt that appended it, the oldest kept quiet
    tick is still no change: the strip draws the journal row itself, once."""
    row = store.begin_epic_tick_log("e", "2026-07-02T00:00:00+00:00")
    tick_id = _append_change(store, "2026-07-02T00:00:10+00:00", "in_progress")
    store.finish_epic_tick_log(
        row,
        ts_finished="2026-07-02T00:00:20+00:00",
        outcome="dispatched",
        signal="progressing",
        n_dispatched=1,
        journal_tick_id=tick_id,
        journaled=True,
    )
    quiet = []
    for _ in range(EPIC_TICK_LOG_CAP + 5):
        attempt = store.begin_epic_tick_log("e", quiet_stamp)
        store.finish_epic_tick_log(
            attempt,
            ts_finished=quiet_stamp,
            outcome="nothing",
            signal="progressing",
            journal_tick_id=tick_id,
            journaled=False,
        )
        quiet.append(attempt)
    assert row not in {a.id for a in store.list_epic_tick_log_tail("e", limit=500)}

    marks = store.list_epic_tick_log_marks("e")
    assert [(m.id, m.journal_tick_id, m.outcome) for m in marks] == [
        (-tick_id, tick_id, "dispatched")
    ]
    assert not {m.id for m in marks} & set(quiet)


def test_v75_marks_the_first_attempt_per_journal_row_as_the_change(
    tmp_path: Path,
) -> None:
    """#2949. A pre-v75 tick log gains ``journaled``, backfilled as the strip
    read it before: the first kept attempt carrying each journal id."""
    db_path = str(tmp_path / "dispatch.db")
    SqliteRunStore(db_path).close()
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.execute("ALTER TABLE epic_tick_log DROP COLUMN journaled")
    conn.execute("UPDATE schema_version SET version = 74")
    for epic, journal_id in [("e", 7), ("e", 7), ("f", 7), ("e", None), ("e", 9)]:
        conn.execute(
            "INSERT INTO epic_tick_log"
            " (epic_ref, ts_started, ts_finished, outcome, journal_tick_id)"
            " VALUES (?, '2026-07-02T00:00:00+00:00',"
            " '2026-07-02T00:00:01+00:00', 'nothing', ?)",
            (epic, journal_id),
        )
    conn.commit()
    conn.close()

    with SqliteRunStore(db_path) as migrated:
        rows = migrated._conn.execute(
            "SELECT epic_ref, journal_tick_id, journaled FROM epic_tick_log ORDER BY id"
        ).fetchall()
    assert [tuple(r) for r in rows] == [
        ("e", 7, 1),
        ("e", 7, 0),
        ("f", 7, 1),
        ("e", None, 0),
        ("e", 9, 1),
    ]


@pytest.mark.parametrize("stored", ["{not json", "null", "[1]"])
def test_the_predecessor_snapshot_of_an_unreadable_row_is_none_and_said(
    store: SqliteRunStore, stored: str, caplog: pytest.LogCaptureFixture
) -> None:
    """#2949: the driver's predecessor read decodes only the snapshot, and a
    latest row whose snapshot is not a JSON object reads as no predecessor,
    with a warning naming the row, where the full row read raises or returns
    a non-snapshot."""
    tick_id, _ = store.append_epic_tick(
        epic_ref="e",
        tick_ts="2026-07-02T00:00:00+00:00",
        snapshot=SNAP_A,
        actions=ACTIONS,
        snapshot_hash="h",
    )
    store._conn.execute(
        "UPDATE epic_ticks SET actions = '{not json' WHERE id = ?", (tick_id,)
    )
    store._conn.commit()
    assert store.latest_epic_tick_snapshot("e") == SNAP_A
    assert store.latest_epic_tick_snapshot("absent") is None

    store._conn.execute(
        "UPDATE epic_ticks SET snapshot = ? WHERE id = ?", (stored, tick_id)
    )
    store._conn.commit()
    with caplog.at_level("WARNING", logger="kew.run_store"):
        assert store.latest_epic_tick_snapshot("e") is None
    assert f"epic_ticks row {tick_id}" in caplog.text
