import { describe, expect, it } from 'vitest';
import { load } from './+page';

// `/dashboard` was the console's pre-cutover home; Group 6 promoted it to `/`,
// and FUI-26 made `/work` the way in. The loader must redirect to `/work` — the
// same destination `/` now forwards to — so old links and bookmarks keep working
// without a second hop.
describe('/dashboard load', () => {
  it('redirects permanently to the Work tab', () => {
    try {
      load();
      throw new Error('expected load() to throw a redirect');
    } catch (e) {
      // SvelteKit's redirect() throws a { status, location } Redirect object.
      const r = e as { status?: number; location?: string };
      expect(r.status).toBe(308);
      expect(r.location).toBe('/work');
    }
  });
});
