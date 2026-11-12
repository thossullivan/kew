"""The one human-review policy, and the artifact risk it judges (I13).

Before this module there were three different answers to "does a person have
to look at this?" — a diff-line threshold nothing read, a publication filter
that flagged executable paths, and an unbounded queue of work waiting on a
human. This module makes them one policy with one decision.

## What the policy owns

* **Capacity** — how many publications may sit awaiting a human at once.
  Occupancy is counted for one canonical GitHub delivery repository and never
  across repositories. A global count would let a busy repo starve a quiet one
  and would make "3 pending reviews" a fact about the host rather than about
  the thing being delivered, so :meth:`ReviewPolicy.validate` refuses a policy
  that does not name its delivery repository.
* **Risk thresholds** — changed lines (additions **plus** deletions, because a
  deletion is not the harmless direction) and distinct paths touched.
* **Terminal judgment** — who may release a hold.

## Four outcomes, and why they are not three

:data:`ALLOWED` and :data:`CONFIRMATION_REQUIRED` are the ordinary pair.
The other two differ in a way that matters to the caller:

* :data:`BLOCKED` means the delivery repository is at review capacity. Nothing
  is wrong with the artifact; there is nowhere to put the review. It clears on
  its own when a slot frees, and it evicts nothing that already holds one.
* :data:`REFUSED` means the publication filter rejected an entry. **No
  approver can override it.** A refusal is evidence about the agent that
  produced the whole artifact, not a threshold someone may raise, so there is
  deliberately no argument anywhere in this module that turns one into a
  confirmation.

## Confirmation is bound to the exact SHA

A person confirms *this tree, under this policy* — never "this branch". The
confirmation carries the base, the tree, the digest of the measurement and the
policy hash and generation it was given under, and
:func:`confirmation_covers` compares all five. Remediation that changes the
SHA therefore invalidates the prior confirmation by construction rather than
by anyone remembering to clear it.

Thresholds are **inclusive**: exactly ``max_diff_lines`` changed lines and
exactly ``max_paths`` paths pass unattended, one more stops for a human.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

__all__ = [
    "ALLOWED",
    "BLOCKED",
    "CONFIRMATION_REQUIRED",
    "REASON_BINARY",
    "REASON_CAPACITY",
    "REASON_DIFF_LINES",
    "REASON_PATHS",
    "REASON_REVIEW_FLAG",
    "REASON_UNMEASURABLE",
    "REFUSED",
    "REVERSION_EXPIRED",
    "REVIEW_CONFIRMED",
    "REVIEW_NOT_REQUIRED",
    "REVIEW_PENDING",
    "REVIEW_REFUSED",
    "REVIEW_RELEASED",
    "REVIEW_REQUIRED",
    "REVIEW_STATE_FOR_OUTCOME",
    "SYSTEM_MAX_DIFF_LINES",
    "SYSTEM_MAX_PATHS",
    "SYSTEM_MAX_PENDING_REVIEWS",
    "ArtifactRisk",
    "HumanReviewError",
    "PathChange",
    "PolicyResolution",
    "RefusalNotOverridableError",
    "ReviewAuthorityError",
    "ReviewCapacityError",
    "ReviewPolicy",
    "ReviewPolicyError",
    "RiskConfirmation",
    "RiskDecision",
    "authorize_approver",
    "confirm_risk",
    "confirmation_covers",
    "decide_publication",
    "measure_risk",
    "policy_from_config",
    "resolve_policy",
]

#: The defaults the product ships with. They are named rather than inlined so
#: that "the system value" an expired override reverts to has one definition.
SYSTEM_MAX_PENDING_REVIEWS = 3
SYSTEM_MAX_DIFF_LINES = 500
SYSTEM_MAX_PATHS = 20

ALLOWED = "allowed"
CONFIRMATION_REQUIRED = "confirmation_required"
BLOCKED = "blocked"
REFUSED = "refused"

REASON_DIFF_LINES = "diff_lines"
REASON_PATHS = "paths"
REASON_BINARY = "binary"
REASON_UNMEASURABLE = "unmeasurable"
REASON_REVIEW_FLAG = "review_flag"
REASON_CAPACITY = "capacity"
REASON_FILTER_REFUSAL = "filter_refusal"

#: Why an override stopped applying. Empty string means it still applies.
REVERSION_EXPIRED = "expired"

#: The review lifecycle of one publication job. ``REVIEW_PENDING`` is the only
#: state that occupies a capacity slot: a hold is what a person is holding, so
#: a job that has been confirmed, refused or released has already stopped
#: consuming the scarce thing.
REVIEW_NOT_REQUIRED = "not_required"
REVIEW_REQUIRED = "required"
REVIEW_PENDING = "pending"
REVIEW_CONFIRMED = "confirmed"
REVIEW_REFUSED = "refused"
REVIEW_RELEASED = "released"

#: What each decision outcome means for the job's persisted review state.
REVIEW_STATE_FOR_OUTCOME = {
    ALLOWED: REVIEW_NOT_REQUIRED,
    CONFIRMATION_REQUIRED: REVIEW_REQUIRED,
    BLOCKED: REVIEW_REQUIRED,
    REFUSED: REVIEW_REFUSED,
}

_REPO = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
_OID = re.compile(r"^[0-9a-f]{40}$|^[0-9a-f]{64}$")
_BOUNDS = ("max_pending_reviews", "max_diff_lines", "max_paths")


class HumanReviewError(RuntimeError):
    """Base class for every refusal this module raises."""


class ReviewPolicyError(HumanReviewError):
    """The policy, the override or the measurement is not usable as stated."""


class ReviewAuthorityError(HumanReviewError):
    """The actor may not give terminal judgement under this policy."""


class ReviewCapacityError(HumanReviewError):
    """The delivery repository has no review slot free."""


class RefusalNotOverridableError(HumanReviewError):
    """Someone tried to confirm a publication the filter refused."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ReviewPolicyError(message)


