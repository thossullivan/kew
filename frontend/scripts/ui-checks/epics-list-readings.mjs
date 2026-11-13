/**
 * The Epics list's readings against the epic page's (#2844): the heartbeat word
 * a row states is the one its epic's page states, no cell prints a snake_case
 * signal code, and at 390 every epic row is the same two-line block. And no
 * CHANGE cell prints a 0 (D34): DESIGN.md shows only non-zero counts.
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 */
// ui-check-owns: src/routes/(factory)/epics/+page.svelte
// ui-check-owns: src/lib/epic/roster.ts
// ui-check-owns: src/routes/(factory)/epics/+page.ts
// ui-check-owns: src/routes/(factory)/epics/[ref]/+page.svelte
// ui-check-owns: src/routes/(factory)/epics/[ref]/+page.ts
// ui-check-owns: src/lib/factory/TickStrip.svelte
// ui-check-owns: fixtures/factory/epics.json
// ui-check-owns: fixtures/factory/epics-go-live-2026-09*.json
// ui-check-owns: fixtures/factory/epics-launch-fixes*.json

const PHONE = { width: 390, height: 844 };

/** A recorded code as printed: lower-case words joined by underscores. */
const SNAKE = /\b[a-z0-9]+_[a-z0-9_]+\b/;

/**
 * The fixture's epics, both shelves: two active, one complete and one archived.
 * The heartbeat check reads every one of them, so a fixture edit that drops or
 * adds an epic fails it out loud rather than shrinking what it compares.
 */
const ACTIVE_EPICS = ['go-live-2026-09', 'launch-fixes'];
const FINISHED_EPICS = ['security-wave', 'tui-rewrite'];

/**
 * The heartbeat word every row of the list states, both shelves open, once the
 * active rows' strips are read. The row's word is read against its tick log,
 * so wait for the strips the ACTIVE rows draw (from 1280); the Finished /
 * Archived shelf reads no strip, so its rows are not waited on.
 */
async function listWords(page) {
  await openFinished(page);
  await page.waitForFunction(
    ([strip, active]) =>
      active.every((ref) => document.querySelector(`[data-epic-row="${ref}"] ${strip}`)),
    [STRIP, ACTIVE_EPICS],
    { timeout: 15_000 }
  );
  return page.$$eval('[data-epic-row]', (rows) =>
    rows.map((row) => ({
      ref: row.getAttribute('data-epic-row'),
      word: row.querySelector('[data-signal]')?.getAttribute('data-heartbeat') ?? null
    }))
  );
}

/**
 * Serve an epic page the fixture set has no detail for (the finished shelf's
 * `security-wave` and `tui-rewrite`) from that epic's own summary row: the
 * go-live detail's shape, with this epic's signal, counts and heartbeat. The
 * page then reads its heartbeat the page's way, off what the list was served.
 */
async function servePageFromSummary(page, ref) {
  await page.route(`**/api/epics/${encodeURIComponent(ref)}`, async (route) => {
    const origin = new URL(route.request().url()).origin;
    const [roster, template] = await Promise.all([
      route.fetch({ url: `${origin}/api/epics` }).then((r) => r.json()),
      route.fetch({ url: `${origin}/api/epics/${GO_LIVE}` }).then((r) => r.json())
    ]);
    const row = roster.find((epic) => epic.epic_ref === ref);
    await route.fulfill({
      json: {
        ...template,
        epic_ref: ref,
        snapshot: { ...template.snapshot, epic_ref: ref, signal: row.signal, counts: row.counts },
        heartbeat: {
          ...template.heartbeat,
          last_tick_ts: row.last_tick_ts,
          last_tick_started_at: row.last_tick_started_at ?? null,
          last_tick_duration_s: row.last_tick_duration_s ?? null,
          interval_s: row.interval_s,
          last_signal: row.signal,
          change_count: row.change_count
        }
      }
    });
  });
  await page.route(`**/api/epics/${encodeURIComponent(ref)}/*`, async (route) => {
    const url = new URL(route.request().url());
    if (!url.pathname.endsWith('/timeline')) return route.fulfill({ json: [] });
    const timeline = await route.fetch({ url: `${url.origin}/api/epics/${GO_LIVE}/timeline` });
    return route.fulfill({ response: timeline });
  });
}

