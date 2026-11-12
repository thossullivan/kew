"""Refuse a base sync made by COPYING files instead of merging (#2457).

The incident, on PR #2438: a review-remediation run produced one ordinary
commit that changed 68 files, 63 of which were byte-identical to versions on
``main`` the branch had never authored. The agent had brought the base's newer
content in by copying files — not by merging it — so the PR's diff against its
unmoved merge base grew from ~45 files to 108. The next review rounds then
reviewed the base's own code as if this node had written it, and when the base
moved again five of the copies conflicted and the node was held as
``base_integration_conflict``. A copy that had landed on a file the branch DID
change would have erased the branch's work silently; that one missed by luck.

What this module decides, for one publication, is which written or deleted
paths are such a copy. A written path qualifies when all of these hold:

* the content is byte-identical to the base branch's head version of it — so
  it is the base's authorship, arriving here whole;
* the trusted base (the branch head this publication applies to) does not
  already hold that content — otherwise nothing is being introduced;
* the base the host itself INTEGRATED into this checkout does not hold it
  either. That integration is kew's own ``git merge origin/<base>`` from
  checkout materialization, so the content it brought is expected and must
  never stop a publication (#2362). It is also what keeps a legitimate
  remediation — "revert this file to how the base has it" — out of the way of
  this rule: the base's version is exactly what the integration holds.

A deletion is the same copy with absence for content (#2633): the base head
no longer holds the path, while the branch and the integration both still do.
An agent that deletes what the base deleted since kew integrated it has synced
the base by hand exactly as surely as one that copies a newer file.

Every one of those three trees has to actually answer. A tree that would not
say what it holds is not a tree that lacks the content — nor one that holds it
— so the rule cannot judge, and an unjudged publication is HELD rather than
pushed (#2633): failing open let a single unreadable tree wave a copy through
— see :func:`kew.publication_canonical._guard_copy_sync`.

What is left is content from a base head the host never merged into this tree,
which an agent can only have fetched and copied. Those paths are named, and the
publication is held for the operator rather than pushed: the right tool for a
base sync is a merge (``kew resolve``), which publishes a real second parent.

A publication that IS a merge commit is never judged here — see
:func:`kew.publication_canonical.publish_checkout`, which is where the parent
count lives.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Iterable

    from kew.publication_filter import ManifestEntry

__all__ = [
    "COPY_SYNC_DELETION_REASON",
    "COPY_SYNC_REASON",
    "CopySync",
    "copy_sync_flags",
]

#: Why one path is held, with the base branch's own name in it so the operator
#: reads a fact about their repository rather than a rule number.
COPY_SYNC_REASON = (
    "copied from {label} rather than merged — this content is {label}'s own, "
    "and neither the branch nor the base kew integrated holds it; sync the "
    "base with a merge (kew resolve), never by copying files"
)

#: Why one DELETED path is held: the deletion is the base's, not the branch's.
COPY_SYNC_DELETION_REASON = (
    "deleted because {label} deleted it, rather than merged — the removal is "
    "{label}'s own, and both the branch and the base kew integrated still hold "
    "the file; sync the base with a merge (kew resolve), never by copying files"
)


@dataclass(frozen=True)
class CopySync:
    """The base head a publication must not introduce by copy.

    *sha* is the base branch's head as the HOST resolved it from the remote at
    publication time, never a value read out of the checkout. *label* is that
    branch's name, carried only so a hold reason can say which base.

    *integrated* is the commit the host's own base integration merged, when it
    is not the publication's ``merged_base_sha``. That sha earns the
    protected-path exemption only as a declared parent (#2632), while this
    rule's question — did the content arrive by kew's merge or by a copy? —
    is answered by the host's record of what it merged, parent or not.
    """

    sha: str
    label: str
    integrated: str | None = None


def copy_sync_flags(
    entries: Iterable[ManifestEntry],
    *,
    published_paths: Collection[str],
    base_digest: Callable[[str], str | None],
    integrated_digest: Callable[[str], str | None],
    upstream_digest: Callable[[str], str | None],
    label: str,
    deletions: Iterable[str] = (),
) -> tuple[str, ...]:
    """The hold reasons this publication earns, one per copied path.

    *entries* are the artifact's written files and *deletions* the paths it
    removed; only those that survived the filter and actually change the tree
    (*published_paths*) can be a copy, so the rest are not judged.

    Each digest reader answers "what does that tree hold at this path", with
    ``None`` for "nothing". None of them may answer "I could not tell": a
    tree that will not say what it holds is evidence neither way, and a
    sentinel compared against a digest would silently pick one. They are
    expected to RAISE instead, and the caller holds the publication
    (:func:`kew.publication_canonical._answered`).

    An entry carrying no digest is skipped: identity is the whole rule, and an
    empty digest would compare equal to another empty one.
    """
    published = set(published_paths)
    copied: dict[str, str] = {}
    for entry in entries:
        if entry.path in published and _written_copy(
            entry, base_digest, integrated_digest, upstream_digest
        ):
            copied[entry.path] = COPY_SYNC_REASON
    for path in deletions:
        if path in published and _deleted_copy(
            path, base_digest, integrated_digest, upstream_digest
        ):
            copied[path] = COPY_SYNC_DELETION_REASON
    return tuple(
        f"{path}: {copied[path].format(label=label)}" for path in sorted(copied)
    )


def _written_copy(
    entry: ManifestEntry,
    base_digest: Callable[[str], str | None],
    integrated_digest: Callable[[str], str | None],
    upstream_digest: Callable[[str], str | None],
) -> bool:
    """Whether *entry* writes the base head's bytes that no exempting tree holds."""
    if not entry.sha256 or upstream_digest(entry.path) != entry.sha256:
        return False
    if base_digest(entry.path) == entry.sha256:
        return False
    return integrated_digest(entry.path) != entry.sha256


def _deleted_copy(
    path: str,
    base_digest: Callable[[str], str | None],
    integrated_digest: Callable[[str], str | None],
    upstream_digest: Callable[[str], str | None],
) -> bool:
    """Whether deleting *path* repeats a deletion only the base head made."""
    if upstream_digest(path) is not None:
        return False
    return base_digest(path) is not None and integrated_digest(path) is not None
