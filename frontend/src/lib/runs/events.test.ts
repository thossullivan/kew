/**
 * #2300 — the run page's readings: phases, the filter, the chips, the presets,
 * the permalink, the live tail.
 */
import { describe, expect, it, vi } from 'vitest';
import { RUN_AUDIT_PAGE, type RunAuditEvent } from '$lib/api';
import { LIVE_STATUSES } from '$lib/run-status';
import {
  EMPTY_RUN_FILTER,
  LOADING_EVENTS,
  NO_KINDS_SELECTED,
  NO_MATCH,
  PRESET_PHASES,
  RUN_QUEUED,
  TAIL_BATCH_MS,
  TAIL_POLL_MS,
  canClearRunFilter,
  clearRunFilter,
  debounce,
  eventCostUsd,
  filterRunEvents,
  focusedRunEvent,
  followsTail,
  isErrorEvent,
  kindChips,
  mayHoldMoreRunEvents,
  mergeRunEvents,
  parseRunFilter,
  phaseDurationMs,
  phaseSpan,
  runEventsCount,
  runEventsEmptyState,
  runFilterQuery,
  runPhases,
  runPresets,
  stillLive,
  tailCursor,
  toggleKind,
  togglePhase,
  verdictScopeNote
} from './events';

const T0 = Date.parse('2026-10-05T12:00:00Z');

function ev(
  seq: number,
  event: string,
  offsetS: number,
  payload: Record<string, unknown> | null = null
): RunAuditEvent {
  return {
    seq,
    ts: new Date(T0 + offsetS * 1000).toISOString(),
    event,
    actor_type: 'policy',
    item_id: '2300',
    payload
  };
}

/** A run: two dispatch events, a slow seal, a refusal, a priced review. */
const EVENTS: RunAuditEvent[] = [
  ev(1, 'run_started', 0),
  ev(2, 'routing_decision', 1, { recommended_runner: 'codex' }),
  ev(3, 'sandbox_resolved', 2, { posture: 'sandboxed' }),
  ev(4, 'sandbox_resolved', 3, { posture: 'sandboxed' }),
  ev(5, 'artifact_sealed', 120, {}),
  ev(6, 'publication_refused', 130, { error: 'protected branch' }),
  ev(7, 'adversarial_review_verdict', 200, { verdict: 'READY', spend_usd: 1.5 })
];

const END = T0 + 240_000;

describe('the phase timeline', () => {
  it('collapses consecutive same-named events into one segment', () => {
    const phases = runPhases(EVENTS, END);

    expect(phases.map((p) => p.name)).toEqual([
      'run_started',
      'routing_decision',
      'sandbox_resolved',
      'artifact_sealed',
      'publication_refused',
      'adversarial_review_verdict'
    ]);
    const sandbox = phases.find((p) => p.name === 'sandbox_resolved');
    expect(sandbox?.count).toBe(2);
  });

  it('runs each segment up to the next one, and the last to the run end', () => {
    const phases = runPhases(EVENTS, END);

    // run_started at +0s, routing_decision at +1s: one second of clock.
    expect(phaseDurationMs(phases[0])).toBe(1000);
    // sandbox_resolved (+2s) closes where artifact_sealed opens (+120s).
    expect(phaseDurationMs(phases[2])).toBe(118_000);
    expect(phases[phases.length - 1].endMs).toBe(END);
  });

  it('leaves out an event whose timestamp cannot be read', () => {
    const broken = { ...ev(8, 'nonsense', 0), ts: 'not a time' };
    expect(runPhases([...EVENTS, broken], END).map((p) => p.name)).not.toContain('nonsense');
  });

  it('marks the segment that failed and the one that cost money', () => {
    const phases = runPhases(EVENTS, END);

    expect(phases.find((p) => p.name === 'publication_refused')?.failed).toBe(true);
    expect(phases.find((p) => p.name === 'artifact_sealed')?.failed).toBe(false);
    expect(phases.find((p) => p.name === 'adversarial_review_verdict')?.costUsd).toBe(1.5);
  });

  it('spans from the first event to the run end, and answers for no events', () => {
    expect(phaseSpan(runPhases(EVENTS, END), END)).toEqual({ startMs: T0, endMs: END });
    expect(phaseSpan([], END)).toEqual({ startMs: END, endMs: END });
  });

  it('never draws a segment backwards when the run end precedes its events', () => {
    const phases = runPhases(EVENTS, T0 - 1);
    expect(phases.every((p) => p.endMs >= p.startMs)).toBe(true);
  });
});

