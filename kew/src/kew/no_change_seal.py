"""Seal the work-package evidence a no-change candidate is attested from.

A no-change judgment is only as good as the package it points at: attestation
(:func:`kew.dispatch_core._attest_no_change`) asks for a *trusted base recorded
for the run* and a *complete work package* declaring ``workspace`` scope, and
without them the candidate lands with no evidence and can never be confirmed.

Both publishing paths used to leave that unsealed. The host broker publishes
through the agent and the contained path publishes inside the sandbox, so
nothing on either one sealed the receipt attestation reads (#1610). This module
seals the one package a zero-diff exit can honestly produce — an EMPTY,
workspace-scoped package at the base — and only after the host has re-read the
workspace itself and found it identical to that base. A workspace that still
carries work refuses the seal, because an empty package would then be a false
statement about it, and an unattested candidate is always the safe outcome.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from kew.artifact_receipt import ArtifactReceipt, ReceiptError, seal_package_receipt
from kew.artifact_store import PackageStore, PackageStoreError, PreservedPackage
from kew.publication_artifact import WORKSPACE_SCOPE, Artifact, ArtifactRejected
from kew.subprocess_utils import run

__all__ = [
    "VERIFICATION_EMPTY_WORKSPACE",
    "NoChangeSealError",
    "seal_no_change_package",
]

#: The verification an empty no-change package carries: the host read the
#: workspace at judgment time and found HEAD naming the base's own tree with
#: nothing staged, unstaged or untracked beside it. Deliberately NOT
#: :data:`~kew.publication_repository.VERIFICATION_EXACT_TREE` — that one means
#: a package was applied twice in the canonical repository, which never
#: happened here, and sealing a receipt under a verification it did not earn is
#: exactly the kind of borrowed credibility a receipt exists to prevent.
VERIFICATION_EMPTY_WORKSPACE = "empty_workspace"

_OID_LENGTHS = (40, 64)
_HEX = "0123456789abcdef"


class NoChangeSealError(RuntimeError):
    """The host could not prove an empty workspace, so it sealed nothing."""


@dataclass(frozen=True)
class _EmptyWorkspace:
    """The three facts a receipt reads off a promoted tree (``PromotedLike``).

    Nothing was promoted: the tree the host would publish is the base's own,
    which is what makes this a no-change rather than a publication.
    """

    base_sha: str
    tree_sha: str
    verification: str = VERIFICATION_EMPTY_WORKSPACE


def seal_no_change_package(
    store: Any,
    *,
    worktree_dir: Path,
    packages_root: Path,
    base_sha: str,
    source_kind: str,
    source_id: str,
    run_id: str,
    attempt_number: int,
    now: datetime | str,
    verified_artifact: Artifact | None = None,
    verified_tree_sha: str | None = None,
) -> ArtifactReceipt:
    """Seal one attempt's empty workspace package, and return its receipt.

    Idempotent, and never destructive: an attempt that already sealed a receipt
    keeps it. That matters most when the receipt names REAL work — the empty
    package must never overwrite the package a publication produced, because
    attestation would then read "nothing to change" off an attempt that changed
    something.
    """
    existing = store.get_artifact_receipt(run_id=run_id, attempt_number=attempt_number)
    if existing is not None:
        return existing  # type: ignore[no-any-return]
    if verified_artifact is None:
        tree_sha = _empty_workspace_tree(worktree_dir, base_sha)
        package = _preserve_empty_package(packages_root, worktree_dir, base_sha)
    else:
        tree_sha = _require_verified_empty(
            verified_artifact,
            base_sha=base_sha,
            tree_sha=verified_tree_sha,
        )
        package = _preserve_verified_package(packages_root, verified_artifact)
    try:
        return seal_package_receipt(
            store,
            package=package,
            promoted=_EmptyWorkspace(base_sha=base_sha, tree_sha=tree_sha),
            source_kind=source_kind,
            source_id=source_id,
            run_id=run_id,
            attempt_number=attempt_number,
            sealed_at=now,
        )
    except ReceiptError as exc:
        raise NoChangeSealError(f"the empty package sealed no receipt: {exc}") from exc


def _require_verified_empty(
    artifact: Artifact, *, base_sha: str, tree_sha: str | None
) -> str:
    """Validate the canonical no-op facts without reopening the checkout."""
    if artifact.base_sha != base_sha:
        raise NoChangeSealError(
            f"the verified package base {artifact.base_sha} differs from {base_sha}"
        )
    if artifact.scope != WORKSPACE_SCOPE:
        raise NoChangeSealError("the verified empty package lacks workspace scope")
    if artifact.entries or artifact.deletions:
        raise NoChangeSealError("the verified package is not empty")
    _refuse_omitted_nested_repositories(artifact)
    if not tree_sha:
        raise NoChangeSealError("the verified empty package has no canonical tree")
    return tree_sha


def _refuse_omitted_nested_repositories(artifact: Artifact) -> None:
    """Refuse an empty package whose walk stepped over a nested checkout.

    Native extraction does not enter an untracked directory that is itself a
    git worktree or clone (#2201), so a workspace whose only work sits inside
    one extracts EMPTY — and an empty package would then say "nothing was
    done" about a directory nobody looked in. The sibling path refuses exactly
    this workspace: ``git status --untracked-files=all`` reports the nested
    directory, and :func:`_refuse_dirty_workspace` calls that carried work.
    The two paths have to agree, or which producer ran decides whether a run
    is attested.
    """
    nested = artifact.omitted_nested_repositories
    if nested:
        # A count, never the paths: they are agent-chosen names, and this
        # string reaches an operator's log.
        raise NoChangeSealError(
            f"the extraction left {len(nested)} nested git worktree or repository"
            " director(ies) unread, so an empty package is not a complete"
            " statement about the workspace"
        )


def _preserve_verified_package(
    packages_root: Path, artifact: Artifact
) -> PreservedPackage:
    """Preserve the exact artifact already verified by canonical publication."""
    try:
        return PackageStore(packages_root).preserve(artifact)
    except (PackageStoreError, ArtifactRejected, OSError) as exc:
        raise NoChangeSealError(f"the empty package was not preserved: {exc}") from exc


def _preserve_empty_package(
    packages_root: Path, worktree_dir: Path, base_sha: str
) -> PreservedPackage:
    """Place the empty workspace package durably, under its own digest."""
    artifact = Artifact(
        base_sha=base_sha,
        entries=(),
        # No entry, so no blob is ever opened through this directory; the field
        # is what an artifact READS content from, and there is no content.
        blobs_dir=worktree_dir,
        deletions=(),
        scope=WORKSPACE_SCOPE,
    )
    try:
        return PackageStore(packages_root).preserve(artifact)
    except (PackageStoreError, ArtifactRejected, OSError) as exc:
        raise NoChangeSealError(f"the empty package was not preserved: {exc}") from exc


def _empty_workspace_tree(worktree_dir: Path, base_sha: str) -> str:
    """The tree the host would publish, proven to be the base's own tree.

    Three facts, every one of them read here rather than assumed: the base
    names a tree, HEAD names that same tree, and git reports nothing staged,
    unstaged or untracked beside it. "The run exited 0 and opened no PR" is not
    one of them — that is an absence, and reading an absence as an empty
    workspace is the mistake the whole no-change lineage exists to refuse.
    """
    base_tree = _rev_parse(worktree_dir, f"{base_sha}^{{tree}}")
    head_tree = _rev_parse(worktree_dir, "HEAD^{tree}")
    if base_tree != head_tree:
        raise NoChangeSealError(
            f"HEAD names tree {head_tree}, not the base's {base_tree}, so the"
            " workspace carries committed work"
        )
    _refuse_dirty_workspace(worktree_dir)
    return base_tree


def _refuse_dirty_workspace(worktree_dir: Path) -> None:
    """Refuse a workspace with anything staged, unstaged or untracked.

    ``--untracked-files=all`` on purpose: the default collapses an untracked
    directory into one line, and an agent that wrote files without committing
    them is precisely the case a committed-diff-only reading calls empty.
    """
    result = _git(worktree_dir, "status", "--porcelain", "--untracked-files=all")
    if result.returncode != 0:
        raise NoChangeSealError(
            f"the workspace at {worktree_dir} could not be read as a repository"
        )
    dirty = [line for line in (result.stdout or "").splitlines() if line.strip()]
    if dirty:
        # A count, never the paths: they are agent-chosen names, and this
        # string reaches an operator's log.
        raise NoChangeSealError(
            f"the workspace carries {len(dirty)} uncommitted path(s), so it is"
            " not a no-change"
        )


def _rev_parse(worktree_dir: Path, ref: str) -> str:
    """The object id *ref* names, or a refusal. Nothing is ever guessed."""
    result = _git(worktree_dir, "rev-parse", ref)
    oid = (result.stdout or "").strip()
    if result.returncode != 0 or len(oid) not in _OID_LENGTHS:
        raise NoChangeSealError(f"{ref} does not name an object in {worktree_dir}")
    if any(char not in _HEX for char in oid):
        raise NoChangeSealError(f"{ref} did not resolve to an object id")
    return oid


def _git(worktree_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """One allowlisted git read. A git that could not run is a refusal."""
    try:
        return run(["git", *args], cwd=worktree_dir, check=False)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise NoChangeSealError(f"git {args[0]} could not run: {exc}") from exc
