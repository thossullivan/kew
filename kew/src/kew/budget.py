"""Budget types and pre-dispatch budget guard."""

import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from kew.config import BudgetConfig, DispatchConfig, ReviewConfig, load_config
from kew.epic_holds import WINDOW_BUDGET_HOLD_PREFIX
from kew.ids import item_ref
from kew.run_store import (
    ADVERSARIAL_REVIEW_RUN_KIND,
    PR_SUBJECT_RUN_KINDS,
    REVIEW_REMEDIATION_RUN_KIND,
    REVIEW_RUN_KINDS,
    REVIEW_STAGE_RUN_KIND,
    RunRecord,
    RunStore,
)

# assign-to-kew delegation (D1/#920) shares the epic dispatch run_kind buckets.
# A delegation run tags its spend with this so per-(model, complexity, run_kind)
# breaker baselines — and the report run-kind breakdown — never mix it with
# cheap first-dispatch medians (I1/#445). Registered in report_export's
# ``_RUN_KIND_ORDER`` alongside dispatch/review/loop/planning.
DELEGATION_RUN_KIND = "delegation"


@dataclass
class BudgetStatus:
    """Snapshot of current spend vs configured limits."""

    issue_spent_usd: float
    issue_limit_usd: float
    daily_spent_usd: float
    daily_limit_usd: float
    weekly_spent_usd: float
    weekly_limit_usd: float

    @property
    def is_within_budget(self) -> bool:
        return (
            self.issue_spent_usd <= self.issue_limit_usd
            and self.daily_spent_usd <= self.daily_limit_usd
            and self.weekly_spent_usd <= self.weekly_limit_usd
        )


class BudgetExceeded(Exception):
    """Raised when a budget limit would be exceeded before dispatch."""

    def __init__(
        self,
        which_limit: str,
        limit_usd: float,
        spent_usd: float,
        status: BudgetStatus,
        *,
        recorded_usd: float | None = None,
        reserved_usd: float = 0.0,
    ) -> None:
        self.which_limit = which_limit
        self.limit_usd = limit_usd
        self.spent_usd = spent_usd
        self.status = status
        # The window's settled spend alone, where ``spent_usd`` also values
        # in-flight runs at their reservation (#3205). None when not recorded.
        self.recorded_usd = recorded_usd
        # What the OTHER in-flight runs hold on top of their recorded spend,
        # never the refused launch's own ceiling (#3205).
        self.reserved_usd = reserved_usd
        super().__init__(
            f"Budget exceeded: {which_limit} limit ${limit_usd:.2f}, "
            f"spent ${spent_usd:.2f}"
        )


@dataclass(frozen=True)
class BudgetView:
    """Effective, human-facing budget picture for the pre-dispatch banner (CS-7).

    Combines the two overlapping knobs the banner used to hide: the runtime
    kill cap (``agent.max_budget_usd``, passed to the runner) and the preflight
    per-issue / daily gate (``budget.*``, checked against historical spend).

    ``issue_spent_usd`` / ``daily_spent_usd`` are ``None`` when spend is
    unknowable (no store, or no issue selected yet) so the renderer can show a
    dash instead of a misleading ``$0.00``.
    """

    runtime_cap_usd: float
    runtime_cap_source: str  # "default" | "kew.toml" | "override"
    per_issue_cap_usd: float
    issue_spent_usd: float | None
    daily_cap_usd: float
    daily_spent_usd: float | None
    issue_num: int | None = None
    # Durable work-item id (A3/L3). For a GitHub item this is the stringified
    # ``issue_num``; for a non-numeric tracker (Linear) it carries the real key
    # (``SFD-12``) so per-item spend never collapses onto the ``0`` sentinel.
    item_id: str | None = None

    @property
    def per_issue_remaining_usd(self) -> float | None:
        """Per-issue headroom left, or ``None`` when spend is unknown."""
        if self.issue_spent_usd is None:
            return None
        return self.per_issue_cap_usd - self.issue_spent_usd

    @property
    def daily_remaining_usd(self) -> float | None:
        """Daily headroom left, or ``None`` when spend is unknown."""
        if self.daily_spent_usd is None:
            return None
        return self.daily_cap_usd - self.daily_spent_usd

    @property
    def runtime_cap_exceeds_issue_headroom(self) -> bool:
        """True when a single run's kill cap can overshoot per-issue headroom.

        This is the CS-7 trap: a $25 kill cap under a $0.40 per-issue budget
        lets one run blow past the per-issue budget before the preflight gate
        (which only sees *historical* spend) ever fires. When spend is unknown
        we compare against the full per-issue cap (the first-run case).
        """
        remaining = self.per_issue_remaining_usd
        if remaining is None:
            remaining = self.per_issue_cap_usd
        return self.runtime_cap_usd > remaining


