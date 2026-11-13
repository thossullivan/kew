/**
 * One needs-you set and one wait sentence across surfaces (#2538, #2539).
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 *
 * And one needs-you block everywhere (#3051): the Epic rail's Needs-you column
 * prints the sentence and the command Monitor prints for the same hold; every
 * needs-you DAG card's status row reads `<STATUS> · NEEDS YOU`; and no block on
 * Work says its reason in its head and again in its sentence.
 *
 * And Monitor's MERGE hold reads like every other hold (#3468 D32): its
 * sentence, then a Do this line whose control is the server's PR link, in
 * quiet text with ↗ and never in cyan.
 */
// ui-check-owns: src/lib/needs-you/summary.ts
// ui-check-owns: src/lib/needs-you/block.ts
// ui-check-owns: src/lib/needs-you/NeedsYouBlock.svelte
// ui-check-owns: src/lib/epic/SelectedRail.svelte
// ui-check-owns: src/lib/epic/DagNode.svelte
// ui-check-owns: src/lib/monitor/Holds.svelte
// ui-check-owns: src/lib/work/entry-detail.ts
import { readFileSync } from 'node:fs';
import { EPIC, FRONTEND } from '../ui-check-lib.mjs';

/** The live hold the rail printed as a bare "Held  why ▸" (#3051 N5). */
const DAEMON = 'supervised-daemon';
const DAEMON_NODE = '2814';
const DAEMON_COMMAND = `kew epic unhold ${DAEMON} ${DAEMON_NODE}`;

/**
 * Serve `supervised-daemon` with #2814 held on the round cap, as live served
 * it: the committed go-live epic with its SFD-229 hold renamed, so every read
 * the epic page makes (detail, ticks, timeline, node) answers, and the
 * dashboard's needs-you set carries the same hold for Monitor to print.
 */
async function servedDaemon(page) {
  const rename = (text) =>
    text.replaceAll('go-live-2026-09', DAEMON).replaceAll('SFD-229', DAEMON_NODE);
  /** Every read the fixtures answer is JSON; the epic's stream is held open. */
  const json = (status, body) => ({ status, contentType: 'application/json', body });
  await page.route(`**/api/epics/${DAEMON}**`, async (route) => {
    const url = route
      .request()
      .url()
      .replace(`/api/epics/${DAEMON}`, '/api/epics/go-live-2026-09')
      .replace(`/nodes/${DAEMON_NODE}`, '/nodes/SFD-229');
    if (/\/stream(\?|$)/.test(new URL(url).pathname)) return route.continue();
    const response = await route.fetch({ url });
    await route.fulfill(json(response.status(), rename(await response.text())));
  });
  await page.route('**/api/dashboard', async (route) => {
    const response = await route.fetch();
    const payload = await response.json();
    const held = payload.rows.find((row) => row.item_id === 'SFD-229');
    payload.rows.push({
      ...JSON.parse(rename(JSON.stringify(held))),
      issue_num: Number(DAEMON_NODE),
      reason: held.reason.replace('SFD-229', `#${DAEMON_NODE}`),
      url: null
    });
    await route.fulfill(json(response.status(), JSON.stringify(payload)));
  });
}

/** The Epic rail's Needs-you block for the selected node: sentence and command. */
async function railBlock(page) {
  const block = page
    .locator('section[aria-label^="Selected node"] section[aria-label^="Needs you"]')
    .first();
  await block.waitFor({ state: 'visible', timeout: 15_000 });
  const summary = block.locator('[data-needs-you-summary]').first();
  const command = block.locator('code[data-command]').first();
  return {
    sentence: (await summary.count()) > 0 ? ((await summary.textContent()) ?? '').trim() : '',
    sentenceShown: (await summary.count()) > 0 && (await summary.isVisible()),
    command: (await command.count()) > 0 ? ((await command.textContent()) ?? '').trim() : '',
    commandShown: (await command.count()) > 0 && (await command.isVisible()),
    bare: ((await block.textContent()) ?? '').replace(/\s+/g, ' ').trim()
  };
}

