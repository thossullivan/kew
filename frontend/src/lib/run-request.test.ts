/**
 * #2537 — Run: which printed commands get a Run button, the one line a block
 * prints about the request it issued, and the follow from queue to settled.
 */
import { render } from 'svelte/server';
import { get } from 'svelte/store';
import { afterEach, describe, expect, it, vi } from 'vitest';
import NeedsYouBlockView from '$lib/needs-you/NeedsYouBlock.svelte';
import type { NeedsYouBlock } from '$lib/needs-you/block';
import {
  FOLLOW_EVERY_MS,
  FOLLOW_PATIENCE,
  FOLLOW_SIGNED_OUT,
  FOLLOW_SLOW_MS,
  RUN_UNREADABLE,
  followRun,
  isRunRequest,
  isRunnable,
  isSubmitting,
  isTerminal,
  lookupRunRequest,
  pairClear,
  pairLedgers,
  pendingRuns,
  resumeRun,
  runButtonLabel,
  runButtonName,
  runLine,
  startRun,
  watchPair
} from '$lib/run-request';
import type { RunRequest } from '$lib/verbs';

const UNHOLD = 'kew epic unhold epic:a 12';

function request(over: Partial<RunRequest> = {}): RunRequest {
  return {
    id: 'r1',
    command: UNHOLD,
    operator: 'tom',
    requested_at: '2026-10-10T12:00:00+00:00',
    source: 'web',
    state: 'queued',
    item_id: '12',
    drive: null,
    host: null,
    pid: null,
    log: null,
    started_at: null,
    finished_at: null,
    exit_code: null,
    detail: null,
    ...over
  };
}

describe('which commands are runnable', () => {
  it.each([UNHOLD, 'kew epic continue epic:a 12', 'kew epic retry go SFD-1', 'kew dispatch 42'])(
    'runs %s',
    (command) => expect(isRunnable(command)).toBe(true)
  );

  it.each([
    'kew epic run epic:a',
    'kew dispatch 42 --force-budget',
    'kew dispatch --help',
    'kew dispatch 1 2',
    'kew dispatch 1;id',
    'kew setup',
    'kew doctor',
    ''
  ])('keeps Copy alone for %s', (command) => expect(isRunnable(command)).toBe(false));
});

describe('the line under the command', () => {
  it('says what the drive did, in its own words', () => {
    expect(runLine(request()).tone).toBe('quiet');
    expect(runLine(request({ state: 'running' })).text).toBe('running as tom');
    expect(runLine(request({ state: 'done', exit_code: 0, detail: 'a\nunheld #12' }))).toEqual({
      tone: 'ok',
      text: 'done — unheld #12'
    });
    expect(runLine(request({ state: 'failed', exit_code: 2, detail: 'not an approver' }))).toEqual({
      tone: 'warn',
      text: 'failed (exit 2) — not an approver'
    });
  });

  it('carries a refusal back to the block that issued the Run', () => {
    const refused = request({ state: 'refused', detail: 'the fleet is stopped (budget breach)' });
    expect(runLine(refused)).toEqual({
      tone: 'warn',
      text: 'refused by the drive — the fleet is stopped (budget breach)'
    });
  });

  it('settles a request the drive fence superseded, naming the run (#2975)', () => {
    const superseded = request({
      command: 'kew dispatch 2060',
      state: 'superseded',
      superseded_by: 'run-7',
      detail: 'superseded by run run-7: the item already had that run in flight'
    });
    expect(isRunRequest(superseded)).toBe(true);
    expect(isTerminal(superseded)).toBe(true);
    expect(runLine(superseded)).toEqual({
      tone: 'warn',
      text: 'not run — superseded by run run-7: the item already had that run in flight'
    });
  });

  it('says why a queued request waits, while it stays queued', () => {
    const waiting = request({ detail: 'waiting: the drive is holding admission for host load' });
    expect(runLine(waiting)).toEqual({
      tone: 'quiet',
      text: 'queued — waiting: the drive is holding admission for host load'
    });
  });
});

function fakeFetch(answers: Array<{ status: number; body: unknown }>) {
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  const f = (async (url: string, init?: RequestInit) => {
    calls.push({ url, init });
    const next = answers.shift() ?? { status: 500, body: {} };
    return new Response(JSON.stringify(next.body), { status: next.status });
  }) as unknown as typeof fetch;
  return { f, calls };
}

