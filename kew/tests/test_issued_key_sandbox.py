"""Inherited driver credentials at the real container launcher seam."""

from datetime import UTC, datetime
from unittest.mock import Mock

from kew import credential_issuer, dispatch_core, dispatch_sandbox
from kew.config import DispatchConfig
from kew.graph.driver.credentials import issue_driver_key
from kew.run_store import RunRecord, SqliteRunStore
from kew.tracking import DispatchResult


def test_sandbox_inherits_driver_key_without_owning_issuance(tmp_path, monkeypatch):
    config = DispatchConfig()
    monkeypatch.setenv("OPENROUTER_PROVISIONING_KEY", "management-secret-1944")
    mint = Mock(
        side_effect=lambda run_id, limit, expiry, *, ledger_id, on_malformed: (
            credential_issuer.BoundedRunKey(
                "bounded-secret-1944", "a" * 64, limit, expiry.isoformat()
            )
        )
    )
    revoke, launch = Mock(), Mock(return_value=Mock())
    monkeypatch.setattr(credential_issuer, "mint_bounded_run_key", mint)
    monkeypatch.setattr(credential_issuer, "revoke_run_key", revoke)
    monkeypatch.setattr(dispatch_core, "_launch_container", launch)
    monkeypatch.setattr(dispatch_core, "_stream_sandbox_log", Mock(return_value=0))
    monkeypatch.setattr(dispatch_sandbox, "finalize_container_hard_stop", Mock())
    with SqliteRunStore(str(tmp_path / "runs.db"), project_root=tmp_path) as store:
        key = issue_driver_key(store, config, "inherited-run", "1944")
        mint.reset_mock()
        monkeypatch.delenv("OPENROUTER_PROVISIONING_KEY")
        monkeypatch.setenv("OPENROUTER_API_KEY", key.secret)
        monkeypatch.setenv("KEW_ISSUED_RUN_KEY_HASH", key.key_hash)
        store.save(
            RunRecord(
                run_id="inherited-run",
                issue_num=1944,
                branch="agent/1944",
                status="pending",
                exit_code=-1,
                started_at=datetime.now(UTC),
            )
        )
        assert (
            dispatch_sandbox.run_container_and_stream(
                config,
                store,
                image="sha256:" + "b" * 64,
                runner="opencode",
                checkout_dir=tmp_path,
                slug="1944",
                run_id="inherited-run",
                model="openrouter/test",
                log_file=tmp_path / "run.log",
                run_kind="adversarial_review",
                result=DispatchResult(
                    exit_code=-1, issue_num=1944, log_file=tmp_path / "run.log"
                ),
            )
            == 0
        )
        mint.assert_not_called()
        revoke.assert_not_called()
        env = launch.call_args.kwargs["env"]
        assert env["OPENROUTER_API_KEY"] == key.secret
        assert "OPENROUTER_PROVISIONING_KEY" not in env
        events = store.list_audit_events(
            event="sandbox_resolved", run_id="inherited-run"
        )
        assert any(event.payload.get("phase") == "inherited" for event in events)
