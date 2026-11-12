/**
 * A replay link round-trips through the address bar (#3142):
 * `?mode=replay&t=<seconds>&speed=<1|10|60>`.
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 *
 * The page writes those keys with shallow routing, which moves `page.state`
 * and the address bar but never `page.url`, and `replaceState` replaces the
 * whole state — so a writer that forgot one key dropped it (#2248, #2250,
 * #2267). Only a browser runs SvelteKit's own router, so this reads the
 * address bar a real reader would copy.
 *
 * The dev fixture's journal is one frame (with done nodes in it) and its
 * heartbeat counts no change, and `change_count` is what offers replay at
 * all. So the round trip is served a journal of three frames, that one frame
 * at +0s, +600s and +3600s, and a heartbeat that counts them: a moment past
 * the first is the only kind `page.state` and `page.url` can disagree on.
 */
// ui-check-owns: src/lib/epic/replay.ts
// ui-check-owns: src/routes/(factory)/epics/[ref]/**
// ui-check-owns: fixtures/factory/epics-launch-fixes*.json
import { EPIC } from '../ui-check-lib.mjs';

/** The fixture epic's own API path. */
const API = EPIC.replace('/epics/', '/api/epics/');

/** The node the clicks select: the one the dev fixtures serve a rail for. */
const NODE = '2055';

/** Seconds from the first frame of each frame `threeFrames` serves. */
const OFFSETS = [0, 600, 3600];

/** Serve the epic a three-frame journal and a heartbeat that counts it. */
const threeFrames = (base) => async (page) => {
  const [frame] = await (await page.request.get(`${base}${API}/ticks`)).json();
  const start = Date.now() - 2 * 3_600_000;
  const journal = OFFSETS.map((s, i) => {
    const tick_ts = new Date(start + s * 1000).toISOString();
    return { ...frame, id: frame.id + i, tick_ts, snapshot: { ...frame.snapshot, tick_ts } };
  });
  await page.route(
    (url) => url.pathname === `${API}/ticks`,
    (route) => {
      const after = Number(new URL(route.request().url()).searchParams.get('after') ?? 0);
      return route.fulfill({ json: journal.filter((f) => f.id > after) });
    }
  );
  await page.route(`**${API}`, async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    body.heartbeat = { ...body.heartbeat, change_count: journal.length };
    await route.fulfill({ response, json: body });
  });
};

/** The query the address bar holds, as a plain object. */
const query = (page) => Object.fromEntries(new URL(page.url()).searchParams);

