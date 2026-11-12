"""Regressions for checkout owners and abandonment authority."""

import json
import os
import shutil
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from support.checkout_generations import (
    fixture_checkout_generation,
    fixture_claim_evidence,
)
from test_dispatch_core import TestInitialCanonicalCheckoutBinding as CheckoutBinding
from test_run_store_epic import _legacy_gap, _recovery_claim, _recovery_family

from kew import continuation_recovery, dispatch_core, run_liveness, worktree
from kew.audit import Actor, emit
from kew.config import DispatchConfig
from kew.continuation_recovery import checkout_abandonment, current_continuation
from kew.graph.driver.dispatch import resume_continuation
from kew.graph.driver.tick import _register_preexisting_dirty_worktree
from kew.overlap import DispatchOutcome
from kew.run_store import RunRecord, SqliteRunStore
from kew.service import verbs


def _round14_case_alias(directory):
    probe = directory.parent / "round14-case-probe"
    alias_probe = directory.parent / "ROUND14-CASE-PROBE"
    probe.touch()
    alias_probe.touch()
    if not os.path.samefile(probe, alias_probe):
        pytest.skip("filesystem is case-sensitive: case probes have different inodes")
    alias = directory.with_name(directory.name.upper())
    assert str(alias.resolve()) != str(directory.resolve())
    assert os.path.samefile(directory, alias)
    return alias


def _round14_checkout_snapshot(checkout):
    if not checkout.exists():
        return None
    return checkout.stat(), (checkout / "edits.txt").read_bytes()


@pytest.fixture
def store(tmp_path):
    with SqliteRunStore(str(tmp_path / "runs.db")) as value:
        try:
            yield value
        finally:
            worktree.release_checkout_claims()


def _owner(store, run_id="original", **kwargs):
    run = RunRecord(
        run_id=run_id,
        issue_num=42,
        item_id="42",
        branch="agent/issue-42",
        status="failure",
        exit_code=1,
        started_at=datetime.now(UTC),
        **kwargs,
    )
    store.save(run)
    return run


def _abandon(store, config=None):
    config = config or DispatchConfig()
    verbs.epic_abandon(
        store,
        epic_ref="epic:x",
        item_id="42",
        reason="replace stopped work",
        config=config,
        checkout_guard=worktree.checkout_claim_guard,
    )


@pytest.mark.parametrize(
    "status,category",
    [
        ("killed", "killed"),
        ("killed", "orphaned_pending_reaped"),
        ("killed", "orphaned_pending_preserved"),
        ("failure", "dispatch_child_died"),
    ],
)
def test_external_terminal_owner_requires_matching_dispatch_exit(
    store, tmp_path, monkeypatch, status, category
):
    args = _recovery_family(store, tmp_path)
    owner = _owner(store, attempt_number=2)
    store.save(replace(owner, status=status, error_category=category))
    record = store.get_continuation(*args)
    config = DispatchConfig()
    assert (
        run_liveness.continuation_owner_death_refusal(store, record, config) is not None
    )
    reason = run_liveness.continuation_owner_death_refusal(store, record, config)
    assert "original" in reason
    assert "no recorded worker identity" in reason
    assert "attempt 2" in reason
    for run_id, payload, timestamp in (
        ("other-run", {"attempt_number": 2, "exit_code": 1}, None),
        ("original", {"attempt_number": 1, "exit_code": 1}, None),
        ("original", {"attempt_number": 3, "exit_code": 1}, None),
        ("original", {"attempt_number": "2", "exit_code": 1}, None),
        ("original", {"attempt_number": 2, "exit_code": None}, None),
        ("original", {"attempt_number": 2, "exit_code": True}, None),
        (
            "original",
            {"attempt_number": 2, "exit_code": 1},
            owner.started_at - timedelta(seconds=1),
        ),
    ):
        store.append_audit_event(
            event="dispatch_completed",
            actor_type="policy",
            actor_id="dispatch",
            run_id=run_id,
            payload=payload,
            timestamp=(timestamp or datetime.now(UTC)).isoformat(),
        )
        assert (
            run_liveness.continuation_owner_death_refusal(store, record, config)
            is not None
        )
    emit(
        store,
        "dispatch_completed",
        actor=Actor("policy", "dispatch"),
        run_id=owner.run_id,
        payload={"attempt_number": 2, "exit_code": 1},
    )
    assert run_liveness.continuation_owner_death_refusal(store, record, config) is None
    store.set_pgid(owner.run_id, 12345)
    monkeypatch.setattr("kew.process_groups.owner_alive", lambda *a: True)
    assert (
        run_liveness.continuation_owner_death_refusal(store, record, config) is not None
    )


def test_release_only_owner_is_probed_and_missing_claim_refuses(
    store, tmp_path, monkeypatch, caplog
):
    args = _recovery_family(store, tmp_path)
    _owner(store)
    _recovery_claim(store, args, "legacy", attest=False)
    _owner(store, "legacy", pgid=12345)
    assert store.mark_continuation_awaiting(*args, 1, "legacy")
    before = (store.get_continuation(*args), store.read_chain_rows())
    probes = []

    def probe(pgid, *owner_identity):
        assert args[-1].absolute() in worktree._checkout_claims
        probes.append(pgid)
        return True

    monkeypatch.setattr("kew.process_groups.owner_alive", probe)
    with pytest.raises(
        verbs.EpicAbandonDenied, match="generation 1.*release.*no claim"
    ):
        _abandon(store)
    assert probes == [12345]
    assert "generation 1" in caplog.text
    assert before == (store.get_continuation(*args), store.read_chain_rows())
    monkeypatch.setattr("kew.process_groups.owner_alive", lambda *a: False)
    with pytest.raises(
        verbs.EpicAbandonDenied, match="generation 1.*release.*no claim"
    ):
        _abandon(store)


def test_owner_generation_count_gap_names_missing_generation(
    store, tmp_path, monkeypatch
):
    args = _recovery_family(store, tmp_path)
    _owner(store)
    _legacy_gap(store, args, monkeypatch)
    before = (store.get_continuation(*args), store.read_chain_rows())
    with pytest.raises(verbs.EpicAbandonDenied, match="generation 1.*claim count 0"):
        _abandon(store)
    assert before == (store.get_continuation(*args), store.read_chain_rows())


@pytest.mark.parametrize("identity", ["pgid", "container", "checkout"])
def test_retained_owners_block_until_every_owner_is_dead(
    store, tmp_path, monkeypatch, identity
):
    args = _recovery_family(store, tmp_path)
    _owner(store)
    for generation in (1, 2):
        run_id = f"claimed-{generation}"
        _recovery_claim(store, args, run_id)
        _owner(store, run_id, pgid=100 + generation)
        assert store.mark_continuation_awaiting(*args, generation, run_id)
    kwargs = {"pgid": 12345} if identity == "pgid" else {}
    if identity == "container":
        kwargs = {"container_name": "retained-container"}
    retained = _owner(store, "retained", **kwargs)
    if identity == "checkout":
        # Runs store checkout identity in publication evidence, not a path column.
        store.save(replace(retained, status="pending"))
        emit(
            store,
            "publication_base",
            actor=Actor("policy", "test"),
            run_id=retained.run_id,
            item_id="42",
            payload={"checkout": str(args[-1]), "branch": retained.branch},
        )
    probes = []

    def probe(pgid, *owner_identity):
        assert args[-1].absolute() in worktree._checkout_claims
        probes.append(pgid)
        return pgid not in {101, 102}

    monkeypatch.setattr("kew.process_groups.owner_alive", probe)
    monkeypatch.setattr("kew.process_termination.attempt_is_dead", lambda _: True)
    monkeypatch.setattr(
        "kew.stop_recovery.record_container_state", lambda *a, **k: "running"
    )
    with pytest.raises(verbs.EpicAbandonDenied, match="live or unproven owner"):
        _abandon(store)
    assert set(probes) == {101, 102, 12345 if identity == "pgid" else 0}
    before = store.read_chain_rows()
    monkeypatch.setattr("kew.process_groups.owner_alive", lambda *a: False)
    monkeypatch.setattr(
        "kew.stop_recovery.record_container_state", lambda *a, **k: "stopped"
    )
    _abandon(store)
    assert store.get_continuation(*args).state == "terminal"
    assert store.read_chain_rows()[: len(before)] == before


def test_replacement_continuation_base_uses_current_original(store, tmp_path):
    config = DispatchConfig()
    checkout = tmp_path / "preserved"
    checkout.mkdir()
    _owner(store)
    args = ("epic:x", "42", "original", "agent/issue-42", checkout)
    store.record_continuation(
        *args,
        checkout_generation=fixture_checkout_generation(args[4]),
    )
    (checkout / ".git").mkdir(exist_ok=True)
    worktree.write_checkout_generation(checkout)
    dispatch_core._bind_publication_base(
        store,
        publish="canonical",
        base="a" * 40,
        run_id="original",
        issue_num=42,
        item_id="42",
        branch=args[3],
        checkout=checkout,
    )
    _abandon(store)
    abandoned = store.get_continuation(*args)
    old_evidence = store.read_chain_rows()
    worktree.write_checkout_generation(checkout)
    _owner(store, "replacement")
    replacement_args = (*args[:2], "replacement", *args[3:])
    store.record_continuation(
        *replacement_args,
        checkout_generation=fixture_checkout_generation(replacement_args[4]),
    )
    dispatch_core._bind_publication_base(
        store,
        publish="canonical",
        base="b" * 40,
        run_id="replacement",
        issue_num=42,
        item_id="42",
        branch=args[3],
        checkout=checkout,
    )
    claimed = verbs.epic_resume(store, epic_ref="epic:x", item_id="42", config=config)
    ctx = CheckoutBinding._context(
        dispatch_core, config, store, run_id=claimed.continuation_run_id
    )
    ctx.epic_ref = "epic:x"
    ctx.worktree_dir = checkout
    assert dispatch_core._continuation_base(ctx) == "b" * 40
    assert store.get_continuation(*args) == abandoned
    assert store.continuation_was_abandoned(abandoned)
    assert store.read_chain_rows()[: len(old_evidence)] == old_evidence


def test_continuation_base_refuses_a_requeued_generation(store, tmp_path, monkeypatch):
    config = DispatchConfig()
    config.publication.publish = "checkout"
    args = _recovery_family(store, tmp_path)
    _owner(store)
    first = _recovery_claim(store, args, "first")
    ctx = CheckoutBinding._context(dispatch_core, config, store, run_id="first")
    ctx.epic_ref = "epic:x"
    ctx.worktree_dir = args[-1]

    def requeued(epic_ref):
        assert epic_ref == "epic:x"
        assert store.mark_continuation_awaiting(*args, first.generation, "first")
        _recovery_claim(store, args, "second")
        return [first]

    monkeypatch.setattr(store, "list_continuations", requeued)
    with pytest.raises(dispatch_core.PublicationError, match="claim identity changed"):
        dispatch_core._continuation_base(ctx)


@pytest.mark.parametrize(
    "invalid",
    [
        "other-path",
        "stale-generation",
        "wrong-original",
        "no-authority",
        "no-checkout-generation",
    ],
)
def test_checkout_replacement_requires_exact_abandonment_binding(
    store, tmp_path, monkeypatch, invalid
):
    config = DispatchConfig()
    config.branches.worktree_base = str(tmp_path / "worktrees")
    checkout = tmp_path / "worktrees" / "agent" / "issue-42"
    checkout.mkdir(parents=True)
    (checkout / "edits").write_text("abandoned bytes")
    args = ("epic:x", "42", "original", "agent/issue-42", checkout)
    store.record_continuation(
        *args,
        checkout_generation=fixture_checkout_generation(args[4]),
    )
    _owner(store)
    _recovery_claim(store, args, "first")
    _owner(store, "first")
    assert store.mark_continuation_awaiting(*args, 1, "first")
    _abandon(store, config)
    abandoned = store.get_continuation(*args)
    replacement = _owner(store, "replacement")
    payload = {
        "epic_ref": "epic:x",
        "item_id": "42",
        "original_run_id": "original",
        "branch": args[3],
        "checkout": str(checkout),
        "checkout_id": "fresh-token",
        "generation": abandoned.generation,
        "abandonment_identity": store.continuation_abandonment_identity(abandoned),
    }
    bad = dict(payload)
    changes = {
        "other-path": ("checkout", str(tmp_path / "sandbox-checkout")),
        "stale-generation": ("generation", abandoned.generation - 1),
        "wrong-original": ("original_run_id", "another-original"),
        "no-authority": ("abandonment_identity", None),
        "no-checkout-generation": ("checkout_id", None),
    }
    field, value = changes[invalid]
    bad[field] = value
    emit(
        store,
        "checkout_replaced",
        actor=Actor("policy", "dispatch"),
        run_id=replacement.run_id,
        item_id="42",
        payload=bad,
    )
    assert current_continuation(store, abandoned, "42")
    monkeypatch.setattr(
        "kew.graph.driver.tick._validate_preexisting_worktree", lambda *args: True
    )
    monkeypatch.setattr(
        "kew.graph.driver.tick._register_dirty_preexisting_worktree",
        lambda *args, **kwargs: pytest.fail("abandoned checkout was registered again"),
    )
    assert (
        _register_preexisting_dirty_worktree(
            store,
            config,
            epic_ref="epic:x",
            item_id="42",
            provider="github",
            reservation_run_id="admission",
            liveness=None,
        )
        == "not_registered"
    )
    assert (checkout / "edits").read_text() == "abandoned bytes"
    emit(
        store,
        "checkout_replaced",
        actor=Actor("policy", "dispatch"),
        run_id=replacement.run_id,
        item_id="42",
        payload=payload,
    )
    assert not current_continuation(store, abandoned, "42")
    assert store.continuation_was_abandoned(store.get_continuation(*args))


def test_production_replacement_event_binds_abandoned_generation(store, tmp_path):
    config = DispatchConfig()
    checkout = tmp_path / "preserved"
    (checkout / ".git").mkdir(parents=True)
    args = ("epic:x", "42", "original", "agent/issue-42", checkout)
    store.record_continuation(
        *args,
        checkout_generation=fixture_checkout_generation(args[4]),
    )
    _owner(store)
    _abandon(store)
    authority = _capture_abandonment(
        store, epic_ref="epic:x", item_id="42", branch=args[3], checkout=checkout
    )
    _owner(store, "replacement")
    ctx = CheckoutBinding._context(dispatch_core, config, store, run_id="replacement")
    ctx.epic_ref = "epic:x"
    ctx.checkout_abandonment = authority
    dispatch_core._record_checkout_replacement(ctx, checkout, "new-checkout-token")
    payload = store.list_audit_events(event="checkout_replaced")[0].payload
    assert payload.get("original_run_id") == "original"
    assert payload.get("generation") == 0
    assert not current_continuation(store, store.get_continuation(*args), "42")


