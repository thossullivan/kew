/**
 * No status word is cut at 390px, on any factory route (#2898, design-c7 C6).
 *
 * #2848 made StatusMark ellipsise every status word everywhere, which keeps a
 * word too long for its cell out of its neighbour — and also lets a narrow DAG
 * card, rail or Health cell show "needs y…" with only a hover title to say
 * what it was. Only Work was read at 390 (`work-titles.mjs`). This sweep reads
 * every status word on screen (`data-status-word`: StatusMark's word, the DAG
 * card's, the node popover's and the epic legend's) on every page under
 * `src/routes/(factory)` and fails on one whose text is wider than its box.
 * A page whose words need a query is visited again with it (`QUERIED`):
 * `/prompts?role=adversarial_review` draws the reviewer's verdict words.
 *
 * #2951: and on one that wraps (#3050 N9) — at 390 the Work block header's
 * `NEEDS YOU` broke over two lines, which no box measurement sees. The
 * popover's run verdicts carry the hook too, and
 * `src/lib/ui/status-word-hook.test.ts` fails on a component that draws a
 * status as text without it, so a renderer cannot leave the sweep unread.
 * The one exception to "one line": a popover run verdict can be a sentence and
 * a phone has no hover, so it wraps inside its row and is never cut.
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 */
// ui-check-owns: src/lib/ui/StatusMark.svelte
import { readdirSync } from 'node:fs';
import { EPIC, FRONTEND, SETTLE_MS } from '../ui-check-lib.mjs';

const PHONE = { width: 390, height: 844 };
const FACTORY = new URL('src/routes/(factory)/', FRONTEND);

/** What a dynamic segment is visited as: every value the fixtures serve. */
const PARAMS = {
  ref: ['launch-fixes', 'go-live-2026-09', 'sweep-2026-10'],
  id: ['f5dcb6a5']
};

/** The routes whose fixture reading draws at least one status word at 390. */
const WORDED = [
  '/audit',
  '/epics/launch-fixes',
  '/epics/go-live-2026-09',
  '/health',
  '/monitor',
  '/prompts?role=adversarial_review',
  '/runs',
  '/runs/f5dcb6a5',
  '/work'
];

/** A state word no table knows, served on Work and the epic: `stateWord` and
 *  `statusWord` echo it. */
const LONG_STATE = 'awaiting_external_approval';
const LONG_WORD = 'awaiting external approval';

/** A served state no phone line holds whole — not the node popover's sheet
 *  (13px mono), not the epic legend's row (11px) — for the controls on the
 *  two renderers that give a word the full width. */
const SPRAWLING_STATE = 'awaiting_external_approval_from_the_security_review_board_and_the_legal_team';
const SPRAWLING_WORD = SPRAWLING_STATE.replace(/_/g, ' ');

/** A served run status that is a sentence: the popover's run verdict (#2951). */
const SENTENCE_VERDICT = 'review round stalled waiting on a reviewer verdict for the head it names '.repeat(3).trim();

/** The Work item whose needs-you block the fixtures draw (`needs-you.mjs`). */
const BLOCK_ITEM = '2055';

/** Every `+page.svelte` under `dir`, as the route path it serves. Route groups
 *  (`(name)`) add no segment. */
function pages(dir = FACTORY, prefix = '') {
  const found = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    if (entry.isFile() && entry.name === '+page.svelte') found.push(prefix || '/');
    if (!entry.isDirectory()) continue;
    const segment = /^\(.*\)$/.test(entry.name) ? '' : `/${entry.name}`;
    found.push(...pages(new URL(`${entry.name}/`, dir), prefix + segment));
  }
  return found;
}

/** Each page as the URLs that visit it: a `[param]` becomes every fixture value
 *  `PARAMS` names. A new dynamic route with no entry there throws, so a route
 *  is never left out of the sweep by having no URL. */
export function factoryRoutes() {
  return pages()
    .sort()
    .flatMap((route) => {
      const param = /\[(\w+)\]/.exec(route);
      if (!param) return [route];
      const values = PARAMS[param[1]];
      if (!values) throw new Error(`status-words: no fixture value for [${param[1]}] in ${route}`);
      return values.map((value) => route.replace(param[0], value));
    });
}

