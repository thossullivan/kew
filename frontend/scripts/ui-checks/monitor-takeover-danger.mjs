/**
 * Monitor: an unconfirmed host takeover states the missing guarantee in red.
 *
 * The audit payload names the provider and the addressed instance. The event
 * stream must show that context beside its human label. Its raw event code is
 * evidence metadata, not interface copy.
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` discovers it.
 */
// ui-check-owns: src/lib/monitor/EventStream.svelte
// ui-check-owns: src/lib/monitor/stream.ts
// ui-check-owns: src/lib/factory/names.ts
// ui-check-owns: src/routes/(factory)/monitor/+page.*
// ui-check-owns: fixtures/factory/audit.json

const EVENT_ID = 2_874_001;
const EVENT_CODE = 'host_lease_takeover_unconfirmed';
const LABEL = 'Lease takeover not confirmed';
const PROVIDER = 'gcp';
const ADDRESS = 'agent-expired-1';
const PHONE = { width: 390, height: 844 };

/** Add one recent takeover without replacing the fixture's other evidence. */
const withUnconfirmedTakeover = (page) =>
  page.route(
    (url) => url.pathname === '/api/audit',
    async (route) => {
      const response = await route.fetch();
      const body = await response.json();
      const takeover = {
        id: EVENT_ID,
        timestamp: new Date(Date.now() - 60_000).toISOString(),
        event: EVENT_CODE,
        issue_num: null,
        item_id: null,
        run_id: null,
        actor_type: 'host',
        actor_id: 'kew',
        summary: JSON.stringify({
          host: PROVIDER,
          address: ADDRESS,
          resource: `${PROVIDER}/project/zone/${ADDRESS}`,
          holder: 'foreign-operator-17-unreadable',
          takeover_id: '2874'.padStart(32, '0'),
          confirmed_on_host: false
        }),
        epic_ref: null
      };
      body.events = [takeover, ...body.events.filter((event) => event.id !== EVENT_ID)];
      await route.fulfill({ response, json: body });
    }
  );

/**
 * Read the visible row and compare its mark with the page's danger token.
 * Runs in the page, so the event id comes in as its argument.
 */
function readTakeover(eventId) {
  const row = document.querySelector(`li[data-event-id="${eventId}"]`);
  if (!row) return null;
  const probe = document.createElement('span');
  probe.style.backgroundColor = 'var(--danger)';
  document.body.append(probe);
  const danger = getComputedStyle(probe).backgroundColor;
  probe.remove();
  const mark = row.querySelector('span.rounded-full');
  const box = mark?.getBoundingClientRect();
  return {
    text: row.innerText.replace(/\s+/g, ' ').trim(),
    danger,
    mark: mark ? getComputedStyle(mark).backgroundColor : '',
    markVisible: Boolean(box && box.width > 0 && box.height > 0)
  };
}

export async function checks({ check, visit, expect, sleep, sizes: { WIDTH, HEIGHT } }) {
  for (const size of [{ width: WIDTH, height: HEIGHT }, PHONE]) {
    await check(`an unconfirmed host takeover is readable and dangerous at ${size.width}px`, () =>
      visit(
        '/monitor',
        async (page) => {
          await page.locator(`li[data-event-id="${EVENT_ID}"]`).waitFor({ state: 'visible', timeout: 15_000 });
          await sleep(300);
          const read = await page.evaluate(readTakeover, EVENT_ID);
          expect(read !== null, 'the injected takeover row is missing');
          expect(read.text.includes(LABEL), `the row has no human label: "${read.text}"`);
          expect(read.text.includes(`${PROVIDER} · ${ADDRESS}`), `the row has no host context: "${read.text}"`);
          expect(!read.text.includes(EVENT_CODE), `the row shows its raw event code: "${read.text}"`);
          expect(read.markVisible, 'the takeover danger mark is not visible');
          expect(read.mark === read.danger, `the takeover mark is ${read.mark}, not danger ${read.danger}`);
        },
        size,
        withUnconfirmedTakeover
      )
    );
  }
}
