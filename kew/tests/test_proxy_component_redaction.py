"""Proxy URL components and userinfo never survive a retained surface."""

import base64
from urllib.parse import quote

import pytest
from support.retained_surfaces import SURFACES as _SURFACES
from support.retained_surfaces import retained as _retained

from kew import sandbox


@pytest.fixture(autouse=True)
def _clean_proxies(monkeypatch):
    for name in sandbox._PROXY_BUILD_NAMES:
        monkeypatch.delenv(name, raising=False)


_COMPONENTS = (
    "PathSecret1933",
    "QuerySecret1933",
    "FragmentSecret1933",
    "QueryOpaque1933",
)


def _encoded_component(value, encoding):
    if encoding == "percent":
        return "".join(f"%{byte:02X}" for byte in value.encode())
    if encoding == "base64":
        return base64.b64encode(value.encode()).decode()
    return value


@pytest.mark.parametrize("surface", _SURFACES)
@pytest.mark.parametrize("component", _COMPONENTS)
@pytest.mark.parametrize("encoding", ["raw", "percent", "base64"])
def test_proxy_components_cross_all_boundaries(
    monkeypatch, tmp_path, capsys, component, encoding, surface
):
    emitted = _encoded_component(component, encoding)
    # Raw URL delimiters precede decoding; escaped delimiters stay in a component.
    proxy = (
        "https://proxy.invalid/path/PathSecret1933"
        "?token=QuerySecret1933&QueryOpaque1933=#FragmentSecret1933"
    )
    if encoding == "percent":
        proxy = proxy.replace(component, emitted)
    monkeypatch.setenv("HTTP_PROXY", proxy)
    with sandbox.proxy_redaction_scope("inherit"):
        retained = _retained(
            f"## Dispatch Report\n{emitted}\nDone.", surface, tmp_path, capsys
        )
    assert emitted not in retained
    assert component not in retained
    assert "redacted" in retained
    assert "Done." in retained


@pytest.mark.parametrize("location", ["path", "query", "fragment", "query_name"])
@pytest.mark.parametrize("encoding", ["raw", "percent", "base64"])
def test_decoded_components_keep_escaped_delimiters(monkeypatch, location, encoding):
    component = "Opaque/Part+Value1933?=#"
    encoded = quote(component, safe="")
    suffix = {
        "path": f"/proxy/{encoded}",
        "query": f"/?token={encoded}",
        "fragment": f"/#{encoded}",
        "query_name": f"/?{encoded}=",
    }[location]
    monkeypatch.setenv("HTTP_PROXY", f"https://proxy.invalid{suffix}")
    emitted = {
        "raw": component,
        "percent": encoded,
        "base64": base64.b64encode(component.encode()).decode(),
    }[encoding]
    with sandbox.proxy_redaction_scope("inherit"):
        assert emitted not in sandbox.redact_credentials(emitted)


@pytest.mark.parametrize("component", ["proxy", "x", "%70%72%6F%78%79"])
def test_short_components_do_not_shred_output(monkeypatch, component):
    monkeypatch.setenv(
        "HTTP_PROXY", f"https://proxy.invalid/{component}?token={component}#{component}"
    )
    text = "proxy diagnostics: explanation available"
    expected = "<HTTP_PROXY redacted> diagnostics: explanation available"
    with sandbox.proxy_redaction_scope("inherit"):
        assert sandbox.redact_credentials(text) == (
            text if component == "x" else expected
        )
    monkeypatch.setenv("HTTP_PROXY", "proxy")
    with sandbox.proxy_redaction_scope("inherit"):
        assert sandbox.redact_credentials(text) == expected


def test_query_labels_are_not_credentials(monkeypatch):
    monkeypatch.setenv(
        "HTTP_PROXY", "https://proxy.invalid/?password=Secret1933&region=other"
    )
    assert sandbox.redact_credentials("password region") == "password region"
    assert "Secret1933" not in sandbox.redact_credentials("Secret1933")


@pytest.mark.parametrize("surface", _SURFACES)
@pytest.mark.parametrize("policy", ["none", "inherit"])
@pytest.mark.parametrize("suffix", ["", ":"])
@pytest.mark.parametrize("encoding", ["raw", "percent", "base64"])
def test_username_only_crosses_all_boundaries(
    monkeypatch, tmp_path, capsys, surface, policy, suffix, encoding
):
    username = "BearerToken1933"
    emitted = _encoded_component(username, encoding)
    configured = emitted if encoding == "percent" else username
    monkeypatch.setenv("HTTP_PROXY", f"https://{configured}{suffix}@proxy.invalid")
    with sandbox.proxy_redaction_scope(policy):
        retained = _retained(
            f"## Dispatch Report\n{emitted}\nDone.", surface, tmp_path, capsys
        )
    assert emitted not in retained
    assert username not in retained
    assert "Done." in retained


