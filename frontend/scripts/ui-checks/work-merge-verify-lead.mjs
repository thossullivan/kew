/**
 * A merge decision over a failed merge verify leads with the failure (#3460).
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 *
 * The fixture's #2055 is a merge decision on PR #2059 whose merge verify
 * failed at the PR's current head (`work-2055-pipeline.json`). Every surface
 * used to read "PR #2059 is open — the merge decision is yours" directly above
 * a red MERGE VERIFY section, asking a person to merge a PR kew had just seen
 * fail. Now Work's list, the item's needs-you block, Monitor's MERGE hold and
 * the epic rail lead with "Merge verify failed at 3670e43f: 2 of main's tests
 * fail on this PR", and each puts the item's MERGE VERIFY section before the
 * PR link. Following that link lands on the section.
 *
 * Negative control: on the old code (today's sentence, no `verify_link`) the
 * list drops the line under IN REVIEW (#2848), the block, the hold and the rail
 * say "PR #2059 is open", and no surface draws a link to the failure.
 *
 * Design review c9 D10 and D14: the failed verify is the one magenta move of
 * Work's block and of the rail's, the PR link quiet after it; and in the rail
 * that move comes before the sentence, inside the rail's visible box. Negative
 * control: on the old code the PR link is the filled control, and the rail
 * draws the sentence first with the links under it, below the fold.
 */
// ui-check-owns: src/lib/work/MergeVerify.svelte
// ui-check-owns: src/lib/work/entry-detail.ts
// ui-check-owns: src/lib/needs-you/moves.ts
// ui-check-owns: src/lib/work/list.ts
// ui-check-owns: src/lib/monitor/holds.ts
// ui-check-owns: src/lib/epic/rail.ts

const ITEM = '2055';
const FAILURE = "Merge verify failed at 3670e43f: 2 of main's tests fail on this PR";
const SENTENCE = `${FAILURE} — the merge decision is yours`;
const VERIFY_LABEL = 'See the failed merge verify';
const VERIFY_HREF = `/work?item=${ITEM}#merge-verify`;
const PR_LABEL = 'Open PR #2059 to merge';
/** DESIGN.md: a control a finger has to hit is at least 32px tall. */
const MIN_TAP = 32;

/** Whether the page scrolls sideways, which no surface may at any width. */
function sideways(page) {
  return page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1);
}

/** The links of a needs-you block, in the order drawn, with their heights. */
function blockLinks(block) {
  return block.evaluate((section) =>
    [...section.querySelectorAll('a[href]')].map((a) => ({
      label: (a.textContent ?? '').replace('↗', '').trim(),
      href: a.getAttribute('href') ?? '',
      height: a.getBoundingClientRect().height
    }))
  );
}

/** A needs-you block's sentence and links. */
async function readBlock(block) {
  await block.waitFor({ state: 'visible', timeout: 15_000 });
  const summary = block.locator('[data-needs-you-summary]').first();
  return {
    summary: ((await summary.textContent()) ?? '').replace(/\s+/g, ' ').trim(),
    links: await blockLinks(block)
  };
}

/** The verify link first, the PR link after it, each tall enough to hit. */
function expectLinks(expect, where, links, { minTap = MIN_TAP } = {}) {
  const verify = links.findIndex((l) => l.label === VERIFY_LABEL);
  const pr = links.findIndex((l) => l.label === PR_LABEL);
  const drawn = links.map((l) => l.label).join(' | ');
  expect(verify === 0, `${where}: the failed verify is not the first link — it draws ${drawn}`);
  expect(verify >= 0 && links[verify].href === VERIFY_HREF, `${where}: the verify link goes to ${links[verify]?.href}`);
  expect(pr > verify, `${where}: the PR link does not follow the verify link — it draws ${drawn}`);
  for (const link of [links[verify], links[pr]].filter(Boolean)) {
    expect(link.height >= minTap, `${where}: "${link.label}" is ${link.height}px tall, under ${minTap}px`);
  }
}

/**
 * Which of the block's links is filled with the accent: the one magenta move.
 * Read off the computed background, so a class rename cannot pass it.
 */
function filledMoves(block) {
  return block.evaluate((section) => {
    const accent = getComputedStyle(document.documentElement).getPropertyValue('--accent').trim();
    const probe = document.createElement('span');
    probe.style.color = accent;
    document.body.append(probe);
    const want = getComputedStyle(probe).color;
    probe.remove();
    return [...section.querySelectorAll('a[href]')]
      .filter((a) => getComputedStyle(a).backgroundColor === want)
      .map((a) => a.getAttribute('href'));
  });
}

/** The MERGE VERIFY section the link lands on, read once the page has moved. */
async function landed(page) {
  await page.waitForURL((url) => url.hash === '#merge-verify' && url.searchParams.get('item') === ITEM, {
    timeout: 15_000
  });
  const section = page.locator('section#merge-verify').first();
  await section.waitFor({ state: 'visible', timeout: 15_000 });
  // The jump is the browser's, or MergeVerify.svelte's once the pipeline lands.
  for (let i = 0; i < 20 && !(await inView(section)); i += 1) await page.waitForTimeout(100);
  return inView(section);
}

function inView(section) {
  return section.evaluate((el) => {
    const box = el.getBoundingClientRect();
    return box.top < window.innerHeight && box.bottom > 0;
  });
}