async function until(check: () => boolean): Promise<void> {
  for (let i = 0; i < 200 && !check(); i++) await new Promise((r) => setTimeout(r, 1));
  expect(check()).toBe(true);
}

describe('a Run', () => {
  it('queues the command with the item, then follows it until the drive settles it', async () => {
    const { f, calls } = fakeFetch([
      { status: 200, body: { request: request(), created: true, drive: { running: true } } },
      { status: 200, body: request({ state: 'running' }) },
      { status: 200, body: request({ state: 'done', exit_code: 0 }) }
    ]);
    const seen: string[] = [];
    startRun(UNHOLD, '12', (s) => seen.push(s.request?.state ?? s.error), { f, everyMs: 0 });

    await until(() => seen.at(-1) === 'done');

    expect(seen).toEqual(['queued', 'running', 'done']);
    expect(calls[0].url).toBe('/api/run-requests');
    expect(calls[0].init?.method).toBe('POST');
    expect(JSON.parse(String(calls[0].init?.body))).toEqual({
      command: UNHOLD,
      item_id: '12',
      key: expect.stringMatching(/^[0-9a-f]{32}$/)
    });
    expect(calls.slice(1).map((c) => c.url)).toEqual(['/api/run-requests/r1', '/api/run-requests/r1']);
  });

  it('says "no drive running" when there is nothing to pick it up', async () => {
    const detail = 'no drive running (no drive holds the lock) — start `kew drive`, or copy the command and run it';
    // A 409 is checked against the ledger, which answers empty (#2975).
    const { f } = fakeFetch([
      { status: 409, body: { detail } },
      { status: 200, body: { requests: [] } }
    ]);
    const seen: string[] = [];
    startRun(UNHOLD, null, (s) => seen.push(s.error), { f, everyMs: 0 });

    await until(() => seen.length === 1);

    expect(seen[0]).toBe(detail);
  });

  it.each([
    ['an empty body', {}],
    ['a request with no id', { request: { state: 'queued', operator: 'tom' } }],
    ['a request in no known state', { request: request({ state: 'lost' as RunRequest['state'] }) }],
    [
      'a partial request with only id, state and operator',
      { request: { id: 'r', state: 'failed', operator: 'tom' } }
    ],
    ['a request missing a nullable field', { request: { ...request(), exit_code: undefined } }],
    ['a request with a mistyped nullable field', { request: request({ pid: '12' as unknown as number }) }],
    ['a request for another command', { request: request({ command: 'kew epic unhold epic:a 13' }) }],
    ['a request for another model', { request: request({ model: 'gpt-5.6-sol' }) }],
    ['null', null]
  ])('puts a 2xx carrying %s in the error state once the ledger answers empty (#2878, #2975)', async (_what, body) => {
    const { f, calls } = fakeFetch([
      { status: 200, body },
      { status: 200, body: { requests: [] } }
    ]);
    const seen: Array<{ state: string | null; error: string }> = [];
    startRun(UNHOLD, '12', (s) => seen.push({ state: s.request?.state ?? null, error: s.error }), {
      f,
      everyMs: 0
    });

    await until(() => seen.length === 1);
    await new Promise((r) => setTimeout(r, 5));

    expect(seen).toEqual([{ state: null, error: RUN_UNREADABLE }]);
    // One POST, then the ledger lookup that says it queued nothing.
    expect(calls.map((c) => c.init?.method ?? 'GET')).toEqual(['POST', 'GET']);
  });

  it('follows the request for the submitted command and model, in the ledger spelling (#2878)', async () => {
    const accepted = request({ model: 'gpt-5.6-sol', state: 'done', exit_code: 0 });
    const { f } = fakeFetch([{ status: 200, body: { request: accepted, created: true, drive: { running: true } } }]);
    const seen: string[] = [];
    startRun(`  ${UNHOLD.replaceAll(' ', '   ')} `, '12', (s) => seen.push(s.request?.state ?? s.error), {
      f,
      everyMs: 0,
      model: 'gpt-5.6-sol'
    });

    await until(() => seen.length === 1);

    expect(seen).toEqual(['done']);
  });

  it('puts a listener that throws on its first reading in the error state, with no poll left (#2878)', async () => {
    const accepted = request({ id: 'throws' });
    const { f, calls } = fakeFetch([
      { status: 200, body: { request: accepted, created: true, drive: { running: true } } },
      { status: 200, body: accepted }
    ]);
    const seen: string[] = [];
    let first = true;
    startRun(
      UNHOLD,
      '12',
      (s) => {
        seen.push(s.request?.state ?? s.error);
        if (first) {
          first = false;
          throw new Error('the block could not draw it');
        }
      },
      { f, everyMs: 0 }
    );

    await until(() => seen.length === 2);
    await new Promise((r) => setTimeout(r, 5));

    expect(seen).toEqual(['queued', RUN_UNREADABLE]);
    expect(calls).toHaveLength(1);
  });
});

