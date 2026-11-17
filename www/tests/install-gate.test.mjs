import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { readFile } from 'node:fs/promises';
import { dirname, join, resolve } from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { promisify } from 'node:util';

import { CREATE_KEW, INSTALL } from '../src/lib/data/install.js';
import {
  npmTarget,
  pypiTarget,
  verifyNpmLauncher,
  verifyPyPIInstall,
  verifySourceInstall
} from '../scripts/install-gate.mjs';

const execFileAsync = promisify(execFile);
const launcher = resolve(dirname(fileURLToPath(import.meta.url)), '../../create-kew');

test('the npm command the site shows names the launcher package in the repository', async () => {
  const manifest = JSON.parse(await readFile(join(launcher, 'package.json'), 'utf8'));
  assert.equal(npmTarget(CREATE_KEW)?.package, manifest.name);
});

test('the launcher installs the project and extras the PyPI command names', async () => {
  // The launcher prints the install it runs; it runs no uv for --help.
  const { stdout } = await execFileAsync(process.execPath, [join(launcher, 'index.js'), '--help']);
  const spec = stdout.match(/uv tool install "([^"]+)"/)?.[1];
  assert.ok(spec, stdout);
  const parsed = spec.match(/^([A-Za-z0-9._-]+)(?:\[([A-Za-z0-9._,-]+)\])?/);
  const installs = { project: parsed[1], extras: parsed[2] ? parsed[2].split(',') : [] };
  assert.deepEqual(installs, pypiTarget(INSTALL));
  assert.deepEqual(installs, { project: 'kew-cli', extras: ['serve'] });
});

const SOURCE_INSTALL =
  'uv tool install "kew-cli[serve] @ git+https://github.com/thossullivan/kew.git@main#subdirectory=kew"';
const README = `Install with:\n\n${SOURCE_INSTALL}\n`;

test('a public repository with an existing ref and subdirectory passes', async () => {
  const calls = [];
  const runGh = async (args) => {
    calls.push(args);
    if (args[0] === 'repo') return { stdout: '{"visibility":"PUBLIC"}' };
    return { stdout: '[{"name":"pyproject.toml","type":"file"}]' };
  };

  const result = await verifySourceInstall(SOURCE_INSTALL, README, runGh);

  assert.equal(result.ok, true);
  assert.deepEqual(calls, [
    ['repo', 'view', 'thossullivan/kew', '--json', 'visibility'],
    ['api', 'repos/thossullivan/kew/contents/kew?ref=main']
  ]);
});

test('an existing file cannot satisfy the source subdirectory check', async () => {
  const runGh = async (args) => {
    if (args[0] === 'repo') return { stdout: '{"visibility":"PUBLIC"}' };
    return { stdout: '{"name":"README.md","type":"file"}' };
  };

  const command = SOURCE_INSTALL.replace('subdirectory=kew', 'subdirectory=README.md');
  const result = await verifySourceInstall(command, command, runGh);

  assert.equal(result.ok, false);
  assert.match(result.reason, /not a directory/i);
});

test('a source directory without pyproject.toml fails the install gate', async () => {
  const runGh = async (args) => {
    if (args[0] === 'repo') return { stdout: '{"visibility":"PUBLIC"}' };
    return { stdout: '[{"name":"README.md","type":"file"}]' };
  };

  const result = await verifySourceInstall(SOURCE_INSTALL, README, runGh);

  assert.equal(result.ok, false);
  assert.match(result.reason, /pyproject\.toml/i);
});

test('a private repository fails the install gate', async () => {
  const runGh = async () => ({ stdout: '{"visibility":"PRIVATE"}' });

  const result = await verifySourceInstall(SOURCE_INSTALL, README, runGh);

  assert.equal(result.ok, false);
  assert.match(result.reason, /not public/i);
});

test('a missing subdirectory fails the install gate', async () => {
  const runGh = async (args) => {
    if (args[0] === 'repo') return { stdout: '{"visibility":"PUBLIC"}' };
    throw new Error('HTTP 404: Not Found');
  };

  const result = await verifySourceInstall(SOURCE_INSTALL, README, runGh);

  assert.equal(result.ok, false);
  assert.match(result.reason, /ref or subdirectory/i);
});

