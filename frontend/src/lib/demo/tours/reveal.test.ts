import { describe, expect, it } from 'vitest';
import { centredScrollTop } from './reveal';

// A panel showing client y 93..740 (the Monitor at 1024x768, above the 28px
// Build history row), scrolled to 0 and able to scroll 400px.
const VIEW = { top: 93, bottom: 740 };

describe('centredScrollTop', () => {
  it('leaves a panel alone when the target is already wholly in view', () => {
    expect(centredScrollTop(VIEW, { top: 300, bottom: 420 }, 0, 400)).toBe(0);
    expect(centredScrollTop(VIEW, { top: 93, bottom: 740 }, 50, 400)).toBe(50);
    // Off-centre but whole: centring would move it to 0, so only "leave it" keeps 50.
    expect(centredScrollTop(VIEW, { top: 100, bottom: 200 }, 50, 400)).toBe(50);
  });

  it('centres a target cut off by the panel foot in what the panel shows', () => {
    // Target 627..793: its middle 710 moves to the view's middle 416.5.
    expect(centredScrollTop(VIEW, { top: 627, bottom: 793 }, 0, 400)).toBe(294);
  });

  it('centres a target above the view by scrolling back, never past the top', () => {
    expect(centredScrollTop(VIEW, { top: 0, bottom: 100 }, 400, 400)).toBe(34);
    expect(centredScrollTop(VIEW, { top: 0, bottom: 100 }, 20, 400)).toBe(0);
  });

  it('stops at the furthest the panel can scroll', () => {
    expect(centredScrollTop(VIEW, { top: 900, bottom: 1000 }, 0, 100)).toBe(100);
  });

  it('lines up the top of a target taller than the view with the top of the view', () => {
    expect(centredScrollTop(VIEW, { top: 600, bottom: 1400 }, 10, 2000)).toBe(517);
  });
});