def _canonical(facts: dict[str, Any]) -> str:
    return json.dumps(facts, sort_keys=True, separators=(",", ":"))


def _digest(prefix: str, facts: dict[str, Any]) -> str:
    body = hashlib.sha256(_canonical(facts).encode("utf-8")).hexdigest()
    return f"{prefix}-{body[:32]}"


@dataclass(frozen=True)
class ReviewPolicy:
    """What the policy says, which edit said it, and until when.

    ``generation`` is monotonic and identifies *which* edit is in force;
    ``policy_hash`` covers only the values. They answer different questions —
    "is this the current edit?" and "did the numbers change?" — and a
    confirmation binds to both, so re-stating the same values under a new
    generation still invalidates a hold that a person granted earlier.
    """

    delivery_repo: str
    max_pending_reviews: int = SYSTEM_MAX_PENDING_REVIEWS
    max_diff_lines: int = SYSTEM_MAX_DIFF_LINES
    max_paths: int = SYSTEM_MAX_PATHS
    approvers: tuple[str, ...] = ()
    generation: int = 0
    expires_at: str | None = None

    def validate(self) -> None:
        """Refuse an unusable policy BEFORE anything is decided under it."""
        _require(
            bool(_REPO.match(self.delivery_repo)),
            f"{self.delivery_repo!r} is not a canonical GitHub delivery"
            " repository (owner/name) — review capacity has to belong to one",
        )
        for name in _BOUNDS:
            value = getattr(self, name)
            _require(
                isinstance(value, int) and not isinstance(value, bool) and value >= 0,
                f"{name} must be a non-negative integer, got {value!r}",
            )
        _require(
            isinstance(self.generation, int)
            and not isinstance(self.generation, bool)
            and self.generation >= 0,
            f"generation must be a non-negative integer, got {self.generation!r}",
        )
        if self.expires_at is not None:
            _parse_utc(self.expires_at)

    @property
    def facts(self) -> dict[str, Any]:
        """The values the hash covers — deliberately not generation or expiry."""
        return {
            "delivery_repo": self.delivery_repo,
            "max_pending_reviews": self.max_pending_reviews,
            "max_diff_lines": self.max_diff_lines,
            "max_paths": self.max_paths,
            "approvers": sorted(self.approvers),
        }

    @property
    def policy_hash(self) -> str:
        """A stable digest of the values in force."""
        return _digest("hrp", self.facts)

    def as_json(self) -> str:
        """The effective policy as one storable string, generation included."""
        return _canonical(
            {**self.facts, "generation": self.generation, "expires_at": self.expires_at}
        )


