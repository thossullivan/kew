/**
 * The "Follow an epic" tour (#3147): run from the More menu and stepped with
 * Next, each step opens the replay at its own `?mode=replay&t=`, and the graph
 * under the popover is the journal's frame at that `t` — card by card, status
 * word by status word — and shows what the step's passage says it shows. The
 * same holds for a visitor who starts it from the timeline with a selection
 * (each step draws the graph, no card dimmed), and for a deep link into the
 * replay step from a live address, a replay address with no moment, and the
 * step's own moment: each opens on the step's frame, paused (#3167).
 *
 * Design review c9: each step highlights the card its passage names, not the
 * whole graph (D1, D2), selects it, and draws it inside the canvas with its
 * title at 11px or more, no node popover over the graph (review round 1); a
 * step that presses Fit all opens on the whole graph, every card inside the
 * canvas (D3); at 1440x960 and 1366x650 the popover covers no highlighted card
 * (D4), nor the graph a step compares across; steps 3 and 4 are one frame apart, the
 * refused item in review and then back in progress; and the tour ends with
 * Done on the journal's last frame (V3), also run at 1366x650 (X8).
 *
 * A demo module (`demo-*.mjs`): `scripts/ui-check.mjs` runs it only against the
 * static server for `build-demo/`. Every read happens inside `check()`:
 * `ui-check.test.ts` loads every module with `visit` set to throw.
 *
 * The expectations come from the demo world's journal, not from numbers
 * copied here: a regenerated world that no longer has a merge, nodes in flight
 * together or a review round at a step's `t` fails this check, and so does a
 * tour step whose `t` drifts off the moment its words describe.
 */
// ui-check-owns: demo/tours/follow-an-epic.json
// ui-check-owns: src/lib/epic/replay.ts
import { readFileSync } from 'node:fs';
import {
  CLEAR_SIZES,
  FRONTEND,
  cardReadable,
  graphFits,
  onScreen,
  popoverCoversNothing,
  showing,
  sizeName,
  squash,
  stepSelector
} from '../ui-check-lib.mjs';

const TOUR = 'follow-an-epic';
const DESKTOP = { width: 1440, height: 960 };
const SHORT = { width: 1366, height: 650 };
const POPOVER = '.driver-popover.kew-tour';
const MORE = 'summary[aria-label="More destinations"]';

/** The word a card prints for each status the demo journal records. */
const WORD = {
  todo: 'todo',
  in_progress: 'in progress',
  in_review: 'in review',
  done: 'done',
  failed: 'failed',
  blocked: 'blocked'
};

const readJson = (path) => JSON.parse(readFileSync(new URL(path, FRONTEND), 'utf8'));

/** Each frame's `t`, as the page spells it: whole seconds since the first tick. */
function offsets(journal) {
  const first = Date.parse(journal[0].tick_ts);
  return journal.map((tick) => Math.floor((Date.parse(tick.tick_ts) - first) / 1000));
}

/** The frame on screen at `t`: the last one recorded at or before it. */
function frameAt(journal, t) {
  let index = 0;
  offsets(journal).forEach((offset, i) => {
    if (offset <= t) index = i;
  });
  return index;
}

/** A frame's statuses as card words, by node id. */
function wordsOf(frame) {
  return Object.fromEntries(
    frame.snapshot.nodes.map((node) => {
      const word = WORD[node.status];
      if (word === undefined) throw new Error(`the journal records status ${node.status}, which this check has no word for`);
      return [node.id, word];
    })
  );
}

/** Every card the graph draws, by id: its status word and its dollar figure. */
function drawn(page) {
  return page.locator('.dag .svelte-flow__node[data-id]').evaluateAll((cards) =>
    Object.fromEntries(
      cards.map((card) => [
        card.getAttribute('data-id'),
        {
          word: (card.querySelector('[data-status-word]')?.textContent ?? '').trim().toLowerCase(),
          drain: Number((card.querySelector('.drain')?.textContent ?? '').replace(/[^\d.]/g, '') || 'NaN')
        }
      ])
    )
  );
}

