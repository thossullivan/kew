import assert from 'node:assert/strict';
import test from 'node:test';

import { dashOffenders } from '../scripts/no-dashes.mjs';
import { publicPackFindings } from '../scripts/public-pack.mjs';

test('the public pack scanner accepts safe inline evidence data', () => {
  const pack =
    '<html><a href="https://github.com/thossullivan/kew">source</a>' +
    '<script type="application/json">{"safe":true}</script></html>';

  assert.deepEqual(publicPackFindings(pack), []);
});

const credentialFormats = [
  ['OpenAI project key', ['sk-proj-', 'a'.repeat(48)].join('')],
  ['Anthropic key', ['sk-ant-api03-', 'b'.repeat(48)].join('')],
  ['GitHub personal token', ['ghp_', 'c'.repeat(36)].join('')],
  ['GitHub OAuth token', ['gho_', 'd'.repeat(36)].join('')],
  ['GitHub server token', ['ghs_', 'e'.repeat(36)].join('')],
  ['GitHub fine-grained token', ['github_pat_', 'f'.repeat(64)].join('')],
  ['npm token', ['npm_', 'g'.repeat(36)].join('')],
  ['PyPI token', ['pypi-', 'h'.repeat(48)].join('')],
  ['AWS access key', ['AKIA', 'J'.repeat(16)].join('')],
  ['Slack token', ['xoxb-', '1'.repeat(12), '-', 'i'.repeat(24)].join('')],
  ['private key', ['-----BEGIN ', 'PRIVATE KEY-----'].join('')]
];

const credentialEncodings = [
  ['plain', (value) => value],
  [
    'URL encoded',
    (value) =>
      Array.from(value, (character) =>
        `%${character.codePointAt(0).toString(16).padStart(2, '0')}`
      ).join('')
  ],
  ['base64 encoded', (value) => Buffer.from(value).toString('base64')]
];

for (const [format, credential] of credentialFormats) {
  for (const [encoding, encode] of credentialEncodings) {
    test(`the public pack scanner rejects a ${encoding} ${format}`, () => {
      const findings = publicPackFindings(`<html>${encode(credential)}</html>`);

      assert.ok(findings.some((finding) => finding.rule === 'credential'));
    });
  }
}

for (const [name, content, rule] of [
  ['macOS home path', '/Users/alice/project/file', 'absolute local path'],
  ['Linux home path', '/home/alice/project/file', 'absolute local path'],
  ['credential', 'token=' + 'ghp_' + 'x'.repeat(36), 'credential'],
  ['private remote', ['git', '@github.com:owner/private.git'].join(''), 'private remote'],
  ['external script', '<script src="https://cdn.example.test/app.js"></script>', 'external script']
]) {
  test(`the public pack scanner rejects a ${name}`, () => {
    const findings = publicPackFindings(`<html>${content}</html>`);

    assert.ok(findings.some((finding) => finding.rule === rule));
  });
}

test('the dash gate checks long HTML lines', () => {
  const text = `<p>${'x'.repeat(4001)} — violation</p>`;
  const offenders = dashOffenders([{ path: 'index.html', text }]);

  assert.equal(offenders.length, 1);
});

test('the dash gate still checks ordinary pages', () => {
  const offenders = dashOffenders([{ path: 'index.html', text: '<p>copy — violation</p>' }]);

  assert.equal(offenders.length, 1);
});
