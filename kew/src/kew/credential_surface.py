"""Credential and proxy redaction forms, with no dependency on the sandbox.

Every surface that shows or retains agent text needs to recognize the shapes a
credential can take: the host's own secrets, a proxy URL's userinfo and its
percent- and base64-encoded forms, and the keys that name them. That
recognition needs nothing from the launch stack, so it lives here rather than
in :mod:`kew.sandbox`: the progress display redacts every line it prints, and
importing the sandbox to do it broke the layered dispatch-stack import contract
(governed review round 6 of PR #1941, 2026-09-22).
"""

from __future__ import annotations

import json
import os
import re
import threading
import unicodedata
from collections.abc import Iterator, Mapping, Sequence, Set
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, fields, is_dataclass
from urllib.parse import SplitResult, unquote, unquote_plus, urlsplit

from kew import credential_redaction

_CREDENTIAL_NAME_RE = re.compile(
    r"TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|API_KEY|_KEY$|^KEY_|AUTH", re.IGNORECASE
)


_REDACT_MIN_CHARS = 6


_REDACT_PERCENT_DECODE_PASSES = 4


MAX_REDACTED_FIELD_CHARS = 65_536


_LAUNCH_PROXY_VALUES: ContextVar[Mapping[str, str] | None] = ContextVar(
    "launch_proxy_values", default=None
)


_CREDENTIAL_MARKERS: ContextVar[Mapping[str, str] | None] = ContextVar(
    "credential_markers", default=None
)


#: Minted run keys live only in a child's launch environment, never in this
#: process's, so the host-environment scan cannot see them (re-audit F7,
#: 2026-09-22). Counted, because retries and nested scopes may register one
#: secret twice; module-level, because stream readers may run on threads.
_ISSUED_SECRETS: dict[str, int] = {}
_ISSUED_SECRETS_LOCK = threading.Lock()
_ISSUED_LABEL = "OPENROUTER_API_KEY"


#: Key-shape backstop for an OpenRouter key kew never registered.
_OPENROUTER_KEY_RE = re.compile(r"sk-or-v1-[0-9A-Za-z]{16,}")
_OPENROUTER_KEY_MARKER = "<OPENROUTER_API_KEY redacted>"
#: The same backstop for an Anthropic key or OAuth token absent from this
#: process's environment, such as one quoted in reviewer text (#1920).
_ANTHROPIC_KEY_RE = re.compile(r"sk-ant-[0-9A-Za-z_-]{16,}")
_ANTHROPIC_KEY_MARKER = "<ANTHROPIC_KEY redacted>"


@contextmanager
def issued_secret_redaction(secret: str) -> Iterator[None]:
    """Redact one issued secret from every surface while its owner holds it."""
    with _ISSUED_SECRETS_LOCK:
        _ISSUED_SECRETS[secret] = _ISSUED_SECRETS.get(secret, 0) + 1
    try:
        yield
    finally:
        with _ISSUED_SECRETS_LOCK:
            remaining = _ISSUED_SECRETS.pop(secret, 1) - 1
            if remaining > 0:
                _ISSUED_SECRETS[secret] = remaining


def _issued_secrets() -> tuple[str, ...]:
    with _ISSUED_SECRETS_LOCK:
        return tuple(sorted(_ISSUED_SECRETS))


_NO_PROXY_HOST_RE = re.compile(
    r"(?:\*|\*?\.?[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*(?::[0-9]+)?"
    r"|[0-9a-fA-F:.\[\]]+(?:/[0-9]+)?)"
)


def _credential_shaped_proxy(name: str, value: str) -> bool:
    """Exclude bare host lists and URLs without userinfo, path, query or fragment."""
    if name.lower() == "no_proxy":
        return not all(
            _NO_PROXY_HOST_RE.fullmatch(host.strip()) for host in value.split(",")
        )
    try:
        parts, _ = _split_proxy(value)
    except ValueError:
        return True
    return bool(
        parts.username is not None
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
    )


def _split_proxy(value: str) -> tuple[SplitResult, bool]:
    """Parse a proxy value, with or without a scheme, into URL parts.

    ``user:secret@proxy:3128`` is valid for common clients, and ``urlsplit``
    reads its ``user`` as a scheme and reports no userinfo at all; parsing
    it as an authority (``//`` prefix) is what makes the credential visible.
    Returns the parts and whether the value carried a scheme. Raises
    ``ValueError`` when the value cannot be parsed or names no host.
    """

    has_scheme = "://" in value
    try:
        parts = urlsplit(value if has_scheme else f"//{value}")
        _ = parts.port  # a bad port raises here, not later inside Docker
    except ValueError:
        # Redaction itself calls this parser. Keep its error value-free.
        raise ValueError("proxy URL could not be parsed") from None
    if not parts.hostname:
        raise ValueError("proxy URL names no host")
    return parts, has_scheme