describe('what an event says about itself', () => {
  it('reads a failure from the recorded name or from an error field', () => {
    expect(isErrorEvent(ev(1, 'publication_failed', 0))).toBe(true);
    expect(isErrorEvent(ev(1, 'run_killed', 0))).toBe(true);
    expect(isErrorEvent(ev(1, 'run_finished', 0, { error_category: 'timeout' }))).toBe(true);
    expect(isErrorEvent(ev(1, 'run_finished', 0, { error_category: '' }))).toBe(false);
    expect(isErrorEvent(ev(1, 'run_started', 0))).toBe(false);
  });

  it('counts a recorded figure and nothing else as a cost', () => {
    expect(eventCostUsd(ev(1, 'x', 0, { cost_usd: 0.25 }))).toBe(0.25);
    expect(eventCostUsd(ev(1, 'x', 0, { spend_usd: '1.00' }))).toBe(0);
    expect(eventCostUsd(ev(1, 'x', 0, null))).toBe(0);
  });
});

describe('the filter in the URL', () => {
  it('round-trips phase, type and free text through one param', () => {
    const raw = 'phase:artifact_sealed type:run_started protected branch';
    const filter = parseRunFilter(raw);

    expect(filter).toEqual({
      phases: ['artifact_sealed'],
      types: ['run_started'],
      text: 'protected branch'
    });
    expect(parseRunFilter(runFilterQuery(filter))).toEqual(filter);
  });

  it('reads an absent or empty param as no filter at all', () => {
    expect(parseRunFilter(null)).toEqual(EMPTY_RUN_FILTER);
    expect(parseRunFilter('   ')).toEqual(EMPTY_RUN_FILTER);
    expect(runFilterQuery(EMPTY_RUN_FILTER)).toBe('');
  });

  it('keeps a token it has no prefix for as free text', () => {
    expect(parseRunFilter('weird:thing').text).toBe('weird:thing');
  });

  it('drops a repeated token rather than filtering twice on it', () => {
    expect(parseRunFilter('phase:a phase:a type:b type:b').phases).toEqual(['a']);
  });
});

describe('clicking a phase', () => {
  it('selects one on a plain click and clears it on a second', () => {
    const one = togglePhase(EMPTY_RUN_FILTER, 'artifact_sealed');
    expect(one.phases).toEqual(['artifact_sealed']);
    expect(togglePhase(one, 'artifact_sealed').phases).toEqual([]);
  });

  it('replaces the selection on a plain click of another phase', () => {
    const one = togglePhase(EMPTY_RUN_FILTER, 'a');
    expect(togglePhase(one, 'b').phases).toEqual(['b']);
  });

  it('adds and removes on shift-click', () => {
    const two = togglePhase(togglePhase(EMPTY_RUN_FILTER, 'a', true), 'b', true);
    expect(two.phases).toEqual(['a', 'b']);
    expect(togglePhase(two, 'a', true).phases).toEqual(['b']);
  });

  it('clears the filter without dropping the selection', () => {
    const filter = parseRunFilter('phase:a type:run_started protected');
    expect(clearRunFilter(filter)).toEqual({ phases: ['a'], types: [], text: '' });
  });
});

