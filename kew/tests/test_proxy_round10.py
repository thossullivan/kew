"""Credential containment across folded headers and display normalization."""

import json
from unittest.mock import MagicMock, patch

import pytest
from support.agent_report import visible_text

from kew.config import DispatchConfig
from kew.progress import display_event, summarize_tool_input
from kew.publication import (
    PublicationError,
    sanitize_publication_error,
    sanitize_publication_evidence,
)
from kew.sandbox import CONTAINER_PROXY_NAMES
from kew.transcript_metrics import StreamDerivation, write_derived_evidence


@pytest.fixture(autouse=True)
def _proxy_credentials(monkeypatch):
    for name in CONTAINER_PROXY_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTP_PROXY", "http://user:ProxySecret123@proxy.invalid")


@pytest.mark.parametrize(
    "header",
    [
        'Proxy-Authorization: Digest nonce="\r\n\tdXNlcjpQcm94\r\n\teVNlY3JldDEyMw=="',
        'Proxy-Authorization: Digest nonce="\n dXNlcjpQcm94\n eVNlY3JldDEyMw=="',
        'Proxy-Authorization: Digest nonce="\n\tdXNlcjpQcm94\n\teVNlY3JldDEyMw=="',
        'Proxy-Authorization: Digest nonce="\r\tdXNlcjpQcm94\r\teVNlY3JldDEyMw=="',
        'Authorization: Digest nonce="prefix\\"dXNlcjpQcm94\n eVNlY3JldDEyMw=="',
        'Authorization: Digest nonce="dXNlcjpQcm94\n eVNlY3JldDEyMw==", realm=public',
        'Proxy-Authorization: Di\n gest nonce="dXNlcjpQcm94\n eVNlY3JldDEyMw=="',
        'Proxy-Authorization: Digest nonce="dXNlcjpQcm94\n eVNlY3JldDEyMw==',
        'Authorization: Digest nonce="x" dXNlcjpQcm94 eVNlY3JldDEyMw==',
        "Authorization: ??? dXNlcjpQcm94 eVNlY3JldDEyMw==",
        "Authorization: <redacted> dXNlcjpQcm94 eVNlY3JldDEyMw==",
    ],
    ids=[
        "crlf-tabs",
        "lf-spaces",
        "lf-tabs",
        "cr-tabs",
        "escaped-quote",
        "parameter-list",
        "folded-scheme",
        "unterminated-quote",
        "trailing-fragments",
        "invalid-scheme",
        "forged-marker",
    ],
)
@pytest.mark.parametrize(
    "surface", ["serialized_report", "publication_body", "error", "live_display"]
)
def test_authorization_value_is_an_indivisible_unit(tmp_path, capsys, header, surface):
    from kew.dispatch_core import _post_agent_report

    # Uncertain credential values end at the next peer key, not arbitrary prose.
    report = f"## Dispatch Report\n{header}\nstatus: complete\nDone."
    log = tmp_path / "dispatch.log"
    if surface in ("serialized_report", "publication_body"):
        derivation = StreamDerivation()
        derivation.observe(json.dumps({"type": "result", "result": report}))
        path = write_derived_evidence(log, derivation.evidence())
        if surface == "serialized_report":
            retained = path.read_text()
        else:
            with patch(
                "kew.dispatch_core.run", return_value=MagicMock(returncode=0)
            ) as post:
                _post_agent_report(
                    "https://github.com/example/repo/pull/1941", log, DispatchConfig()
                )
            argv = post.call_args.args[0]
            retained = visible_text(argv[argv.index("--body") + 1])
    elif surface == "error":
        retained = sanitize_publication_error(report)
    else:
        display_event({"type": "result", "result": report})
        retained = capsys.readouterr().err
    for credential in (
        "dXNlcjpQcm94",
        "eVNlY3JldDEyMw",
        "ProxySecret123",
        "user:ProxySecret123",
        "nonce",
    ):
        assert credential not in retained
    assert "<redacted>" in retained
    assert "Done." in retained


@pytest.fixture(
    params=["Proxy%20Secret123", "Proxy+Secret123"], ids=["percent", "plus"]
)
def _space_password(monkeypatch, request):
    monkeypatch.setenv("HTTP_PROXY", f"http://user:{request.param}@proxy.invalid")


def _assert_space_password_absent(retained):
    assert "Proxy Secret123" not in retained
    assert "Proxy\nSecret123" not in retained
    assert "Proxy\\nSecret123" not in retained
    assert "<HTTP_PROXY redacted>" in retained


def test_split_password_never_reaches_sqlite_deferral(tmp_path, _space_password):
    from test_dispatch_core import TestTheDeferralWriterIsCutOverToTheNormalizedJob

    import kew.dispatch_core as dispatch_core
    from kew.run_store import SqliteRunStore

    scenario = TestTheDeferralWriterIsCutOverToTheNormalizedJob()
    with SqliteRunStore(str(tmp_path / "deferral.db")) as store:
        scenario._run_and_job(store, with_job=False)
        scenario._defer(
            dispatch_core,
            store,
            tmp_path,
            error=PublicationError("push", "request failed: Proxy\nSecret123"),
        )
        [event] = store.list_audit_events(event="publication_deferred")
        _assert_space_password_absent(json.dumps(event.payload))
    _assert_space_password_absent((tmp_path / "run.log").read_text())


@pytest.mark.parametrize(
    "sanitize", [sanitize_publication_error, sanitize_publication_evidence]
)
@pytest.mark.parametrize("separator", ["\n", "\r\n\t", "\t", "\x00"])
def test_publication_rechecks_collapsed_password(_space_password, sanitize, separator):
    retained = sanitize(f"request failed: Proxy{separator}Secret123")
    _assert_space_password_absent(retained)


@pytest.mark.parametrize(
    "surface", ["assistant", "text_block", "result", "tool_summary"]
)
def test_progress_rechecks_collapsed_password(_space_password, capsys, surface):
    text = "request failed: Proxy\nSecret123"
    if surface == "tool_summary":
        retained = summarize_tool_input(text)
    else:
        event = {
            "assistant": {"type": "assistant", "message": text},
            "text_block": {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": text}]},
            },
            "result": {"type": "result", "result": text},
        }[surface]
        display_event(event)
        retained = capsys.readouterr().err
    _assert_space_password_absent(retained)


@pytest.mark.parametrize("encoded", ["%252F", "%25252F", "//"])
def test_evidence_rechecks_decoding_and_path_normalization(monkeypatch, encoded):
    monkeypatch.setenv("HTTP_PROXY", "http://user:Proxy%2FSecret123@proxy.invalid")
    retained = sanitize_publication_evidence(f"request failed: Proxy{encoded}Secret123")
    assert "Proxy/Secret123" not in retained
    assert "<HTTP_PROXY redacted>" in retained
