import { readFileSync } from 'node:fs';
import { describe, expect, it, vi } from 'vitest';
import { RUN_AUDIT_PAGE } from '$lib/api';
import { mayHoldMoreRunEvents } from '$lib/runs/events';
import { load } from './+page';

// The canonical deep view loads three things and only three: the run row, its
// receipt-backed Flight projection, and — when something was sealed — the diff
// bound to that artifact. The exact prompt bytes are deliberately absent from
// this list; `data-sveltekit-preload-data="hover"` in app.html means hovering
// a link to this route runs `load`, so anything fetched here is fetched
// without the operator ever asking for it.

const RUN = {
  run_id: 'r1',
  issue_num: 1525,
  title: 'restore the flight recorder',
  runner: 'claude-code',
  model: 'claude-opus-5',
  status: 'merged',
  cost_usd: 1.25,
  channel: 'hosted',
  started_at: '2026-08-18T10:00:00Z',
  finished_at: '2026-08-18T10:12:00Z'
};

const FLIGHT = {
  run_id: 'r1',
  attempt_number: 1,
  item_id: '1525',
  issue_num: 1525,
  source_tracker: 'github',
  source_id: '1525',
  sealed: true,
  through_seq: 2,
  chain_head: 'f'.repeat(64),
  chain_verified: true,
  artifact_node_id: 'r1:2',
  runner_telemetry: 'structured',
  events: [],
  gaps: [],
  availability: []
};

const DIFF = { node_id: 'r1:2', text: 'artifact', files: [], availability: [] };

const AUDIT = {
  run_id: 'r1',
  events: [
    {
      seq: 1,
      ts: '2026-08-18T10:00:00Z',
      event: 'run_started',
      actor_type: 'policy',
      item_id: '1525',
      payload: {}
    }
  ],
  next_seq: 1
};

/** The route this page is opened at, with whatever a link put in its query. */
const at = (search = ''): URL => new URL(`http://127.0.0.1:8765/runs/r1${search}`);

/** A fetch that answers each canonical route and records every URL asked for. */
function serving(over: Record<string, unknown> = {}) {
  const urls: string[] = [];
  const body = (url: string) => {
    if (url.includes('/flight')) return over.flight ?? FLIGHT;
    if (url.includes('/nodes/')) return over.diff ?? DIFF;
    if (url.includes('/audit')) return over.audit ?? AUDIT;
    return over.run ?? RUN;
  };
  const fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    urls.push(url);
    return new Response(JSON.stringify(body(url)));
  });
  return { urls, fetch: fetch as unknown as typeof globalThis.fetch };
}

