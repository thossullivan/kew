/**
 * The two readings `/budget` draws from, taken as ONE generation.
 *
 * The page shows one gauge per window, and the gauge is assembled from both
 * endpoints at once: its figure, its cap tick and its "% of cap" come from
 * `/api/report/spend` (which resolves the override server-side), while the
 * prior-cap pin on the same track comes from `/api/config/budget`. Refresh one
 * without the other and the two halves describe different caps — right after
 * raising the daily cap to $1,000, a page that re-read only the map pins
 * "prior cap $250" on a track whose own tick still reads $250 and whose meta
 * still says 96.0% of cap, while the table below it reads Effective $1,000.
 *
 * So there is one function to take them, and the page's load and its two write
 * paths all go through it: no caller can refresh half the gauge.
 *
 * The two reads are not equal in weight, and they fail differently. The caps
 * are the page; a map that would not load leaves nothing to render, so the
 * rejection is passed to the caller (the load turns a 401 into the sign-in
 * redirect, a write turns it into the row's error line). Spend is a reading
 * beside them: a spend outage costs the gauges and nothing else, so it is
 * turned into the reason the cells print, exactly as `/monitor` does it.
 */
import { fetchBudget, fetchSpend, type BudgetMap, type SpendWindows } from '$lib/api';
import { unreadable, type Source } from '$lib/monitor/readouts';

export type BudgetPageRead = { budget: BudgetMap; spend: Source<SpendWindows> };

export async function readBudgetPage(f: typeof globalThis.fetch = fetch): Promise<BudgetPageRead> {
  // Attached before the map is awaited, so a map that rejects first does not
  // leave this rejection unhandled.
  const spend = fetchSpend(f).catch((e) => unreadable('spend', e));
  const budget = await fetchBudget(f);
  return { budget, spend: await spend };
}
