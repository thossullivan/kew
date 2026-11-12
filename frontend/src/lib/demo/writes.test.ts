/**
 * Every write the console sends says, in the demo, what it would have done
 * (#3139) — and every command it names is one a real kew accepts the shape of.
 */
import { describe, expect, it } from 'vitest';
import { demoRefusal, UNLISTED_WRITE, WRITES } from './writes';

const SENTENCE = /^Demo: this would [^.]+(?:\.[^.\s][^.]*)*\.(?: In kew, run `kew [^`]+`\.)?$/;

/** Every write route the issue lists, as the console sends it. */
const CASES: [string, string, unknown, string][] = [
  ['POST', 'verbs/resume', { confirm: true }, 'Demo: this would clear the fleet stop and let dispatch start again. In kew, run `kew resume`.'],
  ['POST', 'fleet/stop', { reason: 'x' }, 'Demo: this would kill every running agent and refuse new dispatches until resumed. In kew, run `kew stop`.'],
  ['POST', 'verbs/queue/2060', { confirm: true }, 'Demo: this would queue #2060 for the next dispatch. In kew, run `kew dispatch 2060`.'],
  ['POST', 'verbs/orphans/88/ack', undefined, 'Demo: this would acknowledge orphaned pull request #88. In kew, run `kew tui`.'],
  ['POST', 'approvals/2055', { action: 'approve', reason: '' }, 'Demo: this would approve the hold on #2055 and let its dispatch start. In kew, run `kew approve 2055`.'],
  ['POST', 'config/budget.per_issue_usd', { value: 12, reason: '' }, 'Demo: this would override budget.per_issue_usd to 12.'],
  ['DELETE', 'config/budget.per_issue_usd', undefined, 'Demo: this would drop the override on budget.per_issue_usd and go back to kew.toml.'],
  ['POST', 'run-requests', { command: 'kew epic unhold launch-fixes 2055', item_id: '2055' }, 'Demo: this would ask the drive to run kew epic unhold launch-fixes 2055. In kew, run `kew epic unhold launch-fixes 2055`.'],
  ['POST', 'verbs/work/2055/action', { action: 'dispatch', expected_revision: 'r' }, 'Demo: this would dispatch for #2055. In kew, run `kew dispatch 2055`.'],
  ['POST', 'verbs/work/SFD-229/action', { action: 'finalize_publication', expected_revision: 'r' }, 'Demo: this would finalize publication for SFD-229. In kew, run `kew tui`.'],
  ['POST', 'prompts/dispatch/versions', { body: 'x' }, 'Demo: this would propose a new version of the dispatch prompt for approval. In kew, run `kew prompts propose dispatch --file addendum.md`.'],
  ['POST', 'prompts/dispatch/versions/3/approve', { generation: 2 }, 'Demo: this would approve version 3 of the dispatch prompt. In kew, run `kew prompts approve dispatch 3 --generation 2`.'],
  ['POST', 'prompts/dispatch/versions/3/decline', {}, 'Demo: this would decline version 3 of the dispatch prompt. In kew, run `kew prompts decline dispatch 3`.'],
  ['POST', 'prompts/dispatch/retire', { generation: 2 }, "Demo: this would retire the dispatch prompt's approved version. In kew, run `kew prompts retire dispatch --generation 2`."],
  ['POST', 'palette/ask', { query: 'x', entries: [] }, 'Demo: this would ask the model which command answers your question.'],
  ['POST', 'auth/login', { token: 't' }, 'Demo: this would sign you in to this console.']
];

describe('the demo write table', () => {
  it.each(CASES)('%s %s says what it would do', (method, path, body, sentence) => {
    expect(demoRefusal({ method, path, body })).toBe(sentence);
    expect(sentence).toMatch(SENTENCE);
  });

  it('has one row per route the console writes to, and no row nothing matches', () => {
    for (const row of WRITES) {
      const hit = CASES.some(([method, path]) => row.method === method && row.pattern.test(path));
      expect(hit, String(row.pattern)).toBe(true);
    }
  });

  it('names no command for a run request the drive would refuse', () => {
    expect(demoRefusal({ method: 'POST', path: 'run-requests', body: { command: 'rm -rf /' } })).toBe(
      'Demo: this would ask the drive to run rm -rf /.'
    );
  });

  it('decodes an id the path encoded', () => {
    expect(demoRefusal({ method: 'POST', path: 'prompts/a%20b/versions', body: {} })).toContain(
      'the a b prompt'
    );
  });

  it('refuses a write it does not know as a sentence too', () => {
    expect(demoRefusal({ method: 'POST', path: 'verbs/no-change/j1/confirm', body: {} })).toBe(UNLISTED_WRITE);
    expect(demoRefusal({ method: 'GET', path: 'fleet/stop', body: {} })).toBe(UNLISTED_WRITE);
  });
});
