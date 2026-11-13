/**
 * The "Why was this PR refused?" tour (#3148), run start to end at the three
 * desktop sizes tours run at (1440x960, and the short laptops 1366x650 and
 * 1024x768): every step says exactly the words of its docs passage over a
 * target wholly on screen, and every refusal step shows its reading on the
 * world's refused item, read off the screen the step highlights:
 *
 * - the review's send-back (`NEEDS_CHANGES`, round 1) and the `READY` after
 *   the fix (round 2), each "attested by this run", round 2's reviewed head
 *   the refused head and round 1's another;
 * - the failed merge verify at the refused head, and the merge cell above it
 *   reading "needs you · merge decision": the verify held the merge and the
 *   READY stands, so the pull request waits on a person;
 * - the held-out test ids with the declared one excused, where the tour ends.
 *
 * At 1440x960 and 1366x650 the popover covers no step's target.
 *
 * At the short laptops a deep link to step 3 and to step 4 lands on item 107
 * from an address on no item or another one, and the tour's Back from step 3
 * lands on step 2's run.
 *
 * The readings are checked against `demo/world/manifest.json`, the world's own
 * statement of which item was refused at which head and which held-out tests
 * failed, so a world regenerated under the tour fails here instead of the tour
 * pointing at a different story than its words tell.
 *
 * A demo module (`demo-*.mjs`): `scripts/ui-check.mjs` runs it only against the
 * static server for `build-demo/`. Every read happens inside `check()`:
 * `ui-check.test.ts` loads every module with `visit` set to throw.
 */
// ui-check-owns: demo/tours/refused-pr.json
import { readFileSync } from 'node:fs';
import {
  CLEAR_SIZES,
  FRONTEND,
  TOUR_POPOVER,
  TOUR_SIZES,
  docsPassage,
  onScreen,
  popoverCoversNothing,
  showing,
  sizeName,
  squash,
  stageCaption
} from '../ui-check-lib.mjs';

const TOUR = new URL('demo/tours/refused-pr.json', FRONTEND);
const MANIFEST = new URL('demo/world/manifest.json', FRONTEND);
const MORE = 'summary[aria-label="More destinations"]';

const read = (path) => JSON.parse(readFileSync(new URL(path, FRONTEND), 'utf8'));
const text = async (locator) => squash((await locator.textContent()) ?? '');

/** The verdicts the review steps show, in tour order: the send-back, then the fix's. */
const VERDICTS = [
  { word: 'NEEDS_CHANGES', round: 1 },
  { word: 'READY', round: 2 }
];

/**
 * The reviewed head the run page shows beside the verdict, as drawn ("ad44adf884db…":
 * the page cuts a sha to 12 characters and an ellipsis) and as its hex prefix.
 */
async function reviewedHead(page) {
  const drawn = await page.evaluate(() => {
    const dt = [...document.querySelectorAll('main dt')].find((el) => el.textContent?.trim() === 'reviewed head');
    return (dt?.nextElementSibling?.textContent ?? '').trim();
  });
  return { drawn, hex: /^[0-9a-f]+/.exec(drawn)?.[0] ?? '' };
}

/**
 * Whether two abbreviations of a sha name one commit: each at least 8 hex
 * characters, the shorter a prefix of the longer. The page shows 12, the
 * manifest's `refused_head` holds 8.
 */
const sameHead = (a, b) =>
  a.length >= 8 && b.length >= 8 && (a.length <= b.length ? b.startsWith(a) : a.startsWith(b));

/** Fail unless the address is the Work page on the refused item. */
function onRefusedItem(page, expect, world, at) {
  const url = new URL(page.url());
  expect(
    url.pathname === '/work' && url.searchParams.get('item') === world.refused_item,
    `${at}: the page is ${url.pathname}${url.search}, not Work on item ${world.refused_item}`
  );
}

/**
 * Fail unless `step` (its target highlighted) shows the reading the world says
 * it should, on the screen it highlights. `review` is the step's place among
 * the tour's review steps.
 */
