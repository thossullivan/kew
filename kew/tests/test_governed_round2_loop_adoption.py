"""Governed review rounds 1 and 2 (2026-09-21), the loop-resume findings of PR #1952.

A loop that predates recorded checkout bindings resumes in checkout mode by
adopting its checkout under the claim, through the real reuse path (round 2:
the round-1 test had patched that path away), migrating a legacy one-token
sidecar on the way. A loop with a recorded binding whose checkout would have
to be restored is refused before any Git write. Every loop refusal names a
verb that acts on a loop."""

from __future__ import annotations

import json
import shutil
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_round24_lease_and_claim_events import _loop_scene, scene  # noqa: F401

from kew import worktree
from kew.worktree import _generation_sidecar


def _git_on(branch: str) -> Mock:
    return Mock(return_value=SimpleNamespace(returncode=0, stdout=f"{branch}\n"))


def _events(scn, name: str) -> list:
    return scn.store.list_audit_events(event=name, limit=None)


def _pause(scn, loop):
    current = scn.store.get_loop(loop.loop_id)
    current.status = "paused"
    scn.store.save_loop(current)
    return current


def test_governed_round2_legacy_loop_adopts_its_checkout_through_the_reuse_path(
    scene,  # noqa: F811
    monkeypatch,
):
    loop, driver = _loop_scene(scene, monkeypatch, bind=False)
    monkeypatch.setattr(worktree, "run", _git_on("agent/issue-7"))
    assert _events(scene, "loop_checkout_bound") == []

    driver.resume(loop, "continue")

    driver._run_session.assert_called_once()
    bound = _events(scene, "loop_checkout_bound")
    assert len(bound) == 1
    assert bound[0].payload["loop_id"] == loop.loop_id
    assert bound[0].payload["checkout"] == str(scene.checkout.resolve())
    assert bound[0].payload["checkout_id"] == scene.generation
    adopted = _events(scene, "loop_checkout_adopted")
    assert len(adopted) == 1
    assert adopted[0].payload["reason"] == "loop predates recorded checkout bindings"

    # The second resume reads the recorded binding; nothing is adopted twice.
    loop = _pause(scene, loop)
    driver._run_session.reset_mock()
    driver.resume(loop, "continue again")
    driver._run_session.assert_called_once()
    assert len(_events(scene, "loop_checkout_bound")) == 1
    assert len(_events(scene, "loop_checkout_adopted")) == 1


def test_governed_round2_legacy_loop_migrates_a_one_token_sidecar(
    scene,  # noqa: F811
    monkeypatch,
):
    """A checkout from a build before complete evidence carries one bare token
    and no in-checkout nonce; the resume migrates it once and binds it."""
    loop, driver = _loop_scene(scene, monkeypatch, bind=False)
    sidecar = _generation_sidecar(scene.checkout)
    sidecar.write_text("a" * 32 + "\n")
    (scene.checkout / ".git" / "kew-generation").unlink()
    monkeypatch.setattr(worktree, "run", _git_on("agent/issue-7"))

    driver.resume(loop, "continue")

    driver._run_session.assert_called_once()
    binding = json.loads(sidecar.read_text())
    assert binding["token"] != "a" * 32
    assert (scene.checkout / ".git" / "kew-generation").read_text().strip() == (
        binding["token"]
    )
    bound = _events(scene, "loop_checkout_bound")
    assert len(bound) == 1
    assert json.loads(bound[0].payload["checkout_id"]) == binding
    assert (scene.checkout / "wip.txt").read_text() == "preserved work"


def test_governed_round2_legacy_loop_without_a_checkout_names_the_loop_verb(
    scene,  # noqa: F811
    monkeypatch,
):
    loop, driver = _loop_scene(scene, monkeypatch, bind=False)
    shutil.rmtree(scene.checkout)
    git = _git_on("agent/issue-7")
    monkeypatch.setattr(worktree, "run", git)

    with pytest.raises(RuntimeError, match="kew dispatch <issue> --loop") as refused:
        driver.resume(loop, "continue")

    assert "kew epic abandon" not in str(refused.value)
    git.assert_not_called()
    driver._run_session.assert_not_called()
    assert not scene.checkout.exists()
    assert _events(scene, "loop_checkout_bound") == []


def test_governed_round2_recorded_loop_on_another_branch_is_preserved(
    scene,  # noqa: F811
    monkeypatch,
):
    loop, driver = _loop_scene(scene, monkeypatch)
    git = _git_on("agent/other")
    monkeypatch.setattr(worktree, "run", git)

    with pytest.raises(RuntimeError, match="kew dispatch <issue> --loop") as refused:
        driver.resume(loop, "continue")

    assert "preserved" in str(refused.value)
    assert "kew epic abandon" not in str(refused.value)
    for call in git.call_args_list:
        assert "remove" not in call.args[0]
    assert (scene.checkout / "wip.txt").read_text() == "preserved work"
    driver._run_session.assert_not_called()


