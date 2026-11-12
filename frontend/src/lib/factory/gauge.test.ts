/**
 * FUI-06 — the gauge's geometry, ticks and labels.
 *
 * A gauge clamps its fill so the layout holds, which is exactly how a bar can
 * lie: $600 against a $250 cap paints the same full bar as sitting at cap. So
 * these tests pin the two things that keep it honest — `capBarState`'s `over`
 * and `multiplier` survive, and anything clamped (the fill, a pinned marker)
 * still names its true value — alongside the axis the DMonitor readouts paint.
 *
 * The last block renders the component server-side and reads the inline styles
 * back against the artboard's own numbers. That is the standing check on the
 * pixels until FUI-02's screenshot harness can shoot the two gauges.
 */
import { readFileSync } from 'node:fs';
import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import Gauge from '../ui/Gauge.svelte';
import {
  AXIS_CHAR_PX,
  GAUGE,
  GAUGE_COMPACT,
  gaugeLabelBoxes,
  gaugeLabelsFit,
  gaugeMoney,
  gaugeState,
  gaugeTicks,
  markerPct
} from './gauge';

const DMONITOR = readFileSync(
  new URL('../../../design/factory-ui/DMonitor.html', import.meta.url),
  'utf8'
);

describe('gaugeMoney', () => {
  it('writes whole dollars without cents and groups thousands', () => {
    expect(gaugeMoney(0)).toBe('$0');
    expect(gaugeMoney(125)).toBe('$125');
    expect(gaugeMoney(1500)).toBe('$1,500');
  });

  it('keeps cents when the amount has any', () => {
    expect(gaugeMoney(125.5)).toBe('$125.50');
    expect(gaugeMoney(178.05)).toBe('$178.05');
  });
});

describe('gaugeTicks', () => {
  it('puts a tick every 10% and a major tick at 0, 50 and 100', () => {
    const ticks = gaugeTicks(250);
    expect(ticks.map((t) => t.pct)).toEqual([0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100]);
    expect(ticks.filter((t) => t.major).map((t) => t.pct)).toEqual([0, 50, 100]);
  });

  it('labels only the major ticks, and names the cap at the right edge', () => {
    const ticks = gaugeTicks(250);
    expect(ticks.filter((t) => t.label).map((t) => t.label)).toEqual(['$0', '$125', 'cap $250']);
    expect(ticks.filter((t) => !t.major).every((t) => t.label === null)).toBe(true);
  });

  it('keeps the end labels inside the track and centres the middle one', () => {
    const aligns = new Map(gaugeTicks(250).map((t) => [t.pct, t.align]));
    expect(aligns.get(0)).toBe('start');
    expect(aligns.get(50)).toBe('center');
    expect(aligns.get(100)).toBe('end');
  });

  it('matches the DMonitor This week axis', () => {
    // The artboard paints "$0 · $750 · cap $1,500" under a $1,500 weekly cap.
    const labels = gaugeTicks(1500)
      .filter((t) => t.label)
      .map((t) => t.label as string);
    expect(labels).toEqual(['$0', '$750', 'cap $1,500']);
    for (const label of labels) {
      expect(DMONITOR).toContain(`>${label}</span>`);
    }
  });

  it('has no ticks to draw without a usable cap', () => {
    for (const cap of [null, undefined, 0, -5]) {
      expect(gaugeTicks(cap)).toEqual([]);
    }
  });

  it("labels through the caller's formatter", () => {
    const labels = gaugeTicks(20, (n) => `${n}k`)
      .filter((t) => t.label)
      .map((t) => t.label);
    expect(labels).toEqual(['0k', '10k', 'cap 20k']);
  });
});

describe('markerPct', () => {
  it('places a value in range and reports it unclamped', () => {
    // DMonitor pins "prior cap $600" at 40% of the $1,500 weekly cap.
    expect(markerPct(600, 1500)).toEqual({ pct: 40, clamped: false });
  });

  it('clamps a value above the cap to the right edge', () => {
    expect(markerPct(600, 250)).toEqual({ pct: 100, clamped: true });
  });

  it('clamps a negative value to the left edge', () => {
    expect(markerPct(-10, 250)).toEqual({ pct: 0, clamped: true });
  });

  it('has nowhere to pin without a value or a usable cap', () => {
    expect(markerPct(null, 250)).toBeNull();
    expect(markerPct(undefined, 250)).toBeNull();
    expect(markerPct(600, null)).toBeNull();
    expect(markerPct(600, 0)).toBeNull();
  });
});