@pytest.fixture
def native_abandonment(store, tmp_path, monkeypatch):
    import shutil

    from support.driver_fakes import EPIC
    from test_driver_dispatch import _abandoned_dispatch_fixture
    from test_worktree import _git_out, _origin_repo_with_branch

    from kew.subprocess_utils import TOOL_PATHS

    monkeypatch.setitem(TOOL_PATHS, "git", shutil.which("git"))
    repo = _origin_repo_with_branch(tmp_path, "agent/issue-42")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(worktree, "_repo_root", lambda: repo)
    config = DispatchConfig()
    config.branches.base = "main"
    config.branches.worktree_base = str(tmp_path / "worktrees")
    worktree.setup_worktree(issue_num=42, config=config)
    checkout, kwargs = _abandoned_dispatch_fixture(store, tmp_path, monkeypatch)
    kwargs["config"] = config
    ctx = CheckoutBinding._context(dispatch_core, config, store, run_id="replacement")
    ctx.epic_ref = EPIC
    ctx.publication_base_sha = _git_out("rev-parse", "main", cwd=repo)
    monkeypatch.setattr(
        dispatch_core, "publication_remote", lambda _: str(tmp_path / "origin.git")
    )
    monkeypatch.setattr(dispatch_core, "mark_failed", lambda *a, **k: None)
    try:
        yield checkout, kwargs, ctx
    finally:
        worktree.release_checkout_claims()


def _replace_native_checkout(ctx):
    from kew.sandbox import SandboxDecision

    checkout = dispatch_core._setup_host_worktree(
        ctx,
        ctx.config,
        "codex",
        SandboxDecision(sandboxed=False, runtime=None, posture="native", notice=None),
    )
    dispatch_core._bind_initial_checkout(ctx, checkout)
    return checkout


@pytest.mark.serial
@pytest.mark.parametrize("replacing_epic", [None, "epic:other"])
def test_replacement_outside_abandoning_epic_registers_new_work(
    store, native_abandonment, replacing_epic
):
    from support.driver_fakes import EPIC, _advance

    checkout, kwargs, ctx = native_abandonment
    abandoned = store.list_continuations(EPIC)[0]
    old_token = worktree.read_checkout_generation(checkout)
    # An unrelated direct dispatch must not consume this checkout's authority.
    unrelated = replace(
        ctx,
        epic_ref=None,
        item_id="43",
        issue_num=43,
        branch="agent/issue-43",
        run_id="unrelated",
    )
    _replace_native_checkout(unrelated)
    assert store.continuation_expiry(abandoned) is None
    ctx.epic_ref = replacing_epic
    assert _replace_native_checkout(ctx) == checkout
    expiry = store.continuation_expiry(abandoned)
    assert expiry is not None
    event = store.list_audit_events(event="checkout_replaced", run_id=ctx.run_id)[0]
    assert event.payload["epic_ref"] == EPIC
    assert event.payload["replacing_epic_ref"] == replacing_epic
    assert event.payload["abandoned_checkout_id"] == old_token
    assert event.payload["generation"] == abandoned.generation
    assert event.payload["abandonment_identity"] == expiry.abandonment_identity
    (checkout / "edits.txt").write_text("replacement worker edits")
    _owner(store, ctx.run_id, session_id="executed-session")
    worktree.release_checkout_claims()
    preserved = list(checkout.parent.glob("issue-42.replaced-*"))
    assert len(preserved) == 1
    assert (preserved[0] / "edits.txt").read_text() == "abandoned work"
    store.mark_epic_retry_item(EPIC, "42")
    tick = _advance(**kwargs)
    assert tick.dispatched == ()
    active = [
        r
        for r in store.list_continuations(EPIC)
        if current_continuation(store, r, "42")
    ]
    assert len(active) == 1, tick
    assert active[0].original_run_id == ctx.run_id
    assert active[0].state == "awaiting_continuation"
    assert list(checkout.parent.glob("issue-42.replaced-*")) == preserved
    assert (checkout / "edits.txt").read_text() == "replacement worker edits"


def _refuse_bound_replacement(ctx, checkout, refusal):
    from kew.budget import BudgetExceeded
    from kew.inference_transport import TransportAdmissionError
    from kew.sandbox import SandboxRuntimeUnavailable

    early = dispatch_core._EarlyRunInfo(runner="codex")
    if refusal == "cap":
        return dispatch_core._launch_over_cap(ctx, checkout, False)
    if refusal == "checkout":
        # The fifth pre-spawn refusal handler: a checkout-identity refusal
        # raised after the replacement checkout was bound.
        return dispatch_core._launch_refused_checkout(
            ctx,
            checkout,
            False,
            dispatch_core._CheckoutIdentityRefused(
                "checkout_identity_refused", "replacement checkout identity refused"
            ),
        )
    if refusal == "budget":
        return dispatch_core._launch_lost_budget(
            ctx, checkout, False, BudgetExceeded("daily", 1, 2, MagicMock())
        )
    if refusal == "containment":
        return dispatch_core._launch_refused_containment(
            ctx, checkout, False, SandboxRuntimeUnavailable("unavailable"), early
        )
    return dispatch_core._launch_refused_transport(
        ctx,
        checkout,
        False,
        TransportAdmissionError("route refused", reason="test_route_refusal"),
        early,
    )


@pytest.mark.serial
@pytest.mark.parametrize(
    "refusal", ["cap", "budget", "containment", "transport", "checkout"]
)
@pytest.mark.parametrize("cleanup", ["removed", "failed", "preserved"])
def test_prelaunch_replacement_withdrawal_requires_removed_checkout(
    store, native_abandonment, monkeypatch, capsys, refusal, cleanup
):
    from support.driver_fakes import EPIC, _advance

    from kew.continuation_recovery import require_retained_replacement

    checkout, kwargs, ctx = native_abandonment
    ctx.epic_ref = EPIC
    abandoned = store.list_continuations(EPIC)[0]
    _replace_native_checkout(ctx)
    expiry = store.continuation_expiry(abandoned)
    assert expiry is not None
    assert store.reserve_epic_node_item(EPIC, "42", ctx.run_id)
    if cleanup != "removed":

        def preserve(*args, **kwargs):
            if cleanup == "failed":
                raise OSError("cleanup unavailable")

        monkeypatch.setattr(dispatch_core, "_cleanup_dispatch_dir", preserve)
    expected_exit = 2 if refusal == "checkout" else 1
    refused = _refuse_bound_replacement(ctx, checkout, refusal)
    assert refused.early.exit_code == expected_exit
    events = store.list_audit_events(
        event="checkout_replacement_withdrawn", run_id=ctx.run_id
    )
    if refusal in {"budget", "containment", "checkout"}:
        assert store.get(ctx.run_id).status == "refused"
        store.clear_epic_hold_item("42")
        # Retention may remove refusal rows; withdrawal must remain authoritative.
        store._conn.execute("DELETE FROM runs WHERE run_id = ?", (ctx.run_id,))
        store._conn.commit()
    if cleanup != "removed":
        assert "checkout cleanup" in capsys.readouterr().err.lower()
        assert not events
        assert checkout.exists()
        with pytest.raises(RuntimeError, match="replacement run.*retention"):
            require_retained_replacement(store, epic_ref=EPIC, item_id="42")
        return
    assert len(events) == 1
    payload = events[0].payload
    assert payload["checkout_removed"] is True
    assert payload["worker_launched"] is False
    assert payload["checkout_id"] == expiry.replacing_checkout_id
    assert payload["abandonment_identity"] == expiry.abandonment_identity
    assert payload["reason"]
    assert not checkout.exists()
    assert store.get(ctx.run_id) is None
    assert not current_continuation(store, abandoned, "42")
    assert store.continuation_expiry(abandoned) == expiry
    # Incomplete or unrelated cleanup evidence cannot bypass missing history.
    read_events = store.list_audit_events
    for field, value in (
        ("checkout_id", "different-token"),
        ("abandonment_identity", "different-decision"),
        ("generation", False),
        ("checkout_removed", False),
        ("worker_launched", True),
        ("reason", ""),
    ):
        invalid = replace(events[0], payload={**payload, field: value})
        with monkeypatch.context() as scoped:
            scoped.setattr(
                store,
                "list_audit_events",
                lambda invalid=invalid, **kw: (
                    [invalid]
                    if kw.get("event") == "checkout_replacement_withdrawn"
                    else read_events(**kw)
                ),
            )
            with pytest.raises(RuntimeError, match="replacement run.*retention"):
                require_retained_replacement(store, epic_ref=EPIC, item_id="42")
    require_retained_replacement(store, epic_ref=EPIC, item_id="42")
    with SqliteRunStore(store._db_path) as reopened:
        require_retained_replacement(reopened, epic_ref=EPIC, item_id="42")
    for verb, error in (
        (verbs.epic_resume, verbs.EpicResumeDenied),
        (verbs.epic_abandon, verbs.EpicAbandonDenied),
    ):
        options = {"reason": "again"} if verb is verbs.epic_abandon else {}
        with pytest.raises(error, match="withdrawn.*fresh dispatch"):
            verb(store, epic_ref=EPIC, item_id="42", config=ctx.config, **options)
    worktree.release_checkout_claims()
    store.release_epic_node_item("42", run_id=ctx.run_id)
    store.clear_epic_hold_item("42")
    store.mark_epic_retry_item(EPIC, "42")
    assert _advance(**kwargs).dispatched == ("42",)
    assert (
        next(checkout.parent.glob("issue-42.replaced-*")) / "edits.txt"
    ).read_text() == "abandoned work"


@pytest.mark.serial
def test_abandonment_refuses_changed_checkout_generation(store, native_abandonment):
    checkout, _, ctx = native_abandonment
    authority = dispatch_core._host_checkout_abandonment(ctx, ctx.config)
    assert authority is not None
    worktree.write_checkout_generation(checkout)
    with pytest.raises(RuntimeError, match="authority does not match"):
        worktree._quarantine_abandoned_checkout(checkout, ctx.branch, authority)
    assert (checkout / "edits.txt").read_text() == "abandoned work"


@pytest.mark.serial
@pytest.mark.parametrize("purge_replacement", [False, True])
def test_canonical_replacement_failure_preserves_its_own_continuation(
    store, tmp_path, monkeypatch, purge_replacement
):
    import shutil

    from support.driver_fakes import EPIC, _advance
    from test_driver_dispatch import _abandoned_dispatch_fixture
    from test_worktree import _git_out, _origin_repo_with_branch

    from kew.sandbox import SandboxDecision
    from kew.subprocess_utils import TOOL_PATHS

    monkeypatch.setitem(TOOL_PATHS, "git", shutil.which("git"))
    repo = _origin_repo_with_branch(tmp_path, "agent/issue-42")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(worktree, "_repo_root", lambda: repo)
    config = DispatchConfig()
    config.branches.base = "main"
    config.branches.worktree_base = str(tmp_path / "worktrees")
    worktree.setup_worktree(issue_num=42, config=config)
    checkout, kwargs = _abandoned_dispatch_fixture(store, tmp_path, monkeypatch)
    kwargs["config"] = config
    abandoned = store.list_continuations(EPIC)[0]
    base = _git_out("rev-parse", "main", cwd=repo)
    monkeypatch.setattr(
        dispatch_core, "publication_remote", lambda _: str(tmp_path / "origin.git")
    )
    launches = []

    class HostSink:
        def dispatch(self, item_id):
            run_id = store.list_epic_reservations(EPIC)[0].run_id
            launches.append(run_id)
            ctx = CheckoutBinding._context(dispatch_core, config, store, run_id=run_id)
            ctx.epic_ref = EPIC
            ctx.publication_base_sha = base
            fresh = dispatch_core._setup_host_worktree(
                ctx,
                config,
                "codex",
                SandboxDecision(
                    sandboxed=False, runtime=None, posture="native", notice=None
                ),
            )
            dispatch_core._bind_initial_checkout(ctx, fresh)
            (fresh / "edits.txt").write_text("replacement worker edits")
            _owner(store, run_id, session_id="executed-session")
            worktree.release_checkout_claims()
            return True

    kwargs["sink"] = HostSink()
    try:
        assert _advance(**kwargs).dispatched == ("42",)
        replacement = store.list_audit_events(event="checkout_replaced")[0]
        assert replacement.payload.get("abandonment_identity") == (
            store.continuation_abandonment_identity(abandoned)
        )
        assert replacement.payload["original_run_id"] == "original"
        assert replacement.payload["generation"] == abandoned.generation
        assert replacement.payload["checkout_id"] == (
            worktree.read_checkout_generation(checkout)
        )
        if purge_replacement:
            # No read of current_continuation may be needed to persist expiry.
            run_id = launches[0]
            # The completed worker no longer owns a dispatch slot.
            store.release_epic_node_item("42", run_id=run_id)
            retention_time = datetime.now(UTC) + timedelta(days=40)

            class RetentionClock:
                @staticmethod
                def now(tz):
                    assert tz == UTC
                    return retention_time

            # Preserve the real A-before-B timeline while advancing retention.
            with monkeypatch.context() as scoped:
                scoped.setattr("kew.run_store.datetime", RetentionClock)
                assert store.purge_older_than(30) == 2
            assert store.get(run_id) is None
            assert store.get("original") is None
        assert not current_continuation(store, abandoned, "42")
        if purge_replacement:
            from kew.graph.driver.tick import _PreexistingWorktreeRefusal

            with pytest.raises(
                _PreexistingWorktreeRefusal, match="no terminal implementation producer"
            ):
                _register_preexisting_dirty_worktree(
                    store,
                    config,
                    epic_ref=EPIC,
                    item_id="42",
                    provider="github",
                    reservation_run_id="admission",
                    liveness=None,
                )
            store.mark_epic_retry_item(EPIC, "42")
            # The driver reports the refusal as a stuck node; the tick goes on.
            refused = _advance(**kwargs)
            assert refused.dispatched == ()
            assert [stuck.node_id for stuck in refused.stuck] == ["42"]
            assert f"replacement run '{run_id}'" in refused.stuck[0].reason
            assert "retention" in refused.stuck[0].reason
            with pytest.raises(
                RuntimeError, match=f"replacement run '{run_id}'.*retention"
            ):
                _capture_abandonment(
                    store,
                    epic_ref=EPIC,
                    item_id="42",
                    branch=abandoned.branch,
                    checkout=checkout,
                )
            assert len(launches) == 1
            assert (checkout / "edits.txt").read_text() == "replacement worker edits"
            expiry = store.continuation_expiry(abandoned)
            assert (
                expiry.abandonment_identity
                == replacement.payload["abandonment_identity"]
            )
            assert expiry.abandoned_generation == abandoned.generation
            assert expiry.replacing_checkout_id == replacement.payload["checkout_id"]
            assert expiry.replacing_run_id == run_id
            assert expiry.replaced_at == replacement.timestamp
            with SqliteRunStore(store._db_path) as reopened:
                assert reopened.continuation_expiry(abandoned) == expiry
                assert not current_continuation(reopened, abandoned, "42")
            return
        preserved = list(checkout.parent.glob("issue-42.replaced-*"))
        assert len(preserved) == 1
        assert (preserved[0] / "edits.txt").read_text() == "abandoned work"
        # Ordinary failures need the existing operator retry before admission.
        store.mark_epic_retry_item(EPIC, "42")
        # Real reconciliation and admission must register B before another setup.
        second = _advance(**kwargs)
        assert second.dispatched == ()
        active = [
            record
            for record in store.list_continuations(EPIC)
            if current_continuation(store, record, "42")
        ]
        assert len(active) == 1, second
        assert active[0].original_run_id == launches[0]
        assert active[0].state == "awaiting_continuation"
        following = _advance(**kwargs)
        assert following.dispatched == ()
        assert any("kew epic continue" in node.reason for node in following.waiting)
        assert len(launches) == 1
        assert list(checkout.parent.glob("issue-42.replaced-*")) == preserved
        assert (checkout / "edits.txt").read_text() == "replacement worker edits"
    finally:
        worktree.release_checkout_claims()


