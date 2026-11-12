"""Acceptance disputes and amendments: the implementer never edits (#2731).

A node's acceptance contract (:mod:`kew.run_store_acceptance`) is written by
the ``acceptance_author`` before the implementer starts, and the implementer
never edits it. When the implementer believes a contract test is wrong it says
so in its result instead: a final message

    KEW_NEEDS_HUMAN: acceptance-disputed {"criterion": "AC2", "test": "test_ac2",
    "problem": "...", "evidence": "...", "proposed_fix": "..."}

rides the same stop channel every runner already reads (#2601). The dispute
puts the node on Needs-you under the ``acceptance-disputed`` hold. That is not a
failure and it is not a review round: the node never reached review.

A person then resolves it one of two ways, and both end in an amendment:

- by hand: they commit the changed file and run ``kew epic acceptance amend
  <epic> <N> --reason TEXT``, which records the committed file as the next
  contract version;
- by referral: ``kew epic acceptance amend <epic> <N> --author --reason TEXT``
  releases the hold and the node's next dispatch runs ``acceptance_author``
  again with the dispute as input
  (:func:`kew.acceptance_author.answer_acceptance_dispute`).

Every amendment writes one attested ``acceptance_amended`` audit event: who,
why, the old and new digests, and the dispute it answered. No second approver.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from kew.audit import Actor, emit
from kew.epic_holds import ACCEPTANCE_DISPUTED_HOLD_REASON
from kew.ids import GITHUB_ID_RE

if TYPE_CHECKING:
    from kew.run_store import AcceptanceContractRow, RunStore

_logger = logging.getLogger(__name__)

#: The word that opens a dispute inside a ``KEW_NEEDS_HUMAN:`` reason.
DISPUTE_MARKER = ACCEPTANCE_DISPUTED_HOLD_REASON

#: Where a node's open dispute lives; the value is :meth:`PendingDispute.to_json`,
#: or ``""`` once an amendment answered it.
DISPUTE_META_PREFIX = "acceptance_dispute:"

_FIELD_MAX = 2000


class AcceptanceAmendmentRefused(RuntimeError):
    """An amendment the ledger could not record."""


class AcceptanceDispute(BaseModel):
    """What the implementer says is wrong with one contract test."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    criterion: str = Field(min_length=1, max_length=_FIELD_MAX)
    test: str = Field(min_length=1, max_length=_FIELD_MAX)
    problem: str = Field(min_length=1, max_length=_FIELD_MAX)
    evidence: str = Field(default="", max_length=_FIELD_MAX)
    proposed_fix: str = Field(default="", max_length=_FIELD_MAX)


@dataclass(frozen=True)
class PendingDispute:
    """An open dispute against one version of a node's contract."""

    dispute: AcceptanceDispute
    run_id: str
    """The implementer run whose result carried the dispute."""
    contract_version: int
    contract_sha256: str
    referred: bool = False
    """A person asked the author step to answer it."""

    def to_json(self) -> str:
        return json.dumps(self.as_payload(), sort_keys=True)

    def as_payload(self) -> dict[str, Any]:
        return {
            "dispute": self.dispute.model_dump(),
            "run_id": self.run_id,
            "contract_version": self.contract_version,
            "contract_sha256": self.contract_sha256,
            "referred": self.referred,
        }


def parse_acceptance_dispute(reason: str) -> AcceptanceDispute | None:
    """The dispute a needs-human *reason* carries, or None when it carries none."""
    text = reason.strip()
    if not text.startswith(DISPUTE_MARKER):
        return None
    try:
        return AcceptanceDispute.model_validate_json(
            text.removeprefix(DISPUTE_MARKER).strip()
        )
    except ValidationError as exc:
        _logger.warning(
            "acceptance dispute unreadable; read as a plain needs-human stop: %s",
            exc.errors(include_input=False, include_url=False),
        )
        return None


def dispute_key(epic_ref: str, item_id: str) -> str:
    """The ``meta`` key one node's open dispute lives under."""
    return f"{DISPUTE_META_PREFIX}{epic_ref}:{item_id}"


def _issue_num(item_id: str) -> int:
    return int(item_id) if GITHUB_ID_RE.fullmatch(item_id) else 0


