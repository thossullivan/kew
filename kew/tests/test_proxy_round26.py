"""Round 26: positional redaction and rejection of proxy argv values."""

import base64
import json
import textwrap
import zlib
from functools import partial
from unittest.mock import Mock

import pytest
from test_proxy_round20 import _container_launch
from test_proxy_round21 import SURFACES, _retain

from kew import sandbox
from kew.runner_fields import redact_authorization_values

MARKER = "<HTTP_PROXY redacted>"
REPORT = "## Dispatch Report\nerror_category: transient\nexit code 7\n"


@pytest.fixture(autouse=True)
def _clean_proxies(monkeypatch):
    for name in sandbox._PROXY_BUILD_NAMES:
        monkeypatch.delenv(name, raising=False)


def _environment(value):
    return {"HTTP_PROXY": f"http://proxy.invalid/?token={value}"}


@pytest.mark.parametrize("surface", SURFACES)
def test_round26_decoded_boundary_on_every_retained_surface(
    tmp_path, capsys, caplog, surface
):
    report = REPORT + "echo: %3Aabcde\nDone."
    with sandbox.proxy_redaction_scope("inherit", environment=_environment("abcde")):
        retained = _retain(report, surface, tmp_path, capsys, caplog)
    # Baseline retains abcde because the raw left neighbor is the escape's A.
    assert retained == report.replace("abcde", MARKER)


def test_round26_raw_boundary_union_allows_five_character_overredaction():
    """Control: a boundary at stage zero permits the documented local replacement."""
    with sandbox.proxy_redaction_scope("inherit", environment=_environment("abcde")):
        assert sandbox.redact_credential_values("abcde%58") == f"{MARKER}%58"


def test_round26_windows_escape_exposes_short_boundary():
    with sandbox.proxy_redaction_scope("inherit", environment=_environment("abcde")):
        assert sandbox.redact_credential_values(r"C:\Users\nabcde") == (
            "C:\\Users\\n" + MARKER
        )


def test_round26_hex_odd_parity():
    """Control A1: hexadecimal patterns find a long form at odd nibble parity."""
    spelling = "f" + b"abcdef".hex() + "0"
    with sandbox.proxy_redaction_scope("inherit", environment=_environment("abcdef")):
        assert sandbox.redact_credential_values(spelling) == f"f{MARKER}0"


@pytest.mark.parametrize("prefix", ["container: ", "sha256:"])
def test_round26_short_hex_preserves_complete_identifier(prefix):
    """A2: a short form cannot consume part of a larger hexadecimal run."""
    identifier = "0123456789" + b"abcde".hex() + "f" * 44
    assert len(identifier) == 64
    with sandbox.proxy_redaction_scope("inherit", environment=_environment("abcde")):
        assert (
            sandbox.redact_credential_values(prefix + identifier) == prefix + identifier
        )


@pytest.mark.parametrize("offset", [1, 2])
@pytest.mark.parametrize("urlsafe", [False, True])
@pytest.mark.parametrize("padding", [False, True])
def test_round26_base64_alignment_and_mime_wrap(offset, urlsafe, padding):
    value = "http://proxy.invalid/" + "opaque1933/" * 8 + "?region=café-1"
    raw = b"!" * offset + value.encode() + b"?"
    encode = base64.urlsafe_b64encode if urlsafe else base64.b64encode
    spelling = encode(raw).decode()
    if not padding:
        spelling = spelling.rstrip("=")
    wrapped = "\r\n".join(textwrap.wrap(spelling, 76))
    with sandbox.proxy_redaction_scope("inherit", environment={"HTTP_PROXY": value}):
        retained = sandbox.redact_credential_values("echo: " + wrapped + "; exit 7")
    # Baseline finds some aligned components but leaves most of the encoded URL.
    assert retained.count(MARKER) == 1
    assert len(retained) < len("echo: ; exit 7") + len(MARKER) + 8
    assert retained.startswith("echo: ") and retained.endswith("; exit 7")


@pytest.mark.parametrize("spelling", ["caf%E9-1", "caf%e9-1", "caf%25E9-1"])
def test_round26_latin1_percent_bytes(spelling):
    with sandbox.proxy_redaction_scope("inherit", environment=_environment("café-1")):
        assert sandbox.redact_credential_values(f"echo: {spelling}; exit 7") == (
            f"echo: {MARKER}; exit 7"
        )


