/**
 * The demo world's generator and its leak scan (#3141), read without a
 * browser.
 *
 * - The committed world is what `demo/world/generate.mjs` writes from the
 *   committed `timings.json`, byte for byte, and a rerun writes it again.
 * - `node demo/leak-scan.mjs` passes over `frontend/demo/`, and refuses each
 *   rule's planted example. The examples are planted in a temp dir, never in
 *   the scanned tree, and every token is assembled at run time so no secret
 *   scanner mistakes this file for a leak.
 * - Every run's tokens are fresh input, output and cache reads, each drawn
 *   from its own `timings.json` spread, and a kind the timings do not publish
 *   draws the generator's fixed synthetic spread.
 *
 * The built `build-demo/` is scanned by `ui-checks/demo-world.mjs`, which
 * builds it first; nothing here reads a build that may not exist.
 */
import { execFileSync } from 'node:child_process';
import { cpSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, statSync, symlinkSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, relative, sep } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { brotliCompressSync, deflateRawSync, deflateSync, gzipSync } from 'node:zlib';
import { afterEach, describe, expect, it } from 'vitest';
import { NOT_FIXTURES as BUNDLE_SKIPS } from '../demo/fixture-bundle';
import { committedDenylist, describeFinding, exemptionOf, factoryDenylist, loadAllowlist, scan, scanTree } from '../demo/leak-scan.mjs';
import { NOT_FIXTURES, SYNTHETIC, TOKEN_FIELDS, buildWorld, serialise, writeWorld } from '../demo/world/generate.mjs';

type Spread = { median: number; p90: number };
type TokenField = 'input_tokens' | 'output_tokens' | 'cache_read_tokens';
type RunRow = { run_id: string; run_kind: string; status: string } & Record<TokenField, number>;
const TOKENS_OF = TOKEN_FIELDS as TokenField[];

const FRONTEND = new URL('../', import.meta.url);
const DEMO = new URL('demo/', FRONTEND);
const WORLD = new URL('world/', DEMO);
const FACTORY = new URL('fixtures/factory/', FRONTEND);
const SCAN = fileURLToPath(new URL('leak-scan.mjs', DEMO));

const temps: string[] = [];
afterEach(() => {
  for (const dir of temps.splice(0)) rmSync(dir, { recursive: true, force: true });
});
function tempDir(files: Record<string, string> = {}): string {
  const dir = mkdtempSync(join(tmpdir(), 'demo-world-'));
  temps.push(dir);
  for (const [name, body] of Object.entries(files)) {
    mkdirSync(join(dir, name, '..'), { recursive: true });
    writeFileSync(join(dir, name), body);
  }
  return dir;
}

function filesUnder(dir: string, root = dir): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return filesUnder(path, root);
    return [relative(root, path).split(sep).join('/')];
  });
}

const timings = JSON.parse(readFileSync(new URL('timings.json', WORLD), 'utf8'));

/** Every run row of the world `t` builds, with the total its node reports. */
function rowsWithTotals(t: typeof timings): { row: RunRow; total: number }[] {
  const { files, manifest } = buildWorld(t);
  const rows = new Map((files['runs.json'] as RunRow[]).map((r) => [r.run_id, r]));
  const nodeRuns = Object.entries(files)
    .filter(([name]) => name.startsWith(`epics-${manifest.epic_ref}-nodes-`))
    .flatMap(([, node]) => (node as { runs: { run_id: string; total_tokens: number }[] }).runs);
  expect(nodeRuns.length).toBe(rows.size);
  return nodeRuns.map((run) => ({ row: rows.get(run.run_id) as RunRow, total: run.total_tokens }));
}

/** The first kind whose `field` the timings publish, or undefined. */
function publishedKind(t: typeof timings, field: TokenField): string | undefined {
  return Object.entries(t.by_run_kind as Record<string, Record<string, Spread | null> | null>).find(([, kind]) => kind?.[field])?.[0];
}

/** Each published spread that is not a pair of finite non-negative numbers, named. */
function spreadProblems(t: typeof timings): string[] {
  const problems: string[] = [];
  for (const [kind, spreads] of Object.entries(t.by_run_kind as Record<string, Record<string, unknown>>)) {
    for (const field of ['cost_usd', 'duration_minutes', ...TOKENS_OF]) {
      const spread = spreads[field] as Spread | null | undefined;
      if (spread === null || spread === undefined) continue;
      const pair = [spread.median, spread.p90];
      if (!pair.every((n) => typeof n === 'number' && Number.isFinite(n) && n >= 0)) problems.push(`${kind}.${field}`);
    }
  }
  return problems;
}

/** Whether `value` lies where the generator draws from `spread`: half its median up to 1.1 × its p90. */
const withinSpread = (value: number, spread: Spread) => value >= Math.floor(spread.median * 0.5) && value <= Math.ceil(spread.p90 * 1.1);

/** Scaling `kind`'s `field` spread tenfold moves that kind's `field` tenfold and nothing else. */
function drawsFromOwnAggregate(t: typeof timings, kind: string, field: TokenField): void {
  const scaled = structuredClone(t);
  scaled.by_run_kind[kind][field].median *= 10;
  scaled.by_run_kind[kind][field].p90 *= 10;
  const before = buildWorld(t).files['runs.json'] as RunRow[];
  const after = buildWorld(scaled).files['runs.json'] as RunRow[];
  expect(after.some((row) => row.run_kind === kind && row.status !== 'pending'), `no ${kind} run draws ${field}`).toBe(true);
  for (const [i, row] of after.entries()) {
    const was = before[i];
    for (const other of TOKENS_OF.filter((f) => f !== field)) expect(row[other], `${row.run_id} ${other}`).toBe(was[other]);
    if (row.run_kind === kind && row.status !== 'pending') {
      expect(Math.abs(row[field] - was[field] * 10), `${row.run_id} ${field}`).toBeLessThanOrEqual(10);
    } else expect(row[field]).toBe(was[field]);
  }
}

