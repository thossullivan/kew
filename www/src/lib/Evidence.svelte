<script lang="ts">
  import { onMount } from 'svelte';
  import {
    SHOWN,
    RUN_KINDS,
    SCORECARD,
    SPEND_LABEL,
    ANCHORS,
    LIMITS,
    canonical,
    kindLabel,
    verifyAnchors,
    type VerifiedAnchor
  } from './data/pack';
  import { AGENT_SHARE, REPO_SHOWN } from './data/repo-figures';

  /**
   * The evidence, in one section: kew's development repository (a dated
   * snapshot, data/repo-figures.js), one evidence pack from that repository
   * (data/pack-figures.js), and three of its real anchor records, which the
   * reader's browser hashes again. Each group names its window and its
   * method, so no figure in one is read as the other's (V2). Every figure is
   * marked, and the build's claims check (scripts/page-claims.mjs) holds each
   * to the string its module renders; the two bars' widths are computed from
   * the same data, never typed.
   *
   * The verification is real. These are kew's actual anchor records, and the
   * canonicalisation is the one in audit_chain.py, so the browser recomputes
   * the same bytes Python hashed. The rows arrive computed: the page's load
   * (+page.server.ts) hashes them at build time, so the prerendered HTML holds
   * the real hashes and the real count, with or without script. The status
   * always says where the hashes on screen were computed: "at build" until
   * the browser has recomputed them, "in your browser" after, and that the
   * browser cannot when Web Crypto is missing (plain http to a LAN address),
   * in which case the build rows stay and nothing can be altered. While a
   * recompute runs the status reads "verifying", never "chain intact" (design
   * review c9, C1 and A2).
   *
   * The honest limits are stated on the page rather than omitted.
   */

  let { anchors }: { anchors: VerifiedAnchor[] } = $props();

  // Seeded from the build's rows, then owned here: the reader alters them.
  // svelte-ignore state_referenced_locally
  let rows = $state<VerifiedAnchor[]>(anchors);
  let tampered = $state<number | null>(null);
  let verifying = $state(false);
  /** Where the hashes on screen were computed. */
  let source = $state<'build' | 'browser' | 'unavailable'>('build');
  /** Bumped per recompute, so a slow one never overwrites a newer one. */
  let run = 0;

  async function recompute(target: number | null): Promise<boolean> {
    const mine = ++run;
    verifying = true;
    let out: VerifiedAnchor[] | null = null;
    try {
      out = await verifyAnchors(ANCHORS, target);
    } catch {
      out = null;
    }
    if (mine !== run) return out !== null;
    verifying = false;
    if (out === null) {
      // No Web Crypto here: say so, go back to the build's rows (none of them
      // altered), and allow no alter, so no row stays altered beside a
      // disabled restore.
      source = 'unavailable';
      tampered = null;
      rows = anchors;
      return false;
    }
    rows = out;
    source = 'browser';
    return true;
  }

  onMount(() => {
    if (!globalThis.crypto?.subtle) {
      source = 'unavailable';
      return;
    }
    recompute(tampered);
  });

  function toggle(id: number) {
    if (source === 'unavailable') return;
    tampered = tampered === id ? null : id;
    recompute(tampered);
  }

  const links = $derived(rows.filter((r) => r.linkOk !== null));
  /** A broken link, or an anchor whose own hash no longer matches its record. */
  const ok = $derived(rows.every((r) => r.hashOk && r.linkOk !== false));
  const where = $derived(source === 'browser' ? 'in your browser' : 'at build');

  const short = (h: string) => `${h.slice(0, 10)}…${h.slice(-6)}`;

  /* ── the chain drawn between the cards ──────────────── */

  /** One drawn link: the path from the earlier card's sha-256 row to the
   *  later card's "prev binds" row, where its cross goes when it breaks, and
   *  its state as the rows state it. */
  type Wire = { d: string; x: number; y: number; ok: boolean };

  let chainBox: HTMLElement | null = $state(null);
  let wires = $state<Wire[]>([]);
  let wireBox = $state({ width: 0, height: 0 });

  /** Measure the rows and draw each link: across the gap when the cards sit
   *  in a row, down the left edge when they are stacked. Decoration only:
   *  the rows' text states every link's result. */
  function wire() {
    if (!chainBox) return;
    const box = chainBox.getBoundingClientRect();
    const cards = [...chainBox.querySelectorAll<HTMLElement>('.anchor')];
    const out: Wire[] = [];
    for (let i = 1; i < cards.length; i++) {
      const a = cards[i - 1].getBoundingClientRect();
      const b = cards[i].getBoundingClientRect();
      const sha = cards[i - 1].querySelector('[data-row="sha"]')?.getBoundingClientRect();
      const prev = cards[i].querySelector('[data-row="prev"]')?.getBoundingClientRect();
      if (!sha || !prev) continue;
      const y1 = sha.top + sha.height / 2 - box.top;
      const y2 = prev.top + prev.height / 2 - box.top;
      const ok = rows[i]?.linkOk === true;
      if (b.left >= a.right - 1) {
        const x1 = a.right - box.left;
        const x2 = b.left - box.left;
        const mid = (x1 + x2) / 2;
        out.push({ d: `M${x1},${y1} H${mid} V${y2} H${x2}`, x: mid, y: (y1 + y2) / 2, ok });
      } else {
        const edge = a.left - box.left;
        // Each link gets its own lane: link 1 runs down to the second card's
        // prev row and link 2 starts higher, at its sha-256 row, so on one
        // lane they would overlap and one would paint over the other.
        const lane = edge - 10 - 8 * (i % 2);
        out.push({ d: `M${edge},${y1} H${lane} V${y2} H${edge}`, x: lane, y: (y1 + y2) / 2, ok });
      }
    }
    wires = out;
    wireBox = { width: box.width, height: box.height };
  }

  $effect(() => {
    void rows;
    if (!chainBox) return;
    wire();
    const ro = new ResizeObserver(wire);
    ro.observe(chainBox);
    document.fonts?.ready.then(wire);
    return () => ro.disconnect();
  });

  const shareLabel = `${REPO_SHOWN.agentPrs} of ${REPO_SHOWN.mergedPrs} merged pull requests, ${REPO_SHOWN.agentShare}, were opened by an agent`;
