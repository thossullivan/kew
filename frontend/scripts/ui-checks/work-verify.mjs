/**
 * The Work page says why the merge verify refused a PR (#3140).
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 *
 * #2055's pipeline fixture carries a failed `verify_completed` whose held-out
 * phase found main's tests failing on the PR; #2047's a passed one at its
 * current head; SFD-229's an unfinished one on an older head, ended by the
 * host's deadline. Each is checked at 1440, 768 and 390:
 *
 * - the failed reading prints every failing id, in Plex Mono, inside the
 *   page's width, and notes the excused one in a sentence-case 13px line;
 * - the passed reading names the head it verified;
 * - the error category reads in words, never as its code and never uppercased,
 *   and the code kew recorded sits in the words' `title` (#3466);
 * - the colour rule (DESIGN.md): a failed verify is red, magenta only when a
 *   stage of the same payload says a person is next; a passed one is green.
 */
// ui-check-owns: src/lib/work/MergeVerify.svelte
// ui-check-owns: src/lib/work/merge-verify.ts
// ui-check-owns: src/routes/(factory)/work/+page.*
// ui-check-owns: src/lib/factory/names.ts
import { readFileSync } from 'node:fs';
import { FRONTEND, SETTLE_MS } from '../ui-check-lib.mjs';

const fixture = (item) =>
  JSON.parse(
    readFileSync(new URL(`fixtures/factory/work-${item}-pipeline.json`, FRONTEND), 'utf8')
  );

const FAILED_IDS = [
  'tests/test_queue.py::test_lease_expires_after_ttl',
  'tests/test_queue.py::test_claim_is_exclusive',
  'tests/test_queue.py::test_retry_label_is_renamed'
];
const EXCUSED_ID = 'tests/test_queue.py::test_retry_label_is_renamed';
const EXCUSED_NOTE = '— excused by a declared behavior change';
const PASSED_HEAD = 'c4e19a07';
/** SFD-229's recorded category, in the words `ERROR_CATEGORY_LABELS` gives it. */
const DEADLINE_WORDS = 'The verify host hit its job deadline';

/**
 * The colour token the payload's verify must be drawn in. A failed verify is
 * the machine's refusal (red) unless a stage of the SAME payload already says
 * a person is next — then it carries that person's magenta.
 */
function expectedToken(payload) {
  const verify = payload.merge_verify;
  if (verify.outcome === 'passed') return '--success';
  if (verify.outcome !== 'failed') return null;
  const personNext = payload.lanes.some((lane) => lane.stages.some((s) => s.state === 'needs_you'));
  return personNext ? '--accent' : '--danger';
}

async function readSection(page, base, item) {
  await page.goto(`${base}/work?item=${encodeURIComponent(item)}`, {
    waitUntil: 'load'
  });
  const section = page.locator('section[aria-label="Merge verify"]');
  await section.waitFor({ state: 'visible', timeout: 15_000 });
  await page.waitForTimeout(SETTLE_MS);
  return section.evaluate((root) => {
    const font = (el) => getComputedStyle(el).fontFamily;
    const right = (el) => el.getBoundingClientRect().right;
    const ids = [...root.querySelectorAll('[data-held-out-test]')];
    const head = root.querySelector('[data-merge-verify-head]');
    const excused = root.querySelector('[data-held-out-excused]');
    const category = root.querySelector('[data-merge-verify-category]');
    const mark = root.querySelector('[data-merge-verify] svg');
    const painted = mark
      ? [...mark.querySelectorAll('*')].flatMap((el) =>
          ['fill', 'stroke']
            .map((a) => el.getAttribute(a) ?? '')
            .filter((v) => v.startsWith('var('))
        )
      : [];
    return {
      text: (root.textContent ?? '').replace(/\s+/g, ' ').trim(),
      ids: ids.map((el) => (el.textContent ?? '').trim()),
      idBreaks: ids.map((el) => el.querySelectorAll('wbr').length),
      idFonts: ids.map(font),
      idRights: ids.map(right),
      head: head ? (head.textContent ?? '').trim() : '',
      headFont: head ? font(head) : '',
      excused: excused
        ? {
            line: (excused.closest('li')?.innerText ?? '').replace(/\s+/g, ' ').trim(),
            shown: excused.innerText,
            size: getComputedStyle(excused).fontSize,
            transform: getComputedStyle(excused).textTransform
          }
        : null,
      category: category
        ? {
            shown: category.innerText,
            title: category.getAttribute('title') ?? '',
            transform: getComputedStyle(category).textTransform,
            right: right(category)
          }
        : null,
      painted,
      page: {
        scroll: document.documentElement.scrollWidth,
        client: document.documentElement.clientWidth
      }
    };
  });
}

function expectColour(expect, read, payload, width) {
  const token = expectedToken(payload);
  const used = new Set(read.painted.map((v) => v.replace(/^var\((--[\w-]+)\).*$/, '$1')));
  if (token) {
    expect(used.has(token), `${width}px: the mark is painted ${[...used]}, not ${token}`);
  }
  for (const other of ['--accent', '--danger']) {
    if (other === token) continue;
    expect(!used.has(other), `${width}px: the mark carries ${other} it has no reason for`);
  }
}

