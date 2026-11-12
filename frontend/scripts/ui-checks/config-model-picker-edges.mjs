/**
 * The /config model picker's edges (#2894): the sticky runner header never
 * cuts a row on open nor hides the row an arrow key moves to, the fade never
 * hides the focus ring, and a resize that lets the list fit stops the fade.
 * #2945: a name draws two lines at most, a very long name before the current
 * row cannot push that row out of view, the list ends halfway down a row, and
 * the arrow-key row stays above the fade.
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 */
// ui-check-owns: src/routes/(factory)/config/+page.svelte
// ui-check-owns: src/lib/ModelPicker.svelte
import { readFileSync } from 'node:fs';
import { SHOTS, step } from '../gallery.mjs';

const SHOT = SHOTS.find((s) => s.name === 'config-model-picker');

/** The model the fixture's `agent.model` row has in force. */
const IN_FORCE = 'claude-sonnet-5-5';

const CATALOG = JSON.parse(
  readFileSync(new URL('../../fixtures/factory/models.json', import.meta.url), 'utf8')
);

/** A display name long enough to wrap a row at phone width (#2889: the
 *  fixture's short text hid a wrap once). */
const LONG = 'with an extended context window for long autonomous runs';

/** The catalog with every display name made long. */
function longCatalog() {
  return {
    ...CATALOG,
    models: CATALOG.models.map((m) => ({ ...m, display_name: `${m.display_name} ${LONG}` }))
  };
}

/** Four routable claude-code rows, the one in force first, each name `words`
 *  words longer. Few words: the rows fit the list's box at any width. More:
 *  they wrap, and so overflow it, at 390px first. */
function shortCatalog(words) {
  const routable = CATALOG.models.filter((m) => m.routable && m.runner === 'claude-code');
  const first = routable.find((m) => m.id === IN_FORCE);
  const rest = routable.filter((m) => m.id !== IN_FORCE).slice(0, 3);
  const tail = LONG.split(' ').concat(LONG.split(' ')).slice(0, words).join(' ');
  return {
    ...CATALOG,
    models: [first, ...rest].map((m) => ({ ...m, display_name: `${m.display_name} ${tail}` }))
  };
}

/** A served name of `n` words. */
const wordy = (n) =>
  Array.from({ length: n }, (_, i) => LONG.split(' ')[i % LONG.split(' ').length]).join(' ');

/** The catalog with the model `id` named `n` words long. */
function namedCatalog(id, n) {
  return {
    ...CATALOG,
    models: CATALOG.models.map((m) => (m.id === id ? { ...m, display_name: wordy(n) } : m))
  };
}

/** The catalog with every model named `n` words long. */
const wordyCatalog = (n) => ({
  ...CATALOG,
  models: CATALOG.models.map((m) => ({ ...m, display_name: wordy(n) }))
});

const serve = (payload) => async (page) => {
  await page.route('**/api/models', (route) => route.fulfill({ json: payload }));
};

async function openPicker(page) {
  for (const s of SHOT.then) await step(page, s);
  const listbox = page.locator(SHOT.shows).first();
  await listbox.waitFor({ state: 'visible', timeout: 5_000 });
  return listbox;
}

/** The colours painted at `points` (page coordinates, CSS px) inside `clip`,
 *  read off one screenshot taken at the window's device pixel ratio `scale`:
 *  the computed style says what the border should be, only pixels say it
 *  shows. */
async function paintedAt(page, clip, scale, points) {
  const png = await page.screenshot({ clip });
  const blank = await page.context().newPage();
  try {
    return await blank.evaluate(
      async ([b64, at]) => {
        const img = new Image();
        img.src = `data:image/png;base64,${b64}`;
        await img.decode();
        const canvas = document.createElement('canvas');
        canvas.width = img.width;
        canvas.height = img.height;
        const g = canvas.getContext('2d');
        g.drawImage(img, 0, 0);
        return at.map(([x, y]) => [...g.getImageData(x, y, 1, 1).data.slice(0, 3)]);
      },
      [
        png.toString('base64'),
        points.map(([x, y]) => [Math.floor((x - clip.x) * scale), Math.floor((y - clip.y) * scale)])
      ]
    );
  } finally {
    await blank.close();
  }
}

