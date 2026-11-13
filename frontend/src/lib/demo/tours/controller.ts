/**
 * The demo tours' one driver (#3145): opens a tour at a step, moves it, and
 * ends it — on Escape, on the close press, on Done, when the window narrows
 * below `lg`, or when the visitor navigates somewhere the step is not.
 *
 * driver.js draws the overlay and the popover; this module decides what is in
 * the popover and where the tour is. A step on another route is reached by
 * navigating first, to the bare route, and then waiting for its `data-tour`
 * target. Every move writes the deep link (`deep-link.ts`) into the address
 * bar and the state, merged into what the page holds, and ending takes it out
 * again; it is never left in `page.url` (see `arrive`).
 *
 * Desktop only: a tour never opens below 1024px, and one that is open when the
 * window narrows past it ends.
 */
import { driver, type Driver, type PopoverDOM } from 'driver.js';
import 'driver.js/dist/driver.css';
import { goto, replaceState } from '$app/navigation';
import { page } from '$app/state';
import { TOURS } from './bundle';
import { carriesTourLink, readTourLink, writeTourLink, type TourLink } from './deep-link';
import { revealInPanels, settled } from './reveal';
import type { BundledStep, BundledTour, Segment } from './types';
import './tours.css';

/**
 * Where tours run: `lg` and wider (DESIGN.md, Responsive Behavior). In `rem`,
 * as Tailwind's `lg` is, so the Tours entry (`lg:flex`) and the tours agree
 * at any default font size.
 */
export const DESKTOP = '(min-width: 64rem)';

/** How long a step waits for its target to draw before it shows anyway. */
const TARGET_WAIT_MS = 5_000;

/** The longest a step waits for its drawn target to stop moving before it is scrolled to. */
const SETTLE_MS = 1_500;

export { TOURS };

/**
 * The selector of the element a step highlights: its `data-tour` target, or,
 * for a step that names a `card`, that node's card inside the target (the
 * epic graph draws each card as `.svelte-flow__node[data-id="<node>"]`).
 */
export function stepSelector(step: Pick<BundledStep, 'target' | 'card'>): string {
  // `target` and `card` are checked at build time to be plain ids.
  const target = `[data-tour="${step.target}"]`;
  return step.card === undefined ? target : `${target} .svelte-flow__node[data-id="${step.card}"]`;
}

/** The element a step highlights; null until its page draws it. */
export function targetOf(step: BundledStep): Element | null {
  return document.querySelector(stepSelector(step));
}

/** The epic graph's own Fit all control (`DagView.svelte`), which a `fit` step presses. */
const FIT_ALL = '[data-tour="epic-graph"] button[aria-label="Fit view"]';

/** The epic graph's node popover's own close (`NodePopover.svelte`), which a card step presses. */
const NODE_POPOVER_CLOSE = '[role="dialog"].pop button.close';

function desktop(): boolean {
  return window.matchMedia(DESKTOP).matches;
}

let drv: Driver | null = null;
let current: { tour: BundledTour; index: number } | null = null;
/** Where focus goes when the tour ends. */
let opener: HTMLElement | null = null;
/** Bumped by every move and by ending: a move that finds it changed was overtaken. */
let generation = 0;
/**
 * How many of the tour's own navigations are in flight. A count, not a flag:
 * SvelteKit drops a navigation a newer one overtakes and resolves it at once,
 * so a flag the first cleared would leave the second unguarded (two quick
 * Next presses across routes).
 */
let moving = 0;

/**
 * Watches the highlighted target's size. driver.js places the highlight and
 * the popover once, when a step shows; a target that grows under it (Build
 * history opened from inside the highlight) would leave the popover over the
 * very toggle that closes it, on a short laptop. A resize re-places both.
 */
let resizes: ResizeObserver | null = null;
/**
 * True from `drive()` until driver.js has finished moving to the step. A
 * re-place asked for while it moves is lost: the move ends by placing the
 * popover from what it measured when it began. So it waits for the end.
 */
let highlighting = false;
let replaceAfterHighlight = false;

function replace(): void {
  if (highlighting) replaceAfterHighlight = true;
  else refresh();
}

function highlighted(): void {
  highlighting = false;
  if (!replaceAfterHighlight) {
    keepClear();
    return;
  }
  replaceAfterHighlight = false;
  refresh();
}

