import { describe, expect, it, vi } from 'vitest';
import type { ConfigField, OverridableMap } from './api';
import {
  FIELD_META,
  SECTION_ORDER,
  SOURCE_TONE_CLASS,
  buildSections,
  buildRow,
  deriveSource,
  sourceBadge,
  formatValue,
  isMoneyField,
  isRoutingField,
  isRouteValue,
  buildRouteValue,
  parseNumeric,
  buildSetRequest,
  buildRevertRequest,
  setOverride,
  revertOverride,
  applyOverride,
  applyRevert,
  fetchReviewPolicy,
  buildReviewRows,
  buildReviewSetRequest,
  setReviewOverride,
  revertReviewOverride,
  reviewSourceBadge,
  generationNote,
  capacityNote,
  reversionNote,
  approverNote,
  type ReviewPolicy,
  type Source
} from './config-page';

/** A fake fetch that records calls and returns a canned Response. */
function fakeFetch(body: unknown, init: ResponseInit = {}) {
  const spy = vi.fn(async () => new Response(JSON.stringify(body), init));
  return spy as unknown as typeof fetch & ReturnType<typeof vi.fn>;
}
function lastCall(spy: unknown): [string, RequestInit] {
  const calls = (spy as ReturnType<typeof vi.fn>).mock.calls;
  return calls[calls.length - 1] as [string, RequestInit];
}

/** Build a ConfigField resolution with sensible defaults. */
function field(over: Partial<ConfigField>): ConfigField {
  return {
    effective: 5,
    file_value: 5,
    default_value: 5,
    override: null,
    ...over
  };
}

describe('deriveSource — one badge per layer', () => {
  it('override active → override', () => {
    const f = field({ effective: 9, override: { value: 9, set_by: 'ann', set_at: 't', reason: 'r' } });
    expect(deriveSource(f)).toBe<Source>('override');
  });

  it('override present but shadowed by a different effective → env-hint', () => {
    // env > override in precedence; the only detectable env case is this shadow.
    const f = field({ effective: 12, override: { value: 9, set_by: 'ann', set_at: 't', reason: 'r' } });
    expect(deriveSource(f)).toBe<Source>('env-hint');
  });

  it('no override, file differs from default → kew.toml', () => {
    const f = field({ effective: 7, file_value: 7, default_value: 5 });
    expect(deriveSource(f)).toBe<Source>('kew.toml');
  });

  it('no override, file equals default → default', () => {
    const f = field({ effective: 5, file_value: 5, default_value: 5 });
    expect(deriveSource(f)).toBe<Source>('default');
  });

  it('routing dict values compare structurally (override matches → override)', () => {
    const route = { runner: 'claude-code', model: 'opus' };
    const f = field({
      effective: { ...route },
      file_value: { runner: 'claude-code', model: '' },
      default_value: { runner: '', model: '' },
      override: { value: { ...route }, set_by: 'ann', set_at: 't', reason: 'r' }
    });
    expect(deriveSource(f)).toBe<Source>('override');
  });

  it('colours each tone distinctly for the factory word (FUI-28)', () => {
    const classes = (['override', 'env-hint', 'kew.toml', 'default'] as const).map(
      (s) => SOURCE_TONE_CLASS[sourceBadge(s).tone]
    );
    // Four layers, four colours: a word is all the badge is now, so the colour
    // is the only thing left carrying which layer won.
    expect(new Set(classes).size).toBe(4);
    expect(SOURCE_TONE_CLASS[sourceBadge('override').tone]).toBe('text-warn');
  });

  it('badge maps each source to a distinct tone', () => {
    expect(sourceBadge('override').tone).toBe('warn');
    expect(sourceBadge('env-hint').tone).toBe('info');
    expect(sourceBadge('kew.toml').tone).toBe('accent');
    expect(sourceBadge('default').tone).toBe('neutral');
  });
});

