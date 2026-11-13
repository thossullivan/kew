/**
 * The live gallery takes an epic's linear shot only when the epic is one chain
 * (#2899).
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 *
 * The linear shot is the plain epic page. For a branching epic that page is
 * the graph shot again, which is why `planShots` drops it. This serves an epic
 * under a long ref (`page.route`), once branching and once as a chain, reads
 * the payload back through the page the way `liveEpics` reads a live serve,
 * and holds the plan and the page to each other at both widths.
 */
// ui-check-owns: scripts/gallery.mjs
// ui-check-owns: src/routes/(factory)/epics/[ref]/+page.svelte
import { isSingleChain, planShots } from '../gallery.mjs';

/** The fixture epic whose page the served payloads borrow (it branches). */
const FIXTURE_REF = 'go-live-2026-09';
/** A long, served ref: the page and the shot names must carry it whole. */
const REF = 'live-epic-with-a-deliberately-long-reference-name-that-runs-well-past-one-line-2026-10';

/** The same epic redrawn as one chain: every node after the first depends on the one before. */
function asChain(snapshot) {
  const ids = snapshot.nodes.map((n) => n.id);
  return { ...snapshot, edges: ids.slice(1).map((id, i) => [ids[i], id]) };
}

/** Serve `REF` as the fixture epic, its snapshot swapped by `shape`. */
async function serve(page, base, shape) {
  const detail = await (await fetch(`${base}/api/epics/${FIXTURE_REF}`)).json();
  const long = 'a served node title long enough to wrap twice at a phone width, not the fixture short text ';
  const snapshot = shape({
    ...detail.snapshot,
    epic_ref: REF,
    nodes: detail.snapshot.nodes.map((n) => ({ ...n, title: `${long}${n.title}` }))
  });
  const body = JSON.stringify({ ...detail, epic_ref: REF, snapshot });
  await page.route(`**/api/epics/${REF}**`, (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === `/api/epics/${REF}`) {
      return route.fulfill({ status: 200, contentType: 'application/json', body });
    }
    url.pathname = url.pathname.replace(REF, FIXTURE_REF);
    return route.continue({ url: url.toString() });
  });
}

/** The node ids the canvas drew. */
async function drawn(page) {
  const nodes = page.locator('.dag .svelte-flow__node');
  await nodes.first().waitFor({ state: 'visible', timeout: 15_000 });
  return (await nodes.evaluateAll((els) => els.map((el) => el.getAttribute('data-id')))).sort();
}

export async function checks({ base, check, visit, expect }) {
  const cases = [
    { label: 'a branching', shape: (s) => s, linear: false },
    { label: 'a single-chain', shape: asChain, linear: true }
  ];
  for (const width of [1440, 390]) {
    const size = { width, height: width === 390 ? 844 : 900 };
    for (const c of cases) {
      await check(`the live gallery's linear shot of ${c.label} epic at ${width}px (#2899)`, async () => {
        let graph = null;
        await visit(
          `/epics/${REF}?view=graph`,
          async (page) => {
            graph = await drawn(page);
          },
          size,
          (page) => serve(page, base, c.shape)
        );
        await visit(
          `/epics/${REF}`,
          async (page, problems) => {
            const read = await page.evaluate((r) => fetch(`/api/epics/${r}`).then((res) => res.json()), REF);
            const linear = isSingleChain(read.snapshot);
            expect(linear === c.linear, `${width}px: ${c.label} epic read as linear=${linear}`);
            const { shots, skipped } = planShots([], {
              live: true,
              epics: [{ ref: REF, why: 'progressing', linear }]
            });
            const name = `live-epic-linear-${REF}`;
            const taken = shots.some((s) => s.name === name);
            const skip = skipped.find((s) => s.name === name);
            expect(taken === c.linear, `${width}px: ${c.label} epic's linear shot taken=${taken}`);
            expect(c.linear || /single chain/.test(skip?.reason ?? ''), `${width}px: no reason (${skip?.reason})`);
            expect(shots.some((s) => s.name === `live-epic-graph-${REF}`), `${width}px: graph shot dropped`);
            // The premise: the plain page is the graph shot again.
            const plain = await drawn(page);
            expect(
              JSON.stringify(plain) === JSON.stringify(graph),
              `${width}px: the plain page drew ${plain.length} nodes, the graph view ${graph?.length}`
            );
            const wide = await page.evaluate(() => ({
              scroll: document.documentElement.scrollWidth,
              vw: document.documentElement.clientWidth
            }));
            expect(wide.scroll <= wide.vw, `${width}px: the page scrolls sideways (${wide.scroll} > ${wide.vw})`);
            const thrown = problems.filter((p) => p.startsWith('page error:'));
            expect(thrown.length === 0, thrown.join('\n'));
          },
          size,
          (page) => serve(page, base, c.shape)
        );
      });
    }
  }
}
