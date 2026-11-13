/**
 * The launchpad's per-dispatch model control (#2741).
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 */
// ui-check-owns: src/lib/work/DispatchLaunchpad.svelte
// ui-check-owns: src/lib/ModelPicker.svelte
// ui-check-owns: src/routes/(factory)/work/+page.*
import { SETTLE_MS } from '../ui-check-lib.mjs';

export async function checks({ browser, base, check, expect, sleep, sizes: { WIDTH, HEIGHT } }) {
  /**
   * #2741, AC3 — open the launchpad's drawer, pick a model from the listbox,
   * press Run, and read what the page POSTed.
   *
   * vitest proves the pick reaches the request body; this proves the control a
   * person meets in the drawer is that listbox, that it lays out inside the
   * drawer at phone and desktop width, and that a real click on a row and then
   * on Run reaches `POST /api/run-requests`. The fixture API serves no writes,
   * so the POST is answered here — and its body is the reading.
   */
  for (const width of [390, WIDTH]) {
    await check(`the launchpad picks this dispatch's model from a listbox at ${width}px`, async () => {
      const ctx = await browser.newContext({ viewport: { width, height: HEIGHT } });
      const page = await ctx.newPage();
      try {
        const posted = [];
        await page.route('**/api/run-requests', async (route) => {
          if (route.request().method() !== 'POST') return route.fallback();
          const body = route.request().postDataJSON();
          posted.push(body);
          await route.fulfill({
            json: {
              request: {
                id: 'ui-check',
                command: body.command,
                operator: 'ui-check',
                requested_at: new Date().toISOString(),
                source: 'web',
                state: 'queued',
                item_id: body.item_id ?? null,
                drive: null,
                host: null,
                pid: null,
                log: null,
                started_at: null,
                finished_at: null,
                exit_code: null,
                detail: null,
                model: body.model ?? null
              },
              created: true,
              drive: { state: 'live', running: true, reason: null }
            }
          });
        });

        await page.goto(`${base}/work?item=2060`, { waitUntil: 'load' });
        const panel = page.locator('section[aria-label="Plan a dispatch"]');
        await panel.waitFor({ state: 'visible', timeout: 15_000 });
        await sleep(SETTLE_MS);
        await panel.getByRole('button', { name: 'Details' }).click();

        const drawer = page.getByRole('dialog', { name: /Plan a dispatch/ });
        const control = drawer.locator('section[aria-label="Model for this dispatch"]');
        const listbox = control.getByRole('listbox');
        await listbox.waitFor({ state: 'visible', timeout: 15_000 });

        const runner = ((await drawer.locator('[data-route="runner"]').textContent()) ?? '').trim();
        const offered = await listbox.getByRole('option').evaluateAll((rows) =>
          rows.map((row) => row.getAttribute('data-model-id'))
        );
        expect(
          offered.length > 0 && offered.every((id) => id.startsWith('gpt-')) && runner === 'codex',
          `${width}px: the control offers ${JSON.stringify(offered)} for runner ${runner}, not codex models only`
        );

        const box = await listbox.boundingBox();
        expect(
          box !== null && box.x >= 0 && box.x + box.width <= width + 0.5 && box.height > 40,
          `${width}px: the listbox is laid out at ${JSON.stringify(box)}, not inside the viewport`
        );

        const luna = listbox.locator('[role="option"][data-model-id="gpt-5.6-luna"]');
        await luna.scrollIntoViewIfNeeded();
        await luna.click();
        expect(
          (await luna.getAttribute('aria-selected')) === 'true',
          `${width}px: the clicked GPT-5.6 Luna option is not selected`
        );
        const code = ((await drawer.locator('code[data-command]').textContent()) ?? '').trim();
        expect(
          code.endsWith('--model gpt-5.6-luna'),
          `${width}px: the copied command does not carry the pick: ${code}`
        );

        await drawer.getByRole('button', { name: 'Close' }).click();
        await panel.locator('button[data-run-button]').click();
        for (let i = 0; i < 50 && posted.length === 0; i++) await sleep(100);
        expect(
          posted.length === 1 &&
            posted[0].model === 'gpt-5.6-luna' &&
            posted[0].command.startsWith('kew dispatch '),
          `${width}px: the page POSTed ${JSON.stringify(posted)}, not the command with gpt-5.6-luna`
        );
      } finally {
        await ctx.close();
      }
    });
  }
}
