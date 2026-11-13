/**
 * #2305 AC2 — a canvas with one executing and one `in_review` node animates
 * ONLY the first, and `prefers-reduced-motion` switches all of it off.
 *
 * The console's motion used to point away from the work: the ready frontier
 * pulsed (Dagster pulses its queued items; kew decided not to), `in_review` —
 * a published pull request waiting for a PERSON — drew the live cyan, and the
 * two statuses where kew's own reviewer really is running, `adversarial_review`
 * and `remediating`, drew nothing at all.
 *
 * The cards are rendered one at a time, as `dag-view.test.ts` renders them:
 * Svelte Flow draws no nodes in a server render (its node loop is client-only),
 * so a `render(DagView)` body has no cards in it to ask about. The data each
 * card is handed is built here exactly as `DagView` builds it — through
 * `servedMotion`, which is the shared rule — and the WIRING in `DagView`
 * (which card follows the clock, and what arms it) is held to its own source
 * below.
 *
 * The CSS half of the reduced-motion rule is read off the component's own
 * `<style>` block: a server render carries no stylesheet, and jsdom evaluates
 * no media queries. The clock half — no timer at all for a reader who asked
 * for less motion — is `time/clock.client.test.ts`.
 */
import { readFileSync } from 'node:fs';
import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import type { EpicNode } from '$lib/api';
import { servedMotion } from '$lib/factory/motion';
import DagNode from './DagNode.svelte';

const nodeSource = readFileSync(new URL('./DagNode.svelte', import.meta.url), 'utf8');
const viewSource = readFileSync(new URL('./DagView.svelte', import.meta.url), 'utf8');

const NOW = Date.parse('2026-10-06T12:00:00+00:00');

const node = (id: string, status: string, over: Partial<EpicNode> = {}): EpicNode => ({
  id,
  title: `#${id}`,
  status,
  status_since: '2026-10-06T11:30:00+00:00',
  url: null,
  pr_url: null,
  external: false,
  stuck: null,
  attention: null,
  blocked_reason: null,
  ...over
});

/** One card, with the data `DagView` would hand it. */
const card = (n: EpicNode, frontier = false, now = NOW, live = true): string =>
  render(DagNode, {
    props: {
      data: {
        node: n,
        group: null,
        caption: '',
        drain: null,
        frontier,
        motion: servedMotion(n),
        live,
        dim: false,
        now,
        badges: [],
        zoomedOut: false
      }
    }
  }).body;

describe('only what kew is executing moves', () => {
  // The acceptance fixture: one node kew is executing, one awaiting a human.
  const executing = card(node('2305', 'in_progress'));
  const waiting = card(node('2296', 'in_review'));

  it('gives the halo and the running bar to the executing node alone', () => {
    expect(executing).toContain('class="halo');
    expect(executing).toMatch(/class="bar[^"]*"[^>]*width:/);

    expect(waiting).not.toContain('halo');
    expect(waiting).not.toContain('class="bar');
  });

  it('moves the two governed-review statuses too, and not `awaiting_publication`', () => {
    // The old reading had this exactly backwards on both counts.
    expect(card(node('1', 'adversarial_review'))).toContain('class="halo');
    expect(card(node('2', 'remediating'))).toContain('class="halo');
    expect(card(node('3', 'awaiting_publication'))).not.toContain('halo');
  });

  it('never pulses a queued item: the ready frontier is a still ring', () => {
    // Dagster pulses its queued rows. A node on the frontier has not started,
    // so it gets the halo's end state held still, in the quiet ink.
    const ready = card(node('2305', 'todo'), true);
    expect(ready).toContain('data-ready-ring');
    expect(ready).not.toContain('class="halo');
    expect(card(node('2305', 'queued'), false)).not.toContain('halo');
  });

  it("prefers the server's own motion over the status word", () => {
    // A node whose pending run is an orphan: the word says `in_progress` and
    // the server, which probed the owner, says otherwise.
    expect(card(node('2305', 'in_progress', { motion: 'waiting_kew' }))).not.toContain('halo');
  });
});

