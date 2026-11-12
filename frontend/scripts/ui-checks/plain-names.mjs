/**
 * Plain names for machine ids (#2541): Monitor's Runner cell prints the
 * model's catalog name with the raw id as its `title`, the run page's header
 * names both outcomes when the run and its publication disagree, and the run
 * events' kind chips are worded, with the recorded code as their `title`.
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 */
// ui-check-owns: src/lib/factory/names.ts
// ui-check-owns: src/lib/monitor/live-runs.ts
// ui-check-owns: src/lib/runs/RunEvents.svelte
// ui-check-owns: src/routes/(factory)/runs/[id]/+page.svelte
// ui-check-owns: src/lib/monitor/LiveRuns.svelte
// ui-check-owns: src/routes/(factory)/monitor/+page.*

const PHONE = { width: 390, height: 844 };

export async function checks({ check, visit, expect, sizes }) {
  for (const size of [{ width: sizes.WIDTH, height: sizes.HEIGHT }, PHONE]) {
    await check(`the Monitor Runner cell is a model name, its title the id (${size.width}px)`, () =>
      visit(
        '/monitor',
        async (page) => {
          const cell = page.locator('[data-runner]').first();
          await cell.waitFor({ state: 'visible', timeout: 15_000 });
          const text = ((await cell.textContent()) ?? '').trim();
          const title = (await cell.getAttribute('title')) ?? '';
          // The committed fixture's working runs are on `claude-opus-5`.
          expect(title === 'claude-opus-5', `the Runner cell's title is "${title}", not the model id`);
          expect(text === 'Opus 5', `the Runner cell reads "${text}", not the model's display name`);
        },
        size
      )
    );

    await check(`the run header names both outcomes (${size.width}px)`, () =>
      visit(
        '/runs/f5dcb6a5',
        async (page) => {
          const outcome = page.locator('header [data-run-outcome]');
          await outcome.waitFor({ state: 'visible', timeout: 15_000 });
          const text = ((await outcome.textContent()) ?? '').replace(/\s+/g, ' ').trim();
          expect(
            text === 'Run succeeded · publication blocked',
            `the run header reads "${text}" rather than naming the run and its publication`
          );
        },
        size,
        // The committed run's sealed package reads and its pull request is
        // open, so nothing blocks it (#3057): the outcomes disagree only while
        // the package cannot be opened, which is the case this pins.
        async (page) => {
          await page.route('**/api/runs/f5dcb6a5/nodes/**', (route) =>
            route.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"gone"}' })
          );
        }
      )
    );

    await check(`the run event kind chips are worded, their title the code (${size.width}px)`, () =>
      visit(
        '/runs/f5dcb6a5',
        async (page) => {
          const chip = page.locator('section[aria-label="Run events"] button[title="routing_decision"]');
          await chip.waitFor({ state: 'visible', timeout: 15_000 });
          const text = ((await chip.textContent()) ?? '').replace(/\s+/g, ' ').trim();
          expect(text === 'Route chosen 1', `the routing_decision chip reads "${text}", not its label`);
        },
        size
      )
    );
  }
}