/** The command Monitor's Holds column prints for one item. */
async function monitorCommand(page, item) {
  const row = page.locator(`section[aria-label="Holds and blocks"] li[data-hold-item="${item}"]`);
  await row.first().waitFor({ state: 'attached', timeout: 15_000 });
  const code = row.first().locator('code[data-command]');
  return (await code.count()) > 0 ? ((await code.first().textContent()) ?? '').trim() : '';
}

/**
 * Every drawn needs-you card's status row, as the words a reader SEES: the row
 * is one 14px line and a word that does not fit wraps below it, out of sight
 * (#2543), so a word is read only when it starts on the row's first line.
 */
function needsYouRows(page) {
  return page.evaluate(() =>
    [...document.querySelectorAll('.dag .svelte-flow__node')].flatMap((node) => {
      const card = node.querySelector('.card');
      if (card === null || card.classList.contains('grp')) return [];
      const row = card.querySelector('[data-needs-you-row]');
      const needs = card.classList.contains('needs');
      if (!needs && row === null) return [];
      const segs = row?.querySelector('.segs') ?? null;
      // The row's own box, at whatever zoom the canvas is drawn: a word whose
      // top is at or past its bottom edge is on the hidden second line.
      const bottom = segs?.getBoundingClientRect().bottom ?? 0;
      const words = [...(segs?.querySelectorAll(':scope > .w') ?? [])]
        .filter((w) => w.getBoundingClientRect().top < bottom - 0.5)
        .map((w) => (w.textContent ?? '').replace(/^\s*·\s*/, '').trim().toLowerCase());
      // A needs-you card draws its freshness badges as marks beside the id.
      const badged = card.querySelector('.fb-dot') !== null;
      return [{ id: node.getAttribute('data-id'), needs, built: row !== null, words, badged }];
    })
  );
}

/**
 * A Work needs-you block as a reader sees it with nothing opened: whether its
 * Since row is on screen, what it says, and every visible control filled with
 * the accent (magenta), by its text.
 */
function leadOf(block) {
  return block.evaluate((section) => {
    const probe = document.createElement('span');
    probe.className = 'bg-accent';
    document.body.append(probe);
    const accent = getComputedStyle(probe).backgroundColor;
    probe.remove();
    const shown = (el) => el !== null && el.getClientRects().length > 0 && !el.closest('[hidden]');
    const label = (dt) => (dt.textContent ?? '').trim();
    const since = [...section.querySelectorAll('dt')].find((dt) => label(dt) === 'Since') ?? null;
    const doThis = [...section.querySelectorAll('dt')].find((dt) => label(dt) === 'Do this') ?? null;
    const text = (el) => (el.textContent ?? '').replace('↗', '').replace(/\s+/g, ' ').trim();
    const filled = [...section.querySelectorAll('a, button')]
      .filter((el) => shown(el) && getComputedStyle(el).backgroundColor === accent)
      .map((el) => ({ text: text(el), href: el.getAttribute('href'), tag: el.tagName, height: el.getBoundingClientRect().height }));
    const row = doThis?.parentElement ?? null;
    return {
      sinceShown: shown(since),
      since: since ? text(since.nextElementSibling) : '',
      doThisShown: shown(doThis),
      doThisControls: row ? [...row.querySelectorAll('a, button')].filter(shown).map(text) : [],
      filled
    };
  });
}

/**
 * Serve #2055's freshness row with the head moved past its round 1 verdict.
 * The fixture's own review is current (#3461); a check about a needs-you
 * card that carries a staleness badge serves it one.
 */
function staleReview(page) {
  return page.route('**/api/prs/freshness*', async (route) => {
    const response = await route.fetch();
    const payload = await response.json();
    payload.rows = payload.rows.map((row) =>
      row.item_id === '2055'
        ? {
            ...row,
            latest_head: { ...row.latest_head, sha: 'a1b2c3d4e5f60718293a4b5c6d7e8f9012345678' },
            review_state: 'stale'
          }
        : row
    );
    await route.fulfill({ json: payload });
  });
}

