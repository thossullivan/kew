/**
 * A scoped ui-check run for a changed file selects the module that owns it
 * (#3201), read from the module's `ui-check-owns:` lines, even when the
 * module's name says nothing about the file.
 */
import { execFileSync } from 'node:child_process';
import { mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { afterEach, describe, expect, it } from 'vitest';
import {
  CHECKS_DIR,
  FRONTEND,
  checkModules,
  globMatches,
  ownedGlobs,
  runChecks,
  selectModules
} from './ui-check-lib.mjs';

const made: string[] = [];

function tempModules(files: Record<string, string>): URL {
  const dir = mkdtempSync(join(tmpdir(), 'ui-owners-'));
  made.push(dir);
  for (const [name, body] of Object.entries(files)) writeFileSync(join(dir, name), body);
  return pathToFileURL(`${dir}/`);
}

afterEach(() => {
  for (const dir of made.splice(0)) rmSync(dir, { recursive: true, force: true });
});

const files = {
  'issue-table.mjs': [
    '/** Rows of the Work list. */',
    '// ui-check-owns: src/routes/(factory)/work/**',
    '// ui-check-owns: src/lib/components/IssueList.svelte',
    'export async function checks({ check }) { await check("rows"); }'
  ].join('\n'),
  'spend-meter.mjs': [
    '/**',
    ' * ui-check-owns: src/lib/budget/*.svelte',
    ' */',
    'export async function checks({ check }) { await check("meter"); }'
  ].join('\n'),
  'unowned.mjs': 'export async function checks({ check }) { await check("unowned"); }'
};

describe('a changed surface selects the module that owns it', () => {
  it('selects the owner of a changed file whose name it does not share', async () => {
    const dir = tempModules(files);
    const seen: string[] = [];
    const ran = await runChecks({ check: async (name: string) => seen.push(name) }, dir, [
      'src/lib/components/IssueList.svelte'
    ]);
    expect(ran).toEqual(['issue-table.mjs']);
    expect(seen).toEqual(['rows']);
  });

  it('reads a repo-rooted path, a ** glob through directories and a JSDoc ownership line', () => {
    const dir = tempModules(files);
    const names = checkModules(dir);
    expect(selectModules(names, ['frontend/src/routes/(factory)/work/[id]/+page.svelte'], dir)).toEqual([
      'issue-table.mjs'
    ]);
    expect(selectModules(names, ['src/lib/budget/Meter.svelte', 'unowned'], dir)).toEqual([
      'spend-meter.mjs',
      'unowned.mjs'
    ]);
  });

  it('refuses a path no module owns rather than passing 0/0', () => {
    const dir = tempModules(files);
    expect(() => selectModules(checkModules(dir), ['src/lib/budget/deep/Meter.svelte'], dir)).toThrow(
      /no surface module owns src\/lib\/budget\/deep\/Meter\.svelte/
    );
  });

  it('owns the real Work route through the work-rows module', () => {
    expect(selectModules(checkModules(), ['src/routes/(factory)/work/+page.svelte'])).toContain('work-rows.mjs');
  });

  it('runs the phone-width Work pipeline check when the Work page changes', () => {
    expect(selectModules(checkModules(), ['src/routes/(factory)/work/+page.svelte'])).toContain('work-pipeline.mjs');
  });
});

describe('ownership globs', () => {
  it('lets * stay inside one directory and ** cross them', () => {
    expect(globMatches('src/lib/*.svelte', 'src/lib/A.svelte')).toBe(true);
    expect(globMatches('src/lib/*.svelte', 'src/lib/x/A.svelte')).toBe(false);
    expect(globMatches('src/**/A.svelte', 'src/A.svelte')).toBe(true);
    expect(globMatches('src/**/A.svelte', 'src/a/b/A.svelte')).toBe(true);
    expect(globMatches('src/routes/(factory)/x.ts', 'src/routes/(factory)/x.ts')).toBe(true);
  });
});

/**
 * The ownership lint (#3298). A module with no `ui-check-owns:` line has an
 * unknown surface, so kew's scoped verify selects it for every change and a
 * scoped run falls back to the full one. Every module must say what it owns.
 * The last part of R16 gave the last four modules their lines, so it deleted
 * `ui-check-unowned.txt`, FROZEN_UNOWNED and the tests that held the list to
 * it, as the list's own header planned: with no list, a module with no line is
 * refused outright.
 */
/**
 * The most of the files under src/ one ownership glob may match. A glob over
 * more selects its module for nearly every change, whatever its text says:
 * `src/lib/**\/*` is `src/lib/**` spelled another way.
 */
const MAX_OWNED_SHARE = 0.1;

/** Whether `glob` matches more than MAX_OWNED_SHARE of the files under src/. */
function ownsTooMuch(glob: string, files: string[]): boolean {
  const source = files.filter((file) => file.startsWith('src/'));
  return source.filter((file) => globMatches(glob, file)).length > source.length * MAX_OWNED_SHARE;
}

/** Every rule the lint holds, as one message per breach; empty when the modules pass. */
function ownershipProblems(
  sources: Record<string, string>,
  files: string[]
): { unowned: string[]; globs: string[] } {
  const owns = (name: string) => ownedGlobs(sources[name]);
  const unowned = Object.keys(sources)
    .filter((name) => owns(name).length === 0)
    .map((name) => `${name} carries no ui-check-owns line`);
  const globs = Object.keys(sources).flatMap((name) =>
    owns(name).flatMap((glob) => {
      if (ownsTooMuch(glob, files)) return [`${name} owns ${glob}, which selects it for nearly every change`];
      if (!files.some((file) => globMatches(glob, file))) return [`${name} owns ${glob}, which matches no file`];
      return [];
    })
  );
  return { unowned, globs };
}

function realSources(): Record<string, string> {
  return Object.fromEntries(
    checkModules().map((name: string) => [name, readFileSync(new URL(name, CHECKS_DIR), 'utf8')])
  );
}

/** The directories a walk skips: installs and build output, never committed. */
const UNTRACKED_DIRS = new Set(['node_modules', '.svelte-kit', 'build', 'build-demo', '.vite', '.git']);

/** Every file under `dir`, relative and slash-separated, outside UNTRACKED_DIRS. */
function walkedFiles(dir: string, prefix = ''): string[] {
  return readdirSync(join(dir, prefix), { withFileTypes: true }).flatMap((entry) => {
    const rel = prefix === '' ? entry.name : `${prefix}/${entry.name}`;
    if (entry.isDirectory()) return UNTRACKED_DIRS.has(entry.name) ? [] : walkedFiles(dir, rel);
    return entry.isFile() ? [rel] : [];
  });
}

/**
 * The frontend's files, relative to `dir`: git's tracked list, or a walk of the
 * tree when `dir` has no git checkout (kew-check-frontend copies frontend/
 * without its .git).
 */
function trackedFiles(dir: string = fileURLToPath(FRONTEND)): string[] {
  let listed: string;
  try {
    listed = execFileSync('git', ['ls-files'], { cwd: dir, encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] });
  } catch {
    return walkedFiles(dir);
  }
  return listed.split('\n').filter((line) => line !== '');
}

