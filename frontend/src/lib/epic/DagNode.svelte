<script module lang="ts">
  import type { EpicNode } from '$lib/api';
  import type { Motion } from '$lib/factory/motion';
  import type { StalenessBadge } from '$lib/freshness';
  import type { DoneGroup } from './flow-model';

  /** Everything the card draws, handed over as the Flow node's `data`.
   * In the module script so `DagView` can import it: an instance script owns
   * props, not types. */
  export type DagNodeData = {
    /** The epic node, or null on a collapsed-done-chain group card. */
    node: EpicNode | null;
    /** The collapsed chain, or null on an ordinary card. */
    group: DoneGroup | null;
    /** On a group card: what its members are, as one clamped line. */
    caption: string;
    /** The node's drain (a group's: its members' total), or null when none
     * is recorded. */
    drain: number | null;
    /** On the driver's ready frontier. Drawn as a dashed box, never a pulse:
     * queued work does not move (#2305). */
    frontier: boolean;
    /** This node's motion ($lib/factory/motion). ONLY `executing` animates. */
    motion: Motion;
    /** Whether the frame this card is drawn in is the world as it is NOW,
     * rather than a replayed tick or a stream that has dropped. History keeps
     * its status mark and its word; it does not move (#2305). */
    live: boolean;
    /** Dimmed because some OTHER node's lineage is being hovered. */
    dim: boolean;
    /** The clock the status row's age is read against. */
    now: number;
    /** This node's staleness badges (#2303), or [] when nothing moved. */
    badges: StalenessBadge[];
    /** Too far out for a word to read: the badge draws as its dot variant. */
    zoomedOut: boolean;
  };
</script>

<script lang="ts">
  // One node on the Svelte Flow canvas (#2250): the UXEpic artboard's card —
  // a bordered 196x92 box holding the status mark, id and drain, a two-line
  // clamped title, and the status row. Every node is a card, whatever its
  // status; the needs-you card is the one with the magenta border, wash and
  // halo, which is the strongest signal on the canvas and is rationed to the
  // nodes a person has to act on.
  //
  // NOTHING IN HERE IS A LINK. The old node wrapped its label in an
  // `<a target="_blank">` to the issue or PR and selected on the same click,
  // so one click both opened the panel and threw a GitHub tab over it. The
  // only way out of the graph is the popover's own "Issue ↗" / "PR ↗", and
  // `dag-view.test.ts` holds this component to having no `<a>` at all.
  //
  // No `<Handle>` either: the handles are declared as data on the Flow node
  // (`flowNodes` in DagView), which is what lets this component server-render
  // on its own — `Handle` reads the flow store out of context and throws
  // outside a `<SvelteFlow>`.
  //
  // Nothing inside the card takes pointer events: the whole card is ONE click
  // target, Svelte Flow's node wrapper, so a click on the title, the status
  // row or the mark selects the node exactly as a click on its padding does.
  import FreshnessBadge from '$lib/ui/FreshnessBadge.svelte';
  import StatusMark from '$lib/ui/StatusMark.svelte';
  import ReadyRing from '$lib/epic/ReadyRing.svelte';
  import Title from '$lib/ui/Title.svelte';
  import { plainTitle } from '$lib/factory/names';
  import { barPct } from '$lib/factory/motion';
  import { statusShape } from '$lib/factory/status';
  import { markStatus, needsYou, statusLineText } from './attention';
  import { badgeLabelFits, badgesOnRow, cardStatus, cardWords, idRange } from './card';
  import { fmtUSD } from './format';
  import { displayId, statusWord } from './render';

  let { data, selected = false }: { data: DagNodeData; selected?: boolean } = $props();

  const n = $derived(data.node);

  /**
   * Whether this card moves. One reading, used by the halo and the bar, so the
   * two can never disagree about whether the node is running (#2305).
   *
   * Two facts, both required: kew is EXECUTING this node, and the frame is the
   * world as it is now. A replayed tick fails the second — the node was running
   * at that instant and is not running now, and a breathing halo over a
   * near-full bar would be the console claiming otherwise about work that
   * stopped days ago. What replay keeps is the status mark and the status row,
   * which say `in_progress` and for how long AS AT the tick (`data.now` is the
   * tick's own instant there, not the wall clock).
   *
   * The motion rules in full: only `executing` moves, queued never pulses, the
   * bar transitions over the clock's own second, a finished node settles over
   * `SETTLE_MS`, and `prefers-reduced-motion` switches every one of them off —
   * in CSS, below, so the reader's setting is honoured whatever the clock does.
   */
  const moving = $derived(data.motion === 'executing' && data.live);
