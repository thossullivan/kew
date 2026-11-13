"""`kew drive` holds admission while the host is overloaded (#2813).

The acceptance: above ``[drive] max_load_per_cpu`` (or ``max_swap_used_pct``)
a ready node is admission-held with the load named and nothing launches;
admission resumes after two reads below the thresholds; unset thresholds
change nothing. Every test runs on a real ledger with a fake host reader; the
per-epic tick of the drive loop is replaced as in ``test_drive.py``.
"""

from __future__ import annotations

import json
import math
import subprocess
import sys
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from support.driver_fakes import (
    EPIC,
    FakeFactsReader,
    FakeSink,
    FakeSource,
    _advance,
    _facts,
    _node,
)

import kew.cli as cli
import kew.cli.drive as drive_cli
import kew.drive_host_load as host_load
import kew.subprocess_utils as subprocess_utils
from kew import run_requests as rr
from kew.cli.epic import _admit_under
from kew.config import DispatchConfig, DriveConfig, ProjectConfig
from kew.drive import (
    DriveLease,
    DriveStateError,
    acquire_drive,
    admission,
    current_holder,
    hold,
    host_gate,
    host_load_episode,
    pause,
    request_stop,
)
from kew.drive_host_load import (
    DRIVE_HOST_LOAD_KEY,
    HostLoadHold,
    HostReading,
    host_load_hold,
    overload,
    read_host,
    read_swap,
)
from kew.drive_inbox import Notice
from kew.drive_requests import RequestRunner
from kew.graph.build import build_graph
from kew.graph.driver.tick import summarize
from kew.run_store import SqliteRunStore
from kew.runner_quota import (
    RUNNER_QUOTA_HELD_PREFIX,
    QuotaRejection,
    record_runner_quota_hold,
)

CONTINUE = 10  # a PROGRESSING tick child (cli.epic)

CALM = HostReading(load=2.0, cpus=8, swap=10.0)
BUSY = HostReading(load=98.0, cpus=8, swap=10.0)
SWAPPING = HostReading(load=2.0, cpus=8, swap=97.0)


def _config(tmp_path: Path) -> DispatchConfig:
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    with SqliteRunStore(str(log_dir / "dispatch.db")) as s:
        s.register_epic("epic:a")
    cfg = DispatchConfig(project=ProjectConfig(repo="o/r"))
    cfg.logging.dir = str(log_dir)
    cfg.parallel.max_workers = 8
    return cfg


def _gated(cfg: DispatchConfig) -> DispatchConfig:
    cfg.drive.max_load_per_cpu = 4
    cfg.drive.max_swap_used_pct = 90
    return cfg


@pytest.fixture
def cfg(tmp_path: Path) -> DispatchConfig:
    return _config(tmp_path)


@pytest.fixture
def ledger(cfg: DispatchConfig) -> Iterator[SqliteRunStore]:
    with SqliteRunStore(str(Path(cfg.logging.dir) / "dispatch.db")) as s:
        yield s


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_sleep", lambda _s: None)


def _readings(*seq: HostReading | str) -> Callable[..., HostReading | str]:
    """A host reader that returns ``seq`` in order, one per read."""
    it = iter(seq)

    def read(*, swap: bool) -> HostReading | str:
        assert swap is True
        return next(it)

    return read


def _drive(
    cfg: DispatchConfig,
    ledger: Any,
    host: Callable[..., HostReading | str],
    ticks: int,
    notices: list[Notice] | None = None,
    episodes: list[HostLoadHold | None] | None = None,
) -> list[int]:
    """Run the drive for ``ticks`` ticks of one epic; the cap each was told.
    ``episodes`` collects the episode row each tick ran under."""
    caps: list[int] = []

    def tick(_name: str, admit_cap: int) -> int:
        caps.append(admit_cap)
        if episodes is not None:
            episodes.append(host_load_hold(ledger))
        if len(caps) == ticks:
            holder = current_holder(ledger)
            assert holder is not None
            request_stop(ledger, holder, None)
        return CONTINUE

    notify = None if notices is None else notices.append
    drive_cli.run_drive(cfg, tick, 1, load=lambda: 0.0, notify=notify, host=host)
    return caps


# --------------------------------------------------------------------------- #
# Held: nothing launches, and the load is named
# --------------------------------------------------------------------------- #