test('a missing ref fails the install gate', async () => {
  const command = SOURCE_INSTALL.replace('@main', '@missing-ref');
  const runGh = async (args) => {
    if (args[0] === 'repo') return { stdout: '{"visibility":"PUBLIC"}' };
    throw new Error('HTTP 404: Not Found');
  };

  const result = await verifySourceInstall(command, command, runGh);

  assert.equal(result.ok, false);
  assert.match(result.reason, /ref or subdirectory/i);
});

test('a GitHub network error fails the install gate', async () => {
  const runGh = async () => {
    throw new Error('network unreachable');
  };

  const result = await verifySourceInstall(SOURCE_INSTALL, README, runGh);

  assert.equal(result.ok, false);
  assert.match(result.reason, /could not verify/i);
});

// The release-day commands: the published package with the extra that carries
// the web console. The gate must accept them before the site shows them.
const PUBLISHED = { status: 200, json: { info: { provides_extra: ['serve'] } } };

test('each release-day PyPI form parses to the project and its extra', () => {
  for (const command of [
    'uv tool install "kew-cli[serve]"',
    'pipx install "kew-cli[serve]"'
  ]) {
    assert.deepEqual(pypiTarget(command), { project: 'kew-cli', extras: ['serve'] }, command);
  }
  assert.deepEqual(pypiTarget('pip install kew-cli'), { project: 'kew-cli', extras: [] });
  assert.deepEqual(pypiTarget('uv tool install kew-cli'), { project: 'kew-cli', extras: [] });
});

test('a source command or a malformed install is not a PyPI form', () => {
  for (const command of [
    SOURCE_INSTALL,
    'uv tool install kew-cli[serve]',
    'uv tool install "kew-cli[serve]" --force',
    'pipx install "kew-cli[serve]" && curl example.invalid',
    'npm install kew-cli'
  ]) {
    assert.equal(pypiTarget(command), null, command);
  }
});

test('a published project that declares the extra passes the install gate', async () => {
  const asked = [];
  for (const command of ['uv tool install "kew-cli[serve]"', 'pipx install "kew-cli[serve]"']) {
    const result = await verifyPyPIInstall(command, async (project) => {
      asked.push(project);
      return PUBLISHED;
    });
    assert.equal(result.ok, true, command);
  }
  assert.deepEqual(asked, ['kew-cli', 'kew-cli']);
});

test('a project PyPI does not hold fails the install gate', async () => {
  const result = await verifyPyPIInstall('uv tool install "kew-cli[serve]"', async () => ({
    status: 404,
    json: null
  }));

  assert.equal(result.ok, false);
  assert.match(result.reason, /kew-cli is not on PyPI/);
});

test('a published project without the extra fails the install gate', async () => {
  const result = await verifyPyPIInstall('pipx install "kew-cli[serve]"', async () => ({
    status: 200,
    json: { info: { provides_extra: [] } }
  }));

  assert.equal(result.ok, false);
  assert.match(result.reason, /does not declare the extra serve/);
});

test('a PyPI network error fails the install gate', async () => {
  const result = await verifyPyPIInstall('uv tool install "kew-cli[serve]"', async () => {
    throw new Error('network unreachable');
  });

  assert.equal(result.ok, false);
  assert.match(result.reason, /could not verify kew-cli on PyPI/);
});

test('a source URL without an explicit ref fails the install gate', async () => {
  const command = SOURCE_INSTALL.replace('.git@main', '.git');
  const result = await verifySourceInstall(command, command, async () => {
    throw new Error('GitHub must not be called');
  });

  assert.equal(result.ok, false);
  assert.match(result.reason, /unsupported source command/i);
});

// The npm launcher: create-kew X.Y.Z installs kew-cli >=X,<X+1, so the gate
// holds its latest major to kew-cli's latest major on PyPI.
const NPM_LATEST = (latest) => ({ status: 200, json: { 'dist-tags': { latest } } });
const PYPI_LATEST = (version) => ({ status: 200, json: { info: { version } } });

