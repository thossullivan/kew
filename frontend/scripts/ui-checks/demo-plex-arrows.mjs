/**
 * The demo build draws → ↗ ← in IBM Plex under demo.kew.dev's CSP (#3478).
 *
 * A demo module (`demo-*.mjs`): `scripts/ui-check.mjs` runs it only against
 * the static server for `build-demo/`, which sends `www/Caddyfile`'s CSP, whose
 * `font-src 'self'` refuses a `data:` font. Each Plex arrow subset in
 * `src/lib/fonts/` is under Vite's 4 KB inline limit, so unless both Vite
 * configs keep a woff2 a file, the build pastes it into the CSS as a data: URL,
 * the browser blocks it with a console error, and the arrows fall back to a
 * system font. `plex-arrows` checks the same fonts on the dev server, which
 * inlines nothing and sends no CSP.
 *
 * Negative control: with `assetsInlineLimit` removed from
 * `demo/vite.demo.config.ts`, this module fails: the CSP refuses the inlined
 * fonts, so the font probe's `document.fonts.load` rejects with a network
 * error before any of the per-arrow readings print.
 */
// ui-check-owns: src/lib/fonts/**
// ui-check-owns: demo/vite.demo.config.ts
import { PLEX_ARROWS, plexArrowProbes } from '../ui-check-lib.mjs';

/** Every `@font-face` source the page's stylesheets declare for IBM Plex. */
function plexSources(page) {
  return page.evaluate(() =>
    [...document.styleSheets]
      .flatMap((sheet) => [...sheet.cssRules])
      .filter((rule) => rule instanceof CSSFontFaceRule)
      .filter((rule) => rule.style.getPropertyValue('font-family').includes('IBM Plex'))
      .map((rule) => rule.style.getPropertyValue('src'))
  );
}

/** The arrow subsets the page fetched, as resource URLs. */
function fetchedSubsets(page) {
  return page.evaluate(() =>
    performance
      .getEntriesByType('resource')
      .map((entry) => entry.name)
      .filter((url) => /ibm-plex-(mono|sans)-arrows-\d00-normal[^/]*\.woff2$/.test(new URL(url).pathname))
  );
}

export async function checks({ base, check, visit, expect }) {
  await check('demo: → ↗ ← are drawn in IBM Plex from font files the CSP allows', () =>
    visit('/work', async (page, problems) => {
      for (const { name, token, weight, why } of await plexArrowProbes(page)) {
        expect(why === null, `${PLEX_ARROWS} in var(${token}) (${name}) at ${weight} is drawn in ${why}`);
      }
      const sources = await plexSources(page);
      expect(sources.length > 0, 'the demo declares no IBM Plex @font-face');
      const inlined = sources.filter((src) => src.includes('data:'));
      expect(inlined.length === 0, `the demo inlines ${inlined.length} IBM Plex font(s) as data: URLs`);
      const fetched = await fetchedSubsets(page);
      expect(fetched.length > 0, 'the demo fetched no Plex arrow subset as a file');
      const foreign = fetched.filter((url) => new URL(url).origin !== new URL(base).origin);
      expect(foreign.length === 0, `the arrow subsets came from another origin: ${foreign.join(', ')}`);
      expect(problems.length === 0, `the page reported:\n  ${problems.join('\n  ')}`);
    })
  );
}
