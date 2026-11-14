/**
 * The allowlist schema the build-history files pass before the demo bundles
 * them (#3144).
 *
 * `provenance.json` and `runs.json` are what `kew report provenance` writes
 * from the real ledger (`kew/src/kew/report_provenance.py`). The demo site is
 * public, so each is read here as ledger data and nothing else: every object
 * holds exactly its documented keys, every value has its documented type, and
 * every string is kew's own vocabulary (a run kind, a verdict, a flag, a
 * status, a model in kew's catalog or `other`, an error category the export
 * publishes or `other`), an id of
 * a fixed shape (a uuid run id, a GitHub issue number, a commit sha), an
 * instant, a pull request on the ONE repository the file is for, or a
 * feature's id, title, routes, epics and issues exactly as its features file
 * names them. A key the schema does not know, a wrong type, or a word where a
 * code belongs is a problem, and the demo build refuses to bundle a file with
 * one. The leak scan (`leak-scan.mjs`) stays the second layer.
 *
 * Two ledgers are read the same way: the real export, against
 * `features.toml` and `PUBLIC_REPO`, and the committed `*.sample.json` files,
 * against `provenance/features.sample.toml` and `SAMPLE_REPO`
 * (`scripts/demo-provenance.test.ts`). The two repositories differ, so a
 * sample link in a real file, or a real link in a sample, is refused.
 */
import { MODEL_NAMES } from '../src/lib/factory/names';
import errorCategories from './error-categories.json';
import publicRepo from './public-repo.json';

export const PROVENANCE_SCHEMA = 'kew/provenance@1';
export const RUNS_SCHEMA = 'kew/provenance-runs@1';

/**
 * The repository every PR link of the REAL build history is on: kew itself,
 * since the demo shows how kew was built. Committed (`public-repo.json`), so
 * no export can widen it: a link to any other repository, a private client's
 * included, fails the schema. A publish needs the real export and the
 * owner's approval; a build made without it bundles the committed samples.
 */
export const PUBLIC_REPO: string = publicRepo.repo;

/**
 * The repository the committed samples link to: kew's name under the demo
 * world's invented owner (`owner` in `leak-scan.allow.json`), so the leak
 * scan's owner rule holds the samples without any exemption.
 */
export const SAMPLE_REPO = 'acme/kew';

/** `KNOWN_RUN_KINDS` in `run_store.py`. */
export const RUN_KINDS: ReadonlySet<string> = new Set([
  'dispatch',
  'loop',
  'delegation',
  'review',
  'planning',
  'adversarial_review',
  'review_remediation',
  'review_stage',
  'ci_fix',
  'resolve',
  'readiness_probe',
  'triage',
  'acceptance_author'
]);

/** The verdicts a review round records (`review_rounds.verdict`). */
export const VERDICTS: ReadonlySet<string> = new Set([
  'READY',
  'READY_WITH_RESIDUALS',
  'NEEDS_CHANGES',
  'NEEDS_FULL',
  'DEGRADED',
  'UNPARSEABLE'
]);

/** The test-integrity flag names (`test_integrity.py`). */
export const INTEGRITY_FLAGS: ReadonlySet<string> = new Set([
  'asserts_removed',
  'tests_removed',
  'skip_added',
  'changed_expected',
  'test_only_fix_round',
  'raises_loosened',
  'mock_any_added',
  'undeclared_test_edits'
]);

/** `RunStatus` in `run_store.py`. */
export const RUN_STATUSES: ReadonlySet<string> = new Set([
  'pending',
  'awaiting_publication',
  'success',
  'failure',
  'refused',
  'budget_exceeded',
  'budget_stopped',
  'budget_stop',
  'killed'
]);

/** A run's outcome: its status, or `merged` (`run_report_sink._outcome`). */
export const RUN_OUTCOMES: ReadonlySet<string> = new Set([...RUN_STATUSES, 'merged']);

/** `RUNNERS` in `model_catalog.py`. */
export const RUNNERS: ReadonlySet<string> = new Set(['claude-code', 'codex', 'opencode']);

/**
 * The model ids a run row may name: every id and alias of kew's model catalog
 * (`kew/src/kew/models.json`, read through `names.ts`), and `other`, which
 * `kew report provenance` writes for any id the catalog does not name
 * (`OTHER_MODEL`).
 */
export const MODELS: ReadonlySet<string> = new Set([...Object.keys(MODEL_NAMES), 'other']);

/**
 * The error categories `kew report provenance` publishes as themselves:
 * `error-categories.json`, held equal to `PUBLISHED_ERROR_CATEGORIES` in
 * `report_provenance.py` by `kew/tests/test_error_category_vocabulary.py`.
 * The export writes any other code as `other` (`OTHER_ERROR_CATEGORY`),
 * which a run row may also hold.
 */
