"""Issued credentials at the real sandbox launch seam, with no container runtime."""

import json
import os
import shutil
import subprocess
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from unittest.mock import Mock

import pytest
from test_liveness_owner import process_table as process_table

from kew import credential_issuer as _round16_issuer_module
from kew.config import DispatchConfig
from kew.run_store import RunRecord, SqliteRunStore
from kew.tracking import DispatchResult


@pytest.fixture(autouse=True)
def round16_empty_issuer_sweep(monkeypatch):
    from kew import credential_issuer

    monkeypatch.setattr(
        credential_issuer, "list_run_keys", Mock(return_value=[]), raising=False
    )


def _issued_route_mint(run_id, limit_usd, expires_at, *, ledger_id, on_malformed):
    from kew import credential_issuer

    assert credential_issuer.parse_run_key_name(
        credential_issuer.run_key_name(ledger_id, run_id)
    ) == (ledger_id, run_id)
    return credential_issuer.BoundedRunKey(
        "<run>", "a" * 64, limit_usd, expires_at.isoformat().replace("+00:00", "Z")
    )


@pytest.fixture
def bounded_processes(monkeypatch, process_table):
    owner, client = 100001, 100002
    process_table.members[owner] = (owner, "S", "python3", "kew dispatch")
    process_table.members[client] = (client, "S", "sh", "kew-spawn-gate")
    process_table.parents[client] = owner
    monkeypatch.setattr(
        "kew.process_groups.exclusive_owner_pid", Mock(return_value=owner)
    )


@pytest.fixture
def issued_route(tmp_path, monkeypatch, bounded_processes):
    from kew import credential_issuer

    monkeypatch.setenv("OPENROUTER_PROVISIONING_KEY", "<manage>")
    monkeypatch.setenv("OPENROUTER_API_KEY", "<host>")

    mint_mock = Mock(side_effect=_issued_route_mint)
    revoke = Mock()
    monkeypatch.setattr(credential_issuer, "mint_bounded_run_key", mint_mock)
    monkeypatch.setattr(credential_issuer, "revoke_run_key", revoke)
    store = SqliteRunStore(str(tmp_path / "runs.db"), project_root=tmp_path)
    store.save(
        RunRecord(
            run_id="issued-run",
            issue_num=1944,
            item_id="1944",
            branch="agent/1944",
            status="pending",
            exit_code=-1,
            started_at=datetime.now(UTC),
            runner_name="opencode",
            model="openrouter/test",
            container_name="kew-1944",
        )
    )
    yield store, mint_mock, revoke
    store.close()


def _launch_review(tmp_path, store):
    from kew.dispatch_sandbox import run_container_and_stream

    return run_container_and_stream(
        DispatchConfig(),
        store,
        image="sha256:" + "b" * 64,
        runner="opencode",
        checkout_dir=tmp_path,
        slug="1944",
        run_id="issued-run",
        model="openrouter/test",
        log_file=tmp_path / "run.log",
        run_kind="adversarial_review",
        result=DispatchResult(
            exit_code=-1, issue_num=1944, log_file=tmp_path / "run.log"
        ),
    )


def test_bounded_container_refuses_setup_that_exhausts_lifetime(
    issued_route, tmp_path, monkeypatch
):
    from datetime import timedelta

    from kew import credential_issuer, dispatch_core, dispatch_sandbox, run_credentials

    class Clock(datetime):
        current = datetime(2026, 9, 15, tzinfo=UTC)

        @classmethod
        def now(cls, tz=None):
            return cls.current

    store, mint, revoke = issued_route
    build = dispatch_sandbox.build_agent_container_argv

    def delayed_build(*args, **kwargs):
        argv = build(*args, **kwargs)
        Clock.current += timedelta(seconds=301)
        return argv

    monkeypatch.setattr(run_credentials, "datetime", Clock)
    monkeypatch.setattr(dispatch_sandbox, "build_agent_container_argv", delayed_build)
    launch = Mock(return_value=Mock())
    monkeypatch.setattr(dispatch_core, "_launch_container", launch)
    monkeypatch.setattr(dispatch_core, "_stream_sandbox_log", Mock(return_value=0))
    monkeypatch.setattr(dispatch_sandbox, "finalize_container_hard_stop", Mock())
    with pytest.raises(credential_issuer.IssuerRefused):
        _launch_review(tmp_path, store)
    launch.assert_not_called()
    mint.assert_called_once()
    revoke.assert_called_once_with("a" * 64, issued_key="<run>")


def test_container_creation_cannot_consume_the_issued_lifetime(
    issued_route, tmp_path, monkeypatch
):
    from datetime import timedelta

    from kew import run_credentials, sandbox

    class Clock(datetime):
        current = datetime(2026, 9, 15, tzinfo=UTC)

        @classmethod
        def now(cls, tz=None):
            return cls.current

    store, mint, revoke = issued_route
    runtime = _round9_runtime(monkeypatch)
    create = sandbox.create_registered_container

    def delayed_create(*args, **kwargs):
        identity = create(*args, **kwargs)
        Clock.current += timedelta(seconds=301)
        return identity

    monkeypatch.setattr(run_credentials, "datetime", Clock)
    monkeypatch.setattr(sandbox, "create_registered_container", delayed_create)
    with pytest.raises(run_credentials.CredentialLifetimeRefused):
        _launch_review(tmp_path, store)
    runtime.assert_not_called()
    assert store.get("issued-run").container_id == "c" * 64
    assert store.get("issued-run").error_category == "sandbox_launch_error"
    mint.assert_called_once()
    revoke.assert_called_once_with("a" * 64, issued_key="<run>")


@pytest.mark.parametrize(
    "terminal", [0, 1, 124, 137, "stream_error", "interrupt", "launch_error"]
)
def test_bounded_sandbox_launch_and_terminal_revocation(
    issued_route, tmp_path, monkeypatch, caplog, terminal
):
    from kew import dispatch_core
    from kew.readiness import RealizedRouteProfile

    store, mint, revoke = issued_route
    launch = Mock(return_value=Mock())
    stream = Mock(return_value=terminal)
    if terminal == "launch_error":
        launch.side_effect = RuntimeError("launch failed")
    if terminal == "stream_error":
        stream.side_effect = RuntimeError("stream failed")
    if terminal == "interrupt":
        stream.side_effect = KeyboardInterrupt()
    monkeypatch.setattr(dispatch_core, "_launch_container", launch)
    monkeypatch.setattr(dispatch_core, "_stream_sandbox_log", stream)
    monkeypatch.setattr("kew.dispatch_sandbox.finalize_container_hard_stop", Mock())
    if isinstance(terminal, str):
        with pytest.raises(
            KeyboardInterrupt if terminal == "interrupt" else RuntimeError
        ):
            _launch_review(tmp_path, store)
    else:
        assert _launch_review(tmp_path, store) == terminal
    revoke.assert_called_once_with("a" * 64, issued_key="<run>")
    assert mint.call_args.args[1] == DispatchConfig().review.max_review_usd
    delta = (mint.call_args.args[2] - datetime.now(UTC)).total_seconds()
    assert 14690 < delta <= 14700  # Four-hour container hard stop plus five minutes.
    argv = launch.call_args.args[0]
    assert any(
        argv[i : i + 2] == ["-e", "OPENROUTER_API_KEY"] for i in range(len(argv))
    )
    env = launch.call_args.kwargs["env"]
    assert env["OPENROUTER_API_KEY"] == "<run>"
    assert "OPENROUTER_PROVISIONING_KEY" not in env
    assert "OPENROUTER_PROVISIONING_KEY" not in str(argv)
    assert os.environ["OPENROUTER_API_KEY"] == "<host>"
    profile = RealizedRouteProfile.from_json(
        store.require_route_profile("issued-run", 1).profile_json
    )
    assert profile.auth_path.value == "issued:openrouter:" + "a" * 64
    assert (
        profile.credential_issuer.value["limit_usd"]
        == DispatchConfig().review.max_review_usd
    )
    events = store.list_audit_events(event="sandbox_resolved", limit=100)
    terminal_event = next(e for e in events if e.payload["phase"] == "revocation")
    assert terminal_event.payload["credential_issuer"]["revoked"] is True
    evidence = (
        str(events)
        + profile.to_json()
        + str(store.get("issued-run"))
        + caplog.text
        + str(argv)
    )
    for secret in ("<run>", "<manage>", "<host>"):
        assert secret not in evidence


def test_bounded_sandbox_absent_provisioner_refuses(
    issued_route, tmp_path, monkeypatch
):
    from kew import credential_issuer, dispatch_core

    store, mint, revoke = issued_route
    monkeypatch.delenv("OPENROUTER_PROVISIONING_KEY")
    launch = Mock(return_value=Mock())
    monkeypatch.setattr(dispatch_core, "_launch_container", launch)
    monkeypatch.setattr(dispatch_core, "_stream_sandbox_log", Mock(return_value=0))
    monkeypatch.setattr("kew.dispatch_sandbox.finalize_container_hard_stop", Mock())
    with pytest.raises(
        credential_issuer.IssuerRefused,
        match="OpenRouter provisioning key required for direct routes",
    ):
        _launch_review(tmp_path, store)
    mint.assert_not_called()
    revoke.assert_not_called()
    launch.assert_not_called()
    assert os.environ["OPENROUTER_API_KEY"] == "<host>"
    event = store.list_audit_events(event="sandbox_resolved", limit=100)[0]
    assert event.payload["credential_issuer"] is None
    assert event.payload["credential_issuer_reason"] == (
        "OpenRouter provisioning key required for direct routes"
    )


def test_bounded_sandbox_failed_revocation_records_safe_reason(
    issued_route, tmp_path, monkeypatch
):
    from kew import dispatch_core

    store, _, revoke = issued_route
    revoke.side_effect = RuntimeError("<manage> <run>")
    monkeypatch.setattr(dispatch_core, "_launch_container", Mock(return_value=Mock()))
    monkeypatch.setattr(dispatch_core, "_stream_sandbox_log", Mock(return_value=0))
    monkeypatch.setattr("kew.dispatch_sandbox.finalize_container_hard_stop", Mock())
    assert _launch_review(tmp_path, store) == 0
    events = store.list_audit_events(event="sandbox_resolved", limit=100)
    record = next(
        e.payload["credential_issuer"]
        for e in events
        if e.payload["phase"] == "revocation"
    )
    assert record["revoked"] is False
    assert record["reason"] == "issuer unavailable"
    assert "secret-1944" not in json.dumps(record)


def _round10_producer(tmp_path, store):
    from kew.dispatch_sandbox import _run_sandboxed_container

    return _run_sandboxed_container(
        prompt="test",
        checkout_dir=tmp_path,
        config=DispatchConfig(),
        image="test-image",
        runner="opencode",
        issue_num=1944,
        item_id="1944",
        branch="agent/1944",
        title="test",
        model="openrouter/test",
        store=store,
        run_id="producer-run",
    )


def _round10_producer_runtime(monkeypatch):
    from kew import dispatch_sandbox

    runtime = _round9_runtime(monkeypatch)
    monkeypatch.setattr(
        dispatch_sandbox,
        "_resolve_launch_image_id",
        Mock(return_value="sha256:" + "b" * 64),
    )
    monkeypatch.setattr(
        dispatch_sandbox, "_capture_launch_evidence_or_finalize", Mock()
    )
    monkeypatch.setattr(
        dispatch_sandbox, "_checkout_has_no_changes", Mock(return_value=True)
    )
    return runtime


def test_round10_bounded_sandbox_producer_launches_with_issued_key(
    issued_route, tmp_path, monkeypatch
):
    store, mint, revoke = issued_route
    runtime = _round10_producer_runtime(monkeypatch)
    result = _round10_producer(tmp_path, store)
    runtime.assert_called_once()
    assert result.exit_code == 0
    mint.assert_called_once()
    revoke.assert_called_once_with("a" * 64, issued_key="<run>")
    assert (
        store.require_route_profile("producer-run", 1).auth_path
        == "issued:openrouter:" + "a" * 64
    )
    assert runtime.call_args.kwargs["env"]["OPENROUTER_API_KEY"] == "<run>"
    issuance = next(
        event.payload
        for event in store.list_audit_events(
            event="sandbox_resolved", run_id="producer-run"
        )
        if event.payload.get("phase") == "issuance"
    )
    assert issuance["attempt_number"] == 1
    assert issuance["credential_issuer"]["issuer"] == "openrouter"


def _round10_forged_issuance(store, run_id, key, *, phase, evidence_hash, **kwargs):
    if evidence_hash is None or phase != "issuance":
        return
    store.append_audit_event(
        timestamp=datetime.now(UTC).isoformat(),
        event="sandbox_resolved",
        actor_type="policy",
        actor_id="credential-issuer",
        run_id=run_id,
        payload={
            "phase": "issuance",
            "attempt_number": 1,
            "credential_issuer": {**key.identity, "key_hash": evidence_hash},
        },
    )


@pytest.mark.parametrize("evidence_hash", [None, "c" * 64], ids=["missing", "mismatch"])
def test_round10_forged_issued_profile_refuses_before_launch(
    issued_route, tmp_path, monkeypatch, evidence_hash
):
    from kew import run_credentials

    store, _, revoke = issued_route
    runtime = _round10_producer_runtime(monkeypatch)
    monkeypatch.setattr(
        run_credentials,
        "_record",
        partial(_round10_forged_issuance, evidence_hash=evidence_hash),
    )
    result = _round10_producer(tmp_path, store)
    assert result.error_category == "credential_publication_refused"
    refusal = store.list_audit_events(
        event="credential_publication_refused", run_id="producer-run"
    )[0]
    assert refusal.payload["reason"] == "issued_key_evidence_missing"
    runtime.assert_not_called()
    revoke.assert_called_once()


def _round10_checked_publication(*args, store, run_id, **kwargs):
    from kew.publication_credentials import require_publication_credentials

    require_publication_credentials(store, run_id=run_id)
    return "https://github.com/example/project/pull/1944"


@pytest.mark.parametrize("revocation_fails", [False, True])
def test_round10_producer_unwinds_before_publication(
    issued_route, tmp_path, monkeypatch, revocation_fails
):
    from kew import dispatch_core, dispatch_sandbox

    store, _, revoke = issued_route
    runtime = _round10_producer_runtime(monkeypatch)
    if revocation_fails:
        revoke.side_effect = RuntimeError("issuer unavailable")
    monkeypatch.setattr(
        dispatch_sandbox, "_checkout_has_no_changes", Mock(return_value=False)
    )
    monkeypatch.setattr(dispatch_sandbox, "_run_sandbox_shadow", Mock())
    publish = Mock(side_effect=_round10_checked_publication)
    monkeypatch.setattr(dispatch_core, "_push_and_open_pr", publish)
    monkeypatch.setattr(dispatch_core, "_post_agent_report", Mock())
    monkeypatch.setattr(dispatch_core, "_mint_publication_continuation", Mock())
    result = _round10_producer(tmp_path, store)
    runtime.assert_called_once()
    publish.assert_called_once()
    revoke.assert_called_once()
    if revocation_fails:
        assert result.error_category == "publication_held"
        assert result.subtype == "issued_key_unrevoked\nissuer unavailable"
        assert store.get("producer-run").status == "awaiting_publication"
    else:
        assert result.published_pr_url == "https://github.com/example/project/pull/1944"
        assert store.get("producer-run").status == "success"