async function readsRefusal(page, expect, step, ctx, review, at) {
  const { world } = ctx;
  const target = page.locator(`[data-tour="${step.target}"]`);
  if (step.target === 'review-verdict') {
    const want = VERDICTS[review];
    const word = await text(target.locator('[data-status-word]'));
    expect(word === want.word, `${at}: the verdict reads "${word}", not ${want.word}`);
    expect(
      (await text(target)).includes(`round ${want.round}`),
      `${at}: the verdict does not name round ${want.round}`
    );
    // A review run's own round is attested by that run, never "the decision
    // standing over this work item".
    const note = page.locator('[data-tour="review-verdict"] + p');
    expect(await note.isVisible(), `${at}: no scope note under the verdict`);
    const said = await text(note);
    expect(said === 'attested by this run', `${at}: the scope note reads "${said}"`);
    // The run page agrees with the tour: a run whose round the tour reads is
    // not "publication blocked", and nothing on it waits on a seal.
    const runPage = squash((await page.locator('main').first().innerText()) ?? '');
    for (const words of ['publication blocked', 'nothing is sealed']) {
      expect(!runPage.includes(words), `${at}: the run page says "${words}"`);
    }
    const outcome = page.locator('[data-run-outcome]');
    const outcomes = await outcome.count();
    expect(outcomes === 0, `${at}: the run header reads "${outcomes === 0 ? '' : await text(outcome.first())}"`);
    const safest = squash((await page.locator('section[aria-label="Safest action"]').innerText()) ?? '');
    expect(!/blocked by/.test(safest), `${at}: the safest action reads "${safest}"`);
    expect(
      safest.includes('Nothing to do on this run: a review run publishes nothing. Its verdict is below.'),
      `${at}: the safest action reads "${safest}", not a review run's`
    );
    const head = await reviewedHead(page);
    expect(head.hex.length >= 8, `${at}: the reviewed head reads "${head.drawn}", not a sha`);
    if (want.round === 2) {
      expect(
        sameHead(head.hex, world.refused_head),
        `${at}: round 2 reviewed "${head.drawn}", the verify failed on ${world.refused_head}`
      );
    } else {
      expect(
        !sameHead(head.hex, world.refused_head),
        `${at}: round 1 reviewed "${head.drawn}", which should be the head before the fix, not ${world.refused_head}`
      );
    }
  } else if (step.target === 'merge-verify') {
    onRefusedItem(page, expect, world, at);
    // A failed verify whose merge is a person's move is marked as one
    // (`merge-verify.ts`: `needs_you` when a pipeline cell needs you).
    const status = await target.getAttribute('data-merge-verify');
    expect(status === 'needs_you', `${at}: the merge verify is marked "${status}", not a failure that needs you`);
    const sentence = await text(target);
    expect(
      sentence.startsWith(`Merge verify failed at ${world.refused_head}`),
      `${at}: the reading is "${sentence}", not a failed verify at ${world.refused_head}`
    );
    const head = await text(target.locator('[data-merge-verify-head]'));
    expect(head === world.refused_head, `${at}: the head in mono reads "${head}"`);
    // The verify held the merge and the READY stands: the merge is a person's.
    const merge = await stageCaption(page, 'merge');
    expect(merge === 'needs you · merge decision', `${at}: the merge cell reads "${merge}"`);
  } else if (step.target === 'held-out-tests') {
    onRefusedItem(page, expect, world, at);
    const ids = (await target.locator('[data-held-out-test]').allTextContents()).map(squash);
    expect(
      JSON.stringify([...ids].sort()) === JSON.stringify([...world.held_out_failed].sort()),
      `${at}: the held-out ids are ${JSON.stringify(ids)}, the world failed ${JSON.stringify(world.held_out_failed)}`
    );
    const excused = await target
      .locator('li')
      .evaluateAll((items) =>
        items
          .filter((li) => li.querySelector('[data-held-out-excused]'))
          .map((li) => (li.querySelector('[data-held-out-test]')?.textContent ?? '').replace(/\s+/g, ' ').trim())
      );
    expect(
      JSON.stringify(excused.sort()) === JSON.stringify([...world.held_out_declared].sort()),
      `${at}: the excused ids are ${JSON.stringify(excused)}, the PR declared ${JSON.stringify(world.held_out_declared)}`
    );
  }
}

