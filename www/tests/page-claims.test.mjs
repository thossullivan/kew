import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

import {
  linearStatusFromDocs,
  packPlacements,
  pageClaimOffenders,
  repoPlacements
} from '../scripts/page-claims.mjs';
import { PACK, RUN_KINDS, SHOWN, packFigures } from '../src/lib/data/pack-figures.js';
import { REPO_SHOWN, repoFigures } from '../src/lib/data/repo-figures.js';

const repo = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const linearStatus = 'Experimental';

const mark = (key, text = packFigures().get(key)) => `<b data-pack="${key}">${text}</b>`;
const repoMark = (key, text = repoFigures().get(key)) => `<b data-repo="${key}">${text}</b>`;

/**
 * A front page quoting every pack and snapshot figure where the claims check
 * expects it, each section holding its marks. `sections` replaces a
 * section's body.
 */
function page({
  receipts = '<p>This evidence pack covers 1 to 26 Jul 2026.</p>',
  limits = '<li>Browse the repository&rsquo;s merged pull requests on GitHub.</li>',
  badge = '<span class="badge" data-tracker-badge="linear">Experimental</span>',
  sections = {}
} = {}) {
  const bodies = new Map();
  for (const [key, where] of packPlacements()) {
    for (const section of where) bodies.set(section, [...(bodies.get(section) ?? []), mark(key)]);
  }
  for (const [key, where] of repoPlacements()) {
    for (const section of where) bodies.set(section, [...(bodies.get(section) ?? []), repoMark(key)]);
  }
  const body = (id) => sections[id] ?? (bodies.get(id) ?? []).join('\n');
  return `<!doctype html><html><head><title>kew</title></head><body>
    <section id="evidence">${receipts}${body('evidence')}<ul>${limits}</ul></section>
    <section id="install"><p>Works with GitHub Issues, and with Linear ${badge}.</p>${body('install')}</section>
  </body></html>`;
}

const offenders = (html) => pageClaimOffenders(html, { linearStatus });
const rules = (html) => offenders(html).map((offender) => offender.rule);

test('the claims check passes a clean page', () => {
  assert.deepEqual(offenders(page()), []);
});

test('the claims check refuses a page that says the repository is private', () => {
  const html = page({ limits: '<li>Open the pull requests. The repository is private\n until launch.</li>' });

  assert.deepEqual(rules(html), ['private-repo']);
});

test('the claims check refuses private-repository wording in any case and in attributes', () => {
  for (const html of [
    page({ limits: '<li>The repository is private.</li>' }),
    page({ limits: '<li>Private until release.</li>' }),
    page({ limits: '<li>The REPO stays PRIVATE for now.</li>' }),
    page({ limits: '<li><a href="#" title="Open the pull requests, private until launch">PRs</a></li>' }),
    page({ limits: '<li><a href="#" aria-label="the repository is private">PRs</a></li>' }),
    page({ limits: '<li><img src="x.png" alt="a private repo"></li>' })
  ]) {
    assert.deepEqual(rules(html), ['private-repo'], html);
  }
});

test('the claims check refuses a page that calls the evidence pack a launch pack', () => {
  for (const receipts of [
    '<p>This real launch pack covers the 25 days.</p>',
    '<p>Verbatim from the <em>launch evidence pack</em>.</p>',
    '<p>The launch-day evidence pack covers 25 days.</p>',
    '<p>The Launch Day Pack covers 25 days.</p>',
    '<p><abbr title="the launch evidence pack">pack</abbr> covers 25 days.</p>'
  ]) {
    assert.deepEqual(rules(page({ receipts })), ['launch-pack'], receipts);
  }
});

test('the claims check refuses a pack figure that differs between two places on the page', () => {
  const install = `<p><span data-pack="epics">Ten</span> epics, <span data-pack="prsMerged">sixty</span> merged PRs.</p>`;

  assert.deepEqual(
    offenders(page({ sections: { install } })).map((offender) => offender.detail),
    [
      `prsMerged renders as "sixty", the pack says "${SHOWN.prsMerged}"`,
      `epics renders as "Ten", the pack says "${SHOWN.epics}"`
    ]
  );
  assert.deepEqual(rules(page({ sections: { install } })), ['pack-figure', 'pack-figure']);
});