def test_inherited_mismatch_refuses_container_and_publication(
    issued_route, tmp_path, monkeypatch
):
    from kew import credential_issuer, dispatch_core, dispatch_sandbox
    from kew.graph.driver.credentials import issue_driver_key

    store, mint, revoke = issued_route
    issue_driver_key(store, DispatchConfig(), "producer-run", "1944")
    mint.reset_mock()
    monkeypatch.delenv("OPENROUTER_PROVISIONING_KEY")
    monkeypatch.setenv("KEW_ISSUED_RUN_KEY_HASH", "a" * 64)
    # The marker identifies the issued key; the held value is the host key.
    monkeypatch.setattr(
        dispatch_sandbox,
        "_resolve_launch_image_id",
        lambda *a, **k: "sha256:" + "b" * 64,
    )
    launch, publish = Mock(), Mock()
    monkeypatch.setattr(dispatch_core, "_launch_container", launch)
    monkeypatch.setattr(dispatch_sandbox, "_publish_completed_sandbox", publish)
    with pytest.raises(
        credential_issuer.IssuerRefused, match="issued credential mismatch"
    ):
        dispatch_sandbox._run_sandboxed_container(
            prompt="test",
            checkout_dir=tmp_path,
            config=DispatchConfig(),
            image="test-image",
            runner="opencode",
            issue_num=1944,
            item_id="1944",
            branch="agent/1944",
            title="test",
            model="openrouter/test",
            store=store,
            run_id="producer-run",
        )
    launch.assert_not_called()
    publish.assert_not_called()
    mint.assert_not_called()
    revoke.assert_not_called()


_ROUND9_REASON = "OpenRouter provisioning key required for direct routes"


def _round9_control(argv, **kwargs):
    if argv[1] == "create":
        Path(argv[argv.index("--cidfile") + 1]).write_text("c" * 64)
    return subprocess.CompletedProcess(argv, 0, "c" * 64, "")


def _round9_runtime(monkeypatch):
    from kew import dispatch_core, dispatch_sandbox, subprocess_utils

    runtime = Mock(return_value=Mock(pid=100002, returncode=0))
    monkeypatch.setattr(subprocess_utils, "_popen_gated", runtime)
    monkeypatch.setattr(subprocess_utils.subprocess, "run", _round9_control)
    monkeypatch.setitem(subprocess_utils.TOOL_PATHS, "docker", "/recording/docker")
    monkeypatch.setattr(subprocess_utils.subprocess, "Popen", runtime)
    monkeypatch.setattr(dispatch_core, "_stream_sandbox_log", Mock(return_value=0))
    monkeypatch.setattr(dispatch_sandbox, "finalize_container_hard_stop", Mock())
    return runtime


def _round9_unkeyed_launch(tmp_path):
    from kew import dispatch_sandbox
    from kew.inference_transport import resolve_inference_transport
    from kew.runners import get_runner

    config = DispatchConfig()
    transport = resolve_inference_transport(
        config, get_runner("opencode"), "openrouter/test"
    )
    dispatch_sandbox._launch_streaming_container(
        config,
        None,
        run_id="unkeyed-run",
        log_file=tmp_path / "unkeyed.log",
        image="sha256:" + "b" * 64,
        runner="opencode",
        checkout_dir=tmp_path,
        slug="1944",
        model="openrouter/test",
        transport=transport,
        sidecar_supervisor=None,
        sidecar_ready=None,
        run_kind="dispatch",
    )


def _round9_foreground_context(store, config):
    from kew.dispatch_core import _DispatchContext

    return _DispatchContext(
        config=config,
        store=store,
        notifier=None,
        force_budget=False,
        approved=True,
        interactive=False,
        model_override=None,
        merge_context=None,
        epic_ref="round9",
        provider="github",
        issue_num=1944,
        issue_title="round9",
        issue_body="",
        complexity="simple",
        item_id="1944",
        branch="agent/1944",
        start_monotonic=0.0,
        refuse_rollback=Mock(),
        terminal_failure=None,
        run_kind="dispatch",
        run_id="issued-run",
    )


def test_round9_contained_foreground_requires_provisioning(
    issued_route, tmp_path, monkeypatch
):
    from kew import dispatch_core, dispatch_sandbox

    store, mint, revoke = issued_route
    monkeypatch.delenv("OPENROUTER_PROVISIONING_KEY")
    runtime = _round9_runtime(monkeypatch)
    config = DispatchConfig.model_validate({"agent": {"model": "openrouter/test"}})
    ctx = _round9_foreground_context(store, config)
    store.reserve_epic_node_item("round9", "1944", "issued-run")
    monkeypatch.setattr(dispatch_core, "_preflight_pricing", lambda *a: None)
    monkeypatch.setattr(dispatch_core, "mark_failed", Mock())
    monkeypatch.setattr(
        dispatch_sandbox,
        "_resolve_launch_image_id",
        lambda *a, **kw: "sha256:" + "b" * 64,
    )

    def foreground(*args, **kwargs):
        return dispatch_sandbox._run_sandboxed_container(
            prompt="round9",
            checkout_dir=tmp_path,
            config=config,
            image="round9",
            runner="opencode",
            issue_num=1944,
            item_id="1944",
            branch="agent/1944",
            title="round9",
            model="openrouter/test",
            store=store,
            run_id="issued-run",
        )

    monkeypatch.setattr(
        dispatch_core,
        "_launch_workspace",
        foreground,
    )
    outcome = dispatch_core._launch_runner(ctx, None).early
    assert outcome is not None
    assert outcome.exit_code == 2
    assert outcome.reason == _ROUND9_REASON
    runtime.assert_not_called()
    assert store.get("issued-run").status == "refused"
    assert store.get("issued-run").exit_code == 2
    assert store.list_epic_reservations("round9") == []
    # #1951 delivers the refusal through RefusalDelivery, which is idempotent;
    # the bare Mock here counts the reason and the category deliveries.
    assert [call.args[0] for call in ctx.refuse_rollback.call_args_list] == [
        _ROUND9_REASON,
        "credential_issuer_refused",
    ]
    mint.assert_not_called()
    revoke.assert_not_called()
    # Exercise the real launcher separately to inspect the refused run's candidate env.
    _round9_unkeyed_launch(tmp_path)
    assert "OPENROUTER_API_KEY" not in runtime.call_args.kwargs["env"]


def test_round9_contained_issued_key_is_explicit_at_real_launcher(
    issued_route, tmp_path, monkeypatch
):
    store, mint, revoke = issued_route
    runtime = _round9_runtime(monkeypatch)
    assert _launch_review(tmp_path, store) == 0
    mint.assert_called_once()
    revoke.assert_called_once()
    environment = runtime.call_args.kwargs["env"]
    assert environment["OPENROUTER_API_KEY"] == "<run>"
    assert "<host>" not in environment.values()
    assert "OPENROUTER_PROVISIONING_KEY" not in environment
    # A subsequent launch without an explicit key cannot recover the ambient key.
    _round9_unkeyed_launch(tmp_path)
    assert "OPENROUTER_API_KEY" not in runtime.call_args.kwargs["env"]


def test_round9_host_without_provisioning_is_refused(
    issued_route, tmp_path, monkeypatch
):
    # Governed round 1 (2026-09-21): a host direct route without provisioning
    # is refused before launch, like a contained one; it never launches with
    # the ambient credential stripped and no bounded credential in its place.
    from kew.run_credentials import ProvisioningRequired, bounded_run_credentials
    from kew.subprocess_utils import TOOL_PATHS

    store, mint, revoke = issued_route
    monkeypatch.delenv("OPENROUTER_PROVISIONING_KEY")
    runtime = _round9_runtime(monkeypatch)
    monkeypatch.setitem(TOOL_PATHS, "opencode", "/recording/opencode")
    with (
        pytest.raises(ProvisioningRequired, match="for direct routes"),
        bounded_run_credentials(
            run_id="issued-run",
            config=DispatchConfig(),
            store=store,
            model="openrouter/test",
            credential_names=("OPENROUTER_API_KEY",),
            timeout_seconds=60,
        ),
    ):
        pytest.fail("a host direct route launched without a bounded credential")
    mint.assert_not_called()
    revoke.assert_not_called()
    event = store.list_audit_events(event="sandbox_resolved", limit=100)[0]
    assert event.payload["credential_issuer"] is None
    assert event.payload["credential_issuer_reason"] == _ROUND9_REASON
    _round9_unkeyed_launch(tmp_path)
    assert "OPENROUTER_API_KEY" not in runtime.call_args.kwargs["env"]


@pytest.mark.parametrize("sidecar", [False, True])
def test_round9_native_and_sidecar_no_key_launches_scrub_openrouter(
    issued_route, tmp_path, monkeypatch, sidecar
):
    from types import SimpleNamespace

    from kew import dispatch_sandbox
    from kew.inference_transport import resolve_inference_transport
    from kew.runners import get_runner

    runtime = _round9_runtime(monkeypatch)
    config = DispatchConfig()
    transport = resolve_inference_transport(
        config, get_runner("claude-code"), "claude-sonnet-4-6"
    )
    monkeypatch.setattr(
        dispatch_sandbox,
        "prepare_sidecar_worker",
        lambda *a, **kw: SimpleNamespace(
            argv=["docker", "start", "--attach", "worker"],
            runtime="docker",
            name="worker",
            container_id="c" * 64,
            checkout_dir=tmp_path,
        ),
    )
    dispatch_sandbox._launch_streaming_container(
        config,
        None,
        run_id="unkeyed-run",
        log_file=tmp_path / "unkeyed.log",
        image="sha256:" + "b" * 64,
        runner="claude-code",
        checkout_dir=tmp_path,
        slug="1944",
        model=transport.model,
        transport=transport,
        sidecar_supervisor=object() if sidecar else None,
        sidecar_ready=None,
        run_kind="dispatch",
    )
    assert "OPENROUTER_API_KEY" not in runtime.call_args.kwargs["env"]


def test_round10_inherited_producer_holds_until_owner_records_revocation(
    issued_route, tmp_path, monkeypatch
):
    from kew import dispatch_core, dispatch_sandbox, run_credentials
    from kew.graph.driver.credentials import issue_driver_key
    from kew.publication_credentials import require_publication_credentials

    store, mint, revoke = issued_route
    key = issue_driver_key(store, DispatchConfig(), "producer-run", "1944")
    mint.reset_mock()
    monkeypatch.setenv("KEW_ISSUED_RUN_KEY_HASH", key.key_hash)
    monkeypatch.setenv("OPENROUTER_API_KEY", key.secret)
    monkeypatch.delenv("OPENROUTER_PROVISIONING_KEY")
    runtime = _round10_producer_runtime(monkeypatch)
    monkeypatch.setattr(
        dispatch_sandbox, "_checkout_has_no_changes", Mock(return_value=False)
    )
    monkeypatch.setattr(dispatch_sandbox, "_run_sandbox_shadow", Mock())
    monkeypatch.setattr(
        dispatch_core,
        "_push_and_open_pr",
        Mock(side_effect=_round10_checked_publication),
    )
    monkeypatch.setattr(dispatch_core, "_mint_publication_continuation", Mock())
    result = _round10_producer(tmp_path, store)
    runtime.assert_called_once()
    assert result.error_category == "publication_held"
    assert result.subtype == "issued_key_unrevoked\nrevocation evidence missing"
    assert store.get("producer-run").status == "awaiting_publication"
    mint.assert_not_called()
    revoke.assert_not_called()
    run_credentials._revoke(store, "producer-run", key)
    require_publication_credentials(store, run_id="producer-run")
    revoke.assert_called_once()


class _Round11Clock(datetime):
    current = datetime(2026, 9, 20, tzinfo=UTC)

    @classmethod
    def now(cls, tz=None):
        return cls.current


@pytest.fixture
def round11_issuer(tmp_path, monkeypatch, bounded_processes):
    import sys

    from kew import credential_issuer, run_credentials
    from kew.graph.driver import credentials

    _Round11Clock.current = datetime(2026, 9, 20, tzinfo=UTC)
    monkeypatch.setattr(run_credentials, "datetime", _Round11Clock)
    monkeypatch.setattr(credentials, "datetime", _Round11Clock)
    reconciliation = sys.modules.get("kew.credential_reconciliation")
    if reconciliation is not None:
        monkeypatch.setattr(reconciliation, "datetime", _Round11Clock)
    monkeypatch.setenv("OPENROUTER_PROVISIONING_KEY", "<manage>")
    monkeypatch.delenv("KEW_ISSUED_RUN_KEY_HASH", raising=False)
    key = credential_issuer.BoundedRunKey("<run>", "a" * 64, 25, "2099-01-01T00:00:00Z")
    monkeypatch.setattr(
        credential_issuer, "mint_bounded_run_key", Mock(return_value=key)
    )
    http = Mock(return_value=(200, b'{"deleted":true}'))
    monkeypatch.setattr(
        credential_issuer,
        "revoke_run_key",
        partial(credential_issuer.revoke_run_key, http=http),
    )
    with SqliteRunStore(":memory:", project_root=tmp_path) as store:
        yield store, http, key


def _round11_state(store, run_id="producer-run", key_hash="a" * 64):
    raw = store.get_meta(f"run_key_revocation:{run_id}:{key_hash}")
    assert raw is not None, "failed DELETE must persist a revocation-pending marker"
    return json.loads(raw)


def _round11_revocation(store, run_id="producer-run"):
    return next(
        event.payload["credential_issuer"]
        for event in store.list_audit_events(event="sandbox_resolved", run_id=run_id)
        if event.payload.get("phase") == "revocation"
    )


def _round11_publication(store, tmp_path, monkeypatch, *, mode="canonical"):
    from kew import dispatch_core

    publish = Mock()
    monkeypatch.setattr(dispatch_core, "_canonical_push_branch", publish)
    monkeypatch.setattr(dispatch_core, "_checkout_push_branch", publish)
    dispatch_core._git_push_branch(
        "agent/1944",
        tmp_path,
        store=store,
        run_id="producer-run",
        publish=mode,
        dangerously_skip_containment=mode == "checkout",
    )
    publish.assert_called_once()


def _round11_foreground(store, tmp_path, monkeypatch):
    from kew import dispatch_core, dispatch_sandbox

    runtime = _round10_producer_runtime(monkeypatch)
    monkeypatch.setattr(
        dispatch_sandbox, "_checkout_has_no_changes", Mock(return_value=False)
    )
    monkeypatch.setattr(dispatch_sandbox, "_run_sandbox_shadow", Mock())
    monkeypatch.setattr(
        dispatch_core,
        "_push_and_open_pr",
        Mock(side_effect=_round10_checked_publication),
    )
    monkeypatch.setattr(dispatch_core, "_post_agent_report", Mock())
    monkeypatch.setattr(dispatch_core, "_mint_publication_continuation", Mock())
    result = _round10_producer(tmp_path, store)
    runtime.assert_called_once()
    return result


def _round11_tick(store, tmp_path, monkeypatch):
    from support.driver_fakes import (
        FakeFactsReader,
        FakeSink,
        FakeSource,
        _advance,
        _facts,
        _node,
    )

    from kew.graph.driver import tick

    monkeypatch.setattr(tick, "remote_branch_exists", Mock(return_value=False))
    _advance(
        deps=FakeSource(nodes=[_node("1")], edges=[]),
        facts=FakeFactsReader(issue_facts_by_num={1: _facts(1, state="OPEN")}),
        store=store,
        sink=FakeSink(),
        lock_path=tmp_path / "round11.lock",
        now=_Round11Clock.current,
    )