/**
 * #2975 — a submission outlives the button that pressed Run: leaving it never
 * cancels the POST, a later button picks the answer up, and one pair never has
 * two Runs unsettled.
 */
describe('a submission in flight', () => {
  const RETRY = 'kew epic retry epic:b 7';
  const settled = (over: Partial<RunRequest> = {}) =>
    request({ command: RETRY, state: 'done', exit_code: 0, ...over });

  /** A fetch whose POST answers only when `answer` is called. */
  function heldPost() {
    const posts: unknown[] = [];
    let answer: (body: unknown, status?: number) => void = () => {};
    const f = (async (url: string, init?: RequestInit) => {
      if (init?.method !== 'POST')
        return Response.json(url.startsWith('/api/run-requests?') ? { requests: [] } : settled());
      posts.push(JSON.parse(String(init.body)));
      return new Promise<Response>(
        (resolve) => (answer = (body, status = 200) => resolve(Response.json(body, { status })))
      );
    }) as unknown as typeof fetch;
    return { f, posts, answer: (body: unknown, status?: number) => answer(body, status) };
  }

  it('keeps the POST when its button leaves, and hands the answer to the next one', async () => {
    const { f, answer } = heldPost();
    const first: string[] = [];
    const leave = startRun(RETRY, '7', (s) => first.push(s.request?.state ?? s.error), {
      f,
      model: 'gpt-5.6-sol'
    });
    leave();

    expect(isSubmitting(RETRY, 'gpt-5.6-sol')).toBe(true);
    expect(isSubmitting(RETRY, null)).toBe(false);
    expect(get(pendingRuns)).toEqual([{ command: RETRY, model: 'gpt-5.6-sol', itemId: '7' }]);

    answer({ request: settled({ model: 'gpt-5.6-sol' }), created: true });
    await until(() => get(pendingRuns).length === 0);

    // Nobody heard it land; the next button on the pair does.
    const next: string[] = [];
    const resumed = resumeRun(RETRY, 'gpt-5.6-sol', (s) => next.push(s.request?.state ?? s.error), {
      f
    });
    expect(resumed).not.toBeNull();
    expect(next).toEqual(['done']);
    expect(first).toEqual([]);
    // Delivered once: the ledger is the record from here on.
    expect(resumeRun(RETRY, 'gpt-5.6-sol', () => {}, { f })).toBeNull();
  });

  it('sends one POST for a second Run of the same pair while the first is open', async () => {
    const { f, posts, answer } = heldPost();
    const a: string[] = [];
    const b: string[] = [];
    startRun(RETRY, '7', (s) => a.push(s.request?.state ?? s.error), { f });
    startRun(`  ${RETRY} `, '7', (s) => b.push(s.request?.state ?? s.error), { f });
    await new Promise((r) => setTimeout(r, 5));

    expect(posts).toHaveLength(1);

    answer({ request: settled(), created: true });
    await until(() => a.length === 1 && b.length === 1);

    expect([a, b]).toEqual([['done'], ['done']]);
    expect(isSubmitting(RETRY)).toBe(false);
  });

  it('keeps a refused POST for the button that mounts after it landed', async () => {
    const { f, answer } = heldPost();
    startRun(RETRY, '7', () => {}, { f, model: 'gpt-5.6-luna' })();
    answer({ detail: 'no drive running' }, 409);
    await until(() => !isSubmitting(RETRY, 'gpt-5.6-luna'));

    const seen: string[] = [];
    resumeRun(RETRY, 'gpt-5.6-luna', (s) => seen.push(s.error), { f });
    expect(seen).toEqual(['no drive running']);
  });
});

