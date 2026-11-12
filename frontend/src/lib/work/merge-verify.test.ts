/**
 * #3140 — the merge verify reading: why the merge verify refused this PR.
 *
 * The component is server-rendered, as in `pipeline.test.ts`, so the ids, the
 * head and the category are read off the markup the section really produces.
 */
import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import type { ItemPipeline, MergeVerify, MergeVerifyHeldOut } from '$lib/api';
import MergeVerifySection from './MergeVerify.svelte';
import {
  MERGE_VERIFY_UNREADABLE,
  NO_MERGE_VERIFY,
  UNREADABLE_MERGE_VERIFY,
  idBreaks,
  mergeVerifyReading
} from './merge-verify';
import failedJson from '../../../fixtures/factory/work-2055-pipeline.json';
import passedJson from '../../../fixtures/factory/work-2047-pipeline.json';
import hostJson from '../../../fixtures/factory/work-SFD-229-pipeline.json';
import idleJson from '../../../fixtures/factory/work-2060-pipeline.json';

const FAILED: ItemPipeline = failedJson;
const PASSED: ItemPipeline = passedJson;
const HOST: ItemPipeline = hostJson;
const IDLE: ItemPipeline = idleJson;

function draw(pipeline: ItemPipeline | null): string {
  // Hydration markers are Svelte's, not the section's: read the markup bare.
  return render(MergeVerifySection, { props: { pipeline } }).body.replace(/<!--[^>]*-->/g, '');
}

