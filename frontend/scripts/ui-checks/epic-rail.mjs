/**
 * The epic's selected-node rail at both sides of its folds, and a short window.
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 *
 * And the command the rail prints for a failed node the host died under twice
 * while it kept preserved work (#2960): the server serves `kew epic continue`
 * as the node's `recovery_command`, and `kew epic retry` — which the backend
 * refuses for that node — must not be on screen. Then the same failed snapshot
 * with its attention cleared, as the server serves it while a continuation
 * executes the preserved work: no command may be on screen at all. Negative
 * control: with `railSelected` printing retry for a failed node with no
 * attention again, the second check fails.
 *
 * And the same page kept open while another operator takes that work up
 * (claims the continuation, or abandons it) with no tick journaled: the stream
 * then carries only an attention frame and a recovery frame, and the mounted
 * rail must drop `kew epic continue` without a reload. Negative control: with
 * `applyFrame` ignoring the recovery frame, the third check fails.
 *
 * And the rail at 1440x960 and 1440x900 (D35): its NEEDS YOU column, the
 * shared needs-you block in `compact` mode, outgrew the 196px band at the
 * window's foot, and "Open #2055 in Work", the block's buttons and SFD-229's
 * unhold command were below the window's edge. Every control in that column,
 * and the command block, is now wholly inside the window, or inside it once
 * the rail's own scroll brings it there; the command comes first, so it is on
 * screen at the rail's top. The rail is sized by its content and capped at
 * 218px, and neither window scrolls the page (#3469): the page's column has
 * no more content than height, read off its scroll range.
 */
// ui-check-owns: src/lib/epic/SelectedRail.svelte
// ui-check-owns: src/lib/epic/rail.ts
// ui-check-owns: src/lib/epic/live.ts
// ui-check-owns: src/routes/(factory)/epics/[ref]/**
// ui-check-owns: fixtures/factory/epics-launch-fixes*.json
// ui-check-owns: fixtures/factory/epics-go-live-2026-09*.json
// ui-check-owns: src/lib/needs-you/NeedsYouBlock.svelte
import { EPIC, HEIGHT, WIDTH, widestBox } from '../ui-check-lib.mjs';

const NODE = '2055';
/** The rail's cap beside the canvas (DESIGN.md Grid, #3469): DEpic y 742-960. */
const RAIL_CAP = 218;
/** The design size and the window 900px tall, neither of which scrolls the page. */
const RAIL_SIZES = [
  { width: 1440, height: 960 },
  { width: 1440, height: 900 }
];
const CONTINUE = `kew epic continue launch-fixes ${NODE}`;
const PRESERVED = 'failed-terminal (exit 137, host_restart_repeated, preserved work)';
const TAKEN_UP = 'failed-terminal (exit 137, host_restart_repeated, preserved work taken up)';

/**
 * Serve #2055 failed with *stuck* as its journaled reason, *attention* as the
 * needs-you join fills it from that reason's bucket, and *recovery* as the
 * `recovery_command` the server fills from the same reason
 * (`kew.waits.failed_node_command`).
 */
function servedFailed(stuck, attention, recovery) {
  return (page) =>
    page.route('**/api/epics/launch-fixes', async (route) => {
      const response = await route.fetch();
      const payload = await response.json();
      payload.snapshot.nodes = payload.snapshot.nodes.map((n) =>
        n.id === NODE
          ? {
              ...n,
              status: 'failed',
              motion: 'waiting_human',
              stuck,
              attention,
              recovery_command: recovery
            }
          : n
      );
      await route.fulfill({ json: payload });
    });
}

/** The second-host-death state with preserved work a person continues. */
const servedPreservedWork = servedFailed(
  PRESERVED,
  {
    kind: 'held',
    reason: 'the host restarted under this node twice; its work is preserved',
    url: null,
    action: 'check why the host keeps going down, then kew epic continue the node',
    command: CONTINUE
  },
  CONTINUE
);

