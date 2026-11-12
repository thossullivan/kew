"""Tests for the config-override service (kew.service.config, #681 Group 2).

Covers the approver gate, policy binding, auto-composed provenance, and the
emit-before-write ordering that the serve route reaches only through this shim.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest

from kew.config import load_config
from kew.human_review import ReviewPolicyError, policy_from_config
from kew.run_store import SqliteRunStore
from kew.service.config import (
    OverrideDenied,
    OverrideNotFound,
    StaleGeneration,
    human_review_state,
    is_spend_authorizing,
    revert_config_override,
    revert_human_review_override,
    set_config_override,
    set_human_review_override,
)


@pytest.fixture
def store(tmp_path):
    s = SqliteRunStore(str(tmp_path / "dispatch.db"))
    try:
        yield s
    finally:
        s.close()


def _config(approvers=None):
    cfg = load_config().model_copy(deep=True)
    if approvers is not None:
        cfg.approval.approvers = approvers
    return cfg


class TestSpendAuthorizing:
    def test_known_spend_fields_are_gated(self):
        for field in (
            "budget.per_issue_usd",
            "agent.max_budget_usd",
            "agent.model",
            "routing.medium",
        ):
            assert is_spend_authorizing(field)

    def test_default_strong_for_unclassified_field(self):
        assert is_spend_authorizing("some.future_field")


class TestApproverGate:
    def test_denied_actor_cannot_set_and_nothing_written(self, store, monkeypatch):
        monkeypatch.setenv("KEW_ACTOR", "mallory")
        cfg = _config(approvers=["alice"])
        with pytest.raises(OverrideDenied):
            set_config_override(
                store, field="budget.per_issue_usd", value=42.0, config=cfg
            )
        # Gate fires before any side-effect: no override row, no audit event.
        assert store.get_config_override("budget.per_issue_usd") is None
        assert store.list_audit_events(limit=10, event="config_override_set") == []

    def test_listed_approver_can_set(self, store, monkeypatch):
        monkeypatch.setenv("KEW_ACTOR", "alice")
        cfg = _config(approvers=["alice"])
        result = set_config_override(
            store, field="budget.per_issue_usd", value=42.0, config=cfg
        )
        assert result.field == "budget.per_issue_usd"
        assert result.effective == 42.0
        assert store.get_config_override("budget.per_issue_usd") is not None

    def test_empty_approvers_allows_any_actor(self, store, monkeypatch):
        monkeypatch.setenv("KEW_ACTOR", "whoever")
        cfg = _config(approvers=[])
        set_config_override(
            store, field="agent.model", value="claude-opus-4-8", config=cfg
        )
        assert store.get_config_override("agent.model") is not None


class TestPolicyBinding:
    def test_event_carries_policy_hash_and_snapshot_retained(self, store, monkeypatch):
        monkeypatch.setenv("KEW_ACTOR", "alice")
        cfg = _config(approvers=["alice"])
        set_config_override(store, field="budget.daily_usd", value=100.0, config=cfg)
        ev = store.list_audit_events(limit=1, event="config_override_set")[0]
        assert ev.policy_hash is not None
        assert store.get_policy_snapshot(ev.policy_hash) is not None


class TestProvenance:
    def test_provenance_auto_composed_with_empty_reason(self, store):
        cfg = _config()
        set_config_override(
            store,
            field="budget.per_issue_usd",
            value=15.0,
            config=cfg,
            source="web",
        )
        payload = store.list_audit_events(limit=1, event="config_override_set")[
            0
        ].payload
        assert payload["field"] == "budget.per_issue_usd"
        assert payload["new_value"] == 15.0
        assert payload["actor"]  # actor id auto-composed, non-empty
        assert payload["source"] == "web"
        assert payload["reason"] == ""
        assert "file_value" in payload  # old→new provenance

    def test_event_emitted_before_store_write(self, store, monkeypatch):
        cfg = _config()

        def boom(**_kwargs):
            raise RuntimeError("write failed")

        monkeypatch.setattr(store, "set_config_override", boom)
        with pytest.raises(RuntimeError):
            set_config_override(
                store, field="agent.model", value="claude-opus-4-8", config=cfg
            )
        # The audit event persists even though the effector failed.
        events = store.list_audit_events(limit=10, event="config_override_set")
        assert len(events) == 1


class TestRevert:
    def test_revert_missing_raises_not_found(self, store):
        with pytest.raises(OverrideNotFound):
            revert_config_override(
                store, field="budget.per_issue_usd", config=_config()
            )

    def test_off_allowlist_field_raises_value_error(self, store):
        with pytest.raises(ValueError, match="not overridable"):
            revert_config_override(
                store, field="security.enable_network", config=_config()
            )

    def test_denied_actor_cannot_revert(self, store, monkeypatch):
        monkeypatch.setenv("KEW_ACTOR", "alice")
        cfg = _config(approvers=["alice"])
        set_config_override(store, field="budget.per_issue_usd", value=42.0, config=cfg)
        monkeypatch.setenv("KEW_ACTOR", "mallory")
        with pytest.raises(OverrideDenied):
            revert_config_override(store, field="budget.per_issue_usd", config=cfg)
        assert store.get_config_override("budget.per_issue_usd") is not None

    def test_revert_clears_and_records_provenance(self, store, monkeypatch):
        monkeypatch.setenv("KEW_ACTOR", "alice")
        cfg = _config(approvers=["alice"])
        set_config_override(store, field="budget.per_issue_usd", value=42.0, config=cfg)
        result = revert_config_override(store, field="budget.per_issue_usd", config=cfg)
        assert result.field == "budget.per_issue_usd"
        assert store.get_config_override("budget.per_issue_usd") is None
        rev = store.list_audit_events(limit=1, event="config_override_reverted")[0]
        assert rev.payload["field"] == "budget.per_issue_usd"
        assert rev.payload["actor"] == "alice"
        assert rev.policy_hash is not None


# ── the human-review policy override (I13C, Tasks 55-56) ────────────


class TestHumanReviewOverride:
    """Set / lower / delete the three numbers, with generation and expiry.

    Every case here is about the *durable* policy row I13 owns — not the
    ``config_overrides`` table. The two must never alias: a human_review edit
    that landed in the generic override store would have no generation, no
    expiry and no reversion evidence.
    """

    _REPO = "acme/widgets"
    _NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
    _SOON = datetime(2026, 8, 21, 18, 0, tzinfo=UTC)
    _AFTER = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
    _FIELD = "human_review.max_diff_lines"

    def _cfg(self, approvers=None):
        cfg = _config(approvers=approvers)
        cfg.project.repo = self._REPO
        return cfg

    def _set(self, store, cfg, **kwargs):
        params = {"field": self._FIELD, "value": 200, "now": self._NOW}
        params.update(kwargs)
        return set_human_review_override(store, config=cfg, **params)

    def _hold(self, store, cfg, run_id):
        """Take one real review slot so occupancy is not a mocked number."""
        from kew.human_review import PathChange, decide_publication, measure_risk
        from kew.publication_job import PublicationIntent
        from kew.publication_repository import VERIFICATION_EXACT_TREE
        from kew.run_store import ArtifactLineage

        base, tree = "b" * 40, hashlib.sha1(run_id.encode()).hexdigest()
        policy = policy_from_config(cfg)
        lineage = ArtifactLineage(
            source_kind="github",
            source_id=self._REPO,
            run_id=run_id,
            attempt_number=1,
            base_sha=base,
            package_digest="sha256:" + "a" * 64,
            tree_sha=tree,
            verification=VERIFICATION_EXACT_TREE,
            locator="packages/" + "a" * 64,
        )
        receipt = store.seal_artifact_receipt(lineage=lineage, sealed_at=self._NOW)
        job = store.open_publication_job(
            intent=PublicationIntent(
                source_kind="github",
                source_id=self._REPO,
                run_id=run_id,
                attempt_number=1,
                branch=f"agent/{run_id}",
                base_sha=base,
                tree_sha=tree,
                receipt_digest=receipt.receipt_digest,
                message="feat: the change",
            ),
            now=self._NOW,
        )
        risk = measure_risk(
            base_sha=base, tree_sha=tree, changes=[PathChange("src/big.py", added=900)]
        )
        store.record_publication_review(
            job_id=job.job_id,
            policy=policy,
            risk=risk,
            decision=decide_publication(
                policy=policy,
                risk=risk,
                pending_reviews=store.pending_review_count(self._REPO),
            ),
            now=self._NOW,
        )
        store.reserve_publication_review(
            job_id=job.job_id, policy=policy, actor="ada", now=self._NOW
        )
        return job

    # -- set / lower -----------------------------------------------------

    def test_lowering_a_field_advances_the_generation(self, store, monkeypatch):
        monkeypatch.setenv("KEW_ACTOR", "alice")
        cfg = self._cfg(approvers=["alice"])
        state = self._set(store, cfg)

        assert state.values[self._FIELD] == 200
        assert state.values["human_review.max_paths"] == 20  # untouched
        assert state.generation == 1
        assert state.source == "override"
        assert state.expires_at is None
        # A second edit supersedes the first rather than stacking.
        again = self._set(store, cfg, field="human_review.max_paths", value=5)
        assert again.generation == 2
        assert again.values[self._FIELD] == 200
        assert again.values["human_review.max_paths"] == 5

    def test_the_generic_override_store_is_never_aliased(self, store, monkeypatch):
        monkeypatch.setenv("KEW_ACTOR", "alice")
        self._set(store, self._cfg(approvers=["alice"]))
        assert store.get_config_override(self._FIELD) is None
        assert store.get_human_review_policy(self._REPO).max_diff_lines == 200

    @pytest.mark.parametrize("value", [-1, "many", 3.5, None])
    def test_an_unusable_value_is_refused(self, store, value):
        with pytest.raises(ValueError):
            self._set(store, self._cfg(), value=value)

    def test_an_off_policy_field_is_refused(self, store):
        with pytest.raises(ValueError, match="human_review"):
            self._set(store, self._cfg(), field="human_review.approvers", value=[])

    def test_a_config_without_a_delivery_repo_is_refused(self, store):
        cfg = _config()
        cfg.project.repo = ""
        with pytest.raises(ReviewPolicyError):
            human_review_state(store, config=cfg, now=self._NOW)

    # -- expiry ----------------------------------------------------------

    def test_expiry_is_durable_and_renders_a_pending_reversion(
        self, store, monkeypatch
    ):
        monkeypatch.setenv("KEW_ACTOR", "alice")
        cfg = self._cfg(approvers=["alice"])
        state = self._set(store, cfg, expires_at=self._SOON.isoformat())

        assert state.expires_at == self._SOON.isoformat()
        assert store.get_human_review_policy(self._REPO).expires_at == (
            self._SOON.isoformat()
        )
        assert state.pending_reversion == {
            "at": self._SOON.isoformat(),
            "reason": "expired",
            "to": {
                "human_review.max_pending_reviews": 3,
                "human_review.max_diff_lines": 500,
                "human_review.max_paths": 20,
                "generation": 2,
            },
        }

    @pytest.mark.parametrize(
        "expiry",
        ["2026-08-21T18:00:00", "not-a-time", "2026-08-21T06:00:00+00:00"],
    )
    def test_a_naive_or_past_expiry_is_refused(self, store, expiry):
        with pytest.raises(ValueError):
            self._set(store, self._cfg(), expires_at=expiry)
        assert store.get_human_review_policy(self._REPO) is None

    def test_an_expired_override_reads_as_the_system_policy(self, store, monkeypatch):
        monkeypatch.setenv("KEW_ACTOR", "alice")
        cfg = self._cfg(approvers=["alice"])
        self._set(store, cfg, expires_at=self._SOON.isoformat())

        state = human_review_state(store, config=cfg, now=self._AFTER)
        assert state.values[self._FIELD] == 500
        assert state.source == "system"
        assert state.generation == 2
        assert state.expires_at is None
        assert state.pending_reversion is None

    def test_reading_an_expired_override_writes_nothing(self, store, monkeypatch):
        """A render is not an effect: the durable reversion is the writers' job."""
        monkeypatch.setenv("KEW_ACTOR", "alice")
        cfg = self._cfg(approvers=["alice"])
        self._set(store, cfg, expires_at=self._SOON.isoformat())

        human_review_state(store, config=cfg, now=self._AFTER)
        assert store.get_human_review_policy(self._REPO) is not None
        assert store.list_audit_events(event="human_review_policy_reverted") == []

    def test_a_writer_durably_reverts_an_expired_override_before_its_edit(
        self, store, monkeypatch
    ):
        monkeypatch.setenv("KEW_ACTOR", "alice")
        cfg = self._cfg(approvers=["alice"])
        self._set(store, cfg, expires_at=self._SOON.isoformat())

        state = self._set(store, cfg, value=300, now=self._AFTER)
        assert state.values[self._FIELD] == 300
        assert state.generation == 3  # 1 -> reverted 2 -> the new edit
        reverted = store.list_audit_events(event="human_review_policy_reverted")
        assert len(reverted) == 1
        assert reverted[0].payload["reason"] == "expired"

    def test_a_stale_expiry_cannot_delete_a_newer_generation(self, store, monkeypatch):
        """The newer override replaced the expiry; the old clock is not a delete."""
        monkeypatch.setenv("KEW_ACTOR", "alice")
        cfg = self._cfg(approvers=["alice"])
        self._set(store, cfg, expires_at=self._SOON.isoformat())
        self._set(store, cfg, value=300)

        state = human_review_state(store, config=cfg, now=self._AFTER)
        assert state.source == "override"
        assert state.values[self._FIELD] == 300
        assert state.generation == 2
        assert store.get_human_review_policy(self._REPO) is not None

    # -- generation as a compare-and-swap --------------------------------

    def test_a_stale_generation_is_refused_and_changes_nothing(
        self, store, monkeypatch
    ):
        monkeypatch.setenv("KEW_ACTOR", "alice")
        cfg = self._cfg(approvers=["alice"])
        self._set(store, cfg)
        with pytest.raises(StaleGeneration):
            self._set(store, cfg, value=10, if_generation=0)
        assert store.get_human_review_policy(self._REPO).max_diff_lines == 200

    def test_the_current_generation_is_accepted(self, store, monkeypatch):
        monkeypatch.setenv("KEW_ACTOR", "alice")
        cfg = self._cfg(approvers=["alice"])
        self._set(store, cfg)
        assert self._set(store, cfg, value=10, if_generation=1).generation == 2

    def test_a_stale_generation_cannot_delete_a_newer_override(
        self, store, monkeypatch
    ):
        monkeypatch.setenv("KEW_ACTOR", "alice")
        cfg = self._cfg(approvers=["alice"])
        self._set(store, cfg)
        self._set(store, cfg, value=300)
        with pytest.raises(StaleGeneration):
            revert_human_review_override(
                store, config=cfg, if_generation=1, now=self._NOW
            )
        assert store.get_human_review_policy(self._REPO).max_diff_lines == 300

    # -- authority --------------------------------------------------------

    def test_a_blocked_actor_can_neither_set_nor_revert(self, store, monkeypatch):
        cfg = self._cfg(approvers=["alice"])
        monkeypatch.setenv("KEW_ACTOR", "alice")
        self._set(store, cfg)

        monkeypatch.setenv("KEW_ACTOR", "mallory")
        with pytest.raises(OverrideDenied):
            self._set(store, cfg, value=1)
        with pytest.raises(OverrideDenied):
            revert_human_review_override(store, config=cfg, now=self._NOW)
        assert store.get_human_review_policy(self._REPO).max_diff_lines == 200
        assert store.get_human_review_policy(self._REPO).generation == 1

    # -- evidence ---------------------------------------------------------

    def test_the_event_carries_old_new_actor_reason_time_and_generation(
        self, store, monkeypatch
    ):
        monkeypatch.setenv("KEW_ACTOR", "alice")
        cfg = self._cfg(approvers=["alice"])
        self._set(store, cfg, reason="release week")

        ev = store.list_audit_events(limit=1, event="human_review_override_set")[0]
        assert ev.payload["field"] == self._FIELD
        assert ev.payload["old_value"] == 500
        assert ev.payload["new_value"] == 200
        assert ev.payload["actor"] == "alice"
        assert ev.payload["reason"] == "release week"
        assert ev.payload["source"] == "web"
        assert ev.payload["generation"] == 1
        assert ev.payload["expires_at"] is None
        assert ev.timestamp == self._NOW.isoformat()
        assert ev.policy_hash is not None
        assert store.get_policy_snapshot(ev.policy_hash) is not None

    def test_the_event_is_emitted_before_the_store_write(self, store, monkeypatch):
        monkeypatch.setenv("KEW_ACTOR", "alice")
        cfg = self._cfg(approvers=["alice"])

        def boom(**_kwargs):
            raise RuntimeError("write failed")

        monkeypatch.setattr(store, "set_human_review_policy", boom)
        with pytest.raises(RuntimeError):
            self._set(store, cfg)
        assert len(store.list_audit_events(event="human_review_override_set")) == 1
        assert store.get_human_review_policy(self._REPO) is None

    # -- revert -----------------------------------------------------------

    def test_revert_restores_the_system_policy_and_audits_before_the_delete(
        self, store, monkeypatch
    ):
        monkeypatch.setenv("KEW_ACTOR", "alice")
        cfg = self._cfg(approvers=["alice"])
        self._set(store, cfg)

        state = revert_human_review_override(store, config=cfg, now=self._NOW)
        assert state.values[self._FIELD] == 500
        assert state.source == "system"
        assert store.get_human_review_policy(self._REPO) is None
        ours = store.list_audit_events(limit=1, event="human_review_override_reverted")
        assert ours[0].payload["actor"] == "alice"
        theirs = store.list_audit_events(event="human_review_policy_reverted")
        assert theirs[0].payload["to"]["max_diff_lines"] == 500

    def test_reverting_one_field_keeps_the_rest_of_the_override(
        self, store, monkeypatch
    ):
        monkeypatch.setenv("KEW_ACTOR", "alice")
        cfg = self._cfg(approvers=["alice"])
        self._set(store, cfg)
        self._set(store, cfg, field="human_review.max_paths", value=5)

        state = revert_human_review_override(store, field=self._FIELD, config=cfg)
        assert state.values[self._FIELD] == 500
        assert state.values["human_review.max_paths"] == 5
        assert state.source == "override"
        assert state.generation == 3

    def test_reverting_nothing_raises_not_found(self, store):
        with pytest.raises(OverrideNotFound):
            revert_human_review_override(store, config=self._cfg(), now=self._NOW)

    # -- occupancy --------------------------------------------------------

    def test_lowering_below_occupancy_evicts_nothing_and_renders_over_cap(
        self, store, monkeypatch
    ):
        monkeypatch.setenv("KEW_ACTOR", "alice")
        cfg = self._cfg(approvers=["alice"])
        held = [self._hold(store, cfg, "run-1"), self._hold(store, cfg, "run-2")]

        state = self._set(store, cfg, field="human_review.max_pending_reviews", value=1)
        assert state.over_cap is True
        assert state.pending_reviews == 2
        assert store.pending_review_count(self._REPO) == 2
        for job in held:
            assert store.publication_review(job.job_id).review_state == "pending"

    def test_at_or_under_the_cap_is_not_over_cap(self, store, monkeypatch):
        monkeypatch.setenv("KEW_ACTOR", "alice")
        cfg = self._cfg(approvers=["alice"])
        self._hold(store, cfg, "run-1")
        state = self._set(store, cfg, field="human_review.max_pending_reviews", value=1)
        assert state.over_cap is False
        assert state.pending_reviews == 1
