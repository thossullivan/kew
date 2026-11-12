/**
 * #2302 — the launchpad's readings: row states, the command, and the blockers.
 *
 * Pure, so each rule is checked where it is decided. The browser half (a
 * complexity change that re-reads, and a stale answer that is dropped) is
 * `launchpad.client.test.ts`, which drives the panel in a DOM.
 */
import { describe, expect, it } from 'vitest';
import planJson from '../../../fixtures/factory/dispatch-resolve.json';
import type { DispatchPlan } from '$lib/api';
import {
  PlanRequests,
  attentionRows,
  commandNote,
  dispatchCommand,
  invalidRows,
  launchBlockers,
  noCommandReason,
  planRows,
  planVerdict,
  routeName,
  routeSummary,
  sourceState
} from './launchpad';

/**
 * The committed fixture, with one field swapped out. The same payload the
 * fixture API serves at `/api/dispatch/resolve`, so what these tests read and
 * what `npm run dev:fixtures` draws cannot drift.
 */
function plan(over: Partial<DispatchPlan> = {}): DispatchPlan {
  return { ...(planJson as DispatchPlan), ...over };
}

const row = (rows: ReturnType<typeof planRows>, field: string) =>
  rows.find((r) => r.field === field)!;

/**
 * The refusal a credential-less host really produces, remedy and all (#2445).
 *
 * Copied from `kew.sandbox`'s own preflight text at full length on purpose:
 * this exact string is what the endpoint sends, and the reason it is here is
 * that the backend cap used to cut it mid-word at "do not use subscription
 * au" — so the test that the panel prints a remedy ending in its final word
 * needs a remedy whose final word is known.
 */
const CREDENTIAL_REFUSAL =
  'Sandbox: no claude-code credential in the environment — containers cannot inherit ' +
  'CLI login. Export ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN (ANTHROPIC_API_KEY is ' +
  'issued at console.anthropic.com; `claude setup-token` mints CLAUDE_CODE_OAUTH_TOKEN, a ' +
  'subscription token; do not use subscription auth for autonomous dispatch) to dispatch ' +
  'sandboxed, or set [sandbox] dangerously_skip_containment = true to run this agent on the ' +
  'host with no container boundary.';

/**
 * A refused plan, in the shape the endpoint really sends one: the resolution
 * raised before it had a route, so there is no image and no probe behind it.
 */
const refusedPlan = (
  credentials: { name: string; set: boolean }[] = [
    { name: 'ANTHROPIC_API_KEY', set: false },
    { name: 'CLAUDE_CODE_OAUTH_TOKEN', set: false }
  ]
): DispatchPlan =>
  plan({
    containment: {
      ...plan().containment,
      posture: 'refused',
      refusal_reason: CREDENTIAL_REFUSAL,
      image: null,
      image_present: null,
      credential_names_present: credentials
    }
  });

describe('one row per resolved field', () => {
  it('prints the value and the rule that decided it', () => {
    const runner = row(planRows(plan()), 'runner');
    expect(runner).toMatchObject({
      field: 'runner',
      value: 'codex',
      source: 'kew.toml [routing].simple',
      state: 'set'
    });
  });

  it('draws no rows at all before a plan arrives', () => {
    expect(planRows(null)).toEqual([]);
  });

  it('names every field the endpoint resolves', () => {
    expect(planRows(plan()).map((r) => r.field)).toEqual([
      'complexity',
      'lane',
      'runner',
      'model',
      'transport',
      'posture',
      'image',
      'credential',
      'live boundary',
      'governor',
      'run cap',
      'headroom',
      'spend so far'
    ]);
  });
});

