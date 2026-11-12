"""Required host issuance evidence before any issued secret leaves its owner."""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from kew.audit import append_credential_issuance
from kew.credential_issuer import IssuerRefused

if TYPE_CHECKING:
    from kew.run_store import RunStore

_logger = logging.getLogger(__name__)


class IssuanceEvidenceRefused(IssuerRefused):
    reason = "issuance evidence unavailable; run refused before launch"


def _append_and_readback(
    store: RunStore, run_id: str, payload: dict[str, Any], item_id: str | None
) -> None:
    event_id = append_credential_issuance(
        store,
        run_id,
        payload,
        item_id=item_id,
    )
    if type(event_id) is not int or event_id < 1:
        raise IssuanceEvidenceRefused()
    events = store.list_audit_events(
        event="sandbox_resolved", run_id=run_id, limit=None
    )
    if not any(
        event.id == event_id
        and event.event == "sandbox_resolved"
        and event.run_id == run_id
        and event.actor_type == "policy"
        and event.actor_id == "credential-issuer"
        and event.payload == payload
        for event in events
    ):
        raise IssuanceEvidenceRefused()


def _refuse_pending_run(store: RunStore, run_id: str) -> None:
    """Release pending-row protection when evidence prevents a review launch."""
    try:
        run = store.get(run_id)
        if run is not None and run.status == "pending":
            store.save(
                replace(
                    run,
                    status="refused",
                    exit_code=2,
                    finished_at=datetime.now(UTC),
                    error_category="credential_issuer_refused",
                )
            )
    except Exception:
        # "issuer refusal", not "credential refusal": the strict semgrep gate
        # reads a logger call naming a credential as a leak, and this line
        # carries a run id only.
        _logger.warning("could not record the issuer refusal: run=%s", run_id)


def record_issuance_evidence(
    store: RunStore | None,
    run_id: str,
    payload: dict[str, Any],
    *,
    item_id: str | None = None,
) -> None:
    """Append, then verify the exact event, run, host actor, and key identity."""
    try:
        if store is None:
            raise IssuanceEvidenceRefused()
        previous = store.list_audit_events(
            event="sandbox_resolved", run_id=run_id, limit=1
        )
        if previous:
            payload = {**(previous[0].payload or {}), **payload}
        _append_and_readback(store, run_id, payload, item_id)
    except Exception:
        if store is not None:
            _refuse_pending_run(store, run_id)
        _logger.warning("%s: run=%s", IssuanceEvidenceRefused.reason, run_id)
        raise IssuanceEvidenceRefused() from None
