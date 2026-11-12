"""The drive sweeps the reservations of every epic, ticked or not (#3192).

A reservation with no run row counts as a live fleet slot. Only its own
epic's tick ever reconciled it, so one under an epic nobody ticks held its
slot forever. These pin the sweep the drive runs every round, what it
releases and what it must leave alone, and the listing ``kew drive status``
prints of every reservation holding a slot.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import kew.cli as cli
import kew.cli.drive as drive_cli
from kew.cli import app
from kew.config import DispatchConfig, ProjectConfig
from kew.drive import current_holder, request_stop
from kew.epic_holds import PREEXISTING_WORKTREE_REFUSED_HOLD_PREFIX
from kew.graph.driver.reservation_sweep import sweep_reservations
from kew.graph.driver.tick import _RESERVATION_STARTUP_GRACE_SECONDS
from kew.run_store import RunRecord, SqliteRunStore

runner = CliRunner()

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
AUGUST = datetime(2026, 8, 14, 9, 30, tzinfo=UTC)
UNTICKED = "epic:never-ticked"


@pytest.fixture
def store(tmp_path: Path) -> Any:
    s = SqliteRunStore(str(tmp_path / "run.db"))
    yield s
    s.close()


def _reserve(store: Any, epic_ref: str, item_id: str, run_id: str, at: datetime):
    assert store.reserve_epic_node_item(epic_ref, item_id, run_id) is True
    store._conn.execute(
        "UPDATE epic_reservations SET reserved_at = ? WHERE item_id = ?",
        (at.isoformat(), item_id),
    )
    store._conn.commit()


def _held(store: Any, epic_ref: str) -> set[str]:
    return {r.item_id for r in store.list_epic_reservations(epic_ref)}


def _pending(store: Any, item_id: str, run_id: str, started_at: datetime) -> None:
    store.save(
        RunRecord(
            run_id=run_id,
            issue_num=int(item_id),
            item_id=item_id,
            branch=f"agent/issue-{item_id}",
            status="pending",
            exit_code=-1,
            started_at=started_at,
        )
    )


def test_an_august_reservation_with_no_run_row_is_released_with_a_warning(
    store: Any, caplog: pytest.LogCaptureFixture
) -> None:
    _reserve(store, UNTICKED, "2794", "res-august", AUGUST)
    _reserve(store, UNTICKED, "2795", "res-fresh", NOW - timedelta(seconds=60))
    assert store.live_run_count() == 2

    with caplog.at_level(logging.WARNING):
        swept = sweep_reservations(store, DispatchConfig(), now=NOW)

    assert [(s.item_id, s.run_id) for s in swept] == [("2794", "res-august")]
    assert _held(store, UNTICKED) == {"2795"}
    assert store.live_run_count() == 1
    warning = " ".join(r.getMessage() for r in caplog.records)
    assert UNTICKED in warning and "#2794" in warning and "res-august" in warning
    # The tick's own missing-run rule: the dead spawn reads FAILED, not lost.
    died = store.get("res-august")
    assert died is not None and died.error_category == "dispatch_child_died"


def test_a_reservation_inside_the_grace_survives_until_it_crosses_it(
    store: Any,
) -> None:
    reserved = NOW - timedelta(seconds=_RESERVATION_STARTUP_GRACE_SECONDS)
    _reserve(store, UNTICKED, "7", "res-7", reserved)

    assert sweep_reservations(store, DispatchConfig(), now=NOW) == []
    assert _held(store, UNTICKED) == {"7"}

    later = NOW + timedelta(seconds=1)
    assert [s.item_id for s in sweep_reservations(store, DispatchConfig(), now=later)]
    assert _held(store, UNTICKED) == set()


@pytest.mark.parametrize(
    ("closed", "kept"), [(True, False), (False, True), (None, True)]
)
def test_a_reservation_whose_issue_is_closed_is_released(
    store: Any, closed: bool | None, kept: bool
) -> None:
    _reserve(store, UNTICKED, "40", "res-40", NOW - timedelta(seconds=30))
    asked: list[str] = []

    def issue_closed(reservation: Any) -> bool | None:
        asked.append(reservation.item_id)
        return closed

    swept = sweep_reservations(
        store, DispatchConfig(), now=NOW, issue_closed=issue_closed
    )

    assert asked == ["40"]
    assert (_held(store, UNTICKED) == {"40"}) is kept
    assert [s.why for s in swept] == ([] if kept else ["its tracker item is closed"])


def test_a_later_terminal_run_releases_its_reservation(store: Any) -> None:
    _reserve(store, UNTICKED, "41", "res-41", NOW - timedelta(seconds=30))
    store.save(
        RunRecord(
            run_id="res-41",
            issue_num=41,
            item_id="41",
            branch="agent/issue-41",
            status="failure",
            exit_code=1,
            started_at=NOW - timedelta(seconds=20),
            finished_at=NOW - timedelta(seconds=10),
        )
    )

    swept = sweep_reservations(store, DispatchConfig(), now=NOW)

    assert [s.why for s in swept] == ["its run res-41 ended failure"]
    assert _held(store, UNTICKED) == set()


def test_a_settled_reservation_retires_its_worktree_refusal_hold(
    store: Any,
) -> None:
    """The epic's own reconcile never sees a reservation the sweep released,
    so the sweep must retire the refusal hold the tick would have retired —
    and only that hold, only once its reservation is gone."""
    _reserve(store, UNTICKED, "42", "res-42", AUGUST)
    _reserve(store, UNTICKED, "43", "res-43", NOW - timedelta(seconds=30))
    for item_id in ("42", "43"):
        store.hold_epic_node_item(
            UNTICKED,
            item_id,
            f"{PREEXISTING_WORKTREE_REFUSED_HOLD_PREFIX}: dirty worktree",
        )
    store.hold_epic_node_item(UNTICKED, "44", "operator hold")

    swept = sweep_reservations(store, DispatchConfig(), now=NOW)

    assert [s.item_id for s in swept] == ["42"]
    assert {h.item_id for h in store.list_epic_holds(UNTICKED)} == {"43", "44"}


def test_a_pending_run_keeps_its_reservation_however_old(store: Any) -> None:
    """Proving a pending run's owner dead is its own tick's job, with a probe;
    the probe-free sweep never releases under a run that may be live."""
    _reserve(store, UNTICKED, "42", "res-42", AUGUST)
    _pending(store, "42", "res-42", AUGUST)

    def closed(_reservation: Any) -> bool:
        return True

    assert (
        sweep_reservations(store, DispatchConfig(), now=NOW, issue_closed=closed) == []
    )
    assert _held(store, UNTICKED) == {"42"}


def test_a_run_that_starts_during_the_tracker_read_keeps_its_reservation(
    store: Any,
) -> None:
    """The tracker read takes seconds. A dispatch child admitted under the
    reservation while it ran: the closed-item release must see that run row
    and keep the reservation, never leave a pending run with no slot."""
    _reserve(store, UNTICKED, "60", "res-60", NOW - timedelta(seconds=30))

    def closed_while_the_child_admits(_reservation: Any) -> bool:
        _pending(store, "60", "res-60", NOW)
        return True

    swept = sweep_reservations(
        store, DispatchConfig(), now=NOW, issue_closed=closed_while_the_child_admits
    )

    assert swept == []
    assert [(r.item_id, r.run_id) for r in store.list_epic_reservations(UNTICKED)] == [
        ("60", "res-60")
    ]
    pending = store.get("res-60")
    assert pending is not None and pending.status == "pending"
    assert store.live_run_count() == 1


def _rereserve_refused(store: Any, item_id: str, old: str, new: str) -> None:
    """Another actor (an operator tick) settles ``old``, re-reserves the item
    as ``new``, and ``new``'s reconcile is refused, holding the node."""
    store.release_epic_node_item(item_id, run_id=old)
    assert store.reserve_epic_node_item(UNTICKED, item_id, new) is True
    store.hold_epic_node_item(
        UNTICKED, item_id, f"{PREEXISTING_WORKTREE_REFUSED_HOLD_PREFIX}: {new}"
    )


