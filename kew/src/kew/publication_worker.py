"""I09A/I09B: one persisted job becomes one commit, one branch, one pull request.

The host has to be able to crash anywhere in a publication and come back
knowing whether it already did the thing it was about to do. For the commit
step that question has an exact answer, because a git commit is its own content
hash: if every field of the commit comes off rows that were written *before*
git was asked for anything, then recomputing the commit and remembering it are
the same act. Two runs of this module against one job produce one SHA, or the
second run refuses.

So the order here is deliberate and is the whole design:

1. Refuse anything the state cannot support — a settled job, a repository
   provisioned at another base, a tree that repository does not hold, a job
   that disagrees with the receipt I07 sealed for it.
2. Derive the commit from the persisted row alone
   (:func:`kew.publication_job.commit_intent_for`). Nothing in it reads a
   clock, an environment or a repository.
3. Record the attempt, so a row says "a commit was about to be made" *before*
   one is.
4. Create the object, read it back, and confirm it says what the intent said.
5. Attest the exact SHA against the sealed receipt, then move the job to
   ``committed``.

Steps 4 and 5 are in that order because the object is real the moment it
exists: evidence that names a commit which exists is honest, while a row
claiming ``committed`` with no evidence behind it is not. The attestation is
idempotent against the head it names, so a crash between 4 and 5 leaves one
fact and a retry that completes.

## The two effects that leave the host (I09B)

A commit can be recomputed; a push and a pull request cannot. Their equivalent
of determinism is observation, so every repeat runs the same three steps in the
same order: **look** at the remote, **classify** what is there against the
persisted row, and only then act on what is still owed.

1. Look. An unreadable forge is doubt — the job goes ``ambiguous`` remembering
   what it interrupted, and nothing is attempted blind.
2. Classify (:func:`~kew.publication_job.push_intent_for`,
   :func:`~kew.publication_job.pull_request_intent_for`). Compatible state
   binds: a branch already at our head, or a pull request from our branch onto
   our base — including one the agent opened — becomes this lineage's, with no
   second effect. Conflicting state holds: the observation is written down and
   a typed conflict is raised, because the only ways past it are a force-push
   and a duplicate pull request.
3. Act, once, on a lease. The push carries the exact head the observation
   showed, so a branch that moved underneath us is refused by the forge rather
   than overwritten; the pull request carries a marker derived from the job id.
   A lost response is resolved by LOOKING again, never by trying again.

Evidence is appended in the same order the commit step uses — the remote fact
first, then the row that claims it — so a crash in between leaves a true fact
and a retry that completes. None of it edits the sealed I07 receipt; every
publication fact is an attestation against it.

**Nothing here publishes.** :func:`materialize_next` and
:func:`reconcile_next` are the only doors onto the job selector and the
selector refuses until E7 installs publication capacity, so this module has —
and is asserted by its tests to have — no dispatch or product caller at all.
No forge implementation ships with it either: :class:`PublicationRemote` is a
structural type, and E7/I14 owns binding one to real credentials.

It also runs no git of its own. Every git call goes through the
:class:`~kew.publication_repository.CanonicalRepository` I07 provisioned, which
is the one repository the host is allowed to read or write, so there is no
second place a directory an agent can write could be passed in.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from kew.publication import sanitize_publication_evidence
from kew.publication_credentials import (
    CredentialPublicationRefused,
    publication_output_guard,
    require_normalized_job_credentials,
)
from kew.publication_job import (
    COMMITTED,
    RECONCILED,
    REMOTE_BRANCH,
    REMOTE_PULL_REQUEST,
    REMOTE_SETTLED,
    REMOTE_UNKNOWN,
    STEP_COMMIT,
    STEP_PR_CREATE,
    STEP_PUSH,
    TERMINAL_STATES,
    CommitIdentity,
    ObservedBranch,
    ObservedPullRequest,
    PublicationRemoteConflictError,
    PullRequestIntent,
    PushIntent,
    RemoteObservation,
    commit_intent_for,
    publication_marker,
    pull_request_intent_for,
    push_intent_for,
    remote_conflict,
    resolve_observation,
)
from kew.publication_repository import (
    VERIFICATION_EXACT_COMMIT,
    CanonicalRepository,
    CommitIntentLike,
    _read_env,
    commit_tree,
    read_commit,
)
from kew.publication_scan import OutputScanTarget, recorded_scan_verdict

if TYPE_CHECKING:
    from collections.abc import Iterator
    from datetime import datetime

    from kew.publication_job import PublicationJob
    from kew.run_store import SqliteRunStore

__all__ = [
    "COMMIT_ATTESTATION",
    "PULL_REQUEST_ATTESTATION",
    "PUSH_ATTESTATION",
    "RECONCILE_ATTESTATION",
    "MaterializedCommit",
    "PublicationHeadStale",
    "PublicationRemote",
    "PublicationWorkerError",
    "PullRequestOutcome",
    "PushOutcome",
    "ReconciledPublication",
    "finalize_publication",
    "materialize_commit",
    "materialize_next",
    "open_publication_pull_request",
    "push_publication",
    "reconcile_next",
    "reconcile_publication",
]

#: The receipt attestations a publication appends, one per fact it proved.
#: None of them is terminal: a terminal attestation seals the receipt against
#: every later fact, and this node is not the last thing that will have
#: something true to say about a published artifact.
COMMIT_ATTESTATION = "publication_commit"
PUSH_ATTESTATION = "publication_push"
PULL_REQUEST_ATTESTATION = "publication_pull_request"
RECONCILE_ATTESTATION = "publication_reconciled"


class PublicationWorkerError(RuntimeError):
    """The worker refused to materialize, and the message says what it lacked."""


class PublicationHeadStale(PublicationWorkerError):
    """The row's head moved from the head this call checked; nothing is pushed.

    Not a finding and not a settlement: the row is left as it stands, and the
    next reconcile tick checks the head the row now names.
    """

    reason = "publication_head_stale"


@dataclass(frozen=True)
class MaterializedCommit:
    """The commit a job named, once it exists and has been read back."""

    job_id: str
    head_sha: str
    tree_sha: str
    parent_sha: str | None
    verification: str = VERIFICATION_EXACT_COMMIT


def materialize_commit(
    store: SqliteRunStore,
    repo: CanonicalRepository,
    *,
    job: PublicationJob,
    now: datetime | str,
    identity: CommitIdentity | None = None,
) -> MaterializedCommit:
    """Create — or re-derive — the one commit *job* names, in *repo*.

    Idempotent. A job already in ``committed`` recomputes its commit and is
    checked against the head it recorded: matching, the recorded head comes
    back untouched; differing, that is incompatible state and a refusal, never
    an overwrite of a SHA something downstream may already have pushed.
    """
    require_normalized_job_credentials(store, job)
    _refuse_settled(job)
    _refuse_untrusted_repository(repo, job)
    _refuse_unsealed(store, job)
    intent = commit_intent_for(
        job, predecessor_head=_predecessor_head(store, repo, job), identity=identity
    )
    if job.head_sha is not None:
        # A head is only ever written by a materialization, so any job carrying
        # one — committed, pushed, or ambiguous about a push — is confirmed
        # rather than committed again. Keying on the recorded head instead of
        # on ``state == committed`` is what makes a job that has moved PAST the
        # commit re-derivable without a refusal it has already outgrown.
        return _confirm_recorded(repo, job, intent)
    store.record_publication_attempt(job_id=job.job_id, step=STEP_COMMIT, now=now)
    head = _create_and_verify(repo, intent)
    publication_output_guard(store, run_id=job.run_id)(
        OutputScanTarget(
            repository=repo.path,
            base_sha=intent.parent_sha or job.base_sha,
            head_sha=head,
            env=_read_env(),
        )
    )
    made = _made(job, head, intent)
    _attest(store, job, made, now=now)
    store.mark_publication_state(
        job_id=job.job_id,
        state=COMMITTED,
        expected_state=job.state,
        head_sha=head,
        detail=f"materialized {head} from tree {intent.tree_sha}",
        now=now,
    )
    return made


def materialize_next(
    store: SqliteRunStore,
    repo: CanonicalRepository,
    *,
    now: datetime | str,
    identity: CommitIdentity | None = None,
) -> MaterializedCommit | None:
    """The next job a worker would materialize — closed until E7.

    The single door between persisted intent and a commit. It goes through the
    gated selector rather than around it, so the E5 feature gate governs this
    module the same way it governs everything else that could cause an effect.
    """
    job = store.next_publication_job()
    if job is None or job.state in TERMINAL_STATES:
        return None
    return materialize_commit(store, repo, job=job, now=now, identity=identity)


def _refuse_settled(job: PublicationJob) -> None:
    if job.state in TERMINAL_STATES:
        raise PublicationWorkerError(
            f"publication job {job.job_id} is {job.state} and is settled"
        )


def _refuse_untrusted_repository(
    repo: CanonicalRepository, job: PublicationJob
) -> None:
    """The repository must be the one this job's tree was promoted into.

    Not merely "a repository holding that tree": a canonical repository is
    identified by the base it was provisioned at, and committing a tree onto
    someone else's base is how a publication silently changes what it applies
    to.
    """
    if repo.base_sha != job.base_sha:
        raise PublicationWorkerError(
            f"job {job.job_id} applies to base {job.base_sha}, but the host's"
            f" trusted base is {repo.base_sha}"
        )
    if not repo.holds_tree(job.tree_sha):
        raise PublicationWorkerError(
            f"tree {job.tree_sha} is not in the canonical repository, so the"
            " package it came from was never promoted here"
        )


def _refuse_unsealed(store: SqliteRunStore, job: PublicationJob) -> None:
    """The sealed I07 receipt is the only evidence a commit may be made on.

    The store already checks this when a job is created. It is checked again
    here because a commit is an effect and the row it acts on was read some
    time ago — and because a job reconstructed by hand, or by a future caller,
    must not be able to reach git without the evidence behind it.
    """
    receipt = store.get_artifact_receipt(
        run_id=job.run_id, attempt_number=job.attempt_number
    )
    if receipt is None:
        raise PublicationWorkerError(
            f"run {job.run_id} attempt {job.attempt_number} sealed no artifact"
            " receipt to publish"
        )
    for field, mine in (
        ("receipt_digest", job.receipt_digest),
        ("tree_sha", job.tree_sha),
        ("base_sha", job.base_sha),
    ):
        if getattr(receipt, field) != mine:
            raise PublicationWorkerError(
                f"job {job.job_id} names {field} {mine}, but the sealed receipt"
                f" names {getattr(receipt, field)}"
            )


def _predecessor_head(
    store: SqliteRunStore, repo: CanonicalRepository, job: PublicationJob
) -> str | None:
    """The commit a successor builds on, or None when it starts from the base.

    A successor takes over a lineage whose branch may already carry its
    predecessor's commit, so it commits on top of that rather than beside it.
    A predecessor head the canonical repository cannot see is a refusal: the
    alternative — quietly re-parenting onto the base — would produce a branch
    that only a force-push could land, which is the reconciliation problem
    I09B exists to avoid rather than to inherit.
    """
    if job.predecessor_job_id is None:
        return None
    predecessor = store.get_publication_job(job.predecessor_job_id)
    if predecessor is None or predecessor.head_sha is None:
        return None
    if not repo.holds_commit(predecessor.head_sha):
        raise PublicationWorkerError(
            f"job {job.job_id} would commit onto parent {predecessor.head_sha}"
            f" left by {job.predecessor_job_id}, which is not in the canonical"
            " repository"
        )
    return predecessor.head_sha


def _create_and_verify(repo: CanonicalRepository, intent: CommitIntentLike) -> str:
    """Create the commit, then prove the object says what the intent said.

    The second ``commit_tree`` is not a retry. It is the repeatability claim
    being made out loud: the same intent, asked twice, names one SHA. That is
    the fact :data:`~kew.publication_repository.VERIFICATION_EXACT_COMMIT`
    records, and without it the attestation would only mean "git exited zero".
    """
    head = commit_tree(repo, intent)
    again = commit_tree(repo, intent)
    if again != head:
        raise PublicationWorkerError(
            f"the intent does not name one commit: {head} then {again}"
        )
    facts = read_commit(repo, head)
    expected = () if intent.parent_sha is None else (intent.parent_sha,)
    if facts.tree_sha != intent.tree_sha or facts.parents != expected:
        raise PublicationWorkerError(
            f"commit {head} holds tree {facts.tree_sha} on {facts.parents}, not"
            f" tree {intent.tree_sha} on {expected}"
        )
    return head


def _made(
    job: PublicationJob, head: str, intent: CommitIntentLike
) -> MaterializedCommit:
    return MaterializedCommit(
        job_id=job.job_id,
        head_sha=head,
        tree_sha=intent.tree_sha,
        parent_sha=intent.parent_sha,
    )


def _confirm_recorded(
    repo: CanonicalRepository, job: PublicationJob, intent: CommitIntentLike
) -> MaterializedCommit:
    """Re-derive an already-committed job's commit and check it still holds."""
    head = _create_and_verify(repo, intent)
    if job.head_sha is not None and job.head_sha != head:
        raise PublicationWorkerError(
            f"job {job.job_id} already recorded head {job.head_sha}, but its"
            f" persisted intent names {head}"
        )
    return _made(job, head, intent)


