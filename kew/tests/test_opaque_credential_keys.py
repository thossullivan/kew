"""Which names are opaque credential keys, and what each retained surface keeps."""

import json
import re
from pathlib import Path

import pytest
from support.retained_surfaces import SURFACES as _SURFACES
from support.retained_surfaces import retained as _retained

from kew import sandbox
from kew.runner_fields import redact_authorization_values, runner_text


@pytest.fixture(autouse=True)
def _clean_proxies(monkeypatch):
    for name in sandbox._PROXY_BUILD_NAMES:
        monkeypatch.delenv(name, raising=False)


_KEYS = (
    "x-api-key",
    "api_key",
    "X-Auth-Token",
    "cookie",
    "set-cookie",
    "ANTHROPIC_API_KEY",
    "OPENROUTER_PROVISIONING_KEY",
    r"\N{LATIN CAPITAL LETTER A}uthorization",
    r"\x58-\N{LATIN CAPITAL LETTER A}\u0075\U00000074h-Token",
)


@pytest.mark.parametrize("surface", _SURFACES)
@pytest.mark.parametrize("key", _KEYS)
@pytest.mark.parametrize("spelling", ["quoted", "bytes", "json"])
def test_opaque_keys_cross_all_boundaries(key, spelling, surface, tmp_path, capsys):
    if spelling == "json":
        header = json.dumps({key: "Opaque1933", "keep": "public"})
    elif spelling == "bytes":
        header = f'b"{key}": b"Opaque1933"'
    else:
        header = f'"{key}" = "Opaque1933"'
    retained = _retained(
        f"## Dispatch Report\n{header}\nDone.", surface, tmp_path, capsys
    )
    assert "Opaque1933" not in retained
    assert "<redacted>" in retained
    assert "Done." in retained


@pytest.mark.parametrize("surface", ["display", "exception", "evidence"])
@pytest.mark.parametrize("key", _KEYS)
@pytest.mark.parametrize("bytes_key", [False, True])
def test_structured_opaque_values_are_replaced_whole(
    key, bytes_key, surface, tmp_path, capsys
):
    key = key.encode() if bytes_key else key
    value = {key: ["Opaque1933", {"nested": r"\N{UNDECODED}\xXX"}], "keep": "public"}
    retained = _retained(value, surface, tmp_path, capsys)
    assert "Opaque1933" not in retained
    assert "UNDECODED" not in retained
    assert "<redacted>" in retained
    assert "public" in retained
    assert value[key][0] == "Opaque1933"


@pytest.mark.parametrize("surface", _SURFACES)
def test_noncredential_word_substrings_remain_readable(surface, tmp_path, capsys):
    text = '## Dispatch Report\n{"tokens_total": 1933, "tokenizer": "public"}\nDone.'
    retained = _retained(text, surface, tmp_path, capsys)
    assert "1933" in retained
    assert "public" in retained
    assert "redacted" not in retained


@pytest.mark.parametrize("key", ["x-api-key", "api_key", "cookie", "Authorization"])
@pytest.mark.parametrize(
    "value", [False, 1933, ["Opaque1933"], {"nested": "Opaque1933"}]
)
def test_json_opaque_values_are_not_decoded(key, value):
    text = json.dumps({key: value, "tokens_total": 1933})
    retained = json.loads(sandbox.redact_credentials(text))
    expected = value if isinstance(value, (bool, int)) else "<redacted>"
    assert retained == {key: expected, "tokens_total": 1933}


def test_classifier_uses_exact_or_delimited_names():
    classify = sandbox.is_authorization_key
    for key in getattr(sandbox, "OPAQUE_CREDENTIAL_KEYS", _KEYS):
        assert classify(key.swapcase())
        assert classify(f"prefix-{key}-suffix")
        assert classify(f"prefix_{key}_suffix".encode())


