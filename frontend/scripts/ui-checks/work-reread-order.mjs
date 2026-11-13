/**
 * #2933 — a slow re-read answer does not overwrite a newer one on Work.
 *
 * `work-reread.mjs` proves Work draws what its pulse-driven re-reads return
 * (#2916). Nothing there holds those reads to answer in the order they were
 * sent: if pulse N's read of the list, the pipeline or the evidence is slow and
 * pulse N+1's answers first, a page with no generation guard draws N+1's
 * reading, then N's late answer over it — older data under a newer reading.
 *
 * Here each of the three reads is served version A (the committed fixture)
 * until A is on screen. Then the next read of each — pulse N's — is HELD; the
 * one after — pulse N+1's — answers at once with version B. Once B is on
 * screen, N's held reads are released with version A, and the screen must
 * still read B in every place for `LATE_MS` after they have reached the page.
 * Every read after N+1's is held until the check is over, so no third pulse
 * can draw B again over a page that let A back and hide that it did.
 * `work-reread-order.test.ts` is this check's own test, with the negative
 * control: a page that draws every answer as it lands fails it.
 *
 * One more path can draw an older answer, and has its own check:
 * `earlierSelectionDropped` holds a pulse's pipeline and evidence reads across
 * a selection of A -> B -> A, and lets them land into the pane the second A
 * selection cleared. Pulse list reads are not raced here: no two list reads
 * are ever out together (#3055), which `work-reread-single.mjs` checks. The
 * loader's list answer can still be drawn after a pulse's — it waits on
 * `/api/dashboard` too, after its list read has freed the slot — and
 * `page.list-reads.client.test.ts` holds that one to the list ledger.
 *
 * The places, version B and the in-page reader are `work-reread.mjs`'s.
 */
// ui-check-owns: src/routes/(factory)/work/+page.svelte
// ui-check-owns: src/lib/work/reads.ts
// ui-check-owns: src/lib/work/EvidenceLedger.svelte
import { ITEM, READS, VISITS, onScreen, versions } from './work-reread.mjs';

/** Which read each place is drawn from. */
export const READ_OF = { row: 'work', stage: 'pipeline', line: 'evidence' };

/** Where each read is served from. */
const PATHS = {
  work: '**/api/dashboard/work',
  pipeline: `**/api/work/${ITEM}/pipeline`,
  evidence: `**/api/work/${ITEM}/evidence`
};

/** The shell pulse's interval (`PULSE_INTERVAL_MS`). */
const PULSE_MS = 7_000;

/** How long version A is given on load, and the held and the fast reads to be
 * sent and B to be drawn: the first pulse after load is `load()`'s own reading,
 * so N can be two pulses away and N+1 one more. */
const LOAD_MS = 15_000;
export const ORDER_MS = 3 * PULSE_MS + 3_000;

/** How long the screen is watched after the late answers reach the page. */
export const LATE_MS = 2_000;

/** Answer a held read, and wait until its answer has reached the page. */
async function landed(page, held, ...version) {
  const finished = page.waitForEvent('requestfinished', {
    predicate: (request) => request === held.route.request(),
    timeout: LOAD_MS
  });
  await held.answer(...version);
  await finished;
}

/** Answer `route` with `body` as JSON. */
const fulfill = (route, body) =>
  route
    .fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
    .catch(() => {});

/**
 * Serve the three reads from `page.route`. Each read answers version A until
 * `arm()`; after it, its next request (pulse N's) is held, the one after (pulse
 * N+1's) answers B at once, and every one after that is held too. `releaseLate`
 * answers the held N reads with A and resolves once each has reached the page;
 * `releaseRest` answers whatever is still held with B, so nothing is left
 * hanging when the page closes.
 */
export async function serveOutOfOrder(page, { a, b } = versions()) {
  const state = {};
  for (const [key, glob] of Object.entries(PATHS)) {
    const read = { armed: false, after: 0, late: null, rest: [] };
    state[key] = read;
    await page.route(glob, (route) => {
      const answer = (version) =>
        route
          .fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(version[key]) })
          .catch(() => {});
      if (!read.armed) return answer(a);
      read.after += 1;
      if (read.after === 1) {
        read.late = { route, answer: () => answer(a) };
        return undefined;
      }
      if (read.after === 2) return answer(b);
      read.rest.push(() => answer(b));
      return undefined;
    });
  }
  return {
    arm() {
      for (const read of Object.values(state)) read.armed = true;
    },
    /** How many reads of `key` were sent since `arm()`. */
    sent: (key) => state[key].after,
    /** Answer pulse N's held reads of `keys` with A, and wait for each to land. */
    async releaseLate(keys) {
      await Promise.all(
        keys.map(async (key) => {
          const { late } = state[key];
          if (late === null) return;
          state[key].late = null;
          await landed(page, late);
        })
      );
    },
    async releaseRest() {
      for (const read of Object.values(state)) {
        if (read.late) await read.late.answer();
        read.late = null;
        for (const answer of read.rest.splice(0)) await answer();
      }
    }
  };
}

