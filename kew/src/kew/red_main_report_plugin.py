"""kew's report of a red main's failing tests, run inside a PR verify (#3195).

Never imported by kew. While main is red at a commit whose failing tests
failed alone there, a PR verification runs exactly those node ids once more
after its gate, with this file loaded as a pytest plugin under a name drawn on
the host. It writes one JSON object, node id to outcome, to the path
``KEW_RED_MAIN_REPORT`` names, and :mod:`kew.red_main_check` reads that back.
A landed PR clears the red only when every failing id reads ``passed`` here:
an id the run never reported (a dropped parameter, a deleted class) is absent,
and a skip or an xfail is not a pass.

It depends on nothing but the stdlib: pytest calls its hooks by name.
"""

from __future__ import annotations

import json
import os
from typing import Any

#: Taken out of the environment as the plugin loads, so a pytest a test
#: starts of its own never writes over the report.
_REPORT = os.environ.pop("KEW_RED_MAIN_REPORT", "")

#: The worse of two outcomes for one id wins: a test whose call passed and
#: whose teardown failed did not pass.
_RANK = {"passed": 0, "skipped": 1, "xfailed": 2, "xpassed": 3, "failed": 4}

_outcomes: dict[str, str] = {}


def _outcome(report: Any) -> str | None:
    if getattr(report, "wasxfail", None) is not None:
        return "xpassed" if report.passed else "xfailed"
    if report.failed:
        return "failed"
    if report.skipped:
        return "skipped"
    if report.when == "call":
        return "passed"
    return None


def pytest_runtest_logreport(report: Any) -> None:
    found = _outcome(report)
    if found is None:
        return
    seen = _outcomes.get(report.nodeid)
    if seen is None or _RANK[found] > _RANK[seen]:
        _outcomes[report.nodeid] = found


def pytest_sessionfinish(session: Any, exitstatus: Any) -> None:
    if not _REPORT:
        return
    with open(_REPORT, "w", encoding="utf-8") as handle:
        json.dump(_outcomes, handle, separators=(",", ":"))