/** Pages the sweep visits again with a query that draws words the bare URL
 *  does not: `/prompts` opens on its first role, and only the reviewer's
 *  outcomes draw verdict words — at 390 as one block per version (#3026). */
const QUERIED = { '/prompts': ['/prompts?role=adversarial_review'] };

/** The URLs the sweep visits: every factory route, each followed by its
 *  `QUERIED` visits. */
function sweptRoutes() {
  return factoryRoutes().flatMap((route) => [route, ...(QUERIED[route] ?? [])]);
}

/** Every status word on screen, and those whose text runs past their box or
 *  over more than one line. A word with no box (a fold's hidden copy) is not
 *  on screen. Read once the page's fonts are in: a fallback face measures a
 *  different word. */
const readWords = async (page) => {
  await page.evaluate(() => document.fonts.ready);
  // The epic legend folds behind a disclosure below 640 (D16): open it, so its
  // words are still read at 390.
  await page.evaluate(async () => {
    const fold = document.querySelector('[data-fold-toggle][aria-expanded="false"]');
    if (!fold || fold.getClientRects().length === 0) return;
    fold.click();
    await new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)));
  });
  return page.locator('[data-status-word]').evaluateAll((words) =>
    words
      .filter((el) => el.getClientRects().length > 0)
      .map((el) => ({
        text: (el.textContent ?? '').trim(),
        scroll: el.scrollWidth,
        client: el.clientWidth,
        /** The lines the word's text is laid out on: one per text fragment
         *  that starts below the last line's bottom. */
        lines: (() => {
          const range = document.createRange();
          range.selectNodeContents(el);
          const rects = [...range.getClientRects()]
            .filter((r) => r.width > 0 && r.height > 0)
            .sort((a, b) => a.top - b.top);
          let lines = 0;
          let bottom = -Infinity;
          for (const r of rects) {
            if (r.top >= bottom - 1) lines += 1;
            bottom = Math.max(bottom, r.bottom);
          }
          return lines;
        })(),
        /** In a DAG card: 'selected' or 'unselected'; elsewhere null. */
        card: el.closest('.svelte-flow__node')
          ? el.closest('.card')?.classList.contains('sel')
            ? 'selected'
            : 'unselected'
          : null,
        /** The node popover, the epic legend, or elsewhere. */
        place: el.closest('[role="dialog"]') ? 'popover' : el.closest('[data-legend]') ? 'legend' : null,
        where: el.closest('[aria-label]')?.getAttribute('aria-label') ?? el.parentElement?.tagName
      }))
  );
};

const cutOf = (words) => words.filter((w) => w.scroll > w.client);
const wrappedOf = (words) => words.filter((w) => w.lines > 1);
const describe = (cut) =>
  cut.map((w) => `"${w.text}" needs ${w.scroll}px in ${w.client}px (in "${w.where}")`).join('; ');
const describeWrapped = (wrapped) =>
  wrapped.map((w) => `"${w.text}" wraps over ${w.lines} lines (in "${w.where}")`).join('; ');