/** Nodes a review round sent back: in review on one frame, in progress on the next. */
function sentBack(journal) {
  const back = new Set();
  for (let i = 1; i < journal.length; i++) {
    const before = wordsOf(journal[i - 1]);
    for (const [id, word] of Object.entries(wordsOf(journal[i]))) {
      if (before[id] === 'in review' && word === 'in progress') back.add(id);
    }
  }
  return back;
}


/** The tour, the demo epic's journal, and what each step's passage says the graph shows. */
function readTour(expect) {
  const tour = readJson(`demo/tours/${TOUR}.json`);
  const manifest = readJson('demo/world/manifest.json');
  const ref = manifest.epic_ref;
  const journal = readJson(`demo/world/epics-${ref}-ticks.json`);
  expect(tour.start === `/epics/${ref}`, `the tour starts on ${tour.start}, the demo epic is /epics/${ref}`);

  /** What each step's passage says the graph shows, read off the frame and the cards. */
  const claims = [
    // 1: the named card in progress, the rest todo.
    ({ cards, step }) => {
      const words = Object.values(cards).map((c) => c.word);
      const running = words.filter((w) => w === 'in progress').length;
      const other = words.filter((w) => w !== 'in progress' && w !== 'todo');
      if (running !== 1 || other.length !== 0) return `step 1 draws ${words.join(', ')}`;
      return cards[step.card]?.word === 'in progress' ? null : `step 1 names #${step.card}, which reads ${cards[step.card]?.word}`;
    },
    // 2: the card in progress at step 1 reads done, and the count moved.
    async ({ page, index, prev, frame }) => {
      const first = Object.entries(wordsOf(journal[prev])).find(([, w]) => w === 'in progress')?.[0];
      if (wordsOf(frame)[first] !== 'done') return `node ${first} is not done at step 2`;
      if (index - prev !== 2) return `step 2 is ${index - prev} frames after step 1; its passage says two`;
      const done = Object.values(wordsOf(frame)).filter((w) => w === 'done').length;
      // Read without spaces: the page draws `1<span> / 13</span>`, and Svelte
      // trims the space that opens the span, so the text reads "1/ 13".
      const count = ((await page.locator('[data-tour="epic-progress"]').textContent()) ?? '').replace(/\s+/g, '');
      return count.startsWith(`${done}/${frame.snapshot.nodes.length}done`) ? null : `the count reads "${count}"`;
    },
    // 3: the named card reads in review: the refused item, before its send-back.
    ({ cards, step }) => {
      if (step.card !== manifest.refused_item) return `step 3 names #${step.card}, not the refused item ${manifest.refused_item}`;
      return cards[step.card]?.word === 'in review' ? null : `step 3 names #${step.card}, which reads ${cards[step.card]?.word}`;
    },
    // 4: one frame later the same card reads in progress again: the world's
    // send-back, the refused item's first review round.
    ({ index, prev, cards, step }) => {
      if (index - prev !== 1) return `step 4 is ${index - prev} frames after step 3; its passage says one`;
      if (step.card !== claimed[2]) return `step 4 names #${step.card}, step 3 named #${claimed[2]}`;
      const before = wordsOf(journal[index - 1])[step.card];
      const now = cards[step.card]?.word;
      return before === 'in review' && now === 'in progress'
        ? null
        : `#${step.card} reads ${now}, and ${before} on the frame before: no send-back`;
    },
    // 5: on the last frame each node card's dollar figure is the frame's spend
    // for that node, and the named card reads the most and went back through review.
    ({ index, frame, cards, step }) => {
      if (index !== journal.length - 1) return `step 5 is frame ${index + 1}, not the journal's last`;
      const spent = frame.economics?.node_drain_usd ?? {};
      const wrong = [];
      const priced = [];
      for (const [id, card] of Object.entries(cards)) {
        // A done group's figure is its members' total, not one node's.
        if (id.startsWith('done:')) continue;
        const want = spent[id];
        if (want === undefined) {
          if (Number.isFinite(card.drain)) wrong.push(`${id} reads $${card.drain}, the frame records no spend for it`);
        } else if (!Number.isFinite(card.drain) || Math.abs(card.drain - want) > 0.005) {
          wrong.push(`${id} reads ${Number.isFinite(card.drain) ? `$${card.drain}` : 'no figure'}, the frame has $${want}`);
        } else {
          priced.push([id, card.drain]);
        }
      }
      if (wrong.length > 0) return wrong.join('; ');
      if (priced.length === 0) return 'no node card on the last frame draws a dollar figure';
      priced.sort(([, a], [, b]) => b - a);
      const [[top, most], next] = priced;
      if (next !== undefined && next[1] >= most) return `${top} and ${next[0]} both read the most, $${most}`;
      if (top !== step.card) return `step 5 names #${step.card}, but ${top} reads the most`;
      return sentBack(journal).has(top) ? null : `the card that reads the most, ${top}, never went back through review`;
    }
  ];
  /** The card each step names, by step (0-based). */
  const claimed = tour.steps.map((step) => step.card);
  expect(claims.length === tour.steps.length, `${tour.steps.length} steps, ${claims.length} claims: write the new step's claim here`);
  // The tour ends on the replay's last frame, never on the Build history foot
  // while its figures are samples (design review c9, V3).
  const last = tour.steps.at(-1);
  expect(Number(last.query?.t) === offsets(journal).at(-1), `the tour's last step opens t=${last.query?.t}, not the journal's last frame`);
  expect(tour.steps.every((step) => step.target !== 'build-history'), 'a step points at the Build history foot');
  return { tour, manifest, journal, claims, lastT: offsets(journal).at(-1) };
}