/** The same failed snapshot once a continuation owns its preserved work. */
const servedTakenUp = servedFailed(TAKEN_UP, null, '');

/**
 * The preserved-work detail read, and a live stream held shut until
 * *release* resolves; it then carries what the server's poll sends when the
 * work was taken up without a tick: the attention frame and the recovery
 * frame, and no snapshot frame (`kew.service.epics.fetch_epic_frames`).
 */
function servedThenTakenUp(release) {
  return async (page) => {
    await servedPreservedWork(page);
    await page.route('**/api/epics/launch-fixes/stream', async (route) => {
      await release;
      const frame = (type, data) => `data: ${JSON.stringify({ type, data })}\n\n`;
      await route.fulfill({
        status: 200,
        headers: { 'content-type': 'text/event-stream', 'cache-control': 'no-cache' },
        body:
          frame('attention', { by_node: {}, unavailable: [] }) +
          frame('recovery', {
            by_node: {
              [NODE]: { stuck: TAKEN_UP, blocked_reason: TAKEN_UP, recovery_command: '' }
            }
          })
      });
    });
  };
}

/**
 * The windows the rail is read in, which is both sides of both its folds
 * (#2333, `RAIL_TRACKS`). The first fix for the overprint folded the rail at
 * `sm` and this check only ever visited 390px, so the bug it was written for
 * was still on screen from 641px to ~1230px with the check passing: 1023/1024
 * and 1279/1280 are the two folds, and the pixel below each is where a fold
 * that moved up would show. Every height here is over `SHORT_VIEWPORT`, so
 * these are reading the WIDTH fold; the one below it reads the height.
 *
 * The sideways-scroll half of each reading is about the whole page, not just the
 * rail, and 641 is the width that proved it: nothing of the rail was wrong there
 * — the shell header's desktop row was ~741px of unshrinkable parts inside a
 * 641px window, so the page scrolled to reach `More` (#2419, fixed by moving
 * `FactoryHeader.svelte`'s fold to `lg`; `HEADER_FOLD` holds the measurement).
 * `widestBox` names the box, because the bare number does not say whose it is.
 */
const RAIL_WIDTHS = [
  { width: 390, height: 844 },
  { width: 390, height: 700 },
  { width: 641, height: 900 },
  { width: 1023, height: 900 },
  { width: 1024, height: 900 },
  { width: 1279, height: 900 },
  { width: 1280, height: 900 },
  { width: WIDTH, height: HEIGHT }
];

/**
 * Every box of words the selected-node rail draws, in viewport coordinates: a
 * leaf element with text in it, at a size a reader could see (a screen-reader
 * label is a 1px box and is not one). Two of these sharing pixels is the phone
 * bug this reads for (#2333) — at 390px the rail kept the artboard's 840px of
 * fixed columns and drew Pipeline, Evidence and Needs you over each other.
 */
function railTextBoxes(page) {
  return page.evaluate(() => {
    const rail = document.querySelector('section[aria-label^="Selected node"]');
    if (!rail) return null;
    return [...rail.querySelectorAll('*')]
      .filter((el) => !el.closest('svg'))
      .filter((el) => (el.textContent ?? '').trim() !== '')
      .filter((el) => ![...el.children].some((kid) => (kid.textContent ?? '').trim() !== ''))
      .map((el) => {
        const box = el.getBoundingClientRect();
        return {
          text: (el.textContent ?? '').trim().slice(0, 40),
          x: box.x,
          y: box.y,
          w: box.width,
          h: box.height
        };
      })
      .filter((box) => box.w > 1 && box.h > 1);
  });
}

/**
 * The first pair that shares more than a pixel on both axes, said in the words
 * the reader would have seen printed over each other.
 *
 * Geometry, not paint: a column that clips its overflow still reports the box
 * its words laid out in, so a clipped word counts here. That is deliberate —
 * clipping is the rail's last line of defence (see the Pipeline column), and a
 * fold that let it come to that is a fold this check should fail on.
 */
