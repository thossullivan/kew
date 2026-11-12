"""Run issuance policy and environment boundaries, with mocked issuer calls."""

import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from kew.config import DispatchConfig
from kew.run_store import SqliteRunStore


@pytest.mark.parametrize(
    "model,names,direct,issued",
    [
        ("openrouter/model", (), True, True),
        ("model", ("OPENROUTER_API_KEY",), True, True),
        ("claude", ("ANTHROPIC_API_KEY",), True, False),
        ("openrouter/model", ("OPENROUTER_API_KEY",), False, False),
    ],
)
def test_bounded_route_selection(tmp_path, monkeypatch, model, names, direct, issued):
    from kew import credential_issuer
    from kew.run_credentials import bounded_run_credentials

    monkeypatch.setenv("OPENROUTER_PROVISIONING_KEY", "management-secret-1944")
    mint, revoke = Mock(), Mock()
    mint.return_value = credential_issuer.BoundedRunKey(
        "run-secret-1944", "a" * 64, 25, "2099-01-01T00:00:00Z"
    )
    monkeypatch.setattr(credential_issuer, "mint_bounded_run_key", mint)
    monkeypatch.setattr(credential_issuer, "revoke_run_key", revoke)
    with bounded_run_credentials(
        run_id="run",
        config=DispatchConfig(),
        store=SqliteRunStore(str(tmp_path / "runs.db"), project_root=tmp_path),
        model=model,
        credential_names=names,
        timeout_seconds=120,
        direct=direct,
    ) as key:
        assert (key is not None) == issued
    assert mint.call_count == int(issued)
    assert revoke.call_count == int(issued)


def test_bounded_missing_runtime_cap_uses_daily_remaining(tmp_path, monkeypatch):
    from kew import credential_issuer, run_credentials

    monkeypatch.setenv("OPENROUTER_PROVISIONING_KEY", "management-secret-1944")
    monkeypatch.setattr(
        run_credentials,
        "effective_budget_view",
        lambda *a: SimpleNamespace(runtime_cap_usd=None, daily_remaining_usd=4.5),
    )
    mint, revoke = Mock(), Mock()
    mint.return_value = credential_issuer.BoundedRunKey(
        "run-secret-1944", "a" * 64, 4.5, "2099-01-01T00:00:00Z"
    )
    monkeypatch.setattr(credential_issuer, "mint_bounded_run_key", mint)
    monkeypatch.setattr(credential_issuer, "revoke_run_key", revoke)
    with run_credentials.bounded_run_credentials(
        run_id="run",
        config=DispatchConfig(),
        store=SqliteRunStore(str(tmp_path / "runs.db"), project_root=tmp_path),
        model="openrouter/test",
        credential_names=(),
        timeout_seconds=120,
    ):
        assert mint.call_args.args[1] == 4.5


def test_bounded_launch_environment_has_no_global_mutation(monkeypatch):
    import os

    from kew.credential_issuer import BoundedRunKey
    from kew.orchestrator import _build_child_env
    from kew.run_credentials import launch_environment

    monkeypatch.setenv("OPENROUTER_PROVISIONING_KEY", "management-secret-1944")
    monkeypatch.setenv("OPENROUTER_API_KEY", "host-secret-1944")
    key = BoundedRunKey("run-secret-1944", "a" * 64, 1, "2099-01-01T00:00:00Z")
    env = launch_environment(_build_child_env(), key)
    assert "OPENROUTER_PROVISIONING_KEY" not in env
    assert env["OPENROUTER_API_KEY"] == "run-secret-1944"
    assert "OPENROUTER_PROVISIONING_KEY" not in _build_child_env()
    assert os.environ["OPENROUTER_API_KEY"] == "host-secret-1944"


