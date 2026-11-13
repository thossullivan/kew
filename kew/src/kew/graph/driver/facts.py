"""Driver-side GitHub facts reader.

The Phase 1 `DependencySource` yields only graph structure and a coarse
`NodeStatus` (DONE/BLOCKED/IN_REVIEW/TODO). The driver needs richer per-node
facts — issue state + stateReason, labels, and the linked PR's review/merge
state — to compute its status overlay and merge gate. This module is the
provider-aware read path for those facts; it does not interpret them (that
is the overlay's job).

Linked-PR discovery uses GitHub's own `closedByPullRequestsReferences` issue
field (the same "Closes #N" linkage GitHub itself resolves), so this module
does not need to parse issue bodies for a PR reference.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from kew.audit import Actor, emit
from kew.ci_checks import (
    CheckState,
    check_runs_command,
    classify_check_runs,
    classify_commit_status,
    commit_status_command,
    parse_check_runs,
    parse_commit_status,
)
from kew.graph.domain import NodeStatus
from kew.graph.driver.result import REVIEW_BACKPRESSURE_PREFIX
from kew.human_review import HumanReviewError, ReviewPolicy
from kew.ids import branch_slug
from kew.no_change import (
    AUTHORIZED,
    COMPLETED,
    EFFECT_ACKED,
    EFFECT_PENDING,
    SYNC_ACKED,
    SYNC_PENDING,
    DependencyEffect,
    NoChangeJudgment,
)
from kew.no_change import TERMINAL_STATES as NO_CHANGE_TERMINAL_STATES
from kew.publication_credentials import (
    CREDENTIAL_PUBLICATION_REFUSAL,
    CredentialPublicationRefused,
    IssuedKeyPublicationHeld,
    ensure_publication_credentials,
)
from kew.publication_job import (
    PublicationJobError,
    RemoteReviewObservation,
    require_countable_observation,
    unavailable_review_observation,
)
from kew.repo_host import repo_host
from kew.run_store import PR_SUBJECT_RUN_KINDS
from kew.run_store_publication_jobs import (
    DEFAULT_OBSERVATION_MAX_AGE_SECONDS,
    PublicationReview,
)
from kew.subprocess_utils import run_with_backoff as _default_run

# Callable[..., subprocess.CompletedProcess[str]]-shaped; injectable for tests.
RunFn = Any

_GREEN_MERGE_STATES = frozenset({"CLEAN", "HAS_HOOKS"})

# A persisted PR URL is durable evidence that a PR may still matter to the
# driver after it falls out of the first page of an open-PR listing.  Keep the
# recovery read bounded: a corrupt or unexpectedly large local store must not
# turn one tick into an unbounded GitHub request fan-out.
_PERSISTED_PR_BATCH_LIMIT = 100

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NodeFacts:
    issue_num: int
    state: str
    state_reason: str | None
    labels: frozenset[str]
    pr_number: int | None
    pr_url: str | None
    review_decision: str | None
    merge_state_status: str | None
    head_oid: str | None
    pr_pushed_at: str | None
    # Provider-neutral admission fact. GitHub defaults to TODO; Linear sets
    # QUEUED until both explicit epic admission markers are present.
    admission_status: NodeStatus = NodeStatus.TODO
    # The linked PR's own state ("OPEN"/"MERGED"/"CLOSED"/"UNKNOWN"), or None
    # when no PR is linked. `state` above is the item state.
    pr_state: str | None = None
    # How the PR was linked: "ledger" (the URL kew persisted at publication),
    # "closing-ref" (GitHub's keyword-derived reference), or "branch" (an open
    # or merged PR whose head is this node's branch). None when nothing linked.
    # Reported by `kew epic status` as provenance for the chosen PR.
    pr_link_source: str | None = None
    # True when GitHub reports a closing reference for the resolved PR number.
    #: True/False when the provider can observe a closing reference for the
    #: linked PR (GitHub); None when it cannot (Linear, readers without one).
    pr_has_closing_ref: bool | None = None


@dataclass(frozen=True)
class RepositoryPermission:
    """Fresh GitHub evidence about one repository actor's authority."""

    login: str
    actor_type: str
    permission: str


# Mirrors `config.branches.prefix`'s default. The reader takes the configured
# value from its caller; this only keeps a store-less/legacy construction
# working with the convention every kew dispatch has used.
_DEFAULT_BRANCH_PREFIX = "agent/issue-"
_LINK_PR_FIELDS = (
    "number,url,state,reviewDecision,mergeStateStatus,headRefOid,"
    "headRefName,commits,latestReviews,author,title"
)
#: The one-PR fresh read adds the base branch, which the merge gates bind
#: (#2814): a PR retargeted under a verification merges where none was run.
_FRESH_PR_FIELDS = f"{_LINK_PR_FIELDS},baseRefName"
# The tick's one PR read (#2582): every live node's branch and every PR number
# the ledger or a closing reference names, as aliases of a single GraphQL
# query. The selection mirrors `_LINK_PR_FIELDS`; `_flatten_batch_pr` folds the
# connections back into the shape `gh pr view --json` prints, so every consumer
# reads one payload shape whichever path produced it. Authors are `{ login }`
# only, exactly as `gh` prints them: the merge gate's reviewer checks must see
# the same evidence whichever read produced the PR.
_BATCH_PR_SELECTION = """
fragment LinkPr on PullRequest {
  number url state reviewDecision mergeStateStatus headRefOid headRefName title
  author { login }
  commits(last: 1) { nodes { commit { oid committedDate } } }
  latestReviews(first: 20) {
    nodes { state submittedAt author { login } commit { oid } }
  }
}
"""
# Matches the `--limit 5` of the per-branch fallback it replaces.
_BRANCH_PR_PAGE = 5
# `mergeStateStatus` is still served behind GitHub's merge-info preview.
_MERGE_INFO_PREVIEW = "Accept: application/vnd.github.merge-info-preview+json"


