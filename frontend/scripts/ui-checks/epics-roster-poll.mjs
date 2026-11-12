/**
 * #2900 C3 — the Epics list's 5 s roster read, in a real browser: the tab
 * coming back reads once at once, and a beat while that read is out starts
 * nothing.
 *
 * The beat's phase is learned from the reads the visible page makes, and the
 * tab is shown a second after a beat, so the next beat is four seconds off: a
 * read in the first two seconds after the show is the show's own, which a page
 * without one never makes. That read is then held past the next beat; a page
 * that stacks reads asks again while it is held.
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 */
// ui-check-owns: src/routes/(factory)/epics/+page.svelte
// ui-check-owns: src/lib/epic/roster-poll.ts
import { setHidden } from './hidden-tab.mjs';

const PHONE = { width: 390, height: 844 };

/** The page's roster poll interval (`routes/(factory)/epics/+page.svelte`). */
const EVERY_MS = 5_000;

/** How long after a beat the tab is shown, and the window the show read must
 * start in: both well inside one interval. */
const AFTER_BEAT_MS = 1_000;
const SHOW_WINDOW_MS = 2_000;

/** How long the show read is held: past the next beat. */
const HOLD_MS = EVERY_MS + 2_000;

/**
 * Record every `/api/epics` request's start time, and hold one read once
 * `holdNext` is set. Made before navigation, so the load read counts too.
 */
async function watchRoster(page) {
  const log = { starts: [], holdNext: false, held: null };
  await page.route('**/api/epics', async (route) => {
    log.starts.push(Date.now());
    if (log.holdNext) {
      log.holdNext = false;
      log.held = new Promise((release) => setTimeout(release, HOLD_MS));
      await log.held;
    }
    return route.continue();
  });
  return log;
}

async function waitFor(predicate, sleep, ms = 15_000) {
  const until = Date.now() + ms;
  while (!predicate() && Date.now() < until) await sleep(100);
  return predicate();
}

async function showOnceRound(page, log, { sleep, expect }) {
  // Two reads seen on the page's own clock: the load's and at least one beat.
  const polled = await waitFor(() => log.starts.length >= 2, sleep, 2 * EVERY_MS + 3_000);
  expect(polled, `the Epics page made ${log.starts.length} roster reads while visible, not a poll`);

  await setHidden(page, true);
  const hiddenAt = log.starts.length;
  const lastBeat = log.starts.at(-1);
  let showAt = lastBeat + AFTER_BEAT_MS;
  while (showAt < Date.now() + 1_500) showAt += EVERY_MS;
  await sleep(showAt - Date.now());
  expect(
    log.starts.length === hiddenAt,
    `the hidden Epics tab read the roster ${log.starts.length - hiddenAt} time(s)`
  );

  log.holdNext = true;
  const shownAt = Date.now();
  await setHidden(page, false);
  await sleep(SHOW_WINDOW_MS);
  const early = log.starts.slice(hiddenAt).filter((t) => t - shownAt <= SHOW_WINDOW_MS);
  expect(
    early.length === 1,
    `showing the Epics tab started ${early.length} roster reads in ${SHOW_WINDOW_MS} ms, not one`
  );

  // The show read is held past the next beat: that beat must start nothing.
  await sleep(HOLD_MS - SHOW_WINDOW_MS - 500);
  const whileHeld = log.starts.length - hiddenAt;
  expect(
    whileHeld === 1,
    `a beat while the roster read was out started ${whileHeld - 1} more read(s)`
  );

  // Once the held read lands the poll resumes.
  await log.held;
  const resumed = await waitFor(() => log.starts.length - hiddenAt >= 2, sleep, EVERY_MS + 3_000);
  expect(resumed, 'the Epics poll did not read again after the held read landed');
}

export async function checks({ check, visit, expect, sleep, sizes }) {
  for (const size of [{ width: sizes.WIDTH, height: sizes.HEIGHT }, PHONE]) {
    await check(
      `Epics reads the roster once when the tab is shown, and not again while it is out (${size.width}px)`,
      async () => {
        let log;
        await visit(
          '/epics',
          (page) => showOnceRound(page, log, { sleep, expect }),
          size,
          async (page) => {
            log = await watchRoster(page);
          }
        );
      }
    );
  }
}
