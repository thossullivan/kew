/**
 * What every write says in the demo console (#3139).
 *
 * The demo has no backend and acts on nothing, so a press that would change
 * kew answers 405 with one sentence: what the press would have done, and the
 * command that does it in a real kew —
 *
 *   Demo: this would <what the action does>. In kew, run `<command>`.
 *
 * One table, one row per write route the console can send: the path pattern
 * (over the path after `/api/`), the sentence, and where the command comes
 * from. A command is never written into a sentence by hand: it is built from
 * the request by an existing builder where the console has one (`RUNNABLE` and
 * `isRunnable` in `run-request.ts`, `STOP_FLEET` in `palette/actions.ts`,
 * `workActionLabel` for an offered action's name), and otherwise by `kew()`
 * below from the verb's words and the request's own ids. Config overrides, the
 * palette's ask and sign-in have no CLI command, so they carry the sentence
 * only.
 */
import { workActionLabel } from '$lib/dashboard';
import { STOP_FLEET } from '$lib/palette/actions';
import { isRunnable, RUNNABLE } from '$lib/run-request';
import { itemRef } from '$lib/utils';
import { DEMO_REFUSAL } from './banner.js';

/** A write as the shim received it: the route's path after `/api/`, and its JSON body. */
export type DemoWrite = { method: string; path: string; body: unknown };

/** One row of the table. `match` holds the pattern's groups, decoded. */
export type WriteRow = {
  method: 'POST' | 'DELETE';
  pattern: RegExp;
  /** What the action does, as the clause after "this would". */
  does: (match: string[], body: Record<string, unknown>) => string;
  /** The command that does it in kew, or null where no CLI command exists. */
  command: ((match: string[], body: Record<string, unknown>) => string | null) | null;
  /** Where the command comes from — for a reader of this table, not the screen. */
  source: string;
};

/** A kew command line from its verb's words and arguments. */
export function kew(...words: (string | number)[]): string {
  return ['kew', ...words.map(String)].join(' ');
}

/** The verb words `kew.run_requests.RUNNABLE` accepts for `name`, so a
 * runnable command is spelled the way the drive spells it. */
function runnable(name: string): readonly string[] {
  const row = RUNNABLE.find(([verb]) => verb.join(' ') === name);
  if (row === undefined) throw new Error(`${name} is not a runnable verb`);
  return row[0];
}

const text = (v: unknown): string => (typeof v === 'string' ? v : '');

/**
 * An offered work action as the command that performs it, keyed by action.
 * An action whose CLI needs what the request does not carry (a run id, a PR,
 * a written reason) points at the TUI, which offers the same action on the
 * same item.
 */
const WORK_ACTION_COMMAND: Readonly<Record<string, (item: string) => string>> = {
  dispatch: (item) => kew(...runnable('dispatch'), item),
  resume_fleet: () => kew('resume'),
  confirm_no_change: (item) => kew('no-change', 'confirm', item),
  retry_no_change_sync: (item) => kew('no-change', 'retry-sync', item)
};

const SEGMENT = '([^/]+)';

