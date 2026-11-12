"""Release committed admission when preparation is interrupted before launch."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import get_ident
from types import TracebackType
from typing import TYPE_CHECKING, Literal, NoReturn

from kew import audit
from kew.cancellation import DeferredSIGINT
from kew.cancellation_journal import StartupState
from kew.claim_retention import claim_recovery_pending
from kew.run_store import _BUSY_BASE_DELAY, _BUSY_MAX_RETRIES

if TYPE_CHECKING:
    from kew.run_store import RunStore

_logger = logging.getLogger(__name__)


def _container_cleanup_unconfirmed(exc: BaseException) -> bool:
    """A container owner attached evidence that its termination is unconfirmed.

    The evidence type lives in ``kew.dispatch_sandbox`` (which imports this
    module), so only the attribute is read here.
    """
    evidence = getattr(exc, "container_launch_failure", None)
    return evidence is not None and not getattr(evidence, "cleanup_confirmed", False)


def release_admission_reservation(store: RunStore | None, run_id: str) -> None:
    """Release only this run's reservation, preserving any replacement owner."""
    if store is None:
        return
    record = store.get(run_id)
    if record is not None:
        store.release_epic_node_item(
            record.item_id or str(record.issue_num), run_id=run_id
        )


def _finalize_interrupted_admission(
    store: RunStore | None,
    run_id: str,
    exc: BaseException,
    *,
    startup_state: StartupState,
) -> None:
    if store is None:
        return
    if claim_recovery_pending(store, run_id) or _container_cleanup_unconfirmed(exc):
        # An unlaunched tracker claim keeps the row pending: its owner records
        # retention and terminalizes the row once recovery evidence exists. A
        # created container whose termination is unconfirmed keeps its row and
        # reservation too; the container owner finalizes once cleanup lands.
        return
    if not store.finalize_cancelled_run(run_id, startup_state=startup_state):
        return
    record = store.get(run_id)
    if record is None:
        return
    launched = startup_state in ("started", "unknown")
    audit.emit(
        store,
        "dispatch_completed",
        actor=audit.Actor("policy", "admission"),
        run_id=run_id,
        issue_num=record.issue_num,
        item_id=record.item_id,
        repo=record.repo,
        payload={
            "error_category": record.error_category,
            "reason": (
                f"{'Execution' if launched else 'Pre-launch admission'} interrupted"
                f" by {type(exc).__name__}: {exc}"
            ),
            "launched": launched,
            "exit_code": record.exit_code,
        },
    )


