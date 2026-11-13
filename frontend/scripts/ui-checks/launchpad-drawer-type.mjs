/**
 * The launchpad drawer's type and colour follow DESIGN.md (#2845).
 *
 * - No sentence is set in label-11 with the drawer open: #2787's check read the
 *   page with the drawer shut, and the drawer set "UNTOUCHED: THE ROUTE'S MODEL
 *   RUNS, AND NO OVERRIDE IS SENT" and "SHOW ALL FIELDS (13 MORE)" (D4, P2).
 * - No model id or config key is uppercased by CSS on the drawer, the config
 *   model picker or Health: "GPT-5.6-LUNA", "PER MTOK", "IMAGES.CLAUDE-CODE" (P3).
 * - The drawer's plan command names the selected item: every item was planned
 *   as 2302 out of one fixture (P5).
 * - No cyan text in the drawer except focus: "DEFAULT FOR SIMPLE" was cyan (P4).
 * - In the config model picker, the selected row's bar and the focused list
 *   border are cyan, not magenta, and no option text is cyan (item 7).
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 */
// ui-check-owns: src/lib/work/DispatchLaunchpad.svelte
// ui-check-owns: src/lib/ModelPicker.svelte
// ui-check-owns: src/routes/(factory)/config/+page.svelte
// ui-check-owns: src/routes/(factory)/health/+page.svelte
// ui-check-owns: src/routes/(factory)/work/+page.*
import { SETTLE_MS } from '../ui-check-lib.mjs';

const PHONE = { width: 390, height: 844 };
/** Two ready-queue items, each planned out of its own fixture. */
const ITEMS = ['2060', '2054'];

/**
 * The label-11 blocks inside `rootSelector`, read whole, and the ones that are
 * sentences: more than four words, or more than one ending in a period — the
 * rule #2787's check (`signal-and-labels.mjs`) applies to the page.
 */
function labelSentences(rootSelector) {
  const root = document.querySelector(rootSelector);
  if (root === null) return null;
  const isBlock = (el) => getComputedStyle(el).display !== 'inline';
  const visible = (el) => {
    const box = el.getBoundingClientRect();
    return box.width > 0 && box.height > 0 && getComputedStyle(el).visibility !== 'hidden';
  };
  const out = [];
  const seen = new Set();
  for (const label of root.querySelectorAll('.text-11')) {
    for (const el of [label, ...label.querySelectorAll('*')]) {
      if (seen.has(el) || !visible(el)) continue;
      if (el !== label && !isBlock(el)) continue;
      if ([...el.querySelectorAll('*')].some(isBlock)) continue;
      seen.add(el);
      const text = (el.innerText || '').replace(/\s+/g, ' ').trim();
      const words = text.split(' ').filter((w) => /[\p{L}\p{N}]/u.test(w));
      if (words.length > 4 || (words.length > 1 && /\.$/.test(text))) out.push(text);
    }
  }
  return out;
}

/**
 * Every visible element under `rootSelector` whose own text holds a model id
 * from `ids`, a dotted config key (`images.claude-code`) or a price's "per
 * Mtok", and that CSS sets in capitals.
 */
function shoutedIds([rootSelector, ids]) {
  const root = document.querySelector(rootSelector);
  if (root === null) return null;
  const KEY = /\b[a-z][a-z0-9_]*\.[a-z][a-z0-9_-]+\b/;
  const looksLikeId = (text) =>
    ids.some((id) => text.includes(id)) || KEY.test(text) || /per mtok/i.test(text);
  const out = [];
  let read = 0;
  for (const el of root.querySelectorAll('*')) {
    const own = [...el.childNodes]
      .filter((n) => n.nodeType === Node.TEXT_NODE)
      .map((n) => n.textContent)
      .join(' ')
      .trim();
    if (own === '' || !looksLikeId(own)) continue;
    const box = el.getBoundingClientRect();
    if (box.width === 0 || box.height === 0) continue;
    read += 1;
    if (getComputedStyle(el).textTransform === 'uppercase') out.push(own);
  }
  return { read, out };
}

/**
 * Open /config's Agent model override and pick Opus 5.5, so the picker has a
 * selected row; the dialog's listbox and that row.
 */
