/**
 * What fits at 390px: the needs-you command, the epic progress strip, the
 * canvas toolbar and the run-event chips (#2788, design-c5 N1 N2 N7 N15); the
 * epic page with its node sheet open, its key hints and its header gutter
 * (#3053, design-c7 N2 N6 N10).
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 */
// ui-check-owns: src/lib/needs-you/NeedsYouBlock.svelte
// ui-check-owns: src/lib/ui/Command.svelte
// ui-check-owns: src/routes/(factory)/epics/[ref]/+page.svelte
// ui-check-owns: src/lib/epic/DagView.svelte
// ui-check-owns: src/lib/epic/NodePopover.svelte
// ui-check-owns: src/lib/runs/RunEvents.svelte
const PHONE = { width: 390, height: 844 };
const DESKTOP = { width: 1440, height: 960 };

/** 600 characters of run detail on #2055, so the sheet's body has a fold. */
const LONG_DETAIL = 'review round stalled waiting on a reviewer verdict '.repeat(12).slice(0, 600);

function longRunDetail(page) {
  return page.route('**/api/epics/launch-fixes/nodes/2055', async (route) => {
    const response = await route.fetch();
    const payload = await response.json();
    const [template] = payload.runs;
    payload.runs = [{ ...template, status: LONG_DETAIL }, ...payload.runs.slice(1)];
    await route.fulfill({ json: payload });
  });
}

/** Every key-hint cap and hint field the epic page draws, with whether it is drawn. */
function readHints(page) {
  return page.evaluate(() => {
    const drawn = (el) => {
      const style = getComputedStyle(el);
      return style.visibility !== 'hidden' && el.getClientRects().length > 0;
    };
    const named = (el) => (el.matches('input') ? el.getAttribute('placeholder') : (el.textContent ?? '').trim());
    // `.kbd` is the cap DagView and NodePopover draw, `[data-key-hint]` the
    // view switch's T, `#dag-lineage` the lineage field whose box is a hint.
    return [...document.querySelectorAll('.kbd, [data-key-hint], #dag-lineage')].map((el) => ({
      name: named(el) || el.tagName.toLowerCase(),
      drawn: drawn(el)
    }));
  });
}

/** Every box in `boxes` that reaches past the viewport, named for the message. */
function outside(boxes, viewport) {
  return boxes.filter((b) => b.left < -0.5 || b.right > viewport + 0.5);
}

