"""Persisted kernel identity survives rewritten checkout metadata."""

import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import typer
from test_dispatch_core import TestInitialCanonicalCheckoutBinding as CheckoutBinding
from test_docs_truthfulness import _read
from test_round25_checkout_mode_generation import scene as _round25_scene
from test_worktree import _git_out

from kew import dispatch_core, worktree
from kew.cli import epic as epic_cli
from kew.continuation_recovery import record_run_checkout_generation
from kew.loops import delegate
from kew.run_store import SqliteRunStore

scene = _round25_scene


def _forge_replacement(checkout):
    """Copy the original nonce and rewrite every sidecar stat field."""
    sidecar = worktree._generation_sidecar(checkout)
    original = json.loads(sidecar.read_text())
    preserved = checkout.with_name(checkout.name + "-preserved")
    checkout.rename(preserved)
    shutil.copytree(preserved, checkout)
    current = os.stat(checkout)
    original.update(device=current.st_dev, inode=current.st_ino)
    if hasattr(current, "st_birthtime"):
        original.update(birth_field="st_birthtime", birth_time=current.st_birthtime)
    sidecar.write_text(json.dumps(original))
    (checkout / ".git" / "kew-generation").write_text(original["token"])
    return preserved


def _context(scene, mode):
    scene.config.publication.publish = mode
    ctx = CheckoutBinding._context(
        dispatch_core, scene.config, scene.store, run_id="round26-run"
    )
    ctx.model_override = "model"
    ctx.epic_ref = None
    ctx.publication_base_sha = "a" * 40
    ctx.branch = scene.branch
    return ctx


def _git_calls(invocations):
    return [
        call.args[0]
        for call in invocations.call_args_list
        if Path(call.args[0][0]).name == "git"
    ]


@pytest.mark.serial
@pytest.mark.parametrize("mode", ["checkout", "canonical"])
def test_round26_forged_resume_refuses_before_git(scene, monkeypatch, mode):
    scene.config.publication.publish = mode
    _forge_replacement(scene.checkout)
    invocations = Mock(wraps=subprocess.Popen)
    monkeypatch.setattr(subprocess, "Popen", invocations)
    refused = False
    try:
        epic_cli._prepare_resume_record(
            scene.store, epic_ref="epic:x", item_id="42", config=scene.config
        )
    except typer.Exit as exc:
        refused = exc.exit_code == 2
    assert refused and _git_calls(invocations) == [], (
        "rewritten metadata must not admit a replacement before resume Git"
    )
    assert scene.store.list_continuations("epic:x") == [scene.record]


@pytest.mark.serial
@pytest.mark.parametrize("mode", ["checkout", "canonical"])
def test_round26_forged_loop_resume_refuses_before_git(scene, monkeypatch, mode):
    scene.config.publication.publish = mode
    _forge_replacement(scene.checkout)
    invocations = Mock(wraps=subprocess.Popen)
    monkeypatch.setattr(subprocess, "Popen", invocations)
    worktree.claim_checkout(scene.checkout)
    refused = False
    try:
        worktree.reuse_or_restore_worktree(
            issue_num=42, config=scene.config, expected_generation=scene.generation
        )
    except RuntimeError:
        refused = True
    assert refused and _git_calls(invocations) == [], (
        "loop resume must compare persisted evidence before Git"
    )


class _PaidAttack:
    def __init__(self, checkout):
        self.checkout = checkout
        self.refusal = None

    def __call__(self, **kwargs):
        _forge_replacement(self.checkout)
        try:
            kwargs["paid_launch_check"]()
        except dispatch_core.PublicationError as exc:
            self.refusal = exc
        return Mock()


@pytest.mark.serial
@pytest.mark.parametrize("mode", ["checkout", "canonical"])
def test_round26_forged_paid_launch_refuses(scene, monkeypatch, mode):
    ctx = _context(scene, mode)
    attack = _PaidAttack(scene.checkout)
    CheckoutBinding._stub_native_paid_boundary(monkeypatch, dispatch_core, attack)
    monkeypatch.setattr(
        dispatch_core, "_compute_prefix_routing", Mock(return_value=("", ""))
    )
    runner = SimpleNamespace(name="claude-code", publication_style="agent")
    invocations = Mock(wraps=subprocess.Popen)
    monkeypatch.setattr(subprocess, "Popen", invocations)
    dispatch_core._launch_host(
        ctx,
        scene.checkout,
        SimpleNamespace(runner=runner, model="model"),
        runner,
        dispatch_core._EarlyRunInfo(),
        None,
    )
    assert attack.refusal is not None, (
        "the actual paid callback must refuse replacement"
    )
    assert "native paid launch" in str(attack.refusal)
    assert _git_calls(invocations) == []


