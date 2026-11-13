"""Authenticated process termination shared by fleet and admission recovery."""

from __future__ import annotations

import contextlib
import logging
import os
import signal
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol

from kew import process_groups
from kew.process_groups import (
    ProcessIdentity,
    attempt_groups,
    attempt_identities_from_events,
    valid_pid,
)
from kew.progress import _warn

if TYPE_CHECKING:
    from kew.run_store import RunRecord, RunStore


class SignalProbes(Protocol):
    """Read-only identity probes shared with fleet instrumentation."""

    def start_token_matches(self, pid: int, token: str | None) -> bool: ...

    def process_identity(self, pid: int) -> ProcessIdentity | None: ...

    def identity_matches(
        self,
        pid: int,
        birth: str | None,
        parent: int | None,
        command: str | None,
        start_token: str | None = None,
    ) -> bool: ...

    def retained_identity_matches(self, pid: int, token: str) -> bool: ...

    def group_member_identities(self, pgid: int) -> dict[int, str] | None: ...

    def authenticated_group_members(
        self, pgid: int, retained: dict[int, str]
    ) -> tuple[dict[int, str], list[int]] | None: ...

    def group_alive(self, pgid: int) -> bool | None: ...

    def owner_signallable(
        self,
        pgid: int,
        owner_pid: int | None,
        birth: str | None = None,
        parent: int | None = None,
        command: str | None = None,
        start_token: str | None = None,
    ) -> bool: ...

    def recovered_attempt_signallable(
        self,
        pgid: int,
        owner_pid: int | None,
        birth: str | None = None,
        parent: int | None = None,
        command: str | None = None,
        start_token: str | None = None,
    ) -> bool: ...

    def process_exited(self, pid: int) -> bool: ...


_GRACE_SECONDS = 10.0
_ESCALATION_ROUNDS = 3
_ESCALATION_PAUSE_SECONDS = 0.02


def _group_alive(pgid: int) -> bool:
    """Keep waiting until process group death is positively confirmed."""
    return process_groups.group_alive(pgid) is not False


def _signal_group(pgid: int, sig: int) -> None:
    """Send a signal to a process group, ignoring already-dead groups."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, sig)


def _attempt_groups(record: RunRecord) -> list[int]:
    """Use the retained database group, or every unregistered spawn claim."""
    return attempt_groups(record.attempt_pgid, record.log_file)


def _validated_run_attempt(
    pgid: int, record: RunRecord, *, probes: SignalProbes = process_groups
) -> tuple[int | None, str]:
    """Require a registered kernel token and verified owner ancestry."""
    if not valid_pid(pgid) or pgid == os.getpgrp():
        return None, "unverified attempt: invalid group or caller's group"
    if probes.group_alive(pgid) is False:
        return None, "stale: process group already dead"
    token, owner = _attempt_identity(pgid, record)
    if owner != record.owner_pid or not probes.start_token_matches(pgid, token):
        return None, _attempt_unverifiable(pgid)
    note = _attempt_owner_note(pgid, record, probes=probes)
    return (None, note) if note else (pgid, "")


def _attempt_unverifiable(pgid: int) -> str:
    note = "attempt_identity_unverifiable: missing or mismatched birth token"
    logging.getLogger(__name__).warning("%s for group %s", note, pgid)
    return note


def _attempt_owner_note(
    pgid: int, record: RunRecord, *, probes: SignalProbes = process_groups
) -> str:
    """Authenticate retained owner identity or legacy command and ancestry."""
    token, owner = _attempt_identity(pgid, record)
    if owner != record.owner_pid or not probes.start_token_matches(pgid, token):
        return _attempt_unverifiable(pgid)
    if _registered_attempt(pgid, record):
        # Registration retains ancestry after owner exit. Live owners must still match.
        if probes.process_exited(record.owner_pid or 0):
            return ""
    elif not probes.recovered_attempt_signallable(
        pgid,
        record.owner_pid,
        record.owner_start_time,
        record.owner_ppid,
        record.owner_command,
        record.owner_start_token,
    ):
        return "unverified attempt: no verified kew owner ancestry"
    if not probes.identity_matches(
        record.owner_pid or 0,
        record.owner_start_time,
        record.owner_ppid,
        record.owner_command,
        record.owner_start_token,
    ):
        return "unverified attempt: missing or mismatched owner birth identity"
    return ""


def _attempt_identity(pgid: int, record: RunRecord) -> tuple[str | None, int | None]:
    """Prefer registration evidence over untrusted event claims."""
    if record.attempt_pgid is not None:
        return record.attempt_start_token, record.owner_pid
    return (attempt_identities_from_events(record.log_file) or {}).get(
        pgid, (None, None)
    )


def _registered_attempt(pgid: int | None, record: RunRecord) -> bool:
    """Registration retains ancestry independently of the owner's command line."""
    return (
        valid_pid(pgid)
        and valid_pid(record.owner_pid)
        and record.attempt_owner_verified
        and record.attempt_pgid == pgid
        and record.attempt_start_token is not None
        and record.owner_start_token is not None
        and record.owner_pid == record.pgid
    )


