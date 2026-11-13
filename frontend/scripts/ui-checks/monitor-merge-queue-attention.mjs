/**
 * Monitor: the merge queue's attention, read by its reason code (#2982).
 *
 * The only UI evidence for `merge_queue_attention` was a jsdom test reading an
 * inline style token, so CSS could hide the row or repaint its mark and every
 * check would still pass. This drives the real page:
 *
 * 1. Every reason code the queue emits is injected into `/api/audit`. At
 *    1440×960 and 390×844, in the dark and light themes, each row is visible,
 *    leads with its action and draws the repo and PR it is about, none of it
 *    behind an ellipsis; a person's move wears the computed magenta needs-you
 *    mark, and what the queue handles itself (an amended contract, a withdrawn
 *    review) does not. The histogram, served the same events by name, files
 *    the attention under needs-you and the notice under other.
 * 2. A held main is durable: `/api/dashboard` serves a `merge_queue_suspect`
 *    row — the server's projection of `merge_queue_suspects`, with no audit
 *    event anywhere — and the header's count and Monitor's held-main row are
 *    painted in the viewport (not merely attached), and Work draws it as a
 *    needs-you row.
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` discovers it.
 */
// ui-check-owns: src/routes/(factory)/monitor/+page.*
// ui-check-owns: src/lib/monitor/merge-queue-attention.ts
// ui-check-owns: src/lib/monitor/EventStream.svelte
// ui-check-owns: src/lib/monitor/Holds.svelte
// ui-check-owns: src/lib/shell/FactoryHeader.svelte
// ui-check-owns: src/lib/work/list.ts
// ui-check-owns: src/routes/(factory)/work/+page.*
// ui-check-owns: fixtures/factory/audit.json

import { assertPainted } from '../ui-check-lib.mjs';

const PHONE = { width: 390, height: 844 };
const THEMES = ['dark', 'light'];
const REPO = 'kew-dev/kew';

/**
 * The backend's codes, what the row must say (`merge-queue-attention.ts`), and
 * the PR it is about — none for a red main, which is about the repo alone.
 */
const VARIANTS = [
  {
    code: 'main_red',
    id: 2_982_001,
    person: true,
    words: 'main is red — fix main',
    pr: null
  },
  {
    code: 'main_unverified',
    id: 2_982_002,
    person: true,
    words: 'unverified merge — check main',
    pr: 12
  },
  {
    code: 'declaration_edited',
    id: 2_982_003,
    person: true,
    words: 'declaration edited — re-review, then re-add',
    pr: 13
  },
  {
    code: 'head_held',
    id: 2_982_004,
    person: true,
    words: 'release it to verify',
    pr: 14
  },
  {
    code: 'contract_amended',
    id: 2_982_005,
    person: false,
    words: 'acceptance contract amended',
    pr: 15
  },
  {
    code: 'review_withdrawn',
    id: 2_982_006,
    person: false,
    words: 'review withdrawn before the merge',
    pr: 16
  }
];

const eventName = (v) => (v.person ? 'merge_queue_attention' : 'merge_queue_notice');

/** The words naming what the row is about: its repo, and its PR when it has one. */
const targets = (v) => (v.pr === null ? [REPO] : [REPO, `PR #${v.pr}`]);

function auditEvent(v, index) {
  return {
    id: v.id,
    timestamp: new Date(Date.now() - (index + 1) * 30_000).toISOString(),
    event: eventName(v),
    issue_num: null,
    item_id: null,
    run_id: null,
    actor_type: 'policy',
    actor_id: 'merge-queue',
    // `attention_payload`'s shape and key order (merge_queue_attention.py).
    summary: JSON.stringify({
      reason: v.code,
      pr_number: v.pr,
      repo: REPO,
      sha: 'a'.repeat(40),
      message: `kew merge queue: ${v.code}`
    }),
    epic_ref: null
  };
}

