"""Evidence-pack export for `kew report export` (V1).

Renders a self-contained artifact — one HTML file (inline CSS/JS, no CDN,
prints cleanly) plus a machine-readable JSON twin — proving what ran, what it
cost, and what merged over a window. v1 sections: economics summary (total
spend, cost per merged PR, spend-vs-caps rolling windows), the outcome ledger
(issue -> run(s) -> PR -> merge state, per epic), and the run-kind breakdown
(dispatch/review/loop/planning).

The numbers reuse ``kew.report.compute_stats``; the rolling spend windows read
through the service layer (``kew.service.report.spend_windows``) rather than
reaching into run_store directly (import contract #508). This module is the
report engine's export renderer, not part of ``kew.serve``.
"""

from __future__ import annotations

import base64
import html
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kew import __version__
from kew.audit_chain import (
    Anchor,
    ChainRow,
    classify_anchors,
    verify_anchor_chain,
    verify_chain,
)
from kew.config import DispatchConfig
from kew.ids import item_id_sort_key, item_ref_or_num
from kew.outcomes import SyncResult, sync_outcomes
from kew.policy import repo_root
from kew.pr_states import pr_states_by_url
from kew.report import (
    _distinct_pr_urls,
    _open_report_store,
    _record_item_key,
    compute_stats,
    open_approval_holds,
    outcome_sync_warning,
)
from kew.report_style import evidence_pack_css
from kew.run_store import RunRecord
from kew.scorecard import (
    _merge_rate_cell,
    _per_merged_cell,
    _planning_ratio_cell,
    _reviews_cell,
)
from kew.service.audit import load_anchors
from kew.service.models import ModelScorecardDTO, SandboxPostureDTO
from kew.service.report import model_scorecard, spend_windows
from kew.service.runs import sandbox_posture_summary, select_run_postures
from kew.tui.palette import LIGHT

_UNASSIGNED = "(unassigned)"
# Preferred run-kind ordering; unknown kinds sort after these, alphabetically.
_RUN_KIND_ORDER = ["dispatch", "review", "loop", "planning", "delegation"]


@dataclass
class LedgerRow:
    """One work item's outcome line inside the ledger (issue -> runs -> PR)."""

    item_ref: str
    issue_num: int
    item_id: str | None
    title: str
    run_count: int
    cost_usd: float
    status: str
    pr_url: str | None
    pr_state: str | None
    postures: list[tuple[str, SandboxPostureDTO]]


@dataclass
class EpicLedger:
    """The outcome ledger for one epic (or the unassigned bucket)."""

    epic_ref: str
    rows: list[LedgerRow]

    @property
    def total_cost(self) -> float:
        return sum(r.cost_usd for r in self.rows)

    @property
    def run_count(self) -> int:
        return sum(r.run_count for r in self.rows)


@dataclass
class RunKindStat:
    """Per run-kind aggregation over completed (non-pending) window runs."""

    run_kind: str
    runs: int
    success: int
    cost_usd: float

    @property
    def success_rate(self) -> float:
        return self.success / self.runs if self.runs else 0.0


@dataclass
class SpendWindow:
    """Rolling spend-vs-cap cell for the economics summary."""

    window_days: int
    spent: float
    cap: float | None

    @property
    def pct(self) -> float | None:
        return (self.spent / self.cap * 100) if self.cap else None


@dataclass
class Attestation:
    """In-process chain-verification verdict embedded in the pack (V2).

    Runs the ``kew audit verify --anchors`` machinery (``verify_chain`` plus
    ``classify_anchors``) directly against the run store, so a pack reader gets
    the event count, the verified id range, the chain-head hash to re-verify
    against, and any anchor gaps — without shelling out or re-anchoring.
    """

    events: int
    chain_ok: bool
    broken_at_id: int | None
    head_hash: str | None
    first_id: int | None
    last_id: int | None
    anchors_present: bool
    anchors_ok: bool
    anchors_total: int
    anchors_matched: int
    anchors_mismatched: list[int]
    anchors_absent: list[int]
    anchors_lagging: list[int]
    # Count-shrink relative to the latest anchor / stale-lagging anchor (#912).
    anchors_count_shrink: bool = False
    # Anchor-chain continuity: each anchor binds the previous anchor's hash, so a
    # wholesale re-chain must rewrite the git-tracked anchor history too (#912).
    anchor_chain_ok: bool = True
    anchor_chain_broken_at: int | None = None
    # How many anchor-to-anchor links were actually checked: 0 over a legacy
    # (prev-less) anchor file means the re-chain defence ran vacuously (#1122).
    anchor_chain_checked: int = 0

    @property
    def verified(self) -> bool:
        """True when the chain is intact and no anchor signals tamper.

        Anchor tamper covers hash mismatch/absence, count-shrink relative to the
        latest anchor, and a broken anchor-chain (re-chained history) — all rolled
        into ``anchors_ok``/``anchor_chain_ok``.
        """
        return self.chain_ok and (
            not self.anchors_present or (self.anchors_ok and self.anchor_chain_ok)
        )


