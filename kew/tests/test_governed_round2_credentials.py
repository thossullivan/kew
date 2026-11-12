"""Governed review round 1 (2026-09-21) for PR #1957.

1. A boundary that declares its upstream route credential keeps it through
   the child filter (the managed Switchyard server and the sidecar worker).
2. A held publication (issuer revocation not yet confirmed) never aborts the
   driver's reconciliation; the run stays awaiting publication.
3. A direct OpenRouter route without provisioning is refused on the host too.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_switchyard_supervisor import _install_process_fakes, _settings

from kew.config import DispatchConfig
from kew.credential_environment import child_environment
from kew.graph.driver.facts import reconcile_awaiting_publication
from kew.publication_credentials import IssuedKeyPublicationHeld
from kew.run_credentials import ProvisioningRequired
from kew.run_store import RunRecord, SqliteRunStore


def test_governed_round2_child_filter_retains_only_declared_names():
    source = {
        "OPENROUTER_API_KEY": "upstream-value",
        "OPENROUTER_PROVISIONING_KEY": "management-value",
        "KEW_ISSUED_RUN_KEY_HASH": "a" * 64,
        "PATH": "/usr/bin",
    }
    stripped = child_environment(source)
    assert "OPENROUTER_API_KEY" not in stripped
    retained = child_environment(
        source,
        retain=(
            "OPENROUTER_API_KEY",
            "OPENROUTER_PROVISIONING_KEY",
            "KEW_ISSUED_RUN_KEY_HASH",
        ),
    )
    assert retained["OPENROUTER_API_KEY"] == "upstream-value"
    # Management and handoff authority never pass, whatever a caller declares.
    assert "OPENROUTER_PROVISIONING_KEY" not in retained
    assert "KEW_ISSUED_RUN_KEY_HASH" not in retained
    assert retained["PATH"] == "/usr/bin"


def test_governed_round2_managed_server_launch_retains_its_declared_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_switchyard_supervisor import _ready_probe

    from kew.switchyard_supervisor import ManagedSwitchyardSupervisor

    settings = _settings(
        tmp_path,
        credential_env=("OPENROUTER_API_KEY",),
        route_credential_env="OPENROUTER_API_KEY",
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "upstream-route-value")
    launches, _ = _install_process_fakes(monkeypatch)
    with ManagedSwitchyardSupervisor(settings, endpoint_probe=_ready_probe) as managed:
        assert managed.is_healthy()
    _pinned, _args, kwargs = launches[0]
    assert kwargs["env"] == {"OPENROUTER_API_KEY": "upstream-route-value"}
    assert tuple(kwargs["retain_credentials"]) == ("OPENROUTER_API_KEY",)


def test_governed_round2_pinned_launch_keeps_retained_credential(monkeypatch):
    import subprocess

    from kew import subprocess_utils

    captured: dict[str, object] = {}

    class _Proc:
        pid = 4242

    def fake_popen(cmd, **kwargs):
        captured.update(kwargs)
        return _Proc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(subprocess_utils, "_pinned_cmd", lambda pinned, args: ["x"])
    subprocess_utils.popen_pinned(
        object(),  # type: ignore[arg-type]
        ["--version"],
        env={"OPENROUTER_API_KEY": "upstream-route-value", "HOME": "/tmp"},
        retain_credentials=("OPENROUTER_API_KEY",),
    )
    assert captured["env"] == {
        "OPENROUTER_API_KEY": "upstream-route-value",
        "HOME": "/tmp",
    }
    subprocess_utils.popen_pinned(
        object(),  # type: ignore[arg-type]
        ["--version"],
        env={"OPENROUTER_API_KEY": "upstream-route-value", "HOME": "/tmp"},
    )
    assert captured["env"] == {"HOME": "/tmp"}


def test_governed_round2_held_publication_keeps_the_run_and_the_tick(
    tmp_path, monkeypatch
):
    now = datetime.now(UTC)

    def held(store, *, run_id):
        raise IssuedKeyPublicationHeld("issuer unavailable", run_id=run_id)

    monkeypatch.setattr("kew.graph.driver.facts.ensure_publication_credentials", held)
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        store.save(
            RunRecord(
                run_id="held-run",
                issue_num=42,
                item_id="42",
                branch="agent/issue-42",
                status="awaiting_publication",
                exit_code=0,
                started_at=now,
                finished_at=now,
                container_name="kew-42",
            )
        )
        changed = reconcile_awaiting_publication(
            store,
            item_id="42",
            repo="o/r",
            pr_url="https://example.invalid/pr/42",
            observation="open PR",
        )
        run = store.get("held-run")
        events = store.list_audit_events(event="publication_held")
    assert changed is False
    assert run is not None and run.status == "awaiting_publication"
    assert len(events) == 1
    assert events[0].payload["issuer_reason"] == "issuer unavailable"
    assert events[0].payload["publication_allowed"] is False


def test_governed_round2_host_direct_route_without_provisioning_is_refused(
    monkeypatch,
):
    from kew.run_credentials import ProvisioningRequired, bounded_run_credentials

    monkeypatch.delenv("OPENROUTER_PROVISIONING_KEY", raising=False)
    monkeypatch.delenv("KEW_ISSUED_RUN_KEY_HASH", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "ambient-host-value")
    with (
        pytest.raises(ProvisioningRequired, match="for direct routes"),
        bounded_run_credentials(
            run_id="host-run",
            config=DispatchConfig(),
            store=None,
            model="openrouter/test-model",
            credential_names=("OPENROUTER_API_KEY",),
            timeout_seconds=60,
        ),
    ):
        pytest.fail("a host direct route launched without a bounded credential")


def test_governed_round2_excluded_handoff_keeps_the_declared_upstream_name(
    tmp_path, monkeypatch
):
    """The detached child of an excluded Switchyard handoff keeps exactly the
    route's declared upstream credential through both environment filters."""
    from unittest.mock import Mock

    from test_driver_dispatch import _round6_route_config, _round7_sink

    from kew.run_store import SqliteRunStore

    monkeypatch.setenv("OPENROUTER_API_KEY", "upstream-value")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "unrelated-value")
    monkeypatch.delenv("OPENROUTER_PROVISIONING_KEY", raising=False)
    config = _round6_route_config(tmp_path, "claude-code", "switchyard/test", True)
    launch = Mock(return_value=Mock(pid=4242))
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        sink = _round7_sink(store, config, monkeypatch, launch)
        assert sink.dispatch("42")
    kwargs = launch.call_args.kwargs
    assert kwargs["env"]["OPENROUTER_API_KEY"] == "upstream-value"
    assert "ANTHROPIC_API_KEY" not in kwargs["env"]
    assert tuple(kwargs["retain_credentials"]) == ("OPENROUTER_API_KEY",)


