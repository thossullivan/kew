"""Materialize the checkout a review run executes in — collision-free (#1290).

Reviews used to reuse the *implementation* worktree primitive
(:func:`kew.worktree.checkout_existing_worktree`), which creates a LOCAL branch
named after the PR head. When that exact branch is already checked out — in an
isolated implementation worktree, or in the operator's own clone — ``git
worktree add -b`` exits 255 and the review never launches. When the collision
instead landed on the *same* directory the implementation agent was using, the
review silently shared a dirty checkout and could clobber in-flight work.

This module owns a checkout that is safe by construction:

* it is a **standalone clone with its own ``.git``**, never a linked worktree
  of the operator's repository (W1.1 / D2, doors 2-4). A linked worktree's
  ``.git`` is a *file* pointing into the operator's main ``.git``, so an agent
  running ``git config core.hooksPath`` inside it writes the OPERATOR's config,
  and ``--git-common-dir`` resolves to the operator's ``.git`` — letting the
  agent plant ``hooks/pre-commit`` there directly. That trap then fires on the
  operator's own interactive ``git commit``, with no kew dispatch involved.
  Verified end to end before this was changed, not inferred;
* it lives under its own ``<worktree_base>/review/pr-<n>`` directory, so it is
  never the implementation worktree;
* it fetches a PR branch that exists ONLY on origin into the clone, so
  reviewing a branch the operator has never checked out works rather than
  refusing with "pathspec did not match" (#1655);
* it detects the worktree that already holds the exact branch BEFORE
  materializing and, when there is one, checks out DETACHED at the PR head
  rather than creating a second local branch — no reset, no clean, no removal
  of somebody else's checkout;
* it refuses (never overwrites) a dirty review checkout, and holds an ``flock``
  claim for the life of the review so a second concurrent reviewer refuses
  instead of sharing the tree;
* it records the head sha it was cut at, so publication can bind to the
  intended PR head and reject an identity mismatch
  (:func:`kew.worktree.publish_existing_pr_branch`).

Every refusal raises :class:`ReviewCheckoutError` carrying a stable
``classification``; the caller persists and notifies it as a classified
pre-launch failure (see :mod:`kew.review_outcome`).
"""

from __future__ import annotations

import fcntl
import os
import shutil
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, TYPE_CHECKING

from kew.config import DispatchConfig
from kew.progress import _log, _warn
from kew.subprocess_utils import run
from kew.worktree import (
    BaseIntegrationError,
    ConflictInfo,
    clone_local_checkout,
    discard_checkout,
    fetch_missing_head,
    fetch_ref,
    has_uncommitted_changes,
    integrate_base_branch,
    is_linked_worktree,
)

if TYPE_CHECKING:
    from kew.run_store import RunRecord, RunStore

# Stable classification strings — persisted and notified verbatim, so treat
# them as a contract (dashboards and the audit trail key off them).
HEAD_UNRESOLVABLE = "head_unresolvable"
HEAD_MISMATCH = "head_mismatch"
CHECKOUT_BUSY = "checkout_busy"
CHECKOUT_DIRTY = "checkout_dirty"
CHECKOUT_UNUSABLE = "checkout_unusable"
# The checkout is fine; git could not run the base integration in it (#1455).
# Distinct from CHECKOUT_UNUSABLE so the operator is not sent to inspect a
# checkout that is not the problem.
BASE_INTEGRATION_FAILED = "base_integration_failed"
# The review directory holds (or, the ledger unreadable, may hold) a deferred
# publication's unpublished work, and the record of where that work moves to
# could not be written (#3186): the directory is left in place and the launch
# refused, so a fresh clone never takes the path the deferral names.
DEFERRED_CHECKOUT_UNRECORDED = "deferred_checkout_unrecorded"

_REVIEW_DIRNAME = "review"


class ReviewCheckoutError(RuntimeError):
    """A review could not be materialized safely — classified, never a traceback."""

    def __init__(self, classification: str, message: str) -> None:
        super().__init__(message)
        self.classification = classification


