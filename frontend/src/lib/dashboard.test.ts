import { describe, expect, it } from 'vitest';
import {
  orderRows,
  bannerNotices,
  typeLabel,
  KNOWN_ROW_TYPES,
  pruneKeyedState,
  pruneDashboardState,
  ROW_TYPE_SOURCES,
  rowKey
} from './dashboard';
import type { Dashboard, DashboardRow } from './api';
import { JUDGMENT_ROW_SOURCES } from './shell/header-state';

const row = (over: Partial<DashboardRow>): DashboardRow => ({
  type: 'approval_hold',
  consequence: '',
  age: '2026-07-16T12:00:00Z',
  reason: '',
  primary_action: '',
  issue_num: null,
  estimate_usd: null,
  epic_ref: null,
  url: null,
  pr_number: null,
  ...over
});

const dash = (over: Partial<Dashboard> = {}): Dashboard => ({
  banner: { fleet_stopped: false, no_live_driver: false },
  rows: [],
  glance: { fleet: null, caps: null, routing: [], ready_queue: [], queue_candidates: [], recent_completions: [] },
  as_of: {},
  unavailable: [],
  ...over
});

describe('orderRows', () => {
  it('orders by consequence class: money → epic → review → hygiene', () => {
    const rows = [
      row({ type: 'orphan_pr' }), // hygiene
      row({ type: 'pr_review' }), // review
      row({ type: 'stalled_epic' }), // epic
      row({ type: 'budget_hold' }) // money
    ];
    expect(orderRows(rows).map((r) => r.type)).toEqual([
      'budget_hold',
      'stalled_epic',
      'pr_review',
      'orphan_pr'
    ]);
  });

  it('breaks consequence ties by age, oldest first', () => {
    const rows = [
      row({ type: 'budget_hold', issue_num: 2, age: '2026-07-16T12:00:00Z' }),
      row({ type: 'approval_hold', issue_num: 1, age: '2026-07-16T09:00:00Z' })
    ];
    // both are money-class; the 09:00 one is older → first
    expect(orderRows(rows).map((r) => r.issue_num)).toEqual([1, 2]);
  });

  it('honours an explicit consequence field over the type default', () => {
    const rows = [
      row({ type: 'orphan_pr', consequence: 'money', issue_num: 1 }),
      row({ type: 'budget_hold', consequence: 'hygiene', issue_num: 2 })
    ];
    expect(orderRows(rows).map((r) => r.issue_num)).toEqual([1, 2]);
  });

  it('drops unknown row types silently (additivity)', () => {
    const rows = [row({ type: 'some_future_type' }), row({ type: 'budget_hold' })];
    expect(orderRows(rows).map((r) => r.type)).toEqual(['budget_hold']);
  });

  it('does not mutate its input', () => {
    const rows = [row({ type: 'orphan_pr' }), row({ type: 'budget_hold' })];
    const snapshot = rows.map((r) => r.type);
    orderRows(rows);
    expect(rows.map((r) => r.type)).toEqual(snapshot);
  });
});

describe('typeLabel', () => {
  it('pluralises known types', () => {
    expect(typeLabel('budget_hold', 1)).toBe('budget hold');
    expect(typeLabel('budget_hold', 3)).toBe('budget holds');
    expect(typeLabel('pr_review', 2)).toBe('PR reviews');
  });
  it('falls back to the raw type for unknown types', () => {
    expect(typeLabel('mystery', 2)).toBe('mystery');
  });
});

