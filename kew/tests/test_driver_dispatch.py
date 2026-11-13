"""Tests for the reservation-guarded detached dispatch sink (Epic DAG driver).

Covers ``reserve_and_dispatch`` (the guarded sequence: reserve -> branch-check
-> sink.dispatch, with release-on-failure) and ``DetachedDispatchSink`` (the
allowlisted, detached ``kew epic _dispatch-node`` spawn).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from support.checkout_generations import (
    fixture_checkout_generation,
    fixture_claim_evidence,
)
from support.fake_gcloud import launchable
from typer.testing import CliRunner

import kew.cli as cli
from kew.cli import epic_app
from kew.delegate.claim import ClaimOutcome, claim_and_dispatch
from kew.delegate.recognize import CandidateIssue
from kew.graph.driver.dispatch import (
    REVIEW_SATURATED,
    DetachedDispatchSink,
    reserve_and_dispatch,
    resume_continuation,
)
from kew.graph.driver.result import StuckNode, TerminalSignal, TickResult
from kew.overlap import DispatchOutcome
from kew.run_store import SqliteRunStore
from kew.subprocess_utils import TOOL_PATHS

runner = CliRunner()


@pytest.fixture
def store(tmp_path: Any) -> Any:
    s = SqliteRunStore(str(tmp_path / "dispatch.db"), project_root=tmp_path)
    yield s
    s.close()


class FakeSink:
    """Records dispatch calls; never actually spawns anything."""

    def __init__(self, *, raises: bool = False) -> None:
        self.calls: list[str] = []
        self._raises = raises

    def dispatch(self, item_id: str) -> bool:
        self.calls.append(item_id)
        if self._raises:
            raise RuntimeError("spawn boom")
        return True


class DelegationTrackerFake:
    def fetch_live(self, item_id: str) -> CandidateIssue:
        return CandidateIssue(
            assignee_id="kew-member",
            labels=("kew:ready",),
            state="Todo",
            state_id="Todo",
        )

    def transition_in_progress(self, item_id: str) -> None:
        pass

    def reset_to_ready(self, item_id: str) -> None:
        pass

    def comment(self, item_id: str, body: str) -> None:
        pass


def _never_exists(item_id: str) -> bool:
    return False


def _always_exists(item_id: str) -> bool:
    return True


def test_happy_path_dispatches_and_reserves(store: Any) -> None:
    sink = FakeSink()
    result = reserve_and_dispatch(
        "42",
        epic_ref="alpha",
        store=store,
        sink=sink,
        branch_exists=_never_exists,
    )
    assert result == "dispatched"
    # The sink and the reservation are keyed on the durable item id (str).
    assert sink.calls == ["42"]
    reservations = store.list_epic_reservations("alpha")
    assert [r.item_id for r in reservations] == ["42"]
    assert [r.issue_num for r in reservations] == [42]


def test_linear_item_id_dispatches_and_reserves(store: Any) -> None:
    """A canonical Linear id validates and reserves under the linear provider."""
    sink = FakeSink()
    result = reserve_and_dispatch(
        "SFD-12",
        epic_ref="alpha",
        store=store,
        sink=sink,
        branch_exists=_never_exists,
        provider="linear",
    )
    assert result == "dispatched"
    assert sink.calls == ["SFD-12"]
    reservations = store.list_epic_reservations("alpha")
    assert [r.item_id for r in reservations] == ["SFD-12"]
    assert [r.issue_num for r in reservations] == [0]


def test_double_reserve_second_caller_conflicts(store: Any) -> None:
    sink1 = FakeSink()
    sink2 = FakeSink()
    first = reserve_and_dispatch(
        "42", epic_ref="alpha", store=store, sink=sink1, branch_exists=_never_exists
    )
    second = reserve_and_dispatch(
        "42", epic_ref="alpha", store=store, sink=sink2, branch_exists=_never_exists
    )
    assert first == "dispatched"
    assert second == "reserved_conflict"
    assert sink2.calls == []


def test_epic_dispatch_uses_atomic_fleet_cap_reservation(store: Any) -> None:
    """An epic admission cannot pass the cap race with delegation."""
    sink = FakeSink()
    store.reserve_epic_node_item("delegation", "SFD-1", "delegate")

    result = reserve_and_dispatch(
        "SFD-2",
        epic_ref="epic-alpha",
        store=store,
        sink=sink,
        branch_exists=_never_exists,
        provider="linear",
        fleet_cap=1,
    )

    assert result == "fleet_full"
    assert sink.calls == []
    assert store.list_epic_reservations("epic-alpha") == []


def test_epic_and_delegation_observations_share_one_atomic_admission(tmp_path):
    """Simultaneous rails yield one dispatch and one fleet reservation."""
    db_path = str(tmp_path / "racing.db")
    SqliteRunStore(db_path).close()
    barrier = threading.Barrier(2)
    outcomes: dict[str, object] = {}
    sinks = {"epic": FakeSink(), "delegation": FakeSink()}

    def epic_observer() -> None:
        local = SqliteRunStore(db_path)
        try:
            barrier.wait(timeout=5)
            outcomes["epic"] = reserve_and_dispatch(
                "SFD-99",
                epic_ref="epic-alpha",
                store=local,
                sink=sinks["epic"],
                branch_exists=_never_exists,
                provider="linear",
                fleet_cap=1,
                new_run_id=lambda: "epic-run",
            )
        finally:
            local.close()

    def delegation_observer() -> None:
        local = SqliteRunStore(db_path)
        try:
            barrier.wait(timeout=5)
            outcomes["delegation"] = claim_and_dispatch(
                "SFD-99",
                member_id="kew-member",
                store=local,
                tracker=DelegationTrackerFake(),
                sink=sinks["delegation"],
                fleet_cap=1,
                new_run_id=lambda: "delegation-run",
            )
        finally:
            local.close()

    threads = [
        threading.Thread(target=epic_observer),
        threading.Thread(target=delegation_observer),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert outcomes["epic"] in {"dispatched", "fleet_full"}
    assert outcomes["delegation"] in {
        ClaimOutcome.DISPATCHED,
        ClaimOutcome.QUEUED_FLEET_FULL,
    }
    assert sum(len(sink.calls) for sink in sinks.values()) == 1
    with SqliteRunStore(db_path) as final_store:
        assert final_store.live_run_count() == 1


def test_branch_exists_releases_reservation(store: Any) -> None:
    sink = FakeSink()
    result = reserve_and_dispatch(
        "42",
        epic_ref="alpha",
        store=store,
        sink=sink,
        branch_exists=_always_exists,
    )
    assert result == "branch_exists"
    assert sink.calls == []
    assert store.list_epic_reservations("alpha") == []


def test_pre_dispatch_guard_registers_before_sink(store: Any) -> None:
    sink = FakeSink()
    guard_calls: list[str] = []

    def guard() -> bool:
        guard_calls.append("registered")
        return True

    result = reserve_and_dispatch(
        "42",
        epic_ref="alpha",
        store=store,
        sink=sink,
        branch_exists=_always_exists,
        before_dispatch=guard,
    )

    assert result == "continuation_registered"
    assert guard_calls == ["registered"]
    assert sink.calls == []
    assert store.list_epic_reservations("alpha") == []


def test_pre_dispatch_guard_failure_releases_reservation(store: Any) -> None:
    sink = FakeSink()

    def guard() -> bool:
        raise RuntimeError("identity mismatch")

    with pytest.raises(RuntimeError, match="identity mismatch"):
        reserve_and_dispatch(
            "42",
            epic_ref="alpha",
            store=store,
            sink=sink,
            branch_exists=_never_exists,
            before_dispatch=guard,
        )

    assert sink.calls == []
    assert store.list_epic_reservations("alpha") == []


def test_spawn_failure_releases_reservation_and_reraises(store: Any) -> None:
    sink = FakeSink(raises=True)
    with pytest.raises(RuntimeError, match="spawn boom"):
        reserve_and_dispatch(
            "42",
            epic_ref="alpha",
            store=store,
            sink=sink,
            branch_exists=_never_exists,
        )
    assert store.list_epic_reservations("alpha") == []


def _claimed_continuation(store: Any, tmp_path: Path) -> Any:
    from datetime import UTC, datetime

    from kew.run_store import RunRecord

    worktree = tmp_path / "preserved"
    store.record_continuation(
        "alpha",
        "42",
        "original-run",
        "agent/issue-42",
        worktree,
        checkout_generation=fixture_checkout_generation(worktree),
    )
    assert (
        store.claim_continuation_under_cap(
            "alpha",
            "42",
            "original-run",
            "agent/issue-42",
            worktree,
            "continuation-run",
            1,
            claim_evidence=fixture_claim_evidence(
                store,
                "alpha",
                "42",
                "original-run",
                "agent/issue-42",
                worktree,
                "continuation-run",
            ),
        )
        == "claimed"
    )
    record = store.get_continuation(
        "alpha",
        "42",
        "original-run",
        "agent/issue-42",
        worktree,
    )
    assert record is not None
    # These callbacks model dispatches that reached run creation.
    store.save(
        RunRecord(
            run_id="continuation-run",
            issue_num=42,
            branch="agent/issue-42",
            status="pending",
            exit_code=0,
            started_at=datetime.now(UTC),
        )
    )
    return record


def test_resume_continuation_finalizes_only_after_successful_dispatch(
    store: Any, tmp_path: Path
) -> None:
    """The in-place continuation helper passes the durable identity through and
    finalizes the exact claimed generation after the host dispatch succeeds."""
    record = _claimed_continuation(store, tmp_path)
    calls: list[tuple[str, Path, str, str]] = []

    def dispatch(
        item_id: str, worktree: Path, branch: str, run_id: str
    ) -> DispatchOutcome:
        calls.append((item_id, worktree, branch, run_id))
        return DispatchOutcome(exit_code=0)

    outcome = resume_continuation(record, store=store, dispatch=dispatch)

    assert outcome.exit_code == 0
    assert calls == [
        ("42", tmp_path / "preserved", "agent/issue-42", "continuation-run")
    ]
    terminal = store.get_continuation(
        "alpha",
        "42",
        "original-run",
        "agent/issue-42",
        tmp_path / "preserved",
    )
    assert terminal is not None
    assert terminal.state == "terminal"
    assert store.list_epic_reservations("alpha") == []


def test_resume_continuation_returns_failed_work_to_awaiting(
    store: Any, tmp_path: Path
) -> None:
    record = _claimed_continuation(store, tmp_path)

    outcome = resume_continuation(
        record,
        store=store,
        dispatch=lambda *_args: DispatchOutcome(exit_code=1),
    )

    assert outcome.exit_code == 1
    awaiting = store.get_continuation(
        "alpha",
        "42",
        "original-run",
        "agent/issue-42",
        tmp_path / "preserved",
    )
    assert awaiting is not None
    assert awaiting.state == "awaiting_continuation"
    assert awaiting.generation == record.generation
    assert store.list_epic_reservations("alpha") == []


def test_resume_continuation_restores_work_after_dispatch_exception(
    store: Any, tmp_path: Path
) -> None:
    record = _claimed_continuation(store, tmp_path)

    def dispatch(*_args: Any) -> DispatchOutcome:
        raise RuntimeError("dispatch boom")

    with pytest.raises(RuntimeError, match="dispatch boom"):
        resume_continuation(record, store=store, dispatch=dispatch)

    awaiting = store.get_continuation(
        "alpha",
        "42",
        "original-run",
        "agent/issue-42",
        tmp_path / "preserved",
    )
    assert awaiting is not None
    assert awaiting.state == "awaiting_continuation"
    assert awaiting.generation == record.generation
    assert store.list_epic_reservations("alpha") == []


def test_non_int_node_id_short_circuits(store: Any) -> None:
    sink = FakeSink()
    result = reserve_and_dispatch(
        "ENG-7",
        epic_ref="alpha",
        store=store,
        sink=sink,
        branch_exists=_never_exists,
    )
    assert result == "non_int"
    assert sink.calls == []
    assert store.list_epic_reservations("alpha") == []


class _RecordingPopen:
    """Fake allowlisted popen: records argv/kwargs, never launches a process."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, cmd: list[str], **kwargs: Any) -> subprocess.Popen[str]:
        self.calls.append((cmd, kwargs))
        return None  # type: ignore[return-value]  # never actually launched


@pytest.fixture
def driver_config(monkeypatch):
    from kew import credential_issuer
    from kew.config import AgentConfig, DispatchConfig

    monkeypatch.setenv("OPENROUTER_PROVISIONING_KEY", "management-secret-1944")
    monkeypatch.setenv("OPENROUTER_API_KEY", "host-secret-1944")
    monkeypatch.setattr(
        credential_issuer,
        "mint_bounded_run_key",
        MagicMock(
            side_effect=lambda run_id, limit, expiry, *, ledger_id, on_malformed: (
                credential_issuer.BoundedRunKey(
                    "bounded-secret-1944", "a" * 64, limit, expiry.isoformat()
                )
            )
        ),
    )
    monkeypatch.setattr(credential_issuer, "revoke_run_key", MagicMock())
    return DispatchConfig(agent=AgentConfig(model="openrouter/test"))


@pytest.mark.parametrize("run_kind", ["dispatch", "delegation"])
def test_driver_issues_before_spawn(store, driver_config, monkeypatch, run_kind):
    from datetime import UTC, datetime

    from kew import credential_issuer
    from kew.run_credentials import EXPIRY_GRACE_SECONDS, retry_schedule_seconds

    monkeypatch.setattr(
        "kew.graph.driver.dispatch.resolve_kew_console_script", lambda: "/bin/kew"
    )
    spawn = MagicMock()
    sink = DetachedDispatchSink(
        epic_ref="alpha",
        config=driver_config,
        store=store,
        popen=spawn,
        run_kind=run_kind,
        publication_token=lambda: None,
    )
    started = datetime.now(UTC)
    assert (
        reserve_and_dispatch(
            "1944",
            epic_ref="alpha",
            store=store,
            sink=sink,
            branch_exists=_never_exists,
        )
        == "dispatched"
    )
    env = spawn.call_args.kwargs["env"]
    assert env["OPENROUTER_API_KEY"] == "bounded-secret-1944"
    assert env["KEW_ISSUED_RUN_KEY_HASH"] == "a" * 64
    assert "OPENROUTER_PROVISIONING_KEY" not in env
    argv = spawn.call_args.args[0]
    assert "--run-id" in argv
    run_id = argv[argv.index("--run-id") + 1]
    call = credential_issuer.mint_bounded_run_key.call_args
    assert call.args[0] == run_id
    assert call.args[1] == driver_config.agent.max_budget_usd
    lifetime = retry_schedule_seconds(
        driver_config.retry, driver_config.agent.timeout_minutes * 60
    )
    assert (call.args[2] - started).total_seconds() >= lifetime + EXPIRY_GRACE_SECONDS
    event = store.list_audit_events(event="sandbox_resolved", run_id=run_id)[0]
    assert event.payload["phase"] == "issuance"
    assert event.payload["issued_by"] == "driver"
    assert "secret-1944" not in str(event)


def test_a_claimed_continuation_child_runs_under_the_claims_own_run_id(
    store, driver_config
):
    """The driver's automatic continuation (#2964) spawns the hidden
    ``_continue-node`` child, keyed to the claim's run id end to end."""
    from kew import credential_issuer

    spawn = MagicMock()
    sink = DetachedDispatchSink(
        epic_ref="alpha",
        config=driver_config,
        store=store,
        popen=spawn,
        publication_token=lambda: None,
    )

    assert sink.continue_claimed("1944", "continuation-1944")

    argv = spawn.call_args.args[0]
    assert argv[:5] == ["kew", "epic", "_continue-node", "alpha", "1944"]
    assert argv[argv.index("--continuation-run-id") + 1] == "continuation-1944"
    assert argv[argv.index("--run-id") + 1] == "continuation-1944"
    assert "--credential-route" in argv
    assert credential_issuer.mint_bounded_run_key.call_args.args[0] == (
        "continuation-1944"
    )
    assert spawn.call_args.kwargs["env"]["OPENROUTER_API_KEY"] == (
        "bounded-secret-1944"
    )


def test_a_child_whose_key_handover_write_fails_is_reported_as_launched(
    store, driver_config, monkeypatch
):
    """Once popen returned the child exists and owns its key: a failed marker
    write after it must neither revoke that key nor read as a failed spawn."""
    import sqlite3

    from kew import credential_issuer
    from kew.graph.driver.dispatch import ChildSpawnedError

    spawn = MagicMock()
    sink = DetachedDispatchSink(
        epic_ref="alpha",
        config=driver_config,
        store=store,
        popen=spawn,
        publication_token=lambda: None,
    )

    real = store.compare_set_meta

    def _locked(key, previous, value):
        if '"child_spawned": true' in value:
            raise sqlite3.OperationalError("database is locked")
        return real(key, previous, value)

    monkeypatch.setattr(store, "compare_set_meta", _locked)
    with pytest.raises(ChildSpawnedError) as raised:
        sink.continue_claimed("1944", "continuation-1944")

    assert raised.value.run_id == "continuation-1944"
    assert isinstance(raised.value.__cause__, sqlite3.OperationalError)
    assert spawn.call_count == 1
    credential_issuer.revoke_run_key.assert_not_called()
    tombstone = "run_key_prelaunch_tombstone:continuation-1944:" + "a" * 64
    assert store.get_meta(tombstone) is None


