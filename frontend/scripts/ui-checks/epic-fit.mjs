/**
 * The epic page's text fits its boxes, and its timeline keys every colour
 * (#2843, P1 P15 P16).
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 *
 * - The node popover's body scrolls above its pinned action row (#2783). Its
 *   last line lost its descenders on the scroll box's edge, and a body cut at
 *   the cap gave no cue that it scrolled at all.
 * - The canvas status sentence was ellipsised at 1440: "Centred on #2055, the
 *   node that nee…".
 * - The timeline drew red outlines and magenta-washed spans its legend never
 *   named, and drew remediation in the violet DESIGN.md gives to "blocked".
 * - At 390 the timeline fitted the epic's whole window into a ~150px plot and
 *   every bar was a 2-6px dot (D19). Below 640 it says it needs a wider
 *   window instead, and draws no plot.
 * - The linear view opened go-live-2026-09 at 80% (D37), where a card's 11px
 *   status row rendered at 9px. No first frame opens below 100%.
 */
// ui-check-owns: src/routes/(factory)/epics/[ref]/**
// ui-check-owns: fixtures/factory/epics-go-live-2026-09*.json
// ui-check-owns: fixtures/factory/epics-launch-fixes*.json
// ui-check-owns: src/lib/epic/EpicTimeline.svelte
// ui-check-owns: src/lib/epic/canvas-view.ts
import { EPIC } from '../ui-check-lib.mjs';

const GO_LIVE = '/epics/go-live-2026-09';

/** The popover is read at the design width, a short window and a phone. */
const POPOVER_SIZES = [
  { width: 1440, height: 960 },
  { width: 1440, height: 560 },
  { width: 390, height: 844 }
];

/** The room the last line keeps under it once the body is scrolled to its end. */
const LAST_LINE_ROOM = 4;

/**
 * Every rendered text line in the popover body, against the body's scroll box
 * with the body scrolled to its end. A line is one client rect of a text node:
 * a wrapped sentence is several, each its own box.
 */
function readPopoverBody(page) {
  return page.evaluate(() => {
    const body = document.querySelector('[role="dialog"] .pbody');
    if (!body) return null;
    const pop = body.closest('.pop-at');
    const floor = pop?.offsetParent ?? null;
    const scrolls = body.scrollHeight > body.clientHeight + 1;
    // The cue is read at the top, where the reader first meets the body.
    body.scrollTop = 0;
    const cueAtTop = body.dataset.more === 'true';
    body.scrollTop = body.scrollHeight;
    const box = body.getBoundingClientRect();
    // Content coordinates: a line below the fold is still inside the scroll
    // box when it lies within the scrollable height.
    const top = box.top - body.scrollTop;
    const bottom = top + body.scrollHeight;
    const lines = [];
    const walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      if ((node.textContent ?? '').trim() === '') continue;
      const range = document.createRange();
      range.selectNodeContents(node);
      for (const r of range.getClientRects()) {
        if (r.width === 0 || r.height === 0) continue;
        lines.push({
          text: (node.textContent ?? '').trim().slice(0, 40),
          top: r.top,
          bottom: r.bottom,
          left: r.left,
          right: r.right
        });
      }
    }
    return {
      box: { top: box.top, bottom: box.bottom, left: box.left, right: box.right },
      content: { top, bottom },
      lines,
      scrolls,
      cueAtTop,
      sheet: pop?.classList.contains('sheet') ?? false,
      popH: pop?.getBoundingClientRect().height ?? 0,
      floorH: floor?.clientHeight ?? 0
    };
  });
}

/** Every colour the timeline lanes paint and every colour its legend keys,
 * each as the browser serialises it. */
