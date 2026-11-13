"""Round 13: serialized keys, container representations, and extraction policy."""

import json
from collections import UserDict, UserList
from collections.abc import Mapping, Set
from dataclasses import dataclass
from types import MappingProxyType, SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from support.agent_report import visible_text

from kew.config import DispatchConfig
from kew.progress import display_event, summarize_tool_input
from kew.publication import sanitize_publication_evidence
from kew.publication_extract import extract_in_container
from kew.runner_fields import exception_text, redact_authorization_values
from kew.sandbox import (
    CONTAINER_PROXY_NAMES,
    CONTROLLED_PROXY_NAMES,
    build_extract_argv,
    posture_record_fields,
    redact_credentials,
)
from kew.transcript_metrics import StreamDerivation, write_derived_evidence


@pytest.fixture(autouse=True)
def _clean_proxies(monkeypatch):
    for name in CONTAINER_PROXY_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTP_PROXY", "http://user:ProxySecret123@proxy.invalid")


@pytest.mark.parametrize(
    "header",
    [
        r'{"\u0041uthorization":"opaque-TOPSECRET"}',
        r'{"\x41uthorization":"opaque-TOPSECRET"}',
        r'{"pRoXy-\u0041u\x54hoRization":"opaque-TOPSECRET"}',
        'Authorization = "opaque-TOPSECRET"',
        "proxy-authorization = 'opaque-TOPSECRET'",
        "Authorization=opaque-TOPSECRET",
    ],
    ids=["unicode", "hex", "mixed-case-escapes", "toml", "toml-proxy", "ini"],
)
@pytest.mark.parametrize("surface", ["report", "publication", "display"])
def test_round13_serialized_keys_cross_report_boundaries(
    tmp_path, capsys, header, surface
):
    from kew.dispatch_core import _post_agent_report

    report = f"## Dispatch Report\n{header}\nDone."
    log = tmp_path / "dispatch.log"
    if surface == "display":
        display_event({"type": "result", "result": report})
        retained = capsys.readouterr().err
    else:
        derivation = StreamDerivation()
        derivation.observe(json.dumps({"type": "result", "result": report}))
        path = write_derived_evidence(log, derivation.evidence())
        if surface == "report":
            retained = json.loads(path.read_text())["report"]
        else:
            with patch(
                "kew.dispatch_core.run", return_value=MagicMock(returncode=0)
            ) as post:
                _post_agent_report(
                    "https://github.com/example/repo/pull/1941", log, DispatchConfig()
                )
            argv = post.call_args.args[0]
            retained = visible_text(argv[argv.index("--body") + 1])
    assert "opaque-TOPSECRET" not in retained
    assert "<redacted>" in retained
    assert "Done." in retained


class _CustomMapping(Mapping):
    def __init__(self, values):
        self.values = values

    def __getitem__(self, key):
        return self.values[key]

    def __iter__(self):
        return iter(self.values)

    def __len__(self):
        return len(self.values)

    def __repr__(self):
        return f"CustomMapping({self.values!r})"


class _CustomSet(Set):
    def __init__(self, values):
        self.values = frozenset(values)

    def __contains__(self, value):
        return value in self.values

    def __iter__(self):
        return iter(self.values)

    def __len__(self):
        return len(self.values)


@dataclass(frozen=True)
class _Headers:
    Authorization: object


class _ReprOnly:
    def __repr__(self):
        return "repr=ProxySecret123"


def _surface_text(value, surface, tmp_path):
    if surface == "exception":
        return exception_text(ValueError(value))
    if surface == "tool":
        return summarize_tool_input(value)
    if surface == "publication":
        return sanitize_publication_evidence(value)
    evidence = SimpleNamespace(as_json=lambda: {"payload": value})
    retained = write_derived_evidence(tmp_path / "dispatch.log", evidence).read_text()
    json.loads(retained)  # Redaction must preserve readable JSON.
    return retained


@pytest.mark.parametrize(
    "factory",
    [
        lambda: UserDict({"Authorization": "ProxySecret123"}),
        lambda: MappingProxyType({"Authorization": "ProxySecret123"}),
        lambda: _CustomMapping({"Authorization": "ProxySecret123"}),
        lambda: _Headers("ProxySecret123"),
        _ReprOnly,
    ],
    ids=["userdict", "mapping-proxy", "custom-mapping", "dataclass", "repr-only"],
)
@pytest.mark.parametrize("surface", ["exception", "tool", "evidence", "publication"])
def test_round13_containers_and_final_repr_are_redacted(tmp_path, factory, surface):
    retained = _surface_text(factory(), surface, tmp_path)
    assert "ProxySecret123" not in retained
    assert "redacted" in retained


@pytest.mark.parametrize(
    "key", [r"\u0041uthorization", r"\x41uthorization", r"pRoXy-\u0041u\x54horization"]
)
@pytest.mark.parametrize("surface", ["exception", "tool", "evidence", "publication"])
def test_round13_structured_encoded_keys_are_opaque(tmp_path, key, surface):
    value = UserDict({key: ["opaque-TOPSECRET"], "keep": "public"})
    retained = _surface_text(value, surface, tmp_path)
    assert "opaque-TOPSECRET" not in retained
    assert "<redacted>" in retained
    assert "public" in retained
    assert value[key] == ["opaque-TOPSECRET"]


