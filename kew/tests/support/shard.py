"""Split the xdist phase into N balanced groups, one per verify VM (#2344).

The parallel phase is one process pool on one machine: `-n auto` buys the
cores of a single box, and after #2340/#2341/#2342 that box's wall clock *is*
the suite's wall clock. The remaining lever is more boxes, so `--shard=K/N`
deselects all but the K-th of N groups and the verify host runs the N commands
on N VMs at once.

Three properties make that safe, and `tests/test_shard.py` pins all three:

* **Every test lands in exactly one shard.** The groups are a partition of the
  collection the hook is handed — not a filter each shard applies to the ids it
  happens to like — so the union of the N runs is exactly what the unsharded
  command selects. A sharding scheme that can drop a test is worse than no
  sharding: the suite goes green having never run it.
* **The split is the same on every VM.** Each shard is a separate pytest on a
  separate machine that computes the whole partition and keeps its own slice,
  so the partition has to be a pure function of (collected ids, durations, N).
  It reads an explicit `sorted()` of the ids rather than collection order,
  which xdist and the filesystem both get to decide.
* **The groups are balanced by what the tests actually cost.** N shards only
  divide the wall clock if the slowest shard is near total/N, and test
  durations here span orders of magnitude: the slowest seventy of 18,900 tests
  hold 36% of the total, so a split that ignored cost could land most of them
  on one VM. So the assignment is greedy longest-processing-time first over the
  durations recorded in `.test_durations.json` — the classic 4/3 bound for this
  problem, and on a suite whose slowest test is 10s against a 226s third, even
  to a fraction of a percent.

Two kinds of test have no useful measurement, and they are handled differently
because they are different. One recorded at 0.00s was measured, just below the
resolution pytest prints: it is charged `NOMINAL_SECONDS` so it still weighs on
a shard (see there — charging it nothing silently piles every such test onto one
VM). One with no entry at all — added since the last refresh, or a
parametrisation whose id changed — was never measured, so there is nothing to
weigh it by; those are spread round-robin after the recorded ones, which keeps a
batch of new tests from arriving on one VM together.

The durations file is refreshed by `scripts/refresh_test_durations.py` from a
`--durations=0 --durations-min=0` run; it is committed, so a shard's split does
not depend on a timing run happening first, and a stale file costs balance
rather than correctness.
"""

from __future__ import annotations

import functools
import json
from collections.abc import Iterable
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parents[1]

#: The committed recording: node id → seconds (setup + call + teardown).
DURATIONS_PATH = TESTS_DIR / ".test_durations.json"

#: What a test recorded at 0.00s is charged. pytest prints durations to two
#: decimals, so anything under 5ms records as zero, and 10,175 of this suite's
#: 18,874 recorded tests do. Charging them nothing is what makes a perfectly
#: balanced split useless: a zero never moves a shard's total, so the lightest
#: shard stays the lightest and collects every one of them — three shards came
#: out an even 226s each holding 6,297, 6,297 and 13,080 tests, and the third VM
#: was the whole wall clock. Half the resolution is the honest estimate for a
#: number known only to lie in [0, 0.005), and it distorts the tests that do have
#: a measurement barely at all: all 10,175 together are 7% of the weighted total.
NOMINAL_SECONDS = 0.005


@functools.cache
def recorded_durations() -> dict[str, float]:
    """The committed durations, or an empty mapping when there are none.

    Absent or unreadable is not an error: the partition degrades to round-robin
    over sorted ids, which is still a partition. Refusing to run would make a
    corrupt dotfile break every shard on every VM, for a file whose only job is
    balance.
    """
    try:
        payload = json.loads(DURATIONS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        str(node): float(seconds)
        for node, seconds in payload.items()
        if isinstance(seconds, int | float) and seconds >= 0
    }