describe('the four row states', () => {
  it('reads a default as a default, not as a setting', () => {
    expect(sourceState('default')).toBe('default');
    expect(sourceState('default (claude-code)')).toBe('default');
    expect(row(planRows(plan()), 'transport').state).toBe('default');
  });

  it('reads a DB override as overridden, never as the file', () => {
    expect(sourceState('DB override (agent.model, set by tom)')).toBe('overridden');
    const rows = planRows(
      plan({ runner: { value: 'codex', source: 'DB override (routing.simple, set by tom)' } })
    );
    expect(row(rows, 'runner').state).toBe('overridden');
  });

  it("reads a runner's own default as a default", () => {
    // The reviewer lane with no `[review] model`: the model is the runner's,
    // and the row says so rather than printing an empty value column.
    const rows = planRows(
      plan({
        lane: { value: 'review', source: 'the caller' },
        runner: { value: 'claude-code', source: 'default (claude-code)' },
        model: { value: '', source: "claude-code's own default" }
      })
    );
    expect(row(rows, 'model')).toMatchObject({ value: '—', state: 'default' });
  });

  it('reads an env layer as overridden too', () => {
    expect(sourceState('env (KEW_MODEL)')).toBe('overridden');
  });

  it('reads an unknown complexity as the default lane, never invalid (#2785)', () => {
    const rows = planRows(
      plan({
        complexity: { value: 'unknown', source: 'no complexity given', known: false }
      })
    );
    expect(row(rows, 'complexity')).toEqual({
      field: 'complexity',
      value: 'default',
      source: 'no complexity given',
      state: 'default',
      fix: null
    });
  });

  it('keeps the provenance of a tier the ready queue named', () => {
    const rows = planRows(
      plan({
        complexity: { value: 'complex', source: "the ready queue's `complex` label", known: true }
      })
    );
    expect(row(rows, 'complexity')).toMatchObject({
      value: 'complex',
      source: "the ready queue's `complex` label",
      state: 'set'
    });
  });

  it('marks a missing image invalid and points at the images section', () => {
    const rows = planRows(
      plan({
        containment: { ...plan().containment, image_present: false }
      })
    );
    expect(row(rows, 'image')).toMatchObject({
      state: 'invalid',
      source: 'NOT built on this host',
      fix: { href: '/health#images' }
    });
  });

  it('carries a refusal as the posture row, with the reason as its source', () => {
    const rows = planRows(
      plan({
        containment: {
          ...plan().containment,
          posture: 'refused',
          refusal_reason: 'no container runtime is available'
        }
      })
    );
    expect(row(rows, 'posture')).toMatchObject({
      value: 'refused',
      source: 'no container runtime is available',
      state: 'invalid'
    });
  });

  it('calls an uncontained posture overridden — configured, not broken', () => {
    const rows = planRows(
      plan({ containment: { ...plan().containment, posture: 'uncontained', image: null } })
    );
    expect(row(rows, 'posture').state).toBe('overridden');
    expect(row(rows, 'image').state).toBe('default');
  });

  it('never calls an unset credential a fault — the cli login is the common path', () => {
    const rows = planRows(
      plan({
        containment: {
          ...plan().containment,
          credential_names_present: [
            { name: 'ANTHROPIC_API_KEY', set: false },
            { name: 'CLAUDE_CODE_OAUTH_TOKEN', set: false }
          ]
        }
      })
    );
    expect(row(rows, 'credential')).toMatchObject({ value: 'cli login', state: 'default' });
  });

  it('prints credential NAMES and nothing else', () => {
    const credential = row(planRows(plan()), 'credential');
    expect(credential.value).toBe('OPENAI_API_KEY');
  });

  it('treats an unverified boundary as unknown and a refuted one as a fault', () => {
    expect(row(planRows(plan()), 'live boundary').state).toBe('default');
    const refuted = planRows(
      plan({ live_status: { value: 'refuted', source: 'stored evidence', probed_at: null } })
    );
    expect(row(refuted, 'live boundary').state).toBe('invalid');
    const confirmed = planRows(
      plan({ live_status: { value: 'confirmed', source: 'stored evidence', probed_at: 'x' } })
    );
    expect(row(confirmed, 'live boundary').state).toBe('set');
  });

  it('flags the run cap that can overshoot the per-issue headroom', () => {
    const rows = planRows(
      plan({ budget: { ...plan().budget, runtime_cap_exceeds_issue_headroom: true } })
    );
    expect(row(rows, 'run cap')).toMatchObject({
      state: 'invalid',
      fix: { href: '/budget' }
    });
  });

  it('shows exhausted headroom as invalid, and unknown headroom as a dash', () => {
    const none = planRows(
      plan({
        admission: { ...plan().admission, headroom: { usd: 0, binds: 'daily', source: 's' } }
      })
    );
    expect(row(none, 'headroom').state).toBe('invalid');
    const unknown = planRows(
      plan({
        admission: { ...plan().admission, headroom: { usd: null, binds: null, source: 's' } }
      })
    );
    expect(row(unknown, 'headroom')).toMatchObject({ value: '—', state: 'default' });
  });

  it('prints unknown spend as a dash rather than $0.00', () => {
    const rows = planRows(
      plan({ budget: { ...plan().budget, issue_spent_usd: null, daily_spent_usd: null } })
    );
    expect(row(rows, 'spend so far').value).toBe('— this item · — today');
  });

  it('collects the invalid rows for the summary line', () => {
    const rows = planRows(
      plan({
        complexity: { value: 'unknown', source: 'none given', known: false },
        containment: { ...plan().containment, image_present: false }
      })
    );
    // An unknown tier is not among them (#2785): it is the default lane's.
    expect(invalidRows(rows).map((r) => r.field)).toEqual(['image']);
  });

  it('says a swap the governor would apply is the route that will run', () => {
    const rows = planRows(
      plan({
        governor: {
          ...plan().governor,
          would_swap: true,
          reason: 'warm_prefix_cheaper',
          recommended_runner: 'claude-code',
          recommended_model: 'claude-opus-4-8',
          est_savings_usd: 0.42
        }
      })
    );
    expect(row(rows, 'governor')).toMatchObject({
      value: 'would swap to claude-code / claude-opus-4-8',
      source: 'warm_prefix_cheaper · saves $0.42',
      state: 'overridden'
    });
  });
});