/** driver.js re-places the highlight and the popover; then the popover is moved off what it explains. */
function refresh(): void {
  drv?.refresh();
  keepClear();
}

/** A box on screen, in client pixels. */
type Rect = { left: number; top: number; right: number; bottom: number };

/** The room kept round what a popover must not cover: driver.js's stage padding and the outline. */
const CLEAR_PAD = 12;
/** How far a moved popover keeps off the window's edge, and off what it explains. */
const EDGE = 8;
const GAP = 12;
/** A target this close to the window's edge has its outline drawn inside it (D5). */
const OUTLINE_ROOM = 7;

const grown = (r: DOMRect, by: number): Rect => ({
  left: r.left - by,
  top: r.top - by,
  right: r.right + by,
  bottom: r.bottom + by
});

/** The area two boxes share; 0 when they do not overlap. */
function shared(a: Rect, b: Rect): number {
  const w = Math.min(a.right, b.right) - Math.max(a.left, b.left);
  const h = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
  return w > 0 && h > 0 ? w * h : 0;
}

/**
 * Where a `width` x `height` popover goes so that it covers none of `avoid`
 * inside a `vw` x `vh` window: beside the boxes' union on the right, the left,
 * below or above, each at three alignments, pulled inside the window. The
 * first spot that covers nothing wins; with none, the spot that covers least.
 * Pure, so the arithmetic is tested without a browser.
 */
export function clearSpot(
  avoid: readonly Rect[],
  width: number,
  height: number,
  vw: number,
  vh: number
): { left: number; top: number; covered: number } {
  const u = {
    left: Math.min(...avoid.map((r) => r.left)),
    top: Math.min(...avoid.map((r) => r.top)),
    right: Math.max(...avoid.map((r) => r.right)),
    bottom: Math.max(...avoid.map((r) => r.bottom))
  };
  const ys = [u.top, (u.top + u.bottom) / 2 - height / 2, u.bottom - height];
  const xs = [u.left, (u.left + u.right) / 2 - width / 2, u.right - width];
  const spots = [
    ...ys.map((top) => ({ left: u.right + GAP, top })),
    ...ys.map((top) => ({ left: u.left - GAP - width, top })),
    ...xs.map((left) => ({ left, top: u.bottom + GAP })),
    ...xs.map((left) => ({ left, top: u.top - GAP - height }))
  ];
  let best = { left: EDGE, top: EDGE, covered: Infinity };
  for (const spot of spots) {
    const left = Math.round(Math.min(Math.max(spot.left, EDGE), Math.max(EDGE, vw - width - EDGE)));
    const top = Math.round(Math.min(Math.max(spot.top, EDGE), Math.max(EDGE, vh - height - EDGE)));
    const box = { left, top, right: left + width, bottom: top + height };
    const covered = avoid.reduce((sum, r) => sum + shared(box, r), 0);
    if (covered < best.covered) best = { left, top, covered };
    if (covered === 0) break;
  }
  return best;
}

/**
 * Keep the open step's popover off what it explains (D4, D9): the highlighted
 * target and, when the target sits inside a dialog of the page (the epic
 * graph's node popover), that whole dialog. driver.js places the popover on a
 * side with room for it, and with no such side lays it over the target; a
 * popover that covers either is moved to the nearest spot that covers
 * neither, its arrow dropped, since it no longer sits on one side. A target
 * that touches the window's edge has its outline drawn inside it (D5), or the
 * outline is drawn off screen.
 */
