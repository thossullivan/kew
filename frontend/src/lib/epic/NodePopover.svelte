<script lang="ts">
  // The popover beside a selected DAG node (#2250, the brief's §2.3, drawn to
  // the UXEpic artboard's `.pop`).
  //
  // A renderer. `nodePopover` ($lib/epic/node-popover) decides every word,
  // every href and whether there is a link at all; nothing is composed here.
  //
  // Two things the markup itself owes the reader, which `node-popover.test.ts`
  // holds it to:
  //
  // * "Open in Work" is an IN-APP href. It is the reason the node itself no
  //   longer links anywhere: the graph's way to a node's runs is a kew route,
  //   not a GitHub tab thrown over the panel the operator just opened.
  // * every anchor that leaves kew says so in its own text, with the `↗` the
  //   rest of the console uses, and carries `target="_blank"` and
  //   `rel="noreferrer"`.
  //
  // The command is a real `<code>` block as well as a Copy button, for
  // `EntryDetail`'s reason: the button is a convenience over text that is
  // already there, never the only copy of the command. It reads in `--text-1`
  // like every other command (#2540 D5): it is text to read and copy, not a
  // live or selected thing, so it is not cyan.
  //
  // `model.command` is `attention.command` verbatim — the server's own filled,
  // `fill_command`-guarded line. `''` draws no command box and no button at
  // all, which is the only honest reading where no kew command clears the
  // wait: a Copy button over a line that exits 2 is worse than no button.
  import { TAP_INLINE } from '$lib/factory/classes';
  import { Button, Command } from '$lib/ui';
  import FreshnessBadge from '$lib/ui/FreshnessBadge.svelte';
  import StatusMark from '$lib/ui/StatusMark.svelte';
  import Title from '$lib/ui/Title.svelte';
  import { plainTitle } from '$lib/factory/names';
  import { statusWord } from './render';
  import type { PopoverHistory, PopoverModel } from './node-popover';

  let {
    model,
    history = null,
    copied = false,
    sheet = false,
    short = false,
    oncopy = undefined,
    onclose = undefined
  }: {
    model: PopoverModel;
    /** The node's runs (#2544), which the box leads with; null draws none. */
    history?: PopoverHistory | null;
    /** True for the moment after a copy landed, so the button can say so. */
    copied?: boolean;
    /** Drawn as a bottom sheet rather than a box beside a card: below 1024
     * the canvas is too narrow for a 320px panel to sit clear of the node it
     * is about (D5, DESIGN.md Responsive Behavior). Full width, square along
     * the bottom, and every control at the 32px a thumb needs (D25). */
    sheet?: boolean;
    /** Beside a card on a node area too short for the loose rows
     * (`POPOVER_SHORT_BELOW_H`, #3460): the rows are drawn tighter and the
     * sentence takes two lines, so the detail keeps its fade's room. */
    short?: boolean;
    oncopy?: () => void;
    onclose?: () => void;
  } = $props();

  const needs = $derived(model.mark === 'needs_you');
  const hasPr = $derived(model.external.some((l) => l.key === 'pr'));

  /** Whether the body has more below its fold (#2843 P1): a body cut at the
   * cap gives no cue on its own, and the review-stale disclosure at its foot
   * hid there unseen. Read on every scroll and every resize of the body or of
   * what it holds; the attachment reads `model` and `history`, so a new node
   * re-attaches it and observes the children that node drew. */
  let more = $state(false);

  /** The node whose detail the body last showed. The popover is not keyed by
   * node, so the scroll box outlives a switch to another one; a new node's
   * detail starts at its top, not where the last node's was left (#2892).
   * The same node re-read by a poll keeps the reader's place. */
  let scrolledFor = '';

  /** The shortest body that fades: the 28px band (`.pbody.more`) plus the
   * tallest control the body holds, the sheet's 32px Copy (`h-8`; 28px
   * beside a card), clear above it. A body opened only a line or a control
   * high would be faded through and show nothing whole; there the fade is
   * left off and the scrollbar is the cue (#2892: at 28 + 28 the sheet's Copy
   * could never leave the fade in a 56–59px body). The body's height is read
   * unrounded (#2948): `clientHeight` rounds, so a 59.5px body read as 60
   * and faded with 31.5px above the band. */
  const FADE_ROOM = 28 + 32;

  /** The action row's links: a thumb's 32px on the sheet, 30px beside a card,
   * and 24px in the short box (#3469), where every pinned row is a fixed
   * height. */
  const linkH = $derived(sheet ? 'h-8' : short ? 'h-6' : 'h-[30px]');

  /** The node, title and sentence the reader unfolded with "more" (#2938;
   * the title since #3056). Keyed by all three, so another node, or a new
   * title or sentence on this one, opens folded; the same ones re-read by a
   * poll stay as the reader left them. One control unfolds both: a second
   * "more" beside the first would ask the reader which cut is which. */
  let openFor = $state('');
  const whyKey = $derived(`${model.id}\n${model.title}\n${model.reason}`);
  const whyOpen = $derived(openFor === whyKey);

  /** One id per instance (#3056): a fixed `pop-why` named the first popover's
   * sentence from the second's control when two were in one document. */
  const uid = $props.id();
  const titleId = `${uid}-title`;
  const whyId = `${uid}-why`;

  /** Whether the folded sentence runs past its clamp (#2938): the line clamp
   * cuts it with no other way in for a thumb or a keyboard, so a cut sentence
   * gets a "more" control and a whole one gets none. Read only while folded —
   * unfolded, nothing is cut — and on every resize, as the window's height
   * changes the clamp and its width the wrap. */
  let whyCut = $state(false);
  /** The same reading of the title (#3056), held to two lines (one in a short
   * window) with, before, no way into the rest but a pointer's hover. */
  let titleCut = $state(false);
  /** A node with no sentence keeps the last one's reading; it has none to cut. */
  const whyMore = $derived((whyCut && model.reason !== '') || titleCut);
  /** What the control unfolds: the title, and the sentence where there is one. */
  const controls = $derived(model.reason !== '' ? `${titleId} ${whyId}` : titleId);

  function clamp(el: HTMLElement, cut: (is: boolean) => void, open: boolean, ..._drawn: unknown[]) {
    if (open) return;
    const read = () => cut(el.scrollHeight > el.clientHeight + 1);
    const sizes = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(read);
    sizes?.observe(el);
    read();
    return () => sizes?.disconnect();
  }

  function fade(el: HTMLElement, id: string, ..._drawn: unknown[]) {
    if (id !== scrolledFor) {
      el.scrollTop = 0;
      scrolledFor = id;
    }
    const read = () =>
      (more =
        el.getBoundingClientRect().height >= FADE_ROOM && el.scrollHeight - el.scrollTop - el.clientHeight > 1);
    const sizes = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(read);
    sizes?.observe(el);
    // A `display: contents` child (`.histwrap`) draws no box, so its own
    // resize never fires: its children are observed in its place.
    for (const child of el.children) {
      if (getComputedStyle(child).display === 'contents') for (const inner of child.children) sizes?.observe(inner);
      else sizes?.observe(child);
    }
    el.addEventListener('scroll', read, { passive: true });
    read();
    return () => {
      sizes?.disconnect();
      el.removeEventListener('scroll', read);
    };
  }