@dataclass
class PendingAdmission:
    """Own admission until the primitive confirms a process or container start."""

    store: RunStore | None
    run_id: str
    cleanup_owned: bool = False
    startup_started: bool = False
    startup_refused: bool = False
    receipt_persisted: bool = False
    _settled_started: bool = field(default=False, init=False, repr=False)
    _owner_thread: int = field(default_factory=get_ident, init=False, repr=False)
    launch_cleanup: Callable[[], None] | None = field(default=None, repr=False)

    def cleanup_launch(self, exc: BaseException) -> None:
        """Retain container cleanup custody while handles cross caller boundaries."""
        cleanup, self.launch_cleanup = self.launch_cleanup, None
        if cleanup is None:
            return
        try:
            with DeferredSIGINT():
                cleanup()
        except BaseException as cleanup_exc:
            exc.__cause__ = cleanup_exc
            _logger.error("Launch cleanup failed for %s: %s", self.run_id, cleanup_exc)

    def take_ownership(self) -> None:
        """Accept cleanup custody while SIGINT remains deferred."""
        if get_ident() == self._owner_thread:
            self.cleanup_owned = True

    def record_startup(self, *, refused: bool = False, settled: bool = False) -> None:
        """Retain startup evidence independently of receipt persistence."""
        if get_ident() != self._owner_thread:
            return
        self._settled_started |= settled and not refused
        refused = refused and not self._settled_started
        changed = (self.startup_started, self.startup_refused) != (
            self._settled_started,
            refused,
        )
        self.startup_started = self._settled_started
        self.startup_refused = refused
        if changed:
            self.receipt_persisted = False
        self.persist_receipt()

    def persist_receipt(self) -> None:
        """Retry failures without abandoning a process that already started."""
        if self.receipt_persisted or self.store is None:
            return
        try:
            if self.startup_refused:
                self.store.refuse_launch(self.run_id)
            elif self.startup_started:
                self.store.record_launch(self.run_id)
            else:
                return
            self.receipt_persisted = True
        except Exception:
            _logger.exception("Could not persist launch receipt for %s", self.run_id)

    def finalize_interruption(self, exc: BaseException) -> None:
        """Retry injected cancellations; real SIGINT stays deferred through commit."""
        with DeferredSIGINT() as deferred:
            self.persist_receipt()
            failures = 0
            while True:
                try:
                    if not self._retry_finalization(exc, failures):
                        break
                    failures += 1
                except BaseException:
                    # A cancellation raised by the durable path is a failed
                    # attempt like any other, so it is counted. Without the
                    # count the loop never reaches its limit: it retries
                    # without sleeping and the process wedges here with the
                    # run row pending and no terminal record written.
                    deferred.received = True
                    failures += 1
                    if failures >= _BUSY_MAX_RETRIES:
                        break
            # Keep the original exception primary when a second SIGINT arrived.
            if deferred.received:
                if exc.__cause__ is None:
                    exc.__cause__ = KeyboardInterrupt(
                        "Repeated interruption during cleanup"
                    )
                deferred.received = False

    @property
    def startup_state(self) -> StartupState:
        """Prefer settled evidence over ownership of an unsettled start."""
        if self.startup_started:
            return "started"
        if self.startup_refused:
            return "refused"
        return "unknown" if self.cleanup_owned else "pending"

    def _retry_finalization(self, exc: BaseException, failures: int) -> bool:
        """Alternate durable paths, preserving cancellation if both exhaust retries."""
        try:
            _finalize_interrupted_admission(
                self.store, self.run_id, exc, startup_state=self.startup_state
            )
            return False
        except Exception as finalization_error:
            return self._journal_interruption(exc, finalization_error, failures)

    def _journal_interruption(
        self, exc: BaseException, finalization_error: Exception, failures: int
    ) -> bool:
        try:
            assert self.store is not None
            self.store.defer_cancelled_run(
                self.run_id, startup_state=self.startup_state
            )
        except Exception as journal_error:
            if failures < _BUSY_MAX_RETRIES:
                time.sleep(_BUSY_BASE_DELAY * (2**failures))
                return True
            _logger.error(
                "Cancellation recovery exhausted for run %s; reservation run_id=%s;"
                " finalization=%r; journal=%r",
                self.run_id,
                self.run_id,
                finalization_error,
                journal_error,
            )
            exc.__cause__ = journal_error
            return False
        _logger.warning(
            "Cancellation journaled for run %s; reservation run_id=%s; finalization=%r",
            self.run_id,
            self.run_id,
            finalization_error,
        )
        return False

    def propagate_interruption(
        self, exc: BaseException, *, execution: bool
    ) -> NoReturn:
        """Keep SIGINT deferred across resource cleanup and terminal persistence."""
        with DeferredSIGINT():
            try:
                self.cleanup_launch(exc)
            except BaseException as cleanup_exc:
                exc.__cause__ = cleanup_exc
            finally:
                if self.cleanup_owned == execution:
                    self.finalize_interruption(exc)
            raise exc

    def __enter__(self) -> PendingAdmission:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        if exc is None or isinstance(exc, Exception):
            self.persist_receipt()
            return False
        if self.cleanup_owned:
            return False
        self.propagate_interruption(exc, execution=False)


@contextmanager
def execution_cancellation(admission: PendingAdmission | None) -> Iterator[None]:
    """Terminalize execution cancellation after the primitive confirms startup."""
    try:
        yield
    except BaseException as exc:
        if admission is not None and not isinstance(exc, Exception):
            admission.propagate_interruption(exc, execution=True)
        raise
