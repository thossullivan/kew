"""A known secret is redacted however terminal output spells it.

Agent and tool output wraps long lines and colours words, so a secret kew knows
can reach retained text split by a newline or interrupted by an ANSI escape
sequence. These tests drive ``source_forms`` and ``redact`` on such text. All
secrets here are placeholders.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest
from support.cost_growth import FACTOR, LINEAR_CEILING, cost_growth

from kew.credential_redaction import Form, redact, source_forms

_SECRET = "sk-placeholder0123456789"  # 24 characters
_MARKER = "[REDACTED:TOKEN]"


def _forms(secret: str) -> list[tuple[str, Form]]:
    return [("TOKEN", form) for form in source_forms(secret)]


def _redact(text: str, secret: str = _SECRET) -> str:
    return redact(text, _forms(secret), {"TOKEN": _MARKER})


def test_a_known_secret_split_by_a_newline_is_redacted() -> None:
    text = f"key: {_SECRET[:12]}\n{_SECRET[12:]} done"

    redacted = _redact(text)

    assert redacted == f"key: {_MARKER} done"


def test_a_known_secret_split_by_a_wrapped_indented_line_is_redacted() -> None:
    text = f"key: {_SECRET[:10]}\r\n    {_SECRET[10:]}\n"

    assert _redact(text) == f"key: {_MARKER}\n"


def test_a_known_secret_interrupted_by_an_ansi_sequence_is_redacted() -> None:
    text = f"key: {_SECRET[:8]}\x1b[1;31m{_SECRET[8:]}\x1b[0m done"

    redacted = _redact(text)

    # The escape inside the secret goes with it; the one after it stays.
    assert redacted == f"key: {_MARKER}\x1b[0m done"


def test_a_known_secret_interrupted_by_an_osc_sequence_is_redacted() -> None:
    text = f"{_SECRET[:5]}\x1b]0;title\x07{_SECRET[5:]}"

    assert _redact(text) == _MARKER


def test_a_known_secret_split_by_colour_and_newline_in_unicode_text() -> None:
    text = f"clé → {_SECRET[:8]}\x1b[31m\n{_SECRET[8:]} ✓"

    assert _redact(text) == f"clé → {_MARKER} ✓"


def test_a_known_secret_interrupted_by_a_json_escaped_colour_code_is_redacted() -> None:
    # Codex --json JSONL output carries the escape as the six characters \u001b.
    text = json.dumps({"text": f"{_SECRET[:8]}\x1b[1;31m{_SECRET[8:]}"})

    assert _redact(text) == f'{{"text": "{_MARKER}"}}'


def test_text_without_the_secret_keeps_its_ansi_sequences() -> None:
    text = "\x1b[32mok\x1b[0m sk-placeholder\n0123 unrelated"

    assert _redact(text) == text


def test_a_short_secret_is_not_joined_across_whitespace() -> None:
    assert _redact("ab cde and ab\ncde", secret="abcde") == "ab cde and ab\ncde"
    assert _redact("token abcde here", secret="abcde") == f"token {_MARKER} here"


def test_a_medium_secret_is_not_joined_across_whitespace() -> None:
    secret = "placeholder123"  # under the joined-match length

    assert _redact("placeholder 123", secret=secret) == "placeholder 123"
    assert _redact("placeholder123", secret=secret) == _MARKER


def test_a_known_secret_inside_an_osc_title_is_redacted() -> None:
    text = f"\x1b]0;{_SECRET}\x07 done"

    assert _redact(text) == f"\x1b]0;{_MARKER}\x07 done"


def test_a_known_secret_inside_an_osc_8_hyperlink_url_is_redacted() -> None:
    text = f"\x1b]8;;https://example.invalid/?t={_SECRET}\x1b\\link\x1b]8;;\x1b\\"

    redacted = _redact(text)

    assert _SECRET not in redacted
    assert redacted == (
        f"\x1b]8;;https://example.invalid/?t={_MARKER}\x1b\\link\x1b]8;;\x1b\\"
    )


def test_a_known_secret_right_after_a_bare_csi_introducer_is_redacted() -> None:
    text = f"\x1b[{_SECRET} done"

    assert _redact(text) == f"\x1b[{_MARKER} done"


def _redaction_at(
    text_at: Callable[[int], str], secret: str = _SECRET
) -> Callable[[int], Callable[[], object]]:
    """The redaction of ``text_at(size)``, its forms built before it is timed."""
    forms = _forms(secret)

    def work_at(size: int) -> Callable[[], object]:
        text = text_at(size)
        return lambda: redact(text, forms, {"TOKEN": _MARKER})

    return work_at


def test_a_secret_whose_plus_signs_decode_to_spaces_is_redacted_in_linear_time() -> (
    None
):
    # A decoding stage turns each "+" into a space; a joined pattern that kept
    # those spaces backtracked polynomially on long whitespace runs.
    secret = "FAKEfakeFAKE+fake0123abcd+ABCDabcdABCD0123"
    first, second, _ = secret.split("+")

    def text_at(spaces: int) -> str:
        return first + " " * spaces + second + " " * spaces + "!"

    growth = cost_growth(_redaction_at(text_at, secret), 100)

    assert growth < LINEAR_CEILING, f"{FACTOR}x the run cost {growth:.0f}x"
    assert _redact(text_at(5000), secret=secret) == text_at(5000)


def test_a_secret_with_five_plus_signs_is_redacted_in_linear_time() -> None:
    secret = "FAKE+test+KEY0+1234+abcd+XYZW"
    parts = secret.split("+")

    def text_at(spaces: int) -> str:
        return (" " * spaces).join(parts[:-1]) + " " * spaces + "!"

    # Five gaps backtrack to a high power of their width: from 3 to 24 spaces
    # a backtracking join grows over 10,000x; one that does not, under 3x.
    growth = cost_growth(_redaction_at(text_at, secret), 3)

    assert growth < LINEAR_CEILING, f"{FACTOR}x the gaps cost {growth:.0f}x"
    assert _redact(text_at(2000), secret=secret) == text_at(2000)


def test_a_plus_secret_wrapped_at_its_decoded_space_is_still_redacted() -> None:
    secret = "FAKEfakeFAKE+fake0123abcd+ABCDabcdABCD0123"
    first, second, third = secret.split("+")

    redacted = _redact(f"key: {first} {second}\n{third} done", secret=secret)

    assert redacted == f"key: {_MARKER} done"


@pytest.mark.parametrize(
    "escape",
    [
        pytest.param("\x1b(B\x1b[m", id="tput-sgr0"),
        pytest.param("\x1b7\x1b8", id="save-restore-cursor"),
        pytest.param("\x1b=", id="keypad-mode"),
        pytest.param("\x1bPq#0\x1b\\", id="dcs"),
        pytest.param("\x1b_payload\x1b\\", id="apc"),
        pytest.param("\x1b^payload\x1b\\", id="pm"),
        pytest.param("\x1bXpayload\x1b\\", id="sos"),
        pytest.param("\x9d0;title\x9c", id="osc-8-bit"),
        pytest.param("\x90q#0\x9c", id="dcs-8-bit"),
        pytest.param("\x1b]0;title\x9c", id="osc-8-bit-terminator"),
    ],
)
def test_a_known_secret_interrupted_by_an_ecma_48_escape_is_redacted(
    escape: str,
) -> None:
    text = f"key: {_SECRET[:8]}{escape}{_SECRET[8:]} done"

    assert _redact(text) == f"key: {_MARKER} done"


def test_text_full_of_unterminated_string_introducers_is_scanned_in_linear_time() -> (
    None
):
    def text_at(introducers: int) -> str:
        return "\x9d\x90\x1b]\x1bP" * introducers + _SECRET

    growth = cost_growth(_redaction_at(text_at), 100)

    assert growth < LINEAR_CEILING, f"{FACTOR}x the text cost {growth:.0f}x"
    assert _redact(text_at(4000)) == "\x9d\x90\x1b]\x1bP" * 4000 + _MARKER
