/**
 * #2248 — the overview track: the histogram read into bars, and what the track
 * renders for them.
 *
 * Two things are pinned. The reading — a bar per bucket, placed by the bucket's
 * own instant, stacked by family — and the render, which must keep an empty
 * bucket's place on the track without drawing a bar in it. A quiet stretch that
 * closed up would put every bar after it at the wrong time, and a quiet stretch
 * drawn as a one-pixel bar would say the factory did something.
 *
 * The family bands are deliberately NOT the strip's colours: the histogram
 * counts event names, and a name cannot say whether the check it records passed.
 * `overviewFamily` is held to that here.
 */
import { render } from 'svelte/server';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AuditHistogram } from '$lib/api';
import histogramJson from '../../../fixtures/factory/audit-histogram.json';
import OverviewTrack from './OverviewTrack.svelte';
import {
  FAMILY_COLOR,
  OVERVIEW_FAMILIES,
  overviewBars,
  overviewFamily,
  spanLabel,
  type Span
} from './brush';
import { HOUR_MS, MINUTE_MS } from './stream-query';

const START = Date.parse('2026-10-02T09:00:00.000Z');
const RANGE: Span = { startMs: START, endMs: START + HOUR_MS };

/** Four 15-minute buckets over the range, counts as given. */
const histogram = (counts: Record<string, number>[]): AuditHistogram => ({
  since: new Date(START).toISOString(),
  until: new Date(START + HOUR_MS).toISOString(),
  bucket_s: 900,
  buckets: counts.map((c, i) => ({
    start: new Date(START + i * 15 * MINUTE_MS).toISOString(),
    counts: c
  })),
  total: counts.reduce((sum, c) => sum + Object.values(c).reduce((a, b) => a + b, 0), 0)
});

describe('overviewFamily', () => {
  it('reads money, a request for a person, and review/evidence by name', () => {
    expect(overviewFamily('budget_exhausted')).toBe('money');
    expect(overviewFamily('spend_recorded')).toBe('money');
    expect(overviewFamily('runner_usage')).toBe('money');
    expect(overviewFamily('approval_hold')).toBe('needs-you');
    expect(overviewFamily('needs_human_raised')).toBe('needs-you');
    expect(overviewFamily('adversarial_review_verdict')).toBe('review');
    expect(overviewFamily('publication_output_scan')).toBe('review');
    expect(overviewFamily('sandbox_resolved')).toBe('review');
  });

  it('leaves a name it does not know neutral, never painted', () => {
    expect(overviewFamily('epic_node_status')).toBe('other');
    expect(overviewFamily('dispatch_completed')).toBe('other');
    expect(overviewFamily('something_shipped_after_this_was_written')).toBe('other');
  });

  it('spends no green: a name cannot say a check passed', () => {
    // The strip's green is a verdict read out of a payload (`stream.ts`). The
    // histogram has no payload, so its review band is cyan — a topic.
    expect(Object.values(FAMILY_COLOR)).not.toContain('var(--success)');
    expect(FAMILY_COLOR.review).toBe('var(--accent-2)');
  });
});

describe('overviewBars', () => {
  const reading = overviewBars(
    histogram([{ dispatch_completed: 3 }, {}, { budget_exhausted: 1, review_outcome: 3 }, {}]),
    RANGE
  );

  it('is one bar per bucket, in the order the histogram gave them', () => {
    expect(reading.bars).toHaveLength(4);
    expect(reading.bars.map((bar) => bar.total)).toEqual([3, 0, 4, 0]);
    expect(reading.total).toBe(7);
    expect(reading.peak).toBe(4);
  });

  it('places each bar by its own instant, one bucket wide', () => {
    expect(reading.bars.map((bar) => bar.leftPct)).toEqual([0, 25, 50, 75]);
    expect(reading.bars.every((bar) => bar.widthPct === 25)).toBe(true);
  });

  it('stacks a bucket by family, bottom to top, dropping the empty bands', () => {
    expect(reading.bars[2].bands).toEqual([
      { family: 'review', count: 3 },
      { family: 'money', count: 1 }
    ]);
    expect(reading.bars[1].bands).toEqual([]);
  });

  it('heights a bar against the tallest bucket', () => {
    expect(reading.bars.map((bar) => bar.heightPct)).toEqual([75, 0, 100, 0]);
  });

  it('reads an empty range without dividing by its peak', () => {
    const quiet = overviewBars(histogram([{}, {}]), RANGE);
    expect(quiet.peak).toBe(0);
    expect(quiet.bars.every((bar) => bar.heightPct === 0)).toBe(true);
  });

  it('drops a bucket whose start will not parse, rather than guessing at it', () => {
    const broken = histogram([{ dispatch_completed: 1 }]);
    broken.buckets.push({ start: 'the day before', counts: { dispatch_completed: 9 } });
    expect(overviewBars(broken, RANGE).bars).toHaveLength(1);
  });

  it('places a response for a window the range has moved past where it belongs', () => {
    // The clock moved between the histogram read and this frame: the bars are
    // drawn at their own instants, which puts them left of the range's start.
    const moved = overviewBars(histogram([{ dispatch_completed: 1 }]), {
      startMs: START + 30 * MINUTE_MS,
      endMs: START + 90 * MINUTE_MS
    });
    expect(moved.bars[0].leftPct).toBe(-50);
  });

  it('reads the committed histogram fixture', () => {
    // An empty bucket and a counted one normalise to one JSON module type with
    // optional keys, which `Record<string, number>` rightly refuses; on the wire
    // the key is simply absent. Same narrowing `fixtures.test.ts` does.
    const tallied = (counts: Record<string, number | undefined>) => {
      const out: Record<string, number> = {};
      for (const [name, count] of Object.entries(counts)) if (count !== undefined) out[name] = count;
      return out;
    };
    const fixture: AuditHistogram = {
      ...histogramJson,
      buckets: histogramJson.buckets.map((bucket) => ({
        ...bucket,
        counts: tallied(bucket.counts)
      }))
    };
    const read = overviewBars(fixture, {
      startMs: Date.parse(fixture.since),
      endMs: Date.parse(fixture.until)
    });
    expect(read.bars).toHaveLength(fixture.buckets.length);
    expect(read.total).toBe(fixture.total);
    // Every family the legend offers is drawn somewhere, so the operator's
    // visual check exercises all four colours.
    const drawn = new Set(read.bars.flatMap((bar) => bar.bands.map((band) => band.family)));
    expect([...OVERVIEW_FAMILIES].every((family) => drawn.has(family))).toBe(true);
  });
});

