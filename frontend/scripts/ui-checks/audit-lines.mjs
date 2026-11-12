/**
 * `/audit` rows read in words over the summaries the server really sends
 * (#3470 C7).
 *
 * `/api/audit` carries each event's payload as compact JSON capped at 200
 * characters (`_summary`, kew/src/kew/service/audit.py), and the page printed
 * it as it arrived: a row read `{"pr_number":2059,"item_id":"2055",…`. The
 * committed fixture carried prose the server never sends ("READY · PR #2059"),
 * so no check saw the JSON. The fixture is server-shaped now, and this serves
 * one more event whose payload the cap cut, the shape the scanner exists for.
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it.
 */
// ui-check-owns: src/routes/(factory)/audit/+page.svelte
// ui-check-owns: src/lib/audit-row.ts
// ui-check-owns: fixtures/factory/audit.json

const SIZES = [
  { width: 390, height: 844 },
  { width: 768, height: 1024 },
  { width: 1440, height: 900 }
];

/** A verdict payload as `_summary` serializes it, cut at 200 characters. */
const CUT = (() => {
  const text = JSON.stringify({
    pr_number: 2071,
    item_id: '2066',
    head_sha: 'b'.repeat(40),
    round: 2,
    verdict: 'NOT_READY',
    sidecar_digest: `sha256:${'c'.repeat(40)}`,
    reviewer_model: 'claude-opus-5-5',
    review_mode: 'full'
  });
  return text.length <= 200 ? text : `${text.slice(0, 199)}…`;
})();

const withCutSummary = (page) =>
  page.route(
    (url) => url.pathname === '/api/audit',
    async (route) => {
      const response = await route.fetch();
      const body = await response.json();
      const newest = body.events[0];
      body.events.unshift({
        ...newest,
        id: newest.id + 100,
        issue_num: 2066,
        event: 'adversarial_review_verdict',
        summary: CUT
      });
      await route.fulfill({ response, json: body });
    }
  );

export async function checks({ check, visit, expect, sleep }) {
  for (const size of SIZES) {
    await check(`/audit rows never lead with raw JSON at ${size.width}px`, () =>
      visit(
        '/audit',
        async (page) => {
          const rows = page.locator('[data-audit-row]');
          await rows.first().waitFor({ state: 'visible', timeout: 15_000 });
          await sleep(200);
          const read = await page.evaluate(() => {
            const visible = (el) => {
              const box = el.getBoundingClientRect();
              return box.width > 0 && box.height > 0;
            };
            const rows = [...document.querySelectorAll('[data-audit-row]')].map((row) => ({
              cells: [...row.children].map((c) => (c.innerText || '').replace(/\s+/g, ' ').trim()),
              text: (row.innerText || '').replace(/\s+/g, ' ').trim()
            }));
            const payloads = [...document.querySelectorAll('[data-audit-payload]')];
            return {
              rows,
              shownPayloads: payloads.filter(visible).length,
              payloads: payloads.length,
              chips: [...document.querySelectorAll('[data-audit-chip]')].map((el) => ({
                text: (el.innerText || '').trim(),
                code: el.getAttribute('title') ?? ''
              })),
              toggles: [...document.querySelectorAll('button[aria-controls^="audit-payload-"]')].map(
                (el) => el.getBoundingClientRect().height
              ),
              scroll: document.documentElement.scrollWidth,
              client: document.documentElement.clientWidth
            };
          });
          expect(read.rows.length >= 6, `only ${read.rows.length} audit rows drew`);
          for (const row of read.rows) {
            for (const cell of row.cells) {
              expect(!cell.startsWith('{'), `an /audit cell leads with JSON: "${cell}"`);
            }
            expect(!/\{"|":/.test(row.text), `an /audit row prints JSON: "${row.text}"`);
          }
          const cut = read.rows.find((r) => r.text.includes('PR #2071'));
          expect(cut !== undefined, 'the row whose payload the cap cut does not name its PR in words');
          expect(read.payloads > 0, 'no row offers its payload, so the disclosure proves nothing');
          expect(read.chips.length > 0, 'no event filter chip drew');
          for (const chip of read.chips) {
            expect(!chip.text.includes('_'), `an /audit filter chip prints its code: "${chip.text}"`);
            expect(
              chip.code !== '' && chip.code !== chip.text,
              `the "${chip.text}" chip carries no code in its title`
            );
          }
          if (size.width < 640) {
            const short = read.toggles.filter((h) => h < 32);
            expect(short.length === 0, `${short.length} payload toggle(s) under 32px: ${short.join(', ')}px`);
          }
          expect(read.shownPayloads === 0, `${read.shownPayloads} payload(s) show before they are asked for`);

          // The disclosure opens the JSON, verbatim, under its own row.
          const toggle = page.locator('[data-audit-row] button[aria-controls^="audit-payload-"]').first();
          await toggle.click();
          const id = await toggle.getAttribute('aria-controls');
          const pre = page.locator(`[id="${id}"]`);
          await pre.waitFor({ state: 'visible', timeout: 5_000 });
          const json = ((await pre.textContent()) ?? '').trim();
          expect(json.startsWith('{'), `the opened payload reads "${json.slice(0, 60)}"`);

          expect(read.scroll <= read.client, `/audit scrolls sideways at ${size.width}px`);
        },
        size,
        withCutSummary
      )
    );
  }

  // DESIGN.md: a control on a phone is at least 32px to the press, the linked
  // event's own `payload ▸` as much as a row's.
  await check('/audit linked event: its payload toggle is at least 32px at 390px', () =>
    visit(
      '/audit?event=105',
      async (page) => {
        const toggle = page.locator('[data-audit-focus] button[aria-controls="audit-focus-payload"]');
        await toggle.waitFor({ state: 'visible', timeout: 15_000 });
        const height = (await toggle.boundingBox())?.height ?? 0;
        expect(height >= 32, `the linked event's payload toggle is ${height}px tall`);
      },
      SIZES[0],
      withCutSummary
    )
  );
}
