import { describe, expect, it, vi } from 'vitest';
import { loadInboxReads, type InboxReaders } from './inbox';
import type { Hold, FleetStatus, BudgetMap, SpendWindows } from './api';

const FLEET: FleetStatus = { stopped: false, pending: 2 };
const BUDGET: BudgetMap = {
  'budget.per_issue_usd': { effective: 5, file_value: 5, override: null }
};
const SPEND: SpendWindows = {
  daily: { window_days: 1, spent: 1, cap: 10, agentic: 1, interactive: 0 },
  weekly: { window_days: 7, spent: 3, cap: 50, agentic: 3, interactive: 0 }
};

function readers(over: Partial<InboxReaders> = {}): InboxReaders {
  return {
    fetchApprovals: vi.fn(async (): Promise<Hold[]> => []),
    fetchFleetStatus: vi.fn(async () => FLEET),
    fetchBudget: vi.fn(async () => BUDGET),
    fetchSpend: vi.fn(async () => SPEND),
    ...over
  };
}

describe('loadInboxReads', () => {
  it('populates every read-allowed surface when permitted', async () => {
    const holds: Hold[] = [{ issue_num: 1, estimate_usd: 2, basis: 'x' }];
    const r = await loadInboxReads(readers({ fetchApprovals: vi.fn(async () => holds) }));
    expect(r.holds).toEqual({ ok: true, data: holds });
    expect(r.fleet).toEqual(FLEET);
    expect(r.budget).toEqual(BUDGET);
    expect(r.spend).toEqual(SPEND);
  });

  it('distinguishes an empty hold list from a missing session', async () => {
    const r = await loadInboxReads(readers());
    // Loopback read permitted, list simply empty — NOT a dash-because-signed-out.
    expect(r.holds).toEqual({ ok: true, data: [] });
  });

  it('marks holds unavailable when the read is not permitted (signed out under --expose)', async () => {
    const r = await loadInboxReads(
      readers({
        fetchApprovals: vi.fn(async () => {
          throw new Error('/api/approvals -> 401');
        })
      })
    );
    expect(r.holds).toEqual({ ok: false });
    // Other reads still populate — one 401 must not blank the page.
    expect(r.fleet).toEqual(FLEET);
    expect(r.budget).toEqual(BUDGET);
    expect(r.spend).toEqual(SPEND);
  });

  it('fails each read soft and independently', async () => {
    const r = await loadInboxReads(
      readers({
        fetchFleetStatus: vi.fn(async () => {
          throw new Error('boom');
        })
      })
    );
    expect(r.fleet).toBeNull();
    expect(r.budget).toEqual(BUDGET);
    expect(r.holds).toEqual({ ok: true, data: [] });
  });
});
