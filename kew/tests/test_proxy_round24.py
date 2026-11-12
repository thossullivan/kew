"""Round 24: collision-safe markers and NO_PROXY entry decomposition."""

import json
from functools import partial

import pytest
from test_docs_truthfulness import _read
from test_proxy_round20 import _agent_launch, _container_launch
from test_proxy_round21 import SURFACES, _retain
from test_proxy_round23 import _nested_percent

from kew import sandbox
from kew.runner_fields import redact_authorization_values, runner_text
from kew.transcript_metrics import StreamDerivation, write_derived_evidence

CONTROLS = "## Dispatch Report\nerror_category: transient\nexit code 7\n"
HOSTS = "localhost,.example.com,10.0.0.0/8"


@pytest.fixture(autouse=True)
def _clean_proxies(monkeypatch):
    for name in sandbox._PROXY_BUILD_NAMES:
        monkeypatch.delenv(name, raising=False)


def _retained_report(report, surface, tmp_path, capsys, caplog):
    if surface == "structured":
        payload = {"detail": runner_text(report), "exit_code": 7}
        retained = redact_authorization_values(payload)
        assert retained["exit_code"] == 7
        return retained["detail"]
    if surface == "evidence":
        derivation = StreamDerivation()
        derivation.observe(json.dumps({"type": "result", "result": report}))
        path = write_derived_evidence(tmp_path / "dispatch.log", derivation.evidence())
        return json.loads(path.read_text())["report"]
    return _retain(report, surface, tmp_path, capsys, caplog)


def _observe_marker(*args, original, emitted, **kwargs):
    marker = original(*args, **kwargs)
    emitted.append(marker)
    return marker


@pytest.mark.parametrize("component", ["HTTP", "redacted", "HTTP_PROXY"])
def test_round24_self_collision_uses_safe_marker_from_first_pass(
    monkeypatch, tmp_path, component
):
    monkeypatch.setenv("HTTP_PROXY", f"http://proxy.invalid/{component}")
    emitted = []
    monkeypatch.setattr(
        sandbox.credential_redaction,
        "replacement_marker",
        partial(
            _observe_marker,
            original=sandbox.credential_redaction.replacement_marker,
            emitted=emitted,
        ),
    )
    report = CONTROLS + f"proxy echoed: {component}\nDone."
    with sandbox.proxy_redaction_scope("inherit"):
        text = sandbox.redact_credentials(report)
        derivation = StreamDerivation()
        derivation.observe(json.dumps({"type": "result", "result": report}))
        path = write_derived_evidence(tmp_path / "dispatch.log", derivation.evidence())
        persisted = json.loads(path.read_text())["report"]
    # Baseline emits its own component inside the first generated marker.
    assert emitted and all(component not in marker for marker in emitted)
    assert text == persisted == report.replace(component, "█")


@pytest.mark.parametrize("surface", SURFACES)
def test_round24_http_marker_preserves_every_retained_surface(
    monkeypatch, tmp_path, capsys, caplog, surface
):
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid/HTTP")
    report = CONTROLS + "proxy echoed: HTTP\nDone."
    with sandbox.proxy_redaction_scope("inherit"):
        retained = _retained_report(report, surface, tmp_path, capsys, caplog)
        assert not sandbox.contains_short_credential_value(retained)
    # Baseline retains HTTP or collapses the entire structured report leaf.
    assert retained == report.replace("HTTP", "█")


def test_round24_other_variable_marker_fragment_persists_locally(monkeypatch, tmp_path):
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid/timeout")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid/redacted")
    report = CONTROLS + "proxy echoed: timeout\nDone."
    with sandbox.proxy_redaction_scope("inherit"):
        retained = _retained_report(report, "evidence", tmp_path, None, None)
    assert retained == report.replace("timeout", "<HTTP_PROXY █>")


@pytest.mark.parametrize("surface", SURFACES)
def test_round24_rechecks_short_forms_introduced_by_another_marker(
    monkeypatch, tmp_path, capsys, caplog, surface
):
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid/timeout")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid/HTTP")
    report = CONTROLS + "proxy echoed: timeout\nDone."
    with sandbox.proxy_redaction_scope("inherit"):
        assert not sandbox.contains_short_credential_value(
            sandbox.redact_credentials(report)
        )
        retained = _retained_report(report, surface, tmp_path, capsys, caplog)
    assert retained == report.replace(
        "timeout", "<<HTTPS_PROXY redacted>_PROXY redacted>"
    )


@pytest.mark.parametrize("name", ["NO_PROXY", "no_proxy"])
@pytest.mark.parametrize("surface", SURFACES)
def test_round24_no_proxy_assignment_redacts_every_retained_surface(
    monkeypatch, tmp_path, capsys, caplog, name, surface
):
    report = CONTROLS + f"proxy echoed: s3cr3t\nhosts: {HOSTS}\nDone."
    for hosts in ("", HOSTS, "localhost,.example.com"):
        monkeypatch.setenv(name, f"{hosts},token=s3cr3t".lstrip(","))
        with sandbox.proxy_redaction_scope("inherit"):
            retained = _retained_report(report, surface, tmp_path, capsys, caplog)
        assert retained == report.replace("s3cr3t", f"<{name} redacted>")


