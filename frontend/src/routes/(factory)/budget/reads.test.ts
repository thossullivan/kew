/**
 * `/budget` takes its two readings together — and why that is the whole point.
 *
 * A gauge on this page is assembled from both endpoints at once: the figure,
 * the cap tick and the "% of cap" come from `/api/report/spend`, the prior-cap
 * pin from `/api/config/budget`. The page used to re-read only the map after a
 * write, which left the two halves describing different caps — the case the
 * last test here pins down, stated in the readouts the page actually renders.
 */
import { describe, expect, it, vi } from 'vitest';
import type { BudgetMap, SpendWindows } from '$lib/api';
import {
  DAILY_CAP_FIELD,
  isUnavailable,
  priorCap,
  spendReadout,
  type Source
} from '$lib/monitor/readouts';
import { load } from './+page';
import { readBudgetPage } from './reads';

/** Before the write: the $250 cap the config file declares is in force. */
const BUDGET_BEFORE: BudgetMap = {
  'budget.daily_usd': { effective: 250.0, file_value: 250.0, override: null }
};

/** After it: an override to $1,000, so `file_value` is now the PRIOR cap. */
const BUDGET_AFTER: BudgetMap = {
  'budget.daily_usd': {
    effective: 1000.0,
    file_value: 250.0,
    override: {
      value: 1000.0,
      set_by: 'operator',
      set_at: '2026-09-27T10:00:00+00:00',
      reason: 'backfill'
    }
  }
};

const cell = (cap: number) => ({
  window_days: 1,
  spent: 240.0,
  cap,
  agentic: 240.0,
  interactive: 0.0
});

const SPEND_BEFORE: SpendWindows = { daily: cell(250.0), weekly: cell(1500.0) };
const SPEND_AFTER: SpendWindows = { daily: cell(1000.0), weekly: cell(1500.0) };

/** A fetch answering each path from `answers`; a number answers as a status. */
function api(answers: Record<string, unknown>) {
  return vi.fn(async (url: RequestInfo | URL) => {
    const answer = answers[String(url).split('?')[0]];
    if (typeof answer === 'number') return new Response('', { status: answer });
    return new Response(JSON.stringify(answer));
  }) as unknown as typeof globalThis.fetch;
}

const paths = (f: typeof globalThis.fetch) =>
  (f as unknown as ReturnType<typeof vi.fn>).mock.calls.map((c) => String(c[0])).sort();

describe('readBudgetPage', () => {
  it('takes the caps and the spend windows in one call', async () => {
    const fetch = api({ '/api/config/budget': BUDGET_AFTER, '/api/report/spend': SPEND_AFTER });
    const read = await readBudgetPage(fetch);
    expect(read.budget).toEqual(BUDGET_AFTER);
    expect(read.spend).toEqual(SPEND_AFTER);
    expect(paths(fetch)).toEqual(['/api/config/budget', '/api/report/spend']);
  });

  it('costs the gauges their figures when only spend is down, never the caps', async () => {
    const fetch = api({ '/api/config/budget': BUDGET_AFTER, '/api/report/spend': 503 });
    const read = await readBudgetPage(fetch);
    expect(read.budget).toEqual(BUDGET_AFTER);
    expect(read.spend).toEqual({ available: false, reason: 'spend unavailable (HTTP 503)' });
  });

  it('hands the caps failure to its caller, and leaves no rejection unhandled', async () => {
    const fetch = api({ '/api/config/budget': 500, '/api/report/spend': 503 });
    await expect(readBudgetPage(fetch)).rejects.toThrow('/api/config/budget -> 500');
  });
});

describe('/budget load', () => {
  it('seeds the page from one generation of both readings', async () => {
    const fetch = api({ '/api/config/budget': BUDGET_BEFORE, '/api/report/spend': SPEND_BEFORE });
    expect(await load({ fetch })).toEqual({ budget: BUDGET_BEFORE, spend: SPEND_BEFORE });
  });

  it('sends an unauthenticated read to sign in', async () => {
    const fetch = api({ '/api/config/budget': 401, '/api/report/spend': 401 });
    await expect(load({ fetch })).rejects.toMatchObject({ status: 302 });
  });

  it('says the caps are unread, without blaming spend for their status', async () => {
    const fetch = api({ '/api/config/budget': 500, '/api/report/spend': SPEND_BEFORE });
    expect(await load({ fetch })).toEqual({
      budget: null,
      spend: { available: false, reason: 'spend unavailable' }
    });
  });
});

describe('the gauge after a cap write', () => {
  /** The Today readout, exactly as `+page.svelte` assembles it. */
  const today = (budget: Source<BudgetMap>, spend: Source<SpendWindows>) =>
    spendReadout(
      'Today',
      isUnavailable(spend) ? spend : spend.daily,
      priorCap(budget, DAILY_CAP_FIELD)
    );

  it('contradicts itself when only the caps are re-read (the regression)', () => {
    // What a write that refetched `/api/config/budget` alone used to leave on
    // screen: the pin marks the prior cap at $250 on a track whose own cap
    // tick is also $250, under a meta reading 96.0% — while the table below
    // says the effective cap is $1,000.
    const stale = today(BUDGET_AFTER, SPEND_BEFORE);
    expect(stale.gauge!.marker!.value).toBe(stale.gauge!.cap);
    expect(stale.capLabel).toBe('$250');
    expect(stale.meta).toContain('96.0%');
  });

  it('reads against the new cap when both are re-read together', async () => {
    const fetch = api({ '/api/config/budget': BUDGET_AFTER, '/api/report/spend': SPEND_AFTER });
    const read = await readBudgetPage(fetch);
    const fresh = today(read.budget, read.spend);
    expect(fresh.gauge!.cap).toBe(1000.0);
    expect(fresh.gauge!.marker).toEqual({ value: 250.0, label: 'prior cap' });
    expect(fresh.capLabel).toBe('$1,000');
    expect(fresh.meta).toContain('24.0%');
  });
});