@pytest.mark.parametrize(
    "retry", ["driver_tick", "publication", "foreground_finalizer"]
)
@pytest.mark.parametrize("mode", ["canonical", "checkout"])
def test_round11_foreground_503_reconciles_and_releases(
    round11_issuer, tmp_path, monkeypatch, retry, mode
):
    from datetime import timedelta

    from kew import run_credentials
    from kew.publication_canonical import PublicationHeld
    from kew.publication_credentials import require_publication_credentials

    store, http, key = round11_issuer
    http.side_effect = [(503, b"unavailable"), (200, b'{"deleted":true}')]
    result = _round11_foreground(store, tmp_path, monkeypatch)
    assert result.error_category == "publication_held"
    assert store.get("producer-run").status == "awaiting_publication"
    assert _round11_revocation(store)["revoked"] is False
    http.assert_called_once()
    pending = _round11_state(store)
    assert pending["pending"] is True
    assert pending["reason"] == "issuer unavailable"
    with pytest.raises(PublicationHeld):
        require_publication_credentials(store, run_id="producer-run")
    _Round11Clock.current += timedelta(seconds=30)
    if retry == "driver_tick":
        _round11_tick(store, tmp_path, monkeypatch)
    elif retry == "foreground_finalizer":
        run_credentials._finalize_run_key(store, "producer-run", key)
    _round11_publication(store, tmp_path, monkeypatch, mode=mode)
    assert http.call_count == 2
    assert _round11_revocation(store)["revoked"] is True
    assert _round11_state(store)["pending"] is False
    require_publication_credentials(store, run_id="producer-run")


@pytest.mark.parametrize(
    "response",
    [
        (404, b"not JSON"),
        (200, b'{"error":{"message":"Key no longer exists"}}'),
        (400, b'{"error":{"message":"API key not found"}}'),
    ],
)
def test_round11_confirmed_absence_releases(
    round11_issuer, tmp_path, monkeypatch, response
):
    store, http, _ = round11_issuer
    http.return_value = response
    result = _round11_foreground(store, tmp_path, monkeypatch)
    assert _round11_revocation(store)["revoked"] is True
    assert _round11_revocation(store)["reason"] == "already absent"
    assert result.published_pr_url.endswith("/1944")
    _round11_publication(store, tmp_path, monkeypatch)

    http.assert_called_once()


@pytest.mark.parametrize(
    "response,reason",
    [
        ((503, b"unavailable"), "issuer unavailable"),
        (TimeoutError("secret-1944"), "issuer unavailable"),
        (OSError("secret-1944"), "issuer unavailable"),
        ((200, b"not JSON secret-1944"), "malformed issuer reply"),
        ((200, b'{"deleted":false}'), "malformed issuer reply"),
    ],
)
def test_round11_ambiguous_delete_stays_pending_with_bounded_retries(
    round11_issuer, tmp_path, monkeypatch, response, reason
):
    from kew.graph.driver.credentials import reconcile_driver_keys
    from kew.publication_canonical import PublicationHeld

    store, http, _ = round11_issuer
    if isinstance(response, Exception):
        http.side_effect = response
    else:
        http.return_value = response
    _round11_foreground(store, tmp_path, monkeypatch)
    for count in range(1, 38):
        state = _round11_state(store)
        assert state["pending"] is True
        assert state["reason"] == reason
        assert state["retry_count"] == min(count, 32)
        assert http.call_count == count
        with pytest.raises(PublicationHeld):
            _round11_publication(store, tmp_path, monkeypatch)
        reconcile_driver_keys(store, now=_Round11Clock.current)
        assert http.call_count == count, "retry before next_attempt_at must not DELETE"
        due = datetime.fromisoformat(state["next_attempt_at"])
        assert 30 <= (due - _Round11Clock.current).total_seconds() <= 300
        _Round11Clock.current = due
        reconcile_driver_keys(store, now=due)
    assert _round11_revocation(store)["revoked"] is False
    # Repeated failures cannot exhaust the publication evidence window.
    assert len(store.list_audit_events(event="sandbox_resolved")) < 20
    assert "secret-1944" not in str(_round11_state(store))


@pytest.mark.parametrize("status", [401, 403])
def test_round11_invalid_provisioning_warns_once_and_holds(
    round11_issuer, tmp_path, monkeypatch, caplog, status
):
    from kew.graph.driver.credentials import reconcile_driver_keys
    from kew.publication_canonical import PublicationHeld

    store, http, _ = round11_issuer
    http.return_value = (status, b"<manage>")
    _round11_foreground(store, tmp_path, monkeypatch)
    for _ in range(3):
        state = _round11_state(store)
        assert state["pending"] is True
        assert state["reason"] == (
            "invalid provisioning credential; "
            "replace OPENROUTER_PROVISIONING_KEY on the host"
        )
        with pytest.raises(PublicationHeld, match="invalid provisioning credential"):
            _round11_publication(store, tmp_path, monkeypatch)
        _Round11Clock.current = datetime.fromisoformat(state["next_attempt_at"])
        reconcile_driver_keys(store, now=_Round11Clock.current)
    assert (
        caplog.text.count(
            "revocation pending: run=producer-run "
            "outcome=invalid provisioning credential"
        )
        == 1
    )
    assert "<manage>" not in caplog.text
    assert _round11_revocation(store)["revoked"] is False


def test_round11_expiry_requires_confirmed_absence(
    round11_issuer, tmp_path, monkeypatch
):
    from kew.publication_canonical import PublicationHeld

    store, http, _ = round11_issuer
    http.return_value = (503, b"unavailable")
    _round11_foreground(store, tmp_path, monkeypatch)
    _Round11Clock.current = datetime(2100, 1, 1, tzinfo=UTC)
    with pytest.raises(PublicationHeld):
        _round11_publication(store, tmp_path, monkeypatch)
    http.return_value = (404, b"")
    _Round11Clock.current = datetime.fromisoformat(
        _round11_state(store)["next_attempt_at"]
    )
    _round11_publication(store, tmp_path, monkeypatch)
    assert _round11_revocation(store)["reason"] == "already absent"


def test_round11_driver_issued_key_uses_shared_pending_path(
    round11_issuer, tmp_path, monkeypatch
):
    from kew.graph.driver.credentials import issue_driver_key, reconcile_driver_keys
    from kew.publication_credentials import require_publication_credentials

    store, http, _ = round11_issuer
    key = issue_driver_key(store, DispatchConfig(), "producer-run", "1944")
    monkeypatch.setenv("OPENROUTER_API_KEY", key.secret)
    monkeypatch.setenv("KEW_ISSUED_RUN_KEY_HASH", key.key_hash)
    result = _round11_foreground(store, tmp_path, monkeypatch)
    assert result.error_category == "publication_held"
    http.assert_not_called()
    http.side_effect = [(503, b"unavailable"), (200, b'{"deleted":true}')]
    reconcile_driver_keys(store, now=_Round11Clock.current)
    state = _round11_state(store)
    assert state["pending"] is True
    assert state["reason"] == "issuer unavailable"
    _Round11Clock.current = datetime.fromisoformat(state["next_attempt_at"])
    _round11_publication(store, tmp_path, monkeypatch)
    assert http.call_count == 2
    assert _round11_revocation(store)["revoked"] is True
    require_publication_credentials(store, run_id="producer-run")


def test_round11_interrupted_delete_survives_reopen(round11_issuer, tmp_path):
    from datetime import timedelta

    from kew.graph.driver.credentials import reconcile_driver_keys
    from kew.run_credentials import _record, _revoke

    _, http, key = round11_issuer
    path = str(tmp_path / "reopened.db")
    http.side_effect = KeyboardInterrupt()
    with SqliteRunStore(path, project_root=Path(path).parent) as store:
        _record(store, "producer-run", key, phase="issuance")
        with pytest.raises(KeyboardInterrupt):
            _revoke(store, "producer-run", key)
    with SqliteRunStore(path, project_root=Path(path).parent) as reopened:
        state = _round11_state(reopened)
        assert state["pending"] is True
        assert state["reason"] == "revocation outcome unknown"
        http.side_effect = None
        _Round11Clock.current += timedelta(seconds=30)
        reconcile_driver_keys(reopened, now=_Round11Clock.current)
        assert _round11_revocation(reopened)["revoked"] is True
        assert _round11_state(reopened)["pending"] is False
    assert http.call_count == 2


def test_round11_legacy_foreground_failure_is_reconciled(
    round11_issuer, tmp_path, monkeypatch
):
    from kew.run_credentials import _record

    store, http, key = round11_issuer
    http.return_value = (503, b"")
    _round11_foreground(store, tmp_path, monkeypatch)
    # Reproduce round-ten evidence without the new retry projection.
    store._conn.execute("DELETE FROM meta WHERE key LIKE 'run_key_revocation:%'")
    store._conn.commit()
    _record(
        store,
        "producer-run",
        key,
        phase="revocation",
        revoked=False,
        reason="issuer unavailable",
    )
    http.return_value = (200, b'{"deleted":true}')
    _round11_publication(store, tmp_path, monkeypatch)
    assert _round11_revocation(store)["revoked"] is True
    assert http.call_count == 2


def test_round11_next_foreground_finalization_reconciles_pending_backlog(
    round11_issuer, tmp_path, monkeypatch
):
    from datetime import timedelta

    from kew import credential_issuer
    from kew.run_credentials import bounded_run_credentials

    store, http, _ = round11_issuer
    http.return_value = (503, b"unavailable")
    _round11_foreground(store, tmp_path, monkeypatch)
    _Round11Clock.current += timedelta(seconds=30)
    http.return_value = (200, b'{"deleted":true}')
    second = credential_issuer.BoundedRunKey(
        "<next>", "b" * 64, 25, "2099-01-01T00:00:00Z"
    )
    monkeypatch.setattr(
        credential_issuer, "mint_bounded_run_key", Mock(return_value=second)
    )
    with bounded_run_credentials(
        run_id="next-foreground",
        config=DispatchConfig(),
        store=store,
        model="openrouter/test",
        credential_names=(),
        timeout_seconds=60,
    ):
        pass
    assert _round11_revocation(store)["revoked"] is True
    assert http.call_count == 3
    _round11_publication(store, tmp_path, monkeypatch)


def _round12_run(store, key, *, run_id="producer-run", status="awaiting_publication"):
    from kew.readiness import (
        Containment,
        RealizedLaunchBoundary,
        compose_realized_profile,
        compose_route_profile,
    )

    store.save(
        RunRecord(
            run_id=run_id,
            issue_num=1944,
            item_id="1944",
            branch="agent/1944",
            status=status,
            exit_code=0,
            started_at=_Round11Clock.current,
            runner_name="opencode",
            model="openrouter/test",
            container_name="kew-1944",
        )
    )
    store.record_route_profile(
        compose_realized_profile(
            run_id=run_id,
            attempt_number=1,
            route=compose_route_profile(
                runner="opencode", requested_model="openrouter/test"
            ),
            boundary=RealizedLaunchBoundary(
                containment=Containment.CONTAINED,
                image_ref="kew-agent:latest",
                image_id="sha256:" + "b" * 64,
                auth_path=key.auth_path,
            ),
        )
    )


def _round12_checked_delete(store, key, method, path, **kwargs):
    from kew.credential_reconciliation import revocation_state

    assert method == "DELETE"
    assert path.endswith(key.key_hash)
    state = revocation_state(store, "producer-run", key.key_hash)
    assert state["pending"] is True, "persist the claim before DELETE"
    assert state["retry_count"] == 1
    events = store.list_audit_events(event="sandbox_resolved", run_id="producer-run")
    assert events[0].payload["phase"] == "revocation_pending"
    assert not any(event.payload["phase"] == "revocation" for event in events)
    return 200, b'{"deleted":true}'


def test_round12_publication_recovers_issuance_only_crash(round11_issuer, tmp_path):
    from kew.credential_reconciliation import revocation_state
    from kew.publication_credentials import (
        IssuedKeyPublicationHeld,
        ensure_publication_credentials,
        require_publication_credentials,
    )
    from kew.run_credentials import _record

    _, http, key = round11_issuer
    path = str(tmp_path / "pre-marker-crash.db")
    with SqliteRunStore(path, project_root=Path(path).parent) as store:
        _record(store, "producer-run", key, phase="issuance")
        _round12_run(store, key)
        # SIGKILL here cannot execute cleanup or write begin_revocation's marker.
    with SqliteRunStore(path, project_root=Path(path).parent) as store:
        assert revocation_state(store, "producer-run", key.key_hash) == {}
        events = store.list_audit_events(event="sandbox_resolved")
        assert [event.payload["phase"] for event in events] == ["issuance"]
        with pytest.raises(IssuedKeyPublicationHeld):
            require_publication_credentials(store, run_id="producer-run")
        http.assert_not_called()
        http.side_effect = partial(_round12_checked_delete, store, key)
        ensure_publication_credentials(store, run_id="producer-run")
        http.assert_called_once()
        assert _round11_state(store)["pending"] is False
        assert _round11_revocation(store)["revoked"] is True
        assert [
            event.payload["phase"]
            for event in reversed(store.list_audit_events(event="sandbox_resolved"))
        ] == ["issuance", "revocation_pending", "revocation"]
        require_publication_credentials(store, run_id="producer-run")
        ensure_publication_credentials(store, run_id="producer-run")
        http.assert_called_once()


@pytest.mark.parametrize("expired", [False, True])
def test_round12_pending_run_keeps_key_until_terminal(round11_issuer, expired):
    from dataclasses import replace

    from kew.credential_reconciliation import reconcile_pending_key, revocation_state
    from kew.graph.driver.credentials import reconcile_driver_keys
    from kew.publication_credentials import (
        IssuedKeyPublicationHeld,
        ensure_publication_credentials,
    )
    from kew.run_credentials import _record

    store, http, key = round11_issuer
    if expired:
        key = replace(key, expires_at="2000-01-01T00:00:00Z")
    _round12_run(store, key, status="pending")
    _record(store, "producer-run", key, phase="issuance")
    with pytest.raises(IssuedKeyPublicationHeld):
        ensure_publication_credentials(store, run_id="producer-run")
    reconcile_pending_key(store, "producer-run", key.key_hash)
    reconcile_driver_keys(store, now=_Round11Clock.current)
    http.assert_not_called()
    assert revocation_state(store, "producer-run", key.key_hash) == {}
    store.save(replace(store.get("producer-run"), status="killed"))
    reconcile_pending_key(store, "producer-run", key.key_hash)
    http.assert_called_once()
    assert _round11_revocation(store)["revoked"] is True


@pytest.mark.parametrize("scanner", ["driver", "shared"])
def test_round12_scan_finds_reaped_orphan_within_lifecycle_page(
    round11_issuer, monkeypatch, scanner
):
    from dataclasses import replace

    from kew.credential_reconciliation import reconcile_run_keys
    from kew.graph.driver.credentials import reconcile_driver_keys
    from kew.run_credentials import _record

    store, http, key = round11_issuer
    live_key = replace(key, key_hash="b" * 64)
    _round12_run(store, live_key, run_id="live", status="pending")
    for attempt in range(1, 102):
        _record(store, "live", live_key, phase="issuance", attempt_number=attempt)
    _round12_run(store, key, status="pending")
    _record(store, "producer-run", key, phase="issuance")
    _record(store, "producer-run", key, phase="issuance", attempt_number=2)
    assert store.finalize_orphaned_pending(
        "producer-run",
        error_category="orphaned_pending_reaped",
        finished_at=_Round11Clock.current,
    )
    assert store.delete("producer-run")
    page = Mock(wraps=store.list_run_key_issuances)
    monkeypatch.setattr(store, "list_run_key_issuances", page)
    reconcile = reconcile_driver_keys if scanner == "driver" else reconcile_run_keys
    reconcile(store, now=_Round11Clock.current)
    http.assert_not_called()
    page.assert_called_once_with(after_id=0, limit=100)
    cursor = int(store.get_meta("run_credential_issuance_cursor"))
    assert cursor > 0
    reconcile(store, now=_Round11Clock.current)
    assert page.call_args.kwargs == {"after_id": cursor, "limit": 100}
    http.assert_called_once()
    assert _round11_revocation(store)["revoked"] is True
    assert _round11_state(store)["pending"] is False
    for _ in range(2):
        reconcile(store, now=_Round11Clock.current)
    http.assert_called_once()


