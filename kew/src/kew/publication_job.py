"""I08 vocabulary: one normalized lifecycle for one publication.

A publication is four intentions that have to survive a crash between any two
of them — commit the sealed tree, push the branch, open the pull request,
reconcile what the remote actually shows. The legacy encoding recorded only the
last of those as ``awaiting_publication``, which cannot answer the question a
restarted host actually has: *did the push land?*

So this module names one lifecycle and nothing else:

``pending`` → ``committed`` → ``pushed`` → ``opened`` → ``reconciled``

with three ways out of it. ``ambiguous`` is entered whenever the remote could
not be observed — it remembers the state it interrupted in ``resume_state``, so
an "absent" observation returns the job to what it last *knew* rather than
inventing progress it never proved. ``abandoned`` is a decision. ``superseded``
is what happens to a job when a successor SHA for the same lineage takes over.

Movement is monotonic: a job advances to a state of strictly higher rank, or to
one of the three exits, or nowhere. A job never walks backwards, because the
only evidence that could ask it to — a remote that stopped showing a branch it
once showed — is a reason for doubt, not for rewriting history.

What each state is *made of* lives here too, as pure functions over a persisted
row: :func:`commit_intent_for` names the commit (I09A), and
:func:`push_intent_for` and :func:`pull_request_intent_for` name the push and
the pull request against what the forge was just observed to hold (I09B). None
of the three reads a clock, an environment or a repository, so a restarted host
recomputes the same answer rather than a second one beside it.

**Nothing here publishes.** :func:`publication_jobs_active` is the single gate
between persisted intent and real effect, and it is off in shipped source: the
selector in :mod:`kew.run_store_publication_jobs` is the only caller that
consults it, and nothing in shipped source calls the selector.

I14 adds the last thing a publication needs before it may leave the host —
the *capacity* a person has to look at it. :class:`RemoteReviewObservation`
and :func:`require_countable_observation` at the foot of this module are the
vocabulary that keeps that count honest across a boundary this host does not
own; the transaction that uses them lives in the store.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

PENDING = "pending"
COMMITTED = "committed"
PUSHED = "pushed"
OPENED = "opened"
RECONCILED = "reconciled"
AMBIGUOUS = "ambiguous"
ABANDONED = "abandoned"
SUPERSEDED = "superseded"

#: Progress states in the order a publication passes through them.
LIFECYCLE = (PENDING, COMMITTED, PUSHED, OPENED, RECONCILED)

#: States nothing may leave. ``reconciled`` is the only happy one.
TERMINAL_STATES = frozenset({RECONCILED, ABANDONED, SUPERSEDED})

#: Exits reachable from any live state, regardless of rank.
EXIT_STATES = frozenset({AMBIGUOUS, ABANDONED, SUPERSEDED})

_RANK = {state: index for index, state in enumerate(LIFECYCLE)}

#: The steps a worker records before the effect each one describes. The
#: strings are deliberately the ones :data:`kew.publication.PublicationStep`
#: already names, so a failure reported by the legacy publisher and an attempt
#: recorded by this lifecycle cannot drift into two vocabularies for one step.
STEP_COMMIT = "commit"
STEP_PUSH = "push"
STEP_PR_CREATE = "pr_create"

REMOTE_ABSENT = "absent"
REMOTE_BRANCH = "branch"
REMOTE_PULL_REQUEST = "pull_request"
REMOTE_SETTLED = "settled"
REMOTE_UNKNOWN = "unknown"
#: The remote was read perfectly well and holds something this publication
#: cannot bind to. Evidence worth recording, movement it does not justify —
#: which is why it is not in :data:`_OBSERVED_STATE` below.
REMOTE_CONFLICT = "conflict"

#: How a forge reports a pull request. ``merged`` is a landed publication;
#: ``closed`` is somebody's decision, and reopening it would be another one.
PR_OPEN = "open"
PR_CLOSED = "closed"
PR_MERGED = "merged"

#: What each remote observation PROVES about the lifecycle. ``absent`` and
#: ``unknown`` prove nothing forward and are handled by the resume rules.
_OBSERVED_STATE = {
    REMOTE_BRANCH: PUSHED,
    REMOTE_PULL_REQUEST: OPENED,
    REMOTE_SETTLED: RECONCILED,
    REMOTE_UNKNOWN: AMBIGUOUS,
}

PUBLICATION_JOBS_ACTIVE = False
"""The E5 feature gate. E7/I14 owns flipping it, together with the capacity
transaction that makes a publication safe to start; until then every
effect-bearing entry point refuses rather than quietly doing nothing."""


class PublicationJobError(RuntimeError):
    """Base class for publication-job refusals."""


class PublicationJobValueError(PublicationJobError):
    """An intent field was not something a publication can be built from."""


class PublicationJobConflictError(PublicationJobError):
    """This lineage already has a live job, and two would race each other."""


class PublicationJobStateError(PublicationJobError):
    """The lifecycle does not allow the move that was asked for."""


class PublicationJobsInactiveError(PublicationJobError):
    """Something asked for publication capacity that does not exist yet."""


class PublicationRemoteConflictError(PublicationJobError):
    """The remote holds state this publication cannot bind to.

    Carries the observation that found it, so the caller can record what the
    forge showed before it holds. Holding is the whole point: the alternative
    to a conflict is a force-push or a second pull request, and both of those
    destroy somebody's work to make a lifecycle tidy.
    """

    def __init__(self, message: str, *, observation: RemoteObservation) -> None:
        super().__init__(message)
        self.observation = observation


def publication_jobs_active() -> bool:
    """True once E7 has installed publication capacity. False in shipped kew."""
    return PUBLICATION_JOBS_ACTIVE


def require_publication_jobs_active(effect: str) -> None:
    """Refuse *effect* while E5 is gated shut."""
    if not publication_jobs_active():
        raise PublicationJobsInactiveError(
            f"cannot {effect}: publication jobs stay inactive until E7 installs"
            " the publication-capacity transaction"
        )


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise PublicationJobValueError(message)


def _is_oid(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 40
        and all(char in "0123456789abcdef" for char in value)
    )


@dataclass(frozen=True)
class PublicationIntent:
    """Everything the host means to do, recorded before it does any of it."""

    source_kind: str
    source_id: str
    run_id: str
    attempt_number: int
    branch: str
    base_sha: str
    tree_sha: str
    receipt_digest: str
    message: str = ""

    @property
    def lineage_key(self) -> str:
        """The identity a successor SHA inherits: one source, one branch."""
        return f"{self.source_kind}:{self.source_id}:{self.branch}"

    @property
    def job_id(self) -> str:
        """Derived from the intent, so a retried creation converges on one row."""
        preimage = "\n".join(
            (
                self.source_kind,
                self.source_id,
                self.branch,
                self.run_id,
                str(self.attempt_number),
            )
        )
        return "pj-" + hashlib.sha256(preimage.encode("utf-8")).hexdigest()[:16]

    def validate(self) -> None:
        """Refuse an unbuildable intent BEFORE anything is written."""
        _require(bool(self.source_kind), "a publication job needs a source kind")
        _require(bool(self.source_id), "a publication job needs a source id")
        _require(bool(self.run_id), "a publication job needs a run id")
        _require(bool(self.branch), "a publication job needs a branch")
        _require(
            isinstance(self.attempt_number, int) and self.attempt_number >= 1,
            f"attempt {self.attempt_number!r} is not an attempt number",
        )
        _require(_is_oid(self.base_sha), f"base {self.base_sha!r} is not a commit id")
        _require(_is_oid(self.tree_sha), f"tree {self.tree_sha!r} is not a tree id")
        _require(bool(self.receipt_digest), "a publication job needs a receipt")


@dataclass(frozen=True)
class RemoteObservation:
    """What the forge showed when the host last looked — including nothing."""

    kind: str
    ref: str | None = None
    detail: str = ""


@dataclass(frozen=True)
class PublicationJob:
    """One ``publication_job`` row: the whole lifecycle of one publication."""

    job_id: str
    lineage_key: str
    source_kind: str
    source_id: str
    run_id: str
    attempt_number: int
    branch: str
    base_sha: str
    tree_sha: str
    receipt_digest: str
    message: str
    state: str
    resume_state: str | None
    head_sha: str | None
    remote_state: str | None
    remote_ref: str | None
    attempt_count: int
    last_step: str | None
    last_attempt_at: str | None
    detail: str | None
    predecessor_job_id: str | None
    superseded_by: str | None
    created_at: str
    updated_at: str

    @property
    def active(self) -> bool:
        """True while this job still owns the lineage's publication."""
        return self.state not in TERMINAL_STATES


