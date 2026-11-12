/**
 * `npm run build:demo` — the console as a static site on fixtures (#3139).
 *
 * The console is already a pure SPA (`ssr = false`, adapter-static with an
 * `index.html` fallback, no `paths.base`), so the demo is the same app with a
 * different data layer: `__KEW_DEMO__` is `true` here, which makes
 * `src/hooks.client.ts` install the shim in `src/lib/demo/` before the first
 * `load` runs. Every `/api` read is then answered in the browser out of the
 * fixture set bundled below, and every write with a sentence saying what it
 * would have done. The guided tours are bundled here too, and only here. The
 * build history each surface shows (#3144) ships beside them as one checked
 * file (`provenance-bundle.ts`). `vite.config.ts` defines the same flag as `false`, so the
 * normal build tree-shakes every demo module out.
 *
 * The output goes to `build-demo/`, never `build/`: `svelte.config.js` reads
 * `KEW_DEMO_BUILD`, which is set here before the SvelteKit plugin loads that
 * file (and by the npm script, for anything that loads it earlier).
 */
import { sveltekit } from '@sveltejs/kit/vite';
import tailwindcss from '@tailwindcss/vite';
import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vite';
import { demoFixtures } from './fixture-bundle';
import { DEMO_FIXTURE_SET } from './fixture-set';
import { demoProvenance } from './provenance-bundle';
import { demoTours } from './tour-bundle';

process.env.KEW_DEMO_BUILD = '1';

export default defineConfig({
  root: fileURLToPath(new URL('../', import.meta.url)),
  plugins: [
    tailwindcss(),
    sveltekit(),
    demoFixtures(DEMO_FIXTURE_SET),
    // The tours (#3145): `demo/tours/*.json`, each step's words read out of
    // its passage in `docs/`. A missing or duplicate passage fails this build.
    demoTours(new URL('./tours/', import.meta.url), new URL('../../docs/', import.meta.url)),
    demoProvenance()
  ],
  define: { __KEW_DEMO__: 'true' },
  // As in `vite.config.ts` (#3478): the Plex arrow subsets are under the 4 KB
  // inline limit, and the demo site's CSP (`font-src 'self'`, www/Caddyfile)
  // refuses a data: font.
  build: { assetsInlineLimit: (file) => (file.endsWith('.woff2') ? false : undefined) }
});
