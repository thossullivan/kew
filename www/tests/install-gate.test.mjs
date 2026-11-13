import assert from 'node:assert/strict';
import test from 'node:test';

import { pypiTarget, verifyPyPIInstall, verifySourceInstall } from '../scripts/install-gate.mjs';

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
