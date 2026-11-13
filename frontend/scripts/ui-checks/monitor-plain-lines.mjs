/**
 * Monitor: plain event values, one statement per hold, readouts on one line
 * at 390 (#2896).
 *
 * R5. The event stream printed the values a payload recorded — `in_review →
 * adversarial_review`, `base_integration_failed` — as the row's words. The
 * committed audit fixture carries no snake_case value, so `/api/audit` is
 * served here with the fixture's events AND a status move and a refusal whose
 * values are exactly those codes, and no event row may print a snake_case
 * word. The code is kept as the row's `title`, which is checked too. An error
 * is text, not a code: one naming `src/my_module.py` and one reading
 * `classifier_unavailable` are served, and each must print exactly as
 * recorded. So must a reason no known-code list words (`quota_probe_skipped`).
 *
 * R7. A hold row printed its command in the copy block and then again in an
 * 11px line under it ("resolve the conflict on the branch, then kew epic
 * unhold …"). `/api/dashboard` is served with an epic hold that carries a
 * command, and outside its block the row may not contain that command — nor
 * draw an action line at all.
 *
 * R12. At 390 "THIS WEEK" and "31.5% of cap" each broke over two lines. The
 * caps are served so the cap line is the longest reading it takes (an
 * over-cap multiplier, "×12.3 over cap", "×12.3 over" below 1024), and every
 * readout label and cap line is one line, inside its own cell, clear of the
 * label beside it. A multiplier of 100 or more reads "×99+ over" — "×99+ over
 * cap" at a desk and over the gauge track — with the exact reading only ever
 * a title, so agentic 1000 is served against a 0.001 cap and a 1e-12 one: no
 * drawn text may carry the exact multiplier, every visible multiplier box
 * stays inside its cell, and the page never scrolls sideways.
 *
 * #3468 D33. An event row's separator lost its leading space at the start of
 * the clipped box ("Adversarial review verdict· launch-fixes"), so every `·`
 * on every row must follow a space as drawn. And the epic filter is chips like
 * the kind chips beside it, not a native `<select>`, still writing `epic=`.
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 */
// ui-check-owns: src/routes/(factory)/monitor/+page.*
// ui-check-owns: src/lib/monitor/EventStream.svelte
// ui-check-owns: src/lib/monitor/event-line.ts
// ui-check-owns: src/lib/monitor/Holds.svelte
// ui-check-owns: src/lib/monitor/readouts.ts
// ui-check-owns: fixtures/factory/audit.json

const PHONE = { width: 390, height: 844 };

/** A served event a minute or two ago, so every Monitor range lists it. */
const recent = (id, minutesAgo, event, summary) => ({
  id,
  timestamp: new Date(Date.now() - minutesAgo * 60_000).toISOString(),
  event,
  issue_num: 2201,
  item_id: null,
  run_id: null,
  actor_type: 'host',
  actor_id: 'kew',
  summary,
  epic_ref: 'launch-fixes'
});

/** The codes R5 photographed, each in the field it was recorded under. */
const CODED = [
  ['epic_node_status', '{"epic_ref":"launch-fixes","from":"in_review","to":"adversarial_review"}'],
  ['merge_queue_held', '{"epic_ref":"launch-fixes","reason":"base_integration_failed"}'],
  ['epic_node_status', '{"epic_ref":"launch-fixes","from":"in_progress","to":"in_review","note":{"x":1}}'],
  ['dispatch_failed', '{"epic_ref":"launch-fixes","error":"failed to open src/my_module.py"}'],
  ['dispatch_failed', '{"epic_ref":"launch-fixes","error":"classifier_unavailable"}'],
  ['dispatch_refused', '{"epic_ref":"launch-fixes","reason":"quota_probe_skipped"}']
];

/** Served values that must print exactly: an error is text, an unknown reason is not worded. */
const VERBATIM = ['failed to open src/my_module.py', 'classifier_unavailable', 'quota_probe_skipped'];

const withCodedEvents = (page) =>
  page.route(
    (url) => url.pathname === '/api/audit',
    async (route) => {
      const response = await route.fetch();
      const body = await response.json();
      const top = Math.max(0, ...body.events.map((e) => e.id));
      const served = CODED.map(([event, summary], i) => recent(top + CODED.length - i, i + 1, event, summary));
      body.events = [...served, ...body.events];
      await route.fulfill({ response, json: body });
    }
  );