/** Every text a row's cells print, one entry per leaf element. */
async function rowTexts(page) {
  return page.$$eval('[data-epic-row]', (rows) =>
    rows.flatMap((row) =>
      [...row.querySelectorAll('*')]
        .filter((el) => el.children.length === 0)
        .map((el) => ({ ref: row.getAttribute('data-epic-row'), text: (el.textContent ?? '').trim() }))
        .filter((cell) => cell.text !== '')
    )
  );
}

/**
 * The served roster with signals this console must word: `awaiting_merges`
 * (the code the review found printed raw) and one no table has met.
 */
async function codedSignals(page) {
  await page.route('**/api/epics', async (route) => {
    const response = await route.fetch();
    const epics = await response.json();
    const coded = epics.map((epic, i) =>
      i === 0 ? { ...epic, signal: 'awaiting_merges' } : i === 1 ? { ...epic, signal: 'brand_new_signal' } : epic
    );
    await route.fulfill({ response, json: coded });
  });
}

/** The lines a row's block takes: its drawn cells grouped by vertical centre. */
async function rowLines(page) {
  return page.$$eval('[data-epic-row]', (rows) =>
    rows.map((row) => {
      const centres = [...row.children]
        .filter((cell) => cell.getAttribute('aria-hidden') !== 'true')
        .map((cell) => cell.getBoundingClientRect())
        .filter((box) => box.width > 0 && box.height > 0)
        .map((box) => box.top + box.height / 2)
        .sort((a, b) => a - b);
      let lines = 0;
      let last = Number.NEGATIVE_INFINITY;
      for (const centre of centres) {
        if (centre - last > 8) lines += 1;
        last = centre;
      }
      return {
        ref: row.getAttribute('data-epic-row'),
        lines,
        height: Math.round(row.getBoundingClientRect().height)
      };
    })
  );
}

async function openFinished(page) {
  const toggle = page.getByRole('button', { name: /retired/ });
  if ((await toggle.count()) === 0) return;
  if ((await toggle.getAttribute('aria-expanded')) !== 'true') await toggle.click();
  await page.locator('[data-epic-row]').last().waitFor({ state: 'visible', timeout: 15_000 });
}

