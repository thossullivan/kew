"""Round 21: short proxy forms preserve unrelated retained evidence."""

import json
import logging

import pytest
from test_proxy_round20 import _agent_launch, _container_launch

from kew import sandbox
from kew.progress import _log
from kew.runner_fields import exception_text, redact_authorization_values, runner_text
from kew.transcript_metrics import DerivedRunEvidence, write_derived_evidence

MARKER = "<HTTP_PROXY redacted>"
REPORT = "## Dispatch Report\nAnalysis complete.\nDecision: approve"
CONTROLS = "## Dispatch Report\nexit code 1\nerror: timeout\nabandon"
SURFACES = (
    "text",
    "values",
    "structured",
    "display",
    "exception",
    "log_record",
    "evidence",
)


@pytest.fixture(autouse=True)
def _clean_proxies(monkeypatch):
    for name in sandbox._PROXY_BUILD_NAMES:
        monkeypatch.delenv(name, raising=False)


def _persisted_report(text, tmp_path):
    prefix = "" if text.startswith("## Dispatch Report") else "## Dispatch Report\n"
    evidence = DerivedRunEvidence(report=runner_text(prefix + text))
    path = write_derived_evidence(tmp_path / "dispatch.log", evidence)
    report = json.loads(path.read_text())["report"]
    assert isinstance(report, str)
    return report.removeprefix(prefix)


def _structured_leaf(text):
    controls = {
        "header": "## Dispatch Report",
        "error_category": "timeout",
        "exit_code": 1,
    }
    payload = {**controls, "nested": [{"detail": text}]}
    retained = redact_authorization_values(payload)
    assert {key: retained[key] for key in controls} == controls
    assert payload["nested"][0]["detail"] == text
    return retained["nested"][0]["detail"]


def _retain(text, surface, tmp_path, capsys, caplog):
    if surface == "text":
        return sandbox.redact_credentials(text)
    if surface == "values":
        return sandbox.redact_credential_values(text)
    if surface == "structured":
        return _structured_leaf(text)
    if surface == "display":
        _log(text)
        return capsys.readouterr().err.removeprefix("[dispatch] ").removesuffix("\n")
    if surface == "exception":
        return exception_text(ValueError(text))
    if surface == "log_record":
        logging.getLogger("kew.round21").warning("%s", runner_text(text))
        return caplog.records[-1].getMessage()
    return _persisted_report(text, tmp_path)


@pytest.mark.parametrize("surface", SURFACES)
def test_round21_one_character_preserves_report_and_replaces_only_token(
    monkeypatch, tmp_path, capsys, caplog, surface
):
    monkeypatch.setenv("HTTP_PROXY", "https://proxy.invalid/a")
    with sandbox.proxy_redaction_scope("inherit"):
        assert _retain(REPORT, surface, tmp_path, capsys, caplog) == REPORT
        retained = _retain("found a match", surface, tmp_path, capsys, caplog)
    assert retained == f"found {MARKER} match"


@pytest.mark.parametrize("surface", SURFACES)
def test_round21_numeric_provenance_preserves_diagnostics(
    monkeypatch, tmp_path, capsys, caplog, surface
):
    monkeypatch.setenv("HTTP_PROXY", "https://proxy.invalid/ab?v=1")
    with sandbox.proxy_redaction_scope("inherit"):
        assert _retain(CONTROLS, surface, tmp_path, capsys, caplog) == CONTROLS
        retained = _retain("found ab and v match", surface, tmp_path, capsys, caplog)
    assert retained == f"found {MARKER} and {MARKER} match"


@pytest.mark.parametrize("surface", SURFACES)
def test_round21_numeric_credential_key_beats_exemption(
    monkeypatch, tmp_path, capsys, caplog, surface
):
    monkeypatch.setenv("HTTP_PROXY", "https://proxy.invalid/?token=1234")
    with sandbox.proxy_redaction_scope("inherit"):
        assert (
            _retain("version 12345", surface, tmp_path, capsys, caplog)
            == "version 12345"
        )
        retained = _retain("found 1234 match", surface, tmp_path, capsys, caplog)
    assert retained == f"found {MARKER} match"


