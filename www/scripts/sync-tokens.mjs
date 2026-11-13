#!/usr/bin/env node
/**
 * Derives www's palette from the system of record.
 *
 * frontend/src/app.css is canonical for kew's design tokens (it is the only
 * definition with a complete light theme and the glow tokens). The marketing
 * site commits to dark only, so this lifts just the dark block and writes it
 * to src/lib/styles/tokens.css, which is gitignored and regenerated on every
 * dev/build.
 *
 *   node scripts/sync-tokens.mjs           regenerate
 *   node scripts/sync-tokens.mjs --check   exit 1 if stale (for CI)
 *
 * Copying tokens by hand is what produced five disagreeing identities in the
 * first place. Don't hand-edit the output.
 */
import { readFileSync, writeFileSync, mkdirSync, existsSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const SOURCE = resolve(here, '../../frontend/src/app.css');
const OUT = resolve(here, '../src/lib/styles/tokens.css');

const MARKER = "[data-theme='dark']";

function extractDarkBlock(css) {
  const at = css.indexOf(MARKER);
  if (at === -1) throw new Error(`no ${MARKER} block found in ${SOURCE}`);
  const open = css.indexOf('{', at);
  const close = css.indexOf('}', open);
  if (open === -1 || close === -1) throw new Error('malformed token block');
  return css
    .slice(open + 1, close)
    .split('\n')
    .map((l) => l.trimEnd())
    .filter((l) => l.trim().length)
    .join('\n');
}

function build() {
  const body = extractDarkBlock(readFileSync(SOURCE, 'utf8'));
  return [
    '/* GENERATED — do not edit.',
    ' * Source: frontend/src/app.css (the system of record).',
    ' * Regenerate: npm run tokens',
    ' */',
    ':root {',
    body,
    '}',
    ''
  ].join('\n');
}

const next = build();
const check = process.argv.includes('--check');
const current = existsSync(OUT) ? readFileSync(OUT, 'utf8') : null;

if (check) {
  if (current !== next) {
    console.error('tokens.css is stale — run `npm run tokens`.');
    console.error('www has drifted from frontend/src/app.css.');
    process.exit(1);
  }
  console.log('tokens in sync with frontend/src/app.css');
} else if (current !== next) {
  mkdirSync(dirname(OUT), { recursive: true });
  writeFileSync(OUT, next);
  console.log(`tokens -> ${OUT.replace(process.cwd() + '/', '')}`);
} else {
  console.log('tokens already current');
}
