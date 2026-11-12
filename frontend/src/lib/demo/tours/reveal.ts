/**
 * Bring a tour step's target into view inside the panel that scrolls it
 * (#3146), before driver.js highlights it.
 *
 * driver.js scrolls only when the target is outside the WINDOW. The console's
 * surfaces scroll inside their own panels, each shorter than the window (the
 * Monitor's column ends above the Build history row at the foot of the demo
 * shell), so a target inside the window can still sit under the panel's
 * bottom edge, cut off, with the popover talking about what cannot be seen.
 * Each scrolling ancestor, nearest first, is scrolled so the target sits at
 * the middle of what that panel shows: its padding box, cut down by the
 * window and by every panel around it. Bottom chrome that is a row of the
 * shell's column is outside the panel's box, so it is allowed for by
 * measuring the panel, not the window.
 *
 * Demo tours only: the console's own panels keep their scroll rules.
 */

/** A vertical span on screen, in client pixels. */
export interface Span {
  top: number;
  bottom: number;
}

/**
 * The `scrollTop` that puts `target` at the middle of `view`, for a panel now
 * at `scrollTop` that can scroll as far as `max`. A target already wholly in
 * view keeps the panel where it is; one taller than the view lines its top up
 * with the view's top, so its start is what shows.
 */
export function centredScrollTop(view: Span, target: Span, scrollTop: number, max: number): number {
  if (target.top >= view.top && target.bottom <= view.bottom) return scrollTop;
  const height = target.bottom - target.top;
  const room = view.bottom - view.top;
  const shift = height > room ? target.top - view.top : (target.top + target.bottom) / 2 - (view.top + view.bottom) / 2;
  return Math.round(Math.min(Math.max(scrollTop + shift, 0), Math.max(max, 0)));
}

/** Whether `el` scrolls vertically and has something to scroll. */
function scrolls(el: Element): boolean {
  const { overflowY } = getComputedStyle(el);
  return (overflowY === 'auto' || overflowY === 'scroll') && el.scrollHeight > el.clientHeight;
}

/** The part of `el`'s padding box the window and every clipping panel around it show. */
function shownSpan(el: Element): Span {
  const box = el.getBoundingClientRect();
  let top = Math.max(box.top + el.clientTop, 0);
  let bottom = Math.min(box.top + el.clientTop + el.clientHeight, window.innerHeight);
  for (let up = el.parentElement; up && up !== document.documentElement; up = up.parentElement) {
    if (getComputedStyle(up).overflowY === 'visible') continue;
    const outer = up.getBoundingClientRect();
    top = Math.max(top, outer.top + up.clientTop);
    bottom = Math.min(bottom, outer.top + up.clientTop + up.clientHeight);
  }
  return { top, bottom };
}

/** How many animation frames the target must hold still before it is revealed. */
const STILL_FRAMES = 6;

/**
 * Resolve once `target` has stayed put for a few frames, or after `ms`. A
 * surface draws in stages (the Monitor's sections arrive from separate reads),
 * so a target measured on its first frame can be pushed down under the panel's
 * foot a moment later.
 */
export function settled(target: Element, ms: number): Promise<void> {
  const read = () => {
    const box = target.getBoundingClientRect();
    return `${box.top},${box.height}`;
  };
  return new Promise((resolve) => {
    const until = performance.now() + ms;
    let last = read();
    let still = 0;
    const frame = () => {
      const now = read();
      still = now === last ? still + 1 : 0;
      last = now;
      if (still >= STILL_FRAMES || performance.now() >= until) resolve();
      else requestAnimationFrame(frame);
    };
    requestAnimationFrame(frame);
  });
}

/** Scroll every panel that scrolls `target`, nearest first, to show it whole (or its start). */
export function revealInPanels(target: Element): void {
  // A target with no box (display:none, inside a shut <details>, or swapped
  // out since it was measured) reads as 0,0,0,0, and centring that would
  // scroll the panel up by half its height for nothing.
  if (target.getClientRects().length === 0) return;
  const drawn = target.getBoundingClientRect();
  if (drawn.width === 0 && drawn.height === 0) return;
  for (let panel = target.parentElement; panel && panel !== document.documentElement; panel = panel.parentElement) {
    if (!scrolls(panel)) continue;
    const box = target.getBoundingClientRect();
    const next = centredScrollTop(
      shownSpan(panel),
      { top: box.top, bottom: box.bottom },
      panel.scrollTop,
      panel.scrollHeight - panel.clientHeight
    );
    if (next !== panel.scrollTop) panel.scrollTop = next;
  }
}