describe('a refused posture (#2445)', () => {
  it('never describes the host launch a refused route will not take', () => {
    const rows = planRows(refusedPlan());
    expect(row(rows, 'image')).toEqual({
      field: 'image',
      value: '—',
      source: 'not resolved: the posture is refused',
      state: 'default',
      fix: null
    });
    expect(row(rows, 'credential')).toEqual({
      field: 'credential',
      value: 'none set',
      source: 'not resolved: the posture is refused',
      state: 'default',
      fix: null
    });
    // The two sentences the panel used to print under a refusal, both of them
    // about a launch that cannot happen.
    const sources = rows.map((r) => r.source).join(' | ');
    expect(sources).not.toContain('launches on the host');
    expect(sources).not.toContain('logs in itself');
  });

  it('still prints a credential that IS set — the environment is a fact', () => {
    // Refused for some other cause (no runtime, an unbuilt image): which names
    // this process holds is read off the environment, not off the route.
    const rows = planRows(refusedPlan([{ name: 'ANTHROPIC_API_KEY', set: true }]));
    expect(row(rows, 'credential')).toMatchObject({
      value: 'ANTHROPIC_API_KEY',
      source: 'set in this process',
      state: 'default'
    });
  });

  it('declares no credential at all without inventing one', () => {
    expect(row(planRows(refusedPlan([])), 'credential')).toMatchObject({
      value: '—',
      source: 'not resolved: the posture is refused'
    });
  });

  it('is ONE red row — the posture, which carries the reason and the fix', () => {
    const bad = invalidRows(planRows(refusedPlan()));
    expect(bad.map((r) => r.field)).toEqual(['posture']);
    expect(bad[0].fix).toEqual({ label: 'Health · posture by lane', href: '/health#posture' });
  });

  it('prints the remedy whole, ending in its own final word', () => {
    // The defect this pins: the panel's main message when the lane cannot run
    // reached the reader as "… do not use subscription au".
    const blocker = launchBlockers(refusedPlan())[0];
    expect(row(planRows(refusedPlan()), 'posture').source).toBe(CREDENTIAL_REFUSAL);
    expect(blocker).toContain(CREDENTIAL_REFUSAL);
    expect(blocker.endsWith('with no container boundary.')).toBe(true);
  });

  it("judges the console's process, not the shell the command is pasted into", () => {
    // The refusal is read off `kew serve`'s environment; the operator's own
    // shell may hold the credential, so the panel must not claim their command
    // will fail.
    expect(launchBlockers(refusedPlan())[0]).toContain(
      "this lane cannot run in the console's process"
    );
  });
});