export async function checks({ check, visit, expect, sizes }) {
  /**
   * #2844 N18 — the list said "progressing · no heartbeat" in grey for an epic
   * whose page said live, cyan "progressing": the page read its heartbeat off
   * the tick log and the list off the change journal. Every fixture epic, on
   * both shelves, is read in both places — the finished shelf starts closed,
   * and reading only the rows it drew left the complete and archived epics
   * uncompared. The words must match, and at least one is `live`, which is the
   * reading the review found missing.
   */
  await check('each fixture epic states the same heartbeat word on the list and its page', async () => {
    const listed = await visit('/epics', (page) => listWords(page));
    const refs = listed.map((row) => row.ref).sort();
    const want = [...ACTIVE_EPICS, ...FINISHED_EPICS].sort();
    expect(
      JSON.stringify(refs) === JSON.stringify(want),
      `the Epics list drew ${JSON.stringify(refs)}, not the fixture's ${JSON.stringify(want)}`
    );
    expect(
      listed.some((row) => row.word === 'live'),
      `no Epics row reads live: ${JSON.stringify(listed)}`
    );
    for (const row of listed) {
      expect(row.word !== null, `${row.ref}: the Signal cell carries no heartbeat word`);
      const onPage = await visit(
        `/epics/${encodeURIComponent(row.ref)}`,
        async (page) => {
          const cell = page.locator('[data-heartbeat]').first();
          await cell.waitFor({ state: 'attached', timeout: 15_000 });
          // The page reads its own strip as well; give it the time to agree, and
          // report what it finally says if it never does.
          await page
            .waitForFunction(
              (want) => document.querySelector('[data-heartbeat]')?.getAttribute('data-heartbeat') === want,
              row.word,
              { timeout: 10_000 }
            )
            .catch(() => {});
          return cell.getAttribute('data-heartbeat');
        },
        undefined,
        FINISHED_EPICS.includes(row.ref) ? (page) => servePageFromSummary(page, row.ref) : undefined
      );
      expect(
        onPage === row.word,
        `${row.ref}: the list reads "${row.word}" and the epic page "${onPage}"`
      );
    }
  });

  /**
   * #2844 P8 — the signal cell printed the raw code `awaiting_merges`. Read
   * over the committed fixture and over one whose signals are codes, at both
   * widths: no Epics cell prints a snake_case word, and the code is the
   * signal word's title.
   */
  for (const size of [{ width: sizes.WIDTH, height: sizes.HEIGHT }, PHONE]) {
    await check(`no Epics cell prints a snake_case signal code (${size.width}px)`, async () => {
      for (const prepare of [undefined, codedSignals]) {
        await visit(
          '/epics',
          async (page) => {
            await openFinished(page);
            const cells = await rowTexts(page);
            expect(cells.length > 0, 'the Epics list drew no cells');
            for (const cell of cells) {
              expect(!SNAKE.test(cell.text), `${cell.ref}: a cell prints "${cell.text}"`);
            }
            if (!prepare) return;
            const titled = await page.$$eval('[data-signal] [title]', (els) =>
              els.map((el) => ({ title: el.getAttribute('title'), text: (el.textContent ?? '').trim() }))
            );
            const merges = titled.find((el) => el.title === 'awaiting_merges');
            expect(merges !== undefined, 'no signal word carries awaiting_merges as its title');
            expect(merges.text === 'awaiting merges', `awaiting_merges reads "${merges.text}"`);
          },
          size,
          prepare
        );
      }
    });
  }

  /**
   * #2844 P22 — at 390 go-live broke into three lines and launch-fixes two.
   * Every fixture epic row, on both shelves, is now the same block: the same
   * number of lines and the same height.
   */
  await check('at 390 every fixture epic row has the same number of lines', () =>
    visit(
      '/epics',
      async (page) => {
        await openFinished(page);
        const rows = await rowLines(page);
        expect(rows.length > 1, `only ${rows.length} Epics row drawn at 390`);
        const lines = new Set(rows.map((row) => row.lines));
        expect(lines.size === 1, `the rows take different line counts: ${JSON.stringify(rows)}`);
        expect(rows[0].lines === 2, `a row takes ${rows[0].lines} lines, not two: ${JSON.stringify(rows)}`);
        const heights = rows.map((row) => row.height);
        expect(
          Math.max(...heights) - Math.min(...heights) <= 2,
          `the rows are different heights: ${JSON.stringify(rows)}`
        );
      },
      PHONE
    )
  );

  await heartbeatAndStripChecks({ check, visit, expect, sizes });
  await stalledFitAndStaleChecks({ check, visit, expect, sizes });
}

// --- #3054: stalled says why, Nodes fits, Ticks aligns, a stale list says so --

/** Serve the roster with go-live stalled: the fixture's dashboard has a person
 * waiting on it. `tui-rewrite` (Finished shelf) is stalled with nobody. */
async function stalledGoLive(page) {
  await page.route('**/api/epics', async (route) => {
    const response = await route.fetch();
    const epics = await response.json();
    await route.fulfill({
      response,
      json: epics.map((epic) => (epic.epic_ref === GO_LIVE ? { ...epic, signal: 'stalled' } : epic))
    });
  });
}

/** Each stalled row's signal: its words, its colour, and the two tokens'
 * colours resolved the same way, so they can be compared. */
async function stalledCells(page) {
  return page.$$eval('[data-epic-row]', (rows) => {
    const resolve = (value) => {
      const probe = document.createElement('span');
      probe.style.color = value;
      document.body.append(probe);
      const color = getComputedStyle(probe).color;
      probe.remove();
      return color;
    };
    const accent = resolve('var(--accent)');
    const warn = resolve('var(--warn)');
    return rows
      .map((row) => ({ ref: row.getAttribute('data-epic-row'), cell: row.querySelector('[data-signal]') }))
      .filter(({ cell }) => cell?.querySelector('[title="stalled"]'))
      .map(({ ref, cell }) => ({
        ref,
        text: (cell.textContent ?? '').replace(/\s+/g, ' ').trim(),
        reason: cell.getAttribute('data-stalled-reason'),
        color: getComputedStyle(cell).color,
        accent,
        warn
      }));
  });
}

