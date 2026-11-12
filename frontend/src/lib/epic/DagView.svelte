<script lang="ts">
  // The epic DAG as a Svelte Flow canvas (#2250, the brief's §2.3, drawn to
  // the UXEpic artboard).
  //
  // No fetching, no timers: both live (poll/SSE) and replay feed this
  // component frames, so replay reuse stays free as long as this is
  // side-effect-free and its props keep their shape.
  //
  // Geometry is `flow-model.ts`'s (over `dagre-layout.ts`), and status is not
  // an input to it: the layout is cached by topology hash, so the status and
  // telemetry churn that drives most frames never moves a coordinate. Where
  // the camera looks and where the popover goes are `canvas-view.ts`'s.
  //
  // What this file owns: the Flow wiring, the hover lineage, the keyboard,
  // the controls, and which node the popover is attached to. Everything it
  // draws comes from a pure module — `buildFlowModel`, `lineage`,
  // `tabStep`/`neighbor`, `nodePopover`, `firstFrame`, `placePopover` —
  // because a canvas is the one surface in this app with no test harness of
  // its own.
  import type {
    EpicEconomics,
    EpicNode,
    EpicNodeDetail,
    EpicSnapshot,
    PrFreshnessRow
  } from '$lib/api';
  import type { Source } from '$lib/monitor/readouts';
  import {
    Background,
    BackgroundVariant,
    MiniMap,
    Position,
    SvelteFlow,
    type Edge,
    type EdgeTypes,
    type Node,
    type NodeTypes,
    type Viewport
  } from '@xyflow/svelte';
  import '@xyflow/svelte/dist/base.css';
  import { tick, untrack } from 'svelte';
  import StatusMark from '$lib/ui/StatusMark.svelte';
  import Title from '$lib/ui/Title.svelte';
  import { plainTitle } from '$lib/factory/names';
  import { servedMotion, type Motion } from '$lib/factory/motion';
  import { motionClock } from '$lib/time/clock.svelte';
  import { keepAttentionInGraph, needsYou, statusLineText, statusSegments } from './attention';
  import {
    FIT_PAD,
    FRAME_PAD,
    firstFrame,
    fitAllView,
    hasMinimap,
    MAX_ZOOM,
    MIN_ZOOM,
    MINIMAP_H,
    MINIMAP_W,
    READABLE_ZOOM,
    POPOVER_SHORT_BELOW_H,
    SHEET_BELOW_W,
    onScreen,
    padClearOfMinimap,
    panForPopover,
    placePopover,
    reveal,
    viewFor,
    zoomBy,
    type Box,
    type FrameReason
  } from './canvas-view';
  import { splitStandalone } from './metro';
  import { frameClock } from './replay';
  import { bindShortcuts, keyGlyph, legendOf, registerShortcuts } from '$lib/keys/registry';
  import {
    artboardKey,
    DAG_KEYS,
    lineageStops,
    neighbor,
    readingOrder,
    strayKey,
    strayWithoutSelection,
    tabStep
  } from './dag-keys';
  import { EPIC_KEYS } from './timeline';
  import DagCamera, { type CameraMove } from './DagCamera.svelte';
  import DagEdge, { type DagEdgeData } from './DagEdge.svelte';
  import DagNode, { type DagNodeData } from './DagNode.svelte';
  import NodePopover from './NodePopover.svelte';
  import { formatSelection, parseSelection, selectLineage, withinSelection } from './lineage-select';
  import { NODE_H, NODE_W } from './dagre-layout';
  import {
    buildFlowModel,
    lineage,
    type DoneGroup,
    type FlowModel,
    type FlowNode
  } from './flow-model';
  import { nodePopover, NO_FOCUS, runHistory, select as selectFocus } from './node-popover';
  import { badgesFor, type StalenessBadge } from '$lib/freshness';
  import { displayId, statusColor, statusWord } from './render';
  import { SEP } from '$lib/utils';

  let {
    snapshot,
    economics,
    selected = null,
    popover = false,
    linked = false,
    streaming = true,
    replay = false,
    onselect,
    onpopover,
    lineage: lineageText = null,
    onlineage = undefined,
    history = undefined,
    freshness = undefined
  }: {
    snapshot: EpicSnapshot;
    economics: EpicEconomics | null;
    selected?: string | null;
    /** Whether the selected node's popover is open. The page owns it, because
     * Esc is a two-step rule over both this and the selection. */
    popover?: boolean;
    /** Whether the selection is the READER's — a `?node=` link, a click, a
     * keyboard walk — rather than the page's opening default (D20). Only
     * theirs opens a popover unasked; a default selection fills the rail and
     * leaves the canvas clear. */
    linked?: boolean;
    /** Whether the page's live stream is OPEN — one fact, about the channel.
     *
     * False stops the motion clock and takes the halo and the running bar off
     * every card: a bar that keeps growing on a dropped stream is drawing a
     * second nothing confirmed (#2305). It deliberately does NOT move the
     * frame's clock — that is `replay`'s to say — so ages on a live frame keep
     * reading against the wall clock while the server is down instead of
     * jumping backwards to the last tick. Defaults true so a static render
     * still animates what the snapshot it was handed says is executing. */
    streaming?: boolean;
    /** Whether this frame is a tick out of the JOURNAL rather than the world as
     * it is now — the other fact, about the frame.
     *
     * History does not move (#2305), so a replayed frame animates nothing, and
     * it is read against the snapshot's own `tick_ts` instead of the wall
     * clock, because a replayed tick's ages belong to the tick. */
    replay?: boolean;
    onselect?: (id: string | null) => void;
    onpopover?: (open: boolean) => void;
    /** The lineage selection's text as the URL holds it (`?select=`, #2544):
     * `+#2469` upstream, `#2469+` downstream, `+#2469+` both. The page owns
     * it, for `selected`'s reason — it is URL state. */
    lineage?: string | null;
    /** Write a new lineage selection: its canonical text, or null to clear. */
    onlineage?: (text: string | null) => void;
    /** The selected node's runs (`fetchEpicNode`), which the popover leads
     * with. Undefined while unread, which draws no history line at all. */
    history?: Source<EpicNodeDetail>;
    /**
     * Artifact freshness by item id (#2303) — the badges' only input. An absent
     * node has nothing journaled about its head, which draws NO badge: a page
     * that could not read the endpoint must not paint "current" on every card.
     */
    freshness?: Map<string, PrFreshnessRow>;
  } = $props();


  // The canvas's three paint bands. Edges never cross a card any more
  // (dagre-layout.ts), so the bands only decide which edge wins where two
  // share a channel: the live one and the lineage being looked at are drawn
  // over the rest.
  const QUIET_EDGE_Z = 1;
  const NODE_Z = 2;
  const LIVE_EDGE_Z = 3;

  /** The node type key. One custom node; `nodeTypes` must be a stable object
   * or Svelte Flow re-registers (and so remounts) every node per frame. */
  const nodeTypes: NodeTypes = { dag: DagNode };
  const edgeTypes: EdgeTypes = { dag: DagEdge };

  // Handles declared as DATA rather than as `<Handle>` elements inside the
  // node: `getEdgePosition` refuses an edge whose endpoints have no handle
  // bounds, and a `<Handle>` reads the flow store out of context, which is
  // what would stop `DagNode` server-rendering on its own. They sit on the
  // card's two ports, which is where every route the layout draws starts and
  // ends.
  const HANDLES = Object.freeze([
    Object.freeze({ id: 'out', type: 'source' as const, position: Position.Right, x: NODE_W, y: NODE_H / 2 }),
    Object.freeze({ id: 'in', type: 'target' as const, position: Position.Left, x: 0, y: NODE_H / 2 })
  ]);

  /** The URL's selection, parsed. The box (`LineageBox`) says why a bad one
   * is refused; here a refused one is no selection. */
  const parsedLineage = $derived(parseSelection(lineageText ?? ''));
  const lineageSel = $derived(parsedLineage.ok ? parsedLineage.selection : null);
  /** Group ids the operator has clicked open. */
  let expanded = $state<string[]>([]);
  /** "show done": off collapses fully done upstream chains (the brief's
   * default). The toggle lives in the controls. */
  let showDone = $state(false);

  /**
   * The "show done" toggle. Either way it is pressed, it forgets every chain
   * the operator opened (#2533). `expanded` only ever grows — a click on a
   * group, and every keyboard walk through one, adds to it — so a toggle that
   * only flipped `showDone` left every chain opened that way drawn open: on a
   * live epic "Collapse finished chains" collapsed nothing. What it leaves
   * open now is the selected node's own chain, which the selection opens
   * and not something the operator opened.
   *
   * Then the view refits (#2560): the group card stands on its head member's
   * slot, which the frame the operator was looking at need not include, and a
   * toggle whose result is off screen reads as a toggle that did nothing.
   */
  function toggleDone(): void {
    if (!foldable) return;
    showDone = !showDone;
    expanded = [];
    fitAll();
  }
  /** The node whose lineage is lit, or null. */
  let hovered = $state<string | null>(null);
  /** True for the moment after a copy landed, so the popover can say so. It is
   * about ONE node's command, so moving the selection takes it back — a
   * "Copied" over somebody else's command is a claim about a copy nobody
   * made. */
  let copied = $state(false);
  $effect(() => {
    void selected;
    copied = false;
  });

  /**
   * An epic with no edges at all has nothing for a canvas to trace (#2543,
   * D27): the live launch-fixes, 29 nodes, drew a 330px empty canvas with zoom
   * controls and a lineage legend while every node sat in a text strip below
   * it. Such an epic is a card grid instead — every node a card, never a
   * blank box (DESIGN.md) — and no strip, because there is no graph for the
   * finished ones to be moved out of.
   */
  const independent = $derived(snapshot.edges.length === 0 && snapshot.nodes.length > 0);
  // A done node with no edges at all is history nobody has to trace: it goes
  // to the strip under the graph. This is the ONE status-dependent step and it
  // deliberately sits outside the layout, so status still never reaches the
  // geometry. `keepAttentionInGraph` then takes back the nodes that partition
  // would strand — a done node somebody is waiting on is not history, and only
  // the graph draws the needs-you card the header's count promises.
  const split = $derived(
    independent
      ? { graph: snapshot.nodes, strip: [] }
      : keepAttentionInGraph(splitStandalone(snapshot.nodes, snapshot.edges))
  );

  /**
   * How many done nodes "Collapse finished chains" folds away, with nothing
   * opened: the toggle's count, and 0 disables it (#2560).
   *
   * The collapse no longer gives way to the path into the node being looked
   * at — on an epic whose every finished node fed the one needs-you node,
   * that rule left nothing to fold, and the toggle did nothing. The flow
   * model keeps the DIRECT finished parents of open work drawn instead, so
   * the step that work hangs off is never inside a summary.
   */
  const foldable = $derived(
    buildFlowModel(split.graph, snapshot.edges).groups.reduce((sum, g) => sum + g.count, 0)
  );

  /**
   * A selected node folded into a group — a `?node=` link, or a ← / → walk —
   * opens that group, so the selection is always a card on the canvas. It
   * folds again once the selection leaves it, unless the operator opened it.
   */
  //
  // The lineage selection's root opens its group the same way: a selection
  // rooted inside "8 done" would otherwise dim every card around nothing.
  const model = $derived.by<FlowModel<EpicNode>>(() => {
    const base = buildFlowModel(split.graph, snapshot.edges, { showDone, expanded });
    const roots = [selected, lineageSel?.id ?? null].filter((id) => id !== null);
    const own = base.groups.filter((g) => roots.some((id) => g.ids.includes(id)));
    if (!own.length) return base;
    return buildFlowModel(split.graph, snapshot.edges, {
      showDone,
      expanded: [...expanded, ...own.map((g) => g.id)]
    });
  });

  const frontier = $derived(new Set(snapshot.frontier));
  // `lineage` answers null for an id this model does not draw, which is what
  // keeps `hovered` from dimming the whole canvas: `onnodepointerleave` never
  // fires for a node a new frame REMOVED from under the pointer (one moved to
  // the standalone strip, or collapsed into a done group), so the id outlives
  // the node. Null reads as no hover, which is what is true.
  const lit = $derived(hovered === null ? null : lineage(model, hovered));
  /**
   * The lineage selection (#2544): what the box asked for, and what it keeps
   * at full weight. A hover outranks it while the pointer is on a card — the
   * reader is asking a second, quicker question — and hands back on leave.
   */
  const kept = $derived(lineageSel === null ? null : selectLineage(model, lineageSel));
  const weighted = $derived(lit ?? kept);
  /** The model the arrow keys walk: the selection, when there is one. */
  const walkModel = $derived(kept === null ? model : withinSelection(model, kept));
  /** The selected node's lineage, drawn in cyan while it is selected. */
  const chosen = $derived(selected === null ? null : lineage(model, selected));
  /** Where the ← / → lineage buttons go; null on a side disables it. */
  const stops = $derived(lineageStops(walkModel, selected));

  function drainOf(id: string): number | null {
    const v = economics?.node_drain_usd?.[id];
    return typeof v === 'number' ? v : null;
  }

  /** A group card's drain: what its members spent, or null when none of them
   * recorded any. */
  function groupDrain(ids: readonly string[]): number | null {
    const spent = ids.map(drainOf).filter((v): v is number => v !== null);
    return spent.length ? spent.reduce((a, b) => a + b, 0) : null;
  }

  // Plain text (#2848 P13): a folded group's caption is a joined string, not
  // a `Title`, so a backtick would print as the character.
  const titleOf = $derived(new Map(snapshot.nodes.map((n) => [n.id, plainTitle(n.title)] as const)));
  /**
   * The clock this frame is read against. Every card that is not MOVING is read
   * against this and nothing else — "past windows never redraw" (#2305): a
   * settled card must not re-render once a second for a second with nothing in
   * it.
   *
   * `replay`, never `streaming`: the choice of clock is about WHICH SECOND this
   * frame is a picture of, and a dropped stream does not move a live frame into
   * the past. See `frameClock` for why reading one against its own `tick_ts`
   * would understate every age for as long as the server stays down.
   */
  const frameNow = $derived.by(() => {
    void snapshot;
    return frameClock(replay, snapshot.tick_ts, Date.now());
  });

  /** The motion of one drawn node: the server's reading when it serves one
   * (it knows a pending run's owner liveness, which no status word carries),
   * else the shared table's. A group card is a collapsed chain of DONE nodes
   * and never moves. */
  const motionFor = (node: EpicNode | null): Motion =>
    node === null ? 'settled' : servedMotion(node);

  /**
   * Whether anything on this frame may animate: it is the world as it is NOW
   * (not a replayed tick — nothing in a picture of a past second is still
   * running) AND a stream is still confirming it (a bar that keeps growing on a
   * severed channel draws seconds nothing confirmed). #2305.
   */
  const animated = $derived(streaming && !replay);

  /** Whether one card may animate: kew is executing it AND this frame is one
   * that may animate at all. */
  const movingFor = (node: EpicNode | null): boolean =>
    animated && motionFor(node) === 'executing';

  /** Whether anything on the canvas is moving, which is half of what arms the
   * clock. Read off the drawn model, not the whole snapshot: a node collapsed
   * into a done group or moved to the standalone strip has no card to
   * animate. */
  const anyMoving = $derived(model.nodes.some((n) => movingFor(n.node)));

  /**
   * The one motion clock, armed by what is on screen (#2305).
   *
   * Four of the motion rules live in this single expression: the clock runs
   * only while something is executing (and `movingFor` is false for every card
   * on a replayed or dropped frame, which is how it stops with the stream); it
   * PAUSES while a pointer is over a drawn node, so a bar never slides out from
   * under it; and `motionClock.arm` itself declines to tick at all for a reader
   * who prefers reduced motion. The teardown stops it when the canvas goes
   * away.
   *
   * The pause is armed off `lit`, never off the raw `hovered` id: that id
   * OUTLIVES the node it names (see `lit` above — a frame can remove the node
   * under the pointer, and no `pointerleave` ever comes), and a hover the
   * canvas is no longer drawing would otherwise hold the clock stopped for the
   * life of the page.
   */
  $effect(() => {
    motionClock.arm(anyMoving && lit === null);
    return () => motionClock.arm(false);
  });

  // `$state.raw`-shaped derivations: Svelte Flow takes plain arrays and keeps
  // its own lookup, so a NEW array per frame is the contract, not a mutation.
  const flowNodes = $derived<Node<DagNodeData>[]>(
    model.nodes.map((n): Node<DagNodeData> => ({
      id: n.id,
      type: 'dag',
      position: n.position,
      width: NODE_W,
      height: NODE_H,
      handles: [...HANDLES],
      selected: n.id === selected,
      zIndex: NODE_Z,
      // Not draggable and not connectable: this is a picture of a dependency
      // graph the driver owns, so nothing a pointer does here may imply the
      // operator can re-wire it.
      draggable: false,
      connectable: false,
      ariaRole: 'button',
      ariaLabel: n.group
        ? `${n.group.summary}, collapsed. Expand.`
        : `${displayId(n.id)} ${plainTitle(n.node?.title ?? '')}`,
      data: cardData(n.node, n.group, weighted !== null && !weighted.nodes.has(n.id), zoomedOut)
    }))
  );

  /** What one card draws, on the canvas or in the edge-less grid. */
  function cardData(
    node: EpicNode | null,
    group: DoneGroup | null,
    dim: boolean,
    small: boolean
  ): DagNodeData {
    return {
      node,
      group,
      caption: group ? group.ids.map((id) => titleOf.get(id) ?? id).join(' → ') : '',
      drain: group ? groupDrain(group.ids) : node ? drainOf(node.id) : null,
      frontier: node != null && frontier.has(node.id),
      motion: motionFor(node),
      live: animated,
      dim,
      // Only a moving card follows the clock; see `frameNow`.
      now: motionClock.reading(movingFor(node), frameNow),
      badges: node == null ? [] : badgesOf(node.id),
      zoomedOut: small
    };
  }

  const flowEdges = $derived<Edge<DagEdgeData>[]>(
    model.edges.map((e): Edge<DagEdgeData> => ({
      id: e.id,
      type: 'dag',
      source: e.source,
      target: e.target,
      sourceHandle: 'out',
      targetHandle: 'in',
      selectable: false,
      focusable: false,
      // Edges share channels and lanes, so whichever paints last wins where
      // they overlap: the live edge, and the lineage being looked at, go on
      // top of the quiet history.
      zIndex:
        e.style.kind === 'live' || chosen?.edges.has(e.id) || weighted?.edges.has(e.id)
          ? LIVE_EDGE_Z
          : QUIET_EDGE_Z,
      data: {
        d: e.d,
        style: e.style,
        dim: weighted !== null && !weighted.edges.has(e.id),
        lit: weighted !== null && weighted.edges.has(e.id),
        sel: chosen !== null && chosen.edges.has(e.id),
        anySelected: selected !== null
      }
    }))
  );

  const selectedFacts = $derived<EpicNode | null>(
    model.nodes.find((n) => n.id === selected)?.node ?? null
  );
  const popoverModel = $derived(
    selectedFacts
      ? nodePopover(
          selectedFacts,
          drainOf(selectedFacts.id),
          frameNow,
          freshness?.get(selectedFacts.id) ?? null
        )
      : null
  );
  /** The runs the popover leads with — the replayed frame has none to show. */
  const popoverHistory = $derived(replay ? null : runHistory(history, frameNow));

  // --- the camera -----------------------------------------------------------

  let dagEl = $state<HTMLDivElement | undefined>(undefined);
  let canvasW = $state(0);
  let canvasH = $state(0);

  /**
   * The first frame, decided ONCE, when the canvas first has a size: the flow
   * is not mounted until then, so it opens on this view rather than on a
   * default it then jumps away from. `firstFrame` fits a small epic whole and
   * centres a big one on the node that needs a person (or the `?node=` one).
   */
  let initialView = $state<Viewport | null>(null);
  /** The node the first frame was centred on, which the hint names. Cleared
   * by "Fit all", after which the hint has nothing left to say. */
  let frameFocus = $state<string | null>(null);
  /** Whether that node was the READER's — a `?node=` link — rather than the
   * needs-you node the frame opens on by default. `linked` has to be in it:
   * the page's opening default IS the needs-you node (D20), so
   * `frameFocus === selected` is true for it as well, and reading that alone
   * called the page's own default "the selected node". */
  let frameLinked = $state(false);
  /** Why the first frame is centred where it is (`firstFrame`), for the hint. */
  let frameWhy = $state<FrameReason | null>(null);
  /** The camera has moved since the first frame settled (D39), whatever moved
   * it: the reader's wheel, pinch or drag, the zoom buttons, 0, the minimap,
   * the arrow walk's reveal, or the pan that makes room for a popover. The
   * frame is then no longer "centred on" anything, so the caption drops its
   * sentence and keeps only "Fit all": after a zoom to 152% it still said
   * "Centred on #2055", and after a minimap drag or an arrow walk that panned
   * SFD-229 off screen it still named SFD-229. */
  let frameMoved = $state(false);
  /** The view the caption describes: the first frame's, and where any move
   * the first frame itself made (`framingTo`) came to rest. A move is read as
   * one by comparing against it, not by who reported it: the minimap moves
   * the camera with no input event, exactly as `DagCamera` does, and a move
   * xyflow reports without the view changing is no move at all. */
  let frameView: Viewport | null = null;
  /** Where a camera move the first frame asked for is headed, while it is in
   * flight — the pan that makes room for the popover a `?node=` link opens
   * with (go-live's SFD-229 opens 157px higher and is panned down for it).
   * Its steps are not the reader's, and the view it reaches becomes
   * `frameView` (`onmoveend`). The target, not a flag: that pan is made
   * twice, for the popover's estimated height and then its measured one, and
   * the second interrupts the first, whose end the flow reports too. */
  let framingTo: Viewport | null = null;
  /** The node whose popover the first frame opened, until the first frame
   * rests (`firstFrameRests`). Only its room pan can be the first frame's. */
  let framePopoverFor: string | null = null;

  /** The first frame has come to rest at `at`, and ends there: from now on
   * every move is a move off it, a later room pan for the same popover too
   * (a resize that makes the popover want more room panned 140px under
   * "Centred on SFD-229"). It rests where its own pan lands, when its popover
   * is measured and needs no pan, or at the reader's first input. */
  function firstFrameRests(at: Viewport): void {
    framingTo = null;
    framePopoverFor = null;
    if (!frameMoved) frameView = at;
  }
  $effect(() => {
    if (initialView !== null || canvasW <= 0 || canvasH <= 0) return;
    untrack(() => {
      const frame = firstFrame(model, canvasW, canvasH, selected, frontier);
      initialView = frame?.view ?? { x: 0, y: 0, zoom: 1 };
      frameWhy = frame?.why ?? null;
      // Seeded BEFORE the flow mounts: Svelte Flow writes pan and zoom back
      // through a bound viewport only when the binding starts out defined,
      // and the popover and the zoom readout follow the view through it.
      viewport = initialView;
      frameFocus = frame?.focus ?? null;
      frameLinked = linked && frameFocus !== null && frameFocus === selected;
      frameMoved = false;
      frameView = initialView;
      // A link to a node (`?node=`) opens on it with its popover, as the
      // artboard's `?node=SFD-232` does. The page's opening default does not:
      // a box over the graph nobody asked for is the covered card D5 and D19
      // are about.
      if (linked && selected !== null && model.nodes.some((n) => n.id === selected)) {
        framePopoverFor = selected;
        onpopover?.(true);
      }
    });
  });

  /** Why the first frame is centred where it is, as a word after the id
   * rather than a clause (#2843 P15): "Centred on #2055, the node that nee…"
   * was ellipsised at 1440 with the rail's legend beside it. */
  const FRAME_SAID: Record<FrameReason, string> = {
    selected: 'selected',
    needs_you: 'needs you',
    held: 'held',
    stalled: 'stalled',
    frontier: 'next to run',
    start: 'start'
  };

  /** The live viewport, as the flow reports it on every pan and zoom. Read
   * only: moves go through `DagCamera`. */
  let viewport = $state<Viewport | undefined>(undefined);
  const view = $derived<Viewport>(viewport ?? initialView ?? { x: 0, y: 0, zoom: 1 });
  /** Zoomed past the point a word on a card reads: the badge becomes its dot
   * variant. The same threshold the first-frame fit uses for "reads whole". */
  const zoomedOut = $derived(view.zoom < READABLE_ZOOM);

  /** One node's badges, or [] when nothing is journaled about its head. The
   * page's own clock goes in: the rebase badge is withheld for a base reading
   * too old for the server to act on (`freshness.BASE_READING_MAX_AGE_MS`). */
  function badgesOf(id: string): StalenessBadge[] {
    const row = freshness?.get(id);
    return row === undefined ? [] : badgesFor(row, frameNow);
  }
  let move = $state<CameraMove | null>(null);

  /** Every camera move the page makes. One the first frame asked for
   * (`frame`) keeps the caption; any other is a move off the first frame and
   * drops its "Centred on" (D39) — 0, the zoom buttons, the arrow walk's
   * reveal, a popover's pan for room, Fit all. A move to where the camera
   * already is (Zoom in at the zoom ceiling, 0 on the node 0 framed) moves
   * nothing and drops nothing. */
  function goTo(next: Viewport, duration = 200, frame = false): void {
    if (frame) framingTo = next;
    else if (!sameView(next, view)) frameMoved = true;
    move = { view: next, duration };
  }

  /** Two views are one up to a rounding: half a pixel, a tenth of a percent
   * of zoom. */
  function sameView(a: Viewport, b: Viewport): boolean {
    return Math.abs(a.x - b.x) <= 0.5 && Math.abs(a.y - b.y) <= 0.5 && Math.abs(a.zoom - b.zoom) <= 0.001;
  }

  /** "Fit all" (F), and the fit button in the controls: the whole graph.
   *
   * Measured once the change that asked for it has landed (#2782, D25): the
   * same click relabels the done toggle and drops the first frame's caption,
   * and either can re-lay the rail out below the node area. A fit against
   * the size bound BEFORE that is a fit for a node area that may no longer
   * be there, whose bottom row the rail then covers, so the node area is
   * read off the DOM after `tick()`, not off the `canvasH` its resize
   * observer has yet to update. */
  function fitAll(): void {
    if (canvasW <= 0 || canvasH <= 0 || model.width <= 0) return;
    frameFocus = null;
    void tick().then(() => {
      const floor = dagEl?.querySelector<HTMLElement>('.floor');
      const w = floor?.clientWidth ?? canvasW;
      const h = floor?.clientHeight ?? canvasH;
      if (w <= 0 || h <= 0 || model.width <= 0) return;
      goTo(fitAllView(model, w, h), 260);
    });
  }

  function zoomStep(factor: number): void {
    if (canvasW <= 0 || canvasH <= 0) return;
    goTo(zoomBy(view, factor, canvasW, canvasH), 160);
  }

  /** The flow reports every move: the reader's drag, wheel and pinch with an
   * input event, and the minimap's drag and wheel without one — the map
   * moves the camera through `setViewportConstrained`/`scaleTo`, exactly as
   * `DagCamera` does. So a move is read off the view it reaches, not off
   * the event: the minimap dragged the frame away and zoomed it to 152%
   * under "Centred on SFD-229". The steps of a move the first frame made
   * are skipped, unless the reader's own input interrupts it; and a move
   * that leaves the view where it was (a re-render, a resize) changes
   * nothing. */
  function onmove(event: MouseEvent | TouchEvent | null, at: Viewport): void {
    if (frameMoved || frameView === null || (framingTo !== null && event === null)) return;
    if (!sameView(at, frameView)) frameMoved = true;
  }

  /** Where a move the first frame made comes to rest is the first frame. An
   * end short of its target is the interrupted step before a re-aim. */
  function onmoveend(event: MouseEvent | TouchEvent | null, at: Viewport): void {
    if (framingTo === null || event !== null || !sameView(at, framingTo)) return;
    firstFrameRests(at);
  }

  /** The reader's input ends the first frame where the camera stands. A
   * press interrupts the first frame's pan short of its target, and a click
   * that does not move reports no move at all, so without this `framingTo`
   * stayed set for good and `onmove` skipped every later minimap move: a
   * click on SFD-229 mid-pan, then the minimap's wheel to 132%, left
   * "Centred on SFD-229". */
  function onmovestart(event: MouseEvent | TouchEvent | null, at: Viewport): void {
    if (event !== null) firstFrameRests(at);
  }

  const cardBox = (n: FlowNode<unknown>): Box => ({
    x: n.position.x,
    y: n.position.y,
    width: n.width,
    height: n.height
  });

  /** 0 — the selected node, at a readable zoom rather than a fitted one. A
   * move off the first frame like any other (`goTo`): the caption kept
   * "Centred on #2055" over another node at another zoom (D39). */
  function zoomTo(id: string): void {
    const at = model.nodes.find((n) => n.id === id);
    if (!at || canvasW <= 0 || canvasH <= 0) return;
    const b = cardBox(at);
    goTo(viewFor({ x: b.x - 120, y: b.y - 80, width: b.width + 240, height: b.height + 160 }, canvasW, canvasH, 1, 1.25));
  }

  // Walking the graph with the keyboard follows the selection: a node that
  // has gone off screen is panned back into view, with the least movement.
  // The margin is the one the frame on screen was fitted with: a whole-graph
  // frame (first frame or Fit all) keeps only the rim, and judging its
  // top-row cards against the cropped frame's larger margin panned a picture
  // the operator had just asked for (review round 7). Either way it includes
  // the minimap's corner when there is a map, so the card the keyboard walks
  // to is not revealed under it. A reveal moves the camera off the first
  // frame, so the caption drops "Centred on" (`goTo`, D39); a step to a card
  // already in view moves nothing and keeps it.
  $effect(() => {
    const id = selected;
    untrack(() => {
      const at = model.nodes.find((n) => n.id === id);
      if (!at || initialView === null || canvasW <= 0) return;
      const pad = padClearOfMinimap(
        frameFocus === null ? FIT_PAD : FRAME_PAD,
        model,
        canvasW,
        canvasH
      );
      const next = reveal(cardBox(at), view, canvasW, canvasH, pad);
      if (next) goTo(next);
    });
  });

  /** The minimap earns its corner only when the graph is too big to read
   * whole in the canvas (the brief's rule) — not for four cards a little
   * under full size, where it only covers one of them. */
  const overflows = $derived(hasMinimap(model, canvasW, canvasH));
  const zoomPct = $derived(Math.round(view.zoom * 100));

  // --- selection ------------------------------------------------------------

  function setFocus(focus: { selected: string | null; popover: boolean }): void {
    onselect?.(focus.selected);
    onpopover?.(focus.popover);
  }

  /** A click on a node: expand a collapsed chain, or select and open. The
   * canvas takes the keyboard focus either way, so the keys in the legend
   * work straight after a click. */
  function onnodeclick({ node }: { node: Node }): void {
    dagEl?.focus({ preventScroll: true });
    const group = model.nodes.find((n) => n.id === node.id)?.group;
    if (group) {
      expanded = [...expanded, group.id];
      return;
    }
    copied = false;
    setFocus(selectFocus(node.id));
  }

  /** A click on the background drops the selection outright — the brief puts
   * it beside the SECOND Esc, not the first. */
  function onpaneclick(): void {
    setFocus(NO_FOCUS);
  }

  /**
   * The brief's keyboard table, over the VISIBLE model — a collapsed done
   * chain is one stop, not six.
   *
   *   Tab      next node in reading order (layer, then row)
   *   ← / →    the upstream / downstream neighbour along an edge
   *   ↑ / ↓    the next node in the same layer
   *   Enter    open the popover
   *   F        fit view
   *   0        zoom to the selected node
   *
   * Tab is handled here rather than left to the browser because the stops are
   * the graph's reading order, not the DOM order Flow renders nodes in; the
   * arrows and Enter are handled here because Svelte Flow's own keyboard moves
   * a node, which this graph does not allow.
   *
   * ESC IS NOT HERE. It is the page's, on the window, because its two steps
   * read and write both halves of the focus and the page owns one of them —
   * handled in both places, one press would run the reducer twice and clear
   * the selection the first Esc is supposed to keep.
   *
   * Only keys aimed at the ARTBOARD ITSELF are the graph's — `artboardKey`,
   * which is in `dag-keys.ts` with the rest of the keyboard table. The popover
   * renders inside the `.dag` element, so a Tab out
   * of its Copy button must reach the popover's own links rather than move
   * the graph selection.
   */
  function oncanvaskey(e: KeyboardEvent): void {
    if (!artboardKey(onArtboard(e))) return;
    graphKey(e);
  }

  /** The press as `artboardKey` reads it. The key layer listens on the window
   * (#2512), so the element the press must have landed on is named here
   * rather than read off `currentTarget`, which is the window. */
  const onArtboard = (e: KeyboardEvent) => ({
    metaKey: e.metaKey,
    ctrlKey: e.ctrlKey,
    altKey: e.altKey,
    target: e.target,
    currentTarget: dagEl
  });

  /**
   * The same keys when nothing has the focus at all (it is on the body) but a
   * node IS selected — a `?node=` link lands that way, since the router puts
   * the focus back on the body once the page has loaded. Without this the
   * legend's keys did nothing on a linked node until it was clicked.
   *
   * TAB IS NOT ONE OF THEM — `strayKey` keeps it, and says why: from the body
   * Tab's job is to move the focus INTO the page, including into this canvas,
   * and preventDefaulting it there left the whole page unreachable from the
   * keyboard for as long as a node was selected.
   *
   * With nothing selected only ← and → are answered (`strayWithoutSelection`):
   * they land on the first node, so a fresh load can be walked from the keys
   * the legend names (#2780).
   */
  function onwindowkey(e: KeyboardEvent): void {
    if (!strayKey(e, document.body) || (selected === null && !strayWithoutSelection(e.key))) return;
    graphKey(e);
  }

  /**
   * A pointer click on a canvas control hands the keyboard focus back to the
   * artboard once the control has acted (#2780). Left on the button, the
   * graph's keys did nothing after Zoom in, Fit view or the done toggle:
   * `artboardKey` answers only presses on the artboard itself. A keyboard
   * activation (`detail` 0) keeps the focus where the operator put it, so a
   * tabbed-to control can be pressed again.
   */
  function handBack(e: MouseEvent): void {
    if (e.detail === 0) return;
    if (!(e.target instanceof Element) || e.target.closest('button') === null) return;
    dagEl?.focus({ preventScroll: true });
  }

  // The graph's keys on the console's key layer (#2512), for as long as the
  // canvas is mounted. One press is on the artboard or on the body, never
  // both, so at most one of the two answers it.
  $effect(() =>
    registerShortcuts(
      bindShortcuts('dag', DAG_KEYS, (e) => {
        oncanvaskey(e);
        onwindowkey(e);
      })
    )
  );

  /** The legend: the graph's keys and the page's Esc, as registered. */
  const LEGEND = legendOf([...DAG_KEYS, ...EPIC_KEYS]);

  /**
   * Move the selection to `id` from the keyboard. A collapsed done chain is a
   * card the page cannot select (`?node=` admits only epic nodes), so landing
   * on one dropped the selection and ended the walk (review round 7). Landing
   * on a group now does what a click does, opening it, and selects the member
   * the move reaches first: the chain's first member moving forward (Tab, →,
   * ↓), its last moving back, so the walk goes on through every member.
   */
  function land(id: string, forward: boolean): void {
    const group = model.nodes.find((n) => n.id === id)?.group;
    if (!group) {
      onselect?.(id);
      return;
    }
    expanded = [...expanded, group.id];
    onselect?.(forward ? group.ids[0] : group.ids[group.ids.length - 1]);
  }

  function graphKey(e: KeyboardEvent): void {
    const at = selected;
    // Tab walks the graph's own reading order — but only once the operator is
    // IN the graph. With nothing selected it is left to the browser, because
    // `tabStep` wraps (dag-keys.ts: a canvas, not a form) and intercepting it
    // unconditionally would make the canvas a keyboard trap with no way out.
    // Esc clears the selection, and the Tab after that leaves. Two more ways
    // out, so the canvas is never a trap (review round 5): an open popover
    // gives Tab back to the browser, which walks the canvas controls and then
    // the popover's own (Close, Open in Work, the ↗ links); and a step past
    // either end of the reading order leaves the graph.
    if (e.key === 'Tab' && at) {
      if (popover) return;
      const next = tabStep(model, at, e.shiftKey ? -1 : 1, { wrap: false });
      if (next === null) return;
      e.preventDefault();
      land(next, !e.shiftKey);
      return;
    }
    // The arrows walk `walkModel`: inside the lineage selection when there is
    // one (#2544), so ← → read the lineage asked for rather than stepping out
    // of it. From a node outside the selection they land on its first node.
    const dir = ARROWS[e.key];
    const inWalk = at !== null && walkModel.nodes.some((n) => n.id === at);
    if (dir && at && inWalk) {
      const next = neighbor(walkModel, at, dir);
      if (next === null) return;
      e.preventDefault();
      land(next, dir === 'right' || dir === 'down');
      return;
    }
    if (dir && (!at || kept !== null)) {
      const first = readingOrder(walkModel)[0];
      if (first === undefined) return;
      e.preventDefault();
      land(first, true);
      return;
    }
    if (e.key === 'Enter' && at) {
      e.preventDefault();
      onpopover?.(true);
      return;
    }
    // L fills the box with the selected node's lineage, both ways (#2544);
    // pressed again on the same node it clears it.
    if ((e.key === 'l' || e.key === 'L') && at && model.nodes.some((n) => n.id === at)) {
      e.preventDefault();
      const text = formatSelection({ id: at, up: 1, down: 1 });
      onlineage?.(lineageText === text ? null : text);
      return;
    }
    if (e.key === 'f' || e.key === 'F') {
      e.preventDefault();
      fitAll();
      return;
    }
    if (e.key === '0' && at) {
      e.preventDefault();
      zoomTo(at);
    }
  }

  const ARROWS: Record<string, 'left' | 'right' | 'up' | 'down' | undefined> = {
    ArrowLeft: 'left',
    ArrowRight: 'right',
    ArrowUp: 'up',
    ArrowDown: 'down'
  };

  async function copyCommand(): Promise<void> {
    const command = popoverModel?.command ?? '';
    if (command === '') return;
    try {
      await navigator.clipboard.writeText(command);
      copied = true;
    } catch {
      // The `<code>` block is still selectable by hand: the button is a
      // convenience over text that is already there (EntryDetail's rule), so
      // a denied clipboard is not an error anybody needs to read.
      copied = false;
    }
  }

  // --- the popover's place ---------------------------------------------------

  let popW = $state(320);
  let popH = $state(290);

  /**
   * Where the popover goes: beside its card on whichever side overlaps no
   * other card and none of the canvas's own furniture (`placePopover`). In
   * canvas pixels, so it follows the node through pan and zoom. Null when the
   * node has been panned off the canvas — a popover pointing at nothing is
   * not anchored to anything.
   */
  /** The selected card and everything the popover must keep off, on screen,
   * for the view `v`. Null when the card is off the canvas. */
  function popoverScene(v: Viewport): { node: Box; cards: Box[]; furniture: Box[] } | null {
    const at = model.nodes.find((n) => n.id === selected);
    if (!at || canvasW <= 0) return null;
    const node = onScreen(cardBox(at), v);
    const offCanvas =
      node.x + node.width < 0 || node.y + node.height < 0 || node.x > canvasW || node.y > canvasH;
    if (offCanvas) return null;
    const cards = model.nodes.filter((n) => n.id !== selected).map((n) => onScreen(cardBox(n), v));
    // The toolbar, the key legend and the frame's caption are in the rail
    // below the graph (D19), and the minimap is not drawn while a popover is
    // open (D38, `minimapShown`), so there is no furniture left in the node
    // area to keep off. Keeping off the map only where a free slot allowed it
    // left the popover over the map at 1440x900 (#2977).
    return { node, cards, furniture: [] };
  }

  /**
   * Below `SHEET_BELOW_W` the popover is a bottom sheet rather than a box
   * beside its card (D5): on a phone canvas there is no side of a node that
   * a 320px panel does not cover, so it stops pretending and takes the foot
   * of the canvas, full width. Nothing is placed, nothing is panned for, and
   * no leader is drawn — a sheet points at nothing because it is not beside
   * anything.
   */
  const sheet = $derived(canvasW > 0 && canvasW < SHEET_BELOW_W);

  /** A box beside its card on a node area too short for its loose rows
   * (`POPOVER_SHORT_BELOW_H`, #3460): it draws them tighter, so the detail
   * keeps the height its fade needs. A sheet's cap is the window's, not the
   * node area's. */
  const short = $derived(!sheet && canvasH > 0 && canvasH < POPOVER_SHORT_BELOW_H);

  const placement = $derived.by(() => {
    if (!popoverModel || !popover || selected === null || sheet) return null;
    const scene = popoverScene(view);
    if (!scene) return null;
    const size = { width: popW, height: popH };
    return {
      ...placePopover(scene.node, size, scene.cards, scene.furniture, canvasW, canvasH),
      needs: selectedFacts !== null && needsYou(selectedFacts)
    };
  });

  /** The popover is on screen: a sheet, or a box placed beside its card. */
  const popoverDrawn = $derived(
    popoverModel !== null && popover && selected !== null && (sheet || placement !== null)
  );
  /** The minimap gives way to an open popover (D38): the two are the only
   * things drawn over the graph, and the map is a picture of what the reader
   * is not looking at while they read one node. */
  const minimapShown = $derived(overflows && !popoverDrawn);

  // When the popover opens and has nowhere clear to go, pan the canvas just
  // far enough that a free side of its node comes into view — the artboard's
  // popover sits "inside the canvas and clear of every card". Once per opening:
  // after that the reader's own panning is left alone.
  let roomMadeFor = $state<string | null>(null);

  /** The first frame's popover needs no pan: the first frame rests where it
   * stands — once the popover's height is its measured one, not the
   * estimate a measured box may yet overrule, and not while the first
   * frame's own pan is still on its way (that pan's end rests it). */
  function restUnpanned(own: boolean): void {
    if (!own || framingTo !== null) return;
    const drawn = dagEl?.querySelector<HTMLElement>('.pop-at');
    if (drawn && drawn.clientHeight === popH) firstFrameRests(view);
  }

  $effect(() => {
    // Keyed on the popover's measured height as well as the node: the first
    // look uses an estimate, and the box it actually draws may fit where the
    // estimate did not. `roomMadeFor` keeps it to one pan per opening.
    const key = popover && selected !== null ? `${selected}:${popH}` : null;
    // Re-run once the popover can be placed at all (the flow has a view and
    // the card is on screen).
    const placeable = placement !== null;
    untrack(() => {
      // The first frame's own popover is that frame's until the first frame
      // rests: its pan for room (made for the estimated height, then the
      // measured one) is part of the first frame, and keeps the caption.
      // Closed, or moved to another node, it is not.
      if (key === null || selected !== framePopoverFor) framePopoverFor = null;
      if (key === null) {
        roomMadeFor = null;
        return;
      }
      if (roomMadeFor === key || !placeable || !placement) return;
      const own = framePopoverFor !== null && !frameMoved;
      // Clear, or a sheet (placed and panned for by nobody): no pan.
      if (placement.clear || sheet) restUnpanned(own);
      if (placement.clear) return;
      if (sheet) return;
      roomMadeFor = key;
      const scene = popoverScene(view);
      const size = { width: popW, height: popH };
      const pan = scene ? panForPopover(scene.node, size, scene.cards, scene.furniture, canvasW, canvasH) : null;
      if (pan) goTo({ x: view.x + pan.dx, y: view.y + pan.dy, zoom: view.zoom }, 200, own);
      else restUnpanned(own);
    });
  });

  /** Minimap node colour: the status mark's own, so the corner map and the
   * graph cannot disagree about what a node is. */
  function miniColor(node: Node): string {
    const facts = model.nodes.find((n) => n.id === node.id)?.node;
    return statusColor(facts ? (needsYou(facts) ? 'needs_you' : facts.status) : 'done');
  }
