/**
 * The console draws → ↗ ← in IBM Plex on every platform (#3478).
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 *
 * Fontsource's Plex faces carry no ←, → or ↗, so the browser drew them from a
 * platform fallback, and the epic popover's links row measured 288px on macOS
 * and 309px on the Linux verify host (#3460). `src/lib/fonts/` now ships a Plex
 * subset holding the arrows (`scripts/plex-arrows.sh` rebuilds it), declared in
 * `src/app.css` under the same family names. `demo-plex-arrows` checks the same
 * on the demo build, under the site's CSP.
 *
 * The check asks the browser which fonts drew the text, through the DevTools
 * protocol's `CSS.getPlatformFontsForNode` (`drawnFonts` in
 * `ui-check-lib.mjs`): every font that drew an arrow must be a web font whose
 * family is IBM Plex. Nothing here reads CSS or a font file as text, and no
 * width is guessed at.
 *
 * Negative control: with the six `@font-face` rules for the subset removed from
 * `src/app.css`, both checks fail, naming the system font that drew the arrows
 * (`isCustomFont: false`).
 */
// ui-check-owns: src/app.css
// ui-check-owns: src/lib/fonts/**
import { EPIC, PLEX_ARROWS, drawnFonts, notPlex, plexArrowProbes } from '../ui-check-lib.mjs';

/** A node of the fixture epic whose popover draws both an internal link (→)
 * and its pull request (↗). */
const NODE = '2055';

export async function checks({ check, visit, expect, sleep }) {
  await check('→ ↗ ← are drawn in IBM Plex Mono and IBM Plex Sans at 400, 500 and 600', () =>
    visit('/work', async (page) => {
      for (const { name, token, weight, why } of await plexArrowProbes(page)) {
        expect(why === null, `${PLEX_ARROWS} in var(${token}) (${name}) at ${weight} is drawn in ${why}`);
      }
    })
  );

  await check("the epic popover's link arrows are drawn in IBM Plex", () =>
    visit(`${EPIC}?node=${NODE}`, async (page) => {
      await page.locator('[role="dialog"] .links').first().waitFor({ state: 'visible', timeout: 15_000 });
      const marks = await page.evaluate(async () => {
        await document.fonts.ready;
        const spans = [...document.querySelectorAll('[role="dialog"] .links span[aria-hidden="true"]')];
        return spans.map((span, i) => {
          span.dataset.plexProbe = `link-${i}`;
          return { key: `link-${i}`, text: span.textContent.trim() };
        });
      });
      for (const arrow of ['→', '↗']) {
        expect(
          marks.some((m) => m.text === arrow),
          `the popover on ?node=${NODE} draws no ${arrow} (its marks: ${marks.map((m) => m.text).join(' ')})`
        );
      }
      await sleep(100);
      const fonts = await drawnFonts(
        page,
        marks.map((m) => m.key)
      );
      for (const { key, text } of marks) {
        const why = notPlex(fonts[key]);
        expect(why === null, `the popover's ${text} is drawn in ${why}`);
      }
    })
  );
}
