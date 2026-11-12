/**
 * One needs-you set behind every count, and a stall banner that is one
 * sentence (#2784).
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 *
 * Design review c5 found the counts disagreeing on live data: an Epics column
 * reading 9 over a legend reading ◆ 1, a header of 11 over a list totalling 10
 * (the eleventh an epic-less wait with no visible home), a magenta "◆ 0 needs
 * you" with nobody waiting, and a stall banner of six journal lines carrying
 * raw codes. Each check reads what the BROWSER drew, at both widths.
 */
// ui-check-owns: src/lib/shell/header-state.ts
// ui-check-owns: src/lib/shell/FactoryHeader.svelte
// ui-check-owns: src/lib/epic/attention.ts
// ui-check-owns: src/routes/(factory)/epics/+page.svelte
// ui-check-owns: src/routes/(factory)/epics/[ref]/+page.svelte
import { readFileSync } from 'node:fs';
import { FRONTEND } from '../ui-check-lib.mjs';

const served = JSON.parse(
  readFileSync(new URL('fixtures/factory/dashboard.json', FRONTEND), 'utf8')
);
const GO_LIVE = 'go-live-2026-09';
const ORPHAN_PR = 2608;

/** The committed set plus one epic-less wait — an orphan PR, as #2608 was. */
function withEpiclessWait(payload) {
  const orphan = {
    ...payload.rows[0],
    type: 'orphan_pr',
    consequence: 'hygiene',
    reason: 'orphan PR — no kew run recorded on its branch',
    sentence: 'orphan PR — no kew run recorded on its branch',
    primary_action: 'ack',
    issue_num: null,
    item_id: '',
    epic_ref: null,
    pr_number: ORPHAN_PR,
    url: `https://github.com/o/r/pull/${ORPHAN_PR}`,
    attention_kind: '',
    label: '',
    command: ''
  };
  return { ...payload, rows: [...payload.rows, orphan], needs_you_no_epic: 1 };
}

/** The same payload with nobody waiting on anything. */
function emptySet(payload) {
  return { ...payload, rows: [], needs_you_by_epic: {}, needs_you_no_epic: 0 };
}

/** Serve `/api/dashboard` (exactly that path) through *shape*. */
async function routeDashboard(page, shape) {
  await page.route(
    (url) => url.pathname === '/api/dashboard',
    async (route) => {
      const response = await route.fetch();
      await route.fulfill({ response, json: shape(await response.json()) });
    }
  );
}

/** Serve one epic's live read with its driver journaling `stalled`. */
async function routeStalled(page, ref) {
  await page.route(
    (url) => url.pathname === `/api/epics/${ref}`,
    async (route) => {
      const response = await route.fetch();
      const body = await response.json();
      const at = new Date(Date.now() - 30_000).toISOString();
      body.heartbeat = {
        ...body.heartbeat,
        last_tick_started_at: at,
        last_tick_ts: at,
        last_signal: 'stalled'
      };
      if (body.snapshot) body.snapshot = { ...body.snapshot, signal: 'stalled' };
      await route.fulfill({ response, json: body });
    }
  );
}

/** The header's needs-you number, or null when no counter is drawn. */
async function headerCount(page) {
  const el = page.locator('header [data-needs-you-count]');
  if ((await el.count()) === 0) return null;
  return Number(((await el.first().textContent()) ?? '').trim());
}

/** The epic legend's needs-you number; 0 when the legend draws none.
 *  Waits out the `?` the legend prints before the set has been read. */
async function legendCount(page) {
  await page.locator('[data-legend]').first().waitFor({ state: 'visible', timeout: 15_000 });
  await page.waitForFunction(
    () => {
      const el = document.querySelector('[data-epic-needs-you]');
      return el === null || /^\d+$/.test((el.textContent ?? '').trim());
    },
    null,
    { timeout: 15_000 }
  );
  const el = page.locator('[data-epic-needs-you]');
  if ((await el.count()) === 0) return 0;
  return Number(((await el.first().textContent()) ?? '').trim());
}

