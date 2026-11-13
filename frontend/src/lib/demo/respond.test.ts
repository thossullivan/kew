/**
 * The demo console's `/api` answers what the dev fixture server answers (#3139).
 *
 * The demo build bundles a fixture set and answers every read in the browser;
 * the dev server (`npm run dev:fixtures`) answers the same reads off the disk.
 * Both go through `fixtures/core.ts`, and this pins that they agree: same
 * status, same body, at the same instant — for every file in the set the demo
 * ships, the per-query audit reads, a keyed read and the 404s.
 */
import { readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { readFixtureSet } from '../../../demo/fixture-bundle';
import { FIXTURE_EPOCH, fixtureMiddleware } from '../../../fixtures/plugin';
import { demoApi, demoFetch, rebasedSet } from './respond';

const NOW = Date.parse('2026-10-24T09:30:00Z');
/**
 * The factory set, read the way the demo build reads its own: the dev
 * middleware this is compared with serves the factory set, so the two answer
 * the same files. The demo build itself ships the demo world (#3141).
 */
const FACTORY_SET = new URL('../../../fixtures/factory/', import.meta.url);
const set = readFixtureSet(FACTORY_SET);
const api = demoApi(rebasedSet(set, NOW));

afterEach(() => {
  vi.useRealTimers();
});

/** What the dev middleware answered `url` with at `NOW`: its status and parsed body. */
function served(url: string, method = 'GET'): { status: number; body: unknown } {
  vi.useFakeTimers({ toFake: ['Date'] });
  vi.setSystemTime(NOW);
  let status = 200;
  let text = '';
  const req = { url, method, on: () => req };
  const res = {
    set statusCode(code: number) {
      status = code;
    },
    get statusCode() {
      return status;
    },
    setHeader: () => res,
    write: () => true,
    end(chunk?: string) {
      text += chunk ?? '';
      return res;
    }
  };
  fixtureMiddleware(
    req as unknown as Parameters<typeof fixtureMiddleware>[0],
    res as unknown as Parameters<typeof fixtureMiddleware>[1],
    () => {
      throw new Error(`${url} was passed on, not answered`);
    }
  );
  vi.useRealTimers();
  return { status, body: text === '' ? null : JSON.parse(text) };
}

const topLevel = readdirSync(fileURLToPath(FACTORY_SET)).filter((f) => f.endsWith('.json'));

describe('the bundled set', () => {
  it('holds every file of the set, sub-folders included, read as of the set epoch', () => {
    expect(Object.keys(set.files)).toEqual(expect.arrayContaining(topLevel));
    expect(Object.keys(set.files)).toContain('dispatch-resolve/2060.json');
    // The factory set's epoch is its newest instant, which is FIXTURE_EPOCH.
    expect(Date.parse(set.epoch)).toBe(Date.parse(FIXTURE_EPOCH));
    expect(set.source).toBe('frontend/fixtures/factory/');
  });
});

describe('a read answers what the dev fixture server answers', () => {
  it.each(topLevel.map((file) => `/api/${file.replace(/\.json$/, '')}`))('%s', (url) => {
    expect(api.answer('GET', url)).toEqual(served(url));
  });

  it.each([
    '/api/audit?limit=2',
    '/api/audit?since=2026-10-24T08:00:00Z&until=2026-10-24T10:00:00Z',
    '/api/audit?limit=0',
    '/api/audit/histogram?since=2026-10-24T09:00:00Z&until=2026-10-24T09:30:00Z&bucket_s=30',
    '/api/audit/histogram?since=2026-10-23T09:30:00Z&until=2026-10-24T09:30:00Z&bucket_s=900&epic=launch-fixes',
    '/api/audit/histogram?since=x&until=y&bucket_s=1',
    '/api/dispatch/resolve?item=2060&lane=implement',
    '/api/dispatch/resolve?item=9999',
    '/api/runs/nope',
    '/api/../etc/passwd',
    '/api/runs/f5dcb6a5/nodes/f5dcb6a5:4/diff',
    '/api/runs/f5dcb6a5/audit?after_seq=0&limit=200',
    '/api/runs/f5dcb6a5/audit?after_seq=1045&limit=3',
    '/api/runs/f5dcb6a5/audit?after_seq=1051&limit=200',
    '/api/runs/f5dcb6a5/audit?after_seq=x',
    '/api/runs/f5dcb6a5/audit?limit=201'
  ])('%s', (url) => {
    expect(api.answer('GET', url)).toEqual(served(url));
  });

  it('moves every instant by the one delta now - epoch', () => {
    const runs = api.answer('GET', '/api/runs') as { body: { started_at: string | null }[] };
    const committed = set.files['runs.json'] as { started_at: string | null }[];
    const delta = NOW - Date.parse(set.epoch);
    const i = committed.findIndex((r) => r.started_at !== null);
    expect(Date.parse(runs.body[i].started_at!)).toBe(Date.parse(committed[i].started_at!) + delta);
  });

  it('names the file it looked for when there is none', () => {
    expect(api.answer('GET', '/api/runs/nope')).toEqual({
      status: 404,
      body: { detail: 'no fixture for /api/runs/nope', expected: 'frontend/fixtures/factory/runs-nope.json' }
    });
  });
});

describe("an epic's change strip", () => {
  const strip = [{ id: 30, journal_tick_id: null }];
  const changes = [{ id: 1, journal_tick_id: 1 }, { id: 9, journal_tick_id: 9 }];
  const files = { 'epics-e-tick-strip.json': strip, 'epics-e-tick-strip-changes.json': changes };
  const own = demoApi({ files, epoch: '2026-10-21T15:00:00Z', source: 'set/' });

  it('answers ?changes=true out of the set’s change-strip file, and the plain read out of the strip', () => {
    expect(own.answer('GET', '/api/epics/e/tick-strip?changes=true&limit=200')).toEqual({ status: 200, body: changes });
    expect(own.answer('GET', '/api/epics/e/tick-strip?limit=200')).toEqual({ status: 200, body: strip });
  });

  it('answers ?changes=true out of the strip for a set with no change-strip file, as the dev server does', () => {
    const plain = demoApi({ files: { 'epics-e-tick-strip.json': strip }, epoch: '2026-10-21T15:00:00Z', source: 'set/' });
    expect(plain.answer('GET', '/api/epics/e/tick-strip?changes=true&limit=200')).toEqual({ status: 200, body: strip });
  });
});

describe("a live run's audit pages forward from its cursor", () => {
  // #3461 (C11): the demo's pending run is polled every second from the last
  // `next_seq` it read, and the shim answered every poll with the whole file,
  // so the run page rebuilt its event list and jumped to its foot each second.
  const world = readFixtureSet(new URL('../../../demo/world/', import.meta.url));
  const demo = demoApi(rebasedSet(world, NOW));
  const pending = (world.files['runs.json'] as { run_id: string; status: string }[]).find(
    (r) => r.status === 'pending'
  )!;
  type Page = { run_id: string; events: { seq: number }[]; next_seq: number };
  const page = (after: number, limit = 200) =>
    demo.answer('GET', `/api/runs/${pending.run_id}/audit?after_seq=${after}&limit=${limit}`) as {
      status: number;
      body: Page;
    };

  it('a second poll returns no event the first already showed', () => {
    expect(pending.run_id).toBe('ab4656b0');
    const first = page(0);
    expect(first.status).toBe(200);
    // Not vacuous: the run has events to show the first time.
    expect(first.body.events.length).toBeGreaterThan(0);
    const second = page(first.body.next_seq);
    expect(second.status).toBe(200);
    expect(second.body.events).toEqual([]);
    // An empty page holds the cursor it was asked with, as the server's does.
    expect(second.body.next_seq).toBe(first.body.next_seq);
  });

  it('serves only the events past the cursor, oldest first, a limit at a time', () => {
    const all = page(0).body.events.map((e) => e.seq);
    const head = page(0, 2).body;
    expect(head.events.map((e) => e.seq)).toEqual(all.slice(0, 2));
    expect(head.next_seq).toBe(all[1]);
    const rest = page(head.next_seq).body.events.map((e) => e.seq);
    expect(rest).toEqual(all.slice(2));
    expect(rest.every((seq) => seq > head.next_seq)).toBe(true);
  });

  it('refuses a cursor or limit the server would refuse', () => {
    expect(demo.answer('GET', `/api/runs/${pending.run_id}/audit?after_seq=-1`).status).toBe(422);
    expect(demo.answer('GET', `/api/runs/${pending.run_id}/audit?limit=0`).status).toBe(422);
  });
});

describe('a write answers 405 with what it would have done', () => {
  it('says the sentence and the command', () => {
    expect(api.answer('POST', '/api/fleet/stop', { reason: 'x' })).toEqual({
      status: 405,
      body: {
        detail:
          'Demo: this would kill every running agent and refuse new dispatches until resumed. In kew, run `kew stop`.'
      }
    });
  });

  it('refuses a write the table does not name, still as a sentence', () => {
    const answer = api.answer('PUT', '/api/whatever') as { status: number; body: { detail: string } };
    expect(answer.status).toBe(405);
    expect(answer.body.detail).toMatch(/^Demo: /);
  });
});

describe('demoFetch', () => {
  const ORIGIN = 'https://demo.example';
  const passed: string[] = [];
  const passThrough = (async (input: RequestInfo | URL) => {
    passed.push(String(input));
    return new Response('asset');
  }) as typeof fetch;
  const f = demoFetch(api, ORIGIN, passThrough);

  it('answers this origin’s /api from the set and passes everything else on', async () => {
    const r = await f('/api/meta');
    expect(r.status).toBe(200);
    expect(r.headers.get('Content-Type')).toBe('application/json');
    expect(await r.json()).toEqual(served('/api/meta').body);
    await f('/_app/immutable/x.js');
    expect(passed).toEqual(['/_app/immutable/x.js']);
  });

  it('carries a write’s body to the sentence it is refused with', async () => {
    const r = await f('/api/approvals/2055', {
      method: 'POST',
      body: JSON.stringify({ action: 'deny', reason: '' })
    });
    expect(r.status).toBe(405);
    expect(await r.json()).toEqual({
      detail: 'Demo: this would deny the hold on #2055 and park it. In kew, run `kew deny 2055`.'
    });
  });

  it('reads a write’s body off a Request when the call passes no init', async () => {
    const request = new Request(`${ORIGIN}/api/approvals/2055`, {
      method: 'POST',
      body: JSON.stringify({ action: 'deny', reason: '' })
    });
    const r = await f(request);
    expect(r.status).toBe(405);
    expect(await r.json()).toEqual({
      detail: 'Demo: this would deny the hold on #2055 and park it. In kew, run `kew deny 2055`.'
    });
    // The caller's own request is left unread.
    expect(request.bodyUsed).toBe(false);
  });

  it('opens a stream path and holds it', async () => {
    const r = await f('/api/epics/launch-fixes/stream');
    expect(r.status).toBe(200);
    expect(r.headers.get('Content-Type')).toBe('text/event-stream');
    const reader = r.body!.getReader();
    const first = await reader.read();
    expect(new TextDecoder().decode(first.value)).toMatch(/^: /);
    await reader.cancel();
  });

  it('refuses an aborted read as fetch does', async () => {
    const controller = new AbortController();
    controller.abort();
    await expect(f('/api/meta', { signal: controller.signal })).rejects.toThrow();
  });
});
