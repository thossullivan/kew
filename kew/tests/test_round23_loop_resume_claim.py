"""Checkout leases exclude competing loop restoration and fresh dispatch."""

import shutil
import threading
from contextlib import nullcontext
from dataclasses import replace
from functools import partial
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_loops_delegate import _FakeGate, _loop
from typer.testing import CliRunner

from kew import cli, worktree
from kew.config import DispatchConfig
from kew.continuation_recovery import CheckoutGenerationRefusal
from kew.loops import delegate
from kew.loops.verification import GateVerdict
from kew.run_store import SqliteRunStore
from kew.runners.registry import ResolvedRoute
from kew.tracking import DispatchResult


def _materialize_checkout(checkout):
    (checkout / ".git").mkdir(parents=True)
    (checkout / "agent.txt").write_bytes(b"live dispatch\x00workspace\n")
    return worktree.write_checkout_generation(checkout)


def _snapshot(checkout):
    return (
        checkout.stat().st_ino,
        {
            str(p.relative_to(checkout)): p.read_bytes()
            for p in checkout.rglob("*")
            if p.is_file()
        },
        worktree._generation_sidecar(checkout).read_bytes(),
    )


class _Contender:
    """Keep the competing thread's successful claim until explicitly stopped."""

    def __init__(self, checkout, *, create=False):
        self.checkout = checkout
        self.create = create
        self.ready = threading.Event()
        self.done = threading.Event()
        self.error = None
        self.snapshot = None
        self.thread = threading.Thread(target=self.run)

    def run(self):
        acquired = False
        try:
            worktree.claim_checkout(self.checkout)
            acquired = True
            if self.create:
                _materialize_checkout(self.checkout)
                self.snapshot = _snapshot(self.checkout)
        except BaseException as exc:
            self.error = exc
        finally:
            self.ready.set()
        if acquired:
            self.done.wait()
            worktree.release_checkout_claim(self.checkout)

    def start(self):
        self.thread.start()
        assert self.ready.wait(10), "competing claim did not complete"
        return self

    def stop(self):
        self.done.set()
        if self.thread.ident is not None:
            self.thread.join(10)
            assert not self.thread.is_alive()


def _probe_claim(ctx):
    contender = _Contender(ctx.checkout)
    try:
        contender.start()
        return contender.error
    finally:
        contender.stop()


def _clone_local(_source, checkout, _required_ref):
    (checkout / ".git").mkdir(parents=True)


def _fake_tracked(ctx, **kwargs):
    ctx.launched.append(kwargs["run_id"])
    kwargs["paid_launch_check"]()
    return DispatchResult(
        exit_code=0, issue_num=7, run_id=kwargs["run_id"], log_file=ctx.root / "run.log"
    )


def _fresh_checkout(ctx, **_kwargs):
    _materialize_checkout(ctx.checkout)
    return ctx.checkout


