"""Truthful claim and container launch evidence against merge base aac23378."""

from __future__ import annotations

import json
import subprocess
from contextlib import suppress
from functools import partial
from unittest.mock import Mock

import pytest
from dispatch_owner_fixture import admission_owner as admission_owner
from test_linear_tracker import _config as tracker_config
from test_linear_tracker import _ok
from test_round21_admission_owner import EPIC, ITEM, _admit_host, _config
from test_round26_claim_windows import WindowTransport, _context, _initial_remote

from kew import dispatch_core as core
from kew import dispatch_sandbox as sandbox
from kew.claim_retention import LINEAR_CLAIM_RECOVERY
from kew.cli.epic import _LinearDispatchClaim
from kew.inference_transport import ResolvedInferenceTransport
from kew.linear.client import LinearApiError, LinearClient
from kew.linear.tracker import LinearTracker
from kew.run_store import SqliteRunStore
from kew.sandbox import KillStatus
from kew.tracking import DispatchResult


class IncompleteClaimResponse(WindowTransport):
    def __init__(self, remote, response, *, verify=False):
        super().__init__(remote, "Doing")
        self.response = response
        self.verify = verify
        self.verifications = 0

    def __call__(self, url, payload, headers):
        if "IssueClaimState" in json.loads(payload)["query"]:
            self.verifications += 1
            return _ok({"issue": self.remote_issue() if self.verify else None})
        return super().__call__(url, payload, headers)

    def apply(self, fields):
        super().apply(fields)
        return _ok({"issueUpdate": self.response})


@pytest.fixture
def claim_scene(admission_owner, tmp_path):
    with SqliteRunStore(tmp_path / "runs.db") as store:
        assert store.reserve_epic_node_item(EPIC, ITEM, "reservation-child")
        _admit_host(store, _config(tmp_path), "child")
        yield store, _initial_remote(tmp_path)


def _claim(store, transport):
    tracker = LinearTracker(
        tracker_config(in_review_state="QA"),
        LinearClient("round27-fixture", transport=transport, max_retries=1),
    )
    return _LinearDispatchClaim(tracker, store, ITEM, EPIC)


@pytest.mark.parametrize(
    "response",
    [
        {"success": True, "issue": None},
        {"success": True},
        {"success": True, "issue": {}},
        {"success": True, "issue": "invalid"},
        {"success": True, "issue": {"state": [], "assignee": {"id": "viewer"}}},
    ],
    ids=["null", "missing", "empty", "string", "invalid-state"],
)
def test_round27_incomplete_success_is_unknown_and_retained(
    claim_scene, response, capsys, tmp_path
):
    store, remote = claim_scene
    transport = IncompleteClaimResponse(remote, response)
    claim = _claim(store, transport)
    with suppress(LinearApiError, KeyError, AttributeError):
        claim.admit("child")
    assert claim.outcome_unknown, "Incomplete success must record outcome-unknown"
    assert not claim.completion_recorded
    assert store.list_audit_events(event="linear_claim_outcome_unknown", run_id="child")
    core._rollback_admitted_claim(_context(store, tmp_path, claim))
    assert transport.verifications == 1
    assert not store.list_audit_events(event="linear_claim_completed", run_id="child")
    [retained] = store.list_audit_events(event="linear_claim_retained", run_id="child")
    assert retained.payload["snapshot"] is None
    assert retained.payload["reason"] == "claim outcome unknown"
    assert retained.payload["recovery"] == LINEAR_CLAIM_RECOVERY
    assert LINEAR_CLAIM_RECOVERY in capsys.readouterr().err
    assert not claim.attempted
    assert store.get("child").status == "failure"


def test_round27_completed_claim_has_snapshot_control(claim_scene):
    store, remote = claim_scene
    claim = _claim(store, WindowTransport(remote, "Doing"))
    claim.admit("child")
    [completed] = store.list_audit_events(
        event="linear_claim_completed", run_id="child"
    )
    assert completed.payload["snapshot"] == json.loads(remote.read_text())
    assert completed.payload["source"] == "mutation"
    assert claim.completion_recorded and claim.snapshot is not None


def test_round27_remote_verification_can_complete_incomplete_response(claim_scene):
    store, remote = claim_scene
    transport = IncompleteClaimResponse(
        remote, {"success": True, "issue": None}, verify=True
    )
    claim = _claim(store, transport)
    with suppress(LinearApiError):
        claim.admit("child")
    assert claim.outcome_unknown, "Null mutation snapshots require remote verification"
    claim.retain("dispatch refused")
    [completed] = store.list_audit_events(
        event="linear_claim_completed", run_id="child"
    )
    assert completed.payload["snapshot"] == json.loads(remote.read_text())
    assert completed.payload["source"] == "verification"
    [retained] = store.list_audit_events(event="linear_claim_retained", run_id="child")
    assert retained.payload["recovery"] == LINEAR_CLAIM_RECOVERY


