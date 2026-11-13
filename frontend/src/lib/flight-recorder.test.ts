import { describe, expect, it } from 'vitest';
import type { Diff, FlightEvent, FlightTimeline, ProtectedPrompt, Run } from './api';
import {
  buildFlightView,
  canReveal,
  collapsePrompt,
  eventKindLabel,
  NO_ARTIFACT_RUN_KINDS,
  promptSummary,
  refusedPrompt,
  revealedPrompt,
  runHref,
  type FlightSources
} from './flight-recorder';

// --- fixtures ---------------------------------------------------------------
// Every fixture is a payload the canonical Flight contract can actually
// produce (kew/src/kew/service/models.py). Nothing here invents a field the
// server does not send: a view model that needs one would be a fourth source
// of truth, which is the thing this page exists to remove.

function run(over: Partial<Run> = {}): Run {
  return {
    run_id: 'r1',
    issue_num: 1525,
    title: 'restore the flight recorder',
    runner: 'claude-code',
    model: 'claude-opus-5',
    status: 'merged',
    cost_usd: 1.25,
    channel: 'hosted',
    started_at: '2026-08-18T10:00:00Z',
    finished_at: '2026-08-18T10:12:00Z',
    duration_seconds: 720,
    input_tokens: 1000,
    output_tokens: 200,
    cache_read_tokens: 4000,
    cache_creation_tokens: 500,
    diff_additions: 40,
    diff_deletions: 3,
    diff_files: 2,
    review_cycles: 1,
    pr_url: 'https://github.com/o/r/pull/9',
    pr_state: 'merged',
    attempt_number: 1,
    error_category: '',
    complexity: 'complex',
    branch: 'agent/issue-1525',
    // FUI-13 stage + the posture the DTO always sent, as serve projects them
    // for a governed reviewer's own run.
    stage: 'implement',
    stage_detail: 'PR #9',
    posture: 'sandboxed',
    filesystem_boundary: 'container',
    network_boundary: 'proxied',
    egress_verification_basis: null,
    credential_names: [],
    command_guard: 'allowlist',
    admission: 'admitted',
    reason: null,
    // C3 (#2300) — the recorded route and accounting columns.
    run_kind: 'dispatch',
    item_id: '1525',
    reported_model: null,
    provider: null,
    transport_mode: 'direct',
    planned_cost_usd: null,
    accounting_status: null,
    verdict: null,
    ...over
  };
}

function event(over: Partial<FlightEvent> = {}): FlightEvent {
  return {
    seq: 1,
    timestamp: '2026-08-18T10:00:00Z',
    kind: 'evidence.receipt_fact',
    origin: 'evidence',
    producer_kind: 'host',
    producer_name: 'kew',
    producer_version: '1.0.0',
    attested_by_host: true,
    sealed: true,
    terminal: false,
    facts: {},
    ...over
  };
}

const BASE_SHA = 'a'.repeat(40);
const TREE_SHA = 'b'.repeat(40);
const PACKAGE_DIGEST = 'c'.repeat(64);
const RECEIPT_DIGEST = 'd'.repeat(64);
const PROMPT_DIGEST = 'e'.repeat(64);

function trustedBaseEvent(value = BASE_SHA): FlightEvent {
  return event({
    seq: 2,
    kind: 'evidence.receipt_fact',
    facts: { fact: 'trusted_base_sha', value, source: 'origin/main' }
  });
}

function promptEvent(over: Record<string, unknown> = {}): FlightEvent {
  return event({
    seq: 1,
    kind: 'evidence.artifact',
    facts: {
      artifact_kind: 'compiled_prompt',
      digest: PROMPT_DIGEST,
      byte_length: 8123,
      storage: 'local_protected',
      ...over
    }
  });
}

function usageEvent(metric: string, value: number, unit: string): FlightEvent {
  return event({
    seq: 3,
    kind: 'evidence.runner_usage',
    producer_kind: 'runner',
    producer_name: 'claude-code',
    producer_version: '1.0.0',
    attested_by_host: false,
    sealed: false,
    facts: { metric, value, unit }
  });
}

function flight(over: Partial<FlightTimeline> = {}): FlightTimeline {
  return {
    run_id: 'r1',
    attempt_number: 1,
    item_id: '1525',
    issue_num: 1525,
    source_tracker: 'github',
    source_id: '1525',
    sealed: true,
    through_seq: 2,
    chain_head: 'f'.repeat(64),
    chain_verified: true,
    artifact_node_id: 'r1:2',
    runner_telemetry: 'structured',
    events: [promptEvent(), trustedBaseEvent()],
    gaps: [],
    availability: [
      { dimension: 'host_sequence', availability: 'available', provenance: 'observed', detail: '' },
      { dimension: 'artifact_seal', availability: 'available', provenance: 'observed', detail: '' },
      { dimension: 'artifact_diff', availability: 'available', provenance: 'observed', detail: '' },
      {
        dimension: 'diff_line_counts',
        availability: 'unavailable',
        provenance: 'unobserved',
        detail: 'the sealed package carries final content only'
      }
    ],
    ...over
  };
}

