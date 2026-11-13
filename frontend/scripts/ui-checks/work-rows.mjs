/**
 * Work rows and detail (#2786): a row titled by its issue, the lower panels
 * closed on load, and a No. cell that stays in its column.
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 */
// ui-check-owns: src/routes/(factory)/work/**
// ui-check-owns: src/lib/work/entry-detail.ts
// ui-check-owns: src/lib/shell/FactoryHeader.svelte
import { readFileSync } from 'node:fs';
import { FRONTEND, SETTLE_MS, widestBox } from '../ui-check-lib.mjs';

const GO_LIVE = '/epics/go-live-2026-09';

/** A Work row's cells, by the item id it selects. */
const workRow = (page, item) =>
  page.locator(`aside[aria-label="Issues"] a[data-item="${item}"]`).first();

export async function checks({ check, visit, expect, sleep, sizes: { WIDTH, HEIGHT } }) {
  const SIZES = [
    { width: WIDTH, height: HEIGHT },
    { width: 390, height: 844 }
  ];

  /**
   * D14 — SFD-229 was titled by its hold on Work ("the review round cap was
   * reached…") and by its issue on the epic card ("budget caps per epic…").
   * The card's title is read off the canvas, not typed here, so the two
   * surfaces are held to each other rather than to a string.
   */
  const readCardTitle = () =>
    visit(GO_LIVE, async (page) => {
      const card = page.locator('.dag .svelte-flow__node[data-id="SFD-229"] .tt').first();
      await card.waitFor({ state: 'attached', timeout: 15_000 });
      return ((await card.getAttribute('title')) ?? '').trim();
    });
  for (const size of SIZES) {
    await check(`SFD-229's Work title is its epic card's title at ${size.width}px`, async () => {
      const cardTitle = await readCardTitle();
      await visit(
        '/work',
        async (page) => {
          const row = workRow(page, 'SFD-229');
          await row.waitFor({ state: 'visible', timeout: 15_000 });
          expect(cardTitle !== '', 'the epic card for SFD-229 has no title to compare');
          const title = ((await row.locator('> span').nth(1).textContent()) ?? '').trim();
          expect(title === cardTitle, `Work reads "${title}", the card "${cardTitle}"`);
          // The hold is still on the row, as the quiet line under the title.
          const reason = ((await row.locator('[data-row-reason]').textContent()) ?? '').trim();
          expect(
            reason.includes('the review round cap was reached'),
            `the row's reason line reads "${reason}"`
          );
          const titleBox = await row.locator('> span').nth(1).boundingBox();
          const reasonBox = await row.locator('[data-row-reason]').boundingBox();
          const rowBox = await row.boundingBox();
          expect(
            titleBox && reasonBox && rowBox && reasonBox.y >= titleBox.y + titleBox.height - 1,
            'the reason line is not under the title'
          );
          expect(
            reasonBox.y + reasonBox.height <= rowBox.y + rowBox.height + 0.5,
            `the reason line spills out of its ${rowBox.height}px row`
          );
        },
        size
      );
    });
  }

  /**
   * D16 — the description and the evidence ledger start closed, each behind a
   * disclosure whose head says what is inside it, and one press opens it.
   */
  for (const size of SIZES) {
    await check(`Description and Evidence are collapsed on load at ${size.width}px`, () =>
      visit(
        '/work?item=2055',
        async (page, problems) => {
          const pane = page.locator('section[aria-label="Item detail"]');
          await pane.waitFor({ state: 'visible', timeout: 15_000 });
          // Both reads have landed once each head names what it holds. Read off
          // the text, not off a glyph box: what is checked is the words.
          await page.waitForFunction(
            () => {
              const head = (label) =>
                document.querySelector(`button[data-panel="${label}"]`)?.textContent ?? '';
              return head('Description').includes('word') && head('Evidence ledger').includes('attested');
            },
            null,
            { timeout: 15_000 }
          );
          await sleep(SETTLE_MS);
          for (const label of ['Description', 'Evidence ledger']) {
            const toggle = pane.locator(`button[data-panel="${label}"]`);
            expect((await toggle.count()) === 1, `no ${label} disclosure`);
            expect(
              (await toggle.getAttribute('aria-expanded')) === 'false',
              `${label} is open on load`
            );
            const body = page.locator(`[id="${await toggle.getAttribute('aria-controls')}"]`);
            expect(!(await body.isVisible()), `${label}'s contents are on screen while closed`);
            await toggle.click();
            expect(
              (await toggle.getAttribute('aria-expanded')) === 'true',
              `${label} did not open on a press`
            );
            expect(await body.isVisible(), `${label}'s contents did not appear when opened`);
          }
          expect(problems.length === 0, problems.join('\n'));
        },
        size
      )
    );
  }

  /**
   * N3 — a stalled epic's row printed its whole ref in the 56px No. column, and
   * a live `supervised-daemon` ran into the title cell. The dashboard is the
   * committed fixture with one stalled-epic entry added under that live ref.
   */
  const dashboard = JSON.parse(
    readFileSync(new URL('fixtures/factory/dashboard.json', FRONTEND), 'utf8')
  );
  const dashboardFixture = structuredClone(dashboard);
  dashboard.rows.push({
    type: 'stalled_epic',
    consequence: 'epic',
    age: dashboard.rows[0].age,
    reason: 'driver gone — supervised-daemon — no tick in 6d 8h',
    primary_action: 'resume',
    issue_num: null,
    item_id: null,
    estimate_usd: null,
    epic_ref: 'supervised-daemon',
    url: null,
    pr_number: null,
    attention_kind: '',
    label: '',
    command: 'kew epic run supervised-daemon',
    sentence: 'the driver is gone — no tick in 6d 8h',
    link: null
  });
  const stalled = 'stalled_epic:supervised-daemon';
  for (const size of SIZES) {
    await check(`a stalled-epic row's No. cell stays out of its title at ${size.width}px`, () =>
      visit(
        '/work',
        async (page, problems) => {
          const row = workRow(page, stalled);
          await row.waitFor({ state: 'visible', timeout: 15_000 });
          const number = row.locator('> span').nth(0);
          const title = row.locator('> span').nth(1);
          const n = await number.boundingBox();
          const t = await title.boundingBox();
          expect(n && t, 'the stalled-epic row has no No. or title box');
          expect(n.x + n.width <= t.x + 0.5, `No. ends at ${n.x + n.width}, the title starts at ${t.x}`);
          // The cell's own text must not draw past its box either: a box that
          // keeps its width while its glyphs overflow is the same overlap.
          const spill = await number.evaluate((el) => el.scrollWidth - el.clientWidth);
          const clipped = await number.evaluate((el) => getComputedStyle(el).overflow);
          expect(spill <= 0 || clipped === 'hidden', `the No. text overflows its cell by ${spill}px`);
          expect(((await title.textContent()) ?? '').trim() === 'supervised-daemon', 'the title is not the epic ref');
          expect((await number.getAttribute('title')) !== null, 'the No. cell carries no full id');
          expect(problems.length === 0, problems.join('\n'));
        },
        size,
        (page) =>
          page.route(
            (url) => url.pathname === '/api/dashboard',
            (route) =>
              route.fulfill({
                status: 200,
                contentType: 'application/json',
                body: JSON.stringify(dashboard)
              })
          )
      )
    );
  }

  /**
   * #2947 (#3467 D18) — a row's reason gets two lines, then an ellipsis. At
   * 390 the one nowrap line read "the review round cap w…" and the rest lived
   * only in a `title` a finger cannot open. SFD-229's served sentence is
   * swapped for one of a known length. Negative control: on the old
   * `whitespace-nowrap` span the 60-character reason's `scrollWidth` passes
   * its `clientWidth` at 390 and the 200-character one draws one line.
   */
  const REASON_60 = 'the review pool ran dry; unhold it to restart the next round';
  const REASON_200 =
    'the review round cap was reached after six rounds on this item, the pool of ten dollars ' +
    'is spent and twelve more were charged past it, so the driver holds the node until a person ' +
    'unholds it here';
  const withReason = (sentence) => {
    const served = structuredClone(dashboardFixture);
    const held = served.rows.find((row) => row.item_id === 'SFD-229');
    held.sentence = sentence;
    return served;
  };
  const serve = (payload) => (page) =>
    page.route(
      (url) => url.pathname === '/api/dashboard',
      (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(payload) })
    );
  /** A row's reason line: its text, its box, its line height and what it hides. */
  const readReason = (row) =>
    row.locator('[data-row-reason]').evaluate((el) => {
      const style = getComputedStyle(el);
      const box = el.getBoundingClientRect();
      return {
        text: (el.textContent ?? '').trim(),
        height: box.height,
        line: parseFloat(style.lineHeight),
        clamp: style.webkitLineClamp,
        sideways: el.scrollWidth > el.clientWidth,
        hidden: el.scrollHeight > el.clientHeight + 1
      };
    });
  for (const width of [390, 768, WIDTH]) {
    const size = { width, height: width === WIDTH ? HEIGHT : width === 768 ? 1024 : 844 };
    await check(`a 60-character reason shows whole on its Work row at ${width}px`, () =>
      visit(
        '/work',
        async (page) => {
          expect(REASON_60.length === 60, `the reason is ${REASON_60.length} characters, not 60`);
          const row = workRow(page, 'SFD-229');
          await row.waitFor({ state: 'visible', timeout: 15_000 });
          const reason = await readReason(row);
          expect(reason.text === REASON_60, `${width}px: the row's reason reads "${reason.text}"`);
          expect(!reason.sideways, `${width}px: the reason runs past its cell`);
          expect(!reason.hidden, `${width}px: the reason is cut (${reason.height}px drawn)`);
          expect(reason.height <= 2 * reason.line + 1, `${width}px: the reason draws ${reason.height}px, over two lines`);
        },
        size,
        serve(withReason(REASON_60))
      )
    );
  }
  for (const width of [390, WIDTH]) {
    const size = { width, height: width === WIDTH ? HEIGHT : 844 };
    await check(`a 200-character reason draws two lines and an ellipsis at ${width}px`, () =>
      visit(
        '/work',
        async (page) => {
          const row = workRow(page, 'SFD-229');
          await row.waitFor({ state: 'visible', timeout: 15_000 });
          const reason = await readReason(row);
          const lines = Math.round(reason.height / reason.line);
          expect(reason.text === REASON_200, `${width}px: the row's reason reads "${reason.text}"`);
          expect(!reason.sideways, `${width}px: the reason runs past its cell`);
          if (width === 390) {
            expect(lines === 2, `${width}px: the reason draws ${lines} line(s), not two`);
            // Cut, with the ellipsis the clamp draws, and whole in its title.
            expect(reason.hidden && reason.clamp === '2', `${width}px: the reason is not clamped at two lines`);
          } else {
            expect(lines <= 2, `${width}px: the reason draws ${lines} lines`);
          }
          expect((await row.locator('[data-row-reason]').getAttribute('title')) === REASON_200, 'the reason lost its title');
          const rowBox = await row.boundingBox();
          const box = await row.locator('[data-row-reason]').boundingBox();
          expect(box.y + box.height <= rowBox.y + rowBox.height + 0.5, `${width}px: the reason spills out of its row`);
        },
        size,
        serve(withReason(REASON_200))
      )
    );
  }
  await check(`no reason draws past two lines, and a row without one keeps its height, at ${WIDTH}px`, () =>
    visit('/work', async (page) => {
      const rows = page.locator('aside[aria-label="Issues"] a[data-item]');
      await rows.first().waitFor({ state: 'visible', timeout: 15_000 });
      const read = await rows.evaluateAll((els) =>
        els.map((el) => {
          const reason = el.querySelector('[data-row-reason]');
          return {
            item: el.getAttribute('data-item'),
            row: el.getBoundingClientRect().height,
            reason: reason ? reason.getBoundingClientRect().height : null,
            line: reason ? parseFloat(getComputedStyle(reason).lineHeight) : 0
          };
        })
      );
      expect(read.some((r) => r.reason !== null), 'no row carries a reason to measure');
      expect(read.some((r) => r.reason === null), 'every row carries a reason; none to hold at 56px');
      for (const r of read) {
        if (r.reason === null) expect(Math.abs(r.row - 56) < 1, `${r.item} has no reason and is ${r.row}px tall`);
        else expect(r.reason <= 2 * r.line + 1, `${r.item}'s reason draws ${r.reason}px, over two lines`);
      }
    })
  );

  /**
   * #3467 D30 — quiet states print no noise: no key hints over a list that
   * shows no rows, no external link named a bare "Open", and no "0 working"
   * in the header of a factory running nothing. Negative control: on the old
   * page the Keyboard strip is drawn under "No issue matches", SFD-229's
   * Linear link reads "Open ↗" and the header reads "0 working".
   */
  await check(`no key hints over a list that shows no rows at ${WIDTH}px`, () =>
    visit('/work', async (page) => {
      const keys = page.locator('aside[aria-label="Issues"] [aria-label="Keyboard"]');
      await workRow(page, 'SFD-229').waitFor({ state: 'visible', timeout: 15_000 });
      expect(await keys.isVisible(), 'the control: no key hints are drawn over the full list');
      await page.locator('#work-search').fill('no-item-is-named-this');
      await page.getByText('No issue matches this search or filter.').waitFor({ state: 'visible', timeout: 5_000 });
      expect(!(await keys.isVisible()), 'the key hints are drawn over an empty list');
    })
  );
  for (const size of SIZES) {
    await check(`SFD-229's external link says what it opens at ${size.width}px`, () =>
      visit(
        '/work?item=SFD-229',
        async (page) => {
          const block = page.locator('main section[aria-label^="Needs you"]').first();
          await block.waitFor({ state: 'visible', timeout: 15_000 });
          const labels = await page
            .locator('main a[target="_blank"]')
            .evaluateAll((els) => els.map((el) => (el.textContent ?? '').replace('↗', '').trim()));
          expect(labels.includes('Issue SFD-229'), `SFD-229's links read ${JSON.stringify(labels)}`);
          expect(!labels.includes('Open'), `an external link is named a bare "Open": ${JSON.stringify(labels)}`);
        },
        size
      )
    );
  }
  await check(`the header prints no "0 working" at ${WIDTH}px`, () =>
    visit(
      '/work',
      async (page) => {
        await workRow(page, 'SFD-229').waitFor({ state: 'visible', timeout: 15_000 });
        const header = ((await page.locator('header').first().textContent()) ?? '').replace(/\s+/g, ' ');
        expect(!/\b0 working\b/.test(header), `the header reads "${header.trim()}"`);
      },
      undefined,
      (page) =>
        page.route(
          (url) => url.pathname === '/api/runs',
          (route) => route.fulfill({ status: 200, contentType: 'application/json', body: '[]' })
        )
    )
  );

  /**
   * #3467 D30 — with one wait, the pane expands it and prints no lone
   * "Open #2055" line under it: the block's links already reach the item.
   * From 1024 up only: below it the list takes the one column until a row is
   * chosen, so no unselected pane is drawn.
   */
  const soleWait = { ...dashboardFixture, rows: dashboardFixture.rows.filter((row) => row.item_id === '2055') };
  for (const size of [SIZES[0], { width: 1024, height: 768 }]) {
    await check(`a sole wait has no lone "Open #…" line under its block at ${size.width}px`, () =>
      visit(
        '/work',
        async (page) => {
          const pane = page.locator('main section[aria-label="Needs you"]').first();
          await pane.locator('section[aria-label^="Needs you"]').first().waitFor({ state: 'visible', timeout: 15_000 });
          const lone = await pane
            .locator('a')
            .evaluateAll((els) => els.map((el) => (el.textContent ?? '').trim()).filter((t) => /^Open (#|the item)/.test(t)));
          expect(lone.length === 0, `${size.width}px: the sole wait draws ${JSON.stringify(lone)} under its block`);
        },
        size,
        serve(soleWait)
      )
    );
  }

  await check('the Work list does not scroll sideways at 390px with a stalled epic', () =>
    visit(
      '/work',
      async (page) => {
        await workRow(page, stalled).waitFor({ state: 'visible', timeout: 15_000 });
        const wide = await widestBox(page);
        expect(wide.w <= wide.c, `the page scrolls sideways: ${wide.w} > ${wide.c} (${wide.culprit})`);
      },
      { width: 390, height: 844 },
      (page) =>
        page.route(
          (url) => url.pathname === '/api/dashboard',
          (route) =>
            route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(dashboard) })
        )
    )
  );
}
