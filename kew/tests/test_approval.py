"""Tests for the approval gate: estimator, event log, and confirm prompt."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from kew.approval import (
    Estimate,
    confirm_dispatch,
    estimate_dispatch_cost,
    log_approval_event,
    remote_approve,
    remote_deny,
)
from kew.config import BudgetConfig, DispatchConfig
from kew.run_store import RunRecord, SqliteRunStore


class _FakeStore:
    def __init__(self):
        self.events = []

    def append_audit_event(self, **kw):
        self.events.append(kw)

    def list_audit_events(self, limit=None):
        return []


class _NullStore:
    def append_audit_event(self, **_):  # never reached on gh_error/not_held paths
        raise AssertionError("should not emit on gh_error/not_held")


def _config(approvers=None):
    c = DispatchConfig()
    c.project.repo = "o/r"
    if approvers is not None:
        c.approval.approvers = approvers
    return c


def test_remote_approve_releases_when_held(monkeypatch):
    # NOTE: open_approval_holds is imported lazily inside approval._hold_estimate,
    # so it must be patched at its source module (kew.report), not kew.approval.
    monkeypatch.setenv("KEW_ACTOR", "alice")
    store = _FakeStore()
    with (
        patch("kew.approval._fetch_issue_labels", return_value={"needs-approval"}),
        patch("kew.approval.release_approval_hold") as rel,
        patch("kew.report.open_approval_holds", return_value=[]),
    ):
        result = remote_approve(store, issue_num=42, reason="ship it", config=_config())
    assert result == "released"
    rel.assert_called_once()
    assert rel.call_args[0][0] == 42  # release_approval_hold(issue_num, config)
    assert store.events[0]["event"] == "approval_granted"
    assert store.events[0]["actor_id"] == "alice"
    assert store.events[0]["payload"]["source"] == "remote-comment"
    assert store.events[0]["payload"]["reason"] == "ship it"


def test_remote_approve_idempotent_when_not_held():
    store = _FakeStore()
    with (
        patch("kew.approval._fetch_issue_labels", return_value={"ready"}),
        patch("kew.approval.release_approval_hold") as rel,
    ):
        result = remote_approve(store, issue_num=42, reason="", config=_config())
    assert result == "not_held"
    rel.assert_not_called()
    assert store.events == []  # no duplicate audit event


def test_remote_approve_unauthorized_when_not_in_allowlist(monkeypatch):
    monkeypatch.setenv("KEW_ACTOR", "mallory")
    store = _FakeStore()
    with patch("kew.approval._fetch_issue_labels", return_value={"needs-approval"}):
        result = remote_approve(
            store, issue_num=42, reason="", config=_config(approvers=["alice"])
        )
    assert result == "unauthorized"
    assert store.events == []


def test_remote_deny_is_non_destructive(monkeypatch):
    monkeypatch.setenv("KEW_ACTOR", "alice")
    store = _FakeStore()
    with (
        patch("kew.approval._fetch_issue_labels", return_value={"needs-approval"}),
        patch("kew.approval._update_labels") as upd,
        patch("kew.report.open_approval_holds", return_value=[]),
    ):
        result = remote_deny(store, issue_num=42, reason="too costly", config=_config())
    assert result == "denied"
    assert store.events[0]["event"] == "approval_declined"
    assert store.events[0]["actor_id"] == "alice"
    # removes needs-approval, never adds ready, never closes:
    _, kwargs = upd.call_args
    assert kwargs["remove"] == ["needs-approval"]
    assert "add" not in kwargs or not kwargs.get("add")


def test_remote_deny_unauthorized_when_not_in_allowlist(monkeypatch):
    monkeypatch.setenv("KEW_ACTOR", "mallory")
    store = _FakeStore()
    with (
        patch("kew.approval._fetch_issue_labels", return_value={"needs-approval"}),
        patch("kew.approval._update_labels") as upd,
    ):
        result = remote_deny(
            store, issue_num=42, reason="", config=_config(approvers=["alice"])
        )
    assert result == "unauthorized"
    assert store.events == []
    upd.assert_not_called()


def test_remote_deny_idempotent_when_not_held():
    store = _FakeStore()
    with (
        patch("kew.approval._fetch_issue_labels", return_value={"ready"}),
        patch("kew.approval._update_labels") as upd,
    ):
        result = remote_deny(store, issue_num=42, reason="", config=_config())
    assert result == "not_held"
    assert store.events == []
    upd.assert_not_called()


def test_remote_approve_gh_error_on_fetch_failure(monkeypatch):
    monkeypatch.setenv(
        "KEW_ACTOR", "alice"
    )  # hermetic actor (resolve_actor runs first)
    store = _NullStore()
    with (
        patch("kew.approval._fetch_issue_labels", return_value=None),
        patch("kew.approval.release_approval_hold") as rel,
    ):
        result = remote_approve(store, issue_num=42, reason="", config=_config())
    assert result == "gh_error"
    rel.assert_not_called()


def test_remote_deny_gh_error_on_fetch_failure(monkeypatch):
    monkeypatch.setenv(
        "KEW_ACTOR", "alice"
    )  # hermetic actor (resolve_actor runs first)
    store = _NullStore()
    with (
        patch("kew.approval._fetch_issue_labels", return_value=None),
        patch("kew.approval._update_labels") as upd,
    ):
        result = remote_deny(store, issue_num=42, reason="", config=_config())
    assert result == "gh_error"
    upd.assert_not_called()


def test_remote_approve_gh_unreachable_returns_gh_error(monkeypatch):
    monkeypatch.setenv("KEW_ACTOR", "alice")
    with patch("kew.approval._fetch_issue_labels", return_value=None):
        assert (
            remote_approve(_NullStore(), issue_num=7, reason="", config=_config())
            == "gh_error"
        )


def test_remote_approve_label_absent_returns_not_held(monkeypatch):
    monkeypatch.setenv("KEW_ACTOR", "alice")
    with patch("kew.approval._fetch_issue_labels", return_value=set()):
        assert (
            remote_approve(_NullStore(), issue_num=7, reason="", config=_config())
            == "not_held"
        )


_BASE = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def store():
    """Yield an in-memory SqliteRunStore and close it after the test."""
    with SqliteRunStore(":memory:") as s:
        yield s


def _make_run(
    run_id: str,
    *,
    cost: float,
    model: str = "claude-sonnet-4-6",
    complexity: str = "medium",
    status: str = "success",
    issue_num: int = 1,
    started_at: datetime | None = None,
) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        issue_num=issue_num,
        branch=f"agent/issue-{issue_num}",
        status=status,  # type: ignore[arg-type]
        exit_code=0 if status == "success" else 1,
        started_at=started_at or _BASE,
        estimated_cost_usd=cost,
        model=model,
        complexity=complexity,
    )


def _seed(store: SqliteRunStore, costs: list[float], **kw) -> None:
    for i, cost in enumerate(costs):
        store.save(
            _make_run(f"r{i}", cost=cost, started_at=_BASE + timedelta(minutes=i), **kw)
        )


class TestEstimatorRichHistory:
    """Median of the last 10 non-pending runs matching complexity + model."""

    def test_median_of_matching_runs(self, store):
        _seed(store, [1.0, 2.0, 3.0, 4.0, 5.0])
        est = estimate_dispatch_cost(
            store,
            complexity="medium",
            model="claude-sonnet-4-6",
            budget=BudgetConfig(),
        )
        assert est.usd == pytest.approx(3.0)
        assert est.basis == "based on 5 similar runs (median $3.00)"

    def test_only_last_10_runs_used(self, store):
        # Two old expensive runs, then ten cheap ones: median must ignore the old.
        _seed(store, [100.0, 100.0] + [1.0] * 10)
        est = estimate_dispatch_cost(
            store,
            complexity="medium",
            model="claude-sonnet-4-6",
            budget=BudgetConfig(),
        )
        assert est.usd == pytest.approx(1.0)
        assert est.basis == "based on 10 similar runs (median $1.00)"

    def test_pending_runs_excluded(self, store):
        _seed(store, [2.0, 2.0, 2.0])
        store.save(
            _make_run(
                "pending-1",
                cost=99.0,
                status="pending",
                started_at=_BASE + timedelta(hours=1),
            )
        )
        est = estimate_dispatch_cost(
            store,
            complexity="medium",
            model="claude-sonnet-4-6",
            budget=BudgetConfig(),
        )
        assert est.usd == pytest.approx(2.0)

    def test_non_matching_model_excluded(self, store):
        _seed(store, [2.0, 2.0, 2.0])
        # Distinct run_ids — reusing _seed's r0..r2 would overwrite the sonnet runs
        for i, cost in enumerate([50.0, 50.0, 50.0]):
            store.save(
                _make_run(
                    f"op{i}",
                    cost=cost,
                    model="claude-opus-4-6",
                    started_at=_BASE + timedelta(hours=1, minutes=i),
                )
            )
        est = estimate_dispatch_cost(
            store,
            complexity="medium",
            model="claude-sonnet-4-6",
            budget=BudgetConfig(),
        )
        assert est.usd == pytest.approx(2.0)

    def test_non_matching_complexity_excluded(self, store):
        _seed(store, [2.0, 2.0, 2.0])
        # Different run_ids needed — reseed with distinct ids
        for i, cost in enumerate([50.0, 50.0, 50.0]):
            store.save(
                _make_run(
                    f"cx{i}",
                    cost=cost,
                    complexity="complex",
                    started_at=_BASE + timedelta(hours=1, minutes=i),
                )
            )
        est = estimate_dispatch_cost(
            store,
            complexity="medium",
            model="claude-sonnet-4-6",
            budget=BudgetConfig(),
        )
        assert est.usd == pytest.approx(2.0)

    def test_failure_runs_still_count(self, store):
        """Non-pending means failures count too — they cost real money."""
        _seed(store, [3.0, 3.0, 3.0], status="failure")
        est = estimate_dispatch_cost(
            store,
            complexity="medium",
            model="claude-sonnet-4-6",
            budget=BudgetConfig(),
        )
        assert est.usd == pytest.approx(3.0)


class TestEstimatorSparseHistory:
    """Fewer than 3 matches falls back to the per-issue budget cap."""

    def test_two_matches_falls_back_to_cap(self, store):
        _seed(store, [1.0, 1.0])
        est = estimate_dispatch_cost(
            store,
            complexity="medium",
            model="claude-sonnet-4-6",
            budget=BudgetConfig(per_issue_usd=15.0),
        )
        assert est.usd == pytest.approx(15.0)
        assert est.basis == "only 2 similar run(s) — assuming per-issue cap of $15.00"


class TestEstimatorNoHistory:
    def test_empty_store_falls_back_to_cap(self, store):
        est = estimate_dispatch_cost(
            store,
            complexity="medium",
            model="claude-sonnet-4-6",
            budget=BudgetConfig(per_issue_usd=15.0),
        )
        assert est.usd == pytest.approx(15.0)
        assert est.basis == "no history — assuming per-issue cap of $15.00"

    def test_none_store_falls_back_to_cap(self):
        est = estimate_dispatch_cost(
            None,
            complexity="medium",
            model="claude-sonnet-4-6",
            budget=BudgetConfig(per_issue_usd=12.0),
        )
        assert est.usd == pytest.approx(12.0)
        assert est.basis == "no history — assuming per-issue cap of $12.00"


class TestEstimatorModelOnlyMatching:
    """Empty complexity (e.g. review dispatches) matches on model only."""

    def test_empty_complexity_matches_any_complexity(self, store):
        for i, cx in enumerate(["simple", "medium", "complex"]):
            store.save(
                _make_run(
                    f"m{i}",
                    cost=4.0,
                    complexity=cx,
                    started_at=_BASE + timedelta(minutes=i),
                )
            )
        est = estimate_dispatch_cost(
            store,
            complexity="",
            model="claude-sonnet-4-6",
            budget=BudgetConfig(),
        )
        assert est.usd == pytest.approx(4.0)
        assert est.basis == "based on 3 similar runs (median $4.00)"


class TestLogApprovalEvent:
    """All four approval events land in audit_events with full context."""

    def test_writes_event_row_with_envelope(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KEW_ACTOR", "approver-amy")
        est = Estimate(usd=4.2, basis="based on 7 similar runs (median $4.20)")
        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            log_approval_event(
                store,
                event="approval_hold",
                issue_num=42,
                estimate=est,
                threshold_usd=3.0,
                repo="octo/widgets",
            )
            ev = store.list_audit_events(event="approval_hold")[0]
            assert ev.issue_num == 42
            assert ev.actor_type == "human"
            assert ev.actor_id == "approver-amy"  # actor-per-event
            assert ev.repo == "octo/widgets"
            # Payload parity with the legacy JSONL entry
            assert ev.payload["estimate_usd"] == pytest.approx(4.2)
            assert ev.payload["basis"] == "based on 7 similar runs (median $4.20)"
            assert ev.payload["threshold_usd"] == pytest.approx(3.0)
            assert ev.timestamp  # legacy "timestamp" field → column

    @pytest.mark.parametrize(
        "event",
        [
            "approval_hold",
            "approval_granted",
            "approval_confirmed",
            "approval_declined",
        ],
    )
    def test_all_four_event_types(self, tmp_path, event):
        est = Estimate(usd=1.0, basis="x")
        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            log_approval_event(
                store,
                event=event,
                issue_num=3,
                estimate=est,
                threshold_usd=0.5,
            )
            assert store.list_audit_events(event=event)[0].event == event

    def test_best_effort_never_raises(self):
        """A store failure must not affect the dispatch outcome."""

        class ExplodingStore:
            def append_audit_event(self, **kwargs):
                raise RuntimeError("disk full")

        est = Estimate(usd=1.0, basis="x")
        log_approval_event(
            ExplodingStore(),
            event="approval_hold",
            issue_num=3,
            estimate=est,
            threshold_usd=0.5,
        )

    def test_no_store_never_raises(self):
        est = Estimate(usd=1.0, basis="x")
        log_approval_event(
            None,
            event="approval_hold",
            issue_num=3,
            estimate=est,
            threshold_usd=0.5,
        )


class TestConfirmDispatch:
    """Interactive prompt surfaces estimate, basis, and threshold."""

    @patch("kew.approval.typer.confirm", return_value=True)
    def test_prompt_contains_estimate_basis_threshold(self, mock_confirm: MagicMock):
        est = Estimate(usd=4.2, basis="based on 7 similar runs (median $4.20)")
        assert confirm_dispatch(issue_num=42, estimate=est, threshold_usd=3.0) is True
        prompt = mock_confirm.call_args[0][0]
        assert "#42" in prompt
        assert "$4.20" in prompt
        assert "based on 7 similar runs (median $4.20)" in prompt
        assert "$3.00" in prompt

    @patch("kew.approval.typer.confirm", return_value=False)
    def test_returns_false_on_decline(self, mock_confirm: MagicMock):
        est = Estimate(usd=20.0, basis="no history — assuming per-issue cap of $15.00")
        assert confirm_dispatch(issue_num=7, estimate=est, threshold_usd=10.0) is False


def test_remote_approve_records_source(tmp_path):
    from unittest.mock import patch

    from kew.approval import remote_approve
    from kew.config import DispatchConfig, LoggingConfig, ProjectConfig
    from kew.run_store import SqliteRunStore

    config = DispatchConfig(
        project=ProjectConfig(repo="o/r"),
        logging=LoggingConfig(dir=str(tmp_path)),
    )
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        with (
            patch(
                "kew.approval._fetch_issue_labels",
                return_value=[config.approval.label],
            ),
            patch("kew.labels._update_labels"),
        ):
            result = remote_approve(
                store, issue_num=7, reason="", config=config, source="tui"
            )
        assert result == "released"
        events = store.list_audit_events(limit=None)
        granted = next(e for e in events if e.event == "approval_granted")
        assert (granted.payload or {})["source"] == "tui"


def test_remote_approve_source_defaults_to_remote_comment(tmp_path):
    from unittest.mock import patch

    from kew.approval import remote_approve
    from kew.config import DispatchConfig, LoggingConfig, ProjectConfig
    from kew.run_store import SqliteRunStore

    config = DispatchConfig(
        project=ProjectConfig(repo="o/r"),
        logging=LoggingConfig(dir=str(tmp_path)),
    )
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        with (
            patch(
                "kew.approval._fetch_issue_labels",
                return_value=[config.approval.label],
            ),
            patch("kew.labels._update_labels"),
        ):
            remote_approve(store, issue_num=7, reason="", config=config)
        granted = next(
            e
            for e in store.list_audit_events(limit=None)
            if e.event == "approval_granted"
        )
        assert (granted.payload or {})["source"] == "remote-comment"


def test_apply_approval_decision_binds_policy(tmp_path, monkeypatch):
    # the wrapper must emit a POLICY-BOUND approval event (the governance fix):
    # a non-null policy_hash proves bind_policy_context ran, like the CLI.
    from unittest.mock import patch

    from kew.approval import apply_approval_decision
    from kew.config import DispatchConfig, LoggingConfig, ProjectConfig
    from kew.run_store import SqliteRunStore

    config = DispatchConfig(
        project=ProjectConfig(repo="o/r"),
        logging=LoggingConfig(dir=str(tmp_path)),
    )
    monkeypatch.setenv("KEW_PROJECT", str(tmp_path))
    with (
        patch(
            "kew.approval._fetch_issue_labels",
            return_value=[config.approval.label],
        ),
        patch("kew.labels._update_labels"),
    ):
        result = apply_approval_decision(
            "approve", issue_num=7, reason="", config=config, source="tui"
        )
    assert result == "released"
    # re-open the wrapper's own db and inspect the bound event
    with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
        granted = next(
            e
            for e in store.list_audit_events(limit=None)
            if e.event == "approval_granted"
        )
        assert granted.policy_hash is not None
        assert (granted.payload or {})["source"] == "tui"
