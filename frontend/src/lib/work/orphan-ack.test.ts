/**
 * #3453 — which Work rows offer the orphan-PR acknowledge, and the row leaving
 * once it is acknowledged.
 *
 * Read over the committed fixture set plus one orphan PR entry, the shape the
 * server serves (`issue_num` and `item_id` at their defaults, the PR's number
 * and URL set), through the list Work actually draws (`mergeWorkList`).
 */
import { describe, expect, it } from 'vitest';
import dashboardJson from '../../../fixtures/factory/dashboard.json';
import projectionJson from '../../../fixtures/factory/dashboard-work.json';
import type { Dashboard, DashboardRow, WorkProjection, WorkView } from '$lib/api';
import { mergeWorkList, selectedRow } from './list';
import { orphanAck, withoutAckedOrphans } from './orphan-ack';

const base = dashboardJson as unknown as Dashboard;
const projection = projectionJson as unknown as WorkProjection;
const PR = 2608;
const PR_URL = `https://github.com/thossullivan/kew/pull/${PR}`;

const orphanEntry: DashboardRow = {
  ...base.rows[0],
  type: 'orphan_pr',
  consequence: 'hygiene',
  reason: 'orphan PR — no kew run recorded on its branch',
  sentence: 'orphan PR — no kew run recorded on its branch',
  primary_action: 'ack',
  issue_num: null,
  item_id: '',
  epic_ref: null,
  pr_number: PR,
  url: PR_URL,
  attention_kind: '',
  label: '',
  command: '',
  title: ''
};

const withOrphan: Dashboard = {
  ...base,
  rows: [...base.rows, orphanEntry],
  needs_you_no_epic: 1
};

const prUrl = (pr: number) => `https://github.com/thossullivan/kew/pull/${pr}`;

/**
 * The fixture's first view, as an item that offers `acknowledge_orphan` for the
 * orphan PR `target`, with `latest` as its latest run's own PR (`pr_url`). The
 * two differ when an earlier branch of the item left the orphan and a later run
 * published another PR.
 */
function offeringProjection(target: number, latest: number): WorkProjection {
  const [first, ...rest] = projection.views;
  const linked: WorkView = {
    ...first,
    artifact: { ...first.artifact, pr_url: prUrl(latest), orphan: true },
    actions: [
      {
        action: 'acknowledge_orphan',
        intent: 'record that a person has seen this unbacked artifact',
        target_kind: 'orphan_pr',
        target_ref: String(target),
        safest: true,
        requires_reason: false
      }
    ]
  };
  return { ...projection, views: [linked, ...rest] };
}

/** The fixture's first view, as an item whose run left the orphan behind. */
const linkedProjection = (): WorkProjection => offeringProjection(PR, PR);

const rowsOf = (dashboard: Dashboard | null, proj: WorkProjection = projection) =>
  mergeWorkList({ projection: proj, dashboard, runs: [] });

describe('orphanAck', () => {
  it('gives an unlinked orphan entry row its own Acknowledge for its PR', () => {
    const rows = rowsOf(withOrphan);
    const row = selectedRow(rows, `orphan_pr:${PR}`);
    expect(row).not.toBeNull();
    expect(orphanAck(row, rows)).toEqual({ kind: 'ack', pr: PR, url: PR_URL });
  });

  it('offers no Open PR link for a served URL safeHref refuses', () => {
    const dashboard: Dashboard = {
      ...withOrphan,
      rows: [...base.rows, { ...orphanEntry, url: 'javascript:alert(1)' }]
    };
    const rows = rowsOf(dashboard);
    expect(orphanAck(selectedRow(rows, `orphan_pr:${PR}`), rows)).toEqual({
      kind: 'ack',
      pr: PR,
      url: null
    });
  });

  it('points a linked orphan at the item that offers the acknowledge, so it has one control', () => {
    const proj = linkedProjection();
    const rows = rowsOf(withOrphan, proj);
    const item = proj.views[0].identity.item_id;
    const entry = selectedRow(rows, `orphan_pr:${PR}`);
    expect(orphanAck(entry, rows)).toEqual({
      kind: 'linked',
      pr: PR,
      itemId: item,
      ref: selectedRow(rows, item)?.ref
    });
    // The item row's control is its header's `acknowledge_orphan`, not a second one here.
    expect(orphanAck(selectedRow(rows, item), rows)).toBeNull();
  });

  it('keeps its own Acknowledge when the item offers the acknowledge for another PR', () => {
    // The item's latest run published this very PR number, but its offered
    // acknowledge is for a different orphan: that header control is not this
    // entry's, so the entry row keeps its own.
    const OTHER = 1;
    const rows = rowsOf(withOrphan, offeringProjection(OTHER, PR));
    expect(orphanAck(selectedRow(rows, `orphan_pr:${PR}`), rows)).toEqual({
      kind: 'ack',
      pr: PR,
      url: PR_URL
    });
  });

  it('draws no second control when the item acknowledges this PR under another run’s PR', () => {
    // Item ran on branch b1, where someone opened this PR with no recorded run;
    // a later run on b2 published PR #60. The server's `pr_url` is #60's, and
    // its `acknowledge_orphan` targets this PR — so the header holds the one
    // control and the entry row points at it.
    const proj = offeringProjection(PR, 60);
    const rows = rowsOf(withOrphan, proj);
    const item = proj.views[0].identity.item_id;
    expect(orphanAck(selectedRow(rows, `orphan_pr:${PR}`), rows)).toEqual({
      kind: 'linked',
      pr: PR,
      itemId: item,
      ref: selectedRow(rows, item)?.ref
    });
  });

  it('offers nothing on a row that is not an orphan PR, or on no row', () => {
    const rows = rowsOf(withOrphan);
    for (const row of rows.filter((r) => r.itemId !== `orphan_pr:${PR}`)) {
      expect(orphanAck(row, rows)).toBeNull();
    }
    expect(orphanAck(null, rows)).toBeNull();
  });
});

describe('withoutAckedOrphans', () => {
  it('takes an acknowledged orphan out of the list and the fleet-wide count', () => {
    const after = withoutAckedOrphans(withOrphan, new Set([PR]));
    expect(after?.rows.some((r) => r.type === 'orphan_pr')).toBe(false);
    expect(after?.needs_you_no_epic).toBe(0);
    expect(selectedRow(rowsOf(after), `orphan_pr:${PR}`)).toBeNull();
    // Every other wait stays.
    expect(after?.rows).toEqual(base.rows);
  });

  it('hands back the same reading when nothing it serves was acknowledged', () => {
    expect(withoutAckedOrphans(withOrphan, new Set())).toBe(withOrphan);
    expect(withoutAckedOrphans(withOrphan, new Set([1]))).toBe(withOrphan);
    expect(withoutAckedOrphans(null, new Set([PR]))).toBeNull();
  });
});
