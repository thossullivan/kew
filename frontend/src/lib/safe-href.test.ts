/** C10 (#3470) — the one gate a served URL passes to become an `href`. */
import { describe, expect, it } from 'vitest';
import { safeAppHref, safeHref } from './safe-href';
import { safeHref as markdownSafeHref } from './work/markdown';

describe('safeHref', () => {
  it('passes an absolute http(s) URL', () => {
    expect(safeHref('https://github.com/o/r/pull/9')).toBe('https://github.com/o/r/pull/9');
    expect(safeHref(' http://example.com/x ')).toBe('http://example.com/x');
  });

  it.each([
    'javascript:alert(1)',
    'JAVASCRIPT:alert(1)',
    'java\u0000script:alert(1)',
    '\tjavascript:alert(1)',
    'data:text/html,<script>alert(1)</script>',
    '/runs/r1',
    '#changes',
    '//evil.example/x',
    'https://',
    ''
  ])('refuses %j', (url) => {
    expect(safeHref(url)).toBeNull();
  });

  it('refuses a missing URL', () => {
    expect(safeHref(null)).toBeNull();
    expect(safeHref(undefined)).toBeNull();
  });

  it('is the same gate the tracker body uses', () => {
    expect(markdownSafeHref).toBe(safeHref);
  });
});

describe('safeAppHref', () => {
  it('passes a path inside the app, and whatever safeHref passes', () => {
    expect(safeAppHref('/work?item=9#verify')).toBe('/work?item=9#verify');
    expect(safeAppHref('https://github.com/o/r/pull/9')).toBe('https://github.com/o/r/pull/9');
  });

  it.each(['javascript:alert(1)', '//evil.example/x', '/\\evil.example/x', 'runs/r1', '', null])(
    'refuses %j',
    (url) => {
      expect(safeAppHref(url)).toBeNull();
    }
  );
});