test('the claims check refuses a section that quotes its figures without the pack', () => {
  const html = page().replace(mark('epics'), '<p>Ten epics.</p>');
  const found = offenders(html);

  assert.deepEqual(
    found.map((offender) => offender.rule),
    ['pack-figure']
  );
  assert.match(found[0].detail, /^epics is marked 0 times, the page quotes it in 1 place/);
});

test('the claims check refuses a pack figure changed everywhere it is quoted', () => {
  const html = page().replaceAll(`data-pack="prsMerged">${SHOWN.prsMerged}<`, 'data-pack="prsMerged">999<');

  assert.deepEqual(
    offenders(html).map((offender) => offender.detail),
    [`prsMerged renders as "999", the pack says "${SHOWN.prsMerged}"`]
  );
});

test('the claims check refuses a pack figure that has lost its mark', () => {
  const html = page().replace(mark('totalSpend'), '<b>$1413</b>');

  assert.equal(html.includes('<b>$1413</b>'), true);
  assert.deepEqual(rules(html), ['pack-figure']);
  assert.match(offenders(html)[0].detail, /^totalSpend is marked 1 time, the page quotes it in 2 places/);
});

test('the claims check refuses a snapshot figure that is not the snapshot\'s own, or has lost its mark', () => {
  const wrong = page().replace(repoMark('agentShare'), repoMark('agentShare', '70%'));
  assert.deepEqual(
    offenders(wrong).map((offender) => offender.detail),
    [`agentShare renders as "70%", the snapshot says "${REPO_SHOWN.agentShare}"`]
  );

  const unmarked = page().replace(repoMark('agentPrs'), '<b>1,106</b>');
  assert.deepEqual(rules(unmarked), ['repo-figure']);
  assert.match(offenders(unmarked)[0].detail, /^agentPrs is marked 0 times, the page quotes it in 1 place/);
});

test('the snapshot figures are worked out from its counts, never typed', () => {
  assert.equal(REPO_SHOWN.agentPrs, '1,106');
  assert.equal(REPO_SHOWN.mergedPrs, '1,591');
  assert.equal(REPO_SHOWN.agentShare, `${((1106 / 1591) * 100).toFixed(1)}%`);
  assert.equal(REPO_SHOWN.captured, '2 Nov 2026');
});

test('the run kinds\' spend sums to the pack\'s total, so the spend bar is the whole of it', () => {
  const sum = RUN_KINDS.reduce((total, r) => total + r.cost, 0);

  assert.ok(Math.abs(sum - PACK.totalSpend) < 0.005, `the run kinds sum to ${sum}`);
  assert.equal(SHOWN.totalSpend, '$1,412.67');
  assert.equal(SHOWN.runKinds.dispatch.share, '78.1%');
});

test('the claims check refuses a mark the pack does not hold', () => {
  const html = page({ receipts: '<p><b data-pack="prsMergd">211</b></p>' });

  assert.deepEqual(
    offenders(html).map((offender) => offender.detail),
    ['prsMergd is marked as a pack figure, and the pack holds no such figure']
  );
});

test('every figure the page must quote is one the pack holds', () => {
  const figures = packFigures();
  const missing = [...packPlacements().keys()].filter((key) => !figures.has(key));

  assert.deepEqual(missing, []);
  assert.equal(figures.get('prsMerged'), SHOWN.prsMerged);
  assert.equal(figures.get('scorecard.claude-opus-4-8.perPr'), SHOWN.scorecard['claude-opus-4-8'].perPr);
});

test('the claims check refuses a Linear badge that does not use the docs\' word', () => {
  const partial = '<span class="badge" data-tracker-badge="linear">partial</span>';

  assert.deepEqual(rules(page({ badge: partial })), ['linear-status']);
  assert.deepEqual(rules(page({ badge: '' })), ['linear-status']);
});

test('the docs\' word for Linear is the warning title on its page', async () => {
  const markdown = await readFile(resolve(repo, 'docs/linear-webhooks.md'), 'utf8');

  assert.equal(linearStatusFromDocs(markdown), 'Experimental');
  assert.equal(linearStatusFromDocs('# Title\n\n!!! warning "Beta"\n    body\n'), 'Beta');
});