@dataclass
class ReviewCheckout:
    """A materialized, exclusively-claimed checkout for one review round."""

    path: Path
    head_sha: str
    branch: str
    detached: bool
    impl_worktree: Path | None
    conflict: ConflictInfo | None
    #: The base commit kew's own base integration merged into this tree, before
    #: the agent ran — host-resolved, never read back from the agent's work.
    #: Publication reads it to tell the base content IT brought from base
    #: content an agent copied in (#2457), and to make the protected-path
    #: exemption exact rather than "whatever the base branch has drifted to"
    #: (#2362). Empty when there was no base to integrate, or when the ref
    #: could not be read: the publication then judges nothing for a copy.
    integrated_base_sha: str = ""
    #: The tree the checkout holds once kew's own base integration ran, before
    #: the agent runs — host-resolved like *integrated_base_sha*. A remediation
    #: that publishes exactly this tree changed nothing, however far the merge
    #: moved the commit (#3276). Empty when the merge stopped on conflicts the
    #: agent must finish, or when HEAD could not be read.
    integrated_tree: str = ""
    claim: IO[str] | None = field(default=None, repr=False, compare=False)

    def release(self) -> None:
        """Drop the exclusive claim so the next reviewer can take the tree.

        Idempotent. The claim is an ``flock`` on an open descriptor, so a
        crashed or killed process releases it too — a stale claim can never
        wedge the checkout permanently.
        """
        if self.claim is None:
            return
        self.claim.close()
        self.claim = None


def review_checkout_path(config: DispatchConfig, pr_number: int) -> Path:
    """Where PR *pr_number*'s review checkout lives.

    Deliberately a sibling of — never equal to — the ``agent/issue-*``
    implementation worktrees, so a review can never be handed a tree an
    implementation agent is still writing to.
    """
    return Path(config.branches.worktree_base) / _REVIEW_DIRNAME / f"pr-{pr_number}"


def worktree_for_branch(branch: str) -> Path | None:
    """The worktree that currently has ``refs/heads/<branch>`` checked out.

    Matching is exact: ``agent/issue-7`` never matches ``agent/issue-70``.
    Returns ``None`` when no worktree (including the main checkout) holds it,
    or when the repository cannot be interrogated — the caller then takes the
    conservative path anyway.
    """
    result = run(["git", "worktree", "list", "--porcelain"], check=False)
    if result.returncode != 0:
        return None
    target = f"branch refs/heads/{branch}"
    current: Path | None = None
    for raw_line in (result.stdout or "").splitlines():
        line = raw_line.strip()
        if line.startswith("worktree "):
            current = Path(line[len("worktree ") :])
        elif line == target and current is not None:
            return current
    return None


def _fetch(branch_name: str, base_branch: str) -> None:
    """Refresh the remote refs the checkout is cut from (best-effort)."""
    run(["git", "fetch", "origin", branch_name, "--quiet"], check=False)
    if base_branch:
        run(["git", "fetch", "origin", base_branch, "--quiet"], check=False)


