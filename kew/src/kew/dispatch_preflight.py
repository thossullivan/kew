"""Budget/breaker preflight-refusal helpers, extracted from ``dispatch_core``
(Task 5).

This module owns the budget preflight-refusal sub-surface: the epic-hold reason
prefix (``_BUDGET_HOLD_PREFIX``), the operator-facing refusal message builders,
the terminal refusal-row writer (``_record_budget_refusal_row`` — #891), and the
shared refusal handler (``_refuse_budget``) used by both the cheap preflight and
the atomic admit-time gate (#661/H2).

It is a pure seam extraction. ``mark_failed`` is a ``mock.patch`` target at
``kew.dispatch_core.mark_failed`` (the budget-refusal tests patch it and drive
``execute_dispatch`` through here), so it is reached as an attribute of the
``kew.dispatch_core`` module at call time; ``_emit_early_exit`` and the
``_REFUSAL_STATUS`` / ``_BUDGET_REFUSAL_CATEGORY`` constants stay in
``dispatch_core`` (the latter is imported from there by the delegate claim/run
paths) and are likewise reached through the module. The non-patched helpers
(``item_ref``, ``branch_slug``, ``_warn``) are imported directly. This mirrors
the Wave 1 precedent (``run_store_reservations``) and breaks the import cycle.
"""

from __future__ import annotations

import contextlib
import time
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import kew.dispatch_core
from kew.ids import branch_slug, item_ref
from kew.overlap import DispatchOutcome
from kew.progress import _warn
from kew.run_store_work import (
    DispatchReservationReplaced,
    capture_dispatch_reservation,
)
from kew.runner_fields import exception_text

if TYPE_CHECKING:
    from kew.budget import BudgetExceeded
    from kew.config import DispatchConfig
    from kew.run_store import RunRecord, RunStore

#: Epic-hold reason prefix the driver's overlay routes to HELD (#661). Kept
#: in sync with ``kew.epic_holds.BUDGET_HOLD_PREFIX`` — the vocabulary's owner,
#: which the overlay reads through ``holds_node_out_of_frontier``.
_BUDGET_HOLD_PREFIX = "budget_preflight"

#: The same, for a containment refusal (#1144). Kept in sync with
#: ``kew.epic_holds.CONTAINMENT_HOLD_PREFIX``; the sync is enforced
#: by ``test_containment_hold_prefix_matches_the_overlay``.
_CONTAINMENT_HOLD_PREFIX = "sandbox_containment"


def _budget_refusal_detail(exc: BudgetExceeded) -> str:
    """The numbers behind a preflight refusal: ``spent $X > <limit> cap $Y``."""
    return f"spent ${exc.spent_usd:.2f} > {exc.which_limit} cap ${exc.limit_usd:.2f}"


def _budget_hold_reason(exc: BudgetExceeded) -> str:
    """Epic-hold reason string: the ``budget_preflight`` prefix the overlay
    matches on, plus observed/cap numbers and the real config knob (#661).
    """
    return (
        f"{_BUDGET_HOLD_PREFIX}: {_budget_refusal_detail(exc)} — raise "
        f"budget.{exc.which_limit}_usd in kew.toml"
    )


def _budget_refusal_message(item_id: str, exc: BudgetExceeded) -> str:
    """Classic-queue (non-epic) preflight refusal: name the numbers + the knob
    to raise so the operator is never left guessing (#661). Keyed on the durable
    ``item_id`` so the refusal reference points at the real item."""
    return (
        f"Budget pre-check refused {item_ref(item_id)}: "
        f"{_budget_refusal_detail(exc)} — "
        f"raise budget.{exc.which_limit}_usd in kew.toml"
    )


def record_owned_refusal(
    store: RunStore,
    record: RunRecord,
    *,
    epic_ref: str,
    expected_reservation_run_id: str | None,
    expected_reservation_generation: str | None = None,
    raise_on_replaced: bool = False,
) -> RunRecord:
    """Save a refusal against only the supplied owner, checked in the transaction."""
    record = replace(record, pre_admission_refusal=True)
    if expected_reservation_run_id is None:
        store.save(record)
        return record
    owned = replace(record, run_id=expected_reservation_run_id)
    # The same identity dispatch admission uses: the tracker item id, or the
    # issue number for trackerless rows.
    item_id = record.item_id or str(record.issue_num)

    def _require_owner() -> None:
        capture_dispatch_reservation(
            store,
            epic_ref,
            item_id,
            expected_reservation_run_id=expected_reservation_run_id,
            expected_reservation_generation=expected_reservation_generation,
        )

    try:
        store.try_admit_run(owned, cap=0, on_admit=_require_owner)
        return owned
    except DispatchReservationReplaced:
        if raise_on_replaced:
            raise
        # The transaction rolled back the attributed row. Retain only fresh,
        # unbound evidence, which cannot terminalize another reservation.
        refused = replace(
            record, status="refused", error_category="reservation_replaced", exit_code=2
        )
        store.save(refused)
        return refused


