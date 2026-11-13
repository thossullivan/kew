/**
 * The demo phase of `npm run ui-check` (#3139), read without a browser: which
 * modules run against which server, when the demo is built, what a normal
 * build may not hold, and how the static server answers.
 */
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { afterEach, describe, expect, it } from 'vitest';
import { DEMO_BANNER, DEMO_REFUSAL } from '../src/lib/demo/banner.js';
import { DEMO_SENTINEL } from '../src/lib/demo/sentinel.js';
import {
  buildLeaks,
  buildSentinels,
  checkModules,
  demoBuildNeeded,
  inPhase,
  isDemoModule,
  npmEnv,
  runChecks
} from './ui-check-lib.mjs';
import { cspFrom, cspIn, fileFor, serveStatic } from './static-server.mjs';

const temps: string[] = [];
afterEach(() => {
  for (const dir of temps.splice(0)) rmSync(dir, { recursive: true, force: true });
});
function tempDir(files: Record<string, string>): string {
  const dir = mkdtempSync(join(tmpdir(), 'ui-check-demo-'));
  temps.push(dir);
  for (const [name, body] of Object.entries(files)) {
    mkdirSync(join(dir, name, '..'), { recursive: true });
    writeFileSync(join(dir, name), body);
  }
  return dir;
}

describe('the two phases', () => {
  const names = ['demo-static.mjs', 'routes.mjs', 'work-rows.mjs'];

  it('runs demo-* modules only in the demo phase and the rest only in the dev phase', () => {
    expect(inPhase(names, 'demo')).toEqual(['demo-static.mjs']);
    expect(inPhase(names, 'dev')).toEqual(['routes.mjs', 'work-rows.mjs']);
    expect(inPhase(names, undefined)).toEqual(names);
  });

  it('runs one phase of the folder through runChecks', async () => {
    const seen: string[] = [];
    const dir = tempDir({
      'demo-a.mjs': 'export async function checks({ check }) { await check("demo"); }',
      'b.mjs': 'export async function checks({ check }) { await check("dev"); }'
    });
    const record = { check: async (name: string) => seen.push(name) };
    expect(await runChecks(record, pathToFileURL(`${dir}/`), [], 'demo')).toEqual(['demo-a.mjs']);
    expect(await runChecks(record, pathToFileURL(`${dir}/`), [], 'dev')).toEqual(['b.mjs']);
    expect(seen).toEqual(['demo', 'dev']);
  });

  it('has a demo module in the real folder', () => {
    expect(checkModules().filter(isDemoModule)).toContain('demo-static.mjs');
  });

  it('runs the normal build without the demo build switch, and leaves the rest of the environment', () => {
    const env = { KEW_DEMO_BUILD: '1', PATH: '/bin', KEW_UI_FIXTURES: '1' };
    expect(npmEnv('build', env)).toEqual({ PATH: '/bin', KEW_UI_FIXTURES: '1' });
    expect(npmEnv('build:demo', env)).toEqual(env);
    expect(env.KEW_DEMO_BUILD).toBe('1');
  });

  it('builds the demo on a default run and on a scoped run naming a demo module only', () => {
    expect(demoBuildNeeded([])).toBe(true);
    expect(demoBuildNeeded(['work-rows', 'demo-static'])).toBe(true);
    expect(demoBuildNeeded(['work-rows'])).toBe(false);
  });
});

