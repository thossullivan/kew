"""First-edit metric derivation from stream-json transcripts (spec D2)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kew.transcript_metrics import (
    DERIVED_EVIDENCE_VERSION,
    MAX_REPORT_CHARS,
    DerivedRunEvidence,
    FirstEditMetrics,
    RunnerTotals,
    StreamDerivation,
    agent_report_for,
    derive_first_edit_metrics,
    evidence_path_for,
    first_edit_metrics_for,
    read_derived_evidence,
    write_derived_evidence,
)


def _assistant(content: list[dict[str, object]], usage: dict[str, int]) -> str:
    return json.dumps(
        {"type": "assistant", "message": {"content": content, "usage": usage}}
    )


def _text(text: str) -> dict[str, object]:
    return {"type": "text", "text": text}


def _tool(name: str, path: str = "src/x.py") -> dict[str, object]:
    return {"type": "tool_use", "name": name, "input": {"file_path": path}}


def _transcript_with_edit_on_third_step() -> list[str]:
    """Read, Read, then an Edit — the first edit lands on the third step."""
    return [
        json.dumps({"type": "system", "subtype": "init", "model": "sonnet"}),
        _assistant([_text("Let me look")], {"input_tokens": 100, "output_tokens": 10}),
        _assistant([_tool("Read")], {"input_tokens": 200, "output_tokens": 20}),
        _assistant(
            [_tool("Edit")],
            {
                "input_tokens": 300,
                "output_tokens": 30,
                "cache_creation_input_tokens": 5,
                "cache_read_input_tokens": 40,
            },
        ),
        _assistant([_tool("Edit")], {"input_tokens": 400, "output_tokens": 40}),
        json.dumps({"type": "result", "subtype": "success"}),
    ]


def test_first_edit_derived_from_stream_json() -> None:
    """Fixture transcript → correct step index + cumulative token sum."""
    metrics = derive_first_edit_metrics(_transcript_with_edit_on_third_step())
    # Third usage-bearing assistant message carries the first Edit.
    assert metrics.steps_to_first_edit == 3
    # Cumulative tokens across steps 1-3, all token classes summed:
    # (100+10) + (200+20) + (300+30+5+40) = 705
    assert metrics.tokens_to_first_edit == 705


def test_zero_edit_run_stays_null() -> None:
    """A run that never edits keeps both metrics NULL (never 0)."""
    lines = [
        json.dumps({"type": "system", "subtype": "init", "model": "sonnet"}),
        _assistant([_tool("Read")], {"input_tokens": 100, "output_tokens": 10}),
        _assistant([_text("Nothing to do")], {"input_tokens": 50, "output_tokens": 5}),
        json.dumps({"type": "result", "subtype": "success"}),
    ]
    metrics = derive_first_edit_metrics(lines)
    assert metrics.steps_to_first_edit is None
    assert metrics.tokens_to_first_edit is None
    assert metrics == FirstEditMetrics.none()


def test_write_counts_as_first_edit() -> None:
    """A ``Write`` tool_use is an edit for this metric."""
    lines = [
        _assistant([_tool("Read")], {"input_tokens": 100, "output_tokens": 10}),
        _assistant([_tool("Write")], {"input_tokens": 200, "output_tokens": 20}),
    ]
    metrics = derive_first_edit_metrics(lines)
    assert metrics.steps_to_first_edit == 2
    assert metrics.tokens_to_first_edit == 330


def test_multiedit_counts_as_first_edit() -> None:
    """A ``MultiEdit`` tool_use is an edit for this metric."""
    lines = [
        _assistant([_tool("Read")], {"input_tokens": 100, "output_tokens": 10}),
        _assistant([_tool("MultiEdit")], {"input_tokens": 200, "output_tokens": 20}),
    ]
    metrics = derive_first_edit_metrics(lines)
    assert metrics.steps_to_first_edit == 2
    assert metrics.tokens_to_first_edit == 330


def test_edit_alongside_read_in_same_step() -> None:
    """A step whose message mixes Read + Edit still counts that step's tokens."""
    lines = [
        _assistant(
            [_tool("Read"), _tool("Edit")],
            {"input_tokens": 100, "output_tokens": 50},
        ),
    ]
    metrics = derive_first_edit_metrics(lines)
    assert metrics.steps_to_first_edit == 1
    assert metrics.tokens_to_first_edit == 150


