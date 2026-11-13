#!/usr/bin/env node
/**
 * The demo's leak scan (#3141): one command, one allowlist file.
 *
 *     node frontend/demo/leak-scan.mjs <dir> [--bundle]
 *
 * The demo site is public, so nothing in it may name a person, a machine, a
 * credential or the real factory. It prints one line per finding and exits 1
 * when there is any. Every file under `<dir>` is read, whatever its extension:
 * UTF-32 or UTF-16 by its byte-order mark (its raw bytes read too, and a mark
 * in front of bytes that are not text refused), else UTF-8, and text holding
 * NULs (UTF-16 with no mark) is read again with them removed. The demo and
 * its build are UTF-8, so Latin-1 is never guessed: any other bytes are a
 * finding, an archive or compressed stream it does not unpack (zip, xz,
 * bzip2, 7z, zstd, zlib) named by its signature. Gzip is read decompressed by
 * its signature, whatever the name, nested up to four layers, every byte
 * consumed, each member's name and comment fields read too and a member with
 * an extra field refused; a `.br` file decompressed; a `.gz` or `.br` file
 * that does not decompress, as its raw bytes only when they are UTF-8 text.
 * Compressed bytes it cannot read whole are a finding. Only a font, an
 * image or WebAssembly whose extension AND signature agree is a skipped
 * binary, and an image's printable strings are still read for paths, emails,
 * tokens and the denylist. A symbolic link is never followed but counted; a
 * FIFO, socket or device is a finding, never read. Nothing is dropped
 * silently, and a tree with no file read fails.
 *
 * Source rules (the default, for `frontend/demo/`) refuse:
 *   - a local path: a home directory (macOS's Users root, Linux's home root,
 *     a tilde, root's own home, a Windows profile, and the dash-joined slug
 *     some tools name a directory after one), macOS's per-user temp root
 *     under var/folders, the system temp directory (private or not), a
 *     mounted volume;
 *   - an email address;
 *   - a token shape, each as a full pattern — never a bare `sk-`, which
 *     matches "task-";
 *   - a host the allowlist does not name: in a URL (an IP literal included),
 *     a bare IP literal, and a bare dotted host name whose last label is a
 *     public or private top-level domain (so `manifest.json`, `kew.toml` and
 *     `0.9.0` are file names and versions, never hosts);
 *   - a GitHub owner other than the invented one, in a link or in a
 *     repository field (`repo`, `delivery_repo`, `project.repo`, …);
 *   - every epic ref, run id and owner of the real factory: the terms
 *     `fixtures/factory/` names, read at scan time, UNIONED with the committed
 *     `leak-scan.deny.json` (which names the real repository and owner too),
 *     so editing the fixtures alone can never shrink the list. A term matches
 *     with no letter or digit beside it: `epic-<ref>-notes` holds the ref.
 *
 * Bundle rules (`--bundle`, for a built `build-demo/`) refuse paths, emails,
 * tokens, the denylist, repository fields and URL hosts off `bundle_hosts`
 * (the vendor hosts framework runtimes carry), allow a pull-request link on
 * any owner's repository named `bundle_repo`
 * (`github.com/<any owner>/<bundle_repo>/pull/<n>`: this rule cannot tell
 * kew's own repository from a fork or a namesake; the provenance schema,
 * which pins every PR link to `public-repo.json`, is the guard there), and
 * refuse a bare
 * host only on a private top-level domain (`.local`, `.internal`, …) or a
 * tailnet (`.ts.net`), in any context, comments included — except a minified
 * member read: two labels whose first is one or two letters, not opening a
 * string (d3 reads an attribute namespace's `local` that way). Public-TLD bare names are left to
 * source mode, which scans every string the bundle takes from the world.
 *
 * No file is exempt whole. The release provenance carries real names by
 * design in a few named values, and only those values skip a rule: each
 * `exempt` entry in the allowlist names one file by exact path (relative to
 * `frontend/demo/` in source mode, to the root of `build-demo/` in bundle
 * mode), the one mode it holds in, and per key path the rules its values
 * skip — `owner` (the owner and repository rules) and/or `denylist`. A key
 * path is a JSON value's path, `[]` for an array element
 * (`features[].prs[].url`), or a TOML table's one-line key
 * (`features[].epics`). Every other value of the file, and every other rule —
 * paths, emails, tokens, hosts — holds as anywhere else (#3144):
 *   - `provenance/provenance.json`, `provenance/runs.json` (source): the
 *     real export's PR links (on the real owner) skip both rules, its epic
 *     refs and run ids the denylist;
 *   - `features.toml` (source): its `epics` skip the denylist;
 *   - `public-repo.json` (source): its `repo` skips both rules;
 *   - `demo-provenance.json` (bundle): the bundled copy's `epics` skip the
 *     denylist. Its PR links skip nothing: a `<any owner>/<bundle_repo>`
 *     pull-request link passes every bundle (above), any other link is
 *     refused, and which owner it may be is the schema's to enforce.
 * The committed samples are exempt from nothing: they link to the invented
 * owner's `kew` and name invented epics.
 */
