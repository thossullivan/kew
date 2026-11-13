import { describe, expect, it } from 'vitest';
import type { Dashboard, DashboardRow, Run } from '$lib/api';
import {
  countLabel,
  CURSOR_COLOR,
  cursorTone,
  FLEET_STOPPED,
  fleetStop,
  headerCounts,
  JUDGMENT_ROW_SOURCES,
  UNREADABLE
} from './header-state';
import { bannerNotices } from '$lib/dashboard';

function row(overrides: Partial<DashboardRow> = {}): DashboardRow {
  return {
    type: 'pr_review',
    consequence: 'blocking',
    age: '2026-09-24T15:23:18Z',
    reason: 'PR #2059 is waiting on you',
    primary_action: 'review',
    issue_num: 2055,
    estimate_usd: null,
    epic_ref: null,
    url: null,
    pr_number: 2059,
    ...overrides
  };
}

function dashboard(
  rows: DashboardRow[],
  repo: string | null = 'thossullivan/kew',
  unavailable: string[] = []
): Dashboard {
  return {
    banner: { fleet_stopped: false, no_live_driver: false },
    rows,
    glance: {
      fleet: null,
      caps: null,
      routing: [],
      ready_queue: [],
      queue_candidates: [],
      recent_completions: [],
      pressure: repo === null ? null : ({ delivery_repo: repo } as never)
    },
    as_of: {},
    unavailable
  };
}

// A run as `/api/runs` serves it: the raw backend RunStatus, plus the start
// stamp `displayStatus` needs to tell a started run from a queued one.
function run(status: string, startedAt: string | null = '2026-09-24T15:00:00Z'): Run {
  return { status, started_at: startedAt } as Run;
}

// Every value of the backend enum (kew/src/kew/run_store.py `RunStatus`).
// `running` is deliberately not in it — there is no such server status.
const RUN_STATUSES = [
  'pending',
  'awaiting_publication',
  'success',
  'failure',
  'refused',
  'budget_exceeded',
  'budget_stopped',
  'budget_stop',
  'killed'
];

describe('headerCounts', () => {
  it('needs-you is the count of dashboard judgment rows', () => {
    expect(headerCounts(dashboard([row(), row({ type: 'budget' })]), []).needsYou).toBe(2);
  });

  it('an empty judgment queue is zero, not unreadable', () => {
    expect(headerCounts(dashboard([]), []).needsYou).toBe(0);
  });

  it('working is the count of runs that are actually in flight', () => {
    const runs = [run('pending'), run('success'), run('pending'), run('failure')];
    expect(headerCounts(dashboard([]), runs).working).toBe(2);
  });

  // Regression: the counter used to match `status === 'running'`, a literal the
  // API never sends, so a busy fleet read 0 forever against a real serve.
  it('counts a live fleet made only of real backend statuses', () => {
    const runs = RUN_STATUSES.map((s) => run(s));
    expect(RUN_STATUSES).not.toContain('running');
    expect(headerCounts(dashboard([]), runs).working).toBe(1);
  });

  it('a queued run — pending but never started — is not yet working', () => {
    expect(headerCounts(dashboard([]), [run('pending', null)]).working).toBe(0);
  });

  it('no terminal status ever counts as working', () => {
    const terminal = RUN_STATUSES.filter((s) => s !== 'pending').map((s) => run(s));
    expect(headerCounts(dashboard([]), terminal).working).toBe(0);
  });

  it('no runs at all is zero working', () => {
    expect(headerCounts(dashboard([]), []).working).toBe(0);
  });

  it('a null dashboard reads as unreadable, never zero', () => {
    const counts = headerCounts(null, []);
    expect(counts.needsYou).toBeNull();
    expect(counts.working).toBe(0);
  });

  it('null runs read as unreadable, never zero', () => {
    expect(headerCounts(dashboard([row()]), null).working).toBeNull();
  });

  // Regression: `rows.length` on a fail-soft payload whose row sources failed
  // is a confident 0 for a queue nobody read — the exact null-vs-zero mistake
  // this module exists to prevent.
  it('an unreadable judgment-row source reads as unreadable, never zero', () => {
    expect(headerCounts(dashboard([], 'o/r', ['approval_holds']), []).needsYou).toBeNull();
  });

  it('a partially read queue is unreadable, not a short count', () => {
    const counts = headerCounts(dashboard([row()], 'o/r', ['orphan_prs']), []);
    expect(counts.needsYou).toBeNull();
  });

  it('an unreadable work projection blanks the count, though it renders no rows itself', () => {
    expect(headerCounts(dashboard([row()], 'o/r', ['pr_reviews']), []).needsYou).toBeNull();
  });

  it('every judgment-row source alone is enough to make the count unreadable', () => {
    for (const source of JUDGMENT_ROW_SOURCES) {
      expect(headerCounts(dashboard([row()], 'o/r', [source]), []).needsYou, source).toBeNull();
    }
  });

  // The list itself is held to `assemble` by kew/tests/test_factory_header_parity.py,
  // which reads this array out of header-state.ts; this pins what it is today so
  // an edit here shows up as an intended change rather than a silent one.
  it('watches the work projection and all eight row sources, and nothing else', () => {
    expect([...JUDGMENT_ROW_SOURCES].sort()).toEqual([
      'approval_holds',
      'budget_holds',
      'epic_waits',
      'merge_queue',
      'orphan_prs',
      'pr_reviews',
      'stalled_epics',
      'triage_proposals',
      'work_rows'
    ]);
  });

  it('an unreadable glance source leaves the judgment count alone', () => {
    for (const source of ['fleet', 'caps', 'routing', 'ready_queue', 'recent_completions']) {
      expect(headerCounts(dashboard([row()], 'o/r', [source]), []).needsYou, source).toBe(1);
    }
  });

  it('keeps the rows it did read as a floor under an unreadable count', () => {
    const rows = [row(), row({ type: 'budget' })];
    const counts = headerCounts(dashboard(rows, 'o/r', ['orphan_prs']), []);
    expect(counts.needsYou).toBeNull();
    expect(counts.needsYouFloor).toBe(2);
  });

  it('a fully read queue has the count as its own floor', () => {
    expect(headerCounts(dashboard([row()]), []).needsYouFloor).toBe(1);
    expect(headerCounts(dashboard([]), []).needsYouFloor).toBe(0);
  });

  it('a dashboard that never arrived knows of no rows at all', () => {
    expect(headerCounts(null, []).needsYouFloor).toBe(0);
  });

  it('takes the repo name from the review-pressure reading', () => {
    expect(headerCounts(dashboard([]), []).repo).toBe('thossullivan/kew');
  });

  it('an absent pressure reading leaves the repo name unknown', () => {
    expect(headerCounts(dashboard([], null), []).repo).toBeNull();
    expect(headerCounts(null, null).repo).toBeNull();
  });
});