export const ERROR_CATEGORIES: ReadonlySet<string> = new Set(errorCategories.categories);
const PUBLISHED_CATEGORIES: ReadonlySet<string> = new Set([...ERROR_CATEGORIES, 'other']);

/**
 * At most this many issues a feature may list beyond the ones its features
 * file names: the nodes of the epics it names, which only the ledger knows.
 * kew's largest epics hold a few dozen nodes; a list past this is not a
 * feature's build history.
 */
export const MAX_EPIC_ISSUES = 2000;

const INSTANT = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/;
const SHA = /^[0-9a-f]{40}$/;
/** A run id as kew mints it (`uuid.uuid4()`). */
const RUN_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
/**
 * A GitHub issue number (`GITHUB_ID_RE` in `kew/src/kew/ids.py`) as GitHub
 * numbers one: from 1, no leading zero, so one issue has one spelling.
 */
const ISSUE = /^[1-9][0-9]*$/;
/** An epic ref or an item id in a run row: one token, no spaces, no sentence. */
const IDENT = /^[A-Za-z0-9][A-Za-z0-9:._-]{0,63}$/;
const escaped = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
/** A pull request on `repo`, exactly as GitHub names one. */
const prUrl = (repo: string) => new RegExp(`^https://github\\.com/${escaped(repo)}/pull/[1-9][0-9]*$`);

/** One feature as its features file names it: the vocabulary its export entry is read against. */
export type FeatureVocab = {
  id: string;
  title: string;
  routes: readonly string[];
  epics: readonly string[];
  /** Issue numbers, as the export writes them: strings of digits. */
  issues: readonly string[];
};

/** What one export is read against: its features file, and the repository its PR links are on. */
export type Ledger = { features: readonly FeatureVocab[]; repo: string };

type Problems = string[];

const isObject = (v: unknown): v is Record<string, unknown> =>
  v !== null && typeof v === 'object' && !Array.isArray(v);
const isCount = (v: unknown): v is number => Number.isInteger(v) && (v as number) >= 0;
const isAmount = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v) && v >= 0;
const shown = (v: unknown): string => JSON.stringify(v)?.slice(0, 60) ?? String(v);
const sameList = (v: unknown, want: readonly string[]) =>
  Array.isArray(v) && v.length === want.length && v.every((x, i) => x === want[i]);

/** `value` at `at` as an object with exactly `keys`; false (and a problem per key) when not. */
function exactKeys(value: unknown, keys: readonly string[], at: string, problems: Problems): value is Record<string, unknown> {
  if (!isObject(value)) {
    problems.push(`${at}: not an object`);
    return false;
  }
  for (const key of Object.keys(value)) if (!keys.includes(key)) problems.push(`${at}.${key}: not an allowed key`);
  for (const key of keys) if (!(key in value)) problems.push(`${at}.${key}: missing`);
  return true;
}

/** `value` at `at` as a `{ <word>: <count> }` tally whose words are all in `vocab`. */
function tally(value: unknown, vocab: ReadonlySet<string>, at: string, problems: Problems): void {
  if (value === undefined) return;
  if (!isObject(value)) {
    problems.push(`${at}: not an object`);
    return;
  }
  for (const [key, count] of Object.entries(value)) {
    if (!vocab.has(key)) problems.push(`${at}.${key}: not in the vocabulary`);
    if (!isCount(count)) problems.push(`${at}.${key}: not a count: ${shown(count)}`);
  }
}

/** `value` at `at` passes `test`, or a problem says it does not. A missing key is already a problem of its own. */
function want(value: unknown, test: (v: unknown) => boolean, what: string, at: string, problems: Problems): void {
  if (value !== undefined && !test(value)) problems.push(`${at}: not ${what}: ${shown(value)}`);
}

const matches = (pattern: RegExp) => (v: unknown) => typeof v === 'string' && pattern.test(v);
const among = (vocab: ReadonlySet<string>) => (v: unknown) => typeof v === 'string' && vocab.has(v);
const orNull = (test: (v: unknown) => boolean) => (v: unknown) => v === null || test(v);

const FEATURE_KEYS = [
  'id',
  'title',
  'routes',
  'epics',
  'issues',
  'prs',
  'runs',
  'list_cost_usd',
  'review_rounds',
  'held_out',
  'integrity'
] as const;

/**
 * A feature's `issues` against the ones its features file names: each an
 * issue number, every named one present, and — when the feature names no
 * epic — nothing else. A feature that names epics also lists those epics'
 * nodes (`_Ledger.items_of` in `report_provenance.py`), which only the ledger
 * knows; they are still held to the issue-number shape, and to at most
 * `MAX_EPIC_ISSUES` of them.
 */