describe('the filtered list', () => {
  const phases = runPhases(EVENTS, END);

  it('shows every event when nothing is filtered', () => {
    expect(filterRunEvents(EVENTS, EMPTY_RUN_FILTER, phases)).toHaveLength(EVENTS.length);
  });

  it('narrows to a clicked phase, and to two of them', () => {
    const one = filterRunEvents(EVENTS, parseRunFilter('phase:sandbox_resolved'), phases);
    expect(one.map((e) => e.seq)).toEqual([3, 4]);

    const two = filterRunEvents(
      EVENTS,
      parseRunFilter('phase:sandbox_resolved phase:artifact_sealed'),
      phases
    );
    expect(two.map((e) => e.seq)).toEqual([3, 4, 5]);
  });

  it('ANDs a kind selection with the phase selection', () => {
    const shown = filterRunEvents(
      EVENTS,
      parseRunFilter('phase:sandbox_resolved type:run_started'),
      phases
    );
    expect(shown).toEqual([]);
  });

  it('matches free words against the name, the actor and the payload', () => {
    expect(filterRunEvents(EVENTS, parseRunFilter('codex'), phases).map((e) => e.seq)).toEqual([2]);
    expect(filterRunEvents(EVENTS, parseRunFilter('protected branch'), phases).map((e) => e.seq)).toEqual([6]);
    expect(filterRunEvents(EVENTS, parseRunFilter('codex nowhere'), phases)).toEqual([]);
  });
});

describe('the kind chips', () => {
  it('count every recorded kind, commonest first', () => {
    const chips = kindChips(EVENTS, EMPTY_RUN_FILTER);

    expect(chips[0]).toEqual({ kind: 'sandbox_resolved', count: 2, selected: false });
    expect(chips.map((c) => c.kind)).toHaveLength(6);
    expect(chips.reduce((sum, c) => sum + c.count, 0)).toBe(EVENTS.length);
  });

  it('counts over the whole page, not over the filtered view', () => {
    const filter = parseRunFilter('type:run_started');
    const chips = kindChips(EVENTS, filter);

    expect(chips.find((c) => c.kind === 'sandbox_resolved')?.count).toBe(2);
    expect(chips.find((c) => c.kind === 'run_started')?.selected).toBe(true);
  });

  it('adds and removes a kind on a chip click', () => {
    const on = toggleKind(EMPTY_RUN_FILTER, 'run_started');
    expect(on.types).toEqual(['run_started']);
    expect(toggleKind(on, 'run_started').types).toEqual([]);
  });
});

describe('the presets', () => {
  const phases = runPhases(EVENTS, END);

  it('are built from the data and name what they would select', () => {
    const presets = runPresets(EVENTS, phases);
    const byId = Object.fromEntries(presets.map((p) => [p.id, p]));

    expect(byId.errors.available).toBe(true);
    expect(byId.errors.filter.types).toEqual(['publication_refused']);
    expect(byId.slowest.available).toBe(true);
    expect(byId.slowest.filter.phases.length).toBeLessThanOrEqual(PRESET_PHASES);
    // The sandbox phase holds the longest stretch of this run's clock.
    expect(byId.slowest.filter.phases[0]).toBe('sandbox_resolved');
    expect(byId.cost.filter.phases).toEqual(['adversarial_review_verdict']);
  });

  it('go unavailable rather than offering an empty answer', () => {
    const clean = [ev(1, 'run_started', 0), ev(2, 'run_finished', 5)];
    const presets = runPresets(clean, runPhases(clean, T0 + 5000));
    const byId = Object.fromEntries(presets.map((p) => [p.id, p]));

    expect(byId.errors.available).toBe(false);
    expect(byId.cost.available).toBe(false);
    // The run still took time, so "slowest" has something to say.
    expect(byId.slowest.available).toBe(true);
  });

  it('every preset filter round-trips through the URL', () => {
    for (const preset of runPresets(EVENTS, phases)) {
      expect(parseRunFilter(runFilterQuery(preset.filter))).toEqual(preset.filter);
    }
  });
});

