/**
 * The node popover's needs-you sentence and action row never scroll out of
 * sight (#2892, R2).
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 *
 * The popover's body scrolls above its pinned action row (#2783), and the fade
 * that cues the scroll (#2843 P1) sat over "PR #2059 is open — the merge
 * decision is yours" in the fixture's own `?node=2055` popover. The header, the
 * needs-you block and the action row are now fixed; only the run and review
 * detail scrolls, under its own fade. The fixture's run detail is short, so the
 * second check serves a 600-character one through `page.route`: a short string
 * is how a one-line change ships unchecked (#2889).
 *
 * Live hold reasons run to 300 characters with a command under them (#2889),
 * so a third check serves #2055 a 120-character title, a 300-character reason
 * and a long command: the label, the sentence (held to three lines) and every
 * action-row control stay on screen, and the command scrolls with the detail.
 * Each command line and the Copy control are then scrolled into view and must
 * stand wholly clear of the fade. A fourth serves only the long command over
 * a stale review: at scrollTop 0 the review-stale summary is above
 * the fold, ahead of the command. The fixture's own #2055 review is current
 * (#3461), so each check about a stale one serves it `staleReview`'s moved
 * head. A fifth leaves one node's detail scrolled down
 * and selects another, whose detail has to start at its top (#2892).
 *
 * Two more close round 2 of the review (#2892). One serves #2055 a needs-rebase
 * badge with a long cause beside its stale review: `badgesFor` puts the rebase
 * first, and its wrapped cause once came before the "review stale" summary, so
 * every summary line now leads every cause and the summary is found by its own
 * words, not its place. The other serves a 1,000-character title, which the
 * popover holds to two lines (one in a short window) so the action row stays
 * on screen; the lines are counted as drawn, the clamp read as computed, and
 * the detail under them must keep some height.
 *
 * Round 3 adds the last: the detail held at 56–59px over the long command.
 * The fade once switched on at 56px, where the sheet's 32px Copy could never
 * stand clear of the 28px band; scrolled to, Copy must now be wholly seen.
 * #2948 holds it at 59.5px and 59.9px too: `clientHeight` rounds those to 60,
 * so the fade switched on with 31.5px above it. Below 60px drawn the fade is
 * now off. And a run link Tabbed to inside a faded detail lifts the mask, so
 * its focus ring is drawn undimmed (#2948, C7-1): the first of three served
 * runs, the two after it long, so the detail still has more below the link.
 *
 * #2977 (D38) reads it at 1440x900, the design size, with the rail at its
 * default height: a three-line needs-you sentence over a stale review (served
 * by `staleReview`, as above) shrank the detail to about a line and a half,
 * so "review stale" was cut at its foot, and on go-live the popover sat over
 * the minimap. The detail now keeps two lines, and the minimap is not drawn
 * while a popover is open.
 *
 * #3469 then reads the box at its short height, beside a card on a canvas at
 * its 360px floor, at 1440x960, 1440x900 and 1280x800: a two-line title over
 * an action row that wraps (it wrapped on the Linux verify host while the
 * arrows drew in a wider fallback font, before #3478 drew them in Plex) left
 * the detail 51.6px, under the 60px its fade needs. The box is still sized for
 * the row wrapped, so the check makes it wrap on every platform, with a style
 * that gives its first link the whole row.
 */
// ui-check-owns: src/lib/epic/NodePopover.svelte
// ui-check-owns: src/lib/epic/node-popover.ts
// ui-check-owns: src/routes/(factory)/epics/[ref]/**
// ui-check-owns: fixtures/factory/epics-launch-fixes*.json
// ui-check-owns: fixtures/factory/epics-go-live-2026-09*.json
// ui-check-owns: src/lib/epic/DagView.svelte
import { EPIC } from '../ui-check-lib.mjs';

const GO_LIVE = '/epics/go-live-2026-09';

/** The design width, a short window and a phone. */
const SIZES = [
  { width: 1440, height: 960 },
  { width: 1440, height: 560 },
  { width: 390, height: 844 }
];

const NODE = '2055';

/** 600 characters of run detail, in words so it wraps as prose does. */
const LONG_DETAIL = 'review round stalled waiting on a reviewer verdict '.repeat(12).slice(0, 600);

/** Serve #2055's run history with one run whose detail is 600 characters. */
function longRunDetail(page) {
  return page.route(`**/api/epics/launch-fixes/nodes/${NODE}`, async (route) => {
    const response = await route.fetch();
    const payload = await response.json();
    const [template] = payload.runs;
    payload.runs = [{ ...template, status: LONG_DETAIL }, ...payload.runs.slice(1)];
    await route.fulfill({ json: payload });
  });
}

/** 120 characters of title, 300 of hold reason and a command that wraps. */
const LONG_TITLE = 'hold the review gate until the reviewer verdict lands on the head it names '.repeat(2).slice(0, 120);
const LONG_REASON = 'held: the worktree has changes the last run left uncommitted and the reviewer verdict names an older head '
  .repeat(3)
  .slice(0, 300);
