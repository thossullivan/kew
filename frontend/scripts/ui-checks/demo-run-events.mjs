/**
 * A demo run's events read as sentences (#3458).
 *
 * A demo module (`demo-*.mjs`): `scripts/ui-check.mjs` runs it only against
 * the static server for `build-demo/`. The run the issue photographed,
 * `1b464391`, printed `{"recommended_runner":"claude-code","applied":false}`
 * at the end of a row; `0d1ef5f0` carries a review verdict as well. Each is
 * read the way `run-events.mjs` reads the fixture run: no JSON and no seq on
 * a closed row, every stage named, every preset counted. `9a8eb37d` failed:
 * its completion records an error_category, as the backend writes one, and
 * that row is drawn as a failure.
 */
// ui-check-owns: src/lib/runs/RunEvents.svelte
// ui-check-owns: src/lib/runs/event-words.ts
import { readRunEvents, sentenceProblems } from './run-events.mjs';

const RUNS = ['1b464391', '0d1ef5f0', '9a8eb37d'];

/** A demo run that failed: its completion row must read, and be drawn, as a failure. */
const FAILED = '9a8eb37d';
const SIZES = [
  ['390', { width: 390, height: 844 }],
  ['1440', undefined]
];

export async function checks({ check, visit, expect }) {
  for (const run of RUNS) {
    for (const [label, size] of SIZES) {
      await check(`demo: /runs/${run} events read as sentences under named stages at ${label}px`, () =>
        visit(
          `/runs/${run}`,
          async (page, problems) => {
            const read = await readRunEvents(page);
            const found = sentenceProblems(read);
            expect(found.length === 0, found.join('\n'));
            const routed = read.rows.find((row) => row.text.includes('Route chosen'));
            expect(
              !routed || routed.text.includes('claude-code recommended, not applied'),
              `the route row does not say what was chosen: "${routed?.text}"`
            );
            expect(problems.length === 0, `/runs/${run} reported:\n${problems.join('\n')}`);
          },
          size
        )
      );
    }
  }

  await check(`demo: /runs/${FAILED} draws its failed completion red`, () =>
    visit(`/runs/${FAILED}`, async (page) => {
      await readRunEvents(page);
      const read = await page.evaluate(() => {
        const probe = document.createElement('span');
        probe.style.color = 'var(--danger)';
        document.body.appendChild(probe);
        const danger = getComputedStyle(probe).color;
        probe.remove();
        const label = [...document.querySelectorAll('[aria-label="Ledger events"] [data-event-label]')].find(
          (el) => el.getAttribute('title') === 'dispatch_completed'
        );
        const row = label?.closest('[data-seq]');
        if (!row) return null;
        const text = row.innerText.replace(/\s+/g, ' ').trim();
        return { tone: row.dataset.tone, colour: getComputedStyle(label).color, danger, text };
      });
      expect(read !== null, `/runs/${FAILED} draws no completion row`);
      expect(read.tone === 'failed', `the failed run's completion reads ${read.tone}: "${read.text}"`);
      expect(read.colour === read.danger, `the failed completion is drawn ${read.colour}, not the danger red ${read.danger}`);
      expect(read.text.includes('failed'), `the failed completion does not say it failed: "${read.text}"`);
    })
  );
}
