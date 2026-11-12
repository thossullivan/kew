/**
 * The run page on a demo run (#3459), on the static demo build.
 *
 * A demo module (`demo-*.mjs`): `scripts/ui-check.mjs` runs it only against the
 * static server for `build-demo/`, and `npm run demo-check` against any demo
 * base. The run is the one the spend tour starts on (`/runs/1b464391`, branch
 * `agent/issue-104` — the value the 390 review found cut in half).
 *
 * At 390 and 1440, with every disclosure opened, on the demo run served with
 * values long enough to overflow a phone row (the demo's own fit, so a cut
 * would go unseen): every block is at least 300px wide, every value is drawn
 * whole or titled with the whole of it, every token figure names its source,
 * nothing scrolls sideways, and every control is at least 32px tall. Review
 * and route sits beside Cost at 1440 and above it at 390. On the demo's own
 * values, the branch row reads `agent/issue-104` whole.
 */
// ui-check-owns: src/routes/(factory)/runs/[id]/**
// ui-check-owns: src/lib/flight-recorder.ts
import { runPageLayout } from '../ui-check-lib.mjs';

const RUN = '/runs/1b464391';
/** Run-row values wider than a 390 fact row: each must wrap, or be titled whole. */
const LONG = {
  branch: 'agent/issue-2055-review-gate-lows-comment-and-hold-back-x',
  provider: 'anthropic-bedrock-us-east-1-provisioned',
  reported_model: 'claude-opus-5-20261001-experimental-long-id'
};

/**
 * Lay `over` on the demo's answer for the run row. The demo build answers
 * `/api` inside the page (`installDemo` replaces `window.fetch`), so no
 * `page.route` sees the read: this wraps whichever fetch the page installs,
 * at the same boundary, before any of its scripts run.
 */
function runRow(over) {
  return (page) =>
    page.addInitScript(
      ({ path, over }) => {
        const wrap = (inner) => async (input, init) => {
          const response = await inner(input, init);
          const href = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
          if (new URL(href, location.href).pathname !== path || !response.ok) return response;
          const body = { ...(await response.json()), ...over };
          return new Response(JSON.stringify(body), {
            status: response.status,
            headers: { 'content-type': 'application/json' }
          });
        };
        let current = wrap(window.fetch.bind(window));
        Object.defineProperty(window, 'fetch', {
          configurable: true,
          get: () => current,
          set: (next) => {
            current = wrap(next);
          }
        });
      },
      { path: `/api${RUN}`, over }
    );
}

const branchRow = (page) =>
  page.evaluate(() => {
    const dt = [...document.querySelectorAll('main dt')].find((el) => el.textContent?.trim() === 'branch');
    return (dt?.nextElementSibling?.textContent ?? '').trim();
  });

export async function checks({ check, visit, expect }) {
  for (const [label, size] of [
    ['390', { width: 390, height: 844 }],
    ['1440', undefined]
  ]) {
    await check(`demo ${RUN} is one readable column at 390 and two at 1440 (${label}px)`, () =>
      visit(
        RUN,
        async (page) => {
          const read = await runPageLayout(page);
          expect(read.narrow.length === 0, `blocks under 300px wide: ${read.narrow.join(', ')}`);
          expect(read.cut.length === 0, `values cut with no readable full value: ${read.cut.join(', ')}`);
          expect(!read.sideways, 'the page scrolls sideways');
          expect(read.short.length === 0, `controls under 32px: ${read.short.join(', ')}`);
          const [review, cost] = read.columns;
          const beside = review !== undefined && cost !== undefined && cost.left > review.left + 1;
          expect(read.columns.length === 2, `the review and cost grid has ${read.columns.length} blocks, not 2`);
          expect(size ? !beside : beside, `review and cost are ${beside ? 'side by side' : 'stacked'} at ${label}px`);
          expect(
            read.hostTokens.length === 4,
            `the cost table has ${read.hostTokens.length} host-metered token rows, not 4`
          );
          const unsourced = read.tokens.filter((t) => t.source === '');
          expect(unsourced.length === 0, `token figures with no source: ${unsourced.map((t) => t.figure).join(', ')}`);
          // The long values were served: a check that measured the demo's
          // own short ones could not fail.
          const branch = await branchRow(page);
          expect(branch === LONG.branch, `the branch row reads "${branch}", not the long branch served`);
        },
        size,
        runRow(LONG)
      )
    );
  }

  await check(`demo ${RUN} reads its own branch whole at 390`, () =>
    visit(
      RUN,
      async (page) => {
        const read = await runPageLayout(page);
        expect(read.cut.length === 0, `values cut with no readable full value: ${read.cut.join(', ')}`);
        const branch = await branchRow(page);
        expect(branch === 'agent/issue-104', `the branch row reads "${branch}", not agent/issue-104`);
      },
      { width: 390, height: 844 }
    )
  );
}
