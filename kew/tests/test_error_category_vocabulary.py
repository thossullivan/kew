"""The demo's error-category vocabulary is the one the export publishes (#3144).

``kew report provenance`` writes a run's ``error_category`` only when it is in
:data:`kew.report_provenance.PUBLISHED_ERROR_CATEGORIES`, and ``"other"`` for
any other code. The public demo's allowlist schema
(``frontend/demo/provenance-schema.ts``) refuses a code outside
``frontend/demo/error-categories.json`` and ``"other"``. The two lists are one
vocabulary in two languages, so they are held equal here: a code added to one
and not the other either refuses a real export at build time or publishes a
code the schema was never shown.
"""

from __future__ import annotations

import json
from pathlib import Path

from kew.report_provenance import OTHER_ERROR_CATEGORY, PUBLISHED_ERROR_CATEGORIES

_VOCABULARY = "frontend/demo/error-categories.json"


def _vocabulary_file() -> Path:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / _VOCABULARY
        if candidate.is_file():
            return candidate
    raise AssertionError(
        f"{_VOCABULARY} is missing: the schema would read no vocabulary"
    )


def test_the_demo_vocabulary_is_the_export_allowlist() -> None:
    listed = json.loads(_vocabulary_file().read_text(encoding="utf-8"))["categories"]
    assert listed == sorted(set(listed)), f"{_VOCABULARY}: keep it sorted, once each"
    assert listed == sorted(PUBLISHED_ERROR_CATEGORIES)
    assert OTHER_ERROR_CATEGORY not in listed
