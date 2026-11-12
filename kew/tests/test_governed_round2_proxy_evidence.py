"""Governed review round 1 (2026-09-21) for PR #1941.

Finding 1: a retained report is never truncated after a markdown bullet whose
key looks like a credential stem ("- Tokens: 45,120 across 3 turns").
Finding 2: the container run argv controls all ten proxy names; the four that
Docker client config and Podman can inject are blank under either policy.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kew import sandbox
from kew.sandbox import (
    ALWAYS_BLANK_PROXY_NAMES,
    CONTAINER_PROXY_NAMES,
    CONTROLLED_PROXY_NAMES,
    redact_credentials,
)

REPORT = """# Dispatch Report

## Summary

- Files changed: 3
- Tokens: 45,120 across 3 turns
- Duration: 4m12s

## Verification

Ran the affected modules; 42 passed.

## Notes

Nothing else to report.
"""


def test_governed_round2_metric_bullet_keeps_later_sections():
    redacted = redact_credentials(REPORT)
    for line in (
        "- Duration: 4m12s",
        "## Verification",
        "Ran the affected modules; 42 passed.",
        "## Notes",
        "Nothing else to report.",
    ):
        assert line in redacted, redacted
    assert redacted.count("\n") == REPORT.count("\n")


def test_governed_round2_same_indent_sequence_stays_owned_by_its_key():
    # YAML: a block sequence may sit at the parent key's indentation, so a key
    # WITHOUT an inline value still owns those items.
    text = "password:\n- hunter2-first\n- hunter2-second\nnext: value\n"
    redacted = redact_credentials(text)
    assert "hunter2-first" not in redacted
    assert "hunter2-second" not in redacted
    assert "next: value" in redacted


def test_governed_round2_block_scalar_stays_consumed():
    text = "secret: |\n  line-one-value\n  line-two-value\nafter: kept\n"
    redacted = redact_credentials(text)
    assert "line-one-value" not in redacted
    assert "line-two-value" not in redacted
    assert "after: kept" in redacted


def _run_argv(policy: str) -> list[str]:
    return sandbox.build_run_argv(
        runtime="docker",
        image="fake:image",
        checkout_dir=os.getcwd(),
        slug="governed-round2",
        network="none",
        runner="claude-code",
        proxy_environment=policy,
    )


def _env_assignments(argv: list[str]) -> dict[str, str]:
    return {
        argv[i + 1].partition("=")[0]: argv[i + 1]
        for i, arg in enumerate(argv)
        if arg == "-e" and argv[i + 1].partition("=")[0] in CONTROLLED_PROXY_NAMES
    }


@pytest.mark.parametrize("policy", ["none", "inherit"])
def test_governed_round2_all_ten_proxy_names_are_assigned(monkeypatch, policy):
    for name in CONTROLLED_PROXY_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTP_PROXY", "http://user:pass-word-x@proxy.invalid:3128")
    monkeypatch.setenv("ALL_PROXY", "socks5://user:pass-word-y@proxy.invalid:1080")
    monkeypatch.setenv("ftp_proxy", "http://user:pass-word-z@proxy.invalid:2121")
    assignments = _env_assignments(_run_argv(policy))
    assert set(assignments) == set(CONTROLLED_PROXY_NAMES)
    for name in ALWAYS_BLANK_PROXY_NAMES:
        assert assignments[name] == f"{name}=", (policy, name)
    if policy == "inherit":
        assert assignments["HTTP_PROXY"] == "HTTP_PROXY"
    else:
        assert assignments["HTTP_PROXY"] == "HTTP_PROXY="
    for name in CONTAINER_PROXY_NAMES:
        if name != "HTTP_PROXY":
            assert assignments[name] == f"{name}="


def test_governed_round2_actions_record_the_four_always_blank_names(monkeypatch):
    for name in CONTROLLED_PROXY_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ALL_PROXY", "socks5://proxy.invalid:1080")
    actions = sandbox._proxy_variable_actions("inherit")
    assert actions["inherited"] == []
    assert set(ALWAYS_BLANK_PROXY_NAMES) <= set(actions["blanked"])


def test_governed_round2_separator_free_prose_redacts_in_linear_time():
    """Round 2 (2026-09-21): 42,880 chars of prose took 12.4 s before the key
    bound; the same text now takes well under a second."""
    import time

    words = "the quick brown fox jumps over the lazy dog while the agent runs "
    prose = (words * (42880 // len(words) + 1))[:42880]
    for text in (prose, "Note: " + prose, prose + " password: hunter2-1933"):
        started = time.perf_counter()
        redacted = redact_credentials(text)
        assert time.perf_counter() - started < 2.0
        assert "hunter2-1933" not in redacted
        assert "quick brown fox" in redacted


def test_governed_round2_docs_name_the_ten_blanked_proxy_names():
    from kew.sandbox import posture_record_fields

    page = (Path(__file__).parents[2] / "docs" / "configuration.md").read_text()
    assert "controls ten proxy variables in agent containers" in page
    assert "`blanked` contains all ten names" in page
    fields = posture_record_fields(
        runner="claude-code", posture="sandboxed", egress_verification="allowlist"
    )
    assert fields["proxy_variables"]["blanked"] == list(CONTROLLED_PROXY_NAMES)
    assert fields["proxy_variables"]["inherited"] == []


def test_governed_round2_malformed_usage_never_ends_a_host_run():
    """A non-integral token count on an assistant event is not a run summary
    and never raises; the host line processor ignores a line the runner cannot
    parse instead of letting the error end the read loop."""
    import json
    from io import StringIO

    from kew.orchestrator import _process_line
    from kew.runners import get_runner
    from kew.tracking import DispatchResult
    from kew.transcript_metrics import StreamDerivation

    runner = get_runner("claude-code")
    assistant = {
        "type": "assistant",
        "usage": {"input_tokens": 1.5},
        "message": {"content": []},
    }
    assert runner.parse_line(json.dumps(assistant)) == []

    class _Raising:
        name = "claude-code"

        def parse_line(self, line):
            raise ValueError("usage.tokens is not an integer")

    result = DispatchResult(exit_code=0, issue_num=1933, log_file=None)
    last, model = _process_line(
        json.dumps({"type": "result", "usage": {"input_tokens": True}}),
        StringIO(),
        None,
        _Raising(),  # type: ignore[arg-type]
        result,
        "claude-opus-5",
        None,
        "previous",
        StreamDerivation(),
    )
    assert (last, model) == ("previous", "claude-opus-5")


def test_governed_round3_a_scalar_never_swallows_its_siblings():
    """Round 3 (2026-09-21): a plain scalar took every following line with any
    leading whitespace, so one opaque field deleted its same-indent siblings
    from a retained report. It continues only on lines indented deeper."""
    report = "## Run\n  token: abc123\n  status: available\n  duration: 4m12s\nDone.\n"
    redacted = redact_credentials(report)
    assert "abc123" not in redacted
    for kept in ("  status: available", "  duration: 4m12s", "Done.", "## Run"):
        assert kept in redacted, redacted
    assert redacted.count("\n") == report.count("\n")


def test_governed_round3_a_deeper_continuation_stays_owned_by_its_key():
    """A genuinely deeper line is part of the value and is still replaced."""
    text = "token: first-part\n  second-part-1933\nnext: kept\n"
    redacted = redact_credentials(text)
    assert "first-part" not in redacted
    assert "second-part-1933" not in redacted
    assert "next: kept" in redacted


def test_governed_round3_docs_and_docstring_state_what_the_code_does():
    from pathlib import Path as _Path

    from kew.sandbox import build_run_argv

    assert "blanks all ten controlled" in build_run_argv.__doc__
    page = (_Path(__file__).parents[2] / "docs" / "security.md").read_text()
    assert "Sequence items never terminate an opaque block." not in page
    assert "only when the key carried an inline value" in page


def test_governed_round4_probe_identity_is_the_profile_fingerprint():
    """A probe's identity has to be what its consumer can reconstruct.

    service/work.py looks readiness up by the realized profile's fingerprint.
    Mixing this launch's proxy posture in made every settled probe unfindable
    and retired the readiness_refuted gate (governed review round 4)."""
    from types import SimpleNamespace

    from kew.readiness_probe import ProbeRoute

    profile = SimpleNamespace(fingerprint="f" * 64, runner="claude-code")
    common = {
        "runner": SimpleNamespace(name="claude-code"),
        "model": "claude-opus-5",
        "profile": profile,
        "sandbox": SimpleNamespace(image=None),
        "network": "allowlist",
    }
    plain = ProbeRoute(**common)
    proxied = ProbeRoute(
        **common,
        proxy_environment="inherit",
        inherited_proxy_names=("HTTP_PROXY",),
        blanked_proxy_names=("HTTPS_PROXY",),
        inherited_proxy_digest="d" * 64,
    )
    assert plain.fingerprint == profile.fingerprint
    assert proxied.fingerprint == profile.fingerprint
    # The posture stays attested, just not as identity.
    assert proxied.proxy_material["proxy_environment"] == "inherit"
    assert proxied.proxy_material["proxy_variables"]["inherited"] == ["HTTP_PROXY"]


def test_governed_round4_credential_forms_are_cached_against_their_inputs(monkeypatch):
    """Rebuilding every regex per call cost tens of milliseconds, and this
    branch made redaction a per-stream-line cost (governed review round 4)."""
    from kew import credential_surface as sandbox_module

    monkeypatch.setattr(sandbox_module, "_CREDENTIAL_FORMS_CACHE", None)
    monkeypatch.setenv("SOME_API_TOKEN", "value-1933-abcdefghijkl")
    calls = []
    real = sandbox_module._build_credential_forms

    def counted():
        calls.append(1)
        return real()

    monkeypatch.setattr(sandbox_module, "_build_credential_forms", counted)

    first = sandbox_module._credential_forms()
    second = sandbox_module._credential_forms()
    assert len(calls) == 1
    assert [name for name, _form in first] == [name for name, _form in second]

    # A changed credential rebuilds them.
    monkeypatch.setenv("SOME_API_TOKEN", "value-1933-mnopqrstuvwx")
    sandbox_module._credential_forms()
    assert len(calls) == 2


def test_governed_round4_no_network_launches_never_carry_proxy_values(
    monkeypatch, tmp_path
):
    """Extraction and verification run with --network none, so a forwarded
    proxy value cannot be used and only puts its credentials in the container
    (governed review round 4, 2026-09-21)."""
    from kew.sandbox import CONTROLLED_PROXY_NAMES, build_checkout_program_argv

    for name in CONTROLLED_PROXY_NAMES:
        monkeypatch.setenv(name, "http://user:secret-1933@proxy.invalid")
    argv = build_checkout_program_argv(
        runtime="docker",
        image="test:image",
        checkout_dir=tmp_path / "checkout",
        slug="round4",
        program=["/bin/true"],
        uid=501,
        gid=20,
    )
    assert argv[argv.index("--network") + 1] == "none"
    environment = [argv[i + 1] for i, arg in enumerate(argv) if arg == "-e"]
    for name in CONTROLLED_PROXY_NAMES:
        assert f"{name}=" in environment
        assert name not in environment
    assert all("secret-1933" not in argument for argument in argv)


def test_governed_round5_a_prose_apostrophe_never_swallows_the_report():
    """Round 5 (2026-09-22): every quote character was read as opening a
    scalar, so an apostrophe in prose made a credential-classified key consume
    the rest of a retained report. A quote opens a value only where a value can
    start."""
    report = (
        "## Run\n"
        "  token: the agent's own key\n"
        "  status: available\n"
        "  duration: 4m12s\n"
        "Done.\n"
    )
    redacted = redact_credentials(report)
    assert "the agent's own key" not in redacted
    for kept in ("  status: available", "  duration: 4m12s", "Done.", "## Run"):
        assert kept in redacted, redacted


def test_governed_round5_an_unclosed_quote_still_owns_what_follows():
    """A value that really does open a quote continues below it."""
    text = 'token: "start of a secret\n  still-secret-1933\nnext: kept\n'
    redacted = redact_credentials(text)
    assert "start of a secret" not in redacted
    assert "still-secret-1933" not in redacted
    assert "next: kept" in redacted


def test_governed_round7_a_credential_bullet_never_deletes_its_peers():
    """Round 7 (2026-09-22): a credential-classified bullet with a closed inline
    value deleted every following prose bullet from a retained report. A key
    written inside a sequence item is a peer of the items around it."""
    report = (
        "## Summary\n"
        "- password: hunter2-1933\n"
        "- We then ran the affected modules.\n"
        "- Duration: 4m12s\n"
        "Done.\n"
    )
    redacted = redact_credentials(report)
    assert "hunter2-1933" not in redacted
    for kept in (
        "- We then ran the affected modules.",
        "- Duration: 4m12s",
        "Done.",
        "## Summary",
    ):
        assert kept in redacted, redacted


def test_governed_round7_a_plain_key_still_owns_the_sequence_below_it():
    """Round 19's contract is unchanged: a plain mapping key followed by a
    keyless sequence item is one uncertain value, and both go."""
    text = "token: true\n- still-secret-1933\nstatus: available\n"
    redacted = redact_credentials(text)
    assert "still-secret-1933" not in redacted
    assert "status: available" in redacted