function keepClear(): void {
  for (const el of document.querySelectorAll('.kew-tour-inset')) el.classList.remove('kew-tour-inset');
  const step = current?.tour.steps[current.index];
  const wrapper = document.querySelector<HTMLElement>('.driver-popover.kew-tour');
  const target = step ? targetOf(step) : null;
  if (!target || !wrapper || !target.isConnected) return;
  const box = target.getBoundingClientRect();
  if (box.width === 0 && box.height === 0) return;
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  if (box.left < OUTLINE_ROOM || box.top < OUTLINE_ROOM || box.right > vw - OUTLINE_ROOM || box.bottom > vh - OUTLINE_ROOM) {
    target.classList.add('kew-tour-inset');
  }
  const avoid: Rect[] = [grown(box, CLEAR_PAD)];
  const dialog = target.parentElement?.closest('[role="dialog"]');
  if (dialog && !dialog.classList.contains('driver-popover')) avoid.push(grown(dialog.getBoundingClientRect(), GAP));
  // The graph card the step's `?node=` names, when the target is not that card
  // (a step that reads the node's popover): the card it is about stays seen.
  const named = step?.query?.node;
  const card = named ? document.querySelector(`[data-tour="epic-graph"] .svelte-flow__node[data-id="${named}"]`) : null;
  if (card && card !== target) avoid.push(grown(card.getBoundingClientRect(), CLEAR_PAD));
  // The region the step's sentence compares across (c9 review): kept clear
  // where the window has room for it, never at the cost of the target.
  const region = step?.clear ? document.querySelector(`[data-tour="${step.clear}"]`) : null;
  const all = region ? [...avoid, grown(region.getBoundingClientRect(), 0)] : avoid;
  const pop = wrapper.getBoundingClientRect();
  if (all.every((r) => shared(pop, r) === 0)) return;
  let spot = clearSpot(all, pop.width, pop.height, vw, vh);
  if (spot.covered > 0 && all !== avoid) {
    if (avoid.every((r) => shared(pop, r) === 0)) return;
    spot = clearSpot(avoid, pop.width, pop.height, vw, vh);
  }
  wrapper.style.left = `${spot.left}px`;
  wrapper.style.top = `${spot.top}px`;
  wrapper.style.right = 'auto';
  wrapper.style.bottom = 'auto';
  wrapper.querySelector('.driver-popover-arrow')?.classList.add('driver-popover-arrow-none');
}

/** driver.js re-places on a window resize or scroll, a frame later; the popover is kept clear after it. */
let clearFrame: number | null = null;
function keepClearSoon(): void {
  if (clearFrame !== null) cancelAnimationFrame(clearFrame);
  clearFrame = requestAnimationFrame(() => {
    clearFrame = requestAnimationFrame(() => {
      clearFrame = null;
      keepClear();
    });
  });
}
let watchingWindow = false;
function watchWindow(on: boolean): void {
  if (on === watchingWindow) return;
  watchingWindow = on;
  if (on) {
    window.addEventListener('resize', keepClearSoon);
    window.addEventListener('scroll', keepClearSoon, true);
    return;
  }
  window.removeEventListener('resize', keepClearSoon);
  window.removeEventListener('scroll', keepClearSoon, true);
  if (clearFrame !== null) cancelAnimationFrame(clearFrame);
  clearFrame = null;
}

type Size = { width: number; height: number };

/** A re-place waiting for the next frame: one per frame, however many reports. */
let pendingFrame: number | null = null;

/** The border box an observer entry reports, or the target's own when it has none. */
function sizeOf(entry: ResizeObserverEntry): Size {
  const box = entry.borderBoxSize?.[0];
  if (box) return { width: box.inlineSize, height: box.blockSize };
  const rect = entry.target.getBoundingClientRect();
  return { width: rect.width, height: rect.height };
}

/** More than a pixel in either dimension: what a re-place answers, not a rounding wobble. */
const moved = (a: Size, b: Size) => Math.abs(a.width - b.width) > 1 || Math.abs(a.height - b.height) > 1;

/**
 * Re-place the step when `target`'s border box changes by more than 1px from
 * the size it was last placed at, at most once per animation frame. A refresh
 * can change layout and report the target again; the same size is no change,
 * so it cannot loop. `null` stops watching and drops a frame still pending.
 */
function followSize(target: Element | null): void {
  resizes?.disconnect();
  resizes = null;
  if (pendingFrame !== null) cancelAnimationFrame(pendingFrame);
  pendingFrame = null;
  replaceAfterHighlight = false;
  if (!target || typeof ResizeObserver === 'undefined') return;
  // The first report is the size the step was placed at.
  let placed: Size | null = null;
  let latest: Size | null = null;
  resizes = new ResizeObserver((entries) => {
    const entry = entries.at(-1);
    // An unmounted target reports 0x0; re-placing onto it moves the step to
    // the window's corner. Nothing re-places a target no longer in the page.
    if (!entry || !entry.target.isConnected) return;
    latest = sizeOf(entry);
    if (placed === null) {
      placed = latest;
      return;
    }
    if (pendingFrame !== null || !moved(latest, placed)) return;
    pendingFrame = requestAnimationFrame(() => {
      pendingFrame = null;
      placed = latest;
      replace();
    });
  });
  resizes.observe(target);
}

