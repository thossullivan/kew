/**
 * FUI-26 — the navigation cutover, checked as a list of URLs.
 *
 * The cutover moved every page of the app into the `(factory)` group so that
 * every route draws the 56px header, and a route group is invisible in URLs: a
 * move that lands a directory in the wrong place, or that forgets one, changes
 * no import and breaks no other test — it 404s a URL somebody has bookmarked.
 * So the URLs are written out here, every one the app has ever answered, and
 * each is resolved against the route tree the way SvelteKit resolves it.
 *
 * There is no jsdom in this suite and no way to mount a router, so this reads
 * the filesystem and the sources. That is enough to catch the failure this issue
 * can actually cause.
 */
import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import { railCta } from '$lib/epic/rail';
import { FACTORY_TABS, MORE_DESTINATIONS } from '$lib/shell/nav';

const ROUTES = fileURLToPath(new URL('./', import.meta.url));
const SRC = fileURLToPath(new URL('../', import.meta.url));
const read = (...parts: string[]) => readFileSync(join(ROUTES, ...parts), 'utf8');

/** Every `.ts`/`.svelte` file the app ships, tests excluded. */
function sources(): string[] {
  return readdirSync(SRC, { recursive: true, encoding: 'utf8' })
    .filter((p) => /\.(ts|svelte)$/.test(p) && !p.includes('.test.'))
    .map((p) => join(SRC, p));
}

/**
 * Every URL the app answers, in route-path form (`[id]` where a segment is a
 * parameter). `page` routes render; `redirect` routes are a loader or a one-line
 * stub that forwards, and have no screen of their own.
 */
const URLS: { url: string; kind: 'page' | 'redirect'; why: string }[] = [
  { url: '/', kind: 'redirect', why: 'the console until FUI-26; now the way in to /work' },
  {
    url: '/inbox',
    kind: 'redirect',
    why: 'the original approval inbox, and where being signed out rests'
  },
  { url: '/dashboard', kind: 'redirect', why: "the console's home before it moved to /" },
  { url: '/recorder', kind: 'redirect', why: 'the preserved old cockpit, retired to /runs' },
  { url: '/work', kind: 'page', why: 'FUI-18, the home tab' },
  { url: '/monitor', kind: 'page', why: 'FUI-22' },
  { url: '/epics', kind: 'page', why: 'the epic list' },
  { url: '/epics/[ref]', kind: 'page', why: 'FUI-14, the epic DAG' },
  {
    url: '/console',
    kind: 'redirect',
    why: "the legacy judgment console, removed; now Work's needs-you view"
  },
  { url: '/runs', kind: 'page', why: 'fleet history' },
  { url: '/runs/[id]', kind: 'page', why: 'the flight recorder' },
  { url: '/budget', kind: 'page', why: 'spend caps' },
  { url: '/config', kind: 'page', why: 'runtime overrides' },
  { url: '/prompts', kind: 'page', why: "#3019, each agent role's prompt, read-only" },
  { url: '/scorecard', kind: 'page', why: 'cost per model' },
  { url: '/health', kind: 'page', why: 'FUI/#2298, can the factory work?' },
  { url: '/loops', kind: 'page', why: 'loop status' },
  { url: '/audit', kind: 'page', why: 'the governance log' }
];

