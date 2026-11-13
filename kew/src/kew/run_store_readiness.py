"""Durable retention of the realized route profile of every paid attempt.

``run_admission_identity`` (I01) answers *what work* a run was admitted for.
This module answers the other half of admission: *what was actually about to
run* — which runner executable (by version and digest) or which immutable
container image ID, which requested model, endpoint and tier, which source
tracker versus which inference provider, which auth path, which containment,
and the material fingerprint of the whole boundary.

Design rules this module enforces:

* **Every paid attempt owns its own profile.** The primary key is
  ``(run_id, attempt_number)``. Attempt 2 cannot borrow attempt 1's evidence:
  :meth:`RouteProfileStoreMixin.require_route_profile` looks up the exact
  attempt, so a retry that never captured a boundary fails closed instead of
  inheriting a stale one.
* **Capture happens inside admission.** :meth:`route_profile_enricher` returns
  the bounded ``AdmissionEnricher`` ``admit_run_with_identity`` runs inside the
  open transaction, so the run row, its identity, and its profile commit
  together or not at all.
* **Recorded facts are immutable.** Re-recording a byte-identical profile is an
  idempotent no-op; recording a *different* one for the same attempt raises
  :class:`RouteProfileConflictError`.
* **Later observations append.** What a run *reported* (its reported model, a
  runtime version read after launch) lands in ``run_route_observation``, an
  append-only log beside the profile. It can never rewrite a declared or
  enforced fact — naming an immutable field there is refused outright.
* **Legacy rows stay unknown.** There is no backfill: a run admitted before
  this table simply has no profile row at all, and
  ``RealizedRouteProfile.restore`` turns that absence into an explicitly
  all-unavailable profile rather than a reconstruction from columns that never
  meant this.

Second slice (I05A): readiness *probes* — the durable lifecycle, single-spend
lease, execution handles and cleanup acknowledgement of an actual bounded
provider call, plus the one identity-less ``runs`` row that call pays on.

Layering note: this module deliberately knows nothing about
:mod:`kew.readiness`. ``kew.run_store`` is the bottom layer of the dispatch
stack and may not reach up into ``kew.runners`` (which the composition module
imports), so a profile arrives here as a structural
:class:`RecordableRouteProfile` and leaves as a :class:`StoredRouteProfile`
row carrying the canonical JSON this module stores but never interprets.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, TypeVar, runtime_checkable

import kew.run_store

if TYPE_CHECKING:
    from kew.run_store_work import AdmissionEnricher, AdmissionIdentity

_T = TypeVar("_T")


_PROFILE_COLS = (
    "run_id",
    "attempt_number",
    "runner",
    "requested_model",
    "containment",
    "source_provider",
    "inference_provider",
    "runner_executable",
    "runner_version",
    "runner_digest",
    "image_ref",
    "image_id",
    "endpoint",
    "service_tier",
    "auth_path",
    "fingerprint",
    "profile_json",
    "recorded_at",
)

_PROFILE_SELECT = f"SELECT {', '.join(_PROFILE_COLS)} FROM run_route_profile"  # nosec B608

IMMUTABLE_PROFILE_FIELDS = frozenset(_PROFILE_COLS)
"""Field names an appended observation may never claim to restate."""

_OBSERVATION_COLS = (
    "run_id",
    "attempt_number",
    "field",
    "value",
    "source",
    "observed_at",
)

_OBSERVATION_SELECT = (
    f"SELECT {', '.join(_OBSERVATION_COLS)} FROM run_route_observation"  # nosec B608
)


class RouteProfileError(ValueError):
    """A realized route profile (or an appended observation) is not honest."""


class RouteProfileConflictError(RuntimeError):
    """This attempt already recorded a different realized route profile."""


class RouteProfileMissingError(RuntimeError):
    """This attempt has no realized route profile of its own."""


@runtime_checkable
class RecordableRouteProfile(Protocol):
    """What this module needs of a composed profile to persist it.

    Structural on purpose: ``kew.readiness.RealizedRouteProfile`` satisfies it
    without this module importing it, which is what keeps the store off the
    ``kew.runners`` layer it sits below.
    """

    @property
    def run_id(self) -> str: ...

    @property
    def attempt_number(self) -> int: ...

    @property
    def fingerprint(self) -> str: ...

    def to_json(self) -> str: ...

    def column_values(self) -> Mapping[str, str | None]: ...


@dataclass(frozen=True)
class StoredRouteProfile:
    """One attempt's retained profile, exactly as it was recorded.

    The denormalized columns exist so the profile is queryable (every attempt
    that ran a given image ID, every attempt on a given auth path) without
    parsing JSON. ``profile_json`` remains the canonical record: it carries the
    provenance of every fact, and it — not the columns — is what
    ``fingerprint`` covers.
    """

    run_id: str
    attempt_number: int
    runner: str
    requested_model: str
    containment: str | None
    source_provider: str | None
    inference_provider: str | None
    runner_executable: str | None
    runner_version: str | None
    runner_digest: str | None
    image_ref: str | None
    image_id: str | None
    endpoint: str | None
    service_tier: str | None
    auth_path: str | None
    fingerprint: str
    profile_json: str
    recorded_at: datetime | None = None


@dataclass(frozen=True)
class RouteObservation:
    """One appended post-launch observation about a profiled attempt."""

    run_id: str
    attempt_number: int
    field: str
    value: str | None
    source: str
    observed_at: datetime | None = None


_PROFILE_FACT_COLS = _PROFILE_COLS[
    _PROFILE_COLS.index("containment") : _PROFILE_COLS.index("fingerprint")
]


def _profile_row(profile: RecordableRouteProfile) -> tuple[object, ...]:
    """The persisted column tuple for *profile*, in ``_PROFILE_COLS`` order."""
    if not profile.run_id or profile.attempt_number < 1:
        raise RouteProfileError("a realized profile requires a run id and attempt")
    columns = profile.column_values()
    missing = {"runner", "requested_model"}.difference(columns)
    if missing:
        raise RouteProfileError(f"profile is missing required columns: {missing}")
    return (
        profile.run_id,
        profile.attempt_number,
        columns["runner"],
        columns["requested_model"],
        *(columns.get(name) for name in _PROFILE_FACT_COLS),
        profile.fingerprint,
        profile.to_json(),
        kew.run_store._dt_to_iso(datetime.now(UTC)),
    )


def _row_to_profile(row: sqlite3.Row) -> StoredRouteProfile:
    values: dict[str, object] = {
        field.name: row[field.name]
        for field in fields(StoredRouteProfile)
        if field.name != "recorded_at"
    }
    return StoredRouteProfile(
        recorded_at=kew.run_store._iso_to_dt(row["recorded_at"]),
        **values,  # type: ignore[arg-type]
    )


def _row_to_observation(row: sqlite3.Row) -> RouteObservation:
    return RouteObservation(
        run_id=row["run_id"],
        attempt_number=row["attempt_number"],
        field=row["field"],
        value=row["value"],
        source=row["source"],
        observed_at=kew.run_store._iso_to_dt(row["observed_at"]),
    )


class RouteProfileStoreMixin:
    """The *readiness* slice of ``SqliteRunStore``: realized route profiles.

    Mixed into ``SqliteRunStore``; relies on the composition root for the
    connection, the execute wrapper, and the shared transaction lock.
    """

    if TYPE_CHECKING:
        # Provided by the composed SqliteRunStore — declared here so the mixin
        # body type-checks in isolation (mypy strict).
        _conn: sqlite3.Connection
        _txn_lock: threading.Lock

        def _execute(
            self, sql: str, params: sqlite3._Parameters | None = None
        ) -> sqlite3.Cursor: ...

    # ---------------------------------------------------------------- write

    def route_profile_enricher(
        self, profile: RecordableRouteProfile
    ) -> AdmissionEnricher:
        """The admission hook that binds *profile* to the run being admitted.

        Handed to ``admit_run_with_identity(enrich=...)``: it runs inside the
        already-open admission transaction, after the run row and its identity,
        so a profile write that refuses (a conflicting or dishonest profile)
        rolls the admission back rather than admitting an unprofiled run.
        """

        def _enrich(cursor: sqlite3.Cursor) -> None:
            self._write_profile(cursor, profile)

        return _enrich

    def record_route_profile(self, profile: RecordableRouteProfile) -> None:
        """Record the profile of a paid attempt outside an admission.

        Immutability is the same as at admission — an identical re-write is a
        no-op, a different one raises. Retries do NOT use this: a retry's
        profile and its attempt admission identity land together through
        :meth:`record_attempt_admission`, so a crash mid-retry can never leave
        a profiled attempt with no identity (#1550 review F4).
        """
        with self._txn_lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._write_profile(self._conn.cursor(), profile)
            except BaseException:
                self._conn.rollback()
                raise
            self._conn.commit()

    def record_attempt_admission(
        self, profile: RecordableRouteProfile, identity: AdmissionIdentity
    ) -> None:
        """Attempt N's admission identity and realized profile, one transaction.

        The retry-path twin of the admission enricher: attempt 1 binds run row,
        identity, and profile in ``admit_run_with_identity``; every later paid
        attempt binds its identity and profile HERE, before the provider call,
        so the two can never disagree across a crash window.
        """
        from kew.run_store_work import normalize_admission_identity

        identity = normalize_admission_identity(identity)
        with self._txn_lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._write_identity(identity)  # type: ignore[attr-defined]
                self._write_profile(self._conn.cursor(), profile)
            except BaseException:
                self._conn.rollback()
                raise
            self._conn.commit()

    def _write_profile(
        self, cursor: sqlite3.Cursor, profile: RecordableRouteProfile
    ) -> None:
        """Insert *profile*, tolerating an identical re-write only.

        Must run inside the caller's open transaction: a conflicting row is
        detected by reading back within it, so the raise unwinds whatever else
        that transaction wrote beside it.
        """
        row = _profile_row(profile)
        existing = cursor.execute(
            f"{_PROFILE_SELECT} WHERE run_id = ? AND attempt_number = ?",
            (profile.run_id, profile.attempt_number),
        ).fetchone()
        if existing is not None:
            if existing["fingerprint"] != profile.fingerprint:
                raise RouteProfileConflictError(
                    f"run {profile.run_id} attempt {profile.attempt_number} "
                    "already recorded a different realized route profile"
                )
            return
        cursor.execute(
            # nosec B608 — column list is a module constant; values are bound
            "INSERT INTO run_route_profile"  # nosec B608
            f" ({', '.join(_PROFILE_COLS)})"
            f" VALUES ({', '.join('?' * len(_PROFILE_COLS))})",
            row,
        )

    def append_route_observation(
        self,
        run_id: str,
        attempt_number: int,
        *,
        field: str,
        value: str | None,
        source: str,
    ) -> None:
        """Append one post-launch observation beside an attempt's profile.

        Append-only by construction: observations accumulate in their own table
        and never touch the profile row. Naming an immutable profile field is
        refused — a reported model is evidence about the requested one, not a
        correction to it. An attempt with no profile of its own has nothing to
        observe against, so it raises rather than creating a floating fact.
        """
        if not source.strip():
            raise RouteProfileError("an observation requires a source")
        if field in IMMUTABLE_PROFILE_FIELDS:
            raise RouteProfileError(
                f"{field!r} is an immutable admission fact — observations append, "
                "they do not rewrite the profile"
            )
        self.require_route_profile(run_id, attempt_number)
        self._execute(
            # nosec B608 — column list is a module constant; values are bound
            "INSERT INTO run_route_observation"  # nosec B608
            f" ({', '.join(_OBSERVATION_COLS)})"
            f" VALUES ({', '.join('?' * len(_OBSERVATION_COLS))})",
            (
                run_id,
                attempt_number,
                field,
                value,
                source,
                kew.run_store._dt_to_iso(datetime.now(UTC)),
            ),
        )

    # ----------------------------------------------------------------- read

    def get_route_profile(
        self, run_id: str, attempt_number: int | None = None
    ) -> StoredRouteProfile | None:
        """The profile of *attempt_number* (default: the latest), or ``None``."""
        if attempt_number is None:
            row = self._conn.execute(
                f"{_PROFILE_SELECT} WHERE run_id = ?"
                " ORDER BY attempt_number DESC LIMIT 1",
                (run_id,),
            ).fetchone()
        else:
            row = self._conn.execute(
                f"{_PROFILE_SELECT} WHERE run_id = ? AND attempt_number = ?",
                (run_id, attempt_number),
            ).fetchone()
        return None if row is None else _row_to_profile(row)

    def route_profile_json(self, run_id: str, attempt_number: int = 1) -> str | None:
        """The canonical retained profile payload, or ``None`` for a legacy run.

        ``None`` is the honest answer for an attempt admitted before profiles
        existed; ``RealizedRouteProfile.restore`` turns it into explicit
        unknowns rather than a reconstruction.
        """
        found = self.get_route_profile(run_id, attempt_number)
        return None if found is None else found.profile_json

    def require_route_profile(
        self, run_id: str, attempt_number: int
    ) -> StoredRouteProfile:
        """The attempt's own profile, or raise — the pre-launch fail-closed gate.

        Called immediately before a paid child starts: a launch with no
        immutable profile for *this* attempt is refused rather than silently
        attributed to the boundary some earlier attempt resolved.
        """
        found = self.get_route_profile(run_id, attempt_number)
        if found is None:
            raise RouteProfileMissingError(
                f"run {run_id} attempt {attempt_number} has no realized route "
                "profile — an unprofiled paid attempt must not launch"
            )
        return found

    def list_route_profiles(self, run_id: str) -> list[StoredRouteProfile]:
        """Every attempt's profile for *run_id*, oldest attempt first."""
        rows = self._conn.execute(
            f"{_PROFILE_SELECT} WHERE run_id = ? ORDER BY attempt_number",
            (run_id,),
        ).fetchall()
        return [_row_to_profile(row) for row in rows]

    def list_route_observations(
        self, run_id: str, attempt_number: int | None = None
    ) -> list[RouteObservation]:
        """Appended observations for *run_id*, in the order they were observed."""
        if attempt_number is None:
            rows = self._conn.execute(
                f"{_OBSERVATION_SELECT} WHERE run_id = ? ORDER BY id", (run_id,)
            ).fetchall()
        else:
            rows = self._conn.execute(
                f"{_OBSERVATION_SELECT} WHERE run_id = ? AND attempt_number = ?"
                " ORDER BY id",
                (run_id, attempt_number),
            ).fetchall()
        return [_row_to_observation(row) for row in rows]


# ---------------------------------------------------------------------------
# Readiness probes (I05A): lifecycle, lease, evidence, and the one spend row.
# ---------------------------------------------------------------------------

PROBE_LEASE_TTL = timedelta(minutes=15)
"""How long a leased probe may stay in flight before it is reconcilable."""

PROBE_EVIDENCE_TTL = timedelta(hours=24)
"""How long settled probe evidence answers for its exact fingerprint."""


class ProbeState(StrEnum):
    """Lifecycle of one readiness probe.

    ``LEASED`` is the only state that holds the per-fingerprint single-spend
    lease. ``SUCCEEDED`` and ``FAILED`` are both *evidence* — a probe that
    proved the boundary is broken answers the next lookup exactly as usefully
    as one that proved it works. ``ABANDONED`` is the crash truth: the lease
    outlived its holder and nobody ever reported what the attempt cost.
    """

    LEASED = "leased"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ABANDONED = "abandoned"


class ProbeVerdict(StrEnum):
    """What the store can say about an exact fingerprint right now."""

    HIT = "hit"
    MISS = "miss"
    EXPIRED = "expired"


class ReadinessProbeError(ValueError):
    """A readiness probe record is not internally honest."""


class ProbeLeaseHeldError(RuntimeError):
    """Another probe already holds the live lease for this fingerprint."""


class ProbeStateError(RuntimeError):
    """This probe is not in a state that admits the requested transition."""


@runtime_checkable
class RecordableProbeCost(Protocol):
    """What this module needs of a probe cost to persist it.

    Structural for the same reason as :class:`RecordableRouteProfile`:
    ``kew.readiness.ProbeCost`` satisfies it without the store importing
    ``kew.readiness`` — and through it ``kew.runners``, which sits above the
    store in the layered dispatch stack.
    """

    @property
    def amount_usd(self) -> float: ...

    @property
    def source(self) -> str: ...

    @property
    def certainty(self) -> str: ...


_PROBE_COLS = (
    "probe_id",
    "fingerprint",
    "run_id",
    "state",
    "planned_cost_usd",
    "planned_cost_source",
    "planned_cost_certainty",
    "recorded_cost_usd",
    "recorded_cost_source",
    "recorded_cost_certainty",
    "session_id",
    "container_name",
    "pgid",
    "cleanup_acknowledged",
    "detail",
    "leased_at",
    "lease_expires_at",
    "settled_at",
)

_PROBE_SELECT = f"SELECT {', '.join(_PROBE_COLS)} FROM readiness_probe"  # nosec B608

_PROBE_ABANDONED_CATEGORY = "readiness_probe_abandoned"
_PROBE_FAILED_CATEGORY = "readiness_probe_failed"


@dataclass(frozen=True)
class StoredReadinessProbe:
    """One probe's retained intent and evidence, exactly as recorded.

    ``run_id`` names the single operational ``runs`` row this probe pays on —
    the sole actual-spend truth. The cost columns record what was written
    THERE and with what certainty; they are not a second wallet, which is why
    they are kept flat and uninterpreted here. ``kew.readiness.ProbeCost``
    rules on them (``ProbeCost.restore``); this module only stores them.
    """

    probe_id: str
    fingerprint: str
    run_id: str
    state: ProbeState
    planned_cost_usd: float
    planned_cost_source: str
    planned_cost_certainty: str
    recorded_cost_usd: float | None
    recorded_cost_source: str | None
    recorded_cost_certainty: str | None
    session_id: str | None
    container_name: str | None
    pgid: int | None
    cleanup_acknowledged: bool
    detail: str
    leased_at: datetime | None
    lease_expires_at: datetime | None
    settled_at: datetime | None


@dataclass(frozen=True)
class ProbeLookupResult:
    """The answer to "does this exact boundary already have fresh evidence?"."""

    verdict: ProbeVerdict
    probe: StoredReadinessProbe | None


# The certainty column is stored flat and uninterpreted (see _cost_columns);
# this mirrors readiness.Provenance.UNAVAILABLE without importing it, keeping
# the store free of the domain enum.
_UNAVAILABLE_CERTAINTY = "unavailable"


def _cost_columns(cost: RecordableProbeCost) -> tuple[float, str, str]:
    """Validate and flatten *cost* into its three persisted columns."""
    amount = float(cost.amount_usd)
    if not (amount >= 0) or amount == float("inf"):
        raise ReadinessProbeError("a probe cost must be a finite, non-negative amount")
    certainty = str(cost.certainty)
    if not certainty:
        raise ReadinessProbeError("a probe cost must state how well it is known")
    return amount, str(cost.source), certainty


def _row_to_probe(row: sqlite3.Row) -> StoredReadinessProbe:
    return StoredReadinessProbe(
        probe_id=row["probe_id"],
        fingerprint=row["fingerprint"],
        run_id=row["run_id"],
        state=ProbeState(row["state"]),
        planned_cost_usd=row["planned_cost_usd"],
        planned_cost_source=row["planned_cost_source"],
        planned_cost_certainty=row["planned_cost_certainty"],
        recorded_cost_usd=row["recorded_cost_usd"],
        recorded_cost_source=row["recorded_cost_source"],
        recorded_cost_certainty=row["recorded_cost_certainty"],
        session_id=row["session_id"],
        container_name=row["container_name"],
        pgid=row["pgid"],
        cleanup_acknowledged=bool(row["cleanup_acknowledged"]),
        detail=row["detail"],
        leased_at=kew.run_store._iso_to_dt(row["leased_at"]),
        lease_expires_at=kew.run_store._iso_to_dt(row["lease_expires_at"]),
        settled_at=kew.run_store._iso_to_dt(row["settled_at"]),
    )


def _probe_verdict(probe: StoredReadinessProbe, now: datetime) -> ProbeVerdict:
    """Classify one probe row against the 24-hour evidence window.

    A leased probe is in flight, not evidence; an abandoned one never reported
    anything. Both read as a MISS so the caller probes again rather than
    inheriting a claim nobody ever made.
    """
    if probe.state in (ProbeState.LEASED, ProbeState.ABANDONED):
        return ProbeVerdict.MISS
    if probe.settled_at is None:
        return ProbeVerdict.MISS
    return (
        ProbeVerdict.HIT
        if now - probe.settled_at <= PROBE_EVIDENCE_TTL
        else ProbeVerdict.EXPIRED
    )


def _probe_run_record(
    probe_id: str, *, model: str, runner: str, started_at: datetime, planned: float
) -> kew.run_store.RunRecord:
    """The one operational ``runs`` row a probe's actual spend lands on.

    Deliberately identity-less: ``issue_num`` is the ``0`` sentinel and
    ``item_id`` is left ``None`` so the store backfills it from the unique
    ``run_id`` — a probe gets its own fleet key and can never be mistaken for,
    or aggregated into, a tracker work item. ``complexity`` stays empty for the
    same reason: it is what buckets the cost-anomaly baseline, and a $0.02
    capability check is not a sample of what dispatched work costs.
    """
    return kew.run_store.RunRecord(
        run_id=probe_id,
        issue_num=0,
        branch="",
        status="pending",
        exit_code=-1,
        started_at=started_at,
        run_kind=kew.run_store.READINESS_PROBE_RUN_KIND,
        model=model,
        runner_name=runner,
        complexity="",
        title="readiness probe",
        planned_cost_usd=planned,
    )


class ReadinessProbeStoreMixin:
    """The probe slice of ``SqliteRunStore``: lifecycle, lease, and evidence.

    Two rows, one truth. The ``readiness_probe`` row owns the probe's *intent
    and evidence* — which exact boundary, which lease, which execution handles,
    whether cleanup was acknowledged, how well its cost is known. The ``runs``
    row it names owns the *money*, under ``run_kind='readiness_probe'`` and
    with no work-item identity at all. Spend is written once, there; the probe
    row's cost columns record what was written and with what certainty rather
    than keeping a second copy that could drift.

    Mixed into ``SqliteRunStore``; relies on the composition root for the
    connection, the busy-retry wrapper, and the shared transaction lock.
    """

    if TYPE_CHECKING:
        # Provided by the composed SqliteRunStore — declared here so the mixin
        # body type-checks in isolation (mypy strict).
        _conn: sqlite3.Connection
        _txn_lock: threading.Lock
        _read_only: bool

        def read_ledger_id(self) -> str | None: ...

        def _execute(
            self, sql: str, params: sqlite3._Parameters | None = None
        ) -> sqlite3.Cursor: ...

        def _with_busy_retry(self, operation: str, fn: Callable[[], _T]) -> _T: ...

        @staticmethod
        def _run_record_params(
            record: kew.run_store.RunRecord,
        ) -> tuple[object, ...]: ...

    def readiness_proxy_key(self, *, create: bool = True) -> str | None:
        """Load the operator HMAC key; ledger identities and salts are public data.

        Creation is chosen by the OPERATION, not by how the store was opened:
        ``kew serve`` holds a writable store, yet its GET routes are passive
        reads and must not write a secret to disk (#2564). A passive caller
        passes ``create=False``; a read-only store never creates either way.
        ``None`` means there is no key yet.
        """
        from kew.readiness_key import existing_readiness_hmac_key, readiness_hmac_key

        if not create or getattr(self, "_read_only", False):
            return existing_readiness_hmac_key()
        return readiness_hmac_key()

    # ---------------------------------------------------------------- lease

    def lease_readiness_probe(
        self,
        *,
        probe_id: str,
        fingerprint: str,
        planned_cost: RecordableProbeCost,
        model: str = "",
        runner: str = "",
        now: datetime | None = None,
        lease_ttl: timedelta = PROBE_LEASE_TTL,
    ) -> StoredReadinessProbe:
        """Claim the single-spend lease on *fingerprint* and open its spend row.

        One transaction does all of it: reconcile any lease that outlived its
        holder, refuse if a live one remains, then write BOTH the probe row and
        its operational ``runs`` row. Writing the spend row here — before the
        provider call, not after it — is what makes a crash mid-probe still
        cost what it cost: the row already exists, and reconciliation
        terminalizes it at the declared ceiling instead of losing it.

        ``BEGIN IMMEDIATE`` takes the write lock up front, so the "is a lease
        live?" check and the claim are one critical section across processes,
        not a read-then-write race. The partial unique index on
        ``readiness_probe(fingerprint) WHERE state='leased'`` is the schema-level
        backstop behind that.
        """
        moment = now or datetime.now(UTC)
        if not probe_id or not fingerprint:
            raise ReadinessProbeError("a probe requires an id and a fingerprint")
        if lease_ttl <= timedelta(0):
            raise ReadinessProbeError("a probe lease must have a positive duration")
        amount, source, certainty = _cost_columns(planned_cost)

        def _claim() -> StoredReadinessProbe:
            with self._txn_lock:
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    cursor = self._conn.cursor()
                    self._expire_stale_leases(cursor, moment)
                    self._refuse_live_lease(cursor, fingerprint)
                    self._execute(
                        kew.run_store._UPSERT_SQL,
                        self._run_record_params(
                            _probe_run_record(
                                probe_id,
                                model=model,
                                runner=runner,
                                started_at=moment,
                                planned=amount,
                            )
                        ),
                    )
                    self._insert_probe(
                        cursor,
                        probe_id=probe_id,
                        fingerprint=fingerprint,
                        cost=(amount, source, certainty),
                        leased_at=moment,
                        expires_at=moment + lease_ttl,
                    )
                except BaseException:
                    self._conn.rollback()
                    raise
                self._conn.commit()
            found = self.get_readiness_probe(probe_id)
            assert found is not None  # nosec B101 — just committed above
            return found

        return self._with_busy_retry("lease_readiness_probe", _claim)

    def _refuse_live_lease(self, cursor: sqlite3.Cursor, fingerprint: str) -> None:
        """Raise if *fingerprint* still holds a live lease after reconciliation."""
        held = cursor.execute(
            "SELECT probe_id FROM readiness_probe WHERE fingerprint = ? AND state = ?",
            (fingerprint, ProbeState.LEASED.value),
        ).fetchone()
        if held is not None:
            raise ProbeLeaseHeldError(
                f"readiness probe {held['probe_id']} already holds the lease for "
                "this exact boundary — one boundary pays at most once at a time"
            )

    def _insert_probe(
        self,
        cursor: sqlite3.Cursor,
        *,
        probe_id: str,
        fingerprint: str,
        cost: tuple[float, str, str],
        leased_at: datetime,
        expires_at: datetime,
    ) -> None:
        try:
            cursor.execute(
                # nosec B608 — column list is a module constant; values are bound
                "INSERT INTO readiness_probe"  # nosec B608
                f" ({', '.join(_PROBE_COLS)})"
                f" VALUES ({', '.join('?' * len(_PROBE_COLS))})",
                (
                    probe_id,
                    fingerprint,
                    probe_id,
                    ProbeState.LEASED.value,
                    *cost,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    0,
                    "",
                    kew.run_store._dt_to_iso(leased_at),
                    kew.run_store._dt_to_iso(expires_at),
                    None,
                ),
            )
        except sqlite3.IntegrityError as exc:
            # The partial unique index fired: another writer took the lease
            # between our read and our insert. Same refusal, same meaning.
            raise ProbeLeaseHeldError(
                "another readiness probe took the lease for this exact boundary"
            ) from exc

    # ------------------------------------------------------------ execution

    def attach_probe_execution(
        self,
        probe_id: str,
        *,
        session_id: str | None = None,
        container_name: str | None = None,
        pgid: int | None = None,
    ) -> None:
        """Record the handles a probe's execution can later be cleaned up by.

        Mirrored onto the operational run row so the ordinary orphan-reaping
        machinery can see a probe's container and process group exactly as it
        sees a dispatch's. Only a leased probe has an execution to describe.
        """

        def _attach() -> None:
            with self._txn_lock:
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    probe = self._require_leased(self._conn.cursor(), probe_id)
                    handles = (session_id, container_name, pgid)
                    self._execute(
                        "UPDATE readiness_probe SET"
                        " session_id = COALESCE(?, session_id),"
                        " container_name = COALESCE(?, container_name),"
                        " pgid = COALESCE(?, pgid)"
                        " WHERE probe_id = ?",
                        (*handles, probe_id),
                    )
                    self._execute(
                        "UPDATE runs SET session_id = COALESCE(?, session_id),"
                        " container_name = COALESCE(?, container_name),"
                        " pgid = COALESCE(?, pgid)"
                        " WHERE run_id = ? AND run_kind = ?",
                        (
                            *handles,
                            probe.run_id,
                            kew.run_store.READINESS_PROBE_RUN_KIND,
                        ),
                    )
                except BaseException:
                    self._conn.rollback()
                    raise
                self._conn.commit()

        self._with_busy_retry("attach_probe_execution", _attach)

    def acknowledge_probe_cleanup(self, probe_id: str) -> None:
        """Record that this probe's execution resources were released.

        Idempotent and allowed in any state: cleanup happens after a probe
        settles AND after one is abandoned, and the abandoned case is exactly
        the one worth keeping visible (see
        :meth:`list_probes_awaiting_cleanup`).
        """

        def _ack() -> None:
            cursor = self._execute(
                "UPDATE readiness_probe SET cleanup_acknowledged = 1"
                " WHERE probe_id = ?",
                (probe_id,),
            )
            if cursor.rowcount != 1:
                self._conn.rollback()
                raise ProbeStateError(f"no readiness probe {probe_id!r}")
            self._conn.commit()

        self._with_busy_retry("acknowledge_probe_cleanup", _ack)

    # ----------------------------------------------------------- settlement

    def settle_readiness_probe(
        self,
        probe_id: str,
        *,
        succeeded: bool,
        recorded_cost: RecordableProbeCost,
        detail: str = "",
        now: datetime | None = None,
    ) -> StoredReadinessProbe:
        """Close out a leased probe and write its actual spend, once.

        A probe whose lease already expired may still settle: it really did
        run and it really did cost something, and the honest record of that
        beats the reconciler's declared-ceiling stand-in.
        """
        moment = now or datetime.now(UTC)
        amount, source, certainty = _cost_columns(recorded_cost)
        state = ProbeState.SUCCEEDED if succeeded else ProbeState.FAILED

        def _settle() -> StoredReadinessProbe:
            with self._txn_lock:
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    probe = self._require_leased(self._conn.cursor(), probe_id)
                    self._execute(
                        "UPDATE readiness_probe SET state = ?,"
                        " recorded_cost_usd = ?, recorded_cost_source = ?,"
                        " recorded_cost_certainty = ?, detail = ?, settled_at = ?"
                        " WHERE probe_id = ?",
                        (
                            state.value,
                            amount,
                            source,
                            certainty,
                            detail,
                            kew.run_store._dt_to_iso(moment),
                            probe_id,
                        ),
                    )
                    self._terminalize_spend_row(
                        probe,
                        status="success" if succeeded else "failure",
                        error_category="" if succeeded else _PROBE_FAILED_CATEGORY,
                        # The money row must never read an unknowable cost as
                        # $0 (that is the vanishing this node exists to prevent):
                        # when the recorded cost is unavailable, the declared
                        # planned ceiling stands in — exactly as the reconciler
                        # does for a crashed lease. The probe row keeps its
                        # honest certainty=unavailable above; only the summed
                        # spend falls back.
                        amount=(
                            probe.planned_cost_usd
                            if certainty == _UNAVAILABLE_CERTAINTY
                            else amount
                        ),
                        finished_at=moment,
                    )
                except BaseException:
                    self._conn.rollback()
                    raise
                self._conn.commit()
            found = self.get_readiness_probe(probe_id)
            assert found is not None  # nosec B101 — just committed above
            return found

        return self._with_busy_retry("settle_readiness_probe", _settle)

    def reconcile_readiness_probes(
        self, *, now: datetime | None = None
    ) -> list[StoredReadinessProbe]:
        """Abandon every lease that outlived its holder; return what changed.

        The declared planned ceiling becomes the recorded cost, carrying its
        ``DECLARED`` certainty with it: a crashed probe's real spend is
        unknowable, and the conservative bound that says so is the only honest
        number. Cleanup stays unacknowledged, so the resources the crash may
        have stranded remain visible rather than being written off here.
        """
        moment = now or datetime.now(UTC)

        def _reconcile() -> list[StoredReadinessProbe]:
            with self._txn_lock:
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    abandoned = self._expire_stale_leases(self._conn.cursor(), moment)
                except BaseException:
                    self._conn.rollback()
                    raise
                self._conn.commit()
            return [p for p in (self.get_readiness_probe(i) for i in abandoned) if p]

        return self._with_busy_retry("reconcile_readiness_probes", _reconcile)

    def _expire_stale_leases(self, cursor: sqlite3.Cursor, now: datetime) -> list[str]:
        """Abandon expired leases inside the caller's open transaction."""
        stale = cursor.execute(
            f"{_PROBE_SELECT} WHERE state = ? AND lease_expires_at <= ?",
            (ProbeState.LEASED.value, kew.run_store._dt_to_iso(now)),
        ).fetchall()
        for row in stale:
            probe = _row_to_probe(row)
            cursor.execute(
                "UPDATE readiness_probe SET state = ?, recorded_cost_usd = ?,"
                " recorded_cost_source = ?, recorded_cost_certainty = ?,"
                " detail = ?, settled_at = ? WHERE probe_id = ?",
                (
                    ProbeState.ABANDONED.value,
                    probe.planned_cost_usd,
                    probe.planned_cost_source,
                    probe.planned_cost_certainty,
                    "lease expired before the probe reported an outcome",
                    kew.run_store._dt_to_iso(now),
                    probe.probe_id,
                ),
            )
            self._terminalize_spend_row(
                probe,
                status="failure",
                error_category=_PROBE_ABANDONED_CATEGORY,
                amount=probe.planned_cost_usd,
                finished_at=now,
            )
        return [row["probe_id"] for row in stale]

    def _terminalize_spend_row(
        self,
        probe: StoredReadinessProbe,
        *,
        status: str,
        error_category: str,
        amount: float,
        finished_at: datetime,
    ) -> None:
        """Write the probe's final money onto its one operational run row.

        Scoped to ``run_kind = 'readiness_probe'`` so this can never reach a
        dispatch row even if a probe id ever collided with one.
        """
        duration = 0.0
        if probe.leased_at is not None:
            duration = max(0.0, (finished_at - probe.leased_at).total_seconds())
        self._execute(
            "UPDATE runs SET status = ?, exit_code = ?, error_category = ?,"
            " estimated_cost_usd = ?, finished_at = ?, duration_seconds = ?"
            " WHERE run_id = ? AND run_kind = ?",
            (
                status,
                0 if status == "success" else 1,
                error_category,
                amount,
                kew.run_store._dt_to_iso(finished_at),
                duration,
                probe.run_id,
                kew.run_store.READINESS_PROBE_RUN_KIND,
            ),
        )

    def _require_leased(
        self, cursor: sqlite3.Cursor, probe_id: str
    ) -> StoredReadinessProbe:
        row = cursor.execute(
            f"{_PROBE_SELECT} WHERE probe_id = ?", (probe_id,)
        ).fetchone()
        if row is None:
            raise ProbeStateError(f"no readiness probe {probe_id!r}")
        probe = _row_to_probe(row)
        if probe.state is not ProbeState.LEASED:
            raise ProbeStateError(
                f"readiness probe {probe_id!r} is already {probe.state.value} — "
                "a settled probe's evidence and spend are final"
            )
        return probe

    # ----------------------------------------------------------------- read

    def lookup_readiness_probe(
        self, fingerprint: str, *, now: datetime | None = None
    ) -> ProbeLookupResult:
        """Fresh evidence for this EXACT boundary, or why there is none.

        Exact-match only, by design: the fingerprint covers the whole realized
        boundary, so one character of drift is a different question and must
        not reuse an answer about something else.
        """
        moment = now or datetime.now(UTC)
        row = self._conn.execute(
            f"{_PROBE_SELECT} WHERE fingerprint = ? AND settled_at IS NOT NULL"
            " ORDER BY settled_at DESC LIMIT 1",
            (fingerprint,),
        ).fetchone()
        if row is None:
            return ProbeLookupResult(verdict=ProbeVerdict.MISS, probe=None)
        probe = _row_to_probe(row)
        verdict = _probe_verdict(probe, moment)
        return ProbeLookupResult(
            verdict=verdict,
            probe=None if verdict is ProbeVerdict.MISS else probe,
        )

    def get_readiness_probe(self, probe_id: str) -> StoredReadinessProbe | None:
        row = self._conn.execute(
            f"{_PROBE_SELECT} WHERE probe_id = ?", (probe_id,)
        ).fetchone()
        return None if row is None else _row_to_probe(row)

    def list_readiness_probes(
        self, *, fingerprint: str | None = None
    ) -> list[StoredReadinessProbe]:
        """Every probe (optionally for one fingerprint), oldest lease first."""
        if fingerprint is None:
            rows = self._conn.execute(
                f"{_PROBE_SELECT} ORDER BY leased_at, probe_id"
            ).fetchall()
        else:
            rows = self._conn.execute(
                f"{_PROBE_SELECT} WHERE fingerprint = ? ORDER BY leased_at, probe_id",
                (fingerprint,),
            ).fetchall()
        return [_row_to_probe(row) for row in rows]

    def list_probes_awaiting_cleanup(self) -> list[StoredReadinessProbe]:
        """Settled or abandoned probes whose cleanup nobody acknowledged.

        The crash-truth surface: an abandoned probe with an unacknowledged
        container is a stranded resource, and it stays listed until someone
        says otherwise.
        """
        rows = self._conn.execute(
            f"{_PROBE_SELECT} WHERE cleanup_acknowledged = 0 AND state != ?"
            " ORDER BY leased_at, probe_id",
            (ProbeState.LEASED.value,),
        ).fetchall()
        return [_row_to_probe(row) for row in rows]
