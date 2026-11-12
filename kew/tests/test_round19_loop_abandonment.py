"""Loop checkout replacement consumes the same authority as host dispatch."""

import json
import os
import shutil
from dataclasses import replace

import pytest
from test_continuation_recovery import native_abandonment as native_abandonment
from test_continuation_recovery import store as store
from test_liveness_owner import process_table as process_table
from test_loops_delegate import _FakeGate, _loop, _patch_run

from kew import dispatch_core, worktree
from kew.continuation_recovery import (
    RetainedReplacementRefusal,
    current_continuation,
    replacement_was_withdrawn,
)
from kew.loops import delegate
from kew.loops.verification import GateVerdict
from kew.run_store import SqliteRunStore
from kew.tracking import DispatchResult


def _driver(monkeypatch, tmp_path, store, ctx, *, run_id="loop-replacement"):
    prepare = delegate._prepare_worktree
    _patch_run(monkeypatch, tmp_path)
    monkeypatch.setattr(delegate, "_prepare_worktree", prepare)
    monkeypatch.setattr(
        delegate, "_resolved_runner_preflight_failure", lambda *_a: None
    )
    monkeypatch.setattr(delegate, "build_repo_map", lambda *_a, **_k: "")
    loop = replace(
        _loop(),
        issue_num=42,
        item_id="42",
        loop_id=f"goal-{run_id}",
        model="claude-sonnet-4-6",
    )
    store.save_loop(loop)
    driver = delegate.DelegateDriver(
        store,
        config=ctx.config,
        gate=_FakeGate(GateVerdict(passed=True, evidence="ok", sabotage=False)),
    )
    monkeypatch.setattr(
        driver,
        "_new_publication_binding",
        lambda: delegate._PublicationBinding(run_id, ctx.publication_base_sha, run_id),
    )
    monkeypatch.setattr(driver, "_publish_iteration", lambda *_a: None)
    monkeypatch.setattr(driver, "_decide", lambda *_a, **_k: ("converged", "verified"))
    launched = []

    def launch(**kwargs):
        launched.append(kwargs["run_id"])
        check = kwargs.get("paid_launch_check")
        if check is not None:
            check()
        return DispatchResult(
            exit_code=0,
            issue_num=42,
            run_id=kwargs["run_id"],
            log_file=tmp_path / "run.log",
            event_file=tmp_path / "run.events.jsonl",
        )

    monkeypatch.setattr(delegate, "run_tracked_agent", launch)
    return driver, loop, launched


def _evidence(store, record, run_id, checkout):
    events = store.list_audit_events(event="checkout_replaced", run_id=run_id)
    assert len(events) == 1, "loop replacement must be recorded before launch"
    payload = events[0].payload
    assert payload["checkout"] == str(checkout.resolve())
    assert payload["epic_ref"] == record.epic_ref
    assert payload["item_id"] == "42"
    assert payload["branch"] == record.branch
    assert payload["original_run_id"] == record.original_run_id
    assert payload["generation"] == record.generation
    assert payload["abandonment_identity"] == store.continuation_abandonment_identity(
        record
    )
    assert payload["abandoned_checkout_id"] != payload["checkout_id"]
    expiry = store.continuation_expiry(record)
    assert expiry is not None, "loop replacement must persist expiry before launch"
    assert expiry.replacing_run_id == run_id
    assert expiry.replacing_checkout_id == payload["checkout_id"]
    assert not current_continuation(store, record, "42")
    with SqliteRunStore(store._db_path) as reopened:
        assert reopened.continuation_expiry(record) == expiry
        assert not current_continuation(reopened, record, "42")
    return payload