@dataclass(frozen=True)
class LegacyPublicationView:
    """An ``awaiting_publication`` run, read as the lifecycle it implies.

    ``persisted`` is always False and exists to say so out loud: decoding a
    legacy row must never write a job on its behalf. Backfilling one would
    claim a sealed receipt, a tree and a base that no legacy run ever produced.
    """

    run_id: str
    attempt_number: int
    branch: str
    item_id: str | None
    state: str
    remote_ref: str | None
    detail: str
    persisted: bool = False


def decode_legacy_publication(
    *,
    status: str,
    run_id: str,
    attempt_number: int,
    branch: str,
    item_id: str | None = None,
    pr_url: str | None = None,
    error_category: str = "",
) -> LegacyPublicationView | None:
    """Read one legacy run row as a lifecycle state, or as nothing at all.

    Only ``awaiting_publication`` encoded publication intent, and it encoded it
    without saying which step failed. A row that named a pull request proves
    the open happened, so it reads as ``opened`` with reconciliation still
    outstanding. A row that named none proves nothing about the push, so it
    reads as ``ambiguous`` — the honest answer, and the one that stops a reader
    from treating a preserved artifact as either published or lost.
    """
    if status != "awaiting_publication":
        return None
    if pr_url:
        return LegacyPublicationView(
            run_id=run_id,
            attempt_number=attempt_number,
            branch=branch,
            item_id=item_id,
            state=OPENED,
            remote_ref=pr_url,
            detail="legacy awaiting_publication row that named a pull request",
        )
    reason = error_category or "no recorded step"
    return LegacyPublicationView(
        run_id=run_id,
        attempt_number=attempt_number,
        branch=branch,
        item_id=item_id,
        state=AMBIGUOUS,
        remote_ref=None,
        detail=f"legacy awaiting_publication row: {reason}",
    )