def _empty_attestation() -> Attestation:
    """Attestation for a store with no chain (missing db / empty log)."""
    return Attestation(
        events=0,
        chain_ok=True,
        broken_at_id=None,
        head_hash=None,
        first_id=None,
        last_id=None,
        anchors_present=False,
        anchors_ok=True,
        anchors_total=0,
        anchors_matched=0,
        anchors_mismatched=[],
        anchors_absent=[],
        anchors_lagging=[],
    )


def build_attestation(
    rows: list[ChainRow],
    anchors: list[Anchor] | None,
    *,
    staleness_window: int = 0,
) -> Attestation:
    """Verify the chain (and anchors, if present) with the CLI's own primitives.

    Mirrors ``kew audit verify --anchors``: ``verify_chain`` walks the chain for
    the head hash and any break; ``classify_anchors`` separates tamper
    (mismatched/absent/count-shrink) from tolerated lag using ``staleness_window``;
    ``verify_anchor_chain`` checks anchor-to-anchor continuity (re-chain defence).
    ``anchors=None`` means no anchor file was found — verify-on-demand only.
    """
    if not rows:
        return _empty_attestation()
    result = verify_chain(rows)
    att = Attestation(
        events=result.count,
        chain_ok=result.ok,
        broken_at_id=result.broken_at_id,
        head_hash=result.head_hash,
        first_id=rows[0].id,
        last_id=rows[-1].id,
        anchors_present=anchors is not None,
        anchors_ok=True,
        anchors_total=0,
        anchors_matched=0,
        anchors_mismatched=[],
        anchors_absent=[],
        anchors_lagging=[],
    )
    if anchors is None:
        return att
    report = classify_anchors(rows, anchors, staleness_window=staleness_window)
    anchor_chain = verify_anchor_chain(anchors)
    att.anchors_ok = report.ok
    att.anchors_total = len(anchors)
    att.anchors_matched = report.matched
    att.anchors_mismatched = report.mismatched
    att.anchors_absent = report.absent
    att.anchors_lagging = report.lagging
    att.anchors_count_shrink = report.count_shrink
    att.anchor_chain_ok = anchor_chain.ok
    att.anchor_chain_broken_at = anchor_chain.broken_at_index
    att.anchor_chain_checked = anchor_chain.checked
    return att


@dataclass
class Brand:
    """Resolved kew branding for the pack header, footer, and JSON twin.

    The logo is already inlined as a data URI so the renderers stay pure and
    the HTML pack makes no external fetches. ``version`` is kew's own version:
    the "Generated by kew vX.Y.Z" attribution line always credits kew.
    """

    name: str
    accent: str
    logo_data_uri: str | None
    version: str

    @property
    def generator(self) -> str:
        return f"kew v{self.version}"


def _default_logo_path() -> Path:
    """The shipped serve brand mark (``frontend/static/favicon.svg``, repo root)."""
    return Path(__file__).resolve().parents[3] / "frontend" / "static" / "favicon.svg"


def _default_logo_data_uri() -> str | None:
    """Inline the shipped kew SVG, or return ``None`` when it is unreadable."""
    try:
        raw = _default_logo_path().read_bytes()
    except OSError:
        return None
    return f"data:image/svg+xml;base64,{base64.b64encode(raw).decode('ascii')}"


def _build_brand() -> Brand:
    """Assemble the pack's fixed kew branding."""
    return Brand(
        name="kew",
        accent=LIGHT["accent"],
        logo_data_uri=_default_logo_data_uri(),
        version=__version__,
    )


@dataclass
class EvidencePack:
    """Everything the HTML/JSON renderers need, precomputed and pure."""

    repo: str
    generated_at: datetime
    since: datetime
    days: int
    total_spend: float
    prs_opened: int
    prs_merged: int | None
    cost_per_merged_pr: float | None
    windows: list[SpendWindow]
    run_kinds: list[RunKindStat]
    ledgers: list[EpicLedger]
    attestation: Attestation
    # Per-model × complexity outcome scorecard, straight from the service layer
    # (kew.service.report.model_scorecard — import contract #508); the pack does
    # not re-derive these numbers, only renders them (V3).
    scorecard: ModelScorecardDTO
    # Header/footer branding, resolved from [report] config (B3).
    brand: Brand
    # Runs whose model was not recorded at dispatch time — counted and costed
    # explicitly so they appear in the economics section rather than silently
    # contributing $0 to cold-cost figures (B4).
    unattributed_runs: int = 0
    unattributed_spend: float = 0.0
    sync: SyncResult = field(default_factory=SyncResult)


