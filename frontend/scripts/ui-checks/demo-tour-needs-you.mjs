/**
 * The "What needs me?" tour (#3146), run start to end at three desktop sizes
 * (1440x960, and the short laptops 1366x650 and 1024x768; tours start at 1024
 * wide): every step shows on its route, highlights its target, and says
 * exactly the words of its passage in `docs/` — the title, then each body
 * paragraph in order.
 *
 * At every size each highlighted target has a box, and the whole box is on
 * screen: inside the window AND inside every panel around it that clips. The
 * Monitor scrolls inside its own panel and driver.js only checks the window,
 * so a step's Copy button or merge row could sit under the panel's foot; and a
 * target that renders only on a tall window highlighted an empty box on a
 * laptop. The Monitor steps are also checked on a deep link and on Back from
 * the epic page.
 *
 * The passages are read here straight from the markdown, not through
 * `demo/tour-bundle.ts`, so a bundler that drops or rewrites a word fails this
 * check instead of agreeing with itself.
 *
 * A range click on the Monitor's event stream keeps the tour open (#3468 C8):
 * the stream built its next address from `page.url`, which shallow routing
 * never moves, and replaced `page.state` whole, so a preset click mid-tour
 * dropped `?tour=&step=` from the address bar and the tour's state with it.
 *
 * A demo module (`demo-*.mjs`): `scripts/ui-check.mjs` runs it only against the
 * static server for `build-demo/`. Every read happens inside `check()`:
 * `ui-check.test.ts` loads every module with `visit` set to throw.
 */
// ui-check-owns: demo/tours/needs-you.json
// ui-check-owns: src/routes/(factory)/monitor/+page.svelte
import { readFileSync } from 'node:fs';
import {
  CLEAR_SIZES,
  FRONTEND,
  TOUR_SIZES,
  docsPassage,
  onScreen,
  popoverCoversNothing,
  showing,
  sizeName,
  squash
} from '../ui-check-lib.mjs';

const TOUR = new URL('demo/tours/needs-you.json', FRONTEND);
const MANIFEST = new URL('demo/world/manifest.json', FRONTEND);
const SIZES = TOUR_SIZES;
const SHORT = SIZES.slice(1);
const POPOVER = '.driver-popover.kew-tour';
const MORE = 'summary[aria-label="More destinations"]';
const COPY = '[data-tour="hold-command"] button[aria-label^="Copy command"]';
const MERGE = '[data-tour="merge-decision"]';

/**
 * Fail unless the header's needs-you target frames the whole count — the
 * diamond, the number and the words "needs you" — not the digit alone.
 */
async function framesCount(page, expect, what) {
  const seen = await page.evaluate(() => {
    const el = document.querySelector('[data-tour="needs-you-count"]');
    if (!el) return null;
    const box = el.getBoundingClientRect();
    const within = (node) => {
      const r = node.getBoundingClientRect();
      return r.width > 0 && r.left >= box.left - 0.5 && r.right <= box.right + 0.5 && r.top >= box.top - 0.5 && r.bottom <= box.bottom + 0.5;
    };
    const words = [...el.querySelectorAll('span')].find((s) => s.textContent.trim() === 'needs you');
    const count = el.querySelector('[data-needs-you-count]');
    const mark = el.firstElementChild;
    return {
      box: [box.left, box.top, box.right, box.bottom].map(Math.round),
      words: words ? within(words) : false,
      count: count ? within(count) : false,
      mark: mark && mark !== count ? within(mark) : false
    };
  });
  expect(seen !== null, `${what}: no [data-tour="needs-you-count"]`);
  expect(
    seen.words && seen.count && seen.mark,
    `${what}: the highlight at ${JSON.stringify(seen.box)} does not frame the diamond, the number and "needs you" ` +
      `(diamond ${seen.mark}, number ${seen.count}, words ${seen.words})`
  );
}

