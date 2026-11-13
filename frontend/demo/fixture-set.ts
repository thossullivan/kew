/**
 * The ONE setting the demo build reads (#3139): the fixture set it bundles.
 * The synthetic demo world (#3141), never the factory set: the factory set
 * holds real epic refs, run ids and names, and the demo world holds none.
 * `demo/world/generate.mjs` writes it.
 *
 * Its own module, not a line in `vite.demo.config.ts`, so a test can read the
 * set the demo ships without loading a Vite config that starts SvelteKit.
 */
export const DEMO_FIXTURE_SET = new URL('./world/', import.meta.url);