function resolveColours(page) {
  return page.evaluate(() => {
    const probe = document.createElement('span');
    document.body.append(probe);
    const resolve = (value) => {
      probe.style.color = '';
      probe.style.color = value;
      return getComputedStyle(probe).color;
    };
    const painted = (value) =>
      value !== '' && value !== 'none' && !/^rgba\(.*,\s*0\)$/.test(value) && value !== 'transparent';
    const drawn = new Map();
    for (const el of document.querySelectorAll('.lane svg rect, .lane svg circle')) {
      const style = getComputedStyle(el);
      for (const [channel, value] of [
        ['fill', style.fill],
        ['stroke', style.stroke]
      ]) {
        if (channel === 'stroke' && Number.parseFloat(style.strokeWidth) === 0) continue;
        if (!painted(value)) continue;
        // `fill` serialises an rgb() the same way `color` does; route it
        // through the probe so both sides are one serialisation.
        const colour = resolve(value);
        const where = el.closest('a')?.getAttribute('aria-label') ?? `a ${el.tagName} ${channel}`;
        if (!drawn.has(colour)) drawn.set(colour, where);
      }
    }
    const keyed = new Set();
    const byKey = {};
    for (const swatch of document.querySelectorAll('[data-timeline-legend] [data-key-colors]')) {
      const colours = swatch.dataset.keyColors.split('|').map(resolve);
      for (const c of colours) keyed.add(c);
      const key = swatch.closest('[data-key]')?.getAttribute('data-key');
      if (key) byKey[key] = colours[0];
    }
    const blocked = resolve('var(--status-blocked)');
    probe.remove();
    return { drawn: [...drawn.entries()], keyed: [...keyed], byKey, blocked };
  });
}

