/**
 * THE evidence pack's figures, and the one way each is written on the page.
 *
 * Verbatim from the evidence pack generated on 2026-07-26 with:
 *
 *   kew report export --output <pack>.html --since 2026-07-01
 *
 * The pack file itself is kept in the maintainers' private archive, not in
 * this repository, because it records operator-local paths.
 *
 * Sections previously quoted two different packs (14 Jul to 20 Jul in Runners
 * and Surfaces, 19 Jul to 26 Jul in Receipts), so the same artifact showed
 * different totals in two places on one page. Everything reads from here now.
 * Regenerate the pack, update this file, and the whole page moves together.
 *
 * Components render the strings in SHOWN, never a number they format
 * themselves, and every element quoting one carries data-pack="<key>". The
 * build (scripts/page-claims.mjs) reads SHOWN from this file and refuses a
 * page where a marked figure differs from it, or where a figure the page
 * quotes has lost its mark. Plain JavaScript so that Node gate imports the
 * same strings the page renders.
 *
 * Deliberately omitted: `economics.spend_windows`. The cap is operator-editable
 * config, so a percentage of cap is not evidence of discipline in either
 * direction, and the current 7-day figure sits above 100%.
 */

export const PACK = {
  repo: 'thossullivan/kew',
  generatedAt: '2026-07-26T13:23Z',
  since: '1 Jul 2026',
  until: '26 Jul 2026',
  days: 25,
  totalSpend: 1412.672823,
  prsMerged: 211,
  costPerMergedPr: 6.695132,
  epics: 43,
  items: 304,
  /** Runs with no recorded model. Counted in the totals, so it is named. */
  unattributedRuns: 35,
  attestation: {
    events: 1853,
    chainOk: true,
    headHash: '2fadf9f1eec826a1440c22c1820ed8269745fcf707f77b7c07cea67db2e3f332',
    anchorsTotal: 13,
    anchorsMatched: 13
  }
};

/** @typedef {{ kind: string; runs: number; success: number; rate: number; cost: number }} RunKind */

/** @type {RunKind[]} */
export const RUN_KINDS = [
  { kind: 'dispatch', runs: 304, success: 261, rate: 0.8586, cost: 1103.664976 },
  { kind: 'review', runs: 59, success: 51, rate: 0.8644, cost: 151.59383 },
  { kind: 'adversarial_review', runs: 62, success: 46, rate: 0.7419, cost: 91.96236 },
  { kind: 'review_remediation', runs: 10, success: 10, rate: 1.0, cost: 50.885868 },
  { kind: 'planning', runs: 12, success: 12, rate: 1.0, cost: 14.565789 }
];

/**
 * @typedef {{
 *   model: string;
 *   runner: string;
 *   runs: number;
 *   opened: number;
 *   merged: number;
 *   mergeRate: number | null;
 *   spend: number;
 *   perPr: number | null;
 * }} ScoreRow
 * `runner` is which runner drove the model.
 */

/**
 * Ordered by spend. Low-n rows are kept rather than pruned; see `runs`.
 *
 * @type {ScoreRow[]}
 */
export const SCORECARD = [
  { model: 'claude-opus-4-8', runner: 'claude-code', runs: 299, opened: 159, merged: 159, mergeRate: 1.0, spend: 1128.319968, perPr: 7.096352 },
  { model: 'claude-opus-5', runner: 'claude-code', runs: 43, opened: 5, merged: 1, mergeRate: 0.2, spend: 169.509993, perPr: 169.509993 },
  { model: 'claude-sonnet-4-6', runner: 'claude-code', runs: 54, opened: 52, merged: 52, mergeRate: 1.0, spend: 110.890736, perPr: 2.132514 },
  { model: 'claude-opus-4-6', runner: 'claude-code', runs: 2, opened: 0, merged: 0, mergeRate: null, spend: 3.777813, perPr: null },
  { model: 'deepseek-v4-flash', runner: 'opencode', runs: 3, opened: 2, merged: 1, mergeRate: 0.5, spend: 0.174314, perPr: 0.174314 },
  { model: 'Qwen3-Coder-30B (local)', runner: 'lemonade', runs: 11, opened: 8, merged: 7, mergeRate: 0.875, spend: 0.0, perPr: 0.0 },
  { model: 'unknown', runner: 'n/a', runs: 35, opened: 0, merged: 0, mergeRate: null, spend: 0.0, perPr: null }
];