def _epic_membership(store: Any) -> dict[str, str]:
    """Map each epic node's durable id to its epic_ref, from latest snapshots.

    The node ``id`` in a snapshot is the durable ``item_id`` — the same key
    ``_record_item_key`` derives from a run — so ledger rows attach to the
    right epic. First epic seen wins for a shared node (deterministic by the
    sorted ref order ``list_epic_refs`` returns).
    """
    membership: dict[str, str] = {}
    for ref in store.list_epic_refs():
        for node_id in epic_snapshot_nodes(store, ref):
            membership.setdefault(node_id, ref)
    return membership


def epic_snapshot_nodes(store: Any, ref: str) -> list[str]:
    """The node ids in *ref*'s own latest driver snapshot, in snapshot order.

    Unlike :func:`_epic_membership`, a node another epic also holds is kept:
    this answers "which nodes does this epic hold", not "which epic owns this
    node".
    """
    tick = store.latest_epic_tick(ref)
    snapshot = tick.snapshot if tick else None
    if not isinstance(snapshot, dict):
        return []
    nodes = []
    for node in snapshot.get("nodes") or []:
        if isinstance(node, dict):
            node_id = str(node.get("id", ""))
            if node_id:
                nodes.append(node_id)
    return nodes


def _run_kind_sort_key(kind: str) -> tuple[int, str]:
    return (
        _RUN_KIND_ORDER.index(kind)
        if kind in _RUN_KIND_ORDER
        else len(_RUN_KIND_ORDER),
        kind,
    )


def _run_kind_breakdown(records: Sequence[RunRecord]) -> list[RunKindStat]:
    """Aggregate completed (non-pending) runs by their run-kind discriminator."""
    stats: dict[str, RunKindStat] = {}
    for r in records:
        if r.status == "pending":
            continue
        kind = r.run_kind or "dispatch"
        stat = stats.setdefault(
            kind, RunKindStat(run_kind=kind, runs=0, success=0, cost_usd=0.0)
        )
        stat.runs += 1
        stat.success += int(r.status == "success")
        stat.cost_usd += r.estimated_cost_usd
    return sorted(stats.values(), key=lambda s: _run_kind_sort_key(s.run_kind))


def _ledger_rows(
    records: Sequence[RunRecord], postures: dict[str, SandboxPostureDTO]
) -> dict[str, LedgerRow]:
    """Accumulate per work-item ledger rows over window runs (started_at order).

    A row shows the state of the PR it names (its newest ``pr_url``), read by
    the same rule as the headline (:func:`kew.pr_states.pr_states_by_url`), so
    the per-epic table and the headline cannot disagree about one PR.
    """
    states = pr_states_by_url(records)
    rows: dict[str, LedgerRow] = {}
    for r in records:  # list_since() orders by started_at ascending
        key = _record_item_key(r)
        row = rows.get(key)
        if row is None:
            rows[key] = LedgerRow(
                item_ref=item_ref_or_num(r.item_id, r.issue_num),
                issue_num=r.issue_num,
                item_id=r.item_id,
                title=r.title,
                run_count=1,
                cost_usd=r.estimated_cost_usd,
                status=r.status,
                pr_url=r.pr_url,
                pr_state=r.pr_state,
                postures=[(r.run_id, postures.get(r.run_id, SandboxPostureDTO()))],
            )
            continue
        row.run_count += 1
        row.cost_usd += r.estimated_cost_usd
        row.title = r.title or row.title
        row.status = r.status  # latest run wins
        row.pr_url = r.pr_url or row.pr_url
        row.pr_state = r.pr_state or row.pr_state
        row.postures.append((r.run_id, postures.get(r.run_id, SandboxPostureDTO())))
    for row in rows.values():
        if row.pr_url:
            row.pr_state = states.get(row.pr_url)
    return rows


def _epic_ledgers(
    records: Sequence[RunRecord],
    membership: dict[str, str],
    postures: dict[str, SandboxPostureDTO],
) -> list[EpicLedger]:
    """Group ledger rows by epic; the unassigned bucket sorts last."""
    rows = _ledger_rows(records, postures)
    by_epic: dict[str, list[LedgerRow]] = {}
    for key, row in rows.items():
        epic_ref = membership.get(key, _UNASSIGNED)
        by_epic.setdefault(epic_ref, []).append(row)

    ledgers = []
    for epic_ref in sorted(by_epic, key=lambda e: (e == _UNASSIGNED, e)):
        epic_rows = sorted(
            by_epic[epic_ref],
            key=lambda r: (
                -r.cost_usd,
                item_id_sort_key(r.item_id or str(r.issue_num)),
            ),
        )
        ledgers.append(EpicLedger(epic_ref=epic_ref, rows=epic_rows))
    return ledgers


@dataclass
class _ExportWindow:
    """Everything read from the store for one export window."""

    records: list[RunRecord]
    approval_holds: list[Any]
    interactive: list[Any]
    windows_dto: Any
    membership: dict[str, str]
    chain_rows: list[ChainRow]
    scorecard: ModelScorecardDTO
    postures: dict[str, SandboxPostureDTO]
    sync: SyncResult


