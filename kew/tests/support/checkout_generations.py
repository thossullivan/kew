"""Explicit checkout bindings for continuation test data."""

from pathlib import Path

from kew.audit_identity import continuation_evidence
from kew.worktree import read_checkout_generation


def fixture_checkout_generation(checkout: str | Path) -> str:
    """Keep absent sidecars absent for historical and store-only test records."""
    return read_checkout_generation(Path(checkout))


def fixture_claim_evidence(
    store,
    epic_ref,
    item_id,
    original_run_id,
    branch,
    worktree,
    continuation_run_id,
):
    """Supply the exact observed claim generation through the public API."""
    record = store.get_continuation(
        epic_ref, item_id, original_run_id, branch, worktree
    )
    if record is None:
        return {}
    return continuation_evidence(
        phase="claim",
        epic_ref=epic_ref,
        item_id=item_id,
        original_run_id=original_run_id,
        continuation_run_id=continuation_run_id,
        branch=branch,
        worktree_identity=record.worktree_identity,
        generation=record.generation + 1,
        state="continuing",
        outcome="continuing",
    )
