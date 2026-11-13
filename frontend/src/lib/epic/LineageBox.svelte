<script lang="ts">
  // The epic graph's lineage selection box (#2544): Dagster's `+asset+` over
  // an epic. It sits in the row above the canvas beside the Graph / Timeline
  // switch rather than in a row of its own, because every pixel a row takes
  // comes off the canvas, and go-live's "Fit all" stops fitting at 1440x900
  // with 40px less of it.
  //
  // It owns only the typing: the parse and its sentence are `lineage-select`,
  // the URL is the page's, and what dims is DagView's.
  import { formatSelection, parseSelection } from './lineage-select';
  import { displayId } from './render';

  let {
    text,
    ids,
    onchange
  }: {
    /** The selection as the URL holds it (`?select=`), or null. */
    text: string | null;
    /** The ids the snapshot on screen carries, to say when a root is not one. */
    ids: readonly string[];
    /** Write a selection: its canonical text, or null to clear it. */
    onchange: (text: string | null) => void;
  } = $props();

  /** What the box holds while it is being typed in, and its last refusal. */
  let draft = $state('');
  let refused = $state('');
  $effect(() => {
    draft = text ?? '';
    refused = '';
  });

  const parsed = $derived(parseSelection(text ?? ''));
  /** The one sentence under the box, or '': a refusal of what was typed, a
   * hand-typed `?select=` that is not the grammar, or a root this epic does
   * not have. */
  const note = $derived(
    refused !== ''
      ? refused
      : !parsed.ok
        ? parsed.error
        : parsed.selection !== null && !ids.includes(parsed.selection.id)
          ? `${displayId(parsed.selection.id)} is not on this graph.`
          : ''
  );

  /** Enter: write the canonical form, or say why not and keep the old one. */
  function submit(e: SubmitEvent): void {
    e.preventDefault();
    const next = parseSelection(draft);
    if (!next.ok) {
      refused = next.error;
      return;
    }
    refused = '';
    const canonical = next.selection === null ? null : formatSelection(next.selection);
    draft = canonical ?? '';
    onchange(canonical);
  }
</script>

<form class="select" role="search" aria-label="Lineage selection" onsubmit={submit}>
  <input
    id="dag-lineage"
    class="input"
    type="text"
    spellcheck="false"
    autocomplete="off"
    aria-label="Select lineage, like +#2469+"
    placeholder="+#N · #N+ · L on a node"
    aria-describedby={note !== '' ? 'dag-lineage-note' : undefined}
    aria-invalid={note !== '' ? 'true' : undefined}
    bind:value={draft}
  />
  {#if text}
    <button class="clear" type="button" aria-label="Clear selection" onclick={() => onchange(null)}
      >×</button
    >
  {/if}
  {#if note !== ''}
    <p id="dag-lineage-note" class="note" role="status">{note}</p>
  {/if}
</form>

<style>
  .select {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 2px 4px;
    flex: 0 1 280px;
    min-width: 0;
    margin-left: 12px;
  }

  /* Plain text at rest, not a pill (D39): the 11px-radius box drawn round
     "+#N · #N+ · L on a node" read as a control to press, and the words are
     a hint about the keys. The rule under it comes on hover, and the cyan
     ring on focus, where it is being typed in. */
  .input {
    flex: 1 1 120px;
    min-width: 0;
    height: 22px;
    padding: 0;
    border: 0;
    border-bottom: 1px solid transparent;
    border-radius: 0;
    background: transparent;
    font-family: var(--font-mono);
    font-size: 11px;
    color: var(--text-1);
  }

  .input::placeholder {
    color: var(--text-4);
  }

  .input:hover,
  .input:not(:placeholder-shown) {
    border-bottom-color: var(--hair-strong);
  }

  .input:focus-visible {
    outline: 2px solid var(--accent-2);
    outline-offset: 1px;
  }

  .clear {
    width: 22px;
    height: 22px;
    border-radius: 11px;
    color: var(--text-2);
  }

  .clear:hover {
    background: var(--ink-selected);
  }

  .note {
    flex-basis: 100%;
    margin: 0;
    font-size: 13px;
    color: var(--text-2);
  }
</style>
