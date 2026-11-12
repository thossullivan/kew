/**
 * #2789 (K4) — a hidden tab makes no requests.
 *
 * This replaces a source-text test that read a hand-kept list of files for the
 * string `document.hidden` (`poll-hygiene.test.ts`). Two polls that were not on
 * the list — Monitor's spend read and the Run button's follow — kept asking in
 * the background, and the test stayed green. Here the browser is the witness:
 * the tab is hidden the way a real one is (`visibilityState`, `hidden`, then a
 * `visibilitychange`), and every `/api/*` request the page starts after that is
 * counted. A stream the page opened before it was hidden is not a new request,
 * so only requests that START after the hide count, a reconnecting stream
 * included.
 *
 * #2851 (Q3, K4): zero requests while hidden is also what a page whose poll
 * never started reads, so each route's poll must be seen running before the
 * hide and again after the show. The routes are the folder's, not a hand-kept
 * list — the four-route list missed `/epics` (a 5s poll), `/loops`, `/health`,
 * `/scorecard` and `/config`. `hidden-tab.test.ts` is this check's own test,
 * with the negative control: a page whose poll is gone fails it.
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 */
// ui-check-owns: src/lib/shell/visibility.svelte.ts
// ui-check-owns: src/lib/shell/pulse.svelte.ts
import { readdirSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { EPIC, fetchThrough } from '../ui-check-lib.mjs';

/** How long a route's hidden tab is watched: past the pulse and the route
 * polls of 15s and under. The 30s holds poll and the 60s health beat are
 * watched hidden by `healthRound`. */
export const HIDDEN_MS = 20_000;

/** Past a poll's interval, how long its request is given to start. */
const SLACK_MS = 3_000;

/** A request the drive has not settled, so the Run button follows it. */
const QUEUED = {
  id: 'rq-hidden-tab',
  command: 'kew epic unhold go-live-2026-09 SFD-229',
  item_id: 'SFD-229',
  state: 'queued',
  operator: 'tom',
  requested_at: '2026-10-03T14:47:00+00:00',
  source: 'console',
  drive: null,
  host: null,
  pid: null,
  log: null,
  started_at: null,
  finished_at: null,
  exit_code: null,
  detail: null,
  model: null
};

/** Serve every run-request read a request still in flight. On /work that is
 * what arms `followRun`, the poll that escaped the source-text test (K3). */
async function followAQueuedRequest(page) {
  await page.route('**/api/run-requests**', (route) => {
    const url = new URL(route.request().url());
    const body = url.pathname.endsWith('/run-requests') ? { requests: [QUEUED] } : QUEUED;
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
  });
}

/** Put the tab in the background (`hidden`) or back in front, the way the
 * browser does: both readings, then the event. */
export function setHidden(page, hidden) {
  return page.evaluate((hidden) => {
    const state = hidden ? 'hidden' : 'visible';
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => state });
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden });
    document.dispatchEvent(new Event('visibilitychange'));
  }, hidden);
}

/** The factory's pages: every `+page.svelte` under this folder. */
export const ROUTES_DIR = new URL('../../src/routes/(factory)/', import.meta.url);

/**
 * Every page under `dir`, named by the URL pattern it serves (`/epics/[ref]`).
 * A `(group)` folder adds no segment to the URL, so it adds none here.
 */
export function factoryRoutes(dir = ROUTES_DIR) {
  const found = [];
  const walk = (folder, segments) => {
    const entries = readdirSync(folder, { withFileTypes: true });
    if (entries.some((entry) => entry.isFile() && entry.name === '+page.svelte')) {
      found.push(`/${segments.join('/')}`);
    }
    for (const entry of entries) {
      if (!entry.isDirectory()) continue;
      const group = /^\(.*\)$/.test(entry.name);
      walk(join(folder, entry.name), group ? segments : [...segments, entry.name]);
    }
  };
  walk(fileURLToPath(dir), []);
  return found.sort();
}

/** One poll: what it is, the request that shows it ran, and its interval. */
const poll = (what, request, every) => ({ what, poll: request, every });

/** The shell's pulse (`lib/shell/pulse.svelte.ts`): `/api/dashboard` every 7s
 * on every factory page, so every visit below watches it. */
const PULSE = poll('the shell pulse', /^\/api\/dashboard$/, 7_000);

