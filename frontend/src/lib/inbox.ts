// Inbox read loading (CS-3). Loopback (no --expose) permits unauthenticated
// reads via `read_guard`, so the fleet/caps/spend/holds surfaces populate
// regardless of session — only approval ACTIONS gate on auth. Each read fails
// soft so a single 401 (which only happens under --expose signed-out) never
// blanks the rest of the page.

import type { Hold, FleetStatus, BudgetMap, SpendWindows } from '$lib/api';

/**
 * A guarded read either returns its data or is unavailable. `ok: false`
 * means the read was not permitted (signed out under --expose) — distinct
 * from `ok: true, data: []` (permitted, simply empty). The UI must render
 * these differently: a sign-in prompt vs. an honest "nothing here", never a
 * bare dash because auth is missing.
 */
export type ReadResult<T> = { ok: true; data: T } | { ok: false };

export type InboxReaders = {
  fetchApprovals: () => Promise<Hold[]>;
  fetchFleetStatus: () => Promise<FleetStatus>;
  fetchBudget: () => Promise<BudgetMap>;
  fetchSpend: () => Promise<SpendWindows>;
};

export type InboxReads = {
  holds: ReadResult<Hold[]>;
  fleet: FleetStatus | null;
  budget: BudgetMap;
  spend: SpendWindows | null;
};

/** Fetch every read-allowed inbox surface, regardless of auth. */
export async function loadInboxReads(api: InboxReaders): Promise<InboxReads> {
  const [holds, fleet, budget, spend] = await Promise.all([
    api
      .fetchApprovals()
      .then((data): ReadResult<Hold[]> => ({ ok: true, data }))
      .catch((): ReadResult<Hold[]> => ({ ok: false })),
    api.fetchFleetStatus().catch(() => null),
    api.fetchBudget().catch(() => ({}) as BudgetMap),
    api.fetchSpend().catch(() => null)
  ]);
  return { holds, fleet, budget, spend };
}
