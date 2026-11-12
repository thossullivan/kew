/**
 * How each surface was built, on the static demo build (#3144).
 *
 * A demo module (`demo-*.mjs`): `scripts/ui-check.mjs` runs it only against
 * the static server for `build-demo/`, and `npm run demo-check` against any
 * demo base. The epic graph's ref comes from the world's manifest and every
 * expected figure from the provenance file the base serves
 * (`/demo-provenance.json`): the committed sample, unless the build carried
 * the real one — which a deployed build can while the checkout does not, so
 * against a remote base the checkout is never asked. Against the local
 * `build-demo/` the served file must also be the one the checkout selects
 * (`servedProvenance`), or a build that shipped the wrong file would pass
 * against its own figures. The served file is read inside a check, never at
 * import.
 *
 * - the epic graph's page foot reads "Built by N agent runs across M PRs,
 *   $X list cost." with the sample's figures, in Plex Mono, below `main`;
 * - a sample build ends that line with "sample data", plain 13px text in
 *   `--text-3`; a build of the real file draws no such word;
 * - its toggle is a button with a 13px label; it arrives shut
 *   (`aria-expanded="false"`, no PR showing), and pressing it opens the
 *   region its `aria-controls` names, which lists the feature's PRs and says
 *   costs are list price. Every build draws the PR numbers as plain text
 *   and no link at all: the real PRs are in kew's private development
 *   repository, and the sample repository is an invented owner's, someone
 *   else's URL space;
 * - at 390px nothing scrolls sideways, open or closed, and the toggle is at
 *   least 32px tall;
 * - a surface no feature names draws no foot.
 */
// ui-check-owns: src/lib/demo/BuildHistory.svelte
// ui-check-owns: src/lib/demo/build-history.ts
import { readFileSync } from 'node:fs';
import { FRONTEND, servedProvenance, widestBox } from '../ui-check-lib.mjs';

const read = (path) => JSON.parse(readFileSync(new URL(path, FRONTEND), 'utf8'));
const usd = (n) => `$${n.toFixed(2)}`;
const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;

/** The text of the row headed `label` in feature `id`'s Build history table. */
async function ledgerCell(page, id, label) {
  return page.evaluate(
    ([feature, want]) => {
      const table = document.querySelector(`[data-build-feature="${feature}"]`);
      const row = [...(table?.querySelectorAll('tr') ?? [])].find((tr) => tr.querySelector('th')?.textContent?.trim() === want);
      return row ? (row.querySelector('td')?.textContent ?? '').replace(/\s+/g, ' ').trim() : null;
    },
    [id, label]
  );
}

/** A recorded code as the ledger words it: `NEEDS_CHANGES` → `needs changes`. */
const worded = (code) => code.toLowerCase().replace(/_+/g, ' ');
const counted = (counts) => Object.entries(counts).filter(([, n]) => n > 0);

/**
 * Fail unless feature `f`'s Review rounds, Held-out and Integrity rows say the
 * numbers the provenance file holds for it.
 */
async function readsFeature(page, expect, f, doc, at) {
  const where = `${at}: ${f.id}`;
  const rounds = await ledgerCell(page, f.id, 'Review rounds');
  const tally = counted(f.review_rounds);
  expect(
    rounds !== null && (tally.length === 0 ? rounds === 'None.' : tally.every(([code, n]) => rounds.includes(`${worded(code)} ${n}`))),
    `${where}'s Review rounds read "${rounds}", the file has ${JSON.stringify(f.review_rounds)}`
  );
  const held = await ledgerCell(page, f.id, 'Held-out');
  const h = f.held_out;
  const heldOk =
    held !== null &&
    (h.tests_failed === 0 && h.items_failed === 0
      ? held === 'No held-out test failed.'
      : held.startsWith(`${h.tests_failed} test`) &&
        held.includes(`on ${h.items_failed} item`) &&
        (h.tests_undeclared === 0 || held.includes(`${h.tests_undeclared} undeclared`)));
  expect(heldOk, `${where}'s Held-out reads "${held}", the file has ${JSON.stringify(h)}`);
  const integrity = await ledgerCell(page, f.id, 'Integrity');
  const g = f.integrity;
  const integrityOk =
    integrity !== null &&
    (doc.integrity_source === 'none'
      ? integrity.startsWith('Not read')
      : g.runs_flagged === 0
        ? integrity === 'No run flagged.'
        : integrity.startsWith(`${g.runs_flagged} run`) &&
          counted(g.flags).every(([code, n]) => integrity.includes(`${worded(code)} ${n}`)));
  expect(integrityOk, `${where}'s Integrity reads "${integrity}", the file has ${JSON.stringify(g)}`);
}

