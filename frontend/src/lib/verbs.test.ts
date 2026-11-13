import { describe, expect, it, vi } from 'vitest';
import {
  resumeFleet,
  queueIssue,
  ackOrphan,
  decideHold,
  ackRemovesRow,
  driverStaleNotice,
  resumeChip,
  queueChip,
  ackChip,
  type VerbResult,
  type AckState
} from './verbs';

/** A fake fetch that records the last call and returns a canned Response. */
function fakeFetch(body: unknown, init: ResponseInit = {}) {
  const spy = vi.fn(async () => new Response(JSON.stringify(body), init));
  return spy as unknown as typeof fetch & ReturnType<typeof vi.fn>;
}

/** Read the recorded (path, init) of a fake fetch's first call. */
function lastCall(spy: unknown): [string, RequestInit] {
  const calls = (spy as ReturnType<typeof vi.fn>).mock.calls;
  return calls[calls.length - 1] as [string, RequestInit];
}

describe('resumeFleet — confirm gating', () => {
  it('does NOT fetch when confirm is false, returning confirm_required', async () => {
    const f = fakeFetch({});
    const r = await resumeFleet(false, f);
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.kind).toBe('confirm_required');
    expect(f).not.toHaveBeenCalled();
  });

  it('posts confirm:true to /api/verbs/resume and returns the write-response state', async () => {
    const f = fakeFetch({ resumed: true, driver_stale: false });
    const r = await resumeFleet(true, f);
    expect(r.ok).toBe(true);
    if (r.ok) expect(r.data.resumed).toBe(true);
    const [path, init] = lastCall(f);
    expect(path).toBe('/api/verbs/resume');
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body as string)).toEqual({ confirm: true });
    expect((init.headers as Record<string, string>)['X-Kew-CSRF']).toBeDefined();
  });

  it('maps 403 VerbDenied to a not_approver error', async () => {
    const f = fakeFetch({ detail: "actor 'bob' is not in [approval].approvers" }, { status: 403 });
    const r = await resumeFleet(true, f);
    expect(r.ok).toBe(false);
    if (!r.ok) {
      expect(r.kind).toBe('not_approver');
      expect(r.status).toBe(403);
      expect(r.message.length).toBeGreaterThan(0);
    }
  });

  it('maps 401 to an unauthenticated error', async () => {
    const f = fakeFetch({}, { status: 401 });
    const r = await resumeFleet(true, f);
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.kind).toBe('unauthenticated');
  });
});

describe('queueIssue — confirm gating', () => {
  it('does NOT fetch without confirm', async () => {
    const f = fakeFetch({});
    const r = await queueIssue(42, false, f);
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.kind).toBe('confirm_required');
    expect(f).not.toHaveBeenCalled();
  });

  it('posts to the issue-scoped path with confirm:true', async () => {
    const f = fakeFetch({ issue: 42, ready: true, driver_stale: true });
    const r = await queueIssue(42, true, f);
    expect(r.ok).toBe(true);
    const [path, init] = lastCall(f);
    expect(path).toBe('/api/verbs/queue/42');
    expect(JSON.parse(init.body as string)).toEqual({ confirm: true });
  });

  it('maps 404 to not_found (issue outside the surfaced set)', async () => {
    const f = fakeFetch({ detail: 'issue #99 is not an open issue' }, { status: 404 });
    const r = await queueIssue(99, true, f);
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.kind).toBe('not_found');
  });
});

describe('ackOrphan', () => {
  it('posts to the orphan ack path and reports removal on a successful write', async () => {
    const f = fakeFetch({ pr_number: 12, acked: true, acked_at: '2026-07-16T00:00:00Z' });
    const r = await ackOrphan(12, f);
    expect(r.ok).toBe(true);
    const [path, init] = lastCall(f);
    expect(path).toBe('/api/verbs/orphans/12/ack');
    expect(init.method).toBe('POST');
    expect(ackRemovesRow(r)).toBe(true);
  });

  it('does not remove the row when the write failed', async () => {
    const f = fakeFetch({}, { status: 401 });
    const r = await ackOrphan(12, f);
    expect(ackRemovesRow(r)).toBe(false);
  });
});

describe('decideHold (approve/deny rewired into rows)', () => {
  it('posts the action + reason to the approvals endpoint', async () => {
    const f = fakeFetch({ status: 'approved', issue: '5' });
    const r = await decideHold('5', 'approve', 'looks good', f);
    expect(r.ok).toBe(true);
    if (r.ok) expect(r.data.status).toBe('approved');
    const [path, init] = lastCall(f);
    expect(path).toBe('/api/approvals/5');
    expect(JSON.parse(init.body as string)).toEqual({ action: 'approve', reason: 'looks good' });
  });

  it('encodes a bare non-GitHub tracker id into the route path', async () => {
    // Use an id with a reserved path character so this test actually fails if
    // encodeURIComponent is removed ('SFD-12' round-trips unchanged).
    const f = fakeFetch({ status: 'denied', issue: 'SFD/12' });
    const r = await decideHold('SFD/12', 'deny', '', f);
    expect(r.ok).toBe(true);
    if (r.ok) expect(r.data.issue).toBe('SFD/12');
    const [path] = lastCall(f);
    expect(path).toBe('/api/approvals/SFD%2F12');
  });
});

describe('network failures', () => {
  it('maps a thrown fetch to a network error without crashing', async () => {
    const f = vi.fn(async () => {
      throw new Error('offline');
    }) as unknown as typeof fetch;
    const r = await ackOrphan(1, f);
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.kind).toBe('network');
  });
});

describe('a 2xx that is not JSON (#2285, C16)', () => {
  /** A 200 whose body is an HTML interstitial — a proxy, not kew. */
  function nonJson(status = 200) {
    const spy = vi.fn(async () => new Response('<html>Gateway</html>', { status }));
    return spy as unknown as typeof fetch;
  }

  it('is a server error, not a throw out of the caller', async () => {
    // The regression: `(await r.json())` threw past every `VerbResult` the
    // page knows how to render, so the row that asked kept its spinner for
    // the rest of the session.
    const r = await ackOrphan(7, nonJson());
    expect(r.ok).toBe(false);
    if (!r.ok) {
      expect(r.kind).toBe('server');
      expect(r.status).toBe(200);
      expect(r.message).toContain('not with a readable result');
    }
  });

  it('is reported the same way for every verb', async () => {
    const results = await Promise.all([
      resumeFleet(true, nonJson()),
      queueIssue(2285, true, nonJson()),
      decideHold('2285', 'approve', '', nonJson()),
      ackOrphan(2285, nonJson(201))
    ]);
    for (const r of results) {
      expect(r.ok).toBe(false);
      if (!r.ok) expect(r.kind).toBe('server');
    }
  });
});

describe('row-transition helpers', () => {
  it('driverStaleNotice fires only when the driver is stale', () => {
    expect(driverStaleNotice(true)).toContain('kew run');
    expect(driverStaleNotice(false)).toBeNull();
  });

  it('chips read the authoritative write-response state', () => {
    expect(resumeChip({ resumed: true, driver_stale: false })).toBe('resumed');
    expect(resumeChip({ resumed: false, driver_stale: false })).toBe('already running');
    expect(queueChip({ issue: 1, ready: true, driver_stale: false })).toBe('queued');
    expect(ackChip({ pr_number: 1, acked: true, acked_at: null })).toBe('acknowledged');
  });
});
