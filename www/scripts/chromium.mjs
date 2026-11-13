/**
 * The Chromium the page's Playwright scripts drive (browser-check.mjs,
 * shoot-console.mjs), found the way frontend/scripts/ui-check.mjs finds it:
 * the headless shell or a full Chromium in `$PLAYWRIGHT_BROWSERS_PATH` or this
 * user's Playwright cache, newest revision first. playwright-core, which this
 * package already has, never downloads one itself.
 */
import { readdirSync } from 'node:fs';
import { homedir, platform } from 'node:os';
import { join } from 'node:path';

const CACHE_DIR =
  platform() === 'darwin'
    ? join(homedir(), 'Library', 'Caches', 'ms-playwright')
    : join(homedir(), '.cache', 'ms-playwright');

/** `$PLAYWRIGHT_BROWSERS_PATH` first, then this user's cache. `0` means
 *  "inside node_modules", where playwright-core alone never installs. */
function browserDirs() {
  const named = process.env.PLAYWRIGHT_BROWSERS_PATH;
  return named && named !== '0' ? [named, CACHE_DIR] : [CACHE_DIR];
}

const BINARY_NAMES = new Set(['chrome-headless-shell', 'headless_shell', 'chrome', 'Chromium']);

function findBinary(dir, depth = 0) {
  let entries;
  try {
    entries = readdirSync(dir, { withFileTypes: true });
  } catch {
    return null;
  }
  for (const entry of entries) {
    if (entry.isFile() && BINARY_NAMES.has(entry.name)) return join(dir, entry.name);
  }
  if (depth >= 5) return null;
  for (const entry of entries) {
    if (!entry.isDirectory()) continue;
    const found = findBinary(join(dir, entry.name), depth + 1);
    if (found) return found;
  }
  return null;
}

/** The headless shell first, then a full Chromium, newest revision first. */
function chromiumIn(dir) {
  let names;
  try {
    names = readdirSync(dir);
  } catch {
    return null;
  }
  const found = names
    .map((name) => /^(chromium_headless_shell|chromium)-(\d+)$/.exec(name))
    .filter((match) => match !== null)
    .sort((a, b) =>
      a[1] === b[1]
        ? Number(b[2]) - Number(a[2])
        : a[1] === 'chromium_headless_shell'
          ? -1
          : 1
    );
  for (const match of found) {
    const binary = findBinary(join(dir, match[0]));
    if (binary) return binary;
  }
  return null;
}

/** The Chromium binary to launch, or null when there is none. */
export function chromiumBinary() {
  for (const dir of browserDirs()) {
    const binary = chromiumIn(dir);
    if (binary) return binary;
  }
  return null;
}
