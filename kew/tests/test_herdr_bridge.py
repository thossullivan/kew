"""The herdr bridge: the drive's state as one OSC 7501 report per change.

``scripts/herdr_bridge.py`` reads two kew surfaces — ``kew drive status
--json`` and a snapshot ``kew epic watch --json`` per epic the drive has
ticked — and reports one fixed state word to the herdr pane it runs in. The
kew CLI is the boundary: every test hands the bridge a fake ``kew`` (a
callable, or an executable on disk for the end-to-end runs) and checks what
the bridge reports.

The script is loaded by path: ``scripts/`` is not an importable package.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
import unicodedata
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "herdr_bridge.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("herdr_bridge", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


hb = _load()

#: Every character a hostile journal string could use to reach the terminal:
#: ESC (starts every escape sequence), BEL and ST (end an OSC), the 8-bit CSI
#: and OSC introducers, and a bidi override that reorders what a person reads.
HOSTILE = "x\x1b]7501;state=done:app=evil\x07\x1b\\\x9b31m\x9d0;t\x9c\u202eevil"


def _report(state: str) -> str:
    return f"\x1b]7501;state={state}:app=kew\x1b\\"


def _tick(exit_code: int | None = 10, *, finished: bool = True) -> dict[str, Any]:
    return {
        "started_at": "2026-11-16T00:00:00+00:00",
        "finished_at": "2026-11-16T00:00:06+00:00" if finished else None,
        "exit_code": exit_code if finished else None,
    }


def _drive(
    state: str,
    rotation: Sequence[str] = (),
    *,
    ticks: dict[str, dict[str, Any]] | None = None,
    **extra: Any,
) -> tuple[int, str]:
    """A `kew drive status --json` answer. A running drive has finished a
    routine tick of every epic in its rotation unless `ticks` says otherwise."""
    doc: dict[str, Any] = {"state": state, "heartbeat": None, **extra}
    if state == "running" and "heartbeat" not in extra:
        doc["heartbeat"] = {
            "rotation": list(rotation),
            "ticks": {name: _tick() for name in rotation} if ticks is None else ticks,
        }
    return (0 if state == "running" else 1), json.dumps(doc)


def _events(*kinds: str, item: str = "#1") -> tuple[int, str]:
    lines = [
        json.dumps({"kind": kind, "epic": "e", "item": item, "reason": "r"})
        for kind in kinds
    ]
    return (1 if lines else 0), "\n".join(lines) + ("\n" if lines else "")


class FakeKew:
    """A kew CLI that answers only the exact argv the bridge must send."""

    def __init__(
        self, drive: tuple[int, str], epics: dict[str, tuple[int, str]]
    ) -> None:
        self.answers = {("drive", "status", "--json"): drive}
        for epic, answer in epics.items():
            self.answers[("epic", "watch", "--json", "--", epic)] = answer

    def __call__(self, args: Sequence[str]) -> tuple[int, str]:
        return self.answers.get(tuple(args), (2, ""))


@pytest.mark.parametrize(
    ("drive", "epics", "state"),
    [
        (_drive("not running"), {}, "idle"),
        (_drive("not running", exit={"code": 0, "reason": "stopped"}), {}, "idle"),
        (_drive("not running", exit={"code": 2, "reason": "lost lock"}), {}, "error"),
        (_drive("not running", refusal={"reason": "schema"}), {}, "error"),
        (_drive("running"), {}, "idle"),
        (_drive("running", heartbeat=None), {}, "working"),
        (_drive("gone", ["a"]), {}, "error"),
        ((2, json.dumps({"state": "refused"})), {}, "error"),
        ((2, "Traceback"), {}, "error"),
        (_drive("running", ["a"]), {"a": _events()}, "working"),
        (_drive("running", ["a"]), {"a": _events("merge_ready")}, "blocked"),
        (_drive("running", ["a"]), {"a": _events("a_kind_from_later")}, "blocked"),
        (_drive("running", ["a"]), {"a": _events("usage_limit_warning")}, "blocked"),
        (_drive("running", ["a"]), {"a": _events("epic_complete")}, "idle"),
        (
            _drive("running", ticks={"a": _tick(0)}),
            {"a": _events("epic_complete")},
            "idle",
        ),
        (_drive("running", ["a"], ticks={"a": _tick(1)}), {"a": _events()}, "blocked"),
        (_drive("running", ["a"], ticks={"a": _tick(11)}), {"a": _events()}, "working"),
        (_drive("running", ["a"], ticks={"a": _tick(2)}), {"a": _events()}, "error"),
        (_drive("running", ["a"], ticks={"a": _tick(None)}), {"a": _events()}, "error"),
        (
            _drive("running", ["a", "b"]),
            {"a": _events("epic_complete"), "b": _events()},
            "working",
        ),
        (
            _drive("running", ["a", "b"]),
            {"a": _events("epic_complete"), "b": _events("held")},
            "blocked",
        ),
        (_drive("running", ["a"]), {"a": (2, "")}, "error"),
        (_drive("running", ["a"]), {"a": (1, "not json\n")}, "error"),
        (_drive("running", ["a"]), {"a": (1, "")}, "error"),
        (
            _drive("running", ["a", "b"]),
            {"a": _events("held"), "b": (2, "")},
            "error",
        ),
    ],
)
def test_the_drive_and_its_epics_map_to_one_herdr_state(
    drive: tuple[int, str], epics: dict[str, tuple[int, str]], state: str
) -> None:
    assert hb.Bridge(FakeKew(drive, epics)).poll().state == state


def test_a_stalled_driver_lock_blocks_even_when_the_journal_is_quiet() -> None:
    stalled = [{"epic": "a", "pid": 1, "action": "kill 1"}]
    kew = FakeKew(_drive("running", ["a"], stalled_locks=stalled), {"a": _events()})
    reading = hb.Bridge(kew).poll()
    assert reading.state == "blocked"
    assert "a: driver lock stalled" in reading.detail


def test_a_stalled_tick_says_an_operator_is_needed() -> None:
    kew = FakeKew(_drive("running", ["a"], ticks={"a": _tick(1)}), {"a": _events()})
    assert "a: stalled, needs an operator" in hb.Bridge(kew).poll().detail


def test_a_paused_epic_the_drive_ticked_before_still_blocks() -> None:
    """Paused epics leave the rotation but stay in the heartbeat's ticks."""
    kew = FakeKew(
        _drive("running", ["a"], ticks={"a": _tick(), "b": _tick()}),
        {"a": _events("epic_complete"), "b": _events("held")},
    )
    assert hb.Bridge(kew).poll().state == "blocked"