def test_load_above_the_threshold_admits_nothing_and_names_the_load(
    cfg: DispatchConfig, ledger: Any
) -> None:
    episodes: list[HostLoadHold | None] = []

    caps = _drive(
        _gated(cfg), ledger, _readings(BUSY, BUSY), ticks=2, episodes=episodes
    )

    assert caps == [0, 0]
    episode = episodes[-1]
    assert episode is not None
    assert episode.reason == (
        "host load 98.0 on 8 CPU(s) (12.25 per CPU, above [drive] max_load_per_cpu 4)"
    )


def test_a_held_ready_node_is_admission_held_by_host_load_and_not_dispatched(
    tmp_path: Path, cfg: DispatchConfig, ledger: Any
) -> None:
    """The tick the drive hands a cap of 0 during an episode launches nothing
    and says the host load is why — the line `kew epic status` prints."""
    host_gate(ledger, _gated(cfg), _readings(BUSY))
    config, max_parallel = _admit_under(DispatchConfig(), 5, 0)
    sink = FakeSink()

    result = _advance(
        deps=FakeSource(nodes=[_node("1")], edges=[]),
        facts=FakeFactsReader(issue_facts_by_num={1: _facts(1, state="OPEN")}),
        store=ledger,
        sink=sink,
        lock_path=tmp_path / "epic.lock",
        config=config,
        max_parallel=max_parallel,
    )

    assert sink.calls == []
    assert result.dispatched == ()
    [waiting] = result.waiting
    assert waiting.node_id == "1"
    assert waiting.reason.startswith("admission-held: host load 98.0 on 8 CPU(s)")


def test_host_load_is_named_before_a_runner_quota_hold_on_a_closed_tick(
    tmp_path: Path, cfg: DispatchConfig, ledger: Any
) -> None:
    """With a runner quota hold standing AND the host overloaded, the drive
    hands the tick a cap of 0: the waiting line names the host load, the
    closed admission, not the quota hold that would also have held the node."""
    record_runner_quota_hold(
        ledger,
        QuotaRejection("claude-code", "seven_day", datetime.now(UTC) + timedelta(1)),
    )
    host_gate(ledger, _gated(cfg), _readings(BUSY))
    config, max_parallel = _admit_under(DispatchConfig(), 5, 0)
    sink = FakeSink()

    result = _advance(
        deps=FakeSource(nodes=[_node("1")], edges=[]),
        facts=FakeFactsReader(issue_facts_by_num={1: _facts(1, state="OPEN")}),
        store=ledger,
        sink=sink,
        lock_path=tmp_path / "epic.lock",
        config=config,
        max_parallel=max_parallel,
    )

    assert sink.calls == []
    [waiting] = result.waiting
    assert waiting.reason.startswith("admission-held: host load 98.0 on 8 CPU(s)")
    assert RUNNER_QUOTA_HELD_PREFIX not in waiting.reason


def test_a_runner_quota_hold_is_named_while_admission_is_open(
    tmp_path: Path, ledger: Any
) -> None:
    """With admission open, a standing quota hold is still what the ready
    node waits on."""
    record_runner_quota_hold(
        ledger,
        QuotaRejection("claude-code", "seven_day", datetime.now(UTC) + timedelta(1)),
    )
    config, max_parallel = _admit_under(DispatchConfig(), 5, 5)

    result = _advance(
        deps=FakeSource(nodes=[_node("1")], edges=[]),
        facts=FakeFactsReader(issue_facts_by_num={1: _facts(1, state="OPEN")}),
        store=ledger,
        sink=FakeSink(),
        lock_path=tmp_path / "epic.lock",
        config=config,
        max_parallel=max_parallel,
    )

    [waiting] = result.waiting
    assert waiting.reason.startswith(RUNNER_QUOTA_HELD_PREFIX)


def test_epic_status_names_the_host_load_on_a_ready_node_while_the_drive_runs(
    tmp_path: Path, cfg: DispatchConfig, ledger: Any
) -> None:
    """`kew epic status` reads no tick: it lists a ready node as held by the
    episode a live drive recorded, and not one a stopped drive left behind."""
    deps = FakeSource(nodes=[_node("1")], edges=[])
    facts = FakeFactsReader(issue_facts_by_num={1: _facts(1, state="OPEN")})
    graph = build_graph(deps, EPIC)

    def status() -> list[str]:
        summary = summarize(EPIC, graph, config=cfg, store=ledger, facts=facts)
        return [w.reason for w in summary.waiting if w.node_id == "1"]

    host_gate(ledger, _gated(cfg), _readings(BUSY))
    assert status() == []  # no drive holds the lock: nothing claims the host

    lease = acquire_drive(ledger)
    assert isinstance(lease, DriveLease)
    [reason] = status()
    assert reason.startswith("admission-held: host load 98.0 on 8 CPU(s)")