@pytest.mark.parametrize(
    "key",
    [
        "tokenizer",
        "mytoken",
        "passwordless",
        "X-Trace-Key",
        r"\N{NOT A UNICODE NAME}uthorization",
        r"\UFFFFFFFFuthorization",
        b"\xffAuthorization",
        1933,
    ],
)
def test_classifier_rejects_noncredential_keys(key):
    assert not sandbox.is_authorization_key(key)


def test_key_decoder_preserves_unrelated_values():
    text = r"""{"keep":"\\N{LATIN CAPITAL LETTER A}\\x42\\u0043\\U00000044"}"""
    assert sandbox.redact_credentials(text) == text
    invalid = r'''"\N{NOT A UNICODE NAME}uthorization" = "public"'''
    assert sandbox.redact_credentials(invalid) == invalid


def test_fixed_evidence_vocabulary_remains_exempt(monkeypatch):
    from kew.runner_fields import redact_authorization_values, reported_identity

    value = {
        "error_category": "transient",
        "runner": "claude-code",
        "subtype": "success",
    }
    monkeypatch.setenv("HTTP_PROXY", "https://transient@proxy.invalid/?token=success")
    retained = {key: reported_identity(item, key) for key, item in value.items()}
    assert retained == value  # Fixed kinds still survive identity admission.
    assert redact_authorization_values(retained) == {
        **value,
        "error_category": "<HTTP_PROXY redacted>",
        "subtype": "<HTTP_PROXY redacted>",
    }
    assert "transient" not in runner_text("transient")


def test_security_docs_match_shared_classifier():
    text = (Path(__file__).parents[2] / "docs/security.md").read_text()
    assert "<!-- opaque-credential-keys:start -->" in text
    section = text.split("<!-- opaque-credential-keys:start -->")[1].split(
        "<!-- opaque-credential-keys:end -->"
    )[0]
    import re

    assert set(re.findall(r"`([\w-]+)`", section)) == sandbox.OPAQUE_CREDENTIAL_KEYS
    assert "Exact names or hyphen/underscore-delimited tokens match." in text
    assert "`tokens_total` remains readable." in text


@pytest.mark.parametrize("surface", _SURFACES)
@pytest.mark.parametrize(
    "key",
    [
        "credential",
        "passphrase",
        "signing-key",
        "apiSecret",
        "auth.token",
        "x.authorization",
        "provider/private/key",
        "session key",
        "client:key",
        "providerSigningKey",
        "APISecret",
        "credential!value",
        "x auth context",
    ],
)
@pytest.mark.parametrize("spelling", ["json", "quoted", "bare"])
def test_normalized_keys_cross_all_boundaries(key, spelling, surface, tmp_path, capsys):
    header = {
        "json": json.dumps({key: "Opaque1933"}),
        "quoted": f'"{key}" = "Opaque1933"',
        "bare": f"{key}: Opaque1933",
    }[spelling]
    retained = _retained(
        f"## Dispatch Report\n{header}\nDone.", surface, tmp_path, capsys
    )
    assert "Opaque1933" not in retained
    assert "Done." in retained


@pytest.mark.parametrize("delimiter", ["-", "_", ".", ":", "/", " ", "!", "@", "☃"])
def test_stem_normalization_uses_all_nonalphanumeric_delimiters(delimiter):
    for stem in (
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
    ):
        assert sandbox.is_authorization_key(
            f"provider{delimiter}{stem}{delimiter}value"
        )
    for qualifier in ("api", "access", "private", "signing", "session", "client"):
        assert sandbox.is_authorization_key(f"{qualifier}{delimiter}key")


@pytest.mark.parametrize("surface", ["display", "exception", "evidence"])
@pytest.mark.parametrize(
    "key", ["credential", "passphrase", "apiSecret", "signing/key"]
)
@pytest.mark.parametrize("as_bytes", [False, True])
def test_structured_keys_replace_whole_values(surface, key, as_bytes, tmp_path, capsys):
    key = key.encode() if as_bytes else key
    value = {key: ["Opaque1933", {"child": "Private1933"}], "status": "public"}
    retained = _retained(value, surface, tmp_path, capsys)
    assert "Opaque1933" not in retained
    assert "Private1933" not in retained
    assert "public" in retained
    assert value[key][0] == "Opaque1933"