def _runtime_cap_source(config: DispatchConfig, store: RunStore | None) -> str:
    """Provenance of the runtime kill cap: default / kew.toml / override.

    "override" means an env var or a DB ``config_overrides`` row set it (both
    outrank kew.toml in :func:`kew.config.load_config`). Otherwise a value
    present in the parsed config is "kew.toml"; an untouched field is "default".
    """
    if os.environ.get("KEW_MAX_BUDGET"):
        return "override"
    if store is not None and store.get_config_override("agent.max_budget_usd"):
        return "override"
    if "max_budget_usd" in config.agent.model_fields_set:
        return "kew.toml"
    return "default"


def effective_budget_view(
    config: DispatchConfig,
    store: RunStore | None,
    issue: int | None,
    *,
    item_id: str | None = None,
    now: datetime | None = None,
) -> BudgetView:
    """Build the effective budget picture rendered in the dispatch banner.

    Pure over ``config`` + the store's recorded spend so the math is unit
    testable. Per-item spend needs both a store and an item; daily spend
    needs a store. Anything unavailable is reported as ``None``.

    ``item_id`` is the durable per-item key (A3): when given, per-item spend is
    read via ``list_by_item`` so a non-numeric tracker (Linear, ``issue_num=0``)
    never shares the ``0`` sentinel bucket. Left ``None`` (the classic GitHub
    banner call) it falls back to ``list_by_issue`` keyed on ``issue``.
    """
    if now is None:
        now = datetime.now(UTC)

    issue_spent: float | None = None
    daily_spent: float | None = None
    if store is not None:
        if item_id is not None:
            issue_spent = sum(r.estimated_cost_usd for r in store.list_by_item(item_id))
        elif issue is not None:
            issue_spent = sum(r.estimated_cost_usd for r in store.list_by_issue(issue))
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        daily_spent = sum(r.estimated_cost_usd for r in store.list_since(day_start))

    return BudgetView(
        runtime_cap_usd=config.agent.max_budget_usd,
        runtime_cap_source=_runtime_cap_source(config, store),
        per_issue_cap_usd=config.budget.per_issue_usd,
        issue_spent_usd=issue_spent,
        daily_cap_usd=config.budget.daily_usd,
        daily_spent_usd=daily_spent,
        issue_num=issue,
        item_id=item_id,
    )


def _money(v: float) -> str:
    """Compact cap rendering: whole dollars drop the ``.00`` ($25, $100)."""
    if v == int(v):
        return f"${int(v)}"
    return f"${v:.2f}"


def _spent(v: float | None) -> str:
    """Spent-so-far rendering: 2 decimals, or a dash when unknown."""
    return "—" if v is None else f"${v:.2f}"


def render_budget_line(view: BudgetView) -> str:
    """One shared compact banner block for all dispatch call sites.

    e.g. ``Budget: run cap $25 (default) · issue $0.00/$0.40 · today $12.30/$100``
    """
    return (
        f"Budget: run cap {_money(view.runtime_cap_usd)} ({view.runtime_cap_source})"
        f" · issue {_spent(view.issue_spent_usd)}/{_money(view.per_issue_cap_usd)}"
        f" · today {_spent(view.daily_spent_usd)}/{_money(view.daily_cap_usd)}"
    )


