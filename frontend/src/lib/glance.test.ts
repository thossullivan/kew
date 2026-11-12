import { describe, expect, it } from 'vitest';
import { queueOutcome } from './glance';
import type { QueueState, VerbResult } from './verbs';

describe('queueOutcome', () => {
  const ok = (over: Partial<QueueState> = {}): VerbResult<QueueState> => ({
    ok: true,
    data: { issue: 7, ready: true, driver_stale: false, ...over }
  });

  it('transitions to a queued chip from the write response', () => {
    const out = queueOutcome(7, ok());
    expect(out).toEqual({ ok: true, issue: 7, chip: 'queued', notice: null });
  });

  it('carries the no-loop-running notice when the driver is stale', () => {
    const out = queueOutcome(7, ok({ driver_stale: true }));
    expect(out.ok).toBe(true);
    if (out.ok) expect(out.notice).toMatch(/kew run/);
  });

  it('reads not-queued when the server did not mark the issue ready', () => {
    const out = queueOutcome(7, ok({ ready: false }));
    if (out.ok) expect(out.chip).toBe('not queued');
  });

  it('surfaces the error message inline on a failed write', () => {
    const out = queueOutcome(7, { ok: false, kind: 'not_approver', status: 403, message: 'nope' });
    expect(out).toEqual({ ok: false, issue: 7, message: 'nope' });
  });
});