/** The pulse's health beat, 60s (`HEALTH_INTERVAL_MS`): past `HIDDEN_MS`, so it
 * is watched by its own longer round (`healthRound`), not by the routes'. */
export const HEALTH = poll('the health beat', /^\/api\/health$/, 60_000);

/** A run that is still going, so the run page arms its tail and status polls. */
async function followALiveRun(page) {
  await page.route('**/api/runs/f5dcb6a5', async (route) => {
    const run = await (await fetchThrough(route)).json();
    const body = JSON.stringify({ ...run, status: 'pending', finished_at: null });
    return route.fulfill({ status: 200, contentType: 'application/json', body });
  });
}

/** A loop still pursuing its goal: the fixture set has no loops, and a loop
 * that is selected and not terminal is what arms the detail poll. */
const LOOP = {
  loop_id: 'loop-hidden-tab',
  source: 'console',
  issue_num: null,
  repo: null,
  goal_text: 'Keep the hidden tab quiet',
  runner_name: 'claude',
  model: '',
  drive_mode: 'driven',
  status: 'pursuing',
  status_reason: '',
  needs_human: false,
  budget_usd: null,
  spent_usd: 0,
  wall_clock_secs: 0,
  max_steps: 10,
  step_count: 1,
  created_at: '2026-10-03T14:47:00+00:00',
  updated_at: '2026-10-03T14:47:00+00:00',
  finished_at: null
};

async function selectAPursuingLoop(page) {
  await page.route('**/api/loops**', (route) => {
    const path = new URL(route.request().url()).pathname;
    const body = path.endsWith('/audit')
      ? []
      : path.endsWith(`/${LOOP.loop_id}`)
        ? { ...LOOP, iterations: [] }
        : [LOOP];
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
  });
}

/**
 * An epic the server will not answer (503) until the tab has been hidden and
 * shown again. The page's first read fails, so it is offline and its reconnect
 * read is armed: that read must run while visible, wait while hidden, and run
 * again once shown — when it is answered and the page comes back.
 */
async function answerTheEpicOnlyOnceShown(page) {
  await page.addInitScript(() => {
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) globalThis.__kewWasHidden = true;
    });
  });
  await page.route(`**/api${EPIC}`, async (route) => {
    const shown = await page
      .evaluate(() => globalThis.__kewWasHidden === true && !document.hidden)
      .catch(() => false);
    if (shown) return route.continue();
    return route.fulfill({ status: 503, contentType: 'application/json', body: '{"detail":"down"}' });
  });
}

/** What the epic page says while the server has never answered. */
export const EPIC_OFFLINE = 'start kew serve to see this epic.';

/** The offline epic's reconnect read: the epic's own detail, on the pulse. */
const RECONNECT = poll('the reconnect read', new RegExp(`^/api${EPIC}$`), 7_000);

/**
 * Asking again is not coming back (#2888): a retry whose answer the page drops
 * makes every request the round expects and leaves the offline line up. Once
 * shown, the page must take the answer — the offline line gone, the canvas
 * drawn — and its reconnect read must disarm: no detail read starts in the two
 * intervals after.
 */
export async function epicComesBack(page, log, { sleep, expect }) {
  const back = () =>
    page
      .evaluate(
        (offline) =>
          !document.body.innerText.includes(offline) &&
          document.querySelector('[data-canvas-pane]') !== null,
        EPIC_OFFLINE
      )
      .catch(() => false);
  const wait = RECONNECT.every + SLACK_MS;
  let recovered = await back();
  for (let waited = 0; !recovered && waited < wait; waited += 250) {
    await sleep(250);
    recovered = await back();
  }
  expect(
    recovered,
    `the epic still says "${EPIC_OFFLINE}" ${wait / 1000}s after its reconnect read was answered`
  );
  const retries = () => log.asked.shown.filter((path) => RECONNECT.poll.test(path)).length;
  const before = retries();
  await sleep(2 * RECONNECT.every);
  expect(
    retries() === before,
    `${retries() - before} reconnect read(s) after the epic came back: the retry never disarmed`
  );
}

/**
 * Every route's visits: where the check opens it, every poll that page arms
 * there (each must run while visible and again once shown), and the `prepare`
 * that serves the state arming them. A route in the folder that this table
 * does not name fails, so a new page is watched from the day it lands.
 *
 * Timers that ask the server nothing (the 1s and 30s clocks on Monitor, Work's
 * needs-you clock) are not polls. Polls the fixture state cannot arm: the Run
 * button's follow outside `/work` (Launchpad, an epic's node drawer), armed
 * only after a press, which queues a request.
 */
