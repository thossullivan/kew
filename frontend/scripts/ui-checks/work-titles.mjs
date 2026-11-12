/**
 * Titles that read (#2848): a Work row's title keeps two lines beside its
 * reason at a phone's width (P14), a status word never breaks over two lines
 * (P19), and a title's markdown backticks draw as inline mono on Work, the
 * epic page and the palette rather than as the character (P13).
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 */
// ui-check-owns: src/routes/(factory)/work/+page.*
// ui-check-owns: src/lib/ui/Title.svelte
// ui-check-owns: src/lib/factory/names.ts
// ui-check-owns: src/lib/epic/DagView.svelte
// ui-check-owns: src/routes/(factory)/epics/[ref]/+page.*
// ui-check-owns: src/lib/palette/Palette.svelte
import { EPIC, SETTLE_MS } from '../ui-check-lib.mjs';

const PHONE = { width: 390, height: 844 };
/** The go-live fixture, whose SFD-204 → SFD-201 chain folds to a "2 done" card
 * that draws its members' titles as one joined caption (`epic-done-groups.mjs`). */
const GO_LIVE = '/epics/go-live-2026-09';
/** A state word no table knows: `stateWord` echoes it, at 26 characters. */
const LONG_STATE = 'awaiting_external_approval';
/** …as the mark prints it: its underscores read as spaces. */
const LONG_WORD = 'awaiting external approval';
const ROWS = 'aside[aria-label="Issues"] a[data-item]';
/** A row's title: its second cell, as `work-rows.mjs` reads it. */
const TITLE = `${ROWS} > span:nth-of-type(2)`;

/**
 * Every `title` the API serves, led by a code span — the live title shape that
 * printed "`kew drive install` as a…". The committed fixtures carry no
 * backtick, so without this the check below would pass on any code.
 */
const CODE_LED = '`kew drive install` as a ';
function codeLed(value) {
  if (Array.isArray(value)) return value.map(codeLed);
  if (value === null || typeof value !== 'object') return value;
  return Object.fromEntries(
    Object.entries(value).map(([key, inner]) => [
      key,
      key === 'title' && typeof inner === 'string' && inner !== '' ? CODE_LED + inner : codeLed(inner)
    ])
  );
}
const withCodeTitles = (page) =>
  page.route(
    (url) => url.pathname.startsWith('/api/'),
    async (route) => {
      // A live stream never ends, so it cannot be read whole: let it through.
      if ((route.request().headers().accept ?? '').includes('text/event-stream')) return route.continue();
      const response = await route.fetch();
      const type = response.headers()['content-type'] ?? '';
      if (!type.includes('json')) return route.fulfill({ response });
      const body = JSON.stringify(codeLed(await response.json()));
      return route.fulfill({ response, body });
    }
  );

/** Every Work item served in *state*, with no exception to override it. */
const withLongState = (page) =>
  page.route(
    (url) => url.pathname === '/api/dashboard/work',
    async (route) => {
      const response = await route.fetch();
      const body = await response.json();
      for (const view of body.views ?? []) {
        view.execution.state = LONG_STATE;
        view.exception.state = 'none';
      }
      return route.fulfill({ response, json: body });
    }
  );

/** What a page names in text a person never reads: aria-label and title. */
const ticksIn = (page) =>
  page.evaluate(() =>
    [...document.querySelectorAll('[aria-label], [title]')]
      .flatMap((el) => [el.getAttribute('aria-label'), el.getAttribute('title')])
      .filter((t) => t !== null && t.includes('`'))
  );

/** Each Work row's title box: its lines on screen and the lines it needs. */
const readTitles = (page) =>
  page.locator(TITLE).evaluateAll((titles) =>
    titles.map((el) => {
      const line = parseFloat(getComputedStyle(el).lineHeight);
      // The lines the title would take unclamped, at the same width and font.
      const free = el.cloneNode(true);
      free.style.cssText = `display:block;-webkit-line-clamp:none;overflow:visible;width:${el.clientWidth}px`;
      el.parentElement.appendChild(free);
      const needs = Math.round(free.getBoundingClientRect().height / line);
      free.remove();
      return {
        item: el.closest('a').dataset.item,
        reason: el.parentElement.querySelector('[data-row-reason]') !== null,
        shown: Math.round(el.getBoundingClientRect().height / line),
        needs
      };
    })
  );

