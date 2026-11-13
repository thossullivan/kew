/**
 * A remote read failure is kew's pending read, not a needs-you, and its row
 * says what could not be read (#2893).
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 *
 * The server's needs-you set stopped counting `remote_unavailable`, but the
 * Work list still read the exception as `manual`: live #2870 wore the magenta
 * needs-you mark beside IN REVIEW with no entry behind it and no reason under
 * it. The committed fixture has no such item, so #2047 is served here as #2870
 * was — its tracker unread after the merge — with no needs-you row for it.
 */
// ui-check-owns: src/routes/(factory)/work/+page.svelte
// ui-check-owns: src/lib/work/list.ts

const ITEM = '2047';
const DETAIL = "the source item's state could not be read";

/** `/api/dashboard/work` with #2047's tracker unread. */
function trackerUnread(payload) {
  const views = (payload.views ?? []).map((view) =>
    view.identity?.item_id === ITEM
      ? {
          ...view,
          exception: {
            state: 'remote_unavailable',
            detail: DETAIL,
            blocking: false,
            dimension: 'tracker_item'
          },
          safest_action: {
            action: 'observe_remote',
            reason: `${DETAIL}; observe it before acting on a guess`,
            blocked_by: 'tracker_item'
          }
        }
      : view
  );
  return { ...payload, views };
}

/** `/api/dashboard` as the server serves it now: no needs-you row for #2047. */
function noEntry(payload) {
  const rows = (payload.rows ?? []).filter((row) => String(row.item_id ?? '') !== ITEM);
  return { ...payload, rows };
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
    await check(`a row whose tracker could not be read is no needs-you and says so at ${width}px`, async () => {
      const ctx = await browser.newContext({ viewport: { width, height } });
      const page = await ctx.newPage();
      try {
        await reshape(page, '/api/dashboard/work', trackerUnread);
        await reshape(page, '/api/dashboard', noEntry);
        await page.goto(`${base}/work`, { waitUntil: 'load' });
        const row = page.locator(`aside[aria-label="Issues"] a[data-item="${ITEM}"]`).first();
        await row.waitFor({ state: 'visible', timeout: 15_000 });
        await sleep(700);
        const read = await row.evaluate((a) => {
          const reason = a.querySelector('[data-row-reason]');
          const box = (el) => el?.getBoundingClientRect() ?? null;
          return {
            reason: (reason?.textContent ?? '').trim(),
            reasonBox: box(reason),
            rowBox: box(a),
            diamond: a.querySelector('[data-needs-you-diamond]') !== null,
            state: (a.lastElementChild?.textContent ?? '').trim().toLowerCase()
          };
        });
        expect(!read.diamond, `${width}px: #${ITEM} wears the needs-you diamond`);
        expect(
          !/\bneeds you\b/.test(read.state),
          `${width}px: #${ITEM} reads "${read.state}", a person's move`
        );
        expect(/\bpending\b/.test(read.state), `${width}px: #${ITEM} reads "${read.state}", not PENDING`);
        expect(read.reason === DETAIL, `${width}px: #${ITEM}'s notice reads "${read.reason}"`);
        expect(
          read.reasonBox !== null && read.reasonBox.height > 0 && read.reasonBox.width > 0,
          `${width}px: #${ITEM}'s notice is not drawn`
        );
        expect(
          read.reasonBox === null || read.reasonBox.bottom <= read.rowBox.bottom + 0.5,
          `${width}px: #${ITEM}'s notice spills out of its ${read.rowBox.height}px row`
        );
        // And the needs-you filter does not list it.
        await page.goto(`${base}/work?filter=needs-you`, { waitUntil: 'load' });
        await page
          .locator('aside[aria-label="Issues"] a[data-item]')
          .first()
          .waitFor({ state: 'visible', timeout: 15_000 });
        await sleep(700);
        const listed = await page
          .locator(`aside[aria-label="Issues"] a[data-item="${ITEM}"]`)
          .count();
        expect(listed === 0, `${width}px: the needs-you filter lists #${ITEM}`);
      } finally {
        await page.close();
        await ctx.close();
      }
    });
  }
}