def _reservations(store: Any) -> list[tuple[str, str]]:
    return [(r.item_id, r.run_id) for r in store.list_epic_reservations(UNTICKED)]


def test_a_stale_snapshot_never_clears_the_hold_of_a_newer_reservation(
    store: Any,
) -> None:
    """The sweep's snapshot holds R1 for #71. While it reads another item,
    another actor releases R1, re-reserves #71 as R2 and R2's reconcile is
    refused. Reaching its stale R1, the sweep must leave R2 and R2's hold."""
    _reserve(store, UNTICKED, "70", "res-70", NOW - timedelta(seconds=30))
    _reserve(store, UNTICKED, "71", "res-71-r1", NOW - timedelta(seconds=30))

    def closed(reservation: Any) -> bool:
        if reservation.item_id == "70":
            _rereserve_refused(store, "71", "res-71-r1", "res-71-r2")
        return False

    swept = sweep_reservations(store, DispatchConfig(), now=NOW, issue_closed=closed)

    assert swept == []
    assert _reservations(store) == [("70", "res-70"), ("71", "res-71-r2")]
    [hold] = store.list_epic_holds(UNTICKED)
    assert (hold.item_id, hold.reason.endswith("res-71-r2")) == ("71", True)


def test_a_reservation_replaced_during_its_own_tracker_read_keeps_the_new_hold(
    store: Any,
) -> None:
    """The same interleaving inside R1's own closed-item read: R1 is gone by
    the release, so the sweep released nothing and R2's hold stands."""
    _reserve(store, UNTICKED, "72", "res-72-r1", NOW - timedelta(seconds=30))

    def closed(_reservation: Any) -> bool:
        _rereserve_refused(store, "72", "res-72-r1", "res-72-r2")
        return True

    swept = sweep_reservations(store, DispatchConfig(), now=NOW, issue_closed=closed)

    assert swept == []
    assert _reservations(store) == [("72", "res-72-r2")]
    assert [h.item_id for h in store.list_epic_holds(UNTICKED)] == ["72"]


