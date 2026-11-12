"""The drive's heartbeat, `kew drive status`, and the console's driver health (#2617).

The acceptance: a stalled drive (heartbeat older than its interval x3) reads
gone; a busy one reads busy; archived epics never count. Every test runs on a
real ledger.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import kew.cli as cli
import kew.cli.drive as drive_cli
from kew.cli import app
from kew.config import DispatchConfig, ProjectConfig
from kew.drive import DriveHolder, acquire_drive, current_holder, request_stop
from kew.drive_heartbeat import (
    DRIVE_HEARTBEAT_KEY,
    DRIVE_HOLDER_KEY,
    DriveEpicTick,
    DriveHeartbeat,
    last_drive_heartbeat,
    read_drive_heartbeat,
)
from kew.epic_archive import EPIC_ARCHIVED_EVENT
from kew.run_store import SqliteRunStore
from kew.service.dashboard import _banner
from kew.service.epics import driver_liveness, list_epics
from kew.service.health import ProbeCache, assemble_health

runner = CliRunner()

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
EVERY_S = 30.0


@pytest.fixture
def store(tmp_path: Path) -> Any:
    with SqliteRunStore(str(tmp_path / "dispatch.db")) as s:
        yield s


def _seed_tick(store: Any, ref: str, *, at: datetime, signal: str = "progressing"):
    """One finished tick of ``ref`` at ``at``, recorded on a 60s cadence."""
    stamp = at.isoformat()
    tick_id, _ = store.append_epic_tick(
        epic_ref=ref,
        tick_ts=stamp,
        snapshot={"signal": signal, "counts": {}, "nodes": [], "edges": []},
        actions={},
        snapshot_hash=f"h-{ref}-{stamp}",
    )
    store.begin_epic_tick(ref, stamp)
    store.finish_epic_tick(
        ref,
        tick_ts=stamp,
        duration_s=1.0,
        interval_s=60.0,
        signal=signal,
        tick_id=tick_id,
    )


def _hold(store: Any, *, token: str = "tok", beat: datetime = NOW) -> None:
    holder = DriveHolder(
        pid=os.getpid(), host="h", token=token, started_at=beat, heartbeat=beat
    )
    store.set_meta(DRIVE_HOLDER_KEY, holder.encode())


def _beat(
    store: Any,
    *,
    age_s: float,
    rotation: tuple[str, ...],
    token: str = "tok",
    epic: str | None = None,
) -> None:
    beat = DriveHeartbeat(
        token=token,
        pid=os.getpid(),
        host="h",
        at=NOW - timedelta(seconds=age_s),
        every_s=EVERY_S,
        stage="ticking" if epic else "sleeping",
        epic=epic,
        rotation=rotation,
    )
    store.set_meta(DRIVE_HEARTBEAT_KEY, beat.encode())


def _archive(store: Any, ref: str) -> None:
    store.append_audit_event(
        timestamp="2026-10-01T00:00:00+00:00",
        event=EPIC_ARCHIVED_EVENT,
        actor_type="human",
        actor_id="owner",
        payload={"epic_ref": ref, "reason": "retired"},
    )


def _health(store: Any) -> dict[str, Any]:
    from test_service_health import _probes

    return assemble_health(
        store, DispatchConfig(), cache=ProbeCache(_probes()), now=NOW
    )


# --------------------------------------------------------------------------- #
# The reading rule
# --------------------------------------------------------------------------- #


def test_an_epic_waiting_its_turn_in_a_running_drive_reads_busy(store: Any) -> None:
    """Its own tick is ten minutes old — gone on its own stamps — but the drive
    that ticks it beat 10s ago: it is waiting its turn, not abandoned."""
    _seed_tick(store, "epic:a", at=NOW - timedelta(minutes=10))
    assert driver_liveness(store, "epic:a", now=NOW).status == "gone"

    _hold(store)
    _beat(store, age_s=10, rotation=("epic:a",))
    reading = driver_liveness(store, "epic:a", now=NOW)

    assert reading.status == "busy"
    assert reading.driver_gone is False
    assert reading.drive == "live"


def test_an_epic_ticked_inside_its_cadence_stays_live_under_the_drive(
    store: Any,
) -> None:
    _seed_tick(store, "epic:a", at=NOW - timedelta(seconds=20))
    _hold(store)
    _beat(store, age_s=5, rotation=("epic:a",))

    assert driver_liveness(store, "epic:a", now=NOW).status == "live"


@pytest.mark.parametrize(
    ("age_s", "status"),
    [(3 * EVERY_S - 1, "busy"), (3 * EVERY_S + 1, "gone")],
    ids=["inside-three-intervals", "past-three-intervals"],
)
def test_a_stalled_drive_reads_gone_past_three_of_its_intervals(
    store: Any, age_s: float, status: str
) -> None:
    """The stall line is the drive's own write interval x3. An epic whose own
    tick is still inside its cadence stays live: that tick is direct evidence
    something drives it, whatever the drive's heartbeat says."""
    _seed_tick(store, "epic:a", at=NOW - timedelta(minutes=10))
    _seed_tick(store, "epic:b", at=NOW - timedelta(seconds=10))
    _hold(store)
    _beat(store, age_s=age_s, rotation=("epic:a", "epic:b"))

    assert driver_liveness(store, "epic:a", now=NOW).status == status
    assert driver_liveness(store, "epic:b", now=NOW).status == "live"