@pytest.mark.parametrize(
    ("enabled", "timeout_minutes", "attempts", "expected"),
    [
        ("auto", 60, 3, 4 * 60 * 60 + 60 * 60),
        (True, 120, 1, 4 * 60 * 60 + 120 * 60),
        (False, 60, 3, None),
    ],
)
def test_governed_round2_driver_key_covers_the_contained_wall_clock(
    enabled, timeout_minutes, attempts, expected
):
    from kew.graph.driver.credentials import driver_key_lifetime_seconds
    from kew.run_credentials import retry_schedule_seconds

    config = DispatchConfig()
    config.sandbox.enabled = enabled
    config.agent.timeout_minutes = timeout_minutes
    config.retry.max_attempts = attempts
    schedule = retry_schedule_seconds(config.retry, timeout_minutes * 60)
    lifetime = driver_key_lifetime_seconds(config)
    if expected is None:
        assert lifetime == schedule
    else:
        assert lifetime == max(schedule, expected)
        assert lifetime >= 4 * 60 * 60 + timeout_minutes * 60


def test_governed_round3_startup_gate_validates_the_launch_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The --dry-run gate proves the environment the server will run under.

    Round 2 taught the launch to retain its declared route credential through
    the child filter. The gate runs through the same filter, so without the
    same retention it validated an environment with the credential emptied
    (governed review round 3, 2026-09-21).
    """
    import subprocess as _subprocess

    from test_switchyard_supervisor import _ready_probe, _settings

    from kew import switchyard_supervisor
    from kew.switchyard_supervisor import ManagedSwitchyardSupervisor

    settings = _settings(
        tmp_path,
        credential_env=("OPENROUTER_API_KEY",),
        route_credential_env="OPENROUTER_API_KEY",
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "upstream-route-value")
    runs: list[tuple[list[str], dict]] = []
    launches: list[dict] = []

    def fake_run(pinned, args, **kwargs):
        runs.append((list(args), dict(kwargs)))
        if args == ["--version"]:
            return _subprocess.CompletedProcess(
                args, 0, "switchyard-server 0.1.0\n", ""
            )
        return _subprocess.CompletedProcess(args, 0, "ok\n", "")

    class _Proc:
        pid = 4242
        stdout = None
        stderr = None
        returncode = None

        def poll(self):
            return None

        def wait(self, timeout=None):
            return 0

    def fake_popen(pinned, args, **kwargs):
        launches.append(dict(kwargs))
        return _Proc()

    monkeypatch.setattr(switchyard_supervisor, "run_pinned", fake_run)
    monkeypatch.setattr(switchyard_supervisor, "popen_pinned", fake_popen)
    monkeypatch.setattr(switchyard_supervisor.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(switchyard_supervisor.os, "killpg", lambda pgid, sig: None)

    with ManagedSwitchyardSupervisor(settings, endpoint_probe=_ready_probe) as managed:
        assert managed.is_healthy()

    dry_runs = [call for call in runs if call[0][-1] == "--dry-run"]
    assert len(dry_runs) == 1
    _args, kwargs = dry_runs[0]
    assert kwargs["env"] == {"OPENROUTER_API_KEY": "upstream-route-value"}
    assert tuple(kwargs["retain_credentials"]) == ("OPENROUTER_API_KEY",)
    # The gate and the launch name exactly the same retention.
    assert tuple(launches[0]["retain_credentials"]) == tuple(
        kwargs["retain_credentials"]
    )
    # The version probe carries no credential to retain.
    version = [call for call in runs if call[0] == ["--version"]][0]
    assert version[1]["env"] == {}


def test_governed_round3_a_locked_out_driver_reconciles_no_credentials(
    tmp_path, monkeypatch
):
    """Credential reconciliation belongs to the driver that holds the lock.

    Entering it beside the lock ran the issuance recovery, revocation retries
    and issuer orphan sweep in the locked-out process too, concurrently with
    the holder (governed review round 3, 2026-09-21).
    """
    from support.driver_fakes import (
        FakeFactsReader,
        FakeSink,
        FakeSource,
        _advance,
        _facts,
        _node,
    )

    from kew.graph.driver import tick as tick_module
    from kew.graph.driver.lock import single_driver_lock
    from kew.run_store import SqliteRunStore

    entered: list[str] = []
    real_cleanup = tick_module.driver_key_cleanup

    def recording_cleanup(store):
        entered.append("cleanup")
        return real_cleanup(store)

    monkeypatch.setattr(tick_module, "driver_key_cleanup", recording_cleanup)

    deps = FakeSource(nodes=[_node("1")], edges=[])
    facts = FakeFactsReader(issue_facts_by_num={1: _facts(1, state="OPEN")})
    sink = FakeSink()
    lock_path = tmp_path / "epic.lock"

    with (
        SqliteRunStore(str(tmp_path / "runs.db")) as store,
        single_driver_lock(lock_path) as got,
    ):
        assert got is True
        result = _advance(
            deps=deps, facts=facts, store=store, sink=sink, lock_path=lock_path
        )

    assert result.locked_out is True
    assert entered == []


def test_governed_round4_a_mintable_key_counts_as_handed_to_the_container(
    monkeypatch,
):
    """On a provisioning-only host the per-run key is minted, not inherited, so
    reading the environment alone recorded an empty credential exposure and
    silenced the exfiltration warning while the container held a live key
    (governed review round 4, 2026-09-21)."""
    from kew.sandbox import open_egress_warning, posture_record_fields

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_PROVISIONING_KEY", "management-secret-1944")

    fields = posture_record_fields(
        runner="opencode", posture="sandboxed", egress_verification="allowlist"
    )
    assert "OPENROUTER_API_KEY" in fields["credential_names"]

    warning = open_egress_warning("open", runner="opencode")
    assert warning is not None and "OPENROUTER_API_KEY" in warning
    assert "management-secret-1944" not in warning

    # Without provisioning there is nothing to hand over and nothing to warn about.
    monkeypatch.delenv("OPENROUTER_PROVISIONING_KEY", raising=False)
    fields = posture_record_fields(
        runner="opencode", posture="sandboxed", egress_verification="allowlist"
    )
    assert "OPENROUTER_API_KEY" not in fields["credential_names"]
    assert open_egress_warning("open", runner="opencode") is None


def test_governed_round7_an_issuer_refusal_holds_its_node(tmp_path, monkeypatch):
    """A pinned child's issuer refusal reports the refusal, not a replaced
    reservation. Releasing this run's reservation before the refusal row was
    recorded made the next call re-validate a reservation that was gone, so
    the node was never held (governed review round 7, 2026-09-22)."""
    from kew import dispatch_core

    order: list[str] = []
    monkeypatch.setattr(
        dispatch_core,
        "_record_prelaunch_refusal",
        lambda *_a, **_k: order.append("refusal-row"),
    )
    monkeypatch.setattr(
        dispatch_core,
        "_record_issuer_refusal",
        lambda *_a, **_k: order.append("release"),
    )
    monkeypatch.setattr(dispatch_core, "_finish_refusal", lambda *_a, **_k: None)
    monkeypatch.setattr(dispatch_core, "_error", lambda *_a, **_k: None)
    monkeypatch.setattr(dispatch_core, "mark_failed", lambda *_a, **_k: None)
    monkeypatch.setattr(dispatch_core, "_emit_early_exit", lambda *_a, **_k: None)

    context = SimpleNamespace(
        store=None,
        run_id="run",
        issue_num=42,
        item_id="42",
        branch="agent/issue-42",
        complexity="complex",
        config=DispatchConfig(),
        epic_ref="dag-1",
        worktree_dir=None,
        start_monotonic=0.0,
        expected_reservation_run_id="pinned",
    )
    early = SimpleNamespace(runner="claude-code", model="claude-opus-5")
    result = dispatch_core._launch_refused_transport(
        context,  # type: ignore[arg-type]
        None,
        False,
        ProvisioningRequired(),
        early,  # type: ignore[arg-type]
    )

    assert order == ["refusal-row", "release"]
    assert result.early.category == "credential_issuer_refused"
    assert result.early.exit_code == 2
