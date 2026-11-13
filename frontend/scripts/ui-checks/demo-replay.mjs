/**
 * The demo epic's replay, opened by link (#3142): `?mode=replay&t=<seconds>`.
 *
 * A demo module (`demo-*.mjs`): `scripts/ui-check.mjs` runs it only against
 * the static server for `build-demo/`. Nothing is read at import
 * (`ui-check.test.ts` loads every module and calls `checks()` with `visit` set
 * to throw): the world's manifest and journal are read when `checks()` starts,
 * and every page visit happens inside `check()`.
 *
 * The replay is deterministic, so a moment is a reading: `t=0` is the epic
 * before anything merged, and the journal's last moment is the epic as its
 * static snapshot has it.
 */
// ui-check-owns: src/lib/epic/replay.ts
// ui-check-owns: src/lib/epic/EpicTimeline.svelte
import { readFileSync } from 'node:fs';
import { FRONTEND, widestBox } from '../ui-check-lib.mjs';

const readWorld = (name) => JSON.parse(readFileSync(new URL(`demo/world/${name}`, FRONTEND), 'utf8'));

/** Every card the graph draws, by id, with its status word; done groups read as their own id. */
async function drawn(page) {
  await page.locator('.dag .svelte-flow__node').first().waitFor({ state: 'visible', timeout: 10_000 });
  return page.locator('.dag .svelte-flow__node[data-id]').evaluateAll((cards) =>
    Object.fromEntries(
      cards.map((card) => [
        card.getAttribute('data-id'),
        (card.querySelector('[data-status-word]')?.textContent ?? '').trim().toLowerCase()
      ])
    )
  );
}

/** Wait for the replay a link asked for to draw its frame. */
async function replayDrawn(page) {
  await page.locator('input[aria-label="Replay frame"]').waitFor({ state: 'visible', timeout: 15_000 });
  return drawn(page);
}

export async function checks({ check, visit, expect }) {
  const { epic_ref: ref } = readWorld('manifest.json');
  const journal = readWorld(`epics-${ref}-ticks.json`);
  /** The journal's last moment, as `?t=` spells it: whole seconds since its first tick. */
  const lastT = Math.floor((Date.parse(journal.at(-1).tick_ts) - Date.parse(journal[0].tick_ts)) / 1000);

  await check(`demo: /epics/${ref}?mode=replay&t=0 shows no node done`, async () => {
    await visit(`/epics/${ref}?mode=replay&t=0`, async (page, problems) => {
      const cards = await replayDrawn(page);
      const ids = Object.keys(cards);
      expect(ids.length > 0, 'the replay drew no card, so this proves nothing');
      const done = ids.filter((id) => id.startsWith('done:') || cards[id] === 'done');
      expect(done.length === 0, `t=0 already draws done: ${done.join(', ')}`);
      expect(problems.length === 0, `the replay link reported:\n${problems.join('\n')}`);
    });
  });

  await check(`demo: the last t (${lastT}) draws the epic as its static snapshot has it`, async () => {
    const live = await visit(`/epics/${ref}`, drawn);
    const replayed = await visit(`/epics/${ref}?mode=replay&t=${lastT}`, replayDrawn);
    expect(Object.keys(live).length > 0, 'the static snapshot drew no card, so this proves nothing');
    const differ = [...new Set([...Object.keys(live), ...Object.keys(replayed)])].filter(
      (id) => live[id] !== replayed[id]
    );
    expect(
      differ.length === 0,
      `t=${lastT} disagrees with the snapshot on: ${differ.map((id) => `${id} (${live[id]} / ${replayed[id]})`).join(', ')}`
    );
  });

  await check('demo: the replay does not scroll sideways at 390px and its speed controls are 32px', async () => {
    await visit(
      `/epics/${ref}?mode=replay&t=0`,
      async (page) => {
        await replayDrawn(page);
        const wide = await widestBox(page);
        expect(wide.w <= wide.c, `390px scrolls sideways: ${wide.culprit}`);
        const speeds = page.getByRole('group', { name: 'Replay speed' }).getByRole('button');
        const count = await speeds.count();
        expect(count === 3, `the speed control has ${count} buttons`);
        for (let i = 0; i < count; i++) {
          const box = await speeds.nth(i).boundingBox();
          expect(box !== null && box.height >= 32, `speed button ${i + 1} is ${box?.height}px tall`);
        }
      },
      { width: 390, height: 844 }
    );
  });
}
