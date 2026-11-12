/**
 * The build history the demo shows on each surface (#3144), bundled.
 *
 * `kew report provenance` writes `provenance.json` and `runs.json` into
 * `demo/provenance/` at release; neither is committed, and until they exist
 * the committed `*.sample.json` copies stand in. This plugin reads the real
 * pair when the real `provenance.json` is there, else the samples, refuses
 * either file the allowlist schema (`provenance-schema.ts`) finds a problem
 * in, and ships `provenance.json` as ONE file at a fixed path,
 * `PROVENANCE_ASSET`, which the page foot (`src/lib/demo/BuildHistory.svelte`)
 * reads straight from the site. It never goes through the `/api` shim.
 *
 * The real pair is read against `features.toml` and `PUBLIC_REPO`, the
 * samples against their own `provenance/features.sample.toml` and
 * `SAMPLE_REPO`: one rule, two ledgers, and neither passes as the other.
 *
 * One file at one path, not a JS chunk, because the leak scan names the
 * bundled copy by exact path (`leak-scan.allow.json`) to skip its denylist in
 * the `epics` the file carries by design, and a hashed chunk name could not
 * be named there. Only `provenance.json` ships: the page foot needs no run
 * row. Beside it, `virtual:kew-demo-provenance-meta` serves what the foot
 * must know before the file arrives: the routes it names, so a surface with
 * no history draws nothing, and which file shipped (`source`). The foot draws
 * every PR number as plain text, never a link: the real PRs are in kew's
 * private development repository, and the samples' are on an invented
 * owner's repository, someone else's URL space.
 */
import { existsSync, readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import type { Plugin } from 'vite';
import { type Ledger, PUBLIC_REPO, SAMPLE_REPO, featureVocab, provenanceProblems, runsProblems } from './provenance-schema';

/** Where the build-history file lands in `build-demo/`, and the URL the page foot reads. */
export const PROVENANCE_ASSET = 'demo-provenance.json';

/** The module that serves the routes the shipped file names, and which file it is. */
export const DEMO_PROVENANCE_META_ID = 'virtual:kew-demo-provenance-meta';
const RESOLVED_META = `\0${DEMO_PROVENANCE_META_ID}`;

/** Which file the demo shipped: the real export, or the committed samples. */
export type ProvenanceSource = 'real' | 'sample';

const DIR = new URL('./provenance/', import.meta.url);
const FEATURES = new URL('./features.toml', import.meta.url);
/** The samples' own features file, beside them: their epics and issues are invented. */
const SAMPLE_FEATURES = 'features.sample.toml';

/** The real file under `dir` when it exists, else its committed sample. */
export function provenanceFile(dir: URL, name: 'provenance' | 'runs'): URL {
  const real = new URL(`${name}.json`, dir);
  return existsSync(fileURLToPath(real)) ? real : new URL(`${name}.sample.json`, dir);
}

function readJson(file: URL): unknown {
  try {
    return JSON.parse(readFileSync(file, 'utf8'));
  } catch (error) {
    throw new Error(`demo provenance ${fileURLToPath(file)} is not JSON: ${(error as Error).message}`);
  }
}

/**
 * The `provenance.json` the demo ships, from `dir`, once it and its
 * `runs.json` pass the schema. The real pair is read only together: a real
 * `provenance.json` beside a sample `runs.json` would publish a file whose
 * runs were never checked.
 */
export function readProvenance(dir: URL = DIR, features: URL = FEATURES): unknown {
  return readBuildHistory(dir, features).doc;
}

/** The checked `provenance.json` from `dir`, and which file it is. */
export function readBuildHistory(dir: URL = DIR, features: URL = FEATURES): { doc: unknown; source: ProvenanceSource } {
  const provenance = provenanceFile(dir, 'provenance');
  const runs = provenanceFile(dir, 'runs');
  const isSample = provenance.pathname.endsWith('.sample.json');
  if (isSample !== runs.pathname.endsWith('.sample.json'))
    throw new Error(`demo provenance: ${fileURLToPath(dir)} holds one real file without the other`);
  const ledger: Ledger = isSample
    ? { features: featureVocab(readFileSync(new URL(SAMPLE_FEATURES, dir), 'utf8')), repo: SAMPLE_REPO }
    : { features: featureVocab(readFileSync(features, 'utf8')), repo: PUBLIC_REPO };
  const doc = readJson(provenance);
  const problems = [...provenanceProblems(doc, ledger), ...runsProblems(readJson(runs), ledger)];
  if (problems.length > 0)
    throw new Error(`demo provenance refused by its schema:\n  ${problems.slice(0, 20).join('\n  ')}`);
  return { doc, source: isSample ? 'sample' : 'real' };
}

/** Every route the checked file names, once each, in the file's order. */
function routesOf(doc: unknown): string[] {
  const features = (doc as { features: { routes: string[] }[] }).features;
  return [...new Set(features.flatMap((f) => f.routes))];
}

/** The Vite plugin that ships the checked `provenance.json` at `/${PROVENANCE_ASSET}`. */
export function demoProvenance(dir: URL = DIR, features: URL = FEATURES): Plugin {
  return {
    name: 'kew-demo-provenance',
    buildStart() {
      // Refuse before anything is bundled, not after.
      readProvenance(dir, features);
    },
    resolveId(id) {
      return id === DEMO_PROVENANCE_META_ID ? RESOLVED_META : null;
    },
    load(id) {
      if (id !== RESOLVED_META) return null;
      const { doc, source } = readBuildHistory(dir, features);
      return `export default ${JSON.stringify({ routes: routesOf(doc), source })};`;
    },
    generateBundle() {
      this.emitFile({ type: 'asset', fileName: PROVENANCE_ASSET, source: JSON.stringify(readProvenance(dir, features)) });
    },
    configureServer(server) {
      server.middlewares.use(`/${PROVENANCE_ASSET}`, (_req, res) => {
        res.setHeader('content-type', 'application/json');
        res.end(JSON.stringify(readProvenance(dir, features)));
      });
    }
  };
}