def _validated_owner(
    record: RunRecord, *, probes: SignalProbes = process_groups
) -> tuple[int | None, str]:
    """Require owner birth and registration evidence, or a legacy kew entrypoint."""
    pgid = record.pgid
    if not valid_pid(pgid) or pgid == os.getpgrp():
        return None, "unverified owner: invalid group or caller's group"
    assert pgid is not None
    if probes.identity_matches(
        pgid,
        record.owner_start_time,
        record.owner_ppid,
        record.owner_command,
        record.owner_start_token,
    ) and (
        _registered_attempt(record.attempt_pgid, record)
        or probes.owner_signallable(
            pgid,
            record.owner_pid,
            record.owner_start_time,
            record.owner_ppid,
            record.owner_command,
            record.owner_start_token,
        )
    ):
        return pgid, ""
    return None, "unverified owner: birth or executed kew identity unmatched"


def attempt_is_dead(
    record: RunRecord, *, probes: SignalProbes = process_groups
) -> bool:
    """Prove the recorded attempt empty before handing off its checkout."""
    groups = _attempt_groups(record)
    return bool(groups) and all(probes.group_alive(pgid) is False for pgid in groups)


def _contained_client_dead_or_legacy(record: RunRecord) -> bool:
    """A caller-proven absent container still requires registered client death."""
    return attempt_is_dead(record) or (
        record.attempt_pgid is None
        and attempt_identities_from_events(record.log_file) == {}
    )


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
    if process_groups.group_alive(attempt) is False:
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


def _retain_members(
    pgid: int, identities: dict[int, str], record: RunRecord, store: RunStore | None
) -> None:
    """Save authority before signalling; stale worker saves must not erase it."""
    record.termination_members.setdefault(pgid, {}).update(identities)
    if store is not None:
        store.retain_termination_members(record.run_id, pgid, identities)


def _kill_retained_group(
    pgid: int,
    identities: dict[int, str],
    record: RunRecord | None,
    store: RunStore | None,
    *,
    wait_for_groups: Callable[[list[int], float], list[int]] | None = None,
    probes: SignalProbes = process_groups,
) -> None:
    """Bound fresh ancestry scans, retaining authenticated descendants each round."""
    wait = wait_for_groups or _wait_for_groups
    for _ in range(_ESCALATION_ROUNDS):
        if record is not None and not _retained_owner_matches(record, probes=probes):
            return
        observed = probes.authenticated_group_members(pgid, identities)
        if observed is None:
            _warn(f"Cannot escalate group {pgid}: process snapshot unavailable")
            return
        authenticated, unknown = observed
        if unknown:
            _warn(
                f"attempt_identity_unverifiable: group {pgid}: "
                f"unauthenticated survivors {unknown}"
            )
        if not authenticated:
            return
        identities.update(authenticated)
        if record is not None:
            _retain_members(pgid, authenticated, record, store)
        _kill_retained_members(authenticated, record, probes=probes)
        if not wait([pgid], _ESCALATION_PAUSE_SECONDS):
            return
    _warn(f"Group {pgid}: escalation round limit reached; retaining ownership")


def _retained_owner_matches(
    record: RunRecord, *, probes: SignalProbes = process_groups
) -> bool:
    """A live owner must still match all admission fields during escalation."""
    if probes.process_exited(record.owner_pid or 0):
        return True
    return probes.identity_matches(
        record.owner_pid or 0,
        record.owner_start_time,
        record.owner_ppid,
        record.owner_command,
        record.owner_start_token,
    )


def _kill_retained_members(
    identities: dict[int, str],
    record: RunRecord | None = None,
    *,
    probes: SignalProbes = process_groups,
) -> None:
    """Retained births authorize only individual survivors, never reused groups."""
    for pid, token in identities.items():
        if record is not None and not _retained_owner_matches(record, probes=probes):
            return
        if not probes.retained_identity_matches(pid, token):
            _warn(f"attempt_identity_unverifiable: departed or reused member {pid}")
            continue
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError) as exc:
            _warn(f"Cannot kill retained member {pid}: {exc}")


def _terminate_group(
    pgid: int,
    record: RunRecord,
    retained: dict[int, dict[int, str]],
    store: RunStore | None = None,
    *,
    signal_group: Callable[[int, int], None] | None = None,
    probes: SignalProbes = process_groups,
) -> bool:
    """Capture member births, authenticate the group again, then send SIGTERM."""
    identities = probes.group_member_identities(pgid)
    if not identities:
        _warn(f"Cannot retain members for group {pgid}: process scan unavailable")
        return False
    target, note = (
        _validated_owner(record, probes=probes)
        if pgid == record.pgid
        else _validated_run_attempt(pgid, record, probes=probes)
    )
    if target is None:
        _warn(f"Cannot terminate group {pgid}: {note}")
        return False
    observed = probes.process_identity(pgid)
    if (
        observed is None
        or identities.get(pgid) != observed.token()
        or not probes.retained_identity_matches(pgid, observed.token())
    ):
        _warn(f"Cannot terminate group {pgid}: snapshot leader identity unmatched")
        return False
    _retain_members(pgid, identities, record, store)
    retained[pgid] = identities
    (signal_group or _signal_group)(pgid, signal.SIGTERM)
    return True


def _wait_for_groups(groups: list[int], grace_seconds: float) -> list[int]:
    """Wait within one deadline and return groups whose death remains unproven."""
    deadline = time.monotonic() + grace_seconds
    survivors = [pgid for pgid in groups if _group_alive(pgid)]
    while survivors and time.monotonic() < deadline:
        time.sleep(min(0.02, max(0, deadline - time.monotonic())))
        survivors = [pgid for pgid in survivors if _group_alive(pgid)]
    return survivors