</script>

{#if data.group}
  <!-- A collapsed, fully done upstream chain: "5 done". One stop in the
       reading order and one click to expand, which is the whole point of
       collapsing it. -->
  <div class="card grp" class:dim={data.dim} class:sel={selected}>
    <span class="stack" aria-hidden="true"></span><span class="stack s2" aria-hidden="true"
    ></span>
    <div class="r1">
      <StatusMark kind="done" size={12} label="done" />
      <span class="gn">{data.group.summary}</span>
      <span class="grow"></span>
      {#if data.drain != null}<span class="drain">{fmtUSD(data.drain)}</span>{/if}
    </div>
    <div class="tt">{data.caption}</div>
    <div class="sl split">
      <span>{idRange(data.group.ids)}</span><span class="exp">expand ▾</span>
    </div>
  </div>
{:else if n}
  {@const mark = markStatus(n)}
  {@const needs = needsYou(n)}
  {@const segs = cardStatus(n, data.now)}
  {@const badged = badgesOnRow(n, data.badges.length, data.zoomedOut)}
  <div
    class="card"
    class:needs
    class:broken={!needs && statusShape(n.status).meaning === 'failed'}
    class:queued={!needs && statusShape(n.status).meaning === 'queued'}
    class:quiet={!needs && ['done', 'cancelled'].includes(statusShape(n.status).meaning)}
    class:dim={data.dim}
    class:sel={selected}
  >
    <div class="r1">
      <span class="mk">
        <StatusMark kind={mark} size={needs ? 14 : 12} label={statusWord(mark)} />
        {#if moving}
          <!-- The one thing that moves: kew is running code against this node
               right now. It used to be the ready FRONTIER that pulsed, which
               put the canvas's only animation on work that had not started —
               Dagster pulses its queued items and #2305 is the decision not to.
               -->
          <span class="halo" aria-hidden="true"></span>
        {:else if data.frontier}
          <!-- Ready: the driver can dispatch this node now. The same ring,
               STILL, and not in the live colour — a node waiting its turn is
               not moving, and cyan is reserved for what is (DESIGN.md). -->
          <ReadyRing size={18} centred />
        {/if}
      </span>
      <span class="id">{displayId(n.id)}</span>
      {#if !badged}
        <!-- Zoomed out there is no room for a word, so the badge is its mark
             alone — the same shape, the same colour rule (#2303). So it is on
             a needs-you card, whose status row is all its words (#3051). -->
        {#each data.badges as badge (badge.kind)}
          <FreshnessBadge {badge} dot class="shrink-0" />
        {/each}
      {/if}
      <span class="grow"></span>
      {#if data.drain != null}<span class="drain">{fmtUSD(data.drain)}</span>{/if}
    </div>
    <!-- A div, never an anchor: see the header. The full title rides the
         `title` attribute, so a clamped one still reads whole on hover. -->
    <div class="tt" title={plainTitle(n.title)}><Title text={n.title} /></div>
    <div
      class="sl"
      class:badged
      title={statusLineText(segs)}
      data-needs-you-row={needs ? '' : undefined}
    >
      <!-- On the needs-you card the whole row is magenta (DESIGN.md
           dag-node-card-needs-you: HELD · NEEDS YOU · 18D). -->
      <!-- One status word, then "needs you", then the age — each a whole
           word that wraps out of sight when the row is full, never cut
           mid-word (#2543, D30). The reasons are the popover's. The first
           word is the status word, so it carries `data-status-word`, the
           hook the 390 sweep reads (#2898). -->
      <span class="segs"
        >{#each cardWords(segs) as w, i (i)}<span
            class="w"
            data-status-word={i === 0 ? '' : undefined}
            >{#if w.lead}<span style:color={needs ? 'var(--accent)' : w.lead.color}>{w.lead.text}</span
              >{/if}<span style:color={needs ? 'var(--accent)' : w.word.color}>{w.word.text}</span></span
          >{/each}</span
      >
      <!-- The staleness badge sits on the status row, where the state is
           already read (#2303). Zoomed out it is the dot in `r1` instead. -->
      {#if badged}
        <!-- A status word too long to share the row with a label keeps the
             row, and the badge is its mark (#2543, D30); so are two badges,
             whose two labels fit beside no word at all. -->
        {#each data.badges as badge (badge.kind)}
          <FreshnessBadge {badge} dot={data.badges.length > 1 || !badgeLabelFits(n.status)} />
        {/each}
      {/if}
    </div>
    {#if moving}
      <!-- The running bar, on the card's bottom edge. NOT a progress bar: its
           width is an asymptote on elapsed time (`barPct`), so it always grows
           and never arrives — kew cannot know how far through its work an agent
           is, and a bar that implied it would invent the one number the
           operator most wants. It slides rather than steps because the clock
           ticks once a second and the transition is exactly that long. -->
      <span
        class="bar"
        style:width="{barPct(n.status_since, data.now)}%"
        aria-hidden="true"
      ></span>
    {/if}
  </div>
{/if}

<style>
  /* DESIGN.md dag-node-card (the UXEpic artboard's .node): 196x92, a 1px
     hair-box border on the raised ink, 4px corners, 10px/12px padding; id row,
     two title lines, status row. Every node is a card, every time. */
  .card {
    position: relative;
    box-sizing: border-box;
    display: flex;
    flex-direction: column;
    gap: 3px;
    width: 100%;
    height: 100%;
    padding: 10px 12px;
    border: 1px solid var(--hair-box);
    border-radius: 4px;
    background: var(--ink-raised);
    text-align: left;
    transition:
      opacity 120ms ease-out,
      border-color 120ms ease-out;
  }

  /* One click target: see the header. */
  .card,
  .card * {
    pointer-events: none;
  }

  :global(.svelte-flow__node:hover) > .card {
    border-color: var(--text-4);
  }

  .r1 {
    display: flex;
    flex-shrink: 0;
    align-items: center;
    gap: 7px;
    height: 16px;
  }

  .mk {
    position: relative;
    display: inline-flex;
  }

  .grow {
    flex: 1;
  }

  /* `data-13-mono`: an id is a thing somebody compares in a column, and 12px
     was not one of the five sizes (D18). The row's own height is fixed, so
     the card's geometry does not move with it. */
  .id {
    font-family: var(--font-mono);
    font-size: 13px;
    line-height: 16px;
    color: var(--text-3);
    white-space: nowrap;
  }

  .drain {
    font-family: var(--font-mono);
    font-size: 11px;
    color: var(--text-3);
    font-variant-numeric: tabular-nums;
  }

  /* Two lines, then an ellipsis. `-webkit-box` + `line-clamp` is the only
     way to ellipsize a WRAPPED block. */
  .tt {
    display: -webkit-box;
    flex-shrink: 0;
    height: 34px;
    overflow: hidden;
    -webkit-box-orient: vertical;
    -webkit-line-clamp: 2;
    line-clamp: 2;
    font-size: 13px;
    line-height: 17px;
    color: var(--text-1);
    overflow-wrap: anywhere;
    text-overflow: ellipsis;
  }

  .sl {
    display: block;
    flex-shrink: 0;
    height: 14px;
    margin-top: auto;
    overflow: hidden;
    font-family: var(--font-mono);
    font-size: 11px;
    line-height: 14px;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--text-3);
    white-space: nowrap;
    text-overflow: ellipsis;
  }

  .sl.split {
    display: flex;
    justify-content: space-between;
  }

  /* With a badge the row becomes a flex line, and the badge keeps its full
     width, because a half-drawn cause is worse than no badge. */
  .sl.badged {
    display: flex;
    align-items: center;
    gap: 6px;
  }

  /* The words are one 14px line of whole words: one that does not fit wraps
     to a second line the row's height hides, separator and all, so the row
     never reads `IN REVIE…` (#2543, D30). The status word is first and only
     it may shrink — its ellipsis is the last resort for a word wider than
     the whole card. */
  .segs {
    display: flex;
    flex-wrap: wrap;
    min-width: 0;
    height: 14px;
    overflow: hidden;
  }

  .segs .w {
    flex-shrink: 0;
    white-space: pre;
  }

  .segs .w:first-child {
    flex-shrink: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  /* Work nobody has armed: a dashed box on the bare ink. */
  .card.queued {
    border-style: dashed;
    border-color: var(--hair-strong);
    background: var(--ink);
  }

  /* Finished work is quieter but keeps its box (DESIGN.md). */
  .card.quiet .tt {
    color: var(--text-2);
  }

  .card.broken {
    border-color: var(--danger);
  }

  /* The one card asking for a person (DESIGN.md dag-node-card-needs-you):
     magenta border on a magenta halo fill — rationed, so it is the first
     thing the eye lands on. The halo is laid over the raised ink rather than
     used bare, so the canvas's dot grid does not show through the card. */
  .card.needs {
    border-color: var(--accent);
    background: linear-gradient(var(--halo-magenta), var(--halo-magenta)), var(--ink-raised);
  }

  :global(.svelte-flow__node:hover) > .card.needs {
    border-color: var(--accent);
  }

  .card.needs .id {
    color: var(--text-1);
    font-weight: 500;
  }

  .card.needs .tt {
    color: var(--text-1);
  }

  /* Selected: the artboard's 2px cyan focus ring. */
  .card.sel {
    outline: 2px solid var(--accent-2);
    outline-offset: 2px;
  }

  /* Hover lineage (Dagster's reading): the hovered node's cone stays lit and
     everything else steps back. */
  .card.dim {
    opacity: 0.38;
  }

  /* The collapsed chain: a card with two more stacked behind it. */
  .card.grp {
    border-color: var(--hair-strong);
    overflow: visible;
  }

  .card.grp .tt {
    color: var(--text-2);
  }

  .stack {
    position: absolute;
    inset: 4px -4px -4px 4px;
    z-index: -1;
    border: 1px solid var(--hair-box);
    border-radius: 4px;
    background: var(--ink-raised);
  }

  .stack.s2 {
    inset: 8px -8px -8px 8px;
    opacity: 0.6;
  }

  .gn {
    font-family: var(--font-mono);
    font-size: 13px;
    line-height: 16px;
    font-weight: 500;
    color: var(--text-1);
  }

  .exp {
    color: var(--text-2);
    letter-spacing: 0.06em;
  }

  /* THE MOTION RULES (#2305). Only an `executing` card draws either of the two
     things below, and `prefers-reduced-motion` switches both off at the bottom
     of this block — the elements stay in the DOM either way, so a server
     render and a client render agree whatever the reader has asked for. */

  /* A cyan ring breathing out of the mark of a node kew is running code
     against. This was the ready-frontier pulse until #2305, i.e. the canvas's
     only animation was on work that had not started. */
  .halo {
    position: absolute;
    top: 50%;
    left: 50%;
    width: 0;
    height: 0;
    border-radius: 999px;
    border: 1.5px solid var(--accent-2);
    animation: dag-halo 1.8s ease-out infinite;
  }

  @keyframes dag-halo {
    0% {
      width: 10px;
      height: 10px;
      margin: -5px 0 0 -5px;
      opacity: 0.9;
    }
    100% {
      width: 28px;
      height: 28px;
      margin: -14px 0 0 -14px;
      opacity: 0;
    }
  }

  /* The running bar: a 2px cyan rule on the card's bottom edge whose width
     grows. The transition is exactly the clock's tick (MOTION_TICK_MS), which
     is what makes it slide instead of stepping once a second. */
  .bar {
    position: absolute;
    bottom: 0;
    left: 0;
    height: 2px;
    border-radius: 0 0 0 4px;
    background: var(--accent-2);
    transition: width 1s linear;
  }

  /* A node that finishes SETTLES to done rather than snapping: its title ink
     eases to the quiet one over ~400ms (`SETTLE_MS`). The halo and the bar are
     gone by then — the frame that reports `done` stops drawing them.

     The TITLE and nothing else, because the title is the only thing `.quiet`
     changes: the box keeps its own border and ink (DESIGN.md), and a
     `.card.quiet { transition: ... }` would — being the more specific selector
     — also replace `.card`'s 120ms hover and dim fade with a 400ms one on every
     finished card, slowing the reader's own hover on most of the canvas to
     settle something that does not move. */
  .card.quiet .tt {
    transition: color 400ms ease-out;
  }

  @media (prefers-reduced-motion: reduce) {
    .halo {
      animation: none;
      width: 18px;
      height: 18px;
      margin: -9px 0 0 -9px;
      opacity: 0.7;
    }

    /* The bar still SAYS how long this has been running; it just stops
       sliding there. The clock stops too (`motionClock.arm`), so the width it
       lands on is the one the last frame reported. */
    .bar {
      transition: none;
    }

    .card.quiet .tt {
      transition: none;
    }
  }
</style>
