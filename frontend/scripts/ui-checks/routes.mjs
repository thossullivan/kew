/**
 * Every top-level route loads with a quiet console.
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 */
// ui-check-owns: src/routes/(factory)/work/+page.*
// ui-check-owns: src/routes/(factory)/monitor/+page.*
// ui-check-owns: src/routes/(factory)/epics/+page.*
// ui-check-owns: src/routes/(factory)/epics/[ref]/+page.*
// ui-check-owns: src/routes/(factory)/runs/[id]/+page.*
// ui-check-owns: src/routes/+error.svelte
// ui-check-owns: src/lib/theme.svelte.ts

/** Site data blocked, as a browser setting serves it: every storage touch throws. */
function blockStorage(page) {
  return page.addInitScript(() => {
    for (const name of ['localStorage', 'sessionStorage']) {
      Object.defineProperty(window, name, {
        configurable: true,
        get() {
          throw new DOMException('The operation is insecure.', 'SecurityError');
        }
      });
    }
  });
}

/** Every top-level route, plus the epics and the one run the fixtures draw. */
const ROUTES = [
  '/work',
  '/monitor',
  '/epics',
  '/epics/launch-fixes',
  '/epics/sweep-2026-10',
  '/runs/f5dcb6a5'
];

export async function checks({ check, visit, expect }) {
  for (const route of ROUTES) {
    await check(`${route} loads clean`, () =>
      visit(route, async (page, problems) => {
        // `textContent`, not `innerText`: this asks whether the route drew its
        // READING, and a rendered reading needs a font the machine may lack.
        const text = (await page.locator('main').first().textContent()) ?? '';
        expect(text.trim().length > 0, `${route} rendered an empty <main>`);
        expect(problems.length === 0, `${route} reported:\n${problems.join('\n')}`);
      })
    );
  }

  // Design review c9 X4: the root layout read the theme from storage with no
  // guard, so with site data blocked its effect threw and the page was blank.
  await check('/work draws with storage blocked, in the default theme', () =>
    visit(
      '/work',
      async (page, problems) => {
        const text = (await page.locator('main').first().textContent()) ?? '';
        expect(text.trim().length > 0, '/work rendered an empty <main> with storage blocked');
        const themeAttr = await page.evaluate(() => document.documentElement.getAttribute('data-theme'));
        expect(themeAttr === 'dark', `the theme with storage blocked is ${themeAttr}`);
        expect(problems.length === 0, `/work with storage blocked reported:\n${problems.join('\n')}`);
      },
      undefined,
      blockStorage
    )
  );

  // Design review c9 X7: an address no route matches showed the framework's
  // bare default. It keeps the console header and says, in a sentence, what
  // happened and where to go.
  for (const [label, size] of [
    ['1440', undefined],
    ['390', { width: 390, height: 844 }]
  ]) {
    await check(`an unknown address keeps the header and points to Work at ${label}px`, () =>
      visit(
        '/no-such-page',
        async (page) => {
          const main = page.locator('main[data-error-page]');
          await main.waitFor({ state: 'visible', timeout: 15_000 });
          const said = ((await main.textContent()) ?? '').replace(/\s+/g, ' ').trim();
          expect(said.includes('That page does not exist. Go to Work.'), `the page says "${said}"`);
          const nav = await page.locator('header nav a[href="/work"]').count();
          expect(nav > 0, 'the error page draws no console header with its Work tab');
          const link = main.locator('a[href="/work"]');
          const box = await link.boundingBox();
          expect(box !== null && (label === '1440' || box.height >= 32), `the Work link is ${box?.height}px tall`);
          const sideways = await page.evaluate(
            () => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1
          );
          expect(!sideways, 'the error page scrolls sideways');
          await link.click();
          await page.waitForURL((url) => url.pathname === '/work', { timeout: 15_000 });
        },
        size
      )
    );
  }
}
