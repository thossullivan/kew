/**
 * A clamped needs-you sentence in the node popover can be read in full by
 * touch and keyboard (#2938).
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 *
 * The popover holds its needs-you sentence to three lines (two in a short
 * window) so the action row stays pinned (#2892). Its whole text was then only
 * the paragraph's `title`, which neither a thumb nor a keyboard can reach. A cut
 * sentence now carries a focusable "more" control that unfolds it in place and
 * "less" that folds it again; the run and review detail gives up the height,
 * and where even that is not enough the needs-you block scrolls.
 *
 * The check serves #2055 a 300-character hold reason through `page.route`, at a
 * phone and a short desktop window, once in prose and once in wide glyphs: the
 * control is on screen and Tab reaches it, and after a tap and after Enter on
 * it, the paragraph hides none of its text, every line of the sentence's text
 * range can be scrolled wholly into view inside the popover, and every
 * action-row control is wholly on screen. A one-line sentence draws no control.
 * Beside a card, at a rim node the first look panned for, the unfolded box
 * keeps its width and covers no other card.
 *
 * Negative controls: with the control removed the first check fails (the
 * sentence's last lines stay cut by the clamp); with it `tabindex="-1"`, Tab
 * never reaches it; with the open paragraph left to shrink under its
 * `overflow: hidden`, the wide-glyph sentence loses its last lines; with the
 * box widened to 480px while open, the rim node's popover covers its
 * neighbours.
 */
// ui-check-owns: src/routes/(factory)/epics/[ref]/**
// ui-check-owns: fixtures/factory/epics-launch-fixes*.json
import { EPIC } from '../ui-check-lib.mjs';

/** A phone and a short desktop window, the two tightest boxes. */
const SIZES = [
  { width: 390, height: 844 },
  { width: 1440, height: 560 }
];

const NODE = '2055';

/** 300 characters of hold reason, in words so it wraps as prose does. */
const LONG_REASON = 'held: the worktree has changes the last run left uncommitted and the reviewer verdict names an older head '
  .repeat(3)
  .slice(0, 300);

/** 300 characters in the widest Latin glyphs, so the sentence wraps to the
 * most lines a 300-character hold can take. A full stop ends it, as a
 * trailing space would be trimmed from the sentence drawn. */
const WIDE_REASON = 'HELD: WMW MWWM WWMW MWMW WMWM '.repeat(10).slice(0, 299) + '.';

const REASONS = [
  { name: 'a cut needs-you sentence', text: LONG_REASON },
  { name: 'a cut wide-glyph needs-you sentence', text: WIDE_REASON }
];

const SHORT_REASON = 'held: review the change.';

/** Tabs from the popover's close button before the check gives up on the control. */
const TAB_LIMIT = 12;

/** Serve #2055's attention as a hold whose sentence is `sentence`. */
function servedReason(sentence) {
  return (page) =>
    page.route('**/api/epics/launch-fixes', async (route) => {
      const response = await route.fetch();
      const payload = await response.json();
      payload.snapshot.nodes = payload.snapshot.nodes.map((n) =>
        n.id === NODE ? { ...n, attention: { ...n.attention, kind: 'hold', sentence } } : n
      );
      await route.fulfill({ json: payload });
    });
}

/**
 * The sentence's text range, line by line, against the popover and the box
 * the sentence itself draws (whose overflow the clamp hides); every action-row
 * control against the popover, what clips it and the window; the control; and
 * every other card the popover covers.
 *
 * `hidden` is the lines not wholly seen where the block sits now. `unreached`
 * is the lines no scroll of the needs-you block brings wholly into view: the
 * block is stepped through every position and put back where it was.
 */