@pytest.mark.parametrize(
    "ticks",
    [{"done1": _tick()}, {"done1": _tick(), "new": _tick(finished=False)}],
    ids=["not ticked", "first tick running"],
)
def test_an_epic_with_no_finished_tick_is_not_read_and_keeps_the_drive_working(
    ticks: dict[str, dict[str, Any]],
) -> None:
    """With no journal a snapshot falls back to a live forge read; the bridge
    waits for the epic's first finished tick instead. The fake answers no
    watch of `new`, so reading it would report error."""
    kew = FakeKew(
        _drive("running", ["done1", "new"], ticks=ticks),
        {"done1": _events("epic_complete")},
    )
    assert hb.Bridge(kew).poll().state == "working"


def test_a_running_tick_keeps_what_the_epic_last_said() -> None:
    """A blocked epic stays blocked while its next tick runs: no flicker to
    working each round, and no read of a journal the tick is rewriting."""
    kew = FakeKew(_drive("running", ["a"]), {"a": _events("held")})
    bridge = hb.Bridge(kew)
    assert bridge.poll().state == "blocked"

    running = FakeKew(_drive("running", ["a"], ticks={"a": _tick(finished=False)}), {})
    bridge.kew = running
    assert bridge.poll().state == "blocked"


def _heartbeat(
    stage: str, rotation: Sequence[str], ticks: dict[str, dict[str, Any]]
) -> tuple[int, str]:
    beat = {"stage": stage, "rotation": list(rotation), "ticks": ticks}
    return _drive("running", heartbeat=beat)


