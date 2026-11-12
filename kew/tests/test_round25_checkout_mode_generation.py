"""Checkout publication preserves immutable continuation and launch bindings."""

import inspect
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import typer
from test_dispatch_core import TestInitialCanonicalCheckoutBinding as CheckoutBinding
from test_worktree import _git_out, _origin_repo_with_branch

from kew import dispatch_core, worktree
from kew.cli import epic as epic_cli
from kew.config import DispatchConfig
from kew.graph.driver import tick
from kew.run_store import RunRecord, SqliteRunStore
from kew.subprocess_utils import TOOL_PATHS


@pytest.fixture
def scene(tmp_path, monkeypatch):
    monkeypatch.setitem(TOOL_PATHS, "git", shutil.which("git"))
    branch = "agent/issue-42"
    repo = _origin_repo_with_branch(tmp_path, branch)
    config = DispatchConfig()
    config.publication.publish = "checkout"
    config.sandbox.dangerously_skip_containment = True
    config.branches.worktree_base = str(tmp_path / "checkouts")
    checkout = worktree.dispatch_checkout_path(config, issue_num=42)
    checkout.parent.mkdir(parents=True)
    _git_out("clone", "--no-hardlinks", str(repo), str(checkout), cwd=repo)
    _git_out("-C", str(checkout), "switch", branch, cwd=repo)
    (checkout / "wip.txt").write_text("generation A bytes")
    generation = worktree.write_checkout_generation(checkout)
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        store.save(
            RunRecord(
                run_id="original",
                issue_num=42,
                item_id="42",
                branch=branch,
                status="failure",
                exit_code=1,
                started_at=datetime.now(UTC),
                finished_at=datetime.now(UTC),
            )
        )
        assert store.reserve_epic_node_item("epic:x", "42", "reservation")
        assert (
            tick._persist_preexisting_continuation(
                store,
                config,
                epic_ref="epic:x",
                item_id="42",
                expected_branch=branch,
                candidate=checkout,
                original_run_id="original",
                reservation_run_id="reservation",
                checkout_generation=generation,
            )
            == "registered"
        )
        [record] = store.list_continuations("epic:x")
        monkeypatch.setattr(epic_cli, "_resume_owner_is_dead", Mock(return_value=True))
        try:
            yield SimpleNamespace(
                store=store,
                config=config,
                checkout=checkout,
                generation=generation,
                record=record,
                repo=repo,
                branch=branch,
            )
        finally:
            worktree.release_checkout_claims()


def _replace_checkout(scene):
    shutil.rmtree(scene.checkout)
    _git_out(
        "clone", "--no-hardlinks", str(scene.repo), str(scene.checkout), cwd=scene.repo
    )
    _git_out("-C", str(scene.checkout), "switch", scene.branch, cwd=scene.repo)
    (scene.checkout / "wip.txt").write_text("generation B bytes")
    replacement = worktree.write_checkout_generation(scene.checkout)
    assert replacement != scene.generation
    return replacement


@pytest.mark.serial
def test_round25_checkout_resume_refuses_replacement_before_git(
    scene, monkeypatch, capsys
):
    _replace_checkout(scene)
    invocations = Mock(wraps=subprocess.Popen)
    monkeypatch.setattr(subprocess, "Popen", invocations)
    refused = False
    try:
        epic_cli._prepare_resume_record(
            scene.store, epic_ref="epic:x", item_id="42", config=scene.config
        )
    except typer.Exit as exc:
        refused = exc.exit_code == 2
    output = capsys.readouterr().err
    git_calls = [
        call.args[0]
        for call in invocations.call_args_list
        if Path(call.args[0][0]).name == "git"
    ]
    assert refused and git_calls == [], (
        f"refused={refused}; Git invocations={git_calls}"
    )
    assert "checkout identity changed" in output
    assert "kew epic abandon" in output
    assert scene.store.list_continuations("epic:x") == [scene.record]


class _PaidBoundary:
    def __init__(self, scene, monkeypatch):
        self.scene = scene
        self.monkeypatch = monkeypatch
        self.checks = Mock(wraps=dispatch_core._require_checkout_generation)
        self.refusal = None

    def __call__(self, **kwargs):
        _replace_checkout(self.scene)
        self.monkeypatch.setattr(
            dispatch_core, "_require_checkout_generation", self.checks
        )
        try:
            kwargs["paid_launch_check"]()
        except dispatch_core.PublicationError as exc:
            self.refusal = exc
        return Mock()