@pytest.mark.serial
@pytest.mark.parametrize("publish", ["canonical", "checkout"])
def test_round19_same_path_consumes_before_first_step(
    native_abandonment, store, monkeypatch, tmp_path, publish
):
    checkout, _, ctx = native_abandonment
    ctx.config.publication.publish = publish
    if publish == "checkout":
        ctx.config.sandbox.dangerously_skip_containment = True
    record = store.list_continuations(ctx.epic_ref)[0]
    old_token = worktree.read_checkout_generation(checkout)
    driver, loop, launched = _driver(monkeypatch, tmp_path, store, ctx)
    launch = delegate.run_tracked_agent

    def first_step(**kwargs):
        assert checkout.absolute() in worktree._checkout_claims
        payload = _evidence(store, record, kwargs["run_id"], checkout)
        assert payload["abandoned_checkout_id"] == old_token
        assert "rebound_from" not in payload
        return launch(**kwargs)

    monkeypatch.setattr(delegate, "run_tracked_agent", first_step)
    assert driver.run(loop).status == "converged"
    assert launched == ["loop-replacement"]
    preserved = list(checkout.parent.glob("issue-42.replaced-*"))
    assert len(preserved) == 1
    assert (preserved[0] / "edits.txt").read_text() == "abandoned work"


@pytest.mark.serial
def test_round19_moved_base_records_rebound_before_first_step(
    native_abandonment, store, monkeypatch, tmp_path
):
    recorded, _, ctx = native_abandonment
    record = store.list_continuations(ctx.epic_ref)[0]
    shutil.rmtree(recorded.parent.parent)
    ctx.config.branches.worktree_base = str(tmp_path / "moved-base")
    checkout = worktree.dispatch_checkout_path(ctx.config, issue_num=42)
    driver, loop, launched = _driver(monkeypatch, tmp_path, store, ctx)
    launch = delegate.run_tracked_agent

    def first_step(**kwargs):
        payload = _evidence(store, record, kwargs["run_id"], checkout)
        assert payload["rebound_from"] == str(recorded)
        assert payload["rebound_reason"] == "recorded checkout missing"
        return launch(**kwargs)

    monkeypatch.setattr(delegate, "run_tracked_agent", first_step)
    driver.run(loop)
    assert launched == ["loop-replacement"]
    assert worktree.is_safe_standalone_clone(checkout)


@pytest.mark.serial
@pytest.mark.parametrize("candidate_exists", [False, True])
def test_round19_existing_recorded_path_refuses_without_writes(
    native_abandonment, store, monkeypatch, tmp_path, candidate_exists
):
    recorded, _, ctx = native_abandonment
    ctx.config.branches.worktree_base = str(tmp_path / "moved-base")
    candidate = worktree.dispatch_checkout_path(ctx.config, issue_num=42)
    if candidate_exists:
        candidate.mkdir(parents=True)
    driver, loop, launched = _driver(monkeypatch, tmp_path, store, ctx)
    before = store.read_chain_rows()
    writes_before = store._conn.total_changes
    with pytest.raises(
        RuntimeError,
        match="^abandonment authority does not match the replacement checkout$",
    ):
        driver.run(loop)
    assert not launched
    assert store.read_chain_rows() == before
    assert store._conn.total_changes == writes_before
    assert (recorded / "edits.txt").read_text() == "abandoned work"
    assert not worktree._generation_sidecar(candidate).exists()
    assert candidate.exists() is candidate_exists


@pytest.mark.serial
def test_round19_consumed_once_refuses_host_and_loop_after_retention(
    native_abandonment, store, monkeypatch, tmp_path
):
    checkout, _, ctx = native_abandonment
    record = store.list_continuations(ctx.epic_ref)[0]
    driver, loop, _ = _driver(monkeypatch, tmp_path, store, ctx)
    driver.run(loop)
    # The fake runner leaves no run row, as after retention of a real loop run.
    assert store.get("loop-replacement") is None
    ctx.run_id = "second-host"
    with pytest.raises(RetainedReplacementRefusal, match="replacement run.*retention"):
        dispatch_core._host_checkout_abandonment(ctx, ctx.config)
    second, loop, launched = _driver(
        monkeypatch, tmp_path, store, ctx, run_id="second-loop"
    )
    with pytest.raises(RetainedReplacementRefusal, match="replacement run.*retention"):
        second.run(loop)
    assert not launched
    _evidence(store, record, "loop-replacement", checkout)
    assert len(store.list_audit_events(event="checkout_replaced")) == 1


