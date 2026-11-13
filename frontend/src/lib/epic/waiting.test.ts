/**
 * #2209 — "waiting for human arm" says a person is needed and nothing about
 * what the person does. These pin the one rewrite and, just as importantly,
 * that nothing else is rewritten: the driver's own waiting prose already
 * names its command, and a second paraphrase here would drift from it.
 */
import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { armAction, ARM_LABEL, ARM_WAIT_REASON, waitAction } from './waiting';

/** The modules that explain this rewrite to the next reader. Their prose has to
 * agree with it: a comment naming a command the CLI does not have sends the
 * next person to a "No such command" just as surely as printing it would. */
const CITING_SOURCES = ['./waiting.ts', '../factory/status.ts'].map((rel) => ({
  rel,
  src: readFileSync(new URL(rel, import.meta.url), 'utf8')
}));

describe('waitAction', () => {
  it('replaces the arm wait with the action and the marker', () => {
    expect(waitAction(ARM_WAIT_REASON, 'SFD-223')).toBe(
      'arm it: add the kew:ready label to SFD-223 in Linear'
    );
    // The action leads with the verb, so a list of waiting nodes reads as a
    // list of things to do rather than a list of states.
    expect(waitAction(ARM_WAIT_REASON, 'SFD-223').startsWith('arm it:')).toBe(true);
    expect(waitAction(ARM_WAIT_REASON, 'SFD-223')).toContain(ARM_LABEL);
    expect(waitAction(ARM_WAIT_REASON, 'SFD-223')).not.toContain(ARM_WAIT_REASON);
  });

  it('names the node it is about, so a list of them is actionable', () => {
    for (const id of ['SFD-223', '2055', 'repo#5']) {
      expect(waitAction(ARM_WAIT_REASON, id)).toContain(id);
    }
  });

  it('does not invent a `kew epic arm` command, because there is none', () => {
    // #2209's example wording was "arm it: kew epic arm go-live-2026-09
    // SFD-223". No such command exists (kew/src/kew/cli/epic.py); arming is
    // the `kew:ready` label `linear_admission_status` looks for. Printing a
    // command that does not exist would send the operator to a "No such
    // command", which is worse than the wording it replaced.
    expect(waitAction(ARM_WAIT_REASON, 'SFD-223')).not.toContain('kew epic arm');
  });

  it('is not contradicted by the prose of the modules that cite it', () => {
    // factory/status.ts justified demoting `queued` off the needs-you diamond
    // by saying this module's wording names `kew epic arm <ref> <id>`, which it
    // deliberately does not — the comment documented a command that does not
    // exist and the mark change leaned on it. Both files may NAME the missing
    // command only to say it is missing, so the claim is pinned as "every
    // mention is a denial" rather than "no mention".
    for (const { rel, src } of CITING_SOURCES) {
      expect(src, rel).toContain(ARM_LABEL);
      for (const [line] of src.matchAll(/^.*epic arm.*$/gm)) {
        expect(line, `${rel}: ${line}`).toMatch(/\bno\b|\bnot\b|\bnever\b|DEPARTURE/);
      }
    }
  });

  it('prints every other waiting reason verbatim — the driver owns the wording', () => {
    // The real `_FIXED_WAITING_REASONS` lines, which already name a command.
    const verbatim = [
      'awaiting_continuation — preserved dirty work; use `kew epic continue` or `kew epic abandon`',
      'awaiting_publication — publication unavailable; open an external PR and run `kew finalize`',
      'awaiting_no_change — the run changed nothing; confirm or reject the recorded no-change outcome',
      'blocked (blocked label)',
      'held — budget_preflight exceeds the per-issue cap'
    ];
    for (const reason of verbatim) expect(waitAction(reason, 'SFD-223')).toBe(reason);
  });

  it('is the empty string for a node with nothing recorded', () => {
    for (const reason of [null, undefined, '', '   ']) {
      expect(waitAction(reason, 'SFD-223')).toBe('');
    }
  });

  it('matches on the trimmed reason, so stray whitespace still gets the action', () => {
    expect(waitAction(`  ${ARM_WAIT_REASON}\n`, 'SFD-223')).toBe(
      waitAction(ARM_WAIT_REASON, 'SFD-223')
    );
    // ...but a reason that merely CONTAINS the phrase is somebody else's
    // sentence and is left alone.
    const longer = `${ARM_WAIT_REASON} — and three dependents are waiting on it`;
    expect(waitAction(longer, 'SFD-223')).toBe(longer);
  });
});

describe('armAction', () => {
  it('is the same sentence waitAction prints, for the one reason it rewrites', () => {
    expect(armAction(ARM_WAIT_REASON, 'SFD-223')).toBe(waitAction(ARM_WAIT_REASON, 'SFD-223'));
    expect(armAction(`  ${ARM_WAIT_REASON}\n`, 'SFD-223')).toBe(
      armAction(ARM_WAIT_REASON, 'SFD-223')
    );
  });

  it('is null for every other reason, so a line that has one is left alone', () => {
    // The null is the point: `statusSegments` ADDS to a line that already leads
    // with the status word, so repeating the driver's own waiting prose there
    // would print the status twice (attention.ts).
    const others = [
      null,
      undefined,
      '',
      '   ',
      'blocked (blocked label)',
      'remediating — acting on review findings',
      `${ARM_WAIT_REASON} — and three dependents are waiting on it`
    ];
    for (const reason of others) expect(armAction(reason, 'SFD-223'), String(reason)).toBeNull();
  });
});
