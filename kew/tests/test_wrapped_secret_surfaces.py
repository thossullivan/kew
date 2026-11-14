"""A known secret wrapped across lines is redacted on kew's production surfaces.

``redact_credentials`` and ``runner_text`` replace a key label's value before
they match known secrets, and the diagnostic tail of a killed runner keeps
stderr lines one at a time. Each of those once let the second half of a
wrapped secret through. The tail still redacts each line as it arrives, so a
wrapped value after a credential label on stderr is a documented limit, not
pinned here. All secrets here are placeholders.
"""

from __future__ import annotations

import os
from collections.abc import Callable

import pytest
from support.cost_growth import (
    FACTOR,
    LINEAR_CEILING,
    cost_growth,
    cost_in_calibrations,
)

from kew.credential_surface import redact_credentials, runner_text
from kew.exit_signal import remember_line, signal_exit_reason

_SECRET = "FAKEtestKEY0123456789abcdefXYZ"
_HEAD, _TAIL = _SECRET[:15], _SECRET[15:]

_LABELS = [
    "token=",
    "api_key: ",
    "Authorization: Bearer ",
    "password=",
    "secret: ",
    "x-api-key: ",
    "export MY_API_TOKEN=",
]


@pytest.fixture
def known_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_API_TOKEN", _SECRET)


def _leaks(text: str) -> bool:
    return _HEAD in text or _TAIL in text


@pytest.mark.usefixtures("known_secret")
@pytest.mark.parametrize("label", _LABELS)
def test_a_wrapped_secret_after_a_key_label_is_redacted(label: str) -> None:
    text = f"{label}{_HEAD}\n{_TAIL} tail"

    redacted = redact_credentials(text)

    assert not _leaks(redacted), redacted
    assert redacted.startswith(label.split()[0])


@pytest.mark.usefixtures("known_secret")
@pytest.mark.parametrize("label", _LABELS)
def test_runner_text_redacts_a_wrapped_secret_after_a_key_label(label: str) -> None:
    redacted = runner_text(f"{label}{_HEAD}\n{_TAIL} tail")

    assert not _leaks(redacted), redacted


@pytest.mark.usefixtures("known_secret")
def test_a_wrapped_secret_in_prose_keeps_the_surrounding_words() -> None:
    redacted = redact_credentials(f"got {_HEAD}\n{_TAIL} back")

    assert redacted == "got <MY_API_TOKEN redacted> back"


@pytest.mark.usefixtures("known_secret")
def test_a_killed_runner_reason_redacts_a_secret_its_stderr_wrapped() -> None:
    tail: list[str] = []
    remember_line(tail, f"error: rejected {_HEAD}")
    remember_line(tail, f"{_TAIL} by the gateway")

    reason = signal_exit_reason(137, tail)

    assert not _leaks(reason), reason
    assert reason == (
        "killed by SIGKILL (exit 137); last output: "
        "error: rejected <MY_API_TOKEN redacted> by the gateway"
    )


@pytest.mark.usefixtures("known_secret")
def test_a_killed_runner_reason_keeps_its_line_separator() -> None:
    tail: list[str] = []
    remember_line(tail, "first line")
    remember_line(tail, "second line")

    reason = signal_exit_reason(134, tail)

    assert reason.endswith("last output: first line | second line")


def test_runner_text_cost_grows_linearly_with_a_whitespace_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "FAKEfakeFAKE+fake0123abcd+ABCDabcdABCD0123"
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", secret)
    first, second, _ = secret.split("+")

    def line(spaces: int) -> str:
        return first + " " * spaces + second + " " * spaces + "!"

    def redact_line(spaces: int) -> Callable[[], object]:
        text = line(spaces)
        return lambda: runner_text(text)

    growth = cost_growth(redact_line, 100)

    assert growth < LINEAR_CEILING, f"{FACTOR}x the run cost {growth:.0f}x"
    assert runner_text(line(5000)) == line(5000)


def _agent_output() -> list[str]:
    """Lines of the kinds a runner prints; three of them hold the secret."""
    return [
        "Running the tests for the files I touched before I open the pull request.",
        "tests/test_selector.py::test_an_empty_queue_selects_nothing PASSED [ 12%]",
        '{"type":"tool_use","name":"Bash","input":{"command":"uv run ruff check ."}}',
        "diff --git a/src/kew/selector.py b/src/kew/selector.py",
        "+    return sorted(issues, key=lambda issue: (issue.priority, issue.number))",
        "Traceback (most recent call last):",
        '  File "/workspace/src/kew/selector.py", line 88, in select_next_issue',
        "ValueError: invalid literal for int() with base 10: 'eleven'",
        "error: failed to push some refs to 'https://forge.example/acme/widgets.git'",
        f"export MY_API_TOKEN={_SECRET}",
        f"Authorization: Bearer {_SECRET}",
        f"The gateway rejected the request ({_SECRET}); retrying in 5 s.",
        "=" * 30 + " 214 passed, 3 skipped in 41.07s " + "=" * 30,
        "",
    ]


def test_runner_text_on_ordinary_agent_output_stays_within_its_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Redacting a line of agent output stays cheap, not only linear.

    ``runner_text`` runs on every line a runner prints. A change that keeps
    its cost linear and makes it twenty times larger passes every growth
    guard; this budget, counted in runs of a calibration workload on the same
    machine, does not let it through.
    """
    # The cost of a line grows with the number of variables the process holds
    # (each call reads them all), so the environment is the test's own: the
    # secret and 64 ordinary variables.
    for name in list(os.environ):
        monkeypatch.delenv(name)
    monkeypatch.setenv("MY_API_TOKEN", _SECRET)
    for index in range(64):
        monkeypatch.setenv(f"ORDINARY_SETTING_{index}", "a plain value of no interest")
    lines = _agent_output()

    def redact_every_line() -> None:
        for _ in range(10):
            for line in lines:
                runner_text(line)

    cost = cost_in_calibrations(redact_every_line)

    assert not any(_leaks(runner_text(line)) for line in lines)
    # Measured: 12 to 15 calibration runs without coverage (macOS and Linux,
    # idle or under full load) and 27 to 33 with coverage, which CI uses,
    # because tracing slows this code more than the calibration loop. The
    # budget leaves more than four times that for another machine's mix of
    # speeds; it catches a slowdown of about 5 times under coverage and of 10
    # to 12 times without.
    assert cost < 145, f"140 lines cost {cost:.0f} calibration runs"
