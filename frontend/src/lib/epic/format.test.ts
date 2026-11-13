import { describe, expect, it } from 'vitest';
import { elapsed, fmtTokenSplit, fmtUSD, statusDuration } from './format';

describe('elapsed', () => {
  it('seconds', () => expect(elapsed(45_000)).toBe('45s'));
  it('minutes', () => expect(elapsed(47 * 60_000)).toBe('47m'));
  it('hours + minutes', () => expect(elapsed((2 * 60 + 13) * 60_000)).toBe('2h13m'));
  it('days + hours', () => expect(elapsed((3 * 24 + 4) * 3_600_000)).toBe('3d4h'));
  it('clamps negative to 0s (never a negative duration)', () => expect(elapsed(-500)).toBe('0s'));
});

describe('statusDuration', () => {
  const now = Date.parse('2026-07-02T12:00:00.000Z');
  it('"IN_PROGRESS · 47m" shape', () => {
    const since = new Date(now - 47 * 60_000).toISOString();
    expect(statusDuration('in_progress', since, now)).toBe('IN_PROGRESS · 47m');
  });
  it('no status_since -> label only, no fabricated duration', () => {
    expect(statusDuration('done', null, now)).toBe('DONE');
    expect(statusDuration('done', 'not-a-date', now)).toBe('DONE');
  });
  it('reads an offset-less status_since as UTC, not as the viewer wall clock', () => {
    // Via $lib/time: a bare Date.parse would shift this by the viewer's offset
    // and print that shift as elapsed time (#2205).
    expect(statusDuration('in_progress', '2026-07-02T11:13:00', now)).toBe('IN_PROGRESS · 47m');
  });
});

describe('fmtUSD', () => {
  it('two decimal places', () => expect(fmtUSD(1.5)).toBe('$1.50'));
});

// `fmtTimeUTC` lived here and rendered in UTC so that one screen could not
// disagree with itself. #2205 kept the one-formatter rule and changed which
// zone it is: `$lib/time`'s `fmtTime` renders the viewer's, with the UTC
// instant in a title. The cases this block pinned — HH:MM:SS, withSeconds,
// zero padding, a dash for an unreadable stamp — are asserted there, over the
// one formatter the whole console now shares, plus a DST boundary this one
// never crossed.

describe('fmtTokenSplit', () => {
  it('formats large counts with a k suffix and cache-read percentage', () => {
    expect(fmtTokenSplit(12345, 4938)).toBe('12.3k tok · 40% cached');
  });
  it('small counts render verbatim', () => {
    expect(fmtTokenSplit(500, 100)).toBe('500 tok · 20% cached');
  });
  it('zero total -> no cache split (nothing to divide honestly)', () => {
    expect(fmtTokenSplit(0, 0)).toBe('0 tok');
  });
});
