/**
 * The run page reads top-down and fits a phone (#3459, design review c9 D1).
 *
 * At 390 three blocks were two ~159px columns with no breakpoint, and a fact
 * row cut its value (`agent/issue-2055`) with no title, so on touch it could
 * not be read. The cost block printed host token counts with nothing saying
 * whose they were, and one pricing sentence for every basis. The safest action
 * printed its command as a bare `<code>`, with no way to copy it.
 *
 * At 390, 768 and 1440, with every disclosure opened, on a run served with
 * values long enough to overflow a phone row (a fixture value fits, so a cut
 * would go unseen) and a runner metric with a fifteen-digit value:
 *
 * - every block is at least 300px wide, the page never scrolls sideways, and
 *   every control is at least 32px tall;
 * - the safest action sits above the run's event list;
 * - Review and route sits beside Cost from 1024px and above it below;
 * - every value is drawn whole, or its title holds the whole of it;
 * - every token figure names its source, and no runner metric prints its id;
 *
 * and, on the committed run:
 *
 * - the safest action's command has a Copy control, and pressing it puts the
 *   command on the clipboard, or says it could not;
 * - Details, Evidence, Changes and Compiled prompt arrive shut, each saying in
 *   its summary line what it holds;
 * - a local run's pricing sentence is the local one, and a token count the row
 *   does not carry reads "not recorded", never 0;
 * - with `?attempt=` naming an earlier attempt than the run row's, the header
 *   says which attempt its status and cost are from, and so does each figure
 *   read off the run row, while the attempt's own runner figures do not.
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 */
// ui-check-owns: src/routes/(factory)/runs/[id]/**
// ui-check-owns: src/lib/flight-recorder.ts
// ui-check-owns: src/lib/ui/CopyButton.svelte
import { fetchThrough, runPageLayout } from '../ui-check-lib.mjs';

const RUN = '/runs/f5dcb6a5';
const SIZES = [
  ['390', { width: 390, height: 844 }],
  ['768', { width: 768, height: 1024 }],
  ['1440', undefined]
];
const COMMAND = 'kew finalize f5dcb6a5';
const DISCLOSURES = ['details', 'evidence', 'changes', 'prompt'];
const LOCAL_PRICING = 'a local model: kew meters its tokens and prices them at nothing';
/** Run-row values wider than a 390 fact row: each must wrap, or be titled whole. */
const LONG = {
  branch: 'agent/issue-2055-review-gate-lows-comment-and-hold-back-x',
  provider: 'anthropic-bedrock-us-east-1-provisioned',
  reported_model: 'claude-opus-5-20261001-experimental-long-id'
};

/** Serve the fixture run with `over` laid over its row. */
function runRow(over) {
  return async (page) => {
    await page.route('**/api/runs/f5dcb6a5', async (route) => {
      const response = await fetchThrough(route);
      await route.fulfill({ response, json: { ...(await response.json()), ...over } });
    });
  };
}

/** A runner's own usage figure, as the host sequence records one. */
function usage(seq, metric, value, unit) {
  return {
    seq,
    timestamp: '2026-09-24T15:45:00+00:00',
    kind: 'evidence.runner_usage',
    origin: 'evidence',
    producer_kind: 'runner',
    producer_name: 'claude-code',
    producer_version: '1.0.0',
    attested_by_host: false,
    sealed: false,
    terminal: false,
    facts: { metric, value, unit }
  };
}

/** Serve the fixture flight, for whichever attempt is asked, with `events` added. */
function flightWith(events) {
  return async (page) => {
    await page.route(
      (url) => url.pathname === '/api/runs/f5dcb6a5/flight',
      async (route) => {
        const response = await fetchThrough(route);
        const flight = await response.json();
        await route.fulfill({ response, json: { ...flight, events: [...flight.events, ...events] } });
      }
    );
  };
}

const both = (...prepares) => async (page) => {
  for (const prepare of prepares) await prepare(page);
};

const squash = (s) => (s ?? '').replace(/\s+/g, ' ').trim();