def test_non_usage_messages_are_not_steps() -> None:
    """Assistant messages without usage don't advance the step counter."""
    lines = [
        json.dumps({"type": "assistant", "message": {"content": [_text("thinking")]}}),
        _assistant([_tool("Edit")], {"input_tokens": 100, "output_tokens": 10}),
    ]
    metrics = derive_first_edit_metrics(lines)
    assert metrics.steps_to_first_edit == 1
    assert metrics.tokens_to_first_edit == 110


def test_malformed_lines_are_skipped() -> None:
    """Blank and non-JSON lines never raise."""
    lines = [
        "",
        "not json at all",
        "   ",
        _assistant([_tool("Edit")], {"input_tokens": 100, "output_tokens": 10}),
    ]
    metrics = derive_first_edit_metrics(lines)
    assert metrics.steps_to_first_edit == 1
    assert metrics.tokens_to_first_edit == 110


def _derive(lines: list[str]) -> StreamDerivation:
    derivation = StreamDerivation()
    for line in lines:
        derivation.observe(line)
    return derivation


def _log_with_evidence(tmp_path: Path, name: str, lines: list[str]) -> Path:
    """A sanitized log plus its derived-evidence sidecar — what a run leaves."""
    log = tmp_path / name
    log.write_text("", encoding="utf-8")
    write_derived_evidence(log, _derive(lines).evidence())
    return log


# ---------------------------------------------------------------------------
# The sidecar replaces raw-transcript replay
# ---------------------------------------------------------------------------


def test_first_edit_read_from_derived_evidence(tmp_path: Path) -> None:
    """The same numbers the old raw replay produced, from the sidecar."""
    log = _log_with_evidence(
        tmp_path, "dispatch-1.log", _transcript_with_edit_on_third_step()
    )
    metrics = first_edit_metrics_for(log)
    assert metrics.steps_to_first_edit == 3
    assert metrics.tokens_to_first_edit == 705


def test_first_edit_missing_path_is_null() -> None:
    assert first_edit_metrics_for(None) == FirstEditMetrics.none()
    assert (
        first_edit_metrics_for(Path("/nonexistent/does-not-exist.log"))
        == FirstEditMetrics.none()
    )


def test_legacy_raw_transcript_is_unavailable_not_reingested(tmp_path: Path) -> None:
    """A pre-I06T run has a raw log and NO sidecar: unavailable, never parsed.

    This is the whole migration in one assertion — the transcript sitting on
    disk plainly contains the answer, and kew refuses to read it back.
    """
    legacy = tmp_path / "dispatch-legacy.log"
    legacy.write_text(
        "\n".join(_transcript_with_edit_on_third_step()), encoding="utf-8"
    )
    assert read_derived_evidence(legacy) is None
    assert first_edit_metrics_for(legacy) == FirstEditMetrics.none()
    assert agent_report_for(legacy) is None


def test_sidecar_from_a_newer_kew_reads_as_unavailable(tmp_path: Path) -> None:
    log = tmp_path / "dispatch-2.log"
    evidence_path_for(log).write_text(
        json.dumps({"version": DERIVED_EVIDENCE_VERSION + 1, "steps": 9}),
        encoding="utf-8",
    )
    assert read_derived_evidence(log) is None


