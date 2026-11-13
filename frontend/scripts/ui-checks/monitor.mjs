/**
 * Monitor: the range presets and the spend readouts.
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 */
// ui-check-owns: src/routes/(factory)/monitor/+page.*
// ui-check-owns: src/lib/monitor/EventStream.svelte
// ui-check-owns: src/lib/monitor/readouts.ts
// ui-check-owns: fixtures/factory/audit.json

export async function checks({ check, visit, expect, sleep }) {
  await check('a Monitor preset click changes the range', () =>
    visit('/monitor', async (page) => {
      const presets = page.locator('[aria-label="Range"] button');
      await presets.first().waitFor({ state: 'visible', timeout: 15_000 });
      // The span the range bar writes its own reading into, beside the chips.
      const label = page.locator('[aria-label="Range"] + span');
      const before = ((await label.textContent()) ?? '').trim();
      expect(before.length > 0, 'the range bar reads nothing at all');
      // "7d", the widest span of the clock, and never the default.
      const wide = presets.filter({ hasText: /^7d$/ });
      expect(await wide.count() === 1, 'no 7d range preset to click');
      expect(
        (await wide.getAttribute('aria-pressed')) !== 'true',
        '/monitor opened on 7d, so its click proves nothing'
      );
      await wide.click();
      await sleep(400);
      expect(
        (await wide.getAttribute('aria-pressed')) === 'true',
        'the clicked preset is not aria-pressed'
      );
      const after = ((await label.textContent()) ?? '').trim();
      expect(after !== before, `the range label stayed at "${before}"`);
    })
  );

  /**
   * #2545 D21 — the folded spend readouts at 390 print the figure on one line;
   * the `/ $250` cap that broke it over two is drawn from 1024 up only.
   */
  await check('the Monitor spend figures sit on one line at 390px', async () => {
    await visit(
      '/monitor',
      async (page) => {
        const section = page.locator('section[aria-label="Readouts"]');
        await section.waitFor({ state: 'visible', timeout: 15_000 });
        await page.waitForFunction(() => /\$\d/.test(document.querySelector('section[aria-label="Readouts"]')?.textContent ?? ''), null, { timeout: 15_000 });
        const figures = await section.evaluate((root) =>
          [...root.querySelectorAll('div')]
            // The figure's own box: its first node is the `$178.05` text itself.
            .filter((el) => el.firstChild?.nodeType === Node.TEXT_NODE && /^\s*\$[\d,.]/.test(el.firstChild.textContent))
            .map((el) => {
              const lh = parseFloat(getComputedStyle(el).lineHeight) || parseFloat(getComputedStyle(el).fontSize) * 1.3;
              return { text: el.innerText.trim(), lines: Math.round(el.getBoundingClientRect().height / lh) };
            })
        );
        expect(figures.length >= 2, `found ${figures.length} spend figures, expected Today and This week`);
        for (const f of figures) {
          expect(f.lines <= 1, `"${f.text}" takes ${f.lines} lines at 390px`);
          expect(!f.text.includes('/'), `"${f.text}" still carries its cap at 390px`);
        }
      },
      { width: 390, height: 844 }
    );
  });
}
