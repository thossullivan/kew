"""How a cost grows with its input, measured so that load cannot fake it.

A fixed wall-clock bound ("under 0.5 s") states how fast the machine is, not
how the code scales: a shared runner under full contention misses it, and a
polynomial path can still fit under it on a fast machine. A complexity guard
asks the other question. :func:`cost_growth` runs the same work at two sizes
and returns the ratio of their costs. Linear work grows by about the size
factor (less while a fixed overhead dominates), quadratic work by its square,
so the ratio is the same on a fast machine and on one ten times slower.

Each cost is this thread's CPU time, not wall time: time spent descheduled
behind other processes is not counted. It is the least of several runs with
the garbage collector off, because a stall or a collection only ever adds.

Growth does not see a cost that keeps its shape and becomes twenty times
larger. :func:`cost_in_calibrations` does: it counts a cost in runs of one
fixed workload measured the same way in the same process, so the unit is as
slow as the machine is and a budget in that unit holds on any of them.
"""

from __future__ import annotations

import gc
import re
import time
from collections.abc import Callable

#: Runs per size; the least is kept.
RUNS = 5
#: The size step a guard measures across.
FACTOR = 8
#: The most a linear cost may grow across :data:`FACTOR`: three times the
#: factor, while quadratic work grows by its square (64) and cubic by 512.
LINEAR_CEILING = 3 * FACTOR
#: The floor for a cost, so a run under the clock's resolution cannot divide by 0.
_FLOOR_SECONDS = 1e-6


def least_cpu_seconds(work: Callable[[], object], runs: int = RUNS) -> float:
    """The least CPU time, in this thread, that *work* takes over *runs* runs."""
    collecting = gc.isenabled()
    gc.disable()
    try:
        best = float("inf")
        for _ in range(runs):
            started = time.thread_time()
            work()
            best = min(best, time.thread_time() - started)
    finally:
        if collecting:
            gc.enable()
    return max(best, _FLOOR_SECONDS)


def cost_growth(
    work_at: Callable[[int], Callable[[], object]], size: int, factor: int = FACTOR
) -> float:
    """How many times costlier the work is at ``size * factor`` than at *size*.

    *work_at* builds the input for a size and returns the work to time, so
    building the input is not counted.
    """
    small = least_cpu_seconds(work_at(size))
    large = least_cpu_seconds(work_at(size * factor))
    return large / small


_CALIBRATION_SCAN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{3,}=|\b[0-9a-f]{12,}\b")
_CALIBRATION_TEXT = (
    "The gateway rejected the request with retries=3 at commit 0123456789abcdef; "
    "the runner waits 5 s and sends it again. "
) * 30


def _calibration() -> None:
    """The unit of a budget: a counting loop, a regex scan and a split.

    Bytecode, the regex engine and string methods are where text handling
    spends its time, so the unit moves with all three and not with one.
    """
    total = 0
    for index in range(4_000):
        total += index % 7
    for _ in range(40):
        _CALIBRATION_SCAN.findall(_CALIBRATION_TEXT)
        _CALIBRATION_TEXT.lower().split()


def cost_in_calibrations(work: Callable[[], object], runs: int = RUNS) -> float:
    """How many runs of the calibration workload *work* costs on this machine."""
    return least_cpu_seconds(work, runs) / least_cpu_seconds(_calibration, runs)
