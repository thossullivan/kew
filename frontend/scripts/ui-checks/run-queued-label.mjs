/**
 * The Run button while the drive holds a request queued (#2878).
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 */
// ui-check-owns: src/lib/RunButton.svelte
// ui-check-owns: src/lib/work/DispatchLaunchpad.svelte
// ui-check-owns: src/routes/(factory)/work/+page.*
import { SETTLE_MS, assertPainted } from '../ui-check-lib.mjs';

const HOST_LOAD = 'waiting: host load 98.0 on 8 CPU(s) (12.25 per CPU, above [drive] max_load_per_cpu 4)';

function request(state, detail, { command = 'kew dispatch 2060', item = '2060' } = {}) {
  return {
    id: 'ui-check-queued',
    command,
    operator: 'ui-check',
    requested_at: '2026-10-15T12:00:00+00:00',
    source: 'web',
    state,
    item_id: item,
    drive: null,
    host: null,
    pid: null,
    log: null,
    started_at: state === 'running' ? '2026-10-15T12:40:00+00:00' : null,
    finished_at: null,
    exit_code: null,
    detail,
    model: null
  };
}

/**
 * Bring `locator` into view by scrolling its page vertically only, then
 * `assertPainted` it where it lands. The helper itself never scrolls, so a
 * label laid out past the right edge cannot be scrolled back on screen.
 */
async function painted(page, locator, what) {
  await locator.evaluate((el) => {
    const r = el.getBoundingClientRect();
    const vh = document.documentElement.clientHeight;
    if (r.top < 0 || r.bottom > vh) window.scrollBy(0, r.top - (vh - r.height) / 2);
  });
  await assertPainted(page, locator, what);
}

/** Read `locator`'s label until it is `want` or six seconds pass. */
async function labelUntil(locator, want, sleep) {
  let got = '';
  for (let i = 0; i < 60; i++) {
    got = ((await locator.textContent()) ?? '').trim();
    if (got === want) break;
    await sleep(100);
  }
  return got;
}

/**
 * Whether the Run button's accessible name is `word` and the command — what a
 * screen reader hears, read through the accessibility tree, not the attribute.
 */
async function namedAs(panel, word) {
  const name = `${word} kew dispatch 2060`;
  return (await panel.getByRole('button', { name, exact: true }).count()) === 1;
}

/** The fill, text and border of `locator` and the `--accent` (needs-you) colour, as rgb. */
function paint(locator) {
  return locator.evaluate((el) => {
    const rgb = (color) => {
      const canvas = document.createElement('canvas');
      canvas.width = canvas.height = 1;
      const g = canvas.getContext('2d');
      g.fillStyle = color;
      g.fillRect(0, 0, 1, 1);
      const [r, gr, b, a] = g.getImageData(0, 0, 1, 1).data;
      return { r, g: gr, b, a, css: color };
    };
    const probe = document.createElement('span');
    probe.style.color = 'var(--accent)';
    document.body.append(probe);
    const magenta = rgb(getComputedStyle(probe).color);
    probe.remove();
    const style = getComputedStyle(el);
    return {
      magenta,
      parts: {
        fill: rgb(style.backgroundColor),
        text: rgb(style.color),
        border: rgb(style.borderTopColor)
      }
    };
  });
}

/** True when `c` is painted (any alpha) within a hair of `m`. */
const isMagenta = (c, m) =>
  c.a > 0 && Math.max(Math.abs(c.r - m.r), Math.abs(c.g - m.g), Math.abs(c.b - m.b)) <= 12;