def _rank(state: str, resume_state: str | None) -> int:
    """Where a job stands, reading an ambiguous job as what it interrupted."""
    if state == AMBIGUOUS:
        return _RANK.get(resume_state or PENDING, 0)
    return _RANK.get(state, 0)


def validate_transition(*, current: str, resume_state: str | None, target: str) -> None:
    """Refuse a move the lifecycle does not allow, with the reason it refused."""
    if current in TERMINAL_STATES:
        raise PublicationJobStateError(
            f"a {current} publication job is settled and cannot become {target}"
        )
    if target in EXIT_STATES:
        return
    if current == AMBIGUOUS and target == (resume_state or PENDING):
        # Leaving doubt for the state it interrupted is a resume, not a move.
        return
    if target not in _RANK:
        raise PublicationJobStateError(f"{target!r} is not a publication job state")
    if _rank(target, None) <= _rank(current, resume_state):
        raise PublicationJobStateError(
            f"a publication job never walks backwards: {current} -> {target}"
        )


def _proved_target(proved: str, state: str, resume_state: str | None) -> str:
    """Whether *proved* is enough to move a job out of *state*.

    Strictly more than the job already has, except for a job in doubt: there,
    evidence that MATCHES what it interrupted is what ends the doubt. A host
    that crashed between the push and the row it was about to write sees a
    branch at its own head on the way back, and that observation has to be
    allowed to say "yes, that happened" rather than leaving the job stuck.
    """
    reached, standing = _rank(proved, None), _rank(state, resume_state)
    if state == AMBIGUOUS:
        return proved if reached >= standing else state
    return proved if reached > standing else state