def record_replaced_reservation_refusal(
    store: RunStore, config: DispatchConfig, item_id: str, *, run_kind: str = "dispatch"
) -> None:
    """Record a stale child's refusal without adopting or releasing any reservation."""
    from kew.run_store import RunRecord, _epic_issue_num

    now = datetime.now(UTC)
    record_owned_refusal(
        store,
        RunRecord(
            run_id=str(uuid.uuid4()),
            issue_num=_epic_issue_num(item_id),
            item_id=item_id,
            branch=f"{config.branches.prefix}{branch_slug(item_id)}",
            run_kind=run_kind,
            status="refused",
            exit_code=2,
            started_at=now,
            finished_at=now,
            error_category="reservation_replaced",
            repo=config.project.repo or None,
        ),
        epic_ref="",
        expected_reservation_run_id=None,
    )


def _record_refusal_row(
    store: RunStore,
    config: DispatchConfig,
    epic_ref: str,
    issue_num: int,
    display_id: str,
    *,
    error_category: str,
    run_id: str | None = None,
    expected_reservation_run_id: str | None = None,
    expected_reservation_generation: str | None = None,
    raise_on_replaced: bool = False,
    exit_code: int = 1,
) -> RunRecord:
    """Persist a terminal *refusal* RunRecord for an epic node a preflight
    refused, keyed to the driver's reservation ``run_id`` (#891 / O2).

    Without this the child holds the node and exits leaving NO run row, so the
    driver's dead-child reconciler (``dispatch_child_died``, #835) mistakes a
    clean refusal for a silent corpse once the startup grace expires — and the
    reservation parks a fleet worker slot for the whole reconcile grace at $0.
    Writing a terminal row keyed to the reservation short-circuits that path:
    the tick reconciles the reservation directly from the row, and the row's
    distinct status/category keeps the node re-admittable (never latched
    FAILED) once the hold clears. Mirrors ``cli._record_dispatch_child_refusal``
    for a child that never reached ``execute_dispatch``.

    ``error_category`` names WHICH refusal wrote the row (budget, containment),
    so the delegate paths that key on the budget category are not fooled by a
    different one and the operator can tell them apart.

    Missing identity produces an unbound row. Never adopt the current owner.
    Pipeline callers propagate replacement to their single exit-2 refusal handler.
    ``run_id`` only names the unbound row the caller already owns (the refused
    child's own id); it is never read back off a reservation.
    """
    return record_owned_refusal(
        store,
        refusal_record(
            config,
            issue_num,
            display_id,
            error_category=error_category,
            run_id=run_id,
            exit_code=exit_code,
        ),
        epic_ref=epic_ref,
        expected_reservation_run_id=expected_reservation_run_id,
        expected_reservation_generation=expected_reservation_generation,
        raise_on_replaced=raise_on_replaced,
    )


def refusal_record(
    config: DispatchConfig,
    issue_num: int,
    display_id: str,
    *,
    error_category: str,
    run_id: str | None = None,
    exit_code: int = 1,
) -> RunRecord:
    """The terminal pre-admission refusal row ``_record_refusal_row`` writes."""
    from kew.run_store import RunRecord

    now = datetime.now(UTC)
    return RunRecord(
        run_id=run_id or str(uuid.uuid4()),
        issue_num=issue_num,
        item_id=display_id,
        branch=f"{config.branches.prefix}{branch_slug(display_id)}",
        status=kew.dispatch_core._REFUSAL_STATUS,
        exit_code=exit_code,
        started_at=now,
        finished_at=now,
        error_category=error_category,
        repo=config.project.repo or None,
        pre_admission_refusal=True,
    )


