/**
 * The demo modules are reached from one place only (#3139).
 *
 * `src/hooks.client.ts` imports `src/lib/demo/install` behind `__KEW_DEMO__`,
 * which is `false` in the normal build, so the import is dead code and every
 * demo module goes with it. That holds only while nothing else imports demo
 * code: a shell file importing `$lib/demo/writes` builds fine and ships the
 * write table in the normal bundle, the flag notwithstanding. Normal code
 * reaches demo chrome through the shell's slots (`chrome-slots.svelte.ts`),
 * which the demo fills and the normal build leaves empty.
 *
 * So no shipped file under `src/` but `hooks.client.ts` and the demo modules
 * themselves may import from `src/lib/demo/`, from `frontend/fixtures/` (the
 * fixture data and its helpers) or from `frontend/demo/` (the demo build's
 * config and bundle), nor the bundled set's virtual module, nor the tours'
 * (#3145): neither their virtual module nor driver.js, which only the demo
 * ships. Test files are
 * not shipped and are left out. Imports are read off the parsed source —
 * TypeScript's own import scan, over a `.svelte` file's script blocks as the
 * Svelte compiler finds them — not grepped, so a comment naming a path is not
 * an import and a dynamic `import()` is one.
 */
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { dirname, join, relative, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { parse } from 'svelte/compiler';
import ts from 'typescript';
import { describe, expect, it } from 'vitest';

const FRONTEND = fileURLToPath(new URL('../../../', import.meta.url));
const SRC = join(FRONTEND, 'src');
const LIB = join(SRC, 'lib');

/** Where demo code and fixture data live, each with what it is. */
const FORBIDDEN: ReadonlyArray<{ dir: string; what: string }> = [
  { dir: join(LIB, 'demo'), what: 'a demo module' },
  { dir: join(FRONTEND, 'fixtures'), what: 'the fixtures' },
  { dir: join(FRONTEND, 'demo'), what: "the demo build's own files" }
];
/** Modules with no path on disk that only demo code may import, each with what it is. */
const DEMO_ONLY: Readonly<Record<string, string>> = {
  'virtual:kew-demo-fixtures': 'the bundled fixture set',
  'virtual:kew-demo-tours': 'the bundled tours',
  'virtual:kew-demo-provenance-meta': 'what the bundled build history names',
  'driver.js': 'the tour engine'
};

/** What a bare specifier is, when only demo code may import it: `driver.js/dist/driver.css` is driver.js. */
function demoOnly(specifier: string): string | undefined {
  const name = Object.keys(DEMO_ONLY).find((id) => specifier === id || specifier.startsWith(`${id}/`));
  return name === undefined ? undefined : DEMO_ONLY[name];
}

/** The files allowed to import demo code: the switch, and the demo itself. */
function allowed(file: string): boolean {
  return (
    file === join(SRC, 'hooks.client.ts') ||
    file.startsWith(`${join(LIB, 'demo')}${sep}`) ||
    /\.test\.ts$/.test(file)
  );
}

/** The script text of a source file: the whole of a `.ts`/`.js`, the
 *  `<script>` blocks of a `.svelte`. */
function scriptsOf(file: string, source: string): string[] {
  if (!file.endsWith('.svelte')) return [source];
  const ast = parse(source, { modern: true });
  // The compiler's ESTree `Program` carries its offsets, which the type omits.
  type Located = { start: number; end: number };
  return [ast.module, ast.instance]
    .filter((script) => script != null)
    .map((script) => script.content as unknown as Located)
    .map(({ start, end }) => source.slice(start, end));
}

/** Where a specifier points, on disk; null for a bare package name. */
function target(file: string, specifier: string): string | null {
  if (specifier === '$lib' || specifier.startsWith('$lib/')) return join(LIB, specifier.slice('$lib'.length));
  if (specifier.startsWith('.')) return resolve(dirname(file), specifier);
  return null;
}

/** Each import in `source` (the text of `file`) that reaches demo code or
 *  fixture data, as `<file>: imports <specifier> (<what>)`. */
export function demoImports(file: string, source: string): string[] {
  const found: string[] = [];
  for (const script of scriptsOf(file, source)) {
    for (const { fileName: specifier } of ts.preProcessFile(script, true, true).importedFiles) {
      const path = target(file, specifier);
      const hit =
        demoOnly(specifier) ??
        (path === null
          ? undefined
          : FORBIDDEN.find(({ dir }) => path === dir || path.startsWith(`${dir}${sep}`))?.what);
      if (hit !== undefined) found.push(`${relative(FRONTEND, file)}: imports ${specifier} (${hit})`);
    }
  }
  return found;
}

/** Every source file under `dir`. */
function sources(dir: string): string[] {
  return readdirSync(dir)
    .sort()
    .flatMap((name) => {
      const path = join(dir, name);
      if (statSync(path).isDirectory()) return sources(path);
      return /\.(ts|js|svelte)$/.test(name) ? [path] : [];
    });
}

describe('the demo import boundary', () => {
  it('finds no shipped file under src/ importing demo code or fixtures', () => {
    const offenders = sources(SRC)
      .filter((file) => !allowed(file))
      .flatMap((file) => demoImports(file, readFileSync(file, 'utf8')));
    expect(offenders).toEqual([]);
  });

  it('catches a shell file importing the write table and the banner', () => {
    const layout = join(SRC, 'routes', '(factory)', '+layout.svelte');
    const source = [
      '<script lang="ts">',
      "  import { demoRefusal } from '$lib/demo/writes';",
      "  import DemoBanner from '$lib/demo/DemoBanner.svelte';",
      "  import { chromeSlots } from '$lib/shell/chrome-slots.svelte';",
      '</script>',
      '<p>{demoRefusal}</p>'
    ].join('\n');
    expect(demoImports(layout, source)).toEqual([
      'src/routes/(factory)/+layout.svelte: imports $lib/demo/writes (a demo module)',
      'src/routes/(factory)/+layout.svelte: imports $lib/demo/DemoBanner.svelte (a demo module)'
    ]);
  });

  it('catches relative, dynamic and re-exporting imports, and the bundled set', () => {
    const file = join(LIB, 'shell', 'x.ts');
    const source = [
      "export { DEMO_BANNER } from '../demo/banner.js';",
      "const core = await import('../../../fixtures/core');",
      "import set from 'virtual:kew-demo-fixtures';",
      "import config from '../../../demo/fixture-set';",
      '// a comment naming $lib/demo/writes is not an import',
      "import { nav } from './nav';"
    ].join('\n');
    expect(demoImports(file, source)).toEqual([
      'src/lib/shell/x.ts: imports ../demo/banner.js (a demo module)',
      'src/lib/shell/x.ts: imports ../../../fixtures/core (the fixtures)',
      'src/lib/shell/x.ts: imports virtual:kew-demo-fixtures (the bundled fixture set)',
      "src/lib/shell/x.ts: imports ../../../demo/fixture-set (the demo build's own files)"
    ]);
  });

  it('catches the tour engine, its stylesheet and the bundled tours', () => {
    const file = join(LIB, 'shell', 'FactoryHeader.svelte');
    const source = [
      '<script lang="ts">',
      "  import { driver } from 'driver.js';",
      "  import 'driver.js/dist/driver.css';",
      "  import tours from 'virtual:kew-demo-tours';",
      "  import TourEntry from '$lib/demo/tours/TourEntry.svelte';",
      "  import { page } from '$app/state';",
      '</script>'
    ].join('\n');
    expect(demoImports(file, source)).toEqual([
      'src/lib/shell/FactoryHeader.svelte: imports driver.js (the tour engine)',
      'src/lib/shell/FactoryHeader.svelte: imports driver.js/dist/driver.css (the tour engine)',
      'src/lib/shell/FactoryHeader.svelte: imports virtual:kew-demo-tours (the bundled tours)',
      'src/lib/shell/FactoryHeader.svelte: imports $lib/demo/tours/TourEntry.svelte (a demo module)'
    ]);
  });
});