/** Prepend every variant to the fixture's audit page, keeping its other rows. */
async function withAttention(page) {
  await page.route(
    (url) => url.pathname === '/api/audit',
    async (route) => {
      const response = await route.fetch();
      const body = await response.json();
      const ids = new Set(VARIANTS.map((v) => v.id));
      body.events = [...VARIANTS.map(auditEvent), ...body.events.filter((e) => !ids.has(e.id))];
      await route.fulfill({ response, json: body });
    }
  );
  // The histogram counts events by name, server-side; serve it the same
  // events: attention in the newest bucket, the notice in the one before.
  await page.route(
    (url) => url.pathname === '/api/audit/histogram',
    async (route) => {
      const q = new URL(route.request().url()).searchParams;
      const since = Date.parse(q.get('since'));
      const until = Date.parse(q.get('until'));
      const bucketS = Number(q.get('bucket_s'));
      const count = Math.max(2, Math.ceil((until - since) / (bucketS * 1000)));
      const persons = VARIANTS.filter((v) => v.person).length;
      const buckets = Array.from({ length: count }, (_, i) => ({
        start: new Date(since + i * bucketS * 1000).toISOString(),
        counts:
          i === count - 1
            ? { merge_queue_attention: persons }
            : i === count - 2
              ? { merge_queue_notice: VARIANTS.length - persons }
              : {}
      }));
      await route.fulfill({
        json: {
          since: new Date(since).toISOString(),
          until: new Date(until).toISOString(),
          bucket_s: bucketS,
          buckets,
          total: VARIANTS.length
        }
      });
    }
  );
}

/**
 * Each variant's row, as drawn: its words, whether it shows, its mark, and
 * whether each of its targets — the repo, the PR — is drawn whole. Runs in the
 * page; `wants` maps a row id to its target strings.
 */
function readRows(wants) {
  const ids = Object.keys(wants);
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
  probe.style.backgroundColor = 'var(--accent)';
  document.body.append(probe);
  const magenta = rgb(getComputedStyle(probe).backgroundColor);
  probe.remove();
  const shown = (el) => {
    if (!el) return false;
    const box = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    return (
      box.width > 0 &&
      box.height > 0 &&
      style.visibility !== 'hidden' &&
      style.display !== 'none' &&
      Number(style.opacity) > 0
    );
  };
  /**
   * Whether `range`, text inside `holder`, is drawn whole: `innerText` keeps
   * text an ellipsis hides, so this reads the drawn boxes. Every line of it
   * must sit inside every box from `holder` up that clips, and inside the
   * viewport; text that wraps must grow its row, not spill into the next one.
   */
  const rangeFit = (holder, range) => {
    const row = holder.closest('li')?.firstElementChild?.getBoundingClientRect();
    const boxes = [...range.getClientRects()].filter((b) => b.width > 0);
    const clips = [];
    for (let el = holder; el && el !== document.body; el = el.parentElement) {
      const style = getComputedStyle(el);
      if (style.overflowX !== 'visible' || style.textOverflow === 'ellipsis') {
        clips.push(el.getBoundingClientRect());
      }
    }
    const inside = (b, c) => b.left >= c.left - 0.5 && b.right <= c.right + 0.5;
    const vw = document.documentElement.clientWidth;
    return {
      whole:
        boxes.length > 0 &&
        boxes.every(
          (b) =>
            b.right <= vw + 0.5 &&
            clips.every((c) => inside(b, c)) &&
            row !== undefined &&
            b.top >= row.top - 0.5 &&
            b.bottom <= row.bottom + 0.5
        ),
      lines: boxes.length,
      room: Math.round(Math.min(vw, ...clips.map((c) => c.right)) - Math.min(...boxes.map((b) => b.left))),
      width: Math.round(Math.max(0, ...boxes.map((b) => b.right)) - Math.min(...boxes.map((b) => b.left)))
    };
  };
  /** The lead whole, and not overflowing its own box either. */
  const leadFit = (lead) => {
    const range = document.createRange();
    range.selectNodeContents(lead);
    const fit = rangeFit(lead, range);
    return { ...fit, whole: fit.whole && lead.scrollWidth <= lead.clientWidth + 1 };
  };
  /**
   * Whether `target` is drawn whole in the row's words. The words after the
   * lead truncate with an ellipsis, so a target there counts only when its own
   * text range is still inside the box cutting them.
   */
  const targetFit = (li, target) => {
    const walker = document.createTreeWalker(li, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const at = node.data.indexOf(target);
      if (at < 0) continue;
      const range = document.createRange();
      range.setStart(node, at);
      range.setEnd(node, at + target.length);
      return rangeFit(node.parentElement, range).whole;
    }
    return false;
  };
  const rows = {};
  for (const id of ids) {
    const row = document.querySelector(`li[data-event-id="${id}"]`);
    const mark = row?.querySelector('span.rounded-full');
    const lead = row?.querySelector('[data-lead]');
    rows[id] = row
      ? {
          text: row.innerText.replace(/\s+/g, ' ').trim(),
          lead: lead ? leadFit(lead) : null,
          hidden: wants[id].filter((target) => !targetFit(row, target)),
          visible: shown(row),
          markVisible: shown(mark),
          mark: mark ? rgb(getComputedStyle(mark).backgroundColor) : null
        }
      : null;
  }
  const bands = [...document.querySelectorAll('i[data-family]')].map((band) => ({
    family: band.getAttribute('data-family'),
    color: rgb(getComputedStyle(band).backgroundColor),
    bucket: band.parentElement?.getAttribute('data-bucket') ?? ''
  }));
  return { magenta, rows, bands };
}

