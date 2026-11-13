/**
 * An epic legend prints no 0 count (#2540 D31), and no line of a wrapped
 * header row starts or ends with a separator (#3053 N17).
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 *
 * And the canvas's own marks and words agree with what they stand for (D36,
 * D39): the legend's "next up" ring is the frontier card's ring, in the same
 * colour (it was cyan beside grey cards); a done toggle with nothing to fold
 * says so rather than "Done · shown" in the disabled tint; the caption stops
 * saying "Centred on" once the camera moves off the first frame, whatever
 * moved it, and not before; below 1024 the caption's
 * diamond keeps its "needs you"; and the lineage hint is text, not a pill.
 */
// ui-check-owns: src/lib/epic/DagView.svelte
// ui-check-owns: src/lib/epic/DagNode.svelte
// ui-check-owns: src/lib/epic/ReadyRing.svelte
// ui-check-owns: src/lib/epic/LineageBox.svelte
// ui-check-owns: src/routes/(factory)/epics/[ref]/**
// ui-check-owns: fixtures/factory/epics-go-live-2026-09*.json
// ui-check-owns: fixtures/factory/epics-launch-fixes*.json
import { EPIC, SETTLE_MS } from '../ui-check-lib.mjs';

/** The first-frame caption's words, whitespace folded. */
const captionOf = async (page) =>
  (await page.locator('.dag [data-frame-hint]').first().innerText()).replace(/\s+/g, ' ').trim();

/** Where the camera is: the flow's viewport transform, as drawn. */
const cameraOf = (page) =>
  page.evaluate(() => document.querySelector('.dag .svelte-flow__viewport')?.style.transform ?? null);

/** go-live opens a `?node=` link on SFD-229 with its popover, which the
 * first frame pans down to make room for; past Escape the minimap shows. */
const LINKED = '/epics/go-live-2026-09?node=SFD-229';
const DESK = { width: 1440, height: 900 };

/** Before the page loads: keep every camera transform the canvas draws, in
 * order, in `window.__cameras`, read once a frame — so a check can see the
 * first frame's own pan, which is over before `visit` hands the page on. */
