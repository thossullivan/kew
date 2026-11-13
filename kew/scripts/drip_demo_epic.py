"""Replay the demo-epic fixture into a live dispatch.db one event at a time.

`make_demo_epic.py` writes a *completed* drain; this script performs it live:
runs and journal ticks from a source db are copied into a serve-watched db in
timestamp order, so `/epics/demo-epic` advances in front of the viewer — runs
land first (the cost/telemetry layer moves), then the tick that observed them
journals (status colors move). This is the capture source for live-mode demo
recordings; replay-mode recordings need only the completed db.

All timestamps (runs.started_at/finished_at, epic_ticks.tick_ts, snapshot
status_since, heartbeat) are shifted by a constant delta so the drain appears
to be happening now: staleness reads fresh and drain-window economics
accumulate as frames land. snapshot_hash is recomputed after the
status_since rewrite so stored hashes stay truthful to their content.

Usage (from the kew/ package dir):
    uv run python scripts/make_demo_epic.py --out /tmp/demo-src
    uv run python scripts/drip_demo_epic.py /tmp/demo-src/dispatch.db \\
        <serve-project>/logs/dispatch.db --interval 6
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from kew.graph.driver.journal import snapshot_hash  # noqa: E402
from kew.run_store import SqliteRunStore  # noqa: E402


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _apply_with_retry(dest: sqlite3.Connection, write: Callable[[], None]) -> None:
    """Run one event's writes + commit, retrying with backoff on SQLITE_BUSY.

    The dest db is actively polled by `kew serve`'s watcher; a checkpoint or
    concurrent write can briefly hold it. Retry a few times before aborting.
    """
    attempts = 4
    for attempt in range(attempts):
        try:
            write()
            dest.commit()
            return
        except sqlite3.OperationalError as exc:
            dest.rollback()
            if "locked" not in str(exc) or attempt == attempts - 1:
                raise
            time.sleep(0.5 * 2**attempt)


def _drip_events(
    src: sqlite3.Connection, dest: sqlite3.Connection, interval: float, src_path: str
) -> None:
    run_rows = src.execute(
        "SELECT * FROM runs WHERE finished_at IS NOT NULL ORDER BY finished_at"
    ).fetchall()
    tick_rows = src.execute("SELECT * FROM epic_ticks ORDER BY id").fetchall()
    if not tick_rows:
        raise SystemExit(
            f"no epic_ticks rows in {src_path} — run make_demo_epic.py first"
        )

    first_ts = min(
        [_parse(r["finished_at"]) for r in run_rows]
        + [_parse(r["tick_ts"]) for r in tick_rows]
    )
    delta = datetime.now(UTC) - first_ts

    def shift(ts: str | None) -> str | None:
        return (_parse(ts) + delta).isoformat() if ts else ts

    events = sorted(
        [("run", r["finished_at"], r) for r in run_rows]
        + [("tick", r["tick_ts"], r) for r in tick_rows],
        key=lambda e: e[1],
    )
    run_cols = [d[0] for d in src.execute("SELECT * FROM runs LIMIT 1").description]

    print(
        f"dripping {len(events)} events ({len(run_rows)} runs, "
        f"{len(tick_rows)} ticks) every {interval}s"
    )
    tick_count = 0
    for kind, _ts, row in events:
        if kind == "run":
            vals = {c: row[c] for c in run_cols}
            vals["started_at"] = shift(vals["started_at"])
            vals["finished_at"] = shift(vals["finished_at"])
            placeholders = ",".join("?" for _ in run_cols)

            def write_run(
                vals: dict[str, object] = vals, placeholders: str = placeholders
            ) -> None:
                dest.execute(
                    f"INSERT OR REPLACE INTO runs ({','.join(run_cols)})"
                    f" VALUES ({placeholders})",
                    [vals[c] for c in run_cols],
                )

            _apply_with_retry(dest, write_run)
            cost = row["estimated_cost_usd"] or 0.0
            print(f"  run  issue #{row['issue_num']}  {row['status']}  ${cost:.2f}")
        else:
            tick_count += 1
            snap = json.loads(row["snapshot"])
            for node in snap.get("nodes", []):
                if node.get("status_since"):
                    node["status_since"] = shift(node["status_since"])
            new_ts = shift(row["tick_ts"])
            signal = snap.get("signal", "progressing")

            def write_tick(
                row: sqlite3.Row = row,
                snap: dict = snap,  # type: ignore[type-arg]
                new_ts: str | None = new_ts,
                signal: str = signal,
                tick_count: int = tick_count,
            ) -> None:
                cur = dest.execute(
                    "INSERT INTO epic_ticks"
                    " (epic_ref, tick_ts, snapshot, actions, snapshot_hash)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (
                        row["epic_ref"],
                        new_ts,
                        json.dumps(snap, sort_keys=True),
                        row["actions"],
                        snapshot_hash(snap),
                    ),
                )
                dest.execute(
                    "INSERT INTO epic_driver_state (epic_ref, last_tick_started_at,"
                    " last_tick_ts, last_tick_duration_s, tick_count, interval_s,"
                    " last_signal, last_tick_id)"
                    " VALUES (?, ?, ?, 1.0, ?, ?, ?, ?)"
                    " ON CONFLICT(epic_ref) DO UPDATE SET"
                    " last_tick_started_at = excluded.last_tick_started_at,"
                    " last_tick_ts = excluded.last_tick_ts,"
                    " last_tick_duration_s = excluded.last_tick_duration_s,"
                    " tick_count = excluded.tick_count,"
                    " interval_s = excluded.interval_s,"
                    " last_signal = excluded.last_signal,"
                    " last_tick_id = excluded.last_tick_id",
                    (
                        row["epic_ref"],
                        new_ts,
                        new_ts,
                        tick_count,
                        interval,
                        signal,
                        cur.lastrowid,
                    ),
                )

            _apply_with_retry(dest, write_tick)
            print(f"  tick {tick_count}/{len(tick_rows)}  signal={signal}")
        time.sleep(interval)

    print(
        "drain complete — the epic shows STALLED (failed branch)"
        " and replay mode has the full journal"
    )


def _drip(src_path: str, dest_path: str, interval: float) -> None:
    SqliteRunStore(dest_path).close()  # migrate dest schema before raw inserts

    src = sqlite3.connect(src_path)
    src.row_factory = sqlite3.Row
    # Generous timeout: the serve poller reads this db every second.
    dest = sqlite3.connect(dest_path, timeout=15.0)
    try:
        _drip_events(src, dest, interval, src_path)
    finally:
        src.close()
        dest.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("src", help="completed demo db (from make_demo_epic.py)")
    parser.add_argument("dest", help="the serve project's dispatch.db to drip into")
    parser.add_argument("--interval", type=float, default=6.0, help="seconds per event")
    args = parser.parse_args()
    _drip(args.src, args.dest, args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