/** Every Nodes cell's marks: the box they are clipped to, and each mark's. */
async function nodesMarks(page) {
  return page.$$eval('[data-epic-row] [data-nodes-marks]', (cells) =>
    cells.map((cell) => {
      const box = cell.getBoundingClientRect();
      return {
        ref: cell.closest('[data-epic-row]').getAttribute('data-epic-row'),
        box: { top: box.top, bottom: box.bottom, height: box.height },
        marks: [...cell.children].map((mark) => {
          const r = mark.getBoundingClientRect();
          return { top: r.top, bottom: r.bottom, height: r.height };
        })
      };
    })
  );
}

async function stalledFitAndStaleChecks({ check, visit, expect, sizes }) {
  /**
   * N14 — "stalled" was amber on one epic and magenta on another, with
   * nothing saying why. A stalled epic with people waiting is magenta and says
   * how many need you; one with nobody is amber and says its reason. Neither
   * is a bare "stalled".
   */
  for (const size of [{ width: sizes.WIDTH, height: sizes.HEIGHT }, PHONE]) {
    await check(`a stalled epic says why, magenta only where a person is next (${size.width}px)`, () =>
      visit(
        '/epics',
        async (page) => {
          await openFinished(page);
          // The needs-you count rides the shell pulse: wait for it to reach the
          // row, and let the expects below say what it read if it never does.
          await page
            .waitForFunction(
              (ref) =>
                document
                  .querySelector(`[data-epic-row="${ref}"] [data-signal]`)
                  ?.getAttribute('data-stalled-reason')
                  ?.includes('need you'),
              GO_LIVE,
              { timeout: 15_000 }
            )
            .catch(() => {});
          const cells = await stalledCells(page);
          const waited = cells.find((c) => c.ref === GO_LIVE);
          const none = cells.find((c) => c.ref === 'tui-rewrite');
          expect(waited !== undefined && none !== undefined, `stalled rows drawn: ${JSON.stringify(cells)}`);
          expect(/\bneed you\b/.test(waited.text), `${GO_LIVE} reads "${waited.text}", not "need you"`);
          expect(waited.color === waited.accent, `${GO_LIVE} is ${waited.color}, not magenta ${waited.accent}`);
          expect(!/need you/.test(none.text), `tui-rewrite, with nobody waiting, reads "${none.text}"`);
          expect(none.color === none.warn, `tui-rewrite is ${none.color}, not amber ${none.warn}`);
          for (const cell of cells) {
            expect(cell.reason, `${cell.ref} carries no stalled reason`);
            expect(cell.text !== 'stalled', `${cell.ref} reads a bare "stalled"`);
          }
        },
        size,
        stalledGoLive
      )
    );
  }

  /**
   * N18 — go-live's Nodes cell wrapped onto a second line. At both widths no
   * Nodes cell draws a mark below its first line; at 1440 the column holds
   * go-live's marks whole (nothing is clipped).
   */
  for (const size of [{ width: sizes.WIDTH, height: sizes.HEIGHT }, PHONE]) {
    await check(`no Nodes cell wraps (${size.width}px)`, () =>
      visit(
        '/epics',
        async (page) => {
          await openFinished(page);
          const cells = await nodesMarks(page);
          expect(cells.length > 1, `only ${cells.length} Nodes cells drawn`);
          for (const cell of cells) {
            const shown = cell.marks.filter((m) => m.top < cell.box.bottom - 1);
            const tops = shown.map((m) => Math.round(m.top));
            expect(
              Math.max(...tops) - Math.min(...tops) <= 2,
              `${cell.ref}: the Nodes marks sit on more than one line: ${JSON.stringify(tops)}`
            );
            expect(
              cell.box.height <= Math.max(...shown.map((m) => m.height)) + 6,
              `${cell.ref}: the Nodes cell is ${cell.box.height}px tall, more than one line`
            );
            if (size.width === sizes.WIDTH && cell.ref === GO_LIVE) {
              expect(
                shown.length === cell.marks.length,
                `${GO_LIVE}: ${cell.marks.length - shown.length} Nodes mark(s) do not fit at ${size.width}`
              );
            }
          }
        },
        size
      )
    );
  }

  /** N18 — each row's first Ticks mark starts at its left-aligned head's
   * left edge, not at the right end of a strip of empty slots. */
  await check('each Ticks mark starts at the Ticks head’s left edge', () =>
    visit('/epics', async (page) => {
      await page.waitForFunction(
        ([strip, active]) => active.every((ref) => document.querySelector(`[data-epic-row="${ref}"] ${strip}`)),
        [STRIP, ACTIVE_EPICS],
        { timeout: 15_000 }
      );
      const read = await page.evaluate((strip) => {
        const head = [...document.querySelectorAll('span')].find((s) => s.textContent.trim() === 'Ticks');
        const rows = [...document.querySelectorAll('[data-epic-row]')].flatMap((row) => {
          const drawn = [...(row.querySelector(strip)?.children ?? [])].find((slot) => slot.children.length > 0);
          const mark = drawn?.firstElementChild;
          return mark ? [{ ref: row.getAttribute('data-epic-row'), left: mark.getBoundingClientRect().left }] : [];
        });
        return { head: head?.getBoundingClientRect().left ?? null, rows };
      }, STRIP);
      expect(read.head !== null, 'no Ticks head drawn');
      expect(read.rows.length > 0, 'no Ticks mark drawn');
      for (const row of read.rows) {
        expect(
          Math.abs(row.left - read.head) <= 2,
          `${row.ref}: the first Ticks mark starts at ${row.left}px, the head at ${read.head}px`
        );
      }
    })
  );

  /**
   * C7-5 — a list read held past 20 s is aborted, and the page said nothing:
   * the rows looked current. Every read after the page's load is held for
   * good; the page must say, in a sentence, that the list is stale and when it
   * was last read.
   */
  await check('a list read that passes 20 s shows the stale sentence', () =>
    visit(
      '/epics',
      async (page) => {
        const note = page.locator('[data-epics-stale]');
        await note.waitFor({ state: 'visible', timeout: 40_000 });
        const text = ((await note.textContent()) ?? '').replace(/\s+/g, ' ').trim();
        expect(/\bstale\b/.test(text), `the stale note reads "${text}"`);
        expect(/last read at \d/.test(text), `the stale note says no time it was last read: "${text}"`);
        expect(
          (await page.locator('[data-epic-row]').count()) > 0,
          'the stale note replaced the rows it is about'
        );
      },
      undefined,
      async (page) => {
        let reads = 0;
        await page.route('**/api/epics', async (route) => {
          reads += 1;
          if (reads === 1) return route.continue();
          // Never answered: the poll's own hold has to give up on it.
          await new Promise(() => {});
        });
      }
    )
  );
}