#: Every proxy build argument Docker fills in from ``config.json`` ``proxies``.
_PROXY_BUILD_ARGS = ("HTTP_PROXY", "HTTPS_PROXY", "FTP_PROXY", "ALL_PROXY", "NO_PROXY")


_PROXY_BUILD_NAMES = tuple(
    name for key in _PROXY_BUILD_ARGS for name in (key, key.lower())
)


def _percent_decoded_stages(value: str) -> list[str]:
    """Retain every spelling through four decoding stages, stopping when stable."""

    forms = [value]
    pending = [value]
    for _ in range(_REDACT_PERCENT_DECODE_PASSES):
        decoded = dict.fromkeys(
            form for text in pending for form in (unquote(text), unquote_plus(text))
        )
        pending = [form for form in decoded if form not in forms]
        if not pending:
            break
        forms.extend(pending)
    return forms


def _normalized_proxy_value(parts: SplitResult, has_scheme: bool) -> str:
    """Canonicalize scheme, host and port without discarding proxy userinfo."""
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    userinfo, separator, _ = parts.netloc.rpartition("@")
    normalized = parts._replace(netloc=f"{userinfo}{separator}{host}").geturl()
    return normalized if has_scheme else normalized.removeprefix("//")


def _proxy_credential_forms(
    value: str, *, components: bool = True, inherited: bool = False
) -> list[credential_redaction.Form]:
    """Encode complete proxy values, all userinfo, and selected URL components."""
    forms = credential_redaction.source_forms(value) if value else []
    try:
        parts, has_scheme = _split_proxy(value)
    except ValueError:
        # An unparseable URL retains complete-value protection.
        return forms
    parsed = parts.geturl()
    forms.extend(
        credential_redaction.source_forms(
            parsed if has_scheme else parsed.removeprefix("//")
        )
    )
    forms.extend(
        credential_redaction.source_forms(_normalized_proxy_value(parts, has_scheme))
    )
    if components:
        for component in _proxy_url_components(parts, inherited=inherited):
            forms.extend(credential_redaction.source_forms(component))
    if parts.username is not None:
        userinfo = parts.netloc.rpartition("@")[0]
        for credential in (userinfo, parts.username, parts.password):
            if credential:
                forms.extend(credential_redaction.source_forms(credential))
    return forms


def _named_proxy_credential_forms(
    name: str, value: str, *, components: bool = True, inherited: bool = False
) -> list[credential_redaction.Form]:
    """Use the variable's grammar while preserving complete-value protection."""
    if name.lower() != "no_proxy":
        return _proxy_credential_forms(
            value, components=components, inherited=inherited
        )
    forms = credential_redaction.source_forms(value) if value else []
    if components:
        forms.extend(_no_proxy_component_forms(value, inherited=inherited))
    return forms


def _no_proxy_component_forms(
    value: str, *, inherited: bool
) -> list[credential_redaction.Form]:
    """Split commas before decoding; exclude hosts at each bounded stage."""
    forms: list[credential_redaction.Form] = []
    for entry in value.split(","):
        for stage in _percent_decoded_stages(entry.strip()):
            for component in _no_proxy_entry_components(stage, inherited=inherited):
                forms.append(
                    credential_redaction.make_form(
                        component, short=_short_credential_form(component)
                    )
                )
    return forms


def _no_proxy_entry_components(entry: str, *, inherited: bool) -> list[str]:
    """Select assignment components or a bare non-host using existing tiers."""
    if _NO_PROXY_HOST_RE.fullmatch(entry):
        return []
    if "=" in entry:
        return _named_proxy_components([entry], inherited=inherited)
    return [entry] if _proxy_component_selected(entry, inherited) else []


def _proxy_component_selected(
    component: str, inherited: bool, *, credential: bool = False
) -> bool:
    """Select inherited components, except short provenance-only ASCII numbers.

    Credential keys retain numeric values. Decode before selecting components.
    Forms shorter than six characters match only at ASCII alphanumeric boundaries.
    Short forms inside longer alphanumeric tokens remain unredacted.
    These substrings do not identify credentials.
    Substring replacement destroys unrelated evidence.
    """
    decoded = min(_percent_decoded_stages(component), key=len)
    if len(decoded) >= _REDACT_MIN_CHARS:
        return True
    numeric = decoded.isascii() and decoded.isdigit()
    return bool(decoded) and inherited and (credential or not numeric)


def _inherited_proxy_name(name: str, inherited: bool) -> bool:
    """Credential labels remain metadata; other inherited names are secret."""
    return (
        inherited
        and _proxy_component_selected(name, inherited)
        and not _proxy_credential_key(name)
    )


