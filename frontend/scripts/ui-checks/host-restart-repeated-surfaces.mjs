/**
 * A node the host restarted under twice asks a person once, the same way on
 * every surface (#2971).
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 *
 * #2812 made a second host death under one node the one terminal failure that
 * asks a person: `host_restart_repeated`, with `kew epic retry` as its move.
 * No committed payload carried such a node, so no browser had drawn it. The
 * `*-launch-fixes-host-restart-repeated` scenarios are the committed set with
 * #2058 failed that way, the row and the attention composed by the server.
 * (`host-restart-repeated.mjs` draws the clean and dirty rows #2964 serves for
 * a lone node; this check draws one inside the whole committed set.) Work, Monitor
 * and the epic popover must each draw one needs-you action for it: a magenta
 * marker, the server's sentence, and the retry command with a Copy beside it —
 * at desktop and phone widths. On Work and Monitor that action is the only one
 * the whole page draws for the command. On the epic the selected-node rail is
 * a surface too, as `host-restart-preserved-continue.mjs` already says for
 * #1010: since #3051 it draws the shared needs-you block for a failed node, as
 * it did for a held one, so the popover holds exactly one Copy for the
 * command, the rail at most one, and no other part of the page any. The
 * surface's Copy and command sit inside the viewport once scrolled to, the
 * page never scrolls sideways, and no page error is thrown; only then is the
 * Copy clicked, and it puts exactly the retry command on the clipboard.
 *
 * The marker is the magenta diamond — shape and colour, its fill read against
 * `--accent` — on all three. Nothing holds the node, it failed, so the server
 * stamps its own `host_restart_repeated` kind rather than `held`, and that kind
 * is what each surface draws the diamond from; Monitor keeps drawing every
 * kind's mark off the kind the server stamped (#2280 D14).
 *
 * #2058 failed, so it is in no frontier and no ready queue: the scenario says
 * so, and Work draws it as the attention's own row, not an idle queue row the
 * attention was merged onto.
 *
 * The sentence is plain words, never the machine reason it was composed from
 * (DESIGN.md: a raw reason code is never shown as a sentence). The reason,
 * `failed-terminal (exit 137, host_restart_repeated)`, is secondary detail at
 * most — Work's folded `Why` row — so no surface's prose may carry
 * `failed-terminal` or any snake_case token. The prose is the surface's text
 * less its secondary parts: the command, Work's `Why` detail and the
 * popover's run history.
 */
// ui-check-owns: fixtures/scenarios/*-launch-fixes-host-restart-repeated.json
// ui-check-owns: src/lib/needs-you/kind.ts
// ui-check-owns: src/routes/(factory)/work/+page.svelte
// ui-check-owns: src/lib/monitor/Holds.svelte
// ui-check-owns: src/lib/epic/NodePopover.svelte
// ui-check-owns: src/lib/epic/SelectedRail.svelte
import { readFileSync } from 'node:fs';
import { EPIC, FRONTEND } from '../ui-check-lib.mjs';

const ITEM = '2058';

const read = (name) => readFileSync(new URL(`fixtures/scenarios/${name}`, FRONTEND), 'utf8');
const DASHBOARD = read('dashboard-launch-fixes-host-restart-repeated.json');
const EPIC_DETAIL = read('epics-launch-fixes-host-restart-repeated.json');
/** A machine reason token: the bucket key, or any snake_case word. */
const MACHINE_TOKEN = /failed-terminal|\b[a-z][a-z0-9]*_[a-z0-9_]+\b/g;
/**
 * What a surface may draw beside its prose and still say a code in: the
 * command, Work's `Why` detail list (`NeedsYouBlock`), the popover's runs.
 */
const SECONDARY = ['[data-command]', 'dl[id^="needs-you-why-"]', '[data-history]'].join(', ');
/** Every machine token in *text*, so a failure names what leaked. */
const machineTokens = (text) => text.match(MACHINE_TOKEN) ?? [];

const ROW = JSON.parse(DASHBOARD).rows.find((row) => row.item_id === ITEM);
const READY_QUEUE = JSON.parse(DASHBOARD).glance.ready_queue ?? [];
const SNAPSHOT = JSON.parse(EPIC_DETAIL).snapshot;
const NODE = SNAPSHOT.nodes.find((n) => n.id === ITEM);
const ATTENTION = NODE.attention;

/** Serve the dashboard and the epic detail as the repeated-restart scenario. */
async function routeScenario(page) {
  for (const [path, body] of [
    ['/api/dashboard', DASHBOARD],
    [`/api${EPIC}`, EPIC_DETAIL]
  ]) {
    await page.route(
      (url) => url.pathname === path,
      (route) => route.fulfill({ status: 200, contentType: 'application/json', body })
    );
  }
}

