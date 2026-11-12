/**
 * #3055 (C7-3) — Work keeps one list read open, however slow the server is.
 *
 * Work re-reads `/api/dashboard/work` on the shell's 7 s pulse. The answers
 * were ordered (`NewestReads`) but nothing stopped a new read while the last
 * was out, so a server slower than the pulse stacked one more open request
 * per pulse per tab. A pulse that finds a read out now skips its own.
 *
 * The page load's own list read counts: a server slow from the first read
 * must not see a pulse open a second beside it. So here EVERY list read is
 * held. The load's is held past three pulses, and no pulse may send one; then
 * it is answered, the next pulse's read is held past three pulses in turn, and
 * no pulse after it may send one either. The open requests are counted from
 * the network — opened when the route sees one, closed when the browser
 * reports it finished or failed — and must never exceed one. Last, every read
 * answers at once, and a later pulse must send a fresh read: a page that
 * skipped every re-read for good would pass the count and freeze the list.
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 */
// ui-check-owns: src/routes/(factory)/work/+page.svelte
// ui-check-owns: src/lib/work/reads.ts

/** The shell pulse's interval (`PULSE_INTERVAL_MS`). */
const PULSE_MS = 7_000;

/** How long the reads are held: past three pulses. */
const HOLD_MS = 3 * PULSE_MS + 3_000;

/** How long a released page has to send its next read: two pulses and slack. */
const NEXT_MS = 2 * PULSE_MS + 3_000;

const WORK = '**/api/dashboard/work';
const isWork = (request) => new URL(request.url()).pathname === '/api/dashboard/work';

export async function checks({ check, visit, expect, sleep, sizes: { WIDTH, HEIGHT } }) {
  for (const size of [
    { width: WIDTH, height: HEIGHT },
    { width: 390, height: 844 }
  ]) {
    let served;
    const at = `${size.width}px`;
    /** Wait until a list read past the `before`th is sent; whether one was. */
    const next = async (before) => {
      for (let waited = 0; served.sent() === before && waited < NEXT_MS; waited += 250) {
        await sleep(250);
      }
      return served.sent() > before;
    };
    const one = () =>
      expect(
        served.most() <= 1,
        `${at}: ${served.most()} list reads were open at once (${served.sent()} sent): ` +
          'a pulse stacked a read on one still out'
      );
    await check(
      `/work at ${at} keeps one list read open, the page load's included, while /api/dashboard/work is slower than three pulses`,
      () =>
        visit(
          '/work',
          async (page, problems) => {
            try {
              await sleep(HOLD_MS);
              expect(
                served.sent() === 1,
                `${at}: ${served.sent()} list reads were sent while the page load's was held: ` +
                  'a pulse read the list beside it'
              );
              one();
              await served.answer();
              expect(await next(1), `${at}: no pulse read the list once the page load's read answered`);
              await sleep(HOLD_MS);
              expect(
                served.sent() === 2,
                `${at}: ${served.sent()} list reads were sent while a pulse's was held`
              );
              one();
              await served.release();
              expect(await next(served.sent()), `${at}: no pulse read the list again once the held read answered`);
              one();
              expect(problems.length === 0, problems.join('\n'));
            } finally {
              await served.release();
            }
          },
          size,
          async (page) => {
            served = await serveSlow(page);
          }
        )
    );
  }
}

/**
 * Serve the list read from `page.route`, every one held: `answer()` answers
 * the reads held so far and keeps holding; `release()` answers them and every
 * one after at once. `most()` is the largest number open at any moment.
 */
async function serveSlow(page) {
  let sent = 0;
  let open = 0;
  let most = 0;
  let holding = true;
  const held = [];
  const closed = (request) => {
    if (isWork(request)) open = Math.max(0, open - 1);
  };
  page.on('requestfinished', closed);
  page.on('requestfailed', closed);
  await page.route(WORK, (route) => {
    sent += 1;
    open += 1;
    most = Math.max(most, open);
    if (!holding) return route.fallback();
    held.push(route);
    return undefined;
  });
  const answer = async () => {
    for (const route of held.splice(0)) await route.fallback().catch(() => {});
  };
  return {
    sent: () => sent,
    most: () => most,
    answer,
    async release() {
      holding = false;
      await answer();
    }
  };
}