export async function checks({ check, visit, expect, sleep }) {
  for (const size of POPOVER_SIZES) {
    const at = `${size.width}x${size.height}`;
    await check(`the node popover's text sits inside its scroll box at ${at}`, () =>
      visit(
        `${EPIC}?node=2055`,
        async (page) => {
          await page.locator('[role="dialog"] .pbody').first().waitFor({ state: 'visible', timeout: 15_000 });
          await sleep(300);
          const read = await readPopoverBody(page);
          expect(read !== null, 'no popover body opened on ?node=2055');
          expect(read.lines.length > 0, 'the popover body drew no text');
          for (const line of read.lines) {
            expect(
              line.left >= read.box.left - 0.5 && line.right <= read.box.right + 0.5,
              `"${line.text}" runs ${Math.round(line.left)}..${Math.round(line.right)} ` +
                `outside the body's ${Math.round(read.box.left)}..${Math.round(read.box.right)}`
            );
            expect(
              line.top >= read.content.top - 0.5 && line.bottom <= read.content.bottom + 0.5,
              `"${line.text}" lies outside the body's scrollable height`
            );
          }
          const last = Math.max(...read.lines.map((line) => line.bottom));
          const room = read.box.bottom - last;
          expect(
            room >= LAST_LINE_ROOM,
            `the last line ends ${room.toFixed(1)}px above the body's foot when scrolled to its end ` +
              `(wants ${LAST_LINE_ROOM})`
          );
          if (read.scrolls) {
            expect(read.cueAtTop, 'the body scrolls but shows no fade at its foot');
            // It only scrolls once it has grown to the canvas, less the 8px rim
            // `placePopover` keeps off each edge. A sheet is capped by the window.
            if (!read.sheet) {
              expect(
                read.popH >= read.floorH - 16 - 2,
                `the body scrolls in a ${Math.round(read.popH)}px popover on a ${read.floorH}px canvas`
              );
            }
          }
        },
        size
      )
    );
  }

  for (const route of [EPIC, GO_LIVE]) {
    await check(`the canvas status on ${route} is not ellipsised at 1440`, () =>
      visit(`${route}?view=graph`, async (page) => {
        const said = page.locator('.hint .said');
        await said.first().waitFor({ state: 'visible', timeout: 15_000 });
        const fit = await said.first().evaluate((el) => ({
          text: el.textContent ?? '',
          scroll: el.scrollWidth,
          client: el.clientWidth
        }));
        expect(
          fit.scroll <= fit.client,
          `"${fit.text}" needs ${fit.scroll}px and is given ${fit.client}px`
        );
      })
    );
  }

  for (const route of [EPIC, GO_LIVE]) {
    await check(`every colour on the ${route} timeline has a legend entry`, () =>
      visit(`${route}?view=timeline`, async (page) => {
        await page.locator('[data-timeline-legend]').first().waitFor({ state: 'visible', timeout: 15_000 });
        await page.locator('.lane svg rect').first().waitFor({ state: 'attached', timeout: 10_000 });
        const { drawn, keyed, byKey, blocked } = await resolveColours(page);
        expect(drawn.length > 0, 'the timeline drew no coloured shape');
        const missing = drawn.filter(([colour]) => !keyed.includes(colour));
        expect(
          missing.length === 0,
          `drawn but not in the legend: ${missing.map(([c, where]) => `${c} (${where})`).join(', ')}`
        );
        expect(byKey.remediation !== undefined, 'the legend has no remediation entry');
        expect(
          byKey.remediation !== blocked,
          `remediation is drawn in --status-blocked (${blocked}), DESIGN.md's "blocked on a dependency"`
        );
      })
    );
  }

  for (const route of [EPIC, GO_LIVE]) {
    await check(`at 390 the ${route} timeline says it needs a wider window`, () =>
      visit(
        `${route}?view=timeline`,
        async (page) => {
          const said = page.locator('[data-timeline-narrow]');
          await said.first().waitFor({ state: 'visible', timeout: 15_000 });
          const read = await page.evaluate(() => {
            const shown = (el) => {
              const r = el.getBoundingClientRect();
              return r.width > 0 && r.height > 0;
            };
            return {
              text: (document.querySelector('[data-timeline-narrow]')?.textContent ?? '').replace(/\s+/g, ' ').trim(),
              bars: [...document.querySelectorAll('.lane svg rect')].filter(shown).length,
              scroll: document.documentElement.scrollWidth - document.documentElement.clientWidth
            };
          });
          expect(
            read.text === 'The timeline needs a wider window; Graph shows this epic at any width.',
            `the timeline reads "${read.text}"`
          );
          expect(read.bars === 0, `the timeline still draws ${read.bars} bars at 390`);
          expect(read.scroll <= 0, `the page scrolls sideways by ${read.scroll}px`);
        },
        { width: 390, height: 844 }
      )
    );
  }

  // Not vacuous: from 640 up the plot is drawn and the sentence is not.
  await check('at 768 the timeline draws its plot, not the sentence', () =>
    visit(
      `${EPIC}?view=timeline`,
      async (page) => {
        await page.locator('.lane svg rect').first().waitFor({ state: 'visible', timeout: 15_000 });
        expect(!(await page.locator('[data-timeline-narrow]').first().isVisible()), 'the sentence is drawn at 768');
      },
      { width: 768, height: 1024 }
    )
  );

  for (const size of [
    { width: 1440, height: 900 },
    { width: 1280, height: 800 },
    { width: 768, height: 1024 },
    { width: 390, height: 844 }
  ]) {
    await check(`the first frame of ${GO_LIVE} opens at 100% or more at ${size.width}x${size.height}`, () =>
      visit(
        `${GO_LIVE}?view=graph`,
        async (page) => {
          const flow = page.locator('.dag .svelte-flow__viewport');
          await flow.waitFor({ state: 'attached', timeout: 15_000 });
          const transform = await flow.evaluate((el) => el.style.transform);
          const match = /scale\(([-\d.e]+)\)/.exec(transform);
          expect(match !== null, `no scale() in the viewport transform (${transform})`);
          const zoom = Number(match[1]);
          expect(zoom >= 0.999, `the first frame opens at ${Math.round(zoom * 100)}%`);
          const readout = await page.locator('.dag .zr').first().textContent();
          if (size.width >= 1024) expect(readout === '100%', `the zoom readout says ${readout}`);
        },
        size
      )
    );
  }
}
