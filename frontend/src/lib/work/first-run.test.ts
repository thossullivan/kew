/**
 * #2515 — the first-run card on `/work`: when it shows, which readiness checks
 * it lists, and which ONE command is the next move. The model is held directly
 * and the component is server-rendered, so the markup a new user sees — the
 * check, its copyable fix, the single magenta Copy — is what is asserted.
 */
import { existsSync } from 'node:fs';
import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import type { Dashboard, Readiness, ReadinessCheck, Run, WorkProjection } from '$lib/api';
import { plainCommands } from '$lib/ui/command-markup';
import FirstRunCard from './FirstRunCard.svelte';
import {
  firstRunCard,
  INIT_COMMAND,
  SETUP_COMMAND,
  isFirstRun
} from './first-run';
import type { WorkRow } from './list';

/** The fix `kew doctor` serves for a missing allowlist network on docker. */
const NETWORK_FIX = 'docker network create --driver bridge kew-allowlist';

function check(id: string, status: ReadinessCheck['status'], extra: Partial<ReadinessCheck> = {}) {
  return {
    id,
    status,
    summary: status === 'ok' ? 'passed' : `${id} is wrong`,
    hint: null,
    fix: null,
    details: [],
    warnings: [],
    ...extra
  } satisfies ReadinessCheck;
}

function readiness(checks: ReadinessCheck[]): Readiness {
  return {
    schema: 1,
    checks,
    ok: !checks.some((c) => c.status === 'fail' || c.status === 'warn'),
    checked_at: '2026-10-03T14:47:00+00:00'
  };
}

const NETWORK_FAILING = readiness([
  check('required-tools', 'ok'),
  check('container-runtime', 'ok'),
  check('allowlist-network', 'fail', {
    summary: "'kew-allowlist' is not present",
    hint: 'kew installs no firewall rules on it',
    fix: NETWORK_FIX
  }),
  check('openshell-gateway', 'skip', { summary: 'not reported by its check' })
]);

/** `kew serve` before `kew init`: no config in force, as `kew doctor` says it. */
const NO_CONFIG = readiness([
  check('project-config', 'fail', {
    summary: 'no kew.toml found; running on built-in defaults',
    hint: "kew init writes this project's kew.toml from detection and flags",
    fix: 'kew init'
  }),
  check('required-tools', 'ok'),
  check('allowlist-network', 'fail', { fix: NETWORK_FIX })
]);

const ALL_PASSING = readiness([
  check('project-config', 'ok', { summary: 'config in force: /p/kew.toml' }),
  check('required-tools', 'ok'),
  check('container-runtime', 'ok'),
  check('allowlist-network', 'ok'),
  check('openshell-gateway', 'skip', { summary: 'not reported by its check' })
]);

/** The server render, each command block read as the command it holds. */
function html(model: ReturnType<typeof firstRunCard>, copied = ''): string {
  return plainCommands(render(FirstRunCard, { props: { card: model, copied } }).body);
}

/**
 * The text of every `<code>` in the render, in order: each Copy button's command
 * block and every inline `<code>` too, so a "not taught" assertion sees a
 * command wherever the card writes it.
 */
function commandBlocks(body: string): string[] {
  return [...body.matchAll(/<code[^>]*>([^<]*)<\/code>/g)].map((m) => m[1]);
}

/** The text of every shared `Command` block (`code[data-command]`), in order. */
function sharedCommandBlocks(body: string): string[] {
  return [...body.matchAll(/<code\b[^>]*\bdata-command\b[^>]*>([^<]*)<\/code>/g)].map((m) => m[1]);
}

function count(body: string, needle: string): number {
  return body.split(needle).length - 1;
}

/** The server render wraps a button's text in empty hydration comments. */
function buttonText(body: string): string {
  return body.replace(/<!---->/g, '');
}

