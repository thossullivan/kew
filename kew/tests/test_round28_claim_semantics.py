"""Ownership and semantic claim regressions against 4fc1df4f."""

from __future__ import annotations

import json
from contextlib import suppress
from copy import deepcopy
from functools import partial
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from dispatch_owner_fixture import admission_owner as admission_owner
from test_linear_tracker import _ok
from test_round21_admission_owner import EPIC, _config
from test_round26_claim_windows import WindowTransport, _context
from test_round27_claim_truth import (
    RecordingHandle,
    _claim,
    _launch_scene,
    _stream_container,
)
from test_round27_claim_truth import (
    claim_scene as claim_scene,
)

from kew import dispatch_core as core
from kew import dispatch_sandbox as sandbox
from kew.claim_retention import LINEAR_CLAIM_RECOVERY
from kew.linear.client import LinearApiError
from kew.sandbox import ContainedExecution, ContainerHandle, KillStatus


def _interrupt_launch(append, failure, **kwargs):
    if kwargs["event"] == "linear_claim_launched":
        raise failure
    return append(**kwargs)


def _raise_launch_failure(failure):
    raise failure


@pytest.mark.parametrize(
    "kind", [KeyboardInterrupt, SystemExit, GeneratorExit, OSError]
)
def test_round28_marker_unwind_kills_then_waits(
    claim_scene, tmp_path, monkeypatch, kind
):
    store, remote = claim_scene
    handle = RecordingHandle(store)
    ctx = _launch_scene(store, remote, tmp_path, monkeypatch, handle)
    failure = kind("marker unavailable")
    with pytest.raises(BaseException) as caught:
        _stream_container(store, tmp_path, partial(_raise_launch_failure, failure))
    assert handle.calls[:2] == ["kill", "wait"], "Unwind must kill then wait"
    assert caught.value is failure, "Propagate the original exception unchanged"
    prelaunch = Mock()
    monkeypatch.setattr(core, "_record_prelaunch_refusal", prelaunch)
    core._launch_failed(ctx, caught.value, Mock())
    assert store.get("child").error_category == "sandbox_launch_error"
    prelaunch.assert_not_called()


@pytest.mark.parametrize("failure_mode", ["kill-refused", "wait-timeout"])
def test_round28_interrupt_retains_pending_ownership(
    claim_scene, tmp_path, monkeypatch, failure_mode
):
    store, remote = claim_scene
    handle = RecordingHandle(
        store,
        kill_status=KillStatus.REFUSED
        if failure_mode == "kill-refused"
        else KillStatus.ACCEPTED,
        wait_fails=failure_mode == "wait-timeout",
    )
    ctx = _launch_scene(store, remote, tmp_path, monkeypatch, handle)
    failure = KeyboardInterrupt("marker unavailable")
    monkeypatch.setattr(
        store,
        "append_audit_event_once",
        partial(_interrupt_launch, store.append_audit_event_once, failure),
    )
    with pytest.raises(KeyboardInterrupt) as caught:
        _stream_container(
            store, tmp_path, partial(core._mark_claim_launch_started, ctx)
        )
    assert handle.calls == ["kill", "wait"], "Unconfirmed cleanup must retain identity"
    assert caught.value is failure
    with pytest.raises(KeyboardInterrupt) as propagated:
        core._launch_failed(ctx, caught.value, Mock())
    assert propagated.value is failure
    assert ctx.container_cleanup_pending
    assert store.get("child").status == "pending"
    assert store.list_epic_reservations(EPIC)


class MismatchedClaimTransport(WindowTransport):
    def __init__(self, remote, mismatch, verification="unresolved"):
        super().__init__(remote, "Doing")
        self.mismatch = mismatch
        self.verification = verification
        self.verifications = 0

    def __call__(self, url, payload, headers):
        if "IssueClaimState" in json.loads(payload)["query"]:
            self.verifications += 1
            issue = self.remote_issue()
            if self.verification == "unresolved":
                issue = None
            elif self.verification == "wrong-issue":
                issue["identifier"] = "SFD-999"
            elif self.verification == "wrong-assignee":
                issue["assignee"] = {"id": "another-viewer"}
            return _ok({"issue": issue})
        return super().__call__(url, payload, headers)

    def apply(self, fields):
        super().apply(fields)
        issue = deepcopy(self.remote_issue())
        issue["id"] = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        if self.mismatch == "identifier":
            issue["identifier"] = "SFD-999"
        elif self.mismatch == "state":
            issue["state"]["id"] = "another-state"
        elif self.mismatch == "assignee":
            issue["assignee"]["id"] = "another-viewer"
        elif self.mismatch == "missing":
            issue = None
        return _ok({"issueUpdate": {"success": True, "issue": issue}})


