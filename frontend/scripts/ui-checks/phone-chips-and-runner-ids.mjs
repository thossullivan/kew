/**
 * The run chip row shows that it scrolls at 390, and Health sets runner ids
 * as ids (#2897, design-c7 R9 R10).
 *
 * - R9: at 390 the run-event chip row cut "Cost spik" at the edge with no cue
 *   that it scrolls. Its right edge fades while chips lie past it, and not
 *   once the row is scrolled to its end, nor while a chip in it holds
 *   keyboard focus (the mask would dim the chip and its focus ring).
 * - R10: Health uppercased runner ids beside lane names ("CLAUDE-CODE"), as P3
 *   was in the drawer. #2845's no-uppercased-id check
 *   (`launchpad-drawer-type.mjs`) read Health for model ids and dotted keys
 *   only, so it now also reads Health for every runner id the page is served.
 *
 * Both read a long served string (`page.route`), not only the fixture's short
 * text: #2889 shipped because the fixture caption was short.
 *
 * The fade is read off pixels, not the computed `mask-image`: a transparent
 * mask or a left-edge gradient is still a mask. The runner id is read for its
 * case, size, face and colour, not only for the absence of `uppercase`.
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 */
// ui-check-owns: src/lib/runs/RunEvents.svelte
// ui-check-owns: src/routes/(factory)/runs/[id]/+page.svelte
// ui-check-owns: src/routes/(factory)/health/+page.svelte
const PHONE = { width: 390, height: 844 };

/** A kind no label names, so the chip prints it whole and long. */
const LONG_KIND = 'cost_spike_over_the_lane_budget_for_this_run';
/** A runner id longer than any the fixture carries, in mixed case so any
 * text-transform shows in what is drawn. */
const LONG_RUNNER = 'Claude-Code-Enterprise-Sandboxed';
/** Longer still, served in every lane: from 640 the posture cell sets the id
 * beside its lane, and an id this long must wrap inside its row. */
const LONGER_RUNNER = 'Claude-Code-Enterprise-Sandboxed-For-Restricted-Production';
/** The widths where the posture cell is inline, from `sm` up. */
const INLINE_WIDTHS = [640, 700, 768, 1024];

/** The colour painted behind the chip row while its pixels are read. */
const PROBE = { r: 255, g: 0, b: 0 };
/** How far a pixel may stray from `PROBE` and still be unfaded. */
const NEAR = 20;
/** The fade's width: 2rem. */
const FADE_PX = 32;

/** The chip row only: the run timeline above it draws phase `rect`s with
 * role `button`, `aria-pressed` and a "filter to <kind>" label. */
const CHIP_ROW = 'div[data-more-right]';
/** The long kind's chip, and nothing else on the page. */
const LONG_CHIP = `${CHIP_ROW} > button[title="${LONG_KIND}"]`;

/** The chip row: its scroll and its mask. */
function readChipRow(section) {
  const row = section.querySelector('div[data-more-right]');
  const style = getComputedStyle(row);
  return {
    chips: row.querySelectorAll('button[aria-pressed]').length,
    scrollLeft: row.scrollLeft,
    clientWidth: row.clientWidth,
    scrollWidth: row.scrollWidth,
    mask: style.maskImage || style.webkitMaskImage || 'none'
  };
}

/**
 * How far each column of the chip row's top padding is from `PROBE`, as
 * drawn. The row's background is set to `PROBE` (the mask applies to it as to
 * the chips), and the strip read is 4px down, above every chip: an unmasked
 * column reads ~0, a faded one reads as far as the fade has taken it.
 */
