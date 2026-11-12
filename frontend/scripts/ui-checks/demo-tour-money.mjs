/**
 * The "Where did the money go?" tour (#3149), run start to end at the three
 * desktop sizes tours run at (1440x960, and the short laptops 1366x650 and
 * 1024x768): every step shows on its route, highlights a target wholly on
 * screen (inside the window and every panel that scrolls or clips it, and
 * clear of the fade at the foot of a popover body with more below), says
 * exactly the words of its docs passage, and says the figure is list price.
 *
 * On the way it reads the figures the steps point at, off the page, and fails
 * when they do not add up: the tour's run is the node's run that links to its
 * run page, at the cost the run page shows; the node's listed runs sum to its
 * popover's lifetime line, which is its card's drain figure; and the cards'
 * drain figures sum to the epic's on the Epics list. Lifetime and drain are
 * two figures in kew (`docs/web/epics.md`); they are one here only because
 * every node of the demo world has its lifetime equal to its drain (pinned in
 * `src/lib/demo/world.test.ts`), so this check also reads that off the node's
 * file. The Budget page's today and this week are the world's (kew's UTC
 * calendar day and week, by when a run started), and the week holds the epic.
 * The tour ends on the epic's drain, the sum its node cards add up to.
 *
 * At 1440x960 and 1366x650 the tour popover covers neither a step's target
 * nor the node popover the node step's target sits in (design review c9, D9).
 *
 * At the short laptops, Back from step 3 lands on step 2 with `?node=` and its
 * popover open again, and a deep link to step 2 opens that node's popover.
 *
 * The passages are read here straight from the markdown, not through
 * `demo/tour-bundle.ts`, so a bundler that drops or rewrites a word fails this
 * check instead of agreeing with itself.
 *
 * A demo module (`demo-*.mjs`): `scripts/ui-check.mjs` runs it only against the
 * static server for `build-demo/`. Every read happens inside `check()`:
 * `ui-check.test.ts` loads every module with `visit` set to throw.
 */
// ui-check-owns: demo/tours/where-did-the-money-go.json
import { readFileSync } from 'node:fs';
import {
  CLEAR_SIZES,
  FRONTEND,
  cardReadable,
  TOUR_POPOVER,
  TOUR_SIZES,
  clearOfFade,
  docsPassage,
  onScreen,
  popoverCoversNothing,
  showing,
  sizeName,
  squash
} from '../ui-check-lib.mjs';

const TOUR = new URL('demo/tours/where-did-the-money-go.json', FRONTEND);
const MANIFEST = new URL('demo/world/manifest.json', FRONTEND);
const MORE = 'summary[aria-label="More destinations"]';

const MONEY = String.raw`\$(\d[\d,]*(?:\.\d{2})?)`;
const cents = (figure) => Math.round(Number(figure.replace(/,/g, '')) * 100);
const usdCents = (usd) => Math.round(usd * 100);
/** Every dollar figure in `text`, in cents, in order: `$3.16`, or a whole `$80`. */
const dollars = (text) => [...text.matchAll(new RegExp(MONEY, 'g'))].map((m) => cents(m[1]));
/** Every "<spent> / <cap>" pair in `text`, in cents. */
const againstCap = (text) =>
  [...text.matchAll(new RegExp(`${MONEY}\\s*/\\s*${MONEY}`, 'g'))].map((m) => ({ spent: cents(m[1]), cap: cents(m[2]) }));
const shownUsd = (c) => `$${(c / 100).toFixed(2)}`;

/** The tour, the world and the node step's node, read and checked before any page opens. */
function setUp(expect) {
  const tour = JSON.parse(readFileSync(TOUR, 'utf8'));
  const world = JSON.parse(readFileSync(MANIFEST, 'utf8'));
  const nodeIndex = tour.steps.findIndex((step) => step.target === 'node-runs');
  const nodeStep = tour.steps[nodeIndex];
  expect(nodeStep?.route === `/epics/${world.epic_ref}`, `the node step is on ${nodeStep?.route}, not the world's epic ${world.epic_ref}`);
  const node = nodeStep.query?.node;
  expect(typeof node === 'string', 'the node step names no ?node=');
  const runId = /^\/runs\/([^/]+)$/.exec(tour.start)?.[1];
  expect(runId !== undefined, `the tour starts on ${tour.start}, not a run page`);
  const nodeFile = JSON.parse(readFileSync(new URL(`demo/world/epics-${world.epic_ref}-nodes-${node}.json`, FRONTEND), 'utf8'));
  expect(nodeFile.runs.some((r) => r.run_id === runId), `the tour's run ${runId} is not one of node ${node}'s runs in the demo world`);
  // The popover's summary is the node's LIFETIME, its card the DRAIN: the
  // tour's run → node → epic sum holds only because the world has them equal.
  expect(
    usdCents(nodeFile.lifetime_usd) === usdCents(nodeFile.drain_usd),
    `node ${node}'s lifetime ${nodeFile.lifetime_usd} is not its drain ${nodeFile.drain_usd}: ` +
      `the popover's lifetime line would not be the card's drain figure, and the tour's sum would not hold`
  );
  return { tour, world, node, nodeIndex, nodeStep, runId };
}