/** The listbox's fade, read in the page: whether rows lie below (the
 *  component's own word), every mask on the listbox or inside it, and the
 *  scrolling box's geometry. The list scrolls in a box inside the bordered
 *  listbox (#2894 C1), so the scroller is found, not assumed to be `el`. */
function readFade(el) {
  const boxes = [el, ...el.querySelectorAll('*')];
  const scroller = boxes.find((n) => ['auto', 'scroll'].includes(getComputedStyle(n).overflowY)) ?? el;
  return {
    more: el.dataset.moreBelow,
    masks: boxes.map((n) => getComputedStyle(n).maskImage).filter((m) => m && m !== 'none'),
    boxMask: getComputedStyle(el).maskImage,
    scrollTop: scroller.scrollTop,
    fits: scroller.scrollHeight <= scroller.clientHeight + 1
  };
}

/** The #2894 C1 bug, put back by hand for the negative control: a fade on
 *  the bordered box that keeps only the straight 1px border lines opaque. */
const BOX_MASK = [
  'linear-gradient(to bottom, black calc(100% - 2rem - 1px), transparent calc(100% - 1px), black calc(100% - 1px))',
  'linear-gradient(to right, black 1px, transparent 1px, transparent calc(100% - 1px), black calc(100% - 1px))'
].join(', ');