describe('bannerNotices', () => {
  it('is empty when nothing is wrong', () => {
    expect(bannerNotices({ fleet_stopped: false, no_live_driver: false })).toEqual([]);
  });
  it('surfaces fleet_stopped as a danger notice', () => {
    const n = bannerNotices({ fleet_stopped: true, no_live_driver: false });
    expect(n).toHaveLength(1);
    expect(n[0].kind).toBe('fleet_stopped');
    expect(n[0].tone).toBe('danger');
  });
  it('surfaces no_live_driver as a warn notice', () => {
    const n = bannerNotices({ fleet_stopped: false, no_live_driver: true });
    expect(n).toHaveLength(1);
    expect(n[0].kind).toBe('no_live_driver');
    expect(n[0].tone).toBe('warn');
  });
  it('shows fleet_stopped before no_live_driver when both fire', () => {
    const n = bannerNotices({ fleet_stopped: true, no_live_driver: true });
    expect(n.map((x) => x.kind)).toEqual(['fleet_stopped', 'no_live_driver']);
  });
});

describe('KNOWN_ROW_TYPES', () => {
  it('is the contract row types, in consequence order', () => {
    expect([...KNOWN_ROW_TYPES]).toEqual([
      'approval_hold',
      'budget_hold',
      'stalled_epic',
      'merge_queue_suspect',
      'pr_review',
      'work_exception',
      'epic_wait',
      'orphan_pr',
      'work_unreadable',
      'triage_proposal'
    ]);
  });
});

// --- Canonical exception workspace (I19/I20) --------------------------------
// Work and the palette consume the projection's verdict and the projection's
// offered actions. Nothing below re-derives customer state, capacity or a next step
// from raw run/PR material — that is the whole point of these helpers.

import {
  exceptionLabel,
  exceptionTone,
  workActionLabel,
  offeredActions,
  workActionError,
  applyWorkActionResult,
  CANONICAL_WORK_ACTIONS
} from './dashboard';
import type { ReviewPressure, WorkActionResponse, WorkView } from './api';

const obs = (over: Partial<WorkView['observations'][number]> = {}) => ({
  dimension: 'judgment',
  availability: 'available',
  provenance: 'observed',
  observed_at: '2026-08-20T10:00:00Z',
  detail: null,
  ...over
});

const pressure = (over: Partial<ReviewPressure> = {}): ReviewPressure => ({
  delivery_repo: 'acme/app',
  engaged: true,
  countable: true,
  used: 2,
  total: 5,
  held: 1,
  oldest_hold_age_seconds: 7200,
  diff_burden_lines: 1240,
  saturated: false,
  reasons: [],
  enforcement: 'inactive',
  enforcement_detail: '',
  policy_generation: 3,
  observation: obs({ dimension: 'review_pressure' }),
  ...over
});

const view = (over: Partial<WorkView> = {}): WorkView => ({
  identity: {
    source_tracker: 'github',
    item_id: '42',
    item_ref: '#42',
    delivery_repo: 'acme/app',
    epic_ref: null,
    node_id: null,
    run_id: 'r1',
    attempt_number: 1
  },
  source: { tracker: 'github', item_state: 'open', synchronization: 'pending', observation: obs() },
  route: {
    runner: 'claude-code',
    model: 'claude-opus-5',
    reported_model: 'claude-opus-5',
    containment: 'contained',
    provenance: 'declared',
    claim: 'honored'
  },
  execution: {
    state: 'completed',
    run_status: 'awaiting_publication',
    attempts: 1,
    started_at: null,
    finished_at: null,
    error_category: ''
  },
  artifact: { state: 'preserved', publication: 'pending', branch: 'b', pr_url: null, orphan: false },
  judgment: { state: 'not_applicable', observation: obs(), decided_at: null },
  outcome: { state: 'in_progress', detail: '' },
  integration: { checks: 'unknown', mergeability: 'unknown', review_decision: 'unknown', observation: obs() },
  readiness: { state: 'unproven', fingerprint: null, evidence_ref: null, detail: '', observation: obs() },
  no_change: {
    state: 'not_applicable',
    judgment_state: '',
    policy: '',
    reasons: [],
    sync_outstanding: 0,
    sync_failed: 0,
    effects_outstanding: 0
  },
  review_pressure: pressure(),
  exception: { state: 'awaiting_publication', detail: 'preserved but unpublished', blocking: false, dimension: 'artifact' },
  safest_action: { action: 'finalize_publication', reason: 'publish what already exists', blocked_by: null },
  actions: [
    {
      action: 'finalize_publication',
      intent: 'publish the preserved artifact this run already produced',
      target_kind: 'run',
      target_ref: 'r1',
      safest: true,
      requires_reason: false
    }
  ],
  view_revision: 'rev-abc',
  composed_at: '2026-08-20T10:00:01Z',
  observations: [obs()],
  ...over
});

