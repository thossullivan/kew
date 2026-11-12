"""Round 20: production launch scopes and value-aware credential retention."""

import io
import json
import subprocess
from dataclasses import asdict, replace
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from support.retained_surfaces import SURFACES as _SURFACES
from support.retained_surfaces import retained as _retained
from test_readiness_probe import _config, _contained_route
from test_review_environment import _checkout, _Daemon, _fake_handle

from kew import readiness_probe as probe
from kew import review_environment, sandbox
from kew.config import DispatchConfig, SwitchyardConfig
from kew.run_store import SqliteRunStore
from kew.runner_fields import redact_authorization_values

PROXY = "https://proxy.invalid/gateway/deadbeef?region=opaque1933"
EMITTED = "proxy rejected deadbeef opaque1933"


@pytest.fixture(autouse=True)
def _clean_proxies(monkeypatch):
    for name in sandbox._PROXY_BUILD_NAMES:
        monkeypatch.delenv(name, raising=False)


def _probe_launch(tmp_path, monkeypatch, emitted, *, failure=False, mutate=False):
    config = _config()
    config.sandbox.proxy_environment = "inherit"
    route = replace(_contained_route(), proxy_environment="inherit")
    workspace = tmp_path / "probe"
    workspace.mkdir()
    monkeypatch.setattr(probe, "create_probe_workspace", lambda _: workspace)
    monkeypatch.setattr(probe, "resolve_probe_route", lambda *a, **kw: route)
    monkeypatch.setattr(
        probe, "check_supporting_access", lambda _: probe.SupportingAccess()
    )
    monkeypatch.setattr(probe.os, "getpgid", lambda _: 1933)
    process = MagicMock(
        stdout=[
            json.dumps(
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": emitted}]},
                }
            )
            + "\n"
        ]
    )
    process.wait.return_value = 0
    process.poll.return_value = 0

    def popen(argv, **kwargs):
        assert "HTTP_PROXY" in argv
        assert kwargs["env"]["HTTP_PROXY"] == PROXY
        if mutate:
            monkeypatch.delenv("HTTP_PROXY")
        if failure:
            raise RuntimeError(emitted)
        return process

    monkeypatch.setattr(probe.subprocess_utils, "popen", popen)
    executions = []
    settle = probe._settle

    def record(store, route, execution, **kwargs):
        executions.append(asdict(execution))
        return settle(store, route, execution, **kwargs)

    monkeypatch.setattr(probe, "_settle", record)
    with SqliteRunStore(str(tmp_path / "probe.db")) as store:
        result = probe.confirm_route_live(store, config)
        stored = store.get_readiness_probe(result.probe_id)
        assert stored is not None
        evidence = probe.confirm_route_live(store, config)
        assert evidence.from_evidence
        return stored.detail, json.dumps(
            [asdict(stored), asdict(result), asdict(evidence), executions], default=str
        )


@pytest.mark.parametrize("failure", [False, True], ids=["reply", "launch-exception"])
@pytest.mark.parametrize("mutate", [False, True], ids=["stable-env", "removed-env"])
def test_round20_readiness_retains_no_inherited_components(
    tmp_path, monkeypatch, failure, mutate
):
    monkeypatch.setenv("HTTP_PROXY", PROXY)
    detail, evidence = _probe_launch(
        tmp_path, monkeypatch, EMITTED, failure=failure, mutate=mutate
    )
    for secret in ("deadbeef", "opaque1933"):
        assert secret not in detail
        assert secret not in evidence


