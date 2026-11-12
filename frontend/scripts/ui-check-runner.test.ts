/**
 * The runner, run on fixture modules (#3477): a route handler that throws
 * fails its own check and the run goes on to its summary and exit 1, and a
 * module that calls `process.exit(0)` cannot end the run as a pass.
 *
 * `$KEW_UI_CHECK_DIR` points the runner at one folder of
 * `scripts/ui-check-fixtures/`. A run needs a Chromium; with none on this
 * machine the runner says so and the browser-driven tests are skipped, unless
 * `KEW_UI_CHECK_REQUIRE_CHROMIUM=1` says this machine must have one.
 */
import { spawn, spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

const RUNNER = fileURLToPath(new URL('ui-check.mjs', import.meta.url));
const FIXTURES = new URL('ui-check-fixtures/', import.meta.url);
const LIB = new URL('./ui-check-lib.mjs', import.meta.url).href;
const NO_BROWSER = /no Chromium, and this run requires one/;

type Run = { status: number | null; stdout: string; stderr: string };

/** `npm run ui-check -- <name>` on the fixture folder `name`. */
function runner(name: string): Promise<Run> {
  const env = { ...process.env, KEW_UI_CHECK_DIR: fileURLToPath(new URL(`${name}/`, FIXTURES)) };
  return new Promise((resolve) => {
    const child = spawn(process.execPath, [RUNNER, name], { env, stdio: ['ignore', 'pipe', 'pipe'] });
    let stdout = '';
    let stderr = '';
    child.stdout.on('data', (chunk) => (stdout += chunk));
    child.stderr.on('data', (chunk) => (stderr += chunk));
    child.on('exit', (status) => resolve({ status, stdout, stderr }));
  });
}

/** Whether a run that found no browser may be skipped here. */
function noBrowser(run: Run): boolean {
  if (!NO_BROWSER.test(run.stderr)) return false;
  expect(process.env.KEW_UI_CHECK_REQUIRE_CHROMIUM, run.stderr).not.toBe('1');
  return true;
}

describe('the runner on a module whose route handler throws', () => {
  it('fails that check, runs the next, and prints the summary with exit 1', async (context) => {
    const run = await runner('route-throws');
    if (noBrowser(run)) return context.skip();
    const lines = run.stdout.split('\n');
    expect(lines, run.stdout + run.stderr).toContain('  FAIL  a route handler that throws fails its check');
    expect(run.stdout).toContain('a route or page handler threw: the handler threw on purpose');
    expect(run.stdout).not.toContain('the fetch was');
    expect(lines).toContain('  ok    the check after it still runs');
    expect(lines).toContain('ui-check: 1/2 checks passed');
    expect(run.status).toBe(1);
  }, 180_000);
});

describe('the runner on a module that calls process.exit(0)', () => {
  it('exits 1 with a FAIL line, never 0 without a summary', async (context) => {
    const run = await runner('exits-zero');
    if (noBrowser(run)) return context.skip();
    expect(run.stdout, run.stdout + run.stderr).toContain('  ok    a check before the exit');
    expect(run.stdout).toContain('  FAIL  ui-check exited before its summary');
    expect(run.stdout).not.toContain('checks passed');
    expect(run.status).toBe(1);
  }, 180_000);
});

/** Run `body` in a child node after `exitGuard()`; returns its exit status and output. */
function guarded(body: string): { status: number | null; stdout: string } {
  const script = [
    `const { exitGuard } = await import(${JSON.stringify(LIB)});`,
    'const guard = exitGuard();',
    body
  ].join('\n');
  const run = spawnSync(process.execPath, ['--input-type=module', '-e', script], { encoding: 'utf8' });
  return { status: run.status, stdout: run.stdout };
}

describe('exitGuard', () => {
  it('turns an exit 0 before the summary into exit 1', () => {
    const run = guarded('process.exit(0);');
    expect(run.status).toBe(1);
    expect(run.stdout).toContain('FAIL  ui-check exited before its summary');
  });

  it('lets the summary exit 0 once it is reached', () => {
    const run = guarded('guard.done(); process.exit(0);');
    expect(run.status).toBe(0);
    expect(run.stdout).toBe('');
  });

  it('leaves a failing exit as it is', () => {
    expect(guarded('process.exit(3);').status).toBe(3);
  });
});
