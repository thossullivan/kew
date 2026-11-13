/**
 * FUI-25 — the Monitor event stream, held to DMonitor (x 607–1400, y ≈476–924)
 * and to the colour ration.
 *
 * The fixture is the artboard's own data — the five audit events from 15:23:18
 * to 15:46:58, `design/factory-ui/DMonitor.events.json` — so the strip this module composes can be checked against the
 * percentages, durations and words the designer drew: marks at 11.0% and
 * 89.89%, "22m 35s quiet", "3 events in 8s", "budget refusal".
 *
 * Two things the tests guard beyond position. Green is spent on an attestation
 * that came back good and on nothing else, so a scan that reported findings,
 * and a posture that merely records which sandbox ran, stay neutral. And a
 * fetch that failed says "stream unavailable": an empty list on this screen
 * reads as a calm factory, which is the one wrong answer.
 */
import { readFileSync } from 'node:fs';
import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import type { AuditEvent, AuditList } from '$lib/api';
import { unreadable } from '$lib/monitor/readouts';
import auditJson from '../../../design/factory-ui/DMonitor.events.json';
import EventStream from './EventStream.svelte';
import {
  composeStream,
  duration,
  eventCategory,
  fitStrip,
  MARK_COLOR,
  MAX_MAJOR_TICKS,
  MAX_SKEW_MS,
  spellDuration,
  STREAM_EMPTY,
  oldestRead,
  STREAM_LIMIT,
  STREAM_POLL_MS,
  STREAM_SETTLE_MS,
  STREAM_UNSEEN,
  STREAM_UNSEEN_ARIA,
  streamWindow,
  STRIP,
  tickStepsFor,
  WINDOW_MINUTES,
  type StreamRow,
  type StreamStrip
} from './stream';

/** The component's own source, for the two things only markup can say. */
const STREAM = readFileSync(new URL('./EventStream.svelte', import.meta.url), 'utf8');

const DMONITOR = readFileSync(
  new URL('../../../design/factory-ui/DMonitor.html', import.meta.url),
  'utf8'
);

const audit: AuditList = auditJson;

/** A row as it reads: the lead, then everything the log recorded after it. */
const words = (row: StreamRow): string =>
  row.kind !== 'event' ? row.label : row.rest ? `${row.lead} · ${row.rest}` : row.lead;

/** The artboard's clock: the newest event it draws. */
const NOW = Date.parse('2026-09-24T15:46:58Z');

/** Everything read at the artboard's own moment. */
const reading = composeStream(audit, NOW);
const strip = reading.available ? reading.strip : null;
const rows = reading.available ? reading.rows : [];

function event(over: Partial<AuditEvent> = {}): AuditEvent {
  return {
    id: 1,
    timestamp: '2026-09-24T15:46:58+00:00',
    event: 'dispatch_started',
    issue_num: 2055,
    item_id: null,
    run_id: 'f5dcb6a5',
    actor_type: 'host',
    actor_id: 'kew',
    summary: null,
    epic_ref: null,
    ...over
  };
}

/**
 * A log page of `[minutes before NOW, event name]`, newest first.
 * `truncated` sets `next_after`, the API's "older events exist" signal.
 */
function page(spec: [number, string][], truncated = false): AuditList {
  const events = spec.map(([minutes, name], i) => ({
    ...event({ id: spec.length - i, event: name }),
    timestamp: new Date(NOW - minutes * 60_000).toISOString()
  }));
  return { events, next_after: truncated ? (events.at(-1)?.id ?? null) : null };
}

describe('the window', () => {
  it('spans 30 minutes ending at the next 5-minute mark', () => {
    const { startMs, endMs } = streamWindow(NOW);
    expect(new Date(endMs).toISOString()).toBe('2026-09-24T15:50:00.000Z');
    expect(new Date(startMs).toISOString()).toBe('2026-09-24T15:20:00.000Z');
    expect(endMs - startMs).toBe(WINDOW_MINUTES * 60_000);
  });

  it('already sits on the mark when the clock does', () => {
    const { endMs } = streamWindow(Date.parse('2026-09-24T15:45:00Z'));
    expect(new Date(endMs).toISOString()).toBe('2026-09-24T15:45:00.000Z');
  });

  it('follows a stamp ahead by ordinary skew, and no stamp ahead of that', () => {
    // A host and a viewer disagreeing by minutes is skew, and the axis reaches
    // the event so it has a place above the row listing it.
    const skewed = streamWindow(NOW, [NOW + MAX_SKEW_MS]);
    expect(new Date(skewed.endMs).toISOString()).toBe('2026-09-24T15:55:00.000Z');
    // A day ahead is not skew, it is a clock that is wrong, and the window
    // stays where the rest of the page is.
    const wrong = streamWindow(NOW, [NOW + 86_400_000]);
    expect(new Date(wrong.endMs).toISOString()).toBe('2026-09-24T15:50:00.000Z');
    expect(wrong.endMs - wrong.startMs).toBe(WINDOW_MINUTES * 60_000);
  });

  it('still reaches the newest stamp inside the tolerance past a wrong one', () => {
    // Dropping the far stamp leaves the window the one it would have been
    // without it — the near stamp's. Resetting the reach to the viewer's clock
    // instead stripped a legitimately skewed event of its mark on the axis
    // while the row below went on listing it.
    const { endMs } = streamWindow(NOW, [NOW + MAX_SKEW_MS, NOW + 86_400_000]);
    expect(new Date(endMs).toISOString()).toBe('2026-09-24T15:55:00.000Z');
  });

  it('keeps the strip on the rows when one event is stamped a day ahead', () => {
    // The whole failure the bound exists for: an unbounded window follows the
    // stray stamp a day out, so the strip's 30 minutes hold nothing but it —
    // every other mark off the left edge, and a truncated page's unread run
    // reading as covered because the page now "reaches back" past the start.
    const spec: [number, string][] = [
      [-1440, 'run_finished'],
      [2, 'dispatch_started'],
      [25, 'budget_exceeded']
    ];
    const reading = composeStream(page(spec, true), NOW);
    const seen = reading.available ? reading.strip : null;
    expect(new Date(seen!.endMs).toISOString()).toBe('2026-09-24T15:50:00.000Z');
    // The two events the window does hold are still drawn, oldest first.
    expect(seen!.marks.map((m) => m.id)).toEqual([1, 2]);
    // And the page that stopped inside the window still says so.
    expect(seen!.unseen).not.toBeNull();
    // The stray event is not deleted — it is listed, just not on the axis.
    expect(reading.available && reading.rows.filter((r) => r.kind === 'event')).toHaveLength(3);
  });

  it('ticks every minute, with a labelled major tick every five', () => {
    const ticks = strip!.ticks;
    expect(ticks).toHaveLength(31);
    expect(ticks.filter((t) => t.major).map((t) => t.label)).toEqual([
      '15:20',
      '15:25',
      '15:30',
      '15:35',
      '15:40',
      '15:45',
      '15:50'
    ]);
    expect(ticks.filter((t) => !t.major).every((t) => t.label === null)).toBe(true);
  });

  it('keeps the first and last axis label inside the strip', () => {
    expect(strip!.ticks[0].align).toBe('start');
    expect(strip!.ticks.at(-1)!.align).toBe('end');
    expect(strip!.ticks[5].align).toBe('center');
  });
});