def _record_budget_refusal_row(
    store: RunStore,
    config: DispatchConfig,
    epic_ref: str,
    issue_num: int,
    display_id: str,
    *,
    run_id: str | None = None,
    expected_reservation_run_id: str | None = None,
    expected_reservation_generation: str | None = None,
    raise_on_replaced: bool = False,
) -> RunRecord:
    """:func:`_record_refusal_row` with the budget category — the name the
    #891 tests and the delegate claim/run paths know."""
    return _record_refusal_row(
        store,
        config,
        epic_ref,
        issue_num,
        display_id,
        error_category=kew.dispatch_core._BUDGET_REFUSAL_CATEGORY,
        run_id=run_id,
        expected_reservation_run_id=expected_reservation_run_id,
        expected_reservation_generation=expected_reservation_generation,
        raise_on_replaced=raise_on_replaced,
    )


def _refuse_budget(
    store: RunStore,
    config: DispatchConfig,
    issue_num: int,
    epic_ref: str | None,
    complexity: str,
    exc: BudgetExceeded,
    start_monotonic: float,
    item_id: str | None = None,
    run_id: str | None = None,
    *,
    expected_reservation_run_id: str | None = None,
    expected_reservation_generation: str | None = None,
) -> DispatchOutcome:
    """Handle a budget refusal identically for the cheap preflight and the
    atomic admit-time gate (H2).

    Epic nodes are HELD (non-destructive, un-held on the next driver tick once
    the cap is raised) rather than relabelled ``agent-failed`` (#661); the
    classic queue keeps ``mark_failed`` with the numbers-and-knob message. An
    epic node also writes a terminal refusal RunRecord against the driver's
    reservation (#891) so the refusal is a visible record, never a dead corpse
    the dead-child reconciler misdiagnoses.
    """
    display_id = item_id or str(issue_num)
    if epic_ref is not None:
        hold_reason = _budget_hold_reason(exc)
        # Hold on the durable item id (a Linear id survives; a GitHub number
        # rides as its str) so the driver-tick clear keys on the same currency.
        # The caller's own run id names the unbound row, so a replacement
        # run's refusal stays attributable to it (continuation recovery).
        _record_budget_refusal_row(
            store,
            config,
            epic_ref,
            issue_num,
            display_id,
            run_id=run_id,
            expected_reservation_run_id=expected_reservation_run_id,
            expected_reservation_generation=expected_reservation_generation,
            raise_on_replaced=True,
        )
        store.hold_epic_node(epic_ref, display_id, hold_reason)
        _warn(f"Held {item_ref(display_id)}: {hold_reason}")
    else:
        _warn(_budget_refusal_message(display_id, exc))
        kew.dispatch_core.mark_failed(  # type: ignore[attr-defined]
            issue_num,
            config,
            f"budget pre-check failed: {_budget_refusal_detail(exc)}",
            extra_labels=[config.labels.budget_exceeded],
        )
    kew.dispatch_core._emit_early_exit(
        store,
        issue_num=issue_num,
        item_id=item_id,
        complexity=complexity,
        runner="",
        model="",
        exit_code=1,
        duration_seconds=time.monotonic() - start_monotonic,
        error_category="budget_exceeded",
        repo=config.project.repo or None,
        run_id=run_id,
        epic_ref=epic_ref,
    )
    return DispatchOutcome(exit_code=1, changed_files=[], launched=False)


def _containment_hold_reason(item_id: str, exc: Exception) -> str:
    """Epic-hold reason string for a containment refusal: the
    ``sandbox_containment`` prefix the overlay matches on, plus the resolver's
    own message (which already names the runner, the missing mapping, and the
    exact knob) and the un-hold command (#1144).

    The resolver's ledger-stable cause token (``SandboxRuntimeUnavailable.reason``)
    leads the message in brackets, ``sandbox_containment: [credential_missing]
    Sandbox: ...``: the operator hint is keyed on it (``kew.waits``), because
    the prose is free to be reworded and several causes share words (#2682).
    """
    cause = getattr(exc, "reason", None)
    token = f"[{cause}] " if isinstance(cause, str) and cause else ""
    return (
        f"{_CONTAINMENT_HOLD_PREFIX}: {token}"
        f"{exception_text(exc)} — un-hold with `kew epic unhold {item_id}`"
    )


