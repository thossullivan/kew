import { describe, expect, it } from 'vitest';
import { displayId, labelTone, STATUS_COLOR, statusColor, statusWord } from './render';
import { statusShape, STATUS_MARKS, type StatusMeaning } from '$lib/factory/status';

const STATUSES = [
  'todo',
  'in_progress',
  'in_review',
  'done',
  'failed',
  'held',
  'blocked',
  'cancelled'
];

/**
 * The whole `DriverStatus` enum (kew/src/kew/graph/driver/overlay.py), which is
 * what a snapshot can serve — not the eight `STATUSES` above that this module
 * keeps a convenience colour for. `factory/status.test.ts` holds the same list
 * under the backend parity test that guards it.
 */
const DRIVER_STATUSES = [
  'todo',
  'queued',
  'in_progress',
  'awaiting_publication',
  'awaiting_continuation',
  'in_review',
  'adversarial_review',
  'remediating',
  'awaiting_no_change',
  'done',
  'blocked',
  'failed',
  'cancelled',
  'held'
];

/** The two ink tokens app.css declares NON-TEXT: they fail AA on the ink in
 * both themes (`tokens.test.ts`), so they may draw a mark, a rule or a gauge
 * tick and never a word. */
const NON_TEXT_TOKENS = ['var(--text-4)', 'var(--text-5)'];

/** The three states a node sits in while a PERSON must act on work that
 * already happened. `queued` used to be a fourth; #2209 gave it its own
 * neutral mark, because a node nobody armed is work that has not started and
 * was drawing the same magenta diamond as a merge decision. */
const NEEDS_YOU = ['awaiting_publication', 'awaiting_continuation', 'awaiting_no_change'];

describe('statusColor — the rationing the DAG used to break', () => {
  it('every status has a CSS var mapping and statusColor agrees with it', () => {
    for (const s of STATUSES) {
      expect(STATUS_COLOR[s]).toBeTruthy();
      expect(statusColor(s)).toBe(STATUS_COLOR[s]);
      expect(STATUS_COLOR[s]).toMatch(/^var\(--[a-z0-9-]+\)$/);
    }
  });

  it('in progress is cyan — Factory UI plan finding 3, the rationing fix', () => {
    // This map gave `in_progress` var(--accent), the magenta reserved for
    // "a human is needed". Work in progress needs nobody.
    expect(STATUS_COLOR.in_progress).toBe('var(--accent-2)');
    expect(statusColor('in_progress')).toBe('var(--accent-2)');
  });

  it('nothing but needs-you spends the magenta', () => {
    for (const s of STATUSES) {
      expect(STATUS_COLOR[s], s).not.toBe('var(--accent)');
    }
    // ...and the states that DO need a person keep it, so the colour is
    // rationed rather than merely abolished.
    for (const s of NEEDS_YOU) {
      expect(statusColor(s), s).toBe('var(--accent)');
    }
  });

  it('defers to factory/status rather than holding a second opinion', () => {
    for (const s of STATUSES) {
      expect(statusColor(s)).toBe(statusShape(s).color);
    }
    // Including the six DriverStatus words STATUS_COLOR does not list: the map
    // is a convenience for count tables, never the authority.
    for (const s of [...NEEDS_YOU, 'adversarial_review', 'remediating']) {
      expect(statusColor(s), s).toBe(statusShape(s).color);
    }
  });

  it('unknown status falls back to the todo colour rather than throwing', () => {
    expect(statusColor('some-unknown-status')).toBe(STATUS_COLOR.todo);
  });
});

// Replaces the nodeFill assertions: the node is a MARK now, not a washed box,
// so what has to be pinned is which shape and which size each status draws.
describe('node marks — shape and size replace the status wash', () => {
  it('every status draws a shape, and no two meanings share shape AND colour', () => {
    for (const s of STATUSES) {
      expect(statusShape(s).shape).toBeTruthy();
    }
    const pairs = new Set(
      (Object.keys(STATUS_MARKS) as StatusMeaning[]).map((m) => {
        const { shape, color } = STATUS_MARKS[m];
        return `${shape}|${color}`;
      })
    );
    expect(pairs.size).toBe(Object.keys(STATUS_MARKS).length);
  });

  it('the eight DAG statuses are pairwise distinguishable by shape+colour', () => {
    // in_progress and in_review are both cyan now, so colour alone no longer
    // tells them apart — the SHAPE has to, which is the point of FUI-04.
    const drawn = STATUSES.map((s) => {
      const m = statusShape(s);
      return `${m.shape}|${m.color}`;
    });
    expect(new Set(drawn).size).toBe(STATUSES.length);
  });
});