// --- one heartbeat window, rows past the strip cap, change marks, SPEND -------

const GO_LIVE = 'go-live-2026-09';

/** A row's change strip — not the status marks, which are images too. */
const STRIP = '[role="img"][aria-label*="driver ticks"]';

/** An ISO instant `s` seconds before now. */
const ago = (s) => new Date(Date.now() - s * 1000).toISOString();

/** One tick-log row, as `/tick-strip` serves it. An open one is a tick under
 * way — past `begin_epic_tick` — unless `over` says otherwise (#2882). */
function attempt(id, started, finished, over = {}) {
  return {
    id,
    ts_started: started,
    ts_finished: finished,
    outcome: finished === null ? null : 'nothing',
    signal: 'progressing',
    n_dispatched: 0,
    n_merged: 0,
    n_held: 0,
    n_stuck: 0,
    journal_tick_id: null,
    error_class: null,
    reached_tick: true,
    ...over
  };
}

/**
 * Serve go-live's strip as `kew serve` would: `?changes=true` filters to the
 * changes — the FIRST attempt carrying each journal id, since a no-op after a
 * change carries that change's id — the failed ticks and the open one;
 * otherwise the newest `limit` rows.
 */
async function serveStrip(page, rows) {
  const firsts = new Set();
  const seen = new Set();
  for (const r of rows) {
    if (r.journal_tick_id === null || seen.has(r.journal_tick_id)) continue;
    seen.add(r.journal_tick_id);
    firsts.add(r);
  }
  await page.route(`**/api/epics/${GO_LIVE}/tick-strip*`, async (route) => {
    const url = new URL(route.request().url());
    const limit = Number(url.searchParams.get('limit') ?? '200');
    const marks =
      url.searchParams.get('changes') === 'true'
        ? rows.filter((r) => firsts.has(r) || r.outcome === 'failed' || r.ts_finished === null)
        : rows;
    await route.fulfill({ json: marks.slice(-limit) });
  });
}