describe('marks', () => {
  it('stands each event where the artboard drew it', () => {
    const at = Object.fromEntries(strip!.marks.map((m) => [m.id, Number(m.pct.toFixed(2))]));
    expect(at).toEqual({ 1: 11.0, 2: 14.17, 3: 89.44, 4: 89.67, 5: 89.89 });
    for (const pct of ['11.0%', '14.17%', '89.44%', '89.67%', '89.89%']) {
      expect(DMONITOR, pct).toContain(pct);
    }
  });

  it('drops an event older than the window rather than pinning it to the edge', () => {
    const composed = composeStream(page([[45, 'dispatch_started']]), NOW);
    expect(composed.available && composed.strip.marks).toEqual([]);
    // It is still in the list: the strip is 30 minutes, the log is not.
    expect(composed.available && composed.rows).toHaveLength(1);
  });
});

describe('quiet gaps', () => {
  it('finds the artboard’s 22m 35s of silence and names it', () => {
    expect(strip!.gaps).toHaveLength(1);
    const [gap] = strip!.gaps;
    expect(gap.label).toBe('22m 35s quiet');
    expect(DMONITOR).toContain('22m 35s quiet');
    expect(Number(gap.fromPct.toFixed(2))).toBe(14.17);
    expect(Number(gap.toPct.toFixed(2))).toBe(89.44);
    expect(Number(gap.midPct.toFixed(1))).toBe(51.8);
  });

  it('leaves a silence under five minutes closed up', () => {
    // The fixture's two oldest events are 57s apart and draw no gap.
    const composed = composeStream(page([[0, 'a'], [4.9, 'b']]), NOW);
    expect(composed.available && composed.strip.gaps).toEqual([]);
  });

  it('opens a gap at exactly five minutes', () => {
    const composed = composeStream(page([[0, 'a'], [5, 'b']]), NOW);
    expect(composed.available && composed.strip.gaps.map((g) => g.label)).toEqual(['5m 00s quiet']);
  });
});

describe('bursts', () => {
  it('labels the artboard’s three events in eight seconds', () => {
    expect(strip!.bursts).toHaveLength(1);
    const [burst] = strip!.bursts;
    expect(burst.label).toBe('3 events in 8s');
    expect(DMONITOR).toContain('3 events in 8s');
    // Anchored on the newest mark of the run, which is where it is drawn from.
    expect(Number(burst.pct.toFixed(2))).toBe(89.89);
  });

  it('does not call two close events a burst', () => {
    const composed = composeStream(page([[0, 'a'], [0.5, 'b']]), NOW);
    expect(composed.available && composed.strip.bursts).toEqual([]);
  });

  it('does not join events a minute and more apart into one', () => {
    const composed = composeStream(page([[0, 'a'], [1.5, 'b'], [3, 'c']]), NOW);
    expect(composed.available && composed.strip.bursts).toEqual([]);
  });

  it('counts a longer run whole', () => {
    const composed = composeStream(page([[0, 'a'], [0.5, 'b'], [1, 'c'], [1.5, 'd']]), NOW);
    expect(composed.available && composed.strip.bursts.map((b) => b.label)).toEqual([
      '4 events in 1m 30s'
    ]);
  });
});

/**
 * #2248 — the words a brushed window can hold. The gap and burst rules are
 * clock time, tuned for 30 minutes; over six hours they named dozens of things
 * a few pixels apart and the labels printed over each other, over the marks,
 * and past the strip's left edge into the holds column.
 */
describe('fitting the strip to its width', () => {
  /** The artboard's strip, 793px wide (DMonitor x 607–1400). */
  const ARTBOARD_PX = 793;

  const bare: StreamStrip = {
    startMs: 0,
    endMs: 1,
    ticks: [],
    marks: [],
    gaps: [],
    bursts: [],
    unseen: null
  };

  const gap = (fromPct: number, toPct: number, label = '9m 05s quiet') => ({
    fromPct,
    toPct,
    midPct: (fromPct + toPct) / 2,
    label
  });

  const burst = (pct: number, count: number) => ({
    pct,
    count,
    label: `${count} events in 9s`
  });

  it('leaves the artboard’s strip exactly as composed', () => {
    expect(fitStrip(strip!, ARTBOARD_PX)).toEqual(strip);
  });

  it('draws everything before the strip has been measured', () => {
    const crowded = { ...bare, bursts: [burst(1, 3), burst(1.5, 4)], gaps: [gap(2, 3)] };
    expect(fitStrip(crowded, 0)).toEqual(crowded);
  });

  it('keeps a gap’s run and drops its words where the run cannot hold them', () => {
    const fitted = fitStrip({ ...bare, gaps: [gap(10, 12), gap(20, 60)] }, ARTBOARD_PX);
    expect(fitted.gaps).toHaveLength(2);
    expect(fitted.gaps.map((g) => g.label)).toEqual(['', '9m 05s quiet']);
  });

  it('names the unread run only where it can hold the words', () => {
    const run = (toPct: number) => ({ ...gap(0, toPct), label: STREAM_UNSEEN });
    expect(fitStrip({ ...bare, unseen: run(5) }, ARTBOARD_PX).unseen?.label).toBe('');
    expect(fitStrip({ ...bare, unseen: run(60) }, ARTBOARD_PX).unseen?.label).toBe(
      STREAM_UNSEEN
    );
  });

  it('labels the biggest of two crowded bursts, and never overlaps two', () => {
    const fitted = fitStrip({ ...bare, bursts: [burst(50, 3), burst(52, 12)] }, ARTBOARD_PX);
    expect(fitted.bursts.map((b) => b.count)).toEqual([12]);
  });

  it('keeps bursts that have room, in time order', () => {
    const fitted = fitStrip(
      { ...bare, bursts: [burst(30, 3), burst(60, 12), burst(90, 4)] },
      ARTBOARD_PX
    );
    expect(fitted.bursts.map((b) => b.count)).toEqual([3, 12, 4]);
  });

  it('does not let a burst read past the strip’s left edge', () => {
    // Labelled leftwards from its newest mark, 2% in: the words would start
    // ~90px left of the strip, in the column beside it.
    expect(fitStrip({ ...bare, bursts: [burst(2, 5)] }, ARTBOARD_PX).bursts).toEqual([]);
  });

  it('gives a money annotation the line before any burst', () => {
    const mark = { id: 1, pct: 40, category: 'money' as const, annotation: 'budget refusal' };
    const fitted = fitStrip({ ...bare, marks: [mark], bursts: [burst(45, 30)] }, ARTBOARD_PX);
    expect(fitted.marks[0].annotation).toBe('budget refusal');
    expect(fitted.bursts).toEqual([]);
  });

  it('keeps a mark whose annotation has no room, without the words', () => {
    const a = { id: 1, pct: 40, category: 'money' as const, annotation: 'budget refusal' };
    const b = { ...a, id: 2, pct: 41 };
    const fitted = fitStrip({ ...bare, marks: [a, b] }, ARTBOARD_PX);
    expect(fitted.marks.map((m) => m.annotation)).toEqual(['budget refusal', null]);
  });
});