export async function checks({ browser, base, check, expect, sleep, sizes: { WIDTH, HEIGHT } }) {
  /**
   * #2878 — a request the drive holds queued for host load can wait up to an
   * hour. The launchpad's Run button must read "Queued…" (not "Running…") and
   * the line under it must carry the drive's waiting detail; once the drive
   * starts the request, the follow turns the button to "Running…". Served by
   * route, so both the reload pickup and the follow's re-read are real.
   */
  for (const width of [390, WIDTH]) {
    await check(`a host-held queued request reads Queued… with its wait at ${width}px`, async () => {
      const ctx = await browser.newContext({ viewport: { width, height: width === WIDTH ? HEIGHT : 844 } });
      const page = await ctx.newPage();
      try {
        let current = request('queued', HOST_LOAD);
        await page.route('**/api/run-requests**', (route) => {
          if (route.request().method() !== 'GET') return route.fallback();
          const url = new URL(route.request().url());
          const body = url.pathname.endsWith('/run-requests') ? { requests: [current] } : current;
          return route.fulfill({ json: body });
        });

        await page.goto(`${base}/work?item=2060`, { waitUntil: 'load' });
        const panel = page.locator('section[aria-label="Plan a dispatch"]');
        await panel.waitFor({ state: 'visible', timeout: 15_000 });
        await sleep(SETTLE_MS);

        const button = panel.locator('button[data-run-button]');
        const status = panel.locator('[data-run-line]');
        await status.waitFor({ state: 'visible', timeout: 15_000 });
        const label = ((await button.textContent()) ?? '').trim();
        expect(label === 'Queued…', `${width}px: a queued request labels Run "${label}", not "Queued…"`);
        expect(await namedAs(panel, 'Queued'), `${width}px: a queued Run is not named "Queued kew dispatch 2060"`);
        const said = ((await status.textContent()) ?? '').trim();
        expect(
          said.includes('host load 98.0 on 8 CPU(s)'),
          `${width}px: the line under a host-held request reads "${said}", not the waiting detail`
        );
        await painted(page, button, `${width}px: the Queued… label`);
        await painted(page, status, `${width}px: the waiting detail`);

        // The control: the same label made transparent must fail the helper,
        // or a pass above proves nothing.
        await button.evaluate((el) => {
          el.dataset.uiCheckColour = el.style.color;
          el.style.color = 'transparent';
        });
        let caught = '';
        try {
          await painted(page, button, 'the transparent copy');
        } catch (error) {
          caught = String(error.message);
        }
        expect(
          caught.includes('colour'),
          `${width}px: a transparent Queued… label passed assertPainted (${caught || 'no error'})`
        );
        await button.evaluate((el) => {
          el.style.color = el.dataset.uiCheckColour ?? '';
        });

        // The contrast control: text in the colour of its own ground is
        // opaque, unclipped and on top, and must still fail.
        await button.evaluate((el) => {
          el.dataset.uiCheckStyle = el.getAttribute('style') ?? '';
          el.style.background = '#2a2240';
          el.style.color = '#2a2240';
        });
        let flat = '';
        try {
          await painted(page, button, 'the copy in its ground colour');
        } catch (error) {
          flat = String(error.message);
        }
        expect(
          flat.includes('contrasts'),
          `${width}px: a Queued… label in its own ground colour passed assertPainted (${flat || 'no error'})`
        );
        await button.evaluate((el) => {
          el.setAttribute('style', el.dataset.uiCheckStyle ?? '');
        });
        await painted(page, button, `${width}px: the Queued… label, restored`);

        // The self-clip control: a button narrower than its own label, with
        // `overflow: hidden`, cuts the words it carries and must fail.
        await button.evaluate((el) => {
          el.dataset.uiCheckStyle = el.getAttribute('style') ?? '';
          el.style.width = '24px';
          el.style.minWidth = '0';
          el.style.overflow = 'hidden';
          el.style.whiteSpace = 'nowrap';
        });
        let cut = '';
        try {
          await painted(page, button, 'the copy narrower than its label');
        } catch (error) {
          cut = String(error.message);
        }
        expect(
          cut.includes('clips its text'),
          `${width}px: a Queued… button narrower than its label passed assertPainted (${cut || 'no error'})`
        );
        await button.evaluate((el) => {
          el.setAttribute('style', el.dataset.uiCheckStyle ?? '');
        });
        await painted(page, button, `${width}px: the Queued… label, restored again`);

        // The occlusion control: a busy Run is disabled, and a disabled control
        // takes no pointer events, which the hit test must see past — but not
        // past a real sibling laid over the label, which must still fail.
        const inert = await button.evaluate((el) => getComputedStyle(el).pointerEvents);
        expect(inert === 'none', `${width}px: a queued Run takes pointer events (${inert}); the control is moot`);
        await button.evaluate((el) => {
          const r = el.getBoundingClientRect();
          const cover = document.createElement('span');
          cover.id = 'ui-check-cover';
          cover.textContent = 'over';
          cover.style.cssText =
            `position:fixed;left:${r.left}px;top:${r.top}px;width:${r.width}px;height:${r.height}px;` +
            'background:#000;z-index:9999';
          document.body.append(cover);
        });
        let covered = '';
        try {
          await painted(page, button, 'the label under a sibling');
        } catch (error) {
          covered = String(error.message);
        }
        expect(
          covered.includes('<span> is on top'),
          `${width}px: a Queued… label under an overlapping sibling passed assertPainted (${covered || 'no error'})`
        );
        await page.evaluate(() => document.getElementById('ui-check-cover')?.remove());
        await painted(page, button, `${width}px: the Queued… label, uncovered`);

        // The second control: a label laid out past the right edge fails, and
        // the helper does not scroll it back on screen to pass it.
        if (width === 390) {
          await page.evaluate(() => {
            const stray = document.createElement('span');
            stray.id = 'ui-check-stray';
            stray.textContent = 'Queued…';
            stray.style.cssText = 'position:absolute;top:120px;left:430px;color:#fff;z-index:9999';
            document.body.append(stray);
          });
          const scrolled = () => page.evaluate(() => [window.scrollX, window.scrollY]);
          const at = await scrolled();
          let off = '';
          try {
            await assertPainted(page, page.locator('#ui-check-stray'), 'the label at x=430');
          } catch (error) {
            off = String(error.message);
          }
          expect(off !== '', '390px: a label at x=430 on a 390px page passed assertPainted');
          const still = await scrolled();
          expect(
            still[0] === at[0] && still[1] === at[1],
            `390px: assertPainted scrolled the page from ${at} to ${still}`
          );
          await page.evaluate(() => document.getElementById('ui-check-stray')?.remove());
        }

        current = request('running', null);
        let running = '';
        for (let i = 0; i < 60 && running !== 'Running…'; i++) {
          await sleep(100);
          running = ((await button.textContent()) ?? '').trim();
        }
        expect(
          running === 'Running…',
          `${width}px: once the drive starts the request, Run reads "${running}", not "Running…"`
        );
        expect(await namedAs(panel, 'Running'), `${width}px: a running Run is not named "Running kew dispatch 2060"`);
        await painted(page, button, `${width}px: the Running… label`);
      } finally {
        await ctx.close();
      }
    });
  }

  /**
   * #2878 — between the press and the POST's answer nothing is in the ledger,
   * so the button must not claim a queued request. A POST held open reads
   * "Sending…"; a refused POST prints its error under the command and gives
   * the button back; an accepted one the drive holds queued reads "Queued…".
   */
  for (const width of [390, WIDTH]) {
    await check(`Run reads Sending… until the POST answers at ${width}px`, async () => {
      const ctx = await browser.newContext({ viewport: { width, height: width === WIDTH ? HEIGHT : 844 } });
      const page = await ctx.newPage();
      try {
        let current = null;
        /** Settles the open POST: the route waits on it, so the POST stays open. */
        let answer = null;
        await page.route('**/api/run-requests**', async (route) => {
          if (route.request().method() === 'POST') {
            const reply = await new Promise((resolve) => (answer = resolve));
            return route.fulfill(reply);
          }
          const url = new URL(route.request().url());
          if (url.pathname.endsWith('/run-requests')) {
            return route.fulfill({ json: { requests: current ? [current] : [] } });
          }
          return route.fulfill({ json: current });
        });

        await page.goto(`${base}/work?item=2060`, { waitUntil: 'load' });
        const panel = page.locator('section[aria-label="Plan a dispatch"]');
        await panel.waitFor({ state: 'visible', timeout: 15_000 });
        await sleep(SETTLE_MS);
        const button = panel.locator('button[data-run-button]');
        const status = panel.locator('[data-run-line]');
        const until = async (want) => {
          let got = '';
          for (let i = 0; i < 60; i++) {
            got = ((await button.textContent()) ?? '').trim();
            if (got === want) break;
            await sleep(100);
          }
          return got;
        };
        const posted = async () => {
          for (let i = 0; i < 50 && answer === null; i++) await sleep(100);
          return answer !== null;
        };

        await button.click();
        const sending = await until('Sending…');
        expect(sending === 'Sending…', `${width}px: with the POST still open, Run reads "${sending}", not "Sending…"`);
        expect(await namedAs(panel, 'Sending'), `${width}px: a sending Run is not named "Sending kew dispatch 2060"`);
        await painted(page, button, `${width}px: the Sending… label`);

        expect(await posted(), `${width}px: pressing Run sent no POST`);
        answer({ status: 409, json: { detail: 'no kew drive is running' } });
        answer = null;
        const idle = await until('Run');
        expect(idle === 'Run', `${width}px: after a refused POST, the button reads "${idle}", not "Run"`);
        expect(await button.isEnabled(), `${width}px: after a refused POST, the Run button stays disabled`);
        expect(await namedAs(panel, 'Run'), `${width}px: an idle Run is not named "Run kew dispatch 2060"`);
        const said = ((await status.textContent()) ?? '').trim();
        expect(
          said.includes('no kew drive is running'),
          `${width}px: after a refused POST, the line under the command reads "${said}", not the error`
        );
        await painted(page, status, `${width}px: the refused POST's error`);

        await button.click();
        expect(await posted(), `${width}px: a second press sent no POST`);
        current = request('queued', HOST_LOAD);
        answer({ json: { request: current, created: true, drive: { state: 'running', running: true, reason: null } } });
        const queued = await until('Queued…');
        expect(queued === 'Queued…', `${width}px: an accepted, queued request reads "${queued}", not "Queued…"`);
        expect(
          await namedAs(panel, 'Queued'),
          `${width}px: an accepted, queued Run is not named "Queued kew dispatch 2060"`
        );
        await painted(page, button, `${width}px: the Queued… label after a POST`);
      } finally {
        await ctx.close();
      }
    });
  }

  /**
   * #2878, #2975 — the mount reads the newest request for the command, and
   * that read can land late. Until it answers the console cannot know no
   * request of this pair is already queued, so Run stays disabled (issue
   * #2975, design item 3). A queued row it finds is followed — Run reads
   * "Queued…" and is never pressable; a done row gives Run back, and a press
   * then sends one POST and follows the request it queues.
   */
  for (const width of [390, WIDTH]) {
    for (const prior of ['queued', 'done']) {
      await check(`Run waits for a late mount read of a ${prior} row at ${width}px`, async () => {
        const ctx = await browser.newContext({ viewport: { width, height: width === WIDTH ? HEIGHT : 844 } });
        const page = await ctx.newPage();
        try {
          const stale = { ...request(prior, prior === 'done' ? null : HOST_LOAD), id: 'ui-check-prior' };
          let current = null;
          /** Release the held mount read, and answer the held POST. */
          let release = null;
          let answer = null;
          let reads = 0;
          let posts = 0;
          await page.route('**/api/run-requests**', async (route) => {
            if (route.request().method() === 'POST') {
              posts += 1;
              const reply = await new Promise((resolve) => (answer = resolve));
              return route.fulfill(reply);
            }
            const url = new URL(route.request().url());
            if (url.pathname.endsWith('/run-requests')) {
              reads += 1;
              if (reads === 1) {
                await new Promise((resolve) => (release = resolve));
                return route.fulfill({ json: { requests: [stale] } });
              }
              return route.fulfill({ json: { requests: current ? [current] : [stale] } });
            }
            return route.fulfill({ json: url.pathname.endsWith(stale.id) ? stale : current });
          });

          await page.goto(`${base}/work?item=2060`, { waitUntil: 'load' });
          const panel = page.locator('section[aria-label="Plan a dispatch"]');
          await panel.waitFor({ state: 'visible', timeout: 15_000 });
          const button = panel.locator('button[data-run-button]');
          await button.waitFor({ state: 'visible', timeout: 15_000 });
          for (let i = 0; i < 50 && release === null; i++) await sleep(100);
          expect(release !== null, `${width}px: the mount never read the latest request`);

          await sleep(SETTLE_MS);
          expect(!(await button.isEnabled()), `${width}px: Run is pressable while the mount's ledger read is held`);
          await button.click({ force: true }).catch(() => {});
          await sleep(SETTLE_MS);
          expect(posts === 0, `${width}px: a press during the held mount read sent ${posts} POSTs`);

          release();
          if (prior === 'queued') {
            const queued = await labelUntil(button, 'Queued…', sleep);
            expect(queued === 'Queued…', `${width}px: a late queued mount read labels Run "${queued}", not "Queued…"`);
            expect(!(await button.isEnabled()), `${width}px: a late queued mount read gave Run back`);
            const said = ((await panel.locator('[data-run-line]').textContent()) ?? '').trim();
            expect(said.includes('host load 98.0'), `${width}px: after the read the line reads "${said}", not the wait`);
            await painted(page, button, `${width}px: the Queued… label after a late mount read`);
            await button.click({ force: true }).catch(() => {});
            await sleep(SETTLE_MS);
            expect(posts === 0, `${width}px: a press on the found queued request sent ${posts} POSTs`);
            return;
          }

          const idle = await labelUntil(button, 'Run', sleep);
          expect(idle === 'Run', `${width}px: a late done mount read labels Run "${idle}", not "Run"`);
          for (let i = 0; i < 50 && !(await button.isEnabled()); i++) await sleep(100);
          expect(await button.isEnabled(), `${width}px: a late done mount read left Run disabled`);
          await painted(page, button, `${width}px: Run after a late done mount read`);

          await button.click();
          for (let i = 0; i < 50 && answer === null; i++) await sleep(100);
          expect(posts === 1, `${width}px: pressing Run sent ${posts} POSTs, not one`);
          const sending = await labelUntil(button, 'Sending…', sleep);
          expect(sending === 'Sending…', `${width}px: an open POST labels Run "${sending}", not "Sending…"`);
          current = request('queued', HOST_LOAD);
          answer({ json: { request: current, created: true, drive: { state: 'running', running: true, reason: null } } });
          const queued = await labelUntil(button, 'Queued…', sleep);
          expect(queued === 'Queued…', `${width}px: the confirmed POST reads "${queued}", not "Queued…"`);
          const said = ((await panel.locator('[data-run-line]').textContent()) ?? '').trim();
          expect(
            said.includes('host load 98.0'),
            `${width}px: after the POST the line reads "${said}", not the new request's wait`
          );
        } finally {
          await ctx.close();
        }
      });
    }
  }

  /**
   * #2878 — magenta is a person's next move (DESIGN.md), and a queued request
   * is the drive's. A primary Run is magenta while it can be pressed and
   * neutral while its request is queued, in both themes. The idle reading is
   * the control: it proves this is the magenta button at all. Since #3467
   * (D29) the launchpad's Run is never magenta, so the primary Run read here is
   * the one on SFD-229's needs-you block, whose unhold the drive may run.
   */
  const UNHOLD = { command: 'kew epic unhold go-live-2026-09 SFD-229', item: 'SFD-229' };
  for (const theme of ['dark', 'light']) {
    for (const width of [390, WIDTH]) {
      await check(`a queued primary Run is not the needs-you colour in the ${theme} theme at ${width}px`, async () => {
        const ctx = await browser.newContext({ viewport: { width, height: width === WIDTH ? HEIGHT : 844 } });
        await ctx.addInitScript((t) => localStorage.setItem('kew-theme', t), theme);
        const page = await ctx.newPage();
        try {
          let current = request('queued', HOST_LOAD, UNHOLD);
          await page.route('**/api/run-requests**', (route) => {
            if (route.request().method() !== 'GET') return route.fallback();
            const url = new URL(route.request().url());
            return route.fulfill({ json: url.pathname.endsWith('/run-requests') ? { requests: [current] } : current });
          });
          await page.goto(`${base}/work?item=${UNHOLD.item}`, { waitUntil: 'load' });
          const panel = page.locator('main section[aria-label^="Needs you"]').first();
          await panel.waitFor({ state: 'visible', timeout: 15_000 });
          const button = panel.locator('button[data-run-button]');
          const shown = await page.evaluate(() => document.documentElement.dataset.theme);
          expect(shown === theme, `the page is in the ${shown} theme, not ${theme}`);

          const queued = await labelUntil(button, 'Queued…', sleep);
          expect(queued === 'Queued…', `${theme} ${width}px: a queued request labels Run "${queued}"`);
          // The fill animates (`transition-[background,…]`): read it settled.
          await sleep(SETTLE_MS);
          await painted(page, button, `${theme} ${width}px: the Queued… label`);
          const status = panel.locator('[data-run-line]');
          await status.waitFor({ state: 'visible', timeout: 15_000 });
          const said = ((await status.textContent()) ?? '').trim();
          expect(
            said.includes('host load 98.0 on 8 CPU(s)'),
            `${theme} ${width}px: the line under a host-held request reads "${said}", not the waiting detail`
          );
          await painted(page, status, `${theme} ${width}px: the waiting detail`);
          const waiting = await paint(button);
          for (const [part, colour] of Object.entries(waiting.parts)) {
            expect(
              !isMagenta(colour, waiting.magenta),
              `${theme} ${width}px: a queued Run's ${part} is ${colour.css}, the needs-you colour ${waiting.magenta.css}`
            );
          }

          current = request('done', null, UNHOLD);
          const idle = await labelUntil(button, 'Run', sleep);
          expect(idle === 'Run', `${theme} ${width}px: a settled request labels Run "${idle}"`);
          await sleep(SETTLE_MS);
          await painted(page, button, `${theme} ${width}px: the Run label`);
          const ready = await paint(button);
          expect(
            isMagenta(ready.parts.fill, ready.magenta),
            `${theme} ${width}px: the actionable primary Run's fill is ${ready.parts.fill.css}, ` +
              `not the needs-you colour ${ready.magenta.css} — the control proves nothing`
          );
        } finally {
          await ctx.close();
        }
      });
    }
  }

  /**
   * #2878 — the drive holds one active request per (command, model), so a
   * request queued on one model says nothing about another's Run. With
   * `kew dispatch 2060` queued on Sol, picking Luna gives back an enabled
   * "Run"; picking Sol again finds its request and reads "Queued…".
   */
  for (const width of [390, WIDTH]) {
    await check(`a request queued on one model leaves another model's Run free at ${width}px`, async () => {
      const ctx = await browser.newContext({ viewport: { width, height: width === WIDTH ? HEIGHT : 844 } });
      const page = await ctx.newPage();
      try {
        const sol = { ...request('queued', HOST_LOAD), id: 'ui-check-sol', model: 'gpt-5.6-sol' };
        await page.route('**/api/run-requests**', (route) => {
          if (route.request().method() !== 'GET') return route.fallback();
          const url = new URL(route.request().url());
          return route.fulfill({ json: url.pathname.endsWith('/run-requests') ? { requests: [sol] } : sol });
        });

        await page.goto(`${base}/work?item=2060`, { waitUntil: 'load' });
        const panel = page.locator('section[aria-label="Plan a dispatch"]');
        await panel.waitFor({ state: 'visible', timeout: 15_000 });
        await sleep(SETTLE_MS);
        const button = panel.locator('button[data-run-button]');

        /** Pick `id` in the drawer's model listbox, then close the drawer. */
        const pick = async (id) => {
          await panel.getByRole('button', { name: 'Details' }).click();
          const drawer = page.getByRole('dialog', { name: /Plan a dispatch/ });
          const listbox = drawer.locator('section[aria-label="Model for this dispatch"]').getByRole('listbox');
          await listbox.waitFor({ state: 'visible', timeout: 15_000 });
          const option = listbox.locator(`[role="option"][data-model-id="${id}"]`);
          await option.scrollIntoViewIfNeeded();
          await option.click();
          await drawer.getByRole('button', { name: 'Close' }).click();
          await drawer.waitFor({ state: 'hidden', timeout: 15_000 });
        };

        await pick('gpt-5.6-sol');
        const onSol = await labelUntil(button, 'Queued…', sleep);
        expect(onSol === 'Queued…', `${width}px: with Sol's request queued, Sol's Run reads "${onSol}"`);

        await pick('gpt-5.6-luna');
        const onLuna = await labelUntil(button, 'Run', sleep);
        expect(onLuna === 'Run', `${width}px: Sol's queued request labels Luna's Run "${onLuna}", not "Run"`);
        expect(await button.isEnabled(), `${width}px: Sol's queued request holds Luna's Run disabled`);
        await painted(page, button, `${width}px: Luna's Run label`);

        await pick('gpt-5.6-sol');
        const back = await labelUntil(button, 'Queued…', sleep);
        expect(back === 'Queued…', `${width}px: back on Sol, Run reads "${back}", not "Queued…"`);
        await painted(page, button, `${width}px: Sol's Queued… label`);
      } finally {
        await ctx.close();
      }
    });
  }

  /**
   * #2878 — the list API returns at most 20 rows. Sol's queued request behind
   * more than a page of newer requests on other models must still read
   * "Queued…": the route answers as the server does, filtering by the
   * (command, model) the button asks for before it cuts the page.
   */
  for (const width of [390, WIDTH]) {
    await check(`a model's request behind a full page of others still reads Queued… at ${width}px`, async () => {
      const ctx = await browser.newContext({ viewport: { width, height: width === WIDTH ? HEIGHT : 844 } });
      const page = await ctx.newPage();
      try {
        const sol = { ...request('queued', HOST_LOAD), id: 'ui-check-sol', model: 'gpt-5.6-sol' };
        const newer = Array.from({ length: 25 }, (_, n) => ({
          ...request('queued', HOST_LOAD),
          id: `ui-check-other-${n}`,
          model: `other-model-${n}`
        }));
        const ledger = [...newer, sol];
        await page.route('**/api/run-requests**', (route) => {
          if (route.request().method() !== 'GET') return route.fallback();
          const url = new URL(route.request().url());
          if (!url.pathname.endsWith('/run-requests')) return route.fulfill({ json: sol });
          const model = url.searchParams.get('model');
          const rows = ledger.filter((r) => model === null || (r.model ?? '') === model);
          return route.fulfill({ json: { requests: rows.slice(0, 20) } });
        });

        await page.goto(`${base}/work?item=2060`, { waitUntil: 'load' });
        const panel = page.locator('section[aria-label="Plan a dispatch"]');
        await panel.waitFor({ state: 'visible', timeout: 15_000 });
        await sleep(SETTLE_MS);
        const button = panel.locator('button[data-run-button]');

        await panel.getByRole('button', { name: 'Details' }).click();
        const drawer = page.getByRole('dialog', { name: /Plan a dispatch/ });
        const listbox = drawer.locator('section[aria-label="Model for this dispatch"]').getByRole('listbox');
        await listbox.waitFor({ state: 'visible', timeout: 15_000 });
        const option = listbox.locator('[role="option"][data-model-id="gpt-5.6-sol"]');
        await option.scrollIntoViewIfNeeded();
        await option.click();
        await drawer.getByRole('button', { name: 'Close' }).click();
        await drawer.waitFor({ state: 'hidden', timeout: 15_000 });

        const onSol = await labelUntil(button, 'Queued…', sleep);
        expect(
          onSol === 'Queued…',
          `${width}px: Sol's request behind ${newer.length} newer rows labels Sol's Run "${onSol}", not "Queued…"`
        );
        expect(!(await button.isEnabled()), `${width}px: Sol's request behind a full page leaves Run enabled`);
        await painted(page, button, `${width}px: Sol's Queued… label behind a full page`);
      } finally {
        await ctx.close();
      }
    });
  }

  /**
   * #2878 — a 2xx is not proof of a request. An answer of `{}` once reached
   * the follow as `undefined` and left Run locked at "Sending…": it lands in
   * the normal error state, with the reason under the command and Run usable,
   * once the ledger answers that the POST queued nothing (#2975).
   */
  for (const width of [390, WIDTH]) {
    await check(`a 2xx naming no request gives Run back at ${width}px`, async () => {
      const ctx = await browser.newContext({ viewport: { width, height: width === WIDTH ? HEIGHT : 844 } });
      const page = await ctx.newPage();
      try {
        let posts = 0;
        await page.route('**/api/run-requests**', (route) => {
          if (route.request().method() === 'POST') {
            posts += 1;
            return route.fulfill({ status: 200, json: {} });
          }
          return route.fulfill({ json: { requests: [] } });
        });

        await page.goto(`${base}/work?item=2060`, { waitUntil: 'load' });
        const panel = page.locator('section[aria-label="Plan a dispatch"]');
        await panel.waitFor({ state: 'visible', timeout: 15_000 });
        await sleep(SETTLE_MS);
        const button = panel.locator('button[data-run-button]');
        const status = panel.locator('[data-run-line]');

        await button.click();
        for (let i = 0; i < 50 && posts === 0; i++) await sleep(100);
        expect(posts === 1, `${width}px: pressing Run sent ${posts} POSTs, not one`);
        const idle = await labelUntil(button, 'Run', sleep);
        expect(idle === 'Run', `${width}px: after a 200 of {}, the button reads "${idle}", not "Run"`);
        expect(await button.isEnabled(), `${width}px: after a 200 of {}, Run stays disabled`);
        const said = ((await status.textContent()) ?? '').trim();
        expect(
          said.includes('named no request'),
          `${width}px: after a 200 of {}, the line under the command reads "${said}", not the error`
        );
        await painted(page, status, `${width}px: the error for a 200 of {}`);
        await painted(page, button, `${width}px: Run after a 200 of {}`);
      } finally {
        await ctx.close();
      }
    });
  }

  /**
   * #2878 — a new model drops the button's reading, and a POST still open
   * would then queue (and run) Sol with nothing on screen saying so. So the
   * picker is held while the POST is open: a pick on Luna changes nothing and
   * Run stays "Sending…"; once the POST answers the picker is free again and
   * the label is the submitted model's.
   */
  for (const width of [390, WIDTH]) {
    await check(`the model picker is held while Run is sending at ${width}px`, async () => {
      const ctx = await browser.newContext({ viewport: { width, height: width === WIDTH ? HEIGHT : 844 } });
      const page = await ctx.newPage();
      try {
        let current = null;
        let answer = null;
        let sentModel = null;
        await page.route('**/api/run-requests**', async (route) => {
          if (route.request().method() === 'POST') {
            sentModel = JSON.parse(route.request().postData() ?? '{}').model ?? null;
            const reply = await new Promise((resolve) => (answer = resolve));
            return route.fulfill(reply);
          }
          const url = new URL(route.request().url());
          if (url.pathname.endsWith('/run-requests')) {
            return route.fulfill({ json: { requests: current ? [current] : [] } });
          }
          return route.fulfill({ json: current });
        });

        await page.goto(`${base}/work?item=2060`, { waitUntil: 'load' });
        const panel = page.locator('section[aria-label="Plan a dispatch"]');
        await panel.waitFor({ state: 'visible', timeout: 15_000 });
        await sleep(SETTLE_MS);
        const button = panel.locator('button[data-run-button]');
        const drawer = page.getByRole('dialog', { name: /Plan a dispatch/ });
        const listbox = drawer.locator('section[aria-label="Model for this dispatch"]').getByRole('listbox');
        const option = (id) => listbox.locator(`[role="option"][data-model-id="${id}"]`);
        const openDrawer = async () => {
          await panel.getByRole('button', { name: 'Details' }).click();
          await listbox.waitFor({ state: 'visible', timeout: 15_000 });
        };
        const closeDrawer = async () => {
          await drawer.getByRole('button', { name: 'Close' }).click();
          await drawer.waitFor({ state: 'hidden', timeout: 15_000 });
        };

        await openDrawer();
        await option('gpt-5.6-sol').scrollIntoViewIfNeeded();
        await option('gpt-5.6-sol').click();
        await closeDrawer();

        await button.click();
        for (let i = 0; i < 50 && answer === null; i++) await sleep(100);
        expect(answer !== null, `${width}px: pressing Run sent no POST`);
        expect(sentModel === 'gpt-5.6-sol', `${width}px: Run sent model ${sentModel}, not gpt-5.6-sol`);

        await openDrawer();
        const held = await listbox.getAttribute('aria-disabled');
        expect(held === 'true', `${width}px: with the POST open, the model picker is aria-disabled=${held}`);
        // A pick on the held list is ignored; `force` because a held list is,
        // rightly, not something Playwright will click on its own.
        await option('gpt-5.6-luna').scrollIntoViewIfNeeded();
        await option('gpt-5.6-luna').click({ force: true });
        const picked = await option('gpt-5.6-sol').getAttribute('aria-selected');
        expect(picked === 'true', `${width}px: a pick during the open POST moved the model off Sol`);
        await closeDrawer();
        const sending = ((await button.textContent()) ?? '').trim();
        expect(sending === 'Sending…', `${width}px: a pick during the open POST turned Run to "${sending}"`);
        expect(!(await button.isEnabled()), `${width}px: a pick during the open POST gave Run back`);

        current = { ...request('queued', HOST_LOAD), id: 'ui-check-sol', model: 'gpt-5.6-sol' };
        answer({ json: { request: current, created: true, drive: { state: 'running', running: true, reason: null } } });
        const queued = await labelUntil(button, 'Queued…', sleep);
        expect(queued === 'Queued…', `${width}px: once Sol's POST answers, Run reads "${queued}", not "Queued…"`);
        await painted(page, button, `${width}px: Sol's Queued… label after the POST`);

        await openDrawer();
        const free = await listbox.getAttribute('aria-disabled');
        expect(free === 'false', `${width}px: after the POST answered, the model picker is aria-disabled=${free}`);
        const still = await option('gpt-5.6-sol').getAttribute('aria-selected');
        expect(still === 'true', `${width}px: after the POST answered, Sol is no longer the picked model`);
        await closeDrawer();
      } finally {
        await ctx.close();
      }
    });
  }
}