def _window_days(since: datetime, now: datetime) -> int:
    """Whole-day span of the export window (mirrors ``EvidencePack.days``)."""
    return max(1, (now - since).days)


def _export_sync_result(result: SyncResult | int) -> SyncResult:
    """Normalize a query-failure count before either export renders it."""
    if isinstance(result, int):
        return SyncResult(query_failures=result)
    return result


def _read_export_window(
    store: Any,
    config: DispatchConfig,
    *,
    since: datetime,
    now: datetime,
    daily_cap: float | None,
    weekly_cap: float | None,
) -> _ExportWindow:
    """Sync outcomes (best-effort) and read the window through the store.

    The full chain (``read_chain_rows``, not the window) is read here too so the
    attestation covers the whole audit log, matching ``kew audit verify``.
    """
    sync = _export_sync_result(sync_outcomes(store, config))
    page = store.list_since(since)
    selections = select_run_postures(store, (record.run_id for record in page))
    records = [record for _, record, _ in selections.values() if record is not None]
    return _ExportWindow(
        sync=sync,
        records=records,
        approval_holds=open_approval_holds(store),
        interactive=store.list_interactive_usage_since(since),
        windows_dto=spend_windows(
            store, daily_cap=daily_cap, weekly_cap=weekly_cap, now=now
        ),
        membership=_epic_membership(store),
        chain_rows=store.read_chain_rows(),
        # Reuse the service-layer scorecard (import contract #508) rather than
        # recomputing merge rates / spend from the records ourselves (V3). It
        # reads from the export's own ``since``, not ``now`` minus whole days,
        # so it covers the same runs as the headline.
        scorecard=model_scorecard(
            store, days=_window_days(since, now), now=now, since=since
        ),
        postures={run_id: selected[2] for run_id, selected in selections.items()},
    )


def _empty_pack(
    *,
    repo: str,
    now: datetime,
    since: datetime,
    daily_cap: float | None,
    weekly_cap: float | None,
    brand: Brand,
) -> EvidencePack:
    """Zero-activity pack for a missing dispatch.db (mirrors ``build_report``)."""
    return EvidencePack(
        repo=repo,
        generated_at=now,
        since=since,
        days=_window_days(since, now),
        total_spend=0.0,
        prs_opened=0,
        prs_merged=0,
        cost_per_merged_pr=None,
        windows=[SpendWindow(1, 0.0, daily_cap), SpendWindow(7, 0.0, weekly_cap)],
        run_kinds=[],
        ledgers=[],
        attestation=_empty_attestation(),
        scorecard=ModelScorecardDTO(days=_window_days(since, now), rows=[]),
        brand=brand,
    )


def build_evidence_pack(
    *,
    config: DispatchConfig,
    since: datetime,
    now: datetime | None = None,
    repo_dir: Path | None = None,
) -> EvidencePack:
    """Load window runs and assemble the evidence pack.

    Outcomes are synced from GitHub into the store before reading (best-effort),
    so merge state is current. A missing dispatch.db yields an empty pack rather
    than erroring (mirrors ``build_report``). The rolling spend-vs-cap windows
    read through ``kew.service.report.spend_windows`` (import contract #508).

    ``repo_dir`` locates the anchors file for the audit attestation (V2);
    defaults to the enclosing git repo root so it matches ``kew audit verify``.
    """
    if now is None:
        now = datetime.now(UTC)

    brand = _build_brand()
    daily_cap = config.budget.daily_usd or None
    weekly_cap = config.budget.weekly_usd or None
    db_path = Path(config.logging.dir).resolve() / "dispatch.db"
    if not db_path.exists():
        return _empty_pack(
            repo=config.project.repo or "(unknown repo)",
            now=now,
            since=since,
            daily_cap=daily_cap,
            weekly_cap=weekly_cap,
            brand=brand,
        )

    with _open_report_store(db_path) as store:
        window = _read_export_window(
            store,
            config,
            since=since,
            now=now,
            daily_cap=daily_cap,
            weekly_cap=weekly_cap,
        )

    anchors = load_anchors(repo_dir if repo_dir is not None else repo_root())
    return _assemble_pack(
        config, window, since=since, now=now, anchors=anchors, brand=brand
    )


