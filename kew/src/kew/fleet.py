"""Fleet kill switch: stop all in-flight agent runs and latch dispatch.

`stop_fleet()` engages the latch FIRST (so nothing new dispatches), then
terminates every pending run's process group (SIGTERM -> grace -> SIGKILL),
marks runs `killed`, and resets issue labels to ready. Worktrees are
preserved. `resume_fleet()` clears the latch.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import cast

from kew import process_termination
from kew.audit import emit, resolve_actor
from kew.config import DispatchConfig
from kew.labels import reset_issue_to_ready
from kew.process_groups import (
    attempt_from_events,
    attempt_identities_from_events,
    host_stop_is_dead,
    read_event_evidence,
)
from kew.process_groups import (
    authenticated_group_members as authenticated_group_members,
)
from kew.process_groups import (
    group_alive as group_alive,
)
from kew.process_groups import (
    group_member_identities as group_member_identities,
)
from kew.process_groups import (
    identity_matches as identity_matches,
)
from kew.process_groups import (
    owner_signallable as owner_signallable,
)
from kew.process_groups import (
    process_exited as process_exited,
)
from kew.process_groups import (
    process_identity as process_identity,
)
from kew.process_groups import (
    recovered_attempt_signallable as recovered_attempt_signallable,
)
from kew.process_groups import retained_identity_matches as retained_identity_matches
from kew.process_groups import (
    start_token_matches as start_token_matches,
)
from kew.process_termination import (
    _attempt_groups as _attempt_groups,
)
from kew.process_termination import (
    _attempt_identity as _attempt_identity,
)
from kew.process_termination import (
    _attempt_owner_note as _attempt_owner_note,
)
from kew.process_termination import (
    _attempt_unverifiable as _attempt_unverifiable,
)
from kew.process_termination import (
    _contained_client_dead_or_legacy as _contained_client_dead_or_legacy,
)
from kew.process_termination import (
    _kill_retained_members as _kill_retained_members,
)
from kew.process_termination import (
    _registered_attempt as _registered_attempt,
)
from kew.process_termination import (
    _retain_members as _retain_members,
)
from kew.process_termination import (
    _retained_owner_matches as _retained_owner_matches,
)
from kew.progress import _warn
from kew.run_liveness import launch_runtime
from kew.run_store import RunRecord, RunStore
from kew.runtime_backend import RuntimeBackendUnavailable, backend_for
from kew.stop_recovery import (
    CONTAINER_INSPECT_FORMAT,
    contained_stop_reason,
    container_target,
    inspected_container_state,
)
from kew.subprocess_utils import TOOL_PATHS, run

_GRACE_SECONDS = 10.0
_ESCALATION_ROUNDS = 3
_ESCALATION_PAUSE_SECONDS = 0.02


def _pgid_command(pgid: int) -> str | None:
    """Return the command name of the process-group leader, or None if dead.

    With start_new_session=True the group leader's pid == pgid, so we can
    ask ps about the pid directly. ps may print a full path on macOS.
    """
    if "ps" not in TOOL_PATHS:
        import shutil

        ps_path = shutil.which("ps")
        if ps_path is None:
            return None
        TOOL_PATHS["ps"] = ps_path
    result = run(["ps", "-o", "pgid=,comm=", "-p", str(pgid)], check=False, timeout=10)
    if result.returncode != 0 or not result.stdout.strip():
        return None
    for line in result.stdout.strip().splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[0].strip() == str(pgid):
            return Path(parts[1].strip()).name
    return None


def _group_alive(pgid: int) -> bool:
    """Keep waiting until process group death is positively confirmed."""
    return group_alive(pgid) is not False


def _signal_group(pgid: int, sig: int) -> None:
    """Send a signal to a process group, ignoring already-dead groups."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, sig)


def _pgid_from_events(record: RunRecord) -> list[int]:
    """Recover all spawn claims; these are not authenticated signal targets."""
    return attempt_from_events(record.log_file)


def _cost_from_events(record: RunRecord) -> float:
    """Best-effort spend-to-date: latest tokens.cost_usd from the events file."""
    cost = record.estimated_cost_usd
    if record.log_file is None:
        return cost
    event_path = record.log_file.with_suffix(".events.jsonl")
    for event in read_event_evidence(event_path):
        if event is not None and event.get("type") == "tokens":
            value = event.get("cost_usd") or 0.0
            if isinstance(value, (str, int, float)):
                with contextlib.suppress(ValueError):
                    cost = float(value)
    return cost