def test_bounded_lifecycle_preserves_recorded_posture(tmp_path, monkeypatch):
    from kew import credential_issuer
    from kew.audit import Actor, emit
    from kew.run_credentials import bounded_run_credentials
    from kew.run_store import SqliteRunStore
    from kew.service.runs import get_run_posture

    monkeypatch.setenv("OPENROUTER_PROVISIONING_KEY", "management-secret-1944")
    key = credential_issuer.BoundedRunKey(
        "run-secret-1944", "a" * 64, 25, "2099-01-01T00:00:00Z"
    )
    monkeypatch.setattr(
        credential_issuer, "mint_bounded_run_key", Mock(return_value=key)
    )
    monkeypatch.setattr(credential_issuer, "revoke_run_key", Mock())
    with SqliteRunStore(str(tmp_path / "runs.db"), project_root=tmp_path) as store:
        emit(
            store,
            "sandbox_resolved",
            actor=Actor("policy", "test"),
            run_id="run",
            payload={
                "posture": "sandboxed",
                "credential_names": ["OPENROUTER_API_KEY"],
            },
        )
        with bounded_run_credentials(
            run_id="run",
            config=DispatchConfig(),
            store=store,
            model="openrouter/test",
            credential_names=(),
            timeout_seconds=120,
        ):
            assert get_run_posture(store, "run").posture == "sandboxed"
        event = store.list_audit_events(
            event="sandbox_resolved", run_id="run", limit=1
        )[0]
        assert event.payload["credential_issuer"]["revoked"] is True
        assert get_run_posture(store, "run").posture == "sandboxed"


def test_bounded_terminal_revocation_rejects_issued_secret(monkeypatch, caplog):
    from kew import credential_issuer, run_credentials

    # Even a credential matching the hash grammar must never enter a URL.
    secret = "d" * 64
    key = credential_issuer.BoundedRunKey(secret, secret, 1, "2099-01-01T00:00:00Z")
    request = Mock(return_value=b"")
    monkeypatch.setattr(credential_issuer, "_request", request)
    run_credentials._revoke(None, "run", key)
    request.assert_not_called()
    assert "revocation skipped: invalid issuer identity" in caplog.text
    assert secret not in caplog.text


def test_bounded_all_issuer_management_names_use_shared_filter(monkeypatch):
    from kew import credential_issuer
    from kew.readiness_probe import probe_env
    from kew.run_credentials import launch_environment

    names = ("OPENROUTER_PROVISIONING_KEY", "SECOND_ISSUER_MANAGEMENT_KEY")
    monkeypatch.setattr(
        credential_issuer, "MANAGEMENT_CREDENTIAL_ENVS", names, raising=False
    )
    for name in names:
        monkeypatch.setenv(name, "management-secret-1944")
    key = credential_issuer.BoundedRunKey(
        "run-secret-1944", "a" * 64, 1, "2099-01-01T00:00:00Z"
    )
    env = launch_environment(dict.fromkeys(names, "management-secret-1944"), key)
    assert all(name not in env for name in names)
    assert env == {"OPENROUTER_API_KEY": "run-secret-1944"}
    assert all(name not in probe_env() for name in names)


def _driver_issuance(store, *, run_id="inherited-run"):
    import hashlib
    from datetime import UTC, datetime

    store.append_audit_event(
        timestamp=datetime.now(UTC).isoformat(),
        event="sandbox_resolved",
        run_id=run_id,
        actor_type="policy",
        actor_id="credential-issuer",
        payload={
            "phase": "issuance",
            "issued_by": "driver",
            "credential_issuer": {
                "issuer": "openrouter",
                "key_hash": "a" * 64,
                "limit_usd": 7,
                "expires_at": "2099-01-01T00:00:00Z",
            },
            "secret_sha256": hashlib.sha256(b"bounded-secret-1944").hexdigest(),
        },
    )