</script>

{#if independent}
  <!-- The edge-less epic (#2543, D27): every node a card, in a grid under one
       line that says why there is no graph. A click selects exactly as a
       click on a canvas card does, and the rail below follows it. -->
  <section class="cards" aria-label={`${snapshot.nodes.length} independent nodes`}>
    <span class="strip-head text-11"
      >{snapshot.nodes.length} independent {snapshot.nodes.length === 1 ? 'node' : 'nodes'}</span
    >
    <div class="card-grid">
      {#each snapshot.nodes as n (n.id)}
        <button
          class="grid-card"
          type="button"
          aria-pressed={selected === n.id}
          aria-label={`${displayId(n.id)} ${plainTitle(n.title)}`}
          title={statusLineText(statusSegments(n))}
          onclick={() => {
            copied = false;
            setFocus(selectFocus(n.id));
          }}
        >
          <DagNode data={cardData(n, null, false, false)} selected={selected === n.id} />
          <!-- There is no popover here, so the reasons the card leaves out
               (the driver's, and why a person is needed) ride the button. -->
          <span class="sr-only">{statusLineText(statusSegments(n))}</span>
        </button>
      {/each}
    </div>
  </section>
{:else}
<!-- The canvas fills the space it is given (the page's flex child), rather
     than reporting a height of its own: the rail below it stays in view, which
     is the whole of the brief's complaint about the 70vh box. -->
<!-- The one tab stop that stands for the whole graph: `tabindex` is what lets
     the keyboard reach a canvas whose nodes are not themselves focusable (the
     brief's own table moves the SELECTION, not the DOM focus), and
     `role="application"` is the honest name for a surface whose arrow keys
     mean something other than scrolling. -->
<!-- svelte-ignore a11y_no_noninteractive_tabindex, a11y_no_noninteractive_element_interactions -->
<div
  class="dag"
  class:picked={selectedFacts !== null}
  role="application"
  aria-label="Dependency graph. Pan by dragging, zoom with the wheel or a pinch."
  tabindex="0"
  bind:this={dagEl}
>
  <!-- The node area: the only part of the canvas cards are drawn in, and the
       geometry every fit is measured in (`canvasW`/`canvasH` are ITS size, not
       the pane's). It clips, and the chrome is in the rail below it, so no
       frame — fit, crop or pan — can put a card under the toolbar (D19). -->
  <div class="floor" bind:clientWidth={canvasW} bind:clientHeight={canvasH}>
    <!-- `deleteKey` and `selectionKey` are EMPTY LISTS, not null: Svelte Flow
         turns a null key into a shortcut trigger with no key, and its shortcut
         library then warns on every key press. No trigger at all is what "no
         delete key" means — Backspace must never drop a node of a graph the
         driver owns. -->
    {#if initialView}
      <SvelteFlow
        bind:viewport
        nodes={flowNodes}
        edges={flowEdges}
        {nodeTypes}
        {edgeTypes}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable={false}
        elevateNodesOnSelect={false}
        elevateEdgesOnSelect={false}
        edgesFocusable={false}
        nodesFocusable={false}
        disableKeyboardA11y={true}
        panOnDrag={true}
        deleteKey={[]}
        selectionKey={[]}
        minZoom={MIN_ZOOM}
        maxZoom={MAX_ZOOM}
        zoomOnScroll={true}
        zoomOnPinch={true}
        zoomOnDoubleClick={false}
        preventScrolling={true}
        proOptions={{ hideAttribution: true }}
        {onnodeclick}
        {onpaneclick}
        onnodepointerenter={({ node }: { node: Node }) => (hovered = node.id)}
        onnodepointerleave={() => (hovered = null)}
        {onmovestart}
        {onmove}
        {onmoveend}
        class="dag-flow"
      >
        <Background variant={BackgroundVariant.Dots} gap={22} size={1.2} patternColor="var(--hair)" />
        <DagCamera {move} />
        {#if minimapShown}
          <MiniMap
            position="bottom-right"
            width={MINIMAP_W}
            height={MINIMAP_H}
            nodeColor={miniColor}
            nodeBorderRadius={3}
            pannable
            zoomable
          />
        {/if}
      </SvelteFlow>
    {/if}

    {#if popoverModel && popover && selected !== null && (sheet || placement)}
      {#if placement?.leader}
        <svg class="leader" aria-hidden="true"
          ><line
            x1={placement.leader.x1}
            y1={placement.leader.y1}
            x2={placement.leader.x2}
            y2={placement.leader.y2}
            stroke={placement.needs ? 'var(--accent)' : 'var(--text-4)'}
            stroke-width="1.5"
            stroke-dasharray="3 3"
          /></svg
        >
      {/if}
      <div
        class="pop-at"
        class:sheet
        style:left={sheet ? undefined : `${placement?.x ?? 0}px`}
        style:top={sheet ? undefined : `${placement?.y ?? 0}px`}
        bind:clientWidth={popW}
        bind:clientHeight={popH}
      >
        <NodePopover
          model={popoverModel}
          history={popoverHistory}
          {copied}
          {sheet}
          {short}
          oncopy={copyCommand}
          onclose={() => onpopover?.(false)}
        />
      </div>
    {/if}
  </div>

  <!-- The canvas's rail: the toolbar bottom-left as the artboard draws it, the
       first frame's caption beside the control that answers it ("Fit all"),
       and the keyboard legend at the quiet end. It is a row BELOW the node
       area rather than a bar floating in it, which is what makes "nothing
       covers a card" true of every frame and not only of the fitted ones
       (D19, and the 1024x700 browser pass that found two cards under it). -->
  <div class="bar">
    <!-- `handBack` gives the focus back to the artboard after a pointer click
         on any control here, so the graph's keys work straight after it. -->
    <!-- svelte-ignore a11y_click_events_have_key_events, a11y_interactive_supports_focus -->
    <div class="controls" role="toolbar" aria-label="Canvas controls" onclick={handBack}>
      <button type="button" aria-label="Zoom in" title="Zoom in" onclick={() => zoomStep(1.2)}>
        <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true"
          ><path d="M7 2v10M2 7h10" stroke="currentColor" stroke-width="1.6" /></svg
        >
      </button>
      <button type="button" aria-label="Zoom out" title="Zoom out" onclick={() => zoomStep(1 / 1.2)}>
        <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true"
          ><path d="M2 7h10" stroke="currentColor" stroke-width="1.6" /></svg
        >
      </button>
      <button type="button" aria-label="Fit view" title="Fit all (F)" onclick={fitAll}>
        <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"
          ><path d="M1.5 5V1.5H5M9 1.5h3.5V5M12.5 9v3.5H9M5 12.5H1.5V9" /></svg
        >
      </button>
      <!-- A text control, not an icon (#2560): it says what the canvas is
           doing and how much it folded, and says why when it cannot. -->
      <button
        type="button"
        class="done-toggle"
        class:on={showDone}
        aria-pressed={showDone}
        disabled={!foldable}
        title={!foldable
          ? 'No finished chain can fold here'
          : showDone
            ? 'Collapse finished chains'
            : 'Show every done node'}
        onclick={toggleDone}
      >
        <!-- Disabled, it says why rather than "Done · shown" in the disabled
             tint, which read as a state it could leave (D39, DESIGN.md's
             `dag-canvas`). -->
        {!foldable ? 'No finished chain can fold here' : showDone ? 'Done · shown' : `Done · collapsed (${foldable})`}
      </button>
      <!-- The lineage arrows (#2533): the ← / → keys as buttons, so the move
           the legend names can be made with a pointer, and on a phone, where
           the legend is not drawn. Each lands on the adjacent node along an
           edge and is disabled where the lineage ends. -->
      <button
        type="button"
        aria-label="Upstream in lineage"
        title={stops.up === null ? 'No upstream node' : 'Upstream node (←)'}
        disabled={stops.up === null}
        onclick={() => stops.up !== null && land(stops.up, false)}
      >
        <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"
          ><path d="M12 7H2.5M6 3 2 7l4 4" /></svg
        >
      </button>
      <button
        type="button"
        aria-label="Downstream in lineage"
        title={stops.down === null ? 'No downstream node' : 'Downstream node (→)'}
        disabled={stops.down === null}
        onclick={() => stops.down !== null && land(stops.down, true)}
      >
        <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"
          ><path d="M2 7h9.5M8 3l4 4-4 4" /></svg
        >
      </button>
      <span class="zr" aria-label="Zoom">{zoomPct}%</span>
    </div>
    {#if frameFocus !== null}
      <!-- The first frame is a crop, and it says so: which node it is centred
           on, and the one key that shows everything. -->
      <!-- Once the reader has moved the frame it is "Fit all" alone: it is
           no longer centred on anything (D39). Below 1024 the sentence goes
           and a needs-you frame keeps its word beside the diamond, which
           alone read as a mark with nothing to say (D39). -->
      <div class="hint" data-frame-hint>
        {#if !frameMoved && frameLinked}
          <span class="said">Centred on {displayId(frameFocus)} · {FRAME_SAID.selected}</span>
        {:else if !frameMoved && frameWhy === 'needs_you'}
          <span class="dia" aria-hidden="true"></span>
          <span class="said keep"
            ><span class="lead">Centred on {displayId(frameFocus)}{SEP}</span>{FRAME_SAID.needs_you}</span
          >
        {:else if !frameMoved}
          <span class="said">Centred on {displayId(frameFocus)} · {FRAME_SAID[frameWhy ?? 'start']}</span>
        {/if}
        <button
          type="button"
          onclick={(e) => {
            fitAll();
            handBack(e);
          }}>Fit all <span class="kbd">F</span></button
        >
      </div>
    {/if}
    <div class="keys" aria-label="Keyboard">
      {#each LEGEND as hint (hint.label)}
        <span class:opt={hint.wide}
          >{#each hint.keys as key (key)}<span class="kbd">{keyGlyph(key)}</span>{/each}{hint.label}</span
        >
      {/each}
    </div>
  </div>
</div>

<!-- Standalone done nodes: work that is finished and depends on nothing, so
     it has no line to trace. They are a list, not geometry, so they stay
     outside the canvas — its own labelled region, because "8 done" is a claim
     of its own. -->
{#if split.strip.length}
  <div class="strip" role="group" aria-label={`No dependencies · ${split.strip.length} done`}>
    <span class="strip-head text-11"
      >No dependencies · <span class="strip-count">{split.strip.length} done</span></span
    >
    <div class="strip-grid">
      {#each split.strip as n (n.id)}
        <button
          class="strip-item"
          class:selected={selected === n.id}
          type="button"
          aria-pressed={selected === n.id}
          aria-label={`${displayId(n.id)} ${plainTitle(n.title)} — ${statusWord(n.status)}`}
          title={plainTitle(n.title)}
          onclick={() => {
            copied = false;
            setFocus(selectFocus(n.id));
          }}
        >
          <StatusMark kind={n.status} size={10} label={statusWord(n.status)} />
          <span class="nid">{displayId(n.id)}</span>
          <span class="strip-title"><Title text={n.title} /></span>
          <span class="sr-only">{statusSegments(n)[0].text}</span>
        </button>
      {/each}
    </div>
  </div>
{/if}
{/if}

<style>
  /* Svelte Flow draws nothing inside a parent with no height, and a percentage
     height only resolves when every ancestor's height is definite — on the epic
     page it was not, so the canvas rendered at 1360×0 and the graph was
     invisible. The canvas is the flex child that grows into the page's column
     instead, with a floor so it can never collapse to nothing again. */
  .dag {
    position: relative;
    display: flex;
    flex-direction: column;
    flex: 1 1 0;
    min-height: 360px;
    width: 100%;
    overflow: hidden;
    background: var(--ink);
  }

  /* Wherever the rail has folded (below `xl`, RAIL_TRACKS.fourUp) the canvas
     keeps DESIGN.md's 400px floor (D16): the page's own scroller takes the
     difference, and at 390 a 360px canvas under a phone's header was ~300px
     of graph above the fold. */
  @media (max-width: 1279.98px) {
    .dag {
      min-height: 400px;
    }
  }

  /* The node area — the canvas proper. A positioned flex child with a definite
     height for the flow to fill (C3), and the one box cards are drawn in: it
     clips at its own foot, so a card panned down does not reach the rail
     below, and `canvasH` is ITS height, which is the height every fit is
     computed against. */
  .floor {
    position: relative;
    flex: 1 1 0;
    min-height: 0;
    overflow: hidden;
  }

  /* Design review c9: `.dag`'s floor holds the control bar too, so at
     1366x650 the graph itself was ~307px, too short to read (and ~305px on a
     390x844 phone). In a laptop window 600-799px tall, and on a phone, the
     node area keeps 360px of its own, and `.dag` grows to hold it and the bar
     whole (`min-content`, which is above the 400px folded floor too): the
     page's scroller takes the difference and the panes below move down.

     Not from 800px up on a desktop: at 900px and over DESIGN.md's Grid allows
     no page scroll (a 360px node area overran 1440x900 by 8-25px), and a
     desktop window resized between 800 and 900 keeps its camera only while the
     canvas takes what is left (the D39 caption check). Not under 600px either,
     where the node popover's action row must stay on screen (#2977): a 360px
     area put it below a 1440x560 window. */
  @media (max-width: 1023.98px), (min-height: 600px) and (max-height: 799.98px) {
    .dag {
      min-height: min-content;
    }

    .floor {
      min-height: 360px;
    }
  }

  /* The artboard's focus ring: 2px cyan. The canvas takes it too, because it
     is the one tab stop that stands for the whole graph — until a node is
     selected, when the ring is the selected card's (DagNode) and one is
     enough. */
  .dag:focus-visible:not(.picked) {
    outline: 2px solid var(--accent-2);
    outline-offset: -2px;
  }

  /* Svelte Flow's own tokens, bound to this console's. Without these the
     canvas brings its own light-mode greys onto the factory ink. */
  .dag :global(.dag-flow) {
    --xy-background-color: var(--ink);
    --xy-edge-stroke-default: var(--edge-done);
    --xy-minimap-background-color: var(--ink-raised);
    --xy-minimap-mask-background-color: color-mix(in oklab, var(--ink) 60%, transparent);
    --xy-minimap-mask-stroke-color: var(--text-2);
    --xy-minimap-mask-stroke-width: 1;
    /* Absolute inside `.floor`, not `height: 100%` of it (design review C2,
       C3): a percentage height only resolves against a definite one, and
       this flow is the one DESIGN.md's "don't let a component's height depend
       on a percentage of an indefinite parent" was written about. `.floor` is
       the positioned flex child that has the height; the flow fills it. */
    position: absolute;
    inset: 0;
  }

  /* Edges and nodes fade out at the canvas rim (the artboard's mask), so a
     card or a line that continues off screen reads as "continues", not as a
     card cut in half at the edge. */
  .dag :global(.svelte-flow__renderer) {
    -webkit-mask-image:
      linear-gradient(to right, transparent 0, #000 40px, #000 calc(100% - 40px), transparent 100%),
      linear-gradient(to bottom, transparent 0, #000 24px, #000 calc(100% - 24px), transparent 100%);
    -webkit-mask-composite: source-in;
    mask-image:
      linear-gradient(to right, transparent 0, #000 40px, #000 calc(100% - 40px), transparent 100%),
      linear-gradient(to bottom, transparent 0, #000 24px, #000 calc(100% - 24px), transparent 100%);
    mask-composite: intersect;
  }

  /* A node's own box carries no chrome — the card inside it draws all of it
     (DagNode) — so Flow's default border, padding and shadow are off. */
  .dag :global(.svelte-flow__node) {
    border: 0;
    padding: 0;
    background: none;
    box-shadow: none;
    border-radius: 5px;
    cursor: pointer;
  }

  /* Edges are a picture, never a target: a stroke that took the pointer
     would swallow the click meant for the card beside it. */
  .dag :global(.svelte-flow__edge),
  .dag :global(.svelte-flow__edges) {
    pointer-events: none;
  }

  .dag :global(.svelte-flow__handle) {
    /* The handles exist so `getEdgePosition` can answer; nothing connects
       here, so they are not drawn and not hit-tested. */
    opacity: 0;
    pointer-events: none;
  }

  .dag :global(.svelte-flow__minimap) {
    margin: 16px;
    border: 1px solid var(--hair-strong);
    border-radius: 4px;
    overflow: hidden;
  }

  /* The first frame's caption, in the rail rather than floating over the
     graph (D19): a bar over a card read as text printed over text, and no
     fill could stop it covering the card underneath. It keeps its own box so
     it still reads as a note about the frame and not as another control, and
     it is the one thing in the rail allowed to shrink — the sentence clamps
     before "Fit all" is pushed anywhere. */
  .hint {
    display: flex;
    min-width: 0;
    align-items: center;
    gap: 10px;
    /* 34px, which is the tallest thing the rail holds at any width — so the
       rail's height does not move when the caption appears, and neither does
       the node area whose height every fit was computed from. */
    height: 34px;
    padding: 0 6px 0 12px;
    border: 1px solid var(--hair-box);
    border-radius: 4px;
    background: var(--ink-raised);
    font-family: var(--font-mono);
    font-size: 13px;
    color: var(--text-2);
    white-space: nowrap;
  }

  /* The sentence, which may be clamped on a narrow canvas rather than push
     "Fit all" off the edge (D5). */
  .said {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .hint button {
    flex-shrink: 0;
    display: inline-flex;
    align-items: center;
    gap: 6px;
    height: 22px;
    padding: 0 8px;
    border: 1px solid var(--hair-strong);
    border-radius: 3px;
    font-family: var(--font-mono);
    font-size: 13px;
    color: var(--text-1);
    cursor: pointer;
  }

  .hint button:hover {
    border-color: var(--text-4);
  }

  .dia {
    display: inline-block;
    flex-shrink: 0;
    width: 7px;
    height: 7px;
    background: var(--accent);
    transform: rotate(45deg);
  }

  /* The rail: a row at the foot of the canvas pane, outside the node area, so
     nothing in it can cover a card at any canvas size (D19). It takes its own
     height from the 32px controls; the graph gets the rest. */
  .bar {
    display: flex;
    flex-shrink: 0;
    align-items: center;
    gap: 12px;
    padding: 8px 16px 10px;
    /* A hairline says what it is: a band at the foot of the canvas, like the
       strip below it, and not a bar floating in the graph. */
    border-top: 1px solid var(--hair);
  }

  .controls {
    display: flex;
    flex-shrink: 0;
    align-items: stretch;
    overflow: hidden;
    border: 1px solid var(--hair-strong);
    border-radius: 4px;
    background: var(--ink-raised);
  }

  .controls button {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 34px;
    height: 32px;
    border-left: 1px solid var(--hair);
    color: var(--text-1);
    cursor: pointer;
  }

  .controls button:first-child {
    border-left: 0;
  }

  .controls button:hover {
    background: var(--ink-selected);
  }

  .controls button.on {
    color: var(--accent-2);
  }

  .controls button.done-toggle {
    width: auto;
    padding: 0 10px;
    font-family: var(--font-mono);
    font-size: 11px;
    white-space: nowrap;
    font-variant-numeric: tabular-nums;
  }

  .controls button:disabled {
    color: var(--text-4);
    cursor: default;
  }

  .controls button:disabled:hover {
    background: none;
  }

  .controls button:focus-visible {
    outline: 2px solid var(--accent-2);
    outline-offset: -2px;
  }

  .zr {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 48px;
    border-left: 1px solid var(--hair);
    font-family: var(--font-mono);
    font-size: 11px;
    color: var(--text-2);
    font-variant-numeric: tabular-nums;
  }

  /* The keyboard legend, at the quiet end of the rail: reference, read once
     and then ignored, so it sits furthest from the graph it describes. */
  .keys {
    display: flex;
    flex-shrink: 0;
    margin-left: auto;
    align-items: center;
    gap: 12px;
    height: 30px;
    padding: 0 12px;
    border: 1px solid var(--hair-box);
    border-radius: 4px;
    background: var(--ink-raised);
    font-family: var(--font-mono);
    font-size: 11px;
    color: var(--text-3);
    white-space: nowrap;
    /* A reading of the keyboard, never a target (C12). */
    pointer-events: none;
  }

  .keys > span {
    display: inline-flex;
    align-items: center;
    gap: 5px;
  }

  /* The optional key ("0 zoom to node") gives its room to the hint up to
     1500, past the 1440 design width (#2843 P15): with the lineage arrows in
     the toolbar, go-live-2026-09's "Centred on SFD-229 · needs you" was given
     191px of the 240 it needs and was ellipsised at 1440. */
  @media (max-width: 1500px) {
    .keys .opt {
      display: none;
    }
  }

  /* Below 1024 there is no keyboard to hint at and no room to hint in
     (DESIGN.md Responsive Behavior, D5): the legend goes, the hint keeps its
     diamond, its "needs you" and its "Fit all", and the controls have the corner to
     themselves. The minimap is dropped by `hasMinimap`, not here, so the
     popover's furniture agrees with what is drawn. */
  @media (max-width: 1023px) {
    .keys,
    .hint .said:not(.keep),
    .hint .lead {
      display: none;
    }

    /* "Fit all" stays a control; its F cap is a key hint (#3053 N6). */
    .hint .kbd {
      display: none;
    }

    /* The zoom readout goes with them: it is a reading, not a control, and
       its 48px is what the two lineage arrows (#2533) need for the toolbar
       and the hint to share a 390px row without scrolling it sideways. */
    .zr {
      display: none;
    }

    /* What is left of the hint is a thumb target (D25); the box it sits in
       keeps the 34px the rail is sized for. */
    .hint button {
      height: 32px;
    }
  }

  /* Below 640 the toolbar alone fills a phone's row, and the hint beside it
     ran "Fit all" off the right edge (#2788 N7): the rail wraps, and the hint
     takes a second row under the controls. */
  @media (max-width: 639px) {
    .bar {
      flex-wrap: wrap;
      row-gap: 8px;
    }

    /* "No finished chain can fold here" is 200px of 11px mono on one line,
       and the toolbar must still fit a 390px row: the toggle takes two lines
       inside its 32px instead. */
    .controls button.done-toggle {
      max-width: 128px;
      white-space: normal;
      line-height: 13px;
      text-align: left;
    }
  }

  .kbd {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-width: 18px;
    height: 18px;
    padding: 0 5px;
    border: 1px solid var(--hair-strong);
    border-bottom-width: 2px;
    border-radius: 3px;
    font-family: var(--font-mono);
    font-size: 11px;
    line-height: 16px;
    color: var(--text-2);
  }

  .hint .kbd {
    min-width: 16px;
    height: 16px;
    font-size: 11px;
    line-height: 14px;
  }

  .leader {
    position: absolute;
    inset: 0;
    z-index: 11;
    width: 100%;
    height: 100%;
    overflow: visible;
    pointer-events: none;
  }

  /* The box beside a card is capped to the node area it is placed in, less
     the 8px rim `placePopover` keeps off each edge (`POPOVER_RIM`), and
     scrolls inside itself (`.pop`). Without the cap a needs-you popover
     taller than the node area is clamped to the top rim — all `placePopover`
     can do — and the rest of it, its links row and "Open in Work" with it, is
     clipped away by `.floor { overflow: hidden }` with no scrollbar anywhere
     to reach it; the rail under the graph (D19) took 52px off the box it has
     to fit in. A percentage of `.floor`, whose height is definite for an
     absolutely positioned child (C3's reason), so nothing is measured in
     script.
     The cap is handed to `.pop` by a column flex parent rather than clipping
     here: an `overflow` on this box would eat `.pop`'s border and the page's
     one drop shadow along with the overflow. */
  .pop-at {
    position: absolute;
    z-index: 12;
    display: flex;
    flex-direction: column;
    max-height: calc(100% - 16px);
  }

  /* The bottom sheet (D5): full width at the foot of the WINDOW, not of the
     canvas. Anchored in the canvas it opened under the rail the page pins
     below it (#2278) — at 390x700 the whole sheet was behind the rail, and at
     390x844 all but its header row — because the canvas is a short flex child
     that clips. Fixed to the viewport, its bottom edge is the window's and it
     stacks over the rail for as long as it is open. */
  .pop-at.sheet {
    position: fixed;
    right: 0;
    bottom: 0;
    left: 0;
    z-index: 40;
    max-height: 100%;
  }

  /* The edge-less grid: the canvas's own 196x92 cards, flowed in rows under
     the gutter the strip uses, so an epic with nothing to trace is still a
     page of cards rather than a blank canvas (#2543, D27). */
  .cards {
    display: flex;
    flex-shrink: 0;
    flex-direction: column;
    gap: 12px;
    padding: 12px 40px 16px;
  }

  .card-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, 196px);
    gap: 16px;
  }

  .grid-card {
    position: relative;
    display: block;
    width: 196px;
    height: 92px;
    padding: 0;
    border-radius: 4px;
    cursor: pointer;
    text-align: left;
  }

  .grid-card:focus-visible {
    outline: 2px solid var(--accent-2);
    outline-offset: 2px;
  }

  @media (max-width: 1023px) {
    .cards {
      padding: 12px 16px 16px;
    }

    .card-grid {
      grid-template-columns: minmax(0, 1fr);
    }

    .grid-card {
      width: 100%;
    }
  }

  /* The strip: "No dependencies · 8 done", a 196px label against a 4-column
     grid of 10px marks, under the canvas and inside the page gutter.
     `relative`: its items' `sr-only` labels are placed against the strip,
     not against the window far down the page, where they grew the document
     past the viewport (#3053 N2). */
  .strip {
    position: relative;
    display: grid;
    /* Its own row (#2543, D29): never squeezed under the rail. */
    flex-shrink: 0;
    grid-template-columns: 196px minmax(0, 1fr);
    /* A fixed label column AND a gap (D24): without one the label ran into
       its own first item ("8 DONE●#1117"). */
    column-gap: 24px;
    align-items: start;
    padding: 12px 40px 14px;
    border-top: 1px solid var(--hair);
  }

  .strip-head {
    min-width: 0;
    color: var(--text-3);
    line-height: 22px;
  }

  .strip-count {
    color: var(--text-2);
  }

  .strip-grid {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    row-gap: 6px;
    column-gap: 24px;
  }

  /* Phone: the strip is a list (D5). Four columns of 10px marks and clamped
     titles overprinted each other at 390px, and a label column is 196px the
     page has not got — so the label takes its own line and the items run one
     per row, in the 32px rows D25 asks for. */
  @media (max-width: 1023px) {
    .strip {
      grid-template-columns: minmax(0, 1fr);
      row-gap: 6px;
      padding: 12px 16px 14px;
    }

    .strip-grid {
      grid-template-columns: minmax(0, 1fr);
      row-gap: 0;
    }

    .strip-item {
      min-height: 32px;
    }
  }

  .strip-item {
    display: flex;
    align-items: center;
    gap: 8px;
    min-width: 0;
    white-space: nowrap;
    cursor: pointer;
    border-radius: 4px;
    text-align: left;
  }

  .strip-item.selected {
    background: var(--ink-selected);
    box-shadow: 0 0 0 1px var(--hair-box);
  }

  .strip-item:focus-visible {
    outline: 2px solid var(--accent-2);
    outline-offset: 2px;
  }

  .nid {
    font-family: var(--font-mono);
    font-size: 13px;
    color: var(--text-3);
  }

  .strip-title {
    overflow: hidden;
    font-size: 13px;
    color: var(--text-2);
    text-overflow: ellipsis;
  }

  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip-path: inset(50%);
    white-space: nowrap;
  }
</style>
