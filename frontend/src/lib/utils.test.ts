import { describe, expect, it } from 'vitest';
import { itemRef, timeAgo } from './utils';

describe('itemRef', () => {
  it('renders a GitHub-numeric item_id as #N', () => {
    expect(itemRef('5', 5)).toBe('#5');
  });

  it('renders a non-GitHub tracker id bare', () => {
    expect(itemRef('SFD-12', 0)).toBe('SFD-12');
  });

  it('prefers item_id over the legacy issue_num', () => {
    expect(itemRef('SFD-9', 42)).toBe('SFD-9');
  });

  it('falls back to #issue_num when item_id is absent or empty', () => {
    expect(itemRef(null, 7)).toBe('#7');
    expect(itemRef(undefined, 7)).toBe('#7');
    expect(itemRef('', 7)).toBe('#7');
  });

  it('returns "" when neither id is present', () => {
    expect(itemRef(null, null)).toBe('');
    expect(itemRef('', undefined)).toBe('');
  });
});

describe('timeAgo', () => {
  it('returns "—" for null/undefined inputs', () => {
    expect(timeAgo(null)).toBe('—');
    expect(timeAgo(undefined)).toBe('—');
  });

  it('returns "—" for invalid dates', () => {
    expect(timeAgo('invalid-date')).toBe('—');
  });

  it('returns "now" for recent timestamps', () => {
    const now = new Date();
    expect(timeAgo(now.toISOString())).toBe('now');
  });

  it('returns "now" for timestamps within 60 seconds', () => {
    const now = new Date();
    const justNow = new Date(now.getTime() - 30000); // 30 seconds ago
    expect(timeAgo(justNow.toISOString())).toBe('now');
  });

  it('returns minutes ago for timestamps within an hour', () => {
    const now = new Date();
    const fiveMinutesAgo = new Date(now.getTime() - 5 * 60 * 1000); // 5 minutes ago
    expect(timeAgo(fiveMinutesAgo.toISOString())).toBe('5m ago');
  });

  it('returns hours ago for timestamps within a day', () => {
    const now = new Date();
    const threeHoursAgo = new Date(now.getTime() - 3 * 60 * 60 * 1000); // 3 hours ago
    expect(timeAgo(threeHoursAgo.toISOString())).toBe('3h ago');
  });

  it('returns days ago for timestamps older than a day', () => {
    const now = new Date();
    const twoDaysAgo = new Date(now.getTime() - 2 * 24 * 60 * 60 * 1000); // 2 days ago
    expect(timeAgo(twoDaysAgo.toISOString())).toBe('2d ago');
  });

  it('handles custom nowMs parameter', () => {
    const now = new Date();
    const fiveMinutesAgo = new Date(now.getTime() - 5 * 60 * 1000);
    expect(timeAgo(fiveMinutesAgo.toISOString(), now.getTime())).toBe('5m ago');
  });

  it('handles future timestamps', () => {
    const now = new Date();
    const future = new Date(now.getTime() + 3600000); // 1 hour in the future
    expect(timeAgo(future.toISOString())).toBe('—');
  });
});