def test_version_one_sidecar_reads_without_terminal_error_facts(tmp_path: Path) -> None:
    """Version 1 predates bounded OpenCode terminal-error evidence."""
    log = tmp_path / "dispatch-v1.log"
    evidence_path_for(log).write_text(
        json.dumps(
            {
                "version": 1,
                "terminal_type": "error",
                "error_name": "APIError",
                "http_status": 429,
            }
        ),
        encoding="utf-8",
    )

    evidence = read_derived_evidence(log)

    assert DERIVED_EVIDENCE_VERSION == 2
    assert evidence is not None
    assert evidence.terminal_type == "error"
    assert evidence.error_name is None
    assert evidence.http_status is None


@pytest.mark.parametrize("version", [True, 1.0, "2", 0, 3])
def test_malformed_or_unsupported_sidecar_versions_are_unavailable(
    tmp_path: Path, version: object
) -> None:
    log = tmp_path / "dispatch-invalid-version.log"
    evidence_path_for(log).write_text(
        json.dumps({"version": version, "steps": 9}),
        encoding="utf-8",
    )

    assert read_derived_evidence(log) is None


def test_sidecar_survives_a_round_trip(tmp_path: Path) -> None:
    log = _log_with_evidence(
        tmp_path, "dispatch-3.log", _transcript_with_edit_on_third_step()
    )
    evidence = read_derived_evidence(log)
    assert evidence is not None
    assert evidence.steps == 4
    assert dict(evidence.tool_uses) == {"Edit": 2, "Read": 1}
    assert evidence.tool_use_count == 3
    assert evidence.terminal_type == "result"
    assert evidence.error_name is None
    assert evidence.http_status is None
    assert evidence.first_edit == FirstEditMetrics(3, 705)


def test_sidecar_is_owner_only(tmp_path: Path) -> None:
    log = _log_with_evidence(tmp_path, "dispatch-4.log", ["{}"])
    assert (evidence_path_for(log).stat().st_mode & 0o777) == 0o600


def test_sidecar_refuses_to_follow_a_symlink(tmp_path: Path) -> None:
    """A planted symlink beside the log must not redirect the write."""
    log = tmp_path / "dispatch-5.log"
    target = tmp_path / "elsewhere.json"
    target.write_text("untouched", encoding="utf-8")
    evidence_path_for(log).symlink_to(target)
    assert write_derived_evidence(log, DerivedRunEvidence()) is None
    assert target.read_text(encoding="utf-8") == "untouched"


# ---------------------------------------------------------------------------
# Streaming derivation keeps facts and drops bytes
# ---------------------------------------------------------------------------


_SECRETS = (
    "sk-ant-api03-DEADBEEF",
    "https://internal.example.test/secret",
    "/Users/someone/private/notes.txt",
    "def transfer(): return account.balance",
)


def _hostile_stream() -> list[str]:
    """Provider lines carrying reasoning, source, a credential, a URL, a path."""
    return [
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "thinking", "thinking": _SECRETS[3]},
                        {"type": "text", "text": f"{_SECRETS[0]} at {_SECRETS[1]}"},
                        _tool("Read", _SECRETS[2]),
                    ],
                    "usage": {"input_tokens": 10, "output_tokens": 2},
                },
            }
        ),
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [_tool("Edit", _SECRETS[2])],
                    "usage": {"input_tokens": 20, "output_tokens": 3},
                },
            }
        ),
        json.dumps({"type": "result", "result": f"done {_SECRETS[0]}"}),
    ]


def test_observe_returns_only_structural_progress_text() -> None:
    derivation = StreamDerivation()
    safe = [derivation.observe(line) for line in _hostile_stream()]
    assert safe == [
        "step 1 tokens=12 tools=Read",
        "step 2 tokens=35 tools=Edit",
        "event result",
    ]
    for line in safe:
        assert line is not None
        for secret in _SECRETS:
            assert secret not in line


def test_derived_evidence_carries_no_provider_prose() -> None:
    derivation = _derive(_hostile_stream())
    serialized = json.dumps(derivation.evidence().as_json())
    for secret in _SECRETS:
        assert secret not in serialized
    assert derivation.evidence().first_edit == FirstEditMetrics(2, 35)


