"""Refused executions must not inspect command-guard files."""

from unittest.mock import patch

import pytest

from kew.sandbox import command_guard_state, posture_record_fields


@pytest.mark.parametrize("runner", ["claude-code", "codex"])
def test_refused_posture_never_inspects_command_guard(tmp_path, runner):
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    preserved = b"[" * 20_000 + b"]" * 20_000
    settings.write_bytes(preserved)

    with patch("kew.sandbox.command_guard_state", wraps=command_guard_state) as guard:
        try:
            fields = posture_record_fields(
                runner=runner,
                posture="refused",
                egress_verification="unavailable",
                checkout_dir=tmp_path,
            )
        finally:
            guard.assert_not_called()

    assert fields["command_guard"] == "not-applicable"
    assert settings.read_bytes() == preserved
