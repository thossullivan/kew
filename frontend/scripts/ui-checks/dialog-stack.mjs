/**
 * Dialogs stacked over dialogs, and the page behind every modal (#2841).
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 *
 * Every open dialog's Tab keeper (`use:contained`) listens on the document, and
 * the launchpad drawer's ran first: with the palette or help opened over the
 * drawer, Tab never left the palette's input — the drawer pulled it back. These
 * press real keys in a real browser, because jsdom performs no Tab movement:
 *
 * * Q1 — the palette over the drawer, and help over the drawer, keep Tab.
 * * Q6 — under help, the palette, the drawer and the Config dialog a `j` moves
 *   nothing behind the box (`?item=` holds on Work), and the key layer reads
 *   suspended; ⌘K and `?` still get out of the drawer; the outcome chart leaves
 *   ⌘← to the browser.
 */
// ui-check-owns: src/lib/ui/contained.ts
// ui-check-owns: src/lib/keys/registry.ts
// ui-check-owns: src/lib/palette/Palette.svelte
// ui-check-owns: src/lib/shell/HelpOverlay.svelte
// ui-check-owns: src/lib/work/DispatchLaunchpad.svelte
// ui-check-owns: src/routes/(factory)/config/+page.svelte
// ui-check-owns: src/routes/(factory)/work/+page.*
import { SETTLE_MS } from '../ui-check-lib.mjs';

/** The item `?item=` names, or null. */
const itemOf = (url) => new URL(url).searchParams.get('item');

/** The palette's shortcut in a real browser: ⌘K on a Mac, ^K elsewhere — the
 *  console answers both. */
const PALETTE = 'Control+k';

const PALETTE_BOX = '[role="dialog"][aria-label="Go to"]';
const HELP_BOX = '[role="dialog"][data-help-overlay]';
const DRAWER_BOX = '[role="dialog"][aria-label^="Plan a dispatch"]';

/** Whether the page's own key layer reads suspended (the page's module instance,
 *  as `modal-keys.mjs` reads the registry). */
const suspended = (page) =>
  page.evaluate(async () => (await import('/src/lib/keys/registry.ts')).shortcutsSuspended());

/** Four weeks, each with a different reading, as `outcome-crosshair.client.test.ts`
 *  draws them: the fixture set has no outcome report, so the chart is served one. */
const OUTCOMES = {
  weeks: [0, 1, 2, 3].map((i) => ({
    week_start: `2026-09-${(7 + i * 7).toString().padStart(2, '0')}`,
    merged_main: 40 + i * 10,
    merged_kew_touched: 20 + i,
    merged_kew_authored: 18 + i,
    ready_items: 10 + i,
    ready_hours: { p50: 2 + i, p90: 6, max: 9 },
    rounds_to_ready: { p50: 1 + i * 0.5, max: 4 },
    cost_per_merged: { mean: 26, p50: 20 + i },
    spend_total: 700,
    spend_unattributed: 30,
    nodes_done: 20,
    pr_sync_observed_at: '2026-09-27T18:00:00+00:00',
    partial: false
  })),
  min_sample: 3,
  merged_main_head: 'abc1234',
  merged_main_unavailable: null
};

/** What has the focus: inside which box, and its id or text, for a message. */
function focusIn(page, selector) {
  return page.evaluate((sel) => {
    const box = document.querySelector(sel);
    const el = document.activeElement;
    return {
      inside: box !== null && el !== null && box.contains(el),
      name: el ? el.id || (el.textContent ?? '').trim().slice(0, 40) || el.tagName : 'nothing'
    };
  }, selector);
}

/** A page at `width`, with its errors collected; `body(page, problems)` runs on it. */
async function onPage(browser, width, height, body) {
  const ctx = await browser.newContext({ viewport: { width, height } });
  const page = await ctx.newPage();
  const problems = [];
  page.on('pageerror', (error) => problems.push(`page error: ${error.message}`));
  try {
    await body(page, problems);
  } finally {
    await page.close();
    await ctx.close();
  }
}

