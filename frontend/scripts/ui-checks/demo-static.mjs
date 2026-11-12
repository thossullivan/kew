/**
 * The static demo build: a backend-free console on bundled fixtures (#3139).
 *
 * A demo module (`demo-*.mjs`): `scripts/ui-check.mjs` runs it only against
 * the standard-library static server for `build-demo/`, which does the SPA
 * fallback and sends the site's CSP — so a CSP break is a console error here.
 * Nothing is read at import (`ui-check.test.ts` loads every module and calls
 * `checks()` with `visit` set to throw): the world's manifest is read when
 * `checks()` starts, for the refs the check names carry, and every page visit
 * and route walk happens inside `check()`.
 */
// ui-check-owns: src/lib/demo/install.ts
// ui-check-owns: src/lib/demo/respond.ts
// ui-check-owns: src/lib/demo/writes.ts
// ui-check-owns: src/lib/demo/DemoBanner.svelte
// ui-check-owns: src/lib/demo/banner.js
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { DEMO_BANNER, TAKE_A_TOUR, TOURS_DESKTOP_ONLY } from '../../src/lib/demo/banner.js';
import { FRONTEND, assertPainted, squash, widestBox } from '../ui-check-lib.mjs';

const ROUTES_DIR = fileURLToPath(new URL('src/routes/', FRONTEND));
const TOURS_DIR = new URL('demo/tours/', FRONTEND);

/** Every tour file, parsed, in name order. */
const readTours = () =>
  readdirSync(fileURLToPath(TOURS_DIR))
    .filter((name) => name.endsWith('.json'))
    .sort()
    .map((name) => JSON.parse(readFileSync(new URL(name, TOURS_DIR), 'utf8')));

/**
 * The bundled set is the demo world (#3141): every ref a check reads is
 * named by its manifest, never typed here.
 */
const readManifest = () => JSON.parse(readFileSync(new URL('demo/world/manifest.json', FRONTEND), 'utf8'));

/**
 * A refusal names its command as a code span, and the slot draws it the way a
 * title draws one: the command in a mono `<code>`, no backtick in the text.
 */
async function expectMonoCommand(slot, command, expect) {
  const text = (await slot.textContent()) ?? '';
  expect(!text.includes('`'), `the refusal prints a backtick: "${text}"`);
  const code = slot.locator('code');
  const count = await code.count();
  expect(count === 1, `the refusal draws ${count} code spans`);
  const said = (await code.textContent()) ?? '';
  expect(said === command, `the refusal's code span reads "${said}"`);
  const family = await code.evaluate((el) => getComputedStyle(el).fontFamily);
  expect(/Plex Mono/.test(family), `the command is drawn in ${family}`);
}

/**
 * Every page route under `src/routes`, as a URL: a folder holding a
 * `+page.svelte` or `+page.ts`, with route groups dropped and each `[param]`
 * read as the bundled set's own value. A parameter this table does not know is
 * a failure, so a new route cannot be skipped by accident.
 */
function pageRoutes(params, dir = ROUTES_DIR, segments = []) {
  const entries = readdirSync(dir);
  const routes = entries.some((name) => name === '+page.svelte' || name === '+page.ts')
    ? [`/${segments.join('/')}`]
    : [];
  for (const name of entries) {
    if (!statSync(join(dir, name)).isDirectory()) continue;
    const param = /^\[(\w+)\]$/.exec(name);
    if (param && !(param[1] in params)) throw new Error(`no demo value for route parameter [${param[1]}]`);
    const segment = /^\(.+\)$/.test(name) ? null : param ? params[param[1]] : name;
    routes.push(...pageRoutes(params, join(dir, name), segment === null ? segments : [...segments, segment]));
  }
  return routes.sort();
}