async function openConfigPicker(page, base, sleep) {
  await page.goto(`${base}/config`, { waitUntil: 'load' });
  const row = page.locator('li').filter({ hasText: 'Agent model' }).first();
  await row.waitFor({ state: 'visible', timeout: 15_000 });
  await sleep(SETTLE_MS);
  await row.getByRole('button', { name: 'Override…' }).click();
  const listbox = page.getByRole('dialog', { name: 'Override Agent model' }).getByRole('listbox');
  await listbox.waitFor({ state: 'visible', timeout: 15_000 });
  const opus = listbox.getByRole('option').filter({ has: page.getByText('Opus 5.5', { exact: true }) });
  await opus.scrollIntoViewIfNeeded();
  await opus.click();
  await sleep(200);
  return { listbox, opus };
}

/** Visible elements under `rootSelector` whose own text is drawn in cyan. */
function cyanText(rootSelector) {
  const root = document.querySelector(rootSelector);
  const probe = document.createElement('span');
  probe.style.color = 'var(--accent-2)';
  document.body.append(probe);
  const cyan = getComputedStyle(probe).color;
  probe.remove();
  const out = [];
  for (const el of root.querySelectorAll('*')) {
    const own = [...el.childNodes]
      .filter((n) => n.nodeType === Node.TEXT_NODE)
      .map((n) => n.textContent)
      .join('')
      .trim();
    if (own === '') continue;
    const box = el.getBoundingClientRect();
    if (box.width === 0 || box.height === 0) continue;
    if (getComputedStyle(el).color === cyan) out.push(own);
  }
  return { cyan, out };
}

/** Open `/work?item=` and the launchpad's drawer; the drawer's locator. */
async function openDrawer(page, base, item, sleep) {
  await page.goto(`${base}/work?item=${item}`, { waitUntil: 'load' });
  const panel = page.locator('section[aria-label="Plan a dispatch"]');
  await panel.waitFor({ state: 'visible', timeout: 15_000 });
  await sleep(SETTLE_MS);
  await panel.getByRole('button', { name: 'Details' }).click();
  const drawer = page.getByRole('dialog', { name: /Plan a dispatch/ });
  await drawer.locator('[data-command]').waitFor({ state: 'visible', timeout: 15_000 });
  await drawer.getByRole('listbox').first().waitFor({ state: 'visible', timeout: 15_000 });
  await sleep(SETTLE_MS);
  return drawer;
}

/** The catalog's model ids, as the page reads them. */
async function modelIds(page, base) {
  const response = await page.request.get(`${base}/api/models`);
  const body = await response.json();
  return body.models.map((m) => m.id);
}

const DRAWER = '[role="dialog"][aria-label^="Plan a dispatch"]';

