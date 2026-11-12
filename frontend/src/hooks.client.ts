import type { ClientInit } from '@sveltejs/kit';

/**
 * The demo build's switch (#3139). `__KEW_DEMO__` is `false` in
 * `vite.config.ts`, so in every normal build this branch is dead code and the
 * import inside it — the only path to `src/lib/demo/` — is dropped with it.
 * In `npm run build:demo` it installs the fixture-backed `/api` before the
 * first `load` runs.
 */
export const init: ClientInit = async () => {
  if (__KEW_DEMO__) {
    const { installDemo } = await import('$lib/demo/install');
    installDemo();
  }
};
