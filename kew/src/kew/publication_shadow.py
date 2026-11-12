"""W1.1 node 5c: build what the broker WOULD publish, and compare it.

Shadow mode is the only honest proof that the broker drops nothing. It runs the
whole chain — extract, read, filter, apply — beside a real dispatch, and
compares the tree it produces against the tree the legacy path is about to
publish. Applying the diff base→HEAD onto base must reproduce HEAD's tree bit
for bit, so that comparison is total: one sha, no allowances.

**Nothing here can affect the publication it observes.** :func:`compare_
publication` does not raise — every failure, at every stage, becomes a
:class:`ShadowOutcome` with a status. A shadow that could break a dispatch would
be worse than no shadow, because it would make the safe option (leave it off)
the correct one.

The single exception is deliberate interruption: ``KeyboardInterrupt`` and
``SystemExit`` are re-raised, because they are the operator and the process
saying stop rather than the shadow failing. Suppressing them would make kew
unresponsive for the length of a container run and then publish anyway, having
been told not to.

It also never points host git at the agent's checkout. The checkout is
bind-mounted into a container and nothing else; even the legacy tree sha is
passed IN by the caller, which already has it. The only repository host git
touches here is the canonical one, fetched from the remote.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from kew.publication_apply import apply_to_tree
from kew.publication_extract import extract_in_container
from kew.publication_filter import (
    PublicationRefused,
    ReviewRequired,
    filter_artifact,
)
from kew.publication_repository import CanonicalRepository, provision

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "STATUS_DIVERGENT",
    "STATUS_FAILED",
    "STATUS_MATCH",
    "STATUS_REFUSED",
    "STATUS_REVIEW_REQUIRED",
    "ShadowOutcome",
    "compare_publication",
    "provision_canonical",
]

#: The trees agree. The broker would have published exactly this work.
STATUS_MATCH = "match"
#: Both trees were built and they differ — the finding shadow mode exists for.
STATUS_DIVERGENT = "divergent"
#: The filter refused an entry, so the broker would have published NOTHING while
#: the legacy path published. Distinct from divergence: not a fidelity gap but a
#: policy disagreement, and the two need different answers.
STATUS_REFUSED = "refused"
#: The filter flagged an entry for human review (CI code, hook config).
STATUS_REVIEW_REQUIRED = "review_required"
#: The comparison could not be made. Says nothing about the broker either way,
#: and must never be read as a match.
STATUS_FAILED = "failed"


@dataclass(frozen=True)
class ShadowOutcome:
    """What one shadow comparison found. Never an exception."""

    status: str
    detail: str = ""
    legacy_tree: str | None = None
    broker_tree: str | None = None
    entries: int = 0
    deletions: int = 0
    rejections: tuple[str, ...] = field(default_factory=tuple)
    flags: tuple[str, ...] = field(default_factory=tuple)
    #: Retained evidence, or ``None`` when there was nothing to look at. A
    #: matching comparison is thrown away; anything else is kept, so the disk
    #: only grows when someone has a reason to open it.
    evidence: Path | None = None

    @property
    def agrees(self) -> bool:
        return self.status == STATUS_MATCH


def provision_canonical(dest: Path, *, remote: str, base_sha: str) -> None:
    """A bare repo containing *base_sha*, fetched from *remote*.

    Bare, because nothing that follows needs a working tree. Fetched from the
    REMOTE rather than cloned from any local repository: reading a repository
    runs code from it (``uploadpack.packObjectsHook`` is honoured from the
    *source* config), and the remote is the one source no agent has written to.

    ``--depth 1`` on the commit itself — verified against GitHub, which serves a
    fetch of a bare full-length oid. Roughly 7MB for a repo the size of kew's,
    against a full clone, and the shadow needs exactly one commit.

    I09A moved the body of this into :func:`kew.publication_repository.provision`
    and left the name. The shadow had its own copy of "fetch the base somewhere
    the host owns", and it was the copy WITHOUT the host-ownership check on a
    local source — two implementations of one rule, one of which had already
    drifted. There is now one, and a guard added there covers both callers.
    """
    provision(dest, source=remote, base_sha=base_sha)


def _destination_state(canonical: Path, base_sha: str) -> dict[str, str]:
    """Every path at *base_sha*, with its mode.

    The filter's collision and mode-escalation rules are about the destination.
    Without this they never fire, and the shadow would report clean because it
    never asked the question rather than because the answer was clean.
    """
    return CanonicalRepository(path=canonical, base_sha=base_sha).base_state()


def compare_publication(
    *,
    runtime: str,
    image: str,
    proxy_environment: str,
    checkout_dir: Path,
    workspace: Path,
    remote: str,
    base_sha: str,
    legacy_tree: str,
    slug: str,
) -> ShadowOutcome:
    """Build the broker's tree and compare it against *legacy_tree*.

    *legacy_tree* is what the legacy path is about to publish — ``HEAD^{tree}``
    of the agent's checkout — and is passed in rather than read here, so this
    function runs no host git against a directory an agent can write.

    *workspace* must not exist. It is removed on a match and kept otherwise.
    """
    try:
        workspace.mkdir(mode=0o700, parents=True)
        workspace.chmod(0o700)  # mkdir's mode is masked by the umask
    except OSError as exc:
        return ShadowOutcome(status=STATUS_FAILED, detail=f"workspace: {exc}")

    try:
        return _compare(
            runtime=runtime,
            image=image,
            proxy_environment=proxy_environment,
            checkout_dir=checkout_dir,
            workspace=workspace,
            remote=remote,
            base_sha=base_sha,
            legacy_tree=legacy_tree,
            slug=slug,
        )
    except PublicationRefused as exc:
        return ShadowOutcome(
            status=STATUS_REFUSED,
            detail=str(exc),
            legacy_tree=legacy_tree,
            rejections=tuple(f"{r.path}: {r.detail}" for r in exc.rejections),
            evidence=workspace,
        )
    except ReviewRequired as exc:
        return ShadowOutcome(
            status=STATUS_REVIEW_REQUIRED,
            detail=str(exc),
            legacy_tree=legacy_tree,
            flags=tuple(f"{f.path}: {f.reason}" for f in exc.flags),
            evidence=workspace,
        )
    except (KeyboardInterrupt, SystemExit):
        # The ONE thing that gets through. These are the operator and the
        # process saying stop; they are not shadow failures. Swallowing a Ctrl-C
        # would make kew unresponsive for the length of a container run and then
        # publish anyway, having been told to stop — a worse outcome than the
        # one the never-raises rule exists to prevent.
        raise
    except BaseException as exc:  # noqa: BLE001 — a shadow must never break a dispatch
        # BaseException, not Exception: `except Exception` let a KeyboardInterrupt
        # injected into a stage escape, and the test that was supposed to catch
        # that passed for an unrelated reason. The guarantee is worth stating
        # exactly — nothing escapes but deliberate interruption.
        return ShadowOutcome(
            status=STATUS_FAILED,
            detail=f"{type(exc).__name__}: {exc}",
            legacy_tree=legacy_tree,
            evidence=workspace,
        )


def _compare(
    *,
    runtime: str,
    image: str,
    proxy_environment: str,
    checkout_dir: Path,
    workspace: Path,
    remote: str,
    base_sha: str,
    legacy_tree: str,
    slug: str,
) -> ShadowOutcome:
    """The comparison itself. Raises freely; ``compare_publication`` catches."""
    canonical = workspace / "canonical.git"
    provision_canonical(canonical, remote=remote, base_sha=base_sha)

    artifact = extract_in_container(
        runtime=runtime,
        image=image,
        proxy_environment=proxy_environment,
        checkout_dir=checkout_dir,
        artifact_dir=workspace / "artifact",
        slug=slug,
        base_sha=base_sha,
    )
    existing_modes = _destination_state(canonical, base_sha)
    verdict = filter_artifact(
        artifact,
        existing_modes=existing_modes,
        destination_paths=existing_modes.keys(),
    )
    broker_tree = apply_to_tree(
        canonical,
        base_sha=base_sha,
        approved=verdict.approved(),
        deletions=verdict.deletions(),
        content=artifact.content,
        index_file=workspace / "shadow.index",
    )

    if broker_tree == legacy_tree:
        # Nothing to look at, so nothing is kept.
        shutil.rmtree(workspace, ignore_errors=True)
        return ShadowOutcome(
            status=STATUS_MATCH,
            legacy_tree=legacy_tree,
            broker_tree=broker_tree,
            entries=len(artifact.entries),
            deletions=len(artifact.deletions),
        )
    return ShadowOutcome(
        status=STATUS_DIVERGENT,
        detail="the broker's tree is not the tree the legacy path publishes",
        legacy_tree=legacy_tree,
        broker_tree=broker_tree,
        entries=len(artifact.entries),
        deletions=len(artifact.deletions),
        evidence=workspace,
    )
