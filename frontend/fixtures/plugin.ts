/**
 * FUI-02 — the dev-only fixture API behind `KEW_UI_FIXTURES=1`.
 *
 * `npm run dev:fixtures` starts Vite with this plugin, which answers every
 * `/api/*` read out of `fixtures/factory/*.json` instead of proxying to a live
 * `kew serve`. The fixtures reproduce the data the direction-D artboards in
 * `design/factory-ui/` draw, so "matches the artboard" is a check anyone can
 * repeat: `npm run shoot -- <route> <artboard>` photographs the app and the
 * reference side by side against the same numbers.
 *
 * Three properties keep this out of production:
 *   1. the factory returns `false` unless `KEW_UI_FIXTURES=1`, so an ordinary
 *      `vite dev` / `vite build` never even constructs the plugin;
 *   2. `apply: 'serve'` means it cannot attach to a build;
 *   3. the JSON lives in `fixtures/`, outside both `src/` and `static/`, so
 *      nothing imports it and SvelteKit never copies it.
 * `src/lib/fixtures.test.ts` pins all three.
 *
 * Route mapping is mechanical, not a table: the path after `/api/` becomes the
 * file name with `/` turned into `-`. `/api/report/spend` reads
 * `report-spend.json`; `/api/epics/launch-fixes/nodes/2055` reads
 * `epics-launch-fixes-nodes-2055.json`, and a `:` in a segment reads as `_`.
 * A path that names no fixture gets a 404 that says which file it looked for,
 * so adding one is obvious. The one exception is a read answered per item
 * (`keyedFixtureFileFor`): `/api/dispatch/resolve?item=2060` reads
 * `dispatch-resolve/2060.json`.
 *
 * One transformation happens on the way out — `rebaseClock` — because a
 * committed instant ages and some of this console's readings are GATED on age.
 * It moves every instant in the set by ONE delta, so a surface that composes
 * several of these files still reads them against each other as committed.
 * The reads answered rather than looked up are the audit list and its
 * histogram, which are filtered and counted for the window they ask about
 * (`auditListBody`, `auditHistogramBody`), and a run's audit page, which
 * pages forward from the cursor it asks with (`runAuditPage`).
 *
 * Everything that does not read a file — the mapping, the clock, the audit
 * computations — lives in `core.ts` (#3139), which the demo build's shim also
 * imports; this module re-exports all of it, so its own importers see one API.
 */