test('the npm launcher form parses to its package', () => {
  assert.deepEqual(npmTarget('npx create-kew'), { package: 'create-kew' });
  for (const command of [
    'npx create-kew --yes',
    'npx create-kew && curl example.invalid',
    'npm init kew',
    'npx ../create-kew',
    'npx'
  ]) {
    assert.equal(npmTarget(command), null, command);
  }
});

test('a launcher whose latest major matches the PyPI release passes the install gate', async () => {
  const asked = [];
  const result = await verifyNpmLauncher(
    'npx create-kew',
    'kew-cli',
    async (name) => {
      asked.push(`npm:${name}`);
      return NPM_LATEST('1.2.0');
    },
    async (project) => {
      asked.push(`pypi:${project}`);
      return PYPI_LATEST('1.0.3');
    }
  );

  assert.equal(result.ok, true, result.reason);
  assert.deepEqual(asked, ['npm:create-kew', 'pypi:kew-cli']);
});

test('a launcher on another major than the PyPI release fails the install gate', async () => {
  for (const [latest, released] of [
    ['0.0.1', '1.0.0'],
    ['2.0.0', '1.4.0']
  ]) {
    const result = await verifyNpmLauncher(
      'npx create-kew',
      'kew-cli',
      async () => NPM_LATEST(latest),
      async () => PYPI_LATEST(released)
    );

    assert.equal(result.ok, false, `${latest} against ${released}`);
    assert.ok(result.reason.includes(`create-kew ${latest} on npm`), result.reason);
    assert.ok(result.reason.includes(`PyPI's latest is ${released}`), result.reason);
  }
});

test('a launcher npm does not hold fails the install gate', async () => {
  const result = await verifyNpmLauncher(
    'npx create-kew',
    'kew-cli',
    async () => ({ status: 404, json: null }),
    async () => PYPI_LATEST('1.0.0')
  );

  assert.equal(result.ok, false);
  assert.match(result.reason, /create-kew is not on npm/);
});

test('a launcher without a latest version fails the install gate', async () => {
  const result = await verifyNpmLauncher(
    'npx create-kew',
    'kew-cli',
    async () => ({ status: 200, json: { 'dist-tags': {} } }),
    async () => PYPI_LATEST('1.0.0')
  );

  assert.equal(result.ok, false);
  assert.match(result.reason, /create-kew on npm has no latest version/);
});

test('a launcher checked against a project PyPI does not hold fails the install gate', async () => {
  const result = await verifyNpmLauncher(
    'npx create-kew',
    'kew-cli',
    async () => NPM_LATEST('1.0.0'),
    async () => ({ status: 404, json: null })
  );

  assert.equal(result.ok, false);
  assert.match(result.reason, /kew-cli is not on PyPI/);
});

test('a registry outage fails the gate as unverified, not as unpublished', async () => {
  const outage = async () => ({ status: 503, json: null });

  const install = await verifyPyPIInstall('uv tool install "kew-cli[serve]"', outage);
  assert.equal(install.ok, false);
  assert.equal(install.reason, 'could not verify kew-cli on PyPI (HTTP 503)');

  const npm = await verifyNpmLauncher('npx create-kew', 'kew-cli', outage, async () => PYPI_LATEST('1.0.0'));
  assert.equal(npm.ok, false);
  assert.equal(npm.reason, 'could not verify create-kew on npm (HTTP 503)');

  const pypi = await verifyNpmLauncher('npx create-kew', 'kew-cli', async () => NPM_LATEST('1.0.0'), outage);
  assert.equal(pypi.ok, false);
  assert.equal(pypi.reason, 'could not verify kew-cli on PyPI (HTTP 503)');
});

test('an npm or PyPI network error fails the launcher check', async () => {
  const down = async () => {
    throw new Error('network unreachable');
  };

  const npm = await verifyNpmLauncher('npx create-kew', 'kew-cli', down, async () => PYPI_LATEST('1.0.0'));
  assert.equal(npm.ok, false);
  assert.match(npm.reason, /could not verify create-kew on npm/);

  const pypi = await verifyNpmLauncher('npx create-kew', 'kew-cli', async () => NPM_LATEST('1.0.0'), down);
  assert.equal(pypi.ok, false);
  assert.match(pypi.reason, /could not verify kew-cli on PyPI/);
});