def test_a_drive_restart_keeps_a_blocked_pane_blocked() -> None:
    """A restarted drive process starts with no rotation and no ticks, then
    ticks its epics one by one; what each said before still stands."""
    bridge = hb.Bridge(
        FakeKew(_drive("running", ["a", "b"]), {"a": _events("held"), "b": _events()})
    )
    assert bridge.poll().state == "blocked"

    bridge.kew = FakeKew(_heartbeat("starting", [], {}), {})
    assert bridge.poll().state == "blocked"

    bridge.kew = FakeKew(
        _heartbeat("ticking", ["a", "b"], {"b": _tick(finished=False)}), {}
    )
    assert bridge.poll().state == "blocked"


def test_a_paused_epic_the_bridge_read_before_still_blocks_after_a_restart() -> None:
    bridge = hb.Bridge(FakeKew(_drive("running", ["a"]), {"a": _events("held")}))
    assert bridge.poll().state == "blocked"

    bridge.kew = FakeKew(_heartbeat("ticking", ["b"], {"b": _tick()}), {"b": _events()})
    assert bridge.poll().state == "blocked"


def test_a_failing_epic_stays_an_error_while_its_next_tick_runs() -> None:
    bridge = hb.Bridge(FakeKew(_drive("running", ["a"], ticks={"a": _tick(2)}), {}))
    assert bridge.poll().state == "error"

    bridge.kew = FakeKew(
        _drive("running", ["a"], ticks={"a": _tick(finished=False)}), {}
    )
    assert bridge.poll().state == "error"


def test_a_starting_drive_with_nothing_known_is_working() -> None:
    reading = hb.Bridge(FakeKew(_heartbeat("starting", [], {}), {})).poll()
    assert reading == hb.Reading("working", "the drive is starting")


@pytest.mark.parametrize(
    "drive_doc",
    [
        {"state": "running", "heartbeat": ["a"]},
        {"state": "running", "heartbeat": "x"},
        {"state": "running", "heartbeat": {"rotation": 5}},
        {"state": "running", "heartbeat": {"rotation": "ab"}},
        {"state": "running", "heartbeat": {"rotation": [1]}},
        {"state": "running", "heartbeat": {"rotation": [], "ticks": ["a"]}},
        {"state": "running", "heartbeat": {"rotation": [], "ticks": "a"}},
        {"state": "running", "heartbeat": {"rotation": [], "ticks": {"a": "x"}}},
        {"state": "running", "heartbeat": None, "stalled_locks": "a"},
        {"state": "running", "heartbeat": None, "stalled_locks": [{"epic": 3}]},
    ],
)
def test_a_shape_kew_never_promised_reports_error_instead_of_crashing(
    drive_doc: dict[str, Any],
) -> None:
    """Epic `a` answers as complete, so only the shape check can say error."""
    kew = FakeKew((0, json.dumps(drive_doc)), {"a": _events("epic_complete")})
    assert hb.Bridge(kew).poll().state == "error"


def test_an_epic_named_like_an_option_is_still_read_as_an_epic() -> None:
    kew = FakeKew(_drive("running", ["--replay"]), {"--replay": _events("held")})
    assert hb.Bridge(kew).poll().state == "blocked"


def test_a_blocked_report_names_the_epic_kind_and_item() -> None:
    kew = FakeKew(
        _drive("running", ["release"]), {"release": _events("merge_ready", item="#7")}
    )
    assert "release: merge_ready #7" in hb.Bridge(kew).poll().detail


class ScriptedKew:
    """Answers `kew drive status` from a script of polls, then a Ctrl-C."""

    def __init__(self, drives: list[tuple[int, str]]) -> None:
        self.drives = drives

    def __call__(self, args: Sequence[str]) -> tuple[int, str]:
        if not self.drives:
            raise KeyboardInterrupt
        return self.drives.pop(0)