export async function checks({ base, check, visit, expect, sleep }) {
  await check('a replay link round-trips mode, t and speed, and a node click keeps the moment on screen', async () => {
    await visit(
      `${EPIC}?mode=replay&t=0&speed=10`,
      async (page, problems) => {
        const scrubber = page.locator('input[aria-label="Replay frame"]');
        await scrubber.waitFor({ state: 'visible', timeout: 15_000 });
        await page.locator('.dag .svelte-flow__node').first().waitFor({ state: 'visible', timeout: 10_000 });
        let q = query(page);
        expect(q.mode === 'replay' && q.t === '0' && q.speed === '10', `the link came back as ?${new URLSearchParams(q)}`);
        const speeds = page.getByRole('group', { name: 'Replay speed' });
        const pressed = async (label) =>
          (await speeds.getByRole('button', { name: label, exact: true }).getAttribute('aria-pressed')) === 'true';
        expect(await pressed('10x'), 'the link asked for 10x and the control does not say so');

        await speeds.getByRole('button', { name: '60x', exact: true }).click();
        await sleep(200);
        q = query(page);
        expect(q.speed === '60' && q.t === '0', `60x wrote ?${new URLSearchParams(q)}`);
        expect(await pressed('60x'), 'the control did not move to 60x');

        // Seek past the first frame, so the moment on screen is one only
        // `page.state` holds: `page.url` still says t=0.
        await scrubber.fill('1');
        await sleep(200);
        q = query(page);
        expect(q.t === String(OFFSETS[1]), `the seek to frame 2 wrote ?${new URLSearchParams(q)}`);

        // A node click is the write that dropped keys it did not own. The
        // fixture's node, whose rail reads the dev server answers.
        const card = page.locator(`.dag .svelte-flow__node[data-id="${NODE}"]`);
        const id = await card.getAttribute('data-id');
        await card.click();
        await sleep(300);
        q = query(page);
        expect(q.node === id, `the click on ${id} wrote ?${new URLSearchParams(q)}`);
        expect(
          q.mode === 'replay' && q.t === String(OFFSETS[1]) && q.speed === '60',
          `the node click dropped the replay: ?${new URLSearchParams(q)}`
        );

        // ...and the URL it left opens the same replay.
        await page.reload({ waitUntil: 'load' });
        await scrubber.waitFor({ state: 'visible', timeout: 15_000 });
        await sleep(300);
        q = query(page);
        expect(
          q.mode === 'replay' && q.t === String(OFFSETS[1]) && q.speed === '60' && q.node === id,
          `the reload came back as ?${new URLSearchParams(q)}`
        );
        expect(await pressed('60x'), 'the reload lost the speed');
        expect((await scrubber.inputValue()) === '1', `the reload opened on frame ${Number(await scrubber.inputValue()) + 1}`);

        // Live and back: the press plays from the first frame. `page.url`
        // still names t=600 after the reload, and a page reading it there
        // reopened that moment paused instead.
        await page.getByRole('button', { name: 'live', exact: true }).click();
        await sleep(200);
        q = query(page);
        expect(!('mode' in q) && !('t' in q) && !('speed' in q), `live left ?${new URLSearchParams(q)}`);
        await page.getByRole('button', { name: 'replay', exact: true }).click();
        await scrubber.waitFor({ state: 'visible', timeout: 15_000 });
        await sleep(300);
        q = query(page);
        expect(q.mode === 'replay' && q.t === '0', `replay after live wrote ?${new URLSearchParams(q)}`);
        expect(
          await page.getByRole('button', { name: '⏸ pause' }).isVisible(),
          'replay after live opened paused, on the old link\'s moment'
        );
        expect(problems.length === 0, `the replay link reported:\n${problems.join('\n')}`);
      },
      undefined,
      threeFrames(base)
    );
  });

  // Design review c9 D13: a replaying rail said "not in the journal; replay
  // draws the graph alone" in dim mono under PIPELINE and again under
  // EVIDENCE, with a bare dash beside the first. It says it once, in sans,
  // in one cell across the two columns.
  await check('a replaying rail says once, in sans, why it has no pipeline or evidence', async () => {
    await visit(
      `${EPIC}?mode=replay&t=${OFFSETS[1]}&node=${NODE}`,
      async (page, problems) => {
        const rail = page.locator('section[aria-label^="Selected node"]');
        await rail.waitFor({ state: 'visible', timeout: 15_000 });
        const note = rail.locator('[data-rail-replay]');
        await note.waitFor({ state: 'visible', timeout: 10_000 });
        const read = await rail.evaluate((section) => {
          const text = (section.textContent ?? '').replace(/\s+/g, ' ');
          const p = section.querySelector('[data-rail-replay]');
          return {
            note: (p?.textContent ?? '').trim(),
            font: p ? getComputedStyle(p).fontFamily : '',
            times: text.split('not in the journal').length - 1,
            headings: [...section.querySelectorAll('span')]
              .map((s) => (s.textContent ?? '').trim())
              .filter((t) => /^(Pipeline|Evidence)\b/i.test(t))
          };
        });
        expect(
          read.note === "Replay draws the graph alone: this node's pipeline and evidence are not in the journal.",
          `the rail says "${read.note}"`
        );
        expect(!read.font.includes('Mono'), `the replay sentence is set in ${read.font}`);
        expect(read.times === 1, `the rail says "not in the journal" ${read.times} times`);
        expect(read.headings.length === 0, `the rail still heads ${JSON.stringify(read.headings)} under replay`);
        expect(problems.length === 0, `the page reported:\n${problems.join('\n')}`);
      },
      undefined,
      threeFrames(base)
    );
  });

  await check('a replay link to an epic with no journal says so, and a node click keeps the link', async () => {
    // The fixture as committed: its heartbeat counts no change.
    await visit(`${EPIC}?mode=replay&t=600`, async (page, problems) => {
      const sentence = page.getByText('This epic has no journal to replay yet.');
      await sentence.waitFor({ state: 'visible', timeout: 10_000 });
      const card = page.locator(`.dag .svelte-flow__node[data-id="${NODE}"]`);
      await card.waitFor({ state: 'visible', timeout: 10_000 });
      await card.click();
      await sleep(300);
      const q = query(page);
      expect(q.mode === 'replay' && q.t === '600', `the node click spent the link: ?${new URLSearchParams(q)}`);
      expect(await sentence.isVisible(), 'the sentence went away after the click');
      expect(problems.length === 0, `the page reported:\n${problems.join('\n')}`);
    });
  });
}