def _proxy_credential_key(name: str) -> bool:
    """Recognize credential labels at any bounded percent-decoding stage."""
    return any(
        is_opaque_credential_key(stage) for stage in _percent_decoded_stages(name)
    )


def _named_proxy_components(
    components: list[str], *, adjacent_labels: bool = False, inherited: bool = False
) -> list[str]:
    """Select named secrets, or every inherited value and noncredential name."""
    selected: list[str] = []
    previous = ""
    for component in components:
        name, separator, value = component.partition("=")
        if adjacent_labels and not separator:
            name, value = previous, component
        credential = _proxy_credential_key(name)
        if _proxy_component_selected(value, inherited, credential=credential) and (
            inherited or credential
        ):
            selected.append(value)
        if _inherited_proxy_name(name, inherited):
            selected.append(name)
        previous = component
    return selected


def _proxy_url_components(parts: SplitResult, *, inherited: bool = False) -> list[str]:
    """Split delimiters before decoding, including assignments in fragments."""
    path = parts.path.split("/")
    segments = [
        part
        for part in path
        if inherited and _proxy_component_selected(part, inherited)
    ]
    fragments = (
        [parts.fragment] if _proxy_component_selected(parts.fragment, inherited) else []
    )
    return [
        *segments,
        *_named_proxy_components(path, adjacent_labels=True, inherited=inherited),
        *_named_proxy_components(parts.query.split("&"), inherited=inherited),
        *_named_proxy_components(parts.fragment.split("&"), inherited=inherited),
        *fragments,
    ]


def _credential_forms_inputs() -> tuple[object, ...]:
    """Exactly the values the forms are derived from, for cache identity."""
    received = _LAUNCH_PROXY_VALUES.get()
    return (
        tuple(sorted(received.items())) if received is not None else None,
        tuple(
            sorted(
                (name, value)
                for name, value in os.environ.items()
                if name in _PROXY_BUILD_NAMES or _CREDENTIAL_NAME_RE.search(name)
            )
        ),
        _issued_secrets(),
    )


def _build_credential_forms() -> list[tuple[str, credential_redaction.Form]]:
    found: list[tuple[str, credential_redaction.Form]] = []
    received = _LAUNCH_PROXY_VALUES.get()
    if received is not None:
        for name, value in received.items():
            found.extend(
                (name, form)
                for form in _named_proxy_credential_forms(name, value, inherited=True)
            )
    for name, value in os.environ.items():
        forms = _host_credential_forms(name, value)
        found.extend((name, form) for form in forms)
    for secret in _issued_secrets():
        found.extend(
            (_ISSUED_LABEL, form) for form in credential_redaction.source_forms(secret)
        )
    return found


def _credential_forms() -> list[tuple[str, credential_redaction.Form]]:
    """Read launch proxies and current host secrets without persisting encodings.

    Rebuilding and recompiling every form on every call cost tens of
    milliseconds per redaction, which this branch made a per-stream-line cost
    inside the read loop (governed review round 4, 2026-09-21). The forms are
    cached against the exact values they derive from, so a changed credential
    or launch proxy rebuilds them and an unchanged one does not. The cache
    holds no value the process does not already hold in its environment.
    """
    global _CREDENTIAL_FORMS_CACHE

    inputs = _credential_forms_inputs()
    cached = _CREDENTIAL_FORMS_CACHE
    if cached is not None and cached[0] == inputs:
        return list(cached[1])
    forms = _build_credential_forms()
    _CREDENTIAL_FORMS_CACHE = (inputs, forms)
    return list(forms)


#: The last ``(inputs, forms)`` pair :func:`_credential_forms` produced.
_CREDENTIAL_FORMS_CACHE: (
    tuple[tuple[object, ...], list[tuple[str, credential_redaction.Form]]] | None
) = None


def _host_credential_forms(name: str, value: str) -> list[credential_redaction.Form]:
    if name in _PROXY_BUILD_NAMES:
        if not value or not _credential_shaped_proxy(name, value):
            return []
        received = _LAUNCH_PROXY_VALUES.get()
        return _named_proxy_credential_forms(name, value, components=received is None)
    if _CREDENTIAL_NAME_RE.search(name) and len(value) >= _REDACT_MIN_CHARS:
        return credential_redaction.source_forms(value)
    return []


