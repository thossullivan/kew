/**
 * #2250 — what the Svelte Flow canvas is and is not allowed to do.
 *
 * The claims, each of which the canvas got wrong at least once — the last
 * four only in a real browser, after every unit test here had passed:
 *
 * 1. **A node has no link inside it.** The label used to be an
 *    `<a href={pr_url ?? url} target="_blank">` inside the clickable node, and
 *    one click opened the panel AND threw a GitHub tab over it (P6 of the UX
 *    brief).
 * 2. **Every node is a card, and the whole card is one click target.** The
 *    first canvas drew bare text rows on the edges, and its live edges took
 *    the pointer over the node centre, so a click on a node did nothing.
 * 3. **The canvas is a viewer, not an editor, and the wheel zooms it.** The
 *    canvas fills its own flex space rather than sitting in a page scroll
 *    box, so the wheel over it belongs to it, as on Dagster's graph (Tom's
 *    UI review, 2026-10-03).
 * 4. **Nothing floats over the graph that the artboard dropped.** Full-height
 *    layer captions sat over the canvas as tall boxes.
 * 5. **The camera moves through the flow, not around it.** A bound viewport
 *    written from outside repainted the graph but left the pan/zoom transform
 *    behind, so the next drag snapped back; and a binding that starts out
 *    undefined is never written back, so the popover never followed a pan.
 *
 * Asserted against the source where there is no DOM to mount (this suite has
 * none for the flow itself); the geometry is `canvas-view.test.ts`'s. The
 * browser pass is the Playwright harness the PR describes.
 */
import { readFileSync } from 'node:fs';
import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import type { EpicNode, EpicSnapshot } from '$lib/api';
import { motionOf } from '$lib/factory/motion';
import { POPOVER_RIM, placePopover } from './canvas-view';
import { NODE_H, NODE_W } from './dagre-layout';
import launchJson from '../../../fixtures/factory/epics-launch-fixes.json';
import DagNode, { type DagNodeData } from './DagNode.svelte';
import DagView from './DagView.svelte';
import { displayId } from './render';

const view = readFileSync(new URL('./DagView.svelte', import.meta.url), 'utf8');
const camera = readFileSync(new URL('./DagCamera.svelte', import.meta.url), 'utf8');
const nodeSource = readFileSync(new URL('./DagNode.svelte', import.meta.url), 'utf8');
const popoverSource = readFileSync(new URL('./NodePopover.svelte', import.meta.url), 'utf8');

/** Source with its PROSE stripped: the headers explain the anchor and the
 * captions these components no longer have, and an assertion that one is
 * absent must not read the sentence saying so. */
const code = (source: string): string =>
  source
    .replace(/<!--[\s\S]*?-->/g, '')
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/^\s*\/\/.*$/gm, '');
const nodeCode = code(nodeSource);
const viewCode = code(view);
const popoverCode = code(popoverSource);

/** The body of the `selector { ... }` rule, or a thrown miss. */
function rule(source: string, selector: string): string {
  const at = source.indexOf(`${selector} {`);
  if (at === -1) throw new Error(`no \`${selector} { ... }\` rule`);
  return source.slice(at, source.indexOf('}', at));
}

const node = (over: Partial<EpicNode> = {}): EpicNode => ({
  id: '2219',
  title: 'one needs-you source for the header, Monitor, epic legend and Work',
  status: 'in_review',
  status_since: '2026-10-02T14:48:00+00:00',
  url: 'https://github.com/thossullivan/kew/issues/2219',
  pr_url: 'https://github.com/thossullivan/kew/pull/2231',
  external: false,
  stuck: null,
  attention: null,
  blocked_reason: null,
  ...over
});

const NOW = Date.parse('2026-10-03T14:48:00+00:00');

const drawNode = (
  over: Partial<EpicNode> = {},
  selected = false,
  data: Partial<DagNodeData> = {}
): string =>
  render(DagNode, {
    props: {
      selected,
      data: {
        node: node(over),
        group: null,
        caption: '',
        drain: 12.34,
        frontier: false,
        // The card draws what the snapshot's motion says, and the fixture's
        // `in_review` default is a published pull request waiting on a person:
        // it does not move (#2305). `dag-motion.test.ts` is where the rule
        // itself is held.
        motion: motionOf(node(over).status),
        live: true,
        dim: false,
        now: NOW,
        badges: [],
        zoomedOut: false,
        ...data
      }
    }
  }).body;

describe('a node carries no link out of kew (P6)', () => {
  it('renders no <a> element at all, URLs or no URLs', () => {
    for (const over of [{}, { pr_url: null }, { url: null, pr_url: null }]) {
      expect(drawNode(over)).not.toMatch(/<a\b/);
    }
    // And no href anywhere in the component's source either — a `href` on a
    // Button or a wrapper would be the same defect one element along.
    expect(nodeCode).not.toMatch(/\bhref\b/);
    expect(nodeCode).not.toMatch(/target="_blank"/);
  });
});

