"""Serial merge queue: the combined tree must be green before a merge (#2148).

The approval-gated merge pass (:mod:`kew.graph.driver.merge`) proves a PR is
approved, reviewed READY and green **on its own head**. It cannot prove the PR is
green against the base it is about to land on, and two siblings each green alone
can be red together. Both confirmed instances came from one epic: #2072 and #2075
reached READY carrying silent ``except`` handlers a repo-wide policy gate catches
(#2140), and PR #2096 added a required API field that a sibling's fixtures —
merged first — did not carry. The operator caught both by hand, with a script
that merged main into the READY head in a scratch worktree, ran the full gates on
that combined tree, and merged one PR at a time.

This module is that script, as a driver step an operator opts into per epic
(``[epic] merge_queue = true``). For every candidate the merge pass would merge:

1. one verification AND the merge it releases at a time, repo-wide, under
   ``merge-queue.lock`` — the lock is held until the merge has landed, so the
   next verification reads a base that contains it;
2. the base branch is fetched from the REAL upstream (never a possibly-stale
   local ``main``: the sibling merged one verification ago exists only there),
   its OID is recorded, and it is merged into the head in a scratch clone;
3. the PR's diff against that base is intersected with ``[epic]
   merge_gate_paths``: a PR that edits what the gates ARE is HELD before any
   command runs (#2184), until a human reads that diff and ``kew epic unhold``s
   it — which leaves the receipt this pass reads, because a hold re-derived from
   an unchanged diff is otherwise one no release can clear;
4. ``commands.verify_loop`` — required, never inferred from ``commands.ci`` —
   runs in that combined tree, CONTAINED (#2184);
5. green → the merge proceeds, still under the queue lock, but only while the
   head still equals the head that was verified (the caller re-reads it inside
   the lease, `merge._verified_merge`) and the base still equals the OID the
   tree was built on;
6. red → the base binding is re-checked FIRST (a tree that went red against a
   base something outside kew has since moved proves nothing about the PR), and
   only then is the failure recorded as an evidenced NEEDS_CHANGES review round
   naming the failing gate, which the review stage remediates next tick.

No path here can produce a silent green. An unconfigured command, a refused
checkout, a base merge git could not run, a timeout, a refused containment
posture, a base that moved, and a busy lock each withhold the merge and say why;
only an exit-0 verify command releases it.

TRUST BOUNDARY: the verify command is OPERATOR-CONFIG-ONLY — never issue- or
agent-sourced, exactly like ``commands.verify_loop`` for the loop gate. What it
EXECUTES, though, is agent-owned: every gate worth running runs files from the
tree under test (a gate script, a build backend, a ``conftest.py``), so a host
child would hand a PR the driver's environment and its merge credentials. The
command therefore runs under the same containment-first admission the loop gate
resolves (:func:`kew.loops.verification.resolve_command_verification_admission`):
the default posture is :data:`CONTAINED_VERIFICATION_POSTURE` — the approved
image, no network, no host environment — and the host is reached only when the
operator's own ``[sandbox] dangerously_skip_containment`` produces that fallback
posture. A refused admission holds the candidate; it never runs anyway. Gates
that CANNOT run contained (a docker build, anything needing the network) are
named in ``[epic] merge_queue_uncontained_gates``, recorded as not proven on
every attestation, and left to CI, which the merge pass gates on separately.
``[epic] merge_queue`` also requires ``[review] adversarial`` (a config-load
error otherwise): the review stage is the only thing that remediates the round a
red combined tree records.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import logging
import os
import re
import shlex
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import IO, Any, Literal, Protocol

from kew.audit import Actor, emit
from kew.config import DispatchConfig
from kew.epic_holds import (
    MERGE_GATE_INTEGRITY_PART,
    MERGE_GATE_PARTS_SEP,
    MERGE_GATE_TOUCHED_HOLD_PREFIX,
    MERGE_GATE_UNCHECKED_PART,
    holds_node_out_of_frontier,
    merge_gate_ack_key,
    merge_gate_hold_reason,
    merge_gate_parts,
    merge_gate_reviewed_head,
)
from kew.graph.driver.base_integration import MAX_NAMED_PATHS
from kew.ids import GITHUB_ID_RE, provider_from_config
from kew.loops.verification import (
    CommandCondition,
    ConditionResult,
    HostVerificationAdmission,
    resolve_command_verification_admission,
)
from kew.merge_verify import VERIFY_EVENT, verify_answers_browser
from kew.review_checkout import (
    CHECKOUT_BUSY,
    HEAD_MISMATCH,
    ReviewCheckoutError,
    materialize_review_checkout,
)
from kew.review_contract import (
    NOT_CHECKED_KINDS,
    merge_verify_failure_sidecar,
    unanswered_test_flags,
)
from kew.review_delta import REVIEW_MODE_MERGE_VERIFY
from kew.review_integrity import read_integrity_report
from kew.review_residuals import acceptance_on_record
from kew.run_store import RunStore
from kew.run_store_acceptance import AcceptanceContractRow
from kew.sandbox import DEFAULT_IMAGE_RUNNER
from kew.subprocess_utils import run
from kew.test_integrity import TestIntegrityReport
from kew.worktree import (
    BaseIntegrationError,
    _fetched_base_sha,
    discard_checkout,
    fetch_ref,
    integrate_base_branch,
)

_logger = logging.getLogger(__name__)

MERGE_QUEUE_LOCK_NAME = "merge-queue.lock"

#: Wall-clock budget for one combined-tree verification. A command that overruns
#: it is a gate that could not finish, never a green and never a PR defect.
MERGE_VERIFY_TIMEOUT_SECONDS = 1800.0

#: How often the epic driver's lock heartbeat is refreshed while a verification
#: runs. Must stay well under ``lock.HEARTBEAT_STALE_SECONDS`` (300s): the
#: verification blocks inside the tick, and a tick-end-only refresh let a
#: verification longer than that mark THIS driver stale, so a second driver of
#: the same epic could steal the lock and tick concurrently.
MERGE_VERIFY_HEARTBEAT_SECONDS = 30.0

#: ``review_mode`` recorded on a merge-queue round. Deliberately neither ``full``
#: nor ``delta``: no reviewer ran, so :func:`kew.review_delta.is_full_round` must
#: not count it as a full review pass, and
#: :func:`kew.review_delta.counts_toward_remediation_cap` must not spend a
#: ``[review] max_rounds`` cycle on it.
MERGE_VERIFY_REVIEW_MODE = REVIEW_MODE_MERGE_VERIFY

# Verdict reasons. Every one withholds the merge; the FAILED family is the only
# one that also records a round for the driver to remediate.
MERGE_QUEUE_BUSY = "merge-queue-busy"
#: A queue lock the kernel would not give us for a reason that is NOT contention
#: — a filesystem without locking, a permission error. Deliberately distinct from
#: ``MERGE_QUEUE_BUSY``: the tick reads a busy queue as another driver working
#: and re-ticks, so a lock that can never be taken had to stop reading as a
#: routine wait forever (#2173 review round 4).
MERGE_QUEUE_LOCK_UNUSABLE = "merge-queue-lock-unusable"
MERGE_VERIFY_UNCONFIGURED = "merge-verify-unconfigured"
MERGE_VERIFY_UNCONTAINED = "merge-verify-uncontained"
MERGE_VERIFY_CHECKOUT_REFUSED = "merge-verify-checkout-refused"
MERGE_VERIFY_BASE_UNMERGEABLE = "merge-verify-base-unmergeable"
MERGE_VERIFY_TIMEOUT = "merge-verify-timeout"
MERGE_VERIFY_UNRUNNABLE = "merge-verify-unrunnable"
#: Another local merge verify holds the machine-wide verify lease (#3190): a
#: hand ``kew verify``, a nightly, another project's drive. Contention, not a
#: broken pipeline — the next round verifies once that one ends.
MERGE_VERIFY_LEASE_BUSY = "merge-verify-lease-busy"
MERGE_VERIFY_FAILED = "merge-verify-failed"
MERGE_VERIFY_HEAD_MOVED = "merge-verify-head-moved"
MERGE_VERIFY_HEAD_UNREADABLE = "merge-verify-head-unreadable"
#: A READY whose reviewer could not run the browser check, with no passing
#: browser-checked verify of the head on record yet (#3421). Re-read every
#: pass: the verify host recording one lifts it, with no hold to release.
MERGE_VERIFY_BROWSER_UNCHECKED = "merge-verify-browser-unchecked"
MERGE_VERIFY_NO_BRANCH = "merge-verify-no-branch"
#: The base branch moved between the tree being verified and the merge (#2184).
#: Benign and self-resolving — the next tick verifies against the new base — so
#: it reports like a moved head rather than like a broken pipeline.
MERGE_VERIFY_BASE_MOVED = "merge-verify-base-moved"
#: The base OID could not be read, so the tree cannot be BOUND to a base at all.
#: A verification whose base is unknown proves nothing about the merge it would
#: release, so it withholds one.
MERGE_VERIFY_BASE_UNREADABLE = "merge-verify-base-unreadable"
#: The PR's own diff could not be read, so the gate-definition check below
#: cannot run. Fails closed: an unread diff is not an empty diff.
MERGE_VERIFY_DIFF_UNREADABLE = "merge-verify-diff-unreadable"

#: The hold a PR that edits its own gate definitions earns (#2184). Shares the
#: ``epic_holds`` vocabulary so the overlay routes the node to HELD and only
#: ``kew epic unhold`` — a human — releases it. The reason carries the head it
#: was written against, which is what that release is scoped to.
MERGE_GATE_TOUCHED = MERGE_GATE_TOUCHED_HOLD_PREFIX

#: Environment variable the contained verify child carries, naming the gates
#: ``[epic] merge_queue_uncontained_gates`` declares unrunnable there. A verify
#: command that can skip them reads it; the queue records them as unproven
#: either way, so a command that ignores it cannot make the green dishonest.
UNCONTAINED_GATES_ENV = "KEW_MERGE_QUEUE_UNCONTAINED_GATES"

#: Why a named gate did not run. Recorded verbatim on every attestation, so the
#: evidence for a green says what the green does not cover.
UNCONTAINED_GATES_REASON = (
    "declared unrunnable under contained verification; covered by CI"
)

# The verdict a failed combined tree is recorded as — a reviewer verdict the
# review stage already knows how to remediate.
_VERDICT_NEEDS_CHANGES = "NEEDS_CHANGES"
_VERDICT_READY = "READY"

#: The ``held`` reasons that mean "another process is doing this work right now",
#: never "an operator must repair something". All are ``flock`` contention — on
#: the repo-wide queue lock, on this PR's review checkout, or on the local verify
#: lease (#3190) — so the holder (or its death) releases them, and the next tick
#: re-evaluates with no intervention. :func:`is_queue_contention` is what keeps
#: them out of the STALLED reading that ends ``kew epic run`` with a repair exit
#: code (#2173 review round 4).
_QUEUE_CONTENTION_REASONS = (
    MERGE_QUEUE_BUSY,
    f"{MERGE_VERIFY_CHECKOUT_REFUSED}: {CHECKOUT_BUSY}",
    MERGE_VERIFY_LEASE_BUSY,
)


def is_queue_contention(reason: str) -> bool:
    """Whether a ``held`` queue reason is live contention (see above)."""
    return reason.startswith(_QUEUE_CONTENTION_REASONS)


@dataclass(frozen=True)
class MergeVerifyResult:
    """Whether the combined tree cleared the queue, and if not, how to report it.

    ``action`` mirrors ``MergeAction``: ``held`` for a queue that could not
    decide (busy, unconfigured, unrunnable — re-evaluated every pass), ``skip``
    for a decided refusal the review stage now owns (a recorded round, or a head
    that moved out from under the verification).

    The tick reads the *reason* to decide whether the epic is broken: a ``held``
    contention reason is another driver working (:func:`is_queue_contention` →
    PROGRESSING), every other ``held`` reason is a pipeline an operator must fix
    (STALLED), and the ``skip`` reasons are awaiting re-review (AWAITING_MERGES).
    A by-design outcome that read STALLED ended ``kew epic run`` with a repair
    exit code (#2173 review round 4), so the distinction is load-bearing.
    """

    ok: bool
    action: Literal["held", "skip"] = "held"
    reason: str = ""
    #: The base commit and candidate tree this passing result verified.
    base_sha: str = ""
    tree_sha: str = ""
    #: Acceptance-contract row ids and versions the verifier ran. ``None``
    #: means this verifier does not run acceptance contracts.
    contracts: tuple[tuple[int, int], ...] | None = None


VERIFIED = MergeVerifyResult(True)


def acceptance_contract_versions(
    contracts: Sequence[AcceptanceContractRow | None],
) -> tuple[tuple[int, int], ...]:
    """Bind each present acceptance contract by durable row id and version."""
    return tuple((row.id, row.version) for row in contracts if row is not None)


def acceptance_contract_change(
    verified: tuple[tuple[int, int], ...],
    current: tuple[tuple[int, int], ...],
) -> str | None:
    """Describe changed contract identities, or None when they still match."""
    if current == verified:
        return None

    def describe(rows: tuple[tuple[int, int], ...]) -> str:
        return ", ".join(f"id {row_id} v{version}" for row_id, version in rows) or (
            "none"
        )

    return f"verified {describe(verified)}; current {describe(current)}"


class MergeVerifier(Protocol):
    """The merge pass's seam onto this module (None disables the queue).

    A context manager, NOT a plain call: the queue lock must still be held while
    the caller performs the merge. Released when the verification returned, it
    left a window in which a second epic driver verified — and merged — a
    combined tree built on a base that did not yet contain the merge about to
    land, the cross-epic invalidation :func:`merge_queue_lock_path` exists to
    prevent. The merge belongs INSIDE the ``with`` body.
    """

    def __call__(
        self, *, item_id: str, pr_number: int, head_oid: str, branch: str
    ) -> AbstractContextManager[MergeVerifyResult]: ...


def merge_queue_lock_path(config: DispatchConfig) -> Path:
    """The REPO-WIDE merge-queue lock path.

    Not per epic, unlike ``_epic_lock_path``: two epics tick concurrently on
    purpose, but they merge into the same base branch, so their verifications
    must queue behind one another or each verifies a tree the other invalidates.
    The lock therefore spans verification AND merge (:class:`MergeVerifier` is a
    context manager for exactly this reason): a lock released at the verdict
    still lets the second driver verify against a base missing the first
    driver's merge.
    """
    base = (
        Path(config.epic.lock_path)
        if config.epic.lock_path
        else Path(config.logging.dir).resolve() / "epic-driver.lock"
    )
    return base.with_name(MERGE_QUEUE_LOCK_NAME)


@dataclass(frozen=True)
class QueueLease:
    """Whether the queue lock is ours, and if not, the refusal to report.

    Two very different things deny a non-blocking ``flock``, and they must not
    report the same way: EWOULDBLOCK is contention — another verification holds
    the queue, the by-design serialization outcome the next tick retries — while
    any other errno is a lock that cannot be taken at all. Collapsed into one
    "busy", a filesystem without locking read as a routine queue wait forever,
    which is exactly the reading :func:`is_queue_contention` now grants.
    """

    acquired: bool
    reason: str = ""


#: The errnos a non-blocking ``flock`` denial uses for "held by someone else".
#: POSIX specifies EWOULDBLOCK; EACCES is the documented alternative spelling.
_CONTENTION_ERRNOS = frozenset({errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES})


def _denied_lease(lock_path: Path, exc: OSError) -> QueueLease:
    """Classify a denied ``flock``: contention, or a lock that cannot be used."""
    if exc.errno in _CONTENTION_ERRNOS:
        _logger.warning("merge queue: %s is held elsewhere: %s", lock_path, exc)
        return QueueLease(False, f"{MERGE_QUEUE_BUSY}: {lock_path} is held")
    _logger.error(
        "merge queue: %s cannot be locked at all — no candidate can be verified "
        "until an operator fixes it: %s",
        lock_path,
        exc,
    )
    return QueueLease(False, f"{MERGE_QUEUE_LOCK_UNUSABLE}: {lock_path}: {exc}")


def _take_queue_lock(handle: IO[str], lock_path: Path) -> QueueLease:
    """Take the queue lock for this process, or classify why we could not."""
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        return _denied_lease(lock_path, exc)
    handle.write(f"{os.getpid()}\n")
    handle.flush()
    return QueueLease(True)


@contextmanager
def merge_queue_lock(lock_path: Path) -> Iterator[QueueLease]:
    """Non-blocking ``flock`` over the queue. Yields a classified refusal or ours.

    Deliberately NOT :func:`kew.graph.driver.lock.single_driver_lock`: that lock
    reclaims a holder whose heartbeat is older than ``HEARTBEAT_STALE_SECONDS``
    (300s), and one combined-tree verification can legitimately run longer than
    that — the steal would put two verifications, and then two merges, on the
    same base at once. A bare flock has no staleness path to get wrong: the
    kernel releases it when the holding process dies, and never before.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle: IO[str] = open(lock_path, "a+", encoding="utf-8")  # noqa: SIM115
    try:
        lease = _take_queue_lock(handle, lock_path)
        if not lease.acquired:
            yield lease
            return
        try:
            yield lease
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


@contextmanager
def keep_driver_lock_alive(keepalive: Callable[[], None] | None) -> Iterator[None]:
    """Beat *keepalive* every ``MERGE_VERIFY_HEARTBEAT_SECONDS`` for the body.

    The verification blocks the epic driver's tick for up to
    ``MERGE_VERIFY_TIMEOUT_SECONDS`` (1800s), and the driver lock's heartbeat is
    otherwise only refreshed when the whole tick returns. Six times over
    ``HEARTBEAT_STALE_SECONDS`` of silence marks a perfectly healthy driver stale,
    and a stale heartbeat is stolen automatically (#970) — which would put a
    second driver of the same epic on the same base concurrently, the exact race
    ``merge_queue_lock`` refuses to allow for the queue lock itself.

    The beat is a daemon thread, so an interpreter teardown never waits on it, and
    it is joined on exit so no beat outlives the verification it covers.
    ``refresh_lock_heartbeat`` is ownership-checked, so a beat that races a real
    steal refuses to write rather than resurrecting the stolen lock.
    """
    if keepalive is None:
        yield
        return
    stop = threading.Event()

    def _beat() -> None:
        while not stop.wait(MERGE_VERIFY_HEARTBEAT_SECONDS):
            keepalive()

    thread = threading.Thread(
        target=_beat, name="kew-merge-queue-heartbeat", daemon=True
    )
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=MERGE_VERIFY_HEARTBEAT_SECONDS)


