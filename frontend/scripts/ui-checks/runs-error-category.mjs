/**
 * A failed run's error category is worded on the Runs page (#2973).
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 *
 * The Runs drawer printed `container_start_failed` as itself. It now leads
 * with "Container start failed" and keeps the recorded code as secondary
 * detail and as the line's `title`. The committed fixture has no failed run,
 * so the first run is served as one; the line is read at desktop and phone
 * width, where a long code must wrap inside the drawer, not clip.
 */
// ui-check-owns: src/routes/(factory)/runs/+page.*
// ui-check-owns: src/lib/factory/names.ts

const CATEGORY = 'container_start_failed';
const PHONE = { width: 390, height: 844 };

/** `/api/runs` with its first run failed as a container start. */
async function serveFailedRun(page) {
  await page.route(
    (url) => url.pathname === '/api/runs',
    async (route) => {
      const response = await route.fetch();
      const runs = await response.json();
      const [first, ...rest] = runs;
      await route.fulfill({
        response,
        json: [{ ...first, status: 'failure', error_category: CATEGORY }, ...rest]
      });
    }
  );
}

export async function checks({ check, visit, expect, sizes }) {
  for (const size of [{ width: sizes.WIDTH, height: sizes.HEIGHT }, PHONE]) {
    await check(`a failed run's drawer words its error category (${size.width}px)`, () =>
      visit(
        '/runs',
        async (page) => {
          const row = page.locator('main button[aria-expanded]').first();
          await row.waitFor({ state: 'visible', timeout: 15_000 });
          await row.click();
          const line = page.locator('[data-run-error]').first();
          await line.waitFor({ state: 'visible', timeout: 15_000 });
          const read = await line.evaluate((el) => {
            const code = el.querySelector('[data-run-error-code]');
            const box = el.getBoundingClientRect();
            const codeBox = code?.getBoundingClientRect() ?? null;
            return {
              text: (el.textContent ?? '').replace(/\s+/g, ' ').trim(),
              code: (code?.textContent ?? '').trim(),
              title: el.getAttribute('title') ?? '',
              overflow: el.scrollWidth - el.clientWidth,
              right: box.right,
              codeRight: codeBox?.right ?? Infinity,
              viewport: document.documentElement.clientWidth
            };
          });
          expect(
            read.text.startsWith('error · Container start failed'),
            `the error line reads "${read.text}", not the worded category`
          );
          expect(read.code === CATEGORY, `the secondary code reads "${read.code}"`);
          expect(read.title === CATEGORY, `the error line's title is "${read.title}"`);
          expect(read.overflow <= 0, `the error line overflows its drawer by ${read.overflow}px`);
          expect(
            Math.max(read.right, read.codeRight) <= read.viewport + 0.5,
            `the error line runs past the ${read.viewport}px window`
          );
        },
        size,
        serveFailedRun
      )
    );
  }
}