/** Every place in `places` reads `version`'s value. */
const all = (screen, places, version) =>
  places.every((place) => screen[place] === READS[place][version]);

/** Look every 250ms for up to `ms` until `done(screen)`; the last look. */
async function until(page, done, ms, sleep) {
  let screen = await onScreen(page);
  for (let waited = 0; !done(screen) && waited < ms; waited += 250) {
    await sleep(250);
    screen = await onScreen(page);
  }
  return screen;
}

/**
 * Version A on screen; pulse N's reads held and pulse N+1's answering B; B on
 * screen; N's answers released with A; B still on screen in every place for
 * `LATE_MS` after they reached the page.
 */
export async function lateAnswerDropped(page, served, { sleep, expect, places = Object.keys(READS) }) {
  const keys = places.map((place) => READ_OF[place]);
  const before = await until(page, (s) => all(s, places, 'a'), LOAD_MS, sleep);
  for (const place of places) {
    const { what, a } = READS[place];
    expect(before[place] === a, `${what} never read "${a}" on load: "${before[place]}"`);
  }
  served.arm();
  const newer = await until(page, (s) => all(s, places, 'b'), ORDER_MS, sleep);
  for (const place of places) {
    const { what, b } = READS[place];
    expect(
      newer[place] === b,
      `${what} never read pulse N+1's "${b}" while N's read was held: "${newer[place]}" ` +
        `(${served.sent(READ_OF[place])} read(s) sent since)`
    );
  }
  await served.releaseLate(keys);
  for (let waited = 0; waited <= LATE_MS; waited += 250) {
    const screen = await onScreen(page);
    const stale = places.filter((place) => screen[place] !== READS[place].b);
    expect(
      stale.length === 0,
      stale
        .map(
          (place) =>
            `${READS[place].what} reads "${screen[place]}" ${waited}ms after pulse N's late ` +
            `answer landed, not pulse N+1's "${READS[place].b}": the older answer was drawn over the newer`
        )
        .join('\n')
    );
    if (waited < LATE_MS) await sleep(250);
  }
}

/** The other item a selection of A -> B -> A passes through: a row of the fixture list. */
export const OTHER = '2053';

/**
 * `OTHER`'s description, which the committed fixtures lack: the fixture server
 * would answer its read 404, and the console error that logs would land in the
 * check or not as timing has it. Served at once, it moves no ledger this check
 * watches.
 */
export const OTHER_DESCRIPTION = {
  item_id: OTHER,
  availability: 'available',
  provenance: 'observed',
  observed_at: '2026-09-24T18:11:58+00:00',
  title: 'The other item of A -> B -> A',
  url: null,
  description: 'Selected between two selections of the item under check.',
  expected: '',
  actual: '',
  reason: '',
  composed_at: '2026-09-24T18:12:04+00:00'
};

/** The detail reads, and the places drawn from them. */
const DETAIL = ['pipeline', 'evidence'];
const DETAIL_PLACES = ['stage', 'line'];

/**
 * Serve the selected item's pipeline and evidence reads, and the other item's,
 * from `page.route`. The item's reads answer A until `arm()`; after it they
 * are held as the FIRST selection's until `leave()`, and as the SECOND's
 * after. Every read of the other item is held, so no answer of its own moves
 * the pane's newest-drawn mark; its description answers at once, so selecting
 * it reads nothing the fixture server would 404. `releaseFirst` answers the first selection's
 * held reads with B and resolves once each has reached the page;
 * `releaseSecond` answers the second's with A, and every read after it at
 * once; `releaseRest` answers whatever is still held.
 */
export async function serveSelections(page, { a, b } = versions()) {
  const state = { phase: 'a', first: [], second: [], other: [] };
  await page.route(`**/api/work/${OTHER}/description`, (route) => fulfill(route, OTHER_DESCRIPTION));
  for (const key of DETAIL) {
    await page.route(`**/api/work/${ITEM}/${key}`, (route) => {
      if (state.phase === 'a') return fulfill(route, a[key]);
      state[state.phase].push({ key, route, answer: (version) => fulfill(route, version[key]) });
      return undefined;
    });
    await page.route(`**/api/work/${OTHER}/${key}`, (route) => {
      state.other.push({ key, route, answer: (version) => fulfill(route, version[key]) });
      return undefined;
    });
  }
  return {
    arm() {
      state.phase = 'first';
    },
    leave() {
      state.phase = 'second';
    },
    /** Whether `group` has had a read of every detail key sent. */
    sent: (group) => DETAIL.every((key) => state[group].some((held) => held.key === key)),
    async releaseFirst() {
      await Promise.all(state.first.splice(0).map((held) => landed(page, held, b)));
    },
    async releaseSecond() {
      state.phase = 'a';
      for (const held of state.second.splice(0)) await held.answer(a);
    },
    async releaseRest() {
      state.phase = 'a';
      for (const group of ['first', 'second', 'other']) {
        for (const held of state[group].splice(0)) await held.answer(a);
      }
    }
  };
}

/** Wait every 250ms for up to `ms` until `done()`; whether it came. */
async function waitFor(done, ms, sleep) {
  for (let waited = 0; !done() && waited < ms; waited += 250) await sleep(250);
  return done();
}