@pytest.mark.serial
@pytest.mark.parametrize("failure_at", ["publication", "prompt", "runner"])
def test_round19_prelaunch_withdrawal_allows_fresh_retry(
    native_abandonment, store, monkeypatch, tmp_path, failure_at
):
    recorded, _, ctx = native_abandonment
    record = store.list_continuations(ctx.epic_ref)[0]
    shutil.rmtree(recorded.parent.parent)
    ctx.config.branches.worktree_base = str(tmp_path / "moved-base")
    checkout = worktree.dispatch_checkout_path(ctx.config, issue_num=42)
    driver, loop, launched = _driver(monkeypatch, tmp_path, store, ctx)

    def fail(*_a, **_k):
        raise RuntimeError("prelaunch failure")

    with monkeypatch.context() as scoped:
        if failure_at == "publication":
            scoped.setattr(driver, "_bind_publication", fail)
            assert driver.run(loop).status == "needs_human"
        elif failure_at == "prompt":
            scoped.setattr(delegate, "build_prompt", fail)
            with pytest.raises(RuntimeError, match="prelaunch failure"):
                driver.run(loop)
        else:
            scoped.setattr(
                delegate,
                "run_tracked_agent",
                lambda **kw: DispatchResult(
                    exit_code=1,
                    issue_num=42,
                    run_id=kw["run_id"],
                    log_file=tmp_path / "refused.log",
                ),
            )
            scoped.setattr(
                driver,
                "_publish_iteration",
                lambda *_a: pytest.fail("prelaunch refusal must not publish"),
            )
            assert driver.run(loop).status == "needs_human"
    assert not launched
    events = store.list_audit_events(event="checkout_replacement_withdrawn")
    assert len(events) == 1, "unused loop replacement must record withdrawal"
    payload = events[0].payload
    assert events[0].run_id == "loop-replacement"
    assert payload["checkout_removed"] is True
    assert payload["worker_launched"] is False
    assert payload["rebound_from"] == str(recorded)
    assert payload["rebound_reason"] == "recorded checkout missing"
    assert replacement_was_withdrawn(store, record)
    expiry = store.continuation_expiry(record)
    assert not checkout.exists()
    assert not current_continuation(store, record, "42")
    # Resolve the next attempt without reviving the already consumed decision.
    assert dispatch_core._host_checkout_abandonment(ctx, ctx.config) is None
    retry, loop, launched = _driver(monkeypatch, tmp_path, store, ctx, run_id="retry")
    assert retry.run(loop).status == "converged"
    assert launched == ["retry"]
    assert store.continuation_expiry(record) == expiry


@pytest.mark.serial
def test_round19_no_store_refuses_before_setup(native_abandonment, store, monkeypatch):
    checkout, _, ctx = native_abandonment
    before = store.read_chain_rows()
    monkeypatch.setattr(
        delegate, "setup_worktree", lambda **_k: pytest.fail("setup must not run")
    )
    with pytest.raises(
        RuntimeError,
        match=(
            "loop checkout setup requires a run store.*"
            "kew epic continue.*kew epic abandon"
        ),
    ):
        delegate._prepare_worktree(
            replace(_loop(), issue_num=42), ctx.config, store=None
        )
    assert store.read_chain_rows() == before
    assert (checkout / "edits.txt").read_text() == "abandoned work"