async function onPage(browser, width, height, fn) {
  const ctx = await browser.newContext({ viewport: { width, height } });
  const page = await ctx.newPage();
  try {
    await fn(page);
  } finally {
    await page.close();
    await ctx.close();
  }
}

export async function checks({ browser, base, check, expect, sizes: { WIDTH, HEIGHT } }) {
  const sizes = [
    [WIDTH, HEIGHT],
    [768, 1024],
    [390, 844]
  ];
  for (const [width, height] of sizes) {
    await check(`the failed merge verify lists main's failing tests at ${width}px`, () =>
      onPage(browser, width, height, async (page) => {
        const read = await readSection(page, base, '2055');
        expect(
          read.text.includes("Merge verify failed at 3670e43f: 2 of main's tests fail on this PR."),
          `${width}px: the reading says "${read.text}"`
        );
        expect(
          JSON.stringify([...read.ids].sort()) === JSON.stringify([...FAILED_IDS].sort()),
          `${width}px: the ids shown are ${JSON.stringify(read.ids)}`
        );
        for (const family of read.idFonts) {
          expect(/Plex Mono/i.test(family), `${width}px: a test id is set in ${family}`);
        }
        for (const count of read.idBreaks) {
          expect(count === 2, `${width}px: a test id carries ${count} <wbr>, not one per seam`);
        }
        expect(
          read.page.scroll <= read.page.client,
          `${width}px: the page scrolls sideways (${read.page.scroll} over ${read.page.client})`
        );
        for (const r of read.idRights) {
          expect(r <= read.page.client + 0.5, `${width}px: a test id runs to ${r}px, off the page`);
        }
      })
    );

    await check(`the excused failure is noted in a sentence at ${width}px`, () =>
      onPage(browser, width, height, async (page) => {
        const read = await readSection(page, base, '2055');
        const note = read.excused;
        expect(note !== null, `${width}px: no excused note is drawn`);
        if (!note) return;
        expect(
          note.line === `${EXCUSED_ID} ${EXCUSED_NOTE}`,
          `${width}px: the excused line reads "${note.line}"`
        );
        expect(note.shown.trim() === EXCUSED_NOTE, `${width}px: the note shows "${note.shown}"`);
        expect(note.size === '13px', `${width}px: the note is ${note.size}, not 13px`);
        expect(note.transform === 'none', `${width}px: the note is ${note.transform}`);
      })
    );

    await check(`the failed merge verify keeps the colour rule at ${width}px`, () =>
      onPage(browser, width, height, async (page) => {
        expectColour(expect, await readSection(page, base, '2055'), fixture('2055'), width);
      })
    );

    await check(`the passed merge verify names its head at ${width}px`, () =>
      onPage(browser, width, height, async (page) => {
        const read = await readSection(page, base, '2047');
        expect(
          read.text.includes(`Merge verify passed at ${PASSED_HEAD}.`),
          `${width}px: the reading says "${read.text}"`
        );
        expect(read.head === PASSED_HEAD, `${width}px: the head shown is "${read.head}"`);
        expect(/Plex Mono/i.test(read.headFont), `${width}px: the head is set in ${read.headFont}`);
        expect(
          read.ids.length === 0,
          `${width}px: a passed verify lists ${read.ids.length} failures`
        );
        expectColour(expect, read, fixture('2047'), width);
      })
    );

    await check(`the error category reads in words, its code in a title, at ${width}px`, () =>
      onPage(browser, width, height, async (page) => {
        const payload = fixture('SFD-229');
        const recorded = payload.merge_verify.error_category;
        const read = await readSection(page, base, 'SFD-229');
        const shown = read.category;
        expect(shown !== null, `${width}px: no error category is drawn`);
        if (!shown) return;
        expect(
          recorded === 'host_job_deadline_exceeded',
          `the SFD-229 fixture records "${recorded}", so this check proves nothing`
        );
        expect(
          shown.shown.trim() === DEADLINE_WORDS,
          `${width}px: the category reads "${shown.shown}", not "${DEADLINE_WORDS}"`
        );
        expect(
          !read.text.includes(recorded),
          `${width}px: the section prints the code "${recorded}"`
        );
        expect(
          shown.title === recorded,
          `${width}px: the category's title is "${shown.title}", not the code "${recorded}"`
        );
        expect(shown.transform === 'none', `${width}px: the category is ${shown.transform}`);
        expect(
          shown.right <= read.page.client + 0.5,
          `${width}px: the category runs to ${shown.right}px, off the page`
        );
        expect(
          read.text.includes('Merge verify did not finish at an older head, b71c04e2.'),
          `${width}px: the reading says "${read.text}"`
        );
        expectColour(expect, read, payload, width);
      })
    );
  }
}
