import { beforeEach, describe, expect, it, vi } from 'vitest';

const login = vi.fn();
const fetchSession = vi.fn();
vi.mock('$lib/api', () => ({
  login: (...args: unknown[]) => login(...args),
  fetchSession: (...args: unknown[]) => fetchSession(...args)
}));

import {
  factoryHomeHref,
  inboxLanding,
  legacyInboxHref,
  parseTokenFromHash,
  pickUpSession
} from './auth';

/**
 * Minimal `window` stub so pickup logic is testable in the node env.
 *
 * `history.state` is part of it because SvelteKit keeps its own navigation
 * bookkeeping there (#2278 C2): the stub carries a stand-in for it so the
 * assertions below can show it survived the strip.
 */
const ROUTER_STATE = { 'sveltekit:index': 3 };

function stubWindow(url: string, state: unknown = ROUTER_STATE) {
  const u = new URL(url);
  const replaceState = vi.fn();
  // A partial window is all pickUpSession touches (hash + history).
  globalThis.window = {
    location: { hash: u.hash, pathname: u.pathname, search: u.search },
    history: { replaceState, state }
  } as unknown as Window & typeof globalThis;
  return { replaceState };
}

describe('parseTokenFromHash', () => {
  it('extracts and decodes token from fragment', () => {
    expect(parseTokenFromHash('#token=ab%20cd')).toBe('ab cd');
  });
  it('extracts token when other params present', () => {
    expect(parseTokenFromHash('#foo=1&token=xyz')).toBe('xyz');
  });
  it('returns null when no token', () => {
    expect(parseTokenFromHash('#nothing=1')).toBeNull();
    expect(parseTokenFromHash('')).toBeNull();
  });
});

describe('pickUpSession (root-layout pickup)', () => {
  beforeEach(() => {
    login.mockReset();
    fetchSession.mockReset();
  });

  it('signs in from #token= on any route and strips the fragment', async () => {
    login.mockResolvedValue(undefined);
    const { replaceState } = stubWindow('http://kew.test/runs#token=abc');
    const ok = await pickUpSession();
    expect(ok).toBe(true);
    expect(login).toHaveBeenCalledWith('abc', expect.anything());
    expect(fetchSession).not.toHaveBeenCalled();
    // fragment stripped, path preserved (any route, not just /inbox)
    expect(replaceState).toHaveBeenCalledWith(ROUTER_STATE, '', '/runs');
  });

  it('preserves the query string when stripping the fragment (#714 filter fix)', async () => {
    login.mockResolvedValue(undefined);
    const { replaceState } = stubWindow('http://kew.test/work?filter=needs-you#token=abc');
    const ok = await pickUpSession();
    expect(ok).toBe(true);
    // /inbox#token=… redirects land on /work?filter=needs-you — the filter must
    // survive sign-in.
    expect(replaceState).toHaveBeenCalledWith(ROUTER_STATE, '', '/work?filter=needs-you');
  });

  it('reports a failed exchange without leaving the token in the URL', async () => {
    login.mockRejectedValue(new Error('bad token'));
    const { replaceState } = stubWindow('http://kew.test/dashboard#token=nope');
    const ok = await pickUpSession();
    expect(ok).toBe(false);
    expect(replaceState).toHaveBeenCalledWith(ROUTER_STATE, '', '/dashboard');
  });

  /**
   * #2278 (C2) — the strip kept the URL and threw the history away.
   *
   * `replaceState(null, …)` replaces the current entry's state, and the state
   * on that entry is SvelteKit's: its history index and the snapshot the client
   * router navigates by. Wiping it on arrival left the router unable to place
   * the entry it was standing on, and the back button stopped navigating — on
   * the one page every signed-in visitor lands on. The fragment still has to
   * go, so the write stays; what it writes back is the state it found.
   */
  it('keeps the history state SvelteKit navigates by (#2278 C2)', async () => {
    login.mockResolvedValue(undefined);
    const { replaceState } = stubWindow('http://kew.test/work#token=abc');
    await pickUpSession();
    expect(replaceState).toHaveBeenCalledWith(ROUTER_STATE, '', '/work');
    expect(replaceState.mock.calls[0][0]).not.toBeNull();
  });

  it('writes whatever state it found, even none at all', async () => {
    login.mockResolvedValue(undefined);
    const { replaceState } = stubWindow('http://kew.test/work#token=abc', null);
    await pickUpSession();
    expect(replaceState).toHaveBeenCalledWith(null, '', '/work');
  });

  it('checks the existing session when no token is present', async () => {
    fetchSession.mockResolvedValue({ authenticated: true });
    stubWindow('http://kew.test/dashboard');
    const ok = await pickUpSession();
    expect(ok).toBe(true);
    expect(login).not.toHaveBeenCalled();
  });
});