def _load(raw: str | None) -> PendingDispute | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return PendingDispute(
            dispute=AcceptanceDispute.model_validate(data["dispute"]),
            run_id=str(data["run_id"]),
            contract_version=int(data["contract_version"]),
            contract_sha256=str(data["contract_sha256"]),
            referred=bool(data.get("referred", False)),
        )
    except (ValueError, KeyError, TypeError, ValidationError) as exc:
        _logger.warning(
            "recorded acceptance dispute unreadable: %s", type(exc).__name__
        )
        return None


def pending_dispute(
    store: RunStore, epic_ref: str, item_id: str
) -> PendingDispute | None:
    """The node's open dispute, or None when no dispute is open."""
    return _load(store.get_meta(dispute_key(epic_ref, item_id)))


def is_amended_contract(store: RunStore, contract: AcceptanceContractRow) -> bool:
    """True when *contract*'s file is one an ``acceptance_amended`` event recorded.

    A restore re-records the same digest at a new commit, so the digest, not
    the version, says whether the current file came from an amendment.
    """
    return any(
        (event.payload or {}).get("epic_ref") == contract.epic_ref
        and (event.payload or {}).get("node") == contract.item_id
        and (event.payload or {}).get("new_sha256") == contract.sha256
        for event in store.list_audit_events(
            None, event="acceptance_amended", item_or_issue=contract.item_id
        )
    )


def record_acceptance_dispute(
    store: RunStore,
    *,
    epic_ref: str,
    item_id: str,
    dispute: AcceptanceDispute,
    run_id: str,
    repo: str | None = None,
) -> PendingDispute | None:
    """Hold the node as ``acceptance-disputed``; None when it has no contract.

    A node without a recorded contract has nothing to dispute, so the caller
    reads that result as the plain needs-human stop it otherwise is.
    """
    contract = store.current_acceptance_contract(epic_ref, item_id)
    if contract is None:
        return None
    pending = PendingDispute(
        dispute=dispute,
        run_id=run_id,
        contract_version=contract.version,
        contract_sha256=contract.sha256,
    )
    store.set_meta(dispute_key(epic_ref, item_id), pending.to_json())
    store.hold_epic_node_item(epic_ref, item_id, ACCEPTANCE_DISPUTED_HOLD_REASON)
    emit(
        store,
        "acceptance_disputed",
        actor=Actor("agent", run_id or "unknown"),
        run_id=run_id or None,
        issue_num=_issue_num(item_id),
        item_id=item_id,
        repo=repo,
        payload={"epic_ref": epic_ref, "node": item_id, **pending.as_payload()},
    )
    return pending


def hold_disputed_node(
    store: RunStore | None,
    *,
    epic_ref: str | None,
    item_id: str,
    reason: str,
    run_id: str,
    repo: str | None = None,
) -> AcceptanceDispute | None:
    """Record the dispute a needs-human *reason* carries; None when there is none.

    None too off an epic, without a store, or for a node with no contract:
    the stop is then the plain needs-human stop it otherwise is.
    """
    if store is None or epic_ref is None:
        return None
    dispute = parse_acceptance_dispute(reason)
    if dispute is None:
        return None
    pending = record_acceptance_dispute(
        store,
        epic_ref=epic_ref,
        item_id=item_id,
        dispute=dispute,
        run_id=run_id,
        repo=repo,
    )
    return None if pending is None else dispute


def _release_dispute_hold(store: RunStore, epic_ref: str, item_id: str) -> None:
    """Release the ``acceptance-disputed`` hold and re-admit the node.

    The disputing run ended as a needs-human stop, so with the hold gone the
    node would read FAILED and never dispatch again. The release writes the
    retry marker in the same transaction, which is what lets the implementer
    continue on the amended contract. Any other hold is left standing.
    """
    store.remove_epic_hold(
        epic_ref,
        item_id,
        fresh_review_cycle_reasons=(ACCEPTANCE_DISPUTED_HOLD_REASON,),
        only_reason_prefixes=(ACCEPTANCE_DISPUTED_HOLD_REASON,),
    )


