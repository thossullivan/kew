"""Governed-review verdict attestation (Task 13, F1/F10 — #977 epic).

The per-round ``adversarial_review_verdict`` is an **additive attested fact**:
it binds through the already-hashed audit ``payload`` dict, never as a new
``entry_hash`` preimage slot or column (the audit-chain forward rule — see the
``audit-chain-change`` skill). Policy binding follows the #250 precedent: a
verdict is only meaningful relative to the policy in force, so the policy block
travels inside the payload.

Two honesty invariants live here:

- **Spend comes from RunRecords, never the sidecar** (F10): the reviewer's
  self-reported ``spend_usd`` is dropped by the parser and never reaches this
  module; :func:`review_spend_usd` sums the node's review-kind runs instead.
- **Join keys** ``{pr_number, item_id, head_sha}`` make "show me this merged
  PR's review provenance" a chain lookup, not an inference.
- **The round's scope is attested** (#2111): ``review_mode`` records whether the
  round reviewed the whole PR diff (``full``) or only the change since the
  previous reviewed head (``delta``). Without it the chain cannot tell a delta
  READY from a full one, and the rule that a READY always has a recent full
  pass behind it would be unverifiable from the chain alone.

Beyond the reviewer's READY / NEEDS_CHANGES sidecar verdicts, the driver emits
degenerate verdicts when a human action or a policy grandfather short-circuits
the stage: DEGRADED (advisory reviewer crash), UNPARSEABLE (blocking reviewer
crash — the node is HELD, but the launch evidence is still attested, #1393),
MERGED_UNREVIEWED (out-of-band merge over a live reviewer), PRE_POLICY (flag
flipped after the node was already in review). The chain records them rather
than going silent.
"""

from __future__ import annotations

from typing import Any

from kew.audit import Actor, emit
from kew.config import ReviewConfig
from kew.ids import GITHUB_ID_RE
from kew.run_store import REVIEW_RUN_KINDS, RunStore

REVIEW_VERDICT_EVENT = "adversarial_review_verdict"
#: A reviewer round that collected no verdict and is run again instead of held
#: (#3174). Not a verdict: no round is recorded, and the retry bound reads it.
REVIEW_RETRIED_EVENT = "adversarial_review_retried"

# Degenerate verdicts the driver attests beyond the reviewer's sidecar shapes.
VERDICT_DEGRADED = "DEGRADED"
VERDICT_UNPARSEABLE = "UNPARSEABLE"
VERDICT_MERGED_UNREVIEWED = "MERGED_UNREVIEWED"
VERDICT_PRE_POLICY = "PRE_POLICY"

# The driver is the default attesting actor; human races override it.
_DRIVER_ACTOR = Actor("policy", "epic-driver")


def review_policy_block(review: ReviewConfig) -> dict[str, Any]:
    """The policy-in-force block bound into every verdict payload (#250).

    A verdict without the policy it was judged under is unfalsifiable — the same
    NEEDS_CHANGES means different things at ``blocking=true`` vs ``false``.
    """
    return {
        "blocking": review.blocking,
        "max_rounds": review.max_rounds,
        "max_review_usd": review.max_review_usd,
    }


def review_spend_usd(store: RunStore, item_id: str) -> float:
    """Total review-pool spend for *item_id* from RunRecords (F10).

    Sums ``estimated_cost_usd`` over the node's review-kind runs
    (``adversarial_review`` / ``review_remediation``) only — implementation
    spend never leaks in, and the sidecar's self-reported figure is never used.
    """
    return sum(
        r.estimated_cost_usd
        for r in store.list_by_item(item_id)
        if r.run_kind in REVIEW_RUN_KINDS
    )


def build_review_verdict_payload(
    *,
    pr_number: int | None,
    item_id: str,
    head_sha: str | None,
    review_round: int,
    verdict: str,
    policy: dict[str, Any],
    spend_usd: float,
    sidecar_digest: str | None = None,
    reviewer_model: str | None = None,
    prompt_artifact_sha: str | None = None,
    effective_prompt_sha: str | None = None,
    prompt_profile_version_id: int | None = None,
    base_ref: str | None = None,
    base_commit: str | None = None,
    acceptance_executed: bool | None = None,
    acceptance_reason: str | None = None,
    review_mode: str | None = None,
    actor_note: str | None = None,
    failure_layer: str | None = None,
    error_category: str | None = None,
    refusal: str | None = None,
) -> dict[str, Any]:
    """Build the canonical ``adversarial_review_verdict`` payload (spec §Attestation).

    Every field the spec enumerates is always present (nullable where a
    degenerate verdict has no reviewer output), so a payload-completeness check
    is a set-containment test, not a shape-by-verdict special case.

    ``prompt_profile_version_id`` names the operator addendum the reviewer ran
    under (#3024), None for none; ``effective_prompt_sha`` is sha256 of the
    whole text it received, addendum and verdict channel included.
    ``base_ref`` and ``base_commit`` name where the base artifact was read
    (#3088): the commit whose artifact bytes hash to ``prompt_artifact_sha``.
    """
    payload: dict[str, Any] = {
        "pr_number": pr_number,
        "item_id": item_id,
        "head_sha": head_sha,
        "round": review_round,
        "verdict": verdict,
        "sidecar_digest": sidecar_digest,
        "reviewer_model": reviewer_model,
        "prompt_artifact_sha": prompt_artifact_sha,
        "effective_prompt_sha": effective_prompt_sha,
        "prompt_profile_version_id": prompt_profile_version_id,
        "base_ref": base_ref,
        "base_commit": base_commit,
        "acceptance_executed": acceptance_executed,
        "acceptance_reason": acceptance_reason,
        "review_mode": review_mode,
        "policy": policy,
        "spend_source": "run_records",
        "spend_usd": spend_usd,
    }
    if actor_note is not None:
        # Who bypassed the gate (e.g. ``out_of_band``) — envelope actor records
        # the same, but the payload keeps it queryable per verdict row.
        payload["actor"] = actor_note
    if failure_layer is not None:
        payload["failure_layer"] = failure_layer
    if error_category is not None:
        payload["error_category"] = error_category
    if refusal is not None:
        # Why the reviewer's stream carried no admissible verdict (#3174), so
        # the attested crash says what was wrong, not only that it was.
        payload["refusal"] = refusal
    return payload