const LONG_COMMAND =
  'kew epic unhold launch-fixes 2055 --reason "the worktree was re-checked and the uncommitted changes were the last run\'s own"';

/** A sentence that wraps past three lines at 320px, so the popover holds it
 * at its clamp. */
const LONG_SENTENCE =
  'held: the reviewer verdict names an older head than the one the branch is on, and the merge decision waits for a fresh review of the current head';

/** A title that takes two lines in the 290px the box beside a card gives it. */
const TWO_LINE_TITLE = 'hold the review gate until the verdict lands on the head it names';

/** The shortest detail whose fade can show (`FADE_ROOM` in NodePopover). */
const FADE_ROOM = 28 + 32;

/** The sizes where the canvas sits at its 360px floor and the box beside a
 * card is drawn short (#3469). */
const FLOOR_SIZES = [
  { width: 1440, height: 960 },
  { width: 1440, height: 900 },
  { width: 1280, height: 800 }
];

/** Serve #2055's attention with `over` laid on it, and its title as `title`. */
function servedNode(page, { title, ...over }) {
  return page.route('**/api/epics/launch-fixes', async (route) => {
    const response = await route.fetch();
    const payload = await response.json();
    payload.snapshot.nodes = payload.snapshot.nodes.map((n) =>
      n.id === NODE
        ? {
            ...n,
            title: title ?? n.title,
            attention: { ...n.attention, ...over }
          }
        : n
    );
    await route.fulfill({ json: payload });
  });
}

/** Serve #2055 as a hold with a long title, reason and command. */
const longHold = (page) =>
  servedNode(page, {
    title: LONG_TITLE,
    kind: 'hold',
    sentence: LONG_REASON,
    command: LONG_COMMAND
  });

/** Serve #2055 its own sentence under a long command. */
const longCommand = (page) => servedNode(page, { command: LONG_COMMAND });

/** #2055's freshness row with the head moved past its round 1 verdict. */
const staleRow = (row) => ({
  ...row,
  latest_head: { ...row.latest_head, sha: 'a1b2c3d4e5f60718293a4b5c6d7e8f9012345678' },
  review_state: 'stale'
});

/** Serve #2055's freshness row through `lay`, every other row as served. */
function servedFreshness(page, lay) {
  return page.route('**/api/prs/freshness*', async (route) => {
    const response = await route.fetch();
    const payload = await response.json();
    payload.rows = payload.rows.map((row) => (row.item_id === NODE ? lay(row) : row));
    await route.fulfill({ json: payload });
  });
}

/** Serve #2055 a stale review. */
const staleReview = (page) => servedFreshness(page, staleRow);

/** The long command, over a stale review. */
const longCommandOverStale = async (page) => {
  await longCommand(page);
  await staleReview(page);
};

/** The long needs-you sentence, over a stale review. */
const longSentenceOverStale = async (page) => {
  await servedNode(page, { kind: 'hold', sentence: LONG_SENTENCE });
  await staleReview(page);
};

/** A rebase cause long enough to wrap over several lines at 320px. */
const LONG_BASE_REASON =
  'the base branch moved past the merge base this head was cut from and the files it touched were rewritten upstream '
    .repeat(2)
    .slice(0, 220);

/**
 * Serve #2055's freshness row as behind its base, with the long cause, read
 * now (an older reading earns no badge), beside the stale review it has.
 */
function rebaseAndStale(page) {
  return servedFreshness(page, (row) => {
    const now = new Date().toISOString();
    return {
      ...staleRow(row),
      base: { ...row.base, state: 'behind', reason: LONG_BASE_REASON, since: now, at: now }
    };
  });
}

/** 1,000 characters of title, in words so it wraps as a title does; it ends on
 * a full stop, as a title cut on a space would lose that space to the trim. */
const HUGE_TITLE =
  'keep the action row on screen however long the title of the node it is about runs '.repeat(13).slice(0, 999) + '.';

/** Serve #2055 the 1,000-character title. */
const hugeTitle = (page) => servedNode(page, { title: HUGE_TITLE });

/**
 * Serve #2055 the three runs the popover lists, newest first: a short one, then
 * two with the long detail. Focus on the newest's link scrolls it into view,
 * and the two long rows still run on below the fold under it.
 */
function runsBelowFirst(page) {
  return page.route(`**/api/epics/launch-fixes/nodes/${NODE}`, async (route) => {
    const response = await route.fetch();
    const payload = await response.json();
    const [template] = payload.runs;
    const hour = 3_600_000;
    payload.runs = [0, 1, 2].map((i) => ({
      ...template,
      run_id: `${template.run_id}-${i}`,
      status: i === 0 ? template.status : LONG_DETAIL,
      started_at: new Date(Date.now() - (i + 1) * hour).toISOString()
    }));
    await route.fulfill({ json: payload });
  });
}

/** The long command, over three runs whose first has long rows below it. */
const longCommandOverRuns = async (page) => {
  await longCommand(page);
  await runsBelowFirst(page);
};