/**
 * #2975 — a POST whose answer does not say what happened (dropped, a 5xx, a
 * 503 while the press's first attempt still decides) may still have queued the
 * Run, or be about to. It is asked again with the same key — the server gives
 * every attempt the first one's answer — and the pair stays held until a
 * request or a refusal comes back. A 2xx naming no request is a finished
 * handler: it is reconciled against the ledger, held until a lookup answers.
 */
describe('a POST with no clear answer', () => {
  const CONTINUE = 'kew epic continue epic:c 9';
  const live = (over: Partial<RunRequest> = {}) =>
    request({ id: 'committed', command: CONTINUE, item_id: '9', ...over });
  const keysOf = (calls: Array<{ init?: RequestInit }>) =>
    calls.filter((c) => c.init?.method === 'POST').map((c) => JSON.parse(String(c.init?.body)).key);

  it('reconciles a 2xx naming no request against the ledger, and follows the request it finds', async () => {
    const { f, calls } = fakeFetch([
      { status: 200, body: {} },
      { status: 200, body: { requests: [live()] } },
      { status: 200, body: live({ state: 'done', exit_code: 0 }) }
    ]);
    const seen: string[] = [];
    startRun(CONTINUE, '9', (s) => seen.push(s.request?.state ?? s.error), { f, everyMs: 0 });

    await until(() => seen.at(-1) === 'done');

    expect(seen).toEqual(['queued', 'done']);
    expect(calls.map((c) => c.init?.method ?? 'GET')).toEqual(['POST', 'GET', 'GET']);
    expect(calls[1].url.startsWith('/api/run-requests?')).toBe(true);
    expect(calls.at(-1)?.url).toBe('/api/run-requests/committed');
  });

  it('gives Run back after a 2xx naming no request once the ledger answers empty', async () => {
    const { f, calls } = fakeFetch([
      { status: 200, body: {} },
      { status: 500, body: {} },
      { status: 200, body: { requests: [] } }
    ]);
    const seen: string[] = [];
    startRun(CONTINUE, '9', (s) => seen.push(s.request?.state ?? s.error), { f, everyMs: 0 });

    await until(() => seen.length === 1);

    // The failed lookup did not free the pair: it was asked again first.
    expect(seen).toEqual([RUN_UNREADABLE]);
    expect(calls.map((c) => c.init?.method ?? 'GET')).toEqual(['POST', 'GET', 'GET']);
    expect(isSubmitting(CONTINUE)).toBe(false);
  });

  it('holds the pair while the answer is lost or the first attempt still decides', async () => {
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    let decided = false;
    const answers = [
      () => Promise.reject(new TypeError('Failed to fetch')),
      () => Response.json({ detail: 'this Run is still being queued — ask again' }, { status: 503 }),
      () => new Response('', { status: 502 })
    ];
    const f = (async (url: string, init?: RequestInit) => {
      calls.push({ url, init });
      if (init?.method !== 'POST') return Response.json(live({ model: 'gpt-5.6-sol', state: 'done', exit_code: 0 }));
      const next = answers.shift();
      if (next !== undefined) return next();
      if (!decided) return new Response('', { status: 502 });
      return Response.json({ request: live({ model: 'gpt-5.6-sol' }), created: false });
    }) as unknown as typeof fetch;
    const seen: string[] = [];
    startRun(CONTINUE, '9', (s) => seen.push(s.request?.state ?? s.error), {
      f,
      everyMs: 1,
      model: 'gpt-5.6-sol'
    });
    await until(() => answers.length === 0 && calls.length >= 4);

    // Still unknown: Run stays sent, and a second press joins the same press.
    expect(seen).toEqual([]);
    expect(isSubmitting(CONTINUE, 'gpt-5.6-sol')).toBe(true);
    expect(get(pendingRuns)).toEqual([{ command: CONTINUE, model: 'gpt-5.6-sol', itemId: '9' }]);
    startRun(CONTINUE, '9', () => {}, { f, everyMs: 1, model: 'gpt-5.6-sol' });

    decided = true;
    await until(() => seen.at(-1) === 'done');

    expect(seen).toEqual(['queued', 'done']);
    expect(new Set(keysOf(calls)).size).toBe(1);
    expect(isSubmitting(CONTINUE, 'gpt-5.6-sol')).toBe(false);
  });

  it('shows what the request became when it settled before the answer was asked again', async () => {
    const { f } = fakeFetch([
      { status: 502, body: {} },
      { status: 200, body: { request: live({ state: 'failed', exit_code: 3, detail: 'boom' }), created: false } }
    ]);
    const seen: Array<{ state: string | null; error: string }> = [];
    startRun(CONTINUE, '9', (s) => seen.push({ state: s.request?.state ?? null, error: s.error }), {
      f,
      everyMs: 0
    });

    await until(() => seen.length === 1);

    expect(seen).toEqual([{ state: 'failed', error: '' }]);
  });

  it("takes a refusal as the press's answer, on its first attempt or a later one", async () => {
    const { f, calls } = fakeFetch([
      { status: 502, body: {} },
      { status: 409, body: { detail: 'no drive running (no drive holds the lock)' } },
      { status: 200, body: { requests: [] } }
    ]);
    const seen: string[] = [];
    startRun(CONTINUE, '9', (s) => seen.push(s.error), { f, everyMs: 0 });

    await until(() => seen.length === 1);
    await new Promise((r) => setTimeout(r, 5));

    expect(seen).toEqual(['no drive running (no drive holds the lock)']);
    // A 409 is checked against the ledger before it is the answer (#2975).
    expect(calls.map((c) => c.init?.method ?? 'GET')).toEqual(['POST', 'POST', 'GET']);
    expect(isSubmitting(CONTINUE)).toBe(false);
  });

  it("follows the ledger's request when the pair is already live under another press (#2975)", async () => {
    const { f, calls } = fakeFetch([
      { status: 409, body: { detail: 'kew epic continue epic:a 9 is already queued' } },
      { status: 200, body: { requests: [live({ id: 'theirs' })] } },
      { status: 200, body: live({ id: 'theirs', state: 'done', exit_code: 0 }) }
    ]);
    const seen: Array<{ id: string | null; state: string | null; error: string }> = [];
    startRun(
      CONTINUE,
      '9',
      (s) => seen.push({ id: s.request?.id ?? null, state: s.request?.state ?? null, error: s.error }),
      { f, everyMs: 0 }
    );

    await until(() => seen.at(-1)?.state === 'done');

    expect(seen[0]).toEqual({ id: 'theirs', state: 'queued', error: '' });
    expect(calls.filter((c) => c.init?.method === 'POST')).toHaveLength(1);
    expect(isSubmitting(CONTINUE)).toBe(false);
  });

  it('keeps a 409 open while its ledger lookup fails, and asks the ledger again (#2975)', async () => {
    const { f, calls } = fakeFetch([
      { status: 409, body: { detail: 'no drive running (no drive holds the lock)' } },
      { status: 503, body: {} },
      { status: 200, body: { requests: null } },
      { status: 200, body: { requests: [] } }
    ]);
    const seen: string[] = [];
    startRun(CONTINUE, '9', (s) => seen.push(s.error), { f, everyMs: 0 });

    await until(() => calls.length >= 2);
    expect(isSubmitting(CONTINUE)).toBe(true);
    await until(() => seen.length === 1);

    expect(seen).toEqual(['no drive running (no drive holds the lock)']);
    expect(calls.map((c) => c.init?.method ?? 'GET')).toEqual(['POST', 'GET', 'GET', 'GET']);
    expect(isSubmitting(CONTINUE)).toBe(false);
  });

  it('shows a press whose queued fact could not be written, and asks no more', async () => {
    const withdrawn = 'its queued fact could not be written, so nothing was queued; press Run again';
    const { f, calls } = fakeFetch([{ status: 424, body: { detail: withdrawn } }]);
    const seen: string[] = [];
    startRun(CONTINUE, '9', (s) => seen.push(s.error), { f, everyMs: 0 });

    await until(() => seen.length === 1);
    await new Promise((r) => setTimeout(r, 5));

    expect(seen).toEqual([withdrawn]);
    expect(calls).toHaveLength(1);
    expect(isSubmitting(CONTINUE)).toBe(false);
  });
});

