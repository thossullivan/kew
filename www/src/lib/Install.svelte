<script lang="ts">
  import { INSTALL, INSTALL_PARTS, REPO_URL } from './data/install';
  import { RUNNERS } from './data/runners';

  /**
   * Get kew: the install command and Copy, the page's primary action, then
   * what it needs and works with, the six runners by name, and the docs to
   * read next. The runner names come from data/runners.ts, which a kew test
   * holds equal to the shipped registry.
   */

  let copied = $state(false);

  /* The command scrolls sideways in its block on a narrow screen. The fade at
     the right edge says so, and goes once the block is scrolled to its end
     (or the whole command fits). Without script the fade stays. */
  let scroller: HTMLElement | null = $state(null);
  let atEnd = $state(false);
  function measure() {
    if (scroller) atEnd = scroller.scrollLeft >= scroller.scrollWidth - scroller.clientWidth - 1;
  }
  $effect(() => {
    if (!scroller) return;
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(scroller);
    return () => ro.disconnect();
  });

  async function copy() {
    try {
      await navigator.clipboard.writeText(INSTALL);
      copied = true;
      setTimeout(() => (copied = false), 1600);
    } catch {
      // Clipboard blocked — the command is selectable on screen regardless.
    }
  }

  const DOCS = [
    ['Getting started', '/docs/getting-started/'],
    ['Every command', '/docs/cli/'],
    ['What each runner is contained by', '/docs/runners/'],
    ['Threat model', '/docs/threat-model/']
  ];
</script>

<section id="install" class="shell" aria-labelledby="install-h">
  <h2 id="install-h">Get kew</h2>

  <div class="get">
    <p class="eyebrow">Install from source</p>
    <!-- The command scrolls sideways inside its block and never breaks inside
         a token (L20); Copy is the page's primary action, in magenta (L2). -->
    <div class="cmdline">
      <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
      <div
        class="cmdscroll"
        class:end={atEnd}
        tabindex="0"
        role="region"
        aria-label="Install command, scrolls sideways"
        bind:this={scroller}
        onscroll={measure}
      >
        <code>{#each INSTALL_PARTS as part, i}{#if i > 0}<wbr />{/if}{part}{/each}</code>
      </div>
      <button type="button" class="btn-primary" onclick={copy}>{copied ? 'Copied' : 'Copy'}</button>
    </div>
    <p class="sr-only" role="status" aria-live="polite">{copied ? 'Install command copied' : ''}</p>
    <p class="req">
      Python 3.13. Requires <code>git</code>, <code>gh</code>, a container runtime and a runner.
      Works with GitHub Issues, and with Linear
      <span class="badge" data-tracker-badge="linear">Experimental</span>.
      <a href="{REPO_URL}/blob/main/LICENSE">Apache-2.0&nbsp;&nearr;</a>.
    </p>
  </div>

  <div class="runners">
    <span class="r-label">A CLI, a TUI and a web console over six runners:</span>
    <ul>
      {#each RUNNERS as r (r.id)}
        <li class:experimental={r.experimental}>
          {r.id}{#if r.experimental}<span class="tag">&nbsp;&middot; experimental</span>{/if}
        </li>
      {/each}
    </ul>
  </div>

  <nav aria-label="Go deeper">
    {#each DOCS as [label, href] (href)}
      <a {href}>{label} &rarr;</a>
    {/each}
  </nav>
</section>

<style>
  section {
    padding-block: clamp(64px, 9vw, 96px);
    border-top: 1px solid var(--line-soft);
    display: flex;
    flex-direction: column;
    gap: 24px;
  }
  h2 {
    font-family: var(--mono);
    font-weight: 600;
    font-size: 26px;
    letter-spacing: -0.02em;
    margin: 0;
  }

  .get {
    display: flex;
    flex-direction: column;
    gap: 12px;
    align-items: flex-start;
    min-width: 0;
  }
  .get .eyebrow {
    margin: 0;
  }
  /* The block runs the full column width (L18). The command is one line
     that scrolls sideways inside it, so a phone never breaks it inside a
     token ("kew-/cli"); a wide window shows it whole (L20). */
  .cmdline {
    display: flex;
    align-items: stretch;
    width: 100%;
    border: 1px solid var(--border-strong);
  }
  /* The right edge fades, so a command cut by a narrow window says that it
     scrolls; scrolled to its end, or whole, the fade goes. */
  .cmdscroll {
    flex: 1;
    min-width: 0;
    overflow-x: auto;
    mask-image: linear-gradient(to right, #000 calc(100% - 40px), transparent);
  }
  .cmdscroll.end {
    mask-image: none;
  }
  .cmdline code {
    display: block;
    width: max-content;
    font-family: var(--mono);
    font-size: 15px;
    white-space: pre;
    color: var(--fg);
    padding: 14px 32px 14px 20px;
  }
  .cmdline button {
    flex: none;
    min-height: 44px;
    height: auto;
    padding-inline: 16px;
  }
  .req {
    font-size: 13px;
    line-height: 1.6;
    color: var(--muted);
    margin: 0;
    max-width: 72ch;
  }
  .req code {
    font-family: var(--mono);
    color: var(--fg-dim);
  }
  .req a {
    color: var(--fg-dim);
    text-underline-offset: 3px;
  }
  .req a:hover {
    color: var(--fg);
  }
  /* The docs' own word for Linear's status (docs/linear-webhooks.md), as a
     neutral tag: scripts/page-claims.mjs holds the two together. */
  .badge {
    font-family: var(--mono);
    font-size: 11px;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--fg-dim);
    border: 1px solid var(--hair-strong);
    padding: 1px 6px;
    white-space: nowrap;
  }

  .runners {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px 16px;
    font-family: var(--mono);
    font-size: 13px;
  }
  .r-label {
    color: var(--muted);
  }
  .runners ul {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin: 0;
    padding: 0;
    list-style: none;
  }
  /* The console's chip (DESIGN.md): 13px mono in a fully rounded 1px
     hairline outline. A name, not a control. */
  .runners li {
    border: 1px solid var(--hair-strong);
    border-radius: 9999px;
    padding: 2px 10px;
    line-height: 1.5;
    color: var(--fg-dim);
  }
  .tag {
    color: var(--muted);
  }

  nav {
    display: flex;
    flex-wrap: wrap;
    gap: 0 32px;
    font-family: var(--mono);
    font-size: 13px;
  }
  nav a {
    display: inline-flex;
    align-items: center;
    min-height: 44px;
    color: var(--fg-dim);
    text-underline-offset: 3px;
  }
  nav a:hover {
    color: var(--fg);
  }
</style>
