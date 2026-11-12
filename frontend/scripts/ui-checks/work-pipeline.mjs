/**
 * The Work pipeline at phone width (#2372).
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 */
// ui-check-owns: src/routes/(factory)/work/+page.svelte
// ui-check-owns: src/lib/work/Pipeline.svelte
// ui-check-owns: src/lib/ui/Swimlane.svelte
// ui-check-owns: src/lib/factory/swimlane.ts
// ui-check-owns: src/lib/factory/status.ts
// ui-check-owns: src/lib/ui/StatusMark.svelte
// ui-check-owns: src/lib/work/pipeline.ts
import { SETTLE_MS } from '../ui-check-lib.mjs';

/** The signal tokens a cell's colour is read against. */
const TOKENS = ['--success', '--warn', '--accent-2', '--text-3', '--text-5'];

/**
 * One stage cell of the first lane, read as the browser paints it: the colour
 * of each caption line, the tokens its mark is painted with, and what each
 * token resolves to on this page.
 */
async function readCell(page, stage) {
  return page.evaluate(
    ({ stage, tokens }) => {
      const resolved = {};
      for (const token of tokens) {
        const probe = document.createElement('span');
        probe.style.color = `var(${token})`;
        document.body.append(probe);
        resolved[token] = getComputedStyle(probe).color;
        probe.remove();
      }
      const lane = document.querySelector('[aria-label="Pipeline stages"]');
      const cell = lane?.querySelector(`[data-stage="${stage}"]`);
      if (!cell) return null;
      const captions = [...cell.querySelectorAll('[data-caption]')].map((el) => ({
        text: (el.textContent ?? '').trim(),
        color: getComputedStyle(el).color
      }));
      const mark = cell.querySelector('svg');
      const painted = mark
        ? [...mark.querySelectorAll('*')].flatMap((el) =>
            ['fill', 'stroke']
              .map((a) => el.getAttribute(a) ?? '')
              .filter((v) => v.startsWith('var('))
              .map((v) => v.replace(/^var\((--[\w-]+)\).*$/, '$1'))
          )
        : [];
      // The line leading out of this cell to the next one.
      const line = cell.querySelector('span.h-0[aria-hidden="true"]');
      const segment = line
        ? {
            style: getComputedStyle(line).borderTopStyle,
            color: getComputedStyle(line).borderTopColor
          }
        : null;
      return { captions, painted, resolved, segment };
    },
    { stage, tokens: TOKENS }
  );
}

