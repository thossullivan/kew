from contextlib import nullcontext
from unittest.mock import patch

from kew.service.fleet import fleet_status, stop_fleet_service
from kew.service.models import FleetStatusDTO


class _Store:
    def __init__(self, stopped, pending):
        self._stopped = stopped
        self._pending = pending

    def get_fleet_stopped(self):
        return {"reason": "x"} if self._stopped else None

    def list_pending(self):
        return self._pending


def test_fleet_status_maps():
    assert fleet_status(_Store(True, [1, 2])) == FleetStatusDTO(stopped=True, pending=2)
    assert fleet_status(_Store(False, [])) == FleetStatusDTO(stopped=False, pending=0)


def test_stop_fleet_service_wraps_report():
    from kew.fleet import FleetStopReport, KilledRun

    report = FleetStopReport(
        reason="manual",
        stopped_at="2026-06-27T00:00:00Z",
        already_stopped=False,
        killed=[
            KilledRun(run_id="r1", issue_num=9, item_id="9", cost_usd=0.5, note="")
        ],
    )
    store = _Store(False, [])
    with patch("kew.service.fleet.stop_fleet", return_value=report) as m:
        dto = stop_fleet_service(
            reason="manual", config=object(), store_factory=lambda: nullcontext(store)
        )
    assert dto.stopped is True
    assert dto.already_stopped is False
    assert dto.runs_killed == [
        {"run_id": "r1", "issue": 9, "item_id": "9", "cost_usd": 0.5}
    ]
    assert m.call_args.kwargs["reason"] == "manual"