@dataclass
class KilledRun:
    """One run terminated (or found stale) by stop_fleet."""

    run_id: str
    issue_num: int
    cost_usd: float
    # Durable work-item id (string) alongside the legacy numeric ``issue_num``.
    # For a non-numeric tracker (Linear) ``issue_num`` is the 0 sentinel and
    # this carries the real key; GitHub rows stringify their number.
    item_id: str = ""
    note: str = ""


@dataclass
class FleetStopReport:
    """Outcome summary of a stop_fleet() invocation."""

    reason: str
    stopped_at: str
    already_stopped: bool
    killed: list[KilledRun] = field(default_factory=list)
    retained: list[KilledRun] = field(default_factory=list)


def attempt_is_dead(record: RunRecord) -> bool:
    return process_termination.attempt_is_dead(record, probes=_signal_probes())


def _signal_probes() -> process_termination.SignalProbes:
    return cast(process_termination.SignalProbes, sys.modules[__name__])


def _validated_run_attempt(pgid: int, record: RunRecord) -> tuple[int | None, str]:
    return process_termination._validated_run_attempt(
        pgid, record, probes=_signal_probes()
    )


def _validated_owner(record: RunRecord) -> tuple[int | None, str]:
    return process_termination._validated_owner(record, probes=_signal_probes())


def _live_agent_pgid(record: RunRecord) -> tuple[int | None, str]:
    """Resolve a signallable pgid for a pending run, guarding PID reuse.

    Returns (pgid, note): pgid is None when there is nothing safe to
    signal, with *note* explaining why; note is "" for a live kew agent.

    Attempts take priority. An exited attempt falls back to its dispatch owner.
    Event evidence also covers a failed registration during a retry.
    """
    unresolved = ""
    for attempt in _attempt_groups(record):
        target, note = _validated_run_attempt(attempt, record)
        if target is not None:
            return target, note
        if group_alive(attempt) is not False:
            unresolved = note
    if unresolved:
        return None, unresolved
    if record.pgid:
        return _validated_owner(record)
    return None, "stale: no live process group recorded"


def stop_orphaned_attempt(record: RunRecord, store: RunStore | None = None) -> bool:
    """Stop surviving members and confirm death before finalization.

    Repeated cleanup must pass its store to retain births across row reloads.
    """
    if record.container_name and _contained_client_dead_or_legacy(record):
        return True
    if attempt_is_dead(record):
        return True
    attempts = _attempt_groups(record)
    if not attempts:
        _warn(f"Retaining run {record.run_id}: attempt registration is unconfirmed")
        return False
    for attempt in attempts:
        _stop_orphaned_group(attempt, record, store)
    dead = attempt_is_dead(record)
    if record.container_name and not dead:
        _warn(f"Retaining run {record.run_id}: contained_client_termination_unproven")
    return dead


def _stop_orphaned_group(
    attempt: int, record: RunRecord, store: RunStore | None
) -> None:
    """Authenticate one orphan group and retain authority across escalation."""
    if group_alive(attempt) is False:
        return
    if attempt in record.termination_members:
        _kill_survivors(
            [attempt],
            0,
            attempts={attempt: record},
            retained=record.termination_members,
            store=store,
        )
        return
    pgid, note = _validated_run_attempt(attempt, record)
    if pgid is None:
        _warn(f"Retaining run {record.run_id}: {note}")
        return
    retained: dict[int, dict[int, str]] = {}
    if _terminate_group(pgid, record, retained, store):
        _kill_survivors(
            [pgid],
            _GRACE_SECONDS,
            attempts={pgid: record},
            retained=retained,
            store=store,
        )


def _mark_run_killed(store: RunStore, record: RunRecord, now: datetime) -> None:
    """Persist a run as killed, capturing best-effort spend to date."""
    record.status = "killed"
    record.error_category = "killed"
    record.finished_at = now
    record.estimated_cost_usd = _cost_from_events(record)
    store.save(record)


