/**
 * The demo world on the static demo build (#3141).
 *
 * A demo module (`demo-*.mjs`): `scripts/ui-check.mjs` runs it only against
 * the static server for `build-demo/`, which bundles `demo/world/`. Every ref
 * comes from `demo/world/manifest.json`, and every expected figure from the
 * world's own files, so a regenerated world moves the checks with it:
 *
 * - the header names the invented repository and counts three things that
 *   need a person;
 * - `/loops` and `/scorecard` draw the world's data (the one loop by its
 *   objective, #3461), never their offline state;
 * - the epic canvas draws the blocked node and the node waiting on a person;
 * - the refused PR's work page lists the held-out tests its merge verify
 *   failed on, and says the review cycle sent it back to kew;
 * - the epic's spend, its refused node's spend and the budget page's week
 *   agree with each other and with the files;
 * - the built bundle passes every leak-scan bundle rule: no path, email,
 *   token, unlisted host, other owner or factory name. That scan reads
 *   `build-demo/` on disk, so `npm run demo-check` against a remote base
 *   (`remote`) skips it and says so (`LOCAL_BUILD_CHECKS`, #3207).
 */
// ui-check-owns: demo/world/**
// ui-check-owns: demo/leak-scan.*.json
import { readFileSync } from 'node:fs';
import { factoryDenylist, describeFinding, loadAllowlist, scan } from '../../demo/leak-scan.mjs';
import { localBuildCheck } from '../demo-check-lib.mjs';
import { DEMO_BUILD_DIR, FRONTEND, stageCaption } from '../ui-check-lib.mjs';

const WORLD = new URL('demo/world/', FRONTEND);
const read = (file) => JSON.parse(readFileSync(new URL(file, WORLD), 'utf8'));
const usd = (n) => `$${n.toFixed(2)}`;
const PHONE = { width: 390, height: 844 };