describe('buildSections — grouping', () => {
  function fullMap(): OverridableMap {
    return {
      budget: {
        'budget.per_issue_usd': field({}),
        'budget.daily_usd': field({}),
        'budget.weekly_usd': field({})
      },
      agent: {
        'agent.max_budget_usd': field({}),
        'agent.model': field({ effective: 'sonnet', file_value: 'sonnet', default_value: '' })
      },
      routing: {
        'routing.default': field({ effective: { runner: 'claude-code', model: '' } }),
        'routing.simple': field({ effective: { runner: 'claude-code', model: '' } }),
        'routing.medium': field({ effective: { runner: 'claude-code', model: '' } }),
        'routing.complex': field({ effective: { runner: 'claude-code', model: '' } })
      },
      breaker: {
        'breaker.cost_anomaly_factor': field({ effective: 4, file_value: 4, default_value: 4 })
      },
      review: {
        'review.model': field({ effective: '', file_value: '', default_value: '' })
      }
    };
  }

  it('groups into Budget / Model & Routing / Breaker in that order', () => {
    const sections = buildSections(fullMap());
    expect(sections.map((s) => s.title)).toEqual([...SECTION_ORDER]);
  });

  it('places the agent budget knob under Budget and agent.model under Model & Routing', () => {
    const sections = buildSections(fullMap());
    const budget = sections.find((s) => s.title === 'Budget')!;
    const modelRouting = sections.find((s) => s.title === 'Model & Routing')!;
    expect(budget.rows.map((r) => r.field)).toContain('agent.max_budget_usd');
    expect(modelRouting.rows.map((r) => r.field)).toContain('agent.model');
    expect(modelRouting.rows.map((r) => r.field)).toContain('routing.default');
  });

  it('tags routing rows with kind=route and number rows with kind=number', () => {
    const sections = buildSections(fullMap());
    const rows = sections.flatMap((s) => s.rows);
    expect(rows.find((r) => r.field === 'routing.simple')!.kind).toBe('route');
    expect(rows.find((r) => r.field === 'budget.daily_usd')!.kind).toBe('number');
    expect(rows.find((r) => r.field === 'agent.model')!.kind).toBe('model');
  });

  it('offers the reviewer model as a claude-code model row (#2741)', () => {
    const sections = buildSections(fullMap());
    const modelRouting = sections.find((s) => s.title === 'Model & Routing')!;
    const reviewer = modelRouting.rows.find((r) => r.field === 'review.model')!;
    expect(reviewer.label).toBe('Reviewer model');
    expect(reviewer.kind).toBe('model');
    expect(reviewer.runner).toBe('claude-code');
    expect(modelRouting.rows.find((r) => r.field === 'agent.model')!.runner).toBeUndefined();
  });

  it('skips fields absent from the payload and drops empty sections', () => {
    const sparse: OverridableMap = { budget: { 'budget.daily_usd': field({}) } };
    const sections = buildSections(sparse);
    expect(sections.map((s) => s.title)).toEqual(['Budget']);
    expect(sections[0].rows.map((r) => r.field)).toEqual(['budget.daily_usd']);
  });

  it('ignores API fields with no metadata entry (additivity)', () => {
    const map: OverridableMap = { budget: { 'budget.future_knob': field({}) } };
    expect(buildSections(map)).toEqual([]);
  });

  it('every metadata section is one of the ordered sections', () => {
    for (const m of FIELD_META) {
      expect(SECTION_ORDER).toContain(m.section);
    }
  });
});

