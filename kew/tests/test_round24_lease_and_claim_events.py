"""Atomic continuation claims and unconditional checkout leases."""

import shutil
import sqlite3
import subprocess
import sys
from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime
from functools import partial, partialmethod
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from support.checkout_generations import (
    fixture_checkout_generation,
    fixture_claim_evidence,
)
from test_loops_delegate import _FakeGate, _loop
from test_round23_loop_resume_claim import _Contender
from typer.testing import CliRunner

from kew import cli, worktree
from kew.audit_identity import continuation_evidence_identity
from kew.cli import epic as epic_cli
from kew.config import DispatchConfig
from kew.graph.driver import tick
from kew.graph.driver.dispatch import DetachedDispatchSink, reserve_and_dispatch
from kew.loops import delegate
from kew.loops.verification import GateVerdict
from kew.run_store import RunRecord, SqliteRunStore
from kew.runners.registry import ResolvedRoute
from kew.service import verbs


@pytest.fixture
def scene(tmp_path, monkeypatch):
    config = DispatchConfig()
    config.project.repo = "o/r"
    config.publication.publish = "checkout"
    config.sandbox.dangerously_skip_containment = True
    config.branches.worktree_base = str(tmp_path / "checkouts")
    config.logging.dir = str(tmp_path / "logs")
    config.agent.model = "claude-sonnet-4-6"
    monkeypatch.setenv("KEW_ACTOR", "operator")
    with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
        checkout = worktree.dispatch_checkout_path(config, issue_num=7)
        (checkout / ".git" / "objects").mkdir(parents=True)
        (checkout / ".git" / "HEAD").write_text("ref: refs/heads/agent/issue-7\n")
        (checkout / "wip.txt").write_text("preserved work")
        generation = worktree.write_checkout_generation(checkout)
        store.save(
            RunRecord(
                run_id="original",
                issue_num=7,
                item_id="7",
                branch="agent/issue-7",
                status="failure",
                exit_code=1,
                started_at=datetime.now(UTC),
                finished_at=datetime.now(UTC),
            )
        )
        record = store.record_continuation(
            "epic:x",
            "7",
            "original",
            "agent/issue-7",
            checkout,
            checkout_generation=fixture_checkout_generation(checkout),
        )
        context = SimpleNamespace(
            config=config,
            store=store,
            checkout=checkout,
            record=record,
            generation=generation,
            root=tmp_path,
        )
        try:
            yield context
        finally:
            worktree.release_checkout_claims()


def _claim(scene):
    r = scene.record
    return scene.store.claim_continuation_under_cap(
        r.epic_ref,
        r.item_id,
        r.original_run_id,
        r.branch,
        r.worktree_identity,
        "continued",
        10,
        claim_evidence=fixture_claim_evidence(
            scene.store,
            r.epic_ref,
            r.item_id,
            r.original_run_id,
            r.branch,
            r.worktree_identity,
            "continued",
        ),
    )


@pytest.fixture(params=["file", "memory"])
def claim_scene(scene, request):
    if request.param == "file":
        yield scene
        return
    with SqliteRunStore(":memory:") as store:
        record = store.record_continuation(
            "epic:x",
            "7",
            "original",
            "agent/issue-7",
            scene.checkout,
            checkout_generation=fixture_checkout_generation(scene.checkout),
        )
        yield SimpleNamespace(**{**vars(scene), "store": store, "record": record})


@pytest.mark.parametrize("failure", ["raise", "drop"])
def test_round24_claim_event_failure_rolls_back_every_write(
    claim_scene, monkeypatch, failure
):
    before = claim_scene.store.list_continuations("epic:x")
    failed = (
        Mock(side_effect=OSError("audit failed"))
        if failure == "raise"
        else Mock(return_value=None)
    )
    monkeypatch.setattr(claim_scene.store, "_chained_insert_in_transaction", failed)
    with pytest.raises((OSError, RuntimeError), match="audit failed|durably recorded"):
        _claim(claim_scene)
    assert claim_scene.store.list_continuations("epic:x") == before
    assert claim_scene.store.list_epic_reservations("epic:x") == []
    assert claim_scene.store.list_audit_events(event="continuation_claimed") == []
    assert not claim_scene.store._conn.in_transaction


