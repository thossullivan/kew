/**
 * The build-history files pass an allowlist schema before the demo bundles
 * them (#3144): an extra key, a wrong type or free text where kew's own
 * vocabulary belongs is refused, every PR link points at the one repository
 * the file is for, every feature's epics, issues and routes are the ones its
 * features file names, the committed samples pass, and the plugin ships the
 * real file when it exists and refuses one the schema rejects.
 */
import { copyFileSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { afterEach, describe, expect, it } from 'vitest';
import { demoProvenance, readProvenance } from '../demo/provenance-bundle';
import {
  ERROR_CATEGORIES,
  MAX_EPIC_ISSUES,
  PUBLIC_REPO,
  SAMPLE_REPO,
  featureVocab,
  provenanceProblems,
  runsProblems
} from '../demo/provenance-schema';

const DEMO = new URL('../demo/', import.meta.url);
const SAMPLES = new URL('provenance/', DEMO);
const FEATURES = new URL('features.toml', DEMO);
const SAMPLE_FEATURES = new URL('features.sample.toml', SAMPLES);
const real = { features: featureVocab(readFileSync(FEATURES, 'utf8')), repo: PUBLIC_REPO };
const samples = { features: featureVocab(readFileSync(SAMPLE_FEATURES, 'utf8')), repo: SAMPLE_REPO };
const sample = (name: string) => JSON.parse(readFileSync(new URL(`${name}.sample.json`, SAMPLES), 'utf8'));
const prOn = (repo: string, n = 12) => `https://github.com/${repo}/pull/${n}`;
const [PUBLIC_OWNER] = PUBLIC_REPO.split('/');

/** The sample, reshaped as a real export: features.toml's epics and issues, PR links on the public repo. */
function realDoc() {
  const doc = sample('provenance');
  for (const f of doc.features) {
    const named = real.features.find((v) => v.id === f.id)!;
    f.epics = [...named.epics];
    f.issues = [...named.issues];
    f.prs = f.prs.map((pr: { url: string }, i: number) => ({ ...pr, url: prOn(PUBLIC_REPO, 3000 + i) }));
  }
  return doc;
}
function realRuns() {
  const runs = sample('runs');
  for (const row of runs.runs) if (row.pr_url !== null) row.pr_url = prOn(PUBLIC_REPO, Number(row.pr_url.split('/').at(-1)));
  return runs;
}

describe('the provenance schema', () => {
  it('reads every feature features.toml names, with its routes, epics and issues', () => {
    expect(real.features.map((f) => f.id)).toEqual(['epics-list', 'epic-graph', 'monitor', 'needs-you', 'work', 'prompts', 'budget', 'runs']);
    expect(real.features.find((f) => f.id === 'runs')).toMatchObject({ routes: ['/runs', '/runs/[id]'] });
    // #3455: the file names the work that built every surface, so no build-history foot reads 0 runs.
    for (const f of real.features) expect(f.epics.length + f.issues.length, `${f.id} names no work`).toBeGreaterThan(0);
    expect(real.features.find((f) => f.id === 'epic-graph')?.issues).toContain('3053');
    expect(samples.features.find((f) => f.id === 'epic-graph')).toMatchObject({ epics: ['epic:lumen-graph-canvas'], issues: ['7103', '7104', '7105'] });
  });

  it('refuses a features file it cannot read whole', () => {
    expect(() => featureVocab('[[features]]\nid = "a"\ntitle = "A"\nroutes = ["/a"]\nissues = ["7"]\n')).toThrow(/issues/);
    expect(() => featureVocab('[[features]]\nid = "a"\ntitle = "A"\nroutes = [\n  "/a"\n]\n')).toThrow(/routes/);
  });

  it('passes the committed samples, against their own features file and repository', () => {
    expect(provenanceProblems(sample('provenance'), samples)).toEqual([]);
    expect(runsProblems(sample('runs'), samples)).toEqual([]);
  });

  it('passes a real export of features.toml on the public repository', () => {
    expect(provenanceProblems(realDoc(), real)).toEqual([]);
    expect(runsProblems(realRuns(), real)).toEqual([]);
  });

  it('keeps the samples and the real export on two different repositories', () => {
    expect(SAMPLE_REPO).not.toBe(PUBLIC_REPO);
    // A sample holding a public-repo link fails, and a real file holding a sample-repo link fails.
    const doc = sample('provenance');
    doc.features[0].prs[0].url = prOn(PUBLIC_REPO);
    expect(provenanceProblems(doc, samples)).toEqual([
      `provenance.features[0].prs[0].url: not a pull request on ${SAMPLE_REPO}: "${prOn(PUBLIC_REPO)}"`
    ]);
    const live = realDoc();
    live.features[0].prs[0].url = prOn(SAMPLE_REPO);
    expect(provenanceProblems(live, real)).toEqual([
      `provenance.features[0].prs[0].url: not a pull request on ${PUBLIC_REPO}: "${prOn(SAMPLE_REPO)}"`
    ]);
    const runs = realRuns();
    runs.runs[0].pr_url = prOn(SAMPLE_REPO);
    expect(runsProblems(runs, real)).toEqual([`runs.runs[0].pr_url: not a pull request on ${PUBLIC_REPO} or null: "${prOn(SAMPLE_REPO)}"`]);
  });

  it('refuses a PR link to another repository or another owner', () => {
    const doc = realDoc();
    doc.features[0].prs[0].url = prOn(`${PUBLIC_OWNER}/private-client-repo`);
    doc.features[1].prs[0].url = prOn('someone-else/kew');
    doc.features[2].prs[0].url = `${prOn(PUBLIC_REPO)}/files`;
    // A refused value is shown cut to 60 characters, so a log never carries a long one whole.
    const refused = (i: number, url: string) =>
      `provenance.features[${i}].prs[0].url: not a pull request on ${PUBLIC_REPO}: ${JSON.stringify(url).slice(0, 60)}`;
    expect(provenanceProblems(doc, real)).toEqual([
      refused(0, prOn(`${PUBLIC_OWNER}/private-client-repo`)),
      refused(1, prOn('someone-else/kew')),
      refused(2, `${prOn(PUBLIC_REPO)}/files`)
    ]);
    const runs = realRuns();
    runs.runs[0].pr_url = prOn(`${PUBLIC_OWNER}/private-client-repo`);
    expect(runsProblems(runs, real)).toHaveLength(1);
  });

  it('refuses an extra key, at the top, on a feature, on a PR and on a run row', () => {
    const doc = sample('provenance');
    doc.notes = 'x';
    doc.features[0].log = 'x';
    doc.features[0].prs[0].title = 'x';
    expect(provenanceProblems(doc, samples)).toEqual([
      'provenance.notes: not an allowed key',
      'provenance.features[0].log: not an allowed key',
      'provenance.features[0].prs[0].title: not an allowed key'
    ]);
    const runs = sample('runs');
    runs.runs[0].prompt = 'x';
    expect(runsProblems(runs, samples)).toEqual(['runs.runs[0].prompt: not an allowed key']);
  });

  it('refuses a missing key', () => {
    const doc = sample('provenance');
    delete doc.features[1].held_out.tests_failed;
    expect(provenanceProblems(doc, samples)).toEqual(['provenance.features[1].held_out.tests_failed: missing']);
  });

  it('refuses a wrong type', () => {
    const doc = sample('provenance');
    doc.features[0].list_cost_usd = '10.17';
    doc.features[0].runs.dispatch = 2.5;
    doc.features[0].prs[0].landed_sha = 7;
    expect(provenanceProblems(doc, samples)).toEqual([
      'provenance.features[0].prs[0].landed_sha: not a commit sha or null: 7',
      'provenance.features[0].runs.dispatch: not a count: 2.5',
      'provenance.features[0].list_cost_usd: not a dollar amount: "10.17"'
    ]);
    const runs = sample('runs');
    runs.runs[0].total_tokens = '173000';
    expect(runsProblems(runs, samples)).toEqual(['runs.runs[0].total_tokens: not a count or null: "173000"']);
  });

  it('refuses free text outside the fixed vocabulary', () => {
    const doc = sample('provenance');
    doc.features[0].title = 'Epics list, built by Alex';
    doc.features[0].review_rounds['looks good to me'] = 1;
    doc.features[0].integrity.flags['the reviewer said so'] = 1;
    doc.integrity_source = 'a corpus on my laptop';
    expect(provenanceProblems(doc, samples)).toEqual([
      'provenance.integrity_source: not corpus or none: "a corpus on my laptop"',
      'provenance.features[0].title: not the title features.toml gives epics-list: "Epics list, built by Alex"',
      'provenance.features[0].review_rounds.looks good to me: not in the vocabulary',
      'provenance.features[0].integrity.flags.the reviewer said so: not in the vocabulary'
    ]);
    const runs = sample('runs');
    runs.runs[0].error_category = 'Traceback: something broke';
    runs.runs[0].status = 'done';
    runs.runs[0].pr_url = 'https://example.org/pull/1';
    expect(runsProblems(runs, samples)).toEqual([
      `runs.runs[0].pr_url: not a pull request on ${SAMPLE_REPO} or null: "https://example.org/pull/1"`,
      'runs.runs[0].status: not a run status: "done"',
      'runs.runs[0].error_category: not an error category kew records, "other", or null: "Traceback: something broke"'
    ]);
  });

  it('refuses epics features.toml does not name for the feature, and a missing one', () => {
    const doc = realDoc();
    doc.features[0].epics = ['acme-corp-layoff-plan-q4'];
    const samp = sample('provenance');
    samp.features[0].epics = [];
    expect(provenanceProblems(doc, real)).toEqual([
      'provenance.features[0].epics: not the epics features.toml gives epics-list: ["acme-corp-layoff-plan-q4"]'
    ]);
    expect(provenanceProblems(samp, samples)).toEqual([
      'provenance.features[0].epics: not the epics features.toml gives epics-list: []'
    ]);
  });

  it('refuses an issue that is not an issue number, one features.toml does not name, and a missing one', () => {
    // Built from features.toml's own lists, so the rule holds whatever work the file names.
    const doc = realDoc();
    const [epicsList, graph, monitor, needsYou] = real.features;
    doc.features[0].issues = [...epicsList.issues, 'CLIENTX-42', 'password:hunter2'];
    doc.features[1].issues = [...graph.issues, '7'];
    doc.features[2].issues = monitor.issues.slice(1);
    doc.features[3].issues = needsYou.issues.map(Number);
    expect(provenanceProblems(doc, real)).toEqual([
      'provenance.features[0].issues: not an issue number: "CLIENTX-42"',
      'provenance.features[0].issues: not an issue number: "password:hunter2"',
      'provenance.features[1].issues: names issue 7, which features.toml does not give epic-graph',
      `provenance.features[2].issues: missing issue ${monitor.issues[0]}, which features.toml gives monitor`,
      ...needsYou.issues.map((n) => `provenance.features[3].issues: not an issue number: ${n}`),
      ...needsYou.issues.map((n) => `provenance.features[3].issues: missing issue ${n}, which features.toml gives needs-you`)
    ]);
  });

  it("allows an epic's own issues beside the named ones, but only as issue numbers", () => {
    // `kew report provenance` lists the nodes of each named epic as well.
    const doc = sample('provenance');
    doc.features[0].issues.push('7999');
    expect(provenanceProblems(doc, samples)).toEqual([]);
    doc.features[0].issues.push('LIN-4');
    expect(provenanceProblems(doc, samples)).toEqual(['provenance.features[0].issues: not an issue number: "LIN-4"']);
  });

  it('refuses an issue number with a leading zero, and more epic issues than the cap', () => {
    const doc = sample('provenance');
    doc.features[0].issues.push('07999', '0');
    expect(provenanceProblems(doc, samples)).toEqual([
      'provenance.features[0].issues: not an issue number: "07999"',
      'provenance.features[0].issues: not an issue number: "0"'
    ]);
    const capped = sample('provenance');
    const named = new Set(capped.features[0].issues);
    const extra = Array.from({ length: MAX_EPIC_ISSUES + 1 }, (_, i) => String(10_000 + i)).filter((n) => !named.has(n));
    capped.features[0].issues.push(...extra.slice(0, MAX_EPIC_ISSUES));
    expect(provenanceProblems(capped, samples)).toEqual([]);
    capped.features[0].issues.push(extra[MAX_EPIC_ISSUES]);
    expect(provenanceProblems(capped, samples)).toEqual([
      `provenance.features[0].issues: lists ${MAX_EPIC_ISSUES + 1} issues beyond the ones features.toml names; at most ${MAX_EPIC_ISSUES}`
    ]);
    expect(MAX_EPIC_ISSUES).toBe(2000);
  });

  it('refuses routes that are not exactly the ones features.toml gives, and a feature named twice', () => {
    const doc = realDoc();
    doc.features[0].routes = [];
    doc.features[3].routes = ['/work'];
    doc.features[4].routes = ['/work', '/secret'];
    doc.features[5].id = 'work';
    doc.features[5].title = 'Work';
    doc.features[5].routes = ['/work'];
    doc.features[5].epics = [];
    doc.features[5].issues = [...real.features.find((f) => f.id === 'work')!.issues];
    expect(provenanceProblems(doc, real)).toEqual([
      'provenance.features[0].routes: not the routes features.toml gives epics-list: []',
      'provenance.features[3].routes: not the routes features.toml gives needs-you: ["/work"]',
      'provenance.features[4].routes: not the routes features.toml gives work: ["/work","/secret"]',
      'provenance.features[5].id: names work a second time'
    ]);
  });

  it('refuses a feature features.toml does not name, and a wrong schema tag', () => {
    const doc = sample('provenance');
    doc.schema = 'kew/provenance@2';
    doc.features[0].id = 'admin';
    expect(provenanceProblems(doc, samples)).toEqual([
      'provenance.schema: not kew/provenance@1: "kew/provenance@2"',
      'provenance.features[0].id: not a feature features.toml names: "admin"'
    ]);
  });

  it("refuses a run id that is not a uuid, a model off kew's catalog and an error category kew never records", () => {
    const runs = sample('runs');
    runs.runs[0].run_id = 'run-sample-0001';
    runs.runs[1].model = 'acme-private/llm-7-internal';
    runs.runs[2].error_category = 'acme_corp_layoffs';
    runs.runs[3].model = 'other';
    runs.runs[4].error_category = 'container_start_failed';
    expect(runsProblems(runs, samples)).toEqual([
      'runs.runs[0].run_id: not a run id (a uuid): "run-sample-0001"',
      'runs.runs[1].model: not a model in kew\'s catalog, "other", or null: "acme-private/llm-7-internal"',
      'runs.runs[2].error_category: not an error category kew records, "other", or null: "acme_corp_layoffs"'
    ]);
  });

  it("reads kew's error categories from the committed vocabulary", () => {
    expect(ERROR_CATEGORIES.has('container_start_failed')).toBe(true);
    expect(ERROR_CATEGORIES.has('review_stage_finished')).toBe(true);
    expect(ERROR_CATEGORIES.has('other')).toBe(false);
    for (const code of ERROR_CATEGORIES) expect(code).toMatch(/^[a-z][a-z0-9_]*$/);
  });

  it('passes a run row as the export now writes one: a review-stage code, "other", a null item id, a uuid', () => {
    // `kew report provenance` publishes an error category off its list as
    // "other", writes an empty item id as null, and drops a run whose id is
    // not a uuid (`report_provenance.py`).
    const runs = realRuns();
    Object.assign(runs.runs[0], { error_category: 'review_stage_finished', run_kind: 'review_stage' });
    Object.assign(runs.runs[1], { error_category: 'other' });
    Object.assign(runs.runs[2], { item_id: null, epic: null });
    runs.runs[3].run_id = '3f2b8c1e-9a4d-4e6f-8b2a-1c0d9e8f7a6b';
    expect(runsProblems(runs, real)).toEqual([]);
    // Still refused: a code off the list, an empty item id, a legacy run id.
    Object.assign(runs.runs[1], { error_category: 'acme_private_code' });
    Object.assign(runs.runs[2], { item_id: '' });
    runs.runs[3].run_id = 'LegacyRunId0123456789ABCDEFabcdef01234567890';
    expect(runsProblems(runs, real)).toEqual([
      'runs.runs[1].error_category: not an error category kew records, "other", or null: "acme_private_code"',
      'runs.runs[2].item_id: not an item id or null: ""',
      'runs.runs[3].run_id: not a run id (a uuid): "LegacyRunId0123456789ABCDEFabcdef01234567890"'
    ]);
  });

  it('pins the real export to the public kew repository, in the schema and in public-repo.json alike', () => {
    // scripts/ and demo/public-repo.json are both merge-gate paths (the demo
    // checks read the file to tell the real build history from the samples).
    // The pin lives here, and both the file and the constant read from it must agree.
    const file = JSON.parse(readFileSync(new URL('public-repo.json', DEMO), 'utf8')) as { repo: string };
    expect(file.repo).toBe('thossullivan/kew');
    expect(PUBLIC_REPO).toBe('thossullivan/kew');
    expect(PUBLIC_REPO).toBe(file.repo);
  });
});

describe('the bundled provenance', () => {
  // Every temp folder a test makes is removed after it: each run left one
  // behind per test before.
  const temps: string[] = [];
  afterEach(() => {
    for (const made of temps.splice(0)) rmSync(made, { recursive: true, force: true });
  });
  const dir = (files: Record<string, unknown>) => {
    const root = mkdtempSync(join(tmpdir(), 'kew-provenance-'));
    temps.push(root);
    for (const name of ['provenance.sample.json', 'runs.sample.json', 'features.sample.toml'])
      copyFileSync(new URL(name, SAMPLES), join(root, name));
    for (const [name, value] of Object.entries(files)) writeFileSync(join(root, name), JSON.stringify(value));
    return pathToFileURL(`${root}/`);
  };

  it('is the sample when no real file exists', () => {
    expect(readProvenance(dir({}), FEATURES)).toEqual(sample('provenance'));
  });

  it('is the real file when one exists', () => {
    const live = realDoc();
    live.features[0].list_cost_usd = 99;
    expect(readProvenance(dir({ 'provenance.json': live, 'runs.json': realRuns() }), FEATURES)).toEqual(live);
  });

  it('refuses a real file holding the samples, a real file the schema rejects, and a real file without its runs', () => {
    expect(() =>
      readProvenance(dir({ 'provenance.json': sample('provenance'), 'runs.json': sample('runs') }), FEATURES)
    ).toThrow(/not a pull request on/);
    const live = realDoc();
    live.features[0].log = 'x';
    expect(() => readProvenance(dir({ 'provenance.json': live, 'runs.json': realRuns() }), FEATURES)).toThrow(
      /refused by its schema:\n {2}provenance\.features\[0\]\.log: not an allowed key/
    );
    const runs = realRuns();
    runs.runs[0].prompt = 'x';
    expect(() => readProvenance(dir({ 'provenance.json': realDoc(), 'runs.json': runs }), FEATURES)).toThrow(
      /runs\.runs\[0\]\.prompt: not an allowed key/
    );
    expect(() => readProvenance(dir({ 'provenance.json': realDoc() }), FEATURES)).toThrow(/one real file without the other/);
  });

  it('refuses, in the plugin itself, a real file whose PR links leave the public repository', () => {
    const live = realDoc();
    live.features[0].prs[0].url = prOn(`${PUBLIC_OWNER}/private-client-repo`);
    const plugin = demoProvenance(dir({ 'provenance.json': live, 'runs.json': realRuns() }), FEATURES);
    const buildStart = plugin.buildStart as (this: unknown) => void;
    expect(() => buildStart.call({})).toThrow(
      new RegExp(`refused by its schema:\\n {2}provenance\\.features\\[0\\]\\.prs\\[0\\]\\.url: not a pull request on ${PUBLIC_REPO}`)
    );
    const fine = demoProvenance(dir({}), FEATURES).buildStart as (this: unknown) => void;
    expect(() => fine.call({})).not.toThrow();
  });

  it('serves the routes the shipped file names and which file it is, for the page foot to read before the file arrives', () => {
    const routes = ['/epics', '/epics/[ref]', '/monitor', '/work', '/prompts', '/budget', '/runs', '/runs/[id]'];
    const meta = (root: URL) => {
      const plugin = demoProvenance(root, FEATURES);
      const resolveId = plugin.resolveId as (id: string) => string | null;
      const load = plugin.load as (id: string) => string | null;
      const id = resolveId('virtual:kew-demo-provenance-meta');
      expect(id).not.toBeNull();
      expect(resolveId('virtual:kew-demo-fixtures')).toBeNull();
      return load(id!);
    };
    // Which file shipped: a sample build says so on the foot's line.
    expect(meta(dir({}))).toBe(`export default ${JSON.stringify({ routes, source: 'sample' })};`);
    expect(meta(dir({ 'provenance.json': realDoc(), 'runs.json': realRuns() }))).toBe(
      `export default ${JSON.stringify({ routes, source: 'real' })};`
    );
  });
});