export async function checks({ check, visit, expect, sizes: { WIDTH, HEIGHT }, remote = false, skip }) {
  const manifest = read('manifest.json');
  const epic = read(`epics-${manifest.epic_ref}.json`);
  const spend = read('report-spend.json');
  const scorecard = read('report-models.json');
  const outcomes = read('report-outcomes.json');
  const loops = read('loops.json');

  // The one scan of a built bundle (scripts/demo-world.test.ts scans the
  // source tree; the build exists only here, right after ui-check makes it).
  const leakScan = localBuildCheck('demo world: the bundle passes every leak-scan bundle rule');
  if (remote) skip(leakScan.name, leakScan.why);
  else {
    await check(leakScan.name, async () => {
      const found = scan(DEMO_BUILD_DIR, { bundle: true, allow: loadAllowlist(), denylist: factoryDenylist() }).map(describeFinding);
      expect(found.length === 0, `build-demo/ leaks:\n${found.slice(0, 20).join('\n')}`);
    });
  }

  await check(`demo world: the header names the invented repository ${manifest.repo}`, async () => {
    await visit('/work', async (page) => {
      const repo = page.locator(`header span[title="${manifest.repo}"]`).first();
      await repo.waitFor({ state: 'visible', timeout: 10_000 });
      const said = ((await repo.textContent()) ?? '').trim();
      expect(said === manifest.repo, `the header reads "${said}", not ${manifest.repo}`);
    });
  });

  await check('demo world: /loops and /scorecard draw their data, never an error state', async () => {
    await visit('/loops', async (page) => {
      const main = page.locator('main').first();
      await main.getByText('kew serve · connected').first().waitFor({ state: 'visible', timeout: 10_000 });
      const text = ((await main.innerText()) ?? '').replace(/\s+/g, ' ');
      expect(!/offline|start kew serve/i.test(text), `/loops reads as offline: "${text.slice(0, 200)}"`);
      expect(!/no loops yet\./.test(text), `/loops says the factory has no loops: "${text.slice(0, 200)}"`);
      const goal = loops[0].goal_text.split('\n')[0];
      expect(text.includes(goal), `/loops does not list the world's loop "${goal}": "${text.slice(0, 200)}"`);
    });
    await visit('/scorecard', async (page) => {
      const main = page.locator('main').first();
      await main.locator('section[aria-label="Leverage"]').waitFor({ state: 'visible', timeout: 10_000 });
      const text = ((await main.innerText()) ?? '').replace(/\s+/g, ' ');
      expect(!/offline · no data|not served/.test(text), `/scorecard reads as offline: "${text.slice(0, 200)}"`);
      expect(text.includes(scorecard.rows[0].model), `/scorecard names no ${scorecard.rows[0].model} row`);
      expect(text.includes(`${outcomes.weeks.length}w · main at`), `/scorecard draws no weekly outcomes`);
    });
  });

  for (const size of [{ width: WIDTH, height: HEIGHT }, PHONE]) {
    await check(`demo world: the header counts ${manifest.needs_you} needs-you items at ${size.width}px`, async () => {
      await visit(
        '/work',
        async (page) => {
          const count = page.locator('header [data-needs-you-count]').first();
          await count.waitFor({ state: 'visible', timeout: 10_000 });
          const said = ((await count.textContent()) ?? '').trim();
          expect(said === String(manifest.needs_you), `the header counts "${said}"`);
        },
        size
      );
    });

    await check(`demo world: the refused PR's work page shows its failed verify and send-back at ${size.width}px`, async () => {
      await visit(
        `/work?item=${encodeURIComponent(manifest.refused_item)}`,
        async (page) => {
          const section = page.locator('section[aria-label="Merge verify"]');
          await section.waitFor({ state: 'visible', timeout: 15_000 });
          const text = ((await section.textContent()) ?? '').replace(/\s+/g, ' ');
          const undeclared = manifest.held_out_failed.length - manifest.held_out_declared.length;
          const sentence = `Merge verify failed at ${manifest.refused_head}: ${undeclared} of main's tests fail on this PR.`;
          expect(text.includes(sentence), `the verify reads "${text.trim()}"`);
          const ids = await section.locator('[data-held-out-test]').evaluateAll((els) => els.map((el) => (el.textContent ?? '').trim()));
          expect(
            JSON.stringify([...ids].sort()) === JSON.stringify([...manifest.held_out_failed].sort()),
            `the failing tests shown are ${JSON.stringify(ids)}`
          );
          const main = ((await page.locator('main').first().innerText()) ?? '').replace(/\s+/g, ' ');
          // The failed verify holds the merge and the READY stands, so the PR
          // waits on a person, as it does in kew's drive (#3148).
          const merge = await stageCaption(page, 'merge');
          expect(merge === 'needs you · merge decision', `the refused PR's merge cell reads "${merge}"`);
          expect(/round 2/i.test(main), 'the work page does not show the second review round');
          const wide = await page.evaluate(() => [document.documentElement.scrollWidth, document.documentElement.clientWidth]);
          expect(wide[0] <= wide[1], `${size.width}px scrolls sideways (${wide[0]} over ${wide[1]})`);
        },
        size
      );
    });
  }

  await check('demo world: the epic draws its blocked node and the node waiting on a person', async () => {
    await visit(`/epics/${encodeURIComponent(manifest.epic_ref)}`, async (page) => {
      const card = (id) => page.locator(`.dag .svelte-flow__node[data-id="${id}"]`);
      await card(manifest.blocked_item).waitFor({ state: 'visible', timeout: 15_000 });
      const word = ((await card(manifest.blocked_item).locator('[data-status-word]').first().textContent()) ?? '').trim();
      expect(/blocked/i.test(word), `the blocked node's status reads "${word}"`);
      const waiting = card(manifest.awaiting_merge_item);
      await waiting.waitFor({ state: 'visible', timeout: 5_000 });
      expect(
        (await waiting.locator('[data-needs-you-row]').count()) === 1,
        `node ${manifest.awaiting_merge_item} is not drawn as waiting on a person`
      );
    });
  });

  await check('demo world: spend on the epic, its node and the budget page agree', async () => {
    const epicUsd = epic.economics.drain_total_usd;
    const nodeUsd = epic.economics.node_drain_usd[manifest.refused_item];
    expect(epicUsd === manifest.spend.epic_usd && spend.weekly.spent === epicUsd, 'the world files disagree on the spend');
    await visit('/epics', async (page) => {
      const text = ((await page.locator('main').first().innerText()) ?? '').replace(/\s+/g, ' ');
      expect(text.includes(manifest.epic_ref) && text.includes(usd(epicUsd)), `the epics list does not read ${usd(epicUsd)}`);
    });
    await visit(`/epics/${encodeURIComponent(manifest.epic_ref)}`, async (page) => {
      const drain = page.locator(`.dag .svelte-flow__node[data-id="${manifest.refused_item}"] .drain`);
      await drain.waitFor({ state: 'visible', timeout: 15_000 });
      const said = ((await drain.textContent()) ?? '').trim();
      expect(said === usd(nodeUsd), `node ${manifest.refused_item} reads ${said}, not ${usd(nodeUsd)}`);
    });
    await visit('/budget', async (page) => {
      const text = ((await page.locator('main').first().innerText()) ?? '').replace(/\s+/g, ' ');
      expect(text.includes(usd(spend.weekly.spent)), `the budget page does not read the week's ${usd(spend.weekly.spent)}`);
      expect(text.includes(usd(spend.daily.spent)), `the budget page does not read the day's ${usd(spend.daily.spent)}`);
    });
  });

  // Design review c9, D7: the spend tour's run is a merged implementation run
  // whose item passed review; its page says so, as the epic's rail does.
  await check("demo world: the spend tour's run reads merged, its item's READY verdict and 4/4 checks match", async () => {
    const runId = /^\/runs\/([^/]+)$/.exec(JSON.parse(readFileSync(new URL('demo/tours/where-did-the-money-go.json', FRONTEND), 'utf8')).start)?.[1];
    expect(runId !== undefined, 'the spend tour does not start on a run page');
    const run = read(`runs-${runId}.json`);
    expect(run.pr_state === 'merged', `run ${runId}'s pull request is ${run.pr_state}, not merged`);
    await visit(`/runs/${runId}`, async (page) => {
      const verdict = page.locator('section[aria-label="Review and route"] [data-tour="review-verdict"] [data-status-word]');
      await verdict.waitFor({ state: 'visible', timeout: 10_000 });
      const word = ((await verdict.textContent()) ?? '').trim();
      expect(word === 'READY', `run ${runId}'s page reads the verdict "${word}", not READY`);
      const text = ((await page.locator('main').first().innerText()) ?? '').replace(/\s+/g, ' ');
      expect(/\b4\/4 checks match\b/.test(text), `run ${runId}'s page does not read "4/4 checks match"`);
      expect(/pull request · merged/.test(text), `run ${runId}'s page does not read "pull request · merged"`);
    });
  });

  // Design review c9, D15: the Epics list counts the epic's changes off its
  // change strip, which the world answers with the journal's changes; it read
  // one change beside the epic header's 28 when it was served the plain strip.
  await check("demo world: the Epics list counts the epic's journal changes and draws them in its strip", async () => {
    const changes = read(`epics-${manifest.epic_ref}-tick-strip-changes.json`);
    expect(changes.length === epic.heartbeat.change_count, `the change strip holds ${changes.length} changes, the heartbeat ${epic.heartbeat.change_count}`);
    await visit('/epics', async (page) => {
      const cell = page.locator('[data-change-count]').first();
      await page.waitForFunction(() => (document.querySelector('[data-change-count]')?.getAttribute('data-change-count') ?? '') !== '', null, { timeout: 10_000 });
      const count = Number(await cell.getAttribute('data-change-count'));
      expect(count > 1 && count <= epic.heartbeat.change_count, `the Epics list counts ${count} changes; the journal has ${epic.heartbeat.change_count}`);
    }, { width: WIDTH, height: HEIGHT });
  });
}