def _provision_launch(tmp_path, monkeypatch, emitted, *, failure=False):
    config = DispatchConfig()
    config.sandbox.proxy_environment = "inherit"
    checkout = _checkout(tmp_path)
    _fake_handle(
        monkeypatch,
        {},
        lines=(f"{emitted}\n",),
        wait_returns=2,
        raise_in_iter=RuntimeError(emitted) if failure else None,
    )
    monkeypatch.setattr(review_environment.subprocess_utils, "run", _Daemon([(0, "")]))
    transcripts = []
    bounded = review_environment._run_bounded

    def record(*args, **kwargs):
        transcript = bounded(*args, **kwargs)
        transcripts.append(transcript)
        return transcript

    monkeypatch.setattr(review_environment, "_run_bounded", record)
    try:
        installed = review_environment.provision_review_environment(
            config,
            image="test:image",
            runner="claude-code",
            checkout_dir=checkout,
            slug="s",
        )
    except review_environment.ProvisioningUnconfirmed as exc:
        assert failure
        return str(exc)
    assert not installed
    assert len(transcripts) == 1
    return repr(transcripts[0])


@pytest.mark.parametrize("failure", [False, True], ids=["output", "stream-exception"])
def test_round20_provisioning_tail_and_warning_are_scoped(
    tmp_path, monkeypatch, caplog, failure
):
    monkeypatch.setenv("HTTP_PROXY", PROXY)
    tail = _provision_launch(tmp_path, monkeypatch, EMITTED, failure=failure)
    assert "opaque1933" not in tail
    assert "opaque1933" not in caplog.text
    if not failure:
        assert "last output:" in caplog.text


@pytest.mark.parametrize("surface", _SURFACES)
@pytest.mark.parametrize("location", ["path", "query", "fragment", "query_name"])
@pytest.mark.parametrize("encoded", [False, True])
def test_round20_five_character_components_are_secret(
    monkeypatch, tmp_path, capsys, surface, location, encoded
):
    component = "abcde"
    value = "".join(f"%{b:02x}" for b in component.encode()) if encoded else component
    suffix = {
        "path": f"/{value}",
        "query": f"/?token={value}",
        "fragment": f"/#token={value}",
        "query_name": f"/?{value}=",
    }[location]
    monkeypatch.setenv("HTTP_PROXY", f"https://proxy.invalid{suffix}")
    with sandbox.proxy_redaction_scope("inherit"):
        retained = _retained(
            f"## Dispatch Report\nproxy rejected token {component}\nDone.",
            surface,
            tmp_path,
            capsys,
        )
    assert component not in retained


@pytest.mark.parametrize("location", ["path", "query", "fragment"])
def test_round20_short_component_replaces_only_its_token(monkeypatch, location):
    suffix = {"path": "/xy", "query": "/?token=xy", "fragment": "/#token=xy"}[location]
    monkeypatch.setenv("HTTP_PROXY", f"https://proxy.invalid{suffix}")
    with sandbox.proxy_redaction_scope("inherit"):
        assert (
            sandbox.redact_credentials("proxy rejected xy in unrelated prose")
            == "proxy rejected <HTTP_PROXY redacted> in unrelated prose"
        )
        assert (
            sandbox.redact_credentials("unrelated prose remains intact")
            == "unrelated prose remains intact"
        )


@pytest.mark.parametrize("surface", _SURFACES)
@pytest.mark.parametrize("key", ["secret_count", "password_limit", "token_budget"])
@pytest.mark.parametrize("spelling", ["json", "plain", "quoted"])
def test_round20_metric_keys_redact_strings(tmp_path, capsys, surface, key, spelling):
    secret = "hunter2hunter2" if key == "secret_count" else "opaque-password"
    payload = {key: secret}
    text = {
        "json": json.dumps(payload),
        "plain": f"{key}: {secret}",
        "quoted": f'{key} = "{secret}"',
    }[spelling]
    retained = _retained(
        f"## Dispatch Report\n{text}\nDone.", surface, tmp_path, capsys
    )
    assert secret not in retained
    assert redact_authorization_values(payload)[key] == "<redacted>"


