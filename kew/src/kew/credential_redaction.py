"""Bounded positional decoding and source-hull replacement for retained text."""

from __future__ import annotations

import base64
import binascii
import logging
import re
import unicodedata
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field

DECODE_PASSES = 4
MIN_SUBSTRING_CHARS = 6
#: A form this long also matches with whitespace between its characters.
MIN_JOINED_CHARS = 20
REFUSAL_MARKER = "<retained field refused>"
_ALPHABETS = (
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/",
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_",
)
_BLOCK_CHARACTERS = frozenset("".join(_ALPHABETS) + "0123456789%\\=")
_POSITIONAL = re.compile(
    r"(?:%[0-9A-Fa-f]{2})+"
    r"|\\(?:\r\n?|\n|[\\\"'abfnrtv/]"
    r"|x[0-9A-Fa-f]{2}"
    r"|u[dD][89aAbB][0-9A-Fa-f]{2}\\u[dD][c-fC-F][0-9A-Fa-f]{2}"
    r"|u[0-9A-Fa-f]{4}|U[0-9A-Fa-f]{8}"
    r"|N\{[^}\r\n]+\}|[0-7]{1,3})"
    r"|\+"
)
_SIMPLE_ESCAPES = dict(zip("abfnrtv\\\"'/", "\a\b\f\n\r\t\v\\\"'/", strict=True))
_HEX_RUN = re.compile(r"[0-9A-Fa-f]+")
_BASE64_RUNS = tuple(
    re.compile(rf"[{re.escape(alphabet)}=]+(?:\s+[{re.escape(alphabet)}=]+)*")
    for alphabet in _ALPHABETS
)
# ECMA-48 escapes: terminal control, never content. CSI (7- and 8-bit); the
# DCS, SOS, OSC, PM and APC strings (7- and 8-bit), whose body stops at any
# introducer or terminator so an unterminated one cannot rescan the text; nF
# escapes such as ESC ( B; and single-character Fp, Fe and Fs escapes such as
# ESC 7. No class repeats inside another, so a scan is linear in the text.
_ANSI_STRING_STOPS = "\x07\x1b\x90\x98\x9c\x9d\x9e\x9f"
_ANSI_SEQUENCE = re.compile(
    r"(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]"
    r"|(?:\x1b[P\]X^_]|[\x90\x98\x9d\x9e\x9f])"
    rf"[^{_ANSI_STRING_STOPS}]*(?:\x07|\x1b\\|\x9c)"
    r"|\x1b[ -/]+[0-~]"
    r"|\x1b(?![\[\]PX^_])[0-~]"
)
_ANSI_INTRODUCERS = frozenset("\x1b\x90\x98\x9b\x9d\x9e\x9f")
Span = tuple[int, int]


@dataclass(frozen=True)
class View:
    """Each character maps to one contiguous span in the original field."""

    text: str
    spans: tuple[Span, ...] = field(hash=False)


@dataclass(frozen=True)
class Form:
    """Literal and block patterns share the decoded source's matching tier."""

    text: str
    short: bool
    literal: re.Pattern[str]
    blocks: tuple[re.Pattern[str], ...]


@dataclass(frozen=True)
class Hull:
    start: int
    end: int
    name: str | None


def _normalized_view(text: str) -> View:
    """NFKC text without format characters, mapped to source."""
    if text.isascii():
        return View(text, tuple((i, i + 1) for i in range(len(text))))
    characters: list[str] = []
    spans: list[Span] = []
    _normalize_into(text, 0, len(text), characters, spans)
    return View("".join(characters), tuple(spans))


def _normalize_into(
    text: str, start: int, end: int, characters: list[str], spans: list[Span]
) -> None:
    for index in range(start, end):
        for item in unicodedata.normalize("NFKC", text[index]):
            if unicodedata.category(item) != "Cf":
                characters.append(item)
                spans.append((index, index + 1))