describe('labelTone — dim history, bright work', () => {
  it('done and todo are the dim pair the artboard draws', () => {
    for (const s of ['done', 'todo', 'queued', 'cancelled']) {
      expect(labelTone(s), s).toEqual({
        id: 'var(--text-3)',
        title: 'var(--text-2)',
        weight: 400,
        status: 'var(--text-3)',
        active: false
      });
    }
  });

  it('active nodes are --text-1 at weight 500 with a coloured status line', () => {
    for (const s of ['in_progress', 'in_review', 'blocked', 'failed', 'held']) {
      const tone = labelTone(s);
      expect(tone.id, s).toBe('var(--text-1)');
      expect(tone.title, s).toBe('var(--text-1)');
      expect(tone.weight, s).toBe(500);
      expect(tone.active, s).toBe(true);
      expect(tone.status, s).toBe(statusColor(s));
    }
  });

  it('a finished node never spends the green on its status word', () => {
    // "done" in --success under all thirteen finished nodes would put the
    // loudest ink in the graph on the part nobody needs to read.
    expect(labelTone('done').status).not.toBe(statusColor('done'));
  });

  it('an unknown status takes the dim treatment, like the todo it degrades to', () => {
    expect(labelTone('some-unknown-status')).toEqual(labelTone('todo'));
  });

  it('never prints a status line in a token app.css declares non-text', () => {
    // #2209 gave `queued` its own mark in --text-4, which is a legitimate
    // colour for a MARK and not for a word: app.css documents it as non-text
    // (3.4:1 on --ink, 2.7:1 on the light ink) and tokens.test.ts pins both
    // ratios as failing AA. Reaching the active branch with it painted an
    // unarmed node's whole status line — "queued", and the driver's reason
    // beside it — in a colour the repo's own gate calls unreadable.
    for (const s of [...DRIVER_STATUSES, 'some-unknown-status']) {
      expect(NON_TEXT_TOKENS, s).not.toContain(labelTone(s).status);
    }
    // The mark is still allowed to use it — that is the distinction.
    expect(statusColor('queued')).toBe('var(--text-4)');
    expect(labelTone('queued').status).toBe('var(--text-3)');
  });

  it('dims a queued node with the rest of the work nobody has started', () => {
    // An unarmed node has nothing happening to it, so it reads like `todo`
    // rather than like the in-flight half of the graph.
    expect(labelTone('queued')).toEqual(labelTone('todo'));
    expect(labelTone('queued').active).toBe(false);
  });
});

describe('statusWord — the 11px status line', () => {
  it('humanizes underscored statuses', () => {
    expect(statusWord('in_progress')).toBe('in progress');
    expect(statusWord('in_review')).toBe('in review');
    expect(statusWord('done')).toBe('done');
  });

  it('carries no glyph: the mark beside it is the shape cue', () => {
    // The old caption smuggled a "×" into "cancelled" because an SVG box had
    // no other non-colour signal. The ring-x mark says it now.
    for (const s of [...STATUSES, 'some_new_status']) {
      expect(statusWord(s), s).not.toMatch(/[×·]/);
    }
    expect(statusWord('cancelled')).toBe('cancelled');
  });

  it('unknown statuses echo themselves rather than throwing', () => {
    expect(statusWord('some_new_status')).toBe('some new status');
  });
});

describe('displayId — # prefix only for bare driver ints', () => {
  it('prefixes bare ints', () => {
    expect(displayId('594')).toBe('#594');
  });

  it('leaves external refs untouched', () => {
    expect(displayId('other-repo#5')).toBe('other-repo#5');
    expect(displayId('abc')).toBe('abc');
  });
});
