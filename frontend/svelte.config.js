import adapter from '@sveltejs/adapter-static';
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';

// `npm run build:demo` (demo/vite.demo.config.ts) sets this, so the static
// demo console lands in build-demo/ and never overwrites the build `kew serve`
// ships (#3139).
const out = process.env.KEW_DEMO_BUILD === '1' ? 'build-demo' : 'build';

/** @type {import('@sveltejs/kit').Config} */
const config = {
  preprocess: vitePreprocess(),
  kit: {
    // SPA: no server runtime. All routes render client-side (ssr=false),
    // and unknown paths fall back to the shell so client routing works.
    adapter: adapter({ pages: out, assets: out, fallback: 'index.html', strict: false })
  }
};

export default config;