describe('countLabel', () => {
  it('renders an em dash for an unreadable count, never 0', () => {
    expect(countLabel(null)).toBe(UNREADABLE);
    expect(countLabel(null)).not.toBe('0');
  });

  it('renders a real zero as 0', () => {
    expect(countLabel(0)).toBe('0');
  });

  it('renders a count as its own digits', () => {
    expect(countLabel(12)).toBe('12');
  });
});

describe('cursorTone', () => {
  it('is magenta when the fleet needs a human', () => {
    expect(cursorTone({ needsYou: 1, working: 2 })).toBe('attention');
  });

  it('is magenta when the viewed item needs a human even with a quiet fleet', () => {
    expect(cursorTone({ needsYou: 0, working: 0, itemNeedsYou: true })).toBe('attention');
  });

  it('is cyan when nothing needs a human but something is working', () => {
    expect(cursorTone({ needsYou: 0, working: 1 })).toBe('working');
  });

  it('is muted when the factory is idle', () => {
    expect(cursorTone({ needsYou: 0, working: 0 })).toBe('idle');
  });

  it('is muted when neither reading could be taken — an unknown is not a signal', () => {
    expect(cursorTone({ needsYou: null, working: null })).toBe('idle');
  });

  it('still shows working when only the judgment queue is unreadable', () => {
    expect(cursorTone({ needsYou: null, working: 3 })).toBe('working');
  });

  // Regression: a payload that lost one row source blanks `needsYou`, and the
  // cursor read that as idle — muted, announcing "factory state: idle" — over
  // judgment rows the other sources had delivered. An unknown total is not a
  // reason to stop showing the rows that arrived.
  it('is magenta for rows that were read even when the total is unreadable', () => {
    expect(cursorTone({ needsYou: null, working: 0, needsYouFloor: 2 })).toBe('attention');
  });

  it('rations magenta to the human: a read row outranks a busy fleet', () => {
    expect(cursorTone({ needsYou: null, working: 3, needsYouFloor: 1 })).toBe('attention');
  });

  it('stays quiet when a partial payload read no judgment rows at all', () => {
    expect(cursorTone({ needsYou: null, working: 0, needsYouFloor: 0 })).toBe('idle');
  });

  // The header composes these two itself (FactoryHeader.svelte spreads the
  // counts into the context), so the regression is pinned end to end: the same
  // payload must light the cursor and still print an em dash.
  it('lights the cursor from a real partial payload while the counter stays honest', () => {
    const counts = headerCounts(
      dashboard([row({ type: 'budget' }), row({ type: 'budget' })], 'o/r', ['orphan_prs']),
      []
    );
    expect(cursorTone(counts)).toBe('attention');
    expect(countLabel(counts.needsYou)).toBe(UNREADABLE);
  });

  it('every tone maps to a theme token, never a literal hex', () => {
    for (const tone of ['attention', 'working', 'idle'] as const) {
      expect(CURSOR_COLOR[tone]).toMatch(/^var\(--[a-z0-9-]+\)$/);
    }
    expect(CURSOR_COLOR.attention).toBe('var(--accent)');
    expect(CURSOR_COLOR.working).toBe('var(--accent-2)');
  });
});

describe('fleetStop', () => {
  // FUI-26: after the cutover the header is the only surface on every screen, so
  // it is where a safety stop is announced and where the resume lives.
  it('is null while the fleet is running', () => {
    expect(fleetStop(dashboard([]))).toBeNull();
  });

  it("is the console's own sentence when the fleet is stopped", () => {
    const d = dashboard([]);
    d.banner.fleet_stopped = true;
    const [notice] = bannerNotices(d.banner);
    expect(fleetStop(d)).toEqual({ message: notice.message });
  });

  it('is null for an unread dashboard, so no poll invents an alarm', () => {
    expect(fleetStop(null)).toBeNull();
  });

  it('ignores the other banner, which is a notice and not a stop', () => {
    const d = dashboard([]);
    d.banner.no_live_driver = true;
    expect(fleetStop(d)).toBeNull();
  });

  it('words the header terse, and keeps the sentence for the title', () => {
    expect(FLEET_STOPPED).toBe('fleet stopped');
  });
});