def test_opencode_error_keeps_only_classification_facts() -> None:
    """OpenCode's API status is nested under error.data in its event contract."""
    derivation = StreamDerivation()
    safe_line = derivation.observe(
        json.dumps(
            {
                "type": "error",
                "sessionID": "oc-secret-session",
                "error": {
                    "name": "APIError",
                    "data": {
                        "message": _SECRETS[0],
                        "statusCode": 429,
                        "isRetryable": True,
                        "responseHeaders": {"authorization": _SECRETS[0]},
                        "responseBody": _SECRETS[1],
                    },
                    "arbitrary": {"provider": _SECRETS[2]},
                },
            }
        )
    )

    evidence = derivation.evidence()
    serialized = json.dumps(evidence.as_json())
    assert safe_line == "event error"
    assert evidence.terminal_type == "error"
    assert evidence.error_name == "APIError"
    assert evidence.http_status == 429
    assert evidence.as_json().get("error_name") == "APIError"
    assert evidence.as_json().get("http_status") == 429
    for secret in _SECRETS:
        assert secret not in serialized


@pytest.mark.parametrize(
    "error",
    [
        _SECRETS[0],
        [_SECRETS[1]],
        {"data": {"message": _SECRETS[2]}},
        {"name": {"arbitrary": _SECRETS[3]}, "data": {}},
    ],
    ids=["string", "list", "nested-message", "nested-name"],
)
def test_arbitrary_error_objects_never_enter_evidence(error: object) -> None:
    derivation = _derive([json.dumps({"type": "error", "error": error})])

    evidence = derivation.evidence()
    serialized = json.dumps(evidence.as_json())
    assert evidence.error_name is None
    assert evidence.http_status is None
    for secret in _SECRETS:
        assert secret not in serialized


def test_identifier_shaped_secret_is_not_an_error_name() -> None:
    derivation = _derive(
        [
            json.dumps(
                {
                    "type": "error",
                    "error": {
                        "name": _SECRETS[0],
                        "data": {"statusCode": 503},
                    },
                }
            )
        ]
    )

    evidence = derivation.evidence()
    assert evidence.error_name is None
    assert _SECRETS[0] not in json.dumps(evidence.as_json())


@pytest.mark.parametrize(
    "name",
    [
        "APIError",
        "ContextOverflowError",
        "MessageAbortedError",
        "MessageOutputLengthError",
        "NetworkError",
        "OverloadedError",
        "ProviderAuthError",
        "RateLimitError",
        "StructuredOutputError",
        "UnknownError",
    ],
)
def test_known_opencode_error_names_are_retained(name: str) -> None:
    derivation = _derive(
        [json.dumps({"type": "error", "error": {"name": name, "data": {}}})]
    )

    assert derivation.evidence().error_name == name


@pytest.mark.parametrize(
    "name",
    [True, 7, "", "Bad Error", "LineBreak\nError", "E" * 49],
)
def test_malformed_or_oversized_error_names_are_omitted(name: object) -> None:
    derivation = _derive(
        [
            json.dumps(
                {
                    "type": "error",
                    "error": {"name": name, "data": {"statusCode": 503}},
                }
            )
        ]
    )

    evidence = derivation.evidence()
    assert evidence.error_name is None
    assert evidence.http_status == 503


@pytest.mark.parametrize("status", [True, 429.0, "429", 99, 600, -1])
def test_non_integer_or_out_of_range_http_status_is_omitted(status: object) -> None:
    derivation = _derive(
        [
            json.dumps(
                {
                    "type": "error",
                    "error": {"name": "APIError", "data": {"statusCode": status}},
                }
            )
        ]
    )

    evidence = derivation.evidence()
    assert evidence.error_name == "APIError"
    assert evidence.http_status is None