describe('the exact command', () => {
  it('is the dispatch command for the implement lane', () => {
    expect(dispatchCommand(plan())).toBe('kew dispatch 2302');
  });

  it('carries no --complexity, because the CLI has none', () => {
    // The flag does not exist: `kew dispatch` reads the tier off the item's
    // labels. A command with a flag the CLI would reject is worse than none.
    expect(dispatchCommand(plan())).not.toContain('--complexity');
  });

  it('names the model that runs and the layer that picked it (#3467 D31)', () => {
    // The drawer names `kew.toml [routing].simple` as the model's source; the
    // note says the same, never that the tier chose it.
    expect(commandNote(plan())).toBe(
      "runs gpt-5.6-sol from kew.toml [routing].simple; the dispatch reads the tier from the item's labels"
    );
    expect(
      commandNote(plan({ model: { value: 'claude-opus-5-5', source: 'DB override (routing.simple, set by tom)' } }))
    ).toBe(
      "runs claude-opus-5-5 from DB override (routing.simple, set by tom); the dispatch reads the tier from the item's labels"
    );
    // A runner's own default has no model id to print: the source says it.
    expect(commandNote(plan({ model: { value: '', source: "codex's own default" } }))).toBe(
      "runs codex's own default; the dispatch reads the tier from the item's labels"
    );
    // An override is what Run sends, so it is what runs.
    expect(commandNote(plan(), 'gpt-9')).toBe(
      "runs gpt-9, the override this dispatch sends; the dispatch reads the tier from the item's labels"
    );
    // An unknown tier is read off the item at dispatch: no model here is the one.
    const unknown = plan({ complexity: { value: 'simple', source: 'default', known: false } });
    expect(commandNote(unknown)).toBe('');
  });

  it('prints no command for a reviewer lane, and says why', () => {
    const review = plan({ lane: { value: 'review', source: 'the caller' } });
    expect(dispatchCommand(review)).toBe('');
    expect(noCommandReason(review)).toContain("epic driver's review stage");
  });

  it('prints no command for a tracker key, and says why', () => {
    const linear = plan({ item: 'SFD-12', issue_num: null });
    expect(dispatchCommand(linear)).toBe('');
    expect(noCommandReason(linear)).toContain('GitHub issue number');
  });

  it('has nothing to print before a plan arrives', () => {
    expect(dispatchCommand(null)).toBe('');
    expect(commandNote(null)).toBe('');
  });
});

describe('why Launch is disabled', () => {
  it('always ends with the rule: this console never dispatches', () => {
    expect(launchBlockers(plan()).at(-1)).toBe(
      'the console never dispatches itself — Run hands the command to the drive, or copy it and run it'
    );
  });

  it('is only that rule when every gate is open', () => {
    expect(launchBlockers(plan())).toHaveLength(1);
  });

  it('names the fleet latch and its reason', () => {
    const stopped = plan({
      admission: { ...plan().admission, fleet_stopped: true, fleet_stopped_reason: 'deploy' }
    });
    expect(launchBlockers(stopped)[0]).toBe('the fleet is stopped — deploy');
  });

  it('names an open approval hold', () => {
    const held = plan({ admission: { ...plan().admission, open_hold: true } });
    expect(launchBlockers(held)[0]).toContain('approval hold');
  });

  it('never invents a hold out of an unreadable one', () => {
    const unknown = plan({ admission: { ...plan().admission, open_hold: null } });
    expect(launchBlockers(unknown)).toHaveLength(1);
  });

  it('names exhausted headroom and a lane that cannot run', () => {
    const blocked = plan({
      admission: {
        ...plan().admission,
        headroom: { usd: -2, binds: 'daily', source: 's' }
      },
      containment: {
        ...plan().containment,
        refusal_reason: 'kew-codex:latest is not built on this host'
      }
    });
    const reasons = launchBlockers(blocked);
    expect(reasons[0]).toContain('no budget headroom left (daily)');
    expect(reasons[1]).toContain('not built on this host');
  });

  it('says so when the plan itself could not be read', () => {
    expect(launchBlockers(null)).toEqual(['the plan could not be read']);
  });
});

describe('PlanRequests — the monotonic request id', () => {
  it('lets the newest read own the screen', () => {
    const reads = new PlanRequests();
    const first = reads.begin();
    const second = reads.begin();
    expect(reads.owns(first)).toBe(false);
    expect(reads.owns(second)).toBe(true);
  });

  it('keeps a lone read its own owner', () => {
    const reads = new PlanRequests();
    expect(reads.owns(reads.begin())).toBe(true);
  });

  it('never hands ownership back to a superseded read', () => {
    // The failure this prevents: three reads out, the first answering last.
    const reads = new PlanRequests();
    const tickets = [reads.begin(), reads.begin(), reads.begin()];
    expect(tickets.filter((t) => reads.owns(t))).toEqual([tickets[2]]);
  });
});

