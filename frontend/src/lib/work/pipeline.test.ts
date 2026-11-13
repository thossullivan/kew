/**
 * FUI-20 — the Work pipeline swimlane: what the grid is allowed to claim.
 *
 * The three promises this suite exists for are the three ways a stage table
 * lies. It can invent a column the payload never sent (so the columns are the
 * payload's order, and only that); it can draw a pipeline for an item that has
 * none (so a missing lane renders NOTHING — not a dashed placeholder grid); and
 * it can vouch for a word it has never seen (so an unknown state draws pending,
 * never the green that means attested).
 *
 * As in `header.test.ts`, the component is SERVER-rendered, so the pixels are
 * read off the markup the section really produces rather than off a model that
 * claims to describe it — the standing check on DWork y ≈276–432 until FUI-02's
 * screenshot harness can shoot the artboard. The artboard itself is read below,
 * so the numbers are compared against its own text and the two deliberate
 * deviations (the Reproduce column, the release lane) stay recorded rather than
 * remembered.
 */
import { readFileSync } from 'node:fs';
import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import type { ItemPipeline, PipelineLane, PipelineStage } from '$lib/api';
import { laneView, markerFor } from '$lib/factory/swimlane';
import Pipeline from './Pipeline.svelte';
import fixtureJson from '../../../fixtures/factory/work-2055-pipeline.json';
import idleJson from '../../../fixtures/factory/work-2060-pipeline.json';
import {
  PIPELINE_LEGEND,
  PIPELINE_VOCABULARY,
  NO_RUN_YET,
  pipelineCell,
  pipelineColumns,
  pipelineLane,
  pipelineModel,
  pipelineNotice,
  pipelineReadFailure
} from './pipeline';

const DWORK = readFileSync(
  new URL('../../../design/factory-ui/DWork.html', import.meta.url),
  'utf8'
);

/** The committed FUI-11 payload, which is the DWork main lane. */
const FIXTURE: ItemPipeline = fixtureJson;
/** #2060, a ready-queue item nothing has run for, as the route serves it (#2572). */
const IDLE: ItemPipeline = idleJson;

const stage = (over: Partial<PipelineStage> = {}): PipelineStage => ({
  stage: 'implement',
  state: 'pending',
  label: 'not started',
  detail: '',
  evidence: null,
  ...over
});

const lane = (name: string, stages: PipelineStage[]): PipelineLane => ({ name, stages });

const payload = (lanes: PipelineLane[], unavailable: string[] = []): ItemPipeline => ({
  item_id: '2055',
  lanes,
  unavailable,
  composed_at: '2026-09-24T18:12:04+00:00'
});

/** The rendered section, comments stripped and whitespace between tags gone. */
function draw(props: { pipeline: ItemPipeline | null; notice?: string }): string {
  return render(Pipeline, { props })
    .body.replace(/<!--[\s\S]*?-->/g, '')
    .replace(/>\s+</g, '><');
}

describe('pipelineColumns', () => {
  it('takes the payload’s order, not a table of the stages kew serves today', () => {
    expect(pipelineColumns(FIXTURE.lanes).map((c) => c.stage)).toEqual([
      'implement',
      'publish',
      'review',
      'merge'
    ]);
    // The same four stages, sent in another order, come back in THAT order.
    const reversed = [...FIXTURE.lanes[0].stages].reverse();
    expect(pipelineColumns([lane('main', reversed)]).map((c) => c.stage)).toEqual([
      'merge',
      'review',
      'publish',
      'implement'
    ]);
  });

  it('appends a later lane’s new stages after the ones already named', () => {
    const columns = pipelineColumns([
      lane('main', [stage({ stage: 'implement' }), stage({ stage: 'merge' })]),
      lane('release/1.2', [stage({ stage: 'merge' }), stage({ stage: 'backport' })])
    ]);
    expect(columns.map((c) => c.stage)).toEqual(['implement', 'merge', 'backport']);
  });

  it('gives a lane that names one stage twice a column for each cell', () => {
    const columns = pipelineColumns([
      lane('main', [stage({ stage: 'review' }), stage({ stage: 'review' })])
    ]);
    expect(columns.map((c) => [c.stage, c.occurrence])).toEqual([
      ['review', 0],
      ['review', 1]
    ]);
  });

  it('has no columns for a payload with no lanes, and none for an empty lane', () => {
    expect(pipelineColumns([])).toEqual([]);
    expect(pipelineColumns([lane('main', [])])).toEqual([]);
    expect(pipelineColumns(null)).toEqual([]);
  });
});

