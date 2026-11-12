/**
 * The demo console's `/api`, answered in the browser (#3139).
 *
 * The same answers the dev fixture plugin (`fixtures/plugin.ts`) gives, out of
 * a bundled fixture set instead of the disk, through the same pure helpers
 * (`fixtures/core.ts`): a read maps to its fixture file mechanically, a keyed
 * read answers out of its item's own file, the audit list, its histogram and a
 * run's audit page are computed for the query they ask, a path with no fixture
 * gets the plugin's 404 body, and a stream path opens and stays quiet. Every write answers 405
 * with the sentence `writes.ts` composes for it.
 *
 * The clock is rebased once, when the shim is installed: every instant in the
 * set moves by the one delta `now - epoch`, which is what the dev plugin does
 * per request, read at load.
 */
import {
  auditHistogramPage,
  auditListPage,
  countedEvents,
  fixtureFileFor,
  isRunAuditFile,
  isStreamPath,
  keyedFixtureFileFor,
  ownsPath,
  rebaseClock,
  runAuditPage,
  type Background,
  type Counted,
  type Listed
} from '../../../fixtures/core';
import { demoRefusal } from './writes';

/** A bundled fixture set: payloads by path relative to the set, its epoch, and its folder. */
export type FixtureSet = { files: Record<string, unknown>; epoch: string; source: string };

/** One answer: a status and a JSON body, or the quiet stream an SSE path opens. */
export type DemoAnswer = { status: number; body: unknown } | { status: 200; stream: true };

/** The demo's `/api`, over one set read as of one instant. */
export type DemoApi = {
  answer(method: string, url: string, body?: unknown): DemoAnswer;
};

/** The set as if it had been read at `nowMs`: every instant moved by one delta. */
export function rebasedSet(set: FixtureSet, nowMs: number): FixtureSet {
  return { ...set, files: rebaseClock(set.files, nowMs, set.epoch) as Record<string, unknown> };
}

/**
 * An epic's change strip, `/api/epics/<ref>/tick-strip?changes=true`: the
 * server answers it with the attempts that journaled a change, not the newest
 * attempts, so a set that holds `epics-<ref>-tick-strip-changes.json` answers
 * it from that file. Null for any other read, or a set with no such file.
 */
function changeStripFile(plain: string | null, url: string): string | null {
  if (plain === null || !/^epics-.+-tick-strip\.json$/.test(plain)) return null;
  const query = new URLSearchParams(url.split('#')[0].split('?')[1] ?? '');
  return query.get('changes') === 'true' ? plain.replace(/\.json$/, '-changes.json') : null;
}

/** The demo's answers over `set`, which the caller has already rebased. */
export function demoApi(set: FixtureSet): DemoApi {
  let counted: Counted[] | null = null;
  const listed = (): Listed[] => (set.files['audit.json'] as { events?: Listed[] } | undefined)?.events ?? [];
  const allCounted = (): Counted[] =>
    (counted ??= countedEvents(
      listed(),
      (set.files['audit-histogram.json'] as Background | undefined) ?? { buckets: [] }
    ));

  function read(url: string): DemoAnswer {
    if (isStreamPath(url)) return { status: 200, stream: true };
    const plain = fixtureFileFor(url);
    if (plain === 'audit-histogram.json') {
      const histogram = auditHistogramPage(allCounted(), url);
      if (histogram !== null) return histogram;
    }
    if (plain === 'audit.json') return auditListPage(listed(), url);
    // A keyed read that names its item answers out of that item's own file, or
    // 404s: never out of another item's.
    const changes = changeStripFile(plain, url);
    const file = keyedFixtureFileFor(url) ?? (changes !== null && Object.hasOwn(set.files, changes) ? changes : plain);
    if (file === null) return { status: 404, body: { detail: `${url} is not a fixture-able /api read` } };
    if (!Object.hasOwn(set.files, file))
      return { status: 404, body: { detail: `no fixture for ${url}`, expected: `${set.source}${file}` } };
    // A run's audit pages forward from the cursor it is asked with (C11), so a
    // live run's poll returns only what it has not shown.
    if (isRunAuditFile(file)) return runAuditPage(set.files[file] as Parameters<typeof runAuditPage>[0], url);
    return { status: 200, body: set.files[file] };
  }

  return {
    answer(method, url, body) {
      const verb = method.toUpperCase();
      if (verb === 'GET' || verb === 'HEAD') return read(url);
      const path = url.split(/[?#]/)[0].replace(/^\/api\/?/, '');
      return { status: 405, body: { detail: demoRefusal({ method: verb, path, body }) } };
    }
  };
}

/**
 * The body a write sent, as JSON when it is JSON: `init.body` when the call
 * passed one, else the body of the `Request` it passed — read off a clone, so
 * the caller's request is left unread.
 */
async function sentBody(input: RequestInfo | URL, init: RequestInit | undefined): Promise<unknown> {
  let text: string | undefined;
  if (init?.body != null) text = typeof init.body === 'string' ? init.body : undefined;
  else if (input instanceof Request && input.body !== null) text = await input.clone().text();
  if (text === undefined) return undefined;
  try {
    return JSON.parse(text);
  } catch {
    return undefined;
  }
}

/** The bytes a quiet stream opens with, then holds: one comment frame, never closed. */
function quietStream(): ReadableStream<Uint8Array> {
  return new ReadableStream({
    start(controller) {
      controller.enqueue(new TextEncoder().encode(': kew demo — no live frames\n\n'));
    }
  });
}

function respond(answer: DemoAnswer, method: string): Response {
  if ('stream' in answer) {
    return new Response(method === 'HEAD' ? null : quietStream(), {
      status: 200,
      headers: { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-store' }
    });
  }
  return new Response(method === 'HEAD' ? null : JSON.stringify(answer.body), {
    status: answer.status,
    headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' }
  });
}

/**
 * A `fetch` that answers this origin's `/api` from `api` and hands every other
 * request — the app's own assets — to `passThrough` untouched.
 */
export function demoFetch(api: DemoApi, origin: string, passThrough: typeof fetch): typeof fetch {
  return async (input, init) => {
    const href = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
    const url = new URL(href, origin);
    if (url.origin !== new URL(origin).origin || !ownsPath(url.pathname)) return passThrough(input, init);
    const signal = init?.signal ?? (input instanceof Request ? input.signal : undefined);
    if (signal?.aborted) throw signal.reason ?? new DOMException('Aborted', 'AbortError');
    const method = (init?.method ?? (input instanceof Request ? input.method : 'GET')).toUpperCase();
    const body = method === 'GET' || method === 'HEAD' ? undefined : await sentBody(input, init);
    return respond(api.answer(method, `${url.pathname}${url.search}`, body), method);
  };
}