@pytest.mark.parametrize("username", ["a", "cat", "Private/User+1933"])
@pytest.mark.parametrize("suffix", ["", ":", ":Password1933"])
def test_every_userinfo_username_has_encoding_coverage(monkeypatch, username, suffix):
    monkeypatch.setenv("HTTP_PROXY", f"https://{quote(username, safe='')}{suffix}@host")
    for index, emitted in enumerate(
        (username, quote(username, safe=""), _encoded_component(username, "base64"))
    ):
        if index == 2 and len(username) < 6:
            # Whitespace joins a longer coding run; punctuation isolates the form.
            assert (
                sandbox.redact_credentials(f"before {emitted} after")
                == f"before {emitted} after"
            )
        else:
            assert (
                sandbox.redact_credentials(f"before {emitted} after")
                == "before <HTTP_PROXY redacted> after"
            )
        assert sandbox.redact_credentials(f"before '{emitted}' after") == (
            "before '<HTTP_PROXY redacted>' after"
        )


@pytest.mark.parametrize("surface", _SURFACES)
@pytest.mark.parametrize(
    "word", ["status", "public", "report", "result", "output", "health"]
)
def test_six_character_proxy_words_preserve_reports(
    monkeypatch, word, surface, tmp_path, capsys
):
    monkeypatch.setenv(
        "HTTP_PROXY", f"https://proxy.invalid/{word}?region={word}#{word}"
    )
    text = f"## Dispatch Report\n{word} remains readable\nDone."
    with sandbox.proxy_redaction_scope("none"):
        retained = _retained(text, surface, tmp_path, capsys)
    assert f"{word} remains readable" in retained
    assert "redacted" not in retained


@pytest.mark.parametrize("policy", ["none", "inherit"])
@pytest.mark.parametrize(
    "suffix", ["/credential/aaaaaa", "/apiSecret=aaaaaa", "/?x.authorization=aaaaaa"]
)
def test_named_proxy_components_follow_policy(monkeypatch, policy, suffix):
    proxy = f"https://proxy.invalid{suffix}"
    monkeypatch.setenv("HTTP_PROXY", proxy)
    with sandbox.proxy_redaction_scope(policy):
        retained = sandbox.redact_credentials("before aaaaaa after")
        assert ("aaaaaa" in retained) == (policy == "none")
        assert "aaaaaa" not in sandbox.redact_credentials(proxy)


@pytest.mark.parametrize("policy", ["none", "inherit"])
@pytest.mark.parametrize("component", ["aB3dE6gH9", "aB3dE6gH", "Dictionary", "status"])
@pytest.mark.parametrize("location", ["path", "query", "fragment", "query_name"])
def test_unnamed_components_require_length_and_policy(
    monkeypatch, policy, component, location
):
    suffix = {
        "path": f"/{component}",
        "query": f"/?region={component}",
        "fragment": f"/#{component}",
        "query_name": f"/?{component}=",
    }[location]
    monkeypatch.setenv("HTTP_PROXY", f"https://proxy.invalid{suffix}")
    with sandbox.proxy_redaction_scope(policy):
        retained = sandbox.redact_credentials(f"before {component} after")
    assert (component not in retained) == (policy == "inherit")


def test_none_scope_restores_outer_component_rules(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "https://proxy.invalid/aB3dE6gH9")
    with sandbox.proxy_redaction_scope("inherit"):
        with sandbox.proxy_redaction_scope("none"):
            assert sandbox.redact_credentials("aB3dE6gH9") == "aB3dE6gH9"
        assert "aB3dE6gH9" not in sandbox.redact_credentials("aB3dE6gH9")


@pytest.mark.parametrize("policy", ["none", "inherit"])
def test_fragment_assignments_follow_policy(monkeypatch, policy):
    monkeypatch.setenv("HTTP_PROXY", "https://proxy.invalid/#credential=aaaaaa")
    with sandbox.proxy_redaction_scope(policy):
        retained = sandbox.redact_credentials("before aaaaaa after")
        assert ("aaaaaa" in retained) == (policy == "none")


def test_query_credential_labels_remain_metadata(monkeypatch):
    monkeypatch.setenv(
        "HTTP_PROXY",
        "https://proxy.invalid/?x.authorization=aB3dE6gH9&provider_signing_key=aaaaaa",
    )
    text = "x.authorization provider_signing_key"
    with sandbox.proxy_redaction_scope("inherit"):
        assert sandbox.redact_credentials(text) == text
        assert "aB3dE6gH9" not in sandbox.redact_credentials("aB3dE6gH9")
        assert "aaaaaa" not in sandbox.redact_credentials("aaaaaa")


@pytest.mark.parametrize("surface", _SURFACES)
@pytest.mark.parametrize("emitted", ["deadbeef", "aaaaaa", "gateway", "url"])
def test_inherited_low_entropy_components_are_redacted(
    monkeypatch, tmp_path, capsys, surface, emitted
):
    proxy = "https://proxy.invalid/gateway/deadbeef#token=aaaaaa"
    monkeypatch.setenv("HTTP_PROXY", proxy)
    emitted = proxy if emitted == "url" else emitted
    with sandbox.proxy_redaction_scope("inherit"):
        retained = _retained(
            f"## Dispatch Report\n{emitted}\nDone.", surface, tmp_path, capsys
        )
    assert emitted not in retained
    assert "redacted" in retained
    assert "Done." in retained