def test_error_facts_round_trip_in_the_closed_version_two_schema(
    tmp_path: Path,
) -> None:
    derivation = _derive(
        [
            json.dumps(
                {
                    "type": "error",
                    "error": {
                        "name": "APIError",
                        "data": {"message": _SECRETS[3], "statusCode": 400},
                    },
                }
            )
        ]
    )
    log = tmp_path / "dispatch-error.log"

    write_derived_evidence(log, derivation.evidence())
    raw = json.loads(evidence_path_for(log).read_text(encoding="utf-8"))
    evidence = read_derived_evidence(log)

    assert set(raw) == {
        "error_name",
        "http_status",
        "lines_observed",
        "needs_human",
        "oom_probe_diagnostic",
        "oom_probe_status",
        "quota_exhausted",
        "report",
        "resource_cap_observations",
        "resource_cap_observations_truncated",
        "steps",
        "steps_to_first_edit",
        "terminal_type",
        "tokens_to_first_edit",
        "tool_uses",
        "totals",
        "version",
    }
    assert raw["version"] == DERIVED_EVIDENCE_VERSION == 2
    assert raw["error_name"] == "APIError"
    assert raw["http_status"] == 400
    assert evidence is not None
    assert evidence.error_name == "APIError"
    assert evidence.http_status == 400
    assert _SECRETS[3] not in json.dumps(raw)


def test_later_non_error_event_clears_terminal_error_facts() -> None:
    derivation = _derive(
        [
            json.dumps(
                {
                    "type": "error",
                    "error": {
                        "name": "APIError",
                        "data": {"statusCode": 503},
                    },
                }
            ),
            json.dumps({"type": "result", "result": "done"}),
        ]
    )

    evidence = derivation.evidence()
    assert evidence.terminal_type == "result"
    assert evidence.error_name is None
    assert evidence.http_status is None


def test_unparsable_and_blank_lines_are_counted_not_kept() -> None:
    derivation = StreamDerivation()
    assert derivation.observe("not json {" + _SECRETS[0]) is None
    assert derivation.observe("   \n") is None
    assert derivation.observe(json.dumps(["a", "list"])) is None
    assert derivation.evidence().lines_observed == 3
    assert derivation.evidence().steps == 0


def test_tool_names_are_bounded_identifier_tokens() -> None:
    derivation = StreamDerivation()
    derivation.observe(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "name": "Edit; rm -rf / #" + "x" * 90}
                    ],
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            }
        )
    )
    ((name, count),) = derivation.evidence().tool_uses
    assert count == 1
    assert set(name) <= set(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-"
    )
    assert len(name) <= 48


def test_event_type_is_sanitized() -> None:
    derivation = StreamDerivation()
    assert derivation.observe(json.dumps({"type": "we ird\ntype"})) == "event weirdtype"


# ---------------------------------------------------------------------------
# The bounded Dispatch Report is derived while streaming
# ---------------------------------------------------------------------------


_REPORT = "## Dispatch Report\n\n**Issue:** #5\n\n### What was done\n- x"


def test_report_derived_from_terminal_result_event() -> None:
    derivation = _derive(
        [
            json.dumps({"type": "result", "result": "API Error: stalled"}),
            json.dumps({"type": "result", "result": _REPORT}),
        ]
    )
    assert derivation.evidence().report == _REPORT


def test_report_derived_from_codex_agent_message() -> None:
    derivation = _derive(
        [
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": _REPORT},
                }
            )
        ]
    )
    assert derivation.evidence().report == _REPORT


def test_no_report_when_the_run_ended_without_one() -> None:
    derivation = _derive([json.dumps({"type": "result", "result": "just some prose"})])
    assert derivation.evidence().report is None


def test_report_is_bounded() -> None:
    derivation = _derive(
        [json.dumps({"type": "result", "result": _REPORT + "\n" + "x" * 60_000})]
    )
    report = derivation.evidence().report
    assert report is not None
    assert len(report) == MAX_REPORT_CHARS