def _assemble_pack(
    config: DispatchConfig,
    window: _ExportWindow,
    *,
    since: datetime,
    now: datetime,
    anchors: list[Anchor] | None,
    brand: Brand,
) -> EvidencePack:
    """Turn a loaded window into the finished pack (pure aggregation)."""
    from kew.pricing import build_pricing_overrides

    records = window.records
    # One rule for every reader (kew.pr_states): merged if any run says so.
    pr_states = pr_states_by_url(records)
    stats = compute_stats(
        records,
        pr_states,
        window.approval_holds,
        window.interactive,
        pricing_overrides=build_pricing_overrides(config.pricing.overrides),
    )
    repo = config.project.repo or _repo_from_records(records) or "(unknown repo)"
    daily, weekly = window.windows_dto.daily, window.windows_dto.weekly

    unattr = [r for r in records if not r.model and r.status != "pending"]
    return EvidencePack(
        repo=repo,
        generated_at=now,
        since=since,
        days=_window_days(since, now),
        total_spend=stats.total_spend,
        prs_opened=stats.prs_opened,
        prs_merged=stats.prs_merged,
        cost_per_merged_pr=stats.cost_per_merged_pr,
        windows=[
            SpendWindow(1, daily.spent, daily.cap),
            SpendWindow(7, weekly.spent, weekly.cap),
        ],
        run_kinds=_run_kind_breakdown(records),
        ledgers=_epic_ledgers(records, window.membership, window.postures),
        attestation=build_attestation(
            window.chain_rows,
            anchors,
            staleness_window=config.audit.staleness_window,
        ),
        scorecard=window.scorecard,
        brand=brand,
        unattributed_runs=len(unattr),
        unattributed_spend=sum(r.estimated_cost_usd for r in unattr),
        sync=window.sync,
    )


def _repo_from_records(records: Sequence[RunRecord]) -> str:
    """Best-effort owner/name from a persisted PR URL. No gh calls."""
    from kew.report import _repo_from_pr_urls

    return _repo_from_pr_urls(_distinct_pr_urls(records))


# --------------------------------------------------------------------------- #
# JSON twin
# --------------------------------------------------------------------------- #


def _scorecard_metrics(o: Any) -> dict[str, Any]:
    """Numeric projection shared by a model row and its complexity cells.

    Derived ratios (merge rate, avg reviews, $/merged PR) come straight from the
    service DTO's counts — the same arithmetic ``render_model_scorecard`` shows,
    just as numbers instead of formatted cells. ``None`` where the denominator is
    zero so consumers don't divide by it.
    """
    return {
        "runs": o.runs,
        "opened": o.opened,
        "merged": o.merged,
        "merge_rate": None if o.opened == 0 else round(o.merged / o.opened, 4),
        "reviews_avg": (
            None if o.review_n == 0 else round(o.review_total / o.review_n, 4)
        ),
        "spend": round(o.spend, 6),
        "cost_per_merged_pr": (None if o.merged == 0 else round(o.spend / o.merged, 6)),
        # Planned/actual cost pair (#1092). ``planning_ratio`` is actual ÷
        # planned bound over the runs carrying both; None when none did — a
        # missing bound is never read as $0 planned.
        "planned_spend": round(o.planned_spend, 6),
        "paired_spend": round(o.paired_spend, 6),
        "planned_runs": o.planned_n,
        "planning_ratio": (
            None
            if o.planned_n == 0 or o.planned_spend <= 0
            else round(o.paired_spend / o.planned_spend, 4)
        ),
    }


def _scorecard_to_dict(scorecard: ModelScorecardDTO) -> dict[str, Any]:
    """Machine-readable projection of the model scorecard section (V3)."""
    return {
        "days": scorecard.days,
        "rows": [
            {
                "model": row.model,
                **_scorecard_metrics(row),
                "by_complexity": [
                    {"complexity": cell.complexity, **_scorecard_metrics(cell)}
                    for cell in row.by_complexity
                ],
            }
            for row in scorecard.rows
        ],
    }


def pack_to_dict(pack: EvidencePack) -> dict[str, Any]:
    """Machine-readable projection of the pack (the JSON twin's payload)."""
    return {
        "schema": 1,
        "kind": "kew-evidence-pack",
        "outcome_sync": {
            "query_failures": pack.sync.query_failures,
            "stale": pack.sync.query_failures > 0,
        },
        "repo": pack.repo,
        "brand": {
            "name": pack.brand.name,
            "accent": pack.brand.accent,
            "logo_data_uri": pack.brand.logo_data_uri,
            "generator": pack.brand.generator,
            "kew_version": pack.brand.version,
        },
        "generated_at": pack.generated_at.isoformat(),
        "period": {
            "since": pack.since.isoformat(),
            "until": pack.generated_at.isoformat(),
            "days": pack.days,
        },
        "economics": {
            "total_spend": round(pack.total_spend, 6),
            "prs_opened": pack.prs_opened,
            "prs_merged": pack.prs_merged,
            "cost_per_merged_pr": (
                None
                if pack.cost_per_merged_pr is None
                else round(pack.cost_per_merged_pr, 6)
            ),
            "spend_windows": [
                {
                    "window_days": w.window_days,
                    "spent": round(w.spent, 6),
                    "cap": w.cap,
                    "pct": None if w.pct is None else round(w.pct, 2),
                }
                for w in pack.windows
            ],
            "unattributed_runs": pack.unattributed_runs,
            "unattributed_spend": round(pack.unattributed_spend, 6),
        },
        "run_kinds": [
            {
                "run_kind": s.run_kind,
                "runs": s.runs,
                "success": s.success,
                "success_rate": round(s.success_rate, 4),
                "cost_usd": round(s.cost_usd, 6),
            }
            for s in pack.run_kinds
        ],
        "epics": [
            {
                "epic_ref": ledger.epic_ref,
                "total_cost": round(ledger.total_cost, 6),
                "run_count": ledger.run_count,
                "items": [
                    {
                        "ref": row.item_ref,
                        "issue_num": row.issue_num,
                        "item_id": row.item_id,
                        "title": row.title,
                        "runs": row.run_count,
                        "cost_usd": round(row.cost_usd, 6),
                        "status": row.status,
                        "pr_url": row.pr_url,
                        "pr_state": row.pr_state,
                        "postures": [
                            {"run_id": run_id, **_posture_to_dict(posture)}
                            for run_id, posture in row.postures
                        ],
                    }
                    for row in ledger.rows
                ],
            }
            for ledger in pack.ledgers
        ],
        "attestation": _attestation_to_dict(pack.attestation),
        "model_scorecard": _scorecard_to_dict(pack.scorecard),
    }


