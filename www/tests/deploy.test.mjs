import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { access, cp, mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import test from 'node:test';
import { promisify } from 'node:util';
import { fileURLToPath } from 'node:url';

const execFileAsync = promisify(execFile);
const www = resolve(dirname(fileURLToPath(import.meta.url)), '..');

/**
 * A temp git checkout on `main` (with `origin/main` at HEAD) holding
 * deploy.sh, the real kew/scripts/tool-pins.env and a passing docs gate,
 * with `npm`, `uvx`, `ssh` and `rsync` stubbed on PATH. The build steps are
 * no-ops; `ssh` and `rsync` record each call in a log, so "never invoked" is
 * a missing file.
 */
async function makeCheckout(t) {
  const temporary = await mkdtemp(join(tmpdir(), 'kew-deploy-test-'));
  t.after(() => rm(temporary, { recursive: true, force: true }));
  const checkout = join(temporary, 'checkout');
  const bin = join(temporary, 'bin');
  const remoteLog = join(temporary, 'remote.log');
  await mkdir(bin);
  await cp(join(www, 'deploy.sh'), join(checkout, 'www', 'deploy.sh'));
  // deploy.sh sources the real tool pins for its docs build.
  await cp(join(www, '..', 'kew', 'scripts', 'tool-pins.env'), join(checkout, 'kew', 'scripts', 'tool-pins.env'));
  await mkdir(join(checkout, 'scripts'));
  await writeFile(join(checkout, 'scripts', 'check-docs.sh'), '#!/bin/sh\nexit 0\n', { mode: 0o755 });
  for (const tool of ['npm', 'uvx']) await writeFile(join(bin, tool), '#!/bin/sh\nexit 0\n', { mode: 0o755 });
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
  return { checkout, bin, remoteLog, git };
}

async function run(ctx, env) {
  const environment = { PATH: `${ctx.bin}:${process.env.PATH}`, HOME: process.env.HOME, KEW_STUB_REMOTE_LOG: ctx.remoteLog, ...env };
  try {
    const { stdout, stderr } = await execFileAsync('bash', [join(ctx.checkout, 'www', 'deploy.sh')], { env: environment });
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

test('a DEPLOY_HOST or DEPLOY_USER that could be read as an option refuses before ssh or rsync', async (t) => {
  const ctx = await makeCheckout(t);
  for (const creds of [
    { DEPLOY_HOST: '-oProxyCommand=x', DEPLOY_USER: 'deploy' },
    { DEPLOY_HOST: 'box.example', DEPLOY_USER: '-l' },
    { DEPLOY_HOST: 'box example', DEPLOY_USER: 'deploy' },
    { DEPLOY_HOST: 'box.example', DEPLOY_USER: "de'ploy" }
  ]) {
    const result = await run(ctx, creds);
    assert.equal(result.code, 1, JSON.stringify(creds));
    assert.match(result.stderr, /deploy: refusing: DEPLOY_(HOST|USER) .*not starting with '-'/, JSON.stringify(creds));
    assert.doesNotMatch(result.stdout, /==> Building/, JSON.stringify(creds));
  }
  assert.equal(await exists(ctx.remoteLog), false, 'a refused run invoked ssh or rsync');
});

test('an empty DEPLOY_HOST or DEPLOY_USER refuses before ssh or rsync', async (t) => {
  const ctx = await makeCheckout(t);
  for (const creds of [
    { DEPLOY_HOST: '', DEPLOY_USER: 'deploy' },
    { DEPLOY_HOST: 'box.example', DEPLOY_USER: '' }
  ]) {
    const result = await run(ctx, creds);
    assert.notEqual(result.code, 0, JSON.stringify(creds));
  }
  assert.equal(await exists(ctx.remoteLog), false, 'a refused run invoked ssh or rsync');
});

test('a valid host and user publish, with -- before the destination of ssh and rsync', async (t) => {
  const ctx = await makeCheckout(t);
  const result = await run(ctx, { DEPLOY_HOST: 'box-1.example', DEPLOY_USER: 'deploy_2' });
  assert.equal(result.code, 0, result.stderr);
  const calls = (await readFile(ctx.remoteLog, 'utf8')).trim().split('\n');
  assert.equal(calls.length, 2);
  assert.match(calls[0], /^ssh .* -- deploy_2@box-1\.example mkdir -p '\/home\/deploy_2\/sites\/kew'$/);
  assert.match(calls[1], /^rsync -avz --delete .* -- build\/ deploy_2@box-1\.example:\/home\/deploy_2\/sites\/kew\/$/);
});

const CREDS = { DEPLOY_HOST: 'box.example', DEPLOY_USER: 'deploy' };

test('a KEW_REMOTE_DIR that is not an absolute kew directory of plain characters refuses before ssh or rsync', async (t) => {
  const ctx = await makeCheckout(t);
  for (const dir of [
    "x'; cmd; '",
    '/home/deploy',
    '/home/deploy/sites/kew-demo',
    '/home/deploy/sites/kew/',
    'sites/kew',
    '/home/deploy/sites/../kew',
    "/home/deploy/x'; rm -rf ~; '/kew",
    '/home/deploy/sites kew/kew'
  ]) {
    const result = await run(ctx, { ...CREDS, KEW_REMOTE_DIR: dir });
    assert.equal(result.code, 1, dir);
    assert.match(result.stderr, /deploy: refusing: KEW_REMOTE_DIR/, dir);
    assert.doesNotMatch(result.stdout, /==> Building/, dir);
  }
  const quoted = await run(ctx, { DEPLOY_HOST: 'box.example', DEPLOY_USER: "de'ploy" });
  assert.equal(quoted.code, 1, 'a quote in DEPLOY_USER reached the default remote dir');
  assert.equal(await exists(ctx.remoteLog), false, 'a refused run invoked ssh or rsync');
  const kept = await run(ctx, { ...CREDS, KEW_REMOTE_DIR: '/srv/sites_2/kew' });
  assert.equal(kept.code, 0, kept.stderr);
  assert.match(await readFile(ctx.remoteLog, 'utf8'), /deploy@box\.example:\/srv\/sites_2\/kew\/$/m);
});

test('a DEPLOY_PORT that is not a port number refuses before ssh or rsync', async (t) => {
  const ctx = await makeCheckout(t);
  for (const port of ['22 -oProxyCommand=x', '-p', '0', '65536', '022', '2a', ' 22']) {
    const result = await run(ctx, { ...CREDS, DEPLOY_PORT: port });
    assert.equal(result.code, 1, JSON.stringify(port));
    assert.match(result.stderr, /deploy: refusing: DEPLOY_PORT .* 1-65535/, JSON.stringify(port));
    assert.doesNotMatch(result.stdout, /==> Building/, JSON.stringify(port));
  }
  assert.equal(await exists(ctx.remoteLog), false, 'a refused run invoked ssh or rsync');
  const result = await run(ctx, { ...CREDS, DEPLOY_PORT: '65535' });
  assert.equal(result.code, 0, result.stderr);
  assert.match(await readFile(ctx.remoteLog, 'utf8'), /^ssh -p 65535 /m);
});

test('main ahead of origin/main refuses before anything is built', async (t) => {
  const ctx = await makeCheckout(t);
  await writeFile(join(ctx.checkout, 'www', 'local.txt'), 'local only\n');
  await ctx.git('add', '-A');
  await ctx.git('commit', '-q', '-m', 'local only');
  const result = await run(ctx, CREDS);
  assert.equal(result.code, 1);
  assert.match(result.stderr, /deploy: refusing: HEAD .* is not origin\/main/);
  assert.doesNotMatch(result.stdout, /==> /);
  assert.equal(await exists(ctx.remoteLog), false, 'a commit not on origin/main was published');
  await ctx.git('update-ref', '-d', 'refs/remotes/origin/main');
  const missing = await run(ctx, CREDS);
  assert.equal(missing.code, 1);
  assert.match(missing.stderr, /no origin\/main ref/);
  assert.equal(await exists(ctx.remoteLog), false);
});
