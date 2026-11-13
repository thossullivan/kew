<script lang="ts">
  import Wordmark from './Wordmark.svelte';
  import ConsoleFigure from './ConsoleFigure.svelte';
  import { DEMO_URL, tourOnDesktop } from './data/demo';
  import { REPO_URL } from './data/install';
  import {
    PROOF_RULE,
    PROOF_ALWAYS,
    PROOF_OPT_IN,
    PROOF_OPT_IN_SUMMARY,
    PROOF_DOCS,
    THREAT_MODEL_DOCS
  } from './data/proof';

  /**
   * The hero states the thesis, then the rule kew is built on as a second
   * headline, then the channels it holds on. It is complete at rest: nothing
   * in it waits for an animation, a timer or a click, so a reader who lands
   * and leaves has read the whole claim. The opt-in rows fold into a native
   * <details>, which opens without script.
   *
   * It used to perform a cost-approval hold on a finished run. That drew the
   * gate at the wrong point in the loop (the hold fires before dispatch, on
   * an estimate) and led with an opt-in feature, so it went.
   *
   * Under the copy and the rule, in the same first section, sits the console
   * figure: real frames of kew's demo console, and the demo's own link.
   */
</script>

<div class="hero-stage">
  <header class="bar shell">
    <a href="/" class="brand" aria-label="kew, top of the page">
      <Wordmark />
    </a>
    <nav aria-label="Site">
      <a href="/docs/">Docs</a>
      <a href="#loop">How a change is proven</a>
      <!-- A link that leaves the site carries the arrow at every width; the
           label names the other site too. -->
      <a href={DEMO_URL} use:tourOnDesktop aria-label="Demo (opens the demo site)"
        >Demo<span class="away" aria-hidden="true">&nearr;</span></a
      >
      <a href={REPO_URL} aria-label="GitHub (opens the repository)"
        >GitHub<span class="away" aria-hidden="true">&nearr;</span></a
      >
    </nav>
  </header>

  <div class="hero shell" id="content" tabindex="-1">
    <div class="copy">
      <p class="kicker"><i></i>the agent is an untrusted author</p>

      <!-- "the work." is held together, so a phone never leaves "work." alone
           on a line. -->
      <h1>Offload the&nbsp;work.<br />Keep the <em>judgment</em>.</h1>

      <p class="sub">
        The open-source control plane for autonomous coding agents. kew treats every agent as an
        untrusted author: what it emits is input to check, never proof.
      </p>

      <div class="ctas">
        <a class="cta primary" href="#install">Install kew</a>
        <a class="cta ghost" href="#loop">How a change is proven &rarr;</a>
      </div>
    </div>

    <div class="proof">
      <h2 class="rule">{PROOF_RULE}</h2>

      <div class="card">
        <table class="channels">
          <thead>
            <tr><th scope="col">What the agent emits</th><th scope="col">What kew does with it</th></tr>
          </thead>
          <tbody>
            {#each PROOF_ALWAYS as row (row.emits)}
              <tr>
                <th scope="row">{row.emits}</th>
                <td>
                  {row.kew}
                  {#if row.scope}<span class="scope">{row.scope}</span>{/if}
                </td>
              </tr>
            {/each}
          </tbody>
        </table>

        <details class="optin">
          <summary><span class="sum-text">{PROOF_OPT_IN_SUMMARY}</span></summary>
          <!-- Each row names its own setting: the gates are a separate opt-in
               from governed review (docs/proof.md). -->
          <table class="channels" aria-label="Checked once an opt-in setting is on">
            <tbody>
              {#each PROOF_OPT_IN as row (row.emits)}
                <tr>
                  <th scope="row">{row.emits}</th>
                  <td>
                    {row.kew}
                    {#if row.setting}<span class="scope">With <code>{row.setting}</code>.</span>{/if}
                  </td>
                </tr>
              {/each}
            </tbody>
          </table>
          <p class="setting">kew&rsquo;s own repository runs with governed review on.</p>
        </details>

        <div class="foot">
          <a class="doclink" href={PROOF_DOCS}>How kew decides a change is proven &rarr;</a>
          <a class="doclink" href={THREAT_MODEL_DOCS}>What kew does not defend &rarr;</a>
        </div>
      </div>
    </div>
  </div>

  <ConsoleFigure />
</div>

<style>
  .hero-stage {
    position: relative;
    isolation: isolate;
    display: flex;
    flex-direction: column;
  }

  /* Depth comes from value, not a hue: magenta is the primary action's. */
  .hero-stage::before {
    content: '';
    position: absolute;
    inset: 0;
    z-index: -1;
    background: radial-gradient(120% 80% at 50% 0%, var(--canvas-lift) 0%, transparent 62%);
  }

  /* `.shell` centres itself with auto margins, which in this flex column
     turns off stretch: without the width the bar and hero shrink to their
     content, a narrow strip on desktop and wider than a phone. */
  .hero-stage > .shell {
    width: 100%;
  }

  .bar {
    display: flex;
    align-items: center;
    gap: 26px;
    padding-block: 11px;
    border-bottom: 1px solid var(--line-soft);
  }
  .brand {
    text-decoration: none;
  }
  /* Shown at every width, its four links on one row. Each link is a 44px tap
     target; the padding keeps the 26px between labels the bar had before.
     Below 640px the nav takes its own row under the wordmark, and below
     480px the padding goes (the 44px minimum still centres Docs, Demo and
     GitHub in their targets) and the arrows shrink, so Docs, How a change is
     proven, Demo and GitHub hold one row from 320px. */
  nav {
    margin-left: auto;
    min-width: 0;
    display: flex;
    flex-wrap: wrap;
    justify-content: flex-end;
    gap: 6px;
    font-family: var(--mono);
    font-size: 13px;
  }
  nav a {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-width: 44px;
    min-height: 44px;
    padding-inline: 10px;
    color: var(--muted);
    text-decoration: none;
  }
  nav a:hover {
    color: var(--fg);
  }
  @media (max-width: 700px) {
    .bar {
      gap: 16px;
    }
    nav {
      gap: 4px;
    }
    nav a {
      padding-inline: 6px;
    }
  }
  @media (max-width: 639.98px) {
    .bar {
      flex-wrap: wrap;
      gap: 0;
      padding-block: 8px 0;
    }
    nav {
      flex: 1 0 100%;
      margin-left: 0;
      justify-content: space-between;
    }
    nav a:first-child {
      justify-content: flex-start;
    }
  }
  .away {
    margin-left: 0.4em;
  }
  @media (max-width: 479.98px) {
    nav {
      gap: 0;
      font-size: 11px;
    }
    nav a {
      padding-inline: 0;
    }
    .away {
      margin-left: 2px;
      font-size: 0.85em;
    }
  }
  /* At 320px the four labels and two arrows need 4px more than the row has;
     a hair of tracking gives it back. */
  @media (max-width: 399.98px) {
    nav {
      letter-spacing: -0.02em;
    }
  }

  /* Top-aligned, with the top padding cut (L4): at 1366x650 the rule, the
     three rows, the folded row and both doc links fit the first screen. */
  .hero {
    display: grid;
    grid-template-columns: minmax(0, 1fr);
    gap: 44px;
    align-content: start;
    padding-block: 32px 48px;
  }
  .hero:focus {
    outline: none;
  }
  @media (min-width: 1080px) {
    .hero {
      grid-template-columns: minmax(0, 0.92fr) minmax(0, 1.08fr);
      gap: 60px;
      align-items: start;
      padding-block: 44px 48px;
    }
  }

  .kicker {
    display: inline-flex;
    align-items: center;
    gap: 10px;
    font-family: var(--mono);
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--muted);
    margin: 0 0 24px;
  }
  /* A neutral mark: nothing is live in the hero and no person is the next
     move, so it spends neither cyan nor magenta. */
  .kicker i {
    flex: none;
    width: 6px;
    height: 6px;
    background: var(--muted);
  }

  h1 {
    font-family: var(--mono);
    font-weight: 600;
    font-size: clamp(40px, 7.2vw, 82px);
    line-height: 0.99;
    letter-spacing: -0.05em;
    margin: 0 0 26px;
  }
  /* In two columns the copy column is about 0.46 of the shell's content, so
     the size follows the viewport: "Offload the work." (17 glyphs at 0.55em
     with the tracking) holds one line from 1080px up and at the 1320px cap. */
  @media (min-width: 1080px) {
    h1 {
      font-size: clamp(40px, 3.6vw, 52px);
    }
  }
  /* "judgment" names the person's part, the one thing magenta means. */
  h1 em {
    font-style: normal;
    color: var(--accent);
  }

  .sub {
    font-size: 15px;
    line-height: 1.6;
    color: var(--muted);
    max-width: var(--measure);
    margin: 0 0 32px;
  }

  .ctas {
    display: flex;
    flex-wrap: wrap;
    gap: 12px 16px;
    align-items: flex-start;
  }
  /* Under 480px Install kew runs the full width, and the text link sits
     under it (R10). */
  @media (max-width: 479.98px) {
    .ctas {
      flex-direction: column;
      align-items: stretch;
    }
    .ctas .cta {
      justify-content: center;
    }
    .ctas .ghost {
      justify-content: flex-start;
    }
  }
  .cta {
    display: inline-flex;
    align-items: center;
    min-height: 44px;
    font-family: var(--mono);
    font-size: 13px;
    padding: 0 20px;
    max-width: 100%;
    border: 1px solid var(--border-strong);
    color: var(--fg);
    background: none;
    text-decoration: none;
    white-space: nowrap;
    cursor: pointer;
    transition:
      border-color 180ms ease,
      background 180ms ease,
      color 180ms ease;
  }
  .cta:hover {
    border-color: var(--fg-dim);
  }
  /* The page's primary action: magenta, as the install section's Copy. */
  .primary {
    background: var(--accent);
    border-color: var(--accent);
    color: var(--accent-fg);
    font-weight: 600;
  }
  .primary:hover {
    background: var(--accent-bright);
    border-color: var(--accent-bright);
  }
  .ghost {
    border-color: transparent;
    color: var(--muted);
    padding-inline: 0;
  }
  .ghost:hover {
    color: var(--fg);
    border-color: transparent;
  }

  /* ── the rule and its channels ──────────────────── */

  /* The second headline: sans, 26px, on the page rather than in a card, so
     it reads as a statement and not as a card title (V1). */
  .rule {
    font-family: var(--sans);
    font-weight: 500;
    font-size: 26px;
    line-height: 1.25;
    letter-spacing: -0.01em;
    color: var(--fg);
    margin: 0 0 16px;
    text-wrap: balance;
  }

  /* A hairline box, no shadow and no header: the table is evidence for the
     rule above it, not a second headline. */
  .card {
    border: 1px solid var(--line-soft);
    background: color-mix(in oklab, var(--surface) 50%, transparent);
  }

  .channels {
    border-collapse: collapse;
    width: 100%;
    font-family: var(--sans);
    font-size: 13px;
    line-height: 1.45;
  }
  .channels th,
  .channels td {
    text-align: left;
    vertical-align: top;
    padding: 10px 16px;
  }
  .channels tr + tr,
  .channels tbody tr:first-child {
    border-top: 1px solid var(--line-soft);
  }
  .channels thead th {
    padding-block: 12px 8px;
    font-family: var(--mono);
    font-weight: 400;
    font-size: 11px;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--muted);
  }
  .channels tbody th {
    width: 32%;
    padding-right: 8px;
    font-weight: 500;
    color: var(--fg);
  }
  .channels td {
    color: var(--fg-dim);
  }
  .scope {
    display: block;
    margin-top: 2px;
    color: var(--muted);
  }

  .optin {
    border-top: 1px solid var(--line-soft);
  }
  .optin summary {
    display: flex;
    align-items: baseline;
    gap: 10px;
    min-height: 44px;
    padding: 12px 16px;
    font-family: var(--sans);
    font-size: 13px;
    line-height: 1.45;
    color: var(--fg-dim);
    cursor: pointer;
    list-style: none;
  }
  .optin summary::-webkit-details-marker {
    display: none;
  }
  .optin summary::before {
    content: '\25B8';
    flex: none;
    font-family: var(--mono);
    color: var(--fg-dim);
    transition: transform 160ms ease;
  }
  .optin[open] summary::before {
    transform: rotate(90deg);
  }
  .optin summary:hover,
  .optin summary:focus-visible {
    color: var(--fg);
  }
  .optin summary:hover .sum-text,
  .optin summary:focus-visible .sum-text {
    text-decoration: underline;
    text-underline-offset: 3px;
  }
  .scope code {
    font-family: var(--mono);
    color: var(--fg-dim);
  }
  .setting {
    margin: 0;
    padding: 10px 16px 12px;
    border-top: 1px solid var(--line-soft);
    font-size: 13px;
    line-height: 1.45;
    color: var(--muted);
  }

  /* Below 600px a row stacks, the emitted thing over what kew does, and the
     rule between rows runs the card's full width (L5). */
  @media (max-width: 599.98px) {
    .channels thead {
      display: none;
    }
    .channels tbody tr {
      display: block;
      padding: 10px 16px;
    }
    .channels tbody tr:first-child {
      border-top: 0;
    }
    .channels tbody th,
    .channels tbody td {
      display: block;
      width: auto;
      padding: 0;
    }
    .channels tbody th {
      margin-bottom: 2px;
    }
    .optin .channels tbody tr:first-child {
      border-top: 1px solid var(--line-soft);
    }
  }

  .foot {
    border-top: 1px solid var(--line-soft);
    padding: 4px 16px;
    display: flex;
    flex-wrap: wrap;
    gap: 0 20px;
  }
  .doclink {
    display: inline-flex;
    align-items: center;
    min-height: 36px;
    font-family: var(--mono);
    font-size: 13px;
    color: var(--fg-dim);
    text-decoration: none;
  }
  .doclink:hover {
    color: var(--fg);
    text-decoration: underline;
    text-underline-offset: 3px;
  }
</style>