describe('the colour ration', () => {
  it('reads every event the fixtures carry', () => {
    const byName = Object.fromEntries(audit.events.map((e) => [e.event, eventCategory(e)]));
    expect(byName).toEqual({
      dispatch_completed: 'neutral',
      publication_output_scan: 'evidence',
      publication_posture: 'neutral',
      sandbox_resolved: 'neutral',
      approval_hold: 'money'
    });
  });

  it('falls back to neutral for an event name it has never seen', () => {
    expect(eventCategory(event({ event: 'some_future_event' }))).toBe('neutral');
    expect(eventCategory(event({ event: 'some_future_event', summary: 'clean' }))).toBe('neutral');
  });

  it('spends danger only on an unconfirmed host lease takeover', () => {
    expect(eventCategory(event({ event: 'host_lease_takeover_unconfirmed' }))).toBe('danger');
    expect(eventCategory(event({ event: 'host_lease_lapsed' }))).toBe('neutral');
  });

  it('spends yellow on money whoever recorded it', () => {
    expect(eventCategory(event({ event: 'loop_budget_exceeded' }))).toBe('money');
    expect(eventCategory(event({ event: 'spend_recorded' }))).toBe('money');
    expect(
      eventCategory(event({ event: 'approval_hold', actor_type: 'policy', actor_id: 'budget' }))
    ).toBe('money');
  });

  it('spends magenta on a hold that is waiting for a person, not on one taken', () => {
    expect(eventCategory(event({ event: 'approval_hold', actor_type: 'human' }))).toBe('needs-you');
    expect(eventCategory(event({ event: 'needs_human_raised' }))).toBe('needs-you');
    expect(eventCategory(event({ event: 'approval_granted', actor_type: 'human' }))).toBe(
      'neutral'
    );
  });

  it('spends green only on an attestation that came back good', () => {
    const scan = (summary: string) =>
      eventCategory(event({ event: 'publication_output_scan', summary }));
    expect(scan('clean · 3670e43f')).toBe('evidence');
    expect(scan('{"verdict":"clean","digest":"3670e43f"}')).toBe('evidence');
    // `found` is the other word publication_scan.py records. Nothing else is.
    expect(scan('{"verdict":"found","matches":{"github_token_shape":2}}')).toBe('neutral');
  });

  /**
   * The verdicts here are the ones kew actually emits, not plausible
   * synonyms: a pass word this module invents is a green tick nothing on the
   * backend can ever earn, and it renders a genuine READY neutral while the
   * test suite goes on passing against the fiction.
   */
  it('reads the review verdicts kew emits, and greens only READY', () => {
    const verdict = (v: string) =>
      eventCategory(
        event({
          event: 'adversarial_review_verdict',
          summary: `{"pr_number":2133,"item_id":"issue-2084","round":1,"verdict":"${v}"}`
        })
      );
    expect(verdict('READY')).toBe('evidence');
    for (const held of [
      'NEEDS_CHANGES',
      'NEEDS_FULL',
      'DEGRADED',
      'UNPARSEABLE',
      'MERGED_UNREVIEWED',
      'PRE_POLICY'
    ]) {
      expect(verdict(held), held).toBe('neutral');
    }
  });

  it('holds those verdict words to the backend that emits them', () => {
    const src = (path: string) =>
      readFileSync(new URL(`../../../../kew/src/kew/${path}`, import.meta.url), 'utf8');
    // If these constants move, the map above is drawing a colour off a word
    // that no longer exists — and neutral-by-default hides it.
    expect(src('review_delta.py')).toContain('VERDICT_READY = "READY"');
    expect(src('review_audit.py')).toContain('VERDICT_DEGRADED = "DEGRADED"');
    expect(src('publication_scan.py')).toContain('verdict: Literal["clean", "found"]');
  });

  it('reads the verdict by its key, never off a neighbouring field', () => {
    // `acceptance_reason` is free text out of the reviewer's own sidecar
    // (review_contract.py). Scanning the excerpt for a good-sounding word
    // lets the reviewer paint its own NEEDS_CHANGES green.
    expect(
      eventCategory(
        event({
          event: 'adversarial_review_verdict',
          summary: '{"verdict":"NEEDS_CHANGES","acceptance_reason":"acceptance checks passed"}'
        })
      )
    ).toBe('neutral');
    // An excerpt truncated before the verdict key states nothing, so it stays
    // neutral rather than borrowing a verdict from whatever survived.
    expect(
      eventCategory(
        event({ event: 'adversarial_review_verdict', summary: '{"item_id":"clean-up-pass"…' })
      )
    ).toBe('neutral');
  });

  it('paints the mark, and only the mark', () => {
    expect(MARK_COLOR).toEqual({
      money: 'var(--warn)',
      evidence: 'var(--success)',
      'needs-you': 'var(--accent)',
      danger: 'var(--danger)',
      neutral: 'var(--text-2)'
    });
    // #2282 D15: the row's words used to be painted the same four colours.
    // There is no second colour table to paint them with any more, and the
    // component reaches for no inline colour on a line either.
    expect(STREAM).not.toContain('TEXT_COLOR');
    // The list, which is where the words are. The strip above it still paints
    // its own annotation in the mark's colour — that whole strip is decorative
    // and `aria-hidden`, and the annotation IS a mark's label.
    const list = STREAM.slice(STREAM.indexOf('<ol '), STREAM.indexOf('</ol>'));
    expect(list).not.toContain('style:color');
  });

  it('annotates the budget refusal and nothing that was not money', () => {
    const annotated = strip!.marks.filter((m) => m.annotation !== null);
    expect(annotated.map((m) => [m.id, m.annotation])).toEqual([[1, 'budget refusal']]);
    expect(DMONITOR).toContain('budget refusal');
  });
});