/** Wait for step `n`'s popover. */
async function waitForPopover(page, n) {
  await page.waitForFunction(
    ([sel, id]) => document.querySelector(sel)?.getAttribute('data-tour-step') === id,
    [POPOVER, `${TOUR}/${n}`],
    { timeout: 15_000 }
  );
}

/**
 * Step `i` (0-based) is on screen as its passage reads it: the scrubber on the
 * frame its `t` names, its target highlighted, the address at its moment with
 * no `view` or `select`, the replay paused, the graph drawn with no card
 * dimmed, every card's status word the journal's, and its passage's claim
 * true of the frame. Returns the frame index.
 */
async function showsStep(page, { expect, sleep }, read, i, prev, size = DESKTOP) {
  const { tour, journal, claims, lastT } = read;
  const step = tour.steps[i];
  const n = i + 1;
  const t = Number(step.query?.t);
  expect(step.query?.mode === 'replay' && Number.isInteger(t), `step ${n} opens no replay moment`);
  expect(t <= lastT, `step ${n}'s t=${t} is past the journal's last moment, ${lastT}`);
  const index = frameAt(journal, t);
  // The step shows on its moment: the scrubber holds the frame its `t` names.
  const scrubberAt = (want) =>
    page.waitForFunction(
      (frame) => document.querySelector('input[aria-label="Replay frame"]')?.value === String(frame),
      want,
      { timeout: 15_000 }
    );
  await scrubberAt(index);
  // The step highlights what its passage names: the card, when it names one.
  await page.waitForFunction(
    (sel) => document.querySelector(sel)?.classList.contains('driver-active-element'),
    stepSelector(step),
    { timeout: 10_000 }
  );
  const at = `step ${n} at ${sizeName(size)}`;
  if (step.fit) await graphFits(page, expect, at);
  // A named card is readable, and it is the one selected: the rail and the
  // canvas speak of the node the step names (c9 review, items 3 and 5).
  const named = step.card ?? step.query?.node;
  if (named !== undefined) {
    await cardReadable(page, expect, named, at);
    // A selected card draws as `.card.sel` (`DagNode.svelte`).
    const pressed = await page
      .locator('[data-tour="epic-graph"] .svelte-flow__node[data-id] .card.sel')
      .evaluateAll((els) => els.map((el) => el.closest('.svelte-flow__node')?.getAttribute('data-id')));
    expect(JSON.stringify(pressed) === JSON.stringify([named]), `${at}: the graph has ${JSON.stringify(pressed)} selected, the step names #${named}`);
    expect((await page.locator('[role="dialog"].pop').count()) === 0, `${at}: the node popover of #${named} stays open over the graph`);
  }
  await onScreen(page, expect, stepSelector(step), at);
  if (CLEAR_SIZES.some((s) => s.width === size.width && s.height === size.height)) {
    await popoverCoversNothing(page, expect, step, at);
  }
  const here = new URL(await page.evaluate(() => location.href));
  expect(
    here.searchParams.get('mode') === 'replay' && here.searchParams.get('t') === String(t),
    `step ${n} left the address at ${here.search}`
  );
  // The passages read the graph: a visitor's timeline or selection is not kept.
  expect(
    !here.searchParams.has('view') && !here.searchParams.has('select'),
    `step ${n} kept the visitor's view or selection: ${here.search}`
  );
  // A step is one moment: paused, and still on its frame a moment later.
  expect((await page.getByRole('button', { name: '⏸ pause' }).count()) === 0, `step ${n}'s replay is playing; a step is one moment`);
  await sleep(1_200);
  await scrubberAt(index);

  const frame = journal[index];
  const want = wordsOf(frame);
  const cards = await drawn(page);
  expect(Object.keys(cards).length > 0, `step ${n} drew no graph card, so it proves nothing`);
  const dimmed = await page.locator('.dag .svelte-flow__node[data-id] .card.dim').count();
  expect(dimmed === 0, `step ${n} dims ${dimmed} cards; its passage reads every card`);
  const wrong = [];
  for (const [id, card] of Object.entries(cards)) {
    if (id.startsWith('done:')) continue;
    if (card.word !== want[id]) wrong.push(`${id} reads ${card.word}, the journal has ${want[id]}`);
  }
  // A node with no card of its own is folded into a done group.
  for (const [id, word] of Object.entries(want)) {
    if (!(id in cards) && word !== 'done') wrong.push(`${id} (${word}) has no card`);
  }
  expect(wrong.length === 0, `step ${n} at t=${t}:\n${wrong.join('\n')}`);
  const said = await claims[i]({ page, index, prev, frame, cards, step });
  expect(said === null, `step ${n}'s passage does not hold at t=${t}: ${said}`);
  return index;
}

