/**
 * #3453 — acknowledging an orphan PR from Work, as a pure model.
 *
 * An `orphan_pr` needs-you entry names a pull request no kew run backs. Work
 * draws it as an entry row (`entryRows`), and until now only `/console` and the
 * TUI could clear it: Work's item actions offer `acknowledge_orphan` only on an
 * item whose recorded run is on the orphan's branch, and an orphan with the
 * `no-record` reason is linked to no item at all.
 *
 * So the entry row carries its own Acknowledge, which sends the same ledger-only
 * `ackOrphan` the console sends — EXCEPT for an orphan an item already offers to
 * acknowledge. That item's header has the governed `acknowledge_orphan` action,
 * and a second button for the same PR on the entry row would be two controls for
 * one decision; the entry row points at the item instead.
 */
import type { Dashboard } from '$lib/api';
import { offeredActions } from '$lib/dashboard';
import { safeHref } from '$lib/safe-href';
import type { WorkRow } from './list';

/** What the orphan control on a row is: its own press, a pointer to the item that has one, or nothing. */
export type OrphanAck =
  | { kind: 'ack'; pr: number; url: string | null }
  | { kind: 'linked'; pr: number; itemId: string; ref: string };

/**
 * Whether `row`'s item offers the governed acknowledge for pull request `pr`.
 *
 * Read off the offered action's own target, never off `artifact.pr_url`: that
 * is the item's latest run's PR, while `acknowledge_orphan` targets the item's
 * first orphan (`_TARGET_REF` in kew/service/work.py). An item whose old branch
 * left orphan #50 and whose later run published #60 offers the acknowledge for
 * #50 with #60 in `pr_url`.
 */
function offersAckFor(row: WorkRow, pr: number): boolean {
  const view = row.view;
  if (view === null) return false;
  return offeredActions(view).some(
    (a) =>
      a.action === 'acknowledge_orphan' &&
      a.target_kind === 'orphan_pr' &&
      a.target_ref === String(pr)
  );
}

/**
 * The orphan control `row` carries, read against the whole list `rows`.
 *
 * Null for a row that is not an orphan-PR entry, and for an item row: an item
 * that offers `acknowledge_orphan` already draws it in its header, which is that
 * row's one acknowledge control.
 */
export function orphanAck(row: WorkRow | null, rows: readonly WorkRow[]): OrphanAck | null {
  const entry = row?.entry ?? null;
  if (row === null || entry === null || entry.type !== 'orphan_pr' || entry.pr_number == null) {
    return null;
  }
  const pr = entry.pr_number;
  const owner = rows.find((r) => r !== row && offersAckFor(r, pr));
  if (owner !== undefined) return { kind: 'linked', pr, itemId: owner.itemId, ref: owner.ref };
  return { kind: 'ack', pr, url: safeHref(entry.url) };
}

/**
 * The dashboard with the orphan entries for `acked` pull requests taken out.
 *
 * An acknowledged orphan leaves on the WRITE RESPONSE (`ackRemovesRow`), as it
 * does on the console: the next read no longer serves it, but until that read
 * lands the row would stand there as though the press had not landed. The same
 * object comes back when nothing is taken out, so nothing downstream re-renders.
 */
export function withoutAckedOrphans(
  dashboard: Dashboard | null,
  acked: ReadonlySet<number>
): Dashboard | null {
  if (dashboard === null || acked.size === 0) return dashboard;
  const gone = (r: Dashboard['rows'][number]): boolean =>
    r.type === 'orphan_pr' && r.pr_number != null && acked.has(r.pr_number);
  const taken = dashboard.rows.filter(gone).length;
  if (taken === 0) return dashboard;
  const rows = dashboard.rows.filter((r) => !gone(r));
  // An orphan entry names no epic, so it is counted in the fleet-wide bucket.
  const noEpic = dashboard.needs_you_no_epic;
  return noEpic === undefined
    ? { ...dashboard, rows }
    : { ...dashboard, rows, needs_you_no_epic: Math.max(0, noEpic - taken) };
}