describe('durations', () => {
  it('writes the compact form the strip and the list use', () => {
    expect(duration(8_000)).toBe('8s');
    expect(duration(1_355_000)).toBe('22m 35s');
    expect(duration(3_900_000)).toBe('1h 05m');
    expect(duration(-5_000)).toBe('0s');
  });

  it('stops at two units, so an idle weekend still reads at a glance', () => {
    expect(duration(183_240_000)).toBe('2d 02h');
    expect(spellDuration(183_240_000)).toBe('2 days 2 hours');
  });

  it('spells the same span out for a screen reader', () => {
    expect(spellDuration(1_355_000)).toBe('22 minutes 35 seconds');
    expect(spellDuration(300_000)).toBe('5 minutes');
    expect(spellDuration(1_000)).toBe('1 second');
  });
});

/**
 * #2282 (D15) — what an empty window says of itself.
 *
 * The charts are drawn for the window; the list below them is deliberately
 * not clipped to it. So a Live 30m on a quiet fleet drew a bare axis over a
 * list of yesterday's events and said nothing at all about the difference,
 * which reads as a feed that has broken. Empty states are sentences.
 */
describe('an empty window', () => {
  /** The strip's reading for a window that holds nothing. */
  const sentence = (list: AuditList, window?: { startMs: number; endMs: number }): string => {
    const read = composeStream(list, NOW, window);
    return read.available ? read.emptyWindow : '';
  };

  /** A page whose newest event is 90 minutes older than the 30m window. */
  const behind = page([
    [90, 'dispatch_started'],
    [95, 'approval_hold']
  ]);

  it('says so, and names the last event there was, without seconds', () => {
    // The list still shows both events; the strip has no mark on it. 14:16:58
    // reads as 14:16 here, as it does on the "Load older" label beside it.
    expect(sentence(behind)).toBe('No events in this window · last event 14:16');
  });

  it('says nothing where the window holds events', () => {
    expect(sentence(page([[1, 'dispatch_started']]))).toBe('');
  });

  it('promises an update only where the window follows the clock', () => {
    const nothing: AuditList = { events: [], next_after: null };
    expect(sentence(nothing)).toBe('No events yet');
    // A chosen window is a window that has been left behind: "yet" there is a
    // promise nothing can keep.
    expect(sentence(nothing, { startMs: NOW - 7_200_000, endMs: NOW - 3_600_000 })).toBe(
      'No events in this window'
    );
  });

  it('is the same count the axis above it is drawn from', () => {
    const read = composeStream(behind, NOW);
    expect(read.available && read.strip.marks).toHaveLength(0);
    // …over a list that is not empty, which is the whole reason for saying it.
    expect(read.available && read.rows.length).toBeGreaterThan(0);
    const full = composeStream(audit, NOW);
    expect(full.available && full.emptyWindow).toBe('');
  });
});

describe('the list', () => {
  it('reads time · label · subject · fact, newest first, with the quiet row between', () => {
    // The epic on each row is `epic_ref`, the event's own column, rather than a
    // field found in a capped excerpt (#2248). The lead is a label and never a
    // code (#2282 D15), which is the capital on each first word.
    expect(rows.map((r) => (r.kind === 'event' ? [r.time, words(r)] : [r.kind, words(r)]))).toEqual([
      ['15:46:58', 'Dispatch completed · launch-fixes · #2055 · run f5dcb6a5 · success · PR #2059'],
      ['15:46:54', 'Publication output scanned · launch-fixes · #2055 · clean · 3670e43f'],
      ['15:46:50', 'Publication posture recorded · launch-fixes · #2055 · canonical'],
      ['gap', '22m 35s quiet'],
      ['15:24:15', 'Sandbox resolved · launch-fixes · #2055 · sandboxed'],
      [
        '15:23:18',
        'Held for approval · launch-fixes · #2055 · dispatch refused · budget_exceeded (weekly)'
      ]
    ]);
  });

  it('splits the lead from the recorded words, so only one of them leads', () => {
    const first = rows[0];
    expect(first.kind === 'event' && first.lead).toBe('Dispatch completed');
    expect(first.kind === 'event' && first.rest).toBe(
      'launch-fixes · #2055 · run f5dcb6a5 · success · PR #2059'
    );
  });

  it('gives the quiet row the sentence a screen reader can use', () => {
    const gap = rows.find((r) => r.kind === 'gap');
    expect(gap && gap.kind === 'gap' && gap.aria).toBe('22 minutes 35 seconds without events');
    expect(DMONITOR).toContain('22 minutes 35 seconds without events');
  });

  it('colours each row by what its event meant', () => {
    const coloured = rows
      .filter((r) => r.kind === 'event')
      .map((r) => (r.kind === 'event' ? r.category : null));
    expect(coloured).toEqual(['neutral', 'evidence', 'neutral', 'neutral', 'money']);
  });

  it('names the event alone when the log recorded no payload', () => {
    const composed = composeStream({ events: [event({ summary: null })], next_after: null }, NOW);
    // The event column's item is still named: the line is short of a fact,
    // not short of a subject.
    expect(composed.available && words(composed.rows[0])).toBe('Dispatch started · #2055');
  });

  it('carries the payload the line was read from, for the row’s expand', () => {
    // `event-line.ts` owns the reading (and its own test file holds it); what
    // this pins is that the ROW carries it, so the list and the expand under
    // it cannot come from two different readings of the same event (#2210).
    const payload = '{"reason":"budget_exceeded","scope":"weekly"}';
    const composed = composeStream(
      { events: [event({ event: 'approval_hold', summary: payload })], next_after: null },
      NOW
    );
    const row = composed.available ? composed.rows[0] : null;
    // The value worded (#2896 R5), the recorded code kept for the row's title.
    expect(row && words(row)).toBe('Held for approval · #2055 · budget exceeded');
    expect(row && row.kind === 'event' && row.code).toBe('budget_exceeded');
    expect(row && row.kind === 'event' && row.detail).toContain('"scope": "weekly"');
  });

  it('offers no expand where the line already prints everything recorded', () => {
    const prose = { events: [event({ summary: 'canonical' })], next_after: null };
    const composed = composeStream(prose, NOW);
    const row = composed.available ? composed.rows[0] : null;
    expect(row && row.kind === 'event' && row.detail).toBe('');
  });

  it('heads the section with the last event and the count', () => {
    expect(reading.available && reading.meta).toBe('last event 15:46:58 · 5 events');
  });

  it('does not pluralise one event', () => {
    const composed = composeStream({ events: [event()], next_after: null }, NOW);
    expect(composed.available && composed.meta).toContain('1 event');
  });
});