def test_ordinary_dispatch_keeps_its_reservation_after_post_spawn_bookkeeping_fails(
    store, driver_config, monkeypatch
):
    """A ready-node child that launched owns the reservation it was handed:
    a failed key-handover write after popen must not release it, or a later
    tick could start a second writer for the same node and checkout."""
    import sqlite3

    spawn = MagicMock()
    sink = DetachedDispatchSink(
        epic_ref="alpha",
        config=driver_config,
        store=store,
        popen=spawn,
        publication_token=lambda: None,
    )
    real = store.compare_set_meta

    def _locked(key, previous, value):
        if '"child_spawned": true' in value:
            raise sqlite3.OperationalError("database is locked")
        return real(key, previous, value)

    monkeypatch.setattr(store, "compare_set_meta", _locked)

    result = reserve_and_dispatch(
        "1944",
        epic_ref="alpha",
        store=store,
        sink=sink,
        branch_exists=_never_exists,
    )

    assert result == "dispatched"
    assert spawn.call_count == 1
    argv = spawn.call_args.args[0]
    reservation_run_id = argv[argv.index("--reservation-run-id") + 1]
    reservations = store.list_epic_reservations("alpha")
    assert [(r.item_id, r.run_id) for r in reservations] == [
        ("1944", reservation_run_id)
    ]


@pytest.mark.parametrize("failure", ["unavailable", "refused", "lifetime", "missing"])
def test_driver_mint_refusal_releases_reservation(
    store, driver_config, monkeypatch, failure
):
    from kew import credential_issuer
    from kew.run_credentials import CredentialLifetimeRefused

    monkeypatch.setattr(
        "kew.graph.driver.dispatch.resolve_kew_console_script", lambda: "/bin/kew"
    )
    errors = {
        "unavailable": credential_issuer.IssuerUnavailable,
        "refused": credential_issuer.IssuerRefused,
        "lifetime": CredentialLifetimeRefused,
    }
    reason = "no provisioning key"
    if failure == "missing":
        monkeypatch.delenv("OPENROUTER_PROVISIONING_KEY")
    else:
        credential_issuer.mint_bounded_run_key.side_effect = errors[failure]()
        reason = errors[failure].reason
    spawn = MagicMock()
    sink = DetachedDispatchSink(
        epic_ref="alpha",
        config=driver_config,
        store=store,
        popen=spawn,
        publication_token=lambda: None,
    )
    with pytest.raises(credential_issuer.IssuerError, match=reason):
        reserve_and_dispatch(
            "1944",
            epic_ref="alpha",
            store=store,
            sink=sink,
            branch_exists=_never_exists,
        )
    spawn.assert_not_called()
    assert store.list_epic_reservations("alpha") == []
    assert store.list_by_item("1944")[0].status == "refused"
    event = store.list_audit_events(event="dispatch_completed", item_id="1944")[0]
    assert event.payload["reason"] == reason
    assert "secret-1944" not in str(event)


@pytest.mark.serial
def test_driver_launcher_real_process_filters_authority(
    store, driver_config, monkeypatch
):
    import json
    import sys

    from kew.graph.driver.dispatch import _default_popen

    monkeypatch.setattr(
        "kew.graph.driver.dispatch.resolve_kew_console_script", lambda: "/bin/kew"
    )
    monkeypatch.setitem(TOOL_PATHS, "python", sys.executable)
    observed = []

    def spawn(cmd, **kwargs):
        # Execute a real probe through the sink's production launcher.
        kwargs["stdout"] = subprocess.PIPE
        # The launcher also filters accidentally reintroduced management authority.
        kwargs["env"]["OPENROUTER_PROVISIONING_KEY"] = "management-secret-1944"
        proc = _default_popen(
            ["python", "-c", "import json, os; print(json.dumps(dict(os.environ)))"],
            **kwargs,
        )
        output, _ = proc.communicate(timeout=10)
        assert proc.returncode == 0
        observed.append(json.loads(output))
        return proc

    sink = DetachedDispatchSink(
        epic_ref="alpha",
        config=driver_config,
        store=store,
        popen=spawn,
        publication_token=lambda: None,
    )
    assert sink.dispatch("1944")
    assert "OPENROUTER_PROVISIONING_KEY" not in observed[0]
    assert observed[0]["OPENROUTER_API_KEY"] == "bounded-secret-1944"