/**
 * Serve the roster with go-live's newest attempt replaced — and, given
 * `lastTick`, the real heartbeat both surfaces serve stamped then.
 * `reached` is whether that attempt got past `begin_epic_tick` (#2882).
 */
async function serveAttempt(
  page,
  started,
  finished,
  { reached = true, lastTick = null, outcome = finished === null ? null : 'nothing' } = {}
) {
  await page.route('**/api/epics', async (route) => {
    const response = await route.fetch();
    const epics = await response.json();
    const moved = epics.map((epic) =>
      epic.epic_ref === GO_LIVE
        ? {
            ...epic,
            last_tick_started_at: lastTick,
            last_tick_ts: lastTick,
            last_tick_duration_s: null,
            last_attempt_started_at: started,
            last_attempt_finished_at: finished,
            last_attempt_outcome: outcome,
            last_attempt_reached_tick: reached
          }
        : epic
    );
    await route.fulfill({ response, json: moved });
  });
  if (lastTick === null) return;
  await page.route(`**/api/epics/${GO_LIVE}`, async (route) => {
    const response = await route.fetch();
    const detail = await response.json();
    await route.fulfill({
      response,
      json: {
        ...detail,
        heartbeat: { ...detail.heartbeat, last_tick_started_at: lastTick, last_tick_ts: lastTick }
      }
    });
  });
}

/** The heartbeat word a page states for go-live: its list row, or its own page. */
async function goLiveWord(visit, path, started, finished, served = {}) {
  return visit(
    path,
    async (page) => {
      const sel = path === '/epics' ? `[data-epic-row="${GO_LIVE}"] [data-signal]` : '[data-heartbeat]';
      const cell = page.locator(sel).first();
      await cell.waitFor({ state: 'attached', timeout: 15_000 });
      return cell.getAttribute('data-heartbeat');
    },
    undefined,
    async (page) => {
      await serveAttempt(page, started, finished, served);
      await serveStrip(page, [
        attempt(900, started, finished, { reached_tick: served.reached ?? true })
      ]);
    }
  );
}

