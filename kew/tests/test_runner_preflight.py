"""Runner preflight tests that use explicit host-discovery behavior."""

from pathlib import Path

import pytest

from kew.config import DispatchConfig
from kew.runner_preflight import runner_preflight_result


def test_missing_runner_binary_still_refuses_dispatch(
    real_runner_preflight: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The opt-out preserves production behavior for an absent runner."""
    monkeypatch.setenv("PATH", str(tmp_path))

    failure, warn_cli_login = runner_preflight_result(DispatchConfig())

    assert failure == (
        "Runner 'claude-code' needs the 'claude' CLI, which was not found. "
        "Install: npm install -g @anthropic-ai/claude-code"
    )
    assert warn_cli_login is False