def test_detached_sink_registers_kew_and_spawns_detached(
    store: Any,
    driver_config: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No console script adjacent to the interpreter -> fall back to PATH.
    monkeypatch.setattr(
        "kew.graph.driver.dispatch.sys.executable", "/nonexistent/bin/python"
    )
    monkeypatch.setattr(
        "kew.graph.driver.dispatch.shutil.which",
        lambda name: "/usr/local/bin/kew" if name == "kew" else None,
    )
    TOOL_PATHS.pop("kew", None)
    fake_popen: Callable[..., subprocess.Popen[str]] = _RecordingPopen()
    # Pin the no-token branch so the exact-kwargs assertion below cannot depend
    # on an ambient GH_TOKEN or on the host `gh` credential store.
    sink = DetachedDispatchSink(
        epic_ref="alpha",
        config=driver_config,
        store=store,
        popen=fake_popen,
        publication_token=lambda: None,
    )

    result = sink.dispatch("42")

    assert result is True
    assert TOOL_PATHS["kew"] == "/usr/local/bin/kew"
    recorder = fake_popen
    assert isinstance(recorder, _RecordingPopen)
    assert len(recorder.calls) == 1
    assert recorder.calls[0][0][:5] == ["kew", "epic", "_dispatch-node", "alpha", "42"]
    assert recorder.calls[0][1]["stdin"] == subprocess.DEVNULL
    # Regression guard: the detached child must NOT inherit popen()'s default
    # stdout=PIPE/stderr=STDOUT — that pipe's read end dies with the spawning
    # process, so the detached child crashes with BrokenPipeError on its
    # first progress write (the bug this test guards against).
    spawn_kwargs = recorder.calls[0][1]
    assert spawn_kwargs["stdout"] != subprocess.PIPE
    assert spawn_kwargs["stderr"] != subprocess.STDOUT
    assert spawn_kwargs["start_new_session"] is True
    TOOL_PATHS.pop("kew", None)


def test_detached_sink_hands_foreground_gh_credential_to_child(
    store: Any,
    driver_config: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A detached child must not depend on keychain/TTY auth after ``setsid``."""
    monkeypatch.setattr(
        "kew.graph.driver.dispatch.resolve_kew_console_script",
        lambda: "/usr/local/bin/kew",
    )
    monkeypatch.setenv("HOME", "/operator-home")
    monkeypatch.setenv("PATH", "/usr/local/bin:/usr/bin")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/private/tmp/operator-agent.sock")
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    fake_popen: Callable[..., subprocess.Popen[str]] = _RecordingPopen()
    sink = DetachedDispatchSink(
        epic_ref="alpha",
        config=driver_config,
        store=store,
        popen=fake_popen,
        publication_token=lambda: "gho_foreground_token",
    )

    assert sink.dispatch("42") is True

    recorder = fake_popen
    assert isinstance(recorder, _RecordingPopen)
    child_env = recorder.calls[0][1]["env"]
    assert child_env["HOME"] == "/operator-home"
    assert child_env["PATH"] == "/usr/local/bin:/usr/bin"
    assert child_env["SSH_AUTH_SOCK"] == "/private/tmp/operator-agent.sock"
    assert child_env["KEW_DETACHED_PUBLICATION_TOKEN"] == "gho_foreground_token"
    assert "GH_TOKEN" not in child_env
    assert "gho_foreground_token" not in " ".join(recorder.calls[0][0])


def test_detached_sink_resolves_the_publication_token_once_per_run(
    store: Any,
    driver_config: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unresolved auth shells out to `gh`; never once per dispatched node."""
    monkeypatch.setattr(
        "kew.graph.driver.dispatch.resolve_kew_console_script",
        lambda: "/usr/local/bin/kew",
    )
    calls: list[int] = []

    def _resolve() -> str | None:
        calls.append(1)
        return None

    fake_popen: Callable[..., subprocess.Popen[str]] = _RecordingPopen()
    sink = DetachedDispatchSink(
        epic_ref="alpha",
        config=driver_config,
        store=store,
        popen=fake_popen,
        publication_token=_resolve,
    )

    for item_id in ("41", "42", "43"):
        assert sink.dispatch(item_id) is True

    # A resolution that yields no credential is cached too — otherwise every
    # node in an unauthenticated run pays the `gh auth token` timeout again.
    assert len(calls) == 1
    recorder = fake_popen
    assert isinstance(recorder, _RecordingPopen)
    assert all(
        "OPENROUTER_PROVISIONING_KEY" not in kwargs["env"]
        for _, kwargs in recorder.calls
    )


def test_detached_sink_raises_when_kew_missing_from_path(
    store: Any,
    driver_config: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "kew.graph.driver.dispatch.sys.executable", "/nonexistent/bin/python"
    )
    monkeypatch.setattr("kew.graph.driver.dispatch.shutil.which", lambda name: None)
    TOOL_PATHS.pop("kew", None)
    fake_popen: Callable[..., subprocess.Popen[str]] = _RecordingPopen()
    sink = DetachedDispatchSink(
        epic_ref="alpha", config=driver_config, store=store, popen=fake_popen
    )

    with pytest.raises(RuntimeError, match="kew CLI"):
        sink.dispatch("42")

    assert isinstance(fake_popen, _RecordingPopen)
    assert fake_popen.calls == []


def test_detached_sink_prefers_console_script_adjacent_to_interpreter(
    store: Any, driver_config: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The venv's ``bin/kew`` next to ``sys.executable`` wins over an ambient
    PATH ``kew`` — the child then shares the driver's interpreter+package
    (#808), and the resolved path is registered as the allowlist target."""
    bindir = tmp_path / "venv" / "bin"
    bindir.mkdir(parents=True)
    adjacent = bindir / "kew"
    adjacent.write_text("#!/bin/sh\n")
    adjacent.chmod(0o755)

    monkeypatch.setattr(
        "kew.graph.driver.dispatch.sys.executable", str(bindir / "python")
    )
    # Some tmpfs mounts are `noexec`, so an X_OK probe of a real file there can
    # spuriously fail; assert the executable-bit check targets the venv script.
    monkeypatch.setattr(
        "kew.graph.driver.dispatch.os.access",
        lambda path, mode: str(path) == str(adjacent),
    )
    # A different kew on PATH must be ignored in favour of the adjacent one.
    monkeypatch.setattr(
        "kew.graph.driver.dispatch.shutil.which",
        lambda name: "/usr/local/bin/kew" if name == "kew" else None,
    )
    TOOL_PATHS.pop("kew", None)
    fake_popen: Callable[..., subprocess.Popen[str]] = _RecordingPopen()
    sink = DetachedDispatchSink(
        epic_ref="alpha", config=driver_config, store=store, popen=fake_popen
    )

    assert sink.dispatch("42") is True
    assert TOOL_PATHS["kew"] == str(adjacent)
    assert isinstance(fake_popen, _RecordingPopen)
    assert fake_popen.calls[0][0][:5] == [
        "kew",
        "epic",
        "_dispatch-node",
        "alpha",
        "42",
    ]
    TOOL_PATHS.pop("kew", None)


# --- Task 1: lock-message transparency + Task 2: `epic run --all` -------------


def _terminal_result(signal: TerminalSignal) -> TickResult:
    """A minimal TickResult carrying just the terminal `signal` the poll
    loop branches on (one node done for COMPLETE, none otherwise)."""
    done = ("1",) if signal is TerminalSignal.COMPLETE else ()
    remaining = 0 if signal is TerminalSignal.COMPLETE else 1
    return TickResult(
        dispatched=(),
        merged=(),
        held=(),
        in_flight=(),
        failed=(),
        cancelled=(),
        stuck=(),
        done=done,
        remaining=remaining,
        signal=signal,
    )


class _FakeStoreCtx:
    """Context-manager stand-in for `kew.cli._open_run_store`."""

    def __init__(self, store: Any) -> None:
        self._store = store

    def __enter__(self) -> Any:
        return self._store

    def __exit__(self, *exc: Any) -> bool:
        return False


class _FakeStore:
    def __init__(self, refs: list[str]) -> None:
        self._refs = refs

    def list_epic_refs(self) -> list[str]:
        return list(self._refs)

    def epic_trackers(self) -> dict[str, str | None]:
        # No recorded tracker for any epic -> all drive under the active
        # tracker (#882/B1 scoping treats None as "drive it").
        return {}

    def list_config_overrides(self) -> list[dict[str, Any]]:
        # The driver now re-resolves config WITH the store each tick (#892/O1)
        # so DB overrides govern enforcement; this double carries none.
        return []

    def list_audit_events(self, limit: int | None = 100, **_kw: Any) -> list[Any]:
        # `--all` reads the archive decisions off the chain (#2206); no epic
        # here has been archived, so the roster is driven whole.
        return []


def _wire_epic_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    refs: list[str],
    advance_by_epic: dict[str, list[TerminalSignal]],
) -> list[str]:
    """Patch the seams `epic run`/`epic run --all` need and return the ordered
    list of epic names `advance` was called for."""
    cfg = cli.DispatchConfig()
    cfg.logging.dir = str(tmp_path)
    cfg.project.repo = "o/r"
    monkeypatch.setattr(
        cli,
        "_epic_driver_config",
        lambda mp: (cfg, "o/r", 3, []),
    )
    monkeypatch.setattr(cli, "load_config", lambda **_kw: cfg)
    monkeypatch.setattr(
        cli, "_open_run_store", lambda db: _FakeStoreCtx(_FakeStore(refs))
    )
    monkeypatch.setattr(cli, "GitHubConventionSource", lambda **kw: object())
    monkeypatch.setattr(cli, "GitHubFactsReader", lambda **kw: object())
    monkeypatch.setattr(cli, "DetachedDispatchSink", lambda **kw: object())
    monkeypatch.setattr(cli, "_sleep", lambda _s: None)

    order: list[str] = []
    pending = {k: list(v) for k, v in advance_by_epic.items()}

    def fake_advance(name: str, **kw: Any) -> TickResult:
        order.append(name)
        seq = pending.get(name) or [TerminalSignal.COMPLETE]
        signal = seq.pop(0) if len(seq) > 1 else seq[0]
        return _terminal_result(signal)

    monkeypatch.setattr(cli, "advance", fake_advance)
    return order


def test_locked_out_message_names_holder_and_workaround(tmp_path: Path) -> None:
    """With no live lock file the locked-out line still names the epic and the
    manual-tick workaround (holder unknown)."""
    cfg = cli.DispatchConfig()
    cfg.logging.dir = str(tmp_path)
    msg = cli._locked_out_message("dag-1", cfg)
    assert "another driver holds this epic's lock" in msg
    assert "kew epic advance dag-1" in msg


def test_locked_out_message_reports_holder_pid_and_held_since(tmp_path: Path) -> None:
    """When a driver actually holds the epic's lock, the refusal names its pid,
    epic and held-since (the #861 lock-transparency contract)."""
    import os

    from kew.graph.driver.lock import single_driver_lock

    cfg = cli.DispatchConfig()
    cfg.logging.dir = str(tmp_path)
    lock_path = cli._epic_lock_path(cfg, "dag-1")
    with single_driver_lock(lock_path, holder="dag-1"):
        msg = cli._locked_out_message("dag-1", cfg)
    assert f"pid={os.getpid()}" in msg
    assert "epic=dag-1" in msg
    assert "held-since=" in msg


def test_epic_run_all_advances_every_epic_and_exits_complete(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    order = _wire_epic_run(
        monkeypatch,
        tmp_path,
        refs=["alpha", "beta"],
        advance_by_epic={
            "alpha": [TerminalSignal.COMPLETE],
            "beta": [TerminalSignal.COMPLETE],
        },
    )
    result = runner.invoke(epic_app, ["run", "--all"])
    assert result.exit_code == 0, result.stdout
    # Both epics advanced in the one poll loop, then it exited on all-terminal.
    assert set(order) == {"alpha", "beta"}


def test_epic_run_all_exits_nonzero_when_any_epic_stalls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    order = _wire_epic_run(
        monkeypatch,
        tmp_path,
        refs=["alpha", "beta"],
        advance_by_epic={
            "alpha": [TerminalSignal.COMPLETE],
            "beta": [TerminalSignal.STALLED],
        },
    )
    result = runner.invoke(epic_app, ["run", "--all"])
    assert result.exit_code == 1, result.stdout
    assert set(order) == {"alpha", "beta"}


def test_epic_run_all_loops_until_progressing_epic_terminates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    order = _wire_epic_run(
        monkeypatch,
        tmp_path,
        refs=["alpha"],
        # First tick PROGRESSING, second COMPLETE: the loop must tick twice.
        advance_by_epic={
            "alpha": [TerminalSignal.PROGRESSING, TerminalSignal.COMPLETE]
        },
    )
    result = runner.invoke(epic_app, ["run", "--all"])
    assert result.exit_code == 0, result.stdout
    assert order == ["alpha", "alpha"]


def test_epic_run_single_epic_still_advances_the_named_epic(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    order = _wire_epic_run(
        monkeypatch,
        tmp_path,
        refs=["alpha", "beta"],
        advance_by_epic={"alpha": [TerminalSignal.COMPLETE]},
    )
    result = runner.invoke(epic_app, ["run", "alpha"])
    assert result.exit_code == 0, result.stdout
    # Single-epic run drives ONLY the named epic, ignoring other known refs.
    assert order == ["alpha"]


def test_epic_run_requires_name_or_all(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _wire_epic_run(monkeypatch, tmp_path, refs=[], advance_by_epic={})
    result = runner.invoke(epic_app, ["run"])
    assert result.exit_code == 2


@pytest.fixture
def kew_root(tmp_path: Path) -> Iterator[Path]:
    """Where the fake pinned installs live: under ``tmp_path`` unless the
    sandbox mounts it noexec, else under HOME, removed on teardown."""
    if launchable(tmp_path):
        yield tmp_path / "kews"
        return
    fallback = Path(tempfile.mkdtemp(prefix="kew-pinned-", dir=Path.home()))
    try:
        yield fallback / "kews"
    finally:
        shutil.rmtree(fallback)


def _pinned_kew(root: Path, version: str, body: str) -> Path:
    """A versioned "install" whose bin/kew records the argv it ran with."""
    script = root / version / "bin" / "kew"
    script.parent.mkdir(parents=True)
    script.write_text(
        f'#!/bin/sh\nprintf "{version} %s\\n" "$*" >> "{root}/ticks.log"\n{body}\n'
    )
    script.chmod(0o755)
    return script


def _point_current(root: Path, version: str) -> Path:
    current = root / "current"
    current.symlink_to(root / version, target_is_directory=True)
    return current / "bin" / "kew"


def _tick_log(root: Path) -> list[str]:
    return (root / "ticks.log").read_text().splitlines()


def test_epic_run_kew_binary_pins_each_tick_to_the_kew_current_resolves_at_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, kew_root: Path
) -> None:
    """#2458: a merge repoints `current` while tick 1 runs. Tick 1 still ends
    on v1 (it was launched on the resolved v1 path); tick 2 runs v2."""
    _wire_epic_run(monkeypatch, tmp_path, refs=["alpha"], advance_by_epic={})
    root = kew_root
    # v1 is the "long tick": it repoints current mid-run, then asks to continue.
    _pinned_kew(root, "v1", f'ln -sfn "{root}/v2" "{root}/current"\nexit 10')
    _pinned_kew(root, "v2", "exit 0")
    binary = _point_current(root, "v1")

    result = runner.invoke(epic_app, ["run", "alpha", "--kew-binary", str(binary)])

    assert result.exit_code == 0, result.output
    ticks = _tick_log(root)
    assert [t.split()[0] for t in ticks] == ["v1", "v2"]
    assert all(t.split()[1:4] == ["epic", "_tick", "alpha"] for t in ticks)
    assert f"tick on pinned {root / 'v1' / 'bin' / 'kew'}" in result.output
    assert f"tick on pinned {root / 'v2' / 'bin' / 'kew'}" in result.output


@pytest.mark.parametrize(("child_exit", "run_exit"), [(1, 1), (11, 0), (7, 2)])
def test_epic_run_kew_binary_maps_the_tick_exit_code(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    kew_root: Path,
    child_exit: int,
    run_exit: int,
) -> None:
    """A stalled tick keeps the repair code, a locked-out tick ends the run
    cleanly, and an unknown code is a driver failure — never another tick."""
    _wire_epic_run(monkeypatch, tmp_path, refs=["alpha"], advance_by_epic={})
    root = kew_root
    _pinned_kew(root, "v1", f"exit {child_exit}")
    binary = _point_current(root, "v1")

    result = runner.invoke(epic_app, ["run", "alpha", "--kew-binary", str(binary)])

    assert result.exit_code == run_exit, result.output
    assert len(_tick_log(root)) == 1


def test_epic_run_kew_binary_refuses_all(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _wire_epic_run(monkeypatch, tmp_path, refs=["alpha"], advance_by_epic={})
    result = runner.invoke(
        epic_app, ["run", "--all", "--kew-binary", str(tmp_path / "kew")]
    )
    assert result.exit_code == 2


def test_epic_run_kew_binary_that_is_missing_is_a_driver_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _wire_epic_run(monkeypatch, tmp_path, refs=["alpha"], advance_by_epic={})
    result = runner.invoke(
        epic_app, ["run", "alpha", "--kew-binary", str(tmp_path / "absent")]
    )
    assert result.exit_code == 2


def _recorded_tick_args(root: Path, *extra: str) -> list[str]:
    """The argv `kew epic run --kew-binary` hands its tick child."""
    _pinned_kew(root, "v1", "exit 0")
    binary = _point_current(root, "v1")
    result = runner.invoke(
        epic_app, ["run", "alpha", "--kew-binary", str(binary), *extra]
    )
    assert result.exit_code == 0, result.output
    args = (root / "ticks.log").read_text().split(" ", 1)[1].strip()
    # The log joins argv with spaces; the identity JSON is the one argument
    # that holds spaces, so rebuild argv around it.
    head, rest = args.split(" --identity ", 1)
    identity, tail = rest.split("] ", 1)
    return [*head.split(" "), "--identity", identity + "]", *tail.split(" ")]


@pytest.mark.parametrize(
    ("signal", "code"),
    [
        (TerminalSignal.COMPLETE, 0),
        (TerminalSignal.PROGRESSING, 10),
        (TerminalSignal.AWAITING_MERGES, 10),
        (TerminalSignal.STALLED, 1),
    ],
)
def test_tick_child_runs_one_tick_and_reports_its_build_at_start_and_end(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    kew_root: Path,
    signal: TerminalSignal,
    code: int,
) -> None:
    """The pinned child runs exactly one advance and encodes its signal in
    the exit code the parent loop reads."""
    order = _wire_epic_run(
        monkeypatch, tmp_path, refs=["alpha"], advance_by_epic={"alpha": [signal]}
    )
    args = _recorded_tick_args(kew_root, "--max-parallel", "4")
    assert args[:3] == ["epic", "_tick", "alpha"]
    assert args[args.index("--max-parallel") + 1] == "4"

    preflights: list[bool] = []
    monkeypatch.setattr(cli, "preflight_tools", lambda: preflights.append(True))

    result = runner.invoke(epic_app, args[1:])

    assert result.exit_code == code, result.output
    assert order == ["alpha"]
    assert preflights == [True]
    assert "epic alpha: tick start on kew " in result.output
    assert "epic alpha: tick end on kew " in result.output


def test_tick_child_stops_on_a_reload_that_retargets_the_parents_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, kew_root: Path
) -> None:
    """The parent's startup identity crosses into the child, so a kew.toml
    that now names another repo stops the driver instead of driving it."""
    order = _wire_epic_run(
        monkeypatch,
        tmp_path,
        refs=["alpha"],
        advance_by_epic={"alpha": [TerminalSignal.PROGRESSING]},
    )
    args = _recorded_tick_args(kew_root)
    retargeted = cli.DispatchConfig()
    retargeted.logging.dir = str(tmp_path)
    retargeted.project.repo = "elsewhere/r"
    monkeypatch.setattr(cli, "load_config", lambda **_kw: retargeted)
    monkeypatch.setattr(cli, "_detect_repo", lambda _cfg: None)
    monkeypatch.setattr(cli, "preflight_tools", lambda: None)

    result = runner.invoke(epic_app, args[1:])

    assert result.exit_code == 2, result.output
    assert order == []
    assert "o/r -> elsewhere/r" in result.output


def test_tick_child_skips_a_tick_whose_config_cannot_load(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, kew_root: Path
) -> None:
    order = _wire_epic_run(monkeypatch, tmp_path, refs=["alpha"], advance_by_epic={})
    args = _recorded_tick_args(kew_root)

    def half_saved(**_kw: Any) -> Any:
        raise ValueError("half-saved kew.toml")

    monkeypatch.setattr(cli, "load_config", half_saved)
    monkeypatch.setattr(cli, "preflight_tools", lambda: None)
    result = runner.invoke(epic_app, args[1:])

    assert result.exit_code == 10, result.output
    assert order == []
    assert "half-saved kew.toml" in result.output


def test_dispatch_node_command_is_hidden_from_help() -> None:
    """``_dispatch-node`` is an internal spawn target, not a user-facing verb.

    It must be registered on ``epic_app`` (so the driver can invoke it) but
    absent from ``kew epic --help`` output (``hidden=True`` in cli.py).
    """
    assert any(cmd.name == "_dispatch-node" for cmd in epic_app.registered_commands)

    result = runner.invoke(epic_app, ["--help"])

    assert result.exit_code == 0
    assert "_dispatch-node" not in result.stdout


# --- Linear dispatch-execution vertical (#809) ---------------------------------


def _linear_config() -> Any:
    from kew.config import DispatchConfig, LinearTrackerConfig, TrackerConfig

    return DispatchConfig(
        tracker=TrackerConfig(
            type="linear",
            linear=LinearTrackerConfig(team="SFD", in_review_state="In Review"),
        )
    )


class _FakeLinearTracker:
    """Records fetch/claim ordering; ``claim_raises`` exercises the abort path."""

    def __init__(self, calls: list[Any], *, claim_raises: bool = False) -> None:
        self._calls = calls
        self._claim_raises = claim_raises

    def fetch_issue(self, item_id: str) -> dict[str, Any]:
        self._calls.append(("fetch", item_id))
        return {"number": 0, "title": "T", "body": "B", "url": "u", "labels": []}

    def claim_assignee(self) -> str:
        return "viewer"

    def claim_state(self) -> dict[str, str]:
        return {"id": "started", "name": "In Progress"}

    def claim(self, item_id: str) -> dict[str, Any]:
        self._calls.append(("claim", item_id))
        if self._claim_raises:
            from kew.linear.client import LinearApiError

            raise LinearApiError(f"claim boom {item_id}")
        return {
            "issue": {
                "identifier": item_id,
                "updatedAt": "claim-A",
                "state": {"id": "started", "type": "started"},
                "assignee": {"id": "viewer"},
            }
        }

    def reset_to_ready(self, item_id: str) -> dict[str, Any]:
        self._calls.append(("reset_to_ready", item_id))
        return {}

    def comment(self, item_id: str, body: str) -> dict[str, Any]:
        self._calls.append(("comment", item_id, body))
        return {}

    def close(self, item_id: str) -> dict[str, Any]:
        self._calls.append(("close", item_id))
        return {}


def _patch_linear(monkeypatch: Any, tracker: Any) -> None:
    monkeypatch.setenv("LINEAR_API_KEY", "fake-key-not-a-secret")
    monkeypatch.setattr("kew.linear.client.LinearClient", lambda *a, **k: object())
    monkeypatch.setattr("kew.linear.tracker.LinearTracker", lambda *a, **k: tracker)


def test_dispatch_linear_node_fetches_claims_then_dispatches(
    store: Any, monkeypatch: Any
) -> None:
    calls: list[Any] = []
    _patch_linear(monkeypatch, _FakeLinearTracker(calls))
    captured: dict[str, Any] = {}

    def _fake_dispatch(**kwargs: Any) -> Any:
        from kew.dispatch_core import DispatchOutcome

        calls.append(("dispatch", kwargs["item_id"]))
        captured.update(kwargs)
        kwargs["on_admitted"]("admitted-child")
        return DispatchOutcome(exit_code=0, changed_files=[])

    monkeypatch.setattr(cli, "execute_dispatch", _fake_dispatch)

    cli._dispatch_linear_node(
        store,
        _linear_config(),
        None,
        epic_ref="alpha",
        item_id="SFD-133",
        provider="linear",
    )

    # The dispatch pipeline admits the child before invoking its claim hook.
    assert calls == [
        ("fetch", "SFD-133"),
        ("dispatch", "SFD-133"),
        ("claim", "SFD-133"),
    ]
    assert captured["approved"] is True
    assert captured["epic_ref"] == "alpha"
    assert captured["provider"] == "linear"
    assert captured["item_id"] == "SFD-133"
    assert captured["issue"]["number"] == 0  # 0 sentinel; durable id is item_id


def test_dispatch_linear_node_rejects_partial_continuation_state(
    store: Any, monkeypatch: Any
) -> None:
    calls: list[Any] = []
    _patch_linear(monkeypatch, _FakeLinearTracker(calls))

    with pytest.raises(ValueError, match="continuation dispatch requires"):
        cli._dispatch_linear_node(
            store,
            _linear_config(),
            None,
            epic_ref="alpha",
            item_id="SFD-133",
            provider="linear",
            continuation_run_id="continuation-1",
        )

    assert calls == []


def test_dispatch_linear_node_terminal_failure_only_comments(
    store: Any, monkeypatch: Any
) -> None:
    calls: list[Any] = []
    _patch_linear(monkeypatch, _FakeLinearTracker(calls))

    def _fake_dispatch(**kwargs: Any) -> Any:
        from kew.dispatch_core import DispatchOutcome

        kwargs["on_terminal_failure"]("needs_human", "missing schema")
        return DispatchOutcome(exit_code=1, changed_files=[])

    monkeypatch.setattr(cli, "execute_dispatch", _fake_dispatch)
    cli._dispatch_linear_node(
        store,
        _linear_config(),
        None,
        epic_ref="alpha",
        item_id="SFD-133",
        provider="linear",
    )

    terminal = next(call for call in calls if call[0] == "comment")
    assert "needs_human: missing schema" in terminal[2]
    assert not any(call[0] in {"close", "reset_to_ready"} for call in calls)


def test_dispatch_linear_node_no_change_writes_nothing_to_the_tracker(
    store: Any, monkeypatch: Any
) -> None:
    """A zero-change completion no longer comments on or closes the item.

    The legacy hook did both, on the strength of an empty diff — and on Linear
    a close is what arms the dependents. The outcome is now a durable local
    no-change candidate (I10B); nothing reaches the tracker until a person
    authorizes it, so the dispatch child wires no callback at all.
    """
    calls: list[Any] = []
    _patch_linear(monkeypatch, _FakeLinearTracker(calls))
    seen: dict[str, Any] = {}

    def _fake_dispatch(**kwargs: Any) -> Any:
        from kew.dispatch_core import DispatchOutcome

        seen.update(kwargs)
        return DispatchOutcome(exit_code=0, changed_files=[])

    monkeypatch.setattr(cli, "execute_dispatch", _fake_dispatch)
    cli._dispatch_linear_node(
        store,
        _linear_config(),
        None,
        epic_ref="alpha",
        item_id="SFD-133",
        provider="linear",
    )

    assert "on_complete_no_changes" not in seen
    assert not any(call[0] in {"comment", "close"} for call in calls)


def _dispatch_linear_at_admission(store, monkeypatch):
    from datetime import UTC, datetime

    from kew import dispatch_core as core
    from kew.linear.client import LinearApiError
    from kew.run_store import RunRecord
    from kew.run_store_work import capture_dispatch_reservation

    def pipeline(ctx):
        row = RunRecord(
            run_id="admitted-child",
            issue_num=0,
            item_id=ctx.item_id,
            branch=ctx.branch,
            status="pending",
            exit_code=-1,
            started_at=datetime.now(UTC),
        )
        reservation = capture_dispatch_reservation(
            store,
            ctx.epic_ref,
            ctx.item_id,
            expected_reservation_run_id=ctx.expected_reservation_run_id,
        )
        assert store.admit_run_with_identity(
            row,
            store.build_admission_identity(
                row.run_id,
                tracker="linear",
                item_id=ctx.item_id,
                repo=None,
            ),
            reservation=reservation,
            reservation_pinned=True,
        )
        try:
            core._claim_after_admission(ctx, row.run_id)
        except LinearApiError:
            core._check_dispatch_reservation(ctx)
            return core.DispatchOutcome(exit_code=1, changed_files=[])
        pytest.fail("a failed claim must not reach the agent")

    monkeypatch.setattr(core, "_capture_root_checkout_state", lambda *_a, **_k: None)
    monkeypatch.setattr(core, "_run_dispatch_pipeline", pipeline)
    monkeypatch.setattr(cli, "execute_dispatch", core.execute_dispatch)


def test_dispatch_linear_node_claim_failure_records_terminal_no_spawn(
    store: Any, monkeypatch: Any
) -> None:
    calls: list[Any] = []
    _patch_linear(monkeypatch, _FakeLinearTracker(calls, claim_raises=True))
    _dispatch_linear_at_admission(store, monkeypatch)
    store.reserve_epic_node_item("alpha", "SFD-133", "run-xyz")
    outcome = cli._dispatch_linear_node(
        store,
        _linear_config(),
        None,
        epic_ref="alpha",
        item_id="SFD-133",
        provider="linear",
        expected_reservation_run_id="run-xyz",
    )
    assert outcome.exit_code == 1
    assert ("dispatch", "SFD-133") not in calls
    [row] = store.list_by_item("SFD-133")
    assert row.status == "failure"
    assert row.run_id == "admitted-child"
    assert row.exit_code == 1
    assert row.error_category == "dispatch_child_refused"
    assert ("reset_to_ready", "SFD-133") not in calls
    assert store.list_audit_events(event="linear_claim_completed") == []
    assert store.list_audit_events(event="linear_claim_retained") == []


def test_dispatch_linear_node_fetch_failure_records_terminal_no_spawn(
    store: Any, monkeypatch: Any
) -> None:
    import typer

    class _FetchFailTracker(_FakeLinearTracker):
        def fetch_issue(self, item_id: str) -> dict[str, Any]:
            self._calls.append(("fetch", item_id))
            from kew.linear.client import LinearApiError

            raise LinearApiError(f"fetch boom {item_id}")

    calls: list[Any] = []
    _patch_linear(monkeypatch, _FetchFailTracker(calls))
    monkeypatch.setattr(
        cli, "execute_dispatch", lambda **k: pytest.fail("must not spawn")
    )
    store.reserve_epic_node_item("alpha", "SFD-133", "run-xyz")

    with pytest.raises(typer.Exit) as exc:
        cli._dispatch_linear_node(
            store,
            _linear_config(),
            None,
            epic_ref="alpha",
            item_id="SFD-133",
            provider="linear",
            expected_reservation_run_id="run-xyz",
        )

    assert exc.value.exit_code == 2
    # Fetch was attempted; claim/dispatch never ran.
    assert calls == [("fetch", "SFD-133")]
    rows = store.list_by_item("SFD-133")
    assert len(rows) == 1
    assert rows[0].status == "failure"
    assert rows[0].run_id == "run-xyz"
    # The run record keeps the internal outcome code; only the command
    # boundary translates a fetch failure to the public could-not-run exit (2).
    assert rows[0].exit_code == 1
    assert rows[0].error_category == "dispatch_child_refused"


def test_dispatch_linear_node_rolls_back_claim_on_pre_spawn_refusal(
    store: Any, monkeypatch: Any
) -> None:
    calls: list[Any] = []
    _patch_linear(monkeypatch, _FakeLinearTracker(calls))

    def _refusing_dispatch(**kwargs: Any) -> Any:
        from kew.dispatch_core import DispatchOutcome

        calls.append(("dispatch", kwargs["item_id"]))
        # Simulate a pre-spawn refusal (fleet-stop / budget) firing the hook.
        kwargs["on_refuse"]("pre-admission refusal")
        return DispatchOutcome(exit_code=1, changed_files=[])

    monkeypatch.setattr(cli, "execute_dispatch", _refusing_dispatch)

    cli._dispatch_linear_node(
        store,
        _linear_config(),
        None,
        epic_ref="alpha",
        item_id="SFD-133",
        provider="linear",
    )

    # A pre-admission refusal neither claims nor resets the tracker.
    assert calls == [
        ("fetch", "SFD-133"),
        ("dispatch", "SFD-133"),
    ]


def test_dispatch_linear_node_missing_key_records_terminal_no_spawn(
    store: Any, monkeypatch: Any
) -> None:
    import typer

    calls: list[Any] = []
    _patch_linear(monkeypatch, _FakeLinearTracker(calls))
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    monkeypatch.setattr(
        cli, "execute_dispatch", lambda **k: pytest.fail("must not spawn")
    )

    with pytest.raises(typer.Exit) as exc:
        cli._dispatch_linear_node(
            store,
            _linear_config(),
            None,
            epic_ref="alpha",
            item_id="SFD-133",
            provider="linear",
        )

    assert exc.value.exit_code == 2
    assert calls == []  # no fetch, no claim, no dispatch
    rows = store.list_by_item("SFD-133")
    assert len(rows) == 1
    assert rows[0].exit_code == 2  # missing key is an exit-2
    assert rows[0].error_category == "dispatch_child_refused"


# ---------------------------------------------------------------------------
# I15: the admission gate that human-review backpressure installs on the door.
# ---------------------------------------------------------------------------


def test_admission_gate_refuses_before_anything_is_reserved(store: Any) -> None:
    sink = FakeSink()

    result = reserve_and_dispatch(
        "42",
        epic_ref="alpha",
        store=store,
        sink=sink,
        branch_exists=_never_exists,
        admit=lambda item_id: REVIEW_SATURATED,
    )

    assert result == REVIEW_SATURATED
    assert sink.calls == []
    # No slot was taken, so none has to be handed back: a refusal that
    # reserved first could leak the reservation it then released.
    assert store.list_epic_reservations("alpha") == []


def test_admission_gate_sees_the_canonical_item_id(store: Any) -> None:
    seen: list[str] = []

    reserve_and_dispatch(
        "42",
        epic_ref="alpha",
        store=store,
        sink=FakeSink(),
        branch_exists=_never_exists,
        admit=lambda item_id: seen.append(item_id) or None,
    )

    assert seen == ["42"]


def test_a_non_canonical_id_is_refused_before_the_gate_is_asked(store: Any) -> None:
    asked: list[str] = []

    result = reserve_and_dispatch(
        "not-an-id",
        epic_ref="alpha",
        store=store,
        sink=FakeSink(),
        branch_exists=_never_exists,
        admit=lambda item_id: asked.append(item_id) or None,
    )

    assert result == "non_int"
    assert asked == []


def test_an_open_gate_dispatches_exactly_as_before(store: Any) -> None:
    sink = FakeSink()

    result = reserve_and_dispatch(
        "42",
        epic_ref="alpha",
        store=store,
        sink=sink,
        branch_exists=_never_exists,
        admit=lambda item_id: None,
    )

    assert result == "dispatched"
    assert sink.calls == ["42"]


def test_no_caller_can_walk_around_the_gate_with_its_own_candidate_list(
    store: Any,
) -> None:
    # A second driver (or a fairness pass that computed its own list) still
    # goes through this door, so the cap cannot be bypassed by choosing
    # different candidates.
    sink = FakeSink()
    saturated = {"42", "43"}

    results = [
        reserve_and_dispatch(
            item_id,
            epic_ref="alpha",
            store=store,
            sink=sink,
            branch_exists=_never_exists,
            admit=lambda nid: REVIEW_SATURATED if nid in saturated else None,
        )
        for item_id in ("42", "43", "44")
    ]

    assert results == [REVIEW_SATURATED, REVIEW_SATURATED, "dispatched"]
    assert sink.calls == ["44"]


# --- Detached-child admission refusals (#1658) --------------------------------


def _wire_dispatch_child(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    store: Any,
    *,
    dispatch: Callable[..., Any],
) -> Any:
    """Patch the seams `kew epic _dispatch-node` needs and return its config.

    Everything the child does before `execute_dispatch` — tool preflight,
    publication token, notifier, store — is a seam here; only the refusal
    bookkeeping under test is real.
    """
    import kew.cli.epic as epic_mod

    cfg = cli.DispatchConfig()
    cfg.logging.dir = str(tmp_path)
    cfg.project.repo = "o/r"
    monkeypatch.setattr(epic_mod, "activate_detached_publication_token", lambda: None)
    monkeypatch.setattr(cli, "preflight_tools", lambda: None)
    monkeypatch.setattr(cli, "load_config", lambda **_kw: cfg)
    monkeypatch.setattr(
        cli,
        "runner_preflight_failure",
        lambda _cfg, *, runner_name=None: None,
    )
    monkeypatch.setattr(cli, "build_notifier", lambda _cfg: object())
    monkeypatch.setattr(cli, "_open_run_store", lambda _db: _FakeStoreCtx(store))
    monkeypatch.setattr(
        cli, "select_next_issue", lambda **_kw: {"number": 42, "title": "t"}
    )
    monkeypatch.setattr(cli, "execute_dispatch", dispatch)
    return cfg


def test_dispatch_child_runner_preflight_runs_on_effective_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, store: Any
) -> None:
    import kew.cli.epic as epic_mod

    file_config = cli.DispatchConfig()
    file_config.logging.dir = str(tmp_path)
    file_config.agent.runner = "claude-code"
    from kew.config import RouteEntry, RoutingConfig

    effective_config = cli.DispatchConfig(
        routing=RoutingConfig(
            default=RouteEntry(runner="claude-code", model="claude-sonnet-4-6"),
            medium=RouteEntry(runner="codex", model="gpt-5.6-sol"),
        )
    )
    effective_config.logging.dir = str(tmp_path)
    refusal = "Runner 'codex' is not logged in. Authenticate with: codex login"
    opened: list[str] = []
    preflighted: list[tuple[cli.DispatchConfig, str | None]] = []

    def _load_config(*, store: Any = None) -> cli.DispatchConfig:
        return effective_config if store is not None else file_config

    def _open_store(db: str) -> _FakeStoreCtx:
        opened.append(db)
        return _FakeStoreCtx(store)

    def _preflight(
        config: cli.DispatchConfig, *, runner_name: str | None = None
    ) -> str | None:
        preflighted.append((config, runner_name))
        return refusal if runner_name == "codex" else None

    monkeypatch.setattr(epic_mod, "activate_detached_publication_token", lambda: None)
    monkeypatch.setattr(cli, "preflight_tools", lambda: None)
    monkeypatch.setattr(cli, "load_config", _load_config)
    monkeypatch.setattr(cli, "runner_preflight_failure", _preflight)
    monkeypatch.setattr(cli, "provider_from_config", lambda _cfg: "github")
    monkeypatch.setattr(cli, "validate_item_id", lambda item, _provider: item)
    monkeypatch.setattr(cli, "build_notifier", lambda _cfg: object())
    monkeypatch.setattr(cli, "_open_run_store", _open_store)
    monkeypatch.setattr(
        cli,
        "select_next_issue",
        lambda **_kw: {"number": 42, "title": "t", "labels": []},
    )
    launch = MagicMock()
    monkeypatch.setattr(cli, "execute_dispatch", launch)

    result = runner.invoke(epic_app, ["_dispatch-node", "dag-1", "42"])

    assert result.exit_code == 2
    assert opened == [str(tmp_path / "dispatch.db")]
    assert preflighted == [(effective_config, "codex")]
    launch.assert_not_called()
    holds = store.list_epic_holds("dag-1")
    assert [hold.item_id for hold in holds] == ["42"]
    assert refusal in holds[0].reason
    rows = store.list_by_item("42")
    assert len(rows) == 1
    assert rows[0].status == "refused"
    assert rows[0].error_category == "admission_refused"


def test_dispatch_child_unknown_runner_records_refusal_and_hold(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, store: Any
) -> None:
    from kew.config import RouteEntry, RoutingConfig

    cfg = _wire_dispatch_child(
        monkeypatch,
        tmp_path,
        store,
        dispatch=MagicMock(),
    )
    cfg.routing = RoutingConfig(
        default=RouteEntry(runner="claude-code", model="claude-sonnet-4-6"),
        medium=RouteEntry(runner="codeex", model="gpt-5.6-sol"),
    )

    result = runner.invoke(epic_app, ["_dispatch-node", "dag-1", "42"])

    assert result.exit_code == 2
    holds = store.list_epic_holds("dag-1")
    assert [hold.item_id for hold in holds] == ["42"]
    assert "Unknown runner 'codeex'" in holds[0].reason
    rows = store.list_by_item("42")
    assert len(rows) == 1
    assert rows[0].status == "refused"
    assert rows[0].error_category == "admission_refused"


def _refusal_dispatch(
    *, category: str | None, message: str, exit_code: int = 1
) -> Callable[..., Any]:
    """A fake `execute_dispatch` that refuses exactly like the real gates do:
    print why, record the ledger's `dispatch_completed` category (when the real
    path would), and RETURN a non-zero outcome rather than raising."""
    from kew.dispatch_core import _emit_early_exit
    from kew.progress import _error

    def _dispatch(**kwargs: Any) -> DispatchOutcome:
        _error(message)
        if category is not None:
            _emit_early_exit(
                kwargs["store"],
                issue_num=42,
                item_id="42",
                complexity="medium",
                runner="claude",
                model="claude-opus-5",
                exit_code=exit_code,
                duration_seconds=0.1,
                error_category=category,
                repo="o/r",
                epic_ref=kwargs["epic_ref"],
            )
        return DispatchOutcome(exit_code=exit_code, changed_files=[], launched=False)

    return _dispatch


def test_dispatch_child_holds_the_node_with_the_reason_it_printed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, store: Any
) -> None:
    """A pricing/credential refusal dies in the driver's DEVNULL: the ONLY way
    the operator learns why is the hold the child leaves on the node."""
    _wire_dispatch_child(
        monkeypatch,
        tmp_path,
        store,
        dispatch=_refusal_dispatch(
            category="exception",
            message=(
                "Exception during dispatch of #42: No pricing entry for model "
                "'openrouter/x': a governed dispatch would meter it at $0."
            ),
        ),
    )

    result = runner.invoke(epic_app, ["_dispatch-node", "dag-1", "42"])

    assert result.exit_code == 2
    holds = store.list_epic_holds("dag-1")
    assert [h.item_id for h in holds] == ["42"]
    assert holds[0].reason.startswith("admission_refused: ")
    assert "No pricing entry for model 'openrouter/x'" in holds[0].reason
    assert "kew epic unhold" in holds[0].reason


def test_dispatch_child_refusal_reconciles_the_drivers_reservation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, store: Any
) -> None:
    """The refusal row is keyed to the reservation the driver took and is
    `refused`, not `failure`: the node is re-admittable once un-held, and the
    dead-child reconciler never mistakes a clean refusal for a corpse."""
    store.reserve_epic_node_item("dag-1", "42", "run-abc")
    cfg = _wire_dispatch_child(
        monkeypatch,
        tmp_path,
        store,
        dispatch=_refusal_dispatch(
            category="transport_admission_refused",
            message="Switchyard route admission refused: missing credential",
        ),
    )

    result = runner.invoke(
        epic_app, ["_dispatch-node", "dag-1", "42", "--reservation-run-id", "run-abc"]
    )

    assert result.exit_code == 2
    rows = [r for r in store.list_all() if r.run_id == "run-abc"]
    assert len(rows) == 1
    assert rows[0].status == "refused"
    assert rows[0].error_category == "admission_refused"

    from datetime import UTC, datetime

    from kew.graph.driver.tick import reconcile_epic_reservations

    still_reserved = reconcile_epic_reservations(
        "dag-1", store, cfg, now=datetime.now(UTC)
    )

    assert still_reserved == set()


def test_dispatch_child_boundary_runner_refusal_holds_the_node(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, store: Any
) -> None:
    """A runner refusal raised at the dispatch boundary (the governor's final
    route, after the child's own early preflight passed) must hold the node
    like any other pre-spawn refusal. The category is new, so it had to be
    added to the holdable set; without it the child wrote no hold and no
    refusal row (round-5 review). Mutation caught: dropping the category
    from _HOLDABLE_REFUSAL_CATEGORIES."""
    store.reserve_epic_node_item("dag-1", "42", "run-abc")
    reason = "Runner 'codex' is not logged in. Authenticate with: codex login"
    _wire_dispatch_child(
        monkeypatch,
        tmp_path,
        store,
        dispatch=_refusal_dispatch(
            category="runner_preflight_refused",
            message=f"Runner preflight refused: {reason}",
        ),
    )

    result = runner.invoke(
        epic_app, ["_dispatch-node", "dag-1", "42", "--reservation-run-id", "run-abc"]
    )

    assert result.exit_code == 2
    holds = store.list_epic_holds("dag-1")
    assert [h.item_id for h in holds] == ["42"]
    assert holds[0].reason.startswith("admission_refused: ")
    assert reason in holds[0].reason
    rows = [r for r in store.list_all() if r.run_id == "run-abc"]
    assert len(rows) == 1 and rows[0].status == "refused"


def test_dispatch_child_publication_configuration_refusal_holds_the_node(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, store: Any
) -> None:
    """A checkout-publication refusal needs an operator configuration change."""
    store.reserve_epic_node_item("dag-1", "42", "run-abc")
    reason = "checkout publication requires explicit host-risk admission"
    _wire_dispatch_child(
        monkeypatch,
        tmp_path,
        store,
        dispatch=_refusal_dispatch(
            category="publication_configuration_refused",
            message=f"Publication configuration refused: {reason}",
        ),
    )

    result = runner.invoke(
        epic_app, ["_dispatch-node", "dag-1", "42", "--reservation-run-id", "run-abc"]
    )

    assert result.exit_code == 2
    holds = store.list_epic_holds("dag-1")
    assert [hold.item_id for hold in holds] == ["42"]
    assert holds[0].reason.startswith("admission_refused: ")
    assert reason in holds[0].reason
    rows = [row for row in store.list_all() if row.run_id == "run-abc"]
    assert len(rows) == 1 and rows[0].status == "refused"


def test_dispatch_child_prompt_configuration_refusal_holds_the_node(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, store: Any
) -> None:
    """A custom template beside an active profile needs an operator change."""
    store.reserve_epic_node_item("dag-1", "42", "run-abc")
    reason = (
        "the dispatch prompt profile v1 is active and only the built-in prompt "
        "renders it; retire the profile or remove [prompt] template from kew.toml"
    )
    _wire_dispatch_child(
        monkeypatch,
        tmp_path,
        store,
        dispatch=_refusal_dispatch(
            category="prompt_configuration_refused",
            message=f"Prompt configuration refused: {reason}",
            exit_code=2,
        ),
    )

    result = runner.invoke(
        epic_app, ["_dispatch-node", "dag-1", "42", "--reservation-run-id", "run-abc"]
    )

    assert result.exit_code == 2
    holds = store.list_epic_holds("dag-1")
    assert [hold.item_id for hold in holds] == ["42"]
    assert reason in holds[0].reason
    rows = [row for row in store.list_all() if row.run_id == "run-abc"]
    assert len(rows) == 1 and rows[0].status == "refused"


def test_dispatch_child_github_auth_refusal_holds_the_node(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, store: Any
) -> None:
    """An invalid or expired gh token (401) needs an operator: hold, do not
    re-spawn every tick."""
    store.reserve_epic_node_item("dag-1", "42", "run-abc")
    _wire_dispatch_child(
        monkeypatch,
        tmp_path,
        store,
        dispatch=_refusal_dispatch(
            category="github_auth",
            message="gh: Bad credentials (HTTP 401)",
        ),
    )

    result = runner.invoke(
        epic_app, ["_dispatch-node", "dag-1", "42", "--reservation-run-id", "run-abc"]
    )

    assert result.exit_code == 2
    holds = store.list_epic_holds("dag-1")
    assert [h.item_id for h in holds] == ["42"]
    assert "Bad credentials" in holds[0].reason
    rows = [r for r in store.list_all() if r.run_id == "run-abc"]
    assert [r.status for r in rows] == ["refused"]


def test_dispatch_child_post_spawn_exception_stays_a_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, store: Any
) -> None:
    """A run row proves the agent spawned, despite an `exception` event."""
    from datetime import UTC, datetime

    from kew.dispatch_core import _emit_early_exit
    from kew.graph.driver.tick import resolve_reservation_run
    from kew.progress import _error
    from kew.run_store import RunRecord

    store.reserve_epic_node_item("dag-1", "42", "reservation-run")

    def _failed_dispatch(**kwargs: Any) -> DispatchOutcome:
        now = datetime.now(UTC)
        kwargs["store"].save(
            RunRecord(
                run_id="spawned-run",
                issue_num=42,
                item_id="42",
                branch="agent/issue-42",
                status="failure",
                exit_code=1,
                started_at=now,
                finished_at=now,
                estimated_cost_usd=3.21,
                error_category="exception",
            )
        )
        _error("Unexpected exception in run_tracked_agent: boom")
        _emit_early_exit(
            kwargs["store"],
            issue_num=42,
            item_id="42",
            complexity="medium",
            runner="claude",
            model="claude-opus-5",
            exit_code=1,
            duration_seconds=0.1,
            error_category="exception",
            repo="o/r",
            run_id="spawned-run",
            epic_ref=kwargs["epic_ref"],
        )
        return DispatchOutcome(exit_code=1, changed_files=[], run_id="spawned-run")

    _wire_dispatch_child(monkeypatch, tmp_path, store, dispatch=_failed_dispatch)

    result = runner.invoke(epic_app, ["_dispatch-node", "dag-1", "42"])

    assert result.exit_code == 1
    assert store.list_epic_holds("dag-1") == []
    assert store.get("reservation-run") is None
    assert [row.run_id for row in store.list_all()] == ["spawned-run"]
    assert all(row.error_category != "admission_refused" for row in store.list_all())
    reservation = store.list_epic_reservations("dag-1")[0]
    resolved = resolve_reservation_run(store, reservation)
    assert resolved is not None
    assert resolved.run_id == "spawned-run"
    assert (resolved.status, resolved.error_category) == ("failure", "exception")
    assert resolved.estimated_cost_usd == pytest.approx(3.21)


def test_dispatch_child_never_exits_zero_on_a_refusal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, store: Any
) -> None:
    """The regression itself (#1658): the child used to drop the outcome on the
    floor, so every refusal exited 0 and the driver read it as a clean spawn."""
    _wire_dispatch_child(
        monkeypatch,
        tmp_path,
        store,
        dispatch=_refusal_dispatch(
            category=None, message="Worker cap reached (3/3 live)", exit_code=1
        ),
    )

    result = runner.invoke(epic_app, ["_dispatch-node", "dag-1", "42"])

    assert result.exit_code == 2


