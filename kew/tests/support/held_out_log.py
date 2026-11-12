"""Held-out job log lines, as the held-out phase prints them.

A run's verdict reaches the log as one ``report`` line: the base64 of the JSON
file kew's report plugin wrote (``src/kew/held_out_report_plugin.py``).

In a job's log the phase opens its own section with a nonce and tags each of
its lines with it (``kew-held-out:<nonce> exit 0 …``); :func:`opened` writes
lines the way a host returns them, and ``held_out_report`` reads them untagged
as :func:`kew.merge_verify.held_out_section` hands them over.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Sequence

MARK = "kew-held-out:"

#: A nonce, as the phase draws one: 32 hex digits.
NONCE = "0123456789abcdef" * 2


def report_line(
    *failed: str,
    passed: Sequence[str] = (),
    label: str = "report",
    refused: str | None = None,
    uncollected: Sequence[str] = (),
) -> str:
    """One run's report line, naming *failed* and *passed* — and *uncollected*,
    the base's tests the run never collected, as the plugin lists them."""
    report: dict[str, object] = {
        "failed": list(failed),
        "passed": list(passed),
        "refused": refused,
        "exitstatus": 1 if failed or uncollected else 0,
    }
    if uncollected:
        report["uncollected"] = list(uncollected)
    encoded = base64.b64encode(json.dumps(report).encode("utf-8")).decode("ascii")
    return f"{MARK} {label} {encoded}\n"


def opened(text: str, *, nonce: str = NONCE) -> str:
    """*text* as a job's log carries it: the phase's lines in its own section.

    Every ``kew-held-out: …`` line is tagged with *nonce*, behind one opening
    line that names it; any other line (the gate's output) stays before it.
    """
    lines = text.splitlines()
    before = [line for line in lines if not line.startswith(f"{MARK} ")]
    phase = [
        f"{MARK}{nonce} {line[len(MARK) + 1 :]}"
        for line in lines
        if line.startswith(f"{MARK} ")
    ]
    return "".join(f"{line}\n" for line in [*before, f"{MARK} begin {nonce}", *phase])
