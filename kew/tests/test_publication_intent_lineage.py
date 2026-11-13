"""Publication ownership follows one checkout generation and claimed lineage."""

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from support.checkout_generations import fixture_claim_evidence
from support.host_git_boundary import record_git_calls
from test_publication_round3 import _git
from test_publication_round3 import _git_on_allowlist as _git_on_allowlist
from test_publication_round3 import scene as scene

from kew import dispatch_core, worktree
from kew.audit import Actor, emit_continuation_evidence
from kew.config import DispatchConfig
from kew.publication import PublicationError
from kew.publication_canonical import CanonicalNoChange, CanonicalPublication
from kew.readiness import (
    Containment,
    RealizedLaunchBoundary,
    compose_realized_profile,
    compose_route_profile,
)
from kew.run_store import RunRecord, SqliteRunStore


def _record_native_profile(
    store, run_id, *, branch="agent/issue-7", item_id="7", run_kind="dispatch"
):
    store.save(
        RunRecord(
            run_id=run_id,
            issue_num=7,
            item_id=item_id,
            branch=branch,
            run_kind=run_kind,
            status="awaiting_publication",
            exit_code=0,
            started_at=datetime(2026, 9, 6, tzinfo=UTC),
            runner_name="claude-code",
            model="claude-opus-5",
        )
    )
    store.record_route_profile(
        compose_realized_profile(
            run_id=run_id,
            attempt_number=1,
            route=compose_route_profile(
                runner="claude-code", requested_model="claude-opus-5"
            ),
            boundary=RealizedLaunchBoundary(
                containment=Containment.NATIVE,
                auth_path="cli-login",
            ),
        )
    )


def _publish(scene, store, run_id, *, base=None):
    checkout = Path(scene["checkout"])
    _record_native_profile(store, run_id)
    with record_git_calls() as calls:
        try:
            return dispatch_core._git_push_branch(
                "agent/issue-7",
                checkout,
                repo=str(scene["origin"]),
                base_sha=base or str(scene["base"]),
                store=store,
                run_id=run_id,
                item_id="7",
                max_attempts=1,
            )
        finally:
            assert not [
                call for call in calls if call.repository().resolves_inside(checkout)
            ]


@pytest.mark.parametrize(
    "replacement", ["new_run", "new_generation", "both", "new_base"]
)
def test_stale_same_item_head_is_refused(scene, tmp_path, replacement):
    checkout = Path(scene["checkout"])
    worktree.write_checkout_generation(checkout)
    (checkout / "edit.txt").write_text("first run\n")
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        first = _publish(scene, store, "run-a")
        assert isinstance(first, CanonicalPublication)
        if replacement in {"new_generation", "both"}:
            worktree.revoke_checkout_generation(checkout)
            worktree.write_checkout_generation(checkout)
        (checkout / "edit.txt").write_text("unrelated work\n")
        run_id = "run-a" if replacement in {"new_generation", "new_base"} else "run-b"
        base = scene["base"]
        if replacement == "new_base":
            seed = Path(scene["seed"])
            (seed / "keep.txt").write_text("new base\n")
            _git(seed, "commit", "-qam", "new base")
            _git(seed, "push", "-q", "origin", "main")
            base = _git(seed, "rev-parse", "HEAD")
        with pytest.raises(PublicationError, match="stale branch"):
            _publish(scene, store, run_id, base=base)
        assert (
            _git(Path(scene["origin"]), "rev-parse", "agent/issue-7")
            == first.commit_sha
        )


def test_same_checkout_retry_keeps_its_publication(scene, tmp_path):
    checkout = Path(scene["checkout"])
    worktree.write_checkout_generation(checkout)
    (checkout / "edit.txt").write_text("first run\n")
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        first = _publish(scene, store, "run-a")
        retry = _publish(scene, store, "run-a")
        assert isinstance(retry, CanonicalNoChange)
        assert retry.base_sha == first.commit_sha
        assert (
            _git(Path(scene["origin"]), "rev-parse", "agent/issue-7")
            == first.commit_sha
        )