@pytest.mark.parametrize("container", [UserList, tuple, set, frozenset, _CustomSet])
@pytest.mark.parametrize("surface", ["exception", "tool", "evidence", "publication"])
def test_round13_sequence_and_set_traversal(tmp_path, container, surface):
    value = container([_Headers("opaque-TOPSECRET")])
    retained = _surface_text(value, surface, tmp_path)
    assert "opaque-TOPSECRET" not in retained
    assert "<redacted>" in retained


def test_round13_key_normalization_does_not_decode_values():
    value = r'{"keep":"\u0041uthorization", "\x41uthorization":"opaque"}'
    retained = redact_credentials(value)
    assert "opaque" not in retained
    assert r'"keep":"\u0041uthorization"' in retained
    assert redact_authorization_values({"keep": r"\u0041uthorization"}) == {
        "keep": r"\u0041uthorization"
    }


def _extract_kwargs(tmp_path):
    return dict(
        runtime="docker",
        image="kew-agent:latest",
        checkout_dir=tmp_path / "checkout",
        artifact_dir=tmp_path / "artifact",
        slug="round13",
        base_sha="a" * 40,
    )


@pytest.mark.parametrize("policy", ["inherit", "none"])
def test_round13_extraction_launch_matches_attested_policy(tmp_path, policy):
    config = DispatchConfig()
    config.sandbox.proxy_environment = policy
    posture = posture_record_fields(
        runner="claude-code",
        posture="sandboxed",
        egress_verification="open",
        proxy_environment=config.sandbox.proxy_environment,
    )
    with (
        patch(
            "kew.publication_extract.run_container_with_cpu_cap_retry",
            return_value=MagicMock(returncode=0),
        ) as launch,
        patch("kew.publication_extract.read_artifact", return_value="artifact"),
    ):
        result = extract_in_container(
            **_extract_kwargs(tmp_path),
            proxy_environment=config.sandbox.proxy_environment,
        )
    assert result == "artifact"
    argv = launch.call_args.args[0]
    environment = [argv[i + 1] for i, arg in enumerate(argv) if arg == "-e"]
    # The posture still attests the operator's configured policy for the AGENT
    # container. Extraction carries --network none, so its own argv blanks
    # every name: a value it could never reach would only put its embedded
    # credentials inside it (governed review round 4, 2026-09-21).
    assert posture["proxy_environment"] == policy
    assert (
        "HTTP_PROXY"
        in posture["proxy_variables"]["inherited" if policy == "inherit" else "blanked"]
    )
    for name in CONTROLLED_PROXY_NAMES:
        assert f"{name}=" in environment
        assert name not in environment
    assert all("ProxySecret123" not in arg for arg in argv)


def test_round13_extract_builder_takes_no_policy(tmp_path):
    """Extraction runs with --network none, so there is no proxy policy to
    take: every name is blanked (governed review round 4, 2026-09-21)."""
    from kew.sandbox import CONTROLLED_PROXY_NAMES

    with pytest.raises(TypeError, match="proxy_environment"):
        build_extract_argv(
            **_extract_kwargs(tmp_path),
            uid=501,
            gid=20,
            proxy_environment="inherit",
        )
    argv = build_extract_argv(**_extract_kwargs(tmp_path), uid=501, gid=20)
    environment = [argv[i + 1] for i, arg in enumerate(argv) if arg == "-e"]
    for name in CONTROLLED_PROXY_NAMES:
        assert f"{name}=" in environment
        assert name not in environment


@pytest.mark.parametrize("policy", ["inherit", "none"])
def test_round13_canonical_publication_launches_with_configured_policy(
    tmp_path, policy
):
    from kew.dispatch_core import _push_and_open_pr

    config = DispatchConfig()
    config.project.repo = "example/repo"
    config.sandbox.proxy_environment = policy
    repo = MagicMock(base_sha="a" * 40, path=tmp_path)
    with (
        patch(
            "kew.dispatch_core.publication_remote",
            return_value="https://github.com/example/repo.git",
        ),
        patch("kew.dispatch_core.remote_branch_sha", return_value=""),
        patch("kew.dispatch_core._publication_owner"),
        patch("kew.publication_canonical.provision", return_value=repo),
        patch("kew.publication_canonical._require_branch"),
        patch("kew.publication_canonical._fetch_parents"),
        patch(
            "kew.publication_canonical._promote",
            side_effect=RuntimeError("stop after extraction"),
        ),
        patch(
            "kew.publication_extract.run_container_with_cpu_cap_retry",
            return_value=MagicMock(returncode=0),
        ) as launch,
        patch("kew.publication_extract.read_artifact"),
        pytest.raises(RuntimeError, match="stop after extraction"),
    ):
        _push_and_open_pr(
            tmp_path,
            "fix/1933",
            "1933",
            "Fix",
            config,
            base_sha="a" * 40,
            runtime="docker",
            image="kew-agent:latest",
        )
    argv = launch.call_args.args[0]
    # The extraction container carries --network none under either configured
    # policy, so its argv blanks every name (governed review round 4).
    assert policy in ("inherit", "none")
    assert "HTTP_PROXY" not in argv
    assert "HTTP_PROXY=" in argv