/** A command long enough to be the one an operator really pastes. */
const COMMAND = 'kew epic unhold launch-fixes-with-a-long-epic-name-2026-10 2201';

/** An epic hold whose table action names its command: the R7 row. */
const CONFLICT_HOLD = {
  type: 'budget_hold',
  consequence: 'wait',
  age: '2026-10-14T09:00:00+00:00',
  reason: 'base_integration_conflict: src/kew/graph/driver/merge_queue.py',
  primary_action: '',
  issue_num: 2201,
  item_id: '2201',
  estimate_usd: null,
  epic_ref: 'launch-fixes-with-a-long-epic-name-2026-10',
  url: null,
  pr_number: null,
  attention_kind: 'held',
  label: '',
  command: COMMAND,
  sentence: 'the base branch no longer merges into the branch: src/kew/graph/driver/merge_queue.py'
};

/** Caps whose cap line is the longest it reads: "×12.3 over cap". */
const OVER_CAP = {
  daily: { window_days: 1, spent: 3075.0, cap: 250.0, agentic: 3075.0, interactive: 0.0 },
  weekly: { window_days: 7, spent: 18450.0, cap: 1500.0, agentic: 18450.0, interactive: 0.0 }
};

/** Caps whose multiplier has no width of its own: "×1000000.0 over cap" and wider. */
const farOverCap = (cap) => ({
  daily: OVER_CAP.daily,
  weekly: { window_days: 7, spent: 1000.0, cap, agentic: 1000.0, interactive: 0.0 }
});

/** Each served payload, and the exact This week reading it must keep as a title only. */
const PAYLOADS = [
  { name: '×12.3', caps: OVER_CAP, exact: null },
  { name: 'cap 0.001', caps: farOverCap(0.001), exact: `×${(1000 / 0.001).toFixed(1)} over cap` },
  { name: 'cap 1e-12', caps: farOverCap(1e-12), exact: `×${(1000 / 1e-12).toFixed(1)} over cap` }
];

const withServedDashboard = (page, caps = OVER_CAP) =>
  page.route(
    (url) => url.pathname === '/api/dashboard',
    async (route) => {
      const response = await route.fetch();
      const body = await response.json();
      body.rows = [CONFLICT_HOLD, ...body.rows];
      body.glance = { ...body.glance, caps };
      await route.fulfill({ response, json: body });
    }
  );

/** A snake_case word: two or more word runs joined by underscores. */
const SNAKE = /\b[A-Za-z0-9]+(?:_[A-Za-z0-9]+)+\b/;

/**
 * Every drawn box in the Readouts that states a multiplier, the cell it sits
 * in, and the page's scroll width against the viewport.
 */
function readMultipliers() {
  const section = document.querySelector('section[aria-label="Readouts"]');
  if (!section) return null;
  const boxes = [...section.querySelectorAll('span, div')]
    .filter((el) => [...el.childNodes].some((n) => n.nodeType === 3 && n.textContent.includes('×')))
    .filter((el) => el.getClientRects().length > 0)
    .map((el) => {
      const r = el.getBoundingClientRect();
      const c = [...section.children].find((cell) => cell.contains(el)).getBoundingClientRect();
      return { text: el.textContent.trim(), left: r.left, right: r.right, cellLeft: c.left, cellRight: c.right };
    });
  return {
    boxes,
    drawn: section.innerText,
    titles: [...section.querySelectorAll('[title]')].map((el) => el.getAttribute('title')),
    scrollWidth: document.documentElement.scrollWidth,
    viewport: window.innerWidth
  };
}

/** Every event row's printed words and the title its fact carries. */
function readEventRows() {
  const list = document.querySelector('section[aria-label="Event stream"] ol');
  if (!list) return null;
  return [...list.children]
    .filter((li) => !li.hasAttribute('aria-label'))
    .map((li) => ({
      text: li.innerText.replace(/\s+/g, ' ').trim(),
      titles: [...li.querySelectorAll('[title]')].map((el) => el.getAttribute('title'))
    }));
}