/** Fail unless the address is the node step's: the epic page with `?node=<node>`. */
function onNode(page, expect, ctx, at) {
  const url = new URL(page.url());
  expect(
    url.pathname === ctx.nodeStep.route && url.searchParams.get('node') === ctx.node,
    `${at}: the page is ${url.pathname}${url.search}, not ${ctx.nodeStep.route}?node=${ctx.node}`
  );
}

/**
 * The boxes of the tour popover and of each "<spent> / <cap>" figure in the
 * Budget page's spend section (the innermost element holding one, in page
 * order: today's, then this week's), with which figures the popover overlaps.
 */
function figureBoxes(sel) {
  const pop = document.querySelector(sel)?.getBoundingClientRect();
  const section = document.querySelector('[data-tour="budget-spend"]');
  const pair = /^\s*\$[\d,]+(?:\.\d{2})?\s*\/\s*\$[\d,]+(?:\.\d{2})?\s*$/;
  const figures = [...(section?.querySelectorAll('*') ?? [])].filter(
    (el) => pair.test(el.textContent ?? '') && ![...el.children].some((c) => pair.test(c.textContent ?? ''))
  );
  const box = (r) => [r.left, r.top, r.right, r.bottom].map(Math.round);
  const overlaps = (a, b) => !(a.bottom <= b.top || a.top >= b.bottom || a.right <= b.left || a.left >= b.right);
  const drawn = figures.map((el) => el.getBoundingClientRect());
  return {
    pop: pop ? box(pop) : null,
    figures: drawn.map(box),
    covered: pop ? drawn.map((r, i) => (overlaps(pop, r) ? i : -1)).filter((i) => i >= 0) : []
  };
}

/** Fail unless the tour popover overlaps neither Budget figure, today's or this week's. */
async function figuresClear(page, expect, at) {
  // A step re-places on a frame after its target settles, so the answer is
  // polled for up to 5 s, never read once.
  let read = await page.evaluate(figureBoxes, TOUR_POPOVER);
  for (const until = Date.now() + 5_000; Date.now() < until; ) {
    if (read.pop !== null && read.figures.length === 2 && read.covered.length === 0) return;
    await page.waitForTimeout(100);
    read = await page.evaluate(figureBoxes, TOUR_POPOVER);
  }
  const names = ["today's", "this week's"];
  expect(
    read.pop !== null && read.figures.length === 2 && read.covered.length === 0,
    `${at}: the tour popover at [l,t,r,b] ${JSON.stringify(read.pop)} covers ${read.covered.map((i) => names[i]).join(' and ') || 'no'} ` +
      `figure, of ${read.figures.length} found at ${JSON.stringify(read.figures)}`
  );
}

/**
 * What each step's target says about money, read off the page while the step
 * shows: the figures the sums below are checked against.
 */
const READS = {
  /** The run page's cost: its first figure, before planned cost and tokens. */
  'run-cost': async (page) => ({ run: dollars(await page.locator('[data-tour="run-cost"]').innerText())[0] }),
  /** The node's popover: its summary line, each run's href and cost, and every graph card's figure. */
  'node-runs': async (page, ctx) => {
    const history = page.locator('[data-tour="node-runs"]');
    const summary = squash((await history.locator('.hsum').textContent()) ?? '');
    const count = Number(/^(\d+) runs?\b/.exec(summary)?.[1] ?? NaN);
    const runs = await history.locator('.hrun').evaluateAll((els) =>
      els.map((el) => ({ href: el.getAttribute('href') ?? '', cost: el.querySelector('.num')?.textContent ?? '' }))
    );
    const cards = await page.locator('.svelte-flow__node').evaluateAll((els) =>
      els.map((el) => ({ id: el.getAttribute('data-id'), drain: el.querySelector('.card .drain')?.textContent ?? null }))
    );
    const card = cards.find((c) => c.id === ctx.node);
    return {
      summary,
      count,
      total: dollars(summary)[0],
      runs: runs.map((r) => ({ href: new URL(r.href, page.url()).pathname, cost: dollars(r.cost)[0] })),
      card: card?.drain == null ? undefined : dollars(card.drain)[0],
      cards: cards.filter((c) => c.drain !== null).map((c) => dollars(c.drain)[0])
    };
  },
  /** The Epics list row's drain. */
  'epic-drain': async (page) => ({ epic: dollars(await page.locator('[data-tour="epic-drain"]').innerText())[0] }),
  /**
   * The Budget page's two spend cells, each "<spent> / <cap>": today's, then
   * this week's. At every tour size the tour popover must cover neither
   * figure: a passage long enough to fill the popover once hid the week's.
   */
  'budget-spend': async (page, ctx, expect, at) => {
    const budget = againstCap(await page.locator('[data-tour="budget-spend"]').innerText());
    await figuresClear(page, expect, at);
    return { budget };
  }
};

