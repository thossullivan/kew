"""Shared process and container liveness probes for operator recovery."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from kew import process_groups, process_termination, stop_recovery
from kew.runtime_names import OPENSHELL_RUNTIME

if TYPE_CHECKING:
    from kew.config import DispatchConfig
    from kew.run_store import ContinuationRecord, RunRecord, RunStore


_logger = logging.getLogger(__name__)


def continuation_owner_death_refusal(
    store: RunStore,
    record: ContinuationRecord,
    config: DispatchConfig,
    *,
    own_claim: str | None = None,
) -> str | None:
    """Probe known owners and name any history gap that prevents abandonment.

    *own_claim* is the caller's own continuation run, when the caller is the
    child a claim launched (#2964): it owns the checkout now, so it is the one
    owner not probed.
    """
    try:
        owner_ids = [
            run_id
            for run_id in store.continuation_owner_run_ids(record)
            if run_id != own_claim
        ]
        refusals = [_owner_death_refusal(store, run_id, config) for run_id in owner_ids]
        store.require_continuation_owner_history(record)
    except (RuntimeError, ValueError, OverflowError) as exc:
        # Invalid history cannot establish death. SQLite errors still propagate.
        _logger.warning(
            "continuation %s/%s (run %s): owner history is invalid, "
            "so its owners are treated as possibly live: %s",
            record.epic_ref,
            record.item_id,
            record.original_run_id,
            exc,
        )
        return f"owner history is invalid: {exc}"
    return next((reason for reason in refusals if reason is not None), None)


def _owner_death_refusal(
    store: RunStore, run_id: str, config: DispatchConfig
) -> str | None:
    """A fleet or reconciler status write cannot prove an unrecorded worker dead."""
    run = store.get(run_id)
    reason = f"live or unproven owner run {run_id!r}"
    if run is None:
        return f"{reason}: run history is missing"
    if _requires_owner_probe(run):
        return (
            None
            if _recorded_owner_is_dead(run, config)
            else f"{reason}; wait until the owner stops"
        )
    # These rows can be written outside dispatch without observing worker exit.
    # Other terminal rows retain the dispatch's own attempt result and exit code.
    if run.status != "killed" and run.error_category not in {
        "dispatch_child_died",
        "orphaned_pending_reaped",
        "orphaned_pending_preserved",
    }:
        return None
    if _dispatch_completed(store, run):
        return None
    return (
        f"{reason}: no recorded worker identity or matching dispatch_completed "
        f"exit evidence for attempt {run.attempt_number}"
    )


def _requires_owner_probe(run: RunRecord) -> bool:
    return run.status == "pending" or any(
        (run.pgid, run.owner_pid, run.attempt_pgid, run.container_name)
    )


def _recorded_owner_is_dead(run: RunRecord, config: DispatchConfig) -> bool:
    """Use recorded dispatch ownership and independent attempt death evidence.

    An owner from an earlier boot of this machine is dead whatever its
    recorded pids name now (#2960), but that proves nothing about a container:
    its daemon outlives the boot it launched under, so the container is still
    probed.
    """
    host_died = host_died_under(run)
    owner = (
        False
        if host_died
        else process_groups.owner_alive(
            run.pgid or 0,
            run.owner_pid,
            run.owner_start_time,
            run.owner_ppid,
            run.owner_command,
            run.owner_start_token,
        )
    )
    if run.container_name:
        if (run.pgid or run.owner_pid) and owner is not False:
            return False
        return recorded_container_stopped(run, config)
    return host_died or (owner is False and process_termination.attempt_is_dead(run))


def recorded_container_stopped(run: RunRecord, config: DispatchConfig) -> bool:
    """Is the run's recorded container stopped or absent, asked of its runtime?

    A missing binary or a failed probe reads "unknown", never absent.
    """
    runtime = launch_runtime(run, config)
    state = stop_recovery.record_container_state(
        runtime, run, probe=container_state_probe(runtime, config)
    )
    return state in {"stopped", "absent"}


def host_died_under(run: RunRecord) -> bool:
    """Did this machine reboot under the run's owner (#2812)?

    The owner recorded the boot it ran under. A run from an earlier boot of
    this machine lost every process it had with that boot, so it is dead now —
    not after the startup grace — and its pids, which a later boot may have
    handed to strangers, must never be probed or signalled as its own. A run
    placed on another machine is not this boot's to judge, and neither is an
    OpenShell run: its agent lives in the gateway, which a local reboot does
    not stop, so only the OpenShell probe can prove it dead.
    """
    from kew.fleet_placement import is_remote

    return (
        not is_remote(run)
        and OPENSHELL_RUNTIME not in (run.container_runtime, run.cleanup_runtime)
        and process_groups.from_earlier_boot(run.owner_boot_id)
    )


def container_state_probe(
    runtime: str, config: DispatchConfig
) -> stop_recovery.ContainerStateProbe | None:
    """The gateway state answer ``stop_recovery`` cannot reach for itself.

    ``None`` for every runtime whose state ``stop_recovery`` reads directly —
    it is only ``openshell`` that is a gateway call, and only a caller holding
    the configuration can name the gateway to ask. Built here, above the
    store, because the backend reaches the subprocess chokepoint (see
    :data:`kew.stop_recovery.ContainerStateProbe`).
    """
    if runtime != OPENSHELL_RUNTIME:
        return None
    from kew.runtime_backend import backend_for

    return backend_for(runtime, openshell=config.sandbox.openshell).state


def launch_runtime(run: RunRecord, config: DispatchConfig) -> str:
    """The runtime the run launched under, from its row (re-audit F12).

    Probing with the current config runtime after a docker/podman switch read
    a live container as absent and released its reservation and key. The
    recorded runtime's answer is trusted, "absent" included; only a row with
    no recorded runtime falls back to the configured one.
    """
    return run.cleanup_runtime or run.container_runtime or config.sandbox.runtime


def _dispatch_completed(store: RunStore, run: RunRecord) -> bool:
    """Require this run's dispatch exit, scoped to its current attempt and start."""
    return any(
        event.timestamp >= run.started_at.isoformat()
        and isinstance(event.payload, dict)
        and type(event.payload.get("attempt_number")) is int
        and event.payload["attempt_number"] == run.attempt_number
        and run.attempt_number > 0
        and type(event.payload.get("exit_code")) is int
        for event in store.list_audit_events(
            limit=None, event="dispatch_completed", run_id=run.run_id
        )
    )