def _kill_survivors(
    signalled: list[int],
    grace_seconds: float,
    owners: dict[int, RunRecord] | None = None,
    attempts: dict[int, RunRecord] | None = None,
    retained: dict[int, dict[int, str]] | None = None,
    store: RunStore | None = None,
) -> None:
    """Shared grace window after SIGTERM, then SIGKILL surviving groups."""
    owners = owners or {}
    attempts = attempts or {}
    survivors = _wait_for_groups(signalled, grace_seconds)
    for pgid in survivors:
        if retained is not None and pgid in retained:
            _kill_retained_group(
                pgid, retained[pgid], attempts.get(pgid) or owners.get(pgid), store
            )
            continue
        _warn(f"Process group {pgid}: no retained signal identity; not signalled")
    # SIGKILL delivery is asynchronous. Do not release ownership until a fresh
    # scan confirms death, including the short interval before process exit.
    _wait_for_groups(survivors, grace_seconds)


def _kill_retained_group(
    pgid: int,
    identities: dict[int, str],
    record: RunRecord | None,
    store: RunStore | None,
) -> None:
    process_termination._kill_retained_group(
        pgid,
        identities,
        record,
        store,
        wait_for_groups=_wait_for_groups,
        probes=_signal_probes(),
    )


def _terminate_group(
    pgid: int,
    record: RunRecord,
    retained: dict[int, dict[int, str]],
    store: RunStore | None = None,
) -> bool:
    return process_termination._terminate_group(
        pgid,
        record,
        retained,
        store,
        signal_group=_signal_group,
        probes=_signal_probes(),
    )


def _wait_for_groups(groups: list[int], grace_seconds: float) -> list[int]:
    """Wait within one deadline and return groups whose death remains unproven."""
    deadline = time.monotonic() + grace_seconds
    survivors = [pgid for pgid in groups if _group_alive(pgid)]
    while survivors and time.monotonic() < deadline:
        time.sleep(min(0.02, max(0, deadline - time.monotonic())))
        survivors = [pgid for pgid in survivors if _group_alive(pgid)]
    return survivors


def _stop_host_attempts(
    record: RunRecord,
    retained: dict[int, dict[int, str]] | None = None,
    store: RunStore | None = None,
) -> tuple[list[int], str]:
    """Signal all authenticated attempts; fall back to the owner after attempt death."""
    if (
        record.attempt_pgid is None
        and attempt_identities_from_events(record.log_file) is None
    ):
        return [], "event evidence unavailable; malformed or unreadable event line"
    groups = _attempt_groups(record)
    if retained is None:
        retained = {}
    targets = []
    notes = []
    for group in groups:
        if group in record.termination_members:
            retained[group] = record.termination_members[group]
            targets.append(group)
            continue
        target, note = _validated_run_attempt(group, record)
        if target is not None:
            if _terminate_group(target, record, retained, store):
                targets.append(target)
            else:
                notes.append("attempt member identities unavailable")
        elif note:
            notes.append(note)
    _stop_owner_after_attempts(record, groups, targets, notes, retained, store)
    return targets, "; ".join(notes)


def _stop_owner_after_attempts(
    record: RunRecord,
    groups: list[int],
    targets: list[int],
    notes: list[str],
    retained: dict[int, dict[int, str]],
    store: RunStore | None = None,
) -> None:
    """Fall back to owner termination only after every attempt is empty."""
    if not record.container_name and all(
        group_alive(group) is False for group in groups
    ):
        if record.pgid in record.termination_members:
            assert record.pgid is not None
            retained[record.pgid] = record.termination_members[record.pgid]
            targets.append(record.pgid)
            return
        target, note = _live_agent_pgid(record)
        if target is not None:
            if _terminate_group(target, record, retained, store):
                targets.append(target)
            else:
                notes.append("owner member identities unavailable")
        elif note:
            notes.append(note)


def _finish_host_stops(
    store: RunStore, records: list[RunRecord], killed: list[KilledRun], now: datetime
) -> list[KilledRun]:
    """Finalize confirmed stops; retain unresolved rows and their ownership."""
    retained = []
    by_id = {entry.run_id: entry for entry in killed}
    for record in records:
        target = record.attempt_pgid or record.pgid
        if host_stop_is_dead(
            record.pgid,
            record.owner_pid,
            record.attempt_pgid,
            record.log_file,
            record.owner_start_time,
            record.owner_ppid,
            record.owner_command,
            record.owner_start_token,
        ):
            _mark_run_killed(store, record, now)
            continue
        entry = by_id[record.run_id]
        reason = (
            "owner identity unknown; explicit operator resolution required"
            if record.pgid is None or record.owner_pid != record.pgid
            else f"owner or attempt death unproven for group {target}"
        )
        entry.note = f"retained: {reason}; {entry.note}"
        killed.remove(entry)
        retained.append(entry)
        _warn(f"Run {record.run_id}: {entry.note}")
    return retained