import { existsSync, readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import type { Connect, Plugin } from 'vite';
import {
  auditHistogramPage,
  auditListPage,
  countedEvents,
  fixtureBody as fixtureBodyAt,
  fixtureFileFor,
  isRunAuditFile,
  isStreamPath,
  keyedFixtureFileFor,
  ownsPath,
  rebaseClock as rebaseClockAt,
  runAuditPage,
  type Background,
  type Listed
} from './core';

export {
  auditHistogramPage,
  auditListPage,
  countedEvents,
  fixtureFileFor,
  INSTANT,
  isStreamPath,
  keyedFixtureFileFor,
  ownsPath,
  type Background,
  type Counted,
  type Listed
} from './core';

/** Committed fixture payloads, one file per read endpoint. */
export const FIXTURE_DIR = new URL('./factory/', import.meta.url);

/** The env var that arms the middleware. Anything but "1" leaves it off. */
export const FIXTURE_ENV = 'KEW_UI_FIXTURES';

/**
 * The instant the committed fixture set is read AS: the newest instant anywhere
 * under `fixtures/factory/`. Serving maps it to now, so the freshest reading in
 * the set is as fresh as the clock allows and nothing lands in the future.
 * `src/lib/fixtures.test.ts` pins it to the set's real maximum, so a fixture
 * committed with a newer instant fails until this is bumped with it.
 */
export const FIXTURE_EPOCH = '2026-10-03T14:48:00+00:00';

/**
 * The payload as if the whole fixture set had been read NOW: every instant
 * moved by the one delta `now - epoch` (`core.ts` says why). `epoch` is
 * `FIXTURE_EPOCH` unless the caller serves another set, which carries its own.
 */
export function rebaseClock(payload: unknown, nowMs: number, epoch: string = FIXTURE_EPOCH): unknown {
  return rebaseClockAt(payload, nowMs, epoch);
}

/**
 * The bytes a fixture file is answered with: its own, unless it holds an
 * instant — in which case the rebased payload, re-serialised.
 */
export function fixtureBody(
  raw: string,
  nowMs: number = Date.now(),
  epoch: string = FIXTURE_EPOCH
): string {
  return fixtureBodyAt(raw, nowMs, epoch);
}

/** The rebased contents of one committed fixture file. */
function served(file: string, nowMs: number): unknown {
  return JSON.parse(fixtureBody(readFileSync(new URL(file, FIXTURE_DIR), 'utf8'), nowMs));
}

/** The events the committed list holds, rebased. */
function listedEvents(nowMs: number): Listed[] {
  return (served('audit.json', nowMs) as { events: Listed[] }).events;
}

/**
 * `/api/audit/histogram`, answered for the window it asks about (#2847), over
 * the committed list and histogram as read now (`auditHistogramPage`). A read
 * with no window is not one the console makes; it gets null, and the
 * middleware serves the committed file, rebased like any other.
 */
export function auditHistogramBody(
  url: string,
  nowMs: number = Date.now()
): { status: number; body: unknown } | null {
  const background = served('audit-histogram.json', nowMs) as Background;
  return auditHistogramPage(countedEvents(listedEvents(nowMs), background), url);
}

/** `/api/audit`, answered for the query it asks (#2875): `auditListPage` over
 * the committed list as read now. */
export function auditListBody(
  url: string,
  nowMs: number = Date.now()
): { status: number; body: unknown } {
  return auditListPage(listedEvents(nowMs), url);
}

function sendJSON(res: Parameters<Connect.NextHandleFunction>[1], code: number, body: unknown) {
  const text = JSON.stringify(body);
  res.statusCode = code;
  res.setHeader('Content-Type', 'application/json');
  res.setHeader('Cache-Control', 'no-store');
  res.end(text);
}

/**
 * The fixture middleware. It answers `/api` requests and passes every other
 * request to `next()` untouched, so Vite and SvelteKit still serve the app.
 * Inside `/api` it always ends the response: with the proxy dropped in
 * fixture mode there is nothing behind it that could answer instead.
 */
export function fixtureMiddleware(
  req: Connect.IncomingMessage,
  res: Parameters<Connect.NextHandleFunction>[1],
  next: Connect.NextFunction
): void {
  const url = req.url ?? '';
  if (!ownsPath(url)) {
    next();
    return;
  }
  const method = (req.method ?? 'GET').toUpperCase();
  if (method !== 'GET' && method !== 'HEAD') {
    // Fixture mode is a read-only mirror: a write would have no world to move.
    sendJSON(res, 405, { detail: `${method} ${url} is not served in fixture mode` });
    return;
  }
  if (isStreamPath(url)) {
    res.statusCode = 200;
    res.setHeader('Content-Type', 'text/event-stream');
    res.setHeader('Cache-Control', 'no-store');
    if (method === 'HEAD') {
      // A HEAD has no body, so there is no stream to hold open.
      res.end();
      return;
    }
    // Hold the connection open with one comment frame. Closing it would make
    // EventSource reconnect in a loop and never settle for a screenshot.
    res.write(': kew fixture mode — no live frames\n\n');
    req.on('close', () => res.end());
    return;
  }
  if (fixtureFileFor(url) === 'audit-histogram.json') {
    const histogram = auditHistogramBody(url);
    if (histogram !== null) {
      res.statusCode = histogram.status;
      res.setHeader('Content-Type', 'application/json');
      res.setHeader('Cache-Control', 'no-store');
      res.end(method === 'HEAD' ? undefined : JSON.stringify(histogram.body));
      return;
    }
  }
  if (fixtureFileFor(url) === 'audit.json') {
    const list = auditListBody(url);
    res.statusCode = list.status;
    res.setHeader('Content-Type', 'application/json');
    res.setHeader('Cache-Control', 'no-store');
    res.end(method === 'HEAD' ? undefined : JSON.stringify(list.body));
    return;
  }
  // A keyed read that names its item answers out of that item's own file, or
  // 404s: never out of another item's.
  const file = keyedFixtureFileFor(url) ?? fixtureFileFor(url);
  if (file === null) {
    sendJSON(res, 404, { detail: `${url} is not a fixture-able /api read` });
    return;
  }
  const path = fileURLToPath(new URL(file, FIXTURE_DIR));
  if (!existsSync(path)) {
    sendJSON(res, 404, {
      detail: `no fixture for ${url}`,
      expected: `frontend/fixtures/factory/${file}`
    });
    return;
  }
  if (isRunAuditFile(file)) {
    // A run's audit pages forward from the cursor it is asked with (C11).
    const page = runAuditPage(JSON.parse(fixtureBody(readFileSync(path, 'utf8'))), url);
    sendJSON(res, page.status, method === 'HEAD' ? undefined : page.body);
    return;
  }
  res.statusCode = 200;
  res.setHeader('Content-Type', 'application/json');
  res.setHeader('Cache-Control', 'no-store');
  res.end(method === 'HEAD' ? undefined : fixtureBody(readFileSync(path, 'utf8')));
}

/**
 * The plugin, or `false` when the env var is absent — Vite drops a falsy
 * plugin, so the disabled case adds nothing to the pipeline at all.
 */
export function fixtureApi(env: NodeJS.ProcessEnv = process.env): Plugin | false {
  if (env[FIXTURE_ENV] !== '1') return false;
  return {
    name: 'kew-fixture-api',
    apply: 'serve',
    configureServer(server) {
      // Registered from `configureServer`, so it runs ahead of Vite's own
      // middlewares. It answers /api and calls next() for everything else, so
      // Vite and SvelteKit still serve the app the harness photographs.
      // `vite.config.ts` drops the /api proxy in this mode, so an /api request
      // is always ended here rather than passed on.
      server.middlewares.use(fixtureMiddleware);
      server.config.logger.info(
        `  ${FIXTURE_ENV}=1 — /api served from frontend/fixtures/factory (artboard data)`
      );
    }
  };
}