def test_child_inherits_without_minting_or_revoking(tmp_path, monkeypatch):
    from kew import credential_issuer
    from kew.run_credentials import bounded_run_credentials
    from kew.run_store import SqliteRunStore

    monkeypatch.delenv("OPENROUTER_PROVISIONING_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "bounded-secret-1944")
    monkeypatch.setenv("KEW_ISSUED_RUN_KEY_HASH", "a" * 64)
    mint, revoke = Mock(), Mock()
    monkeypatch.setattr(credential_issuer, "mint_bounded_run_key", mint)
    monkeypatch.setattr(credential_issuer, "revoke_run_key", revoke)
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        _driver_issuance(store)
        with bounded_run_credentials(
            run_id="inherited-run",
            config=DispatchConfig(),
            store=store,
            model="openrouter/test",
            credential_names=(),
            timeout_seconds=60,
        ) as key:
            assert key is not None
            assert key.secret == "bounded-secret-1944"
            assert key.limit_usd == 7
        mint.assert_not_called()
        revoke.assert_not_called()
        assert (
            store.list_audit_events(run_id="inherited-run")[0].payload["phase"]
            == "inherited"
        )


@pytest.mark.parametrize("mismatch", ["secret", "marker", "run_id", "missing_event"])
def test_child_refuses_unverified_issued_credential(tmp_path, monkeypatch, mismatch):
    from kew import credential_issuer
    from kew.run_credentials import bounded_run_credentials
    from kew.run_store import SqliteRunStore

    monkeypatch.delenv("OPENROUTER_PROVISIONING_KEY", raising=False)
    monkeypatch.setenv(
        "OPENROUTER_API_KEY",
        "wrong-secret" if mismatch == "secret" else "bounded-secret-1944",
    )
    monkeypatch.setenv(
        "KEW_ISSUED_RUN_KEY_HASH", "b" * 64 if mismatch == "marker" else "a" * 64
    )
    mint, revoke, publish = Mock(), Mock(), Mock()
    monkeypatch.setattr(credential_issuer, "mint_bounded_run_key", mint)
    monkeypatch.setattr(credential_issuer, "revoke_run_key", revoke)
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        if mismatch != "missing_event":
            _driver_issuance(store)
        with (
            pytest.raises(
                credential_issuer.IssuerRefused, match="issued credential mismatch"
            ),
            bounded_run_credentials(
                run_id="other-run" if mismatch == "run_id" else "inherited-run",
                config=DispatchConfig(),
                store=store,
                model="openrouter/test",
                credential_names=(),
                timeout_seconds=60,
            ),
        ):
            publish()
        mint.assert_not_called()
        revoke.assert_not_called()
        publish.assert_not_called()


@pytest.fixture(autouse=True)
def round16_empty_issuer_sweep(monkeypatch):
    from kew import credential_issuer

    monkeypatch.setattr(credential_issuer, "list_run_keys", Mock(return_value=[]))


def test_issued_run_key_is_redacted_while_it_lives(tmp_path, monkeypatch):
    """Re-audit F7: the minted key lives only in the child's launch env."""
    from kew import credential_issuer
    from kew.credential_surface import redact_credentials
    from kew.run_credentials import bounded_run_credentials

    monkeypatch.setenv("OPENROUTER_PROVISIONING_KEY", "management-secret-1978")
    secret = "run-secret-1978-placeholder"
    monkeypatch.setattr(
        credential_issuer,
        "mint_bounded_run_key",
        Mock(
            return_value=credential_issuer.BoundedRunKey(
                secret, "a" * 64, 25, "2099-01-01T00:00:00Z"
            )
        ),
    )
    monkeypatch.setattr(credential_issuer, "revoke_run_key", Mock())
    with bounded_run_credentials(
        run_id="run",
        config=DispatchConfig(),
        store=SqliteRunStore(str(tmp_path / "runs.db"), project_root=tmp_path),
        model="openrouter/model",
        credential_names=(),
        timeout_seconds=120,
    ) as key:
        assert key is not None and key.secret == secret
        assert secret not in redact_credentials(f"agent said: my key is {secret}")
    # Unregistered on exit; the key-shape backstop still covers OpenRouter keys.
    assert secret in redact_credentials(secret)
    shaped = "sk-or-v1-" + "0123456789abcdef" * 4
    assert shaped not in redact_credentials(f"curl -H 'x: {shaped}'")


def test_failed_issuance_record_revokes_the_minted_key(tmp_path, monkeypatch):
    """Re-audit F9: a key whose issuance record fails is revoked in-process."""
    from kew import credential_evidence, credential_issuer, run_credentials
    from kew.graph.driver import credentials as driver_credentials

    monkeypatch.setenv("OPENROUTER_PROVISIONING_KEY", "management-secret-1978")
    mint, revoke = Mock(), Mock()
    mint.return_value = credential_issuer.BoundedRunKey(
        "run-secret-1978", "a" * 64, 25, "2099-01-01T00:00:00Z"
    )
    monkeypatch.setattr(credential_issuer, "mint_bounded_run_key", mint)
    monkeypatch.setattr(credential_issuer, "revoke_run_key", revoke)
    recorded: list[tuple[str, str | None]] = []

    def refuse(store, run_id, payload, *, item_id=None):
        recorded.append((run_id, item_id))
        assert payload["phase"] == "issuance"
        raise credential_evidence.IssuanceEvidenceRefused()

    monkeypatch.setattr(run_credentials, "record_issuance_evidence", refuse)
    monkeypatch.setattr(driver_credentials, "record_issuance_evidence", refuse)
    store = SqliteRunStore(str(tmp_path / "runs.db"), project_root=tmp_path)
    with (
        pytest.raises(credential_evidence.IssuanceEvidenceRefused),
        run_credentials.bounded_run_credentials(
            run_id="run",
            config=DispatchConfig(),
            store=store,
            model="openrouter/model",
            credential_names=(),
            timeout_seconds=120,
        ),
    ):
        pytest.fail("a key without issuance evidence must not be yielded")
    assert (mint.call_count, revoke.call_count) == (1, 1)
    with pytest.raises(credential_evidence.IssuanceEvidenceRefused):
        driver_credentials.issue_driver_key(store, DispatchConfig(), "driver-run", "7")
    assert (mint.call_count, revoke.call_count) == (2, 2)
    assert recorded == [("run", None), ("driver-run", "7")]
    assert revoke.call_args.args == ("a" * 64,)
    assert revoke.call_args.kwargs == {"issued_key": "run-secret-1978"}


# --- the OpenShell credential broker (epic openshell-runtime, node N5) -------
#
# On `[sandbox] runtime = "openshell"` the minted key never reaches the agent:
# it goes into a per-run gateway PROVIDER and the gateway injects it at the
# endpoint. Two orderings make that safe, and both are asserted here rather
# than described:
#
#   * the provider is deleted BEFORE the key it holds is revoked — the reverse
#     leaves the gateway injecting a dead credential, which surfaces as an
#     inference error with no cause attached;
#   * the provider is deleted on EVERY exit, including the one where the
#     dispatch raised.
#
# The seam is `kew.subprocess_utils.run`, the same chokepoint the docker calls
# go through, so these tests see the exact argv kew would launch — which is how
# the "no credential value in argv" rule is checked here too.

_BROKER_SECRET = "run-secret-openshell-2318"


def _provider_call(cmd) -> str:
    """``provider <verb> <name>`` out of one recorded argv."""
    at = list(cmd).index("provider")
    return " ".join(cmd[at : at + 3])


def _openshell_dispatch_config(**openshell):
    from kew.config import OpenShellConfig, SandboxConfig

    return DispatchConfig(
        sandbox=SandboxConfig(
            runtime="openshell",
            openshell=OpenShellConfig(
                gateway="http://gateway.invalid:8080",
                providers={"opencode": "kew-opencode"},
                **openshell,
            ),
        )
    )


class _BrokerCli:
    """The ``openshell`` CLI, recording every provider call in order."""

    def __init__(self, fail: BaseException | None = None) -> None:
        self.events: list[str] = []
        self.calls: list[tuple[tuple[str, ...], dict]] = []
        self.fail = fail

    def run(self, cmd, *, env=None, retain_credentials=(), **_):
        import subprocess

        self.calls.append((tuple(cmd), dict(env or {})))
        self.events.append(_provider_call(cmd))
        if self.fail is not None and "create" in cmd:
            raise self.fail
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def install(self, monkeypatch, revoke):
        from kew import credential_issuer, subprocess_utils

        monkeypatch.setattr(subprocess_utils, "run", self.run)
        monkeypatch.setattr(
            credential_issuer,
            "revoke_run_key",
            lambda *a, **k: self.events.append("revoke") or "revoked",
        )
        return self


def _minted(monkeypatch, secret: str = _BROKER_SECRET):
    from kew import credential_issuer

    monkeypatch.setenv("OPENROUTER_PROVISIONING_KEY", "management-secret-2318")
    key = credential_issuer.BoundedRunKey(secret, "b" * 64, 25, "2099-01-01T00:00:00Z")
    monkeypatch.setattr(
        credential_issuer, "mint_bounded_run_key", Mock(return_value=key)
    )
    return key


def _bounded(config, store, run_id="run-2318", *, contained=True):
    from kew.run_credentials import bounded_run_credentials

    return bounded_run_credentials(
        run_id=run_id,
        config=config,
        store=store,
        model="openrouter/model",
        credential_names=(),
        timeout_seconds=120,
        contained=contained,
    )


def test_openshell_per_run_provider_is_deleted_before_the_key_is_revoked(
    tmp_path, monkeypatch
):
    """AC1: created around the run, gone before the key it holds is revoked."""
    from kew.openshell_broker import run_provider_name

    key = _minted(monkeypatch)
    cli = _BrokerCli().install(monkeypatch, revoke=None)
    store = SqliteRunStore(str(tmp_path / "runs.db"), project_root=tmp_path)
    with _bounded(_openshell_dispatch_config(), store) as issued:
        assert issued is not None and issued.secret == key.secret
        assert cli.events == ["provider create kew-run-run-2318"]
    assert cli.events[1:] == ["provider delete kew-run-run-2318", "revoke"]
    assert run_provider_name("run-2318") == "kew-run-run-2318"
    # The value travelled in the child environment, never in an argument.
    create_argv, create_env = cli.calls[0]
    assert key.secret not in " ".join(create_argv)
    assert create_env["OPENROUTER_API_KEY"] == key.secret
    assert "OPENROUTER_PROVISIONING_KEY" not in create_env


def test_openshell_per_run_provider_is_deleted_when_the_run_raises(
    tmp_path, monkeypatch
):
    """AC1: the failure path unwinds in the same order as the success path."""
    _minted(monkeypatch)
    cli = _BrokerCli().install(monkeypatch, revoke=None)
    store = SqliteRunStore(str(tmp_path / "runs.db"), project_root=tmp_path)
    with (
        pytest.raises(RuntimeError, match="dispatch blew up"),
        _bounded(_openshell_dispatch_config(), store),
    ):
        raise RuntimeError("dispatch blew up")
    assert cli.events == [
        "provider create kew-run-run-2318",
        "provider delete kew-run-run-2318",
        "revoke",
    ]


def test_openshell_provider_creation_failure_still_revokes_the_minted_key(
    tmp_path, monkeypatch
):
    """AC1: a key that never reached a provider is not left live."""
    from kew.openshell import OpenShellGatewayUnreachable

    _minted(monkeypatch)
    unreachable = OpenShellGatewayUnreachable(
        ("openshell", "provider", "create"), reason="gateway refused the connection"
    )
    cli = _BrokerCli(fail=unreachable).install(monkeypatch, revoke=None)
    store = SqliteRunStore(str(tmp_path / "runs.db"), project_root=tmp_path)
    with (
        pytest.raises(OpenShellGatewayUnreachable),
        _bounded(_openshell_dispatch_config(), store),
    ):
        pytest.fail("a key with no provider must not be yielded")
    assert cli.events[-1] == "revoke"
    assert not [event for event in cli.events if "delete" in event]


def test_openshell_cleanup_failure_is_loud_and_keeps_revoking_the_key(
    tmp_path, monkeypatch, caplog
):
    """Rule 4: a provider that could not be deleted is said, not swallowed."""
    import logging

    from kew import credential_issuer, subprocess_utils
    from kew.openshell import OpenShellTimeout

    _minted(monkeypatch)
    events: list[str] = []
    timeout = OpenShellTimeout(
        ("openshell", "provider", "delete"), reason="openshell exceeded 10s"
    )

    def run(cmd, **_):
        import subprocess

        verb = _provider_call(cmd).split()[1]
        events.append(verb)
        if verb == "delete":
            raise timeout
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess_utils, "run", run)
    monkeypatch.setattr(
        credential_issuer,
        "revoke_run_key",
        lambda *a, **k: events.append("revoke") or "revoked",
    )
    store = SqliteRunStore(str(tmp_path / "runs.db"), project_root=tmp_path)
    with (
        caplog.at_level(logging.ERROR, logger="kew.openshell_broker"),
        _bounded(_openshell_dispatch_config(), store),
    ):
        pass
    assert events == ["create", "delete", "revoke"]
    assert "kew-run-run-2318 could not be deleted" in caplog.text