class _ObserveClaimInsert:
    def __init__(self, scene):
        self.scene = scene
        self.insert = scene.store._chained_insert_in_transaction
        self.observed = []

    def __call__(self, values, **kwargs):
        if values[1] == "continuation_claimed":
            store = self.scene.store
            assert store._conn.in_transaction
            [claimed] = store.list_continuations("epic:x")
            assert (claimed.state, claimed.generation) == ("continuing", 1)
            assert len(store.list_epic_reservations("epic:x")) == 1
            with sqlite3.connect(store._db_path) as reader:
                assert reader.execute(
                    "SELECT state, generation FROM epic_continuations"
                ).fetchall() == [("awaiting_continuation", 0)]
                assert (
                    reader.execute("SELECT * FROM epic_reservations").fetchall() == []
                )
                assert (
                    reader.execute(
                        "SELECT * FROM audit_events"
                        " WHERE event = 'continuation_claimed'"
                    ).fetchall()
                    == []
                )
            self.observed.append(values[2])
        return self.insert(values, **kwargs)


def test_round24_claim_commit_has_no_crash_window(scene, monkeypatch):
    observe = _ObserveClaimInsert(scene)
    monkeypatch.setattr(scene.store, "_chained_insert_in_transaction", observe)
    assert _claim(scene) == "claimed"
    # Stop here, where the old caller had not yet appended any claim evidence.
    with SqliteRunStore(scene.store._db_path) as recovered:
        [event] = recovered.list_audit_events(event="continuation_claimed")
        [claimed] = recovered.list_continuations("epic:x")
        assert (claimed.state, claimed.generation) == ("continuing", 1)
        assert len(recovered.list_epic_reservations("epic:x")) == 1
        assert event.payload["evidence_identity"] == continuation_evidence_identity(
            "continuation_claimed", event.payload
        )
    assert observe.observed == ["continued"]


def test_round24_service_claim_is_atomic_and_reconcile_does_not_duplicate(
    scene, monkeypatch
):
    observe = _ObserveClaimInsert(scene)
    monkeypatch.setattr(scene.store, "_chained_insert_in_transaction", observe)
    claimed = verbs.epic_resume(
        scene.store, epic_ref="epic:x", item_id="7", config=scene.config
    )
    scene.store.save(
        replace(scene.store.get("original"), run_id=claimed.continuation_run_id)
    )
    emit = Mock(wraps=tick._emit_continuation_state)
    monkeypatch.setattr(tick, "_emit_continuation_state", emit)
    tick._attest_continuing_continuation(scene.store, scene.config, claimed)
    assert observe.observed == [claimed.continuation_run_id]
    assert len(scene.store.list_audit_events(event="continuation_claimed")) == 1
    assert all(
        call.kwargs["event"] != "continuation_claimed" for call in emit.call_args_list
    )


@pytest.mark.parametrize("caller", ["orphan", "missing"])
def test_round24_recovery_callers_never_append_claim_events(scene, monkeypatch, caller):
    assert _claim(scene) == "claimed"
    [owned] = scene.store.list_continuations("epic:x")
    [reservation] = scene.store.list_epic_reservations("epic:x")
    emit = Mock(wraps=tick._emit_continuation_state)
    monkeypatch.setattr(tick, "_emit_continuation_state", emit)
    worktree.claim_checkout(scene.checkout)
    if caller == "orphan":
        run = replace(
            scene.store.get("original"),
            run_id="continued",
            status="pending",
            finished_at=None,
            attempt_pgid=2**31 - 1,
        )
        scene.store.save(run)
        assert tick._preserve_claimed_dirty_orphan(
            scene.store,
            scene.config,
            reservation,
            run,
            scene.checkout,
            scene.generation,
            owned,
            now=datetime.now(UTC),
        )
    else:
        tick._reopen_missing_run_continuation(
            scene.store,
            scene.config,
            reservation,
            owned,
            scene.checkout,
            scene.generation,
        )
    assert all(
        call.kwargs["event"] != "continuation_claimed" for call in emit.call_args_list
    )
    assert len(scene.store.list_audit_events(event="continuation_claimed")) == 1
    [released] = scene.store.list_continuations("epic:x")
    assert released.state == "awaiting_continuation"


