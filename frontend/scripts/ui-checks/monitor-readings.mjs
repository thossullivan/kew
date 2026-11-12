/**
 * Monitor: a one-line needs-you caption, named stages, the events inside the
 * window the header names, and one spend read (#2847, design-c6 P10 P11 P12 Q2).
 *
 * - P10. The needs-you caption ran six lines, the server's cut leaving
 *   "the conflict...lve executed, exit 1)". It is one line at 1440 and at most
 *   two at 390, with "and N more" never clipped.
 * - P11. Live runs drew "— · review_stage" and a runner "—". No live-run row
 *   prints a snake_case code or a bare dash.
 * - P12. The fixture listed events nine days before the window over them, which
 *   said "No events in this window". Every listed event is inside it, and every
 *   overview bucket the histogram serves is inside the range it was asked for.
 * - Q2. The page polled `/api/report/spend` beside the pulse's dashboard read
 *   of the same caps. It makes no spend request at all.
 * - #3052 N3. A sentence too long for its line is not cut to an ellipsis that
 *   names nothing: the caption says the queue by kind ("1 merge decision ·
 *   1 hold") instead. N15: a quiet row says "13m 26s quiet", as the strip does.
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 */
// ui-check-owns: src/routes/(factory)/monitor/+page.*
// ui-check-owns: src/lib/monitor/readouts.ts
// ui-check-owns: src/lib/monitor/LiveRuns.svelte
// ui-check-owns: src/lib/monitor/live-runs.ts
// ui-check-owns: src/lib/monitor/stream.ts
// ui-check-owns: fixtures/factory/audit.json

const PHONE = { width: 390, height: 844 };

/** The live P10 sentence: a conflict resolve's refusal, past the server's cap. */
const LONG_SENTENCE =
  'the conflict resolve for #2766 could not finish its rebase: the resolve executed, exit 1 ' +
  '(git rebase --continue stopped on src/kew/graph/driver/merge_queue.py)';

/** The live queue P10 was photographed over: a dozen rows, each a long sentence. */
const withLongQueue = (page) =>
  page.route(
    (url) => url.pathname === '/api/dashboard',
    async (route) => {
      const response = await route.fetch();
      const body = await response.json();
      const top = body.rows[0];
      body.rows = Array.from({ length: 13 }, (_, n) => ({
        ...top,
        item_id: String(2766 + n),
        issue_num: 2766 + n,
        sentence: LONG_SENTENCE
      }));
      await route.fulfill({ response, json: body });
    }
  );

/**
 * The live runs P11 was photographed on: a run of a kind kew has no stage
 * for (served `unknown`, its kind `review_stage` in the detail), with neither a
 * model nor a runner recorded; and one an older serve reports no stage for.
 */
const withUnnamedRuns = (page) =>
  page.route(
    (url) => url.pathname === '/api/runs',
    async (route) => {
      const response = await route.fetch();
      const runs = await response.json();
      const working = runs.find((run) => run.status === 'pending' && run.started_at);
      runs.unshift(
        {
          ...working,
          run_id: 'live-unknown',
          issue_num: 0,
          stage: 'unknown',
          stage_detail: 'review_stage',
          model: '',
          runner: ''
        },
        { ...working, run_id: 'live-unreported', stage: '', stage_detail: '', model: null, runner: null }
      );
      await route.fulfill({ response, json: runs });
    }
  );

/**
 * The caption as drawn: its line count, read off its text's own boxes (padding
 * aside), and which reading it is showing — the top row's sentence with its
 * "and N more", or the queue by kind when the sentence does not fit (#3052
 * N3). The probe that measures the sentence is laid out but never seen, so it
 * is no part of what this reads.
 */
