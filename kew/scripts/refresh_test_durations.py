"""Refresh ``tests/.test_durations.json`` from a ``--durations=0`` run (#2344).

``--shard=K/N`` (``tests/support/shard.py``) balances the N groups by what each
test actually costs, and the costs are a committed recording rather than a
measurement each shard takes for itself — a shard cannot time the tests it is
about to skip, and three VMs timing the suite to decide how to split the suite
would cost exactly what sharding saves.

So the recording is made once, by hand, from a full run of both phases:

    cd kew
    uv run pytest -n auto -m "not serial" --durations=0 --durations-min=0 \\
        | tee /tmp/xdist.txt
    uv run pytest -m serial --durations=0 --durations-min=0 | tee /tmp/serial.txt
    uv run python scripts/refresh_test_durations.py /tmp/xdist.txt /tmp/serial.txt

``--durations-min=0`` matters: ``--durations=0`` lists every test but still hides
the ones under 0.005s, and a recording missing the fast tests would leave a
third of the suite looking free.

Per test, the recorded number is setup + call + teardown, which is what a shard
pays. A stale recording costs balance and never correctness: the partition in
``shard.py`` covers every collected id whether it is recorded or not.

The reports named on the command line ARE the recording — it is replaced, so a
deleted test leaves instead of lingering. Name every report of the run; a run
cut short is topped up by passing its reports and the second pass's together.

Kept stdlib-only: it runs from a plain ``python`` on the verify host.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterable
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parents[1] / "tests"
DURATIONS_PATH = TESTS_DIR / ".test_durations.json"

#: ``write_sep("=", "slowest durations")`` — pytest's own section banner. Only
#: lines inside such a section are read, so a test whose captured output happens
#: to look like a duration line cannot enter the recording.
_SECTION = re.compile(r"^=+ slowest (\d+ )?durations? =+$")
#: A ``=``/``-`` banner ends the section it follows.
_BANNER = ("=", "-")
#: ``0.30s call     tests/test_x.py::test_y[a b]`` — the id may hold spaces, so
#: it runs to the end of the line.
_ENTRY = re.compile(r"^(\d+(?:\.\d+)?)s\s+(setup|call|teardown)\s+(\S.*)$")
#: Every id a run from ``kew/`` produces. A log captured from the repo root
#: names tests ``kew/tests/...`` and would record a whole suite of ids that
#: collection never yields, i.e. a recording that balances nothing.
_ID_PREFIX = "tests/"


def parse_durations(lines: Iterable[str]) -> dict[str, float]:
    """Sum setup + call + teardown per node id, from the report's own section.

    The LAST durations section in the report, not every section: this suite's
    tests run child pytests, and a failing one prints the child's whole output
    — banner, duration lines and all — in its captured-output block, which
    pytest reports BEFORE its own durations. Reading every section let a
    handful of a child's measurements of two ids outlive the parent's
    measurement of fifteen thousand.
    """
    sections: list[dict[str, float]] = []
    current: dict[str, float] | None = None
    for raw in lines:
        line = raw.rstrip("\n")
        if _SECTION.match(line.strip()):
            current = {}
            sections.append(current)
            continue
        if current is None:
            continue
        if line.startswith(_BANNER):
            current = None
            continue
        entry = _ENTRY.match(line)
        if entry is None:
            continue
        seconds, _, node_id = entry.groups()
        current[node_id] = round(current.get(node_id, 0.0) + float(seconds), 4)
    totals = sections[-1] if sections else {}
    # Checked on the section that is kept, never on a decoy: a test may print
    # anything, and a report refused because of its captured output would be a
    # script nobody could use.
    foreign = sorted(node for node in totals if not node.startswith(_ID_PREFIX))
    if foreign:
        raise SystemExit(
            f"{foreign[0]!r} is not a 'tests/...' id: the report was captured "
            "somewhere other than kew/, so its ids would never be collected"
        )
    return totals


def _read(path: str) -> list[str]:
    if path == "-":
        return sys.stdin.read().splitlines()
    return Path(path).read_text(encoding="utf-8", errors="replace").splitlines()


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    # The reports given ARE the recording: it is replaced, not added to, so a
    # renamed or deleted test leaves rather than accumulating forever. Pass
    # every report of the run — topping a cut-short run up means naming both
    # its reports and the second pass's, not the second pass's alone.
    #
    # Within that, reports MERGE rather than add up. The three phases of one
    # test are summed (above), but two reports naming the same test are two
    # measurements of it and the later one wins; summing would double an
    # overlapping test's cost on every top-up.
    durations: dict[str, float] = {}
    for path in argv:
        durations.update(parse_durations(_read(path)))
    if not durations:
        print(
            "refresh_test_durations: no duration lines found. The run needs "
            "--durations=0 --durations-min=0 and its full output.",
            file=sys.stderr,
        )
        return 1
    was: dict[str, float] = {}
    if DURATIONS_PATH.exists():
        was = json.loads(DURATIONS_PATH.read_text(encoding="utf-8"))
    DURATIONS_PATH.write_text(
        json.dumps(durations, sort_keys=True, indent=1) + "\n", encoding="utf-8"
    )
    print(
        f"{DURATIONS_PATH}: {len(durations)} tests, "
        f"{sum(durations.values()):.0f}s total "
        f"(+{len(set(durations) - set(was))} new, "
        f"-{len(set(was) - set(durations))} gone)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