async function heartbeatAndStripChecks({ check, visit, expect, sizes }) {
  /**
   * Finding 1 — one window on both surfaces. A 60s driver whose tick started
   * 150s ago (2.5 intervals) read "no heartbeat" on the list and live on the
   * page; a finished tick that ran 100s widens the window to 260s, and the
   * list did not know the duration.
   */
  for (const [name, started, finished] of [
    ['a tick 2.5 intervals old', ago(150), null],
    ['a long finished tick, 250s old', ago(250), ago(150)]
  ]) {
    await check(`the list and the epic page read one heartbeat for ${name}`, async () => {
      const listed = await goLiveWord(visit, '/epics', started, finished);
      const onPage = await goLiveWord(visit, `/epics/${GO_LIVE}`, started, finished);
      expect(listed === 'live', `the list reads "${listed}" for ${name}`);
      expect(onPage === listed, `the list reads "${listed}" and the epic page "${onPage}"`);
    });
  }

  /**
   * #2882 — an attempt row opens before the lock, the preflight and the graph
   * build. A 60s driver whose last real tick was ten minutes ago, with an
   * attempt opened 10s ago that is still blocked in preflight, read live on
   * both surfaces though no tick ran. Until the server says the attempt
   * reached its tick, both read no heartbeat; once it does, both read live.
   */
  for (const [reached, want] of [
    [false, 'no heartbeat'],
    [true, 'live']
  ]) {
    await check(`an open attempt ${reached ? 'past' : 'before'} its tick reads ${want} on both surfaces`, async () => {
      const served = { reached, lastTick: ago(600) };
      const started = ago(10);
      const listed = await goLiveWord(visit, '/epics', started, null, served);
      const onPage = await goLiveWord(visit, `/epics/${GO_LIVE}`, started, null, served);
      expect(listed === want, `the list reads "${listed}", not "${want}"`);
      expect(onPage === want, `the epic page reads "${onPage}", not "${want}"`);
    });
  }

  /**
   * #2882 round 2 — the heartbeat word was not the only reading. A 60s driver
   * whose real tick was 600s ago, then a 10s-old attempt that ran no tick
   * (refused, or still before `begin_epic_tick`), drew that attempt as a cyan
   * `running` tick and printed "last tick 10s ago" beside "no heartbeat", on
   * the list's strip and the page's alike. Neither surface may: no cyan, no
   * `running`, and the last tick stays the T-600s one.
   */
  for (const [name, finished, over] of [
    ['a failed attempt', ago(9), { outcome: 'failed', error_class: 'RuntimeError' }],
    ['a locked-out attempt', ago(9), { outcome: 'locked_out' }],
    ['an empty-graph attempt', ago(9), { outcome: 'empty_graph' }],
    ['an attempt before its tick', null, { outcome: null, reached_tick: false }]
  ]) {
    await check(`${name} 10s after a 600s-old tick draws no live tick on either surface`, async () => {
      const lastTick = ago(600);
      const started = ago(10);
      const rows = [
        attempt(800, lastTick, ago(599)),
        attempt(900, started, finished, { reached_tick: false, ...over })
      ];
      const arrange = async (page) => {
        await serveAttempt(page, started, finished, {
          reached: false,
          lastTick,
          outcome: over.outcome
        });
        await serveStrip(page, rows);
      };
      const read = async (page, strip, lastSel) => {
        const drawn = page.locator(strip).first();
        await drawn.waitFor({ state: 'attached', timeout: 15_000 });
        const last = page.locator(lastSel).first();
        await last.waitFor({ state: 'attached', timeout: 15_000 });
        return drawn.evaluate(
          (el, lastTs) => ({
            label: el.getAttribute('aria-label') ?? '',
            cyan: el.outerHTML.includes('--accent-2'),
            grown: [...el.querySelectorAll('rect')].some((r) => r.getAttribute('height') === '14'),
            last: lastTs
          }),
          await last.getAttribute('data-last-tick')
        );
      };
      const listed = await visit(
        '/epics',
        (page) => {
          const row = `[data-epic-row="${GO_LIVE}"]`;
          return read(page, `${row} ${STRIP}`, `${row} [data-last-tick]`);
        },
        { width: sizes.WIDTH, height: sizes.HEIGHT },
        arrange
      );
      const onPage = await visit(
        `/epics/${GO_LIVE}`,
        (page) => read(page, STRIP, '[data-last-tick]'),
        { width: sizes.WIDTH, height: sizes.HEIGHT },
        arrange
      );
      for (const [where, got] of [
        ['the list', listed],
        ['the epic page', onPage]
      ]) {
        expect(!got.cyan, `${where} paints ${name} cyan`);
        expect(!got.grown, `${where} grows the bar for ${name}`);
        expect(!got.label.includes('running'), `${where} reads ${name} as running: "${got.label}"`);
        expect(got.last === lastTick, `${where} stamps the last tick ${got.last}, not ${lastTick}`);
      }
    });
  }

  /**
   * Finding 2 — the list fetches strips for at most STRIP_ROW_CAP (20) rows a
   * shelf, and the heartbeat used to come from them: row 21 said "no
   * heartbeat" while its page said live. Twenty-four active epics, each with a
   * fresh attempt on the summary and no strip at all, all read live.
   */
  await check('every active epic past the strip cap reads its heartbeat', () =>
    visit(
      '/epics',
      async (page) => {
        await page.locator('[data-epic-row="cap-24"]').waitFor({ state: 'attached', timeout: 15_000 });
        const words = await page.$$eval('[data-epic-row^="cap-"] [data-signal]', (cells) =>
          cells.map((cell) => cell.getAttribute('data-heartbeat'))
        );
        expect(words.length === 24, `${words.length} capped rows drawn, not 24`);
        const quiet = words.filter((word) => word !== 'live').length;
        expect(quiet === 0, `${quiet} of 24 rows read no heartbeat: ${JSON.stringify(words)}`);
      },
      { width: sizes.WIDTH, height: sizes.HEIGHT },
      async (page) => {
        await page.route('**/api/epics/*/tick-strip*', (route) => route.fulfill({ json: [] }));
        await page.route('**/api/epics', async (route) => {
          const response = await route.fetch();
          const [template] = await response.json();
          const epics = Array.from({ length: 24 }, (_, i) => {
            const ref = `cap-${String(i + 1).padStart(2, '0')}`;
            return {
              ...template,
              epic_ref: ref,
              signal: 'progressing',
              last_tick_ts: ago(600 + i),
              last_tick_started_at: ago(600 + i),
              last_attempt_started_at: ago(10),
              last_attempt_finished_at: ago(9),
              tick_attempt_stamp: `${i}.1`,
              archived: false
            };
          });
          await route.fulfill({ response, json: epics });
        });
      }
    )
  );

  /**
   * Finding 3 — one change followed by 201 no-op attempts. Reading the last
   * 200 attempts found no change and drew an empty strip beside CHANGE 1. The
   * no-ops carry the change's journal id, as real rows do (the journal's dedup
   * hands back the row it has), and must not draw a mark each.
   */
  await check('a change behind 201 no-op attempts still draws its mark', () =>
    visit(
      '/epics',
      async (page) => {
        const strip = page.locator(`[data-epic-row="${GO_LIVE}"] ${STRIP}`);
        await strip.waitFor({ state: 'attached', timeout: 15_000 });
        const dots = await strip.locator('span[style*="--text-2"]').count();
        expect(dots === 1, `the strip draws ${dots} change marks, not 1`);
      },
      { width: sizes.WIDTH, height: sizes.HEIGHT },
      (page) =>
        serveStrip(page, [
          attempt(1, ago(7200), ago(7199), { journal_tick_id: 1 }),
          ...Array.from({ length: 201 }, (_, i) =>
            attempt(2 + i, ago(7000 - i * 30), ago(6999 - i * 30), { journal_tick_id: 1 })
          )
        ])
    )
  );

  /**
   * Finding 5 — a change was drawn as the green done dot: three live strips of
   * all-green slots read the same at very different CHANGE counts. A change is
   * a neutral dot now; green appears in no strip of the committed fixture.
   */
  await check('the Epics strips draw changes as neutral dots, never green', () =>
    visit('/epics', async (page) => {
      const strip = page.locator(`[data-epic-row="${GO_LIVE}"] ${STRIP}`);
      await strip.waitFor({ state: 'attached', timeout: 15_000 });
      const dots = await strip.locator('span[style*="--text-2"]').count();
      expect(dots === 2, `go-live's strip draws ${dots} change dots for its CHANGE of 2`);
      const green = await page.$$eval(`[data-epic-row] ${STRIP}`, (strips) =>
        strips.filter((s) => s.innerHTML.includes('--success')).length
      );
      expect(green === 0, `${green} strips draw a green mark`);
      const legend = await page.$$eval('span', (spans) =>
        spans.filter((s) => s.textContent.trim() === 'Ticks').map((s) => s.getAttribute('title'))
      );
      expect(
        legend.length > 0 && legend.every((title) => title && title.includes('change')),
        `the Ticks head carries no legend: ${JSON.stringify(legend)}`
      );
    })
  );

  /** Finding 4 — the SPEND head prints one word and is titled "drain spend". */
  await check('the Spend head is titled drain spend', () =>
    visit('/epics', async (page) => {
      const heads = await page.$$eval('span', (spans) =>
        spans
          .filter((s) => s.textContent.trim() === 'Spend')
          .map((s) => ({ text: s.textContent.trim(), title: s.getAttribute('title') }))
      );
      expect(heads.length > 0, 'no Spend head drawn');
      for (const head of heads)
        expect(head.title === 'drain spend', `the Spend head is titled "${head.title}"`);
    })
  );

  /**
   * D34 — a quiet epic's CHANGE cell printed "0". The committed fixture has
   * one epic with no change in its strip's window, so the reading is made.
   */
  await check('no CHANGE cell on the Epics list reads 0', () =>
    visit('/epics', async (page) => {
      await openFinished(page);
      await page.locator('[data-change-count]').first().waitFor({ state: 'attached', timeout: 15_000 });
      const cells = await page.$$eval('[data-change-count]', (els) =>
        els.map((el) => ({ count: el.getAttribute('data-change-count'), text: (el.textContent ?? '').trim() }))
      );
      expect(
        cells.some((cell) => cell.count === '0'),
        'no fixture epic counts 0 changes, so this proves nothing'
      );
      for (const cell of cells) {
        expect(cell.text !== '0', `a CHANGE cell reads "0" (${cell.count} changes)`);
        if (cell.count !== '' && cell.count !== '0') {
          expect(cell.text === cell.count, `a CHANGE cell counting ${cell.count} reads "${cell.text}"`);
        }
      }
    })
  );
}