import { isUtf8 } from 'node:buffer';
import { readFileSync, readdirSync, realpathSync, statSync } from 'node:fs';
import { join, relative, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { brotliDecompressSync, gunzipSync, inflateRawSync } from 'node:zlib';

export const ALLOWLIST = new URL('./leak-scan.allow.json', import.meta.url);
export const DENYLIST = new URL('./leak-scan.deny.json', import.meta.url);
export const FACTORY_DIR = new URL('../fixtures/factory/', import.meta.url);

/** Never scanned in source mode: dependencies and build caches. A bundle skips no directory: all of it ships. */
const SKIP_DIRS = new Set(['node_modules', '.svelte-kit', '.git']);
/** The committed denylist names the real factory by design; it is exempt from the denylist only. */
const DENYLIST_FILE = 'leak-scan.deny.json';

/** Never letter, digit or `_` before a token: `task-…` is not an OpenAI key. */
const T = '(?<![A-Za-z0-9_])';
/** Credential shapes, each a whole pattern. */
export const TOKEN_PATTERNS = [
  ['an Anthropic key', new RegExp(`${T}sk-ant-[A-Za-z0-9_-]{20,}`, 'g')],
  ['an OpenRouter key', new RegExp(`${T}sk-or-v1-[0-9a-f]{64}`, 'g')],
  ['an OpenAI key', new RegExp(`${T}sk-(?:proj|svcacct|admin)-[A-Za-z0-9_-]{20,}`, 'g')],
  ['an OpenAI key', new RegExp(`${T}sk-[A-Za-z0-9]{32,}`, 'g')],
  ['a GitHub token', new RegExp(`${T}gh[pousr]_[A-Za-z0-9]{36}`, 'g')],
  ['a GitHub fine-grained token', new RegExp(`${T}github_pat_\\w{20,}`, 'g')],
  ['a GitLab token', new RegExp(`${T}glpat-[A-Za-z0-9_-]{20,}`, 'g')],
  ['a Google API key', new RegExp(`${T}AIza[0-9A-Za-z_-]{35}`, 'g')],
  ['an npm token', new RegExp(`${T}npm_[A-Za-z0-9]{36}`, 'g')],
  ['a Linear key', new RegExp(`${T}lin_api_\\w{20,}`, 'g')],
  ['a Slack token', new RegExp(`${T}xox[bpas]-[\\w-]{10,}`, 'g')],
  ['an AWS access key', new RegExp(`${T}AKIA[0-9A-Z]{16}`, 'g')],
  ['a private key', /-----BEGIN [A-Z ]*PRIVATE KEY-----/g]
];
/**
 * Local paths that name a person or a machine. Written with escaped slashes,
 * so this file's own source never holds the path it refuses.
 */
export const PATH_PATTERNS = [
  ['a home path', /\/(?:Users|home)\/[^\s"'`<>)]*/g],
  ['a home path', /(?<![\w~./-])~\/[A-Za-z0-9._-][^\s"'`<>)]*/g],
  ['a home path', /(?<![\w.-])\/root\/[^\s"'`<>)]*/g],
  ['a home path', /\b[A-Za-z]:(?:\\\\|\\)Users(?:\\\\|\\)[^\s"'`<>)]*/g],
  // A path slug some tools name a directory by: `-Users-<name>-Projects-…`.
  ['a home path', /(?<=^|[/"'`\s])-(?:Users|home)-[A-Za-z0-9._]+-[^\s"'`<>)]*/g],
  ['a temp path', /\/var\/folders\/[^\s"'`<>)]*/g],
  ['a temp path', /(?<![\w.-])\/(?:private\/)?tmp\/[^\s"'`<>)]*/g],
  ['a mounted volume path', /\/Volumes\/[^\s"'`<>)]*/g]
];
const EMAIL = /[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}/g;
const URL_HOST = /\b[a-z][a-z0-9+.-]*:\/\/([A-Za-z0-9.-]+)/g;
const OCTET = '(?:25[0-5]|2[0-4]\\d|1\\d\\d|[1-9]?\\d)';
const IPV4 = new RegExp(`^${OCTET}(?:\\.${OCTET}){3}$`);
const BARE_IP = new RegExp(`(?<![\\w.])${OCTET}(?:\\.${OCTET}){3}(?![\\w.])`, 'g');
/** A dotted name: labels of letters, digits and inner hyphens. */
const BARE_HOST = /(?<![\w.@/:-])((?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\.)+([A-Za-z]{2,}))(?![\w-]|\.[A-Za-z0-9])/g;
/** Top-level domains no public site uses: a machine's own name. */
export const PRIVATE_TLDS = new Set(['local', 'internal', 'lan', 'corp', 'home', 'localdomain', 'intranet', 'localhost']);
/** Public suffixes that only ever name a private machine: a Tailscale tailnet. */
export const PRIVATE_SUFFIXES = ['.ts.net'];
/**
 * Public top-level domains a leaked host would carry. Deliberately not every
 * one: `.md`, `.py`, `.sh`, `.rs`, `.js`-shaped ones are file extensions here.
 */
export const PUBLIC_TLDS = new Set(['com', 'net', 'org', 'io', 'dev', 'ai', 'app', 'co', 'cloud', 'xyz', 'info', 'me', 'us', 'uk', 'de', 'tech', 'site', 'online', 'gov', 'edu']);
const GITHUB_PATH = /github\.com[/:]([A-Za-z0-9][A-Za-z0-9-]*)(?:\/([A-Za-z0-9._-]+)(?:\/pull\/(\d+))?)?/g;
/** A repository field and its `owner/name` value, quoted, in JSON or in code. */
const REPO_FIELD = /["']?\b([A-Za-z_.]*repo(?:sitory)?)["']?\s*:\s*["']([A-Za-z0-9_.-]+)\/([A-Za-z0-9_.-]+)["']/g;
const REPO_KEY = /(?:^|[._])repo(?:sitory)?$/i;
const OWNER_REPO = /^([A-Za-z0-9_.-]+)\/([A-Za-z0-9_.-]+)$/;

/** The allowlist file, read. */
export function loadAllowlist(url = ALLOWLIST) {
  return JSON.parse(readFileSync(url, 'utf8'));
}

/** Every value under `key` anywhere in `value`. */
function valuesOf(value, keys, out) {
  if (Array.isArray(value)) for (const item of value) valuesOf(item, keys, out);
  else if (value !== null && typeof value === 'object') {
    for (const [k, v] of Object.entries(value)) {
      if (keys.has(k) && typeof v === 'string' && v !== '') out.push([k, v]);
      valuesOf(v, keys, out);
    }
  }
  return out;
}

/**
 * Every regular file under `dir`, below no directory `seen.skipDirs` names;
 * each symbolic link, never followed, goes to `seen.links`, and each FIFO,
 * socket or device, never read, to `seen.specials`.
 */
function filesUnder(dir, seen = { links: [], specials: [], skipDirs: SKIP_DIRS }, root = dir) {
  return readdirSync(dir, { withFileTypes: true })
    .flatMap((entry) => {
      const path = join(dir, entry.name);
      const rel = relative(root, path).split(sep).join('/');
      if (entry.isSymbolicLink()) {
        seen.links.push(rel);
        return [];
      }
      if (entry.isDirectory()) return seen.skipDirs.has(entry.name) ? [] : filesUnder(path, seen, root);
      if (entry.isFile()) return [rel];
      seen.specials.push(rel);
      return [];
    })
    .sort();
}

/** The committed denylist: the real factory's names, whatever `fixtures/factory/` holds. */
export function committedDenylist(url = DENYLIST) {
  const deny = JSON.parse(readFileSync(url, 'utf8'));
  return [
    ...deny.repositories.map((term) => [term, 'a real repository']),
    ...deny.owners.map((term) => [term, 'a factory owner']),
    ...deny.epic_refs.map((term) => [term, 'a factory epic ref']),
    ...deny.run_ids.map((term) => [term, 'a factory run id'])
  ];
}

/**
 * The factory denylist: every epic ref, run id and owner the factory set
 * names — owners from GitHub links and `owner/name` repository fields, run
 * ids from every `run_id` — and every term of the committed denylist.
 */
export function factoryDenylist(dir = FACTORY_DIR, committed = DENYLIST) {
  const root = fileURLToPath(dir);
  const terms = new Map();
  const add = (term, what) => {
    if (term && !terms.has(term)) terms.set(term, what);
  };
  for (const [term, what] of committedDenylist(committed)) add(term, what);
  for (const file of filesUnder(root).filter((f) => f.endsWith('.json'))) {
    const text = readFileSync(join(root, file), 'utf8');
    for (const [key, value] of valuesOf(JSON.parse(text), new Set(['run_id', 'epic_ref', 'delivery_repo', 'repo']), [])) {
      if (key === 'run_id') add(value, 'a factory run id');
      else if (key === 'epic_ref') add(value, 'a factory epic ref');
      else if (OWNER_REPO.test(value)) add(value.split('/')[0], 'a factory owner');
    }
    for (const match of text.matchAll(GITHUB_PATH)) add(match[1], 'a factory owner');
  }
  return terms;
}

const escape = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
/** `term` as a whole word: no letter or digit on either side (`-`, `_` and `/` are boundaries). */
const wordPattern = (term) => new RegExp(`(?<![A-Za-z0-9])${escape(term)}(?![A-Za-z0-9])`, 'g');

/** `file` as a path under `frontend/demo/`, when it is given relative to the repo or `frontend/`. */
const underDemo = (file) => file.replace(/^(?:frontend\/)?demo\//, '');

/**
 * The `{ <key path>: [<rule>, …] }` exemption of `file` in `mode`, or null.
 * Exact paths only: in source mode relative to the scanned dir or to
 * `frontend/demo/`, in bundle mode relative to the bundle's root and nothing
 * else, so a stray copy anywhere else is scanned in full.
 */
export function exemptionOf(file, allow, mode) {
  for (const entry of allow.exempt ?? []) {
    if (entry.mode !== mode) continue;
    if (file === entry.path || (mode === 'source' && underDemo(file) === entry.path)) return entry.keys;
  }
  return null;
}

/**
 * Every string value of a JSON document, as `{ path, start, end }`: its key
 * path and its span, quotes included. A key holding `.`, `[` or `]` gives
 * itself and everything below it no path at all, so a key spelled like a
 * path (`"prs[].url"`, a top-level `"features[].prs[].url"`) can never land
 * on an exemption.
 */
function jsonValueSpans(text) {
  const out = [];
  let i = 0;
  const ws = () => {
    while (i < text.length && /\s/.test(text[i])) i += 1;
  };
  const string = () => {
    const start = i;
    for (i += 1; i < text.length && text[i] !== '"'; i += text[i] === '\\' ? 2 : 1);
    i += 1;
    return [start, i];
  };
  const value = (path) => {
    ws();
    const open = text[i];
    if (open === '{' || open === '[') {
      const close = open === '{' ? '}' : ']';
      i += 1;
      ws();
      while (text[i] !== close) {
        if (open === '{') {
          const [start, end] = string();
          const key = JSON.parse(text.slice(start, end));
          ws();
          i += 1; // the colon
          const pathless = path === null || /[.[\]]/.test(key);
          value(pathless ? null : path === '' ? key : `${path}.${key}`);
        } else value(path === null ? null : `${path}[]`);
        ws();
        if (text[i] === ',') i += 1;
        ws();
      }
      i += 1;
    } else if (open === '"') {
      const [start, end] = string();
      if (path !== null) out.push({ path, start, end });
    } else {
      while (i < text.length && !/[\s,\]}]/.test(text[i])) i += 1;
    }
  };
  value('');
  return out;
}

/** Every one-line TOML value, as `{ path, start, end }`: `<table>[].<key>` under `[[table]]`, the quoted string or list only. */
function tomlValueSpans(text) {
  const out = [];
  let table = '';
  let offset = 0;
  for (const line of text.split('\n')) {
    const header = /^\s*\[\[([A-Za-z0-9_.-]+)\]\]\s*$/.exec(line) ?? /^\s*\[([A-Za-z0-9_.-]+)\]\s*$/.exec(line);
    if (header) table = line.includes('[[') ? `${header[1]}[]` : header[1];
    else {
      const kv = /^(\s*([A-Za-z0-9_-]+)\s*=\s*)(\[[^\]\n]*\]|"[^"\n]*")/.exec(line);
      if (kv) {
        const start = offset + kv[1].length;
        out.push({ path: table === '' ? kv[2] : `${table}.${kv[2]}`, start, end: start + kv[3].length });
      }
    }
    offset += line.length + 1;
  }
  return out;
}

/** The spans of `text` (the file `file`) whose values `keys` exempt, each with the rules it skips. */
function exemptSpans(text, file, keys) {
  let values = [];
  if (file.endsWith('.json') && jsonOf(text) !== undefined) values = jsonValueSpans(text);
  else if (file.endsWith('.toml')) values = tomlValueSpans(text);
  return values
    .filter(({ path }) => Object.prototype.hasOwnProperty.call(keys, path))
    .map(({ path, start, end }) => ({ start, end, rules: keys[path] }));
}

function lineOf(text, index) {
  let line = 1;
  for (let i = 0; i < index; i += 1) if (text.charCodeAt(i) === 10) line += 1;
  return line;
}

/** Every `owner/name` string a JSON document holds under a repository key, at any depth below it. */
function repoValues(value, out = [], under = false) {
  if (typeof value === 'string') {
    if (under && OWNER_REPO.test(value)) out.push(value);
  } else if (Array.isArray(value)) {
    for (const item of value) repoValues(item, out, under);
  } else if (value !== null && typeof value === 'object') {
    for (const [k, v] of Object.entries(value)) repoValues(v, out, under || REPO_KEY.test(k));
  }
  return out;
}

function jsonOf(text) {
  try {
    return JSON.parse(text);
  } catch {
    return undefined;
  }
}

/** The host rules: URL hosts (IPs included), bare IPs, and bare dotted names on a known TLD. */
function scanHosts(text, mode, allow, found) {
  const urlHosts = new Set((mode === 'bundle' ? allow.bundle_hosts : allow.hosts).map((h) => h.toLowerCase()));
  const named = new Set([...allow.hosts, ...allow.bundle_hosts].map((h) => h.toLowerCase()));
  const spans = [];
  for (const m of text.matchAll(URL_HOST)) {
    const host = m[1].toLowerCase().replace(/\.$/, '');
    spans.push([m.index, m.index + m[0].length]);
    const tld = host.split('.').at(-1);
    const isHost = IPV4.test(host) || (host.includes('.') && /^[a-z]{2,}$/.test(tld));
    if (isHost && !urlHosts.has(host)) found('a host not on the allowlist', host, m.index);
  }
  const inUrl = (index) => spans.some(([start, end]) => index >= start && index < end);
  if (mode === 'source') {
    for (const m of text.matchAll(BARE_IP)) if (!inUrl(m.index)) found('an IP address', m[0], m.index);
  }
  for (const m of text.matchAll(BARE_HOST)) {
    const host = m[1].toLowerCase();
    const tld = m[2].toLowerCase();
    const privateName = PRIVATE_TLDS.has(tld) || PRIVATE_SUFFIXES.some((suffix) => host.endsWith(suffix));
    // A minifier names a variable with one or two letters, so in a bundle
    // a one-letter name, a dot and `local` (not opening a string) is a
    // member read. Every other private name is a machine, in any context.
    const labels = host.split('.');
    const memberRead = labels.length === 2 && labels[0].length <= 2 && !/["'`]/.test(text[m.index - 1] ?? '');
    const shaped = mode === 'source' ? privateName || PUBLIC_TLDS.has(tld) : privateName && !memberRead;
    if (shaped && !named.has(host) && !inUrl(m.index)) found('a host not on the allowlist', host, m.index);
  }
}

/**
 * The owner rules: GitHub links and repository fields naming another owner.
 * `exempt(index)` says a match there sits in a value the allowlist exempts
 * from these rules.
 */
function scanOwners(text, file, mode, allow, found, exempt) {
  const allowed = [];
  for (const m of text.matchAll(GITHUB_PATH)) {
    const [, owner, repo, pull] = m;
    if (owner === allow.owner || exempt(m.index)) continue;
    // A pull-request link on any owner's `bundle_repo` is allowed in a
    // bundle, and its span is skipped by the denylist too (which names the
    // real owner). Any owner passes here; the provenance schema is what pins
    // the bundled PR links to kew's own repository.
    if (mode === 'bundle' && repo === allow.bundle_repo && pull !== undefined) {
      allowed.push([m.index, m.index + m[0].length]);
      continue;
    }
    found('a GitHub owner other than the invented one', m[0], m.index);
  }
  const fielded = new Set();
  for (const m of text.matchAll(REPO_FIELD)) {
    const value = `${m[2]}/${m[3]}`;
    fielded.add(value);
    // The value's own position, so an exemption of the value is what counts.
    const at = m.index + m[0].lastIndexOf(value);
    if (m[2] !== allow.owner && !exempt(at)) found('a repository other than the invented one', value, at);
  }
  if (file.endsWith('.json')) {
    const doc = jsonOf(text);
    for (const value of doc === undefined ? [] : repoValues(doc)) {
      if (fielded.has(value) || value.split('/')[0] === allow.owner) continue;
      const quoted = `"${value}"`;
      let at = text.indexOf(quoted);
      while (at !== -1 && exempt(at)) at = text.indexOf(quoted, at + 1);
      if (at !== -1) found('a repository other than the invented one', value, at);
    }
  }
  return allowed;
}

/**
 * Every finding in `text` (the file `file`), as `{ file, line, rule, match }`.
 * `text` may be several readings of one file (decoded by its byte-order mark,
 * and its raw bytes); each finding is kept once. `mode` is `source` or
 * `bundle`; `denylist` the factory terms.
 */
export function scanText(text, file, options) {
  // UTF-16 with no byte-order mark reads as ASCII between NULs: read it again
  // without them, keeping each finding once.
  const [first, ...rest] = [text].flat().flatMap((t) => (t.includes('\0') ? [t, t.replaceAll('\0', '')] : [t]));
  const findings = scanOnce(first, file, options);
  const key = (f) => `${f.line}\0${f.rule}\0${f.match}`;
  const seen = new Set(findings.map(key));
  for (const view of rest) {
    for (const finding of scanOnce(view, file, options)) {
      if (!seen.has(key(finding))) findings.push(finding);
      seen.add(key(finding));
    }
  }
  return findings;
}

/**
 * `scanText` over `text` as it is. `strings` marks the printable strings of a
 * skipped image: only the path, email, token and denylist rules read them.
 */
function scanOnce(text, file, { mode = 'source', allow, denylist, strings = false }) {
  const findings = [];
  const found = (rule, match, index) => findings.push({ file, line: lineOf(text, Math.max(index, 0)), rule, match });
  for (const [what, pattern] of PATH_PATTERNS) for (const m of text.matchAll(pattern)) found(what, m[0], m.index);
  for (const m of text.matchAll(EMAIL)) found('an email address', m[0], m.index);
  for (const [what, pattern] of TOKEN_PATTERNS) for (const m of text.matchAll(pattern)) found(what, m[0], m.index);
  if (!strings) scanHosts(text, mode, allow, found);
  const keys = exemptionOf(file, allow, mode);
  const spans = keys === null ? [] : exemptSpans(text, file, keys);
  const skips = (rule) => (index) => spans.some((s) => s.rules.includes(rule) && index >= s.start && index < s.end);
  const allowed = strings ? [] : scanOwners(text, file, mode, allow, found, skips('owner'));
  const inAllowed = (index) => allowed.some(([start, end]) => index >= start && index < end);
  const denied = skips('denylist');
  if (file !== DENYLIST_FILE) {
    for (const [term, what] of denylist) {
      for (const m of text.matchAll(wordPattern(term))) if (!inAllowed(m.index) && !denied(m.index)) found(what, m[0], m.index);
    }
  }
  return findings;
}

/**
 * The signatures of the binary formats a bundle ships, by extension: fonts,
 * images and WebAssembly. Read as Latin-1, their bytes are noise the path and
 * host rules match by chance, so a file is skipped only when its extension
 * names one of these AND its bytes carry that format's signature (every part
 * of one alternative). Either alone is a file read like any other.
 */
const SIGNATURES = Object.fromEntries(
  Object.entries({
    woff: [[[0, 'wOFF']]],
    woff2: [[[0, 'wOF2']]],
    ttf: [[[0, '\x00\x01\x00\x00']], [[0, 'true']]],
    otf: [[[0, 'OTTO']]],
    png: [[[0, '\x89PNG\r\n\x1a\n']]],
    jpg: [[[0, '\xff\xd8\xff']]],
    jpeg: [[[0, '\xff\xd8\xff']]],
    gif: [[[0, 'GIF87a']], [[0, 'GIF89a']]],
    webp: [
      [
        [0, 'RIFF'],
        [8, 'WEBP']
      ]
    ],
    avif: [[[4, 'ftypavif']], [[4, 'ftypavis']]],
    ico: [[[0, '\x00\x00\x01\x00']]],
    wasm: [[[0, '\x00asm']]]
  }).map(([ext, alternatives]) => [ext, alternatives.map((parts) => parts.map(([at, magic]) => [at, Buffer.from(magic, 'latin1')]))])
);
/** Images: skipped as noise, but their printable strings (EXIF, XMP, PNG text chunks) are still read. */
const IMAGES = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp', 'avif', 'ico']);
const extensionOf = (name) => /\.([A-Za-z0-9]+)$/.exec(name)?.[1].toLowerCase() ?? '';

/** Whether `bytes` are the binary format `name`'s extension names. */
function isBinary(name, bytes) {
  const alternatives = SIGNATURES[extensionOf(name)] ?? [];
  return alternatives.some((parts) => parts.every(([at, magic]) => bytes.subarray(at, at + magic.length).equals(magic)));
}

/** A skipped binary's `{ binary, strings }`: an image's printable runs of six or more characters, ASCII and UTF-16LE, one per line; null for a font or WebAssembly. */
function skippedBinary(name, bytes) {
  if (!IMAGES.has(extensionOf(name))) return { binary: true, strings: null };
  const latin1 = bytes.toString('latin1');
  const ascii = latin1.match(/[\x20-\x7e]{6,}/g) ?? [];
  const wide = (latin1.match(/(?:[\x20-\x7e]\x00){6,}/g) ?? []).map((run) => run.replaceAll('\x00', ''));
  return { binary: true, strings: [...ascii, ...wide].join('\n') };
}

const GZIP = Buffer.from([0x1f, 0x8b]);
/** How deep gzip inside gzip is read before the file is refused. */
const GZIP_LAYERS = 4;
/** The finding for compressed bytes the scan cannot read whole. */
export const UNREADABLE = 'a compressed file that does not decompress whole';

/** Gzip header flags: a header checksum, an extra field, a file name, a comment. */
const [FHCRC, FEXTRA, FNAME, FCOMMENT] = [0x02, 0x04, 0x08, 0x10];

/**
 * The name and comment fields of every member of `bytes`, gzip already known
 * to decompress whole, as `{ fields }` of Latin-1 text; or `{ refused }` for
 * a member with an extra field, bytes the scan does not read.
 */
function gzipFields(bytes) {
  const fields = [];
  for (let at = 0, member = 1; at < bytes.length; member += 1) {
    const flags = bytes[at + 3];
    if (flags & FEXTRA) return { refused: `gzip member ${member} with an extra field (FEXTRA), never read` };
    at += 10;
    for (const flag of [FNAME, FCOMMENT].filter((f) => flags & f)) {
      const end = bytes.indexOf(0, at);
      fields.push(bytes.toString('latin1', at, end));
      at = end + 1;
    }
    if (flags & FHCRC) at += 2;
    // The deflate stream, then the member's CRC and length.
    at += inflateRawSync(bytes.subarray(at), { info: true }).engine.bytesWritten + 8;
  }
  return { fields };
}

/**
 * `bytes` decompressed, as `{ bytes, name, headers }` (`name` without its
 * `.gz` or `.br` once decompressed, `headers` every gzip member's name and
 * comment fields), or `{ refused }`: gzip by its signature, whatever the
 * name, nested up to GZIP_LAYERS deep, and a `.br` file by its name. Gzip
 * that does not decompress whole (truncated, or bytes after its last member
 * that zlib would drop silently) or that has a member with an extra field is
 * refused. A `.gz` or `.br` file that is not compressed is read as it is only
 * when it is UTF-8 text: any other bytes behind a compressed name are noise
 * that would pass every rule.
 */
function decompressed(name, raw) {
  let bytes = raw;
  let layers = 0;
  const headers = [];
  while (bytes.subarray(0, 2).equals(GZIP)) {
    if (layers === GZIP_LAYERS) return { refused: `gzip nested more than ${GZIP_LAYERS} deep` };
    let unzipped;
    try {
      unzipped = gunzipSync(bytes, { info: true });
    } catch {
      return { refused: 'gzip, truncated or with bytes after its end' };
    }
    // zlib reads member after member and drops what follows the last one
    // unless it opens with the gzip signature: every byte must be consumed.
    if (unzipped.engine.bytesWritten !== bytes.length) return { refused: 'gzip with bytes after its last member' };
    const member = gzipFields(bytes);
    if (member.refused) return member;
    headers.push(...member.fields);
    bytes = unzipped.buffer;
    layers += 1;
  }
  const inner = name.replace(/\.(?:gz|br)$/, '').replace(/\.svgz$/, '.svg');
  if (layers > 0) return { bytes, name: inner, headers };
  if (name.endsWith('.br')) {
    try {
      return { bytes: brotliDecompressSync(bytes), name: inner };
    } catch {
      // Not brotli: plain UTF-8 text behind the name is still read, below.
    }
  }
  if (/\.(?:gz|br)$/.test(name) && !isUtf8(bytes)) {
    return { refused: `${name.endsWith('.br') ? 'brotli' : 'gzip'} that does not decompress, and not UTF-8 text` };
  }
  return { bytes, name };
}

/** The finding for bytes the scan neither reads as text nor skips as a known binary. */
export const NOT_TEXT = 'a file that is neither text nor a format the scan reads';

/** Archives and compressed streams the scan does not unpack, by their signatures. */
const PACKED = Object.entries({
  'a zip archive': ['PK\x03\x04', 'PK\x05\x06', 'PK\x07\x08'],
  xz: ['\xfd7zXZ\x00'],
  bzip2: ['1', '2', '3', '4', '5', '6', '7', '8', '9'].map((level) => `BZh${level}`),
  'a 7z archive': ['7z\xbc\xaf\x27\x1c'],
  zstd: ['\x28\xb5\x2f\xfd'],
  'a zlib stream': ['\x78\x01', '\x78\x5e', '\x78\x9c', '\x78\xda'],
  gzip: ['\x1f\x8b\x08']
});
/** The archive or compressed stream `bytes` open with, by its signature, or null. */
function packedAt(bytes) {
  const head = bytes.subarray(0, 8).toString('latin1');
  return PACKED.find(([, magics]) => magics.some((magic) => head.startsWith(magic)))?.[0] ?? null;
}

/**
 * Whether `positions` (ascending) crowd any window of `length` units: more
 * than one in a hundred of a 256-unit window (three or more), or of the whole
 * when it is shorter. A window, not the whole file: padding a compressed
 * stream with text must not dilute it.
 */
function crowded(positions, length) {
  const window = Math.min(256, length);
  const needed = Math.floor(window / 100) + 1;
  return positions.some((at, i) => i + needed <= positions.length && positions[i + needed - 1] - at < window);
}

/** The control bytes text holds: NUL (UTF-16 with no mark), tab, line breaks, form feed and escape. */
const TEXT_CONTROLS = new Set([0x00, 0x09, 0x0a, 0x0b, 0x0c, 0x0d, 0x1b]);
/**
 * Where the raw bytes behind a byte-order mark, not UTF-8, hold what ASCII
 * text with a stray mark does not: a control byte text never holds, or two
 * high bytes in a row (deflate output, which can hold no control byte at
 * all, has high bytes side by side).
 */
function oddBytes(bytes) {
  const positions = [];
  for (let i = 0; i < bytes.length; i += 1) {
    const control = (bytes[i] < 0x20 && !TEXT_CONTROLS.has(bytes[i])) || bytes[i] === 0x7f;
    if (control || (bytes[i] >= 0x80 && bytes[i + 1] >= 0x80)) positions.push(i);
  }
  return positions;
}
/** Characters no text holds: controls but tab, line breaks and form feed; unassigned and private-use code points; U+FFFD for an invalid unit. */
const NOT_TEXT_CHARS = /[^\P{Cc}\t\n\r\f]|[\p{Cn}\p{Co}�]/gu;

/** `bytes` decoded by a UTF-32 or UTF-16 byte-order mark as `{ text, body }` (`body` the bytes after the mark), or null with none. */
function byMark(bytes) {
  const mark = bytes.subarray(0, 4).toString('hex');
  if (mark === 'fffe0000') return { text: utf32(bytes.subarray(4), true), body: bytes.subarray(4) };
  if (mark === '0000feff') return { text: utf32(bytes.subarray(4), false), body: bytes.subarray(4) };
  if (bytes[0] === 0xff && bytes[1] === 0xfe) return { text: new TextDecoder('utf-16le').decode(bytes), body: bytes.subarray(2) };
  if (bytes[0] === 0xfe && bytes[1] === 0xff) return { text: new TextDecoder('utf-16be').decode(bytes), body: bytes.subarray(2) };
  return null;
}

/**
 * `bytes` as `{ texts }`, every reading the scan makes of them, or
 * `{ refused }`. A byte-order mark picks UTF-32 or UTF-16, and the raw bytes
 * are read as Latin-1 too: a mark in front of ASCII decodes it to CJK. A mark
 * in front of a signature, or of bytes that are text neither decoded nor raw,
 * is refused. With no mark, UTF-8 only: the demo and its build are UTF-8, so
 * any other bytes (Latin-1, an archive, a compressed stream, brotli with no
 * name) are refused, an archive or compressed stream named by its signature.
 */
function decoded(bytes) {
  const marked = byMark(bytes);
  if (marked !== null) {
    const packed = packedAt(marked.body);
    if (packed) return { refused: `${packed} behind a byte-order mark, which the scan does not unpack`, rule: NOT_TEXT };
    // Text either way: decoded by the mark (CJK in UTF-16), or as raw bytes (ASCII behind a stray mark).
    const odd = [...marked.text.matchAll(NOT_TEXT_CHARS)].map((m) => m.index);
    const raw = isUtf8(marked.body) || !crowded(oddBytes(marked.body), marked.body.length);
    if (crowded(odd, marked.text.length) && !raw) {
      return { refused: 'a byte-order mark in front of bytes that are not text, decoded or raw', rule: NOT_TEXT };
    }
    return { texts: [marked.text, bytes.toString('latin1')] };
  }
  if (isUtf8(bytes)) return { texts: [new TextDecoder('utf-8').decode(bytes)] };
  const packed = packedAt(bytes);
  return { refused: `not UTF-8 text; the demo is UTF-8${packed ? ` (${packed}, which the scan does not unpack)` : ''}`, rule: NOT_TEXT };
}

function utf32(bytes, little) {
  const chars = [];
  for (let i = 0; i + 4 <= bytes.length; i += 4) {
    const code = little ? bytes.readUInt32LE(i) : bytes.readUInt32BE(i);
    chars.push(code <= 0x10ffff ? String.fromCodePoint(code) : '�');
  }
  return chars.join('');
}

/**
 * The file at `path` (named `name`) as the scan reads it: `{ texts }`, each
 * reading of it; `{ binary: true, strings }` for a font, an image or
 * WebAssembly whose extension and signature agree, compressed or not
 * (`strings` an image's printable strings, else null); or `{ refused, rule }`
 * for compressed bytes it cannot read whole (UNREADABLE) or bytes that are
 * not text (NOT_TEXT). A read or skipped file also carries `headers`, the
 * name and comment fields of its gzip members, read as text either way.
 */
export function contentOf(path, name = path) {
  const raw = readFileSync(path);
  if (isBinary(name, raw)) return { ...skippedBinary(name, raw), headers: [] };
  const unpacked = decompressed(name, raw);
  if (unpacked.refused) return { ...unpacked, rule: UNREADABLE };
  const binary = isBinary(unpacked.name, unpacked.bytes);
  const content = binary ? skippedBinary(unpacked.name, unpacked.bytes) : decoded(unpacked.bytes);
  return content.refused ? content : { ...content, headers: unpacked.headers ?? [] };
}

/**
 * Every finding under `dir`, how many files it read, the binaries it skipped
 * and the symbolic links not followed. A tree it read no file of throws: a
 * scan of nothing proves nothing.
 */
export function scanTree(dir, { bundle = false, allow = loadAllowlist(), denylist = factoryDenylist() } = {}) {
  const root = dir instanceof URL ? fileURLToPath(dir) : dir;
  const mode = bundle ? 'bundle' : 'source';
  const seen = { links: [], specials: [], skipDirs: bundle ? new Set() : SKIP_DIRS };
  const stat = statSync(root);
  if (!stat.isDirectory() && !stat.isFile()) throw new Error(`leak scan: ${dir} is neither a directory nor a regular file`);
  const files = stat.isDirectory() ? filesUnder(root, seen) : [''];
  const findings = [];
  const skipped = [];
  for (const file of files) {
    const path = file === '' ? root : join(root, file);
    const name = file || path;
    const content = contentOf(path, name);
    if (content.refused) findings.push({ file: name, line: 1, rule: content.rule, match: content.refused });
    else {
      if (content.binary) skipped.push(name);
      if (content.strings) findings.push(...scanText(content.strings, name, { mode, allow, denylist, strings: true }));
      const texts = [...(content.texts ?? []), ...content.headers];
      if (texts.length > 0) findings.push(...scanText(texts, name, { mode, allow, denylist }));
    }
  }
  for (const special of seen.specials) findings.push({ file: special, line: 1, rule: 'a special file, never read', match: special });
  const scanned = files.length - skipped.length;
  if (scanned === 0) throw new Error(`leak scan: scanned 0 files in ${dir}`);
  return { findings, scanned, skipped, links: seen.links.sort() };
}

/** Every finding under `dir`. */
export const scan = (dir, options) => scanTree(dir, options).findings;

/** One finding, as the command prints it. */
export const describeFinding = (f) => `${f.file}:${f.line}: ${f.rule} — ${f.match}`;

/** The real path of `path`, symbolic links resolved, or null when it does not exist. */
function realPath(path) {
  try {
    return realpathSync(path);
  } catch {
    return null;
  }
}

// Real paths on both sides: node resolves the module's own path through
// symbolic links but leaves argv[1] as typed, so a symlinked directory (macOS
// /var → /private/var) would otherwise scan nothing and exit 0.
if (process.argv[1] && realPath(fileURLToPath(import.meta.url)) === realPath(process.argv[1])) {
  const args = process.argv.slice(2);
  const dir = args.find((a) => !a.startsWith('--'));
  if (!dir) {
    console.error('usage: node frontend/demo/leak-scan.mjs <dir> [--bundle]');
    process.exit(2);
  }
  let result;
  try {
    result = scanTree(dir, { bundle: args.includes('--bundle') });
  } catch (error) {
    // A scan of nothing proves nothing: an empty or all-binary tree fails closed.
    console.error(error instanceof Error ? error.message : String(error));
    process.exit(1);
  }
  const { findings, skipped, links } = result;
  for (const finding of findings) console.log(describeFinding(finding));
  const names = skipped.length ? `: ${skipped.join(', ')}` : '';
  console.log(`leak scan: skipped ${skipped.length} binary file(s), fonts, images or WebAssembly (an image's strings read)${names}`);
  console.log(`leak scan: skipped ${links.length} symbolic link(s), not followed${links.length ? `: ${links.join(', ')}` : ''}`);
  console.log(findings.length === 0 ? `leak scan: ${dir} is clean` : `leak scan: ${findings.length} finding(s) in ${dir}`);
  process.exit(findings.length === 0 ? 0 : 1);
}
