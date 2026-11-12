<script lang="ts">
  /**
   * #2542 — the one model control: a listbox of `/api/models`, grouped by
   * runner, with each row's price, the lanes this project routes to it, kew's
   * shipped lane default (worded apart) and lifecycle.
   *
   * A lookup, not a text field. Every id this component hands `onpick` has
   * been through `pickModel` against the list it is showing, so the value a
   * caller writes is always a catalog id. A retired model is on the list only
   * while it is the configured value, and it is flagged there.
   *
   * #2846 — `current` is the model in force when the control opened. Its row
   * carries a check and the word "current", the list opens scrolled to it,
   * and while rows lie below the list's edge that edge fades out.
   * `currentLabel` is that word: the launchpad's says "runs now" (#2895).
   *
   * #2895 — the shaded row is the value, else the current model, never the
   * first row: a list with nothing picked shaded its top row, and the shade
   * read as the choice. The row the arrows are on is not shaded: while the
   * list has focus it carries a dashed outline, a cursor that cannot read as
   * a second choice. A move is forgotten when the value changes from outside.
   * A retired `current` stays on the list, flagged, so the model in force
   * always has its row.
   *
   * #2945 — a name draws at most two lines (the whole name is its title),
   * and the list's height ends halfway down a row, so a cut row shows that
   * more models lie below.
   *
   * Keyboard: the listbox is one tab stop; arrows, Home and End move the
   * active row, and Enter or Space picks it. A click picks the row clicked.
   */
  import { untrack } from 'svelte';
  import type { CatalogModel } from '$lib/api';
  import {
    groupByRunner,
    laneLine,
    lifecycleLabel,
    offeredModels,
    orderedIds,
    pickModel,
    priceLabel
  } from '$lib/model-picker';

  let {
    models,
    value = '',
    current = '',
    currentLabel = 'current',
    label = 'Model',
    disabled = false,
    onpick
  }: {
    models: readonly CatalogModel[];
    value?: string;
    current?: string;
    currentLabel?: string;
    label?: string;
    disabled?: boolean;
    onpick: (id: string, model: CatalogModel) => void;
  } = $props();

  const uid = $props.id();
  const offered = $derived(offeredModels(models, value, current));
  const groups = $derived(groupByRunner(offered));
  const ids = $derived(orderedIds(groups));
  // The row moved to, and the value it was moved under: a value set from
  // outside (a reset) drops the move.
  let active = $state<{ id: string; under: string } | null>(null);
  // The shaded row: the value, else the current model.
  const shaded = $derived(pickModel(offered, value) ?? pickModel(offered, current));
  // The row the arrows are on: the one moved to, else the shaded one, else the first.
  const activeId = $derived(
    active !== null && active.under === value && ids.includes(active.id)
      ? active.id
      : (shaded ?? ids[0] ?? null)
  );

  const optionDomId = (id: string) => `model-${uid}-${ids.indexOf(id)}`;

  let listEl = $state<HTMLElement | null>(null);
  // The fade's height in px (2rem): reveal() keeps the arrow-key row above it.
  const FADE = 32;
  // Rows lie below the visible box: the bottom edge fades while this holds.
  let more = $state(false);

  function measure() {
    if (listEl) more = listEl.scrollTop + listEl.clientHeight < listEl.scrollHeight - 1;
  }

  /** #2894 R3 — the y the sticky group header covering the list's top edge
   * reaches down to; the list's own top when no header covers it. */
  function coverLine(el: HTMLElement) {
    const top = el.getBoundingClientRect().top + el.clientTop;
    let line = top;
    for (const head of el.querySelectorAll<HTMLElement>('[data-runner-head]')) {
      const box = head.getBoundingClientRect();
      if (box.top <= top + 0.5 && box.bottom > top) line = Math.max(line, box.bottom);
    }
    return line;
  }

  /** The y of the list's bottom edge. */
  function floorOf(el: HTMLElement) {
    return el.getBoundingClientRect().top + el.clientTop + el.clientHeight;
  }

  /** #2894 R3 — scroll up until no option is cut by the sticky header: an
   * option straddling the header's bottom is revealed whole. Every pass moves
   * up, so it ends at the top at the latest, where no header is stuck.
   * #2945 — a move up that would push `keep` below the list's bottom scrolls
   * the cut option wholly under the header instead, so `keep` stays in view. */
  function uncut(el: HTMLElement, keep: HTMLElement) {
    for (let pass = 0; pass < 4 && el.scrollTop > 0; pass++) {
      const line = coverLine(el);
      const cut = [...el.querySelectorAll<HTMLElement>('[role="option"]')]
        .map((row) => row.getBoundingClientRect())
        .find((box) => box.top < line - 0.5 && box.bottom > line + 0.5);
      if (!cut) return;
      const up = line - cut.top;
      if (keep.getBoundingClientRect().bottom + up > floorOf(el) + 0.5) {
        el.scrollTop += cut.bottom - line;
        return;
      }
      el.scrollTop = Math.max(0, el.scrollTop - up);
    }
  }

  /** #2945 N12 — end the list halfway down a row when rows overflow it, so
   * the cut row shows that more lie below. Starts from the default height
   * (max-h-72) each time; the row the default edge falls on (else the first
   * below it) is the one cut, at its middle. That row lies below every row
   * wholly in view, so the edge never moves onto one of them. */
  function size(el: HTMLElement) {
    el.style.maxHeight = '';
    if (el.scrollHeight > el.clientHeight + 1) {
      const top = el.getBoundingClientRect().top + el.clientTop;
      const edge = floorOf(el);
      const row = [...el.querySelectorAll<HTMLElement>('[role="option"]')]
        .map((r) => r.getBoundingClientRect())
        .find((box) => box.bottom > edge + 0.5);
      if (row) {
        const mid = (row.top + row.bottom) / 2 - top;
        el.style.maxHeight = `${Math.round(mid + el.offsetHeight - el.clientHeight)}px`;
      }
    }
    measure();
  }

  // On open, centre the current model (else the value) in the list's own box,
  // then move up so the row above it is not cut by the sticky header, then
  // size the list to end halfway down a row. Only the list's scrollTop and
  // height move, so the dialog and page around it stay put.
  $effect(() => {
    const el = listEl;
    if (!el) return;
    untrack(() => {
      const id = pickModel(offered, current) ?? pickModel(offered, value);
      const row =
        id === null ? null : el.querySelector<HTMLElement>(`#${CSS.escape(optionDomId(id))}`);
      if (row) {
        const top = row.getBoundingClientRect().top - el.getBoundingClientRect().top + el.scrollTop;
        el.scrollTop = Math.max(0, top - (el.clientHeight - row.offsetHeight) / 2);
        uncut(el, row);
      }
      size(el);
    });
  });

  // #2945 N12 — a new width rewraps the rows: size the list again.
  $effect(() => {
    const el = listEl;
    if (!el) return;
    const resize = () => size(el);
    window.addEventListener('resize', resize);
    return () => window.removeEventListener('resize', resize);
  });

  // #2894 C2 — re-read the fade whenever the list or a row changes size (a
  // resize, a rotation, a row that rewraps), as NodePopover's fade does, not
  // only on open and on scroll. It reads `groups`, so a new list re-observes
  // the groups that list drew.
  $effect(() => {
    const el = listEl;
    void groups;
    if (!el || typeof ResizeObserver === 'undefined') return;
    const sizes = new ResizeObserver(measure);
    sizes.observe(el);
    for (const child of el.children) sizes.observe(child);
    return () => sizes.disconnect();
  });

  function pick(candidate: string | null) {
    if (disabled) return;
    const id = pickModel(offered, candidate);
    if (id === null) return;
    active = { id, under: id };
    onpick(id, offered.find((m) => m.id === id)!);
  }

  /** #2894 R3 — scroll the list the least that shows `row` whole between the
   * sticky header's bottom and the list's bottom: `scrollIntoView` takes the
   * list's top for its edge and leaves the row under the header. Only the
   * list's scrollTop moves; a pass that cannot move it ends the walk.
   * #2945 C7-1 — the bottom edge is 2rem up, above the fade, so the arrow-key
   * row is never dimmed; at the list's end the fade is gone. */
  function reveal(el: HTMLElement, row: HTMLElement) {
    for (let pass = 0; pass < 4; pass++) {
      const box = row.getBoundingClientRect();
      const line = coverLine(el);
      const bottom = floorOf(el) - FADE;
      const by =
        box.top < line - 0.5 ? box.top - line : box.bottom > bottom + 0.5 ? box.bottom - bottom : 0;
      const before = el.scrollTop;
      if (by !== 0) el.scrollTop = Math.max(0, before + by);
      if (el.scrollTop === before) return;
    }
  }

  function move(to: number) {
    if (ids.length === 0) return;
    const id = ids[Math.max(0, Math.min(ids.length - 1, to))];
    active = { id, under: value };
    const row = listEl?.querySelector<HTMLElement>(`#${CSS.escape(optionDomId(id))}`);
    if (listEl && row) reveal(listEl, row);
  }

  function onkeydown(e: KeyboardEvent) {
    const at = activeId === null ? -1 : ids.indexOf(activeId);
    if (e.key === 'ArrowDown') move(at + 1);
    else if (e.key === 'ArrowUp') move(at - 1);
    else if (e.key === 'Home') move(0);
    else if (e.key === 'End') move(ids.length - 1);
    else if (e.key === 'Enter' || e.key === ' ') pick(activeId);
    else return;
    e.preventDefault();
  }