/** The body of `DagView`'s `frameNow` derivation, which is the one expression
 * that decides which second a frame is a picture of. */
const frameNowBody = (): string => {
  const at = viewSource.indexOf('const frameNow');
  expect(at, 'the canvas has no frameNow').toBeGreaterThan(-1);
  const end = viewSource.indexOf('});', at);
  expect(end, 'frameNow is not a derivation').toBeGreaterThan(at);
  return viewSource.slice(at, end);
};

describe('a replayed frame draws history still', () => {
  // Replay hands this canvas a tick out of the journal: the node WAS executing
  // at that instant and is not executing now. A breathing halo and a bar read
  // against today's wall clock would animate work that stopped days ago, which
  // is the same defect #2305 is about pointed at the past.
  const weekOld = node('2305', 'in_progress', { status_since: '2026-09-29T11:30:00+00:00' });

  it('gives a replayed executing node neither the halo nor the bar', () => {
    const replayed = card(weekOld, false, NOW, false);
    expect(replayed).not.toContain('halo');
    expect(replayed).not.toContain('class="bar');
    // The card still SAYS what it was doing — the mark and the status row are
    // history honestly drawn; only the motion is withheld.
    expect(replayed).toContain('in progress');
  });

  it('reads the frame against the tick’s own instant, not the wall clock', () => {
    // `tick_ts` is the only honest "now" for a replayed frame: against today's
    // clock a week-old tick ages every card to this morning, and the running
    // bar it would have drawn saturates. The rule itself is `frameClock` in
    // `./replay` and is held to its behaviour in `replay.test.ts`; what this
    // asks is that the canvas hands it the REPLAY fact and not the stream's —
    // the two used to be one flag, and a dropped live stream then read every
    // card against `tick_ts` (review r5).
    expect(viewSource).toContain('return frameClock(replay, snapshot.tick_ts, Date.now());');
    expect(frameNowBody()).not.toContain('streaming');
    // And the live/replay fact reaches the card, which is what gates its motion.
    expect(viewSource).toContain('live: animated,');
    expect(viewSource).toContain('const animated = $derived(streaming && !replay);');
    expect(nodeSource).toContain("const moving = $derived(data.motion === 'executing' && data.live);");
  });
});

describe('the bar grows, and the clock is what grows it', () => {
  it('is wider for a node that has been running longer', () => {
    const width = (since: string): number => {
      const html = card(node('2305', 'in_progress', { status_since: since }));
      const found = /class="bar[^"]*"[^>]*width:\s*([\d.]+)%/.exec(html);
      expect(found, 'the running bar carries no width').toBeTruthy();
      return Number(found![1]);
    };
    const minutesAgo = (n: number) => new Date(NOW - n * 60_000).toISOString();
    expect(width(minutesAgo(30))).toBeGreaterThan(width(minutesAgo(1)));
  });

  it('grows between two ticks of the clock, on one unchanged frame', () => {
    // What the clock is FOR: the same node, the same snapshot, a second later.
    const n = node('2305', 'in_progress');
    const widthAt = (now: number) =>
      Number(/width:\s*([\d.]+)%/.exec(card(n, false, now))![1]);
    expect(widthAt(NOW + 1000)).toBeGreaterThan(widthAt(NOW));
  });

  it('slides over the clock’s own tick rather than stepping', () => {
    // The tick is 1s (MOTION_TICK_MS) and so is the transition; anything else
    // and the bar jumps once a second instead of moving.
    expect(nodeSource).toMatch(/\.bar\s*\{[^}]*transition:\s*width 1s linear/);
  });

  it('is armed by what is on screen, and torn down with the canvas', () => {
    // Four rules in one expression: only while something MOVES (`movingFor` is
    // false for every card of a replayed or dropped frame, which is how the
    // clock stops with the stream), paused under the pointer, and stopped on
    // unmount.
    expect(viewSource).toContain('motionClock.arm(anyMoving && lit === null)');
    expect(viewSource).toContain("animated && motionFor(node) === 'executing'");
    expect(viewSource).toContain('return () => motionClock.arm(false);');
    // A card that is not moving is read against the frame, never the clock.
    expect(viewSource).toContain('motionClock.reading(movingFor(node), frameNow)');
  });

  it('pauses on a hover the canvas is still drawing, never on a stale id', () => {
    // `hovered` OUTLIVES the node it names: `onnodepointerleave` never fires
    // for a node a new frame removed from under the pointer, so one ordinary
    // interaction would leave the clock disarmed for the life of the page.
    // `lit` is the lineage of that id, and `lineage` answers null for an id
    // this model no longer draws.
    expect(viewSource).not.toContain('hovered === null)');
    expect(viewSource).toContain('const lit = $derived(hovered === null ? null : lineage(model, hovered));');
  });
});