describe('every ui-check module says which files it owns', () => {
  it('every module carries an ownership line', () => {
    expect(ownershipProblems(realSources(), trackedFiles()).unowned).toEqual([]);
  });

  it('every ownership glob matches a file and none is a whole-tree glob', () => {
    expect(ownershipProblems(realSources(), trackedFiles()).globs).toEqual([]);
  });
});

describe('the ownership lint refuses', () => {
  /** Twenty files under src/, so one file is 5% of the tree and a whole directory is over 10%. */
  const files = [
    'src/a.svelte',
    'src/b.ts',
    ...Array.from({ length: 9 }, (_, i) => `src/lib/c${i}.ts`),
    ...Array.from({ length: 9 }, (_, i) => `src/routes/r${i}/+page.svelte`),
    'scripts/x.mjs'
  ];

  it('a module with no ownership line, new or one that dropped its lines', () => {
    const sources = {
      'a.mjs': '// ui-check-owns: src/a.svelte\n',
      'work-newcomer.mjs': '',
      'work-items.mjs': 'export async function checks() {}'
    };
    expect(ownershipProblems(sources, files).unowned).toEqual([
      'work-newcomer.mjs carries no ui-check-owns line',
      'work-items.mjs carries no ui-check-owns line'
    ]);
  });

  it('a glob over a tenth of the tree whatever its text, and keeps a one-directory glob', () => {
    const lines = ['src/lib/**/*', 'src/routes/(factory)/**', 'src/**/*.svelte', 'src/routes/(factory)/work/**'];
    const sources = { 'a.mjs': lines.map((glob) => `// ui-check-owns: ${glob}`).join('\n') };
    expect(ownershipProblems(sources, trackedFiles()).globs).toEqual([
      'a.mjs owns src/lib/**/*, which selects it for nearly every change',
      'a.mjs owns src/routes/(factory)/**, which selects it for nearly every change',
      'a.mjs owns src/**/*.svelte, which selects it for nearly every change'
    ]);
  });

  it('a glob that matches no file, and every whole-tree glob', () => {
    const lines = ['src/missing.svelte', '**', 'src/**', 'src/lib/**', 'src/routes/**', 'src/*.ts'].map(
      (glob) => `// ui-check-owns: ${glob}`
    );
    expect(ownershipProblems({ 'a.mjs': lines.join('\n') }, files).globs).toEqual([
      'a.mjs owns src/missing.svelte, which matches no file',
      'a.mjs owns **, which selects it for nearly every change',
      'a.mjs owns src/**, which selects it for nearly every change',
      'a.mjs owns src/lib/**, which selects it for nearly every change',
      'a.mjs owns src/routes/**, which selects it for nearly every change'
    ]);
  });

  it('a glob that matches one file in a small tree', () => {
    const sources = { 'a.mjs': '// ui-check-owns: src/a.svelte\n// ui-check-owns: src/lib/c0.ts' };
    expect(ownershipProblems(sources, files).globs).toEqual([]);
  });
});

describe('the ownership lint reads a frontend copied without its git checkout', () => {
  it('lists the files by walking the tree, skipping installs and builds', () => {
    const dir = mkdtempSync(join(tmpdir(), 'ui-owners-copy-'));
    made.push(dir);
    for (const path of [
      'src/a.svelte',
      'src/lib/b.ts',
      'scripts/c.mjs',
      'node_modules/x/index.js',
      '.svelte-kit/types.d.ts',
      'build/index.html'
    ]) {
      mkdirSync(join(dir, dirname(path)), { recursive: true });
      writeFileSync(join(dir, path), '');
    }
    expect(trackedFiles(dir).sort()).toEqual(['scripts/c.mjs', 'src/a.svelte', 'src/lib/b.ts']);
  });
});