# Compatibility examples and repository credential variable names.
# The classifier uses normalized words, not this finite example vocabulary.
OPAQUE_CREDENTIAL_KEYS = frozenset(
    {
        "authorization",
        "x-api-key",
        "api-key",
        "apikey",
        "api_key",
        "x-auth-token",
        "auth-token",
        "access-token",
        "access_token",
        "secret",
        "client-secret",
        "client_secret",
        "password",
        "passwd",
        "token",
        "bearer",
        "private-key",
        "private_key",
        "session-token",
        "cookie",
        "set-cookie",
        "OPENROUTER_API_KEY",
        "OPENROUTER_PROVISIONING_KEY",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "OPENAI_API_KEY",
        "CODEX_ACCESS_TOKEN",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "LEMONADE_API_KEY",
        "LINEAR_API_KEY",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SECURITY_TOKEN",
        "AWS_SESSION_TOKEN",
        "GH_TOKEN",
        "GH_ENTERPRISE_TOKEN",
        "GITHUB_TOKEN",
        "GITHUB_ENTERPRISE_TOKEN",
        "NPM_TOKEN",
        "PYPI_TOKEN",
        "KEW_SERVE_TOKEN",
        "KEW_DETACHED_PUBLICATION_TOKEN",
    }
)


OPAQUE_CREDENTIAL_STEMS = frozenset(
    {
        "auth",
        "authorization",
        "token",
        "secret",
        "password",
        "passwd",
        "passphrase",
        "credential",
        "cookie",
        "bearer",
    }
)


OPAQUE_CREDENTIAL_KEY_QUALIFIERS = frozenset(
    {"api", "access", "private", "signing", "session", "client"}
)


# Keep this contract together and bind it to docs/security.md in documentation tests.
OPAQUE_CREDENTIAL_DIGIT_SUFFIX = r"\d+$"


OPAQUE_CREDENTIAL_PLURAL_SUFFIXES = (("ies", "y"), ("es", ""), ("s", ""))


OPAQUE_CREDENTIAL_METRIC_WORDS = frozenset(
    {
        "total",
        "count",
        "used",
        "usage",
        "input",
        "output",
        "read",
        "write",
        "cache",
        "cached",
        "creation",
        "max",
        "min",
        "limit",
        "budget",
        "per",
        "rate",
        "remaining",
        "estimated",
        "planned",
        "cost",
    }
)


_REPOSITORY_CREDENTIAL_KEYS = frozenset(
    name.lower() for name in OPAQUE_CREDENTIAL_KEYS if name.isupper()
)


_KEY_ESCAPE = r"\\+(?:u[0-9a-fA-F]{4}|U[0-9a-fA-F]{8}|x[0-9a-fA-F]{2}|N\{[^{}\r\n]+\})"


_SERIALIZED_KEY_ESCAPE_RE = re.compile(_KEY_ESCAPE)


_BARE_KEY_WORD = r"(?:(?!:[ \t\r\n]|:$)[^\s\"'{}\[\],=])"


# A bare key is at most five space-joined words. An unbounded run made the
# scan rescan the rest of the line from every offset, so separator-free prose
# cost quadratic time (governed review round 2, 2026-09-21).
_OPAQUE_FIELD_RE = re.compile(
    r"(?<![\w\\?&/:-])(?:(?P<bytes>[bB]?)(?P<key_quote>[\"'])"
    r"(?P<key>(?:\\[^\r\n]|(?!(?P=key_quote))[^\\\r\n])+)(?P=key_quote)"
    rf"|(?P<bare>{_BARE_KEY_WORD}+(?:[ \t]+{_BARE_KEY_WORD}+){{0,4}}?))"
    r"(?P<separator>[ \t\r\n]*[:=][ \t]*)"
)


_OPAQUE_QUOTED_VALUE_RE = re.compile(
    r"[bB]?(?P<quote>[\"'])(?:\\[\s\S]|(?!(?P=quote))[^\\])*"
    r"(?P=quote)(?=[ \t]*[,}\]\r\n]|[ \t]*$)"
)


_OPAQUE_PLAIN_VALUE_RE = re.compile(r"[^\r\n]*(?:(?:\r\n?|\n)[ \t]+[^\r\n]*)*")


_SCALAR_TEXT_RE = re.compile(
    r"(?:[^\[\]{}\"']|\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*')+"
)


def _decode_serialized_key(key: str) -> str:
    """Decode one layer of numeric or named escapes without decoding values."""
    return _SERIALIZED_KEY_ESCAPE_RE.sub(_decoded_key_escape, key)


def _decoded_key_escape(match: re.Match[str]) -> str:
    escape = match[0].lstrip("\\")
    if escape.startswith("N{"):
        try:
            return unicodedata.lookup(escape[2:-1])
        except KeyError:
            return match[0]
    codepoint = int(escape[1:], 16)
    return chr(codepoint) if codepoint <= 0x10FFFF else match[0]