export const POLLS = {
  '/audit': [{ visit: '/audit', polls: [PULSE] }],
  '/budget': [{ visit: '/budget', polls: [PULSE] }],
  '/config': [{ visit: '/config', polls: [PULSE] }],
  '/epics': [{ visit: '/epics', polls: [PULSE, poll('the roster read', /^\/api\/epics$/, 5_000)] }],
  // Graph and timeline are two views with one poll each; the fixture epic is
  // unsettled, so the timeline's now-poll is armed.
  '/epics/[ref]': [
    { visit: EPIC, polls: [PULSE, poll('the freshness read', /^\/api\/prs\/freshness$/, 15_000)] },
    {
      visit: `${EPIC}?view=timeline`,
      polls: [PULSE, poll('the timeline now-poll', new RegExp(`^/api${EPIC}/timeline$`), 15_000)]
    },
    // #2888: an epic whose first read failed is offline, and only its reconnect
    // read brings it back without a reload — so the page must come back.
    {
      visit: EPIC,
      prepare: answerTheEpicOnlyOnceShown,
      polls: [PULSE, RECONNECT],
      after: epicComesBack
    }
  ],
  '/health': [{ visit: '/health', polls: [PULSE] }],
  '/loops': [
    {
      visit: '/loops',
      prepare: selectAPursuingLoop,
      polls: [
        PULSE,
        poll('the list read', /^\/api\/loops$/, 7_000),
        poll('the selected loop’s detail', new RegExp(`^/api/loops/${LOOP.loop_id}$`), 4_000)
      ]
    }
  ],
  '/monitor': [
    {
      visit: '/monitor',
      polls: [
        PULSE,
        poll('the event stream', /^\/api\/audit$/, 5_000),
        poll('the holds column', /^\/api\/config\/budget$/, 30_000)
      ]
    }
  ],
  '/prompts': [{ visit: '/prompts', polls: [PULSE] }],
  '/runs': [{ visit: '/runs', polls: [PULSE] }],
  '/runs/[id]': [
    {
      visit: '/runs/f5dcb6a5',
      prepare: followALiveRun,
      polls: [
        PULSE,
        poll('the event tail', /^\/api\/runs\/f5dcb6a5\/audit$/, 1_000),
        poll('the run’s status', /^\/api\/runs\/f5dcb6a5$/, 10_000)
      ]
    }
  ],
  '/scorecard': [{ visit: '/scorecard', polls: [PULSE] }],
  // The held node's unhold block carries a Run button, so the page follows the
  // queued request — the run follower K4 never saw start. Every pulse after the
  // first is a new dashboard, so Work re-reads its list and the selected item's
  // pipeline and evidence on the pulse's beat (#2888). That what they return is
  // drawn is `work-reread.mjs`'s check (#2916): here the answers never change.
  '/work': [
    {
      visit: '/work?item=SFD-229',
      prepare: followAQueuedRequest,
      polls: [
        PULSE,
        poll('the run follower', new RegExp(`^/api/run-requests/${QUEUED.id}$`), 2_000),
        poll('the work re-read', /^\/api\/dashboard\/work$/, PULSE.every),
        poll('the selected item’s pipeline', /^\/api\/work\/SFD-229\/pipeline$/, PULSE.every),
        poll('the selected item’s evidence', /^\/api\/work\/SFD-229\/evidence$/, PULSE.every)
      ]
    }
  ]
};

/** The Work visit the slow-pulse round watches: a selected item, so a pulse
 * that lands is three re-reads (the list, the pipeline, the evidence). */
export const SLOW_PULSE_VISIT = '/work?item=SFD-229';

/** A state word no table knows, served by every Work read once the tab is
 * shown: a row that prints it was drawn from a read taken after the show. */
const SHOWN_STATE = 'read_after_show';
/** …as the row prints it: its underscores read as spaces. */
const SHOWN_WORD = 'read after show';