describe('the generator', () => {
  it('writes the committed world from the committed timings, byte for byte', () => {
    const dir = tempDir();
    cpSync(fileURLToPath(new URL('timings.json', WORLD)), join(dir, 'timings.json'));
    writeWorld(pathToFileURL(`${dir}/`));
    const committed = filesUnder(fileURLToPath(WORLD)).filter((f) => f.endsWith('.json')).sort();
    expect(filesUnder(dir).sort()).toEqual(committed);
    for (const file of committed) {
      expect(readFileSync(join(dir, file), 'utf8'), file).toBe(readFileSync(new URL(file, WORLD), 'utf8'));
    }
  });

  it('writes the same world on every run', () => {
    expect(serialise(buildWorld(timings))).toBe(serialise(buildWorld(timings)));
  });

  it("copies kew's catalogs from the factory set unchanged", () => {
    for (const file of ['prompts.json', 'prompts-dispatch.json', 'models.json']) {
      expect(readFileSync(new URL(file, WORLD), 'utf8')).toBe(readFileSync(new URL(file, FACTORY), 'utf8'));
    }
  });

  it('moves costs and durations with the timings, and keeps every id and the reconciliation', () => {
    const doubled = structuredClone(timings);
    for (const kind of Object.values(doubled.by_run_kind) as Record<string, Spread | null>[]) {
      for (const field of ['cost_usd', 'duration_minutes', ...TOKENS_OF]) {
        const spread = kind[field];
        if (spread === null) continue;
        spread.median *= 1.5;
        spread.p90 *= 1.5;
      }
    }
    const before = buildWorld(timings);
    const after = buildWorld(doubled);
    expect(Object.keys(after.files)).toEqual(Object.keys(before.files));
    expect(after.manifest.run_id).toBe(before.manifest.run_id);
    expect(after.manifest.spend.epic_usd).toBeGreaterThan(before.manifest.spend.epic_usd);
    expect(after.manifest.spend.weekly_usd).toBe(after.manifest.spend.epic_usd);
  });

  it("gives every run fresh input, output and cache reads that sum to its total", () => {
    const rows = rowsWithTotals(timings);
    for (const { row, total } of rows) {
      expect(row.input_tokens + row.output_tokens + row.cache_read_tokens, row.run_id).toBe(total);
      if (timings.source === 'fake' && row.status === 'success') {
        // The fake timings are written so cache reads dominate a run and
        // fresh input is the smallest part; a corpus publishes what it saw.
        expect(row.cache_read_tokens).toBeGreaterThan(row.output_tokens);
        expect(row.output_tokens).toBeGreaterThan(row.input_tokens);
      }
    }
  });

  it.for(TOKENS_OF)('draws %s from its own aggregate and from nothing else', (field, { skip }) => {
    const kind = publishedKind(timings, field);
    if (kind === undefined) return skip(`the timings publish no ${field}`);
    drawsFromOwnAggregate(timings, kind, field);
  });

  it('holds a corpus-sourced timings file to the shape it declares', () => {
    // A corpus publishes only what it saw: a kind under the floor is absent,
    // a field may be null, and cache reads may fall below output.
    const corpus = structuredClone(timings);
    corpus.source = 'corpus';
    delete corpus.by_run_kind.dispatch;
    corpus.by_run_kind.adversarial_review.input_tokens = null;
    corpus.by_run_kind.review_remediation.cache_read_tokens = { median: 1000, p90: 2000 };
    expect(spreadProblems(corpus)).toEqual([]);
    const rows = rowsWithTotals(corpus);
    for (const { row, total } of rows) {
      expect(row.input_tokens + row.output_tokens + row.cache_read_tokens, row.run_id).toBe(total);
      for (const field of TOKENS_OF) expect(Number.isFinite(row[field]) && row[field] >= 0, `${row.run_id} ${field}`).toBe(true);
    }
    // The fake timings' ordering does not hold here, and is not asserted.
    const remediation = rows.filter(({ row }) => row.run_kind === 'review_remediation' && row.status === 'success');
    expect(remediation.some(({ row }) => row.cache_read_tokens < row.output_tokens)).toBe(true);
    const drawn = (kind: string, field: TokenField) =>
      rows.filter(({ row }) => row.run_kind === kind && row.status === 'success').map(({ row }) => row[field]);
    for (const [kind, field] of [...TOKENS_OF.map((f) => ['dispatch', f] as const), ['adversarial_review', 'input_tokens'] as const]) {
      const values = drawn(kind, field);
      expect(values.length, `${kind} ${field}`).toBeGreaterThan(0);
      for (const value of values) expect(withinSpread(value, SYNTHETIC[field as keyof typeof SYNTHETIC]), `${kind} ${field} ${value}`).toBe(true);
    }
    for (const field of TOKENS_OF) {
      const kind = publishedKind(corpus, field);
      expect(kind, field).toBeDefined();
      expect(kind).not.toBe('dispatch');
      drawsFromOwnAggregate(corpus, kind as string, field);
    }
  });

  it('holds the committed timings to the same shape', () => {
    expect(spreadProblems(timings)).toEqual([]);
  });

  it('draws the fixed synthetic spread for a run kind the timings do not publish', () => {
    // A kind under the floor of 20 runs publishes nothing. Whatever the
    // committed timings hold, a clone without ci_fix (the merge verify's
    // duration) and review_remediation still builds.
    const without = structuredClone(timings);
    delete without.by_run_kind.ci_fix;
    delete without.by_run_kind.review_remediation;
    const rows = (buildWorld(without).files['runs.json'] as RunRow[]).filter((r) => r.run_kind === 'review_remediation');
    expect(rows.length).toBeGreaterThan(0);
    for (const row of rows) {
      for (const field of TOKENS_OF) {
        const spread = SYNTHETIC[field as keyof typeof SYNTHETIC];
        expect(row[field]).toBeGreaterThanOrEqual(Math.floor(spread.median * 0.5));
        expect(row[field]).toBeLessThanOrEqual(Math.ceil(spread.p90 * 1.1));
      }
    }
  });

  it('takes only numbers from the timings: every key and every string but an instant is the same whatever they say', () => {
    // The timings are the one input built from real history, and an allowlist
    // holds them to numbers (kew/scripts/demo_world_timings.py). This holds
    // the generator to using them as numbers: no key, name or sentence of the
    // world moves when they change; only figures and the instants durations set.
    const INSTANT = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/;
    const words = (value: unknown, out: string[] = []): string[] => {
      if (typeof value === 'string') {
        if (!INSTANT.test(value)) out.push(value);
      } else if (Array.isArray(value)) value.forEach((v) => words(v, out));
      else if (value !== null && typeof value === 'object') {
        for (const [k, v] of Object.entries(value)) {
          out.push(`key:${k}`);
          words(v, out);
        }
      }
      return out;
    };
    const other = structuredClone(timings);
    for (const kind of Object.values(other.by_run_kind) as Record<string, Spread | null>[]) {
      for (const field of ['cost_usd', 'duration_minutes', ...TOKENS_OF]) {
        const spread = kind[field];
        if (spread !== null) [spread.median, spread.p90] = [spread.median * 1.37, spread.p90 * 1.37];
      }
    }
    delete other.by_run_kind.adversarial_review;
    const before = buildWorld(timings);
    const after = buildWorld(other);
    expect(Object.keys(after.files)).toEqual(Object.keys(before.files));
    // Which events land in a window moves with the durations, so the strings
    // are compared as one vocabulary across the world, not file by file.
    const vocabulary = (world: { files: Record<string, unknown> }) => [...new Set(words(world.files))].sort();
    expect(vocabulary(after)).toEqual(vocabulary(before));
    expect(words(after.manifest).sort()).toEqual(words(before.manifest).sort());
  });

  it('copies no string of the timings into the world: a planted marker changes no byte', () => {
    // Built in memory, past the allowlist that would refuse it: an extra
    // top-level key, an extra field in a kind the world draws from, and an
    // unknown kind, all carrying one marker.
    const MARK = ['planted', 'marker', '3141', 'zq'].join('-');
    const planted = structuredClone(timings);
    planted[MARK] = MARK;
    planted.note = MARK;
    planted.by_run_kind.dispatch[MARK] = { median: 1, p90: 2 };
    planted.by_run_kind.dispatch.note = MARK;
    planted.by_run_kind[MARK] = structuredClone(timings.by_run_kind.dispatch);
    const serialised = (world: { files: Record<string, unknown>; manifest: unknown }) =>
      Object.entries(world.files)
        .map(([name, payload]) => `${name}\n${serialise(payload)}`)
        .join('\n') + serialise(world.manifest);
    const marked = serialised(buildWorld(planted));
    expect(marked.includes(MARK)).toBe(false);
    expect(marked).toBe(serialised(buildWorld(timings)));
  });

  it('keeps the files the bundle skips: the manifest and the timings', () => {
    expect(NOT_FIXTURES).toEqual(['manifest.json', 'timings.json']);
    expect(BUNDLE_SKIPS).toEqual(NOT_FIXTURES);
  });
});

