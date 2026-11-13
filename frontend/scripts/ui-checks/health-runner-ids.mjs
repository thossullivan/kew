/**
 * Health sets every runner id in the id role and never cuts it, and keeps the
 * phone gutter Work keeps (#2952, design-c8 N10).
 *
 * - The Runners row and the Images table printed runner ids in truncating sans
 *   `text-text-1`, so a long mixed-case id ended in "…" at 390. They are now
 *   data-13 mono in `--text-3`, case kept, and the row grows when an id wraps.
 *   #2897 did the same for the posture lanes (`phone-chips-and-runner-ids.mjs`).
 * - N10: Health kept a 40px gutter at 390, where Work keeps 16px, so
 *   "CONTAINER RUNTIMES" wrapped. Below `sm` its content box starts 16px in.
 *
 * Each runner and each image is served its own long id (`page.route`), so a
 * check that found the first match only would not prove the rest. Where an id
 * lies is read off the text range, not the element box: `truncate` keeps the
 * box inside its row and cuts the ink.
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 */
// ui-check-owns: src/routes/(factory)/health/+page.svelte
const PHONE = { width: 390, height: 844 };
const WIDTHS = [390, 640, 1440];

/** Long and mixed-case, so a cut or a text-transform shows in what is drawn. */
const LONG = 'Claude-Code-Enterprise-Sandboxed-For-Restricted-Production';

/** Serve every runner and every image a distinct long id; hands back the ids. */
function serveLongIds(page, served) {
  return page.route(
    (url) => url.pathname === '/api/health',
    async (route) => {
      const response = await route.fetch();
      const body = await response.json();
      body.runners = body.runners.map((r, n) => ({ ...r, name: `${LONG}-Runner${n + 1}` }));
      body.images = body.images.map((i, n) => ({ ...i, runner: `${LONG}-Image${n + 1}` }));
      served.runners = body.runners.map((r) => r.name);
      served.images = body.images.map((i) => i.runner);
      await route.fulfill({ response, json: body });
    }
  );
}

/**
 * For each id, the leaf element in `section#<id>` whose text is exactly that
 * id: its text as drawn, face, size and colour, the clipping ancestors and the
 * row (the section's direct child) its text range runs past, and what the
 * tokens `--font-mono` and `--text-3` resolve to.
 */
function readIds({ section, ids }) {
  const root = document.querySelector(`main section#${section}`);
  if (root === null) return null;
  const probe = document.createElement('span');
  probe.style.fontFamily = 'var(--font-mono)';
  probe.style.color = 'var(--text-3)';
  root.append(probe);
  const tokens = { mono: getComputedStyle(probe).fontFamily, text3: getComputedStyle(probe).color };
  probe.remove();
  const outside = (ink, box) =>
    ink.left < box.left - 0.5 ||
    ink.right > box.right + 0.5 ||
    ink.top < box.top - 0.5 ||
    ink.bottom > box.bottom + 0.5;
  const found = ids.map((id) => {
    const el = [...root.querySelectorAll('*')].find(
      (e) => e.childElementCount === 0 && e.textContent.trim() === id
    );
    if (el === undefined) return { id, missing: true };
    el.scrollIntoView({ block: 'center' });
    const range = document.createRange();
    range.selectNodeContents(el);
    const ink = range.getBoundingClientRect();
    const cuts = [];
    for (let up = el.parentElement; up !== null; up = up.parentElement) {
      const s = getComputedStyle(up);
      if (s.overflowX === 'visible' && s.overflowY === 'visible') continue;
      if (outside(ink, up.getBoundingClientRect())) cuts.push(`<${up.tagName.toLowerCase()} class="${up.className}">`);
    }
    const own = getComputedStyle(el);
    if (own.overflowX !== 'visible' && el.scrollWidth > el.clientWidth + 0.5) cuts.push('its own box');
    if (ink.right > window.innerWidth + 0.5) cuts.push('the viewport');
    let row = el;
    while (row.parentElement !== null && row.parentElement !== root) row = row.parentElement;
    return {
      id,
      missing: false,
      drawn: el.innerText.trim(),
      transform: own.textTransform,
      size: own.fontSize,
      family: own.fontFamily,
      color: own.color,
      cuts,
      pastRow: outside(ink, row.getBoundingClientRect())
    };
  });
  return { tokens, found };
}