describe('gaugeState', () => {
  it('reads the DMonitor Today gauge', () => {
    const state = gaugeState(178.05, 250);
    expect(state.track).toBe(true);
    expect(state.pct).toBeCloseTo(71.22, 2);
    expect(state.over).toBe(false);
    expect(state.caption).toBe('71.2% of cap');
    expect(DMONITOR).toContain('71.2% of cap');
  });

  it('has a short reading that fits half a phone, for every caption it states', () => {
    // #2896 R12: "THIS WEEK" beside "31.5% of cap" broke over two lines at 390.
    expect(gaugeState(472.5, 1500).short).toBe('31.5% cap');
    expect(gaugeState(3075, 250).caption).toBe('×12.3 over cap');
    expect(gaugeState(3075, 250).short).toBe('×12.3 over');
    expect(gaugeState(10, null).short).toBe('no cap');
  });

  it('reads the DMonitor This week gauge, prior-cap marker included', () => {
    const state = gaugeState(915.05, 1500, { marker: { value: 600, label: 'prior cap' } });
    expect(state.caption).toBe('61.0% of cap');
    expect(state.marker).toEqual({ pct: 40, clamped: false, label: 'prior cap $600' });
    expect(DMONITOR).toContain('prior cap $600');
  });

  it('renders "no cap" with no track when there is no cap', () => {
    // Same reading as CapBar: no usable cap means no bar at all, not an empty one.
    for (const cap of [null, undefined, 0]) {
      const state = gaugeState(5, cap, { marker: { value: 600, label: 'prior cap' } });
      expect(state.track).toBe(false);
      expect(state.pct).toBeNull();
      expect(state.ticks).toEqual([]);
      expect(state.marker).toBeNull();
      expect(state.caption).toBe('no cap');
    }
  });

  it('clamps the fill over cap but still says how far over', () => {
    // loop #307: $14.42 against a $2.00 cap. The wording matches CapBar's.
    const state = gaugeState(14.42, 2);
    expect(state.pct).toBe(100);
    expect(state.over).toBe(true);
    expect(state.multiplier).toBeCloseTo(7.21, 2);
    expect(state.caption).toBe('×7.2 over cap');
  });

  it('bounds every drawn multiplier at ×99+ and keeps the exact one for a title', () => {
    // At agentic 1000 against a 0.001 cap the reading was "×1000000.0 over
    // cap", wider than its box on the Monitor track and in a 390px cell.
    const huge = gaugeState(1000, 0.001);
    expect(huge.short).toBe('×99+ over');
    expect(huge.caption).toBe('×99+ over cap');
    expect(huge.exact).toBe('×1000000.0 over cap');
    expect(gaugeState(1000, 1e-12).caption).toBe('×99+ over cap');
    expect(gaugeState(99.9, 1).short).toBe('×99.9 over');
    expect(gaugeState(99.9, 1).caption).toBe('×99.9 over cap');
    expect(gaugeState(100, 1).short).toBe('×99+ over');
    expect(gaugeState(100, 1).caption).toBe('×99+ over cap');
  });

  it('gives the exact reading as the caption wherever the caption is not bounded', () => {
    expect(gaugeState(178.05, 250).exact).toBe('71.2% of cap');
    expect(gaugeState(3075, 250).exact).toBe('×12.3 over cap');
    expect(gaugeState(10, null).exact).toBe('no cap');
  });

  it("keeps a clamped marker's true value in its label", () => {
    // A $600 prior cap against today's $250 cap pins at the edge; the label
    // must not round it down to the cap it is being compared against.
    const state = gaugeState(178.05, 250, { marker: { value: 600, label: 'prior cap' } });
    expect(state.marker).toEqual({ pct: 100, clamped: true, label: 'prior cap $600' });
  });

  it("formats the marker label with the caller's formatter", () => {
    const state = gaugeState(5, 20, {
      fmt: (n) => `${n}k`,
      marker: { value: 8, label: 'prior cap' }
    });
    expect(state.marker?.label).toBe('prior cap 8k');
    expect(state.ticks[10].label).toBe('cap 20k');
  });
});