def _facts(made: MaterializedCommit, job: PublicationJob) -> dict[str, Any]:
    return {
        "job_id": made.job_id,
        "branch": job.branch,
        "head_sha": made.head_sha,
        "tree_sha": made.tree_sha,
        "parent_sha": made.parent_sha,
        "verification": made.verification,
    }


def _attest(
    store: SqliteRunStore,
    job: PublicationJob,
    made: MaterializedCommit,
    *,
    now: datetime | str,
) -> None:
    """Append the exact SHA to the receipt's chain, once per head.

    Idempotent against the head it names, so a retry after a crash between the
    attestation and the state write does not append the same fact twice. A
    DIFFERENT head for the same job would be a new fact and is appended — the
    chain records what happened rather than what was tidy.
    """
    _append_once(
        store,
        job,
        kind=COMMIT_ATTESTATION,
        payload=_facts(made, job),
        key="head_sha",
        now=now,
    )


def _append_once(
    store: SqliteRunStore,
    job: PublicationJob,
    *,
    kind: str,
    payload: dict[str, Any],
    key: str,
    now: datetime | str,
) -> None:
    """Append one publication fact, keyed on the thing it is a fact about.

    The key is what makes the append idempotent across a crash: a retry that
    re-proves the same head, or re-binds the same pull request, adds nothing.
    A DIFFERENT head or a different pull request is a different fact and is
    appended — the chain records what happened rather than what was tidy.
    """
    already = store.artifact_receipt_attestations(
        run_id=job.run_id, attempt_number=job.attempt_number
    )
    if any(
        recorded.kind == kind and recorded.payload.get(key) == payload.get(key)
        for recorded in already
    ):
        return
    store.attest_artifact_receipt(
        run_id=job.run_id,
        attempt_number=job.attempt_number,
        kind=kind,
        timestamp=now,
        payload=payload,
    )


