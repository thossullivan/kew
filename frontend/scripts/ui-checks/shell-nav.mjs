/**
 * The top nav at phone width (#2418).
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 */
// ui-check-owns: src/lib/shell/FactoryHeader.svelte
// ui-check-owns: src/lib/shell/nav.ts
import { SETTLE_MS } from '../ui-check-lib.mjs';

export async function checks({ browser, base, check, expect, sleep }) {
  /**
   * #2418 — the top nav at phone width. The header's nav was the row's one
   * compressible item (`min-w-0`, a `truncate` label, `overflow-clip`), so at
   * 390px it paid for the readings beside it and the three tabs read
   * `Wo… Monit… Epi…`. A truncated nav label is the one reading on that row
   * with nowhere else to be had: it is the way around the console.
   *
   * `textContent` cannot see this — `text-overflow: ellipsis` is a paint-time
   * effect over a line box that is still the whole string wide, so the DOM text
   * stays `Monitor` and the Range of it reports its full, overflowing width.
   * What gives it away is the label's own BOX: a clipped label is a box
   * narrower than the text laid out in it. Three readings together, then: the
   * computed `text-overflow`, the ink against the box, and the box against the
   * nav it must not be painted out of.
   */
  await check('the top nav keeps Work, Monitor and Epics whole at 390px', async () => {
    const phone = await browser.newContext({ viewport: { width: 390, height: 844 } });
    const page = await phone.newPage();
    try {
      await page.goto(`${base}/work?item=2055`, { waitUntil: 'load' });
      const nav = page.locator('nav[aria-label="Factory views"]');
      await nav.waitFor({ state: 'visible', timeout: 15_000 });
      await sleep(SETTLE_MS);

      const read = await nav.evaluate((root) => {
        const navBox = root.getBoundingClientRect();
        const labels = [];
        for (const a of root.querySelectorAll('a')) {
          const span = a.querySelector('span') ?? a;
          const range = document.createRange();
          range.selectNodeContents(span);
          const ink = range.getBoundingClientRect();
          const box = span.getBoundingClientRect();
          labels.push({
            text: (span.textContent ?? '').trim(),
            inkWidth: ink.width,
            inkRight: ink.right,
            boxWidth: box.width,
            overflow: getComputedStyle(span).textOverflow
          });
        }
        return { navRight: navBox.right, labels };
      });

      expect(read.labels.length === 3, `the nav has ${read.labels.length} links, not three`);
      const words = read.labels.map((l) => l.text).join(' ');
      expect(words === 'Work Monitor Epics', `the nav reads "${words}"`);
      for (const label of read.labels) {
        expect(label.inkWidth > 0, `"${label.text}" inks nothing at 390px`);
        // Half a pixel of tolerance on each: a fractional glyph advance rounds
        // either way, and a label that fits exactly is not a label that is cut.
        expect(
          label.inkWidth <= label.boxWidth + 0.5,
          `"${label.text}" is cut at 390px: ${label.inkWidth.toFixed(1)}px of text ` +
            `laid out in a ${label.boxWidth.toFixed(1)}px box`
        );
        expect(
          label.overflow !== 'ellipsis',
          `"${label.text}" still carries text-overflow: ellipsis at 390px`
        );
        expect(
          label.inkRight <= read.navRight + 0.5,
          `"${label.text}" is painted past the nav at 390px: its ink ends at ` +
            `${label.inkRight.toFixed(1)} against the nav's ${read.navRight.toFixed(1)}`
        );
      }

      // And the counts the nav used to pay for are still reachable: the
      // needs-you mark with its count on the row, and everything that folded
      // below `lg` inside the More menu, one press away.
      const needsYou = page.locator('header svg[aria-label="needs you"]');
      expect(await needsYou.isVisible(), 'the needs-you mark left the row at 390px');
      const more = page.locator('summary[aria-label="More destinations"]');
      expect(await more.isVisible(), 'the More summary left the row at 390px');
      await more.click();
      await sleep(200);
      const folded = await page.locator('summary[aria-label="More destinations"] + div').innerText();
      expect(
        /working/.test(folded),
        `the folded readings are not in the More menu: "${folded.replace(/\n/g, ' / ')}"`
      );

      const pageBox = await page.evaluate(() => ({
        scroll: document.documentElement.scrollWidth,
        client: document.documentElement.clientWidth
      }));
      expect(
        pageBox.scroll <= pageBox.client,
        `the page scrolls sideways at 390px: scrollWidth ${pageBox.scroll} ` +
          `over clientWidth ${pageBox.client}`
      );
    } finally {
      await page.close();
      await phone.close();
    }
  });

  /**
   * D40 (#3470) — the More menu is three groups, Factory, History and Settings,
   * then the theme toggle, on the popover's float tokens: an 11px radius, a
   * `--hair-strong` border and the one soft shadow. The old panel was one flat
   * list of eight links in a square `hair` box with no shadow.
   */
  for (const width of [390, 1440]) {
    await check(`the More menu is Factory, History, Settings, then the theme, at ${width}px`, async () => {
      const ctx = await browser.newContext({ viewport: { width, height: 900 } });
      const page = await ctx.newPage();
      try {
        await page.goto(`${base}/work`, { waitUntil: 'load' });
        const more = page.locator('summary[aria-label="More destinations"]');
        await more.waitFor({ state: 'visible', timeout: 15_000 });
        await sleep(SETTLE_MS);
        await more.click();
        await sleep(200);
        const read = await page.locator('summary[aria-label="More destinations"] + div').evaluate((panel) => {
          const visible = (el) => {
            const box = el.getBoundingClientRect();
            return box.width > 0 && box.height > 0;
          };
          // Walk the panel in document order: group labels and the theme
          // toggle, so the order they are DRAWN in is what is read.
          const order = [];
          for (const el of panel.querySelectorAll('[role="group"], button')) {
            if (!visible(el)) continue;
            if (el.getAttribute('role') === 'group') {
              order.push(`group:${el.getAttribute('aria-label')}`);
            } else if (/light|dark/.test(el.textContent ?? '')) {
              order.push('theme');
            }
          }
          const labels = [...panel.querySelectorAll('[role="group"]')].map((g) => {
            const first = g.firstElementChild;
            return first ? (first.textContent ?? '').trim() : '';
          });
          const style = getComputedStyle(panel);
          const probe = document.createElement('span');
          probe.style.color = 'var(--hair-strong)';
          document.body.appendChild(probe);
          const hairStrong = getComputedStyle(probe).color;
          probe.remove();
          return {
            order,
            labels,
            radius: style.borderTopLeftRadius,
            border: style.borderTopColor,
            hairStrong,
            shadow: style.boxShadow
          };
        });
        const order = read.order.join(' > ');
        expect(
          order === 'group:Factory > group:History > group:Settings > theme',
          `the More menu at ${width}px reads "${order}"`
        );
        expect(
          read.labels.join(' ') === 'Factory History Settings',
          `the More menu's group labels read "${read.labels.join(' / ')}"`
        );
        expect(read.radius === '11px', `the More panel's radius is ${read.radius}, not 11px`);
        expect(
          read.border === read.hairStrong,
          `the More panel's border is ${read.border}, not --hair-strong (${read.hairStrong})`
        );
        expect(
          read.shadow !== '' && read.shadow !== 'none',
          `the More panel has no shadow at ${width}px`
        );
        const pageBox = await page.evaluate(() => ({
          scroll: document.documentElement.scrollWidth,
          client: document.documentElement.clientWidth
        }));
        expect(
          pageBox.scroll <= pageBox.client,
          `the page scrolls sideways at ${width}px with More open`
        );
      } finally {
        await page.close();
        await ctx.close();
      }
    });
  }
}
