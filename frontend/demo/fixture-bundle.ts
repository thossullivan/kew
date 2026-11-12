/**
 * The demo build's fixture set, bundled (#3139).
 *
 * The demo console has no backend: every `/api` read is answered in the
 * browser out of a copy of one fixture set, compiled into the bundle. This
 * plugin is where that copy comes from. It serves one virtual module,
 * `virtual:kew-demo-fixtures`, whose default export is every `*.json` under
 * the set's directory (sub-folders too: a keyed read answers out of
 * `dispatch-resolve/<item>.json`), keyed by its path relative to the set, plus
 * the instant the set is read AS.
 *
 * The epoch is not a second setting: it is the newest instant anywhere in the
 * set, which is exactly how `FIXTURE_EPOCH` is defined for the factory set —
 * so serving maps the freshest reading to now and nothing lands in the
 * future, whichever set the demo config names.
 *
 * Only the demo config registers this plugin. The normal build never resolves
 * the virtual module, so a demo module that survived into it would fail that
 * build rather than ship.
 */
import { readdirSync, readFileSync } from 'node:fs';
import { join, relative, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import type { Plugin } from 'vite';
import { INSTANT } from '../fixtures/core';

export const DEMO_FIXTURES_ID = 'virtual:kew-demo-fixtures';
const RESOLVED = `\0${DEMO_FIXTURES_ID}`;

/**
 * Top-level files a set may hold that answer no `/api` read: the demo world's
 * manifest (the refs its checks and tours read) and the timings it is
 * generated from (#3141). Bundled, they would answer `/api/manifest`.
 */
export const NOT_FIXTURES: readonly string[] = ['manifest.json', 'timings.json'];

/** Every `*.json` file under `dir`, as paths relative to it with `/` separators. */
function jsonFiles(dir: string, root: string = dir): string[] {
  return readdirSync(dir, { withFileTypes: true })
    .flatMap((entry) => {
      const path = join(dir, entry.name);
      if (entry.isDirectory()) return jsonFiles(path, root);
      return entry.isFile() && entry.name.endsWith('.json') ? [relative(root, path)] : [];
    })
    .map((file) => file.split(sep).join('/'))
    .filter((file) => !NOT_FIXTURES.includes(file))
    .sort();
}

/** The newest full ISO-8601 instant anywhere in `value`, in epoch ms. */
function newestInstant(value: unknown): number {
  if (typeof value === 'string') return INSTANT.test(value) ? Date.parse(value) : -Infinity;
  if (Array.isArray(value)) return Math.max(-Infinity, ...value.map(newestInstant));
  if (value !== null && typeof value === 'object')
    return Math.max(-Infinity, ...Object.values(value).map(newestInstant));
  return -Infinity;
}

export type DemoFixtureSet = {
  /** Fixture payloads by their path relative to the set: `runs.json`, `dispatch-resolve/2060.json`. */
  files: Record<string, unknown>;
  /** The instant the set is read AS: its newest instant. */
  epoch: string;
  /** Where the set lives, repo-relative, for a 404 that names the file it looked for. */
  source: string;
};

/** The set under `dir`, read and parsed. Throws on a file that is not JSON. */
export function readFixtureSet(dir: URL): DemoFixtureSet {
  const root = fileURLToPath(dir);
  const files: Record<string, unknown> = {};
  for (const file of jsonFiles(root)) {
    try {
      files[file] = JSON.parse(readFileSync(join(root, file), 'utf8'));
    } catch (error) {
      throw new Error(`demo fixture ${file} is not JSON: ${(error as Error).message}`);
    }
  }
  const newest = newestInstant(Object.values(files));
  if (!Number.isFinite(newest)) throw new Error(`demo fixture set ${root} holds no instant`);
  const repo = fileURLToPath(new URL('../../', import.meta.url));
  return {
    files,
    epoch: new Date(newest).toISOString(),
    source: `${relative(repo, root).split(sep).join('/')}/`
  };
}

/** The Vite plugin that bundles the set under `dir` as `virtual:kew-demo-fixtures`. */
export function demoFixtures(dir: URL): Plugin {
  return {
    name: 'kew-demo-fixtures',
    resolveId(id) {
      return id === DEMO_FIXTURES_ID ? RESOLVED : null;
    },
    load(id) {
      if (id !== RESOLVED) return null;
      return `export default ${JSON.stringify(readFixtureSet(dir))};`;
    }
  };
}