class PublicationRemote(Protocol):
    """The forge, as reconciliation needs it: two reads and two effects.

    Structural rather than a class kew ships, for the same reason the commit
    step takes a :class:`~kew.publication_repository.CanonicalRepository`: the
    type is the boundary. A reconciler can only observe, push under a lease,
    look a pull request up and open one — there is no verb here for deleting a
    branch, closing a pull request or forcing anything, so no amount of
    reconciliation logic can reach for one.
    """

    def observe_branch(self, branch: str) -> ObservedBranch:
        """What *branch* holds on the remote right now, or that it is absent."""
        ...

    def push_branch(self, intent: PushIntent) -> None:
        """Move *branch* to the intent's head, honouring its lease exactly."""
        ...

    def observe_pull_request(self, branch: str) -> ObservedPullRequest | None:
        """The pull request from *branch*, whoever opened it, or None."""
        ...

    def create_pull_request(self, intent: PullRequestIntent) -> ObservedPullRequest:
        """Open the pull request the intent names and report what was opened."""
        ...


@dataclass(frozen=True)
class PushOutcome:
    """The branch after a push step, and whether this pass caused one."""

    job: PublicationJob
    pushed: bool


@dataclass(frozen=True)
class PullRequestOutcome:
    """The pull request this lineage is bound to, and who opened it."""

    job: PublicationJob
    pull_request: ObservedPullRequest
    created: bool