/**
 * Each event row's lead and the gap drawn between its last glyph and the `·`
 * that follows it, in px. The text a script reads keeps the space either way;
 * what lost it was the drawn line, so the gap is measured off the glyphs.
 */
function readSeparatorGaps() {
  const list = document.querySelector('section[aria-label="Event stream"] ol');
  if (!list) return null;
  const glyph = (node, offset) => {
    const range = document.createRange();
    range.setStart(node, offset);
    range.setEnd(node, offset + 1);
    return range.getBoundingClientRect();
  };
  const lastText = (el) => {
    const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    let last = null;
    for (let n = walker.nextNode(); n; n = walker.nextNode()) if (n.data.trim() !== '') last = n;
    return last;
  };
  return [...list.querySelectorAll('[data-lead]')]
    .map((lead) => {
      const rest = lead.nextElementSibling;
      const node = rest ? document.createTreeWalker(rest, NodeFilter.SHOW_TEXT).nextNode() : null;
      const dot = node ? node.data.indexOf('·') : -1;
      const end = lastText(lead);
      if (!node || dot < 0 || !end) return null;
      const last = glyph(end, end.data.trimEnd().length - 1);
      return { text: `${lead.textContent}${rest.textContent}`.slice(0, 80), gap: glyph(node, dot).left - last.right };
    })
    .filter((row) => row !== null);
}

/** Each hold row that draws a command block: the command, and the row without it. */
function readCommandedHolds() {
  const section = document.querySelector('section[aria-label="Holds and blocks"]');
  if (!section) return null;
  return [...section.querySelectorAll('li')]
    .filter((li) => li.querySelector('code[data-command]'))
    .map((li) => {
      const code = li.querySelector('code[data-command]');
      const command = code.textContent.trim();
      // What the row draws under its block, other than a PR link: the action
      // line is the one thing R7 forbids there, marked or not.
      const after = [];
      for (let el = code.parentElement.nextElementSibling; el; el = el.nextElementSibling) {
        if (!el.hasAttribute('data-hold-link') && !el.hasAttribute('data-hold-do-this')) after.push(el.textContent.replace(/\s+/g, ' ').trim());
      }
      const rest = li.cloneNode(true);
      rest.querySelector('code[data-command]').parentElement.remove();
      return {
        command,
        rest: rest.textContent.replace(/\s+/g, ' ').trim(),
        actionLines: li.querySelectorAll('[data-hold-action]').length,
        after
      };
    });
}

/** Each readout's label and meta: lines drawn, box, and the cell's box. */
function readReadoutLines() {
  const section = document.querySelector('section[aria-label="Readouts"]');
  if (!section) return null;
  const lines = (el) => {
    const range = document.createRange();
    range.selectNodeContents(el);
    return new Set([...range.getClientRects()].filter((r) => r.width > 0).map((r) => Math.round(r.top))).size;
  };
  const box = (el) => {
    const r = el.getBoundingClientRect();
    return { left: r.left, right: r.right };
  };
  return [...section.children].map((cell) => {
    const head = cell.children[0];
    const label = head.children[0];
    // Below 1024 the cap line is the short reading and the whole one is
    // hidden, so the meta is the one span after the label that is drawn.
    const meta = [...head.children].slice(1).find((el) => el.getClientRects().length > 0) ?? null;
    return {
      label: label.textContent.trim(),
      labelLines: lines(label),
      labelBox: box(label),
      meta: meta && meta.textContent.trim(),
      metaTitle: meta && meta.getAttribute('title'),
      metaLines: meta && lines(meta),
      metaBox: meta && box(meta),
      cellBox: box(cell)
    };
  });
}