describe('every node is a card (UXEpic, DESIGN.md dag-node-card)', () => {
  it('prints the card: mark, id, two-line title, status row and cost', () => {
    const html = drawNode({ status: 'in_review' });
    expect(html).toMatch(/class="card[\s"]/);
    expect(html).toContain('#2219');
    expect(html).toContain('in review');
    expect(html).toContain('$12.34');
    // The age since `status_since`: a day, as the card has room for.
    expect(html).toContain('1d');
    // The full title rides the `title` attribute, so a clamped one reads whole.
    expect(html).toContain('title="one needs-you source for the header');
  });

  it('draws a box for every status, not only the needs-you one', () => {
    const card = rule(nodeSource, '.card');
    expect(card).toContain('border: 1px solid var(--hair-box)');
    expect(card).toContain('background: var(--ink-raised)');
    for (const status of ['done', 'todo', 'queued', 'in_progress', 'held', 'failed']) {
      expect(drawNode({ status }), status).toMatch(/class="card[\s"]/);
    }
  });

  it('clamps the title to two lines with an ellipsis', () => {
    const tt = rule(nodeSource, '.tt');
    expect(tt).toContain('display: -webkit-box');
    expect(tt).toContain('-webkit-box-orient: vertical');
    expect(tt).toContain('-webkit-line-clamp: 2');
    expect(tt).toContain('overflow: hidden');
    expect(tt).not.toContain('white-space: nowrap');
  });

  it('marks the needs-you card, and nothing else, with the magenta treatment', () => {
    const hold = { kind: 'held', reason: 'preserved worktree', url: null, command: 'kew x' };
    const loud = drawNode({ status: 'held', attention: hold });
    expect(loud).toMatch(/class="card[^"]*\bneeds\b/);
    expect(loud).toContain('needs you');
    expect(drawNode({ status: 'in_review' })).not.toMatch(/class="card[^"]*\bneeds\b/);
    expect(rule(nodeSource, '.card.needs')).toContain('border-color: var(--accent)');
  });

  it('rings the selected card in cyan, outside the card', () => {
    expect(drawNode({}, true)).toMatch(/class="card[^"]*\bsel\b/);
    const sel = rule(nodeSource, '.card.sel');
    expect(sel).toContain('outline: 2px solid var(--accent-2)');
    expect(sel).toMatch(/outline-offset: [1-9]/);
  });

  it('is ONE click target: nothing inside the card takes the pointer', () => {
    // The node wrapper Svelte Flow renders is the click target. A child that
    // took pointer events (a label, a plate) is how a click on the title
    // stopped selecting the node.
    expect(nodeSource).toMatch(/\.card,\s*\.card \* \{\s*pointer-events: none;/);
    // And edges never take the pointer over a card.
    expect(rule(view, '.dag :global(.svelte-flow__edge),\n  .dag :global(.svelte-flow__edges)')).toContain(
      'pointer-events: none'
    );
  });
});