def test_a_stopped_drive_ends_its_episode(cfg: DispatchConfig, ledger: Any) -> None:
    _drive(_gated(cfg), ledger, _readings(BUSY), ticks=1)

    assert host_load_hold(ledger) is None


def test_swap_above_the_threshold_holds_and_names_the_swap(
    cfg: DispatchConfig, ledger: Any
) -> None:
    gate = host_gate(ledger, _gated(cfg), _readings(SWAPPING))

    assert gate.hold is not None
    assert gate.hold.reason == "swap 97% used (above [drive] max_swap_used_pct 90)"
    assert admission(ledger, cfg, 0.0, gate.hold).cap == 0


def test_an_unreadable_host_holds_rather_than_admitting(
    cfg: DispatchConfig, ledger: Any
) -> None:
    gate = host_gate(
        ledger, _gated(cfg), _readings(HostReading(2.0, 8, "swap unreadable (x)"))
    )

    assert gate.hold is not None
    assert gate.hold.reason == "swap unreadable (x)"
    assert (
        overload("host load unreadable (y)", max_load_per_cpu=4, max_swap_used_pct=None)
        == "host load unreadable (y)"
    )


def test_one_attention_event_per_episode(cfg: DispatchConfig, ledger: Any) -> None:
    notices: list[Notice] = []
    host = _readings(BUSY, BUSY, CALM, CALM, SWAPPING, SWAPPING)

    caps = _drive(_gated(cfg), ledger, host, ticks=6, notices=notices)

    assert caps == [0, 0, 0, 3, 0, 0]
    assert [n.event.reason for n in notices] == [
        "host overloaded: host load 98.0 on 8 CPU(s) (12.25 per CPU, above "
        "[drive] max_load_per_cpu 4)",
        "host overloaded: swap 97% used (above [drive] max_swap_used_pct 90)",
    ]
    assert {n.owner.value for n in notices} == {"operator"}


# --------------------------------------------------------------------------- #
# Resume after two calm reads
# --------------------------------------------------------------------------- #


def test_admission_resumes_after_two_ticks_below_the_threshold(
    cfg: DispatchConfig, ledger: Any
) -> None:
    host = _readings(BUSY, CALM, CALM, CALM)

    caps = _drive(_gated(cfg), ledger, host, ticks=4)

    assert caps == [0, 0, 3, 3]
    assert host_load_hold(ledger) is None


def test_a_reading_back_above_the_threshold_restarts_the_calm_count(
    cfg: DispatchConfig, ledger: Any
) -> None:
    host = _readings(BUSY, CALM, BUSY, CALM, CALM)

    caps = _drive(_gated(cfg), ledger, host, ticks=5)

    assert caps == [0, 0, 0, 0, 3]


def test_an_episode_keeps_the_time_it_began(cfg: DispatchConfig, ledger: Any) -> None:
    gated = _gated(cfg)
    first = datetime(2026, 10, 12, 21, 58, tzinfo=UTC)
    later = datetime(2026, 10, 12, 22, 30, tzinfo=UTC)

    began = host_gate(ledger, gated, _readings(BUSY), now=first)
    still = host_gate(ledger, gated, _readings(SWAPPING), now=later)
    easing = host_gate(ledger, gated, _readings(CALM), now=later)

    assert (began.began, still.began, easing.began) == (True, False, False)
    assert still.hold is not None and still.hold.since == first
    assert easing.hold is not None and easing.hold.calm == 1
    assert "1 of 2 calm reads" in easing.hold.describe()


def test_an_unreadable_episode_row_is_an_error_not_a_calm_host(
    cfg: DispatchConfig, ledger: Any
) -> None:
    ledger.set_meta(DRIVE_HOST_LOAD_KEY, "{not json")

    with pytest.raises(DriveStateError):
        host_gate(ledger, _gated(cfg), _readings(CALM))


# --------------------------------------------------------------------------- #
# Unset thresholds change nothing
# --------------------------------------------------------------------------- #