@dataclass(frozen=True)
class ReconciledPublication:
    """One publication, as the remote was last observed to hold it."""

    job_id: str
    state: str
    branch: str
    head_sha: str
    pull_request_url: str
    pull_request_number: int | None
    branch_pushed: bool
    pull_request_created: bool


def push_publication(
    store: SqliteRunStore,
    remote: PublicationRemote,
    *,
    repo: CanonicalRepository,
    job: PublicationJob,
    now: datetime | str,
) -> PushOutcome:
    """Put the job's commit on its branch, whatever happened last time.

    Observes first, every time — including on a repeat, because "did my push
    land?" has no answer the host can hold in memory across a crash. A branch
    already at the head is bound rather than pushed again; a branch at a head
    this lineage never left is held, not forced.

    A head with a recorded finding is refused first, whatever the run's type
    or posture, on every call. The head checked is the head pushed: a row
    whose head differs from the caller's job refuses as
    ``publication_head_stale`` and pushes nothing, so the next tick
    re-evaluates the fresh row. A head with no recorded scan (materialized
    before the scan existed) is scanned in *repo* now, against the base its
    commit step would use.
    """
    live = _live_row(store, job)
    _refuse_found_head(store, job)
    _refuse_found_head(store, live)
    _refuse_stale_head(job, live)
    require_normalized_job_credentials(store, job)
    _scan_unscanned_head(store, repo, job)
    require_normalized_job_credentials(store, job, published_head=job.head_sha)
    observed = _observe_branch(store, remote, live, now)
    with _holding(store, live, now):
        intent = push_intent_for(
            live, observed=observed, predecessor_head=_predecessor_head_row(store, live)
        )
    if intent is not None:
        store.record_publication_attempt(job_id=live.job_id, step=STEP_PUSH, now=now)
        _push_once(store, remote, live, intent, now)
    _attest_push(store, live, intent, now=now)
    return PushOutcome(
        job=_advance(store, live, _branch_observation(live), now),
        pushed=intent is not None,
    )