def _parse_json_object(stdout: str | None) -> dict[str, Any]:
    """Best-effort parse of a `gh ... --json` object response.

    Returns {} on any parse failure or non-object payload rather than
    raising — callers treat a missing/unparseable response as "no facts",
    consistent with `GitHubConventionSource.status_of`'s existing behavior.
    """
    try:
        parsed: Any = json.loads(stdout or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _is_canonical_pr_path(parsed: Any, repo: str, host: str) -> bool:
    """Whether a parsed URL points at this repository's pull endpoint."""
    return (
        parsed.scheme == "https"
        and parsed.netloc == host
        and not parsed.query
        and not parsed.fragment
        and parsed.path.startswith(f"/{repo}/pull/")
    )


def _latest_awaiting_publication_run(store: Any, item_id: str) -> Any | None:
    """Return the newest deferred implementation run for one durable item."""
    list_by_item = getattr(store, "list_by_item", None)
    if not callable(list_by_item):
        return None
    return next(
        (
            run
            for run in reversed(list_by_item(item_id))
            if getattr(run, "status", None) == "awaiting_publication"
            and getattr(run, "run_kind", "dispatch") not in PR_SUBJECT_RUN_KINDS
        ),
        None,
    )


@dataclass(frozen=True)
class _PrLink:
    """One resolved node -> PR linkage, with the source that resolved it."""

    number: int | None
    url: str | None
    pr: dict[str, Any]
    source: str | None
    has_closing_ref: bool


_NO_PR_LINK = _PrLink(None, None, {}, None, False)


@dataclass(frozen=True)
class _PrCandidate:
    """One source's candidate before state-first resolution."""

    number: int
    url: str | None
    pr: dict[str, Any]
    source: str
    ledger_order: int = -1


_PR_STATE_RANK = {"UNKNOWN": 0, "CLOSED": 1, "MERGED": 2, "OPEN": 3}
_PR_SOURCE_RANK = {"branch": 0, "closing-ref": 1, "ledger": 2}


def _normalise_link_pr(
    pr: dict[str, Any], *, number: int, url: str | None
) -> dict[str, Any]:
    """Return a candidate payload with a four-value state."""
    payload = dict(pr)
    state = str(payload.get("state", "")).upper()
    payload["state"] = state if state in _PR_STATE_RANK else "UNKNOWN"
    payload.setdefault("number", number)
    if url is not None:
        payload.setdefault("url", url)
    return payload


def _implementation_pr_runs(
    store: Any,
    item_id: str,
    branch: str,
    *,
    runs: Iterable[Any] | None = None,
) -> list[Any]:
    """Return implementation runs that durably name a PR on this branch."""
    if runs is None:
        list_by_item = getattr(store, "list_by_item", None)
        if not callable(list_by_item):
            return []
        runs = list(list_by_item(item_id))
    return [
        run
        for run in runs
        if getattr(run, "branch", None) == branch
        and getattr(run, "run_kind", "dispatch") not in PR_SUBJECT_RUN_KINDS
        and isinstance(getattr(run, "pr_url", None), str)
        and bool(run.pr_url)
    ]


def _candidate_pr(
    batch: Any,
    *,
    number: int,
    url: str | None,
    source: str,
    initial: dict[str, Any] | None = None,
    ledger_order: int = -1,
) -> _PrCandidate:
    """Read and normalize one candidate without dropping failed reads."""
    pr = initial or {}
    if source != "branch" or str(pr.get("state", "")).upper() not in _PR_STATE_RANK:
        read = batch.pr_facts(number)
        # ``None`` is a batch-only view saying "not read this tick": keep what
        # the candidate brought with it rather than a read that never happened.
        if read is not None:
            pr = read if isinstance(read, dict) else {}
    return _PrCandidate(
        number,
        url,
        _normalise_link_pr(pr, number=number, url=url),
        source,
        ledger_order,
    )


def _ledger_candidates(
    item_id: str,
    branch: str,
    *,
    store: Any,
    batch: Any,
    runs: Iterable[Any] | None = None,
) -> list[_PrCandidate]:
    candidates: list[_PrCandidate] = []
    for order, run in enumerate(
        _implementation_pr_runs(store, item_id, branch, runs=runs)
    ):
        url = run.pr_url
        number = batch.pr_number_from_url(url)
        if number is None:
            continue
        candidates.append(
            _candidate_pr(
                batch,
                number=number,
                url=url,
                source="ledger",
                ledger_order=order,
            )
        )
    return candidates


def _closing_ref_candidates(
    closing_refs: Iterable[dict[str, Any]], *, batch: Any
) -> list[_PrCandidate]:
    candidates: list[_PrCandidate] = []
    for ref in closing_refs:
        if not isinstance(ref, dict):
            continue
        number = ref.get("number")
        if not isinstance(number, int):
            continue
        pr_number = int(number)
        url = ref.get("url")
        candidates.append(
            _candidate_pr(
                batch,
                number=pr_number,
                url=url if isinstance(url, str) else None,
                source="closing-ref",
                initial=ref,
            )
        )
    return candidates


def _branch_candidates(branch: str, *, batch: Any) -> list[_PrCandidate]:
    candidates: list[_PrCandidate] = []
    for pr in batch.pr_candidates_for_branch(branch):
        if not isinstance(pr, dict):
            continue
        number = pr.get("number")
        if not isinstance(number, int):
            continue
        url = pr.get("url")
        candidates.append(
            _candidate_pr(
                batch,
                number=number,
                url=url if isinstance(url, str) else None,
                source="branch",
                initial=pr,
            )
        )
    return candidates


def _candidate_rank(candidate: _PrCandidate) -> tuple[int, int, int, int]:
    """Rank by state, recency, then durable ledger identity."""
    return (
        _PR_STATE_RANK[candidate.pr["state"]],
        candidate.number,
        _PR_SOURCE_RANK[candidate.source],
        candidate.ledger_order,
    )


def _log_pr_disagreement(
    item_id: str,
    candidates: list[_PrCandidate],
    seen: set[tuple[str, tuple[int, ...]]] | None,
) -> None:
    """Name every source candidate when the sources disagree."""
    numbers = tuple(sorted({candidate.number for candidate in candidates}))
    if len(numbers) < 2:
        return
    key = (item_id, numbers)
    if seen is not None:
        if key in seen:
            return
        seen.add(key)
    detail = ", ".join(
        f"{candidate.source}=#{candidate.number}:{candidate.pr['state']}"
        for candidate in candidates
    )
    _logger.warning("%s: linked PR disagreement: %s", item_id, detail)


def resolve_linked_pr(
    item_id: str,
    branch: str,
    *,
    store: Any,
    batch: Any,
    closing_refs: Iterable[dict[str, Any]],
    runs: Iterable[Any] | None = None,
) -> _PrLink:
    """Resolve one item through ledger, closing-reference, and branch facts.

    State outranks source. Newer PR numbers win within one state. A ledger
    candidate breaks an exact tie. Failed candidate reads remain ``UNKNOWN``.
    """
    candidates = _ledger_candidates(
        item_id, branch, store=store, batch=batch, runs=runs
    )
    candidates.extend(_closing_ref_candidates(closing_refs, batch=batch))
    candidates.extend(_branch_candidates(branch, batch=batch))
    if not candidates:
        return _NO_PR_LINK
    _log_pr_disagreement(
        item_id,
        candidates,
        getattr(batch, "_logged_pr_disagreements", None),
    )
    winner = max(candidates, key=_candidate_rank)
    has_closing_ref = any(
        candidate.source == "closing-ref" and candidate.number == winner.number
        for candidate in candidates
    )
    return _PrLink(winner.number, winner.url, winner.pr, winner.source, has_closing_ref)


def reconcile_awaiting_publication(
    store: Any,
    *,
    item_id: str,
    repo: str,
    pr_url: str | None,
    observation: str,
) -> bool:
    """Finalize deferred publication from one authoritative remote fact.

    Production stores atomically terminalize the unclaimed continuation minted
    for the preserved worktree.  The transition fallback keeps older/fake
    stores source-compatible while still reconciling the run row.
    """
    run = _latest_awaiting_publication_run(store, item_id)
    run_id = getattr(run, "run_id", None)
    if not isinstance(run_id, str):
        return False
    try:
        ensure_publication_credentials(store, run_id=run_id)
    except CredentialPublicationRefused as exc:
        transition = getattr(store, "transition_run_status", None)
        if callable(transition):
            transition(
                run_id,
                expected_status="awaiting_publication",
                new_status="refused",
                error_category=CREDENTIAL_PUBLICATION_REFUSAL,
                pr_url=pr_url,
            )
        emit(
            store,
            CREDENTIAL_PUBLICATION_REFUSAL,
            actor=Actor("policy", "epic-driver"),
            run_id=run_id,
            issue_num=getattr(run, "issue_num", None),
            item_id=item_id,
            repo=repo,
            payload={"reason": exc.reason, "publication_allowed": False},
        )
        return False
    except IssuedKeyPublicationHeld as exc:
        # The issuer has not confirmed revocation yet: the run stays awaiting
        # publication and the tick continues with the other nodes.
        emit(
            store,
            "publication_held",
            actor=Actor("policy", "epic-driver"),
            run_id=run_id,
            issue_num=getattr(run, "issue_num", None),
            item_id=item_id,
            repo=repo,
            payload={
                "reason": exc.reason,
                "issuer_reason": exc.issuer_reason,
                "publication_allowed": False,
            },
        )
        return False
    finalize = getattr(store, "finalize_awaiting_publication", None)
    if callable(finalize):
        changed = bool(finalize(run_id, pr_url=pr_url))
    else:
        transition = getattr(store, "transition_run_status", None)
        changed = bool(
            callable(transition)
            and transition(
                run_id,
                expected_status="awaiting_publication",
                new_status="success",
                error_category="",
                pr_url=pr_url,
            )
        )
    if changed:
        emit(
            store,
            "publication_reconciled",
            actor=Actor("policy", "epic-driver"),
            run_id=run_id,
            issue_num=getattr(run, "issue_num", None),
            item_id=item_id,
            repo=repo,
            payload={"observation": observation, "pr_url": pr_url},
        )
    return changed


# ---------------------------------------------------------------------------
# Human-review backpressure facts (I15).
#
# The driver is a READER here. Occupancy, the effective policy and the remote
# observation are the E5/I14 capacity facts; this module consumes them and
# invents nothing. It takes no slot, holds no reservation, and keeps no count
# of its own — a second count would be a second scheduler, and the two would
# disagree exactly when it mattered.
#
# Before broker promotion this is honest backpressure over the obligations
# this host can actually see (its own holds, plus the pull requests the forge
# reported). It is NOT a claim that every legacy agent publication path has
# been removed: a publication kew never brokered is visible here only insofar
# as the delivery repository reports its pull request.
# ---------------------------------------------------------------------------

#: Local review rows are read for DETAIL only (how many, how old, how big) —
#: never for the number the cap is compared against. Keep that read bounded so
#: a long-lived publication history cannot turn one tick into a full-table
#: fan-out; the authoritative occupancy is a single indexed count either way.
_REVIEW_DETAIL_SCAN_LIMIT = 200


# ---------------------------------------------------------------------------
# I12: the durable no-change projection one tick reads.
#
# A zero-diff outcome writes nothing to the provider, so provider state is
# never evidence that one landed. These facts are the local truth the overlay
# and the frontier consume instead: which judgment owns the item, whether
# every authority ACKNOWLEDGED the synchronization, and which dependents hold
# an acknowledged dependency effect.
# ---------------------------------------------------------------------------

#: States in which a dependent may still be owed a durable effect intent.
_EFFECT_ENQUEUEABLE_STATES = frozenset({AUTHORIZED, SYNC_PENDING, EFFECT_PENDING})

#: States in which an acknowledged effect still means what it said. A rejected
#: judgment revokes the outcome, so an effect it already wrote stops granting.
_EFFECT_HONOURING_STATES = frozenset({EFFECT_PENDING, COMPLETED})


@dataclass(frozen=True)
class NoChangeFacts:
    """One item's no-change judgment, as the graph driver is allowed to read it."""

    item_id: str
    judgment_id: str
    generation: int
    state: str
    #: True only when at least one sync job exists and EVERY one of them is
    #: ``acked``. A job that is merely observed (a warning, a conflicting
    #: remote state) is a look, not an acknowledgement — and a settled-but-not
    #: -acked job is not one either.
    sync_acknowledged: bool
    effects: tuple[DependencyEffect, ...]

    @property
    def live(self) -> bool:
        """True while this judgment still owns the item's outcome."""
        return self.state not in NO_CHANGE_TERMINAL_STATES

    @property
    def acked_dependents(self) -> frozenset[str]:
        """The dependents this outcome has explicitly, durably satisfied."""
        if self.state not in _EFFECT_HONOURING_STATES:
            return frozenset()
        return frozenset(
            effect.dependent_id
            for effect in self.effects
            if effect.state == EFFECT_ACKED
        )

    @property
    def may_enqueue_effects(self) -> bool:
        """Whether the driver may still record this outcome's effect intents."""
        return self.state in _EFFECT_ENQUEUEABLE_STATES

    @property
    def may_apply_effect(self) -> bool:
        """Whether one more dependent may be advanced right now.

        Both halves are required: the source authorities have acknowledged the
        outcome, and the judgment is the one state that owes dependents an
        effect. Neither a provider CLOSED nor a local authorization gets here.
        """
        return self.state == EFFECT_PENDING and self.sync_acknowledged


def _supersedes(candidate: NoChangeJudgment, current: NoChangeFacts) -> bool:
    """A live judgment outranks a settled one; otherwise the newer generation."""
    candidate_live = candidate.state not in NO_CHANGE_TERMINAL_STATES
    if candidate_live != current.live:
        return candidate_live
    return candidate.generation >= current.generation


def _no_change_fact(
    store: Any, judgment: NoChangeJudgment, item_id: str
) -> NoChangeFacts:
    jobs = store.list_outcome_sync_jobs(judgment.judgment_id)
    return NoChangeFacts(
        item_id=item_id,
        judgment_id=judgment.judgment_id,
        generation=judgment.generation,
        state=judgment.state,
        sync_acknowledged=bool(jobs) and all(j.state == SYNC_ACKED for j in jobs),
        effects=tuple(store.list_dependency_effects(judgment.judgment_id)),
    )


def read_no_change_facts(store: Any, provider: str) -> dict[str, NoChangeFacts]:
    """The no-change projection per item id, for every lineage of *provider*.

    Keyed the way :class:`kew.no_change.NoChangeIntent` derives its lineage —
    ``<provider>:<item id>`` — so a judgment written by a dispatch on this host
    is found by the driver that later reads the item, across restarts. One read
    per tick rather than one per node, bounded by how often a run has ever
    exited with a zero diff: a rare event, and one row each.

    Settled judgments are read too, not filtered out: an acknowledged effect
    outlives the judgment that completed, and a dependent that was explicitly
    satisfied must stay satisfied across a restart.
    """
    lister = getattr(store, "list_no_change_judgments", None)
    if not callable(lister):
        return {}
    prefix = f"{provider}:"
    facts: dict[str, NoChangeFacts] = {}
    for judgment in lister():
        if not judgment.lineage_key.startswith(prefix):
            continue
        item_id = judgment.lineage_key.removeprefix(prefix)
        current = facts.get(item_id)
        if current is None or _supersedes(judgment, current):
            facts[item_id] = _no_change_fact(store, judgment, item_id)
    return facts


@dataclass(frozen=True)
class _LocalHolds:
    """The bounded local-hold detail behind a pressure reading."""

    count: int = 0
    oldest_age_seconds: float | None = None
    diff_lines: int = 0


@dataclass(frozen=True)
class ReviewPressure:
    """What one delivery repository is holding in front of a person.

    ``engaged`` is False when this host has never reconciled the repository:
    there is no E5 observation, so there are no normalized obligations to be
    honest about and the driver invents no pressure from silence.

    ``countable`` is False when an observation EXISTS but may not be counted
    from — unavailable, stale, unreadable, or another repository's. That is
    not zero and never becomes zero: a forge nobody could read is the one case
    where the queue is most likely full, so it brakes.

    ``occupancy`` / ``cap`` / ``policy_generation`` are the authoritative I14
    numbers. ``held`` / ``oldest_hold_age_seconds`` / ``diff_burden_lines``
    are bounded local detail that explains the number to an operator and is
    never compared against the cap.
    """

    delivery_repo: str
    engaged: bool = False
    countable: bool = False
    occupancy: int = 0
    cap: int = 0
    policy_generation: int = 0
    observation_generation: int | None = None
    held: int = 0
    oldest_hold_age_seconds: float | None = None
    diff_burden_lines: int = 0
    detail: str = ""

    @property
    def saturated(self) -> bool:
        """Whether fresh independent work must wait for a person."""
        if not self.engaged:
            return False
        return not self.countable or self.occupancy >= self.cap

    def reason(self) -> str:
        """The operator-facing line, or "" when nothing is being held back."""
        if not self.saturated:
            return ""
        if not self.countable:
            return (
                f"{REVIEW_BACKPRESSURE_PREFIX}: {self.delivery_repo} review capacity is"
                f" unknown ({self.detail}); an unreadable delivery repository"
                " is not an empty one"
            )
        return (
            f"{REVIEW_BACKPRESSURE_PREFIX}: {self.delivery_repo} is holding"
            f" {self.occupancy} of {self.cap} human review(s)"
            f" ({self._burden()}); no existing review was evicted"
        )

    def _burden(self) -> str:
        parts = [f"{self.held} local hold(s)"]
        if self.oldest_hold_age_seconds is not None:
            parts.append(f"oldest {self.oldest_hold_age_seconds / 3600:.1f}h")
        parts.append(f"{self.diff_burden_lines} diff line(s) waiting")
        return ", ".join(parts)


def _persisted_review_observation(
    store: Any, delivery_repo: str, *, now: datetime
) -> RemoteReviewObservation | None:
    """The last observation of *delivery_repo*, or None when E5 is not engaged.

    A store with no capacity reads at all, and a driver whose epic has no
    canonical GitHub delivery repository, both mean "no obligations to read".
    A persisted-but-unreadable observation is the opposite: it is engaged and
    blind, so it is reported as unavailable rather than dropped.
    """
    reader = getattr(store, "review_observation", None)
    if not callable(reader):
        return None
    try:
        observation: RemoteReviewObservation | None = reader(delivery_repo)
        return observation
    except HumanReviewError:
        return None
    except Exception as exc:  # noqa: BLE001
        # Engaged but blind. A capacity error (PublicationJobError) is the
        # expected unreadable case, but the underlying capacity reads run raw
        # SQL without the reservation path's busy-retry, so a transient
        # sqlite3.OperationalError (or any other unexpected store failure) can
        # surface here too. Both must brake, never be dropped as "not engaged"
        # and never crash the tick: "unknown -> assume saturated" is total.
        return unavailable_review_observation(
            delivery_repo=delivery_repo,
            generation=0,
            observed_at=now.isoformat(),
            detail=f"the persisted review observation is unreadable: {exc}",
        )


def _uncountable_reason(
    observation: RemoteReviewObservation,
    *,
    delivery_repo: str,
    now: datetime,
    max_age_seconds: float,
) -> str:
    """Why this observation may not be counted from, or "" when it may.

    Delegates to the capacity transaction's own rule rather than restating it.
    The driver reconciles nothing, so it has no generation of its own to
    demand: it reports the generation it read, and asks only the questions a
    reader can answer — is this repository's observation available, valid, and
    recent enough to still describe the forge?
    """
    try:
        require_countable_observation(
            observation,
            delivery_repo=delivery_repo,
            expected_generation=observation.generation,
            now=now,
            max_age_seconds=max_age_seconds,
        )
    except PublicationJobError as exc:
        return str(exc)
    return ""


def _effective_review_policy(
    store: Any, system_policy: ReviewPolicy, now: datetime
) -> ReviewPolicy:
    """The policy in force, with an expired override already resolved away."""
    resolver = getattr(store, "effective_human_review_policy", None)
    if not callable(resolver):
        return system_policy
    resolved: ReviewPolicy = resolver(system=system_policy, now=now)
    return resolved


def _hold_age_seconds(review: PublicationReview, now: datetime) -> float | None:
    """How long this hold has been in front of a person, or None if unknown."""
    if not review.reserved_at:
        return None
    try:
        reserved = datetime.fromisoformat(review.reserved_at)
    except ValueError:
        return None
    if reserved.tzinfo is None:
        return None
    return max(0.0, (now - reserved).total_seconds())


def _held_reviews(store: Any, delivery_repo: str) -> list[PublicationReview]:
    """This repository's live holds, from a bounded read of recent jobs."""
    lister = getattr(store, "list_publication_jobs", None)
    reader = getattr(store, "publication_review", None)
    if not (callable(lister) and callable(reader)):
        return []
    recent = list(lister())[-_REVIEW_DETAIL_SCAN_LIMIT:]
    reviews = [reader(job.job_id) for job in recent]
    return [
        review
        for review in reviews
        if review is not None and _is_live_hold(review, delivery_repo)
    ]


def _is_live_hold(review: PublicationReview, delivery_repo: str) -> bool:
    """Whether this row is a slot THIS repository still owes a person."""
    return review.delivery_repo == delivery_repo and review.occupies_slot


def _local_review_holds(
    store: Any, delivery_repo: str, *, now: datetime
) -> _LocalHolds:
    """The bounded detail behind the count: how many, how old, how much diff."""
    held = _held_reviews(store, delivery_repo)
    ages = [age for age in (_hold_age_seconds(r, now) for r in held) if age is not None]
    return _LocalHolds(
        count=len(held),
        oldest_age_seconds=max(ages) if ages else None,
        diff_lines=sum(
            (r.risk_added_lines or 0) + (r.risk_deleted_lines or 0) for r in held
        ),
    )


def read_review_pressure(
    store: Any,
    *,
    delivery_repo: str,
    system_policy: ReviewPolicy,
    now: datetime,
    max_observation_age_seconds: float = DEFAULT_OBSERVATION_MAX_AGE_SECONDS,
) -> ReviewPressure:
    """Read the authoritative human-review pressure on one delivery repository.

    Every number here is read, never derived a second time: the effective
    policy resolves its own expiry, and the occupancy is the same lineage
    union the reservation transaction counts, so the driver cannot brake on a
    number the capacity transaction would disagree with.
    """
    observation = _persisted_review_observation(store, delivery_repo, now=now)
    if observation is None:
        return ReviewPressure(delivery_repo=delivery_repo)
    try:
        policy = _effective_review_policy(store, system_policy, now)
        holds = _local_review_holds(store, delivery_repo, now=now)
        pressure = ReviewPressure(
            delivery_repo=delivery_repo,
            engaged=True,
            cap=policy.max_pending_reviews,
            policy_generation=policy.generation,
            observation_generation=observation.generation,
            held=holds.count,
            oldest_hold_age_seconds=holds.oldest_age_seconds,
            diff_burden_lines=holds.diff_lines,
        )
        uncountable = _uncountable_reason(
            observation,
            delivery_repo=delivery_repo,
            now=now,
            max_age_seconds=max_observation_age_seconds,
        )
        if uncountable:
            return replace(pressure, detail=uncountable)
        return replace(
            pressure,
            countable=True,
            occupancy=store.review_capacity_occupancy(
                delivery_repo=delivery_repo, observation=observation
            ),
        )
    except Exception as exc:  # noqa: BLE001
        # We are engaged (an observation exists) but a downstream read blew up
        # unexpectedly: the policy resolver, the hold scan, or the occupancy
        # read. Fail safe to a braking pressure rather than crashing the tick
        # or, worse, falling through as countable with a zero occupancy.
        return ReviewPressure(
            delivery_repo=delivery_repo,
            engaged=True,
            observation_generation=observation.generation,
            detail=f"the review pressure is unreadable: {exc}",
        )


def _connection_nodes(connection: Any) -> list[dict[str, Any]]:
    """The dict ``nodes`` of one GraphQL connection; [] for anything else."""
    nodes = connection.get("nodes") if isinstance(connection, dict) else None
    return [node for node in nodes or [] if isinstance(node, dict)]


def _query_cost(data: dict[str, Any]) -> int:
    """A query's own ``rateLimit.cost``; GitHub's 1-point floor if absent."""
    rate_limit = data.get("rateLimit")
    cost = rate_limit.get("cost") if isinstance(rate_limit, dict) else None
    return cost if isinstance(cost, int) else 1


def _flatten_batch_pr(node: Any) -> dict[str, Any]:
    """One batch PR node in the shape ``gh pr view --json`` prints."""
    if not isinstance(node, dict):
        return {}
    pr = dict(node)
    pr["commits"] = [
        entry["commit"]
        for entry in _connection_nodes(node.get("commits"))
        if isinstance(entry.get("commit"), dict)
    ]
    pr["latestReviews"] = _connection_nodes(node.get("latestReviews"))
    return pr


def _pr_batch_command(
    repo: str, branches: list[str], numbers: list[int], probes: list[str]
) -> list[str]:
    """The one ``gh api graphql`` call asking about every live node at once.

    Branch PRs alias as ``b<i>``, PR numbers as ``p<i>``, branch-existence
    probes as ``r<i>``. Every value travels as a typed variable, never spliced
    into the query text.
    """
    owner, _, name = repo.partition("/")
    declarations = ["$owner: String!", "$name: String!"]
    selections: list[str] = []
    args = ["-f", f"owner={owner}", "-f", f"name={name}"]
    for index, branch in enumerate(branches):
        declarations.append(f"$b{index}: String!")
        selections.append(
            f"b{index}: pullRequests(headRefName: $b{index}, "
            f"first: {_BRANCH_PR_PAGE}, "
            "orderBy: {field: CREATED_AT, direction: DESC}) "
            "{ nodes { ...LinkPr } }"
        )
        args += ["-f", f"b{index}={branch}"]
    for index, number in enumerate(numbers):
        declarations.append(f"$p{index}: Int!")
        selections.append(f"p{index}: pullRequest(number: $p{index}) {{ ...LinkPr }}")
        args += ["-F", f"p{index}={number}"]
    for index, branch in enumerate(probes):
        declarations.append(f"$r{index}: String!")
        selections.append(f"r{index}: ref(qualifiedName: $r{index}) {{ name }}")
        args += ["-f", f"r{index}=refs/heads/{branch}"]
    query = (
        f"query({', '.join(declarations)}) {{\n"
        "  rateLimit { cost }\n"
        "  repository(owner: $owner, name: $name) {\n    "
        + "\n    ".join(selections)
        + "\n  }\n}\n"
        + (_BATCH_PR_SELECTION if branches or numbers else "")
    )
    return [
        "gh",
        "api",
        "graphql",
        "-H",
        _MERGE_INFO_PREVIEW,
        "-f",
        f"query={query}",
        *args,
    ]


class _BatchOnly:
    """The reader with its network reads removed, for a node with nothing to
    look up (#2582): candidates resolve from this tick's batch and from what
    they carry — a closing reference's own state — and never from GitHub."""

    def __init__(self, reader: GitHubFactsReader) -> None:
        self._reader = reader
        self._logged_pr_disagreements = reader._logged_pr_disagreements

    def pr_facts(self, pr_number: int) -> dict[str, Any] | None:
        return self._reader._publication_pr_by_number.get(pr_number)

    def pr_candidates_for_branch(self, branch: str) -> list[dict[str, Any]]:
        return list(self._reader._publication_pr_by_branch.get(branch, []))

    def pr_number_from_url(self, url: str) -> int | None:
        return self._reader.pr_number_from_url(url)


class GitHubFactsReader:
    """Reads richer per-node GitHub facts for the Phase 2 driver."""

    def __init__(
        self,
        repo: str,
        *,
        store: Any | None = None,
        run: RunFn = _default_run,
        branch_prefix: str = _DEFAULT_BRANCH_PREFIX,
        host: str | None = None,
    ) -> None:
        if host is None:
            host = repo_host()
        self.repo = repo
        self._store = store
        self.run = run
        self._branch_prefix = branch_prefix
        self.host = host
        self._publication_pr_by_branch: dict[str, list[dict[str, Any]]] = {}
        self._publication_pr_by_number: dict[int, dict[str, Any]] = {}
        self._branch_by_issue: dict[int, str] = {}
        self._branch_fallback_attempted: set[str] = set()
        self._batch_succeeded: bool | None = None
        self._logged_pr_disagreements: set[tuple[str, tuple[int, ...]]] = set()
        self._prepared_issues: dict[int, dict[str, Any]] = {}
        self._queried_branches: set[str] = set()
        self._unresolved_numbers: set[int] = set()
        #: GraphQL points this reader spent since the last ``prepare`` — the
        #: batch query's own ``rateLimit.cost``, plus GitHub's 1-point floor
        #: for each per-node ``gh`` read it still had to make (#2582).
        self.graphql_points = 0

    def prime_issue_snapshots(self, snapshots: Mapping[int, dict[str, Any]]) -> None:
        """Seed this tick's issue reads from the epic's one native snapshot.

        The GitHub-native adapter already read number/state/stateReason/labels/
        closing references for every node in one GraphQL query; handing that
        payload here removes the per-node ``gh issue view``. Each entry is
        consumed by the first read of that issue (the Linear reader's
        ``_prepared_issues`` contract), so any *re-read* — notably the merge
        pass verifying its own ``gh issue close`` — still goes to GitHub and
        can never be answered from a snapshot kew has since invalidated.
        """
        self._prepared_issues = {int(num): dict(raw) for num, raw in snapshots.items()}

    def prepare(self, item_ids: list[str]) -> None:
        """Read this tick's PR linkage facts for every node in ONE query (#2582).

        A done or cancelled node whose snapshot already states every candidate
        PR is not asked about at all. A never-started node — no run, no closing
        reference — is asked only whether its branch exists; only a branch that
        does (work begun elsewhere) costs a second query for its PRs. Everything
        else is asked by branch and by every PR number the ledger or a closing
        reference names. A failed batch falls back to bounded per-node reads.
        """
        self._publication_pr_by_branch.clear()
        self._publication_pr_by_number.clear()
        self._branch_by_issue.clear()
        self._branch_fallback_attempted.clear()
        self._queried_branches.clear()
        self._unresolved_numbers.clear()
        self._batch_succeeded = None
        self.graphql_points = 0
        self._branch_by_issue.update(
            {
                int(item_id): f"{self._branch_prefix}{branch_slug(item_id)}"
                for item_id in item_ids
                if item_id.isdigit()
            }
        )
        live: dict[int, str] = {}
        probes: list[str] = []
        for issue_num, branch in self._branch_by_issue.items():
            issue = self._prepared_issues.get(issue_num)
            if self._settled_closed(issue_num, issue):
                continue
            if self._unstarted(issue_num, issue):
                probes.append(branch)
            else:
                live[issue_num] = branch
        numbers = sorted(
            {
                number
                for issue_num, branch in live.items()
                for number in self._known_pr_numbers(issue_num, branch)
            }
        )
        self._batch_succeeded = True
        begun = self._fetch_pr_batch(sorted(set(live.values())), numbers, probes)
        if begun:
            self._fetch_pr_batch(begun, [], [])

    def _ledger_pr_numbers(self, issue_num: int, branch: str) -> set[int]:
        runs = _implementation_pr_runs(self._store, str(issue_num), branch)
        return {
            number
            for number in (self.pr_number_from_url(run.pr_url) for run in runs)
            if number is not None
        }

    def _known_pr_numbers(self, issue_num: int, branch: str) -> set[int]:
        """PR numbers the ledger and the primed snapshot already name."""
        numbers = self._ledger_pr_numbers(issue_num, branch)
        issue = self._prepared_issues.get(issue_num) or {}
        numbers.update(
            ref["number"]
            for ref in self._closing_refs(issue)
            if isinstance(ref.get("number"), int)
        )
        return numbers

    def _settled_closed(self, issue_num: int, issue: Mapping[str, Any] | None) -> bool:
        """A closed issue whose payload already states every candidate PR.

        Done or cancelled: nothing but kew acting changes it, and every PR the
        ledger or a closing reference names arrived with its state, so there is
        nothing to look up. A closed issue whose ledger names a PR the payload
        does not state is still asked about, so a landed node is never read as
        an unknown PR.
        """
        if issue is None or issue.get("state") != "CLOSED":
            return False
        refs = self._closing_refs(dict(issue))
        stated = {
            ref["number"]
            for ref in refs
            if isinstance(ref.get("number"), int)
            and str(ref.get("state", "")).upper() in _PR_STATE_RANK
        }
        if len(stated) != len(refs):
            return False
        branch = self._branch_by_issue.get(
            issue_num, f"{self._branch_prefix}{branch_slug(str(issue_num))}"
        )
        return self._ledger_pr_numbers(issue_num, branch).issubset(stated)

    def _unstarted(self, issue_num: int, issue: Mapping[str, Any] | None) -> bool:
        """No run in the store and no closing reference: only a branch pushed
        from somewhere else could give this node a PR. ``issue`` is None when
        no snapshot was primed; its closing references are read with it."""
        if issue is not None and (
            issue.get("state") == "CLOSED" or self._closing_refs(dict(issue))
        ):
            return False
        list_by_item = getattr(self._store, "list_by_item", None)
        return callable(list_by_item) and not list(list_by_item(str(issue_num)))

    def _fetch_pr_batch(
        self, branches: list[str], numbers: list[int], probes: list[str]
    ) -> list[str]:
        """Run one batch query; return the probed branches that exist.

        A failure marks the batch failed so every read this tick falls back to
        its own bounded per-node lookup.
        """
        if not branches and not numbers and not probes:
            return []
        repository = self._run_pr_batch(
            _pr_batch_command(self.repo, branches, numbers, probes)
        )
        if repository is None:
            self._batch_succeeded = False
            return []
        self._ingest_pr_batch(repository, branches, numbers)
        begun = [
            branch
            for index, branch in enumerate(probes)
            if isinstance(repository.get(f"r{index}"), dict)
        ]
        # A probed branch that does not exist has no PR to find: as answered
        # as a branch the batch read and found empty.
        self._queried_branches.update(set(probes) - set(begun))
        return begun

    def _ingest_pr_batch(
        self, repository: dict[str, Any], branches: list[str], numbers: list[int]
    ) -> None:
        self._queried_branches.update(branches)
        for index, branch in enumerate(branches):
            payload = [
                _flatten_batch_pr(node)
                for node in _connection_nodes(repository.get(f"b{index}"))
            ]
            self._ingest_publication_prs(payload, {branch})
        for index, number in enumerate(numbers):
            pr = _flatten_batch_pr(repository.get(f"p{index}"))
            if pr.get("number") == number:
                self._publication_pr_by_number[number] = pr
            else:
                # Asked and not found: re-asking per node would say the same.
                self._unresolved_numbers.add(number)

    def _run_pr_batch(self, command: list[str]) -> dict[str, Any] | None:
        """Run the batch query; the ``repository`` object, or None if unusable.

        ``gh api graphql`` exits non-zero when ANY alias errors — a ledger URL
        naming a deleted PR, say — while still printing the data it resolved,
        so the payload, not the exit code, decides.
        """
        try:
            result = self.run(command, check=False)
        except Exception:
            # A transport failure is a failed batch, never an empty one.
            _logger.warning("PR facts batch failed to run", exc_info=True)
            return None
        payload = _parse_json_object(getattr(result, "stdout", None))
        data = payload.get("data")
        if not isinstance(data, dict):
            return None
        self.graphql_points += _query_cost(data)
        repository = data.get("repository")
        return repository if isinstance(repository, dict) else None

    def _ingest_publication_prs(self, payload: list[Any], branches: set[str]) -> None:
        for candidate in payload:
            if not isinstance(candidate, dict):
                continue
            branch = candidate.get("headRefName")
            number = candidate.get("number")
            if (
                not isinstance(branch, str)
                or branch not in branches
                or not isinstance(number, int)
            ):
                continue
            self._publication_pr_by_number[number] = candidate
            self._publication_pr_by_branch.setdefault(branch, []).append(candidate)

    def _fetch_branch_prs(self, branch: str) -> list[Any]:
        """Read at most five branch PRs when the tick's batch missed them."""
        self.graphql_points += 1
        result = self.run(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                self.repo,
                "--head",
                branch,
                "--state",
                "all",
                "--limit",
                "5",
                "--json",
                _LINK_PR_FIELDS,
            ],
            check=False,
        )
        if getattr(result, "returncode", 1) != 0:
            return []
        try:
            payload = json.loads(getattr(result, "stdout", "") or "[]")
        except (json.JSONDecodeError, TypeError):
            return []
        return payload if isinstance(payload, list) else []

    def pr_candidates_for_branch(self, branch: str) -> list[dict[str, Any]]:
        """Return broad candidates, with fallback after a missed batch only."""
        prepared = self._publication_pr_by_branch.get(branch)
        if prepared:
            return list(prepared)
        batch_missed = self._batch_succeeded is False or (
            self._batch_succeeded is True and branch not in self._queried_branches
        )
        if not batch_missed:
            return []
        if branch not in self._branch_fallback_attempted:
            self._branch_fallback_attempted.add(branch)
            self._ingest_publication_prs(self._fetch_branch_prs(branch), {branch})
        return list(self._publication_pr_by_branch.get(branch, []))

    def _reconcile_issue_publication(
        self,
        issue_num: int,
        issue: dict[str, Any],
        pr_number: int | None,
        pr_url: str | None,
        pr: dict[str, Any],
    ) -> None:
        if self._store is None:
            return
        completed = (
            issue.get("state") == "CLOSED" and issue.get("stateReason") == "COMPLETED"
        )
        pr_state = str(pr.get("state", "")).upper()
        if not completed and (pr_number is None or pr_url is None):
            return
        if not completed and pr_state not in {"OPEN", "MERGED"}:
            return
        reconcile_awaiting_publication(
            self._store,
            item_id=str(issue_num),
            repo=self.repo,
            pr_url=pr_url,
            observation=(
                "provider_completed" if completed else f"pr_{pr_state.lower()}"
            ),
        )

    def issue_facts(self, issue_num: int) -> NodeFacts:
        """Return NodeFacts for one issue, enriched from its linked PR (if any).

        Non-int ids are the caller's job to guard against (Task 10); this
        method takes a plain int and does not attempt to validate it further.
        """
        issue = self._fetch_issue(issue_num)
        labels = self._labels_of(issue)
        branch = self._branch_by_issue.get(
            issue_num, f"{self._branch_prefix}{branch_slug(str(issue_num))}"
        )
        link = resolve_linked_pr(
            str(issue_num),
            branch,
            store=self._store,
            batch=(
                _BatchOnly(self) if self._settled_closed(issue_num, issue) else self
            ),
            closing_refs=self._closing_refs(issue),
        )
        pr = link.pr
        if (
            issue.get("state") == "CLOSED"
            and pr.get("state") == "OPEN"
            and link.number is not None
        ):
            _logger.warning(
                "#%s: orphaned-pr #%s is open after the issue closed",
                issue_num,
                link.number,
            )
        self._reconcile_issue_publication(issue_num, issue, link.number, link.url, pr)

        return NodeFacts(
            issue_num=issue_num,
            state=str(issue.get("state", "")),
            state_reason=issue.get("stateReason"),
            labels=labels,
            pr_number=link.number,
            pr_url=link.url,
            review_decision=pr.get("reviewDecision") or None,
            merge_state_status=pr.get("mergeStateStatus") or None,
            head_oid=pr.get("headRefOid") or None,
            pr_pushed_at=self._last_commit_date(pr),
            pr_state=str(pr["state"]) if link.number is not None else None,
            pr_link_source=link.source,
            # Only a linked PR can carry a closing reference; no PR → not a fact.
            pr_has_closing_ref=(
                link.has_closing_ref if link.number is not None else None
            ),
        )

    def pr_facts(self, pr_number: int) -> dict[str, Any]:
        """Return the raw parsed `gh pr view --json ...` fields for one PR.

        Answered from this tick's `prepare` batch when it covered the PR.
        Returns {} on any gh failure or unparseable response.
        """
        prepared = self._publication_pr_by_number.get(pr_number)
        if prepared is not None:
            return prepared
        if pr_number in self._unresolved_numbers:
            # The batch asked and GitHub has no such PR: re-asking says the same.
            return {}
        return self.fresh_pr_facts(pr_number)

    def fresh_pr_facts(self, pr_number: int) -> dict[str, Any]:
        """Read one PR from GitHub NOW, never from this tick's `prepare` batch.

        The batch is taken once per tick, so it cannot show a push that landed
        mid-tick. The serial merge queue (#2148) needs exactly that: its
        post-verification head check must see a head that moved *during* a
        verification the same tick started and which can run for many minutes —
        through ``pr_facts`` it would re-read the batch's own head and always
        agree with the classification it is meant to re-check.

        Returns {} on any gh failure or unparseable response, which callers must
        treat as "unreadable", never as "unchanged".
        """
        self.graphql_points += 1
        result = self.run(
            [
                "gh",
                "pr",
                "view",
                str(pr_number),
                "--repo",
                self.repo,
                "--json",
                _FRESH_PR_FIELDS,
            ],
            check=False,
        )
        if result.returncode != 0:
            return {}
        return _parse_json_object(result.stdout)

    def pr_facts_for_urls(
        self, pr_urls: Iterable[str]
    ) -> dict[str, dict[str, Any]] | None:
        """Resolve persisted PR URLs through bounded GitHub REST reads.

        ``gh pr list`` is an active-listing surface and can omit an older
        merged PR.  A persisted URL gives us the stable REST resource instead:
        ``GET /repos/{owner}/{repo}/pulls/{number}`` still resolves after the
        PR has left the open/recent listing.

        The complete batch fails closed when a URL is malformed, targets a
        different repository, GitHub is unavailable, or a response has no
        usable state.  Callers must not infer ``MERGED`` from a partial batch.
        An empty input is a successful empty resolution and performs no call.
        """
        unique_urls = list(dict.fromkeys(pr_urls))
        if not unique_urls:
            return {}
        if len(unique_urls) > _PERSISTED_PR_BATCH_LIMIT:
            return None

        refs: list[tuple[str, int]] = []
        for url in unique_urls:
            number = self._persisted_pr_number(url)
            if number is None:
                return None
            refs.append((url, number))

        resolved: dict[str, dict[str, Any]] = {}
        for url, number in refs:
            payload = self._persisted_pr_fact(url, number)
            if payload is None:
                return None
            resolved[url] = payload
        return resolved

    def _persisted_pr_fact(self, url: str, number: int) -> dict[str, Any] | None:
        """Read and normalize one persisted PR REST fact."""
        try:
            result = self.run(
                [
                    "gh",
                    "api",
                    "--method",
                    "GET",
                    f"repos/{self.repo}/pulls/{number}",
                ],
                check=False,
            )
        except Exception:
            # A REST transport failure is an unknown fact, never evidence
            # that a PR is gone or merged.
            return None
        if getattr(result, "returncode", 1) != 0:
            return None
        payload = _parse_json_object(getattr(result, "stdout", None))
        state = payload.get("state")
        if not isinstance(state, str):
            return None
        if payload.get("merged_at"):
            state = "MERGED"
        else:
            state = state.upper()
            if state not in {"OPEN", "CLOSED", "MERGED"}:
                return None
        payload = dict(payload)
        payload["state"] = state
        payload.setdefault("number", number)
        payload.setdefault("url", url)
        return payload

    def pr_number_from_url(self, url: str) -> int | None:
        """Return a PR number only for a canonical URL in this repository."""
        if not isinstance(url, str):
            return None
        try:
            parsed = urlsplit(url)
        except ValueError:
            return None
        if not _is_canonical_pr_path(parsed, self.repo, self.host):
            return None
        prefix = f"/{self.repo}/pull/"
        if not parsed.path.startswith(prefix):
            return None
        tail = parsed.path[len(prefix) :]
        if not tail.isdigit() or "/" in tail:
            return None
        number = int(tail)
        return number if number > 0 else None

    def _persisted_pr_number(self, url: str) -> int | None:
        """Backward-compatible alias for persisted-facts callers."""
        return self.pr_number_from_url(url)

    def repository_permission(self, login: str) -> RepositoryPermission | None:
        """Read current collaborator authority, failing closed on bad evidence."""
        result = self.run(
            ["gh", "api", f"repos/{self.repo}/collaborators/{login}/permission"],
            check=False,
        )
        if result.returncode != 0:
            return None
        payload = _parse_json_object(result.stdout)
        user = payload.get("user")
        permission = payload.get("permission")
        if not isinstance(user, dict):
            return None
        returned_login = user.get("login")
        actor_type = user.get("type")
        if (
            returned_login != login
            or actor_type != "User"
            or not isinstance(permission, str)
            or not permission
        ):
            return None
        return RepositoryPermission(returned_login, actor_type, permission)

    def required_checks_green(self, base: str, pr: dict[str, Any]) -> bool:
        """Return True iff the PR's merge state indicates a clean merge.

        The CLEAN/BLOCKED decision is driven entirely by GitHub's own
        computed `mergeStateStatus`: CLEAN and HAS_HOOKS are treated as
        green; BLOCKED/BEHIND/DIRTY/UNSTABLE/UNKNOWN are not.

        `base` is currently unused: `mergeStateStatus` is already
        rulesets- and classic-branch-protection-aware, so a separate
        `gh api repos/{repo}/rules/branches/{base}` fetch is redundant.
        Kept in the signature for interface stability and a possible
        future ruleset-aware enhancement.
        """
        return pr.get("mergeStateStatus") in _GREEN_MERGE_STATES

    def head_checks(
        self, sha: str, gated_checks: Sequence[str]
    ) -> tuple[CheckState, str]:
        """What *sha*'s own check runs say, decided by :mod:`kew.ci_checks`
        with ``ci.approval_gated_checks`` (*gated_checks*).

        ``mergeStateStatus`` counts a skipped required check as passing, and
        private-repository CI skips an agent PR's self-hosted tests until a
        human adds ``ci-approved`` (#2033). The merge gate reads this too. An
        unreadable answer is ``pending``: never green.
        """
        result = self.run(check_runs_command(self.repo, sha), check=False)
        if result.returncode != 0:
            detail = str(getattr(result, "stderr", "") or "").strip()[:200]
            return "pending", f"check runs unreadable: {detail or 'gh failed'}"
        try:
            runs = parse_check_runs(result.stdout)
        except (ValueError, TypeError) as exc:
            return "pending", f"check runs unreadable: {exc}"
        if runs:
            return classify_check_runs(runs, gated_checks=gated_checks)
        # No check runs: a CI that posts only legacy commit statuses.
        status = self.run(commit_status_command(self.repo, sha), check=False)
        if status.returncode != 0:
            detail = str(getattr(status, "stderr", "") or "").strip()[:200]
            return "pending", f"commit status unreadable: {detail or 'gh failed'}"
        try:
            return classify_commit_status(parse_commit_status(status.stdout))
        except (ValueError, TypeError) as exc:
            return "pending", f"commit status unreadable: {exc}"

    def fetch_issue_or_none(self, issue_num: int) -> dict[str, Any] | None:
        """Raw issue read that returns ``None`` on a failed ``gh`` read.

        Unlike ``_fetch_issue`` (which folds a failure into an empty dict), this
        distinguishes a *failed* read (non-zero ``gh`` exit → ``None``) from a
        *successful* read of an issue that simply has no linked PR (a dict with
        no ``closedByPullRequestsReferences``). The review-race handler needs
        that distinction so a transient ``gh`` hiccup never looks like a
        vanished PR reference and supersedes a live reviewer (#980).

        A snapshot primed by the GitHub-native adapter answers the first read
        of an issue without a subprocess at all; it is popped as it is served,
        so a second read of the same issue in one tick is live.
        """
        prepared = self._prepared_issues.pop(issue_num, None)
        if prepared is not None:
            return prepared
        self.graphql_points += 1
        result = self.run(
            [
                "gh",
                "issue",
                "view",
                str(issue_num),
                "--repo",
                self.repo,
                "--json",
                "number,state,stateReason,labels,closedByPullRequestsReferences",
            ],
            check=False,
        )
        if result.returncode != 0:
            return None
        return _parse_json_object(result.stdout)

    def _fetch_issue(self, issue_num: int) -> dict[str, Any]:
        return self.fetch_issue_or_none(issue_num) or {}

    @staticmethod
    def _labels_of(issue: dict[str, Any]) -> frozenset[str]:
        return frozenset(
            lbl.get("name", "")
            for lbl in issue.get("labels", []) or []
            if isinstance(lbl, dict)
        )

    @staticmethod
    def linked_pr_ref(issue: dict[str, Any]) -> tuple[int, str | None] | None:
        refs = issue.get("closedByPullRequestsReferences") or []
        if not refs or not isinstance(refs[0], dict):
            return None
        number = refs[0].get("number")
        if number is None:
            return None
        return int(number), refs[0].get("url")

    @staticmethod
    def _closing_refs(issue: dict[str, Any]) -> list[dict[str, Any]]:
        refs = issue.get("closedByPullRequestsReferences") or []
        return [ref for ref in refs if isinstance(ref, dict)]

    @staticmethod
    def _last_commit_date(pr: dict[str, Any]) -> str | None:
        commits = pr.get("commits") or []
        if not commits or not isinstance(commits[-1], dict):
            return None
        date = commits[-1].get("committedDate")
        return date if isinstance(date, str) else None


def prime_issue_snapshots_from(deps: Any, facts: Any) -> bool:
    """Hand a dependency source's epic snapshot to the facts reader.

    The GitHub-native adapter publishes `issue_snapshots` from the one GraphQL
    query it already ran to build the graph. Wiring the two here keeps both the
    driver tick and `kew epic status` on one read per issue per tick without
    either knowing which adapter it holds. Returns whether a snapshot was
    handed over (False for the convention/Linear adapters, which have none).
    """
    snapshots = getattr(deps, "issue_snapshots", None)
    prime = getattr(facts, "prime_issue_snapshots", None)
    if not snapshots or not callable(prime):
        return False
    prime(snapshots)
    return True