def test_a_heartbeat_from_a_drive_that_no_longer_holds_the_lock_vouches_nothing(
    store: Any,
) -> None:
    _seed_tick(store, "epic:a", at=NOW - timedelta(minutes=10))
    _beat(store, age_s=1, rotation=("epic:a",), token="old")

    assert read_drive_heartbeat(store) is None  # no holder at all
    _hold(store, token="new")
    assert read_drive_heartbeat(store) is None  # another drive holds it
    assert last_drive_heartbeat(store) is not None
    assert driver_liveness(store, "epic:a", now=NOW).status == "gone"


def test_an_epic_outside_the_rotation_keeps_its_own_reading(store: Any) -> None:
    """Paused, stopped and archived epics are not in the rotation: the drive
    is not ticking them, so it does not vouch for them."""
    _seed_tick(store, "epic:paused", at=NOW - timedelta(minutes=10))
    _hold(store)
    _beat(store, age_s=1, rotation=("epic:a",))

    assert driver_liveness(store, "epic:paused", now=NOW).status == "gone"


def test_an_unreadable_heartbeat_row_is_no_heartbeat(store: Any) -> None:
    _hold(store)
    store.set_meta(DRIVE_HEARTBEAT_KEY, "{not json")
    assert read_drive_heartbeat(store) is None
    store.set_meta(
        DRIVE_HEARTBEAT_KEY,
        json.dumps(
            {"token": "tok", "pid": 1, "host": "h", "at": NOW.isoformat()}
            | {"every_s": 0, "stage": "sleeping"}
        ),
    )
    assert read_drive_heartbeat(store) is None


# --------------------------------------------------------------------------- #
# The console: /health, the header banner, the Epics list
# --------------------------------------------------------------------------- #


def test_health_reads_busy_for_a_running_drive_and_never_counts_archived(
    store: Any,
) -> None:
    for ref in ("epic:a", "epic:b", "epic:old", "epic:done"):
        _seed_tick(store, ref, at=NOW - timedelta(minutes=10))
    _seed_tick(store, "epic:done", at=NOW - timedelta(minutes=9), signal="complete")
    _archive(store, "epic:old")
    _hold(store)
    _beat(store, age_s=5, rotation=("epic:a", "epic:b", "epic:done"), epic="epic:b")

    reading = _health(store)

    rows = {row["epic_ref"]: row for row in reading["drivers"]}
    assert rows["epic:a"]["liveness"] == "busy"
    assert rows["epic:b"]["liveness"] == "busy"
    assert rows["epic:a"]["drive"] == "live"
    assert rows["epic:old"]["active"] is False
    assert rows["epic:done"]["active"] is False
    down = [r for r in rows.values() if r["active"] and r["liveness"] == "gone"]
    assert down == []
    assert reading["drive"]["status"] == "live"
    assert reading["drive"]["stage"] == "ticking"
    assert reading["drive"]["epic"] == "epic:b"
    assert reading["drive"]["stall_after_s"] == 3 * EVERY_S


def test_health_reads_gone_for_a_stalled_drive(store: Any) -> None:
    _seed_tick(store, "epic:a", at=NOW - timedelta(minutes=10))
    _hold(store)
    _beat(store, age_s=3 * EVERY_S + 5, rotation=("epic:a",))

    reading = _health(store)

    (row,) = reading["drivers"]
    assert row["liveness"] == "gone"
    assert row["active"] is True
    assert reading["drive"]["status"] == "gone"


def test_health_has_no_drive_block_without_a_drive(store: Any) -> None:
    assert _health(store)["drive"] is None


def test_the_header_banner_sees_a_running_drive(store: Any) -> None:
    _seed_tick(store, "epic:a", at=NOW - timedelta(minutes=10))
    assert _banner(store, NOW).no_live_driver is True

    _hold(store)
    _beat(store, age_s=5, rotation=("epic:a",))
    assert _banner(store, NOW).no_live_driver is False

    _beat(store, age_s=3 * EVERY_S + 5, rotation=("epic:a",))
    assert _banner(store, NOW).no_live_driver is True


def test_the_epics_list_reads_the_drive_too(store: Any) -> None:
    _seed_tick(store, "epic:a", at=NOW - timedelta(minutes=10))
    _hold(store)
    _beat(store, age_s=5, rotation=("epic:a",))

    (row,) = list_epics(store, now=NOW)

    assert row.driver_status == "busy"


# --------------------------------------------------------------------------- #
# The drive writes it
# --------------------------------------------------------------------------- #


def _config(tmp_path: Path) -> Any:
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    with SqliteRunStore(str(log_dir / "dispatch.db")) as s:
        for ref in ("epic:a", "epic:b"):
            s.register_epic(ref)
    cfg = DispatchConfig(project=ProjectConfig(repo="o/r"))
    cfg.logging.dir = str(log_dir)
    return cfg


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_sleep", lambda _s: None)