def refer_dispute_to_author(
    store: RunStore,
    *,
    epic_ref: str,
    item_id: str,
    reason: str,
    actor: Actor,
    repo: str | None = None,
) -> PendingDispute:
    """Ask the author step to answer the node's open dispute.

    Releases the ``acceptance-disputed`` hold so the next dispatch runs; that
    dispatch re-runs the author with the dispute before the implementer.
    """
    pending = pending_dispute(store, epic_ref, item_id)
    if pending is None:
        raise AcceptanceAmendmentRefused(
            "no open acceptance dispute to refer to the author step"
        )
    referred = PendingDispute(
        dispute=pending.dispute,
        run_id=pending.run_id,
        contract_version=pending.contract_version,
        contract_sha256=pending.contract_sha256,
        referred=True,
    )
    if not store.compare_set_meta(
        dispute_key(epic_ref, item_id), pending.to_json(), referred.to_json()
    ):
        raise AcceptanceAmendmentRefused("the dispute changed while referring it")
    emit(
        store,
        "acceptance_dispute_referred",
        actor=actor,
        issue_num=_issue_num(item_id),
        item_id=item_id,
        repo=repo,
        payload={
            "epic_ref": epic_ref,
            "node": item_id,
            "reason": reason,
            **referred.as_payload(),
        },
        strict=True,
    )
    _release_dispute_hold(store, epic_ref, item_id)
    return referred


def record_acceptance_amendment(
    store: RunStore,
    *,
    epic_ref: str,
    item_id: str,
    sha256: str,
    author_run_id: str,
    commit: str,
    reason: str,
    actor: Actor,
    repo: str | None = None,
) -> AcceptanceContractRow:
    """Record the next contract version and attest it as ``acceptance_amended``.

    The open dispute, if any, is the one this amendment answers: it rides in
    the event and is closed, and the ``acceptance-disputed`` hold is released
    so the implementer continues on the amended contract.
    """
    if not reason.strip():
        raise AcceptanceAmendmentRefused("an amendment needs a reason")
    old = store.current_acceptance_contract(epic_ref, item_id)
    if old is None:
        raise AcceptanceAmendmentRefused(
            f"node {item_id} of {epic_ref} has no acceptance contract to amend"
        )
    if old.sha256 == sha256:
        raise AcceptanceAmendmentRefused(
            f"{old.path} is unchanged from contract version {old.version}"
        )
    key = dispute_key(epic_ref, item_id)
    raw = store.get_meta(key)
    answered = _load(raw)
    new = store.record_acceptance_contract(
        epic_ref,
        item_id,
        path=old.path,
        sha256=sha256,
        author_run_id=author_run_id,
        commit=commit,
        plan_sha=old.plan_sha,
        test_names=old.test_names,
    )
    emit(
        store,
        "acceptance_amended",
        actor=actor,
        issue_num=_issue_num(item_id),
        item_id=item_id,
        repo=repo,
        payload={
            "epic_ref": epic_ref,
            "node": item_id,
            "reason": reason,
            "path": new.path,
            "old_version": old.version,
            "new_version": new.version,
            "old_sha256": old.sha256,
            "new_sha256": new.sha256,
            "commit": new.commit,
            "author_run_id": new.author_run_id,
            "dispute": None if answered is None else answered.as_payload(),
        },
        strict=True,
    )
    if raw:
        store.compare_set_meta(key, raw, "")
    _release_dispute_hold(store, epic_ref, item_id)
    return new


def close_superseded_dispute(
    store: RunStore, *, contract: AcceptanceContractRow, repo: str | None = None
) -> PendingDispute | None:
    """Close the open dispute a contract for a new plan leaves behind.

    The dispute names tests of a contract written for an older plan; the
    author has just written *contract* afresh for the current one, so there
    is nothing left for it to answer. Closing it keeps the next dispatch from
    amending the new contract as if it answered a dispute filed against the
    old one. One ``acceptance_dispute_superseded`` event records it.
    """
    key = dispute_key(contract.epic_ref, contract.item_id)
    raw = store.get_meta(key)
    closed = _load(raw)
    if not raw or closed is None or not store.compare_set_meta(key, raw, ""):
        return None
    emit(
        store,
        "acceptance_dispute_superseded",
        actor=Actor("agent", contract.author_run_id or "unknown"),
        run_id=contract.author_run_id or None,
        issue_num=_issue_num(contract.item_id),
        item_id=contract.item_id,
        repo=repo,
        payload={
            "epic_ref": contract.epic_ref,
            "node": contract.item_id,
            "new_version": contract.version,
            "new_sha256": contract.sha256,
            "plan_sha": contract.plan_sha,
            "dispute": closed.as_payload(),
        },
    )
    return closed