def _hold_for_containment(
    store: RunStore,
    config: DispatchConfig,
    epic_ref: str,
    issue_num: int,
    display_id: str,
    exc: Exception,
    *,
    run_id: str | None = None,
    expected_reservation_run_id: str | None = None,
    expected_reservation_generation: str | None = None,
) -> None:
    """Hold an epic node whose runner could not be contained, and reconcile its
    reservation from a terminal refusal row (#1144).

    Pinned callers must prove ownership before changing a hold or rolling back
    a tracker claim. Ownership failures propagate to the replacement handler.
    Unpinned callers retain best-effort recording with a warning.
    """
    hold_reason = _containment_hold_reason(display_id, exc)
    try:
        _record_refusal_row(
            store,
            config,
            epic_ref,
            issue_num,
            display_id,
            error_category=kew.dispatch_core._CONTAINMENT_REFUSAL_CATEGORY,
            run_id=run_id,
            expected_reservation_run_id=expected_reservation_run_id,
            expected_reservation_generation=expected_reservation_generation,
            raise_on_replaced=True,
        )
    except DispatchReservationReplaced:
        raise
    except Exception as row_exc:
        if expected_reservation_run_id is not None:
            raise
        _warn(
            f"Failed to record the containment refusal for {item_ref(display_id)}: "
            f"{row_exc} — the driver's reservation will grace-time out instead"
        )
    try:
        store.hold_epic_node(epic_ref, display_id, hold_reason)
    except Exception as hold_exc:
        _warn(f"Failed to hold {item_ref(display_id)} on containment: {hold_exc}")
    _warn(f"Held {item_ref(display_id)}: {hold_reason}")


def _refuse_containment(
    store: RunStore | None,
    config: DispatchConfig,
    issue_num: int,
    epic_ref: str | None,
    complexity: str,
    runner: str,
    exc: Exception,
    start_monotonic: float,
    item_id: str | None = None,
    run_id: str | None = None,
    *,
    expected_reservation_run_id: str | None = None,
    expected_reservation_generation: str | None = None,
) -> DispatchOutcome:
    """Handle a fail-closed containment refusal without stranding the node.

    A containment refusal (:class:`~kew.sandbox.SandboxRuntimeUnavailable` and
    its subclasses) reaches dispatch AFTER the driver reserved a fleet worker
    slot for the node, so letting it fall through to the generic mid-dispatch
    exception handler parks that slot for the driver's full reconcile grace at
    $0 — with a config-level cause, that is every affected node, every tick
    (#1144 review). Handle it like the budget refusal it structurally is:

    - Epic node: HOLD it (non-destructive; the reason names the exact knob) and
      write a terminal refusal RunRecord keyed to the reservation, so the tick
      reconciles the slot immediately instead of grace-timing it out — and the
      node does NOT spin, re-admitted and re-refused on every subsequent tick.
      The hold is operator-cleared (``kew epic unhold``): unlike a budget cap,
      kew cannot observe that a `[sandbox] images` entry now exists for a
      runner it never got to resolve.
    - Classic queue: ``mark_failed`` with the resolver's message, exactly as
      any other pre-spawn refusal.
    """
    display_id = item_id or str(issue_num)
    if store is not None and epic_ref is not None:
        _hold_for_containment(
            store,
            config,
            epic_ref,
            issue_num,
            display_id,
            exc,
            run_id=run_id,
            expected_reservation_run_id=expected_reservation_run_id,
            expected_reservation_generation=expected_reservation_generation,
        )
    else:
        _warn(f"Containment refused {item_ref(display_id)}: {exception_text(exc)}")
        # Best-effort, like every other refusal's relabel: a `gh` failure must
        # not mask the refusal or skip the caller's claim rollback.
        with contextlib.suppress(Exception):
            kew.dispatch_core.mark_failed(  # type: ignore[attr-defined]
                issue_num, config, f"containment refused: {exception_text(exc)}"
            )
    kew.dispatch_core._emit_early_exit(
        store,
        issue_num=issue_num,
        item_id=item_id,
        complexity=complexity,
        runner=runner,
        model="",
        exit_code=1,
        duration_seconds=time.monotonic() - start_monotonic,
        error_category=kew.dispatch_core._CONTAINMENT_REFUSAL_CATEGORY,
        repo=config.project.repo or None,
        run_id=run_id,
        epic_ref=epic_ref,
    )
    return DispatchOutcome(exit_code=1, changed_files=[], launched=False)