/** The run kinds' spend, which sums to the pack's total: each kind's share of it. */
const KIND_TOTAL = RUN_KINDS.reduce((sum, r) => sum + r.cost, 0);

/** The merged column of the scorecard, summed: each PR counted under the run that wrote it. */
const SCORECARD_MERGED = SCORECARD.reduce((sum, r) => sum + r.merged, 0);

/**
 * "1 Jul 2026" and "26 Jul 2026" as "1 to 26 Jul 2026"; across months or
 * years, both dates in full.
 *
 * @param {string} since
 * @param {string} until
 */
function windowOf(since, until) {
  const [d1, m1, y1] = since.split(' ');
  const [, m2, y2] = until.split(' ');
  if (y1 !== y2) return `${since} to ${until}`;
  if (m1 !== m2) return `${d1} ${m1} to ${until}`;
  return `${d1} to ${until}`;
}

/** Dollars as the page writes them: "$1,412.67". @param {number} n */
const usd = (n) =>
  `$${n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
/** @param {number | null} n */
const usdOrNa = (n) => (n === null ? 'n/a' : usd(n));
/** A share to one decimal place: "78.1%". @param {number} n */
const share = (n) => `${(n * 100).toFixed(1)}%`;

/**
 * Every pack figure the page quotes, as the page writes it.
 *
 * @typedef {{ cost: string; share: string }} ShownRunKind
 * @typedef {{ runs: string; opened: string; merged: string; spend: string; perPr: string }} ShownScore
 */
export const SHOWN = {
  /**
   * The pack's window, the one way the page writes it ("1 to 26 Jul 2026"),
   * from its own since and until fields. The pack's day count is not printed
   * beside it: 1 to 26 July counted inclusively is 26 days, and `days` says 25.
   */
  window: windowOf(PACK.since, PACK.until),
  prsMerged: String(PACK.prsMerged),
  costPerMergedPr: usd(PACK.costPerMergedPr),
  totalSpend: usd(PACK.totalSpend),
  epics: String(PACK.epics),
  items: String(PACK.items),
  unattributedRuns: String(PACK.unattributedRuns),
  /** The scorecard's merged column summed, set against `prsMerged` on the page. */
  scorecardMerged: String(SCORECARD_MERGED),
  /**
   * Each run kind's spend, and its share of the run kinds' total spend.
   * @type {Record<string, ShownRunKind>}
   */
  runKinds: Object.fromEntries(
    RUN_KINDS.map((r) => [r.kind, { cost: usd(r.cost), share: share(r.cost / KIND_TOTAL) }])
  ),
  /** @type {Record<string, ShownScore>} */
  scorecard: Object.fromEntries(
    SCORECARD.map((r) => [
      r.model,
      {
        runs: String(r.runs),
        opened: String(r.opened),
        merged: String(r.merged),
        spend: usd(r.spend),
        perPr: usdOrNa(r.perPr)
      }
    ])
  )
};

/** A run kind as the page names it: "adversarial review". @param {string} kind */
export const kindLabel = (kind) => kind.replaceAll('_', ' ');

/** The run kinds' spend as a sentence, for the spend bar's accessible name. */
export const SPEND_LABEL = `Spend by run kind: ${RUN_KINDS.map(
  (r) => `${kindLabel(r.kind)} ${SHOWN.runKinds[r.kind].cost}, ${SHOWN.runKinds[r.kind].share}`
).join('; ')}`;

/**
 * SHOWN flattened to the keys the page marks: `prsMerged`,
 * `runKinds.dispatch.cost`, `scorecard.claude-opus-4-8.perPr`.
 *
 * @returns {Map<string, string>}
 */
export function packFigures() {
  /** @type {Map<string, string>} */
  const flat = new Map();
  /**
   * @param {string} prefix
   * @param {object} value
   */
  const walk = (prefix, value) => {
    for (const [key, inner] of Object.entries(value)) {
      const path = prefix ? `${prefix}.${key}` : key;
      if (typeof inner === 'string') flat.set(path, inner);
      else walk(path, inner);
    }
  };
  walk('', SHOWN);
  return flat;
}
