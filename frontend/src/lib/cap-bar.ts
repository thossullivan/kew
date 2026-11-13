// Pure spend-vs-cap math shared by the factory's `Gauge` (gauge.ts), the Loops
// readings (loops.ts) and their unit tests. The `CapBar` it was written for is
// gone (#3454).
//
// A flat 100% bar hides violations: loop #307 spending $14.42 against a $2.00
// cap looks identical to one sitting exactly at cap. `capBarState` keeps the
// fill clamped for layout but surfaces the true `over` / `multiplier` so the
// component can render a distinct violation state.

export type CapBarState = {
  /** Fill percentage, clamped to 0–100. `null` when there is no usable cap. */
  pct: number | null;
  /** True when spend has exceeded the cap. */
  over: boolean;
  /** spent / cap when over cap (e.g. 7.21), otherwise `null`. */
  multiplier: number | null;
};

export function capBarState(spent: number, cap: number | null | undefined): CapBarState {
  if (cap == null || !(cap > 0)) {
    return { pct: null, over: false, multiplier: null };
  }
  const ratio = spent / cap;
  const over = spent > cap;
  return {
    pct: Math.max(0, Math.min(100, ratio * 100)),
    over,
    multiplier: over ? ratio : null
  };
}
