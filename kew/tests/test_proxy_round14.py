"""Round 14 regressions for round 16 of the proxy containment review."""

import json
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from support.agent_report import visible_text

from kew import sandbox
from kew.config import DispatchConfig
from kew.progress import display_event
from kew.publication_canonical import CanonicalNoChange, publish_checkout
from kew.runner_fields import exception_text, runner_text
from kew.transcript_metrics import StreamDerivation, write_derived_evidence


@pytest.fixture(autouse=True)
def _clean_proxies(monkeypatch):
    for name in sandbox.CONTAINER_PROXY_NAMES:
        monkeypatch.delenv(name, raising=False)


def _scope(policy, **kwargs):
    # Exercise behavior against the committed baseline before scopes existed.
    factory = getattr(sandbox, "proxy_redaction_scope", None)
    return nullcontext() if factory is None else factory(policy, **kwargs)


def _retained(report, surface, tmp_path, capsys):
    from kew.dispatch_core import _post_agent_report

    if surface == "display":
        display_event({"type": "result", "result": runner_text(report)})
        return capsys.readouterr().err
    if surface == "exception":
        return exception_text(ValueError(report))
    log = tmp_path / "dispatch.log"
    if surface == "evidence":
        evidence = SimpleNamespace(as_json=lambda: {"payload": report})
        path = write_derived_evidence(log, evidence)
        return json.loads(path.read_text())["payload"]
    derivation = StreamDerivation()
    derivation.observe(json.dumps({"type": "result", "result": report}))
    path = write_derived_evidence(log, derivation.evidence())
    if surface == "report":
        return json.loads(path.read_text())["report"]
    with patch("kew.dispatch_core.run", return_value=MagicMock(returncode=0)) as post:
        _post_agent_report(
            "https://github.com/example/repo/pull/1941", log, DispatchConfig()
        )
    argv = post.call_args.args[0]
    return visible_text(argv[argv.index("--body") + 1])


@pytest.mark.parametrize(
    "header",
    [
        r'"\U00000041uthorization" = "opaque-TOPSECRET"',
        r'"\U00000041uthorization": "opaque-TOPSECRET"',
        r'"pRoXy-\u0041\U00000075\x74horization" = "opaque-TOPSECRET"',
        'b"Authorization": b"opaque-TOPSECRET"',
        "b'proxy-authorization': b'opaque-TOPSECRET'",
        r"""b"\x41uthorization": b'opaque-TOPSECRET\x22\ncontinued' """,
    ],
    ids=["toml-U", "yaml-U", "mixed", "bytes-double", "bytes-single", "bytes-hex"],
)
@pytest.mark.parametrize(
    "surface", ["display", "exception", "evidence", "report", "publication"]
)
def test_round14_authorization_keys_cross_all_boundaries(
    header, surface, tmp_path, capsys
):
    retained = _retained(
        f"## Dispatch Report\n{header}\nDone.", surface, tmp_path, capsys
    )
    assert "opaque-TOPSECRET" not in retained
    assert "continued" not in retained
    assert "<redacted>" in retained
    assert "Done." in retained


@pytest.mark.parametrize(
    "surface", ["display", "exception", "evidence", "report", "publication"]
)
def test_round14_unrelated_bytes_keys_remain_readable(surface, tmp_path, capsys):
    header = r'''b"\x58-Trace-Key": b"keep-opaque\U00000041\u0042\x43"'''
    retained = _retained(
        f"## Dispatch Report\n{header}\nDone.", surface, tmp_path, capsys
    )
    assert header in retained
    assert "redacted" not in retained


@pytest.mark.parametrize(
    "key",
    [
        b"Authorization",
        b"proxy-authorization",
        rb"\x41uthorization",
        r"\U00000041uthorization",
    ],
)
@pytest.mark.parametrize("surface", ["exception", "evidence", "display"])
def test_round14_structured_bytes_keys_redact_opaque_values(
    key, surface, tmp_path, capsys
):
    value = {key: b"opaque-TOPSECRET\\x22\\ncontinued", b"X-Trace-Key": "keep-opaque"}
    retained = _retained(value, surface, tmp_path, capsys)
    retained = json.dumps(retained) if isinstance(retained, dict) else retained
    assert "opaque-TOPSECRET" not in retained
    assert "continued" not in retained
    assert "<redacted>" in retained
    assert "keep-opaque" in retained
    assert value[key] == b"opaque-TOPSECRET\\x22\\ncontinued"


