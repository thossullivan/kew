import { redirect } from '@sveltejs/kit';

export const ssr = false;

/**
 * `/recorder` preserved the byte-identical old home cockpit, but nothing links
 * to it: `/runs` owns runs-browsing and each run's flight recorder. The page is
 * retired to a permanent redirect so typed URLs and old bookmarks keep working.
 * Any `#token=…` fragment survives the redirect automatically (browsers carry
 * the fragment across a 3xx), and the target layout picks it up.
 */
export function load(): never {
  throw redirect(308, '/runs');
}
