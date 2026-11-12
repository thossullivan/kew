"""Each runner declares native loop style."""

from kew.inference_transport import RunnerTransportCapability
from kew.runners.claude_code import ClaudeCodeRunner
from kew.runners.opencode import OpenCodeRunner
from kew.runners.pi import PiRunner
from kew.runners.registry import KNOWN_RUNNER_NAMES, get_runner


def test_all_runners_declare_native_loop_style():
    assert ClaudeCodeRunner().loop_style == "native"
    assert OpenCodeRunner().loop_style == "native"
    assert PiRunner().loop_style == "native"


def test_registered_runners_publicly_declare_transport_capability():
    for name in KNOWN_RUNNER_NAMES:
        capability = get_runner(name).transport_capability
        if name == "claude-code":
            assert capability.incoming_api_formats == ("anthropic-messages",)
            assert capability.endpoint_override is True
            assert capability.model_override is True
        elif name == "codex":
            assert capability == RunnerTransportCapability(
                incoming_api_formats=("openai-responses",),
                endpoint_override=True,
                model_override=True,
            )
        else:
            assert capability == RunnerTransportCapability()