/** Work on item 2060 with its launchpad drawer open (Details). */
async function openDrawer(page, base, sleep) {
  await page.goto(`${base}/work?item=2060`, { waitUntil: 'load' });
  await page.locator('main').first().waitFor({ state: 'visible', timeout: 15_000 });
  await sleep(SETTLE_MS);
  const panel = page.locator('section[aria-label="Plan a dispatch"]');
  await panel.locator('button').filter({ hasText: 'Details' }).first().click();
  const drawer = page.locator(DRAWER_BOX);
  await drawer.waitFor({ state: 'visible', timeout: 5_000 });
  return drawer;
}

/**
 * A `j` behind the open box at `selector`: `?item=` holds and the key layer is
 * suspended. Focus is put on the box itself, where a `j` types nothing.
 */
async function jMovesNothing(page, selector, label, { expect, sleep }) {
  const before = page.url();
  const scroll = await page.evaluate(() => window.scrollY);
  await page.locator(selector).focus();
  expect(await suspended(page), `${label}: the key layer is not suspended while the box is open`);
  await page.keyboard.press('j');
  await sleep(300);
  expect(page.url() === before, `${label}: j behind the box moved the page from ${before} to ${page.url()}`);
  const after = await page.evaluate(() => window.scrollY);
  expect(after === scroll, `${label}: j behind the box scrolled the page from ${scroll} to ${after}`);
  expect(await page.locator(selector).isVisible(), `${label}: j closed the box`);
}