def policy_from_config(config: Any, *, generation: int = 0) -> ReviewPolicy:
    """The system policy a ``kew.toml`` states, bound to its delivery repo.

    ``[human_review].approvers`` falls back to ``[approval].approvers`` so a
    project that already named its approvers does not have to name them twice.
    """
    section = config.human_review
    approvers = tuple(section.approvers) or tuple(config.approval.approvers)
    policy = ReviewPolicy(
        delivery_repo=config.project.repo,
        max_pending_reviews=section.max_pending_reviews,
        max_diff_lines=section.max_diff_lines,
        max_paths=section.max_paths,
        approvers=approvers,
        generation=generation,
    )
    policy.validate()
    return policy


def _parse_utc(stamp: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(stamp)
    except ValueError as exc:
        raise ReviewPolicyError(f"{stamp!r} is not an ISO-8601 timestamp") from exc
    _require(
        parsed.tzinfo is not None,
        f"{stamp!r} has no timezone — a policy expiry must be explicit UTC",
    )
    return parsed.astimezone(UTC)


@dataclass(frozen=True)
class PolicyResolution:
    """The policy in force now, and whether an override stopped applying."""

    policy: ReviewPolicy
    reverted: bool = False
    reason: str = ""


def resolve_policy(
    *, system: ReviewPolicy, override: ReviewPolicy | None, now: datetime
) -> PolicyResolution:
    """Which policy is in force, resolving expiry into a system reversion.

    An expired override does not linger and does not silently keep applying:
    it resolves to the *system* values under a **new** generation, because
    reverting is itself a policy change and every hold granted under the
    override has to stop counting as current.
    """
    system.validate()
    if override is None:
        return PolicyResolution(system)
    override.validate()
    _require(
        override.delivery_repo == system.delivery_repo,
        "an override may not move the canonical delivery repository",
    )
    _require(
        override.generation > system.generation,
        f"override generation {override.generation} does not supersede"
        f" {system.generation}",
    )
    if override.expires_at is not None and _parse_utc(override.expires_at) <= now:
        reverted = replace(system, generation=override.generation + 1, expires_at=None)
        return PolicyResolution(reverted, reverted=True, reason=REVERSION_EXPIRED)
    return PolicyResolution(override)


@dataclass(frozen=True)
class PathChange:
    """One path the artifact touches, and how much of it the host could read.

    ``binary`` and ``measurable`` are separate facts. A known-binary blob is
    measurable in bytes but not in lines; an unreadable one is not measurable
    at all. Both stop for a human, and saying which is which is the difference
    between "we can't count this" and "we didn't try".
    """

    path: str
    added: int = 0
    deleted: int = 0
    binary: bool = False
    measurable: bool = True


@dataclass(frozen=True)
class ArtifactRisk:
    """What changed between the trusted base and the exact artifact tree."""

    base_sha: str
    tree_sha: str
    added_lines: int
    deleted_lines: int
    paths: tuple[str, ...]
    binary_paths: tuple[str, ...] = ()
    unmeasurable_paths: tuple[str, ...] = ()
    review_flags: tuple[str, ...] = ()
    refusals: tuple[str, ...] = ()

    @property
    def changed_lines(self) -> int:
        """Additions plus deletions — removing 900 lines is not a small diff."""
        return self.added_lines + self.deleted_lines

    @property
    def path_count(self) -> int:
        return len(self.paths)

    @property
    def measurable(self) -> bool:
        return not self.binary_paths and not self.unmeasurable_paths

    @property
    def risk_digest(self) -> str:
        """Binds the measurement to the exact base and tree it was taken on."""
        return _digest(
            "hrr",
            {
                "base_sha": self.base_sha,
                "tree_sha": self.tree_sha,
                "added_lines": self.added_lines,
                "deleted_lines": self.deleted_lines,
                "paths": list(self.paths),
                "binary_paths": list(self.binary_paths),
                "unmeasurable_paths": list(self.unmeasurable_paths),
                "review_flags": list(self.review_flags),
                "refusals": list(self.refusals),
            },
        )


def measure_risk(
    *,
    base_sha: str,
    tree_sha: str,
    changes: Iterable[PathChange],
    review_flags: Sequence[str] = (),
    refusals: Sequence[str] = (),
) -> ArtifactRisk:
    """Fold per-path changes into one risk measurement for one exact tree.

    An unmeasurable or binary path contributes nothing to the line count —
    counting a blob's bytes as lines would let a threshold be gamed in either
    direction — so it is carried as a named path instead, which is what makes
    the publication stop.
    """
    _require(bool(_OID.match(base_sha)), f"base {base_sha!r} is not a commit id")
    _require(bool(_OID.match(tree_sha)), f"tree {tree_sha!r} is not a tree id")
    added = deleted = 0
    paths: list[str] = []
    binary: list[str] = []
    unmeasurable: list[str] = []
    for change in changes:
        _require(bool(change.path), "a measured change needs a path")
        paths.append(change.path)
        if not change.measurable:
            unmeasurable.append(change.path)
            continue
        if change.binary:
            binary.append(change.path)
            continue
        added += change.added
        deleted += change.deleted
    return ArtifactRisk(
        base_sha=base_sha,
        tree_sha=tree_sha,
        added_lines=added,
        deleted_lines=deleted,
        paths=tuple(sorted(set(paths))),
        binary_paths=tuple(sorted(set(binary))),
        unmeasurable_paths=tuple(sorted(set(unmeasurable))),
        review_flags=tuple(review_flags),
        refusals=tuple(refusals),
    )


@dataclass(frozen=True)
class RiskDecision:
    """One verdict, bound to the policy and the tree that produced it."""

    outcome: str
    reasons: tuple[str, ...]
    policy_hash: str
    policy_generation: int
    delivery_repo: str
    base_sha: str
    tree_sha: str
    risk_digest: str

    @property
    def overridable(self) -> bool:
        """Only a confirmation-required hold can be released by a person."""
        return self.outcome == CONFIRMATION_REQUIRED

    @property
    def publishable(self) -> bool:
        """True only when nothing is owed before the remote effect."""
        return self.outcome == ALLOWED

    @property
    def retryable(self) -> bool:
        """Capacity clears on its own; a filter refusal never does."""
        return self.outcome == BLOCKED


def _risk_reasons(policy: ReviewPolicy, risk: ArtifactRisk) -> tuple[str, ...]:
    """Every reason this artifact wants a person, in a stable order."""
    checks = (
        (risk.changed_lines > policy.max_diff_lines, REASON_DIFF_LINES),
        (risk.path_count > policy.max_paths, REASON_PATHS),
        (bool(risk.binary_paths), REASON_BINARY),
        (bool(risk.unmeasurable_paths), REASON_UNMEASURABLE),
        (bool(risk.review_flags), REASON_REVIEW_FLAG),
    )
    return tuple(reason for tripped, reason in checks if tripped)


def _decision(
    outcome: str, reasons: tuple[str, ...], policy: ReviewPolicy, risk: ArtifactRisk
) -> RiskDecision:
    return RiskDecision(
        outcome=outcome,
        reasons=reasons,
        policy_hash=policy.policy_hash,
        policy_generation=policy.generation,
        delivery_repo=policy.delivery_repo,
        base_sha=risk.base_sha,
        tree_sha=risk.tree_sha,
        risk_digest=risk.risk_digest,
    )


def decide_publication(
    *, policy: ReviewPolicy, risk: ArtifactRisk, pending_reviews: int
) -> RiskDecision:
    """Judge one artifact under one policy, against this repo's occupancy.

    *pending_reviews* must already be scoped to ``policy.delivery_repo``;
    passing a cross-repository count would make one repo's backlog hold another
    repo's work, which is the thing this policy exists to prevent.

    Capacity is only consulted when a review is actually wanted. An artifact
    under every threshold publishes while three others wait for a human,
    because it needs no slot to leave free.
    """
    policy.validate()
    _require(
        isinstance(pending_reviews, int)
        and not isinstance(pending_reviews, bool)
        and pending_reviews >= 0,
        f"pending_reviews must be a non-negative count, got {pending_reviews!r}",
    )
    if risk.refusals:
        return _decision(REFUSED, (REASON_FILTER_REFUSAL,), policy, risk)
    reasons = _risk_reasons(policy, risk)
    if not reasons:
        return _decision(ALLOWED, (), policy, risk)
    if pending_reviews >= policy.max_pending_reviews:
        return _decision(BLOCKED, (*reasons, REASON_CAPACITY), policy, risk)
    return _decision(CONFIRMATION_REQUIRED, reasons, policy, risk)


@dataclass(frozen=True)
class RiskConfirmation:
    """A person's terminal judgement on one exact tree under one exact policy."""

    job_id: str
    delivery_repo: str
    base_sha: str
    tree_sha: str
    risk_digest: str
    policy_hash: str
    policy_generation: int
    actor: str
    confirmed_at: str


def authorize_approver(policy: ReviewPolicy, actor: str) -> None:
    """Refuse terminal judgement from anyone the policy did not name.

    An empty allowlist means "any authenticated actor", the same stance
    ``[approval].approvers`` already takes for spend. It never means "anyone",
    because an actor with no id is nobody: a hold released by an unnamed caller
    leaves no one to ask afterwards.
    """
    if not actor or not actor.strip():
        raise ReviewAuthorityError(
            "an unauthenticated actor cannot give terminal judgement"
        )
    if policy.approvers and actor not in policy.approvers:
        raise ReviewAuthorityError(
            f"actor {actor!r} is not a human_review approver for {policy.delivery_repo}"
        )


def confirm_risk(
    *,
    job_id: str,
    decision: RiskDecision,
    policy: ReviewPolicy,
    actor: str,
    now: datetime | str,
) -> RiskConfirmation:
    """Record that a named person cleared this exact tree under this policy.

    Refuses a :data:`REFUSED` decision unconditionally — there is no actor,
    allowlist entry or flag that turns a filter rejection into a publication —
    and refuses a :data:`BLOCKED` one because a review with no slot is not a
    review anyone is holding.
    """
    if decision.outcome == REFUSED:
        raise RefusalNotOverridableError(
            "the publication filter refused this artifact; no approver may confirm it"
        )
    if decision.outcome == BLOCKED:
        raise ReviewCapacityError(
            f"{decision.delivery_repo} is at review capacity — no hold exists"
            " to confirm"
        )
    _require(
        decision.outcome == CONFIRMATION_REQUIRED,
        "nothing was held for review on this artifact",
    )
    _require(
        decision.policy_hash == policy.policy_hash
        and decision.policy_generation == policy.generation,
        "the decision was made under a different policy than the one"
        " authorizing this confirmation",
    )
    authorize_approver(policy, actor)
    return RiskConfirmation(
        job_id=job_id,
        delivery_repo=decision.delivery_repo,
        base_sha=decision.base_sha,
        tree_sha=decision.tree_sha,
        risk_digest=decision.risk_digest,
        policy_hash=decision.policy_hash,
        policy_generation=decision.policy_generation,
        actor=actor,
        confirmed_at=now if isinstance(now, str) else now.isoformat(),
    )


def confirmation_covers(
    confirmation: RiskConfirmation, *, decision: RiskDecision
) -> bool:
    """True only for the exact tree, measurement and policy edit confirmed.

    Remediation that changes the SHA produces a different tree and a different
    risk digest, so the prior confirmation stops covering the work without
    anyone having to remember to revoke it. A policy edit or an expiry
    reversion moves the generation and does the same.
    """
    return (
        confirmation.delivery_repo == decision.delivery_repo
        and confirmation.base_sha == decision.base_sha
        and confirmation.tree_sha == decision.tree_sha
        and confirmation.risk_digest == decision.risk_digest
        and confirmation.policy_hash == decision.policy_hash
        and confirmation.policy_generation == decision.policy_generation
    )