def test_an_uncontained_openshell_run_mints_no_gateway_provider(tmp_path, monkeypatch):
    """A run on the host is brokered by nothing, whatever `runtime` names.

    `[sandbox] runtime = "openshell"` plus `dangerously_skip_containment` runs
    the agent on the host with the credential in its own environment: there is
    no gateway in that path at all. Keying the broker off the configured runtime
    name alone pushed the minted key into a provider for it anyway — and
    hard-failed the dispatch on a host with no `openshell` CLI installed.
    """
    _minted(monkeypatch)
    cli = _BrokerCli().install(monkeypatch, revoke=None)
    config = _openshell_dispatch_config()
    store = SqliteRunStore(str(tmp_path / "runs.db"), project_root=tmp_path)
    with _bounded(config, store, contained=False) as issued:
        assert issued is not None
        assert cli.calls == []
    # The key still has its full lifecycle; only the provider is absent.
    assert cli.events == ["revoke"]
    assert config.sandbox.runtime == "openshell"


def test_an_inherited_bounded_key_gets_the_same_per_run_provider(tmp_path, monkeypatch):
    """A key the driver minted is still this run's key, so it is still brokered.

    A detached dispatch child is HANDED the driver's bounded key and mints
    nothing itself, so the provisioning key that minted it is not in this
    process at all. Skipping the broker here created no provider while the
    launch sites still recorded `broker:openshell:kew-run-<run_id>` as what
    authenticated the attempt — an auth path naming a provider that does not
    exist, beside a posture event naming the operator's long-lived provider
    (#2318 review). The driver owns revocation, so this child creates and
    deletes the provider and revokes nothing.
    """
    from kew.openshell_broker import (
        MINTED_ROUTE,
        attested_provider,
        broker_route,
        launch_provider,
        run_provider_name,
    )

    monkeypatch.delenv("OPENROUTER_PROVISIONING_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "bounded-secret-1944")
    monkeypatch.setenv("KEW_ISSUED_RUN_KEY_HASH", "a" * 64)
    cli = _BrokerCli().install(monkeypatch, revoke=None)
    config = _openshell_dispatch_config()
    per_run = run_provider_name("inherited-run")
    with SqliteRunStore(str(tmp_path / "runs.db"), project_root=tmp_path) as store:
        _driver_issuance(store)
        with _bounded(config, store, run_id="inherited-run") as issued:
            assert issued is not None and issued.secret == "bounded-secret-1944"
            assert cli.events == [f"provider create {per_run}"]
        assert cli.events[1:] == [f"provider delete {per_run}"]
    # The value reached the gateway in the environment, never in an argument.
    create_argv, create_env = cli.calls[0]
    assert "bounded-secret-1944" not in " ".join(create_argv)
    assert create_env["OPENROUTER_API_KEY"] == "bounded-secret-1944"
    # And both ledger facts about this launch name the provider that exists.
    assert broker_route("opencode", direct=True) == MINTED_ROUTE
    assert (
        attested_provider(
            config.sandbox, "opencode", run_id="inherited-run", direct=True
        )
        == per_run
    )
    assert (
        launch_provider(config.sandbox, "opencode", run_id="inherited-run", issued=True)
        == per_run
    )


def test_an_uncontained_inherited_openshell_run_brokers_nothing(tmp_path, monkeypatch):
    """The uncontained half of the inherited pair: no gateway in its path.

    An inherited key on the host runs with the value in the agent's own
    environment, exactly as a locally minted one does.
    """
    monkeypatch.delenv("OPENROUTER_PROVISIONING_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "bounded-secret-1944")
    monkeypatch.setenv("KEW_ISSUED_RUN_KEY_HASH", "a" * 64)
    cli = _BrokerCli().install(monkeypatch, revoke=None)
    with SqliteRunStore(str(tmp_path / "runs.db"), project_root=tmp_path) as store:
        _driver_issuance(store)
        with _bounded(
            _openshell_dispatch_config(), store, run_id="inherited-run", contained=False
        ) as issued:
            assert issued is not None
    assert cli.calls == [] and cli.events == []


def test_a_create_of_unknown_outcome_deletes_the_provider_it_may_have_made(
    tmp_path, monkeypatch
):
    """`OpenShellOutcomeUnknown` must never be read as "nothing happened".

    A create that broke after it went on the wire may already have committed,
    so the provider can be holding the key the `finally` below revokes moments
    later — a live provider injecting a dead credential. The delete runs before
    the failure is re-raised.
    """
    from kew.openshell import OpenShellTimeout

    _minted(monkeypatch)
    timeout = OpenShellTimeout(
        ("openshell", "provider", "create"), reason="openshell exceeded 60s"
    )
    cli = _BrokerCli(fail=timeout).install(monkeypatch, revoke=None)
    store = SqliteRunStore(str(tmp_path / "runs.db"), project_root=tmp_path)
    with (
        pytest.raises(OpenShellTimeout),
        _bounded(_openshell_dispatch_config(), store),
    ):
        pytest.fail("a key whose provider state is unknown must not be yielded")
    assert cli.events == [
        "provider create kew-run-run-2318",
        "provider delete kew-run-run-2318",
        "revoke",
    ]


def test_openshell_launch_environment_carries_no_credential_value(monkeypatch):
    """AC3: on this runtime the launch environment holds names, not values."""
    from kew.openshell_broker import PLACEHOLDER_CREDENTIAL_VALUE
    from kew.run_credentials import openshell_launch_environment
    from kew.sandbox import runner_credential_env_vars

    secrets = {
        "OPENROUTER_API_KEY": "host-secret-2318",
        "OPENROUTER_PROVISIONING_KEY": "management-secret-2318",
        "ANTHROPIC_API_KEY": "anthropic-secret-2318",
        "CLAUDE_CODE_OAUTH_TOKEN": "oauth-secret-2318",
        "GH_TOKEN": "github-secret-2318",
        "KEW_ISSUED_RUN_KEY_HASH": "b" * 64,
    }
    for name, value in secrets.items():
        monkeypatch.setenv(name, value)
    environment = openshell_launch_environment(dict(os.environ), "claude-code")
    assert not [
        name for name, value in environment.items() if value in secrets.values()
    ]
    declared = runner_credential_env_vars("claude-code")
    assert declared
    assert all(
        environment[name] == PLACEHOLDER_CREDENTIAL_VALUE for name in declared
    ), environment


def test_openshell_launch_environment_forwards_only_the_allowlist(monkeypatch):
    """Shown to fail: it removed credential names and kept everything else.

    `openshell sandbox exec` forwards this mapping into the sandbox, so it is
    the contained agent's environment. Built as the host environment minus
    kew's credential names, it handed the agent every host variable kew does
    not enumerate — the two below stand for a CI token and an operator's
    editor session token — while the docker boundary beside it passes the
    runner's declared credential names and nothing else (PR #2401 review).

    The assertion is the exact key set rather than "the secrets are absent": a
    denylist passes that weaker form until the next unenumerated name appears,
    which is the whole failure mode.
    """
    from kew.openshell import SANDBOX_EXEC_ENV_NAMES, SANDBOX_PATH
    from kew.run_credentials import openshell_launch_environment
    from kew.sandbox import runner_credential_env_vars

    monkeypatch.setenv("ACME_DEPLOY_TOKEN", "not-enumerated-by-kew")
    monkeypatch.setenv("EDITOR_SESSION_TOKEN", "also-not-enumerated")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setenv("PATH", "/work/bin:.:/usr/bin")

    environment = openshell_launch_environment(dict(os.environ), "opencode")

    declared = set(runner_credential_env_vars("opencode"))
    assert declared
    assert set(environment) <= set(SANDBOX_EXEC_ENV_NAMES) | declared
    assert environment["PATH"] == SANDBOX_PATH