def _loop_scene(scene, monkeypatch, *, bind=True):
    loop = replace(
        _loop(),
        status="paused",
        status_reason="operator pause",
        model=scene.config.agent.model,
    )
    scene.store.save_loop(loop)
    driver = delegate.DelegateDriver(
        scene.store,
        config=scene.config,
        gate=_FakeGate(GateVerdict(passed=True, evidence="ok", sabotage=False)),
    )
    if bind:
        worktree.claim_checkout(scene.checkout)
        try:
            driver._bind_publication(
                loop,
                scene.checkout,
                delegate._PublicationBinding(
                    "loop-owner", "", "", checkout_generation=scene.generation
                ),
            )
        finally:
            worktree.release_checkout_claim(scene.checkout)
    route = ResolvedRoute(
        runner=SimpleNamespace(name="claude-code", loop_style="native"),
        model=loop.model,
    )
    monkeypatch.setattr(delegate, "_resolve_resume_route", Mock(return_value=route))
    monkeypatch.setattr(delegate, "build_prompt", Mock(return_value="prompt"))
    monkeypatch.setattr(delegate, "build_repo_map", Mock(return_value=""))
    monkeypatch.setattr(
        delegate, "_resolved_runner_preflight_failure", Mock(return_value=None)
    )
    monkeypatch.setattr(driver, "_run_session", Mock(return_value=loop))
    return loop, driver


def test_round24_checkout_loop_binding_matches_then_refuses_replacement(
    scene, monkeypatch
):
    loop, driver = _loop_scene(scene, monkeypatch)
    git = Mock(return_value=SimpleNamespace(returncode=0, stdout="agent/issue-7\n"))
    monkeypatch.setattr(worktree, "run", git)
    assert driver.resume(loop, "continue") is loop
    assert git.call_count == 1
    session_binding = driver._run_session.call_args.kwargs["binding"]
    git.reset_mock()
    driver._run_session.reset_mock()
    loop.status = "paused"
    scene.store.save_loop(loop)
    before = scene.store.get_loop(loop.loop_id)
    # Model a later dispatch replacing the directory and minting a new nonce.
    worktree.claim_checkout(scene.checkout)
    try:
        shutil.rmtree(scene.checkout)
        (scene.checkout / ".git" / "objects").mkdir(parents=True)
        (scene.checkout / ".git" / "HEAD").write_text("ref: refs/heads/agent/issue-7\n")
        worktree.revoke_checkout_generation(scene.checkout)
        assert worktree.write_checkout_generation(scene.checkout) != scene.generation
    finally:
        worktree.release_checkout_claim(scene.checkout)
    with pytest.raises(RuntimeError, match="directory binding mismatch"):
        driver.resume(loop, "continue")
    git.assert_not_called()
    driver._run_session.assert_not_called()
    assert session_binding.checkout_generation == scene.generation
    assert scene.store.get_loop(loop.loop_id) == before


def test_round24_prebinding_canonical_loop_refuses_naming_loop_run(scene, monkeypatch):
    # A loop that predates recorded checkout bindings resumes in checkout mode
    # by adopting its checkout (governed review round 1, 2026-09-21;
    # test_governed_round2_loop_adoption.py). In canonical mode the recorded
    # base is the publication authority, so the resume still refuses before
    # touching the checkout, and the refusal names a verb that exists.
    scene.config.publication.publish = "canonical"
    loop, driver = _loop_scene(scene, monkeypatch, bind=False)
    git = Mock(return_value=SimpleNamespace(returncode=0, stdout="agent/issue-7\n"))
    monkeypatch.setattr(worktree, "run", git)
    with pytest.raises(RuntimeError, match="kew dispatch <issue> --loop"):
        driver.resume(loop, "continue")
    git.assert_not_called()
    driver._run_session.assert_not_called()