describe('isFirstRun', () => {
  const projection = { views: [] } as unknown as WorkProjection;
  const dashboard = {} as Dashboard;
  const row = { itemId: '7' } as WorkRow;
  const run = { run_id: 'a' } as Run;

  it('is true with no work and no run history', () => {
    expect(isFirstRun({ rows: [], projection, dashboard, runs: [] })).toBe(true);
  });

  it('is false once there is work or a run', () => {
    expect(isFirstRun({ rows: [row], projection, dashboard, runs: [] })).toBe(false);
    expect(isFirstRun({ rows: [], projection, dashboard, runs: [run] })).toBe(false);
  });

  it('is false while any read is missing — a failed read is not an empty factory', () => {
    expect(isFirstRun({ rows: [], projection: null, dashboard, runs: [] })).toBe(false);
    expect(isFirstRun({ rows: [], projection, dashboard: null, runs: [] })).toBe(false);
    expect(isFirstRun({ rows: [], projection, dashboard, runs: null })).toBe(false);
  });
});

describe('firstRunCard', () => {
  it('AC1: lists a failing allowlist-network check with its own copyable fix', () => {
    const card = firstRunCard(NETWORK_FAILING);
    expect(card.checks).toEqual([
      {
        id: 'allowlist-network',
        status: 'fail',
        summary: "'kew-allowlist' is not present",
        command: NETWORK_FIX,
        hint: 'kew installs no firewall rules on it'
      }
    ]);
    // The fix is the next move, so it — not the setup step — is the magenta one.
    expect(card.primary).toBe(NETWORK_FIX);
    expect(card.next).toBe(SETUP_COMMAND);

    const body = html(card);
    expect(body).toContain('allowlist-network');
    expect(body).toContain("'kew-allowlist' is not present");
    // The copyable blocks, in order: the check's fix, then the next command.
    expect(commandBlocks(body).slice(0, 2)).toEqual([NETWORK_FIX, SETUP_COMMAND]);
    expect(count(buttonText(body), '>Copy</button>')).toBe(2);
    // Exactly one magenta Copy (the `c` key's), and it sits on the fix.
    expect(count(body, 'aria-label="Copy command (c)"')).toBe(1);
    const primary = body.indexOf('aria-label="Copy command (c)"');
    expect(body.lastIndexOf(NETWORK_FIX, primary)).toBeGreaterThan(body.lastIndexOf(SETUP_COMMAND, primary));
    expect(body).toContain(`aria-label="Copy ${SETUP_COMMAND}"`);
    // A passing or skipped check is not listed.
    expect(body).not.toContain('required-tools');
    expect(body).not.toContain('openshell-gateway');
  });

  it('AC2: with every check passing, shows only the next command', () => {
    const card = firstRunCard(ALL_PASSING);
    expect(card.checks).toEqual([]);
    expect(card.primary).toBe(SETUP_COMMAND);
    expect(card.reading).toBe('read');

    const body = html(card);
    expect(body).not.toContain('Readiness checks to fix');
    expect(body).not.toContain('allowlist-network');
    // One copyable command, and it is the magenta next move.
    expect(count(buttonText(body), '>Copy</button>')).toBe(1);
    expect(count(body, 'aria-label="Copy command (c)"')).toBe(1);
    expect(commandBlocks(body)[0]).toBe(SETUP_COMMAND);
    expect(body).toContain('Every readiness check passes');
  });

  it('#2652: with no config in force, kew init is the one primary command', () => {
    const card = firstRunCard(NO_CONFIG);
    expect(card.primary).toBe(INIT_COMMAND);
    expect(card.next).toBe(INIT_COMMAND);
    // Another check's fix does not outrank writing the config those checks read:
    // it waits behind the disclosure (#2776).
    expect(card.checks.map((c) => c.id)).toEqual(['project-config']);
    expect(card.deferred.map((c) => c.id)).toEqual(['allowlist-network']);

    const body = html(card);
    expect(count(body, 'aria-label="Copy command (c)"')).toBe(1);
    const primary = body.indexOf('aria-label="Copy command (c)"');
    expect(body.lastIndexOf(`>${INIT_COMMAND}</code>`, primary)).toBeGreaterThan(
      body.lastIndexOf(NETWORK_FIX, primary)
    );
    // `kew init` is written once (it runs setup itself, so setup is not taught).
    expect(commandBlocks(body).filter((c) => c === INIT_COMMAND)).toHaveLength(1);
    expect(commandBlocks(body)).not.toContain(SETUP_COMMAND);
    // The check still says what is wrong, with its hint in place of a command.
    expect(body).toContain('no kew.toml found');
    expect(body).toContain('kew init writes this project');
  });

  it('#2776 AC1: with no config, kew init comes first and the other checks wait behind a disclosure', () => {
    const card = firstRunCard(
      readiness([
        check('allowlist-network', 'fail', { fix: NETWORK_FIX }),
        check('project-config', 'fail', { fix: 'kew init', hint: 'kew init writes it' }),
        check('model-eol', 'warn', { fix: 'kew model-eol refresh' }),
        check('containment', 'fail', { hint: 'set [sandbox] enabled = true' })
      ])
    );
    expect(card.primary).toBe(INIT_COMMAND);
    expect(card.checks.map((c) => c.id)).toEqual(['project-config']);
    // Failures before warnings, as on the open card.
    expect(card.deferred.map((c) => c.id)).toEqual(['allowlist-network', 'containment', 'model-eol']);
    expect(card.deferred.map((c) => c.command)).not.toContain(card.primary);

    const body = html(card);
    // The first command a person meets, top-down, is kew init.
    expect(commandBlocks(body)[0]).toBe(INIT_COMMAND);
    // The rest sit inside one closed disclosure that names the count.
    const open = body.indexOf('aria-controls="first-run-deferred"');
    expect(open).toBeGreaterThan(body.indexOf(`>${INIT_COMMAND}</code>`));
    expect(body.slice(open, body.indexOf('</button>', open))).toContain(
      '3 more checks read built-in defaults'
    );
    expect(body).toMatch(/aria-expanded="false"[^>]*aria-controls="first-run-deferred"/);
    expect(body).toMatch(/<div id="first-run-deferred" hidden/);
    const inside = body.slice(body.indexOf('id="first-run-deferred"'));
    expect(commandBlocks(inside)).toEqual([NETWORK_FIX, 'kew model-eol refresh']);
    expect(inside).toContain('set [sandbox] enabled = true');
    expect(body.slice(0, open)).not.toContain(NETWORK_FIX);
    expect(body.slice(0, open)).not.toContain('model-eol');
    // None of them is the magenta one.
    expect(count(body, 'aria-label="Copy command (c)"')).toBe(1);
    expect(inside).not.toContain('aria-label="Copy command (c)"');
  });

  it('#2776: one deferred check is named in the singular', () => {
    expect(html(firstRunCard(NO_CONFIG))).toContain('1 more check reads built-in defaults');
  });

  it('#2776: with config in force there is no disclosure', () => {
    const card = firstRunCard(NETWORK_FAILING);
    expect(card.deferred).toEqual([]);
    expect(html(card)).not.toContain('first-run-deferred');
  });

  it('#2652: with config in force, kew setup stays the primary command', () => {
    const card = firstRunCard(ALL_PASSING);
    expect(card.primary).toBe(SETUP_COMMAND);
    expect(commandBlocks(html(card))).not.toContain(INIT_COMMAND);
  });

  it('puts failures before warnings and leaves a fix-less check as its hint', () => {
    const card = firstRunCard(
      readiness([
        check('model-eol', 'warn', { fix: 'kew model-eol refresh' }),
        check('containment', 'fail', { hint: 'set [sandbox] enabled = true' }),
        check('allowlist-network', 'fail', { fix: NETWORK_FIX })
      ])
    );
    expect(card.checks.map((c) => [c.id, c.status])).toEqual([
      ['containment', 'fail'],
      ['allowlist-network', 'fail'],
      ['model-eol', 'warn']
    ]);
    // The first check with a command is the one the magenta Copy carries.
    expect(card.primary).toBe(NETWORK_FIX);
    const body = html(card);
    expect(body).toContain('set [sandbox] enabled = true');
    expect(count(body, 'aria-label="Copy command (c)"')).toBe(1);
  });

  it('never claims the checks pass when it has no reading', () => {
    for (const [reading, word] of [
      [undefined, 'Reading the readiness checks'],
      [null, 'could not be read']
    ] as const) {
      const card = firstRunCard(reading);
      expect(card.checks).toEqual([]);
      expect(card.primary).toBe(SETUP_COMMAND);
      const body = html(card);
      expect(body).toContain(word);
      expect(body).not.toContain('Every readiness check passes');
    }
  });

  it('teaches only registered kew commands', () => {
    // Every command the card itself writes (a check's fix is the doctor's, not
    // the card's) must be a real subcommand: a command the CLI answers with
    // "No such command" strands a new user. `kew init` is registered since
    // #2502. `docs/cli/` carries one page per registered command, so it is the
    // live list.
    const body = html(firstRunCard(ALL_PASSING)) + html(firstRunCard(NO_CONFIG));
    const taught = [
      SETUP_COMMAND,
      INIT_COMMAND,
      'kew dispatch',
      ...body.matchAll(/\bkew ([a-z][a-z-]*)/g)
    ].map((c) => (typeof c === 'string' ? /^kew (\S+)/.exec(c)?.[1] : c[1]));
    expect(taught.length).toBeGreaterThan(1);
    for (const name of taught) {
      expect(existsSync(new URL(`../../../../docs/cli/${name}.md`, import.meta.url)), `kew ${name}`).toBe(
        true
      );
    }
  });

  it('THEN is a real dispatch with a quiet Copy, or a sentence — never a template (#3467 D28)', () => {
    const glance = (ready: number[], candidates: number[]) =>
      ({
        glance: {
          ready_queue: ready.map((n) => ({ issue_num: n, title: `#${n}`, complexity: '', route: '', estimate_usd: null })),
          queue_candidates: candidates.map((n) => ({ issue_num: n, title: `#${n}` }))
        }
      }) as unknown as Dashboard;
    // A ready item first, then an untouched open issue; the `0` sentinel names none.
    expect(firstRunCard(ALL_PASSING, glance([2060], [17])).then).toBe('kew dispatch 2060');
    expect(firstRunCard(ALL_PASSING, glance([0], [17])).then).toBe('kew dispatch 17');
    expect(firstRunCard(NO_CONFIG, glance([], [17])).then).toBe('kew dispatch 17');

    const body = html(firstRunCard(ALL_PASSING, glance([], [17])));
    expect(commandBlocks(body)).toContain('kew dispatch 17');
    // Quiet: the magenta Copy stays on the next move, and this one names its line.
    expect(count(body, 'aria-label="Copy command (c)"')).toBe(1);
    expect(body).toContain('aria-label="Copy kew dispatch 17"');

    for (const card of [firstRunCard(ALL_PASSING), firstRunCard(NO_CONFIG, glance([], []))]) {
      expect(card.then).toBe('');
      const drawn = html(card);
      expect(drawn).toContain('dispatch a first issue by its number');
      expect(drawn).not.toMatch(/kew dispatch/);
      expect(drawn).not.toMatch(/&lt;|&gt;/);
    }
  });

  it('shows Copied on the button whose command was just copied', () => {
    const text = buttonText(html(firstRunCard(NETWORK_FAILING), SETUP_COMMAND));
    expect(count(text, '>Copied</button>')).toBe(1);
    expect(text.indexOf('>Copied</button>')).toBeGreaterThan(text.indexOf(SETUP_COMMAND));
  });

  it('FirstRunCard prints every command through the shared Command block', () => {
    // #3077 — each command wraps only at its own spaces and never scrolls
    // sideways: a check's fix, the next command and every deferred fix is a
    // `code[data-command]`, one per Copy button, and none scrolls inside itself.
    const card = firstRunCard(
      readiness([
        check('allowlist-network', 'fail', { fix: NETWORK_FIX }),
        check('project-config', 'fail', { fix: 'kew init', hint: 'kew init writes it' }),
        check('model-eol', 'warn', { fix: 'kew model-eol refresh' })
      ])
    );
    const raw = render(FirstRunCard, { props: { card } }).body;
    const body = plainCommands(raw);
    expect(sharedCommandBlocks(body)).toEqual([INIT_COMMAND, NETWORK_FIX, 'kew model-eol refresh']);
    expect(count(body, 'data-command')).toBe(count(buttonText(body), '>Copy</button>'));
    expect(raw).not.toContain('overflow-x-auto');
    for (const model of [firstRunCard(NETWORK_FAILING), firstRunCard(ALL_PASSING)]) {
      const each = html(model);
      expect(sharedCommandBlocks(each)).toContain(model.next);
      expect(count(each, 'data-command')).toBe(count(buttonText(each), '>Copy</button>'));
      expect(each).not.toContain('overflow-x-auto');
    }
  });
});