@pytest.mark.serial
def test_round19_failed_expiry_retains_authority_for_retry(
    native_abandonment, store, monkeypatch, tmp_path
):
    recorded, _, ctx = native_abandonment
    record = store.list_continuations(ctx.epic_ref)[0]
    shutil.rmtree(recorded.parent.parent)
    ctx.config.branches.worktree_base = str(tmp_path / "moved-base")
    driver, loop, launched = _driver(monkeypatch, tmp_path, store, ctx)

    def fail(*_a, **_k):
        raise OSError("expiry unavailable")

    with monkeypatch.context() as scoped:
        scoped.setattr(store, "record_continuation_expiry", fail)
        with pytest.raises(OSError, match="expiry unavailable"):
            driver.run(loop)
    assert not launched
    assert store.continuation_expiry(record) is None
    assert current_continuation(store, record, "42")
    authority = dispatch_core._host_checkout_abandonment(ctx, ctx.config)
    assert authority.evidence_identity == store.continuation_abandonment_identity(
        record
    )
    retry, loop, launched = _driver(monkeypatch, tmp_path, store, ctx, run_id="retry")
    assert retry.run(loop).status == "converged"
    assert launched == ["retry"]
    assert store.continuation_expiry(record).replacing_run_id == "retry"


@pytest.mark.serial
@pytest.mark.parametrize("cleanup", ["failed", "preserved"])
def test_round19_failed_cleanup_retains_replacement(
    native_abandonment, store, monkeypatch, tmp_path, cleanup
):
    checkout, _, ctx = native_abandonment
    record = store.list_continuations(ctx.epic_ref)[0]
    driver, loop, launched = _driver(monkeypatch, tmp_path, store, ctx)

    def fail(*_a, **_k):
        raise RuntimeError("prelaunch failure")

    def preserve(*_a):
        if cleanup == "failed":
            raise OSError("cleanup unavailable")

    monkeypatch.setattr(driver, "_bind_publication", fail)
    monkeypatch.setattr(delegate, "cleanup_worktree", preserve)
    assert driver.run(loop).status == "needs_human"
    _evidence(store, record, "loop-replacement", checkout)
    assert checkout.exists()
    assert not launched
    assert not store.list_audit_events(event="checkout_replacement_withdrawn")
    with pytest.raises(RetainedReplacementRefusal, match="replacement run.*retention"):
        dispatch_core._host_checkout_abandonment(ctx, ctx.config)


@pytest.mark.serial
def test_round19_worker_failure_preserves_successor_checkout(
    native_abandonment, store, monkeypatch, tmp_path
):
    checkout, _, ctx = native_abandonment
    record = store.list_continuations(ctx.epic_ref)[0]
    driver, loop, _ = _driver(monkeypatch, tmp_path, store, ctx)

    def worker(**kwargs):
        check = kwargs.get("paid_launch_check")
        if check is not None:
            check()
        (checkout / "worker-edits.txt").write_text("retain worker edits")
        raise RuntimeError("worker failed")

    monkeypatch.setattr(delegate, "run_tracked_agent", worker)
    with pytest.raises(RuntimeError, match="worker failed"):
        driver.run(loop)
    _evidence(store, record, "loop-replacement", checkout)
    assert (checkout / "worker-edits.txt").read_text() == "retain worker edits"
    assert not store.list_audit_events(event="checkout_replacement_withdrawn")


