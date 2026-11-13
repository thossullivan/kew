/**
 * The launchpad drawer marks the model that runs, and only it (#2895).
 *
 * The drawer's model list shaded GPT-5.6 Luna — the first row, "default for
 * simple" — while MODEL read gpt-5.6-sol and the note said the route's model
 * runs. The shade read as the choice.
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 */
// ui-check-owns: src/lib/work/DispatchLaunchpad.svelte
// ui-check-owns: src/lib/ModelPicker.svelte
// ui-check-owns: src/lib/model-picker.ts
// ui-check-owns: src/routes/(factory)/work/+page.*
import { SETTLE_MS } from '../ui-check-lib.mjs';

const PHONE = { width: 390, height: 844 };

/**
 * A served model with a long name and id, so the "✓ runs now" mark is read on
 * a row that wraps rather than on the fixture's short "GPT-5.6 Sol" (#2889).
 * The id's tail has no hyphen to break at: wider than the list on its own, it
 * must still wrap inside it.
 */
const LONG = {
  id: 'gpt-5.6-sol-extendedcontextpreview20261014withaverylongsuffixthathasnobreakpoint',
  display_name: 'GPT-5.6 Sol Extended Context Preview (October 2026, research access)'
};

/**
 * Every option in the drawer's model list that reads as marked: selected,
 * carrying the current mark, or shaded with a background of its own. With
 * the row's id, words and box, and the listbox's box.
 */
function markedOptions() {
  const dialog = document.querySelector('[role="dialog"][aria-label^="Plan a dispatch"]');
  const listbox = dialog?.querySelector('section[aria-label="Model for this dispatch"] [role="listbox"]');
  if (!listbox) return null;
  const clear = (color) => color === 'transparent' || /rgba\(.*,\s*0\)$/.test(color);
  const edge = listbox.getBoundingClientRect().right;
  // The rightmost edge of any text a row draws: a row's box ends at the list's
  // edge even when an unbreakable word inside it runs past.
  const range = document.createRange();
  const textRight = (row) => {
    range.selectNodeContents(row);
    return Math.max(...[...range.getClientRects()].map((r) => r.right), -Infinity);
  };
  const overflowing = [];
  for (const row of listbox.querySelectorAll('[role="option"]')) {
    const right = textRight(row);
    if (right > edge + 0.5) overflowing.push({ id: row.dataset.modelId, right });
  }
  const marked = [];
  for (const row of listbox.querySelectorAll('[role="option"]')) {
    const shaded = !clear(getComputedStyle(row).backgroundColor);
    const selected = row.getAttribute('aria-selected') === 'true';
    const current = row.hasAttribute('data-current');
    if (!shaded && !selected && !current) continue;
    const box = row.getBoundingClientRect();
    marked.push({
      id: row.dataset.modelId,
      shaded,
      selected,
      current,
      text: (row.textContent ?? '').replace(/\s+/g, ' ').trim(),
      right: box.right
    });
  }
  return {
    marked,
    right: edge,
    overflowing,
    scrollWidth: listbox.scrollWidth,
    clientWidth: listbox.clientWidth,
    active: document.getElementById(listbox.getAttribute('aria-activedescendant') ?? '')?.dataset
      .modelId
  };
}

async function openDrawer(page, base, sleep) {
  await page.goto(`${base}/work?item=2060`, { waitUntil: 'load' });
  const panel = page.locator('section[aria-label="Plan a dispatch"]');
  await panel.waitFor({ state: 'visible', timeout: 15_000 });
  await sleep(SETTLE_MS);
  await panel.getByRole('button', { name: 'Details' }).click();
  const drawer = page.getByRole('dialog', { name: /Plan a dispatch/ });
  await drawer
    .locator('section[aria-label="Model for this dispatch"] [role="listbox"]')
    .waitFor({ state: 'visible', timeout: 15_000 });
  await sleep(SETTLE_MS);
  return drawer;
}

export async function checks({ browser, base, check, expect, sleep, sizes: { WIDTH, HEIGHT } }) {
  const withPage = async (size, body) => {
    const ctx = await browser.newContext({ viewport: size });
    const page = await ctx.newPage();
    try {
      await body(page);
    } finally {
      await ctx.close();
    }
  };

  /** The one marked row is MODEL's, reads "✓ runs now", and stays in the list. */
  const onlyModelMarked = async (page, drawer, at) => {
    const named = ((await drawer.locator('[data-route="model"]').textContent()) ?? '').trim();
    const read = await page.evaluate(markedOptions);
    expect(read !== null, `${at}: the drawer shows no model list`);
    const { marked, right } = read;
    expect(
      marked.length === 1 && marked[0].id === named,
      `${at}: MODEL reads ${named}, but the marked rows are ${JSON.stringify(marked)}`
    );
    const [row] = marked;
    expect(
      row.current && !row.selected && row.text.includes('✓ runs now'),
      `${at}: the row that runs is not marked "✓ runs now" alone: ${JSON.stringify(row)}`
    );
    expect(row.right <= right + 0.5, `${at}: the marked row overflows the list: ${JSON.stringify(read)}`);
    expect(
      read.scrollWidth <= read.clientWidth,
      `${at}: the list scrolls sideways: scrollWidth ${read.scrollWidth} > clientWidth ${read.clientWidth}`
    );
    expect(
      read.overflowing.length === 0,
      `${at}: row text runs past the list's edge (${right}): ${JSON.stringify(read.overflowing)}`
    );
    return named;
  };

  for (const size of [{ width: WIDTH, height: HEIGHT }, PHONE]) {
    const at = `${size.width}px`;

    await check(`with no override the launchpad marks only MODEL's row at ${at}`, () =>
      withPage(size, async (page) => {
        const drawer = await openDrawer(page, base, sleep);
        const named = await onlyModelMarked(page, drawer, at);
        expect(named === 'gpt-5.6-sol', `${at}: the fixture's MODEL reads ${named}, not gpt-5.6-sol`);
      })
    );

    await check(`arrowing through the list marks no second model at ${at}`, () =>
      withPage(size, async (page) => {
        const drawer = await openDrawer(page, base, sleep);
        const listbox = drawer.locator(
          'section[aria-label="Model for this dispatch"] [role="listbox"]'
        );
        await listbox.focus();
        for (const [key, cursor] of [
          ['ArrowUp', 'gpt-5.6-luna'],
          ['ArrowDown', 'gpt-5.6-sol']
        ]) {
          await page.keyboard.press(key);
          await sleep(SETTLE_MS);
          const read = await page.evaluate(markedOptions);
          expect(
            read?.active === cursor,
            `${at}: after ${key} the cursor is on ${read?.active}, not ${cursor}`
          );
          await onlyModelMarked(page, drawer, `${at} after ${key}`);
        }
      })
    );

    await check(`a long served model name is the one marked, inside the list, at ${at}`, () =>
      withPage(size, async (page) => {
        await page.route('**/api/models', async (route) => {
          const response = await route.fetch();
          const body = await response.json();
          const sol = body.models.find((m) => m.id === 'gpt-5.6-sol');
          body.models.push({ ...sol, ...LONG });
          await route.fulfill({ response, json: body });
        });
        await page.route('**/api/dispatch/resolve**', async (route) => {
          const response = await route.fetch();
          const body = await response.json();
          body.model = { ...body.model, value: LONG.id };
          await route.fulfill({ response, json: body });
        });
        const drawer = await openDrawer(page, base, sleep);
        const named = await onlyModelMarked(page, drawer, at);
        expect(named === LONG.id, `${at}: MODEL reads ${named}, not the served ${LONG.id}`);
      })
    );
  }
}