describe('formatValue', () => {
  it('renders a route as runner · model, runner-only, or unset in words', () => {
    expect(formatValue('route', { runner: 'claude-code', model: 'opus' })).toBe('claude-code · opus');
    expect(formatValue('route', { runner: 'claude-code', model: '' })).toBe('claude-code');
    expect(formatValue('route', { runner: '', model: '' })).toBe('unset');
    expect(formatValue('route', null, 'routing.medium')).toBe('unset · uses the default route');
  });

  it('renders numbers, trimming trailing zeros only for non-integers', () => {
    expect(formatValue('number', 5)).toBe('5');
    expect(formatValue('number', 4.5)).toBe('4.50');
  });

  it('renders empty text as unset, never a bare dash (D42)', () => {
    expect(formatValue('text', '')).toBe('unset');
    expect(formatValue('text', 'sonnet')).toBe('sonnet');
  });

  it('says what applies when the reviewer model is unset (D42)', () => {
    expect(formatValue('model', '', 'review.model')).toBe('unset · uses the agent model');
    expect(formatValue('model', null, 'review.model')).toBe('unset · uses the agent model');
    expect(formatValue('model', 'claude-opus-5', 'review.model')).toBe('claude-opus-5');
  });

  it('gives every field an unset sentence, so no row prints a bare dash', () => {
    for (const m of FIELD_META) {
      const text = formatValue(m.kind, null, m.field);
      expect(text, m.field).toMatch(/^unset · \S/);
      expect(text, m.field).not.toContain('—');
    }
  });

  it('prints a budget cap as money (D42)', () => {
    expect(formatValue('number', 25, 'budget.per_issue_usd')).toBe('$25');
    expect(formatValue('number', 250, 'budget.daily_usd')).toBe('$250');
    expect(formatValue('number', 1500, 'budget.weekly_usd')).toBe('$1,500');
    expect(formatValue('number', 2.5, 'agent.max_budget_usd')).toBe('$2.50');
    expect(isMoneyField('budget.weekly_usd')).toBe(true);
    // The breaker's factor is a multiplier, not money.
    expect(formatValue('number', 3, 'breaker.cost_anomaly_factor')).toBe('3');
    expect(isMoneyField('breaker.cost_anomaly_factor')).toBe(false);
  });
});

describe('request building', () => {
  it('isRoutingField / isRouteValue guards', () => {
    expect(isRoutingField('routing.simple')).toBe(true);
    expect(isRoutingField('budget.daily_usd')).toBe(false);
    expect(isRouteValue({ runner: 'x', model: '' })).toBe(true);
    expect(isRouteValue('claude-code')).toBe(false);
    // Partial or wrongly-typed objects must never read as routes (review
    // finding: the old OR-check accepted {runner} alone).
    expect(isRouteValue({ runner: 'x' })).toBe(false);
    expect(isRouteValue({ model: 'y' })).toBe(false);
    expect(isRouteValue({ runner: 1, model: 'y' })).toBe(false);
    expect(isRouteValue(null)).toBe(false);
  });

  it('buildRouteValue trims and requires at least one field', () => {
    expect(buildRouteValue(' claude-code ', ' opus ')).toEqual({ runner: 'claude-code', model: 'opus' });
    expect(() => buildRouteValue('', '')).toThrow();
  });

  it('parseNumeric rejects blanks and non-numbers', () => {
    expect(parseNumeric('4.5')).toBe(4.5);
    expect(() => parseNumeric('')).toThrow();
    expect(() => parseNumeric('abc')).toThrow();
  });

  it('parseNumeric accepts a runtime number (bind:value on type=number)', () => {
    // The override dialog's numeric input yields a JS number at runtime; the
    // string-only version threw "e.trim is not a function" on every numeric
    // cap override (live 2026-07-19).
    expect(parseNumeric(100 as unknown as string)).toBe(100);
    expect(parseNumeric(0 as unknown as string)).toBe(0);
  });

  it('buildSetRequest builds a POST body against /api/config/{field}', () => {
    const req = buildSetRequest('budget.daily_usd', 12, 'raise it');
    expect(req).toEqual({
      path: '/api/config/budget.daily_usd',
      method: 'POST',
      body: { value: 12, reason: 'raise it' }
    });
  });

  it('buildSetRequest enforces the routing dict shape — rejects a bare string', () => {
    expect(() => buildSetRequest('routing.simple', 'claude-code')).toThrow(/footgun|runner/i);
  });

  it('buildSetRequest accepts a {runner, model} dict for routing', () => {
    const req = buildSetRequest('routing.simple', { runner: 'claude-code', model: 'haiku' });
    expect(req.body?.value).toEqual({ runner: 'claude-code', model: 'haiku' });
  });

  it('buildRevertRequest builds a DELETE with no body', () => {
    expect(buildRevertRequest('budget.daily_usd')).toEqual({
      path: '/api/config/budget.daily_usd',
      method: 'DELETE'
    });
  });
});