/**
 * #2911: a pulse response that lands after the hide. Every round above waits
 * for its polls before hiding, and the fixture server answers at once, so a
 * reply still out when the tab went to the background was never seen — and
 * `/work` re-read its list, pipeline and evidence on it, three requests from a
 * hidden tab. Here the next `/api/dashboard` is held until the tab is hidden,
 * then answered: nothing may start while hidden. Once shown, Work re-reads
 * once — one list read before the timer's next pulse — and its rows print
 * what that read served.
 */
export async function slowPulseRound(page, { sleep, expect, log = recordRequests(page) }) {
  const { asked } = log;
  const pulse = (path) => PULSE.poll.test(path);
  const work = (path) => path === '/api/dashboard/work';
  let release;
  const held = new Promise((resolve) => {
    release = resolve;
  });
  let caught = false;
  let landed = false;
  let shown = false;
  page.on('requestfinished', (request) => {
    if (log.phase === 'hidden' && pulse(new URL(request.url()).pathname)) landed = true;
  });
  await page.route(
    (url) => pulse(url.pathname),
    async (route) => {
      if (!caught) {
        caught = true;
        await held;
      }
      return route.continue();
    }
  );
  await page.route(
    (url) => work(url.pathname),
    async (route) => {
      if (!shown) return route.continue();
      const response = await fetchThrough(route);
      const body = await response.json();
      for (const view of body.views ?? []) {
        view.execution.state = SHOWN_STATE;
        view.exception.state = 'none';
      }
      return route.fulfill({ response, json: body });
    }
  );

  const wait = PULSE.every + SLACK_MS;
  expect(await within(wait, () => caught, sleep), `no pulse in ${wait / 1000}s to hold while visible`);
  log.phase = 'hidden';
  await setHidden(page, true);
  release();
  // Not vacuous: the held reply really did land while the tab was hidden.
  expect(
    await within(SLACK_MS, () => landed, sleep),
    `the held pulse was not answered within ${SLACK_MS / 1000}s of the hide`
  );
  await sleep(SLACK_MS);
  expect(
    asked.hidden.length === 0,
    `${asked.hidden.length} request(s) while hidden after a pulse landed: ${[...new Set(asked.hidden)].join(', ')}`
  );

  shown = true;
  log.phase = 'shown';
  await setHidden(page, false);
  const current = () =>
    page
      .locator('aside[aria-label="Issues"] a[data-item]')
      .evaluateAll((rows, word) => rows.some((row) => row.textContent.includes(word)), SHOWN_WORD)
      .catch(() => false);
  let drawn = await current();
  for (let waited = 0; !drawn && waited < wait; waited += 250) {
    await sleep(250);
    drawn = await current();
  }
  expect(drawn, `no Work row reads "${SHOWN_WORD}" ${wait / 1000}s after the show: the rows are stale`);
  await sleep(SLACK_MS);
  // The show's own reading is the first pulse. The timer's next beat may land
  // in this window too, and a re-read on it is the poll, not a duplicate.
  const pulses = asked.shown.flatMap((path, at) => (pulse(path) ? [at] : []));
  const beforeNextBeat = asked.shown.slice(0, pulses[1] ?? asked.shown.length);
  const rereads = beforeNextBeat.filter(work).length;
  expect(
    pulses.length >= 1 && rereads === 1,
    `Work re-read its list ${rereads} time(s) on the show (shown requests: ${asked.shown.join(', ')})`
  );
}

/** The Epics list's roster read, and the per-row change-strip reads it starts. */
const ROSTER = /^\/api\/epics$/;
const CHANGE_STRIP = /^\/api\/epics\/[^/]+\/tick-strip$/;

/**
 * #3054 C7-2 — the roster answer that lands after the hide. `/epics` reads
 * each row's change strip when that row's tick-attempt stamp moves, so a
 * roster drawn in a hidden tab started up to 20 strip reads from it: #2911's
 * bug in the C3 poll. Here the first poll after the page's load is held until
 * the tab is hidden and then answered with every row's stamp moved, so drawing
 * it WOULD read every strip. No strip read may start while hidden; once shown,
 * the show's own roster read lands and the strips are read — which is also
 * what proves the moved stamps would have started reads.
 */