async function rowFade(page, row) {
  await row.evaluate((el, { r, g, b }) => el.style.setProperty('background-color', `rgb(${r}, ${g}, ${b})`), PROBE);
  const png = await row.screenshot({ animations: 'disabled', scale: 'css' });
  return page.evaluate(
    async ({ b64, probe }) => {
      const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
      const image = await createImageBitmap(new Blob([bytes], { type: 'image/png' }));
      const canvas = document.createElement('canvas');
      canvas.width = image.width;
      canvas.height = image.height;
      const g = canvas.getContext('2d');
      g.drawImage(image, 0, 0);
      const data = g.getImageData(0, 4, image.width, 1).data;
      const out = [];
      for (let x = 0; x < image.width; x++) {
        const [r, gr, b] = data.slice(x * 4, x * 4 + 3);
        out.push(Math.max(Math.abs(r - probe.r), Math.abs(gr - probe.g), Math.abs(b - probe.b)));
      }
      return out;
    },
    { b64: png.toString('base64'), probe: PROBE }
  );
}

/**
 * Every visible element under `main` whose own text holds one of `ids` as a
 * whole word, with its case, face, size and colour as drawn, the clipping
 * ancestors its text runs past, and the face and
 * colour the tokens `--font-mono` and `--text-3` resolve to.
 */
function runnerIdFaces(ids) {
  const root = document.querySelector('main');
  if (root === null) return null;
  const escape = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const pattern = new RegExp(`(^|[^\\w.-])(${ids.map(escape).join('|')})($|[^\\w.-])`);
  const out = [];
  for (const el of root.querySelectorAll('*')) {
    const own = [...el.childNodes]
      .filter((n) => n.nodeType === Node.TEXT_NODE)
      .map((n) => n.textContent)
      .join(' ')
      .trim();
    if (own === '' || !pattern.test(own)) continue;
    // Below the fold is not a cut: bring the element into the page's own
    // scroller's view before reading where its text lies.
    el.scrollIntoView({ block: 'center' });
    const box = el.getBoundingClientRect();
    if (box.width === 0 || box.height === 0) continue;
    // Where the text itself is drawn, against every ancestor that clips it:
    // `innerText` reads the whole id even where `truncate` ends it in "…".
    const range = document.createRange();
    range.selectNodeContents(el);
    const ink = range.getBoundingClientRect();
    const clippedBy = [];
    for (let up = el; up !== null; up = up.parentElement) {
      const s = getComputedStyle(up);
      if (s.overflowX === 'visible' && s.overflowY === 'visible') continue;
      const clip = up.getBoundingClientRect();
      const inside =
        ink.left >= clip.left - 0.5 &&
        ink.right <= clip.right + 0.5 &&
        ink.top >= clip.top - 0.5 &&
        ink.bottom <= clip.bottom + 0.5;
      if (!inside) {
        clippedBy.push(`<${up.tagName.toLowerCase()} class="${up.className}">`);
      }
    }
    if (ink.right > window.innerWidth + 0.5) clippedBy.push('the viewport');
    const style = getComputedStyle(el);
    out.push({
      clippedBy,
      text: own,
      drawn: el.innerText.trim(),
      transform: style.textTransform,
      size: style.fontSize,
      family: style.fontFamily,
      color: style.color
    });
  }
  const probe = document.createElement('span');
  probe.style.fontFamily = 'var(--font-mono)';
  probe.style.color = 'var(--text-3)';
  root.append(probe);
  const tokens = {
    mono: getComputedStyle(probe).fontFamily,
    text3: getComputedStyle(probe).color
  };
  probe.remove();
  return { found: out, tokens };
}

/**
 * Every posture lane's runner id: where its text is drawn against its own
 * row, and whether the lane name beside it is cut short.
 */
function postureRunnerRows(id) {
  const section = document.querySelector('section#posture');
  if (section === null) return null;
  const out = [];
  for (const el of section.querySelectorAll('span')) {
    if (el.childElementCount !== 0 || el.textContent.trim() !== id) continue;
    const row = el.parentElement?.parentElement;
    const lane = el.previousElementSibling;
    if (row === null || row === undefined || lane === null) continue;
    row.scrollIntoView({ block: 'center' });
    const range = document.createRange();
    range.selectNodeContents(el);
    const ink = range.getBoundingClientRect();
    const box = row.getBoundingClientRect();
    out.push({
      lane: lane.textContent.trim(),
      laneCut: lane.scrollWidth > lane.clientWidth + 0.5,
      above: Math.max(0, Math.round(box.top - ink.top)),
      below: Math.max(0, Math.round(ink.bottom - box.bottom)),
      past: Math.max(0, Math.round(ink.right - box.right))
    });
  }
  return out;
}