const recordCameras = (page) =>
  page.addInitScript(() => {
    const seen = [];
    window.__cameras = seen;
    const tick = () => {
      const t = document.querySelector('.dag .svelte-flow__viewport')?.style.transform;
      if (t && seen[seen.length - 1] !== t) seen.push(t);
      requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  });

export async function checks({ browser, base, context, check, visit, expect, sleep }) {
  /**
   * #2540 (D31) — a legend shows only non-zero counts (DESIGN.md). The
   * fixture epic counts `failed 0` and `held 0`, and nobody needs you on it,
   * so all three are drawn as zeros the moment the filter goes.
   */
  await check(`no legend on ${EPIC} prints a 0 count`, async () => {
    const page = await context.newPage();
    try {
      await page.goto(`${base}${EPIC}`, { waitUntil: 'load' });
      const legend = page.locator('[data-legend]');
      await legend.first().waitFor({ state: 'visible', timeout: 15_000 });
      await sleep(SETTLE_MS);
      const texts = await legend.evaluateAll((els) =>
        els.map((el) => (el.textContent ?? '').replace(/\s+/g, ' ').trim())
      );
      expect(texts.length > 0, 'the epic draws no legend');
      for (const text of texts) {
        expect(text !== '', 'a legend is drawn empty');
        expect(!/(^|\s)0(\s|$)/.test(text), `a legend prints a zero: "${text}"`);
      }
    } finally {
      await page.close();
    }
  });

  /**
   * #3053 N17 — the header's rows wrap at 390, and a separator was left at
   * the end of one line ("▸ ticker |") and the start of another ("| ◆ needs
   * you 10"). A separator is read as painted when any of it falls inside its
   * row's box less 3px of slack — the row clips what lies further out, and
   * the check holds it to that — and a painted one must have a reading on its
   * left AND on its right on the same line.
   */
  for (const route of [EPIC, '/epics/go-live-2026-09']) {
    await check(`no header line on ${route} starts or ends with a separator at 390px`, () =>
      visit(
        route,
        async (page) => {
          await page.locator('[data-legend]').first().waitFor({ state: 'visible', timeout: 15_000 });
          const rows = await page.evaluate(() =>
            [...document.querySelectorAll('[data-sep-row]')].map((row) => {
              const box = (el) => {
                const r = el.getBoundingClientRect();
                return { left: r.left, right: r.right, top: r.top, bottom: r.bottom };
              };
              const shown = (el) => {
                const r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0 && getComputedStyle(el).visibility !== 'hidden';
              };
              return {
                row: box(row),
                clip: getComputedStyle(row).clipPath,
                items: [...row.children]
                  .filter(shown)
                  .map((el) => ({ ...box(el), text: (el.textContent ?? '').replace(/\s+/g, ' ').trim() })),
                seps: [...row.querySelectorAll('[data-sep]')].filter(shown).map((el) => ({
                  ...box(el),
                  of: (el.parentElement?.textContent ?? '').replace(/\s+/g, ' ').trim()
                }))
              };
            })
          );
          const seps = rows.flatMap((r) => r.seps);
          expect(seps.length >= 2, `${route} drew ${seps.length} separators, so this proves nothing`);
          for (const { row, clip, items, seps: own } of rows) {
            for (const sep of own) {
              const painted = sep.right > row.left - 3 && sep.left < row.right + 3;
              if (!painted) {
                expect(clip !== 'none', `the separator before "${sep.of}" lies outside an unclipped row`);
                continue;
              }
              const line = items.filter((i) => i.top < sep.bottom && i.bottom > sep.top && i.text !== '');
              const left = line.some((i) => i.right <= sep.left + 0.5);
              const right = line.some((i) => i.left >= sep.right - 0.5);
              expect(left, `a line starts with the separator before "${sep.of}"`);
              expect(right, `a line ends with the separator before "${sep.of}"`);
            }
          }
        },
        { width: 390, height: 844 }
      )
    );
  }

  await check("the legend's next-up ring and a frontier card's ring are one colour", () =>
    visit(EPIC, async (page) => {
      await page.locator('[data-legend]').first().waitFor({ state: 'visible', timeout: 15_000 });
      await page.locator('.dag .svelte-flow__node').first().waitFor({ state: 'attached', timeout: 10_000 });
      const read = await page.evaluate(() => {
        const ring = (el) => (el ? getComputedStyle(el).borderTopColor : null);
        const legend = document.querySelector('[data-legend] [data-next-up]');
        return {
          legend: ring(legend?.querySelector('[data-ready-ring]')),
          dot: legend?.querySelector('[data-ready-dot]')
            ? getComputedStyle(legend.querySelector('[data-ready-dot]')).backgroundColor
            : null,
          cards: [...document.querySelectorAll('.dag .svelte-flow__node [data-ready-ring]')].map(ring)
        };
      });
      expect(read.legend !== null, 'the legend draws no next-up ring');
      expect(read.cards.length > 0, 'no card on the canvas draws the frontier ring');
      for (const card of read.cards) {
        expect(card === read.legend, `the legend rings next up in ${read.legend}, a frontier card in ${card}`);
      }
      expect(read.dot === read.legend, `the legend's dot is ${read.dot}, its ring ${read.legend}`);
    })
  );

  for (const route of [EPIC, '/epics/go-live-2026-09']) {
    for (const size of [
      { width: 1440, height: 900 },
      { width: 390, height: 844 }
    ]) {
      await check(`a disabled done toggle on ${route} never reads "Done · shown" at ${size.width}`, () =>
        visit(
          route,
          async (page) => {
            const toggle = page.locator('.dag .controls .done-toggle');
            await toggle.first().waitFor({ state: 'visible', timeout: 15_000 });
            const read = await toggle.first().evaluate((el) => ({
              disabled: el.disabled,
              text: (el.textContent ?? '').replace(/\s+/g, ' ').trim(),
              box: el.getBoundingClientRect().toJSON(),
              bar: el.closest('.controls').getBoundingClientRect().toJSON()
            }));
            if (read.disabled) {
              expect(read.text !== 'Done · shown', 'a disabled done toggle reads "Done · shown"');
              expect(read.text === 'No finished chain can fold here', `the disabled toggle reads "${read.text}"`);
            }
            expect(read.box.right <= size.width && read.bar.right <= size.width, 'the toolbar runs off the window');
            expect(read.box.height >= 32, `the done toggle is ${read.box.height}px tall`);
          },
          size
        )
      );
    }
  }

  // Not vacuous: launch-fixes has no finished chain to fold, so the toggle
  // above was read disabled there.
  await check(`the done toggle on ${EPIC} is disabled, so the reading above is made`, () =>
    visit(EPIC, async (page) => {
      const toggle = page.locator('.dag .controls .done-toggle');
      await toggle.first().waitFor({ state: 'visible', timeout: 15_000 });
      expect(await toggle.first().isDisabled(), `${EPIC}'s done toggle is enabled`);
    })
  );

  for (const how of ['the wheel', 'the zoom-in button']) {
    await check(`the caption stops saying "Centred on" after a zoom by ${how}`, () =>
      visit(`${EPIC}?node=2055`, async (page) => {
        const hint = page.locator('.dag [data-frame-hint]');
        await hint.first().waitFor({ state: 'visible', timeout: 15_000 });
        const before = (await hint.first().innerText()).replace(/\s+/g, ' ');
        // Not vacuous: the first frame does say where it is centred.
        expect(before.includes('Centred on'), `the first frame's caption reads "${before}"`);
        await page.keyboard.press('Escape');
        if (how === 'the wheel') {
          const box = await page.locator('.dag .floor').boundingBox();
          await page.mouse.move(box.x + 40, box.y + 40);
          await page.mouse.wheel(0, -300);
        } else {
          await page.locator('.dag .controls [aria-label="Zoom in"]').click();
        }
        await sleep(500);
        const after = (await hint.first().innerText()).replace(/\s+/g, ' ').trim();
        expect(!after.includes('Centred on'), `after a zoom the caption still reads "${after}"`);
        expect(after.includes('Fit all'), `after a zoom the caption lost "Fit all" ("${after}")`);
      })
    );
  }

  await check('the caption stops saying "Centred on" after 0 zooms to another node', () =>
    visit(`${EPIC}?node=2055`, async (page) => {
      const node = () => new URL(page.url()).searchParams.get('node');
      const hint = page.locator('.dag [data-frame-hint]');
      await hint.first().waitFor({ state: 'visible', timeout: 15_000 });
      const before = (await hint.first().innerText()).replace(/\s+/g, ' ');
      expect(before.includes('Centred on'), `the first frame's caption reads "${before}"`);
      await page.keyboard.press('Escape');
      // Walk off the centred node to a card already in view. A walk that
      // pans drops "Centred on" by itself (checked below), so this one must
      // not: then it is the 0 that the caption answers to.
      const start = await cameraOf(page);
      for (const key of ['ArrowLeft', 'ArrowUp', 'ArrowRight', 'ArrowDown']) {
        if (node() !== null && node() !== '2055') break;
        await page.keyboard.press(key);
        await sleep(250);
      }
      expect(node() !== null && node() !== '2055', `the arrows left the selection on ${node()}`);
      const walked = await cameraOf(page);
      expect(walked === start, `the walk to ${node()} moved the camera (${start} -> ${walked})`);
      const mid = await captionOf(page);
      expect(mid.includes('Centred on #2055'), `a walk that moved nothing left the caption "${mid}"`);
      await page.keyboard.press('0');
      await sleep(500);
      const after = (await hint.first().innerText()).replace(/\s+/g, ' ').trim();
      expect(!after.includes('Centred on'), `after 0 on ${node()} the caption still reads "${after}"`);
      expect(after.includes('Fit all'), `after 0 the caption lost "Fit all" ("${after}")`);
    })
  );

  /**
   * D39 — ANY move of the camera after the first frame drops "Centred on",
   * whatever made it. The minimap moves the camera with no input event, as
   * the page's own camera does (`setViewportConstrained` for a drag,
   * `scaleTo` for the wheel), and both left "Centred on SFD-229 · selected"
   * over a frame dragged away and zoomed to 152%.
   */
  for (const how of ['drag', 'wheel']) {
    await check(`the caption stops saying "Centred on" after a minimap ${how}`, () =>
      visit(
        LINKED,
        async (page) => {
          await page.locator('.dag [data-frame-hint]').first().waitFor({ state: 'visible', timeout: 15_000 });
          await page.keyboard.press('Escape');
          const map = page.locator('.dag .svelte-flow__minimap');
          await map.first().waitFor({ state: 'visible', timeout: 5_000 });
          await sleep(300);
          const before = await captionOf(page);
          // Not vacuous: the first frame says where it is centred, and
          // closing its popover does not move it.
          expect(before.includes('Centred on SFD-229'), `before the minimap ${how} the caption reads "${before}"`);
          const start = await cameraOf(page);
          const box = await map.first().boundingBox();
          const cx = box.x + box.width / 2;
          const cy = box.y + box.height / 2;
          await page.mouse.move(cx, cy);
          if (how === 'drag') {
            await page.mouse.down();
            await page.mouse.move(cx + 20, cy + 10, { steps: 5 });
            await page.mouse.up();
          } else {
            await page.mouse.wheel(0, -200);
          }
          await sleep(500);
          const moved = await cameraOf(page);
          expect(moved !== start, `the minimap ${how} did not move the camera (${start})`);
          if (how === 'wheel') expect(!moved.endsWith('scale(1)'), `the minimap wheel did not zoom (${moved})`);
          const after = await captionOf(page);
          expect(!after.includes('Centred on'), `after a minimap ${how} the caption still reads "${after}"`);
          expect(after.includes('Fit all'), `after a minimap ${how} the caption lost "Fit all" ("${after}")`);
        },
        DESK
      )
    );
  }

  /**
   * D39 — the arrow walk's reveal is a camera move like any other: Right
   * from SFD-229 selects SFD-264 and pans 340px up, leaving SFD-229 off
   * screen under a caption that still named it. A step that pans nothing
   * keeps the caption, so each key is read for which it did.
   */
  await check('the caption stops saying "Centred on" once an arrow walk pans the frame', () =>
    visit(
      LINKED,
      async (page) => {
        const node = () => new URL(page.url()).searchParams.get('node');
        await page.locator('.dag [data-frame-hint]').first().waitFor({ state: 'visible', timeout: 15_000 });
        await page.keyboard.press('Escape');
        await sleep(300);
        const before = await captionOf(page);
        expect(before.includes('Centred on SFD-229'), `before the walk the caption reads "${before}"`);
        const camera = await cameraOf(page);
        let panned = false;
        for (const key of ['ArrowRight', 'ArrowDown', 'ArrowLeft', 'ArrowUp']) {
          const was = node();
          await page.keyboard.press(key);
          await sleep(500);
          const now = await cameraOf(page);
          const caption = await captionOf(page);
          if (now === camera) {
            expect(caption.includes('Centred on SFD-229'), `${key} to ${node()} moved nothing, yet the caption reads "${caption}"`);
            continue;
          }
          panned = true;
          expect(node() !== was, `${key} panned the camera without moving the selection off ${was}`);
          expect(!caption.includes('Centred on'), `${key} panned to ${node()} and the caption still reads "${caption}"`);
          expect(caption.includes('Fit all'), `after the walk's pan the caption lost "Fit all" ("${caption}")`);
          break;
        }
        // Not vacuous: the walk did pan.
        expect(panned, `no arrow from SFD-229 panned the camera (${camera})`);
      },
      DESK
    )
  );

  /**
   * D39, the other way — the first frame keeps its caption until the camera
   * really moves: through its own pan for the linked popover's room, a
   * window resize (the view does not change), and a click on a card already
   * in view, which moves the selection and opens a popover with room to
   * spare, but not the camera.
   */
  await check('the first frame keeps "Centred on" through its own pan and a resize', () =>
    visit(
      LINKED,
      async (page) => {
        await page.locator('.dag [data-frame-hint]').first().waitFor({ state: 'visible', timeout: 15_000 });
        await sleep(500);
        const first = await captionOf(page);
        expect(first.includes('Centred on SFD-229 · selected'), `the linked first frame's caption reads "${first}"`);
        const start = await cameraOf(page);
        // Not vacuous: the first frame did pan, off the view it opened on.
        const seen = await page.evaluate(() => window.__cameras ?? []);
        expect(seen.length >= 2 && seen[0] !== start, `the first frame never panned (${seen.join(' -> ')})`);
        await page.setViewportSize({ width: 1300, height: 820 });
        await sleep(500);
        expect((await cameraOf(page)) === start, 'the resize moved the camera, so this proves nothing');
        const resized = await captionOf(page);
        expect(resized.includes('Centred on SFD-229'), `after a resize the caption reads "${resized}"`);
      },
      DESK,
      recordCameras
    )
  );

  /**
   * D39 — the first frame ENDS where it comes to rest. A popover-room pan
   * after that is a move like any other, for the same popover too: at
   * 1440x900 SFD-229's popover fits once panned, and a window grown to 1100
   * tall re-measures it and pans 140px under "Centred on SFD-229".
   */
  await check('a room pan after the first frame has rested drops "Centred on"', () =>
    visit(
      LINKED,
      async (page) => {
        await page.locator('.dag [data-frame-hint]').first().waitFor({ state: 'visible', timeout: 15_000 });
        await sleep(800);
        const before = await captionOf(page);
        expect(before.includes('Centred on SFD-229'), `the first frame's caption reads "${before}"`);
        const start = await cameraOf(page);
        await page.setViewportSize({ width: 1440, height: 1100 });
        await sleep(800);
        const after = await cameraOf(page);
        // Not vacuous: the grown window did make the page pan for room.
        expect(after !== start, `the resize to 1100 did not pan the camera (${start})`);
        const caption = await captionOf(page);
        expect(!caption.includes('Centred on'), `after a room pan the caption still reads "${caption}"`);
        expect(caption.includes('Fit all'), `after a room pan the caption lost "Fit all" ("${caption}")`);
      },
      DESK
    )
  );

  /**
   * D39 — a press during the first frame's pan interrupts it short of its
   * target and moves nothing itself, so the first frame has to end there:
   * it was left waiting for its pan to land for good, and every later
   * minimap move was skipped as the first frame's (a minimap wheel to 132%
   * under "Centred on SFD-229 · selected").
   */
  await check('after a click mid-way through the first pan, a minimap wheel drops "Centred on"', async () => {
    const view = await browser.newContext({ viewport: DESK });
    try {
      // Where the first frame's pan lands when nothing stops it.
      const ref = await view.newPage();
      await ref.goto(`${base}${LINKED}`, { waitUntil: 'load' });
      await ref.locator('.dag [data-frame-hint]').first().waitFor({ state: 'visible', timeout: 15_000 });
      await sleep(SETTLE_MS);
      const rest = await cameraOf(ref);
      await ref.close();

      const page = await view.newPage();
      await recordCameras(page);
      await page.goto(`${base}${LINKED}`, { waitUntil: 'commit' });
      await page.waitForFunction(() => (window.__cameras ?? []).length >= 2, null, {
        polling: 'raf',
        timeout: 15_000
      });
      const card = await page.locator('.dag .svelte-flow__node[data-id="SFD-229"]').first().boundingBox();
      await page.mouse.click(card.x + card.width / 2, card.y + card.height / 2);
      await sleep(SETTLE_MS);
      const stopped = await cameraOf(page);
      // Not vacuous: the click did stop the pan short of where it lands.
      expect(stopped !== rest, `the click came after the first pan had landed (${rest}), so this proves nothing`);
      await page.keyboard.press('Escape');
      const map = page.locator('.dag .svelte-flow__minimap');
      await map.first().waitFor({ state: 'visible', timeout: 5_000 });
      await sleep(300);
      const before = await captionOf(page);
      expect(before.includes('Centred on SFD-229'), `a click that moved nothing left the caption "${before}"`);
      const box = await map.first().boundingBox();
      await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
      await page.mouse.wheel(0, -200);
      await sleep(500);
      const zoomed = await cameraOf(page);
      expect(zoomed !== stopped && !zoomed.endsWith('scale(1)'), `the minimap wheel did not zoom (${zoomed})`);
      const after = await captionOf(page);
      expect(!after.includes('Centred on'), `after the minimap wheel the caption still reads "${after}"`);
      await page.close();
    } finally {
      await view.close();
    }
  });

  await check('a click on a card already in view keeps "Centred on"', () =>
    visit(
      EPIC,
      async (page) => {
        const node = () => new URL(page.url()).searchParams.get('node');
        await page.locator('.dag [data-frame-hint]').first().waitFor({ state: 'visible', timeout: 15_000 });
        const before = await captionOf(page);
        expect(before.includes('Centred on #2055'), `${EPIC}'s first frame reads "${before}"`);
        const start = await cameraOf(page);
        await page.locator('.dag .svelte-flow__node[data-id="2047"]').first().click();
        await sleep(600);
        expect(node() === '2047', `the click selected ${node()}, not 2047`);
        const after = await cameraOf(page);
        expect(after === start, `the click on 2047 moved the camera (${start} -> ${after}), so this proves nothing`);
        const caption = await captionOf(page);
        expect(caption.includes('Centred on #2055'), `a click that moved nothing left the caption "${caption}"`);
      },
      DESK
    )
  );

  await check('below 1024 the caption keeps "needs you" beside its diamond', () =>
    visit(
      EPIC,
      async (page) => {
        const hint = page.locator('.dag [data-frame-hint]');
        await hint.first().waitFor({ state: 'visible', timeout: 15_000 });
        const read = await hint.first().evaluate((el) => {
          const dia = el.querySelector('.dia');
          const box = dia?.getBoundingClientRect();
          return {
            dia: Boolean(box && box.width > 0 && getComputedStyle(dia).display !== 'none'),
            text: el.innerText.replace(/\s+/g, ' ').trim()
          };
        });
        // Not vacuous: launch-fixes opens on the node that needs you.
        expect(read.dia, 'the caption draws no diamond at 390');
        expect(read.text.startsWith('needs you'), `the caption beside its diamond reads "${read.text}"`);
      },
      { width: 390, height: 844 }
    )
  );

  await check('the lineage hint is plain text, not a pill', () =>
    visit(EPIC, async (page) => {
      const input = page.locator('[data-hint-field] input');
      await input.first().waitFor({ state: 'visible', timeout: 15_000 });
      const read = await input.first().evaluate((el) => {
        const s = getComputedStyle(el);
        const drawn = (side) =>
          Number.parseFloat(s[`border${side}Width`]) > 0 && !/rgba\(.*,\s*0\)$/.test(s[`border${side}Color`]);
        return {
          placeholder: el.placeholder,
          radius: Number.parseFloat(s.borderTopLeftRadius),
          sides: ['Top', 'Right', 'Bottom', 'Left'].filter(drawn)
        };
      });
      expect(read.placeholder.includes('L on a node'), `the hint reads "${read.placeholder}"`);
      expect(read.radius === 0, `the hint is drawn with a ${read.radius}px radius`);
      expect(read.sides.length === 0, `the hint draws a border on ${read.sides.join(', ')}`);
    })
  );
}