/** Assembled at run time: a literal of any of these would be a planted secret in this file. */
const hex64 = 'a1b2c3d4e5f6a7b8'.repeat(4);
const TOKENS: Record<string, string[]> = {
  'an Anthropic key': [['sk', 'ant', 'api03', 'A'.repeat(24)].join('-')],
  'an OpenRouter key': [`${'sk'}-or-v1-${hex64}`],
  'an OpenAI key': [
    `${'sk'}-${'Q'.repeat(40)}`,
    `${'sk'}-proj-Ab3_${'x'.repeat(30)}-${'Y'.repeat(30)}`,
    `${'sk'}-svcacct-${'Z'.repeat(60)}`
  ],
  'a GitHub token': [`${'gh'}p_${'b'.repeat(36)}`],
  'a GitHub fine-grained token': [`${'github'}_pat_${'c'.repeat(24)}`],
  'a GitLab token': [`${'glpat'}-${'r'.repeat(20)}`],
  'a Google API key': [`${'AI'}za${'Q'.repeat(35)}`],
  'an npm token': [`${'npm'}_${'q'.repeat(36)}`],
  'a Linear key': [`${'lin'}_api_${'d'.repeat(24)}`],
  'a Slack token': [`${'xo'}xb-${'1'.repeat(12)}`]
};

/** Every input the review of #3169 found the scan passing, each with the rule that must now refuse it. */
const PLANTED: [string, string][] = [
  ...Object.entries(TOKENS).flatMap(([rule, tokens]) => tokens.map((t): [string, string] => [rule, `{"k": "${t}"}`])),
  ['a home path', `{"path": "/${'Users'}/someone/code/x"}`],
  ['a home path', `{"path": "/${'home'}/someone/.kew"}`],
  ['a home path', `{"p": "${'~'}/Projects/kew/kew.toml"}`],
  ['a home path', `{"p": "/${'root'}/.config/kew/agent-token.env"}`],
  ['a home path', `{"p": "C:\\\\${'Users'}\\\\someone\\\\kew"}`],
  ['a temp path', `{"p": "/private/${'var'}/folders/xy/T/kew-abc"}`],
  ['a mounted volume path', `{"p": "/${'Volumes'}/Work/kew"}`],
  ['a temp path', `{"p": "/${'tmp'}/kew-verify-someone/worktree"}`],
  ['a temp path', `{"p": "/private/${'tmp'}/claude-501/x.json"}`],
  ['a home path', `{"p": "/private/${'tmp'}/claude-501/-${'Users'}-someone-Projects-kew/x.json"}`],
  ['a home path', `{"slug": "-${'home'}-someone-code-kew"}`],
  ['a host not on the allowlist', `{"host": "kew-verify.tail1a2b3.${'ts'}.net"}`],
  ['a host not on the allowlist', `{"url": "https://not${'kew'}.dev/docs/"}`],
  ['a host not on the allowlist', `{"url": "https://${'kew'}.dev.example/docs/"}`],
  ['a host not on the allowlist', `{"host": "not${'kew'}.dev"}`],
  ['an email address', `{"by": "someone@${'example'}.org"}`],
  ['a host not on the allowlist', `{"url": "https://${'tracker'}.example.org/issue/1"}`],
  ['a host not on the allowlist', `{"u": "http://34.123.45.${'67'}:8765/api/meta"}`],
  ['a host not on the allowlist', `{"host": "someones-laptop.${'local'}"}`],
  ['a host not on the allowlist', `{"h": "kew-verify.us-central1-a.c.someproj.${'internal'}"}`],
  ['a host not on the allowlist', `{"mirror": "pypi.someone.${'com'}"}`],
  ['an IP address', `{"addr": "10.0.12.${'7'}"}`],
  ['a GitHub owner other than the invented one', `{"url": "https://github.com/${'someone'}/ledger/pull/3"}`],
  ['a repository other than the invented one', `{"delivery_repo": "${'janedoe'}/payroll"}`],
  ['a repository other than the invented one', `{"repo": "${'janedoe'}/payroll"}`],
  ['a repository other than the invented one', `{"project.repo": {"value": "${'janedoe'}/payroll", "source": "kew.toml"}}`]
];