function firstOverlap(boxes) {
  for (let i = 0; i < boxes.length; i += 1) {
    for (let j = i + 1; j < boxes.length; j += 1) {
      const a = boxes[i];
      const b = boxes[j];
      const x = Math.min(a.x + a.w, b.x + b.w) - Math.max(a.x, b.x);
      const y = Math.min(a.y + a.h, b.y + b.h) - Math.max(a.y, b.y);
      if (x > 1 && y > 1) {
        return `"${a.text}" and "${b.text}" share ${Math.round(x)}x${Math.round(y)}px`;
      }
    }
  }
  return null;
}

export async function checks({ check, visit, expect, sleep }) {
  for (const { width, height } of RAIL_WIDTHS) {
    await check(`the epic rail folds without overlapping at ${width}x${height}`, () =>
      visit(
        EPIC,
        async (page) => {
          const boxes = await railTextBoxes(page);
          expect(boxes !== null, 'no selected-node rail on the page at all');
          expect(boxes.length > 3, `the rail drew ${boxes.length} readable boxes`);
          const collision = firstOverlap(boxes);
          expect(collision === null, `the rail overlaps itself: ${collision}`);
          // Nothing of the rail off the side of the window, and no page that
          // can be dragged sideways to find it (DESIGN.md, 390x844 and 1280).
          const spill = boxes.filter((box) => box.x < -1 || box.x + box.w > width + 1);
          expect(spill.length === 0, `off the right edge: ${spill.map((b) => b.text).join(', ')}`);
          const scroll = await widestBox(page);
          expect(
            scroll.w <= scroll.c + 1,
            `the page scrolls sideways (${scroll.w} > ${scroll.c}), widest box: ${scroll.culprit}`
          );
          // The canvas keeps a graph's worth of height under a folded rail:
          // the pane that shrank to nothing is the other half of #2333.
          const pane = await page.locator('[data-canvas-pane]').boundingBox();
          expect(pane !== null, 'the canvas pane is not laid out at all');
          expect(pane.height >= 300, `the canvas pane is ${Math.round(pane.height)}px tall`);
        },
        { width, height }
      )
    );
  }

  await check('a short window keeps the canvas and folds the rail away', () =>
    visit(
      EPIC,
      async (page) => {
        const rail = page.locator('section[aria-label^="Selected node #"]');
        await rail.waitFor({ state: 'visible', timeout: 15_000 });
        const pane = page.locator('[data-canvas-pane]');
        const box = await pane.boundingBox();
        expect(box !== null, 'the canvas pane is not laid out at all');
        expect(box.height >= 300, `the canvas pane is ${Math.round(box.height)}px tall`);
        // Folded: the four sections are gone and one line names the node.
        const disclosure = page.locator('button[aria-controls="selected-rail-detail"]');
        expect(await disclosure.count() === 1, 'no disclosure on a 600px-tall window');
        expect(
          await page.locator('#selected-rail-detail').count() === 0,
          'the rail is still showing its sections on a short window'
        );
        await disclosure.click();
        await sleep(250);
        const detail = page.locator('#selected-rail-detail');
        expect(await detail.count() === 1, 'the disclosure opened nothing');
        const words = (await detail.textContent()) ?? '';
        for (const section of ['Selected', 'Pipeline', 'Evidence', 'Needs you']) {
          expect(words.includes(section), `the opened rail does not show ${section}`);
        }
        // And its bottom can be got to: the page column owns a scroller for
        // exactly this (#2278 C4).
        await page.evaluate(() => {
          document
            .querySelector('section[aria-label^="Selected node"]')
            ?.scrollIntoView({ block: 'end' });
        });
        await sleep(250);
        const foot = await page.evaluate(() => {
          const el = document.querySelector('section[aria-label^="Selected node"]');
          return { bottom: el.getBoundingClientRect().bottom, inner: window.innerHeight };
        });
        expect(
          foot.bottom <= foot.inner + 1,
          `the rail's bottom stays ${Math.round(foot.bottom - foot.inner)}px below the window`
        );
      },
      { width: WIDTH, height: 600 }
    )
  );

  await check('the rail prints continue, never retry, for preserved work the host died under', () =>
    visit(
      `${EPIC}?node=${NODE}`,
      async (page) => {
        const rail = page.locator('section[aria-label^="Selected node"]');
        await rail.waitFor({ state: 'visible', timeout: 15_000 });
        const words = (await rail.textContent()) ?? '';
        const commands = await rail.locator('[data-command]').allTextContents();
        expect(
          commands.some((c) => c.trim() === CONTINUE),
          `the rail's commands are ${JSON.stringify(commands)}, not ${CONTINUE}`
        );
        expect(!words.includes('kew epic retry'), 'the rail still offers kew epic retry');
      },
      { width: WIDTH, height: HEIGHT },
      servedPreservedWork
    )
  );

  await check('the rail invents no command once attention clears on a failed node', () =>
    visit(
      `${EPIC}?node=${NODE}`,
      async (page) => {
        const rail = page.locator('section[aria-label^="Selected node"]');
        await rail.waitFor({ state: 'visible', timeout: 15_000 });
        const words = (await rail.textContent()) ?? '';
        const commands = await rail.locator('[data-command]').allTextContents();
        expect(
          commands.length === 0,
          `the rail still prints ${JSON.stringify(commands)} for work a continuation owns`
        );
        expect(!words.includes('kew epic retry'), 'the rail invents kew epic retry');
      },
      { width: WIDTH, height: HEIGHT },
      servedTakenUp
    )
  );

  let release = () => {};
  const released = new Promise((resolve) => {
    release = resolve;
  });
  await check('the open rail drops continue once the work is taken up without a tick', () =>
    visit(
      `${EPIC}?node=${NODE}`,
      async (page) => {
        const rail = page.locator('section[aria-label^="Selected node"]');
        await rail.waitFor({ state: 'visible', timeout: 15_000 });
        const before = await rail.locator('[data-command]').allTextContents();
        expect(
          before.some((c) => c.trim() === CONTINUE),
          `the mounted rail's commands are ${JSON.stringify(before)}, not ${CONTINUE}`
        );
        release();
        try {
          await rail.locator('[data-command]').first().waitFor({ state: 'detached', timeout: 10_000 });
        } catch {
          // Read below: the expectation names what is still on screen.
        }
        const after = await rail.locator('[data-command]').allTextContents();
        const words = (await rail.textContent()) ?? '';
        expect(
          after.length === 0,
          `the open rail still prints ${JSON.stringify(after)} for work taken up elsewhere`
        );
        expect(!words.includes('kew epic retry'), 'the open rail invents kew epic retry');
      },
      { width: WIDTH, height: HEIGHT },
      servedThenTakenUp(released)
    )
  );

  // #2055 waits on a merge decision, so its block is links and no command;
  // SFD-229 is held, and its block carries the unhold command.
  for (const { route, command, size } of [
    { route: `${EPIC}?node=${NODE}`, command: false },
    { route: '/epics/go-live-2026-09?node=SFD-229', command: true }
  ].flatMap((at) => RAIL_SIZES.map((size) => ({ ...at, size })))) {
    const at = `${size.width}x${size.height}`;
    await check(`the rail's NEEDS YOU column and its command are reachable whole at ${at} on ${route}`, () =>
      visit(
        route,
        async (page) => {
          const column = page.locator('section[aria-label^="Selected node"] section[aria-label^="Needs you"]');
          await column.first().waitFor({ state: 'attached', timeout: 15_000 });
          await sleep(300);
          const read = await page.evaluate(async () => {
            const rail = document.querySelector('section[aria-label^="Selected node"]');
            const block = rail?.querySelector('section[aria-label^="Needs you"]');
            const command = block?.querySelector('code[data-command]');
            const summary = block?.querySelector('[data-needs-you-summary]');
            // The page's column: the scroller the canvas pane and the rail sit in.
            const column = document.querySelector('[data-canvas-pane]')?.parentElement ?? null;
            if (!rail || !block) return null;
            const frames = () => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)));
            const whole = (r, box) =>
              r.top >= box.top - 0.5 && r.bottom <= box.bottom + 0.5 && r.left >= box.left - 0.5 && r.right <= box.right + 0.5;
            const win = { top: 0, left: 0, bottom: innerHeight, right: innerWidth };
            const railBox = rail.getBoundingClientRect();
            const scrolls = rail.scrollHeight > rail.clientHeight + 1 && getComputedStyle(rail).overflowY !== 'visible';
            const targets = [
              ...[...block.querySelectorAll('a, button')].map((el) => ({
                name: (el.getAttribute('aria-label') ?? el.textContent ?? '').trim().replace(/\s+/g, ' '),
                el
              })),
              ...(command ? [{ name: 'the command block', el: command.closest('div') }] : [])
            ];
            const missed = [];
            for (const { name, el } of targets) {
              rail.scrollTop = 0;
              await frames();
              if (whole(el.getBoundingClientRect(), win)) continue;
              // Not on screen at rest: only the rail's own scroll may bring it.
              if (!scrolls || !whole(railBox, win)) {
                missed.push(`${name} (rail ${scrolls ? 'scrolls' : 'does not scroll'}, ends at ${Math.round(railBox.bottom)})`);
                continue;
              }
              const r = el.getBoundingClientRect();
              rail.scrollTop += r.bottom - rail.getBoundingClientRect().bottom + 8;
              await frames();
              const seen = el.getBoundingClientRect();
              if (!(whole(seen, rail.getBoundingClientRect()) && whole(seen, win))) missed.push(`${name} (scrolled)`);
            }
            rail.scrollTop = 0;
            await frames();
            const cmdTop = command?.getBoundingClientRect().top ?? null;
            const firstLine = command ? Number.parseFloat(getComputedStyle(command).lineHeight) : 0;
            return {
              controls: targets.length,
              command: command?.textContent?.trim() ?? '',
              commandFirst:
                command !== null &&
                command !== undefined &&
                (summary === null || summary === undefined ||
                  (command.compareDocumentPosition(summary) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0),
              commandOnScreen: cmdTop !== null && cmdTop >= 0 && cmdTop + firstLine <= innerHeight + 0.5,
              railH: railBox.height,
              page: column ? { scroll: column.scrollHeight, client: column.clientHeight } : null,
              missed
            };
          });
          expect(read !== null, `no NEEDS YOU column in the rail on ${route}`);
          expect(read.controls >= 2, `the column has ${read.controls} controls, so this proves little`);
          expect(read.page !== null, `no page column around the canvas pane on ${route}`);
          // No page scroll at 900px tall or more (#3469): the page's column
          // holds the header, the canvas and the capped rail. Read off its
          // scroll range, not `scrollTop`, which is 0 on a page that could
          // still be scrolled.
          expect(
            read.page.scroll <= read.page.client + 1,
            `the page scrolls: its column is ${read.page.scroll}px of content in ${read.page.client}px`
          );
          // Sized by its content and capped at 218px: above the cap the rail
          // scrolls inside itself.
          expect(read.railH <= RAIL_CAP + 0.5, `the rail is ${read.railH.toFixed(1)}px, over its ${RAIL_CAP}px cap`);
          expect(read.missed.length === 0, `out of reach: ${read.missed.join('; ')}`);
          if (command) {
            expect(read.command.startsWith('kew '), `the column prints no command ("${read.command}")`);
            expect(read.commandFirst, 'the command comes after the sentence in the NEEDS YOU column');
            expect(read.commandOnScreen, "the command's first line is not on screen at the rail's top");
          }
        },
        size
      )
    );
  }
}