def test_round20_metric_exemption_validates_complete_numbers():
    for key in ("secret_count", "password_limit", "token_budget", "tokens_total"):
        for value in (
            12,
            -1.5,
            True,
            False,
            None,
            "12",
            "+12",
            "-1.5",
            ".5",
            "1e3",
            "12k",
            "2 MiB",
        ):
            payload = {key: value}
            assert redact_authorization_values(payload) == payload
            assert (
                json.loads(sandbox.redact_credentials(json.dumps(payload))) == payload
            )
        for value in (
            "hunter2hunter2",
            "12password",
            "NaN",
            "Infinity",
            "12\nsecret",
            ["12"],
            {"nested": "12"},
        ):
            assert redact_authorization_values({key: value})[key] == "<redacted>"
    assert redact_authorization_values({"token": "12"}) == {  # nosec B105 - synthetic numeric credential fixture
        "token": "<redacted>"  # nosec B105 - expected redaction marker
    }


def _agent_launch(tmp_path, monkeypatch, emitted):
    from kew import orchestrator
    from kew.runners import get_runner

    process = MagicMock(
        stdout=io.StringIO(
            json.dumps(
                {"type": "result", "result": f"## Dispatch Report\n{emitted}\nDone."}
            )
            + "\n"
        )
    )
    process.poll.return_value = 0
    process.wait.return_value = 0
    process.pid = 1933
    process.returncode = 0
    monkeypatch.setattr(orchestrator, "popen", lambda *a, **kw: process)
    selector = MagicMock()
    selector.select.return_value = [(None, None)]
    monkeypatch.setattr(orchestrator.selectors, "DefaultSelector", lambda: selector)
    log = tmp_path / "agent.log"
    orchestrator.run_agent(
        runner=get_runner("claude-code"),
        prompt="task",
        worktree_dir=tmp_path,
        config=DispatchConfig(),
        issue_num=1933,
        model="claude-sonnet-4-6",
        log_file_path=log,
        emit_events=True,
    )
    return "\n".join(
        path.read_text() for path in tmp_path.glob("agent.*") if path.is_file()
    )


def _container_launch(tmp_path, monkeypatch, emitted):
    from kew import dispatch_sandbox
    from kew.tracking import DispatchResult

    config = DispatchConfig()
    config.sandbox.proxy_environment = "inherit"
    handle = MagicMock()
    handle.__iter__.return_value = iter(
        [
            json.dumps(
                {"type": "result", "result": f"## Dispatch Report\n{emitted}\nDone."}
            )
        ]
    )
    handle.wait.return_value = 1

    def launch_streaming_container(
        launch_config,
        store,
        *,
        run_id,
        log_file,
        image,
        runner,
        checkout_dir,
        slug,
        model,
        transport,
        sidecar_supervisor,
        sidecar_ready,
        admission,
        credential,
        run_kind,
        separate_stderr=False,
        read_only_mounts=(),
    ):
        # Main's admission (#1943) and bounded key (#1957) reach the launcher;
        # a direct Anthropic route mints no key.
        from kew.dispatch_admission import PendingAdmission

        assert launch_config is config
        assert run_id == "round20"
        assert isinstance(admission, PendingAdmission)
        assert credential is None
        assert sidecar_supervisor is None
        return handle

    monkeypatch.setattr(
        dispatch_sandbox, "_launch_streaming_container", launch_streaming_container
    )
    monkeypatch.setattr(
        dispatch_sandbox, "finalize_container_hard_stop", lambda *a, **kw: None
    )
    monkeypatch.setattr(
        dispatch_sandbox, "write_container_evidence", lambda *a, **kw: None
    )
    dispatch_sandbox.run_container_and_stream(
        config,
        None,
        image="test:image",
        runner="claude-code",
        checkout_dir=tmp_path,
        slug="round20",
        run_id="round20",
        model="claude-sonnet-4-6",
        log_file=tmp_path / "container.log",
        run_kind="dispatch",
        result=DispatchResult(exit_code=-1, issue_num=1933, log_file=None),
        # Main's credential-route check (#1957) reads the transport mode.
        inference_transport=SimpleNamespace(
            sidecar=False, model="claude-sonnet-4-6", mode="direct"
        ),
    )
    return "\n".join(path.read_text() for path in tmp_path.glob("container.*"))