def _recorded_runtime(record: RunRecord, config: DispatchConfig) -> str | None:
    """The run's launch runtime, registered for this probe, else None.

    After a docker/podman switch the configured runtime reads a container
    still running under the recorded one as absent (re-audit F12). A
    recorded runtime whose binary this host lacks is unknown, never absent.
    A recorded runtime this build has no backend for is unknown too, and says
    so: pinning capacity in silence is what that branch must never do.
    """
    runtime = launch_runtime(record, config)
    if runtime != config.sandbox.runtime and runtime not in TOOL_PATHS:
        try:
            path = backend_for(runtime).probe()
        except RuntimeBackendUnavailable as exc:
            _warn(f"Run {record.run_id}: {exc}")
            return None
        if path is None:
            return None
        TOOL_PATHS[runtime] = path
    return runtime


def _probe_contained_state(record: RunRecord, config: DispatchConfig) -> str:
    identity = container_target(record)
    runtime = _recorded_runtime(record, config)
    if identity is None or runtime is None:
        return "unknown"
    result = run(
        [
            runtime,
            "inspect",
            "--format",
            CONTAINER_INSPECT_FORMAT,
            identity,
        ],
        check=False,
        timeout=10,
    )
    return inspected_container_state(result, identity, record.container_name or "")


def _container_is_stopped(record: RunRecord, config: DispatchConfig) -> bool:
    """Validate the recorded name, signal only its id, then inspect that id again."""
    identity = container_target(record)
    if identity is None:
        return False
    try:
        state = _probe_contained_state(record, config)
        if state in {"absent", "stopped"}:
            return True
        if state != "running":
            return False
        runtime = _recorded_runtime(record, config)
        if runtime is None:
            return False
        run([runtime, "kill", identity], check=False, timeout=10)
        return _probe_contained_state(record, config) in {"absent", "stopped"}
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        _warn(f"Cannot confirm container stop for {record.run_id}: {exc}")
        return False


def _finish_contained_stops(
    store: RunStore,
    config: DispatchConfig,
    records: list[RunRecord],
    killed: list[KilledRun],
    now: datetime,
) -> list[KilledRun]:
    """Recheck the container after client exit; an alive client can still create it."""
    retained = []
    by_id = {entry.run_id: entry for entry in killed}
    for record in records:
        reason = contained_stop_reason(
            record, now, partial(_contained_stop_state, record, config)
        )
        if reason:
            by_id[record.run_id].note = reason
            _mark_run_killed(store, record, now)
            continue
        entry = by_id[record.run_id]
        entry.note = (
            f"retained: runtime client death or container stop unproven; {entry.note}"
        )
        killed.remove(entry)
        retained.append(entry)
        _warn(f"Run {record.run_id}: {entry.note}")
    return retained


def _contained_stop_state(record: RunRecord, config: DispatchConfig) -> str:
    """Require immutable proof for registered and legacy contained rows alike."""
    if _attempt_groups(record):
        return "stopped" if _container_is_stopped(record, config) else "unknown"
    try:
        return _probe_contained_state(record, config)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        _warn(f"Cannot confirm legacy container absence for {record.run_id}: {exc}")
        return "unknown"


def _reset_stop_labels(
    issue: int, task: dict[str, str], *, config: DispatchConfig
) -> None:
    """Preserve the stopped row's repository and labels across config changes."""
    if task["tracker"] != "github":
        return
    target = config.model_copy(deep=True)
    target.project.repo = task["repo"]
    target.tracker = target.tracker.model_copy(update={"type": "github"})
    target.labels.ready = task["ready"]
    target.labels.in_progress = task["in_progress"]
    target.labels.agent_working = task["agent_working"]
    reset_issue_to_ready(issue, target)


def _emit_fleet_stop_event(
    store: RunStore, config: DispatchConfig, reason: str, killed: list[KilledRun]
) -> None:
    """Record the fleet_stop audit event for this invocation."""
    emit(
        store,
        "fleet_stop",
        actor=resolve_actor(),
        repo=config.project.repo or None,
        payload={
            "reason": reason,
            "runs_killed": [
                {
                    "run_id": k.run_id,
                    "issue": k.issue_num,
                    "cost_usd": k.cost_usd,
                    "note": k.note,
                }
                for k in killed
            ],
        },
    )


