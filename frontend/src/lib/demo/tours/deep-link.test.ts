import { describe, expect, it } from 'vitest';
import { carriesTourLink, PAGE_SEARCH_KEYS, readTourLink, writeTourLink } from './deep-link';

const url = (path: string) => new URL(path, 'http://demo.test');

describe('readTourLink', () => {
  it('reads ?tour=&step= off the URL when the state has no word on it', () => {
    expect(readTourLink({}, url('/work?tour=sample&step=2'))).toEqual({ tour: 'sample', step: 2 });
    expect(readTourLink({ workSearch: '?item=1' }, url('/work?tour=sample'))).toEqual({ tour: 'sample', step: 1 });
    expect(readTourLink({}, url('/work?item=7'))).toBeNull();
  });

  it('reads the state first: a step written there beats the URL, and null ends it', () => {
    const deep = url('/work?tour=sample&step=1');
    expect(readTourLink({ tour: 'sample', tourStep: 2 }, deep)).toEqual({ tour: 'sample', step: 2 });
    expect(readTourLink({ tour: null, tourStep: null }, deep)).toBeNull();
  });

  // Behavior change (design review c9, X6): an unreadable step was read as
  // step 1 and opened the tour; it is now step 0, which no tour has, so the
  // link opens nothing and the controller takes it out of the address.
  it.each(['0', '-1', '1.5', 'two', 'abc', ''])('reads step=%s as step 0, a step no tour has', (step) => {
    expect(readTourLink({}, url(`/work?tour=sample&step=${step}`))).toEqual({ tour: 'sample', step: 0 });
  });
});

describe('writeTourLink', () => {
  it("merges the two keys into the page's own query and state", () => {
    const written = writeTourLink({ workSearch: '?item=7', node: 'n1' }, url('/work?item=7'), {
      tour: 'sample',
      step: 2
    });
    expect(written.url.pathname + written.url.search).toBe('/work?item=7&tour=sample&step=2');
    expect(written.state).toMatchObject({ node: 'n1', tour: 'sample', tourStep: 2 });
    expect(written.state.workSearch).toBe('?item=7&tour=sample&step=2');
  });

  it('takes the tour out and leaves everything else', () => {
    const written = writeTourLink(
      { workSearch: '?item=7', node: 'n1', tour: 'sample', tourStep: 2 },
      url('/work?item=7&tour=sample&step=2'),
      null
    );
    expect(written.url.pathname + written.url.search).toBe('/work?item=7');
    expect(written.state).toMatchObject({ workSearch: '?item=7', node: 'n1', tour: null, tourStep: null });
    expect(readTourLink(written.state, url('/work?tour=sample&step=2'))).toBeNull();
  });

  it("takes the link out of every page's own search key, so the page's next write cannot bring it back", () => {
    // A deep link: the Work page's last write was made while the address
    // still held the tour, and the Monitor stream's key was never written.
    const written = writeTourLink(
      { workSearch: '?tour=sample&step=1&item=7', tour: 'sample', tourStep: 1 },
      url('/work?tour=sample&step=1&item=7'),
      null
    );
    expect(written.url.pathname + written.url.search).toBe('/work?item=7');
    for (const key of PAGE_SEARCH_KEYS) expect(written.state[key], key).toBe('?item=7');
  });

  it("sets the step in force inside every page search key, seeded from the address when the page has none", () => {
    const written = writeTourLink(
      { workSearch: '?item=7&tour=sample&step=1' },
      url('/work?item=7&tour=sample&step=1&filter=needs-you'),
      { tour: 'sample', step: 2 }
    );
    expect(written.state.workSearch).toBe('?item=7&tour=sample&step=2');
    expect(written.state.streamSearch).toBe('?item=7&tour=sample&step=2&filter=needs-you');
    expect(written.state.runSearch).toBe(written.state.streamSearch);
  });

  it('round-trips through readTourLink', () => {
    const { url: u, state } = writeTourLink({}, url('/work'), { tour: 'sample', step: 2 });
    expect(readTourLink(state, u)).toEqual({ tour: 'sample', step: 2 });
    expect(readTourLink({}, u)).toEqual({ tour: 'sample', step: 2 });
  });
});

describe('carriesTourLink', () => {
  it('sees either key, and nothing else', () => {
    expect(carriesTourLink(url('/work?tour=sample&step=1'))).toBe(true);
    expect(carriesTourLink(url('/work?step=2'))).toBe(true);
    expect(carriesTourLink(url('/work?item=7'))).toBe(false);
  });
});