@pytest.mark.parametrize("response", [(404, b""), (200, b'{"deleted":true}')])
def test_round12_expired_orphan_requires_confirmed_delete(round11_issuer, response):
    from dataclasses import replace

    from kew.publication_credentials import (
        ensure_publication_credentials,
        require_publication_credentials,
    )
    from kew.run_credentials import _record

    store, http, key = round11_issuer
    key = replace(key, expires_at="2000-01-01T00:00:00Z")
    _round12_run(store, key)
    _record(store, "producer-run", key, phase="issuance")
    http.return_value = response
    ensure_publication_credentials(store, run_id="producer-run")
    http.assert_called_once()
    outcome = _round11_revocation(store)
    assert outcome["revoked"] is True
    assert outcome.get("reason") == ("already absent" if response[0] == 404 else None)
    assert _round11_state(store)["pending"] is False
    require_publication_credentials(store, run_id="producer-run")
    ensure_publication_credentials(store, run_id="producer-run")
    http.assert_called_once()


@pytest.mark.parametrize(
    ("actor_type", "actor_id"),
    [("worker", "credential-issuer"), ("policy", "worker")],
)
def test_round12_forged_issuance_cannot_authorize_delete(
    round11_issuer, actor_type, actor_id
):
    from kew.credential_reconciliation import reconcile_pending_key, revocation_state
    from kew.graph.driver.credentials import reconcile_driver_keys
    from kew.run_credentials import _record

    store, http, key = round11_issuer
    _round12_run(store, key)
    store.append_audit_event(
        timestamp=_Round11Clock.current.isoformat(),
        event="sandbox_resolved",
        run_id="producer-run",
        actor_type=actor_type,
        actor_id=actor_id,
        payload={
            "phase": "issuance",
            "attempt_number": 1,
            "credential_issuer": key.identity,
        },
    )
    reconcile_pending_key(store, "producer-run", key.key_hash)
    reconcile_driver_keys(store, now=_Round11Clock.current)
    http.assert_not_called()
    assert revocation_state(store, "producer-run", key.key_hash) == {}
    # The matching host event, without any revocation event, establishes the obligation.
    _record(store, "producer-run", key, phase="issuance")
    reconcile_pending_key(store, "producer-run", key.key_hash)
    http.assert_called_once()
    assert _round11_revocation(store)["revoked"] is True


def test_round12_legacy_failed_delete_remains_retryable_and_idempotent(round11_issuer):
    from kew.credential_reconciliation import reconcile_pending_key, revocation_state
    from kew.run_credentials import _record

    store, http, key = round11_issuer
    _round12_run(store, key)
    _record(
        store,
        "producer-run",
        key,
        phase="revocation",
        revoked=False,
        reason="issuer unavailable",
    )
    assert revocation_state(store, "producer-run", key.key_hash) == {}
    reconcile_pending_key(store, "producer-run", key.key_hash)
    http.assert_called_once()

    assert _round11_revocation(store)["revoked"] is True
    assert _round11_state(store)["pending"] is False
    # Older stores can have confirmation without the newer metadata projection.
    store._conn.execute("DELETE FROM meta WHERE key LIKE 'run_key_revocation:%'")
    store._conn.commit()
    reconcile_pending_key(store, "producer-run", key.key_hash)
    http.assert_called_once()


def _round12_continuation(store, worktree):
    from support.checkout_generations import (
        fixture_checkout_generation,
        fixture_claim_evidence,
    )

    from kew.audit import Actor, emit_continuation_evidence

    # PR #1952 made the checkout generation and claim evidence required.
    store.record_continuation(
        "epic:1944",
        "1944",
        "producer-run",
        "agent/1944",
        worktree,
        checkout_generation=fixture_checkout_generation(worktree),
    )
    assert (
        store.claim_continuation_under_cap(
            "epic:1944",
            "1944",
            "producer-run",
            "agent/1944",
            worktree,
            "current",
            0,
            claim_evidence=fixture_claim_evidence(
                store,
                "epic:1944",
                "1944",
                "producer-run",
                "agent/1944",
                worktree,
                "current",
            ),
        )
        == "claimed"
    )
    record = store.get_continuation(
        "epic:1944", "1944", "producer-run", "agent/1944", worktree
    )
    emit_continuation_evidence(
        store,
        "continuation_claimed",
        actor=Actor("policy", "test"),
        phase="claim",
        epic_ref=record.epic_ref,
        item_id=record.item_id,
        original_run_id=record.original_run_id,
        continuation_run_id="current",
        branch=record.branch,
        worktree_identity=record.worktree_identity,
        generation=record.generation,
        state=record.state,
        outcome="continuing",
        run_id="current",
        issue_num=1944,
    )


def _round12_predecessor(store, monkeypatch):
    from types import SimpleNamespace

    identity = {
        "lineage_key": "github:acme/widgets:agent/1944",
        "source_kind": "github",
        "source_id": "acme/widgets",
        "branch": "agent/1944",
        "attempt_number": 1,
    }
    predecessor = SimpleNamespace(
        **identity,
        job_id="predecessor",
        run_id="producer-run",
        predecessor_job_id=None,
        superseded_by="current-job",
    )
    current = SimpleNamespace(
        **identity,
        job_id="current-job",
        run_id="current",
        predecessor_job_id="predecessor",
        superseded_by=None,
    )
    monkeypatch.setattr(store, "get_normalized_job", Mock(return_value=predecessor))
    return current


@pytest.mark.parametrize("relation", ["ancestor", "predecessor"])
def test_round12_publication_recovers_all_lineage_issuances(
    round11_issuer, tmp_path, monkeypatch, relation
):
    from dataclasses import replace

    from kew.publication_credentials import (
        ensure_publication_credentials,
        require_normalized_job_credentials,
        require_publication_credentials,
    )
    from kew.run_credentials import _record

    store, http, key = round11_issuer
    current_key = replace(key, key_hash="b" * 64)
    for run_id, issued in (("producer-run", key), ("current", current_key)):
        _round12_run(store, issued, run_id=run_id)
        _record(store, run_id, issued, phase="issuance")
    if relation == "ancestor":
        _round12_continuation(store, tmp_path / "preserved")
        reconcile = partial(ensure_publication_credentials, store, run_id="current")
    else:
        job = _round12_predecessor(store, monkeypatch)
        reconcile = partial(require_normalized_job_credentials, store, job)
    reconcile()
    assert http.call_count == 2
    for run_id in ("producer-run", "current"):
        assert _round11_revocation(store, run_id)["revoked"] is True
        require_publication_credentials(store, run_id=run_id)
    reconcile()
    assert http.call_count == 2


def _round13_marker(store, key, run_id="producer-run"):
    raw = store.get_meta(f"run_key_prelaunch:{run_id}:{key.key_hash}")
    assert raw is not None, "issuance must have durable pre-launch protection"
    return json.loads(raw)


def _round13_tombstone(store, key, run_id="producer-run"):
    raw = store.get_meta(f"run_key_prelaunch_tombstone:{run_id}:{key.key_hash}")
    assert raw is not None, "owner cleanup must tombstone before DELETE"
    return json.loads(raw)


def _round13_prepare_paused(prepare, ready, resume, *args):
    result = prepare(*args)
    ready.set()
    assert resume.wait(10), "test did not release the prepared key"
    return result


def _round13_child(store, pending, finish, cmd, **kwargs):
    from dataclasses import replace

    key = kwargs["credential"]
    run_id = cmd[cmd.index("--run-id") + 1]
    _round12_run(store, key, run_id=run_id, status="pending")
    pending.set()
    assert finish.wait(10), "test did not release the pending child"
    store.save(replace(store.get(run_id), status="success"))
    return Mock()


def _round13_dispatch_thread(path, pending, finish):
    from kew.config import AgentConfig
    from kew.graph.driver.dispatch import DetachedDispatchSink

    with SqliteRunStore(path, project_root=Path(path).parent) as store:
        sink = DetachedDispatchSink(
            epic_ref="1944",
            config=DispatchConfig(agent=AgentConfig(model="openrouter/test")),
            store=store,
            popen=partial(_round13_child, store, pending, finish),
            publication_token=Mock(return_value=None),
        )
        return sink.dispatch("1944")


def _round13_scan_all(store, run_id, key):
    from kew.credential_reconciliation import reconcile_pending_key, reconcile_run_keys
    from kew.graph.driver.credentials import reconcile_driver_keys
    from kew.publication_credentials import (
        IssuedKeyPublicationHeld,
        ensure_publication_credentials,
    )

    reconcile_run_keys(store)
    # This is also the delegation watcher's per-pass credential hook.
    reconcile_driver_keys(store)
    reconcile_run_keys(store, pending_only=True)
    reconcile_pending_key(store, run_id, key.key_hash)
    try:
        ensure_publication_credentials(store, run_id=run_id)
    except IssuedKeyPublicationHeld:
        # Re-audit F8: a pre-launch key with no run row holds publication too.
        run = store.get(run_id)
        assert run is None or run.status == "pending"


def test_round13_concurrent_prelaunch_scans_preserve_key_until_terminal(
    round11_issuer, tmp_path, monkeypatch, caplog
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from kew.graph.driver import dispatch

    _, http, key = round11_issuer
    ready, resume, pending, finish = Event(), Event(), Event(), Event()
    path = str(tmp_path / "prelaunch-race.db")
    monkeypatch.setattr(
        dispatch, "resolve_kew_console_script", Mock(return_value="/bin/kew")
    )
    monkeypatch.setattr(
        dispatch,
        "prepare_driver_key",
        partial(_round13_prepare_paused, dispatch.prepare_driver_key, ready, resume),
    )
    caplog.set_level("DEBUG", logger="kew.credential_prelaunch")
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_round13_dispatch_thread, path, pending, finish)
        try:
            assert ready.wait(10)
            with SqliteRunStore(path, project_root=Path(path).parent) as observer:
                issuance = observer.list_driver_key_issuances()[0]
                run_id = issuance.run_id
                assert observer.get(run_id) is None
                _round13_scan_all(observer, run_id, key)
                http.assert_not_called()  # b4359779 DELETEs the live pre-launch key.
                marker = _round13_marker(observer, key, run_id)
                assert marker == {
                    "run_id": run_id,
                    "key_hash": key.key_hash,
                    "name": key.name,
                    "issuing_pid": os.getpid(),
                    "issued_at": _Round11Clock.current.isoformat(),
                    "expires_at": key.expires_at,
                    "child_spawned": False,
                }
                assert "pre-launch issuance marker skipped" in caplog.text
                resume.set()
                assert pending.wait(10)
                _round13_scan_all(observer, run_id, key)
                http.assert_not_called()
                finish.set()
                assert future.result(timeout=10)
                _round13_scan_all(observer, run_id, key)
                _round13_scan_all(observer, run_id, key)
                http.assert_called_once()
                assert _round11_revocation(observer, run_id)["revoked"] is True
        finally:
            resume.set()
            finish.set()
            future.result(timeout=10)


@pytest.mark.parametrize("evidence", ["audit", "finalize"])
def test_round13_absent_row_requires_reaping_tombstone(round11_issuer, evidence):
    from kew.credential_reconciliation import reconcile_run_keys
    from kew.graph.driver.credentials import issue_driver_key

    store, http, key = round11_issuer
    issue_driver_key(store, DispatchConfig(), "producer-run", "1944")
    reconcile_run_keys(store)
    http.assert_not_called()  # b4359779 treats absence alone as terminal.
    if evidence == "audit":
        store.append_audit_event(
            timestamp=_Round11Clock.current.isoformat(),
            event="orphaned_run_reaped",
            actor_type="policy",
            actor_id="driver_reconcile",
            run_id="producer-run",
        )
    else:
        _round12_run(store, key, status="pending")
        assert store.finalize_orphaned_pending(
            "producer-run",
            error_category="orphaned_pending_reaped",
            finished_at=_Round11Clock.current,
        )
        assert store.delete("producer-run")
    reconcile_run_keys(store)
    reconcile_run_keys(store)
    http.assert_called_once()
    assert _round11_revocation(store)["revoked"] is True


@pytest.mark.parametrize("response", [(404, b""), (200, b'{"deleted":true}')])
def test_round13_absent_row_expiry_confirms_once(round11_issuer, response):
    from kew.credential_reconciliation import reconcile_run_keys
    from kew.graph.driver.credentials import issue_driver_key

    store, http, key = round11_issuer
    issue_driver_key(store, DispatchConfig(), "producer-run", "1944")
    reconcile_run_keys(store)
    http.assert_not_called()  # b4359779 revokes before expires_at.
    http.return_value = response
    _Round11Clock.current = datetime.fromisoformat(key.expires_at)
    reconcile_run_keys(store)
    reconcile_run_keys(store)
    http.assert_called_once()
    outcome = _round11_revocation(store)
    assert outcome["revoked"] is True
    assert outcome.get("reason") == ("already absent" if response[0] == 404 else None)
    assert _round11_state(store)["pending"] is False


def _round13_checked_cleanup(store, key, method, path, **kwargs):
    issuance = store.list_driver_key_issuances()[0]
    run_id = issuance.run_id
    _round13_marker(store, key, run_id)
    _round13_tombstone(store, key, run_id)
    assert _round11_state(store, run_id)["pending"] is True
    assert method == "DELETE"
    return 200, b'{"deleted":true}'


@pytest.mark.parametrize("failure", ["popen", "environment"])
def test_round13_driver_launch_failure_tombstones_before_delete(
    round11_issuer, monkeypatch, failure
):
    from kew.config import AgentConfig
    from kew.graph.driver import dispatch

    store, http, key = round11_issuer
    monkeypatch.setattr(
        dispatch, "resolve_kew_console_script", Mock(return_value="/bin/kew")
    )
    spawn = Mock(side_effect=OSError("launch failed"))
    if failure == "environment":
        monkeypatch.setattr(
            dispatch, "launch_environment", Mock(side_effect=OSError("launch failed"))
        )
    http.side_effect = partial(_round13_checked_cleanup, store, key)
    sink = dispatch.DetachedDispatchSink(
        epic_ref="1944",
        config=DispatchConfig(agent=AgentConfig(model="openrouter/test")),
        store=store,
        popen=spawn,
        publication_token=Mock(return_value=None),
    )
    with pytest.raises(OSError, match="launch failed"):
        sink.dispatch("1944")
    run_id = store.list_driver_key_issuances()[0].run_id
    # b4359779 has no tombstone, and misses environment-construction cleanup.
    _round13_tombstone(store, key, run_id)
    http.assert_called_once()
    assert _round11_revocation(store, run_id)["revoked"] is True


def test_round13_driver_exit_closes_only_own_unspawned_keys(
    round11_issuer, tmp_path, monkeypatch
):
    from dataclasses import replace

    from kew import credential_issuer
    from kew.config import AgentConfig
    from kew.graph.driver import credentials, dispatch

    store, http, key = round11_issuer
    http.side_effect = partial(_round13_checked_cleanup, store, key)
    with credentials.driver_key_cleanup(store):
        credentials.issue_driver_key(store, DispatchConfig(), "producer-run", "1944")
        # A different driver store owns its prepared key, even in this process.
        with SqliteRunStore(":memory:", project_root=tmp_path) as other:
            credentials.issue_driver_key(other, DispatchConfig(), "other", "1944")
            credentials.reconcile_driver_keys(store)
            http.assert_not_called()  # b4359779 closes the prepared key on a tick.
        spawned = replace(key, key_hash="b" * 64)
        monkeypatch.setattr(
            credential_issuer, "mint_bounded_run_key", Mock(return_value=spawned)
        )
        monkeypatch.setattr(
            dispatch, "resolve_kew_console_script", Mock(return_value="/bin/kew")
        )
        sink = dispatch.DetachedDispatchSink(
            epic_ref="1944",
            config=DispatchConfig(agent=AgentConfig(model="openrouter/test")),
            store=store,
            popen=Mock(return_value=Mock()),
            publication_token=Mock(return_value=None),
        )
        assert sink.dispatch("1944")
        spawned_run = store.list_driver_key_issuances()[1].run_id
        assert store.get(spawned_run) is None
    _round13_tombstone(store, key)
    http.assert_called_once()
    assert http.call_args.args[1].endswith(key.key_hash)
    assert _round11_revocation(store)["revoked"] is True
    assert _round13_marker(store, spawned, spawned_run)["child_spawned"] is True


