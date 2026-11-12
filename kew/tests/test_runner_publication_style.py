"""Every shipped runner declares who performs GitHub publication."""

import pytest

from kew.runners.claude_code import ClaudeCodeRunner
from kew.runners.hermes import HermesRunner
from kew.runners.lemonade import LemonadeRunner
from kew.runners.opencode import OpenCodeRunner
from kew.runners.pi import PiRunner


@pytest.mark.parametrize(
    "runner",
    [
        ClaudeCodeRunner(),
        OpenCodeRunner(),
        PiRunner(),
        HermesRunner(),
        LemonadeRunner(),
    ],
)
def test_existing_runners_publish_their_own_host_runs(runner):
    assert runner.publication_style == "agent"
