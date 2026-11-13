import { describe, expect, it, vi } from 'vitest';
import { load } from './+page';

describe('/runs load', () => {
  it('returns run list when API answers', async () => {
    const dto = [
      {
        run_id: 'r1',
        issue_num: 12,
        title: 'fix x',
        runner: 'lemonade/qwen',
        model: 'qwen',
        status: 'merged',
        cost_usd: 0,
        channel: 'local',
        started_at: null,
        finished_at: null
      }
    ];
    const fetch = vi.fn(async () => new Response(JSON.stringify(dto)));
    const data = await load({ fetch: fetch as unknown as typeof globalThis.fetch });
    expect(data.runs?.[0].run_id).toBe('r1');
  });

  it('returns null when API unreachable', async () => {
    const fetch = vi.fn(async () => {
      throw new Error('offline');
    });
    const data = await load({ fetch: fetch as unknown as typeof globalThis.fetch });
    expect(data.runs).toBeNull();
  });
});
