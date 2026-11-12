/**
 * A merge row says its reason once, however the PR is served (#2893).
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 *
 * #2848 dropped the generic "PR #N is open — the merge decision is yours" line
 * under a merge row's title, but only where the row carried `pr_number`. Live
 * rows carry the PR as a URL with `pr_number: null`, so every live merge row
 * still printed it under IN REVIEW. The fixture's row has a `pr_number`, so it
 * is served here as the live one is — and with a long title, because the line
 * the dropped reason gives back is the title's second one, and a short fixture
 * title cannot show whether it fills it or spills out of the row (#2889).
 *
 * The fixture's 2055 is a merge over a verify that failed at its head, whose
 * sentence leads with the failure and is kept (#3460, `work-merge-verify-lead`).
 * The drop is about the generic sentence, so the row is served as a merge with
 * no failure: today's sentence and no verify link.
 */
// ui-check-owns: src/routes/(factory)/work/+page.svelte
// ui-check-owns: src/lib/work/list.ts

const MERGE_SENTENCE = /is open — the merge decision is yours/;
const ITEM = '2055';
const LONG_TITLE =
  'Merge gate: refuse a publication whose recorded head is not the branch tip the reviewer ' +
  'read, and name the head it read alongside the one it found, so that an operator can tell ' +
  'a moved branch from a rewritten one without opening the journal';

/**
 * *value* with every record naming the item titled long. The row's title is
 * the first one any payload in hand gives the item (`titleIndex`), so every
 * record that names it is retitled, not only the one read first today.
 */
function longTitle(value) {
  if (Array.isArray(value)) return value.map(longTitle);
  if (value === null || typeof value !== 'object') return value;
  const out = Object.fromEntries(Object.entries(value).map(([k, v]) => [k, longTitle(v)]));
  const names = String(out.item_id ?? out.issue_num ?? out.number ?? '') === ITEM;
  return names && typeof out.title === 'string' && out.title !== '' ? { ...out, title: LONG_TITLE } : out;
}

/** `/api/dashboard` with every merge row's PR served as a URL only, and no failed verify. */
function liveMergeRows(payload) {
  const rows = (payload.rows ?? []).map((row) =>
    row.attention_kind === 'merge_decision' || row.attention_kind === 'merge_ready'
      ? {
          ...row,
          pr_number: null,
          sentence: `PR #${row.pr_number} is open — the merge decision is yours`,
          verify_link: null
        }
      : row
  );
  return longTitle({ ...payload, rows });
}

/** Serve *pathname* (exactly that path) through *shape*. */
async function reshape(page, pathname, shape) {
  await page.route(
    (url) => url.pathname === pathname,
    async (route) => {
      const response = await route.fetch();
      await route.fulfill({ response, json: shape(await response.json()) });
    }
  );
}

export async function checks({ browser, base, check, expect, sleep, sizes: { WIDTH, HEIGHT } }) {
  const sizes = [
    [WIDTH, HEIGHT],
    [390, 844]
  ];

  for (const [width, height] of sizes) {
    await check(`a merge row served with pr_number: null drops the generic reason at ${width}px`, async () => {
      const ctx = await browser.newContext({ viewport: { width, height } });
      const page = await ctx.newPage();
      try {
        await reshape(page, '/api/dashboard', liveMergeRows);
        await reshape(page, '/api/runs', longTitle);
        await page.goto(`${base}/work`, { waitUntil: 'load' });
        const row = page.locator(`aside[aria-label="Issues"] a[data-item="${ITEM}"]`).first();
        await row.waitFor({ state: 'visible', timeout: 15_000 });
        await sleep(700);
        const read = await row.evaluate((a) => {
          const title = a.querySelector(':scope > span:nth-child(2)');
          const box = (el) => el?.getBoundingClientRect() ?? null;
          return {
            reasons: [...a.querySelectorAll('[data-row-reason]')].map((el) => el.textContent ?? ''),
            text: a.textContent ?? '',
            diamond: a.querySelector('[data-needs-you-diamond]') !== null,
            state: (a.lastElementChild?.textContent ?? '').trim().toLowerCase(),
            title: (title?.textContent ?? '').trim(),
            titleBox: box(title),
            rowBox: box(a)
          };
        });
        // The served state must be the one the drop is about, or its absence
        // proves nothing: IN REVIEW beside the needs-you diamond.
        expect(read.diamond, `${width}px: #${ITEM} wears no needs-you diamond`);
        expect(/\bin review\b/.test(read.state), `${width}px: #${ITEM} reads "${read.state}"`);
        expect(read.title.startsWith('Merge gate:'), `${width}px: #${ITEM} is titled "${read.title}"`);
        expect(read.reasons.length === 0, `${width}px: #${ITEM} still has a reason line: ${read.reasons}`);
        expect(!MERGE_SENTENCE.test(read.text), `${width}px: #${ITEM} still says the merge decision is yours`);
        expect(
          read.titleBox.bottom <= read.rowBox.bottom + 0.5,
          `${width}px: the long title spills out of its ${read.rowBox.height}px row`
        );
        // And no other row on the list prints it either.
        const anywhere = await page
          .locator('aside[aria-label="Issues"] a[data-item]')
          .evaluateAll((rows) => rows.map((a) => a.textContent ?? ''));
        const saying = anywhere.filter((text) => MERGE_SENTENCE.test(text));
        expect(saying.length === 0, `${width}px: ${saying.length} Work row(s) still say it`);
      } finally {
        await page.close();
        await ctx.close();
      }
    });
  }
}