@pytest.mark.parametrize(
    ("value", "spelling"),
    [
        ("abcdef", r"\141\142\143\144\145\146"),
        ("abcdef", r"\N{LATIN SMALL LETTER A}bcdef"),
        ("abcdef", r"\x61b\u0063de\U00000066"),
        ("abc😀ef", r"abc\ud83d\ude00ef"),
        ("abcdef", r"\\x61\\x62\\x63\\x64\\x65\\x66"),
        ("abcdef", "ab\\\ncdef"),
        ("abcdef", "ab\\\r\ncdef"),
        ("abcdef", "ab\\\rcdef"),
        ("abcdef", "ab\u200bcd\u2060ef"),
        ("abcdef", "ａｂｃｄｅｆ"),
    ],
    ids=[
        "octal",
        "named",
        "mixed",
        "surrogate-pair",
        "double-escape",
        "lf",
        "crlf",
        "cr",
        "format",
        "nfkc",
    ],
)
def test_round26_positional_escape_grammar_and_normalization(value, spelling):
    with sandbox.proxy_redaction_scope("inherit", environment=_environment(value)):
        assert sandbox.redact_credential_values(f"before {spelling} after") == (
            f"before {MARKER} after"
        )


def test_round26_overlapping_variables_merge_original_hulls():
    environment = {
        "HTTP_PROXY": "http://proxy.invalid/?token=abcdef",
        "HTTPS_PROXY": "http://proxy.invalid/?token=cdefsuffix",
    }
    with sandbox.proxy_redaction_scope("inherit", environment=environment):
        retained = sandbox.redact_credential_values("before ab%63defsuffix after")
        assert retained == "before █ after"
        assert sandbox.redact_credential_values(retained) == retained


def test_round26_overlapping_occurrences_share_one_hull():
    """Collect overlapping occurrences, including their final character."""
    with sandbox.proxy_redaction_scope(
        "inherit", environment=_environment("abcdefabcdef")
    ):
        assert sandbox.redact_credential_values("abcdefabcdefabcdef") == MARKER


def _forced_markers(*args, spelling, **kwargs):
    return {"HTTP_PROXY": spelling}


def test_round26_output_selfcheck_clears_colliding_decoded_markers(monkeypatch):
    monkeypatch.setattr(
        sandbox,
        "_select_credential_markers",
        partial(_forced_markers, spelling="ａｂｃｄｅｆ"),
    )
    with sandbox.proxy_redaction_scope("inherit", environment=_environment("abcdef")):
        retained = sandbox.redact_credential_values("before abcdef after")
    # Baseline's raw-pattern recheck misses the emitted fullwidth form.
    assert retained == "before █ after"
    assert "<retained field refused>" not in retained


@pytest.mark.parametrize("surrounding", ["%3{}", "123abc{}def456", "YW{}Jj"])
def test_round26_residual_cannot_complete_an_escape(monkeypatch, surrounding):
    """Control A9: a forced residual character contributes no coding alphabet bytes."""
    monkeypatch.setattr(
        sandbox,
        "_select_credential_markers",
        partial(_forced_markers, spelling="redacted"),
    )
    with sandbox.proxy_redaction_scope("inherit", environment=_environment("redacted")):
        retained = sandbox.redact_credential_values(surrounding.format("redacted"))
    assert retained == surrounding.format("█")
    forbidden = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz%+\\=/_-"
    assert "█" not in forbidden


def test_round26_structured_leaf_preserves_evidence_and_input():
    payload = {"detail": "before %3Aabcde after", "exit_code": 7}
    with sandbox.proxy_redaction_scope("inherit", environment=_environment("abcde")):
        retained = redact_authorization_values(payload)
    assert retained == {"detail": f"before %3A{MARKER} after", "exit_code": 7}
    assert payload["detail"] == "before %3Aabcde after"