/** The open tour and its 1-based step, or null. */
export function activeTour(): TourLink | null {
  return current && drv ? { tour: current.tour.id, step: current.index + 1 } : null;
}

/** Wait until `selector` is drawn, or `ms` passes. */
function waitFor(selector: string, ms: number): Promise<void> {
  if (document.querySelector(selector)) return Promise.resolve();
  return new Promise((resolve) => {
    const done = () => {
      observer.disconnect();
      window.clearTimeout(timer);
      resolve();
    };
    const observer = new MutationObserver(() => {
      if (document.querySelector(selector)) done();
    });
    const timer = window.setTimeout(done, ms);
    observer.observe(document.body, { childList: true, subtree: true, attributes: true });
  });
}

/**
 * Press the epic graph's own Fit all (D3), so the step opens on the whole
 * graph: the page's first frame of a graph too big to read whole at 100% is a
 * crop centred on one node, and the card a step names can be off it.
 */
async function fitGraph(ms: number): Promise<void> {
  await waitFor(FIT_ALL, ms);
  document.querySelector<HTMLButtonElement>(FIT_ALL)?.click();
}

/** Write `link` (or no tour) into the address bar, merged into the page's own. */
function writeLink(link: TourLink | null): void {
  const written = writeTourLink(page.state, new URL(window.location.href), link);
  try {
    replaceState(written.url, written.state);
  } catch {
    // The router is not up yet (dev only); the next move writes it.
  }
}

/** A passage paragraph as DOM: text nodes, `<code>` and `<strong>`. Never HTML. */
function paragraph(runs: Segment[]): HTMLParagraphElement {
  const p = document.createElement('p');
  for (const run of runs) {
    if (!run.code && !run.strong) {
      p.append(run.text);
      continue;
    }
    const el = document.createElement(run.code ? 'code' : 'strong');
    el.textContent = run.text;
    p.append(el);
  }
  return p;
}

/** Fill the popover with the step's words and its docs link. */
function render(popover: PopoverDOM, step: BundledStep): void {
  popover.title.textContent = step.title;
  popover.title.style.display = 'block';
  const link = document.createElement('a');
  link.href = step.href;
  link.target = '_blank';
  link.rel = 'noopener noreferrer';
  link.className = 'kew-tour-docs';
  link.textContent = 'Read it in the docs ↗';
  popover.description.replaceChildren(...step.body.map(paragraph), link);
  popover.description.style.display = 'block';
  popover.wrapper.dataset.tourStep = step.passage;
  // driver.js focuses the popover's first focusable element right after this
  // hook returns, which is the close press: its ring read as a second
  // highlighted target (design review c9 D16). The step's forward move takes
  // the focus instead, once driver.js is done; Tab still reaches the rest.
  const next = popover.nextButton;
  queueMicrotask(() => {
    // No scroll: the step has already placed its target and the popover.
    if (next.isConnected) next.focus({ preventScroll: true });
  });
}

function makeDriver(tour: BundledTour): Driver {
  return driver({
    steps: tour.steps.map((step) => ({
      element: () => targetOf(step) as Element,
      // A card a step points at is there to be read: pressed, it would open
      // the node's popover outside the highlight, under the overlay, where
      // nothing in it can be pressed without ending the tour.
      disableActiveInteraction: step.card !== undefined,
      popover: { onPopoverRender: (popover) => render(popover, step) }
    })),
    animate: !window.matchMedia('(prefers-reduced-motion: reduce)').matches,
    allowClose: true,
    overlayOpacity: 0.6,
    stagePadding: 6,
    stageRadius: 4,
    popoverClass: 'kew-tour',
    showProgress: true,
    progressText: '{{current}} of {{total}} · Esc ends the tour',
    nextBtnText: 'Next',
    prevBtnText: 'Back',
    doneBtnText: 'Done',
    closeBtnLabel: 'End the tour',
    onNextClick: () => {
      if (!current) return;
      if (current.index + 1 >= current.tour.steps.length) endTour();
      else void show(current.tour, current.index + 1);
    },
    onPrevClick: () => {
      if (current && current.index > 0) void show(current.tour, current.index - 1);
    },
    // Escape, the close press and an overlay click all come here first.
    onDestroyStarted: () => endTour(),
    // The move to a step has ended: a resize it held back re-places now.
    onHighlighted: () => highlighted()
  });
}

