// Page model for `ModelPicker.svelte` (#2542). Pure and vitest-covered: which
// catalog rows the picker offers, how they group, what each row says, and the
// one gate every emitted id passes through — `pickModel` returns an id only if
// the picker's own list holds it, so a model control cannot write free text.
//
// Nothing here names a model: the rows are `/api/models`, so a model added to
// the catalog appears with no UI change.

import type { CatalogModel } from './api';

export type ModelGroup = { runner: string; models: CatalogModel[] };

/**
 * The rows the picker offers. The endpoint already leaves out a retired model
 * no config names; this drops one again unless it is one of `kept` — the value
 * the control is editing, or the model in force (#2895) — so a retired row is
 * never offered as a fresh choice.
 */
export function offeredModels(
  models: readonly CatalogModel[],
  ...kept: string[]
): CatalogModel[] {
  return models.filter(
    (m) => m.lifecycle.status !== 'retired' || (m.configured && kept.includes(m.id))
  );
}

/**
 * The catalog id `id` names: itself, or the row that lists it as an alias, or
 * `id` unchanged when the catalog holds neither (#2895).
 */
export function canonicalModel(models: readonly CatalogModel[], id: string): string {
  if (id === '' || models.some((m) => m.id === id)) return id;
  return models.find((m) => m.aliases.includes(id))?.id ?? id;
}

/**
 * The rows a control bound to one runner may offer (#2741): that runner's
 * routable models, plus `current` so the value in force is still shown. The
 * server refuses anything else; this keeps the picker from offering it.
 */
export function runnerModels(
  models: readonly CatalogModel[],
  runner: string,
  current = ''
): CatalogModel[] {
  return models.filter((m) => m.runner === runner && (m.routable || m.id === current));
}

/** Grouped by runner, groups and rows in the order the catalog lists them. */
export function groupByRunner(models: readonly CatalogModel[]): ModelGroup[] {
  const groups: ModelGroup[] = [];
  for (const model of models) {
    let group = groups.find((g) => g.runner === model.runner);
    if (!group) {
      group = { runner: model.runner, models: [] };
      groups.push(group);
    }
    group.models.push(model);
  }
  return groups;
}

/** The ids in display order — the order arrow keys move through. */
export function orderedIds(groups: readonly ModelGroup[]): string[] {
  return groups.flatMap((g) => g.models.map((m) => m.id));
}

/**
 * The id to emit for `candidate`, or null when the list does not hold it. The
 * picker emits through this and nothing else.
 */
export function pickModel(models: readonly CatalogModel[], candidate: unknown): string | null {
  if (typeof candidate !== 'string') return null;
  return models.some((m) => m.id === candidate) ? candidate : null;
}

/**
 * The `{runner, model}` a route override writes. A model must be one the list
 * holds, and its runner is the catalog row's — a runner that differs is refused
 * the way `pickModel` refuses an unlisted id, so a picked model never travels
 * with a typed runner. A route with no model keeps its typed runner: the catalog
 * does not list every runner, and a runner-only route names no model.
 */
export function routeDraft(
  models: readonly CatalogModel[],
  runner: string,
  model: string
): { runner: string; model: string } {
  const r = runner.trim();
  const m = model.trim();
  if (m === '') {
    if (r === '') throw new Error('A routing override needs a runner, a model, or both.');
    return { runner: r, model: '' };
  }
  const id = pickModel(models, m);
  if (id === null) throw new Error('Pick a model from the catalog.');
  const row = models.find((x) => x.id === id)!;
  if (r !== '' && r !== row.runner) {
    throw new Error(`${id} runs on ${row.runner}, not ${r}.`);
  }
  return { runner: row.runner, model: id };
}

function usd(n: number): string {
  return Number.isInteger(n) ? `$${n}` : `$${n.toFixed(2)}`;
}

/** `$3 / $15 per Mtok`, or `price not listed` for a row with no price. */
export function priceLabel(model: CatalogModel): string {
  if (!model.price) return 'price not listed';
  return `${usd(model.price.input_per_mtok)} / ${usd(model.price.output_per_mtok)} per Mtok`;
}

/** `complex tier`, `default, medium tiers` — lanes named once, as tiers. */
function tiers(lanes: string[]): string {
  return `${lanes.join(', ')} ${lanes.length === 1 ? 'tier' : 'tiers'}`;
}

/**
 * `default, medium tiers` — the lanes this project's effective routing sends
 * to the model — or '' when it routes none there.
 */
export function laneLabel(model: CatalogModel): string {
  return model.routed_lanes.length ? tiers(model.routed_lanes) : '';
}

/**
 * `kew default` — the catalog's shipped lane default, worded apart from
 * `laneLabel` so it never reads as this project's routing — or ''. A lane the
 * row's routing already names is not named again (#3055: say it once); one it
 * does not is: `kew default for the complex tier`.
 */
export function shippedLaneLabel(model: CatalogModel): string {
  if (!model.default_lanes.length) return '';
  const unnamed = model.default_lanes.filter((lane) => !model.routed_lanes.includes(lane));
  return unnamed.length ? `kew default for the ${tiers(unnamed)}` : 'kew default';
}

/**
 * The row's one lane line (#3055): `kew default · default, medium tiers`, or
 * either half alone, or '' for a row no lane defaults to.
 */
export function laneLine(model: CatalogModel): string {
  return [shippedLaneLabel(model), laneLabel(model)].filter((part) => part !== '').join(' · ');
}

/**
 * The lifecycle words a row shows, or '' for a model with nothing to say
 * (`ok`, `untracked`). A retired row says it is still configured, because that
 * is the only reason it is on the list.
 */
export function lifecycleLabel(model: CatalogModel): string {
  const { status, shutdown, replacement } = model.lifecycle;
  if (status === 'ok' || status === 'untracked') return '';
  const parts = [status === 'retired' ? 'retired — still configured' : status];
  if (shutdown) parts.push(`shuts down ${shutdown}`);
  if (replacement) parts.push(`use ${replacement}`);
  return parts.join(' · ');
}