def shard_groups(
    node_ids: Iterable[str],
    shards: int,
    durations: dict[str, float] | None = None,
) -> tuple[frozenset[str], ...]:
    """Partition *node_ids* into *shards* groups of roughly equal duration.

    Pure: same ids, same durations, same N — same tuple of groups, whatever
    order the ids arrived in. Every id appears in exactly one group, so the
    union over all N is the input set and the groups are pairwise disjoint.
    """
    if shards < 1:
        raise ValueError(f"shards must be at least 1, got {shards}")
    recorded = recorded_durations() if durations is None else durations
    ids = sorted(set(node_ids))
    groups: list[list[str]] = [[] for _ in range(shards)]
    totals = [0.0] * shards
    # Longest first, so the big tests are placed while every shard is still
    # nearly empty and the small ones are left to even the result out.
    weights = {
        node: max(recorded[node], NOMINAL_SECONDS) for node in ids if node in recorded
    }
    for node in sorted(weights, key=lambda node: (-weights[node], node)):
        lightest = min(range(shards), key=lambda index: (totals[index], index))
        groups[lightest].append(node)
        totals[lightest] += weights[node]
    unknown = [node for node in ids if node not in recorded]
    for position, node in enumerate(unknown):
        groups[position % shards].append(node)
    return tuple(frozenset(group) for group in groups)


def parse_shard(spec: str) -> tuple[int, int]:
    """``"2/3"`` → ``(2, 3)``, raising :class:`pytest.UsageError` otherwise.

    A typo here is the one failure mode that loses tests silently: `--shard=0/3`
    read as an index would run two thirds of the suite across three VMs and
    report green. So every malformed form is a usage error before collection.
    """
    index_text, _, total_text = spec.partition("/")
    if not index_text.isdigit() or not total_text.isdigit():
        raise pytest.UsageError(f"--shard must look like K/N, got {spec!r}")
    index, total = int(index_text), int(total_text)
    if not 1 <= index <= total:
        raise pytest.UsageError(
            f"--shard must satisfy 1 <= K <= N, got {index}/{total}"
        )
    return index, total


# --- The pytest plugin -------------------------------------------------------
# Registered by ``tests/conftest.py`` in ``pytest_configure`` rather than
# written there, because the deselection has to run LAST among the
# ``pytest_collection_modifyitems`` implementations and conftest already has
# one that has to run FIRST (it applies the ``serial`` marker that pytest's own
# ``-m`` deselection then reads). One module cannot hold both under one name.
#
# Running last is what makes the shards balance the phase that actually runs:
# `-m "not serial"` has already removed the serial phase by then, so its 285
# tests and 40s do not sit in the budget of shards that will never execute them.
# ``tests/test_shard.py`` pins the ordering against the live plugin manager.

_SHARD = pytest.StashKey[tuple[int, int]]()
_SELECTED = pytest.StashKey[int]()
_RECORDED = pytest.StashKey[float]()


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    spec = config.getoption("shard", default=None)
    if not spec:
        return
    index, total = parse_shard(spec)
    keep = shard_groups((item.nodeid for item in items), total)[index - 1]
    dropped = [item for item in items if item.nodeid not in keep]
    if dropped:
        config.hook.pytest_deselected(items=dropped)
    items[:] = [item for item in items if item.nodeid in keep]
    recorded = recorded_durations()
    config.stash[_SHARD] = (index, total)
    config.stash[_SELECTED] = len(items)
    config.stash[_RECORDED] = sum(recorded.get(item.nodeid, 0.0) for item in items)


def pytest_report_header(config: pytest.Config) -> str | None:
    """Name the shard before collection, so a log says which slice it is.

    What belongs here is the fact that this run is a slice at all: a shard's log
    otherwise reads as a suite that has mysteriously lost two thirds of its
    tests. The counts are not known yet — a header runs before collection — and
    the summary below adds them wherever the collection happened in this
    process, which an xdist controller's never does. So this line, not that one,
    is the one guaranteed to appear.
    """
    spec = config.getoption("shard", default=None)
    if not spec:
        return None
    index, total = parse_shard(spec)
    recorded = recorded_durations()
    source = (
        f"{len(recorded)} recorded durations"
        if recorded
        else f"no durations at {DURATIONS_PATH.name}, splitting round-robin"
    )
    return f"--shard={index}/{total}: running group {index} of {total} ({source})"


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    """What this slice came to, where this process is the one that collected it.

    Silent on an xdist controller: it never collects, so it has no partition to
    report, and the header above is what names the shard there.
    """
    stash = terminalreporter.config.stash
    shard = stash.get(_SHARD, None)
    if shard is None:
        return
    index, total = shard
    terminalreporter.write_line(
        f"shard {index}/{total}: selected {stash[_SELECTED]} tests, "
        f"{stash[_RECORDED]:.0f}s recorded"
    )