def test_round14_decoder_leaves_values_and_invalid_escapes_untouched():
    text = r""""\U00000041uthorization" = "opaque"\n"""
    assert "opaque" not in sandbox.redact_credentials(text)
    unrelated = r'''b"X-Trace-Key": b"\U00000041\u0042\x43"'''
    assert sandbox.redact_credentials(unrelated) == unrelated
    invalid = r'''"\UFFFFFFFFuthorization": "retained"'''
    assert sandbox.redact_credentials(invalid) == invalid
    assert not sandbox.is_authorization_key(b"\xffAuthorization")
    assert not sandbox.is_authorization_key(b"\xff-Authorization")


def _publish_args(tmp_path):
    return dict(
        checkout_dir=tmp_path / "checkout",
        remote="https://github.com/example/repo.git",
        branch="fix/1933",
        parents=("a" * 40,),
        base_sha="a" * 40,
        message="Remediate proxy containment",
        identity=SimpleNamespace(name="test", email="test@example.invalid"),
        output_guard=None,
        scratch=tmp_path / "scratch",
    )


@pytest.mark.parametrize(
    "partial",
    [
        {"runtime": "docker", "image": "test:image"},
        {"runtime": "docker"},
        {"image": "test:image"},
    ],
)
def test_round14_contained_publication_requires_policy_before_provision(
    tmp_path, partial
):
    with (
        patch("kew.publication_canonical.provision") as provision,
        pytest.raises(
            TypeError, match="^contained extraction requires proxy_environment$"
        ),
    ):
        publish_checkout(**_publish_args(tmp_path), **partial)
    provision.assert_not_called()


@pytest.mark.parametrize(
    "contained", [False, True], ids=["host-omission", "contained-inherit"]
)
def test_round14_publication_host_omission_and_inherit_forwarding(
    tmp_path, monkeypatch, contained
):
    monkeypatch.setenv("HTTP_PROXY", "http://user:LongProxySecret@proxy.invalid")
    repo = MagicMock(path=tmp_path, base_sha="a" * 40)
    promoted = MagicMock(
        changed_paths=(), entry_count=0, deletion_count=0, tree_sha="b" * 40
    )
    args = _publish_args(tmp_path)
    if contained:
        args.update(runtime="docker", image="test:image", proxy_environment="inherit")
    with (
        patch("kew.publication_canonical.provision", return_value=repo),
        patch("kew.publication_canonical._require_branch"),
        patch("kew.publication_canonical._fetch_parents"),
        patch("kew.publication_canonical._promote", return_value=promoted),
        patch("kew.publication_canonical._empty_evidence_artifact"),
        patch(
            "kew.publication_canonical._base_blob_reader",
            return_value=({}, MagicMock(), {}),
        ),
        patch("kew.publication_canonical.extract_on_host") as host,
        patch("kew.publication_extract.read_artifact"),
        patch(
            "kew.publication_extract.run_container_with_cpu_cap_retry",
            return_value=MagicMock(returncode=0),
        ) as launch,
    ):
        result = publish_checkout(**args)
    assert isinstance(result, CanonicalNoChange)
    assert host.call_count == int(not contained)
    assert launch.call_count == int(contained)
    if contained:
        argv = launch.call_args.args[0]
        # Extraction carries --network none: the configured policy is attested
        # but never reaches its argv (governed review round 4, 2026-09-21).
        assert "HTTP_PROXY=" in argv
        assert "HTTP_PROXY" not in argv
        assert "LongProxySecret" not in " ".join(argv)