class _GitPublication:
    def __init__(self, checkout):
        self.checkout = checkout

    def __call__(self, *_args):
        _git_out("rev-parse", "HEAD", cwd=self.checkout)


@pytest.mark.serial
@pytest.mark.parametrize("mode", ["checkout", "canonical"])
def test_round26_forged_publication_refuses_before_git(scene, monkeypatch, mode):
    ctx = _context(scene, mode)
    dispatch_core._bind_initial_checkout(ctx, scene.checkout)
    _forge_replacement(scene.checkout)
    monkeypatch.setattr(
        dispatch_core, "_root_checkout_integrity_check", Mock(return_value=None)
    )
    monkeypatch.setattr(
        dispatch_core, "_collect_special_category", Mock(return_value=None)
    )
    monkeypatch.setattr(
        dispatch_core, "_collect_success", _GitPublication(scene.checkout)
    )
    invocations = Mock(wraps=subprocess.Popen)
    monkeypatch.setattr(subprocess, "Popen", invocations)
    refused = False
    try:
        dispatch_core._collect_result(
            ctx,
            result=SimpleNamespace(run_id=ctx.run_id, error_category="", exit_code=0),
            worktree_dir=scene.checkout,
            run_sandboxed_path=False,
        )
    except dispatch_core.PublicationError:
        refused = True
    assert refused and _git_calls(invocations) == [], (
        "publication must refuse replacement before reaching Git"
    )


@pytest.fixture
def identity_scene(tmp_path):
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    generation = worktree.write_checkout_generation(checkout)
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        from kew.config import DispatchConfig

        config = DispatchConfig()
        config.publication.publish = "checkout"
        try:
            yield SimpleNamespace(
                checkout=checkout,
                generation=generation,
                store=store,
                config=config,
                branch="agent/issue-42",
            )
        finally:
            worktree.release_checkout_claims()


def _record_binding(scene, kind, generation):
    if kind == "continuation_checkout_bound":
        scene.store.record_continuation(
            "epic:x",
            "42",
            "round26-run",
            scene.branch,
            scene.checkout,
            checkout_generation=generation,
        )
    elif kind == "loop_checkout_bound":
        delegate._record_loop_checkout_binding(
            scene.store,
            SimpleNamespace(loop_id="loop", issue_num=42),
            scene.checkout,
            delegate._PublicationBinding(
                run_id="round26-run",
                base_sha="",
                lineage_run_id="round26-run",
                checkout_generation=generation,
            ),
            scene.config,
        )
    elif kind == "dispatch_checkout_bound":
        record_run_checkout_generation(
            scene.store,
            run_id="round26-run",
            item_id="42",
            branch=scene.branch,
            checkout=scene.checkout,
            generation=generation,
        )
    else:
        dispatch_core._bind_publication_base(
            scene.store,
            publish="canonical",
            base="a" * 40,
            run_id="round26-run",
            issue_num=42,
            item_id="42",
            branch=scene.branch,
            checkout=scene.checkout,
        )


@pytest.mark.parametrize(
    "kind",
    [
        "continuation_checkout_bound",
        "loop_checkout_bound",
        "dispatch_checkout_bound",
        dispatch_core._PUBLICATION_BASE_EVENT,
    ],
)
def test_round26_evidence_is_complete_and_immutable(identity_scene, kind):
    scene = identity_scene
    _record_binding(scene, kind, scene.generation)
    [event] = scene.store.list_audit_events(event=kind)
    encoded = event.payload["checkout_id"]
    evidence = json.loads(encoded) if encoded.startswith("{") else {}
    assert {"device", "inode", "birth_field", "nonce", "token"} <= evidence.keys(), (
        "the immutable event must carry complete identity, not just an identifier"
    )
    current = os.stat(scene.checkout)
    assert (evidence["device"], evidence["inode"]) == (current.st_dev, current.st_ino)
    if hasattr(current, "st_birthtime"):
        assert evidence["birth_time"] == current.st_birthtime
    else:
        assert evidence["birth_field"] == "none" and "birth_time" not in evidence
    assert (
        evidence["nonce"]
        == (scene.checkout / ".git" / "kew-generation").read_text().strip()
    )
    replacement = worktree.write_checkout_generation(scene.checkout)
    with pytest.raises((RuntimeError, ValueError)):
        _record_binding(scene, kind, replacement)
    with SqliteRunStore(scene.store._db_path) as reopened:
        assert reopened.list_audit_events(event=kind) == [event]