def _previous_fixture_binding(ctx, *_args, **_kwargs):
    generation = (
        worktree.read_checkout_generation(ctx.checkout)
        if ctx.checkout.exists()
        else "previous-generation"
    )
    return delegate._PreviousPublicationBinding("run23", "a" * 40, generation, 1)


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    config = DispatchConfig()
    config.branches.worktree_base = str(tmp_path / "checkouts")
    config.logging.dir = str(tmp_path / "logs")
    config.agent.model = "claude-sonnet-4-6"
    config.sandbox.dangerously_skip_containment = True
    loop = replace(
        _loop(),
        status="paused",
        status_reason="operator requested review",
        model=config.agent.model,
        budget_usd=config.agent.max_budget_usd,
    )
    route = ResolvedRoute(
        runner=SimpleNamespace(name="claude-code", loop_style="native"),
        model=loop.model,
    )
    gate = _FakeGate(GateVerdict(passed=True, evidence="ok", sabotage=False))
    with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
        store.save_loop(loop)
        driver = delegate.DelegateDriver(store, config=config, gate=gate)
        context = SimpleNamespace(
            root=tmp_path,
            config=config,
            loop=loop,
            driver=driver,
            store=store,
            checkout=worktree.dispatch_checkout_path(config, issue_num=7),
            route=route,
            launched=[],
        )
        monkeypatch.setattr(delegate, "_resolve_resume_route", Mock(return_value=route))
        monkeypatch.setattr(delegate, "resolve_runner", Mock(return_value=route))
        monkeypatch.setattr(
            delegate, "_resolved_runner_preflight_failure", Mock(return_value=None)
        )
        monkeypatch.setattr(delegate, "build_repo_map", Mock(return_value=""))
        monkeypatch.setattr(delegate, "build_prompt", Mock(return_value="prompt"))
        monkeypatch.setattr(
            delegate, "run_tracked_agent", partial(_fake_tracked, context)
        )
        monkeypatch.setattr(
            delegate, "setup_worktree", partial(_fresh_checkout, context)
        )
        monkeypatch.setattr(
            driver,
            "_new_publication_binding",
            Mock(return_value=delegate._PublicationBinding("run23", "a" * 40, "run23")),
        )
        monkeypatch.setattr(
            driver,
            "_previous_publication_binding",
            partial(_previous_fixture_binding, context),
        )
        monkeypatch.setattr(driver, "_require_current_publication_binding", Mock())
        monkeypatch.setattr(driver, "_bind_publication", Mock())
        monkeypatch.setattr(driver, "_publish_iteration", Mock(return_value=None))
        monkeypatch.setattr(
            driver, "_decide", Mock(return_value=("paused", "session finished"))
        )
        monkeypatch.setattr(
            worktree, "is_safe_standalone_clone", Mock(return_value=True)
        )
        monkeypatch.setattr(worktree, "remote_branch_exists", Mock(return_value=True))
        monkeypatch.setattr(worktree, "_repo_root", Mock(return_value=tmp_path))
        monkeypatch.setattr(worktree, "clone_local_checkout", _clone_local)
        monkeypatch.setattr(worktree, "fetch_ref", Mock())
        monkeypatch.setattr(
            worktree,
            "run",
            Mock(return_value=SimpleNamespace(returncode=0, stdout="agent/issue-7\n")),
        )
        try:
            yield context
        finally:
            worktree.release_checkout_claims()


def _remote_query_with_contender(contender, _branch):
    contender.start()
    return True


@pytest.mark.parametrize("publish", ["canonical", "checkout"])
@pytest.mark.parametrize("clone_fails", [False, True])
def test_round23_resume_claims_before_remote_query(
    ctx, monkeypatch, publish, clone_fails
):
    ctx.config.publication.publish = publish
    contender = _Contender(ctx.checkout, create=True)
    monkeypatch.setattr(
        worktree,
        "remote_branch_exists",
        partial(_remote_query_with_contender, contender),
    )
    if clone_fails:
        monkeypatch.setattr(
            worktree, "clone_local_checkout", Mock(side_effect=OSError("clone failed"))
        )
    assert not ctx.checkout.exists()
    try:
        if clone_fails:
            with pytest.raises(RuntimeError, match="clone failed"):
                ctx.driver.resume(ctx.loop, "continue")
        else:
            assert ctx.driver.resume(ctx.loop, "continue").status == "paused"
        # If the contender entered, preserve its bytes before reporting the failure.
        if contender.snapshot is not None:
            assert ctx.checkout.exists(), "resume removed the concurrent checkout"
            assert _snapshot(ctx.checkout) == contender.snapshot, (
                "resume replaced the concurrent generation"
            )
        assert isinstance(contender.error, worktree.CheckoutBusy), (
            "resume must claim before its remote query"
        )
        assert not list(ctx.checkout.parent.glob("issue-7.replaced-*"))
    finally:
        contender.stop()
    assert _probe_claim(ctx) is None


