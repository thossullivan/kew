/**
 * #3466 (D25, D26) — Work's pipeline colours mean one thing. SFD-229 published
 * with no scan recorded and its review round ended "needs changes": the publish
 * cell drew the green success dot, and the review cell the cyan working halo.
 * Server-rendered, as in `pipeline.test.ts`, so the colours are read off the
 * markup the section really produces.
 */
import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import type { ItemPipeline } from '$lib/api';
import Pipeline from './Pipeline.svelte';
import { mergeVerifyReading, NO_MERGE_VERIFY } from './merge-verify';
import sfd229Json from '../../../fixtures/factory/work-SFD-229-pipeline.json';

const SFD_229 = sfd229Json as unknown as ItemPipeline;

/** The markup of one stage cell, from its `data-stage` to the next cell's. */
function cell(html: string, stage: string): string {
  const start = html.indexOf(`data-stage="${stage}"`);
  expect(start, `no ${stage} cell`).toBeGreaterThan(-1);
  const next = html.indexOf('data-stage="', start + 1);
  return html.slice(start, next === -1 ? undefined : next);
}

/** The inline colour of the caption that reads `text`. */
function captionColour(markup: string, text: string): string | null {
  const re = new RegExp(`data-caption="[a-z]+"[^>]*style="color: ([^;"]+);?"[^>]*>${text}<`);
  return re.exec(markup)?.[1] ?? null;
}

describe('SFD-229’s pipeline cells', () => {
  const html = render(Pipeline, { props: { pipeline: SFD_229 } }).body;

  it('does not draw a publish nobody scanned in the success green', () => {
    const publish = cell(html, 'publish');
    expect(publish).toContain('no scan recorded');
    expect(publish).not.toContain('var(--success)');
    expect(captionColour(publish, 'no scan recorded')).toBe('var(--text-3)');
  });

  it('draws a not-ready review round amber, not the cyan of work in flight', () => {
    const review = cell(html, 'review');
    expect(captionColour(review, 'needs changes')).toBe('var(--warn)');
    expect(review).toContain('var(--warn)');
    expect(review).not.toContain('var(--accent-2)');
    // The half-filled ring: a ring and the filled half of its disc.
    expect(review).toMatch(/<path d="M [\d.]+ [\d.]+ A [^"]+ Z" fill="var\(--warn\)"/);
  });

  it('keeps the path through the unscanned publish: attested in, working out', () => {
    // #3466: a pending publish stopped the path, so implement→publish and
    // publish→review drew the dashed never-reached line under a PR that is up.
    const lines = [...html.matchAll(/border-top: (2px (?:solid|dashed) var\(--[\w-]+\))/g)].map(
      (m) => m[1]
    );
    expect(lines).toEqual([
      '2px solid var(--success)',
      '2px solid var(--accent-2)',
      '2px dashed var(--text-5)'
    ]);
  });

  it('keys the amber mark in the legend', () => {
    expect(html).toMatch(/aria-label="not ready"/);
  });
});

describe('a published PR nobody scanned', () => {
  it('is still published to the merge verify reading, though its cell is not green', () => {
    // The cell's state is `pending` now; the PR is up all the same, so a PR
    // with no verify linked says so rather than vanishing.
    const unlinked = { ...SFD_229, merge_verify: null };
    expect(mergeVerifyReading(unlinked)).toMatchObject({
      status: 'pending',
      sentence: NO_MERGE_VERIFY
    });
  });

  it('is published by its `passed` field, never by the worded label', () => {
    const relabel = (label: string, passed?: boolean): ItemPipeline => ({
      ...SFD_229,
      merge_verify: null,
      lanes: SFD_229.lanes.map((lane) => ({
        ...lane,
        stages: lane.stages.map((stage) =>
          stage.stage === 'publish' ? { ...stage, label, passed } : stage
        )
      }))
    });
    // Reworded, still up: the reading still says no verify is linked.
    expect(mergeVerifyReading(relabel('pushed', true))).toMatchObject({
      sentence: NO_MERGE_VERIFY
    });
    // The old word without the field is a pending stage, not a published PR.
    expect(mergeVerifyReading(relabel('published', undefined))).toBeNull();
  });
});