def _round20_paid_attempts(
    monkeypatch, tmp_path, checkout, *, change_at=None, change=None
):
    """Exercise dispatch's actual paid boundary for each simulated retry."""
    from contextlib import nullcontext
    from types import SimpleNamespace

    from kew.tracking import LaunchState

    started = []
    monkeypatch.setattr(dispatch_core, "_check_existing_pr", lambda *a: (False, None))
    monkeypatch.setattr(
        dispatch_core, "_sidecar_attempt_context", lambda *a: nullcontext(None)
    )
    monkeypatch.setattr(
        dispatch_core, "_admit_attempt_route_profile", lambda *a, **kw: None
    )
    monkeypatch.setattr(
        dispatch_core, "_contained_execution_for", lambda *a, **kw: None
    )
    monkeypatch.setattr(dispatch_core, "_classify_attempt", lambda *a: False)

    def worker(**kwargs):
        started.append(len(started) + 1)
        return DispatchResult(
            exit_code=0,
            issue_num=42,
            log_file=tmp_path / "run.log",
            run_id="loop-replacement",
        )

    monkeypatch.setattr(dispatch_core, "run_agent", worker)

    def tracked(**kwargs):
        launch_state = LaunchState()
        # main's retry helper attests a posture per attempt (#1955) and reads the
        # tracked run's identity fields; supply the full _TrackedRun shape.
        run = SimpleNamespace(
            paid_launch_check=kwargs["paid_launch_check"],
            runner=kwargs["runner"],
            config=kwargs["config"],
            store=None,
            run_id="loop-replacement",
            issue_num=42,
            item_id="42",
            branch="agent/issue-42",
            title="t",
            complexity="medium",
            run_kind="dispatch",
            loop_id="L1",
            prefix_key=None,
            provider=None,
            effective_model=kwargs["model"],
            actor=None,
            endpoint=None,
            repo=None,
            log_file=tmp_path / "run.log",
            dispatch_start=0.0,
            transport=SimpleNamespace(
                resolved=SimpleNamespace(sidecar=False, mode="direct")
            ),
            transport_health_check=None,
            contained_image=None,
            resolved_image_id=None,
            sandbox_posture=None,
            map_tokens=None,
            lesson_corpus_tokens=None,
            source_tracker="github",
            # Fields main added to _TrackedRun (#1943/#1957): launch evidence,
            # admission receipt, bounded key and launch callback. The launch
            # state is real so the paid boundary's evidence can be checked.
            launched=False,
            launch_state=launch_state,
            admission=None,
            credential=None,
            on_launch=None,
            reservation=None,
            reservation_pinned=False,
        )
        for attempt in range(2 if change_at == "retry" else 1):
            if change_at == "first" or (change_at == "retry" and attempt == 1):
                change()
            # main (#1955/#1956) runs the retry guard before this helper: it now
            # returns (result, terminal) and takes no current_result.
            result, _ = dispatch_core._run_retry_attempt(
                run,
                prompt="prompt",
                worktree_dir=checkout,
                attempt=attempt,
                prev_result=None,
                store=None,
                budget_cap_usd=10,
                pricing_overrides=None,
                resume_session_id=None,
                budget_override_usd=None,
                timeout_override_minutes=None,
                on_spawn=None,
                should_pause=None,
                budget_check=None,
                anomaly_threshold_usd=None,
                anomaly_compaction_allowance_usd=0,
                managed_switchyard=None,
            )
        return result

    monkeypatch.setattr(delegate, "run_tracked_agent", tracked)
    return started


def _round20_generation_change(checkout, ctx, change):
    from test_worktree import _round20_recreate_checkout

    if change == "token":
        worktree.write_checkout_generation(checkout)
    else:
        _round20_recreate_checkout(checkout, worktree._repo_root(), ctx.branch)