@pytest.mark.parametrize("mismatch", ["identifier", "state", "assignee"])
def test_round28_mutation_mismatch_is_unknown_and_retained(
    claim_scene, tmp_path, capsys, mismatch
):
    store, remote = claim_scene
    transport = MismatchedClaimTransport(remote, mismatch)
    claim = _claim(store, transport)
    with suppress(LinearApiError):
        claim.admit("child")
    assert claim.outcome_unknown, "Mismatched mutation evidence must be outcome-unknown"
    assert not claim.completion_recorded
    assert store.list_audit_events(event="linear_claim_outcome_unknown", run_id="child")
    core._rollback_admitted_claim(_context(store, tmp_path, claim))
    claim.retain("repeat refusal")
    assert transport.verifications == 1
    assert not store.list_audit_events(event="linear_claim_completed", run_id="child")
    [retained] = store.list_audit_events(event="linear_claim_retained", run_id="child")
    assert retained.payload["reason"] == "claim outcome unknown"
    assert retained.payload["snapshot"] is None
    assert retained.payload["recovery"] == LINEAR_CLAIM_RECOVERY
    assert LINEAR_CLAIM_RECOVERY in capsys.readouterr().err


@pytest.mark.parametrize("verification", ["matching", "wrong-issue", "wrong-assignee"])
def test_round28_verification_requires_matching_snapshot(claim_scene, verification):
    store, remote = claim_scene
    transport = MismatchedClaimTransport(remote, "missing", verification)
    claim = _claim(store, transport)
    with pytest.raises(LinearApiError):
        claim.admit("child")
    claim.retain("dispatch refused")
    assert transport.verifications == 1
    completed = store.list_audit_events(event="linear_claim_completed", run_id="child")
    if verification == "matching":
        assert len(completed) == 1
        assert completed[0].payload["source"] == "verification"
        assert not claim.outcome_unknown
    else:
        assert not completed, (
            "Verification of a different owner or issue must not complete"
        )
        assert claim.outcome_unknown
    [retained] = store.list_audit_events(event="linear_claim_retained", run_id="child")
    assert retained.payload["recovery"] == LINEAR_CLAIM_RECOVERY


def test_round28_oserror_cleanup_control(claim_scene, tmp_path, monkeypatch):
    store, remote = claim_scene
    handle = RecordingHandle(store)
    ctx = _launch_scene(store, remote, tmp_path, monkeypatch, handle)
    with pytest.raises(Exception) as caught:
        _stream_container(
            store, tmp_path, partial(core._mark_claim_launch_started, ctx)
        )
    assert isinstance(caught.value, RuntimeError)
    assert handle.calls == ["kill", "wait", "finalize:sandbox_launch_error", "cleanup"]


class RuntimeCleanup:
    def __init__(self, *, refused=False):
        self.calls = []
        self.refused = refused

    def __call__(self, argv, **kwargs):
        self.calls.append(argv[1])
        return SimpleNamespace(returncode=1 if self.refused else 0)


def _created_then_interrupt(failure, argv, *, prepare, **kwargs):
    prepare(argv)
    raise failure


def _reject_identity_after_create(set_identity, failure, run_id, name, identity, *args):
    if identity is not None:
        raise failure
    return set_identity(run_id, name, identity, *args)