/** Serve every node the long run detail, so whichever node is open scrolls. */
function longRunDetailEverywhere(page) {
  return page.route('**/api/epics/launch-fixes/nodes/*', async (route) => {
    const url = route.request().url().replace(/\/nodes\/[^/?]+/, `/nodes/${NODE}`);
    const response = await route.fetch({ url });
    const payload = await response.json();
    const [template] = payload.runs;
    payload.runs = [{ ...template, status: LONG_DETAIL }, ...payload.runs.slice(1)];
    await route.fulfill({ json: payload });
  });
}

/** The long run detail on every node, and a stale review on #2055. */
const longRunDetailOverStale = async (page) => {
  await longRunDetailEverywhere(page);
  await staleReview(page);
};

/**
 * The needs-you sentence, the action row and the scroll body's fade band, each
 * as a viewport box, with the popover's own box and what clips it. The fade is
 * the body's foot while it has more below the fold; `masked` is any ancestor of
 * the sentence that draws a mask at all.
 */
function readPopover(page) {
  return page.evaluate(() => {
    const pop = document.querySelector('[role="dialog"].pop');
    if (!pop) return null;
    const rect = (el) => {
      if (!el) return null;
      const r = el.getBoundingClientRect();
      return { top: r.top, bottom: r.bottom, left: r.left, right: r.right };
    };
    const why = pop.querySelector('.ny .why');
    const body = pop.querySelector('.pbody');
    const masked = [];
    for (let el = why; el && el !== document.body; el = el.parentElement) {
      const mask = getComputedStyle(el).maskImage;
      if (mask && mask !== 'none') masked.push(el.className);
    }
    const bodyBox = rect(body);
    const fading = body?.dataset.more === 'true';
    const floor = pop.closest('.pop-at')?.offsetParent ?? null;
    return {
      pop: rect(pop),
      clip: rect(floor) ?? {
        top: 0,
        left: 0,
        bottom: innerHeight,
        right: innerWidth
      },
      viewport: { top: 0, left: 0, bottom: innerHeight, right: innerWidth },
      why: rect(why),
      whyText: why?.textContent?.trim() ?? '',
      links: rect(pop.querySelector('.links')),
      label: rect(pop.querySelector('.ny .cap')),
      controls: [...pop.querySelectorAll('.links > *')].map((el) => ({
        text: el.textContent?.trim() ?? '',
        ...rect(el)
      })),
      whyClamped: why ? why.scrollHeight > why.clientHeight + 1 && why.title === why.textContent : false,
      fade: fading && bodyBox ? { ...bodyBox, top: bodyBox.bottom - 28 } : null,
      scrolls: body ? body.scrollHeight > body.clientHeight + 1 : false,
      masked
    };
  });
}

const inside = (inner, outer) =>
  inner.top >= outer.top - 0.5 &&
  inner.bottom <= outer.bottom + 0.5 &&
  inner.left >= outer.left - 0.5 &&
  inner.right <= outer.right + 0.5;

const overlaps = (a, b) => a.top < b.bottom && b.top < a.bottom && a.left < b.right && b.left < a.right;

const box = (r) => `${Math.round(r.top)}..${Math.round(r.bottom)}`;

/** Both boxes wholly on screen: inside the popover, its clip and the window. */
function expectSeen(expect, read, name, r) {
  expect(r !== null, `the popover drew no ${name}`);
  expect(inside(r, read.pop), `the ${name} (${box(r)}) runs outside the popover (${box(read.pop)})`);
  expect(inside(r, read.clip), `the ${name} (${box(r)}) is clipped by the canvas (${box(read.clip)})`);
  expect(inside(r, read.viewport), `the ${name} (${box(r)}) is off the window`);
}

/**
 * Scroll the detail so each line of the command, then the Copy control, sits
 * at the body's top, and read whether it is then wholly inside the body and
 * clear of the fade band (the bottom 28px while the body has more below). A
 * line the scroll cannot lift that far — the body's last screenful — is read
 * where it stops, with the fade as the body then draws it.
 */
function reachEach(page) {
  return page.evaluate(async () => {
    const body = document.querySelector('[role="dialog"] .pbody');
    const code = body?.querySelector('code[data-command]');
    const copy = body?.querySelector('.cmd [aria-label="Copy command"]');
    if (!body || !code || !copy) return null;
    const frames = () => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)));
    const range = document.createRange();
    range.selectNodeContents(code);
    const tops = [
      ...new Set(
        [...range.getClientRects()].map((r) => Math.round(r.top - body.getBoundingClientRect().top + body.scrollTop))
      )
    ];
    const lineRect = (i) => {
      range.selectNodeContents(code);
      const b = body.getBoundingClientRect();
      const want = tops[i];
      return [...range.getClientRects()].find((r) => Math.round(r.top - b.top + body.scrollTop) === want);
    };
    const targets = [
      ...tops.map((_, i) => ({
        name: `command line ${i + 1}`,
        rect: () => lineRect(i)
      })),
      { name: 'Copy control', rect: () => copy.getBoundingClientRect() }
    ];
    const missed = [];
    for (const target of targets) {
      const before = target.rect();
      body.scrollTop += before.top - body.getBoundingClientRect().top - 4;
      await frames();
      const b = body.getBoundingClientRect();
      const r = target.rect();
      const floor = body.dataset.more === 'true' ? b.bottom - 28 : b.bottom;
      if (!(r && r.top >= b.top - 0.5 && r.bottom <= floor + 0.5)) missed.push(target.name);
    }
    body.scrollTop = 0;
    return { text: code.textContent?.trim() ?? '', lines: tops.length, missed };
  });
}