def _fail_issue_lookup(store, record, monkeypatch):
    from kew import cli
    from kew.cli.epic import _resume_dispatch_callback

    def lookup(**kwargs):
        raise RuntimeError("GitHub issue lookup failed")

    with monkeypatch.context() as scoped:
        scoped.setattr(cli, "select_next_issue", lookup)
        callback = _resume_dispatch_callback(
            store,
            DispatchConfig(),
            MagicMock(),
            epic_ref=record.epic_ref,
            provider="github",
        )
        resume_continuation(record, store=store, dispatch=callback)


def _cancel_prelaunch_claim(store, args, monkeypatch):
    record = verbs.epic_resume(
        store, epic_ref=args[0], item_id="42", config=DispatchConfig()
    )
    with pytest.raises(RuntimeError, match="GitHub issue lookup failed"):
        _fail_issue_lookup(store, record, monkeypatch)
    assert store.get_continuation(*args).generation == record.generation - 1
    return store.list_audit_events(event="continuation_released")[0]


def test_prelaunch_cancelled_owner_allows_recovery_but_purged_owner_refuses(
    store, tmp_path, monkeypatch
):
    args = _recovery_family(store, tmp_path)
    _owner(store)
    cancelled = _cancel_prelaunch_claim(store, args, monkeypatch)
    record = store.get_continuation(*args)
    assert store.continuation_owner_run_ids(record) == ["original"]
    assert cancelled.payload["outcome"] == "claim_undone"
    assert (
        cancelled.payload["reason"] == "dispatch_prelaunch_failure: dispatch_exception"
    )
    assert cancelled.payload["restored_generation"] == record.generation == 0
    assert store.get(cancelled.run_id) is None
    assert store.list_epic_reservations(args[0]) == []
    with SqliteRunStore(store._db_path) as reopened:
        assert reopened.continuation_owner_run_ids(record) == ["original"]
        assert (
            run_liveness.continuation_owner_death_refusal(
                reopened, record, DispatchConfig()
            )
            is None
        )
    claimed = verbs.epic_resume(
        store, epic_ref=args[0], item_id="42", config=DispatchConfig()
    )
    assert claimed.generation == 1
    assert store.continuation_origins(claimed.continuation_run_id) == ["original"]
    _owner(store, claimed.continuation_run_id)
    assert store.mark_continuation_awaiting(*args, 1, claimed.continuation_run_id)
    store._execute("DELETE FROM runs WHERE run_id = ?", (claimed.continuation_run_id,))
    store._conn.commit()
    record = store.get_continuation(*args)
    assert store.continuation_owner_run_ids(record) == [
        "original",
        claimed.continuation_run_id,
    ]
    reason = run_liveness.continuation_owner_death_refusal(
        store, record, DispatchConfig()
    )
    assert claimed.continuation_run_id in reason
    assert "run history is missing" in reason
    with pytest.raises(verbs.EpicAbandonDenied, match="run history is missing"):
        _abandon(store)