</script>

<div
  class="pop"
  class:sheet
  class:short
  role="dialog"
  aria-label={`${model.id} ${plainTitle(model.title)}`}
  aria-describedby={model.reason !== '' ? whyId : undefined}
>
  <div class="head">
    <StatusMark kind={model.mark} size={needs ? 14 : 12} label={statusWord(model.mark)} />
    <span class="pid">{model.id}</span>
    <span class="grow"></span>
    <span class="kbd" aria-hidden="true">esc</span>
    <button class="close" type="button" aria-label="Close (Esc)" onclick={onclose}>×</button>
  </div>

  <p
    id={titleId}
    class="ptitle"
    class:open={whyOpen}
    title={plainTitle(model.title)}
    {@attach (el) => clamp(el, (is) => (titleCut = is), whyOpen, model.title)}
  >
    <Title text={model.title} />
  </p>

  <!-- The needs-you label and sentence are fixed between the title and the
       detail (#2892, R2): what a person owes is never scrolled away or faded
       out. The sentence is held to three lines (two in a short window), the
       whole of it in its title, so a long hold reason cannot push the action
       row off the box; the command and the note under it scroll with the
       detail. -->
  {#if needs || model.reason !== '' || whyMore}
    <div class="ny" class:open={whyOpen}>
      <!-- A cut sentence's "more" sits at the end of the label's row (#2938),
           so the fold costs the detail no height of its own. A cut title's is
           the same control (#3056); with no label and no sentence it is the
           row's only word. -->
      {#if needs || whyMore}
        <div class="nyhead">
          {#if needs}
            <span class="cap"><span class="dia" aria-hidden="true"></span>Needs you</span>
          {/if}
          {#if whyMore}
            <button
              class="whymore"
              type="button"
              aria-expanded={whyOpen}
              aria-controls={controls}
              onclick={() => (openFor = whyOpen ? '' : whyKey)}>{whyOpen ? 'less' : 'more'}</button
            >
          {/if}
        </div>
      {/if}
      {#if model.reason !== ''}
        <p
          id={whyId}
          class="why"
          class:open={whyOpen}
          title={model.reason}
          {@attach (el) => clamp(el, (is) => (whyCut = is), whyOpen, model.reason)}>{model.reason}</p
        >
      {/if}
    </div>
  {/if}

  <!-- Only the detail scrolls (#2783, N12; #2892, R2): the action row is the
       popover's foot, pinned, and the needs-you sentence sits above, so the
       fade at this box's foot can never cover either. The review-stale
       disclosure leads, so its summary line is above the fold however long
       the command under it wraps; then the command the sentence asks for. -->
  <div class="pbody" class:more data-more={more} {@attach (el) => fade(el, model.id, model, history)}>
    {#if model.badges.length > 0 || model.baseNote !== ''}
      <!-- The badge CARRIES ITS CAUSE (#2303): one line per cause, with the
           commit it is about linked out where the row named one. Nothing is
           composed here — `freshness.ts` decides every sentence and every href.
           Every badge's summary line comes before any cause (#2892): a
           wrapped rebase cause ahead of "review stale" pushed that summary
           below the fold. The causes follow in the badges' own order, each
           list named for its badge. -->
      <div class="fresh">
        {#if model.badges.length > 0}
          <div class="fsum">
            {#each model.badges as badge (badge.kind)}
              <FreshnessBadge {badge} />
            {/each}
          </div>
        {/if}
        {#each model.badges as badge (badge.kind)}
          <ul class="causes" aria-label={badge.label}>
            {#each badge.causes as cause, i (i)}
              <li>
                {cause.text}{#if cause.href !== ''}
                  <!-- A word in a sentence, so the phone press target is
                       vertical padding (D25's TAP_INLINE), which does not move
                       the line it sits in. -->
                  <a href={cause.href} target="_blank" rel="noreferrer" class={TAP_INLINE}
                    >{cause.commit} <span aria-hidden="true">↗</span></a
                  >{/if}
              </li>
            {/each}
          </ul>
        {/each}
        {#if model.baseNote !== ''}
          <p class="causes">{model.baseNote}</p>
        {/if}
      </div>
    {/if}

    {#if model.command !== ''}
      <div class="cmd">
        <Command command={model.command} />
        <Button
          variant="needs-you"
          size="sm"
          aria-label="Copy command"
          onclick={oncopy}
          class={`shrink-0 px-2.5 ${sheet ? 'h-8' : 'h-7'}`}>{copied ? 'Copied' : 'Copy'}</Button
        >
      </div>
    {/if}
    {#if model.note !== ''}
      <p class="note">{model.note}</p>
    {/if}

    {#if history}
      <!-- Run history before the status line (#2544), the way a Dagster
           asset leads with its materializations: each run's outcome and cost,
           linked to its run page in-app. `runHistory` decides every word and
           href. Each run's verdict is a status word, so it carries
           `data-status-word`. A served verdict can be a sentence and a phone
           has no hover, so it is never cut: it wraps inside its own column,
           and the cost and age keep their room in the row (#2951). -->
      <!-- `data-tour`: the demo's spend tour reads the node's runs and their total here (#3149).
           The wrapper draws no box (`display: contents`) and is there so the
           highlighted element's parent is not the scrolling `.pbody`: driver.js
           sets `overflow: hidden` on a highlighted element's scrolling parent,
           which would freeze the popover's body mid-tour. -->
      <div class="histwrap">
        <div class="hist" data-history data-tour="node-runs">
          <span class="hsum">{history.summary}</span>
          {#each history.runs as run (run.id)}
            <a class="hrun" href={run.href}>
              <span class="hverdict" data-status-word>{run.verdict}</span>
              <span class="num">{run.cost}</span>
              {#if run.ago !== ''}<span class="hago">{run.ago}</span>{/if}
            </a>
          {/each}
        </div>
      </div>
    {/if}

    <!-- The status word is its own element, apart from its age, so it can
         carry `data-status-word`, the hook the 390 sweep reads (#2898). A
         word too long for the box ends in an ellipsis, as StatusMark's does
         (#2848); the full word is its hover text. -->
    <div class="state">
      <span class="word" data-status-word title={model.word}>{model.word}</span>
      {#if model.age !== ''}
        <span class="sep">·</span><span class="num">{model.age}</span>
      {/if}
      {#if model.drain !== ''}
        <span class="sep">·</span><span class="num">{model.drain}</span> drain
      {/if}
    </div>
    {#if model.recorded !== ''}
      <p class="rec">{model.recorded}</p>
    {/if}
  </div>

  <!-- One row where it fits (DESIGN.md `needs-you-block`: the secondary links
       "in one quiet row"). Each mark sits 4px from its word, not the Button's
       8. The arrows are drawn in Plex Mono on every platform (`lib/fonts/`,
       #3478). The row may wrap, and the short box (`.pop.short`) is sized for
       it wrapped: there each link is 24px, the `more` control's height beside
       a card, so two rows leave the detail its `FADE_ROOM` under a two-line
       title (#3469: at 30px they left it 51.6px). -->
  <div class="links">
    {#each model.links as link (link.key)}
      <Button
        variant="factory"
        size="sm"
        href={link.href}
        class={`gap-1 px-2.5 ${linkH}`}
        >{link.label} <span aria-hidden="true">→</span></Button
      >
    {/each}
    {#each model.external as link (link.key)}
      <Button
        variant="factory"
        size="sm"
        href={link.href}
        target="_blank"
        rel="noreferrer"
        class={`gap-1 border-transparent px-1.5 text-text-2 ${linkH}`}
        >{link.label} <span aria-hidden="true">↗</span></Button
      >
    {/each}
    {#if !hasPr}
      <span class="none" title="No pull request yet">no PR</span>
    {/if}
  </div>
</div>

<style>
  /* The freshness block: a badge, then the cause under it. `--text-3` labels
     and `--text-2` prose, which is the weight the rest of the popover's
     secondary lines already take. */
  .fresh {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  /* The summary lines, side by side and wrapping: each badge is its own
     line of the summary, ahead of every cause (#2892). */
  .fsum {
    display: flex;
    flex-wrap: wrap;
    gap: 4px 12px;
  }

  .causes {
    margin: 0;
    padding: 0;
    list-style: none;
    font-size: 13px;
    line-height: 1.4;
    color: var(--text-2);
  }

  /* A link out is text-2 with ↗ (#2787 N9): cyan is live, selected, focus
     and in review, and a commit is none of those. */
  .causes a {
    margin-left: 4px;
    font-family: var(--font-mono);
    color: var(--text-2);
    text-decoration: none;
  }

  .causes a:hover {
    color: var(--text-1);
    text-decoration: underline;
  }

  .pop {
    display: flex;
    flex-direction: column;
    gap: 8px;
    /* The artboard's 300, plus room for "Open in Work →  Issue ↗  PR #1234 ↗"
       on one line. */
    width: 320px;
    /* Detail long enough to outgrow the box it is given scrolls inside it
       rather than being clipped away unreachably: beside a card
       `.pop-at` caps the height to the node area (DagView), as a sheet the
       rule below caps it to the window, and either way the links row stays
       reachable. `min-height: 0` is what lets the flex item shrink to the cap
       at all — the default `auto` holds a flex item at its content height,
       and the overflow would be clipped by `.floor` as before. The scroll is
       `.pbody`'s, not this box's: a box that scrolled whole kept its links
       row below the fold, cut off at its own bottom edge (#2783, N12). */
    min-height: 0;
    overflow: hidden;
    padding: 12px 14px;
    border: 1px solid var(--hair-strong);
    /* DESIGN.md popover: raised ink, hair-strong border, the 11px panel
       radius, and the only drop shadow on the page. */
    border-radius: 11px;
    background: var(--ink-raised);
    box-shadow:
      0 1px 2px rgb(0 0 0 / 50%),
      0 18px 48px -12px rgb(0 0 0 / 85%);
    text-align: left;
    cursor: default;
  }

  /* The bottom sheet: the width is the window's, the bottom corners are the
     window's edge, and long detail scrolls inside it rather than
     growing up the screen.
     The cap is 70vh of the WINDOW, which is what the sheet is anchored to:
     DagView fixes it to the viewport (`.pop-at.sheet`) rather than to the
     canvas, because a sheet inside the canvas opened behind the rail pinned
     under it and was clipped at the canvas's foot — its links row cut off
     unreachably, with no scrollbar to reach it. */
  .pop.sheet {
    width: 100%;
    max-height: 70vh;
    border-bottom: 0;
    border-radius: 11px 11px 0 0;
  }

  /* Only the sheet's body scrolls (#3053 N2): a scroll that reaches the end
     of the detail stops there rather than carrying on into the page under
     the sheet and taking the nav with it. */
  .pop.sheet .pbody,
  .pop.sheet .ny.open {
    overscroll-behavior: contain;
  }

  .pop.sheet .close {
    width: 32px;
    height: 32px;
  }

  .head {
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .grow {
    flex: 1;
  }

  .pid {
    font-family: var(--font-mono);
    font-size: 13px;
    font-weight: 500;
    color: var(--text-1);
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

  /* Below 1024 there is no keyboard to hint at (DESIGN.md Responsive
     Behavior): the close button stays, its "esc" cap goes (#3053 N6). It
     follows the base rule: at the same specificity the later rule wins, and
     placed before it this one lost and "esc" was still drawn at 390. */
  @media (max-width: 1023px) {
    .kbd {
      display: none;
    }
  }

  .close {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 24px;
    height: 24px;
    border-radius: 3px;
    font-size: 15px;
    line-height: 1;
    color: var(--text-3);
    cursor: pointer;
  }

  .close:hover {
    color: var(--text-1);
    background: var(--ink-track);
  }

  /* No negative margin: one pulled the title's ascenders out of the box it
     sat in, where a scrolled body clipped them (#2843 P1). Two lines at most
     at every height (#2892; one in a short window, below): the title is
     pinned, so a 1,000-character one left whole pushed the action row past
     the sheet's cap. The whole title is the element's title. */
  .ptitle {
    display: -webkit-box;
    margin: 0;
    overflow: hidden;
    font-size: 15px;
    font-weight: 500;
    line-height: 1.35;
    color: var(--text-1);
    overflow-wrap: anywhere;
    -webkit-box-orient: vertical;
    -webkit-line-clamp: 2;
    line-clamp: 2;
  }

  /* Unfolded by "more" (#3056), with the sentence: the whole title. Should
     a title outgrow the box it scrolls rather than push the action row off;
     the detail under it gives up its height first (`.ny.open + .pbody`), and
     two lines of it always show. */
  .ptitle.open {
    display: block;
    min-height: 2lh;
    flex-shrink: 1;
    overflow-y: auto;
    -webkit-line-clamp: none;
    line-clamp: none;
  }

  .state {
    display: flex;
    align-items: center;
    gap: 6px;
    font-family: var(--font-mono);
    font-size: 13px;
    color: var(--text-2);
  }

  .sep {
    color: var(--text-5);
  }

  .word {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .histwrap {
    display: contents;
  }

  .hist {
    display: flex;
    flex-direction: column;
    gap: 2px;
    font-family: var(--font-mono);
    font-size: 13px;
    color: var(--text-2);
  }

  .hsum {
    color: var(--text-3);
  }

  .hrun {
    display: flex;
    gap: 8px;
    color: var(--text-1);
    text-decoration: none;
  }

  /* A thumb's 32px on the bottom sheet (D25), as the sheet's buttons are. */
  .sheet .hrun {
    min-height: 32px;
    align-items: center;
  }

  .hverdict {
    min-width: 0;
    overflow-wrap: anywhere;
  }

  .hrun > :not(.hverdict) {
    flex-shrink: 0;
  }

  .hrun:hover .hverdict {
    text-decoration: underline;
  }

  .hago {
    color: var(--text-3);
  }

  /* The driver's reason for the status, which the card no longer prints. */
  .rec {
    margin: 0;
    font-size: 13px;
    line-height: 18px;
    color: var(--text-2);
    overflow-wrap: anywhere;
  }

  .num {
    font-variant-numeric: tabular-nums;
  }

  .ny {
    display: flex;
    flex-direction: column;
    gap: 6px;
    padding-top: 10px;
    border-top: 1px solid var(--hair);
  }

  .cap {
    display: flex;
    align-items: center;
    gap: 8px;
    font-family: var(--font-mono);
    font-size: 11px;
    font-weight: 500;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--accent);
  }

  .dia {
    display: inline-block;
    width: 7px;
    height: 7px;
    background: var(--accent);
    transform: rotate(45deg);
  }

  /* Three lines at most (#2892; two in a short window, below): the sentence
     is pinned, so a 300-character hold reason left whole would push the
     action row off a short popover. The whole sentence is the element's
     title and its text. */
  .why {
    display: -webkit-box;
    margin: 0;
    overflow: hidden;
    font-size: 13px;
    line-height: 1.4;
    color: var(--text-1);
    overflow-wrap: anywhere;
    -webkit-box-orient: vertical;
    -webkit-line-clamp: 3;
    line-clamp: 3;
  }

  /* Unfolded by "more" (#2938): the whole sentence, which the detail under
     it makes room for — the detail is the part that gives way to the cap.
     Should even that not fit (a 300-character hold at 1440x560), the block
     scrolls rather than push the action row off the box: the open sentence
     holds its whole height and hides nothing, so it is the block that
     scrolls — a paragraph left to shrink under `overflow: hidden` cut its
     last lines away unreachably (wide glyphs at 1440x560). The box keeps its
     320px: DagView places it beside its card at the size it measured, and a
     box widened to 480 beside a rim node had no clear side left, so it was
     laid over the cards next to it. */
  .why.open {
    display: block;
    flex-shrink: 0;
    overflow: visible;
    -webkit-line-clamp: none;
    line-clamp: none;
  }

  .ny.open {
    min-height: 0;
    flex-shrink: 1;
    overflow-y: auto;
  }

  .nyhead {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
  }

  /* The way into a cut sentence for a thumb and a keyboard (#2938), not only
     a pointer's hover over the title. A quiet word at the end of the label's
     row; beside a card its 24px reach into the row's gaps rather than grow
     it, and on the sheet it is a thumb's 32px (D25). No negative margin at
     the side: the unfolded block scrolls, and focus scrolled it sideways to
     bring an overhanging control in, cutting the sentence's first letters. */
  .whymore {
    height: 24px;
    margin: -5px 0 -5px auto;
    padding: 0 6px;
    border-radius: 3px;
    font-family: var(--font-mono);
    font-size: 11px;
    color: var(--text-2);
    cursor: pointer;
  }

  .whymore:hover {
    color: var(--text-1);
    background: var(--ink-track);
  }

  .sheet .whymore {
    height: 32px;
    margin: 0 0 0 auto;
  }

  /* A short window caps the box beside a card near 290px (1440x560): a
     two-line title over a three-line sentence left the detail no height at
     all. There the title
     takes one line and the sentence two, the whole of each in its title, so
     the detail under them stays open at least a Copy control high (#2892:
     a two-line title over a 300-character hold left it 24px, under the 28px
     control). The card the popover sits beside carries the title too. */
  @media (max-height: 720px) {
    .ptitle {
      -webkit-line-clamp: 1;
      line-clamp: 1;
    }

    .why {
      -webkit-line-clamp: 2;
      line-clamp: 2;
    }
  }

  /* `flex-shrink: 0` because it sits in the scrolling `.pbody` column now
     (#2892): its own `min-height` stands in for the content height a flex
     item keeps, so a wrapped command was squeezed to 38px and spilled out. */
  .cmd {
    display: flex;
    flex-shrink: 0;
    min-width: 0;
    align-items: center;
    gap: 8px;
    min-height: 38px;
    padding: 5px 4px 5px 10px;
    border: 1px solid var(--hair-box);
    border-radius: 4px;
    background: var(--ink-selected);
  }

  /* The shared `Command` (#3052 N4): wrapped only at its own spaces, never
     broken at an id's hyphen, never scrolled sideways inside the box. */
  .cmd :global(code) {
    flex: 1;
    min-width: 0;
    font-family: var(--font-mono);
    font-size: 13px;
    line-height: 17px;
    color: var(--text-1);
    user-select: all;
  }

  .note {
    margin: 0;
    font-size: 11px;
    line-height: 1.4;
    color: var(--text-3);
  }

  /* The header, the title, the needs-you label and sentence and the links
     row hold their height (#2892, R2); the detail between them — review,
     command, note and run history — is the one part that gives way to the
     cap, and scrolls when it does. */
  .head,
  .ptitle,
  .ny,
  .links {
    flex-shrink: 0;
  }

  /* The bottom padding is the last line's room (#2843 P1): with none, a body
     scrolled to its end sat its last line on the scroll box's edge and lost
     its descenders. The top padding is the first line's (#2892, R2): the
     review-stale badge leads now, and its 11px word at `line-height: 1`
     draws its glyphs past its own line box, which the scroll box would cut.
     While there is more below the fold the foot fades out, which is the cue
     that the body scrolls — unless the body is too short to hold a control
     clear of the 28px band (`FADE_ROOM`). */
  /* Never shorter than two of its own lines (D38, #2977): at `min-height: 0`
     a three-line needs-you sentence shrank the detail to about a line and a
     half, and "review stale" was cut at its foot. Two 13px lines at 1.4 plus
     the padding above and below them; the box beside a card is capped to the
     node area, so the height comes out of what the canvas gives the popover,
     never out of the action row under it. */
  .pbody {
    display: flex;
    min-height: calc(2 * 13px * 1.4 + 9px);
    flex: 0 1 auto;
    flex-direction: column;
    gap: 8px;
    padding-top: 3px;
    padding-bottom: 6px;
    overflow-y: auto;
  }

  .pbody.more {
    mask-image: linear-gradient(to bottom, #000 calc(100% - 28px), transparent);
  }

  /* A keyboard focus inside the body lifts the fade (#2948, C7-1), as
     RunEvents does: a Tabbed run link at the foot kept its ring under the
     mask. */
  .pbody.more:has(:focus-visible) {
    mask-image: none;
  }

  /* Under an unfolded sentence (#2938, `.ny.open`): flex shrinks items in
     proportion, so the detail's far larger factor has it give up all of its
     height before the sentence gives up any. */
  .ny.open + .pbody {
    flex-shrink: 1000;
  }

  .links {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 4px;
    padding-top: 10px;
    border-top: 1px solid var(--hair);
  }

  .none {
    padding: 0 6px;
    font-family: var(--font-mono);
    font-size: 13px;
    color: var(--text-3);
    white-space: nowrap;
  }

  /* The short box (#3460): beside a card on a node area under
     `POPOVER_SHORT_BELOW_H`, which a canvas at its 360px floor is at any
     window height (a 291px box). Loose, its pinned rows left the detail 44px
     whenever the action row wrapped, under the 60px its fade needs, so a
     detail that scrolled showed no cue. Tight, they give it 28px back — the
     gaps on the 4px unit, the hairline rows 6px under their rule, 2px less
     padding top and bottom — and the sentence takes two lines as in a short
     window, its "more" the way to the rest; the title keeps the window's
     clamp. Every amount here is a fixed height, so the detail's room no
     longer turns on whether the links fit one row; the links themselves are
     24px here (`linkH`), as a wrapped row of 30px ones still left a two-line
     title's detail 51.6px (#3469). Last in the sheet, after the rules it
     tightens. */
  .pop.short {
    gap: 4px;
    padding-block: 10px;
  }

  .pop.short .ny,
  .pop.short .links {
    padding-top: 6px;
  }

  .pop.short .why:not(.open) {
    -webkit-line-clamp: 2;
    line-clamp: 2;
  }
</style>