def resolve_observation(
    *, state: str, resume_state: str | None, observation: RemoteObservation
) -> str:
    """The state *observation* proves for a job currently in *state*.

    An ``absent`` remote is the interesting case: it resolves doubt without
    proving progress, so an ambiguous job returns to the state it interrupted
    and any other job simply stays where it is. A ``conflict`` proves nothing
    about this publication at all — it is somebody else's state — so it moves
    the job nowhere while still being worth writing down.
    """
    if observation.kind == REMOTE_ABSENT:
        return (resume_state or PENDING) if state == AMBIGUOUS else state
    if observation.kind == REMOTE_CONFLICT:
        return state
    proved = _OBSERVED_STATE.get(observation.kind)
    if proved is None:
        raise PublicationJobValueError(
            f"{observation.kind!r} is not a remote observation"
        )
    if proved == AMBIGUOUS:
        return AMBIGUOUS
    return _proved_target(proved, state, resume_state)


#: The identity a host-brokered commit is made under. Deliberately not the
#: agent's: the agent's ``user.name`` lives in a directory the agent can write,
#: and a commit carrying it would attribute host-verified work to
#: attacker-chosen metadata. E7 may make this configurable; until something
#: needs it to vary, one constant is one fewer thing that can move a SHA.
_HOST_NAME = "kew"
_HOST_EMAIL = "kew@invalid.local"


@dataclass(frozen=True)
class CommitIdentity:
    """The name and address a commit is authored and committed under."""

    name: str
    email: str


@dataclass(frozen=True)
class CommitIntent:
    """The whole of the commit a persisted job names, and nothing else.

    Every field comes off a row written before git was asked for anything, so
    recomputing this after a crash is the same act as remembering it. Nothing
    here reads a clock, an environment or a repository — that is the property
    the deterministic SHA rests on.
    """

    tree_sha: str
    parent_sha: str | None
    message: str
    author: CommitIdentity
    committer: CommitIdentity
    when: str
    extra_parents: tuple[str, ...] = ()


def host_commit_identity() -> CommitIdentity:
    """The identity kew commits under when a caller names no other."""
    return CommitIdentity(name=_HOST_NAME, email=_HOST_EMAIL)


def _git_date(created_at: str) -> str:
    """*created_at* as git's raw date, read as UTC when it carries no zone.

    UTC rather than local: a host that moves zone, or a colleague running the
    same recovery elsewhere, must still name the same commit.
    """
    try:
        moment = datetime.fromisoformat(created_at)
    except (TypeError, ValueError) as exc:
        raise PublicationJobValueError(
            f"{created_at!r} is not a timestamp a commit can be dated from"
        ) from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return f"@{int(moment.timestamp())} +0000"


def _commit_message(job: PublicationJob) -> str:
    """The message the job persisted, or a deterministic stand-in for it.

    A stand-in rather than a refusal: an empty message is a thin intent, not a
    contradictory one. What matters is that it is the same string every time.
    """
    if job.message:
        return job.message
    return f"kew: publish {job.run_id} attempt {job.attempt_number}"


def _commit_parent(job: PublicationJob, predecessor_head: str | None) -> str:
    """What the new commit descends from.

    A first job commits onto the trusted base. A successor commits onto the
    head its predecessor left on the branch, because that branch may already be
    published and a commit that skipped it would need a force-push to land. A
    predecessor that never got as far as a commit leaves the base.
    """
    if predecessor_head is None:
        return job.base_sha
    if job.predecessor_job_id is None:
        raise PublicationJobValueError(
            f"job {job.job_id} was offered parent {predecessor_head} but has no"
            " predecessor that could have left it"
        )
    _require(
        _is_oid(predecessor_head),
        f"predecessor head {predecessor_head!r} is not a commit id",
    )
    return predecessor_head


def commit_intent_for(
    job: PublicationJob,
    *,
    predecessor_head: str | None = None,
    identity: CommitIdentity | None = None,
) -> CommitIntent:
    """The one commit *job* names, derived from persisted fields alone.

    Deliberately pure. The fields a retry moves — ``attempt_count``,
    ``last_step``, ``last_attempt_at``, ``updated_at``, ``detail`` — are not
    read, so a second attempt at a publication names the first attempt's commit
    rather than a sibling of it.
    """
    if job.state in TERMINAL_STATES:
        raise PublicationJobStateError(
            f"a {job.state} publication job is settled and names no commit"
        )
    _require(_is_oid(job.tree_sha), f"tree {job.tree_sha!r} is not a tree id")
    _require(_is_oid(job.base_sha), f"base {job.base_sha!r} is not a commit id")
    who = identity or host_commit_identity()
    return CommitIntent(
        tree_sha=job.tree_sha,
        parent_sha=_commit_parent(job, predecessor_head),
        message=_commit_message(job),
        author=who,
        committer=who,
        when=_git_date(job.created_at),
    )


