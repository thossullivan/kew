/**
 * The /config model control is a listbox of the catalog (#2542), and every
 * config row gives its text the width of a phone (#3468 D14).
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 */
// ui-check-owns: src/routes/(factory)/config/+page.svelte
// ui-check-owns: src/lib/ModelPicker.svelte
// ui-check-owns: src/lib/model-picker.ts
// ui-check-owns: src/lib/config-page.ts
import { SETTLE_MS } from '../ui-check-lib.mjs';

export async function checks({ browser, base, check, expect, sleep, sizes: { WIDTH, HEIGHT } }) {
  /**
   * #2542, AC3 — open the Agent model override, pick Opus 5.5 from the
   * listbox, confirm, and read what the page POSTed.
   *
   * vitest proves the picker emits only listed ids; this proves the control
   * a person meets on /config is that picker, that it lays out inside the
   * dialog at phone and desktop width, and that a real click reaches the write.
   * The fixture API serves no writes, so the POST is answered here — and its
   * body is the reading: `claude-opus-5-5`, not the display name and not free
   * text.
   */
  for (const width of [390, WIDTH]) {
    await check(`/config picks a model from a listbox at ${width}px`, async () => {
      const ctx = await browser.newContext({ viewport: { width, height: HEIGHT } });
      const page = await ctx.newPage();
      try {
        const posted = [];
        await page.route('**/api/config/agent.model', async (route) => {
          if (route.request().method() !== 'POST') return route.fallback();
          const body = route.request().postDataJSON();
          posted.push(body);
          await route.fulfill({ json: { field: 'agent.model', effective: body.value } });
        });

        await page.goto(`${base}/config`, { waitUntil: 'load' });
        const row = page.locator('li').filter({ hasText: 'Agent model' }).first();
        await row.waitFor({ state: 'visible', timeout: 15_000 });
        await sleep(SETTLE_MS);
        await row.getByRole('button', { name: 'Override…' }).click();

        const dialog = page.getByRole('dialog', { name: 'Override Agent model' });
        const listbox = dialog.getByRole('listbox');
        await listbox.waitFor({ state: 'visible', timeout: 15_000 });
        expect(
          (await dialog.locator('input[type="text"]').count()) === 1,
          `${width}px: the model dialog still has a free-text model field beside the reason`
        );

        const box = await listbox.boundingBox();
        expect(
          box !== null && box.x >= 0 && box.x + box.width <= width + 0.5 && box.height > 40,
          `${width}px: the listbox is laid out at ${JSON.stringify(box)}, not inside the viewport`
        );

        const opus = listbox.getByRole('option').filter({
          has: page.getByText('Opus 5.5', { exact: true })
        });
        expect((await opus.count()) === 1, `${width}px: no single Opus 5.5 option`);
        await opus.scrollIntoViewIfNeeded();
        await opus.click();
        expect(
          (await opus.getAttribute('aria-selected')) === 'true',
          `${width}px: the clicked Opus 5.5 option is not selected`
        );

        await dialog.getByRole('button', { name: 'Confirm override' }).click();
        await dialog.waitFor({ state: 'detached', timeout: 15_000 });
        expect(
          posted.length === 1 && posted[0].value === 'claude-opus-5-5',
          `${width}px: the page POSTed ${JSON.stringify(posted)}, not claude-opus-5-5`
        );
        const shown = (await row.textContent()) ?? '';
        expect(
          shown.includes('claude-opus-5-5'),
          `${width}px: the row does not show the written model: ${shown.trim()}`
        );
      } finally {
        await ctx.close();
      }
    });
  }

  /**
   * #2741, AC3 — the reviewer model is the same listbox, offering only the
   * reviewer's runner (claude-code), and a pick reaches
   * `POST /api/config/review.model` as the catalog id.
   */
  for (const width of [390, WIDTH]) {
    await check(`/config picks the reviewer model from a listbox at ${width}px`, async () => {
      const ctx = await browser.newContext({ viewport: { width, height: HEIGHT } });
      const page = await ctx.newPage();
      try {
        const posted = [];
        await page.route('**/api/config/review.model', async (route) => {
          if (route.request().method() !== 'POST') return route.fallback();
          const body = route.request().postDataJSON();
          posted.push(body);
          await route.fulfill({ json: { field: 'review.model', effective: body.value } });
        });

        await page.goto(`${base}/config`, { waitUntil: 'load' });
        const row = page.locator('li').filter({ hasText: 'Reviewer model' }).first();
        await row.waitFor({ state: 'visible', timeout: 15_000 });
        await sleep(SETTLE_MS);
        await row.getByRole('button', { name: 'Override…' }).click();

        const dialog = page.getByRole('dialog', { name: 'Override Reviewer model' });
        const listbox = dialog.getByRole('listbox');
        await listbox.waitFor({ state: 'visible', timeout: 15_000 });
        const box = await listbox.boundingBox();
        expect(
          box !== null && box.x >= 0 && box.x + box.width <= width + 0.5 && box.height > 40,
          `${width}px: the listbox is laid out at ${JSON.stringify(box)}, not inside the viewport`
        );
        const offered = await listbox.getByRole('option').evaluateAll((rows) =>
          rows.map((r) => r.getAttribute('data-model-id'))
        );
        expect(
          offered.length > 0 && offered.every((id) => id.startsWith('claude-')),
          `${width}px: the reviewer picker offers ${JSON.stringify(offered)}, not claude-code models only`
        );

        const sonnet = listbox.locator('[role="option"][data-model-id="claude-sonnet-5-5"]');
        await sonnet.scrollIntoViewIfNeeded();
        await sonnet.click();
        await dialog.getByRole('button', { name: 'Confirm override' }).click();
        await dialog.waitFor({ state: 'detached', timeout: 15_000 });
        expect(
          posted.length === 1 && posted[0].value === 'claude-sonnet-5-5',
          `${width}px: the page POSTed ${JSON.stringify(posted)}, not claude-sonnet-5-5`
        );
      } finally {
        await ctx.close();
      }
    });
  }

  /**
   * D42 and D45 (#3470) — the values read in words. An unset reviewer model
   * printed a bare `—`, the budget caps printed as bare `25` / `250` / `1500`,
   * the override note's reason was italic and ran into the time with no space,
   * and the override dialog was a square box on `hair-box`.
   */
  for (const width of [390, 768, WIDTH]) {
    await check(`/config prints no bare dash, caps as money, nothing italic, at ${width}px`, async () => {
      const ctx = await browser.newContext({ viewport: { width, height: HEIGHT } });
      const page = await ctx.newPage();
      try {
        await page.goto(`${base}/config`, { waitUntil: 'load' });
        const row = page.locator('li').filter({ hasText: 'Per issue' }).first();
        await row.waitFor({ state: 'visible', timeout: 15_000 });
        await sleep(SETTLE_MS);
        const read = await page.evaluate(() => {
          const values = [...document.querySelectorAll('[data-config-value]')].map((el) => ({
            text: (el.textContent ?? '').trim(),
            label: (el.parentElement?.firstElementChild?.textContent ?? '').trim()
          }));
          const italic = [...document.querySelectorAll('main li *')]
            .filter((el) => getComputedStyle(el).fontStyle === 'italic')
            .map((el) => (el.textContent ?? '').trim());
          const notes = [...document.querySelectorAll('[data-override-note]')].map((el) =>
            (el.textContent ?? '').replace(/\s+/g, ' ').trim()
          );
          return {
            values,
            italic,
            notes,
            scroll: document.documentElement.scrollWidth,
            client: document.documentElement.clientWidth
          };
        });
        expect(read.values.length >= 8, `only ${read.values.length} config values drew`);
        for (const v of read.values) {
          expect(v.text !== '—' && v.text !== '', `"${v.label}" prints a bare "${v.text}"`);
        }
        const byLabel = Object.fromEntries(read.values.map((v) => [v.label, v.text]));
        expect(byLabel['Per issue'] === '$25', `Per issue reads "${byLabel['Per issue']}"`);
        expect(byLabel['Daily'] === '$250', `Daily reads "${byLabel['Daily']}"`);
        expect(/^\$/.test(byLabel['Weekly'] ?? ''), `Weekly reads "${byLabel['Weekly']}"`);
        expect(
          (byLabel['Reviewer model'] ?? '').startsWith('unset · '),
          `an unset reviewer model reads "${byLabel['Reviewer model']}"`
        );
        expect(read.italic.length === 0, `italic text in a config row: ${read.italic.join(' / ')}`);
        expect(read.notes.length > 0, 'no override note drew, so the spacing proves nothing');
        for (const note of read.notes) {
          expect(!/\S·/.test(note), `the override note runs into its "·": "${note}"`);
        }
        expect(read.scroll <= read.client, `/config scrolls sideways at ${width}px`);

        await row.getByRole('button', { name: 'Override…' }).click();
        const dialog = page.locator('[data-config-dialog]');
        await dialog.waitFor({ state: 'visible', timeout: 5_000 });
        const box = await dialog.evaluate((el) => {
          const probe = document.createElement('span');
          probe.style.color = 'var(--hair-strong)';
          document.body.appendChild(probe);
          const hairStrong = getComputedStyle(probe).color;
          probe.remove();
          const style = getComputedStyle(el);
          const rect = el.getBoundingClientRect();
          return {
            radius: style.borderTopLeftRadius,
            border: style.borderTopColor,
            hairStrong,
            right: rect.right,
            client: document.documentElement.clientWidth
          };
        });
        expect(box.radius === '11px', `the override dialog's radius is ${box.radius}`);
        expect(box.border === box.hairStrong, `the override dialog's border is ${box.border}`);
        expect(box.right <= box.client + 0.5, `the override dialog is cut at ${width}px`);
      } finally {
        await page.close();
        await ctx.close();
      }
    });
  }

  /**
   * #3468 D14 — at 390 a config row is one column: its text, then its buttons
   * under it. The row was `minmax(0,1fr) auto` at every width, and on Weekly
   * the Override… and Revert buttons took about 200px, so the label, value,
   * hint and override note shared an 80px column, twelve lines tall.
   */
  for (const width of [390, 768]) {
    await check(`every /config row's text has 300px and its buttons sit under it at ${width}px`, async () => {
      const ctx = await browser.newContext({ viewport: { width, height: HEIGHT } });
      const page = await ctx.newPage();
      try {
        await page.goto(`${base}/config`, { waitUntil: 'load' });
        await page.locator('li').filter({ hasText: 'Agent model' }).first().waitFor({ state: 'visible', timeout: 15_000 });
        await page.getByText('Policy in force').first().waitFor({ state: 'visible', timeout: 15_000 });
        await sleep(SETTLE_MS);
        const rows = await page.evaluate(() =>
          [...document.querySelectorAll('main li')]
            .filter((li) => [...li.querySelectorAll('button')].some((b) => b.textContent.trim() === 'Override…'))
            .map((li) => {
              const text = li.children[0].getBoundingClientRect();
              return {
                label: li.children[0].textContent.replace(/\s+/g, ' ').trim().slice(0, 40),
                textWidth: text.width,
                textBottom: text.bottom,
                buttons: [...li.querySelectorAll('button')].map((b) => {
                  const r = b.getBoundingClientRect();
                  return { name: b.textContent.trim(), top: r.top, height: r.height };
                })
              };
            })
        );
        expect(rows.length >= 4, `${width}px: only ${rows.length} config rows with an Override… button`);
        expect(
          rows.some((row) => row.buttons.some((b) => b.name === 'Revert')),
          `${width}px: no row carries a Revert, so the widest button group was never measured`
        );
        for (const row of rows) {
          expect(row.textWidth >= 300, `${width}px: "${row.label}" has a ${Math.round(row.textWidth)}px text column`);
          for (const b of row.buttons) {
            expect(b.height >= 32, `${width}px: "${row.label}"'s ${b.name} is ${b.height}px tall`);
            if (width < 640) {
              expect(
                b.top >= row.textBottom - 0.5,
                `${width}px: "${row.label}"'s ${b.name} starts at y=${Math.round(b.top)}, beside its text (ends y=${Math.round(row.textBottom)})`
              );
            }
          }
        }
        const sideways = await page.evaluate(
          () => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1
        );
        expect(!sideways, `${width}px: /config scrolls sideways`);
      } finally {
        await ctx.close();
      }
    });
  }
}