async function opened(page, sleep) {
  await page.locator('[role="dialog"] .ny .why').first().waitFor({ state: 'visible', timeout: 15_000 });
  await page.locator('[role="dialog"] [data-history]').first().waitFor({ state: 'visible', timeout: 15_000 });
  await sleep(300);
}

export async function checks({ check, visit, expect, sleep }) {
  for (const size of SIZES) {
    const at = `${size.width}x${size.height}`;
    await check(`the needs-you sentence is inside the popover and clear of its fade at ${at}`, () =>
      visit(
        `${EPIC}?node=${NODE}`,
        async (page) => {
          await opened(page, sleep);
          const read = await readPopover(page);
          expect(read !== null, `no popover opened on ?node=${NODE}`);
          expect(read.whyText.includes('merge decision'), `the needs-you sentence reads "${read.whyText}"`);
          expectSeen(expect, read, 'needs-you sentence', read.why);
          expect(read.masked.length === 0, `the needs-you sentence sits under a mask on .${read.masked.join(', .')}`);
          if (read.fade) {
            expect(
              !overlaps(read.why, read.fade),
              `the needs-you sentence (${box(read.why)}) is under the fade (${box(read.fade)})`
            );
          }
        },
        size
      )
    );
  }

  for (const size of SIZES) {
    const at = `${size.width}x${size.height}`;
    await check(`a 600-character run detail scrolls under the needs-you sentence and the action row at ${at}`, () =>
      visit(
        `${EPIC}?node=${NODE}`,
        async (page) => {
          await opened(page, sleep);
          const served = await page.locator('[role="dialog"] .hverdict').first().textContent();
          expect((served ?? '').trim().length === 600, 'the served 600-character run detail never drew');
          const read = await readPopover(page);
          expect(read !== null, `no popover opened on ?node=${NODE}`);
          // Not vacuous: the long detail outgrows the box it is given, so the
          // body scrolls and fades — and the pinned parts still stand clear.
          expect(read.scrolls && read.fade !== null, 'the 600-character detail did not make the body scroll');
          expectSeen(expect, read, 'needs-you sentence', read.why);
          expectSeen(expect, read, 'action row', read.links);
          expect(read.masked.length === 0, `the needs-you sentence sits under a mask on .${read.masked.join(', .')}`);
          expect(
            !overlaps(read.why, read.fade) && !overlaps(read.links, read.fade),
            `the fade (${box(read.fade)}) covers the needs-you sentence (${box(read.why)}) ` +
              `or the action row (${box(read.links)})`
          );
        },
        size,
        longRunDetail
      )
    );
  }

  for (const size of [SIZES[1], SIZES[2]]) {
    const at = `${size.width}x${size.height}`;
    await check(`a 300-character hold reason leaves the label, sentence and action row on screen at ${at}`, () =>
      visit(
        `${EPIC}?node=${NODE}`,
        async (page) => {
          await opened(page, sleep);
          const read = await readPopover(page);
          expect(read !== null, `no popover opened on ?node=${NODE}`);
          expect(read.whyText === LONG_REASON, `the needs-you sentence reads "${read.whyText.slice(0, 60)}…"`);
          // Not vacuous: 300 characters run past three lines, so the clamp is
          // what keeps them, and the whole sentence is still the title.
          expect(read.whyClamped, 'the 300-character reason was not held to its three lines');
          expectSeen(expect, read, 'needs-you label', read.label);
          expectSeen(expect, read, 'needs-you sentence', read.why);
          expect(read.controls.length > 0, 'the action row drew no controls');
          for (const control of read.controls) expectSeen(expect, read, `"${control.text}" control`, control);
          // Reachable by scrolling: each command line and the Copy control,
          // scrolled to the body's top, is wholly inside the body and clear of
          // its fade. A body opened one line high, faded through, fails here.
          const reached = await reachEach(page);
          expect(reached !== null, 'the command is not in the scrolling detail');
          expect(reached.text === LONG_COMMAND, `the command reads "${reached.text}"`);
          expect(reached.lines > 1, `the command drew ${reached.lines} line(s), so its wrap proves nothing`);
          for (const miss of reached.missed) expect(false, `the ${miss} never comes wholly into view`);
        },
        size,
        longHold
      )
    );
  }

  for (const size of SIZES) {
    const at = `${size.width}x${size.height}`;
    await check(`a long command leaves the review-stale summary above the fold at ${at}`, () =>
      visit(
        `${EPIC}?node=${NODE}`,
        async (page) => {
          await opened(page, sleep);
          const read = await page.evaluate(() => {
            const body = document.querySelector('[role="dialog"] .pbody');
            // The summary line is the badge that names the state, found by
            // its own words; its causes are the detail under it.
            const summary = [...(body?.querySelectorAll('.fresh .fb-word') ?? [])].find(
              (el) => el.textContent?.trim() === 'review stale'
            );
            const code = body?.querySelector('code[data-command]');
            if (!body || !summary) return null;
            const b = body.getBoundingClientRect();
            const s = summary.getBoundingClientRect();
            const floor = body.dataset.more === 'true' ? b.bottom - 28 : b.bottom;
            return {
              top: body.scrollTop,
              command: code?.textContent?.trim() ?? '',
              ahead: code ? s.bottom <= code.getBoundingClientRect().top : false,
              summary: `${Math.round(s.top)}..${Math.round(s.bottom)}`,
              body: `${Math.round(b.top)}..${Math.round(floor)}`,
              seen: s.top >= b.top - 0.5 && s.bottom <= floor + 0.5
            };
          });
          expect(read !== null, `#${NODE}'s popover drew no review-stale summary`);
          // Not vacuous: the served long command is drawn in the same detail,
          // and the summary is ahead of it, not merely beside a short one.
          expect(read.command === LONG_COMMAND, `the command reads "${read.command}"`);
          expect(read.ahead, 'the review-stale summary comes after the command');
          expect(read.top === 0, `#${NODE}'s detail opened at scrollTop ${read.top}, not its top`);
          expect(
            read.seen,
            `the review-stale summary (${read.summary}) is not wholly above the fold (${read.body}) at scrollTop 0`
          );
        },
        size,
        longCommandOverStale
      )
    );
  }

  for (const size of SIZES) {
    const at = `${size.width}x${size.height}`;
    await check(`another node's detail starts at its top, not where the last was scrolled, at ${at}`, () =>
      visit(
        `${EPIC}?node=2053`,
        async (page) => {
          await page.locator('[role="dialog"] [data-history]').first().waitFor({ state: 'visible', timeout: 15_000 });
          await sleep(300);
          const left = await page.evaluate(() => {
            const body = document.querySelector('[role="dialog"] .pbody');
            if (!body) return 0;
            body.scrollTop = body.scrollHeight;
            return body.scrollTop;
          });
          // Not vacuous: the first node's detail really was left scrolled.
          expect(left > 0, `#2053's detail did not scroll (scrollTop ${left})`);
          await page.locator(`.dag .svelte-flow__node[data-id="${NODE}"]`).click();
          await opened(page, sleep);
          const read = await page.evaluate(() => {
            const body = document.querySelector('[role="dialog"] .pbody');
            const summary = [...(body?.querySelectorAll('.fresh .fb-word') ?? [])].find(
              (el) => el.textContent?.trim() === 'review stale'
            );
            if (!body || !summary) return null;
            const b = body.getBoundingClientRect();
            const s = summary.getBoundingClientRect();
            return {
              top: body.scrollTop,
              scrolls: body.scrollHeight > body.clientHeight + 1,
              seen: s.top >= b.top - 0.5 && s.bottom <= b.bottom + 0.5
            };
          });
          expect(read !== null, `#${NODE}'s popover drew no review-stale summary`);
          expect(read.scrolls, `#${NODE}'s detail does not scroll, so its start proves nothing`);
          expect(read.top === 0, `#${NODE}'s detail opened at scrollTop ${read.top}, not its top`);
          expect(read.seen, `#${NODE}'s review-stale summary is not fully inside the detail`);
        },
        size,
        longRunDetailOverStale
      )
    );
  }

  for (const size of SIZES) {
    const at = `${size.width}x${size.height}`;
    await check(`a long rebase cause leaves the review-stale summary above the fold at ${at}`, () =>
      visit(
        `${EPIC}?node=${NODE}`,
        async (page) => {
          await opened(page, sleep);
          await page
            .locator('[role="dialog"] .fresh .fb-word', { hasText: 'needs rebase' })
            .first()
            .waitFor({ state: 'visible', timeout: 15_000 });
          const read = await page.evaluate(() => {
            const body = document.querySelector('[role="dialog"] .pbody');
            const words = [...(body?.querySelectorAll('.fresh .fb-word') ?? [])];
            const named = (text) => words.find((el) => el.textContent?.trim() === text);
            const summary = named('review stale');
            if (!body || !summary) return null;
            const b = body.getBoundingClientRect();
            const s = summary.getBoundingClientRect();
            const floor = body.dataset.more === 'true' ? b.bottom - 28 : b.bottom;
            const causes = [...body.querySelectorAll('.fresh .causes li')];
            const range = document.createRange();
            const rebase = causes.find((li) => li.textContent?.includes('the base branch moved'));
            let lines = 0;
            if (rebase) {
              range.selectNodeContents(rebase);
              lines = new Set([...range.getClientRects()].map((r) => Math.round(r.top))).size;
            }
            return {
              top: body.scrollTop,
              rebase: rebase?.textContent?.trim() ?? '',
              lines,
              hasRebaseBadge: named('needs rebase') !== undefined,
              ahead: causes.every((li) => s.bottom <= li.getBoundingClientRect().top + 0.5),
              summary: `${Math.round(s.top)}..${Math.round(s.bottom)}`,
              body: `${Math.round(b.top)}..${Math.round(floor)}`,
              seen: s.top >= b.top - 0.5 && s.bottom <= floor + 0.5
            };
          });
          expect(read !== null, `#${NODE}'s popover drew no "review stale" summary`);
          // Not vacuous: both badges are on, and the rebase cause drawn ahead
          // of the review's is the long one, wrapped.
          expect(read.hasRebaseBadge, `#${NODE}'s popover drew no "needs rebase" badge`);
          expect(read.rebase === LONG_BASE_REASON, `the rebase cause reads "${read.rebase.slice(0, 60)}…"`);
          expect(read.lines > 1, `the rebase cause drew ${read.lines} line(s), so its wrap proves nothing`);
          expect(read.ahead, 'a freshness cause comes before the "review stale" summary');
          expect(read.top === 0, `#${NODE}'s detail opened at scrollTop ${read.top}, not its top`);
          expect(
            read.seen,
            `the "review stale" summary (${read.summary}) is not wholly above the fold (${read.body}) at scrollTop 0`
          );
        },
        size,
        rebaseAndStale
      )
    );
  }

  for (const size of SIZES) {
    const at = `${size.width}x${size.height}`;
    await check(`a 1,000-character title leaves every action-row control on screen at ${at}`, () =>
      visit(
        `${EPIC}?node=${NODE}`,
        async (page) => {
          await opened(page, sleep);
          const title = await page.evaluate(() => {
            const el = document.querySelector('[role="dialog"] .ptitle');
            const body = document.querySelector('[role="dialog"] .pbody');
            if (!el || !body) return null;
            const r = el.getBoundingClientRect();
            const style = getComputedStyle(el);
            return {
              text: el.title,
              clamped: el.scrollHeight > el.clientHeight + 1,
              lines: Math.round(el.clientHeight / parseFloat(style.lineHeight)),
              clamp: style.webkitLineClamp,
              detail: body.clientHeight,
              box: { top: r.top, bottom: r.bottom, left: r.left, right: r.right }
            };
          });
          expect(title !== null, `#${NODE}'s popover drew no title or no detail`);
          // Not vacuous: the served title is the one drawn, and it runs past
          // the clamp, which is what holds it.
          expect(title.text === HUGE_TITLE, `the title reads "${title.text.slice(0, 60)}…"`);
          expect(title.clamped, 'the 1,000-character title was not held to its lines');
          // Two lines, one in a short window — counted as drawn and read off
          // the computed clamp, so neither a looser clamp nor a hard clip
          // passes for it.
          const want = size.height <= 720 ? 1 : 2;
          expect(title.clamp === String(want), `the title's computed line clamp is ${title.clamp}, not ${want}`);
          expect(title.lines === want, `the title draws ${title.lines} line(s), not ${want}`);
          expect(title.detail > 0, `the title left the detail ${title.detail}px high`);
          const read = await readPopover(page);
          expect(read !== null, `no popover opened on ?node=${NODE}`);
          expectSeen(expect, read, 'title', title.box);
          expectSeen(expect, read, 'needs-you sentence', read.why);
          expect(read.controls.length > 0, 'the action row drew no controls');
          for (const control of read.controls) expectSeen(expect, read, `"${control.text}" control`, control);
        },
        size,
        hugeTitle
      )
    );
  }

  for (const size of SIZES) {
    const at = `${size.width}x${size.height}`;
    await check(`Copy leaves the fade in a 56–59.9px detail at ${at}`, () =>
      visit(
        `${EPIC}?node=${NODE}`,
        async (page) => {
          await opened(page, sleep);
          await page.locator('[role="dialog"] .cmd [aria-label="Copy command"]').first().waitFor({ state: 'visible' });
          for (const height of [56, 57, 58, 59, 59.5, 59.9]) {
            // The detail held at `height` — the band a pinned title, sentence
            // and action row leave it in a short window — over the served
            // long command, so it still scrolls and Copy sits mid-detail.
            const held = await page.evaluate(async (h) => {
              const body = document.querySelector('[role="dialog"] .pbody');
              const copy = body?.querySelector('.cmd [aria-label="Copy command"]');
              if (!body || !copy) return null;
              body.style.flex = `0 0 ${h}px`;
              body.style.height = `${h}px`;
              await new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)));
              return {
                height: body.getBoundingClientRect().height,
                more: body.dataset.more === 'true',
                scrolls: body.scrollHeight > body.clientHeight + 1,
                copy: copy.getBoundingClientRect().height
              };
            }, height);
            expect(held !== null, `#${NODE}'s popover drew no detail or no Copy control`);
            // Not vacuous: the detail is the height named, it scrolls, and on
            // the sheet Copy is its 32px, taller than 56px less the fade.
            // Drawn, not rounded: layout keeps 1/64px, so 59.9 draws 59.89.
            expect(Math.abs(held.height - height) < 1 / 32, `the detail is ${held.height}px, not ${height}px`);
            expect(held.scrolls, `the ${height}px detail does not scroll`);
            if (size.width < 1024) expect(held.copy >= 32 - 0.5, `the sheet's Copy is ${held.copy}px, not 32px`);
            // Under 28 + 32px drawn the band would leave Copy less than its
            // height (reachEach's half-pixel slack cannot see 59.5's 31.5px),
            // so the fade stays off there.
            expect(!held.more, `a ${held.height}px detail fades, leaving Copy ${held.height - 28}px above the band`);
            const reached = await reachEach(page);
            expect(reached !== null, `#${NODE}'s popover drew no command`);
            expect(
              !reached.missed.includes('Copy control'),
              `in a ${height}px detail the Copy control never stands wholly clear of the fade`
            );
          }
        },
        size,
        longCommand
      )
    );
  }

  for (const size of SIZES) {
    const at = `${size.width}x${size.height}`;
    await check(`a run link Tabbed to in a faded detail is drawn unmasked at ${at}`, () =>
      visit(
        `${EPIC}?node=${NODE}`,
        async (page) => {
          await opened(page, sleep);
          // The detail held short over the long command, so it fades and the
          // run history sits below its fold; Tab from Copy walks into it. The
          // first run link is short and the two after it long, so the scroll
          // that brings it into view leaves the detail more below (#2948).
          const faded = await page.evaluate(async () => {
            const body = document.querySelector('[role="dialog"] .pbody');
            const copy = body?.querySelector('.cmd [aria-label="Copy command"]');
            if (!body || !copy || body.querySelectorAll('.hrun').length < 3) return null;
            body.style.flex = '0 0 96px';
            body.style.height = '96px';
            body.scrollTop = 0;
            await new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)));
            copy.focus();
            return body.dataset.more === 'true';
          });
          expect(faded !== null, `#${NODE}'s popover drew no detail, Copy control or three run links`);
          expect(faded, 'the 96px detail over the long command does not fade');
          let read = null;
          for (let i = 0; i < 12 && !read; i++) {
            await page.keyboard.press('Tab');
            read = await page.evaluate(async () => {
              const link = document.activeElement;
              if (!link?.matches('[role="dialog"] .pbody .hrun')) return null;
              await new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)));
              const body = link.closest('.pbody');
              const masked = [];
              for (let el = link; el && el !== document.body; el = el.parentElement) {
                const mask = getComputedStyle(el).maskImage;
                if (mask && mask !== 'none') masked.push(el.className);
              }
              return {
                first: link === body.querySelector('.hrun'),
                ring: link.matches(':focus-visible'),
                more: body.dataset.more === 'true',
                masked
              };
            });
          }
          expect(read !== null, 'Tab from Copy never reached a run link in the detail');
          // Not vacuous: the first link holds a keyboard focus and the detail
          // still has more below it, so without the exemption the mask is drawn.
          expect(read.first, 'Tab from Copy reached a run link other than the first');
          expect(read.ring, 'the Tabbed run link does not match :focus-visible');
          expect(read.more, 'the detail no longer fades with the run link focused');
          expect(read.masked.length === 0, `the focused run link sits under a mask on .${read.masked.join(', .')}`);
        },
        size,
        longCommandOverRuns
      )
    );
  }

  await check('a long needs-you sentence leaves the review-stale summary whole at 1440x900', () =>
    visit(
      `${EPIC}?node=${NODE}`,
      async (page) => {
        await opened(page, sleep);
        const read = await readPopover(page);
        expect(read !== null, `no popover opened on ?node=${NODE}`);
        expect(read.whyText === LONG_SENTENCE, `the needs-you sentence reads "${read.whyText}"`);
        // Not vacuous: the sentence takes every line its clamp allows (three,
        // or two in a node area under POPOVER_SHORT_BELOW_H) and is cut there.
        const lines = await page.evaluate(() => {
          const why = document.querySelector('[role="dialog"] .ny .why');
          return why ? Math.round(why.clientHeight / Number.parseFloat(getComputedStyle(why).lineHeight)) : 0;
        });
        expect(lines >= 2, `the needs-you sentence is drawn on ${lines} line(s)`);
        expect(read.whyClamped, 'the needs-you sentence is not held at its clamp');
        expect(read.controls.length > 0, 'the popover drew no action row');
        for (const control of read.controls) expectSeen(expect, read, `"${control.text}" control`, control);
        const stale = await page.evaluate(() => {
          const body = document.querySelector('[role="dialog"] .pbody');
          const word = [...(body?.querySelectorAll('.fresh .fb-word') ?? [])].find(
            (el) => el.textContent?.trim() === 'review stale'
          );
          if (!body || !word) return null;
          // The whole summary line: the badge the word is in, as drawn.
          const line = word.closest('.fsum > *') ?? word;
          const b = body.getBoundingClientRect();
          const s = line.getBoundingClientRect();
          const floor = body.dataset.more === 'true' ? b.bottom - 28 : b.bottom;
          return {
            top: body.scrollTop,
            height: b.height,
            body: `${Math.round(b.top)}..${Math.round(floor)}`,
            summary: `${Math.round(s.top)}..${Math.round(s.bottom)}`,
            seen: s.top >= b.top - 0.5 && s.bottom <= floor + 0.5
          };
        });
        expect(stale !== null, `#${NODE}'s popover drew no review-stale summary`);
        expect(stale.top === 0, `#${NODE}'s detail opened at scrollTop ${stale.top}, not its top`);
        expect(
          stale.seen,
          `the review-stale summary (${stale.summary}) is not whole in the detail (${stale.body}) at scrollTop 0`
        );
        // Two 13px body lines at 1.4, and their padding.
        expect(stale.height >= 45, `the detail is ${stale.height.toFixed(1)}px, under two lines`);
      },
      { width: 1440, height: 900 },
      longSentenceOverStale
    )
  );

  await check('the minimap is not drawn while a popover is open on go-live at 1440x900', () =>
    visit(
      `${GO_LIVE}?node=SFD-229`,
      async (page) => {
        await page.locator('[role="dialog"].pop').first().waitFor({ state: 'visible', timeout: 15_000 });
        await sleep(300);
        expect(
          (await page.locator('.dag .svelte-flow__minimap').count()) === 0,
          'the minimap is drawn under the open popover'
        );
        // Not vacuous: this canvas draws a minimap once the popover closes.
        await page.keyboard.press('Escape');
        await page.locator('[role="dialog"].pop').first().waitFor({ state: 'detached', timeout: 5_000 });
        await page
          .locator('.dag .svelte-flow__minimap')
          .first()
          .waitFor({ state: 'visible', timeout: 5_000 })
          .catch(() => {});
        expect(
          (await page.locator('.dag .svelte-flow__minimap').count()) > 0,
          'go-live draws no minimap at 1440x900 with the popover shut, so its absence proves nothing'
        );
      },
      { width: 1440, height: 900 }
    )
  );

  for (const size of FLOOR_SIZES) {
    const at = `${size.width}x${size.height}`;
    await check(`a two-line title over a wrapped action row leaves the detail its fade room at ${at}`, () =>
      visit(
        `${EPIC}?node=${NODE}`,
        async (page) => {
          // The row may wrap (309px of 290 on the Linux verify host while its
          // arrows drew in a fallback font, #3460; Plex since #3478), and the
          // short box is sized for it wrapped; here it wraps everywhere.
          await page.addStyleTag({ content: '[role="dialog"] .links > :first-child { flex-basis: 100%; }' });
          await opened(page, sleep);
          const read = await page.evaluate(() => {
            const pop = document.querySelector('[role="dialog"].pop');
            const title = pop?.querySelector('.ptitle');
            const body = pop?.querySelector('.pbody');
            const links = pop?.querySelector('.links');
            if (!pop || !title || !body || !links) return null;
            const tops = new Set([...links.children].map((el) => Math.round(el.getBoundingClientRect().top)));
            return {
              title: title.textContent?.trim() ?? '',
              titleLines: Math.round(title.scrollHeight / Number.parseFloat(getComputedStyle(title).lineHeight)),
              rows: tops.size,
              detail: body.getBoundingClientRect().height,
              wants: body.scrollHeight,
              box: pop.getBoundingClientRect().height,
              short: pop.classList.contains('short')
            };
          });
          expect(read !== null, `#${NODE}'s popover drew no title, detail or action row`);
          // Not vacuous: the served title runs to two lines, the row wraps,
          // the box is the short one, and the detail has more than its fade
          // room to show.
          expect(read.title === TWO_LINE_TITLE, `the title reads "${read.title}"`);
          expect(read.titleLines >= 2, `the title runs to ${read.titleLines} line(s), not two`);
          expect(read.rows >= 2, `the action row drew ${read.rows} row(s), not wrapped`);
          expect(read.short, `the ${read.box.toFixed(1)}px box is not drawn short, so the canvas is off its floor`);
          expect(read.wants > FADE_ROOM, `the detail holds only ${read.wants}px, so its height proves nothing`);
          expect(
            read.detail >= FADE_ROOM,
            `the detail is ${read.detail.toFixed(1)}px of the ${read.box.toFixed(1)}px box, under its ${FADE_ROOM}px fade room`
          );
        },
        size,
        (page) => servedNode(page, { title: TWO_LINE_TITLE })
      )
    );
  }
}
