import { describe, expect, it } from 'vitest';
import { STATUS_COLOR } from '$lib/epic/render';
import {
  MARK_SIZES,
  QUIET_MEANINGS,
  STATUS_MARKS,
  statusShape,
  type MarkShape,
  type StatusMeaning,
  type StatusVocabulary
} from './status';

/**
 * Every value `DriverStatus` (kew/src/kew/graph/driver/overlay.py) can put in an
 * epic snapshot — all fourteen, transcribed from the enum and NOT from the
 * frontend's own eight-entry `STATUS_COLOR`, which is a subset (asserted below)
 * and so cannot catch a status the server serves and the UI has never heard of.
 * `kew/tests/test_factory_status_parity.py` fails if the enum grows a member
 * this list is missing.
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

/** The #2050 run-state vocabulary: three live states, four outcomes. */
const RUN_STATE_WORDS = ['working', 'blocked', 'idle', 'success', 'flawed', 'manual'];

/** Evidence-chain states (FUI-12). */
const EVIDENCE_STATES = ['attested', 'pending', 'missing', 'unverified'];

/**
 * The pipeline cell's own word that no vocabulary above carries: a review round
 * that ended with a not-ready verdict (`_REVIEW_VERDICT` in
 * kew/src/kew/service/pipeline.py, #3466).
 */
const PIPELINE_STATES = ['not_ready'];

/**
 * `WorkArtifactDTO.publication` — every word `_artifact_dto` in
 * kew/src/kew/service/work.py can put in the field, transcribed from it.
 * `test_factory_status_parity.py` fails if that function grows a fourth.
 */
const PUBLICATION_STATES = ['not_required', 'pending', 'published'];

/**
 * The real `RunStatus` literal in run_store.py — all nine, including the three
 * (`awaiting_publication`, `refused`, `budget_stop`) that kpi.ts's stale comment
 * leaves out. Guarded by the same parity test as `DRIVER_STATUSES`.
 */