# ---------------------------------------------------------------------------
# I09B: the two effects that leave the host.
#
# A commit can be recomputed, so remembering it and recreating it are the same
# act. A push and a pull request cannot: the only way to know whether one
# happened is to look. So each of the two classifiers below takes the persisted
# row AND what the forge was just observed to show, and answers with exactly
# one of three things — the single effect that is still owed, ``None`` because
# the remote already holds it, or a typed conflict. There is no fourth answer,
# and in particular there is no "do it again and hope", which is what turns a
# lost response into a second branch or a duplicate pull request.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ObservedBranch:
    """What the forge showed for one branch — including that it has none."""

    name: str
    head_sha: str | None = None

    @property
    def exists(self) -> bool:
        return self.head_sha is not None


@dataclass(frozen=True)
class ObservedPullRequest:
    """One pull request as the forge reports it, whoever opened it."""

    number: int
    url: str
    head_branch: str
    base_branch: str
    head_sha: str | None = None
    state: str = PR_OPEN
    body: str = ""

    @property
    def merged(self) -> bool:
        return self.state == PR_MERGED


@dataclass(frozen=True)
class PushIntent:
    """One push, carrying the compare-and-set the observation justifies."""

    branch: str
    head_sha: str
    expected_head: str | None = None

    @property
    def lease(self) -> str:
        """The CAS in git's own spelling, for a forge that speaks it.

        ``refs/heads/x:<sha>`` requires the ref to be exactly that sha;
        ``refs/heads/x:`` requires it not to exist at all. Nothing here runs
        git — this is the *statement* of the expectation, so that a forge
        adapter cannot quietly widen it into a force-push.
        """
        return f"refs/heads/{self.branch}:{self.expected_head or ''}"


@dataclass(frozen=True)
class PullRequestIntent:
    """One pull request, named by the persisted row and nothing else."""

    branch: str
    base_branch: str
    head_sha: str
    title: str
    body: str
    marker: str


def publication_marker(job: PublicationJob) -> str:
    """The stable string that identifies a pull request as this job's.

    Derived from the job id, so it survives every crash and every retry: a
    host that lost the response to a create can still tell its own pull
    request from one that happens to share the branch.
    """
    return f"<!-- kew-publication-job: {job.job_id} -->"


def compatible_heads(
    job: PublicationJob, predecessor_head: str | None = None
) -> tuple[str, ...]:
    """The remote heads this publication may move a branch on from.

    Its own head (already there), the trusted base, and the head a predecessor
    left — every one of them an ancestor of the commit this job names, by
    construction. Any other head is somebody else's work, and moving a branch
    off it would need a force.
    """
    heads = [job.base_sha]
    if predecessor_head is not None:
        heads.append(predecessor_head)
    if job.head_sha is not None:
        heads.append(job.head_sha)
    return tuple(heads)


def remote_conflict(message: str, *, ref: str | None) -> PublicationRemoteConflictError:
    """A typed conflict carrying the observation that should be recorded."""
    return PublicationRemoteConflictError(
        message,
        observation=RemoteObservation(kind=REMOTE_CONFLICT, ref=ref, detail=message),
    )


def _require_head(job: PublicationJob, effect: str) -> str:
    if job.head_sha is None:
        raise PublicationJobStateError(
            f"job {job.job_id} has no commit to {effect}: it is {job.state}"
        )
    return job.head_sha


