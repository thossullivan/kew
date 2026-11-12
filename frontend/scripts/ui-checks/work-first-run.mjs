/**
 * The first-run card (#2515, #2652).
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 */
// ui-check-owns: src/routes/(factory)/work/+page.svelte
// ui-check-owns: src/lib/work/FirstRunCard.svelte
// ui-check-owns: src/lib/work/first-run.ts
import { readFileSync } from 'node:fs';
import { FRONTEND, SETTLE_MS, widestBox } from '../ui-check-lib.mjs';

export async function checks({ browser, base, check, expect, sleep, sizes: { WIDTH, HEIGHT } }) {
  /**
   * #2515 — the first-run card, over the committed `scenarios/*-first-run.json`
   * set the `work-first-run` gallery shot photographs: no work, no runs, and a
   * failing `allowlist-network` check. At both widths the card is drawn (below
   * 1024 it takes the list's column), it lists that check with its own fix
   * command, exactly one Copy button is the magenta next move, and the page
   * does not scroll sideways.
   */
  const FIRST_RUN = {
    '/api/dashboard': 'dashboard-first-run.json',
    '/api/dashboard/work': 'dashboard-work-first-run.json',
    '/api/runs': 'runs-first-run.json',
    '/api/readiness': 'readiness-first-run.json'
  };
  /**
   * The first-run set at `size`, with `readiness` (a body) swapped in when
   * given, and `dashboard` (a function of the committed payload) when given.
   */
  async function firstRunPage(size, readiness, assert, dashboard = null) {
    const ctx = await browser.newContext({ viewport: size });
    const page = await ctx.newPage();
    const problems = [];
    page.on('pageerror', (error) => problems.push(`page error: ${error.message}`));
    page.on('console', (message) => {
      if (message.type() === 'error') problems.push(`console error: ${message.text()}`);
    });
    try {
      for (const [path, file] of Object.entries(FIRST_RUN)) {
        const fixture = readFileSync(new URL(`fixtures/scenarios/${file}`, FRONTEND), 'utf8');
        const body =
          path === '/api/readiness' && readiness
            ? readiness
            : path === '/api/dashboard' && dashboard
              ? JSON.stringify(dashboard(JSON.parse(fixture)))
              : fixture;
        await page.route(
          (url) => url.pathname === path,
          (route) => route.fulfill({ status: 200, contentType: 'application/json', body })
        );
      }
      await page.goto(`${base}/work`, { waitUntil: 'load' });
      const card = page.locator('section[aria-label="First run"]');
      await card.waitFor({ state: 'visible', timeout: 15_000 });
      await page
        .locator('ul[aria-label="Readiness checks to fix"]')
        .waitFor({ state: 'visible', timeout: 15_000 });
      await sleep(SETTLE_MS);
      await assert(page, card);
      const wide = await widestBox(page);
      expect(wide.w <= wide.c, `the page scrolls sideways: ${wide.w} > ${wide.c} (${wide.culprit})`);
      expect(problems.length === 0, problems.join('\n'));
    } finally {
      await ctx.close();
    }
  }
  for (const size of [{ width: WIDTH, height: HEIGHT }, { width: 390, height: 844 }]) {
    await check(`the first-run card shows the failing check at ${size.width}px`, () =>
      firstRunPage(size, null, async (page, card) => {
        const text = (await card.textContent()) ?? '';
        expect(text.includes('allowlist-network'), `the card does not name the check:\n${text}`);
        const fix = 'docker network create --driver bridge kew-allowlist';
        expect(
          (await card.locator('code').filter({ hasText: fix }).count()) === 1,
          `the card does not carry the fix command "${fix}"`
        );
        const primary = card.locator('button[aria-label="Copy command (c)"]');
        expect((await primary.count()) === 1, 'the card has no single magenta next move');
        const near = await primary.evaluate((el) => el.parentElement?.textContent ?? '');
        expect(near.includes(fix), `the magenta Copy is not on the fix: ${near}`);
        const box = await primary.boundingBox();
        expect(box !== null && box.height >= 31.5, `the Copy button is ${box?.height}px tall`);
      })
    );
  }

  /**
   * #2652 — `kew serve` before `kew init`: the same set with `project-config`
   * failing as `kew doctor` reports it. The card's one magenta Copy is
   * `kew init`, written once, and `kew setup` is not taught over it. #2776 —
   * `kew init` is also the first command in DOM order: the set's failing
   * `allowlist-network` (and a `model-eol` warning added here) wait behind a
   * closed disclosure after it, which names their count.
   */
  const noConfig = JSON.parse(
    readFileSync(new URL('fixtures/scenarios/readiness-first-run.json', FRONTEND), 'utf8')
  );
  noConfig.checks = noConfig.checks.map((c) =>
    c.id === 'project-config'
      ? {
          ...c,
          status: 'fail',
          summary: 'no kew.toml found; running on built-in defaults',
          hint: "kew init writes this project's kew.toml from detection and flags",
          fix: 'kew init'
        }
      : c.id === 'model-eol'
        ? { ...c, status: 'warn', summary: 'a pinned model retires soon', fix: 'kew model-eol refresh' }
        : c
  );
  noConfig.ok = false;
  for (const size of [{ width: WIDTH, height: HEIGHT }, { width: 390, height: 844 }]) {
    await check(`the first-run card teaches kew init with no config at ${size.width}px`, () =>
      firstRunPage(size, JSON.stringify(noConfig), async (page, card) => {
        const text = (await card.textContent()) ?? '';
        expect(text.includes('project-config'), `the card does not name the check:\n${text}`);
        const primary = card.locator('button[aria-label="Copy command (c)"]');
        expect((await primary.count()) === 1, 'the card has no single magenta next move');
        const near = await primary.evaluate((el) => el.parentElement?.textContent ?? '');
        expect(near.includes('kew init'), `the magenta Copy is not on kew init: ${near}`);
        expect(
          (await card.locator('code').filter({ hasText: /^kew init$/ }).count()) === 1,
          'kew init is not written exactly once'
        );
        expect(
          (await card.locator('code').filter({ hasText: /^kew setup$/ }).count()) === 0,
          'the card still teaches kew setup over kew init'
        );
        const commands = await card.locator('code').allTextContents();
        expect(commands[0] === 'kew init', `kew init is not the first command: ${commands.join(' | ')}`);
        const toggle = card.locator('button[aria-controls="first-run-deferred"]');
        expect((await toggle.count()) === 1, 'the card has no single disclosure for the other checks');
        expect((await toggle.getAttribute('aria-expanded')) === 'false', 'the disclosure starts open');
        const label = (await toggle.textContent()) ?? '';
        expect(
          label.includes('2 more checks read built-in defaults'),
          `the disclosure does not name the count: ${label}`
        );
        const fix = 'docker network create --driver bridge kew-allowlist';
        const deferred = card.locator('#first-run-deferred code').filter({ hasText: fix });
        expect((await deferred.count()) === 1, `the deferred fix "${fix}" is not behind the disclosure`);
        expect(!(await deferred.isVisible()), 'a deferred fix is visible before the disclosure opens');
        await toggle.click();
        expect(await deferred.isVisible(), 'opening the disclosure does not show the deferred fix');
        expect((await primary.count()) === 1, 'opening the disclosure adds a second magenta Copy');
        await primary.scrollIntoViewIfNeeded();
        expect(await primary.isVisible(), 'the magenta Copy is not visible');
      })
    );
  }

  /**
   * #3467 D28 — no visible command is a template. THEN printed "kew dispatch
   * <issue>", a line a terminal refuses. With an open issue served, THEN is
   * that issue's dispatch with a quiet Copy; with none, a sentence. Negative
   * control: on the old card the `<issue>` code block is visible in both.
   */
  const visibleCommands = (card) =>
    card
      .locator('code')
      .evaluateAll((els) =>
        els.filter((el) => el.getClientRects().length > 0).map((el) => (el.textContent ?? '').trim())
      );
  const withCandidate = (payload) => ({
    ...payload,
    glance: { ...payload.glance, queue_candidates: [{ issue_num: 41, title: 'Add a contributing guide' }] }
  });
  for (const size of [{ width: WIDTH, height: HEIGHT }, { width: 768, height: 1024 }, { width: 390, height: 844 }]) {
    await check(`the first-run card prints no template command at ${size.width}px`, () =>
      firstRunPage(size, null, async (page, card) => {
        const commands = await visibleCommands(card);
        expect(commands.length > 0, 'the card shows no command at all');
        const template = commands.filter((c) => /[<>]/.test(c));
        expect(template.length === 0, `the card shows template command(s): ${template.join(' | ')}`);
        expect(
          !commands.some((c) => c.startsWith('kew dispatch')),
          `with no open issue served the card still prints a dispatch: ${commands.join(' | ')}`
        );
        const text = (await card.textContent()) ?? '';
        expect(text.includes('dispatch a first issue'), `the card says nothing of the first dispatch:\n${text}`);
      })
    );
    await check(`the first-run card's THEN dispatches the served open issue at ${size.width}px`, () =>
      firstRunPage(
        size,
        null,
        async (page, card) => {
          const commands = await visibleCommands(card);
          expect(commands.includes('kew dispatch 41'), `THEN is not kew dispatch 41: ${commands.join(' | ')}`);
          expect(!commands.some((c) => /[<>]/.test(c)), `a template command is shown: ${commands.join(' | ')}`);
          const copy = card.locator('button[aria-label="Copy kew dispatch 41"]');
          expect((await copy.count()) === 1, 'the dispatch has no Copy of its own');
          expect((await card.locator('button[aria-label="Copy command (c)"]').count()) === 1, 'the magenta Copy moved');
          const fill = await copy.evaluate((el) => getComputedStyle(el).backgroundColor);
          expect(fill === 'rgba(0, 0, 0, 0)' || fill === 'transparent', `the dispatch's Copy is filled (${fill})`);
          const box = await copy.boundingBox();
          expect(box !== null && box.height >= 31.5, `the dispatch's Copy is ${box?.height}px tall`);
        },
        withCandidate
      )
    );
  }
}