/** What the served file says the epic graph's foot reads, and whether it is the real export. */
async function served(base, remote) {
  const { real, doc } = await servedProvenance(base, { remote });
  const graph = doc.features.find((f) => f.id === 'epic-graph');
  if (!graph) throw new Error(`${base}/demo-provenance.json names no epic-graph feature`);
  const runs = Object.values(graph.runs).reduce((a, b) => a + b, 0);
  const line = `Built by ${plural(runs, 'agent run', 'agent runs')} across ${plural(graph.prs.length, 'PR', 'PRs')}, ${usd(graph.list_cost_usd)} list cost.`;
  return { real, graph, line };
}

export async function checks({ base, check, visit, expect, remote = false }) {
  const manifest = read('demo/world/manifest.json');
  const route = `/epics/${manifest.epic_ref}`;
  let figures = null;
  const figuresOf = async () => (figures ??= await served(base, remote));

  const foot = async (page) => {
    const el = page.locator('[data-build-history="ready"]');
    await el.waitFor({ state: 'visible', timeout: 10_000 });
    return el;
  };
  // The disclosure's toggle: a button, as the epic page's ticker toggle is.
  const toggleOf = (el) => el.locator('button[data-build-toggle]');

  await check("demo provenance: the epic graph says how it was built, with the served file's figures", async () => {
    const { line } = await figuresOf();
    await visit(route, async (page) => {
      const el = await foot(page);
      const summary = el.locator('[data-build-summary]');
      const said = ((await summary.textContent()) ?? '').trim();
      expect(said === line, `the epic graph's foot reads "${said}"`);
      const family = await summary.evaluate((node) => getComputedStyle(node).fontFamily);
      expect(/Plex Mono/.test(family), `the figures are drawn in ${family}`);
      const [f, m] = await Promise.all([el.boundingBox(), page.locator('main').first().boundingBox()]);
      expect(f !== null && m !== null && f.y >= m.y + m.height - 0.5, 'the foot overlaps the page');
      // A small button's label is 13px (#3130), in mono.
      const size = await toggleOf(el).evaluate((node) => getComputedStyle(node).fontSize);
      expect(size === '13px', `the toggle's label is ${size}`);
    });
  });

  await check('demo provenance: a sample build ends the line with "sample data", a build of the real export says nothing', async () => {
    const { real } = await figuresOf();
    await visit(route, async (page) => {
      const el = await foot(page);
      const label = el.locator('[data-build-sample]');
      if (real) {
        expect((await label.count()) === 0, 'the real build calls its line sample data');
        return;
      }
      expect((await label.count()) === 1, 'the sample build does not say its line is sample data');
      const said = ((await label.textContent()) ?? '').trim();
      expect(said === 'sample data', `the label reads "${said}"`);
      // Plain 13px text in the muted token (DESIGN.md), not a badge.
      const look = await label.evaluate((node) => {
        const probe = document.createElement('span');
        probe.style.color = 'var(--text-3)';
        node.parentElement.append(probe);
        const want = getComputedStyle(probe).color;
        probe.remove();
        const cs = getComputedStyle(node);
        return { size: cs.fontSize, color: cs.color, want, border: cs.borderTopWidth, bg: cs.backgroundColor };
      });
      expect(look.size === '13px', `the label is ${look.size}`);
      expect(look.color === look.want, `the label is ${look.color}, not --text-3 (${look.want})`);
      expect(look.border === '0px' && look.bg === 'rgba(0, 0, 0, 0)', `the label is drawn as a badge: ${JSON.stringify(look)}`);
      const summary = await el.locator('[data-build-summary]').boundingBox();
      const box = await label.boundingBox();
      expect(summary !== null && box !== null && box.x >= summary.x + summary.width - 0.5, 'the label is not after the summary');
    });
  });

  await check('demo provenance: expanding the foot lists the PRs as text, never links, and says the cost is list price', async () => {
    const { graph } = await figuresOf();
    await visit(route, async (page) => {
      const el = await foot(page);
      const toggle = toggleOf(el);
      const prs = el.locator('[data-build-pr]');
      // Shut on arrival: the ledger region is hidden and the button says so.
      expect((await toggle.getAttribute('aria-expanded')) === 'false', 'the foot arrives open');
      expect(!(await prs.first().isVisible()), 'a PR shows while the foot is shut');
      await toggle.click();
      expect((await toggle.getAttribute('aria-expanded')) === 'true', 'the toggle does not say it opened');
      const region = page.locator(`#${await toggle.getAttribute('aria-controls')}`);
      expect((await region.count()) === 1 && (await region.isVisible()), 'aria-controls names no visible region');
      await prs.first().waitFor({ state: 'visible', timeout: 5_000 });
      const graphPrs = el.locator('[data-build-feature="epic-graph"] [data-build-pr]');
      const numbers = graph.prs.map((pr) => `#${pr.url.split('/').at(-1)}`);
      // The real PRs are in kew's private development repository, and the
      // samples' repository is an invented owner's, someone else's URL space:
      // either way the ledger names the numbers and links nothing.
      const anchors = await el.locator('a').count();
      expect(anchors === 0, `the ledger draws ${anchors} link(s)`);
      const said = await graphPrs.evaluateAll((ns) => ns.map((n) => (n.textContent ?? '').trim()));
      expect(JSON.stringify(said) === JSON.stringify(numbers), `the ledger's PRs read ${JSON.stringify(said)}`);
      const note = ((await el.locator('[data-build-note]').textContent()) ?? '').trim();
      expect(/API-equivalent list price/.test(note), `the ledger says nothing about list price: "${note}"`);
      const cost = ((await el.locator(`[data-build-feature="epic-graph"]`).textContent()) ?? '').includes(usd(graph.list_cost_usd));
      expect(cost, `the ledger does not show ${usd(graph.list_cost_usd)}`);
    });
  });

  // Moved here from the refused-PR tour's Build history step, which the tour
  // no longer has (design review c9): the foot still ships these rows.
  await check("demo provenance: Work's foot reads each feature's review rounds, held-out and integrity as the served file has them", async () => {
    const { doc } = await servedProvenance(base, { remote });
    const features = doc.features.filter((f) => f.routes.includes('/work'));
    expect(features.length > 0, 'the provenance file names no feature on /work');
    await visit('/work', async (page) => {
      const el = await foot(page);
      await toggleOf(el).click();
      expect((await toggleOf(el).getAttribute('aria-expanded')) === 'true', 'the foot did not open');
      for (const f of features) await readsFeature(page, expect, f, doc, '/work');
    });
  });

  await check('demo provenance: at 390px the foot never scrolls sideways and its toggle is 32px tall', async () => {
    await visit(
      route,
      async (page) => {
        const el = await foot(page);
        const toggle = await toggleOf(el).boundingBox();
        expect(toggle !== null && toggle.height >= 32, `the toggle is ${toggle?.height}px tall`);
        let wide = await widestBox(page);
        expect(wide.w <= wide.c, `closed, 390px scrolls sideways: ${wide.culprit}`);
        await toggleOf(el).click();
        await el.locator('[data-build-pr]').first().waitFor({ state: 'visible', timeout: 5_000 });
        wide = await widestBox(page);
        expect(wide.w <= wide.c, `open, 390px scrolls sideways: ${wide.culprit}`);
      },
      { width: 390, height: 844 }
    );
  });

  await check('demo provenance: a surface no feature names draws no foot', async () => {
    await visit('/health', async (page) => {
      await page.locator('main').first().waitFor({ state: 'visible', timeout: 10_000 });
      await page.waitForFunction(() => !document.querySelector('[data-build-history="loading"]'), null, { timeout: 5_000 });
      const count = await page.locator('[data-build-history]').count();
      expect(count === 0, `/health draws ${count} build-history element(s)`);
    });
  });
}