/**
 * Whether a diamond under *selector* is painted `--accent` (magenta) — read
 * off the drawing, StatusMark's diamond being its rect rotated 45°, and the
 * colour resolved from the token itself so a fill that merely looks pink
 * cannot pass.
 */
function paintsDiamond(page, selector) {
  return page.evaluate((selector) => {
    const probe = document.createElement('span');
    probe.style.color = 'var(--accent)';
    document.body.append(probe);
    const accent = getComputedStyle(probe).color;
    probe.remove();
    const root = document.querySelector(selector);
    const rects = root === null ? [] : [...root.querySelectorAll('svg rect[transform^="rotate(45"]')];
    return { accent, diamond: rects.some((el) => getComputedStyle(el).fill === accent) };
  }, selector);
}

/**
 * What one surface drew under *selector*: how many such surfaces the page
 * shows, their commands, Copy buttons and text, and every action on the WHOLE
 * page — a visible Copy labelled *copyLabel* beside a command reading
 * *command* — so a duplicate drawn outside the surface still counts. The
 * actions are split into the surface's, those inside a *companion* surface
 * (the epic's selected-node rail; none elsewhere), and the rest. The
 * surface's own action is marked `data-ui-check-action` for the box reading
 * and the click.
 */
function drawn(page, selector, copyLabel, command, companion) {
  return page.evaluate(
    ([selector, copyLabel, command, secondary, companion]) => {
      const visible = (el) => el.checkVisibility({ visibilityProperty: true, opacityProperty: true });
      const roots = [...document.querySelectorAll(selector)].filter(visible);
      if (roots.length === 0) return null;
      const root = roots[0];
      const labelled = (el) => (el.getAttribute('aria-label') ?? '').startsWith(copyLabel);
      const commandBeside = (button) => {
        let scope = button.parentElement;
        while (scope !== null && scope.querySelector('[data-command]') === null) {
          scope = scope.parentElement;
        }
        return scope?.querySelector('[data-command]') ?? null;
      };
      const actions = [...document.querySelectorAll('button[aria-label]')].filter(
        (el) => labelled(el) && visible(el) && (commandBeside(el)?.textContent ?? '').trim() === command
      );
      const companions = companion === null ? [] : [...document.querySelectorAll(companion)];
      const inSurface = actions.filter((el) => root.contains(el));
      const inCompanion = actions.filter(
        (el) => !root.contains(el) && companions.some((c) => c.contains(el))
      );
      if (inSurface.length === 1) inSurface[0].setAttribute('data-ui-check-action', '');
      return {
        roots: roots.length,
        actions: actions.length,
        inSurface: inSurface.length,
        inCompanion: inCompanion.length,
        elsewhere: actions.length - inSurface.length - inCompanion.length,
        commands: [...root.querySelectorAll('[data-command]')].map((el) => (el.textContent ?? '').trim()),
        copies: [...root.querySelectorAll('button[aria-label]')].filter(labelled).length,
        text: (root.textContent ?? '').replace(/\s+/g, ' ').trim(),
        prose: (() => {
          const copy = root.cloneNode(true);
          for (const el of copy.querySelectorAll(secondary)) el.remove();
          return (copy.textContent ?? '').replace(/\s+/g, ' ').trim();
        })()
      };
    },
    [selector, copyLabel, command, SECONDARY, companion]
  );
}

/**
 * Where the one action sits once scrolled to: its Copy's and its command's
 * boxes against the viewport, and how wide the page scrolls. The boxes are
 * read only after the scroll settles, so a smooth scroll is not caught midway.
 */
async function placed(page, sleep) {
  await page.locator('[data-ui-check-action]').scrollIntoViewIfNeeded();
  await sleep(200);
  return page.evaluate(() => {
    const width = document.documentElement.clientWidth;
    const height = window.innerHeight;
    const inside = (el) => {
      const box = el?.getBoundingClientRect();
      return (
        box !== undefined &&
        box.width > 0 &&
        box.height > 0 &&
        box.left >= -0.5 &&
        box.top >= -0.5 &&
        box.right <= width + 0.5 &&
        box.bottom <= height + 0.5
      );
    };
    const button = document.querySelector('[data-ui-check-action]');
    let scope = button.parentElement;
    while (scope !== null && scope.querySelector('[data-command]') === null) {
      scope = scope.parentElement;
    }
    return {
      copyInside: inside(button),
      commandInside: inside(scope?.querySelector('[data-command]')),
      scrollWidth: document.documentElement.scrollWidth,
      viewport: width
    };
  });
}