describe('canonical exception row types', () => {
  it('admits the projection-derived row types the server now emits', () => {
    expect(KNOWN_ROW_TYPES).toContain('work_exception');
    expect(KNOWN_ROW_TYPES).toContain('work_unreadable');
  });

  it('orders a work exception with reviews and an unreadable item with hygiene', () => {
    const rows = [
      row({ type: 'work_unreadable' }),
      row({ type: 'work_exception' }),
      row({ type: 'budget_hold' })
    ];
    expect(orderRows(rows).map((r) => r.type)).toEqual([
      'budget_hold',
      'work_exception',
      'work_unreadable'
    ]);
  });

  it('keeps an unreadable item visible rather than dropping it as healthy', () => {
    expect(orderRows([row({ type: 'work_unreadable' })])).toHaveLength(1);
  });

  it('labels the new types in singular and plural', () => {
    expect(typeLabel('work_exception', 1)).toBe('exception');
    expect(typeLabel('work_exception', 3)).toBe('exceptions');
    expect(typeLabel('work_unreadable', 1)).toBe('unreadable item');
  });
});

describe('exceptionLabel', () => {
  it('names every exception the projection can raise', () => {
    const cases: Record<string, string> = {
      publication_risk_hold: 'publication risk held',
      awaiting_publication: 'awaiting publication',
      no_change_pending: 'no-change awaiting judgment',
      no_change_authorized: 'no-change sync outstanding',
      no_change_sync_failed: 'no-change sync failed',
      source_desynchronized: 'tracker sync pending',
      checks_failing: 'checks failing',
      merge_conflict: 'merge conflict',
      orphan_artifact: 'orphan artifact',
      route_substituted: 'route substituted',
      review_capacity_saturated: 'review capacity saturated',
      remote_unavailable: 'remote unavailable',
      readiness_refuted: 'route boundary refuted',
      terminal_refused: 'refused',
      fleet_stopped: 'fleet stopped'
    };
    for (const [state, label] of Object.entries(cases)) {
      expect(exceptionLabel(state)).toBe(label);
    }
  });

  it('renders an unknown future exception readably instead of hiding it', () => {
    expect(exceptionLabel('some_new_thing')).toBe('some new thing');
  });

  it('takes its tone from the projection, never from the exception name', () => {
    expect(exceptionTone({ state: 'merge_conflict', detail: '', blocking: true, dimension: 'integration' })).toBe('danger');
    expect(exceptionTone({ state: 'awaiting_publication', detail: '', blocking: false, dimension: 'artifact' })).toBe('warn');
    expect(exceptionTone({ state: 'none', detail: '', blocking: false, dimension: '' })).toBe('neutral');
  });
});

describe('workActionLabel', () => {
  it('labels every action the projection is able to offer', () => {
    expect(CANONICAL_WORK_ACTIONS.map((a) => workActionLabel(a))).toEqual([
      'Finalize publication',
      'Confirm publication risk',
      'Confirm no-change',
      'Reject no-change',
      'Retry source sync',
      'Acknowledge orphan',
      'Dispatch',
      'Resume fleet'
    ]);
  });

  it('renders an action it does not know rather than inventing a friendlier one', () => {
    expect(workActionLabel('rehydrate_widget')).toBe('rehydrate widget');
  });
});