def test_round13_foreground_mint_records_marker_before_evidence(round11_issuer):
    from kew.run_credentials import bounded_run_credentials

    store, http, key = round11_issuer
    with bounded_run_credentials(
        run_id="producer-run",
        config=DispatchConfig(),
        store=store,
        model="openrouter/test",
        credential_names=("OPENROUTER_API_KEY",),
        timeout_seconds=60,
    ):
        marker = _round13_marker(store, key)  # b4359779 never records this marker.
        assert marker["issuing_pid"] == os.getpid()
        assert store.list_run_key_issuances()[0].payload["prelaunch_protocol"] == 1
    _round13_tombstone(store, key)
    http.assert_called_once()


def test_round13_hard_death_marker_waits_with_operator_warning(round11_issuer, caplog):
    from datetime import timedelta

    from kew.credential_reconciliation import reconcile_run_keys
    from kew.graph.driver.credentials import issue_driver_key

    store, http, key = round11_issuer
    issue_driver_key(store, DispatchConfig(), "producer-run", "1944")
    _Round11Clock.current += timedelta(days=2)
    reconcile_run_keys(store)
    reconcile_run_keys(store)
    http.assert_not_called()  # b4359779 revokes the hard-death marker immediately.
    assert caplog.text.count("pre-launch issuance marker waiting") == 1
    assert "producer-run" in caplog.text
    assert key.key_hash in caplog.text
    assert key.expires_at in caplog.text
    assert key.secret not in caplog.text


def _round13_append_after_marker(store, key, append, **kwargs):
    marker = _round13_marker(store, key, kwargs["run_id"])
    assert marker["expires_at"] == key.expires_at
    assert marker["issued_at"] <= kwargs["timestamp"]
    assert store.list_run_key_issuances() == []
    return append(**kwargs)


def test_round13_driver_marker_commits_before_issuance(round11_issuer, monkeypatch):
    from kew.graph.driver.credentials import issue_driver_key

    store, http, key = round11_issuer
    append = Mock(
        side_effect=partial(
            _round13_append_after_marker, store, key, store.append_audit_event
        )
    )
    monkeypatch.setattr(store, "append_audit_event", append)
    # b4359779 reaches append_audit_event with no durable pre-launch marker.
    assert issue_driver_key(store, DispatchConfig(), "producer-run", "1944") == key
    append.assert_called_once()
    http.assert_not_called()


def _round14_fake_docker(tmp_path, monkeypatch):
    import sys

    from kew import subprocess_utils

    observed = tmp_path / "child-environment.json"
    docker = tmp_path / "docker"
    docker.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        f"open({str(observed)!r}, 'w').write(json.dumps(dict(os.environ)))\n"
        # A child can learn a credential independently of its environment.
        "print('<manage>', file=sys.stderr)\n"
        "print(json.dumps(dict(os.environ)), file=sys.stderr)\n"
        "sys.exit(1)\n"
    )
    docker.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    monkeypatch.setitem(subprocess_utils.TOOL_PATHS, "docker", str(docker))
    return observed


@pytest.mark.serial
@pytest.mark.parametrize("owner", ["foreground", "driver"])
def test_round14_admission_child_environment_and_diagnostic(
    issued_route, tmp_path, monkeypatch, owner
):
    from kew import sandbox
    from kew.graph.driver.credentials import issue_driver_key
    from kew.run_credentials import ISSUED_KEY_HASH_ENV, bounded_run_credentials

    store, _, _ = issued_route
    observed = _round14_fake_docker(tmp_path, monkeypatch)
    if owner == "driver":
        key = issue_driver_key(store, DispatchConfig(), "admission-run", "1944")
        monkeypatch.setenv(ISSUED_KEY_HASH_ENV, key.key_hash)
        monkeypatch.setenv("OPENROUTER_API_KEY", key.secret)
    with bounded_run_credentials(
        run_id="admission-run",
        config=DispatchConfig(),
        store=store,
        model="openrouter/test",
        credential_names=("OPENROUTER_API_KEY",),
        timeout_seconds=60,
    ):
        resolution = sandbox.inspect_image_id("docker", "test-image")
        child_env = json.loads(observed.read_text())
        # 4700ef17 leaks the ambient key in foreground admission.
        assert "OPENROUTER_API_KEY" not in child_env
        assert "OPENROUTER_PROVISIONING_KEY" not in child_env
        assert resolution.status == sandbox.ImageIdStatus.UNAVAILABLE
        # 4700ef17 retains a credential printed independently of inheritance.
        for secret in ("<host>", "<manage>", "<run>"):
            assert secret not in resolution.detail


@pytest.mark.serial
@pytest.mark.parametrize("api", ["run", "popen"])
def test_round14_scope_filters_generic_children_and_clears(
    issued_route, tmp_path, monkeypatch, api
):
    import subprocess

    from kew import subprocess_utils
    from kew.run_credentials import bounded_run_credentials

    store, _, _ = issued_route
    observed = _round14_fake_docker(tmp_path, monkeypatch)
    with (
        pytest.raises(RuntimeError, match="unwind"),
        bounded_run_credentials(
            run_id="scope-run",
            config=DispatchConfig(),
            store=store,
            model="openrouter/test",
            credential_names=("OPENROUTER_API_KEY",),
            timeout_seconds=60,
        ),
    ):
        if api == "run":
            subprocess_utils.run(["docker"], check=False)
        else:
            with subprocess_utils.popen(["docker"], stderr=subprocess.PIPE) as child:
                child.communicate(timeout=10)
        env = json.loads(observed.read_text())
        assert "OPENROUTER_API_KEY" not in env  # Fails on 4700ef17.
        assert "OPENROUTER_PROVISIONING_KEY" not in env
        raise RuntimeError("unwind")
    assert "OPENROUTER_API_KEY" not in subprocess_utils.child_environment()
    assert os.environ["OPENROUTER_API_KEY"] == "<host>"
    assert os.environ["OPENROUTER_PROVISIONING_KEY"] == "<manage>"


_ROUND14_CRASH_CHILD = r"""
import json, os, signal, sys
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from kew import credential_issuer, run_credentials
from kew.config import DispatchConfig
from kew.run_store import SqliteRunStore

root, boundary = Path(sys.argv[1]), sys.argv[2]
os.environ['OPENROUTER_PROVISIONING_KEY'] = '<child>'

def pause():
    (root / 'ready').write_text('ready')
    signal.pause()

def issuer(method, url, *, headers, body):
    if boundary == 'before-post':
        pause()
    request = json.loads(body)
    (root / 'issuer-post.json').write_text(json.dumps(request))
    return 201, json.dumps({'key': '<fake-run>', 'data': {
        **request, 'hash': 'e' * 64,
    }}).encode()

def marker(*args, **kwargs):
    pause()

credential_issuer.mint_bounded_run_key = partial(
    credential_issuer.mint_bounded_run_key, http=issuer)
run_credentials.record_prelaunch = marker
with SqliteRunStore(str(root / 'crash.db'), project_root=root) as store:
    run_credentials.mint_run_credential(
        run_id='round14-crash', config=DispatchConfig(), store=store,
        timeout_seconds=60, limit_usd=1,
    )
"""


def _round14_kill_minter(tmp_path, boundary, script=_ROUND14_CRASH_CHILD):
    import subprocess
    import sys
    import time

    child_env = dict(os.environ)
    child_env.pop("OPENROUTER_API_KEY", None)
    child_env.pop("OPENROUTER_PROVISIONING_KEY", None)
    with subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path), boundary],
        env=child_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as child:
        try:
            deadline = time.monotonic() + 15
            while not (tmp_path / "ready").exists() and child.poll() is None:
                assert time.monotonic() < deadline, "mint boundary was not reached"
                time.sleep(0.01)
            assert child.poll() is None, child.communicate()[1].decode()
            child.kill()
            child.communicate(timeout=10)
            assert child.returncode == -9
            return child.pid
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=10)


class _Round14Issuer:
    def __init__(self, root, mode):
        self.root, self.mode, self.calls = root, mode, []

    def __call__(self, method, url, *, headers, body):
        self.calls.append((method, url))
        assert headers["Authorization"] == "Bearer <parent>"
        if method == "DELETE":
            assert url.endswith("e" * 64)
            return 200, b'{"deleted":true}'
        assert method == "GET"
        assert "include_disabled=true&offset=0" in url
        if self.mode == "unsupported":
            return 405, b"not supported"
        posted = self.root / "issuer-post.json"
        data = []
        if posted.exists() and self.mode == "present":
            data = [{**json.loads(posted.read_text()), "hash": "e" * 64}]
        return 200, json.dumps({"data": data}).encode()


@pytest.mark.serial
@pytest.mark.parametrize("mode", ["present", "absent", "unsupported"])
def test_round14_killed_response_before_marker_recovers_intent(
    tmp_path, monkeypatch, caplog, mode
):
    from datetime import timedelta

    from kew import credential_issuer
    from kew.credential_reconciliation import reconcile_run_keys

    pid = _round14_kill_minter(tmp_path, "after-response")
    assert (tmp_path / "issuer-post.json").exists()
    issuer = _Round14Issuer(tmp_path, mode)
    monkeypatch.setenv("OPENROUTER_PROVISIONING_KEY", "<parent>")
    monkeypatch.setattr(
        credential_issuer,
        "lookup_run_key_hashes",
        partial(
            getattr(credential_issuer, "lookup_run_key_hashes", Mock()), http=issuer
        ),
        raising=False,
    )
    monkeypatch.setattr(
        credential_issuer,
        "revoke_run_key",
        partial(credential_issuer.revoke_run_key, http=issuer),
    )
    with SqliteRunStore(str(tmp_path / "crash.db"), project_root=tmp_path) as store:
        raw = store.get_meta("run_key_intent:round14-crash")
        assert raw is not None  # 4700ef17 loses the obligation after POST.
        intent = json.loads(raw)
        assert intent["issued_by"] == pid
        assert intent["pending"] is True
        assert intent["name"] == f"kew-run:{store.read_ledger_id()}:round14-crash"
        assert intent["limit_usd"] == 1
        assert (
            intent["expires_at"]
            == json.loads((tmp_path / "issuer-post.json").read_text())["expires_at"]
        )
        assert store.list_run_key_issuances() == []
        assert store.get_meta("run_key_prelaunch:round14-crash:" + "e" * 64) is None
        # No recovery races a live POST or a delayed foreground admission.
        reconcile_run_keys(store)
        assert issuer.calls == []
        now = datetime.fromisoformat(intent["expires_at"]) + timedelta(seconds=1)
        reconcile_run_keys(store, now=now)
        reconcile_run_keys(store, now=now)
        state = json.loads(store.get_meta("run_key_intent:round14-crash"))
        assert state["pending"] is (mode == "unsupported")
        assert [call[0] for call in issuer.calls] == (
            ["GET", "DELETE"] if mode == "present" else ["GET"]
        )
        if mode == "present":
            assert _round11_revocation(store, "round14-crash")["revoked"] is True
            assert state["outcome"] == "confirmed deletion"
        elif mode == "absent":
            assert state["outcome"] == "confirmed absence"
        else:
            assert "run=round14-crash" in caplog.text
            assert "lookup unsupported" in caplog.text
        assert "<parent>" not in str(state) + caplog.text


@pytest.mark.serial
def test_round14_killed_before_post_settles_after_one_lookup(tmp_path, monkeypatch):
    from datetime import timedelta

    from kew import credential_issuer
    from kew.credential_reconciliation import reconcile_run_keys

    _round14_kill_minter(tmp_path, "before-post")
    assert not (tmp_path / "issuer-post.json").exists()
    issuer = _Round14Issuer(tmp_path, "absent")
    monkeypatch.setenv("OPENROUTER_PROVISIONING_KEY", "<parent>")
    monkeypatch.setattr(
        credential_issuer,
        "lookup_run_key_hashes",
        partial(
            getattr(credential_issuer, "lookup_run_key_hashes", Mock()), http=issuer
        ),
        raising=False,
    )
    with SqliteRunStore(str(tmp_path / "crash.db"), project_root=tmp_path) as store:
        raw = store.get_meta("run_key_intent:round14-crash")
        assert raw is not None  # 4700ef17 has no pre-POST obligation.
        now = datetime.fromisoformat(json.loads(raw)["expires_at"]) + timedelta(
            seconds=1
        )
        reconcile_run_keys(store, now=now)
        reconcile_run_keys(store, now=now)
        assert len(issuer.calls) == 1
        state = json.loads(store.get_meta("run_key_intent:round14-crash"))
        assert state["outcome"] == "confirmed absence"
        assert state["pending"] is False


class _Round15BlockedIssuer:
    """Commit POST only after recovery has observed the empty issuer list."""

    def __init__(self):
        from threading import Event

        self.started, self.release = Event(), Event()
        self.present = False
        self.calls = []

    def __call__(self, method, url, *, headers, body):
        self.calls.append(method)
        if method == "POST":
            self.name = json.loads(body)["name"]
            self.started.set()
            assert self.release.wait(10), "test did not release POST"
            self.present = True
            raise KeyboardInterrupt("issuer committed; owner died before reply")
        if method == "DELETE":
            assert url.endswith("e" * 64)
            self.present = False
            return 200, b'{"deleted":true}'
        assert method == "GET"
        data = [{"name": self.name, "hash": "e" * 64}] if self.present else []
        return 200, json.dumps({"data": data}).encode()


def _round15_blocked_mint(path):
    from kew.run_credentials import mint_run_credential

    with SqliteRunStore(path, project_root=Path(path).parent) as store:
        mint_run_credential(
            run_id="round15-race",
            config=DispatchConfig(),
            store=store,
            timeout_seconds=60,
            limit_usd=1,
        )


@pytest.mark.serial
def test_round15_blocked_post_survives_negative_lookup(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from datetime import timedelta

    from kew import credential_issuer, run_credentials
    from kew.credential_reconciliation import reconcile_run_keys

    created = datetime.now(UTC)
    expiry = created + timedelta(seconds=1)
    path = str(tmp_path / "round15-race.db")
    issuer = _Round15BlockedIssuer()
    monkeypatch.setenv("OPENROUTER_PROVISIONING_KEY", "<manage15>")
    monkeypatch.setattr(run_credentials, "_expiry_after", Mock(return_value=expiry))
    for name in ("mint_bounded_run_key", "lookup_run_key_hashes", "revoke_run_key"):
        monkeypatch.setattr(
            credential_issuer,
            name,
            partial(getattr(credential_issuer, name), http=issuer),
        )
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_round15_blocked_mint, path)
        try:
            assert issuer.started.wait(10)
            with SqliteRunStore(path, project_root=Path(path).parent) as observer:
                reconcile_run_keys(observer, now=expiry)
                assert issuer.calls == ["POST", "GET"]
                state = json.loads(observer.get_meta("run_key_intent:round15-race"))
                # 54d330b6 settles here, while POST can still create a key.
                assert state["pending"] is True
                issuer.release.set()
                with pytest.raises(KeyboardInterrupt):
                    future.result(timeout=10)
                assert issuer.present
                assert observer.list_run_key_issuances() == []
                assert (
                    observer.get_meta("run_key_prelaunch:round15-race:" + "e" * 64)
                    is None
                )
                reconcile_run_keys(observer, now=expiry + timedelta(seconds=300))
                state = json.loads(observer.get_meta("run_key_intent:round15-race"))
                assert state["pending"] is False
                assert state["outcome"] == "confirmed deletion"
                assert issuer.calls == ["POST", "GET", "GET", "DELETE"]
                assert not issuer.present
                assert _round11_revocation(observer, "round15-race")["revoked"] is True
        finally:
            issuer.release.set()
            with pytest.raises(KeyboardInterrupt):
                future.result(timeout=10)