/**
 * The query keys every step owns, whether its query names them or not: the
 * epic page's replay (`mode`, `t`, `speed`) and canvas (`view`, `select`),
 * the one page that reads them. A step sets the ones it names and takes the
 * rest out, so a replay step opens its moment on the graph its passage reads,
 * with no selection dimming cards, whatever view the visitor left; and a step
 * that names no replay opens the page live, whatever replay the address held.
 * Every other key is the page's, and stays as the visitor has it.
 */
const STEP_KEYS = ['mode', 't', 'speed', 'view', 'select'] as const;

/** `url` with `step`'s query in it: its keys set, the other keys a step owns taken out. */
function applyQuery(url: URL, query: Record<string, string> | undefined): void {
  for (const key of STEP_KEYS) url.searchParams.delete(key);
  for (const [key, value] of Object.entries(query ?? {})) url.searchParams.set(key, value);
}

/**
 * True when the address bar already says what the step's query says: every
 * key it names at its value, and no other key a step owns.
 */
function holdsQuery(here: URL, query: Record<string, string> | undefined): boolean {
  const named = query ?? {};
  const keys = new Set<string>([...STEP_KEYS, ...Object.keys(named)]);
  return [...keys].every((key) => here.searchParams.get(key) === (named[key] ?? null));
}

/**
 * Get to `step`'s route, with its query, and no tour link in `page.url`,
 * which is what a page builds its next write from. Another route is a plain
 * navigation to it with the step's query. On this route, an address that
 * does not say what the step's query says (`STEP_KEYS`: a replay step's
 * `?mode=replay&t=`, #3147, or no replay at all) is navigated to, keeping
 * the page's other keys and taking any tour link out, so the page answers
 * the moment as it answers any link; and a `page.url` that carries a link (a
 * deep link, a reload, Back to a step) is navigated away from, to the same
 * address without it. The link is written into the
 * address bar and the state once the step shows.
 */
async function arrive(step: BundledStep): Promise<void> {
  const { route, query } = step;
  if (window.location.pathname !== route) {
    const there = new URL(route, window.location.href);
    applyQuery(there, query);
    await goto(there);
    return;
  }
  const here = new URL(window.location.href);
  const held = holdsQuery(here, query);
  if (held && !carriesTourLink(page.url)) return;
  const clean = writeTourLink(page.state, here, null);
  if (!held) applyQuery(clean.url, query);
  // A new moment arrives with no state, as a pasted link does: a page reads
  // its own state before `page.url`, and the state it left holds the moment
  // (and the view and selection) it is leaving.
  const state = held ? clean.state : {};
  await goto(clean.url, { replaceState: true, keepFocus: true, noScroll: true, state });
}

