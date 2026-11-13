/**
 * A `?node=` link followed ON the epic page opens that node's popover, as a
 * first load of one does, and a popover the reader shut stays shut (#3149).
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 *
 * `?node=` names the SELECTION: a close with × or Esc keeps the node in the
 * address. `DagView` opens a linked node's popover on the canvas's first frame
 * only, so a link to a node followed with the page already open (same epic, no
 * remount) selected the node and left its popover shut. The page now watches
 * `page.url`, which every navigation moves and a `replaceState` does not, and
 * opens the popover when a navigation lands on a node the address did not
 * name before, or from another page (`linkOpensPopover` in
 * `$lib/epic/node-popover`). A navigation onto the node already selected —
 * the same address with another key added, as a demo tour's Next makes —
 * leaves a shut popover shut.
 *
 * Checks, at a desktop and a phone size, each with the popover shut before
 * the navigation that must open it:
 * - a link to a node clicked on the open page opens its popover;
 * - Back and then Forward, each to another node's link, open that node's;
 * - Esc shuts it, the node stays in the address, and it stays shut through
 *   the page's polls (about 3 s);
 * - after a close, a navigation to the same node with an unrelated key added
 *   leaves it shut.
 *
 * Nodes 2000 and 2021 have no node-detail fixture; their popovers open all
 * the same, and the 404s of those reads are not what this checks.
 */
// ui-check-owns: src/routes/(factory)/epics/[ref]/**
// ui-check-owns: fixtures/factory/epics-launch-fixes*.json
import { EPIC } from '../ui-check-lib.mjs';

const SIZES = [
  { width: 1440, height: 960 },
  { width: 390, height: 844 }
];
const FIRST = '2000';
const SECOND = '2021';
const POPOVER = '[role="dialog"].pop';
/** Longer than the page's 2 s local poll, so at least one poll lands in it. */
const POLLS_MS = 3_000;

/** Click an in-page link to `?<search>`: a client-side navigation on the same page. */
async function follow(page, search) {
  await page.evaluate(
    ([path, query]) => {
      document.getElementById('ui-check-node-link')?.remove();
      const a = document.createElement('a');
      a.id = 'ui-check-node-link';
      a.href = `${path}?${query}`;
      a.textContent = query;
      document.querySelector('main')?.prepend(a);
    },
    [EPIC, search]
  );
  await page.locator('#ui-check-node-link').click();
}

/** Wait for the address to name `?node=<id>`. */
async function addressed(page, expect, id, what) {
  try {
    await page.waitForFunction((node) => new URL(location.href).searchParams.get('node') === node, id, { timeout: 10_000 });
  } catch {
    const url = new URL(page.url());
    expect(false, `${what}: the address is ${url.pathname}${url.search}, not ?node=${id}`);
  }
}

/** Wait for the popover on node `id` with the address on `?node=<id>`; fail if it never opens. */
async function popoverOn(page, expect, id, what) {
  try {
    await page.waitForFunction(
      ([sel, node]) => {
        const pop = document.querySelector(sel);
        return new URL(location.href).searchParams.get('node') === node && !!pop && pop.textContent.includes(node);
      },
      [POPOVER, id],
      { timeout: 10_000 }
    );
  } catch {
    const url = new URL(page.url());
    const open = await page.locator(POPOVER).count();
    expect(false, `${what}: on ${url.pathname}${url.search} no popover opened on node ${id} (${open} popover drawn)`);
  }
}

/** Shut the open popover with Esc (the selection stays) and wait until it is gone. */
async function shut(page, expect, what) {
  await page.keyboard.press('Escape');
  try {
    await page.locator(POPOVER).waitFor({ state: 'detached', timeout: 5_000 });
  } catch {
    expect(false, `${what}: Esc did not shut the popover`);
  }
}

/** Fail if a popover is drawn now. */
async function staysShut(page, expect, what) {
  const open = await page.locator(POPOVER).count();
  const url = new URL(page.url());
  expect(open === 0, `${what}: a popover is open again on ${url.pathname}${url.search}`);
}

const ready = (page) => page.locator('.svelte-flow__node').first().waitFor({ state: 'visible', timeout: 15_000 });

export async function checks({ check, visit, expect, sleep }) {
  for (const size of SIZES) {
    const at = `${size.width}x${size.height}`;
    await check(`a ?node= link followed on the open epic page opens that node's popover at ${at}`, () =>
      visit(
        EPIC,
        async (page) => {
          await ready(page);
          expect((await page.locator(POPOVER).count()) === 0, 'a popover is open on the page with no ?node=');
          await follow(page, `node=${FIRST}`);
          await popoverOn(page, expect, FIRST, `a link to node ${FIRST} at ${at}`);
        },
        size
      )
    );
    await check(`Back and Forward to another node's ?node= link open that node's popover at ${at}`, () =>
      visit(
        EPIC,
        async (page) => {
          await ready(page);
          await follow(page, `node=${FIRST}`);
          await popoverOn(page, expect, FIRST, `a link to node ${FIRST} at ${at}`);
          await follow(page, `node=${SECOND}`);
          await popoverOn(page, expect, SECOND, `a link to node ${SECOND} at ${at}`);
          await shut(page, expect, `node ${SECOND}'s popover at ${at}`);
          await page.goBack();
          await popoverOn(page, expect, FIRST, `Back to node ${FIRST} at ${at}`);
          await shut(page, expect, `node ${FIRST}'s popover at ${at}`);
          await page.goForward();
          await popoverOn(page, expect, SECOND, `Forward to node ${SECOND} at ${at}`);
        },
        size
      )
    );
    await check(`a popover shut with Esc keeps its node in the address and stays shut through the page's polls at ${at}`, () =>
      visit(
        `${EPIC}?node=${FIRST}`,
        async (page) => {
          await ready(page);
          await popoverOn(page, expect, FIRST, `a first load of ?node=${FIRST} at ${at}`);
          await shut(page, expect, `node ${FIRST}'s popover at ${at}`);
          await addressed(page, expect, FIRST, `after Esc at ${at}`);
          await sleep(POLLS_MS);
          await staysShut(page, expect, `${POLLS_MS / 1000} s after Esc at ${at}`);
        },
        size
      )
    );
    await check(`after a close, a navigation to the same node with another key added leaves the popover shut at ${at}`, () =>
      visit(
        EPIC,
        async (page) => {
          await ready(page);
          await follow(page, `node=${FIRST}`);
          await popoverOn(page, expect, FIRST, `a link to node ${FIRST} at ${at}`);
          await shut(page, expect, `node ${FIRST}'s popover at ${at}`);
          await follow(page, `node=${FIRST}&ui-check=1`);
          await page.waitForFunction(() => new URL(location.href).searchParams.get('ui-check') === '1', null, { timeout: 10_000 });
          await addressed(page, expect, FIRST, `the same node with a key added at ${at}`);
          await sleep(500);
          await staysShut(page, expect, `after a navigation to ?node=${FIRST}&ui-check=1 at ${at}`);
        },
        size
      )
    );
  }
}
