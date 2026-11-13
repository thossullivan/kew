// #2542 — the model picker's page model. AC2: the picker cannot emit an id
// that is not in its list.
import { describe, expect, it } from 'vitest';
import type { CatalogModel } from './api';
import {
  canonicalModel,
  groupByRunner,
  laneLabel,
  laneLine,
  lifecycleLabel,
  offeredModels,
  orderedIds,
  pickModel,
  priceLabel,
  routeDraft,
  runnerModels,
  shippedLaneLabel
} from './model-picker';
import modelsJson from '../../fixtures/factory/models.json';

const catalog = (modelsJson as { models: CatalogModel[] }).models;

const row = (over: Partial<CatalogModel> = {}): CatalogModel => ({
  id: 'claude-opus-5-5',
  display_name: 'Opus 5.5',
  provider: 'anthropic',
  runner: 'claude-code',
  price: { input_per_mtok: 5, output_per_mtok: 25 },
  lifecycle: { status: 'ok', shutdown: null, replacement: null },
  routable: true,
  routed_lanes: [],
  default_lanes: [],
  aliases: [],
  configured: false,
  ...over
});

describe('pickModel', () => {
  it('emits an id only when the list holds it', () => {
    expect(pickModel(catalog, 'claude-opus-5-5')).toBe('claude-opus-5-5');
    for (const stray of ['opus', 'claude-opus-5-5 ', 'CLAUDE-OPUS-5-5', '', 'gpt-9', null, 7]) {
      expect(pickModel(catalog, stray), String(stray)).toBeNull();
    }
  });

  it('does not accept an alias or display name in place of the id', () => {
    const aliased = [row({ aliases: ['opus-latest'] })];
    expect(pickModel(aliased, 'opus-latest')).toBeNull();
    expect(pickModel(aliased, 'Opus 5.5')).toBeNull();
  });

  it('refuses everything from an empty list', () => {
    expect(pickModel([], 'claude-opus-5-5')).toBeNull();
  });
});

describe('offeredModels', () => {
  const retired = row({
    id: 'claude-opus-4',
    lifecycle: { status: 'retired', shutdown: '2026-01-05', replacement: 'claude-opus-5-5' },
    configured: true
  });

  it('offers a retired model only as the value being edited', () => {
    expect(offeredModels([row(), retired], 'claude-opus-4').map((m) => m.id)).toContain(
      'claude-opus-4'
    );
    expect(offeredModels([row(), retired], 'claude-opus-5-5').map((m) => m.id)).toEqual([
      'claude-opus-5-5'
    ]);
  });

  it('offers a retired model that is the one in force (#2895)', () => {
    expect(offeredModels([row(), retired], '', 'claude-opus-4').map((m) => m.id)).toEqual([
      'claude-opus-5-5',
      'claude-opus-4'
    ]);
  });

  it('flags the retired row it does offer', () => {
    expect(lifecycleLabel(retired)).toBe(
      'retired — still configured · shuts down 2026-01-05 · use claude-opus-5-5'
    );
    expect(lifecycleLabel(row())).toBe('');
  });
});

