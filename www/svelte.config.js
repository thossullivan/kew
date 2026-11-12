import adapter from '@sveltejs/adapter-static';
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';

/** @type {import('@sveltejs/kit').Config} */
export default {
  preprocess: vitePreprocess(),
  kit: {
    // Fully static marketing site — Caddy serves build/ at / and the
    // mkdocs output at /docs. No SSR, no fallback route needed.
    adapter: adapter({ pages: 'build', assets: 'build', precompress: false }),
    prerender: {
      // /docs/* joins the SvelteKit output during the distribution build.
      // The prerenderer cannot resolve those links here.
      handleHttpError: ({ path, message }) => {
        if (path === '/docs' || path.startsWith('/docs/')) return;
        throw new Error(message);
      }
    }
  }
};
