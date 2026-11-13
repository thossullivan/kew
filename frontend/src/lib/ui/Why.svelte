<script lang="ts">
  // #2536 — the console's one progressive-disclosure control (DESIGN.md "Disclose
  // progressively"): a block leads with the line a person acts on, and the
  // explanation, provenance and remedy sit one click away behind `why ▸`.
  //
  // The toggle only. The text it opens is the caller's, written where it
  // belongs in the caller's layout (under a table row, inside a card) and
  // carrying `id={controls}` and `hidden={!open}`: hidden rather than absent,
  // so the words stay in the document for find-in-page, server render and the
  // tests that read them, and only leave the reading order until asked for.
  let {
    open = $bindable(false),
    controls,
    label = 'why',
    class: className = ''
  }: {
    open?: boolean;
    /** The id of the element this toggle shows and hides. */
    controls: string;
    label?: string;
    class?: string;
  } = $props();
</script>

<button
  type="button"
  aria-expanded={open}
  aria-controls={controls}
  class={`inline-flex shrink-0 items-center gap-1 font-mono text-13 text-text-3 hover:text-text-1 ${className}`}
  onclick={() => (open = !open)}
  >{label}<span aria-hidden="true" class="inline-block transition-transform motion-reduce:transition-none"
    class:rotate-90={open}>▸</span
  ></button
>