export async function checks({ check, visit, expect }) {
  for (const [label, size] of SIZES) {
    await check(`${RUN} is one readable column at 390 and two from 1024 (${label}px)`, () =>
      visit(
        RUN,
        async (page, problems) => {
          const read = await runPageLayout(page);
          expect(read.narrow.length === 0, `blocks under 300px wide: ${read.narrow.join(', ')}`);
          expect(read.cut.length === 0, `values cut with no readable full value: ${read.cut.join(', ')}`);
          expect(!read.sideways, 'the page scrolls sideways');
          expect(read.short.length === 0, `controls under 32px: ${read.short.join(', ')}`);
          const { safest, events } = read.order;
          expect(
            safest !== null && events !== null && safest < events,
            `the safest action's top (${safest}) is not above the run events' (${events}) at ${label}px`
          );
          expect(read.columns.length === 2, `the review and cost grid has ${read.columns.length} blocks, not 2`);
          const [review, cost] = read.columns;
          const wide = (size?.width ?? 1440) >= 1024;
          const beside = review !== undefined && cost !== undefined && cost.left > review.left + 1;
          expect(
            wide ? beside : !beside,
            `${review?.name} and ${cost?.name} are ${beside ? 'side by side' : 'stacked'} at ${label}px`
          );
          expect(
            read.hostTokens.length === 4,
            `the cost table has ${read.hostTokens.length} host-metered token rows, not 4`
          );
          const unsourced = read.tokens.filter((t) => t.source === '');
          expect(unsourced.length === 0, `token figures with no source: ${unsourced.map((t) => t.figure).join(', ')}`);
          const ids = read.figures.filter((f) => f.figure.includes('_'));
          expect(ids.length === 0, `cost figures printed as ids: ${ids.map((f) => f.figure).join(', ')}`);
          expect(
            read.figures.some((f) => f.figure === 'tokens to first edit'),
            `no "tokens to first edit" row: ${read.figures.map((f) => f.figure).join(', ')}`
          );
          expect(problems.length === 0, `${RUN} reported:\n${problems.join('\n')}`);
        },
        size,
        both(
          runRow(LONG),
          flightWith([
            usage(5, 'tool_uses', 40, 'calls'),
            usage(6, 'tokens_to_first_edit', 52000, 'tokens'),
            usage(7, 'steps', 123456789012345, 'cache_creation_input_tokens')
          ])
        )
      )
    );
  }

  await check(`${RUN} gives the safest action's command a Copy control`, () =>
    visit(
      RUN,
      async (page) => {
        const section = page.locator('section[aria-label="Safest action"]');
        await section.locator('[data-safest-command]').waitFor({ state: 'visible', timeout: 15_000 });
        const copy = section.getByRole('button', { name: /copy/i });
        expect((await copy.count()) === 1, `the safest action draws ${await copy.count()} Copy controls, not 1`);
        if ((await copy.count()) !== 1) return;
        const height = (await copy.boundingBox())?.height ?? 0;
        expect(height >= 32 - 0.5, `the Copy control is ${height.toFixed(1)}px tall`);
        await copy.click();
        // "Copied" flashes only once the clipboard took the command.
        await copy.filter({ hasText: 'Copied' }).waitFor({ timeout: 5_000 });
        const clip = await page.evaluate(() => navigator.clipboard.readText());
        expect(clip === COMMAND, `Copy put "${clip}" on the clipboard, not "${COMMAND}"`);
        expect((await section.locator('[data-copy-failed]').count()) === 0, 'a copy that landed says it failed');
      },
      { width: 390, height: 844 },
      async (page) => {
        await page.context().grantPermissions(['clipboard-read', 'clipboard-write']);
        await runRow({ status: 'awaiting_publication', pr_url: null, pr_state: null })(page);
      }
    )
  );

  await check(`${RUN} says so when Copy cannot reach the clipboard`, () =>
    visit(
      RUN,
      async (page, problems) => {
        const section = page.locator('section[aria-label="Safest action"]');
        await section.locator('[data-safest-command]').waitFor({ state: 'visible', timeout: 15_000 });
        await section.getByRole('button', { name: /copy/i }).click();
        const failed = section.locator('[data-copy-failed]');
        await failed.waitFor({ state: 'visible', timeout: 5_000 });
        const said = squash(await failed.textContent());
        expect(said.startsWith('copy failed'), `a failed copy reads "${said}"`);
        const read = await runPageLayout(page);
        expect(!read.sideways, 'the failed-copy line scrolls the page sideways');
        expect(problems.length === 0, `${RUN} reported:\n${problems.join('\n')}`);
      },
      { width: 390, height: 844 },
      async (page) => {
        // An insecure origin has no clipboard: the browser's boundary, removed.
        await page.addInitScript(() => {
          Object.defineProperty(navigator, 'clipboard', { value: undefined, configurable: true });
        });
        await runRow({ status: 'awaiting_publication', pr_url: null, pr_state: null })(page);
      }
    )
  );

  await check(`${RUN} keeps details, evidence, changes and the prompt behind disclosures that say what they hold`, () =>
    visit(RUN, async (page) => {
      await page.locator('section[aria-label="Cost"]').waitFor({ state: 'visible', timeout: 15_000 });
      for (const id of DISCLOSURES) {
        const block = page.locator(`[data-disclosure="${id}"]`);
        expect((await block.count()) === 1, `no ${id} disclosure`);
        if ((await block.count()) !== 1) continue;
        const toggle = block.locator(':scope > button');
        expect((await toggle.getAttribute('aria-expanded')) === 'false', `${id} does not arrive shut`);
        const summary = squash(await block.locator('[data-disclosure-summary]').textContent());
        expect(summary !== '', `${id} shut says nothing about what it holds`);
        await toggle.click();
        expect((await toggle.getAttribute('aria-expanded')) === 'true', `${id} did not open`);
        expect(await block.locator(`#${id}-body`).isVisible(), `${id} opened onto nothing`);
      }
      const changes = squash(await page.locator('[data-disclosure="changes"] [data-disclosure-summary]').textContent());
      expect(changes === '6 files', `Changes shut reads "${changes}", not "6 files"`);
      const evidence = squash(await page.locator('[data-disclosure="evidence"] [data-disclosure-summary]').textContent());
      expect(
        evidence.startsWith('sealed · chain verified ·'),
        `Evidence shut reads "${evidence}", not the seal and the chain`
      );
      const prompt = squash(await page.locator('[data-disclosure="prompt"] [data-disclosure-summary]').textContent());
      expect(
        /^(prompt recorded\b|no prompt was recorded for this run$)/.test(prompt),
        `Compiled prompt shut reads "${prompt}", not what it holds`
      );
    })
  );

  await check(`${RUN} prices a local run as local and never prints a missing count as 0`, () =>
    visit(
      RUN,
      async (page) => {
        const read = await runPageLayout(page);
        const pricing = squash(await page.locator('[data-cost-pricing]').textContent());
        expect(pricing === LOCAL_PRICING, `a local run's pricing sentence reads "${pricing}"`);
        const input = read.tokens.find((t) => t.figure === 'input tokens');
        expect(input?.value === 'not recorded', `a null input count reads "${input?.value}"`);
        expect(input?.source === 'host-metered', `the input count's source reads "${input?.source}"`);
        const zero = read.tokens.filter((t) => t.value === '0');
        expect(zero.length === 0, `counts printed as 0: ${zero.map((t) => t.figure).join(', ')}`);
      },
      { width: 390, height: 844 },
      runRow({ channel: 'local', cost_usd: 0, input_tokens: null })
    )
  );

  await check(`${RUN}?attempt=1 says which attempt each run-row figure is from`, () =>
    visit(
      `${RUN}?attempt=1`,
      async (page) => {
        const note = page.locator('header [data-row-attempt]');
        await note.waitFor({ state: 'visible', timeout: 15_000 });
        const said = squash(await note.textContent());
        expect(said.includes('attempt 2'), `the header's run-row note reads "${said}"`);
        const read = await runPageLayout(page);

        // No section-wide label: the sections mix the run row with the
        // attempt's own evidence, so each figure says which it is.
        for (const name of ['Review and route', 'Cost']) {
          const head = squash(
            await page
              .locator(`section[aria-label="${name}"]`)
              .evaluate((section) => section.querySelector('h2')?.parentElement?.textContent ?? '')
          );
          expect(!head.includes('attempt'), `the ${name} head reads "${head}", labelling the whole section`);
        }

        const runner = read.figures.filter((f) => f.source.startsWith('runner claim'));
        expect(runner.length === 1, `the cost table has ${runner.length} runner-claim rows, not 1`);
        const mislabelled = runner.filter((f) => f.source.includes('attempt'));
        expect(mislabelled.length === 0, `runner figures labelled as the run row's: ${mislabelled.map((f) => f.source).join(', ')}`);
        const rowFigures = read.figures.filter((f) => /^(host-metered|planned)\b/.test(f.source));
        expect(rowFigures.length === 5, `the cost table has ${rowFigures.length} run-row figures, not 5`);
        const unlabelled = rowFigures.filter((f) => !f.source.endsWith('· attempt 2'));
        expect(unlabelled.length === 0, `run-row figures not labelled attempt 2: ${unlabelled.map((f) => `${f.figure} (${f.source})`).join(', ')}`);

        const labelled = (locator) => locator.locator('[data-row-attempt]').filter({ hasText: 'attempt 2' }).count();
        const cost = page.locator('section[aria-label="Cost"]');
        expect((await labelled(cost.locator('[data-cost-amount]').locator('..'))) === 1, 'the amount does not say attempt 2');
        const review = page.locator('section[aria-label="Review and route"]');
        expect((await labelled(review.locator('[data-tour="review-verdict"]'))) === 1, 'the verdict does not say attempt 2');
        const row = (dt) => review.locator('dl > div').filter({ has: page.locator('dt', { hasText: dt }) });
        expect((await labelled(row('route claimed'))) === 1, 'the route claim does not say attempt 2');
        expect(
          (await row('runner reported').locator('[data-row-attempt]').count()) === 0,
          "the runner's report, this attempt's own evidence, is labelled as the run row's"
        );
      },
      undefined,
      both(runRow({ attempt_number: 2 }), flightWith([usage(5, 'steps', 12, 'steps')]))
    )
  );
}