def open_publication_pull_request(
    store: SqliteRunStore,
    remote: PublicationRemote,
    *,
    job: PublicationJob,
    base_branch: str,
    now: datetime | str,
) -> PullRequestOutcome:
    """Bind this lineage to one pull request, opening it only if none fits.

    The lookup is by branch, which is the exact question the forge can answer
    without kew having remembered anything: *is there already a pull request
    from this branch?* One the agent opened counts, provided it targets the
    base this publication applies to — a second pull request for one change is
    a worse outcome than adopting the first.
    """
    require_normalized_job_credentials(store, job)
    live = _live_row(store, job)
    observed = _observe_pull_request(store, remote, live, now)
    with _holding(store, live, now):
        intent = pull_request_intent_for(
            live,
            base_branch=base_branch,
            observed=observed,
            predecessor_head=_predecessor_head_row(store, live),
        )
    if intent is not None:
        store.record_publication_attempt(
            job_id=live.job_id, step=STEP_PR_CREATE, now=now
        )
        observed = _create_once(store, remote, live, intent, now)
    bound = _require_pull_request(live, observed)
    _attest_pull_request(store, live, bound, created=intent is not None, now=now)
    return PullRequestOutcome(
        job=_advance(store, live, _pull_request_observation(bound), now),
        pull_request=bound,
        created=intent is not None,
    )


def finalize_publication(
    store: SqliteRunStore,
    remote: PublicationRemote,
    *,
    job: PublicationJob,
    base_branch: str,
    now: datetime | str,
) -> PublicationJob:
    """Settle the job against what the remote shows NOW, exactly once.

    Re-observed rather than remembered: settling is a claim about the current
    remote, so a branch that has moved off our head or a pull request that has
    gone holds the job instead of settling it. A job another finalizer already
    settled comes straight back — a duplicate finalizer is a no-op, never a
    second write.
    """
    require_normalized_job_credentials(store, job)
    live = _current(store, job)
    if live.state == RECONCILED:
        return live
    _refuse_settled(live)
    branch = _observe_branch(store, remote, live, now)
    pull = _observe_pull_request(store, remote, live, now)
    with _holding(store, live, now):
        bound = _bound_pull_request(
            store, live, branch=branch, pull=pull, base_branch=base_branch
        )
    _attest_reconciled(store, live, bound, now=now)
    return _advance(store, live, _settled_observation(live, bound), now)


def reconcile_publication(
    store: SqliteRunStore,
    repo: CanonicalRepository,
    remote: PublicationRemote,
    *,
    job: PublicationJob,
    base_branch: str,
    now: datetime | str,
    identity: CommitIdentity | None = None,
) -> ReconciledPublication:
    """Drive one job to a settled publication, from wherever it stands.

    Every step is idempotent on its own, so this is safe to run again after a
    crash at any point: the commit is recomputed, the branch and the pull
    request are observed, and only what is still owed is done. A job that is
    already reconciled answers out of its own evidence and touches no remote at
    all — completed work is never redispatched.
    """
    require_normalized_job_credentials(store, job)
    current = _current(store, job)
    if current.state == RECONCILED:
        return _recorded(store, current)
    _refuse_settled(current)
    made = materialize_commit(store, repo, job=current, now=now, identity=identity)
    # Push what the row names after the commit step, so the head the push
    # checks is the head it pushes (a fresh job had no head before it).
    committed = _current(store, current)
    pushed = push_publication(store, remote, repo=repo, job=committed, now=now)
    opened = open_publication_pull_request(
        store, remote, job=pushed.job, base_branch=base_branch, now=now
    )
    settled = finalize_publication(
        store, remote, job=opened.job, base_branch=base_branch, now=now
    )
    return ReconciledPublication(
        job_id=settled.job_id,
        state=settled.state,
        branch=settled.branch,
        head_sha=made.head_sha,
        pull_request_url=opened.pull_request.url,
        pull_request_number=opened.pull_request.number,
        branch_pushed=pushed.pushed,
        pull_request_created=opened.created,
    )


def reconcile_next(
    store: SqliteRunStore,
    repo: CanonicalRepository,
    remote: PublicationRemote,
    *,
    base_branch: str,
    now: datetime | str,
    identity: CommitIdentity | None = None,
) -> ReconciledPublication | None:
    """The next job a reconciler would publish — closed until E7.

    The same door :func:`materialize_next` uses, for the same reason: the E5
    feature gate governs the remote effects exactly as it governs the commit.
    """
    job = store.next_publication_job()
    if job is None or job.state in TERMINAL_STATES:
        return None
    return reconcile_publication(
        store,
        repo,
        remote,
        job=job,
        base_branch=base_branch,
        now=now,
        identity=identity,
    )