function readCaption() {
  const caption = document.querySelector('[data-needs-you-caption]');
  if (!caption) return null;
  const shown = caption.querySelector('[data-caption-sentence], [data-caption-summary]');
  if (!shown) return { text: '', lines: 0, mode: '', count: '', countClipped: false, clipped: false, figure: '' };
  const tops = [];
  const walker = document.createTreeWalker(shown, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    if (!node.textContent.trim()) continue;
    const range = document.createRange();
    range.selectNodeContents(node);
    for (const rect of range.getClientRects()) {
      if (rect.width === 0) continue;
      if (!tops.some((top) => Math.abs(top - rect.top) < 4)) tops.push(rect.top);
    }
  }
  const sentence = shown.matches('[data-caption-sentence]');
  const spans = sentence ? [...shown.children] : [];
  const count = spans.find((el) => /^and \d+ more$/.test(el.textContent.trim()));
  const clip = sentence ? shown.querySelector('.truncate') : null;
  const cell = caption.closest('section[aria-label="Readouts"] > *');
  return {
    text: shown.textContent.replace(/\s+/g, ' ').trim(),
    lines: tops.length,
    mode: sentence ? 'sentence' : 'summary',
    count: count?.textContent.trim() ?? '',
    countClipped: count ? count.scrollWidth > count.clientWidth + 1 : false,
    // The one way a caption can end in an ellipsis: its sentence cut by the clamp.
    clipped: clip ? clip.scrollWidth > clip.clientWidth + 1 : false,
    figure: cell?.children[1]?.textContent.replace(/\s+/g, ' ').trim() ?? ''
  };
}

/** What the Needs you summary may call a kind of row (`readouts.ts`, `typeLabel`). */
const KIND =
  /^(merge decisions?|holds?|approvals?|stalled epics?|PR reviews?|exceptions?|epic waits?|orphan PRs?|unreadable items?|triage proposals?|merge queues?|more)$/;

/**
 * Why a summary does not say the queue by kind, or null when it does: every
 * part is "<n> <kind>", one of them is a kind and not only "more", and the
 * parts add up to the figure above it.
 */
export function summaryFault(text, figure) {
  const parts = text.split(' · ').map((part) => /^(\d+) (.+)$/.exec(part));
  if (parts.some((part) => part === null || !KIND.test(part[2]))) return `"${text}" is not a count of kinds`;
  if (!parts.some((part) => part[2] !== 'more')) return `"${text}" names no kind`;
  const total = parts.reduce((sum, part) => sum + Number(part[1]), 0);
  return String(total) === figure ? null : `"${text}" counts ${total} rows under a figure of ${figure}`;
}

/**
 * The window label the event stream names and the events it lists, read in
 * the page: `from`/`to` are the label's own edges, in the page's local time,
 * and each event carries the audit id its row was drawn for.
 *
 * A label with no date names the day of `anchorMs`: the window's own start,
 * or the instant the page's clock is pinned to. A second read of the clock is
 * not that day once local midnight falls between the label and this read
 * (#3014), so every caller passes it.
 */
function readStream(root, anchorMs) {
  // The window's own label sits first on the line with the key hints.
  const line = [...root.querySelectorAll('div')].find((el) =>
    [...el.children].some((child) => child.textContent.includes('pan · + − zoom'))
  );
  const label = line?.querySelector('span')?.textContent.trim() ?? '';
  const anchor = new Date(anchorMs);
  // "01:30 – 02:00" today; "10-13 23:45 – 10-14 00:15" on any other day.
  const at = (part) => {
    const m = /^(?:(\d{2})-(\d{2}) )?(\d{2}):(\d{2})$/.exec(part.trim());
    if (!m) return NaN;
    const month = m[1] ? Number(m[1]) - 1 : anchor.getMonth();
    const day = m[2] ? Number(m[2]) : anchor.getDate();
    return new Date(anchor.getFullYear(), month, day, Number(m[3]), Number(m[4])).getTime();
  };
  const [from, to] = label.split(' – ').map(at);
  const events = [...root.querySelectorAll('li[data-event-id]')].map((li) => {
    const el = li.querySelector('span[title$=" UTC"]');
    return {
      id: Number(li.getAttribute('data-event-id')),
      time: el?.textContent.trim() ?? '',
      ms: el ? Date.parse(el.getAttribute('title').replace(' UTC', 'Z').replace(' ', 'T')) : NaN
    };
  });
  return { label, from, to, events, text: root.textContent };
}