describe('/runs/[id] load', () => {
  it('loads the run, its flight evidence and the artifact-bound diff', async () => {
    const { urls, fetch } = serving();
    const data = await load({ params: { id: 'r1' }, url: at(), fetch });
    expect(data.runId).toBe('r1');
    expect(data.run?.run_id).toBe('r1');
    expect(data.flight?.through_seq).toBe(2);
    expect(data.diff?.node_id).toBe('r1:2');
    expect(urls).toEqual([
      '/api/runs/r1',
      '/api/runs/r1/flight',
      '/api/runs/r1/audit?after_seq=0&limit=200',
      '/api/runs/r1/nodes/r1%3A2/diff'
    ]);
  });

  it('reads the first forward page of the run\'s ledger events (#2300)', async () => {
    const { urls, fetch } = serving();
    const data = await load({ params: { id: 'r1' }, url: at(), fetch });

    expect(data.audit?.events.map((e) => e.seq)).toEqual([1]);
    expect(data.audit?.next_seq).toBe(1);
    // Bounded and from the start: the page walks forward from next_seq.
    expect(urls).toContain('/api/runs/r1/audit?after_seq=0&limit=200');
  });

  it('selects the attempt the link asked for, not the latest (#2300)', async () => {
    const { urls, fetch } = serving();
    const data = await load({ params: { id: 'r1' }, url: at('?attempt=3'), fetch });

    expect(data.attempt).toBe(3);
    expect(urls).toContain('/api/runs/r1/flight?attempt=3');
  });

  it('ignores an attempt that is not a positive whole number', async () => {
    for (const search of ['?attempt=0', '?attempt=-2', '?attempt=latest', '?attempt=1.5', '']) {
      const { urls, fetch } = serving();
      const data = await load({ params: { id: 'r1' }, url: at(search), fetch });

      expect(data.attempt, search).toBeNull();
      expect(urls, search).toContain('/api/runs/r1/flight');
    }
  });

  it('keeps the evidence when only the event page fails to read', async () => {
    const fetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/audit')) return new Response('nope', { status: 500 });
      if (url.includes('/flight')) return new Response(JSON.stringify(FLIGHT));
      if (url.includes('/nodes/')) return new Response(JSON.stringify(DIFF));
      return new Response(JSON.stringify(RUN));
    });
    const data = await load({
      params: { id: 'r1' },
      url: at(),
      fetch: fetch as unknown as typeof globalThis.fetch
    });

    expect(data.audit).toBeNull();
    expect(data.run?.run_id).toBe('r1');
    expect(data.flight?.through_seq).toBe(2);
  });

  it('hands a full first page on as a page, never as the whole run', async () => {
    // The loader reads page 0 only. A run with more ledger than the cap used
    // to be truncated in silence: there was no further-page control and the
    // header counted the rows it held as the run's whole history.
    const events = Array.from({ length: RUN_AUDIT_PAGE }, (_, i) => ({
      ...AUDIT.events[0],
      seq: i + 1
    }));
    const { fetch } = serving({
      audit: { run_id: 'r1', events, next_seq: RUN_AUDIT_PAGE }
    });
    const data = await load({ params: { id: 'r1' }, url: at(), fetch });

    expect(data.audit?.events).toHaveLength(RUN_AUDIT_PAGE);
    // The page the component is handed says, by being full, that there is
    // more behind it — and the cursor to read it with.
    expect(mayHoldMoreRunEvents(data.audit)).toBe(true);
    expect(data.audit?.next_seq).toBe(RUN_AUDIT_PAGE);
  });

  it('never requests the protected prompt while loading or preloading', async () => {
    const { urls, fetch } = serving();
    await load({ params: { id: 'r1' }, url: at(), fetch });
    for (const url of urls) expect(url).not.toMatch(/prompt/);
  });

  it('asks for no diff when nothing is sealed', async () => {
    const { urls, fetch } = serving({
      flight: { ...FLIGHT, sealed: false, artifact_node_id: null, through_seq: null }
    });
    const data = await load({ params: { id: 'r1' }, url: at(), fetch });
    expect(data.diff).toBeNull();
    expect(urls.some((u) => u.includes('/nodes/'))).toBe(false);
  });

  it('renders an empty state instead of erroring when the API is unreachable', async () => {
    const fetch = vi.fn(async () => {
      throw new Error('offline');
    });
    const data = await load({
      params: { id: 'r1' },
      url: at(),
      fetch: fetch as unknown as typeof globalThis.fetch
    });
    expect(data.run).toBeNull();
    expect(data.flight).toBeNull();
    expect(data.diff).toBeNull();
    expect(data.runId).toBe('r1');
  });

  it('keeps the run and reports the gap when only the evidence read fails', async () => {
    const fetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/flight')) return new Response('nope', { status: 500 });
      return new Response(JSON.stringify(RUN));
    });
    const data = await load({
      params: { id: 'r1' },
      url: at(),
      fetch: fetch as unknown as typeof globalThis.fetch
    });
    expect(data.run?.run_id).toBe('r1');
    expect(data.flight).toBeNull();
  });

  it('reads a run this store has never heard of as a missing run, not a crash', async () => {
    const fetch = vi.fn(async () => new Response('no', { status: 404 }));
    const data = await load({
      params: { id: 'ghost' },
      url: new URL('http://127.0.0.1:8765/runs/ghost'),
      fetch: fetch as unknown as typeof globalThis.fetch
    });
    expect(data.run).toBeNull();
    expect(data.runId).toBe('ghost');
  });

  it('survives a 401 outside a browser rather than throwing at the loader', async () => {
    const fetch = vi.fn(async () => new Response('no', { status: 401 }));
    const data = await load({
      params: { id: 'r1' },
      url: at(),
      fetch: fetch as unknown as typeof globalThis.fetch
    });
    expect(data.run).toBeNull();
  });

  it('re-reads the durable evidence after a server restart', async () => {
    const first = serving();
    const before = await load({ params: { id: 'r1' }, url: at(), fetch: first.fetch });
    expect(before.flight?.through_seq).toBe(2);

    // Same run id, same URL, a restarted server that has chained more facts.
    const after = serving({ flight: { ...FLIGHT, through_seq: 9 } });
    const reloaded = await load({ params: { id: 'r1' }, url: at(), fetch: after.fetch });
    expect(reloaded.flight?.through_seq).toBe(9);
    expect(after.urls).toEqual(first.urls);
  });
});