@pytest.mark.parametrize("surface", _SURFACES)
def test_noncredential_words_remain_readable(surface, tmp_path, capsys):
    value = {"tokens_total": 1933, "keyboard": "public", "status": "passed"}
    retained = _retained(
        f"## Dispatch Report\n{json.dumps(value)}\nDone.", surface, tmp_path, capsys
    )
    assert "1933" in retained
    assert "public" in retained
    assert "passed" in retained
    assert "redacted" not in retained
    for key in (
        "keyboard",
        "apiKeyboard",
        "status",
        "key",
        "tokenizer",
    ):
        assert not sandbox.is_authorization_key(key)


_BLOCKS = (
    "\n- public\n- Bearer Opaque1933",
    "\n- public\n-\tpart: Bearer Opaque1933",
    "\n  - public\n  - Bearer Opaque1933",
    "[public,\nBearer Opaque1933\n]",
    "|\n  public\n  Bearer Opaque1933",
    ">+\n  public\n  Bearer Opaque1933",
    '"public\nBearer Opaque1933',
    "'public\nBearer Opaque1933",
    "\n  nested:\n    - public\n    - Bearer Opaque1933",
    "!custom\n- public\n- Bearer Opaque1933",
    'public "unbalanced\nBearer Opaque1933',
    "public\n- Bearer Opaque1933",
    '"public"\n- Bearer Opaque1933',
    '"public"\n\n- Bearer Opaque1933',
    '"public", "Bearer Opaque1933"',
)


@pytest.mark.parametrize("surface", _SURFACES)
@pytest.mark.parametrize("value", _BLOCKS)
def test_serialized_blocks_cross_all_boundaries(value, surface, tmp_path, capsys):
    text = f"## Dispatch Report\nAuthorization: {value}\nstatus: available\nDone."
    retained = _retained(text, surface, tmp_path, capsys)
    assert "Opaque1933" not in retained
    assert "public" not in retained
    assert "available" in retained
    assert "Done." in retained


@pytest.mark.parametrize("indent", ["", "  ", "\t"])
@pytest.mark.parametrize("ending", ["", "\nstatus: available"])
def test_opaque_block_stops_only_at_peer_or_parent_key(indent, ending):
    text = (
        f"{indent}Authorization:\n{indent}- public\n"
        f"{indent}  nested: Opaque1933\n{indent}- Private1933{ending}"
    )
    retained = sandbox.redact_credentials(text)
    assert "Opaque1933" not in retained
    assert "Private1933" not in retained
    assert "public" not in retained
    assert ("available" in retained) == bool(ending)


def test_security_docs_bind_stems_qualifiers_and_components():
    text = (Path(__file__).parents[2] / "docs/security.md").read_text()
    assert hasattr(sandbox, "OPAQUE_CREDENTIAL_STEMS")
    for label, expected in (
        ("stems", sandbox.OPAQUE_CREDENTIAL_STEMS),
        ("key-qualifiers", sandbox.OPAQUE_CREDENTIAL_KEY_QUALIFIERS),
    ):
        section = text.split(f"<!-- opaque-credential-{label}:start -->")[1].split(
            f"<!-- opaque-credential-{label}:end -->"
        )[0]
        assert set(re.findall(r"`([a-z]+)`", section)) == expected
    assert "except short provenance-only ASCII numbers" in text
    assert (
        "Short forms require ASCII alphanumeric boundaries on any stage, "
        "using the union of stage matches." in text
    )
    assert "next key at the same or lower indentation" in text
    assert "Username-only userinfo receives the same protection." in text