@pytest.mark.parametrize("policy", ["none", "inherit"])
def test_round14_short_proxy_preserves_prose_and_replaces_only_token(
    monkeypatch, policy
):
    monkeypatch.setenv("NO_PROXY", "a")
    text = "status: passed; artifact available"
    with _scope(policy):
        retained = sandbox.redact_credentials(text)
        token = sandbox.redact_credentials("found a match")
    assert retained == text
    assert token == (
        "found a match" if policy == "none" else "found <NO_PROXY redacted> match"
    )


@pytest.mark.parametrize(
    "value", ["localhost,.example.invalid,10.0.0.0/8,[::1],*.internal:80", "a"]
)
def test_round14_none_ignores_only_bare_host_lists(monkeypatch, value):
    monkeypatch.setenv("NO_PROXY", value)
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:3128")
    text = f"status: passed; artifact available; {value}; http://proxy.invalid:3128"
    with _scope("none"):
        assert sandbox.redact_credentials(text) == text


@pytest.mark.parametrize(
    "name,value",
    [
        ("HTTP_PROXY", "http://user:LongProxySecret@proxy.invalid"),
        ("NO_PROXY", "user:LongProxySecret@proxy.invalid"),
        ("HTTP_PROXY", "http://proxy.invalid/?token=LongProxySecret"),
        ("HTTP_PROXY", "http://user:LongProxySecret@[bad"),
    ],
)
def test_round14_none_still_redacts_credential_shaped_host_proxies(
    monkeypatch, name, value
):
    monkeypatch.setenv(name, value)
    with _scope("none"):
        retained = sandbox.redact_credentials(f"before {value} after")
    assert "LongProxySecret" not in retained
    assert retained == f"before <{name} redacted> after"


def test_round14_repeated_proxy_preserves_separators_and_bounds_output(monkeypatch):
    proxy = "http://user:LongProxySecret@proxy.invalid"
    monkeypatch.setenv("HTTP_PROXY", proxy)
    with _scope("inherit"):
        assert sandbox.redact_credentials("; ".join([proxy] * 9)) == "; ".join(
            ["<HTTP_PROXY redacted>"] * 9
        )
        retained = sandbox.redact_credentials("z" * 65_535 + " LongProxySecret")
    assert "LongProxySecret" not in retained
    assert len(retained) <= 65_536


def test_round14_scope_snapshots_received_values_and_resets(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "received-host.invalid")
    with _scope("inherit"):
        monkeypatch.setenv("NO_PROXY", "different-host.invalid")
        assert "received-host.invalid" not in runner_text("received-host.invalid")
        with _scope("none"):
            assert runner_text("received-host.invalid") == "received-host.invalid"
        assert "received-host.invalid" not in runner_text("received-host.invalid")
    assert runner_text("received-host.invalid") == "received-host.invalid"


def test_round14_short_proxy_keeps_evidence_json_valid(monkeypatch, tmp_path):
    monkeypatch.setenv("NO_PROXY", "a")
    evidence = SimpleNamespace(
        as_json=lambda: {
            "report": "status: passed; artifact available",
            "detail": "found a match",
        }
    )
    with _scope("inherit"):
        path = write_derived_evidence(tmp_path / "dispatch.log", evidence)
    retained = json.loads(path.read_text())
    assert retained["report"] == "status: passed; artifact available"
    assert retained["detail"] == "found <NO_PROXY redacted> match"


def test_round14_marker_collision_cannot_reintroduce_proxy_value(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "R")
    with _scope("inherit"):
        assert sandbox.redact_credentials("Result ready") == "Result ready"
        assert sandbox.redact_credentials("Result R ready") == (
            "Result <NO_PROXY redacted> ready"
        )


