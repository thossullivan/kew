"""Service shim: the inbox reaches approve/deny only through here (kew.serve
may not import kew.approval, which pulls in kew.audit/run_store)."""

from __future__ import annotations

from typing import Any

from kew.approval import apply_approval_decision
from kew.report import open_approval_holds
from kew.service.models import ApprovalResultDTO, HoldDTO


def list_open_holds(store: Any) -> list[HoldDTO]:
    return [
        HoldDTO(issue_num=h.issue_num, estimate_usd=h.estimate_usd, basis=h.basis)
        for h in open_approval_holds(store)
    ]


def decide_hold(
    action: str, *, issue_num: int, reason: str, config: Any, source: str = "web"
) -> ApprovalResultDTO:
    status = apply_approval_decision(
        action, issue_num=issue_num, reason=reason, config=config, source=source
    )
    return ApprovalResultDTO(status=status)
