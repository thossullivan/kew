/**
 * What the Prompts page's outcomes table prints for one version (#3026).
 *
 * Numbers are formatted here, once, so the table and its check read the same
 * strings. A version under the server's `min_items` prints a sentence in
 * place of its rates: a rate over four items reads as a finding it is not.
 */
import type { ImplementerOutcome, ReviewerOutcome } from '$lib/api';

/** The sentence a version with too few items (or rounds) shows instead of rates. */
export function tooFew(n: number, unit: 'items' | 'rounds' = 'items'): string {
  return `Too few ${unit} to compare yet (n = ${n}).`;
}

/** A share as a whole percentage, or an em dash when there was none to take. */
export function percent(rate: number | null): string {
  return rate === null ? '—' : `${Math.round(rate * 100)}%`;
}

/** A mean round count to one decimal, or an em dash. */
export function rounds(mean: number | null): string {
  return mean === null ? '—' : mean.toFixed(1);
}

/** Dollars to the cent, or an em dash when no item merged. */
export function dollars(cost: number | null): string {
  return cost === null ? '—' : `$${cost.toFixed(2)}`;
}

/** The three rate cells of an implementer row, in table order. */
export function implementerCells(row: ImplementerOutcome): [string, string, string] {
  return [percent(row.first_pass_rate), rounds(row.mean_rounds_to_ready), dollars(row.median_cost_usd)];
}

/**
 * The reviewer table's verdict columns: every verdict a comparable version
 * returned, the most returned across those versions first, then by name. A
 * version under `min_items` prints a sentence, so its verdicts add no column.
 */
export function verdictColumns(rows: ReviewerOutcome[]): string[] {
  const totals = new Map<string, number>();
  for (const row of rows.filter((r) => r.comparable)) {
    for (const [verdict, n] of Object.entries(row.verdicts)) {
      totals.set(verdict, (totals.get(verdict) ?? 0) + n);
    }
  }
  return [...totals.entries()].sort(([a, x], [b, y]) => y - x || a.localeCompare(b)).map(([verdict]) => verdict);
}

/** A reviewer row's count for one verdict column: `0` when it returned none. */
export function verdictCount(row: ReviewerOutcome, verdict: string): string {
  return String(row.verdicts[verdict] ?? 0);
}

/** A reviewer row's cells, one per verdict column in column order. The cell
 *  draws `count`, a number; the verdict names its column only. */
export function verdictCells(row: ReviewerOutcome, verdicts: string[]): { verdict: string; count: string }[] {
  return verdicts.map((verdict) => ({ verdict, count: verdictCount(row, verdict) }));
}

/** A verdict's column heading: `NEEDS_CHANGES` reads `NEEDS CHANGES`. */
export function verdictLabel(verdict: string): string {
  return verdict.replaceAll('_', ' ');
}

/** How many times the page reads the outcomes and the versions again while their generations differ. */
export const OUTCOMES_READ_ATTEMPTS = 3;

/** The sentence drawn in place of a table whose generation is not the versions'. */
export const OUTCOMES_REREAD = 'The versions changed while this table was read. Reading again.';

/** The sentence once the two reads still differ after every re-read. */
export const OUTCOMES_DIVERGED =
  'The versions kept changing while this table was read, so no version can be compared here.';

/**
 * Whether an outcomes table may be drawn beside the versions the editor shows
 * (#3090). The two are separate reads, and a decision committed between them
 * (a CLI approval, say) would pair one generation's table with another's
 * history: a pair no committed state produced. Each names its generation:
 *
 * - `draw`: one generation, or no history to compare with (`history` null:
 *   its read failed). The table is current.
 * - `wait`: a read is in flight on either side (`history` undefined is the
 *   editor's). The table last drawn stays; this one is not drawn yet.
 * - `reread`: they differ and both reads have landed; read both again.
 * - `diverged`: they still differ after `OUTCOMES_READ_ATTEMPTS` re-reads.
 */
export type TableAgreement = 'draw' | 'wait' | 'reread' | 'diverged';

export function tableAgreement(state: {
  outcomes: number;
  history: number | null | undefined;
  pending: boolean;
  attempts: number;
}): TableAgreement {
  if (state.history === null) return 'draw';
  if (state.pending || state.history === undefined) return 'wait';
  if (state.outcomes === state.history) return 'draw';
  return state.attempts >= OUTCOMES_READ_ATTEMPTS ? 'diverged' : 'reread';
}