def _credential_key_words(key: str) -> list[str]:
    """Split camelCase and acronym boundaries before lowercasing and delimiting."""
    decoded = _decode_serialized_key(key)
    words = re.findall(r"[^\W_]+", decoded.lower())
    decoded = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", decoded)
    decoded = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", decoded)
    return words + re.findall(r"[^\W_]+", decoded.lower())


def _credential_word_forms(word: str) -> set[str]:
    """Strip one digit run, then test unchanged spelling and one plural suffix."""
    word = re.sub(OPAQUE_CREDENTIAL_DIGIT_SUFFIX, "", word)
    return {word} | {
        word[: -len(suffix)] + replacement
        for suffix, replacement in OPAQUE_CREDENTIAL_PLURAL_SUFFIXES
        if word.endswith(suffix)
    }


def _opaque_key_words(key: object) -> list[str]:
    """Decode supported key types before applying the shared word rules."""
    if isinstance(key, bytes):
        if not key.isascii():
            return []
        key = key.decode("ascii")
    return _credential_key_words(key) if isinstance(key, str) else []


def is_opaque_credential_key(key: object) -> bool:
    """Match credential words independently of the field's value."""
    return _matches_credential_words(_opaque_key_words(key))


def _matches_credential_words(words: list[str]) -> bool:
    """Recognize credential stems and repository names without metric exemptions."""
    stems = {stem for word in words for stem in _credential_word_forms(word)}
    if stems & OPAQUE_CREDENTIAL_STEMS:
        return True
    if "key" in stems and stems & OPAQUE_CREDENTIAL_KEY_QUALIFIERS:
        return True
    # Retain the historical joined spelling and repository-specific env names.
    normalized = "_" + "_".join(words) + "_"
    return "apikey" in stems or any(
        f"_{name}_" in normalized for name in _REPOSITORY_CREDENTIAL_KEYS
    )


def is_opaque_credential_field(key: object, value: object) -> bool:
    """Preserve primitive measurements; contain credential strings and collections."""
    return (
        value is not None
        and not isinstance(value, (bool, int, float))
        and is_opaque_credential_key(key)
        and not _numeric_metric_value(key, value)
    )


# Token scales use k/m; resource sizes use g and the doctor image-size units.
_METRIC_NUMBER_RE = re.compile(
    r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?"
    r"(?:[ \t]*(?:[kmg]|B|kB|KB|KiB|MB|MiB|GB|GiB|TB|TiB))?"
)


def _numeric_metric_value(key: object, value: object) -> bool:
    """Exempt complete numeric strings only when a metric word qualifies the key."""
    if not isinstance(value, str) or _METRIC_NUMBER_RE.fullmatch(value) is None:
        return False
    return any(
        _credential_word_forms(word) & OPAQUE_CREDENTIAL_METRIC_WORDS
        for word in _opaque_key_words(key)
    )


def _inline_value_is_open(inline: str) -> bool:
    """Whether an inline value continues on the lines below it.

    It does when it opens a flow collection, or a quoted scalar it never
    closes. A quote only opens a scalar where a value can start: at the head of
    the value or just inside a flow collection. Treating every quote as an
    opener made an ordinary prose apostrophe swallow the rest of a retained
    report (governed review round 5, 2026-09-22).
    """
    state = _InlineScan()
    for char in inline:
        state.read(char)
    return bool(state.quote) or state.depth > 0


@dataclass
class _InlineScan:
    """One pass over an inline value, tracking what it leaves open."""

    depth: int = 0
    quote: str = ""
    escaped: bool = False
    value_start: bool = True

    def read(self, char: str) -> None:
        if self.quote:
            self._read_quoted(char)
            return
        if char in "\"'" and self.value_start:
            self.quote = char
        elif char in "[{":
            self.depth += 1
        elif char in "]}":
            self.depth = max(0, self.depth - 1)
        # A quote can open a scalar at the head of the value, after a space, or
        # just inside a flow collection. One inside a word is an apostrophe.
        self.value_start = char.isspace() or char in "[{,"

    def _read_quoted(self, char: str) -> None:
        if self.escaped:
            self.escaped = False
        elif char == "\\":
            self.escaped = True
        elif char == self.quote:
            self.quote = ""