/** The clauses of a sentence, as `say_wait` joins them: `: ` and ` — `. */
function clauses(sentence) {
  return sentence
    .split(/: | — /)
    .map((c) => c.trim().toLowerCase())
    .filter((c) => c !== '');
}

export async function checks({ check, visit, expect, sleep, sizes: { WIDTH, HEIGHT } }) {
  /**
   * #2538 — the epic legend and the header counter read ONE needs-you set.
   *
   * The header counts the served set's rows; the legend counts this epic's
   * bucket of it (`needs_you_by_epic`). A legend counting for itself read
   * `needs you 0` over a go-live banner listing six person actions, so this
   * holds both readings, in a browser, to the committed set they are drawn
   * from — non-zero, so a page that drew neither cannot pass by agreeing on 0.
   */
  const served = JSON.parse(
    readFileSync(new URL('fixtures/factory/dashboard.json', FRONTEND), 'utf8')
  );
  const GO_LIVE = 'go-live-2026-09';
  for (const [width, height] of [
    [WIDTH, HEIGHT],
    [390, 844]
  ]) {
    await check(`the ${GO_LIVE} legend and the header count one set at ${width}px`, () =>
      visit(
        `/epics/${GO_LIVE}`,
        async (page) => {
          const legendEl = page.locator('[data-epic-needs-you]').first();
          const headerEl = page.locator('header [data-needs-you-count]').first();
          await legendEl.waitFor({ state: 'attached', timeout: 15_000 });
          await headerEl.waitFor({ state: 'attached', timeout: 15_000 });
          const legend = Number(((await legendEl.textContent()) ?? '').trim());
          const header = Number(((await headerEl.textContent()) ?? '').trim());
          const bucket = served.rows.filter((row) => row.epic_ref === GO_LIVE).length;
          expect(legend > 0, `${width}px: the ${GO_LIVE} legend reads ${legend}, not a count`);
          expect(header > 0, `${width}px: the header reads ${header}, not a count`);
          expect(
            legend === served.needs_you_by_epic[GO_LIVE] && legend === bucket,
            `${width}px: the legend reads ${legend}; the set files ${bucket} row(s) ` +
              `under ${GO_LIVE} and its map says ${served.needs_you_by_epic[GO_LIVE]}`
          );
          expect(
            header === served.rows.length && legend <= header,
            `${width}px: the header reads ${header} over a set of ${served.rows.length} ` +
              `row(s), ${legend} of them ${GO_LIVE}'s`
          );
        },
        { width, height }
      )
    );
  }

  /**
   * #2539 — one server sentence per wait, drawn the same on every surface.
   *
   * The fixture's #2055 is the wait that read four ways: Work said "kew opens
   * the pull request; you decide whether it lands", the popover and rail "PR
   * open for #2055 — awaiting review". The server now writes ONE sentence on
   * the needs-you row (and the same on the node's attention), and every
   * surface prints it. Read off the committed payload, not typed here, so a
   * reworded server sentence moves this check with it.
   *
   * And the Work list: every row the header counts wears the magenta diamond
   * and sits at the top (D15) — #2055 was a cyan IN REVIEW in the middle of the
   * list while counted in "2 needs you". The diamond is read off the DRAWING
   * (StatusMark's diamond is its one rect rotated 45°), not off a data flag a
   * renderer could set without drawing anything.
   */
  const SENTENCE_ITEM = '2055';
  const sentenceRow = served.rows.find((row) => row.item_id === SENTENCE_ITEM);
  const sentence = sentenceRow?.sentence ?? '';
  for (const [width, height] of [
    [WIDTH, HEIGHT],
    [390, 844]
  ]) {
    const size = { width, height };
    await check(`#${SENTENCE_ITEM} says one sentence on /work, /monitor and the epic popover at ${width}px`, async () => {
      expect(sentence !== '', `the committed set serves no sentence for #${SENTENCE_ITEM}`);
      const said = {};
      said.work = await visit(
        `/work?item=${SENTENCE_ITEM}`,
        async (page) => {
          const block = page.locator('section[aria-label^="Needs you"]').first();
          await block.waitFor({ state: 'attached', timeout: 15_000 });
          return (await block.locator('p').first().textContent())?.trim() ?? '';
        },
        size
      );
      said.monitor = await visit(
        '/monitor',
        async (page) => {
          const holds = page.locator('section[aria-label="Holds and blocks"] li');
          await holds.first().waitFor({ state: 'attached', timeout: 15_000 });
          const texts = await holds.allTextContents();
          const mine = texts.find((t) => t.includes(`#${SENTENCE_ITEM}`)) ?? '';
          return mine.includes(sentence) ? sentence : mine.trim();
        },
        size
      );
      said.popover = await visit(
        EPIC,
        async (page) => {
          const node = page.locator(`.dag .svelte-flow__node[data-id="${SENTENCE_ITEM}"]`);
          await node.first().waitFor({ state: 'attached', timeout: 15_000 });
          const why = page.locator('[role="dialog"] .why');
          for (let attempt = 0; attempt < 3 && (await why.count()) === 0; attempt += 1) {
            await node.first().click();
            await sleep(300);
          }
          expect((await why.count()) > 0, `no popover reason opened on #${SENTENCE_ITEM}`);
          return ((await why.first().textContent()) ?? '').trim();
        },
        size
      );
      for (const [surface, text] of Object.entries(said)) {
        expect(
          text === sentence,
          `${width}px: ${surface} says "${text}", not the server's "${sentence}"`
        );
      }
    });

    await check(`every row the header counts wears the diamond and leads /work at ${width}px`, () =>
      visit(
        '/work',
        async (page) => {
          const headerEl = page.locator('header [data-needs-you-count]').first();
          await headerEl.waitFor({ state: 'attached', timeout: 15_000 });
          const counted = Number(((await headerEl.textContent()) ?? '').trim());
          const rows = page.locator('aside[aria-label="Issues"] a[data-item]');
          await rows.first().waitFor({ state: 'attached', timeout: 15_000 });
          const drawn = await rows.evaluateAll((els) =>
            els.map((el) => ({
              item: el.getAttribute('data-item'),
              member: el.hasAttribute('data-needs-you'),
              diamond: el.querySelector('svg rect[transform^="rotate(45"]') !== null
            }))
          );
          expect(counted > 0, `${width}px: the header reads ${counted}, not a count`);
          const members = drawn.filter((r) => r.member);
          expect(
            members.length === counted,
            `${width}px: ${members.length} row(s) are set members under a header of ${counted}`
          );
          const lead = drawn.slice(0, counted);
          expect(
            lead.every((r) => r.member),
            `${width}px: the first ${counted} rows are ${lead.map((r) => r.item).join(', ')}, ` +
              `not the set's ${members.map((r) => r.item).join(', ')}`
          );
          for (const r of members)
            expect(r.diamond, `${width}px: counted row ${r.item} draws no diamond`);
          for (const r of drawn.filter((x) => !x.member))
            expect(!r.diamond, `${width}px: row ${r.item} is not in the set and draws a diamond`);
        },
        size
      )
    );
  }

  for (const [width, height] of [
    [WIDTH, HEIGHT],
    [390, 844]
  ]) {
    const size = { width, height };

    /**
     * #3051 N5 — the rail's Needs-you column is the shared block: a sentence,
     * "Do this", and the command Monitor prints for the same hold. It printed
     * "Held  why ▸" and nothing else for #2814 and SFD-229. Negative control:
     * with `SelectedRail` back on the title-and-why column, no
     * `section[aria-label^="Needs you"]` is in the rail and both checks fail.
     */
    const held = served.rows.find((row) => row.item_id === 'SFD-229');
    const holds = [
      { epic: GO_LIVE, node: 'SFD-229', command: held?.command ?? '', prepare: undefined },
      { epic: DAEMON, node: DAEMON_NODE, command: DAEMON_COMMAND, prepare: servedDaemon }
    ];
    for (const hold of holds) {
      await check(`the ${hold.epic} rail prints ${hold.node}'s sentence and Monitor's command at ${width}px`, async () => {
        expect(hold.command !== '', `the committed set serves no command for ${hold.node}`);
        const rail = await visit(
          `/epics/${hold.epic}?node=${encodeURIComponent(hold.node)}`,
          railBlock,
          size,
          hold.prepare
        );
        const monitor = await visit('/monitor', (page) => monitorCommand(page, hold.node), size, hold.prepare);
        expect(
          rail.sentenceShown && rail.sentence.split(' ').length >= 3,
          `${width}px: the ${hold.node} rail shows no sentence — it reads "${rail.bare}"`
        );
        expect(
          rail.commandShown && rail.command === hold.command,
          `${width}px: the ${hold.node} rail prints "${rail.command}", not "${hold.command}"`
        );
        expect(
          monitor === rail.command,
          `${width}px: Monitor prints "${monitor}" for ${hold.node}, the rail "${rail.command}"`
        );
      });
    }

    /**
     * #3051 N7 — every needs-you card's status row is built by one function
     * and reads `<STATUS> · NEEDS YOU`. #2055's staleness badge took the row's
     * room and "needs you" wrapped out of sight, leaving `IN REVIEW` alone in
     * magenta. Negative control: with `badgesOnRow` ignoring `needsYou`, #2055
     * reads `in review` alone and this fails. The fixture's #2055 review is
     * current since #3461, so the launch-fixes visit serves it a stale one.
     */
    for (const epic of [EPIC, `/epics/${GO_LIVE}`]) {
      await check(`every needs-you card on ${epic} ends its row in NEEDS YOU at ${width}px`, () =>
        visit(
          epic,
          async (page) => {
            await page
              .locator('.dag .svelte-flow__node .card.needs')
              .first()
              .waitFor({ state: 'attached', timeout: 15_000 });
            const rows = await needsYouRows(page);
            expect(rows.length > 0, `${width}px: ${epic} draws no needs-you card`);
            if (epic === EPIC) {
              const stale = rows.find((row) => row.id === '2055');
              expect(stale?.badged, `${width}px: #2055 drew no staleness badge, so its row proves nothing`);
            }
            for (const row of rows) {
              expect(
                row.needs && row.built,
                `${width}px: card ${row.id} is ${row.needs ? '' : 'not '}a needs-you card ` +
                  `and ${row.built ? '' : 'not '}built as one`
              );
              expect(
                row.words.length === 2 && row.words[1] === 'needs you',
                `${width}px: card ${row.id}'s status row reads "${row.words.join(' · ')}"`
              );
            }
          },
          size,
          epic === EPIC ? staleReview : undefined
        )
      );
    }

    /**
     * #3051 N8 — a needs-you block on Work says its reason once: the head
     * names the kind ("Held", "Merge decision") and the sentence the reason.
     * SFD-229's head read "the review round cap was reached" over a sentence
     * opening with it. Negative control: with `entryBlock`'s label back on
     * `words.label`, SFD-229 fails.
     */
    for (const row of served.rows.filter((r) => (r.item_id ?? '') !== '')) {
      await check(`#3051 the ${row.item_id} block on Work says its reason once at ${width}px`, () =>
        visit(
          `/work?item=${encodeURIComponent(row.item_id)}`,
          async (page) => {
            const block = page.locator('section[aria-label^="Needs you"]').first();
            await block.waitFor({ state: 'attached', timeout: 15_000 });
            const kind = block.locator('[data-needs-you-kind]');
            const head = (await kind.count()) > 0 ? ((await kind.first().textContent()) ?? '').trim() : '';
            const sentence = (
              (await block.locator('[data-needs-you-summary]').first().textContent()) ?? ''
            ).trim();
            expect(sentence !== '', `${width}px: the ${row.item_id} block has no sentence`);
            const twice = clauses(sentence).filter((c) => head.toLowerCase().includes(c));
            expect(
              twice.length === 0,
              `${width}px: the ${row.item_id} head "${head}" repeats "${twice.join('", "')}" ` +
                `from its sentence "${sentence}"`
            );
          },
          size
        )
      );
    }
  }

  /**
   * #3467 D24 — every Work needs-you block says since when without a click,
   * and a merge decision's Do this is the server's PR link, filled as the one
   * magenta control. #2055 printed Since only behind `why ▸` and drew its PR
   * link as a quiet button under the block; SFD-229's command keeps the
   * magenta (its Run). Read off the committed rows, so the PR link is the
   * server's own label and href. Negative control: on the old block the Since
   * row is hidden at load, #2055 has no Do this row and nothing on it is
   * filled.
   *
   * Design review c9 D10: #2055's merge verify failed at its PR's head, so its
   * one magenta control is the server's link to that failure, and the PR link
   * stays in Do this, quiet, after it. Negative control: on the D24 block the
   * filled control is the PR link.
   */
  const MERGE = served.rows.find((row) => row.item_id === '2055');
  const soleMerge = (page) =>
    page.route(
      (url) => url.pathname === '/api/dashboard',
      async (route) => {
        const response = await route.fetch();
        const payload = await response.json();
        payload.rows = payload.rows.filter((row) => row.item_id === '2055');
        await route.fulfill({ response, json: payload });
      }
    );
  const mergeLead = (where, width, lead) => {
    expect(lead.sinceShown && /^waiting \S+$/.test(lead.since), `${width}px ${where}: Since is not shown at load ("${lead.since}")`);
    expect(lead.doThisShown, `${width}px ${where}: the merge decision has no Do this row`);
    for (const label of [MERGE.verify_link.label, MERGE.link.label]) {
      expect(
        lead.doThisControls.includes(label),
        `${width}px ${where}: Do this draws [${lead.doThisControls.join(' | ')}], not "${label}"`
      );
    }
    expect(
      lead.filled.length === 1 &&
        lead.filled[0].href === MERGE.verify_link.href &&
        lead.filled[0].text === MERGE.verify_link.label,
      `${width}px ${where}: the magenta controls are ${JSON.stringify(lead.filled)}, not the failed verify alone`
    );
    expect(lead.filled[0]?.height >= 32, `${width}px ${where}: the verify link is ${lead.filled[0]?.height}px tall`);
  };
  for (const [width, height] of [
    [390, 844],
    [768, 1024],
    [WIDTH, HEIGHT]
  ]) {
    const size = { width, height };
    await check(`#2055's block on Work shows Since and the failed verify as its magenta Do this at ${width}px`, () =>
      visit(
        '/work?item=2055',
        async (page) => {
          const block = page.locator('main section[aria-label^="Needs you"]').first();
          await block.waitFor({ state: 'visible', timeout: 15_000 });
          mergeLead('/work?item=2055', width, await leadOf(block));
        },
        size
      )
    );
    await check(`SFD-229's block on Work shows Since and keeps the magenta on its command at ${width}px`, () =>
      visit(
        '/work?item=SFD-229',
        async (page) => {
          const block = page.locator('main section[aria-label^="Needs you"]').first();
          await block.waitFor({ state: 'visible', timeout: 15_000 });
          const lead = await leadOf(block);
          expect(lead.sinceShown && /^held \S+$/.test(lead.since), `${width}px: SFD-229's Since is not shown at load ("${lead.since}")`);
          expect(
            lead.filled.length === 1 && lead.filled[0].tag === 'BUTTON',
            `${width}px: SFD-229's magenta controls are ${JSON.stringify(lead.filled)}, not its command's one button`
          );
          const near = await block.locator('code[data-command]').first().textContent();
          expect((near ?? '').trim() === served.rows.find((r) => r.item_id === 'SFD-229').command, `${width}px: SFD-229 prints "${near}"`);
        },
        size
      )
    );
  }
  // The sole-wait pane is drawn beside the list from 1024 up; below it the
  // list holds the one column until a row is chosen.
  for (const [width, height] of [
    [1024, 768],
    [WIDTH, HEIGHT]
  ]) {
    await check(`the sole-wait pane's #2055 block shows Since and the failed verify as its magenta Do this at ${width}px`, () =>
      visit(
        '/work',
        async (page) => {
          const block = page.locator('main section[aria-label="Needs you"] section[aria-label^="Needs you"]').first();
          await block.waitFor({ state: 'visible', timeout: 15_000 });
          mergeLead('sole-wait pane', width, await leadOf(block));
        },
        { width, height },
        soleMerge
      )
    );
  }

  /**
   * Design review c9 D12 — an approval hold names its item, then the action in
   * plain words. It read "awaiting approval — no history — assuming per-issue
   * cap of $25.00": the basis string, raw, with no item. `/api/dashboard` is
   * served an approval row as `kew.service.attention.say_wait` now writes it
   * (the reason unchanged, the sentence the action). Negative control: on the
   * old Holds column a row with no epic printed its sentence alone, with no
   * `#114` before it.
   */
  const APPROVAL_SENTENCE =
    'approve a run estimated at $25.00, the per-issue cap; no cost history to estimate from';
  const withApproval = (page) =>
    page.route(
      (url) => url.pathname === '/api/dashboard',
      async (route) => {
        const response = await route.fetch();
        const payload = await response.json();
        payload.rows.unshift({
          type: 'approval_hold',
          consequence: 'money',
          // 3h05m, so the page's own clock, read before this answer lands,
          // still words it "held 3h…" rather than "held 2h59m".
          age: new Date(Date.now() - (3 * 60 + 5) * 60_000).toISOString(),
          reason: 'awaiting approval — no history — assuming per-issue cap of $25.00',
          primary_action: 'approve',
          issue_num: 114,
          item_id: '114',
          estimate_usd: 25,
          epic_ref: null,
          url: null,
          pr_number: null,
          work_unread: false,
          attention_kind: 'approval',
          label: '',
          command: '',
          sentence: APPROVAL_SENTENCE,
          title: 'Rebuild the balances cache from the journal',
          link: null,
          node_ids: []
        });
        await route.fulfill({ response, json: payload });
      }
    );
  for (const size of [
    { width: WIDTH, height: HEIGHT },
    { width: 390, height: 844 }
  ]) {
    await check(`an approval hold names its item, then the action, on Monitor and Work at ${size.width}px`, () =>
      visit(
        '/monitor',
        async (page) => {
          const row = page.locator('section[aria-label="Holds and blocks"] li[data-hold-item="114"]').first();
          await row.waitFor({ state: 'visible', timeout: 15_000 });
          const said = ((await row.textContent()) ?? '').replace(/\s+/g, ' ');
          expect(said.includes(`#114 — ${APPROVAL_SENTENCE} · held 3h`), `${size.width}px: the hold reads "${said.trim()}"`);
          expect(!said.includes('no history'), `${size.width}px: the hold still prints the raw basis: "${said.trim()}"`);
          await page.goto(page.url().replace('/monitor', '/work'), { waitUntil: 'load' });
          const item = page.locator('aside[aria-label="Issues"] a[data-item="114"]').first();
          await item.waitFor({ state: 'visible', timeout: 15_000 });
          const line = ((await item.locator('[data-row-reason]').first().textContent()) ?? '').replace(/\s+/g, ' ').trim();
          expect(line === APPROVAL_SENTENCE, `${size.width}px: Work's line under #114 reads "${line}"`);
        },
        size,
        withApproval
      )
    );
  }

  /**
   * #3460 — every control in the rail's Needs-you block stays inside its
   * bordered panel. The column is about 230px wide at 1440, and a nowrap
   * button wider than that ran past the panel's right border: #2055's "See
   * the failed merge verify" ended 25px outside it. A short window folds the
   * rail, so the check opens it first. Negative control: with
   * the compact block's buttons back on `whitespace-nowrap` and no
   * `max-w-full`, this fails at both sizes.
   */
  for (const size of [
    { width: 1440, height: 960 },
    { width: 1440, height: 560 }
  ]) {
    const at = `${size.width}x${size.height}`;
    await check(`every control in #2055's rail Needs-you block stays inside its panel at ${at}`, () =>
      visit(
        `${EPIC}?node=2055`,
        async (page) => {
          await page.locator('section[aria-label^="Selected node #"]').first().waitFor({ state: 'visible', timeout: 15_000 });
          // A short window folds the rail behind one disclosure: open it, as a
          // reader there does, so the block is measured where it is read.
          const disclosure = page.locator('button[aria-controls="selected-rail-detail"]');
          if ((await disclosure.count()) > 0 && (await disclosure.getAttribute('aria-expanded')) !== 'true') {
            await disclosure.click();
            await sleep(250);
          }
          const panel = page
            .locator('section[aria-label^="Selected node"] section[aria-label^="Needs you"]')
            .first();
          await panel.waitFor({ state: 'visible', timeout: 15_000 });
          const read = await panel.evaluate((el) => {
            const box = el.getBoundingClientRect();
            const style = getComputedStyle(el);
            // Inside the border, not on it: a control over the border line has
            // already crossed the panel's edge.
            const left = box.left + parseFloat(style.borderLeftWidth);
            const right = box.right - parseFloat(style.borderRightWidth);
            const controls = [...el.querySelectorAll('a, button')].map((c) => {
              const r = c.getBoundingClientRect();
              return { text: (c.textContent ?? '').replace(/\s+/g, ' ').trim(), left: r.left, right: r.right, height: r.height };
            });
            return { left, right, controls };
          });
          expect(read.controls.length > 0, `${at}: the rail's Needs-you block drew no control`);
          for (const c of read.controls) {
            expect(
              c.left >= read.left - 0.5 && c.right <= read.right + 0.5,
              `${at}: "${c.text}" runs ${Math.round(c.left)}..${Math.round(c.right)}, ` +
                `outside the panel's ${Math.round(read.left)}..${Math.round(read.right)}`
            );
          }
        },
        size
      )
    );
  }

  /**
   * #3468 D32 — the MERGE hold on Monitor has a Do this line, and its link is
   * not cyan. The row used to end in a bare `text-accent-2` link: cyan marks
   * no link anywhere else (DESIGN.md Signal), and every other hold row gives
   * its move a line of its own.
   */
  for (const size of [
    { width: WIDTH, height: HEIGHT },
    { width: 390, height: 844 }
  ]) {
    await check(`Monitor's MERGE hold has a Do this line with a link that is not cyan at ${size.width}px`, () =>
      visit(
        '/monitor',
        async (page) => {
          const row = page.locator('section[aria-label="Holds and blocks"] li[data-hold-item="2055"]').first();
          await row.waitFor({ state: 'visible', timeout: 15_000 });
          const read = await row.evaluate((li) => {
            const probe = document.createElement('span');
            probe.style.color = 'var(--accent-2)';
            li.append(probe);
            const cyan = getComputedStyle(probe).color;
            probe.remove();
            const word = (li.children[0]?.textContent ?? '').replace(/\s+/g, ' ').trim();
            // The line that says "Do this", and every link drawn on it.
            const line = [...li.querySelectorAll('*')].find(
              (el) => el.children.length > 0 && el.children[0].textContent.trim() === 'Do this'
            );
            const links = line
              ? [...line.querySelectorAll('a')].map((a) => {
                  const r = a.getBoundingClientRect();
                  return {
                    text: a.textContent.replace(/\s+/g, ' ').trim(),
                    color: getComputedStyle(a).color,
                    height: r.height,
                    target: a.getAttribute('target')
                  };
                })
              : [];
            return { word, cyan, hasLine: line !== undefined, links };
          });
          expect(/merge/i.test(read.word), `${size.width}px: #2055's hold is a "${read.word}" row, not MERGE`);
          expect(read.hasLine, `${size.width}px: the MERGE hold has no Do this line`);
          const pr = read.links.find((l) => l.text.startsWith('Open PR #2059 to merge'));
          expect(pr !== undefined, `${size.width}px: the Do this line links ${JSON.stringify(read.links.map((l) => l.text))}`);
          expect(pr.text.endsWith('↗') && pr.target === '_blank', `${size.width}px: the PR link reads "${pr.text}", not an external link`);
          for (const link of read.links) {
            expect(link.color !== read.cyan, `${size.width}px: "${link.text}" is drawn in --accent-2 (${link.color})`);
            if (size.width < 640) {
              expect(link.height >= 32, `${size.width}px: "${link.text}" is ${link.height}px tall`);
            }
          }
        },
        size
      )
    );
  }
}
