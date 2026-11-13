"""Review rounds whose verdict names checks the review sandbox could not run.

Every merge path reads ``not_checked`` (#3419, #3421) off the stored sidecar of
the round it merges on: the head's latest READY, or a NEEDS_CHANGES whose
residuals the stop rule accepted (#2112). :func:`record_reviewed` writes either
the way the review stage and the stop rule do — the round with its sidecar
file and digest, then for an acceptance the chain event and the round's mark.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from kew.review_residuals import mark_round_accepted, record_acceptance


def not_checked(kinds: Sequence[str]) -> list[dict[str, str]]:
    """One ``not_checked`` entry per kind, as a reviewer writes them."""
    return [{"check": f"the {k} check", "kind": k, "reason": "no"} for k in kinds]


def record_reviewed(
    store: Any,
    sidecar_dir: Path,
    *,
    epic_ref: str,
    item_id: str,
    head: str,
    kinds: Sequence[str],
    accepted: bool = False,
    repo: str = "o/r",
) -> None:
    """Record the latest round on *head*, its sidecar naming *kinds*.

    READY by default; with *accepted*, a NEEDS_CHANGES whose residuals the
    stop rule accepted on both of its records (``acceptance_on_record``).
    """
    verdict = "NEEDS_CHANGES" if accepted else "READY"
    sidecar = sidecar_dir / f"review-sidecar-{item_id}-{head[:12]}-{verdict}.json"
    sidecar.write_text(
        json.dumps({"verdict": verdict, "not_checked": not_checked(kinds)})
    )
    digest = hashlib.sha256(sidecar.read_bytes()).hexdigest()
    round_num = store.record_review_round(
        epic_ref,
        item_id,
        head_sha=head,
        verdict=verdict,
        review_mode="full",
        sidecar_path=str(sidecar),
        sidecar_digest=digest,
    )
    if not accepted:
        return
    record_acceptance(
        store,
        item_id=item_id,
        repo=repo,
        pr_number=None,
        round_num=round_num,
        head_sha=head,
        sidecar_digest=digest,
        issue=99,
        after_rounds=round_num,
        findings=[],
    )
    mark_round_accepted(store, item_id, store.latest_review_round(item_id), issue=99)
    assert store.latest_review_round(item_id).accepted_verdict is not None