def test_dispatch_child_leaves_a_transient_refusal_admissible(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, store: Any
) -> None:
    """A worker-cap/fleet-stop refusal clears on its own. Holding it would wedge
    a node the very next tick could run, so it is reported and left alone."""
    _wire_dispatch_child(
        monkeypatch,
        tmp_path,
        store,
        dispatch=_refusal_dispatch(
            category=None, message="Worker cap reached (3/3 live)", exit_code=1
        ),
    )

    runner.invoke(epic_app, ["_dispatch-node", "dag-1", "42"])

    assert store.list_epic_holds("dag-1") == []


def test_dispatch_child_never_overwrites_a_hold_the_refusal_itself_wrote(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, store: Any
) -> None:
    """A budget refusal writes its own richer, self-clearing hold. Replacing it
    with the generic one would strip the numbers AND the automatic un-hold."""
    budget_reason = (
        "budget_preflight: spent $9.00 > per_issue cap $5.00 — raise "
        "budget.per_issue_usd in kew.toml"
    )

    def _dispatch(**kwargs: Any) -> DispatchOutcome:
        kwargs["store"].hold_epic_node("dag-1", "42", budget_reason)
        return DispatchOutcome(exit_code=1, changed_files=[], launched=False)

    _wire_dispatch_child(monkeypatch, tmp_path, store, dispatch=_dispatch)

    result = runner.invoke(epic_app, ["_dispatch-node", "dag-1", "42"])

    assert result.exit_code == 2
    assert [h.reason for h in store.list_epic_holds("dag-1")] == [budget_reason]