def _unicode_escape(spelling: str) -> str:
    number = int(spelling[2:6], 16)
    if len(spelling) == 12:
        number = 0x10000 + ((number - 0xD800) << 10) + int(spelling[8:], 16) - 0xDC00
    return chr(number)


def _escape_value(spelling: str) -> str:
    kind = spelling[1]
    if kind in _SIMPLE_ESCAPES:
        return _SIMPLE_ESCAPES[kind]
    if kind in "\r\n":
        return ""
    if kind == "u":
        return _unicode_escape(spelling)
    if kind == "N":
        return unicodedata.lookup(spelling[3:-1])
    if kind in "xU":
        return chr(int(spelling[2:], 16))
    return chr(int(spelling[1:], 8))


def _percent_view(spelling: str, spans: Sequence[Span], codec: str) -> View:
    raw = bytes.fromhex(spelling.replace("%", ""))
    decoded = raw.decode(codec, errors="surrogateescape")
    mapped: list[Span] = []
    cursor = 0
    for character in decoded:
        width = len(character.encode(codec, errors="surrogateescape")) * 3
        mapped.append((spans[cursor][0], spans[cursor + width - 1][1]))
        cursor += width
    return View(decoded, tuple(mapped))


def _decoded_piece(
    spelling: str, spans: Sequence[Span], codec: str, plus: bool
) -> View:
    if spelling.startswith("%"):
        return _percent_view(spelling, spans, codec)
    if spelling == "+":
        value = " " if plus else "+"
    else:
        try:
            value = _escape_value(spelling)
        except (ValueError, KeyError):
            return View(spelling, tuple(spans))
    hull = (spans[0][0], spans[-1][1])
    return View(value, (hull,) * len(value))


def _decode_view(view: View, codec: str, plus: bool) -> View:
    pieces: list[str] = []
    spans: list[Span] = []
    cursor = 0
    for match in _POSITIONAL.finditer(view.text):
        pieces.append(view.text[cursor : match.start()])
        spans.extend(view.spans[cursor : match.start()])
        piece = _decoded_piece(
            match[0], view.spans[match.start() : match.end()], codec, plus
        )
        pieces.append(piece.text)
        spans.extend(piece.spans)
        cursor = match.end()
    if not pieces:
        return view
    pieces.append(view.text[cursor:])
    spans.extend(view.spans[cursor:])
    return View("".join(pieces), tuple(spans))


def decoded_views(text: str) -> Iterator[View]:
    """Decode a deduplicated frontier through at most four positional stages."""
    pending: list[View] = []
    seen: set[View] = set()
    for initial in _with_ansi_variant(_normalized_view(text)):
        yield initial
        seen.add(initial)
        pending.append(initial)
    for _ in range(DECODE_PASSES):
        following: list[View] = []
        for view in pending:
            for decoded in _next_views(view):
                if decoded not in seen:
                    yield decoded
                    seen.add(decoded)
                    following.append(decoded)
        if not following:
            break
        pending = following


def _next_views(view: View) -> Iterator[View]:
    for codec in ("utf-8", "latin-1"):
        for plus in (False, True):
            yield from _with_ansi_variant(_decode_view(view, codec, plus))


def _with_ansi_variant(view: View) -> Iterator[View]:
    """The view as spelled, then without ANSI sequences when it has any.

    The stripped view is an addition, never a replacement: an OSC body or the
    final byte of a bare CSI introducer can hold the secret itself. A dropped
    sequence sits inside the hull of any match that spans it, so replacing
    the hull removes the escape codes along with the secret.
    """
    yield view
    stripped = _without_ansi(view)
    if stripped != view:
        yield stripped


def _without_ansi(view: View) -> View:
    """Drop escape sequences, including any a decoding stage revealed."""
    if _ANSI_INTRODUCERS.isdisjoint(view.text):
        return view
    pieces: list[str] = []
    spans: list[Span] = []
    cursor = 0
    for match in _ANSI_SEQUENCE.finditer(view.text):
        pieces.append(view.text[cursor : match.start()])
        spans.extend(view.spans[cursor : match.start()])
        cursor = match.end()
    pieces.append(view.text[cursor:])
    spans.extend(view.spans[cursor:])
    return View("".join(pieces), tuple(spans))


