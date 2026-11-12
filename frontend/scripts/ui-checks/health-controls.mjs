/**
 * Health: the phone controls fit, and the headline names what it draws in red
 * (#2849, design-c6 P17 P21).
 *
 * - P17. At 390 the "show problems only" chip wrapped to three lines inside its
 *   pill, and the data's age to three more beside it. Below 640 the chip and
 *   Refresh share a line and the age takes its own.
 * - P21. "Nothing required is down." sat above red `credential placeholder`
 *   readouts on loops-only lanes. Every red word on the page is now either
 *   named in the headline or on a row the headline counts.
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 */
// ui-check-owns: src/routes/(factory)/health/+page.svelte
// ui-check-owns: src/lib/health.ts
// ui-check-owns: src/routes/(factory)/health/+page.ts
// ui-check-owns: fixtures/factory/readiness.json

const PHONE = { width: 390, height: 844 };

/** The build command the doctor would serve for the stock image. */
const BUILD = 'docker build -f docker/agent.Dockerfile -t kew-agent:0.9.0 .';

/**
 * D41 (#3470) — a routed image that is missing, and the readiness read whose
 * `route-image` check carries its `fix`. Routed here rather than read off the
 * fixture world, which may make the image present (#3461). A lane on a real
 * credential and a week of runs with one recording no runtime are routed in
 * too, so the two internal words have rows to be printed on.
 */
const withMissingImage = (page) =>
  Promise.all([
    page.route(
      (url) => url.pathname === '/api/health',
      async (route) => {
        const response = await route.fetch();
        const body = await response.json();
        body.images = [
          { runner: 'claude-code', ref: 'kew-agent:0.9.0', source: 'images.claude-code', routed: true, present: false },
          ...body.images.filter((i) => i.runner !== 'claude-code')
        ];
        body.observed_7d = { docker: 34, '': 2 };
        body.posture = body.posture.map((lane, n) => (n === 0 ? { ...lane, credential_mode: 'real' } : lane));
        await route.fulfill({ response, json: body });
      }
    ),
    page.route(
      (url) => url.pathname === '/api/readiness',
      (route) =>
        route.fulfill({
          json: {
            schema: 1,
            ok: false,
            checked_at: '2026-11-07T10:00:00Z',
            checks: [
              {
                id: 'route-image',
                status: 'fail',
                summary: "'kew-agent:0.9.0' is not present locally",
                hint: `from a kew checkout, run '${BUILD}'`,
                fix: BUILD,
                details: [],
                warnings: []
              }
            ]
          }
        })
    )
  ]);

/**
 * The live shape P21 was photographed on: lanes the console cannot launch and
 * whose credential is a placeholder, plus one with containment skipped. The
 * fixture's missing image is made present so nothing is counted, which is the
 * case the headline used to read "Nothing required is down." over.
 */
const withRedLanes = (page) =>
  page.route(
    (url) => url.pathname === '/api/health',
    async (route) => {
      const response = await route.fetch();
      const body = await response.json();
      body.images = body.images.map((i) => ({ ...i, present: true }));
      body.posture = body.posture.map((lane, n) => ({
        ...lane,
        credential_mode: n === 2 ? 'real' : 'placeholder',
        console_refusal: n === 2 ? null : 'credential_missing',
        dangerously_skip_containment: n === 2
      }));
      await route.fulfill({ response, json: body });
    }
  );

/**
 * Every visible text element in a section drawn in the danger token, with the
 * row it sits on; and the headline. A row is the section's direct child.
 */
function readRed() {
  const probe = document.createElement('span');
  probe.style.color = 'var(--danger)';
  document.body.append(probe);
  const danger = getComputedStyle(probe).color;
  probe.remove();
  const rows = new Map();
  for (const el of document.querySelectorAll('main section[id] *')) {
    const box = el.getBoundingClientRect();
    if (box.width === 0 || box.height === 0) continue;
    if (getComputedStyle(el).color !== danger) continue;
    const own = [...el.childNodes]
      .filter((n) => n.nodeType === Node.TEXT_NODE)
      .map((n) => n.textContent)
      .join(' ')
      .replace(/\s+/g, ' ')
      .trim();
    if (!own) continue;
    const row = el.closest('main section[id] > *');
    if (!rows.has(row)) rows.set(row, []);
    rows.get(row).push(own);
  }
  const headline = (document.querySelector('main header p.text-15')?.textContent ?? '')
    .replace(/\s+/g, ' ')
    .trim();
  return { headline, rows: [...rows.values()] };
}

