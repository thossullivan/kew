"""Round 25: grammar-based encoding coverage across retained surfaces."""

import json
import random
from functools import partial
from urllib.parse import quote

import pytest
from test_docs_truthfulness import (
    PROXY_ENCODING_GRAMMAR,
    _assert_proxy_encoding_grammar,
    _read,
)
from test_proxy_round20 import _agent_launch, _container_launch
from test_proxy_round21 import SURFACES
from test_proxy_round24 import _retained_report

from kew import sandbox
from kew.runner_fields import redact_authorization_values

FORM = "s3:cr3;t"
MIXED = "s3%3Acr3%3bt"
MARKER = "<HTTP_PROXY redacted>"
CONTROLS = "## Dispatch Report\nerror_category: transient\nexit code 7\n"


@pytest.fixture(autouse=True)
def _clean_proxies(monkeypatch):
    for name in sandbox._PROXY_BUILD_NAMES:
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("surface", SURFACES)
def test_round25_mixed_percent_across_retained_surfaces(
    monkeypatch, tmp_path, capsys, caplog, surface
):
    monkeypatch.setenv("HTTP_PROXY", f"http://proxy.invalid/?token={FORM}")
    report = CONTROLS + f"proxy echoed: {MIXED}\nDone."
    with sandbox.proxy_redaction_scope("inherit"):
        retained = _retained_report(report, surface, tmp_path, capsys, caplog)
    assert retained == report.replace(MIXED, MARKER)


@pytest.mark.parametrize("spelling", ["s3%3Acr3;t", "s3:cr3%3bt"])
@pytest.mark.parametrize("boundary", ["text", "values"])
def test_round25_partial_percent_encoding(monkeypatch, spelling, boundary):
    monkeypatch.setenv("HTTP_PROXY", f"http://proxy.invalid/?token={FORM}")
    redact = (
        sandbox.redact_credentials
        if boundary == "text"
        else sandbox.redact_credential_values
    )
    with sandbox.proxy_redaction_scope("inherit"):
        assert redact(f"proxy echoed: {spelling}") == f"proxy echoed: {MARKER}"


@pytest.mark.parametrize("spelling", ["3a3B3c3D3e3F", "3A3b3C3d3E3f"])
def test_round25_hexadecimal_bytes_in_mixed_case(monkeypatch, spelling):
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid/?token=:;<=>?")
    with sandbox.proxy_redaction_scope("inherit"):
        assert sandbox.redact_credentials(f"proxy echoed: {spelling}") == (
            f"proxy echoed: {MARKER}"
        )


@pytest.mark.parametrize("depth", [2, 3, 4])
@pytest.mark.parametrize("stage", ["inner", "outer"])
def test_round25_nested_stage_mixed_percent(monkeypatch, depth, stage):
    original = quote(FORM, safe="")
    for _ in range(depth - 1):
        original = original.replace("%", "%25")
    spelling = MIXED
    if stage == "outer":
        for _ in range(depth - 1):
            spelling = spelling.replace("%", "%25")
    monkeypatch.setenv("HTTP_PROXY", f"http://proxy.invalid/?token={original}")
    with sandbox.proxy_redaction_scope("inherit"):
        assert sandbox.redact_credentials(f"proxy echoed: {spelling}") == (
            f"proxy echoed: {MARKER}"
        )


@pytest.mark.parametrize("spelling", ["a%3Ab%3bc", "a:b%3Bc", "%61:b;c"])
def test_round25_structured_short_form_containment(monkeypatch, spelling):
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid/?token=a:b;c")
    with sandbox.proxy_redaction_scope("inherit"):
        assert redact_authorization_values({"detail": spelling, "exit_code": 7}) == {
            "detail": MARKER,
            "exit_code": 7,
        }
        assert sandbox.redact_credentials(f"X{spelling}Y") == f"X{spelling}Y"


@pytest.mark.parametrize(
    "launch", [_agent_launch, _container_launch], ids=["host", "container"]
)
def test_round25_production_launch_persists_sanitized_evidence(
    monkeypatch, tmp_path, launch
):
    monkeypatch.setenv("HTTP_PROXY", f"http://proxy.invalid/?token={FORM}")
    launch(tmp_path, monkeypatch, f"exit code 7\nproxy echoed: {MIXED}")
    [path] = tmp_path.glob("*.evidence.json")
    assert MIXED not in path.read_text()
    assert json.loads(path.read_text())["report"] == (
        f"## Dispatch Report\nexit code 7\nproxy echoed: {MARKER}\nDone."
    )


def _random_percent_encoding(value, rng):
    parts = []
    for character in value:
        if rng.choice((True, False)):
            digits = f"{ord(character):02x}"
            parts.append("%" + "".join(rng.choice((d, d.upper())) for d in digits))
        else:
            parts.append(character)
    return "".join(parts)


def _colliding_marker(*args, spelling, **kwargs):
    return spelling


@pytest.mark.parametrize("boundary", ["text", "backstop"])
def test_round25_generated_percent_grammar(monkeypatch, boundary):
    rng = random.Random(193325)  # nosec B311 -- deterministic encoding test data
    spellings = [_random_percent_encoding(FORM, rng) for _ in range(50)]
    monkeypatch.setenv("HTTP_PROXY", f"http://proxy.invalid/?token={FORM}")
    with sandbox.proxy_redaction_scope("inherit"):
        for spelling in spellings:
            if boundary == "backstop":
                # Force four colliding passes before the residual replacement.
                monkeypatch.setattr(
                    sandbox.credential_redaction,
                    "replacement_marker",
                    partial(_colliding_marker, spelling=spelling),
                )
                retained = sandbox.redact_credential_values(f"proxy echoed: {FORM}")
                assert retained == "proxy echoed: █", spelling
            else:
                assert sandbox.redact_credentials(f"proxy echoed: {spelling}") == (
                    f"proxy echoed: {MARKER}"
                ), spelling


def test_round25_docs_encoding_grammar():
    _assert_proxy_encoding_grammar(_read("docs/security.md"))


@pytest.mark.parametrize("rule", PROXY_ENCODING_GRAMMAR)
def test_round25_docs_reject_missing_encoding_family(rule):
    text = _read("docs/security.md")
    _assert_proxy_encoding_grammar(text)
    with pytest.raises(AssertionError):
        _assert_proxy_encoding_grammar(text.replace(f"- {rule}\n", ""))