@pytest.mark.parametrize("policy", ["none", "inherit"])
def test_round14_container_stream_uses_launch_redaction_policy(
    tmp_path, monkeypatch, policy
):
    from kew.dispatch_sandbox import run_container_and_stream
    from kew.tracking import DispatchResult

    monkeypatch.setenv("NO_PROXY", "a")
    config = DispatchConfig()
    config.sandbox.proxy_environment = policy
    seen = []
    # Main's credential-route check (#1957) reads the transport mode; a direct
    # non-OpenRouter route mints no key and launches through the same stream.
    transport = SimpleNamespace(sidecar=False, model="claude-sonnet-4-6", mode="direct")
    text = "status: passed; artifact available; a"

    def stream(*args, **kwargs):
        seen.append(runner_text(text))
        return 0

    with (
        patch(
            "kew.dispatch_sandbox._launch_streaming_container", return_value=MagicMock()
        ),
        patch("kew.dispatch_core._stream_sandbox_log", side_effect=stream),
        patch("kew.dispatch_sandbox.finalize_container_hard_stop"),
        patch("kew.dispatch_sandbox.write_container_evidence"),
    ):
        code = run_container_and_stream(
            config,
            None,
            image="test:image",
            runner="claude-code",
            checkout_dir=tmp_path,
            slug="round14",
            run_id="round14",
            model=transport.model,
            log_file=tmp_path / "dispatch.log",
            run_kind="dispatch",
            result=DispatchResult(exit_code=-1, issue_num=1933, log_file=None),
            inference_transport=transport,
        )
    assert code == 0
    assert seen == [
        text if policy == "none" else text.removesuffix("a") + "<NO_PROXY redacted>"
    ]
    assert runner_text(text) == text


@pytest.mark.parametrize("policy", ["none", "inherit"])
def test_round14_extraction_exception_is_sanitized_before_scope_release(
    tmp_path, monkeypatch, policy
):
    from kew.publication_extract import ExtractionFailed, extract_in_container

    monkeypatch.setenv("NO_PROXY", "a")
    text = "status: passed; artifact available; a"
    with (
        patch(
            "kew.publication_extract.run_container_with_cpu_cap_retry",
            return_value=MagicMock(returncode=1, stderr=text),
        ),
        pytest.raises(ExtractionFailed) as error,
    ):
        extract_in_container(
            runtime="docker",
            image="test:image",
            checkout_dir=tmp_path / "checkout",
            artifact_dir=tmp_path / "artifact",
            slug="round14",
            base_sha="a" * 40,
            proxy_environment=policy,
        )
    if policy == "none":
        assert text in str(error.value)
    else:
        assert text.removesuffix("a") + "<NO_PROXY redacted>" in str(error.value)
    assert runner_text(text) == text


@pytest.mark.parametrize("policy", ["none", "inherit"])
def test_round14_orchestrator_scopes_retained_fields(tmp_path, monkeypatch, policy):
    from kew.orchestrator import run_agent
    from kew.runners import get_runner

    monkeypatch.setenv("NO_PROXY", "a")
    config = DispatchConfig()
    config.sandbox.proxy_environment = policy
    text = "status: passed; artifact available; a"
    seen = []

    def process(*args, **kwargs):
        seen.append(runner_text(text))
        return None, "claude-sonnet-4-6", 0

    with (
        patch("kew.orchestrator._run_process", side_effect=process),
        patch("kew.orchestrator._contained_prompt_handoff", return_value=nullcontext()),
        patch("kew.orchestrator._finalize_agent_result"),
    ):
        run_agent(
            runner=get_runner("claude-code"),
            prompt="task",
            worktree_dir=tmp_path,
            config=config,
            issue_num=1933,
            model="claude-sonnet-4-6",
            log_file_path=tmp_path / "dispatch.log",
            contained=SimpleNamespace(argv=["docker"]),
        )
    assert seen == [
        text if policy == "none" else text.removesuffix("a") + "<NO_PROXY redacted>"
    ]
    assert runner_text(text) == text


@pytest.mark.parametrize(
    "password,payload",
    [
        ("123456", '{"value":123456,"ok":true}'),
        ("a", '{"Authorization":"opaque"}'),
    ],
)
def test_round14_json_values_remain_safe_and_parseable(monkeypatch, password, payload):
    monkeypatch.setenv("HTTP_PROXY", f"http://user:{password}@proxy.invalid")
    retained = json.loads(sandbox.redact_credentials(payload))
    value = next(iter(retained.values()))
    assert isinstance(value, str)
    assert value == (
        "<redacted>" if "Authorization" in retained else "<HTTP_PROXY redacted>"
    )
    assert "opaque" not in value