export async function checks({ check, visit, expect, sleep }) {
  const routes = sweptRoutes();
  const counts = {};
  for (const route of routes) {
    await check(`no status word is cut or wrapped on ${route} at 390px`, () =>
      visit(
        route,
        async (page) => {
          // The reviewer's verdict words land after two reads (the roles, then
          // the outcomes), so that visit waits for its blocks, not a fixed pause.
          if (route.includes('role=adversarial_review')) {
            await page.locator('[data-outcome-block]').first().waitFor({ state: 'visible', timeout: 15_000 });
          }
          await sleep(SETTLE_MS);
          const words = await readWords(page);
          counts[route] = words.length;
          const cut = cutOf(words);
          expect(cut.length === 0, `${route}: ${describe(cut)}`);
          const wrapped = wrappedOf(words);
          expect(wrapped.length === 0, `${route}: ${describeWrapped(wrapped)}`);
        },
        PHONE
      )
    );
  }

  /** Not vacuous: the sweep read words on the routes that draw them — the
   *  Work rows, the epic's selected-node rail, the run list and a run, Health's
   *  cells, Monitor's columns, Audit's verdict and the reviewer's outcome
   *  blocks on Prompts. A route that stopped drawing
   *  any (or a hook that left StatusMark) shows here. */
  await check('the 390px status-word sweep read words where the fixtures draw them', async () => {
    for (const route of WORDED) {
      expect((counts[route] ?? 0) > 0, `the sweep read no status word on ${route}`);
    }
  });

  /** The negative control: a served long state the Work cell cannot hold is
   *  read as cut by the same sweep, so a pass above means the words fit. */
  await check('the 390px status-word sweep catches a served long status word on Work', () =>
    visit(
      '/work',
      async (page) => {
        await page.locator('aside[aria-label="Issues"] a[data-item]').first().waitFor({ timeout: 15_000 });
        await sleep(SETTLE_MS);
        const cut = cutOf(await readWords(page));
        expect(
          cut.some((w) => w.text === LONG_WORD),
          `the sweep did not flag "${LONG_WORD}"; it flagged: ${describe(cut) || 'nothing'}`
        );
      },
      PHONE,
      (page) =>
        page.route(
          (url) => url.pathname === '/api/dashboard/work',
          async (route) => {
            const response = await route.fetch();
            const body = await response.json();
            for (const view of body.views ?? []) {
              view.execution.state = LONG_STATE;
              view.exception.state = 'none';
            }
            return route.fulfill({ response, json: body });
          }
        )
    )
  );

  /** The same control on the epic canvas: a served long status on the nodes
   *  is read as cut in an UNSELECTED DAG card (196px), so the sweep reads the
   *  card's word and not only the selected node's rail. */
  await check('the 390px status-word sweep catches a long status word in an unselected DAG card', () =>
    visit(
      EPIC,
      async (page) => {
        await page.locator('.dag .svelte-flow__node').first().waitFor({ state: 'attached', timeout: 15_000 });
        await sleep(SETTLE_MS);
        const cut = cutOf(await readWords(page));
        expect(
          cut.some((w) => w.text === LONG_WORD && w.card === 'unselected'),
          `the sweep did not flag "${LONG_WORD}" in an unselected card; it flagged: ${describe(cut) || 'nothing'}`
        );
      },
      PHONE,
      (page) =>
        page.route(`**/api${EPIC}`, async (route) => {
          const response = await route.fetch();
          const payload = await response.json();
          for (const node of payload.snapshot.nodes) node.status = LONG_STATE;
          return route.fulfill({ response, json: payload });
        })
    )
  );

  /** The same control on the node popover: its status word is its own
   *  element apart from its age, so a served status too long for the phone's
   *  sheet is read as cut there. */
  await check('the 390px status-word sweep catches a long status word in the node popover', () =>
    visit(
      `${EPIC}?node=2055`,
      async (page) => {
        await page.locator('[role="dialog"] .pbody').first().waitFor({ state: 'visible', timeout: 15_000 });
        await sleep(SETTLE_MS);
        const cut = cutOf(await readWords(page));
        expect(
          cut.some((w) => w.text === SPRAWLING_WORD && w.place === 'popover'),
          `the sweep did not flag "${SPRAWLING_WORD}" in the popover; it flagged: ${describe(cut) || 'nothing'}`
        );
      },
      PHONE,
      (page) =>
        page.route(`**/api${EPIC}`, async (route) => {
          const response = await route.fetch();
          const payload = await response.json();
          for (const node of payload.snapshot.nodes) node.status = SPRAWLING_STATE;
          return route.fulfill({ response, json: payload });
        })
    )
  );

  /** And on the epic legend: a served count for a status too long for the
   *  phone's row is read as cut in the legend. */
  await check('the 390px status-word sweep catches a long status word in the epic legend', () =>
    visit(
      EPIC,
      async (page) => {
        await page.locator('[data-legend]').first().waitFor({ state: 'visible', timeout: 15_000 });
        await sleep(SETTLE_MS);
        const cut = cutOf(await readWords(page));
        expect(
          cut.some((w) => w.text === SPRAWLING_WORD && w.place === 'legend'),
          `the sweep did not flag "${SPRAWLING_WORD}" in the legend; it flagged: ${describe(cut) || 'nothing'}`
        );
      },
      PHONE,
      (page) =>
        page.route(`**/api${EPIC}`, async (route) => {
          const response = await route.fetch();
          const payload = await response.json();
          payload.snapshot.counts = { ...payload.snapshot.counts, [SPRAWLING_STATE]: 1 };
          return route.fulfill({ response, json: payload });
        })
    )
  );

  /** #3050 N9: the Work block header's status word is one line at 390. */
  const blockWord = async (page) => {
    await page.locator('section[aria-label^="Needs you"]').first().waitFor({ state: 'attached', timeout: 15_000 });
    await sleep(SETTLE_MS);
    return (await readWords(page)).filter((w) => w.where?.startsWith('Needs you') && /^needs you$/i.test(w.text));
  };

  await check('the Work block header reads NEEDS YOU on one line at 390px', () =>
    visit(
      `/work?item=${BLOCK_ITEM}`,
      async (page) => {
        const words = await blockWord(page);
        expect(words.length > 0, `the sweep read no "needs you" in the block header of #${BLOCK_ITEM}`);
        expect(wrappedOf(words).length === 0, describeWrapped(wrappedOf(words)));
        expect(cutOf(words).length === 0, describe(cutOf(words)));
      },
      PHONE
    )
  );

  /** The wrap control: the same header with its word let wrap and narrowed to
   *  its longest piece is read as wrapped, so a pass above means one line. */
  await check('the 390px status-word sweep catches a wrapped status word', () =>
    visit(
      `/work?item=${BLOCK_ITEM}`,
      async (page) => {
        await page.addStyleTag({
          content:
            'section[aria-label^="Needs you"] [data-status-word] { white-space: normal !important; width: min-content !important; flex-shrink: 1 !important; }'
        });
        const words = await blockWord(page);
        expect(
          wrappedOf(words).length > 0,
          `the sweep did not flag the wrapped header word; it read: ${JSON.stringify(words)}`
        );
      },
      PHONE
    )
  );

  /** A popover run verdict is never cut (#2951): a served verdict can be a
   *  sentence and a phone has no hover, so it wraps inside its own column, and
   *  the cost beside it stays in the row. */
  await check('a sentence-long run verdict wraps inside its popover row and the cost stays in the row at 390px', () =>
    visit(
      `${EPIC}?node=${BLOCK_ITEM}`,
      async (page) => {
        await page.locator('[role="dialog"] .hverdict').first().waitFor({ state: 'visible', timeout: 15_000 });
        await page.evaluate(() => document.fonts.ready);
        await sleep(SETTLE_MS);
        const read = await page
          .locator('[role="dialog"] .hrun')
          .first()
          .evaluate((row) => {
            const verdict = row.querySelector('.hverdict');
            const cost = row.querySelector('.num');
            const box = row.getBoundingClientRect();
            const at = cost?.getBoundingClientRect();
            const range = document.createRange();
            if (verdict) range.selectNodeContents(verdict);
            const tops = new Set([...range.getClientRects()].map((r) => Math.round(r.top)));
            return {
              text: (verdict?.textContent ?? '').trim(),
              cut: verdict ? verdict.scrollWidth > verdict.clientWidth : true,
              lines: tops.size,
              overflows: row.scrollWidth > row.clientWidth + 1,
              costIn: at !== undefined && at.width > 0 && at.left >= box.left - 1 && at.right <= box.right + 1
            };
          });
        expect(read.text === SENTENCE_VERDICT, `the served verdict reads "${read.text.slice(0, 60)}…"`);
        expect(!read.cut, 'the sentence-long run verdict is cut');
        expect(read.lines > 1, `the sentence-long run verdict sits on ${read.lines} line(s), not wrapped`);
        expect(!read.overflows && read.costIn, 'the run cost left its row beside a sentence-long verdict');
      },
      PHONE,
      (page) =>
        page.route(`**/api${EPIC}/nodes/${BLOCK_ITEM}`, async (route) => {
          const response = await route.fetch();
          const payload = await response.json();
          const [first, ...rest] = payload.runs;
          payload.runs = [{ ...first, status: SENTENCE_VERDICT }, ...rest];
          return route.fulfill({ response, json: payload });
        })
    )
  );
}