function readPopover(page, node) {
  return page.evaluate((node) => {
    const pop = document.querySelector('[role="dialog"].pop');
    if (!pop) return null;
    const why = pop.querySelector('.ny .why');
    const ny = pop.querySelector('.ny');
    const rect = (r) => ({ top: r.top, bottom: r.bottom, left: r.left, right: r.right });
    const within = (r, o) =>
      r.top >= o.top - 0.5 && r.bottom <= o.bottom + 0.5 && r.left >= o.left - 0.5 && r.right <= o.right + 0.5;
    const popBox = pop.getBoundingClientRect();
    const floor = pop.closest('.pop-at')?.offsetParent?.getBoundingClientRect() ?? null;
    const view = { top: 0, left: 0, bottom: innerHeight, right: innerWidth };
    const range = document.createRange();
    if (why) range.selectNodeContents(why);
    const lineRects = () => (why ? [...range.getClientRects()].filter((r) => r.width > 0 && r.height > 0) : []);
    // A line is seen when the popover, the paragraph (whose overflow the clamp
    // hides), the block (which scrolls) and the window all hold it whole.
    const seenAt = () => {
      const whyBox = why?.getBoundingClientRect();
      const nyBox = ny?.getBoundingClientRect();
      return lineRects().map(
        (r) => within(r, popBox) && within(r, whyBox) && within(r, nyBox) && within(r, view)
      );
    };
    const lines = lineRects();
    const now = seenAt();
    const hidden = lines.filter((_, i) => !now[i]);
    const reached = [...now];
    if (ny) {
      const start = ny.scrollTop;
      const end = ny.scrollHeight - ny.clientHeight;
      const step = Math.max(1, Math.floor(ny.clientHeight / 2));
      for (let top = 0; ; top = Math.min(end, top + step)) {
        ny.scrollTop = top;
        seenAt().forEach((seen, i) => (reached[i] ||= seen));
        if (top >= end) break;
      }
      ny.scrollTop = start;
    }
    const unreached = lines.filter((_, i) => !reached[i]);
    const whyBox = why?.getBoundingClientRect();
    const nyBox = ny?.getBoundingClientRect();
    const controls = [...pop.querySelectorAll('.links > *')].map((el) => {
      const r = el.getBoundingClientRect();
      const seen = within(r, popBox) && within(r, view) && (!floor || within(r, floor));
      return { text: el.textContent?.trim() ?? '', seen, box: rect(r) };
    });
    const more = pop.querySelector('.ny button.whymore');
    const moreBox = more?.getBoundingClientRect();
    const covers = [...document.querySelectorAll('.dag .svelte-flow__node')]
      .filter((el) => el.dataset.id !== node)
      .filter((el) => {
        const r = el.getBoundingClientRect();
        return r.left < popBox.right && popBox.left < r.right && r.top < popBox.bottom && popBox.top < r.bottom;
      })
      .map((el) => el.dataset.id);
    const where = (r) =>
      `${Math.round(r.top)}..${Math.round(r.bottom)} x ${Math.round(r.left)}..${Math.round(r.right)}`;
    return {
      text: why?.textContent ?? '',
      lines: lines.length,
      hidden: hidden.map(where),
      unreached: unreached.map(where),
      clipped: why ? why.scrollHeight > why.clientHeight + 1 : false,
      pop: rect(popBox),
      why: whyBox ? rect(whyBox) : null,
      ny: nyBox ? rect(nyBox) : null,
      covers,
      controls,
      more: more
        ? {
            text: more.textContent?.trim() ?? '',
            height: moreBox.height,
            seen: within(moreBox, popBox) && within(moreBox, view) && (!floor || within(moreBox, floor)),
            focused: document.activeElement === more
          }
        : null
    };
  }, node);
}

async function opened(page, sleep) {
  await page.locator('[role="dialog"] .ny .why').first().waitFor({ state: 'visible', timeout: 15_000 });
  await page.locator('[role="dialog"] [data-history]').first().waitFor({ state: 'visible', timeout: 15_000 });
  await sleep(300);
}

/** A box as `top..bottom x left..right`, for a failure message. */
const span = (r) =>
  r ? `${Math.round(r.top)}..${Math.round(r.bottom)} x ${Math.round(r.left)}..${Math.round(r.right)}` : 'none';

/** The whole sentence can be read on screen and every action control is seen. */
function expectReadable(expect, read, text, how) {
  expect(read.text === text, `${how}: the sentence reads "${read.text.slice(0, 60)}…"`);
  expect(read.lines > 0, `${how}: the sentence drew no text`);
  expect(!read.clipped, `${how}: the unfolded paragraph still hides some of its own text`);
  expect(
    read.unreached.length === 0,
    `${how}: ${read.unreached.length} of ${read.lines} sentence line(s) no scroll brings into view ` +
      `(${read.unreached.join(', ')}) in the popover ${span(read.pop)}, its paragraph ${span(read.why)} ` +
      `and block ${span(read.ny)}`
  );
  for (const control of read.controls) {
    expect(control.seen, `${how}: the action control "${control.text}" is not wholly on screen`);
  }
  expect(read.controls.length > 0, `${how}: the popover drew no action row`);
}

/** Tab from the popover's close button until focus lands on "more", or give up. */
async function tabToMore(page, read) {
  await page.locator('[role="dialog"] button.close').focus();
  for (let i = 0; i < TAB_LIMIT; i++) {
    await page.keyboard.press('Tab');
    if ((await read()).more?.focused) return true;
  }
  return false;
}