/**
 * #2789 (K3) — the follow is a poll, and an unwatched poll is a cost: it slows
 * down when nothing moves, it stops when the session is gone, it asks nothing
 * while the tab is hidden, and two blocks showing one request share one poll.
 */
describe('following a request', () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  /** A fetch that answers every read of the request with the next reading, and
   * then with the last one for good — a drive sitting on a queued request. */
  function ledger(readings: Array<{ status: number; body: unknown }>) {
    const urls: string[] = [];
    const f = (async (url: string) => {
      urls.push(url);
      const next = readings.length > 1 ? readings.shift()! : readings[0];
      return new Response(JSON.stringify(next.body), { status: next.status });
    }) as unknown as typeof fetch;
    return { f, urls };
  }

  it('backs off to the slow beat after unchanged reads, and speeds up when one moves', async () => {
    vi.useFakeTimers();
    const queued = request({ id: 'backoff' });
    const { f, urls } = ledger([{ status: 200, body: queued }]);
    const stop = followRun(queued, () => {}, { f });

    // The first FOLLOW_PATIENCE reads come at the fast beat.
    await vi.advanceTimersByTimeAsync(FOLLOW_EVERY_MS * FOLLOW_PATIENCE);
    expect(urls).toHaveLength(FOLLOW_PATIENCE);
    // Then nothing for most of the slow beat, however many fast beats pass.
    await vi.advanceTimersByTimeAsync(FOLLOW_SLOW_MS - 1);
    expect(urls).toHaveLength(FOLLOW_PATIENCE);
    await vi.advanceTimersByTimeAsync(1);
    expect(urls).toHaveLength(FOLLOW_PATIENCE + 1);
    stop();
    await vi.advanceTimersByTimeAsync(FOLLOW_SLOW_MS * 4);
    expect(urls).toHaveLength(FOLLOW_PATIENCE + 1);
  });

  it('goes back to the fast beat when the drive moves the request', async () => {
    vi.useFakeTimers();
    const queued = request({ id: 'moves' });
    const answers = Array.from({ length: FOLLOW_PATIENCE }, () => ({ status: 200, body: queued }));
    const running = request({ id: 'moves', state: 'running' });
    const { f, urls } = ledger([...answers, { status: 200, body: running }]);
    const seen: string[] = [];
    const stop = followRun(queued, (s) => seen.push(s.request?.state ?? s.error), { f });

    await vi.advanceTimersByTimeAsync(FOLLOW_EVERY_MS * FOLLOW_PATIENCE + FOLLOW_SLOW_MS);
    expect(seen.at(-1)).toBe('running');
    const read = urls.length;
    await vi.advanceTimersByTimeAsync(FOLLOW_EVERY_MS);
    expect(urls).toHaveLength(read + 1);
    stop();
  });

  it('stops on a 401 and says the session is gone', async () => {
    vi.useFakeTimers();
    const queued = request({ id: 'lapsed' });
    const { f, urls } = ledger([{ status: 401, body: { detail: 'unauthorized' } }]);
    const seen: Array<{ state: string | null; error: string }> = [];
    followRun(queued, (s) => seen.push({ state: s.request?.state ?? null, error: s.error }), { f });

    await vi.advanceTimersByTimeAsync(FOLLOW_EVERY_MS);
    expect(urls).toEqual(['/api/run-requests/lapsed']);
    expect(seen.at(-1)).toEqual({ state: null, error: FOLLOW_SIGNED_OUT });
    await vi.advanceTimersByTimeAsync(FOLLOW_SLOW_MS * 4);
    expect(urls).toHaveLength(1);
  });

  it('keeps following through a failed read that is not a 401', async () => {
    vi.useFakeTimers();
    const queued = request({ id: 'blip' });
    const done = request({ id: 'blip', state: 'done', exit_code: 0 });
    const { f } = ledger([
      { status: 503, body: {} },
      { status: 200, body: done }
    ]);
    const seen: string[] = [];
    followRun(queued, (s) => seen.push(s.request?.state ?? s.error), { f });

    await vi.advanceTimersByTimeAsync(FOLLOW_EVERY_MS * 2);
    expect(seen).toEqual(['queued', 'queued', 'done']);
  });

  it('asks nothing while the tab is hidden, and reads at once when it is shown', async () => {
    vi.useFakeTimers();
    const doc = new EventTarget() as EventTarget & { hidden: boolean };
    doc.hidden = true;
    vi.stubGlobal('document', doc);
    const queued = request({ id: 'hidden' });
    const { f, urls } = ledger([{ status: 200, body: queued }]);
    const stop = followRun(queued, () => {}, { f });

    await vi.advanceTimersByTimeAsync(FOLLOW_SLOW_MS * 2);
    expect(urls).toHaveLength(0);
    doc.hidden = false;
    doc.dispatchEvent(new Event('visibilitychange'));
    await vi.advanceTimersByTimeAsync(0);
    expect(urls).toHaveLength(1);
    stop();
  });

  it('shares one poll between two blocks following the same request', async () => {
    vi.useFakeTimers();
    const queued = request({ id: 'shared' });
    const running = request({ id: 'shared', state: 'running' });
    const { f, urls } = ledger([{ status: 200, body: running }]);
    const first: string[] = [];
    const second: string[] = [];
    const leaveFirst = followRun(queued, (s) => first.push(s.request?.state ?? s.error), { f });
    const leaveSecond = followRun(queued, (s) => second.push(s.request?.state ?? s.error), { f });

    await vi.advanceTimersByTimeAsync(FOLLOW_EVERY_MS);
    expect(urls).toHaveLength(1);
    expect(first).toEqual(['queued', 'running']);
    expect(second).toEqual(['queued', 'running']);
    // One block leaving keeps the other's follow; the last one out stops it.
    leaveFirst();
    await vi.advanceTimersByTimeAsync(FOLLOW_EVERY_MS);
    expect(urls).toHaveLength(2);
    leaveSecond();
    await vi.advanceTimersByTimeAsync(FOLLOW_SLOW_MS);
    expect(urls).toHaveLength(2);
  });
});