/** Fail unless the figures read along the tour add up, and agree with the world. */
function addsUp(expect, seen, ctx) {
  const { node, runId, world } = ctx;
  expect(Number.isInteger(seen.run), `the run page shows no dollar figure`);
  expect(Number.isInteger(seen.total), `node ${node}'s popover says "${seen.summary}", no total`);
  expect(
    seen.count === seen.runs.length,
    `node ${node} says "${seen.summary}" but lists ${seen.runs.length} runs, so its sum cannot be checked`
  );
  const mine = seen.runs.filter((r) => r.href === `/runs/${runId}`);
  expect(mine.length === 1, `node ${node}'s popover links ${mine.length} runs to /runs/${runId} (${seen.runs.map((r) => r.href).join(', ')})`);
  expect(
    mine[0].cost === seen.run,
    `node ${node}'s popover prices run ${runId} at ${shownUsd(mine[0].cost)}, its run page at ${shownUsd(seen.run)}`
  );
  const runSum = seen.runs.reduce((a, r) => a + r.cost, 0);
  expect(runSum === seen.total, `node ${node}'s runs sum to ${shownUsd(runSum)} but its popover says ${shownUsd(seen.total)}`);
  expect(
    seen.card === seen.total,
    `node ${node}'s graph card reads ${seen.card === undefined ? 'no figure' : shownUsd(seen.card)}, its popover's lifetime ${shownUsd(seen.total)}`
  );
  expect(seen.cards.length > 1, `the graph drew ${seen.cards.length} cards with a figure`);
  const nodeSum = seen.cards.reduce((a, b) => a + b, 0);
  expect(Number.isInteger(seen.epic), 'the Epics list row shows no drain');
  expect(
    nodeSum === seen.epic,
    `the graph's cards sum to ${shownUsd(nodeSum)} (${seen.cards.map(shownUsd).join(' + ')}) but the epic reads ${shownUsd(seen.epic)}`
  );
  expect(seen.epic === usdCents(world.spend.epic_usd), `the epic reads ${shownUsd(seen.epic)}, the world's is ${world.spend.epic_usd}`);
  expect(seen.budget.length === 2, `the Budget page shows ${seen.budget.length} figures against a cap, not today's and this week's`);
  const [day, week] = seen.budget;
  expect(day.spent === usdCents(world.spend.daily_usd), `today's spend reads ${shownUsd(day.spent)}, the world's is ${world.spend.daily_usd}`);
  expect(week.spent === usdCents(world.spend.weekly_usd), `this week's spend reads ${shownUsd(week.spent)}, the world's is ${world.spend.weekly_usd}`);
  expect(week.spent >= seen.epic, `this week's spend ${shownUsd(week.spent)} is less than the epic's ${shownUsd(seen.epic)}`);
  expect(day.spent <= day.cap && week.spent <= week.cap, `today's or this week's spend is past its cap: ${JSON.stringify(seen.budget)}`);
}