function checkIssues(value: unknown, named: FeatureVocab, at: string, problems: Problems): void {
  if (value === undefined) return;
  if (!Array.isArray(value)) {
    problems.push(`${at}: not a list of issue numbers: ${shown(value)}`);
    return;
  }
  const listed = new Set<string>();
  for (const issue of value) {
    if (typeof issue !== 'string' || !ISSUE.test(issue)) problems.push(`${at}: not an issue number: ${shown(issue)}`);
    else if (listed.has(issue)) problems.push(`${at}: names issue ${issue} twice`);
    else listed.add(issue);
  }
  for (const issue of named.issues)
    if (!listed.has(issue)) problems.push(`${at}: missing issue ${issue}, which features.toml gives ${named.id}`);
  const extra = [...listed].filter((issue) => !named.issues.includes(issue));
  if (named.epics.length === 0)
    for (const issue of extra) problems.push(`${at}: names issue ${issue}, which features.toml does not give ${named.id}`);
  else if (extra.length > MAX_EPIC_ISSUES)
    problems.push(`${at}: lists ${extra.length} issues beyond the ones features.toml names; at most ${MAX_EPIC_ISSUES}`);
}

function checkFeature(entry: unknown, ledger: Ledger, seen: Set<string>, at: string, problems: Problems): void {
  if (!exactKeys(entry, FEATURE_KEYS, at, problems)) return;
  const named = ledger.features.find((f) => f.id === entry.id);
  if (named === undefined) problems.push(`${at}.id: not a feature features.toml names: ${shown(entry.id)}`);
  else if (seen.has(named.id)) problems.push(`${at}.id: names ${named.id} a second time`);
  else seen.add(named.id);
  if (named !== undefined) {
    want(entry.title, (v) => v === named.title, `the title features.toml gives ${named.id}`, `${at}.title`, problems);
    want(entry.routes, (v) => sameList(v, named.routes), `the routes features.toml gives ${named.id}`, `${at}.routes`, problems);
    want(entry.epics, (v) => sameList(v, named.epics), `the epics features.toml gives ${named.id}`, `${at}.epics`, problems);
    checkIssues(entry.issues, named, `${at}.issues`, problems);
  }
  if (!Array.isArray(entry.prs)) problems.push(`${at}.prs: not a list`);
  else
    entry.prs.forEach((pr, i) => {
      if (!exactKeys(pr, ['url', 'landed_sha'], `${at}.prs[${i}]`, problems)) return;
      want(pr.url, matches(prUrl(ledger.repo)), `a pull request on ${ledger.repo}`, `${at}.prs[${i}].url`, problems);
      want(pr.landed_sha, orNull(matches(SHA)), 'a commit sha or null', `${at}.prs[${i}].landed_sha`, problems);
    });
  tally(entry.runs, RUN_KINDS, `${at}.runs`, problems);
  want(entry.list_cost_usd, isAmount, 'a dollar amount', `${at}.list_cost_usd`, problems);
  tally(entry.review_rounds, VERDICTS, `${at}.review_rounds`, problems);
  const held = ['items_failed', 'tests_failed', 'tests_undeclared'];
  if (exactKeys(entry.held_out, held, `${at}.held_out`, problems))
    for (const key of held) want(entry.held_out[key], isCount, 'a count', `${at}.held_out.${key}`, problems);
  if (exactKeys(entry.integrity, ['runs_flagged', 'flags'], `${at}.integrity`, problems)) {
    want(entry.integrity.runs_flagged, isCount, 'a count', `${at}.integrity.runs_flagged`, problems);
    tally(entry.integrity.flags, INTEGRITY_FLAGS, `${at}.integrity.flags`, problems);
  }
}

/** Every way `doc` is not a `provenance.json` of `ledger`; empty when it is one. */
export function provenanceProblems(doc: unknown, ledger: Ledger): string[] {
  const problems: Problems = [];
  if (!exactKeys(doc, ['schema', 'as_of', 'integrity_source', 'features'], 'provenance', problems)) return problems;
  want(doc.schema, (v) => v === PROVENANCE_SCHEMA, PROVENANCE_SCHEMA, 'provenance.schema', problems);
  want(doc.as_of, orNull(matches(INSTANT)), 'an instant', 'provenance.as_of', problems);
  want(doc.integrity_source, among(new Set(['corpus', 'none'])), 'corpus or none', 'provenance.integrity_source', problems);
  const seen = new Set<string>();
  if (!Array.isArray(doc.features)) problems.push('provenance.features: not a list');
  else doc.features.forEach((f, i) => checkFeature(f, ledger, seen, `provenance.features[${i}]`, problems));
  return problems;
}