def _opaque_block_end(text: str, field: re.Match[str]) -> int:
    """End an uncertain value at the next line of equal or lesser indentation.

    A key whose value starts on its own line, or whose inline value opens a
    flow collection or quote it does not close, owns the more-indented block
    that follows and every same-indent sequence item, up to the next key at
    its own indentation or less. A key with a closed inline value owns only
    the more-indented lines below it and a same-indent sequence item that
    carries no key of its own; the next other line at equal or lesser
    indentation is a sibling, whatever it contains. Without that distinction
    a markdown bullet such as "- Tokens: 45,120 across 3 turns" consumed every
    later section of a dispatch report (governed review round 1, 2026-09-21).
    """
    line_start = text.rfind("\n", 0, field.start()) + 1
    prefix = text[line_start : field.start()]
    indent = len(prefix) - len(prefix.lstrip(" \t"))
    line_end = text.find("\n", field.end())
    inline = text[field.end() : line_end if line_end >= 0 else len(text)].strip()
    closed_inline = bool(inline) and not _inline_value_is_open(inline)
    # A key written inside a sequence item is a peer of the items around it, so
    # it never owns them. A plain mapping key still can: `token: true` followed
    # by `- secret` is one uncertain value, while `- password: x` followed by
    # `- We ran the tests` is two bullets (governed review round 7, 2026-09-22).
    # Read the line, not the text before the key: a bare key can begin at
    # the bullet marker itself, leaving no prefix to inspect.
    key_line = text[line_start : line_end if line_end >= 0 else len(text)]
    in_sequence_item = key_line.lstrip(" \t").startswith("- ")
    for line in re.finditer(r"(?m)^[ \t]*(?=\S)", text[field.end() :]):
        start = field.end() + line.start()
        if start <= field.end() or len(line[0]) > indent:
            continue
        if _opaque_block_ends_at(
            text, field.end() + line.end(), closed_inline, in_sequence_item
        ):
            return start
    return len(text)


def _opaque_block_ends_at(
    text: str, at: int, closed_inline: bool, in_sequence_item: bool = False
) -> bool:
    """Decide whether the line starting at the given offset (already at the
    key's indentation or less) is a sibling that ends the opaque block."""
    if closed_inline:
        item = re.match(r"-[ \t]+", text[at:])
        if item is None or in_sequence_item:
            return True
        return _OPAQUE_FIELD_RE.match(text, at + item.end()) is not None
    candidate = _OPAQUE_FIELD_RE.match(text, at)
    return candidate is not None and re.match(r"-\s", text[at:]) is None


def _plain_scalar_within_indent(text: str, field: re.Match[str], raw: str) -> str:
    """Keep only the continuation lines a plain scalar can own.

    A plain scalar continues on lines indented deeper than its own key. Taking
    every following line with any leading whitespace let one field swallow its
    same-indent siblings and delete them from a retained report (governed
    review round 3, 2026-09-21).
    """
    line_start = text.rfind("\n", 0, field.start()) + 1
    prefix = text[line_start : field.start()]
    indent = len(prefix) - len(prefix.lstrip(" \t"))
    head, *rest = raw.split("\n")
    kept = [head]
    for line in rest:
        stripped = line.lstrip(" \t")
        if not stripped or len(line) - len(stripped) <= indent:
            break
        kept.append(line)
    return "\n".join(kept)


def _opaque_scalar_span(text: str, field: re.Match[str]) -> tuple[int, str] | None:
    """Recognize quoted or plain scalar syntax without interpreting its contents."""
    start = field.end()
    scalar_start = start + len(text[start:]) - len(text[start:].lstrip())
    quoted = _OPAQUE_QUOTED_VALUE_RE.match(text, scalar_start)
    if quoted:
        return quoted.end(), quoted["quote"]
    value = _OPAQUE_PLAIN_VALUE_RE.match(text, start)
    assert value is not None
    raw = _plain_scalar_within_indent(text, field, value[0])
    # YAML collection, tag, anchor, alias, and block indicators cannot prove a scalar.
    if raw and raw[0] not in "-?:,[]{}#&*!|>'\"%@`" and _SCALAR_TEXT_RE.fullmatch(raw):
        return start + len(raw), ""
    return None


def _scalar_continues_as_block(text: str, end: int) -> bool:
    """A following sequence or non-key comma item invalidates a scalar boundary."""
    rest = text[end:]
    if rest.startswith(","):
        return (
            _OPAQUE_FIELD_RE.match(
                text, end + 1 + len(rest[1:]) - len(rest[1:].lstrip())
            )
            is None
        )
    return re.match(r"(?:[ \t]*\r?\n)+[ \t]*(?:-[ \t]|[\[\]{|>])", rest) is not None


def _opaque_value_span(text: str, field: re.Match[str]) -> tuple[int, str]:
    """Keep proven scalars bounded; replace every uncertain value as one block."""
    if not field["key_quote"] and field.start() > 0:
        wrapper = _OPAQUE_QUOTED_VALUE_RE.match(text, field.start() - 1)
        if wrapper:
            return wrapper.end() - 1, ""
    scalar = _opaque_scalar_span(text, field)
    if scalar and not _scalar_continues_as_block(text, scalar[0]):
        return scalar
    return _opaque_block_end(text, field), ""


