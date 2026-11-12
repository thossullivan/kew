/**
 * #2299 — `formatDuration`: one or two units, and the per-unit limits that
 * decide which two. The readings committed here are the contract: "1h 40m",
 * never "100m".
 */
import { describe, expect, it } from 'vitest';
import { formatDuration } from './duration';

const SECOND = 1_000;
const MINUTE = 60 * SECOND;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

describe('formatDuration', () => {
  it('counts in seconds up to two minutes', () => {
    expect(formatDuration(45 * SECOND)).toBe('45s');
    expect(formatDuration(90 * SECOND)).toBe('1m 30s');
    expect(formatDuration(119 * SECOND)).toBe('1m 59s');
    // Two minutes is the limit: past it a second is noise.
    expect(formatDuration(120 * SECOND)).toBe('2m');
    expect(formatDuration(150 * SECOND)).toBe('2m');
  });

  it('counts in minutes up to two hours, and says hours and minutes', () => {
    expect(formatDuration(47 * MINUTE)).toBe('47m');
    // The brief's own reading: 100 minutes is an hour and forty.
    expect(formatDuration(100 * MINUTE)).toBe('1h 40m');
    expect(formatDuration(119 * MINUTE)).toBe('1h 59m');
    // Past two hours the minutes drop out rather than the unit growing a
    // third digit.
    expect(formatDuration(200 * MINUTE)).toBe('3h');
  });

  it('counts in hours up to two days, and says days and hours', () => {
    expect(formatDuration(13 * HOUR)).toBe('13h');
    expect(formatDuration(30 * HOUR)).toBe('1d 6h');
    expect(formatDuration(47 * HOUR)).toBe('1d 23h');
    expect(formatDuration(100 * HOUR)).toBe('4d');
  });

  it('counts in days up to a fortnight, and says weeks and days', () => {
    expect(formatDuration(3 * DAY)).toBe('3d');
    expect(formatDuration(10 * DAY)).toBe('1w 3d');
    expect(formatDuration(30 * DAY)).toBe('4w');
  });

  it('drops a zero remainder rather than printing it', () => {
    expect(formatDuration(HOUR)).toBe('1h');
    expect(formatDuration(MINUTE)).toBe('1m');
    expect(formatDuration(2 * DAY)).toBe('2d');
    expect(formatDuration(7 * DAY)).toBe('1w');
  });

  it('never reads as more than two units', () => {
    const spans = [0, 1, SECOND, 61 * SECOND, 100 * MINUTE, 30 * HOUR, 10 * DAY, 400 * DAY];
    for (const ms of spans) {
      const read = formatDuration(ms);
      expect(read.split(' ').length, read).toBeLessThanOrEqual(2);
      expect(read, read).toMatch(/^\d+[smhdw]( \d+[smhdw])?$/);
    }
  });

  it('is monotonic — a longer duration never reads as a shorter one', () => {
    const value = (read: string): number => {
      const unit: Record<string, number> = { s: SECOND, m: MINUTE, h: HOUR, d: DAY, w: 7 * DAY };
      return read
        .split(' ')
        .reduce((sum, part) => sum + Number(part.slice(0, -1)) * unit[part.slice(-1)], 0);
    };
    let last = -1;
    for (let ms = 0; ms < 40 * DAY; ms += 7 * MINUTE + 13 * SECOND) {
      const read = value(formatDuration(ms));
      expect(read, formatDuration(ms)).toBeGreaterThanOrEqual(last);
      last = read;
    }
  });

  it('reads a duration that cannot have been meant as no time at all', () => {
    // Clock skew between the driver's host and the serve host, an unparsed
    // stamp: never a number with a minus sign in front of it.
    expect(formatDuration(-5 * MINUTE)).toBe('0s');
    expect(formatDuration(Number.NaN)).toBe('0s');
    expect(formatDuration(Number.POSITIVE_INFINITY)).toBe('0s');
    expect(formatDuration(0)).toBe('0s');
    expect(formatDuration(400)).toBe('0s');
  });
});