def runtime_cap_conflict_warning(view: BudgetView) -> str | None:
    """Warning text when the runtime kill cap overshoots per-issue headroom.

    Returns ``None`` when the caps are aligned (nothing to warn about).
    """
    if not view.runtime_cap_exceeds_issue_headroom:
        return None
    remaining = view.per_issue_remaining_usd
    if remaining is None:
        remaining = view.per_issue_cap_usd
    return (
        f"Runtime cap {_money(view.runtime_cap_usd)} exceeds per-issue headroom "
        f"${max(0.0, remaining):.2f} — a run may spend past the per-issue budget "
        f"before the preflight gate ever sees it. Align agent.max_budget_usd with "
        f"budget.per_issue_usd."
    )


def per_issue_overspend_note(view: BudgetView) -> str | None:
    """Completion note when recorded issue spend has passed the per-issue cap.

    Returns ``None`` when spend is unknown or still within the cap.
    """
    if view.issue_spent_usd is None or view.issue_spent_usd <= view.per_issue_cap_usd:
        return None
    # Render by the durable item id (``#5`` for GitHub, ``SFD-12`` bare for a
    # non-numeric tracker) so a Linear item never surfaces as ``Issue #0``.
    ref = item_ref(view.item_id) if view.item_id is not None else f"#{view.issue_num}"
    return (
        f"Item {ref} spend ${view.issue_spent_usd:.2f} is over the "
        f"per-issue cap {_money(view.per_issue_cap_usd)} — the next dispatch for "
        f"this item will be blocked at the budget preflight."
    )


def _dimension_spend(
    records: list[RunRecord], reservation: float
) -> tuple[float, float, int]:
    """Fold a window of runs into ``(recorded, worst_case, pending_count)``.

    ``recorded`` sums each run's stored estimate — a still-``pending`` run's is
    ~$0 until its real cost lands. ``worst_case`` instead values every in-flight
    pending run at *reservation* (the most it could still cost). ``pending_count``
    is how many in-flight runs the window holds.
    """
    recorded = 0.0
    worst_case = 0.0
    pending = 0
    for r in records:
        recorded += r.estimated_cost_usd
        if r.status == "pending":
            pending += 1
            worst_case += max(r.estimated_cost_usd, reservation)
        else:
            worst_case += r.estimated_cost_usd
    return recorded, worst_case, pending


def _spend_windows(
    store: RunStore, now: datetime
) -> tuple[list[RunRecord], list[RunRecord]]:
    """Every run in today's window and in the last seven days, issue-wide."""
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = day_start - timedelta(days=6)
    recent_records = store.list_since(week_start)
    return [r for r in recent_records if r.started_at >= day_start], recent_records


def _over_budget(recorded: float, worst_case: float, pending: int, cap: float) -> bool:
    """Whether a budget dimension is over *cap*.

    Recorded overspend always trips. A worst-case *reservation* overshoot trips
    only under genuine concurrency (>= 2 in-flight runs): a lone in-flight run
    is allowed to overshoot its cap — the runtime kill cap bounds a single run,
    and that single-run overshoot is the warned-but-allowed CS-7 posture, not a
    parallelism bug. Blocking it here would refuse every dispatch whenever the
    kill cap exceeds a cap's headroom (the default $25 cap vs $15 per-issue).
    """
    return recorded > cap or (pending >= 2 and worst_case > cap)


