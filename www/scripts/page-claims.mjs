#!/usr/bin/env node
/**
 * The front page states only true claims and figures.
 *
 * Five rules, checked on the rendered page rather than component source:
 *
 *   private-repo     the page says the repository is private, which is false
 *                    from release day on. Any case, in the text or in a
 *                    title, aria-label, alt or content attribute: "private"
 *                    beside repository, repo or until;
 *   launch-pack      the evidence pack is called a launch (or launch-day)
 *                    pack, though it covers a dated window, not a launch;
 *   pack-figure      an evidence-pack figure is not the pack's own. Every
 *                    element quoting a figure carries data-pack="<key>", and
 *                    its text must be the string pack-figures.js renders for
 *                    that key. Each key must be marked at least as many times
 *                    as packPlacements() says the page quotes it, so a
 *                    quotation retyped by hand without its mark fails too;
 *   repo-figure      a figure of the development repository's snapshot is
 *                    not the snapshot's own: the same rule over
 *                    data-repo="<key>" and repo-figures.js, with each key
 *                    marked at least as often as repoPlacements() says;
 *   linear-status    the Linear badge (data-tracker-badge="linear") is
 *                    missing or does not use the docs' word, the warning
 *                    title in docs/linear-webhooks.md.
 */
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { RUN_KINDS, SCORECARD, packFigures } from '../src/lib/data/pack-figures.js';
import { repoFigures } from '../src/lib/data/repo-figures.js';

const here = dirname(fileURLToPath(import.meta.url));
const PAGE = resolve(here, '../build/index.html');
const LINEAR_DOC = resolve(here, '../../docs/linear-webhooks.md');

const ENTITIES = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: ' ', middot: '·' };

const CLAIMS = [
  [
    'private-repo',
    /\b(?:repository|repo)\b(?:\s+[\w'’]+){0,3}?\s+private\b|\bprivate\s+(?:[\w'’-]+\s+)?(?:repository|repo|until)\b/i
  ],
  ['launch-pack', /\blaunch(?:[-\s]day)?\s+(?:evidence\s+)?pack\b/i]
];

/** Attributes a reader sees or hears as page copy. */
const COPY_ATTRIBUTES = /\s(?:title|aria-label|alt|content)\s*=\s*(?:"([^"]*)"|'([^']*)')/gi;

function decode(text) {
  return text.replace(/&(#x[0-9a-f]+|#\d+|[a-z]+);/gi, (whole, name) => {
    if (name[0] === '#') {
      const hex = name[1] === 'x' || name[1] === 'X';
      return String.fromCodePoint(parseInt(name.slice(hex ? 2 : 1), hex ? 16 : 10));
    }
    return ENTITIES[name.toLowerCase()] ?? whole;
  });
}

/**
 * The visible text of an HTML fragment, whitespace collapsed. `between` is
 * what a tag leaves behind: nothing inside one figure ("<span>$</span>6.70"),
 * a space across the page, where two elements' words must not run together.
 */
function textOf(fragment, between = '') {
  return decode(fragment.replace(/<!--[\s\S]*?-->/g, '').replace(/<[^>]*>/g, between))
    .replace(/\s+/g, ' ')
    .trim();
}

/** Every element carrying `attribute`, as { value, text }. */
function marked(html, attribute) {
  const found = [];
  const open = new RegExp(`<([a-z][a-z0-9-]*)\\b[^>]*\\s${attribute}="([^"]*)"[^>]*>`, 'gi');
  for (const match of html.matchAll(open)) {
    const tag = match[1].toLowerCase();
    const start = match.index + match[0].length;
    const end = html.toLowerCase().indexOf(`</${tag}>`, start);
    found.push({ value: decode(match[2]), text: textOf(html.slice(start, end === -1 ? start : end)) });
  }
  return found;
}

/**
 * Where the front page quotes each pack figure, as key -> the sections that
 * quote it, once per quotation. The check requires each key on the page at
 * least that many times, so a quotation that drops its mark, or is retyped
 * by hand, fails the build. Keys only: the values come from pack-figures.js.
 */
export function packPlacements() {
  /** @type {Map<string, string[]>} */
  const where = new Map();
  const quote = (section, keys) => {
    for (const key of keys) where.set(key, [...(where.get(key) ?? []), section]);
  };
  const score = (model, fields) => fields.map((field) => `scorecard.${model}.${field}`);

  // The pack's group label and its four tiles.
  quote('evidence', ['window', 'prsMerged', 'costPerMergedPr', 'totalSpend', 'epics', 'items']);
  // "Where the $X went", and the spend bar's legend.
  quote('evidence', ['totalSpend', ...RUN_KINDS.map((r) => `runKinds.${r.kind}.cost`)]);
  // The per-model fold: its table and its notes.
  quote('evidence', SCORECARD.flatMap((r) => score(r.model, ['runs', 'opened', 'merged', 'spend', 'perPr'])));
  quote('evidence', ['unattributedRuns', 'scorecardMerged', 'prsMerged']);
  return where;
}

