/**
 * The two `/console` actions Work now offers (#3453): acknowledging an orphan
 * PR no item links to, and queueing an open issue kew has not touched.
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 *
 * Over the committed fixture set plus one unlinked orphan PR entry, served the
 * way the server serves it (no item, no epic, its PR's number and URL) — added
 * here rather than to `fixtures/factory/dashboard.json`, so every other check's
 * needs-you count stays the set's own. The set's `glance.queue_candidates` are
 * the picker's issues. The two writes are answered here, never sent anywhere.
 *
 * At 390, 768 and 1440 px: the orphan's Acknowledge takes the row away on a
 * 2xx and shows the refusal on a 4xx; the picker queues a candidate through its
 * confirm and shows the result; every control is at least 32px tall and the
 * page never scrolls sideways.
 */
// ui-check-owns: src/lib/work/OrphanAckControl.svelte
// ui-check-owns: src/lib/work/orphan-ack.ts
// ui-check-owns: src/lib/work/QueuePicker.svelte
// ui-check-owns: src/routes/(factory)/work/+page.svelte
import { readFileSync } from 'node:fs';
import { FRONTEND, widestBox } from '../ui-check-lib.mjs';

const served = JSON.parse(readFileSync(new URL('fixtures/factory/dashboard.json', FRONTEND), 'utf8'));
const ORPHAN_PR = 2608;
const ORPHAN_KEY = `orphan_pr:${ORPHAN_PR}`;
const SIZES = [
  { width: 390, height: 844 },
  { width: 768, height: 1024 },
  { width: 1440, height: 960 }
];

/** The committed set plus one unlinked orphan PR, as `needs_you._orphan_rows` builds it. */
function withOrphan(payload) {
  const orphan = {
    ...payload.rows[0],
    type: 'orphan_pr',
    consequence: 'hygiene',
    age: payload.rows[0].age,
    reason: 'orphan PR — no-record',
    sentence: '',
    primary_action: 'ack',
    issue_num: null,
    item_id: '',
    epic_ref: null,
    pr_number: ORPHAN_PR,
    url: `https://github.com/o/r/pull/${ORPHAN_PR}`,
    attention_kind: '',
    label: '',
    command: '',
    title: '',
    link: null,
    node_ids: []
  };
  return { ...payload, rows: [...payload.rows, orphan], needs_you_no_epic: 1 };
}

/** Serve `/api/dashboard` with the orphan in it, and answer `write` (a path) with `reply`. */
function prepare(write, reply) {
  return async (page) => {
    await page.route(
      (url) => url.pathname === '/api/dashboard',
      async (route) => {
        const response = await route.fetch();
        await route.fulfill({ response, json: withOrphan(await response.json()) });
      }
    );
    await page.route(
      (url) => url.pathname === write,
      (route) =>
        route.request().method() === 'POST'
          ? route.fulfill({ status: reply.status, contentType: 'application/json', body: JSON.stringify(reply.body) })
          : route.fallback()
    );
  };
}

/** A 4xx the check asked for is logged by the browser as a failed load; nothing else is excused. */
const unexpected = (problems) => problems.filter((p) => !/Failed to load resource/.test(p));

async function tallEnough(expect, locator, what) {
  const box = await locator.boundingBox();
  expect(box !== null && box.height >= 31.5, `${what} is ${box?.height}px tall, under 32`);
}

async function noSidewaysScroll(page, expect) {
  const wide = await widestBox(page);
  expect(wide.w <= wide.c, `the page scrolls sideways: ${wide.w} > ${wide.c} (${wide.culprit})`);
}

