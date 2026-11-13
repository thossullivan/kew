/**
 * The /config model picker opens on the model in force, marked (#2846).
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 */
// ui-check-owns: src/routes/(factory)/config/+page.svelte
// ui-check-owns: src/lib/ModelPicker.svelte
import { SHOTS, step } from '../gallery.mjs';

const SHOT = SHOTS.find((s) => s.name === 'config-model-picker');

/** The model the fixture's `agent.model` row has in force. */
const IN_FORCE = 'claude-sonnet-5-5';

export async function checks({ check, visit, expect, sizes: { WIDTH, HEIGHT } }) {
  /**
   * #2846, AC1 and AC2 — the gallery's own `config-model-picker` steps, then:
   * the page logged no error of any kind (the gallery manifest records console
   * errors too, and the human_review read used to 404 here); the in-force
   * model's option is the one marked "current" and lies wholly inside the
   * listbox's box without anyone scrolling; and the list's bottom edge fades
   * while rows lie below it, and stops fading at the end.
   *
   * The in-force row is the fifteenth in the catalog, so a list that opened at
   * its top would hold it well below the edge.
   */
  for (const width of [390, WIDTH]) {
    await check(`/config opens the model picker on the current model at ${width}px`, () =>
      visit(
        SHOT.route,
        async (page, problems) => {
          for (const s of SHOT.then) await step(page, s);
          const listbox = page.locator(SHOT.shows).first();
          await listbox.waitFor({ state: 'visible', timeout: 5_000 });

          expect(problems.length === 0, `${width}px: /config logged\n${problems.join('\n')}`);

          const marked = listbox.locator('[role="option"][data-current]');
          const ids = await marked.evaluateAll((rows) => rows.map((r) => r.dataset.modelId));
          expect(
            ids.length === 1 && ids[0] === IN_FORCE,
            `${width}px: the options marked current are ${JSON.stringify(ids)}, not [${IN_FORCE}]`
          );
          const words = (await marked.first().textContent()) ?? '';
          expect(words.includes('✓ current'), `${width}px: the current row reads "${words.trim()}"`);

          const [list, row] = await Promise.all([listbox.boundingBox(), marked.first().boundingBox()]);
          expect(
            list !== null &&
              row !== null &&
              row.y >= list.y - 0.5 &&
              row.y + row.height <= list.y + list.height + 0.5,
            `${width}px: the current row ${JSON.stringify(row)} is outside the list ${JSON.stringify(list)}`
          );

          // The rows scroll, and fade, in a box inside the bordered listbox
          // (#2894 C1: a mask on the bordered box fades its focus ring), so
          // read the mask wherever it is under the listbox and scroll the box
          // that scrolls.
          const fade = () =>
            listbox.evaluate((el) => ({
              more: el.dataset.moreBelow,
              mask: [el, ...el.querySelectorAll('*')]
                .map((n) => getComputedStyle(n).maskImage)
                .filter((m) => m && m !== 'none')
                .join(', ') || 'none'
            }));
          const open = await fade();
          expect(
            open.more === 'true' && open.mask !== 'none',
            `${width}px: rows lie below the edge but it does not fade: ${JSON.stringify(open)}`
          );
          await listbox.evaluate((el) => {
            const scroller =
              [el, ...el.querySelectorAll('*')].find((n) => ['auto', 'scroll'].includes(getComputedStyle(n).overflowY)) ??
              el;
            scroller.scrollTo({ top: scroller.scrollHeight });
          });
          await page.waitForTimeout(200);
          const end = await fade();
          expect(
            end.more === 'false' && end.mask === 'none',
            `${width}px: the list still fades at its end: ${JSON.stringify(end)}`
          );
        },
        { width, height: width === 390 ? 844 : HEIGHT }
      )
    );
  }
}
