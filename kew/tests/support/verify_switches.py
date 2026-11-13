"""The switches ``scripts/verify.sh`` reads, for tests that run it.

``kew verify`` starts the script as ``KEW_VERIFY_STAGE=suite bash
scripts/verify.sh``, and the script runs this suite. A test that runs the
script again builds its environment from :func:`environ_without_switches`, so
a switch in the environment it was started from never decides what the
script under test runs: the test sets the switches it means.
"""

from __future__ import annotations

import os

#: Every switch verify.sh reads to run only part of its gate.
VERIFY_SWITCHES = (
    "KEW_VERIFY_STAGE",
    "KEW_VERIFY_SUITE",
    "KEW_VERIFY_FRONTEND",
    "KEW_VERIFY_IMAGE_SCAN",
)

#: What the suite job of a tiered merge verify sets around verify.sh.
SUITE_JOB_SWITCHES = {
    "KEW_VERIFY_STAGE": "suite",
    "KEW_VERIFY_SUITE": "selected",
    "KEW_VERIFY_FRONTEND": "full",
}


def environ_without_switches() -> dict[str, str]:
    """This process's environment, minus every :data:`VERIFY_SWITCHES` name."""
    return {
        name: value for name, value in os.environ.items() if name not in VERIFY_SWITCHES
    }