/** Within a hair of `m`, painted at all. */
const same = (c, m) =>
  c !== null &&
  c.a > 0 &&
  Math.max(Math.abs(c.r - m.r), Math.abs(c.g - m.g), Math.abs(c.b - m.b)) <= 12;

/** The needs-you token is magenta in this theme: red and blue well over green. */
const isMagentaHue = (c) => c.r > 150 && c.b > 60 && c.g < 0.5 * Math.min(c.r, c.b);

const themed = (theme, then) => async (page) => {
  await page.addInitScript((t) => localStorage.setItem('kew-theme', t), theme);
  if (then) await then(page);
};

const SUSPECT = {
  type: 'merge_queue_suspect',
  consequence: 'epic',
  age: new Date(Date.now() - 3 * 3_600_000).toISOString(),
  reason:
    'merge queue stopped for 3h 0m — kew-dev/kew — main aaaaaaaaaaaa verified red ' +
    '(test_api failed) after #10 failed on it; inspect with `kew merge queue list`, ' +
    'repair main, then resume',
  sentence:
    'merge queue stopped for 3h 0m — kew-dev/kew — main aaaaaaaaaaaa verified red ' +
    '(test_api failed) after #10 failed on it; inspect with `kew merge queue list`, ' +
    'repair main, then resume',
  primary_action: 'resume',
  command: 'kew merge queue run',
  attention_kind: 'merge_queue_suspect',
  label: '',
  title: 'merge queue stopped — kew-dev/kew',
  issue_num: null,
  item_id: null,
  estimate_usd: null,
  epic_ref: null,
  url: null,
  pr_number: null,
  repo: 'kew-dev/kew'
};

/**
 * Bring `locator` into view vertically, in whichever box scrolls it, then
 * `assertPainted` it where it lands: inside the viewport, unclipped, uncovered
 * and legible. `inline: 'nearest'` leaves a box that fits sideways alone, and
 * `assertPainted` fails any box scrolled sideways, so text laid out off the
 * right edge cannot be carried back on screen and pass.
 */
async function painted(page, locator, what) {
  await locator.evaluate((el) => el.scrollIntoView({ block: 'center', inline: 'nearest' }));
  await assertPainted(page, locator, what);
}

const countOf = async (locator) => Number(((await locator.textContent()) ?? '').trim());

/** `/api/dashboard` as the server serves it with one held main in the set. */
async function withSuspect(page) {
  await page.route(
    (url) => url.pathname === '/api/dashboard',
    async (route) => {
      const response = await route.fetch();
      const body = await response.json();
      body.rows = [SUSPECT, ...body.rows.filter((row) => row.type !== SUSPECT.type)];
      await route.fulfill({ response, json: body });
    }
  );
}

