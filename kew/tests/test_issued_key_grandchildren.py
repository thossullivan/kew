"""Round-six credential boundaries without a container runtime."""

import subprocess
from pathlib import Path
from unittest.mock import Mock

from test_liveness_owner import process_table as process_table

from kew import dispatch_core, dispatch_sandbox, sandbox, subprocess_utils
from kew.config import DispatchConfig
from kew.graph.driver.credentials import issue_driver_key
from kew.run_credentials import bounded_run_credentials
from kew.run_store import SqliteRunStore


def test_round6_container_receives_key_but_runtime_helper_does_not(
    tmp_path, monkeypatch, process_table
):
    from kew import credential_issuer

    owner, client = 100001, 100002
    process_table.members[owner] = (owner, "S", "python3", "kew dispatch")
    process_table.members[client] = (client, "S", "sh", "kew-spawn-gate")
    process_table.parents[client] = owner
    monkeypatch.setattr(
        "kew.process_groups.exclusive_owner_pid", Mock(return_value=owner)
    )
    monkeypatch.setenv("OPENROUTER_PROVISIONING_KEY", "management-secret-1944")
    monkeypatch.setattr(
        credential_issuer,
        "mint_bounded_run_key",
        lambda run_id, limit, expiry, *, ledger_id, on_malformed: (
            credential_issuer.BoundedRunKey(
                "bounded-secret-1944", "a" * 64, limit, expiry.isoformat()
            )
        ),
    )
    config = DispatchConfig()
    with SqliteRunStore(str(tmp_path / "runs.db"), project_root=tmp_path) as store:
        key = issue_driver_key(store, config, "container-run", "1944")
        monkeypatch.delenv("OPENROUTER_PROVISIONING_KEY")
        monkeypatch.setenv("OPENROUTER_API_KEY", key.secret)
        monkeypatch.setenv("KEW_ISSUED_RUN_KEY_HASH", key.key_hash)
        _round6_launch_container(store, config, key, tmp_path, monkeypatch)


def _round6_launch_container(store, config, key, tmp_path, monkeypatch):
    from kew.inference_transport import resolve_inference_transport
    from kew.runners import get_runner

    dispatch_sandbox._record_sandbox_start(
        store,
        run_id="container-run",
        issue_num=1944,
        item_id="1944",
        branch="agent/1944",
        title="fixture",
        model="openrouter/test",
        runner_name="opencode",
        log_file=tmp_path / "container.log",
    )
    spawn = Mock(return_value=Mock(pid=100002))
    helper = Mock(side_effect=_round6_control)
    monkeypatch.setattr(subprocess_utils, "_popen_gated", spawn)
    monkeypatch.setitem(subprocess_utils.TOOL_PATHS, "docker", "/mock/docker")
    monkeypatch.setattr(subprocess_utils.subprocess, "Popen", spawn)
    monkeypatch.setattr(subprocess_utils.subprocess, "run", helper)
    monkeypatch.setattr(dispatch_core, "_launch_container", sandbox.launch_container)
    transport = resolve_inference_transport(
        config, get_runner("opencode"), "openrouter/test"
    )
    with bounded_run_credentials(
        run_id="container-run",
        config=config,
        store=store,
        model=transport.model,
        credential_names=("OPENROUTER_API_KEY",),
        timeout_seconds=60,
    ) as inherited:
        dispatch_sandbox._launch_streaming_container(
            store=store,
            config=config,
            run_id="container-run",
            log_file=tmp_path / "container.log",
            image="sha256:" + "b" * 64,
            runner="opencode",
            checkout_dir=tmp_path,
            slug="1944",
            model=transport.model,
            transport=transport,
            sidecar_supervisor=None,
            sidecar_ready=None,
            credential=inherited,
            run_kind="dispatch",
        )
    # The launch also reads which daemon it is on (`docker info`) before the
    # create; that read is a runtime helper too, so it gets no key either.
    [create] = [c for c in helper.call_args_list if c.args[0][1] == "create"]
    argv = create.args[0]
    assert create.kwargs["env"]["OPENROUTER_API_KEY"] == key.secret
    for call in helper.call_args_list:
        if call is not create:
            assert "OPENROUTER_API_KEY" not in call.kwargs["env"]
            assert "KEW_ISSUED_RUN_KEY_HASH" not in call.kwargs["env"]
    assert spawn.call_args.args[0] == [
        "/mock/docker",
        "start",
        "--attach",
        "--interactive",
        "c" * 64,
    ]
    assert "OPENROUTER_API_KEY" in argv
    assert argv[argv.index("OPENROUTER_API_KEY") - 1] == "-e"
    assert key.secret not in str(argv)
    env = spawn.call_args.kwargs["env"]
    assert env["OPENROUTER_API_KEY"] == key.secret
    assert "KEW_ISSUED_RUN_KEY_HASH" not in env
    assert "OPENROUTER_PROVISIONING_KEY" not in env
    sandbox.inspect_image_id("docker", "test-image")
    helper_env = helper.call_args.kwargs["env"]
    assert "OPENROUTER_API_KEY" not in helper_env
    assert "KEW_ISSUED_RUN_KEY_HASH" not in helper_env


def _round6_control(argv, **kwargs):
    if argv[1] == "create":
        Path(argv[argv.index("--cidfile") + 1]).write_text("c" * 64)
    return subprocess.CompletedProcess(argv, 0, stdout="false")