export async function checks({ browser, base, check, expect, sleep, sizes: { WIDTH, HEIGHT } }) {
  const withPage = async (size, body) => {
    const ctx = await browser.newContext({ viewport: size });
    const page = await ctx.newPage();
    try {
      await body(page);
    } finally {
      await page.close();
      await ctx.close();
    }
  };

  for (const size of [{ width: WIDTH, height: HEIGHT }, PHONE]) {
    const at = `${size.width}px`;

    await check(`no sentence is set in label-11 with the launchpad drawer open at ${at}`, () =>
      withPage(size, async (page) => {
        const drawer = await openDrawer(page, base, ITEMS[0], sleep);
        // Folded and unfolded: the toggle unfolds rows that carry labels too.
        const shut = await page.evaluate(labelSentences, DRAWER);
        await drawer.getByRole('button', { name: /more fields/ }).click();
        await sleep(SETTLE_MS);
        const open = await page.evaluate(labelSentences, DRAWER);
        const sentences = [...(shut ?? ['no drawer']), ...(open ?? ['no drawer'])];
        expect(
          sentences.length === 0,
          `${at}: the drawer's label-11 carries:\n` + sentences.map((s) => `  "${s}"`).join('\n')
        );
      })
    );

    await check(`no model id or config key is uppercased on the drawer, config picker or Health at ${at}`, () =>
      withPage(size, async (page) => {
        const ids = await modelIds(page, base);
        const found = [];
        let read = 0;
        const take = (where, result) => {
          if (result === null) return found.push(`${where}: nothing to read`);
          read += result.read;
          for (const text of result.out) found.push(`${where}: "${text}"`);
        };

        await openDrawer(page, base, ITEMS[0], sleep);
        take('drawer', await page.evaluate(shoutedIds, [DRAWER, ids]));

        await openConfigPicker(page, base, sleep);
        take('config picker', await page.evaluate(shoutedIds, ['[role="dialog"] [role="listbox"]', ids]));

        await page.goto(`${base}/health`, { waitUntil: 'load' });
        await page.locator('section#images').waitFor({ state: 'visible', timeout: 15_000 });
        await sleep(SETTLE_MS);
        take('health', await page.evaluate(shoutedIds, ['main', ids]));

        // Not vacuous: ids were found on every surface before none was shouted.
        expect(read >= 3, `${at}: only ${read} id(s) read across the three surfaces`);
        expect(found.length === 0, `${at}: uppercased by CSS:\n  ${found.join('\n  ')}`);
      })
    );

    await check(`the drawer's plan command names the selected item, for two items, at ${at}`, () =>
      withPage(size, async (page) => {
        for (const item of ITEMS) {
          const drawer = await openDrawer(page, base, item, sleep);
          const command = ((await drawer.locator('[data-command]').textContent()) ?? '').trim();
          expect(
            command === `kew dispatch ${item}` || command.startsWith(`kew dispatch ${item} `),
            `${at}: the drawer for item ${item} prints "${command}"`
          );
        }
      })
    );

    await check(`no cyan text in the launchpad drawer except focus at ${at}`, () =>
      withPage(size, async (page) => {
        await openDrawer(page, base, ITEMS[0], sleep);
        // Focus off every control, so a focus ring is not what is read.
        await page.evaluate(() => document.activeElement?.blur?.());
        const read = await page.evaluate(cyanText, DRAWER);
        expect(
          read.out.length === 0,
          `${at}: drawer text drawn in the cyan token ${read.cyan}: ` +
            read.out.map((t) => `"${t}"`).join(', ')
        );
      })
    );
  }

  await check('the config model picker marks selection and focus in cyan, never magenta', () =>
    withPage({ width: WIDTH, height: HEIGHT }, async (page) => {
      const { listbox } = await openConfigPicker(page, base, sleep);
      const selected = listbox.locator('[role="option"][aria-selected="true"]');
      expect((await selected.count()) === 1, 'the config picker has no selected row after a pick');
      await listbox.focus();
      await sleep(200);
      const read = await page.evaluate(
        ([tokens]) => {
          const colours = Object.fromEntries(
            tokens.map((t) => {
              const probe = document.createElement('span');
              probe.style.color = `var(${t})`;
              document.body.append(probe);
              const c = getComputedStyle(probe).color;
              probe.remove();
              return [t, c];
            })
          );
          const list = document.querySelector('[role="dialog"] [role="listbox"]');
          const row = list.querySelector('[role="option"][aria-selected="true"]');
          const cyanOptionText = [...list.querySelectorAll('[role="option"] *')]
            .filter((el) => [...el.childNodes].some((n) => n.nodeType === Node.TEXT_NODE && n.textContent.trim()))
            .filter((el) => getComputedStyle(el).color === colours['--accent-2'])
            .map((el) => el.textContent.trim());
          return {
            colours,
            bar: getComputedStyle(row).borderLeftColor,
            border: getComputedStyle(list).borderTopColor,
            focused: document.activeElement === list,
            cyanOptionText
          };
        },
        [['--accent', '--accent-2']]
      );
      const cyan = read.colours['--accent-2'];
      const magenta = read.colours['--accent'];
      expect(read.focused, 'the listbox did not take focus');
      expect(
        read.bar === cyan && read.bar !== magenta,
        `the selected row's bar is ${read.bar}, not cyan ${cyan}`
      );
      expect(
        read.border === cyan && read.border !== magenta,
        `the focused list's border is ${read.border}, not cyan ${cyan}`
      );
      expect(
        read.cyanOptionText.length === 0,
        `option text drawn cyan: ${read.cyanOptionText.map((t) => `"${t}"`).join(', ')}`
      );
    })
  );
}
