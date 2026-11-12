/**
 * `npm run ui-check` is one runner and a folder of surface modules (#2570).
 *
 * These read the split without a browser: the runner finds every module in
 * `scripts/ui-checks/`, and every check the monolith ran at the split's base is
 * still made, exactly once. `ui-check.base.json` is that base, frozen: the
 * `check(` titles as written in `ui-check.mjs` at 8bdec83a and the 79 titles a
 * full run printed there. It never grows — a new module adds checks without
 * touching it, which is the point of the split.
 */
import { spawn, spawnSync } from 'node:child_process';
import { chmodSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { type AddressInfo } from 'node:net';
import { type Server, type ServerResponse, createServer } from 'node:http';
import { join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { afterEach, describe, expect, it } from 'vitest';
import { CHECKS_DIR, checkModules, chromiumRequired, runChecks, selectModules, servedProvenance } from './ui-check-lib.mjs';

const SCRIPTS = new URL('./', import.meta.url);
const read = (name: string) => readFileSync(new URL(name, SCRIPTS), 'utf8');
const base = JSON.parse(read('ui-check.base.json')) as { sources: string[]; titles: string[] };

/** The first argument of every `check(` call, as written: quotes and `${…}` kept. */
function checkTitles(source: string): string[] {
  const re = /await check\(\s*(['"`])((?:\\.|(?!\1)[\s\S])*?)\1/g;
  return [...source.matchAll(re)].map((m) => m[1] + m[2] + m[1]);
}

function tally(items: string[]): Map<string, number> {
  const counts = new Map<string, number>();
  for (const item of items) counts.set(item, (counts.get(item) ?? 0) + 1);
  return counts;
}

/**
 * Every title the real modules would print, read by running them with a
 * `check` that records the title and never runs the body — so no browser, page
 * or server is touched, and a title built in a loop is counted once per pass.
 */
async function runtimeTitles(): Promise<string[]> {
  const titles: string[] = [];
  const never = () => {
    throw new Error('a module reached the browser outside check()');
  };
  await runChecks({
    browser: { newContext: never },
    base: 'http://127.0.0.1:0',
    context: { newPage: never },
    check: async (name: string) => {
      titles.push(name);
    },
    visit: never,
    expect: never,
    sleep: never,
    sizes: { WIDTH: 1440, HEIGHT: 960 }
  });
  return titles;
}

const temps: string[] = [];
afterEach(() => {
  for (const dir of temps.splice(0)) rmSync(dir, { recursive: true, force: true });
});
function tempModules(files: Record<string, string>): URL {
  const dir = mkdtempSync(join(tmpdir(), 'ui-checks-'));
  temps.push(dir);
  for (const [name, body] of Object.entries(files)) writeFileSync(join(dir, name), body);
  return pathToFileURL(`${dir}/`);
}

describe('the runner discovers every module in scripts/ui-checks/', () => {
  it('lists every .mjs file in the folder, in name order', () => {
    const onDisk = readdirSync(CHECKS_DIR)
      .filter((name) => name.endsWith('.mjs'))
      .sort();
    expect(onDisk.length).toBeGreaterThan(0);
    expect(checkModules()).toEqual(onDisk);
  });

  it('runs every module it lists, and each exports checks()', async () => {
    const ran = await runChecks(
      {
        browser: null,
        base: '',
        context: null,
        check: async () => {},
        visit: null,
        expect: null,
        sleep: null,
        sizes: { WIDTH: 1440, HEIGHT: 960 }
      },
      CHECKS_DIR
    );
    expect(ran).toEqual(checkModules());
    // It imports every check module: about 4 s on a quiet machine, over the
    // 5 s default on a loaded one.
  }, 30_000);

  it('picks up a module added to the folder with no other edit', async () => {
    const seen: string[] = [];
    const dir = tempModules({
      'b-second.mjs': 'export async function checks({ check }) { await check("b"); }',
      'a-first.mjs': 'export async function checks({ check }) { await check("a"); }',
      'notes.md': 'not a module'
    });
    const ran = await runChecks({ check: async (name: string) => seen.push(name) }, dir);
    expect(ran).toEqual(['a-first.mjs', 'b-second.mjs']);
    expect(seen).toEqual(['a', 'b']);
  });

  it('refuses an empty folder rather than passing 0/0', async () => {
    await expect(runChecks({}, tempModules({}))).rejects.toThrow(/no surface modules/);
  });

  it('refuses a module with no checks()', async () => {
    const dir = tempModules({ 'x.mjs': 'export const nothing = 1;' });
    await expect(runChecks({}, dir)).rejects.toThrow(/x\.mjs exports no checks\(\)/);
  });
});

describe('a scoped run makes only the surfaces it names', () => {
  const files = {
    'a-first.mjs': 'export async function checks({ check }) { await check("a"); }',
    'b-second.mjs': 'export async function checks({ check }) { await check("b"); }',
    'c-third.mjs': 'export async function checks({ check }) { await check("c"); }'
  };

  it('runs just the named modules, with or without .mjs, in name order', async () => {
    const seen: string[] = [];
    const ran = await runChecks({ check: async (name: string) => seen.push(name) }, tempModules(files), [
      'c-third',
      'a-first.mjs'
    ]);
    expect(ran).toEqual(['a-first.mjs', 'c-third.mjs']);
    expect(seen).toEqual(['a', 'c']);
  });

  it('runs every module when none is named', async () => {
    const ran = await runChecks({ check: async () => {} }, tempModules(files), []);
    expect(ran).toEqual(['a-first.mjs', 'b-second.mjs', 'c-third.mjs']);
  });

  it('refuses a name with no module rather than passing 0/0', () => {
    expect(() => selectModules(['a-first.mjs'], ['a-frist'])).toThrow(/no surface module named a-frist\.mjs/);
  });

  it('fails without a browser instead of skipping, so a scoped pass means Chromium loaded it', () => {
    expect(chromiumRequired(['work-rows'], {})).toBe(true);
    expect(chromiumRequired([], {})).toBe(false);
    expect(chromiumRequired([], { KEW_UI_CHECK_REQUIRE_CHROMIUM: '1' })).toBe(true);
  });
});

describe('every check at the base is still made, exactly once', () => {
  const modules = checkModules().map((name) => read(`ui-checks/${name}`));

  it('every base check( title is in exactly one module', () => {
    const counts = tally(modules.flatMap(checkTitles));
    expect(base.sources).toHaveLength(41);
    for (const title of base.sources) {
      expect([title, counts.get(title) ?? 0]).toEqual([title, 1]);
    }
  });

  it('the modules print every base title once: the same 79 checks', async () => {
    const counts = tally(await runtimeTitles());
    expect(base.titles).toHaveLength(79);
    expect(new Set(base.titles).size).toBe(79);
    for (const title of base.titles) {
      expect([title, counts.get(title) ?? 0]).toEqual([title, 1]);
    }
  });

  it('no surface block stays in the runner', () => {
    const runner = read('ui-check.mjs');
    expect(checkTitles(runner)).toEqual([]);
    expect(runner).not.toMatch(/\bcheck\(\s*['"`]/);
    expect(runner).not.toMatch(/\bvisit\(\s*['"`/]/);
    expect(runner).toMatch(/await runChecks\(\{ browser, base, context, check, visit, expect, sleep, sizes/);
  });
});

/**
 * Where the runner finds a browser, and what it does with none (#2630). Run as
 * a child process with a HOME and a browser directory it owns, so neither this
 * machine's Playwright cache nor its environment can supply one.
 */
describe('the runner finds a browser, or says it has none', () => {
  const RUNNER = fileURLToPath(new URL('ui-check.mjs', SCRIPTS));

  function emptyEnv(extra: Record<string, string>): NodeJS.ProcessEnv {
    const home = mkdtempSync(join(tmpdir(), 'ui-check-home-'));
    temps.push(home);
    const env: NodeJS.ProcessEnv = { ...process.env, HOME: home, ...extra };
    delete env.KEW_UI_CHECK_CHROMIUM;
    if (!('PLAYWRIGHT_BROWSERS_PATH' in extra)) delete env.PLAYWRIGHT_BROWSERS_PATH;
    if (!('KEW_UI_CHECK_REQUIRE_CHROMIUM' in extra)) delete env.KEW_UI_CHECK_REQUIRE_CHROMIUM;
    return env;
  }

  function emptyDir(): string {
    const dir = mkdtempSync(join(tmpdir(), 'ui-check-browsers-'));
    temps.push(dir);
    return dir;
  }

  it('skips, exit 0, when no browser is found and none is required', () => {
    const run = spawnSync(process.execPath, [RUNNER], {
      env: emptyEnv({ PLAYWRIGHT_BROWSERS_PATH: emptyDir() }),
      encoding: 'utf8'
    });
    expect(run.status).toBe(0);
    expect(run.stdout).toContain('skipped: no Chromium');
  });

  it('fails, exit 1, when no browser is found and one is required', () => {
    const browsers = emptyDir();
    const run = spawnSync(process.execPath, [RUNNER], {
      env: emptyEnv({ PLAYWRIGHT_BROWSERS_PATH: browsers, KEW_UI_CHECK_REQUIRE_CHROMIUM: '1' }),
      encoding: 'utf8'
    });
    expect(run.status).toBe(1);
    expect(run.stdout).not.toContain('skipped');
    expect(run.stderr).toContain('requires one');
    expect(run.stderr).toContain(browsers);
  });

  it('finds the headless shell a provisioned host keeps in $PLAYWRIGHT_BROWSERS_PATH', async () => {
    const browsers = emptyDir();
    const shellDir = join(browsers, 'chromium_headless_shell-1179', 'chrome-linux');
    mkdirSync(shellDir, { recursive: true });
    const shell = join(shellDir, 'headless_shell');
    writeFileSync(shell, '#!/bin/sh\nexit 1\n');
    chmodSync(shell, 0o755);

    // The stub is not a browser, so the launch fails; the runner then stops
    // its own dev server and exits 1. What it names first is the binary it chose.
    const run = await new Promise<{ status: number | null; stdout: string }>((resolve) => {
      const child = spawn(process.execPath, [RUNNER], {
        env: emptyEnv({ PLAYWRIGHT_BROWSERS_PATH: browsers, KEW_UI_CHECK_REQUIRE_CHROMIUM: '1' }),
        stdio: ['ignore', 'pipe', 'pipe']
      });
      let stdout = '';
      child.stdout.on('data', (chunk) => (stdout += chunk));
      child.on('exit', (status) => resolve({ status, stdout }));
    });
    expect(run.stdout.split('\n')).toContain(`ui-check: ${shell}`);
    // The runner's own skip line, not Playwright's `<skipped force kill ...>`
    // launch log, which it prints when the stub closes before the kill.
    expect(run.stdout).not.toContain('ui-check: skipped');
    expect(run.status).toBe(1);
  }, 60_000);
});

/**
 * The build history a demo serves (`servedProvenance`, #3207). A remote base's
 * file is taken as served: a deployed build can carry the real export while
 * the checkout holds only the samples. A local base is the checkout's own
 * build, so its file must also be the one the checkout selects
 * (`demo/provenance-bundle.ts`): a build that ships the wrong file fails.
 */
describe('the build history a demo serves', () => {
  const SAMPLE = readFileSync(new URL('../demo/provenance/provenance.sample.json', import.meta.url), 'utf8');
  const servers: Server[] = [];
  afterEach(async () => {
    for (const server of servers.splice(0)) await new Promise((done) => server.close(done));
  });

  /** A server whose `/demo-provenance.json` is `answer`; resolves to its base. */
  async function serving(answer: (res: ServerResponse) => void): Promise<string> {
    const server = createServer((req, res) => {
      if (req.url === '/demo-provenance.json') answer(res);
      else res.writeHead(404).end();
    });
    servers.push(server);
    await new Promise<void>((done) => server.listen(0, '127.0.0.1', done));
    return `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  }
  const json = (text: string) => (res: ServerResponse) => res.writeHead(200, { 'content-type': 'application/json' }).end(text);

  /** A checkout's `demo/provenance/`: the sample, and the real file when given. */
  function checkout(real?: unknown): URL {
    const dir = mkdtempSync(join(tmpdir(), 'kew-provenance-'));
    temps.push(dir);
    writeFileSync(join(dir, 'provenance.sample.json'), SAMPLE);
    if (real !== undefined) writeFileSync(join(dir, 'provenance.json'), JSON.stringify(real));
    return pathToFileURL(`${dir}/`);
  }
  /** The sample with one figure changed: what a build that shipped the wrong file serves. */
  const altered = () => {
    const doc = JSON.parse(SAMPLE);
    doc.features[0].list_cost_usd += 1;
    return doc;
  };

  it('reads a local build that serves the file the checkout selects', async () => {
    const base = await serving(json(SAMPLE));
    const { real, doc } = await servedProvenance(base, { remote: false, dir: checkout() });
    expect(real).toBe(false);
    expect(doc).toEqual(JSON.parse(SAMPLE));
  });

  it('fails a local build that serves another file than the checkout selects', async () => {
    const base = await serving(json(JSON.stringify(altered())));
    await expect(servedProvenance(base, { remote: false, dir: checkout() })).rejects.toThrow(
      /demo-provenance\.json is not .*provenance\.sample\.json/
    );
  });

  it('selects the real file when the checkout holds one, as the demo build does', async () => {
    const real = altered();
    const dir = checkout(real);
    await expect(servedProvenance(await serving(json(SAMPLE)), { remote: false, dir })).rejects.toThrow(
      /demo-provenance\.json is not .*\/provenance\.json/
    );
    const { doc } = await servedProvenance(await serving(json(JSON.stringify(real))), { remote: false, dir });
    expect(doc).toEqual(real);
  });

  it('takes a remote base at its word: the checkout does not say what was deployed', async () => {
    const base = await serving(json(JSON.stringify(altered())));
    const { doc } = await servedProvenance(base, { remote: true, dir: checkout() });
    expect(doc).toEqual(altered());
  });

  it('refuses a redirect off the base, remote or local', async () => {
    const elsewhere = await serving(json(SAMPLE));
    const base = await serving((res) => res.writeHead(302, { location: `${elsewhere}/demo-provenance.json` }).end());
    for (const remote of [true, false]) {
      await expect(servedProvenance(base, { remote, dir: checkout() })).rejects.toThrow(/demo-provenance\.json/);
    }
  });
});