/**
 * Where the front page quotes each figure of the development repository's
 * snapshot, as key -> the sections that quote it, once per quotation: the
 * group label's date, the large count, and the share bar's legend.
 */
export function repoPlacements() {
  return new Map(
    ['captured', 'agentPrs', 'mergedPrs', 'agentShare', 'otherShare'].map((key) => [key, ['evidence']])
  );
}

/** The docs' word for Linear's status: the title of the page's first warning. */
export function linearStatusFromDocs(markdown) {
  const hit = markdown.match(/^!!! warning "([^"]+)"/m);
  if (!hit) throw new Error('docs/linear-webhooks.md has no titled warning to read the status from');
  return hit[1];
}

const times = (n) => `${n} time${n === 1 ? '' : 's'}`;

function claimOffenders(html) {
  const sources = [textOf(html, ' ')];
  for (const match of html.matchAll(COPY_ATTRIBUTES)) {
    sources.push(decode(match[1] ?? match[2]).replace(/\s+/g, ' '));
  }
  const offenders = [];
  for (const [rule, pattern] of CLAIMS) {
    const hit = sources.map((source) => source.match(pattern)).find(Boolean);
    if (hit) offenders.push({ rule, detail: hit[0] });
  }
  return offenders;
}

/**
 * The marked figures of one source that are not its own: `attribute` names
 * the mark, `rule` the offence, `source` the data ("pack", "snapshot").
 */
function figureOffenders(html, { attribute, rule, source }, figures, placements) {
  const offenders = [];
  const quoted = new Map();
  for (const { value, text } of marked(html, attribute)) {
    quoted.set(value, [...(quoted.get(value) ?? []), text]);
  }
  for (const [key, shown] of quoted) {
    if (!figures.has(key)) {
      offenders.push({
        rule,
        detail: `${key} is marked as a ${source} figure, and the ${source} holds no such figure`
      });
      continue;
    }
    const expected = figures.get(key);
    for (const wrong of new Set(shown.filter((text) => text !== expected))) {
      offenders.push({ rule, detail: `${key} renders as "${wrong}", the ${source} says "${expected}"` });
    }
  }
  for (const [key, where] of placements) {
    const count = quoted.get(key)?.length ?? 0;
    if (count < where.length) {
      offenders.push({
        rule,
        detail: `${key} is marked ${times(count)}, the page quotes it in ${where.length} places (${where.join(', ')})`
      });
    }
  }
  return offenders;
}

const PACK_MARK = { attribute: 'data-pack', rule: 'pack-figure', source: 'pack' };
const REPO_MARK = { attribute: 'data-repo', rule: 'repo-figure', source: 'snapshot' };

function linearOffenders(html, linearStatus) {
  const badges = marked(html, 'data-tracker-badge').filter((badge) => badge.value === 'linear');
  if (!badges.length) return [{ rule: 'linear-status', detail: 'no Linear badge on the page' }];
  return badges
    .filter((badge) => badge.text !== linearStatus)
    .map((badge) => ({
      rule: 'linear-status',
      detail: `Linear badge reads "${badge.text}", the docs say "${linearStatus}"`
    }));
}

/** Find untrue claims, and figures that are not their source's own, in a rendered page. */
export function pageClaimOffenders(
  html,
  {
    linearStatus,
    figures = packFigures(),
    placements = packPlacements(),
    repo = repoFigures(),
    repoAt = repoPlacements()
  }
) {
  return [
    ...claimOffenders(html),
    ...figureOffenders(html, PACK_MARK, figures, placements),
    ...figureOffenders(html, REPO_MARK, repo, repoAt),
    ...linearOffenders(html, linearStatus)
  ];
}

function main() {
  const linearStatus = linearStatusFromDocs(readFileSync(LINEAR_DOC, 'utf8'));
  const offenders = pageClaimOffenders(readFileSync(PAGE, 'utf8'), { linearStatus });
  if (offenders.length) {
    console.error(`Found ${offenders.length} untrue claim(s) on the front page:\n`);
    for (const offender of offenders) console.error(`  ${offender.rule}: ${offender.detail}`);
    process.exitCode = 1;
    return;
  }
  console.log('front page claims and figures agree');
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main();
}