/**
 * Click the one action's Copy and read back what it put on the clipboard.
 * The page's context is granted the clipboard first, so a denied permission
 * cannot pass for a button that copies nothing.
 */
async function copied(page) {
  await page.context().grantPermissions(['clipboard-read', 'clipboard-write']);
  await page.evaluate(() => navigator.clipboard.writeText(''));
  await page.locator('[data-ui-check-action]').click();
  for (let attempt = 0; attempt < 20; attempt += 1) {
    const text = await page.evaluate(() => navigator.clipboard.readText());
    if (text !== '') return text;
    await page.waitForTimeout(100);
  }
  return '';
}

/**
 * What a surface drew, where its one action sits, and — only then, and only
 * when the surface holds exactly one — what its Copy put on the clipboard.
 * Page errors are read last, so one the click throws counts too.
 */
async function surveyed(page, problems, sleep, { selector, copyLabel, command, diamondAt, companion }) {
  const seen = await drawn(page, selector, copyLabel, command, companion ?? null);
  if (seen === null) return null;
  const { accent, diamond } = await paintsDiamond(page, diamondAt ?? selector);
  if (seen.inSurface !== 1) return { ...seen, accent, diamond, clipboard: '', errors: [] };
  const where = await placed(page, sleep);
  const clipboard = await copied(page);
  const errors = problems.filter((problem) => problem.startsWith('page error:'));
  return { ...seen, ...where, accent, diamond, clipboard, errors };
}