/**
 * One page against a 30-minute window: a factory busy enough to fill the page
 * inside the window leaves its earlier minutes unread, and blank on this strip
 * reads as calm. `next_after` is the only signal that happened — the page size
 * itself is not, which is why the geometry below is stated in events and
 * minutes rather than borrowed from `STREAM_LIMIT`.
 */
describe('a page that ran out before the window did', () => {
  /** 50 events inside the last 12 minutes, with older ones beyond the page. */
  const busy = page(
    Array.from({ length: 50 }, (_, i) => [i * 0.24, 'dispatch_started'] as [number, string]),
    true
  );
  const busyRead = composeStream(busy, NOW);
  const busyStrip = busyRead.available ? busyRead.strip : null;

  it('names the unread stretch instead of drawing it as silence', () => {
    const unseen = busyStrip!.unseen;
    expect(unseen).not.toBeNull();
    expect(unseen!.label).toBe(STREAM_UNSEEN);
    expect(unseen!.fromPct).toBe(0);
    // The page's oldest event is 11m 45.6s before NOW, i.e. 15:35:12.4 in a
    // window opening at 15:20 — so a little over half the strip was unread.
    expect(Number(unseen!.toPct.toFixed(2))).toBe(50.69);
    expect(unseen!.midPct).toBe(unseen!.toPct / 2);
    // And nothing in the unread stretch was passed off as a quiet gap.
    expect(busyStrip!.gaps).toEqual([]);
  });

  it('closes the list with a row saying the page stopped, not the log', () => {
    const rows = busyRead.available ? busyRead.rows : [];
    const tail = rows.at(-1)!;
    expect(tail.kind).toBe('unseen');
    expect(tail.kind === 'unseen' && tail.label).toBe(STREAM_UNSEEN);
    expect(tail.kind === 'unseen' && tail.aria).toBe(STREAM_UNSEEN_ARIA);
    expect(rows.filter((r) => r.kind === 'unseen')).toHaveLength(1);
  });

  it('counts what the page holds rather than passing it off as the total', () => {
    expect(busyRead.available && busyRead.meta).toBe(`last event 15:46:58 · newest 50 events`);
  });

  it('says nothing when the page reaches back past the window', () => {
    // Truncated, but its oldest event predates 15:20 — the window is covered.
    const composed = composeStream(page([[0, 'a'], [40, 'b']], true), NOW);
    expect(composed.available && composed.strip.unseen).toBeNull();
    // The list still ends with the marker: the log goes on past the page.
    expect(composed.available && composed.rows.at(-1)!.kind).toBe('unseen');
  });

  it('leaves an untruncated page alone', () => {
    expect(strip!.unseen).toBeNull();
    expect(rows.some((r) => r.kind === 'unseen')).toBe(false);
    expect(reading.available && reading.meta).not.toContain('newest');
  });

  it('adds no marker to an empty page', () => {
    const empty = composeStream({ events: [], next_after: null }, NOW);
    expect(empty.available && empty.rows).toEqual([]);
  });

  it('draws the unread run dotted, so it cannot read as a quiet gap', () => {
    const body = render(EventStream, { props: { source: busy, nowMs: NOW } }).body;
    expect(body).toContain(STREAM_UNSEEN);
    expect(body).toContain('border-dotted');
  });
});

describe('a reading that could not be taken', () => {
  it('says the stream is unavailable rather than showing an empty list', () => {
    const failed = composeStream(unreadable('stream', new Error('GET /api/audit -> 503')), NOW);
    expect(failed.available).toBe(false);
    expect(!failed.available && failed.notice).toBe('stream unavailable (HTTP 503)');
  });

  it('keeps an empty log distinct from a failed read', () => {
    const empty = composeStream({ events: [], next_after: null }, NOW);
    expect(empty.available).toBe(true);
    expect(empty.available && empty.rows).toEqual([]);
    expect(empty.available && empty.meta).toBe('0 events');
  });
});

describe('composition', () => {
  it('is pure: the same page at the same moment is the same reading', () => {
    const before = JSON.stringify(audit);
    expect(composeStream(audit, NOW)).toEqual(composeStream(audit, NOW));
    expect(JSON.stringify(audit)).toBe(before);
  });
});

/**
 * #2248 — the strip is drawn for the brushed window, not only for 30 minutes.
 *
 * The window arrives from `stream-query.ts` and is used as given: no 5-minute
 * snap and no skew-following, because the operator chose those two edges and the
 * overview above the strip is drawing the same two. Everything else about the
 * strip — the marks, the quiet gaps, the bursts, the unread run — is the same
 * reading over a different span, which is what the assertions below check.
 */