describe('the closed one-line summary (#2535)', () => {
  it('names the route as runner / model, the model id verbatim', () => {
    expect(routeSummary(plan())).toBe('codex / gpt-5.6-sol');
    // The id the resolver sent, never folded to the family it starts with.
    expect(
      routeSummary(plan({ model: { value: 'claude-opus-5-5', source: 'kew.toml [agent].model' } }))
    ).toBe('codex / claude-opus-5-5');
    // An unpinned reviewer model is the empty string: a dash, not a blank.
    expect(routeSummary(plan({ model: { value: '', source: "claude-code's own default" } }))).toBe(
      'codex / —'
    );
  });

  it('names the model in the line, keeping the id for its title (#2785)', () => {
    expect(routeName(plan())).toBe('codex / GPT-5.6 Sol');
    expect(routeName(plan(), 'gpt-5.6-luna')).toBe('codex / GPT-5.6 Luna (override)');
    // An id the catalog has not met prints as itself, never a blank.
    expect(routeName(plan({ model: { value: 'gpt-9', source: 's' } }))).toBe('codex / gpt-9');
    expect(routeName(plan({ model: { value: '', source: "claude-code's own default" } }))).toBe(
      'codex / —'
    );
  });

  it('is ready when nothing the dispatch would refuse is in the way', () => {
    expect(planVerdict(plan())).toEqual({ ready: true, reason: '' });
    // An unknown tier is a what-if, not a refusal.
    expect(
      planVerdict(
        plan({ complexity: { value: 'unknown', source: 'no complexity given', known: false } })
      ).ready
    ).toBe(true);
  });

  it('is refused with one phrase, never the remedy paragraph', () => {
    expect(planVerdict(refusedPlan())).toEqual({ ready: false, reason: 'posture refused' });
    expect(
      planVerdict(plan({ containment: { ...plan().containment, image_present: false } }))
    ).toEqual({ ready: false, reason: 'image not built' });
    const stopped = plan({
      admission: { ...plan().admission, fleet_stopped: true, fleet_stopped_reason: 'deploy' }
    });
    expect(planVerdict(stopped)).toEqual({ ready: false, reason: 'fleet stopped' });
    expect(planVerdict(plan({ admission: { ...plan().admission, open_hold: true } }))).toEqual({
      ready: false,
      reason: 'approval hold open'
    });
    const broke = plan({
      admission: { ...plan().admission, headroom: { usd: 0, binds: 'daily', source: 's' } }
    });
    expect(planVerdict(broke)).toEqual({ ready: false, reason: 'no budget headroom' });
  });

  it('names the most specific refusal first, as the Launch reasons do', () => {
    const both = plan({
      ...refusedPlan(),
      admission: { ...plan().admission, fleet_stopped: true, fleet_stopped_reason: '' }
    });
    expect(planVerdict(both).reason).toBe('fleet stopped');
  });
});

describe('the opened plan (#2535)', () => {
  it('folds every set and default row, keeping overridden and invalid ones', () => {
    expect(attentionRows(planRows(plan()))).toEqual([]);
    const rows = planRows(
      plan({
        transport: { value: 'proxy', source: 'DB override (inference_transport.mode)' },
        complexity: { value: 'unknown', source: 'no complexity given', known: false }
      })
    );
    // The unknown tier folds with the defaults (#2785); the override stays.
    expect(attentionRows(rows).map((r) => r.field)).toEqual(['transport']);
  });

  it('gives every invalid row a short phrase for its one-line remedy', () => {
    const bad = invalidRows(planRows(refusedPlan()));
    expect(bad[0].brief).toBe('refused');
    // The paragraph is still the row's source, for `details`.
    expect(bad[0].source).toBe(CREDENTIAL_REFUSAL);
    for (const r of invalidRows(
      planRows(
        plan({
          complexity: { value: 'unknown', source: 'no complexity given', known: false },
          containment: { ...plan().containment, image_present: false },
          live_status: { value: 'refuted', source: 's', probed_at: null },
          admission: { ...plan().admission, headroom: { usd: -1, binds: 'daily', source: 's' } }
        })
      )
    )) {
      expect(r.brief, r.field).toBeTruthy();
      expect(r.brief!.length, r.field).toBeLessThan(40);
    }
  });

  it('names a refusal in the brief Launch reasons instead of printing it again', () => {
    const brief = launchBlockers(refusedPlan(), { brief: true });
    expect(brief[0]).toBe(
      "this lane cannot run in the console's process — the posture is refused"
    );
    expect(brief.join(' ')).not.toContain('ANTHROPIC_API_KEY');
    // The full form is unchanged.
    expect(launchBlockers(refusedPlan())[0]).toContain(CREDENTIAL_REFUSAL);
  });
});
