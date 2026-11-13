import { redirect } from '@sveltejs/kit';
import type { BudgetMap, SpendWindows } from '$lib/api';
import { unreadable, type Source } from '$lib/monitor/readouts';
import { readBudgetPage } from './reads';

export const ssr = false;

export async function load({
  fetch
}: {
  fetch: typeof globalThis.fetch;
}): Promise<{ budget: BudgetMap | null; spend: Source<SpendWindows> }> {
  try {
    // One generation of both readings — see `reads.ts`. Spend is already
    // non-essential in there: a failure costs the gauges their figures and
    // does not take the caps with it.
    return await readBudgetPage(fetch);
  } catch (e) {
    if (e instanceof Error && e.message.includes('-> 401')) {
      throw redirect(302, '/inbox');
    }
    // The caps are what failed; the spend read was abandoned with them, so it
    // is reported as not taken rather than blamed for the caps' status code.
    return { budget: null, spend: unreadable('spend', null) };
  }
}