def test_unset_thresholds_never_read_the_host_and_admit_as_before(
    cfg: DispatchConfig, ledger: Any
) -> None:
    def never(**_kw: Any) -> HostReading:
        raise AssertionError("the host was read with no threshold set")

    caps = _drive(cfg, ledger, never, ticks=3)

    assert caps == [3, 3, 3]
    assert ledger.get_meta(DRIVE_HOST_LOAD_KEY) is None


def test_unsetting_the_thresholds_clears_an_episode_left_behind(
    cfg: DispatchConfig, ledger: Any
) -> None:
    host_gate(ledger, _gated(DispatchConfig()), _readings(BUSY))
    assert host_load_hold(ledger) is not None

    gate = host_gate(ledger, cfg, _readings())

    assert gate.hold is None
    assert host_load_hold(ledger) is None


@pytest.mark.parametrize(
    "field",
    [
        {"max_load_per_cpu": 0},
        {"max_swap_used_pct": 0},
        {"max_swap_used_pct": 101},
        {"max_load_per_cpu": math.nan},
        {"max_load_per_cpu": math.inf},
        {"max_swap_used_pct": math.nan},
    ],
)
def test_drive_config_rejects_thresholds_out_of_range(field: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        DriveConfig(**field)


# --------------------------------------------------------------------------- #
# A malformed reading or row holds; it never reads as a calm host
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "reading",
    [
        HostReading(math.nan, 8, 10.0),
        HostReading(math.inf, 8, 10.0),
        HostReading(-1.0, 8, 10.0),
        HostReading(2.0, 0, 10.0),
        HostReading(2.0, 8, -100.0),
        HostReading(2.0, 8, 150.0),
        HostReading(2.0, 8, math.nan),
    ],
)
def test_a_malformed_reading_holds_with_its_reason(reading: HostReading) -> None:
    why = overload(reading, max_load_per_cpu=4, max_swap_used_pct=90)

    assert why is not None
    assert "unreadable" in why


def test_malformed_reads_never_count_toward_resuming(
    cfg: DispatchConfig, ledger: Any
) -> None:
    nan = HostReading(math.nan, 8, 10.0)

    caps = _drive(_gated(cfg), ledger, _readings(BUSY, nan, nan, nan), ticks=4)

    assert caps == [0, 0, 0, 0]


def test_an_unknown_cpu_count_is_unreadable_not_one_cpu(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(host_load.os, "getloadavg", lambda: (98.0, 0.0, 0.0))
    monkeypatch.setattr(host_load.os, "cpu_count", lambda: None)

    reading = read_host(swap=False)

    assert reading == "host CPU count unreadable"
    assert overload(reading, max_load_per_cpu=4, max_swap_used_pct=None) == reading


def test_swap_free_above_its_total_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("SwapTotal:       100 kB\nSwapFree:        200 kB\n")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(host_load, "_MEMINFO", meminfo)

    why = overload(
        HostReading(2.0, 8, read_swap()), max_load_per_cpu=None, max_swap_used_pct=90
    )

    assert why == "swap unreadable (-100.0% used)"


class _Sysctl:
    """Stands in for the sysctl process, recording the executable it ran."""

    def __init__(self, stdout: str) -> None:
        self.stdout = stdout
        self.ran: list[str] = []

    def __call__(self, argv: list[str], **_kw: Any) -> Any:
        self.ran.append(argv[0])
        return subprocess.CompletedProcess(argv, 0, stdout=self.stdout, stderr="")


def _macos(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stdout: str
) -> tuple[_Sysctl, Path]:
    sysctl = tmp_path / "usr-sbin-sysctl"
    sysctl.write_text("")
    fake = _Sysctl(stdout)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(host_load, "_SYSCTL", sysctl)
    monkeypatch.setattr(subprocess_utils.subprocess, "run", fake)
    monkeypatch.setitem(subprocess_utils.TOOL_PATHS, "sysctl", "/elsewhere/sysctl")
    return fake, sysctl


def test_macos_swap_runs_sysctl_by_its_absolute_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = "total = 2048.00M  used = 1536.00M  free = 512.00M  (encrypted)\n"
    fake, sysctl = _macos(tmp_path, monkeypatch, out)

    assert read_swap() == 75.0
    assert fake.ran == [str(sysctl)]


@pytest.mark.parametrize(
    "out",
    [
        "garbage total = 2048.00M  used = 0.00M  free = 2048.00M",
        "total = 2048.00M  used = 0.00M  free = 2048.00M trailing",
        "total = 2048.00M  used = 0.00M",
        "total = 1.2.3M  used = 0.00M  free = 2048.00M",
    ],
)
def test_malformed_sysctl_output_is_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, out: str
) -> None:
    _macos(tmp_path, monkeypatch, out)

    assert isinstance(read_swap(), str)