describe('spanLabel', () => {
  it('reads two clock times for a window inside today', () => {
    expect(spanLabel({ startMs: START, endMs: START + 90 * MINUTE_MS }, ' – ', START)).toBe('09:00 – 10:30');
  });

  it('says "to" where the reading is spoken rather than drawn', () => {
    expect(spanLabel({ startMs: START, endMs: START + HOUR_MS }, ' to ', START)).toBe('09:00 to 10:00');
  });

  it('names the day where the two edges fall on different ones', () => {
    // A 24-hour range read "15:46 – 15:46" before this: both edges correct, and
    // the label saying the range held nothing.
    expect(spanLabel({ startMs: START, endMs: START + 24 * HOUR_MS })).toBe(
      '10-02 09:00 – 10-03 09:00'
    );
  });

  it('names the day of a window inside a day that is not today', () => {
    // #3014: yesterday's window read as two clock times, which say today.
    const window = { startMs: START, endMs: START + 90 * MINUTE_MS };
    expect(spanLabel(window, ' – ', START + 24 * HOUR_MS)).toBe('10-02 09:00 – 10-02 10:30');
  });

  it('names the day of a window before midnight once the clock is past it', () => {
    const midnight = Date.parse('2026-10-03T00:00:00.000Z');
    const window = { startMs: midnight - HOUR_MS, endMs: midnight - MINUTE_MS };
    expect(spanLabel(window, ' – ', midnight - 30_000)).toBe('23:00 – 23:59');
    expect(spanLabel(window, ' – ', midnight + 5_000)).toBe('10-02 23:00 – 10-02 23:59');
  });
});

describe('the rendered track', () => {
  // The fixture's day is today, so its windows read as two clock times.
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(START);
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  const html = (counts: Record<string, number>[]) =>
    render(OverviewTrack, {
      props: {
        reading: overviewBars(histogram(counts), RANGE),
        range: RANGE,
        window: { startMs: START + 15 * MINUTE_MS, endMs: START + 30 * MINUTE_MS },
        bucketMs: 15 * MINUTE_MS,
        onwindow: () => {},
        onkey: () => {}
      }
    }).body;

  it('draws one bar per bucket, and no bar for an empty bucket', () => {
    const body = html([{ dispatch_completed: 3 }, {}, { budget_exhausted: 1 }, {}]);
    expect([...body.matchAll(/data-bucket=/g)]).toHaveLength(4);
    // One band element per family present, and nothing at all in the two
    // buckets that recorded nothing.
    expect([...body.matchAll(/data-family=/g)]).toHaveLength(2);
    expect(body).toContain(FAMILY_COLOR.money);
  });

  it('keeps an empty bucket at zero height rather than at a visible floor', () => {
    const body = html([{}, { dispatch_completed: 1 }]);
    expect(body).toContain('height: 0;');
    expect([...body.matchAll(/data-family=/g)]).toHaveLength(1);
  });

  it('carries the brushed window as the slider it is', () => {
    const body = html([{ dispatch_completed: 1 }]);
    expect(body).toContain('role="slider"');
    expect(body).toContain('aria-valuetext="09:15 to 09:30"');
    expect(body).toContain('L returns to live');
  });

  it('renders the track before the histogram has been read', () => {
    const body = render(OverviewTrack, {
      props: {
        reading: null,
        range: RANGE,
        window: RANGE,
        bucketMs: 15 * MINUTE_MS,
        onwindow: () => {},
        onkey: () => {}
      }
    }).body;
    expect(body).toContain('role="slider"');
    expect([...body.matchAll(/data-bucket=/g)]).toHaveLength(0);
  });
});