def _smoke_launch(tmp_path, monkeypatch, emitted):
    monkeypatch.setattr(sandbox, "detect_runtime", lambda _: "/mock/docker")
    monkeypatch.setattr(sandbox, "_ensure_smoke_image", lambda *a, **kw: None)
    monkeypatch.setattr(sandbox.subprocess_utils, "TOOL_PATHS", {})
    handle = MagicMock()
    handle.__iter__.return_value = iter([emitted])
    handle.wait.return_value = 1
    monkeypatch.setattr(sandbox, "launch_container", lambda _: handle)
    config = DispatchConfig().sandbox
    config.proxy_environment = "inherit"
    return sandbox.run_smoke(config).detail


def _extract_launch(tmp_path, monkeypatch, emitted):
    from kew import publication_extract

    monkeypatch.setattr(
        publication_extract,
        "run_container_with_cpu_cap_retry",
        lambda *a, **kw: subprocess.CompletedProcess(a[0], 1, "", emitted),
    )
    with pytest.raises(publication_extract.ExtractionFailed) as error:
        publication_extract.extract_in_container(
            runtime="docker",
            image="test:image",
            checkout_dir=tmp_path / "checkout",
            artifact_dir=tmp_path / "artifact",
            slug="round20",
            base_sha="a" * 40,
            proxy_environment="inherit",
        )
    return str(error.value)


def _build_launch(tmp_path, monkeypatch, emitted):
    monkeypatch.setattr(
        sandbox.subprocess_utils,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(a[0], 1, emitted, emitted),
    )
    result = sandbox.build_agent_image(
        "docker", "test:image", unpinned=True, proxy_environment="inherit"
    )
    return result.stdout + result.stderr


def _switchyard_launch(tmp_path, monkeypatch, emitted):
    from kew.switchyard_supervisor import (
        SidecarAttemptSupervisor,
        SwitchyardSupervisorError,
    )

    supervisor = SidecarAttemptSupervisor(
        SwitchyardConfig(),
        runtime="docker",
        worker_image_id="sha256:" + "a" * 64,
        proxy_environment="inherit",
    )
    supervisor.proxy_image_id = "sha256:" + "b" * 64
    monkeypatch.setattr(
        sandbox.subprocess_utils,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(a[0], 1, emitted, emitted),
    )
    with pytest.raises(SwitchyardSupervisorError) as error:
        supervisor._create_proxy(())
    return str(error.value)


def _loop_launch(tmp_path, monkeypatch, emitted):
    from kew.loops.verification import CommandCondition

    monkeypatch.setattr(
        sandbox.subprocess_utils,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(a[0], 1, emitted, ""),
    )
    return CommandCondition("false").check(worktree=tmp_path).evidence


def test_round20_launch_path_audit(tmp_path, monkeypatch, caplog):
    """Every table row calls production retention without a test-owned scope."""
    launches = {
        "agent": _agent_launch,
        "contained_agent": _container_launch,
        "review_provisioning": _provision_launch,
        "readiness_probe": _probe_launch,
        "smoke": _smoke_launch,
        "extraction": _extract_launch,
        "sandbox_build": _build_launch,
        "switchyard_creation": _switchyard_launch,
        "loop_verification": _loop_launch,
    }
    leaks = {}
    for name, launch in launches.items():
        directory = tmp_path / name
        directory.mkdir()
        caplog.clear()
        with monkeypatch.context() as child:
            child.setenv("HTTP_PROXY", PROXY)
            retained = str(launch(directory, child, EMITTED)) + caplog.text
        found = [secret for secret in ("deadbeef", "opaque1933") if secret in retained]
        if found:
            leaks[name] = found
    assert not leaks, leaks