describe('the focusedTime permalink', () => {
  it('points at the event recorded at that instant', () => {
    expect(focusedRunEvent(EVENTS, EVENTS[4].ts)?.seq).toBe(5);
  });

  it('falls back to the last event before a time nothing was recorded at', () => {
    const between = new Date(T0 + 125_000).toISOString();
    expect(focusedRunEvent(EVENTS, between)?.seq).toBe(5);
  });

  it('points at nothing for a time it cannot read', () => {
    expect(focusedRunEvent(EVENTS, 'not a time')).toBeNull();
    expect(focusedRunEvent(EVENTS, null)).toBeNull();
    expect(focusedRunEvent([], EVENTS[0].ts)).toBeNull();
  });

  it('falls FORWARD to the first event for a time before all of them', () => {
    // The bar link's instant is the run row's `started_at`, and the row is
    // written before anything is appended under its id — so in production
    // every `?focusedTime=` from a Gantt bar lands before the first ledger
    // event. Nearest-before alone resolved that to nothing, and the marker,
    // the row highlight and the caption all silently did nothing.
    const before = new Date(T0 - 1000).toISOString();
    expect(focusedRunEvent(EVENTS, before)?.seq).toBe(EVENTS[0].seq);
  });

  it('prefers the event before over the event after', () => {
    // Fall-forward is a last resort: a time inside the run still reads back
    // to the step that was running at it, not on to the next one.
    const between = new Date(T0 + 1500).toISOString();
    expect(focusedRunEvent(EVENTS, between)?.seq).toBe(EVENTS[1].seq);
  });
});

describe('the live tail', () => {
  it('folds a new page in by seq, adding nothing twice', () => {
    const held = EVENTS.slice(0, 3);
    const again = mergeRunEvents(held, EVENTS.slice(2));

    expect(again.map((e) => e.seq)).toEqual([1, 2, 3, 4, 5, 6, 7]);
    expect(mergeRunEvents(again, [])).toBe(again);
  });

  it('resumes from the newest seq it holds', () => {
    expect(tailCursor(EVENTS)).toBe(7);
    expect(tailCursor([])).toBe(0);
  });

  it('follows the tail only while the reader is at the bottom', () => {
    expect(followsTail({ scrollTop: 900, clientHeight: 100, scrollHeight: 1000 })).toBe(true);
    expect(followsTail({ scrollTop: 400, clientHeight: 100, scrollHeight: 1000 })).toBe(false);
  });
});

describe('the hover debounce', () => {
  it('draws one marker for a pointer that crossed six rows', () => {
    vi.useFakeTimers();
    try {
      const marks: Array<number | null> = [];
      const mark = debounce<number | null>((ms) => marks.push(ms), 100);
      for (const ms of [1, 2, 3, 4, 5, 6]) mark(ms);
      vi.advanceTimersByTime(99);
      expect(marks).toEqual([]);
      vi.advanceTimersByTime(1);
      expect(marks).toEqual([6]);
    } finally {
      vi.useRealTimers();
    }
  });

  it('cancels a pending mark', () => {
    vi.useFakeTimers();
    try {
      const marks: Array<number | null> = [];
      const mark = debounce<number | null>((ms) => marks.push(ms), 100);
      mark(1);
      mark.cancel();
      vi.advanceTimersByTime(500);
      expect(marks).toEqual([]);
    } finally {
      vi.useRealTimers();
    }
  });
});

describe('the empty states', () => {
  const state = (over: Partial<Parameters<typeof runEventsEmptyState>[0]>) =>
    runEventsEmptyState({
      loading: false,
      events: EVENTS,
      shown: EVENTS,
      filter: EMPTY_RUN_FILTER,
      ...over
    });

  it('says nothing when there are rows to draw', () => {
    expect(state({})).toBeNull();
  });

  it('distinguishes a read in flight from a run that recorded nothing', () => {
    expect(state({ loading: true, events: [], shown: [] })).toBe(LOADING_EVENTS);
    expect(state({ events: [], shown: [] })).toBe(RUN_QUEUED);
  });

  it('distinguishes a kind selection that admits nothing from no match', () => {
    expect(state({ shown: [], filter: parseRunFilter('type:from_another_run') })).toBe(
      NO_KINDS_SELECTED
    );
    expect(state({ shown: [], filter: parseRunFilter('nothing matches this') })).toBe(NO_MATCH);
  });
});