export async function checks({ check, visit, expect, sleep, sizes: { WIDTH, HEIGHT } }) {
  for (const pressed of [false, true]) {
    await check(`the Health filter chip is one line at 390px (${pressed ? 'on' : 'off'})`, () =>
      visit(
        '/health',
        async (page) => {
          const chip = page.locator('main header button[aria-pressed]');
          await chip.waitFor({ state: 'visible', timeout: 15_000 });
          if (pressed) {
            await chip.click();
            await page.locator('main header button[aria-pressed="true"]').waitFor({ timeout: 5_000 });
          }
          const read = await chip.evaluate((el) => {
            // Line boxes, one client rect top each, so a clipped line cannot pass.
            const lines = (node) => {
              const range = document.createRange();
              range.selectNodeContents(node);
              return new Set([...range.getClientRects()].map((r) => Math.round(r.top))).size;
            };
            const age = el.parentElement.querySelector('span.text-text-3');
            return {
              text: el.textContent.trim(),
              lines: lines(el),
              chipBottom: el.getBoundingClientRect().bottom,
              ageText: age?.textContent.trim() ?? '',
              ageLines: age ? lines(age) : 0,
              ageTop: age?.getBoundingClientRect().top ?? 0
            };
          });
          expect(read.text !== '', 'the Health header drew no chip, so this proves nothing');
          expect(read.lines === 1, `"${read.text}" takes ${read.lines} lines at 390px`);
          expect(read.ageText !== '', 'the Health header drew no data age');
          expect(read.ageLines === 1, `"${read.ageText}" takes ${read.ageLines} lines at 390px`);
          expect(
            read.ageTop >= read.chipBottom,
            `the data age is not on its own line under the chip (${read.ageTop} < ${read.chipBottom})`
          );
        },
        PHONE
      )
    );
  }

  for (const size of [{ width: WIDTH, height: HEIGHT }, PHONE]) {
    for (const red of [false, true]) {
      const what = red ? 'red loops-only lanes' : 'the fixture';
      await check(
        `every red row on /health is named or counted by the headline, over ${what} (${size.width}px)`,
        () =>
          visit(
            '/health',
            async (page) => {
              await page.locator('#posture').waitFor({ state: 'visible', timeout: 15_000 });
              await sleep(300);
              const { headline, rows } = await page.evaluate(readRed);
              expect(headline !== '', 'the Health page drew no headline');
              const counted = Number(/^(\d+) things? needs? attention/.exec(headline)?.[1] ?? 0);
              const unnamed = rows.filter((words) => !words.every((w) => headline.includes(w)));
              // A counted fault is a row the band names and the headline counts;
              // any red row beyond that count is one the headline says nothing of.
              expect(
                unnamed.length <= counted,
                `"${headline}" counts ${counted} but ${unnamed.length} red row(s) are not named: ` +
                  unnamed.map((w) => `[${w.join(', ')}]`).join(' ')
              );
              if (red) {
                expect(rows.length >= 3, `only ${rows.length} red row(s) drew, so this proves nothing`);
                expect(counted === 0, `"${headline}" counts a fault the served payload does not have`);
              }
            },
            size,
            red ? withRedLanes : undefined
          )
      );
    }
  }

  for (const size of [PHONE, { width: 768, height: 1024 }, { width: WIDTH, height: HEIGHT }]) {
    await check(`a missing routed image prints its build command with Copy (${size.width}px)`, () =>
      visit(
        '/health',
        async (page) => {
          await page.locator('#images').waitFor({ state: 'visible', timeout: 15_000 });
          const block = page.locator('#images [data-image-repair] [data-command-block]');
          await block.first().waitFor({ state: 'visible', timeout: 10_000 });
          const read = await block.first().evaluate((el) => {
            const button = el.querySelector('button');
            const box = button?.getBoundingClientRect();
            return {
              text: (el.textContent ?? '').replace(/\s+/g, ' ').trim(),
              copy: (button?.textContent ?? '').trim(),
              copyHeight: box?.height ?? 0,
              right: el.getBoundingClientRect().right
            };
          });
          expect(read.text.includes(BUILD), `the image row's command block reads "${read.text}"`);
          expect(read.copy === 'Copy', `the command block's button reads "${read.copy}"`);
          expect(read.copyHeight >= 24, `the Copy button is ${read.copyHeight}px tall`);
          const words = (await page.locator('main').innerText()).toLowerCase();
          for (const jargon of ['none recorded', 'credential real', 'observed 7d']) {
            expect(!words.includes(jargon), `/health still reads "${jargon}"`);
          }
          expect(
            words.includes('2 with no runtime recorded'),
            'the runtimes note does not say the runs with no runtime in words'
          );
          expect(words.includes('uses a real credential'), 'the real-credential lane is not said in words');
          // In the text is not on the screen: the words sit wholly inside the
          // posture cell's box and the row's, so no truncate or clip cuts them.
          const credential = await page.evaluate(() =>
            [...document.querySelectorAll('[data-lane-credential]')].map((el) => {
              const box = el.getBoundingClientRect();
              const cell = el.closest('[data-lane-posture]').getBoundingClientRect();
              const row = el.closest('[data-lane-posture]').parentElement.parentElement.getBoundingClientRect();
              const inside = (outer) =>
                box.left >= outer.left - 0.5 &&
                box.right <= outer.right + 0.5 &&
                box.top >= outer.top - 0.5 &&
                box.bottom <= outer.bottom + 0.5;
              return {
                text: (el.textContent ?? '').trim(),
                width: box.width,
                inside: inside(cell) && inside(row) && box.right <= document.documentElement.clientWidth + 0.5
              };
            })
          );
          const real = credential.filter((c) => c.text === 'uses a real credential');
          expect(real.length > 0, 'no lane draws "uses a real credential" in its posture cell');
          for (const c of real) {
            expect(c.width > 0 && c.inside, `"${c.text}" is cut out of its posture cell at ${size.width}px`);
          }
          const where = await page.locator('#images [data-image-repair] [data-command-where]').first().innerText();
          expect(
            where.toLowerCase().includes('from a kew checkout'),
            `the build command does not say where to run it: "${where}"`
          );
          const box = await page.evaluate(() => ({
            scroll: document.documentElement.scrollWidth,
            client: document.documentElement.clientWidth
          }));
          expect(box.scroll <= box.client, `/health scrolls sideways at ${size.width}px`);
          expect(read.right <= box.client + 0.5, `the command block is cut at ${size.width}px`);
        },
        size,
        withMissingImage
      )
    );
  }
}