def merge_queue_verifier(
    config: DispatchConfig,
    store: RunStore,
    *,
    epic_ref: str,
    repo: str,
    base: str,
    repo_root: Path,
    keepalive: Callable[[], None] | None = None,
) -> MergeVerifier:
    """Bind the queue to one epic's tick, as the merge pass's verifier seam.

    *keepalive* refreshes the epic driver's lock heartbeat; see
    :func:`keep_driver_lock_alive`. None (the default) leaves the tick's own
    tick-end refresh as the only one, which is correct only for a caller that
    holds no heartbeat-reclaimable lock.
    """

    @contextmanager
    def _verify(
        *, item_id: str, pr_number: int, head_oid: str, branch: str
    ) -> Iterator[MergeVerifyResult]:
        with verify_combined_tree(
            config,
            store,
            epic_ref=epic_ref,
            repo=repo,
            base=base,
            repo_root=repo_root,
            item_id=item_id,
            pr_number=pr_number,
            head_oid=head_oid,
            branch=branch,
            keepalive=keepalive,
        ) as result:
            yield result

    return _verify


@contextmanager
def verify_combined_tree(
    config: DispatchConfig,
    store: RunStore,
    *,
    epic_ref: str,
    repo: str,
    base: str,
    repo_root: Path,
    item_id: str,
    pr_number: int,
    head_oid: str,
    branch: str,
    keepalive: Callable[[], None] | None = None,
) -> Iterator[MergeVerifyResult]:
    """Verify ``base`` merged into ``head_oid``, holding the queue lock for the body.

    A context manager because the lock must outlive the verdict: the caller
    merges inside the body, and only releasing the lock after that merge keeps
    the next verification — this driver's or another epic driver's — on a base
    that already contains it. A verdict reached before the lock (a standing
    refusal, a preflight refusal) is yielded without one: nothing is verified and
    nothing will be merged, so there is no tree to serialize.
    """
    standing = _standing_refusal(store, item_id=item_id, head_oid=head_oid)
    if standing is not None:
        yield standing
        return
    command = config.commands.verify_loop.strip()
    preflight = _preflight(config, item_id=item_id, command=command)
    if preflight.refusal is not None:
        yield preflight.refusal
        return
    assert preflight.admission is not None
    lock_path = merge_queue_lock_path(config)
    with merge_queue_lock(lock_path) as lease:
        if not lease.acquired:
            _logger.warning(
                "merge queue: %s is not merged on this tick — %s",
                item_id,
                lease.reason,
            )
            yield MergeVerifyResult(False, "held", lease.reason)
            return
        with keep_driver_lock_alive(keepalive):
            # The heartbeat covers the merge too: it is the same blocked tick.
            yield _verify_locked(
                config,
                store,
                epic_ref=epic_ref,
                repo=repo,
                base=base,
                repo_root=repo_root,
                item_id=item_id,
                pr_number=pr_number,
                head_oid=head_oid,
                branch=branch,
                command=command,
                admission=preflight.admission,
            )