/**
 * The item's pipeline and evidence on screen as A; a pulse's re-read of both
 * held; the selection moved to another item, whose reads are held too, and
 * back; the earlier selection's held reads released with B into the pane the
 * second selection cleared — which must not draw them for `LATE_MS`; then the
 * second selection's own reads released with A, which it must draw. `select`
 * selects an item by id; `reopen` opens the evidence ledger again.
 */
export async function earlierSelectionDropped(page, served, { sleep, expect, select, reopen }) {
  const before = await until(page, (s) => all(s, DETAIL_PLACES, 'a'), LOAD_MS, sleep);
  for (const place of DETAIL_PLACES) {
    const { what, a } = READS[place];
    expect(before[place] === a, `${what} never read "${a}" on load: "${before[place]}"`);
  }
  served.arm();
  expect(
    await waitFor(() => served.sent('first'), ORDER_MS, sleep),
    'no pulse re-read the selected item’s pipeline and evidence after they were on screen'
  );
  served.leave();
  await select(OTHER);
  expect(
    await waitFor(() => served.sent('other'), LOAD_MS, sleep),
    `selecting ${OTHER} never read its pipeline and evidence`
  );
  await select(ITEM);
  expect(
    await waitFor(() => served.sent('second'), LOAD_MS, sleep),
    `selecting ${ITEM} again never read its pipeline and evidence`
  );
  await served.releaseFirst();
  for (let waited = 0; waited <= LATE_MS; waited += 250) {
    const screen = await onScreen(page);
    const stale = DETAIL_PLACES.filter((place) => screen[place] === READS[place].b);
    expect(
      stale.length === 0,
      stale
        .map(
          (place) =>
            `${READS[place].what} reads "${screen[place]}" ${waited}ms after the earlier ` +
            `selection's held answer landed: an answer from before A -> ${OTHER} -> A was drawn`
        )
        .join('\n')
    );
    if (waited < LATE_MS) await sleep(250);
  }
  await served.releaseSecond();
  await reopen();
  const after = await until(page, (s) => all(s, DETAIL_PLACES, 'a'), LOAD_MS, sleep);
  for (const place of DETAIL_PLACES) {
    const { what, a } = READS[place];
    expect(
      after[place] === a,
      `${what} never drew the current selection's own "${a}": "${after[place]}"`
    );
  }
}

/** Open the evidence ledger, which starts closed (#2786 D16). */
async function openLedger(page) {
  const toggle = page.locator('button[data-panel="Evidence ledger"]');
  await toggle.waitFor({ state: 'visible', timeout: LOAD_MS });
  if ((await toggle.getAttribute('aria-expanded')) !== 'true') await toggle.click();
}

export async function checks({ check, visit, expect, sleep, sizes: { WIDTH, HEIGHT } }) {
  const SIZES = [
    { size: { width: WIDTH, height: HEIGHT }, visits: VISITS.wide },
    { size: { width: 390, height: 844 }, visits: VISITS.phone }
  ];
  for (const { size, visits } of SIZES) {
    // No two list reads are ever out together (#3055): a pulse that finds one
    // out — its last one or the page load's — skips its own, which
    // `work-reread-single.mjs` checks. So pulse N held and N+1 answering is a
    // race only the detail reads can still run. (The loader's late answer
    // against a pulse's is `page.list-reads.client.test.ts`'s.)
    const detail = visits
      .map(({ visit, places }) => ({ visit, places: places.filter((place) => place !== 'row') }))
      .filter(({ places }) => places.length > 0);
    for (const { visit: route, places } of detail) {
      const named = places.map((place) => READS[place].what).join(', ');
      let served;
      await check(
        `${route} at ${size.width}px keeps pulse N+1's reading when N's answers late: ${named}`,
        () =>
          visit(
            route,
            async (page, problems) => {
              try {
                if (places.includes('line')) await openLedger(page);
                await lateAnswerDropped(page, served, { sleep, expect, places });
                expect(problems.length === 0, problems.join('\n'));
              } finally {
                await served.releaseRest();
              }
            },
            size,
            async (page) => {
              served = await serveOutOfOrder(page);
            }
          )
      );
    }
  }
  // A -> B -> A selects in place beside the list, which only the wide layout
  // draws together; the ledgers it exercises are the same at every width.
  const route = `/work?item=${ITEM}`;
  let selections;
  await check(`${route} at ${WIDTH}px drops an earlier selection's late detail answers (A -> B -> A)`, () =>
    visit(
      route,
      async (page, problems) => {
        try {
          await openLedger(page);
          await earlierSelectionDropped(page, selections, {
            sleep,
            expect,
            select: (item) => page.locator(`aside[aria-label="Issues"] a[data-item="${item}"]`).click(),
            reopen: () => openLedger(page)
          });
          expect(problems.length === 0, problems.join('\n'));
        } finally {
          await selections.releaseRest();
        }
      },
      { width: WIDTH, height: HEIGHT },
      async (page) => {
        selections = await serveSelections(page);
      }
    )
  );
}