@pytest.mark.parametrize("surface", SURFACES)
def test_round21_short_token_flood_preserves_other_lines(
    monkeypatch, tmp_path, capsys, caplog, surface
):
    monkeypatch.setenv("HTTP_PROXY", "https://proxy.invalid/a")
    report = REPORT + "\n" + "found a match\n" * 500 + CONTROLS
    with sandbox.proxy_redaction_scope("inherit"):
        # Every string leaf preserves both sibling and surrounding evidence.
        retained = _retain(report, surface, tmp_path, capsys, caplog)
    assert retained == report.replace("found a match", f"found {MARKER} match")
    assert retained.count(MARKER) == 500
    assert "[REDACTED]" not in retained


@pytest.mark.parametrize("surface", SURFACES)
def test_round21_five_character_token_preserves_surrounding_report(
    monkeypatch, tmp_path, capsys, caplog, surface
):
    monkeypatch.setenv("HTTP_PROXY", "https://proxy.invalid/?token=abcde")
    report = REPORT + "\nfound abcde match\n" + CONTROLS
    with sandbox.proxy_redaction_scope("inherit"):
        retained = _retain(report, surface, tmp_path, capsys, caplog)
    assert "abcde" not in retained
    assert retained == report.replace("abcde", MARKER)


@pytest.mark.parametrize(
    "launch", [_agent_launch, _container_launch], ids=["agent", "container"]
)
def test_round21_launch_persists_readable_report(tmp_path, monkeypatch, launch):
    """Use production launch scopes and retention with simulated process boundaries."""
    monkeypatch.setenv("HTTP_PROXY", "https://proxy.invalid/a?v=1")
    launch(
        tmp_path,
        monkeypatch,
        "Analysis complete.\nerror: timeout\nexit code 1\nfound a match",
    )
    [path] = tmp_path.glob("*.evidence.json")
    report = json.loads(path.read_text())["report"]
    assert report == (
        "## Dispatch Report\nAnalysis complete.\nerror: timeout\nexit code 1\n"
        f"found {MARKER} match\nDone."
    )


@pytest.mark.parametrize(
    "suffix", ["/2", "/%32", "/?v=2", "/?%32=", "/#2", "/#v=2", "/#2="]
)
def test_round21_numeric_provenance_is_not_a_form(monkeypatch, suffix):
    monkeypatch.setenv("HTTP_PROXY", f"https://proxy.invalid{suffix}")
    with sandbox.proxy_redaction_scope("inherit"):
        assert sandbox.redact_credentials("exit code 2; version 2; count 2") == (
            "exit code 2; version 2; count 2"
        )
        assert "2" not in {form.text for _, form in sandbox._credential_forms()}


@pytest.mark.parametrize("component", ["ab", "a.b", "%61%2Eb", "abcde"])
def test_round21_short_form_uses_ascii_alphanumeric_lookarounds(monkeypatch, component):
    from urllib.parse import unquote_plus

    decoded = unquote_plus(component)
    monkeypatch.setenv("HTTP_PROXY", f"https://proxy.invalid/{component}")
    text = f"X{decoded} {decoded}9 _{decoded}_ é{decoded}é [{decoded}]"
    with sandbox.proxy_redaction_scope("inherit"):
        assert sandbox.redact_credential_values(text) == (
            f"X{decoded} {decoded}9 _{MARKER}_ é{MARKER}é [{MARKER}]"
        )


def test_round21_length_tiers_keep_long_substrings_and_repeated_markers(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "https://proxy.invalid/abcde/abcdef")
    with sandbox.proxy_redaction_scope("inherit"):
        assert sandbox.redact_credentials("XabcdeY") == "XabcdeY"
        assert sandbox.redact_credentials("XabcdefY") == f"X{MARKER}Y"
        assert sandbox.redact_credentials("abcdef " * 9) == f"{MARKER} " * 9