def _sextet_pattern(bits: str, alphabet: str) -> str:
    if "?" not in bits:
        return re.escape(alphabet[int(bits, 2)])
    choices = [
        character
        for number, character in enumerate(alphabet)
        if all(
            bit == "?" or bit == actual
            for bit, actual in zip(bits, f"{number:06b}", strict=True)
        )
    ]
    if len(choices) == 1:
        return re.escape(choices[0])
    return "[" + re.escape("".join(choices)) + "]"


def _base64_pattern(raw: bytes, offset: int, alphabet: str) -> str:
    bits = "?" * (offset * 8 % 6) + "".join(f"{byte:08b}" for byte in raw)
    bits += "?" * (-len(bits) % 6)
    body = r"\s*".join(
        _sextet_pattern(bits[index : index + 6], alphabet)
        for index in range(0, len(bits), 6)
    )
    padding = (-(offset + len(raw))) % 3
    return body + (rf"(?:\s*=){{0,{padding}}}" if padding else "")


def make_form(text: str, *, short: bool | None = None) -> Form:
    text = _normalized_view(text).text
    short = len(text) < MIN_SUBSTRING_CHARS if short is None else short
    literal = re.escape(text)
    compact = [character for character in text if not character.isspace()]
    if not short and len(compact) >= MIN_JOINED_CHARS:
        # Wrapped output splits a long secret; a short one never joins. The
        # form's own whitespace is dropped and each gap is possessive: a
        # literal space between two optional runs backtracked polynomially.
        literal = r"\s*+".join(map(re.escape, compact))
    if short:
        literal = rf"(?<![A-Za-z0-9]){literal}(?![A-Za-z0-9])"
    raw = text.encode("utf-8", errors="surrogatepass")
    blocks = [re.compile(re.escape(raw.hex()), re.IGNORECASE)]
    blocks.extend(
        re.compile(_base64_pattern(raw, offset, alphabet))
        for alphabet in _ALPHABETS
        for offset in range(3)
    )
    return Form(text, short, re.compile(literal), tuple(blocks))


def source_forms(value: str) -> list[Form]:
    stages = list(dict.fromkeys(view.text for view in decoded_views(value)))
    short = min(map(len, stages)) < MIN_SUBSTRING_CHARS
    return [make_form(stage, short=short) for stage in stages if stage]


def _whole_block_matches(text: str, form: Form) -> Iterator[Span]:
    raw = form.text.encode("utf-8", errors="surrogatepass")
    for match in _HEX_RUN.finditer(text):
        if len(match[0]) == len(raw) * 2 and match[0].lower() == raw.hex():
            yield match.span()
    for run, alphabet in zip(_BASE64_RUNS, _ALPHABETS, strict=True):
        for match in run.finditer(text):
            compact = "".join(match[0].split())
            if len(compact.rstrip("=")) != (len(raw) * 8 + 5) // 6:
                continue
            try:
                decoded = base64.b64decode(
                    compact + "=" * (-len(compact) % 4),
                    altchars=alphabet[-2:].encode(),
                    validate=True,
                )
            except (ValueError, binascii.Error):
                continue
            if decoded == raw:
                yield match.span()


def _overlapping_spans(pattern: re.Pattern[str], text: str) -> Iterator[Span]:
    cursor = 0
    while match := pattern.search(text, cursor):
        yield match.span()
        cursor = match.start() + 1


def _form_spans(text: str, form: Form) -> Iterator[Span]:
    if not form.text:
        return
    yield from _overlapping_spans(form.literal, text)
    if form.short:
        yield from _whole_block_matches(text, form)
    else:
        for pattern in form.blocks:
            yield from _overlapping_spans(pattern, text)


