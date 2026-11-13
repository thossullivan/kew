/**
 * FUI-22 — what `/monitor` reads, and what it says when a read fails.
 *
 * Since #2847 (Q2) the load takes one read, the budget map: the spend figures
 * are the shell pulse's dashboard `glance.caps`, so a `/api/report/spend` read
 * here would be the second read of one figure every load. A budget map that
 * will not load costs the This week gauge its prior-cap pin and nothing more —
 * the load still answers, with the reason.
 */
import { describe, expect, it, vi } from 'vitest';
import { load } from './+page';

const SPEND = {
  daily: { window_days: 1, spent: 178.05, cap: 250.0, agentic: 178.05, interactive: 0.0 },
  weekly: { window_days: 7, spent: 915.05, cap: 1500.0, agentic: 915.05, interactive: 0.0 }
};

const BUDGET = {
  'budget.weekly_usd': {
    effective: 1500.0,
    file_value: 600.0,
    override: {
      value: 1500.0,
      set_by: 'operator',
      set_at: '2026-09-24T15:23:40+00:00',
      reason: 'weekly cap reached at $600'
    }
  }
};

/** A fetch that answers each path from `answers`, or fails as instructed. */
function api(answers: Record<string, unknown | Error | number>) {
  return vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url).split('?')[0];
    const answer = answers[path];
    if (answer instanceof Error) throw answer;
    if (typeof answer === 'number') return new Response('', { status: answer });
    return new Response(JSON.stringify(answer));
  }) as unknown as typeof globalThis.fetch;
}

describe('/monitor load', () => {
  it('reads the budget map, and nothing the shell already polls', async () => {
    const fetch = api({ '/api/report/spend': SPEND, '/api/config/budget': BUDGET });
    const data = await load({ fetch });
    expect(data.budget).toEqual(BUDGET);
    const paths = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls.map((c) =>
      String(c[0])
    );
    expect(paths).toEqual(['/api/config/budget']);
  });

  it('says why when the budget map is refused', async () => {
    const fetch = api({ '/api/config/budget': 500 });
    const data = await load({ fetch });
    expect(data.budget).toEqual({ available: false, reason: 'budget unavailable (HTTP 500)' });
  });

  it('never throws: an offline serve is a reason, not an error page', async () => {
    const fetch = api({ '/api/config/budget': new Error('offline') });
    const data = await load({ fetch });
    expect(data.budget).toEqual({ available: false, reason: 'budget unavailable' });
  });
});
