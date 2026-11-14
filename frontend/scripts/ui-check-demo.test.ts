/**
 * The demo phase of `npm run ui-check` (#3139), read without a browser: which
 * modules run against which server, when the demo is built, what a normal
 * build may not hold, and how the static server answers.
 */
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { request, type IncomingHttpHeaders } from 'node:http';
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

/**
 * One GET whose request line carries `target` exactly as written. `fetch`
 * parses its URL first, so it cannot send `//x`, `/\` or a bare `%`.
 */
function rawGet(base: string, target: string): Promise<{ status: number; headers: IncomingHttpHeaders; body: string }> {
  const { hostname, port } = new URL(base);
  return new Promise((resolve, reject) => {
    const sent = request({ hostname, port, path: target, method: 'GET', agent: false }, (res) => {
      let body = '';
      res.setEncoding('utf8');
      res.on('data', (chunk) => (body += chunk));
      res.on('end', () => resolve({ status: res.statusCode ?? 0, headers: res.headers, body }));
    });
    sent.on('error', reject);
    sent.end();
  });
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

  it('answers a file it has, and index.html for any other path outside /_app/', () => {
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

  it('names no file for a missing path under /_app/, a directory there included', () => {
    const root = tempDir({ 'index.html': 'shell', '_app/immutable/a.js': 'js' });
    expect(fileFor(root, '/_app/immutable/a.js')).toBe(join(root, '_app/immutable/a.js'));
    expect(fileFor(root, '/_app/immutable/x.js')).toBeNull();
    expect(fileFor(root, '/_app/version.json')).toBeNull();
    expect(fileFor(root, '/_app/immutable')).toBeNull();
    expect(fileFor(root, '/_app/')).toBeNull();
    // `/_app` itself is not under `/_app/`: it is a page path like any other.
    expect(fileFor(root, '/_app')).toBe(join(root, 'index.html'));
    expect(fileFor(root, '/_application/x.js')).toBe(join(root, 'index.html'));
  });

  it('decides /_app/ on the path with its dot segments resolved, which never leaves the root', () => {
    const root = tempDir({ 'index.html': 'shell', '_app/immutable/a.js': 'js' });
    // Resolved into /_app/: missing there, so no file.
    expect(fileFor(root, '/../_app/immutable/x.js')).toBeNull();
    expect(fileFor(root, '/epics/../_app/immutable/x.js')).toBeNull();
    expect(fileFor(root, '/_app%2Fimmutable%2Fx.js')).toBeNull();
    expect(fileFor(root, '/%2e%2e/_app/immutable/x.js')).toBeNull();
    // Resolved into /_app/ and there: the file.
    expect(fileFor(root, '/../../_app/immutable/a.js')).toBe(join(root, '_app/immutable/a.js'));
    // Resolved out of /_app/: a page path, so the shell.
    expect(fileFor(root, '/_app/../epics/launch-fixes')).toBe(join(root, 'index.html'));
    expect(fileFor(root, '/_app/../../etc/passwd')).toBe(join(root, 'index.html'));
    expect(fileFor(root, '/_app/%2e%2e/%2e%2e/etc/passwd')).toBe(join(root, 'index.html'));
  });

  it('answers 404 for a missing file under /_app/, never the shell', async () => {
    const root = tempDir({ 'index.html': 'shell', '_app/immutable/a.js': 'js' });
    const server = await serveStatic(pathToFileURL(`${root}/`), 0, "default-src 'self'");
    try {
      for (const path of ['/_app/immutable/x.js', '/_app/version.json', '/_app/immutable', '/_app%2Fimmutable%2Fx.js']) {
        const missing = await fetch(`${server.base}${path}`);
        expect(missing.status, path).toBe(404);
        expect(await missing.text(), path).toBe('');
        expect(missing.headers.get('content-security-policy'), path).toBe("default-src 'self'");
        expect(missing.headers.get('x-content-type-options'), path).toBe('nosniff');
        expect(missing.headers.get('cache-control'), path).toBe('no-store');
      }
      expect((await fetch(`${server.base}/_app/immutable/x.js`, { method: 'HEAD' })).status).toBe(404);
    } finally {
      await server.close();
    }
  });

  it('still answers a file under /_app/ that exists, and the shell for a deep link', async () => {
    const root = tempDir({ 'index.html': 'shell', '_app/immutable/a.js': 'js' });
    const server = await serveStatic(pathToFileURL(`${root}/`), 0, "default-src 'self'");
    try {
      const chunk = await fetch(`${server.base}/_app/immutable/a.js`);
      expect(chunk.status).toBe(200);
      expect(chunk.headers.get('content-type')).toMatch(/^text\/javascript/);
      expect(chunk.headers.get('content-security-policy')).toBe("default-src 'self'");
      expect(chunk.headers.get('x-content-type-options')).toBe('nosniff');
      expect(await chunk.text()).toBe('js');
      for (const path of ['/', '/runs/abc', '/_app', '/missing.js']) {
        const page = await fetch(`${server.base}${path}`);
        expect(page.status, path).toBe(200);
        expect(page.headers.get('content-type'), path).toMatch(/^text\/html/);
        expect(await page.text(), path).toBe('shell');
      }
    } finally {
      await server.close();
    }
  });

  it('reads /_app/ in any case, as the host does', async () => {
    const root = tempDir({ 'index.html': 'shell', '_app/immutable/a.js': 'js' });
    expect(fileFor(root, '/_APP/immutable/zz.js')).toBeNull();
    expect(fileFor(root, '/_App/Version.json')).toBeNull();
    expect(fileFor(root, '/_APPLICATION/zz.js')).toBe(join(root, 'index.html'));
    const server = await serveStatic(pathToFileURL(`${root}/`), 0, "default-src 'self'");
    try {
      const missing = await fetch(`${server.base}/_APP/immutable/zz.js`);
      expect(missing.status).toBe(404);
      expect(await missing.text()).toBe('');
    } finally {
      await server.close();
    }
  });

  it('decides a path it cannot decode as it is written: 404 under /_app/, the shell elsewhere', async () => {
    const root = tempDir({ 'index.html': 'shell', '_app/immutable/a.js': 'js', '_app/%zz.js': 'never served' });
    for (const path of ['/_app/%ff.js', '/_app/%', '/_app/immutable/%zz.js', '/_APP/%', '/epics/../_app/%', '//_app/%ff.js']) {
      expect(fileFor(root, path), path).toBeNull();
    }
    // The undecoded text is never looked up on the disk, even when a file has that name.
    expect(fileFor(root, '/_app/%zz.js')).toBeNull();
    for (const path of ['/%', '/epics/%zz', '/_app/../%ff']) {
      expect(fileFor(root, path), path).toBe(join(root, 'index.html'));
    }
    const server = await serveStatic(pathToFileURL(`${root}/`), 0, "default-src 'self'");
    try {
      for (const path of ['/_app/%ff.js', '/_app/%', '/_app/immutable/%zz.js']) {
        const missing = await rawGet(server.base, path);
        expect(missing.status, path).toBe(404);
        expect(missing.body, path).toBe('');
      }
      const page = await rawGet(server.base, '/epics/%zz');
      expect(page.status).toBe(200);
      expect(page.body).toBe('shell');
    } finally {
      await server.close();
    }
  });

  it('takes the path from the request target as written, so a leading // is a path and never a host', async () => {
    const root = tempDir({ 'index.html': 'shell', '_app/immutable/a.js': 'js' });
    const server = await serveStatic(pathToFileURL(`${root}/`), 0, "default-src 'self'");
    try {
      const missing = await rawGet(server.base, '//_app/immutable/zz.js');
      expect(missing.status).toBe(404);
      expect(missing.body).toBe('');
      const chunk = await rawGet(server.base, '//_app/immutable/a.js');
      expect(chunk.status).toBe(200);
      expect(chunk.body).toBe('js');
      const query = await rawGet(server.base, '/_app/immutable/a.js?v=1#top');
      expect(query.status).toBe(200);
      expect(query.body).toBe('js');
      const tour = await rawGet(server.base, '/runs/abc?tour=refused-pr&step=1');
      expect(tour.status).toBe(200);
      expect(tour.body).toBe('shell');
    } finally {
      await server.close();
    }
  });

  it('answers every request target, and 400 for one that is not a path', async () => {
    const root = tempDir({ 'index.html': 'shell', '_app/immutable/a.js': 'js' });
    const server = await serveStatic(pathToFileURL(`${root}/`), 0, "default-src 'self'");
    try {
      // Each of these, read as a URL against a base, is an invalid one.
      for (const target of ['//', '/\\', '//[', '//?x=1']) {
        const page = await rawGet(server.base, target);
        expect(page.status, target).toBe(200);
        expect(page.body, target).toBe('shell');
      }
      for (const target of ['http://other.example/_app/immutable/a.js', '*']) {
        const refused = await rawGet(server.base, target);
        expect(refused.status, target).toBe(400);
        expect(refused.body, target).toBe('');
        expect(refused.headers['content-security-policy'], target).toBe("default-src 'self'");
        expect(refused.headers['x-content-type-options'], target).toBe('nosniff');
      }
      // Node's own parser refuses these request lines before the server sees them.
      for (const target of ['runs/abc', '?x=1']) {
        expect((await rawGet(server.base, target)).status, target).toBe(400);
      }
      // The server is still there.
      expect((await fetch(`${server.base}/_app/immutable/a.js`)).status).toBe(200);
    } finally {
      await server.close();
    }
  });

  it('sends the CSP on a refused method too', async () => {
    const root = tempDir({ 'index.html': 'shell' });
    const server = await serveStatic(pathToFileURL(`${root}/`), 0, "default-src 'self'");
    try {
      const refused = await fetch(`${server.base}/x`, { method: 'POST' });
      expect(refused.status).toBe(405);
      expect(refused.headers.get('allow')).toBe('GET, HEAD');
      expect(refused.headers.get('content-security-policy')).toBe("default-src 'self'");
      expect(refused.headers.get('x-content-type-options')).toBe('nosniff');
      expect(await refused.text()).toBe('');
    } finally {
      await server.close();
    }
  });
});