describe('setOverride / revertOverride — transport + write response', () => {
  it('POSTs value+reason and returns the write-response state', async () => {
    const f = fakeFetch({ field: 'budget.daily_usd', effective: 12 });
    const r = await setOverride('budget.daily_usd', 12, 'raise', f);
    expect(r.ok).toBe(true);
    if (r.ok) expect(r.data).toEqual({ field: 'budget.daily_usd', effective: 12 });
    const [path, init] = lastCall(f);
    expect(path).toBe('/api/config/budget.daily_usd');
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body as string)).toEqual({ value: 12, reason: 'raise' });
    expect((init.headers as Record<string, string>)['X-Kew-CSRF']).toBeDefined();
  });

  it('short-circuits a bad routing value to bad_value WITHOUT fetching', async () => {
    const f = fakeFetch({});
    const r = await setOverride('routing.simple', 'claude-code', '', f);
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.kind).toBe('bad_value');
    expect(f).not.toHaveBeenCalled();
  });

  it('maps 403 VerbDenied to a not_approver error with the inline explanation', async () => {
    const f = fakeFetch({ detail: "actor 'bob' is not in [approval].approvers" }, { status: 403 });
    const r = await setOverride('budget.daily_usd', 12, '', f);
    expect(r.ok).toBe(false);
    if (!r.ok) {
      expect(r.kind).toBe('not_approver');
      expect(r.status).toBe(403);
      expect(r.message).toMatch(/not an approver/i);
    }
  });

  it('maps 400 to bad_value carrying the server detail', async () => {
    const f = fakeFetch({ detail: 'value must be >= 1.0' }, { status: 400 });
    const r = await setOverride('breaker.cost_anomaly_factor', 0.5, '', f);
    expect(r.ok).toBe(false);
    if (!r.ok) {
      expect(r.kind).toBe('bad_value');
      expect(r.message).toBe('value must be >= 1.0');
    }
  });

  it('revertOverride DELETEs and returns the restored effective', async () => {
    const f = fakeFetch({ field: 'budget.daily_usd', effective: 5 });
    const r = await revertOverride('budget.daily_usd', f);
    expect(r.ok).toBe(true);
    if (r.ok) expect(r.data.effective).toBe(5);
    const [path, init] = lastCall(f);
    expect(path).toBe('/api/config/budget.daily_usd');
    expect(init.method).toBe('DELETE');
  });

  it('reports a thrown fetch as a network error', async () => {
    const f = vi.fn(async () => {
      throw new Error('offline');
    }) as unknown as typeof fetch;
    const r = await revertOverride('budget.daily_usd', f);
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.kind).toBe('network');
  });
});

describe('response-driven row transitions', () => {
  const baseRow = buildRow('budget.daily_usd', field({ effective: 5, file_value: 5, default_value: 5 }));

  it('applyOverride flips the row to an override sourced from the write response', () => {
    const next = applyOverride(baseRow, 12, { reason: 'raise', setBy: 'ann', setAt: '2026-07-17T00:00:00Z' });
    expect(next.resolution.effective).toBe(12);
    expect(next.resolution.override).toEqual({
      value: 12,
      reason: 'raise',
      set_by: 'ann',
      set_at: '2026-07-17T00:00:00Z'
    });
    expect(next.source).toBe<Source>('override');
    // Pure — original row untouched.
    expect(baseRow.resolution.override).toBeNull();
  });

  it('applyOverride keeps the submitted value so an env-shadowed write reads env-hint', () => {
    // Server resolves env > override: the submitted 12 is shadowed and
    // effective comes back 99. The row must label env-hint, not override.
    const next = applyOverride(baseRow, 99, { value: 12, reason: 'raise' });
    expect(next.resolution.effective).toBe(99);
    expect(next.resolution.override?.value).toBe(12);
    expect(next.source).toBe<Source>('env-hint');
    // Unshadowed: submitted === effective → override.
    const clean = applyOverride(baseRow, 12, { value: 12 });
    expect(clean.source).toBe<Source>('override');
  });

  it('applyRevert clears the override and recomputes the source from the restored value', () => {
    const overridden = applyOverride(baseRow, 12, { reason: 'raise' });
    const reverted = applyRevert(overridden, 5);
    expect(reverted.resolution.override).toBeNull();
    expect(reverted.resolution.effective).toBe(5);
    expect(reverted.source).toBe<Source>('default');
  });
});