/** Health's scroller's content-box left edge, and every heading that wraps. */
function readGutter() {
  const main = document.querySelector('main');
  const h1 = main?.querySelector('h1');
  if (!h1) return null;
  let box = h1.parentElement;
  while (box !== null && getComputedStyle(box).overflowY !== 'auto') box = box.parentElement;
  if (box === null) return null;
  const r = box.getBoundingClientRect();
  const left = r.left + parseFloat(getComputedStyle(box).paddingLeft);
  const wrapped = [];
  for (const h of main.querySelectorAll('h1, h2, h3')) {
    const range = document.createRange();
    range.selectNodeContents(h);
    const tops = new Set([...range.getClientRects()].map((q) => Math.round(q.top)));
    if (tops.size > 1) wrapped.push(h.textContent.trim());
  }
  return { left, wrapped };
}

export async function checks({ check, visit, expect, sizes: { HEIGHT } }) {
  for (const width of WIDTHS) {
    await check(`Health sets every served runner id whole, in 13px mono --text-3, at ${width}px`, async () => {
      const served = { runners: [], images: [] };
      await visit(
        '/health',
        async (page, problems) => {
          await page.locator('section#runners').waitFor({ state: 'visible', timeout: 15_000 });
          await page.locator('section#images').waitFor({ state: 'visible', timeout: 15_000 });
          for (const [section, ids] of [
            ['runners', served.runners],
            ['images', served.images]
          ]) {
            expect(ids.length > 0, `${width}px: the fixture served no ${section}, so nothing is proved`);
            const read = await page.evaluate(readIds, { section, ids });
            expect(read !== null, `${width}px: Health drew no section#${section}`);
            const { tokens, found } = read;
            for (const f of found) {
              const at = `${width}px ${section}: "${f.id}"`;
              expect(!f.missing, `${at} is not drawn as its own text`);
              expect(f.drawn === f.id, `${at} is drawn "${f.drawn}"`);
              expect(f.transform === 'none', `${at} is transformed by CSS (${f.transform})`);
              expect(f.size === '13px', `${at} is set at ${f.size}, not 13px`);
              expect(f.family === tokens.mono, `${at} is set in ${f.family}, not --font-mono (${tokens.mono})`);
              expect(f.color === tokens.text3, `${at} is ${f.color}, not --text-3 (${tokens.text3})`);
              expect(f.cuts.length === 0, `${at} is cut by ${f.cuts.join(', ')}`);
              expect(!f.pastRow, `${at} runs past its own row`);
            }
          }
          expect(problems.length === 0, `/health at ${width}px logged\n${problems.join('\n')}`);
        },
        { width, height: width === PHONE.width ? PHONE.height : HEIGHT },
        (page) => serveLongIds(page, served)
      );
    });
  }

  await check('at 390 Health keeps a 16px gutter and no heading wraps', () =>
    visit(
      '/health',
      async (page, problems) => {
        await page.locator('section#runtimes, section#images').first().waitFor({ state: 'visible', timeout: 15_000 });
        const read = await page.evaluate(readGutter);
        expect(read !== null, '390: Health drew no scrolling content box under its h1');
        expect(Math.abs(read.left - 16) <= 0.5, `390: Health's content box starts ${read.left}px in, not 16px`);
        expect(read.wrapped.length === 0, `390: Health headings wrap: ${read.wrapped.join(', ')}`);
        expect(problems.length === 0, `/health at 390 logged\n${problems.join('\n')}`);
      },
      PHONE
    )
  );
}
