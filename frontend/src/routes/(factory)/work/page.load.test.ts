/**
 * FUI-18 — `/work`'s loader must fail soft, per payload.
 *
 * The list is built from the work projection and the dashboard, and each says
 * something the other cannot: the projection knows the items kew has touched,
 * the dashboard's `glance.ready_queue` knows the idle ones. A throw from either
 * would swap the whole screen for an error boundary, so both are settled
 * separately and a failure arrives as `null` for the page to name.
 */
import { describe, expect, it, vi } from 'vitest';
import dashboardJson from '../../../../fixtures/factory/dashboard.json';
import projectionJson from '../../../../fixtures/factory/dashboard-work.json';
import { load } from './+page';

/** A fetch that answers `/api/dashboard/work` and `/api/dashboard` by path. */
function fetchWith(answers: Record<string, unknown | Error>) {
  return vi.fn(async (input: unknown) => {
    const url = String(input);
    const key = Object.keys(answers)
      .sort((a, b) => b.length - a.length)
      .find((path) => url.startsWith(path));
    if (key === undefined) throw new Error(`unexpected read: ${url}`);
    const answer = answers[key];
    if (answer instanceof Error) throw answer;
    return new Response(JSON.stringify(answer));
  }) as unknown as typeof globalThis.fetch;
}

/** What the page awaits: the loader's own reading, taken the way the page takes it. */
function reading({ fetch }: { fetch: typeof globalThis.fetch }) {
  return load({ fetch }).reading;
}

const BOTH = {
  '/api/dashboard/work': projectionJson,
  '/api/dashboard': dashboardJson
};

describe('/work load', () => {
  it('hands the reading to the page without waiting for it (#2545 D9)', () => {
    // An awaited read held the whole client-rendered app — header included —
    // off the screen until both payloads were back. `load` is synchronous now,
    // so a server that never answers still lets the shell draw.
    const fetch = vi.fn(() => new Promise<Response>(() => {}));
    const data = load({ fetch: fetch as unknown as typeof globalThis.fetch });
    expect(data).not.toBeInstanceOf(Promise);
    expect(data.reading).toBeInstanceOf(Promise);
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it('reads both payloads when the server answers', async () => {
    const data = await reading({ fetch: fetchWith(BOTH) });
    expect(data.projection?.views).toHaveLength(6);
    expect(data.dashboard?.glance.ready_queue).toHaveLength(4);
  });

  it('takes the two reads in parallel, not one after the other', async () => {
    // #2131 review: this used to assert `toHaveBeenCalledTimes(2)` on a settled
    // loader, which a strictly sequential `await`-then-`await` satisfies too.
    // The reads are held open instead, so the question is whether the SECOND
    // one is taken while the FIRST is still outstanding — which is the only
    // thing "in parallel" means and the only thing a sequential loader fails.
    const started: string[] = [];
    let answer!: () => void;
    const held = new Promise<void>((resolve) => {
      answer = resolve;
    });
    const fetch = vi.fn(async (input: unknown) => {
      const url = String(input);
      started.push(url);
      await held;
      return new Response(JSON.stringify(url.includes('/work') ? projectionJson : dashboardJson));
    });
    const loading = reading({ fetch: fetch as unknown as typeof globalThis.fetch });
    try {
      await vi.waitFor(() => expect(started).toHaveLength(2));
    } finally {
      // Always release, so a failure here reports itself rather than hanging.
      answer();
    }
    expect(started.some((url) => url.startsWith('/api/dashboard/work'))).toBe(true);
    expect(started.some((url) => url === '/api/dashboard')).toBe(true);
    const data = await loading;
    expect(data.projection?.views).toHaveLength(6);
    expect(data.dashboard?.glance.ready_queue).toHaveLength(4);
  });

  it('reports an unreadable projection as null, keeping the dashboard', async () => {
    const data = await reading({
      fetch: fetchWith({ ...BOTH, '/api/dashboard/work': new Error('offline') })
    });
    expect(data.projection).toBeNull();
    expect(data.dashboard?.glance.ready_queue).toHaveLength(4);
  });

  it('reports an unreadable dashboard as null, keeping the projection', async () => {
    const data = await reading({
      fetch: fetchWith({ ...BOTH, '/api/dashboard': new Error('offline') })
    });
    expect(data.dashboard).toBeNull();
    expect(data.projection?.views).toHaveLength(6);
  });

  it('never throws, so the screen is never an error boundary', async () => {
    const data = await reading({
      fetch: vi.fn(async () => {
        throw new Error('offline');
      }) as unknown as typeof globalThis.fetch
    });
    expect(data).toEqual({ projection: null, dashboard: null });
  });

  it('settles `listRead` when the list read does, while the dashboard read is still out (#3055)', async () => {
    // The page keeps one list read open, and frees the slot on `listRead`: a
    // slow dashboard must not hold the pulse's list re-reads off with it.
    const fetch = vi.fn((input: unknown) =>
      String(input).startsWith('/api/dashboard/work')
        ? Promise.reject(new Error('offline'))
        : new Promise<Response>(() => {})
    );
    const data = load({ fetch: fetch as unknown as typeof globalThis.fetch });
    await expect(data.listRead).resolves.toBeUndefined();
    let read = false;
    void data.reading.then(() => {
      read = true;
    });
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(read).toBe(false);
  });
});