@pytest.mark.parametrize("first_scan_seconds", [1, 20])
def test_round15_absence_waits_past_settlement_instant(
    tmp_path, monkeypatch, first_scan_seconds
):
    from datetime import timedelta

    from kew import credential_issuer
    from kew.credential_intents import record_issuance_intent
    from kew.credential_reconciliation import reconcile_run_keys

    created = datetime(2026, 9, 20, tzinfo=UTC)
    lookup = Mock(return_value=[])
    monkeypatch.setattr(credential_issuer, "lookup_run_key_hashes", lookup)
    with SqliteRunStore(str(tmp_path / "absence.db"), project_root=tmp_path) as store:
        record_issuance_intent(
            store, "round15-absent", 1, created + timedelta(seconds=30), created
        )
        raw = json.loads(store.get_meta("run_key_intent:round15-absent"))
        raw["next_attempt_at"] = created.isoformat()
        store.set_meta("run_key_intent:round15-absent", json.dumps(raw))
        before = created + timedelta(seconds=first_scan_seconds)
        reconcile_run_keys(store, now=before)
        state = json.loads(store.get_meta("run_key_intent:round15-absent"))
        assert state["pending"] is True  # 54d330b6 settles on this empty lookup.
        assert datetime.fromisoformat(state["next_attempt_at"]) == before + timedelta(
            seconds=30
        )
        reconcile_run_keys(store, now=before + timedelta(seconds=29))
        lookup.assert_called_once_with(
            f"kew-run:{store.read_ledger_id()}:round15-absent"
        )
        reconcile_run_keys(store, now=before + timedelta(seconds=30))
        state = json.loads(store.get_meta("run_key_intent:round15-absent"))
        assert state["pending"] is False
        assert state["outcome"] == "confirmed absence"
        assert lookup.call_count == 2


def _round15_corrupt_append(append, mode, **kwargs):
    payload = kwargs.get("payload") or {}
    if payload.get("phase") != "issuance" or not payload.get("credential_issuer"):
        return append(**kwargs)
    if mode == "append_error":
        raise OSError("synthetic private storage diagnostic")
    if mode == "absent":
        return 999999
    if mode == "identity":
        kwargs["payload"] = {
            **payload,
            "credential_issuer": {**payload["credential_issuer"], "key_hash": "f" * 64},
        }
    elif mode == "actor":
        kwargs["actor_id"] = "untrusted-child"
    return append(**kwargs)


def _round15_credential_owner(owner, tmp_path, store):
    from kew.graph.driver.credentials import issue_driver_key
    from kew.run_credentials import bounded_run_credentials

    if owner == "review":
        return _launch_review(tmp_path, store)
    if owner == "producer":
        return _round10_producer(tmp_path, store)
    if owner == "driver":
        return issue_driver_key(store, DispatchConfig(), "issued-run", "1944")
    with bounded_run_credentials(
        run_id="issued-run",
        config=DispatchConfig(),
        store=store,
        model="openrouter/test",
        credential_names=("OPENROUTER_API_KEY",),
        timeout_seconds=60,
    ) as key:
        return key


@pytest.mark.parametrize("owner", ["review", "producer", "driver", "context"])
@pytest.mark.parametrize("mode", ["append_error", "absent", "identity", "actor"])
def test_round15_strict_evidence_refuses_before_yield_or_launch(
    issued_route, tmp_path, monkeypatch, caplog, owner, mode
):
    from datetime import timedelta

    from kew import credential_issuer
    from kew.credential_reconciliation import reconcile_run_keys

    store, mint, revoke = issued_route
    runtime = _round10_producer_runtime(monkeypatch)
    run_id = "producer-run" if owner == "producer" else "issued-run"
    with monkeypatch.context() as writes:
        writes.setattr(
            store,
            "append_audit_event",
            partial(_round15_corrupt_append, store.append_audit_event, mode),
        )
        with pytest.raises(credential_issuer.IssuerRefused) as caught:
            _round15_credential_owner(owner, tmp_path, store)
        # 54d330b6 yields/launches, or raises an untyped OSError for the driver.
        assert type(caught.value).__name__ == "IssuanceEvidenceRefused"
    assert "issuance evidence unavailable" in str(caught.value)
    assert "synthetic private storage diagnostic" not in caplog.text
    assert runtime.call_count == 0
    # Re-audit F9: the refused key is revoked in-process, not left to a scan.
    revoke.assert_called_once_with("a" * 64, issued_key="<run>")
    mint.assert_called_once()
    state = json.loads(store.get_meta("run_key_intent:" + run_id))
    assert state["pending"] is True
    if run_id == "issued-run":
        assert store.get(run_id).status == "refused"
    lookup = Mock(return_value=["a" * 64])
    monkeypatch.setattr(credential_issuer, "lookup_run_key_hashes", lookup)
    reconcile_run_keys(
        store,
        now=datetime.fromisoformat(state["expires_at"]) + timedelta(seconds=1),
    )
    lookup.assert_called_once_with(f"kew-run:{store.read_ledger_id()}:{run_id}")
    # The in-process revocation already deleted the key; no second DELETE.
    assert [call.args[0] for call in revoke.call_args_list].count("a" * 64) == 1
    state = json.loads(store.get_meta("run_key_intent:" + run_id))
    assert state["pending"] is False
    assert state["outcome"] == "confirmed deletion"
    assert _round11_revocation(store, run_id)["revoked"] is True


class _Round15ReadbackObserver:
    def __init__(self, store):
        self.append = store.append_audit_event
        self.read = store.list_audit_events
        self.issuance_id = None
        self.verified = False

    def append_event(self, **kwargs):
        event_id = self.append(**kwargs)
        payload = kwargs.get("payload") or {}
        if payload.get("phase") == "issuance" and payload.get("credential_issuer"):
            self.issuance_id = event_id
        return event_id

    def read_events(self, **kwargs):
        events = self.read(**kwargs)
        if self.issuance_id is not None and kwargs.get("run_id"):
            # A latch, not a snapshot: later per-run reads (the revocation's
            # previous-resolution lookup, the #1951 claim-recovery probe) must
            # not unset the readback this test is about.
            self.verified = self.verified or any(
                event.id == self.issuance_id for event in events
            )
        return events


def _round15_assert_verified(observer, *args, **kwargs):
    assert observer.verified, "no issuance readback preceded intent settlement"


@pytest.mark.parametrize("owner", ["review", "producer", "driver", "context"])
def test_round15_strict_readback_precedes_intent_transfer(
    issued_route, tmp_path, monkeypatch, owner
):
    from kew import run_credentials
    from kew.graph.driver import credentials

    store, _, _ = issued_route
    _round10_producer_runtime(monkeypatch)
    observer = _Round15ReadbackObserver(store)
    monkeypatch.setattr(store, "append_audit_event", observer.append_event)
    monkeypatch.setattr(store, "list_audit_events", observer.read_events)
    target = credentials if owner == "driver" else run_credentials
    monkeypatch.setattr(
        target, "settle_issued_intent", partial(_round15_assert_verified, observer)
    )
    # 54d330b6 first attempts transfer without reading back its appended event.
    _round15_credential_owner(owner, tmp_path, store)
    assert observer.verified


def test_round15_legacy_grace_removed():
    from pathlib import Path

    from kew import credential_prelaunch

    source = Path(credential_prelaunch.__file__).read_text()
    assert "LEGACY_PRELAUNCH_GRACE" not in source  # Present on 54d330b6.
    assert "legacy =" not in source
    root = Path(__file__).resolve().parents[2]
    for path in ("docs/runners/openrouter.md", "docs/security.md"):
        doc = (root / path).read_text()
        assert "24-hour marker-less grace" not in doc
        assert "strict append-and-readback" in doc
        assert "negative lookup strictly after the requested expiry" in doc


def test_round15_review_launcher_preserves_evidence_refusal(
    issued_route, tmp_path, monkeypatch, caplog
):
    from kew import review_environment
    from kew.graph.driver import review_launcher
    from kew.review_dispatch import ReviewerLaunchContext

    store, _, revoke = issued_route
    runtime = _round10_producer_runtime(monkeypatch)
    monkeypatch.setattr(
        review_launcher, "detect_runtime_cached", Mock(return_value="docker")
    )
    monkeypatch.setattr(
        review_environment, "provision_review_environment", Mock(return_value=True)
    )
    monkeypatch.setattr(
        store,
        "append_audit_event",
        partial(_round15_corrupt_append, store.append_audit_event, "append_error"),
    )
    config = DispatchConfig.model_validate(
        {
            "agent": {"model": "openrouter/test"},
            "review": {"runner": "claude-code", "model": "openrouter/test"},
            "sandbox": {"images": {"claude-code": "round15-image"}},
            "logging": {"dir": str(tmp_path / "logs")},
        }
    )
    ctx = ReviewerLaunchContext(
        item_id="1944",
        run_id="round15-review-launcher",
        prompt="review",
        prompt_artifact_sha="sha256:prompt",
        head_sha="a" * 40,
        worktree=tmp_path,
        log_dir=tmp_path / "logs",
    )
    collected = review_launcher.sandbox_reviewer_launcher(config, store)(ctx)
    assert runtime.call_count == 0  # 54d330b6 launches despite the failed append.
    assert store.get(ctx.run_id).status == "refused"
    assert store.get(ctx.run_id).error_category == "credential_issuer_refused"
    assert collected.error_category == "credential_issuer_refused"
    assert "issuance evidence unavailable" in caplog.text
    assert json.loads(store.get_meta("run_key_intent:" + ctx.run_id))["pending"] is True
    # Re-audit F9: the refused key is revoked in-process, not left to a scan.
    revoke.assert_called_once_with("a" * 64, issued_key="<run>")


def _round16_child(tmp_path, monkeypatch, tool):
    import sys

    from kew import subprocess_utils

    observed = tmp_path / "round16-environment.json"
    script = tmp_path / tool
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, os\n"
        f'open({str(observed)!r}, "w").write(json.dumps(dict(os.environ)))\n'
        'print("fixture-output")\n'
    )
    script.chmod(0o700)
    monkeypatch.setitem(subprocess_utils.TOOL_PATHS, tool, str(script))
    monkeypatch.setenv("OPENROUTER_API_KEY", "<host>")
    monkeypatch.setenv("OPENROUTER_PROVISIONING_KEY", "<manage>")
    monkeypatch.delenv("KEW_ISSUED_RUN_KEY_HASH", raising=False)
    return observed


@pytest.mark.serial
def test_round16_real_child_cli_preflight(tmp_path, monkeypatch):
    from kew import cli

    observed = _round16_child(tmp_path, monkeypatch, "gh")
    for name in (
        "preflight_tools",
        "_log_project_banner",
        "_emit_stray_ledger_warnings",
        "_detect_repo",
        "bind_policy_context",
        "_emit_orchestrator_skew_warnings",
        "_emit_governance_drift_warnings",
    ):
        monkeypatch.setattr(cli, name, Mock())
    config = DispatchConfig.model_validate(
        {
            "project": {"repo": "fixture/repo"},
            "logging": {"dir": str(tmp_path / "logs")},
            "branches": {"worktree_base": str(tmp_path / "worktrees")},
        }
    )
    cli._preflight(config)
    environment = json.loads(observed.read_text())
    assert "OPENROUTER_API_KEY" not in environment
    assert "OPENROUTER_PROVISIONING_KEY" not in environment


@pytest.mark.serial
def test_round16_real_child_detached_capture(tmp_path, monkeypatch):
    from kew.publication import capture_detached_publication_token

    observed = _round16_child(tmp_path, monkeypatch, "gh")
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "KEW_DETACHED_PUBLICATION_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    assert capture_detached_publication_token() == "fixture-output"
    environment = json.loads(observed.read_text())
    assert "OPENROUTER_API_KEY" not in environment
    assert "OPENROUTER_PROVISIONING_KEY" not in environment


@pytest.mark.serial
def test_round16_uncontained_boundary_filters_ambient(tmp_path, monkeypatch):
    from kew import orchestrator, subprocess_utils
    from kew.runners import get_runner

    observed = _round16_child(tmp_path, monkeypatch, "opencode")
    monkeypatch.setattr(orchestrator, "find_timeout_cmd", Mock(return_value=None))
    result = orchestrator.run_agent(
        runner=get_runner("opencode"),
        prompt="fixture",
        worktree_dir=tmp_path,
        config=DispatchConfig(),
        issue_num=1944,
        model="openrouter/test",
        log_file_path=tmp_path / "round16.log",
    )
    assert result.exit_code == 0
    environment = json.loads(observed.read_text())
    assert "OPENROUTER_API_KEY" not in environment
    assert "OPENROUTER_PROVISIONING_KEY" not in environment
    # Generic launches of the same executable also receive no authority.
    with subprocess_utils.popen(["opencode"]) as child:
        child.communicate(timeout=10)
    assert "OPENROUTER_API_KEY" not in json.loads(observed.read_text())


class _Round16Issuer:
    def __init__(self, created, expiry, visible_at):
        self.created, self.expiry, self.visible_at = created, expiry, visible_at
        self.now = created
        self.deleted = False
        self.calls = []

    def __call__(self, method, url, *, headers, body):
        self.calls.append(method)
        if method == "DELETE":
            self.deleted = True
            return 200, b'{"deleted":true}'
        assert method == "GET"
        data = []
        if not self.deleted and self.now >= self.visible_at:
            data = [
                {
                    "name": self.name,
                    "hash": "e" * 64,
                    "limit": 1,
                    "created_at": self.created.isoformat(),
                    "expires_at": self.expiry.isoformat(),
                }
            ]
        return 200, json.dumps({"data": data}).encode()


def _round16_intent(store, created, expiry):
    from kew.credential_intents import record_issuance_intent

    record_issuance_intent(store, "round16-late", 1, expiry, created)
    state = json.loads(store.get_meta("run_key_intent:round16-late"))
    # Exercise an already scheduled retry, independent of first-scan admission.
    state.update(next_attempt_at=created.isoformat())
    store.set_meta("run_key_intent:round16-late", json.dumps(state))


def _round16_wire_issuer(monkeypatch, issuer):
    from kew import credential_issuer

    monkeypatch.setenv("OPENROUTER_PROVISIONING_KEY", "<manage>")
    for name in ("lookup_run_key_hashes", "revoke_run_key"):
        monkeypatch.setattr(
            credential_issuer,
            name,
            partial(getattr(credential_issuer, name), http=issuer),
        )
    # Restore the real list implementation hidden by this module's isolation fixture.
    monkeypatch.setattr(
        credential_issuer, "list_run_keys", partial(_ROUND16_LIST_KEYS, http=issuer)
    )


# Capture before the per-test isolation fixture runs. Base has no sweep API.
_ROUND16_LIST_KEYS = getattr(
    _round16_issuer_module, "list_run_keys", Mock(return_value=[])
)


