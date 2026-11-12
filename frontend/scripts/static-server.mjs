/**
 * The static server `npm run ui-check` runs the demo build behind (#3139).
 *
 * It serves `build-demo/` the way the demo host will: a file when the path
 * names one, `index.html` otherwise (Caddy's `try_files {path} /index.html`,
 * which is what makes a deep link like `/epics/<ref>` load), and every answer
 * carries the Content-Security-Policy `www/Caddyfile` sends — read from that
 * file, not copied — so a demo build that breaks the CSP fails in the browser
 * as a console error, here, before any host serves it.
 *
 * Node's standard library only: no dependency stands between the check and
 * what it checks.
 */
import { createReadStream, existsSync, readFileSync, statSync } from 'node:fs';
import { createServer } from 'node:http';
import { extname, join, normalize, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

export const CADDYFILE = new URL('../../www/Caddyfile', import.meta.url);

/**
 * The one CSP a Caddyfile's text sends, verbatim. A `#` comment line is not a
 * directive, so a commented-out policy is never served; and a file that sends
 * none, or more than one, throws rather than letting the check pick one —
 * `where` names the file in the sentence.
 */
export function cspIn(text, where = 'the Caddyfile') {
  const directives = text
    .split('\n')
    .map((line) => line.trim())
    .filter((line) => !line.startsWith('#') && /\bContent-Security-Policy\b(?!-)/.test(line));
  if (directives.length !== 1) {
    const count = directives.length === 0 ? 'no' : `${directives.length}`;
    throw new Error(`${where} sends ${count} Content-Security-Policy headers; the static server needs exactly one`);
  }
  const found = /Content-Security-Policy\s+"([^"]+)"/.exec(directives[0]);
  if (found === null) throw new Error(`${where} sends a Content-Security-Policy with no quoted policy: ${directives[0]}`);
  return found[1];
}

/** The CSP the site's Caddyfile sends, verbatim (see `cspIn`). */
export function cspFrom(caddyfile = CADDYFILE) {
  return cspIn(readFileSync(caddyfile, 'utf8'), fileURLToPath(caddyfile));
}

const TYPES = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.ico': 'image/x-icon',
  '.woff': 'font/woff',
  '.woff2': 'font/woff2',
  '.txt': 'text/plain; charset=utf-8',
  '.map': 'application/json'
};

/**
 * The file under `root` that answers `pathname`: the file it names, or the
 * SPA fallback. A path that would leave `root` is answered by the fallback,
 * never resolved against the disk outside it.
 */
export function fileFor(root, pathname) {
  let decoded;
  try {
    decoded = decodeURIComponent(pathname);
  } catch {
    decoded = '/';
  }
  const path = normalize(join(root, decoded));
  const inside = path === root || path.startsWith(root.endsWith(sep) ? root : `${root}${sep}`);
  if (inside && existsSync(path) && statSync(path).isFile()) return path;
  return join(root, 'index.html');
}

/**
 * Serve `root` on `127.0.0.1:port` (0 picks a free one). Resolves to
 * `{ base, close }` once listening.
 */
export function serveStatic(root, port = 0, csp = cspFrom()) {
  const dir = fileURLToPath(root);
  const server = createServer((req, res) => {
    const method = req.method ?? 'GET';
    if (method !== 'GET' && method !== 'HEAD') {
      res.writeHead(405, { Allow: 'GET, HEAD' }).end();
      return;
    }
    const file = fileFor(dir, new URL(req.url ?? '/', 'http://localhost').pathname);
    res.writeHead(200, {
      'Content-Type': TYPES[extname(file)] ?? 'application/octet-stream',
      'Content-Security-Policy': csp,
      'X-Content-Type-Options': 'nosniff',
      'Cache-Control': 'no-store'
    });
    if (method === 'HEAD') res.end();
    else createReadStream(file).pipe(res);
  });
  return new Promise((resolve, reject) => {
    server.on('error', reject);
    server.listen(port, '127.0.0.1', () => {
      const { port: bound } = server.address();
      resolve({
        base: `http://127.0.0.1:${bound}`,
        close: () => new Promise((done) => server.close(() => done()))
      });
    });
  });
}
