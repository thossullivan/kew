"""`kew drive` admission and its load gate (#2616).

The acceptance: with a fake load source above each threshold the cap drops as
configured; a hold stops new admission while running work finishes; the cap
counts a placed remote run. The drive's own loop is exercised in
``test_drive.py``; this module pins the cap arithmetic, the hold row, and what
an epic tick does with the cap the drive hands it.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from support.driver_fakes import (
    NOW,
    FakeFactsReader,
    FakeSink,
    FakeSource,
    _advance,
    _facts,
    _node,
)

from kew.cli.epic import _admit_under
from kew.config import DispatchConfig, DriveLoadConfig, DriveLoadStep
from kew.drive import (
    DRIVE_HOLD_KEY,
    DriveStateError,
    admission,
    drive_hold,
    hold,
    load_cap,
    release,
)
from kew.graph.driver.tick import _review_concurrency
from kew.run_store import RunRecord, SqliteRunStore


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SqliteRunStore]:
    with SqliteRunStore(str(tmp_path / "dispatch.db")) as opened:
        yield opened


def _gated(max_parallel: int = 4) -> DispatchConfig:
    config = _wide()
    config.drive.max_parallel = max_parallel
    config.drive.load = DriveLoadConfig(
        thresholds=[
            DriveLoadStep(above=14, max_parallel=3),
            DriveLoadStep(above=25, max_parallel=2),
        ]
    )
    return config


def _wide() -> DispatchConfig:
    """A config whose own fleet cap (5) is wider than any drive cap below, so
    the drive's cap is the only thing that can bound the tick."""
    config = DispatchConfig()
    config.parallel.max_workers = 5
    return config


def _remote_run(run_id: str = "remote-1") -> RunRecord:
    """An in-flight run placed on a gcp instance — on no process table here."""
    return RunRecord(
        run_id=run_id,
        issue_num=99,
        branch="agent/issue-99",
        status="pending",
        exit_code=0,
        started_at=NOW,
        item_id="99",
        run_host="gcp",
        run_host_instance="vm-one",
    )


# --------------------------------------------------------------------------- #
# The cap
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("load", "cap"),
    [(0.5, 4), (14.0, 4), (14.1, 3), (25.0, 3), (25.1, 2), (99.0, 2)],
)
def test_each_threshold_the_load_is_above_lowers_the_cap(load: float, cap: int) -> None:
    assert load_cap(_gated(), load)[0] == cap


def test_the_reason_names_the_threshold_that_set_the_cap() -> None:
    assert load_cap(_gated(), 26.04)[1] == "load 26.0 above 25"
    assert load_cap(_gated(), 1.0)[1] == "[drive] max_parallel 4"


def test_an_unreadable_load_takes_the_lowest_threshold_never_the_full_cap() -> None:
    cap, reason = load_cap(_gated(), "load average unreadable (boom)")

    assert cap == 2
    assert "unreadable" in reason


def test_a_threshold_never_raises_the_cap() -> None:
    assert load_cap(_gated(max_parallel=1), 30.0)[0] == 1


def test_the_drive_never_raises_the_fleet_cap() -> None:
    """Every run re-checks admission against the fleet cap in its own process,
    so a drive cap above it would only reserve work that is then refused."""
    config = _gated()
    config.fleet.max_concurrent = 2
    assert load_cap(config, 0.0) == (2, "fleet cap 2")

    config = _gated()
    config.parallel.max_workers = 1
    assert load_cap(config, 0.0) == (1, "fleet cap 1")


def test_a_threshold_of_zero_admits_nothing() -> None:
    config = _gated()
    config.drive.load.thresholds.append(DriveLoadStep(above=40, max_parallel=0))

    assert load_cap(config, 41.0)[0] == 0


def test_drive_config_rejects_a_cap_below_one() -> None:
    with pytest.raises(ValueError, match="drive.max_parallel"):
        DispatchConfig.model_validate({"drive": {"max_parallel": 0}})


# --------------------------------------------------------------------------- #
# The hold
# --------------------------------------------------------------------------- #


def test_hold_and_release_round_trip_and_keep_the_first_time(store: Any) -> None:
    first = hold(store, now=datetime(2026, 10, 1, tzinfo=UTC))
    again = hold(store, now=datetime(2026, 10, 2, tzinfo=UTC))

    assert again.since == first.since
    assert drive_hold(store) == first
    assert release(store) == first
    assert drive_hold(store) is None
    assert release(store) is None


def test_a_hold_admits_nothing_whatever_the_load(store: Any) -> None:
    hold(store)

    granted = admission(store, _gated(), 0.0)

    assert granted.cap == 0
    assert "held since" in granted.reason


def test_an_unreadable_hold_row_is_an_error_not_a_release(store: Any) -> None:
    store.set_meta(DRIVE_HOLD_KEY, "{not json")

    with pytest.raises(DriveStateError):
        admission(store, _gated(), 0.0)