def _current(store: SqliteRunStore, job: PublicationJob) -> PublicationJob:
    """The row as it stands now, not as the caller last saw it.

    Every step re-reads, so two reconcilers racing the same job resolve against
    one row rather than each writing what it believed a moment ago.
    """
    fresh = store.get_publication_job(job.job_id)
    if fresh is None:
        raise PublicationWorkerError(
            f"publication job {job.job_id} is not persisted, so there is nothing"
            " to reconcile"
        )
    return fresh


def _recorded_head_verdict(store: SqliteRunStore, job: PublicationJob) -> str:
    """The recorded scan verdict for the job's head; unreadable evidence refuses."""
    if job.head_sha is None:
        return "missing"
    try:
        return recorded_scan_verdict(store, run_id=job.run_id, head_sha=job.head_sha)
    except Exception as exc:  # noqa: BLE001 — unreadable evidence refuses
        raise CredentialPublicationRefused("publication_evidence_unavailable") from exc


def _refuse_stale_head(job: PublicationJob, live: PublicationJob) -> None:
    """Refuse a push whose checked head is not the head the row would push."""
    if live.head_sha != job.head_sha:
        raise PublicationHeadStale(
            f"publication job {job.job_id} was checked at head"
            f" {job.head_sha or '(none)'} but its row now names"
            f" {live.head_sha or '(none)'}; nothing was pushed, and the next"
            " reconcile tick re-evaluates the row"
        )


def _refuse_found_head(store: SqliteRunStore, job: PublicationJob) -> None:
    """A recorded finding for this head refuses the push, permanently.

    Checked before anything else and for every run type and posture: a
    host, uncontained, ``env:`` or native run alike. A finding is final
    (:func:`recorded_scan_verdict`), so no later tick may admit the head.
    """
    if _recorded_head_verdict(store, job) == "found":
        raise CredentialPublicationRefused("credential_in_output")


def _scan_unscanned_head(
    store: SqliteRunStore, repo: CanonicalRepository, job: PublicationJob
) -> None:
    """Scan and record a head that has no scan record, so it is not wedged.

    A job materialized before the scan existed carries a head no scan was
    recorded for. The scan runs now with the commit step's base (the
    predecessor's head, else the job's base); a finding refuses, and so does a
    head or base *repo* does not hold. A recorded finding refuses here too,
    whatever the run's posture; a clean record is left to the gate.
    """
    if job.head_sha is None:
        return
    verdict = _recorded_head_verdict(store, job)
    if verdict == "found":
        raise CredentialPublicationRefused("credential_in_output")
    if verdict != "missing":
        return
    publication_output_guard(store, run_id=job.run_id)(
        OutputScanTarget(
            repository=repo.path,
            base_sha=_predecessor_head_row(store, job) or job.base_sha,
            head_sha=job.head_sha,
            env=_read_env(),
        )
    )


def _live_row(store: SqliteRunStore, job: PublicationJob) -> PublicationJob:
    fresh = _current(store, job)
    _refuse_settled(fresh)
    return fresh


def _predecessor_head_row(store: SqliteRunStore, job: PublicationJob) -> str | None:
    """The head a predecessor left, read off its row alone.

    The remote steps ask this to decide what a branch may be fast-forwarded
    from; unlike the commit step they never need the object itself, so they do
    not read the canonical repository to answer it.
    """
    if job.predecessor_job_id is None:
        return None
    predecessor = store.get_publication_job(job.predecessor_job_id)
    return None if predecessor is None else predecessor.head_sha


@contextmanager
def _holding(
    store: SqliteRunStore, job: PublicationJob, now: datetime | str
) -> Iterator[None]:
    """Write down what the forge showed before a conflict leaves this module.

    A conflict that was not recorded is a conflict the next operator has to
    rediscover by hand, so the observation lands on the row first — and it
    moves the job nowhere, because somebody else's branch proves nothing about
    this publication.
    """
    try:
        yield
    except PublicationRemoteConflictError as exc:
        _note(store, job, exc.observation, now)
        raise


def _note(
    store: SqliteRunStore,
    job: PublicationJob,
    observation: RemoteObservation,
    now: datetime | str,
) -> PublicationJob:
    return store.observe_publication_remote(
        job_id=job.job_id, observation=observation, now=now
    )