export async function checks({ browser, base, check, visit, expect, sleep }) {
  /**
   * `visit`, in a context with a touchscreen, so the control is tapped as a
   * thumb taps it: the runner's own contexts have none, and Playwright refuses
   * `tap()` without one. Settles as `visit` does: the popover's own waits.
   */
  async function touchVisit(route, body, size, prepare) {
    const context = await browser.newContext({ viewport: size, hasTouch: true, isMobile: size.width < 1024 });
    const page = await context.newPage();
    try {
      await prepare(page);
      await page.goto(`${base}${route}`, { waitUntil: 'load' });
      await page.locator('main').first().waitFor({ state: 'visible', timeout: 15_000 });
      return await body(page);
    } finally {
      await context.close();
    }
  }

  for (const size of SIZES) {
    const at = `${size.width}x${size.height}`;
    const phone = size.width < 1024;
    for (const reason of REASONS) {
      await check(`${reason.name} unfolds whole by a tap and by Tab then Enter at ${at}`, () =>
        touchVisit(
          `${EPIC}?node=${NODE}`,
          async (page) => {
            const read = () => readPopover(page, NODE);
            await opened(page, sleep);
            const folded = await read();
            expect(folded !== null, `no popover opened on ?node=${NODE}`);
            // Not vacuous: folded, the clamp cuts the served sentence.
            expect(folded.hidden.length > 0, 'the 300-character sentence was not cut while folded');
            expect(folded.more !== null, 'a cut sentence drew no "more" control');
            expect(folded.more.text === 'more', `the control reads "${folded.more.text}" while folded`);
            expect(folded.more.seen, 'the "more" control is not wholly on screen');
            if (phone) {
              expect(folded.more.height >= 31.5, `the "more" control is ${folded.more.height}px tall on a phone`);
            }

            const more = page.locator('[role="dialog"] .ny button.whymore');
            await more.tap();
            await sleep(200);
            const tapped = await read();
            expect(tapped.more?.text === 'less', `after a tap the control reads "${tapped.more?.text}"`);
            expectReadable(expect, tapped, reason.text, 'after a tap');

            await more.tap();
            await sleep(200);
            const refolded = await read();
            expect(refolded.hidden.length > 0, '"less" did not fold the sentence again');

            // Reached by the keyboard's own path, not `focus()`, which takes
            // a control Tab skips (`tabindex="-1"`) just as readily.
            expect(
              await tabToMore(page, read),
              `${TAB_LIMIT} Tabs from the close button never reached the "more" control`
            );
            await page.keyboard.press('Enter');
            await sleep(200);
            const entered = await read();
            expect(entered.more?.focused, 'Enter moved focus off the control');
            expectReadable(expect, entered, reason.text, 'after Tab then Enter');
          },
          size,
          servedReason(reason.text)
        )
      );
    }

    await check(`a one-line needs-you sentence draws no "more" control at ${at}`, () =>
      visit(
        `${EPIC}?node=${NODE}`,
        async (page) => {
          await opened(page, sleep);
          const read = await readPopover(page, NODE);
          expect(read !== null, `no popover opened on ?node=${NODE}`);
          expect(read.text === SHORT_REASON, `the sentence reads "${read.text}"`);
          expect(read.lines === 1, `the one-line sentence drew ${read.lines} lines`);
          expect(read.more === null, 'a sentence that fits draws a "more" control');
        },
        size,
        servedReason(SHORT_REASON)
      )
    );
  }

  // #2055 sits in the graph's rightmost column, past the canvas's right edge:
  // opening it pans to make room beside it, and DagView placed the box at the
  // size it measured then. Unfolded, the box must stay that size and clear of
  // every other card, not be laid over them.
  for (const size of [
    { width: 1440, height: 900 },
    { width: 1440, height: 560 }
  ]) {
    const at = `${size.width}x${size.height}`;
    await check(`an unfolded sentence at a rim node covers no other card at ${at}`, () =>
      visit(
        `${EPIC}?node=${NODE}`,
        async (page) => {
          await opened(page, sleep);
          const folded = await readPopover(page, NODE);
          expect(folded !== null, `no popover opened on ?node=${NODE}`);
          expect(folded.covers.length === 0, `folded, the popover covers ${folded.covers.join(', ')}`);
          expect(folded.more !== null, 'a cut sentence drew no "more" control');
          await page.locator('[role="dialog"] .ny button.whymore').click();
          await sleep(600);
          const open = await readPopover(page, NODE);
          expect(open.more?.text === 'less', `after a click the control reads "${open.more?.text}"`);
          expect(open.covers.length === 0, `unfolded, the popover covers card(s) ${open.covers.join(', ')}`);
          const width = (r) => Math.round(r.right - r.left);
          expect(
            width(open.pop) === width(folded.pop),
            `unfolding changed the popover's width from ${width(folded.pop)} to ${width(open.pop)}px`
          );
        },
        size,
        servedReason(LONG_REASON)
      )
    );
  }
}
