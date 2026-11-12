<script lang="ts">
  /**
   * #3052 N4 — a command, printed the way it is typed.
   *
   * Every surface that prints a command prints it through this. The browser
   * breaks a line after a hyphen, so a command in a narrow block read
   * "kew epic unhold supervised- / daemon 2814" and "go-live-2026-09 SFD- /
   * 229": an id cut in two at a line end, which is not what anybody typed.
   *
   * So a command wraps only at its own spaces (operator ruling 2026-10-20). It
   * never scrolls inside its block: DESIGN.md allows no sideways scroll at 390,
   * and a scrolled command is not painted whole. Each space-separated word is
   * its own inline-block, so a line can end only between words, never at a
   * hyphen inside one; the spaces between them are the command's own,
   * kept as typed (`white-space: pre-wrap`). A word wider than the whole block
   * is the one case left: capped at the block's width, it breaks inside itself
   * (`overflow-wrap: anywhere`) rather than run out of the block. A word box
   * is exactly its glyphs' height (`line-height: normal`), so its top is its
   * text's top and a reading of the block's lines counts each line once; the
   * block's own line height still sets the spacing between lines.
   *
   * The text is the command exactly — the words and the spaces between them,
   * in order, with nothing added — so a select-and-copy of the block is the
   * command too.
   */
  let { command, class: className = '' }: { command: string; class?: string } = $props();

  /** The command's words and the runs of whitespace between them, in order. */
  const parts = $derived(
    command
      .split(/(\s+)/)
      .filter((part) => part !== '')
      .map((text) => ({ text, space: /^\s/.test(text) }))
  );
</script>

<code data-command class={`whitespace-pre-wrap ${className}`}
  >{#each parts as part, i (i)}{#if part.space}{part.text}{:else}<span
        class="inline-block max-w-full [line-height:normal] [overflow-wrap:anywhere]">{part.text}</span
      >{/if}{/each}</code
>