def _advance(
    store: SqliteRunStore,
    job: PublicationJob,
    observation: RemoteObservation,
    now: datetime | str,
) -> PublicationJob:
    """Write the observation only when it actually moves the job.

    A job that is already past this step keeps the reference it holds: a
    ``pushed`` observation written over an ``opened`` job would replace the
    pull request URL on the row with a branch name, which is a worse record of
    the same truth.
    """
    target = resolve_observation(
        state=job.state, resume_state=job.resume_state, observation=observation
    )
    if target == job.state:
        return job
    return _note(store, job, observation, now)


def _doubt(
    store: SqliteRunStore,
    job: PublicationJob,
    exc: Exception,
    now: datetime | str,
) -> None:
    """Record an unreadable remote as doubt, with evidence-safe detail.

    Sanitized rather than raw: the row and the audit chain outlive the host,
    and forge failures quote URLs, paths and occasionally credentials.
    """
    _note(
        store,
        job,
        RemoteObservation(
            kind=REMOTE_UNKNOWN, detail=sanitize_publication_evidence(exc)
        ),
        now,
    )


def _observe_branch(
    store: SqliteRunStore,
    remote: PublicationRemote,
    job: PublicationJob,
    now: datetime | str,
) -> ObservedBranch:
    try:
        return remote.observe_branch(job.branch)
    except Exception as exc:
        _doubt(store, job, exc, now)
        raise PublicationWorkerError(
            f"could not observe branch {job.branch}:"
            f" {sanitize_publication_evidence(exc)}"
        ) from exc


def _observe_pull_request(
    store: SqliteRunStore,
    remote: PublicationRemote,
    job: PublicationJob,
    now: datetime | str,
) -> ObservedPullRequest | None:
    try:
        return remote.observe_pull_request(job.branch)
    except Exception as exc:
        _doubt(store, job, exc, now)
        raise PublicationWorkerError(
            f"could not observe the pull request for {job.branch}:"
            f" {sanitize_publication_evidence(exc)}"
        ) from exc


def _push_once(
    store: SqliteRunStore,
    remote: PublicationRemote,
    job: PublicationJob,
    intent: PushIntent,
    now: datetime | str,
) -> None:
    """Push once. If the answer does not come back, look — never push again."""
    try:
        remote.push_branch(intent)
    except Exception as exc:
        _confirm_push(store, remote, job, intent, exc, now)


def _confirm_push(
    store: SqliteRunStore,
    remote: PublicationRemote,
    job: PublicationJob,
    intent: PushIntent,
    exc: Exception,
    now: datetime | str,
) -> None:
    """A failed push and a lost response look identical from here.

    The remote tells them apart: a branch that now holds our head means the
    effect landed and only the answer was lost. Anything else is doubt, and
    doubt is where this stops — a second push under the same lease would either
    fail again or, if the lease had been widened, overwrite whatever moved.
    """
    observed = _observe_branch(store, remote, job, now)
    if observed.head_sha == intent.head_sha:
        return
    _doubt(store, job, exc, now)
    raise PublicationWorkerError(
        f"pushing {job.branch} did not land: {sanitize_publication_evidence(exc)}"
    ) from exc


def _create_once(
    store: SqliteRunStore,
    remote: PublicationRemote,
    job: PublicationJob,
    intent: PullRequestIntent,
    now: datetime | str,
) -> ObservedPullRequest:
    try:
        return remote.create_pull_request(intent)
    except Exception as exc:
        return _confirm_pull_request(store, remote, job, intent, exc, now)


def _confirm_pull_request(
    store: SqliteRunStore,
    remote: PublicationRemote,
    job: PublicationJob,
    intent: PullRequestIntent,
    exc: Exception,
    now: datetime | str,
) -> ObservedPullRequest:
    """Resolve a lost create by looking the branch up, not by creating again.

    Whatever comes back is put through the same compatibility rules as any
    other pre-existing pull request: a lost response is not a licence to adopt
    something that does not fit the lineage.
    """
    found = _observe_pull_request(store, remote, job, now)
    if found is not None:
        with _holding(store, job, now):
            pull_request_intent_for(
                job,
                base_branch=intent.base_branch,
                observed=found,
                predecessor_head=_predecessor_head_row(store, job),
            )
        return found
    _doubt(store, job, exc, now)
    raise PublicationWorkerError(
        f"opening a pull request for {job.branch} did not land:"
        f" {sanitize_publication_evidence(exc)}"
    ) from exc


def _require_pull_request(
    job: PublicationJob, observed: ObservedPullRequest | None
) -> ObservedPullRequest:
    if observed is None:  # pragma: no cover - one of the two paths always sets it
        raise PublicationWorkerError(
            f"no pull request for {job.branch} after the open step"
        )
    return observed