def test_round24_restore_requires_nonempty_expected_generation(scene, monkeypatch):
    _loop_scene(scene, monkeypatch)
    worktree.claim_checkout(scene.checkout)
    git = Mock(return_value=SimpleNamespace(returncode=0, stdout="agent/issue-7\n"))
    monkeypatch.setattr(worktree, "run", git)
    with pytest.raises(RuntimeError, match="kew dispatch <issue> --loop"):
        worktree.reuse_or_restore_worktree(issue_num=7, config=scene.config)
    git.assert_not_called()


def _record_path_inspection(path, scene, calls, original, *args, **kwargs):
    if path == scene.checkout or scene.checkout in path.parents:
        calls.append(str(path))
    return original(path, *args, **kwargs)


def _configure_epic_cli(scene, monkeypatch):
    monkeypatch.setattr(cli, "load_config", Mock(return_value=scene.config))
    monkeypatch.setattr(cli, "preflight_tools", Mock())
    monkeypatch.setattr(cli, "_epic_repo", Mock(return_value="o/r"))
    monkeypatch.setattr(cli, "build_notifier", Mock())
    monkeypatch.setattr(
        cli, "_open_run_store", Mock(return_value=nullcontext(scene.store))
    )
    monkeypatch.setattr(epic_cli, "_resume_owner_is_dead", Mock(return_value=True))


def test_round24_epic_resume_busy_never_inspects_checkout(scene, monkeypatch):
    _configure_epic_cli(scene, monkeypatch)
    inspections = []
    for name in ("resolve", "is_dir", "stat", "lstat"):
        original = getattr(Path, name)
        monkeypatch.setattr(
            Path,
            name,
            partialmethod(_record_path_inspection, scene, inspections, original),
        )
    dispatch = Mock(return_value=SimpleNamespace(exit_code=0))
    monkeypatch.setattr(epic_cli, "_run_resume_continuation", dispatch)
    for publish in ("canonical", "checkout"):
        scene.config.publication.publish = publish
        holder = _Contender(scene.checkout).start()
        try:
            assert holder.error is None
            inspections.clear()
            result = CliRunner().invoke(cli.epic_app, ["continue", "epic:x", "7"])
            assert result.exit_code == 2, (result.output, result.exception)
            assert inspections == [], "busy CLI inspected the claimed checkout"
            assert "claimed by another" in result.output
            dispatch.assert_not_called()
        finally:
            holder.stop()


def _inspect_resume_dispatch(scene, fail, *_args, **_kwargs):
    worktree.require_checkout_claim(scene.checkout)
    holder = _Contender(scene.checkout).start()
    try:
        assert isinstance(holder.error, worktree.CheckoutBusy)
    finally:
        holder.stop()
    if fail:
        raise RuntimeError("dispatch failed")
    return SimpleNamespace(exit_code=0)


@pytest.mark.parametrize("fail", [False, True])
def test_round24_epic_resume_holds_lease_through_dispatch_then_releases(
    scene, monkeypatch, fail
):
    _configure_epic_cli(scene, monkeypatch)
    monkeypatch.setattr(epic_cli, "_require_legacy_continuation", Mock())
    monkeypatch.setattr(
        epic_cli,
        "_run_resume_continuation",
        partial(_inspect_resume_dispatch, scene, fail),
    )
    result = CliRunner().invoke(cli.epic_app, ["continue", "epic:x", "7"])
    if fail:
        assert (
            isinstance(result.exception, RuntimeError)
            and str(result.exception) == "dispatch failed"
        )
    else:
        assert result.exit_code == 0, (result.output, result.exception)
    holder = _Contender(scene.checkout).start()
    try:
        assert holder.error is None, "finished CLI leaked its checkout lease"
    finally:
        holder.stop()