def test_one_reservation_that_cannot_be_settled_does_not_stop_the_rest(
    store: Any, caplog: pytest.LogCaptureFixture
) -> None:
    _reserve(store, UNTICKED, "80", "res-80", NOW - timedelta(seconds=30))
    _reserve(store, UNTICKED, "81", "res-81", NOW - timedelta(seconds=30))

    def closed(reservation: Any) -> bool:
        if reservation.item_id == "80":
            raise LookupError("tracker answered garbage")
        return True

    with caplog.at_level(logging.ERROR):
        swept = sweep_reservations(
            store, DispatchConfig(), now=NOW, issue_closed=closed
        )

    assert [s.item_id for s in swept] == ["81"]
    assert _reservations(store) == [("80", "res-80")]
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "#80" in logged and "res-80" in logged and "LookupError" in logged


def test_slot_holders_name_the_owner_pid_and_what_its_probe_says(
    store: Any,
) -> None:
    from kew.graph.driver.reservation_sweep import slot_holders

    _reserve(store, UNTICKED, "90", "res-90", AUGUST)
    store.save(
        RunRecord(
            run_id="res-90",
            issue_num=90,
            item_id="90",
            branch="agent/issue-90",
            status="pending",
            exit_code=-1,
            started_at=AUGUST,
            owner_pid=4242,
        )
    )
    _reserve(store, UNTICKED, "91", "res-91", NOW - timedelta(seconds=30))
    probed: list[str] = []

    def dead(run: RunRecord) -> bool:
        probed.append(run.run_id)
        return False

    holders = slot_holders(store, now=NOW, probe=dead)

    assert probed == ["res-90"]
    assert [(h.reservation.item_id, h.owner_pid, h.owner) for h in holders] == [
        ("90", 4242, "dead"),
        ("91", None, "no run row"),
    ]
    assert "pending" in holders[0].verdict
    assert "inside the startup grace" in holders[1].verdict


# --------------------------------------------------------------------------- #
# kew drive
# --------------------------------------------------------------------------- #


def _config(tmp_path: Path) -> DispatchConfig:
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    with SqliteRunStore(str(log_dir / "dispatch.db")) as s:
        s.register_epic("epic:a")
    cfg = DispatchConfig(project=ProjectConfig(repo="o/r"))
    cfg.logging.dir = str(log_dir)
    return cfg


