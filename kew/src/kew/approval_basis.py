"""The approval estimate's ``basis``: written once, and read back in words.

:func:`kew.approval.estimate_dispatch_cost` records WHY an estimate is what it
is as a short basis string, which the audit payload stores verbatim and the
CLI's confirm prompt prints. The console read that string raw, so an approval
hold said "awaiting approval — no history — assuming per-issue cap of $25.00"
(design review c9, D12): debug output, naming no action.

The three formats live here, beside the reader that words them, so the two
cannot drift: the writer formats with these templates, and
:func:`approval_sentence` matches the same templates to say the hold as an
action ("approve a run estimated at $15.00, the per-issue cap; no cost history
to estimate from"). The stored basis string and the audit payload are
unchanged.

The amount is an ESTIMATE, never a ceiling: the per-issue cap is checked at
admission only (``kew.budget``), and what bounds a running agent's spend is
``agent.max_budget_usd``. So no sentence here says "up to".
"""

from __future__ import annotations

import re

#: The estimate is the per-issue cap: no matching run to estimate from.
NO_HISTORY = "no history — assuming per-issue cap of ${cap:.2f}"
#: The estimate is the per-issue cap: too few matching runs for a median.
FEW_RUNS = "only {count} similar run(s) — assuming per-issue cap of ${cap:.2f}"
#: The estimate is the median of the recent matching runs.
MEDIAN = "based on {count} similar runs (median ${median:.2f})"

_MONEY = r"\$(?P<usd>\d+(?:\.\d+)?)"
_NO_HISTORY = re.compile(rf"^no history — assuming per-issue cap of {_MONEY}$")
_FEW_RUNS = re.compile(
    rf"^only (?P<count>\d+) similar run\(s\) — assuming per-issue cap of {_MONEY}$"
)
_MEDIAN = re.compile(rf"^based on (?P<count>\d+) similar runs \(median {_MONEY}\)$")


def _runs(count: str) -> str:
    return f"{count} similar run" + ("" if count == "1" else "s")


def approval_sentence(basis: str, estimate_usd: float | None = None) -> str:
    """One approval hold as the action a person takes, in plain words.

    The amount is the one the basis states, which is the estimate it records;
    a basis this module did not write is kept as written after the action,
    with the hold's own estimate where the row carries one.
    """
    text = (basis or "").strip()
    if match := _NO_HISTORY.match(text):
        return (
            f"approve a run estimated at ${match['usd']}, the per-issue cap; "
            "no cost history to estimate from"
        )
    if match := _FEW_RUNS.match(text):
        return (
            f"approve a run estimated at ${match['usd']}, the per-issue cap; "
            f"only {_runs(match['count'])} to estimate from"
        )
    if match := _MEDIAN.match(text):
        return (
            f"approve a run estimated at ${match['usd']}, "
            f"the median of {_runs(match['count'])}"
        )
    action = (
        "approve a run"
        if estimate_usd is None
        else f"approve a run estimated at ${estimate_usd:.2f}"
    )
    return f"{action}; {text}" if text else action
