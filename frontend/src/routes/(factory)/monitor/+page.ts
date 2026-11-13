import { fetchBudget, type BudgetMap } from '$lib/api';
import { unreadable, type Source } from '$lib/monitor/readouts';

export const ssr = false;

export type MonitorData = {
  /**
   * `/api/config/budget` — the first reading of the caps, shared by the two
   * things on this page that are drawn from an override: the holds column's
   * override rows and the prior-cap pin on the spend gauges (`priorCap` marks
   * `file_value` if and only if `override` is in force).
   *
   * It is a SEED, not the page's standing answer. An override raised or
   * reverted by another operator moves both of those, so the holds column
   * re-reads the map on its own poll and hands each reading back to the page
   * (`onbudget`) — one reading behind both, rather than a pin left marking an
   * override the list below it has already dropped.
   */
  budget: Source<BudgetMap>;
};

/**
 * FUI-22 — the one read the Monitor readouts need that the factory pulse does
 * not already take. Working, Needs you and the two spend cells come from the
 * shell's shared poll (`factoryPulse`; spend is its dashboard's `glance.caps`,
 * #2847 Q2), so they are deliberately absent here: fetching them again would
 * double every page's load on a surface whose whole job is to be cheap to
 * leave open.
 *
 * A rejection is turned into the reason the pin's absence stands for rather
 * than swallowed into a `null`, so the screen can say which reading is missing
 * and why — and a budget map that would not load costs the page its prior-cap
 * pin and nothing more.
 */
export async function load({
  fetch
}: {
  fetch: typeof globalThis.fetch;
}): Promise<MonitorData> {
  try {
    return { budget: await fetchBudget(fetch) };
  } catch (err) {
    return { budget: unreadable('budget', err) };
  }
}