describe('offeredActions', () => {
  it('is exactly what the projection offered, safest first', () => {
    const v = view({
      actions: [
        { action: 'confirm_no_change', intent: 'a', target_kind: 'no_change_judgment', target_ref: 'j1', safest: true, requires_reason: false },
        { action: 'reject_no_change', intent: 'b', target_kind: 'no_change_judgment', target_ref: 'j1', safest: false, requires_reason: true }
      ]
    });
    expect(offeredActions(v).map((a) => a.action)).toEqual(['confirm_no_change', 'reject_no_change']);
    expect(offeredActions(v)[0].safest).toBe(true);
  });

  it('offers nothing when the projection offered nothing — including for a conflict', () => {
    const v = view({
      exception: { state: 'merge_conflict', detail: 'conflicts with base', blocking: true, dimension: 'integration' },
      safest_action: { action: 'resolve_conflict', reason: 'a person must resolve it', blocked_by: null },
      actions: []
    });
    expect(offeredActions(v)).toEqual([]);
  });

  it('never synthesises a dispatch over work that is already preserved', () => {
    expect(offeredActions(view()).map((a) => a.action)).toEqual(['finalize_publication']);
  });

  it('acts on the target the projection read back, not one a caller composed', () => {
    expect(offeredActions(view())[0].target_ref).toBe('r1');
  });
});

describe('workActionError', () => {
  it('classifies a stale revision as re-read-and-decide-again', () => {
    const e = workActionError(409, 'view_revision is no longer current');
    expect(e.kind).toBe('stale');
    expect(e.message).toContain('no longer current');
  });

  it('classifies an approver refusal without leaking the allowlist', () => {
    expect(workActionError(403, 'actor not in [approval].approvers').kind).toBe('not_approver');
    expect(workActionError(403, 'actor not in [approval].approvers').message).not.toContain('[approval]');
  });

  it('classifies a missing session, a bad request, a transport failure and the rest', () => {
    expect(workActionError(401, '').kind).toBe('unauthenticated');
    expect(workActionError(400, 'bad').kind).toBe('invalid');
    expect(workActionError(0, 'offline').kind).toBe('network');
    expect(workActionError(500, '').kind).toBe('server');
  });
});

describe('applyWorkActionResult', () => {
  const okResult = (over: Record<string, unknown> = {}): WorkActionResponse => ({
    ok: true,
    data: {
      action: 'finalize_publication',
      target_kind: 'run',
      target_ref: 'r1',
      applied: true,
      idempotent: false,
      detail: 'publication requested',
      expected_revision: 'rev-abc',
      view: view(),
      ...over
    }
  });

  it('renders the post-write projection the server returned, not a guess', () => {
    const out = applyWorkActionResult(okResult());
    expect(out.view?.view_revision).toBe('rev-abc');
    expect(out.chip).toBe('applied');
    expect(out.error).toBeNull();
    expect(out.reread).toBe(false);
  });

  it('marks a replayed request as already applied rather than acting twice', () => {
    expect(applyWorkActionResult(okResult({ idempotent: true })).chip).toBe('already applied');
  });

  it('drops the row only when the returned view says the exception is gone', () => {
    const resolved = okResult({
      view: view({ exception: { state: 'none', detail: '', blocking: false, dimension: '' } })
    });
    expect(applyWorkActionResult(resolved).resolved).toBe(true);
    expect(applyWorkActionResult(okResult()).resolved).toBe(false);
  });

  it('asks for a re-read on a stale revision and keeps no stale view around', () => {
    const out = applyWorkActionResult({ ok: false, status: 409, detail: 'view_revision is no longer current' });
    expect(out.reread).toBe(true);
    expect(out.view).toBeNull();
    expect(out.resolved).toBe(false);
    expect(out.error).toContain('no longer current');
  });

  it('surfaces a failure inline without pretending the action landed', () => {
    const out = applyWorkActionResult({ ok: false, status: 500, detail: '' });
    expect(out.reread).toBe(false);
    expect(out.chip).toBeNull();
    expect(out.error).toBeTruthy();
    expect(out.resolved).toBe(false);
  });
});

