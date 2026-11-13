<script lang="ts">
  // FUI-21 — the description panel (DWork x 468–853, y ≈624–928): what a person
  // wrote about this item on its tracker, read through FUI-31.
  //
  // A renderer over `descriptionPanel`, which is where the one rule that matters
  // lives: the body is drawn only for a reading the server stands behind, and
  // every other answer is the reason, with the "Open on GitHub" link beside it
  // where the payload named a URL (a read that landed no reading names none, so
  // that answer is its reason alone). This file cannot show a body the model did
  // not hand it, which is the point — a stale or failed remote read that
  // rendered as prose would be indistinguishable from a current one.
  //
  // The body is drawn as the SUBSET `./markdown` tokenizes (Design C1): three
  // heading levels, paragraphs, lists, fences, quotes, GFM tables and rules,
  // with code spans, bold, em and http(s) links inside them. Anything outside
  // that subset — a `<details>` wrapper, a footnote, an image — stays the text
  // it was written as, in a paragraph, so the panel never claims a fidelity it
  // does not have and never silently loses a line.
  //
  // What makes the subset safe to render at all is that there is no HTML in it:
  // **this file must never use Svelte's `@html` tag** (and `markdown.test.ts`
  // fails on one in any component here). Every character of a tracker body
  // reaches the page as a text node or an attribute Svelte escapes, and the only
  // target that becomes an `href` is one `safeHref` allowed — so a body with
  // `<img onerror=…>` or a `javascript:` link in it renders those as characters,
  // which is the only treatment of a stranger's string this console trusts.
  import type { ItemDescription } from '$lib/api';
  import { descriptionPanel } from './evidence';
  import PanelDisclosure from './PanelDisclosure.svelte';
  import type { Inline, MarkdownList } from './markdown';

  let {
    description,
    notice = ''
  }: {
    /** The payload, or `null` when the read has not landed or failed. */
    description: ItemDescription | null;
    /** A read that FAILED, worded by the page. '' otherwise. */
    notice?: string;
  } = $props();

  const model = $derived(descriptionPanel(description, notice));

  /**
   * A body heading's tag. The panel's own head is the `h2`, so the body starts
   * at `h3` and runs down: a tracker's `#` cannot outrank the page it is on.
   */
  const headingTag = (level: number): 'h3' | 'h4' | 'h5' =>
    level === 1 ? 'h3' : level === 2 ? 'h4' : 'h5';

  /** 15px for `#`/`##`, 13px for `###` — the two prose sizes, nothing new. */
  const headingSize = (level: number): string => (level === 3 ? 'text-13' : 'text-15');

  /**
   * An `<ol>`'s own first number, and only where a body wrote one that is not
   * 1: a procedure numbered from 3, or the second half of one a fenced block
   * split in two, would otherwise be renumbered from 1 and show the reader a
   * step number nobody wrote. `undefined` leaves the attribute off entirely.
   */
  const startAt = (group: MarkdownList): number | undefined =>
    group.ordered && group.start !== 1 ? group.start : undefined;

  const PROSE = 'text-15 leading-[1.6] text-text-2';
  const CODE = 'rounded-xs bg-ink-track px-1 py-0.5 font-mono text-13 text-text-1';
  const CELL = 'border-b border-hair px-2 py-1.5 text-left align-top';
</script>

