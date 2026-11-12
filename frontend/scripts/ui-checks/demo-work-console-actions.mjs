/**
 * Work's orphan-PR acknowledge and its queue picker on the static demo build
 * (#3453).
 *
 * A demo module (`demo-*.mjs`): `scripts/ui-check.mjs` runs it only against the
 * static server for `build-demo/` (or a remote demo base, through
 * `npm run demo-check`). The world serves one orphan PR no item links to and two
 * open issues kew has not touched (`demo/world/manifest.json` names them). The
 * demo has no backend, so each press is answered by the demo shim with its one
 * sentence — "Demo: this would …" — which the control shows where a real refusal
 * would land, and nothing is sent anywhere.
 */
// ui-check-owns: src/lib/work/OrphanAckControl.svelte
// ui-check-owns: src/lib/work/QueuePicker.svelte
// ui-check-owns: demo/world/dashboard.json
import { readFileSync } from 'node:fs';
import { FRONTEND, widestBox } from '../ui-check-lib.mjs';

const WORLD = new URL('demo/world/', FRONTEND);
const read = (file) => JSON.parse(readFileSync(new URL(file, WORLD), 'utf8'));
const SIZES = [
  { width: 390, height: 844 },
  { width: 768, height: 1024 },
  { width: 1440, height: 960 }
];

async function tallEnough(expect, locator, what) {
  const box = await locator.boundingBox();
  expect(box !== null && box.height >= 31.5, `${what} is ${box?.height}px tall, under 32`);
}

async function noSidewaysScroll(page, expect) {
  const wide = await widestBox(page);
  expect(wide.w <= wide.c, `the page scrolls sideways: ${wide.w} > ${wide.c} (${wide.culprit})`);
}

/** The demo shim's answer is a 405 the browser logs as a failed load; nothing else is excused. */
const unexpected = (problems) => problems.filter((p) => !/Failed to load resource/.test(p));

export async function checks({ check, visit, expect }) {
  const manifest = read('manifest.json');
  const dashboard = read('dashboard.json');
  const pr = manifest.orphan_pr;
  const candidates = dashboard.glance.queue_candidates;

  for (const size of SIZES) {
    await check(`demo world: Work's Acknowledge on orphan PR #${pr} is answered by the demo at ${size.width}px`, async () => {
      await visit(
        `/work?item=${encodeURIComponent(`orphan_pr:${pr}`)}`,
        async (page, problems) => {
          const control = page.locator(`[data-orphan-ack="${pr}"]`);
          await control.waitFor({ state: 'visible', timeout: 15_000 });
          const button = control.getByRole('button', { name: 'Acknowledge', exact: true });
          await tallEnough(expect, button, 'Acknowledge');
          await noSidewaysScroll(page, expect);
          await button.click();
          const said = control.locator('[data-orphan-ack-error]');
          await said.waitFor({ state: 'visible', timeout: 10_000 });
          const text = ((await said.textContent()) ?? '').trim();
          expect(
            text.startsWith(`Demo: this would acknowledge orphaned pull request #${pr}.`),
            `the press reads "${text}"`
          );
          const left = unexpected(problems);
          expect(left.length === 0, left.join('\n'));
        },
        size
      );
    });

    await check(`demo world: Work's Not started picker queues through its confirm at ${size.width}px`, async () => {
      const issue = candidates[0].issue_num;
      await visit(
        '/work',
        async (page, problems) => {
          const picker = page.locator('[data-queue-picker]');
          await picker.waitFor({ state: 'visible', timeout: 15_000 });
          const select = picker.locator('select');
          const offered = await select.locator('option').evaluateAll((os) => os.map((o) => o.value).filter((v) => v !== ''));
          expect(
            JSON.stringify(offered) === JSON.stringify(manifest.queue_candidates),
            `the picker offers ${JSON.stringify(offered)}`
          );
          await tallEnough(expect, select, 'the issue picker');
          await select.selectOption(String(issue));
          await picker.getByRole('button', { name: 'Queue', exact: true }).click();
          const confirm = picker.getByRole('button', { name: 'Confirm queue', exact: true });
          await tallEnough(expect, confirm, 'Confirm queue');
          await noSidewaysScroll(page, expect);
          await confirm.click();
          const said = picker.locator('[data-queue-error]');
          await said.waitFor({ state: 'visible', timeout: 10_000 });
          const text = ((await said.textContent()) ?? '').trim();
          expect(text.startsWith(`Demo: this would queue #${issue} for the next dispatch.`), `the press reads "${text}"`);
          const left = unexpected(problems);
          expect(left.length === 0, left.join('\n'));
        },
        size
      );
    });
  }
}