class _AbandonHolder(_Contender):
    """Hold the exact guard injected into the abandonment service."""

    def run(self):
        try:
            with worktree.checkout_claim_guard(self.checkout):
                self.ready.set()
                self.done.wait()
        except BaseException as exc:
            self.error = exc
        finally:
            self.ready.set()


def test_round24_driver_refuses_abandon_lease_before_inspection(scene, monkeypatch):
    inspections = []
    for name in ("resolve", "is_dir", "stat", "lstat"):
        original = getattr(Path, name)
        monkeypatch.setattr(
            Path,
            name,
            partialmethod(_record_path_inspection, scene, inspections, original),
        )
    inspect_checkout = Mock(return_value=True)
    monkeypatch.setattr(tick, "_validate_preexisting_worktree", inspect_checkout)
    for publish in ("canonical", "checkout"):
        scene.config.publication.publish = publish
        holder = _AbandonHolder(scene.checkout).start()
        try:
            assert holder.error is None
            with pytest.raises(worktree.CheckoutBusy):
                tick._claim_and_validate_continuation_checkout(
                    scene.store, scene.config, scene.record, boundary="abandon race"
                )
            inspect_checkout.assert_not_called()
            assert inspections == []
        finally:
            holder.stop()


_CHILD_CLAIM = """
import sys
from pathlib import Path
from kew.worktree import CheckoutBusy, claim_checkout, release_checkout_claim
checkout = Path(sys.argv[1])
try:
    claim_checkout(checkout)
except CheckoutBusy:
    print('busy')
else:
    print('acquired')
    release_checkout_claim(checkout)
"""