@pytest.mark.parametrize("name", ["HTTP_PROXY", "NO_PROXY", "no_proxy"])
@pytest.mark.parametrize(
    "launch", [_agent_launch, _container_launch], ids=["host", "container"]
)
def test_round24_production_launch_persists_complete_sanitized_report(
    monkeypatch, tmp_path, name, launch
):
    secret = "HTTP" if name == "HTTP_PROXY" else "s3cr3t"
    value = "http://proxy.invalid/HTTP" if name == "HTTP_PROXY" else "token=s3cr3t"
    monkeypatch.setenv(name, value)
    launch(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        emitted=(
            f"error_category: transient\nexit code 7\n"
            f"proxy echoed: {secret}\nhosts: {HOSTS}"
        ),
    )
    [path] = tmp_path.glob("*.evidence.json")
    persisted = path.read_text()
    marker = "█" if name == "HTTP_PROXY" else f"<{name} redacted>"
    assert secret not in persisted
    assert json.loads(persisted)["report"] == (
        CONTROLS + f"proxy echoed: {marker}\nhosts: {HOSTS}\nDone."
    )


@pytest.mark.parametrize("name", ["NO_PROXY", "no_proxy"])
@pytest.mark.parametrize(
    ("entry", "spellings"),
    [
        ("opaque!value", ["opaque!value"]),
        ("token=s3cr3t", ["s3cr3t"]),
        (
            "token=" + _nested_percent("s3cr3t", 3),
            ["s3cr3t", _nested_percent("s3cr3t", 2)],
        ),
        (_nested_percent("token=s3cr3t", 3), ["s3cr3t"]),
        ("token=" + _nested_percent("s3cr3t,tail", 2), ["s3cr3t,tail"]),
        ("token=1234", ["1234"]),
        ("hint=xy", ["xy"]),
        ("!xy", ["!xy"]),
    ],
)
def test_round24_no_proxy_decodes_entries_with_existing_provenance_rules(
    monkeypatch, name, entry, spellings
):
    monkeypatch.setenv(name, f"  {HOSTS} , {entry}  ")
    marker = f"<{name} redacted>"
    with sandbox.proxy_redaction_scope("inherit"):
        for spelling in spellings:
            assert sandbox.redact_credentials(f"proxy echoed: {spelling}") == (
                f"proxy echoed: {marker}"
            )
        for host in (*HOSTS.split(","), "*.internal", "example.com:3128", "[::1]", "*"):
            assert host not in dict(
                (form.text, variable) for variable, form in sandbox._credential_forms()
            )
        assert sandbox.redact_credentials(
            "token label; exit code 7; version 12345"
        ) == ("token label; exit code 7; version 12345")


@pytest.mark.parametrize("name", ["NO_PROXY", "no_proxy"])
def test_round24_no_proxy_host_exemptions_and_decode_bound(monkeypatch, name):
    hosts = [
        *HOSTS.split(","),
        "*.internal",
        "example.com:3128",
        "[::1]",
        "2001:db8::/32",
        "*",
    ]
    encoded_host = _nested_percent(".example.com", 2)
    value = ",".join(
        [
            *hosts,
            encoded_host,
            "token=s3cr3t",
            "hint=1234",
            "token=" + _nested_percent("beyond!limit", 5),
        ]
    )
    with sandbox.proxy_redaction_scope("inherit", environment={name: value}):
        forms = {form.text for _, form in sandbox._credential_forms()}
        assert "s3cr3t" in forms
        assert value in forms
        assert not forms.intersection(hosts)
        assert "1234" not in forms
        assert "beyond!limit" not in forms
        assert _nested_percent("beyond!limit", 1) in forms
        assert sandbox.redact_credentials(
            "hosts: " + ",".join(hosts)
        ) == "hosts: " + ",".join(hosts)


def test_round24_marker_scope_is_selected_at_entry_and_restored():
    with sandbox.proxy_redaction_scope(
        "inherit", environment={"HTTP_PROXY": "http://proxy.invalid/HTTP"}
    ):
        context = getattr(sandbox, "_CREDENTIAL_MARKERS", None)
        assert context is not None and context.get()["HTTP_PROXY"] == "█"
        outer = context.get()
        with sandbox.proxy_redaction_scope(
            "inherit", environment={"HTTP_PROXY": "http://proxy.invalid/timeout"}
        ):
            assert context.get()["HTTP_PROXY"] == "<HTTP_PROXY redacted>"
        assert context.get() == outer
    assert context.get() is None


NO_PROXY_RULES = (
    "Split `NO_PROXY` and `no_proxy` on commas and trim whitespace "
    "before bounded decoding.",
    "Host entries include hostnames, optional numeric ports, leading dots, "
    "wildcards, IP addresses, and CIDR entries.",
    "Each decoding stage excludes host entries from component selection.",
    "Parse each remaining entry at its first `=` as `name=value`; "
    "otherwise treat it as a bare value.",
    "The shared opaque-key classifier identifies credential names "
    "at every bounded decoding stage.",
    "Register selected values and bare entries at every stage "
    "using the existing length tiers and provenance rule.",
    "The complete inherited value remains a form, including complete host lists.",
    "Each variable selects its marker at scope entry "
    "using all its forms and their matching rules.",
    "A marker containing its variable's form uses the collision-safe local character "
    "from the first substitution.",
    "After every substitution pass, recheck all short and long forms "
    "under their respective matching rules.",
)


def _assert_round24_configuration(text):
    normalized = " ".join(
        text.split('**`proxy_environment`** (default `"none"`)')[1]
        .split("- **`on_missing_runtime`**")[0]
        .split()
    )
    assert "Other `NO_PROXY` forms are credential-shaped." not in normalized
    for rule in NO_PROXY_RULES:
        assert rule in normalized, rule


def test_round24_configuration_binds_no_proxy_grammar_and_marker_rules():
    _assert_round24_configuration(_read("docs/configuration.md"))


@pytest.mark.parametrize("rule", NO_PROXY_RULES)
def test_round24_configuration_rejects_missing_grammar_or_marker_rule(rule):
    text = _read("docs/configuration.md")
    _assert_round24_configuration(text)
    with pytest.raises(AssertionError):
        _assert_round24_configuration(text.replace(rule, ""))
