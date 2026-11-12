#!/usr/bin/env node
/**
 * House style forbids em dashes and en dashes in published page copy.
 */
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join, dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const BUILD = resolve(dirname(fileURLToPath(import.meta.url)), '../build');

function walk(dir) {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    return statSync(path).isDirectory() ? walk(path) : [path];
  });
}

/** Find house-style violations in named built files. */
export function dashOffenders(files) {
  const offenders = [];
  for (const file of files) {
    if (!/\.(html|js|css)$/.test(file.path)) continue;
    file.text.split('\n').forEach((line, index) => {
      const vendorPayload = line.length > 4000 && /\.(js|css)$/.test(file.path);
      if (vendorPayload) {
        return;
      }
      const hit = line.match(/[—–]|&mdash;|&ndash;/);
      if (hit) {
        offenders.push({
          file: file.path,
          line: index + 1,
          snippet: line.slice(Math.max(0, hit.index - 45), hit.index + 45).trim()
        });
      }
    });
  }
  return offenders;
}

function main() {
  const files = walk(BUILD).map((path) => ({
    path: path.replace(BUILD + '/', ''),
    text: readFileSync(path, 'utf8')
  }));
  const offenders = dashOffenders(files);

  if (offenders.length) {
    console.error(`Found ${offenders.length} dash(es) in built output:\n`);
    for (const offender of offenders.slice(0, 25)) {
      console.error(`  ${offender.file}:${offender.line}\n    …${offender.snippet}…`);
    }
    process.exitCode = 1;
    return;
  }
  console.log('no em/en dashes in built output');
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main();
}