def test_dispatch_child_ignores_a_stale_refusal_from_an_earlier_attempt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, store: Any
) -> None:
    """The category read is bounded to THIS invocation. An older refusal of the
    same node must not turn a fresh transient refusal into a permanent hold."""
    from kew.dispatch_core import _emit_early_exit

    _emit_early_exit(
        store,
        issue_num=42,
        item_id="42",
        complexity="medium",
        runner="claude",
        model="claude-opus-5",
        exit_code=1,
        duration_seconds=0.1,
        error_category="exception",
        repo="o/r",
        epic_ref="dag-1",
    )
    _wire_dispatch_child(
        monkeypatch,
        tmp_path,
        store,
        dispatch=_refusal_dispatch(
            category=None, message="Worker cap reached (3/3 live)"
        ),
    )

    runner.invoke(epic_app, ["_dispatch-node", "dag-1", "42"])

    assert store.list_epic_holds("dag-1") == []


def test_dispatch_child_success_holds_nothing_and_exits_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, store: Any
) -> None:
    _wire_dispatch_child(
        monkeypatch,
        tmp_path,
        store,
        dispatch=lambda **_kw: DispatchOutcome(exit_code=0, changed_files=[]),
    )

    result = runner.invoke(epic_app, ["_dispatch-node", "dag-1", "42"])

    assert result.exit_code == 0
    assert store.list_epic_holds("dag-1") == []


def test_admission_refused_hold_keeps_the_node_out_of_the_frontier(
    store: Any,
) -> None:
    """The hold has to MEAN something: a refused node reads HELD, so the driver
    stops re-spawning a child that will be refused for the same reason."""
    from kew.graph.driver.overlay import DriverStatus, overlay_status

    status = overlay_status(
        None,
        [],
        reserved=False,
        held_reason="admission_refused: No pricing entry for model 'x'",
    )

    assert status is DriverStatus.HELD


def test_admission_refused_hold_has_a_stall_hint() -> None:
    from kew.cli.epic import _stall_dominant

    waiting = (
        StuckNode(
            "42",
            "admission_refused: No pricing entry for model 'x'",
            (),
        ),
    )

    assert _stall_dominant((), waiting) == (
        "1 held on admission refusal — fix the cause, then kew epic unhold"
    )


def test_driver_tick_logs_a_childs_admission_refusal(
    store: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Task 73's driver half: the hold reaches the driver log within one tick."""
    import logging

    from kew.graph.driver.tick import _log_admission_refusals

    store.hold_epic_node(
        "dag-1", "42", "admission_refused: No pricing entry for model 'x'"
    )
    store.hold_epic_node("dag-1", "43", "ci_pending")

    with caplog.at_level(logging.WARNING, logger="kew.graph.driver.tick"):
        _log_admission_refusals(store, "dag-1")

    logged = [r.getMessage() for r in caplog.records]
    assert any("No pricing entry for model 'x'" in m for m in logged)
    # Match on the reason, not the item number: the "(held since HH:MM:SS)"
    # suffix can contain any two digits.
    assert all("ci_pending" not in m for m in logged)


@pytest.mark.parametrize("exceptional", [False, True])
def test_resume_unwind_attests_release(store, tmp_path, exceptional):
    record = _claimed_continuation(store, tmp_path)

    def dispatch(*args):
        if exceptional:
            raise KeyboardInterrupt()
        return DispatchOutcome(exit_code=2)

    if exceptional:
        with pytest.raises(KeyboardInterrupt):
            resume_continuation(record, store=store, dispatch=dispatch)
    else:
        resume_continuation(record, store=store, dispatch=dispatch)
    events = store.list_audit_events(event="continuation_released")
    assert len(events) == 1
    assert events[0].run_id == record.continuation_run_id
    assert events[0].payload["generation"] == record.generation
    assert events[0].payload["reason"] == (
        "dispatch_exception" if exceptional else "dispatch_exit_2"
    )
    assert store.list_epic_reservations("alpha") == []
    # A stale callback cannot attest another release or remove a new owner.
    assert (
        store.claim_continuation_under_cap(
            "alpha",
            "42",
            "original-run",
            record.branch,
            record.worktree_identity,
            "new",
            0,
            claim_evidence=fixture_claim_evidence(
                store,
                "alpha",
                "42",
                "original-run",
                record.branch,
                record.worktree_identity,
                "new",
            ),
        )
        == "claimed"
    )
    resume_continuation(
        record, store=store, dispatch=lambda *args: DispatchOutcome(exit_code=1)
    )
    assert store.list_audit_events(event="continuation_released") == events
    assert store.list_epic_reservations("alpha")[0].run_id == "new"


def test_abandoned_dirty_node_dispatches_on_next_tick(store, tmp_path, monkeypatch):
    from datetime import UTC, datetime

    from support.driver_fakes import (
        EPIC,
        FakeFactsReader,
        FakeSource,
        _advance,
        _facts,
        _node,
    )

    from kew.audit import Actor, emit
    from kew.config import DispatchConfig
    from kew.run_store import RunRecord
    from kew.service import verbs

    abandon = getattr(verbs, "epic_abandon", None)
    assert callable(abandon), "the operator must be able to abandon preserved work"
    config = DispatchConfig()
    config.branches.worktree_base = str(tmp_path / "worktrees")
    config.project.repo = "o/r"
    worktree = tmp_path / "worktrees" / "agent" / "issue-42"
    worktree.mkdir(parents=True)
    (worktree / "edits.txt").write_text("old work")
    store.save(
        RunRecord(
            run_id="original",
            issue_num=42,
            item_id="42",
            branch="agent/issue-42",
            status="killed",
            exit_code=137,
            error_category="cost_breaker",
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
        )
    )
    # A breaker status alone cannot attest that the dispatch reaped its worker.
    emit(
        store,
        "dispatch_completed",
        actor=Actor("policy", "dispatch"),
        run_id="original",
        payload={"attempt_number": 1, "exit_code": 137},
    )
    store.record_continuation(
        EPIC,
        "42",
        "original",
        "agent/issue-42",
        worktree,
        checkout_generation=fixture_checkout_generation(worktree),
    )
    monkeypatch.setattr(
        "kew.graph.driver.tick.remote_branch_exists", lambda branch: False
    )
    monkeypatch.setattr(
        "kew.graph.driver.tick._validate_preexisting_worktree", lambda *args: True
    )

    # If abandonment fails to bypass detection, this reproduces the original
    # registration refusal. No child or external tool runs in this fixture.
    def cannot_register(*args, **kwargs):
        raise AssertionError("abandoned checkout was registered again")

    monkeypatch.setattr(
        "kew.graph.driver.tick._register_dirty_preexisting_worktree", cannot_register
    )
    sink = FakeSink()
    kwargs = dict(
        deps=FakeSource(nodes=[_node("42")], edges=[]),
        facts=FakeFactsReader(issue_facts_by_num={42: _facts(42, state="OPEN")}),
        store=store,
        sink=sink,
        lock_path=tmp_path / "epic.lock",
        config=config,
    )
    waiting = _advance(**kwargs)
    assert not waiting.dispatched
    assert any("kew epic abandon" in node.reason for node in waiting.waiting)
    from kew.worktree import checkout_claim_guard

    abandon(
        store,
        epic_ref=EPIC,
        item_id="42",
        reason="fresh start",
        config=config,
        checkout_guard=checkout_claim_guard,
    )
    result = _advance(**kwargs)
    assert result.dispatched == ("42",)
    assert sink.calls == ["42"]
    assert store.list_continuations(EPIC)[0].state == "terminal"
    assert (worktree / "edits.txt").read_text() == "old work"

    # A later failed checkout needs its own continuation. The old abandonment
    # must neither discard those edits nor make the selector ambiguous.
    from kew.graph.driver.tick import _preexisting_continuation

    store.save(
        RunRecord(
            run_id="next-original",
            issue_num=42,
            item_id="42",
            branch="agent/issue-42",
            status="failure",
            exit_code=1,
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
        )
    )
    emit(
        store,
        "checkout_replaced",
        actor=Actor("policy", "dispatch"),
        run_id="next-original",
        item_id="42",
        issue_num=42,
        payload={
            "epic_ref": EPIC,
            "item_id": "42",
            "branch": "agent/issue-42",
            "checkout": str(worktree),
            "checkout_id": "replacement-generation",
            "original_run_id": "original",
            "generation": 0,
            "abandonment_identity": store.continuation_abandonment_identity(
                store.list_continuations(EPIC)[0]
            ),
        },
    )
    assert (
        _preexisting_continuation(store, EPIC, "42", "agent/issue-42", worktree) is None
    )
    reservation = store.list_epic_reservations(EPIC)[0]
    store.release_epic_node_item("42", run_id=reservation.run_id)
    store.record_continuation(
        EPIC,
        "42",
        "next-original",
        "agent/issue-42",
        worktree,
        checkout_generation=fixture_checkout_generation(worktree),
    )
    continued = verbs.epic_resume(store, epic_ref=EPIC, item_id="42", config=config)
    assert continued.original_run_id == "next-original"


@pytest.mark.parametrize("intervening", ["refused", "dispatch_child_died"])
def test_abandonment_survives_unsuccessful_dispatch_tick(
    store, tmp_path, monkeypatch, intervening
):
    from datetime import UTC, datetime

    from support.driver_fakes import (
        EPIC,
        FakeFactsReader,
        FakeSource,
        _advance,
        _facts,
        _node,
    )

    from kew.config import DispatchConfig
    from kew.graph.driver.tick import _current_continuation
    from kew.run_store import RunRecord
    from kew.service import verbs
    from kew.worktree import checkout_claim_guard

    config = DispatchConfig()
    config.project.repo = "o/r"
    config.branches.worktree_base = str(tmp_path / "worktrees")
    worktree = tmp_path / "worktrees" / "agent" / "issue-42"
    worktree.mkdir(parents=True)
    (worktree / "edits.txt").write_text("abandoned work")
    store.save(
        RunRecord(
            run_id="original",
            issue_num=42,
            item_id="42",
            branch="agent/issue-42",
            status="failure",
            exit_code=1,
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
        )
    )
    store.record_continuation(
        EPIC,
        "42",
        "original",
        "agent/issue-42",
        worktree,
        checkout_generation=fixture_checkout_generation(worktree),
    )
    verbs.epic_abandon(
        store,
        epic_ref=EPIC,
        item_id="42",
        reason="fresh start",
        config=config,
        checkout_guard=checkout_claim_guard,
    )
    monkeypatch.setattr("kew.graph.driver.tick.remote_branch_exists", lambda _: False)
    monkeypatch.setattr(
        "kew.graph.driver.tick._validate_preexisting_worktree", lambda *args: True
    )

    # Reaching registration would reuse the abandoned terminal identity.
    def register_again(*args, **kwargs):
        raise AssertionError("continuation identity changed during registration")

    monkeypatch.setattr(
        "kew.graph.driver.tick._register_dirty_preexisting_worktree", register_again
    )
    sink = FakeSink()
    kwargs = dict(
        deps=FakeSource(nodes=[_node("42")], edges=[]),
        facts=FakeFactsReader(issue_facts_by_num={42: _facts(42, state="OPEN")}),
        store=store,
        sink=sink,
        lock_path=tmp_path / "epic.lock",
        config=config,
    )
    first = _advance(**kwargs)
    assert first.dispatched == ("42",)
    reservation = store.list_epic_reservations(EPIC)[0]
    # The detached child either refuses budget preflight or never starts.
    store.save(
        RunRecord(
            run_id=reservation.run_id,
            issue_num=42,
            item_id="42",
            branch="agent/issue-42",
            status="refused" if intervening == "refused" else "failure",
            exit_code=1,
            error_category="budget_preflight"
            if intervening == "refused"
            else intervening,
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
        )
    )
    store.release_epic_node_item("42", run_id=reservation.run_id)
    record = store.list_continuations(EPIC)[0]
    assert _current_continuation(store, record, "42"), (
        "failed dispatch must retain abandonment"
    )
    before = (
        store.list_continuations(EPIC),
        store.read_chain_rows(),
        store.list_epic_retries(EPIC),
    )
    with pytest.raises(verbs.EpicAbandonDenied, match="not awaiting"):
        verbs.epic_abandon(
            store,
            epic_ref=EPIC,
            item_id="42",
            reason="again",
            config=config,
            checkout_guard=checkout_claim_guard,
        )
    assert before == (
        store.list_continuations(EPIC),
        store.read_chain_rows(),
        store.list_epic_retries(EPIC),
    )
    if intervening == "dispatch_child_died":
        # Ordinary failure admission still requires an operator retry.
        store.mark_epic_retry_item(EPIC, "42")
    second = _advance(**kwargs)
    assert second.dispatched == ("42",)
    assert sink.calls == ["42", "42"]
    assert (worktree / "edits.txt").read_text() == "abandoned work"


def _abandoned_dispatch_fixture(store, tmp_path, monkeypatch):
    from datetime import UTC, datetime

    from support.driver_fakes import EPIC, FakeFactsReader, FakeSource, _facts, _node

    from kew.config import DispatchConfig
    from kew.run_store import RunRecord
    from kew.service import verbs
    from kew.worktree import checkout_claim_guard

    config = DispatchConfig()
    config.project.repo = "o/r"
    config.publication.publish = "checkout"
    config.branches.worktree_base = str(tmp_path / "worktrees")
    checkout = tmp_path / "worktrees" / "agent" / "issue-42"
    checkout.mkdir(parents=True, exist_ok=True)
    (checkout / "edits.txt").write_text("abandoned work")
    store.save(
        RunRecord(
            run_id="original",
            issue_num=42,
            item_id="42",
            branch="agent/issue-42",
            status="failure",
            exit_code=1,
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
        )
    )
    store.record_continuation(
        EPIC,
        "42",
        "original",
        "agent/issue-42",
        checkout,
        checkout_generation="",  # Historical continuation used only for abandonment.,
    )
    verbs.epic_abandon(
        store,
        epic_ref=EPIC,
        item_id="42",
        reason="fresh start",
        config=config,
        checkout_guard=checkout_claim_guard,
    )
    monkeypatch.setattr("kew.graph.driver.tick.remote_branch_exists", lambda _: False)
    return checkout, dict(
        deps=FakeSource(nodes=[_node("42")], edges=[]),
        facts=FakeFactsReader(issue_facts_by_num={42: _facts(42, state="OPEN")}),
        store=store,
        sink=FakeSink(),
        lock_path=tmp_path / "epic.lock",
        config=config,
    )


def test_publication_refusal_keeps_abandonment_expired_across_two_ticks(
    store, tmp_path, monkeypatch
):
    from dataclasses import replace
    from datetime import UTC, datetime

    from support.driver_fakes import EPIC, _advance

    from kew.audit import Actor, emit
    from kew.graph.driver.tick import _current_continuation
    from kew.run_store import RunRecord

    checkout, kwargs = _abandoned_dispatch_fixture(store, tmp_path, monkeypatch)
    monkeypatch.setattr(
        "kew.graph.driver.tick._validate_preexisting_worktree", lambda *a: True
    )
    assert _advance(**kwargs).dispatched == ("42",)
    replacement = store.list_epic_reservations(EPIC)[0].run_id
    store.save(
        RunRecord(
            run_id=replacement,
            issue_num=42,
            item_id="42",
            branch="agent/issue-42",
            status="refused",
            exit_code=1,
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
            error_category="credential_publication_refused",
        )
    )
    abandoned = store.list_continuations(EPIC)[0]
    assert _current_continuation(store, abandoned, "42")
    emit(
        store,
        "checkout_replaced",
        actor=Actor("policy", "dispatch"),
        run_id=replacement,
        item_id="42",
        issue_num=42,
        payload={
            "epic_ref": EPIC,
            "item_id": "42",
            "branch": "agent/issue-42",
            "checkout": str(checkout),
            "checkout_id": "replacement-generation",
            "original_run_id": abandoned.original_run_id,
            "generation": abandoned.generation,
            "abandonment_identity": store.continuation_abandonment_identity(abandoned),
        },
    )
    assert _current_continuation(store, abandoned, "42")
    store.save(
        replace(store.get(replacement), session_id="executed-session", total_tokens=123)
    )
    (checkout / "edits.txt").write_text("replacement worker edits")
    store.release_epic_node_item("42", run_id=replacement)
    abandoned = store.list_continuations(EPIC)[0]
    assert not _current_continuation(store, abandoned, "42"), (
        "post-execution publication refusal must not reactivate abandonment"
    )
    registered = []

    def register(store, config, **kw):
        registered.append(kw["candidate"])
        assert (
            store.register_continuation_and_release_reservation(
                kw["epic_ref"],
                kw["item_id"],
                replacement,
                kw["expected_branch"],
                kw["candidate"],
                kw["reservation_run_id"],
                checkout_generation=fixture_checkout_generation(kw["candidate"]),
            )
            is not None
        )
        return "registered"

    monkeypatch.setattr(
        "kew.graph.driver.tick._register_dirty_preexisting_worktree", register
    )
    second = _advance(**kwargs)
    assert second.dispatched == ()
    assert registered == [checkout]
    assert {r.original_run_id for r in store.list_continuations(EPIC)} == {
        "original",
        replacement,
    }
    assert (checkout / "edits.txt").read_text() == "replacement worker edits"


def test_checkout_abandon_next_tick_quarantines_real_dirty_checkout(
    store, tmp_path, monkeypatch
):
    import shutil

    from support.driver_fakes import EPIC, _advance
    from test_dispatch_core import TestInitialCanonicalCheckoutBinding
    from test_worktree import _origin_repo_with_branch

    from kew import dispatch_core, worktree
    from kew.config import DispatchConfig

    monkeypatch.setitem(TOOL_PATHS, "git", shutil.which("git"))
    repo = _origin_repo_with_branch(tmp_path, "agent/issue-42")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(worktree, "_repo_root", lambda: repo)
    config = DispatchConfig()
    config.publication.publish = "checkout"
    config.branches.base = "main"
    config.branches.worktree_base = str(tmp_path / "worktrees")
    worktree.setup_worktree(issue_num=42, config=config)
    checkout, kwargs = _abandoned_dispatch_fixture(store, tmp_path, monkeypatch)
    kwargs["config"].branches.base = "main"
    failures = []
    launched = []
    monkeypatch.setattr(dispatch_core, "_emit_host_setup_posture", lambda *a: None)

    class HostSink:
        def dispatch(self, item_id):
            ctx = TestInitialCanonicalCheckoutBinding._context(
                dispatch_core,
                kwargs["config"],
                store,
                run_id=store.list_epic_reservations(EPIC)[0].run_id,
            )
            ctx.epic_ref = EPIC
            try:
                fresh = dispatch_core._setup_host_worktree(
                    ctx, kwargs["config"], "codex", MagicMock()
                )
            except RuntimeError as exc:
                failures.append(str(exc))
                return False
            dispatch_core._bind_initial_checkout(ctx, fresh)
            launched.append(fresh)
            return True

    kwargs["sink"] = HostSink()
    try:
        result = _advance(**kwargs)
        assert result.dispatched == ("42",), failures
        assert launched == [checkout], failures
        assert not (checkout / "edits.txt").exists()
        preserved = list(checkout.parent.glob("issue-42.replaced-*"))
        assert len(preserved) == 1
        assert (preserved[0] / "edits.txt").read_text() == "abandoned work"
        assert worktree.is_safe_standalone_clone(checkout)
        abandonment = store.list_audit_events(event="continuation_abandoned")[0]
        replacement = store.list_audit_events(event="checkout_replaced")[0]
        assert (
            replacement.payload["abandonment_identity"]
            == abandonment.payload["evidence_identity"]
        )
        assert store.list_continuations(EPIC)[0].state == "terminal"
    finally:
        worktree.release_checkout_claims()


@pytest.fixture
def round18_tick_checkout(store, tmp_path, monkeypatch):
    import shutil

    from test_worktree import _origin_repo_with_branch

    from kew import worktree
    from kew.config import DispatchConfig

    monkeypatch.setitem(TOOL_PATHS, "git", shutil.which("git"))
    repo = _origin_repo_with_branch(tmp_path, "agent/issue-42")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(worktree, "_repo_root", lambda: repo)
    config = DispatchConfig()
    config.branches.base = "main"
    config.branches.worktree_base = str(tmp_path / "worktrees")
    worktree.setup_worktree(issue_num=42, config=config)
    recorded, kwargs = _abandoned_dispatch_fixture(store, tmp_path, monkeypatch)
    kwargs["config"].branches.base = "main"
    shutil.rmtree(Path(config.branches.worktree_base))
    try:
        yield recorded, kwargs
    finally:
        worktree.release_checkout_claims()


@pytest.mark.serial
@pytest.mark.parametrize("moved", [False, True], ids=["deleted-base", "moved-base"])
def test_round18_tick_launches_child_after_abandon_and_base_deletion(
    store, round18_tick_checkout, tmp_path, capsys, caplog, moved
):
    import sys

    from support.driver_fakes import EPIC, _advance
    from test_dispatch_core import (
        TestInitialCanonicalCheckoutBinding as CheckoutBinding,
    )

    from kew import dispatch_core, worktree

    recorded, kwargs = round18_tick_checkout
    config = kwargs["config"]
    if moved:
        config.branches.worktree_base = str(tmp_path / "moved-base")
    candidate = worktree.dispatch_checkout_path(config, issue_num=42)
    launched = []
    failures = []

    class HostSink:
        def dispatch(self, item_id):
            ctx = CheckoutBinding._context(
                dispatch_core,
                config,
                store,
                run_id=store.list_epic_reservations(EPIC)[0].run_id,
            )
            ctx.epic_ref = EPIC
            try:
                fresh = dispatch_core._setup_host_worktree(
                    ctx, config, "codex", CheckoutBinding._decision(sandboxed=False)
                )
                dispatch_core._bind_initial_checkout(ctx, fresh)
            except RuntimeError as exc:
                failures.append(str(exc))
                return False
            subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; Path('child-launched').write_text('42')",
                ],
                cwd=fresh,
                check=True,
            )
            launched.append((item_id, fresh))
            return True

    kwargs["sink"] = HostSink()
    result = _advance(**kwargs)
    assert not result.stuck, [node.reason for node in result.stuck]
    assert result.dispatched == ("42",), (result, failures)
    assert launched == [("42", candidate)]
    assert (candidate / "child-launched").read_text() == "42"
    payload = store.list_audit_events(event="checkout_replaced")[0].payload
    assert payload["rebound_from"] == str(recorded)
    assert payload["rebound_reason"] == "recorded checkout missing"
    assert store.continuation_expiry(store.list_continuations(EPIC)[0]) is not None
    output = capsys.readouterr()
    diagnostics = (
        repr((store.read_chain_rows(), store.list_epic_holds(EPIC), result, failures))
        + output.out
        + output.err
        + caplog.text
    )
    assert "abandonment authority does not match the replacement checkout" not in (
        diagnostics
    )