export async function checks({ check, visit, expect, sleep, sizes: { WIDTH, HEIGHT } }) {
  await check('no event row prints a snake_case word, and each code stays as a title', () =>
    visit(
      '/monitor',
      async (page) => {
        await page.locator('section[aria-label="Event stream"] ol li').first().waitFor({ timeout: 15_000 });
        await sleep(300);
        const rows = await page.evaluate(readEventRows);
        expect(rows !== null && rows.length > 0, 'the Event stream drew no rows');
        const text = rows.map((row) => row.text).join('\n');
        // The served codes arrived, worded — or the check proves nothing.
        expect(text.includes('in review → adversarial review'), `no row reads the served move:\n${text}`);
        expect(text.includes('base integration failed'), `no row reads the served reason:\n${text}`);
        for (const value of VERBATIM) {
          expect(text.includes(value), `no row prints the served "${value}" exactly:\n${text}`);
        }
        expect(!text.includes('classifier unavailable'), `an error was worded:\n${text}`);
        expect(!text.includes('quota probe skipped'), `an unknown reason was worded:\n${text}`);
        for (const row of rows) {
          // The served verbatim values are printed as written; any other
          // snake_case word on the row is still a code that was not worded.
          const hit = SNAKE.exec(VERBATIM.reduce((left, value) => left.replace(value, ''), row.text));
          expect(!hit, `an event row prints "${hit?.[0]}": ${row.text}`);
        }
        const titles = rows.flatMap((row) => row.titles);
        expect(titles.includes('in_review → adversarial_review'), `the move's code is no row's title: ${titles}`);
        expect(titles.includes('base_integration_failed'), `the reason's code is no row's title: ${titles}`);
      },
      { width: WIDTH, height: HEIGHT },
      withCodedEvents
    )
  );

  for (const size of [{ width: WIDTH, height: HEIGHT }, PHONE]) {
    await check(`every event row has a space before each · at ${size.width}px`, () =>
      visit(
        '/monitor',
        async (page) => {
          await page.locator('section[aria-label="Event stream"] ol li').first().waitFor({ timeout: 15_000 });
          await sleep(300);
          const rows = await page.evaluate(readEventRows);
          expect(rows !== null && rows.length > 0, 'the Event stream drew no rows');
          const dotted = rows.filter((row) => row.text.includes('·'));
          expect(dotted.length > 0, 'no event row draws a · separator, so none was checked');
          for (const row of dotted) {
            expect(!/\S·/.test(row.text), `an event row runs its words into the ·: ${row.text}`);
          }
          // The space after the lead is drawn, not just present in the text.
          const gaps = await page.evaluate(readSeparatorGaps);
          expect(gaps.length > 0, 'no event row has a lead followed by a ·, so no gap was measured');
          for (const row of gaps) {
            expect(row.gap >= 2, `"${row.text}" draws its · ${row.gap.toFixed(1)}px after its lead, with no space`);
          }
        },
        size
      )
    );

    await check(`the event stream's epic filter is chips, not a <select>, at ${size.width}px`, () =>
      visit(
        '/monitor',
        async (page) => {
          const stream = page.locator('section[aria-label="Event stream"]');
          await stream.locator('ol li').first().waitFor({ timeout: 15_000 });
          expect((await stream.locator('select').count()) === 0, 'the event stream still draws a <select>');
          const epics = stream.locator('[role="group"][aria-label="Epic"] button');
          const names = await epics.allTextContents();
          expect(names[0]?.trim() === 'All', `the epic chips start ${JSON.stringify(names)}, not with All`);
          expect(names.length > 1, `the epic chips offer ${JSON.stringify(names)}: no epic to pick`);
          const ref = names[1].trim();
          await epics.nth(1).click();
          await sleep(300);
          const epic = await page.evaluate(() => new URL(location.href).searchParams.get('epic'));
          expect(epic === ref, `a click on the ${ref} chip wrote epic=${epic}`);
          expect(
            (await epics.nth(1).getAttribute('aria-pressed')) === 'true',
            `the ${ref} chip is not pressed after its click`
          );
          if (size.width < 640) {
            for (const box of await epics.evaluateAll((els) => els.map((el) => el.getBoundingClientRect().height))) {
              expect(box >= 32, `an epic chip is ${box}px tall at ${size.width}px`);
            }
          }
        },
        size
      )
    );
  }

  for (const size of [{ width: WIDTH, height: HEIGHT }, PHONE]) {
    await check(`a hold row with a command block says the command once at ${size.width}px`, () =>
      visit(
        '/monitor',
        async (page) => {
          await page
            .locator('section[aria-label="Holds and blocks"] code[data-command]')
            .first()
            .waitFor({ timeout: 15_000 });
          await sleep(300);
          const holds = await page.evaluate(readCommandedHolds);
          expect(holds !== null, 'the Monitor drew no Holds and blocks section');
          expect(
            holds.some((hold) => hold.command === COMMAND),
            `the served hold drew no command block: ${holds.map((h) => h.command)}`
          );
          for (const hold of holds) {
            expect(
              !hold.rest.includes(hold.command),
              `a hold row says "${hold.command}" outside its command block: ${hold.rest}`
            );
            expect(
              hold.actionLines === 0 && hold.after.length === 0,
              `a hold row with a command block draws an action line under it: ${hold.after}`
            );
          }
        },
        size,
        withServedDashboard
      )
    );

    for (const payload of PAYLOADS) {
      await check(`every readout label and cap line is one line at ${size.width}px, ${payload.name} over cap`, () =>
        visit(
          '/monitor',
          async (page) => {
            await page.locator('section[aria-label="Readouts"]').waitFor({ timeout: 15_000 });
            await page
              .locator('section[aria-label="Readouts"] span:visible', { hasText: '×' })
              .first()
              .waitFor({ timeout: 15_000 });
            await sleep(300);
            const cells = await page.evaluate(readReadoutLines);
            expect(cells !== null && cells.length === 4, `the Readouts section drew ${cells?.length} cells`);
            const caps = cells.filter((cell) => cell.meta);
            expect(caps.length === 2, `${caps.length} readouts drew a cap line, not Today and This week`);
            for (const cap of caps) {
              expect(/^×(?:[\d.]+|99\+) over/.test(cap.meta), `the served caps did not arrive: "${cap.meta}"`);
            }
            if (payload.exact) {
              // The bounded reading at every width, the exact one as its title only.
              const { exact } = payload;
              const week = caps.find((cell) => cell.label.toLowerCase() === 'this week');
              const bounded = size.width < 1024 ? '×99+ over' : '×99+ over cap';
              expect(week, `no This week cap line among ${caps.map((cap) => cap.label)}`);
              expect(week.meta === bounded, `the far-over cap line reads "${week.meta}", not "${bounded}"`);
              expect(
                week.metaTitle === exact,
                `"${bounded}" carries "${week.metaTitle}" as its title, not "${exact}"`
              );
            }
            const drawn = await page.evaluate(readMultipliers);
            expect(drawn.boxes.length > 0, 'the Readouts drew no multiplier');
            for (const box of drawn.boxes) {
              expect(/^×(?:\d{1,2}\.\d|99\+) over/.test(box.text), `a multiplier box reads "${box.text}"`);
              expect(
                box.left >= box.cellLeft - 0.5 && box.right <= box.cellRight + 0.5,
                `"${box.text}" spans x=${Math.round(box.left)}–${Math.round(box.right)}, ` +
                  `outside its cell (x=${Math.round(box.cellLeft)}–${Math.round(box.cellRight)})`
              );
            }
            if (payload.exact) {
              expect(!drawn.drawn.includes(payload.exact), `"${payload.exact}" is drawn, not only a title`);
              expect(
                drawn.titles.filter((title) => title === payload.exact).length >= 1,
                `"${payload.exact}" is no title in the Readouts: ${drawn.titles}`
              );
            }
            expect(
              drawn.scrollWidth <= drawn.viewport,
              `the page scrolls sideways: ${drawn.scrollWidth}px wide in a ${drawn.viewport}px viewport`
            );
            for (const cell of cells) {
              expect(cell.labelLines === 1, `"${cell.label}" is drawn on ${cell.labelLines} lines`);
              expect(
                cell.labelBox.right <= cell.cellBox.right + 0.5,
                `"${cell.label}" runs out of its cell to x=${Math.round(cell.labelBox.right)}`
              );
              if (!cell.meta) continue;
              expect(cell.metaLines === 1, `"${cell.meta}" is drawn on ${cell.metaLines} lines`);
              expect(
                cell.metaBox.right <= cell.cellBox.right + 0.5 && cell.metaBox.left >= cell.labelBox.right,
                `"${cell.meta}" spans x=${Math.round(cell.metaBox.left)}–${Math.round(cell.metaBox.right)}, ` +
                  `outside its cell (to x=${Math.round(cell.cellBox.right)}) or over "${cell.label}"`
              );
            }
          },
          size,
          (page) => withServedDashboard(page, payload.caps)
        )
      );
    }
  }
}
