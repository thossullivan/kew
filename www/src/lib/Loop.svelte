<script lang="ts">
  import { PROOF_DOCS } from './data/proof';
  import { FRAME_DIR, JUDGMENT } from './data/console-frames';

  /**
   * How a change is proven: the loop's six stages as one strip, the two that
   * are the person's marked in magenta, then how a change leaves the agent's
   * sandbox on the contained route. Nothing here moves or waits on a click.
   *
   * Each stage keeps one sentence of what the page said about it before (the
   * scroll spine and its consoles, retired for this strip). Each sentence in
   * the diagram restates docs/proof.md, "The agent's repository", which names
   * the code and the tests behind it.
   *
   * From 1000px, where the strip is one row, a crop of the demo console's
   * "needs you" block hangs under stage 05: what keeping the judgment looks
   * like in the product. Narrower it is not drawn, and being lazy it never
   * loads.
   */

  type Stage = { title: string; you: boolean; line: string };

  const STAGES: Stage[] = [
    { title: 'You scope the work.', you: true, line: 'Label an issue ready and it enters the queue.' },
    {
      title: 'It dispatches into isolation.',
      you: false,
      line: 'Every agent gets a standalone clone with its own .git and branch.'
    },
    {
      title: 'It works under a hard cap.',
      you: false,
      line: 'Once a cap is reached, every dispatch is refused at admission.'
    },
    {
      title: "A second agent attacks the first one's work.",
      you: false,
      line: 'Opt-in. With it on, the reviewer is a fresh context and the verdict is bound to the pull request head it reviewed.'
    },
    { title: 'You keep the judgment.', you: true, line: 'What arrives is a pull request, not a merge.' },
    {
      title: 'Every decision is attested.',
      you: false,
      line: 'Each event is hash-chained to the one before it.'
    }
  ];

  type Step = { where: string; title: string; line: string; untrusted?: boolean };

  const ROUTE: Step[] = [
    {
      where: 'Untrusted',
      title: 'The agent’s checkout',
      line: 'It can write anything here, .git included: hooks, config, replace refs, a push URL.',
      untrusted: true
    },
    {
      where: 'Second container',
      title: 'Copy the files out',
      line: 'No network, no credentials. The changed files leave as plain bytes.'
    },
    {
      where: 'Host',
      title: 'Rebuild the commit',
      line: 'Each file is checked. The commit is made in a repository only the host has touched.'
    },
    {
      where: 'Host',
      title: 'Scan, then push',
      line: 'Every object the push would send is scanned for the credentials the host holds and for known key shapes. Then one commit goes to the branch the operator configured.'
    }
  ];
</script>