def test_round26_descriptor_survives_replacement_and_closes(identity_scene):
    scene = identity_scene
    ctx = _context(scene, "checkout")
    dispatch_core._bind_initial_checkout(ctx, scene.checkout)
    binding = getattr(ctx, "checkout_binding", None)
    assert binding is not None, "run admission must retain a directory descriptor"
    original = os.fstat(binding.descriptor)
    assert not os.get_inheritable(binding.descriptor)
    _forge_replacement(scene.checkout)
    assert os.fstat(binding.descriptor).st_ino == original.st_ino
    with pytest.raises(dispatch_core.PublicationError, match="binding mismatch"):
        dispatch_core._require_bound_checkout_generation(
            ctx, scene.checkout, scene.generation, boundary="publication"
        )
    worktree.release_checkout_claim(scene.checkout)
    with pytest.raises(OSError):
        os.fstat(binding.descriptor)


class _LineageBoundary:
    def __init__(self):
        self.bind = dispatch_core._bind_publication_lineage

    def __call__(self, ctx, record, base):
        binding = getattr(ctx, "checkout_binding", None)
        assert binding is not None, (
            "continuation must hold its directory before lineage recording"
        )
        assert os.fstat(binding.descriptor).st_ino == os.stat(ctx.worktree_dir).st_ino
        self.bind(ctx, record, base)


@pytest.mark.parametrize("mode", ["checkout", "canonical"])
def test_round26_continuation_holds_before_base_recording(
    identity_scene, monkeypatch, mode
):
    scene = identity_scene
    scene.config.publication.publish = mode
    CheckoutBinding._save_checkout_producer(scene.store, "producer-a")
    if mode == "canonical":
        dispatch_core._bind_publication_base(
            scene.store,
            publish=mode,
            base="a" * 40,
            run_id="producer-a",
            issue_num=42,
            item_id="42",
            branch=scene.branch,
            checkout=scene.checkout,
        )
    ctx = CheckoutBinding()._claim_checkout_continuation(
        dispatch_core, scene.config, scene.store, scene.checkout
    )
    monkeypatch.setattr(dispatch_core, "_bind_publication_lineage", _LineageBoundary())
    dispatch_core._continuation_base(ctx)


def test_round26_descriptor_is_compared_with_evidence(identity_scene, monkeypatch):
    scene = identity_scene
    ctx = _context(scene, "checkout")
    dispatch_core._bind_initial_checkout(ctx, scene.checkout)
    current = os.stat(scene.checkout)
    replacement = SimpleNamespace(st_dev=222, st_ino=333, st_mode=current.st_mode)
    monkeypatch.setattr(os, "fstat", Mock(return_value=replacement))
    refused = False
    try:
        dispatch_core._require_bound_checkout_generation(
            ctx, scene.checkout, scene.generation, boundary="paid launch"
        )
    except dispatch_core.PublicationError:
        refused = True
    assert refused, "the held descriptor must match persisted evidence"


class _OpenRecorder:
    def __init__(self):
        self.open = os.open
        self.descriptors = []

    def __call__(self, *args, **kwargs):
        descriptor = self.open(*args, **kwargs)
        self.descriptors.append(descriptor)
        return descriptor


def test_round26_failed_capture_closes_descriptor(identity_scene, monkeypatch):
    scene = identity_scene
    current = os.stat(scene.checkout)
    replacement = SimpleNamespace(st_dev=222, st_ino=333, st_mode=current.st_mode)
    opened = _OpenRecorder()
    with monkeypatch.context() as changed:
        changed.setattr(os, "open", opened)
        changed.setattr(os, "fstat", Mock(return_value=replacement))
        refused = False
        try:
            dispatch_core._bind_initial_checkout(
                _context(scene, "checkout"), scene.checkout
            )
        except dispatch_core.PublicationError:
            refused = True
    assert refused and len(opened.descriptors) == 1, (
        "binding capture must reject a descriptor with different kernel identity"
    )
    with pytest.raises(OSError):
        os.fstat(opened.descriptors[0])


def test_round26_sidecar_disagreement_is_tampered(identity_scene):
    scene = identity_scene
    sidecar = worktree._generation_sidecar(scene.checkout)
    changed = json.loads(sidecar.read_text())
    changed["device"], changed["inode"] = 222, 333
    sidecar.write_text(json.dumps(changed))
    refusal = None
    try:
        dispatch_core._require_checkout_generation(
            scene.checkout, scene.generation, boundary="base recording"
        )
    except dispatch_core.PublicationError as exc:
        refusal = exc
    assert refusal is not None and "tampered" in str(refusal), (
        "metadata disagreement must be reported as tampering against stored evidence"
    )


def test_round26_threat_model_statement_is_bound():
    for path in ("docs/threat-model.md", "docs/security.md"):
        assert "A host agent is outside this boundary." in _read(path)
        assert "Containment is the defense for that case." in _read(path)
    truthfulness = _read("kew/tests/test_docs_truthfulness.py")
    assert "def test_round26_checkout_binding_threat_model():" in truthfulness
