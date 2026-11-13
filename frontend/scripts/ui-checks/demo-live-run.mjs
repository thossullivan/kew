/**
 * The demo's live run stops repeating itself (#3461, C11).
 *
 * A demo module (`demo-*.mjs`): `scripts/ui-check.mjs` runs it only against
 * the static server for `build-demo/`. The world's pending run, `ab4656b0`,
 * has no `finished_at`, so its page polls `/api/runs/{id}/audit?after_seq=N`
 * every second. The shim answered every poll with the whole file, whatever
 * `after_seq` said, so each poll handed the page the run again and its event
 * list rebuilt and jumped to its foot each second. The shim now pages forward
 * from the cursor (`runAuditPage` in `fixtures/core.ts`).
 *
 * The shim answers inside the page, so no poll reaches the network: the check
 * wraps the page's `fetch` (the shim's own) once the events are drawn, and
 * watches the page's own polls for four seconds. There must be more
 * than one (or the run stopped being live and this proves nothing), no poll
 * may return an event an earlier one returned, and the list's first row must
 * be the same element at the end as at the start.
 */
// ui-check-owns: src/lib/demo/respond.ts
// ui-check-owns: fixtures/core.ts
// ui-check-owns: demo/world/runs-ab4656b0*.json
import { readRunEvents } from './run-events.mjs';

const RUN = 'ab4656b0';
const SIZES = [
  { width: 390, height: 844 },
  { width: 1440, height: 960 }
];

export async function checks({ check, visit, expect, sleep }) {
  for (const size of SIZES) {
    await check(`demo: /runs/${RUN}'s polls return no event already shown, at ${size.width}px`, () =>
      visit(
        `/runs/${RUN}`,
        async (page, problems) => {
          // The rows the first read drew, before any poll is watched.
          const first = await readRunEvents(page);
          await page.evaluate((run) => {
            const row = document.querySelector('[aria-label="Ledger events"] [data-seq]');
            if (row) row.setAttribute('data-kew-first-row', '');
            const shim = globalThis.fetch;
            globalThis.__kewPolls = [];
            globalThis.fetch = async (input, init) => {
              const response = await shim(input, init);
              const url = new URL(typeof input === 'string' ? input : input.url, location.href);
              if (url.pathname === `/api/runs/${run}/audit`) {
                const body = await response.clone().json().catch(() => null);
                globalThis.__kewPolls.push({
                  after: url.searchParams.get('after_seq'),
                  seqs: (body?.events ?? []).map((e) => e.seq)
                });
              }
              return response;
            };
          }, RUN);
          await sleep(4_000);
          const polls = [
            { after: '0', seqs: first.rows.map((row) => Number(row.seq)) },
            ...(await page.evaluate(() => globalThis.__kewPolls))
          ];
          const after = polls.slice(1);
          expect(after.length > 1, `the page polled ${after.length} time(s) in 4s: the run is not live, so this proves nothing`);
          const seen = new Set();
          for (const poll of polls) {
            for (const seq of poll.seqs) {
              expect(!seen.has(seq), `poll after_seq=${poll.after} returned seq ${seq}, which an earlier poll had shown`);
              seen.add(seq);
            }
          }
          const kept = await page.evaluate(
            () => document.querySelector('[aria-label="Ledger events"] [data-seq]')?.hasAttribute('data-kew-first-row') ?? false
          );
          expect(kept, `/runs/${RUN}'s event list was rebuilt while the page polled`);
          expect(problems.length === 0, `/runs/${RUN} reported:\n${problems.join('\n')}`);
        },
        size
      )
    );
  }
}