def test_governed_round2_canonical_legacy_loop_refusal_names_the_loop_verb(
    scene,  # noqa: F811
    monkeypatch,
):
    scene.config.publication.publish = "canonical"
    loop, driver = _loop_scene(scene, monkeypatch, bind=False)
    git = _git_on("agent/issue-7")
    monkeypatch.setattr(worktree, "run", git)

    with pytest.raises(RuntimeError, match="kew dispatch <issue> --loop") as refused:
        driver.resume(loop, "continue")

    assert "kew epic abandon" not in str(refused.value)
    assert str(scene.checkout) in str(refused.value)
    git.assert_not_called()
    driver._run_session.assert_not_called()
    assert scene.store.get_loop(loop.loop_id).status == "paused"
    assert _events(scene, "loop_checkout_bound") == []


def test_governed_round4_every_checkout_refusal_exits_two(monkeypatch):
    """The CLI guard turns a checkout refusal into exit 2, because callers read
    2 as an empty queue and would otherwise exit 0 on a refused dispatch. It
    enumerated two of the three categories by hand and missed the identity
    refusal (governed review round 4, 2026-09-21)."""
    import typer

    from kew import cli, dispatch_core
    from kew.continuation_recovery import (
        ActiveContinuationRefusal,
        CheckoutAdmissionRefusal,
        checkout_refusal_categories,
    )

    categories = checkout_refusal_categories()
    for refusal in (
        CheckoutAdmissionRefusal,
        ActiveContinuationRefusal,
        dispatch_core._CheckoutIdentityRefused,
    ):
        assert refusal.category in categories, refusal

    for category in sorted(categories):
        outcome = SimpleNamespace(category=category, exit_code=0, run_id="")
        monkeypatch.setattr(
            cli, "execute_dispatch", lambda _outcome=outcome, **_k: _outcome
        )
        with pytest.raises(typer.Exit) as exit_info:
            cli._execute_and_capture_outcome(
                issue=SimpleNamespace(number=42),
                config=None,
                store=None,
                notifier=None,
                force_budget=False,
                approve=False,
                model=None,
            )
        assert exit_info.value.exit_code == 2, category


def test_governed_round5_a_restored_checkout_rebinds_and_resumes(scene, monkeypatch):  # noqa: F811
    """A loop whose checkout is gone restores it and resumes.

    Rebinding under the PRIOR run id always refused, because a recorded binding
    is immutable, so this path raised every time (governed review round 5,
    2026-09-21). The resuming run records its own binding for the directory it
    just created, keeping the lineage and the publication base.
    """
    import shutil

    from kew import worktree

    loop, driver = _loop_scene(scene, monkeypatch)
    before = _events(scene, "loop_checkout_bound")
    assert len(before) == 1
    shutil.rmtree(scene.checkout)

    def restore(worktree_dir, branch_name):
        # Stand in for the clone: a fresh directory with its own git metadata.
        worktree_dir.mkdir(parents=True, exist_ok=True)
        (worktree_dir / ".git").mkdir(exist_ok=True)
        return worktree_dir

    monkeypatch.setattr(worktree, "_restore_worktree", restore)
    monkeypatch.setattr(worktree, "run", _git_on("agent/issue-7"))

    driver.resume(loop, "continue")

    driver._run_session.assert_called_once()
    bound = _events(scene, "loop_checkout_bound")
    assert len(bound) == 2
    # The new record belongs to the run that restored the checkout, which is
    # why it can be written at all: a recorded binding is immutable, so the
    # prior run's could never carry the restored generation.
    assert bound[1].run_id != bound[0].run_id
    assert bound[1].payload["checkout"] == str(scene.checkout.resolve())
    assert bound[1].payload["lineage_run_id"] == before[0].payload["lineage_run_id"]


def test_governed_round5_removing_a_checkout_tree_takes_its_lock(tmp_path):
    """The claim lock lives beside the checkout, so it outlived every removed
    tree and left a file per sandboxed dispatch and per reviewer worktree in
    the system temp directory (governed review round 5, 2026-09-21)."""
    from kew.worktree import claim_checkout, remove_checkout_tree

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    lock = tmp_path / "checkout.lock"

    claim_checkout(checkout)
    assert lock.exists()

    remove_checkout_tree(checkout)

    assert not checkout.exists()
    assert not lock.exists()

    # Claiming again after removal still works.
    checkout.mkdir()
    claim_checkout(checkout)
    remove_checkout_tree(checkout)
    assert not lock.exists()
