import { sveltekit } from '@sveltejs/kit/vite';
import tailwindcss from '@tailwindcss/vite';
import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vitest/config';
import { fixtureApi } from './fixtures/plugin';

// `fixtureApi()` is `false` unless KEW_UI_FIXTURES=1 (`npm run dev:fixtures`),
// and Vite drops a falsy plugin — so an ordinary dev server or build is
// byte-for-byte what it was before FUI-02.
const fixtures = fixtureApi();

const CATALOG = fileURLToPath(new URL('../kew/src/kew/models.json', import.meta.url));

export default defineConfig({
  plugins: [tailwindcss(), sveltekit(), fixtures],
  // The demo build's flag (#3139). `demo/vite.demo.config.ts` defines it as
  // `true`; here it is `false`, so every `if (__KEW_DEMO__)` is dead code the
  // bundler drops, and no build, dev server or test reads an undefined global.
  define: { __KEW_DEMO__: 'false' },
  // The Plex arrow subsets (`src/lib/fonts/`, #3478) are each under Vite's
  // 4 KB inline limit, so by default they would be pasted into the CSS as
  // `data:font/woff2` URLs, which a `font-src 'self'` CSP (demo.kew.dev,
  // `www/Caddyfile`) refuses. A font always ships as its own file.
  build: { assetsInlineLimit: (file) => (file.endsWith('.woff2') ? false : undefined) },
  // Vitest only — `vite dev` and `vite build` ignore it. The suite's committed
  // expectations are the artboard's own readings, which are UTC, and since
  // #2205 every timestamp renders in the *runner's* zone: unpinned, those
  // assertions pass in CI (UTC) and fail on a laptop in Berlin, which is a
  // suite that is green for a reason unrelated to the code. Pinned here rather
  // than in the test command so `npm test`, `npx vitest`, CI and verify.sh
  // cannot drift. The tests that are *about* a zone set `process.env.TZ`
  // themselves, per call, and restore it after.
  test: {
    env: { TZ: 'UTC' },
    // A kew sandbox passes its `--cpus` cap here (#2887): a CPU quota does not
    // hide the host's cores, so an unset pool sizes itself to the HOST and
    // runs the container out of memory. Unset everywhere else: the default.
    maxWorkers: Number(process.env.VITEST_MAX_WORKERS) || undefined,
    /**
     * Two projects, because two kinds of test live in this suite (#2248).
     *
     * Almost everything here is a pure reading or a server-side render, which
     * wants Node and Vite's `ssr` resolution. A handful of behaviours cannot be
     * checked that way at all — a poll's timer, a cursor advanced by a click —
     * because `$effect` does not run in an SSR render and `mount()` is not
     * available there. Those files are named `*.client.test.ts` and get jsdom
     * plus the `browser` resolve condition, which is what makes `svelte` resolve
     * to the client runtime rather than `index-server.js`.
     *
     * jsdom is a dev-only dependency and must stay one: it is named nowhere but
     * here and in `devDependencies`, so no bundle can reach it.
     *
     * `extends: true` carries the plugins and the pinned `TZ` above into both,
     * so a reading cannot be checked in one zone here and another there.
     */
    projects: [
      {
        extends: true,
        test: {
          name: 'server',
          environment: 'node',
          include: ['src/**/*.test.ts', 'scripts/**/*.test.ts'],
          exclude: ['src/**/*.client.test.ts']
        }
      },
      {
        extends: true,
        resolve: { conditions: ['browser'] },
        test: {
          name: 'client',
          environment: 'jsdom',
          include: ['src/**/*.client.test.ts']
        }
      }
    ]
  },
  server: {
    // The model catalog (kew/src/kew/models.json) is imported from outside
    // this package for display names (#2541); dev serves that one file only.
    fs: { allow: [CATALOG] },
    ...(fixtures
      ? // Fixture mode answers /api itself; proxying as well would race the
        // middleware against a `kew serve` that may not even be running.
        {}
      : {
          // Dev-only: `vite dev` proxies /api to `kew serve` (matches
          // flight_recorder.port). Production serves the built bundle from
          // `kew serve` itself (same origin), so no proxy is involved there.
          proxy: { '/api': 'http://localhost:8765' }
        })
  }
});