@pytest.mark.parametrize("policy", ["inherit", "none"])
def test_round13_shadow_publication_launches_with_configured_policy(tmp_path, policy):
    from kew.publication_shadow_hook import run_shadow_comparison

    config = DispatchConfig()
    config.project.repo = "example/repo"
    config.publication.shadow = True
    config.publication.publish = "checkout"
    config.sandbox.dangerously_skip_containment = True
    config.sandbox.proxy_environment = policy
    with (
        patch(
            "kew.publication_shadow_hook.resolve_shadow_inputs",
            return_value=("a" * 40, "b" * 40),
        ),
        patch("kew.publication_shadow_hook._project_host", return_value="github.com"),
        patch("kew.publication_shadow.provision_canonical"),
        patch(
            "kew.publication_shadow._destination_state",
            side_effect=RuntimeError("stop after extraction"),
        ),
        patch(
            "kew.publication_extract.run_container_with_cpu_cap_retry",
            return_value=MagicMock(returncode=0),
        ) as launch,
        patch("kew.publication_extract.read_artifact"),
    ):
        outcome = run_shadow_comparison(
            config,
            checkout_dir=tmp_path,
            slug="round13",
            image="kew-agent:latest",
        )
    assert outcome.detail == "RuntimeError: stop after extraction"
    argv = launch.call_args.args[0]
    # The extraction container carries --network none under either configured
    # policy, so its argv blanks every name (governed review round 4).
    assert policy in ("inherit", "none")
    assert "HTTP_PROXY" not in argv
    assert "HTTP_PROXY=" in argv


def test_round13_orchestrator_failed_event_redacts_mapping(tmp_path):
    from kew.orchestrator import run_agent
    from kew.runners import get_runner

    log = tmp_path / "dispatch.log"
    with (
        patch(
            "kew.orchestrator._run_process",
            side_effect=ValueError(UserDict({"Authorization": "ProxySecret123"})),
        ),
        pytest.raises(ValueError),
    ):
        run_agent(
            runner=get_runner("claude-code"),
            prompt="task",
            worktree_dir=tmp_path,
            config=DispatchConfig(),
            issue_num=1933,
            model="claude-sonnet-4-6",
            log_file_path=log,
            emit_events=True,
        )
    failed = json.loads(log.with_suffix(".events.jsonl").read_text().splitlines()[-1])
    assert "ProxySecret123" not in failed["error"]
    assert "<redacted>" in failed["error"]


@pytest.mark.parametrize("policy", ["inherit", "none"])
def test_round13_resolution_publication_launches_with_configured_policy(
    tmp_path, policy
):
    from kew.resolve import _finish_conflict_resolution
    from kew.tracking import DispatchResult

    config = DispatchConfig()
    config.project.repo = "https://github.com/example/repo.git"
    config.sandbox.proxy_environment = policy
    repo = MagicMock(base_sha="b" * 40, path=tmp_path)
    with (
        patch("kew.resolve.remote_branch_sha", side_effect=["b" * 40, "a" * 40]),
        patch("kew.resolve.remote_commit_is_ancestor", return_value=True),
        patch("kew.publication_canonical.provision", return_value=repo),
        patch("kew.publication_canonical._require_branch"),
        patch("kew.publication_canonical._fetch_parents"),
        patch(
            "kew.publication_canonical._promote",
            side_effect=RuntimeError("stop after extraction"),
        ),
        patch(
            "kew.publication_extract.run_container_with_cpu_cap_retry",
            return_value=MagicMock(returncode=0),
        ) as launch,
        patch("kew.publication_extract.read_artifact"),
    ):
        result = _finish_conflict_resolution(
            result=DispatchResult(exit_code=0, issue_num=1933, log_file=None),
            containment=SimpleNamespace(image="kew-agent:latest"),
            host_publication=True,
            publication_mode="canonical",
            worktree_dir=tmp_path,
            conflict_info=SimpleNamespace(base_sha="a" * 40, strategy_used="merge"),
            pr_number=1941,
            issue_number=1933,
            branch_name="fix/1933",
            base_branch="main",
            expected_head_sha="b" * 40,
            config=config,
            store=None,
        )
    # Main's two-axis scheme (#1943) returns a CommandOutcome; the refused
    # publication still ran and failed, so its exit code stays 1.
    assert result.kind == "executed"
    assert result.exit_code == 1
    argv = launch.call_args.args[0]
    # The extraction container carries --network none under either configured
    # policy, so its argv blanks every name (governed review round 4).
    assert policy in ("inherit", "none")
    assert "HTTP_PROXY" not in argv
    assert "HTTP_PROXY=" in argv