def test_round16_delayed_visibility_after_former_settlement(tmp_path, monkeypatch):
    from datetime import timedelta

    from kew.credential_reconciliation import reconcile_run_keys

    created = datetime(2026, 9, 20, tzinfo=UTC)
    expiry = created + timedelta(hours=1)
    issuer = _Round16Issuer(created, expiry, created + timedelta(seconds=21))
    _round16_wire_issuer(monkeypatch, issuer)
    with SqliteRunStore(str(tmp_path / "late.db"), project_root=tmp_path) as store:
        _round16_intent(store, created, expiry)
        issuer.name = f"kew-run:{store.read_ledger_id()}:round16-late"
        issuer.now = created + timedelta(seconds=21)
        issuer.visible_at = created + timedelta(seconds=22)
        reconcile_run_keys(store, now=issuer.now)
        state = json.loads(store.get_meta("run_key_intent:round16-late"))
        assert state["pending"] is True
        issuer.now = datetime.fromisoformat(state["next_attempt_at"])
        reconcile_run_keys(store, now=issuer.now)
        assert issuer.deleted
        assert _round11_revocation(store, "round16-late")["revoked"] is True
        assert (
            json.loads(store.get_meta("run_key_intent:round16-late"))["pending"]
            is False
        )


def test_round16_after_expiry_visibility_is_swept(tmp_path, monkeypatch):
    from datetime import timedelta

    from kew.credential_reconciliation import reconcile_run_keys

    created = datetime(2026, 9, 20, tzinfo=UTC)
    expiry = created + timedelta(seconds=600)
    issuer = _Round16Issuer(created, expiry, expiry + timedelta(seconds=2))
    _round16_wire_issuer(monkeypatch, issuer)
    with SqliteRunStore(str(tmp_path / "expired.db"), project_root=tmp_path) as store:
        _round16_intent(store, created, expiry)
        issuer.name = f"kew-run:{store.read_ledger_id()}:round16-late"
        issuer.now = expiry + timedelta(seconds=1)
        reconcile_run_keys(store, now=issuer.now)
        assert (
            json.loads(store.get_meta("run_key_intent:round16-late"))["pending"]
            is False
        )
        assert not issuer.deleted
        issuer.now = expiry + timedelta(seconds=2)
        reconcile_run_keys(store, now=issuer.now)
        assert issuer.deleted
        assert _round11_revocation(store, "round16-late")["revoked"] is True


def test_round16_negative_lookup_stays_pending_before_expiry(tmp_path, monkeypatch):
    from datetime import timedelta

    from kew import credential_issuer
    from kew.credential_reconciliation import reconcile_run_keys

    created = datetime(2026, 9, 20, tzinfo=UTC)
    expiry = created + timedelta(hours=1)
    lookup = Mock(return_value=[])
    monkeypatch.setattr(credential_issuer, "lookup_run_key_hashes", lookup)
    with SqliteRunStore(str(tmp_path / "pending.db"), project_root=tmp_path) as store:
        _round16_intent(store, created, expiry)
        now = created + timedelta(seconds=21)
        for delay in (30, 60, 120, 240, 300, 300):
            reconcile_run_keys(store, now=now)
            state = json.loads(store.get_meta("run_key_intent:round16-late"))
            assert state["pending"] is True
            assert datetime.fromisoformat(state["next_attempt_at"]) == now + timedelta(
                seconds=delay
            )
            now += timedelta(seconds=delay)
        assert lookup.call_count == 6
        reconcile_run_keys(store, now=expiry + timedelta(seconds=1))
        assert (
            json.loads(store.get_meta("run_key_intent:round16-late"))["pending"]
            is False
        )