/** Every rule in `DagNode.svelte` that animates something.
 *
 * `.card.quiet` is deliberately NOT one of them: `.quiet` changes the title's
 * ink and nothing else, and a transition on the compound selector would — being
 * the more specific one — replace `.card`'s 120ms hover and dim fade with a
 * 400ms one on every finished card. */
const ANIMATED_RULES = ['.bar', '.card', '.card.quiet .tt', '.halo'];

describe('prefers-reduced-motion switches all of it off', () => {
  /** The body of the component's reduced-motion block. */
  const reduced = (): string => {
    const at = nodeSource.indexOf('@media (prefers-reduced-motion: reduce)');
    expect(at, 'the component has no reduced-motion block').toBeGreaterThan(-1);
    return nodeSource.slice(at);
  };

  it('stops the halo, the bar and the settle', () => {
    const block = reduced();
    expect(block).toMatch(/\.halo\s*\{[^}]*animation:\s*none/);
    expect(block).toMatch(/\.bar\s*\{[^}]*transition:\s*none/);
    expect(block).toMatch(/\.card\.quiet[^{]*\{[^}]*transition:\s*none/);
  });

  it('leaves the hover and dim fade at the card’s own 120ms', () => {
    // The settle is the TITLE's colour. A `.card.quiet` transition would beat
    // `.card` on specificity and slow the reader's own hover to 400ms on every
    // done card — a 400ms response to a pointer, to settle a box that does not
    // change.
    const styles = nodeSource.slice(nodeSource.indexOf('<style>'));
    expect(styles).not.toMatch(/^\s{2}\.card\.quiet\s*\{/m);
    expect(styles).toMatch(/\.card\s*\{[^}]*transition:\s*\n?\s*opacity 120ms/);
  });

  it('marks nothing with a class no stylesheet reads', () => {
    // A `class:x` with no rule and no consumer is a marker that changes
    // nothing, and a test asserting it is a test of nothing (#2305 review).
    const styles = nodeSource.slice(nodeSource.indexOf('<style>'));
    const marked = [...nodeSource.matchAll(/class:([\w-]+)/g)].map(([, name]) => name);
    expect(marked.length).toBeGreaterThan(0);
    for (const name of new Set(marked)) expect(styles, name).toContain(`.${name}`);
  });

  it('leaves no animation outside that switch', () => {
    // Every `animation:` and every `transition:` in the component has to be
    // reachable by the block above — an animated property it does not name is
    // one that keeps moving for a reader who asked for stillness.
    const styles = nodeSource.slice(nodeSource.indexOf('<style>'));
    const animated = [...styles.matchAll(/^\s{2}([.\w][^{]*?)\s*\{([^}]*)\}/gm)]
      .filter(([, , body]) => /animation:(?!\s*none)|transition:(?!\s*none)/.test(body))
      .map(([, selector]) => selector.trim());
    // Pinned as a list, so a sixth animated rule cannot be added without this
    // test asking whether the switch above covers it. `.card` itself is the one
    // entry NOT in that block: its 120ms opacity/border fade is a response to a
    // hover or a selection — motion the reader asked for — rather than
    // something the page starts on its own.
    expect(animated.sort()).toEqual(ANIMATED_RULES);
  });
});