export async function checks({ check, visit, expect, sleep, sizes: { WIDTH, HEIGHT } }) {
  const sentence = ROW?.sentence ?? '';
  const command = ROW?.command ?? '';

  await check('the repeated host restart scenario is the one wait the server composes', () => {
    expect(sentence !== '', `the scenario serves no sentence for #${ITEM}`);
    expect(
      ROW?.reason.includes('host_restart_repeated'),
      `the row's reason "${ROW?.reason}" no longer carries the code this check keeps out of prose`
    );
    expect(command === `kew epic retry launch-fixes ${ITEM}`, `the row's command is "${command}"`);
    expect(ATTENTION?.sentence === sentence, `the node says "${ATTENTION?.sentence}"`);
    expect(ATTENTION?.command === command, `the node's command is "${ATTENTION?.command}"`);
  });

  await check(`#${ITEM} failed and nothing could dispatch it again`, () => {
    expect(NODE.status === 'failed', `the node's status is "${NODE.status}", not failed`);
    expect(
      !SNAPSHOT.frontier.includes(ITEM),
      `the snapshot's frontier ${JSON.stringify(SNAPSHOT.frontier)} offers the failed #${ITEM} for dispatch`
    );
    expect(
      !READY_QUEUE.some((issue) => String(issue.item_id ?? issue.issue_num) === ITEM),
      `the dashboard's ready queue offers the failed #${ITEM} for dispatch`
    );
  });

  await check('the needs-you sentence excludes machine reason tokens', () => {
    for (const [who, said] of [
      ['row', sentence],
      ['node', ATTENTION?.sentence ?? '']
    ]) {
      expect(
        machineTokens(said).length === 0,
        `the ${who}'s sentence "${said}" carries ${JSON.stringify(machineTokens(said))}`
      );
    }
  });

  for (const [width, height] of [
    [WIDTH, HEIGHT],
    [390, 844]
  ]) {
    const size = { width, height };
    const surfaces = {};
    await check(`#${ITEM}'s repeated host restart asks once on /work at ${width}px`, async () => {
      surfaces.work = await visit(
        `/work?item=${ITEM}`,
        async (page, problems) => {
          const block = page.locator('section[aria-label^="Needs you"]').first();
          await block.waitFor({ state: 'attached', timeout: 15_000 });
          // Nothing but the attention draws #2058 on Work: it is in no queue,
          // so the row is the needs-you entry's own, reading `needs you`, and
          // never an idle queue row the attention was merged onto.
          const row = page.locator(`aside[aria-label="Issues"] a[data-item="${ITEM}"]`);
          expect((await row.count()) === 1, `${width}px: Work lists ${await row.count()} row(s) for #${ITEM}`);
          const state = ((await row.first().locator('span').last().textContent()) ?? '').trim();
          expect(
            /^needs you$/i.test(state) && (await row.first().getAttribute('data-needs-you')) === '',
            `${width}px: Work's #${ITEM} row reads "${state}", not the attention-only needs-you row`
          );
          return surveyed(page, problems, sleep, {
            selector: 'section[aria-label^="Needs you"]',
            copyLabel: 'Copy command',
            command,
            diamondAt: `aside[aria-label="Issues"] a[data-item="${ITEM}"]`
          });
        },
        size,
        routeScenario
      );
    });
    await check(`#${ITEM}'s repeated host restart asks once on /monitor at ${width}px`, async () => {
      surfaces.monitor = await visit(
        '/monitor',
        async (page, problems) => {
          const holds = page.locator('section[aria-label="Holds and blocks"] li');
          await holds.first().waitFor({ state: 'attached', timeout: 15_000 });
          const mine = holds.filter({ hasText: `#${ITEM}` });
          expect(
            (await mine.count()) === 1,
            `${width}px: Monitor draws ${await mine.count()} hold row(s) for #${ITEM}`
          );
          await mine.first().evaluate((el) => el.setAttribute('data-ui-check-row', ''));
          return surveyed(page, problems, sleep, {
            selector: '[data-ui-check-row]',
            copyLabel: 'Copy command for',
            command
          });
        },
        size,
        routeScenario
      );
    });
    await check(`#${ITEM}'s repeated host restart asks once in the epic popover at ${width}px`, async () => {
      surfaces.popover = await visit(
        EPIC,
        async (page, problems) => {
          const node = page.locator(`.dag .svelte-flow__node[data-id="${ITEM}"]`);
          await node.first().waitFor({ state: 'attached', timeout: 15_000 });
          const why = page.locator('[role="dialog"] .why');
          for (let attempt = 0; attempt < 3 && (await why.count()) === 0; attempt += 1) {
            await node.first().click();
            await sleep(300);
          }
          expect((await why.count()) > 0, `${width}px: no popover opened on #${ITEM}`);
          await why.first().evaluate((el) =>
            el.closest('[role="dialog"]').setAttribute('data-ui-check-row', '')
          );
          // The selected-node rail is the epic's other surface for the same
          // wait (#3051): it may hold the action once beside the popover's.
          return surveyed(page, problems, sleep, {
            selector: '[data-ui-check-row]',
            copyLabel: 'Copy command',
            command,
            companion: 'section[aria-label^="Selected node"]'
          });
        },
        size,
        routeScenario
      );
    });
    for (const [surface, seen] of Object.entries(surfaces)) {
      await check(`${surface} draws one magenta retry for #${ITEM} at ${width}px`, () => {
        expect(seen !== null && seen !== undefined, `${width}px: ${surface} drew nothing`);
        expect(
          seen.diamond === true,
          `${width}px: ${surface} draws no diamond filled --accent (${seen.accent}) for #${ITEM}`
        );
        expect(
          seen.text.includes(sentence),
          `${width}px: ${surface} does not say "${sentence}": "${seen.text.slice(0, 200)}"`
        );
        expect(
          seen.prose.includes(sentence) && machineTokens(seen.prose).length === 0,
          `${width}px: ${surface}'s prose carries ${JSON.stringify(machineTokens(seen.prose))}: "${seen.prose.slice(0, 200)}"`
        );
        expect(
          seen.commands.length === 1 && seen.commands[0] === command,
          `${width}px: ${surface} draws commands ${JSON.stringify(seen.commands)}, not one "${command}"`
        );
        expect(seen.copies === 1, `${width}px: ${surface} draws ${seen.copies} Copy buttons`);
        expect(seen.roots === 1, `${width}px: the page draws ${seen.roots} ${surface} surfaces`);
        expect(
          seen.inSurface === 1,
          `${width}px: ${surface} draws ${seen.inSurface} Copy actions for "${command}", not one`
        );
        expect(
          seen.inCompanion <= 1,
          `${width}px: the selected-node rail draws ${seen.inCompanion} Copy actions for "${command}", not at most one`
        );
        expect(
          seen.elsewhere === 0,
          `${width}px: the page draws ${seen.actions} Copy actions for "${command}", ${seen.elsewhere} outside ${surface}` +
            (surface === 'popover' ? ' and the selected-node rail' : '')
        );
        expect(seen.copyInside === true, `${width}px: ${surface}'s Copy is not inside the viewport`);
        expect(seen.commandInside === true, `${width}px: ${surface}'s command is not inside the viewport`);
        expect(
          seen.scrollWidth <= seen.viewport,
          `${width}px: the page scrolls sideways (${seen.scrollWidth}px over ${seen.viewport}px)`
        );
        expect(seen.errors.length === 0, `${width}px: ${surface}: ${seen.errors.join('; ')}`);
        expect(
          seen.clipboard === command,
          `${width}px: ${surface}'s Copy put ${JSON.stringify(seen.clipboard)} on the clipboard, not "${command}"`
        );
      });
    }
  }
}