def match_hulls(text: str, forms: Sequence[tuple[str, Form]]) -> list[Hull]:
    """Union matches across all stages and map each back to its original hull."""
    if not forms:
        return []
    hulls: list[Hull] = []
    for view in decoded_views(text):
        for name, form in forms:
            for start, end in _form_spans(view.text, form):
                hulls.append(Hull(view.spans[start][0], view.spans[end - 1][1], name))
    return _merged_hulls(hulls, len(text))


def _merged_hulls(hulls: list[Hull], size: int) -> list[Hull]:
    if not hulls:
        return []
    starts: dict[int, Hull] = {}
    for hull in hulls:
        previous = starts.get(hull.start, hull)
        starts[hull.start] = _union_hulls(previous, hull)
    merged: list[Hull] = []
    for start in range(size):
        next_hull = starts.get(start)
        if next_hull is None:
            continue
        if merged and next_hull.start <= merged[-1].end:
            next_hull = _union_hulls(merged.pop(), next_hull)
        merged.append(next_hull)
    return merged


def _union_hulls(first: Hull, second: Hull) -> Hull:
    name = first.name if first.name == second.name else None
    return Hull(first.start, max(first.end, second.end), name)


def contains(text: str, forms: Sequence[tuple[str, Form]]) -> bool:
    return bool(match_hulls(text, forms))


def residual_marker(forms: Sequence[tuple[str, Form]]) -> str:
    used = set("".join(form.text for _, form in forms)) | _BLOCK_CHARACTERS
    codepoint = ord("█")
    while not _safe_residual(chr(codepoint), used):
        codepoint += 1
    return chr(codepoint)


def _safe_residual(character: str, used: set[str]) -> bool:
    return (
        character not in used
        and unicodedata.normalize("NFKC", character) == character
        and unicodedata.category(character) not in {"Cf", "Cs"}
        and not character.isspace()
    )


def replacement_marker(
    name: str | None, markers: Mapping[str, str], residual: str
) -> str:
    return residual if name is None else markers[name]


def replace_hulls(
    text: str, hulls: Sequence[Hull], markers: Mapping[str, str], residual: str
) -> str:
    """Assemble right to left once; keep every character outside the hulls."""
    pieces: list[str] = []
    cursor = len(text)
    for hull in reversed(hulls):
        pieces.append(text[hull.end : cursor])
        pieces.append(replacement_marker(hull.name, markers, residual))
        cursor = hull.start
    pieces.append(text[:cursor])
    return "".join(reversed(pieces))


def _residual_cleanup(
    text: str, forms: Sequence[tuple[str, Form]], residual: str
) -> str | None:
    """Each pass removes at least one non-residual source character."""
    hulls = match_hulls(text, forms)
    while hulls:
        before = len(text.replace(residual, ""))
        # Bypass the ordinary marker callback: residual substitution is final.
        pieces: list[str] = []
        cursor = 0
        for hull in hulls:
            pieces.extend((text[cursor : hull.start], residual))
            cursor = hull.end
        pieces.append(text[cursor:])
        text = "".join(pieces)
        if len(text.replace(residual, "")) >= before:
            return None
        hulls = match_hulls(text, forms)
    return text


def redact(
    text: str,
    forms: Sequence[tuple[str, Form]],
    markers: Mapping[str, str],
    *,
    surface: str = "retained text",
    limit: int = 65_536,
) -> str:
    residual = residual_marker(forms)
    for _ in range(4):
        hulls = match_hulls(text, forms)
        if not hulls:
            break
        text = replace_hulls(text, hulls, markers, residual)
    # This independent output check also protects against replacement faults.
    checked = _residual_cleanup(text, forms, residual)
    if checked is not None and len(checked) > limit:
        # Truncation can expose a short boundary. Residual cleanup never grows it.
        checked = _residual_cleanup(checked[:limit], forms, residual)
    if checked is None:
        logging.getLogger(__name__).error(
            "Redaction refused retained field: %s", surface
        )
        return REFUSAL_MARKER
    return checked