<section id="loop" class="shell" aria-labelledby="loop-h">
  <div class="intro">
    <p class="eyebrow">The loop</p>
    <h2 id="loop-h">Six stages.<br />You own two of them.</h2>
  </div>

  <ol class="stages">
    {#each STAGES as stage, i (stage.title)}
      <li class="stage" class:you={stage.you}>
        <span class="n">{String(i + 1).padStart(2, '0')}{stage.you ? ' · you' : ''}</span>
        <span class="title">{stage.title}</span>
        <span class="line">{stage.line}</span>
      </li>
    {/each}
  </ol>

  <!-- Under stage 05's column, joined to its cell by a short hairline. -->
  <figure class="judgment" aria-label="Stage 05 in the console">
    <div class="j-frame">
      <p class="j-head"><span>05 in the console</span><span>demo data</span></p>
      <img
        src="{FRAME_DIR}{JUDGMENT.file}"
        width={JUDGMENT.width}
        height={JUDGMENT.height}
        loading="lazy"
        decoding="async"
        alt={JUDGMENT.alt}
      />
    </div>
  </figure>

  <div class="route">
    <p class="r-label">How a change leaves the agent&rsquo;s sandbox &middot; the contained route</p>
    <ol class="steps">
      {#each ROUTE as step, i (step.title)}
        {#if i > 0}<li class="arrow" aria-hidden="true"></li>{/if}
        <li class="step" class:untrusted={step.untrusted}>
          <span class="where">{step.where}</span>
          <strong>{step.title}</strong>
          <span class="line">{step.line}</span>
        </li>
      {/each}
    </ol>
    <a class="doclink" href={PROOF_DOCS}>Each rule, with its code, its tests and its limits &rarr;</a>
  </div>
</section>

<style>
  section {
    padding-block: clamp(64px, 9vw, 96px);
    border-top: 1px solid var(--line-soft);
    display: flex;
    flex-direction: column;
    gap: 40px;
  }
  .intro {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .intro .eyebrow {
    margin: 0;
  }
  .intro h2 {
    font-family: var(--mono);
    font-weight: 600;
    font-size: clamp(26px, 4.4vw, 44px);
    line-height: 1.12;
    letter-spacing: -0.03em;
    margin: 0;
  }

  /* One strip of six; two rows of three on a tablet; one column on a phone. */
  .stages {
    margin: 0;
    padding: 0;
    list-style: none;
    display: grid;
    grid-template-columns: minmax(0, 1fr);
    gap: 1px;
    background: var(--line-soft);
    border: 1px solid var(--line-soft);
  }
  @media (min-width: 600px) {
    .stages {
      grid-template-columns: repeat(3, minmax(0, 1fr));
    }
  }
  @media (min-width: 1000px) {
    .stages {
      grid-template-columns: repeat(6, minmax(0, 1fr));
    }
  }
  .stage {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 20px 16px;
    background: var(--bg);
    border-top: 3px solid var(--hair-strong);
  }
  /* The person's two stages: magenta, the one thing it means here. */
  .stage.you {
    border-top-color: var(--accent);
  }
  .n {
    font-family: var(--mono);
    font-size: 11px;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--muted);
  }
  .stage.you .n {
    color: var(--accent);
  }
  .title {
    font-size: 15px;
    font-weight: 500;
    line-height: 1.35;
    color: var(--fg);
  }
  .stage .line {
    font-size: 13px;
    line-height: 1.5;
    color: var(--muted);
  }

  /* ── stage 05 in the console ──────────────────────── */

  /* Only where the strip is one row: the crop starts at stage 05's column,
     which begins four sixths of the way across (the strip's 1px gaps
     aside), and a 24px hairline drops to it from the cell's bottom edge. */
  .judgment {
    display: none;
  }
  @media (min-width: 1000px) {
    .judgment {
      display: block;
      position: relative;
      margin: -40px 0 0 calc((100% - 5px) * 4 / 6 + 5px);
      padding-top: 24px;
      max-width: 360px;
    }
    .judgment::before {
      content: '';
      position: absolute;
      top: 0;
      left: 24px;
      width: 1px;
      height: 24px;
      background: var(--hair-strong);
    }
  }
  .j-frame {
    border: 1px solid var(--hair-strong);
    background: var(--ink-raised);
    padding: 8px;
  }
  .j-head {
    display: flex;
    justify-content: space-between;
    gap: 12px;
    margin: 0;
    padding: 2px 4px 8px;
    font-family: var(--mono);
    font-size: 11px;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--muted);
  }
  .j-frame img {
    display: block;
    width: 100%;
    height: auto;
  }

  /* ── the contained route ──────────────────────────── */

  .route {
    display: flex;
    flex-direction: column;
    gap: 16px;
  }
  .r-label {
    margin: 0;
    font-family: var(--mono);
    font-size: 11px;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--muted);
  }
  /* Four boxes joined by arrows; stacked, the arrows point down. */
  .steps {
    margin: 0;
    padding: 0;
    list-style: none;
    display: flex;
    flex-direction: column;
    align-items: stretch;
  }
  @media (min-width: 900px) {
    .steps {
      flex-direction: row;
    }
  }
  .step {
    flex: 1 1 0;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 6px;
    padding: 16px;
    border: 1px solid var(--hair-box);
    background: var(--ink-raised);
  }
  .step.untrusted {
    border: 1px dashed var(--text-5);
    background: transparent;
  }
  .where {
    font-family: var(--mono);
    font-size: 11px;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--muted);
  }
  .step strong {
    font-size: 15px;
    font-weight: 500;
    color: var(--fg);
  }
  .step .line {
    font-size: 13px;
    line-height: 1.5;
    color: var(--fg-dim);
  }
  .arrow {
    flex: 0 0 28px;
    position: relative;
    align-self: center;
    width: 1px;
    background: var(--text-3);
  }
  .arrow::after {
    content: '';
    position: absolute;
    left: -4px;
    bottom: 0;
    border: 4.5px solid transparent;
    border-top-color: var(--text-3);
    border-bottom: 0;
  }
  @media (min-width: 900px) {
    .arrow {
      flex-basis: 36px;
      width: 36px;
      height: 1px;
    }
    .arrow::after {
      left: auto;
      right: 0;
      bottom: -4px;
      border: 4.5px solid transparent;
      border-left-color: var(--text-3);
      border-right: 0;
    }
  }

  .doclink {
    align-self: flex-start;
    display: inline-flex;
    align-items: center;
    min-height: 44px;
    font-family: var(--mono);
    font-size: 13px;
    color: var(--fg-dim);
    text-underline-offset: 3px;
  }
  .doclink:hover {
    color: var(--fg);
  }
</style>