export async function slowRosterRound(page, { sleep, expect, log = recordRequests(page) }) {
  const { asked } = log;
  let reads = 0;
  let caught = false;
  // Installed after the page's load, so the first read routed here is a beat.
  let landed = false;
  let release;
  const held = new Promise((resolve) => {
    release = resolve;
  });
  page.on('requestfinished', (request) => {
    if (log.phase === 'hidden' && ROSTER.test(new URL(request.url()).pathname)) landed = true;
  });
  await page.route(
    (url) => ROSTER.test(url.pathname),
    async (route) => {
      reads += 1;
      if (!caught) {
        caught = true;
        await held;
      }
      const response = await fetchThrough(route);
      const epics = await response.json();
      const moved = epics.map((epic) => ({ ...epic, tick_attempt_stamp: `moved-${reads}` }));
      return route.fulfill({ response, json: moved });
    }
  );

  const wait = 5_000 + SLACK_MS;
  expect(await within(wait, () => caught, sleep), `no roster poll in ${wait / 1000}s to hold while visible`);
  log.phase = 'hidden';
  await setHidden(page, true);
  release();
  expect(
    await within(SLACK_MS, () => landed, sleep),
    `the held roster read was not answered within ${SLACK_MS / 1000}s of the hide`
  );
  await sleep(SLACK_MS);
  const strips = asked.hidden.filter((path) => CHANGE_STRIP.test(path));
  expect(
    strips.length === 0,
    `${strips.length} change-strip request(s) from a hidden tab after a roster landed: ${[...new Set(strips)].join(', ')}`
  );
  expect(
    asked.hidden.length === 0,
    `${asked.hidden.length} request(s) while hidden after a roster landed: ${[...new Set(asked.hidden)].join(', ')}`
  );

  log.phase = 'shown';
  await setHidden(page, false);
  expect(
    await within(wait, () => asked.shown.some((path) => CHANGE_STRIP.test(path)), sleep),
    `no change strip was read ${wait / 1000}s after the show: the moved stamps never reached the rows`
  );
}

/** True once `seen()` holds, looked at every 250ms for at most `ms`. */
async function within(ms, seen, sleep) {
  for (let waited = 0; waited < ms; waited += 250) {
    if (seen()) return true;
    await sleep(250);
  }
  return seen();
}

/**
 * Count every `/api/*` request `page` starts, by the phase it starts in. Made
 * before navigation (the health round) it sees the page's first reads too.
 */
export function recordRequests(page) {
  const log = { phase: 'visible', asked: { visible: [], hidden: [], shown: [] } };
  page.on('request', (request) => {
    const path = new URL(request.url()).pathname;
    if (path.startsWith('/api/')) log.asked[log.phase].push(path);
  });
  return log;
}

/**
 * One page through visible, hidden and shown. Each `/api/*` request is counted
 * in the phase it STARTS in: every poll must run while visible, nothing may be
 * asked while hidden, and every poll must run again once shown. The phase
 * moves BEFORE the visibility event, so a request the event itself starts is
 * counted in the phase it belongs to. A visit's `after` then checks what the
 * shown polls were for.
 */
export async function hiddenTabRound(
  page,
  polls,
  { sleep, expect, hiddenMs = HIDDEN_MS, log = recordRequests(page), after }
) {
  const { asked } = log;
  const missing = (when) => polls.filter(({ poll }) => !asked[when].some((path) => poll.test(path)));
  const wait = Math.max(...polls.map(({ every }) => every)) + SLACK_MS;
  const named = (when) =>
    missing(when)
      .map(({ what, poll }) => `${what} (${poll})`)
      .join(', ');

  await within(wait, () => missing('visible').length === 0, sleep);
  expect(
    missing('visible').length === 0,
    `no ${named('visible')} in ${wait / 1000}s while visible: the poll never ran`
  );
  log.phase = 'hidden';
  await setHidden(page, true);
  await sleep(hiddenMs);
  expect(
    asked.hidden.length === 0,
    `${asked.hidden.length} request(s) while hidden: ${[...new Set(asked.hidden)].join(', ')}`
  );
  log.phase = 'shown';
  await setHidden(page, false);
  await within(wait, () => missing('shown').length === 0, sleep);
  expect(missing('shown').length === 0, `no ${named('shown')} in ${wait / 1000}s after the tab came back`);
  await after?.(page, log, { sleep, expect });
}

/**
 * The 60s health beat, on one page: hidden for longer than the beat, so a
 * health read that ignored `document.hidden` would land inside the window.
 * The shell's first health read is the visible one (it is recorded from
 * navigation); the beat after the show is the timer's own. Monitor is the
 * page, so its 30s holds poll is watched across two of its beats as well.
 */