</script>

<section id="evidence" class="shell" aria-labelledby="evidence-h">
  <div class="intro">
    <p class="eyebrow">Evidence</p>
    <h2 id="evidence-h">Built with itself.<br />Numbers you can check.</h2>
    <p class="lede">
      kew is the tool that built itself, under its own budgets, its own review gate and its own audit
      chain. Every merge went through the maintainer&rsquo;s GitHub account. Dollar figures are
      API-equivalent list price, not money billed.
    </p>
  </div>

  <!-- ── the development repository ─────────────────── -->

  <div class="group">
    <p class="g-label">
      kew&rsquo;s development repository &middot; <span class="nowrap"
        >to <span data-repo="captured">{REPO_SHOWN.captured}</span></span
      >
    </p>
    <div class="big-row">
      <p class="big">
        <b data-repo="agentPrs">{REPO_SHOWN.agentPrs}</b>
        <span class="of">of <span data-repo="mergedPrs">{REPO_SHOWN.mergedPrs}</span></span>
      </p>
      <p class="big-note">
        merged pull requests were opened by an agent, by the <code>agent/issue-N</code> branch name.
        Bots are in the total.
      </p>
    </div>
    <div class="bar" role="img" aria-label={shareLabel} data-bar="share">
      <span class="seg agent" style:flex-grow={AGENT_SHARE}></span>
      <span class="seg rest" style:flex-grow={1 - AGENT_SHARE}></span>
    </div>
    <p class="legend">
      <span><i class="sw agent" aria-hidden="true"></i>opened by an agent &middot;
        <span data-repo="agentShare">{REPO_SHOWN.agentShare}</span></span
      >
      <span><i class="sw rest" aria-hidden="true"></i>other branches &middot;
        <span data-repo="otherShare">{REPO_SHOWN.otherShare}</span></span
      >
    </p>
  </div>

  <!-- ── one evidence pack ──────────────────────────── -->

  <div class="group">
    <p class="g-label">
      One evidence pack &middot; <span class="nowrap" data-pack="window">{SHOWN.window}</span> &middot;
      list price
    </p>
    <div class="tiles">
      <div class="tile">
        <b data-pack="prsMerged">{SHOWN.prsMerged}</b><span>PRs merged</span>
      </div>
      <div class="tile">
        <b class="money" data-pack="costPerMergedPr">{SHOWN.costPerMergedPr}</b><span>per merged PR</span>
      </div>
      <div class="tile">
        <b class="money" data-pack="totalSpend">{SHOWN.totalSpend}</b><span>total spend</span>
      </div>
      <div class="tile">
        <b data-pack="epics">{SHOWN.epics}</b>
        <span>epics, <span data-pack="items">{SHOWN.items}</span> items</span>
      </div>
    </div>

    <p class="went">
      Where the <span class="money" data-pack="totalSpend">{SHOWN.totalSpend}</span> went.
    </p>
    <!-- Every part is money, so every part is amber: five steps of lightness
         from the page's amber down, an ink gap between neighbours. Each grows
         by its spend, so its width is its share of the gaps' remainder. -->
    <div class="bar spend" role="img" aria-label={SPEND_LABEL} data-bar="spend">
      {#each RUN_KINDS as r (r.kind)}
        <span class="seg kind-{r.kind}" style:flex-grow={r.cost}></span>
      {/each}
    </div>
    <ul class="legend kinds">
      {#each RUN_KINDS as r (r.kind)}
        <li>
          <i class="sw kind-{r.kind}" aria-hidden="true"></i>{kindLabel(r.kind)}
          <span class="money" data-pack="runKinds.{r.kind}.cost">{SHOWN.runKinds[r.kind].cost}</span>
        </li>
      {/each}
    </ul>

    <details class="fold models">
      <summary><span class="sum-text">Per-model figures and the method</span></summary>
      <div class="table-wrap">
        <table class="score">
          <thead>
            <tr>
              <th>Model</th>
              <th>Runner</th>
              <th class="num">Runs</th>
              <th class="num">Opened</th>
              <th class="num">Merged</th>
              <th class="num">Spend</th>
              <th class="num">Per merged PR</th>
            </tr>
          </thead>
          <tbody>
            <!-- Below 640px each row is a card: the model and runner on one
                 line, the figures under it, each named by its data-label (L8). -->
            {#each SCORECARD as r (r.model)}
              {@const shown = SHOWN.scorecard[r.model]}
              {@const key = `scorecard.${r.model}`}
              <tr>
                <td class="model">{r.model}</td>
                <td class="runner">{r.runner}</td>
                <td class="num" data-label="runs" data-pack="{key}.runs">{shown.runs}</td>
                <td class="num" data-label="opened" data-pack="{key}.opened">{shown.opened}</td>
                <!-- No rate column: the two counts are shown, and a rate near
                     100% would read as a success rate it is not. -->
                <td class="num" data-label="merged" data-pack="{key}.merged">{shown.merged}</td>
                <td class="num money" data-label="spend" data-pack="{key}.spend">{shown.spend}</td>
                <td class="num" class:money={r.perPr !== null} data-label="per merged PR" data-pack="{key}.perPr"
                  >{shown.perPr}</td
                >
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
      <!-- Opened is the count of distinct pull requests whose authoring run
           used the model (kew/src/kew/scorecard.py, _attribute_prs), whatever
           their state; Runs counts every run of the model, reviews included. -->
      <p class="foot-note">
        From the evidence pack <code>kew report export</code> wrote for kew&rsquo;s own repository.
        Opened counts the pull requests each model wrote; runs count all of its runs, reviews
        included. The models ran in different periods and on different work, so the per-PR figures
        are not a like-for-like comparison.
      </p>
      <p class="foot-note">
        <span data-pack="unattributedRuns">{SHOWN.unattributedRuns}</span> runs carry no recorded model.
        They are in the totals rather than dropped from them, so the cost per merged PR is not
        flattered by hiding them.
      </p>
      <!-- report_export.py takes each PR's last record with a state for the
           headline; scorecard.py takes the run that wrote it for this column.
           The pack is a frozen export: kew's ledger recorded more merges for
           this window after it was written (checked against the ledger on
           2026-11-12), so both figures are low. -->
      <p class="foot-note">
        The merged column sums to <span data-pack="scorecardMerged">{SHOWN.scorecardMerged}</span>, the
        headline to <span data-pack="prsMerged">{SHOWN.prsMerged}</span>: the column reads each
        PR&rsquo;s state from the run that wrote it, the headline from the PR&rsquo;s latest run record.
        The pack was written before kew had recorded every merge in this window, so both figures
        are lower than the ledger&rsquo;s count today.
      </p>
    </details>
  </div>

  <!-- ── live verification ────────────────────────── -->

  <div class="verify">
    <div class="v-head">
      <div>
        <p class="g-label">Verify it here</p>
        <p class="v-lede">
          Three of kew&rsquo;s real anchor records, hashed with the algorithm kew uses: each hash is
          checked against the record, each <code>prev</code> against the whole previous record.
        </p>
      </div>
      <!-- Status is a word and a mark, never colour alone. -->
      <div class="v-state" role="status" aria-live="polite">
        {#if verifying}
          <span class="dim"><i class="mark run" aria-hidden="true"></i>verifying</span>
        {:else if ok}
          <span class="ok"><i class="mark pass" aria-hidden="true"></i>chain intact</span>
          <span class="said"
            >{rows.length} hashes, {links.length} link{links.length === 1 ? '' : 's'} verified {where}</span
          >
          {#if source === 'unavailable'}
            <span class="said">your browser cannot recompute the hashes here</span>
          {/if}
        {:else}
          <span class="bad-t"><i class="mark fail" aria-hidden="true">&times;</i>chain broken</span>
          <span class="said">kew audit verify exits 1</span>
        {/if}
      </div>
    </div>

    <!-- Three cards in a row, stacked on a phone. Each link is drawn from the
         earlier card's sha-256 row to the later card's prev row: green while
         it verifies, red with a cross once an alter breaks it. The drawing is
         decoration; the rows say the same in words. -->
    <div class="anchors" bind:this={chainBox}>
      {#if wires.length}
        <svg
          class="wires"
          width={wireBox.width}
          height={wireBox.height}
          viewBox="0 0 {wireBox.width} {wireBox.height}"
          aria-hidden="true"
        >
          {#each wires as w, i (i)}
            <path class="wire" class:cut={!w.ok} data-wire={w.ok ? 'ok' : 'cut'} d={w.d} />
            {#if !w.ok}
              <g class="cross" transform="translate({w.x},{w.y})">
                <circle r="8" />
                <path d="M-3.5,-3.5 L3.5,3.5 M3.5,-3.5 L-3.5,3.5" />
              </g>
            {/if}
          {/each}
        </svg>
      {/if}
      {#each rows as r, i (r.id)}
        <div class="anchor" class:broken={r.linkOk === false || !r.hashOk} class:edited={r.edited}>
          <div class="a-top">
            <span class="a-id">anchor #{r.id}</span>
            <button
              type="button"
              class="btn-quiet a-btn"
              aria-label="{tampered === r.id ? 'Restore' : 'Alter'} anchor #{r.id}"
              disabled={source === 'unavailable'}
              onclick={() => toggle(r.id)}
            >
              {tampered === r.id ? 'restore' : 'alter'}
            </button>
          </div>
          <p class="a-meta dim">
            {r.timestamp.slice(0, 10)} &middot; count {r.count}{r.edited ? ' (altered)' : ''}
          </p>

          <div class="a-row">
            <span class="a-k">canonical</span>
            <code class="a-canon">{canonical(r)}</code>
          </div>
          <div class="a-row" data-row="sha">
            <span class="a-k">sha-256</span>
            <code class="a-hash" class:bad-t={!r.hashOk}>{short(r.computed)}</code>
          </div>
          <p class="a-verdict">
            {#if r.hashOk}
              <span class="ok">matches the record</span>
            {:else}
              <span class="bad-t">does not match the record ({short(r.recorded)})</span>
            {/if}
          </p>
          <div class="a-row" data-row="prev">
            <span class="a-k">prev binds</span>
            {#if r.prev !== null}
              <code class="a-hash" class:bad-t={r.linkOk === false} class:ok={r.linkOk}>{short(r.prev)}</code>
            {:else}
              <span class="dim">nothing</span>
            {/if}
          </div>
          <p class="a-verdict">
            {#if r.prev === null}
              <span class="said"
                >Written before the anchor chain existed, so this link cannot be checked.</span
              >
            {:else if r.linkOk}
              <span class="ok">matches anchor #{rows[i - 1]?.id}</span>
            {:else}
              <span class="bad-t">does not match anchor #{rows[i - 1]?.id}</span>
            {/if}
          </p>
        </div>
      {/each}
    </div>

    <details class="fold v-fold">
      <summary><span class="sum-text">How the anchors are made</span></summary>
      <p class="v-note">
        A scheduled job on the ledger host commits anchors to git, out of band. It does not run
        every day; the record has gaps of weeks. Rewriting the audit log would mean rewriting that
        git history too, which is what makes a silent re-chain detectable rather than merely
        unlikely.
      </p>
    </details>
  </div>

  <!-- ── honest limits ────────────────────────────── -->

  <div class="limits">
    <div class="lim">
      <p class="g-label">What you can check from this page</p>
      <ul>
        {#each LIMITS.can as line (line)}<li>{line}</li>{/each}
      </ul>
    </div>
    <div class="lim">
      <p class="g-label">What you cannot</p>
      <ul class="cannot">
        {#each LIMITS.cannot as line (line)}<li>{line}</li>{/each}
      </ul>
    </div>
  </div>
</section>

<style>
  section {
    padding-block: clamp(64px, 9vw, 96px);
    border-top: 1px solid var(--line-soft);
    display: flex;
    flex-direction: column;
    gap: clamp(40px, 5vw, 56px);
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
  .lede {
    font-size: 15px;
    line-height: 1.6;
    color: var(--muted);
    margin: 0;
    max-width: 62ch;
  }

  .group {
    display: flex;
    flex-direction: column;
    gap: 16px;
    min-width: 0;
  }
  .g-label {
    font-family: var(--mono);
    font-size: 11px;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--muted);
    margin: 0;
  }

  /* ── the repository's share ───────────────────────── */

  .big-row {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 8px 48px;
  }
  .big {
    margin: 0;
    font-family: var(--mono);
    font-weight: 600;
    line-height: 1;
    font-variant-numeric: tabular-nums;
  }
  .big b {
    font-size: 44px;
    letter-spacing: -0.02em;
    color: var(--fg);
  }
  .big .of {
    font-size: 26px;
    font-weight: 400;
    color: var(--muted);
  }
  .big-note {
    margin: 0;
    max-width: 44ch;
    font-size: 15px;
    line-height: 1.6;
    color: var(--fg-dim);
  }
  .big-note code {
    font-family: var(--mono);
    font-size: 13px;
  }

  /* A hairline box; each part grows by its count or its spend from a zero
     basis, so its width is its share. */
  .bar {
    display: flex;
    height: 28px;
    border: 1px solid var(--hair-strong);
  }
  .seg {
    display: block;
    height: 100%;
    flex: 0 1 0;
    min-width: 0;
  }
  /* The spend bar's parts sit apart on a 1px ink gap. */
  .bar.spend {
    gap: 1px;
    background: var(--ink);
  }
  .agent {
    background: var(--text-1);
  }
  .rest {
    background: transparent;
  }
  /* Five steps of one amber, from the page's own down to a dark one, so no
     two neighbours read alike. */
  .kind-dispatch {
    background: var(--warn);
  }
  .kind-review {
    background: color-mix(in oklab, var(--warn) 78%, var(--ink));
  }
  .kind-adversarial_review {
    background: color-mix(in oklab, var(--warn) 58%, var(--ink));
  }
  .kind-review_remediation {
    background: color-mix(in oklab, var(--warn) 40%, var(--ink));
  }
  .kind-planning {
    background: color-mix(in oklab, var(--warn) 24%, var(--ink));
  }

  .legend {
    display: flex;
    flex-wrap: wrap;
    gap: 6px 28px;
    margin: 0;
    padding: 0;
    list-style: none;
    font-family: var(--mono);
    font-size: 13px;
    color: var(--fg-dim);
  }
  .legend > span,
  .legend li {
    display: inline-flex;
    align-items: center;
    gap: 8px;
  }
  .sw {
    flex: none;
    display: inline-block;
    width: 10px;
    height: 10px;
  }
  .sw.rest {
    border: 1px solid var(--text-5);
  }

  /* ── the pack ─────────────────────────────────────── */

  .tiles {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
    gap: 1px;
    background: var(--line-soft);
    border: 1px solid var(--line-soft);
  }
  .tile {
    background: var(--bg);
    padding: 20px clamp(12px, 2vw, 24px);
    min-width: 0;
  }
  .tile b {
    display: block;
    font-family: var(--mono);
    font-size: 26px;
    font-weight: 600;
    letter-spacing: -0.02em;
    font-variant-numeric: tabular-nums;
    color: var(--fg);
  }
  .tile > span {
    display: block;
    margin-top: 6px;
    font-family: var(--mono);
    font-size: 11px;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--muted);
  }
  .tile b.money,
  .money {
    color: var(--warn);
  }

  .went {
    margin: 8px 0 0;
    font-size: 15px;
    color: var(--fg-dim);
  }

  .fold {
    border-top: 1px solid var(--line-soft);
    border-bottom: 1px solid var(--line-soft);
  }
  .fold summary {
    display: flex;
    align-items: center;
    gap: 10px;
    min-height: 44px;
    font-family: var(--mono);
    font-size: 13px;
    color: var(--fg-dim);
    cursor: pointer;
    list-style: none;
  }
  .fold summary::-webkit-details-marker {
    display: none;
  }
  .fold summary::before {
    content: '\25B8';
    flex: none;
    transition: transform 160ms ease;
  }
  .fold[open] summary::before {
    transform: rotate(90deg);
  }
  .fold summary:hover,
  .fold summary:focus-visible {
    color: var(--fg);
  }
  .fold summary:hover .sum-text {
    text-decoration: underline;
    text-underline-offset: 3px;
  }
  .fold[open] {
    padding-bottom: 20px;
  }

  .table-wrap {
    overflow-x: auto;
    border: 1px solid var(--line-soft);
    margin-top: 8px;
  }
  .score {
    border-collapse: collapse;
    width: 100%;
    font-family: var(--mono);
    font-size: 13px;
    min-width: 720px;
  }
  .score th {
    font-size: 11px;
    letter-spacing: 0.13em;
    text-transform: uppercase;
    color: var(--muted);
    font-weight: 600;
    text-align: left;
    padding: 11px 16px;
    background: var(--surface);
    border-bottom: 1px solid var(--line-soft);
    white-space: nowrap;
  }
  .score th.num {
    text-align: right;
  }
  .score td {
    padding: 10px 16px;
    border-bottom: 1px solid var(--line-soft);
    white-space: nowrap;
    color: var(--fg-dim);
  }
  .score tbody tr:last-child td {
    border-bottom: none;
  }
  .score td.model {
    color: var(--fg);
  }
  .score td.runner {
    color: var(--muted);
  }
  .num {
    text-align: right;
    font-variant-numeric: tabular-nums;
  }
  /* On a phone each model is a card: the model and its runner, then the
     figures in a grid of label over value (L8, R8). */
  @media (max-width: 639.98px) {
    .score,
    .score tbody {
      display: block;
      min-width: 0;
    }
    .score thead {
      display: none;
    }
    .score tbody tr {
      display: grid;
      grid-template-columns: repeat(6, minmax(0, 1fr));
      gap: 10px 12px;
      padding: 14px 16px;
      border-bottom: 1px solid var(--line-soft);
    }
    .score tbody tr:last-child {
      border-bottom: none;
    }
    .score td {
      padding: 0;
      border-bottom: none;
      text-align: left;
      white-space: normal;
    }
    .score td.model {
      grid-column: 1 / 5;
      overflow-wrap: anywhere;
    }
    .score td.runner {
      grid-column: span 2;
      text-align: right;
    }
    .score td[data-label] {
      grid-column: span 2;
    }
    .score td[data-label='spend'],
    .score td[data-label='per merged PR'] {
      grid-column: span 3;
    }
    .score td[data-label]::before {
      content: attr(data-label);
      display: block;
      font-size: 11px;
      color: var(--muted);
    }
  }

  .foot-note {
    font-size: 13px;
    line-height: 1.5;
    color: var(--muted);
    max-width: 72ch;
    margin: 12px 0 0;
  }
  .foot-note code {
    font-family: var(--mono);
  }

  /* ── verification ─────────────────────────────────── */

  .verify {
    border: 1px solid var(--border-strong);
  }
  .v-head {
    display: grid;
    gap: 16px;
    padding: 20px 24px;
    border-bottom: 1px solid var(--line-soft);
  }
  @media (min-width: 860px) {
    .v-head {
      grid-template-columns: minmax(0, 1fr) auto;
      align-items: start;
      gap: 36px;
    }
  }
  .v-head .g-label {
    margin-bottom: 8px;
  }
  .v-lede {
    font-size: 13px;
    line-height: 1.6;
    color: var(--fg-dim);
    margin: 0;
    max-width: 78ch;
  }
  .v-lede code {
    font-family: var(--mono);
  }
  /* Under the lede on a narrow screen, beside it from 860px; read from the
     left either way. The status word is mono; what follows it is a sentence,
     in sans, on one line on a wide screen. */
  .v-state {
    font-family: var(--mono);
    font-size: 11px;
    display: flex;
    flex-direction: column;
    gap: 3px;
    text-align: left;
  }
  .said {
    font-family: var(--sans);
    font-size: 13px;
    line-height: 1.45;
    color: var(--muted);
  }
  @media (min-width: 860px) {
    .v-state .said {
      white-space: nowrap;
    }
  }

  /* Stacked, the links run down the left edge, so the cards stand in from
     it; in a row, across the gap between them. */
  .anchors {
    position: relative;
    display: flex;
    flex-direction: column;
    align-items: stretch;
    gap: 24px;
    padding: 24px 24px 24px 40px;
  }
  @media (min-width: 900px) {
    .anchors {
      flex-direction: row;
      gap: 40px;
      padding-left: 24px;
    }
  }
  .wires {
    position: absolute;
    inset: 0;
    pointer-events: none;
    overflow: visible;
  }
  .wire {
    fill: none;
    stroke: var(--success);
    stroke-width: 1.5;
  }
  .wire.cut {
    stroke: var(--danger);
    stroke-dasharray: 4 3;
  }
  .cross circle {
    fill: var(--ink-raised);
    stroke: var(--danger);
    stroke-width: 1.5;
  }
  .cross path {
    stroke: var(--danger);
    stroke-width: 1.5;
  }
  .anchor {
    flex: 1 1 0;
    min-width: 0;
    border: 1px solid var(--hair-box);
    background: var(--ink-raised);
    padding: 14px 16px;
    font-family: var(--mono);
    font-size: 11px;
    transition: background 240ms ease;
  }
  .anchor.broken {
    background: color-mix(in oklab, var(--danger) 7%, var(--ink-raised));
  }
  .anchor.edited {
    background: color-mix(in oklab, var(--danger) 11%, var(--ink-raised));
  }

  .a-top {
    display: flex;
    align-items: center;
    gap: 8px 16px;
  }
  .a-id {
    color: var(--fg);
    font-weight: 600;
    font-size: 13px;
  }
  /* The look comes from the page's one quiet button (app.css); a 44px
     target. */
  .a-btn {
    margin-left: auto;
    min-height: 44px;
    min-width: 72px;
  }
  .a-meta {
    margin: 2px 0 10px;
  }
  .a-row {
    display: flex;
    gap: 12px;
    align-items: baseline;
    min-width: 0;
    padding-top: 4px;
  }
  .a-k {
    flex: none;
    width: 11ch;
    color: var(--muted);
  }
  .a-canon {
    min-width: 0;
    color: var(--muted);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .a-hash {
    color: var(--fg-dim);
  }
  .a-verdict {
    margin: 2px 0 0 calc(11ch + 12px);
  }

  .dim {
    color: var(--muted);
  }
  .ok {
    color: var(--success);
  }
  /* A broken link is a failure (red with a cross), not a person's next move. */
  .bad-t {
    color: var(--danger);
  }
  .mark {
    display: inline-block;
    width: 7px;
    height: 7px;
    margin-right: 7px;
    vertical-align: 1px;
    font-style: normal;
  }
  .mark.pass {
    background: var(--success);
    border-radius: 50%;
  }
  .mark.run {
    border: 2px solid var(--accent-2);
    border-radius: 50%;
  }
  .mark.fail {
    width: auto;
    height: auto;
    vertical-align: 0;
  }

  .v-fold {
    border-bottom: 0;
    padding: 0 24px;
  }
  .v-fold[open] {
    padding-bottom: 14px;
  }
  .v-note {
    font-size: 13px;
    line-height: 1.55;
    color: var(--muted);
    margin: 0;
    max-width: 72ch;
  }

  /* ── limits ───────────────────────────────────────── */

  .limits {
    display: grid;
    gap: 1px;
    background: var(--line-soft);
    border: 1px solid var(--line-soft);
  }
  @media (min-width: 860px) {
    .limits {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }
  }
  .lim {
    background: var(--bg);
    padding: 20px 24px 22px;
  }
  .lim .g-label {
    margin-bottom: 12px;
  }
  .lim ul {
    margin: 0;
    padding: 0;
    list-style: none;
    display: flex;
    flex-direction: column;
    gap: 9px;
  }
  .lim li {
    font-size: 13px;
    line-height: 1.5;
    color: var(--fg-dim);
    padding-left: 16px;
    position: relative;
  }
  .lim li::before {
    content: '\002B';
    position: absolute;
    left: 0;
    color: var(--success);
  }
  .cannot li {
    color: var(--muted);
  }
  .cannot li::before {
    content: '\2212';
    color: var(--muted);
  }
</style>