def test_round16_fixture_values_fail_generic_api_key_grep():
    import ast
    import subprocess
    from pathlib import Path

    tree = ast.parse(Path(__file__).read_text())
    values = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        index = {"setenv": 1, "BoundedRunKey": 0}.get(node.func.attr)
        if index is not None and len(node.args) > index:
            value = node.args[index]
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                values.append(value.value)
    # The exact capture alternatives from gitleaks v8.30.0 generic-api-key.
    pattern = r"(?i)^(?:[\w.=-]{10,150}|[a-z0-9][a-z0-9+/]{11,}={0,3})$"
    ripgrep = shutil.which("rg")
    if ripgrep is None:
        pytest.skip("ripgrep is required to evaluate the gitleaks alternatives")
    result = subprocess.run(
        [ripgrep, "--pcre2", pattern],
        input="\n".join(values),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1, result.stdout


@pytest.mark.parametrize(
    "protection", ["intent", "marker", "issuance", "grace", "foreign"]
)
def test_round16_sweep_preserves_owned_and_young_entries(
    tmp_path, monkeypatch, protection
):
    from datetime import timedelta

    from kew import credential_issuer, run_credentials
    from kew.credential_prelaunch import record_prelaunch
    from kew.credential_reconciliation import reconcile_run_keys

    created = datetime(2026, 9, 20, tzinfo=UTC)
    expiry = created + timedelta(hours=1)
    issuer = _Round16Issuer(created, expiry, created)
    _round16_wire_issuer(monkeypatch, issuer)
    issuer.now = created + timedelta(seconds=301)
    target = {
        "hash": "f" * 64,
        "limit": 1,
        "created_at": created.isoformat(),
        "expires_at": expiry.isoformat(),
    }
    if protection == "grace":
        target["created_at"] = (created + timedelta(seconds=1)).isoformat()
    with SqliteRunStore(str(tmp_path / "sweep.db"), project_root=tmp_path) as store:
        target["name"] = (
            "another-application"
            if protection == "foreign"
            else f"kew-run:{store.read_ledger_id()}:protected"
        )
        key = credential_issuer.BoundedRunKey(
            "<run>", "f" * 64, 1, expiry.isoformat(), name=target["name"]
        )
        if protection == "intent":
            from kew.credential_intents import record_issuance_intent

            record_issuance_intent(store, "protected", 1, expiry, created)
        elif protection == "marker":
            record_prelaunch(store, "protected", key, created)
        elif protection == "issuance":
            run_credentials._record(store, "protected", key, phase="issuance")
        items = [
            {
                "name": f"kew-run:{store.read_ledger_id()}:round16-late",
                "hash": "e" * 64,
                "limit": 1,
                "created_at": created.isoformat(),
                "expires_at": expiry.isoformat(),
            },
            target,
        ]
        monkeypatch.setattr(
            credential_issuer, "list_run_keys", Mock(return_value=items)
        )
        reconcile_run_keys(store, now=issuer.now)
        assert issuer.deleted  # The unowned control proves the sweep ran.
        events = store.list_audit_events(
            event="sandbox_resolved", run_id="protected", limit=None
        )
        assert not any(
            (event.payload or {}).get("phase") == "revocation" for event in events
        )
        assert _round11_revocation(store, "round16-late")["revoked"] is True


@pytest.mark.parametrize("name", ["round14-crash", "round15-absent", "round16-late"])
def test_round16_metadata_allowlist_preserves_detection(tmp_path, name):
    import shutil
    import subprocess
    from pathlib import Path

    executable = shutil.which("gitleaks")
    if executable is None:
        pytest.skip("gitleaks is not installed")
    target = tmp_path / "kew/tests/test_bounded_run_keys.py"
    target.parent.mkdir(parents=True)
    synthetic = "ghp_" + "A1b2C3d4E5f6" * 3
    # Keep the positive control on the same line as the metadata fixture.
    target.write_text(f'row = "run_key_intent:{name}"; github_token = "{synthetic}"\n')
    report = tmp_path / "findings.json"
    result = subprocess.run(
        [
            executable,
            "dir",
            ".",
            "--config",
            str(Path(__file__).resolve().parents[2] / ".gitleaks.toml"),
            "--no-banner",
            "--redact",
            "--report-format",
            "json",
            "--report-path",
            str(report),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    findings = json.loads(report.read_text())
    assert [finding["RuleID"] for finding in findings] == ["github-pat"]


def _round17_listed(name, digest, now):
    from datetime import timedelta

    return {
        "name": name,
        "hash": digest * 64,
        "limit": 1,
        "created_at": (now - timedelta(seconds=301)).isoformat(),
        "expires_at": (now + timedelta(hours=1)).isoformat(),
    }


def test_round17_two_ledgers_preserve_foreign_live_credential(
    tmp_path, monkeypatch, caplog
):
    from kew import credential_issuer, credential_orphans, run_credentials

    now = datetime.now(UTC)
    with (
        SqliteRunStore(str(tmp_path / "a.db"), project_root=tmp_path / "a") as a,
        SqliteRunStore(str(tmp_path / "b.db"), project_root=tmp_path / "b") as b,
    ):
        foreign = _round17_listed(f"kew-run:{a.read_ledger_id()}:live", "a", now)
        own = _round17_listed(f"kew-run:{b.read_ledger_id()}:orphan", "b", now)
        a.save(
            RunRecord(
                run_id="live",
                issue_num=1944,
                branch="agent/1944",
                status="pending",
                exit_code=-1,
                started_at=now,
            )
        )
        run_credentials._record(
            a,
            "live",
            credential_issuer.BoundedRunKey(
                "<run>", foreign["hash"], 1, foreign["expires_at"]
            ),
            phase="issuance",
        )
        monkeypatch.setattr(
            credential_issuer, "list_run_keys", Mock(return_value=[foreign, own])
        )
        revoke = Mock()
        ownership = Mock(wraps=credential_orphans._locally_owned)
        monkeypatch.setattr(run_credentials, "_revoke", revoke)
        monkeypatch.setattr(credential_orphans, "_locally_owned", ownership)
        caplog.set_level("DEBUG", logger="kew.credential_orphans")
        seen = set()
        credential_orphans.sweep_issuer_orphans(b, now=now, seen=seen)
        assert not any(
            call.args[2].key_hash == foreign["hash"] for call in revoke.call_args_list
        )
        revoke.assert_called_once()
        assert revoke.call_args.args[1:][0] == "orphan"
        assert revoke.call_args.args[2].key_hash == own["hash"]
        ownership.assert_called_once_with(b, "orphan", own["hash"], own["name"])
        assert seen == {own["hash"]}
        messages = [
            r.message for r in caplog.records if r.name == "kew.credential_orphans"
        ]
        assert messages == ["orphan scan: skipped outside ledger namespace=1"]
        assert a.get("live").status == "pending"


def test_round17_unqualified_name_is_never_revoked(tmp_path, monkeypatch, caplog):
    from kew import credential_issuer, credential_orphans, run_credentials

    now = datetime.now(UTC)
    item = _round17_listed("kew-run:unqualified", "c", now)
    with SqliteRunStore(str(tmp_path / "bound.db"), project_root=tmp_path) as store:
        monkeypatch.setattr(
            credential_issuer, "list_run_keys", Mock(return_value=[item])
        )
        revoke, ownership = Mock(), Mock(return_value=False)
        monkeypatch.setattr(run_credentials, "_revoke", revoke)
        monkeypatch.setattr(credential_orphans, "_locally_owned", ownership)
        caplog.set_level("DEBUG", logger="kew.credential_orphans")
        credential_orphans.sweep_issuer_orphans(store, now=now, seen=set())
        revoke.assert_not_called()
        ownership.assert_not_called()
        assert "orphan scan: skipped outside ledger namespace=1" in caplog.text
        assert item["name"] not in caplog.text


def test_round17_unbound_store_refuses_issuance(tmp_path, monkeypatch):
    from kew import credential_issuer, run_credentials

    with SqliteRunStore(str(tmp_path / "unbound.db")) as store:
        assert store.read_ledger_id() is None
        mint = Mock(
            return_value=credential_issuer.BoundedRunKey(
                "<run>", "a" * 64, 1, "2099-01-01T00:00:00Z"
            )
        )
        monkeypatch.setattr(credential_issuer, "mint_bounded_run_key", mint)
        with pytest.raises(credential_issuer.IssuerRefused, match="^issuer refused$"):
            run_credentials.mint_run_credential(
                run_id="unbound",
                config=DispatchConfig(),
                store=store,
                timeout_seconds=60,
                limit_usd=1,
            )
        mint.assert_not_called()
        assert store.list_meta("run_key_intent:") == []


@pytest.mark.parametrize(
    "name",
    [
        "kew-run:unqualified",
        "kew-run:not-a-uuid:run",
        "kew-run::run",
        "kew-run:11111111111141118111111111111111:run",
        "kew-run:AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA:run",
        "kew-run:11111111-1111-4111-8111-111111111111:",
        "kew-run:11111111-1111-4111-8111-111111111111:run:extra",
        "other:11111111-1111-4111-8111-111111111111:run",
    ],
)
def test_round17_parser_rejects_malformed_segment(name):
    from kew import credential_issuer

    parser = getattr(credential_issuer, "parse_run_key_name", None)
    assert callable(parser), "listed names require a ledger namespace parser"
    assert parser(name) is None
    ledger_id = "11111111-1111-4111-8111-111111111111"
    assert parser(f"kew-run:{ledger_id}:run") == (ledger_id, "run")


def test_round17_issuance_persists_qualified_name(tmp_path, monkeypatch):
    from kew import credential_issuer, run_credentials

    request = Mock(side_effect=_round17_reply)
    monkeypatch.setattr(credential_issuer, "_request", request)
    with SqliteRunStore(str(tmp_path / "bound.db"), project_root=tmp_path) as store:
        run_credentials.mint_run_credential(
            run_id="qualified",
            config=DispatchConfig(),
            store=store,
            timeout_seconds=60,
            limit_usd=1,
        )
        expected = f"kew-run:{store.read_ledger_id()}:qualified"
        intent = json.loads(store.get_meta("run_key_intent:qualified"))
        assert intent["name"] == expected
        assert request.call_args.args[2]["name"] == expected


def _round17_reply(method, url, body, http):
    assert method == "POST"
    return json.dumps({"key": "<run>", "data": {**body, "hash": "d" * 64}}).encode()


@pytest.mark.parametrize("name_kind", ["foreign", "unqualified"])
def test_round17_intent_recovery_rejects_other_names(tmp_path, monkeypatch, name_kind):
    from datetime import timedelta

    from kew import credential_issuer
    from kew.credential_reconciliation import reconcile_run_keys

    now = datetime.now(UTC)
    with SqliteRunStore(str(tmp_path / "bound.db"), project_root=tmp_path) as store:
        name = (
            "kew-run:11111111-1111-4111-8111-111111111111:run"
            if name_kind == "foreign"
            else "kew-run:run"
        )
        store.set_meta(
            "run_key_intent:run",
            json.dumps(
                {
                    "run_id": "run",
                    "name": name,
                    "pending": True,
                    "limit_usd": 1,
                    "expires_at": (now - timedelta(seconds=1)).isoformat(),
                }
            ),
        )
        lookup = Mock(return_value=[])
        monkeypatch.setattr(credential_issuer, "lookup_run_key_hashes", lookup)
        reconcile_run_keys(store, now=now)
        lookup.assert_not_called()
        assert json.loads(store.get_meta("run_key_intent:run"))["pending"] is True


def test_round17_docs_binding():
    import test_docs_truthfulness

    test_docs_truthfulness.test_round14_docs_bind_prepost_intent_and_child_filtering()


class _Round18Issuer:
    def __init__(self, name_kind, root=None):
        self.name_kind, self.root = name_kind, root
        self.item = (
            None if root is None else json.loads((root / "issued.json").read_text())
        )
        self.calls = []
        self.delete_status = 200

    def __call__(self, method, url, *, headers, body):
        self.calls.append((method, url))
        if method == "POST":
            request = json.loads(body)
            data = {**request, "hash": "e" * 64}
            data["name"] = _round18_reply_name(self.name_kind, request["name"])
            self.item = {**data, "created_at": datetime.now(UTC).isoformat()}
            if self.name_kind == "missing":
                data.pop("name")
                self.item["name"] = "another-application"
            return 200, json.dumps({"key": "<run>", "data": data}).encode()
        if method == "DELETE":
            assert url.endswith("/" + "e" * 64)
            if self.delete_status == 200:
                self.item = None
            return self.delete_status, b'{"deleted":true}'
        assert method == "GET"
        return 200, json.dumps(
            {"data": [] if self.item is None else [self.item]}
        ).encode()


def _round18_reply_name(kind, expected):
    names = {
        "missing": None,
        "unqualified": "kew-run:" + expected.rsplit(":", 1)[1],
        "foreign": "kew-run:11111111-1111-4111-8111-111111111111:run",
        "different-run": expected + "-other",
        "exact": expected,
    }
    return names[kind]


def _round18_wire_issuer(monkeypatch, issuer):
    from kew import credential_issuer

    _round16_wire_issuer(monkeypatch, issuer)
    monkeypatch.setattr(
        credential_issuer,
        "mint_bounded_run_key",
        partial(credential_issuer.mint_bounded_run_key, http=issuer),
    )


def _round18_mint(store):
    from kew import run_credentials

    return run_credentials.mint_run_credential(
        run_id="run",
        config=DispatchConfig(),
        store=store,
        timeout_seconds=60,
        limit_usd=1,
    )


@pytest.mark.parametrize(
    "name_kind", ["missing", "unqualified", "foreign", "different-run"]
)
def test_round18_reply_requires_exact_name(tmp_path, monkeypatch, name_kind):
    from kew import credential_issuer
    from kew.credential_reconciliation import revocation_state

    issuer = _Round18Issuer(name_kind)
    _round18_wire_issuer(monkeypatch, issuer)
    with SqliteRunStore(str(tmp_path / "reply.db"), project_root=tmp_path) as store:
        with pytest.raises(credential_issuer.MalformedIssuerReply):
            _round18_mint(store)  # da8fdbc3 returns an unconfirmed credential.
        assert issuer.item is None
        assert [method for method, _ in issuer.calls] == ["POST", "DELETE"]
        assert _round11_revocation(store, "run")["revoked"] is True
        assert revocation_state(store, "run", "e" * 64)["pending"] is False
        assert store.get_meta("run_key_prelaunch:run:" + "e" * 64) is None


def test_round18_exact_reply_is_accepted(tmp_path, monkeypatch):
    from kew import run_credentials

    issuer = _Round18Issuer("exact")
    _round18_wire_issuer(monkeypatch, issuer)
    with SqliteRunStore(str(tmp_path / "reply.db"), project_root=tmp_path) as store:
        issued = _round18_mint(store)
        intent = json.loads(store.get_meta("run_key_intent:run"))
        assert issuer.item["name"] == intent["name"]
        assert issued.key_hash == issuer.item["hash"]  # Acceptance control.
        assert [method for method, _ in issuer.calls] == ["POST"]
        run_credentials._revoke(store, "run", issued)
        assert issuer.item is None


def test_round18_unconfirmed_cleanup_retries_by_hash(tmp_path, monkeypatch):
    from datetime import timedelta

    from kew import credential_issuer
    from kew.credential_reconciliation import reconcile_run_keys, revocation_state

    issuer = _Round18Issuer("foreign")
    issuer.delete_status = 503
    _round18_wire_issuer(monkeypatch, issuer)
    with SqliteRunStore(str(tmp_path / "reply.db"), project_root=tmp_path) as store:
        with pytest.raises(credential_issuer.MalformedIssuerReply):
            _round18_mint(store)
        state = revocation_state(store, "run", "e" * 64)
        assert state["pending"] is True
        assert _round11_revocation(store, "run")["revoked"] is False
        issuer.delete_status = 200
        reconcile_run_keys(
            store,
            now=datetime.fromisoformat(state["next_attempt_at"]) + timedelta(seconds=1),
        )
        assert issuer.item is None
        assert _round11_revocation(store, "run")["revoked"] is True


_ROUND18_CRASH_CHILD = r"""
import json, os, signal, sys
from pathlib import Path
from kew import credential_issuer, run_credentials
from kew.config import DispatchConfig
from kew.run_store import SqliteRunStore

root = Path(sys.argv[1])
os.environ['OPENROUTER_PROVISIONING_KEY'] = '<child>'

def pause(*args, **kwargs):
    (root / 'ready').write_text('ready')
    signal.pause()

def request(method, url, body, http):
    if method == 'DELETE':
        pause()
    assert method == 'POST'
    data = {**body, 'hash': 'e' * 64,
        'name': 'kew-run:11111111-1111-4111-8111-111111111111:run',
        'created_at': '2026-01-01T00:00:00Z'}
    (root / 'issued.json').write_text(json.dumps(data))
    return json.dumps({'key': '<run>', 'data': data}).encode()

credential_issuer._request = request
run_credentials.record_prelaunch = pause
with SqliteRunStore(str(root / 'crash.db'), project_root=root) as store:
    run_credentials.mint_run_credential(
        run_id='run', config=DispatchConfig(), store=store,
        timeout_seconds=60, limit_usd=1,
    )
"""


@pytest.mark.serial
def test_round18_crash_before_prelaunch_recovers_mismatched_hash(tmp_path, monkeypatch):
    from datetime import timedelta

    from kew.credential_reconciliation import reconcile_run_keys, revocation_state

    _round14_kill_minter(tmp_path, "after-response", _ROUND18_CRASH_CHILD)
    issuer = _Round18Issuer("foreign", root=tmp_path)
    _round18_wire_issuer(monkeypatch, issuer)
    with SqliteRunStore(str(tmp_path / "crash.db"), project_root=tmp_path) as store:
        assert issuer.item is not None
        assert store.get_meta("run_key_prelaunch:run:" + "e" * 64) is None
        reconcile_run_keys(store, now=datetime.now(UTC) + timedelta(seconds=31))
        assert (
            issuer.item is None
        )  # da8fdbc3 leaves the foreign-name credential active.
        assert _round11_revocation(store, "run")["revoked"] is True
        assert revocation_state(store, "run", "e" * 64)["pending"] is False
        assert [method for method, _ in issuer.calls].count("DELETE") == 1


@pytest.mark.parametrize("owner", ["intent", "pending", "marker", "issuance"])
def test_round18_orphan_ownership_requires_persisted_name(tmp_path, monkeypatch, owner):
    from datetime import timedelta

    from kew import credential_orphans, run_credentials

    now = datetime.now(UTC)
    issuer = _Round18Issuer("exact")
    _round18_wire_issuer(monkeypatch, issuer)
    with SqliteRunStore(str(tmp_path / "ownership.db"), project_root=tmp_path) as store:
        issued = _round18_mint(store)
        name = issuer.item["name"]
        issuer.item["created_at"] = (now - timedelta(seconds=301)).isoformat()
        # Keep the real hash, but persist a different name at each ownership site.
        store.set_meta("run_key_prelaunch:run:" + issued.key_hash, "{}")
        intent = json.loads(store.get_meta("run_key_intent:run"))
        intent.update(name=name + "-other", pending=owner in {"intent", "pending"})
        store.set_meta("run_key_intent:run", json.dumps(intent))
        if owner == "pending":
            store.save(
                RunRecord(
                    run_id="run",
                    issue_num=1944,
                    branch="agent/1944",
                    status="pending",
                    exit_code=-1,
                    started_at=now,
                )
            )
        elif owner == "marker":
            store.set_meta(
                "run_key_prelaunch:run:" + issued.key_hash,
                json.dumps({"key_hash": issued.key_hash, "name": name + "-other"}),
            )
        elif owner == "issuance":
            evidence = Mock(
                secret="<run>",
                key_hash=issued.key_hash,
                identity={**issued.identity, "name": name + "-other"},
            )
            run_credentials._record(store, "run", evidence, phase="issuance")
        credential_orphans.sweep_issuer_orphans(store, now=now, seen=set())
        assert issuer.item is None  # da8fdbc3 protects mismatched ownership records.
        assert _round11_revocation(store, "run")["revoked"] is True


@pytest.mark.parametrize("name_kind", ["unqualified", "foreign", "different-run"])
def test_round18_reconciliation_lookup_rejects_other_names(
    tmp_path, monkeypatch, name_kind
):
    from datetime import timedelta

    from kew.credential_intents import (
        reconcile_issuance_intents,
        record_issuance_intent,
    )

    now = datetime.now(UTC)
    issuer = _Round18Issuer(name_kind)
    _round18_wire_issuer(monkeypatch, issuer)
    with SqliteRunStore(str(tmp_path / "lookup.db"), project_root=tmp_path) as store:
        record_issuance_intent(store, "run", 1, now - timedelta(seconds=1), now)
        intent = json.loads(store.get_meta("run_key_intent:run"))
        issuer.item = {
            "hash": "e" * 64,
            "name": _round18_reply_name(name_kind, intent["name"]),
        }
        reconcile_issuance_intents(store, now=now, seen=set(), limit=100)
        assert issuer.item is not None  # Existing exact-name lookup control.
        assert [method for method, _ in issuer.calls] == ["GET"]


# ── Proxy policy on the bounded-key launch paths (#1933 merged with #1957) ──

_PROXY_1933_SECRET = "http://user:proxy-pw-1933@proxy.invalid:3128"


def _proxy_1933_host(monkeypatch):
    from kew.sandbox import CONTROLLED_PROXY_NAMES

    for name in CONTROLLED_PROXY_NAMES:
        monkeypatch.setenv(name, _PROXY_1933_SECRET)


def _proxy_1933_expected(mode):
    from kew.sandbox import CONTAINER_PROXY_NAMES, CONTROLLED_PROXY_NAMES

    return [
        name if mode == "inherit" and name in CONTAINER_PROXY_NAMES else f"{name}="
        for name in CONTROLLED_PROXY_NAMES
    ]


def _proxy_1933_args(argv):
    from kew.sandbox import CONTROLLED_PROXY_NAMES

    return [
        argv[i + 1]
        for i, arg in enumerate(argv[:-1])
        if arg == "-e" and argv[i + 1].partition("=")[0] in CONTROLLED_PROXY_NAMES
    ]


def _proxy_1933_assert_payloads(store, mode):
    from kew.sandbox import CONTAINER_PROXY_NAMES, CONTROLLED_PROXY_NAMES

    events = store.list_audit_events(event="sandbox_resolved", limit=100)
    assert events, "the posture and key lifecycle were not attested"
    lifecycle = [e.payload for e in events if "phase" in e.payload]
    assert {p["phase"] for p in lifecycle} >= {"issuance", "revocation"}
    assert all("proxy_variables" in p for p in lifecycle), (
        "a key lifecycle event lost the pre-admission proxy attestation"
    )
    postured = [e.payload for e in events if "proxy_variables" in e.payload]
    for payload in postured:
        assert payload["proxy_environment"] == mode
        actions = payload["proxy_variables"]
        named = set(actions["inherited"]) | set(actions["blanked"])
        assert named == set(CONTROLLED_PROXY_NAMES)
        assert "OPENROUTER_API_KEY" not in named
        names = payload["credential_names"]
        assert "OPENROUTER_API_KEY" in names
        inherited = set(CONTAINER_PROXY_NAMES) if mode == "inherit" else set()
        assert set(names) - {"OPENROUTER_API_KEY"} == inherited
    dump = "\n".join(store._conn.iterdump())
    for secret in ("<run>", "<manage>", "<host>", "proxy-pw-1933"):
        assert secret not in dump


@pytest.mark.parametrize("mode", ["none", "inherit"])
def test_1933_bounded_key_launch_controls_proxies_whatever_env_holds(
    issued_route, tmp_path, monkeypatch, caplog, mode
):
    """The issued-key launch keeps all ten proxy names explicit in argv.

    The runtime client's environment carries the minted key and every host
    proxy value. Under ``none`` the explicit ``NAME=`` blanks must still be in
    argv, so the environment cannot smuggle a proxy into the container.
    """
    from kew import credential_issuer, dispatch_core, sandbox

    store, mint, revoke = issued_route
    _proxy_1933_host(monkeypatch)
    config = DispatchConfig()
    config.sandbox.proxy_environment = mode
    # Posture first, as the tracked path does: the key lifecycle events carry it.
    sandbox.attest_flow_posture(
        store,
        flow="adversarial_review",
        runner="opencode",
        config=config.sandbox,
        posture=sandbox.POSTURE_SANDBOXED,
        image="sha256:" + "b" * 64,
        run_id="issued-run",
    )
    launches = []

    def launch(
        argv, *, admission, on_spawn, prepare, env, credential, separate_stderr=False
    ):
        assert admission is not None
        assert callable(on_spawn)
        assert callable(prepare)
        assert isinstance(credential, credential_issuer.BoundedRunKey)
        launches.append((list(argv), dict(env), credential))
        return Mock()

    monkeypatch.setattr(dispatch_core, "_launch_container", launch)
    monkeypatch.setattr(dispatch_core, "_stream_sandbox_log", Mock(return_value=0))
    monkeypatch.setattr("kew.dispatch_sandbox.finalize_container_hard_stop", Mock())
    from kew.dispatch_sandbox import run_container_and_stream

    assert (
        run_container_and_stream(
            config,
            store,
            image="sha256:" + "b" * 64,
            runner="opencode",
            checkout_dir=tmp_path,
            slug="1944",
            run_id="issued-run",
            model="openrouter/test",
            log_file=tmp_path / "run.log",
            run_kind="adversarial_review",
            result=DispatchResult(
                exit_code=-1, issue_num=1944, log_file=tmp_path / "run.log"
            ),
        )
        == 0
    )
    [(argv, env, credential)] = launches
    assert credential.secret == "<run>"
    # The environment really does hold proxy values and the key...
    assert env["HTTP_PROXY"] == _PROXY_1933_SECRET
    assert env["OPENROUTER_API_KEY"] == "<run>"
    # ...and argv still names every proxy variable explicitly.
    assert _proxy_1933_args(argv) == _proxy_1933_expected(mode)
    assert any(
        argv[i : i + 2] == ["-e", "OPENROUTER_API_KEY"] for i in range(len(argv))
    )
    joined = " ".join(argv)
    for secret in ("<run>", "<manage>", "<host>", "proxy-pw-1933"):
        assert secret not in joined
    revoke.assert_called_once_with("a" * 64, issued_key="<run>")
    _proxy_1933_assert_payloads(store, mode)
    assert "proxy-pw-1933" not in caplog.text
    assert "<run>" not in caplog.text


@pytest.mark.parametrize("mode", ["none", "inherit"])
def test_1933_registered_creation_with_key_controls_proxies(
    tmp_path, monkeypatch, mode
):
    """The tracked path's create (and its CPU-cap retry) keeps the proxy blanks."""
    from kew import sandbox
    from kew.credential_issuer import BoundedRunKey
    from kew.dispatch_sandbox import build_agent_container_argv

    _proxy_1933_host(monkeypatch)
    config = DispatchConfig()
    config.sandbox.proxy_environment = mode
    key = BoundedRunKey("<run>", "a" * 64, 1.0, "2099-01-01T00:00:00Z")
    argv = build_agent_container_argv(
        config,
        image="sha256:" + "b" * 64,
        runner="opencode",
        checkout_dir=tmp_path,
        slug="1944",
        model="openrouter/test",
        inference_transport=None,
    )
    cidfile = sandbox._argv_cidfile(argv)
    assert cidfile is not None
    calls = []
    rejection = (
        "docker: Error response from daemon: Range of CPUs is from 0.01 to 2.00, "
        "as there are only 2 CPUs available"
    )

    def run(command, *, check, timeout, credential):
        assert check is False
        assert timeout == sandbox.RUNTIME_CONTROL_TIMEOUT_SECONDS
        assert credential is key
        calls.append(list(command))
        if len(calls) == 1:
            return subprocess.CompletedProcess(command, 125, "", rejection)
        cidfile.write_text("c" * 64, encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(sandbox.subprocess_utils, "run", run)
    try:
        assert sandbox.create_registered_container(argv, credential=key) == "c" * 64
    finally:
        cidfile.unlink(missing_ok=True)
    assert len(calls) == 2, "the CPU-cap retry did not run"
    for command in calls:
        assert command[1] == "create"
        assert _proxy_1933_args(command) == _proxy_1933_expected(mode)
        joined = " ".join(command)
        assert "<run>" not in joined
        assert "proxy-pw-1933" not in joined