@pytest.mark.parametrize("calm", [-1_000_000_000, 3, 1_000_000_000, "1", True])
def test_an_episode_row_with_a_calm_count_out_of_range_is_an_error(
    cfg: DispatchConfig, ledger: Any, calm: Any
) -> None:
    row = {"since": "2026-10-12T21:58:00+00:00", "reason": "host load", "calm": calm}
    ledger.set_meta(DRIVE_HOST_LOAD_KEY, json.dumps(row))

    with pytest.raises(DriveStateError):
        host_gate(ledger, _gated(cfg), _readings(CALM))
    with pytest.raises(DriveStateError):
        host_load_episode(ledger)


# --------------------------------------------------------------------------- #
# A queued request waits, queued, while the host holds admission
# --------------------------------------------------------------------------- #

WAITING = "waiting: the drive is holding admission for host load"


class _Child:
    """A spawned request's child, already exited 0 (the process boundary)."""

    pid = 4242

    def poll(self) -> int:
        return 0


class _Spawner:
    """Records each request the drive starts; ``forbid`` fails the test."""

    def __init__(self, *, forbid: bool = False) -> None:
        self.calls: list[list[str]] = []
        self.forbid = forbid

    def __call__(self, argv: list[str], *_a: Any) -> Any:
        if self.forbid:
            raise AssertionError("a request started on an overloaded host")
        self.calls.append(argv)
        return _Child()


def _runner(cfg: DispatchConfig, spawn: _Spawner) -> Callable[[Any, DriveLease], Any]:
    """The drive's request factory, over ``spawn``."""

    def build(store: Any, lease: DriveLease) -> RequestRunner:
        return RequestRunner(
            store, lease, cfg, Path(cfg.logging.dir), spawn=spawn, say=lambda _m: None
        )

    return build


def _assert_waiting_for_host_load(ledger: Any, request_id: str) -> None:
    waiting = rr.get_request(ledger, request_id)
    assert waiting is not None
    assert waiting.state == rr.QUEUED
    assert (waiting.detail or "").startswith(WAITING)
    assert "host load 98.0 on 8 CPU(s)" in (waiting.detail or "")