describe('gauge geometry', () => {
  it('matches the DMonitor gauge, measurement for measurement', () => {
    expect(DMONITOR).toContain(
      `top: ${GAUGE.trackTop}px; height: ${GAUGE.trackHeight}px; background: #1d1730`
    );
    expect(DMONITOR).toContain(
      `right: 0; top: ${GAUGE.capMarker.top}px; width: ${GAUGE.capMarker.width}px;` +
        ` height: ${GAUGE.capMarker.height}px; background: #ffc94a`
    );
    expect(DMONITOR).toContain(`top: ${GAUGE.tickTop}px; width: 1px; height: ${GAUGE.majorTick}px`);
    expect(DMONITOR).toContain(`top: ${GAUGE.tickTop}px; width: 1px; height: ${GAUGE.minorTick}px`);
    expect(DMONITOR).toContain(
      `top: ${GAUGE.markerTop}px; width: 1px; height: ${GAUGE.markerHeight}px; background: #b5acc8`
    );
    expect(DMONITOR).toContain(`top: ${GAUGE.labelTop}px`);
    expect(DMONITOR).toContain(`top: ${GAUGE.markerLabelTop}px`);
  });

  it('labels its marker above the row, which the caller must leave room for', () => {
    expect(GAUGE.markerLabelTop).toBeLessThan(0);
    expect(GAUGE_COMPACT.markerLabelTop).toBeLessThan(0);
    // The artboard's marked This week gauge reserves that headroom; the
    // unmarked Today gauge above it needs only 8px.
    expect(DMONITOR).toContain(`height: ${GAUGE.height}px; margin-top: 26px`);
    expect(DMONITOR).toContain(`height: ${GAUGE.height}px; margin-top: 8px`);
  });

  it('gives the compact variant a dashed hairline in less vertical room', () => {
    expect(GAUGE.dashed).toBe(false);
    expect(GAUGE_COMPACT.dashed).toBe(true);
    expect(GAUGE_COMPACT.trackHeight).toBe(1);
    expect(GAUGE_COMPACT.height).toBeLessThan(GAUGE.height);
  });

  it('keeps every axis label clear of the ticks above it', () => {
    for (const g of [GAUGE, GAUGE_COMPACT]) {
      expect(g.tickTop + g.majorTick).toBeLessThan(g.labelTop);
      expect(g.labelTop).toBeLessThan(g.height);
    }
  });
});