def test_report_strips_preamble_and_wrapping_fence() -> None:
    wrapped = "Committed. Here is my dispatch report.\n\n```\n" + _REPORT + "\n```"
    derivation = _derive([json.dumps({"type": "result", "result": wrapped})])
    assert derivation.evidence().report == _REPORT


def test_runner_totals_round_trip_through_the_sidecar(tmp_path: Path) -> None:
    derivation = _derive(_transcript_with_edit_on_third_step())
    derivation.record_totals(
        RunnerTotals(
            cost_usd=1.25,
            input_tokens=7,
            output_tokens=8,
            reported_model="claude-opus-5",
            from_result_event=True,
            has_native_cost=True,
        )
    )
    log = tmp_path / "dispatch-6.log"
    write_derived_evidence(log, derivation.evidence())
    evidence = read_derived_evidence(log)
    assert evidence is not None
    assert evidence.totals.cost_usd == 1.25
    assert evidence.totals.reported_model == "claude-opus-5"
    assert evidence.totals.from_result_event is True


@pytest.mark.parametrize("source", ["stream", "direct", "readback"])
@pytest.mark.parametrize(
    "padding", ["", "a" * (MAX_REPORT_CHARS - 45)], ids=["whole", "boundary"]
)
def test_inherited_proxy_is_redacted_from_retained_report(
    tmp_path, monkeypatch, source, padding
):
    secret = "http://user:s3cret@proxy.invalid:3128"
    monkeypatch.setenv("HTTP_PROXY", secret)
    report = f"## Dispatch Report\n{padding}{secret}\nDone."
    if source == "stream":
        derivation = StreamDerivation()
        derivation.observe(json.dumps({"type": "result", "result": report}))
        evidence = derivation.evidence()
    elif source == "direct":
        evidence = DerivedRunEvidence(report=report)
    else:
        evidence = DerivedRunEvidence.from_json(
            {"version": DERIVED_EVIDENCE_VERSION, "report": report}
        )
        assert evidence is not None
    log = tmp_path / "proxy.log"
    path = write_derived_evidence(log, evidence)
    assert path is not None
    serialized = path.read_text()
    assert secret not in serialized
    assert "proxy.invalid" not in serialized
    assert "http://user" not in serialized
    assert "redacted" in serialized
    if source != "direct":
        assert secret not in (evidence.report or "")
    assert secret not in (agent_report_for(log) or "")


@pytest.mark.parametrize("source", ["stream", "direct", "readback"])
def test_container_diagnostics_redact_proxy_before_retention(
    tmp_path, monkeypatch, source
):
    secret = "http://user:s3cret@proxy.invalid:3128"
    monkeypatch.setenv("HTTP_PROXY", secret)
    reason = "probe failed: " + secret
    observations = [{"code": "oom", "reason": reason}]
    if source == "stream":
        derivation = StreamDerivation()
        derivation.record_container_evidence(
            oom_probe_status="unavailable",
            oom_probe_diagnostic=reason,
            resource_cap_observations=observations,
            resource_cap_observations_truncated=False,
        )
        evidence = derivation.evidence()
    elif source == "direct":
        evidence = DerivedRunEvidence(
            oom_probe_diagnostic=reason, resource_cap_observations=(("oom", reason),)
        )
    else:
        evidence = DerivedRunEvidence.from_json(
            {
                "version": DERIVED_EVIDENCE_VERSION,
                "oom_probe_diagnostic": reason,
                "resource_cap_observations": observations,
            }
        )
        assert evidence is not None
    serialized = json.dumps(evidence.as_json())
    assert secret not in serialized
    assert "probe failed: <HTTP_PROXY redacted>" in serialized