describe('a brushed window', () => {
  const brush = (fromMinutes: number, toMinutes: number) => ({
    startMs: NOW - fromMinutes * 60_000,
    endMs: NOW - toMinutes * 60_000
  });

  it('draws the window it was given, to the millisecond', () => {
    const window = brush(105, 0);
    const read = composeStream(audit, NOW, window);
    expect(read.available && read.strip.startMs).toBe(window.startMs);
    expect(read.available && read.strip.endMs).toBe(window.endMs);
  });

  it('spaces the axis for the span, not for 30 minutes', () => {
    // 1h 45m: a 15-minute major with a 5-minute minor, as the artboard draws it.
    const read = composeStream(audit, NOW, brush(105, 0));
    const ticks = read.available ? read.strip.ticks : [];
    const majors = ticks.filter((tick) => tick.major).map((tick) => tick.label);
    expect(majors).toEqual(['14:15', '14:30', '14:45', '15:00', '15:15', '15:30', '15:45']);
    expect(ticks.filter((tick) => !tick.major)).toHaveLength(14);
  });

  it('lays the ticks on the clock, so a window off the mark still reads 15:30', () => {
    const read = composeStream(audit, NOW, { startMs: NOW - 3_600_000 + 1234, endMs: NOW });
    const labels = read.available
      ? read.strip.ticks.flatMap((tick) => (tick.label ? [tick.label] : []))
      : [];
    expect(labels).toContain('15:30');
    expect(labels.every((label) => /^\d\d:[0-9]0$/.test(label))).toBe(true);
  });

  it('names the day on a window wide enough for 00:00 to be ambiguous', () => {
    const week = { startMs: NOW - 7 * 86_400_000, endMs: NOW };
    const read = composeStream(audit, NOW, week);
    const labels = read.available
      ? read.strip.ticks.flatMap((tick) => (tick.label ? [tick.label] : []))
      : [];
    expect(labels[0]).toMatch(/^\d\d-\d\d \d\d:\d\d$/);
    expect(labels).toContain('09-24 00:00');
  });

  it('places a mark by the window, and drops what falls outside it', () => {
    // The artboard's five events span 15:23:18–15:46:58. A window of the last
    // ten minutes holds the three newest and none of the older two.
    const read = composeStream(audit, NOW, brush(10, 0));
    expect(read.available && read.strip.marks.map((mark) => mark.id)).toEqual([3, 4, 5]);
    // The rows are NOT clipped: the list is the page, the strip is the window.
    expect(read.available && read.rows.filter((row) => row.kind === 'event')).toHaveLength(5);
  });

  it('keeps the 30-minute reading when no window is given', () => {
    const unbrushed = composeStream(audit, NOW);
    expect(unbrushed).toEqual(composeStream(audit, NOW, undefined));
    expect(unbrushed.available && unbrushed.strip.startMs).toBe(streamWindow(NOW, [NOW]).startMs);
  });
});

/**
 * #2248 — a span no rung of the tick ladder is coarse enough for.
 *
 * `from`/`to` arrive from the URL, so the strip's span is whatever a link says
 * it is, and the cap on labelled ticks has to hold for all of them. Falling
 * through to the ladder's coarsest rung was not a cap: anything over about 16
 * days got two-day majors and twelve-hour minors between them, so a year-long
 * window asked for 730 ticks and a decade-long one for thousands — on an axis
 * 793px wide.
 */
describe('the axis of a very wide window', () => {
  const DAY = 86_400_000;

  it('spaces the majors to the cap, whatever the span', () => {
    for (const span of [17 * DAY, 90 * DAY, 365 * DAY, 40 * 365 * DAY]) {
      const { majorMs, minorMs } = tickStepsFor(span);
      expect(span / majorMs, `${span / DAY}d`).toBeLessThanOrEqual(MAX_MAJOR_TICKS);
      // Still a round amount of clock time, and still a multiple of the step the
      // axis is laid out in — a major the minor does not divide is never reached,
      // which would be an axis of unlabelled ticks.
      expect(majorMs % DAY, `${span / DAY}d`).toBe(0);
      expect(majorMs % minorMs, `${span / DAY}d`).toBe(0);
    }
  });

  it('lays a bounded axis for a year, every tick of it labelled', () => {
    const read = composeStream(audit, NOW, { startMs: NOW - 365 * DAY, endMs: NOW });
    const ticks = read.available ? read.strip.ticks : [];
    expect(ticks.length).toBeLessThanOrEqual(MAX_MAJOR_TICKS + 1);
    expect(ticks.every((tick) => tick.major && tick.label !== null)).toBe(true);
  });

  it('leaves the spans the ladder does cover exactly as they were', () => {
    expect(tickStepsFor(30 * 60_000)).toEqual({ majorMs: 5 * 60_000, minorMs: 60_000 });
    expect(tickStepsFor(7 * DAY)).toEqual({ majorMs: DAY, minorMs: 6 * 3_600_000 });
  });

  /**
   * Two majors with the same label are two ticks a reader cannot tell apart, on
   * the one control that says which window is on the screen.
   *
   * "MM-DD HH:MM" wraps around in a year and "HH:MM" wraps around in a day, and
   * an axis spans up to `MAX_MAJOR_TICKS` majors — eight 73 days apart span 584
   * of them, and the first and the sixth both read "10-02 00:00". So the sweep
   * is every span, not the three the assertions above happen to name: a label
   * has to carry whatever field its own axis can wrap around.
   *
   * The sweep composes ~3,700 strips: 1.7s on the operator Mac, 4.1s alone on
   * the Linux verify VM, 5.7s there under the full suite, so it carries its own
   * timeout rather than the 5s default.
   */
  it('tells every major on the axis apart, at every span', { timeout: 30_000 }, () => {
    const majorsFor = (spanMs: number) => {
      const read = composeStream(audit, NOW, { startMs: NOW - spanMs, endMs: NOW });
      return read.available
        ? read.strip.ticks.flatMap((tick) => (tick.label === null ? [] : [tick.label]))
        : [];
    };
    for (let hours = 1; hours <= 96; hours += 1) {
      const labels = majorsFor(hours * 3_600_000);
      expect(new Set(labels).size, `${hours}h`).toBe(labels.length);
    }
    for (let days = 5; days <= 3650; days += 1) {
      const labels = majorsFor(days * DAY);
      expect(new Set(labels).size, `${days}d`).toBe(labels.length);
    }
  });
});