describe('pipelineCell', () => {
  const cells = FIXTURE.lanes[0].stages.map(pipelineCell);

  it('carries the server’s two lines verbatim', () => {
    expect(cells[0]).toMatchObject({
      state: 'done',
      primary: 'run f5dcb6a5',
      secondary: 'success'
    });
  });

  it('colours the line that IS the status word, as the artboard does', () => {
    // "run f5dcb6a5" over a green "success": the second line.
    expect(cells[0].statusLine).toBe('secondary');
    expect(cells[2].statusLine).toBe('secondary');
    // A magenta "needs you" over a quiet "merge decision": the first.
    expect(cells[3].statusLine).toBe('primary');
    // A cell with one line is that line, so pending stays the quiet word.
    expect(pipelineCell(stage()).statusLine).toBe('primary');
    expect(pipelineCell(stage({ state: 'not_required', label: 'not required' })).statusLine).toBe(
      'primary'
    );
  });
});

describe('pipelineLane', () => {
  it('aligns a lane’s cells to the columns and leaves the rest inactive', () => {
    const main = lane('main', [stage({ stage: 'implement' }), stage({ stage: 'merge' })]);
    const release = lane('release/1.2', [stage({ stage: 'backport', state: 'awaiting' })]);
    const columns = pipelineColumns([main, release]);
    // Three columns; the release lane named only the third of them, so the two
    // it sat out are inactive — the grid's own statement, not a pending claim.
    expect(pipelineLane(release, columns).cells.map((c) => c.state)).toEqual([
      null,
      null,
      'awaiting'
    ]);
    expect(pipelineLane(main, columns).cells.map((c) => c.state)).toEqual([
      'pending',
      'pending',
      null
    ]);
  });

  it('names a lane nobody is running quiet, and one with live work loud', () => {
    expect(pipelineLane(FIXTURE.lanes[0], pipelineColumns(FIXTURE.lanes)).muted).toBe(false);
    const idle = lane('release/1.2', [stage({ state: 'pending' }), stage({ state: 'unknown' })]);
    expect(pipelineLane(idle, pipelineColumns([idle])).muted).toBe(true);
  });
});

describe('pipelineModel', () => {
  it('builds the DWork main lane out of the committed payload', () => {
    const model = pipelineModel(FIXTURE);
    expect(model.empty).toBe(false);
    expect(model.stages).toEqual(['implement', 'publish', 'review', 'merge']);
    expect(model.lanes.map((l) => l.label)).toEqual(['main']);
    expect(model.lanes[0].cells).toHaveLength(4);
    expect(model.notice).toBe('');
  });

  it('is empty — never a grid of placeholders — when there is no lane', () => {
    for (const p of [null, undefined, payload([]), payload([lane('main', [])])]) {
      const model = pipelineModel(p);
      expect(model.empty).toBe(true);
      expect(model.lanes).toEqual([]);
      expect(model.stages).toEqual([]);
    }
  });

  it('reads an unknown stage state as pending, keeping the server’s own word', () => {
    const model = pipelineModel(payload([lane('main', [stage({ state: 'backported' })])]));
    // The cell keeps what came in; the MARKER is what refuses to vouch for it.
    expect(model.lanes[0].cells[0].state).toBe('backported');
    const marker = markerFor('backported', PIPELINE_VOCABULARY);
    expect(marker).toMatchObject({ status: 'pending', word: 'backported', known: false });
    expect(marker.mark.color).not.toBe('var(--success)');
    // And `unknown`, which is FUI-11's own word for a fact it could not read.
    expect(markerFor('unknown', PIPELINE_VOCABULARY)).toMatchObject({
      status: 'pending',
      known: false
    });
  });

  it('never lets a segment out of an unknown state claim attestation', () => {
    const model = pipelineModel(
      payload([lane('main', [stage({ state: 'backported' }), stage({ state: 'done' })])])
    );
    const view = laneView(model.lanes[0], 2, model.stages, PIPELINE_VOCABULARY);
    expect(view[0].segment.kind).toBe('pending');
  });
});

