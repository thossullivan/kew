/**
 * Commands break only at their own spaces, never at an id's hyphen (#3052 N4).
 *
 * A command block wrapped as the browser wraps prose, which breaks after a
 * hyphen: the Monitor's hold read "kew epic unhold / supervised- / daemon
 * 2814" at 390 and the epic popover "go-live-2026-09 SFD- / 229" at 1440 — an
 * id cut in two at a line end, where the block should hold the command exactly
 * as typed. Every surface prints a command through the shared `Command`, which
 * wraps it only at its own spaces and never scrolls it inside its block.
 *
 * On Monitor, Epic and the first-run card (#3077), at 1440 and 390 (the
 * first-run card at 768 too), over the fixture's command and over a long one
 * served through `page.route`, every `code[data-command]`'s drawn
 * lines — read off each character's own box — joined by spaces are the command
 * text exactly, and no line ends in a hyphen the command does not have before
 * a space. The one exception is the last resort the operator ruling of
 * 2026-10-20 allows: a single word wider than the whole block (measured in the
 * block's own font against its content width) may break inside itself. Every
 * other word is whole on one line.
 *
 * Every block is read, drawn or not, and a block that draws no line is a
 * fault: so the first-run card with no config in force opens its deferred
 * disclosure first, and the checks' fixes behind it are measured as drawn.
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 */
// ui-check-owns: src/lib/ui/Command.svelte
// ui-check-owns: src/lib/ui/command-markup.ts
import { readFileSync } from 'node:fs';
import { FRONTEND } from '../ui-check-lib.mjs';

const SIZES = [
  { width: 1440, height: 960 },
  { width: 390, height: 844 }
];

/** The first-run card is also checked at the tablet width (#3077). */
const FIRST_RUN_SIZES = [SIZES[0], { width: 768, height: 1024 }, SIZES[1]];

/** Longer than any command block at 390, and hyphens all through it. */
const LONG_COMMAND =
  'kew epic unhold go-live-2026-09-supervised-daemon SFD-2814 --reason review-round-cap-reached';

/** Serve every command the Monitor's holds read as the long one. */
const withLongDashboardCommand = (page) =>
  page.route(
    (url) => url.pathname === '/api/dashboard',
    async (route) => {
      const response = await route.fetch();
      const body = await response.json();
      body.rows = body.rows.map((row) => (row.command ? { ...row, command: LONG_COMMAND } : row));
      await route.fulfill({ response, json: body });
    }
  );

/** Serve every command the go-live epic's nodes carry as the long one. */
const withLongEpicCommand = (page) =>
  page.route(
    (url) => url.pathname === '/api/epics/go-live-2026-09',
    async (route) => {
      const response = await route.fetch();
      const body = await response.json();
      const nodes = body.snapshot?.nodes ?? [];
      for (const node of nodes) {
        if (node.attention?.command) node.attention.command = LONG_COMMAND;
      }
      await route.fulfill({ response, json: body });
    }
  );

/** The first-run set `work-first-run.mjs` routes: no work, no runs, a failing check. */
const FIRST_RUN = {
  '/api/dashboard': 'dashboard-first-run.json',
  '/api/dashboard/work': 'dashboard-work-first-run.json',
  '/api/runs': 'runs-first-run.json',
  '/api/readiness': 'readiness-first-run.json'
};

/**
 * `kew serve` before `kew init`, as `work-first-run.mjs` serves it: `project-config`
 * fails, so `kew init` is the next command and the other checks' fixes wait
 * behind the deferred disclosure, a `model-eol` warning among them.
 */
function withoutConfig(checks) {
  return checks.map((check) =>
    check.id === 'project-config'
      ? { ...check, status: 'fail', summary: 'no kew.toml found; running on built-in defaults', fix: 'kew init' }
      : check.id === 'model-eol'
        ? { ...check, status: 'warn', summary: 'a pinned model retires soon', fix: 'kew model-eol refresh' }
        : check
  );
}

/**
 * Serve the first-run set, with no config in force when `noConfig`; when
 * `long`, every failing check's fix but `kew init` is the long command.
 */
const withFirstRun = (long, noConfig = false) => async (page) => {
  for (const [path, file] of Object.entries(FIRST_RUN)) {
    const body = JSON.parse(readFileSync(new URL(`fixtures/scenarios/${file}`, FRONTEND), 'utf8'));
    if (path === '/api/readiness') {
      if (noConfig) body.checks = withoutConfig(body.checks);
      if (long) for (const check of body.checks) if (check.fix && check.fix !== 'kew init') check.fix = LONG_COMMAND;
    }
    await page.route(
      (url) => url.pathname === path,
      (route) => route.fulfill({ status: 200, contentType: 'application/json', json: body })
    );
  }
};

