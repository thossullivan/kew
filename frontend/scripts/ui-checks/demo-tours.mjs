/**
 * The demo's guided tours (#3145): every step's target is on its route, a
 * deep link opens the step it names, Escape ends a tour and hands focus back,
 * an ended tour stays ended through the page's own writes, a reload and Back,
 * and below 1024px the tours give way to one line.
 *
 * Design review c9: a link no tour can open (an unknown tour, a step past
 * the end, a phone) is taken out of the address (X6); and a tour ends with
 * its close button, with Escape midway and with Done, switches to another
 * tour midway, starts again after it was closed, and follows the browser's
 * Back and Forward across routes (X8).
 *
 * A demo module (`demo-*.mjs`): `scripts/ui-check.mjs` runs it only against the
 * static server for `build-demo/`. Every read happens inside `check()`:
 * `ui-check.test.ts` loads every module with `visit` set to throw.
 *
 * The target check walks EVERY tour file under `demo/tours/`, so a console
 * change that drops a `data-tour` attribute a tour points at fails here.
 */
// ui-check-owns: src/lib/demo/tours/**
// ui-check-owns: demo/tours/*.json
import { readFileSync, readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { TOURS_DESKTOP_ONLY as NOTE } from '../../src/lib/demo/banner.js';
import { FRONTEND, assertPainted, stepSelector } from '../ui-check-lib.mjs';

const TOURS_DIR = new URL('demo/tours/', FRONTEND);
const DESKTOP = { width: 1440, height: 960 };
const PHONE = { width: 390, height: 844 };
const POPOVER = '.driver-popover.kew-tour';
const MORE = 'summary[aria-label="More destinations"]';

/** Every tour file, parsed. */
function tours() {
  const dir = fileURLToPath(TOURS_DIR);
  return readdirSync(dir)
    .filter((name) => name.endsWith('.json'))
    .sort()
    .map((name) => JSON.parse(readFileSync(`${dir}${name}`, 'utf8')));
}

const target = (page, step) => page.locator(stepSelector(step));

/** Wait for the popover to show `<tour>/<n>`. */
async function shows(page, passage) {
  await page.waitForFunction(
    ([sel, want]) => document.querySelector(sel)?.getAttribute('data-tour-step') === want,
    [POPOVER, passage],
    { timeout: 15_000 }
  );
}

/** The tour popover is gone, the overlay with it, and the address names no tour. */
async function ended(page, expect, how) {
  await page.locator(POPOVER).waitFor({ state: 'detached', timeout: 5_000 });
  expect((await page.locator('.driver-overlay').count()) === 0, `${how}: the overlay stayed`);
  const left = await page.evaluate(() => location.href);
  expect(!new URL(left).searchParams.has('tour'), `${how}: the ended tour left ${left}`);
}

/** Where a step opens: its route, with its query (a replay moment) when it names one. */
function stepUrl(step) {
  const query = new URLSearchParams(step.query ?? {}).toString();
  return query === '' ? step.route : `${step.route}?${query}`;
}

/** The tour link the address bar holds, as `<tour>/<step>`, or '' for none. */
function linkIn(page) {
  const url = new URL(page.url());
  const tour = url.searchParams.get('tour');
  return tour ? `${tour}/${url.searchParams.get('step')}` : '';
}

/** Open the More menu and start `id` from its Tours entry. */
async function startFromMenu(page, id) {
  await page.locator(MORE).click();
  const start = page.locator(`[data-tour-start="${id}"]`);
  await start.waitFor({ state: 'visible', timeout: 5_000 });
  await start.click();
  await page.locator(POPOVER).waitFor({ state: 'visible', timeout: 10_000 });
}

export async function checks({ check, visit, expect }) {
  await check('demo tours: every step of every tour finds its target on its route at 1440px', async () => {
    const all = tours();
    expect(all.length > 0, 'demo/tours/ holds no tour');
    const missing = [];
    for (const tour of all) {
      for (const [index, step] of tour.steps.entries()) {
        await visit(
          stepUrl(step),
          async (page) => {
            const found = target(page, step);
            try {
              await found.first().waitFor({ state: 'visible', timeout: 10_000 });
            } catch {
              missing.push(`${tour.id} step ${index + 1}: no visible ${stepSelector(step)} on ${stepUrl(step)}`);
              return;
            }
            const count = await found.count();
            if (count !== 1) missing.push(`${tour.id} step ${index + 1}: ${count} ${stepSelector(step)} on ${stepUrl(step)}`);
          },
          DESKTOP
        );
      }
    }
    expect(missing.length === 0, missing.join('\n'));
  });

  await check('demo tours: a deep link opens the step it names, on its route, highlighted', async () => {
    for (const tour of tours()) {
      const n = tour.steps.length;
      const step = tour.steps[n - 1];
      // Opened from the start route, so a last step on another route navigates first.
      await visit(
        `${tour.start}?tour=${tour.id}&step=${n}`,
        async (page, problems) => {
          const popover = page.locator(POPOVER);
          await popover.waitFor({ state: 'visible', timeout: 10_000 });
          await page.waitForFunction(
            (sel) => document.querySelector(sel)?.classList.contains('driver-active-element'),
            stepSelector(step),
            { timeout: 10_000 }
          );
          const shown = await popover.getAttribute('data-tour-step');
          expect(shown === `${tour.id}/${n}`, `?tour=${tour.id}&step=${n} opened ${shown}`);
          const path = new URL(page.url()).pathname;
          expect(path === step.route, `step ${n} is on ${step.route}, the tour is on ${path}`);
          expect(linkIn(page) === `${tour.id}/${n}`, `the address bar reads ${page.url()}`);
          const docs = await popover.locator('a.kew-tour-docs').getAttribute('href');
          expect(docs?.startsWith('https://kew.dev/docs/') && docs.includes('#'), `the docs link is ${docs}`);
          const title = ((await popover.locator('.driver-popover-title').textContent()) ?? '').trim();
          expect(title !== '', 'the step has no title');
          await assertPainted(page, popover.locator('.driver-popover-description p').first(), 'the step text');
          expect(problems.length === 0, `the tour reported:\n${problems.join('\n')}`);
        },
        DESKTOP
      );
    }
  });

  await check('demo tours: the highlight is a neutral text-1 outline, and the popover is a Float', async () => {
    const [tour] = tours();
    await visit(
      `${tour.start}?tour=${tour.id}&step=1`,
      async (page) => {
        const popover = page.locator(POPOVER);
        await popover.waitFor({ state: 'visible', timeout: 10_000 });
        const step = target(page, tour.steps[0]);
        await page.waitForFunction(
          (sel) => document.querySelector(sel)?.classList.contains('driver-active-element'),
          stepSelector(tour.steps[0]),
          { timeout: 10_000 }
        );
        const read = await page.evaluate(
          ([sel, pop]) => {
            const probe = document.createElement('span');
            document.body.append(probe);
            const token = (name) => {
              probe.style.color = `var(${name})`;
              return getComputedStyle(probe).color;
            };
            const tokens = { text1: token('--text-1'), raised: token('--ink-raised'), strong: token('--hair-strong') };
            probe.remove();
            const el = getComputedStyle(document.querySelector(sel));
            const box = getComputedStyle(document.querySelector(pop));
            const title = getComputedStyle(document.querySelector(`${pop} .driver-popover-description`));
            const heading = getComputedStyle(document.querySelector(`${pop} .driver-popover-title`));
            return {
              tokens,
              outline: [el.outlineStyle, el.outlineColor],
              surface: box.backgroundColor,
              border: box.borderTopColor,
              shadow: box.boxShadow,
              font: title.fontFamily,
              size: title.fontSize,
              heading: [heading.fontSize, heading.fontWeight, heading.fontFamily]
            };
          },
          [stepSelector(tour.steps[0]), POPOVER]
        );
        expect(read.outline[0] === 'solid' && read.outline[1] === read.tokens.text1, `the highlight is ${read.outline.join(' ')}`);
        expect(read.surface === read.tokens.raised, `the popover surface is ${read.surface}`);
        expect(read.border === read.tokens.strong, `the popover border is ${read.border}`);
        expect(read.shadow !== 'none', 'the popover has no shadow');
        expect(/Plex Sans/.test(read.font) && read.size === '13px', `the step text is ${read.size} ${read.font}`);
        // DESIGN.md: the title is {typography.prose-15}, Plex Sans 15 at 400.
        const [hSize, hWeight, hFont] = read.heading;
        expect(hSize === '15px' && hWeight === '400' && /Plex Sans/.test(hFont), `the step title is ${read.heading.join(' ')}`);
        expect((await step.count()) === 1, 'the target is gone');
      },
      DESKTOP
    );
  });

  await check('demo tours: Next moves the link, Escape ends the tour and gives focus back to More', async () => {
    const [tour] = tours();
    await visit(
      tour.start,
      async (page, problems) => {
        await startFromMenu(page, tour.id);
        expect(linkIn(page) === `${tour.id}/1`, `starting wrote ${page.url()}`);
        if (tour.steps.length > 1) {
          await page.locator(`${POPOVER} .driver-popover-next-btn`).click();
          await page.waitForFunction(
            (sel) => document.querySelector(sel)?.getAttribute('data-tour-step')?.endsWith('/2'),
            POPOVER,
            { timeout: 10_000 }
          );
          expect(linkIn(page) === `${tour.id}/2`, `Next wrote ${page.url()}`);
        }
        await page.keyboard.press('Escape');
        await page.locator(POPOVER).waitFor({ state: 'detached', timeout: 5_000 });
        expect(linkIn(page) === '', `the ended tour left ${page.url()}`);
        const focused = await page.evaluate((sel) => document.activeElement === document.querySelector(sel), MORE);
        expect(focused, 'focus did not come back to the More press');
        const overlay = await page.locator('.driver-overlay').count();
        expect(overlay === 0, 'the overlay stayed after Escape');
        expect(problems.length === 0, `the tour reported:\n${problems.join('\n')}`);
      },
      DESKTOP
    );
  });

  await check('demo tours: Escape pressed with focus inside the popover ends the tour and takes focus out of it', async () => {
    const [tour] = tours();
    await visit(
      tour.start,
      async (page) => {
        await startFromMenu(page, tour.id);
        // driver.js keeps Tab inside the popover while a tour is open; Escape
        // is the way out, and it must work from a control in the popover.
        const button = page.locator(`${POPOVER} button:visible`).first();
        await button.focus();
        const inside = await page.evaluate((sel) => document.querySelector(sel)?.contains(document.activeElement), POPOVER);
        expect(inside === true, 'no control in the popover took focus');
        await page.keyboard.press('Escape');
        await page.locator(POPOVER).waitFor({ state: 'detached', timeout: 5_000 });
        const where = await page.evaluate(() => {
          const el = document.activeElement;
          return { body: el === document.body, inTour: Boolean(el?.closest('.driver-popover')), label: el?.getAttribute('aria-label') ?? el?.tagName };
        });
        expect(!where.inTour && !where.body, `after Escape focus is on ${where.label}`);
      },
      DESKTOP
    );
  });

  // The Work page builds every write from its own state key, or `page.url`,
  // which shallow routing never moves: a tour link left in either came back
  // with the next row click, after the tour had ended.
  const workTour = () => {
    const tour = tours().find((t) => t.start === '/work');
    expect(tour !== undefined, 'no tour starts on /work: point these checks at a tour that does, and at a write its page makes');
    return tour;
  };
  const ROW = 'a[data-item]';
  /** The address bar as the page holds it now, not as Playwright last heard of it. */
  const here = (page) => page.evaluate(() => location.href);
  const noPopover = async (page) => {
    try {
      await page.locator(POPOVER).waitFor({ state: 'visible', timeout: 2_000 });
      return false;
    } catch {
      return true;
    }
  };

  await check('demo tours: a deep-linked tour that ended stays ended through a row click, a reload and Monitor-then-Back', async () => {
    const tour = workTour();
    await visit(
      `/work?tour=${tour.id}&step=1`,
      async (page, problems) => {
        await page.locator(POPOVER).waitFor({ state: 'visible', timeout: 10_000 });
        await page.keyboard.press('Escape');
        await page.locator(POPOVER).waitFor({ state: 'detached', timeout: 5_000 });
        const focused = await page.evaluate((sel) => document.activeElement === document.querySelector(sel), MORE);
        expect(focused, 'a deep-linked tour ended with focus off the More press');
        const row = page.locator(ROW).first();
        await row.click();
        await page.waitForFunction(() => new URL(location.href).searchParams.has('item'), null, { timeout: 5_000 });
        const clicked = await here(page);
        expect(!clicked.includes('tour='), `a row click after the tour ended wrote ${clicked}`);
        await page.keyboard.press('ArrowDown');
        const pressed = await here(page);
        expect(!pressed.includes('tour='), `an arrow press after the tour ended wrote ${pressed}`);
        await page.locator('header a[href="/monitor"]').first().click();
        await page.waitForURL((url) => url.pathname === '/monitor', { timeout: 10_000 });
        await page.goBack();
        await page.waitForURL((url) => url.pathname === '/work', { timeout: 10_000 });
        expect(await noPopover(page), `Back to ${page.url()} opened the ended tour again`);
        await page.reload();
        await page.locator(ROW).first().waitFor({ state: 'visible', timeout: 10_000 });
        expect(await noPopover(page), `a reload of ${page.url()} opened the ended tour again`);
        expect(problems.length === 0, `the tour reported:\n${problems.join('\n')}`);
      },
      DESKTOP
    );
  });

  await check('demo tours: Back and Forward over a row click mid-tour leave the address and the tour agreeing', async () => {
    const tour = workTour();
    await visit(
      '/monitor',
      async (page, problems) => {
        await page.locator('header a[href="/work"]').first().click();
        await page.waitForURL((url) => url.pathname === '/work', { timeout: 10_000 });
        await page.locator(ROW).first().waitFor({ state: 'visible', timeout: 10_000 });
        await startFromMenu(page, tour.id);
        // The Work page's own write: the link stays in the address, and the
        // state it writes has no tour in it.
        await page.locator(ROW).first().dispatchEvent('click');
        await page.waitForFunction(() => new URL(location.href).searchParams.has('item'), null, { timeout: 5_000 });
        await page.goBack();
        await page.waitForURL((url) => url.pathname === '/monitor', { timeout: 10_000 });
        await page.locator(POPOVER).waitFor({ state: 'detached', timeout: 5_000 });
        await page.goForward();
        await page.waitForURL((url) => url.pathname === '/work', { timeout: 10_000 });
        // The address Forward brings back names step 1, so step 1 shows: a
        // closed tour under an address that names one would reopen on reload.
        await page.locator(POPOVER).waitFor({ state: 'visible', timeout: 10_000 });
        const shown = await page.locator(POPOVER).getAttribute('data-tour-step');
        const back = new URL(await here(page));
        expect(shown === `${tour.id}/1`, `Forward showed ${shown}`);
        expect(back.searchParams.get('tour') === tour.id && back.searchParams.get('step') === '1', `Forward left ${back}`);
        await page.keyboard.press('Escape');
        await page.locator(POPOVER).waitFor({ state: 'detached', timeout: 5_000 });
        const ended = await here(page);
        expect(!ended.includes('tour='), `the ended tour left ${ended}`);
        await page.reload();
        await page.locator(ROW).first().waitFor({ state: 'visible', timeout: 10_000 });
        expect(await noPopover(page), `a reload of ${await here(page)} opened the ended tour again`);
        expect(problems.length === 0, `the tour reported:\n${problems.join('\n')}`);
      },
      DESKTOP
    );
  });

  await check("demo tours: mid-tour, a row click writes the step in force, not the deep link's", async () => {
    const tour = workTour();
    expect(tour.steps.length > 1 && tour.steps[1].route === '/work', `${tour.id} has no second step on /work`);
    await visit(
      `/work?tour=${tour.id}&step=1`,
      async (page) => {
        await page.locator(POPOVER).waitFor({ state: 'visible', timeout: 10_000 });
        await page.locator(`${POPOVER} .driver-popover-next-btn`).click();
        await page.waitForFunction(
          (sel) => document.querySelector(sel)?.getAttribute('data-tour-step')?.endsWith('/2'),
          POPOVER,
          { timeout: 10_000 }
        );
        // The overlay takes a pointer click (it would end the tour), so the
        // click is dispatched on the row itself, as a keyboard Enter would be.
        await page.locator(ROW).first().dispatchEvent('click');
        await page.waitForFunction(() => new URL(location.href).searchParams.has('item'), null, { timeout: 5_000 });
        const written = new URL(await here(page));
        const link = `${written.searchParams.get('tour')}/${written.searchParams.get('step')}`;
        expect(link === `${tour.id}/2`, `a row click at step 2 wrote ${written}`);
      },
      DESKTOP
    );
  });

  await check('demo tours: a tour open when the window narrows below 1024px ends', async () => {
    const [tour] = tours();
    await visit(
      tour.start,
      async (page) => {
        await startFromMenu(page, tour.id);
        await page.setViewportSize({ width: 900, height: 900 });
        await page.locator(POPOVER).waitFor({ state: 'detached', timeout: 5_000 });
        expect((await page.locator('.driver-overlay').count()) === 0, 'the overlay stayed below 1024px');
        expect(linkIn(page) === '', `the ended tour left ${page.url()}`);
      },
      DESKTOP
    );
  });

  await check('demo tours: at 390px the entry is hidden, the note shows, and a deep link opens nothing and leaves the address', async () => {
    const [tour] = tours();
    await visit(
      `${tour.start}?tour=${tour.id}&step=1`,
      async (page) => {
        // A link no tour can open here is taken out of the address (X6).
        await page.waitForFunction(() => !new URL(location.href).searchParams.has('tour'), null, { timeout: 10_000 });
        const left = await page.evaluate(() => location.href);
        expect(!new URL(left).searchParams.has('step'), `a deep link at 390px left ${left}`);
        await page.locator(MORE).click();
        const note = page.locator('[data-tour-note]');
        await note.waitFor({ state: 'visible', timeout: 5_000 });
        const said = ((await note.textContent()) ?? '').trim();
        expect(said === NOTE, `the note says "${said}"`);
        expect(!(await page.locator('[data-tour-entry]').isVisible()), 'the Tours entry shows at 390px');
        expect((await page.locator(POPOVER).count()) === 0, 'a deep link opened a tour at 390px');
        await assertPainted(page, note, 'the desktop-only note');
      },
      PHONE
    );
  });

  // X6: a link no tour can open opens nothing, and the address loses it, so
  // the page's next write cannot carry it on.
  for (const [what, link] of [
    ['a tour the build does not have', 'tour=nope&step=1'],
    ['a step past the end of its tour', 'tour={id}&step=99'],
    ['step 0 of its tour', 'tour={id}&step=0'],
    ['a step that is not a number', 'tour={id}&step=abc']
  ]) {
    await check(`demo tours: ${what} opens nothing and is taken out of the address`, async () => {
      const tour = workTour();
      const query = link.replace('{id}', tour.id);
      await visit(
        `/work?${query}`,
        async (page, problems) => {
          await page.waitForFunction(() => !new URL(location.href).searchParams.has('tour'), null, { timeout: 10_000 });
          const left = new URL(await here(page));
          expect(!left.searchParams.has('step'), `?${query} left ${left.search}`);
          expect(await noPopover(page), `?${query} opened a tour`);
          await page.locator(ROW).first().click();
          await page.waitForFunction(() => new URL(location.href).searchParams.has('item'), null, { timeout: 5_000 });
          const clicked = await here(page);
          expect(!clicked.includes('tour='), `a row click after ?${query} wrote ${clicked}`);
          expect(problems.length === 0, `?${query} reported:\n${problems.join('\n')}`);
        },
        DESKTOP
      );
    });
  }

  // X8: the ways a tour ends, switches and starts again.
  await check("demo tours: the popover's close button ends a tour midway and takes the link out", async () => {
    const [tour] = tours();
    await visit(
      tour.start,
      async (page, problems) => {
        await startFromMenu(page, tour.id);
        await page.locator(`${POPOVER} .driver-popover-next-btn`).click();
        await shows(page, `${tour.id}/2`);
        await page.locator(`${POPOVER} .driver-popover-close-btn`).click();
        await ended(page, expect, 'the close button');
        const focused = await page.evaluate((sel) => document.activeElement === document.querySelector(sel), MORE);
        expect(focused, 'focus did not come back to the More press after the close button');
        expect(problems.length === 0, `the tour reported:\n${problems.join('\n')}`);
      },
      DESKTOP
    );
  });

  await check('demo tours: Escape midway, on a later step, ends the tour and takes the link out', async () => {
    const tour = workTour();
    await visit(
      `/work?tour=${tour.id}&step=2`,
      async (page, problems) => {
        await shows(page, `${tour.id}/2`);
        await page.keyboard.press('Escape');
        await ended(page, expect, 'Escape at step 2');
        expect(problems.length === 0, `the tour reported:\n${problems.join('\n')}`);
      },
      DESKTOP
    );
  });

  await check('demo tours: Done on the last step of every tour ends it and takes the link out', async () => {
    for (const tour of tours()) {
      const n = tour.steps.length;
      await visit(
        `${tour.start}?tour=${tour.id}&step=${n}`,
        async (page, problems) => {
          await shows(page, `${tour.id}/${n}`);
          const done = page.locator(`${POPOVER} .driver-popover-next-btn`);
          expect(((await done.textContent()) ?? '').trim() === 'Done', `${tour.id}'s last step offers no Done`);
          await done.click();
          await ended(page, expect, `Done on ${tour.id}/${n}`);
          expect(problems.length === 0, `${tour.id} reported:\n${problems.join('\n')}`);
        },
        DESKTOP
      );
    }
  });

  await check('demo tours: starting another tour midway ends the first and opens the second on its step 1', async () => {
    const [first, second] = tours();
    await visit(
      first.start,
      async (page, problems) => {
        await startFromMenu(page, first.id);
        await page.locator(`${POPOVER} .driver-popover-next-btn`).click();
        await shows(page, `${first.id}/2`);
        // The press a visitor makes on the Tours entry, past the tour's overlay.
        await page.locator(`[data-tour-start="${second.id}"]`).dispatchEvent('click');
        await shows(page, `${second.id}/1`);
        const path = new URL(await here(page)).pathname;
        expect(path === second.steps[0].route, `${second.id}/1 is on ${second.steps[0].route}, the tour is on ${path}`);
        const switched = new URL(await here(page));
        expect(
          switched.searchParams.get('tour') === second.id && switched.searchParams.get('step') === '1',
          `the address reads ${switched.search}`
        );
        await page.keyboard.press('Escape');
        await ended(page, expect, `Escape on ${second.id}/1`);
        expect(problems.length === 0, `the tours reported:\n${problems.join('\n')}`);
      },
      DESKTOP
    );
  });

  await check('demo tours: a tour closed midway starts again from the menu on its step 1', async () => {
    const [tour] = tours();
    await visit(
      tour.start,
      async (page, problems) => {
        await startFromMenu(page, tour.id);
        await page.locator(`${POPOVER} .driver-popover-next-btn`).click();
        await shows(page, `${tour.id}/2`);
        await page.locator(`${POPOVER} .driver-popover-close-btn`).click();
        await ended(page, expect, 'the close button');
        await startFromMenu(page, tour.id);
        await shows(page, `${tour.id}/1`);
        const again = new URL(await here(page));
        expect(again.searchParams.get('tour') === tour.id && again.searchParams.get('step') === '1', `the restart wrote ${again.search}`);
        expect(problems.length === 0, `the tour reported:\n${problems.join('\n')}`);
      },
      DESKTOP
    );
  });

  await check("demo tours: the browser's Back across routes mid-tour shows the step that address names, and Forward the next", async () => {
    const tour = tours().find((t) => t.steps.some((s, i) => i > 0 && s.route !== t.steps[i - 1].route));
    expect(tour !== undefined, 'no tour moves across routes');
    const i = tour.steps.findIndex((s, k) => k > 0 && s.route !== tour.steps[k - 1].route);
    const [from, to] = [tour.steps[i - 1], tour.steps[i]];
    await visit(
      `${from.route}?tour=${tour.id}&step=${i}`,
      async (page, problems) => {
        await shows(page, from.passage);
        await page.locator(`${POPOVER} .driver-popover-next-btn`).click();
        await shows(page, to.passage);
        await page.waitForURL((url) => url.pathname === to.route, { timeout: 10_000 });
        await page.goBack();
        await page.waitForURL((url) => url.pathname === from.route, { timeout: 10_000 });
        await shows(page, from.passage);
        const back = new URL(await here(page));
        expect(back.searchParams.get('step') === String(i), `Back left ${back.pathname}${back.search}, not step ${i}`);
        await page.goForward();
        await page.waitForURL((url) => url.pathname === to.route, { timeout: 10_000 });
        await shows(page, to.passage);
        await page.keyboard.press('Escape');
        await ended(page, expect, 'Escape after Back and Forward');
        expect(problems.length === 0, `the tour reported:\n${problems.join('\n')}`);
      },
      DESKTOP
    );
  });
}