export async function checks({ browser, base, check, expect, sleep, sizes: { WIDTH, HEIGHT } }) {
  /**
   * #3466 (D25, D26) — SFD-229 published with no scan recorded, and its review
   * round ended "needs changes". The publish cell drew the green success dot
   * beside "no scan recorded", and the review cell the cyan working halo beside
   * a verdict that went against the change. Neither colour meant what it said:
   * an unscanned push is not a success, and a not-ready verdict is amber
   * (DESIGN.md, Run-timeline colours). The legend keys the amber mark. The
   * publish keeps its pending mark, but the PR is up, so the path's lines run
   * through it rather than stopping there.
   */
  for (const [width, height] of [
    [WIDTH, HEIGHT],
    [768, 1024],
    [390, 844]
  ]) {
    await check(`SFD-229's pipeline colours mean what they say at ${width}px`, async () => {
      const ctx = await browser.newContext({ viewport: { width, height } });
      const page = await ctx.newPage();
      try {
        await page.goto(`${base}/work?item=SFD-229`, { waitUntil: 'load' });
        await page
          .locator('[aria-label="Pipeline stages"]')
          .waitFor({ state: 'visible', timeout: 15_000 });
        await sleep(SETTLE_MS);

        const publish = await readCell(page, 'publish');
        expect(publish !== null, `${width}px: no publish cell is drawn`);
        if (!publish) return;
        const { resolved } = publish;
        const unscanned = publish.captions.find((c) => c.text === 'no scan recorded');
        expect(unscanned, `${width}px: the publish cell reads ${JSON.stringify(publish.captions)}`);
        for (const line of publish.captions) {
          expect(
            line.color !== resolved['--success'],
            `${width}px: "${line.text}" in the publish cell is green (--success)`
          );
        }
        expect(
          !publish.painted.includes('--success'),
          `${width}px: the publish mark is painted ${publish.painted}, which includes --success`
        );

        // The PR is up, so the path runs through its pending mark: attested
        // into the publish, working out of it into the review round. A pending
        // publish used to stop the path, and both lines drew the dashed
        // never-reached line.
        const implement = await readCell(page, 'implement');
        expect(
          implement?.segment?.style === 'solid' &&
            implement.segment.color === resolved['--success'],
          `${width}px: implement→publish is ${JSON.stringify(implement?.segment)}, not solid --success`
        );
        expect(
          publish.segment?.style === 'solid' && publish.segment.color === resolved['--accent-2'],
          `${width}px: publish→review is ${JSON.stringify(publish.segment)}, not solid --accent-2`
        );

        const review = await readCell(page, 'review');
        expect(review !== null, `${width}px: no review cell is drawn`);
        if (!review) return;
        const verdict = review.captions.find((c) => c.text === 'needs changes');
        expect(verdict, `${width}px: the review cell reads ${JSON.stringify(review.captions)}`);
        if (verdict) {
          expect(
            verdict.color === resolved['--warn'],
            `${width}px: "needs changes" is ${verdict.color}, not --warn (${resolved['--warn']})`
          );
          expect(
            verdict.color !== resolved['--accent-2'],
            `${width}px: "needs changes" is cyan (--accent-2)`
          );
        }
        expect(
          review.painted.includes('--warn') && !review.painted.includes('--accent-2'),
          `${width}px: the review mark is painted ${review.painted}, not --warn alone`
        );

        // The legend keys the amber mark, and nothing in the section scrolls
        // the page sideways or clips the legend.
        const legend = await page.evaluate(() => {
          const section = document.querySelector('section[aria-label="Pipeline"]');
          const head = section?.querySelector('h2')?.parentElement;
          const mark = head?.querySelector('svg[aria-label="not ready"]');
          const entry = mark?.parentElement?.parentElement;
          return {
            found: Boolean(mark),
            painted: mark
              ? [...mark.querySelectorAll('*')].flatMap((el) =>
                  ['fill', 'stroke'].map((a) => el.getAttribute(a) ?? '')
                )
              : [],
            right: entry ? entry.getBoundingClientRect().right : 0,
            scroll: document.documentElement.scrollWidth,
            client: document.documentElement.clientWidth
          };
        });
        expect(legend.found, `${width}px: the pipeline legend has no "not ready" entry`);
        expect(
          legend.painted.includes('var(--warn)'),
          `${width}px: the legend's not-ready mark is painted ${legend.painted}`
        );
        expect(
          legend.right <= legend.client + 0.5,
          `${width}px: the legend's not-ready entry runs to ${legend.right}px, off the page`
        );
        expect(
          legend.scroll <= legend.client,
          `${width}px: the page scrolls sideways (${legend.scroll} over ${legend.client})`
        );
      } finally {
        await page.close();
        await ctx.close();
      }
    });
  }

  /**
   * #2372 — the Work pipeline at phone width. The lane is fixed stage columns,
   * and at 390px those columns used to be narrower than their own 11px
   * headers: the headers printed as "IMPLEMENTPUBLISHREVIEW" and the captions
   * sat on each other ("f5dcb6a5" over "scan"). The lane now carries a
   * `laneMinWidth` floor and scrolls inside its own box, so the reading to take
   * is the INKED text, not the element boxes — the grid items stayed neatly
   * disjoint the whole time it was broken. A Range over each text node is what
   * the glyphs actually occupy.
   */
  await check('the Work pipeline does not overlap itself at 390px', async () => {
    const phone = await browser.newContext({ viewport: { width: 390, height: 844 } });
    const page = await phone.newPage();
    try {
      await page.goto(`${base}/work?item=2055`, { waitUntil: 'load' });
      const lane = page.locator('[aria-label="Pipeline stages"]');
      await lane.waitFor({ state: 'visible', timeout: 15_000 });
      await sleep(SETTLE_MS);

      // The box scrolls; the page does not.
      const pageBox = await page.evaluate(() => ({
        scroll: document.documentElement.scrollWidth,
        client: document.documentElement.clientWidth
      }));
      expect(
        pageBox.scroll <= pageBox.client,
        `the page scrolls sideways at 390px: scrollWidth ${pageBox.scroll} ` +
          `over clientWidth ${pageBox.client}`
      );
      const box = await lane.evaluate((el) => ({
        scroll: el.scrollWidth,
        client: el.clientWidth
      }));
      expect(box.client <= 390, `the lane is ${box.client}px wide inside a 390px page`);
      expect(
        box.scroll > box.client,
        `the lane did not scroll at 390px (${box.scroll} in ${box.client}): ` +
          'either the floor is gone or the columns are being squeezed again'
      );

      const inked = await lane.evaluate((root) => {
        const walk = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
        const out = [];
        for (let node = walk.nextNode(); node; node = walk.nextNode()) {
          const text = (node.textContent ?? '').trim();
          if (text === '') continue;
          const range = document.createRange();
          range.selectNodeContents(node);
          const r = range.getBoundingClientRect();
          if (r.width === 0 || r.height === 0) continue;
          out.push({ text, x: r.x, y: r.y, w: r.width, h: r.height });
        }
        return out;
      });
      expect(inked.length > 1, `the lane inked ${inked.length} readings, so this proves nothing`);
      // Half a pixel of tolerance: stacked caption lines and the columns of one
      // grid row TOUCH by construction, and touching is not overlapping.
      const over = (a, b) =>
        a.x < b.x + b.w - 0.5 &&
        b.x < a.x + a.w - 0.5 &&
        a.y < b.y + b.h - 0.5 &&
        b.y < a.y + a.h - 0.5;
      for (let i = 0; i < inked.length; i += 1) {
        for (let j = i + 1; j < inked.length; j += 1) {
          expect(
            !over(inked[i], inked[j]),
            `"${inked[i].text}" overlaps "${inked[j].text}" at 390px`
          );
        }
      }
    } finally {
      await page.close();
      await phone.close();
    }
  });
}