describe('Gauge.svelte', () => {
  /** The rendered markup, whitespace between tags collapsed. */
  function draw(props: Record<string, unknown>): string {
    return render(Gauge, { props: { spent: 0, cap: null, ...props } })
      .body.replace(/<!--[\s\S]*?-->/g, '')
      .replace(/>\s+</g, '><');
  }

  it('draws the DMonitor Today gauge', () => {
    const html = draw({ spent: 178.05, cap: 250 });
    expect(html).toContain('style="height: 40px;"');
    expect(html).toContain('bg-ink-track" style="top: 4px; height: 4px;"');
    expect(html).toContain(
      'style="width: 71.22%; top: 4px; height: 4px; background-color: var(--warn);"'
    );
    // The 2×16 cap pin at the right edge.
    expect(html).toContain(
      'style="top: -2px; width: 2px; height: 16px; background-color: var(--warn);"'
    );
    expect(html).toContain(
      'style="left: 50%; top: 10px; height: 8px; background-color: var(--text-4);"'
    );
    expect(html).toContain(
      'style="left: 60%; top: 10px; height: 4px; background-color: var(--hair-strong);"'
    );
    expect(html).toContain('>cap $250</span>');
  });

  it('pins a prior cap on the track and names it in a caption under the axis', () => {
    // #2953: above the track the label was a third money label beside the
    // axis ends. Its tick stays at 40%; its words move to their own line.
    const html = draw({ spent: 915.05, cap: 1500, marker: { value: 600, label: 'prior cap' } });
    expect(html).toContain('bg-text-2" style="left: 40%; top: -6px; height: 16px;"');
    expect(html).toContain(`style="height: ${GAUGE.height + GAUGE.captionLine}px;"`);
    expect(html).toContain(
      `style="top: ${GAUGE.height}px; line-height: ${GAUGE.captionLine}px;">prior cap $600</span>`
    );
    expect(html).not.toContain('top: -24px');
  });

  it('keeps a clamped marker on the track edge, its label on the caption line', () => {
    const html = draw({ spent: 178.05, cap: 250, marker: { value: 600, label: 'prior cap' } });
    expect(html).toContain('bg-text-2" style="left: 100%; top: -6px; height: 16px;"');
    expect(html).toContain(`style="top: ${GAUGE.height}px;`);
    expect(html).toContain('>prior cap $600</span>');
  });

  it('gives the over-cap caption its row to itself when a prior cap is clamped (#2953)', () => {
    // cap 0.001, prior cap 600, spend 1000: the marker clamps to 100%, the
    // spot the over-cap caption is drawn at. They are now on different rows.
    const html = draw({ spent: 1000, cap: 0.001, marker: { value: 600, label: 'prior cap' } });
    const above = [...html.matchAll(/top: -24px[^>]*>([^<]*)<\/span>/g)].map((m) => m[1]);
    expect(above).toEqual(['×99+ over cap']);
    expect(html).toContain(`style="top: ${GAUGE.height}px;`);
  });

  it('grows only when there is a marker to name', () => {
    expect(draw({ spent: 915.05, cap: 1500 })).toContain(`style="height: ${GAUGE.height}px;"`);
    const compact = draw({
      spent: 0,
      cap: 250,
      dashed: true,
      marker: { value: 100, label: 'prior cap' }
    });
    expect(compact).toContain(
      `style="height: ${GAUGE_COMPACT.height + GAUGE_COMPACT.captionLine}px;"`
    );
  });

  it('renders the word alone, with no track, when there is no cap', () => {
    const html = draw({ spent: 5, cap: null });
    expect(html).toBe('<div class="font-mono text-[11px] text-text-3">no cap</div>');
  });

  it('turns the clamped fill to danger over cap and says how far over', () => {
    const html = draw({ spent: 14.42, cap: 2 });
    expect(html).toContain(
      'style="width: 100%; top: 4px; height: 4px; background-color: var(--danger);"'
    );
    expect(html).toContain(
      'text-danger" title="×7.2 over cap" style="top: -24px;">×7.2 over cap</span>'
    );
  });

  it('draws a far-over multiplier bounded, the exact one only as its title (#2896)', () => {
    const html = draw({ spent: 1000, cap: 0.001 });
    expect(html).toContain('title="×1000000.0 over cap" style="top: -24px;">×99+ over cap</span>');
    expect(html.match(/×1000000\.0/g)).toHaveLength(1);
  });

  it('draws the compact variant as a dashed hairline with no fill or cap pin', () => {
    const html = draw({ spent: 0, cap: 250, dashed: true });
    expect(html).toContain('style="height: 28px;"');
    expect(html).toContain('border-t border-dashed border-hair-strong" style="top: 4px;"');
    expect(html).not.toContain('var(--warn)');
    expect(html).toContain(
      'style="left: 0%; top: 7px; height: 6px; background-color: var(--text-4);"'
    );
    expect(html).toContain('>cap $250</span>');
  });
});

/**
 * #2282 (D25) — the axis at 390px.
 *
 * The operator browser check read "$600 cap $1,200" out of the Today gauge and
 * "$2,000cap $4,000" out of This week: in the Monitor's 2×2 phone grid the
 * halfway label prints over the cap's, which is the one label on the scale the
 * fill is measured against. The widths below are that screen's — a 390px body
 * inside a 16px gutter, split in two, less each cell's own padding.
 */