export async function checks({ check, visit, expect, sleep, sizes: { WIDTH, HEIGHT } }) {
  const ids = VARIANTS.map((v) => v.id);
  const wants = Object.fromEntries(VARIANTS.map((v) => [v.id, targets(v)]));
  for (const theme of THEMES) {
    for (const size of [{ width: WIDTH, height: HEIGHT }, PHONE]) {
      const at = `${theme} ${size.width}px`;
      await check(
        `every merge-queue attention reads its reason, magenta only for a person, ${at}`,
        () =>
          visit(
            '/monitor',
            async (page) => {
              const shownTheme = await page.evaluate(() => document.documentElement.dataset.theme);
              expect(shownTheme === theme, `${at}: the page is in the ${shownTheme} theme`);
              for (const id of ids) {
                await page
                  .locator(`li[data-event-id="${id}"]`)
                  .waitFor({ state: 'visible', timeout: 15_000 });
              }
              await page
                .locator('i[data-family]')
                .first()
                .waitFor({ state: 'attached', timeout: 15_000 });
              await sleep(300);
              const read = await page.evaluate(readRows, wants);
              expect(
                isMagentaHue(read.magenta),
                `${at}: --accent is ${read.magenta.css}, not magenta`
              );
              for (const v of VARIANTS) {
                const row = read.rows[v.id];
                expect(row !== null, `${at}: the ${v.code} row is missing`);
                expect(row.visible, `${at}: the ${v.code} row is not visible`);
                expect(row.text.includes(v.words), `${at}: the ${v.code} row reads "${row.text}"`);
                expect(row.lead !== null, `${at}: the ${v.code} row draws no lead`);
                expect(
                  row.lead.whole,
                  `${at}: the ${v.code} action is clipped (${row.lead.width}px in ${row.lead.room}px)`
                );
                expect(
                  row.hidden.length === 0,
                  `${at}: the ${v.code} row does not show ${row.hidden.join(' or ')}: "${row.text}"`
                );
                expect(!row.text.includes(v.code), `${at}: the ${v.code} row prints its raw code`);
                expect(row.markVisible, `${at}: the ${v.code} mark is not visible`);
                expect(
                  same(row.mark, read.magenta) === v.person,
                  `${at}: the ${v.code} mark is ${row.mark?.css}; magenta is ${read.magenta.css}`
                );
              }
              const needsYou = read.bands.filter((band) => band.family === 'needs-you');
              const other = read.bands.filter((band) => band.family === 'other');
              expect(
                needsYou.length === 1,
                `${at}: ${needsYou.length} needs-you band(s) on the histogram`
              );
              expect(
                same(needsYou[0].color, read.magenta),
                `${at}: the needs-you band is ${needsYou[0].color.css}`
              );
              expect(
                other.length === 1 && other[0].bucket !== needsYou[0].bucket,
                `${at}: the notice is not its own other band`
              );
            },
            size,
            themed(theme, withAttention)
          )
      );
    }
  }

  for (const size of [{ width: WIDTH, height: HEIGHT }, PHONE]) {
    const at = `${size.width}px`;
    await check(
      `a held merge-queue main is counted and listed from /api/dashboard at ${at}`,
      async () => {
        const counts = {};
        for (const [label, prepare] of [
          ['without', undefined],
          ['with', withSuspect]
        ]) {
          counts[label] = await visit(
            '/monitor',
            async (page) => {
              // Drawn, not merely in the DOM: `display: none` keeps a node
              // attached and its `textContent` readable (#2982 review).
              const holds = page.locator('section[aria-label="Holds and blocks"] li');
              await holds.first().waitFor({ state: 'visible', timeout: 15_000 });
              const header = page.locator('header [data-needs-you-count]').first();
              const audit = await page.locator('li[data-event-id]').allTextContents();
              const read = {
                header: 0,
                fromAudit: audit.some((t) => t.includes('Merge queue stopped'))
              };
              if (label === 'without') {
                // No counter is drawn over an empty set (#2784): that is 0.
                if ((await header.count()) > 0) read.header = await countOf(header);
                return read;
              }
              await header.waitFor({ state: 'visible', timeout: 15_000 });
              await painted(page, header, `${at}: the header's needs-you count`);
              read.header = await countOf(header);
              const held = holds
                .filter({ hasText: 'kew-dev/kew — main aaaaaaaaaaaa verified red' })
                .locator('div.break-words')
                .first();
              await held.waitFor({ state: 'visible', timeout: 15_000 });
              await painted(page, held, `${at}: Monitor's held-main row`);
              return read;
            },
            size,
            prepare
          );
        }
        expect(!counts.with.fromAudit, `${at}: the fixture audit already holds a queue attention`);
        expect(
          counts.with.header === counts.without.header + 1,
          `${at}: the header reads ${counts.with.header} with a held main, ${counts.without.header} without`
        );
      }
    );

    await check(`Work draws a held merge-queue main as a needs-you row at ${at}`, () =>
      visit(
        '/work',
        async (page) => {
          const row = page.locator(
            'aside[aria-label="Issues"] a[data-item="merge_queue_suspect:kew-dev/kew"]'
          );
          await row.waitFor({ state: 'visible', timeout: 15_000 });
          const read = await row.evaluate((el) => ({
            member: el.hasAttribute('data-needs-you'),
            diamond: el.querySelector('svg rect[transform^="rotate(45"]') !== null,
            text: el.textContent ?? ''
          }));
          expect(read.member, `${at}: the held main is not a needs-you row`);
          expect(read.diamond, `${at}: the held main draws no needs-you diamond`);
          expect(
            read.text.includes('kew-dev/kew'),
            `${at}: the row does not name the repo: "${read.text}"`
          );
        },
        size,
        withSuspect
      )
    );
  }
}