def test_dispatch_child_uses_parent_run_id_and_records_early_refusal(
    monkeypatch, tmp_path, store
):
    dispatch = MagicMock(return_value=DispatchOutcome(exit_code=0, changed_files=[]))
    _wire_dispatch_child(monkeypatch, tmp_path, store, dispatch=dispatch)
    result = runner.invoke(
        epic_app, ["_dispatch-node", "dag-1", "42", "--run-id", "parent-run"]
    )
    assert result.exit_code == 0
    assert dispatch.call_args.kwargs["run_id"] == "parent-run"
    # The fake produces no run row; the child closes the parent's unused identity.
    assert store.get("parent-run").error_category == "dispatch_child_refused"


def _round6_route_config(tmp_path, runner_name, model, sidecar):
    from kew.config import DispatchConfig

    values = {"agent": {"runner": runner_name, "model": model}}
    if sidecar:
        routes = tmp_path / "routes.toml"
        routes.write_text("schema_version = 1\n")
        values["inference_transport"] = {
            "mode": "switchyard",
            "switchyard": {
                "route": model,
                "config_path": str(routes),
                "expected_version": "test-version",
                "credential_env": ["OPENROUTER_API_KEY"],
                "sidecar": {
                    "enabled": True,
                    "image": "test-proxy",
                    "egress_network": "test-egress",
                },
            },
        }
    return DispatchConfig.model_validate(values)


_ROUND6_ROUTES = [
    ("claude-code", "claude-sonnet-4-6", False, False),
    ("codex", "gpt-5.4", False, False),
    ("opencode", "openrouter/test", False, True),
    ("claude-code", "switchyard/passthrough", True, False),
    ("claude-code", "openrouter/test", True, False),
]


@pytest.mark.parametrize("runner_name,model,sidecar,issued", _ROUND6_ROUTES)
@pytest.mark.parametrize("provisioning", [True, False])
def test_round7_detached_route_matrix(
    store,
    driver_config,
    tmp_path,
    monkeypatch,
    runner_name,
    model,
    sidecar,
    issued,
    provisioning,
):
    from kew import credential_issuer

    config = _round6_route_config(tmp_path, runner_name, model, sidecar)
    for name in _round8_inference_names():
        monkeypatch.setenv(name, "operator-secret-1944")
    monkeypatch.setattr(
        "kew.graph.driver.dispatch.resolve_kew_console_script", lambda: "/bin/kew"
    )
    if not provisioning:
        monkeypatch.delenv("OPENROUTER_PROVISIONING_KEY")
    spawn = MagicMock()
    sink = DetachedDispatchSink(
        epic_ref="alpha",
        config=config,
        store=store,
        popen=spawn,
        publication_token=lambda: None,
    )
    if issued and not provisioning:
        with pytest.raises(credential_issuer.IssuerUnavailable):
            sink.dispatch("1944")
        spawn.assert_not_called()
        event = store.list_audit_events(event="dispatch_completed")[0]
        assert (event.payload.get("credential_route") or {}).get("bounded_key") is True
        return
    assert sink.dispatch("1944")
    env = spawn.call_args.kwargs["env"]
    required = _round8_expected_names(runner_name, sidecar, issued)
    _round8_assert_child_env(env, issued, required)
    argv = spawn.call_args.args[0]
    run_id = argv[argv.index("--run-id") + 1]
    event = store.list_audit_events(event="sandbox_resolved", run_id=run_id)[0]
    assert event.payload.get("credential_route") == {
        "runner": runner_name,
        "model": model,
        "transport": "sidecar" if sidecar else "direct" if issued else "native",
        "sidecar": sidecar,
        "bounded_key": issued,
        "required_credential_names": sorted(required),
        "claude_auth_mode": "anthropic-api"
        if runner_name == "claude-code" and not sidecar
        else None,
    }
    _round6_assert_inheritance(store, config, env, run_id, issued, monkeypatch)


def _round8_assert_child_env(env, issued, required):
    if issued:
        assert env["OPENROUTER_API_KEY"] == "bounded-secret-1944"
    else:
        # An excluded handoff keeps exactly the route's required names, the
        # declared upstream credential included (governed review round 2).
        assert _round8_inference_names().intersection(env) == required
    assert ("KEW_ISSUED_RUN_KEY_HASH" in env) == issued
    assert "OPENROUTER_PROVISIONING_KEY" not in env


