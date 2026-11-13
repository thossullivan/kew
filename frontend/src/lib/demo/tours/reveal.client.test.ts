/**
 * `revealInPanels` on a stand-in layout: jsdom draws nothing, so the panel's
 * and the target's boxes are set by hand, as the browser would report them.
 */
import { afterEach, describe, expect, it } from 'vitest';
import { revealInPanels } from './reveal';

/** A 647px panel at client y 93..740 that can scroll 600px, scrolled to 200. */
function panel(): HTMLElement {
  const el = document.createElement('div');
  el.style.overflowY = 'auto';
  let top = 200;
  Object.defineProperties(el, {
    scrollHeight: { value: 1247 },
    clientHeight: { value: 647 },
    clientTop: { value: 0 },
    scrollTop: { get: () => top, set: (v: number) => (top = v) }
  });
  el.getBoundingClientRect = () => DOMRect.fromRect({ x: 0, y: 93, width: 1024, height: 647 });
  document.body.append(el);
  return el;
}

/** A target inside `parent` whose box is `rect`, or that has no box at all. */
function target(parent: HTMLElement, rect: DOMRect | null): HTMLElement {
  const el = document.createElement('div');
  el.getBoundingClientRect = () => rect ?? DOMRect.fromRect({ x: 0, y: 0, width: 0, height: 0 });
  el.getClientRects = () => (rect ? ([rect] as unknown as DOMRectList) : ([] as unknown as DOMRectList));
  parent.append(el);
  return el;
}

afterEach(() => document.body.replaceChildren());

describe('revealInPanels', () => {
  it('scrolls a panel to centre a target cut off by its foot', () => {
    const scroller = panel();
    revealInPanels(target(scroller, DOMRect.fromRect({ x: 40, y: 627, width: 353, height: 166 })));
    expect(scroller.scrollTop).toBe(494);
  });

  it('leaves the panel where it is when the target has no box (display:none, a shut <details>, swapped out)', () => {
    const scroller = panel();
    revealInPanels(target(scroller, null));
    expect(scroller.scrollTop).toBe(200);
  });

  it('leaves the panel where it is when the target is drawn with no width and no height', () => {
    const scroller = panel();
    const empty = DOMRect.fromRect({ x: 0, y: 0, width: 0, height: 0 });
    const el = target(scroller, empty);
    el.getClientRects = () => [empty] as unknown as DOMRectList;
    revealInPanels(el);
    expect(scroller.scrollTop).toBe(200);
  });
});