@pytest.mark.parametrize(
    "command",
    [
        "kew dispatch 7",
        "kew epic continue epic:a 12",
        "kew epic retry go-live SFD-232",
        "kew epic unhold epic:a 12",
    ],
)
def test_a_host_load_episode_leaves_a_spend_bearing_request_queued(
    cfg: DispatchConfig,
    ledger: Any,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    monkeypatch.setenv("KEW_ACTOR", "tom")
    gated = _gated(cfg)
    host_gate(ledger, gated, _readings(BUSY))
    request, _ = rr.queue(ledger, command, operator="tom", source="web")
    lease = acquire_drive(ledger)
    assert isinstance(lease, DriveLease)

    _runner(gated, _Spawner(forbid=True))(ledger, lease)()

    _assert_waiting_for_host_load(ledger, request.id)


def test_an_operator_hold_still_refuses_a_queued_request(
    cfg: DispatchConfig, ledger: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KEW_ACTOR", "tom")
    gated = _gated(cfg)
    host_gate(ledger, gated, _readings(BUSY))
    hold(ledger)
    request, _ = rr.queue(ledger, "kew dispatch 7", operator="tom", source="web")
    lease = acquire_drive(ledger)
    assert isinstance(lease, DriveLease)

    _runner(gated, _Spawner(forbid=True))(ledger, lease)()

    refused = rr.get_request(ledger, request.id)
    assert refused is not None
    assert refused.state == rr.REFUSED
    assert "`kew drive release` lifts it" in (refused.detail or "")


def test_an_overload_at_start_holds_a_request_queued_before_any_tick(
    cfg: DispatchConfig, ledger: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KEW_ACTOR", "tom")
    gated = _gated(cfg)
    request, _ = rr.queue(ledger, "kew dispatch 7", operator="tom", source="web")
    notices: list[Notice] = []

    def tick(_name: str, _cap: int) -> int:
        holder = current_holder(ledger)
        assert holder is not None
        request_stop(ledger, holder, None)
        return CONTINUE

    drive_cli.run_drive(
        gated,
        tick,
        1,
        load=lambda: 0.0,
        requests=_runner(gated, _Spawner(forbid=True)),
        notify=notices.append,
        host=lambda *, swap: BUSY,
    )

    _assert_waiting_for_host_load(ledger, request.id)
    assert len(notices) == 1


def test_an_overload_that_begins_during_sleep_holds_a_request_queued_then(
    cfg: DispatchConfig, ledger: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KEW_ACTOR", "tom")
    gated = _gated(cfg)
    caps: list[int] = []
    queued: list[str] = []

    def sleep(_s: float) -> None:
        if not queued:
            request, _ = rr.queue(
                ledger, "kew dispatch 7", operator="tom", source="web"
            )
            queued.append(request.id)
            return
        holder = current_holder(ledger)
        assert holder is not None
        request_stop(ledger, holder, None)

    monkeypatch.setattr(cli, "_sleep", sleep)

    def tick(_name: str, cap: int) -> int:
        caps.append(cap)
        return CONTINUE

    drive_cli.run_drive(
        gated,
        tick,
        10,
        load=lambda: 0.0,
        requests=_runner(gated, _Spawner(forbid=True)),
        host=_readings(CALM, BUSY),
    )

    assert caps == [3]  # the tick saw a calm host; the overload began after it
    _assert_waiting_for_host_load(ledger, queued[0])


def _drive_with_request(
    cfg: DispatchConfig,
    ledger: Any,
    per_tick: list[HostReading],
    *,
    queue_at_tick: int,
) -> tuple[list[int], list[rr.RunRequest | None], _Spawner]:
    """Drive one epic for ``len(per_tick)`` ticks; the host reads
    ``per_tick[n]`` from the admission of tick n until the next one's, request
    probes included. A ``kew dispatch 7`` is queued during tick
    ``queue_at_tick`` (0 = before the drive starts). Returns each tick's cap,
    the request as each tick saw it, and the spawner."""
    gated = _gated(cfg)
    spawn = _Spawner()
    caps: list[int] = []
    seen: list[rr.RunRequest | None] = []
    queued: list[str] = []
    reading = [per_tick[0]]

    def queue() -> None:
        request, _ = rr.queue(ledger, "kew dispatch 7", operator="tom", source="web")
        queued.append(request.id)

    def tick(_name: str, cap: int) -> int:
        caps.append(cap)
        if len(caps) == queue_at_tick:
            queue()
        seen.append(rr.get_request(ledger, queued[0]) if queued else None)
        if len(caps) == len(per_tick):
            holder = current_holder(ledger)
            assert holder is not None
            request_stop(ledger, holder, None)
        else:
            reading[0] = per_tick[len(caps)]
        return CONTINUE

    if queue_at_tick == 0:
        queue()
    drive_cli.run_drive(
        gated,
        tick,
        1,
        load=lambda: 0.0,
        requests=_runner(gated, spawn),
        host=lambda *, swap: reading[0],
    )
    return caps, seen, spawn


def test_a_held_request_starts_once_two_calm_ticks_end_the_episode(
    cfg: DispatchConfig, ledger: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """queued → held (still queued, with the host load as its reason) →
    started by the pass after the second calm tick."""
    monkeypatch.setenv("KEW_ACTOR", "tom")

    caps, seen, spawn = _drive_with_request(
        cfg, ledger, [BUSY, CALM, CALM], queue_at_tick=0
    )

    assert caps == [0, 0, 3]
    for request in seen:
        assert request is not None and request.state == rr.QUEUED
        assert (request.detail or "").startswith(WAITING)
    assert spawn.calls == [["dispatch", "7"]]
    [started] = rr.list_requests(ledger)
    assert started.state != rr.QUEUED
    assert not (started.detail or "").startswith(WAITING)


def test_request_probe_does_not_count_as_second_calm_tick(
    cfg: DispatchConfig, ledger: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tick 1 is overloaded and tick 2 the first calm read. A request queued
    then is probed calm on every request pass before tick 3, yet it waits for
    tick 3's calm read to end the episode."""
    monkeypatch.setenv("KEW_ACTOR", "tom")

    caps, seen, spawn = _drive_with_request(
        cfg, ledger, [BUSY, CALM, CALM], queue_at_tick=2
    )

    assert caps == [0, 0, 3]
    waiting = seen[2]
    assert waiting is not None and waiting.state == rr.QUEUED
    assert (waiting.detail or "").startswith(WAITING)
    assert spawn.calls == [["dispatch", "7"]]


@pytest.mark.parametrize("paused", [False, True])
def test_a_round_with_no_epic_to_tick_still_counts_a_calm_read(
    cfg: DispatchConfig,
    ledger: Any,
    monkeypatch: pytest.MonkeyPatch,
    paused: bool,
) -> None:
    """A request probe opens an episode while no epic is due (an empty
    rotation, or every epic paused); each round still takes one calm read, so
    the episode ends and the queued request starts without any tick."""
    monkeypatch.setenv("KEW_ACTOR", "tom")
    gated = _gated(cfg)
    if paused:
        pause(ledger, None)
    else:
        from kew.service.verbs import set_epic_archived

        set_epic_archived(
            ledger, epic_ref="epic:a", archived=True, reason="", config=cfg
        )
    request, _ = rr.queue(ledger, "kew dispatch 7", operator="tom", source="web")
    reads = [BUSY]
    spawn = _Spawner()
    sleeps: list[float] = []

    def sleep(s: float) -> None:
        sleeps.append(s)
        if len(sleeps) == 5:
            holder = current_holder(ledger)
            assert holder is not None
            request_stop(ledger, holder, None)

    def host(*, swap: bool) -> HostReading:
        return reads.pop(0) if reads else CALM

    def tick(_name: str, _cap: int) -> int:
        raise AssertionError("no epic is due")

    monkeypatch.setattr(cli, "_sleep", sleep)
    drive_cli.run_drive(
        gated,
        tick,
        1,
        load=lambda: 0.0,
        requests=_runner(gated, spawn),
        host=host,
    )

    assert spawn.calls == [["dispatch", "7"]]
    started = rr.get_request(ledger, request.id)
    assert started is not None and started.state != rr.QUEUED


def test_a_calm_probe_neither_counts_nor_ends_an_episode(
    cfg: DispatchConfig, ledger: Any
) -> None:
    gated = _gated(cfg)
    host_gate(ledger, gated, _readings(BUSY))
    host_gate(ledger, gated, _readings(CALM))

    probed = host_gate(ledger, gated, _readings(CALM), tick=False)

    assert probed.hold is not None and probed.hold.calm == 1
    assert not probed.resumed
    assert host_load_episode(ledger) == probed.hold


def test_an_overloaded_probe_resets_the_calm_count(
    cfg: DispatchConfig, ledger: Any
) -> None:
    gated = _gated(cfg)
    host_gate(ledger, gated, _readings(BUSY))
    host_gate(ledger, gated, _readings(CALM))

    probed = host_gate(ledger, gated, _readings(BUSY), tick=False)

    assert probed.hold is not None and probed.hold.calm == 0


# --------------------------------------------------------------------------- #
# Both holds at once: the operator's names the remedy
# --------------------------------------------------------------------------- #


def test_operator_hold_reason_wins_while_host_is_overloaded(
    tmp_path: Path, cfg: DispatchConfig, ledger: Any
) -> None:
    """With `kew drive hold` standing and the host overloaded, the tick and
    `kew epic status` name the hold work actually waits on — `kew drive
    release` — never the host load's automatic recovery."""
    host_gate(ledger, _gated(cfg), _readings(BUSY))
    hold(ledger)
    config, max_parallel = _admit_under(DispatchConfig(), 5, 0)
    deps = FakeSource(nodes=[_node("1")], edges=[])
    facts = FakeFactsReader(issue_facts_by_num={1: _facts(1, state="OPEN")})

    result = _advance(
        deps=deps,
        facts=facts,
        store=ledger,
        sink=FakeSink(),
        lock_path=tmp_path / "epic.lock",
        config=config,
        max_parallel=max_parallel,
    )
    lease = acquire_drive(ledger)
    assert isinstance(lease, DriveLease)
    summary = summarize(
        EPIC, build_graph(deps, EPIC), config=cfg, store=ledger, facts=facts
    )

    [ticked] = result.waiting
    [status] = [w.reason for w in summary.waiting if w.node_id == "1"]
    for reason in (ticked.reason, status):
        assert reason.startswith("admission-held: admission held by `kew drive hold`")
        assert "`kew drive release` lifts it" in reason
        assert "host load" not in reason