describe('the leak scan', () => {
  const allow = loadAllowlist();
  const denylist = factoryDenylist();
  const rules = (files: Record<string, string>, bundle = false) =>
    scan(tempDir(files), { bundle, allow, denylist }).map((f) => f.rule);
  const termOf = (kind: string) => [...denylist].find(([, what]) => what === kind)?.[0] ?? '';

  it('passes over frontend/demo/, the committed world included', () => {
    const { findings, skipped, links } = scanTree(fileURLToPath(DEMO), { allow, denylist });
    expect(findings.map(describeFinding)).toEqual([]);
    expect(skipped).toEqual([]);
    expect(links).toEqual([]);
  });

  it('reads epic refs, run ids and owners off the factory set', () => {
    const kinds = new Set(denylist.values());
    expect(kinds).toEqual(new Set(['a factory run id', 'a factory epic ref', 'a factory owner', 'a real repository']));
    const runs = JSON.parse(readFileSync(new URL('runs.json', FACTORY), 'utf8')) as { run_id: string }[];
    for (const { run_id } of runs) expect(denylist.get(run_id)).toBe('a factory run id');
    const epics = JSON.parse(readFileSync(new URL('epics.json', FACTORY), 'utf8')) as { epic_ref: string }[];
    for (const { epic_ref } of epics) expect(denylist.get(epic_ref)).toBe('a factory epic ref');
  });

  it('keeps the committed denylist when the factory set names nothing', () => {
    const emptied = tempDir({ 'runs.json': '[]' });
    const shrunk = factoryDenylist(pathToFileURL(`${emptied}/`));
    const committed = committedDenylist();
    expect(committed.map(([, what]) => what)).toContain('a real repository');
    for (const [term, what] of committed) expect(shrunk.get(term), term).toBe(what);
    // Everything the factory set names today is in the committed list too.
    for (const term of denylist.keys()) expect(shrunk.has(term), term).toBe(true);
  });

  it.each(PLANTED)('refuses %s: %s', (rule, text) => {
    expect(rules({ 'world/x.json': text })).toContain(rule);
  });

  it.each(['a factory epic ref', 'a factory run id', 'a factory owner', 'a real repository'])('refuses %s', (kind) => {
    const term = termOf(kind);
    expect(term).not.toBe('');
    expect(rules({ 'world/x.json': `{"value": "${term}"}` })).toContain(kind);
  });

  it.each([
    ['a factory epic ref', (t: string) => `{"x": "epic-${t}"}`],
    ['a factory run id', (t: string) => `{"x": "runs-${t}-audit"}`],
    ['a factory run id', (t: string) => `{"x": "runs_${t}_audit"}`]
  ] as [string, (t: string) => string][])('refuses %s with a hyphen or underscore beside it', (kind, wrap) => {
    expect(rules({ 'world/x.json': wrap(termOf(kind)) })).toContain(kind);
  });

  it('reads every file that is text, whatever its name, and counts the binaries it skips', () => {
    const dir = tempDir({ 'notes.yaml': `by: someone@${'example'}.org\n`, Makefile: `HOST = someones-laptop.${'local'}\n` });
    writeFileSync(join(dir, 'font.woff2'), Buffer.from([0x77, 0x4f, 0x46, 0x32, 0xff, 0xfe, 0x00, 0x80]));
    const { findings, skipped } = scanTree(dir, { allow, denylist });
    expect(findings.map((f) => `${f.file}: ${f.rule}`).sort()).toEqual([
      'Makefile: a host not on the allowlist',
      'notes.yaml: an email address'
    ]);
    expect(skipped).toEqual(['font.woff2']);
  });

  it('refuses a home path in a UTF-16 file with a BOM and plain text named .gz, and a Latin-1 file as not UTF-8', () => {
    const leak = `"/${'Users'}/alice/secret.txt"`;
    const utf16 = (text: string, bom: number[], le: boolean) => {
      const body = Buffer.from(text, 'utf16le');
      if (!le) body.swap16();
      return Buffer.concat([Buffer.from(bom), body]);
    };
    // The demo and its build are UTF-8: Latin-1 is never guessed, so a Latin-1 file is refused whole.
    const latin1 = scanOne('a.txt', Buffer.from(`caf\u00e9 ${leak}`, 'latin1'));
    expect(latin1).toEqual({ findings: ['a.txt: a file that is neither text nor a format the scan reads'], skipped: [] });
    const encodings: [string, Buffer][] = [
      ['a.json', utf16(leak, [0xff, 0xfe], true)],
      ['a.json', utf16(leak, [0xfe, 0xff], false)],
      ['a.js.gz', Buffer.from(leak)],
      ['a.js.br', Buffer.from(leak)]
    ];
    for (const [name, bytes] of encodings) {
      const dir = tempDir({ 'b.js': 'export const b = 1;\n' });
      writeFileSync(join(dir, name), bytes);
      const { findings, skipped } = scanTree(dir, { allow, denylist });
      expect(findings.map((f) => `${f.file}: ${f.rule}`), name).toEqual([`${name}: a home path`]);
      expect(skipped, name).toEqual([]);
    }
  });

  /** Each `[name, bytes]` alone beside a clean file: its findings as `file: rule`, and the files skipped. */
  const scanOne = (name: string, bytes: Buffer, bundle = false) => {
    const dir = tempDir({ 'b.js': 'export const b = 1;\n' });
    mkdirSync(join(dir, name, '..'), { recursive: true });
    writeFileSync(join(dir, name), bytes);
    const { findings, skipped } = scanTree(dir, { bundle, allow, denylist });
    return { findings: findings.map((f) => `${f.file}: ${f.rule}`), skipped };
  };
  const LEAK = `"/${'Users'}/alice/secret.txt"`;

  it('refuses a home path in UTF-16 with no byte-order mark and in UTF-32 with one', () => {
    const utf32 = (text: string, little: boolean) => {
      const out = Buffer.alloc((text.length + 1) * 4);
      [0xfeff, ...[...text].map((c) => c.codePointAt(0) as number)].forEach((code, i) =>
        little ? out.writeUInt32LE(code, i * 4) : out.writeUInt32BE(code, i * 4)
      );
      return out;
    };
    const encodings: [string, Buffer][] = [
      ['le.json', Buffer.from(LEAK, 'utf16le')],
      ['be.json', Buffer.from(LEAK, 'utf16le').swap16()],
      ['le32.json', utf32(LEAK, true)],
      ['be32.json', utf32(LEAK, false)]
    ];
    for (const [name, bytes] of encodings) {
      const { findings, skipped } = scanOne(name, bytes);
      expect(findings, name).toEqual([`${name}: a home path`]);
      expect(skipped, name).toEqual([]);
    }
  });

  it('skips a binary only when its extension and its signature agree', () => {
    const disguised: [string, Buffer][] = [
      ['a.json', Buffer.concat([Buffer.from('GIF8'), Buffer.from(LEAK)])],
      ['a.txt', Buffer.concat([Buffer.from([0, 1, 0, 0]), Buffer.from(LEAK)])],
      ['a.woff2', Buffer.concat([Buffer.from('GIF8'), Buffer.from(LEAK)])],
      ['a.png', Buffer.concat([Buffer.from('wOF2'), Buffer.from(LEAK)])]
    ];
    for (const [name, bytes] of disguised) {
      const { findings, skipped } = scanOne(name, bytes);
      expect(findings, name).toEqual([`${name}: a home path`]);
      expect(skipped, name).toEqual([]);
    }
  });

  it("reads a skipped image's printable strings for paths, emails, tokens and the denylist", () => {
    const png = Buffer.concat([
      Buffer.from('\x89PNG\r\n\x1a\n', 'latin1'),
      Buffer.from(`\x00\x00\x00\x20tEXtSource\x00${LEAK.slice(1, -1)}\x00\x01`, 'latin1')
    ]);
    const jpeg = Buffer.concat([Buffer.from([0xff, 0xd8, 0xff, 0xe1]), Buffer.from(`<x:xmpmeta>by someone@${'example'}.org</x:xmpmeta>`)]);
    const run = termOf('a factory run id');
    const gif = Buffer.concat([Buffer.from('GIF89a'), Buffer.from([0, 1, 2]), Buffer.from(`comment ${run} here`)]);
    expect(scanOne('shot.png', png)).toEqual({ findings: ['shot.png: a home path'], skipped: ['shot.png'] });
    expect(scanOne('p.jpg', jpeg)).toEqual({ findings: ['p.jpg: an email address'], skipped: ['p.jpg'] });
    expect(scanOne('a.gif', gif, true)).toEqual({ findings: ['a.gif: a factory run id'], skipped: ['a.gif'] });
    // Hosts are not read off an image, and a string shorter than six characters is noise.
    const quiet = Buffer.concat([Buffer.from('\x89PNG\r\n\x1a\n', 'latin1'), Buffer.from(`\x00studio.${'local'}\x00~/ab\x00`, 'latin1')]);
    expect(scanOne('q.png', quiet)).toEqual({ findings: [], skipped: ['q.png'] });
  });

  it('names each binary it skips on the command line', () => {
    const dir = tempDir({ 'a.json': '{}' });
    writeFileSync(join(dir, 'font.woff2'), Buffer.from([0x77, 0x4f, 0x46, 0x32, 0xff, 0xfe, 0x00, 0x80]));
    const out = execFileSync('node', [SCAN, dir], { encoding: 'utf8' });
    expect(out).toContain('skipped 1 binary file(s)');
    expect(out).toMatch(/skipped 1 binary file\(s\).*: font\.woff2$/m);
  });

  it('reads gzip by its signature, nested gzip too, whatever the name', () => {
    const svg = gzipSync(`<svg>${LEAK}</svg>`);
    const cases: [string, Buffer][] = [
      ['a.js', gzipSync(LEAK)],
      ['a.svgz', svg],
      ['a.js.gz', gzipSync(gzipSync(LEAK))],
      ['a.json.gz', gzipSync(gzipSync(gzipSync(LEAK)))]
    ];
    for (const [name, bytes] of cases) expect(scanOne(name, bytes).findings, name).toEqual([`${name}: a home path`]);
  });

  it('refuses compressed bytes it cannot read whole', () => {
    const long = Buffer.from(`${'x'.repeat(10)}${LEAK}${'y'.repeat(5000)}`);
    const gz = gzipSync(long);
    const br = brotliCompressSync(long);
    let deep: Buffer = Buffer.from(LEAK);
    for (let i = 0; i < 8; i += 1) deep = gzipSync(deep);
    const refused: [string, Buffer][] = [
      ['a.js.gz', gz.subarray(0, Math.floor(gz.length / 2))],
      ['a.js', gz.subarray(0, Math.floor(gz.length / 2))],
      ['a.js.gz', Buffer.concat([gzipSync(LEAK), Buffer.from('garbage')])],
      ['a.js.br', br.subarray(0, Math.floor(br.length / 2))],
      ['a.js.gz', deep],
      ['a.bin.gz', Buffer.from([0xe9, 0x20, 0xff, 0x00, 0x41])]
    ];
    for (const [name, bytes] of refused) {
      const { findings } = scanOne(name, bytes);
      expect(findings, name).toContain(`${name}: a compressed file that does not decompress whole`);
    }
  });

  it('refuses gzip followed by bytes that open no other gzip member, and reads every member', () => {
    const trailing = Buffer.concat([gzipSync('clean\n'), Buffer.from([0x00, 0xff]), Buffer.from(`${LEAK}\n`)]);
    expect(scanOne('a.js', trailing).findings).toContain('a.js: a compressed file that does not decompress whole');
    expect(scanOne('b.js', Buffer.concat([gzipSync('clean\n'), gzipSync(LEAK)])).findings).toEqual(['b.js: a home path']);
  });

  /** A gzip member of `text` whose header carries the optional extra, name and comment fields given. */
  const gzipWith = (text: string, fields: { extra?: Buffer; name?: string; comment?: string }) => {
    const plain = gzipSync(text);
    const head = Buffer.from(plain.subarray(0, 10));
    const parts: Buffer[] = [];
    if (fields.extra) {
      head[3] |= 0x04;
      const length = Buffer.alloc(2);
      length.writeUInt16LE(fields.extra.length);
      parts.push(length, fields.extra);
    }
    if (fields.name !== undefined) {
      head[3] |= 0x08;
      parts.push(Buffer.from(`${fields.name}\0`, 'latin1'));
    }
    if (fields.comment !== undefined) {
      head[3] |= 0x10;
      parts.push(Buffer.from(`${fields.comment}\0`, 'latin1'));
    }
    return Buffer.concat([head, ...parts, plain.subarray(10)]);
  };

  it("reads every gzip member's name and comment fields, and refuses an extra field", () => {
    const token = 'AKIA' + 'FAKE'.repeat(4);
    expect(scanOne('a.js.gz', gzipWith('clean\n', { comment: `key ${token}` })).findings).toEqual(['a.js.gz: an AWS access key']);
    expect(scanOne('b.js.gz', gzipWith('clean\n', { name: LEAK.slice(1, -1) })).findings).toEqual(['b.js.gz: a home path']);
    const second = Buffer.concat([gzipSync('clean\n'), gzipWith('more\n', { comment: LEAK })]);
    expect(scanOne('c.js', second).findings).toEqual(['c.js: a home path']);
    const dir = tempDir({ 'b.js': 'export const b = 1;\n' });
    writeFileSync(join(dir, 'd.js.gz'), Buffer.concat([gzipSync('clean\n'), gzipWith('clean\n', { extra: Buffer.from('xx') })]));
    const { findings } = scanTree(dir, { allow, denylist });
    expect(findings.map((f) => `${f.file}: ${f.rule}`)).toEqual(['d.js.gz: a compressed file that does not decompress whole']);
    expect(findings[0].match).toContain('FEXTRA');
  });

  it('refuses an archive, a compressed stream or binary bytes it does not read as text, by name', () => {
    const body = Buffer.concat([Buffer.from([0xff, 0x00]), deflateRawSync(LEAK)]);
    const packed: [string, string, Buffer][] = [
      ['data.zip', 'zip', Buffer.concat([Buffer.from('PK\x03\x04', 'latin1'), body])],
      ['a.xz', 'xz', Buffer.concat([Buffer.from('\xfd7zXZ\x00', 'latin1'), body])],
      ['a.bz2', 'bzip2', Buffer.concat([Buffer.from('BZh9'), body])],
      ['a.7z', '7z', Buffer.concat([Buffer.from('7z\xbc\xaf\x27\x1c', 'latin1'), body])],
      ['a.zst', 'zstd', Buffer.concat([Buffer.from('\x28\xb5\x2f\xfd', 'latin1'), body])],
      ['a.bin', 'zlib', deflateSync(LEAK)],
      ['a.js', 'not UTF-8', brotliCompressSync(`${LEAK}\n`)]
    ];
    for (const [name, format, bytes] of packed) {
      const dir = tempDir({ 'b.js': 'export const b = 1;\n' });
      writeFileSync(join(dir, name), bytes);
      const { findings } = scanTree(dir, { allow, denylist });
      expect(findings.map((f) => `${f.file}: ${f.rule}`), name).toEqual([`${name}: a file that is neither text nor a format the scan reads`]);
      expect(findings[0].match, name).toContain(format);
    }
  });

  it('reads the raw bytes behind a byte-order mark too, so a mark cannot turn ASCII into CJK', () => {
    const marked: [string, Buffer][] = [
      ['le.js', Buffer.concat([Buffer.from([0xff, 0xfe]), Buffer.from(LEAK)])],
      ['be.js', Buffer.concat([Buffer.from([0xfe, 0xff]), Buffer.from(LEAK)])]
    ];
    for (const [name, bytes] of marked) expect(scanOne(name, bytes).findings, name).toEqual([`${name}: a home path`]);
  });

  it('refuses binary bytes behind a byte-order mark, and still reads UTF-16 CJK text', () => {
    const notText = 'a file that is neither text nor a format the scan reads';
    const hidden: [string, Buffer][] = [
      ['gzip.js', Buffer.concat([Buffer.from([0xff, 0xfe]), gzipSync(LEAK)])],
      ['zip.js', Buffer.concat([Buffer.from([0xff, 0xfe]), Buffer.from('PK\x03\x04', 'latin1'), deflateRawSync(LEAK)])],
      ['deflate.js', Buffer.concat([Buffer.from([0xfe, 0xff]), deflateRawSync(LEAK)])],
      // Deflate output with no control byte at all: only its high bytes side by side give it away.
      ['token.js', Buffer.concat([Buffer.from([0xfe, 0xff]), deflateRawSync(`const k = "${'AKIA' + 'FAKE'.repeat(4)}";\n`)])]
    ];
    for (const [name, bytes] of hidden) expect(scanOne(name, bytes).findings, name).toEqual([`${name}: ${notText}`]);
    const cjk = `${'日本語の説明文です。これは公開できる文章です。\n'.repeat(20)}${LEAK}\n`;
    const le = Buffer.concat([Buffer.from([0xff, 0xfe]), Buffer.from(cjk, 'utf16le')]);
    const be = Buffer.concat([Buffer.from([0xfe, 0xff]), Buffer.from(cjk, 'utf16le').swap16()]);
    expect(scanOne('le.txt', le).findings).toEqual(['le.txt: a home path']);
    expect(scanOne('be.txt', be).findings).toEqual(['be.txt: a home path']);
  });

  it('refuses a compressed stream padded with text, or behind a text prefix', () => {
    const notText = 'a file that is neither text nor a format the scan reads';
    const body = Buffer.from(`${LEAK}\n${'y'.repeat(2000)}`);
    const token = Buffer.from(`const k = "${'AKIA' + 'FAKE'.repeat(4)}";\n${'y'.repeat(2000)}`);
    const pad = (stream: Buffer, tail: number[]) =>
      Buffer.concat([Buffer.from(`${'x'.repeat(stream.length * 100)}\n`), stream, Buffer.from(tail)]);
    const padded: [string, Buffer][] = [
      ['brotli.js', pad(brotliCompressSync(body), [0xff])],
      ['deflate.js', pad(deflateRawSync(body), [0xff])],
      ['token.js', pad(deflateRawSync(token), [0xff])],
      ['gzip.js', pad(gzipSync(LEAK), [])]
    ];
    for (const [name, bytes] of padded) expect(scanOne(name, bytes).findings, name).toEqual([`${name}: ${notText}`]);
  });

  it('reads node_modules, .git and .svelte-kit in a bundle like any other directory', () => {
    const dir = tempDir({ 'node_modules/x/index.js': LEAK, '.git/config': LEAK, 'assets/.svelte-kit/x.js': LEAK });
    const { findings } = scanTree(dir, { bundle: true, allow, denylist });
    expect(findings.map((f) => `${f.file}: ${f.rule}`).sort()).toEqual([
      '.git/config: a home path',
      'assets/.svelte-kit/x.js: a home path',
      'node_modules/x/index.js: a home path'
    ]);
  });

  it('refuses a FIFO without reading it, in either mode', () => {
    const dir = tempDir({ 'a.json': '{}' });
    execFileSync('mkfifo', [join(dir, 'pipe')]);
    for (const bundle of [false, true]) {
      const { findings } = scanTree(dir, { bundle, allow, denylist });
      expect(findings.map((f) => `${f.file}: ${f.rule}`), String(bundle)).toEqual(['pipe: a special file, never read']);
    }
  });

  it('scan() refuses a tree it read no file of', () => {
    const empty = tempDir();
    expect(() => scan(empty, { allow, denylist })).toThrow(/scanned 0 files/);
    const fonts = tempDir();
    writeFileSync(join(fonts, 'font.woff2'), Buffer.from([0x77, 0x4f, 0x46, 0x32, 0xff, 0xfe, 0x00, 0x80]));
    expect(() => scan(fonts, { allow, denylist })).toThrow(/scanned 0 files/);
    const linksOnly = tempDir();
    symlinkSync(join(tempDir({ 'a.json': '{}' }), 'a.json'), join(linksOnly, 'a.json'));
    expect(() => scan(linksOnly, { allow, denylist })).toThrow(/scanned 0 files/);
  });

  it('counts a symbolic link it does not follow, never dropping it silently', () => {
    const outside = tempDir({ 'leak.json': `{"by": "someone@${'example'}.org"}` });
    const dir = tempDir({ 'a.json': '{}' });
    symlinkSync(join(outside, 'leak.json'), join(dir, 'linked.json'));
    symlinkSync(outside, join(dir, 'linked-dir'));
    const { findings, links } = scanTree(dir, { allow, denylist });
    expect(findings).toEqual([]);
    expect(links).toEqual(['linked-dir', 'linked.json']);
    const out = (() => {
      try {
        return execFileSync('node', [SCAN, dir], { encoding: 'utf8' });
      } catch (error) {
        return (error as { stdout: string }).stdout;
      }
    })();
    expect(out).toContain('skipped 2 symbolic link(s)');
  });

  it('allows the docs host the tours link to, by its exact name only', () => {
    expect(rules({ 'tour-bundle.ts': `const DOCS = "https://${'kew'}.dev/docs/";` })).toEqual([]);
    expect(rules({ 'x.json': `{"docs": "${'kew'}.dev"}` })).toEqual([]);
  });

  it('stays quiet on file names, versions, allowlisted hosts, the invented owner and "task-"', () => {
    const run = termOf('a factory run id');
    const text = [
      '{"a": "task-1", "b": "sk-", "t": "task-' + 'k'.repeat(40) + '"',
      `"c": "x${run}y"`,
      '"files": ["manifest.json", "kew.toml", "tests/test_import.py", "deploy.sh", "README.md", "svelte.config.js"]',
      '"version": "0.9.0+demo", "image": "kew-agent:0.9.0", "producer": "kew.dispatch"',
      '"hosts": ["svelte.dev", "kew.dev", "docs.anthropic.com"], "url": "https://github.com/acme/ledger/pull/207"',
      '"repo": "acme/ledger", "delivery_repo": "acme/ledger", "project.repo": {"value": "acme/ledger"}}'
    ].join(', ');
    expect(scan(tempDir({ 'x.json': text }), { allow, denylist }).map(describeFinding)).toEqual([]);
  });

  describe('--bundle', () => {
    it("allows framework vendor hosts and links to kew's own pull requests", () => {
      const owner = termOf('a factory owner');
      const text = `a="https://svelte.dev/e/x";b="https://github.com/${owner}/kew/pull/3141"`;
      expect(rules({ '_app/a.js': text }, true)).toEqual([]);
    });

    it('reads a minified member access as code, never as a host', () => {
      // d3's `n.local` (attribute namespaces): a one- or two-letter name, two
      // labels, not opening a string. Every other private-TLD name is a host.
      const text = `return n.local?r.getAttributeNS(n.space,n.local):e.home;x=t.internal;y=ab.lan`;
      expect(rules({ '_app/a.js': text }, true)).toEqual([]);
    });

    it.each([
      `const h=x?"mac.${'local'}":y`,
      `host=studio.${'local'};`,
      `n=nas.${'lan'}`,
      'u=`http://${h}`;h=studio.' + 'internal',
      `/* built on buildbox.${'local'} */`,
      ` macmini.${'local'} `,
      `h=macmini.${'local'}`,
      `(macmini.${'home'})`,
      `[a,macmini.${'home'}]`,
      `x=build.${'corp'}`,
      `"kew-verify.tail1a2b3.${'ts'}.net"`,
      `"n.${'local'}"`
    ])('refuses a machine name on a private TLD in any context: %s', (text) => {
      expect(rules({ '_app/a.js': text }, true)).toContain('a host not on the allowlist');
    });

    it('still refuses paths, emails, tokens, hosts off its list, machine names and the factory denylist', () => {
      const run = termOf('a factory run id');
      const text = [
        `"/${'home'}/someone"`,
        `"someone@${'example'}.org"`,
        `"${TOKENS['a GitHub token'][0]}"`,
        `"https://${'tracker'}.example.org"`,
        `"http://34.123.45.${'67'}:8765"`,
        `"someones-laptop.${'local'}"`,
        `"${run}"`
      ].join(';');
      expect(rules({ '_app/a.js': text }, true)).toEqual([
        'a home path',
        'an email address',
        'a GitHub token',
        'a host not on the allowlist',
        'a host not on the allowlist',
        'a host not on the allowlist',
        'a factory run id'
      ]);
    });

    it('reads a precompressed copy decompressed', () => {
      const dir = tempDir();
      writeFileSync(join(dir, 'a.js.gz'), gzipSync(`"someone@${'example'}.org"`));
      expect(scan(dir, { bundle: true, allow, denylist }).map((f) => f.rule)).toEqual(['an email address']);
    });
  });

  describe('release provenance', () => {
    // The real export's shapes, holding the real factory's names where the
    // export holds them by design: a denylisted epic ref and run id, and a PR
    // link on a denylisted owner.
    const epic = () => termOf('a factory epic ref');
    const runId = () => `${termOf('a factory run id')}-0000-4000-8000-000000000001`;
    const pr = () => `https://github.com/${termOf('a factory owner')}/kew/pull/12`;
    const feature = (over: Record<string, unknown> = {}) => ({
      id: 'epic-graph',
      title: 'Epic graph',
      routes: ['/epics/[ref]'],
      epics: [epic()],
      issues: ['2843'],
      prs: [{ url: pr(), landed_sha: null }],
      ...over
    });
    const provenance = (over: Record<string, unknown> = {}) =>
      JSON.stringify({ schema: 'kew/provenance@1', features: [feature(over)] });
    const runs = (over: Record<string, unknown> = {}) =>
      JSON.stringify({ runs: [{ run_id: runId(), item_id: '2843', epic: epic(), pr_url: pr(), model: 'other', ...over }] });
    const toml = (epics: string) =>
      `[[features]]\nid = "epic-graph"\ntitle = "Epic graph"\nepics = ["${epics}"]\nissues = [2843]\n`;

    it('skips the owner rule and the denylist only in the keys the allowlist names, in source mode', () => {
      expect(
        rules({
          'provenance/provenance.json': provenance(),
          'provenance/runs.json': runs(),
          'features.toml': toml(epic()),
          'public-repo.json': `{"about": "kew itself", "repo": "${termOf('a real repository')}"}`
        })
      ).toEqual([]);
    });

    it("skips only the denylist in the bundled copy's epics, and refuses a PR link to a repository not named kew", () => {
      expect(rules({ 'demo-provenance.json': provenance() }, true)).toEqual([]);
      const url = `https://github.com/${termOf('a factory owner')}/private-client-repo/pull/12`;
      expect(rules({ 'demo-provenance.json': provenance({ prs: [{ url, landed_sha: null }] }) }, true)).toContain(
        'a GitHub owner other than the invented one'
      );
    });

    it.each([
      ['title', 'a factory run id', () => termOf('a factory run id')],
      ['title', 'a factory epic ref', () => termOf('a factory epic ref')],
      ['id', 'a GitHub owner other than the invented one', () => `https://github.com/${termOf('a factory owner')}/x`],
      ['routes', 'a home path', () => [`/${'Users'}/someone/x`]],
      ['issues', 'an email address', () => [`someone@${'example'}.org`]],
      ['title', 'a GitHub token', () => TOKENS['a GitHub token'][0]],
      ['title', 'a host not on the allowlist', () => `https://${'tracker'}.example.org/x`],
      ['title', 'a host not on the allowlist', () => `someones-laptop.${'local'}`]
    ] as [string, string, () => unknown][])('refuses a name planted in any other key of the provenance: %s, %s', (key, rule, value) => {
      expect(rules({ 'demo-provenance.json': provenance({ [key]: value() }) }, true)).toContain(rule);
      expect(rules({ 'provenance/provenance.json': provenance({ [key]: value() }) })).toContain(rule);
    });

    it.each([false, true])('still refuses a path, an email, a token or a host inside an exempt key (bundle: %s)', (bundle) => {
      const planted = [`/${'Users'}/someone`, `someone@${'example'}.org`, TOKENS['a Linear key'][0], `someones-laptop.${'local'}`];
      const file = bundle ? 'demo-provenance.json' : 'provenance/provenance.json';
      expect(rules({ [file]: provenance({ epics: planted }) }, bundle)).toEqual([
        'a home path',
        'an email address',
        'a Linear key',
        'a host not on the allowlist'
      ]);
    });

    it('skips in an exempt key only the rules named for it', () => {
      // `runs[].epic` skips the denylist only: an owner link there is still refused.
      const owner = `https://github.com/${termOf('a factory owner')}/x`;
      expect(rules({ 'provenance/runs.json': runs({ epic: owner }) })).toContain('a GitHub owner other than the invented one');
      // `runs[].item_id` skips nothing.
      expect(rules({ 'provenance/runs.json': runs({ item_id: termOf('a factory run id') }) })).toEqual(['a factory run id']);
      // features.toml skips the denylist on its `epics` line only.
      expect(rules({ 'features.toml': `${toml(epic())}notes = "${epic()}"\n` })).toEqual(['a factory epic ref']);
    });

    it('gives a key holding ".", "[" or "]" no exempt path, at any depth', () => {
      // A key spelled like a path would otherwise land on the path it spells.
      const owner = 'a GitHub owner other than the invented one';
      const nested = JSON.stringify({ features: [{ 'prs[].url': pr(), epics: [epic()] }] });
      const top = JSON.stringify({ 'features[].prs[].url': pr() });
      const below = JSON.stringify({ 'features[]': { prs: [{ url: pr() }] } });
      expect(rules({ 'provenance/provenance.json': nested })).toContain(owner);
      expect(rules({ 'provenance/provenance.json': top })).toContain(owner);
      expect(rules({ 'provenance/provenance.json': below })).toContain(owner);
      expect(rules({ 'demo-provenance.json': JSON.stringify({ 'features[].epics[]': epic() }) }, true)).toEqual([
        'a factory epic ref'
      ]);
    });

    it('applies no exemption to the same file at another path or in the other mode', () => {
      expect(rules({ 'demo-provenance.json': provenance() })).not.toEqual([]);
      expect(rules({ 'world/demo-provenance.json': provenance() }, true)).not.toEqual([]);
      expect(rules({ '_app/immutable/demo-provenance.json': provenance() }, true)).not.toEqual([]);
      expect(rules({ 'provenance/provenance.json': provenance() }, true)).not.toEqual([]);
    });

    it('names each exempt file by exact path and mode, never by pattern, and never a sample', () => {
      expect(exemptionOf('provenance/runs.json', allow, 'source')).not.toBeNull();
      expect(exemptionOf('demo/provenance/runs.json', allow, 'source')).not.toBeNull();
      expect(exemptionOf('frontend/demo/features.toml', allow, 'source')).not.toBeNull();
      expect(exemptionOf('demo-provenance.json', allow, 'bundle')).not.toBeNull();
      expect(exemptionOf('demo-provenance.json', allow, 'source')).toBeNull();
      expect(exemptionOf('demo/demo-provenance.json', allow, 'source')).toBeNull();
      expect(exemptionOf('demo/demo-provenance.json', allow, 'bundle')).toBeNull();
      expect(exemptionOf('provenance/runs.json', allow, 'bundle')).toBeNull();
      expect(exemptionOf('provenance/provenance.sample.json', allow, 'source')).toBeNull();
      expect(exemptionOf('provenance/runs.sample.json', allow, 'source')).toBeNull();
      expect(exemptionOf('provenance/a/b.json', allow, 'source')).toBeNull();
      expect(exemptionOf('world/provenance-runs.json', allow, 'source')).toBeNull();
      expect(exemptionOf('world/demo/provenance/runs.json', allow, 'source')).toBeNull();
      const entries = allow.exempt as { path: string; mode: string; keys: Record<string, string[]> }[];
      for (const entry of entries) {
        expect(entry.path, entry.path).not.toMatch(/[*?[]/);
        expect(['source', 'bundle']).toContain(entry.mode);
        expect(Object.keys(entry.keys).length, entry.path).toBeGreaterThan(0);
        for (const skipped of Object.values(entry.keys)) for (const rule of skipped) expect(['owner', 'denylist']).toContain(rule);
      }
    });
  });

  it('is one command: exit 0 on a clean tree, 1 with a line per finding', () => {
    expect(execFileSync('node', [SCAN, fileURLToPath(DEMO)], { encoding: 'utf8' })).toMatch(/is clean/);
    const dirty = tempDir({ 'x.json': `{"by": "someone@${'example'}.org"}` });
    let status = 0;
    let out = '';
    try {
      execFileSync('node', [SCAN, dirty], { encoding: 'utf8' });
    } catch (error) {
      status = (error as { status: number }).status;
      out = (error as { stdout: string }).stdout;
    }
    expect(status).toBe(1);
    expect(out).toContain(`x.json:1: an email address — someone@${'example'}.org`);
    expect(out).toContain('skipped 0 binary file(s)');
  });
});

describe('the leak scan command', () => {
  const run = (script: string, dir: string) => {
    try {
      return { status: 0, stdout: execFileSync('node', [script, dir], { encoding: 'utf8', stdio: 'pipe' }), stderr: '' };
    } catch (error) {
      const failed = error as { status: number; stdout: string; stderr: string };
      return { status: failed.status, stdout: failed.stdout, stderr: failed.stderr };
    }
  };

  it('scans its files when run through a symlinked directory', () => {
    const dir = tempDir({ 'a.json': '{}' });
    const linked = join(tempDir(), 'demo');
    symlinkSync(fileURLToPath(DEMO), linked);
    const result = run(join(linked, 'leak-scan.mjs'), dir);
    expect(result.status).toBe(0);
    expect(result.stdout).toContain(`leak scan: ${dir} is clean`);
  });

  it('fails a scan of an empty directory, whatever path ran it', () => {
    const empty = tempDir();
    const linked = join(tempDir(), 'demo');
    symlinkSync(fileURLToPath(DEMO), linked);
    for (const script of [SCAN, join(linked, 'leak-scan.mjs')]) {
      const result = run(script, empty);
      expect(result.status, script).not.toBe(0);
      expect(result.stderr, script).toContain('scanned 0 files');
      expect(result.stdout, script).not.toContain('is clean');
    }
  });
});

// A built `build-demo/` is scanned by `ui-checks/demo-world.mjs`, right after
// the build every `ui-check` run naming a demo module makes: every bundle rule,
// not only the factory denylist. No test here passes when the build is absent.