@pytest.mark.parametrize("surface", _SURFACES)
@pytest.mark.parametrize(
    "payload,secret",
    [
        ({"credentials": "Bearer Credential1933"}, "Credential1933"),
        ({"passwords": ["a", "b"]}, '["a", "b"]'),
        ({"token2": "Token1933"}, "Token1933"),
        ({"secrets": {"nested": "Secret1933"}}, "Secret1933"),
    ],
)
def test_inflected_fields_cross_all_surfaces(
    tmp_path, capsys, surface, payload, secret
):
    retained = _retained(
        f"## Dispatch Report\n{json.dumps(payload)}\nstatus: available\nDone.",
        surface,
        tmp_path,
        capsys,
    )
    assert secret not in retained.replace('\\"', '"')
    assert "redacted" in retained
    assert "available" in retained
    assert json.loads(sandbox.redact_credentials(json.dumps(payload))) == dict.fromkeys(
        payload, "<redacted>"
    )


@pytest.mark.parametrize(
    "key", ["credentials", "passwords", "token2", "secret_1", "password01"]
)
@pytest.mark.parametrize("surface", ["display", "exception", "evidence"])
def test_structured_inflected_keys_replace_whole_values(tmp_path, capsys, key, surface):
    value = {key: ["Private1933", {"nested": "Opaque1933"}], "status": "available"}
    retained = _retained(value, surface, tmp_path, capsys)
    assert "Private1933" not in retained
    assert "Opaque1933" not in retained
    assert "available" in retained


@pytest.mark.parametrize("surface", _SURFACES)
@pytest.mark.parametrize(
    "payload",
    [
        {"tokens_total": 12, "input_tokens": 3, "output_tokens": "9"},
        {"usage": {"cache_read_tokens": "7", "cache_creation_tokens": 4}},
        *(
            {key: "available"}
            for key in (
                "tokenizer",
                "tokenize",
                "keyboard",
                "status",
                "author",
                "authentic",
                "secretary",
            )
        ),
    ],
)
def test_usage_and_noncredential_words_remain_readable(
    tmp_path, capsys, surface, payload
):
    text = json.dumps(payload)
    retained = _retained(
        f"## Dispatch Report\n{text}\nDone.", surface, tmp_path, capsys
    )
    assert "redacted" not in retained
    assert text in retained.replace('\\"', '"')
    assert redact_authorization_values(payload) == payload


@pytest.mark.parametrize("value", [12, -1.5, True, False, None])
@pytest.mark.parametrize("surface", _SURFACES)
def test_primitive_credential_values_remain_readable(tmp_path, capsys, surface, value):
    payload = {"token": value, "password": value, "secret": value}
    text = json.dumps(payload)
    retained = _retained(
        f"## Dispatch Report\n{text}\nDone.", surface, tmp_path, capsys
    )
    assert "redacted" not in retained
    assert text in retained.replace('\\"', '"')
    assert sandbox.redact_credentials(text) == text
    assert redact_authorization_values(payload) == payload


@pytest.mark.parametrize(
    "key",
    [
        "tokens",
        "cookies",
        "passphrases",
        "authorizations",
        "secretes",
        "tokens2",
        "token٢",
        "secrets９",
    ],
)
def test_inflected_whole_words_are_credential_keys(key):
    assert sandbox.is_opaque_credential_key(key)
    assert sandbox.is_opaque_credential_key(f"{key}_usage")
    assert sandbox.is_opaque_credential_key(f"{key}_counts")


@pytest.mark.parametrize(
    "metric",
    [
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
    ],
)
def test_metric_words_preserve_numeric_values(metric):
    assert sandbox.is_opaque_credential_key(f"secret_{metric}")
    assert sandbox.is_opaque_credential_key(f"{metric}_token2")
    assert sandbox.redact_credentials(
        json.dumps({f"secret_{metric}": "9"})
    ) == json.dumps({f"secret_{metric}": "9"})


@pytest.mark.parametrize(
    "value",
    ['"12"', '"true"', '"null"', "12\n  nested: Private1933", "true\n- Private1933"],
)
def test_only_complete_primitives_are_exempt(value):
    retained = sandbox.redact_credentials(f"token: {value}\nstatus: available")
    assert "Private1933" not in retained
    assert "redacted" in retained
    assert "available" in retained
