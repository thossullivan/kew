/**
 * A clamped title in the node popover can be read in full by touch and
 * keyboard (#3056).
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 *
 * The popover holds its title to two lines (one in a short window) so the
 * action row stays pinned (#2892). The whole of it was then only the
 * paragraph's `title`, which neither a thumb nor a keyboard can reach. The
 * "more" control the needs-you sentence carries (#2938) now unfolds the title
 * with it.
 *
 * The check serves #2055 a title that wraps to three lines through
 * `page.route`, at 1440 and at 390: folded, the clamp cuts the title; Tab from
 * the close button reaches "more"; after one press (Enter) the title hides
 * none of its text, every line of it is wholly inside the title, the popover
 * and the window, and every action-row control is wholly on screen.
 *
 * Negative controls: with `.ptitle.open` left clamped, the title's third line
 * stays cut; with the title's cut left out of the control's condition, a
 * short sentence draws no control and Tab never reaches one.
 */
// ui-check-owns: src/lib/epic/NodePopover.svelte
// ui-check-owns: src/routes/(factory)/epics/[ref]/**
// ui-check-owns: fixtures/factory/epics-launch-fixes*.json
import { EPIC } from '../ui-check-lib.mjs';

const SIZES = [
  { width: 1440, height: 900 },
  { width: 390, height: 844 }
];

const NODE = '2055';

/** Three lines in the 320px box beside a card, in words so it wraps as prose does. */
const LONG_TITLE =
  'review gate lows: the reviewer verdict names an older head than the branch, ' +
  'and the round cap reads the stale one';

/** A one-line sentence, so the only cut in the popover is the title's. */
const SHORT_REASON = 'held: review the change.';

/** Tabs from the popover's close button before the check gives up on the control. */
const TAB_LIMIT = 12;

function servedTitle(page) {
  return page.route('**/api/epics/launch-fixes', async (route) => {
    const response = await route.fetch();
    const payload = await response.json();
    payload.snapshot.nodes = payload.snapshot.nodes.map((n) =>
      n.id === NODE
        ? { ...n, title: LONG_TITLE, attention: { ...n.attention, kind: 'hold', sentence: SHORT_REASON } }
        : n
    );
    await route.fulfill({ json: payload });
  });
}

/** The title's lines against itself, the popover and the window; the control; the action row. */
function readPopover(page) {
  return page.evaluate(() => {
    const pop = document.querySelector('[role="dialog"].pop');
    if (!pop) return null;
    const title = pop.querySelector('.ptitle');
    const within = (r, o) =>
      r.top >= o.top - 0.5 && r.bottom <= o.bottom + 0.5 && r.left >= o.left - 0.5 && r.right <= o.right + 0.5;
    const popBox = pop.getBoundingClientRect();
    const titleBox = title.getBoundingClientRect();
    const view = { top: 0, left: 0, bottom: innerHeight, right: innerWidth };
    const range = document.createRange();
    range.selectNodeContents(title);
    // One rect per line: the Title component may split a line into spans.
    const tops = new Set();
    const lines = [...range.getClientRects()].filter((r) => r.width > 0 && r.height > 0);
    for (const r of lines) tops.add(Math.round(r.top));
    const hidden = lines.filter((r) => !(within(r, titleBox) && within(r, popBox) && within(r, view)));
    const more = pop.querySelector('button.whymore');
    const moreBox = more?.getBoundingClientRect();
    const controls = [...pop.querySelectorAll('.links > *')].map((el) => ({
      text: el.textContent?.trim() ?? '',
      seen: within(el.getBoundingClientRect(), popBox) && within(el.getBoundingClientRect(), view)
    }));
    return {
      text: title.textContent?.trim() ?? '',
      lines: tops.size,
      hidden: hidden.length,
      clipped: title.scrollHeight > title.clientHeight + 1,
      controls,
      more: more
        ? {
            text: more.textContent?.trim() ?? '',
            height: moreBox.height,
            seen: within(moreBox, popBox) && within(moreBox, view),
            controls: more.getAttribute('aria-controls') ?? '',
            focused: document.activeElement === more
          }
        : null,
      titleId: title.id
    };
  });
}

export async function checks({ check, visit, expect, sleep }) {
  for (const size of SIZES) {
    const at = `${size.width}x${size.height}`;
    const phone = size.width < 1024;
    await check(`a three-line popover title unfolds whole by Tab then one Enter at ${at}`, () =>
      visit(
        `${EPIC}?node=${NODE}`,
        async (page) => {
          await page.locator('[role="dialog"] .ptitle').first().waitFor({ state: 'visible', timeout: 15_000 });
          await page.locator('[role="dialog"] [data-history]').first().waitFor({ state: 'visible', timeout: 15_000 });
          await sleep(300);
          const read = () => readPopover(page);
          const folded = await read();
          expect(folded !== null, `no popover opened on ?node=${NODE}`);
          expect(folded.text === LONG_TITLE, `the title reads "${folded.text.slice(0, 60)}…"`);
          // Not vacuous: folded, the clamp cuts the served title.
          expect(folded.clipped, 'the three-line title was not cut while folded');
          expect(folded.more !== null, 'a cut title drew no "more" control');
          expect(folded.more.text === 'more', `the control reads "${folded.more.text}" while folded`);
          expect(folded.more.seen, 'the "more" control is not wholly on screen');
          expect(
            folded.more.controls.split(' ').includes(folded.titleId),
            `the control's aria-controls "${folded.more.controls}" does not name the title`
          );
          if (phone) {
            expect(folded.more.height >= 31.5, `the "more" control is ${folded.more.height}px tall on a phone`);
          }

          // Reached by the keyboard's own path, not `focus()`.
          await page.locator('[role="dialog"] button.close').focus();
          let reached = false;
          for (let i = 0; i < TAB_LIMIT && !reached; i++) {
            await page.keyboard.press('Tab');
            reached = (await read()).more?.focused === true;
          }
          expect(reached, `${TAB_LIMIT} Tabs from the close button never reached the "more" control`);

          await page.keyboard.press('Enter');
          await sleep(200);
          const open = await read();
          expect(open.more?.text === 'less', `after one Enter the control reads "${open.more?.text}"`);
          expect(!open.clipped, 'the unfolded title still hides some of its own text');
          expect(open.lines >= 3, `the unfolded title drew ${open.lines} line(s), not three`);
          expect(open.hidden === 0, `${open.hidden} line rect(s) of the unfolded title are not wholly on screen`);
          for (const control of open.controls) {
            expect(control.seen, `the action control "${control.text}" is not wholly on screen`);
          }
          expect(open.controls.length > 0, 'the popover drew no action row');
        },
        size,
        servedTitle
      )
    );
  }
}