describe('the needs-you block', () => {
  function block(command: string): NeedsYouBlock {
    return {
      label: 'Held',
      summary: 'held',
      why: '',
      since: '',
      command,
      note: '',
      links: [],
      external: []
    };
  }
  const draw = (b: NeedsYouBlock) =>
    render(NeedsYouBlockView, { props: { block: b, itemId: '12' } }).body;

  it('offers Run beside Copy for a command the drive may run', () => {
    const html = draw(block(UNHOLD));
    expect(html).toContain(`aria-label="Run ${UNHOLD}"`);
    expect(html).toContain('aria-label="Copy command (c)"');
  });

  it('keeps Copy alone for a command the drive will not run', () => {
    const html = draw(block('kew epic run epic:a'));
    expect(html).not.toContain('data-run-button');
    expect(html).toContain('aria-label="Copy command (c)"');
  });
});

describe('runButtonLabel (#2878)', () => {
  it('reads Sending… until the POST answers: no request is in the ledger yet', () => {
    expect(runButtonLabel('sending')).toBe('Sending…');
  });

  it('reads Queued… until the drive starts the request, Running… once it has', () => {
    expect(runButtonLabel('queued')).toBe('Queued…');
    expect(runButtonLabel('running')).toBe('Running…');
  });

  it('is the idle Run with nothing in flight', () => {
    expect(runButtonLabel(null)).toBe('Run');
    expect(runButtonLabel('done')).toBe('Run');
    expect(runButtonLabel('refused')).toBe('Run');
  });
});