describe('grouping and labels', () => {
  it('groups the catalog by runner without dropping or adding a row', () => {
    const groups = groupByRunner(catalog);
    expect(groups.map((g) => g.runner)).toEqual([...new Set(catalog.map((m) => m.runner))]);
    expect(orderedIds(groups).sort()).toEqual(catalog.map((m) => m.id).sort());
  });

  it("marks the project's routed lanes, never kew's shipped default, as the lane default", () => {
    expect(laneLabel(row({ routed_lanes: ['default', 'medium'] }))).toBe(
      'default, medium tiers'
    );
    // A shipped catalog default is not this project's routing.
    expect(laneLabel(row({ default_lanes: ['default'] }))).toBe('');
    expect(shippedLaneLabel(row({ default_lanes: ['default'] }))).toBe(
      'kew default for the default tier'
    );
    expect(shippedLaneLabel(row({ routed_lanes: ['default'] }))).toBe('');
    expect(laneLabel(row())).toBe('');
  });

  it('labels the fixture rows from the routing the fixture config names', () => {
    const byId = new Map(catalog.map((m) => [m.id, m]));
    expect(laneLabel(byId.get('claude-sonnet-4-6')!)).toBe('default, medium tiers');
    expect(laneLabel(byId.get('claude-opus-5')!)).toBe('complex tier');
    expect(laneLabel(byId.get('claude-opus-5-5')!)).toBe('');
  });

  it('says "default" once per meaning on one lane line (#3055)', () => {
    const byId = new Map(catalog.map((m) => [m.id, m]));
    const sonnet = byId.get('claude-sonnet-4-6')!;
    expect(sonnet.default_lanes).toEqual(['default']);
    expect(laneLine(sonnet)).toBe('kew default · default, medium tiers');
    expect(laneLine(row({ default_lanes: ['complex'], routed_lanes: ['simple'] }))).toBe(
      'kew default for the complex tier · simple tier'
    );
    expect(laneLine(row({ default_lanes: ['default'] }))).toBe('kew default for the default tier');
    expect(laneLine(row())).toBe('');
  });

  it('prints the price per Mtok', () => {
    expect(priceLabel(row())).toBe('$5 / $25 per Mtok');
    expect(priceLabel(row({ price: { input_per_mtok: 0.8, output_per_mtok: 4 } }))).toBe(
      '$0.80 / $4 per Mtok'
    );
    expect(priceLabel(row({ price: null }))).toBe('price not listed');
  });
});

describe('routeDraft', () => {
  it("takes the picked model's catalog runner", () => {
    expect(routeDraft(catalog, '', 'claude-opus-5-5')).toEqual({
      runner: 'claude-code',
      model: 'claude-opus-5-5'
    });
    expect(routeDraft(catalog, ' claude-code ', 'claude-opus-5-5')).toEqual({
      runner: 'claude-code',
      model: 'claude-opus-5-5'
    });
  });

  it('refuses a runner edited away from the picked model', () => {
    expect(() => routeDraft(catalog, 'codex', 'claude-opus-5-5')).toThrow(
      'claude-opus-5-5 runs on claude-code, not codex.'
    );
  });

  it('refuses a model the catalog does not list', () => {
    expect(() => routeDraft(catalog, 'claude-code', 'opus')).toThrow(
      'Pick a model from the catalog.'
    );
  });

  it('keeps a runner-only route, and refuses an empty one', () => {
    expect(routeDraft(catalog, 'aider', '')).toEqual({ runner: 'aider', model: '' });
    expect(() => routeDraft(catalog, ' ', '')).toThrow('needs a runner, a model, or both');
  });
});

describe('runnerModels (#2741)', () => {
  it("offers one runner's routable models only", () => {
    const codex = runnerModels(catalog, 'codex').map((m) => m.id);
    expect(codex).toEqual(['gpt-5.6-luna', 'gpt-5.6-sol']);
    const claude = runnerModels(catalog, 'claude-code');
    expect(claude.every((m) => m.runner === 'claude-code' && m.routable)).toBe(true);
    expect(claude.map((m) => m.id)).not.toContain('claude-opus-4-7');
  });

  it('keeps the value in force on the list even when it is not routable', () => {
    const kept = runnerModels(catalog, 'claude-code', 'claude-opus-4-7').map((m) => m.id);
    expect(kept).toContain('claude-opus-4-7');
  });
});

describe('canonicalModel', () => {
  const aliased = row({ aliases: ['opus-latest'] });

  it('names the catalog row an alias points at (#2895)', () => {
    expect(canonicalModel([aliased], 'opus-latest')).toBe('claude-opus-5-5');
  });

  it('keeps a catalog id, and an id the catalog does not hold', () => {
    expect(canonicalModel([aliased], 'claude-opus-5-5')).toBe('claude-opus-5-5');
    expect(canonicalModel([aliased], 'gpt-7')).toBe('gpt-7');
    expect(canonicalModel([aliased], '')).toBe('');
  });
});
