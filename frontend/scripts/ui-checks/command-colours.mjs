/**
 * The colours jsdom cannot compute: commands and the focus ring (#2540).
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 */
// ui-check-owns: src/lib/ui/Command.svelte
// ui-check-owns: src/app.css
// ui-check-owns: src/lib/needs-you/NeedsYouBlock.svelte
// ui-check-owns: src/lib/work/DispatchLaunchpad.svelte
// ui-check-owns: src/routes/(factory)/work/+page.*
import { SETTLE_MS } from '../ui-check-lib.mjs';

export async function checks({ browser, base, check, expect, sleep, sizes: { WIDTH, HEIGHT } }) {
  /**
   * #2540 (design review c4, D5/D6) — the two colours jsdom cannot compute.
   * A command is text to read and copy, not a live or selected thing, so it is
   * `--text-1`, not cyan; and the keyboard focus ring is cyan, mixed from
   * `--accent-2`, because focus is one of cyan's four meanings and magenta is
   * rationed for "a person is the next move". Both are read as the browser
   * resolved them: the expected value is a probe element painted with the
   * token itself, and the ring's hue is compared by painting both colours on a
   * canvas, because a `color-mix` with `transparent` is not an rgb string.
   */
  for (const width of [390, WIDTH]) {
    await check(`commands read in text-1 and the focus ring is cyan at ${width}px`, async () => {
      const ctx = await browser.newContext({ viewport: { width, height: width === WIDTH ? HEIGHT : 844 } });
      const page = await ctx.newPage();
      try {
        // The needs-you block's command (SFD-229 is the fixture row that
        // carries one) and the launchpad's, which lives in the plan's drawer.
        const readCommands = () =>
          page.evaluate(() => {
            const probe = document.createElement('span');
            probe.style.color = 'var(--text-1)';
            document.body.append(probe);
            const want = getComputedStyle(probe).color;
            probe.remove();
            return [...document.querySelectorAll('code[data-command]')].map((el) => ({
              text: (el.textContent ?? '').trim(),
              color: getComputedStyle(el).color,
              want
            }));
          });
        const commands = [];
        await page.goto(`${base}/work?item=SFD-229`, { waitUntil: 'load' });
        await page.locator('code[data-command]').first().waitFor({ state: 'visible', timeout: 15_000 });
        await sleep(SETTLE_MS);
        commands.push(...(await readCommands()));
        await page.goto(`${base}/work?item=2060`, { waitUntil: 'load' });
        const line = page.locator('section[aria-label="Plan a dispatch"] [data-plan-summary]');
        await line.filter({ hasText: ' / ' }).waitFor({ state: 'visible', timeout: 15_000 });
        await line.locator('button').filter({ hasText: 'Details' }).click();
        const drawer = page.locator('[role="dialog"][aria-modal="true"]');
        await drawer.locator('code[data-command]').first().waitFor({ state: 'visible', timeout: 15_000 });
        commands.push(...(await readCommands()));
        await page.keyboard.press('Escape');
        await drawer.waitFor({ state: 'detached', timeout: 15_000 });

        expect(commands.length >= 2, `${width}px: ${commands.length} commands drawn, want the block's and the launchpad's`);
        for (const command of commands) {
          expect(
            command.color === command.want,
            `${width}px: "${command.text}" is ${command.color}, --text-1 is ${command.want}`
          );
        }

        // A focused control, focused from the KEYBOARD so :focus-visible
        // holds: Tab from the Work tab onto the next one.
        await page.locator('nav[aria-label="Factory views"] a[href="/work"]').focus();
        await page.keyboard.press('Tab');
        // `transition-colors` animates outline-color too: read the settled ring.
        await sleep(SETTLE_MS);
        const ring = await page.evaluate(() => {
          const el = document.activeElement;
          if (!el || el === document.body) return null;
          const style = getComputedStyle(el);
          const rgb = (color) => {
            const canvas = document.createElement('canvas');
            canvas.width = canvas.height = 1;
            const g = canvas.getContext('2d');
            g.fillStyle = color;
            g.fillRect(0, 0, 1, 1);
            const [r, gr, b, a] = g.getImageData(0, 0, 1, 1).data;
            return { r, g: gr, b, a };
          };
          const token = (name) => {
            const probe = document.createElement('span');
            probe.style.color = `var(${name})`;
            document.body.append(probe);
            const color = getComputedStyle(probe).color;
            probe.remove();
            return rgb(color);
          };
          return {
            what: `${el.tagName.toLowerCase()} ${(el.textContent ?? '').trim().slice(0, 30)}`,
            style: style.outlineStyle,
            outline: style.outlineColor,
            got: rgb(style.outlineColor),
            cyan: token('--accent-2'),
            magenta: token('--accent')
          };
        });
        expect(ring !== null, `${width}px: Tab focused nothing`);
        expect(ring.style !== 'none', `${width}px: the focused ${ring.what} draws no outline`);
        expect(ring.got.a > 0, `${width}px: the focused ${ring.what}'s outline ${ring.outline} is invisible`);
        const far = (a, b) => Math.max(Math.abs(a.r - b.r), Math.abs(a.g - b.g), Math.abs(a.b - b.b));
        expect(
          far(ring.got, ring.cyan) <= 12,
          `${width}px: the focus ring on ${ring.what} is ${ring.outline} ` +
            `(rgb ${ring.got.r},${ring.got.g},${ring.got.b}), not --accent-2 ` +
            `(rgb ${ring.cyan.r},${ring.cyan.g},${ring.cyan.b})`
        );
        expect(far(ring.got, ring.magenta) > 12, `${width}px: the focus ring is still magenta`);
      } finally {
        await page.close();
        await ctx.close();
      }
    });
  }
}