describe('pipelineModel — an item nothing has run for (#2572)', () => {
  it('draws the four pending stages and says no run yet', () => {
    const model = pipelineModel(IDLE);
    expect(model.empty).toBe(false);
    expect(model.stages).toEqual(['implement', 'publish', 'review', 'merge']);
    expect(model.lanes[0].cells.map((c) => c.state)).toEqual([
      'pending',
      'pending',
      'pending',
      'pending'
    ]);
    expect(model.noRun).toBe(true);
    expect(model.notice).toBe('');
  });

  it('never says no run yet over a run, a piece of evidence or an unread fact', () => {
    expect(pipelineModel(FIXTURE).noRun).toBe(false);
    expect(pipelineModel(payload([lane('main', [stage(), stage({ state: 'working' })])])).noRun).toBe(
      false
    );
    expect(
      pipelineModel(payload([lane('main', [stage({ evidence: { kind: 'run', ref: 'f5dcb6a5' } })])]))
        .noRun
    ).toBe(false);
    // "We could not look" is not "nothing happened".
    expect(pipelineModel(payload([lane('main', [stage()])], ['pipeline:audit'])).noRun).toBe(false);
    expect(pipelineModel(null).noRun).toBe(false);
    expect(pipelineModel(payload([lane('main', [])])).noRun).toBe(false);
  });
});

describe('pipelineNotice', () => {
  it('says nothing when the payload read everything', () => {
    expect(pipelineNotice([])).toBe('');
    expect(pipelineNotice(undefined)).toBe('');
  });

  it('names FUI-11’s reasons, and prints an unfamiliar one verbatim', () => {
    expect(pipelineNotice(['pipeline:audit'])).toContain(
      'Not every fact could be read: the audit chain'
    );
    expect(pipelineNotice(['pipeline:review_rounds', 'pipeline:judgment'])).toContain(
      'the review rounds, the judgment record'
    );
    // Verbatim, and un-capitalised: this is a token somebody may grep for.
    expect(pipelineNotice(['pipeline:tea_leaves'])).toContain('pipeline:tea_leaves');
  });
});

describe('pipelineReadFailure', () => {
  it('treats a 404 as the answer it is: this item has no pipeline', () => {
    const out = pipelineReadFailure(new Error('/api/work/9999/pipeline -> 404'));
    expect(out).toEqual({ absent: true, notice: '' });
  });

  it('says a failed read out loud, and never mistakes it for an absence', () => {
    expect(pipelineReadFailure(new Error('/api/work/2055/pipeline -> 500'))).toMatchObject({
      absent: false
    });
    expect(pipelineReadFailure(new Error('/api/work/2055/pipeline -> 500')).notice).toContain('500');
    expect(pipelineReadFailure(new TypeError('Failed to fetch'))).toMatchObject({ absent: false });
    expect(pipelineReadFailure(new TypeError('Failed to fetch')).notice).toContain(
      'could not be read'
    );
  });
});