describe('/runs/[id] page source', () => {
  const source = readFileSync(new URL('./+page.svelte', import.meta.url), 'utf8');

  it('never persists anything to browser storage', () => {
    expect(source).not.toMatch(/localStorage|sessionStorage|indexedDB|caches\./);
  });

  it('reaches the protected prompt route only from an explicit click handler', () => {
    expect(source).toContain('fetchPrompt');
    // The one call site is inside the reveal handler, which is what an
    // onclick invokes — never a module-level or $effect-driven read.
    const outsideHandler = source
      .split('\n')
      .filter((line) => line.includes('fetchPrompt('))
      .every((line) => /await fetchPrompt\(/.test(line));
    expect(outsideHandler).toBe(true);
    expect(source).not.toMatch(/\$effect\([^)]*fetchPrompt/);
  });

  it('drops any revealed bytes before a refresh re-reads the evidence', () => {
    const refresh = /async function refresh\(\)[\s\S]*?\n  }/.exec(source)?.[0] ?? '';
    expect(refresh).toContain('acted = null');
    expect(refresh.indexOf('acted = null')).toBeLessThan(refresh.indexOf('invalidateAll'));
  });

  it('mounts the event split and hands it the loader\'s first page (#2300)', () => {
    expect(source).toContain('<RunEvents');
    // The page passes the page the LOADER read, never a fetch of its own: a
    // read in this file would run on a hover-preload of the route.
    expect(source).toMatch(/first=\{data\.audit\}/);
    expect(source).not.toContain('fetchRunAudit');
  });

  it('states the governed-review verdict, or that none is recorded (#2300)', () => {
    expect(source).toContain('data.run.verdict');
    // Coloured by the console's ONE verdict mapping, so READY_WITH_RESIDUALS
    // is not painted as a setback here while the epic Gantt calls it success.
    // Design review c9 D11: the page draws the verdict through `verdictReading`
    // ($lib/runs/verdict), whose ready-or-not is `verdictTone`'s and which
    // `verdict.test.ts` holds to it.
    expect(source).toContain('verdictReading(');
    expect(source).toContain('no governed-review verdict is recorded');
  });

  it('prints the recorded route and accounting columns, or "not recorded"', () => {
    for (const column of [
      'run_kind',
      'provider',
      'transport_mode',
      'reported_model',
      'planned_cost_usd',
      'accounting_status'
    ]) {
      expect(source, column).toContain(column);
    }
    // An absent column is a stated absence, never a blank value.
    expect(source).toContain("'not recorded'");
  });

  it('carries no analytics or service-worker registration', () => {
    expect(source).not.toMatch(/navigator\.sendBeacon|serviceWorker|analytics|gtag/);
  });
});