/**
 * Every `/api/audit` list the page is answered with, plus one event `extra`
 * builds out of the served ones (#2915). The fixture server rebases every
 * response on its own request's clock, so the same event comes back a few
 * seconds apart on two reads: rows are matched to what the PAGE was served,
 * never to a separate fetch, and the added event proves a page answer the
 * check never read itself cannot fail it. `lists` collects `{ query, body, added }`.
 */
const withAddedEvent = (lists, extra) => (page) =>
  page.route(
    (url) => url.pathname === '/api/audit',
    async (route) => {
      const response = await route.fetch();
      if (!response.ok()) return route.fulfill({ response });
      const body = await response.json();
      const added = extra(body.events);
      body.events = [added, ...body.events.filter((e) => e.id !== added.id)];
      lists.push({ query: new URL(route.request().url()).searchParams, body, added });
      await route.fulfill({ response, json: body });
    }
  );

/** Every listed event is inside the window the header names, and some are listed. */
function expectInside(read, expect, where = '') {
  expect(Number.isFinite(read.from) && Number.isFinite(read.to), `${where}the window reads "${read.label}"`);
  expect(read.events.length >= 1, `${where}the fixture stream listed no event, so this proves nothing`);
  for (const e of read.events) {
    expect(e.ms >= read.from && e.ms <= read.to, `${where}the ${e.time} event is outside the window "${read.label}"`);
  }
}

/**
 * Why the drawn stream is not one of the page's own `/api/audit` answers, or
 * null when it is: row n drew the id of the answer's event n, as many rows as
 * events, newest first as served (#2921). A set comparison passed a stream
 * drawn oldest-first or one that drew a row twice, and a comparison by second
 * passed two events of one second drawn swapped. The page may read more than
 * once, so the stream may match any answer it got; a mismatch is reported
 * against the last one.
 */
export function streamOrderFault(drawn, answers) {
  const ids = drawn.map((row) => row.id);
  const matches = (events) => events.length === ids.length && events.every((e, n) => e.id === ids[n]);
  if (answers.length === 0) return 'the page got no /api/audit answer to compare with';
  if (answers.some(matches)) return null;
  return `the stream draws ids [${ids}], not the answered [${answers[answers.length - 1].map((e) => e.id)}]`;
}