describe('the canvas is a viewer, not an editor', () => {
  it('refuses dragging and connecting', () => {
    expect(view).toContain('nodesDraggable={false}');
    expect(view).toContain('nodesConnectable={false}');
    // Per node as well as per canvas: a node that said otherwise would be
    // draggable the moment somebody flipped the flow-level prop back.
    expect(view).toContain('draggable: false');
    expect(view).toContain('connectable: false');
  });

  it('zooms on a plain wheel, with no modifier, and keeps the wheel off the page', () => {
    expect(view).toContain('zoomOnScroll={true}');
    expect(view).toContain('preventScrolling={true}');
    expect(view).toContain('zoomOnPinch={true}');
    expect(view).not.toContain('zoomActivationKey');
  });

  it('refuses the delete key with NO trigger rather than a null one', () => {
    // A null key became a shortcut with no key, which the shortcut library
    // warned about on every key press in the browser console.
    expect(view).toContain('deleteKey={[]}');
    expect(view).toContain('selectionKey={[]}');
    expect(viewCode).not.toContain('deleteKey={null}');
  });

  it('leaves Esc to the page, so one press is not two steps', () => {
    const handler = view.slice(view.indexOf('function oncanvaskey('));
    expect(handler.slice(0, handler.indexOf('\n  }'))).not.toContain('Escape');
    expect(view).not.toContain('escapeFocus');
  });

  it('answers only the presses `artboardKey` admits', () => {
    const handler = view.slice(view.indexOf('function oncanvaskey('));
    const body = handler.slice(0, handler.indexOf('\n  }'));
    expect(body.split('\n')[1].trim()).toBe('if (!artboardKey(onArtboard(e))) return;');
    expect(body.split('\n')[2].trim()).toBe('graphKey(e);');
    // The layer listens on the window, so "on the artboard" names the
    // element rather than reading `currentTarget`, which is the window.
    expect(view).toContain('currentTarget: dagEl');
  });

  it('answers the same keys from the body for a linked node, never Esc', () => {
    // A `?node=` link lands with the focus on the body (the router puts it
    // there), and the legend's keys must still work on the linked node.
    const fn = view.slice(view.indexOf('function onwindowkey('));
    const body = fn.slice(0, fn.indexOf('\n  }'));
    // With nothing selected only ← and → are answered, landing on the first
    // node (#2780).
    expect(body.split('\n')[1].trim()).toBe(
      'if (!strayKey(e, document.body) || (selected === null && !strayWithoutSelection(e.key))) return;'
    );
    // Both reach the graph through the console's key layer (#2512): one
    // registration, whose handler offers a press to each in turn.
    expect(viewCode).not.toContain('<svelte:window onkeydown');
    expect(view).toMatch(
      /bindShortcuts\('dag', DAG_KEYS, \(e\) => \{\s*oncanvaskey\(e\);\s*onwindowkey\(e\);\s*\}\)/
    );
    const keys = view.slice(view.indexOf('function graphKey('));
    expect(keys.slice(0, keys.indexOf('\n  }'))).not.toContain('Escape');
  });

  it('decides a press from the body with `strayKey`, which withholds Tab', () => {
    // The window handler must not hold a press of its own back or let one
    // through on its own terms: `strayKey` is where "which keys the graph
    // answers off its own element" is written and tested, and the ONE key it
    // withholds there is Tab — preventDefaulting Tab with the focus on the
    // body left the whole page, the popover's Copy and links included,
    // unreachable from the keyboard for as long as a node stayed selected.
    const fn = view.slice(view.indexOf('function onwindowkey('));
    const body = fn.slice(0, fn.indexOf('\n  }'));
    expect(body).not.toContain('metaKey');
    expect(body).not.toContain("'Tab'");
    expect(viewCode).toContain('strayKey');
  });

  it('opens a collapsed chain the keyboard lands on, not dropping the walk (#2250 r7)', () => {
    // `?node=` admits only epic nodes, so a group id handed to `onselect`
    // cleared the selection. Every keyboard move goes through `land`.
    const keys = view.slice(view.indexOf('function graphKey('));
    const body = keys.slice(0, keys.indexOf('\n  }'));
    expect(body).not.toContain('onselect?.(');
    expect(body.match(/land\(/g)?.length).toBe(3);
    const fn = view.slice(view.indexOf('function land('));
    const land = fn.slice(0, fn.indexOf('\n  }'));
    expect(land).toContain('expanded = [...expanded, group.id];');
    expect(land).toContain('forward ? group.ids[0] : group.ids[group.ids.length - 1]');
    expect(body).toContain('land(next, !e.shiftKey)');
    expect(body).toContain("land(next, dir === 'right' || dir === 'down')");
  });

  it('lets Tab leave the canvas: popover open, or past either end (#2250 r5)', () => {
    // A Tab the canvas always answered, over an order that wrapped, kept the
    // focus on the artboard for good, and the popover's controls with it.
    const keys = view.slice(view.indexOf('function graphKey('));
    const body = keys.slice(0, keys.indexOf('\n  }'));
    const tab = body.slice(body.indexOf("if (e.key === 'Tab' && at) {"));
    expect(tab.split('\n')[1].trim()).toBe('if (popover) return;');
    expect(tab).toContain('tabStep(model, at, e.shiftKey ? -1 : 1, { wrap: false })');
  });

  it('takes the keyboard focus on a node click, so the legend`s keys work next', () => {
    const fn = view.slice(view.indexOf('function onnodeclick('));
    expect(fn.slice(0, fn.indexOf('\n  }'))).toContain('dagEl?.focus(');
  });
});

describe('the controls the artboard draws', () => {
  it('has a row of controls bottom-left, a minimap bottom-right, and Fit all', () => {
    expect(viewCode).toMatch(/class="controls"/);
    expect(viewCode).toMatch(/aria-label="Zoom in"/);
    expect(viewCode).toMatch(/aria-label="Fit view"/);
    expect(viewCode).toMatch(/class="done-toggle"/);
    expect(viewCode).toMatch(/\{#if minimapShown\}\s*<MiniMap\s+position="bottom-right"/);
    expect(viewCode).toMatch(/Fit all <span class="kbd">F<\/span>/);
  });

  it('draws no layer captions over the canvas', () => {
    // The artboard dropped them; the full-height caption boxes floated over
    // the graph and stood in front of the cards.
    expect(viewCode).not.toContain('layer-head');
    expect(viewCode).not.toContain('ViewportPortal');
    expect(viewCode).not.toContain('layerLabel');
  });
});

describe('the camera (first frame, Fit all, follow the selection)', () => {
  it('decides the first frame once, off `firstFrame`, before the flow mounts', () => {
    expect(view).toContain('firstFrame(model, canvasW, canvasH, selected, frontier)');
    expect(viewCode).toMatch(/\{#if initialView\}\s*<SvelteFlow/);
    // No library fit on top of it: `fitView` would re-fit the whole graph and
    // throw the needs-you framing away.
    expect(viewCode).not.toContain('fitView={true}');
  });

  it('seeds the bound viewport, so pan and zoom are written back to it', () => {
    expect(view).toMatch(/viewport = initialView;/);
    expect(view).toContain('bind:viewport');
  });

  it('moves the camera through the flow`s own pan/zoom, never by writing the binding', () => {
    expect(camera).toContain('panZoom.setViewport(move.view');
    expect(viewCode).toMatch(/<DagCamera \{move\} \/>/);
    // The only writes to `viewport` are the seed above.
    expect(viewCode.match(/(?<!let )\bviewport = /g)).toHaveLength(1);
  });

  it('places the popover with `placePopover` and pans to make room for it', () => {
    expect(view).toContain('placePopover(scene.node, size, scene.cards, scene.furniture');
    expect(view).toContain('panForPopover(scene.node, size, scene.cards, scene.furniture');
    expect(viewCode).not.toContain('NodeToolbar');
    expect(viewCode).toMatch(/<NodePopover\b/);
  });
});

/**
 * Design review C2 — what the canvas floats over its own cards, and what it
 * does on a phone. Six findings, each a thing a reader lost to a decoration:
 *
 * * D19 the zoom controls and the key legend sat on cards and cut their
 *   status rows, through a 92%-transparent fill;
 * * C12 those same decorations took the pointer, so a drag that began on them
 *   did nothing;
 * * D5 at 390px the strip overprinted, the minimap took a third of the canvas
 *   and the popover covered the node it was about;
 * * D21 edges were cyan with nothing selected;
 * * D24 the strip's label ran into its first item;
 * * C3 the flow's height was a percentage of an indefinite parent.
 */
/** The canvas as the server renders it: no flow (it needs a measured canvas),
 * which is exactly the markup the rail's place in the column can be read
 * off. */
const dagBody = render(DagView, {
  props: { snapshot: launchJson.snapshot as unknown as EpicSnapshot, economics: null }
}).body;

/** The slice of `html` holding the element that opens at `at`, by balancing
 * its `<div>`s — enough to ask whether another element is INSIDE it. */
function element(html: string, at: number): string {
  let depth = 0;
  for (let i = at; i < html.length; i++) {
    if (html.startsWith('<div', i)) depth += 1;
    else if (html.startsWith('</div>', i)) {
      depth -= 1;
      if (depth === 0) return html.slice(at, i + 6);
    }
  }
  return html.slice(at);
}

describe('the canvas floats nothing over a card (D19, C12)', () => {
  /**
   * Round 1 reserved the toolbar's band as padding in every fit, and the
   * browser pass at 1024x700 still found two cards under it: the first frame
   * of a big epic is a CROP at 100%, so cards the frame is not about carry on
   * past every edge of a short canvas, and a floating band cannot be emptied
   * by arithmetic. So the toolbar, the legend and the frame's caption are a
   * ROW BELOW the node area, and the node area clips — which is a claim about
   * this markup, and is why it is pinned here rather than in canvas-view.
   */
  it('puts the toolbar, the legend and the caption in a rail below the graph', () => {
    // One row, after the node area, inside the canvas's own column.
    expect(viewCode).toMatch(/<\/div>\s*<div class="bar">/);
    expect(viewCode).toMatch(/<div class="bar">\s*<div class="controls"/);
    expect(viewCode).toMatch(/\{#if frameFocus !== null\}[\s\S]*?<div class="hint"[\s>]/);
    expect(viewCode.indexOf('class="hint"')).toBeGreaterThan(viewCode.indexOf('class="bar"'));
    expect(viewCode.indexOf('class="keys"')).toBeGreaterThan(viewCode.indexOf('class="hint"'));
    // Nothing in the rail is positioned over the canvas any more.
    for (const selector of ['.bar', '.hint', '.keys']) {
      expect(rule(viewCode, selector)).not.toContain('position: absolute');
    }
    expect(rule(viewCode, '.dag')).toContain('flex-direction: column');
    expect(rule(viewCode, '.bar')).toContain('flex-shrink: 0');
  });

  it('renders the rail OUTSIDE the node area, as its sibling', () => {
    // The rendered markup, not only the source: the rail being a sibling of
    // the box the cards are drawn in is the whole of the fix, and a stray
    // indent that nested it back inside would pass every other claim here.
    const dag = element(dagBody, dagBody.indexOf('<div class="dag'));
    const floor = element(dag, dag.indexOf('<div class="floor'));
    expect(floor).not.toContain('class="bar');
    expect(floor).not.toContain('class="controls');
    expect(dag).toContain('class="bar');
    expect(dag.indexOf('class="bar')).toBeGreaterThan(dag.indexOf('class="floor'));
  });

  it('measures the fit in the NODE area`s geometry, not the pane`s', () => {
    // `canvasW`/`canvasH` are what every fit is computed against, so they
    // have to be the box the cards are drawn in — the one the rail is not in.
    expect(view).toMatch(
      /<div class="floor" bind:clientWidth=\{canvasW\} bind:clientHeight=\{canvasH\}>/
    );
    expect(rule(viewCode, '.floor')).toContain('overflow: hidden');
    expect(rule(viewCode, '.floor')).toContain('position: relative');
  });

  it('leaves the minimap as the one overlay, and measures it in the fit`s geometry', () => {
    // DESIGN.md's `dag-canvas` draws the map bottom-right, so it is the one
    // thing still over the graph, and the pads reserve its corner
    // (`padClearOfMinimap`). It gives way to an open popover (D38, #2977),
    // which the epic-popover-pinned browser check proves on go-live.
    expect(view).toContain('padClearOfMinimap(');
    expect(rule(viewCode, '.dag :global(.svelte-flow__minimap)')).toContain('margin: 16px');
  });

  it('fills the caption and the key legend, rather than washing them', () => {
    for (const selector of ['.hint', '.keys']) {
      expect(rule(viewCode, selector)).toContain('background: var(--ink-raised)');
      expect(rule(viewCode, selector)).not.toContain('color-mix');
    }
  });

  it('keeps the key legend inert: it is a reading, never a target (C12)', () => {
    expect(rule(viewCode, '.keys')).toContain('pointer-events: none');
    // And the decorations that ARE still over the canvas take no pointer.
    expect(rule(viewCode, '.leader')).toContain('pointer-events: none');
  });
});

describe('the flow fills the node area, never a percentage of it (C3)', () => {
  it('is absolutely positioned inside the positioned parent', () => {
    const flow = rule(viewCode, '.dag :global(.dag-flow)');
    expect(flow).toContain('position: absolute');
    expect(flow).toContain('inset: 0');
    expect(flow).not.toMatch(/height: 100%/);
    expect(rule(viewCode, '.floor')).toContain('position: relative');
    expect(rule(viewCode, '.floor')).toContain('min-height: 0');
  });
});

describe('the canvas on a phone (D5)', () => {
  it('drops the key legend and the hint`s sentence below 1024', () => {
    expect(viewCode).toMatch(
      /@media \(max-width: 1023px\) \{\s*\.keys,\s*\.hint \.said:not\(\.keep\),\s*\.hint \.lead \{\s*display: none/
    );
  });

  it('turns the popover into a bottom sheet, placing and panning for neither', () => {
    expect(view).toContain('const sheet = $derived(canvasW > 0 && canvasW < SHEET_BELOW_W)');
    // Nothing is placed in a sheet: it is not beside a card.
    expect(view).toMatch(/if \(!popoverModel \|\| !popover \|\| selected === null \|\| sheet\) return null;/);
    expect(view).toMatch(/if \(sheet\) return;/);
    expect(viewCode).toContain('class:sheet');
    expect(viewCode).toMatch(/style:left=\{sheet \? undefined :/);
    const at = rule(viewCode, '.pop-at.sheet');
    expect(at).toContain('bottom: 0');
    expect(at).toContain('left: 0');
    expect(at).toContain('right: 0');
  });

  it('fixes the sheet to the WINDOW, above the rail the page pins (AC4)', () => {
    // Anchored in the canvas, the sheet opened behind the rail: the canvas is
    // a short flex child that clips (`.floor { overflow: hidden }`) and the
    // rail is a band under it (#2278), so at 390x844 only the sheet's header
    // row showed and at 390x700 none of it did. Fixed to the viewport, its
    // bottom edge is the window's and it stacks over the rail while open.
    const at = rule(viewCode, '.pop-at.sheet');
    expect(at).toContain('position: fixed');
    expect(at).toContain('bottom: 0');
    expect(at).toContain('left: 0');
    expect(at).toContain('right: 0');
    expect(at).toMatch(/z-index: \d+/);
    // Above the rail, which is page markup with no z-index of its own.
    expect(Number(/z-index: (\d+)/.exec(at)![1])).toBeGreaterThan(12);
    const sheetRule = rule(popoverCode, '.pop.sheet');
    expect(sheetRule).toContain('max-height: 70vh');
    // The canvas's own height is no longer what caps it: it is not in it.
    expect(sheetRule).not.toContain('--canvas-h');
    expect(viewCode).not.toContain('--canvas-h');
    // What outgrows the sheet scrolls inside it — in `.pbody`, the part
    // between the header and the links row, so the links stay on screen
    // (#2783, N12; the box scrolled whole before, links and all).
    expect(rule(popoverCode, '.pbody')).toContain('overflow-y: auto');
  });

  it('lays the standalone strip out as a list, in 32px rows', () => {
    const phone = viewCode.slice(viewCode.indexOf('@media (max-width: 1023px)', viewCode.indexOf('.strip-grid')));
    expect(phone).toContain('grid-template-columns: minmax(0, 1fr)');
    expect(phone).toContain('min-height: 32px');
  });
});

/**
 * The box beside a card has to fit the node area, which the D19 rail made
 * 52px shorter. `placePopover` can only clamp its top to the 8px rim: a
 * popover taller than the node area is pinned there and the rest of it — the
 * links row, "Open in Work" included — is clipped away by
 * `.floor { overflow: hidden }`, with no scrollbar anywhere to reach it. The
 * sheet was given `max-height`/`overflow-y` for this; the box beside a card
 * needs the same, against the node area's height rather than the window's.
 */
describe('the box beside a card is capped to the node area it sits in', () => {
  it('caps `.pop-at` to the node area less the rim, and scrolls `.pop` inside', () => {
    const at = rule(viewCode, '.pop-at');
    // A percentage of the POSITIONED parent (`.floor`), whose height is
    // definite for an absolutely positioned child — no `--canvas-h` written
    // from script, which the sheet's own fix retired.
    expect(at).toContain(`max-height: calc(100% - ${2 * POPOVER_RIM}px)`);
    expect(viewCode).not.toContain('--canvas-h');
    // The cap is handed down by a column flex parent rather than clipping
    // here: `.pop` keeps its border and the page's one drop shadow OUTSIDE
    // any scroll box, which an `overflow` on `.pop-at` would have eaten.
    expect(at).toContain('display: flex');
    expect(at).toContain('flex-direction: column');
    expect(at).not.toMatch(/overflow(-y)?: (auto|scroll|hidden)/);

    const pop = rule(popoverCode, '.pop');
    // Without this a flex item never shrinks below its content, so the cap
    // above would be ignored and the clipping would be unchanged.
    expect(pop).toContain('min-height: 0');
    // The scroll is the body's, not the box's (#2783, N12): a `.pop` that
    // scrolled whole kept its links row below the fold, cut off at its own
    // bottom edge. The body gives way to the cap and scrolls; the links row
    // holds its height at the foot.
    const body = rule(popoverCode, '.pbody');
    expect(body).toContain('overflow-y: auto');
    // Never under two of its lines, though (D38, #2977): at 0 a three-line
    // sentence left "review stale" cut at its foot.
    expect(body).toContain('min-height: calc(2 * 13px * 1.4 + 9px)');
    expect(rule(popoverCode, '.links')).toContain('flex-shrink: 0');
  });

  it('leaves a box capped to that height fully inside the node area', () => {
    // The cap in the stylesheet and the rim `placePopover` keeps are the same
    // number: a popover measured at the cap must land wholly on the canvas,
    // at the top rim, with nothing for `.floor` to clip.
    const capped = Number(/max-height: calc\(100% - (\d+)px\)/.exec(rule(viewCode, '.pop-at'))![1]);
    expect(capped).toBe(2 * POPOVER_RIM);
    const h = 480;
    const node = { x: 400, y: 200, width: NODE_W, height: NODE_H };
    const size = { width: 320, height: h - capped };
    const p = placePopover(node, size, [], [], 1024, h);
    expect(p.y).toBe(POPOVER_RIM);
    expect(p.y + size.height).toBe(h - POPOVER_RIM);
  });

  it('is the clipping that cap prevents: an uncapped box runs off the floor', () => {
    // The failure itself, so the cap is never dropped as decoration. 520 is
    // what a needs-you popover measures with a long reason and a wrapped
    // command; the node area is 480 once the 52px rail is taken off a 532px
    // canvas pane.
    const h = 480;
    const node = { x: 400, y: 200, width: NODE_W, height: NODE_H };
    const p = placePopover(node, { width: 320, height: 520 }, [], [], 1024, h);
    expect(p.y).toBe(POPOVER_RIM);
    expect(p.y + 520).toBeGreaterThan(h);
  });
});

describe('the strip`s label has a column of its own (D24)', () => {
  it('keeps a 24px gap between the label and the first item', () => {
    const strip = rule(viewCode, '.strip');
    expect(strip).toContain('grid-template-columns: 196px minmax(0, 1fr)');
    expect(strip).toContain('column-gap: 24px');
  });
});

describe('cyan is the selected lineage (D21)', () => {
  it('tells every edge whether anything is selected at all', () => {
    expect(view).toContain('anySelected: selected !== null');
  });

  it('decides the stroke in `edgeStroke`, not in the renderer', () => {
    const edge = readFileSync(new URL('./DagEdge.svelte', import.meta.url), 'utf8');
    expect(edge).toContain('edgeStroke(data.style');
    // The old inline rule, which had cyan in it twice.
    expect(code(edge)).not.toContain("? 'var(--accent-2)'");
  });
});

describe('a selection the reader did not make opens no popover (D20)', () => {
  it('opens one only for a linked node', () => {
    expect(view).toMatch(/if \(linked && selected !== null && model\.nodes\.some/);
  });

  it('calls the frame "the selected node" only for a linked one', () => {
    // The page's opening default IS the needs-you node, so `frameFocus ===
    // selected` holds for it too: without `linked` the hint read the page's
    // own default as a followed `?node=` link, and the first frame lost both
    // the needs-you diamond and the words that explain the crop.
    expect(view).toMatch(/frameLinked = linked && frameFocus !== null && frameFocus === selected/);
  });
});

describe('the staleness badge on a card (#2303)', () => {
  const staleBadge = {
    kind: 'review-stale' as const,
    label: 'review stale',
    needsAPerson: false,
    causes: [
      {
        origin: 'head' as const,
        text: 'the head moved to a1b2c3d after the round 2 READY verdict on 9f8e7d1',
        commit: 'a1b2c3d',
        href: 'https://github.com/o/r/pull/2059/commits/a1b2c3d',
        since: null
      }
    ]
  };
  const conflictBadge = {
    kind: 'needs-rebase' as const,
    label: 'needs rebase',
    needsAPerson: true,
    causes: [
      {
        origin: 'base' as const,
        text: 'the head and its base have conflicting changes',
        commit: '',
        href: '',
        since: null
      }
    ]
  };

  it('prints the badge and its words on the status row', () => {
    const html = drawNode({ status: 'in_review' }, false, { badges: [staleBadge] });

    expect(html).toContain('review stale');
    // Its cause rides the title attribute, so a hover says WHAT moved even on
    // a card with no room for the sentence.
    expect(html).toContain('after the round 2 READY verdict');
  });

  it('is quiet in --text-3 unless a person is the next move', () => {
    expect(drawNode({}, false, { badges: [staleBadge] })).toContain('var(--text-3)');
    expect(drawNode({}, false, { badges: [conflictBadge] })).toContain('var(--accent)');
  });

  it('draws no badge at all when nothing moved', () => {
    const html = drawNode({ status: 'in_review' });

    expect(html).not.toContain('review stale');
    expect(html).not.toContain('needs rebase');
  });

  it('drops the word when zoomed out, keeping the mark and its label', () => {
    const html = drawNode({}, false, { badges: [staleBadge], zoomedOut: true });

    // No word on the row…
    expect(html).not.toMatch(/>review stale</);
    // …but the mark is there, named for a screen reader.
    expect(html).toContain('aria-label="review stale"');
  });

  it('carries no link inside the card, badge or not (P6)', () => {
    expect(drawNode({}, false, { badges: [staleBadge] })).not.toMatch(/<a\b/);
  });

  it('drops whole status words rather than the badge when both are on the row', () => {
    // A half-drawn cause is worse than no badge, so the words give way first —
    // and they give way a WORD at a time (#2543, D30): a word that does not
    // fit wraps onto a line the row's height hides, never `IN REVIE…`.
    const badged = rule(nodeSource, '.sl.badged');
    expect(badged).toContain('display: flex');
    const segs = rule(nodeSource, '.segs');
    expect(segs).toContain('flex-wrap: wrap');
    expect(segs).toContain('height: 14px');
    expect(segs).toContain('overflow: hidden');
    expect(rule(nodeSource, '.segs .w')).toContain('flex-shrink: 0');
  });
});

/**
 * #2533 — the two controls Tom's console review found did not do what they
 * said. The browser half (a click that collapses, a click that moves) is
 * `ui-check`'s; what an SSR render can pin is which arrow is live where.
 */
describe('the lineage arrows and the show-done toggle (#2533)', () => {
  const drawn = (selected: string | null): string =>
    render(DagView, {
      props: {
        snapshot: launchJson.snapshot as unknown as EpicSnapshot,
        economics: null,
        selected
      }
    }).body;
  /** The opening tag of the toolbar button labelled `label`. */
  const button = (html: string, label: string): string => {
    const at = html.indexOf(`aria-label="${label}"`);
    expect(at, label).toBeGreaterThan(-1);
    return html.slice(html.lastIndexOf('<button', at), html.indexOf('>', at) + 1);
  };
  const live = (html: string, label: string): boolean => !/\sdisabled\b/.test(button(html, label));

  it('closes ← at a root and opens → onto its child', () => {
    // 2000 → 2001 → 2040: 2000 is a root of the fixture graph.
    const html = drawn('2000');
    expect(live(html, 'Upstream in lineage')).toBe(false);
    expect(live(html, 'Downstream in lineage')).toBe(true);
  });

  it('closes → at a sink and opens ← onto its parent', () => {
    // 2047 → 2055, and nothing hangs off 2055.
    const html = drawn('2055');
    expect(live(html, 'Upstream in lineage')).toBe(true);
    expect(live(html, 'Downstream in lineage')).toBe(false);
  });

  it('closes both with nothing selected', () => {
    const html = drawn(null);
    expect(live(html, 'Upstream in lineage')).toBe(false);
    expect(live(html, 'Downstream in lineage')).toBe(false);
  });

  it('forgets every chain the operator opened whenever show-done is pressed', () => {
    // `expanded` only grows (a group click, a keyboard walk through one), so
    // a toggle that only flipped `showDone` left those chains open.
    const toggle = view.slice(view.indexOf('function toggleDone'));
    expect(toggle.slice(0, toggle.indexOf('\n  }\n'))).toMatch(/expanded = \[\];/);
    expect(view).toMatch(/onclick=\{toggleDone\}/);
  });

  it('refits after a toggle, so the group card it draws is on screen (#2560)', () => {
    const toggle = view.slice(view.indexOf('function toggleDone'));
    expect(toggle.slice(0, toggle.indexOf('\n  }\n'))).toMatch(/fitAll\(\);/);
  });
});

/**
 * #2560 — the done toggle on host-contract, the live epic it did nothing on:
 * eight finished nodes in a chain into #2399, which #2526 (in review, needs
 * you) depends on.
 */
describe('the done toggle says what it folds, and why it cannot (#2560)', () => {
  const template = launchJson.snapshot.nodes[0];
  const chainIds = ['2378', '2379', '2380', '2381', '2397', '2398', '2411', '2433'];
  const hostContract = {
    ...launchJson.snapshot,
    frontier: [],
    nodes: [
      ...chainIds.map((id) => ({ ...template, id, status: 'done' })),
      { ...template, id: '2399', status: 'done' },
      {
        ...template,
        id: '2526',
        status: 'in_review',
        attention: { kind: 'merge_decision', reason: 'merge decision', url: null }
      }
    ],
    edges: [
      ...chainIds.slice(1).map((id, i) => [chainIds[i], id]),
      ['2433', '2399'],
      ['2399', '2526']
    ]
  } as unknown as EpicSnapshot;
  const drawn = (snapshot: EpicSnapshot, selected: string | null = null): string =>
    render(DagView, { props: { snapshot, economics: null, selected } }).body;
  const toggle = (html: string): string => {
    const at = html.indexOf('class="done-toggle');
    expect(at).toBeGreaterThan(-1);
    return html.slice(html.lastIndexOf('<button', at), html.indexOf('</button>', at));
  };

  it('counts the chain it folds, as text', () => {
    const t = toggle(drawn(hostContract));
    expect(t).toContain('Done · collapsed (8)');
    expect(t).not.toMatch(/\sdisabled\b/);
    expect(t).toContain('title="Show every done node"');
  });

  it('is disabled and says why where nothing can fold', () => {
    // Every finished node in the fixture epic feeds open work directly, and
    // what is left behind them is no chain of two.
    const t = toggle(drawn(launchJson.snapshot as unknown as EpicSnapshot));
    expect(t).toMatch(/\sdisabled\b/);
    expect(t).toContain('title="No finished chain can fold here"');
    // DESIGN.md's `dag-canvas`: disabled WITH the sentence (D39), not
    // "Done · shown" in the disabled tint.
    // Once in its title, once as its text.
    expect(t.split('No finished chain can fold here')).toHaveLength(3);
    expect(t).not.toContain('Done · shown');
  });

  it('opens the group a ?node= link lands in, with ← and → both live', () => {
    const html = drawn(hostContract, '2380');
    const button = (label: string): string => {
      const at = html.indexOf(`aria-label="${label}"`);
      return html.slice(html.lastIndexOf('<button', at), html.indexOf('>', at) + 1);
    };
    expect(button('Upstream in lineage')).not.toMatch(/\sdisabled\b/);
    expect(button('Downstream in lineage')).not.toMatch(/\sdisabled\b/);
  });
});

/**
 * #2543, D27 — an epic with no edges has nothing for a canvas to trace. It
 * drew a 330px empty canvas with zoom controls and the lineage legend, its
 * nodes in a text strip below; it is one card per node now.
 */
describe('the edge-less epic (#2543)', () => {
  const edgeless = {
    ...(launchJson.snapshot as unknown as EpicSnapshot),
    edges: []
  } as EpicSnapshot;
  const html = render(DagView, { props: { snapshot: edgeless, economics: null, selected: null } }).body;

  it('draws one card per node and no canvas, strip or lineage legend', () => {
    const want = edgeless.nodes.length;
    expect(html).toContain(`aria-label="${want} independent nodes"`);
    expect(html.match(/class="grid-card/g)?.length).toBe(want);
    for (const n of edgeless.nodes) expect(html).toContain(`aria-label="${displayId(n.id)} `);
    expect(html).not.toContain('class="dag');
    expect(html).not.toMatch(/class="strip[ "]/);
    expect(html).not.toContain('Upstream in lineage');
  });

  it('keeps the reasons the card leaves out on the card`s button', () => {
    // #2053 is blocked with a recorded reason; the card prints `blocked` alone.
    const blocked = edgeless.nodes.find((n) => n.blocked_reason)!;
    expect(html).toContain(blocked.blocked_reason!);
  });

  it('is still a canvas the moment there is one edge', () => {
    const one = render(DagView, {
      props: {
        snapshot: { ...edgeless, edges: [[edgeless.nodes[0].id, edgeless.nodes[1].id]] } as EpicSnapshot,
        economics: null,
        selected: null
      }
    }).body;
    expect(one).toContain('class="dag');
    expect(one).not.toContain('class="grid-card');
  });
});

