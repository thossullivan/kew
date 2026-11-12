/**
 * C3/#2301 — the outcome scorecard's readings, checked without a browser.
 *
 * The week below is the one `kew/tests/test_outcome_scorecard.py` hand-computes
 * on the server: 66 merged on main, 28 of them kew's, a 2.3 h median to READY.
 * What is pinned here is what the chart does with those numbers — the share,
 * the two segments of the bar, the band a pointer lands in, the greying of a
 * sample too small to read, and the sentence each chart prints when it has
 * nothing to draw.
 */
import { describe, expect, it } from 'vitest';
import type { OutcomeReport, OutcomeWeek } from '$lib/api';
import {
  VIEW_H,
  VIEW_W,
  bandX,
  barSegments,
  buildCharts,
  crosshairNote,
  hoursText,
  mergedShare,
  plotY,
  polylines,
  stepIndex,
  weekFooter,
  weekIndexAt,
  weekLabel
} from './outcomes';

function week(over: Partial<OutcomeWeek> = {}): OutcomeWeek {
  return {
    week_start: '2026-09-21',
    merged_main: 66,
    merged_kew_touched: 30,
    merged_kew_authored: 28,
    ready_items: 12,
    ready_hours: { p50: 2.3, p90: 6.1, max: 9.0 },
    rounds_to_ready: { p50: 1.5, max: 4 },
    cost_per_merged: { mean: 26.4, p50: 24.77 },
    spend_total: 740.2,
    spend_unattributed: 31.0,
    nodes_done: 24,
    pr_sync_observed_at: '2026-09-27T18:00:00+00:00',
    partial: false,
    ...over
  };
}

function report(weeks: OutcomeWeek[], over: Partial<OutcomeReport> = {}): OutcomeReport {
  return {
    weeks,
    min_sample: 3,
    merged_main_head: 'abc1234',
    merged_main_unavailable: null,
    ...over
  };
}

const TWO = report([
  week(),
  week({
    week_start: '2026-09-28',
    merged_main: 71,
    merged_kew_touched: 55,
    merged_kew_authored: 53,
    ready_items: 20,
    ready_hours: { p50: 2.4, p90: 7.0, max: 11.0 },
    cost_per_merged: { mean: 25.1, p50: 24.77 }
  })
]);

describe('readings', () => {
  it('prints the Monday of the week, in UTC', () => {
    expect(weekLabel('2026-09-21')).toBe('Sep 21');
    // A week start the server could not have produced is printed, not hidden.
    expect(weekLabel('not-a-date')).toBe('not-a-date');
  });

  it('reads a median under an hour in minutes', () => {
    expect(hoursText(2.3)).toBe('2.3 h');
    expect(hoursText(0.5)).toBe('30 m');
    expect(hoursText(null)).toBe('—');
  });

  it("computes kew's share off what landed on main", () => {
    expect(mergedShare(week())).toBeCloseTo((28 / 66) * 100, 6);
  });

  it('has no share to compute when the clone could not be read', () => {
    expect(mergedShare(week({ merged_main: null }))).toBeNull();
    expect(mergedShare(week({ merged_main: 0 }))).toBeNull();
  });
});

describe('the two-segment bar', () => {
  it('splits the week into kew-authored and everything else', () => {
    expect(barSegments(week())).toEqual({ authored: 28, other: 38, total: 66 });
  });

  it("is kew's own count alone when there is no denominator", () => {
    // Never a full bar off a missing denominator: that reads as "kew merged
    // all of it", which is the one thing the chart must not say.
    expect(barSegments(week({ merged_main: null }))).toEqual({
      authored: 28,
      other: 0,
      total: 28
    });
  });

  it('does not go negative when kew authored more than main shows', () => {
    // A merge on a branch main has not been fetched for yet: the ledger can
    // legitimately know about more than the clone does. Tolerated, never
    // repaired.
    expect(barSegments(week({ merged_main: 2, merged_kew_authored: 5 }))).toEqual({
      authored: 5,
      other: 0,
      total: 5
    });
  });
});