@dataclass(frozen=True)
class _Preflight:
    """Either the posture the verify command may run under, or the refusal."""

    admission: HostVerificationAdmission | None = None
    refusal: MergeVerifyResult | None = None


def _preflight(config: DispatchConfig, *, item_id: str, command: str) -> _Preflight:
    """Refuse before any clone: nothing to run, or no authority to run it.

    The admission is the load-bearing half. The scratch clone holds
    agent-authored code and the verify command executes the FILES IN IT — the
    gate script, the build backend, the ``conftest.py`` — so it resolves the
    same containment-first admission the loop gate resolves for the same kind of
    execution (#1946/#2184). Containment is the ordinary outcome; the host
    posture exists only where the operator's own
    ``[sandbox] dangerously_skip_containment`` produces it, and a refusal holds
    the candidate rather than running anything. ``[epic] merge_queue = true`` on
    its own is an opt-in to the queue, never an authority to execute agent code
    on the host.

    The image is the one mapped for this epic's runner, falling back to the
    stock image runner when ``[agent] runner`` is unset — verification needs no
    runner credentials, only the project's toolchain, and an unset runner must
    not be a reason to reach for the host instead.
    """
    if not command:
        # Unreachable through a loaded config (the model validator refuses the
        # combination), so this is the guard for a hand-built one.
        _logger.error(
            "merge queue: %s is not merged — no verify command is configured; "
            "set commands.verify_loop",
            item_id,
        )
        return _Preflight(
            refusal=MergeVerifyResult(
                False,
                "held",
                f"{MERGE_VERIFY_UNCONFIGURED}: set commands.verify_loop",
            )
        )
    admission = resolve_command_verification_admission(
        config.sandbox, runner=config.agent.runner or DEFAULT_IMAGE_RUNNER
    )
    if admission.refusal is not None:
        _logger.error(
            "merge queue: %s is not merged — the verify command has no admitted "
            "posture to run the agent-written combined tree under: %s",
            item_id,
            admission.refusal,
        )
        return _Preflight(
            refusal=MergeVerifyResult(
                False, "held", f"{MERGE_VERIFY_UNCONTAINED}: {admission.refusal}"
            )
        )
    return _Preflight(admission=admission)