// --- Human-review policy projection (I13W) ----------------------------------
// The whole contract is what GET/POST/DELETE return; these cases pin that the
// client PROJECTS it and derives none of it.

/** The ReviewPolicyState JSON shape, with overridable defaults. */
function policy(over: Partial<ReviewPolicy> = {}): ReviewPolicy {
  return {
    delivery_repo: 'octocat/kew',
    values: {
      'human_review.max_pending_reviews': 3,
      'human_review.max_diff_lines': 500,
      'human_review.max_paths': 20
    },
    generation: 0,
    expires_at: null,
    source: 'system',
    approvers: [],
    pending_reviews: 1,
    over_cap: false,
    pending_reversion: null,
    ...over
  };
}

describe('fetchReviewPolicy — the generated contract, verbatim', () => {
  it('GETs /api/config/human_review and returns the payload untouched', async () => {
    const body = policy({ generation: 4, source: 'override' });
    const f = fakeFetch(body);
    const r = await fetchReviewPolicy(f);
    expect(r.ok).toBe(true);
    if (r.ok) expect(r.data).toEqual(body);
    const [path, init] = lastCall(f);
    expect(path).toBe('/api/config/human_review');
    expect(init.method).toBe('GET');
  });

  it('surfaces the 400 a project with no canonical delivery repo gets', async () => {
    const f = fakeFetch({ detail: 'no canonical delivery repository' }, { status: 400 });
    const r = await fetchReviewPolicy(f);
    expect(r.ok).toBe(false);
    if (!r.ok) {
      expect(r.kind).toBe('bad_value');
      expect(r.message).toMatch(/delivery repository/);
    }
  });
});

describe('buildReviewRows — the three thresholds, as the server states them', () => {
  it('renders value + label in policy order', () => {
    const rows = buildReviewRows(policy({ values: { 'human_review.max_diff_lines': 900 } }));
    expect(rows.map((r) => r.field)).toEqual([
      'human_review.max_pending_reviews',
      'human_review.max_diff_lines',
      'human_review.max_paths'
    ]);
    expect(rows[1].value).toBe(900);
    expect(rows[1].display).toBe('900');
  });

  it('a threshold the server omits reads "not read" — never a client-side default', () => {
    const rows = buildReviewRows(policy({ values: {} }));
    expect(rows.every((r) => r.value === null)).toBe(true);
    expect(rows.every((r) => r.display === 'not read')).toBe(true);
  });
});

describe('the client derives no policy truth', () => {
  it('over_cap is the server bit — rendered even when pending is under the limit', () => {
    // Contradictory on purpose: a client recomputation would say "not over".
    const note = capacityNote(policy({ pending_reviews: 1, over_cap: true }));
    expect(note).not.toBeNull();
    expect(note).toMatch(/1/);
    expect(note).toMatch(/evict/i);
  });

  it('no over_cap bit → no over-cap note, even when pending exceeds the limit', () => {
    expect(capacityNote(policy({ pending_reviews: 9, over_cap: false }))).toBeNull();
  });

  it('a past expiry still reads as the override in force — the clock is the server’s', () => {
    const p = policy({ source: 'override', generation: 7, expires_at: '2020-01-01T00:00:00+00:00' });
    expect(reviewSourceBadge(p).label).toBe('override');
    expect(generationNote(p)).toMatch(/generation 7/);
    expect(generationNote(p)).toContain('2020-01-01T00:00:00+00:00');
  });

  it('renders the pending reversion the server computed, not one it infers', () => {
    const p = policy({
      source: 'override',
      generation: 7,
      expires_at: '2030-01-01T00:00:00+00:00',
      pending_reversion: {
        at: '2030-01-01T00:00:00+00:00',
        reason: 'expired',
        to: { 'human_review.max_pending_reviews': 3, generation: 8 }
      }
    });
    const note = reversionNote(p);
    expect(note).toContain('2030-01-01T00:00:00+00:00');
    expect(note).toContain('expired');
    expect(note).toMatch(/generation 8/);
    // An override with an expiry but no server-computed reversion renders none.
    expect(reversionNote({ ...p, pending_reversion: null })).toBeNull();
  });

  it('names terminal judgment separately from the config-write authority', () => {
    expect(approverNote(policy({ approvers: [] }))).toMatch(/\[approval\]\.approvers/);
    expect(approverNote(policy({ approvers: ['octocat'] }))).toMatch(/octocat/);
  });

  it('badges the source word the server sent, unknown words included', () => {
    expect(reviewSourceBadge(policy({ source: 'system' })).label).toBe('system');
    expect(reviewSourceBadge(policy({ source: 'future-layer' })).label).toBe('future-layer');
  });
});