export async function checks({ browser, base, check, expect, sleep, sizes: { WIDTH, HEIGHT } }) {
  const sizes = [
    [WIDTH, HEIGHT],
    [390, 844]
  ];

  /** One fresh page at *width*, its routes installed before the first load. */
  async function withPage(width, height, routes, fn) {
    const ctx = await browser.newContext({ viewport: { width, height } });
    const page = await ctx.newPage();
    try {
      for (const install of routes) await install(page);
      return await fn(page);
    } finally {
      await page.close();
      await ctx.close();
    }
  }

  for (const [width, height] of sizes) {
    for (const [label, shape, epicless] of [
      ['the fixture', (p) => p, 0],
      ['the fixture plus an epic-less wait', withEpiclessWait, 1]
    ]) {
      await check(
        `each Epics NEEDS YOU cell equals its legend and the header is their sum plus epic-less waits — ${label} at ${width}px`,
        () =>
          withPage(width, height, [(page) => routeDashboard(page, shape)], async (page) => {
            await page.goto(`${base}/epics`, { waitUntil: 'load' });
            await page.locator('[data-epic-row]').first().waitFor({ state: 'attached', timeout: 15_000 });
            await page
              .locator('header [data-needs-you-count]')
              .first()
              .waitFor({ state: 'attached', timeout: 15_000 });
            await sleep(700);
            const column = await page.evaluate(() =>
              Object.fromEntries(
                [...document.querySelectorAll('[data-epic-row]')].map((row) => [
                  row.getAttribute('data-epic-row'),
                  Number(row.querySelector('[data-epic-row-needs-you]')?.textContent?.trim() ?? 0)
                ])
              )
            );
            const noEpicEl = page.locator('[data-no-epic-row] [data-no-epic-needs-you]');
            const noEpic =
              (await noEpicEl.count()) === 0 ? 0 : Number(((await noEpicEl.textContent()) ?? '').trim());
            const header = await headerCount(page);

            const refs = Object.keys(column);
            expect(refs.length > 0, `${width}px: the Epics list drew no rows`);
            expect(
              Object.values(column).some((n) => n > 0),
              `${width}px: no Epics row reads a needs-you count, so agreement on 0 proves nothing`
            );
            expect(
              noEpic === epicless,
              `${width}px: the "no epic" row reads ${noEpic}; the set holds ${epicless} epic-less wait(s)`
            );
            const sum = Object.values(column).reduce((a, b) => a + b, 0);
            expect(
              header === sum + noEpic,
              `${width}px: the header reads ${header}; the column sums to ${sum} plus ${noEpic} epic-less`
            );

            for (const ref of refs) {
              await page.goto(`${base}/epics/${ref}`, { waitUntil: 'load' });
              const legend = await legendCount(page);
              expect(
                legend === column[ref],
                `${width}px: ${ref}'s Epics cell reads ${column[ref]} and its legend ◆ ${legend}`
              );
            }
          })
      );
    }

    await check(`the header draws no magenta diamond over an empty set at ${width}px`, () =>
      withPage(width, height, [(page) => routeDashboard(page, emptySet)], async (page) => {
        await page.goto(`${base}/work`, { waitUntil: 'load' });
        await page.locator('header').first().waitFor({ state: 'visible', timeout: 15_000 });
        // The counter is drawn only once the set is read; wait for the payload
        // to land rather than passing on a header that has read nothing yet.
        await page.waitForResponse((r) => new URL(r.url()).pathname === '/api/dashboard', {
          timeout: 15_000
        });
        await sleep(700);
        const read = await page.evaluate(() => ({
          diamonds: document.querySelectorAll('header svg rect[transform^="rotate(45"]').length,
          words: [...document.querySelectorAll('header *')]
            .filter((el) => el.children.length === 0)
            .map((el) => (el.textContent ?? '').trim())
            .filter((t) => /needs you/.test(t))
        }));
        expect(read.diamonds === 0, `${width}px: the header draws ${read.diamonds} needs-you diamond(s)`);
        expect(
          (await headerCount(page)) === null,
          `${width}px: the header still prints a needs-you count over an empty set`
        );
        expect(read.words.length === 0, `${width}px: the header still says ${JSON.stringify(read.words)}`);
      })
    );

    for (const [label, shape, waits] of [
      ['with waits in its bucket', (p) => p, true],
      [
        'with none',
        (p) => ({ ...p, rows: p.rows.filter((r) => r.epic_ref !== GO_LIVE), needs_you_by_epic: {} }),
        false
      ]
    ]) {
      await check(`the stall banner is one sentence with a link to Work, ${label}, at ${width}px`, () =>
        withPage(
          width,
          height,
          [(page) => routeDashboard(page, shape), (page) => routeStalled(page, GO_LIVE)],
          async (page) => {
            await page.goto(`${base}/epics/${GO_LIVE}`, { waitUntil: 'load' });
            const banner = page.locator('[data-stall-banner]');
            await banner.first().waitFor({ state: 'visible', timeout: 15_000 });
            const legend = await legendCount(page);
            await sleep(700);
            const read = await banner.first().evaluate((el) => ({
              text: (el.textContent ?? '').replace(/\s+/g, ' ').trim(),
              blocks: [...el.querySelectorAll('div, p, li, code, pre')].length,
              links: [...el.querySelectorAll('a')].map((a) => ({
                href: a.getAttribute('href'),
                text: (a.textContent ?? '').replace(/\s+/g, ' ').trim()
              }))
            }));
            const sentence = 'stalled — the driver stopped making progress.';
            expect(waits === legend > 0, `${width}px: ${GO_LIVE}'s legend reads ${legend} ${label}`);
            expect(read.blocks === 0, `${width}px: the banner holds ${read.blocks} line(s) under its sentence`);
            expect(!/[`_]/.test(read.text), `${width}px: the banner prints a raw code: "${read.text}"`);
            if (legend > 0) {
              const words = `${legend} ${legend === 1 ? 'wait' : 'waits'} on Work →`;
              expect(
                read.text === `${sentence} ${words}`,
                `${width}px: the banner reads "${read.text}", not "${sentence} ${words}"`
              );
              expect(
                read.links.length === 1 &&
                  read.links[0].href === '/work?filter=needs-you' &&
                  read.links[0].text === words,
                `${width}px: the banner's links are ${JSON.stringify(read.links)}`
              );
            } else {
              expect(read.text === sentence, `${width}px: the banner reads "${read.text}"`);
              expect(read.links.length === 0, `${width}px: a link to no waits: ${JSON.stringify(read.links)}`);
            }
          }
        )
      );
    }
  }
}