def check_budget(
    store: RunStore,
    budget: BudgetConfig,
    issue_num: int,
    *,
    item_id: str | None = None,
    now: datetime | None = None,
    pending_reservation_usd: float = 0.0,
) -> BudgetStatus:
    """Check per-issue, daily, and weekly spend against budget limits.

    A ``pending`` run carries a $0 estimate until its real cost lands, so N
    parallel in-flight workers reading only committed spend could each pass and
    jointly overshoot a nearly-exhausted cap (H2). Passing
    *pending_reservation_usd* (the runtime kill cap) values every pending row at
    its worst case. Called inside ``try_admit_run``'s ``BEGIN IMMEDIATE``
    transaction — after the admitting run's own pending row is inserted — this
    makes the spend check atomic with the slot claim: contending admits
    serialize, each sees the others' reservations, and once two are in flight
    the one that would breach the cap is refused (see :func:`_over_budget` for
    why a *single* in-flight overshoot is still allowed).

    Left at the ``0.0`` default (the cheap preflight) pending rows keep their
    stored $0 estimate, preserving the historical read-only snapshot behavior.

    ``item_id`` keys the *per-issue* dimension on the durable work-item id (A3):
    a non-GitHub item rides ``issue_num=0`` but keeps its own per-item cap
    instead of the shared ``0`` sentinel bucket (#809). Daily/weekly stay
    issue-wide. Left ``None`` the per-issue dimension falls back to issue keying.

    Returns BudgetStatus when within budget.
    Raises BudgetExceeded when any limit is exceeded.
    """
    if now is None:
        now = datetime.now(UTC)

    reservation = max(0.0, pending_reservation_usd)

    # Daily and weekly spend: query only the relevant window (last 7 days)
    daily_records, recent_records = _spend_windows(store, now)

    # Per-item dimension keys on the durable ``item_id`` (A3): a Linear item
    # rides ``issue_num=0`` but carries its real id, so ``list_by_item`` keeps
    # each non-GitHub item on its own cap instead of the ``0`` sentinel's shared
    # bucket. ``list_by_item`` returns the same full rows as ``list_by_issue``
    # (pending rows included), so worst-case pending valuation is preserved. For
    # GitHub ``item_id == str(issue_num)`` — byte-identical to the old scan.
    # Left ``None`` (unit tests / legacy callers) it falls back to issue keying.
    raw_per_item = (
        store.list_by_item(item_id)
        if item_id is not None
        else store.list_by_issue(issue_num)
    )
    # PR-subject work never consumes the implementation pool.
    per_item_runs = [r for r in raw_per_item if r.run_kind not in PR_SUBJECT_RUN_KINDS]
    issue_rec, issue_worst, issue_pending = _dimension_spend(per_item_runs, reservation)
    daily_rec, daily_worst, daily_pending = _dimension_spend(daily_records, reservation)
    weekly_rec, weekly_worst, weekly_pending = _dimension_spend(
        recent_records, reservation
    )

    status = BudgetStatus(
        issue_spent_usd=issue_worst,
        issue_limit_usd=budget.per_issue_usd,
        daily_spent_usd=daily_worst,
        daily_limit_usd=budget.daily_usd,
        weekly_spent_usd=weekly_worst,
        weekly_limit_usd=budget.weekly_usd,
    )

    # Check limits in priority order
    if _over_budget(issue_rec, issue_worst, issue_pending, budget.per_issue_usd):
        raise BudgetExceeded("per_issue", budget.per_issue_usd, issue_worst, status)
    if _over_budget(daily_rec, daily_worst, daily_pending, budget.daily_usd):
        raise BudgetExceeded("daily", budget.daily_usd, daily_worst, status)
    if _over_budget(weekly_rec, weekly_worst, weekly_pending, budget.weekly_usd):
        raise BudgetExceeded("weekly", budget.weekly_usd, weekly_worst, status)

    return status