export const HEALTH_HIDDEN_MS = HEALTH.every + SLACK_MS;

export function healthRound(page, log, { sleep, expect }) {
  return hiddenTabRound(page, [HEALTH], { sleep, expect, hiddenMs: HEALTH_HIDDEN_MS, log });
}

/** How many rounds share the browser at once: each counts only its own page's
 * requests, so they do not see each other. */
export const ROUNDS_AT_ONCE = 6;

/** Run every task, at most `size` at a time. */
export async function inPool(tasks, size) {
  const queue = [...tasks];
  const worker = async () => {
    while (queue.length > 0) await queue.shift()();
  };
  await Promise.all(Array.from({ length: Math.min(size, queue.length) }, worker));
}

export async function checks({ check, visit, expect, sleep }) {
  const routes = factoryRoutes();
  await check('the hidden-tab route list is the routes folder', async () => {
    const named = Object.keys(POLLS).sort();
    expect(
      JSON.stringify(named) === JSON.stringify(routes),
      `src/routes/(factory) has ${routes.join(', ')}\nhidden-tab.mjs names ${named.join(', ')}`
    );
  });

  const rounds = [];
  rounds.push(() => {
    let log;
    return check(
      `the 60s health beat stays quiet for ${HEALTH_HIDDEN_MS / 1000}s hidden and runs again when shown`,
      () =>
        visit(
          '/monitor',
          async (page) => {
            await sleep(3_000);
            await healthRound(page, log, { sleep, expect });
          },
          undefined,
          (page) => {
            log = recordRequests(page);
          }
        )
    );
  });
  for (const route of routes) {
    const visits = POLLS[route] ?? [{ visit: route }];
    for (const spec of visits) {
      rounds.push(() =>
        check(
          `${spec.visit}: every poll runs while visible, no /api request for 20s hidden, and each runs again when shown`,
          async () => {
            expect(
              spec.polls !== undefined,
              `${route} is a page in the routes folder with no poll named here`
            );
            await visit(
              spec.visit,
              async (page) => {
                // Let the page's first reads land, so a request seen while
                // visible is the poll and not the page still loading.
                await sleep(3_000);
                await hiddenTabRound(page, spec.polls, { sleep, expect, after: spec.after });
              },
              undefined,
              spec.prepare
            );
          }
        )
      );
    }
  }
  rounds.push(() =>
    check(
      `${SLOW_PULSE_VISIT}: a pulse answered after the hide starts no request, and the show re-reads Work once`,
      () =>
        visit(SLOW_PULSE_VISIT, async (page) => {
          await sleep(3_000);
          await slowPulseRound(page, { sleep, expect });
        })
    )
  );
  rounds.push(() =>
    check('/epics: a roster answered after the hide starts zero change-strip requests', () =>
      visit('/epics', async (page) => {
        await sleep(3_000);
        await slowRosterRound(page, { sleep, expect });
      })
    )
  );
  await inPool(rounds, ROUNDS_AT_ONCE);

  // K7: the epic's live stream is not a poll, so it is closed, not skipped —
  // and the page that comes back to the front opens it again and catches the
  // tick strip up rather than waiting for the next attempt.
  await check(`the epic stream closes when the tab is hidden and reopens when it is shown`, () =>
    visit(EPIC, async (page) => {
      const opened = [];
      const closed = [];
      const strips = [];
      const isStream = (request) => new URL(request.url()).pathname.endsWith('/stream');
      page.on('request', (request) => {
        if (isStream(request)) opened.push(request);
        if (new URL(request.url()).pathname.endsWith('/tick-strip')) strips.push(request);
      });
      page.on('requestfailed', (request) => {
        if (isStream(request)) closed.push(request);
      });
      await page.waitForFunction(
        () => performance.getEntriesByType('resource').some((e) => e.name.includes('/stream')),
        null,
        { timeout: 15_000 }
      );
      await setHidden(page, true);
      await sleep(1_500);
      expect(closed.length >= 1, 'the stream stayed open in a hidden tab');
      const before = { opened: opened.length, strips: strips.length };
      await setHidden(page, false);
      await sleep(1_500);
      expect(opened.length > before.opened, 'the stream was not opened again when the tab came back');
      expect(strips.length > before.strips, 'the tick strip did not catch up when the tab came back');
    })
  );
}