/** `RUN_EXPORT_KEYS` in `report_provenance.py`, each with the values it may hold. */
function runFields(repo: string): ReadonlyArray<[string, (v: unknown) => boolean, string]> {
  return [
    ['run_id', matches(RUN_ID), 'a run id (a uuid)'],
    ['item_id', orNull(matches(IDENT)), 'an item id or null'],
    ['epic', orNull(matches(IDENT)), 'an epic ref or null'],
    ['pr_url', orNull(matches(prUrl(repo))), `a pull request on ${repo} or null`],
    ['run_kind', orNull(among(RUN_KINDS)), 'a run kind or null'],
    ['model', orNull(among(MODELS)), `a model in kew's catalog, "other", or null`],
    ['runner', orNull(among(RUNNERS)), 'a runner or null'],
    ['status', among(RUN_STATUSES), 'a run status'],
    ['outcome', among(RUN_OUTCOMES), 'a run outcome'],
    ['cost_usd', orNull(isAmount), 'a dollar amount or null'],
    ['duration_seconds', orNull(isAmount), 'a duration or null'],
    ['total_tokens', orNull(isCount), 'a count or null'],
    ['review_cycles', orNull(isCount), 'a count or null'],
    ['attempts', orNull(isCount), 'a count or null'],
    ['started_at', orNull(matches(INSTANT)), 'an instant or null'],
    ['finished_at', orNull(matches(INSTANT)), 'an instant or null'],
    ['error_category', orNull(among(PUBLISHED_CATEGORIES)), 'an error category kew records, "other", or null']
  ];
}

/** Every way `doc` is not a `runs.json` of `ledger`; empty when it is one. */
export function runsProblems(doc: unknown, ledger: Ledger): string[] {
  const problems: Problems = [];
  if (!exactKeys(doc, ['schema', 'as_of', 'runs'], 'runs', problems)) return problems;
  want(doc.schema, (v) => v === RUNS_SCHEMA, RUNS_SCHEMA, 'runs.schema', problems);
  want(doc.as_of, orNull(matches(INSTANT)), 'an instant', 'runs.as_of', problems);
  if (!Array.isArray(doc.runs)) {
    problems.push('runs.runs: not a list');
    return problems;
  }
  const fields = runFields(ledger.repo);
  const keys = fields.map(([key]) => key);
  doc.runs.forEach((row, i) => {
    if (!exactKeys(row, keys, `runs.runs[${i}]`, problems)) return;
    for (const [key, test, what] of fields) want(row[key], test, what, `runs.runs[${i}].${key}`, problems);
  });
  return problems;
}

/** The quoted strings of a one-line TOML list, or a throw naming what could not be read. */
function stringList(body: string, where: string): string[] {
  if (body.trim() === '') return [];
  return body.split(',').map((item) => {
    const m = /^\s*"([^"]*)"\s*$/.exec(item);
    if (m === null) throw new Error(`${where}: not a list of quoted strings`);
    return m[1];
  });
}

/** The integers of a one-line TOML list, as strings of digits, or a throw. */
function issueList(body: string, where: string): string[] {
  if (body.trim() === '') return [];
  return body.split(',').map((item) => {
    const n = item.trim();
    if (!/^[0-9]+$/.test(n)) throw new Error(`${where}: not a list of issue numbers`);
    return String(Number(n));
  });
}

/**
 * The features a features file names: each `[[features]]` table's `id`,
 * `title`, `routes`, `epics` and `issues` — the shapes `load_features` in
 * `report_provenance.py` accepts, as the one-line form the committed files
 * use. A key this cannot read whole (a list over several lines, an issue that
 * is not an integer) and a feature missing its id or title are errors, so a
 * file this cannot read never yields a vocabulary that is quietly wrong.
 */
export function featureVocab(toml: string): FeatureVocab[] {
  const tables = toml.split(/^\[\[features\]\]\s*$/m).slice(1);
  return tables.map((table, i) => {
    const where = `features.toml: feature ${i + 1}`;
    const str = (key: string) => new RegExp(`^${key}\\s*=\\s*"([^"]*)"\\s*$`, 'm').exec(table)?.[1];
    const list = (key: string): string | null => {
      if (!new RegExp(`^${key}\\s*=`, 'm').test(table)) return null;
      const body = new RegExp(`^${key}\\s*=\\s*\\[(.*)\\]\\s*$`, 'm').exec(table)?.[1];
      if (body === undefined) throw new Error(`${where}: ${key} is not a one-line list`);
      return body;
    };
    const id = str('id');
    const title = str('title');
    if (id === undefined || title === undefined) throw new Error(`${where} has no id or title`);
    return {
      id,
      title,
      routes: stringList(list('routes') ?? '', `${where}: routes`),
      epics: stringList(list('epics') ?? '', `${where}: epics`),
      issues: issueList(list('issues') ?? '', `${where}: issues`)
    };
  });
}