/** `rgb(r, g, b)` (or `rgba`/`color(srgb …)`) as [r, g, b] in 0–255. */
function rgbOf(css) {
  const srgb = css.match(/color\(srgb ([\d.]+) ([\d.]+) ([\d.]+)/);
  if (srgb) return srgb.slice(1, 4).map((v) => Math.round(Number(v) * 255));
  return (css.match(/[\d.]+/g) ?? []).slice(0, 3).map(Number);
}

const near = (a, b) => a.length === 3 && a.every((v, i) => Math.abs(v - b[i]) <= 24);

export async function checks({ check, visit, expect, sleep, sizes: { WIDTH, HEIGHT } }) {
  /**
   * #2894, AC1 — on open, no option lies part under the sticky runner header:
   * every option's box is either wholly above the header's bottom edge
   * (hidden under it or scrolled out) or wholly below it. Read with the
   * fixture catalog and with one whose names are long enough to wrap, since
   * where the edge falls depends on how tall the rows are.
   */
  for (const [name, prepare] of [
    ['fixture names', undefined],
    ['long names', serve(longCatalog())]
  ]) {
    for (const width of [390, WIDTH]) {
      await check(`/config model picker opens with no row cut by its header (${name}, ${width}px)`, () =>
        visit(
          SHOT.route,
          async (page) => {
            const listbox = await openPicker(page);
            const read = await listbox.evaluate((el, inForce) => {
              const top = el.getBoundingClientRect().top + el.clientTop;
              const heads = [...el.querySelectorAll('[role="group"] > :first-child')].map((h) =>
                h.getBoundingClientRect()
              );
              const stuck = heads.find((b) => b.top <= top + 0.5 && b.bottom > top);
              const line = stuck ? stuck.bottom : top;
              const rows = [...el.querySelectorAll('[role="option"]')].map((r) => {
                const b = r.getBoundingClientRect();
                return { id: r.dataset.modelId, top: b.top, bottom: b.bottom };
              });
              return {
                line,
                cut: rows.filter((r) => r.top < line - 0.5 && r.bottom > line + 0.5),
                current: rows.find((r) => r.id === inForce),
                bottom: el.getBoundingClientRect().bottom
              };
            }, IN_FORCE);
            expect(
              read.cut.length === 0,
              `${width}px: options cut by the sticky header at y=${read.line}: ${JSON.stringify(read.cut)}`
            );
            expect(
              read.current !== undefined &&
                read.current.top >= read.line - 0.5 &&
                read.current.bottom <= read.bottom + 0.5,
              `${width}px: the current row ${JSON.stringify(read.current)} is not wholly in view ` +
                `below the header (line ${read.line}, list bottom ${read.bottom})`
            );
          },
          { width, height: width === 390 ? 844 : HEIGHT },
          prepare
        )
      );
    }
  }

  /**
   * #2945 AC1 — a 100-word name on the option before the current model
   * cannot push the current row out of the list on open: the current row is
   * wholly between the sticky header's bottom and the list's bottom, and no
   * option is cut by the header. The option before it is read off the page
   * first, since the list's order is the picker's, not the catalog file's.
   */
  for (const width of [390, WIDTH]) {
    const size = { width, height: width === 390 ? 844 : HEIGHT };
    await check(`/config model picker keeps the current row in view after a 100-word name (${width}px)`, async () => {
      const ids = await visit(
        SHOT.route,
        async (page) => {
          const listbox = await openPicker(page);
          return listbox.evaluate((el) =>
            [...el.querySelectorAll('[role="option"]')].map((r) => r.dataset.modelId)
          );
        },
        size
      );
      const before = ids[ids.indexOf(IN_FORCE) - 1];
      expect(before !== undefined, `${width}px: no option before ${IN_FORCE} in ${JSON.stringify(ids)}`);
      await visit(
        SHOT.route,
        async (page) => {
          const listbox = await openPicker(page);
          const read = await listbox.evaluate(
            (el, [inForce, prior]) => {
              const scroller =
                [...el.querySelectorAll('*')].find((n) =>
                  ['auto', 'scroll'].includes(getComputedStyle(n).overflowY)
                ) ?? el;
              const top = scroller.getBoundingClientRect().top + scroller.clientTop;
              const heads = [...el.querySelectorAll('[role="group"] > :first-child')].map((h) =>
                h.getBoundingClientRect()
              );
              const stuck = heads.find((b) => b.top <= top + 0.5 && b.bottom > top);
              const line = stuck ? stuck.bottom : top;
              const rows = [...el.querySelectorAll('[role="option"]')].map((r) => {
                const b = r.getBoundingClientRect();
                return { id: r.dataset.modelId, top: b.top, bottom: b.bottom, name: r.textContent.length };
              });
              return {
                line,
                floor: top + scroller.clientHeight,
                cut: rows.filter((r) => r.top < line - 0.5 && r.bottom > line + 0.5),
                current: rows.find((r) => r.id === inForce),
                prior: rows.find((r) => r.id === prior)
              };
            },
            [IN_FORCE, before]
          );
          expect(
            read.prior !== undefined && read.prior.name > 400,
            `${width}px: the option before the current one does not carry the long name: ${JSON.stringify(read.prior)}`
          );
          expect(
            read.current !== undefined &&
              read.current.top >= read.line - 0.5 &&
              read.current.bottom <= read.floor + 0.5,
            `${width}px: the current row ${JSON.stringify(read.current)} is not wholly in view ` +
              `below the header (line ${read.line}, list bottom ${read.floor})`
          );
          expect(
            read.cut.length === 0,
            `${width}px: options cut by the sticky header at y=${read.line}: ${JSON.stringify(read.cut)}`
          );
        },
        size,
        serve(namedCatalog(before, 100))
      );
    });
  }

  /**
   * #2945 AC2 — no option name draws more than two lines. The name's text is
   * laid out line by line (a Range's rects), and only the lines inside the
   * name's own box are drawn. Every name is 100 words, so the check also
   * proves it is reading a name that would run past two lines unclamped, and
   * that the whole name is still the row's title.
   */
  for (const width of [390, WIDTH]) {
    await check(`/config model picker draws no name past two lines (${width}px)`, () =>
      visit(
        SHOT.route,
        async (page) => {
          const listbox = await openPicker(page);
          const names = await listbox.evaluate((el) =>
            [...el.querySelectorAll('[role="option"] > :first-child > :first-child')].map((span) => {
              const box = span.getBoundingClientRect();
              const range = document.createRange();
              range.selectNodeContents(span);
              const tops = new Set();
              const drawn = new Set();
              for (const r of range.getClientRects()) {
                if (r.width === 0 || r.height === 0) continue;
                tops.add(Math.round(r.top));
                if (r.top >= box.top - 1 && r.bottom <= box.bottom + 1) drawn.add(Math.round(r.top));
              }
              return {
                id: span.closest('[role="option"]').dataset.modelId,
                lines: tops.size,
                drawn: drawn.size,
                titled: span.getAttribute('title') === span.textContent
              };
            })
          );
          expect(names.length > 1, `${width}px: ${names.length} option name(s) to read`);
          const long = names.filter((n) => n.lines > 2);
          expect(
            long.length > 0,
            `${width}px: no 100-word name lays out past two lines, so the check proves nothing: ${JSON.stringify(names.slice(0, 3))}`
          );
          const over = names.filter((n) => n.drawn > 2 || n.drawn === 0);
          expect(over.length === 0, `${width}px: names drawn past two lines: ${JSON.stringify(over.slice(0, 3))}`);
          const untitled = names.filter((n) => !n.titled);
          expect(
            untitled.length === 0,
            `${width}px: names without their whole text as a title: ${JSON.stringify(untitled.slice(0, 3))}`
          );
        },
        { width, height: width === 390 ? 844 : HEIGHT },
        serve(wordyCatalog(100))
      )
    );
  }

  /**
   * #2945 N12 — with more models than fit, the list ends halfway down a row:
   * on open, the option the list's bottom edge falls on shows between 30% and
   * 70% of its height, so it reads as cut and more models as below.
   */
  for (const [name, prepare] of [
    ['fixture names', undefined],
    ['long names', serve(longCatalog())]
  ]) {
    for (const width of [390, WIDTH]) {
      await check(`/config model picker ends its list halfway down a row (${name}, ${width}px)`, () =>
        visit(
          SHOT.route,
          async (page) => {
            const listbox = await openPicker(page);
            const read = await listbox.evaluate((el) => {
              const scroller =
                [...el.querySelectorAll('*')].find((n) =>
                  ['auto', 'scroll'].includes(getComputedStyle(n).overflowY)
                ) ?? el;
              const floor = scroller.getBoundingClientRect().top + scroller.clientTop + scroller.clientHeight;
              const rows = [...el.querySelectorAll('[role="option"]')].map((r) => r.getBoundingClientRect());
              const last = rows.find((b) => b.top < floor && b.bottom > floor);
              return {
                overflows: scroller.scrollHeight > scroller.clientHeight + 1,
                shown: last ? (floor - last.top) / last.height : null
              };
            });
            expect(read.overflows, `${width}px: the list fits, so the check proves nothing: ${JSON.stringify(read)}`);
            expect(
              read.shown !== null && read.shown >= 0.3 && read.shown <= 0.7,
              `${width}px: the list's last visible row shows ${read.shown} of its height, not 30%-70%`
            );
          },
          { width, height: width === 390 ? 844 : HEIGHT },
          prepare
        )
      );
    }
  }

  /**
   * #2894 R3, #2945 — a keyboard move never leaves the active row under the
   * sticky header nor below the list's bottom: walk from the current row down
   * to the last with ArrowDown, then the whole list back up to the first with
   * ArrowUp (across every group boundary), then the whole list down again
   * with ArrowDown. After every press the active row lies wholly between the
   * header's bottom and the scrolling box's bottom, and after an ArrowDown its
   * bottom is at least 32px above the list's bottom (out of the fade) unless
   * the list is scrolled to its end, where it does not fade. Enter picks the
   * active row, so a row the user cannot read must never be the active one.
   */
  for (const [name, prepare] of [
    ['fixture names', undefined],
    ['long names', serve(longCatalog())]
  ]) {
    for (const width of [390, WIDTH]) {
      await check(
        `/config model picker keeps the active row clear of its header on arrow keys (${name}, ${width}px)`,
        () =>
          visit(
            SHOT.route,
            async (page) => {
              const listbox = await openPicker(page);
              await listbox.focus();
              const count = await listbox.locator('[role="option"]').count();
              expect(count > 1, `${width}px: the list has ${count} option(s) to walk`);
              const readActive = () =>
                listbox.evaluate((el) => {
                  const scroller =
                    [...el.querySelectorAll('*')].find((n) =>
                      ['auto', 'scroll'].includes(getComputedStyle(n).overflowY)
                    ) ?? el;
                  const top = scroller.getBoundingClientRect().top + scroller.clientTop;
                  const heads = [...el.querySelectorAll('[role="group"] > :first-child')].map((h) =>
                    h.getBoundingClientRect()
                  );
                  const stuck = heads.find((b) => b.top <= top + 0.5 && b.bottom > top);
                  const row = document.getElementById(el.getAttribute('aria-activedescendant') ?? '');
                  const box = row?.getBoundingClientRect();
                  return {
                    id: row?.dataset.modelId,
                    top: box?.top,
                    bottom: box?.bottom,
                    line: stuck ? stuck.bottom : top,
                    floor: top + scroller.clientHeight,
                    end: scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 1
                  };
                });
              const ids = await listbox.evaluate((el) =>
                [...el.querySelectorAll('[role="option"]')].map((r) => r.dataset.modelId)
              );
              const hidden = [];
              const faded = [];
              const walks = { down: [], up: [], again: [] };
              for (const [walk, key] of [
                ['down', 'ArrowDown'],
                ['up', 'ArrowUp'],
                ['again', 'ArrowDown']
              ]) {
                for (let n = 0; n < count; n++) {
                  await page.keyboard.press(key);
                  const at = await readActive();
                  walks[walk].push(at.id);
                  if (!(at.id && at.top >= at.line - 0.5 && at.bottom <= at.floor + 0.5)) {
                    hidden.push({ walk, key, ...at });
                  }
                  if (key === 'ArrowDown' && !at.end && !(at.bottom <= at.floor - 32 + 0.5)) {
                    faded.push({ walk, key, ...at });
                  }
                }
              }
              expect(
                walks.down.at(-1) === ids.at(-1) && walks.again.at(-1) === ids.at(-1),
                `${width}px: an ArrowDown walk did not reach the last option ${ids.at(-1)}: ${JSON.stringify(walks)}`
              );
              expect(
                new Set([ids.at(-1), ...walks.up]).size === count && walks.up.at(-1) === ids[0],
                `${width}px: the ArrowUp walk from the last option did not cross every option to the first: ${JSON.stringify(walks.up)}`
              );
              expect(
                new Set([ids[0], ...walks.again]).size === count,
                `${width}px: the ArrowDown walk from the first option did not cross every option: ${JSON.stringify(walks.again)}`
              );
              expect(
                hidden.length === 0,
                `${width}px: ${hidden.length} arrow-key move(s) left the active row out of view ` +
                  `(under the header or below the list): ${JSON.stringify(hidden.slice(0, 3))}`
              );
              expect(
                faded.length === 0,
                `${width}px: ${faded.length} ArrowDown move(s) left the active row within 32px of the ` +
                  `fading bottom edge: ${JSON.stringify(faded.slice(0, 3))}`
              );
            },
            { width, height: width === 390 ? 844 : HEIGHT },
            prepare
          )
      );
    }
  }

  /**
   * #2894, AC2 — with the list focused and fading, the focus border is drawn
   * on all four sides and round both bottom corners: the pixel at the middle
   * of each edge, at each side inside the faded bottom 2rem, and on each
   * bottom corner's arc is the focus colour. Read at 4x, where the arc's
   * middle is a whole device pixel. The negative control masks the bordered
   * box the way the first fix did (straight lines kept, corners not), and the
   * arc samples must then miss the ring: a check that cannot see the corners
   * would pass it.
   */
  const SCALE = 4;
  for (const width of [390, WIDTH]) {
    await check(`/config model picker draws its focus ring on all four sides and corners at ${width}px`, () =>
      visit(
        SHOT.route,
        async (page) => {
          const listbox = await openPicker(page);
          const resting = rgbOf(await listbox.evaluate((el) => getComputedStyle(el).borderBottomColor));
          await listbox.focus();
          await sleep(200);
          const fade = await listbox.evaluate(readFade);
          expect(
            fade.more === 'true' && fade.masks.length > 0,
            `${width}px: the list does not fade, so the check proves nothing: ${JSON.stringify(fade)}`
          );
          expect(
            fade.boxMask === 'none',
            `${width}px: the bordered listbox itself is masked (${fade.boxMask}), so the fade reaches its border`
          );
          const style = await listbox.evaluate((el) => {
            const cs = getComputedStyle(el);
            return {
              border: cs.borderBottomColor,
              width: parseFloat(cs.borderBottomWidth),
              radius: parseFloat(cs.borderBottomLeftRadius)
            };
          });
          const ring = rgbOf(style.border);
          expect(
            !near(ring, resting),
            `${width}px: the focused list's border ${ring} is its resting colour ${resting}`
          );
          expect(style.radius > style.width, `${width}px: the list has no rounded corner to read: ${JSON.stringify(style)}`);

          // The middle of each border line, and of each bottom arc at 45°:
          // the band between the outer radius and the inner one.
          const box = await listbox.boundingBox();
          const half = style.width / 2;
          const right = box.x + box.width;
          const bottom = box.y + box.height;
          const arc = style.radius - (style.radius - half) / Math.SQRT2;
          const straight = {
            top: [box.x + box.width / 2, box.y + half],
            bottom: [box.x + box.width / 2, bottom - half],
            left: [box.x + half, box.y + box.height / 2],
            right: [right - half, box.y + box.height / 2],
            'left, in the fade': [box.x + half, bottom - 12],
            'right, in the fade': [right - half, bottom - 12]
          };
          const arcs = {
            'bottom-left corner': [box.x + arc, bottom - arc],
            'bottom-right corner': [right - arc, bottom - arc]
          };
          const clip = {
            x: Math.floor(box.x),
            y: Math.floor(box.y),
            width: Math.ceil(right) - Math.floor(box.x),
            height: Math.ceil(bottom) - Math.floor(box.y)
          };
          const sample = async (points) => {
            const colours = await paintedAt(page, clip, SCALE, Object.values(points));
            return Object.fromEntries(Object.keys(points).map((k, n) => [k, colours[n]]));
          };

          for (const [side, painted] of Object.entries(await sample({ ...straight, ...arcs }))) {
            expect(
              near(painted, ring),
              `${width}px: the ${side} of the focused list is painted ${painted}, not the ring ${ring}`
            );
          }

          await listbox.evaluate((el, mask) => {
            el.style.maskImage = mask;
          }, BOX_MASK);
          await sleep(100);
          const masked = await sample(arcs);
          for (const [corner, painted] of Object.entries(masked)) {
            expect(
              !near(painted, ring),
              `${width}px: negative control: with the bordered box masked, the ${corner} still ` +
                `reads as the ring ${ring} (${painted}), so the arc samples cannot see a faded corner`
            );
          }
        },
        { width, height: width === 390 ? 844 : HEIGHT, scale: SCALE }
      )
    );
  }

  /**
   * #2894, AC3 — a list that overflows at phone width fades; widen the
   * viewport until its rows stop wrapping and it fits, and the fade is gone
   * without anyone scrolling it. The row in force is first, so the list opens
   * at its top and the resize moves no scrollTop (a scroll would re-measure
   * on its own and hide a missing resize read).
   *
   * Where a row wraps depends on the machine's fonts, so the names are sized
   * here first: the fewest added words whose list overflows on a fresh load at
   * 390px and fits on a fresh load at desktop width. Those loads read only the
   * geometry, never the fade, so the calibration cannot pass a broken fade.
   */
  const fits = (width, words) =>
    visit(
      SHOT.route,
      async (page) => {
        const listbox = await openPicker(page);
        return (await listbox.evaluate(readFade)).fits;
      },
      { width, height: width === 390 ? 844 : HEIGHT },
      serve(shortCatalog(words))
    );

  await check('/config model picker drops its fade when a resize lets the list fit', async () => {
    let words = null;
    for (let n = 0; n <= 16 && words === null; n += 2) {
      if (!(await fits(390, n)) && (await fits(WIDTH, n))) words = n;
    }
    expect(
      words !== null,
      'no four-row catalog both overflows the list at 390px and fits it at desktop width'
    );
    await visit(
      SHOT.route,
      async (page) => {
        const listbox = await openPicker(page);
        const fade = () => listbox.evaluate(readFade);
        const narrow = await fade();
        expect(
          narrow.more === 'true' && narrow.masks.length > 0 && narrow.scrollTop === 0,
          `390px: the four long rows should overflow and fade from the top: ${JSON.stringify(narrow)}`
        );
        await page.setViewportSize({ width: WIDTH, height: HEIGHT });
        await sleep(400);
        const wide = await fade();
        expect(wide.fits, `${WIDTH}px: the four rows still overflow the list: ${JSON.stringify(wide)}`);
        expect(
          wide.more === 'false' && wide.masks.length === 0,
          `${WIDTH}px: the list fits but still fades: ${JSON.stringify(wide)}`
        );
      },
      { width: 390, height: 844 },
      serve(shortCatalog(words))
    );
  });
}