/** Show step `index` of `tour`: navigate to its route if need be, then highlight. */
async function show(tour: BundledTour, index: number): Promise<void> {
  const mine = ++generation;
  // Nothing re-places a step that is leaving: its page may unmount the target.
  followSize(null);
  const step = tour.steps[index];
  current = { tour, index };
  moving++;
  try {
    await arrive(step);
  } finally {
    moving--;
  }
  await waitFor(stepSelector(step), TARGET_WAIT_MS);
  if (mine !== generation) return;
  if (step.query?.node !== undefined && !targetOf(step)?.closest('[role="dialog"]')) {
    // A step's `?node=` selects the card it is about, so the rail reads that
    // node and the page frames that card at 100%; the node popover the link
    // also opens would cover the graph, so unless the step reads the popover
    // itself it is shut with its own ×, which keeps the selection (c9 review).
    const graph = document.querySelector('[data-tour="epic-graph"]');
    if (graph) await settled(graph, SETTLE_MS);
    if (mine !== generation) return;
    document.querySelector<HTMLButtonElement>(NODE_POPOVER_CLOSE)?.click();
  }
  if (step.fit) {
    // Once the graph's first frame has drawn: a press before it is undone by it.
    const graph = document.querySelector('[data-tour="epic-graph"]');
    if (graph) await settled(graph, SETTLE_MS);
    if (mine !== generation) return;
    await fitGraph(TARGET_WAIT_MS);
    if (mine !== generation) return;
  }
  const drawn = targetOf(step);
  if (drawn) await settled(drawn, SETTLE_MS);
  if (mine !== generation) return;
  if (!desktop()) {
    endTour();
    return;
  }
  const target = targetOf(step);
  if (!target) {
    // driver.js would show the step centred and say nothing: a tour whose
    // page dropped its target must be loud (the browser checks fail on it).
    console.error(
      `demo tour "${tour.id}" step ${index + 1}: no ${stepSelector(step)} on ${step.route} after ${TARGET_WAIT_MS / 1000}s`
    );
  } else {
    // driver.js scrolls only the window; the console's panels scroll themselves.
    revealInPanels(target);
  }
  drv ??= makeDriver(tour);
  highlighting = true;
  drv.drive(index);
  watchWindow(true);
  // With motion off, driver.js has placed the popover by now; with it on,
  // `onHighlighted` keeps it clear once the move ends.
  keepClear();
  followSize(target);
  writeLink({ tour: tour.id, step: index + 1 });
}

/**
 * Open `id` at 1-based `step`. `from` gets focus back at the end. Opens
 * nothing, and says so with `false`, for a tour this build does not have, a
 * step the tour does not have, or a window narrower than the tours run at.
 */
export function startTour(id: string, step = 1, from: HTMLElement | null = null): boolean {
  const tour = TOURS.find((t) => t.id === id);
  if (!tour || !desktop() || !Number.isInteger(step) || step < 1 || step > tour.steps.length) return false;
  if (drv) endTour();
  opener = from;
  void show(tour, step - 1);
  return true;
}

/**
 * End the open tour, if any: overlay gone, link out of the address bar, focus
 * back. A tour still on its way to a step (no popover yet) ends too, and its
 * link comes out all the same.
 */
export function endTour(): void {
  generation++;
  followSize(null);
  watchWindow(false);
  for (const el of document.querySelectorAll('.kew-tour-inset')) el.classList.remove('kew-tour-inset');
  highlighting = false;
  const d = drv;
  const back = opener;
  const open = d !== null || current !== null;
  drv = null;
  current = null;
  opener = null;
  if (!open) return;
  // Forced: no second trip through `onDestroyStarted`. driver.js refocuses
  // whatever had focus when the step opened, which is gone or hidden by now,
  // so the opener is focused after it returns.
  d?.destroy();
  writeLink(null);
  if (d && back?.isConnected) back.focus();
}

/**
 * A navigation finished. The tour's own moves are ignored. Any other reads
 * the tour link from `page.state`, then the ADDRESS BAR — not `page.url`,
 * which Back and Forward restore without the link a page's own write left in
 * the address — and makes the tour agree with it: a link opens its tour (with
 * `from` to take focus when it ends) or moves the open tour to the step it
 * names; no link ends the tour, including one still on its way to a step, and
 * takes a stale link out of the address, so a reload cannot reopen it. A link
 * no tour can open (a tour this build does not have, a step past its end, or
 * a window narrower than the tours run at) opens nothing and is taken out of
 * the address too (X6): left there, the page's next write carried it on.
 */
export function navigated(from: HTMLElement | null = null): void {
  if (moving > 0) return;
  const here = new URL(window.location.href);
  const link = readTourLink(page.state, here);
  const open = current ? { tour: current.tour.id, step: current.index + 1 } : null;
  if (!link) {
    if (open) endTour();
    else if (carriesTourLink(here)) writeLink(null);
    return;
  }
  if (!open) {
    if (!startTour(link.tour, link.step, from)) writeLink(null);
  } else if (link.tour !== open.tour) endTour();
  else if (link.step !== open.step && !startTour(link.tour, link.step, opener)) endTour();
}

/** End an open tour when the window narrows below `lg`. Returns the unsubscribe. */
export function endBelowDesktop(): () => void {
  const query = window.matchMedia(DESKTOP);
  const onChange = () => {
    if (!query.matches) endTour();
  };
  query.addEventListener('change', onChange);
  return () => query.removeEventListener('change', onChange);
}