@pytest.mark.parametrize("publish", ["canonical", "checkout"])
def test_round23_fresh_claim_wins_before_resume_inspection(ctx, monkeypatch, publish):
    ctx.config.publication.publish = publish
    contender = _Contender(ctx.checkout, create=True)
    previous = Mock(return_value=None)
    monkeypatch.setattr(ctx.driver, "_previous_publication_binding", previous)
    before = ctx.store.get_loop(ctx.loop.loop_id)
    writes = ctx.store._conn.total_changes
    try:
        contender.start()
        assert contender.error is None
        with pytest.raises(worktree.CheckoutBusy, match="claimed by another"):
            ctx.driver.resume(ctx.loop, "continue", budget_usd=99)
        previous.assert_not_called()
        worktree.remote_branch_exists.assert_not_called()
        worktree.run.assert_not_called()
        assert _snapshot(ctx.checkout) == contender.snapshot
        assert not list(ctx.checkout.parent.glob("issue-7.replaced-*"))
        assert ctx.store.get_loop(ctx.loop.loop_id) == before
        assert ctx.store._conn.total_changes == writes
        assert not ctx.launched
    finally:
        contender.stop()


def _tracked_with_claim_probe(ctx, discard, **kwargs):
    assert isinstance(_probe_claim(ctx), worktree.CheckoutBusy), (
        "session must retain its checkout claim"
    )
    result = _fake_tracked(ctx, **kwargs)
    if discard:
        shutil.rmtree(ctx.checkout)
    return result


def _publish_with_claim_probe(ctx, *_args):
    assert isinstance(_probe_claim(ctx), worktree.CheckoutBusy), (
        "publication must retain its checkout claim"
    )


@pytest.mark.parametrize("entry", ["resume", "run"])
@pytest.mark.parametrize("discard", [False, True])
def test_round23_session_holds_then_releases_claim(ctx, monkeypatch, entry, discard):
    if entry == "resume":
        _materialize_checkout(ctx.checkout)
    monkeypatch.setattr(
        delegate, "run_tracked_agent", partial(_tracked_with_claim_probe, ctx, discard)
    )
    monkeypatch.setattr(
        ctx.driver, "_publish_iteration", partial(_publish_with_claim_probe, ctx)
    )
    if entry == "resume":
        ctx.driver.resume(ctx.loop, "continue")
    else:
        ctx.driver.run(ctx.loop, route=ctx.route)
    assert len(ctx.launched) == 1
    assert ctx.checkout.exists() is not discard
    assert _probe_claim(ctx) is None, "the finished session must release its claim"


def _fail_while_claimed(ctx, error, *_args, **_kwargs):
    assert isinstance(_probe_claim(ctx), worktree.CheckoutBusy), (
        "prelaunch inspection requires the claim"
    )
    raise error("prelaunch refusal")


@pytest.mark.parametrize(
    "error",
    [ValueError, RuntimeError, OSError, CheckoutGenerationRefusal, KeyboardInterrupt],
)
@pytest.mark.parametrize("boundary", ["binding", "restore", "iterations"])
def test_round23_prelaunch_failure_releases_claim(ctx, monkeypatch, error, boundary):
    target, name = {
        "binding": (ctx.driver, "_previous_publication_binding"),
        "restore": (delegate, "reuse_or_restore_worktree"),
        "iterations": (ctx.store, "list_iterations"),
    }[boundary]
    _materialize_checkout(ctx.checkout)
    before = ctx.store.get_loop(ctx.loop.loop_id)
    monkeypatch.setattr(target, name, partial(_fail_while_claimed, ctx, error))
    with pytest.raises(error, match="prelaunch refusal"):
        ctx.driver.resume(ctx.loop, "continue")
    assert _probe_claim(ctx) is None
    assert not ctx.launched
    assert ctx.store.get_loop(ctx.loop.loop_id) == before