</script>

{#if offered.length === 0}
  <p class="text-13 text-text-3">The model catalog lists no model to pick.</p>
{:else}
  <div
    role="listbox"
    aria-label={label}
    aria-activedescendant={activeId === null ? undefined : optionDomId(activeId)}
    aria-disabled={disabled}
    tabindex={disabled ? -1 : 0}
    data-more-below={more}
    class="group overflow-hidden rounded-[4px] border border-hair-strong focus:border-accent-2 focus:outline-none"
    {onkeydown}
  >
    <!-- #2894 C1 — the list scrolls, and fades, inside the bordered box, so
         the fade never reaches the border or its rounded corners. -->
    <div bind:this={listEl} class="max-h-72 overflow-y-auto" class:fade={more} onscroll={measure}>
      {#each groups as group (group.runner)}
        <div role="group" aria-labelledby={`model-${uid}-runner-${group.runner}`}>
          <div
            id={`model-${uid}-runner-${group.runner}`}
            data-runner-head
            class="sticky top-0 bg-ink-raised px-3 pt-2.5 pb-1 font-mono text-11 text-text-3"
          >
            {group.runner}
          </div>
          {#each group.models as model (model.id)}
            {@const lane = laneLine(model)}
            {@const life = lifecycleLabel(model)}
            {@const selected = model.id === value}
            {@const inForce = current !== '' && model.id === current}
            <!-- Keyboard reaches the rows through the listbox's own handler
                 (aria-activedescendant), so a row is not itself a tab stop. -->
            <!-- svelte-ignore a11y_click_events_have_key_events -->
            <div
              id={optionDomId(model.id)}
              role="option"
              tabindex="-1"
              aria-selected={selected}
              data-model-id={model.id}
              data-current={inForce || undefined}
              data-active={model.id === activeId || undefined}
              class={`flex cursor-pointer flex-col gap-0.5 border-l-2 px-3 py-2 ${
                selected ? 'border-accent-2' : 'border-transparent'
              } ${model.id === shaded ? 'bg-white/5' : ''} ${
                model.id === activeId
                  ? 'group-focus:outline-1 group-focus:-outline-offset-2 group-focus:outline-text-3 group-focus:outline-dashed'
                  : ''
              }`}
              onclick={() => pick(model.id)}
            >
              <div class="flex flex-wrap items-baseline gap-x-2">
                <!-- #2945 — two lines at most, the whole name in the title. -->
                <span
                  class="line-clamp-2 min-w-0 text-13 [overflow-wrap:anywhere] text-text-1"
                  title={model.display_name}>{model.display_name}</span
                >
                {#if inForce}
                  <span class="text-11 text-text-2">✓ {currentLabel}</span>
                {/if}
                <!-- The id as typed (data-13-mono): a copyable id is never set
                     in capitals. The lane line is text-3: cyan here is the
                     selected row and focus, never a property of a row (#2845). -->
                <!-- An id has no spaces: it breaks anywhere rather than run
                     past the list's edge. -->
                <span class="min-w-0 font-mono text-13 [overflow-wrap:anywhere] text-text-3"
                  >{model.id}</span
                >
                {#if lane}
                  <span class="text-11 text-text-3" data-lane-line>{lane}</span>
                {/if}
              </div>
              <div class="flex flex-wrap items-baseline gap-x-2">
                <span class="font-mono text-13 text-text-3">{priceLabel(model)}</span>
                {#if life}
                  <span
                    class={`text-11 ${model.lifecycle.status === 'retired' ? 'text-danger' : 'text-warn'}`}
                    >{life}</span
                  >
                {/if}
              </div>
            </div>
          {/each}
        </div>
      {/each}
    </div>
  </div>
{/if}

<style>
  /* #2846 — while rows lie below the edge, the bottom 2rem fades out. The
     mask is on the inner scroller, never on the bordered box (#2894 C1). */
  .fade {
    mask-image: linear-gradient(to bottom, black calc(100% - 2rem), transparent);
  }
</style>
