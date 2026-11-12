/**
 * "How this was built" (#3144): what the demo's page foot says about the
 * surface it sits under, read from the build history `kew report provenance`
 * exported (`demo/provenance-bundle.ts` ships it, already checked against its
 * allowlist schema).
 *
 * A surface is matched by its SvelteKit route id with route groups dropped:
 * `/(factory)/epics/[ref]` is the `/epics/[ref]` that `features.toml` names.
 * A route can belong to more than one feature (`/work` is both Work and Needs
 * you), so the foot reads every feature that names it: the one-line summary
 * adds them up, counting a pull request once however many features name it,
 * and the ledger draws one table per feature.
 */

export type ProvenancePr = { url: string; landed_sha: string | null };

export type ProvenanceFeature = {
  id: string;
  title: string;
  routes: string[];
  epics: string[];
  issues: string[];
  prs: ProvenancePr[];
  runs: Record<string, number>;
  list_cost_usd: number;
  review_rounds: Record<string, number>;
  held_out: { items_failed: number; tests_failed: number; tests_undeclared: number };
  integrity: { runs_flagged: number; flags: Record<string, number> };
};

export type Provenance = {
  schema: string;
  as_of: string | null;
  integrity_source: 'corpus' | 'none';
  features: ProvenanceFeature[];
};

/** Where the demo build serves the checked `provenance.json` (`PROVENANCE_ASSET`). */
export const PROVENANCE_URL = '/demo-provenance.json';

/** The disclosure's toggle, on every surface that has one: it names what opens. */
export const BUILD_HISTORY_LABEL = 'Build history';

/** What the expanded view says about the cost figures. */
export const LIST_PRICE_NOTE =
  'Costs are API-equivalent list price: what the tokens would cost at the provider’s published rates, not what was billed.';

/** A route id as `features.toml` writes it: route groups dropped. */
export function surfaceRoute(routeId: string): string {
  const route = routeId
    .split('/')
    .filter((segment) => !/^\(.+\)$/.test(segment))
    .join('/');
  return route === '' ? '/' : route;
}

/** Whether `routes` (as `features.toml` writes them) hold the route `routeId`. */
export function namesRoute(routes: readonly string[], routeId: string | null): boolean {
  return routeId !== null && routes.includes(surfaceRoute(routeId));
}

/** The features that name the route `routeId`, in the file's order. */
export function featuresFor(doc: Provenance, routeId: string | null): ProvenanceFeature[] {
  if (routeId === null) return [];
  const route = surfaceRoute(routeId);
  return doc.features.filter((feature) => feature.routes.includes(route));
}

const sum = (counts: Record<string, number>) => Object.values(counts).reduce((a, b) => a + b, 0);
const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;

/** A list-price figure as the ledger prints it. */
export const usd = (amount: number) => `$${amount.toFixed(2)}`;

/** The figures the one-line summary reads. */
export function buildTotals(features: readonly ProvenanceFeature[]): { runs: number; prs: number; cost: number } {
  return {
    runs: features.reduce((n, f) => n + sum(f.runs), 0),
    prs: new Set(features.flatMap((f) => f.prs.map((pr) => pr.url))).size,
    cost: features.reduce((n, f) => n + f.list_cost_usd, 0)
  };
}

/** The collapsed line: "Built by N agent runs across M PRs, $X list cost." */
export function buildSummary(features: readonly ProvenanceFeature[]): string {
  const { runs, prs, cost } = buildTotals(features);
  return `Built by ${plural(runs, 'agent run', 'agent runs')} across ${plural(prs, 'PR', 'PRs')}, ${usd(cost)} list cost.`;
}

/** A recorded code, worded: `review_remediation` → `review remediation`. */
const worded = (code: string) => code.toLowerCase().replace(/_+/g, ' ');

/** A `{ code: count }` tally as `(word, count)` pairs, largest first, then by code. */
export function tallied(counts: Record<string, number>): Array<{ word: string; count: number }> {
  return Object.entries(counts)
    .filter(([, count]) => count > 0)
    .sort(([a, x], [b, y]) => y - x || a.localeCompare(b))
    .map(([code, count]) => ({ word: worded(code), count }));
}

/** The pull request number a GitHub PR URL ends in, as `#41`. */
export function prLabel(url: string): string {
  const number = /\/pull\/(\d+)\/?$/.exec(url)?.[1];
  return number === undefined ? url : `#${number}`;
}

/** The held-out row, as a sentence. */
export function heldOutLine(held: ProvenanceFeature['held_out']): string {
  if (held.tests_failed === 0 && held.items_failed === 0) return 'No held-out test failed.';
  const undeclared = held.tests_undeclared === 0 ? '' : `, ${held.tests_undeclared} undeclared`;
  return `${plural(held.tests_failed, 'test', 'tests')} failed on ${plural(held.items_failed, 'item', 'items')}${undeclared}.`;
}

/** The integrity row, as a sentence; `source` says whether any run report was read. */
export function integrityLine(integrity: ProvenanceFeature['integrity'], source: Provenance['integrity_source']): string {
  if (source === 'none') return 'Not read: the export had no run reports to check.';
  if (integrity.runs_flagged === 0) return 'No run flagged.';
  const flags = tallied(integrity.flags)
    .map(({ word, count }) => `${word} ${count}`)
    .join(', ');
  return `${plural(integrity.runs_flagged, 'run', 'runs')} flagged${flags === '' ? '' : `: ${flags}`}.`;
}