function diff(over: Partial<Diff> = {}): Diff {
  return {
    node_id: 'r1:2',
    text: `artifact ${PACKAGE_DIGEST}\nbase ${BASE_SHA}\n`,
    run_id: 'r1',
    attempt_number: 1,
    base_sha: BASE_SHA,
    tree_sha: TREE_SHA,
    package_digest: PACKAGE_DIGEST,
    receipt_digest: RECEIPT_DIGEST,
    verification: 'exact_tree',
    files: [
      {
        path: 'src/kew/serve/api_runs.py',
        change: 'changed',
        mode: '100644',
        digest: '1'.repeat(64),
        byte_length: 120,
        text: 'print("hi")\n',
        text_availability: 'available',
        detail: ''
      },
      {
        path: 'src/kew/gone.py',
        change: 'deleted',
        mode: '',
        digest: '',
        byte_length: 0,
        text: null,
        text_availability: 'unavailable',
        detail: 'the package records the removal, not the removed bytes'
      }
    ],
    availability: [
      { dimension: 'artifact_diff', availability: 'available', provenance: 'observed', detail: '' }
    ],
    ...over
  };
}

function sources(over: Partial<FlightSources> = {}): FlightSources {
  return { run: run(), flight: flight(), diff: diff(), ...over };
}

const NOW = new Date('2026-08-18T12:00:00Z');

// --- identity ---------------------------------------------------------------