def test_each_change_is_reported_once_and_ctrl_c_leaves_the_pane_idle(
    capsys: pytest.CaptureFixture[str],
) -> None:
    gone = _drive("gone")
    kew = ScriptedKew([_drive("not running"), _drive("not running"), gone, gone])

    hb.run(kew, interval=0, once=False)

    reports = re.findall(
        r"\x1b\]7501;state=(\w+):app=kew\x1b\\", capsys.readouterr().out
    )
    assert reports == ["idle", "error", "idle"]


def test_the_interval_has_a_floor(capsys: pytest.CaptureFixture[str]) -> None:
    for bad in ("0", "-1", "nan", "inf"):
        with pytest.raises(SystemExit) as exit_:
            hb.main(["--interval", bad, "--once"])
        assert exit_.value.code == 2


def _fake_kew_executable(tmp_path: Path, answers: dict[str, list[Any]]) -> Path:
    """An executable `kew` that answers by argv from a JSON table."""
    table = tmp_path / "answers.json"
    table.write_text(json.dumps(answers))
    exe = tmp_path / "kew"
    exe.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        f"answers = json.load(open({str(table)!r}))\n"
        "code, out = answers.get(' '.join(sys.argv[1:]), [2, ''])\n"
        "sys.stdout.write(out)\n"
        "sys.exit(code)\n"
    )
    exe.chmod(0o755)
    return exe


def _run_once(kew: Path) -> bytes:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--kew", str(kew), "--once"],
        capture_output=True,
        check=True,
        timeout=60,
    ).stdout


def _assert_only_the_report_controls(stdout: bytes, state: str) -> str:
    report = _report(state).encode()
    assert stdout.count(report) == 1
    rest = stdout.replace(report, b"").decode()
    # Its own oracle, not the script's `is_control`: a broken sanitizer must
    # fail this test, not agree with it.
    controls = [c for c in rest if c != "\n" and unicodedata.category(c)[0] == "C"]
    assert controls == []
    assert "evil" in rest, "the text is shown, only its control characters go"
    return rest


def test_hostile_journal_text_never_reaches_the_terminal(tmp_path: Path) -> None:
    epic = f"epic{HOSTILE}"
    kew = _fake_kew_executable(
        tmp_path,
        {
            "drive status --json": list(_drive("running", [epic])),
            f"epic watch --json -- {epic}": list(
                _events(f"held{HOSTILE}", item=HOSTILE)
            ),
        },
    )
    _assert_only_the_report_controls(_run_once(kew), "blocked")


def test_a_hostile_drive_state_never_reaches_the_terminal(tmp_path: Path) -> None:
    kew = _fake_kew_executable(
        tmp_path, {"drive status --json": [1, json.dumps({"state": HOSTILE})]}
    )
    _assert_only_the_report_controls(_run_once(kew), "error")


def test_a_hostile_unreadable_epic_name_never_reaches_the_terminal(
    tmp_path: Path,
) -> None:
    epic = f"epic{HOSTILE}"
    kew = _fake_kew_executable(
        tmp_path,
        {
            "drive status --json": list(_drive("running", [epic])),
            f"epic watch --json -- {epic}": [2, ""],
        },
    )
    _assert_only_the_report_controls(_run_once(kew), "error")


def test_one_run_reports_the_state_and_a_readable_line(tmp_path: Path) -> None:
    kew = _fake_kew_executable(
        tmp_path, {"drive status --json": list(_drive("not running"))}
    )

    stdout = _run_once(kew).decode()

    assert stdout.startswith(_report("idle"))
    assert re.search(r"\d\d:\d\d:\d\d idle +no drive runs", stdout)


def test_a_missing_kew_reports_an_error_instead_of_crashing(tmp_path: Path) -> None:
    stdout = _run_once(tmp_path / "no-such-kew").decode()
    assert stdout.startswith(_report("error"))