export async function checks({ check, visit, expect, sizes: { WIDTH, HEIGHT } }) {
  for (const [width, height] of [
    [390, 844],
    [768, 1024],
    [WIDTH, HEIGHT]
  ]) {
    const size = { width, height };

    await check(`Work's list leads #${ITEM} with its failed merge verify at ${width}px`, () =>
      visit(
        '/work',
        async (page) => {
          const row = page.locator(`aside[aria-label="Issues"] a[data-item="${ITEM}"]`).first();
          await row.waitFor({ state: 'visible', timeout: 15_000 });
          const reasons = await row
            .locator('[data-row-reason]')
            .evaluateAll((els) => els.map((el) => (el.textContent ?? '').replace(/\s+/g, ' ').trim()));
          expect(
            reasons.length === 1 && reasons[0].startsWith(FAILURE),
            `${width}px: #${ITEM}'s line under its title reads ${JSON.stringify(reasons)}`
          );
          expect(!(await sideways(page)), `${width}px: Work scrolls sideways`);
        },
        size
      )
    );

    await check(`#${ITEM}'s needs-you block leads with the failure and links it first at ${width}px`, () =>
      visit(
        `/work?item=${ITEM}`,
        async (page) => {
          const block = page.locator('main section[aria-label^="Needs you"]').first();
          const read = await readBlock(block);
          expect(read.summary === SENTENCE, `${width}px: the block says "${read.summary}"`);
          expectLinks(expect, `${width}px Work block`, read.links);
          const filled = await filledMoves(block);
          expect(
            filled.length === 1 && filled[0] === VERIFY_HREF,
            `${width}px: the Work block's magenta control is ${JSON.stringify(filled)}, not the failed verify alone`
          );
          expect(!(await sideways(page)), `${width}px: the item pane scrolls sideways`);
          await block.locator(`a[href="${VERIFY_HREF}"]`).first().click();
          expect(await landed(page), `${width}px: the verify link does not land on MERGE VERIFY`);
        },
        size
      )
    );

    await check(`Monitor's MERGE hold for #${ITEM} leads with the failure at ${width}px`, () =>
      visit(
        '/monitor',
        async (page) => {
          const row = page.locator(`section[aria-label="Holds and blocks"] li[data-hold-item="${ITEM}"]`).first();
          await row.waitFor({ state: 'visible', timeout: 15_000 });
          const read = await row.evaluate((li) => {
            const verify = li.querySelector('[data-hold-verify-link] a');
            const pr = li.querySelector('[data-hold-link] a');
            return {
              text: (li.textContent ?? '').replace(/\s+/g, ' '),
              verify: verify ? { href: verify.getAttribute('href'), height: verify.getBoundingClientRect().height } : null,
              order: verify && pr ? verify.compareDocumentPosition(pr) & Node.DOCUMENT_POSITION_FOLLOWING : 0
            };
          });
          expect(read.text.includes(SENTENCE), `${width}px: the hold reads "${read.text.trim()}"`);
          expect(read.verify?.href === VERIFY_HREF, `${width}px: the hold links the failure to ${read.verify?.href}`);
          expect(read.order !== 0, `${width}px: the hold's PR link does not follow its verify link`);
          // Inline links take the 32px tap height where a finger is the pointer
          // (`TAP_INLINE`); at desktop widths they are text in a line.
          if (width < 640) {
            expect(read.verify.height >= MIN_TAP, `${width}px: the verify link is ${read.verify.height}px tall`);
          }
          expect(!(await sideways(page)), `${width}px: Monitor scrolls sideways`);
          await row.locator('[data-hold-verify-link] a').first().click();
          expect(await landed(page), `${width}px: Monitor's verify link does not land on MERGE VERIFY`);
        },
        size
      )
    );

    await check(`the epic rail for #${ITEM} leads with the failure at ${width}px`, () =>
      visit(
        `/epics/launch-fixes?node=${ITEM}`,
        async (page) => {
          const block = page
            .locator('section[aria-label^="Selected node"] section[aria-label^="Needs you"]')
            .first();
          const read = await readBlock(block);
          expect(read.summary === SENTENCE, `${width}px: the rail says "${read.summary}"`);
          expectLinks(expect, `${width}px rail`, read.links);
          const filled = await filledMoves(block);
          expect(
            filled.length === 1 && filled[0] === VERIFY_HREF,
            `${width}px: the rail's magenta control is ${JSON.stringify(filled)}, not the failed verify alone`
          );
          // The move comes before the sentence, and its box is inside every
          // scroller around it — the rail's own and the window (D14).
          const placed = await block.evaluate((section, href) => {
            const move = section.querySelector(`a[href="${href}"]`);
            const summary = section.querySelector('[data-needs-you-summary]');
            if (!move || !summary) return null;
            const box = move.getBoundingClientRect();
            let clipped = box.bottom > window.innerHeight || box.top < 0;
            for (let el = move.parentElement; el && el !== document.body; el = el.parentElement) {
              const style = getComputedStyle(el);
              if (!/(auto|scroll|hidden)/.test(style.overflowY)) continue;
              const frame = el.getBoundingClientRect();
              if (box.top < frame.top - 1 || box.bottom > frame.bottom + 1) clipped = true;
            }
            return { first: move.compareDocumentPosition(summary) & Node.DOCUMENT_POSITION_FOLLOWING, clipped };
          }, VERIFY_HREF);
          expect(placed !== null && placed.first !== 0, `${width}px: the rail's move does not come before its sentence`);
          if (width >= 1024) {
            expect(placed !== null && !placed.clipped, `${width}px: the rail's move is cut off by the rail or the window`);
          }
          expect(!(await sideways(page)), `${width}px: the epic page scrolls sideways`);
        },
        size
      )
    );
  }
}