/**
 * What "Load older" reaches back past — which is the oldest event in hand, and
 * never the window's own start. The window start goes out as `since`, which is
 * INCLUSIVE, so every event the button can load is at or AFTER it: a label that
 * printed it promised events from before an instant it was asking for events
 * after.
 */
describe('the instant the next page back is older than', () => {
  it('is the oldest event the page in hand holds', () => {
    const held = page([
      [0, 'dispatch_started'],
      [22, 'budget_refusal'],
      [40, 'review_passed']
    ]);
    expect(oldestRead(held.events)).toBe(NOW - 40 * 60_000);
  });

  it('is the oldest instant whatever order the page arrived in', () => {
    const held = page([
      [40, 'review_passed'],
      [0, 'dispatch_started']
    ]);
    expect(oldestRead(held.events)).toBe(NOW - 40 * 60_000);
  });

  it('is nothing at all for a page with no readable stamp', () => {
    expect(oldestRead([])).toBeNull();
    expect(oldestRead([event({ timestamp: 'not a time' })])).toBeNull();
  });
});

describe('a status row', () => {
  it('names the transition, which is what makes each one different (#2248)', () => {
    const read = composeStream(
      {
        events: [
          event({
            event: 'epic_node_status',
            issue_num: 2219,
            epic_ref: 'ui-refine',
            summary: '{"epic_ref":"ui-refine","from":"in_review","to":"done"}'
          })
        ],
        next_after: null
      },
      NOW
    );
    const row = read.available ? read.rows[0] : null;
    expect(row && words(row)).toBe('Epic node status changed · ui-refine · #2219 · in review → done');
    // The move as recorded stays on the row, for its title (#2896 R5).
    expect(row && row.kind === 'event' && row.code).toBe('in_review → done');
  });

  it('carries what the row can link to, read off the event\u2019s own columns', () => {
    const read = composeStream(
      { events: [event({ issue_num: 2219, run_id: '90afb9f4' })], next_after: null },
      NOW
    );
    const row = read.available ? read.rows[0] : null;
    expect(row?.kind === 'event' && row.itemId).toBe('2219');
    expect(row?.kind === 'event' && row.ref).toBe('#2219');
    expect(row?.kind === 'event' && row.runId).toBe('90afb9f4');
  });

  it('offers no link where the event names nothing', () => {
    const read = composeStream(
      { events: [event({ issue_num: null, item_id: null, run_id: null })], next_after: null },
      NOW
    );
    const row = read.available ? read.rows[0] : null;
    expect(row?.kind === 'event' && row.itemId).toBeNull();
    expect(row?.kind === 'event' && row.ref).toBeNull();
    expect(row?.kind === 'event' && row.runId).toBeNull();
  });
});

describe('the poll', () => {
  it('asks for the page the plan specifies, at the interval it specifies', () => {
    expect(STREAM_POLL_MS).toBe(5000);
    // One page is the largest the API will serve — `MAX_LIMIT = 200` in
    // kew/src/kew/service/audit.py, which clamps anything bigger — so "Load
    // older" is the only way past it rather than a bigger `?limit=`.
    expect(STREAM_LIMIT).toBe(200);
  });

  it('settles a burst of windows well inside one poll of the log', () => {
    // The brush can change the query far faster than a person can mean to (a
    // held arrow key repeats about thirty times a second), and every change is
    // two requests. A tenth of a second coalesces a burst without reading as
    // lag, and it is nowhere near the 5s the live poll already costs.
    expect(STREAM_SETTLE_MS).toBeGreaterThan(1000 / 30);
    expect(STREAM_SETTLE_MS).toBeLessThan(STREAM_POLL_MS / 10);
  });

  it('draws the 2×24 marks the artboard drew', () => {
    expect(STRIP.markWidth).toBe(2);
    expect(STRIP.markHeight).toBe(24);
    expect(DMONITOR).toContain('width: 2px; height: 24px');
  });
});