def _claim_original(scene, store, run_id="run-b"):
    checkout = Path(scene["checkout"])
    dispatch_core._mint_publication_continuation(
        store,
        epic_ref="epic",
        item_id="7",
        branch="agent/issue-7",
        issue_num=7,
        repo=str(scene["origin"]),
        publish="canonical",
        base=str(scene["base"]),
        run_id="run-a",
        worktree_dir=checkout,
    )
    claim = store.claim_continuation_under_cap(
        "epic",
        "7",
        "run-a",
        "agent/issue-7",
        checkout,
        run_id,
        2,
        claim_evidence=fixture_claim_evidence(
            store, "epic", "7", "run-a", "agent/issue-7", checkout, run_id
        ),
    )
    assert claim == "claimed"
    record = store.get_continuation("epic", "7", "run-a", "agent/issue-7", checkout)
    assert record is not None
    emit_continuation_evidence(
        store,
        "continuation_claimed",
        actor=Actor("policy", "test"),
        phase="claim",
        epic_ref=record.epic_ref,
        item_id=record.item_id,
        original_run_id=record.original_run_id,
        continuation_run_id=run_id,
        branch=record.branch,
        worktree_identity=record.worktree_identity,
        generation=record.generation,
        state=record.state,
        outcome="continuing",
        run_id=run_id,
        issue_num=7,
    )
    return SimpleNamespace(
        config=DispatchConfig(),
        store=store,
        epic_ref="epic",
        worktree_dir=checkout,
        run_id=run_id,
        branch="agent/issue-7",
        item_id="7",
    )


def test_claimed_continuation_owns_the_original_publication(scene, tmp_path):
    checkout = Path(scene["checkout"])
    worktree.write_checkout_generation(checkout)
    (checkout / "edit.txt").write_text("first run\n")
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        first = _publish(scene, store, "run-a")
        ctx = _claim_original(scene, store)
        base = dispatch_core._continuation_base(ctx)
        assert base == scene["base"]
        retry = _publish(scene, store, "run-b", base=base)
        assert isinstance(retry, CanonicalNoChange)
        assert retry.base_sha == first.commit_sha
        (checkout / "edit.txt").write_text("continuation work\n")
        second = _publish(scene, store, "run-b", base=base)
        assert isinstance(second, CanonicalPublication)
        record = store.list_continuations("epic")[0]
        assert store.mark_continuation_awaiting(
            "epic",
            "7",
            "run-a",
            "agent/issue-7",
            checkout,
            record.generation,
            "run-b",
        )
        third_ctx = _claim_original(scene, store, "run-c")
        third_base = dispatch_core._continuation_base(third_ctx)
        third = _publish(scene, store, "run-c", base=third_base)
        assert isinstance(third, CanonicalNoChange)
        assert third.base_sha == second.commit_sha


def test_lost_lineage_write_refuses_continuation(scene, tmp_path, monkeypatch):
    checkout = Path(scene["checkout"])
    worktree.write_checkout_generation(checkout)
    (checkout / "edit.txt").write_text("first run\n")
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        first = _publish(scene, store, "run-a")
        ctx = _claim_original(scene, store)
        monkeypatch.setattr(dispatch_core.audit, "emit", lambda *args, **kwargs: None)
        with pytest.raises(PublicationError, match="lineage was not durably recorded"):
            dispatch_core._continuation_base(ctx)
        with pytest.raises(PublicationError, match="stale branch"):
            _publish(scene, store, "run-b")
        assert (
            _git(Path(scene["origin"]), "rev-parse", "agent/issue-7")
            == first.commit_sha
        )


@pytest.mark.parametrize("item_id", ["SFD-258", None])
def test_publication_intent_round_trips_its_scope(tmp_path, monkeypatch, item_id):
    branch = "feature/standalone"
    scope = "item" if item_id else "branch"
    key = item_id or branch
    with SqliteRunStore(str(tmp_path / "scopes.db")) as store:
        dispatch_core._record_publication_intent(
            store,
            run_id="producer",
            item_id=item_id,
            branch=branch,
            commit_sha="a" * 40,
            tree_sha="b" * 40,
            parent_sha="c" * 40,
        )
        (event,) = store.list_audit_events(event="publication_intent", item_id=key)
        assert event.payload["scope"] == scope
        assert event.item_id == key
        assert event.run_id == "producer"
        # A lost append cannot reuse another producer's evidence.
        monkeypatch.setattr(dispatch_core.audit, "emit", lambda *a, **kw: None)
        with pytest.raises(PublicationError, match="not durably recorded"):
            dispatch_core._record_publication_intent(
                store,
                run_id="other",
                item_id=item_id,
                branch=branch,
                commit_sha="a" * 40,
                tree_sha="b" * 40,
                parent_sha="c" * 40,
            )