describe('the axis in a phone-width readout', () => {
  const THIS_WEEK_PX = 135; // 390 − 32 gutter, halved, less `pl-4 pr-7`.
  const TODAY_PX = 163; //   …and the flush-left cell, less `pr-4` only.

  /** The axis the component actually prints below `sm`: the two ends. */
  const ends = (cap: number) => gaugeTicks(cap).filter((t) => t.align !== 'center');

  it('measures a monospaced label by its length', () => {
    const [floor, mid, cap] = gaugeLabelBoxes(gaugeTicks(4000), 100);
    expect(floor).toEqual({ label: '$0', left: 0, right: 2 * AXIS_CHAR_PX });
    // Centred on its tick, and the cap's label flush inside the right edge.
    expect(mid.left).toBeCloseTo(50 - 3 * AXIS_CHAR_PX, 5);
    expect(cap.right).toBe(100);
    expect(cap.left).toBeCloseTo(100 - 10 * AXIS_CHAR_PX, 5);
  });

  it('cannot fit three money labels across either money readout', () => {
    // The two caps the operator's screen was reading when it found the clash.
    expect(gaugeLabelsFit(gaugeTicks(4000), THIS_WEEK_PX)).toBe(false);
    expect(gaugeLabelsFit(gaugeTicks(1200), TODAY_PX)).toBe(false);
  });

  it('counts two labels a space apart as not fitting, as the screen read them', () => {
    // "$600 cap $1,200" at 163px: 2px between the boxes, and two readings a
    // reader has to take apart is the same failure as an overlap.
    const boxes = gaugeLabelBoxes(gaugeTicks(1200), TODAY_PX);
    expect(boxes[2].left - boxes[1].right).toBeLessThan(AXIS_CHAR_PX);
    expect(boxes[2].left).toBeGreaterThan(boxes[1].right);
  });

  it('fits the floor and the cap, which are the reading that must survive', () => {
    expect(gaugeLabelsFit(ends(4000), THIS_WEEK_PX)).toBe(true);
    expect(gaugeLabelsFit(ends(1200), TODAY_PX)).toBe(true);
  });

  it('still fits all three at the desktop width the artboard draws', () => {
    // DMonitor's This week readout is ~380px wide at 1440.
    expect(gaugeLabelsFit(gaugeTicks(1500), 380)).toBe(true);
    // …and at the `sm` fold, where the 2×2 cell is ~264px.
    expect(gaugeLabelsFit(gaugeTicks(4000), 264)).toBe(true);
  });

  it('has nothing to collide with when there is no cap', () => {
    expect(gaugeLabelBoxes(gaugeTicks(null), THIS_WEEK_PX)).toEqual([]);
    expect(gaugeLabelsFit(gaugeTicks(null), THIS_WEEK_PX)).toBe(true);
  });

  it('fits what it actually prints below `sm` across the narrower readout', () => {
    // The markup and the measurement, joined: whatever the component does not
    // hide below `sm` has to fit the width it is drawn at.
    const printed = [
      ...render(Gauge, { props: { spent: 900, cap: 4000 } }).body.matchAll(
        /<span class="([^"]*)"[^>]*>([^<]*)<\/span>/g
      )
    ]
      .filter(([, cls]) => !cls.includes('hidden'))
      .map(([, , label]) => label);
    expect(printed).toEqual(['$0', 'cap $4,000']);
    const shown = gaugeTicks(4000).filter((t) => t.label != null && printed.includes(t.label));
    expect(gaugeLabelsFit(shown, THIS_WEEK_PX)).toBe(true);
  });

  it('prints both ends of the axis and never the middle, at every width', () => {
    // DESIGN.md Responsive: a spend gauge prints the two ends of its axis only.
    const html = render(Gauge, { props: { spent: 900, cap: 4000 } })
      .body.replace(/<!--[\s\S]*?-->/g, '')
      .replace(/>\s+</g, '><');
    expect(html).not.toContain('$2,000');
    expect(html).not.toContain('hidden');
    // The middle tick keeps its taller mark.
    expect(html).toContain('left: 50%; top: 10px; height: 8px;');
    expect(html).toContain('class="absolute whitespace-nowrap" style="left: 0%;');
    expect(html).toContain('>cap $4,000</span>');
  });
});
