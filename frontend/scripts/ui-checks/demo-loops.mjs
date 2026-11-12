/**
 * The demo's /loops page shows a loop (#3461, design review c9 D23).
 *
 * A demo module (`demo-*.mjs`): `scripts/ui-check.mjs` runs it only against
 * the static server for `build-demo/`. The demo world held no loop, so
 * `/loops` said "no loops yet." and the surface was never seen. The world now
 * holds one (`loopOf` in `demo/world/generate.mjs`): an objective, four
 * iterations and an achieved gate. The page must list it, open it, and draw
 * its objective and its terminal state, at 390, 768 and 1440 px with no
 * sideways scroll and nothing reported.
 */
// ui-check-owns: src/routes/(factory)/loops/**
// ui-check-owns: demo/world/loops*.json

const SIZES = [
  { width: 390, height: 844 },
  { width: 768, height: 1024 },
  { width: 1440, height: 960 }
];

/** The world's loop, by the words its objective opens with (lowercased). */
const GOAL = '#98: round half to even';

export async function checks({ check, visit, expect, sleep }) {
  for (const size of SIZES) {
    await check(`demo: /loops lists and opens the world's achieved loop at ${size.width}px`, () =>
      visit(
        '/loops',
        async (page, problems) => {
          const read = () => page.evaluate(() => document.body.innerText.replace(/\s+/g, ' ').toLowerCase());
          let text = await read();
          for (let waited = 0; !(text.includes('gate passed') && text.includes('#98')) && waited < 15_000; waited += 250) {
            await sleep(250);
            text = await read();
          }
          expect(!text.includes('no loops yet'), `${size.width}px: /loops still reads "no loops yet."`);
          expect(!text.includes('start kew serve'), `${size.width}px: /loops reads "start kew serve"`);
          expect(text.includes(GOAL), `${size.width}px: /loops draws no objective for #98`);
          expect(text.includes('achieved'), `${size.width}px: /loops does not name the loop's achieved state`);
          expect(text.includes('gate passed'), `${size.width}px: /loops does not draw the passed gate`);
          const over = await page.evaluate(
            () => document.documentElement.scrollWidth - document.documentElement.clientWidth
          );
          expect(over <= 0, `${size.width}px: /loops scrolls ${over}px sideways`);
          expect(problems.length === 0, `${size.width}px: /loops reported:\n${problems.join('\n')}`);
        },
        size
      )
    );
  }
}