describe('runButtonName (#2878)', () => {
  it("names the button's state and command, as its label reads", () => {
    expect(runButtonName(null, 'kew dispatch 2060')).toBe('Run kew dispatch 2060');
    expect(runButtonName('sending', 'kew dispatch 2060')).toBe('Sending kew dispatch 2060');
    expect(runButtonName('queued', 'kew dispatch 2060')).toBe('Queued kew dispatch 2060');
    expect(runButtonName('running', '  kew  dispatch 2060')).toBe('Running kew dispatch 2060');
  });
});

describe('the latest request for a command and model (#2878)', () => {
  const DISPATCH = 'kew dispatch 2060';
  const sol = request({ id: 'sol', command: DISPATCH, model: 'gpt-5.6-sol' });
  const plain = request({ id: 'plain', command: DISPATCH, state: 'done', model: null });

  it("does not hand one model's queued request to another model's Run", async () => {
    const { f } = fakeFetch([{ status: 200, body: { requests: [sol, plain] } }]);
    expect(await lookupRunRequest(DISPATCH, 'gpt-5.6-luna', f)).toEqual({ kind: 'empty' });
  });

  it('skips a row that is not a request', async () => {
    const { f } = fakeFetch([{ status: 200, body: { requests: [{ model: 'gpt-5.6-sol' }, sol] } }]);
    expect(await lookupRunRequest(DISPATCH, 'gpt-5.6-sol', f)).toEqual({ kind: 'found', request: sol });
  });

  it("finds the chosen model's request behind a newer one on another model", async () => {
    const { f, calls } = fakeFetch([{ status: 200, body: { requests: [plain, sol] } }]);
    expect(await lookupRunRequest(DISPATCH, 'gpt-5.6-sol', f)).toEqual({ kind: 'found', request: sol });
    const asked = new URL(calls[0].url, 'http://x').searchParams;
    expect([asked.get('command'), asked.get('model')]).toEqual([DISPATCH, 'gpt-5.6-sol']);
  });

  it('matches no model (null or an empty pick) to a request sent without one', async () => {
    const { f, calls } = fakeFetch([
      { status: 200, body: { requests: [sol, plain] } },
      { status: 200, body: { requests: [sol, plain] } }
    ]);
    expect(await lookupRunRequest(DISPATCH, null, f)).toEqual({ kind: 'found', request: plain });
    expect(await lookupRunRequest(DISPATCH, '', f)).toEqual({ kind: 'found', request: plain });
    // The server reads an empty model as "sent without one", before its limit.
    for (const call of calls) {
      expect(new URL(call.url, 'http://x').searchParams.get('model')).toBe('');
    }
  });
});

