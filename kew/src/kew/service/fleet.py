"""Operator services for authenticated run stops and fleet control."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import Any, Literal

from kew import fleet
from kew.config import DispatchConfig
from kew.fleet import stop_fleet
from kew.run_liveness import launch_runtime
from kew.run_store import RunRecord, RunStore
from kew.service.models import FleetStatusDTO, FleetStopResultDTO


@dataclass(frozen=True)
class RunStopResult:
    """The observed stop outcome, shared by CLI and TUI notifications."""

    outcome: Literal["signalled", "exited", "unverifiable", "not_pending", "not_found"]
    message: str


def stop_run(store: RunStore, run_id: str, config: DispatchConfig) -> RunStopResult:
    """Persist stop intent, reload identity, and stop only authenticated groups."""
    record = store.get(run_id)
    if record is None:
        return RunStopResult("not_found", f"Run {run_id} was not found")
    if record.status != "pending":
        return RunStopResult("not_pending", f"Run {run_id} is no longer pending")
    store.request_run_stop(
        run_id,
        label_reset={
            "repo": config.project.repo,
            "tracker": config.tracker.type,
            "ready": config.labels.ready,
            "in_progress": config.labels.in_progress,
            "agent_working": config.labels.agent_working,
            "runtime": launch_runtime(record, config),
        },
        reset_labels=partial(fleet._reset_stop_labels, config=config),
    )
    record = store.get(run_id)
    if record is None or record.status != "pending":
        return RunStopResult("not_pending", f"Run {run_id} is no longer pending")
    targets, note = _stop_run_groups(store, record)
    _finish_run_stop(store, record, config)
    if targets:
        return RunStopResult(
            "signalled",
            f"Run {run_id}: authenticated and signalled; stop intent recorded",
        )
    if fleet.attempt_is_dead(record):
        return RunStopResult(
            "exited", f"Run {run_id}: attempt already exited; nothing signalled; {note}"
        )
    return RunStopResult(
        "unverifiable",
        f"Run {run_id}: identity unverifiable; nothing signalled; {note}"
        + (
            f"; if its container never started, `kew settle {run_id}` reaps it"
            if record.container_name and record.attempt_pgid is None
            else ""
        ),
    )


@dataclass(frozen=True)
class RunSettleResult:
    """What ``kew settle`` did to one run whose container never started."""

    outcome: Literal["settled", "refused", "removed_only", "not_pending", "not_found"]
    message: str


def settle_run(store: RunStore, run_id: str) -> RunSettleResult:
    """Reap one dead run whose container never started, by hand (#3124).

    The proof is the reconcilers' own: an owner proven dead past the startup
    grace, no attempt ever registered, and a container ``created`` with pid 0
    or absent. An owner whose liveness is unknown is refused, never reaped.
    The reap records its intent, removes the container by id, finalizes the
    run as ``container_start_failed`` (``killed`` for a run already stopped),
    frees its slot and writes the reap event. A container removed for a run
    that then could not be finalized is reported as removed, never as
    nothing changed.
    """
    record = store.get(run_id)
    if record is None:
        return RunSettleResult("not_found", f"Run {run_id} was not found")
    if record.status != "pending":
        return RunSettleResult("not_pending", f"Run {run_id} is no longer pending")
    reap = store.settle_never_started(record, actor=("human", "operator"))
    if reap.reaped:
        stopped = "; it had been stopped" if reap.category == "killed" else ""
        return RunSettleResult(
            "settled",
            f"Run {run_id}: container never started and its owner is dead"
            f"{stopped}; container removed, run finalized as {reap.category}, "
            "slot freed",
        )
    if reap.removed is not None:
        return RunSettleResult(
            "removed_only", f"Run {run_id}: not settled; {reap.detail}"
        )
    return RunSettleResult(
        "refused", f"Run {run_id}: not settled; {reap.detail}; nothing changed"
    )


def _stop_run_groups(store: RunStore, record: RunRecord) -> tuple[list[int], str]:
    """Share fleet authentication and escalation, then reconsider the owner."""
    groups = fleet._attempt_groups(record)
    if not groups:
        return [], "attempt registration is unconfirmed"
    retained: dict[int, dict[int, str]] = {}
    targets, note = fleet._stop_host_attempts(record, retained, store)
    _wait_run_groups(store, record, targets, retained)
    if record.pgid not in targets:
        owners: list[int] = []
        notes = [note] if note else []
        fleet._stop_owner_after_attempts(record, groups, owners, notes, retained, store)
        _wait_run_groups(store, record, owners, retained)
        targets.extend(owners)
        note = "; ".join(notes)
    return targets, note


def _wait_run_groups(
    store: RunStore,
    record: RunRecord,
    targets: list[int],
    retained: dict[int, dict[int, str]],
) -> None:
    fleet._kill_survivors(
        targets,
        fleet._GRACE_SECONDS,
        owners={pid: record for pid in targets if pid == record.pgid},
        attempts={pid: record for pid in targets if pid != record.pgid},
        retained=retained,
        store=store,
    )


def _finish_run_stop(
    store: RunStore, record: RunRecord, config: DispatchConfig
) -> None:
    """Keep unresolved rows pending until the shared stop proofs succeed."""
    killed = [
        fleet.KilledRun(record.run_id, record.issue_num, record.estimated_cost_usd)
    ]
    now = datetime.now(UTC)
    if record.container_name:
        fleet._container_is_stopped(record, config)
        fleet._finish_contained_stops(store, config, [record], killed, now)
    else:
        fleet._finish_host_stops(store, [record], killed, now)


def fleet_status(store: Any) -> FleetStatusDTO:
    stopped = store.get_fleet_stopped() is not None
    return FleetStatusDTO(stopped=stopped, pending=len(store.list_pending()))


def stop_fleet_service(
    *,
    reason: str,
    config: Any,
    store_factory: Callable[[], AbstractContextManager[Any]],
) -> FleetStopResultDTO:
    with store_factory() as store:
        report = stop_fleet(store, config, reason=reason)
    return FleetStopResultDTO(
        stopped=True,
        already_stopped=report.already_stopped,
        runs_killed=[
            {
                "run_id": k.run_id,
                "issue": k.issue_num,
                "item_id": k.item_id,
                "cost_usd": k.cost_usd,
            }
            for k in report.killed
        ],
    )