def _posture_to_dict(posture: SandboxPostureDTO) -> dict[str, Any]:
    """Preserve a recorded basis without inventing one for historical runs."""
    data = posture.model_dump()
    if posture.egress_verification_basis is None:
        data.pop("egress_verification_basis")
    return data


def _attestation_to_dict(att: Attestation) -> dict[str, Any]:
    """Machine-readable projection of the audit attestation (V2)."""
    return {
        "verified": att.verified,
        "events": att.events,
        "chain_ok": att.chain_ok,
        "broken_at_id": att.broken_at_id,
        "head_hash": att.head_hash,
        "verified_range": {"first_id": att.first_id, "last_id": att.last_id},
        "anchors": {
            "present": att.anchors_present,
            "ok": att.anchors_ok,
            "total": att.anchors_total,
            "matched": att.anchors_matched,
            "mismatched": att.anchors_mismatched,
            "absent": att.anchors_absent,
            "lagging": att.anchors_lagging,
            "count_shrink": att.anchors_count_shrink,
            "chain_ok": att.anchor_chain_ok,
            "chain_broken_at": att.anchor_chain_broken_at,
            "chain_links_verified": att.anchor_chain_checked,
        },
    }


def render_evidence_pack_json(pack: EvidencePack) -> str:
    """Render the machine-readable JSON twin."""
    return json.dumps(pack_to_dict(pack), indent=2, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# Self-contained HTML
#
# The stylesheet lives in report_style: kew's light theme straight off
# frontend/src/app.css, IBM Plex inlined as base64, no hex literal outside the
# palette (#1126).
# --------------------------------------------------------------------------- #


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _usd(value: float | None) -> str:
    return "n/a" if value is None else f"${value:,.2f}"


def _pr_cell(row: LedgerRow) -> str:
    state = row.pr_state or "none"
    label = row.pr_state or "—"
    badge = f'<span class="badge {_esc(state)}">{_esc(label)}</span>'
    if row.pr_url:
        return f'<a href="{_esc(row.pr_url)}">{_esc(row.pr_url)}</a> {badge}'
    return badge


def _economics_html(pack: EvidencePack) -> list[str]:
    cards = [
        ("Total spend", _usd(pack.total_spend)),
        ("PRs opened", str(pack.prs_opened)),
        (
            "PRs merged",
            "n/a" if pack.prs_merged is None else str(pack.prs_merged),
        ),
        ("Cost / merged PR", _usd(pack.cost_per_merged_pr)),
    ]
    parts = ["<h2>Economics summary</h2>", '<div class="cards">']
    parts += [
        f'<div class="card"><div class="label">{_esc(label)}</div>'
        f'<div class="value">{_esc(value)}</div></div>'
        for label, value in cards
    ]
    parts.append("</div>")
    parts.append(
        "<table><thead><tr><th>Rolling window</th><th class='num'>Spend</th>"
        "<th class='num'>Cap</th><th class='num'>% of cap</th></tr></thead><tbody>"
    )
    for w in pack.windows:
        pct = "—" if w.pct is None else f"{w.pct:.1f}%"
        cap = "—" if w.cap is None else _usd(w.cap)
        parts.append(
            f"<tr><td>{w.window_days}d</td><td class='num'>{_usd(w.spent)}</td>"
            f"<td class='num'>{_esc(cap)}</td><td class='num'>{_esc(pct)}</td></tr>"
        )
    parts.append("</tbody></table>")
    if pack.unattributed_runs:
        parts.append(
            f"<p class='meta'>Unattributed: {pack.unattributed_runs} run(s) "
            f"with no recorded model · "
            f"{_usd(pack.unattributed_spend)} spend included in total.</p>"
        )
    return parts


def _run_kinds_html(pack: EvidencePack) -> list[str]:
    parts = ["<h2>Run-kind breakdown</h2>"]
    if not pack.run_kinds:
        parts.append('<p class="empty">No runs in window.</p>')
        return parts
    parts.append(
        "<table><thead><tr><th>Run kind</th><th class='num'>Runs</th>"
        "<th class='num'>Success rate</th><th class='num'>Cost</th></tr></thead><tbody>"
    )
    for s in pack.run_kinds:
        parts.append(
            f"<tr><td>{_esc(s.run_kind)}</td><td class='num'>{s.runs}</td>"
            f"<td class='num'>{s.success_rate * 100:.1f}%</td>"
            f"<td class='num'>{_usd(s.cost_usd)}</td></tr>"
        )
    parts.append("</tbody></table>")
    return parts


def _ledger_table_html(ledger: EpicLedger) -> list[str]:
    parts = [
        f"<h3>{_esc(ledger.epic_ref)} "
        f"<span class='meta'>· {ledger.run_count} run(s) · "
        f"{_usd(ledger.total_cost)}</span></h3>",
        "<table><thead><tr><th>Item</th><th>Title</th><th class='num'>Runs</th>"
        "<th class='num'>Cost</th><th>Status</th><th>PR</th>"
        "<th>Posture</th></tr></thead><tbody>",
    ]
    for row in ledger.rows:
        parts.append(
            f"<tr><td>{_esc(row.item_ref)}</td><td>{_esc(row.title)}</td>"
            f"<td class='num'>{row.run_count}</td>"
            f"<td class='num'>{_usd(row.cost_usd)}</td>"
            f"<td>{_esc(row.status)}</td><td>{_pr_cell(row)}</td>"
            f"<td>{_posture_cell(row)}</td></tr>"
        )
    parts.append("</tbody></table>")
    return parts


def _posture_cell(row: LedgerRow) -> str:
    return "<br>".join(
        f"<code>{_esc(run_id)}</code>: {_esc(sandbox_posture_summary(posture))}"
        for run_id, posture in row.postures
    )


def _scorecard_cells_html(o: Any) -> str:
    """Right-aligned metric cells for one scorecard row (model or complexity).

    Reuses ``kew.scorecard``'s cell formatters so the pack shows the exact same
    merge-rate / reviews / $-per-merged strings as the text scorecard.
    """
    return (
        f"<td class='num'>{o.runs}</td>"
        f"<td class='num'>{_esc(_merge_rate_cell(o.merged, o.opened))}</td>"
        f"<td class='num'>{_esc(_reviews_cell(o.review_total, o.review_n))}</td>"
        f"<td class='num'>{_usd(o.spend)}</td>"
        f"<td class='num'>{_esc(_per_merged_cell(o.spend, o.merged))}</td>"
        f"<td class='num'>{_esc(_planning_ratio_cell(o))}</td>"
    )


def _scorecard_html(pack: EvidencePack) -> list[str]:
    parts = ["<h2>Model scorecard</h2>"]
    if not pack.scorecard.rows:
        parts.append('<p class="empty">No model activity in window.</p>')
        return parts
    parts.append(
        "<table><thead><tr><th>Model</th><th class='num'>Runs</th>"
        "<th class='num'>Merge rate</th><th class='num'>Reviews</th>"
        "<th class='num'>Spend</th><th class='num'>$/merged PR</th>"
        "<th class='num'>Planned vs actual</th>"
        "</tr></thead><tbody>"
    )
    for row in pack.scorecard.rows:
        parts.append(f"<tr><td>{_esc(row.model)}</td>{_scorecard_cells_html(row)}</tr>")
        for cell in row.by_complexity:
            parts.append(
                f"<tr class='sub'><td>· {_esc(cell.complexity)}</td>"
                f"{_scorecard_cells_html(cell)}</tr>"
            )
    parts.append("</tbody></table>")
    parts.append(
        "<p class='meta'>PRs are credited to the model that authored them, not "
        "the one that reviewed them. Merge rate = merged ÷ opened PRs "
        "(* low sample, &lt;3). Reviews = avg PR review submissions per PR. "
        "$/merged PR = all of that model's run spend (failed, abandoned and "
        "review runs included) ÷ the PRs it authored that merged. "
        "Planned vs actual = recorded actual ÷ planned bound over the runs "
        "carrying both (below 1.00x = the bound over-records).</p>"
    )
    return parts


def _ledger_html(pack: EvidencePack) -> list[str]:
    parts = ["<h2>Outcome ledger</h2>"]
    if not pack.ledgers:
        parts.append('<p class="empty">No runs in window.</p>')
        return parts
    for ledger in pack.ledgers:
        parts += _ledger_table_html(ledger)
    return parts


def _attestation_anchor_summary(att: Attestation) -> str:
    """One-line anchor-coverage summary for the attestation card: matched count,
    gaps (mismatched + absent), anchors ahead of the pack window, count-shrink,
    and whether the anchor chain re-verified intact."""
    if not att.anchors_present:
        return "no anchor file (verify-on-demand)"
    anchors = f"{att.anchors_matched}/{att.anchors_total} matched"
    gaps = len(att.anchors_mismatched) + len(att.anchors_absent)
    if gaps:
        anchors += f" · {gaps} gap(s)"
    if att.anchors_lagging:
        anchors += f" · {len(att.anchors_lagging)} ahead"
    if att.anchors_count_shrink:
        anchors += " · COUNT-SHRINK"
    if att.anchor_chain_ok:
        anchors += f" · chain intact ({att.anchor_chain_checked} link(s) verified)"
    else:
        anchors += " · RE-CHAINED"
    return anchors


def _attestation_html(pack: EvidencePack) -> list[str]:
    att = pack.attestation
    parts = ["<h2>Audit attestation</h2>"]
    if att.events == 0:
        parts.append('<p class="empty">No audit events to attest.</p>')
        return parts
    verdict = "VERIFIED" if att.verified else "TAMPER"
    badge_cls = "MERGED" if att.verified else "CLOSED"
    rng = f"#{att.first_id} → #{att.last_id}" if att.first_id is not None else "—"
    anchors = _attestation_anchor_summary(att)
    cards = [
        ("Chain", f'<span class="badge {badge_cls}">{_esc(verdict)}</span>'),
        ("Events", str(att.events)),
        ("Verified range", _esc(rng)),
        ("Anchors", _esc(anchors)),
    ]
    parts.append('<div class="cards">')
    parts += [
        f'<div class="card"><div class="label">{_esc(label)}</div>'
        f'<div class="value">{value}</div></div>'
        for label, value in cards
    ]
    parts.append("</div>")
    if not att.chain_ok:
        parts.append(
            f'<p class="empty">Chain breaks at event #{_esc(att.broken_at_id)}.</p>'
        )
    parts.append(
        f'<p class="meta">Chain-head hash '
        f"<code>{_esc(att.head_hash)}</code> — re-verify with "
        f"<code>kew audit verify --anchors audit-anchors.jsonl</code>.</p>"
    )
    return parts


def _brand_header_html(pack: EvidencePack) -> list[str]:
    """Render the kew header with its inlined logo and product name."""
    brand = pack.brand
    logo = (
        f'<img class="brand-logo" alt="{_esc(brand.name)} logo" '
        f'src="{_esc(brand.logo_data_uri)}">'
        if brand.logo_data_uri
        else ""
    )
    return [
        '<header class="brand">',
        logo,
        f'<span class="brand-name">{_esc(brand.name)}</span>'
        '<span class="brand-tag">evidence pack</span>',
        "</header>",
    ]


def _generated_line(pack: EvidencePack) -> str:
    """The 'Generated by kew vX.Y.Z' attribution line (timestamp + period)."""
    period = f"{pack.since:%Y-%m-%d} → {pack.generated_at:%Y-%m-%d} ({pack.days} days)"
    return (
        f"Generated by {_esc(pack.brand.generator)} · "
        f"{_esc(f'{pack.generated_at:%Y-%m-%d %H:%M}')} UTC · "
        f"Period {_esc(period)}"
    )


def render_evidence_pack_html(pack: EvidencePack) -> str:
    """Render the self-contained HTML evidence pack (inline CSS/JS, no CDN)."""
    # Escape "<" so a title containing "</script>" cannot break out of the
    # embedded data island; the result is still valid JSON (<).
    data_json = json.dumps(pack_to_dict(pack), ensure_ascii=False).replace(
        "<", "\\u003c"
    )
    body: list[str] = [
        "<!DOCTYPE html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{_esc(pack.brand.name)} evidence pack — {_esc(pack.repo)}</title>",
        f"<style>{evidence_pack_css()}</style></head><body>",
    ]
    warning = outcome_sync_warning(pack.sync.query_failures)
    if warning:
        body.append(f'<h1 role="alert">{_esc(warning)}</h1>')
    body += _brand_header_html(pack)
    body += [
        f"<h1>Evidence pack — {_esc(pack.repo)}</h1>",
        f'<p class="meta">Period {_esc(f"{pack.since:%Y-%m-%d}")} → '
        f"{_esc(f'{pack.generated_at:%Y-%m-%d}')} ({pack.days} days) · "
        f"Generated {_esc(f'{pack.generated_at:%Y-%m-%d %H:%M}')} UTC</p>",
    ]
    body += _economics_html(pack)
    body += _run_kinds_html(pack)
    body += _scorecard_html(pack)
    body += _ledger_html(pack)
    body += _attestation_html(pack)
    body += [
        "<footer>Cost per merged PR = total window spend ÷ distinct merged PRs. "
        "Spend on failed and killed runs is included — the honest unit economics. "
        "Machine-readable data is embedded below."
        f"<br><span class='gen-line'>{_generated_line(pack)}</span></footer>",
        f'<script type="application/json" id="kew-evidence-data">{data_json}</script>',
        "</body></html>",
    ]
    return "\n".join(body)