describe('geometry', () => {
  it('puts week i at the centre of band i', () => {
    expect(bandX(0, 8)).toBeCloseTo(VIEW_W / 16, 6);
    expect(bandX(7, 8)).toBeCloseTo(VIEW_W - VIEW_W / 16, 6);
  });

  it('maps the scale top to the top of the plot and zero to the floor', () => {
    expect(plotY(0, 10)).toBeGreaterThan(plotY(10, 10));
    expect(plotY(10, 10)).toBeCloseTo(7, 6);
    expect(plotY(0, 10)).toBeCloseTo(VIEW_H - 7, 6);
  });

  it('clamps a reading above the scale rather than drawing outside the box', () => {
    expect(plotY(99, 10)).toBeCloseTo(plotY(10, 10), 6);
  });

  it('breaks the line at a week with no reading instead of bridging it', () => {
    const charts = buildCharts(
      report([
        week(),
        week({
          week_start: '2026-09-28',
          ready_items: 0,
          ready_hours: { p50: null, p90: null, max: null }
        }),
        week({ week_start: '2026-10-05' })
      ])
    );
    const ready = charts.find((c) => c.key === 'ready');
    // Two readable weeks either side of a blank one: two subpaths, each of a
    // single point, so nothing is drawn across the gap.
    expect(polylines(ready!.points, ready!.max)).toEqual([]);
  });

  it('draws one subpath across consecutive readable weeks', () => {
    const ready = buildCharts(TWO).find((c) => c.key === 'ready');
    expect(polylines(ready!.points, ready!.max)).toHaveLength(1);
  });
});

describe('the crosshair', () => {
  it('gives the whole band to its week, so there is no dead space', () => {
    expect(weekIndexAt(0, 8)).toBe(0);
    expect(weekIndexAt(0.124, 8)).toBe(0);
    expect(weekIndexAt(0.126, 8)).toBe(1);
    expect(weekIndexAt(1, 8)).toBe(7);
    expect(weekIndexAt(-5, 8)).toBe(0);
  });

  it('has no band to light when there are no weeks', () => {
    expect(weekIndexAt(0.5, 0)).toBeNull();
    expect(stepIndex(null, 1, 0)).toBeNull();
  });

  it('steps from the newest week and stops at both ends', () => {
    expect(stepIndex(null, -1, 8)).toBe(6);
    expect(stepIndex(0, -1, 8)).toBe(0);
    expect(stepIndex(7, 1, 8)).toBe(7);
  });

  it('reads the hovered week, and the newest week with no hover', () => {
    const charts = buildCharts(TWO);
    const cost = charts.find((c) => c.key === 'cost')!;
    expect(crosshairNote(cost, 0)).toBe('Sep 21 · $24.77 · mean $26.40 · n=30 merged PRs');
    expect(crosshairNote(cost, null)).toContain('Sep 28');
  });

  it('reads the served tail beside the plotted median', () => {
    const charts = buildCharts(TWO);
    // p90 and max are served and p50 is plotted: a week whose median is fine
    // and whose worst case is a day is not a week that went well.
    expect(
      crosshairNote(
        charts.find((c) => c.key === 'ready')!,
        0
      )
    ).toContain('p90 6.1 h · worst 9.0 h');
    expect(
      crosshairNote(
        charts.find((c) => c.key === 'rounds')!,
        0
      )
    ).toContain('worst 4');
    // The share chart reads its own two counts, which is what the bar draws.
    expect(
      crosshairNote(
        charts.find((c) => c.key === 'merged')!,
        0
      )
    ).toContain('28 of 66 · 30 touched');
  });

  it('says the denominator is missing rather than printing a share of nothing', () => {
    const charts = buildCharts(report([week({ merged_main: null })]));
    expect(charts.find((c) => c.key === 'merged')!.points[0].detail).toBe(
      '28 authored · main not readable'
    );
  });

  it('says so when a week is too thin to read, and when it is unfinished', () => {
    const thin = report([week({ merged_kew_touched: 2, ready_items: 1, partial: true })]);
    const cost = buildCharts(thin).find((c) => c.key === 'cost')!;
    expect(cost.points[0].thin).toBe(true);
    expect(crosshairNote(cost, 0)).toContain('sample too small to read');
    expect(crosshairNote(cost, 0)).toContain('week in progress');
  });
});