export async function checks({ check, visit, expect }) {
  /**
   * N1 — the DO THIS command wrapped one word per line beside its 96px label
   * and the Run and Copy buttons. Below 640 the label stacks over a full-width
   * command and the buttons go under it. The line count is read off the text's
   * own client rects, one per line box, so a command clipped to one line by
   * overflow cannot pass by looking short.
   */
  await check('the DO THIS command wraps to at most 2 lines at 390px', () =>
    visit(
      '/work?item=SFD-229',
      async (page) => {
        const code = page.locator('code[data-command]').first();
        await code.waitFor({ state: 'visible', timeout: 15_000 });
        const read = await code.evaluate((el) => {
          const range = document.createRange();
          range.selectNodeContents(el);
          const tops = new Set([...range.getClientRects()].map((r) => Math.round(r.top)));
          return { text: (el.textContent ?? '').trim(), lines: tops.size, width: el.clientWidth };
        });
        expect(read.text !== '', 'the needs-you block drew no command, so this proves nothing');
        expect(
          read.lines <= 2,
          `"${read.text}" takes ${read.lines} lines in a ${read.width}px box at 390px`
        );
      },
      PHONE
    )
  );

  /**
   * N2 — a live epic of 39 nodes drew 39 fixed 12px cells, 544px of bar, and
   * the page clipped it: the held and queued squares at its end were gone and
   * the phone showed only green. The fixture epic has 18 nodes, which fit, so
   * its counts are served at the live size here, ending in the statuses the
   * clip took.
   */
  await check('every progress-strip square is inside the viewport at 390px', () =>
    visit(
      '/epics/go-live-2026-09',
      async (page) => {
        const bar = page.locator('[role="img"][aria-label^="Progress:"]').first();
        await bar.waitFor({ state: 'visible', timeout: 15_000 });
        const read = await bar.evaluate((el) => ({
          viewport: document.documentElement.clientWidth,
          cells: [...el.children].map((c) => {
            const b = c.getBoundingClientRect();
            return { left: b.left, right: b.right, width: b.width };
          })
        }));
        expect(read.cells.length === 39, `the bar drew ${read.cells.length} squares for 39 nodes`);
        expect(
          read.cells.every((c) => c.width >= 2),
          `a square drew narrower than 2px: ${read.cells.map((c) => c.width.toFixed(1)).join(' ')}`
        );
        const off = outside(read.cells, read.viewport);
        expect(
          off.length === 0,
          `${off.length} of 39 squares fall outside the ${read.viewport}px viewport; ` +
            `the last ends at ${read.cells.at(-1).right.toFixed(1)}`
        );
      },
      PHONE,
      async (page) => {
        await page.route(
          (url) => url.pathname === '/api/epics/go-live-2026-09',
          async (route) => {
            const response = await route.fetch();
            const body = await response.json();
            body.snapshot.counts = { done: 32, in_progress: 1, held: 1, queued: 5 };
            await route.fulfill({ response, json: body });
          }
        );
      }
    )
  );

  /**
   * N7 — the canvas rail ran off the right edge at 390 and cut "Fit all" in
   * half. Every button the rail draws is read: the toolbar's and the first
   * frame's "Fit all" beside it, on both fixture epics with a canvas.
   */
  for (const route of ['/epics/go-live-2026-09', '/epics/launch-fixes']) {
    await check(`every canvas toolbar button is inside the viewport on ${route} at 390px`, () =>
      visit(
        route,
        async (page) => {
          const toolbar = page.locator('[role="toolbar"][aria-label="Canvas controls"]').first();
          await toolbar.waitFor({ state: 'visible', timeout: 15_000 });
          const read = await toolbar.evaluate((el) => {
            const rail = el.parentElement ?? el;
            return {
              viewport: document.documentElement.clientWidth,
              buttons: [...rail.querySelectorAll('button')]
                .filter((b) => b.getClientRects().length > 0)
                .map((b) => {
                  const box = b.getBoundingClientRect();
                  return {
                    name: b.getAttribute('aria-label') ?? (b.textContent ?? '').trim(),
                    left: box.left,
                    right: box.right
                  };
                })
            };
          });
          expect(read.buttons.length >= 5, `${route}: the rail drew ${read.buttons.length} buttons`);
          const off = outside(read.buttons, read.viewport);
          expect(
            off.length === 0,
            `${route}: ${off.map((b) => `"${b.name}" ends at ${b.right.toFixed(1)}`).join(', ')} ` +
              `in a ${read.viewport}px viewport`
          );
        },
        PHONE
      )
    );
  }

  /**
   * N15 — the run-event kind chips were 22px tall with 3px corners. They are
   * the shared pill (`chipClasses`), which grows to 32px of press below 640.
   */
  await check('run-event chips are at least 32px tall at 390px', () =>
    visit(
      '/runs/f5dcb6a5',
      async (page) => {
        const section = page.locator('section[aria-label="Run events"]');
        await section.locator('button[aria-pressed]').first().waitFor({ state: 'visible', timeout: 15_000 });
        const heights = await section.evaluate((el) =>
          [...el.querySelectorAll('button[aria-pressed]')].map((b) => ({
            name: (b.textContent ?? '').trim(),
            height: b.getBoundingClientRect().height
          }))
        );
        const short = heights.filter((h) => h.height < 32 - 0.5);
        expect(
          short.length === 0,
          `${short.map((h) => `"${h.name}" ${h.height.toFixed(1)}px`).join(', ')} under 32px`
        );
      },
      PHONE
    )
  );

  /**
   * #3053 N2 — with the node sheet open the epic page was 1130px tall in an
   * 844px window: the page scrolled and the nav went with it. The page is the
   * window's height, and a scroll of the sheet's body — long enough here to
   * have a fold, so the wheel has something to move — leaves the nav at 0.
   */
  await check('the epic page with the node sheet open is no taller than the window at 390px', () =>
    visit(
      '/epics/launch-fixes?node=2055',
      async (page) => {
        const body = page.locator('[role="dialog"].sheet .pbody').first();
        await body.waitFor({ state: 'visible', timeout: 15_000 });
        await page.locator('[role="dialog"] [data-history]').first().waitFor({ state: 'visible', timeout: 15_000 });
        const before = await page.evaluate(() => ({
          height: document.documentElement.scrollHeight,
          window: window.innerHeight
        }));
        expect(
          before.height <= before.window,
          `the page is ${before.height}px tall in a ${before.window}px window with the sheet open`
        );
        const fold = await body.evaluate((el) => el.scrollHeight - el.clientHeight);
        expect(fold > 0, "the sheet's body has no fold, so a scroll of it proves nothing");
        const box = await body.boundingBox();
        expect(box !== null, "the sheet's body is not laid out");
        await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
        for (let i = 0; i < 4; i += 1) {
          await page.mouse.wheel(0, 600);
          await new Promise((done) => setTimeout(done, 120));
        }
        const after = await page.evaluate(() => {
          const sheet = document.querySelector('[role="dialog"].sheet .pbody');
          const nav = document.querySelector('header');
          return {
            scrolled: sheet?.scrollTop ?? 0,
            navTop: nav ? nav.getBoundingClientRect().top : null,
            pageY: window.scrollY,
            height: document.documentElement.scrollHeight
          };
        });
        expect(after.scrolled > 0, "the wheel over the sheet's body did not scroll it");
        expect(after.navTop !== null, 'the page drew no nav');
        expect(
          Math.abs(after.navTop) < 0.5 && after.pageY === 0,
          `after scrolling the sheet the nav's top edge is at ${after.navTop}px (page scrolled ${after.pageY}px)`
        );
        expect(after.height <= before.window, `the page grew to ${after.height}px after the scroll`);
      },
      PHONE,
      longRunDetail
    )
  );

  // The same reading on the other fixture epic, the one the design pass shot.
  await check('go-live-2026-09 with the node sheet open is no taller than the window at 390px', () =>
    visit(
      '/epics/go-live-2026-09?node=SFD-229',
      async (page) => {
        await page.locator('[role="dialog"].sheet').first().waitFor({ state: 'visible', timeout: 15_000 });
        const read = await page.evaluate(() => ({
          height: document.documentElement.scrollHeight,
          window: window.innerHeight
        }));
        expect(read.height <= read.window, `the page is ${read.height}px tall in a ${read.window}px window`);
      },
      PHONE
    )
  );

  /**
   * #3053 N6 — at 390 the page drew "esc", "T", "Fit all F" and the lineage
   * field "+#N · #N+ · L on a", cut at its edge. Below 1024 there is no
   * keyboard to hint at (DESIGN.md): none is drawn at 390, and every one is at
   * 1440, so the reading cannot pass by finding nothing to read.
   */
  for (const [size, wide] of [
    [PHONE, false],
    [DESKTOP, true]
  ]) {
    await check(`key hints and the hint field are ${wide ? '' : 'not '}drawn at ${size.width}px`, () =>
      visit(
        // go-live is the epic big enough to open on a crop, so the canvas
        // draws its "Fit all F" hint; the link opens the node's popover (esc).
        '/epics/go-live-2026-09?node=SFD-229',
        async (page) => {
          await page.locator('[role="dialog"]').first().waitFor({ state: 'visible', timeout: 15_000 });
          await page.locator('.dag .hint').first().waitFor({ state: 'attached', timeout: 15_000 });
          const hints = await readHints(page);
          const drawn = hints.filter((h) => h.drawn).map((h) => h.name);
          if (wide) {
            for (const want of ['esc', 'T', 'F', '+#N · #N+ · L on a node']) {
              expect(drawn.includes(want), `"${want}" is not drawn at ${size.width}px (drawn: ${drawn.join(', ')})`);
            }
          } else {
            expect(hints.length > 0, 'the page has no key hint to hide, so this proves nothing');
            expect(drawn.length === 0, `drawn at ${size.width}px: ${drawn.map((d) => `"${d}"`).join(', ')}`);
          }
        },
        size
      )
    );
  }

  /**
   * #3053 N10 — the epic header kept the 40px desktop gutter at 390, where
   * Work and Monitor use 16px. Its content box starts 16px in.
   */
  await check("the epic header's content starts 16px from the edge at 390px", () =>
    visit(
      '/epics/launch-fixes',
      async (page) => {
        const header = page.locator('section[aria-label^="Epic "]').first();
        await header.waitFor({ state: 'visible', timeout: 15_000 });
        const read = await header.evaluate((el) => {
          const box = el.getBoundingClientRect();
          return {
            content: box.left + parseFloat(getComputedStyle(el).paddingLeft),
            first: el.querySelector('span')?.getBoundingClientRect().left ?? null
          };
        });
        expect(Math.abs(read.content - 16) < 0.5, `the header's content box starts ${read.content}px in`);
        expect(
          read.first !== null && Math.abs(read.first - 16) < 0.5,
          `the header's first word starts ${read.first}px in, not 16px`
        );
      },
      PHONE
    )
  );
}