def _bound_pull_request(
    store: SqliteRunStore,
    job: PublicationJob,
    *,
    branch: ObservedBranch,
    pull: ObservedPullRequest | None,
    base_branch: str,
) -> ObservedPullRequest:
    """The pull request this job settles against, or a conflict saying why not."""
    predecessor = _predecessor_head_row(store, job)
    if push_intent_for(job, observed=branch, predecessor_head=predecessor) is not None:
        raise remote_conflict(
            f"branch {job.branch} no longer holds {job.head_sha}, so this"
            " publication is not settled",
            ref=job.branch,
        )
    if pull is None:
        raise remote_conflict(
            f"the pull request for {job.branch} is no longer there", ref=job.branch
        )
    pull_request_intent_for(
        job, base_branch=base_branch, observed=pull, predecessor_head=predecessor
    )
    return pull


def _branch_observation(job: PublicationJob) -> RemoteObservation:
    return RemoteObservation(
        kind=REMOTE_BRANCH,
        ref=job.branch,
        detail=f"branch {job.branch} holds {job.head_sha}",
    )


def _pull_request_observation(observed: ObservedPullRequest) -> RemoteObservation:
    return RemoteObservation(
        kind=REMOTE_PULL_REQUEST,
        ref=observed.url,
        detail=f"pull request {observed.url} is bound to this lineage",
    )


def _settled_observation(
    job: PublicationJob, observed: ObservedPullRequest
) -> RemoteObservation:
    return RemoteObservation(
        kind=REMOTE_SETTLED,
        ref=observed.url,
        detail=f"{job.branch} holds {job.head_sha} and {observed.url} is open on it",
    )


def _attest_push(
    store: SqliteRunStore,
    job: PublicationJob,
    intent: PushIntent | None,
    *,
    now: datetime | str,
) -> None:
    """The head the branch carries, and the lease it was moved under."""
    _append_once(
        store,
        job,
        kind=PUSH_ATTESTATION,
        payload={
            "job_id": job.job_id,
            "branch": job.branch,
            "head_sha": job.head_sha,
            "expected_head": None if intent is None else intent.expected_head,
            "lease": None if intent is None else intent.lease,
            "pushed": intent is not None,
        },
        key="head_sha",
        now=now,
    )


def _attest_pull_request(
    store: SqliteRunStore,
    job: PublicationJob,
    observed: ObservedPullRequest,
    *,
    created: bool,
    now: datetime | str,
) -> None:
    """Which pull request this lineage bound, and whether kew opened it.

    ``marker_present`` is the honest form of that second question: a pull
    request carrying this job's marker is one kew opened, and one without it is
    the agent's, adopted. Recording which is which is what lets a reviewer read
    the compatibility finding rather than infer it.
    """
    _append_once(
        store,
        job,
        kind=PULL_REQUEST_ATTESTATION,
        payload={
            "job_id": job.job_id,
            "branch": job.branch,
            "base_branch": observed.base_branch,
            "head_sha": job.head_sha,
            "pull_request_url": observed.url,
            "pull_request_number": observed.number,
            "created": created,
            "marker_present": publication_marker(job) in observed.body,
        },
        key="pull_request_url",
        now=now,
    )


def _attest_reconciled(
    store: SqliteRunStore,
    job: PublicationJob,
    observed: ObservedPullRequest,
    *,
    now: datetime | str,
) -> None:
    _append_once(
        store,
        job,
        kind=RECONCILE_ATTESTATION,
        payload={
            "job_id": job.job_id,
            "branch": job.branch,
            "head_sha": job.head_sha,
            "pull_request_url": observed.url,
            "pull_request_number": observed.number,
        },
        key="head_sha",
        now=now,
    )


def _recorded(store: SqliteRunStore, job: PublicationJob) -> ReconciledPublication:
    """What a settled job published, read off its own append-only evidence.

    No remote call: a reconciled job has nothing left to observe, and asking
    the forge about it again would be the first half of redispatching work that
    is already done.
    """
    facts = _last_payload(store, job, PULL_REQUEST_ATTESTATION)
    number = facts.get("pull_request_number")
    return ReconciledPublication(
        job_id=job.job_id,
        state=job.state,
        branch=job.branch,
        head_sha=job.head_sha or "",
        pull_request_url=str(facts.get("pull_request_url") or job.remote_ref or ""),
        pull_request_number=number if isinstance(number, int) else None,
        branch_pushed=False,
        pull_request_created=False,
    )


def _last_payload(
    store: SqliteRunStore, job: PublicationJob, kind: str
) -> dict[str, Any]:
    found = [
        recorded.payload
        for recorded in store.artifact_receipt_attestations(
            run_id=job.run_id, attempt_number=job.attempt_number
        )
        if recorded.kind == kind
    ]
    return found[-1] if found else {}