# --------------------------------------------------------------------------- #
# Runs on every host count
# --------------------------------------------------------------------------- #


def test_the_drive_counts_a_run_placed_on_a_remote_host(store: Any) -> None:
    store.save(_remote_run())

    granted = admission(store, _gated(max_parallel=1), 0.0)

    assert (granted.cap, granted.live, granted.room) == (1, 1, 0)


def test_a_tick_under_the_drive_cap_counts_a_remote_run(
    tmp_path: Path, store: Any
) -> None:
    """The cap the drive hands a tick is that tick's fleet cap: a run on a gcp
    instance fills it, so a ready root is held, not dispatched."""
    store.save(_remote_run())
    config, max_parallel = _admit_under(_wide(), 5, 1)
    sink = FakeSink()

    result = _advance(
        deps=FakeSource(nodes=[_node("1")], edges=[]),
        facts=FakeFactsReader(issue_facts_by_num={1: _facts(1, state="OPEN")}),
        store=store,
        sink=sink,
        lock_path=tmp_path / "epic.lock",
        config=config,
        max_parallel=max_parallel,
    )

    assert sink.calls == []
    assert result.dispatched == ()
    [waiting] = result.waiting
    assert "worker cap 1 consumed (1 live run(s))" in waiting.reason


def test_the_same_tick_dispatches_once_the_cap_has_room(
    tmp_path: Path, store: Any
) -> None:
    store.save(_remote_run())
    config, max_parallel = _admit_under(_wide(), 5, 2)
    sink = FakeSink()

    result = _advance(
        deps=FakeSource(nodes=[_node("1")], edges=[]),
        facts=FakeFactsReader(issue_facts_by_num={1: _facts(1, state="OPEN")}),
        store=store,
        sink=sink,
        lock_path=tmp_path / "epic.lock",
        config=config,
        max_parallel=max_parallel,
    )

    assert result.dispatched == ("1",)


# --------------------------------------------------------------------------- #
# A held tick
# --------------------------------------------------------------------------- #


def test_a_held_tick_dispatches_nothing_and_says_why(
    tmp_path: Path, store: Any
) -> None:
    config, max_parallel = _admit_under(DispatchConfig(), 5, 0)
    sink = FakeSink()

    result = _advance(
        deps=FakeSource(nodes=[_node("1")], edges=[]),
        facts=FakeFactsReader(issue_facts_by_num={1: _facts(1, state="OPEN")}),
        store=store,
        sink=sink,
        lock_path=tmp_path / "epic.lock",
        config=config,
        max_parallel=max_parallel,
    )

    assert max_parallel == 0
    assert sink.calls == []
    [waiting] = result.waiting
    assert "admission closed by kew drive" in waiting.reason


def test_an_operator_stop_names_max_parallel_zero_not_the_drive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``kew epic advance E --max-parallel 0`` with no drive running (#3192):
    the held node names what closed admission, the operator's flag — never a
    ``kew drive`` hold nobody set."""
    from typer.testing import CliRunner

    import kew.cli as cli
    from kew.cli.epic import epic_app

    (tmp_path / "kew.toml").write_text(
        f'[project]\nrepo = "o/r"\n\n[logging]\ndir = "{tmp_path / "logs"}"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("KEW_PROJECT", str(tmp_path))

    def tracker(*_args: Any, **_kwargs: Any) -> Any:
        facts = FakeFactsReader(issue_facts_by_num={1: _facts(1, state="OPEN")})
        return FakeSource(nodes=[_node("1")], edges=[]), facts, None

    # The tools on PATH and the tracker are the process and forge boundaries.
    monkeypatch.setattr(cli, "preflight_tools", lambda *_a, **_k: None)
    monkeypatch.setattr(cli, "_epic_driver_sources", tracker)

    invoked = CliRunner().invoke(
        epic_app, ["advance", "epic/dag-1", "--max-parallel", "0"]
    )

    assert "admission closed by --max-parallel 0" in invoked.output, invoked.output
    assert "kew drive" not in invoked.output


def test_a_held_tick_starts_no_review_or_remediation(store: Any) -> None:
    assert (
        _review_concurrency(store, epic_ref="epic:a", max_parallel=0, fleet_cap=3) == 0
    )
    assert (
        _review_concurrency(store, epic_ref="epic:a", max_parallel=1, fleet_cap=3) == 1
    )


def test_the_drive_cap_replaces_the_tick_fleet_cap_without_touching_the_original() -> (
    None
):
    original = DispatchConfig()
    capped, max_parallel = _admit_under(original, 5, 2)

    assert capped.fleet_concurrency() == 2
    assert max_parallel == 2
    assert original.fleet.max_concurrent is None