describe('Pipeline.svelte', () => {
  it('draws an item nothing has run for as its stages and "No run yet" (#2572)', () => {
    const html = draw({ pipeline: IDLE });
    expect(NO_RUN_YET).toBe('No run yet');
    expect(html).toContain('aria-label="Pipeline"');
    expect(html).toContain('>implement</div>');
    expect(html).toContain('>merge</div>');
    expect(html).toContain(`>${NO_RUN_YET}</p>`);
    // An idle item is not a read failure: no warning line under it.
    expect(html).not.toContain('role="status"');
    expect(draw({ pipeline: FIXTURE })).not.toContain(NO_RUN_YET);
  });

  it('lays the section out on the artboard’s grid, minus the Reproduce column', () => {
    const html = draw({ pipeline: FIXTURE });
    // DWork draws five stage columns; kew has no Reproduce stage, so it
    // renders the four the payload sent beside the same 148px lane column.
    expect(DWORK).toContain('grid-template-columns: 148px repeat(5, minmax(0, 1fr));');
    expect(html).toContain('grid-template-columns: 148px repeat(4, minmax(0, 1fr));');
    expect(html).toContain('padding-top: 14px; padding-bottom: 16px;');
    expect(html).toContain('>Lane</div>');
    // The headings are the payload's words; `.text-11` upper-cases them.
    expect(html).toContain('>implement</div>');
    expect(html).toContain('>merge</div>');
    // The 6px tick that drops each column's rule into the first lane.
    expect(html).toContain('class="grid h-[6px]"');
    expect(html).toContain('border-l border-hair-strong');
    expect(html).toContain('aria-label="Pipeline"');
  });

  it('scrolls the lane inside its own box at phone width (#2372)', () => {
    const html = draw({ pipeline: FIXTURE });
    // 390px used to print "IMPLEMENTPUBLISHREVIEW" across four columns that
    // were narrower than their own labels. The lane now carries a floor and
    // scrolls under it; the page stays 390 because the scroll box's own
    // automatic minimum size is zero.
    expect(html).toContain('overflow-x-auto');
    expect(html).toContain(`min-width: ${148 + 4 * 112}px;`);
  });

  it('draws the DWork main lane’s marks, lines and caption colours', () => {
    const html = draw({ pipeline: FIXTURE });
    expect(html).toContain('left: 7px; top: 6px; border-top: 2px solid var(--success);');
    // Two solid-success lines, not one: #2279/D8 landed the fixture's review
    // round, so the line into the review cell is its own done tone. It was
    // `accent-2` (working) while that cell claimed round 1 was still reading a
    // pull request the merge cell beside it called a person's decision.
    expect(html.match(/border-top: 2px solid var\(--success\);/g)).toHaveLength(2);
    expect(html).toContain('left: 7px; top: 6px; border-top: 2px dashed var(--text-5);');
    // Merge leads nowhere: three lines for four cells.
    expect(html.match(/border-top: 2px/g)).toHaveLength(3);
    expect(html).toContain('color: var(--text-1);">run f5dcb6a5</span>');
    expect(html).toContain('color: var(--success);">success</span>');
    expect(html).toContain('color: var(--accent);">needs you</span>');
    expect(html).toContain('color: var(--text-3);">merge decision</span>');
    // One lane, named loud: nothing in DWork's release lane reaches kew.
    expect(html).toContain('text-text-1">main');
    expect(html).not.toContain('release/');
  });

  it('heads the section with the artboard’s four legend words, and not ready', () => {
    const html = draw({ pipeline: FIXTURE });
    // `not ready` postdates DWork: a not-ready review round is amber (#3466).
    const added = new Set(['not_ready']);
    for (const entry of PIPELINE_LEGEND) {
      if (!added.has(entry.key)) expect(DWORK).toContain(`>${entry.label}</span>`);
      expect(html).toContain(entry.label);
    }
    expect(PIPELINE_LEGEND.filter((e) => !added.has(e.key))).toHaveLength(4);
    expect(html).toContain('>Pipeline</h2>');
    // The legend is a key, not a shout: sentence case in the head's meta slot.
    expect(html).toContain('normal-case');
  });

  it('renders nothing at all for an item with no pipeline', () => {
    for (const p of [null, payload([]), payload([lane('main', [])])]) {
      const html = draw({ pipeline: p });
      expect(html).not.toContain('Pipeline');
      expect(html).not.toContain('dashed');
      expect(html.trim()).toBe('');
    }
  });

  it('draws an unknown state as the dashed pending ring, with its own word', () => {
    const html = draw({
      pipeline: payload([lane('main', [stage({ stage: 'backport', state: 'backported' })])])
    });
    expect(html).toContain('aria-label="backported"');
    expect(html).toContain('stroke-dasharray');
    // Nothing in the LANES is green — the legend's own attested dot is, which
    // is why the claim is read off the swimlane rather than off the section.
    const lanes = html.slice(html.indexOf('role="group"'));
    expect(lanes).not.toContain('var(--success)');
  });

  it('prints what the payload could not read under the lanes it did', () => {
    const html = draw({ pipeline: payload(FIXTURE.lanes, ['pipeline:audit']) });
    expect(html).toContain('Not every fact could be read: the audit chain');
    expect(html).toContain('role="status"');
    expect(html).toContain('text-text-1">main');
  });

  it('keeps the head and the reason when the read itself failed', () => {
    const html = draw({ pipeline: null, notice: 'The pipeline could not be read (500).' });
    expect(html).toContain('>Pipeline</h2>');
    expect(html).toContain('The pipeline could not be read (500).');
    // No grid: there is no payload, so there are no lanes to claim.
    expect(html).not.toContain('>Lane</div>');
  });
});
