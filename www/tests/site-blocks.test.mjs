import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

/**
 * The Caddy site blocks are written down twice each: `Caddyfile` is what
 * `npm run serve` runs and what the demo's local checks read the CSP from,
 * and `DEPLOY.md` is what an operator pastes into the host's Caddy. A header
 * in only one of them means the thing tested is not the thing shipped, so the
 * copies are held equal here: the demo block, the kew.dev block, and the
 * Content-Security-Policy in all four.
 */
const www = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const lines = async (name) => (await readFile(resolve(www, name), 'utf8')).split('\n');

/** A block's directives: its lines without indentation, blank lines and comments. */
const directives = (block) => block.map((line) => line.trim()).filter((line) => line !== '' && !line.startsWith('#'));

/**
 * The body of the site block `address { … }` in `text`: the lines between
 * the one that reads exactly `address {` and the first `}` in column 0
 * after it. The address line is not part of it.
 */
function siteBlock(text, address, where) {
  const start = text.indexOf(`${address} {`);
  assert.notEqual(start, -1, `${where} has no "${address} {" line`);
  const end = text.findIndex((line, i) => i > start && line === '}');
  assert.notEqual(end, -1, `${where}: the "${address} {" block is never closed`);
  return directives(text.slice(start + 1, end));
}

/** `block` without its `name { … }` directive, which must be there. */
function without(block, name, where) {
  const start = block.indexOf(`${name} {`);
  assert.notEqual(start, -1, `${where} has no "${name} {" block`);
  const end = block.indexOf('}', start);
  return [...block.slice(0, start), ...block.slice(end + 1)];
}

/** The sentence for the first directive two copies differ at, or null when they are equal. */
function difference(one, other) {
  const length = Math.max(one.block.length, other.block.length);
  for (let i = 0; i < length; i += 1) {
    if (one.block[i] === other.block[i]) continue;
    const read = (line) => (line === undefined ? 'nothing' : `"${line}"`);
    return (
      `${one.where} and ${other.where} differ at directive ${i + 1}: ` +
      `the first reads ${read(one.block[i])}, the second ${read(other.block[i])}`
    );
  }
  return null;
}

/** The one Content-Security-Policy a block sends. */
function csp({ block, where }) {
  const sent = block.filter((line) => /^Content-Security-Policy\s/.test(line));
  assert.equal(sent.length, 1, `${where} sends ${sent.length} Content-Security-Policy headers, not one`);
  const found = /^Content-Security-Policy\s+"([^"]+)"$/.exec(sent[0]);
  assert.ok(found, `${where} sends a Content-Security-Policy with no quoted policy: ${sent[0]}`);
  return found[1];
}

/** The four copies, each with the place a failure names. */
async function copies() {
  const caddyfile = await lines('Caddyfile');
  const deploy = await lines('DEPLOY.md');
  // The demo block of Caddyfile is a reference: every line is commented out.
  const reference = caddyfile.filter((line) => line.startsWith('#')).map((line) => line.replace(/^# ?/, ''));
  const live = 'the :80 block of www/Caddyfile';
  return {
    live: { where: `${live}, without its log block`, block: without(siteBlock(caddyfile, ':80', live), 'log', live) },
    kew: {
      where: 'the kew.dev block of www/DEPLOY.md ("Add the site to the host\'s Caddy")',
      block: siteBlock(deploy, 'kew.dev', 'www/DEPLOY.md')
    },
    reference: {
      where: 'the commented demo.kew.dev block of www/Caddyfile',
      block: siteBlock(reference, 'demo.kew.dev', 'the commented lines of www/Caddyfile')
    },
    demo: {
      where: 'the demo.kew.dev block of www/DEPLOY.md ("Caddy site block")',
      block: siteBlock(deploy, 'demo.kew.dev', 'www/DEPLOY.md')
    }
  };
}

test("the demo site block in DEPLOY.md is Caddyfile's commented one", async () => {
  const { demo, reference } = await copies();
  assert.ok(demo.block.length > 0, `${demo.where} is empty`);
  const differs = difference(demo, reference);
  assert.ok(differs === null, differs);
});

test("the kew.dev site block in DEPLOY.md is Caddyfile's :80 block, apart from the address and the log block", async () => {
  const { kew, live } = await copies();
  assert.ok(kew.block.length > 0, `${kew.where} is empty`);
  const differs = difference(kew, live);
  assert.ok(differs === null, differs);
});

test('every copy of a site block sends the same Content-Security-Policy', async () => {
  const { live, ...rest } = await copies();
  const policy = csp(live);
  for (const copy of Object.values(rest)) {
    assert.equal(csp(copy), policy, `${copy.where} and ${live.where} send different Content-Security-Policy headers`);
  }
});

test('a directive that differs, is missing or is extra is reported with both places named', () => {
  const one = { where: 'the first place', block: ['root * /srv/x', 'file_server'] };
  assert.equal(difference(one, { where: 'the second place', block: [...one.block] }), null);
  assert.equal(
    difference(one, { where: 'the second place', block: ['root * /srv/x', 'file_server browse'] }),
    'the first place and the second place differ at directive 2: the first reads "file_server", the second "file_server browse"'
  );
  assert.equal(
    difference(one, { where: 'the second place', block: ['root * /srv/x'] }),
    'the first place and the second place differ at directive 2: the first reads "file_server", the second nothing'
  );
  assert.equal(
    difference({ where: 'the second place', block: [] }, one),
    'the second place and the first place differ at directive 1: the first reads nothing, the second "root * /srv/x"'
  );
});

test('comments, blank lines and indentation are not part of a block, and a commented directive is not one', () => {
  const text = ['a.example {', '\troot * /srv/a', '', '\t# header X "1"', '    header {', '        Y "2"', '    }', '}', 'b.example {', '}'];
  assert.deepEqual(siteBlock(text, 'a.example', 'the text'), ['root * /srv/a', 'header {', 'Y "2"', '}']);
  assert.deepEqual(siteBlock(text, 'b.example', 'the text'), []);
  assert.deepEqual(without(['a', 'log {', 'output stdout', '}', 'b'], 'log', 'the text'), ['a', 'b']);
  assert.throws(() => siteBlock(text, 'c.example', 'the text'), /the text has no "c\.example \{" line/);
});