<!-- Inline content. Written tight on purpose: a newline inside a `pre-line`
     paragraph would render as a line break nobody wrote. -->
{#snippet inlines(parts: Inline[])}{#each parts as part, i (i)}{#if part.kind === 'code'}<code
        class={CODE}>{part.text}</code
      >{:else if part.kind === 'strong'}<strong class="font-medium text-text-1">{part.text}</strong
      >{:else if part.kind === 'em'}<em>{part.text}</em>{:else if part.kind === 'link'}<a
        href={part.href}
        target="_blank"
        rel="noopener noreferrer"
        class="text-text-1 underline decoration-hair-strong underline-offset-2 hover:decoration-text-2"
        >{part.text}<span aria-hidden="true"> ↗</span></a
      >{:else}{part.text}{/if}{/each}{/snippet}

{#snippet list(group: MarkdownList)}
  <svelte:element
    this={group.ordered ? 'ol' : 'ul'}
    class="{PROSE} {group.ordered ? 'list-decimal' : 'list-disc'} flex flex-col gap-1 pl-5"
    start={startAt(group)}
  >
    {#each group.items as item, i (i)}
      <li>
        {@render inlines(item.inline)}
        {#if item.sub}{@render list(item.sub)}{/if}
      </li>
    {/each}
  </svelte:element>
{/snippet}

<section aria-label="Description" class="flex min-w-0 flex-col overflow-y-auto pb-[22px] lg:pr-9 lg:pb-0">
  <!-- Closed on load (#2786 D16); the summary says how much is inside. -->
  <PanelDisclosure label="Description" meta={model.contents}>
    {#if model.stale}
      <p class="text-13 text-text-3">{model.stale}</p>
    {/if}

    {#each model.blocks as block, i (i)}
      {#if block.kind === 'heading'}
        <svelte:element
          this={headingTag(block.level)}
          class="{headingSize(block.level)} font-medium text-text-1"
          >{@render inlines(block.inline)}</svelte:element
        >
      {:else if block.kind === 'paragraph'}
        <p class="{PROSE} whitespace-pre-line">{@render inlines(block.inline)}</p>
      {:else if block.kind === 'code'}
        <!-- The long line scrolls inside its own block; the page never does. -->
        <pre
          class="overflow-x-auto rounded-xs bg-ink-track p-3 font-mono text-13 leading-[1.5] text-text-2"
          data-lang={block.lang === '' ? undefined : block.lang}><code>{block.text}</code></pre>
      {:else if block.kind === 'list'}
        {@render list(block)}
      {:else if block.kind === 'quote'}
        <blockquote class="flex flex-col gap-2 border-l-2 border-hair-strong pl-3">
          {#each block.paragraphs as paragraph, p (p)}
            <p class="text-15 leading-[1.6] whitespace-pre-line text-text-3">
              {@render inlines(paragraph)}
            </p>
          {/each}
        </blockquote>
      {:else if block.kind === 'table'}
        <div class="overflow-x-auto">
          <table class="w-full border-collapse text-13">
            <thead>
              <tr>
                {#each block.head as cell, c (c)}
                  <th class="{CELL} font-medium text-text-1">{@render inlines(cell)}</th>
                {/each}
              </tr>
            </thead>
            <tbody>
              {#each block.rows as row, r (r)}
                <tr>
                  {#each row as cell, c (c)}
                    <td class="{CELL} font-normal text-text-2">{@render inlines(cell)}</td>
                  {/each}
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      {:else}
        <hr class="border-t border-hair" />
      {/if}
    {/each}

    {#each model.sections as section (section.key)}
      <!-- The artboard labels a named section inline, 11px uppercase before its
           own first words, rather than heading it as a block. These two are one
           line each on every tracker kew reads, so they stay literal text. -->
      <p class="text-15 leading-[1.6] whitespace-pre-line text-text-2">
        <span class="mr-2 text-11 text-text-1">{section.label}</span>{section.text}
      </p>
    {/each}

    {#if model.notice !== ''}
      <p class="text-15 leading-[1.6] text-text-3">{model.notice}</p>
    {/if}

    {#if model.link}
      <!-- The fallback, and the only link out of this panel that is not a
           tracker's own: the description is not synced into kew, so the tracker is
           where it is read. -->
      <a
        href={model.link.href}
        target="_blank"
        rel="noreferrer"
        class="flex items-center gap-2 font-mono text-13 text-text-1 hover:text-text-2"
        >{model.link.label}<span aria-hidden="true">↗</span></a
      >
    {/if}
  </PanelDisclosure>
</section>