const RUN_STORE_STATUSES = [
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

/**
 * The `LoopStatus` literal in run_store.py — the six words a goal loop can be
 * in. They reach the UI on the Loops page (FUI-27), where the list row and the
 * detail head draw one mark each, so a paused loop and a loop that ran out of
 * budget cannot read as work nobody has started.
 */
const LOOP_STATUSES = [
  'pursuing',
  'achieved',
  'unmet',
  'budget_limited',
  'needs_human',
  'paused'
];

/** `running` and `queued` are kpi.ts's display-only refinements of `pending`:
 * no server ever sends them, but the UI paints them. */
const DISPLAY_ONLY_STATUSES = ['running', 'queued'];

/** Words the UI invents for itself, in no server's vocabulary. */
// `published_waiting` joins them since #2305: it is a meaning name rather than
// a served status, and `statusWord` prints a meaning by looking it up as a word.
const UI_JOINS = ['needs_you', 'multi_task', 'published_waiting'];

/**
 * Every vocabulary, with the `StatusVocabulary` a caller holding one of its
 * words passes. Only the epic snapshot speaks `'epic'`; everything else is a
 * run, an evidence row or a UI join, and reads `queued` the run way.
 */
const VOCABULARIES: [string, string[], StatusVocabulary][] = [
  ['DriverStatus', DRIVER_STATUSES, 'epic'],
  ['#2050 run states', RUN_STATE_WORDS, 'run'],
  ['evidence states', EVIDENCE_STATES, 'run'],
  ['artifact publication states', PUBLICATION_STATES, 'run'],
  ['pipeline states', PIPELINE_STATES, 'run'],
  ['run_store RunStatus', RUN_STORE_STATUSES, 'run'],
  ['run_store LoopStatus', LOOP_STATUSES, 'run'],
  ['kpi.ts display-only states', DISPLAY_ONLY_STATUSES, 'run'],
  ['UI joins', UI_JOINS, 'run']
];

type Pin = { meaning: StatusMeaning; shape: MarkShape; color: string; word: string };

/**
 * The exact mark every status word must draw, keyed `<vocabulary>:<word>`.
 *
 * This table, not a "some mark came back" guard, is what pins the mapping. The
 * guards that used to stand alone here asserted only that each served status
 * resolved to *a* known shape with *a* `var(--token)` colour, which held just as
 * well if `killed` and `failure` mapped to `done` — a green done dot for a run
 * somebody killed — or `queued` to `not_required`. `pinsEveryServedWord` below
 * fails if any word above is missing an entry, so the exhaustiveness cannot rot
 * back into that.
 */
const PINNED_MARKS: Record<string, Pin> = {
  // The epic snapshot's DriverStatus.
  'epic:todo': { meaning: 'todo', shape: 'ring', color: 'var(--text-3)', word: 'todo' },
  // Neutral since #2209: a node nobody armed is work that has not started,
  // and the magenta diamond is for work a person has to decide about.
  'epic:queued': {
    meaning: 'queued',
    shape: 'ring-dashed',
    color: 'var(--text-4)',
    word: 'queued'
  },
  'epic:in_progress': {
    meaning: 'working',
    shape: 'dot-halo',
    color: 'var(--accent-2)',
    word: 'in progress'
  },
  'epic:awaiting_publication': {
    meaning: 'needs_you',
    shape: 'diamond-halo',
    color: 'var(--accent)',
    word: 'awaiting publication'
  },
  'epic:awaiting_continuation': {
    meaning: 'needs_you',
    shape: 'diamond-halo',
    color: 'var(--accent)',
    word: 'awaiting continuation'
  },
  'epic:in_review': {
    meaning: 'published_waiting',
    shape: 'ring-thick',
    color: 'var(--accent-2)',
    word: 'in review'
  },
  'epic:adversarial_review': {
    meaning: 'working',
    shape: 'dot-halo',
    color: 'var(--accent-2)',
    word: 'adversarial review'
  },
  'epic:remediating': {
    meaning: 'working',
    shape: 'dot-halo',
    color: 'var(--accent-2)',
    word: 'remediating'
  },
  'epic:awaiting_no_change': {
    meaning: 'needs_you',
    shape: 'diamond-halo',
    color: 'var(--accent)',
    word: 'awaiting no change'
  },
  'epic:done': { meaning: 'done', shape: 'dot', color: 'var(--success)', word: 'done' },
  'epic:blocked': {
    meaning: 'blocked',
    shape: 'hatch-square',
    color: 'var(--status-blocked)',
    word: 'blocked'
  },
  'epic:failed': { meaning: 'failed', shape: 'x', color: 'var(--danger)', word: 'failed' },
  'epic:cancelled': {
    meaning: 'cancelled',
    shape: 'ring-x',
    color: 'var(--status-cancelled)',
    word: 'cancelled'
  },
  'epic:held': { meaning: 'held', shape: 'ring-thick', color: 'var(--warn)', word: 'held' },
  // The #2050 run states.
  'run:working': {
    meaning: 'working',
    shape: 'dot-halo',
    color: 'var(--accent-2)',
    word: 'working'
  },
  'run:blocked': {
    meaning: 'blocked',
    shape: 'hatch-square',
    color: 'var(--status-blocked)',
    word: 'blocked'
  },
  'run:idle': { meaning: 'idle', shape: 'ring', color: 'var(--text-5)', word: 'idle' },
  'run:success': { meaning: 'done', shape: 'dot', color: 'var(--success)', word: 'success' },
  'run:flawed': { meaning: 'failed', shape: 'x', color: 'var(--danger)', word: 'flawed' },
  'run:manual': {
    meaning: 'needs_you',
    shape: 'diamond-halo',
    color: 'var(--accent)',
    word: 'manual'
  },
  // The evidence chain.
  'run:attested': { meaning: 'done', shape: 'dot', color: 'var(--success)', word: 'attested' },
  'run:pending': {
    meaning: 'pending',
    shape: 'ring-dashed',
    color: 'var(--text-3)',
    word: 'pending'
  },
  'run:missing': { meaning: 'missing', shape: 'ring-x', color: 'var(--danger)', word: 'missing' },
  'run:unverified': {
    meaning: 'unverified',
    shape: 'ring-dashed',
    color: 'var(--warn)',
    word: 'unverified'
  },
  // The pipeline's not-ready review round: amber, in a shape of its own.
  'run:not_ready': {
    meaning: 'not_ready',
    shape: 'ring-half',
    color: 'var(--warn)',
    word: 'not ready'
  },
  // WorkArtifactDTO.publication.
  'run:not_required': {
    meaning: 'not_required',
    shape: 'ring',
    color: 'var(--text-4)',
    word: 'not required'
  },
  'run:published': { meaning: 'done', shape: 'dot', color: 'var(--success)', word: 'published' },
  // The run_store RunStatus enum.
  'run:awaiting_publication': {
    meaning: 'needs_you',
    shape: 'diamond-halo',
    color: 'var(--accent)',
    word: 'awaiting publication'
  },
  'run:failure': { meaning: 'failed', shape: 'x', color: 'var(--danger)', word: 'failure' },
  'run:refused': { meaning: 'held', shape: 'ring-thick', color: 'var(--warn)', word: 'refused' },
  'run:budget_exceeded': {
    meaning: 'held',
    shape: 'ring-thick',
    color: 'var(--warn)',
    word: 'budget exceeded'
  },
  'run:budget_stopped': {
    meaning: 'held',
    shape: 'ring-thick',
    color: 'var(--warn)',
    word: 'budget stopped'
  },
  'run:budget_stop': {
    meaning: 'held',
    shape: 'ring-thick',
    color: 'var(--warn)',
    word: 'budget stop'
  },
  'run:killed': { meaning: 'failed', shape: 'x', color: 'var(--danger)', word: 'killed' },
  // The run_store LoopStatus literal.
  'run:pursuing': {
    meaning: 'working',
    shape: 'dot-halo',
    color: 'var(--accent-2)',
    word: 'pursuing'
  },
  'run:achieved': { meaning: 'done', shape: 'dot', color: 'var(--success)', word: 'achieved' },
  // A loop that ran to its last step without meeting its goal is finished and
  // intact, not broken: the muted cancelled ring, never the failure X.
  'run:unmet': {
    meaning: 'cancelled',
    shape: 'ring-x',
    color: 'var(--status-cancelled)',
    word: 'unmet'
  },
  'run:budget_limited': {
    meaning: 'held',
    shape: 'ring-thick',
    color: 'var(--warn)',
    word: 'budget limited'
  },
  'run:needs_human': {
    meaning: 'needs_you',
    shape: 'diamond-halo',
    color: 'var(--accent)',
    word: 'needs human'
  },
  'run:paused': { meaning: 'held', shape: 'ring-thick', color: 'var(--warn)', word: 'paused' },
  // kpi.ts's display-only refinements of a pending run.
  'run:running': {
    meaning: 'working',
    shape: 'dot-halo',
    color: 'var(--accent-2)',
    word: 'running'
  },
  'run:queued': {
    meaning: 'pending',
    shape: 'ring-dashed',
    color: 'var(--text-3)',
    word: 'queued'
  },
  // The joins the UI adds itself.
  'run:needs_you': {
    meaning: 'needs_you',
    shape: 'diamond-halo',
    color: 'var(--accent)',
    word: 'needs you'
  },
  'run:multi_task': {
    meaning: 'multi_task',
    shape: 'bars',
    color: 'var(--text-5)',
    word: 'tasks'
  },
  // The word stays "in review": what changed in #2305 is the NAME of the
  // meaning, so a reader of the code cannot mistake a published pull request
  // waiting on a person for work kew is executing.
  'run:published_waiting': {
    meaning: 'published_waiting',
    shape: 'ring-thick',
    color: 'var(--accent-2)',
    word: 'in review'
  }
};

/** `<vocabulary>:<word>` for every word any vocabulary above can hand the UI. */
const SERVED_KEYS = [
  ...new Set(VOCABULARIES.flatMap(([, words, vocabulary]) => words.map((w) => `${vocabulary}:${w}`)))
];

const ALL_KNOWN = [
  ...DRIVER_STATUSES,
  ...RUN_STATE_WORDS,
  ...EVIDENCE_STATES,
  ...PUBLICATION_STATES,
  ...PIPELINE_STATES,
  ...RUN_STORE_STATUSES,
  ...LOOP_STATUSES,
  ...DISPLAY_ONLY_STATUSES,
  ...UI_JOINS
];

describe('statusShape — every vocabulary in the app maps to a shape', () => {
  it('pins a mark for every served word, so no mapping is left unasserted', () => {
    // The exhaustiveness guard. Without it, a word could be added to a
    // vocabulary list above (and satisfy the Python parity test, which only
    // checks that the word is in MEANING_OF at all) while nothing in this file
    // ever said which mark it must draw.
    expect(Object.keys(PINNED_MARKS).sort()).toEqual([...SERVED_KEYS].sort());
  });

  it.each(SERVED_KEYS)('%s draws exactly its pinned mark', (key) => {
    const [vocabulary, word] = key.split(':') as [StatusVocabulary, string];
    expect(statusShape(word, vocabulary)).toEqual({ ...PINNED_MARKS[key], known: true });
  });

  it('epic STATUS_COLOR is a strict subset of DriverStatus, never the authority', () => {
    // The guard that used to stand here read STATUS_COLOR's eight keys and so
    // passed while five DriverStatus values fell through to the todo ring.
    const coloured = Object.keys(STATUS_COLOR);
    expect(coloured.every((s) => DRIVER_STATUSES.includes(s))).toBe(true);
    expect(coloured.length).toBeLessThan(DRIVER_STATUSES.length);
  });

  it('no status the server can serve shares the todo ring with "not started"', () => {
    // The fallback is deliberate for a status kew has not shipped yet; it is a
    // bug for one it has, because nothing then tells the two apart but the word.
    const degraded = SERVED_KEYS.filter((key) => {
      const [vocabulary, word] = key.split(':') as [StatusVocabulary, string];
      return word !== 'todo' && statusShape(word, vocabulary).meaning === 'todo';
    });
    expect(degraded).toEqual([]);
  });

  it('no two meanings share both a shape and a colour', () => {
    const pairs = new Map<string, StatusMeaning>();
    for (const [meaning, mark] of Object.entries(STATUS_MARKS) as [
      StatusMeaning,
      { shape: string; color: string }
    ][]) {
      const pair = `${mark.shape}|${mark.color}`;
      expect(pairs.get(pair), `${meaning} collides with ${pairs.get(pair)}`).toBeUndefined();
      pairs.set(pair, meaning);
    }
    expect(pairs.size).toBe(Object.keys(STATUS_MARKS).length);
  });

  it('every meaning in the table is reachable from at least one status word', () => {
    const reached = new Set(
      SERVED_KEYS.map((key) => {
        const [vocabulary, word] = key.split(':') as [StatusVocabulary, string];
        return statusShape(word, vocabulary).meaning;
      })
    );
    expect([...Object.keys(STATUS_MARKS)].sort()).toEqual([...reached].sort());
  });

  it('QUIET_MEANINGS is a strict subset of the meanings and leaves the signals out', () => {
    for (const m of QUIET_MEANINGS) expect(STATUS_MARKS[m]).toBeTruthy();
    for (const signal of ['working', 'needs_you', 'done', 'failed', 'blocked'] as StatusMeaning[]) {
      expect(QUIET_MEANINGS.has(signal), signal).toBe(false);
    }
  });
});

describe('statusShape — the two vocabularies that spell a word the same', () => {
  it('disagrees about exactly one word, and `queued` is it', () => {
    // The whole collision set, pinned. A second word that means two things
    // would otherwise be a silent choice of one of them.
    const differing = ALL_KNOWN.filter(
      (s) => statusShape(s, 'run').meaning !== statusShape(s, 'epic').meaning
    );
    expect([...new Set(differing)]).toEqual(['queued']);
  });

  it('an unarmed epic node and a queued run are not the same mark', () => {
    // `linear_admission_status` returns QUEUED exactly when the arm marker is
    // absent, so the epic's `queued` is a node waiting on an operator command
    // (epic/waiting.ts names it). kpi.ts's `queued` is the opposite: an
    // untouched pending run the dispatcher starts by itself. Both are quiet —
    // neither is work in flight — and they are told apart by colour.
    expect(statusShape('queued')).toMatchObject({
      meaning: 'queued',
      shape: 'ring-dashed',
      color: 'var(--text-4)'
    });
    expect(statusShape('queued', 'run')).toMatchObject({
      meaning: 'pending',
      shape: 'ring-dashed',
      color: 'var(--text-3)'
    });
    for (const vocabulary of ['epic', 'run'] as StatusVocabulary[]) {
      expect(QUIET_MEANINGS.has(statusShape('queued', vocabulary).meaning)).toBe(true);
    }
    // Until #2209 the epic reading was `needs_you`: every unarmed node drew
    // the same magenta diamond as a node somebody is actually holding up, and
    // on the go-live epic that was most of the graph.
    expect(statusShape('queued').color).not.toBe(STATUS_MARKS.needs_you.color);
  });

  it('the epic reading is the default, because it is the one that names a command', () => {
    // A caller that forgets to say gets the epic reading, whose mark and
    // wording both point at `kew:ready`; the run reading would say "the
    // dispatcher's turn" about a node waiting on a person.
    expect(statusShape('queued')).toEqual(statusShape('queued', 'epic'));
  });

  it('the run vocabulary refines meanings and never adds or drops words', () => {
    for (const s of ALL_KNOWN) {
      expect(statusShape(s, 'run').known, s).toBe(true);
      expect(statusShape(s, 'run').word, s).toBe(statusShape(s, 'epic').word);
    }
    expect(statusShape('awaiting_snapshot', 'run').known).toBe(false);
  });
});

describe('statusShape — the shapes the DEpic legend and DWork state column draw', () => {
  it('in progress is cyan, not the magenta STATUS_COLOR gives it today', () => {
    // Factory UI plan finding 3: magenta is reserved for human judgment, so the DAG's
    // `in_progress: var(--accent)` is superseded here.
    expect(statusShape('in_progress').color).toBe('var(--accent-2)');
    expect(statusShape('needs_you').color).toBe('var(--accent)');
  });

  it('attested reads as done, and an unverified chain never gets that green mark', () => {
    expect(statusShape('attested')).toMatchObject({ shape: 'dot', color: 'var(--success)' });
    const unverified = statusShape('unverified');
    expect(unverified.color).not.toBe('var(--success)');
    expect(unverified.shape).not.toBe('dot');
  });

  it('flawed shares the failed mark and is told apart by its word', () => {
    expect(statusShape('flawed').meaning).toBe('failed');
    expect(statusShape('flawed').word).toBe('flawed');
  });

  it('a killed run and a failed one wear the failure X, never the success dot', () => {
    // The mark, not the word, is what a glance reads: `killed` and `failure`
    // taking the green done dot would be invisible to every guard that only
    // asks whether *a* mark exists.
    for (const s of ['killed', 'failure', 'failed', 'flawed']) {
      expect(statusShape(s), s).toMatchObject({ shape: 'x', color: 'var(--danger)' });
    }
    expect(statusShape('killed').color).not.toBe(STATUS_MARKS.done.color);
  });

  it('a budget stop is a hold, not a failure — and so is a refusal', () => {
    // `_REFUSAL_TERMINAL` in kew/service/work.py: "terminal run statuses that
    // are somebody's decision rather than a fault". A refused node is intact
    // and re-admittable, so it must not wear the failure X.
    for (const s of ['budget_exceeded', 'budget_stopped', 'budget_stop', 'refused']) {
      expect(statusShape(s).meaning, s).toBe('held');
    }
    expect(statusShape('budget_exceeded').word).toBe('budget exceeded');
    expect(statusShape('refused').word).toBe('refused');
  });

  it('a live governed-review round is work kew is doing, not a published wait', () => {
    // #2305: `adversarial_review` used to share the published-and-waiting ring
    // with `in_review`, which drew kew's own running reviewer exactly like a
    // pull request sitting in somebody's queue. They are opposites in the
    // motion vocabulary, so they no longer share a mark.
    expect(statusShape('adversarial_review')).toMatchObject({
      meaning: 'working',
      shape: 'dot-halo',
      color: 'var(--accent-2)',
      word: 'adversarial review'
    });
    expect(statusShape('remediating')).toMatchObject({
      meaning: 'working',
      shape: 'dot-halo',
      word: 'remediating'
    });
  });

  it('the states waiting on a person take the needs-you diamond', () => {
    // `_FIXED_WAITING_REASONS` in kew/graph/driver/tick.py names the operator
    // command for each: `kew finalize` / an external PR, `kew epic continue` or
    // `kew epic abandon`, and confirm-or-reject of the no-change outcome. A
    // person is the next step, which is exactly what magenta is rationed for.
    for (const s of ['awaiting_publication', 'awaiting_continuation', 'awaiting_no_change']) {
      expect(statusShape(s), s).toMatchObject({
        meaning: 'needs_you',
        shape: 'diamond-halo',
        color: 'var(--accent)'
      });
    }
    expect(statusShape('awaiting_no_change').word).toBe('awaiting no change');
  });

  it('tells the four #2209 statuses apart by glyph AND colour', () => {
    // The owner's review: queued nodes were drawn with the same red diamond
    // as needs-you. These four have to be distinguishable at a glance —
    // queued neutral, waiting-on-a-person red, held amber, done green.
    const drawn = ['queued', 'needs_you', 'held', 'done'].map((s) => statusShape(s));
    expect(drawn.map((m) => m.color)).toEqual([
      'var(--text-4)',
      'var(--accent)',
      'var(--warn)',
      'var(--success)'
    ]);
    expect(new Set(drawn.map((m) => m.shape)).size).toBe(4);
    expect(new Set(drawn.map((m) => m.color)).size).toBe(4);
  });
});

describe('statusShape — normalisation and the unknown fallback', () => {
  it.each(['in progress', 'in-progress', 'IN_PROGRESS', '  In Progress  '])(
    '%s normalises to the in-progress mark',
    (raw) => {
      expect(statusShape(raw)).toMatchObject({
        meaning: 'working',
        shape: 'dot-halo',
        word: 'in progress',
        known: true
      });
    }
  );

  it('an unknown status falls back to todo and echoes its own word', () => {
    const mark = statusShape('awaiting_snapshot');
    expect(mark.meaning).toBe('todo');
    expect(mark.shape).toBe(STATUS_MARKS.todo.shape);
    expect(mark.color).toBe(STATUS_MARKS.todo.color);
    expect(mark.word).toBe('awaiting snapshot');
    expect(mark.known).toBe(false);
  });

  it.each([null, undefined, '', '   '])('a blank status (%s) is todo, never an empty word', (raw) => {
    expect(statusShape(raw)).toMatchObject({ meaning: 'todo', word: 'todo', known: false });
  });

  it.each(['constructor', 'toString', 'valueOf', '__proto__', 'hasOwnProperty'])(
    'a status named after an Object.prototype member (%s) takes the fallback',
    (raw) => {
      // The alias tables are object literals, so a plain `TABLE[key]` lookup
      // answers `constructor` with the Object function — not `undefined`, so it
      // escapes the fallback, indexes STATUS_MARKS with a function and yields
      // shape and colour `undefined`, which StatusMark.svelte draws as an empty
      // svg. The status word comes off the wire, so this is reachable input.
      // `constructor` and `__proto__` are the two that survive `normalize`'s
      // lowercasing and reach the tables; the mixed-case ones are here because
      // normalisation is the only thing saving them, and that could change.
      const mark = statusShape(raw);
      expect(mark.known).toBe(false);
      expect(mark.meaning).toBe('todo');
      expect(mark.shape).toBe(STATUS_MARKS.todo.shape);
      expect(mark.color).toBe(STATUS_MARKS.todo.color);
      expect(typeof mark.word).toBe('string');
      expect(mark.word).toBe(raw.toLowerCase().replace(/_/g, ' '));
      expect(statusShape(raw, 'run')).toEqual(mark);
    }
  );

  it('never throws, whatever the server sends', () => {
    for (const raw of ['💥', '0', 'a'.repeat(400)]) {
      expect(() => statusShape(raw)).not.toThrow();
      expect(() => statusShape(raw, 'run')).not.toThrow();
    }
  });
});

describe('MARK_SIZES', () => {
  it('is the five sizes the artboards draw, ascending', () => {
    expect(MARK_SIZES).toEqual([7, 10, 12, 14, 18]);
  });
});
