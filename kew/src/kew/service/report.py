"""Model-scorecard read web/API surface.

Store injected (a RunStore-shaped object); never imports
run_store — uses pure kew.scorecard aggregation, import-linter
"serve reads runs only through service" contract holds.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from kew.config import load_config
from kew.model_eol_snapshot import LifecycleEntry
from kew.pricing import is_free_model
from kew.scorecard import _COMPLEXITY_ORDER, compute_model_scorecard
from kew.service.models import (
    ComplexityCellDTO,
    ModelScorecardDTO,
    ModelScorecardRowDTO,
    SpendCellDTO,
    SpendWindowsDTO,
)


def model_scorecard(
    store: Any,
    *,
    days: int,
    now: datetime | None = None,
    lifecycle_map: dict[str, LifecycleEntry] | None = None,
    since: datetime | None = None,
) -> ModelScorecardDTO:
    """Per-model scorecard over runs started since *since*.

    *since* defaults to *now* minus *days*. A caller with its own window start
    (the evidence pack) passes it, so the scorecard reads the same runs as the
    caller's headline instead of a window rounded to whole days; *days* then
    only labels the window.
    """
    if now is None:
        now = datetime.now(UTC)
    if since is None:
        since = now - timedelta(days=days)
    records = store.list_since(since)
    card = compute_model_scorecard(records)
    lifecycle_map = lifecycle_map or {}
    rows = []
    for r in card.rows:
        cells = [
            ComplexityCellDTO(
                complexity=c.complexity,
                runs=c.runs,
                opened=c.opened,
                merged=c.merged,
                review_total=c.review_total,
                review_n=c.review_n,
                spend=c.spend,
                planned_spend=c.planned_spend,
                paired_spend=c.paired_spend,
                planned_n=c.planned_n,
            )
            for c in sorted(
                r.by_complexity.values(),
                key=lambda c: _COMPLEXITY_ORDER.get(c.complexity, 99),
            )
        ]
        entry = lifecycle_map.get(r.model)
        rows.append(
            ModelScorecardRowDTO(
                model=r.model,
                channel="local" if is_free_model(r.model) else "hosted",
                lifecycle_status=entry.status if entry else "",
                runs=r.runs,
                opened=r.opened,
                merged=r.merged,
                review_total=r.review_total,
                review_n=r.review_n,
                spend=r.spend,
                planned_spend=r.planned_spend,
                paired_spend=r.paired_spend,
                planned_n=r.planned_n,
                by_complexity=cells,
            )
        )
    return ModelScorecardDTO(days=days, rows=rows)


def governed_spend_windows(
    store: Any, config_path: Path | None, *, now: datetime | None = None
) -> SpendWindowsDTO:
    """Spend against the caps the budget governor enforces.

    ``/api/report/spend`` and the dashboard's ``glance.caps`` serve the same two
    windows, and built them from two configs: the report re-read
    ``load_config(store=...)`` while the dashboard took the config ``kew serve``
    started with, which never sees a Budget-panel override set since (#2847 Q2).
    Both now go through here, and here resolves the caps the way the governor
    does (``kew.budget``): ``load_config(config_path=..., store=store)`` — DB
    override > kew.toml > default, the file read again on each call. A config
    held from startup would freeze the caps there: an edit to kew.toml the
    governor enforces would never reach the gauges, and neither would the file
    value an override revert brings back.

    *config_path* is the file the server resolved (``resolved_config_path()``),
    never a guess from wherever this is called.

    A cap of 0 is "no cap" (``None``), as the report has always served it.
    Duck-typed like the rest of this module: a store with no override table
    to read (``list_config_overrides``) is read as one with no override.
    """
    overrides = callable(getattr(store, "list_config_overrides", None))
    budget = load_config(
        config_path=config_path, store=store if overrides else None
    ).budget
    return spend_windows(
        store,
        daily_cap=budget.daily_usd or None,
        weekly_cap=budget.weekly_usd or None,
        now=now,
    )


def spend_windows(
    store: Any,
    *,
    daily_cap: float | None,
    weekly_cap: float | None,
    now: datetime | None = None,
) -> SpendWindowsDTO:
    """Spend vs cap over the daily and weekly windows, split by governance channel.

    The ``agentic`` figure is the ENFORCED number: it uses the same windows and
    composition as :func:`kew.budget.check_budget` — today's UTC day for daily
    and that day with the six before it (``day_start - 6d``) for weekly, over
    the cost of every run kew records, reviews included. This is deliberately
    identical to the governor so the web bar can compare ``agentic`` against
    ``cap`` and never
    contradict enforcement (N5). ``interactive`` (Claude Code OTel
    ``interactive_usage``) is observed spend over the same window that the
    governor does NOT gate; ``spent == agentic + interactive`` is kept for the
    total, but only ``agentic`` is the enforced quantity.

    Duck-typed on ``store.list_since(since)`` and
    ``store.list_interactive_usage_since(since)`` only; never imports run_store —
    the import-linter "serve reads runs only through service" contract holds.
    """
    if now is None:
        now = datetime.now(UTC)

    # Enforcement window boundaries, byte-identical to budget.check_budget:
    # daily is the current calendar day; weekly is that day plus the prior six.
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = day_start - timedelta(days=6)

    def _cell(window_days: int, since: datetime, cap: float | None) -> SpendCellDTO:
        agentic = float(sum(r.estimated_cost_usd for r in store.list_since(since)))
        interactive = float(
            sum(u.cost_usd for u in store.list_interactive_usage_since(since))
        )
        return SpendCellDTO(
            window_days=window_days,
            spent=agentic + interactive,
            cap=cap,
            agentic=agentic,
            interactive=interactive,
        )

    return SpendWindowsDTO(
        daily=_cell(1, day_start, daily_cap),
        weekly=_cell(7, week_start, weekly_cap),
    )