export async function checks({ browser, base, check, visit, expect, sleep, sizes: { WIDTH, HEIGHT } }) {
  await check('a Work row title shows two lines before it clamps at 390px, reason or not', () =>
    visit(
      '/work',
      async (page) => {
        await page.locator(ROWS).first().waitFor({ state: 'visible', timeout: 15_000 });
        await sleep(SETTLE_MS);
        const titles = await readTitles(page);
        // Not vacuous: a row WITH a reason line has a title long enough to
        // need a second line at 390 — the row P14 cut to three words.
        expect(
          titles.some((t) => t.reason && t.needs >= 2),
          `no row with a reason has a two-line title to judge: ${JSON.stringify(titles)}`
        );
        for (const t of titles) {
          expect(
            t.shown >= Math.min(2, t.needs),
            `${t.item}'s title shows ${t.shown} line(s) of the ${t.needs} it needs`
          );
        }
      },
      PHONE
    )
  );

  await check('no status word on Work breaks over two lines or into its title at 390px', () =>
    visit(
      '/work',
      async (page) => {
        await page.locator(ROWS).first().waitFor({ state: 'visible', timeout: 15_000 });
        await sleep(SETTLE_MS);
        const words = await page.locator(ROWS).evaluateAll((rows) =>
          rows.map((row) => {
            const cell = row.lastElementChild;
            const word = cell.lastElementChild.lastElementChild;
            const title = row.children[1].getBoundingClientRect();
            const box = word.getBoundingClientRect();
            return {
              item: row.dataset.item,
              text: word.textContent.trim(),
              lines: Math.round(box.height / parseFloat(getComputedStyle(word).lineHeight)),
              gap: Math.min(...[...cell.querySelectorAll('*')].map((el) => el.getBoundingClientRect().left)) - title.right
            };
          })
        );
        expect(words.length > 0, 'the Work list has no rows');
        for (const w of words) {
          expect(w.lines === 1, `${w.item}'s "${w.text}" takes ${w.lines} lines`);
          expect(w.gap >= 0, `${w.item}'s state cell runs ${-w.gap}px into its title`);
        }
      },
      PHONE
    )
  );

  await check('a long unknown Work state ends in an ellipsis inside its cell at 390px', () =>
    visit(
      '/work',
      async (page) => {
        await page.locator(ROWS).first().waitFor({ state: 'visible', timeout: 15_000 });
        await sleep(SETTLE_MS);
        const cells = await page.locator(ROWS).evaluateAll((rows) =>
          rows.map((row) => {
            const cell = row.lastElementChild;
            const word = cell.lastElementChild.lastElementChild;
            const box = cell.getBoundingClientRect();
            // The boxes, not the marks' paint: a diamond's halo is drawn past
            // its box on purpose (StatusMark's `overflow: visible`).
            const inner = [...cell.querySelectorAll('span')].map((el) => el.getBoundingClientRect());
            return {
              item: row.dataset.item,
              text: word.textContent.trim(),
              hover: word.getAttribute('title'),
              clipped: word.scrollWidth > word.clientWidth,
              lines: Math.round(word.getBoundingClientRect().height / parseFloat(getComputedStyle(word).lineHeight)),
              spill: Math.max(...inner.map((r) => Math.max(box.left - r.left, r.right - box.right))),
              gap: Math.min(...inner.map((r) => r.left)) - row.children[1].getBoundingClientRect().right
            };
          })
        );
        const long = cells.filter((c) => c.text === LONG_WORD);
        expect(long.length > 0, `no Work row reads the served state: ${JSON.stringify(cells)}`);
        for (const c of long) {
          expect(c.lines === 1, `${c.item}'s "${c.text}" takes ${c.lines} lines`);
          expect(c.spill <= 0.5, `${c.item}'s state runs ${c.spill}px out of its cell`);
          expect(c.gap >= 0, `${c.item}'s state cell runs ${-c.gap}px into its title`);
          expect(c.hover === LONG_WORD, `${c.item}'s clipped state hovers "${c.hover}"`);
        }
        // Not vacuous: the word really is wider than the 108px cell, and cut.
        expect(long.some((c) => c.clipped), `"${LONG_WORD}" is not cut, so no ellipsis is judged`);
        const wide = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
        expect(wide <= 0, `the Work page scrolls ${wide}px sideways`);
      },
      PHONE,
      withLongState
    )
  );

  for (const size of [{ width: WIDTH, height: HEIGHT }, PHONE]) {
    await check(`a Work title draws its backticks as mono at ${size.width}px`, () =>
      visit(
        '/work',
        async (page) => {
          await page.locator(ROWS).first().waitFor({ state: 'visible', timeout: 15_000 });
          await sleep(SETTLE_MS);
          const texts = await page.locator(TITLE).allTextContents();
          expect(
            texts.some((t) => t.includes('kew drive install')),
            `no Work title carries the served code span: ${JSON.stringify(texts)}`
          );
          for (const t of texts) expect(!t.includes('`'), `a Work title reads "${t}"`);
          expect((await page.locator(`${TITLE} code`).count()) > 0, 'no title draws a mono span');
          // And the open item's own heading, the same title at 26px.
          await page.locator(ROWS).first().click();
          const heading = page.locator('section[aria-label="Item detail"] h1:visible').first();
          await heading.waitFor({ state: 'visible', timeout: 15_000 });
          await sleep(SETTLE_MS);
          const h1 = ((await heading.textContent()) ?? '').trim();
          expect(!h1.includes('`'), `the item heading reads "${h1}"`);
        },
        size,
        withCodeTitles
      )
    );

    await check(`no title on the epic page prints a backtick at ${size.width}px`, () =>
      visit(
        EPIC,
        async (page) => {
          const card = page.locator('.dag .svelte-flow__node .tt[title]').first();
          await card.waitFor({ state: 'attached', timeout: 15_000 });
          await sleep(SETTLE_MS);
          const read = await page.evaluate(() => ({
            text: document.body.innerText,
            titles: [...document.querySelectorAll('[title]')].map((el) => el.getAttribute('title')),
            mono: document.querySelectorAll('.tt code').length
          }));
          expect(read.text.includes('kew drive install'), 'the epic page shows no served title');
          const ticked = read.text.split('\n').filter((line) => line.includes('`'));
          expect(ticked.length === 0, `the epic page prints: ${ticked.join(' | ')}`);
          const named = read.titles.filter((t) => t.includes('`'));
          expect(named.length === 0, `a title attribute reads: ${named.join(' | ')}`);
          expect(read.mono > 0, 'no epic card draws a mono span');
          // A screen reader reads a node's aria-label, not its card.
          const labels = await page
            .locator('.dag .svelte-flow__node[aria-label]')
            .evaluateAll((nodes) => nodes.map((n) => n.getAttribute('aria-label')));
          expect(
            labels.some((l) => l.includes('kew drive install')),
            `no epic node names a served title: ${JSON.stringify(labels)}`
          );
          const spoken = await ticksIn(page);
          expect(spoken.length === 0, `an accessible name reads: ${spoken.join(' | ')}`);
        },
        size,
        withCodeTitles
      )
    );

    await check(`a folded done group's caption prints no backtick at ${size.width}px`, () =>
      visit(
        GO_LIVE,
        async (page) => {
          const group = page.locator('.dag .svelte-flow__node[data-id^="done:"]').first();
          await group.waitFor({ state: 'attached', timeout: 15_000 });
          await sleep(SETTLE_MS);
          const caption = ((await group.locator('.tt').textContent()) ?? '').trim();
          // Not vacuous: the caption is the members' served titles, joined.
          expect(caption.includes('kew drive install'), `the group card reads "${caption}"`);
          expect(!caption.includes('`'), `the group card reads "${caption}"`);
          const spoken = await ticksIn(page);
          expect(spoken.length === 0, `an accessible name reads: ${spoken.join(' | ')}`);
        },
        size,
        withCodeTitles
      )
    );
  }

  for (const width of [WIDTH, 390]) {
    await check(`no palette title prints a backtick at ${width}px`, async () => {
      const ctx = await browser.newContext({ viewport: { width, height: width === 390 ? 844 : HEIGHT } });
      const page = await ctx.newPage();
      try {
        await withCodeTitles(page);
        await page.goto(`${base}/work`, { waitUntil: 'load' });
        await page.locator(ROWS).first().waitFor({ state: 'visible', timeout: 15_000 });
        await sleep(SETTLE_MS);
        await page.keyboard.press('Control+k');
        const dialog = page.locator('[role="dialog"][aria-label="Go to"]');
        await dialog.waitFor({ state: 'visible', timeout: 5_000 });
        await page.keyboard.type('kew drive install');
        await sleep(SETTLE_MS);
        const options = await dialog.locator('[role="option"]').allTextContents();
        expect(
          options.some((o) => o.includes('kew drive install')),
          `no palette entry shows a served title: ${JSON.stringify(options)}`
        );
        for (const o of options) expect(!o.includes('`'), `a palette entry reads "${o}"`);
        expect((await dialog.locator('[role="option"] code').count()) > 0, 'no palette title draws a mono span');
      } finally {
        await ctx.close();
      }
    });
  }
}
