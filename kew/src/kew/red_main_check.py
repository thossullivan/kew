"""A PR verify's evidence that it ran a red main's failing tests (#3195).

While main is red at a commit whose failing tests failed alone there
(:data:`kew.main_health.REPRODUCED`), a PR verification runs exactly those
node ids once more after its gate, in a phase of its own. The phase writes
kew's report plugin (:mod:`kew.red_main_report_plugin`) beside the checkout,
never in it, under a name drawn on the host, and runs the ids with it loaded.
The phase opens its section of the log with a nonce drawn on the host and
reports the plugin's JSON on one line tagged with it.

The phase never fails the job: the gate already decided the verdict, and this
only records, per id, what the candidate's tree did with it. A landed PR
clears a reproduced red only when every failing id reads ``passed``
(:func:`kew.main_health.read_main_health`); one the run never reported, or
skipped, or xfailed, does not clear it.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import shlex
from collections.abc import Iterable, Sequence
from importlib import resources

logger = logging.getLogger(__name__)

#: The prefix of every line the phase reports in the job log.
MARK = "kew-red-main:"

#: What the plugin reads its report path from.
REPORT_ENV = "KEW_RED_MAIN_REPORT"

#: The outcome that counts as the candidate having run and passed a test.
PASSED = "passed"

_RANDOM_HEX = "$(od -An -N{n} -tx1 /dev/urandom | tr -d ' \\n')"
_BEGIN = re.compile(rf"^{MARK} begin ([0-9a-f]{{16}})$", re.MULTILINE)


def phase_lines(tests: Sequence[str]) -> list[str]:
    """The job lines that run *tests* with kew's report plugin, from ``kew/``."""
    if not tests:
        return []
    source = resources.files("kew").joinpath("red_main_report_plugin.py")
    encoded = base64.b64encode(source.read_bytes()).decode("ascii")
    ids = " ".join(shlex.quote(test) for test in tests)
    return [
        f'red_main_nonce="{_RANDOM_HEX.format(n=8)}"',
        f'red_main_mark="{MARK}$red_main_nonce"',
        f'echo "{MARK} begin $red_main_nonce"',
        'red_main_dir="$(mktemp -d "${TMPDIR:-/tmp}/kew-red-main-XXXXXX")"',
        f'red_main_plugin="kew_red_main_{_RANDOM_HEX.format(n=8)}"',
        f"printf '%s' {shlex.quote(encoded)} | base64 -d "
        '> "$red_main_dir/$red_main_plugin.py"',
        f'{REPORT_ENV}="$red_main_dir/report.json" '
        'PYTHONPATH="$red_main_dir${PYTHONPATH:+:$PYTHONPATH}" '
        'uv run pytest -p no:cacheprovider -p "$red_main_plugin" -q '
        f'{ids} > "$red_main_dir/run.log" 2>&1 || true',
        'red_main_report="$red_main_dir/report.json"',
        'if [ -f "$red_main_report" ]; then',
        '    echo "$red_main_mark report $(tr -d \'\\n\' < "$red_main_report")"',
        "else",
        '    echo "$red_main_mark report none"',
        "fi",
        'rm -rf "$red_main_dir"',
    ]


def outcomes(logs: Iterable[str], tests: Sequence[str]) -> dict[str, str] | None:
    """What the phase found for each of *tests*, or ``None`` with no evidence.

    Exactly one job of the verification opens the phase. A log that opens it
    more than once, a report that is not a JSON object, and a phase that never
    reported are no evidence at all. An id the report does not name reads
    ``absent``.
    """
    report = _report(logs) if tests else None
    if report is None:
        return None
    try:
        found = json.loads(report)
    except ValueError as exc:
        logger.warning("the red-main phase reported no readable outcomes: %s", exc)
        return None
    if not isinstance(found, dict):
        return None
    return {test: str(found.get(test, "absent")) for test in tests}


def _report(logs: Iterable[str]) -> str | None:
    """The one report line of the one opened phase, untagged."""
    opened = [(log, match) for log in logs for match in _BEGIN.finditer(log)]
    if len(opened) != 1:
        return None
    log, match = opened[0]
    tag = f"{MARK}{match.group(1)} report "
    reports = [
        line[len(tag) :]
        for line in log[match.end() :].splitlines()
        if line.startswith(tag)
    ]
    return reports[0] if len(reports) == 1 else None
