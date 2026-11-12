"""The one merge verdict rule every merge path applies (#3196).

A merge refuses while the newest real ``adversarial_review_verdict`` attested
for the exact head is NEEDS_CHANGES, or NEEDS_FULL (a review round that still
owes an independent full round at that head, #2111, #3422). ``kew merge
check``, every merge the epic driver makes, ``kew epic merge`` and ``kew
drive`` (through it) and the project merge queue all ask
:func:`check_merge_verdict` or :func:`judge_head`, so no path carries its own
copy.

* **Real verdicts only.** UNPARSEABLE and DEGRADED are kew faults, not verdicts
  on the code, and so is any row naming one of the review-fault classifications
  in :data:`REVIEW_FAULT_CATEGORIES` (#3174): a crashed round after a
  NEEDS_CHANGES does not hide it. Any other ``error_category`` is a note on a
  real verdict, and the verdict counts.
* **The exact head.** A verdict attested for another head reviewed other code
  and is ignored. A head that is not a full 40-hex sha refuses.
* **Fail closed.** A verdict outside kew's verdict vocabulary refuses, and so
  does a ledger the check cannot read or a verdict row whose payload does not
  decode: that row could be the newest verdict on any head.
* **No verdict.** Passes the rule. The drive's merges still need a READY round
  under the standing round rule wherever its review is blocking.

An operator may merge over a NEEDS_CHANGES or NEEDS_FULL only by naming a
reason: the override is attested as a ``merge_verdict_override`` event naming
the actor, the head and the reason (:func:`record_override`).
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from kew.audit import Actor, emit
from kew.review_audit import (
    REVIEW_VERDICT_EVENT,
    VERDICT_DEGRADED,
    VERDICT_MERGED_UNREVIEWED,
    VERDICT_PRE_POLICY,
    VERDICT_UNPARSEABLE,
)
from kew.review_contract import (
    FAILED_NO_VERDICT_CATEGORY,
    MULTIPLE_RESULTS_CATEGORY,
    RATE_LIMITED_CATEGORY,
)
from kew.review_delta import VERDICT_NEEDS_FULL
from kew.run_store import AuditEvent, RunStore
from kew.tracking import STREAM_SILENT_CATEGORY

MERGE_VERDICT_OVERRIDE_EVENT = "merge_verdict_override"

VERDICT_READY = "READY"
VERDICT_NEEDS_CHANGES = "NEEDS_CHANGES"

#: Every value kew attests as a review verdict; anything else refuses.
KNOWN_VERDICTS = frozenset(
    {
        VERDICT_READY,
        VERDICT_NEEDS_CHANGES,
        VERDICT_NEEDS_FULL,
        VERDICT_DEGRADED,
        VERDICT_UNPARSEABLE,
        VERDICT_MERGED_UNREVIEWED,
        VERDICT_PRE_POLICY,
    }
)
#: Verdicts that record a kew fault rather than a judgment of the code.
FAULT_VERDICTS = frozenset({VERDICT_DEGRADED, VERDICT_UNPARSEABLE})
#: The ``error_category`` values the review stage names a failed reviewer round
#: with (its crash holds): a launch that never ran, a provider limit, a stream
#: with no admissible verdict (#3174, #3186). A row naming one is a kew fault.
REVIEW_FAULT_CATEGORIES = frozenset(
    {
        "sandbox_refused",
        "launch_failed",
        "credential_missing",
        "container_start_failed",
        "runtime_timeout",
        "sandbox_startup_refused",
        "prompt_configuration_refused",
        RATE_LIMITED_CATEGORY,
        MULTIPLE_RESULTS_CATEGORY,
        FAILED_NO_VERDICT_CATEGORY,
        STREAM_SILENT_CATEGORY,
    }
)

#: The prefix of a merge path's refusal reason: ``merge-verdict-<code>``.
MERGE_VERDICT_REFUSED = "merge-verdict"
# Refusal codes, stable for scripts and attestations.
REFUSED_MALFORMED_HEAD = "malformed-head"
REFUSED_UNREADABLE_LEDGER = "unreadable-ledger"
REFUSED_CORRUPT_VERDICT = "corrupt-verdict"
REFUSED_UNKNOWN_VERDICT = "unknown-verdict"
REFUSED_NEEDS_CHANGES = "needs-changes"
REFUSED_NEEDS_FULL = "needs-full"

#: The refusals an operator's attested reason may merge over.
OVERRIDABLE_REFUSALS = frozenset({REFUSED_NEEDS_CHANGES, REFUSED_NEEDS_FULL})

#: The verdicts that stop a merge, each with its refusal code and why.
_STANDING = {
    VERDICT_NEEDS_CHANGES: (REFUSED_NEEDS_CHANGES, ""),
    VERDICT_NEEDS_FULL: (
        REFUSED_NEEDS_FULL,
        ": a review round that still owes an independent full round at this head",
    ),
}

_FULL_SHA_RE = re.compile(r"[0-9a-fA-F]{40}")


class CorruptVerdictError(Exception):
    """A verdict row whose payload did not decode; ``event_id`` names it."""

    def __init__(self, event_id: int) -> None:
        super().__init__(f"audit event #{event_id} has an unreadable payload")
        self.event_id = event_id


@dataclass(frozen=True)
class VerdictCheck:
    """What the rule found for one head.

    ``verdict`` is the newest real verdict on the head (None for none),
    ``event_id`` its audit row, and ``refusal`` the code that stops the merge,
    None when it may go on. ``detail`` says why in a sentence.
    """

    head: str
    verdict: str | None
    event_id: int | None
    refusal: str | None
    detail: str

    @property
    def passed(self) -> bool:
        return self.refusal is None


def _is_fault(payload: dict[str, object]) -> bool:
    return (
        payload.get("verdict") in FAULT_VERDICTS
        or payload.get("error_category") in REVIEW_FAULT_CATEGORIES
    )


def _newest_real_verdict(store: RunStore, head: str) -> AuditEvent | None:
    """The newest verdict event attested for exactly *head*, faults skipped.

    A store with no audit trail at all holds no verdicts, the contract
    :func:`kew.audit.emit` keeps for it; a trail that fails to read raises,
    and so does a verdict row whose payload did not decode
    (:class:`CorruptVerdictError`): which head it names cannot be known.
    """
    read = getattr(store, "list_audit_events", None)
    if read is None:
        return None
    events: list[AuditEvent] = read(limit=None, event=REVIEW_VERDICT_EVENT)
    for event in events:
        payload = event.payload
        if not isinstance(payload, dict):
            raise CorruptVerdictError(event.id)
        recorded = payload.get("head_sha")
        if not isinstance(recorded, str) or recorded.lower() != head.lower():
            continue
        if not _is_fault(payload):
            return event
    return None


def _result(
    head: str, event: AuditEvent | None, refusal: str | None, detail: str
) -> VerdictCheck:
    verdict = None if event is None else (event.payload or {}).get("verdict")
    return VerdictCheck(
        head=head,
        verdict=verdict if isinstance(verdict, str) else repr(verdict),
        event_id=None if event is None else event.id,
        refusal=refusal,
        detail=detail,
    )


def check_merge_verdict(
    store: RunStore, head: str, *, accepted: bool = False
) -> VerdictCheck:
    """Apply the merge verdict rule to an operator-named *head*.

    A head that is not a full 40-hex sha refuses: an abbreviation would match
    no verdict and pass as unreviewed. Then :func:`judge_head`.
    """
    if not _FULL_SHA_RE.fullmatch(head):
        return VerdictCheck(
            head=head,
            verdict=None,
            event_id=None,
            refusal=REFUSED_MALFORMED_HEAD,
            detail=f"the head must be a full 40-character commit sha, got {head!r}",
        )
    return judge_head(store, head.lower(), accepted=accepted)


def judge_head(store: RunStore, head: str, *, accepted: bool = False) -> VerdictCheck:
    """Apply the merge verdict rule to *head* exactly as the forge names it.

    The drive's merge paths call this with the head the forge reported; an
    operator's head goes through :func:`check_merge_verdict`. *accepted* says
    the caller's standing rule already holds an attested acceptance of this
    head's NEEDS_CHANGES residuals (#2112), which reads as READY. It never
    passes a NEEDS_FULL: that verdict accepts nothing.
    """
    try:
        event = _newest_real_verdict(store, head)
    except (sqlite3.Error, OSError) as exc:
        return _result(
            head,
            None,
            REFUSED_UNREADABLE_LEDGER,
            f"the review verdicts could not be read: {exc}",
        )
    except CorruptVerdictError as exc:
        return VerdictCheck(
            head=head,
            verdict=None,
            event_id=exc.event_id,
            refusal=REFUSED_CORRUPT_VERDICT,
            detail=(
                f"review verdict audit event #{exc.event_id} has a payload that "
                "does not decode, so no head's newest verdict can be known"
            ),
        )
    return _judge(head, event, accepted=accepted)


def _judge(head: str, event: AuditEvent | None, *, accepted: bool) -> VerdictCheck:
    if event is None:
        return _result(
            head, None, None, f"no review verdict is attested for {head[:12]}"
        )
    verdict = (event.payload or {}).get("verdict")
    where = f"audit event #{event.id} for {head[:12]}"
    if verdict not in KNOWN_VERDICTS:
        return _result(
            head,
            event,
            REFUSED_UNKNOWN_VERDICT,
            f"{where} carries {verdict!r}, which is not a kew review verdict",
        )
    standing = _STANDING.get(verdict)
    if standing is not None and not (accepted and verdict == VERDICT_NEEDS_CHANGES):
        refusal, why = standing
        return _result(
            head,
            event,
            refusal,
            f"the newest review verdict ({where}) is {verdict}{why}",
        )
    return _result(
        head, event, None, f"the newest review verdict ({where}) is {verdict}"
    )


def record_override(
    store: RunStore,
    check: VerdictCheck,
    *,
    reason: str,
    actor: Actor,
    pr: int | None = None,
    repo: str | None = None,
) -> int:
    """Attest an operator merging over *check*'s NEEDS_CHANGES or NEEDS_FULL.

    Strict: an override whose row did not land is not audited, so the append
    raises and the caller refuses. Returns the event's row id.
    """
    event_id = emit(
        store,
        MERGE_VERDICT_OVERRIDE_EVENT,
        actor=actor,
        repo=repo,
        payload={
            "actor": actor.id,
            "actor_type": actor.type,
            "head_sha": check.head,
            "pr": pr,
            "reason": reason,
            "verdict": check.verdict,
            "verdict_event_id": check.event_id,
        },
        strict=True,
    )
    if event_id is None:
        raise RuntimeError("the merge verdict override was not recorded")
    return event_id