@pytest.mark.serial
@pytest.mark.parametrize("change", ["token", "directory"])
@pytest.mark.parametrize("attempt", ["setup", "first", "retry"])
def test_round20_loop_paid_boundary_refuses_changed_generation(
    native_abandonment, store, monkeypatch, tmp_path, change, attempt
):
    checkout, _, ctx = native_abandonment
    driver, loop, _ = _driver(monkeypatch, tmp_path, store, ctx)
    published = []
    monkeypatch.setattr(driver, "_publish_iteration", lambda *a: published.append(a))
    started = _round20_paid_attempts(
        monkeypatch,
        tmp_path,
        checkout,
        change_at=attempt,
        change=lambda: _round20_generation_change(checkout, ctx, change),
    )
    if attempt == "setup":
        prepare = driver._prepare_admitted_worktree

        def changed_after_setup(*args, **kwargs):
            created = prepare(*args, **kwargs)
            _round20_generation_change(checkout, ctx, change)
            return created

        monkeypatch.setattr(driver, "_prepare_admitted_worktree", changed_after_setup)
    with pytest.raises(worktree.CheckoutAdmissionRefusal):
        driver.run(loop)
    assert started == ([1] if attempt == "retry" else [])
    assert not published
    assert store.get_loop(loop.loop_id).status == "needs_human"
    withdrawals = store.list_audit_events(event="checkout_replacement_withdrawn")
    if attempt != "retry":
        assert len(withdrawals) == 1
        assert withdrawals[0].payload["checkout_removed"] is True
        assert withdrawals[0].payload["worker_launched"] is False
        assert not checkout.exists()
    else:
        assert not withdrawals
        assert checkout.exists()
    assert len(store.list_audit_events(event="checkout_replaced")) == 1


def _round20_host_publication(monkeypatch, driver, ctx):
    from unittest.mock import MagicMock

    published = []
    monkeypatch.setattr(
        driver,
        "_publish_iteration",
        delegate.DelegateDriver._publish_iteration.__get__(driver),
    )
    monkeypatch.setattr(driver, "_loop_has_pr", lambda *a: True)

    def publish(*args, **kwargs):
        published.append(kwargs)
        return delegate.CanonicalNoChange(
            artifact=MagicMock(), base_sha=ctx.publication_base_sha, tree_sha="b" * 40
        )

    monkeypatch.setattr(dispatch_core, "_git_push_branch", publish)
    return published


@pytest.mark.serial
@pytest.mark.parametrize("change", ["token", "directory"])
def test_round20_loop_publication_refuses_changed_generation(
    native_abandonment, store, monkeypatch, tmp_path, change
):
    checkout, _, ctx = native_abandonment
    driver, loop, _ = _driver(monkeypatch, tmp_path, store, ctx)
    published = _round20_host_publication(monkeypatch, driver, ctx)
    started = _round20_paid_attempts(monkeypatch, tmp_path, checkout)
    launch = driver._launch_bound_session

    def returned(*args, **kwargs):
        result = launch(*args, **kwargs)
        _round20_generation_change(checkout, ctx, change)
        return result

    monkeypatch.setattr(driver, "_launch_bound_session", returned)
    with pytest.raises(dispatch_core._CheckoutIdentityRefused):
        driver.run(loop)
    assert started == [1]
    assert not published
    assert not store.list_audit_events(event="checkout_no_change_evidence")
    assert not store.list_audit_events(event="checkout_replacement_withdrawn")
    assert checkout.exists()
    assert store.get_loop(loop.loop_id).status == "needs_human"


@pytest.mark.serial
def test_round20_unchanged_loop_launches_and_publishes(
    native_abandonment, store, monkeypatch, tmp_path
):
    checkout, _, ctx = native_abandonment
    driver, loop, _ = _driver(monkeypatch, tmp_path, store, ctx)
    published = _round20_host_publication(monkeypatch, driver, ctx)
    started = _round20_paid_attempts(monkeypatch, tmp_path, checkout)
    bind = driver._bind_publication
    generations = []

    def remember(loop, checkout, binding):
        generations.append(
            (
                getattr(binding, "checkout_generation", ""),
                worktree.read_checkout_generation(checkout),
            )
        )
        return bind(loop, checkout, binding)

    monkeypatch.setattr(driver, "_bind_publication", remember)
    assert driver.run(loop).status == "converged"
    assert started == [1]
    assert len(published) == 1
    assert len(store.list_audit_events(event="checkout_no_change_evidence")) == 1
    assert not store.list_audit_events(event="checkout_replacement_withdrawn")
    assert len(generations) == 1
    assert generations[0][0] == generations[0][1], (
        "loop must retain the checkout generation captured during setup"
    )


