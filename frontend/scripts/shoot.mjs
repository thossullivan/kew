#!/usr/bin/env node
/**
 * FUI-02 — photograph a route and its reference artboard at the same size.
 *
 *   npm run dev:fixtures                      # /api from fixtures/factory
 *   npm run shoot -- /epics/launch-fixes DEpic
 *
 * Writes two PNGs at 1440x960 into `frontend/.shots/` (gitignored):
 *   DEpic.app.png  — the running app at the given route
 *   DEpic.ref.png  — design/factory-ui/DEpic.html, the pixel reference
 * Compare them for position (+-2px), token colours, type size and weight,
 * hairlines and status shapes, and list every deviation in the PR.
 *
 * No npm dependency: this drives the Chrome that is already installed, through
 * its own `--screenshot` flag. Set `$CHROME` to the binary (the probe list
 * below is a convenience, not a contract).
 */
import { spawnSync } from 'node:child_process';
import { existsSync, mkdirSync, mkdtempSync, openSync, readSync, closeSync, rmSync, statSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = new URL('../', import.meta.url);
const ARTBOARDS = new URL('design/factory-ui/', ROOT);
const WIDTH = 1440;
const HEIGHT = 960;

const CHROME_CANDIDATES = [
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  '/usr/bin/google-chrome',
  '/usr/bin/google-chrome-stable',
  '/usr/bin/chromium',
  '/usr/bin/chromium-browser'
];

function die(message) {
  console.error(`shoot: ${message}`);
  process.exit(1);
}

function chromeBinary() {
  const named = process.env.CHROME;
  if (named) {
    if (!existsSync(named)) die(`$CHROME is set to ${named}, which does not exist`);
    return named;
  }
  const found = CHROME_CANDIDATES.find((c) => existsSync(c));
  if (!found) {
    die(
      'no Chrome found. Set $CHROME to the browser binary, e.g.\n' +
        "  CHROME='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' " +
        'npm run shoot -- /epics/launch-fixes DEpic'
    );
  }
  return found;
}

function isPng(path) {
  if (!existsSync(path) || statSync(path).size === 0) return false;
  const fd = openSync(path, 'r');
  const head = Buffer.alloc(8);
  try {
    readSync(fd, head, 0, 8, 0);
  } finally {
    closeSync(fd);
  }
  return head.equals(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]));
}

function shoot(chrome, url, out) {
  const profile = mkdtempSync(join(tmpdir(), 'kew-shoot-'));
  const args = [
    '--headless=new',
    '--disable-gpu',
    '--hide-scrollbars',
    '--force-device-scale-factor=1',
    `--user-data-dir=${profile}`,
    '--virtual-time-budget=4000',
    `--window-size=${WIDTH},${HEIGHT}`,
    `--screenshot=${out}`,
    url
  ];
  // Chrome refuses its sandbox when it is already running as root (CI images,
  // dev containers). Only then, and never on a normal user account.
  if (typeof process.getuid === 'function' && process.getuid() === 0) {
    args.unshift('--no-sandbox');
  }
  const result = spawnSync(chrome, args, { stdio: ['ignore', 'inherit', 'inherit'] });
  rmSync(profile, { recursive: true, force: true });
  if (result.error) die(`could not run ${chrome}: ${result.error.message}`);
  if (result.status !== 0) die(`chrome exited ${result.status} shooting ${url}`);
  if (!isPng(out)) die(`chrome wrote no PNG for ${url} (expected ${out})`);
  console.log(`  ${out}  <-  ${url}`);
}

/**
 * Advisory, never fatal: some environments refuse a loopback connection from
 * node while Chrome connects happily (containers with a socket policy, for
 * one). A wrong answer here must not refuse a shoot that would have worked —
 * so an unreachable base is a warning, and the PNG of an error page is the
 * unmissable second signal.
 */
async function warnIfUnreachable(base) {
  try {
    await fetch(base, { method: 'GET' });
  } catch {
    console.warn(
      `shoot: could not reach ${base} from node. If the app PNG shows an error page,\n` +
        '       start the fixture server first:  npm run dev:fixtures'
    );
  }
}

const [route, artboardArg] = process.argv.slice(2);
if (!route || !artboardArg) {
  die('usage: npm run shoot -- <route> <artboard>   e.g. /epics/launch-fixes DEpic');
}
if (!route.startsWith('/')) die(`route must start with "/" (got ${route})`);

const artboard = artboardArg.replace(/\.html$/, '');
if (!/^[A-Za-z0-9_-]+$/.test(artboard)) {
  die(`artboard must be a plain file name in design/factory-ui (got ${artboardArg})`);
}
const reference = new URL(`${artboard}.html`, ARTBOARDS);
if (!existsSync(fileURLToPath(reference))) {
  die(`no artboard design/factory-ui/${artboard}.html`);
}

const chrome = chromeBinary();

const base = (process.env.KEW_UI_BASE ?? 'http://localhost:5173').replace(/\/$/, '');
await warnIfUnreachable(base);

const outDir = process.env.KEW_UI_SHOTS
  ? process.env.KEW_UI_SHOTS
  : fileURLToPath(new URL('.shots/', ROOT));
mkdirSync(outDir, { recursive: true });

console.log(`shoot: ${chrome} at ${WIDTH}x${HEIGHT}`);
shoot(chrome, `${base}${route}`, join(outDir, `${artboard}.app.png`));
shoot(chrome, reference.href, join(outDir, `${artboard}.ref.png`));