def push_intent_for(
    job: PublicationJob,
    *,
    observed: ObservedBranch,
    predecessor_head: str | None = None,
) -> PushIntent | None:
    """The one push *job* still owes, or None when the branch already holds it.

    Raises :class:`PublicationRemoteConflictError` when the branch is at a head
    this lineage never left. That is the whole difference between reconciling
    and overwriting.
    """
    head = _require_head(job, "push")
    _require(
        observed.name == job.branch,
        f"branch {observed.name!r} is not the branch job {job.job_id} names",
    )
    if observed.head_sha is None:
        return PushIntent(branch=job.branch, head_sha=head, expected_head=None)
    _require(
        _is_oid(observed.head_sha),
        f"remote head {observed.head_sha!r} is not a commit id",
    )
    if observed.head_sha == head:
        return None
    if observed.head_sha in compatible_heads(job, predecessor_head):
        return PushIntent(
            branch=job.branch, head_sha=head, expected_head=observed.head_sha
        )
    raise remote_conflict(
        f"branch {job.branch} is at {observed.head_sha}, which this publication"
        " never left",
        ref=job.branch,
    )


def pull_request_title(job: PublicationJob) -> str:
    """The first meaningful line of the persisted message."""
    lines = [line.strip() for line in _commit_message(job).splitlines()]
    return next(
        (line for line in lines if line),
        f"kew: publish {job.run_id} attempt {job.attempt_number}",
    )


def pull_request_body(job: PublicationJob) -> str:
    """The persisted message plus the marker that names the job."""
    return f"{_commit_message(job)}\n\n{publication_marker(job)}"


def _refuse_incompatible_pull_request(
    job: PublicationJob,
    observed: ObservedPullRequest,
    *,
    base_branch: str,
    predecessor_head: str | None,
) -> None:
    """Bind what is compatible with this lineage; hold everything else.

    A pull request the agent opened from the same branch onto the same base is
    compatible — it is a description of the same change — so the host adopts it
    rather than opening a second one beside it. A different base, a different
    branch, a closed pull request or a head this lineage never named are all
    somebody's decision, and reconciliation does not get to overrule one.
    """
    if observed.head_branch != job.branch:
        raise remote_conflict(
            f"pull request {observed.url} is from {observed.head_branch}, not"
            f" {job.branch}",
            ref=observed.url,
        )
    if observed.state == PR_CLOSED:
        raise remote_conflict(
            f"pull request {observed.url} is closed; reopening it is a decision",
            ref=observed.url,
        )
    if observed.base_branch != base_branch:
        raise remote_conflict(
            f"pull request {observed.url} targets {observed.base_branch}, not"
            f" {base_branch}",
            ref=observed.url,
        )
    if observed.head_sha is not None and observed.head_sha not in compatible_heads(
        job, predecessor_head
    ):
        raise remote_conflict(
            f"pull request {observed.url} is at {observed.head_sha}, which this"
            " publication never named",
            ref=observed.url,
        )


def pull_request_intent_for(
    job: PublicationJob,
    *,
    base_branch: str,
    observed: ObservedPullRequest | None,
    predecessor_head: str | None = None,
) -> PullRequestIntent | None:
    """The one pull request *job* still owes, or None when one already fits."""
    head = _require_head(job, "open a pull request for")
    _require(bool(base_branch), "a pull request needs a base branch")
    if observed is not None:
        _refuse_incompatible_pull_request(
            job,
            observed,
            base_branch=base_branch,
            predecessor_head=predecessor_head,
        )
        return None
    return PullRequestIntent(
        branch=job.branch,
        base_branch=base_branch,
        head_sha=head,
        title=pull_request_title(job),
        body=pull_request_body(job),
        marker=publication_marker(job),
    )


# ---------------------------------------------------------------------------
# I14: the capacity one publication holds while a person looks at it.
#
# Occupancy is not a local number. A restarted host, a second host, or a
# person opening a pull request by hand all put work in front of the same
# reviewers, so the only honest count is the local holds PLUS what the
# delivery repository was last *observed* to be carrying. Reading the forge is
# a network call and must never happen inside the SQLite write transaction, so
# the reconciliation is a separate step that leaves behind the typed
# observation below — availability, when it was taken, which generation it is,
# and the exact obligations it saw.
#
# The one rule that makes the whole thing safe: an observation that could not
# be taken, or that is too old to trust, is NOT an observation of zero. It
# refuses the reservation instead, because "we could not ask" and "nobody is
# waiting" are opposite facts and only one of them frees a slot.
# ---------------------------------------------------------------------------