describe('EventStream.svelte', () => {
  const html = (props: Record<string, unknown>) =>
    render(EventStream, { props }).body.replace(/&#183;|&middot;/g, '·');

  it('draws the marks, the gap and the burst at the artboard’s positions', () => {
    const body = html({ source: audit, nowMs: NOW });
    expect(body).toContain('left: 89.89%');
    expect(body).toContain('left: 11%');
    expect(body).toContain('22m 35s quiet');
    expect(body).toContain('3 events in 8s');
    expect(body).toContain('budget refusal');
  });

  it('prints the empty window’s sentence over the list it is not clipped to', () => {
    // #2282 D15. The page's events are 90 minutes behind a 30-minute window:
    // the charts have nothing to draw, the list below still has rows, and the
    // sentence between them is what tells the two apart. It sits above the
    // filters, with the charts whose emptiness it explains.
    const body = html({ source: page([[90, 'dispatch_started']]), nowMs: NOW });
    expect(body).toContain('No events in this window · last event 14:16');
    expect(body.indexOf('No events in this window')).toBeLessThan(
      body.indexOf('aria-label="Filters"')
    );
    expect(body).toContain('14:16:58');
  });

  it('colours through tokens, never a hex a theme cannot switch', () => {
    const body = html({ source: audit, nowMs: NOW });
    expect(body).toContain('background-color: var(--warn)');
    expect(body).toContain('background-color: var(--success)');
    expect(body).not.toMatch(/#[0-9a-f]{6}/i);
  });

  it('offers the payload on an expand, closed until it is asked for (#2210)', () => {
    const payload: AuditList = {
      events: [
        {
          id: 9,
          timestamp: '2026-09-24T15:46:58+00:00',
          event: 'dispatch_refused',
          issue_num: 2055,
          item_id: null,
          run_id: null,
          actor_type: 'policy',
          actor_id: 'budget',
          summary: '{"reason":"budget_exceeded","scope":"weekly"}',
          epic_ref: null
        }
      ],
      next_after: null
    };
    const body = html({ source: payload, nowMs: NOW });
    // The line, in words — the label leading, the recorded words after it
    // (#2282 D15) — and the payload not printed as the row's text.
    expect(body).toContain('>Dispatch refused</span>');
    // The value worded, its code the title (#2896 R5).
    // The separator leads with a no-break space, which the browser keeps at
    // the start of the clipped box (#3468 D33).
    expect(body).toContain('title="budget_exceeded">\u00a0· #2055 · budget exceeded</span>');
    expect(body).not.toContain('{"reason"');
    // A control that says what it is, and starts shut: the JSON is one click
    // away, not thirty rows of it down the screen.
    expect(body).toContain('aria-expanded="false"');
    expect(body).toContain('<button');
  });

  it('gives a row with nothing more to show no expand at all', () => {
    // The artboard's page is prose summaries: every fact is already on the
    // line, so a control promising more would show the row again. Asserted on
    // the expand's own hooks: the range bar and the filter row are buttons too.
    const body = html({ source: audit, nowMs: NOW });
    expect(body).not.toContain('aria-expanded');
    expect(body).not.toContain('stream-payload-');
  });

  it('shows the notice, and no list, when the read failed', () => {
    const body = html({ source: unreadable('stream', new Error('boom')), nowMs: NOW });
    expect(body).toContain('stream unavailable');
    expect(body).not.toContain('<ol');
  });

  it('says so when the log is readable and empty', () => {
    const body = html({ source: { events: [], next_after: null }, nowMs: NOW });
    expect(body).toContain(STREAM_EMPTY);
    expect(body).not.toContain('<ol');
  });
});

/**
 * #2205 — the strip and the list read one clock.
 *
 * The screen shipped with its rows at 13:33–14:26 under an axis reading
 * 16:35–17:05 and an epic header saying `last tick 16:26:16`: three correct
 * readings of one moment in three zones. Nothing in a UTC container can catch
 * that — local *is* UTC there and every assertion in this file would pass with
 * the axis in one zone and the rows in another — so these tests move the
 * process into a zone that is not UTC and compare the two readings to each
 * other rather than to a constant.
 */
describe('one zone', () => {
  /**
   * `body` run with the process in `zone`. Node resolves `process.env.TZ` per
   * `Date`/`Intl` call, and `$lib/time` deliberately builds its formatter per
   * call, so a zone set here reaches the formatter — a memoized one would
   * answer with whichever zone ran first.
   */
  function inZone<T>(zone: string, body: () => T): T {
    const before = process.env.TZ;
    process.env.TZ = zone;
    try {
      return body();
    } finally {
      if (before === undefined) delete process.env.TZ;
      else process.env.TZ = before;
    }
  }

  /** "17:46:58" / "17:20" -> seconds since midnight, for an ordering compare. */
  function secondsOfDay(clock: string): number {
    const [h, m, s = '0'] = clock.split(':');
    return Number(h) * 3600 + Number(m) * 60 + Number(s);
  }

  const eventTimes = (reading: ReturnType<typeof composeStream>): string[] =>
    reading.available
      ? reading.rows.flatMap((row) => (row.kind === 'event' ? [row.time] : []))
      : [];

  const majorLabels = (reading: ReturnType<typeof composeStream>): string[] =>
    reading.available
      ? reading.strip.ticks.flatMap((tick) => (tick.label ? [tick.label] : []))
      : [];

  it('renders both the axis and the rows in the viewer zone', () => {
    // The artboard's own page, read from Berlin: 15:20–15:50 UTC is 17:20–17:50
    // there, and the newest event moves with it.
    const berlin = inZone('Europe/Berlin', () => composeStream(audit, NOW));
    expect(majorLabels(berlin)).toEqual(['17:20', '17:25', '17:30', '17:35', '17:40', '17:45', '17:50']);
    expect(eventTimes(berlin)[0]).toBe('17:46:58');

    // The same page from New York, where the offset has the other sign.
    const newYork = inZone('America/New_York', () => composeStream(audit, NOW));
    expect(majorLabels(newYork)).toEqual(['11:20', '11:25', '11:30', '11:35', '11:40', '11:45', '11:50']);
    expect(eventTimes(newYork)[0]).toBe('11:46:58');
  });

  it('puts every row time inside the axis range it is drawn under', () => {
    inZone('Europe/Berlin', () => {
      const reading = composeStream(audit, NOW);
      const labels = majorLabels(reading);
      const first = secondsOfDay(labels[0]);
      const last = secondsOfDay(labels[labels.length - 1]);
      const times = eventTimes(reading);
      expect(times).toHaveLength(5);
      for (const time of times) {
        expect(secondsOfDay(time)).toBeGreaterThanOrEqual(first);
        expect(secondsOfDay(time)).toBeLessThanOrEqual(last);
      }
    });
  });

  it('keeps the axis over an event stamped ahead of the viewer clock', () => {
    // Server/viewer clock skew: the newest event is 5 minutes past `nowMs`,
    // i.e. past the 5-minute mark the window would otherwise end on. It is
    // listed either way, so the axis has to reach it — an event in the list
    // with no place on the strip above it is the same disagreement in miniature.
    inZone('Europe/Berlin', () => {
      const skewed = page([[-5, 'dispatch_started'], [4, 'run_finished']]);
      const reading = composeStream(skewed, NOW);
      const labels = majorLabels(reading);
      const times = eventTimes(reading);
      expect(times[0]).toBe('17:51:58');
      expect(labels[labels.length - 1]).toBe('17:55');
      expect(secondsOfDay(times[0])).toBeLessThanOrEqual(
        secondsOfDay(labels[labels.length - 1])
      );
      expect(reading.available && reading.strip.marks).toHaveLength(2);
    });
  });

  it('renders the same strings it composed, each time titled with its UTC instant', () => {
    inZone('Europe/Berlin', () => {
      const reading = composeStream(audit, NOW);
      const body = render(EventStream, { props: { source: audit, nowMs: NOW } }).body;
      for (const label of majorLabels(reading)) expect(body).toContain(`>${label}</span>`);
      for (const time of eventTimes(reading)) expect(body).toContain(`>${time}</span>`);
      // The local reading above, the instant it is, underneath.
      expect(body).toContain('title="2026-09-24 15:46:58 UTC"');
      expect(body).toContain('>17:46:58</span>');
    });
  });

  it('says nothing in a title it could not read, and no time either', () => {
    const broken: AuditList = {
      events: [event({ id: 7, timestamp: 'not-a-date' })],
      next_after: null
    };
    const reading = composeStream(broken, NOW);
    const row = reading.available ? reading.rows[0] : null;
    expect(row?.kind === 'event' && row.time).toBe('—');
    expect(row?.kind === 'event' && row.title).toBe('');
    // The event is still listed: an unreadable stamp must not delete it.
    expect(reading.available && reading.rows).toHaveLength(1);
  });
});