@pytest.mark.serial
def test_round25_checkout_paid_launch_checks_and_refuses_replacement(
    scene, monkeypatch
):
    ctx = CheckoutBinding._context(
        dispatch_core, scene.config, scene.store, run_id="new"
    )
    ctx.model_override = "model"
    ctx.epic_ref = None
    ctx.branch = scene.branch
    boundary = _PaidBoundary(scene, monkeypatch)
    monkeypatch.setattr(dispatch_core, "_build_repo_map", Mock(return_value=""))
    monkeypatch.setattr(dispatch_core, "build_prompt", Mock(return_value="prompt"))
    monkeypatch.setattr(dispatch_core, "_require_final_runner_preflight", Mock())
    monkeypatch.setattr(
        dispatch_core, "_compute_prefix_routing", Mock(return_value=("", ""))
    )
    monkeypatch.setattr(dispatch_core, "run_tracked_agent", boundary)
    runner = SimpleNamespace(name="claude-code", publication_style="agent")
    dispatch_core._launch_host(
        ctx,
        scene.checkout,
        SimpleNamespace(runner=runner, model="model"),
        runner,
        dispatch_core._EarlyRunInfo(),
        None,
    )
    assert boundary.checks.call_count == 1
    assert boundary.refusal is not None
    assert "native paid launch" in str(boundary.refusal)
    [binding] = scene.store.list_audit_events(event="dispatch_checkout_bound")
    assert binding.payload["checkout_id"] == scene.generation


@pytest.mark.serial
def test_round25_checkout_producer_returns_recorded_generation(scene):
    _replace_checkout(scene)
    with SqliteRunStore(scene.store._db_path) as recovered:
        original, base, generation = tick._terminal_run_for_preexisting_work(
            recovered,
            "42",
            scene.branch,
            scene.checkout,
            scene.config,
            required_run_id="original",
        )
    assert original.run_id == "original" and base == ""
    assert generation == scene.generation


@pytest.mark.serial
def test_round25_unbound_continuation_refuses_with_abandon(scene, capsys):
    # Model a historical writer that never recorded checkout-generation evidence.
    scene.store._conn.execute(
        "DELETE FROM audit_events WHERE event = 'continuation_checkout_bound'"
    )
    scene.store._conn.commit()
    refused = False
    try:
        epic_cli._prepare_resume_record(
            scene.store, epic_ref="epic:x", item_id="42", config=scene.config
        )
    except typer.Exit as exc:
        refused = exc.exit_code == 2
    assert refused, "an unbound continuation must not adopt the present checkout"
    assert "kew epic abandon" in capsys.readouterr().err


def test_round25_claim_checkout_has_no_compatibility_arguments():
    assert set(inspect.signature(worktree.claim_checkout).parameters) == {
        "worktree_dir"
    }


class _ReplaceBeforeRegistration:
    def __init__(self, scene):
        self.scene = scene
        self.record = scene.store.record_continuation

    def __call__(self, *args, **kwargs):
        _replace_checkout(self.scene)
        return self.record(*args, **kwargs)


@pytest.mark.serial
def test_round25_publication_registration_keeps_bound_generation(scene, monkeypatch):
    ctx = CheckoutBinding._context(
        dispatch_core, scene.config, scene.store, run_id="publication-run"
    )
    ctx.epic_ref = "epic:x"
    dispatch_core._bind_initial_checkout(ctx, scene.checkout)
    before = scene.store.list_continuations("epic:x")
    monkeypatch.setattr(
        scene.store, "record_continuation", _ReplaceBeforeRegistration(scene)
    )
    refused = False
    try:
        dispatch_core._record_publication_continuation(
            ctx, SimpleNamespace(run_id="publication-run"), scene.checkout
        )
    except dispatch_core.PublicationError as exc:
        refused = "checkout identity changed" in str(exc)
    assert refused, "registration must not adopt bytes replaced after the run bound"
    assert scene.store.list_continuations("epic:x") == before
    assert len(scene.store.list_audit_events(event="continuation_checkout_bound")) == 1