def _project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DispatchConfig:
    """A project on disk that ``kew drive status`` loads its config from."""
    cfg = _config(tmp_path)
    (tmp_path / "kew.toml").write_text(
        f'[project]\nrepo = "o/r"\n\n[logging]\ndir = "{cfg.logging.dir}"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("KEW_PROJECT", str(tmp_path))
    return cfg


def test_the_drive_releases_a_stale_reservation_under_an_epic_it_never_ticks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "_sleep", lambda _s: None)
    cfg = _config(tmp_path)
    db = str(Path(cfg.logging.dir) / "dispatch.db")
    ticked: list[str] = []
    with SqliteRunStore(db) as ledger:
        _reserve(ledger, UNTICKED, "2794", "res-august", AUGUST)
        _reserve(ledger, UNTICKED, "2795", "res-fresh", datetime.now(UTC))

        def tick(name: str, _admit_cap: int) -> int:
            ticked.append(name)
            holder = current_holder(ledger)
            assert holder is not None
            request_stop(ledger, holder, None)
            return 10

        drive_cli.run_drive(cfg, tick, interval=1)

        assert ticked == ["epic:a"]
        assert _held(ledger, UNTICKED) == {"2795"}
        assert ledger.live_run_count() == 1
    err = capsys.readouterr().err
    assert "released the reservation of #2794" in err


def test_a_sweep_that_fails_on_every_reservation_never_stops_the_drive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "_sleep", lambda _s: None)
    cfg = _config(tmp_path)
    db = str(Path(cfg.logging.dir) / "dispatch.db")
    ticked: list[str] = []

    def tracker_down(_store: Any) -> Any:
        def closed(_reservation: Any) -> bool:
            raise KeyError("tracker answered without a state")

        return closed

    with SqliteRunStore(db) as ledger:
        _reserve(ledger, UNTICKED, "2796", "res-2796", datetime.now(UTC))

        def tick(name: str, _admit_cap: int) -> int:
            ticked.append(name)
            holder = current_holder(ledger)
            assert holder is not None
            request_stop(ledger, holder, None)
            return 10

        drive_cli.run_drive(cfg, tick, interval=1, issue_closed=tracker_down)

        assert ticked == ["epic:a"]
        assert _held(ledger, UNTICKED) == {"2796"}
    capsys.readouterr()


def test_status_gives_each_reservation_a_human_age_and_its_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _project(tmp_path, monkeypatch)
    now = datetime.now(UTC)
    with SqliteRunStore(str(Path(cfg.logging.dir) / "dispatch.db")) as ledger:
        _reserve(ledger, UNTICKED, "2794", "res-old", now - timedelta(days=73, hours=1))
        ledger.save(
            RunRecord(
                run_id="res-old",
                issue_num=2794,
                item_id="2794",
                branch="agent/issue-2794",
                status="pending",
                exit_code=-1,
                started_at=now - timedelta(days=73),
                owner_pid=4242,
            )
        )
        _reserve(ledger, "epic:a", "12", "res-12", now - timedelta(hours=2, minutes=5))

    result = runner.invoke(app, ["drive", "status"])

    # No pgid recorded: this machine cannot tell whether pid 4242 is the owner.
    assert (
        f"{UNTICKED}: #2794 run res-old, reserved 73d ago, owner pid 4242 "
        "unprobeable — run res-old pending" in result.output
    ), result.output
    assert "epic:a: #12 run res-12, reserved 2h ago, no run row — no run row past" in (
        result.output
    )
    assert "s ago" not in result.output.split("reservations holding a slot")[1]

    doc = json.loads(runner.invoke(app, ["drive", "status", "--json"]).output)
    old = next(r for r in doc["reservations"] if r["item"] == "2794")
    assert (old["owner_pid"], old["owner"]) == (4242, "unprobeable")
    assert old["age_s"] > 73 * 86400


def test_status_names_every_reservation_holding_a_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _project(tmp_path, monkeypatch)
    with SqliteRunStore(str(Path(cfg.logging.dir) / "dispatch.db")) as ledger:
        _reserve(ledger, UNTICKED, "2794", "res-august", AUGUST)
        _reserve(ledger, "epic:a", "12", "res-12", datetime.now(UTC))

    result = runner.invoke(app, ["drive", "status"])

    assert result.exit_code == 1, result.output  # no drive runs
    assert "reservations holding a slot: 2" in result.output
    assert f"{UNTICKED}: #2794 run res-august" in result.output
    assert "epic:a: #12 run res-12" in result.output

    result = runner.invoke(app, ["drive", "status", "--json"])
    doc = json.loads(result.output)
    assert {(r["epic"], r["item"], r["run_id"]) for r in doc["reservations"]} == {
        (UNTICKED, "2794", "res-august"),
        ("epic:a", "12", "res-12"),
    }


def test_status_says_when_no_reservation_holds_a_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _project(tmp_path, monkeypatch)

    result = runner.invoke(app, ["drive", "status"])

    assert "reservations holding a slot: none" in result.output