def check_window_budget(
    store: RunStore,
    budget: BudgetConfig,
    *,
    reservation_usd: float = 0.0,
    now: datetime | None = None,
    count_admitting_run: bool = False,
    admitting_run_id: str | None = None,
) -> BudgetStatus:
    """The daily and weekly cap check every agent launch passes (#3205).

    Sums each window's recorded spend and values every in-flight ``pending``
    row at *reservation_usd*, the launch's own per-run ceiling, as the dispatch
    gate does. Run inside the transaction that admits the row, the admitting
    run's own pending row is already in both windows; *admitting_run_id* names
    that row, so a refusal's ``reserved_usd`` leaves its ceiling out.
    *count_admitting_run* adds that one row at the reservation for a caller
    that asks before the row exists, so its answer matches the admit gate's.

    A lone in-flight run may still overshoot a cap by its ceiling: see
    :func:`_over_budget`. Raises ``BudgetExceeded("daily" | "weekly", ...)``.
    """
    if now is None:
        now = datetime.now(UTC)
    reservation = max(0.0, reservation_usd)
    extra = 1 if count_admitting_run else 0
    # A detached review stage's own row is bookkeeping, never an agent: it
    # pays nothing, and the reviewer or remediation it starts is counted here
    # under its own row.
    daily_records, weekly_records = (
        [r for r in records if r.run_kind != REVIEW_STAGE_RUN_KIND]
        for records in _spend_windows(store, now)
    )
    daily_rec, daily_worst, daily_pending = _dimension_spend(daily_records, reservation)
    weekly_rec, weekly_worst, weekly_pending = _dimension_spend(
        weekly_records, reservation
    )
    daily_worst += extra * reservation
    weekly_worst += extra * reservation
    status = BudgetStatus(
        issue_spent_usd=0.0,
        issue_limit_usd=budget.per_issue_usd,
        daily_spent_usd=daily_worst,
        daily_limit_usd=budget.daily_usd,
        weekly_spent_usd=weekly_worst,
        weekly_limit_usd=budget.weekly_usd,
    )
    windows = (
        (
            "daily",
            budget.daily_usd,
            daily_records,
            daily_rec,
            daily_worst,
            daily_pending,
        ),
        (
            "weekly",
            budget.weekly_usd,
            weekly_records,
            weekly_rec,
            weekly_worst,
            weekly_pending,
        ),
    )
    for which, cap, records, rec, worst, pending in windows:
        if _over_budget(rec, worst, pending + extra, cap):
            raise BudgetExceeded(
                which,
                cap,
                worst,
                status,
                recorded_usd=rec,
                reserved_usd=_reserved_by_others(
                    records, reservation, admitting_run_id
                ),
            )
    return status


def _reserved_by_others(
    records: list[RunRecord], reservation: float, admitting_run_id: str | None
) -> float:
    """What in-flight runs other than *admitting_run_id* hold past their
    recorded spend, each valued at *reservation* as :func:`_dimension_spend`
    values it."""
    return sum(
        max(r.estimated_cost_usd, reservation) - r.estimated_cost_usd
        for r in records
        if r.status == "pending" and r.run_id != admitting_run_id
    )


def check_pr_subject_budget(
    store: RunStore,
    budget: BudgetConfig,
    *,
    now: datetime | None = None,
    pending_reservation_usd: float = 0.0,
    count_admitting_run: bool = False,
) -> BudgetStatus:
    """Budget gate for a ci-fix: PR-subject work with no pool of its own.

    It binds the global daily and weekly caps (:func:`check_window_budget`);
    the runtime kill cap bounds the run itself. It never binds the per-issue
    implementation cap. PR-subject spend stays out of that pool (#1932), so an
    issue's implementation spend, settled or in flight, never refuses the fix
    of its PR. ci-fix is not a review kind, so no review pool applies either.
    """
    return check_window_budget(
        store,
        budget,
        reservation_usd=pending_reservation_usd,
        now=now,
        count_admitting_run=count_admitting_run,
    )


def run_ceiling_usd(config: DispatchConfig, run_kind: str) -> float:
    """The most one run of *run_kind* may cost: its window reservation.

    A reviewer's is ``[review].max_review_usd``, a remediation's
    ``[review].max_remediation_usd``, and every other run's the runtime kill cap
    ``agent.max_budget_usd`` (#3205).
    """
    if run_kind == ADVERSARIAL_REVIEW_RUN_KIND:
        return config.review.max_review_usd
    if run_kind == REVIEW_REMEDIATION_RUN_KIND:
        return config.review.max_remediation_usd
    return config.agent.max_budget_usd


