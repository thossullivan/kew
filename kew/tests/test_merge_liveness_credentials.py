"""Credential filtering through ownership registration and container creation."""

import subprocess
from datetime import UTC, datetime, timedelta
from functools import partial
from unittest.mock import Mock

import pytest

from kew import process_groups, sandbox, stop_recovery, subprocess_utils
from kew.credential_issuer import BoundedRunKey


@pytest.fixture
def authorities(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "<operator>")
    monkeypatch.setenv("OPENROUTER_PROVISIONING_KEY", "<management>")
    monkeypatch.setenv("KEW_ISSUED_RUN_KEY_HASH", "a" * 64)
    return BoundedRunKey("<issued>", "a" * 64, 3, "2099-01-01T00:00:00Z")


def _assert_filtered(environment, credential=None):
    assert "OPENROUTER_PROVISIONING_KEY" not in environment
    assert "KEW_ISSUED_RUN_KEY_HASH" not in environment
    if credential is None:
        assert "OPENROUTER_API_KEY" not in environment
    else:
        assert environment["OPENROUTER_API_KEY"] == credential.secret


@pytest.mark.parametrize("gated", [False, True])
@pytest.mark.parametrize("issued", [False, True])
def test_launch_gate_keeps_only_explicit_issuance(
    monkeypatch, authorities, gated, issued
):
    launch = Mock()
    monkeypatch.setattr(subprocess_utils.subprocess, "Popen", launch)
    monkeypatch.setattr(subprocess_utils, "_popen_gated", launch)
    monkeypatch.setitem(subprocess_utils.TOOL_PATHS, "agent", "/fixture/agent")
    credential = authorities if issued else None
    subprocess_utils.popen(
        ["agent"], gate=gated, start_new_session=True, credential=credential
    )
    _assert_filtered(launch.call_args.kwargs["env"], credential)


def _creation_response(cidfile, refusals, command, **kwargs):
    if refusals:
        return subprocess.CompletedProcess(
            command,
            refusals.pop(),
            "",
            "docker: Error response from daemon: "
            "range of CPUs is from 0.01 to 2.00, as there are only 2 CPUs available",
        )
    cidfile.write_text("c" * 64)
    return subprocess.CompletedProcess(command, 0, "c" * 64, "")


@pytest.mark.parametrize("refusal_code", [1, 125])
def test_create_retry_injects_issued_credential(
    tmp_path, monkeypatch, authorities, refusal_code
):
    cidfile = tmp_path / "worker.cid"
    runtime = Mock(side_effect=partial(_creation_response, cidfile, [refusal_code]))
    monkeypatch.setitem(subprocess_utils.TOOL_PATHS, "docker", "/fixture/docker")
    monkeypatch.setattr(subprocess_utils.subprocess, "run", runtime)
    monkeypatch.setattr(sandbox, "_daemon_cpu_count", Mock(return_value=2))
    argv = [
        "docker",
        "run",
        "--cpus",
        "4",
        "--name",
        "kew-1944",
        "--cidfile",
        str(cidfile),
        "-e",
        "OPENROUTER_API_KEY",
        "image",
    ]
    assert sandbox.create_registered_container(argv, credential=authorities) == "c" * 64
    assert runtime.call_count == 2
    for call in runtime.call_args_list:
        assert call.args[0][1] == "create"
        _assert_filtered(call.kwargs["env"], authorities)
        assert "<issued>" not in call.args[0]


@pytest.mark.parametrize("operation", ["start", "exec"])
def test_container_client_registration_keeps_filter(
    tmp_path, monkeypatch, authorities, operation
):
    launch = Mock(return_value=Mock(pid=100002))
    registration = Mock()
    monkeypatch.setitem(subprocess_utils.TOOL_PATHS, "docker", "/fixture/docker")
    monkeypatch.setattr(subprocess_utils, "_popen_gated", launch)
    sandbox.launch_container(
        ["docker", operation, "c" * 64],
        container_name="kew-1944",
        container_id="c" * 64,
        checkout_dir=tmp_path,
        credential=authorities,
        on_spawn=registration,
    )
    registration.assert_called_once_with(launch.return_value)
    assert launch.call_args.kwargs["start_new_session"] is True
    _assert_filtered(launch.call_args.kwargs["env"], authorities)


@pytest.mark.parametrize("operation", ["identity", "inspect", "labels"])
def test_store_level_children_filter_authority(monkeypatch, authorities, operation):
    runtime = Mock(
        return_value=subprocess.CompletedProcess([], 0, '{"labels": []}', "")
    )
    monkeypatch.setattr(subprocess, "run", runtime)
    monkeypatch.setattr(stop_recovery, "_tool", lambda name: "/fixture/" + name)
    if operation == "identity":
        process_groups.run(["ps", "-axo", "pid="])
    elif operation == "inspect":
        stop_recovery.container_state("docker", "c" * 64, "kew-1944")
    else:
        stop_recovery.reset_labels(
            1944,
            {
                "tracker": "github",
                "repo": "example/project",
                "ready": "ready",
                "in_progress": "in-progress",
                "agent_working": "working",
            },
        )
    assert runtime.called
    for call in runtime.call_args_list:
        _assert_filtered(call.kwargs["env"])


@pytest.mark.parametrize("alive", [True, None, False])
def test_pending_owner_verdict_cannot_bypass_main_recovery(
    tmp_path, monkeypatch, authorities, alive
):
    from kew import credential_issuer, credential_orphans, run_credentials
    from kew.credential_intents import record_issuance_intent, settle_issued_intent
    from kew.run_store import RunRecord, SqliteRunStore

    now = datetime.now(UTC)
    monkeypatch.setattr(process_groups, "owner_alive", Mock(return_value=alive))
    monkeypatch.setattr(
        process_groups,
        "owner_snapshot",
        Mock(return_value=process_groups.OwnerSnapshot(None)),
    )
    revoke = Mock()
    monkeypatch.setattr(run_credentials, "_revoke", revoke)
    with SqliteRunStore(":memory:", project_root=tmp_path) as store:
        record_issuance_intent(
            store,
            "live",
            authorities.limit_usd,
            datetime.fromisoformat(authorities.expires_at),
            now,
        )
        settle_issued_intent(store, "live")
        store.save(
            RunRecord(
                run_id="live",
                issue_num=1944,
                branch="agent/1944",
                status="pending",
                exit_code=-1,
                started_at=now - timedelta(hours=1),
                pgid=100001,
                owner_pid=100001,
            )
        )
        item = {
            "name": credential_issuer.run_key_name(store.read_ledger_id(), "live"),
            "hash": authorities.key_hash,
            "limit": authorities.limit_usd,
            "expires_at": authorities.expires_at,
            "created_at": (now - timedelta(hours=1)).isoformat(),
        }
        monkeypatch.setattr(
            credential_issuer, "list_run_keys", Mock(return_value=[item])
        )
        credential_orphans.sweep_issuer_orphans(store, now=now, seen=set())
        revoke.assert_not_called()
        from kew.run_store_liveness import prepare_admission

        prepare_admission(store)
        credential_orphans.sweep_issuer_orphans(store, now=now, seen=set())
        if alive is False:
            assert store.get("live").status == "killed"
            revoke.assert_called_once()
        else:
            assert store.get("live").status == "pending"
            revoke.assert_not_called()