export async function checks({ browser, base, check, expect, sleep, sizes: { WIDTH, HEIGHT } }) {
  const sizes = [
    [390, 844],
    [WIDTH, HEIGHT]
  ];

  /** AC1 — the palette's confirm step over the drawer: Tab from the reason
   *  field reaches Cancel (Confirm is disabled until a reason is typed), and
   *  Shift+Tab comes back. */
  for (const [width, height] of sizes) {
    await check(`the palette over the drawer keeps Tab: input → Cancel and back at ${width}px`, () =>
      onPage(browser, width, height, async (page, problems) => {
        await openDrawer(page, base, sleep);
        await page.keyboard.press(PALETTE);
        const palette = page.locator(PALETTE_BOX);
        await palette.waitFor({ state: 'visible', timeout: 5_000 });
        await page.keyboard.type('stop the fleet');
        await sleep(200);
        await page.keyboard.press('Enter');
        const reason = page.locator('#palette-reason');
        await reason.waitFor({ state: 'visible', timeout: 5_000 });
        await reason.focus();

        await page.keyboard.press('Tab');
        const forward = await focusIn(page, PALETTE_BOX);
        expect(
          forward.inside && forward.name === 'Cancel',
          `${width}px: Tab from the reason field landed on "${forward.name}"` +
            `${forward.inside ? '' : ' outside the palette'}, not Cancel`
        );
        await page.keyboard.press('Shift+Tab');
        const back = await focusIn(page, PALETTE_BOX);
        expect(back.name === 'palette-reason', `${width}px: Shift+Tab from Cancel landed on "${back.name}"`);

        // Closing the palette hands the focus back to the drawer beneath.
        await page.keyboard.press('Escape');
        await page.keyboard.press('Escape');
        await palette.waitFor({ state: 'detached', timeout: 5_000 });
        await sleep(100);
        const home = await focusIn(page, DRAWER_BOX);
        expect(home.inside, `${width}px: closing the palette left the focus on "${home.name}", outside the drawer`);
        expect(problems.length === 0, problems.join('\n'));
      })
    );
  }

  /** AC2 — help over the drawer: every Tab stays inside help, and one of them
   *  reaches its last control. */
  for (const [width, height] of sizes) {
    await check(`help over the drawer keeps Tab and reaches its last control at ${width}px`, () =>
      onPage(browser, width, height, async (page, problems) => {
        const drawer = await openDrawer(page, base, sleep);
        await drawer.focus();
        await page.keyboard.press('?');
        const help = page.locator(HELP_BOX);
        await help.waitFor({ state: 'visible', timeout: 5_000 });
        const last = await help.evaluate((box) => {
          const stops = [...box.querySelectorAll('a[href], button, input, select, textarea, [tabindex]')].filter(
            (el) => el.tabIndex >= 0 && !el.disabled
          );
          const el = stops[stops.length - 1];
          return el ? el.id || (el.textContent ?? '').trim().slice(0, 40) || el.tagName : null;
        });
        expect(last !== null, `${width}px: help has no control to Tab to`);
        // The drawer's keeper used to run first and move the focus into the
        // drawer before help's pulled it back: with one stop in help the press
        // still ends there, so what is counted is every focus the drawer took.
        await drawer.evaluate((box) => {
          window.__drawerFocus = 0;
          box.addEventListener('focusin', () => (window.__drawerFocus += 1));
        });
        let reached = false;
        for (let i = 0; i < 4; i += 1) {
          await page.keyboard.press('Tab');
          const at = await focusIn(page, HELP_BOX);
          expect(at.inside, `${width}px: Tab ${i + 1} left help for "${at.name}"`);
          if (at.name === last) reached = true;
        }
        expect(reached, `${width}px: four Tabs never reached help's last control "${last}"`);
        const stolen = await page.evaluate(() => window.__drawerFocus);
        expect(stolen === 0, `${width}px: the drawer beneath took the focus ${stolen} times during Tab in help`);
        await page.keyboard.press('Escape');
        await help.waitFor({ state: 'detached', timeout: 5_000 });
        expect(await drawer.isVisible(), `${width}px: Esc in help closed the drawer beneath too`);
        expect(problems.length === 0, problems.join('\n'));
      })
    );
  }

  /** AC3 — a `j` behind each modal changes nothing; the same `j` with no modal
   *  open does move Work's `?item=`, or the rest proves nothing. */
  for (const [width, height] of sizes) {
    await check(`a j behind help, the palette or the drawer leaves Work alone at ${width}px`, () =>
      onPage(browser, width, height, async (page, problems) => {
        const drawer = await openDrawer(page, base, sleep);
        await jMovesNothing(page, DRAWER_BOX, `${width}px drawer`, { expect, sleep });

        // The reserved keys still get out of the drawer (K9).
        await drawer.focus();
        await page.keyboard.press(PALETTE);
        await page.locator(PALETTE_BOX).waitFor({ state: 'visible', timeout: 5_000 });
        await jMovesNothing(page, PALETTE_BOX, `${width}px palette over the drawer`, { expect, sleep });
        await page.keyboard.press('Escape');
        await page.locator(PALETTE_BOX).waitFor({ state: 'detached', timeout: 5_000 });
        await drawer.focus();
        await page.keyboard.press('?');
        await page.locator(HELP_BOX).waitFor({ state: 'visible', timeout: 5_000 });
        await page.keyboard.press('Escape');
        await page.locator(HELP_BOX).waitFor({ state: 'detached', timeout: 5_000 });
        await page.keyboard.press('Escape');
        await drawer.waitFor({ state: 'detached', timeout: 5_000 });

        // Each of help and the palette on its own, over the Work list.
        await page.evaluate(() => (document.activeElement instanceof HTMLElement ? document.activeElement.blur() : null));
        await page.keyboard.press('?');
        await page.locator(HELP_BOX).waitFor({ state: 'visible', timeout: 5_000 });
        await jMovesNothing(page, HELP_BOX, `${width}px help`, { expect, sleep });
        await page.keyboard.press('Escape');
        await page.locator(HELP_BOX).waitFor({ state: 'detached', timeout: 5_000 });
        await page.keyboard.press(PALETTE);
        await page.locator(PALETTE_BOX).waitFor({ state: 'visible', timeout: 5_000 });
        await jMovesNothing(page, PALETTE_BOX, `${width}px palette`, { expect, sleep });
        await page.keyboard.press('Escape');
        await page.locator(PALETTE_BOX).waitFor({ state: 'detached', timeout: 5_000 });

        await page.evaluate(() => (document.activeElement instanceof HTMLElement ? document.activeElement.blur() : null));
        expect(!(await suspended(page)), `${width}px: the key layer is still suspended with every box closed`);
        await page.keyboard.press('j');
        await sleep(300);
        expect(
          itemOf(page.url()) !== '2060',
          `${width}px: j with every box closed did not move ?item= either, so the check proves nothing`
        );
        expect(problems.length === 0, problems.join('\n'));
      })
    );
  }

  /** AC3 — the Config override dialog: the key layer is suspended and a `j`
   *  changes nothing behind it. */
  for (const [width, height] of sizes) {
    await check(`a j behind the Config dialog leaves the page alone at ${width}px`, () =>
      onPage(browser, width, height, async (page, problems) => {
        await page.goto(`${base}/config`, { waitUntil: 'load' });
        await page.locator('main').first().waitFor({ state: 'visible', timeout: 15_000 });
        await sleep(SETTLE_MS);
        await page.getByRole('button', { name: 'Override…' }).first().click();
        const dialog = page.locator('[role="dialog"][data-config-dialog]');
        await dialog.waitFor({ state: 'visible', timeout: 5_000 });
        await jMovesNothing(page, '[role="dialog"][data-config-dialog]', `${width}px Config dialog`, {
          expect,
          sleep
        });

        // The palette over the dialog owns Escape: one pressed with the focus
        // left on `body` must not close the Config dialog beneath it.
        await page.keyboard.press(PALETTE);
        const palette = page.locator(PALETTE_BOX);
        await palette.waitFor({ state: 'visible', timeout: 5_000 });
        await page.evaluate(() => (document.activeElement instanceof HTMLElement ? document.activeElement.blur() : null));
        await page.keyboard.press('Escape');
        await sleep(200);
        expect(await dialog.isVisible(), `${width}px: Esc under the palette closed the Config dialog beneath it`);
        await palette.focus();
        await page.keyboard.press('Escape');
        await palette.waitFor({ state: 'detached', timeout: 5_000 });

        await dialog.focus();
        await page.keyboard.press('Escape');
        await dialog.waitFor({ state: 'detached', timeout: 5_000 });
        expect(!(await suspended(page)), `${width}px: the key layer is still suspended after the dialog closed`);
        expect(problems.length === 0, problems.join('\n'));
      })
    );
  }

  /** AC4 — ⌘← (and ^←) on the focused outcome chart is the browser's: the
   *  crosshair does not move and the press is not claimed, while a bare ←
   *  does move it. */
  for (const [width, height] of sizes) {
    await check(`the outcome chart leaves ⌘← to the browser at ${width}px`, () =>
      onPage(browser, width, height, async (page, problems) => {
        await page.route('**/api/report/outcomes*', (route) =>
          route.fulfill({ contentType: 'application/json', body: JSON.stringify(OUTCOMES) })
        );
        await page.goto(`${base}/scorecard`, { waitUntil: 'load' });
        const chart = page.locator('[role="application"][aria-label^="Weekly outcomes"]');
        await chart.waitFor({ state: 'visible', timeout: 15_000 });
        await sleep(SETTLE_MS);
        await page.evaluate(() => {
          window.__claimed = [];
          window.addEventListener('keydown', (e) => {
            if (e.key === 'ArrowLeft' && (e.metaKey || e.ctrlKey)) window.__claimed.push(e.defaultPrevented);
          });
        });
        await chart.focus();
        const reading = () => chart.evaluate((el) => el.textContent);
        // The crosshair on the newest week, so a ← has somewhere to go.
        await page.keyboard.press('End');
        await sleep(200);

        const before = await reading();
        for (const chord of ['Meta+ArrowLeft', 'Control+ArrowLeft']) {
          await page.keyboard.press(chord);
          await sleep(200);
          expect((await reading()) === before, `${width}px: ${chord} moved the crosshair`);
        }
        const claimed = await page.evaluate(() => window.__claimed);
        expect(
          claimed.length === 2 && claimed.every((c) => c === false),
          `${width}px: the chart took a chorded ← from the browser (${JSON.stringify(claimed)})`
        );
        expect(page.url().endsWith('/scorecard'), `${width}px: ⌘← left /scorecard for ${page.url()}`);

        await page.keyboard.press('ArrowLeft');
        await sleep(200);
        expect((await reading()) !== before, `${width}px: a bare ← did not move the crosshair, so ⌘← proves nothing`);
        expect(problems.length === 0, problems.join('\n'));
      })
    );
  }
}