@pytest.mark.serial
@pytest.mark.parametrize("entrypoint", ["dispatch", "resume"])
def test_round20_loop_identity_refusal_exits_two(
    native_abandonment, store, monkeypatch, tmp_path, entrypoint
):
    from contextlib import nullcontext

    from test_cli_dispatch_loop import _common_patches
    from typer.testing import CliRunner

    from kew import cli

    checkout, _, ctx = native_abandonment
    driver, loop, _ = _driver(monkeypatch, tmp_path, store, ctx)
    run = driver.run
    started = _round20_paid_attempts(
        monkeypatch,
        tmp_path,
        checkout,
        change_at="first",
        change=lambda: worktree.write_checkout_generation(checkout),
    )
    _common_patches(monkeypatch)
    ctx.config.logging.dir = str(tmp_path / "logs")
    monkeypatch.setenv("KEW_PROJECT", str(worktree._repo_root()))
    monkeypatch.setattr(cli, "load_config", lambda **kw: ctx.config)
    monkeypatch.setattr(cli, "_open_run_store", lambda *a: nullcontext(store))
    monkeypatch.setattr(cli, "preflight_tools", lambda: None)
    monkeypatch.setattr(cli, "_log_budget_banner", lambda *a, **kw: None)
    monkeypatch.setattr(delegate.DelegateDriver, "run", lambda *a, **kw: run(loop))
    monkeypatch.setattr(delegate.DelegateDriver, "resume", lambda *a, **kw: run(loop))
    # The outer CLI driver supplies no gate; only the prepared driver executes.
    monkeypatch.setattr(delegate, "_build_default_gate", lambda *a: driver._gate)
    args = (
        ["dispatch", "42", "--loop"]
        if entrypoint == "dispatch"
        else ["loop", "resume", loop.loop_id, "--guidance", "continue"]
    )
    result = CliRunner().invoke(cli.app, args)
    assert result.exit_code == 2, result.output
    assert "checkout identity changed" in result.output
    assert not started
    assert len(store.list_audit_events(event="checkout_replacement_withdrawn")) == 1


@pytest.mark.serial
@pytest.mark.parametrize("changed_attempt", [1, 2])
def test_round20_tracked_loop_refusal_records_exit_two(
    native_abandonment, store, monkeypatch, tmp_path, changed_attempt, process_table
):
    owner = os.getpid()
    process_table.members[owner] = (owner, "S", "kew", "kew dispatch 42")
    checkout, _, ctx = native_abandonment
    driver, loop, _ = _driver(monkeypatch, tmp_path, store, ctx)
    ctx.config.logging.dir = str(tmp_path / "logs")
    ctx.config.run_reports.enabled = False
    ctx.config.sandbox.enabled = False
    ctx.config.sandbox.dangerously_skip_containment = True
    ctx.config.retry.max_attempts = 2
    ctx.config.retry.backoff_base_seconds = 0
    monkeypatch.setattr(delegate, "run_tracked_agent", dispatch_core.run_tracked_agent)
    monkeypatch.setattr(
        dispatch_core, "_contained_execution_for", lambda *a, **kw: None
    )
    monkeypatch.setattr(dispatch_core, "_check_existing_pr", lambda *a: (False, None))
    admit = dispatch_core._admit_attempt_route_profile
    started = []

    def admit_then_change(run, attempt, **kwargs):
        admit(run, attempt, **kwargs)
        if attempt == changed_attempt:
            worktree.write_checkout_generation(checkout)

    def worker(**kwargs):
        started.append(True)
        return DispatchResult(
            exit_code=1,
            issue_num=42,
            log_file=tmp_path / "run.log",
            error_category="transient",
            total_tokens=123,
            estimated_cost_usd=0.01,
        )

    monkeypatch.setattr(
        dispatch_core, "_admit_attempt_route_profile", admit_then_change
    )
    monkeypatch.setattr(dispatch_core, "run_agent", worker)
    with pytest.raises(dispatch_core._CheckoutIdentityRefused):
        driver.run(loop)
    assert len(started) == changed_attempt - 1
    saved = store.get("loop-replacement")
    assert saved.status == "refused"
    assert saved.exit_code == 2
    assert saved.error_category == "checkout_identity_changed"
    [completed] = store.list_audit_events(
        event="dispatch_completed", run_id=saved.run_id
    )
    assert completed.payload["exit_code"] == 2
    if changed_attempt == 2:
        assert saved.total_tokens == 123
    withdrawals = store.list_audit_events(event="checkout_replacement_withdrawn")
    assert len(withdrawals) == (1 if changed_attempt == 1 else 0)


