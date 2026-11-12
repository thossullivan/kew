"""Tests for the foreground ``kew delegate watch`` assembly (D5, #924).

Two layers: the pure orchestration in :mod:`kew.delegate.run` (recognition →
shared fleet cap → claim/dispatch, plus the terminally-decided predicate), and
the ``kew delegate watch`` CLI command driving a fake watcher through one tick
to a real dispatch call — and to a no-op under ``--dry-run``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from kew.cli import app
from kew.cli.delegate import _WatchContext
from kew.config import (
    DelegationConfig,
    DispatchConfig,
    LoggingConfig,
    ParallelConfig,
    TrackerConfig,
)
from kew.delegate.claim import ClaimOutcome
from kew.delegate.recognize import CandidateIssue
from kew.delegate.run import (
    WatchOutcome,
    make_is_terminally_decided,
    process_candidate,
)
from kew.delegate.watch import Candidate
from kew.run_store import RunRecord, SqliteRunStore

runner = CliRunner()

MEMBER_ID = "11111111-2222-3333-4444-555555555555"
LABEL = "kew:ready"
EPIC_REF = "delegation"


@pytest.fixture(autouse=True)
def _isolated_ledger_project(tmp_path):
    """Stop registry discovery at the test project inside this worktree."""
    (tmp_path / ".git").mkdir()


def _generous_toml(tmp_path):
    toml = tmp_path / "kew.toml"
    toml.write_text(
        "[budget]\nper_issue_usd = 2000.0\ndaily_usd = 2000.0\nweekly_usd = 2000.0\n"
    )
    return toml


class _FakeTracker:
    """A DelegationTracker fake recording the live re-verify + write I/O."""

    def __init__(
        self, *, assignee_id=MEMBER_ID, state="Todo", labels=(), epic_admitted=False
    ):
        self._issue = CandidateIssue(
            assignee_id=assignee_id,
            labels=tuple(labels),
            state=state,
            state_id=state,
            epic_admitted=epic_admitted,
        )
        self.fetched: list[str] = []
        self.transitioned: list[str] = []
        self.comments: list[tuple[str, str]] = []

    def fetch_live(self, item_id: str) -> CandidateIssue:
        self.fetched.append(item_id)
        return self._issue

    def transition_in_progress(self, item_id: str) -> None:
        self.transitioned.append(item_id)

    def comment(self, item_id: str, body: str) -> None:
        self.comments.append((item_id, body))


class _FakeSink:
    def __init__(self) -> None:
        self.dispatched: list[str] = []

    def dispatch(self, item_id: str) -> bool:
        self.dispatched.append(item_id)
        return True


class _FakeWatcher:
    """Emits its candidates on the first tick, then raises KeyboardInterrupt
    (a simulated Ctrl-C) so the foreground loop exits cleanly."""

    def __init__(self, candidates):
        self._candidates = list(candidates)
        self.ticks = 0

    def tick(self):
        self.ticks += 1
        if self.ticks == 1:
            return list(self._candidates)
        raise KeyboardInterrupt


# --- process_candidate: recognition -----------------------------------------


def test_process_candidate_dispatches_recognized_issue(tmp_path):
    toml = _generous_toml(tmp_path)
    tracker, sink = _FakeTracker(), _FakeSink()
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        outcome = process_candidate(
            Candidate("SFD-1", "poll"),
            member_id=MEMBER_ID,
            label=LABEL,
            store=store,
            tracker=tracker,
            sink=sink,
            fleet_cap=1,
            dry_run=False,
            config_path=toml,
        )
    assert outcome is WatchOutcome.DISPATCHED
    assert sink.dispatched == ["SFD-1"]


def test_process_candidate_skips_unrecognized_issue(tmp_path):
    """A candidate whose LIVE assignee is not kew (and no label) is skipped."""
    toml = _generous_toml(tmp_path)
    tracker = _FakeTracker(assignee_id="99999999-aaaa-bbbb-cccc-dddddddddddd")
    sink = _FakeSink()
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        outcome = process_candidate(
            Candidate("SFD-2", "local"),
            member_id=MEMBER_ID,
            label=LABEL,
            store=store,
            tracker=tracker,
            sink=sink,
            fleet_cap=1,
            dry_run=False,
            config_path=toml,
        )
    assert outcome is WatchOutcome.SKIPPED_NOT_RECOGNIZED
    assert sink.dispatched == []
    assert tracker.transitioned == []


def test_process_candidate_skips_epic_admitted_item_assigned_to_kew(tmp_path):
    """Epic ownership wins at recognition, before reservation or transition."""
    toml = _generous_toml(tmp_path)
    tracker = _FakeTracker(
        labels=("kew", LABEL),
        epic_admitted=True,
    )
    sink = _FakeSink()
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        outcome = process_candidate(
            Candidate("SFD-epic", "poll"),
            member_id=MEMBER_ID,
            label=LABEL,
            store=store,
            tracker=tracker,
            sink=sink,
            fleet_cap=1,
            dry_run=False,
            config_path=toml,
        )
        assert store.list_epic_reservations(EPIC_REF) == []
    assert outcome is WatchOutcome.SKIPPED_NOT_RECOGNIZED
    assert sink.dispatched == []
    assert tracker.transitioned == []


# --- process_candidate: shared fleet cap ------------------------------------


def test_process_candidate_queues_when_shared_fleet_cap_full(tmp_path):
    """A delegated claim is refused/queued when the shared cap (#864) is full.

    Two existing reservations — one epic, one prior delegation — fill a cap of
    two, so the recognized candidate is queued (never reserved, never
    dispatched) and retried on the next tick.
    """
    toml = _generous_toml(tmp_path)
    tracker, sink = _FakeTracker(), _FakeSink()
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        assert store.reserve_epic_node_item("epic-alpha", "ALPHA-1", "r1")
        assert store.reserve_epic_node_item(EPIC_REF, "SFD-prior", "r2")
        assert store.live_run_count() == 2

        outcome = process_candidate(
            Candidate("SFD-9", "poll"),
            member_id=MEMBER_ID,
            label=LABEL,
            store=store,
            tracker=tracker,
            sink=sink,
            fleet_cap=2,
            dry_run=False,
            config_path=toml,
        )
        assert outcome is WatchOutcome.QUEUED_FLEET_FULL
        assert sink.dispatched == []
        # The queued item was never reserved, so it is claimable next tick.
        assert store.reserve_epic_node_item(EPIC_REF, "SFD-9", "later") is True


def test_process_candidate_admits_when_cap_has_a_free_slot(tmp_path):
    """One reservation under a cap of two leaves room for a delegated claim."""
    toml = _generous_toml(tmp_path)
    tracker, sink = _FakeTracker(), _FakeSink()
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        assert store.reserve_epic_node_item("epic-alpha", "ALPHA-1", "r1")
        outcome = process_candidate(
            Candidate("SFD-3", "poll"),
            member_id=MEMBER_ID,
            label=LABEL,
            store=store,
            tracker=tracker,
            sink=sink,
            fleet_cap=2,
            dry_run=False,
            config_path=toml,
        )
    assert outcome is WatchOutcome.DISPATCHED
    assert sink.dispatched == ["SFD-3"]


# --- process_candidate: dry-run ---------------------------------------------


def test_process_candidate_dry_run_is_a_no_op(tmp_path):
    """--dry-run logs intent without reserving or spending."""
    toml = _generous_toml(tmp_path)
    tracker, sink = _FakeTracker(), _FakeSink()
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        outcome = process_candidate(
            Candidate("SFD-5", "poll"),
            member_id=MEMBER_ID,
            label=LABEL,
            store=store,
            tracker=tracker,
            sink=sink,
            fleet_cap=1,
            dry_run=True,
            config_path=toml,
        )
        assert outcome is WatchOutcome.DRY_RUN
        # No dispatch, no transition, and — crucially — no reservation held.
        assert sink.dispatched == []
        assert tracker.transitioned == []
        assert store.reserve_epic_node_item(EPIC_REF, "SFD-5", "later") is True


def test_process_candidate_maps_lost_claim_outcome(tmp_path):
    """A losing claim from the D4 seam surfaces as SKIPPED_LOST_CLAIM."""
    toml = _generous_toml(tmp_path)
    tracker, sink = _FakeTracker(), _FakeSink()

    def _fake_claim(item_id, **_kwargs):
        return ClaimOutcome.SKIPPED_LOST_CLAIM

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        outcome = process_candidate(
            Candidate("SFD-6", "local"),
            member_id=MEMBER_ID,
            label=LABEL,
            store=store,
            tracker=tracker,
            sink=sink,
            fleet_cap=1,
            dry_run=False,
            config_path=toml,
            claim=_fake_claim,
        )
    assert outcome is WatchOutcome.SKIPPED_LOST_CLAIM


# --- make_is_terminally_decided ---------------------------------------------


def test_is_terminally_decided_tracks_reservation_and_runs(tmp_path):
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        decided = make_is_terminally_decided(store, EPIC_REF)
        assert decided("SFD-unknown") is False
        # A held reservation marks the item decided.
        store.reserve_epic_node_item(EPIC_REF, "SFD-10", "r1")
        assert decided("SFD-10") is True
        # A run row (e.g. a terminal refusal) also marks it decided.
        store.save(
            RunRecord(
                run_id="run-x",
                issue_num=0,
                branch="agent/issue-0",
                status="refused",
                exit_code=0,
                started_at=datetime.now(UTC),
                item_id="SFD-11",
            )
        )
        assert decided("SFD-11") is True


# --- CLI: kew delegate watch -------------------------------------------------


def _linear_config(tmp_path, *, assignee="kew", max_workers=1):
    # This temporary project must own its ledger, even inside another worktree.
    (tmp_path / ".git").mkdir(exist_ok=True)
    return DispatchConfig(
        tracker=TrackerConfig(type="linear"),
        delegation=DelegationConfig(assignee=assignee),
        parallel=ParallelConfig(max_workers=max_workers),
        logging=LoggingConfig(dir=str(tmp_path / "logs")),
    )


def test_watch_drives_one_tick_to_dispatch(tmp_path, monkeypatch):
    """One tick assembles watcher → recognize → claim → dispatch, then Ctrl-C."""
    # Main's autouse _isolated_ledger_project already created it.
    (tmp_path / ".git").mkdir(exist_ok=True)  # Bound ledger discovery here.
    monkeypatch.setenv("KEW_PROJECT", str(tmp_path))
    _generous_toml(tmp_path)
    monkeypatch.setattr("time.sleep", lambda _s: None)

    tracker, sink = _FakeTracker(), _FakeSink()
    watcher = _FakeWatcher([Candidate("SFD-1", "poll")])
    cfg = _linear_config(tmp_path)

    def _fake_ctx(config, store):
        return _WatchContext(
            watcher=watcher, tracker=tracker, sink=sink, member_id=MEMBER_ID
        )

    with (
        patch("kew.cli.load_config", return_value=cfg),
        patch("kew.cli.delegate._build_watch_context", side_effect=_fake_ctx),
    ):
        result = runner.invoke(app, ["delegate", "watch"])

    assert result.exit_code == 0, result.output
    assert sink.dispatched == ["SFD-1"]
    assert tracker.transitioned == []
    assert "stopped" in result.output
    assert watcher.ticks == 2  # first tick emitted, second raised Ctrl-C


def test_watch_dry_run_dispatches_nothing(tmp_path, monkeypatch):
    # Main's autouse _isolated_ledger_project already created it.
    (tmp_path / ".git").mkdir(exist_ok=True)  # Bound ledger discovery here.
    monkeypatch.setenv("KEW_PROJECT", str(tmp_path))
    _generous_toml(tmp_path)
    monkeypatch.setattr("time.sleep", lambda _s: None)

    tracker, sink = _FakeTracker(), _FakeSink()
    watcher = _FakeWatcher([Candidate("SFD-1", "poll")])
    cfg = _linear_config(tmp_path)

    def _fake_ctx(config, store):
        return _WatchContext(
            watcher=watcher, tracker=tracker, sink=sink, member_id=MEMBER_ID
        )

    with (
        patch("kew.cli.load_config", return_value=cfg),
        patch("kew.cli.delegate._build_watch_context", side_effect=_fake_ctx),
    ):
        result = runner.invoke(app, ["delegate", "watch", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert sink.dispatched == []
    assert tracker.transitioned == []
    assert "DRY RUN" in result.output


def test_watch_refuses_non_linear_tracker(tmp_path):
    cfg = DispatchConfig(logging=LoggingConfig(dir=str(tmp_path / "logs")))
    with patch("kew.cli.load_config", return_value=cfg):
        result = runner.invoke(app, ["delegate", "watch"])
    assert result.exit_code == 2
    assert "linear" in result.output.lower()


def test_watch_refuses_missing_assignee(tmp_path):
    cfg = _linear_config(tmp_path, assignee="")
    with patch("kew.cli.load_config", return_value=cfg):
        result = runner.invoke(app, ["delegate", "watch"])
    assert result.exit_code == 2
    assert "assignee" in result.output.lower()


def _round6_seed_keys(store, monkeypatch, config):
    from unittest.mock import Mock

    from kew import credential_issuer
    from kew.graph.driver.credentials import issue_driver_key

    monkeypatch.setenv("OPENROUTER_PROVISIONING_KEY", "management-secret-1944")
    monkeypatch.setattr(
        credential_issuer,
        "mint_bounded_run_key",
        lambda run_id, limit, expiry, *, ledger_id, on_malformed: (
            credential_issuer.BoundedRunKey(
                "bounded-secret-1944", run_id[0] * 64, limit, expiry.isoformat()
            )
        ),
    )
    revoke = Mock()
    monkeypatch.setattr(credential_issuer, "revoke_run_key", revoke)
    for run_id in ("a-terminal", "b-running", "c-exit"):
        issue_driver_key(store, config, run_id, "SFD-1944")
        store.save(
            RunRecord(
                run_id=run_id,
                issue_num=0,
                item_id="SFD-1944",
                branch="agent/test",
                status="completed" if run_id == "a-terminal" else "pending",
                exit_code=0 if run_id == "a-terminal" else -1,
                started_at=datetime.now(UTC),
            )
        )
    return revoke


def test_round6_watcher_reconciles_terminal_keys_each_pass(tmp_path, monkeypatch):
    from unittest.mock import Mock

    import kew.cli.delegate as delegate

    config = _linear_config(tmp_path)
    monkeypatch.setattr("kew.cli.load_config", lambda **kwargs: config)
    seen = {}

    def build_context(config, store):
        seen["store"] = store
        seen["revoke"] = _round6_seed_keys(store, monkeypatch, config)
        return _WatchContext(
            watcher=Mock(),
            tracker=_FakeTracker(),
            sink=_FakeSink(),
            member_id=MEMBER_ID,
        )

    def watch_loop(**kwargs):
        kwargs["reconcile"]()
        seen["revoke"].assert_called_once_with("a" * 64, issued_key="")
        kwargs["reconcile"]()
        assert seen["revoke"].call_count == 1
        assert seen["store"].get("b-running").status == "pending"
        raise KeyboardInterrupt

    monkeypatch.setattr(delegate, "_build_watch_context", build_context)
    monkeypatch.setattr(delegate, "run_watch_loop", watch_loop)
    result = runner.invoke(app, ["delegate", "watch"])
    assert result.exit_code == 0, result.exception
    seen["revoke"].assert_called_once_with("a" * 64, issued_key="")


@pytest.mark.parametrize("exit_error", [None, KeyboardInterrupt, RuntimeError])
def test_round6_watcher_exit_revokes_newly_terminal_keys(
    tmp_path, monkeypatch, exit_error
):
    from unittest.mock import Mock, call

    import kew.cli.delegate as delegate

    config = _linear_config(tmp_path)
    monkeypatch.setattr("kew.cli.load_config", lambda **kwargs: config)
    seen = {}

    def build_context(config, store):
        seen["store"] = store
        seen["revoke"] = _round6_seed_keys(store, monkeypatch, config)
        return _WatchContext(
            watcher=Mock(),
            tracker=_FakeTracker(),
            sink=_FakeSink(),
            member_id=MEMBER_ID,
        )

    def watch_loop(**kwargs):
        kwargs["reconcile"]()
        run = seen["store"].get("c-exit")
        run.status = "completed"
        run.exit_code = 0
        seen["store"].save(run)
        if exit_error is not None:
            raise exit_error()

    monkeypatch.setattr(delegate, "_build_watch_context", build_context)
    monkeypatch.setattr(delegate, "run_watch_loop", watch_loop)
    result = runner.invoke(app, ["delegate", "watch"])
    assert result.exit_code == (1 if exit_error is RuntimeError else 0), (
        result.exception
    )
    assert seen["revoke"].call_args_list == [
        call("a" * 64, issued_key=""),
        call("c" * 64, issued_key=""),
    ]


def test_round6_watcher_retries_failed_revoke_once_per_pass(tmp_path, monkeypatch):
    from unittest.mock import Mock

    import kew.cli.delegate as delegate
    from kew import credential_issuer

    config = _linear_config(tmp_path)
    monkeypatch.setattr("kew.cli.load_config", lambda **kwargs: config)
    seen = {}

    def build_context(config, store):
        seen["store"] = store
        seen["revoke"] = _round6_seed_keys(store, monkeypatch, config)
        seen["revoke"].side_effect = credential_issuer.IssuerUnavailable()
        return _WatchContext(
            watcher=Mock(),
            tracker=_FakeTracker(),
            sink=_FakeSink(),
            member_id=MEMBER_ID,
        )

    def watch_loop(**kwargs):
        kwargs["reconcile"]()
        assert seen["revoke"].call_count == 1
        kwargs["reconcile"]()
        assert seen["revoke"].call_count == 1
        seen["store"]._conn.execute(
            "UPDATE meta SET value=json_set(value, '$.next_attempt_at', "
            "'2000-01-01T00:00:00+00:00') WHERE key LIKE 'run_key_revocation:%'"
        )
        seen["store"]._conn.commit()
        kwargs["reconcile"]()
        assert seen["revoke"].call_count == 2
        raise KeyboardInterrupt

    monkeypatch.setattr(delegate, "_build_watch_context", build_context)
    monkeypatch.setattr(delegate, "run_watch_loop", watch_loop)
    result = runner.invoke(app, ["delegate", "watch"])
    assert result.exit_code == 0, result.exception
    assert seen["revoke"].call_count == 2
