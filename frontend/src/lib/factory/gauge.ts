/**
 * FUI-06 — the pure geometry and labelling behind `Gauge.svelte`.
 *
 * A gauge is an instrument, not a progress bar. Its fill clamps at the cap so
 * the layout holds, and that clamp is where a bar starts to lie: $600 against
 * a $250 cap would paint exactly like sitting at cap. So the state here keeps
 * `capBarState`'s `over` and `multiplier`, and every clamped thing — the fill,
 * a pinned marker — still carries its true value in its label.
 *
 * The axis furniture is measured off the DMonitor readouts: a tick every 10%,
 * a taller tick plus a label at 0 / 50 / 100 ("$0 · $125 · cap $250"), and an
 * optional marker such as "prior cap $600", which sits at 40% of a $1,500 cap.
 */
import { capBarState, type CapBarState } from '$lib/cap-bar';

/** How a label sits on its tick: flush inside at the ends, centred between. */
export type LabelAlign = 'start' | 'center' | 'end';

export type GaugeTick = {
  /** Position along the track, 0–100. */
  pct: number;
  /** 0, 50 and 100: the taller tick, and the only ticks that carry a label. */
  major: boolean;
  /** Axis label, `null` on minor ticks. */
  label: string | null;
  align: LabelAlign;
};

export type GaugeMarker = {
  /** Position along the track, clamped to 0–100. */
  pct: number;
  /** True when the marked value fell outside 0–cap, so the pin is clamped. */
  clamped: boolean;
  /** The caller's label plus the true value, e.g. "prior cap $600". */
  label: string;
};

/** A value to pin on the track beside the fill, e.g. a superseded cap. */
export type GaugeValueMarker = { value: number; label: string };

export type GaugeOptions = {
  marker?: GaugeValueMarker | null;
  /** Money formatter for the axis and the marker; defaults to `gaugeMoney`. */
  fmt?: (n: number) => string;
};

export type GaugeState = CapBarState & {
  /** False without a usable cap: no track, no ticks, no marker — just a word. */
  track: boolean;
  ticks: GaugeTick[];
  marker: GaugeMarker | null;
  /**
   * The right-hand reading: "71.2% of cap", "×7.2 over cap", or "no cap". A
   * multiplier of 100 or more reads "×99+ over cap" (#2896): the exact one is
   * unbounded — "×1000000.0 over cap" at a 0.001 cap — and left its box.
   */
  caption: string;
  /** `caption` with the exact multiplier, for a `title` and nothing drawn. */
  exact: string;
  /**
   * The same reading for half a phone (#2896 R12): "71.2% cap", "×7.2 over",
   * "no cap". A 390px Monitor cell is ~163px wide, and "THIS WEEK" beside
   * "31.5% of cap" did not fit on one line. A multiplier of 100 or more reads
   * "×99+ over"; `exact` keeps the exact one.
   */
  short: string;
};

export type GaugeGeometry = {
  /** Height of the whole gauge row, px. */
  height: number;
  trackTop: number;
  trackHeight: number;
  /** A dashed hairline instead of a filled track: the "no data yet" scale. */
  dashed: boolean;
  tickTop: number;
  minorTick: number;
  majorTick: number;
  labelTop: number;
  /** The cap pin at the right edge of the track. */
  capMarker: { width: number; height: number; top: number };
  markerTop: number;
  markerHeight: number;
  /** The over-cap caption's row, above the track. */
  markerLabelTop: number;
  /**
   * The one line a marker's label takes under the axis labels ("prior cap
   * $600"). The gauge grows by it only when it carries a marker (#2953): drawn
   * above the track, the label was a third money label beside the axis ends,
   * and clamped to 100% it painted over the over-cap caption on the same row.
   */
  captionLine: number;
};

/**
 * The DMonitor Today and This week gauges, measurement for measurement.
 *
 * `markerLabelTop` is negative: the over-cap caption sits above the row, so a
 * caller has to leave headroom. DMonitor gives its marked This week gauge
 * `margin-top: 26px` where the unmarked Today gauge gets 8px. A marker's own
 * label is a caption under the axis, `captionLine` tall.
 */
export const GAUGE: GaugeGeometry = {
  height: 40,
  trackTop: 4,
  trackHeight: 4,
  dashed: false,
  tickTop: 10,
  minorTick: 4,
  majorTick: 8,
  labelTop: 22,
  capMarker: { width: 2, height: 16, top: -2 },
  markerTop: -6,
  markerHeight: 16,
  markerLabelTop: -24,
  captionLine: 16
};

/** The compact variant: the same axis over a dashed hairline, in 28px. */
export const GAUGE_COMPACT: GaugeGeometry = {
  height: 28,
  trackTop: 4,
  trackHeight: 1,
  dashed: true,
  tickTop: 7,
  minorTick: 3,
  majorTick: 6,
  labelTop: 15,
  capMarker: { width: 2, height: 12, top: 0 },
  markerTop: -4,
  markerHeight: 12,
  markerLabelTop: -18,
  captionLine: 16
};