#: The forge answered, and this is what it holds.
REVIEW_REMOTE_AVAILABLE = "available"
#: The forge could not be read. Says nothing about occupancy, deliberately.
REVIEW_REMOTE_UNAVAILABLE = "unavailable"

#: What a taken review slot is still owed to.
SLOT_HELD = "held"
SLOT_RELEASED = "released"


class PublicationCapacityUnknownError(PublicationJobError):
    """Remote truth is stale or unavailable, so occupancy cannot be counted.

    Deliberately not a capacity error: the caller is not being told the queue
    is full, it is being told nobody knows how full it is. Both refuse, but
    only one of them is fixed by waiting for a slot.
    """


@dataclass(frozen=True)
class ReviewObligation:
    """One thing the delivery repository is holding in front of a person.

    ``job_id`` is filled in when the pull request carries this host's
    publication marker. An obligation without one is somebody else's open pull
    request — an orphan, or a colleague's — and it occupies a reviewer just the
    same, which is why it is counted rather than filtered out.
    """

    ref: str
    job_id: str | None = None
    detail: str = ""

    def validate(self) -> None:
        _require(bool(self.ref), "a review obligation needs a remote reference")
        _require(
            self.job_id is None or bool(self.job_id),
            "a review obligation names a publication job or none at all",
        )

    def as_facts(self) -> dict[str, str | None]:
        return {"ref": self.ref, "job_id": self.job_id, "detail": self.detail}

    @classmethod
    def from_facts(cls, facts: dict[str, Any]) -> ReviewObligation:
        return cls(
            ref=str(facts.get("ref") or ""),
            job_id=facts.get("job_id") or None,
            detail=str(facts.get("detail") or ""),
        )


@dataclass(frozen=True)
class RemoteReviewObservation:
    """What one delivery repository was last seen to be holding, and when.

    Persisted as a whole. A reader that trusted the obligation list without
    the availability and the timestamp beside it would turn a failed ``gh``
    call into an empty queue, which is the exact mistake this type exists to
    make impossible.
    """

    delivery_repo: str
    availability: str
    observed_at: str
    generation: int
    obligations: tuple[ReviewObligation, ...] = ()
    detail: str = ""

    @property
    def available(self) -> bool:
        """True only when the forge actually answered."""
        return self.availability == REVIEW_REMOTE_AVAILABLE

    def validate(self) -> None:
        _require(bool(self.delivery_repo), "an observation names a repository")
        _require(
            self.availability in (REVIEW_REMOTE_AVAILABLE, REVIEW_REMOTE_UNAVAILABLE),
            f"{self.availability!r} is not a remote availability",
        )
        _require(
            isinstance(self.generation, int)
            and not isinstance(self.generation, bool)
            and self.generation >= 0,
            f"{self.generation!r} is not an observation generation",
        )
        _parse_observed(self.observed_at)
        for obligation in self.obligations:
            obligation.validate()
        _require(
            self.available or not self.obligations,
            "an unavailable observation saw nothing and may claim nothing",
        )

    def age_seconds(self, now: datetime) -> float:
        """How long ago this was taken, never negative."""
        return max(0.0, (now - _parse_observed(self.observed_at)).total_seconds())

    def fresh(self, *, now: datetime, max_age_seconds: float) -> bool:
        """Whether this observation is recent enough to reserve against."""
        return self.age_seconds(now) <= max_age_seconds

    def as_json(self) -> str:
        return json.dumps(
            {
                "delivery_repo": self.delivery_repo,
                "availability": self.availability,
                "observed_at": self.observed_at,
                "generation": self.generation,
                "obligations": [o.as_facts() for o in self.obligations],
                "detail": self.detail,
            },
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, raw: str) -> RemoteReviewObservation:
        try:
            facts = json.loads(raw)
        except ValueError as exc:
            raise PublicationJobValueError(
                "the persisted review observation is not readable"
            ) from exc
        return cls(
            delivery_repo=str(facts.get("delivery_repo") or ""),
            availability=str(facts.get("availability") or ""),
            observed_at=str(facts.get("observed_at") or ""),
            generation=int(facts.get("generation") or 0),
            obligations=tuple(
                ReviewObligation.from_facts(item)
                for item in facts.get("obligations") or ()
            ),
            detail=str(facts.get("detail") or ""),
        )