describe('factoryHomeHref', () => {
  // FUI-26: `/` is a redirect to the Work tab. It is also the URL the serve
  // banner prints the sign-in token on, so what it carries across is load-bearing.
  it('sends a bare root visit to the Work tab', () => {
    expect(factoryHomeHref({ search: '', hash: '' })).toBe('/work');
  });

  it('carries the #token= fragment, which is how sign-in at the root survives', () => {
    expect(factoryHomeHref({ search: '', hash: '#token=abc' })).toBe('/work#token=abc');
  });

  it('keeps the query, and puts it before the fragment', () => {
    expect(factoryHomeHref({ search: '?item=2090', hash: '#token=abc' })).toBe(
      '/work?item=2090#token=abc'
    );
  });

  it("renames the console's approvals filter to Work's needs-you filter", () => {
    expect(factoryHomeHref({ search: '?filter=approvals', hash: '' })).toBe(
      '/work?filter=needs-you'
    );
  });

  it('leaves any other filter alone rather than guessing at it', () => {
    expect(factoryHomeHref({ search: '?filter=budget', hash: '' })).toBe('/work?filter=budget');
  });

  it('renames the approvals filter without losing what sits beside it', () => {
    expect(factoryHomeHref({ search: '?filter=approvals&item=2090', hash: '#token=abc' })).toBe(
      '/work?filter=needs-you&item=2090#token=abc'
    );
  });

  it('reads window.location when called with no argument', () => {
    stubWindow('http://kew.test/?filter=approvals#token=abc');
    expect(factoryHomeHref()).toBe('/work?filter=needs-you#token=abc');
  });
});

describe('legacyInboxHref', () => {
  it('redirects to the needs-you filter of the Work tab', () => {
    expect(legacyInboxHref({ hash: '' })).toBe('/work?filter=needs-you');
  });

  it('preserves the #token= fragment, keeping query before fragment', () => {
    expect(legacyInboxHref({ hash: '#token=abc' })).toBe('/work?filter=needs-you#token=abc');
  });

  it('reads window.location when called with no argument, as the page calls it', () => {
    stubWindow('http://kew.test/inbox#token=abc');
    expect(legacyInboxHref()).toBe('/work?filter=needs-you#token=abc');
  });
});

describe('inboxLanding', () => {
  // The rule that keeps a signed-out visitor from bouncing: `/inbox` is where
  // every 401 in the app sends the browser, and its redirect target is inside the
  // `(factory)` group, whose shared poll signs out on its own 401.
  it('rests on the sign-in card when there is no session and no token', () => {
    expect(inboxLanding({ hash: '' }, false)).toBeNull();
  });

  it('redirects a live session to the needs-you filter, as the bookmark expects', () => {
    expect(inboxLanding({ hash: '' }, true)).toBe('/work?filter=needs-you');
  });

  it('forwards a #token= even with no session — it IS the session arriving', () => {
    expect(inboxLanding({ hash: '#token=abc' }, false)).toBe(
      '/work?filter=needs-you#token=abc'
    );
  });

  it('keeps carrying the token when a session is already live', () => {
    expect(inboxLanding({ hash: '#token=abc' }, true)).toBe(
      '/work?filter=needs-you#token=abc'
    );
  });

  it('is not fooled by a fragment that carries no token', () => {
    expect(inboxLanding({ hash: '#item=2090' }, false)).toBeNull();
  });
});
