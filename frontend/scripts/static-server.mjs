/**
 * The static server `npm run ui-check` runs the demo build behind (#3139).
 *
 * It serves `build-demo/` the way the demo host's site block does
 * (`www/DEPLOY.md`; `www/Caddyfile` carries it commented out). A path that
 * names a file answers that file. A path under `/_app/` that names no file
 * answers 404: `/_app/` holds only the build's own files, so the page shell is
 * never the answer to a script's or a stylesheet's URL. Every other path
 * answers `index.html`, which is what makes a deep link like `/epics/<ref>`
 * load. Every answer the server writes, the 404, the 400 and the 405
 * included, carries the Content-Security-Policy `www/Caddyfile` sends — read
 * from that file, not copied — so a demo build that breaks the CSP fails in
 * the browser as a console error, here, before any host serves it.
 *
 * Node's standard library only: no dependency stands between the check and
 * what it checks.
 */
import { createReadStream, existsSync, readFileSync, statSync } from 'node:fs';
import { createServer } from 'node:http';
import { extname, join, posix, sep } from 'node:path';
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
 * The folder the build keeps its own files in: nothing under it is a page.
 * Compared in lower case, since the host's matcher ignores case.
 */
const ASSETS = '/_app/';

/**
 * The file under `root` that answers `pathname`, or `null` when the answer is
 * 404.
 *
 * The path is percent-decoded, then resolved from `/` as a URL path is: its
 * dot segments are removed (`/a/../b` is `/b`, and `/../b` is `/b` too, so no
 * path leaves `root`) and repeated slashes become one. On that resolved path:
 *
 *   - it names a file: that file;
 *   - it is under `/_app/`, in any letter case, and names no file (a
 *     directory names none): `null`;
 *   - anything else, `/_app` itself included: the SPA fallback, `index.html`.
 *
 * So `/../_app/x.js` and `//_APP/x.js` are missing assets, and
 * `/_app/../../etc/passwd` is a page path. A backslash is a character of a
 * name, never a separator.
 *
 * For the paths a browser sends, the demo block answers the same way on
 * Caddy 2.11: its `path` matcher compares the decoded path with dot segments
 * removed and slashes merged, ignoring case, and its `file` matcher asks the
 * disk, as the first rule does. Two kinds of path answer differently, and no
 * page asks for either. A path that cannot be decoded (`/_app/%`,
 * `/_app/%ff.js`) is never looked up on the disk here: it is resolved as
 * written, and is 404 under `/_app/` and the fallback elsewhere. Caddy
 * answers 400 when a `%` starts no escape. A file's name with a slash after
 * it (`/index.html/`) is the fallback here; Caddy redirects to the file.
 *
 * On a platform whose separator is not `/`, a path that climbs out of `root`
 * through that separator is never read from the disk either.
 */
export function fileFor(root, pathname) {
  let decoded = null;
  try {
    decoded = decodeURIComponent(pathname);
  } catch {
    // Decided below on the path as written; nothing is read from the disk.
  }
  const resolved = posix.normalize(`/${decoded ?? pathname}`);
  if (decoded !== null) {
    const path = join(root, resolved);
    const inside = path === root || path.startsWith(root.endsWith(sep) ? root : `${root}${sep}`);
    if (inside && existsSync(path) && statSync(path).isFile()) return path;
  }
  if (resolved.toLowerCase().startsWith(ASSETS)) return null;
  return join(root, 'index.html');
}

/**
 * The path of a request target, as written: what stands before its first `?`
 * or `#`. `null` when the target is not a path, which is one that does not
 * start with `/` (an absolute URL, `*`); Caddy serves those, and no browser
 * sends them to a site. The target is never parsed as a URL: read against a
 * base, `//_app/x.js` names a host and `//` is no URL at all. No browser
 * sends a `#` either; Caddy reads one as a character of the path.
 */
function pathOf(target) {
  return target.startsWith('/') ? target.split(/[?#]/, 1)[0] : null;
}

/**
 * Serve `root` on `127.0.0.1:port` (0 picks a free one): the file `fileFor`
 * names, 404 when it names none, 400 for a request target that is not a path
 * and 405 for a method other than GET and HEAD, those three with no body.
 * The method is refused before the path is read; the host reads the path
 * first, so it answers 404 where this answers 405.
 * Every answer carries the same three headers. A request line Node's own
 * parser refuses never arrives here: Node answers it 400 without them.
 * Resolves to `{ base, close }` once listening.
 */
export function serveStatic(root, port = 0, csp = cspFrom()) {
  const dir = fileURLToPath(root);
  const headers = { 'Content-Security-Policy': csp, 'X-Content-Type-Options': 'nosniff', 'Cache-Control': 'no-store' };
  const server = createServer((req, res) => {
    const method = req.method ?? 'GET';
    if (method !== 'GET' && method !== 'HEAD') {
      res.writeHead(405, { Allow: 'GET, HEAD', ...headers }).end();
      return;
    }
    const pathname = pathOf(req.url ?? '/');
    if (pathname === null) {
      res.writeHead(400, headers).end();
      return;
    }
    const file = fileFor(dir, pathname);
    if (file === null) {
      res.writeHead(404, headers).end();
      return;
    }
    res.writeHead(200, { 'Content-Type': TYPES[extname(file)] ?? 'application/octet-stream', ...headers });
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