def _round6_assert_inheritance(store, config, env, run_id, issued, monkeypatch):
    from kew import credential_issuer
    from kew.inference_transport import resolve_inference_transport
    from kew.run_credentials import bounded_run_credentials
    from kew.runners import resolve_runner

    monkeypatch.delenv("OPENROUTER_PROVISIONING_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr("os.environ", env)
    route = resolve_runner(config)
    transport = resolve_inference_transport(config, route.runner, route.model)
    with bounded_run_credentials(
        run_id=run_id,
        config=config,
        store=store,
        model=transport.model,
        credential_names=tuple(route.runner.required_credentials),
        direct=transport.mode == "direct",
        timeout_seconds=60,
    ) as key:
        assert (key is not None) == issued
    assert credential_issuer.mint_bounded_run_key.call_count == int(issued)
    credential_issuer.revoke_run_key.assert_not_called()


@pytest.mark.parametrize("runner_name,model,sidecar,issued", _ROUND6_ROUTES)
def test_round6_child_refuses_marker_on_excluded_route(
    store,
    driver_config,
    tmp_path,
    monkeypatch,
    runner_name,
    model,
    sidecar,
    issued,
):
    from kew.graph.driver.credentials import issue_driver_key
    from kew.inference_transport import resolve_inference_transport
    from kew.run_credentials import IssuedCredentialMismatch, bounded_run_credentials
    from kew.runners import resolve_runner

    key = issue_driver_key(store, driver_config, "route-change", "1944")
    config = _round6_route_config(tmp_path, runner_name, model, sidecar)
    monkeypatch.setenv("OPENROUTER_API_KEY", key.secret)
    monkeypatch.setenv("KEW_ISSUED_RUN_KEY_HASH", key.key_hash)
    monkeypatch.delenv("OPENROUTER_PROVISIONING_KEY")
    route = resolve_runner(config)
    transport = resolve_inference_transport(config, route.runner, route.model)
    context = bounded_run_credentials(
        run_id="route-change",
        config=config,
        store=store,
        model=transport.model,
        credential_names=tuple(route.runner.required_credentials),
        direct=transport.mode == "direct",
        timeout_seconds=60,
    )
    if issued:
        with context as inherited:
            assert inherited == key
    else:
        with pytest.raises(IssuedCredentialMismatch), context:
            pytest.fail("an excluded route inherited the driver's key")


@pytest.mark.parametrize("provider", ["github", "linear"])
@pytest.mark.parametrize(
    "model,issued", [("claude-sonnet-4-6", False), ("openrouter/test", True)]
)
def test_round6_parent_uses_item_complexity(
    store,
    driver_config,
    monkeypatch,
    provider,
    model,
    issued,
):
    from kew import credential_issuer
    from kew.config import RouteEntry, RoutingConfig

    driver_config.routing = RoutingConfig(
        default=RouteEntry(runner="codex", model="gpt-5.4"),
        simple=RouteEntry(model=model),
    )
    driver_config.tracker.type = provider
    if provider == "linear":
        # The Linear tracker needs a credential; never depend on the host's.
        monkeypatch.setenv("LINEAR_API_KEY", "linear-test-credential")
    fetch = MagicMock(return_value={"labels": [{"name": "simple"}]})
    monkeypatch.setattr("kew.selector.select_next_issue", fetch)
    monkeypatch.setattr("kew.linear.tracker.LinearTracker.fetch_issue", fetch)
    monkeypatch.setattr(
        "kew.graph.driver.dispatch.resolve_kew_console_script", lambda: "/bin/kew"
    )
    spawn = MagicMock()
    sink = DetachedDispatchSink(
        epic_ref="alpha",
        config=driver_config,
        store=store,
        popen=spawn,
        publication_token=lambda: None,
    )
    assert sink.dispatch("1944" if provider == "github" else "SFD-1944")
    fetch.assert_called_once()
    assert credential_issuer.mint_bounded_run_key.call_count == int(issued)
    assert ("KEW_ISSUED_RUN_KEY_HASH" in spawn.call_args.kwargs["env"]) == issued


def test_round6_excluded_route_drops_ambient_issued_key(
    store,
    driver_config,
    monkeypatch,
):
    from kew import credential_issuer
    from kew.config import DispatchConfig

    monkeypatch.setenv("OPENROUTER_API_KEY", "bounded-secret-1944")
    monkeypatch.setenv("KEW_ISSUED_RUN_KEY_HASH", "a" * 64)
    monkeypatch.setattr(
        "kew.graph.driver.dispatch.resolve_kew_console_script", lambda: "/bin/kew"
    )
    spawn = MagicMock()
    sink = DetachedDispatchSink(
        epic_ref="alpha",
        config=DispatchConfig(),
        store=store,
        popen=spawn,
        publication_token=lambda: None,
    )
    assert sink.dispatch("1944")
    env = spawn.call_args.kwargs["env"]
    assert "OPENROUTER_API_KEY" not in env
    assert "KEW_ISSUED_RUN_KEY_HASH" not in env
    assert "OPENROUTER_PROVISIONING_KEY" not in env
    credential_issuer.mint_bounded_run_key.assert_not_called()


def _round7_sink(store, config, monkeypatch, spawn):
    monkeypatch.setattr(
        "kew.graph.driver.dispatch.resolve_kew_console_script", lambda: "/bin/kew"
    )
    return DetachedDispatchSink(
        epic_ref="dag-1",
        config=config,
        store=store,
        popen=spawn,
        publication_token=lambda: None,
    )


def test_round7_excluded_real_child_has_no_inference_credentials(
    store, driver_config, monkeypatch, tmp_path
):
    import json
    import sys

    from kew.runners.codex import CODEX_CREDENTIAL_ENV_NAMES
    from kew.subprocess_utils import popen

    names = CODEX_CREDENTIAL_ENV_NAMES - {"GH_TOKEN", "GITHUB_TOKEN"}
    names |= {"LEMONADE_API_KEY", "OPENROUTER_PROVISIONING_KEY"}
    for name in names:
        monkeypatch.setenv(name, "operator-secret-1944")
    output = tmp_path / "child-environment.json"

    def launch(_cmd, **kwargs):
        # Exercise the sink's normal filtered launcher with an actual process.
        code = (
            "import json, os, pathlib; "
            f"pathlib.Path({str(output)!r}).write_text(json.dumps(dict(os.environ)))"
        )
        monkeypatch.setitem(TOOL_PATHS, "python", sys.executable)
        process = popen(["python", "-c", code], **kwargs)
        assert process.wait(timeout=10) == 0
        return process

    config = _round6_route_config(tmp_path, "codex", "gpt-5.4", False)
    sink = _round7_sink(store, config, monkeypatch, launch)
    assert sink.dispatch("42")
    environment = json.loads(output.read_text())
    assert "OPENROUTER_API_KEY" not in environment
    assert "KEW_ISSUED_RUN_KEY_HASH" not in environment
    assert names.isdisjoint(environment)


def _round7_native_digest():
    import hashlib
    import json

    values = {
        "runner": "claude-code",
        "model": "claude-sonnet-4-6",
        "transport": "native",
        "sidecar": False,
        "bounded_key": False,
        "claude_auth_mode": "anthropic-api",
        "required_credential_names": sorted(
            _round8_expected_names("claude-code", False, False)
        ),
    }
    return hashlib.sha256(
        json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _round7_assert_refusal(result, store, launch, reason):
    assert result.exit_code == 2, result.output
    assert reason in result.output
    launch.assert_not_called()
    assert store.list_epic_reservations("dag-1") == []
    row = store.get("parent-run")
    assert row.status == "refused"
    assert row.exit_code == 2
    assert reason in store.list_epic_holds("dag-1")[0].reason
    event = store.list_audit_events(event="dispatch_completed")[0]
    assert event.payload["reason"] == reason


def test_round7_changed_labels_refuse_before_launch(store, tmp_path, monkeypatch):
    from kew.runners import resolve_runner

    launch = MagicMock()
    config = _wire_dispatch_child(monkeypatch, tmp_path, store, dispatch=launch)
    store.reserve_epic_node_item("dag-1", "42", "parent-run")
    # The parent's native decision precedes a label-driven OpenRouter resolution.
    direct = cli.DispatchConfig.model_validate({"agent": {"model": "openrouter/test"}})
    resolver = MagicMock(return_value=resolve_runner(direct))
    monkeypatch.setattr(cli, "resolve_runner", resolver)
    monkeypatch.setattr(
        cli,
        "select_next_issue",
        lambda **kw: {
            "number": 42,
            "title": "changed labels",
            "labels": [{"name": "complex"}],
        },
    )
    result = runner.invoke(
        epic_app,
        [
            "_dispatch-node",
            "dag-1",
            "42",
            "--reservation-run-id",
            "parent-run",
            "--reservation-generation",
            "parent-run",
            "--run-id",
            "parent-run",
            "--credential-route",
            _round7_native_digest(),
        ],
    )
    _round7_assert_refusal(result, store, launch, "credential route changed")
    resolver.assert_called_once_with(config, complexity="complex")


def test_round7_store_routing_override_refuses_before_launch(
    store, tmp_path, monkeypatch
):
    from kew.config import load_config
    from kew.graph.driver.credential_route import resolve_driver_credential_route

    launch = MagicMock()
    _wire_dispatch_child(monkeypatch, tmp_path, store, dispatch=launch)
    path = tmp_path / "route.toml"
    path.write_text(
        '[routing]\ndefault = { runner = "claude-code", model = "claude-sonnet-4-6" }\n'
    )
    monkeypatch.setattr("kew.selector.select_next_issue", lambda **kw: {"labels": []})
    parent = resolve_driver_credential_route(load_config(path), "42")
    # This assertion also proves the digest's canonical encoding independently.
    assert getattr(parent, "digest", None) == _round7_native_digest()
    store.set_config_override(
        field="routing.default",
        value_json='{"runner":"opencode","model":"openrouter/z-ai/glm-5.2"}',
        file_value_json='{"runner":"claude-code","model":"claude-sonnet-4-6"}',
        set_at="2026-09-20T00:00:00+00:00",
        set_by="test",
    )
    monkeypatch.setattr(cli, "load_config", lambda **kw: load_config(path, **kw))
    store.reserve_epic_node_item("dag-1", "42", "parent-run")
    result = runner.invoke(
        epic_app,
        [
            "_dispatch-node",
            "dag-1",
            "42",
            "--reservation-run-id",
            "parent-run",
            "--reservation-generation",
            "parent-run",
            "--run-id",
            "parent-run",
            "--credential-route",
            _round7_native_digest(),
        ],
    )
    _round7_assert_refusal(result, store, launch, "credential route changed")


@pytest.mark.parametrize("flag", ["--run-id", "--credential-route"])
def test_round7_detached_direct_without_marker_never_uses_host_key(
    store, tmp_path, monkeypatch, flag
):
    from kew.run_credentials import bounded_run_credentials, launch_environment

    agent_environments = []
    launch = MagicMock()

    def dispatch(**kwargs):
        with bounded_run_credentials(
            run_id="parent-run",
            config=kwargs["config"],
            store=store,
            model="openrouter/test",
            credential_names=("OPENROUTER_API_KEY",),
            timeout_seconds=60,
        ) as key:
            agent_environments.append(launch_environment(dict(os.environ), key))
            launch()
        return DispatchOutcome(exit_code=0, changed_files=[])

    config = _wire_dispatch_child(monkeypatch, tmp_path, store, dispatch=dispatch)
    config.agent.model = "openrouter/test"
    monkeypatch.setenv("OPENROUTER_API_KEY", "operator-secret-1944")
    monkeypatch.delenv("OPENROUTER_PROVISIONING_KEY", raising=False)
    monkeypatch.delenv("KEW_ISSUED_RUN_KEY_HASH", raising=False)
    store.reserve_epic_node_item("dag-1", "42", "parent-run")
    if flag == "--credential-route":
        from kew.graph.driver.credential_route import resolve_driver_credential_route

        decision = resolve_driver_credential_route(config, "42")
        value = getattr(decision, "digest", "missing-parent-digest")
    else:
        value = "parent-run"
    result = runner.invoke(
        epic_app,
        [
            "_dispatch-node",
            "dag-1",
            "42",
            "--reservation-run-id",
            "parent-run",
            "--reservation-generation",
            "parent-run",
            flag,
            value,
        ],
    )
    assert all("operator-secret-1944" not in env.values() for env in agent_environments)
    _round7_assert_refusal(result, store, launch, "issued credential mismatch")


def _round7_admit_without_worker(store, config, monkeypatch):
    from types import SimpleNamespace

    from kew import dispatch_core

    workspace = MagicMock(return_value=object())
    monkeypatch.setattr(dispatch_core, "_preflight_pricing", lambda *a: None)
    monkeypatch.setattr(dispatch_core, "_launch_workspace", workspace)

    def dispatch(**kwargs):
        ctx = SimpleNamespace(
            config=config,
            complexity=None,
            model_override=None,
            store=store,
            run_id=kwargs["run_id"],
            item_id="42",
        )
        result = dispatch_core._launch_runner(ctx, None)
        assert result.early is None
        return DispatchOutcome(exit_code=0, changed_files=[])

    return dispatch, workspace


@pytest.mark.parametrize("issued", [True, False])
def test_round7_handoff_digest_matches_issuance_and_child_admission(
    store, driver_config, tmp_path, monkeypatch, issued
):
    config = driver_config if issued else cli.DispatchConfig()
    spawn = MagicMock()
    sink = _round7_sink(store, config, monkeypatch, spawn)
    assert sink.dispatch("42")
    argv = spawn.call_args.args[0]
    digest, run_id, issuance = _round7_issuance_evidence(store, argv)
    dispatch, workspace = _round7_admit_without_worker(store, config, monkeypatch)
    _wire_dispatch_child(monkeypatch, tmp_path, store, dispatch=dispatch)
    monkeypatch.setattr(cli, "load_config", lambda **kw: config)
    result = runner.invoke(epic_app, argv[2:])
    assert result.exit_code == 0, result.output
    workspace.assert_called_once()
    admission = next(
        event.payload
        for event in store.list_audit_events(run_id=run_id, event="sandbox_resolved")
        if event.payload.get("phase") == "admission"
    )
    assert admission["credential_route_digest"] == digest
    assert admission["credential_route"] == issuance["credential_route"]


def _round7_issuance_evidence(store, argv):
    import hashlib
    import json

    assert "--credential-route" in argv
    digest = argv[argv.index("--credential-route") + 1]
    run_id = argv[argv.index("--run-id") + 1]
    issuance = store.list_audit_events(run_id=run_id, event="sandbox_resolved")[
        0
    ].payload
    assert issuance["credential_route_digest"] == digest
    assert (
        hashlib.sha256(
            json.dumps(
                issuance["credential_route"],
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        == digest
    )
    return digest, run_id, issuance


def _round8_inference_names():
    from kew.credential_handoff import CLAUDE_CREDENTIAL_NAMES
    from kew.runners.codex import CODEX_CREDENTIAL_ENV_NAMES

    return (CODEX_CREDENTIAL_ENV_NAMES | CLAUDE_CREDENTIAL_NAMES) - {
        "GH_TOKEN",
        "GITHUB_TOKEN",
    } | {"LEMONADE_API_KEY"}


def _round8_expected_names(runner_name, sidecar, issued):
    from kew.credential_handoff import CLAUDE_MODE_CREDENTIAL_NAMES

    if sidecar or issued:
        return frozenset({"OPENROUTER_API_KEY"})
    if runner_name == "claude-code":
        return CLAUDE_MODE_CREDENTIAL_NAMES["anthropic-api"]
    return frozenset()


def _round8_real_child(store, config, tmp_path, monkeypatch):
    import json
    import sys

    from kew.subprocess_utils import popen

    output = tmp_path / "round8-child.json"
    # The child's WHOLE environment used to land in this file, so a real
    # OPENROUTER_API_KEY belonging to the operator was written to a pytest
    # temp dir on every run (#2197). Report the names these tests assert on
    # and nothing else — the assertions are about presence and absence within
    # that set, so nothing outside it was ever read.
    reported = sorted(
        _round8_inference_names()
        | {"OPENROUTER_PROVISIONING_KEY", "KEW_ISSUED_RUN_KEY_HASH"}
    )

    def launch(_cmd, **kwargs):
        code = (
            "import json, os, pathlib; "
            f"pathlib.Path({str(output)!r}).write_text(json.dumps("
            f"{{n: os.environ[n] for n in {reported!r} if n in os.environ}}))"
        )
        monkeypatch.setitem(TOOL_PATHS, "python", sys.executable)
        process = popen(["python", "-c", code], **kwargs)
        assert process.wait(timeout=10) == 0
        return process

    sink = _round7_sink(store, config, monkeypatch, launch)
    assert sink.dispatch("42")
    return json.loads(output.read_text())


def test_round8_native_claude_real_child_authenticates(
    store, driver_config, monkeypatch, tmp_path
):
    from kew.runner_preflight import runner_preflight_failure
    from kew.subprocess_utils import inspect_runner

    for name in _round8_inference_names():
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "claude-round8")
    config = _round6_route_config(tmp_path, "claude-code", "claude-sonnet-4-6", False)
    environment = _round8_real_child(store, config, tmp_path, monkeypatch)
    assert environment.get("ANTHROPIC_API_KEY") == "claude-round8"
    assert _round8_inference_names().intersection(environment) == {"ANTHROPIC_API_KEY"}
    assert {"OPENROUTER_PROVISIONING_KEY", "KEW_ISSUED_RUN_KEY_HASH"}.isdisjoint(
        environment
    )
    monkeypatch.setattr("os.environ", environment)
    monkeypatch.setattr("kew.subprocess_utils.shutil.which", lambda _: "/bin/claude")
    monkeypatch.setattr("kew.subprocess_utils._probe_claude_cli_auth", lambda: False)
    assert inspect_runner(config).credential_detected is True
    assert runner_preflight_failure(config, runner_name="claude-code") is None


def test_round8_sidecar_real_child_keeps_upstream(
    store, driver_config, monkeypatch, tmp_path
):
    from kew.switchyard_supervisor import SidecarAttemptSupervisor

    for name in _round8_inference_names():
        monkeypatch.setenv(name, "unrelated-round8")
    monkeypatch.setenv("OPENROUTER_API_KEY", "upstream-round8")
    config = _round6_route_config(tmp_path, "claude-code", "switchyard/test", True)
    environment = _round8_real_child(store, config, tmp_path, monkeypatch)
    # The sidecar's declared upstream credential reaches the real child; no
    # other inference credential and no management or handoff authority does.
    assert environment["OPENROUTER_API_KEY"] == "upstream-round8"
    others = _round8_inference_names() - {"OPENROUTER_API_KEY"}
    assert not others.intersection(environment)
    assert {"OPENROUTER_PROVISIONING_KEY", "KEW_ISSUED_RUN_KEY_HASH"}.isdisjoint(
        environment
    )
    monkeypatch.setattr("os.environ", environment)
    # Exercise the supervisor's actual credential reader without launching Docker.
    supervisor = object.__new__(SidecarAttemptSupervisor)
    supervisor._settings = config.inference_transport.switchyard
    contents = b"""
[llm_clients.upstream]
format = "openai"
api_key_env = "OPENROUTER_API_KEY"
[targets.test]
llm_client = "upstream"
[routes.test]
id = "switchyard/test"
target = "test"
"""
    assert supervisor._validate_route(contents) == ("OPENROUTER_API_KEY",)


def test_round8_native_codex_real_child_requires_no_inference_names(
    store, driver_config, monkeypatch, tmp_path
):
    from kew.graph.driver.credential_route import resolve_driver_credential_route

    for name in _round8_inference_names():
        monkeypatch.setenv(name, "unrelated-round8")
    config = _round6_route_config(tmp_path, "codex", "gpt-5.4", False)
    environment = _round8_real_child(store, config, tmp_path, monkeypatch)
    assert _round8_inference_names().isdisjoint(environment)
    decision = resolve_driver_credential_route(config, "42")
    assert decision.evidence().get("required_credential_names") == []


def test_round8_digest_binds_required_names(tmp_path):
    from dataclasses import replace

    from kew.credential_handoff import (
        CredentialRouteChanged,
        credential_route,
        detached_credential_handoff,
        require_credential_route,
    )
    from kew.inference_transport import resolve_inference_transport
    from kew.runners import resolve_runner

    config = _round6_route_config(tmp_path, "claude-code", "switchyard/test", True)
    route = resolve_runner(config)
    transport = resolve_inference_transport(config, route.runner, route.model)
    first = credential_route(route.runner, transport)
    assert first.evidence().get("required_credential_names") == ["OPENROUTER_API_KEY"]
    second = replace(first, required_credential_names=frozenset({"OPENAI_API_KEY"}))
    assert first.digest != second.digest
    changed = replace(transport, credential_env_names=("OPENAI_API_KEY",))
    with (
        detached_credential_handoff(first.digest, "round8-run"),
        pytest.raises(CredentialRouteChanged),
    ):
        require_credential_route(route.runner, changed)


_ROUND9_MODES = {
    "anthropic-api": {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
    },
    "bedrock": {
        "CLAUDE_CODE_USE_BEDROCK",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
        "AWS_PROFILE",
        "AWS_BEARER_TOKEN_BEDROCK",
        "ANTHROPIC_BEDROCK_BASE_URL",
    },
    "vertex": {
        "CLAUDE_CODE_USE_VERTEX",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "CLOUD_ML_REGION",
        "ANTHROPIC_VERTEX_PROJECT_ID",
        "GOOGLE_CLOUD_PROJECT",
        "ANTHROPIC_VERTEX_BASE_URL",
    },
}


def _round9_select_mode(monkeypatch, mode):
    for name in ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX"):
        monkeypatch.delenv(name, raising=False)
    if mode != "anthropic-api":
        monkeypatch.setenv(f"CLAUDE_CODE_USE_{mode.upper()}", "1")


@pytest.mark.parametrize("mode", ["bedrock", "vertex"])
def test_round9_native_claude_cloud_real_child_authenticates(
    store, tmp_path, monkeypatch, mode
):
    import json
    import sys

    from kew.subprocess_utils import popen

    all_names = set().union(*_ROUND9_MODES.values()) | {"OPENROUTER_API_KEY"}
    for name in all_names:
        monkeypatch.setenv(name, "round9-" + name)
    _round9_select_mode(monkeypatch, mode)
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(sys.path))
    output = tmp_path / "round9-child.json"
    code = f"""
import json, os, pathlib
from kew.config import DispatchConfig
from kew.subprocess_utils import inspect_runner
import kew.subprocess_utils as utils
from kew.runner_preflight import runner_preflight_result
utils.shutil.which = lambda name: "/fake/claude"
utils._probe_claude_cli_auth = lambda: False
config = DispatchConfig()
pathlib.Path({str(output)!r}).write_text(json.dumps({{
    "environment": {{
        n: os.environ[n] for n in {sorted(all_names)!r} if n in os.environ
    }},
    "authenticated": inspect_runner(config).credential_detected,
    "preflight": runner_preflight_result(config, runner_name="claude-code"),
}}))
"""

    def launch(_cmd, **kwargs):
        monkeypatch.setitem(TOOL_PATHS, "python", sys.executable)
        child = popen(["python", "-c", code], **kwargs)
        assert child.wait(timeout=10) == 0
        return child

    config = _round6_route_config(tmp_path, "claude-code", "claude-sonnet-4-6", False)
    assert _round7_sink(store, config, monkeypatch, launch).dispatch("42")
    result = json.loads(output.read_text())
    environment = result["environment"]
    assert all_names.intersection(environment) == _ROUND9_MODES[mode]
    for name in _ROUND9_MODES[mode] - {f"CLAUDE_CODE_USE_{mode.upper()}"}:
        assert environment[name] == "round9-" + name
    assert result["authenticated"] is True
    assert result["preflight"] == [None, False]


def test_round9_claude_modes_change_digest(tmp_path, monkeypatch):
    from kew.graph.driver.credential_route import resolve_driver_credential_route

    config = cli.DispatchConfig()
    decisions = {}
    for mode in _ROUND9_MODES:
        _round9_select_mode(monkeypatch, mode)
        decisions[mode] = resolve_driver_credential_route(config, "42")
    assert len({decision.digest for decision in decisions.values()}) == 3
    for mode, decision in decisions.items():
        assert decision.evidence().get("claude_auth_mode") == mode
        assert decision.required_credential_names == _ROUND9_MODES[mode]


def test_round9_bedrock_parent_anthropic_child_refuses_before_launch(
    store, tmp_path, monkeypatch
):
    from kew.graph.driver.credential_route import resolve_driver_credential_route

    launch = MagicMock(return_value=DispatchOutcome(exit_code=0, changed_files=[]))
    config = _wire_dispatch_child(monkeypatch, tmp_path, store, dispatch=launch)
    _round9_select_mode(monkeypatch, "bedrock")
    parent = resolve_driver_credential_route(config, "42")
    _round9_select_mode(monkeypatch, "anthropic-api")
    store.reserve_epic_node_item("dag-1", "42", "parent-run")
    result = runner.invoke(
        epic_app,
        [
            "_dispatch-node",
            "dag-1",
            "42",
            "--reservation-run-id",
            "parent-run",
            "--reservation-generation",
            "parent-run",
            "--run-id",
            "parent-run",
            "--credential-route",
            parent.digest,
        ],
    )
    _round7_assert_refusal(result, store, launch, "credential route changed")


@pytest.mark.parametrize("mode", list(_ROUND9_MODES))
def test_round9_mode_keep_sets_exclude_management_and_publication(
    tmp_path, monkeypatch, mode
):
    from kew.credential_issuer import MANAGEMENT_CREDENTIAL_ENVS
    from kew.graph.driver.credential_route import resolve_driver_credential_route
    from kew.subprocess_utils import PUBLICATION_CREDENTIAL_ENVS

    _round9_select_mode(monkeypatch, mode)
    config = _round6_route_config(tmp_path, "claude-code", "claude-sonnet-4-6", False)
    decision = resolve_driver_credential_route(config, "42")
    assert decision.required_credential_names == _ROUND9_MODES[mode]
    assert decision.required_credential_names.isdisjoint(MANAGEMENT_CREDENTIAL_ENVS)
    assert decision.required_credential_names.isdisjoint(PUBLICATION_CREDENTIAL_ENVS)


def test_round9_switchyard_keep_set_rejects_authority_names(tmp_path):
    from dataclasses import replace

    from kew.credential_handoff import credential_route
    from kew.credential_issuer import MANAGEMENT_CREDENTIAL_ENVS
    from kew.inference_transport import resolve_inference_transport
    from kew.runners import resolve_runner
    from kew.subprocess_utils import PUBLICATION_CREDENTIAL_ENVS

    config = cli.DispatchConfig()
    route = resolve_runner(config)
    transport = resolve_inference_transport(config, route.runner, route.model)
    names = (
        *MANAGEMENT_CREDENTIAL_ENVS,
        *PUBLICATION_CREDENTIAL_ENVS,
        "OPENROUTER_API_KEY",
    )
    transport = replace(transport, mode="switchyard", credential_env_names=names)
    decision = credential_route(route.runner, transport)
    assert decision.required_credential_names == {"OPENROUTER_API_KEY"}


@pytest.fixture(autouse=True)
def round16_empty_issuer_sweep(monkeypatch):
    from unittest.mock import Mock

    from kew import credential_issuer

    monkeypatch.setattr(credential_issuer, "list_run_keys", Mock(return_value=[]))


@pytest.mark.parametrize("fleet_cap", [0, 3])
def test_detached_sink_pins_acquired_reservation_after_replacement(
    store, monkeypatch, driver_config, fleet_cap
):
    """A slow parent must pass A even when B exists before the actual spawn."""
    monkeypatch.setattr(
        "kew.graph.driver.dispatch.resolve_kew_console_script", lambda: "/bin/kew"
    )
    popen = _RecordingPopen()
    sink = DetachedDispatchSink(
        epic_ref="alpha",
        config=driver_config,
        store=store,
        popen=popen,
        publication_token=lambda: None,
    )

    def replace_before_spawn(item_id):
        store.release_epic_node_item(item_id, run_id="reservation-A")
        assert store.reserve_epic_node_item("alpha", item_id, "reservation-B")
        return False

    assert (
        reserve_and_dispatch(
            "42",
            epic_ref="alpha",
            store=store,
            sink=sink,
            branch_exists=replace_before_spawn,
            fleet_cap=fleet_cap,
            new_run_id=lambda: "reservation-A",
        )
        == "dispatched"
    )
    # The reservation pin precedes the child's own minted run identity, which
    # the bounded-key driver appends after it.
    argv = popen.calls[0][0]
    assert argv[:9] == [
        "kew",
        "epic",
        "_dispatch-node",
        "alpha",
        "42",
        "--reservation-run-id",
        "reservation-A",
        "--reservation-generation",
        "reservation-A",
    ]


@pytest.mark.parametrize("provider,item_id", [("github", "42"), ("linear", "SFD-42")])
def test_dispatch_child_forwards_pinned_reservation_options(
    store, monkeypatch, tmp_path, provider, item_id
):
    assert store.reserve_epic_node_item("alpha", item_id, "reservation-A")
    dispatch = MagicMock(return_value=DispatchOutcome(exit_code=0, changed_files=[]))
    _wire_dispatch_child(monkeypatch, tmp_path, store, dispatch=dispatch)
    monkeypatch.setattr(cli, "provider_from_config", lambda _: provider)
    if provider == "linear":
        import kew.cli.epic as epic_mod

        monkeypatch.setattr(
            epic_mod,
            "_prepare_linear_dispatch",
            lambda *_: (object(), {"number": 0, "title": "t"}),
        )
    result = runner.invoke(
        epic_app,
        [
            "_dispatch-node",
            "alpha",
            item_id,
            "--reservation-run-id",
            "reservation-A",
            "--reservation-generation",
            "reservation-A",
        ],
    )
    assert result.exit_code == 0, result.output
    assert dispatch.call_args.kwargs["expected_reservation_run_id"] == "reservation-A"
    assert (
        dispatch.call_args.kwargs["expected_reservation_generation"] == "reservation-A"
    )


def _round15_replace_reservation(store, epic_ref, item_id):
    store.release_epic_node_item(item_id, run_id="reservation-A")
    assert store.reserve_epic_node_item(epic_ref, item_id, "reservation-B")
    return store.list_epic_reservations(epic_ref)[0]


def _round15_assert_replacement_admits(store, config, epic_ref, item_id, provider):
    from datetime import UTC, datetime

    from kew.graph.driver.tick import (
        reconcile_epic_reservations,
        resolve_reservation_run,
    )
    from kew.run_store import RunRecord
    from kew.run_store_work import capture_dispatch_reservation

    [replacement] = store.list_epic_reservations(epic_ref)
    assert replacement.run_id == "reservation-B"
    assert store.get("reservation-B") is None
    assert resolve_reservation_run(store, replacement) is None
    assert reconcile_epic_reservations(
        epic_ref, store, config, now=datetime.now(UTC)
    ) == {item_id}
    reservation = capture_dispatch_reservation(
        store,
        epic_ref,
        item_id,
        expected_reservation_run_id="reservation-B",
        expected_reservation_generation="reservation-B",
    )
    child = RunRecord(
        run_id="replacement-child",
        issue_num=42 if provider == "github" else 0,
        item_id=item_id,
        branch="agent/item",
        started_at=datetime.now(UTC),
        status="pending",
        exit_code=-1,
    )
    assert store.admit_run_with_identity(
        child,
        store.build_admission_identity(
            child.run_id,
            tracker=provider,
            item_id=item_id,
            repo=None,
        ),
        reservation=reservation,
        reservation_pinned=True,
    )
    assert resolve_reservation_run(store, replacement).run_id == child.run_id


@pytest.mark.parametrize("provider,item_id", [("github", "42"), ("linear", "SFD-42")])
@pytest.mark.parametrize("failure", ["missing_key", "fetch", "claim", "runner"])
def test_round15_stale_child_refuses_before_provider_setup(
    store, monkeypatch, tmp_path, provider, item_id, failure
):
    import kew.cli.epic as epic_mod

    execute = MagicMock(return_value=DispatchOutcome(exit_code=0))
    config = _wire_dispatch_child(monkeypatch, tmp_path, store, dispatch=execute)
    config.tracker.type = provider
    assert store.reserve_epic_node_item("alpha", item_id, "reservation-A")
    _round15_replace_reservation(store, "alpha", item_id)
    calls = []
    tracker = MagicMock()
    tracker.claim_state.return_value = {"id": "started", "name": "In Progress"}
    tracker.fetch_issue.return_value = {"number": 0, "title": "item"}
    _patch_linear(monkeypatch, tracker)
    if failure == "missing_key":
        monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    elif failure in {"fetch", "claim"}:
        from kew.linear.client import LinearApiError

        getattr(
            tracker, "fetch_issue" if failure == "fetch" else "claim"
        ).side_effect = LinearApiError(failure)
    else:
        monkeypatch.setattr(
            cli, "runner_preflight_failure", lambda *a, **k: "missing runner"
        )
    monkeypatch.setattr(cli, "select_next_issue", lambda **k: None)
    monkeypatch.setattr(
        cli, "provider_from_config", lambda _: calls.append("provider") or provider
    )
    monkeypatch.setattr(cli, "preflight_tools", lambda: calls.append("preflight"))
    monkeypatch.setattr(
        epic_mod,
        "activate_detached_publication_token",
        lambda: calls.append("credentials"),
    )
    monkeypatch.setattr(epic_mod, "_report_child_refusal", MagicMock())

    result = runner.invoke(
        epic_app,
        [
            "_dispatch-node",
            "alpha",
            item_id,
            "--reservation-run-id",
            "reservation-A",
            "--reservation-generation",
            "reservation-A",
        ],
    )
    assert (calls, result.exit_code) == ([], 2), result.output
    tracker.fetch_issue.assert_not_called()
    tracker.claim.assert_not_called()
    execute.assert_not_called()
    epic_mod._report_child_refusal.assert_not_called()
    [row] = store.list_by_item(item_id)
    assert (row.status, row.error_category) == ("refused", "reservation_replaced")
    assert row.run_id not in {"reservation-A", "reservation-B"}
    assert not store.list_audit_events(event="dispatch_reservation_bound")
    _round15_assert_replacement_admits(store, config, "alpha", item_id, provider)


@pytest.mark.parametrize("failure", ["missing_key", "fetch", "claim"])
def test_round15_linear_refusal_after_replacement_cannot_attribute_current_owner(
    store, monkeypatch, failure
):
    import typer

    from kew.linear.client import LinearApiError

    item_id = "SFD-42"
    assert store.reserve_epic_node_item("alpha", item_id, "reservation-A")
    tracker = MagicMock()
    tracker.claim_state.return_value = {"id": "started", "name": "In Progress"}
    tracker.fetch_issue.return_value = {"number": 0, "title": "item"}
    _patch_linear(monkeypatch, tracker)
    monkeypatch.setattr(cli, "runner_preflight_failure", lambda *a, **k: None)
    if failure == "missing_key":
        _round15_replace_reservation(store, "alpha", item_id)
        monkeypatch.delenv("LINEAR_API_KEY")
    else:

        def replace_and_fail(*args):
            _round15_replace_reservation(store, "alpha", item_id)
            raise LinearApiError(failure)

        getattr(
            tracker, "fetch_issue" if failure == "fetch" else "claim"
        ).side_effect = replace_and_fail
    config = _linear_config()

    def dispatch():
        return cli._dispatch_linear_node(
            store,
            config,
            None,
            epic_ref="alpha",
            item_id=item_id,
            provider="linear",
            expected_reservation_run_id="reservation-A",
            expected_reservation_generation="reservation-A",
        )

    if failure == "claim":
        _dispatch_linear_at_admission(store, monkeypatch)
        assert dispatch().exit_code == 2
    else:
        with pytest.raises(typer.Exit):
            dispatch()
    assert store.get("reservation-B") is None
    [row] = [r for r in store.list_by_item(item_id) if r.status == "refused"]
    assert row.error_category == "reservation_replaced"
    _round15_assert_replacement_admits(store, config, "alpha", item_id, "linear")


@pytest.mark.parametrize("failure", ["fetch", "runner"])
def test_round15_github_refusal_after_replacement_preserves_current_owner(
    store, monkeypatch, tmp_path, failure
):
    config = _wire_dispatch_child(
        monkeypatch,
        tmp_path,
        store,
        dispatch=MagicMock(return_value=DispatchOutcome(exit_code=0)),
    )
    assert store.reserve_epic_node_item("alpha", "42", "reservation-A")

    def fetch(**kwargs):
        _round15_replace_reservation(store, "alpha", "42")
        return None if failure == "fetch" else {"number": 42, "title": "item"}

    monkeypatch.setattr(cli, "select_next_issue", fetch)
    monkeypatch.setattr(
        cli, "runner_preflight_failure", lambda *a, **k: "runner unavailable"
    )
    result = runner.invoke(
        epic_app,
        [
            "_dispatch-node",
            "alpha",
            "42",
            "--reservation-run-id",
            "reservation-A",
            "--reservation-generation",
            "reservation-A",
        ],
    )
    assert result.exit_code != 0
    assert store.get("reservation-B") is None
    rows = store.list_by_item("42")
    assert len(rows) == 1
    [row] = rows
    assert row.error_category == "reservation_replaced"
    assert store.list_epic_holds("alpha") == []
    _round15_assert_replacement_admits(store, config, "alpha", "42", "github")


@pytest.mark.parametrize("expected", [None, "reservation-A"])
def test_round15_child_reporter_never_adopts_an_unsupplied_reservation(store, expected):
    import inspect

    import kew.cli.epic as epic_mod

    assert store.reserve_epic_node_item("alpha", "42", "reservation-B")
    kwargs = {}
    if (
        "expected_reservation_run_id"
        in inspect.signature(epic_mod._record_dispatch_child_refusal).parameters
    ):
        kwargs["expected_reservation_run_id"] = expected
    epic_mod._record_dispatch_child_refusal(
        store,
        cli.DispatchConfig(),
        "alpha",
        "42",
        "dispatch_child_refused",
        **kwargs,
    )
    assert store.get("reservation-B") is None
    [row] = store.list_by_item("42")
    assert row.run_id not in {"reservation-A", "reservation-B"}
    if expected:
        assert row.error_category == "reservation_replaced"

    _round15_assert_replacement_admits(
        store, cli.DispatchConfig(), "alpha", "42", "github"
    )


@pytest.mark.parametrize("held", [False, True])
def test_round16_generic_reporter_rechecks_before_existing_state(store, held):
    from datetime import UTC, datetime

    import kew.cli.epic as epic_mod

    assert store.reserve_epic_node_item("alpha", "42", "reservation-B")
    if held:
        store.hold_epic_node("alpha", "42", "B's hold")
    reservations = store.list_epic_reservations("alpha")
    holds = store.list_epic_holds("alpha")
    with pytest.raises(epic_mod.typer.Exit) as exc:
        epic_mod._report_child_refusal(
            store,
            cli.DispatchConfig(),
            epic_ref="alpha",
            item_id="42",
            detail="unreachable",
            exit_code=3,
            since=datetime.now(UTC),
            known_category="github_unreachable",
            expected_reservation_run_id="reservation-A",
        )
    assert exc.value.exit_code == 2
    assert store.list_epic_reservations("alpha") == reservations
    assert store.list_epic_holds("alpha") == holds
    assert store.get("reservation-B") is None
    [row] = store.list_by_item("42")
    assert row.run_id not in {"reservation-A", "reservation-B"}
    assert (row.error_category, row.exit_code) == ("reservation_replaced", 2)
