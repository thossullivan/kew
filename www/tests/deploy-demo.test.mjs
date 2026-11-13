import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { access, cp, mkdir, mkdtemp, readdir, readFile, rm, symlink, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import test from 'node:test';
import { promisify } from 'node:util';
import { fileURLToPath } from 'node:url';

const execFileAsync = promisify(execFile);
const www = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const repo = resolve(www, '..');

/**
 * The parts of the repository the script reaches: itself, the real leak scan
 * with its lists, the real demo world (timings.json included), the committed
 * provenance samples, the static assets, and both ignore files (so the
 * planted files below are really gitignored, as they are in the repository).
 */
const COPIED = [
  '.gitignore',
  'www/deploy-demo.sh',
  'frontend/.gitignore',
  'frontend/demo/leak-scan.mjs',
  'frontend/demo/leak-scan.allow.json',
  'frontend/demo/leak-scan.deny.json',
  'frontend/demo/provenance',
  'frontend/demo/world',
  'frontend/fixtures/factory',
  'frontend/static'
];

const APPROVAL = { KEW_DEMO_PUBLISH_APPROVED: 'publish-demo.kew.dev' };
const CREDS = { DEPLOY_HOST: 'box.example', DEPLOY_USER: 'deploy' };

/**
 * A temp git checkout on `main` (with `origin/main` at HEAD) holding the
 * script and the real leak scan, with `npm`, `ssh` and `rsync` stubbed on
 * PATH. The `npm` stub stands in for `npm ci` (a no-op) and `npm run
 * build:demo` (there is no node_modules here), run in whatever directory the
 * script builds in: it writes the files in KEW_STUB_BUILD (JSON, path →
 * content) and the symlinks in KEW_STUB_LINKS (path → target) into
 * build-demo/. With KEW_STUB_VITE=1 it also does what vite does with the
 * build's inputs: copies every file under static/ and bundles every *.json
 * under demo/world/ (sub-folders too) into one chunk, and ships the real
 * demo/provenance/provenance.json (else the sample) as demo-provenance.json,
 * as the provenance plugin does; with KEW_STUB_SEEN set it copies the
 * export's demo/provenance/ there, so a test can read what the build read.
 * The `ssh` and `rsync`
 * stubs record each call in a log, so "never invoked" is a missing file.
 * TMPDIR is a dedicated directory, so a run that leaves its export behind is
 * visible.
 */
async function makeCheckout(t) {
  const temporary = await mkdtemp(join(tmpdir(), 'kew-deploy-demo-test-'));
  t.after(() => rm(temporary, { recursive: true, force: true }));
  const checkout = join(temporary, 'checkout');
  const bin = join(temporary, 'bin');
  const tmp = join(temporary, 'tmp');
  const remoteLog = join(temporary, 'remote.log');
  await mkdir(bin);
  await mkdir(tmp);
  for (const path of COPIED) {
    await cp(join(repo, path), join(checkout, path), { recursive: true });
  }
  await writeFile(
    join(bin, 'npm'),
    `#!/bin/sh
printf '%s\\n' "$*" >> "$KEW_STUB_NPM_LOG"
[ "$*" = 'run build:demo' ] || exit 0
exec node -e '
const fs = require("node:fs"), path = require("node:path");
const out = "build-demo";
for (const [file, text] of Object.entries(JSON.parse(process.env.KEW_STUB_BUILD))) {
  fs.mkdirSync(path.dirname(path.join(out, file)), { recursive: true });
  fs.writeFileSync(path.join(out, file), text);
}
for (const [file, target] of Object.entries(JSON.parse(process.env.KEW_STUB_LINKS || "{}"))) {
  fs.mkdirSync(path.dirname(path.join(out, file)), { recursive: true });
  fs.symlinkSync(target, path.join(out, file));
}
if (process.env.KEW_STUB_VITE === "1") {
  if (fs.existsSync("static")) fs.cpSync("static", out, { recursive: true });
  const world = [];
  const walk = (dir) => {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const p = path.join(dir, entry.name);
      if (entry.isDirectory()) walk(p);
      else if (entry.name.endsWith(".json")) world.push(fs.readFileSync(p, "utf8"));
    }
  };
  walk("demo/world");
  fs.mkdirSync(path.join(out, "_app/immutable"), { recursive: true });
  fs.writeFileSync(path.join(out, "_app/immutable/fixtures.js"), "export default [" + world.join(",") + "];\\n");
  const real = "demo/provenance/provenance.json";
  fs.copyFileSync(fs.existsSync(real) ? real : "demo/provenance/provenance.sample.json", path.join(out, "demo-provenance.json"));
}
if (process.env.KEW_STUB_SEEN) fs.cpSync("demo/provenance", process.env.KEW_STUB_SEEN, { recursive: true });'
`,
    { mode: 0o755 }
  );
  for (const tool of ['ssh', 'rsync']) {
    await writeFile(join(bin, tool), `#!/bin/sh\nprintf '${tool} %s\\n' "$*" >> "$KEW_STUB_REMOTE_LOG"\n`, {
      mode: 0o755
    });
  }
  const git = (...args) =>
    execFileAsync('git', ['-c', 'user.name=test', '-c', 'user.email=test@example.invalid', ...args], {
      cwd: checkout
    });
  await git('init', '-q', '-b', 'main');
  await git('add', '-A');
  await git('commit', '-q', '-m', 'fixture');
  await git('update-ref', 'refs/remotes/origin/main', 'HEAD');
  return { temporary, checkout, bin, tmp, remoteLog, npmLog: join(temporary, 'npm.log'), git };
}

/** Commit `text` at `path` in the checkout and move origin/main with it, so the tree stays clean and current. */
async function commitFile(ctx, path, text) {
  await mkdir(dirname(join(ctx.checkout, path)), { recursive: true });
  await writeFile(join(ctx.checkout, path), text);
  await ctx.git('add', '-A');
  await ctx.git('commit', '-q', '-m', `change ${path}`);
  await ctx.git('update-ref', 'refs/remotes/origin/main', 'HEAD');
}

/** Plant a file the checkout's ignore rules hide from `git status`, and prove they do. */
async function plantIgnored(ctx, path, text) {
  await mkdir(dirname(join(ctx.checkout, path)), { recursive: true });
  await writeFile(join(ctx.checkout, path), text);
  await ctx.git('check-ignore', '-q', path);
  const { stdout } = await ctx.git('status', '--porcelain');
  assert.equal(stdout, '', `${path} is not hidden from git status`);
}

const CLEAN_BUILD = {
  'index.html': '<!doctype html><html><body><div id="app"></div></body></html>\n',
  '_app/immutable/entry.js': 'export const demo = "a synthetic factory";\n'
};

async function run(ctx, { args = [], build = CLEAN_BUILD, env = {} } = {}) {
  const environment = {
    PATH: `${ctx.bin}:${process.env.PATH}`,
    HOME: process.env.HOME,
    TMPDIR: ctx.tmp,
    KEW_STUB_BUILD: JSON.stringify(build),
    KEW_STUB_NPM_LOG: ctx.npmLog,
    KEW_STUB_REMOTE_LOG: ctx.remoteLog,
    ...env
  };
  try {
    const { stdout, stderr } = await execFileAsync('bash', [join(ctx.checkout, 'www', 'deploy-demo.sh'), ...args], {
      env: environment
    });
    return { code: 0, stdout, stderr };
  } catch (error) {
    if (typeof error.code !== 'number') throw error;
    return { code: error.code, stdout: error.stdout, stderr: error.stderr };
  }
}

const exists = (path) =>
  access(path).then(
    () => true,
    () => false
  );

/** Every file's text under `dir`, concatenated: what the staged bundle would publish. */
async function treeText(dir) {
  const parts = [];
  for (const entry of await readdir(dir, { recursive: true, withFileTypes: true })) {
    if (entry.isFile()) parts.push(await readFile(join(entry.parentPath, entry.name), 'utf8'));
  }
  return parts.join('\n');
}

const staged = (ctx) => join(ctx.checkout, 'frontend', 'build-demo');

/** The real date a test pair carries, so a shipped file can be told from the sample. */
const REAL_AS_OF = '2026-10-28T12:00:00+00:00';

/**
 * A `kew report provenance` pair outside the checkout, built from the
 * committed samples with the real date: `edit` changes the parsed
 * provenance.json, and `omit` leaves a file out.
 */
async function provenancePair(ctx, { edit = (doc) => doc, omit = [] } = {}) {
  const dir = join(ctx.temporary, 'provenance');
  await mkdir(dir, { recursive: true });
  const samples = join(ctx.checkout, 'frontend', 'demo', 'provenance');
  for (const name of ['provenance', 'runs']) {
    if (omit.includes(name)) continue;
    const doc = JSON.parse(await readFile(join(samples, `${name}.sample.json`), 'utf8'));
    doc.as_of = REAL_AS_OF;
    await writeFile(join(dir, `${name}.json`), `${JSON.stringify(name === 'provenance' ? edit(doc) : doc, null, 2)}\n`);
  }
  return dir;
}

test('a clean run on main builds a clean export, scans and prints the dry-run plan without touching the box', async (t) => {
  const ctx = await makeCheckout(t);
  const result = await run(ctx);
  assert.equal(result.code, 0, result.stderr);
  assert.match(result.stdout, /leak scan: demo is clean/);
  assert.match(result.stdout, /leak scan: build-demo is clean/);
  assert.match(result.stdout, /Dry run: nothing was published/);
  assert.match(result.stdout, /rsync -avz --delete .* frontend\/build-demo\/ DEPLOY_USER@DEPLOY_HOST:\/home\/DEPLOY_USER\/sites\/kew-demo\//);
  assert.match(result.stdout, /^ {4}_app\/immutable\/entry\.js$/m);
  assert.match(result.stdout, /^ {4}index\.html$/m);
  assert.equal(await exists(join(staged(ctx), 'index.html')), true, 'the build was not staged in frontend/build-demo/');
  assert.deepEqual((await readFile(ctx.npmLog, 'utf8')).trim().split('\n'), ['ci --no-audit --no-fund', 'run build:demo']);
  assert.deepEqual(await readdir(ctx.tmp), [], 'the clean export was left behind');
  assert.equal(await exists(ctx.remoteLog), false, 'a dry run invoked ssh or rsync');
});

test('a dry run prints the demo-check command to run after a real deploy', async (t) => {
  const ctx = await makeCheckout(t);
  const result = await run(ctx);
  assert.equal(result.code, 0, result.stderr);
  assert.match(result.stdout, /^ {2}After a real deploy, check the live demo: cd frontend && npm run demo-check -- https:\/\/demo\.kew\.dev$/m);
  // It prints the command and runs nothing more: npm saw only the build.
  assert.deepEqual((await readFile(ctx.npmLog, 'utf8')).trim().split('\n'), ['ci --no-audit --no-fund', 'run build:demo']);
  assert.equal(await exists(ctx.remoteLog), false, 'a dry run invoked ssh or rsync');

  const published = await run(ctx, { args: ['--publish', '--provenance', await provenancePair(ctx)], env: { ...CREDS, ...APPROVAL } });
  assert.equal(published.code, 0, published.stderr);
  const tail = published.stdout.trim().split('\n').at(-1);
  assert.equal(tail, '  Then check it: cd frontend && npm run demo-check -- https://demo.kew.dev');
  const npmCalls = (await readFile(ctx.npmLog, 'utf8')).trim().split('\n');
  assert.deepEqual(npmCalls.slice(2), ['ci --no-audit --no-fund', 'run build:demo'], 'the publish ran npm for more than the build');
});

test('a dry run with deploy credentials set still invokes no ssh or rsync', async (t) => {
  const ctx = await makeCheckout(t);
  const result = await run(ctx, { env: { ...CREDS, ...APPROVAL } });
  assert.equal(result.code, 0, result.stderr);
  assert.match(result.stdout, /deploy@box\.example:\/home\/deploy\/sites\/kew-demo\//);
  assert.equal(await exists(ctx.remoteLog), false, 'a dry run invoked ssh or rsync');
});

test('a dirty checkout refuses before anything is built', async (t) => {
  const ctx = await makeCheckout(t);
  await writeFile(join(ctx.checkout, 'frontend', 'demo', 'untracked.ts'), 'export {};\n');
  const result = await run(ctx);
  assert.equal(result.code, 1);
  assert.match(result.stderr, /refusing to deploy from a dirty checkout/);
  assert.match(result.stderr, /frontend\/demo\/untracked\.ts/);
  assert.equal(await exists(ctx.npmLog), false, 'a dirty checkout was built');
  assert.equal(await exists(ctx.remoteLog), false);
});

test('a branch other than main refuses before anything is built', async (t) => {
  const ctx = await makeCheckout(t);
  await ctx.git('checkout', '-q', '-b', 'feature');
  const result = await run(ctx);
  assert.equal(result.code, 1);
  assert.match(result.stderr, /refusing to deploy from 'feature'/);
  assert.equal(await exists(ctx.npmLog), false, 'a non-main branch was built');
  assert.equal(await exists(ctx.remoteLog), false);
});

test('main ahead of origin/main refuses before anything is built', async (t) => {
  const ctx = await makeCheckout(t);
  await writeFile(join(ctx.checkout, 'frontend', 'demo', 'local.ts'), 'export {};\n');
  await ctx.git('add', '-A');
  await ctx.git('commit', '-q', '-m', 'local only');
  const result = await run(ctx);
  assert.equal(result.code, 1);
  assert.match(result.stderr, /HEAD .* is not origin\/main/);
  assert.equal(await exists(ctx.npmLog), false, 'a commit not on origin/main was built');
});

test('a checkout with no origin/main ref refuses before anything is built', async (t) => {
  const ctx = await makeCheckout(t);
  await ctx.git('update-ref', '-d', 'refs/remotes/origin/main');
  const result = await run(ctx);
  assert.equal(result.code, 1);
  assert.match(result.stderr, /origin\/main/);
  assert.equal(await exists(ctx.npmLog), false);
});

test('a gitignored JSON file under demo/world/ never reaches the staged bundle', async (t) => {
  const ctx = await makeCheckout(t);
  await plantIgnored(ctx, 'frontend/demo/world/dist/extra.json', '{"planted": "ignored-world-marker"}\n');
  const result = await run(ctx, { env: { KEW_STUB_VITE: '1' } });
  assert.equal(result.code, 0, result.stderr);
  const text = await treeText(staged(ctx));
  // The stub bundled the committed world, so an absent marker means the build did not see the file.
  assert.match(text, /"min_runs": 20/);
  assert.doesNotMatch(text, /ignored-world-marker/);
});

test('a gitignored file under static/ never reaches the staged bundle', async (t) => {
  const ctx = await makeCheckout(t);
  await plantIgnored(ctx, 'frontend/static/logs/hosts.txt', 'ignored-static-marker\n');
  const result = await run(ctx, { env: { KEW_STUB_VITE: '1' } });
  assert.equal(result.code, 0, result.stderr);
  assert.equal(await exists(join(staged(ctx), 'favicon.svg')), true, 'the stub did not copy the committed static/');
  assert.equal(await exists(join(staged(ctx), 'logs')), false);
  assert.doesNotMatch(await treeText(staged(ctx)), /ignored-static-marker/);
  assert.doesNotMatch(result.stdout, /hosts\.txt/);
});

test('a stale frontend/build-demo/ from an earlier run neither refuses nor rides along', async (t) => {
  const ctx = await makeCheckout(t);
  await plantIgnored(ctx, 'frontend/build-demo/_app/immutable/stale.js', 'export const stale = 1;\n');
  const result = await run(ctx);
  assert.equal(result.code, 0, result.stderr);
  assert.equal(await exists(join(staged(ctx), '_app', 'immutable', 'stale.js')), false);
  assert.doesNotMatch(result.stdout, /stale\.js/);
});

test('a bare public host in the committed demo source refuses through the source-mode scan, before building', async (t) => {
  const ctx = await makeCheckout(t);
  await commitFile(ctx, 'frontend/demo/world/extra.json', '{"host": "ci.acmecorp.com"}\n');
  const result = await run(ctx);
  assert.equal(result.code, 1);
  assert.match(result.stdout, /world\/extra\.json:1: a host not on the allowlist — ci\.acmecorp\.com/);
  assert.match(result.stderr, /the leak scan found something in frontend\/demo\//);
  assert.equal(await exists(ctx.npmLog), false, 'a tree that failed the source scan was built');
  assert.deepEqual(await readdir(ctx.tmp), [], 'the clean export was left behind');
});

test('a corpus-sourced timings.json refuses before building', async (t) => {
  const ctx = await makeCheckout(t);
  const path = join(ctx.checkout, 'frontend', 'demo', 'world', 'timings.json');
  const timings = JSON.parse(await readFile(path, 'utf8'));
  await commitFile(ctx, 'frontend/demo/world/timings.json', JSON.stringify({ ...timings, source: 'corpus' }, null, 2));
  const result = await run(ctx);
  assert.equal(result.code, 1);
  assert.match(result.stderr, /timings\.json is not "source": "fake"/);
  assert.match(result.stderr, /owner's approval/);
  assert.equal(await exists(ctx.npmLog), false);
});

test('a home-directory path planted in the build refuses through the real leak scan', async (t) => {
  const ctx = await makeCheckout(t);
  const build = { ...CLEAN_BUILD, '_app/immutable/chunk.js': 'const root = "/Users/someone/src/kew";\n' };
  const result = await run(ctx, { build });
  assert.equal(result.code, 1);
  assert.match(result.stdout, /_app\/immutable\/chunk\.js:1: /);
  assert.match(result.stderr, /the leak scan found something/);
  assert.doesNotMatch(result.stdout, /Dry run/);
  assert.deepEqual(await readdir(ctx.tmp), [], 'the clean export was left behind');
  assert.equal(await exists(ctx.remoteLog), false);
});

test('a source map in the build refuses', async (t) => {
  const ctx = await makeCheckout(t);
  const build = { ...CLEAN_BUILD, '_app/immutable/entry.js.map': '{"version":3,"mappings":""}\n' };
  const result = await run(ctx, { build });
  assert.equal(result.code, 1);
  assert.match(result.stderr, /holds source maps/);
  assert.match(result.stderr, /entry\.js\.map/);
  assert.doesNotMatch(result.stdout, /Dry run/);
  assert.equal(await exists(ctx.remoteLog), false);
});

test('an inline data: source map in the build refuses', async (t) => {
  const ctx = await makeCheckout(t);
  const build = {
    ...CLEAN_BUILD,
    '_app/immutable/chunk.js': 'export const a = 1;\n//# sourceMappingURL=data:application/json;base64,eyJ2ZXJzaW9uIjozfQ==\n'
  };
  const result = await run(ctx, { build });
  assert.equal(result.code, 1);
  assert.match(result.stderr, /inline source map/);
  assert.match(result.stderr, /chunk\.js/);
  assert.doesNotMatch(result.stdout, /Dry run/);
});

test('a symbolic link in the build refuses', async (t) => {
  const ctx = await makeCheckout(t);
  const result = await run(ctx, { env: { KEW_STUB_LINKS: JSON.stringify({ 'leak.txt': '/etc/hosts' }) } });
  assert.equal(result.code, 1);
  assert.match(result.stderr, /symbolic link/);
  assert.match(result.stderr, /leak\.txt/);
  assert.doesNotMatch(result.stdout, /Dry run/);
});

test('a build that leaves no files refuses', async (t) => {
  const ctx = await makeCheckout(t);
  const result = await run(ctx, { build: {} });
  assert.equal(result.code, 1);
  assert.match(result.stderr, /left no files/);
  assert.equal(await exists(ctx.remoteLog), false);
});

test('--publish without DEPLOY_HOST and DEPLOY_USER refuses before building', async (t) => {
  const ctx = await makeCheckout(t);
  const result = await run(ctx, { args: ['--publish'] });
  assert.notEqual(result.code, 0);
  assert.match(result.stderr, /DEPLOY_HOST/);
  assert.equal(await exists(ctx.npmLog), false);
  assert.equal(await exists(ctx.remoteLog), false);
});

test('--publish with kew.dev deploy credentials but no demo approval refuses before building', async (t) => {
  const ctx = await makeCheckout(t);
  for (const approval of [{}, { KEW_DEMO_PUBLISH_APPROVED: '1' }, { KEW_DEMO_PUBLISH_APPROVED: 'yes' }]) {
    const result = await run(ctx, { args: ['--publish'], env: { ...CREDS, ...approval } });
    assert.equal(result.code, 1, JSON.stringify(approval));
    assert.match(result.stderr, /KEW_DEMO_PUBLISH_APPROVED/);
    assert.match(result.stderr, /owner's approval/);
  }
  assert.equal(await exists(ctx.npmLog), false);
  assert.equal(await exists(ctx.remoteLog), false);
});

test('an unknown argument prints usage and does nothing', async (t) => {
  const ctx = await makeCheckout(t);
  const result = await run(ctx, { args: ['--dry-run'] });
  assert.equal(result.code, 2);
  assert.match(result.stderr, /usage: .* \[--publish\]/);
  assert.equal(await exists(ctx.npmLog), false);
});

test('more than one argument prints usage and does nothing, even with --publish first', async (t) => {
  const ctx = await makeCheckout(t);
  for (const args of [['--publish', '--dry-run'], ['--publish', '--publish'], ['--publish', '']]) {
    const result = await run(ctx, { args, env: { ...CREDS, ...APPROVAL } });
    assert.equal(result.code, 2, args.join(' '));
    assert.match(result.stderr, /usage: .* \[--publish\]/);
  }
  assert.equal(await exists(ctx.npmLog), false);
  assert.equal(await exists(ctx.remoteLog), false);
});

test('a remote directory that is not a kew-demo directory refuses before building', async (t) => {
  const ctx = await makeCheckout(t);
  for (const dir of [
    '/home/deploy/sites/kew',
    '/home/deploy',
    '/home/deploy/sites/kew-demo/',
    'sites/kew-demo',
    '/home/deploy/sites/../kew-demo',
    "/home/deploy/sites/x'; rm -rf ~; '/kew-demo",
    '/home/deploy/sites/kew demo/kew-demo'
  ]) {
    const result = await run(ctx, { env: { KEW_DEMO_REMOTE_DIR: dir } });
    assert.equal(result.code, 1, dir);
    assert.match(result.stderr, /KEW_DEMO_REMOTE_DIR/, dir);
  }
  const quoted = await run(ctx, { env: { DEPLOY_USER: "de'ploy", DEPLOY_HOST: 'box.example' } });
  assert.equal(quoted.code, 1, 'a quote in DEPLOY_USER reached the default remote dir');
  assert.equal(await exists(ctx.npmLog), false);
  assert.equal(await exists(ctx.remoteLog), false);
});

test('a kew-demo remote directory override is used', async (t) => {
  const ctx = await makeCheckout(t);
  const result = await run(ctx, { env: { KEW_DEMO_REMOTE_DIR: '/srv/sites_2/kew-demo' } });
  assert.equal(result.code, 0, result.stderr);
  assert.match(result.stdout, /DEPLOY_USER@DEPLOY_HOST:\/srv\/sites_2\/kew-demo\//);
});

test('--publish names the owner approval before it reaches the box', async (t) => {
  const ctx = await makeCheckout(t);
  const result = await run(ctx, { args: ['--publish', '--provenance', await provenancePair(ctx)], env: { ...CREDS, ...APPROVAL } });
  assert.equal(result.code, 0, result.stderr);
  const approval = result.stdout.indexOf("demo.kew.dev goes public only with the owner's approval.");
  const firstRemote = result.stdout.indexOf('==> Ensuring');
  assert.ok(approval !== -1 && approval < firstRemote, result.stdout);
  const calls = (await readFile(ctx.remoteLog, 'utf8')).trim().split('\n');
  assert.equal(calls.length, 2);
  assert.match(calls[0], /^ssh .*deploy@box\.example mkdir -p '\/home\/deploy\/sites\/kew-demo'$/);
  assert.match(calls[1], /^rsync -avz --delete .*\/build-demo\/ deploy@box\.example:\/home\/deploy\/sites\/kew-demo\/$/);
  assert.deepEqual(await readdir(ctx.tmp), [], 'the clean export was left behind');
});

const BAD_NAMES = [
  { DEPLOY_HOST: '-oProxyCommand=x', DEPLOY_USER: 'deploy' },
  { DEPLOY_HOST: 'box.example', DEPLOY_USER: '-l' },
  { DEPLOY_HOST: '-box', DEPLOY_USER: 'deploy' },
  { DEPLOY_HOST: 'box example', DEPLOY_USER: 'deploy' }
];

test('a DEPLOY_HOST or DEPLOY_USER that could be read as an option refuses before ssh or rsync, even with --publish', async (t) => {
  const ctx = await makeCheckout(t);
  for (const creds of BAD_NAMES) {
    const result = await run(ctx, { args: ['--publish'], env: { ...creds, ...APPROVAL } });
    assert.equal(result.code, 1, JSON.stringify(creds));
    assert.match(result.stderr, /deploy-demo: refusing: DEPLOY_(HOST|USER) .*not starting with '-'/, JSON.stringify(creds));
  }
  assert.equal(await exists(ctx.npmLog), false, 'a refused run was built');
  assert.equal(await exists(ctx.remoteLog), false, 'a refused run invoked ssh or rsync');
});

test('--publish puts -- before the destination of ssh and rsync', async (t) => {
  const ctx = await makeCheckout(t);
  const result = await run(ctx, {
    args: ['--publish', '--provenance', await provenancePair(ctx)],
    env: { DEPLOY_HOST: 'box-1.example', DEPLOY_USER: 'deploy_2', ...APPROVAL }
  });
  assert.equal(result.code, 0, result.stderr);
  const calls = (await readFile(ctx.remoteLog, 'utf8')).trim().split('\n');
  assert.equal(calls.length, 2);
  assert.match(calls[0], /^ssh .* -- deploy_2@box-1\.example mkdir -p /);
  assert.match(calls[1], /^rsync .* -- \S+\/build-demo\/ deploy_2@box-1\.example:/);
});

test('--publish refuses a build that would bundle the sample provenance', async (t) => {
  const ctx = await makeCheckout(t);
  const result = await run(ctx, { args: ['--publish'], env: { ...CREDS, ...APPROVAL, KEW_STUB_VITE: '1' } });
  assert.equal(result.code, 1);
  assert.match(result.stderr, /refusing to publish: .*provenance\.sample\.json/);
  assert.match(result.stderr, /--provenance <dir>/);
  assert.equal(await exists(ctx.npmLog), false, 'a sample build was built for publishing');
  assert.equal(await exists(ctx.remoteLog), false, 'a sample build reached ssh or rsync');
  assert.equal(await exists(staged(ctx)), false);
});

test('a dry run without --provenance still builds the samples', async (t) => {
  const ctx = await makeCheckout(t);
  const result = await run(ctx, { env: { KEW_STUB_VITE: '1' } });
  assert.equal(result.code, 0, result.stderr);
  const samples = join(ctx.checkout, 'frontend', 'demo', 'provenance', 'provenance.sample.json');
  assert.equal(await readFile(join(staged(ctx), 'demo-provenance.json'), 'utf8'), await readFile(samples, 'utf8'));
  assert.match(result.stdout, /committed sample; --publish needs --provenance <dir>/);
});

test('--provenance copies the real pair into the export and ships it', async (t) => {
  const ctx = await makeCheckout(t);
  const pair = await provenancePair(ctx);
  const seen = join(ctx.temporary, 'seen');
  const result = await run(ctx, {
    args: ['--provenance', pair, '--publish'],
    env: { ...CREDS, ...APPROVAL, KEW_STUB_VITE: '1', KEW_STUB_SEEN: seen }
  });
  assert.equal(result.code, 0, result.stderr);
  for (const name of ['provenance.json', 'runs.json']) {
    assert.equal(await readFile(join(seen, name), 'utf8'), await readFile(join(pair, name), 'utf8'), `the build read another ${name}`);
  }
  const shipped = await readFile(join(staged(ctx), 'demo-provenance.json'), 'utf8');
  assert.equal(shipped, await readFile(join(pair, 'provenance.json'), 'utf8'));
  assert.equal(JSON.parse(shipped).as_of, REAL_AS_OF);
  // The scans ran over the copied files, and the box got the staged tree.
  assert.match(result.stdout, /leak scan: demo is clean/);
  assert.match(result.stdout, /leak scan: build-demo is clean/);
  const calls = (await readFile(ctx.remoteLog, 'utf8')).trim().split('\n');
  assert.equal(calls.length, 2);
  assert.match(calls[1], /^rsync .* -- \S+\/build-demo\/ deploy@box\.example:/);
  assert.deepEqual(await readdir(ctx.tmp), [], 'the clean export was left behind');
});

test('a --provenance directory without both files, or holding a symlink, refuses before building', async (t) => {
  const ctx = await makeCheckout(t);
  const cases = {
    'no runs.json': async () => provenancePair(ctx, { omit: ['runs'] }),
    'no provenance.json': async () => provenancePair(ctx, { omit: ['provenance'] }),
    'a symlinked provenance.json': async () => {
      const dir = await provenancePair(ctx);
      await rm(join(dir, 'provenance.json'));
      await symlink(join(ctx.checkout, 'frontend', 'demo', 'provenance', 'provenance.sample.json'), join(dir, 'provenance.json'));
      return dir;
    },
    'a symlinked runs.json': async () => {
      const dir = await provenancePair(ctx);
      await rm(join(dir, 'runs.json'));
      await symlink(join(ctx.checkout, 'frontend', 'demo', 'provenance', 'runs.sample.json'), join(dir, 'runs.json'));
      return dir;
    },
    'no directory': async () => join(ctx.temporary, 'absent')
  };
  for (const [what, make] of Object.entries(cases)) {
    await rm(join(ctx.temporary, 'provenance'), { recursive: true, force: true });
    const dir = await make();
    const result = await run(ctx, { args: ['--publish', '--provenance', dir], env: { ...CREDS, ...APPROVAL } });
    assert.equal(result.code, 1, what);
    assert.match(result.stderr, /deploy-demo: refusing: --provenance /, what);
  }
  assert.equal(await exists(ctx.npmLog), false, 'a refused pair was built');
  assert.equal(await exists(ctx.remoteLog), false, 'a refused pair reached ssh or rsync');
});

test('a copied provenance file holding a home path refuses through the source-mode scan', async (t) => {
  const ctx = await makeCheckout(t);
  const pair = await provenancePair(ctx, {
    edit: (doc) => ({ ...doc, features: [{ ...doc.features[0], title: `/${'Users'}/alice/secret.txt` }, ...doc.features.slice(1)] })
  });
  const result = await run(ctx, { args: ['--publish', '--provenance', pair], env: { ...CREDS, ...APPROVAL } });
  assert.equal(result.code, 1);
  assert.match(result.stdout, /provenance\/provenance\.json:\d+: a home path — \/Users\/alice\/secret\.txt/);
  assert.match(result.stderr, /the leak scan found something in frontend\/demo\//);
  assert.equal(await exists(ctx.npmLog), false, 'a pair that failed the source scan was built');
  assert.equal(await exists(ctx.remoteLog), false);
  assert.deepEqual(await readdir(ctx.tmp), [], 'the clean export was left behind');
});

test('a DEPLOY_PORT that is not a port number refuses before ssh or rsync', async (t) => {
  const ctx = await makeCheckout(t);
  const pair = await provenancePair(ctx);
  for (const port of ['22 -oProxyCommand=x', '-p', '0', '65536', '022', '2a']) {
    const result = await run(ctx, { args: ['--publish', '--provenance', pair], env: { ...CREDS, ...APPROVAL, DEPLOY_PORT: port } });
    assert.equal(result.code, 1, port);
    assert.match(result.stderr, /deploy-demo: refusing: DEPLOY_PORT .* 1-65535/, port);
  }
  const dry = await run(ctx, { env: { DEPLOY_PORT: '22 -oProxyCommand=x' } });
  assert.equal(dry.code, 1, 'a dry run printed a plan with a bad port');
  assert.equal(await exists(ctx.npmLog), false, 'a refused run was built');
  assert.equal(await exists(ctx.remoteLog), false, 'a refused run invoked ssh or rsync');
});

test('a provenance file swapped for a symbolic link after the check refuses at the copy', async (t) => {
  const ctx = await makeCheckout(t);
  const pair = await provenancePair(ctx);
  const { stdout: realGit } = await execFileAsync('sh', ['-c', 'command -v git']);
  // A git that swaps runs.json for a symlink while the export is read: after
  // the up-front check, before the copy.
  await writeFile(
    join(ctx.bin, 'git'),
    `#!/bin/sh
case " $* " in
  *" archive "*) rm -f "$KEW_STUB_SWAP" && ln -s "$KEW_STUB_SWAP_TARGET" "$KEW_STUB_SWAP" ;;
esac
exec ${realGit.trim()} "$@"
`,
    { mode: 0o755 }
  );
  const target = join(ctx.checkout, 'frontend', 'demo', 'provenance', 'runs.sample.json');
  const result = await run(ctx, {
    args: ['--provenance', pair],
    env: { KEW_STUB_SWAP: join(pair, 'runs.json'), KEW_STUB_SWAP_TARGET: target }
  });
  assert.equal(result.code, 1, result.stdout);
  assert.match(result.stderr, /deploy-demo: refusing: .*runs\.json.* symbolic link/);
  assert.equal(await exists(ctx.npmLog), false, 'a swapped pair was built');
  assert.deepEqual(await readdir(ctx.tmp), [], 'the clean export was left behind');
});

test('a refused bundle removes the staged build from the checkout', async (t) => {
  const ctx = await makeCheckout(t);
  const cases = {
    'a home path': { build: { ...CLEAN_BUILD, '_app/immutable/chunk.js': `const root = "/${'Users'}/someone/src";\n` } },
    'a symbolic link': { env: { KEW_STUB_LINKS: JSON.stringify({ 'leak.txt': '/etc/hosts' }) } },
    'a source map': { build: { ...CLEAN_BUILD, 'entry.js.map': '{"version":3}\n' } }
  };
  for (const [what, options] of Object.entries(cases)) {
    const result = await run(ctx, options);
    assert.equal(result.code, 1, what);
    assert.match(result.stderr, /deploy-demo: refusing/, what);
    assert.equal(await exists(staged(ctx)), false, `${what}: the refused build was left in frontend/build-demo/`);
  }
  assert.equal(await exists('/etc/hosts'), true);
});

test('a home path under node_modules/ in the build refuses through the bundle scan', async (t) => {
  const ctx = await makeCheckout(t);
  const build = { ...CLEAN_BUILD, 'node_modules/x/index.js': `const root = "/${'Users'}/someone/src";\n` };
  const result = await run(ctx, { build });
  assert.equal(result.code, 1);
  assert.match(result.stdout, /node_modules\/x\/index\.js:1: a home path/);
  assert.match(result.stderr, /the leak scan found something in frontend\/build-demo\//);
  assert.equal(await exists(ctx.remoteLog), false);
});