def test_round26_production_container_launch_persists_sanitized_report(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("HTTP_PROXY", _environment("abcde")["HTTP_PROXY"])
    _container_launch(tmp_path, monkeypatch, "exit code 7\necho: %3Aabcde")
    [path] = tmp_path.glob("*.evidence.json")
    assert json.loads(path.read_text())["report"] == (
        f"## Dispatch Report\nexit code 7\necho: %3A{MARKER}\nDone."
    )


@pytest.mark.parametrize("name", sandbox._PROXY_BUILD_NAMES)
@pytest.mark.parametrize("policy", ["none", "inherit"])
def test_round26_proxy_mapping_rejected_before_argv(
    monkeypatch, tmp_path, name, policy
):
    construct = Mock(return_value=[])
    monkeypatch.setattr(sandbox, "_container_identity_args", construct)
    fake = "fake-round26-proxy-value"
    with pytest.raises(
        ValueError, match="proxy environment assignments are not permitted"
    ) as caught:
        sandbox.build_run_argv(
            runtime="docker",
            image="fake:image",
            checkout_dir=tmp_path,
            slug="round26",
            network="none",
            runner="opencode",
            credential_environment={name: fake},
            proxy_environment=policy,
        )
    assert fake not in str(caught.value)
    assert name not in str(caught.value)
    construct.assert_not_called()


@pytest.mark.parametrize(
    "spelling",
    [
        "".join(f"%{byte:02X}" for byte in "abcdef".encode("utf-16-le")),
        "=61=62=63=64=65=66",
        "&#97;&#98;&#99;&#100;&#101;&#102;",
        base64.b64encode(zlib.compress(b"abcdef")).decode(),
    ],
    ids=["utf16", "quoted-printable", "html", "compression"],
)
def test_round26_documented_non_goals_remain_unchanged(spelling):
    """Control: these families remain outside the closed decoding contract."""
    with sandbox.proxy_redaction_scope("inherit", environment=_environment("abcdef")):
        assert sandbox.redact_credential_values(spelling) == spelling


def test_round26_short_base64_requires_complete_decoded_run():
    spelling = base64.b64encode(b"abcd\0").decode()
    with sandbox.proxy_redaction_scope("inherit", environment=_environment("abcd")):
        assert sandbox.redact_credential_values(spelling) == spelling
        assert (
            sandbox.redact_credential_values(base64.b64encode(b"abcd").decode())
            == MARKER
        )


def test_round26_truncation_selfcheck_clears_new_short_boundary():
    limit = sandbox.MAX_REDACTED_FIELD_CHARS
    text = ":" * (limit - 5) + "abcdeX"
    with sandbox.proxy_redaction_scope("inherit", environment=_environment("abcde")):
        retained = sandbox.redact_credential_values(text)
    assert retained == ":" * (limit - 5) + "█"


def test_round26_short_base64_preserves_longer_maximal_run():
    spelling = base64.b64encode(b"abcd").decode() + "===="
    with sandbox.proxy_redaction_scope("inherit", environment=_environment("abcd")):
        assert sandbox.redact_credential_values(spelling) == spelling


@pytest.mark.parametrize(
    ("value", "text", "expected"),
    [
        ("a" + "\u200b" * 5, "echo: a; abandon", "echo: <NO_PROXY redacted>; abandon"),
        ("ﬃﬃ", "echo: XffiffiY", "echo: X<NO_PROXY redacted>Y"),
    ],
    ids=["format-short-tier", "nfkc-long-tier"],
)
def test_round26_no_proxy_normalized_source_selects_correct_tier(value, text, expected):
    with sandbox.proxy_redaction_scope(
        "inherit", environment={"NO_PROXY": "hint=" + value}
    ):
        assert sandbox.redact_credential_values(text) == expected


@pytest.mark.parametrize("spelling", ["caf%C3%A9%25F8-1", "caf%E9%25C3%25B8-1"])
def test_round26_byte_views_compose_across_stages(spelling):
    with sandbox.proxy_redaction_scope("inherit", environment=_environment("caféø-1")):
        assert sandbox.redact_credential_values(f"before {spelling} after") == (
            f"before {MARKER} after"
        )


@pytest.mark.parametrize(
    "value",
    [
        "http://alice:placeholder-pw-1978@proxy.corp:99999",
        "http://alice:placeholder-pw-1978@proxy.corp:abc",
        "http://alice:placeholder-pw-1978@[::1",
    ],
    ids=["bad-port", "non-numeric-port", "unclosed-ipv6"],
)
def test_inherited_unparseable_proxy_refuses_container_launch(monkeypatch, value):
    """Re-audit F10: userinfo in an unparseable URL cannot be redacted."""
    for name in sandbox.CONTROLLED_PROXY_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", value)
    monkeypatch.setenv("NO_PROXY", "localhost,.corp")
    with pytest.raises(sandbox.SandboxRuntimeUnavailable) as raised:
        sandbox._container_proxy_environment_args("inherit")
    assert type(raised.value).__name__ == "SandboxProxyUnparseable"
    assert raised.value.reason == "proxy_unparseable"
    assert "HTTPS_PROXY" in str(raised.value)
    assert "placeholder-pw-1978" not in str(raised.value)
    assert "alice" not in str(raised.value)
    # "none" forwards nothing, so every controlled name stays explicitly blank.
    blank = sandbox._container_proxy_environment_args("none")
    assert blank[1::2] == [f"{name}=" for name in sandbox.CONTROLLED_PROXY_NAMES]
    monkeypatch.setenv("HTTPS_PROXY", "http://alice:placeholder-pw-1978@proxy:3128")
    inherited = sandbox._container_proxy_environment_args("inherit")
    assert "HTTPS_PROXY" in inherited[1::2]
    assert len(inherited) == 2 * len(sandbox.CONTROLLED_PROXY_NAMES)


def test_inherited_unparseable_proxy_refuses_image_build(monkeypatch):
    """PR #1984 review round 1: the build path forwards inherited proxies too."""
    for name in sandbox.CONTROLLED_PROXY_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://alice:placeholder-pw-1984@proxy:99999")
    run = Mock()
    monkeypatch.setattr(sandbox.subprocess_utils, "run", run)
    with pytest.raises(sandbox.SandboxRuntimeUnavailable) as raised:
        sandbox.build_agent_image(
            "docker", "test:image", unpinned=True, proxy_environment="inherit"
        )
    assert type(raised.value).__name__ == "SandboxProxyUnparseable"
    assert "HTTPS_PROXY" in str(raised.value)
    assert "placeholder-pw-1984" not in str(raised.value)
    assert raised.value.reason == "proxy_unparseable"
    run.assert_not_called()
    for helper in (sandbox._build_env, sandbox._proxy_build_args):
        with pytest.raises(sandbox.SandboxRuntimeUnavailable):
            helper("inherit")
        assert helper("none") is not None


@pytest.mark.parametrize("layer", ["image", "launch"])
def test_smoke_reports_an_unparseable_inherited_proxy_as_a_failed_layer(
    tmp_path, monkeypatch, layer
):
    """PR #1984 review round 3: smoke returns a SmokeResult, never a traceback."""
    from kew.config import DispatchConfig

    for name in sandbox.CONTROLLED_PROXY_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://alice:placeholder-pw-1984@proxy:99999")
    monkeypatch.setattr(sandbox, "detect_runtime", lambda config: "/mock/docker")
    monkeypatch.setattr(sandbox.subprocess_utils, "TOOL_PATHS", {})
    monkeypatch.setattr(sandbox, "resolve_runner_image", lambda config, runner: "img")
    monkeypatch.setattr(
        sandbox, "_image_exists_locally", lambda runtime, image: layer == "launch"
    )
    monkeypatch.setattr(
        sandbox, "_expected_packaged_image_pin", lambda context, runner: "img"
    )
    monkeypatch.setattr(sandbox, "packaged_agent_image_context", lambda: tmp_path)
    monkeypatch.setattr(sandbox, "_validate_packaged_image_tag", Mock())
    launch = Mock()
    monkeypatch.setattr(sandbox, "launch_container", launch)
    monkeypatch.setattr(sandbox.subprocess_utils, "run", Mock())
    config = DispatchConfig().sandbox
    config.proxy_environment = "inherit"
    result = sandbox.run_smoke(config)
    assert (result.ok, result.layer) == (False, layer)
    assert "HTTPS_PROXY does not parse as a proxy URL" in result.detail
    assert 'Set [sandbox] proxy_environment to "none", or fix HTTPS_PROXY.' in (
        result.detail
    )
    assert "placeholder-pw-1984" not in result.detail
    launch.assert_not_called()
    sandbox.subprocess_utils.run.assert_not_called()


def test_unparseable_proxy_refusal_survives_launch_sanitization(monkeypatch):
    """PR #1984 review round 3: the remediation half must not be redacted."""
    from kew.runner_fields import sanitize_exception_args

    for name in sandbox.CONTROLLED_PROXY_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://alice:placeholder-pw-1984@proxy:99999")
    with (
        pytest.raises(sandbox.SandboxProxyUnparseable) as raised,
        sandbox.proxy_redaction_scope("inherit"),
    ):
        sandbox._container_proxy_environment_args("inherit")
    expected = (
        "Sandbox: HTTPS_PROXY does not parse as a proxy URL, so kew cannot redact "
        "its credentials while inheriting host proxies. "
        'Set [sandbox] proxy_environment to "none", or fix HTTPS_PROXY.'
    )
    assert str(raised.value) == expected
    sanitize_exception_args(raised.value)
    assert str(raised.value) == expected
