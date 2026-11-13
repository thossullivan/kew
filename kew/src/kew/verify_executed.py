"""Which collected tests a shard really ran: its collect log against its junit.

A shard counts its selection with a ``--collect-only`` pass and then runs that
selection in a second pytest process. The count alone proves only what was
*collected*: a ``conftest.py`` hook that keeps a test at collect-only time and
deselects it at run time passes the union check while the test never runs
(#2629). So the run writes junit, and this compares the node ids the collect
pass listed against the test cases the run reported, and fails on any test
that was collected and never reported.

This module is not imported on the verify host. kew ships its *source* inside
the shard's job script (:func:`kew.merge_verify.executed_lines`) and the host
runs it with ``python -I -``, so the comparison a candidate's checkout is
checked by is the operator's kew, not a file the candidate could rewrite. That
is why it depends on nothing but the stdlib and pytest, which the shard's own
run has just proved is installed.

Its one line of output, :data:`REPORT_FORMAT`, is what kew reads back
(:func:`kew.merge_verify.read_executed`).
"""

from __future__ import annotations

import sys

# The only XML parsed is the junit the shard's own pytest run just wrote.
import xml.etree.ElementTree as ET  # nosec B405
from pathlib import Path

#: The line kew reads back off the shard's log. Keep it in step with
#: ``kew.merge_verify.EXECUTED_PATTERN``; ``tests/test_verify_shards.py`` pins
#: the two together.
REPORT_FORMAT = "kew-verify: {executed} of {collected} collected tests executed"

#: How many never-run ids are printed. The count is the verdict; these are the
#: start of the explanation.
_MISSING_SHOWN = 40


def collected_ids(log: str) -> list[str]:
    """The node ids a ``--collect-only -q`` pass listed, in its order.

    pytest prints one id per line, then a blank line, then its summary; what
    follows the blank line (warnings, the count) is not an id.
    """
    ids: list[str] = []
    for line in log.splitlines():
        if not line.strip():
            break
        if "::" in line and not line.startswith(" "):
            ids.append(line)
    return ids


def junit_key(nodeid: str) -> tuple[str, str]:
    """A node id as junit names it: ``(classname, name)``, pytest's own mapping."""
    from _pytest.junitxml import mangle_test_address

    names = mangle_test_address(nodeid)
    return ".".join(names[:-1]), names[-1]


def executed_keys(junit: str) -> set[tuple[str, str]]:
    """Every test case the run's junit reported, run or skipped."""
    root = ET.fromstring(junit)  # nosec B314
    return {
        (case.get("classname", ""), case.get("name", ""))
        for case in root.iter("testcase")
    }


def never_run(collected: list[str], executed: set[tuple[str, str]]) -> list[str]:
    """The collected ids the run never reported, in collection order."""
    return [nodeid for nodeid in collected if junit_key(nodeid) not in executed]


def main(argv: list[str]) -> int:
    """``<collect log> <junit xml>`` → the report line, and 1 if a test never ran.

    An unreadable junit is a failure, not a pass with nothing to compare: a run
    that wrote no report proved nothing about what it ran.
    """
    log_path, junit_path = argv
    collected = collected_ids(Path(log_path).read_text(encoding="utf-8"))
    try:
        executed = executed_keys(Path(junit_path).read_text(encoding="utf-8"))
    except (OSError, ET.ParseError) as exc:
        print(f"kew-verify: no readable junit report: {exc}", file=sys.stderr)
        return 1
    missing = never_run(collected, executed)
    print(
        REPORT_FORMAT.format(
            executed=len(collected) - len(missing), collected=len(collected)
        )
    )
    if not missing:
        return 0
    print(
        f"kew-verify: {len(missing)} collected tests were never run:",
        file=sys.stderr,
    )
    for nodeid in missing[:_MISSING_SHOWN]:
        print(f"  {nodeid}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
