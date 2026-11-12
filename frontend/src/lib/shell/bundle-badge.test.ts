import { describe, expect, it } from 'vitest';
import { bundleBadge } from './bundle-badge';
import type { Bundle } from '$lib/api';

const mk = (over: Partial<Bundle> = {}): Bundle => ({
  present: true,
  git_sha: 'abc1234',
  built_at: 1_700_000_000,
  stale: false,
  ...over
});

describe('bundleBadge', () => {
  it('returns null when there is no bundle', () => {
    expect(bundleBadge(null)).toBeNull();
  });

  it('returns null when the bundle is absent', () => {
    expect(bundleBadge(mk({ present: false }))).toBeNull();
  });

  it('shows the short sha and build day for a fresh bundle', () => {
    const badge = bundleBadge(mk());
    expect(badge?.label).toBe('build abc1234');
    expect(badge?.stale).toBe(false);
    expect(badge?.title).toContain('2023-11-14');
  });

  it('flags a stale bundle with a rebuild hint', () => {
    const badge = bundleBadge(mk({ stale: true }));
    expect(badge?.label).toBe('build abc1234 · stale');
    expect(badge?.stale).toBe(true);
    expect(badge?.title).toContain('build_web.sh');
  });

  it('labels an unstamped-but-present bundle', () => {
    const badge = bundleBadge(mk({ git_sha: '', built_at: 0 }));
    expect(badge?.label).toBe('build unstamped');
    expect(badge?.title).not.toContain('built');
  });
});