export async function checks({ base, check, visit, expect }) {
  const manifest = readManifest();
  /** What a route parameter reads as in the bundled set: an epic and a run it holds. */
  const PARAMS = { ref: manifest.epic_ref, id: manifest.run_id };
  /** The world's own approval hold (`approvals.json`, and its `approval_hold` row in `dashboard.json`). */
  const HOLD = { item_id: manifest.hold_id };

  await check('demo: every route in src/routes loads with no page error', async () => {
    const failures = [];
    for (const route of pageRoutes(PARAMS)) {
      await visit(route, async (page, problems) => {
        const text = (await page.locator('main').first().textContent()) ?? '';
        if (text.trim() === '') failures.push(`${route} rendered an empty <main>`);
        if (problems.length > 0) failures.push(`${route} reported:\n  ${problems.join('\n  ')}`);
      });
    }
    expect(failures.length === 0, failures.join('\n'));
  });

  await check(`demo: a deep link to /epics/${PARAMS.ref} loads the epic`, async () => {
    await visit(`/epics/${PARAMS.ref}`, async (page, problems) => {
      const nodes = page.locator('.dag .svelte-flow__node');
      await nodes.first().waitFor({ state: 'visible', timeout: 10_000 });
      expect((await nodes.count()) > 0, 'the deep-linked epic drew no node');
      expect(problems.length === 0, `the deep link reported:\n${problems.join('\n')}`);
    });
  });

  await check('demo: no request leaves the origin', async () => {
    const origin = new URL(base).origin;
    const outside = [];
    const watch = async (page) => {
      page.on('request', (request) => {
        const url = request.url();
        if (url.startsWith('data:') || url.startsWith('blob:')) return;
        if (new URL(url).origin !== origin) outside.push(url);
      });
    };
    for (const route of pageRoutes(PARAMS)) await visit(route, async () => {}, undefined, watch);
    expect(outside.length === 0, `the demo build asked another origin for:\n${[...new Set(outside)].join('\n')}`);
  });

  await check('demo: Stop the fleet shows the demo sentence', async () => {
    await visit('/work', async (page) => {
      await page.keyboard.press('Control+k');
      const dialog = page.locator('[role="dialog"][aria-label="Go to"]');
      await dialog.waitFor({ state: 'visible', timeout: 5_000 });
      await page.keyboard.type('stop the fleet');
      await dialog.locator('[role="option"]', { hasText: 'Stop the fleet' }).first().click();
      await page.locator('#palette-reason').fill('a demo press');
      await page.locator('#palette-confirm').click();
      const alert = page.locator('[role="alert"]', { hasText: 'Demo: this would' });
      await alert.waitFor({ state: 'visible', timeout: 5_000 });
      const said = ((await alert.textContent()) ?? '').trim();
      expect(
        said === 'Demo: this would kill every running agent and refuse new dispatches until resumed. In kew, run kew stop.',
        `Stop the fleet said "${said}"`
      );
      await expectMonoCommand(alert, 'kew stop', expect);
    });
  });

  await check('demo: approve on a hold shows the demo sentence', async () => {
    await visit(
      '/monitor',
      async (page) => {
        const row = page.locator(`section[aria-label="Holds and blocks"] li[data-hold-item="${HOLD.item_id}"]`);
        const approve = row.getByRole('button', { name: 'Approve', exact: true });
        await approve.waitFor({ state: 'visible', timeout: 10_000 });
        await approve.click();
        const said = row.locator('p.text-danger');
        await said.waitFor({ state: 'visible', timeout: 5_000 });
        const text = ((await said.textContent()) ?? '').trim();
        expect(
          text ===
            `Demo: this would approve the hold on #${HOLD.item_id} and let its dispatch start. ` +
              `In kew, run kew approve ${HOLD.item_id}.`,
          `Approve on the hold said "${text}"`
        );
        await expectMonoCommand(said, `kew approve ${HOLD.item_id}`, expect);
      }
    );
  });

  // X5: the demo answers every `/api` read in the browser, so nothing under
  // `/api/` may reach the network. One that did would be answered by the
  // host's SPA fallback with index.html, and an event stream would reconnect
  // for ever. Every route, and every tour step's page with its tour running.
  await check('demo: no request to /api/ reaches the network, on any route or tour step', async () => {
    const reached = [];
    const watch = async (page) => {
      page.on('request', (request) => {
        const url = new URL(request.url());
        if (url.origin === new URL(base).origin && url.pathname.startsWith('/api/')) reached.push(`${page.url()} -> ${url.pathname}${url.search}`);
      });
    };
    for (const route of pageRoutes(PARAMS)) await visit(route, async () => {}, undefined, watch);
    for (const tour of readTours()) {
      for (const [i, step] of tour.steps.entries()) {
        await visit(
          `${step.route}?tour=${tour.id}&step=${i + 1}`,
          async (page) => {
            await page
              .waitForFunction(
                ([sel, want]) => document.querySelector(sel)?.getAttribute('data-tour-step') === want,
                ['.driver-popover.kew-tour', step.passage],
                { timeout: 15_000 }
              )
              .catch(() => reached.push(`${step.passage}: the step never showed, so its reads were not all seen`));
          },
          { width: 1440, height: 960 },
          watch
        );
      }
    }
    expect(reached.length === 0, `requests to /api/ reached the network:\n${[...new Set(reached)].join('\n')}`);
  });

  for (const size of [
    { width: 1440, height: 960 },
    { width: 390, height: 844 }
  ]) {
    await check(`demo: the banner shows and covers nothing at ${size.width}px`, async () => {
      await visit(
        '/work',
        async (page) => {
          const banner = page.locator('[data-demo-banner]');
          await banner.waitFor({ state: 'visible', timeout: 5_000 });
          const said = ((await banner.locator('[data-demo-banner-text]').textContent()) ?? '').trim();
          expect(said === DEMO_BANNER, `the banner says "${said}"`);
          await assertPainted(page, banner, 'the demo banner');
          const [b, h] = await Promise.all([banner.boundingBox(), page.locator('header').first().boundingBox()]);
          expect(b !== null && h !== null && b.y + b.height <= h.y + 0.5, 'the banner overlaps the header');
          const wide = await widestBox(page);
          expect(wide.w <= wide.c, `${size.width}px scrolls sideways: ${wide.culprit}`);
        },
        size
      );
    });
  }

  // D17: on a phone the banner says what kew is and is the way into the
  // tours: "Take a tour" opens the list, which says tours run on a desktop
  // screen; and the sticky foot is one line.
  await check('demo: at 390px "Take a tour" opens the tour list, which says tours run on a desktop screen, and the foot is one line', async () => {
    await visit(
      '/work',
      async (page) => {
        const toggle = page.locator('[data-demo-tours-toggle]');
        await toggle.waitFor({ state: 'visible', timeout: 5_000 });
        expect(((await toggle.textContent()) ?? '').includes(TAKE_A_TOUR), 'the banner offers no "Take a tour"');
        const box = await toggle.boundingBox();
        expect(box !== null && box.height >= 32, `"Take a tour" is ${box?.height}px tall, under 32`);
        await toggle.click();
        expect((await toggle.getAttribute('aria-expanded')) === 'true', '"Take a tour" did not open');
        const list = page.locator('[data-demo-tour-list]');
        await list.waitFor({ state: 'visible', timeout: 5_000 });
        const titles = squash((await list.textContent()) ?? '');
        for (const tour of readTours()) expect(titles.includes(tour.title), `the tour list does not name "${tour.title}"`);
        const note = list.locator('[data-demo-tour-note]');
        expect(await note.isVisible(), 'the tour list does not say where tours run');
        expect(squash((await note.textContent()) ?? '') === TOURS_DESKTOP_ONLY, `the note says "${await note.textContent()}"`);
        await assertPainted(page, note, 'the desktop-only note');
        const wide = await widestBox(page);
        expect(wide.w <= wide.c, `the open tour list scrolls sideways: ${wide.culprit}`);
        const foot = page.locator('[data-build-history="ready"] > div').first();
        await foot.waitFor({ state: 'visible', timeout: 10_000 });
        // One line is the 32px toggle and its padding; a wrapped line was ~100px.
        const height = await foot.evaluate((el) => el.getBoundingClientRect().height);
        expect(height <= 48, `the foot's line is ${Math.round(height)}px tall at 390px, more than one line`);
      },
      { width: 390, height: 844 }
    );
  });

  await check('demo: from 1024px up a tour starts from the banner\'s "Take a tour" list, and ends with focus back on it', async () => {
    const [tour] = readTours();
    await visit(
      '/work',
      async (page, problems) => {
        await page.locator('[data-demo-tours-toggle]').click();
        const start = page.locator(`[data-banner-tour-start="${tour.id}"]`);
        await start.waitFor({ state: 'visible', timeout: 5_000 });
        expect(!(await page.locator('[data-demo-tour-note]').isVisible()), 'the desktop-only note shows at 1440px');
        await start.click();
        await page.waitForFunction(
          ([sel, want]) => document.querySelector(sel)?.getAttribute('data-tour-step') === want,
          ['.driver-popover.kew-tour', `${tour.id}/1`],
          { timeout: 15_000 }
        );
        await page.keyboard.press('Escape');
        await page.locator('.driver-popover.kew-tour').waitFor({ state: 'detached', timeout: 5_000 });
        const focused = await page.evaluate(() => document.activeElement?.hasAttribute('data-demo-tours-toggle') ?? false);
        expect(focused, 'focus did not come back to "Take a tour"');
        expect(problems.length === 0, `the tour reported:\n${problems.join('\n')}`);
      },
      { width: 1440, height: 960 }
    );
  });
}