/**
 * Run the tour from the More menu on `address`, step by step with Next, to
 * Done on the journal's last frame, which ends it and takes the link out.
 */
async function walk(tools, read, address, size = DESKTOP) {
  const { visit, expect } = tools;
  await visit(
    address,
    async (page, problems) => {
      await page.locator(MORE).click();
      await page.locator(`[data-tour-start="${TOUR}"]`).click();
      let prev = 0;
      for (const i of read.tour.steps.keys()) {
        if (i > 0) await page.locator(`${POPOVER} .driver-popover-next-btn`).click();
        await waitForPopover(page, i + 1);
        prev = await showsStep(page, tools, read, i, prev, size);
      }
      const done = page.locator(`${POPOVER} .driver-popover-next-btn`);
      expect(squash((await done.textContent()) ?? '') === 'Done', 'the last step offers no Done');
      await done.click();
      await page.locator(POPOVER).waitFor({ state: 'detached', timeout: 5_000 });
      const left = new URL(await page.evaluate(() => location.href));
      expect(!left.searchParams.has('tour'), `Done left ${left.search}`);
      expect(problems.length === 0, `the tour reported:\n${problems.join('\n')}`);
    },
    size
  );
}

export async function checks(tools) {
  const { check, visit, expect } = tools;

  await check('demo tours: Follow an epic, run with Next, draws the journal frame each step names', async () => {
    const read = readTour(expect);
    await walk(tools, read, read.tour.start);
  });

  await check("demo tours: Follow an epic started on the timeline with a selection draws every step's graph, nothing dimmed", async () => {
    const read = readTour(expect);
    // The address a visitor leaves after reading the refused node on the timeline.
    await walk(tools, read, `${read.tour.start}?view=timeline&select=${read.manifest.refused_item}`);
  });

  // The short laptop (design review c9, D4 and X8): every step fits the whole
  // graph, highlights its card and keeps its popover off it, and the tour
  // ends with Done on the journal's last frame.
  await check(
    'demo tours: Follow an epic at 1366x650 fits the graph, highlights the card each step names, and keeps the popover off it to Done',
    async () => {
      const read = readTour(expect);
      await walk(tools, read, read.tour.start, SHORT);
    }
  );

  // `?node=` names the selection, which a close keeps: a node popover shut
  // with × stays shut through the tour's next steps, which navigate to the
  // same address with a new moment (#3149).
  await check('demo tours: Follow an epic keeps a node popover shut with × shut through Next, step after step', async () => {
    const read = readTour(expect);
    const first = read.tour.steps[0];
    await visit(
      // Step 1's own moment, so starting the tour there navigates nowhere.
      `${read.tour.start}?mode=replay&t=${first.query.t}`,
      async (page, problems) => {
        // A step's card is there to be read, not pressed (the controller
        // turns its interaction off), so the node is picked and its popover
        // shut with × before the tour starts, which keeps `?node=`.
        const card = page.locator(`.dag .svelte-flow__node[data-id="${first.card}"]`);
        await card.waitFor({ state: 'visible', timeout: 15_000 });
        const id = await card.getAttribute('data-id');
        await card.click();
        const pop = page.locator('[role="dialog"].pop');
        await pop.waitFor({ state: 'visible', timeout: 5_000 });
        await pop.locator('button.close').click();
        await pop.waitFor({ state: 'detached', timeout: 5_000 });
        await page.locator(MORE).click();
        await page.locator(`[data-tour-start="${TOUR}"]`).click();
        await showing(page, first.passage, first);
        expect((await pop.count()) === 0, `step 1: node ${id}'s popover, shut with ×, is open again`);
        for (const n of [2, 3]) {
          await page.locator(POPOVER).locator('.driver-popover-next-btn').click();
          const step = read.tour.steps[n - 1];
          await showing(page, step.passage, step);
          const here = new URL(page.url());
          // A step that names its own node selects it (c9 review, item 5);
          // the visitor's shut popover stays shut either way.
          const want = step.query?.node ?? id;
          expect(here.searchParams.get('node') === want, `step ${n}: the address ${here.search} does not name node ${want}`);
          expect((await pop.count()) === 0, `step ${n}: a node popover, shut with ×, is open again on ${here.search}`);
        }
        expect(problems.length === 0, `the tour reported:\n${problems.join('\n')}`);
      },
      DESKTOP
    );
  });

  await check('demo tours: a deep link to the replay step opens its frame, paused, by any of three addresses', async () => {
    const read = readTour(expect);
    const i = read.tour.steps.findIndex((step) => step.passage === `${TOUR}/3`);
    const step = read.tour.steps[i];
    expect(step?.query?.mode === 'replay' && step.query.t !== undefined, `${TOUR}/3 is not a replay step`);
    const link = `tour=${TOUR}&step=${i + 1}`;
    // A live address, a replay address with no moment (the journal still
    // loading, then playing from frame 0, when the tour's own navigation
    // lands), and the step's own moment.
    for (const query of [link, `mode=replay&${link}`, `mode=replay&t=${step.query.t}&${link}`]) {
      await visit(
        `${read.tour.start}?${query}`,
        async (page, problems) => {
          await waitForPopover(page, i + 1);
          await showsStep(page, tools, read, i, frameAt(read.journal, Number(read.tour.steps[i - 1].query.t)));
          const here = new URL(page.url());
          expect(
            here.searchParams.get('tour') === TOUR && here.searchParams.get('step') === String(i + 1),
            `?${query} left the address at ${here.search}`
          );
          expect(problems.length === 0, `?${query} reported:\n${problems.join('\n')}`);
        },
        DESKTOP
      );
    }
  });
}