export async function checks({ check, visit, expect }) {
  for (const size of TOUR_SIZES) {
    await check(
      `demo tours: "Where did the money go?" runs start to end at ${sizeName(size)}, and its run, node and epic figures add up`,
      async () => {
        const ctx = setUp(expect);
        const { tour } = ctx;
        // The tour ends on its own point, the epic's summed spend, never on
        // the Build history foot while its figures are samples (c9, V3).
        expect(tour.steps.at(-1).target === 'epic-drain', `the tour ends on ${tour.steps.at(-1).target}, not the epic's drain`);
        const clear = CLEAR_SIZES.some((s) => s.width === size.width && s.height === size.height);
        await visit(
          '/work',
          async (page, problems) => {
            await page.locator(MORE).click();
            const start = page.locator(`[data-tour-start="${tour.id}"]`);
            await start.waitFor({ state: 'visible', timeout: 5_000 });
            await start.click();
            const popover = page.locator(TOUR_POPOVER);
            const seen = {};
            for (const [index, step] of tour.steps.entries()) {
              const at = `step ${index + 1} at ${sizeName(size)}`;
              await showing(page, step.passage, step.target);
              const path = new URL(page.url()).pathname;
              expect(path === step.route, `${at} is on ${path}, the tour says ${step.route}`);
              await onScreen(page, expect, `[data-tour="${step.target}"]`, at);
              await clearOfFade(page, expect, `[data-tour="${step.target}"]`, at);
              const frozen = await page.locator('.driver-active-element-parent-no-scroll').count();
              expect(frozen === 0, `${at}: the highlight stopped the scrolling of its target's parent`);
              const want = docsPassage(step.docs, step.passage);
              const title = squash((await popover.locator('.driver-popover-title').textContent()) ?? '');
              expect(title === want.title, `${at}: the title is "${title}", the docs say "${want.title}"`);
              const body = (await popover.locator('.driver-popover-description p').allTextContents()).map(squash);
              expect(
                JSON.stringify(body) === JSON.stringify(want.body),
                `${at} says ${JSON.stringify(body)}\nthe docs say ${JSON.stringify(want.body)}`
              );
              expect(/list price/.test(body.join(' ')), `${at} never says its figure is list price: ${JSON.stringify(body)}`);
              const read = READS[step.target];
              expect(read !== undefined, `${at}: this check does not know how to read [data-tour="${step.target}"]`);
              Object.assign(seen, await read(page, ctx, expect, at));
              if (clear) await popoverCoversNothing(page, expect, step, at);
              // The node the step selects is readable on the graph (c9 review, item 3).
              if (clear && step.query?.node) await cardReadable(page, expect, step.query.node, at);
              await popover.locator('.driver-popover-next-btn').click();
            }
            await popover.waitFor({ state: 'detached', timeout: 5_000 });
            expect((await page.locator('.driver-overlay').count()) === 0, 'the overlay stayed after the last step');
            addsUp(expect, seen, ctx);
            expect(problems.length === 0, `the tour reported:\n${problems.join('\n')}`);
          },
          size
        );
      }
    );
  }

  for (const size of TOUR_SIZES.slice(1)) {
    await check(
      `demo tours: at ${sizeName(size)} Back from "Where did the money go?" step 3 reopens the node's popover, and a deep link to step 2 opens it`,
      async () => {
        const ctx = setUp(expect);
        const { tour, nodeIndex, nodeStep } = ctx;
        const after = tour.steps[nodeIndex + 1];
        expect(after !== undefined, 'the node step is the last step');
        // Back: from the step after the node step, on another page.
        await visit(
          `${after.route}?tour=${tour.id}&step=${nodeIndex + 2}`,
          async (page, problems) => {
            await showing(page, after.passage, after.target);
            await page.locator(TOUR_POPOVER).locator('.driver-popover-prev-btn').click();
            const at = `Back from step ${nodeIndex + 2} at ${sizeName(size)}`;
            await showing(page, nodeStep.passage, nodeStep.target);
            onNode(page, expect, ctx, at);
            await onScreen(page, expect, `[data-tour="${nodeStep.target}"]`, at);
            await clearOfFade(page, expect, `[data-tour="${nodeStep.target}"]`, at);
            const hrefs = await page.locator('[data-tour="node-runs"] .hrun').evaluateAll((els) => els.map((el) => el.getAttribute('href')));
            expect(hrefs.includes(`/runs/${ctx.runId}`), `${at}: the popover lists ${JSON.stringify(hrefs)}, not /runs/${ctx.runId}`);
            expect(problems.length === 0, `the tour reported:\n${problems.join('\n')}`);
          },
          size
        );
        // Deep link: the epic page on no node; the step's own query opens it.
        await visit(
          `${nodeStep.route}?tour=${tour.id}&step=${nodeIndex + 1}`,
          async (page, problems) => {
            const at = `a deep link to step ${nodeIndex + 1} at ${sizeName(size)}`;
            await showing(page, nodeStep.passage, nodeStep.target);
            onNode(page, expect, ctx, at);
            await onScreen(page, expect, `[data-tour="${nodeStep.target}"]`, at);
            await clearOfFade(page, expect, `[data-tour="${nodeStep.target}"]`, at);
            const hrefs = await page.locator('[data-tour="node-runs"] .hrun').evaluateAll((els) => els.map((el) => el.getAttribute('href')));
            expect(hrefs.includes(`/runs/${ctx.runId}`), `${at}: the popover lists ${JSON.stringify(hrefs)}, not /runs/${ctx.runId}`);
            expect(problems.length === 0, `the tour reported:\n${problems.join('\n')}`);
          },
          size
        );
      }
    );
  }
}
