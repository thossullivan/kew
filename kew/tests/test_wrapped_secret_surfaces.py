"""A known secret wrapped across lines is redacted on kew's production surfaces.

``redact_credentials`` and ``runner_text`` replace a key label's value before
they match known secrets, and the diagnostic tail of a killed runner keeps
stderr lines one at a time. Each of those once let the second half of a
wrapped secret through. The tail still redacts each line as it arrives, so a
wrapped value after a credential label on stderr is a documented limit, not
pinned here. All secrets here are placeholders.
"""

from __future__ import annotations

import time

import pytest

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


def test_runner_text_on_a_long_whitespace_line_finishes_quickly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "FAKEfakeFAKE+fake0123abcd+ABCDabcdABCD0123"
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", secret)
    first, second, _ = secret.split("+")
    line = first + " " * 5000 + second + " " * 5000 + "!"

    started = time.perf_counter()
    redacted = runner_text(line)

    assert time.perf_counter() - started < 0.5
    assert redacted == line