class RecordingHandle:
    """Exercise actual streaming with observable process lifecycle methods."""

    def __init__(self, store, *, kill_status=KillStatus.ACCEPTED, wait_fails=False):
        self.store = store
        self.calls = []
        self.kill_status = kill_status
        self.wait_fails = wait_fails

    def __iter__(self):
        self.calls.append("stream")
        yield '{"type":"system","subtype":"init"}\n'

    def kill(self):
        assert self.store.get("child").status == "pending"
        assert self.store.list_epic_reservations(EPIC)
        self.calls.append("kill")
        return self.kill_status

    def wait(self, timeout=None, *, cleanup_identity=True):
        assert self.store.get("child").status == "pending"
        self.calls.append("wait")
        if self.wait_fails:
            raise subprocess.TimeoutExpired("container", timeout)
        return 0

    def cleanup_identity(self):
        self.calls.append("cleanup")


class RecordingFinalizer:
    def __init__(self, handle):
        self.handle = handle
        self.finalize = sandbox._finalize_sandbox_run

    def __call__(self, *args, **kwargs):
        self.handle.calls.append("finalize:" + kwargs["error_category"])
        return self.finalize(*args, **kwargs)


def _fail_launch_audit(append, **kwargs):
    if kwargs["event"] == "linear_claim_launched":
        raise OSError("launch audit unavailable")
    return append(**kwargs)


def _stream_container(store, tmp_path, callback):
    return sandbox.run_container_and_stream(
        _config(tmp_path),
        store,
        image="fixture-image",
        runner="claude-code",
        checkout_dir=tmp_path,
        slug="round27",
        run_id="child",
        model="test",
        log_file=tmp_path / "container.log",
        run_kind="delegation",
        result=DispatchResult(
            exit_code=-1, issue_num=0, log_file=tmp_path / "container.log"
        ),
        inference_transport=ResolvedInferenceTransport("direct", "test", "test", ""),
        on_launch=callback,
    )


def _launch_scene(store, remote, tmp_path, monkeypatch, handle):
    claim = _claim(store, WindowTransport(remote, "Doing"))
    ctx = _context(store, tmp_path, claim)
    core._claim_after_admission(ctx, "child")
    monkeypatch.setattr(
        sandbox, "_launch_streaming_container", Mock(return_value=handle)
    )
    monkeypatch.setattr(sandbox, "_finalize_sandbox_run", RecordingFinalizer(handle))
    monkeypatch.setattr(
        store,
        "append_audit_event_once",
        partial(_fail_launch_audit, store.append_audit_event_once),
    )
    return ctx


def test_round27_launch_marker_failure_cleans_up_before_refusal(
    claim_scene, tmp_path, monkeypatch
):
    store, remote = claim_scene
    handle = RecordingHandle(store)
    ctx = _launch_scene(store, remote, tmp_path, monkeypatch, handle)
    # Keep retention delivery real while injecting failure only into launch auditing.
    with pytest.raises(Exception) as caught:
        _stream_container(
            store, tmp_path, partial(core._mark_claim_launch_started, ctx)
        )
    assert handle.calls == [
        "kill",
        "wait",
        "finalize:sandbox_launch_error",
        "cleanup",
    ], "Marker failure must kill, wait, finalize the launch layer, then clean identity"
    prelaunch = Mock()
    monkeypatch.setattr(core, "_record_prelaunch_refusal", prelaunch)
    outcome = core._launch_failed(ctx, caught.value, Mock()).early
    assert outcome.category == "sandbox_launch_error"
    assert store.get("child").error_category == "sandbox_launch_error"
    assert store.get("child").status == "failure"
    prelaunch.assert_not_called()
    [retained] = store.list_audit_events(event="linear_claim_retained", run_id="child")
    assert retained.payload["recovery"] == LINEAR_CLAIM_RECOVERY


@pytest.mark.parametrize("failure", ["kill-refused", "wait-timeout"])
def test_round27_unconfirmed_cleanup_preserves_ownership(
    claim_scene, tmp_path, monkeypatch, failure
):
    store, remote = claim_scene
    handle = RecordingHandle(
        store,
        kill_status=KillStatus.REFUSED
        if failure == "kill-refused"
        else KillStatus.ACCEPTED,
        wait_fails=failure == "wait-timeout",
    )
    ctx = _launch_scene(store, remote, tmp_path, monkeypatch, handle)
    with pytest.raises(Exception) as caught:
        _stream_container(
            store, tmp_path, partial(core._mark_claim_launch_started, ctx)
        )
    assert handle.calls == ["kill", "wait"], (
        "Unconfirmed cleanup must preserve identity and pending ownership"
    )
    with pytest.raises(RuntimeError, match="launch audit unavailable"):
        core._launch_failed(ctx, caught.value, Mock())
    core._rollback_admitted_claim(ctx)
    assert store.get("child").status == "pending"
    assert store.list_epic_reservations(EPIC)


def test_round27_successful_marker_preserves_streaming_control(
    claim_scene, tmp_path, monkeypatch
):
    store, _ = claim_scene
    handle = RecordingHandle(store)
    monkeypatch.setattr(
        sandbox, "_launch_streaming_container", Mock(return_value=handle)
    )
    marker = Mock()
    assert _stream_container(store, tmp_path, marker) == 0
    marker.assert_called_once_with()
    assert handle.calls == ["stream", "wait", "cleanup"]
    assert (tmp_path / "container.log").is_file()