/** Axis money: `$0`, `$125`, `$1,500`, and cents only when there are any. */
export function gaugeMoney(n: number): string {
  const digits = Number.isInteger(n) ? 0 : 2;
  return (
    '$' +
    n.toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits })
  );
}

/** A tick every 10%, labelled at 0 / 50 / 100. Empty without a usable cap. */
export function gaugeTicks(
  cap: number | null | undefined,
  fmt: (n: number) => string = gaugeMoney
): GaugeTick[] {
  if (cap == null || !(cap > 0)) {
    return [];
  }
  const ticks: GaugeTick[] = [];
  for (let pct = 0; pct <= 100; pct += 10) {
    const major = pct === 0 || pct === 50 || pct === 100;
    ticks.push({
      pct,
      major,
      label: !major ? null : pct === 100 ? `cap ${fmt(cap)}` : fmt((cap * pct) / 100),
      align: pct === 0 ? 'start' : pct === 100 ? 'end' : 'center'
    });
  }
  return ticks;
}

/**
 * The axis prints in IBM Plex Mono at 11px, which advances every glyph by
 * 0.6em — 6.6px. A monospaced label's width is therefore its length, which is
 * what lets `gaugeLabelBoxes` measure the axis without a browser.
 */
export const AXIS_CHAR_PX = 6.6;

/** A printed axis label's horizontal extent across the track, in px. */
export type LabelBox = { label: string; left: number; right: number };

/**
 * Where each labelled tick's words actually sit across a `width`px track, with
 * the same alignment `Gauge.svelte` draws them by: flush in at the ends,
 * centred on the tick between.
 */
export function gaugeLabelBoxes(
  ticks: readonly GaugeTick[],
  width: number,
  charPx: number = AXIS_CHAR_PX
): LabelBox[] {
  const boxes: LabelBox[] = [];
  for (const tick of ticks) {
    if (tick.label == null) continue;
    const span = tick.label.length * charPx;
    const at = (width * tick.pct) / 100;
    const left = tick.align === 'start' ? at : tick.align === 'end' ? at - span : at - span / 2;
    boxes.push({ label: tick.label, left, right: left + span });
  }
  return boxes;
}

/**
 * True when every axis label clears the one beside it by a space or more.
 *
 * A space, not nothing: the operator browser check at 390px read "$600 cap
 * $1,200" and "$2,000cap $4,000" off the two money readouts, and the first of
 * those had 2px between its boxes — two readings a reader has to take apart,
 * which is the same failure as the overlap in the second (#2282 D25). Which is
 * why `Gauge.svelte` prints the middle label only from `sm` up: the floor and
 * the cap are the reading that cannot be dropped, and this is the measurement
 * that says where the third one fits.
 */
export function gaugeLabelsFit(
  ticks: readonly GaugeTick[],
  width: number,
  charPx: number = AXIS_CHAR_PX
): boolean {
  const boxes = gaugeLabelBoxes(ticks, width, charPx).sort((a, b) => a.left - b.left);
  return boxes.every((box, i) => i === 0 || boxes[i - 1].right + charPx <= box.left);
}

/**
 * Where a value pins on the track, clamped to 0–100, with `clamped` set when
 * the clamp moved it. `null` when there is no value or no usable cap.
 */
export function markerPct(
  value: number | null | undefined,
  cap: number | null | undefined
): { pct: number; clamped: boolean } | null {
  if (value == null || cap == null || !(cap > 0)) {
    return null;
  }
  return {
    pct: Math.max(0, Math.min(100, (value / cap) * 100)),
    clamped: value < 0 || value > cap
  };
}

/**
 * The multiplier as a box can hold it: one decimal below 100, and "99+" from
 * there up, so `×1000000.0 over` cannot leave its cell at 390. The exact
 * reading stays in `exact`, which is only ever a title.
 */
function shortMultiplier(multiplier: number): string {
  return multiplier >= 100 ? '99+' : multiplier.toFixed(1);
}

/** Everything `Gauge.svelte` draws for one spend-against-cap reading. */
export function gaugeState(
  spent: number,
  cap: number | null | undefined,
  opts: GaugeOptions = {}
): GaugeState {
  const fmt = opts.fmt ?? gaugeMoney;
  const state = capBarState(spent, cap);
  const marker = opts.marker ?? null;
  const pin = markerPct(marker?.value, cap);
  return {
    ...state,
    track: state.pct != null,
    ticks: gaugeTicks(cap, fmt),
    marker: pin && marker ? { ...pin, label: `${marker.label} ${fmt(marker.value)}` } : null,
    caption:
      state.pct == null
        ? 'no cap'
        : state.over && state.multiplier != null
          ? `×${shortMultiplier(state.multiplier)} over cap`
          : `${state.pct.toFixed(1)}% of cap`,
    exact:
      state.pct == null
        ? 'no cap'
        : state.over && state.multiplier != null
          ? // One decimal, as CapBar already writes it.
            `×${state.multiplier.toFixed(1)} over cap`
          : `${state.pct.toFixed(1)}% of cap`,
    short:
      state.pct == null
        ? 'no cap'
        : state.over && state.multiplier != null
          ? `×${shortMultiplier(state.multiplier)} over`
          : `${state.pct.toFixed(1)}% cap`
  };
}