def _parse_observed(stamp: str) -> datetime:
    """*stamp* as an aware UTC moment, refusing anything else."""
    try:
        moment = datetime.fromisoformat(stamp)
    except (TypeError, ValueError) as exc:
        raise PublicationJobValueError(
            f"{stamp!r} is not a time an observation could have been taken at"
        ) from exc
    if moment.tzinfo is None:
        raise PublicationJobValueError(
            f"{stamp!r} has no timezone — an observation time must be explicit UTC"
        )
    return moment.astimezone(UTC)


def unavailable_review_observation(
    *, delivery_repo: str, generation: int, observed_at: str, detail: str = ""
) -> RemoteReviewObservation:
    """The observation a failed remote read produces — never an empty one."""
    return RemoteReviewObservation(
        delivery_repo=delivery_repo,
        availability=REVIEW_REMOTE_UNAVAILABLE,
        observed_at=observed_at,
        generation=generation,
        detail=detail or "the delivery repository could not be read",
    )


def require_countable_observation(
    observation: RemoteReviewObservation | None,
    *,
    delivery_repo: str,
    expected_generation: int,
    now: datetime,
    max_age_seconds: float,
) -> RemoteReviewObservation:
    """The observation capacity may be counted from, or a loud refusal.

    Four ways to be uncountable, all of them the same answer: there is none,
    it is another repository's, it is not the generation the caller just
    reconciled, or it is unavailable or too old. None of them is zero.
    """
    if observation is None:
        raise PublicationCapacityUnknownError(
            f"{delivery_repo} has no remote review observation; reconcile before"
            " reserving"
        )
    observation.validate()
    if observation.delivery_repo != delivery_repo:
        raise PublicationCapacityUnknownError(
            f"the persisted observation is for {observation.delivery_repo}, not"
            f" {delivery_repo}"
        )
    if observation.generation != expected_generation:
        raise PublicationCapacityUnknownError(
            f"{delivery_repo} observation is generation {observation.generation},"
            f" not the expected {expected_generation}"
        )
    if not observation.available:
        raise PublicationCapacityUnknownError(
            f"{delivery_repo} was unavailable when it was last observed; an"
            " unreadable forge is not an empty one"
        )
    if not observation.fresh(now=now, max_age_seconds=max_age_seconds):
        raise PublicationCapacityUnknownError(
            f"{delivery_repo} was last observed"
            f" {observation.age_seconds(now):.0f}s ago, past the"
            f" {max_age_seconds:.0f}s this reservation trusts"
        )
    return observation


def review_slot_disposition(
    *, availability: str, pull_request: ObservedPullRequest | None
) -> str:
    """Whether the slot this publication took is still owed to a person.

    Only two things free a slot: a terminal human decision (merged, closed,
    cancelled), or a *definite* observation that there is no pull request to
    review at all. Everything else holds — an approved-but-unmerged pull
    request, one with changes requested, one with failing CI or a conflict,
    and above all a forge that could not be read. Doubt holds, because
    releasing on doubt is how a cap becomes a suggestion.
    """
    if availability != REVIEW_REMOTE_AVAILABLE:
        return SLOT_HELD
    if pull_request is None:
        return SLOT_RELEASED
    if pull_request.state in (PR_MERGED, PR_CLOSED):
        return SLOT_RELEASED
    return SLOT_HELD


def count_review_occupancy(
    *,
    held_lineages: Iterable[str],
    obligations: Iterable[ReviewObligation],
    lineage_by_job: Mapping[str, str],
) -> int:
    """How many distinct publications one repository has in front of a person.

    Local holds and remote obligations are the same fact seen twice, so they
    are unioned by lineage rather than added: a job holding a slot whose pull
    request the forge also reports is one obligation, not two. A successor SHA
    shares its predecessor's lineage and collapses onto it for the same reason.
    An obligation this host cannot attribute to a job counts under its own
    reference — an orphan pull request occupies a reviewer too.
    """
    occupied = set(held_lineages)
    for obligation in obligations:
        lineage = lineage_by_job.get(obligation.job_id or "")
        occupied.add(lineage or obligation.ref)
    return len(occupied)