/** Every write route the console sends, in one place. */
export const WRITES: readonly WriteRow[] = [
  {
    method: 'POST',
    pattern: /^verbs\/resume$/,
    does: () => 'clear the fleet stop and let dispatch start again',
    command: () => kew('resume'),
    source: 'kew resume'
  },
  {
    method: 'POST',
    pattern: /^fleet\/stop$/,
    does: () => STOP_FLEET.intent,
    command: () => kew('stop'),
    source: 'palette/actions.ts STOP_FLEET, kew stop'
  },
  {
    method: 'POST',
    pattern: new RegExp(`^verbs/queue/${SEGMENT}$`),
    does: ([issue]) => `queue #${issue} for the next dispatch`,
    command: ([issue]) => kew(...runnable('dispatch'), issue),
    source: 'run-request.ts RUNNABLE dispatch'
  },
  {
    method: 'POST',
    pattern: new RegExp(`^verbs/orphans/${SEGMENT}/ack$`),
    does: ([pr]) => `acknowledge orphaned pull request #${pr}`,
    command: () => kew('tui'),
    source: 'the TUI orphan screen (no CLI verb)'
  },
  {
    method: 'POST',
    pattern: new RegExp(`^approvals/${SEGMENT}$`),
    does: ([issue], body) =>
      body.action === 'deny'
        ? `deny the hold on #${issue} and park it`
        : `approve the hold on #${issue} and let its dispatch start`,
    command: ([issue], body) => kew(body.action === 'deny' ? 'deny' : 'approve', issue),
    source: 'kew approve / kew deny'
  },
  {
    method: 'POST',
    pattern: new RegExp(`^config/${SEGMENT}$`),
    does: ([field], body) =>
      typeof body.value === 'number' || typeof body.value === 'string'
        ? `override ${field} to ${body.value}`
        : `override ${field}`,
    command: null,
    source: 'no CLI command: overrides are made here'
  },
  {
    method: 'DELETE',
    pattern: new RegExp(`^config/${SEGMENT}$`),
    does: ([field]) => `drop the override on ${field} and go back to kew.toml`,
    command: null,
    source: 'no CLI command: overrides are made here'
  },
  {
    method: 'POST',
    pattern: /^run-requests$/,
    does: (_, body) => `ask the drive to run ${text(body.command) || 'this command'}`,
    command: (_, body) => (isRunnable(text(body.command)) ? text(body.command).trim() : null),
    source: 'the request body, held to run-request.ts isRunnable'
  },
  {
    method: 'POST',
    pattern: new RegExp(`^verbs/work/${SEGMENT}/action$`),
    does: ([item], body) => `${workActionLabel(text(body.action)).toLowerCase()} for ${itemRef(item, null)}`,
    command: ([item], body) => (WORK_ACTION_COMMAND[text(body.action)] ?? (() => kew('tui')))(item),
    source: 'dashboard.ts workActionLabel; WORK_ACTION_COMMAND, else kew tui'
  },
  {
    method: 'POST',
    pattern: new RegExp(`^prompts/${SEGMENT}/versions$`),
    does: ([role]) => `propose a new version of the ${role} prompt for approval`,
    command: ([role]) => kew('prompts', 'propose', role, '--file', 'addendum.md'),
    source: 'kew prompts propose'
  },
  {
    method: 'POST',
    pattern: new RegExp(`^prompts/${SEGMENT}/versions/${SEGMENT}/approve$`),
    does: ([role, version]) => `approve version ${version} of the ${role} prompt`,
    command: ([role, version], body) =>
      kew('prompts', 'approve', role, version, '--generation', String(body.generation ?? 0)),
    source: 'kew prompts approve'
  },
  {
    method: 'POST',
    pattern: new RegExp(`^prompts/${SEGMENT}/versions/${SEGMENT}/decline$`),
    does: ([role, version]) => `decline version ${version} of the ${role} prompt`,
    command: ([role, version]) => kew('prompts', 'decline', role, version),
    source: 'kew prompts decline'
  },
  {
    method: 'POST',
    pattern: new RegExp(`^prompts/${SEGMENT}/retire$`),
    does: ([role]) => `retire the ${role} prompt's approved version`,
    command: ([role], body) => kew('prompts', 'retire', role, '--generation', String(body.generation ?? 0)),
    source: 'kew prompts retire'
  },
  {
    method: 'POST',
    pattern: /^palette\/ask$/,
    does: () => 'ask the model which command answers your question',
    command: null,
    source: 'no CLI command'
  },
  {
    method: 'POST',
    pattern: /^auth\/login$/,
    does: () => 'sign you in to this console',
    command: null,
    source: 'no CLI command'
  }
];

/** What a write the table does not name says: still a refusal, still a sentence. */
export const UNLISTED_WRITE = `${DEMO_REFUSAL} change kew, and nothing here acts on a real repository.`;

function decoded(groups: string[]): string[] | null {
  try {
    return groups.map((g) => decodeURIComponent(g));
  } catch {
    return null;
  }
}

/** The sentence one write is refused with. */
export function demoRefusal(write: DemoWrite): string {
  const body =
    write.body !== null && typeof write.body === 'object' ? (write.body as Record<string, unknown>) : {};
  for (const row of WRITES) {
    if (row.method !== write.method.toUpperCase()) continue;
    const found = row.pattern.exec(write.path);
    if (found === null) continue;
    const match = decoded(found.slice(1));
    if (match === null) break;
    const does = `${DEMO_REFUSAL} ${row.does(match, body)}.`;
    const command = row.command?.(match, body) ?? null;
    return command === null ? does : `${does} In kew, run \`${command}\`.`;
  }
  return UNLISTED_WRITE;
}
