"""The run page's no-artifact kinds are the kinds kew never publishes for (#3148).

The console's run page reads an unsealed run as blocked on its seal, except for
a run kind that produces no artifact: there it says the run publishes nothing
(``frontend/src/lib/flight-recorder.ts`` ``NO_ARTIFACT_RUN_KINDS``, read from
``frontend/src/lib/no-artifact-run-kinds.json``). kew's own list of those kinds
is :data:`kew.publication_credentials._NON_PUBLICATION_RUN_KINDS`. The two are
one set in two languages, so they are held equal here: a kind added to one and
not the other either tells an operator that a publishing run has nothing to
publish, or blocks a review run on a seal kew never makes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kew.publication_credentials import _NON_PUBLICATION_RUN_KINDS

# It reads a frontend file, so a JSON-only edit runs it in every targeted round.
pytestmark = pytest.mark.policy

_KINDS_FILE = "frontend/src/lib/no-artifact-run-kinds.json"


def _kinds_file() -> Path:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / _KINDS_FILE
        if candidate.is_file():
            return candidate
    raise AssertionError(f"{_KINDS_FILE} is missing: the run page would read no kinds")


def test_the_run_page_no_artifact_kinds_are_the_non_publication_kinds() -> None:
    listed = json.loads(_kinds_file().read_text(encoding="utf-8"))["kinds"]
    assert listed == sorted(set(listed)), f"{_KINDS_FILE}: keep it sorted, once each"
    assert listed == sorted(_NON_PUBLICATION_RUN_KINDS)