describe('a ledger lookup has three results (#2975)', () => {
  const DISPATCH = 'kew dispatch 2060';

  it.each([
    ['a 5xx', { status: 500, body: { detail: 'locked' } }],
    ['a 2xx that is not a list of requests', { status: 200, body: { requests: null } }],
    ['a 2xx with no body', { status: 200, body: null }]
  ])('reads %s as failed, never as an empty ledger', async (_name, answer) => {
    const { f } = fakeFetch([answer]);
    expect(await lookupRunRequest(DISPATCH, null, f)).toEqual({ kind: 'failed' });
  });

  it('reads a body that is not JSON, or a dropped connection, as failed', async () => {
    const garbled = (async () => new Response('<html>', { status: 200 })) as unknown as typeof fetch;
    const dropped = (async () => {
      throw new TypeError('network');
    }) as unknown as typeof fetch;
    expect(await lookupRunRequest(DISPATCH, null, garbled)).toEqual({ kind: 'failed' });
    expect(await lookupRunRequest(DISPATCH, null, dropped)).toEqual({ kind: 'failed' });
  });
});

describe("a pair's ledger (#2975)", () => {
  const DISPATCH = 'kew dispatch 2061';
  const clear = () => pairClear(get(pairLedgers), DISPATCH, null);

  it('stays blocked through a failed lookup and asks again until the ledger answers', async () => {
    const { f, calls } = fakeFetch([
      { status: 500, body: {} },
      { status: 200, body: { requests: null } },
      { status: 200, body: { requests: [] } }
    ]);
    const found: Array<RunRequest | null> = [];
    const leave = watchPair(DISPATCH, null, (r) => found.push(r), { f, everyMs: 0 });
    expect(clear()).toBe(false);

    await until(() => found.length === 1);
    expect(calls).toHaveLength(3);
    expect(found).toEqual([null]);
    expect(clear()).toBe(true);
    leave();
    expect(clear()).toBe(false);
  });

  it('holds while the request it found is unsettled, and frees once the drive settles it', async () => {
    const queued = request({ id: 'q1', command: DISPATCH, item_id: '2061', model: null });
    const { f } = fakeFetch([
      { status: 200, body: { requests: [queued] } },
      { status: 200, body: { ...queued, state: 'done', exit_code: 0 } }
    ]);
    const leave = watchPair(DISPATCH, null, null, { f, everyMs: 0 });

    const held = () => get(pairLedgers).get(JSON.stringify([DISPATCH, null])) === 'held';
    await until(held);
    await until(clear);
    leave();
  });
});