@pytest.mark.parametrize("surface", _SURFACES)
@pytest.mark.parametrize("assignment", ["token=aaaaaa", "abcdef=ghijkl"])
@pytest.mark.parametrize("encoded", [False, True])
def test_fragment_assignment_values_never_reach_a_retained_surface(
    monkeypatch, tmp_path, capsys, surface, assignment, encoded
):
    name, _, value = assignment.partition("=")
    encoded_value = "".join(f"%{byte:02X}" for byte in value.encode())
    configured = f"{name}={encoded_value}" if encoded else assignment
    monkeypatch.setenv("HTTP_PROXY", f"https://proxy.invalid/#{configured}")
    text = f"## Dispatch Report\n{name} {value}\nDone."
    with sandbox.proxy_redaction_scope("inherit"):
        retained = _retained(text, surface, tmp_path, capsys)
    assert value not in retained
    assert (name in retained) == (name == "token")
    assert "Done." in retained


@pytest.mark.parametrize("location", ["path", "query_value", "query_name", "fragment"])
@pytest.mark.parametrize("value", ["aaaaaa", "abcdef", "aaaaa", "a/b?c=d"])
def test_short_components_redact_by_decoded_length(monkeypatch, location, value):
    encoded = quote(value, safe="")
    suffix = {
        "path": f"/{encoded}",
        "query_value": f"?region={encoded}",
        "query_name": f"?{encoded}=",
        "fragment": f"#{encoded}",
    }[location]
    monkeypatch.setenv("HTTP_PROXY", f"https://proxy.invalid/{suffix}")
    with sandbox.proxy_redaction_scope("inherit"):
        retained = sandbox.redact_credentials(f"before {value} after")
    assert value not in retained


@pytest.mark.parametrize("surface", _SURFACES)
def test_none_preserves_path_words(monkeypatch, tmp_path, capsys, surface):
    proxy = "https://ProxyUser:ProxyPass@proxy.invalid/gateway/deadbeef#token=aaaaaa"
    monkeypatch.setenv("HTTP_PROXY", proxy)
    text = (
        f"## Dispatch Report\ngateway remains readable\n{proxy}\n"
        "ProxyUser:ProxyPass\nDone."
    )
    with sandbox.proxy_redaction_scope("none"):
        retained = _retained(text, surface, tmp_path, capsys)
    assert "gateway remains readable" in retained
    assert proxy not in retained
    assert "ProxyUser" not in retained
    assert "ProxyPass" not in retained


@pytest.mark.parametrize(
    "suffix,secret,visible",
    [
        ("/gateway/credential/aaaaaa", "aaaaaa", "gateway"),
        ("/?token=aaaaaa&abcdef=ghijkl", "aaaaaa", "ghijkl"),
        ("/#token=aaaaaa", "aaaaaa", "gateway"),
        ("/#abcdef=ghijkl", "abcdef=ghijkl", "gateway"),
    ],
)
def test_named_components_redact_outside_a_scope(monkeypatch, suffix, secret, visible):
    monkeypatch.setenv("HTTP_PROXY", f"https://proxy.invalid{suffix}")
    retained = sandbox.redact_credentials(f"before {secret} {visible} after")
    assert secret not in retained
    assert visible in retained


@pytest.mark.parametrize("suffix", ["?authorization&abcdef", "?authorization=&abcdef="])
def test_bare_query_names_keep_credential_labels(monkeypatch, suffix):
    monkeypatch.setenv("HTTP_PROXY", f"https://proxy.invalid/{suffix}")
    with sandbox.proxy_redaction_scope("inherit"):
        retained = sandbox.redact_credentials("authorization abcdef")
    assert "abcdef" not in retained
    assert "authorization" in retained


def test_fallback_report_redacts_the_proxy_and_keeps_usage(monkeypatch, tmp_path):
    from kew.dispatch_core import _fallback_report_body
    from kew.tracking import DispatchResult

    proxy = "https://proxyuser:proxypass@proxy.invalid"
    monkeypatch.setenv("HTTP_PROXY", proxy)
    result = DispatchResult(
        exit_code=0,
        issue_num=1933,
        log_file=tmp_path / "dispatch.log",
        total_tokens=12000,
        tool_uses=2,
        duration_seconds=30,
        reported_service_tier=proxy,
    )
    report = _fallback_report_body("1933", "simple", result)
    assert "12,000 tokens (2 tool uses)" in report
    assert "Duration: 30s" in report
    assert "Reported service tier:" in report
    assert "<HTTP_PROXY redacted>" in report
    assert proxy not in report