@pytest.mark.parametrize("write_failure", ["raise", "drop"])
def test_prelaunch_cancellation_write_failure_keeps_claim(
    store, tmp_path, monkeypatch, write_failure
):
    args = _recovery_family(store, tmp_path)
    _owner(store)
    claimed = verbs.epic_resume(
        store, epic_ref=args[0], item_id="42", config=DispatchConfig()
    )
    before = (
        store.get_continuation(*args),
        store.list_epic_reservations(args[0]),
        store.read_chain_rows(),
    )
    insert = store._chained_insert_in_transaction

    def fail(values, **kwargs):
        if json.loads(values[9]).get("outcome") == "claim_undone":
            # Observe the state changes before failing the transaction's audit write.
            assert store.get_continuation(*args).generation == 0
            assert store.list_epic_reservations(args[0]) == []
            if write_failure == "raise":
                raise sqlite3.OperationalError("cancellation write failed")
            return None
        return insert(values, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(store, "_chained_insert_in_transaction", fail)
        with pytest.raises(
            (RuntimeError, sqlite3.OperationalError), match="cancellation"
        ):
            _fail_issue_lookup(store, claimed, monkeypatch)
    assert before == (
        store.get_continuation(*args),
        store.list_epic_reservations(args[0]),
        store.read_chain_rows(),
    )
    with SqliteRunStore(store._db_path) as reopened:
        assert reopened.get_continuation(*args) == claimed
        assert reopened.list_epic_reservations(args[0]) == before[1]
        assert reopened.read_chain_rows() == before[2]
    for verb, error in (
        (verbs.epic_resume, verbs.EpicResumeDenied),
        (verbs.epic_abandon, verbs.EpicAbandonDenied),
    ):
        kwargs = {"reason": "replace"} if verb is verbs.epic_abandon else {}
        with pytest.raises(error, match=claimed.continuation_run_id):
            verb(
                store, epic_ref=args[0], item_id="42", config=DispatchConfig(), **kwargs
            )


@pytest.mark.parametrize("execution", ["row", "dispatch_started", "publication_intent"])
def test_prelaunch_exit_cancels_only_without_execution_evidence(
    store, tmp_path, execution
):
    args = _recovery_family(store, tmp_path)
    _owner(store)
    claimed = _recovery_claim(store, args, "prelaunch")

    def failure(*args):
        return DispatchOutcome(exit_code=3)

    resume_continuation(claimed, store=store, dispatch=failure)
    assert store.get_continuation(*args).generation == 0
    release = store.list_audit_events(event="continuation_released")[0]
    assert release.payload["outcome"] == "claim_undone"
    assert release.payload["reason"] == "dispatch_prelaunch_failure: dispatch_exit_3"
    claimed = _recovery_claim(store, args, "possible-worker")
    if execution == "row":
        store.save(replace(_owner(store, "possible-worker"), status="pending"))
    else:
        emit(store, execution, actor=Actor("policy", "test"), run_id="possible-worker")
    resume_continuation(claimed, store=store, dispatch=failure)
    record = store.get_continuation(*args)
    assert record.generation == 1
    assert store.continuation_owner_run_ids(record) == ["original", "possible-worker"]
    assert (
        store.list_audit_events(event="continuation_released")[0].payload["outcome"]
        == "released"
    )
    assert (
        run_liveness.continuation_owner_death_refusal(store, record, DispatchConfig())
        is not None
    )


@pytest.mark.parametrize(
    "forgery", ["unreleased", "generation", "restored_generation", "event"]
)
def test_prelaunch_cancellation_requires_matching_release(
    store, tmp_path, monkeypatch, forgery
):
    from kew.audit_identity import continuation_evidence, continuation_evidence_identity

    args = _recovery_family(store, tmp_path)
    _owner(store)
    claimed = _recovery_claim(store, args, "forged-owner")
    # Only the unreleased case still owns the mutable claim and reservation.
    if forgery != "unreleased":
        assert store.mark_continuation_awaiting(*args, 1, "forged-owner")
    payload = continuation_evidence(
        phase="release",
        epic_ref=args[0],
        item_id="42",
        original_run_id="original",
        branch=args[3],
        worktree_identity=args[4],
        continuation_run_id="forged-owner",
        generation=2 if forgery == "generation" else 1,
        state="awaiting_continuation",
        outcome="claim_undone",
    )
    payload["reason"] = "dispatch_prelaunch_failure: dispatch_exception"
    payload["restored_generation"] = (
        1 if forgery in {"generation", "restored_generation"} else 0
    )
    payload["evidence_identity"] = continuation_evidence_identity(
        "continuation_released", payload
    )
    event = "continuation_cancelled" if forgery == "event" else "continuation_released"
    before = store.read_chain_rows()
    emit(
        store,
        event,
        actor=Actor("policy", "test"),
        run_id="forged-owner",
        payload=payload,
    )
    record = store.get_continuation(*args)
    assert "forged-owner" in store.continuation_owner_run_ids(record)
    assert (
        run_liveness.continuation_owner_death_refusal(store, record, DispatchConfig())
        is not None
    )
    # Remove only the forged fixture and perform the real atomic cancellation.
    store._execute("DELETE FROM audit_events WHERE id > ?", (before[-1].id,))
    store._conn.commit()
    if forgery != "unreleased":
        # The normal release consumed generation one. Cancel a different claim.
        _owner(store, "forged-owner")
        claimed = verbs.epic_resume(
            store, epic_ref=args[0], item_id="42", config=DispatchConfig()
        )
    with pytest.raises(RuntimeError, match="GitHub issue lookup failed"):
        _fail_issue_lookup(store, claimed, monkeypatch)
    record = store.get_continuation(*args)
    assert claimed.continuation_run_id not in store.continuation_owner_run_ids(record)
    _abandon(store)


@pytest.mark.parametrize("consumed", [0, 16, 32, 33])
def test_prelaunch_cancelled_pages_count_only_consumed_generations(
    store, tmp_path, monkeypatch, consumed
):
    # Run the round-six boundary contract with retained pre-launch claim events.
    _check_cancelled_pages_bound(
        store, tmp_path, monkeypatch, consumed, _cancel_prelaunch_claim
    )


def _cancel_dropped_claim(store, args, monkeypatch):
    generation = store.get_continuation(*args).generation
    # Model a historical writer that committed before appending its event.
    with monkeypatch.context() as scoped:
        scoped.setattr(store, "_attest_continuation_claim", lambda *a, **kw: None)
        claimed = verbs.epic_resume(
            store, epic_ref=args[0], item_id="42", config=DispatchConfig()
        )
    assert store.release_unevidenced_claim(
        *args, claimed.generation, claimed.continuation_run_id
    )
    cancelled = store.list_audit_events(event="continuation_released")[0]
    assert cancelled.payload["outcome"] == "claim_undone"
    assert store.get(cancelled.run_id) is None
    assert store.get_continuation(*args).generation == generation
    return cancelled


@pytest.mark.parametrize("retained_row", [False, True])
def test_cancelled_claim_stays_out_of_retried_owner_history(
    store, tmp_path, monkeypatch, retained_row
):
    args = _recovery_family(store, tmp_path)
    _owner(store)
    cancelled = _cancel_dropped_claim(store, args, monkeypatch)
    before = store.read_chain_rows()
    retried = verbs.epic_resume(
        store, epic_ref=args[0], item_id="42", config=DispatchConfig()
    )
    assert store.continuation_owner_run_ids(retried) == [
        "original",
        retried.continuation_run_id,
    ]
    store.require_continuation_owner_history(retried)
    assert store.continuation_origins(retried.continuation_run_id) == ["original"]
    assert store.read_chain_rows()[: len(before)] == before
    if retained_row:
        # Even an initial pending row keeps the cancelled run as a possible owner.
        store.save(
            replace(_owner(store, cancelled.run_id), status="pending", exit_code=0)
        )
        assert cancelled.run_id in store.continuation_owner_run_ids(retried)
        assert (
            run_liveness.continuation_owner_death_refusal(
                store, retried, DispatchConfig()
            )
            is not None
        )
    # Late execution evidence must also prevent transparent ancestry.
    emit(
        store,
        "dispatch_started",
        actor=Actor("policy", "test"),
        run_id=cancelled.run_id,
    )
    assert cancelled.run_id in store.continuation_owner_run_ids(retried)
    with pytest.raises(RuntimeError, match="launched run"):
        store.continuation_origins(retried.continuation_run_id)


def test_cancelled_claim_cannot_fill_a_later_consumed_generation_gap(
    store, tmp_path, monkeypatch
):
    args = _recovery_family(store, tmp_path)
    _owner(store)
    _cancel_dropped_claim(store, args, monkeypatch)
    _recovery_claim(store, args, "lost-owner", attest=False)
    with monkeypatch.context() as scoped:
        scoped.setattr(store, "_attest_continuation_transition", lambda **kw: None)
        assert store.mark_continuation_awaiting(*args, 1, "lost-owner")
    before = (store.get_continuation(*args), store.read_chain_rows())
    with pytest.raises(verbs.EpicResumeDenied, match="generation 1.*no claim"):
        verbs.epic_resume(
            store, epic_ref=args[0], item_id="42", config=DispatchConfig()
        )
    with pytest.raises(verbs.EpicAbandonDenied, match="generation 1.*no claim"):
        _abandon(store)
    assert before == (store.get_continuation(*args), store.read_chain_rows())


@pytest.mark.parametrize("consumed", [0, 16, 32, 33])
def test_cancelled_pages_count_only_consumed_generations(
    store, tmp_path, monkeypatch, consumed
):
    _check_cancelled_pages_bound(
        store, tmp_path, monkeypatch, consumed, _cancel_dropped_claim
    )


def _check_cancelled_pages_bound(store, tmp_path, monkeypatch, consumed, cancel_claim):
    args = _recovery_family(store, tmp_path)
    _owner(store)
    for _ in range(40):
        cancel_claim(store, args, monkeypatch)
    for generation in range(1, consumed + 1):
        _recovery_claim(store, args, f"consumed-{generation}")
        _owner(store, f"consumed-{generation}")
        assert store.mark_continuation_awaiting(
            *args, generation, f"consumed-{generation}"
        )
        if generation < 32:
            cancel_claim(store, args, monkeypatch)
    before = store.read_chain_rows()
    record = store.get_continuation(*args)
    if consumed > 32:
        with pytest.raises(OverflowError, match="history exceeds policy bound"):
            store.require_continuation_history(record)
        with pytest.raises(
            verbs.EpicAbandonDenied, match="history exceeds policy bound"
        ):
            _abandon(store)
    else:
        store.require_continuation_history(record)
        store.require_continuation_owner_history(record)
        assert store.continuation_owner_run_ids(record) == [
            "original",
            *(f"consumed-{i}" for i in range(1, consumed + 1)),
        ]
        if consumed < 32:
            claimed = verbs.epic_resume(
                store, epic_ref=args[0], item_id="42", config=DispatchConfig()
            )
            assert claimed.generation == consumed + 1
            assert store.continuation_origins(claimed.continuation_run_id) == [
                f"consumed-{consumed}" if consumed else "original"
            ]
            _owner(store, claimed.continuation_run_id)
            assert store.mark_continuation_awaiting(
                *args, claimed.generation, claimed.continuation_run_id
            )
        _abandon(store)
    assert store.read_chain_rows()[: len(before)] == before


def test_cancelled_history_refuses_an_unreadable_later_page(
    store, tmp_path, monkeypatch
):
    args = _recovery_family(store, tmp_path)
    _owner(store)
    for _ in range(40):
        _cancel_dropped_claim(store, args, monkeypatch)
    before = (store.get_continuation(*args), store.read_chain_rows())
    read_page = store._continuation_family_page
    pages = []

    def unreadable(target, event, after_id):
        if event == "continuation_released":
            pages.append(after_id)
            if after_id:
                raise RuntimeError("continuation cancellation page unavailable")
        return read_page(target, event, after_id)

    monkeypatch.setattr(store, "_continuation_family_page", unreadable)
    with pytest.raises(verbs.EpicResumeDenied, match="cancellation page unavailable"):
        verbs.epic_resume(
            store, epic_ref=args[0], item_id="42", config=DispatchConfig()
        )
    with pytest.raises(verbs.EpicAbandonDenied, match="cancellation page unavailable"):
        _abandon(store)
    assert len(pages) == 4
    assert pages[0] == pages[2] == 0
    assert pages[1] == pages[3] > 0
    assert before == (store.get_continuation(*args), store.read_chain_rows())


def test_replacement_refuses_a_dropped_durable_expiry(store, tmp_path, monkeypatch):
    config = DispatchConfig()
    checkout = tmp_path / "preserved"
    (checkout / ".git").mkdir(parents=True)
    args = ("epic:x", "42", "original", "agent/issue-42", checkout)
    store.record_continuation(
        *args,
        checkout_generation=fixture_checkout_generation(args[4]),
    )
    _owner(store)
    _abandon(store)
    authority = _capture_abandonment(
        store, epic_ref="epic:x", item_id="42", branch=args[3], checkout=checkout
    )
    ctx = CheckoutBinding._context(dispatch_core, config, store, run_id="replacement")
    ctx.epic_ref = "epic:x"
    ctx.checkout_abandonment = authority
    monkeypatch.setattr(
        store, "record_continuation_expiry", lambda *args: None, raising=False
    )
    with pytest.raises(RuntimeError, match="expiry.*not durably recorded"):
        dispatch_core._record_checkout_replacement(ctx, checkout, "new-checkout-token")
    assert store.list_audit_events(event="checkout_replaced")
    assert store.continuation_expiry(store.get_continuation(*args)) is None


def test_legacy_replacement_event_expires_after_run_retention(
    store, tmp_path, monkeypatch
):
    from kew.graph.driver.dispatch import reserve_and_dispatch

    args = _recovery_family(store, tmp_path)
    _owner(store)
    _abandon(store)
    abandoned = store.get_continuation(*args)
    config = DispatchConfig()
    ctx = CheckoutBinding._context(dispatch_core, config, store, run_id="replacement")
    ctx.epic_ref = args[0]
    ctx.checkout_abandonment = _capture_abandonment(
        store, epic_ref=args[0], item_id="42", branch=args[3], checkout=args[-1]
    )
    ctx.checkout_abandonment = replace(ctx.checkout_abandonment, rebound_from="")
    _owner(store, "replacement")
    # Simulate an event written before schema v50 and before any expiry read.
    with monkeypatch.context() as scoped:
        scoped.setattr(
            continuation_recovery,
            "expire_checkout_abandonment",
            lambda *a, **kw: None,
            raising=False,
        )
        dispatch_core._record_checkout_replacement(ctx, args[-1], "legacy-token")
    retention_time = datetime.now(UTC) + timedelta(days=40)

    class RetentionClock:
        @staticmethod
        def now(tz):
            assert tz == UTC
            return retention_time

    with monkeypatch.context() as scoped:
        scoped.setattr("kew.run_store.datetime", RetentionClock)
        assert store.purge_older_than(30) == 2
    audit_before = store.read_chain_rows()

    class Sink:
        def dispatch(self, item_id):
            pytest.fail("a purged replacement must not dispatch fresh work")

    # Admission must backfill from the audit event without an earlier read.
    with pytest.raises(RuntimeError, match="replacement run 'replacement'.*retention"):
        reserve_and_dispatch(
            "42",
            epic_ref=args[0],
            store=store,
            sink=Sink(),
            branch_exists=lambda _: False,
        )
    assert not current_continuation(store, abandoned, "42")
    assert store.continuation_expiry(abandoned).replacing_run_id == "replacement"
    assert store.list_epic_reservations(args[0]) == []
    assert store.read_chain_rows() == audit_before


def _legacy_replacement_family(store, tmp_path):
    args = _recovery_family(store, tmp_path)
    _owner(store)
    _abandon(store)
    record = store.get_continuation(*args)
    payload = {
        "epic_ref": record.epic_ref,
        "item_id": record.item_id,
        "original_run_id": record.original_run_id,
        "branch": record.branch,
        "checkout": record.worktree_identity,
        "generation": record.generation,
        "abandonment_identity": store.continuation_abandonment_identity(record),
        "checkout_id": "legacy-checkout",
    }
    return record, payload


@pytest.mark.parametrize("purged", [False, True])
def test_replacement_backfill_bounds_unrelated_ledger_queries(store, tmp_path, purged):
    import json

    record, payload = _legacy_replacement_family(store, tmp_path)
    later = datetime.now(UTC).isoformat()
    variations = {
        "epic_ref": "other-epic",
        "item_id": "other-item",
        "original_run_id": "other-original",
        "branch": "other-branch",
        "checkout": "/other-checkout",
        "generation": record.generation + 1,
        "timestamp": record.terminal_at.isoformat(),
        "older": (record.terminal_at - timedelta(seconds=1)).isoformat(),
    }
    unrelated = []
    for index in range(10_000):
        key, value = list(variations.items())[index % len(variations)]
        bad = dict(payload)
        timestamp = later
        if key in {"timestamp", "older"}:
            timestamp = value
        else:
            bad[key] = value
        unrelated.append((timestamp, f"purged-{index}", json.dumps(bad)))
    # Bulk fixtures model unrelated retained audit rows after their producers purge.
    store._conn.executemany(
        "INSERT INTO audit_events(timestamp, event, run_id, payload, chain_format)"
        " VALUES (?, 'checkout_replaced', ?, ?, 2)",
        unrelated,
    )
    store._conn.commit()
    statements = []
    store._conn.set_trace_callback(statements.append)
    try:
        assert current_continuation(store, record, "42")
        selects = [
            sql for sql in statements if sql.lstrip().upper().startswith("SELECT")
        ]
        assert len(selects) <= 3, f"unrelated ledger caused {len(selects)} SELECTs"
    finally:
        store._conn.set_trace_callback(None)
    _owner(store, "replacement")
    if purged:
        store._execute("DELETE FROM runs WHERE run_id = 'replacement'")
        store._conn.commit()
    emit(
        store,
        "checkout_replaced",
        actor=Actor("policy", "dispatch"),
        run_id="replacement",
        payload=payload,
    )
    statements.clear()
    store._conn.set_trace_callback(statements.append)
    try:
        assert not current_continuation(store, record, "42")
        selects = [
            sql for sql in statements if sql.lstrip().upper().startswith("SELECT")
        ]
        assert len(selects) <= 5, f"matching replacement caused {len(selects)} SELECTs"
    finally:
        store._conn.set_trace_callback(None)
    expiry = store.continuation_expiry(record)
    assert expiry.replacing_run_id == "replacement"
    assert expiry.replacing_checkout_id == "legacy-checkout"
    with SqliteRunStore(store._db_path) as reopened:
        assert reopened.continuation_expiry(record) == expiry
        assert not current_continuation(reopened, record, "42")


@pytest.mark.parametrize("unreadable", [False, True])
def test_replacement_backfill_pages_and_refuses_unreadable_history(
    store, tmp_path, monkeypatch, unreadable
):
    import sqlite3

    record, payload = _legacy_replacement_family(store, tmp_path)
    # These match the SQL family but fail direct authority validation.
    for index in range(32):
        emit(
            store,
            "checkout_replaced",
            actor=Actor("policy", "dispatch"),
            run_id=f"invalid-{index}",
            payload={**payload, "abandonment_identity": "wrong-authority"},
        )
    emit(
        store,
        "checkout_replaced",
        actor=Actor("policy", "dispatch"),
        run_id="purged-replacement",
        payload=payload,
    )
    execute = store._execute
    page_sizes = []
    cursors = []

    def read(sql, params=None):
        if "json_extract(payload, '$.generation')" not in sql:
            return execute(sql, params)
        assert "ORDER BY id ASC LIMIT ?" in sql
        assert params[-1] == 32
        cursors.append(params[0])
        if unreadable and len(cursors) == 2:
            raise sqlite3.OperationalError("replacement page unreadable")
        rows = execute(sql, params).fetchall()
        page_sizes.append(len(rows))

        class Page:
            def fetchall(self):
                return rows

        return Page()

    monkeypatch.setattr(store, "_execute", read)
    if unreadable:
        with pytest.raises(
            sqlite3.OperationalError, match="replacement page unreadable"
        ):
            current_continuation(store, record, "42")
        assert store.continuation_expiry(record) is None
    else:
        assert not current_continuation(store, record, "42")
        assert (
            store.continuation_expiry(record).replacing_run_id == "purged-replacement"
        )
        assert page_sizes == [32, 1]
    assert len(cursors) == 2
    assert cursors[0] == 0 < cursors[1]


@pytest.fixture
def active_checkout(native_abandonment, store, request):
    checkout, _, ctx = native_abandonment
    # Reuse the native checkout, with only the active continuation as its owner.
    store._conn.execute("DELETE FROM epic_continuations")
    store._conn.commit()
    args = ("epic:owner", "42", "active", ctx.branch, checkout / ".." / checkout.name)
    _owner(store, "active")
    store.record_continuation(
        *args,
        checkout_generation=fixture_checkout_generation(args[4]),
    )
    if request.param == "continuing":
        _recovery_claim(store, args, "continuing-owner")
        store.release_epic_node_item("42", run_id="continuing-owner")
    return checkout, ctx, store.get_continuation(*args)


def _stub_checkout_launch(monkeypatch, ctx):
    from kew.sandbox import SandboxDecision

    monkeypatch.setattr(dispatch_core, "resolve_runner", lambda *a, **k: MagicMock())
    monkeypatch.setattr(dispatch_core, "_preflight_pricing", lambda *a: None)
    monkeypatch.setattr(
        dispatch_core, "_require_dispatch_transport_admission", lambda *a: None
    )
    monkeypatch.setattr(
        dispatch_core,
        "_launch_workspace",
        lambda *a, **k: dispatch_core._setup_host_worktree(
            ctx,
            ctx.config,
            "codex",
            SandboxDecision(
                sandboxed=False, runtime=None, posture="native", notice=None
            ),
        ),
    )


@pytest.mark.serial
@pytest.mark.parametrize("active_checkout", ["awaiting", "continuing"], indirect=True)
@pytest.mark.parametrize("replacing_epic", [None, "epic:other"])
def test_active_continuation_refuses_setup_across_epics(
    active_checkout, store, monkeypatch, replacing_epic
):
    checkout, ctx, active = active_checkout
    ctx.epic_ref = replacing_epic
    setup = MagicMock(wraps=dispatch_core.setup_worktree)
    monkeypatch.setattr(dispatch_core, "setup_worktree", setup)
    with pytest.raises(
        RuntimeError, match="epic:owner item 42.*kew epic continue.*kew epic abandon"
    ) as refused:
        _replace_native_checkout(ctx)
    assert type(refused.value).__name__ == "ActiveContinuationRefusal"
    setup.assert_not_called()
    assert (checkout / "edits.txt").read_text() == "abandoned work"
    assert not list(checkout.parent.glob("issue-42.replaced-*"))
    assert (
        store.get_continuation(
            active.epic_ref,
            active.item_id,
            active.original_run_id,
            active.branch,
            checkout,
        )
        == active
    )


@pytest.mark.serial
@pytest.mark.parametrize("active_checkout", ["awaiting", "continuing"], indirect=True)
@pytest.mark.parametrize("replacing_epic", [None, "epic:other"])
def test_active_continuation_cli_refusal_releases_reservation(
    active_checkout, store, monkeypatch, replacing_epic
):
    from typer.testing import CliRunner

    from kew import cli

    checkout, ctx, _ = active_checkout
    ctx.epic_ref = replacing_epic
    ctx.refuse_rollback = MagicMock()
    assert store.reserve_epic_node_item(
        replacing_epic or "epic:reservation", "42", ctx.run_id
    )
    _stub_checkout_launch(monkeypatch, ctx)
    launch = MagicMock(
        wraps=lambda **kw: (
            dispatch_core._launch_runner(ctx, None).early
            or DispatchOutcome(exit_code=0)
        )
    )
    monkeypatch.setattr(cli, "execute_dispatch", launch)

    # Keep the real CLI exit classifier and pre-launch refusal bookkeeping.
    def command(*args, **kwargs):
        return cli._execute_and_capture_outcome(
            issue={"number": 42},
            config=ctx.config,
            store=store,
            notifier=None,
            force_budget=False,
            approve=False,
            model=None,
        )

    _stub_cli_checkout(monkeypatch, ctx, store)
    monkeypatch.setattr(cli, "_dispatch_one", command)
    result = CliRunner().invoke(cli.app, ["dispatch", "42"])
    assert result.exit_code == 2, result.output
    assert launch.call_count == 1
    ctx.refuse_rollback.assert_called_once()
    assert store.list_epic_reservations(replacing_epic or "epic:reservation") == []
    events = store.list_audit_events(event="dispatch_completed", run_id=ctx.run_id)
    assert events[0].payload["error_category"] == "active_continuation_refused"
    assert events[0].payload["exit_code"] == 2
    assert store.list_audit_events(event="publication_intent", run_id=ctx.run_id) == []
    assert store.list_audit_events(event="checkout_replaced", run_id=ctx.run_id) == []
    # A later refusal must not overwrite or release the active owner's reservation.
    owner = store.get("active")
    assert store.reserve_epic_node_item("epic:owner", "42", owner.run_id)
    ctx.epic_ref = "epic:owner"
    ctx.run_id = "later-refusal"
    assert dispatch_core._launch_runner(ctx, None).early.exit_code == 2
    assert store.get(owner.run_id) == owner
    assert store.list_epic_reservations("epic:owner")[0].run_id == owner.run_id
    assert store.get(ctx.run_id).status == "refused"
    assert (checkout / "edits.txt").read_text() == "abandoned work"
    assert not list(checkout.parent.glob("issue-42.replaced-*"))


@pytest.mark.serial
@pytest.mark.parametrize("cleanup", ["failed", "preserved"])
@pytest.mark.parametrize("refusal", ["budget", "containment"])
@pytest.mark.parametrize("replacing_epic", [None, "epic:other"])
def test_retained_refusal_requires_withdrawal_before_and_after_retention(
    native_abandonment, store, monkeypatch, cleanup, refusal, replacing_epic
):
    from support.driver_fakes import EPIC

    checkout, _, ctx = native_abandonment
    _replace_native_checkout(ctx)
    assert store.reserve_epic_node_item(EPIC, "42", ctx.run_id)

    def preserve(*args, **kwargs):
        if cleanup == "failed":
            raise OSError("cleanup failed")

    monkeypatch.setattr(dispatch_core, "_cleanup_dispatch_dir", preserve)
    _refuse_bound_replacement(ctx, checkout, refusal)
    assert store.get(ctx.run_id).status == "refused"
    # Test withdrawal independently from the owner's separate hold requirement.
    store.clear_epic_hold_item("42")
    ctx.epic_ref = replacing_epic
    for purged in (False, True):
        if purged:
            store._conn.execute("DELETE FROM runs WHERE run_id = ?", (ctx.run_id,))
            store._conn.commit()
        with pytest.raises(RuntimeError, match="replacement run") as refused:
            dispatch_core._host_checkout_abandonment(ctx, ctx.config)
        assert type(refused.value).__name__ == "RetainedReplacementRefusal"
        assert (
            "retention" in str(refused.value)
            if purged
            else "withdrawal" in str(refused.value)
        )
    assert checkout.exists()
    assert len(list(checkout.parent.glob("issue-42.replaced-*"))) == 1


@pytest.mark.serial
@pytest.mark.parametrize("hold", ["budget", "sandbox_containment", "operator_review"])
@pytest.mark.parametrize("replacing_epic", [None, "epic:other"])
def test_abandoned_checkout_refuses_owning_epic_hold(
    native_abandonment, store, hold, replacing_epic
):
    from support.driver_fakes import EPIC

    checkout, _, ctx = native_abandonment
    store.hold_epic_node_item(EPIC, "42", hold)
    ctx.epic_ref = replacing_epic
    with pytest.raises(RuntimeError, match=f"{EPIC} item 42 has hold '{hold}'"):
        dispatch_core._host_checkout_abandonment(ctx, ctx.config)
    assert (checkout / "edits.txt").read_text() == "abandoned work"
    assert not list(checkout.parent.glob("issue-42.replaced-*"))


@pytest.mark.serial
@pytest.mark.parametrize("sidecar_state", ["empty", "malformed", "unreadable"])
def test_abandonment_capture_refuses_invalid_sidecar(
    native_abandonment, monkeypatch, sidecar_state
):
    from pathlib import Path

    checkout, _, ctx = native_abandonment
    sidecar = worktree._generation_sidecar(checkout)
    if sidecar_state == "unreadable":
        read = Path.read_text

        def unreadable(path, *args, **kwargs):
            if path == sidecar:
                raise PermissionError("private diagnostic")
            return read(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", unreadable)
    else:
        sidecar.write_text("" if sidecar_state == "empty" else "malformed-token")
    with pytest.raises(
        RuntimeError, match="checkout generation is (malformed|unreadable)"
    ) as refused:
        dispatch_core._host_checkout_abandonment(ctx, ctx.config)
    assert "private diagnostic" not in str(refused.value)
    assert (checkout / "edits.txt").read_text() == "abandoned work"
    assert not list(checkout.parent.glob("issue-42.replaced-*"))


@pytest.mark.serial
def test_legacy_abandonment_mints_claimed_token_and_refuses_replacement(
    native_abandonment, monkeypatch
):
    checkout, _, ctx = native_abandonment
    worktree.revoke_checkout_generation(checkout)
    write = worktree.write_checkout_generation
    minted = []

    def claimed_write(path):
        assert path.absolute() in worktree._checkout_claims
        minted.append(write(path))
        return minted[-1]

    monkeypatch.setattr(worktree, "write_checkout_generation", claimed_write)
    authority = dispatch_core._host_checkout_abandonment(ctx, ctx.config)
    assert authority.checkout_id and minted == [authority.checkout_id]
    assert authority.checkout_id == worktree.read_checkout_generation(checkout)
    worktree.write_checkout_generation(checkout)
    with pytest.raises(RuntimeError, match="authority does not match"):
        worktree._quarantine_abandoned_checkout(checkout, ctx.branch, authority)
    assert (checkout / "edits.txt").read_text() == "abandoned work"
    assert not list(checkout.parent.glob("issue-42.replaced-*"))


def _capture_abandonment(store, **kwargs):
    with worktree.checkout_claim_guard(kwargs["checkout"]):
        return checkout_abandonment(
            store,
            **kwargs,
            capture_generation=lambda: worktree.capture_checkout_generation(
                kwargs["checkout"]
            ),
        )


@pytest.fixture
def round18_rebind(native_abandonment, store, tmp_path):
    recorded, _, ctx = native_abandonment
    record = store.list_continuations(ctx.epic_ref)[0]
    shutil.rmtree(Path(ctx.config.branches.worktree_base))
    ctx.config.branches.worktree_base = str(tmp_path / "moved-base")
    candidate = worktree.dispatch_checkout_path(ctx.config, issue_num=42)
    return recorded, candidate, ctx, record


def _round18_replacement_evidence(store, recorded, candidate, ctx, record):
    event = store.list_audit_events(event="checkout_replaced", run_id=ctx.run_id)[0]
    assert event.payload["checkout"] == str(candidate.resolve())
    assert event.payload["rebound_from"] == str(recorded)
    assert event.payload["rebound_reason"] == "recorded checkout missing"
    expiry = store.continuation_expiry(record)
    assert expiry.replacing_run_id == ctx.run_id
    assert expiry.replacing_checkout_id == event.payload["checkout_id"]
    with SqliteRunStore(store._db_path) as reopened:
        assert reopened.continuation_expiry(record) == expiry
        assert not current_continuation(reopened, record, "42")
    return expiry


@pytest.mark.serial
@pytest.mark.parametrize("moved", [False, True], ids=["deleted-base", "moved-base"])
def test_round18_missing_checkout_rebind_creates_and_expires(
    store, round18_rebind, moved
):
    recorded, candidate, ctx, record = round18_rebind
    if not moved:
        ctx.config.branches.worktree_base = str(recorded.parent.parent)
        candidate = recorded
    assert not candidate.exists()
    assert _replace_native_checkout(ctx) == candidate
    assert worktree.is_safe_standalone_clone(candidate)
    _round18_replacement_evidence(store, recorded, candidate, ctx, record)


@pytest.mark.serial
@pytest.mark.parametrize("candidate_exists", [False, True])
def test_round18_existing_recorded_checkout_refuses_move(
    native_abandonment, store, tmp_path, candidate_exists
):
    recorded, _, ctx = native_abandonment
    ctx.config.branches.worktree_base = str(tmp_path / "moved-base")
    candidate = worktree.dispatch_checkout_path(ctx.config, issue_num=42)
    if candidate_exists:
        candidate.mkdir(parents=True)
    before = store.read_chain_rows()
    with pytest.raises(
        RuntimeError,
        match="^abandonment authority does not match the replacement checkout$",
    ):
        dispatch_core._host_checkout_abandonment(ctx, ctx.config)
    assert store.read_chain_rows() == before
    assert (recorded / "edits.txt").read_text() == "abandoned work"
    assert not worktree._generation_sidecar(candidate).exists()


@pytest.mark.serial
@pytest.mark.parametrize("valid", [True, False], ids=["valid-token", "invalid-token"])
def test_round18_existing_candidate_generation_governs(store, round18_rebind, valid):
    from kew.continuation_recovery import CheckoutGenerationRefusal

    recorded, candidate, ctx, record = round18_rebind
    worktree.setup_worktree(issue_num=42, config=ctx.config)
    token = worktree.read_checkout_generation(candidate)
    if not valid:
        worktree._generation_sidecar(candidate).write_text("invalid\n")
        before = store.read_chain_rows()
        with pytest.raises(CheckoutGenerationRefusal, match="generation is malformed"):
            dispatch_core._host_checkout_abandonment(ctx, ctx.config)
        assert store.read_chain_rows() == before
        assert store.continuation_expiry(record) is None
        return
    authority = dispatch_core._host_checkout_abandonment(ctx, ctx.config)
    assert authority.checkout_id == token
    assert _replace_native_checkout(ctx) == candidate
    _round18_replacement_evidence(store, recorded, candidate, ctx, record)


@pytest.mark.serial
def test_round18_rebound_authority_is_consumed_once(store, round18_rebind):
    from kew.continuation_recovery import RetainedReplacementRefusal

    recorded, candidate, ctx, record = round18_rebind
    _replace_native_checkout(ctx)
    expiry = _round18_replacement_evidence(store, recorded, candidate, ctx, record)
    before = store.read_chain_rows()
    # Retained expiry still refuses fresh work when the replacing run is absent.
    ctx.run_id = "second-fresh"
    with pytest.raises(RetainedReplacementRefusal, match="replacement run.*retention"):
        dispatch_core._host_checkout_abandonment(ctx, ctx.config)
    assert store.continuation_expiry(record) == expiry
    assert store.read_chain_rows() == before


@pytest.mark.serial
@pytest.mark.parametrize("failure_at", ["setup", "expiry"])
def test_round18_failed_rebind_keeps_authority_for_next_attempt(
    store, round18_rebind, monkeypatch, failure_at
):
    recorded, candidate, ctx, record = round18_rebind
    authority = dispatch_core._host_checkout_abandonment(ctx, ctx.config)
    assert authority is not None

    def fail(*args, **kwargs):
        raise OSError("replacement attempt interrupted")

    target = (
        "setup_worktree" if failure_at == "setup" else "expire_checkout_abandonment"
    )
    with monkeypatch.context() as scoped:
        owner = dispatch_core if failure_at == "setup" else continuation_recovery
        scoped.setattr(owner, target, fail)
        with pytest.raises(OSError, match="replacement attempt interrupted"):
            _replace_native_checkout(ctx)
    assert store.continuation_expiry(record) is None
    worktree.release_checkout_claims()
    ctx.run_id = "next-attempt"
    retried = dispatch_core._host_checkout_abandonment(ctx, ctx.config)
    assert retried.evidence_identity == authority.evidence_identity
    assert store.continuation_expiry(record) is None
    _replace_native_checkout(ctx)
    _round18_replacement_evidence(store, recorded, candidate, ctx, record)


@pytest.mark.serial
@pytest.mark.parametrize(
    "item_id,branch", [("43", "agent/issue-42"), ("42", "agent/other")]
)
def test_round18_missing_checkout_rebind_requires_item_and_branch(
    store, round18_rebind, item_id, branch
):
    recorded, candidate, ctx, record = round18_rebind
    before = store.read_chain_rows()
    assert (
        _capture_abandonment(
            store,
            epic_ref=ctx.epic_ref,
            item_id=item_id,
            branch=branch,
            checkout=candidate,
        )
        is None
    )
    assert store.read_chain_rows() == before
    assert store.continuation_expiry(record) is None
    # Positive control proves that only the complete logical owner can rebind.
    _replace_native_checkout(ctx)
    _round18_replacement_evidence(store, recorded, candidate, ctx, record)


@pytest.mark.serial
def test_round18_rebound_withdrawal_keeps_original_evidence_family(
    store, round18_rebind
):
    from kew.continuation_recovery import replacement_was_withdrawn

    recorded, candidate, ctx, record = round18_rebind
    _replace_native_checkout(ctx)
    expiry = _round18_replacement_evidence(store, recorded, candidate, ctx, record)
    shutil.rmtree(candidate)
    dispatch_core._record_checkout_withdrawal(
        ctx, candidate, reason="prelaunch refusal"
    )
    event = store.list_audit_events(event="checkout_replacement_withdrawn")[0]
    assert event.payload["rebound_from"] == str(recorded)
    assert event.payload["rebound_reason"] == "recorded checkout missing"
    assert replacement_was_withdrawn(store, record)
    assert store.continuation_expiry(record) == expiry
    assert dispatch_core._host_checkout_abandonment(ctx, ctx.config) is None


@pytest.mark.serial
@pytest.mark.parametrize(
    "field,value",
    [
        ("rebound_from", "another-checkout"),
        ("rebound_from", None),
        ("rebound_reason", "another-reason"),
        ("rebound_reason", None),
    ],
)
def test_round18_rebind_expiry_requires_original_path_and_reason(
    store, round18_rebind, field, value
):
    from kew.continuation_recovery import expire_checkout_abandonment

    recorded, candidate, ctx, record = round18_rebind
    authority = dispatch_core._host_checkout_abandonment(ctx, ctx.config)
    assert authority is not None
    payload = {
        "epic_ref": record.epic_ref,
        "item_id": record.item_id,
        "original_run_id": record.original_run_id,
        "branch": record.branch,
        "generation": record.generation,
        "abandonment_identity": authority.evidence_identity,
        "checkout": str(candidate),
        "checkout_id": "a" * 32,
        **authority.rebound_evidence,
        field: value,
    }
    emit(
        store,
        "checkout_replaced",
        actor=Actor("policy", "dispatch"),
        run_id="invalid-replacement",
        payload=payload,
    )
    before = store.read_chain_rows()
    with pytest.raises(RuntimeError, match="no validated abandonment expiry"):
        expire_checkout_abandonment(
            store, epic_ref=record.epic_ref, item_id="42", run_id="invalid-replacement"
        )
    assert store.continuation_expiry(record) is None
    assert store.read_chain_rows() == before
    _replace_native_checkout(ctx)
    _round18_replacement_evidence(store, recorded, candidate, ctx, record)


@pytest.mark.serial
@pytest.mark.parametrize("active_checkout", ["awaiting", "continuing"], indirect=True)
def test_active_continuation_loop_cli_refuses_before_setup(
    active_checkout, store, monkeypatch
):
    from contextlib import nullcontext
    from types import SimpleNamespace

    from typer.testing import CliRunner

    from kew import cli
    from kew.loops import delegate

    checkout, ctx, _ = active_checkout
    ctx.config.logging.dir = str(checkout.parent / "logs")
    monkeypatch.setattr(cli, "load_config", lambda **kw: ctx.config)
    monkeypatch.setattr(cli, "_open_run_store", lambda *a: nullcontext(store))
    for name in (
        "_emit_staleness_warnings",
        "_preflight",
        "_detect_repo",
        "_refuse_if_fleet_stopped",
        "_log_budget_banner",
    ):
        monkeypatch.setattr(cli, name, lambda *a, **kw: None)
    monkeypatch.setattr(
        cli,
        "select_next_issue",
        lambda **kw: {"number": 42, "title": "resume work", "body": "", "labels": []},
    )
    resolved = SimpleNamespace(runner=SimpleNamespace(name="codex"), model="test-model")
    monkeypatch.setattr(cli, "_resolve_loop_route", lambda *a: resolved)
    monkeypatch.setattr(delegate, "_build_default_gate", lambda *a: MagicMock())
    monkeypatch.setattr(
        delegate,
        "_loop_verification_admission",
        lambda *a: SimpleNamespace(refusal=None),
    )
    monkeypatch.setattr(delegate, "_announce_verification_posture", lambda *a: None)
    monkeypatch.setattr(delegate, "_require_loop_transport_admission", lambda *a: None)
    monkeypatch.setattr(delegate, "loop_route_rejection", lambda *a, **kw: None)
    monkeypatch.setattr(delegate, "_is_priced", lambda *a: True)
    monkeypatch.setattr(delegate, "_resolved_runner_preflight_failure", lambda *a: None)
    monkeypatch.setattr(
        delegate.DelegateDriver,
        "_new_publication_binding",
        lambda *a: SimpleNamespace(base_sha=ctx.publication_base_sha),
    )
    monkeypatch.setattr(
        delegate.DelegateDriver, "_finalize", lambda self, loop, *a: loop
    )
    launches = []

    def session(self, loop, *args, **kwargs):
        launches.append(loop.loop_id)
        return loop

    monkeypatch.setattr(delegate.DelegateDriver, "_run_session", session)
    setup = MagicMock(wraps=delegate.setup_worktree)
    monkeypatch.setattr(delegate, "setup_worktree", setup)
    result = CliRunner().invoke(cli.app, ["dispatch", "42", "--loop"])
    assert result.exit_code == 2, result.output
    assert "epic:owner item 42" in result.output
    assert launches == []
    setup.assert_not_called()
    assert (checkout / "edits.txt").read_text() == "abandoned work"
    assert not list(checkout.parent.glob("issue-42.replaced-*"))


@pytest.mark.parametrize(
    "evidence",
    ["dispatch_started", "publication_intent", "process", "attempt", "container"],
)
def test_executed_refusal_keeps_replacement_admission(store, tmp_path, evidence):
    from kew.continuation_recovery import (
        expire_checkout_abandonment,
        require_retained_replacement,
    )

    record, payload = _legacy_replacement_family(store, tmp_path)
    run = _owner(store, "replacement")
    store.save(
        replace(
            run,
            status="refused",
            pgid=123 if evidence == "process" else None,
            attempt_pgid=456 if evidence == "attempt" else None,
            container_name="worker" if evidence == "container" else None,
        ),
        update_ownership=True,
    )
    emit(
        store,
        "checkout_replaced",
        actor=Actor("policy", "dispatch"),
        run_id=run.run_id,
        payload=payload,
    )
    expire_checkout_abandonment(
        store, epic_ref=record.epic_ref, item_id="42", run_id=run.run_id
    )
    if evidence in {"dispatch_started", "publication_intent"}:
        emit(
            store,
            evidence,
            actor=Actor("policy", "dispatch"),
            run_id=run.run_id,
            payload={},
        )
    require_retained_replacement(store, epic_ref=record.epic_ref, item_id="42")


def _stub_cli_checkout(monkeypatch, ctx, store):
    from contextlib import nullcontext

    from kew import cli

    ctx.config.logging.dir = str(
        worktree.dispatch_checkout_path(ctx.config, issue_num=42).parent / "logs"
    )
    monkeypatch.setattr(cli, "load_config", lambda **kw: ctx.config)
    monkeypatch.setattr(cli, "_open_run_store", lambda *a: nullcontext(store))
    monkeypatch.setattr(cli, "build_notifier", lambda *a: None)
    for name in (
        "_emit_staleness_warnings",
        "_preflight",
        "_detect_repo",
        "_refuse_if_fleet_stopped",
        "_log_budget_banner",
    ):
        monkeypatch.setattr(cli, name, lambda *a, **kw: None)


@pytest.mark.serial
@pytest.mark.parametrize("active_checkout", ["awaiting", "continuing"], indirect=True)
@pytest.mark.parametrize("replacing_epic", [None, "epic:other"])
def test_round14_case_alias_refuses_active_dispatch(
    active_checkout, store, monkeypatch, replacing_epic
):
    from kew.continuation_recovery import ActiveContinuationRefusal

    checkout, ctx, active = active_checkout
    base = checkout.parent.parent
    alias_base = _round14_case_alias(base)
    ctx.config.branches.worktree_base = str(alias_base)
    ctx.epic_ref = replacing_epic
    setup = MagicMock(wraps=dispatch_core.setup_worktree)
    monkeypatch.setattr(dispatch_core, "setup_worktree", setup)
    before = checkout.stat(), (checkout / "edits.txt").read_bytes()
    with pytest.raises(
        ActiveContinuationRefusal, match="epic:owner item 42"
    ) as refused:
        _replace_native_checkout(ctx)
    assert active.worktree_identity in str(refused.value)
    setup.assert_not_called()
    assert (checkout.stat(), (checkout / "edits.txt").read_bytes()) == before
    assert not list(checkout.parent.glob("issue-42.replaced-*"))
    assert store.continuations_for_checkout(
        alias_base / "agent" / "issue-42", item_id="42", branch="agent/issue-42"
    ) == [active]


@pytest.mark.serial
@pytest.mark.parametrize("active_checkout", ["awaiting"], indirect=True)
@pytest.mark.parametrize("directory_state", ["moved", "deleted"])
def test_round14_missing_recorded_path_refuses_dispatch(
    active_checkout, store, monkeypatch, directory_state
):
    import shutil

    from kew.continuation_recovery import ActiveContinuationRefusal

    checkout, ctx, active = active_checkout
    recorded = checkout
    old_base = checkout.parent.parent
    new_base = old_base.with_name("relocated-worktrees")
    if directory_state == "moved":
        old_base.rename(new_base)
        checkout = new_base / "agent" / "issue-42"
    else:
        shutil.rmtree(old_base)
    ctx.config.branches.worktree_base = str(new_base)
    ctx.epic_ref = None
    setup = MagicMock(wraps=dispatch_core.setup_worktree)
    monkeypatch.setattr(dispatch_core, "setup_worktree", setup)
    assert not recorded.exists()
    candidate = new_base / "agent" / "issue-42"
    before = _round14_checkout_snapshot(candidate)
    with pytest.raises(ActiveContinuationRefusal) as refused:
        _replace_native_checkout(ctx)
    assert str(refused.value) == (
        f"epic:owner item 42 owns an active continuation; "
        f"recorded checkout {active.worktree_identity!r}; "
        "unverifiable checkout ownership refuses fresh dispatch; "
        "use `kew epic continue` or `kew epic abandon` before fresh dispatch"
    )
    setup.assert_not_called()
    assert _round14_checkout_snapshot(candidate) == before
    assert not list(new_base.rglob("issue-42.replaced-*"))
    assert store.continuations_for_checkout(
        new_base / "agent" / "issue-42", item_id="42", branch="agent/issue-42"
    ) == [active]


@pytest.mark.serial
def test_round14_alias_abandonment_authorizes_bound_replacement(
    store, native_abandonment
):
    checkout, _, ctx = native_abandonment
    base = checkout.parent.parent
    alias_base = _round14_case_alias(base)
    alias = alias_base / "agent" / "issue-42"
    abandoned = store.list_continuations(ctx.epic_ref)[0]
    authority = _capture_abandonment(
        store, epic_ref=None, item_id="42", branch=ctx.branch, checkout=alias
    )
    assert authority is not None
    assert authority.original_run_id == abandoned.original_run_id
    assert authority.evidence_identity == store.continuation_abandonment_identity(
        abandoned
    )
    ctx.config.branches.worktree_base = str(alias_base)
    ctx.epic_ref = None
    _replace_native_checkout(ctx)
    expiry = store.continuation_expiry(abandoned)
    assert expiry is not None
    assert expiry.replacing_run_id == ctx.run_id
    assert expiry.replacing_checkout_id == worktree.read_checkout_generation(alias)
    assert not current_continuation(store, abandoned, "42")
    preserved = list(checkout.parent.glob("issue-42.replaced-*"))
    assert len(preserved) == 1
    assert (preserved[0] / "edits.txt").read_text() == "abandoned work"


def test_round14_alias_replacement_evidence_expires_abandonment(store, tmp_path):
    record, payload = _legacy_replacement_family(store, tmp_path)
    checkout = tmp_path / "preserved"
    checkout.mkdir()
    alias = _round14_case_alias(checkout)
    payload["checkout"] = str(alias)
    _owner(store, "replacement")
    emit(
        store,
        "checkout_replaced",
        actor=Actor("policy", "dispatch"),
        run_id="replacement",
        item_id="42",
        payload=payload,
    )
    assert not current_continuation(store, record, "42")
    assert store.continuation_expiry(record).replacing_run_id == "replacement"
    assert (
        store.get_continuation(
            record.epic_ref,
            record.item_id,
            record.original_run_id,
            record.branch,
            alias,
        )
        == record
    )


def test_round14_alias_claim_preserves_original_database_key(store, tmp_path):
    checkout = tmp_path / "preserved"
    checkout.mkdir()
    alias = _round14_case_alias(checkout)
    args = ("epic:x", "42", "original", "agent/issue-42", checkout)
    record = store.record_continuation(
        *args,
        checkout_generation=fixture_checkout_generation(args[4]),
    )
    alias_args = (*args[:-1], alias)
    assert (
        store.claim_continuation_under_cap(
            *alias_args,
            "continued",
            0,
            claim_evidence=fixture_claim_evidence(store, *alias_args, "continued"),
        )
        == "claimed"
    )
    claimed = store.get_continuation(*alias_args)
    assert claimed.worktree_identity == record.worktree_identity
    assert store.mark_continuation_awaiting(*alias_args, 1, "continued")
    assert store.record_continuation(
        *alias_args,
        checkout_generation=fixture_checkout_generation(alias_args[4]),
    ) == store.get_continuation(*args)
    assert store.reserve_epic_node_item("epic:x", "42", "reservation")
    registered = store.register_continuation_and_release_reservation(
        *alias_args,
        "reservation",
        checkout_generation=fixture_checkout_generation(alias_args[4]),
    )
    assert registered.worktree_identity == record.worktree_identity
    assert len(store.list_continuations("epic:x")) == 1
    other = tmp_path / "unrelated"
    other.mkdir()
    assert (
        store.continuations_for_checkout(other, item_id="42", branch="agent/issue-42")
        == []
    )
    assert store.get_continuation(*args[:-1], other) is None


def test_round14_alias_retains_checkout_owner_evidence(store, tmp_path):
    checkout = tmp_path / "preserved"
    checkout.mkdir()
    alias = _round14_case_alias(checkout)
    args = ("epic:x", "42", "original", "agent/issue-42", checkout)
    record = store.record_continuation(
        *args,
        checkout_generation=fixture_checkout_generation(args[4]),
    )
    _owner(store)
    _owner(store, "retained")
    emit(
        store,
        "publication_base",
        actor=Actor("policy", "test"),
        run_id="retained",
        payload={"checkout": str(alias)},
    )
    assert "retained" in store.continuation_owner_run_ids(record)


@pytest.mark.parametrize("publish", ["canonical", "checkout"])
def test_round14_alias_publication_ancestry_preserves_credential_refusal(
    store, tmp_path, publish
):
    from test_publication_credentials import _profile

    from kew.publication_credentials import (
        CredentialPublicationRefused,
        require_publication_credentials,
    )
    from kew.readiness import Containment

    checkout = tmp_path / "preserved"
    checkout.mkdir()
    alias = _round14_case_alias(checkout)
    _owner(store)
    args = ("epic:x", "42", "original", "agent/issue-42", checkout)
    (checkout / ".git").mkdir(exist_ok=True)
    worktree.write_checkout_generation(checkout)
    store.record_continuation(
        *args,
        checkout_generation=fixture_checkout_generation(args[4]),
    )
    dispatch_core._bind_publication_base(
        store,
        publish="canonical",
        base="a" * 40,
        run_id="original",
        issue_num=42,
        item_id="42",
        branch=args[3],
        checkout=checkout,
    )
    claimed = _recovery_claim(store, args, "continued")
    ctx = CheckoutBinding._context(
        dispatch_core, DispatchConfig(), store, run_id="continued"
    )
    ctx.config.publication.publish = publish
    ctx.epic_ref = "epic:x"
    ctx.worktree_dir = alias
    assert dispatch_core._continuation_base(ctx) == "a" * 40
    assert dispatch_core._continuation_identity_is_claimed(
        store, "epic:x", "42", str(alias), args[3], "continued"
    )
    assert store.continuations_for_checkout(
        alias, item_id="42", branch="agent/issue-42"
    ) == [claimed]
    owner = dispatch_core._publication_owner(
        store,
        run_id="continued",
        item_id="42",
        branch=args[3],
        checkout=checkout,
        base_sha="a" * 40,
    )
    assert owner["lineage_run_id"] == "original"
    _owner(store, "continued")
    _profile(
        store,
        "original",
        1,
        containment=Containment.CONTAINED,
        auth_path="env:SYNTHETIC_PROVIDER_KEY",
    )
    _profile(store, "continued", 1, containment=Containment.HOST, auth_path="cli-login")
    with pytest.raises(CredentialPublicationRefused) as refused:
        require_publication_credentials(
            store, run_id="continued", published_head="e" * 40
        )
    assert refused.value.reason == "credential_scan_missing"
    assert store.continuation_origins("continued") == ["original"]


def test_round14_unverifiable_owner_requires_explicit_abandonment(store, tmp_path):
    from kew.continuation_recovery import ActiveContinuationRefusal

    args = _recovery_family(store, tmp_path)
    _owner(store)
    other = tmp_path / "unrelated"
    (other / ".git").mkdir(parents=True)
    with pytest.raises(ActiveContinuationRefusal, match="recorded checkout"):
        _capture_abandonment(
            store, epic_ref=None, item_id="42", branch=args[3], checkout=other
        )
    _abandon(store)
    authority = _capture_abandonment(
        store, epic_ref=None, item_id="42", branch=args[3], checkout=other
    )
    assert authority.rebound_from == str(args[-1])
    assert store.continuation_expiry(store.get_continuation(*args)) is None


def test_round14_alias_publication_intent_keeps_original_owner(store, tmp_path):
    checkout = tmp_path / "preserved"
    checkout.mkdir()
    alias = _round14_case_alias(checkout)
    (checkout / ".git").mkdir(exist_ok=True)
    worktree.write_checkout_generation(checkout)
    args = dict(run_id="original", item_id="42", branch="agent/issue-42")
    owner = dispatch_core._publication_owner(
        store, **args, checkout=checkout, base_sha="a" * 40
    )
    dispatch_core._record_publication_intent(
        store,
        **args,
        commit_sha="b" * 40,
        tree_sha="c" * 40,
        parent_sha="a" * 40,
        owner=owner,
    )
    alias_owner = dispatch_core._publication_owner(
        store, **args, checkout=alias, base_sha="a" * 40
    )
    assert dispatch_core._recorded_publication_intents(
        store, item_id="42", branch="agent/issue-42", owner=alias_owner
    ) == {"b" * 40}
    worktree.write_checkout_generation(checkout)
    successor = dispatch_core._publication_owner(
        store, **args, checkout=alias, base_sha="a" * 40
    )
    assert (
        dispatch_core._recorded_publication_intents(
            store, item_id="42", branch="agent/issue-42", owner=successor
        )
        == set()
    )


@pytest.fixture
def round15_dispatch(store, tmp_path, monkeypatch):
    """Exercise real admission and claims before the stubbed checkout mutation."""
    config = DispatchConfig()
    config.branches.worktree_base = str(tmp_path / "worktrees")
    setup = MagicMock()
    monkeypatch.setattr(dispatch_core, "setup_worktree", setup)

    def dispatch(item_id, *, branch=None):
        ctx = CheckoutBinding._context(dispatch_core, config, store, run_id="fresh")
        ctx.item_id = item_id
        ctx.issue_num = int(item_id)
        ctx.branch = branch or f"agent/issue-{item_id}"
        ctx.epic_ref = None
        checkout = worktree.dispatch_checkout_path(config, issue_num=ctx.issue_num)
        setup.reset_mock()
        setup.return_value = checkout
        try:
            result = dispatch_core._setup_host_worktree(
                ctx, config, "codex", CheckoutBinding._decision(sandboxed=False)
            )
            assert result == checkout
            setup.assert_called_once()
            return ctx.checkout_abandonment
        finally:
            worktree.release_checkout_claims()

    return config, setup, dispatch


def _round15_terminal(store, checkout, *, status="failure"):
    args = ("epic:x", "42", "original", "agent/issue-42", checkout)
    _owner(store)
    store.record_continuation(
        *args,
        checkout_generation=fixture_checkout_generation(args[4]),
    )
    _recovery_claim(store, args, "last-owner")
    run = _owner(store, "last-owner")
    store.save(replace(run, status=status, finished_at=datetime.now(UTC)))
    assert store.finalize_continuation(*args, 1, "last-owner")
    return store.get_continuation(*args)


def _round15_completed(store, checkout, completion):
    args = ("epic:x", "42", "original", "agent/issue-42", checkout)
    if completion == "released_publication":
        _owner(store)
        store.record_continuation(
            *args,
            checkout_generation=fixture_checkout_generation(args[4]),
        )
        _recovery_claim(store, args, "released-owner")
        run = _owner(store, "released-owner")
        store.save(replace(run, status="awaiting_publication"))
        assert store.mark_continuation_awaiting(*args, 1, "released-owner")
        assert store.finalize_awaiting_publication("released-owner")
        record = store.get_continuation(*args)
    elif completion == "continuation":
        record = _round15_terminal(store, checkout, status="success")
    else:
        run = _owner(store)
        status = "success" if completion == "original" else "awaiting_publication"
        store.save(replace(run, status=status, finished_at=datetime.now(UTC)))
        record = store.record_continuation(
            *args,
            checkout_generation=fixture_checkout_generation(args[4]),
        )
        if completion == "publication":
            assert store.finalize_awaiting_publication("original")
            record = store.get_continuation(*args)
    return record


@pytest.mark.parametrize(
    "completion", ["original", "continuation", "publication", "released_publication"]
)
@pytest.mark.parametrize("item_id", ["99", "42"])
def test_round15_completed_deleted_checkout_allows_fresh_dispatch(
    store, round15_dispatch, completion, item_id
):
    config, _, dispatch = round15_dispatch
    checkout = worktree.dispatch_checkout_path(config, issue_num=42)
    checkout.mkdir(parents=True)
    record = _round15_completed(store, checkout, completion)
    checkout.rmdir()
    assert record.state == "terminal"
    assert record.terminal_at is not None
    assert dispatch(item_id) is None
    assert not current_continuation(store, record, "42")
    assert store.list_continuations("epic:x") == [record]


@pytest.mark.parametrize("state", ["awaiting", "continuing"])
@pytest.mark.parametrize("recorded_location", ["relocated", "candidate"])
def test_round15_missing_checkout_blocks_only_same_item_and_branch(
    store, round15_dispatch, tmp_path, state, recorded_location
):
    from kew.continuation_recovery import ActiveContinuationRefusal

    config, setup, dispatch = round15_dispatch
    checkout = (
        tmp_path / "deleted-base"
        if recorded_location == "relocated"
        else worktree.dispatch_checkout_path(config, issue_num=99)
    )
    args = ("epic:x", "42", "original", "agent/issue-42", checkout)
    store.record_continuation(
        *args,
        checkout_generation=fixture_checkout_generation(args[4]),
    )
    if state == "continuing":
        _recovery_claim(store, args, "last-owner")
    assert dispatch("99") is None
    assert dispatch("42", branch="agent/another-branch") is None
    with pytest.raises(ActiveContinuationRefusal) as refused:
        dispatch("42")
    assert str(refused.value) == (
        "epic:x item 42 owns an active continuation; "
        f"recorded checkout {str(args[-1])!r}; "
        "unverifiable checkout ownership refuses fresh dispatch; "
        "use `kew epic continue` or `kew epic abandon` before fresh dispatch"
    )
    setup.assert_not_called()


@pytest.mark.parametrize("status", ["failure", "killed", "refused", None])
def test_round15_unproven_terminal_blocks_only_same_logical_checkout(
    store, round15_dispatch, status
):
    from kew.continuation_recovery import ActiveContinuationRefusal

    config, setup, dispatch = round15_dispatch
    checkout = worktree.dispatch_checkout_path(config, issue_num=42)
    record = _round15_terminal(store, checkout)
    if status is None:
        store._conn.execute(
            "DELETE FROM runs WHERE run_id = ?", (record.continuation_run_id,)
        )
        store._conn.commit()
    else:
        store.save(replace(store.get(record.continuation_run_id), status=status))
    assert dispatch("99") is None
    with pytest.raises(
        ActiveContinuationRefusal, match="kew epic continue.*kew epic abandon"
    ):
        dispatch("42")
    setup.assert_not_called()


@pytest.mark.parametrize("checkout_exists", [False, True])
def test_round15_failed_terminal_abandon_allows_fresh_dispatch(
    store, round15_dispatch, monkeypatch, checkout_exists
):
    from kew.continuation_recovery import ActiveContinuationRefusal

    monkeypatch.setenv("KEW_ACTOR", "operator")
    config, setup, dispatch = round15_dispatch
    checkout = worktree.dispatch_checkout_path(config, issue_num=42)
    if checkout_exists:
        (checkout / ".git").mkdir(parents=True)
    record = _round15_terminal(store, checkout)
    with pytest.raises(ActiveContinuationRefusal, match="kew epic abandon"):
        dispatch("42")
    setup.assert_not_called()
    _abandon(store, config)
    authority = dispatch("42")
    assert authority is not None
    assert authority.generation == record.generation
    abandoned = store.list_continuations("epic:x")[0]
    assert store.continuation_was_abandoned(abandoned)
    assert len(store.list_audit_events(event="continuation_abandoned")) == 1


def test_round15_failed_terminal_can_resume(store, tmp_path, monkeypatch):
    monkeypatch.setenv("KEW_ACTOR", "operator")
    record = _round15_terminal(store, tmp_path / "preserved")
    claimed = verbs.epic_resume(
        store, epic_ref="epic:x", item_id="42", config=DispatchConfig()
    )
    assert claimed.state == "continuing"
    assert claimed.generation == record.generation + 1
    assert claimed.continuation_run_id != record.continuation_run_id
    assert claimed.terminal_at is None
    assert store.continuation_origins(claimed.continuation_run_id) == ["last-owner"]


@pytest.mark.parametrize("alias_kind", ["relocated", "case"])
def test_round15_other_item_inode_owner_blocks_until_proven_complete(
    store, round15_dispatch, tmp_path, alias_kind
):
    from kew.continuation_recovery import ActiveContinuationRefusal

    config, setup, dispatch = round15_dispatch
    recorded_base = tmp_path / "recorded-base"
    recorded_base.mkdir()
    if alias_kind == "case":
        candidate_base = _round14_case_alias(recorded_base)
    else:
        candidate_base = tmp_path / "relocated-base"
        recorded_base.rename(candidate_base)
        recorded_base.symlink_to(candidate_base, target_is_directory=True)
    config.branches.worktree_base = str(candidate_base)
    checkout = recorded_base / "agent" / "issue-99"
    checkout.mkdir(parents=True)
    record = _round15_terminal(store, checkout)
    candidate = worktree.dispatch_checkout_path(config, issue_num=99)
    assert os.path.samefile(checkout, candidate)
    assert record.item_id == "42"
    with pytest.raises(ActiveContinuationRefusal, match="epic:x item 42"):
        dispatch("99")
    setup.assert_not_called()
    owner = store.get(record.continuation_run_id)
    store.save(replace(owner, status="success"))
    assert dispatch("99") is None
    assert store.list_continuations("epic:x") == [record]


@pytest.mark.serial
@pytest.mark.parametrize("boundary", ["admission", "quarantine", "base", "host"])
def test_round20_recreated_checkout_refuses_consumers(
    native_abandonment, store, monkeypatch, boundary
):
    from test_worktree import _round20_recreate_checkout

    checkout, _, ctx = native_abandonment
    authority = dispatch_core._host_checkout_abandonment(ctx, ctx.config)
    dispatch_core._bind_publication_base(
        store,
        publish="canonical",
        base=ctx.publication_base_sha,
        run_id=ctx.run_id,
        issue_num=42,
        item_id="42",
        branch=ctx.branch,
        checkout=checkout,
    )
    before = store.read_chain_rows()
    _round20_recreate_checkout(checkout, worktree._repo_root(), ctx.branch)
    published = []
    monkeypatch.setattr(
        dispatch_core, "publish_checkout", lambda *a, **kw: published.append(kw)
    )
    monkeypatch.setattr(dispatch_core, "remote_branch_sha", lambda *a, **kw: "")
    with pytest.raises(
        worktree.CheckoutGenerationRefusal, match="directory binding mismatch"
    ):
        if boundary == "admission":
            dispatch_core._host_checkout_abandonment(ctx, ctx.config)
        elif boundary == "quarantine":
            worktree._quarantine_abandoned_checkout(checkout, ctx.branch, authority)
        elif boundary == "base":
            dispatch_core._bind_publication_base(
                store,
                publish="canonical",
                base=ctx.publication_base_sha,
                run_id=ctx.run_id,
                issue_num=42,
                item_id="42",
                branch=ctx.branch,
                checkout=checkout,
            )
        else:
            dispatch_core._git_push_branch(
                ctx.branch,
                checkout,
                repo=ctx.config.project.repo or None,
                base_sha=ctx.publication_base_sha,
                publish="canonical",
                store=store,
                run_id=ctx.run_id,
                item_id="42",
            )
    assert [
        row for row in store.read_chain_rows() if row.event != "publication_posture"
    ] == before
    assert not published
    assert (checkout / "external-edits.txt").exists()
    assert not list(checkout.parent.glob("issue-42.replaced-*"))


@pytest.mark.serial
@pytest.mark.parametrize("active_checkout", ["awaiting"], indirect=True)
def test_round20_epic_resume_refuses_recreated_checkout(
    active_checkout, store, monkeypatch
):
    from contextlib import nullcontext

    from test_worktree import _round20_recreate_checkout
    from typer.testing import CliRunner

    from kew import cli
    from kew.cli import epic

    checkout, ctx, record = active_checkout
    ctx.config.logging.dir = str(checkout.parent / "logs")
    dispatch_core._bind_publication_base(
        store,
        publish="canonical",
        base=ctx.publication_base_sha,
        run_id=record.original_run_id,
        issue_num=42,
        item_id="42",
        branch=ctx.branch,
        checkout=checkout,
    )
    _round20_recreate_checkout(checkout, worktree._repo_root(), ctx.branch)
    before = store.read_chain_rows()
    monkeypatch.setattr(cli, "load_config", lambda **kw: ctx.config)
    monkeypatch.setattr(cli, "_open_run_store", lambda *a: nullcontext(store))
    monkeypatch.setattr(cli, "preflight_tools", lambda: None)
    monkeypatch.setattr(cli, "_epic_repo", lambda _: "o/r")
    monkeypatch.setattr(cli, "build_notifier", lambda _: MagicMock())
    monkeypatch.setattr(epic, "_resume_owner_is_dead", lambda *a: True)
    monkeypatch.setattr("kew.graph.driver.tick._checkout_holds_work", lambda *a: True)
    dispatched = []
    monkeypatch.setattr(
        epic,
        "_run_resume_continuation",
        lambda *a, **kw: dispatched.append(kw) or DispatchOutcome(exit_code=0),
    )
    result = CliRunner().invoke(epic.epic_app, ["continue", record.epic_ref, "42"])
    assert result.exit_code == 2, result.output
    assert "directory binding mismatch" in result.output
    assert not dispatched
    assert store.read_chain_rows() == before
    assert (
        store.get_continuation(
            record.epic_ref, "42", record.original_run_id, ctx.branch, checkout
        )
        == record
    )
    assert (checkout / "external-edits.txt").exists()


@pytest.mark.serial
@pytest.mark.parametrize("occupant", [False, True])
def test_round20_missing_candidate_defers_bound_token(round18_rebind, store, occupant):
    recorded, candidate, ctx, record = round18_rebind
    authority = dispatch_core._host_checkout_abandonment(ctx, ctx.config)
    assert authority.checkout_id == "", (
        "an absent directory cannot carry a bound generation"
    )
    assert not worktree._generation_sidecar(candidate).exists()
    if occupant:
        candidate.mkdir(parents=True)
        (candidate / "edits.txt").write_text("arrived after admission")
        with pytest.raises(
            worktree.CheckoutGenerationRefusal, match="authority does not match"
        ):
            worktree._quarantine_abandoned_checkout(candidate, ctx.branch, authority)
        assert (candidate / "edits.txt").read_text() == "arrived after admission"
    else:
        assert _replace_native_checkout(ctx) == candidate
        _round18_replacement_evidence(store, recorded, candidate, ctx, record)
        assert worktree.read_checkout_generation(candidate)


class _Round21HostSink:
    def __init__(self, store, ctx):
        self.store = store
        self.ctx = ctx
        self.launched = []

    def dispatch(self, item_id):
        self.ctx.run_id = self.store.list_epic_reservations(self.ctx.epic_ref)[0].run_id
        checkout = _replace_native_checkout(self.ctx)
        dispatch_core._require_bound_checkout_generation(
            self.ctx,
            checkout,
            worktree.read_checkout_generation(checkout),
            boundary="round21 paid launch",
        )
        evidence = self.store.list_audit_events(event="checkout_replaced")[0]
        assert evidence.payload["legacy_sidecar_retired"] is True
        assert (
            self.store.continuation_expiry(
                self.store.list_continuations(self.ctx.epic_ref)[0]
            )
            is not None
        )
        self.launched.append(item_id)
        return True


@pytest.mark.serial
@pytest.mark.parametrize("publish", ["canonical", "checkout"])
def test_round21_origin_main_abandon_then_tick(store, tmp_path, monkeypatch, publish):
    from contextlib import nullcontext

    from support.driver_fakes import EPIC, _advance
    from test_driver_dispatch import _abandoned_dispatch_fixture
    from test_worktree import _git_out, _origin_repo_with_branch
    from typer.testing import CliRunner

    from kew import cli
    from kew.cli import epic
    from kew.subprocess_utils import TOOL_PATHS

    monkeypatch.setitem(TOOL_PATHS, "git", shutil.which("git"))
    repo = _origin_repo_with_branch(tmp_path, "agent/issue-42")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(worktree, "_repo_root", lambda: repo)
    config = DispatchConfig()
    config.branches.base = "main"
    config.branches.worktree_base = str(tmp_path / "worktrees")
    config.publication.publish = publish
    config.logging.dir = str(tmp_path)
    (tmp_path / "dispatch.db").touch()
    checkout = worktree.setup_worktree(issue_num=42, config=config)
    worktree.release_checkout_claims()
    token = "a" * 32
    worktree.revoke_checkout_generation(checkout)
    sidecar = worktree._generation_sidecar(checkout)
    sidecar.write_text(token + "\n")
    # Construct the stopped continuation before invoking the actual CLI verb.
    with monkeypatch.context() as scoped:
        scoped.setattr(verbs, "epic_abandon", MagicMock())
        _, kwargs = _abandoned_dispatch_fixture(store, tmp_path, monkeypatch)
    kwargs["config"] = config
    ctx = CheckoutBinding._context(dispatch_core, config, store, run_id="replacement")
    ctx.epic_ref = EPIC
    ctx.publication_base_sha = _git_out("rev-parse", "main", cwd=repo)
    monkeypatch.setattr(
        dispatch_core, "publication_remote", lambda _: str(tmp_path / "origin.git")
    )
    monkeypatch.setattr(cli, "load_config", lambda **kw: config)
    monkeypatch.setattr(cli, "_open_run_store", lambda *a: nullcontext(store))
    try:
        # No validated abandonment means no legacy migration or retirement.
        with (
            worktree.checkout_claim_guard(checkout),
            pytest.raises(
                worktree.CheckoutGenerationRefusal, match="directory binding mismatch"
            ),
        ):
            worktree.capture_checkout_generation(checkout)
        with pytest.raises(continuation_recovery.ActiveContinuationRefusal):
            dispatch_core._host_checkout_abandonment(ctx, config)
        assert sidecar.read_text() == token + "\n"
        worktree.release_checkout_claims()
        result = CliRunner().invoke(
            epic.epic_app, ["abandon", EPIC, "42", "--reason", "replace legacy work"]
        )
        assert result.exit_code == 0, result.output
        assert store.list_continuations(EPIC)[0].state == "terminal"
        assert sidecar.read_text() == token + "\n"
        retired = MagicMock(wraps=worktree.revoke_checkout_generation)
        monkeypatch.setattr(worktree, "revoke_checkout_generation", retired)
        sink = _Round21HostSink(store, ctx)
        kwargs["sink"] = sink
        advanced = _advance(**kwargs)
        assert advanced.dispatched == ("42",), advanced
        assert sink.launched == ["42"]
        retired.assert_any_call(checkout)
        assert worktree.read_checkout_generation(checkout) != token
        binding = json.loads(sidecar.read_text())
        assert (checkout / ".git" / "kew-generation").read_text().strip() == binding[
            "token"
        ]
        if hasattr(checkout.stat(), "st_birthtime"):
            assert binding["birth_field"] == "st_birthtime"
            assert binding["birth_time"] == checkout.stat().st_birthtime
        else:
            assert binding["birth_field"] == "none"
            assert "birth_time" not in binding
        preserved = list(checkout.parent.glob("issue-42.replaced-*"))
        assert len(preserved) == 1
        assert (preserved[0] / "edits.txt").read_text() == "abandoned work"
        assert not (preserved[0] / ".git" / "kew-generation").exists()
        assert not (checkout / "edits.txt").exists()
        # Consumed authority cannot retire a later token-only sidecar.
        sidecar.write_text(token + "\n")
        retired.reset_mock()
        with pytest.raises(continuation_recovery.RetainedReplacementRefusal):
            dispatch_core._host_checkout_abandonment(ctx, config)
        retired.assert_not_called()
        assert sidecar.read_text() == token + "\n"
    finally:
        worktree.release_checkout_claims()


@pytest.mark.serial
@pytest.mark.parametrize("active_checkout", ["awaiting"], indirect=True)
def test_round21_epic_resume_refuses_reused_identity(
    active_checkout, store, monkeypatch
):
    from contextlib import nullcontext

    from test_worktree import _round21_replace_reusing_identity
    from typer.testing import CliRunner

    from kew import cli
    from kew.cli import epic

    checkout, ctx, record = active_checkout
    refusal_factor = (
        "birth identity" if hasattr(checkout.stat(), "st_birthtime") else "nonce"
    )
    ctx.config.logging.dir = str(checkout.parent / "logs")
    dispatch_core._bind_publication_base(
        store,
        publish="canonical",
        base=ctx.publication_base_sha,
        run_id=record.original_run_id,
        issue_num=42,
        item_id="42",
        branch=ctx.branch,
        checkout=checkout,
    )
    _round21_replace_reusing_identity(
        monkeypatch, checkout, source=worktree._repo_root(), branch=ctx.branch
    )
    before = store.read_chain_rows()
    monkeypatch.setattr(cli, "load_config", lambda **kw: ctx.config)
    monkeypatch.setattr(cli, "_open_run_store", lambda *a: nullcontext(store))
    monkeypatch.setattr(cli, "preflight_tools", lambda: None)
    monkeypatch.setattr(cli, "_epic_repo", lambda _: "o/r")
    monkeypatch.setattr(cli, "build_notifier", lambda _: MagicMock())
    monkeypatch.setattr(epic, "_resume_owner_is_dead", lambda *a: True)
    monkeypatch.setattr("kew.graph.driver.tick._checkout_holds_work", lambda *a: True)
    dispatched = MagicMock(return_value=DispatchOutcome(exit_code=0))
    monkeypatch.setattr(epic, "_run_resume_continuation", dispatched)
    result = CliRunner().invoke(epic.epic_app, ["continue", record.epic_ref, "42"])
    assert result.exit_code == 2, result.output
    assert refusal_factor in result.output
    dispatched.assert_not_called()
    assert store.read_chain_rows() == before
    assert (
        store.get_continuation(
            record.epic_ref, "42", record.original_run_id, ctx.branch, checkout
        )
        == record
    )
    assert (checkout / "foreign.txt").exists()


def _bind_launch_checkout(store, publish, checkout, *, run_id="original"):
    """Record one run's launch binding the way its publication mode writes it."""
    dispatch_core._bind_publication_base(
        store,
        publish=publish,
        base="a" * 40,
        run_id=run_id,
        issue_num=42,
        item_id="42",
        branch="agent/issue-42",
        checkout=checkout,
    )


def _launch_checkout(tmp_path, name="kew-sandbox-42-ab12cd"):
    """A standalone clone where a CONTAINED dispatch puts one: a temp directory."""
    checkout = tmp_path / "containers" / name
    checkout.mkdir(parents=True)
    (checkout / ".git").mkdir()
    worktree.write_checkout_generation(checkout)
    return checkout


@pytest.mark.parametrize("publish", ["checkout", "canonical"])
def test_a_runs_recorded_checkout_is_readable_without_knowing_its_path(
    store, tmp_path, publish
):
    """A recovery that must WRITE to a dead run's checkout cannot derive it.

    A contained dispatch's checkout is the clone kew made in a temp directory,
    so the deterministic host worktree path names the wrong directory entirely.
    Both publication modes record the real one — under different event names —
    and the generation comes back with it so the writer can require that exact
    directory first.
    """
    checkout = _launch_checkout(tmp_path)
    _bind_launch_checkout(store, publish, checkout)

    found = continuation_recovery.recorded_run_checkout(
        store, run_id="original", item_id="42", branch="agent/issue-42"
    )

    assert found is not None
    recorded, generation = found
    assert recorded == checkout.resolve()
    assert generation == worktree.read_checkout_generation(checkout)


def test_a_run_whose_recorded_checkouts_disagree_names_none_of_them(store, tmp_path):
    """Conflicting evidence is "no checkout a writer may touch", not a guess.

    Picking either one would hand a recovery a directory the run may never have
    had, and the bytes being recovered are the only copy of the work.
    """
    first = _launch_checkout(tmp_path)
    second = _launch_checkout(tmp_path, name="kew-sandbox-42-ef34gh")
    _bind_launch_checkout(store, "checkout", first)
    _bind_launch_checkout(store, "canonical", second)

    assert (
        continuation_recovery.recorded_run_checkout(
            store, run_id="original", item_id="42", branch="agent/issue-42"
        )
        is None
    )


def test_another_runs_binding_is_not_this_runs_checkout(store, tmp_path):
    """The binding is keyed to the run, so a sibling's checkout is invisible."""
    _bind_launch_checkout(store, "checkout", _launch_checkout(tmp_path))

    assert (
        continuation_recovery.recorded_run_checkout(
            store, run_id="other-run", item_id="42", branch="agent/issue-42"
        )
        is None
    )


def test_a_run_that_bound_a_second_checkout_still_recognizes_both(store, tmp_path):
    """Asking "is this path mine" must survive a run naming more than one.

    A gateway-restart recovery merges a restarted sandbox into the HOST
    dispatch checkout and binds the dead run there as well as at the clone it
    launched from, so from that moment the run names two checkouts. The
    writer-facing accessor refuses to pick between them, by design — but the
    orphan path still has to recognize the host one as this run's own, or the
    recovered work is read as some other dispatch's leftover and never offered
    as a continuation.
    """
    launched = _launch_checkout(tmp_path)
    recovered = _launch_checkout(tmp_path, name="issue-42")
    _bind_launch_checkout(store, "checkout", launched)
    _bind_launch_checkout(store, "checkout", recovered)

    recorded = continuation_recovery.recorded_run_checkouts(
        store, run_id="original", item_id="42", branch="agent/issue-42"
    )

    assert recorded == frozenset({str(launched.resolve()), str(recovered.resolve())})
    assert (
        continuation_recovery.recorded_run_checkout(
            store, run_id="original", item_id="42", branch="agent/issue-42"
        )
        is None
    )


def test_a_run_with_no_binding_names_no_checkout_at_all(store, tmp_path):
    """An empty answer is what tells a reader to leave an existing tree alone."""
    _bind_launch_checkout(store, "checkout", _launch_checkout(tmp_path))

    assert (
        continuation_recovery.recorded_run_checkouts(
            store, run_id="other-run", item_id="42", branch="agent/issue-42"
        )
        == frozenset()
    )
