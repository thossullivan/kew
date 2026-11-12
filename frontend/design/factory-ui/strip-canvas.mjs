#!/usr/bin/env node
// Strip the design-canvas runtime from the Factory UI artboards.
//
//   node strip-canvas.mjs <src-dir> <out-dir>
//
// Reads <src-dir>/{DEpic,DWork,DMonitor}.dc.html and writes the static
// <out-dir>/{DEpic,DWork,DMonitor}.html. No dependencies. Every rule must match
// exactly as expected, or the script exits non-zero without writing anything,
// so a changed canvas export fails loudly instead of producing a half-stripped
// file. Inline styles are never touched.
//
// Rules:
//   1. remove <script src="./support.js"></script>
//   2. move the <helmet> children into <head>
//   3. unwrap <x-dc>
//   4. remove the <script ... data-dc-script ...> block
//   5. links to an artboard that is not committed here (DEvals) become plain <span> text with a title, keeping their style
//   6. rewrite *.dc.html hrefs to *.html

import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import { join } from 'node:path';

const ARTBOARDS = ['DEpic', 'DWork', 'DMonitor'];
const UNCOMMITTED = { DEvals: 'Evals is not part of kew' };

function fail(name, message) {
  throw new Error(`${name}: ${message}`);
}

function removeOnce(name, text, pattern, label) {
  const matches = text.match(new RegExp(pattern.source, 'g'));
  if (!matches || matches.length !== 1) {
    fail(name, `expected exactly one ${label}, found ${matches ? matches.length : 0}`);
  }
  return text.replace(pattern, '');
}

export function strip(name, source) {
  let text = source;

  text = removeOnce(name, text, /<script src="\.\/support\.js"><\/script>\r?\n/, 'support.js script');

  const helmet = text.match(/<helmet>\r?\n([\s\S]*?)<\/helmet>\r?\n/);
  if (!helmet) fail(name, 'no <helmet> block');
  text = removeOnce(name, text, /<helmet>\r?\n[\s\S]*?<\/helmet>\r?\n/, '<helmet> block');
  if (text.split('</head>').length !== 2) fail(name, 'expected exactly one </head>');
  text = text.replace('</head>', `${helmet[1]}</head>`);

  text = removeOnce(name, text, /<x-dc>\r?\n/, '<x-dc>');
  text = removeOnce(name, text, /<\/x-dc>\r?\n/, '</x-dc>');
  text = removeOnce(
    name,
    text,
    /<script type="text\/x-dc" data-dc-script[^>]*>[\s\S]*?<\/script>\r?\n/,
    'data-dc-script block'
  );

  for (const [target, title] of Object.entries(UNCOMMITTED)) {
    const link = new RegExp(`<a href="${target}\\.dc\\.html"([^>]*)>([^<]*)</a>`, 'g');
    text = text.replace(link, `<span title="${title}"$1>$2</span>`);
  }

  text = text.replace(/href="([A-Za-z0-9_-]+)\.dc\.html"/g, 'href="$1.html"');

  for (const leftover of ['x-dc', '<helmet', '</helmet', 'support.js', 'data-dc-script', '.dc.html']) {
    if (text.includes(leftover)) fail(name, `"${leftover}" survived stripping`);
  }
  for (const target of Object.keys(UNCOMMITTED)) {
    if (text.includes(target)) fail(name, `a reference to ${target} survived stripping`);
  }
  return text;
}

function main([srcDir, outDir]) {
  if (!srcDir || !outDir) {
    console.error('usage: node strip-canvas.mjs <src-dir> <out-dir>');
    process.exit(2);
  }
  const outputs = ARTBOARDS.map((name) => [
    name,
    strip(name, readFileSync(join(srcDir, `${name}.dc.html`), 'utf8'))
  ]);
  mkdirSync(outDir, { recursive: true });
  for (const [name, text] of outputs) {
    writeFileSync(join(outDir, `${name}.html`), text);
    console.log(`wrote ${join(outDir, `${name}.html`)}`);
  }
}

try {
  main(process.argv.slice(2));
} catch (error) {
  console.error(`strip-canvas: ${error.message}`);
  process.exit(1);
}