describe('identity', () => {
  it('names the run, the attempt and the GitHub work item it was dispatched for', () => {
    const view = buildFlightView(sources(), NOW);
    expect(view.identity.runId).toBe('r1');
    expect(view.identity.attempt).toBe(1);
    expect(view.identity.itemRef).toBe('#1525');
    expect(view.identity.tracker).toBe('github');
    expect(view.identity.branch).toBe('agent/issue-1525');
    expect(view.identity.title).toBe('restore the flight recorder');
  });

  it('shows a Linear id bare and never synthesizes an issue number for it', () => {
    const view = buildFlightView(
      sources({
        run: run({ issue_num: 0, title: 'linear work' }),
        flight: flight({ item_id: 'SFD-12', issue_num: null, source_tracker: 'linear', source_id: 'SFD-12' })
      }),
      NOW
    );
    expect(view.identity.itemRef).toBe('SFD-12');
    expect(view.identity.tracker).toBe('linear');
    expect(view.identity.itemRef).not.toMatch(/#/);
  });

  it('deep-links to itself and out to the pull request when one is recorded', () => {
    const view = buildFlightView(sources(), NOW);
    expect(view.identity.selfHref).toBe('/runs/r1');
    expect(view.identity.deepLink?.href).toBe('https://github.com/o/r/pull/9');
  });

  it('offers no outbound deep link when nothing recorded one', () => {
    const view = buildFlightView(sources({ run: run({ pr_url: null }) }), NOW);
    expect(view.identity.deepLink).toBeNull();
  });

  // C10 (#3470): a served pr_url is a link only through safeHref.
  it.each([
    ['a javascript: URL', 'javascript:alert(1)'],
    ['a relative path', '/runs/r2'],
    ['an anchor', '#evidence']
  ])('builds no outbound deep link or safest link from %s', (_, pr_url) => {
    const view = buildFlightView(sources({ run: run({ pr_url }) }), NOW);
    expect(view.identity.deepLink).toBeNull();
    expect(view.safestLink?.href).not.toBe(pr_url);
    expect(view.safestLink?.external ?? false).toBe(false);
  });

  it('escapes a run id in the deep link it builds', () => {
    expect(runHref('r 1/2')).toBe('/runs/r%201%2F2');
  });
});

// --- route claim ------------------------------------------------------------

describe('route and claim', () => {
  it('separates the host-recorded route from what the runner reported about itself', () => {
    const view = buildFlightView(
      sources({ flight: flight({ events: [promptEvent(), trustedBaseEvent(), usageEvent('steps', 12, 'steps')] }) }),
      NOW
    );
    expect(view.route.claimed).toBe('claude-code / claude-opus-5');
    expect(view.route.claimedProvenance).toMatch(/host/i);
    expect(view.route.reported).toBe('claude-code 1.0.0');
    expect(view.route.reportedAvailable).toBe(true);
    // A runner's account of itself is a claim, never a host attestation.
    expect(view.route.attested).toBe(false);
  });

  it('reports the runner claim unavailable rather than echoing the route back', () => {
    const view = buildFlightView(sources({ flight: flight({ runner_telemetry: 'missing' }) }), NOW);
    expect(view.route.reportedAvailable).toBe(false);
    expect(view.route.reported).not.toContain('claude-code');
  });
});

// --- artifact lineage and exact-SHA checks ----------------------------------

describe('artifact lineage', () => {
  it('states every link the sealed change set is bound to', () => {
    const view = buildFlightView(sources(), NOW);
    const byLabel = Object.fromEntries(view.lineage.map((r) => [r.label, r]));
    expect(byLabel['base commit'].value).toBe(BASE_SHA);
    expect(byLabel['tree'].value).toBe(TREE_SHA);
    expect(byLabel['package digest'].value).toBe(PACKAGE_DIGEST);
    expect(byLabel['receipt digest'].value).toBe(RECEIPT_DIGEST);
    expect(byLabel['verification'].value).toBe('exact_tree');
    expect(byLabel['sealed through'].value).toBe('2');
    expect(byLabel['chain head'].value).toBe('f'.repeat(64));
  });

  it('marks lineage unavailable — not empty — when nothing is sealed', () => {
    const view = buildFlightView(
      sources({
        // No receipt means no seal, and every receipt-derived field with it.
        flight: flight({
          sealed: false,
          through_seq: null,
          chain_head: null,
          artifact_node_id: null
        }),
        diff: null
      }),
      NOW
    );
    for (const row of view.lineage) expect(row.available).toBe(false);
    expect(view.lineage.every((r) => r.value === '')).toBe(true);
  });
});

describe('exact-SHA checks', () => {
  const check = (view: ReturnType<typeof buildFlightView>, label: string) =>
    view.checks.find((c) => c.label === label)!;

  it('matches the host-attested trusted base against the sealed base', () => {
    const view = buildFlightView(sources(), NOW);
    const c = check(view, 'trusted base');
    expect(c.status).toBe('match');
    expect(c.expected).toBe(BASE_SHA);
    expect(c.actual).toBe(BASE_SHA);
  });

  it('flags a sealed base that is not the base the host attested at launch', () => {
    const view = buildFlightView(
      sources({ flight: flight({ events: [promptEvent(), trustedBaseEvent('9'.repeat(40))] }) }),
      NOW
    );
    expect(check(view, 'trusted base').status).toBe('mismatch');
  });

  it('says unavailable when no launch attested a base at all', () => {
    const view = buildFlightView(sources({ flight: flight({ events: [promptEvent()] }) }), NOW);
    expect(check(view, 'trusted base').status).toBe('unavailable');
  });

  it('fails the chain check when the ledger did not verify', () => {
    const view = buildFlightView(sources({ flight: flight({ chain_verified: false }) }), NOW);
    expect(check(view, 'evidence chain').status).toBe('mismatch');
  });

  it('fails the sealed-prefix check when an event inside the prefix is unsealed', () => {
    const view = buildFlightView(
      sources({
        flight: flight({ events: [promptEvent(), event({ seq: 2, sealed: false })] })
      }),
      NOW
    );
    expect(check(view, 'sealed prefix').status).toBe('mismatch');
  });

  it('accepts an exact-tree verification and rejects any other guarantee', () => {
    expect(check(buildFlightView(sources(), NOW), 'exact tree').status).toBe('match');
    const loose = buildFlightView(sources({ diff: diff({ verification: 'best_effort' }) }), NOW);
    expect(check(loose, 'exact tree').status).toBe('mismatch');
  });
});

// --- sealed diff ------------------------------------------------------------

describe('sealed diff', () => {
  it('projects the sealed package change set with its per-file text availability', () => {
    const view = buildFlightView(sources(), NOW);
    expect(view.diff.available).toBe(true);
    expect(view.diff.nodeId).toBe('r1:2');
    expect(view.diff.files.map((f) => f.path)).toEqual([
      'src/kew/serve/api_runs.py',
      'src/kew/gone.py'
    ]);
    expect(view.diff.files[1].textAvailability).toBe('unavailable');
    expect(view.diff.files[1].detail).toMatch(/removal/);
  });

  it('refuses a change set that is not bound to the artifact and says why', () => {
    const view = buildFlightView(
      sources({
        diff: null,
        flight: flight({
          availability: [
            {
              dimension: 'artifact_diff',
              availability: 'unavailable',
              provenance: 'error',
              detail: 'the preserved package could not be opened'
            }
          ]
        })
      }),
      NOW
    );
    expect(view.diff.available).toBe(false);
    expect(view.diff.detail).toBe('the preserved package could not be opened');
    expect(view.diff.files).toEqual([]);
  });
});

// --- cost provenance --------------------------------------------------------

describe('cost provenance', () => {
  it('prices the host figure and keeps runner-reported usage separately labelled', () => {
    const view = buildFlightView(
      sources({ flight: flight({ events: [promptEvent(), usageEvent('steps', 12, 'steps')] }) }),
      NOW
    );
    expect(view.cost.basis).toBe('host_priced');
    expect(view.cost.amount).toBe('$1.25');
    expect(view.cost.runnerReported).toEqual([
      { metric: 'steps', value: '12 steps', producer: 'claude-code 1.0.0', attested: false }
    ]);
  });

  it('calls a local run free rather than pricing it at zero', () => {
    const view = buildFlightView(
      sources({ run: run({ channel: 'local', cost_usd: 0, model: 'ollama/qwen' }) }),
      NOW
    );
    expect(view.cost.basis).toBe('free_local');
  });

  it('reports missing runner usage as unavailable, never as zero', () => {
    const view = buildFlightView(
      sources({
        flight: flight({
          runner_telemetry: 'missing',
          availability: [
            {
              dimension: 'runner_usage',
              availability: 'unavailable',
              provenance: 'unobserved',
              detail: 'the runner reported no usage'
            }
          ]
        })
      }),
      NOW
    );
    expect(view.cost.runnerReported).toEqual([]);
    expect(view.cost.detail).toBe('the runner reported no usage');
  });
});

describe('cost figures name their source', () => {
  it('chooses the pricing sentence by the basis, never the hosted one for every run', () => {
    const hosted = buildFlightView(sources(), NOW).cost;
    const local = buildFlightView(sources({ run: run({ channel: 'local', cost_usd: 0 }) }), NOW).cost;
    const unread = buildFlightView(sources({ run: null }), NOW).cost;
    expect(hosted.pricing).toBe("priced by kew from the host-metered usage at the provider's rates");
    expect(hosted.meta).toBe('host-priced');
    expect(local.pricing).toBe('a local model: kew meters its tokens and prices them at nothing');
    expect(local.meta).toBe('local · unpriced');
    expect(unread.pricing).toBe('no run row was read, so kew has no usage to price');
    expect(unread.meta).toBe('unavailable');
    expect(unread.amount).toBe('unavailable');
  });

  it('labels every figure with the source it came from', () => {
    const view = buildFlightView(
      sources({
        run: run({ planned_cost_usd: 2.8 }),
        flight: flight({ events: [promptEvent(), usageEvent('steps', 12, 'steps')] })
      }),
      NOW
    );
    expect(view.cost.figures).toEqual([
      { figure: 'input tokens', value: '1,000', available: true, source: 'host-metered' },
      { figure: 'output tokens', value: '200', available: true, source: 'host-metered' },
      { figure: 'cache read tokens', value: '4,000', available: true, source: 'host-metered' },
      { figure: 'cache write tokens', value: '500', available: true, source: 'host-metered' },
      { figure: 'steps', value: '12 steps', available: true, source: 'runner claim · claude-code 1.0.0' },
      { figure: 'planned cost', value: '$2.80', available: true, source: 'planned' }
    ]);
    expect(view.cost.runnerSilent).toBe('');
  });

  it('prints a null count as not recorded, never as 0', () => {
    const nulls = run({ planned_cost_usd: null });
    // The DTO types the counts as numbers; a row written before the meter
    // existed still answers null for them.
    Object.assign(nulls, { input_tokens: null, cache_creation_tokens: undefined });
    const figures = buildFlightView(sources({ run: nulls }), NOW).cost.figures;
    const input = figures.find((f) => f.figure === 'input tokens');
    const write = figures.find((f) => f.figure === 'cache write tokens');
    const planned = figures.find((f) => f.figure === 'planned cost');
    expect(input).toEqual({ figure: 'input tokens', value: 'not recorded', available: false, source: 'host-metered' });
    expect(write?.value).toBe('not recorded');
    expect(planned).toEqual({ figure: 'planned cost', value: 'not recorded', available: false, source: 'planned' });
    expect(figures.some((f) => f.value === '0')).toBe(false);
  });

  it('says once that the host meter is the only source when the runner reported nothing', () => {
    expect(buildFlightView(sources(), NOW).cost.runnerSilent).toBe(
      'the runner reported no usage; the host meter is the only source'
    );
    expect(buildFlightView(sources({ run: null }), NOW).cost.runnerSilent).toBe(
      'the runner reported no usage, and no host meter was read'
    );
  });

  it('names a host-attested runner figure as attested, not as a claim', () => {
    const attested = { ...usageEvent('steps', 3, 'steps'), attested_by_host: true };
    const figures = buildFlightView(
      sources({ flight: flight({ events: [attested] }) }),
      NOW
    ).cost.figures;
    expect(figures.find((f) => f.figure === 'steps')?.source).toBe('host-attested · claude-code 1.0.0');
  });
});

describe('an earlier attempt on the page', () => {
  it('names the attempt the run row speaks for when the link asked for another', () => {
    const view = buildFlightView(
      sources({ run: run({ attempt_number: 2 }), flight: flight({ attempt_number: 1 }), attempt: 1 }),
      NOW
    );
    expect(view.identity.attempt).toBe(1);
    expect(view.rowAttempt).toBe('attempt 2');
  });

  it('labels nothing when the run row is the attempt on the page', () => {
    expect(buildFlightView(sources({ attempt: 1 }), NOW).rowAttempt).toBe('');
    expect(buildFlightView(sources(), NOW).rowAttempt).toBe('');
  });

  it("labels the run row's cost figures with its attempt, and the runner's own figures with none", () => {
    const figures = buildFlightView(
      sources({
        run: run({ attempt_number: 2, planned_cost_usd: 2.8 }),
        flight: flight({ attempt_number: 1, events: [usageEvent('steps', 12, 'steps')] }),
        attempt: 1
      }),
      NOW
    ).cost.figures;
    const source = (figure: string) => figures.find((f) => f.figure === figure)?.source;
    expect(source('input tokens')).toBe('host-metered · attempt 2');
    expect(source('cache write tokens')).toBe('host-metered · attempt 2');
    expect(source('planned cost')).toBe('planned · attempt 2');
    expect(source('steps')).toBe('runner claim · claude-code 1.0.0');
  });
});

describe('runner metrics in the cost table', () => {
  it('prints each metric in words, never as its id', () => {
    const figures = buildFlightView(
      sources({
        flight: flight({
          events: [
            usageEvent('tool_uses', 40, 'calls'),
            usageEvent('steps_to_first_edit', 6, 'steps'),
            usageEvent('tokens_to_first_edit', 52000, 'tokens')
          ]
        })
      }),
      NOW
    ).cost.figures;
    const runner = figures.filter((f) => f.source.startsWith('runner claim'));
    expect(runner.map((f) => [f.figure, f.value])).toEqual([
      ['tool uses', '40 calls'],
      ['steps to first edit', '6 steps'],
      ['tokens to first edit', '52000 tokens']
    ]);
  });
});

describe('the shut Compiled prompt disclosure', () => {
  it('says whether a prompt was recorded and its size, never a status word', () => {
    const recorded = buildFlightView(sources({ flight: flight({ events: [promptEvent()] }) }), NOW).prompt;
    expect(promptSummary(recorded)).toBe('prompt recorded, 8.1 kB');
    const none = buildFlightView(sources({ flight: flight({ events: [] }) }), NOW).prompt;
    expect(promptSummary(none)).toBe('no prompt was recorded for this run');
    const purged = buildFlightView(
      sources({ flight: flight({ events: [promptEvent({ storage: 'unavailable', byte_length: 640 })] }) }),
      NOW
    ).prompt;
    expect(promptSummary(purged)).toBe('prompt recorded, 640 bytes; its bytes are no longer kept');
    expect(promptSummary({ ...recorded, status: 'revealed', text: 'x' })).toBe('prompt recorded, 8.1 kB, shown below');
    // A recorded prompt whose read came back not found keeps its digest: the
    // line says it was recorded and could not be read, never that none was.
    expect(promptSummary({ ...recorded, status: 'missing' })).toBe('prompt recorded, 8.1 kB; it could not be read');
    for (const panel of [recorded, none, purged]) {
      expect(promptSummary(panel)).not.toMatch(/\b(missing|collapsed|unavailable)\b/);
    }
  });
});

describe('the link beside a safest action with no command', () => {
  it('sends a sealed run with a pull request out to it', () => {
    expect(buildFlightView(sources(), NOW).safestLink).toEqual({
      href: 'https://github.com/o/r/pull/9',
      label: 'pull request',
      external: true
    });
  });

  it('points an unpublished sealed run at its change set on the page', () => {
    expect(buildFlightView(sources({ run: run({ pr_url: null }) }), NOW).safestLink).toEqual({
      href: '#changes',
      label: 'Changes',
      external: false
    });
  });

  it('points an unverified chain at the evidence, and an unknown run back to the list', () => {
    const chain = buildFlightView(sources({ flight: flight({ chain_verified: false }) }), NOW);
    expect(chain.safestLink?.href).toBe('#evidence');
    const unknown = buildFlightView({ runId: 'zz', run: null, flight: null, diff: null }, NOW);
    expect(unknown.safestLink).toEqual({ href: '/runs', label: 'Runs', external: false });
  });

  it("points a review run at its own verdict, never at a pull request it did not open", () => {
    const own = { round: 1, verdict: 'READY', head_sha: null, reviewer_model: null, ts: '', scope: 'run' };
    const review = run({ run_kind: 'adversarial_review', verdict: own as Run['verdict'] });
    expect(buildFlightView(sources({ run: review }), NOW).safestLink?.href).toBe('#review');
    const standing = run({ run_kind: 'adversarial_review', verdict: { ...own, scope: 'item' } as Run['verdict'] });
    expect(buildFlightView(sources({ run: standing }), NOW).safestLink).toBeNull();
  });

  it('offers no link where the action carries a command', () => {
    const view = buildFlightView(
      sources({
        run: run({ status: 'awaiting_publication', pr_url: null }),
        events: [
          {
            seq: 1,
            ts: '2026-08-18T10:10:00Z',
            event: 'publication_deferred',
            actor_type: 'policy',
            item_id: '1525',
            payload: { step: 'push' }
          }
        ]
      }),
      NOW
    );
    expect(view.safestAction.command).toBe('kew finalize r1');
    expect(view.safestLink).toBeNull();
  });
});

describe('evidence and change-set summaries', () => {
  it('sums the evidence up in one line a closed disclosure can show', () => {
    const readable = flight({ availability: flight().availability.slice(0, 3) });
    expect(buildFlightView(sources({ flight: readable }), NOW).evidenceSummary).toBe(
      'sealed · chain verified · 4/4 checks match · no gaps'
    );
    expect(buildFlightView(sources(), NOW).evidenceSummary).toBe(
      'sealed · chain verified · 4/4 checks match · 1 gap'
    );
    const broken = buildFlightView(
      sources({
        flight: flight({ chain_verified: false, sealed: false, gaps: [{ seq: 3, event: 'x', reason: 'y' }] })
      }),
      NOW
    );
    expect(broken.evidenceSummary).toBe('unsealed · chain unverified · 3/4 checks match · 2 gaps');
  });

  it('counts the sealed files, or says there is no change set', () => {
    expect(buildFlightView(sources(), NOW).diff.summary).toBe('2 files');
    expect(buildFlightView(sources({ diff: null }), NOW).diff.summary).toBe('no sealed change set');
  });

  it('names a host-sequence kind in words', () => {
    expect(eventKindLabel('evidence.runner_usage')).toBe('runner usage');
    expect(eventKindLabel('evidence.dispatch')).toBe('dispatch');
    expect(eventKindLabel('lifecycle')).toBe('lifecycle');
  });
});

// --- gaps -------------------------------------------------------------------

describe('evidence gaps', () => {
  it('lists unreadable ledger rows alongside every unavailable dimension', () => {
    const view = buildFlightView(
      sources({
        flight: flight({
          gaps: [{ seq: 7, event: 'evidence.artifact', reason: 'facts are not a mapping' }]
        })
      }),
      NOW
    );
    expect(view.gaps).toContainEqual({
      seq: 7,
      event: 'evidence.artifact',
      reason: 'facts are not a mapping',
      kind: 'gap'
    });
    expect(view.gaps).toContainEqual({
      seq: null,
      event: 'diff_line_counts',
      reason: 'the sealed package carries final content only',
      kind: 'dimension'
    });
  });

  it('is empty when every dimension was read', () => {
    const view = buildFlightView(
      sources({ flight: flight({ availability: [], gaps: [] }) }),
      NOW
    );
    expect(view.gaps).toEqual([]);
  });
});

// --- safest action ----------------------------------------------------------

describe('safest action', () => {
  it('sends a sealed, verified run with a PR to the pull request', () => {
    // An open one: a merged or closed one asks no review (design review c9).
    const view = buildFlightView(sources({ run: run({ pr_state: 'OPEN' }) }), NOW);
    expect(view.safestAction.action).toMatch(/pull request/i);
    expect(view.safestAction.blockedBy).toBeNull();
  });

  it('refuses to recommend anything but verification when the chain did not verify', () => {
    const view = buildFlightView(sources({ flight: flight({ chain_verified: false }) }), NOW);
    expect(view.safestAction.action).toMatch(/verify/i);
    expect(view.safestAction.blockedBy).toBe('host_sequence');
  });

  it('asks the operator to find out when the sealed diff cannot be read', () => {
    const view = buildFlightView(sources({ diff: null }), NOW);
    expect(view.safestAction.blockedBy).toBe('artifact_diff');
  });

  it('says nothing is sealed rather than inventing a review action', () => {
    const view = buildFlightView(
      sources({ flight: flight({ sealed: false, artifact_node_id: null }), diff: null }),
      NOW
    );
    expect(view.safestAction.action).toMatch(/wait|nothing/i);
    expect(view.safestAction.blockedBy).toBe('artifact_seal');
  });

  it('sends an unknown run back to the list', () => {
    const view = buildFlightView({ run: null, flight: null, diff: null }, NOW);
    expect(view.safestAction.action).toMatch(/runs list/i);
  });
});

describe('safest action from a publication deferral (#3057)', () => {
  const deferred = (seq: number, payload: Record<string, unknown>) => ({
    seq,
    ts: '2026-08-18T11:44:20+00:00',
    event: 'publication_deferred',
    actor_type: 'policy',
    item_id: '1525',
    payload
  });
  const DEFERRAL = deferred(1048, {
    category: 'push',
    step: 'push',
    error: 'protected branch; base merge required'
  });
  /** The run status `kew finalize` accepts: the deferral is still open. */
  const awaiting = run({ status: 'awaiting_publication', pr_url: null });

  it("derives the action, reason and command from the deferral's own facts", () => {
    const view = buildFlightView(sources({ run: awaiting, events: [DEFERRAL] }), NOW);
    expect(view.safestAction).toEqual({
      action: 'publish the preserved artifact this run already produced',
      reason: 'the host deferred publication at push: protected branch; base merge required',
      blockedBy: 'publication',
      command: 'kew finalize r1'
    });
  });

  it('wins over an unread sealed package, so the page never sends the reader there', () => {
    const view = buildFlightView(sources({ run: awaiting, diff: null, events: [DEFERRAL] }), NOW);
    expect(view.safestAction.action).not.toMatch(/sealed package/);
    expect(view.safestAction.command).toBe('kew finalize r1');
  });

  it('still yields to an evidence chain that did not verify', () => {
    const view = buildFlightView(
      sources({ run: awaiting, flight: flight({ chain_verified: false }), events: [DEFERRAL] }),
      NOW
    );
    expect(view.safestAction.blockedBy).toBe('host_sequence');
    expect(view.safestAction.command).toBe('');
  });

  it('stops standing once a later event settles the publication', () => {
    const settled = { ...DEFERRAL, seq: 1050, event: 'publication_finalized', payload: {} };
    const view = buildFlightView(sources({ run: awaiting, events: [settled, DEFERRAL] }), NOW);
    expect(view.safestAction.action).toMatch(/sealed change set/i);
    expect(view.safestAction.command).toBe('');
  });

  it('offers no finalize for a run whose status moved past the deferral', () => {
    // `kew finalize` refuses any run that is not awaiting publication, so a
    // success or merged run with its pull request open is never sent there —
    // the f5dcb6a5 fixture is exactly this run.
    for (const status of ['success', 'merged']) {
      const view = buildFlightView(sources({ run: run({ status, pr_state: 'OPEN' }), events: [DEFERRAL] }), NOW);
      expect(view.safestAction.command).toBe('');
      expect(view.safestAction.action).toMatch(/pull request/i);
      expect(view.outcome).toBe('');
    }
  });

  it("keeps a job-owned deferral standing, with no command, while nothing reached the forge", () => {
    // A normalized publication job leaves the run status alone and its state
    // is not on this page, so the deferral stands but no command is offered.
    const owned = deferred(1048, { step: 'push', error: 'rejected', job_id: 'job-1' });
    const view = buildFlightView(
      sources({ run: run({ status: 'success', pr_url: null }), events: [owned] }),
      NOW
    );
    expect(view.safestAction).toEqual({
      action: "find out whether this run's publication job still holds its work",
      reason: 'the host deferred publication at push: rejected',
      blockedBy: 'publication',
      command: ''
    });
    expect(view.outcome).toBe('Run succeeded · publication blocked');

    const published = buildFlightView(
      sources({ run: run({ status: 'success' }), events: [owned] }),
      NOW
    );
    expect(published.safestAction.blockedBy).toBeNull();
  });

  it('offers no command anywhere else', () => {
    expect(buildFlightView(sources(), NOW).safestAction.command).toBe('');
    expect(buildFlightView(sources({ diff: null }), NOW).safestAction.command).toBe('');
  });
});

describe('run outcome (#2541)', () => {
  it('names both outcomes when the run succeeded and its publication is blocked', () => {
    const view = buildFlightView(sources({ run: run({ status: 'success' }), diff: null }), NOW);
    expect(view.safestAction.blockedBy).toBe('artifact_diff');
    expect(view.outcome).toBe('Run succeeded · publication blocked');
  });

  it('says nothing extra when the run and its publication agree', () => {
    expect(buildFlightView(sources({ run: run({ status: 'success' }) }), NOW).outcome).toBe('');
    expect(
      buildFlightView(sources({ run: run({ status: 'failure' }), diff: null }), NOW).outcome
    ).toBe('');
  });
});

// --- protected prompt -------------------------------------------------------

describe('protected prompt panel', () => {
  it('starts collapsed with the digest-only description and no bytes', () => {
    const view = buildFlightView(sources(), NOW);
    expect(view.prompt.status).toBe('collapsed');
    expect(view.prompt.digest).toBe(PROMPT_DIGEST);
    expect(view.prompt.byteLength).toBe(8123);
    expect(view.prompt.text).toBeNull();
    expect(canReveal(view.prompt)).toBe(true);
    expect(JSON.stringify(view)).not.toMatch(/prompt_text|compiled prompt bytes/);
  });

  it('will not offer a reveal once the retention window has closed', () => {
    const view = buildFlightView(
      sources({
        flight: flight({ events: [promptEvent({ expires_at: '2026-08-18T11:00:00Z' })] })
      }),
      NOW
    );
    expect(view.prompt.status).toBe('unavailable');
    expect(view.prompt.expired).toBe(true);
    expect(canReveal(view.prompt)).toBe(false);
  });

  it('reports a run that retained no prompt as missing, not as empty', () => {
    const view = buildFlightView(sources({ flight: flight({ events: [trustedBaseEvent()] }) }), NOW);
    expect(view.prompt.status).toBe('missing');
    expect(canReveal(view.prompt)).toBe(false);
  });

  it('holds the bytes only after an explicit reveal answers with them', () => {
    const view = buildFlightView(sources(), NOW);
    const dto: ProtectedPrompt = {
      run_id: 'r1',
      attempt_number: 1,
      artifact_kind: 'compiled_prompt',
      digest: PROMPT_DIGEST,
      byte_length: 8123,
      storage: 'local_protected',
      availability: 'available',
      detail: '',
      created_at: '2026-08-18T10:00:00Z',
      terminal_at: null,
      expires_at: null,
      purged_at: null,
      purge_reason: '',
      text: 'EXACT PROMPT BYTES'
    };
    const revealed = revealedPrompt(view.prompt, dto);
    expect(revealed.status).toBe('revealed');
    expect(revealed.text).toBe('EXACT PROMPT BYTES');
    // The revealed panel is a new value: the collapsed one never gains bytes.
    expect(view.prompt.text).toBeNull();
  });

  it('keeps purge provenance when retention removed the bytes', () => {
    const view = buildFlightView(sources(), NOW);
    const revealed = revealedPrompt(view.prompt, {
      run_id: 'r1',
      attempt_number: 1,
      artifact_kind: 'compiled_prompt',
      digest: PROMPT_DIGEST,
      byte_length: 8123,
      storage: 'unavailable',
      availability: 'unavailable',
      detail: '',
      created_at: '2026-08-18T10:00:00Z',
      terminal_at: null,
      expires_at: null,
      purged_at: '2026-08-18T11:30:00Z',
      purge_reason: 'retention window closed',
      text: null
    });
    expect(revealed.status).toBe('unavailable');
    expect(revealed.text).toBeNull();
    expect(revealed.detail).toBe('retention window closed');
  });

  it('drops the bytes when the panel is collapsed again', () => {
    const view = buildFlightView(sources(), NOW);
    const revealed = { ...view.prompt, status: 'revealed' as const, text: 'EXACT PROMPT BYTES' };
    const collapsed = collapsePrompt(revealed);
    expect(collapsed.status).toBe('collapsed');
    expect(collapsed.text).toBeNull();
    expect(JSON.stringify(collapsed)).not.toContain('EXACT PROMPT BYTES');
  });

  it('explains each refusal the protected route can answer with', () => {
    const view = buildFlightView(sources(), NOW);
    const refuse = (status: number) =>
      refusedPrompt(view.prompt, new Error(`/api/runs/r1/prompt -> ${status}`));
    expect(refuse(403).status).toBe('refused');
    expect(refuse(403).detail).toMatch(/expose/i);
    expect(refuse(401).status).toBe('refused');
    expect(refuse(401).detail).toMatch(/session/i);
    expect(refuse(404).status).toBe('missing');
    expect(refuse(500).status).toBe('error');
    for (const s of [401, 403, 404, 500]) expect(refuse(s).text).toBeNull();
  });
});

// --- run kinds that produce no artifact --------------------------------------

describe('a run whose kind produces no artifact', () => {
  // Every run kind kew launches (kew/src/kew/run_store.py `KNOWN_RUN_KINDS`),
  // with whether it publishes. kew seals a receipt only on dispatch_core's
  // path; the kinds that never publish are publication_credentials.py's
  // `_NON_PUBLICATION_RUN_KINDS`. Every other kind keeps the seal rule.
  // `null`: the kind publishes and keeps the seal rule; otherwise the exact
  // sentence its run page says.
  const KINDS: Array<[string, string | null]> = [
    ['dispatch', null],
    ['loop', null],
    ['delegation', null],
    ['review', null],
    ['planning', 'Nothing to do on this run: a planning run publishes nothing.'],
    ['adversarial_review', 'Nothing to do on this run: a review run publishes nothing. Its verdict is below.'],
    ['review_remediation', null],
    ['review_stage', null],
    ['ci_fix', null],
    ['resolve', null],
    ['readiness_probe', 'Nothing to do on this run: a readiness probe publishes nothing.'],
    ['triage', 'Nothing to do on this run: a triage run publishes nothing.'],
    ['acceptance_author', null]
  ];
  const unsealed = { flight: flight({ sealed: false, artifact_node_id: null }), diff: null };
  /** A governed-review verdict as the run detail serves it, scoped `run` (its own round) or `item`. */
  const verdict = (scope: string) => ({
    round: 2,
    verdict: 'READY',
    head_sha: 'a'.repeat(40),
    reviewer_model: 'gpt-5-codex',
    ts: '2026-08-18T10:12:00Z',
    scope
  });
  /** A finished run of `kind`; a review run carries its own round, as the tour's do. */
  const finished = (kind: string) =>
    run({ run_kind: kind, status: 'success', verdict: kind === 'adversarial_review' ? verdict('run') : null });

  it('names exactly the kinds the table says publish nothing', () => {
    const none = KINDS.filter(([, said]) => said !== null).map(([kind]) => kind);
    expect([...NO_ARTIFACT_RUN_KINDS].sort()).toEqual(none.sort());
  });

  it.each(KINDS)('%s, unsealed: blocked on the seal unless it publishes nothing', (kind, said) => {
    const view = buildFlightView(sources({ run: finished(kind), ...unsealed }), NOW);
    if (said === null) {
      expect(view.safestAction.action).toBe('wait — nothing is sealed for this attempt yet');
      expect(view.safestAction.blockedBy).toBe('artifact_seal');
      expect(view.outcome).toBe('Run succeeded · publication blocked');
    } else {
      expect(view.safestAction).toEqual({ action: said, reason: '', blockedBy: null, command: '' });
      expect(view.outcome).toBe('');
    }
  });

  it.each(KINDS)('%s, sealed with a pull request open', (kind, said) => {
    // The fixture run's pull request is merged; this case is about an open one.
    const view = buildFlightView(sources({ run: { ...finished(kind), pr_state: 'OPEN' } }), NOW);
    expect(view.safestAction.blockedBy).toBeNull();
    expect(view.outcome).toBe('');
    expect(view.safestAction.action).toBe(said ?? 'review the pull request against this sealed change set');
  });

  // The action and the reason follow the pull request's recorded state (design
  // review c9): only an open one asks a person for a review; the state is a
  // cached reading, so "open" is "open when last read"; and a state the ledger
  // did not record is never assumed open. The link beside it is always quiet.
  const SEALED = 'the artifact is sealed, the chain verified and';
  it.each([
    [
      'OPEN',
      'review the pull request against this sealed change set',
      `${SEALED} its pull request was open when last read`,
      'pull request'
    ],
    [
      'open',
      'review the pull request against this sealed change set',
      `${SEALED} its pull request was open when last read`,
      'pull request'
    ],
    ['MERGED', 'nothing to do: this sealed change set is what landed', `${SEALED} its pull request was merged`, 'pull request'],
    [
      'CLOSED',
      'nothing to do: the pull request was closed without a merge, and nothing is waiting on this run',
      `${SEALED} its pull request was closed without a merge`,
      'pull request'
    ],
    [
      null,
      'open the pull request to see where this sealed change set stands',
      'the artifact is sealed and the chain verified; the pull request’s state is not recorded',
      'open the pull request'
    ],
    [
      'DRAFTISH',
      'open the pull request to see where this sealed change set stands',
      'the artifact is sealed and the chain verified; the pull request’s state is not recorded',
      'open the pull request'
    ]
  ])('a sealed run whose pull request is %s gets the move that state allows', (pr_state, action, reason, label) => {
    const view = buildFlightView(sources({ run: run({ pr_state }) }), NOW);
    expect(view.safestAction).toEqual({ action, reason, blockedBy: null, command: '' });
    expect(view.safestLink).toEqual({ href: 'https://github.com/o/r/pull/9', label, external: true });
    // Only an open one asks for a review, and nothing says "is open" as fact.
    expect(view.safestAction.action.startsWith('review')).toBe(pr_state?.toLowerCase() === 'open');
    expect(view.safestAction.reason).not.toMatch(/\bis open\b/);
  });

  const SHORT = 'Nothing to do on this run: a review run publishes nothing.';
  const LONG = `${SHORT} Its verdict is below.`;
  // A review run points at its verdict only when the page shows its own round.
  it.each([
    ['succeeded with its own verdict', 'success', verdict('run'), LONG],
    ['still running', 'pending', null, SHORT],
    ['failed', 'failure', null, SHORT],
    ["with only the work item's standing verdict", 'success', verdict('item'), SHORT],
    ['with no verdict recorded', 'success', null, SHORT]
  ])('a review run %s publishes nothing, blocked on nothing', (_, status, recorded, said) => {
    const view = buildFlightView(
      sources({ run: run({ run_kind: 'adversarial_review', status, verdict: recorded }), ...unsealed }),
      NOW
    );
    expect(view.safestAction).toEqual({ action: said, reason: '', blockedBy: null, command: '' });
    expect(view.outcome).toBe('');
  });

  it('still verifies the chain first: a review run whose chain did not replay is blocked on it', () => {
    const view = buildFlightView(
      sources({
        run: run({ run_kind: 'adversarial_review' }),
        flight: flight({ sealed: false, chain_verified: false })
      }),
      NOW
    );
    expect(view.safestAction.blockedBy).toBe('host_sequence');
  });
});
