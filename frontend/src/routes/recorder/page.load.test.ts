import { describe, expect, it } from 'vitest';
import { load } from './+page';

// `/recorder` was the byte-identical preserved old home cockpit; `/runs` owns
// runs-browsing and each run's flight recorder, so `/recorder` is retired to a
// permanent redirect. The loader must 308 to `/runs` so typed URLs and old
// bookmarks keep working.
describe('/recorder load', () => {
  it('redirects permanently to /runs', () => {
    try {
      load();
      throw new Error('expected load() to throw a redirect');
    } catch (e) {
      // SvelteKit's redirect() throws a { status, location } Redirect object.
      const r = e as { status?: number; location?: string };
      expect(r.status).toBe(308);
      expect(r.location).toBe('/runs');
    }
  });
});
