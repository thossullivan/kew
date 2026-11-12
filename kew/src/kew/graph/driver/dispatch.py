"""Reservation-guarded detached dispatch — the epic driver's dispatch sink.

The driver reserves a DAG node in the run-store BEFORE spawning any dispatch
child — ``RunStore.reserve_epic_node`` is the authoritative in-flight signal
(atomic INSERT, not check-then-insert). The driver's own scheduling
deliberately does NOT call ``try_claim_issue`` (label-based, requires
``ready``, non-atomic): ownership of "who is working this node" lives in the
run-store reservation, not in a GitHub label race.

``DetachedDispatchSink`` hands off to a detached ``kew epic _dispatch-node``
child (its own process group via ``start_new_session=True``) so the child
survives the driver tick that spawned it.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import uuid
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

from kew.continuation_recovery import require_retained_replacement
from kew.credential_environment import child_environment
from kew.graph.driver.credentials import (
    abandon_driver_key,
    driver_key_spawned,
    prepare_driver_key,
)
from kew.ids import InvalidItemIdError, validate_item_id
from kew.publication import (
    capture_detached_publication_token,
    detached_child_env,
)
from kew.run_credentials import ISSUED_KEY_HASH_ENV, launch_environment
from kew.subprocess_utils import TOOL_PATHS
from kew.subprocess_utils import popen as _default_popen

if TYPE_CHECKING:
    from kew.config import DispatchConfig
    from kew.credential_issuer import BoundedRunKey
    from kew.run_store import ContinuationRecord, RunStore

_logger = logging.getLogger(__name__)

DispatchResult = Literal[
    "dispatched",
    "reserved_conflict",
    "fleet_full",
    "branch_exists",
    "continuation_registered",
    "non_int",
    "review_saturated",
]
_ReservationResult = Literal["reserved", "reserved_conflict", "fleet_full"]
ContinuationRegistrationResult = Literal["registered", "not_registered", "lost_claim"]
ContinuationRegistrar = Callable[[str], ContinuationRegistrationResult]
#: An admission gate consulted BEFORE anything is reserved: it returns the
#: refusal to report, or None to let the dispatch proceed. It takes nothing and
#: releases nothing, so a refusal cannot leak a reservation.
AdmissionGuard = Callable[[str], "DispatchResult | None"]
#: Refusal for a node human-review backpressure is holding back (I15). The
#: delivery repository has no review slot for fresh independent work; the node
#: is untouched and re-enters the frontier when pressure drops.
REVIEW_SATURATED: DispatchResult = "review_saturated"


def resolve_kew_console_script() -> str | None:
    """Resolve the ``kew`` console script, preferring the one adjacent to
    ``sys.executable`` (the running interpreter's ``bin/`` dir) over ambient PATH.

    The epic driver spawns detached ``kew epic _dispatch-node`` children. When
    the driver runs from a virtualenv whose ``bin/`` dir is not on PATH — or PATH
    resolves a *different*, stale ``kew`` from another environment — the console
    script sitting next to the running interpreter is the correct target: it
    shares the very interpreter and installed ``kew`` package the driver itself
    is using, so the child cannot die on an import/version mismatch the ambient
    ``kew`` would introduce. Falls back to ``shutil.which`` when no adjacent
    script exists (e.g. a bare system-Python install), and returns ``None`` when
    ``kew`` is nowhere to be found.
    """
    adjacent = Path(sys.executable).parent / "kew"
    if adjacent.is_file() and os.access(adjacent, os.X_OK):
        return str(adjacent)
    return shutil.which("kew")


class DispatchSink(Protocol):
    """Spawns (or simulates spawning) a dispatch child for one work item."""

    def dispatch(self, item_id: str) -> bool:
        """Return True if a child was spawned."""
        ...


class _PopenFn(Protocol):
    """Shape of the filtered launcher this sink needs (cmd + detach flag)."""

    def __call__(
        self,
        cmd: list[str],
        *,
        stdin: int | None = ...,
        stdout: int | None = ...,
        stderr: int | None = ...,
        env: dict[str, str] | None = ...,
        start_new_session: bool = ...,
        credential: BoundedRunKey | None = ...,
        issued_key_handoff: bool = ...,
        retain_credentials: Iterable[str] = ...,
    ) -> subprocess.Popen[str]: ...


class DispatchedOutcome(Protocol):
    """Minimum outcome contract required by continuation finalization."""

    @property
    def exit_code(self) -> int:
        """Return the process-like exit code for the dispatch."""
        ...


class DetachedDispatchSink:
    """Mint in the parent and spawn a detached child without management authority."""

    def __init__(
        self,
        *,
        epic_ref: str,
        config: DispatchConfig,
        store: RunStore,
        run_kind: str = "dispatch",
        popen: _PopenFn = _default_popen,
        publication_token: Callable[
            [], str | None
        ] = capture_detached_publication_token,
    ) -> None:
        self.config = config
        self.store = store
        self.epic_ref = epic_ref
        self.run_kind = run_kind
        self._popen = popen
        self._publication_token = publication_token
        self._cached_token: str | None = None
        self._token_resolved = False
        self._children: list[subprocess.Popen[str]] = []
        self._max_children = 1024

    def reap(self) -> None:
        """Collect exited children without waiting for running dispatchers."""
        running = []
        for child in self._children:
            status = child.poll()
            if status is None:
                running.append(child)
            else:
                _logger.debug(
                    "dispatch child %s exited with status %s", child.pid, status
                )
        self._children = running

    def _resolve_publication_token(self) -> str | None:
        """Resolve the credential once per sink (i.e. once per driver run).

        Unresolved auth falls through to ``gh auth token``, a subprocess with a
        10-second timeout. The credential cannot change mid-run, so resolving it
        per dispatched node would block the tick loop once per node for nothing.
        A failed resolution caches ``None`` and is not retried.
        """
        if not self._token_resolved:
            self._cached_token = self._publication_token()
            self._token_resolved = True
        return self._cached_token

    def _resolve_cli(self, item_id: str) -> None:
        """Pin the console script this driver spawns children through."""
        kew_path = resolve_kew_console_script()
        if kew_path is None:
            raise RuntimeError(
                "kew CLI not found adjacent to the running interpreter or on "
                "PATH — the epic driver spawns detached dispatch children via "
                "`kew epic _dispatch-node`, which requires the `kew` console "
                "script to be installed."
            )
        TOOL_PATHS["kew"] = kew_path
        _logger.info(
            "epic %s: dispatching %s via detached child %s",
            self.epic_ref,
            item_id,
            kew_path,
        )

    def _child_command(
        self,
        item_id: str,
        *,
        reservation_run_id: str | None,
        reservation_generation: str | None,
    ) -> list[str]:
        """The child argv, minus the run id and credential route the caller adds.

        Non-default run kinds (for example "delegation") ride an explicit flag
        so the child buckets spend under the right run_kind. An unreserved
        default caller keeps the historical flag-free command.
        """
        cmd = ["kew", "epic", "_dispatch-node", self.epic_ref, item_id]
        if self.run_kind != "dispatch":
            cmd += ["--run-kind", self.run_kind]
        if reservation_run_id is not None:
            cmd += ["--reservation-run-id", reservation_run_id]
        if reservation_generation is not None:
            cmd += ["--reservation-generation", reservation_generation]
        return cmd

    def dispatch(
        self,
        item_id: str,
        *,
        reservation_run_id: str | None = None,
        reservation_generation: str | None = None,
    ) -> bool:
        cmd = self._child_command(
            item_id,
            reservation_run_id=reservation_run_id,
            reservation_generation=reservation_generation,
        )
        return self._spawn(item_id, cmd, str(uuid.uuid4()))

    def continue_claimed(self, item_id: str, continuation_run_id: str) -> bool:
        """Spawn the detached child that runs one claimed continuation (#2964).

        The driver claimed the continuation, and its worker slot, before this
        call; the child validates the preserved checkout and runs the agent in
        it under ``continuation_run_id``, the claim's own run id.
        """
        cmd = [
            "kew",
            "epic",
            "_continue-node",
            self.epic_ref,
            item_id,
            "--continuation-run-id",
            continuation_run_id,
        ]
        return self._spawn(item_id, cmd, continuation_run_id)

    def _spawn(self, item_id: str, cmd: list[str], run_id: str) -> bool:
        """Launch one detached child for *run_id* with its credential route."""
        if len(self._children) >= self._max_children:
            self.reap()
            if len(self._children) >= self._max_children:
                raise RuntimeError("detached dispatch child registry is full")
        self._resolve_cli(item_id)
        parent_env = detached_child_env(self._resolve_publication_token()) or dict(
            os.environ
        )
        decision, key = prepare_driver_key(self.store, self.config, run_id, item_id)
        # An excluded handoff keeps exactly the resolved route's required
        # credential names (the sidecar's declared upstream credential) through
        # both filters; a bounded handoff keeps none, the issued key replaces
        # the ambient one (governed review round 2, 2026-09-21).
        retained: tuple[str, ...] = ()
        if not decision.bounded_key:
            retained = tuple(sorted(decision.required_credential_names))
        try:
            child_env = launch_environment(
                child_environment(parent_env, retain=retained),
                key,
                excluded=not decision.bounded_key,
                keep=decision.required_credential_names,
            )
            if key is not None:
                child_env[ISSUED_KEY_HASH_ENV] = key.key_hash
            cmd += ["--run-id", run_id, "--credential-route", decision.digest]
            # This CLI invocation exits right after dispatch returns, closing
            # its end of any inherited pipe. popen()'s default PIPE would leave
            # the detached child writing into a pipe with no reader, crashing it
            # with BrokenPipeError on its first progress write. All three
            # standard streams go to DEVNULL: the child's real output goes to
            # the run store's per-run log file, not the console.
            child = self._popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=child_env,
                start_new_session=True,
                credential=key,
                issued_key_handoff=True,
                retain_credentials=retained,
            )
        except BaseException:
            if key is not None:
                abandon_driver_key(self.store, run_id, key)
            raise
        self._children.append(child)
        if key is not None:
            try:
                driver_key_spawned(self.store, run_id, key)
            except Exception as exc:
                raise ChildSpawnedError(run_id) from exc
        return True


class ChildSpawnedError(RuntimeError):
    """The detached child launched, but bookkeeping after its launch failed.

    The child owns its run from here: a caller must not treat this as a spawn
    that never happened and roll back the claim the child is running under.
    """

    def __init__(self, run_id: str) -> None:
        super().__init__(f"child for run {run_id} launched; its bookkeeping failed")
        self.run_id = run_id


def continue_claimed_child(
    sink: DispatchSink, item_id: str, continuation_run_id: str
) -> bool:
    """Hand one claimed continuation to the sink's detached child (#2964)."""
    launch = getattr(sink, "continue_claimed", None)
    if not callable(launch):
        raise TypeError("dispatch sink cannot run continuations")
    return bool(launch(item_id, continuation_run_id))


def can_continue_claimed(sink: DispatchSink) -> bool:
    """Whether *sink* can run a claimed continuation at all."""
    return callable(getattr(sink, "continue_claimed", None))


def reap_dispatch_children(sink: DispatchSink) -> None:
    """Reap real detached children while preserving simulated sink support."""
    if isinstance(sink, DetachedDispatchSink):
        sink.reap()


def _reserve_dispatch_slot(
    store: RunStore,
    *,
    epic_ref: str,
    item_id: str,
    run_id: str,
    fleet_cap: int,
) -> _ReservationResult:
    """Take the shared item reservation, optionally under the fleet cap."""
    if fleet_cap > 0:
        result = store.reserve_epic_node_item_under_cap(
            epic_ref, item_id, run_id, fleet_cap
        )
        outcomes: dict[str, _ReservationResult] = {
            "reserved": "reserved",
            "lost_claim": "reserved_conflict",
            "fleet_full": "fleet_full",
        }
        return outcomes[result]
    return (
        "reserved"
        if store.reserve_epic_node_item(epic_ref, item_id, run_id)
        else "reserved_conflict"
    )


def reserve_and_dispatch(
    node_id: str,
    *,
    epic_ref: str,
    store: RunStore,
    sink: DispatchSink,
    branch_exists: Callable[[str], bool],
    before_dispatch: Callable[[], bool] | None = None,
    register_continuation: ContinuationRegistrar | None = None,
    admit: AdmissionGuard | None = None,
    provider: str = "github",
    fleet_cap: int = 0,
    new_run_id: Callable[[], str] = lambda: str(uuid.uuid4()),
) -> DispatchResult:
    """Reserve a node, guard against an already-started branch, then dispatch.

    Sequence: (0) validate ``node_id`` as a canonical item id for *provider*
    (rejecting — never coercing — a non-canonical id) and consult the optional
    ``admit`` gate, which refuses BEFORE any slot is taken — this is the one
    door every dispatch goes through, so a limiter installed here cannot be
    walked around by a caller that computes its own candidate list; (1)
    atomically reserve
    the node in the run-store keyed on that item id — a losing caller gets
    ``"reserved_conflict"`` and never touches the sink. When ``fleet_cap`` is
    positive, the reservation uses the same atomic cap-and-reservation
    transaction as delegation; (2) run the optional pre-dispatch continuation
    registration while this caller still owns the
    reservation; the callback receives the temporary reservation generation
    and must release it atomically with its registration; (3) run the legacy
    pre-dispatch guard, whose true result releases that reservation and returns
    ``"continuation_registered"`` without touching the sink; (4) if a remote
    branch for this item already exists, release the reservation and return
    ``"branch_exists"`` (another process started it out-of-band); (5) hand off
    to ``sink.dispatch`` — on ANY exception, release the reservation and
    re-raise so the caller sees the real failure.

    Never raises on a node id that isn't canonical for *provider* (e.g. a
    Linear id under a GitHub epic) — returns ``"non_int"`` instead.
    """
    try:
        item_id = validate_item_id(node_id, provider)
    except InvalidItemIdError:
        return "non_int"

    if admit is not None:
        refusal = admit(item_id)
        if refusal is not None:
            return refusal

    require_retained_replacement(store, epic_ref=epic_ref, item_id=item_id)
    run_id = new_run_id()
    reservation = _reserve_dispatch_slot(
        store,
        epic_ref=epic_ref,
        item_id=item_id,
        run_id=run_id,
        fleet_cap=fleet_cap,
    )
    if reservation != "reserved":
        return reservation

    try:
        return _dispatch_reserved(
            item_id,
            run_id,
            store=store,
            sink=sink,
            branch_exists=branch_exists,
            before_dispatch=before_dispatch,
            register_continuation=register_continuation,
        )
    except ChildSpawnedError as exc:
        # The child launched and may already have bound this reservation;
        # releasing it would let a later tick start a second writer for the
        # same node and checkout. Reconciliation settles the child's run.
        _logger.warning(
            "epic %s: %s launched with failed bookkeeping: %s",
            epic_ref,
            item_id,
            exc.__cause__,
        )
        return "dispatched"
    except Exception:
        store.release_epic_node_item(item_id, run_id=run_id)
        raise


def _dispatch_reserved(
    item_id: str,
    run_id: str,
    *,
    store: RunStore,
    sink: DispatchSink,
    branch_exists: Callable[[str], bool],
    before_dispatch: Callable[[], bool] | None,
    register_continuation: ContinuationRegistrar | None,
) -> DispatchResult:
    """Run the pre-dispatch guards for an ALREADY-reserved item, then hand off.

    Every refusal here releases the reservation the caller just took, except
    the ``"registered"`` continuation path, which released it atomically with
    its own registration. Raising is the caller's to clean up.
    """
    if register_continuation is not None:
        registration = register_continuation(run_id)
        if registration == "registered":
            return "continuation_registered"
        if registration == "lost_claim":
            store.release_epic_node_item(item_id, run_id=run_id)
            return "reserved_conflict"
    if before_dispatch is not None and before_dispatch():
        store.release_epic_node_item(item_id, run_id=run_id)
        return "continuation_registered"
    if branch_exists(item_id):
        store.release_epic_node_item(item_id, run_id=run_id)
        return "branch_exists"
    dispatch_reserved_child(sink, item_id, run_id)
    return "dispatched"


def dispatch_reserved_child(sink: DispatchSink, item_id: str, run_id: str) -> bool:
    """Pass the acquired generation to a detached child without rereading storage."""
    if isinstance(sink, DetachedDispatchSink):
        return sink.dispatch(
            item_id, reservation_run_id=run_id, reservation_generation=run_id
        )
    return sink.dispatch(item_id)


def resume_continuation(
    record: ContinuationRecord,
    *,
    store: RunStore,
    dispatch: Callable[[str, Path, str, str], DispatchedOutcome],
) -> DispatchedOutcome:
    """Run one already-claimed continuation and close its generation safely.

    The caller must claim the record through the run-store before invoking this
    helper. The callback receives the durable item id, exact recorded worktree,
    exact recorded branch, and continuation run id, so it cannot silently fall
    back to a fresh checkout. A successful dispatch finalizes the same
    generation; every failed or exceptional dispatch returns it to
    awaiting_continuation. Generation-conditional store operations make a
    stale callback unable to release a later owner's reservation.
    """
    continuation_run_id = record.continuation_run_id
    if not continuation_run_id:
        raise ValueError("continuation has no durable owner run id")

    def _return_to_awaiting(reason: str) -> None:
        changed = store.mark_continuation_awaiting(
            record.epic_ref,
            record.item_id,
            record.original_run_id,
            record.branch,
            record.worktree_identity,
            record.generation,
            continuation_run_id,
            reason=reason,
            cancel_unlaunched=True,
        )
        if not changed:
            _logger.warning(
                "continuation %s/%s lost generation ownership while unwinding",
                record.epic_ref,
                record.item_id,
            )

    try:
        outcome = dispatch(
            record.item_id,
            Path(record.worktree_identity),
            record.branch,
            continuation_run_id,
        )
    except BaseException as exc:
        _logger.warning(
            "continuation claim %s dispatch failed: %s",
            continuation_run_id,
            exc,
        )
        _return_to_awaiting("dispatch_exception")
        raise

    if outcome.exit_code == 0:
        finalized = store.finalize_continuation(
            record.epic_ref,
            record.item_id,
            record.original_run_id,
            record.branch,
            record.worktree_identity,
            record.generation,
            continuation_run_id,
        )
        if not finalized:
            raise RuntimeError(
                "continuation completed but its ownership generation was lost"
            )
    else:
        _return_to_awaiting(f"dispatch_exit_{outcome.exit_code}")
    return outcome