@pytest.mark.serial
def test_round21_loop_retires_legacy_sidecar(
    native_abandonment, store, monkeypatch, tmp_path
):
    checkout, _, ctx = native_abandonment
    record = store.list_continuations(ctx.epic_ref)[0]
    worktree.revoke_checkout_generation(checkout)
    worktree._generation_sidecar(checkout).write_text("a" * 32 + "\n")
    driver, loop, launched = _driver(monkeypatch, tmp_path, store, ctx)
    assert driver.run(loop).status == "converged"
    assert launched == ["loop-replacement"]
    payload = _evidence(store, record, "loop-replacement", checkout)
    assert payload["legacy_sidecar_retired"] is True
    assert (checkout / ".git" / "kew-generation").read_text().strip() == json.loads(
        payload["checkout_id"]
    )["nonce"]


@pytest.fixture
def round22_native_abandonment(monkeypatch, tmp_path, request):
    from test_worktree import _round21_patch_identity

    checkout = tmp_path / "worktrees" / "agent" / "issue-42"
    _round21_patch_identity(
        monkeypatch, checkout, no_birth=True, st_ctime=10.0, st_mtime=20.0
    )
    return request.getfixturevalue("native_abandonment")


def _round22_root_edits(monkeypatch, checkout):
    from test_worktree import _round21_patch_identity, _round22_edit_root

    _round22_edit_root(checkout)
    _round21_patch_identity(
        monkeypatch, checkout, no_birth=True, st_ctime=30.0, st_mtime=40.0
    )


def _round22_return_after_root_edits(*args, launch, monkeypatch, checkout, **kwargs):
    result = launch(*args, **kwargs)
    _round22_root_edits(monkeypatch, checkout)
    return result


@pytest.mark.serial
@pytest.mark.parametrize("boundary", ["first", "retry", "publication"])
def test_round22_loop_root_edits_launch_and_publish(
    round22_native_abandonment, store, monkeypatch, tmp_path, boundary
):
    from functools import partial

    checkout, _, ctx = round22_native_abandonment
    driver, loop, _ = _driver(monkeypatch, tmp_path, store, ctx)
    published = _round20_host_publication(monkeypatch, driver, ctx)
    started = _round20_paid_attempts(
        monkeypatch,
        tmp_path,
        checkout,
        change_at=boundary,
        change=partial(_round22_root_edits, monkeypatch, checkout),
    )
    if boundary == "publication":
        monkeypatch.setattr(
            driver,
            "_launch_bound_session",
            partial(
                _round22_return_after_root_edits,
                launch=driver._launch_bound_session,
                monkeypatch=monkeypatch,
                checkout=checkout,
            ),
        )
    assert driver.run(loop).status == "converged"
    assert started == ([1, 2] if boundary == "retry" else [1])
    assert len(published) == 1
    assert len(store.list_audit_events(event="checkout_no_change_evidence")) == 1
    assert not store.list_audit_events(event="checkout_replacement_withdrawn")
    assert worktree.read_checkout_generation(checkout)
    assert (checkout / "created").read_text() == "new content"
    assert not (checkout / "delete-me").exists()
    assert (checkout / "renamed").read_text() == "retained content"