@pytest.mark.parametrize("boundary", ["identity", "start", "readiness"])
@pytest.mark.parametrize("kind", [KeyboardInterrupt, SystemExit, GeneratorExit])
def test_round28_created_container_unwind(
    claim_scene, tmp_path, monkeypatch, boundary, kind
):
    store, _ = claim_scene
    failure = kind("launch interrupted")
    runtime = RuntimeCleanup()
    identity = "a" * 64
    argv = ["docker", "run", "--name", "round28", "fixture"]
    monkeypatch.setattr(sandbox, "_run", runtime)
    monkeypatch.setattr(sandbox, "build_agent_container_argv", Mock(return_value=argv))
    monkeypatch.setattr(
        "kew.sandbox.create_registered_container", Mock(return_value=identity)
    )
    if boundary == "identity":
        monkeypatch.setattr(
            store,
            "set_container_identity",
            partial(
                _reject_identity_after_create, store.set_container_identity, failure
            ),
        )
    else:
        monkeypatch.setattr(
            core, "_launch_container", partial(_created_then_interrupt, failure)
        )
    supervisor = None
    ready = None
    if boundary == "readiness":
        supervisor = object()
        monkeypatch.setattr(
            sandbox,
            "prepare_sidecar_worker",
            Mock(
                return_value=ContainedExecution(
                    ("docker", "exec", identity, "agent"),
                    "round28",
                    "docker",
                    tmp_path,
                    identity,
                    True,
                )
            ),
        )
        ready = Mock(side_effect=failure)
    with pytest.raises(kind) as caught:
        sandbox._launch_streaming_container(
            _config(tmp_path),
            store,
            run_id="child",
            log_file=tmp_path / "run.log",
            image="fixture",
            runner="claude-code",
            checkout_dir=tmp_path,
            slug="round28",
            model="test",
            transport=SimpleNamespace(),
            sidecar_supervisor=supervisor,
            sidecar_ready=ready,
            run_kind="dispatch",
        )
    assert runtime.calls == ["kill", "wait"], (
        "Every created-container boundary owns cleanup"
    )
    assert caught.value is failure


def test_round28_refused_kill_preserves_real_identity(tmp_path):
    cidfile = tmp_path / "container.cid"
    cidfile.write_text("a" * 64)
    proc = Mock()
    proc.wait.return_value = 0
    handle = ContainerHandle(proc, "round28", "docker", cidfile=cidfile)
    handle.kill = Mock(return_value=KillStatus.REFUSED)
    failure = OSError("launch delivery unavailable")
    with pytest.raises(Exception) as caught:
        sandbox._deliver_container_launch(
            handle, partial(_raise_launch_failure, failure)
        )
    assert caught.value is not None
    handle.kill.assert_called_once_with()
    proc.wait.assert_called_once()
    assert cidfile.exists(), "Refused kill must preserve the actual identity file"


def test_round28_verification_is_once_even_when_retention_retries(
    claim_scene, monkeypatch
):
    store, remote = claim_scene
    transport = MismatchedClaimTransport(remote, "missing")
    claim = _claim(store, transport)
    with pytest.raises(LinearApiError):
        claim.admit("child")
    append = store.append_audit_event_once
    monkeypatch.setattr(
        store, "append_audit_event_once", Mock(side_effect=OSError("disk unavailable"))
    )
    with pytest.raises(OSError):
        claim.retain("refused")
    monkeypatch.setattr(store, "append_audit_event_once", append)
    claim.retain("refused again")
    assert transport.verifications == 1, (
        "Retention retries must not repeat remote verification"
    )


@pytest.mark.parametrize("kind", [KeyboardInterrupt, SystemExit, GeneratorExit])
def test_round28_sidecar_creation_unwind(monkeypatch, kind):
    failure = kind("worker startup interrupted")
    runtime = RuntimeCleanup()
    supervisor = Mock(worker_name="round28")
    supervisor.create_worker.side_effect = failure
    monkeypatch.setattr(sandbox, "_run", runtime)
    with pytest.raises(kind) as caught:
        sandbox._create_registered_sidecar(None, None, supervisor, ["docker", "create"])
    # No id was reported, and the worker's name may be another run's (#1963).
    assert runtime.calls == [], "An unidentified sidecar is never stopped by name"
    assert caught.value is failure