/** Where a URL's route directory is: inside the group, outside it, or nowhere. */
function locate(url: string): { dir: string; factory: boolean }[] {
  const rel = url.replace(/^\//, '');
  const candidates = [
    { dir: join(ROUTES, '(factory)', rel), factory: true },
    { dir: join(ROUTES, rel), factory: false }
  ];
  return candidates.filter(
    (c) => existsSync(join(c.dir, '+page.svelte')) || existsSync(join(c.dir, '+page.ts'))
  );
}

describe('every URL the app has ever answered', () => {
  it.each(URLS)('$url still resolves — $why', ({ url }) => {
    expect(locate(url), `${url} resolves to no route`).toHaveLength(1);
  });

  it.each(URLS)('$url resolves in exactly one place', ({ url }) => {
    // Two matching directories is not a redundancy, it is a SvelteKit build
    // error: `/runs` and `/(factory)/runs` are the same URL.
    expect(locate(url).length, `${url} is claimed by two routes`).toBeLessThan(2);
  });

  it('draws every screen inside the (factory) group, and so under the header', () => {
    for (const { url, kind } of URLS) {
      if (kind !== 'page') continue;
      const [found] = locate(url);
      expect(found.factory, `${url} renders outside the factory header`).toBe(true);
    }
  });

  it('leaves outside the group only the redirects, which need no shell', () => {
    for (const { url, kind } of URLS) {
      if (kind !== 'redirect') continue;
      const [found] = locate(url);
      expect(found.factory, `${url} is a redirect; it needs no shell`).toBe(false);
    }
  });
});

describe('the redirects', () => {
  it('sends / to the Work tab through the helper that keeps query and fragment', () => {
    const source = read('+page.svelte');
    // Not `goto`: a client-side navigation drops the `#token=…` the serve banner
    // prints at the root, and sign-in would break.
    expect(source).toContain("import { factoryHomeHref } from '$lib/auth'");
    expect(source).toContain('window.location.replace(factoryHomeHref())');
    expect(source).not.toContain('goto(');
  });

  it('sends /inbox onward through the helper that decides where it lands', () => {
    const source = read('inbox', '+page.svelte');
    expect(source).toContain("import { inboxLanding } from '$lib/auth'");
    expect(source).toContain('window.location.replace(');
    // Not `goto`: a client-side navigation drops the `#token=…` a stale
    // `/inbox#token=…` link carries, and that link must still sign in.
    expect(source).not.toContain('goto(');
  });

  it('sends /dashboard to /work, not to a page that forwards again', () => {
    expect(read('dashboard', '+page.ts')).toContain("redirect(308, '/work')");
  });

  it('leaves /recorder pointing at /runs, which the cutover did not move', () => {
    expect(read('recorder', '+page.ts')).toContain("redirect(308, '/runs')");
  });
});

describe('being signed out', () => {
  /**
   * Where the app sends a lapsed session. Two forms reach it: the client-side
   * `window.location.href = …` of the shell's poll and the epic and run screens,
   * and the SvelteKit `redirect` the budget and config loaders throw when their
   * read comes back 401. The redirect form is only read out of files that
   * mention 401 at all, which keeps the plain cutover redirects (`/dashboard` →
   * `/work`) out of the set.
   */
  function signOutDestinations(): string[] {
    const found = new Set<string>();
    for (const file of sources()) {
      const source = readFileSync(file, 'utf8');
      for (const [, href] of source.matchAll(/window\.location\.href = '([^']+)'/g)) {
        found.add(href);
      }
      if (!source.includes('401')) continue;
      for (const [, href] of source.matchAll(/redirect\(\d+, '([^']+)'\)/g)) found.add(href);
    }
    return [...found];
  }

  it('has somewhere to send a lapsed session at all', () => {
    expect(signOutDestinations().length).toBeGreaterThan(0);
  });

  it('never sends one to a URL inside the (factory) group', () => {
    // The loop this forbids: the group's shared poll (`pulse.svelte.ts`) signs
    // out on a 401, so a sign-out destination that resolves inside the group is
    // read by that poll, 401s again and navigates again — a full-page reload
    // loop with no way to sign in between bounces.
    for (const href of signOutDestinations()) {
      const [found] = locate(href.replace(/[?#].*$/, ''));
      expect(found, `${href} resolves to no route`).toBeDefined();
      expect(found.factory, `${href} signs out into the factory group`).toBe(false);
    }
  });

  it('rests at that destination instead of forwarding a signed-out visitor on', () => {
    // Being outside the group is not enough: `/inbox` is outside it and used to
    // replace the document with `/work?filter=needs-you` unconditionally, which
    // closed the same loop one hop later. So the page reads the session first,
    // and draws the sign-in card when there is none.
    for (const href of signOutDestinations()) {
      const dir = href.replace(/^\//, '').replace(/[?#].*$/, '');
      const source = read(dir, '+page.svelte');
      expect(source, `${href} never reads the session`).toContain('fetchSession(');
      expect(source, `${href} offers no way to sign in`).toContain('SignInCard');
    }
  });
});

describe('the approval call to action', () => {
  /**
   * A screen's sources: its `+page.svelte` and every `$lib` module reachable
   * from it. Enough to answer "is that control on this screen?" in a suite with
   * no browser — which is the question the epic rail's approval CTA turns on.
   */
  function closure(url: string): string {
    const [found] = locate(url);
    const seen = new Set<string>();
    const texts: string[] = [];
    const walk = (file: string) => {
      // `$lib/ui` is a directory of components behind an `index.ts`, so the
      // barrel is a candidate alongside the file forms.
      const resolved = ['', '.ts', '.svelte', '/index.ts']
        .map((e) => file + e)
        .find((c) => existsSync(c) && statSync(c).isFile());
      if (resolved === undefined || seen.has(resolved)) return;
      seen.add(resolved);
      const source = readFileSync(resolved, 'utf8');
      texts.push(source);
      for (const [, spec] of source.matchAll(/from '(\$lib\/[^']+)'/g)) {
        walk(join(SRC, 'lib', spec.slice('$lib/'.length)));
      }
    };
    walk(join(found.dir, '+page.svelte'));
    return texts.join('\n');
  }

  it('sends an approval to a screen that can actually decide one', () => {
    // `railCta('approval')` is the one CTA with no forge URL to point at, so it
    // names a kew screen — and it named `/inbox`, which the cutover turned into
    // a redirect to Work, a screen with no approve control anywhere in it.
    const cta = railCta('approval', null)!;
    expect(cta.external).toBe(false);
    expect(URLS.find((u) => u.url === cta.href)?.kind, `${cta.href} is not a screen`).toBe(
      'page'
    );
    expect(closure(cta.href), `${cta.href} draws no approve control`).toContain('decideHold(');
  });
});

describe('the navigation the header offers', () => {
  it('points every tab and every More destination at a URL in this list', () => {
    const known = new Set(URLS.map((u) => u.url));
    for (const d of [...FACTORY_TABS, ...MORE_DESTINATIONS]) {
      expect(known.has(d.href), `${d.href} is not a URL this app answers`).toBe(true);
    }
  });

  it('offers a way to every screen: no page is reachable by typed URL alone', () => {
    // The tabs and the menu are the whole navigation now that the rail is gone,
    // so a page in neither is a page nobody can find. `/epics/[ref]` and
    // `/runs/[id]` are detail URLs their list page links to, not destinations.
    const offered = new Set([...FACTORY_TABS, ...MORE_DESTINATIONS].map((d) => d.href));
    const details = new Set(['/epics/[ref]', '/runs/[id]']);
    for (const { url, kind } of URLS) {
      if (kind !== 'page' || details.has(url)) continue;
      expect(offered.has(url), `${url} is in no tab and no menu`).toBe(true);
    }
  });
});

describe('the retired left rail', () => {
  it('is deleted, and the root layout draws no shell of its own', () => {
    expect(existsSync(fileURLToPath(new URL('../lib/shell/Rail.svelte', import.meta.url)))).toBe(
      false
    );
    const layout = read('+layout.svelte');
    // The word survives in the comment that records the deletion; the import
    // and the element are what must be gone.
    expect(layout).not.toMatch(/import Rail|<Rail/);
    expect(layout).toContain('{@render children()}');
  });

  it('keeps the root layout doing the two jobs no page can do for itself', () => {
    const layout = read('+layout.svelte');
    // The theme, because it is the document's, and token pickup, because a
    // `#token=…` can arrive on any route.
    expect(layout).toContain('theme.init()');
    expect(layout).toContain('pickUpSession()');
  });
});