def _standing_refusal(
    store: RunStore, *, item_id: str, head_oid: str
) -> MergeVerifyResult | None:
    """The refusal this queue already recorded against this exact head, if any.

    Verifying the same head twice would re-run the whole gate and record a second
    identical round on every tick — an unbounded cost, and unbounded rounds, for
    as long as the head stands. The recorded round IS the refusal: repeat it
    verbatim until the head moves. (In a blocking-review epic the governed-review
    invariant refuses this candidate earlier, so this is the guard for an
    advisory-mode epic, whose review stage still remediates the round.)
    """
    latest = store.latest_review_round(item_id)
    if (
        latest is None
        or latest.head_sha != head_oid
        or latest.verdict != _VERDICT_NEEDS_CHANGES
        or latest.review_mode != MERGE_VERIFY_REVIEW_MODE
    ):
        return None
    _logger.warning(
        "merge queue: %s still carries the round %d merge-verify failure against "
        "head %s — not re-verifying and not merging",
        item_id,
        latest.round,
        head_oid[:12],
    )
    return MergeVerifyResult(
        False,
        "skip",
        f"{MERGE_VERIFY_FAILED}: round {latest.round} stands against this head",
    )


def _verify_locked(
    config: DispatchConfig,
    store: RunStore,
    *,
    epic_ref: str,
    repo: str,
    base: str,
    repo_root: Path,
    item_id: str,
    pr_number: int,
    head_oid: str,
    branch: str,
    command: str,
    admission: HostVerificationAdmission,
) -> MergeVerifyResult:
    """Materialize the combined tree, verify it, and always discard it.

    The scratch tree is the review checkout primitive (#1290) — a standalone
    clone, exclusively claimed, refused when a reviewer already holds it — cut at
    the intended head, which also refuses a head that moved before we started.
    It is discarded on every path: leaving a tree that carries base commits the
    PR head does not would silently change what a later review round reads.
    """
    try:
        checkout = materialize_review_checkout(
            pr_number=pr_number,
            branch_name=branch,
            config=config,
            expected_head_sha=head_oid,
            store=store,
        )
    except ReviewCheckoutError as exc:
        return _refused_checkout(exc, item_id=item_id)
    try:
        return _verify_in_tree(
            checkout.path,
            config,
            store,
            epic_ref=epic_ref,
            repo=repo,
            base=base,
            repo_root=repo_root,
            item_id=item_id,
            pr_number=pr_number,
            head_oid=head_oid,
            command=command,
            admission=admission,
        )
    finally:
        discard_checkout(checkout.path)
        checkout.release()


def _refused_checkout(exc: ReviewCheckoutError, *, item_id: str) -> MergeVerifyResult:
    """Report a checkout the queue could not materialize as what it actually is.

    ``head_mismatch`` is not a broken pipeline: the PR head moved between this
    tick's classification read and the moment the tree was cut, which is the same
    benign condition as a head that moves *during* the verification — so it
    reports under the same ``MERGE_VERIFY_HEAD_MOVED`` reason, the one the tick
    already reads as awaiting re-review rather than a stall. Every other
    classification is held: a busy checkout is contention
    (:func:`is_queue_contention`), the rest need an operator.
    """
    if exc.classification == HEAD_MISMATCH:
        _logger.warning(
            "merge queue: %s head moved before the verification tree was cut — "
            "not merging: %s",
            item_id,
            exc,
        )
        return MergeVerifyResult(False, "skip", MERGE_VERIFY_HEAD_MOVED)
    _logger.error(
        "merge queue: %s is not merged — no verification tree (%s): %s",
        item_id,
        exc.classification,
        exc,
    )
    return MergeVerifyResult(
        False, "held", f"{MERGE_VERIFY_CHECKOUT_REFUSED}: {exc.classification}"
    )


@dataclass(frozen=True)
class _BaseIntegration:
    """Outcome of merging the fetched base into the head."""

    conflicted: tuple[str, ...] = ()
    error: str | None = None


def _fetch_base(worktree: Path, *, base: str, repo_root: Path) -> str | None:
    """Fetch the REAL upstream ``base`` and return the OID it now points at.

    The fetch goes through :func:`kew.worktree.fetch_ref`, so ``origin/<base>``
    in the scratch clone is the upstream branch as it is right now — including
    the sibling this queue merged a moment ago, which exists nowhere locally.
    None means the fetch or the read failed; the caller withholds the merge,
    because a combined tree with no base OID is a tree bound to nothing.
    """
    try:
        fetch_ref(worktree, base, repo_root=repo_root)
        return _fetched_base_sha(worktree, base)
    except Exception as exc:  # noqa: BLE001 — classified by the caller, never a green
        _logger.error("merge queue: cannot read %s in the scratch clone: %s", base, exc)
        return None


def _integrate_base(worktree: Path, *, base: str) -> _BaseIntegration:
    """Merge the already-fetched ``origin/<base>`` into the checked-out head.

    ``strategy="merge"`` is fixed rather than read from ``[resolve]``: the queue
    verifies the tree a squash-merge onto the current base produces, and a rebase
    would verify a rewritten head instead.
    """
    try:
        conflict = integrate_base_branch(
            worktree_dir=worktree, base_branch=base, strategy="merge"
        )
    except BaseIntegrationError as exc:
        return _BaseIntegration(error=str(exc))
    return _BaseIntegration(
        conflicted=tuple(conflict.conflicted_files) if conflict is not None else ()
    )