def emit_review_verdict(
    store: RunStore | None,
    review: ReviewConfig,
    *,
    pr_number: int | None,
    item_id: str,
    head_sha: str | None,
    review_round: int,
    verdict: str,
    sidecar_digest: str | None = None,
    reviewer_model: str | None = None,
    prompt_artifact_sha: str | None = None,
    effective_prompt_sha: str | None = None,
    prompt_profile_version_id: int | None = None,
    base_ref: str | None = None,
    base_commit: str | None = None,
    acceptance_executed: bool | None = None,
    acceptance_reason: str | None = None,
    review_mode: str | None = None,
    repo: str | None = None,
    run_id: str | None = None,
    actor: Actor | None = None,
    failure_layer: str | None = None,
    error_category: str | None = None,
    refusal: str | None = None,
) -> None:
    """Append one ``adversarial_review_verdict`` attested fact.

    Best-effort (delegates to :func:`kew.audit.emit`, which never raises). Spend
    is always drawn from RunRecords via :func:`review_spend_usd` (F10): the
    attested ``spend_usd`` can never be a caller-supplied figure while the
    payload claims ``spend_source: run_records``. ``actor`` defaults to the
    driver; a human race passes ``Actor("human", "out_of_band")`` and its id is
    also mirrored into the payload for per-row querying.
    """
    spend_usd = review_spend_usd(store, item_id) if store is not None else 0.0
    resolved_actor = actor if actor is not None else _DRIVER_ACTOR
    actor_note = resolved_actor.id if resolved_actor.type == "human" else None
    payload = build_review_verdict_payload(
        pr_number=pr_number,
        item_id=item_id,
        head_sha=head_sha,
        review_round=review_round,
        verdict=verdict,
        policy=review_policy_block(review),
        spend_usd=spend_usd,
        sidecar_digest=sidecar_digest,
        reviewer_model=reviewer_model,
        prompt_artifact_sha=prompt_artifact_sha,
        effective_prompt_sha=effective_prompt_sha,
        prompt_profile_version_id=prompt_profile_version_id,
        base_ref=base_ref,
        base_commit=base_commit,
        acceptance_executed=acceptance_executed,
        acceptance_reason=acceptance_reason,
        review_mode=review_mode,
        actor_note=actor_note,
        failure_layer=failure_layer,
        error_category=error_category,
        refusal=refusal,
    )
    emit(
        store,
        REVIEW_VERDICT_EVENT,
        actor=resolved_actor,
        run_id=run_id,
        issue_num=int(item_id) if GITHUB_ID_RE.fullmatch(item_id) else 0,
        item_id=item_id,
        repo=repo,
        payload=payload,
    )


def emit_review_retried(
    store: RunStore,
    *,
    item_id: str,
    head_sha: str,
    error_category: str,
    refusal: str | None,
    repo: str | None = None,
    run_id: str | None = None,
) -> None:
    """Attest that a verdict-less round at *head_sha* is being run again.

    Strict: the retry bound is read back from these rows, so a retry whose
    row did not land would leave the count at zero and every tick would launch
    another paid run. The append raises instead, and the caller holds.
    """
    emit(
        store,
        REVIEW_RETRIED_EVENT,
        actor=_DRIVER_ACTOR,
        run_id=run_id,
        issue_num=int(item_id) if GITHUB_ID_RE.fullmatch(item_id) else 0,
        item_id=item_id,
        repo=repo,
        payload={
            "item_id": item_id,
            "head_sha": head_sha,
            "error_category": error_category,
            "refusal": refusal,
        },
        strict=True,
    )


def review_retries(
    store: RunStore, *, item_id: str, head_sha: str, error_category: str
) -> int:
    """How many rounds of *item_id* at *head_sha* were already retried for
    *error_category* — the bound is per node and per head, so a new head starts
    from none."""
    return sum(
        1
        for event in store.list_audit_events(
            None, event=REVIEW_RETRIED_EVENT, item_id=item_id
        )
        if isinstance(event.payload, dict)
        and event.payload.get("head_sha") == head_sha
        and event.payload.get("error_category") == error_category
    )
