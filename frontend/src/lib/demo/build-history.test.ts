/**
 * What the demo's page foot says about each surface (#3144), read from the
 * committed provenance sample.
 */
import { describe, expect, it } from 'vitest';
import sample from '../../../demo/provenance/provenance.sample.json';
import {
  type Provenance,
  buildSummary,
  featuresFor,
  heldOutLine,
  integrityLine,
  prLabel,
  surfaceRoute,
  tallied
} from './build-history';

const doc = sample as Provenance;
const feature = (id: string) => doc.features.find((f) => f.id === id)!;

describe('which features a surface shows', () => {
  it('drops route groups from the route id', () => {
    expect(surfaceRoute('/(factory)/epics/[ref]')).toBe('/epics/[ref]');
    expect(surfaceRoute('/(factory)')).toBe('/');
  });

  it('reads the epic graph as its one feature, and /work as both features that name it', () => {
    expect(featuresFor(doc, '/(factory)/epics/[ref]').map((f) => f.id)).toEqual(['epic-graph']);
    expect(featuresFor(doc, '/(factory)/work').map((f) => f.id)).toEqual(['needs-you', 'work']);
  });

  it('reads no feature on a surface none names, or with no route', () => {
    expect(featuresFor(doc, '/(factory)/health')).toEqual([]);
    expect(featuresFor(doc, null)).toEqual([]);
  });
});

describe('the collapsed line', () => {
  it('counts runs, PRs and list cost for the epic graph', () => {
    expect(buildSummary([feature('epic-graph')])).toBe('Built by 10 agent runs across 3 PRs, $21.75 list cost.');
  });

  it('adds up every feature on the surface, counting a shared PR once', () => {
    const a = feature('work');
    const b = { ...feature('needs-you'), prs: [...feature('needs-you').prs, a.prs[0]] };
    const runs = [a, b].reduce((n, f) => n + Object.values(f.runs).reduce((x, y) => x + y, 0), 0);
    const prs = new Set([...a.prs, ...b.prs].map((pr) => pr.url)).size;
    const cost = (a.list_cost_usd + b.list_cost_usd).toFixed(2);
    expect(buildSummary([a, b])).toBe(`Built by ${runs} agent runs across ${prs} PRs, $${cost} list cost.`);
    expect(prs).toBe(a.prs.length + b.prs.length - 1);
  });

  it('says one run and one PR in the singular', () => {
    const one = { ...feature('epic-graph'), runs: { dispatch: 1 }, prs: [feature('epic-graph').prs[0]], list_cost_usd: 1 };
    expect(buildSummary([one])).toBe('Built by 1 agent run across 1 PR, $1.00 list cost.');
  });
});

describe('the ledger rows', () => {
  it('words a tally, largest first', () => {
    expect(tallied(feature('epic-graph').runs)).toEqual([
      { word: 'adversarial review', count: 5 },
      { word: 'dispatch', count: 3 },
      { word: 'review remediation', count: 2 }
    ]);
    expect(tallied({ READY_WITH_RESIDUALS: 1, NEEDS_CHANGES: 2, READY: 0 })).toEqual([
      { word: 'needs changes', count: 2 },
      { word: 'ready with residuals', count: 1 }
    ]);
  });

  it('labels a PR by its number', () => {
    expect(prLabel('https://github.com/acme/ledger/pull/43')).toBe('#43');
  });

  it('says the held-out and integrity readings as sentences', () => {
    expect(heldOutLine(feature('epic-graph').held_out)).toBe('1 test failed on 1 item, 1 undeclared.');
    expect(heldOutLine(feature('epics-list').held_out)).toBe('No held-out test failed.');
    expect(integrityLine(feature('epic-graph').integrity, 'corpus')).toBe('1 run flagged: skip added 1.');
    expect(integrityLine(feature('epics-list').integrity, 'corpus')).toBe('No run flagged.');
    expect(integrityLine(feature('epics-list').integrity, 'none')).toBe('Not read: the export had no run reports to check.');
  });
});