export async function checks({ check, visit, expect }) {
  for (const size of SIZES) {
    await check(
      `demo tours: "What needs me?" runs start to end at ${sizeName(size)}, each popover saying its docs passage over a target wholly on screen`,
      async () => {
        const tour = JSON.parse(readFileSync(TOUR, 'utf8'));
        const { epic_ref: epic } = JSON.parse(readFileSync(MANIFEST, 'utf8'));
        for (const step of tour.steps) {
          if (step.route.startsWith('/epics/')) {
            expect(step.route === `/epics/${epic}`, `${step.passage} is on ${step.route}, not the world's epic ${epic}`);
          }
        }
        // The tour ends on its own point, the hold's Copy command, never on
        // the Build history foot while its figures are samples (c9, V3).
        expect(tour.steps.at(-1).target === 'hold-command', `the tour ends on ${tour.steps.at(-1).target}, not the hold's command`);
        const clear = CLEAR_SIZES.some((s) => s.width === size.width && s.height === size.height);
        await visit(
          tour.start,
          async (page, problems) => {
            await page.locator(MORE).click();
            const start = page.locator(`[data-tour-start="${tour.id}"]`);
            await start.waitFor({ state: 'visible', timeout: 5_000 });
            await start.click();
            const popover = page.locator(POPOVER);
            for (const [index, step] of tour.steps.entries()) {
              const n = index + 1;
              const at = `step ${n} at ${sizeName(size)}`;
              await showing(page, step.passage, step.target);
              const path = new URL(page.url()).pathname;
              expect(path === step.route, `step ${n} is on ${step.route}, the tour is on ${path}`);
              await onScreen(page, expect, `[data-tour="${step.target}"]`, at);
              // driver.js sets overflow:hidden on a target's parent that scrolls,
              // which freezes the page's scroller and shifts its layout.
              const frozen = await page.locator('.driver-active-element-parent-no-scroll').count();
              expect(frozen === 0, `${at}: the highlight stopped the scrolling of its target's parent`);
              if (step.target === 'hold-command') await onScreen(page, expect, COPY, `${at}, its Copy button`);
              if (step.target === 'needs-you-count') await framesCount(page, expect, at);
              if (clear) await popoverCoversNothing(page, expect, step, at);
              const want = docsPassage(step.docs, step.passage);
              const title = squash((await popover.locator('.driver-popover-title').textContent()) ?? '');
              expect(title === want.title, `step ${n}'s title is "${title}", the docs say "${want.title}"`);
              const body = (await popover.locator('.driver-popover-description p').allTextContents()).map(squash);
              expect(
                JSON.stringify(body) === JSON.stringify(want.body),
                `step ${n} says ${JSON.stringify(body)}\nthe docs say ${JSON.stringify(want.body)}`
              );
              await popover.locator('.driver-popover-next-btn').click();
            }
            await popover.waitFor({ state: 'detached', timeout: 5_000 });
            expect((await page.locator('.driver-overlay').count()) === 0, 'the overlay stayed after the last step');
            const left = await page.evaluate(() => location.href);
            expect(!new URL(left).searchParams.has('tour'), `the finished tour left ${left}`);
            expect(problems.length === 0, `the tour reported:\n${problems.join('\n')}`);
          },
          size
        );
      }
    );
  }

  for (const size of SHORT) {
    await check(
      `demo tours: at ${sizeName(size)} the Monitor steps show the merge row and the Copy button whole, on a deep link and on Back`,
      async () => {
        const tour = JSON.parse(readFileSync(TOUR, 'utf8'));
        const merge = tour.steps.findIndex((step) => step.target === 'merge-decision');
        const command = tour.steps.findIndex((step) => step.target === 'hold-command');
        expect(command === merge + 1, 'the tour no longer has the command step right after the merge step');
        const before = tour.steps[merge - 1];
        expect(before !== undefined && before.route !== '/monitor', 'the step before the merge step is no longer on another route');
        await visit(
          `/work?tour=${tour.id}&step=${command + 1}`,
          async (page, problems) => {
            const popover = page.locator(POPOVER);
            await showing(page, tour.steps[command].passage, 'hold-command');
            await onScreen(page, expect, COPY, `a deep link to step ${command + 1} at ${sizeName(size)}, its Copy button`);
            await popover.locator('.driver-popover-prev-btn').click();
            await showing(page, tour.steps[merge].passage, 'merge-decision');
            await onScreen(page, expect, MERGE, `Back from step ${command + 1} to step ${merge + 1} at ${sizeName(size)}`);
            await popover.locator('.driver-popover-prev-btn').click();
            await showing(page, before.passage, before.target);
            await popover.locator('.driver-popover-next-btn').click();
            await showing(page, tour.steps[merge].passage, 'merge-decision');
            await onScreen(page, expect, MERGE, `Next from step ${merge} to step ${merge + 1} at ${sizeName(size)}`);
            await popover.locator('.driver-popover-next-btn').click();
            await showing(page, tour.steps[command].passage, 'hold-command');
            await onScreen(page, expect, COPY, `Next to step ${command + 1} at ${sizeName(size)}, its Copy button`);
            expect(problems.length === 0, `the tour reported:\n${problems.join('\n')}`);
          },
          size
        );
      }
    );
  }

  await check('demo tours: a range click at the Monitor step keeps the tour in the address bar and open', async () => {
    const tour = JSON.parse(readFileSync(TOUR, 'utf8'));
    const index = tour.steps.findIndex((step) => step.route === '/monitor');
    expect(index >= 0, 'the tour has no Monitor step');
    const step = index + 1;
    await visit(
      `/work?tour=${tour.id}&step=${step}`,
      async (page, problems) => {
        await showing(page, tour.steps[index].passage, tour.steps[index].target);
        const range = page.locator('section[aria-label="Event stream"] [role="group"][aria-label="Range"] button');
        const preset = range.filter({ hasText: /^1h$/ }).first();
        await preset.waitFor({ state: 'attached', timeout: 5_000 });
        // The click a reader makes on the preset, past the tour's overlay.
        await preset.dispatchEvent('click');
        await page.waitForFunction(() => new URL(location.href).searchParams.has('range'), null, { timeout: 5_000 });
        const here = new URL(await page.evaluate(() => location.href));
        expect(
          here.searchParams.get('tour') === tour.id && here.searchParams.get('step') === String(step),
          `the range click left ${here.pathname}${here.search}, not tour=${tour.id}&step=${step}`
        );
        expect((await preset.getAttribute('aria-pressed')) === 'true', 'the 1h preset is not pressed after its click');
        await page.waitForTimeout(500);
        expect((await page.locator(POPOVER).count()) === 1, 'the tour popover closed on the range click');
        await showing(page, tour.steps[index].passage, tour.steps[index].target);
        expect(problems.length === 0, `the tour reported:\n${problems.join('\n')}`);
      },
      SIZES[0]
    );
  });
}
