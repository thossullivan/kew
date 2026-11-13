import type { ScorecardRow } from './api';

/** Totals for the scorecard leverage headline. All figures are API-equivalent
 * list price across window runs (not merged-only, not billed spend). Local
 * spend is keyed on the backend-computed `channel` (kew.pricing.is_free_model)
 * rather than re-deriving a free-model heuristic from the model id (N11). */
export function deriveHeadline(rows: ScorecardRow[]): {
  totalSpend: number;
  totalMerged: number;
  localSpend: number;
} {
  return {
    totalSpend: rows.reduce((a, r) => a + r.spend, 0),
    totalMerged: rows.reduce((a, r) => a + r.merged, 0),
    localSpend: rows.reduce((a, r) => a + (r.channel === 'local' ? r.spend : 0), 0)
  };
}

/** True when at least one model opened a PR in the window. When false the
 * merge-rate columns are all "—", so the headline should say so plainly
 * rather than imply a real 0% merge rate. */
export function windowHasPRs(rows: ScorecardRow[]): boolean {
  return rows.some((r) => r.opened > 0);
}

/** Planning accuracy for one row: recorded actual ÷ planned bound, over the
 * runs carrying both halves of the pair (#1092). "1.00x" means the token x
 * pricing-bound estimate predicted spend exactly; below 1.00x means the bound
 * over-records. "—" when no run recorded a bound — a missing bound is never
 * read as $0 planned, which would render a meaningless infinite ratio. */
export function planningRatio(row: {
  planned_spend?: number;
  paired_spend?: number;
  planned_n?: number;
}): string {
  if (!row.planned_n || !row.planned_spend || row.planned_spend <= 0) return '—';
  return `${((row.paired_spend ?? 0) / row.planned_spend).toFixed(2)}x`;
}

// --- Row cells (FUI-28) -----------------------------------------------------
// The table's cells, as words. They live here rather than in the page because a
// model row and its by-complexity rows print the SAME figures: the page had two
// copies of each formula, and the expanded rows quietly drifted (their merge
// rate rounded, the parent's did too, but only one of them said "—" for a model
// that opened nothing). One formula, two callers.

/** The free/local execution channel, as the backend computes it. */
export const LOCAL_CHANNEL = 'local';

/**
 * True when the backend put this row on the free/local channel.
 *
 * Keyed on `channel` (kew.pricing.is_free_model), never on the model id: a
 * `lemonade/` prefix test in the UI is a second, disagreeing definition of
 * "local" (N11) — the same reason `deriveHeadline` reads the field.
 */
export function isLocalRow(row: { channel?: string }): boolean {
  return row.channel === LOCAL_CHANNEL;
}

/** Merge rate as the table says it: "86% (18/21)", or "—" when none opened. */
export function mergeRate(row: { opened: number; merged: number }): string {
  if (row.opened === 0) return '—';
  return `${Math.round((100 * row.merged) / row.opened)}% (${row.merged}/${row.opened})`;
}

/**
 * Spend per merged PR, or "—" when nothing merged. Never spend ÷ 0: a window
 * that merged nothing has no cost per merge, and `$Infinity` is not a reading.
 */
export function perMerged(row: { spend: number; merged: number }, fmt: (n: number) => string): string {
  return row.merged ? fmt(row.spend / row.merged) : '—';
}

/** Mean review rounds over the runs that recorded one, or "—" when none did. */
export function avgReviews(row: { review_total: number; review_n: number }): string {
  return row.review_n ? (row.review_total / row.review_n).toFixed(1) : '—';
}

/**
 * The model-lifecycle word and the colour that carries it, from the model-eol
 * snapshot's status — or `null` for a model the snapshot is content with (and
 * for `untracked`, which says nothing about the model, only about the snapshot).
 */
export function lifecycleNote(status: string): { label: string; class: string } | null {
  switch (status) {
    case 'retired':
      return { label: 'retired', class: 'text-danger' };
    case 'retiring':
      return { label: 'retiring', class: 'text-warn' };
    case 'scheduled':
      return { label: 'scheduled', class: 'text-accent-2' };
    case 'watch':
      return { label: 'watching', class: 'text-warn' };
    default:
      return null;
  }
}