describe('policy writes — expiry and the generation compare-and-swap', () => {
  it('omits expires_at / if_generation when the operator gave neither', () => {
    const req = buildReviewSetRequest('human_review.max_paths', 30, 'wide refactor');
    expect(req).toEqual({
      path: '/api/config/human_review.max_paths',
      method: 'POST',
      body: { value: 30, reason: 'wide refactor' }
    });
  });

  it('sends the UTC expiry and the generation the surface was showing', () => {
    const req = buildReviewSetRequest('human_review.max_diff_lines', 900, '', {
      expiresAt: ' 2030-01-01T00:00:00+00:00 ',
      ifGeneration: 4
    });
    expect(req.body).toEqual({
      value: 900,
      reason: '',
      expires_at: '2030-01-01T00:00:00+00:00',
      if_generation: 4
    });
  });

  it('refuses a field that is not one of the three thresholds', () => {
    expect(() => buildReviewSetRequest('human_review.approvers', ['x'])).toThrow(/threshold/i);
    expect(() => buildReviewSetRequest('budget.daily_usd', 5)).toThrow(/threshold/i);
  });

  it('POSTs and returns the whole new policy state', async () => {
    const state = { field: 'human_review.max_paths', effective: 30, ...policy({ generation: 5, source: 'override' }) };
    const f = fakeFetch(state);
    const r = await setReviewOverride('human_review.max_paths', 30, 'wide', {}, f);
    expect(r.ok).toBe(true);
    if (r.ok) {
      expect(r.data.generation).toBe(5);
      expect(r.data.source).toBe('override');
    }
    const [, init] = lastCall(f);
    expect((init.headers as Record<string, string>)['X-Kew-CSRF']).toBeDefined();
  });

  it('maps 409 to stale_generation carrying the server’s own wording', async () => {
    const f = fakeFetch({ detail: 'policy generation 3 is stale; 5 is in force' }, { status: 409 });
    const r = await setReviewOverride('human_review.max_paths', 30, '', { ifGeneration: 3 }, f);
    expect(r.ok).toBe(false);
    if (!r.ok) {
      expect(r.kind).toBe('stale_generation');
      expect(r.status).toBe(409);
      expect(r.message).toBe('policy generation 3 is stale; 5 is in force');
    }
  });

  it('sends a past expiry rather than judging it — the refusal is the server’s', async () => {
    const f = fakeFetch({ detail: 'expires_at is already past — that override never applies' }, { status: 400 });
    const r = await setReviewOverride('human_review.max_paths', 30, '', { expiresAt: '2020-01-01T00:00:00+00:00' }, f);
    expect(f).toHaveBeenCalled();
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.message).toMatch(/already past/);
  });

  it('revertReviewOverride DELETEs the threshold and returns the reverted state', async () => {
    const state = { field: 'human_review.max_paths', effective: 20, ...policy({ generation: 6 }) };
    const f = fakeFetch(state);
    const r = await revertReviewOverride('human_review.max_paths', f);
    expect(r.ok).toBe(true);
    if (r.ok) expect(r.data.source).toBe('system');
    const [path, init] = lastCall(f);
    expect(path).toBe('/api/config/human_review.max_paths');
    expect(init.method).toBe('DELETE');
  });
});