@pytest.mark.parametrize("claimed_elsewhere", [False, True])
@pytest.mark.parametrize("entry", ["reuse", "restore"])
def test_round23_unclaimed_restore_refuses_before_touching_checkout(
    ctx, claimed_elsewhere, entry
):
    _materialize_checkout(ctx.checkout)
    before = _snapshot(ctx.checkout)
    contender = _Contender(ctx.checkout)
    try:
        if claimed_elsewhere:
            contender.start()
            assert contender.error is None
        with pytest.raises(worktree.CheckoutBusy, match="claim"):
            if entry == "reuse":
                worktree.reuse_or_restore_worktree(issue_num=7, config=ctx.config)
            else:
                worktree._restore_worktree(ctx.checkout, "agent/issue-7")
        worktree.is_safe_standalone_clone.assert_not_called()
        worktree.remote_branch_exists.assert_not_called()
        worktree.run.assert_not_called()
        assert _snapshot(ctx.checkout) == before
    finally:
        contender.stop()


def _configure_cli(ctx, monkeypatch):
    (ctx.root / ".git").mkdir()
    monkeypatch.setenv("KEW_PROJECT", str(ctx.root))
    monkeypatch.setattr(cli, "load_config", Mock(return_value=ctx.config))
    monkeypatch.setattr(
        cli, "_open_run_store", Mock(return_value=nullcontext(ctx.store))
    )
    monkeypatch.setattr(cli, "preflight_tools", Mock())
    monkeypatch.setattr(cli, "_preflight", Mock())
    monkeypatch.setattr(cli, "_detect_repo", Mock())
    monkeypatch.setattr(cli, "_refuse_if_fleet_stopped", Mock())
    monkeypatch.setattr(cli, "_log_budget_banner", Mock())
    monkeypatch.setattr(cli, "resolve_runner", Mock(return_value=ctx.route))
    monkeypatch.setattr(
        cli,
        "select_next_issue",
        Mock(return_value={"number": 7, "title": "fix", "labels": []}),
    )
    monkeypatch.setattr(delegate, "build_loop_from_issue", Mock(return_value=ctx.loop))
    monkeypatch.setattr(delegate, "DelegateDriver", Mock(return_value=ctx.driver))


@pytest.mark.parametrize("entry", ["resume", "dispatch"])
def test_round23_cli_busy_exits_two_preserves_paused_record(ctx, monkeypatch, entry):
    _configure_cli(ctx, monkeypatch)
    before = ctx.store.get_loop(ctx.loop.loop_id)
    contender = _Contender(ctx.checkout, create=True)
    command = (
        ["loop", "resume", ctx.loop.loop_id, "--guidance", "continue"]
        if entry == "resume"
        else [entry, "7", "--loop"]
    )
    try:
        contender.start()
        assert contender.error is None
        result = CliRunner().invoke(cli.app, command)
        assert result.exit_code == 2, (result.output, result.exception)
        assert "claimed by another run" in result.stderr
        assert "Traceback" not in result.output
        assert isinstance(result.exception, SystemExit)
        assert ctx.store.get_loop(ctx.loop.loop_id) == before
        assert _snapshot(ctx.checkout) == contender.snapshot
        assert not ctx.launched
    finally:
        contender.stop()


def test_round23_busy_run_does_not_record_a_new_route(ctx):
    ctx.loop.model = "previous-model"
    ctx.store.save_loop(ctx.loop)
    before = ctx.store.get_loop(ctx.loop.loop_id)
    writes = ctx.store._conn.total_changes
    contender = _Contender(ctx.checkout, create=True)
    try:
        contender.start()
        with pytest.raises(worktree.CheckoutBusy):
            ctx.driver.run(ctx.loop, route=ctx.route)
        assert ctx.store.get_loop(ctx.loop.loop_id) == before
        assert ctx.store._conn.total_changes == writes
        assert _snapshot(ctx.checkout) == contender.snapshot
    finally:
        contender.stop()


def test_round23_release_cannot_drop_another_threads_claim(ctx):
    contender = _Contender(ctx.checkout, create=True)
    try:
        contender.start()
        assert contender.error is None
        # Removal can release an old lease before an outer session finalizer runs.
        worktree.release_checkout_claim(ctx.checkout)
        with pytest.raises(worktree.CheckoutBusy, match="claimed by another"):
            worktree.claim_checkout(ctx.checkout)
        assert _snapshot(ctx.checkout) == contender.snapshot
    finally:
        contender.stop()
