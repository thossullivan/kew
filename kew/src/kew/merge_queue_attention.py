"""Why the merge queue raised its attention, as a stable reason code (#2982).

``merge_queue_attention`` used to fire with one shape for four different
situations — a red main, a merge that landed a tree no verify read, a
declaration edited after enqueue, a head the pre-verify gate held — and for an
amended acceptance contract, which the queue requeues by itself. The console
drew every one magenta with "check main". Each emit now carries one of these
codes, and the code decides the event's name: a person is the next move only
for :data:`PERSON_NEXT`, so only those are ``merge_queue_attention``; the rest
are ``merge_queue_notice``, history the queue handles itself.

The console derives the row's action words and its tone from the same code
(``frontend/src/lib/monitor/merge-queue-attention.ts``), so a new code must be
named there too; an unknown code on the attention event still reads needs-you.
"""

from __future__ import annotations

#: The event a person has to act on.
ATTENTION_EVENT = "merge_queue_attention"
#: The event the queue raised about something it is already handling.
NOTICE_EVENT = "merge_queue_notice"

#: Main verified red after a partial train; nothing merges until it is green.
MAIN_RED = "main_red"
#: A merge landed a tree no verify read (or its validation state is missing).
MAIN_UNVERIFIED = "main_unverified"
#: A queued PR's declaration changed after enqueue: dropped, re-review, re-add.
DECLARATION_EDITED = "declaration_edited"
#: The pre-verify gate held a head until a human releases it.
HEAD_HELD = "head_held"
#: An acceptance contract changed while verifying; the queue requeued itself.
CONTRACT_AMENDED = "contract_amended"
#: A review round withdrew a verified head's admission just before its merge:
#: the entry is dropped and the train after it requeued. The round itself
#: sends the node back to its fix loop, so no person is the next move.
REVIEW_WITHDRAWN = "review_withdrawn"

#: The codes whose next move is a person's.
PERSON_NEXT: frozenset[str] = frozenset(
    {MAIN_RED, MAIN_UNVERIFIED, DECLARATION_EDITED, HEAD_HELD}
)
REASONS: frozenset[str] = PERSON_NEXT | {CONTRACT_AMENDED, REVIEW_WITHDRAWN}


def person_next(reason: str) -> bool:
    """Whether *reason* is a person's move: :data:`ATTENTION_EVENT` if so,
    :data:`NOTICE_EVENT` if not. An unknown code is a programming error."""
    if reason not in REASONS:
        raise ValueError(f"unknown merge queue attention reason: {reason!r}")
    return reason in PERSON_NEXT


def attention_payload(
    reason: str, *, repo: str, pr: int | None, sha: str | None, message: str
) -> dict[str, object]:
    """The event payload: ``reason``, then what it is about, then the rest.

    In that order because the console reads the audit summary — the payload's
    JSON cut at 200 characters — and a long message must not push the code, the
    PR or the repository off its end. ``pr_number`` is the key the console
    names a PR by; ``repo`` tells one main from another in a shared ledger.
    """
    return {
        "reason": reason,
        "pr_number": pr,
        "repo": repo,
        "sha": sha,
        "message": message,
    }
