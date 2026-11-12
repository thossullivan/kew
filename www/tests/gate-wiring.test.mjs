import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { lstat, mkdir, mkdtemp, readFile, rm, symlink, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import test from 'node:test';
import { promisify } from 'node:util';
import { fileURLToPath } from 'node:url';

const execFileAsync = promisify(execFile);
const www = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const gateRunner = join(www, 'scripts', 'run-gates.mjs');

async function makeGateProject(t, { initial = Buffer.from('entry\n') } = {}) {
  const temporary = await mkdtemp(join(tmpdir(), 'kew-www-gates-test-'));
  t.after(() => rm(temporary, { recursive: true, force: true }));
  const project = join(temporary, 'project');
  const data = join(project, 'src', 'lib', 'data');
  const stats = join(data, 'repo-stats.json');
  const log = join(temporary, 'commands.log');
  const bin = join(temporary, 'bin');
  await mkdir(data, { recursive: true });
  await mkdir(bin);
  if (initial !== null) {
    await writeFile(stats, initial);
  }
  await writeFile(
    join(bin, 'npm'),
    `#!/bin/sh
printf '%s\\n' "$*" >> "$KEW_GATE_LOG"
if [ "$*" = 'run build' ]; then
  case "$KEW_GATE_MODE" in
    replace-symlink)
      rm -f "$KEW_GATE_STATS"
      ln -s "$KEW_GATE_OUTSIDE" "$KEW_GATE_STATS"
      ;;
    restoration-failure)
      printf 'generated\\n' > "$KEW_GATE_STATS"
      rm -f "$KEW_GATE_STATS"
      rmdir "$(dirname "$KEW_GATE_STATS")"
      printf 'blocks restoration\\n' > "$(dirname "$KEW_GATE_STATS")"
      ;;
    signal)
      printf 'generated\\n' > "$KEW_GATE_STATS"
      kill -TERM $$
      ;;
    write)
      printf 'generated\\n' > "$KEW_GATE_STATS"
      ;;
  esac
fi
if [ "$*" = "$KEW_GATE_FAIL_AT" ]; then
  exit "\${KEW_GATE_FAIL_CODE:-1}"
fi
exit 0
`,
    { mode: 0o755 }
  );
  return {
    data,
    log,
    project,
    stats,
    temporary,
    env: {
      ...process.env,
      KEW_GATE_LOG: log,
      KEW_GATE_STATS: stats,
      PATH: `${bin}:${process.env.PATH}`
    }
  };
}

async function runGates(project, overrides = {}) {
  return execFileAsync(process.execPath, [gateRunner], {
    cwd: project.project,
    env: { ...project.env, ...overrides }
  });
}

async function commands(project) {
  try {
    return (await readFile(project.log, 'utf8')).trim().split('\n');
  } catch (error) {
    if (error.code === 'ENOENT') return [];
    throw error;
  }
}

async function rejected(promise) {
  try {
    await promise;
  } catch (error) {
    return error;
  }
  assert.fail('expected the command to fail');
}

test('package gates run through the statistics-guarding wrapper', async () => {
  const pkg = JSON.parse(await readFile(join(www, 'package.json'), 'utf8'));

  assert.equal(pkg.scripts.gates, 'node scripts/run-gates.mjs');
});

/**
 * Whether package script `name` reaches the statistics fetcher: it names
 * fetch-repo-stats itself, or runs (`npm run <x>`, `npm run-script <x>`,
 * `npm <x>` for the lifecycle names) a script that does, at any depth.
 */
function reachesFetcher(scripts, name, seen = new Set()) {
  if (seen.has(name) || !(name in scripts)) return false;
  seen.add(name);
  const body = scripts[name];
  if (/fetch-repo-stats/.test(body)) return true;
  const called = [
    ...[...body.matchAll(/\bnpm\s+(?:run|run-script|rum|urn)\s+(?:--?[\w-]+\s+)*([\w:.-]+)/g)].map((m) => m[1]),
    ...[...body.matchAll(/\bnpm\s+(test|start|stop|restart)\b/g)].map((m) => m[1])
  ];
  // npm also runs pre<name> and post<name> around a script.
  const around = [`pre${name}`, `post${name}`];
  return [...called, ...around].some((next) => reachesFetcher(scripts, next, seen));
}

test('the build reads the committed statistics snapshot and never fetches it', async () => {
  const pkg = JSON.parse(await readFile(join(www, 'package.json'), 'utf8'));

  assert.equal(reachesFetcher(pkg.scripts, 'build'), false);
  assert.equal(pkg.scripts.stats, 'node scripts/fetch-repo-stats.mjs');
});

test('a build that reaches the fetcher through another script is caught', () => {
  const stats = 'node scripts/fetch-repo-stats.mjs';
  for (const scripts of [
    { build: 'node scripts/fetch-repo-stats.mjs --quiet && vite build' },
    { build: 'npm run stats && vite build', stats },
    { build: 'npm run-script stats && vite build', stats },
    { build: 'npm run --silent stats && vite build', stats },
    { build: 'npm run data && vite build', data: 'npm run stats', stats },
    { build: 'vite build', prebuild: 'npm run stats', stats },
    { build: 'npm test && vite build', test: 'npm run stats', stats }
  ]) {
    assert.equal(reachesFetcher(scripts, 'build'), true, JSON.stringify(scripts));
  }
  assert.equal(reachesFetcher({ build: 'npm run tokens && vite build', tokens: 'node x.mjs', stats }, 'build'), false);
});

test('gates run in order and leave the exact entry bytes', async (t) => {
  const entry = Buffer.from([0x00, 0x7b, 0xff, 0x0a]);
  const project = await makeGateProject(t, { initial: entry });

  await runGates(project);

  assert.deepEqual(await commands(project), ['test', 'run tokens', 'run check', 'run build']);
  assert.deepEqual(await readFile(project.stats), entry);
});

test('a failure before generation stops later commands and keeps its status', async (t) => {
  const project = await makeGateProject(t);

  await assert.rejects(
    runGates(project, { KEW_GATE_FAIL_AT: 'run tokens', KEW_GATE_FAIL_CODE: '23' }),
    (error) => error.code === 23
  );

  assert.deepEqual(await commands(project), ['test', 'run tokens']);
  assert.deepEqual(await readFile(project.stats), Buffer.from('entry\n'));
});

test('a build that rewrites the statistics snapshot fails the gates and is undone', async (t) => {
  const project = await makeGateProject(t);

  const error = await rejected(runGates(project, { KEW_GATE_MODE: 'write' }));

  assert.equal(error.code, 1);
  assert.match(error.stderr, /changed .*repo-stats\.json.*must not fetch/);
  assert.deepEqual(await commands(project), ['test', 'run tokens', 'run check', 'run build']);
  assert.deepEqual(await readFile(project.stats), Buffer.from('entry\n'));
});

test('a build that creates an absent statistics snapshot fails the gates and is undone', async (t) => {
  const project = await makeGateProject(t, { initial: null });

  const error = await rejected(runGates(project, { KEW_GATE_MODE: 'write' }));

  assert.equal(error.code, 1);
  assert.match(error.stderr, /changed .*repo-stats\.json/);
  await assert.rejects(lstat(project.stats), (error) => error.code === 'ENOENT');
});

test('a failure after generation removes churn and keeps its status', async (t) => {
  const project = await makeGateProject(t);

  await assert.rejects(
    runGates(project, {
      KEW_GATE_MODE: 'write',
      KEW_GATE_FAIL_AT: 'run build',
      KEW_GATE_FAIL_CODE: '29'
    }),
    (error) => error.code === 29
  );

  assert.deepEqual(await commands(project), ['test', 'run tokens', 'run check', 'run build']);
  assert.deepEqual(await readFile(project.stats), Buffer.from('entry\n'));
});

test('an entry file that was absent remains absent after successful gates', async (t) => {
  const project = await makeGateProject(t, { initial: null });
  const callerOwned = join(project.data, 'caller-owned.json');
  await writeFile(callerOwned, 'keep\n');

  await runGates(project);

  await assert.rejects(lstat(project.stats), (error) => error.code === 'ENOENT');
  assert.equal(await readFile(callerOwned, 'utf8'), 'keep\n');
});

test('an unsafe entry file prevents every gate command', async (t) => {
  const project = await makeGateProject(t, { initial: null });
  const outside = join(project.temporary, 'outside.json');
  await writeFile(outside, 'outside\n');
  await writeFile(project.stats, 'placeholder\n');
  await rm(project.stats);
  await symlink(outside, project.stats);

  await assert.rejects(runGates(project), (error) => {
    assert.match(error.stderr, /unsafe.*repo-stats\.json/i);
    return true;
  });

  assert.deepEqual(await commands(project), []);
  assert.equal(await readFile(outside, 'utf8'), 'outside\n');
});

test('a snapshot setup failure prevents every gate command', async (t) => {
  const project = await makeGateProject(t);
  const invalidTemporaryRoot = join(project.temporary, 'not-a-directory');
  await writeFile(invalidTemporaryRoot, 'file\n');

  await assert.rejects(runGates(project, { TMPDIR: invalidTemporaryRoot }), (error) => {
    assert.match(error.stderr, /snapshot failed/i);
    return true;
  });

  assert.deepEqual(await commands(project), []);
});

test('restoration replaces a generated symlink without changing its target', async (t) => {
  const project = await makeGateProject(t);
  const outside = join(project.temporary, 'outside.json');
  await writeFile(outside, 'outside\n');

  const error = await rejected(
    runGates(project, {
      KEW_GATE_MODE: 'replace-symlink',
      KEW_GATE_OUTSIDE: outside
    })
  );
  assert.match(error.stderr, /changed .*repo-stats\.json/);

  assert.equal(await readFile(outside, 'utf8'), 'outside\n');
  assert.equal((await lstat(project.stats)).isFile(), true);
  assert.deepEqual(await readFile(project.stats), Buffer.from('entry\n'));
});

test('restoration failure alone is nonzero and retains only its snapshot', async (t) => {
  const first = await makeGateProject(t);

  const error = await rejected(
    runGates(first, {
      KEW_GATE_MODE: 'restoration-failure'
    })
  );
  assert.equal(error.code, 1);
  assert.match(error.stderr, /restoration failed/i);
  const match = error.stderr.match(/snapshot retained at (.+)$/im);
  assert.ok(match);
  const retained = match[1].trim();
  assert.deepEqual(await readFile(retained), Buffer.from('entry\n'));
  assert.equal((await lstat(dirname(retained))).mode & 0o077, 0);
  assert.equal((await lstat(retained)).mode & 0o077, 0);

  const second = await makeGateProject(t);
  await runGates(second);

  assert.deepEqual(await readFile(retained), Buffer.from('entry\n'));
  await rm(dirname(retained), { recursive: true });
});

test('restoration reports its failure beside the original command failure', async (t) => {
  const project = await makeGateProject(t);

  const error = await rejected(
    runGates(project, {
      KEW_GATE_MODE: 'restoration-failure',
      KEW_GATE_FAIL_AT: 'run build',
      KEW_GATE_FAIL_CODE: '31'
    })
  );

  assert.match(error.stderr, /npm run build.*31/i);
  assert.match(error.stderr, /restoration failed/i);
  const match = error.stderr.match(/snapshot retained at (.+)$/im);
  assert.ok(match);
  const retained = match[1].trim();
  assert.deepEqual(await readFile(retained), Buffer.from('entry\n'));
  await rm(dirname(retained), { recursive: true });
});

test('a child spawn failure is nonzero and still restores entry bytes', async (t) => {
  const project = await makeGateProject(t);

  await assert.rejects(runGates(project, { PATH: project.temporary }), (error) => {
    assert.match(error.stderr, /npm test.*failed/i);
    return true;
  });

  assert.deepEqual(await readFile(project.stats), Buffer.from('entry\n'));
});

test('a child signal is nonzero and still restores generated churn', async (t) => {
  const project = await makeGateProject(t);

  await assert.rejects(runGates(project, { KEW_GATE_MODE: 'signal' }), (error) => {
    assert.match(error.stderr, /npm run build.*terminated by SIGTERM/i);
    return true;
  });

  assert.deepEqual(await readFile(project.stats), Buffer.from('entry\n'));
});

test('deploy fails before publication when the install gate rejects the repository', async (t) => {
  const temporary = await mkdtemp(join(tmpdir(), 'kew-deploy-gate-'));
  t.after(() => rm(temporary, { recursive: true, force: true }));
  const bin = join(temporary, 'bin');
  await mkdir(bin);
  const gh = join(bin, 'gh');
  await writeFile(gh, '#!/bin/sh\nprintf \'{"visibility":"PRIVATE"}\'\n', { mode: 0o755 });

  // deploy.sh refuses anything but a clean main before the install gate, and
  // CI checks out a PR ref, so the gate runs in a scratch repo on main holding
  // just what it reads.
  const repo = join(temporary, 'repo');
  for (const file of [
    'README.md',
    'www/deploy.sh',
    'www/package.json',
    'www/scripts/check-install.mjs',
    'www/scripts/install-gate.mjs',
    'www/src/lib/data/install.js'
  ]) {
    await mkdir(dirname(join(repo, file)), { recursive: true });
    await writeFile(join(repo, file), await readFile(resolve(www, '..', file)));
  }
  const git = (...args) =>
    execFileAsync('git', ['-c', 'user.name=t', '-c', 'user.email=t@t', ...args], { cwd: repo });
  await git('init', '-q', '-b', 'main');
  await git('add', '.');
  await git('commit', '-q', '-m', 'scratch');
  // deploy.sh refuses a HEAD that is not the local origin/main ref.
  await git('update-ref', 'refs/remotes/origin/main', 'HEAD');

  await assert.rejects(
    execFileAsync('bash', [join(repo, 'www/deploy.sh')], {
      cwd: join(repo, 'www'),
      env: {
        ...process.env,
        DEPLOY_HOST: 'deploy.invalid',
        DEPLOY_USER: 'kew',
        PATH: `${bin}:${process.env.PATH}`
      },
      timeout: 30_000
    }),
    (error) => {
      assert.match(`${error.stdout}\n${error.stderr}`, /install gate:.*not public/i);
      return true;
    }
  );
});