def _changed_paths(worktree: Path, *, base_sha: str, head_oid: str) -> list[str] | None:
    """The PR's own files: ``git diff --name-only <base>...<head>``.

    Three dots on purpose — the diff against the MERGE BASE, so the files the
    base branch changed since this PR forked are not read as the PR's edits.
    ``--ignore-submodules=none``: a ``.gitmodules`` entry set ``ignore = all``
    would otherwise drop a moved submodule pointer from the list (#3422).
    None means git could not answer, and an unread diff is never an empty one.
    """
    try:
        result = run(
            [
                "git",
                "-C",
                str(worktree),
                "diff",
                "--no-renames",
                "--ignore-submodules=none",
                "--name-only",
                "-z",
                f"{base_sha}...{head_oid}",
                "--",
            ],
            check=False,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        _logger.error("merge queue: git diff could not run: %s", exc)
        return None
    if result.returncode != 0:
        _logger.error(
            "merge queue: git diff exited %s: %s",
            result.returncode,
            (result.stderr or result.stdout or "").strip(),
        )
        return None
    return sorted(path for path in (result.stdout or "").split("\0") if path)


def _matches_gate_path(path: str, pattern: str) -> bool:
    """Whether one changed path is one of the configured gate definitions.

    ``PurePosixPath.full_match`` semantics, so ``**/pyproject.toml`` catches the
    root one and every nested one, and ``frontend/package*.json`` catches the
    lockfile beside the manifest. A trailing ``/`` is the directory form —
    everything under it — which is how an operator names ``scripts/`` without
    having to know the glob spelling.
    """
    if pattern.endswith("/"):
        pattern = f"{pattern}**"
    try:
        return PurePosixPath(path).full_match(pattern)
    except ValueError:  # An unparsable pattern matches nothing, and says so.
        _logger.error("merge queue: ignoring unusable merge_gate_paths %r", pattern)
        return False


def touched_gate_paths(
    paths: Sequence[str], patterns: Sequence[str]
) -> tuple[str, ...]:
    """The changed paths that DEFINE the gates (#2184).

    Public because it is the whole content of the check and is tested directly:
    the queue's command proves nothing about a PR that can edit the command's
    own definition — the gate script it runs, the build backend that assembles
    the project under it, the ``conftest.py`` that collects its tests.
    """
    return tuple(
        path
        for path in paths
        if any(_matches_gate_path(path, pattern) for pattern in patterns)
    )


def _named_paths(paths: tuple[str, ...]) -> str:
    """Name the touched gate paths, bounded — the reason is a stored string an
    operator reads back (the same bound the merge-main hold uses)."""
    named = ", ".join(paths[:MAX_NAMED_PATHS])
    extra = len(paths) - MAX_NAMED_PATHS
    return f"{named} (+{extra} more)" if extra > 0 else named


def _hold_merge_gate(
    store: RunStore, provider: str, *, epic_ref: str, item_id: str, reason: str
) -> None:
    """Persist the gate hold, never over an honored hold we do not own.

    Mirrors ``merge._hold_ci_pending``: ``hold_epic_node`` upserts, and another
    writer's honored reason says something this one cannot. The refusal is
    returned either way, so a node already held for a different reason is still
    not merged — it just keeps the reason that is actually blocking it.
    """
    existing = next(
        (h.reason for h in store.list_epic_holds(epic_ref) if h.item_id == item_id),
        None,
    )
    if (
        existing is not None
        and not existing.startswith(MERGE_GATE_TOUCHED_HOLD_PREFIX)
        and holds_node_out_of_frontier(existing)
    ):
        _logger.info(
            "epic %s: not replacing hold %r with %s — the node is already held "
            "for a reason this pass does not own",
            item_id,
            existing,
            MERGE_GATE_TOUCHED,
        )
        return
    if provider == "github":
        store.hold_epic_node(epic_ref, item_id, reason)
    else:
        store.hold_epic_node_item(epic_ref, item_id, reason)


def _unreleased(
    store: RunStore,
    *,
    epic_ref: str,
    item_id: str,
    head_oid: str,
    named: Sequence[str],
) -> tuple[str, ...]:
    """The risks in *named* no ``kew epic unhold`` of *head_oid* released.

    The hold is DERIVED from the PR's diff and review, so the release has to be
    readable here or the next pass just re-derives it: ``kew epic unhold``
    writes the released reason to this node's ``meta`` receipt in the same
    transaction as the delete. The reason carries the head it was written
    against AND what the human was shown, so a release of a gate-path hold on H
    never answers a ``docker`` a later re-review of H names. A list part — the
    unanswered flags, the unrun checks — is released when the receipt's part
    of the same kind named each of its entries.
    """
    receipt = store.get_meta(merge_gate_ack_key(epic_ref, item_id))
    if merge_gate_reviewed_head(receipt) != head_oid:
        return tuple(named)
    released = merge_gate_parts(receipt)
    return tuple(part for part in named if not _released_part(part, released))


def _released_part(part: str, released: Sequence[str]) -> bool:
    """Whether one named risk is among the *released* parts of a receipt."""
    if part in released:
        return True
    for kind in (MERGE_GATE_INTEGRITY_PART, MERGE_GATE_UNCHECKED_PART):
        if part.startswith(kind):
            wanted = set(part[len(kind) :].split(", "))
            return any(
                was.startswith(kind) and wanted <= set(was[len(kind) :].split(", "))
                for was in released
            )
    return False


def _gate_paths_named(paths: Sequence[str], config: DispatchConfig) -> str | None:
    """The gate definitions a PR's net *paths* edit, named; None for none.

    The command is operator-owned; the files it executes are not. A PR that
    edits ``scripts/verify.sh``, a ``pyproject.toml``, a ``conftest.py`` or the
    container the gates build can decide what proves it, and a green from a gate
    the PR wrote is not evidence. Configured paths only
    (``[epic] merge_gate_paths``, empty = the operator turned the check off),
    and the hold names them so the human who un-holds knows what to read.
    """
    patterns = config.epic.merge_gate_paths
    if not patterns:
        return None
    touched = touched_gate_paths(paths, patterns)
    return _named_paths(touched) if touched else None


def _stored_verdict(
    store: RunStore, item_id: str, head_oid: str, *, accepted: bool = False
) -> dict[str, Any] | None:
    """The verdict this head was reviewed with, as stored.

    The latest round on *head_oid* when it is READY — or, with *accepted*, a
    NEEDS_CHANGES whose residuals the stop rule accepted on both of its records
    (#2112, #2151), the other round a merge path merges on. ``{}`` when the
    head has no such round with a sidecar; None when it has one kew cannot
    stand behind — unreadable, or no longer matching the digest the round
    recorded — so each rule decides how such a verdict fails closed.

    A sidecar FILE that is gone is unreadable too: what its reviewer could not
    run is unknown, not empty, so it holds as ``other`` for a person, and
    ``kew epic unhold`` releases that head like any other hold.
    """
    latest = store.latest_review_round(item_id)
    if latest is None or latest.head_sha != head_oid or not latest.sidecar_path:
        return {}
    if latest.verdict != _VERDICT_READY and not (
        accepted and acceptance_on_record(store, item_id, latest)
    ):
        return {}
    try:
        data = Path(latest.sidecar_path).read_bytes()
        parsed = json.loads(data)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        _logger.warning(
            "merge queue: %s round %d verdict unreadable, so it answers nothing: %s",
            item_id,
            latest.round,
            exc,
        )
        return None
    if hashlib.sha256(data).hexdigest() != latest.sidecar_digest:
        _logger.warning(
            "merge queue: %s round %d verdict no longer matches its digest, so "
            "it answers nothing",
            item_id,
            latest.round,
        )
        return None
    return parsed if isinstance(parsed, dict) else None


def _verdict_answers(store: RunStore, item_id: str, head_oid: str) -> dict[str, Any]:
    """The READY verdict this head was reviewed with, as stored; ``{}`` if none.

    Read from the host's own stored sidecar, and only while it still matches
    the digest the round recorded — a verdict kew cannot stand behind answers
    nothing.
    """
    return _stored_verdict(store, item_id, head_oid) or {}


def _integrity_named(
    report: TestIntegrityReport, store: RunStore, *, item_id: str, head_oid: str
) -> str | None:
    """The test-integrity flags its READY verdict did not answer, named.

    The verdict parser already refuses such a READY (#2486), but only when the
    reviewer was shown the flags under the prompt that states the contract —
    a stale deployed prompt, or a round reviewed before it, reaches here with
    no **Test changes** section at all. A human reads the test changes.
    """
    open_flags = unanswered_test_flags(
        _verdict_answers(store, item_id, head_oid), report.flags
    )
    if not open_flags:
        return None
    return MERGE_GATE_INTEGRITY_PART + ", ".join(open_flags)


#: The ``not_checked`` kinds (#3419) the merge path treats on their own: a
#: browser check a later verify of the head can record, and the full suite a
#: ``kew verify`` merge gate runs itself. Every other kind waits for a person.
UNCHECKED_BROWSER = "browser"
UNCHECKED_FULL_SUITE = "full_suite"
_UNCHECKED_OTHER = "other"


def unchecked_kinds(answers: Mapping[str, Any]) -> tuple[str, ...]:
    """The kinds a stored verdict's ``not_checked`` names, sorted, once each.

    Read off the raw sidecar, never a re-parsed verdict: every re-parser runs
    without the coverage contract and leaves ``not_checked`` empty. A sidecar
    with no ``not_checked`` key was written before #3419 and names nothing.
    Anything else that is not a list of entries naming a known kind — an entry
    with no kind, an invented one, a ``not_checked`` that is not a list — is a
    check the reviewer could not run that this cannot classify, so it reads as
    ``other`` and waits for a person.
    """
    raw = answers.get("not_checked")
    if raw is None:
        return ()
    if not isinstance(raw, list):
        return (_UNCHECKED_OTHER,)
    kinds: set[str] = set()
    for entry in raw:
        kind = entry.get("kind") if isinstance(entry, dict) else None
        named = kind.strip().lower() if isinstance(kind, str) else ""
        kinds.add(named if named in NOT_CHECKED_KINDS else _UNCHECKED_OTHER)
    return tuple(sorted(kinds))


def review_unchecked_kinds(
    store: RunStore, item_id: str, head_oid: str
) -> tuple[str, ...]:
    """The :func:`unchecked_kinds` of the round *head_oid* merges on, as stored:
    its latest READY, or the accepted NEEDS_CHANGES every merge path takes as
    READY (#2112) — whose reviewer ran in the same sandbox.

    A sidecar kew cannot stand behind (unreadable, or off its digest) may list
    anything, so it reads as ``other``: a person reads it.
    """
    stored = _stored_verdict(store, item_id, head_oid, accepted=True)
    return (_UNCHECKED_OTHER,) if stored is None else unchecked_kinds(stored)


def ran_a_browser_module(payload: Mapping[str, Any]) -> bool:
    """Whether a verify that ran ``ui-check`` ran any module of it: its
    ``ui_check_modules`` is ``None`` (or absent, from before #3213) for every
    module, else the list a scoped run ran."""
    modules = payload.get("ui_check_modules")
    if modules is None:
        return True
    return isinstance(modules, list) and len(modules) > 0


def browser_checked(store: RunStore, head: str) -> bool:
    """Whether a passing ``kew verify`` that ran the UI check verified *head*:
    the head itself, or a merge whose ``ref_parents`` carry it (the drive's
    combined tree of main merged into the head, an operator's merge). Every
    merge path — both queues and the drive — reads this one predicate.

    A scoped run (``[verify] frontend = "scoped"``, #3213) counts when it ran
    at least one module: a scoped run of none made the builds and opened no
    browser, so it checked nothing."""
    for event in store.list_audit_events(limit=500, event=VERIFY_EVENT):
        payload = event.payload or {}
        parents = payload.get("ref_parents")
        verified = payload.get("ref_sha") == head or (
            isinstance(parents, list) and head in parents
        )
        if (
            verified
            and payload.get("ui_check") is True
            and payload.get("outcome") == "passed"
            and ran_a_browser_module(payload)
        ):
            return True
    return False


@dataclass(frozen=True)
class UnrunChecks:
    """What a head's reviewed verdict could not check, as a merge path acts on it."""

    #: The kinds only a person can stand behind, sorted: held, and released by
    #: the head's ``kew epic unhold`` receipt.
    person: tuple[str, ...] = ()
    #: A browser check a later ``kew verify`` of the head can still record.
    browser_wait: bool = False


def unrun_checks(
    store: RunStore,
    *,
    item_id: str,
    head_oid: str,
    verify_gate: bool,
    ui_checkable: bool,
) -> UnrunChecks:
    """Sort what the merged-on verdict of *head_oid* could not check (#3421).

    The reviewer runs offline, with no browser, Docker or network, and a READY
    needs only one executed command. *verify_gate* says the path's own merge
    gate runs the full suite — the epic queue's ``[commands] verify_loop``, or
    a ``kew verify`` its caller forces full for a head that names one, whatever
    ``[verify] suite`` says — so a ``full_suite`` entry adds nothing there;
    where it does not, the full suite is as unrun as Docker. A browser check
    waits for a passing browser-checked verify of the head — but only while
    *ui_checkable*, a ui-check verify of the head's diff answering every
    browser check it needs (:func:`verify_answers_browser`):
    for any other diff no verify record answers it, so the browser check is a
    person's. Docker, network, host and other checks are always a person's.
    """
    kinds = [
        kind
        for kind in review_unchecked_kinds(store, item_id, head_oid)
        if not (kind == UNCHECKED_FULL_SUITE and verify_gate)
    ]
    person = [kind for kind in kinds if kind != UNCHECKED_BROWSER]
    if UNCHECKED_BROWSER not in kinds:
        return UnrunChecks(person=tuple(sorted(person)))
    if not ui_checkable:
        # No verify record answers it, however the UI check went.
        return UnrunChecks(person=tuple(sorted([*person, UNCHECKED_BROWSER])))
    return UnrunChecks(
        person=tuple(sorted(person)),
        browser_wait=not browser_checked(store, head_oid),
    )


def _merge_gate_refusal(
    store: RunStore,
    provider: str,
    *,
    epic_ref: str,
    item_id: str,
    head_oid: str,
    named: Sequence[str],
    browser_wait: bool,
) -> MergeVerifyResult | None:
    """Hold *head_oid* for every risk in *named* at once, or wait for a browser
    check of it; None when nothing is left to answer.

    ONE hold names the gate definitions the PR edits, the test-integrity flags
    its READY left unanswered and the checks only a person can run, so the
    human who un-holds sees everything the release answers: one
    ``kew epic unhold`` receipt for this head releases them all.

    Releasable, which a derived hold is not for free: the diff does not change
    when the ``epic_holds`` row is deleted, so a bare unhold would be re-held by
    this very function on the next pass, forever. The release an operator
    already performs leaves a receipt naming the head it read (see
    :func:`_unreleased`), and a receipt for THIS head lets the node
    through. A push moves the head, the receipt stops matching and the new diff
    is held again — the receipt says "a human read this diff", never "this node
    is exempt". It answers only the risks the released reason named
    (:func:`_unreleased`): a re-review of the same head that adds one is held
    again. It never answers a browser check a verify can still record.
    """
    if named:
        what = MERGE_GATE_PARTS_SEP.join(named)
        new = _unreleased(
            store, epic_ref=epic_ref, item_id=item_id, head_oid=head_oid, named=named
        )
        if not new:
            _logger.info(
                "merge queue: %s carries %s, and a human released that exact "
                "head with `kew epic unhold` — verifying it",
                item_id,
                what,
            )
        else:
            reason = merge_gate_hold_reason(head_oid, what)
            _logger.error(
                "merge queue: %s is HELD — %s (not released for this head: %s); "
                "a human reads the diff, runs what the review could not, and "
                "`kew epic unhold`s it",
                item_id,
                what,
                MERGE_GATE_PARTS_SEP.join(new),
            )
            _hold_merge_gate(
                store, provider, epic_ref=epic_ref, item_id=item_id, reason=reason
            )
            return MergeVerifyResult(False, "held", reason)
    if not browser_wait:
        return None
    _logger.warning(
        "merge queue: %s waits — its READY verdict could not run the browser "
        "check, and no passing browser-checked verify of %s is on record",
        item_id,
        head_oid[:12],
    )
    return MergeVerifyResult(False, "skip", MERGE_VERIFY_BROWSER_UNCHECKED)


def merge_gate_refusal(
    paths: Sequence[str] | None,
    integrity_root: Path,
    config: DispatchConfig,
    store: RunStore,
    *,
    epic_ref: str,
    item_id: str,
    base_sha: str,
    head_oid: str,
) -> MergeVerifyResult | None:
    """Refuse a head the queue's verify cannot prove, before it runs.

    *paths* is the PR's net diff against *base_sha* (None: git could not read
    it, and an unread diff is never an empty one); *integrity_root* is a
    checkout holding both commits, which the test-integrity report reads. The
    gate definitions it edits (#2184), the test-integrity flags its READY left
    unanswered (#2486) and the checks its review could not run (#3421) are held
    together under one reason (:func:`_merge_gate_refusal`); a browser check a
    verify of the head can still record waits instead.
    """
    report = (
        None
        if paths is None
        else read_integrity_report(integrity_root, base_sha, head_oid)
    )
    if paths is None or report is None:
        span = f"{base_sha[:12]}...{head_oid[:12]}"
        return MergeVerifyResult(
            False, "held", f"{MERGE_VERIFY_DIFF_UNREADABLE}: cannot read {span}"
        )
    unrun = unrun_checks(
        store,
        item_id=item_id,
        head_oid=head_oid,
        verify_gate=True,
        ui_checkable=verify_answers_browser(paths),
    )
    named = [
        what
        for what in (
            _gate_paths_named(paths, config),
            _integrity_named(report, store, item_id=item_id, head_oid=head_oid),
            (
                MERGE_GATE_UNCHECKED_PART + ", ".join(unrun.person)
                if unrun.person
                else None
            ),
        )
        if what is not None
    ]
    return _merge_gate_refusal(
        store,
        provider_from_config(config),
        epic_ref=epic_ref,
        item_id=item_id,
        head_oid=head_oid,
        named=named,
        browser_wait=unrun.browser_wait,
    )


def _unchecked_refusal(
    store: RunStore,
    *,
    provider: str,
    epic_ref: str,
    item_id: str,
    head_oid: str,
    verify_gate: bool = True,
    ui_checkable: bool,
) -> MergeVerifyResult | None:
    """:func:`_merge_gate_refusal` of the unrun checks alone, for a path that
    reads no diff of its own (the merge with no queue)."""
    unrun = unrun_checks(
        store,
        item_id=item_id,
        head_oid=head_oid,
        verify_gate=verify_gate,
        ui_checkable=ui_checkable,
    )
    return _merge_gate_refusal(
        store,
        provider,
        epic_ref=epic_ref,
        item_id=item_id,
        head_oid=head_oid,
        named=(
            [MERGE_GATE_UNCHECKED_PART + ", ".join(unrun.person)]
            if unrun.person
            else []
        ),
        browser_wait=unrun.browser_wait,
    )


@dataclass(frozen=True)
class _CombinedTree:
    """A tree ready to verify, or the refusal that stopped it being built."""

    base_sha: str = ""
    refusal: MergeVerifyResult | None = None


def _prepare_combined_tree(
    worktree: Path,
    config: DispatchConfig,
    store: RunStore,
    *,
    epic_ref: str,
    repo: str,
    base: str,
    repo_root: Path,
    item_id: str,
    pr_number: int,
    head_oid: str,
    posture: str | None,
) -> _CombinedTree:
    """Bind a base OID, check the gate definitions, then merge the base in.

    *posture* is the admission the verify command WOULD have run under; it is
    only used to attest the one decision this step can make on its own (a base
    that will not merge), so that refusal reads like every other one.
    """
    base_sha = _fetch_base(worktree, base=base, repo_root=repo_root)
    if base_sha is None:
        return _CombinedTree(
            refusal=MergeVerifyResult(
                False, "held", f"{MERGE_VERIFY_BASE_UNREADABLE}: {base}"
            )
        )
    gate_refusal = merge_gate_refusal(
        _changed_paths(worktree, base_sha=base_sha, head_oid=head_oid),
        worktree,
        config,
        store,
        epic_ref=epic_ref,
        item_id=item_id,
        base_sha=base_sha,
        head_oid=head_oid,
    )
    if gate_refusal is not None:
        return _CombinedTree(refusal=gate_refusal)
    integration = _integrate_base(worktree, base=base)
    if integration.error is not None:
        _logger.error(
            "merge queue: %s is not merged — the base integration could not run: %s",
            item_id,
            integration.error,
        )
        return _CombinedTree(
            refusal=MergeVerifyResult(
                False, "held", f"{MERGE_VERIFY_BASE_UNMERGEABLE}: {integration.error}"
            )
        )
    if integration.conflicted:
        files = ", ".join(integration.conflicted)
        return _CombinedTree(
            refusal=_failed(
                store,
                config,
                epic_ref=epic_ref,
                repo=repo,
                base=base,
                base_sha=base_sha,
                item_id=item_id,
                pr_number=pr_number,
                head_oid=head_oid,
                command=f"git merge origin/{base}",
                gate=f"merge with {base}",
                evidence=f"conflicted files: {files}",
                posture=posture,
            )
        )
    return _CombinedTree(base_sha=base_sha)


def _uncontained_gates(config: DispatchConfig) -> tuple[str, ...]:
    """Gates the operator declares the contained verification cannot run."""
    return tuple(config.epic.merge_queue_uncontained_gates)


def _executed_command(command: str, gates: Sequence[str]) -> str:
    """The verify command with the declared-unrunnable gates named in its env.

    The contained child inherits nothing from the host, so the declaration has
    to travel inside the program: an export the operator's command may read to
    skip those gates. ``shlex.quote`` keeps a gate name a value and never a
    second command. The clean command is what gets reported and attested; this
    string only ever runs.
    """
    if not gates:
        return command
    value = shlex.quote(",".join(gates))
    return f"{UNCONTAINED_GATES_ENV}={value}; export {UNCONTAINED_GATES_ENV}; {command}"


def _run_verify(
    worktree: Path,
    config: DispatchConfig,
    *,
    command: str,
    admission: HostVerificationAdmission,
) -> ConditionResult:
    """Run the verify command over the combined tree under *admission*."""
    return CommandCondition(
        _executed_command(command, _uncontained_gates(config)),
        timeout=MERGE_VERIFY_TIMEOUT_SECONDS,
        needs_writes=config.commands.verify_loop_needs_writes,
    ).check_admitted_directory(worktree, admission)


def _base_binding_refusal(
    worktree: Path,
    *,
    base: str,
    base_sha: str,
    repo_root: Path,
    item_id: str,
) -> MergeVerifyResult | None:
    """Refuse ANY verdict whose base moved while the verification was running.

    The queue lock serializes kew's own merges, so the base can only move under
    a verification when something OUTSIDE kew merges — a human, another bot, a
    revert. A tree built on the old base then proves nothing about the merge it
    would release, which is the whole point of verifying a combined tree. The
    OID is re-read from the real upstream as late as possible, still under the
    lock; the residual window between this read and GitHub's own merge is
    GitHub's to close (the merge argv binds the HEAD with
    ``--match-head-commit`` and GitHub re-checks mergeability itself).

    A RED tree is checked here too, and before the failure is attributed to the
    PR (#2192). The base that moved may be exactly what made the tree red, and
    a NEEDS_CHANGES round is not a withheld merge the next tick retries: it
    stands against the head until a remediation and a re-review clear it, so
    the queue would refuse to re-verify the new combined tree for a whole cycle
    over a defect the PR may not have.
    """
    current = _fetch_base(worktree, base=base, repo_root=repo_root)
    if current is None:
        return MergeVerifyResult(
            False, "held", f"{MERGE_VERIFY_BASE_UNREADABLE}: {base}"
        )
    if current == base_sha:
        return None
    _logger.warning(
        "merge queue: %s verified against %s at %s but it moved to %s — not "
        "merging; the next tick verifies the new combined tree",
        item_id,
        base,
        base_sha[:12],
        current[:12],
    )
    return MergeVerifyResult(False, "skip", MERGE_VERIFY_BASE_MOVED)


def _candidate_tree_sha(worktree: Path) -> tuple[str | None, str | None]:
    """The candidate's tree id, or the process failure detail."""
    result = run(
        ["git", "-C", str(worktree), "rev-parse", "HEAD^{tree}"],
        check=False,
    )
    tree_sha = (result.stdout or "").strip().lower()
    if result.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", tree_sha):
        return tree_sha, None
    detail = (result.stderr or result.stdout or "").strip() or "no tree id"
    return None, detail


def _verify_in_tree(
    worktree: Path,
    config: DispatchConfig,
    store: RunStore,
    *,
    epic_ref: str,
    repo: str,
    base: str,
    repo_root: Path,
    item_id: str,
    pr_number: int,
    head_oid: str,
    command: str,
    admission: HostVerificationAdmission,
) -> MergeVerifyResult:
    """Build the combined tree, verify it, and report the outcome."""
    prepared = _prepare_combined_tree(
        worktree,
        config,
        store,
        epic_ref=epic_ref,
        repo=repo,
        base=base,
        repo_root=repo_root,
        item_id=item_id,
        pr_number=pr_number,
        head_oid=head_oid,
        posture=admission.posture,
    )
    if prepared.refusal is not None:
        return prepared.refusal

    result = _run_verify(worktree, config, command=command, admission=admission)
    if result.timed_out or result.unrunnable:
        # A gate that could not run is never a green, and never a PR defect
        # either: report it as the operator problem it is and withhold the merge.
        reason = MERGE_VERIFY_TIMEOUT if result.timed_out else MERGE_VERIFY_UNRUNNABLE
        _logger.error(
            "merge queue: %s is not merged — the verify command did not complete: %s",
            item_id,
            result.evidence,
        )
        return MergeVerifyResult(False, "held", f"{reason}: {result.evidence}")
    # BEFORE the red/green split, not after it: a red tree whose base moved is
    # a tree that will never be merged, so reading it as a PR defect records a
    # NEEDS_CHANGES round against a head that may be perfectly good — and that
    # standing round then blocks the re-verification of the NEW combined tree
    # until a full remediation and re-review cycle has run (#2192).
    moved = _base_binding_refusal(
        worktree,
        base=base,
        base_sha=prepared.base_sha,
        repo_root=repo_root,
        item_id=item_id,
    )
    if moved is not None:
        return moved
    if not result.met:
        return _failed(
            store,
            config,
            epic_ref=epic_ref,
            repo=repo,
            base=base,
            base_sha=prepared.base_sha,
            item_id=item_id,
            pr_number=pr_number,
            head_oid=head_oid,
            command=command,
            gate=command,
            evidence=result.evidence,
            posture=admission.posture,
        )

    tree_sha, tree_error = _candidate_tree_sha(worktree)
    if tree_sha is None:
        return MergeVerifyResult(
            False,
            "held",
            f"{MERGE_VERIFY_UNRUNNABLE}: verified candidate tree unreadable: "
            f"{tree_error}",
        )

    _attest(
        store,
        repo=repo,
        item_id=item_id,
        payload={
            "result": "pass",
            "base": base,
            "base_sha": prepared.base_sha,
            "tree_sha": tree_sha,
            "sha": head_oid,
            "pr": pr_number,
            "command": command,
            "posture": admission.posture,
            **_unproven_payload(config),
        },
    )
    _logger.info(
        "merge queue: %s verified against %s at %s with `%s` (%s) — merging",
        item_id,
        base,
        prepared.base_sha[:12],
        command,
        admission.posture,
    )
    return MergeVerifyResult(True, base_sha=prepared.base_sha, tree_sha=tree_sha)


def _unproven_payload(config: DispatchConfig) -> dict[str, Any]:
    """What this verification did NOT prove, on the record with the green.

    A gate the contained run cannot execute is not a gate that passed. Naming
    it on the attestation is what keeps the green honest — and keeps the reader
    pointed at CI, which is what actually covers those gates.
    """
    gates = _uncontained_gates(config)
    if not gates:
        return {}
    return {"skipped_gates": list(gates), "skipped_reason": UNCONTAINED_GATES_REASON}


def _failed(
    store: RunStore,
    config: DispatchConfig,
    *,
    epic_ref: str,
    repo: str,
    base: str,
    base_sha: str,
    item_id: str,
    pr_number: int,
    head_oid: str,
    command: str,
    gate: str,
    evidence: str,
    posture: str | None,
) -> MergeVerifyResult:
    """Send the failure back to the item as a round the driver can remediate.

    The round is recorded against the head that was verified, so the review
    stage reads it as findings standing against the CURRENT head and remediates
    (and the merge gate reads the same round as not-READY until it does). The
    sidecar carries the evidenced, high-severity finding naming *gate*; the audit
    event records the refusal even if nothing ever reads the round.

    The attestation carries the same facts as the green one (#2192): a red is a
    decision about a specific combined tree, so without the base OID and the
    posture nothing can say afterwards WHICH main this head was red against or
    what it ran under — and the gates the containment could not run are as
    unproven here as they are on a pass, which is what the docs and the
    ``merge_queue_uncontained_gates`` contract promise for EVERY attestation.
    """
    sidecar = merge_verify_failure_sidecar(
        Path(config.logging.dir).resolve(),
        f"merge-verify-{item_id.replace('/', '-')}-{head_oid[:12]}",
        base=base,
        head_sha=head_oid,
        command=command,
        evidence=evidence,
    )
    store.record_review_round(
        epic_ref,
        item_id,
        head_sha=head_oid,
        verdict=_VERDICT_NEEDS_CHANGES,
        sidecar_digest=sidecar.digest,
        sidecar_path=str(sidecar.stored_path),
        review_mode=MERGE_VERIFY_REVIEW_MODE,
    )
    _attest(
        store,
        repo=repo,
        item_id=item_id,
        payload={
            "result": "fail",
            "base": base,
            "base_sha": base_sha,
            "sha": head_oid,
            "pr": pr_number,
            "command": command,
            "gate": gate,
            "posture": posture,
            "sidecar_digest": sidecar.digest,
            **_unproven_payload(config),
        },
    )
    _logger.error(
        "merge queue: %s NOT merged — the combined tree (%s + %s) fails %s: %s",
        item_id,
        base,
        head_oid[:12],
        gate,
        evidence,
    )
    return MergeVerifyResult(False, "skip", f"{MERGE_VERIFY_FAILED}: {gate}")


def _attest(
    store: RunStore, *, repo: str, item_id: str, payload: dict[str, Any]
) -> None:
    """Record one queue decision. Facts ride in ``payload`` (never new columns).

    A non-numeric tracker item attests under the chained 0 sentinel and stays
    addressable by ``item_id``, like ``merge._attest``.
    """
    emit(
        store,
        "merge_verify",
        actor=Actor("policy", "epic-driver"),
        issue_num=int(item_id) if GITHUB_ID_RE.fullmatch(item_id) else 0,
        item_id=item_id,
        repo=repo,
        payload=payload,
    )