/** The markup's text as a reader gets it: tags dropped, entities decoded. */
function text(html: string): string {
  return html
    .replace(/<[^>]+>/g, '')
    .replace(/&#39;/g, "'")
    .replace(/&amp;/g, '&')
    .replace(/\s+/g, ' ')
    .trim();
}

/** The failed fixture with every stage's person taken out of it. */
function machineOwned(pipeline: ItemPipeline): ItemPipeline {
  return {
    ...pipeline,
    lanes: pipeline.lanes.map((lane) => ({
      ...lane,
      stages: lane.stages.map((s) => (s.state === 'needs_you' ? { ...s, state: 'pending' } : s))
    }))
  };
}

/** *base* with its verify replaced field by field. */
function withVerify(base: ItemPipeline, over: Partial<MergeVerify>): ItemPipeline {
  return { ...base, merge_verify: { ...base.merge_verify!, ...over } };
}

function heldOut(over: Partial<MergeVerifyHeldOut>): MergeVerifyHeldOut {
  return {
    outcome: 'passed',
    failed: [],
    declared: [],
    undeclared: [],
    failed_more: 0,
    declared_more: 0,
    undeclared_more: 0,
    ...over
  };
}

describe('mergeVerifyReading', () => {
  it('names the head, counts the undeclared failures and lists every id, undeclared first', () => {
    const reading = mergeVerifyReading(FAILED);
    expect(reading?.sentence).toBe(
      "Merge verify failed at 3670e43f: 2 of main's tests fail on this PR."
    );
    expect(reading?.head).toBe('3670e43f');
    expect(reading?.tests).toEqual([
      {
        id: 'tests/test_queue.py::test_lease_expires_after_ttl',
        excused: false
      },
      { id: 'tests/test_queue.py::test_claim_is_exclusive', excused: false },
      { id: 'tests/test_queue.py::test_retry_label_is_renamed', excused: true }
    ]);
    // A held-out refusal is no host failure: kew records no category for it.
    expect(reading?.category).toBe('');
  });

  it('is magenta only when a stage already says a person is next', () => {
    expect(mergeVerifyReading(FAILED)?.status).toBe('needs_you');
    expect(mergeVerifyReading(machineOwned(FAILED))?.status).toBe('failed');
  });

  it('says the head a passed verify ran at', () => {
    const reading = mergeVerifyReading(PASSED);
    expect(reading?.status).toBe('done');
    expect(reading?.sentence).toBe('Merge verify passed at c4e19a07.');
    expect(reading?.tests).toEqual([]);
  });

  it('says a verify ran on an older head, and names that head', () => {
    const passed = mergeVerifyReading(withVerify(PASSED, { is_current_head: false }));
    expect(passed?.sentence).toBe(
      'Merge verify passed at an older head, c4e19a07; the pull request has moved since.'
    );
    expect(passed?.head).toBe('c4e19a07');
    const failed = mergeVerifyReading(withVerify(FAILED, { is_current_head: false }));
    expect(failed?.sentence).toBe(
      "Merge verify failed at an older head, 3670e43f: 2 of main's tests fail on that head."
    );
  });

  it('reads an unfinished verify as unknown, never as passed, with its host category', () => {
    expect(mergeVerifyReading(HOST)).toMatchObject({
      status: 'unknown',
      sentence: 'Merge verify did not finish at an older head, b71c04e2.',
      head: 'b71c04e2',
      category: 'host_job_deadline_exceeded'
    });
    const odd = withVerify(PASSED, { outcome: 'incomplete' });
    expect(mergeVerifyReading(odd)?.sentence).toBe('Merge verify did not finish at c4e19a07.');
  });

  it('words one failure in the singular, and a failure with no held-out phase by head', () => {
    const one = withVerify(FAILED, {
      held_out: heldOut({
        outcome: 'failed',
        failed: ['tests/a.py::test_one'],
        undeclared: ['tests/a.py::test_one']
      })
    });
    expect(mergeVerifyReading(one)?.sentence).toBe(
      "Merge verify failed at 3670e43f: 1 of main's tests fails on this PR."
    );
    const none = withVerify(FAILED, { held_out: null });
    expect(mergeVerifyReading(none)?.sentence).toBe('Merge verify failed at 3670e43f.');
  });

  it('gives each held-out outcome of a failed verify its own sentence', () => {
    const failedWith = (h: MergeVerifyHeldOut) =>
      mergeVerifyReading(withVerify(FAILED, { held_out: h }))?.sentence;
    // not_run only says the phase reported nothing: a preflight, image build or
    // setup failure ends there as readily as the PR's gate, so nothing is blamed.
    expect(failedWith(heldOut({ outcome: 'not_run' }))).toBe(
      "Merge verify failed at 3670e43f before main's tests ran."
    );
    expect(failedWith(heldOut({ outcome: 'passed' }))).toBe(
      "Merge verify failed at 3670e43f: the PR's own checks failed; main's tests pass on it."
    );
    expect(
      failedWith(
        heldOut({
          outcome: 'passed',
          failed: ['tests/a.py::t'],
          declared: ['tests/a.py::t']
        })
      )
    ).toBe(
      "Merge verify failed at 3670e43f: the PR's own checks failed; main's tests fail only where a behavior change is declared."
    );
    expect(failedWith(heldOut({ outcome: 'skipped' }))).toBe(
      "Merge verify failed at 3670e43f: the PR's own checks failed, and main's tests were skipped."
    );
    expect(failedWith(heldOut({ outcome: 'failed' }))).toBe(
      "Merge verify failed at 3670e43f: main's tests did not run to a verdict on it."
    );
  });

  it('says when a passed verify skipped main’s tests', () => {
    const skipped = withVerify(PASSED, {
      held_out: heldOut({ outcome: 'skipped' })
    });
    expect(mergeVerifyReading(skipped)?.sentence).toBe(
      "Merge verify passed at c4e19a07, with main's tests skipped."
    );
  });

  it('has nothing to say for an idle item or a server that predates the field', () => {
    expect(IDLE.merge_verify).toBeNull();
    expect(mergeVerifyReading(IDLE)).toBeNull();
    const { merge_verify: _drop, ...old } = PASSED;
    expect(mergeVerifyReading(old)).toBeNull();
    expect(mergeVerifyReading(null)).toBeNull();
  });

  it('never claims no verify exists: none is linked to the heads, without a "yet"', () => {
    const reading = mergeVerifyReading({ ...PASSED, merge_verify: null });
    expect(reading).toMatchObject({
      status: 'pending',
      sentence: NO_MERGE_VERIFY
    });
    expect(NO_MERGE_VERIFY).toBe("No merge verify is linked to this pull request's heads.");
    expect(NO_MERGE_VERIFY).not.toMatch(/recorded|yet/);
  });

  it('says the record could not be read when the server names the read as unavailable', () => {
    const unread = {
      ...PASSED,
      merge_verify: null,
      unavailable: [UNREADABLE_MERGE_VERIFY]
    };
    expect(UNREADABLE_MERGE_VERIFY).toBe('pipeline:merge_verify');
    expect(mergeVerifyReading(unread)).toMatchObject({
      status: 'unknown',
      sentence: MERGE_VERIFY_UNREADABLE
    });
    expect(MERGE_VERIFY_UNREADABLE).toBe('The merge verify record could not be read.');
  });

  it('cuts a test id after every "::" and "/", losing nothing', () => {
    const id = 'kew/tests/test_queue.py::TestLease::test_expires[a/b]';
    expect(idBreaks(id)).toEqual([
      'kew/',
      'tests/',
      'test_queue.py::',
      'TestLease::',
      'test_expires[a/',
      'b]'
    ]);
    expect(idBreaks(id).join('')).toBe(id);
    expect(idBreaks('plain')).toEqual(['plain']);
  });
});

describe('MergeVerify.svelte', () => {
  it('prints the failing ids in Plex Mono beneath the sentence', () => {
    const html = draw(FAILED);
    expect(html).toContain('aria-label="Merge verify"');
    expect(text(html)).toContain(
      "Merge verify failed at 3670e43f: 2 of main's tests fail on this PR."
    );
    expect(html.match(/<span class="font-mono [^"]*" data-held-out-test/g)).toHaveLength(3);
  });

  it('breaks each id after "::" and "/" with <wbr>, never by injected HTML', () => {
    const html = draw(FAILED);
    expect(html).toContain(
      'data-held-out-test="">tests/<wbr/>test_queue.py::<wbr/>test_claim_is_exclusive</span>'
    );
    expect(text(html)).toContain('tests/test_queue.py::test_claim_is_exclusive');
  });

  it('notes an excused failure in a sentence-case 13px line, after a real space', () => {
    const html = draw(FAILED);
    expect(html).toContain(
      '<span class="text-13 text-text-3" data-held-out-excused=""> — excused by a declared behavior change</span>'
    );
    expect(text(html)).toContain(
      'tests/test_queue.py::test_retry_label_is_renamed — excused by a declared behavior change'
    );
    expect(html).not.toMatch(/text-11[^>]*>[^<]*excused/);
  });

  it('words the error category, keeps its code in the title, and never uppercases it', () => {
    // #3466: the value used to print `host_job_deadline_exceeded` as recorded.
    const html = draw(HOST);
    const value = html.match(
      /<span class="([^"]*)" title="([^"]*)" data-merge-verify-category="">([^<]*)<\/span>/
    );
    expect(value?.[3]).toBe('The verify host hit its job deadline');
    expect(value?.[2]).toBe(HOST.merge_verify!.error_category);
    expect(html.replace(/title="[^"]*"/g, '')).not.toContain(HOST.merge_verify!.error_category);
    expect(value?.[1].split(' ')).toEqual(expect.arrayContaining(['normal-case', 'tracking-normal']));
  });

  it('sets the passed head in Plex Mono', () => {
    const html = draw(PASSED);
    expect(html).toContain('<span class="font-mono" data-merge-verify-head="">c4e19a07</span>');
    expect(html).not.toContain('data-held-out-test');
    expect(html).not.toContain('data-merge-verify-category');
  });

  it('counts the failures the server left off, in the sentence and beneath the list', () => {
    const ids = Array.from({ length: 50 }, (_, n) => `tests/test_many.py::test_${n}`);
    const capped = withVerify(FAILED, {
      held_out: heldOut({
        outcome: 'failed',
        failed: ids,
        undeclared: ids,
        failed_more: 21,
        undeclared_more: 21
      })
    });
    const reading = mergeVerifyReading(capped);
    expect(reading?.sentence).toBe(
      "Merge verify failed at 3670e43f: 71 of main's tests fail on this PR."
    );
    expect(reading?.more).toBe(21);
    const html = draw(capped);
    expect(html.match(/data-held-out-test=/g)).toHaveLength(50);
    expect(html).toContain(
      '<li class="text-13 text-text-3" data-held-out-more="">and 21 more</li>'
    );
    expect(draw(FAILED)).not.toContain('data-held-out-more');
  });

  it('draws nothing for an idle item', () => {
    expect(draw(IDLE)).not.toContain('Merge verify');
  });
});
