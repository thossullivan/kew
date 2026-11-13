/**
 * Derives the site's model lifecycle view from kew's model catalog.
 *
 * kew/src/kew/models.json is the single lifecycle source (#2709): each catalog
 * row's `lifecycle` carries the status, the shutdown and replacement facts and
 * the evidence behind them, and the catalog's top-level `reviewedAt` is the
 * date a human reviewed them. This script joins those rows against the
 * committed routing config and the catalog's OpenRouter-priced rows, derives
 * only `routed` and `priced`, and writes nothing else. There is no package, no network call, no
 * fallback, and no build-time date stamp, so the same committed inputs always
 * render byte-identical output.
 *
 * Selection is the same set kew's own `model-eol check` covers: every model id
 * the routing/agent/review config names, plus every OpenRouter-priced row
 * (`price_source: "openrouter"` with a price, written by `kew models refresh`). A selected id
 * with no catalog row is a hard failure, never a silently untracked row.
 *
 * This is the site build's read-only check. The one writer is kew's generator,
 * kew/scripts/model_catalog_docs.py, which renders this view and the model-eol
 * docs list from the same catalog (#2710); its pytest names the same command.
 *
 *   node scripts/fetch-model-lifecycle.mjs            read-only check (default)
 *   node scripts/fetch-model-lifecycle.mjs --check    read-only check
 */
import { readFileSync } from 'node:fs';
import { dirname, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const WWW = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const ROOT = resolve(WWW, '..');
const CATALOG = resolve(ROOT, 'kew/src/kew/models.json');
const OUT = resolve(WWW, 'src/lib/data/model-lifecycle.json');
const CATALOG_SCHEMA = 'kew/models@1';
const VIEW_SCHEMA = 'kew/model-lifecycle-view@1';
const KNOWN_FLAGS = new Set(['--check', '--quiet']);
const REGENERATE = 'cd kew && uv run python scripts/model_catalog_docs.py';
// Keep this in sync with _ALLOWED_EVIDENCE_KINDS in model_eol_snapshot.py.
const ALLOWED_EVIDENCE_KINDS = new Set([
  'provider-lifecycle-doc',
  'provider-announcement',
  'provider-catalog',
  'router-catalog'
]);

const argv = process.argv.slice(2);
const unknown = argv.filter((arg) => !KNOWN_FLAGS.has(arg));
if (unknown.length) {
  console.error(`unknown option(s): ${unknown.join(' ')}; use --check or --quiet`);
  process.exit(2);
}
const quiet = argv.includes('--quiet');

function rel(path) {
  return relative(ROOT, path);
}

function modelValues(source) {
  return [...source.matchAll(/^\s*[^#\n]*?\bmodel\s*=\s*"([^"]+)"/gm)].map((match) => match[1]);
}

function pricedValues(doc) {
  const rows = Array.isArray(doc?.models) ? doc.models : [];
  return rows.filter((row) => row?.price_source === 'openrouter' && row.price).map((row) => row.id);
}

/** Model ids the committed config selects, split by why they were selected. */
function selection() {
  const routed = new Set(modelValues(readFileSync(resolve(ROOT, 'kew.toml'), 'utf8')));
  const priced = new Set(pricedValues(JSON.parse(readFileSync(CATALOG, 'utf8'))));
  const ids = [...new Set([...routed, ...priced])].sort();
  if (!ids.length) throw new Error('no routed or priced model ids in the committed config and catalog');
  return { ids, routed, priced };
}

function text(value, what) {
  if (typeof value !== 'string' || !value) throw new Error(`${what} must be a nonempty string`);
  return value;
}

function optionalText(value, what) {
  return value === null || value === undefined ? null : text(value, what);
}

function evidenceRecord(item, id) {
  const kind = text(item?.kind, `${id} evidence kind`);
  if (!ALLOWED_EVIDENCE_KINDS.has(kind)) {
    throw new Error(
      `${id} has unknown evidence kind ${JSON.stringify(kind)}; allowed kinds: ` +
        `${[...ALLOWED_EVIDENCE_KINDS].join(', ')}`
    );
  }
  return {
    kind,
    url: text(item?.url, `${id} evidence url`),
    checkedAt: text(item?.checkedAt, `${id} evidence checkedAt`)
  };
}

/** The catalog's lifecycle rows, parsed strictly enough that drift cannot pass silently. */
function catalog() {
  const doc = JSON.parse(readFileSync(CATALOG, 'utf8'));
  if (doc.schema !== CATALOG_SCHEMA) {
    throw new Error(`${rel(CATALOG)} schema must be ${CATALOG_SCHEMA}, got ${doc.schema}`);
  }
  const reviewedAt = text(doc.reviewedAt, `${rel(CATALOG)} reviewedAt`);
  if (!Array.isArray(doc.models) || !doc.models.length) {
    throw new Error(`${rel(CATALOG)} needs a nonempty models array`);
  }
  const rows = new Map();
  for (const row of doc.models) {
    const id = text(row?.id, `${rel(CATALOG)} model id`);
    if (rows.has(id)) throw new Error(`${rel(CATALOG)} has a duplicate row for ${id}`);
    const lifecycle = row.lifecycle;
    if (!lifecycle || typeof lifecycle !== 'object') throw new Error(`${id} needs a lifecycle object`);
    if (!Array.isArray(lifecycle.evidence)) throw new Error(`${id} lifecycle evidence must be a list`);
    rows.set(id, {
      provider: text(row.provider, `${id} provider`),
      status: text(lifecycle.status, `${id} status`),
      shutdown: optionalText(lifecycle.shutdown, `${id} shutdown`),
      replacement: optionalText(lifecycle.replacement, `${id} replacement`),
      evidence: lifecycle.evidence.map((item) => evidenceRecord(item, id))
    });
  }
  return { reviewedAt, rows };
}

function view() {
  const selected = selection();
  const curated = catalog();
  const missing = selected.ids.filter((id) => !curated.rows.has(id));
  if (missing.length) {
    throw new Error(
      `${rel(CATALOG)} has no row for ${missing.length} selected model id(s): ` +
        `${missing.join(', ')}. Add the row before rebuilding the site view.`
    );
  }
  const unevidenced = selected.ids.filter((id) => !curated.rows.get(id).evidence.length);
  if (unevidenced.length) {
    throw new Error(`selected model id(s) with no lifecycle evidence: ${unevidenced.join(', ')}`);
  }
  return {
    schema: VIEW_SCHEMA,
    reviewedAt: curated.reviewedAt,
    models: selected.ids.map((id) => {
      const row = curated.rows.get(id);
      return {
        id,
        provider: row.provider,
        status: row.status,
        shutdown: row.shutdown,
        replacement: row.replacement,
        evidence: row.evidence,
        routed: selected.routed.has(id),
        priced: selected.priced.has(id)
      };
    })
  };
}

function main() {
  const data = view();
  const rendered = JSON.stringify(data, null, 2) + '\n';
  let committed;
  try {
    committed = readFileSync(OUT, 'utf8');
  } catch {
    throw new Error(`${rel(OUT)} is missing; run \`${REGENERATE}\``);
  }
  if (committed !== rendered) {
    throw new Error(
      `${rel(OUT)} does not match the model catalog; run \`${REGENERATE}\` and commit the result`
    );
  }
  if (!quiet) {
    console.log(`model lifecycle: ${rel(OUT)} matches the model catalog (${data.models.length} model(s))`);
  }
}

try {
  main();
} catch (err) {
  console.error(`model lifecycle: ${err.message}`);
  process.exit(1);
}