def _bare_url_key(field: re.Match[str], key: str) -> bool:
    """URL query assignments remain under proxy-value matching, not header parsing."""
    return bool(field["bare"] and ("://" in key or "/?" in key))


def _public_plain_field(text: str, field: re.Match[str], key: str) -> bool:
    """Keep unrelated keys and proven primitive or numeric metric values."""
    return (
        _bare_url_key(field, key)
        or not is_opaque_credential_key(key)
        or _plain_primitive_value(text, field)
        or _plain_numeric_metric(text, field)
    )


def _redact_plain_opaque_fields(text: str) -> str:
    """Classify each field before consuming its complete, undecoded value."""
    if ":" not in text and "=" not in text:
        return text  # no field separator, so no field to classify
    parts: list[str] = []
    cursor = 0
    for field in _OPAQUE_FIELD_RE.finditer(text):
        if field.start() < cursor:
            continue
        key = field["key"] or field["bare"]
        if _public_plain_field(text, field, key):
            continue
        end, quote = _opaque_value_span(text, field)
        key_quote = field["key_quote"] or ""
        prefix = (
            f"{key_quote}{_decode_serialized_key(key)}{key_quote}{field['separator']}"
        )
        replacement = f"{prefix}{quote}<redacted>{quote}"
        if end < len(text) and text[end - 1 : end] == "\n":
            replacement += "\n"
        parts.extend((text[cursor : field.start()], replacement))
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts)


_JSON_TOKEN_RE = re.compile(
    r'"(?:\\[\s\S]|[^"\\])*"|true|false|null'
    r"|-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?"
)


_JSON_SEPARATOR_RE = re.compile(r"\s*:\s*")


_PLAIN_PRIMITIVE_RE = re.compile(
    r"(?:true|false|null|True|False|None"
    r"|-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)"
    r"(?=[ \t]*(?:[,}\]\r\n]|$))"
)


def _plain_primitive_value(text: str, field: re.Match[str]) -> bool:
    """Exempt only a complete primitive token with no opaque block continuation."""
    primitive = _PLAIN_PRIMITIVE_RE.match(text, field.end())
    if primitive is None or _scalar_continues_as_block(text, primitive.end()):
        return False
    return re.match(r"[ \t]*\r?\n[ \t]+\S", text[primitive.end() :]) is None


def _plain_numeric_metric(text: str, field: re.Match[str]) -> bool:
    """Validate the complete scalar before granting a text metric exemption."""
    end, quote = _opaque_value_span(text, field)
    value = text[field.end() : end].strip()
    if quote:
        value = value[1:-1]
    return _numeric_metric_value(field["key"] or field["bare"], value)


def _redact_json_token(text: str, match: re.Match[str]) -> tuple[str, int]:
    """Redact a decoded JSON string or the complete value of an opaque key."""
    if not match[0].startswith('"'):
        sanitized = redact_credential_values(match[0])
        return _retained_json_token(match[0], sanitized, match[0]), match.end()
    value = json.loads(match[0])
    separator = _JSON_SEPARATOR_RE.match(text, match.end())
    if separator and is_opaque_credential_key(value):
        item, end = json.JSONDecoder().raw_decode(text, separator.end())
        if is_opaque_credential_field(value, item):
            key = _retained_json_token(match[0], redact_credential_values(value), value)
            marker = json.dumps(redact_credential_values("<redacted>"))
            return f"{key}{separator[0]}{marker}", end
    sanitized = redact_credentials(value)
    # Preserve original escapes in unchanged values; key normalization must not
    # rewrite unrelated value spellings. Encode changed strings exactly once.
    return _retained_json_token(match[0], sanitized, value), match.end()


def _retained_json_token(original: str, sanitized: str, value: str) -> str:
    """Quote replaced primitives while preserving unchanged token spellings."""
    return json.dumps(sanitized) if sanitized != value else original


def _redact_plain_authorization(text: str) -> str:
    """Apply header containment to plain text, including malformed JSON.

    Known values are matched on the text as written first: a field value is
    replaced only to its line end, which would strand the wrapped second half
    of a known secret on the next line. The recheck after the field pass
    covers a value that pass reshaped; text it left alone is already checked.
    """
    known = redact_credential_values(text)
    fields_redacted = _redact_plain_opaque_fields(known)
    if fields_redacted == known:
        return known
    return redact_credential_values(fields_redacted)


