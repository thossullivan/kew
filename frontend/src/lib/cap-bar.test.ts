import { describe, expect, it } from 'vitest';
import { capBarState } from './cap-bar';

describe('capBarState', () => {
  it('computes a clamped pct for an under-cap spend', () => {
    const s = capBarState(1, 2);
    expect(s.pct).toBe(50);
    expect(s.over).toBe(false);
    expect(s.multiplier).toBeNull();
  });

  it('reports 90% at the warn threshold without going over', () => {
    const s = capBarState(1.8, 2);
    expect(s.pct).toBeCloseTo(90);
    expect(s.over).toBe(false);
    expect(s.multiplier).toBeNull();
  });

  it('clamps pct to 100 and reports the multiplier when over cap', () => {
    // loop #307: $14.42 spent against a $2.00 cap → ×7.21 over.
    const s = capBarState(14.42, 2);
    expect(s.pct).toBe(100);
    expect(s.over).toBe(true);
    expect(s.multiplier).toBeCloseTo(7.21, 2);
  });

  it('treats spend equal to cap as at-cap, not over', () => {
    const s = capBarState(2, 2);
    expect(s.pct).toBe(100);
    expect(s.over).toBe(false);
    expect(s.multiplier).toBeNull();
  });

  it('returns a null pct when the cap is zero, null, or undefined', () => {
    for (const cap of [0, null, undefined]) {
      const s = capBarState(5, cap as number | null | undefined);
      expect(s.pct).toBeNull();
      expect(s.over).toBe(false);
      expect(s.multiplier).toBeNull();
    }
  });

  it('clamps negative spend to zero', () => {
    expect(capBarState(-3, 2).pct).toBe(0);
  });
});