def _process_claim(scene):
    # The child program is fixed; all arguments come from this test fixture.
    child = subprocess.run(  # nosec B603
        [
            sys.executable,
            "-c",
            _CHILD_CLAIM,
            str(scene.checkout),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return child.stdout.strip()


@pytest.mark.serial
def test_round24_checkout_mode_claim_uses_process_flock(scene):
    worktree.claim_checkout(scene.checkout)
    try:
        assert _process_claim(scene) == "busy"
    finally:
        worktree.release_checkout_claim(scene.checkout)
    assert _process_claim(scene) == "acquired"


def _validate_under_lease(scene, order, *_args):
    worktree.require_checkout_claim(scene.checkout)
    order.append("validate")
    return False


def _spawn_after_release(
    scene,
    order,
    cmd,
    *,
    stdin,
    stdout,
    stderr,
    env,
    start_new_session,
    credential,
    issued_key_handoff,
    retain_credentials,
):
    # Main's detached launch (#1957) passes the child env and bounded-key
    # arguments; this scene's non-OpenRouter route mints no key.
    assert cmd and start_new_session is True
    assert stdin == stdout == stderr == subprocess.DEVNULL
    assert isinstance(env, dict)
    assert credential is None
    assert issued_key_handoff is True
    assert retain_credentials is not None
    assert scene.checkout.absolute() not in worktree._checkout_claims
    order.append("spawn")
    assert _process_claim(scene) == "acquired"
    order.append("child claimed")
    return SimpleNamespace(pid=123)


def _register_for_spawn(scene, run_id):
    return tick._register_preexisting_dirty_worktree(
        scene.store,
        scene.config,
        epic_ref="epic:x",
        item_id="7",
        provider="github",
        liveness=None,
        reservation_run_id=run_id,
    )


@pytest.mark.serial
def test_round24_driver_releases_before_detached_child_claim(scene, monkeypatch):
    order = []
    # A clean checkout has no existing recovery family for this dispatch.
    monkeypatch.setattr(tick, "_preexisting_continuation", Mock(return_value=None))
    monkeypatch.setattr(
        tick,
        "_validate_preexisting_worktree",
        partial(_validate_under_lease, scene, order),
    )
    registrar = partial(_register_for_spawn, scene)
    sink = DetachedDispatchSink(
        epic_ref="epic:x",
        config=scene.config,
        store=scene.store,
        popen=partial(_spawn_after_release, scene, order),
        publication_token=Mock(return_value=None),
    )
    monkeypatch.setattr(
        "kew.graph.driver.dispatch.resolve_kew_console_script", Mock(return_value="kew")
    )
    for publish in ("canonical", "checkout"):
        scene.config.publication.publish = publish
        outcome = reserve_and_dispatch(
            "7",
            epic_ref="epic:x",
            store=scene.store,
            sink=sink,
            branch_exists=Mock(return_value=False),
            register_continuation=registrar,
        )
        assert outcome == "dispatched"
        assert order == ["validate", "spawn", "child claimed"]
        order.clear()
        scene.store.release_epic_node_item("7")


def _claim_then_stop(checkout, *_args, **_kwargs):
    worktree.require_checkout_claim(checkout)
    raise RuntimeError("claimed before clone")


def test_round24_temporary_dispatch_claims_before_clone(scene, monkeypatch):
    import tempfile

    from kew import dispatch_core

    monkeypatch.setattr(tempfile, "mkdtemp", Mock(return_value=str(scene.checkout)))
    monkeypatch.setattr(
        dispatch_core, "run", Mock(return_value=SimpleNamespace(stdout=str(scene.root)))
    )
    monkeypatch.setattr(
        worktree, "clone_local_checkout", partial(_check_clone_lease, scene.checkout)
    )
    with pytest.raises(RuntimeError, match="claimed before clone"):
        dispatch_core._materialize_checkout("7", "agent/issue-7", scene.config)


def _check_clone_lease(checkout, *_args, **_kwargs):
    _claim_then_stop(checkout)


def test_round24_temporary_reviewer_claims_before_clone_and_releases(
    scene, monkeypatch
):
    import tempfile

    from kew import review_dispatch

    monkeypatch.setattr(tempfile, "mkdtemp", Mock(return_value=str(scene.checkout)))
    monkeypatch.setattr(
        worktree, "clone_local_checkout", partial(_check_clone_lease, scene.checkout)
    )
    with pytest.raises(RuntimeError, match="claimed before clone"):
        review_dispatch.materialize_reviewer_worktree(scene.root, "a" * 40, "main")
    assert scene.checkout.absolute() not in worktree._checkout_claims


def test_round24_planning_claims_before_setup_and_releases(scene, monkeypatch):
    from kew.graph import plan_run

    checkout = Path(scene.config.branches.worktree_base) / "agent" / "epic-plan-test"
    monkeypatch.setattr(
        plan_run, "_setup_plan_worktree", partial(_claim_then_stop, checkout)
    )
    # Main's two-axis scheme (#1943) returns planning failures as an outcome:
    # setup that fails under the lease is could-not-run, never a raise.
    outcome = plan_run._dispatch_plan_agent(
        name="test",
        plan_path=scene.root / "plan.md",
        config=scene.config,
        store=scene.store,
        item_id="epic-plan:test",
    )
    assert outcome.could_not_run is True
    assert outcome.launched is False
    assert outcome.manifest_path is None
    assert outcome.failure == "claimed before clone"
    assert checkout.absolute() not in worktree._checkout_claims


def test_round24_legacy_replacement_retains_lease_until_clone(scene, monkeypatch):
    shutil.rmtree(scene.checkout / ".git")
    (scene.checkout / ".git").write_text("gitdir: legacy\n")
    monkeypatch.setattr(worktree, "run", Mock())
    monkeypatch.setattr(worktree, "has_uncommitted_changes", Mock(return_value=False))
    monkeypatch.setattr(worktree, "_repo_root_or_none", Mock(return_value=None))
    monkeypatch.setattr(worktree, "_create_fresh_worktree", _claim_then_stop)
    with pytest.raises(RuntimeError, match="claimed before clone"):
        worktree.checkout_existing_worktree(
            issue_num=7,
            branch_name="agent/issue-7",
            config=scene.config,
        )