export async function checks({ check, visit, expect, sleep, sizes: { WIDTH, HEIGHT } }) {
  const serveLongKind = async (page) => {
    await page.route('**/api/runs/f5dcb6a5/audit*', async (route) => {
      const response = await route.fetch();
      const payload = await response.json();
      if (new URL(route.request().url()).searchParams.get('after_seq') === '0' && payload.events.length > 0) {
        // A ledger sequence of its own: RunEvents keys rows by `seq`, and a
        // repeated one is a duplicate key Svelte refuses.
        const last = payload.events[payload.events.length - 1];
        const seq = Math.max(last.seq, payload.next_seq ?? 0) + 1;
        payload.events.push({ ...last, seq, event: LONG_KIND, payload: {} });
        payload.next_seq = seq;
      }
      await route.fulfill({ response, json: payload });
    });
  };

  await check('at 390 the run chip row fades at its right edge from the start, and not at its end', () =>
    visit(
      '/runs/f5dcb6a5',
      async (page, problems) => {
        const section = page.locator('section[aria-label="Run events"]');
        await section
          .locator(`${CHIP_ROW} > button[aria-pressed]`)
          .first()
          .waitFor({ state: 'visible', timeout: 15_000 });
        await section.locator(LONG_CHIP).waitFor({
          state: 'visible',
          timeout: 15_000
        });
        await sleep(200);
        expect(problems.length === 0, `/runs/f5dcb6a5 at 390 logged\n${problems.join('\n')}`);
        const row = section.locator(CHIP_ROW);
        const start = await section.evaluate(readChipRow);
        expect(start.chips >= 5, `the run drew ${start.chips} kind chips, too few to prove the row`);
        expect(
          start.scrollWidth > start.clientWidth,
          `the chip row does not overflow at 390 (${start.scrollWidth} <= ${start.clientWidth})`
        );
        expect(start.scrollLeft === 0, `the chip row opened scrolled to ${start.scrollLeft}`);

        const opened = await rowFade(page, row);
        const w = opened.length;
        expect(w > 4 * FADE_PX, `the chip row read ${w}px wide, too narrow to hold a 2rem fade`);
        const body = opened.slice(0, w - FADE_PX - 4);
        const faded = body.findIndex((d) => d > NEAR);
        expect(
          faded === -1,
          `the chip row is faint at x=${faded} of ${w}, left of its last 2rem (off by ${body[faded]})`
        );
        const mid = opened[w - FADE_PX / 2];
        const edge = opened[w - 2];
        expect(
          mid > NEAR && edge > mid,
          `the chip row does not fade across its last 2rem while chips lie past it ` +
            `(off by ${mid} at -1rem, ${edge} at its edge)`
        );

        await section.evaluate((el) => {
          const r = el.querySelector('div[data-more-right]');
          r.scrollLeft = r.scrollWidth;
        });
        await sleep(300);
        const end = await section.evaluate(readChipRow);
        expect(
          end.scrollLeft + end.clientWidth >= end.scrollWidth - 1,
          `the chip row did not scroll to its end (${end.scrollLeft} + ${end.clientWidth} < ${end.scrollWidth})`
        );
        const ended = await rowFade(page, row);
        const faint = ended.findIndex((d) => d > NEAR);
        expect(
          faint === -1,
          `the chip row still fades at its end: x=${faint} of ${ended.length} is off by ${ended[faint]}`
        );
        expect(problems.length === 0, `/runs/f5dcb6a5 at 390 logged\n${problems.join('\n')}`);
      },
      PHONE,
      serveLongKind
    )
  );

  await check('at 390 a keyboard-focused chip inside the fade is drawn unfaded', () =>
    visit(
      '/runs/f5dcb6a5',
      async (page, problems) => {
        const section = page.locator('section[aria-label="Run events"]');
        await section.locator(LONG_CHIP).waitFor({ state: 'visible', timeout: 15_000 });
        await sleep(200);
        // The first focusable chip that reaches into the last 2rem and is not
        // the row's last: the one the fade would dim.
        const pick = await section.evaluate((el, fadePx) => {
          const row = el.querySelector('div[data-more-right]');
          const edge = row.getBoundingClientRect().right - fadePx;
          const buttons = [...row.querySelectorAll(':scope > button:not([disabled])')];
          const at = buttons.findIndex(
            (b, i) => i > 0 && i < buttons.length - 1 && b.getBoundingClientRect().right > edge
          );
          if (at === -1) return null;
          buttons[at - 1].setAttribute('data-ui-check', 'before-faded');
          buttons[at].setAttribute('data-ui-check', 'faded');
          return { at, of: buttons.length, title: buttons[at].title || buttons[at].textContent };
        }, FADE_PX);
        expect(pick !== null, 'no chip but the last reaches into the chip row\'s fade at 390');
        // Reach it from the keyboard, so it draws its :focus-visible ring.
        await section.locator(`${CHIP_ROW} > button[data-ui-check="before-faded"]`).focus();
        await page.keyboard.press('Tab');
        await sleep(300);
        const focus = await section.evaluate((el, fadePx) => {
          const row = el.querySelector('div[data-more-right]');
          const chip = row.querySelector('button[data-ui-check="faded"]');
          const r = row.getBoundingClientRect();
          const c = chip.getBoundingClientRect();
          return {
            active: document.activeElement === chip,
            visible: chip.matches(':focus-visible'),
            pastEdge: row.dataset.moreRight === 'true',
            inFade: c.right > r.right - fadePx && c.left < r.right,
            from: Math.max(0, Math.round(c.left - r.left)),
            to: Math.min(Math.round(r.width), Math.round(c.right - r.left))
          };
        }, FADE_PX);
        expect(focus.active && focus.visible, `Tab did not give the chip "${pick.title}" keyboard focus`);
        expect(focus.pastEdge, 'the chip row has no chips past its edge once a chip is focused, so nothing is proved');
        expect(focus.inFade, `the focused chip "${pick.title}" lies outside the row's last 2rem`);
        const drawn = await rowFade(page, section.locator(CHIP_ROW));
        const over = drawn.slice(focus.from, focus.to);
        const faint = over.findIndex((d) => d > NEAR);
        expect(
          faint === -1,
          `the focused chip "${pick.title}" is faded: x=${focus.from + faint} is off by ${over[faint]}`
        );
        const anywhere = drawn.findIndex((d) => d > NEAR);
        expect(
          anywhere === -1,
          `the chip row still fades while a chip holds focus: ` +
            `x=${anywhere} of ${drawn.length} is off by ${drawn[anywhere]}`
        );
        expect(problems.length === 0, `/runs/f5dcb6a5 at 390 logged\n${problems.join('\n')}`);
      },
      PHONE,
      serveLongKind
    )
  );

  await check(`at ${WIDTH}px the wrapped run chip row draws no fade`, () =>
    visit(
      '/runs/f5dcb6a5',
      async (page, problems) => {
        const section = page.locator('section[aria-label="Run events"]');
        await section
          .locator(`${CHIP_ROW} > button[aria-pressed]`)
          .first()
          .waitFor({ state: 'visible', timeout: 15_000 });
        await section.locator(LONG_CHIP).waitFor({
          state: 'visible',
          timeout: 15_000
        });
        await sleep(200);
        expect(problems.length === 0, `/runs/f5dcb6a5 at ${WIDTH}px logged\n${problems.join('\n')}`);
        const read = await section.evaluate(readChipRow);
        expect(read.mask === 'none', `the wrapped chip row fades at ${WIDTH}px: ${read.mask}`);
      },
      { width: WIDTH, height: HEIGHT },
      serveLongKind
    )
  );

  for (const size of [{ width: WIDTH, height: HEIGHT }, PHONE]) {
    const at = `${size.width}px`;
    await check(`no runner id is uppercased on Health at ${at}`, async () => {
      let ids = [];
      await visit(
        '/health',
        async (page) => {
          await page.locator('section#posture').waitFor({ state: 'visible', timeout: 15_000 });
          const read = await page.evaluate(runnerIdFaces, ids);
          expect(read !== null, `${at}: Health drew no main`);
          const { found, tokens } = read;
          const shouted = found.filter((f) => f.transform !== 'none').map((f) => `"${f.text}" (${f.transform})`);
          // Not vacuous: the long id beside its lane is among what was read.
          const lane = found.find((f) => f.text === LONG_RUNNER);
          expect(lane !== undefined, `${at}: the served runner id "${LONG_RUNNER}" is not on Health as its own text`);
          expect(shouted.length === 0, `${at}: runner ids transformed by CSS: ${shouted.join(', ')}`);
          expect(
            lane.drawn === LONG_RUNNER,
            `${at}: the lane's runner id is drawn "${lane.drawn}", served "${LONG_RUNNER}"`
          );
          expect(
            lane.clippedBy.length === 0,
            `${at}: the lane's runner id "${LONG_RUNNER}" is cut by ${lane.clippedBy.join(', ')}`
          );
          expect(lane.size === '13px', `${at}: the lane's runner id is set at ${lane.size}, not 13px`);
          expect(
            lane.family === tokens.mono,
            `${at}: the lane's runner id is set in ${lane.family}, not --font-mono (${tokens.mono})`
          );
          expect(
            lane.color === tokens.text3,
            `${at}: the lane's runner id is ${lane.color}, not --text-3 (${tokens.text3})`
          );
        },
        size,
        async (page) => {
          await page.route('**/api/health*', async (route) => {
            const response = await route.fetch();
            const payload = await response.json();
            const rename = (r) => (r === 'claude-code' ? LONG_RUNNER : r);
            payload.posture = payload.posture.map((l) => ({
              ...l,
              runner: rename(l.runner)
            }));
            ids = [
              ...new Set([
                ...payload.posture.map((l) => l.runner),
                ...payload.runners.map((r) => r.name),
                ...payload.images.map((i) => i.runner)
              ])
            ];
            await route.fulfill({ response, json: payload });
          });
        }
      );
    });
  }

  await check(`a long posture runner id stays inside its row at ${INLINE_WIDTHS.join(', ')}px`, async () => {
    for (const width of INLINE_WIDTHS) {
      await visit(
        '/health',
        async (page, problems) => {
          await page.locator('section#posture').waitFor({ state: 'visible', timeout: 15_000 });
          const rows = await page.evaluate(postureRunnerRows, LONGER_RUNNER);
          expect(rows !== null, `${width}px: Health drew no posture section`);
          expect(rows.length > 0, `${width}px: no posture lane drew the served runner id "${LONGER_RUNNER}"`);
          for (const r of rows) {
            expect(
              r.above === 0 && r.below === 0 && r.past === 0,
              `${width}px: lane "${r.lane}"'s runner id runs past its row ` +
                `(${r.above}px above, ${r.below}px below, ${r.past}px right)`
            );
            expect(!r.laneCut, `${width}px: lane "${r.lane}"'s name is cut short beside its runner id`);
          }
          expect(problems.length === 0, `/health at ${width}px logged\n${problems.join('\n')}`);
        },
        { width, height: HEIGHT },
        async (page) => {
          await page.route('**/api/health*', async (route) => {
            const response = await route.fetch();
            const payload = await response.json();
            payload.posture = payload.posture.map((l) => ({ ...l, runner: LONGER_RUNNER }));
            await route.fulfill({ response, json: payload });
          });
        }
      );
    }
  });
}