@pytest.mark.parametrize("host_proxy", [True, False])
@pytest.mark.parametrize(
    "identifier",
    [
        "http://user:s3cret@proxy.invalid:3128",
        "user:s3cret@proxy.invalid:3128",
        "http://user:s3cret@" + "x" * 80 + ":3128",
    ],
)
def test_credential_identifiers_never_reach_logs_or_metadata(
    tmp_path, monkeypatch, host_proxy, identifier
):
    from dataclasses import asdict

    from kew.sandbox import CONTAINER_PROXY_NAMES

    for name in CONTAINER_PROXY_NAMES:
        monkeypatch.delenv(name, raising=False)
    if host_proxy:
        monkeypatch.setenv("HTTP_PROXY", identifier)
    derivation = StreamDerivation()
    line = derivation.observe(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "usage": {"input_tokens": 1},
                    "content": [{"type": "tool_use", "name": identifier}],
                },
            }
        )
    )
    assert line is not None
    assert "s3cret" not in line
    event_line = derivation.observe(json.dumps({"type": identifier}))
    assert "s3cret" not in (event_line or "")
    derivation.record_totals(RunnerTotals(reported_model=identifier))
    derivation.record_container_evidence(
        oom_probe_status=identifier,
        oom_probe_diagnostic=None,
        resource_cap_observations=[{"code": identifier, "reason": "bounded"}],
        resource_cap_observations_truncated=False,
    )
    evidence = derivation.evidence()
    assert all("s3cret" not in name for name, _ in evidence.tool_uses)
    assert "s3cret" not in json.dumps(asdict(evidence))
    assert "s3cret" not in json.dumps(evidence.as_json())
    path = write_derived_evidence(tmp_path / "proxy.log", evidence)
    assert path is not None
    assert "s3cret" not in path.read_text()


@pytest.mark.parametrize("source", ["direct", "readback"])
def test_all_retained_identifier_fields_redact_passwords(monkeypatch, source):
    secret = "http://user:s3cret@proxy.invalid:3128"
    monkeypatch.setenv("HTTP_PROXY", secret)
    if source == "direct":
        evidence = DerivedRunEvidence(
            tool_uses=((secret, 1),),
            terminal_type=secret,
            oom_probe_status=secret,
            totals=RunnerTotals(reported_model=secret),
            resource_cap_observations=((secret, "bounded"),),
        )
    else:
        evidence = DerivedRunEvidence.from_json(
            {
                "version": DERIVED_EVIDENCE_VERSION,
                "tool_uses": {secret: 1},
                "terminal_type": secret,
                "oom_probe_status": secret,
                "totals": {"reported_model": secret},
                "resource_cap_observations": [{"code": secret, "reason": "bounded"}],
            }
        )
        assert evidence is not None
    assert "s3cret" not in json.dumps(evidence.as_json())


def test_needs_human_stop_round_trips_through_the_sidecar(tmp_path: Path) -> None:
    """#2601: the contained path reads the agent's stop from the sidecar, so
    the reason must survive the write and come back sanitized and bounded."""
    derivation = StreamDerivation()
    derivation.record_message("tool_use", "")
    derivation.record_message("activity", "KEW_NEEDS_HUMAN: policy gate failed")
    log = tmp_path / "dispatch-stop.log"

    write_derived_evidence(log, derivation.evidence())
    evidence = read_derived_evidence(log)

    assert evidence is not None
    assert evidence.needs_human == "policy gate failed"
    overlong = json.loads(evidence_path_for(log).read_text(encoding="utf-8"))
    overlong["needs_human"] = "x" * 2000
    evidence_path_for(log).write_text(json.dumps(overlong), encoding="utf-8")
    assert read_derived_evidence(log).needs_human == "x" * 500  # type: ignore[union-attr]
    assert read_derived_evidence(tmp_path / "missing.log") is None
    clean = tmp_path / "dispatch-clean.log"
    write_derived_evidence(clean, StreamDerivation().evidence())
    assert read_derived_evidence(clean).needs_human is None  # type: ignore[union-attr]
