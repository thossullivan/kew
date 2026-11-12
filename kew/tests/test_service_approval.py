from unittest.mock import patch

from kew.service.approval import decide_hold, list_open_holds
from kew.service.models import ApprovalResultDTO, HoldDTO


def test_list_open_holds_maps_dto():
    from kew.report import ApprovalHold

    fake = [ApprovalHold(issue_num=5, estimate_usd=3.0, basis="median")]
    with patch("kew.service.approval.open_approval_holds", return_value=fake):
        out = list_open_holds(object())
    assert out == [HoldDTO(issue_num=5, estimate_usd=3.0, basis="median")]


def test_decide_hold_passes_source_web_and_wraps_status():
    with patch(
        "kew.service.approval.apply_approval_decision", return_value="released"
    ) as m:
        dto = decide_hold(
            "approve", issue_num=5, reason="ok", config=object(), source="web"
        )
    assert dto == ApprovalResultDTO(status="released")
    assert m.call_args.kwargs["source"] == "web"
    assert m.call_args.args[0] == "approve"