export async function checks({ check, visit, expect }) {
  const candidates = served.glance.queue_candidates;

  for (const size of SIZES) {
    await check(`work: an unlinked orphan PR is acknowledged and its row leaves at ${size.width}px`, async () => {
      const ack = `/api/verbs/orphans/${ORPHAN_PR}/ack`;
      await visit(
        `/work?item=${encodeURIComponent(ORPHAN_KEY)}`,
        async (page, problems) => {
          const control = page.locator(`[data-orphan-ack="${ORPHAN_PR}"]`);
          await control.waitFor({ state: 'visible', timeout: 15_000 });
          const button = control.getByRole('button', { name: 'Acknowledge', exact: true });
          expect((await button.count()) === 1, 'the orphan row has no single Acknowledge');
          await tallEnough(expect, button, 'Acknowledge');
          await noSidewaysScroll(page, expect);
          await button.click();
          await control.waitFor({ state: 'detached', timeout: 10_000 });
          expect(
            (await page.locator(`a[data-item="${ORPHAN_KEY}"]`).count()) === 0,
            'the acknowledged orphan is still a row in the list'
          );
          expect(!new URL(page.url()).searchParams.has('item'), `the page still selects it: ${page.url()}`);
          expect(problems.length === 0, problems.join('\n'));
        },
        size,
        prepare(ack, { status: 200, body: { pr_number: ORPHAN_PR, acked: true, acked_at: null } })
      );
    });

    await check(`work: a refused orphan acknowledge says why and keeps the row at ${size.width}px`, async () => {
      const ack = `/api/verbs/orphans/${ORPHAN_PR}/ack`;
      const why = `PR #${ORPHAN_PR} is not an orphan any more`;
      await visit(
        `/work?item=${encodeURIComponent(ORPHAN_KEY)}`,
        async (page, problems) => {
          const control = page.locator(`[data-orphan-ack="${ORPHAN_PR}"]`);
          await control.waitFor({ state: 'visible', timeout: 15_000 });
          await control.getByRole('button', { name: 'Acknowledge', exact: true }).click();
          const error = control.locator('[data-orphan-ack-error]');
          await error.waitFor({ state: 'visible', timeout: 10_000 });
          const said = ((await error.textContent()) ?? '').trim();
          expect(said === why, `the refusal reads "${said}"`);
          expect((await page.locator(`a[data-item="${ORPHAN_KEY}"]`).count()) === 1, 'the refused orphan left the list');
          await noSidewaysScroll(page, expect);
          const left = unexpected(problems);
          expect(left.length === 0, left.join('\n'));
        },
        size,
        prepare(ack, { status: 404, body: { detail: why } })
      );
    });

    await check(`work: an untouched issue is queued through the picker's confirm at ${size.width}px`, async () => {
      const issue = candidates[0].issue_num;
      await visit(
        '/work',
        async (page, problems) => {
          const picker = page.locator('[data-queue-picker]');
          await picker.waitFor({ state: 'visible', timeout: 15_000 });
          const select = picker.locator('select');
          const offered = await select.locator('option').evaluateAll((os) => os.map((o) => o.value).filter((v) => v !== ''));
          expect(candidates.length >= 2, 'the fixture set serves fewer than two queue candidates');
          expect(
            JSON.stringify(offered) === JSON.stringify(candidates.map((c) => String(c.issue_num))),
            `the picker offers ${JSON.stringify(offered)}`
          );
          await tallEnough(expect, select, 'the issue picker');
          await select.selectOption(String(issue));
          const queue = picker.getByRole('button', { name: 'Queue', exact: true });
          await tallEnough(expect, queue, 'Queue');
          await queue.click();
          const confirm = picker.locator('[data-queue-confirm]');
          await confirm.waitFor({ state: 'visible', timeout: 5_000 });
          const asks = ((await confirm.textContent()) ?? '').replace(/\s+/g, ' ');
          expect(asks.includes(candidates[0].title), `the confirm does not name the issue: "${asks}"`);
          for (const name of ['Cancel', 'Confirm queue']) {
            await tallEnough(expect, confirm.getByRole('button', { name, exact: true }), name);
          }
          await noSidewaysScroll(page, expect);
          await confirm.getByRole('button', { name: 'Confirm queue', exact: true }).click();
          const result = picker.locator(`[data-queue-result="${issue}"]`);
          await result.waitFor({ state: 'visible', timeout: 10_000 });
          const said = ((await result.textContent()) ?? '').replace(/\s+/g, ' ');
          expect(said.includes('queued'), `the result reads "${said}"`);
          const left = await select.locator('option').evaluateAll((os) => os.map((o) => o.value));
          expect(!left.includes(String(issue)), `the queued issue is still offered: ${JSON.stringify(left)}`);
          await noSidewaysScroll(page, expect);
          expect(problems.length === 0, problems.join('\n'));
        },
        size,
        prepare(`/api/verbs/queue/${issue}`, { status: 200, body: { issue, ready: true, driver_stale: false } })
      );
    });
  }
}