export async function checks({ base, check, visit, expect, sleep, sizes: { WIDTH, HEIGHT } }) {
  for (const size of [{ width: WIDTH, height: HEIGHT }, PHONE]) {
    const most = size.width === PHONE.width ? 2 : 1;
    for (const long of [false, true]) {
      const what = long ? 'a long live queue' : 'the fixture';
      await check(`the needs-you caption is at most ${most} line(s) at ${size.width}px, over ${what}`, () =>
        visit(
          '/monitor',
          async (page) => {
            await page.locator('[data-needs-you-caption]').waitFor({ state: 'visible', timeout: 15_000 });
            await sleep(300);
            const read = await page.evaluate(readCaption);
            expect(read !== null && read.text !== '', 'the Needs you cell drew no caption');
            expect(read.lines >= 1, `"${read.text}" measured no line at all, so this proves nothing`);
            expect(read.lines <= most, `"${read.text}" takes ${read.lines} lines at ${size.width}px`);
            if (read.mode === 'sentence') {
              expect(read.count !== '', `"${read.text}" says nothing of the rows behind the top one`);
              expect(!read.countClipped, `"${read.count}" is clipped at ${size.width}px`);
              if (long) expect(read.count === 'and 12 more', `the long queue reads "${read.count}"`);
            } else {
              // The summary counts every row, so it is its own "and N more".
              const fault = summaryFault(read.text, read.figure);
              expect(fault === null, `${fault} at ${size.width}px`);
              if (long) expect(read.text === '13 merge decisions', `the long queue reads "${read.text}"`);
            }
          },
          size,
          long ? withLongQueue : undefined
        )
      );
    }
  }

  /**
   * #3052 N3 — since the four readouts went equal the fixture's caption read
   * "PR #2059 is open —…" at 1440 and the live one "the review round c…": a
   * cut that names nothing. Whatever it draws, the caption names a kind of row
   * or an item, and no clamp has cut it to an ellipsis.
   */
  for (const size of [{ width: WIDTH, height: HEIGHT }, PHONE]) {
    for (const long of [false, true]) {
      const what = long ? 'a long live-like reason' : 'the fixture';
      await check(`the needs-you caption names a kind or an item, uncut, at ${size.width}px over ${what}`, () =>
        visit(
          '/monitor',
          async (page) => {
            await page.locator('[data-needs-you-caption]').waitFor({ state: 'visible', timeout: 15_000 });
            await sleep(300);
            const read = await page.evaluate(readCaption);
            expect(read !== null && read.text !== '', 'the Needs you cell drew no caption');
            expect(!read.clipped, `"${read.text}" is cut to an ellipsis at ${size.width}px`);
            expect(!/(…|\.\.\.)$/.test(read.text), `"${read.text}" ends in an ellipsis`);
            const item = /(#\d+|\b[A-Z]+-\d+\b)/.test(read.text);
            const kind = read.mode === 'summary' && summaryFault(read.text, read.figure) === null;
            expect(item || kind, `"${read.text}" names neither a kind nor an item at ${size.width}px`);
          },
          size,
          long ? withLongQueue : undefined
        )
      );
    }
  }

  /**
   * #3052 N15 — the quiet rows printed a bare uppercase "13M 26S" under a
   * histogram that says "13m 26s quiet". One term: every quiet row says it the
   * strip's way.
   */
  await check('every quiet-gap row reads "<m>m <s>s quiet" or "<s>s quiet"', () =>
    visit('/monitor', async (page) => {
      const stream = page.locator('section[aria-label="Event stream"]');
      await stream.locator('li span[title$=" UTC"]').first().waitFor({ timeout: 15_000 });
      await sleep(300);
      const rows = await stream.locator('[data-quiet-gap]').evaluateAll((els) =>
        els.map((el) => ({ text: el.innerText.trim(), transform: getComputedStyle(el).textTransform }))
      );
      expect(rows.length >= 1, 'the fixture stream drew no quiet row, so this proves nothing');
      for (const row of rows) {
        expect(/^\d+m \d+s quiet$/.test(row.text) || /^\d+s quiet$/.test(row.text), `a quiet row reads "${row.text}"`);
        expect(row.transform === 'none', `the quiet row "${row.text}" is drawn ${row.transform}`);
      }
    })
  );

  for (const size of [{ width: WIDTH, height: HEIGHT }, PHONE]) {
    await check(`no live-run row prints a snake_case stage or a bare dash (${size.width}px)`, () =>
      visit(
        '/monitor',
        async (page) => {
          const rows = page.locator('section[aria-label="Live runs"] [data-run]');
          await page
            .locator('section[aria-label="Live runs"] [data-run="live-unknown"]')
            .waitFor({ state: 'attached', timeout: 15_000 });
          const cells = await rows.evaluateAll((els) =>
            els.map((row) => ({
              run: row.getAttribute('data-run'),
              cells: [...row.children].map((el) => el.textContent.replace(/\s+/g, ' ').trim())
            }))
          );
          expect(cells.length >= 3, `only ${cells.length} live-run row(s) drew, so this proves nothing`);
          for (const { run, cells: texts } of cells) {
            for (const text of texts) {
              expect(!/^[—–-]$/.test(text), `run ${run} prints a bare dash: [${texts.join(' | ')}]`);
              expect(!/(^|\s)[—–-] ·/.test(text), `run ${run} prints a dash for a part: "${text}"`);
              expect(!/\b[a-z0-9]+_[a-z0-9_]+\b/.test(text), `run ${run} prints a code: "${text}"`);
            }
          }
          const unknown = cells.find((row) => row.run === 'live-unknown').cells.join(' | ');
          expect(unknown.includes('review stage'), `the unknown-stage run reads [${unknown}]`);
          expect(unknown.includes('runner not recorded'), `the runner-less run reads [${unknown}]`);
        },
        size,
        withUnnamedRuns
      )
    );
  }

  /**
   * #3468 D33 — the Stage track is bounded at 280px. As `minmax(0, 1fr)` it
   * took about 600px at 1440 for one word, while the runner and posture
   * sentences beside it truncated in 180px and 160px: the rest of the row goes
   * to those two.
   */
  await check(`the Live runs Stage column is at most 280px and the sentences take the rest (${WIDTH}px)`, () =>
    visit(
      '/monitor',
      async (page) => {
        const head = page.locator('section[aria-label="Live runs"] div.lg\\:grid').first();
        await head.waitFor({ state: 'visible', timeout: 15_000 });
        await sleep(300);
        const widths = await head.evaluate((el) =>
          Object.fromEntries(
            [...el.children].map((cell) => [cell.textContent.trim(), cell.getBoundingClientRect().width])
          )
        );
        const { Stage: stage, Runner: runner, Posture: posture } = widths;
        expect(
          stage !== undefined && runner !== undefined && posture !== undefined,
          `the Live runs heads are ${JSON.stringify(Object.keys(widths))}, not Stage, Runner and Posture`
        );
        expect(stage <= 280.5, `the Stage column is ${Math.round(stage)}px wide, past its 280px bound`);
        expect(
          runner > 180.5 && posture > 160.5,
          `Runner is ${Math.round(runner)}px and Posture ${Math.round(posture)}px: the row's spare width did not go to them`
        );
      },
      { width: WIDTH, height: HEIGHT },
      withUnnamedRuns
    )
  );

  // #3014 — the page's clock is pinned to the instant the check starts, and a
  // dateless label is read against that same instant: the day the page labels
  // and the day the read resolves it to are one clock read, not two.
  await check('every listed fixture event is inside the window the header names', () => {
    const pinnedMs = Date.now();
    return visit(
      '/monitor',
      async (page) => {
        const stream = page.locator('section[aria-label="Event stream"]');
        await stream.locator('li span[title$=" UTC"]').first().waitFor({ timeout: 15_000 });
        await sleep(300);
        const read = await stream.evaluate(readStream, pinnedMs);
        expectInside(read, expect);
        expect(!read.text.includes('No events in this window'), 'the strip says its window is empty');
      },
      undefined,
      (page) => page.clock.setFixedTime(pinnedMs)
    );
  });

  /**
   * #2875 — the fixture list served the whole log whatever the window, so a
   * custom range over a past stretch listed events outside it. The window is
   * pinned off the served instants, on minute edges (the label's resolution),
   * and is interior: it starts at the third-oldest event's minute and ends
   * before the newest, so events lie on BOTH sides of it — a list that drops
   * `since` shows the two oldest, one that drops `until` shows the newest.
   *
   * #3014 — the page's clock is pinned a few seconds past the local midnight
   * after the window, so the window is always on the page's yesterday: its
   * label must say the date, and the run's own hour never decides it.
   */
  for (const size of [{ width: WIDTH, height: HEIGHT }, PHONE]) {
    await check(`on a pinned past window every listed event is inside it (${size.width}px)`, async () => {
      const served = (await (await fetch(`${base}/api/audit`)).json()).events;
      const byAge = [...served].sort((a, b) => Date.parse(a.timestamp) - Date.parse(b.timestamp));
      expect(byAge.length >= 4, `the fixture list serves ${byAge.length} event(s)`);
      const minute = (ms) => Math.floor(ms / 60_000) * 60_000;
      const fromMs = minute(Date.parse(byAge[2].timestamp));
      const toMs = minute(Date.parse(byAge[byAge.length - 1].timestamp));
      const inside = (e) => Date.parse(e.timestamp) >= fromMs && Date.parse(e.timestamp) <= toMs;
      // This read only pins the window and guards that it proves something;
      // what the rows must be is read off the page's own answer below.
      const wanted = served.filter(inside).map((e) => e.id);
      expect(
        Date.parse(byAge[1].timestamp) < fromMs && Date.parse(byAge[byAge.length - 1].timestamp) > toMs,
        'no served event lies outside the pinned window on both sides, so this proves nothing'
      );
      expect(wanted.length >= 1, 'the pinned window holds no served event, so this proves nothing');
      const [from, to] = [new Date(fromMs).toISOString(), new Date(toMs).toISOString()];
      const route = `/monitor?range=custom&from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}`;
      const end = new Date(toMs);
      const pastMidnight = new Date(end.getFullYear(), end.getMonth(), end.getDate() + 1, 0, 0, 5).getTime();
      // The page's answer gains an event inside the window that the read above
      // never served: the rows must still match what the page was answered.
      const lists = [];
      const added = withAddedEvent(lists, (events) => ({
        ...events[0],
        id: Math.max(...served.map((e) => e.id)) + 1,
        timestamp: new Date(toMs - 30_000).toISOString()
      }));
      await visit(
        route,
        async (page) => {
          const stream = page.locator('section[aria-label="Event stream"]');
          await stream.locator('li span[title$=" UTC"]').first().waitFor({ timeout: 15_000 });
          await sleep(300);
          const read = await stream.evaluate(readStream, fromMs);
          expect(
            /^\d{2}-\d{2} \d{2}:\d{2} – \d{2}-\d{2} \d{2}:\d{2}$/.test(read.label),
            `[${from} – ${to}] the window on the page's yesterday reads "${read.label}", with no date`
          );
          expectInside(read, expect, `[${from} – ${to}] `);
          // The list read asks for exactly the window: `since` inclusive at its
          // start, `until` a millisecond past its end (`streamFilters`).
          expect(lists.length >= 1, 'the page made no /api/audit read');
          for (const { query } of lists) {
            const bounds = `since=${query.get('since')} until=${query.get('until')}`;
            expect(
              Date.parse(query.get('since')) === fromMs && Date.parse(query.get('until')) === toMs + 1,
              `[${from} – ${to}] the list read asked for ${bounds}`
            );
          }
          // What the page was answered for that window is what it must draw:
          // every answered event inside it, more than the added one alone.
          const last = lists[lists.length - 1];
          const answered = last.body.events;
          const outside = answered.filter((e) => !inside(e)).map((e) => e.id);
          expect(outside.length === 0, `[${from} – ${to}] the list read answered ids [${outside}] outside the window`);
          const ids = answered.map((e) => e.id);
          expect(
            ids.some((id) => id !== last.added.id),
            `[${from} – ${to}] the list read answered only the added event, so this proves nothing`
          );
          // The rows drawn are the answer's events, in its order and no more.
          const fault = streamOrderFault(read.events, lists.map(({ body }) => body.events));
          expect(fault === null, `[${from} – ${to}] ${fault}`);
        },
        size,
        async (page) => {
          await page.clock.setFixedTime(pastMidnight);
          await added(page);
        }
      );
    });
  }

  /**
   * #2875 — the Monitor's item filter sends an issue number as `?item=`, which
   * the store matches as `item_id = n OR issue_num = n`. Matched on `item_id`
   * alone, the fixture rows (none of which carries one) listed nothing for it.
   */
  await check('/monitor?item=2055 lists the events of #2055 and no others', async () => {
    const served = (await (await fetch(`${base}/api/audit`)).json()).events;
    const wanted = served.filter((e) => e.issue_num === 2055).map((e) => e.id);
    expect(wanted.length >= 1, 'the fixture list serves no #2055 event, so this proves nothing');
    expect(wanted.length < served.length, 'every served event is #2055, so this proves nothing');
    // The page's answer gains a #2055 event the read above never served, 30 s
    // after the newest event of that same answer, so the live window holds it.
    const lists = [];
    const added = withAddedEvent(lists, (events) => ({
      ...events[0],
      id: Math.max(...served.map((e) => e.id)) + 1,
      issue_num: 2055,
      timestamp: new Date(Math.max(...events.map((e) => Date.parse(e.timestamp))) + 30_000).toISOString()
    }));
    // The page's clock and the label's read share one instant (#3014).
    const pinnedMs = Date.now();
    await visit(
      '/monitor?item=2055',
      async (page) => {
        const stream = page.locator('section[aria-label="Event stream"]');
        await stream.locator('li span[title$=" UTC"]').first().waitFor({ timeout: 15_000 });
        await sleep(300);
        const read = await stream.evaluate(readStream, pinnedMs);
        expect(read.events.length >= 1, 'the item-filtered stream listed no event');
        expect(lists.length >= 1, 'the page made no /api/audit read');
        for (const { query } of lists) expect(query.get('item') === '2055', `the list read asked for item=${query.get('item')}`);
        // Each row drawn, matched by id to the event the page was served: the
        // expectation is the page's own answer, never the read above.
        const last = lists[lists.length - 1];
        const answered = last.body.events;
        expect(
          answered.some((e) => e.id !== last.added.id),
          'the list read answered only the added event, so this proves nothing'
        );
        for (const row of read.events) {
          const e = answered.find((event) => event.id === row.id);
          expect(e?.issue_num === 2055, `row ${row.time} (id ${row.id}) is ${e ? `#${e.issue_num}` : 'no served event'}, not #2055`);
        }
        // The rows drawn are the answer's events, in its order and no more.
        const fault = streamOrderFault(read.events, lists.map(({ body }) => body.events));
        expect(fault === null, `item=2055: ${fault}`);
      },
      undefined,
      async (page) => {
        await page.clock.setFixedTime(pinnedMs);
        await added(page);
      }
    );
  });

  for (const size of [{ width: WIDTH, height: HEIGHT }, PHONE]) {
    await check(`every overview bucket is inside the range it was asked for (${size.width}px)`, async () => {
      const reads = [];
      const record = (page) =>
        page.on('response', async (response) => {
          const url = new URL(response.url());
          if (url.pathname !== '/api/audit/histogram' || !response.ok()) return;
          reads.push({ query: url.searchParams, body: await response.json() });
        });
      await visit(
        '/monitor',
        async (page) => {
          const stream = page.locator('section[aria-label="Event stream"]');
          await stream.locator('li span[title$=" UTC"]').first().waitFor({ timeout: 15_000 });
          await stream.getByRole('button', { name: '24h', exact: true }).click();
          const deadline = Date.now() + 10_000;
          while (reads.length < 2 && Date.now() < deadline) await sleep(100);
          expect(reads.length >= 2, `${reads.length} histogram read(s): the live and 24h ranges were not both read`);
          for (const { query, body } of reads) {
            const since = Date.parse(query.get('since'));
            const until = Date.parse(query.get('until'));
            const label = `${query.get('since')} – ${query.get('until')}`;
            expect(Date.parse(body.since) === since && Date.parse(body.until) === until, `[${label}] answered ${body.since} – ${body.until}`);
            expect(body.buckets.length >= 1, `[${label}] served no bucket, so this proves nothing`);
            for (const bucket of body.buckets) {
              const start = Date.parse(bucket.start);
              expect(start >= since && start < until, `[${label}] serves a bucket at ${bucket.start}`);
            }
          }
          expect(reads.some(({ body }) => body.total >= 1), 'no histogram counted an event, so this proves nothing');
        },
        size,
        record
      );
    });
  }

  await check('Monitor makes no /api/report/spend request in 20 s', async () => {
    const paths = [];
    await visit(
      '/monitor',
      async (page) => {
        await page.locator('section[aria-label="Readouts"]').waitFor({ state: 'visible', timeout: 15_000 });
        await sleep(20_000);
        const spend = paths.filter((path) => path === '/api/report/spend').length;
        const pulses = paths.filter((path) => path === '/api/dashboard').length;
        // The pulse beats every 7s: three reads in 20s is a page that was live.
        expect(pulses >= 3, `only ${pulses} dashboard read(s) in 20 s, so the page was not polling`);
        expect(spend === 0, `${spend} /api/report/spend request(s) in 20 s`);
        const figures = await page.locator('section[aria-label="Readouts"]').textContent();
        expect(/\$178\.05/.test(figures), 'the Today figure is not drawn from the pulse');
      },
      undefined,
      (page) => page.on('request', (request) => paths.push(new URL(request.url()).pathname))
    );
  });
}
