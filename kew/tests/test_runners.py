"""Session identifiers must pass runner grammar before resume command creation."""

from unittest.mock import MagicMock, patch

import pytest

from kew.config import DispatchConfig
from kew.orchestrator import _build_agent_cmd

UUID = "12345678-1234-5678-9abc-123456789abc"


@pytest.mark.parametrize(
    ("name", "valid"),
    [
        ("claude-code", UUID),
        ("codex", UUID),
        ("pi", UUID),
        ("lemonade", UUID),
        ("opencode", "ses_0123456789abcdefghijklmnop"),
        ("hermes", "20260725_153412_d9da19"),
    ],
)
@pytest.mark.parametrize(
    "invalid",
    ["http://user:s3cret@proxy.invalid:3128", "--help", "bad id", "wrong", UUID + "\n"],
)
def test_resume_identifier_grammar(name, valid, invalid):
    runner = MagicMock()
    runner.name = name
    runner.supports_resume.return_value = True
    runner.build_resume_command.return_value = ["agent", "--resume", valid]
    with patch("kew.orchestrator.find_timeout_cmd", return_value=None):
        cmd, _, _ = _build_agent_cmd(runner, "go", DispatchConfig(), {}, valid, "")
        assert valid in cmd
        runner.build_resume_command.reset_mock()
        with pytest.raises(ValueError, match="session identifier") as error:
            _build_agent_cmd(runner, "go", DispatchConfig(), {}, invalid, "")
    runner.build_resume_command.assert_not_called()
    assert invalid not in str(error.value)


def test_resume_rejects_credential_even_when_uuid_shaped(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", f"http://user:{UUID}@proxy.invalid")
    runner = MagicMock()
    runner.name = "claude-code"
    with pytest.raises(ValueError, match="session identifier"):
        _build_agent_cmd(runner, "go", DispatchConfig(), {}, UUID, "")
    runner.build_resume_command.assert_not_called()


@pytest.mark.parametrize(
    "name", ["claude-code", "codex", "opencode", "pi", "lemonade", "hermes"]
)
def test_adapter_redacts_decoded_text_before_truncation(monkeypatch, name):
    import json

    from kew.runners import get_runner

    password = "s3cret" * 60
    secret = f"http://user:{password}@proxy.invalid:3128"
    monkeypatch.setenv("HTTP_PROXY", secret)
    payloads = {
        "claude-code": {"type": "assistant", "message": secret},
        "codex": {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": secret},
        },
        "opencode": {"type": "text", "part": {"text": secret}},
        "pi": {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": secret}],
            },
        },
        "lemonade": {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": secret}],
            },
        },
    }
    line = secret if name == "hermes" else json.dumps(payloads[name])
    events = get_runner(name).parse_line(line)
    messages = [event.message for event in events if event.event_type == "activity"]
    assert messages
    assert password[:8] not in " ".join(messages)
    assert "redacted" in " ".join(messages)