describe('a dismissal ends when the read it bridges lands', () => {
  // `rowKey` is `type:item_id`, so a re-raised approval hold on #77 carries
  // the identical key the operator's last decision was filed under. Kept for
  // the life of the page, that decision swallows the new question — a
  // surface going quiet about a hold the Monitor's column is drawing.
  const held = row({ type: 'approval_hold', item_id: '77', issue_num: 77 });
  const key = rowKey(held);

  it('keeps the dismissal while the poll still serves the row', () => {
    const dismissed = { [key]: true };
    expect(pruneKeyedState([key], dismissed)).toBe(dismissed);
  });

  it('drops it once the row is gone, so the SAME item can hold again', () => {
    expect(pruneKeyedState([], { [key]: true })).toEqual({});
  });

  it('drops only the keys that left', () => {
    const other = rowKey(row({ type: 'orphan_pr', pr_number: 12 }));
    expect(pruneKeyedState([other], { [key]: 'gone', [other]: 'kept' })).toEqual({
      [other]: 'kept'
    });
  });

  it('returns the same map when nothing is to drop, so a poll churns nothing', () => {
    const state = { [key]: true };
    expect(pruneKeyedState([key, 'extra:1'], state)).toBe(state);
  });
});

describe('a fail-softed row source is not an empty row source', () => {
  // The regression: `/api/dashboard` fail-softs per row source, so a 200 can
  // carry `unavailable: ['approval_holds']` and no approval rows. Pruning the
  // dismissal on that reading drops the bridge across the write, and the
  // decided row is drawn again the moment the source recovers while the
  // read-back still lags — the CS-6 resurrection the dismissal exists to stop.
  const held = row({ type: 'approval_hold', item_id: '77', issue_num: 77 });
  const key = rowKey(held);

  it('keeps the dismissal when the row source did not answer', () => {
    const dismissed = { [key]: true };
    const softFailed = dash({ rows: [], unavailable: ['approval_holds'] });
    expect(pruneDashboardState(softFailed, dismissed)).toBe(dismissed);
  });

  it('drops it on a read that covered the row and came back without it', () => {
    // Another source failing says nothing about approvals, which WERE read.
    const read = dash({ rows: [], unavailable: ['orphan_prs'] });
    expect(pruneDashboardState(read, { [key]: true })).toEqual({});
  });

  it('prunes the sources that answered and holds the one that did not', () => {
    const orphan = rowKey(row({ type: 'orphan_pr', pr_number: 12 }));
    const partial = dash({ rows: [], unavailable: ['approval_holds'] });
    expect(pruneDashboardState(partial, { [key]: true, [orphan]: 'refused' })).toEqual({
      [key]: true
    });
  });

  it('keeps a work row held when the projection behind it failed', () => {
    // `_work_rows` succeeds over a `None` projection and serves nothing, so
    // `work_rows` never appears — `pr_reviews` is the only name that says the
    // review rows were not read.
    const review = rowKey(row({ type: 'pr_review', pr_number: 2137 }));
    const projectionDown = dash({ rows: [], unavailable: ['pr_reviews'] });
    expect(pruneDashboardState(projectionDown, { [review]: true })).toEqual({ [review]: true });
  });

  it('keeps a work row held when the projection could not read its ITEM', () => {
    // The projection fails per item too: `assemble` serves an item in
    // `projection.unreadable` as a `work_unreadable` row while `work_rows` and
    // `pr_reviews` both answer, so the `pr_review` the operator just decided is
    // absent from a read that never looked at it. Read as "the row is gone",
    // the dismissal bridging the write is dropped and the decided row comes
    // back the moment the facts are readable again.
    const review = rowKey(row({ type: 'pr_review', item_id: '77', issue_num: 77 }));
    const unread = dash({ rows: [row({ type: 'work_unreadable', item_id: '77', issue_num: 77 })] });
    expect(pruneDashboardState(unread, { [review]: true })).toEqual({ [review]: true });
  });

  it('keeps a work row whose item is unreadable under a collapsed row', () => {
    // The needs-you set collapses to one entry per item and `work_unreadable`
    // is the lowest consequence there is, so an unreadable item that is also
    // held is served as the HOLD, carrying `work_unread` instead of the
    // `work_unreadable` type. Reading only the type here went blind on exactly
    // the items that had something else wrong with them too.
    const review = rowKey(row({ type: 'pr_review', item_id: '77', issue_num: 77 }));
    const unread = dash({
      rows: [row({ type: 'budget_hold', item_id: '77', issue_num: 77, work_unread: true })]
    });
    expect(pruneDashboardState(unread, { [review]: true })).toEqual({ [review]: true });
  });

  it('holds the unreadable item alone, not every row the projection serves', () => {
    const other = rowKey(row({ type: 'work_exception', item_id: '88', issue_num: 88 }));
    const unread = dash({ rows: [row({ type: 'work_unreadable', item_id: '77', issue_num: 77 })] });
    expect(pruneDashboardState(unread, { [other]: true })).toEqual({});
  });

  it('holds an unreadable item whose id carries colons of its own', () => {
    // `rowKey` is `<type>:<id>` and a non-GitHub item id contains colons, so the
    // item is everything after the FIRST one.
    const review = rowKey(row({ type: 'pr_review', item_id: 'linear:ENG-12' }));
    const unread = dash({ rows: [row({ type: 'work_unreadable', item_id: 'linear:ENG-12' })] });
    expect(pruneDashboardState(unread, { [review]: true })).toEqual({ [review]: true });
  });

  it('does not hold a row from another reader on that same item', () => {
    // An approval hold on #77 is read by `approval_holds`, which answered; the
    // work projection's silence about #77 says nothing about it, and holding it
    // would hide a re-raised hold behind a decision already made.
    const unread = dash({ rows: [row({ type: 'work_unreadable', item_id: '77', issue_num: 77 })] });
    expect(pruneDashboardState(unread, { [key]: true })).toEqual({});
  });

  it('holds a key of a row type it does not know rather than dropping it', () => {
    const future = dash({ rows: [], unavailable: [] });
    expect(pruneDashboardState(future, { 'quota_hold:9': true })).toEqual({ 'quota_hold:9': true });
  });

  it('still drops a dismissal once the row leaves a fully-read dashboard', () => {
    expect(pruneDashboardState(dash(), { [key]: true })).toEqual({});
    const serving = dash({ rows: [held] });
    const dismissed = { [key]: true };
    expect(pruneDashboardState(serving, dismissed)).toBe(dismissed);
  });

  it('names every row type, in the backend vocabulary the header is held to', () => {
    // The source names are `assemble`'s, checked against the list
    // `kew/tests/test_factory_header_parity.py` holds to the backend — so a
    // renamed reader cannot leave this table silently pointing at nothing, and
    // a new row type cannot arrive with no source behind its keys.
    expect(Object.keys(ROW_TYPE_SOURCES).sort()).toEqual([...KNOWN_ROW_TYPES].sort());
    for (const names of Object.values(ROW_TYPE_SOURCES)) {
      for (const name of names) expect(JUDGMENT_ROW_SOURCES).toContain(name);
    }
  });
});

describe('rowKey for a row naming only its title', () => {
  it('keeps two stopped merge queues apart', () => {
    const a = rowKey(row({ type: 'merge_queue_suspect', title: 'merge queue stopped — o/a' }));
    const b = rowKey(row({ type: 'merge_queue_suspect', title: 'merge queue stopped — o/b' }));
    expect(a).not.toBe(b);
    expect(rowKey(row({ type: 'orphan_pr', pr_number: 12, title: 'x' }))).toBe('orphan_pr:12');
  });
});