export async function checks({ check, visit, expect }) {
  for (const size of TOUR_SIZES) {
    await check(
      `demo tours: "Why was this PR refused?" runs start to end at ${sizeName(size)}, each refusal step showing its reading under its docs passage`,
      async () => {
        const tour = JSON.parse(readFileSync(TOUR, 'utf8'));
        const world = JSON.parse(readFileSync(MANIFEST, 'utf8'));
        const ctx = { world };
        const targets = tour.steps.map((step) => step.target);
        for (const want of ['review-verdict', 'merge-verify', 'held-out-tests']) {
          expect(targets.includes(want), `the tour has no step on [data-tour="${want}"]`);
        }
        // The tour ends on its own point, the held-out tests, never on the
        // Build history foot while its figures are samples (c9, V3).
        expect(targets.at(-1) === 'held-out-tests', `the tour ends on ${targets.at(-1)}, not the held-out tests`);
        const clear = CLEAR_SIZES.some((s) => s.width === size.width && s.height === size.height);
        const reviews = tour.steps.filter((step) => step.target === 'review-verdict');
        expect(
          reviews.length === VERDICTS.length,
          `the tour has ${reviews.length} review steps, not ${VERDICTS.length}`
        );
        for (const step of reviews) {
          // A review step's run is one of the refused item's own runs.
          const run = read(`demo/world/runs-${step.route.split('/').at(-1)}.json`);
          expect(
            run.item_id === world.refused_item,
            `${step.passage} is on a run of item ${run.item_id}, not the refused ${world.refused_item}`
          );
        }
        await visit(
          tour.start,
          async (page, problems) => {
            await page.locator(MORE).click();
            const start = page.locator(`[data-tour-start="${tour.id}"]`);
            await start.waitFor({ state: 'visible', timeout: 5_000 });
            await start.click();
            const popover = page.locator(TOUR_POPOVER);
            for (const [index, step] of tour.steps.entries()) {
              const n = index + 1;
              const at = `step ${n} at ${sizeName(size)}`;
              await showing(page, step.passage, step.target);
              const path = new URL(page.url()).pathname;
              expect(path === step.route, `${at} is on ${step.route}, the tour is on ${path}`);
              await onScreen(page, expect, `[data-tour="${step.target}"]`, at);
              const frozen = await page.locator('.driver-active-element-parent-no-scroll').count();
              expect(frozen === 0, `${at}: the highlight stopped the scrolling of its target's parent`);
              const want = docsPassage(step.docs, step.passage);
              const title = squash((await popover.locator('.driver-popover-title').textContent()) ?? '');
              expect(title === want.title, `${at}'s title is "${title}", the docs say "${want.title}"`);
              const body = (await popover.locator('.driver-popover-description p').allTextContents()).map(squash);
              expect(
                JSON.stringify(body) === JSON.stringify(want.body),
                `${at} says ${JSON.stringify(body)}\nthe docs say ${JSON.stringify(want.body)}`
              );
              await readsRefusal(page, expect, step, ctx, reviews.indexOf(step), at);
              if (clear) await popoverCoversNothing(page, expect, step, at);
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

  for (const size of TOUR_SIZES.slice(1)) {
    await check(
      `demo tours: at ${sizeName(size)} a deep link to "Why was this PR refused?" steps 3 and 4 lands on the refused item, and Back from step 3 on step 2's run`,
      async () => {
        const tour = JSON.parse(readFileSync(TOUR, 'utf8'));
        const world = JSON.parse(readFileSync(MANIFEST, 'utf8'));
        const verify = tour.steps.findIndex((step) => step.target === 'merge-verify');
        const held = tour.steps.findIndex((step) => step.target === 'held-out-tests');
        const before = tour.steps[verify - 1];
        expect(before?.target === 'review-verdict', 'the step before the merge verify is no longer the READY verdict');
        // From Work on no item: the step's own query selects the refused one.
        await visit(
          `/work?tour=${tour.id}&step=${verify + 1}`,
          async (page, problems) => {
            const at = `a deep link to step ${verify + 1} at ${sizeName(size)}`;
            await showing(page, tour.steps[verify].passage, 'merge-verify');
            onRefusedItem(page, expect, world, at);
            await onScreen(page, expect, '[data-tour="merge-verify"]', at);
            await page.locator(TOUR_POPOVER).locator('.driver-popover-prev-btn').click();
            await showing(page, before.passage, before.target);
            const back = `Back from step ${verify + 1} at ${sizeName(size)}`;
            const path = new URL(page.url()).pathname;
            expect(path === before.route, `${back} is on ${path}, not ${before.route}`);
            const word = await text(page.locator('[data-tour="review-verdict"] [data-status-word]'));
            expect(word === 'READY', `${back}: the verdict reads "${word}"`);
            expect(problems.length === 0, `the tour reported:\n${problems.join('\n')}`);
          },
          size
        );
        // From Work on another item: the step moves the page to the refused one.
        const other = world.awaiting_merge_item;
        await visit(
          `/work?item=${other}&tour=${tour.id}&step=${held + 1}`,
          async (page, problems) => {
            const at = `a deep link to step ${held + 1} from item ${other} at ${sizeName(size)}`;
            await showing(page, tour.steps[held].passage, 'held-out-tests');
            onRefusedItem(page, expect, world, at);
            await onScreen(page, expect, '[data-tour="held-out-tests"]', at);
            expect(problems.length === 0, `the tour reported:\n${problems.join('\n')}`);
          },
          size
        );
      }
    );
  }
}