describe('the read cadence against the batching window', () => {
  it('reads far less often than it applies', () => {
    // A 100ms READ cadence is ten requests a second for a run that may last an
    // hour — the poll nobody counted (#2285 C7). Batching exists to make one
    // update out of a page, not to make ten requests out of a second.
    expect(TAIL_POLL_MS).toBeGreaterThan(TAIL_BATCH_MS);
  });
});

describe('the clear control', () => {
  it('is offered only when there is something for it to drop', () => {
    expect(canClearRunFilter(EMPTY_RUN_FILTER)).toBe(false);
    expect(canClearRunFilter(parseRunFilter('type:sandbox_resolved'))).toBe(true);
    expect(canClearRunFilter(parseRunFilter('slow'))).toBe(true);
    expect(canClearRunFilter(parseRunFilter('phase:run_started type:x'))).toBe(true);
  });

  it('is NOT offered for a phase-only selection, which it cannot change', () => {
    // `clearRunFilter` keeps the clicked selection, so here it returns an
    // identical filter: the button was offered, pressed, and nothing moved.
    const phaseOnly = parseRunFilter('phase:run_started');
    expect(clearRunFilter(phaseOnly)).toEqual(phaseOnly);
    expect(canClearRunFilter(phaseOnly)).toBe(false);
  });

  it('is offered exactly when pressing it would change the filter', () => {
    for (const query of ['', 'phase:a', 'phase:a phase:b', 'type:a', 'word', 'phase:a word']) {
      const filter = parseRunFilter(query);
      const moves = runFilterQuery(clearRunFilter(filter)) !== runFilterQuery(filter);
      expect(canClearRunFilter(filter), query).toBe(moves);
    }
  });
});

describe('a page is a page, never the run', () => {
  const page = (count: number) => ({
    events: Array.from({ length: count }, (_, i) => ev(i + 1, 'agent_turn', i))
  });

  it('reads a FULL page as "there may be more"', () => {
    expect(mayHoldMoreRunEvents(page(RUN_AUDIT_PAGE))).toBe(true);
    expect(mayHoldMoreRunEvents(page(RUN_AUDIT_PAGE - 1))).toBe(false);
    expect(mayHoldMoreRunEvents(page(0))).toBe(false);
    expect(mayHoldMoreRunEvents(null)).toBe(false);
  });

  it('counts a truncated page with a + rather than as the whole run', () => {
    expect(runEventsCount(30, 200, true)).toBe('30 of 200+');
    expect(runEventsCount(30, 200, false)).toBe('30 of 200');
  });
});

describe('whether the run is still executing', () => {
  it('takes the terminal reading of the prop and the page’s own read', () => {
    // The loader's `run` prop is never invalidated, so "live" from it alone
    // outlives the run and the tail polls a finished run for ever.
    expect(stillLive(LIVE_STATUSES, 'pending', null)).toBe(true);
    expect(stillLive(LIVE_STATUSES, 'pending', 'pending')).toBe(true);
    expect(stillLive(LIVE_STATUSES, 'pending', 'success')).toBe(false);
    // Monotone: a stale prop can never re-arm a timer the row has stopped,
    // and a fresher read can never restart one the prop already ended.
    expect(stillLive(LIVE_STATUSES, 'success', 'pending')).toBe(false);
    expect(stillLive(LIVE_STATUSES, null, null)).toBe(false);
    expect(stillLive(LIVE_STATUSES, undefined, 'pending')).toBe(false);
  });
});

describe('whose verdict the run page shows', () => {
  it('says it is the run’s own only when the run attested it', () => {
    expect(verdictScopeNote('run')).toContain('this run');
    expect(verdictScopeNote('item')).toContain('work item');
    // An unlabelled round and reviewed head read as this run's evidence; an
    // unrecognised scope is the cautious reading, not the confident one.
    expect(verdictScopeNote('')).toBe(verdictScopeNote('item'));
  });
});