describe('what the normal build may not hold', () => {
  it("names the demo sentinel, the demo's own words, driver.js and a fixture-only run id", () => {
    const sentinels = buildSentinels().map((s) => s.sentinel);
    expect(sentinels).toEqual([
      DEMO_SENTINEL,
      'Demo: synthetic data. kew runs coding agents headless; you make the merge calls.',
      'Demo: this would',
      'driver-popover',
      '9c41e2b7'
    ]);
  });

  it('finds each, in any file at any depth, and nothing in a clean tree', () => {
    const clean = tempDir({ 'index.html': '<html></html>', '_app/a.js': 'console.log(1)' });
    expect(buildLeaks(pathToFileURL(`${clean}/`))).toEqual([]);
    const dirty = tempDir({
      'index.html': `<html data-x="${DEMO_SENTINEL}"></html>`,
      '_app/immutable/c.css': '.driver-popover{}',
      '_app/immutable/d.js': 'const run="9c41e2b7"',
      // A shell file that imported the banner and the write table (#3139):
      // the minified chunk holds their words, not the shim's sentinel.
      '_app/immutable/nodes/2.x.js': `p("${DEMO_BANNER}");const s=\`${DEMO_REFUSAL} \${d}.\``
    });
    expect(buildLeaks(pathToFileURL(`${dirty}/`))).toEqual([
      `_app/immutable/c.css: driver.js's shipped class name (driver-popover)`,
      `_app/immutable/d.js: a fixture-only run id (9c41e2b7)`,
      `_app/immutable/nodes/2.x.js: the demo banner's sentence (${DEMO_BANNER})`,
      `_app/immutable/nodes/2.x.js: a demo refusal's sentence (${DEMO_REFUSAL})`,
      `index.html: the demo flag sentinel (${DEMO_SENTINEL})`
    ]);
  });
});

describe('the static demo server', () => {
  it("sends the site's own CSP", () => {
    expect(cspFrom()).toMatch(/^default-src 'self'; /);
  });

  it('reads the one CSP directive and never a commented-out one', () => {
    const caddyfile = [
      'example.com {',
      '\theader {',
      '\t\t# Content-Security-Policy "default-src *"',
      '\t\tContent-Security-Policy "default-src \'self\'"',
      '\t\tX-Content-Type-Options "nosniff"',
      '\t}',
      '}'
    ].join('\n');
    expect(cspIn(caddyfile)).toBe("default-src 'self'");
  });

  it('throws a sentence when the Caddyfile sends no CSP, or more than one', () => {
    expect(() => cspIn('header {\n\t# Content-Security-Policy "default-src *"\n}', 'www/Caddyfile')).toThrow(
      'www/Caddyfile sends no Content-Security-Policy headers; the static server needs exactly one'
    );
    const two = [
      'a {',
      '\theader Content-Security-Policy "default-src \'self\'"',
      '}',
      'b {',
      '\theader Content-Security-Policy "default-src *"',
      '}'
    ].join('\n');
    expect(() => cspIn(two, 'www/Caddyfile')).toThrow(
      'www/Caddyfile sends 2 Content-Security-Policy headers; the static server needs exactly one'
    );
    expect(() => cspIn('header Content-Security-Policy-Report-Only "x"')).toThrow(/sends no Content-Security-Policy/);
  });

  it('answers a file it has, and index.html for anything else', () => {
    const root = tempDir({ 'index.html': 'shell', '_app/a.js': 'js' });
    expect(fileFor(root, '/_app/a.js')).toBe(join(root, '_app/a.js'));
    expect(fileFor(root, '/epics/launch-fixes')).toBe(join(root, 'index.html'));
    expect(fileFor(root, '/../../etc/passwd')).toBe(join(root, 'index.html'));
    expect(fileFor(root, '/%2e%2e/%2e%2e/etc/passwd')).toBe(join(root, 'index.html'));
  });

  it('serves the SPA fallback with the CSP header', async () => {
    const root = tempDir({ 'index.html': 'shell', '_app/a.js': 'js' });
    const server = await serveStatic(pathToFileURL(`${root}/`), 0, "default-src 'self'");
    try {
      const deep = await fetch(`${server.base}/epics/launch-fixes`);
      expect(deep.status).toBe(200);
      expect(await deep.text()).toBe('shell');
      expect(deep.headers.get('content-security-policy')).toBe("default-src 'self'");
      const js = await fetch(`${server.base}/_app/a.js`);
      expect(js.headers.get('content-type')).toMatch(/^text\/javascript/);
      expect(await js.text()).toBe('js');
      expect((await fetch(`${server.base}/x`, { method: 'POST' })).status).toBe(405);
    } finally {
      await server.close();
    }
  });
});
