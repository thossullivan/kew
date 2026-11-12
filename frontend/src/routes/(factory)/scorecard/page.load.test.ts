import { describe, expect, it, vi } from 'vitest';
import { load } from './+page';

const MODELS = {
  days: 3,
  rows: [
    {
      model: 'qwen',
      runs: 12,
      opened: 10,
      merged: 8,
      review_total: 4,
      review_n: 2,
      spend: 120.5
    }
  ]
};

const OUTCOMES = {
  weeks: [
    {
      week_start: '2026-09-21',
      merged_main: 66,
      merged_kew_touched: 30,
      merged_kew_authored: 28,
      ready_items: 12,
      ready_hours: { p50: 2.3, p90: 6.1, max: 9.0 },
      rounds_to_ready: { p50: 1.5, max: 4 },
      cost_per_merged: { mean: 26.4, p50: 24.77 },
      spend_total: 740.2,
      spend_unattributed: 31.0,
      nodes_done: 24,
      pr_sync_observed_at: '2026-09-27T18:00:00+00:00',
      partial: false
    }
  ],
  min_sample: 3,
  merged_main_head: 'abc1234',
  merged_main_unavailable: null
};

/** A fetch that answers each of the page's two reads with its own payload. */
function apiFetch() {
  return vi.fn(async (url: RequestInfo | URL) => {
    const body = String(url).includes('/outcomes') ? OUTCOMES : MODELS;
    return new Response(JSON.stringify(body));
  });
}

describe('/scorecard load', () => {
  it('returns scorecard data when API answers', async () => {
    const fetch = apiFetch();
    const data = await load({ fetch: fetch as unknown as typeof globalThis.fetch });
    expect(data.scorecard?.rows?.[0].model).toBe('qwen');
  });

  it('returns null when API unreachable', async () => {
    const fetch = vi.fn(async () => {
      throw new Error('offline');
    });
    const data = await load({ fetch: fetch as unknown as typeof globalThis.fetch });
    expect(data.scorecard).toBeNull();
    expect(data.outcomes).toBeNull();
  });

  it('requests a 30-day window by default so older frontier runs stay visible', async () => {
    const fetch = apiFetch();
    await load({ fetch: fetch as unknown as typeof globalThis.fetch });
    const urls = fetch.mock.calls.map((call) => String(call[0]));
    expect(urls.filter((url) => url.includes('/api/report/models'))).toEqual([
      '/api/report/models?days=30'
    ]);
  });

  it('reads eight weeks of outcomes alongside the per-model table', async () => {
    const fetch = apiFetch();
    const data = await load({ fetch: fetch as unknown as typeof globalThis.fetch });
    const urls = fetch.mock.calls.map((call) => String(call[0]));
    expect(urls).toContain('/api/report/outcomes?weeks=8');
    expect(data.outcomes?.weeks[0].merged_main).toBe(66);
  });

  it('keeps each read independent: one failing does not null the other', async () => {
    const fetch = vi.fn(async (url: RequestInfo | URL) => {
      if (String(url).includes('/outcomes')) throw new Error('not served');
      return new Response(JSON.stringify(MODELS));
    });
    const data = await load({ fetch: fetch as unknown as typeof globalThis.fetch });
    expect(data.scorecard?.rows?.[0].model).toBe('qwen');
    expect(data.outcomes).toBeNull();
  });
});