def window_admission_gate(
    store: RunStore, budget: BudgetConfig, ceiling_usd: float, *, run_id: str
) -> Callable[[], None]:
    """The window check as an admission callback (``on_admit``).

    Runs inside the ``BEGIN IMMEDIATE`` transaction that inserts the pending
    row of *run_id*, so contending launches serialize and each sees the others'
    reservations. Raises :class:`BudgetExceeded`, which rolls the row back.
    """

    def _gate() -> None:
        check_window_budget(
            store, budget, reservation_usd=ceiling_usd, admitting_run_id=run_id
        )

    return _gate


#: The review-outcome classification of a governed remediation a daily or
#: weekly cap refused before launch (#3205). Its detail is the hold reason.
WINDOW_BUDGET_REFUSAL = "budget_window"

#: The ``BudgetExceeded.which_limit`` values :func:`check_window_budget` raises.
WINDOW_LIMITS = frozenset({"daily", "weekly"})


def window_budget_hold_reason(exc: BudgetExceeded) -> str:
    """``budget: <daily|weekly> cap $<cap> reached ($<spend> spent)``.

    The spend named is the window's recorded spend, the figure the Budget page
    shows. When other in-flight runs hold reservations on top of it, the reason
    names them apart: ``($5.00 spent, $10.00 reserved by running or starting
    runs)``. The refused launch's own ceiling is in neither number.
    """
    if exc.recorded_usd is None:
        spend = f"${exc.spent_usd:.2f} spent"
    else:
        spend = f"${exc.recorded_usd:.2f} spent"
        if exc.reserved_usd > 0:
            spend += f", ${exc.reserved_usd:.2f} reserved by running or starting runs"
    return (
        f"{WINDOW_BUDGET_HOLD_PREFIX} {exc.which_limit} cap ${exc.limit_usd:.2f} "
        f"reached ({spend})"
    )


def window_budget_hold_limit(reason: str | None) -> str | None:
    """The window, ``daily`` or ``weekly``, a cap hold's *reason* names."""
    if not is_window_budget_hold(reason):
        return None
    words = str(reason)[len(WINDOW_BUDGET_HOLD_PREFIX) :].split()
    return words[0] if words and words[0] in WINDOW_LIMITS else None


def is_window_budget_hold(reason: str | None) -> bool:
    """Whether *reason* is a daily or weekly cap hold."""
    return bool(reason) and str(reason).startswith(WINDOW_BUDGET_HOLD_PREFIX)


def check_review_budget(
    store: RunStore,
    item_id: str,
    max_review_usd: float,
    *,
    since: datetime | None = None,
    now: datetime | None = None,
    pending_reservation_usd: float = 0.0,
) -> BudgetStatus:
    """Review-pool preflight — a SEPARATE attested pool from the implementation
    cap (F5). Sums only review-kind spend (``adversarial_review`` /
    ``review_remediation``) for *item_id*, so review runs never draw down the
    per-issue implementation budget and a near-cap implementation never makes
    its own reviewer refuse (bidirectional-starvation fix).

    *since* would scope the sum to the node's current review cycle (F15), but no
    dispatch path passes one: both review gates leave it ``None`` and so sum the
    item's whole review history — a retry resets the round counter, not the pool.
    Raises ``BudgetExceeded("review", ...)`` when the next
    reservation would breach *max_review_usd*, mirroring :func:`check_budget`'s
    worst-case pending valuation so a live reviewer's $0 pending row is counted
    at *pending_reservation_usd*.
    """
    if now is None:
        now = datetime.now(UTC)
    reservation = max(0.0, pending_reservation_usd)
    review_runs = [
        r
        for r in store.list_by_item(item_id)
        if r.run_kind in REVIEW_RUN_KINDS and (since is None or r.started_at >= since)
    ]
    recorded, worst_case, pending = _dimension_spend(review_runs, reservation)
    status = BudgetStatus(
        issue_spent_usd=worst_case,
        issue_limit_usd=max_review_usd,
        daily_spent_usd=worst_case,
        daily_limit_usd=max_review_usd,
        weekly_spent_usd=worst_case,
        weekly_limit_usd=max_review_usd,
    )
    if _over_budget(recorded, worst_case, pending, max_review_usd):
        raise BudgetExceeded("review", max_review_usd, worst_case, status)
    return status