def stop_fleet(
    store: RunStore,
    config: DispatchConfig,
    *,
    reason: str = "",
    grace_seconds: float = _GRACE_SECONDS,
) -> FleetStopReport:
    """Kill every in-flight run and latch the fleet.

    Order is load-bearing:
    1. Latch FIRST — no new dispatches, no worker retries.
    2. Persist stop intent before signalling so surviving workers cannot retry.
       Authenticate and stop every registered client group.
       Also request container termination for contained runs.
    3. One shared grace window, then revalidate and SIGKILL survivors.
       Confirm host owner death, or container stop after client death,
       before finalizing rows or resetting labels.
    4. Reset issue labels to ready (idempotent).
    5. Emit a fleet_stop audit event.

    Worktrees are never removed. Idempotent: a second call finds the
    latch set (original timestamp/reason preserved). Unresolved rows stay pending.
    """
    existing = store.get_fleet_stopped()
    already_stopped = existing is not None
    if existing is None:
        store.set_fleet_stopped(reason)
    latch = store.get_fleet_stopped()
    assert latch is not None  # we just set it

    reset_labels = partial(_reset_stop_labels, config=config)
    store.retry_stop_label_resets(reset_labels=reset_labels)
    now = datetime.now(UTC)
    killed: list[KilledRun] = []
    signalled: list[int] = []
    owners: dict[int, RunRecord] = {}
    attempts: dict[int, RunRecord] = {}
    retained_members: dict[int, dict[int, str]] = {}
    host_records: list[RunRecord] = []
    contained_records: list[RunRecord] = []

    for record in store.list_pending():
        # Publish stop intent before signalling so workers cannot retry.
        store.request_run_stop(
            record.run_id,
            label_reset={
                "repo": config.project.repo,
                "tracker": config.tracker.type,
                "ready": config.labels.ready,
                "in_progress": config.labels.in_progress,
                "agent_working": config.labels.agent_working,
                "runtime": launch_runtime(record, config),
            },
            reset_labels=reset_labels,
        )
        record = store.get(record.run_id) or record

        targets, note = _stop_host_attempts(record, retained_members, store)
        signalled.extend(targets)
        for pgid in targets:
            if pgid == record.pgid:
                owners[pgid] = record
            else:
                attempts[pgid] = record
        killed.append(
            KilledRun(
                run_id=record.run_id,
                issue_num=record.issue_num,
                item_id=record.item_id or str(record.issue_num),
                cost_usd=record.estimated_cost_usd,
                note=note,
            )
        )
        if record.container_name is not None:
            contained_records.append(record)
            _container_is_stopped(record, config)
        else:
            host_records.append(record)

    _kill_survivors(signalled, grace_seconds, owners, attempts, retained_members, store)
    retained = _finish_host_stops(store, host_records, killed, now)
    retained.extend(
        _finish_contained_stops(store, config, contained_records, killed, now)
    )
    _emit_fleet_stop_event(store, config, latch.get("reason", reason), killed)

    return FleetStopReport(
        reason=latch.get("reason", ""),
        stopped_at=latch.get("stopped_at", ""),
        already_stopped=already_stopped,
        killed=killed,
        retained=retained,
    )


def resume_fleet(store: RunStore, config: DispatchConfig) -> bool:
    """Clear the fleet-stop latch. Returns False when it was not set."""
    store.reconcile_host_stops()
    if store.get_fleet_stopped() is None:
        return False
    store.clear_fleet_stopped()
    emit(
        store,
        "fleet_resume",
        actor=resolve_actor(),
        repo=config.project.repo or None,
    )
    return True


def format_relative_time(stopped_at: str) -> str:
    """Render an ISO timestamp as a coarse relative string ('5m ago').

    Falls back to the raw value when it cannot be parsed, or 'unknown
    time' when empty. Shared by the CLI and TUI fleet-stop banners so
    both render the timestamp identically.
    """
    if not stopped_at:
        return "unknown time"
    try:
        ts = datetime.fromisoformat(stopped_at)
    except ValueError:
        return stopped_at
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    delta = (datetime.now(UTC) - ts).total_seconds()
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


def fleet_stop_message(latch: dict[str, str]) -> str:
    """Human-readable one-liner describing the engaged latch."""
    reason = latch.get("reason") or "no reason given"
    when = format_relative_time(latch.get("stopped_at") or "")
    return f"FLEET STOPPED {when} ({reason}) — run `kew resume` to re-enable dispatch"