/** Each command block's text and the lines it is drawn on, read per character. */
function readCommands() {
  return [...document.querySelectorAll('code[data-command]')].map((code) => {
    const lines = [];
    const walker = document.createTreeWalker(code, NodeFilter.SHOW_TEXT);
    const range = document.createRange();
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      for (let i = 0; i < node.length; i++) {
        range.setStart(node, i);
        range.setEnd(node, i + 1);
        const rect = [...range.getClientRects()].find((r) => r.height > 0);
        if (!rect) continue;
        let line = lines.find((l) => Math.abs(l.top - rect.top) < 4);
        if (!line) {
          line = { top: rect.top, text: '' };
          lines.push(line);
        }
        line.text += node.data[i];
      }
    }
    lines.sort((a, b) => a.top - b.top);
    // The words no line of this block can hold whole: each word's unwrapped
    // width in the block's own font, against the block's content width.
    const style = getComputedStyle(code);
    const room = code.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
    const pen = document.createElement('canvas').getContext('2d');
    // Longhands: the computed `font` shorthand is empty when a longhand it
    // cannot spell is set, and the canvas then measures in 10px sans-serif.
    pen.font = `${style.fontStyle} ${style.fontWeight} ${style.fontSize} ${style.fontFamily}`;
    const text = code.textContent ?? '';
    const wide = [...new Set(text.split(/\s+/).filter((word) => pen.measureText(word).width > room + 0.5))];
    return { text, lines: lines.map((l) => l.text.trim()).filter(Boolean), wide };
  });
}

/**
 * Whether the lines are the command in order, each break at the command's own
 * whitespace or inside one of the `wide` words — a word wider than the block,
 * the one word allowed to break (operator ruling 2026-10-20).
 */
function breaksOnlyAtSpacesOrInWideWords(text, lines, wide) {
  const typed = text.trim();
  let at = 0;
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (typed.slice(at, at + line.length) !== line) return false;
    at += line.length;
    if (i === lines.length - 1) return at === typed.length;
    if (/\s/.test(typed.charAt(at))) {
      while (/\s/.test(typed.charAt(at))) at++;
      continue;
    }
    const start = typed.slice(0, at).search(/\S+$/);
    const rest = typed.slice(at).search(/\s/);
    const word = typed.slice(start === -1 ? at : start, rest === -1 ? typed.length : at + rest);
    if (!wide.includes(word)) return false;
  }
  return false;
}

/** Why a drawn command is not the command as typed, or null when it is. */
export function commandLineFault({ text, lines, wide = [] }) {
  if (lines.length === 0) return `"${text}" drew no line`;
  const joined = lines.join(' ');
  if (joined !== text.trim()) {
    if (wide.length > 0 && breaksOnlyAtSpacesOrInWideWords(text, lines, wide)) return null;
    return `the lines [${lines.join(' / ')}] join to "${joined}", not "${text}"`;
  }
  const words = text.split(' ');
  const cut = lines.slice(0, -1).find((line) => line.endsWith('-') && !words.includes(line.split(' ').pop()));
  return cut ? `"${text}" breaks at a hyphen: [${lines.join(' / ')}]` : null;
}

/** Open the first-run card's deferred disclosure, so its fixes are drawn. */
async function openDeferred(page) {
  await page.locator('button[aria-controls="first-run-deferred"]').click();
  await page.locator('#first-run-deferred code[data-command]').first().waitFor({ state: 'visible', timeout: 15_000 });
}

const SURFACES = [
  { name: 'Monitor', route: '/monitor', ready: 'section[aria-label="Holds and blocks"] code[data-command]', long: withLongDashboardCommand },
  {
    name: 'Epic',
    route: '/epics/go-live-2026-09?node=SFD-229',
    ready: '[role="dialog"] code[data-command]',
    long: withLongEpicCommand
  },
  {
    name: 'First run',
    route: '/work',
    ready: 'section[aria-label="First run"] code[data-command]',
    sizes: FIRST_RUN_SIZES,
    fixture: withFirstRun(false),
    long: withFirstRun(true)
  },
  {
    name: 'First run, no config',
    route: '/work',
    ready: 'section[aria-label="First run"] code[data-command]',
    sizes: FIRST_RUN_SIZES,
    fixture: withFirstRun(false, true),
    long: withFirstRun(true, true),
    open: openDeferred
  }
];

export async function checks({ check, visit, expect, sleep }) {
  for (const surface of SURFACES) {
    for (const size of surface.sizes ?? SIZES) {
      for (const long of [false, true]) {
        const what = long ? 'a long served command' : 'the fixture command';
        await check(`${surface.name}: every command's lines join to it exactly at ${size.width}px, over ${what}`, () =>
          visit(
            surface.route,
            async (page) => {
              await page.locator(surface.ready).first().waitFor({ state: 'visible', timeout: 15_000 });
              if (surface.open) await surface.open(page);
              await sleep(300);
              const commands = await page.evaluate(readCommands);
              expect(commands.length >= 1, `${surface.name} drew no command, so this proves nothing`);
              if (long) {
                expect(
                  commands.some((command) => command.text === LONG_COMMAND),
                  `${surface.name} never drew the served long command, so this proves nothing`
                );
              }
              for (const command of commands) {
                const fault = commandLineFault(command);
                expect(fault === null, `${surface.name} at ${size.width}px: ${fault}`);
              }
              const sideways = await page.evaluate(
                () => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1
              );
              expect(!sideways, `${surface.name} scrolls sideways at ${size.width}px`);
            },
            size,
            long ? surface.long : surface.fixture
          )
        );
      }
    }
  }
}