def _redact_json_credentials(text: str) -> str:
    """Keep final JSON valid when header text contains escaped line boundaries."""
    if not text.lstrip().startswith(("{", "[")):
        return _redact_plain_authorization(text)
    try:
        json.loads(text)
    except (ValueError, RecursionError):
        # Malformed JSON still crosses the complete header-containment boundary.
        return _redact_plain_authorization(text)
    parts: list[str] = []
    cursor = 0
    for match in _JSON_TOKEN_RE.finditer(text):
        if match.start() < cursor:
            continue
        replacement, end = _redact_json_token(text, match)
        parts.extend((text[cursor : match.start()], replacement))
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts)


def redact_credentials(text: str) -> str:
    """Redact opaque credential fields and current host credentials in free text.

    Call after protocol parsing and before truncating displayed or retained text.
    Never persist credential values, encodings or matching prefixes.
    """
    return _redact_json_credentials(str(text))


def redact_credential_values(text: str, *, surface: str = "retained text") -> str:
    """Recheck host secrets after normalization of already header-redacted text.

    Headers must be removed before their line boundaries are flattened.
    This recheck does not reinterpret joined diagnostic prose as a header value.
    """
    forms = list(dict.fromkeys(_credential_forms()))
    markers = _select_credential_markers(forms, _CREDENTIAL_MARKERS.get())
    text = _OPENROUTER_KEY_RE.sub(_OPENROUTER_KEY_MARKER, text)
    text = _ANTHROPIC_KEY_RE.sub(_ANTHROPIC_KEY_MARKER, text)
    return credential_redaction.redact(
        text, forms, markers, surface=surface, limit=MAX_REDACTED_FIELD_CHARS
    )


def _select_credential_markers(
    credentials: list[tuple[str, credential_redaction.Form]],
    previous: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Use a per-name marker only when none of that name's forms matches it."""
    by_name: dict[str, list[tuple[str, credential_redaction.Form]]] = {}
    for name, form in credentials:
        by_name.setdefault(name, []).append((name, form))
    local = credential_redaction.residual_marker(credentials)
    markers = dict(previous or {})
    for name, forms in by_name.items():
        marker = markers.get(name, f"<{name} redacted>")
        markers[name] = (
            local if credential_redaction.contains(marker, forms) else marker
        )
    return markers


def _short_credential_form(secret: str) -> bool:
    """Measure all bounded decoding stages before applying the matching tier."""
    return (
        min(len(view.text) for view in credential_redaction.decoded_views(secret))
        < _REDACT_MIN_CHARS
    )


def runner_text(value: object) -> str:
    """Redact one extracted free-text field before display, truncation or retention.

    Never pass an event, protocol key, event type or raw stream line here.
    """
    from kew.credential_surface import redact_credentials

    return redact_credentials(str(_redact_text_leaves(value)))


def _authorization_value(key: object, value: object) -> object:
    """Contain credential strings and collections while retaining primitives."""
    from kew.credential_surface import is_opaque_credential_field

    if is_opaque_credential_field(key, value):
        return "<redacted>"
    return value


def _dataclass_values(value: object) -> object:
    """Read dataclass fields without deepcopy or invoking the object's repr."""
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: getattr(value, field.name) for field in fields(value)}
    return value


def _redact_text_leaves(value: object) -> object:
    """Copy an extracted display value, redacting strings before repr escapes them."""
    from kew.credential_surface import redact_credentials

    if isinstance(value, str):
        return redact_credentials(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        # Decode before repr can escape a credential inside a failed buffer.
        return redact_credentials(bytes(value).decode("utf-8", errors="replace"))
    if isinstance(value, BaseException):
        return exception_text(value)
    value = _dataclass_values(value)
    if isinstance(value, Mapping):
        return {
            _redact_key(key): _redact_text_leaves(_authorization_value(key, item))
            for key, item in value.items()
        }
    if isinstance(value, (Sequence, Set)):
        return _redact_items(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_credentials(str(value))


def _redact_key(key: object) -> object:
    """Keep transformed dataclasses usable as mapping keys."""
    sanitized = _redact_text_leaves(key)
    try:
        hash(sanitized)
    except TypeError:
        return runner_text(sanitized)
    return sanitized


def _redact_items(value: Sequence[object] | Set[object]) -> object:
    """Preserve hashable containers used as nested exception dictionary keys."""
    items = [_redact_text_leaves(item) for item in value]
    if isinstance(value, (tuple, Set)):
        return tuple(items)
    return items


def exception_text(exc: BaseException) -> str:
    """Sanitize nested args before Python quotes them; never format raw args.

    Use the base formatter, avoiding custom formatters that can expose raw
    attributes. Do not mutate the exception used for classification or retries.
    """
    from kew.credential_surface import redact_credentials

    args = tuple(_redact_text_leaves(arg) for arg in exc.args)
    return redact_credentials(str(Exception(*args)) or type(exc).__name__)