@pytest.mark.parametrize("kind", [KeyboardInterrupt, SystemExit, GeneratorExit])
def test_an_interrupted_sidecar_creation_stops_the_worker_it_reported(
    monkeypatch, kind
):
    failure = kind("worker startup interrupted")
    worker_id = "b" * 64
    sent = []

    def runtime(argv, **kwargs):
        sent.append(argv[1:])
        return SimpleNamespace(returncode=0)

    def create_worker(bootstrap, *, on_identity):
        on_identity(worker_id)
        raise failure

    supervisor = Mock(worker_name="round28")
    supervisor.create_worker.side_effect = create_worker
    monkeypatch.setattr(sandbox, "_run", runtime)
    with pytest.raises(kind) as caught:
        sandbox._create_registered_sidecar(None, None, supervisor, ["docker", "create"])
    assert sent == [["kill", worker_id], ["wait", worker_id]]
    assert caught.value is failure


@pytest.mark.parametrize("kind", [KeyboardInterrupt, SystemExit, GeneratorExit])
def test_round28_sidecar_filter_unwind(tmp_path, monkeypatch, kind):
    failure = kind("worker filter interrupted")
    runtime = RuntimeCleanup()
    supervisor = Mock(worker_name="round28")
    supervisor.create_worker.return_value = "a" * 64
    supervisor.worker_environment.return_value = {}
    monkeypatch.setattr(sandbox, "_run", runtime)
    monkeypatch.setattr(
        sandbox, "build_run_argv", Mock(return_value=["docker", "create"])
    )
    monkeypatch.setattr(
        sandbox, "install_worker_netns_filter", Mock(side_effect=failure)
    )
    transport = SimpleNamespace(
        sidecar=True,
        endpoint="http://172.30.0.2:4000",
        worker_credential_env_name="fixture",
    )
    with pytest.raises(kind) as caught:
        sandbox.prepare_sidecar_worker(
            _config(tmp_path),
            supervisor=supervisor,
            image_id="fixture",
            runner="claude-code",
            checkout_dir=tmp_path,
            slug="round28",
            model="test",
            inference_transport=transport,
            agent_command=["agent"],
        )
    assert runtime.calls == ["kill", "wait"], "Worker filter setup must own cleanup"
    assert caught.value is failure


def test_round28_id_request_rejects_different_id(claim_scene):
    store, remote = claim_scene
    transport = MismatchedClaimTransport(remote, "identifier")
    claim = _claim(store, transport)
    claim.item_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    with suppress(LinearApiError):
        claim.admit("child")
    assert claim.outcome_unknown, "An ID request requires the same returned ID"
    claim.retain("refused")
    assert transport.verifications == 1
    assert not claim.completion_recorded


class FilterCleanupRuntime:
    def __init__(self, failure_mode):
        self.failure_mode = failure_mode
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(tuple(argv[1:]))
        if argv[1] == "create":
            return SimpleNamespace(returncode=0, stdout="b" * 64, stderr="")
        if (
            argv[1:3] == ["container", "rm"]
            and self.failure_mode == "cleanup-interrupt"
        ):
            raise SystemExit("helper removal interrupted")
        refused = argv[1:] == ["kill", "b" * 64] and self.failure_mode == "kill-refused"
        return SimpleNamespace(returncode=int(refused), stdout="", stderr="")


@pytest.mark.parametrize("failure_mode", ["clean", "kill-refused", "cleanup-interrupt"])
def test_round28_filter_helper_and_worker_preserve_original_unwind(
    monkeypatch, failure_mode
):
    failure = KeyboardInterrupt("filter installation interrupted")
    runtime = FilterCleanupRuntime(failure_mode)
    monkeypatch.setattr(sandbox, "_run", runtime)
    monkeypatch.setattr(sandbox, "_run_bytes", Mock(side_effect=failure))
    start_agent = Mock()
    with pytest.raises(BaseException) as caught:
        sandbox.start_agent_after_worker_netns_filter(
            runtime="docker",
            image_id="sha256:" + "c" * 64,
            worker_id="a" * 64,
            owner="0" * 32,
            proxy_address="172.30.0.2",
            proxy_port=4000,
            start_agent=start_agent,
        )
    cleanup = [call for call in runtime.calls if call[0] in {"kill", "wait"}]
    assert cleanup == [
        ("kill", "b" * 64),
        ("wait", "b" * 64),
        ("kill", "a" * 64),
        ("wait", "a" * 64),
    ], "Helper and worker each require termination and waiting"
    assert caught.value is failure
    start_agent.assert_not_called()
