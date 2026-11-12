"""Operator attestation of a head kew did not push itself (#2124).

Canonical posture has no approve-and-publish path out of
``publication_review_required``: an owner can approve protected output, but the
operator then publishes the files by hand, and the head that reaches the remote
carries no ``publication_intent`` because the host publisher never ran. The
head-binding lookup accepts only the host's own intent as a claim, so every
governed remediation leg on that branch refuses the head as unbound — forever,
because nothing in the canonical flow can ever record the missing claim.

These verbs record exactly that missing fact: *this* named human says *this*
remote head is the published output of *this* run. The claim is a chained
``publication_head_attested`` audit event, which
:func:`kew.publication_credentials.publication_producer_for_head` accepts
alongside the host's intent, under the same one-claim-per-head rule and the
same requirement that the bound run has a row on the branch.

Two callers record one:

* :func:`kew.service.verbs.finalize_publication_run`, for the head the operator
  published for the run being finalized — that run is the producer.
* :func:`attest_published_head`, for an operator's later commits on an
  already-bound branch. There is no finalized run to name, so the claim binds
  to whatever producer the new head's *first-parent path* resolves to: the
  operator may extend a bound history, never invent a producer for one. That
  covers the hand-resolved base merge too (#2177) — two parents, the bound
  head first and a commit on the fetched base second — and a bounded chain of
  such commits, because folding them into one needs a force push the operator
  hook blocks (#2199), but nothing wider.

Both require the sha to be the branch's current remote head, read from the
operator origin. A sha that is not the remote head is not what a remediation
would check out, and binding it would bind the wrong thing.

Both also require the head to be unclaimed, or claimed by the same run
already. The one-claim-per-head rule is enforced where claims are read, and a
chained event cannot be withdrawn, so a writer that appended blindly would turn
a working head binding into a permanent refusal — see
:func:`head_unclaimable_for_run`.

Asking first is not enough on its own: a bare check-then-append lets two
writers naming one head (two stranded runs finalized at once, or ``kew
finalize`` racing ``kew publication attest``) both pass the check and both
append, producing exactly the permanent ambiguity the check exists to prevent.
So the claim carries the head itself as its durable identity — one claim per
head is the rule, so the head is the key — and the append runs through the
store's dedup path, where the existence check and the insert share one
``BEGIN IMMEDIATE`` transaction behind a UNIQUE index. The loser of that race
appends nothing and is told whose claim stands; see
:func:`record_head_attestation`.

The origin reads themselves are injected, never imported here: this module is
policy over recorded evidence, and ``kew.service`` must be able to reach it
without pulling the host's whole git surface in behind it.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any, NamedTuple

from kew.audit import Actor, emit, resolve_actor
from kew.publication_canonical import PublicationHeld
from kew.publication_credentials import (
    HEAD_ATTESTATION_EVENT,
    CredentialPublicationRefused,
    recorded_head_claim_scopes,
    recorded_head_claims,
    recorded_head_producer_with_credentials,
)

if TYPE_CHECKING:
    from kew.run_store import RunStore

_SHA_RE = re.compile(r"[0-9a-f]{40}")


class HeadAttestationRefused(ValueError):
    """The head claim was not recorded, and nothing was bound."""


def _require_sha(value: str, *, what: str) -> str:
    if not isinstance(value, str) or _SHA_RE.fullmatch(value) is None:
        raise HeadAttestationRefused(f"{what} must be a full 40-character commit sha")
    return value


def _head_claim_identity(*, branch: str, commit_sha: str) -> str:
    """The durable identity of a head claim: the head, not the claimant.

    One claim per head is the rule the lookup enforces, so the head is what
    two writers must not both append under. Keying the dedup column on the
    claimant instead would let each of them write its own row and leave the
    branch ambiguous for good (#2124).
    """
    return f"publication-head-claim:v1:{branch}:{commit_sha}"


def record_head_attestation(
    store: RunStore | None,
    *,
    run_id: str,
    item_id: str | None,
    branch: str,
    commit_sha: str,
    parent_shas: Sequence[str],
    chain_shas: Sequence[str] = (),
    bound_parent_sha: str | None = None,
    actor: Actor | None = None,
    source: str = "cli",
) -> None:
    """Claim one head for one run, atomically, then read the claim back.

    ``emit`` is best-effort by design; a head claim is not. A dropped append
    would leave the operator believing the branch was bound while the next
    remediation still refused it, so the claim is read back under the same
    scope key the lookup pages and a missing row is reported as a failure.

    The read-back is also what makes the claim safe against a concurrent
    writer. ``dedup_key`` is the head's own identity, and the store resolves it
    under ``BEGIN IMMEDIATE`` against a UNIQUE index, so of two writers naming
    one head exactly one appends — the other's ``emit`` collapses onto the row
    that already exists. Reading the claim back therefore answers both
    questions at once: a run that finds another run's claim on the head wrote
    nothing and is refused, rather than being told it bound a head it does not
    own. The callers' ``head_unclaimable_for_run`` check stays where it is —
    it reports the refusal before anything is emitted, and it sees the host
    publisher's intent claims too — but this is the check that cannot be
    raced.

    Scope mirrors the host publisher's: item scope keys on the tracker item,
    branch scope on the branch for a run that names no item.
    """
    actor = actor or resolve_actor()
    if actor.type != "human":
        raise HeadAttestationRefused(
            f"a head attestation names a human actor, not {actor.type!r}"
        )
    if not branch:
        raise HeadAttestationRefused("a head attestation names a branch")
    _require_sha(commit_sha, what="the attested head")
    scope = "item" if item_id else "branch"
    scope_key = item_id or branch
    payload: dict[str, Any] = {
        "branch": branch,
        "commit_sha": commit_sha,
        # The bound parent stays under its own key; the full ordered list is
        # what makes an operator's two-parent base merge legible (#2177).
        "parent_sha": parent_shas[0] if parent_shas else None,
        "parent_shas": list(parent_shas),
        # Every commit this one claim binds through, head first, and the
        # already-bound commit the chain reached (#2199). A head bound
        # directly binds through itself alone and reached nothing.
        "chain_shas": list(chain_shas) or [commit_sha],
        "bound_parent_sha": bound_parent_sha,
        "run_id": run_id,
        "scope": scope,
        "source": source,
    }
    emit(
        store,
        HEAD_ATTESTATION_EVENT,
        actor=actor,
        run_id=run_id,
        item_id=scope_key,
        payload=payload,
        dedup_key=_head_claim_identity(branch=branch, commit_sha=commit_sha),
    )
    recorded = _recorded_claimants(
        store, item_id=scope_key, branch=branch, commit_sha=commit_sha
    )
    others = sorted(recorded - {run_id})
    if others:
        # The dedup append kept this writer out of the chain, so the claim
        # that was already there is still the only one on this head.
        raise HeadAttestationRefused(_already_claimed(branch, commit_sha, others))
    if run_id not in recorded:
        raise HeadAttestationRefused(
            f"the head attestation for {branch!r} ({commit_sha[:12]}) was not "
            "durably recorded; the branch remains unbound"
        )


def _already_claimed(branch: str, commit_sha: str, others: list[str]) -> str:
    """The one wording for "this head is not yours to claim"."""
    return (
        f"{commit_sha[:12]} on {branch!r} is already claimed by "
        f"{', '.join(others)}; a second claim would refuse the branch as "
        "ambiguous"
    )


def _recorded_claimants(
    store: RunStore | None,
    *,
    item_id: str,
    branch: str,
    commit_sha: str,
) -> set[str]:
    """The runs whose durably stored human claim names this exact head.

    This reads the one claim shape the append is serialized against — the
    attestation rows sharing the head's dedup identity — under the same scope
    key the write used. It answers both questions the writer has after it
    emits: whether its own claim landed, and whether it is the only one.
    """
    if store is None or getattr(store, "list_audit_events", None) is None:
        return set()
    events = store.list_audit_events(
        limit=None, event=HEAD_ATTESTATION_EVENT, item_id=item_id
    )
    return {
        str(event.run_id)
        for event in events
        if event.actor_type == "human"
        and isinstance(event.payload, dict)
        and event.payload.get("branch") == branch
        and event.payload.get("commit_sha") == commit_sha
    }


def attest_published_head(
    store: RunStore,
    *,
    branch: str,
    head_sha: str,
    item_id: str,
    base_branch: str,
    remote_head: Callable[[str], str],
    commit_parents: Callable[[str], Sequence[str]],
    commit_on_base: Callable[[str, str], bool],
    source: str = "cli",
) -> str:
    """Bind an operator-pushed head to the producer its first parents reach.

    Every git read is injected: this module is policy over recorded evidence,
    and the host layer owns the hardened operator-origin reads (``kew.cli``
    passes ``kew.worktree``'s). Failure of any of them is reported, never
    defaulted through: an unreadable remote, an absent object or a base branch
    that cannot be fetched means the head is unverified, and an unverified head
    is not bound.

    Two commit shapes bind. A single-parent commit binds through that parent —
    the operator's later commits on an already-bound branch (#2067). A
    two-parent merge binds through its FIRST parent, and only when its second
    parent is on the fetched base branch: that is the shape an operator leaves
    behind when a base conflict the driver could not resolve was merged by hand
    (#2177). Everything else — an octopus merge, a merge of something that is
    not the base, a merge made the other way round so the bound head is the
    second parent — is refused, by name.

    The bound commit does not have to be the head's own parent. When the
    operator left a chain of such commits — main merged into the READY head,
    then a test fix on top, which is two commits because folding them needs a
    force push the operator hook blocks (#2199) — the head's first-parent path
    is walked until it reaches one, up to ``_MAX_ATTEST_CHAIN`` commits. Every
    commit on that path is checked with the same two shapes, and one failing
    commit refuses the whole chain, naming it.

    Only a commit kew's recorded evidence binds — a normalized publication job
    or a head claim for that exact commit — ends the walk. The branch-wide
    newest-run guess the remediation lookup falls back to is not evidence that
    a run produced the commit, so it never supplies the producer (#2635).

    A head some other run already claims is refused rather than claimed again:
    the second claim would refuse the branch as ambiguous for good, so the
    command reports the existing claim instead of destroying it.

    Returns the run id the head is now bound to.
    """
    head_sha = _require_sha(head_sha, what="the attested head")
    if not branch:
        raise HeadAttestationRefused("a head attestation names a branch")
    if not item_id:
        raise HeadAttestationRefused("a head attestation names a tracker item")
    if not base_branch:
        raise HeadAttestationRefused("a head attestation names a base branch")
    _require_scope_the_lookup_reads(store, branch=branch, item_id=item_id)
    observed = _observed_remote_head(remote_head, branch)
    if observed != head_sha:
        raise HeadAttestationRefused(
            f"{head_sha[:12]} is not the remote head of {branch!r} "
            f"({observed[:12]} is); attest the head a remediation would check out"
        )
    chain = _bindable_chain(
        store,
        branch=branch,
        head_sha=head_sha,
        base_branch=base_branch,
        commit_parents=commit_parents,
        commit_on_base=commit_on_base,
    )
    unclaimable = head_unclaimable_for_run(
        store,
        branch=branch,
        head_sha=head_sha,
        run_id=chain.producer,
        item_id=item_id,
    )
    if unclaimable is not None:
        raise HeadAttestationRefused(unclaimable)
    record_head_attestation(
        store,
        run_id=chain.producer,
        item_id=item_id,
        branch=branch,
        commit_sha=head_sha,
        parent_shas=chain.head_parents,
        chain_shas=chain.shas,
        bound_parent_sha=chain.bound_parent,
        source=source,
    )
    return chain.producer


def _observed_remote_head(remote_head: Callable[[str], str], branch: str) -> str:
    try:
        observed = remote_head(branch)
    except Exception as exc:
        # Every read failure is reported; an unverified head is never bound.
        raise HeadAttestationRefused(
            f"the remote head of {branch!r} could not be read, so no head can "
            f"be attested: {exc}"
        ) from None
    return _require_sha(observed, what="the observed remote head")


_MAX_ATTEST_CHAIN = 10
"""How far the first-parent walk follows a chain of operator commits (#2199).

The chain this exists for is two commits long — a base merge and a fix on top.
The cap is what keeps a branch with nothing bound on it from walking back
through the base branch's history instead of reporting that.
"""

_BINDABLE_SHAPES = (
    "only a single-parent commit, or a two-parent merge of the base branch "
    "into the bound head, can be attested"
)


def _observed_parents(
    commit_parents: Callable[[str], Sequence[str]], head_sha: str
) -> tuple[str, ...]:
    """The head's parents, refused by count when no bindable shape fits."""
    try:
        parents = tuple(commit_parents(head_sha))
    except Exception as exc:
        # Every read failure is reported; an unverified head is never bound.
        raise HeadAttestationRefused(
            f"the parents of {head_sha[:12]} could not be read, so it cannot "
            f"be bound through them: {exc}"
        ) from None
    for parent in parents:
        _require_sha(parent, what="the attested head's parent")
    if not 1 <= len(parents) <= 2:
        raise HeadAttestationRefused(
            f"{head_sha[:12]} has {len(parents)} parents; {_BINDABLE_SHAPES}"
        )
    return parents


def _require_bindable_shape(
    commit_on_base: Callable[[str, str], bool],
    *,
    commit_sha: str,
    parents: tuple[str, ...],
    base_branch: str,
    unbound: str,
) -> None:
    """Refuse a commit on the path the chain cannot legitimately pass through.

    A single-parent commit passes. A two-parent commit is the operator's
    hand-resolved base merge only when its second parent is on the freshly
    fetched base (#2177), and the refusal names the commit that failed, which
    is not always the head once a chain is walked (#2199).

    *unbound* is why the first parent binds to no producer, or empty when it
    does. A merge that fails both tests at once is the shape an operator who
    merged the other way round leaves behind, so both facts are reported: the
    chain stops here, and fixing the merge alone would not restart it.
    """
    if len(parents) < 2:
        return
    try:
        on_base = commit_on_base(parents[1], base_branch)
    except Exception as exc:
        # Every read failure is reported; an unverified head is never bound.
        raise HeadAttestationRefused(
            f"whether the second parent {parents[1][:12]} of {commit_sha[:12]} "
            f"is on {base_branch!r} could not be read, so the head is not "
            f"bound: {exc}"
        ) from None
    if on_base:
        return
    off_base = (
        f"its second parent {parents[1][:12]} is not reachable from the "
        f"fetched {base_branch!r}"
    )
    if unbound:
        raise HeadAttestationRefused(
            f"the first parent {parents[0][:12]} of {commit_sha[:12]} is not "
            f"bound to a producer, and {off_base}, so the chain stops there; "
            f"{_BINDABLE_SHAPES}"
        )
    raise HeadAttestationRefused(
        f"the second parent {parents[1][:12]} of the merge {commit_sha[:12]} "
        f"is not reachable from the fetched {base_branch!r}; {_BINDABLE_SHAPES}"
    )


def _require_scope_the_lookup_reads(
    store: RunStore, *, branch: str, item_id: str
) -> None:
    """Refuse an item the head lookup would never page this branch's claims under.

    The claim is keyed on the tracker item, and the lookup derives that key
    from the branch's own runs. A mistyped ``--item`` would write a durable
    event nobody ever reads — the command reporting success while the branch
    stayed refused, which is exactly the silence this issue is about.
    """
    scoped = _branch_item_the_lookup_reads(store, branch=branch)
    if scoped is None and _owner_beside_its_prs(store, branch=branch) == item_id:
        # The lookup reads a mixed branch under every item its runs name, so a
        # claim under the owning item is read back (#3193).
        return
    if scoped is None:
        raise HeadAttestationRefused(
            f"the runs on {branch!r} name more than one tracker item, so no one "
            "item owns the branch to attest under; the host publisher's own "
            "claim for the head is what binds it"
        )
    if scoped != item_id:
        raise HeadAttestationRefused(
            f"{branch!r} belongs to item {scoped or '(none)'}, not {item_id!r}; "
            "a claim under another item would never be read"
        )


def _owner_beside_its_prs(store: RunStore, *, branch: str) -> str | None:
    """The item owning a branch whose other runs only served its PR, or None.

    A run store that cannot be read here is the same refusal as in
    :func:`_branch_item_the_lookup_reads`: the claim cannot be read back, so
    the refusal says why instead of reading as a mixed branch.
    """
    from kew.publication_credentials import branch_owner_beside_its_prs

    try:
        return branch_owner_beside_its_prs(store, branch=branch)
    except (CredentialPublicationRefused, PublicationHeld) as exc:
        raise HeadAttestationRefused(
            f"the runs on {branch!r} could not be read for the item owning the "
            f"branch beside its PR runs, so no claim on it can be read back: {exc}"
        ) from None


def _branch_item_the_lookup_reads(store: RunStore, *, branch: str) -> str | None:
    """The tracker item the head lookup pages this branch's claims under.

    Every refusal the evidence layer can raise here — a branch with more runs
    than the producer guard's window, an unreadable run store — means the same
    thing for this command: the claim cannot be read back, so it is not
    recorded. Reporting it as a refusal to attest keeps the command inside its
    documented exit codes instead of surfacing a publication error as a
    traceback.
    """
    from kew.publication_credentials import _single_branch_item

    try:
        return _single_branch_item(store, branch=branch)
    except (CredentialPublicationRefused, PublicationHeld) as exc:
        raise HeadAttestationRefused(
            f"the runs on {branch!r} could not be read as one tracker item, so "
            f"no claim on it can be read back: {exc}"
        ) from None


def head_unclaimable_for_run(
    store: RunStore,
    *,
    branch: str,
    head_sha: str,
    run_id: str,
    item_id: str | None,
) -> str | None:
    """Why *run_id* may not claim this head, or ``None`` when it may.

    The one-claim-per-head rule is enforced where claims are read: two claims
    on one head refuse as ``publication_lineage_ambiguous``. A writer that
    appends blindly therefore breaks bindings it did not make — finalizing a
    second run stranded on the same branch would add a second claim for the one
    remote head and leave every later remediation on that branch refused, with
    no way to withdraw a chained event (#2124). So both writers ask first.

    Asking is the readable refusal, not the enforcement: between this answer
    and the append another writer may claim the same head, so the append
    itself is serialized on the head's identity and this same question is put
    again once it has run (:func:`record_head_attestation`).

    A claim this same run already recorded under the scope this claim would be
    written under is no obstacle: re-attesting the same head for the same
    producer and item is idempotent. Everything else is: a head another run
    claims, the same run's claim under another scope (a mixed branch's lookup
    counts (run, scope) pairs, so a second scope is a second claim, #3193), a
    scope the lookup would never page this claim under, and evidence that
    cannot be read at all — an unverifiable head is not claimable, exactly as
    an unverifiable head is not bound.
    """
    try:
        _require_sha(head_sha, what="the head to claim")
        _require_scope_the_lookup_reads(store, branch=branch, item_id=item_id or "")
        claims = recorded_head_claim_scopes(store, branch=branch, head_sha=head_sha)
    except HeadAttestationRefused as exc:
        return str(exc)
    except (CredentialPublicationRefused, PublicationHeld) as exc:
        return (
            f"the claims already recorded for {branch!r} could not be read, so "
            f"{head_sha[:12]} cannot be claimed for run {run_id}: {exc}"
        )
    return _claim_obstacle(
        claims, branch=branch, head_sha=head_sha, run_id=run_id, item_id=item_id
    )


def _claim_obstacle(
    claims: set[tuple[Any, str]],
    *,
    branch: str,
    head_sha: str,
    run_id: str,
    item_id: str | None,
) -> str | None:
    """Why the recorded (run, scope) *claims* leave no room for this one.

    The claim would be written under ``item_id`` (or the branch, for a run
    naming no item), so only this run's claim under that same key is no
    obstacle.
    """
    others = sorted({str(claim) for claim, _scope_key in claims if claim != run_id})
    if others:
        return _already_claimed(branch, head_sha, others)
    scope_key = item_id or branch
    elsewhere = sorted({key for _claim, key in claims if key != scope_key})
    if elsewhere:
        return (
            f"{head_sha[:12]} on {branch!r} is already claimed by {run_id} under "
            f"{', '.join(map(repr, elsewhere))}; a second claim under "
            f"{scope_key!r} would refuse the branch as ambiguous"
        )
    return None


class _BoundChain(NamedTuple):
    """The first-parent path from an attested head to an already-bound commit."""

    shas: tuple[str, ...]
    """The operator commits the claim binds through, head first."""

    head_parents: tuple[str, ...]
    """The attested head's own parents, as the claim records them."""

    bound_parent: str
    """The already-bound commit the path reached."""

    producer: str
    """The run that bound commit binds to."""


class _ChainStep(NamedTuple):
    """One commit on the first-parent path, and what its first parent binds to."""

    sha: str
    """The commit itself."""

    parents: tuple[str, ...]
    """Its observed parents, first parent first."""

    unbound: str
    """Why its first parent binds to no producer; empty when one is bound."""


def _bindable_chain(
    store: RunStore,
    *,
    branch: str,
    head_sha: str,
    base_branch: str,
    commit_parents: Callable[[str], Sequence[str]],
    commit_on_base: Callable[[str, str], bool],
) -> _BoundChain:
    """Walk the head's first-parent path to the first commit already bound.

    The first parent is the bound head the operator extended — for a merge
    that is what "merged the base INTO my branch" means, so a merge made the
    other way round stops the walk, and the refusal says which parent it was
    (#2177). What is new is that the walk does not stop at the head's own
    parent: an operator who cannot force-push leaves a chain of commits, and
    every one of them is checked with the same two shapes before the claim
    binds through it (#2199).

    Only the head's own shape is checked before the walk moves, though. That
    check costs a fetch of the base from the operator's origin for every merge
    it covers, and on a branch with nothing bound the walk ends at the cap
    whatever the shapes below the head are — so paying it per commit there
    would buy ten origin reads and change no answer. The rest of the path is
    checked once the walk has found the bound commit, head first, so the
    commit a refusal names is the one an eager check would have named too.

    The walk is capped. Without a cap a head whose branch has no bound commit
    at all would read back through the base branch's whole history, one
    evidence lookup per commit, and report the operator's real mistake —
    nothing on this branch is bound — only at the root.
    """
    steps: list[_ChainStep] = []
    sha = head_sha
    while len(steps) < _MAX_ATTEST_CHAIN:
        parents = _observed_parents(commit_parents, sha)
        producer, unbound = _bound_producer(store, branch=branch, commit_sha=parents[0])
        step = _ChainStep(sha, parents, unbound)
        if not steps:
            _require_bindable_shape(
                commit_on_base,
                commit_sha=step.sha,
                parents=step.parents,
                base_branch=base_branch,
                unbound=step.unbound,
            )
        steps.append(step)
        if producer:
            _require_bindable_path(
                commit_on_base, steps=steps[1:], base_branch=base_branch
            )
            return _BoundChain(
                tuple(walked.sha for walked in steps),
                steps[0].parents,
                parents[0],
                producer,
            )
        sha = parents[0]
    raise HeadAttestationRefused(_nothing_bound_within_the_cap(head_sha, steps[0]))


def _require_bindable_path(
    commit_on_base: Callable[[str, str], bool],
    *,
    steps: Sequence[_ChainStep],
    base_branch: str,
) -> None:
    """Check the shapes of the path below the head, in the walk's own order.

    These are the commits the walk passed through to reach the bound one. They
    are checked only once it has been reached — a path that reaches nothing
    binds nothing, and the check is an origin fetch per merge — but they are
    checked head first, so one failing commit refuses the whole chain and names
    itself exactly as it would have when the walk stepped through it.
    """
    for step in steps:
        _require_bindable_shape(
            commit_on_base,
            commit_sha=step.sha,
            parents=step.parents,
            base_branch=base_branch,
            unbound=step.unbound,
        )


def _nothing_bound_within_the_cap(head_sha: str, head_step: _ChainStep) -> str:
    """The refusal for a path the cap ended, reported against the head.

    The operator's mistake is at the head they named: it binds through a parent
    that is bound to nothing, and the cap is only why the walk stopped looking
    for one further back. Naming whichever ancestor the walk happened to end on
    would report the base branch's history at them, which is not theirs to fix.
    """
    return (
        f"the first parent {head_step.parents[0][:12]} of {head_sha[:12]} is "
        "not bound to a producer, and the first-parent path from it was not "
        f"followed past {_MAX_ATTEST_CHAIN} commits; an attestation extends a "
        "bound history, it never invents a producer for one: "
        f"{head_step.unbound}"
    )


def _bound_producer(
    store: RunStore, *, branch: str, commit_sha: str
) -> tuple[str, str]:
    """The producer this commit already binds to, or why it binds to none.

    Exactly one of the two strings is filled. Only recorded evidence binds: a
    normalized publication job or a head claim naming this exact commit. The
    branch-wide newest-run heuristic is never consulted, because it names a
    run for every commit on a legacy branch, including commits that run never
    produced, and an operator head attested through one would bind to an
    unrelated run (#2635). An unbound commit is the ordinary case on a chain,
    so it is carried out rather than raised and the refusal at the end of the
    walk reports it. A commit the evidence itself claims, but which does not
    bind, is not that case — see :func:`_require_steppable`.
    """
    try:
        producer = recorded_head_producer_with_credentials(
            store, branch=branch, head_sha=commit_sha
        )
    except (CredentialPublicationRefused, PublicationHeld) as exc:
        _require_steppable(store, branch=branch, commit_sha=commit_sha, refusal=exc)
        return "", str(exc)
    if not producer:
        return "", "it names no producer run"
    return producer, ""


def _require_steppable(
    store: RunStore,
    *,
    branch: str,
    commit_sha: str,
    refusal: Exception,
) -> None:
    """Refuse a walk that would step over a claimed commit's own refusal.

    The walk continues past a commit the lookup refuses only when no claim
    names it: then the refusal is not about that commit's own producer. A
    commit some claim *does* name is the opposite case — the refusal is that
    claim's own, a producer whose credentials the lookup will not issue or two
    claims holding one commit —
    and stepping over it would bind the new head to an older producer, which is
    exactly the binding refused one commit below. Attesting the head directly
    is refused outright there, and extending it is no weaker.

    Evidence that cannot be read is not evidence of an unclaimed commit
    either: an unverifiable commit is not one the walk may pass through.
    """
    try:
        claimants = _claimants_of(store, branch=branch, commit_sha=commit_sha)
    except Exception as exc:
        raise HeadAttestationRefused(
            f"whether {commit_sha[:12]} on {branch!r} is itself claimed could "
            "not be read, so the first-parent path is not walked past it: "
            f"{exc}"
        ) from None
    if not claimants:
        return
    raise HeadAttestationRefused(
        f"{commit_sha[:12]} on {branch!r} is claimed by {', '.join(claimants)}, "
        f"and that claim does not bind: {refusal}; the path is not walked past "
        "a claimed commit to bind an older producer"
    )


def _claimants_of(store: RunStore, *, branch: str, commit_sha: str) -> list[str]:
    """Every run whose recorded evidence claims this exact commit as a head.

    Both shapes the lookup reads as a claim on one head: a normalized
    publication job for it (kew's own push), and a recorded head claim (the
    host publisher's intent, or an operator's attestation).
    """
    claims = {
        str(run_id)
        for run_id in recorded_head_claims(store, branch=branch, head_sha=commit_sha)
    }
    jobs = store.normalized_jobs_for_head(branch=branch, head_sha=commit_sha, limit=2)
    return sorted(claims | {str(job.run_id) for job in jobs})
