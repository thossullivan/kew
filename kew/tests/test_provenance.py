"""Tests for the additive orchestrator provenance payload."""

from __future__ import annotations

import subprocess
from unittest.mock import patch

from kew.provenance import orchestrator_provenance


def test_orchestrator_provenance_records_commit_and_dirty_state() -> None:
    responses = iter(
        [
            subprocess.CompletedProcess([], 0, "abc123\n", ""),
            subprocess.CompletedProcess([], 0, " M src/kew/config.py\n", ""),
        ]
    )
    with patch(
        "kew.provenance.run", side_effect=lambda *args, **kwargs: next(responses)
    ):
        result = orchestrator_provenance()

    assert result["orchestrator_version"]
    assert result["orchestrator_commit"] == "abc123"
    assert result["orchestrator_dirty"] is True


def test_orchestrator_provenance_reports_unknown_git_state() -> None:
    with patch(
        "kew.provenance.run",
        return_value=subprocess.CompletedProcess([], 1, "", "not a repository"),
    ):
        result = orchestrator_provenance()

    assert result["orchestrator_commit"] is None
    assert result["orchestrator_dirty"] is None