def test_the_drive_writes_its_stage_rotation_and_each_tick(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    db = str(Path(cfg.logging.dir) / "dispatch.db")
    seen: list[DriveHeartbeat | None] = []

    with SqliteRunStore(db) as ledger:

        def tick(name: str, _admit_cap: int) -> int:
            seen.append(read_drive_heartbeat(ledger))
            if name == "epic:b":
                holder = current_holder(ledger)
                assert holder is not None
                request_stop(ledger, holder, None)
            return 10

        drive_cli.run_drive(cfg, tick, interval=1)
        final = last_drive_heartbeat(ledger)
        assert read_drive_heartbeat(ledger) is None  # the lock is released

    first, second = seen
    assert first is not None and second is not None
    assert (first.stage, first.epic) == ("ticking", "epic:a")
    assert first.rotation == ("epic:a", "epic:b")
    assert first.ticks["epic:a"].finished_at is None
    assert (second.stage, second.epic) == ("ticking", "epic:b")
    assert second.ticks["epic:a"].exit_code == 10
    assert final is not None and final.stage == "stopped"
    assert set(final.ticks) == {"epic:a", "epic:b"}
    assert final.every_s == drive_cli._HEARTBEAT_EVERY_S


def test_a_paused_epic_leaves_the_heartbeat_s_rotation(tmp_path: Path) -> None:
    from kew.drive import pause

    cfg = _config(tmp_path)
    db = str(Path(cfg.logging.dir) / "dispatch.db")
    seen: list[DriveHeartbeat | None] = []
    with SqliteRunStore(db) as ledger:
        pause(ledger, "epic:b")

        def tick(_name: str, _admit_cap: int) -> int:
            seen.append(read_drive_heartbeat(ledger))
            holder = current_holder(ledger)
            assert holder is not None
            request_stop(ledger, holder, None)
            return 10

        drive_cli.run_drive(cfg, tick, interval=1)

    (beat,) = seen
    assert beat is not None and beat.rotation == ("epic:a",)


# --------------------------------------------------------------------------- #
# kew drive status
# --------------------------------------------------------------------------- #


def _wire(monkeypatch: pytest.MonkeyPatch, cfg: Any) -> None:
    monkeypatch.setattr(cli, "load_config", lambda **_kw: cfg)


def _ledger(cfg: Any) -> SqliteRunStore:
    return SqliteRunStore(str(Path(cfg.logging.dir) / "dispatch.db"))


def test_status_prints_a_running_drive_s_heartbeat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _config(tmp_path)
    _wire(monkeypatch, cfg)
    with _ledger(cfg) as ledger:
        lease = acquire_drive(ledger)
        assert not isinstance(lease, type(None))
        holder = current_holder(ledger)
        assert holder is not None
        beat = DriveHeartbeat(
            token=holder.token,
            pid=holder.pid,
            host=holder.host,
            at=datetime.now(UTC),
            every_s=EVERY_S,
            stage="ticking",
            epic="epic:b",
            rotation=("epic:a", "epic:b"),
            ticks={
                "epic:a": DriveEpicTick(
                    started_at=datetime.now(UTC).isoformat(),
                    finished_at=datetime.now(UTC).isoformat(),
                    exit_code=10,
                )
            },
        )
        ledger.set_meta(DRIVE_HEARTBEAT_KEY, beat.encode())

    result = runner.invoke(app, ["drive", "status"])
    assert result.exit_code == 0, result.output
    assert "kew drive: running" in result.output
    assert "stage: ticking epic:b" in result.output
    assert "rotation: epic:a, epic:b" in result.output
    assert "exit 10" in result.output

    result = runner.invoke(app, ["drive", "status", "--json"])
    assert result.exit_code == 0, result.output
    doc = json.loads(result.output)
    assert doc["state"] == "running"
    assert doc["heartbeat"]["epic"] == "epic:b"
    assert doc["heartbeat"]["stall_after_s"] == 3 * EVERY_S


def test_status_reads_gone_for_a_stalled_drive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _config(tmp_path)
    _wire(monkeypatch, cfg)
    with _ledger(cfg) as ledger:
        acquire_drive(ledger)
        holder = current_holder(ledger)
        assert holder is not None
        stale = DriveHeartbeat(
            token=holder.token,
            pid=holder.pid,
            host=holder.host,
            at=datetime.now(UTC) - timedelta(seconds=3 * EVERY_S + 10),
            every_s=EVERY_S,
            stage="ticking",
            epic="epic:a",
        )
        ledger.set_meta(DRIVE_HEARTBEAT_KEY, stale.encode())

    result = runner.invoke(app, ["drive", "status"])
    assert result.exit_code == 1, result.output
    assert "kew drive: gone" in result.output
    assert "stalled after 90s" in result.output


def test_status_with_no_drive_running_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _config(tmp_path)
    _wire(monkeypatch, cfg)

    result = runner.invoke(app, ["drive", "status"])

    assert result.exit_code == 1, result.output
    assert "kew drive: not running" in result.output