# Epic-hold reason prefix a review-pool refusal writes (N3/#977). The overlay
# routes it to HELD; the driver's reconcile pass clears it as soon as the named
# cap admits the node again, so raising that cap is the whole fix.
REVIEW_BUDGET_HOLD_PREFIX = "review_budget"

# The two ``[review]`` caps that can write a ``review_budget`` hold: the
# adversarial-reviewer pool and the remediation pool (#993). The key rides IN the
# hold reason because the pools sum identical rows and differ only in their
# limit — re-checking a reviewer hold against the (usually larger) remediation
# cap would resume a node the reviewer pool still refuses.
REVIEW_POOL_KEY = "max_review_usd"
REMEDIATION_POOL_KEY = "max_remediation_usd"

_REVIEW_POOL_LABELS = {REVIEW_POOL_KEY: "review", REMEDIATION_POOL_KEY: "remediation"}


def review_budget_hold_reason(
    *, limit_usd: float, spent_usd: float, config_key: str
) -> str:
    """The durable hold reason a review-pool refusal writes (#2153).

    Names both numbers an operator needs — what the pool allows and what the
    node already spent — and the exact key to raise. ``config_key`` is also the
    machine-readable half: :func:`review_budget_hold_cap` reads it back so the
    reconciler re-checks the hold against the pool that wrote it.
    """
    return (
        f"{REVIEW_BUDGET_HOLD_PREFIX}: {_REVIEW_POOL_LABELS[config_key]} pool "
        f"${limit_usd:.2f} exhausted (spent ${spent_usd:.2f}) — "
        f"raise [review].{config_key}"
    )


def review_budget_hold_cap(reason: str, review: ReviewConfig) -> float | None:
    """The cap *reason* was written against, or None when it is not a review-pool
    hold (or names a key this version no longer knows — such a hold stays put and
    waits for ``kew epic unhold`` rather than being cleared against a guess)."""
    if not reason.startswith(f"{REVIEW_BUDGET_HOLD_PREFIX}:"):
        return None
    for key in (REMEDIATION_POOL_KEY, REVIEW_POOL_KEY):
        if f"[review].{key}" in reason:
            return float(getattr(review, key))
    return None


def delegation_budget_preflight(
    store: RunStore,
    issue_num: int,
    *,
    config_path: Path | None = None,
    item_id: str | None = None,
    now: datetime | None = None,
    pending_reservation_usd: float = 0.0,
) -> BudgetStatus:
    """Budget preflight for an assign-to-kew delegation dispatch (D1/#920).

    The parity seam every other delegation node builds on: it resolves the
    *effective* config through the one shared resolver — ``load_config(store=
    store)`` (precedence env > DB override > kew.toml > default) — so a DB
    weekly override governs a delegation run exactly as it governs an epic node
    or the detached ``_dispatch-node`` child (O1/#892). Reading config file-only
    here would silently drop every override, which is the live incident this
    seam exists to prevent.

    Delegation spend buckets under :data:`DELEGATION_RUN_KIND` at dispatch, so
    the shared budget guard is applied to the same effective caps as any other
    dispatch kind.
    """
    config = load_config(config_path=config_path, store=store)
    return check_budget(
        store,
        config.budget,
        issue_num,
        item_id=item_id,
        now=now,
        pending_reservation_usd=pending_reservation_usd,
    )