describe('the four charts', () => {
  it('builds exactly the four, oldest week first', () => {
    const charts = buildCharts(TWO);
    expect(charts.map((c) => c.key)).toEqual(['merged', 'ready', 'rounds', 'cost']);
    expect(charts.every((c) => c.points.map((p) => p.week)[0] === '2026-09-21')).toBe(true);
  });

  it('is empty — with no charts at all — when there is no report', () => {
    expect(buildCharts(null)).toEqual([]);
  });

  it('only the money chart is money, so only it is amber', () => {
    expect(
      buildCharts(TWO)
        .filter((c) => c.money)
        .map((c) => c.key)
    ).toEqual(['cost']);
  });

  it('shares the x-axis: every chart has one point per week', () => {
    const charts = buildCharts(TWO);
    expect(new Set(charts.map((c) => c.points.length))).toEqual(new Set([2]));
  });

  it('scales the share chart to 100%, not to its own maximum', () => {
    // A share chart whose top is the best week makes 42% look like all of it.
    expect(buildCharts(TWO).find((c) => c.key === 'merged')!.max).toBe(100);
  });

  it('says why the share chart is empty when the clone could not be read', () => {
    const charts = buildCharts(
      report([week({ merged_main: null })], {
        merged_main_unavailable: 'origin/main could not be read from the local clone'
      })
    );
    expect(charts.find((c) => c.key === 'merged')!.empty).toBe(
      "kew's share of main cannot be drawn: origin/main could not be read from the local clone."
    );
  });

  it('says a quiet fortnight is a quiet fortnight, not a failure', () => {
    const quiet = report([
      week({
        merged_main: 0,
        merged_kew_touched: 0,
        merged_kew_authored: 0,
        ready_items: 0,
        ready_hours: { p50: null, p90: null, max: null },
        rounds_to_ready: { p50: null, max: null },
        cost_per_merged: { mean: null, p50: null }
      })
    ]);
    const charts = buildCharts(quiet);
    expect(charts.find((c) => c.key === 'merged')!.empty).toBe(
      'no PRs merged on main in the last 1 week.'
    );
    expect(charts.find((c) => c.key === 'ready')!.empty).toContain('nothing to time');
    expect(charts.find((c) => c.key === 'cost')!.empty).toContain('no merged PR carried');
    // Every empty chart says why. None of them is a blank box.
    expect(charts.every((c) => c.empty !== null && c.empty.length > 0)).toBe(true);
  });

  it('prints the whole bill beside the medians, including the part with no PR', () => {
    expect(weekFooter(TWO, 0)).toBe(
      // `gaugeMoney` prints cents only when there are any — the page's rule.
      'Sep 21 · $740.20 dispatched · $31 of it on runs with no PR · 24 nodes done · ' +
        'PR states as of 2026-09-27 18:00:00'
    );
    // No hover reads the newest week; no report reads nothing at all.
    expect(weekFooter(TWO, null)).toContain('Sep 28');
    expect(weekFooter(null, null)).toBe('');
  });

  it('says a week whose PR states were never observed was never observed', () => {
    const unobserved = report([week({ pr_sync_observed_at: null, nodes_done: 1 })]);
    expect(weekFooter(unobserved, 0)).toContain('no PR state observed this week');
    expect(weekFooter(unobserved, 0)).toContain('1 node done');
  });

  it('greys a thin week against the served threshold, not a hard-coded one', () => {
    const strict = report([week({ ready_items: 12 })], { min_sample: 20 });
    expect(buildCharts(strict).find((c) => c.key === 'ready')!.points[0].thin).toBe(true);
  });
});
