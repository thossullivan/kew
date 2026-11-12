import { describe, expect, it } from 'vitest';
import { load } from './+page';

// `/console` was the legacy judgment console; #3454 removed it once Work, Monitor
// and Budget offered every action it had. The loader must 308 to Work's
// needs-you view so typed URLs and old bookmarks land where the console's
// questions are answered now.
describe('/console load', () => {
  it("redirects permanently to Work's needs-you view", () => {
    try {
      load();
      throw new Error('expected load() to throw a redirect');
    } catch (e) {
      // SvelteKit's redirect() throws a { status, location } Redirect object.
      const r = e as { status?: number; location?: string };
      expect(r.status).toBe(308);
      expect(r.location).toBe('/work?filter=needs-you');
    }
  });
});