def _integrated_base_sha(review_dir: Path, base_branch: str) -> str:
    """The commit ``origin/<base_branch>`` names in the freshly fetched tree.

    Read right after the integration, so it is the commit the merge actually
    used — the host's own fact about what it brought in. Best-effort: an
    unreadable ref returns ``""``, which only takes away the copy-sync hold and
    the exact #2362 exemption.
    """
    if not base_branch:
        return ""
    result = run(
        [
            "git",
            "-C",
            str(review_dir),
            "rev-parse",
            "--verify",
            "--quiet",
            f"origin/{base_branch}^{{commit}}",
        ],
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _head_tree(review_dir: Path) -> str:
    """The tree HEAD holds in *review_dir*, or ``""`` when it cannot be read.

    Read right after kew's base integration and before the agent runs, so it
    is the host's own record of the tree the agent was handed (#3276).
    """
    result = run(
        [
            "git",
            "-C",
            str(review_dir),
            "rev-parse",
            "--verify",
            "--quiet",
            "HEAD^{tree}",
        ],
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _resolve_head_sha(branch_name: str) -> str:
    """The commit ``origin/<branch_name>`` points at, or a classified refusal."""
    result = run(
        ["git", "rev-parse", "--verify", f"origin/{branch_name}^{{commit}}"],
        check=False,
    )
    sha = result.stdout.strip() if result.returncode == 0 else ""
    if not sha:
        raise ReviewCheckoutError(
            HEAD_UNRESOLVABLE,
            f"cannot resolve origin/{branch_name} — the PR head is not fetchable",
        )
    return sha


def _quarantine_review_dir(
    review_dir: Path,
    config: DispatchConfig,
    *,
    pr_number: int,
    store: RunStore | None,
) -> None:
    """Move an existing review directory aside under canonical mode; a rename
    that fails is a classified refusal, never a reason to run git there.

    Only the newest preserved copy is kept (#2961): every failed review used to
    leave one more, and two refusal loops filled 33 GB. The caller holds the
    PR's claim, so no other reviewer is moving a copy aside meanwhile. The copy
    each deferred publication names is kept besides (#3186,
    :func:`_live_deferrals`): when the moved directory is the one a deferral
    was recorded in, its evidence is re-pointed at the preserved copy, so the
    deferral (and its hold) keep naming where the work is. A re-point that
    cannot be written puts the directory back and refuses the launch
    (``deferred_checkout_unrecorded``): a fresh clone at the recorded path
    would read as the deferred work. A ledger that cannot be read refuses the
    move the same way: which deferral the directory holds, and so which record
    must follow it, is unknown. A deferral whose copy the hold would call
    unknown prunes nothing.
    """
    from kew.worktree import quarantine_agent_checkout

    deferrals = _live_deferrals(store, pr_number)
    if store is not None and deferrals is None:
        _refuse_unrecorded_move(review_dir, config)
    try:
        preserved = quarantine_agent_checkout(
            review_dir, config, reason="a previous review left this checkout"
        )
    except RuntimeError as exc:
        raise ReviewCheckoutError(CHECKOUT_UNUSABLE, str(exc)) from exc
    if preserved is None or deferrals is None or store is None:
        return
    # Every known deferral follows the move, even when another one's copy is
    # unknown: only the prune waits on that, never the record of where a
    # known deferral's work went.
    protected = _follow_moved_deferrals(store, deferrals.known, review_dir, preserved)
    if deferrals.unknown is None:
        _prune_preserved(review_dir, keep=1, protected=protected)


def _refuse_unrecorded_move(review_dir: Path, config: DispatchConfig) -> None:
    """Refuse to move *review_dir* aside when the ledger saying which deferral
    it holds could not be read (#3186): moved unrecorded, a deferral's work
    would become an unprotected copy, its evidence naming the fresh clone."""
    if config.publication.publish != "canonical" or not review_dir.exists():
        return
    raise ReviewCheckoutError(
        DEFERRED_CHECKOUT_UNRECORDED,
        f"{review_dir} may hold a deferred publication's work and the run ledger "
        "could not be read to record its move; the checkout is left in place — "
        "publish or discard it, then retry once the ledger is readable",
    )


#: The run states whose work is finished but unpublished, waiting for a person
#: to publish or discard it from the review checkout it was built in (#3186):
#: the deferred-publication status, and the categories a held or
#: owner-reviewed publication records.
_DEFERRED_PUBLICATION_STATUSES = frozenset({"awaiting_publication"})
_DEFERRED_PUBLICATION_CATEGORIES = frozenset(
    {"publication_held", "publication_review_required", "held_publication"}
)


@dataclass(frozen=True)
class _LiveDeferral:
    """One deferred publication still waiting on the copy it names."""

    key: str
    evidence: dict[str, object]
    path: Path


@dataclass(frozen=True)
class _Deferrals:
    """What the ledger says about a PR's deferred publications: the ones whose
    copy is known and on disk, and the first deferred run (if any) whose copy
    is unknown — which stops every prune of the PR, and nothing else."""

    known: list[_LiveDeferral]
    unknown: str | None = None


#: The run kinds ``kew review`` records a PR's remediation under (an item's,
#: and a hand-started one's): the runs whose work is built in its checkout.
_REVIEW_CHECKOUT_RUN_KINDS = frozenset({"review_remediation", "review"})


def _in_deferred_state(run: RunRecord) -> bool:
    return (
        run.status in _DEFERRED_PUBLICATION_STATUSES
        or (run.error_category or "") in _DEFERRED_PUBLICATION_CATEGORIES
    )


def _live_deferrals(store: RunStore | None, pr_number: int) -> _Deferrals | None:
    """The deferred publications of *pr_number* whose copy is still on disk.

    A deferral protects exactly the one copy its evidence names while its run
    is in a deferred state and that copy exists — WHATEVER the PR head (#3186):
    kew's own merge-main step, GitHub's "Update branch", a rebase or a
    force-push all move the head without publishing the deferred work. Its row
    never settles, so a discarded deferral is told by its absent copy; at most
    one copy per deferral is ever kept.

    ``None`` — keep every copy, and re-point nothing — when there is no
    ledger or it cannot be read. A deferred run of the PR whose evidence the
    review stage's hold would read as "preserved checkout unknown" is named in
    ``unknown``: no copy can be told apart from the one holding its work, so
    nothing is pruned, but the known deferrals are still returned so their
    records follow a move.
    """
    if store is None:
        return None
    try:
        unknown = _deferral_with_unknown_checkout(store, pr_number)
        known = _evidenced_deferrals(store, pr_number)
    except Exception as exc:  # noqa: BLE001 — unreadable means keep
        _warn(
            f"Keeping every review checkout of PR #{pr_number}: the run ledger "
            f"could not be read ({exc})"
        )
        return None
    if unknown is not None:
        _warn(
            f"Keeping every review checkout of PR #{pr_number}: deferred run "
            f"{unknown} recorded no readable checkout, so its copy is unknown"
        )
    return _Deferrals(known, unknown)


def _deferral_with_unknown_checkout(store: RunStore, pr_number: int) -> str | None:
    """The run id of a deferred run of the PR whose checkout is unknown."""
    from kew.epic_holds import deferred_publication_key

    for record in store.list_by_issue(pr_number):
        if record.run_kind not in _REVIEW_CHECKOUT_RUN_KINDS:
            continue
        if not _in_deferred_state(record):
            continue
        evidence = _parsed_evidence(
            store.get_meta(deferred_publication_key(record.run_id))
        )
        if evidence is None or _recorded_checkout(evidence) is None:
            return record.run_id
    return None


def _parsed_evidence(raw: str | None) -> dict[str, object] | None:
    """Deferred-publication evidence as a table, or None when it is not one."""
    import json

    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        _warn("A deferred-publication record is not readable JSON")
        return None
    return parsed if isinstance(parsed, dict) else None


def _recorded_checkout(evidence: dict[str, object]) -> Path | None:
    """The absolute checkout *evidence* records, or None (unknown, as the
    review stage's hold reads a missing or relative one)."""
    recorded = Path(str(evidence.get("checkout") or ""))
    return recorded if recorded.is_absolute() else None


def _evidenced_deferrals(store: RunStore, pr_number: int) -> list[_LiveDeferral]:
    from kew.epic_holds import DEFERRED_PUBLICATION_META_PREFIX

    live: list[_LiveDeferral] = []
    after = ""
    while page := store.list_meta(DEFERRED_PUBLICATION_META_PREFIX, after=after):
        for key, value in page:
            deferral = _live_deferral(store, key, _parsed_evidence(value), pr_number)
            if deferral is not None:
                live.append(deferral)
        after = page[-1][0]
    return live


def _live_deferral(
    store: RunStore,
    key: str,
    evidence: dict[str, object] | None,
    pr_number: int,
) -> _LiveDeferral | None:
    """*evidence* as a deferral of *pr_number* still holding its copy, or None."""
    from kew.epic_holds import DEFERRED_PUBLICATION_META_PREFIX

    if evidence is None or evidence.get("pr") != pr_number:
        return None
    recorded = _recorded_checkout(evidence)
    if recorded is None or _copy_is_gone(recorded):
        return None
    run = store.get(key.removeprefix(DEFERRED_PUBLICATION_META_PREFIX))
    if run is None or not _in_deferred_state(run):
        return None
    return _LiveDeferral(key, evidence, recorded)


def _copy_is_gone(path: Path) -> bool:
    """Whether *path* is DEFINITELY not on disk — the review stage's own
    reading: a path that cannot be checked counts as present."""
    try:
        path.stat()
    except (FileNotFoundError, NotADirectoryError):
        return True
    except OSError as exc:
        _warn(f"Could not read the deferred checkout {path} ({exc}); kept")
    return False


def _follow_moved_deferrals(
    store: RunStore,
    deferrals: list[_LiveDeferral],
    review_dir: Path,
    preserved: Path,
) -> set[Path]:
    """Re-point each deferral recorded in *review_dir* at *preserved*, where
    its work now is; return every copy a deferral names.

    A re-point that cannot be written leaves the evidence naming *review_dir*,
    where the next launch would clone afresh and the work would read as that
    clone. So every re-point already written is undone, the directory is moved
    back, and the launch is refused with ``deferred_checkout_unrecorded``.
    """
    import json

    moved = review_dir.resolve(strict=False)
    written: list[_LiveDeferral] = []
    protected: set[Path] = set()
    for deferral in deferrals:
        path = deferral.path
        if path.resolve(strict=False) == moved:
            evidence = {**deferral.evidence, "checkout": str(preserved)}
            try:
                store.set_meta(deferral.key, json.dumps(evidence))
            except Exception as exc:
                _undo_moved_deferrals(store, written, review_dir, preserved)
                raise ReviewCheckoutError(
                    DEFERRED_CHECKOUT_UNRECORDED,
                    f"{review_dir} holds a deferred publication's work and its "
                    f"move could not be re-recorded ({exc}); the checkout is left "
                    "in place — publish or discard it, then retry",
                ) from exc
            written.append(deferral)
            path = preserved
        protected.add(path.resolve(strict=False))
    return protected


def _undo_moved_deferrals(
    store: RunStore,
    written: list[_LiveDeferral],
    review_dir: Path,
    preserved: Path,
) -> None:
    """Put a move whose re-point failed back: the evidence already re-pointed
    names *review_dir* again, and the directory returns there."""
    import json

    for deferral in written:
        try:
            store.set_meta(deferral.key, json.dumps(deferral.evidence))
        except Exception as exc:  # noqa: BLE001 — the refusal below says why
            _warn(f"Could not restore the deferred-publication record: {exc}")
    preserved.rename(review_dir)


def _preserved_copies(review_dir: Path) -> list[Path]:
    """The preserved copies of *review_dir*, oldest first.

    The stamp ``quarantine_agent_checkout`` writes sorts by time, and the
    literal ``.preserved-`` keeps ``pr-1`` from matching ``pr-10``'s copies.
    """
    parent = review_dir.parent
    if not parent.is_dir():
        return []
    return sorted(
        path
        for path in parent.glob(f"{review_dir.name}.preserved-*")
        if path.is_dir() and not path.is_symlink()
    )


def _prune_preserved(
    review_dir: Path, *, keep: int, protected: AbstractSet[Path] = frozenset()
) -> list[Path]:
    """Remove all but the newest *keep* preserved copies, never one in
    *protected* (a live deferral's, #3186); return the removed."""
    copies = [
        path
        for path in _preserved_copies(review_dir)
        if path.resolve(strict=False) not in protected
    ]
    doomed = copies[: len(copies) - keep] if keep else copies
    for path in doomed:
        shutil.rmtree(path, ignore_errors=True)
    return doomed


def discard_review_checkout(checkout: ReviewCheckout) -> None:
    """Remove the tree of a review no agent ever ran in (#2961).

    A launch refused before start leaves nothing worth debugging — a fresh
    clone at a known head — so it is not kept for the next launch to move
    aside. Only a checkout this process still claims is removed.
    """
    if checkout.claim is None:
        return
    discard_checkout(checkout.path, release_claim=False)


def forget_review_checkouts(
    config: DispatchConfig,
    pr_number: int,
    *,
    store: RunStore | None,
    merged: bool,
) -> list[Path]:
    """Remove every review checkout of a PR kew saw merged or closed (#2961).

    The live checkout and each preserved copy go; nothing will review the PR
    again. A checkout another reviewer still claims is left whole — logged,
    and the next sighting tries again. A PR closed WITHOUT merging keeps the
    copy each deferred publication names (#3186): its unpublished work may
    still be wanted; a merged PR keeps none, since a person merged it without
    that work. A ledger that cannot be read keeps everything. Returns what was
    removed.
    """
    review_dir = review_checkout_path(config, pr_number)
    # First, before any ledger read: a PR with nothing on disk costs a stat.
    if not review_dir.exists() and not _preserved_copies(review_dir):
        return []
    try:
        claim = _claim(review_dir)
    except ReviewCheckoutError as exc:
        _log(
            f"Keeping the review checkouts of PR #{pr_number} for now: {exc} "
            "(the next sighting tries again)"
        )
        return []
    try:
        protected = _closed_pr_protected(store, pr_number, merged=merged)
        if protected is None:
            return []
        removed = _prune_preserved(review_dir, keep=0, protected=protected)
        if review_dir.exists() and review_dir.resolve(strict=False) not in protected:
            discard_checkout(review_dir, release_claim=False)
            removed.append(review_dir)
        return removed
    finally:
        claim.close()


def _closed_pr_protected(
    store: RunStore | None, pr_number: int, *, merged: bool
) -> set[Path] | None:
    """The copies a finished PR keeps: none once merged, each deferral's when
    closed unmerged, and None (keep everything) when the ledger is unreadable
    or a deferral's copy is unknown."""
    if merged:
        return set()
    deferrals = _live_deferrals(store, pr_number)
    if deferrals is None or deferrals.unknown is not None:
        return None
    return {deferral.path.resolve(strict=False) for deferral in deferrals.known}


def _claim(review_dir: Path) -> IO[str]:
    """Take an exclusive, process-lifetime claim on *review_dir*.

    Two concurrent reviews of the same PR must not share one tree: the second
    one refuses (``checkout_busy``) rather than running an agent inside a
    checkout another agent is editing.
    """
    review_dir.parent.mkdir(parents=True, exist_ok=True)
    lock_path = review_dir.parent / f"{review_dir.name}.lock"
    handle = open(lock_path, "a+", encoding="utf-8")  # noqa: SIM115 — held for the run
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise ReviewCheckoutError(
            CHECKOUT_BUSY,
            f"review checkout {review_dir} is claimed by another reviewer",
        ) from exc
    handle.write(f"{os.getpid()}\n")
    handle.flush()
    return handle


def _repo_root() -> Path:
    """The repository the review checkout is cloned from.

    This is the operator's own checkout, which is exactly why the clone must
    exist: it is the thing being protected. Cloning FROM it is safe — an
    agent-writable source would already mean the boundary was lost.
    """
    result = run(["git", "rev-parse", "--show-toplevel"], check=False)
    root = result.stdout.strip() if result.returncode == 0 else ""
    if not root:
        raise ReviewCheckoutError(
            CHECKOUT_UNUSABLE,
            "cannot locate the repository root to clone the review checkout from",
        )
    return Path(root)


def _has_ref(review_dir: Path, ref: str) -> bool:
    """True when *ref* resolves inside the review clone."""
    return (
        run(
            ["git", "-C", str(review_dir), "rev-parse", "--verify", "--quiet", ref],
            check=False,
        ).returncode
        == 0
    )


def _fetch_origin_only_branch(review_dir: Path, branch_name: str) -> None:
    """Materialize a PR branch that exists ONLY on origin (#1655).

    The narrow clone deliberately carries no source branch refs. A PR branch
    therefore reaches the review clone under no name until this helper fetches
    it. That is also the ordinary shape of reviewing somebody else's PR. The
    old checkout failed with "pathspec did not match" before the review
    launched.

    The shared fetch helper authenticates through the operator repository.
    It transfers the branch into the clone's origin tracking ref.
    It creates no local branch in the operator repository.
    Skip the fetch when either clone ref already exists.
    """
    if _has_ref(review_dir, f"refs/heads/{branch_name}") or _has_ref(
        review_dir, f"refs/remotes/origin/{branch_name}"
    ):
        return
    try:
        fetch_ref(review_dir, branch_name, repo_root=_repo_root())
    except Exception as exc:
        raise ReviewCheckoutError(
            CHECKOUT_UNUSABLE,
            f"cannot fetch PR branch {branch_name!r} into {review_dir}",
        ) from exc


def _repoint(
    review_dir: Path, *, branch_name: str, head_sha: str, detached: bool
) -> None:
    """Move an existing CLEAN review checkout onto the current PR head.

    Never resets or cleans: a checkout that cannot be fast-forwarded onto the
    head is refused (``checkout_unusable``) so no work is silently discarded.
    """
    if detached:
        result = run(
            ["git", "-C", str(review_dir), "checkout", "--detach", head_sha, "--quiet"],
            check=False,
        )
    else:
        _fetch_origin_only_branch(review_dir, branch_name)
        if _has_ref(review_dir, f"refs/heads/{branch_name}"):
            result = run(
                ["git", "-C", str(review_dir), "checkout", branch_name, "--quiet"],
                check=False,
            )
        else:
            result = run(
                [
                    "git",
                    "-C",
                    str(review_dir),
                    "checkout",
                    "-b",
                    branch_name,
                    f"origin/{branch_name}",
                    "--quiet",
                ],
                check=False,
            )
        if result.returncode == 0:
            result = run(
                ["git", "-C", str(review_dir), "merge", "--ff-only", head_sha],
                check=False,
            )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[:200]
        raise ReviewCheckoutError(
            CHECKOUT_UNUSABLE,
            f"cannot place {review_dir} on {head_sha[:12]}: {detail}",
        )
    # The branch is fetched again here, after head_sha was resolved: if the PR
    # moved in between, the checkout lands on the newer commit and the
    # fast-forward to head_sha is a no-op. The reviewer must run exactly the
    # head kew records, so anything else is refused.
    actual = run(
        ["git", "-C", str(review_dir), "rev-parse", "HEAD"], check=False
    ).stdout.strip()
    if actual != head_sha:
        raise ReviewCheckoutError(
            HEAD_MISMATCH,
            f"{review_dir} is at {actual[:12] or 'no commit'} after placing it on "
            f"{head_sha[:12]}: the PR branch moved while the checkout was built",
        )


def _materialize(
    review_dir: Path,
    *,
    branch_name: str,
    head_sha: str,
    detached: bool,
    base_branch: str = "",
) -> None:
    """Put *review_dir* on *head_sha*, creating it when it does not exist."""
    if review_dir.exists() and is_linked_worktree(review_dir):
        # Upgrade path: a review checkout created before the clone conversion
        # still shares the operator's `.git`. The dirty check below runs FIRST
        # on every other path, but a legacy worktree must be replaced rather
        # than repointed — reusing it keeps the exposure alive for exactly the
        # operators who already ran a review.
        if has_uncommitted_changes(review_dir):
            raise ReviewCheckoutError(
                CHECKOUT_DIRTY,
                f"review checkout {review_dir} has uncommitted changes and "
                "shares the operator's .git — refusing to overwrite it",
            )
        _warn(
            f"Replacing legacy linked review checkout at {review_dir} with an "
            "isolated clone (it shared the operator's .git)."
        )
        discard_checkout(review_dir)

    if review_dir.exists():
        if has_uncommitted_changes(review_dir):
            raise ReviewCheckoutError(
                CHECKOUT_DIRTY,
                f"review checkout {review_dir} has uncommitted changes — "
                f"refusing to overwrite it",
            )
        # A reused checkout needs the fetch as much as a fresh one. As a linked
        # worktree it shared the operator's object store, so a fetch over there
        # made new commits visible here for free. A standalone clone has its
        # own store — which is the entire point — so the head of a PR that has
        # been pushed since the last round must be fetched explicitly.
        _fetch_head_or_refuse(review_dir, head_sha)
        _repoint(
            review_dir, branch_name=branch_name, head_sha=head_sha, detached=detached
        )
        if base_branch:
            fetch_ref(review_dir, base_branch, repo_root=_repo_root())
        return

    review_dir.parent.mkdir(parents=True, exist_ok=True)
    # A CLONE, not `git worktree add`. The agent that runs here must not be able
    # to reach the operator's `.git` — see the module docstring. This is the
    # same clone seam sandboxed dispatch and the graph reviewer already use, so
    # there is one definition of "a checkout an agent may be handed".
    try:
        clone_local_checkout(_repo_root(), review_dir, head_sha)
        _fetch_head_or_refuse(review_dir, head_sha)
        _repoint(
            review_dir, branch_name=branch_name, head_sha=head_sha, detached=detached
        )
        if base_branch:
            fetch_ref(review_dir, base_branch, repo_root=_repo_root())
    except BaseException:
        # A half-built clone must not be left behind: the next round would take
        # the reuse path into it and repoint a checkout that was never finished.
        shutil.rmtree(review_dir, ignore_errors=True)
        raise


def _fetch_head_or_refuse(review_dir: Path, head_sha: str) -> None:
    """Ensure *head_sha*'s objects are in *review_dir*, or refuse (#1004)."""
    try:
        fetch_missing_head(review_dir, head_sha, repo_root=_repo_root())
    except Exception as exc:
        raise ReviewCheckoutError(
            CHECKOUT_UNUSABLE,
            f"cannot fetch PR head {head_sha[:12]} into {review_dir}: {exc}",
        ) from exc


def materialize_review_checkout(
    *,
    pr_number: int,
    branch_name: str,
    config: DispatchConfig,
    base_branch: str = "",
    expected_head_sha: str | None = None,
    store: RunStore | None = None,
) -> ReviewCheckout:
    """Materialize an exclusively-claimed review checkout for *pr_number*.

    Args:
        pr_number: The PR under review (names the checkout directory).
        branch_name: The PR head branch.
        config: Dispatch configuration (worktree base + resolve strategy).
        base_branch: When set, the base is integrated with the configured
            resolve strategy and any residual conflict is returned.
        expected_head_sha: The PR head the caller intends to review (GitHub's
            ``headRefOid``). A mismatch against the fetched ``origin/<branch>``
            means the head moved under us — refused (``head_mismatch``) rather
            than reviewing, and later publishing to, a different commit.
        store: The run ledger the preserved-copy prune reads for deferred
            publications (#3186). Without one, no copy is pruned.

    Returns:
        A :class:`ReviewCheckout`. The caller MUST call ``release()`` on every
        terminal path; process exit releases the claim as a backstop.

    Raises:
        ReviewCheckoutError: Any classified pre-launch refusal.
    """
    review_dir = review_checkout_path(config, pr_number)
    # The claim comes first: a second reviewer must refuse (``checkout_busy``)
    # before anything moves or reads the first reviewer's live tree (review
    # round 11 of #1440).
    claim = _claim(review_dir)
    try:
        _quarantine_review_dir(review_dir, config, pr_number=pr_number, store=store)
        _fetch(branch_name, base_branch)
        head_sha = _resolve_head_sha(branch_name)
        if expected_head_sha and expected_head_sha != head_sha:
            raise ReviewCheckoutError(
                HEAD_MISMATCH,
                f"PR #{pr_number} head is {expected_head_sha[:12]} but "
                f"origin/{branch_name} resolves to {head_sha[:12]}",
            )

        occupant = worktree_for_branch(branch_name)
        detached = occupant is not None and occupant.resolve() != review_dir.resolve()
        if detached:
            _warn(
                f"Branch '{branch_name}' is checked out at {occupant} — "
                f"reviewing detached at {head_sha[:12]} instead of sharing it."
            )
        else:
            _log(f"Review checkout for PR #{pr_number} at {review_dir}")

        _materialize(
            review_dir,
            branch_name=branch_name,
            head_sha=head_sha,
            base_branch=base_branch,
            detached=detached,
        )
        try:
            conflict = (
                integrate_base_branch(
                    worktree_dir=review_dir,
                    base_branch=base_branch,
                    strategy=config.resolve.strategy,
                )
                if base_branch
                else None
            )
        except BaseIntegrationError as exc:
            # A merge that could not run is a pre-launch refusal, not conflicts
            # for an agent to resolve (#1455). Carry git's own diagnosis: the
            # first instance of this cost a CI round-trip because the symptom
            # named the wrong subsystem entirely.
            raise ReviewCheckoutError(BASE_INTEGRATION_FAILED, str(exc)) from exc
    except BaseException:
        claim.close()
        raise

    return ReviewCheckout(
        path=review_dir,
        head_sha=head_sha,
        branch=branch_name,
        detached=detached,
        impl_worktree=occupant if detached else None,
        conflict=conflict,
        integrated_base_sha=_integrated_base_sha(review_dir, base_branch),
        integrated_tree="" if conflict is not None else _head_tree(review_dir),
        claim=claim,
    )
