/**
 * FUI-17 — what the selected-node rail may claim, per column.
 *
 * The rail's whole job is to survive a partial factory: four columns, four
 * reads, and one of them being down must cost exactly one column. So most of
 * what is pinned here is degradation — which column dashes, what it says when
 * it does, and which facts it refuses to borrow from a neighbour to fill a gap.
 */
import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import { plainCommands } from '$lib/ui/command-markup';
import SelectedRail from './SelectedRail.svelte';
import type {
  EpicDetail,
  EpicNode,
  EpicNodeDetail,
  ItemEvidence,
  ItemPipeline,
  WorkView
} from '$lib/api';
import epicJson from '../../../fixtures/factory/epics-launch-fixes.json';
import nodeDetailJson from '../../../fixtures/factory/epics-launch-fixes-nodes-2055.json';
import evidenceJson from '../../../fixtures/factory/work-2055-evidence.json';
import pipelineJson from '../../../fixtures/factory/work-2055-pipeline.json';
import workViewJson from '../../../fixtures/factory/verbs-work-2055.json';
import {
  dependsOn,
  nodeMoment,
  railAttention,
  railCta,
  railEvidence,
  railModel,
  railPipeline,
  railSelected,
  RAIL_DASH,
  RAIL_EMPTY,
  RAIL_REPLAY_NOTE,
  RAIL_REPLAY_UNREAD,
  EVIDENCE_ABSENT,
  PIPELINE_ABSENT,
  railSource,
  workHref,
  type RailInput
} from './rail';

const node = (over: Partial<EpicNode> = {}): EpicNode => ({
  id: '2055',
  title: 'review gate lows',
  status: 'in_review',
  status_since: '2026-09-24T15:00:00Z',
  url: 'https://github.com/o/r/issues/2055',
  pr_url: 'https://github.com/o/r/pull/2059',
  external: false,
  stuck: null,
  attention: null,
  blocked_reason: null,
  ...over
});

const detail = (lifetime = 3.1, drain = 0.4): EpicNodeDetail => ({
  id: '2055',
  runs: [],
  lifetime_usd: lifetime,
  drain_usd: drain
});

const view = (containment = 'contained'): WorkView =>
  ({ route: { runner: 'claude', model: 'opus', containment, provenance: 'declared' } }) as WorkView;

const pipeline = (over: Partial<ItemPipeline> = {}): ItemPipeline => ({
  item_id: '2055',
  lanes: [
    {
      name: 'main',
      stages: [
        { stage: 'implement', state: 'done', label: 'f5dcb6a5', detail: 'success', evidence: null },
        { stage: 'merge', state: 'needs_you', label: 'needs you', detail: '', evidence: null }
      ]
    }
  ],
  unavailable: [],
  composed_at: '2026-09-24T15:47:00Z',
  ...over
});

const evidence = (over: Partial<ItemEvidence> = {}): ItemEvidence => ({
  item_id: '2055',
  rows: [
    {
      n: 1,
      label: 'sandboxed',
      event: 'sandbox_resolved',
      summary: 'contained',
      ts: '2026-09-24T15:24:15Z',
      state: 'attested'
    },
    {
      n: 2,
      label: 'verdict',
      event: 'adversarial_review_verdict',
      summary: 'not recorded',
      ts: '',
      state: 'pending'
    }
  ],
  attested_count: 1,
  total: 2,
  chain_verified: true,
  reason: 'one guarantee is still pending.',
  unavailable: [],
  composed_at: '2026-09-24T15:47:00Z',
  ...over
});

const input = (over: Partial<RailInput> = {}): RailInput => ({
  epicRef: 'factory-ui',
  node: node(),
  edges: [
    ['2047', '2055'],
    ['2055', '2060']
  ],
  unavailable: [],
  detail: detail(),
  view: view(),
  pipeline: pipeline(),
  evidence: evidence(),
  ...over
});

const unread = (reason: string) => ({ available: false as const, reason });

describe('the empty rail', () => {
  it('shows a quiet hint and selects nothing on its own', () => {
    const model = railModel(input({ node: null }));
    expect(model.empty).toBe(true);
    expect(model.hint).toBe(RAIL_EMPTY);
    expect(model.selected).toBeNull();
    // Not "the first node", not "the one that needs you": no columns at all.
    expect([model.pipeline, model.evidence, model.attention]).toEqual([null, null, null]);
  });

  it('stays empty even when every read landed', () => {
    // The reads are for a selection that does not exist; a model that drew
    // them would be showing an item the user never picked.
    expect(railModel(input({ node: null })).pipeline).toBeNull();
  });
});

describe('the Selected column', () => {
  it('takes the dollar amount from lifetime_usd, not the drain', () => {
    const selected = railSelected(input({ detail: detail(3.1, 0.4) }))!;
    expect(selected.spend).toEqual({ value: '$3.10', reason: '' });
  });

  it('dashes the spend, with a reason, when the node read failed', () => {
    const selected = railSelected(input({ detail: unread('node spend unavailable (HTTP 503)') }))!;
    expect(selected.spend.value).toBe(RAIL_DASH);
    expect(selected.spend.reason).toBe('node spend unavailable (HTTP 503)');
  });

  it('shows the declared containment, and dashes it when the view is unread', () => {
    expect(railSelected(input())!.containment.value).toBe('contained');
    const without = railSelected(input({ view: unread('route unavailable') }))!;
    expect(without.containment).toEqual({ value: RAIL_DASH, reason: 'route unavailable' });
  });

  it('renders a GitHub id as a hash ref and a Linear key bare', () => {
    expect(railSelected(input())!.ref).toBe('#2055');
    expect(railSelected(input({ node: node({ id: 'SFD-12' }) }))!.ref).toBe('SFD-12');
  });

  it('links into Work by the opaque item id', () => {
    const selected = railSelected(input({ node: node({ id: 'SFD-12' }) }))!;
    expect(selected.workHref).toBe('/work?item=SFD-12');
    expect(selected.workLabel).toBe('Open SFD-12 in Work');
    expect(workHref('a/b')).toBe('/work?item=a%2Fb');
  });

  it('names dependencies from the edges pointing AT the node, and its PR', () => {
    expect(dependsOn('2055', input().edges)).toEqual(['2047']);
    expect(railSelected(input())!.relations).toBe('depends on #2047 · PR #2059');
  });

  it('says nothing about relations a node has not got', () => {
    const lone = railSelected(input({ node: node({ pr_url: null }), edges: [] }))!;
    expect(lone.relations).toBe('');
  });

  it('keeps the kew epic retry hint for a failed node, and only for one', () => {
    const failed = railSelected(
      input({
        node: node({
          status: 'failed',
          stuck: 'tests failed twice',
          recovery_command: 'kew epic retry factory-ui 2055'
        })
      })
    )!;
    expect(failed.retry).toBe('kew epic retry factory-ui 2055');
    expect(failed.retryReason).toBe('tests failed twice');
    expect(railSelected(input())!.retry).toBe('');
  });

  // The host died under the node twice while it kept preserved work (#2960):
  // the server serves continue, and retry would be refused.
  const PRESERVED = 'failed-terminal (exit 137, host_restart_repeated, preserved work)';
  const preserved = (recovery_command: string | undefined, attention: boolean) =>
    node({
      status: 'failed',
      stuck: PRESERVED,
      ...(recovery_command === undefined ? {} : { recovery_command }),
      attention: attention
        ? {
            kind: 'held',
            reason: 'the host restarted under this node twice; its work is preserved',
            url: null,
            action: 'check why the host keeps going down, then kew epic continue the node',
            command: 'kew epic continue factory-ui 2055'
          }
        : null
    });

  it("prints the server's continue for preserved work, never retry", () => {
    const selected = railSelected(
      input({ node: preserved('kew epic continue factory-ui 2055', true) })
    )!;
    expect(selected.retry).toBe('kew epic continue factory-ui 2055');
    expect(selected.retry).not.toContain('retry');
  });

  it('prints no command when the server served none for a failed node', () => {
    expect(railSelected(input({ node: preserved('', true) }))!.retry).toBe('');
  });

  // Attention clears while a continuation executes the preserved work, and
  // the journaled snapshot still reads failed: no command is invented then.
  it('invents no retry when attention is cleared on a failed node', () => {
    expect(railSelected(input({ node: preserved('', false) }))!.retry).toBe('');
    expect(railSelected(input({ node: preserved(undefined, false) }))!.retry).toBe('');
  });
});

describe('the Pipeline column', () => {
  it('names the lane the payload named', () => {
    expect(railPipeline(input())!.heading).toBe('Pipeline · main');
  });

  it('draws the payload stages in the payload order', () => {
    expect(railPipeline(input())!.stages).toEqual(['implement', 'merge']);
  });

  it('gives an em dash and a reason when the pipeline could not be read', () => {
    const column = railPipeline(input({ pipeline: unread('pipeline unavailable (HTTP 500)') }))!;
    expect(column.placeholder).toBe(RAIL_DASH);
    expect(column.reason).toBe('pipeline unavailable (HTTP 500)');
    expect(column.lanes).toEqual([]);
  });

  it('separates "no pipeline recorded" from "could not be read"', () => {
    const column = railPipeline(input({ pipeline: pipeline({ lanes: [] }) }))!;
    expect(column.reason).toBe(PIPELINE_ABSENT);
  });

  it('confesses a partial read beside a grid it did draw', () => {
    const column = railPipeline(
      input({ pipeline: pipeline({ unavailable: ['pipeline:review_rounds'] }) })
    )!;
    expect(column.placeholder).toBe('');
    expect(column.notice).toContain('the review rounds');
  });

  it('never prints both a dash reason and a notice', () => {
    const column = railPipeline(
      input({ pipeline: pipeline({ lanes: [], unavailable: ['pipeline:audit'] }) })
    )!;
    expect(column.notice).toBe('');
    expect(column.reason).toBe(PIPELINE_ABSENT);
  });

  it('degrades on its own — the other columns are untouched', () => {
    const model = railModel(input({ pipeline: unread('pipeline unavailable') }));
    expect(model.pipeline!.placeholder).toBe(RAIL_DASH);
    expect(model.selected!.spend.value).toBe('$3.10');
    expect(model.evidence!.count).toBe('1 / 2 attested');
  });
});

describe('the Evidence column', () => {
  it('reads the count from the server, not from the rows it drew', () => {
    expect(railEvidence(input())!.count).toBe('1 / 2 attested');
  });

  it('zero-pads the position and shows the time, or the state word', () => {
    const rows = railEvidence(input())!.rows;
    expect(rows[0].n).toBe('01');
    expect(rows[0].right).toBe('15:24:15');
    // No row recorded: the state's own word, never a borrowed timestamp.
    expect(rows[1].right).toBe('pending');
  });

  it('refuses attestation on a chain that does not verify', () => {
    const column = railEvidence(
      input({ evidence: evidence({ chain_verified: false, attested_count: 1 }) })
    )!;
    expect(column.attested).toBe(0);
    expect(column.count).toBe('0 / 2 attested');
    expect(column.rows.map((r) => r.state)).toEqual(['unverified', 'pending']);
  });

  it('gives an em dash and a reason when the chain could not be read', () => {
    const column = railEvidence(input({ evidence: unread('evidence unavailable (HTTP 503)') }))!;
    expect(column.count).toBe(RAIL_DASH);
    expect(column.reason).toBe('evidence unavailable (HTTP 503)');
  });

  it('separates an empty chain from an unread one', () => {
    const column = railEvidence(input({ evidence: evidence({ rows: [], total: 0 }) }))!;
    expect(column.reason).toBe(EVIDENCE_ABSENT);
  });

  it('prints the server sentence beside rows it did draw', () => {
    expect(railEvidence(input())!.notice).toBe('one guarantee is still pending.');
  });
});

describe('railSource', () => {
  it('reads a 404 as the absent answer, not as a failure', () => {
    expect(railSource('pipeline', PIPELINE_ABSENT, new Error('/api/x -> 404'))).toEqual({
      available: false,
      reason: PIPELINE_ABSENT
    });
  });

  it('names any other status as a read that failed', () => {
    expect(railSource('evidence', EVIDENCE_ABSENT, new Error('/api/x -> 503'))).toEqual({
      available: false,
      reason: 'evidence unavailable (HTTP 503)'
    });
  });

  it('says a transport failure without inventing a status', () => {
    expect(railSource('pipeline', PIPELINE_ABSENT, new Error('network down'))).toEqual({
      available: false,
      reason: 'pipeline unavailable'
    });
  });
});

describe('the Needs you column', () => {
  it('is hidden when nobody is waiting', () => {
    expect(railAttention(input())).toBeNull();
    expect(railModel(input()).attention).toBeNull();
  });

  it('stays, and confesses, when the attention join could not be read', () => {
    const column = railAttention(input({ unavailable: ['attention:approval_holds'] }))!;
    expect(column.notice).toContain('attention:approval_holds');
    expect(column.prose).toBe('');
    expect(column.cta).toBeNull();
    // `attention` is the whole projection, so it clouds this node too.
    expect(railAttention(input({ unavailable: ['attention'] }))!.notice).toContain('attention');
  });

  // #2207: an unread archive ledger makes the server drop every journaled wait
  // in the epic, so the column it leaves behind on THIS node is unread, not
  // empty — the one claim a hidden column would make.
  it('stays when the epic`s archive read failed, dropping every journaled wait', () => {
    const unavailable = ['attention:epic_archive'];
    const column = railAttention(input({ unavailable }))!;
    expect(column.notice).toContain('attention:epic_archive');
    expect(column.prose).toBe('');
    expect(railModel(input({ unavailable })).attention).not.toBeNull();
  });

  // `unavailable` is ONE list for the whole snapshot, and the server's
  // `unreadable_item` entry names ONE item (service/attention.py, pinned by
  // test_one_unreadable_item_is_named_and_the_rest_still_join). Reading it
  // epic-wide put a spurious column on every cleanly-read node in the epic,
  // whose notice named a different node.
  it('stays silent when it was a SIBLING whose attention could not be read', () => {
    const unavailable = ['attention:2047', 'economics'];
    expect(railAttention(input({ unavailable }))).toBeNull();
    expect(railModel(input({ unavailable })).attention).toBeNull();
  });

  it('confesses when the unread item is this node itself, naming it', () => {
    const column = railAttention(
      input({ node: node({ id: '2047' }), unavailable: ['attention:2047', 'attention:2050'] })
    )!;
    expect(column.title).toBe('Needs you');
    expect(column.notice).toContain('attention:2047');
    expect(column.notice).not.toContain('attention:2050');
  });

  it('prints the kind as a heading and the server reason verbatim', () => {
    const column = railAttention(
      input({
        node: node({
          attention: {
            kind: 'merge_decision',
            reason: 'PR open for #2055 — awaiting review',
            url: 'https://github.com/o/r/pull/2059'
          }
        })
      })
    )!;
    expect(column.title).toBe('Merge decision');
    expect(column.prose).toBe('PR open for #2055 — awaiting review');
    expect(column.cta).toEqual({
      label: 'Open PR #2059 to merge',
      href: 'https://github.com/o/r/pull/2059',
      external: true
    });
  });

  it('makes no CTA out of a served link that is not http(s) (C10)', () => {
    // A `javascript:` link from the server is a script on a click; the CTA
    // falls back to the served URL, which passes the same gate.
    const column = railAttention(
      input({
        node: node({
          attention: {
            kind: 'merge_decision',
            reason: 'x',
            link: { label: 'Open PR', href: 'javascript:alert(1)' },
            url: 'https://github.com/o/r/pull/2059'
          }
        })
      })
    )!;
    expect(column.cta?.href).toBe('https://github.com/o/r/pull/2059');
    const bare = railAttention(
      input({
        node: node({
          attention: {
            kind: 'merge_decision',
            reason: 'x',
            link: { label: 'Open PR', href: 'javascript:alert(1)' },
            url: null
          }
        })
      })
    )!;
    expect(bare.cta).toBeNull();
  });

  it('is the shared needs-you block: the kind, the sentence and the command (#3051 N5)', () => {
    // #2814's round-cap hold as live `supervised-daemon` served it. The column
    // printed "Held  why ▸" and nothing else; Monitor printed the unhold.
    const attention = {
      kind: 'held',
      reason: 'review_rounds_exhausted: review pool $10.00 exhausted (spent $22.18)',
      url: null,
      action: 'kew epic unhold, which also restarts the review cycle',
      command: 'kew epic unhold supervised-daemon 2814',
      sentence: 'the review round cap was reached: review pool $10.00 exhausted (spent $22.18)'
    };
    const column = railAttention(input({ node: node({ id: '2814', attention }) }))!;
    expect(column.block).toMatchObject({
      label: 'Held',
      summary: attention.sentence,
      command: 'kew epic unhold supervised-daemon 2814',
      // The action is prose about the command, which is already printed.
      note: ''
    });
    const html = plainCommands(
      render(SelectedRail, {
        props: { input: input({ node: node({ id: '2814', attention }) }) }
      }).body
    );
    expect(html).toContain('>kew epic unhold supervised-daemon 2814</code>');
    expect(html).toContain(attention.sentence);
    expect(html).toContain('Do this');
  });

  it('keeps a link it made from a URL quiet: only a link the server served is the move', () => {
    // A wait that is not a merge, with a PR URL and no command: Work prints
    // its URL as a quiet link, so the rail may not make it magenta either.
    const column = railAttention(
      input({
        node: node({
          attention: {
            kind: 'work_exception',
            reason: 'r',
            command: '',
            action: 'decide the scope',
            url: 'https://github.com/o/r/pull/2059'
          }
        })
      })
    )!;
    expect(column.block!.moves).toEqual([]);
    expect(column.block!.external.map((l) => l.label)).toEqual(['Open PR #2059']);
    const html = render(SelectedRail, {
      props: {
        input: input({
          node: node({
            attention: {
              kind: 'work_exception',
              reason: 'r',
              command: '',
              action: 'decide the scope',
              url: 'https://github.com/o/r/pull/2059'
            }
          })
        })
      }
    }).body;
    expect(html).not.toContain('data-needs-you-move');
  });

  it('makes no move of an issue URL read as a pull request', () => {
    // `prRef` reads an issue's number as `#2055`, so the fallback names
    // "Open PR #2055" for an issue; it is not the server's link, so it is not
    // the magenta move.
    const column = railAttention(
      input({
        node: node({
          attention: { kind: 'merge_ready', reason: 'r', command: '', url: 'https://github.com/o/r/issues/2055' }
        })
      })
    )!;
    expect(column.block!.moves).toEqual([]);
    expect(column.block!.external.every((l) => !('move' in l))).toBe(true);
  });

  it('says the move in words where no command makes it', () => {
    const column = railAttention(
      input({
        node: node({
          attention: { kind: 'work_exception', reason: 'r', url: null, action: 'decide the scope' }
        })
      })
    )!;
    expect(column.block).toMatchObject({ command: '', note: 'decide the scope' });
  });

  it('words an unrecognised kind rather than dropping the column', () => {
    const column = railAttention(
      input({ node: node({ attention: { kind: 'new_kind', reason: 'why', url: null } }) })
    )!;
    expect(column.title).toBe('New kind');
  });

  it("sends an approval to Monitor's holds, which is where it is decided", () => {
    // Not `/inbox`: after the cutover that path lands on the Work screen, which
    // has no approve, deny or decide control on it. Monitor's holds column does
    // (`monitor/holds.ts`), and `routes/urls.test.ts` holds this href to it.
    expect(railCta('approval', null)).toEqual({
      label: 'Open Monitor to approve',
      href: '/monitor',
      external: false
    });
  });

  it('offers nothing when there is nowhere to send anyone', () => {
    expect(railCta('work_exception', null)).toBeNull();
  });

  it('does not name a PR number it could not read out of the URL', () => {
    expect(railCta('merge_decision', 'https://example.invalid/pr')!.label).toBe(
      'Open the pull request to merge'
    );
  });

  it.each(['javascript:alert(1)', '/runs/r1', '//evil.example/x'])(
    'makes no link of a served URL safeHref refuses (%j)',
    (url) => {
      expect(railCta('merge_decision', url)).toBeNull();
      expect(railCta('approval', url)).toEqual({
        label: 'Open Monitor to approve',
        href: '/monitor',
        external: false
      });
    }
  );
});

// --- one moment -------------------------------------------------------------
// Rule 4. The rail runs on two clocks: the snapshot the graph is drawing, and
// the four reads the page takes of the server as it is now. Left to drift they
// contradict each other on screen — the shipped-once failure being a node whose
// attention had cleared, so the Needs-you column was hidden, sitting beside a
// pipeline read from before the merge with its merge stage still `needs_you`.

describe('nodeMoment, the fingerprint the reads are rekeyed on', () => {
  it('is stable across a fresh snapshot carrying the same facts', () => {
    // Every SSE frame rebuilds the nodes array, so keying the reads on the
    // node OBJECT would re-read four endpoints on every tick of the driver.
    expect(nodeMoment(node())).toBe(nodeMoment(node()));
  });

  it('moves with the attention that shows or hides the Needs you column', () => {
    const waiting = node({
      attention: { kind: 'merge_decision', reason: 'awaiting review', url: 'https://x/pull/9' }
    });
    expect(nodeMoment(waiting)).not.toBe(nodeMoment(node()));
    // Same kind, different PR: still a different thing to be waiting on.
    const elsewhere = node({
      attention: { kind: 'merge_decision', reason: 'awaiting review', url: 'https://x/pull/10' }
    });
    expect(nodeMoment(elsewhere)).not.toBe(nodeMoment(waiting));
  });

  it('holds still when only the attention prose moves', () => {
    // The server recomposes that sentence on every 1s poll, and for a
    // `review_capacity_saturated` exception it renders the oldest local hold's
    // age into it (`service/work.py` `_burden`: "oldest 2.4h"). Keying the
    // reads on it made one selected node refire the page's read effect on a
    // wall clock for as long as the tab was open — three columns blinking to
    // em dashes and four more HTTP reads every time, forever.
    const saturated = (age: string): EpicNode =>
      node({
        attention: {
          kind: 'work_exception',
          reason: `#2055 — review capacity saturated: o/r is holding 3 of 3 human review(s) (1 local hold(s), oldest ${age}, 420 diff line(s) waiting)`,
          url: null
        }
      });
    expect(nodeMoment(saturated('2.5h'))).toBe(nodeMoment(saturated('2.4h')));
    // Nor does the same node's prose being replaced wholesale move it: nothing
    // the three read-fed columns draw is keyed on the server's wording.
    expect(nodeMoment(saturated('2.4h'))).toBe(
      nodeMoment(node({ attention: { kind: 'work_exception', reason: 'something else', url: null } }))
    );
    // It still moves when the column APPEARS, which is the fact that matters.
    expect(nodeMoment(saturated('2.4h'))).not.toBe(nodeMoment(node()));
  });

  it('moves with every other node fact the rail prints', () => {
    const base = nodeMoment(node());
    expect(nodeMoment(node({ status: 'done' }))).not.toBe(base);
    expect(nodeMoment(node({ stuck: 'tests failed twice' }))).not.toBe(base);
    expect(nodeMoment(node({ pr_url: null }))).not.toBe(base);
    expect(nodeMoment(node({ id: '2060' }))).not.toBe(base);
  });

  it("never lets one field's change cancel another's", () => {
    // Joined on NUL, not a printable separator: `stuck` is free prose, so
    // "a b" + "c" and "a" + "b c" would otherwise be the same fingerprint —
    // holding the reads on a moment the node has already left.
    expect(nodeMoment(node({ stuck: 'a b', pr_url: 'c' }))).not.toBe(
      nodeMoment(node({ stuck: 'a', pr_url: 'b c' }))
    );
  });

  it('has nothing to fingerprint with nothing selected', () => {
    expect(nodeMoment(null)).toBe('');
    expect(nodeMoment(undefined)).toBe('');
  });
});

describe('the rail under replay', () => {
  it('dashes every column fed by a read of now, and says why', () => {
    const model = railModel(input({ replay: true }));
    expect(model.pipeline!.placeholder).toBe(RAIL_DASH);
    expect(model.pipeline!.reason).toBe(RAIL_REPLAY_UNREAD);
    expect(model.evidence!.count).toBe(RAIL_DASH);
    expect(model.evidence!.reason).toBe(RAIL_REPLAY_UNREAD);
    expect(model.selected!.spend).toEqual({ value: RAIL_DASH, reason: RAIL_REPLAY_UNREAD });
    expect(model.selected!.containment).toEqual({ value: RAIL_DASH, reason: RAIL_REPLAY_UNREAD });
    // ...and the rail says it once, as a sentence (design review c9 D13).
    expect(model.replay).toBe(RAIL_REPLAY_NOTE);
    expect(railModel(input()).replay).toBe('');
  });

  it('draws no grid and no rows a frame from the past cannot vouch for', () => {
    // The fixture pipeline's merge stage is `needs_you` and its evidence has
    // an attested row; both are reads of NOW. Beside a scrubbed frame whose
    // node has no attention — so the Needs-you column is hidden — drawing
    // them is the rail asserting two moments at once.
    const model = railModel(input({ replay: true }));
    expect(model.attention).toBeNull();
    expect(model.pipeline!.stages).toEqual([]);
    expect(model.pipeline!.lanes).toEqual([]);
    expect(model.evidence!.rows).toEqual([]);
    expect(model.evidence!.attested).toBe(0);
  });

  it('still draws everything the frame itself carried', () => {
    // Identity, status, relations and attention come off the snapshot on
    // screen, which IS the frame — replay must not blank those.
    const model = railModel(
      input({
        replay: true,
        node: node({
          attention: { kind: 'merge_decision', reason: 'awaiting review', url: 'https://x/pull/9' }
        })
      })
    );
    expect(model.selected!.ref).toBe('#2055');
    expect(model.selected!.state).toBe('in review');
    expect(model.selected!.relations).toBe('depends on #2047 · PR #2059');
    expect(model.attention!.title).toBe('Merge decision');
    expect(model.attention!.prose).toBe('awaiting review');
  });

  it('leaves the live rail exactly as it was', () => {
    // `replay` is opt-in: an input without it reads the four sources.
    expect(railModel(input()).pipeline!.placeholder).toBe('');
    expect(railModel(input({ replay: false })).evidence!.rows.length).toBe(2);
  });
});

// --- the component ---------------------------------------------------------
// There is no DOM renderer in this suite, so the rail's geometry is checked
// the way `Swimlane`'s is: rendered server-side, with the artboard's own
// numbers read back out of the markup (DEpic y 742–960, x 40–1400).

describe('SelectedRail.svelte', () => {
  function draw(over: Partial<RailInput> = {}): string {
    return plainCommands(render(SelectedRail, { props: { input: input(over) } }).body)
      .replace(/<!--[\s\S]*?-->/g, '')
      .replace(/>\s+</g, '><');
  }

  it('lays the artboard\'s four columns on the raised ink, hairline-separated', () => {
    const html = draw({
      node: node({
        attention: { kind: 'merge_decision', reason: 'awaiting review', url: 'https://x/pull/9' }
      })
    });
    expect(html).toContain('--rail-cols: 300px minmax(0, 1fr) 280px 260px;');
    expect(html).toContain('bg-ink-raised');
    // Three interior rules, one per column boundary.
    expect(html.match(/border-l border-hair/g)?.length).toBe(3);
  });

  it('closes the grid over the Needs you column when nobody is waiting', () => {
    const html = draw();
    expect(html).toContain('--rail-cols: 300px minmax(0, 1fr) 280px;');
    expect(html).not.toContain('Needs you');
  });

  it('draws the quiet empty state, and no columns, with nothing selected', () => {
    const html = draw({ node: null });
    expect(html).toContain(RAIL_EMPTY);
    expect(html).not.toContain('Evidence');
    expect(html).not.toContain('--rail-cols');
  });

  it('collapses to one line when it is empty, giving the height back (D20)', () => {
    // 196px of ruled emptiness round one sentence was a third of the canvas's
    // own height (design review C2, D20). The band keeps its height for a
    // selection and gives it up for a hint.
    const empty = draw({ node: null });
    expect(empty).not.toContain('min-h-[196px]');
    expect(empty).toContain('py-3');
    const full = draw();
    expect(full).toContain('min-h-[196px]');
    expect(full).toContain('py-5');
  });

  it('stacks the columns below `lg`, folding each left rule into a top one (#2333)', () => {
    // 300 + 280 + 260 of fixed track does not fit a 390px phone OR a 1023px
    // laptop, and what did not fit drew on top of itself at both. The widths
    // travel as a property so the stylesheet's queries can replace them, and
    // each column carries what it does when it is a row instead. The widths
    // the folds happen at are `rail-fold.test.ts`'s subject.
    const html = draw({
      node: node({
        attention: { kind: 'merge_decision', reason: 'awaiting review', url: 'https://x/pull/9' }
      })
    });
    expect(html).toContain('rail-grid grid gap-0 max-lg:gap-4');
    expect(html.match(/max-lg:border-l-0 max-lg:border-t max-lg:px-0 max-lg:pt-4/g)?.length).toBe(3);
    // The page's own gutter halves on a phone: 40px either side of a 390px
    // window is a fifth of it.
    expect(html).toContain('px-5 sm:px-10');
  });

  it('keeps the whole rail open on a window of unknown height (server, first frame)', () => {
    // `innerHeight` is 0 until the browser answers. Collapsing on that would
    // shut the rail on every load, so the short-window reading is opt-in.
    const html = draw();
    expect(html).toContain('id="selected-rail-detail"');
    expect(html).not.toContain('aria-expanded');
  });

  it('prints the identity, the readings and the Work link', () => {
    const html = draw();
    expect(html).toContain('#2055');
    expect(html).toContain('review gate lows');
    expect(html).toContain('contained');
    expect(html).toContain('$3.10');
    expect(html).toContain('depends on #2047 · PR #2059');
    expect(html).toContain('href="/work?item=2055"');
    expect(html).toContain('Open #2055 in Work');
  });

  it('heads the evidence column with the count, greening the numerator alone', () => {
    const html = draw();
    expect(html).toContain('<span class="text-success">1</span> / 2 attested');
    // The artboard's 26 / 1fr / 70 / 14 checklist row at 24px.
    expect(html).toContain('grid h-6 grid-cols-[26px_minmax(0,1fr)_70px_14px]');
  });

  it('shows the dash and the reason for a column whose read failed', () => {
    const html = draw({ pipeline: unread('pipeline unavailable (HTTP 500)') });
    expect(html).toContain('pipeline unavailable (HTTP 500)');
    // The neighbours are unharmed.
    expect(html).toContain('$3.10');
    expect(html).toContain('attested');
  });

  it('keeps the retry hint on a failed node', () => {
    const html = draw({
      node: node({
        status: 'failed',
        stuck: 'tests failed twice',
        recovery_command: 'kew epic retry factory-ui 2055'
      })
    });
    expect(html).toContain('kew epic retry factory-ui 2055');
    expect(html).toContain('tests failed twice');
  });

  it('draws a waiting failed node’s continue once, in the needs-you block (#3051)', () => {
    // The Selected column's recovery hint and the Needs-you block are fed the
    // same server command for one wait; the rail prints it in one place.
    const continueCmd = 'kew epic continue factory-ui 2055';
    const html = draw({
      node: node({
        status: 'failed',
        stuck: 'failed-terminal (exit 137, host_restart_repeated, preserved work)',
        recovery_command: continueCmd,
        attention: {
          kind: 'host_restart_repeated',
          reason: 'the host restarted under this node twice; its work is preserved',
          url: null,
          action: 'check why the host keeps going down, then kew epic continue the node',
          command: continueCmd
        }
      })
    });
    expect(html.match(/\bdata-command\b/g)?.length).toBe(1);
    expect(html).toContain(`>${continueCmd}</code>`);
  });

  it('draws no retry command on a failed node the server served none for', () => {
    const html = draw({ node: node({ status: 'failed', stuck: 'tests failed twice' }) });
    expect(html).not.toContain('kew epic retry');
    expect(html).not.toContain('data-command');
  });

  it('draws no magenta merge stage beside a hidden Needs you column in replay', () => {
    const html = draw({ replay: true });
    // The whole contradiction, in the markup: the fixture's merge stage is
    // `needs_you` and this frame's node needs nobody, so the swimlane is not
    // drawn at all and each column says which moment it is missing.
    expect(html).not.toContain('Pipeline stages');
    expect(html).not.toContain('Needs you');
    expect(html).not.toContain('$3.10');
    // The reason is said once, as one sentence across the two columns it
    // stands for (design review c9 D13), never as a line under each heading.
    expect(html.match(new RegExp(RAIL_REPLAY_NOTE.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'g'))).toHaveLength(1);
    expect(html).not.toContain(`>${RAIL_REPLAY_UNREAD}<`);
    expect(html).not.toContain('>Evidence<');
    // The frame's own facts stay: identity is not a read of now.
    expect(html).toContain('#2055');
    expect(html).toContain('depends on #2047 · PR #2059');
  });
});

// --- against the artboard ---------------------------------------------------
// The FUI-02 fixtures are the payloads DEpic was drawn from, so composing the
// rail out of them is the readable half of the visual check: every reading the
// artboard prints at y 742-960 has to come back out of the model unedited.

describe('DEpic, from the fixture payloads', () => {
  const snapshot = (epicJson as unknown as EpicDetail).snapshot!;
  const fixtureEvidence = evidenceJson as unknown as ItemEvidence;
  const fixture: RailInput = {
    epicRef: 'launch-fixes',
    node: snapshot.nodes.find((n) => n.id === '2055')!,
    edges: snapshot.edges,
    unavailable: snapshot.unavailable,
    detail: nodeDetailJson as unknown as EpicNodeDetail,
    view: workViewJson as unknown as WorkView,
    pipeline: pipelineJson as unknown as ItemPipeline,
    evidence: fixtureEvidence
  };

  it('reads "#2055 review gate lows · in review · contained · $3.10"', () => {
    const model = railModel(fixture);
    expect(model.selected!.ref).toBe('#2055');
    expect(model.selected!.title).toBe('review gate lows');
    expect(model.selected!.state).toBe('in review');
    expect(model.selected!.containment.value).toBe('contained');
    expect(model.selected!.spend.value).toBe('$3.10');
    expect(model.selected!.relations).toBe('depends on #2047 · PR #2059');
  });

  it('heads the pipeline with the one lane kew serves', () => {
    // Reproduce and the release lane are absent by design.
    expect(railPipeline(fixture)!.heading).toBe('Pipeline · main');
    expect(railPipeline(fixture)!.stages).toEqual(['implement', 'publish', 'review', 'merge']);
  });

  it('reads "4 / 4 attested" with the READY verdict the pipeline also reads', () => {
    // #3461: one story — round 1 READY at 3670e43f, recorded before the
    // failed merge verify at that head.
    const column = railEvidence(fixture)!;
    expect(column.count).toBe('4 / 4 attested');
    expect(column.rows.map((r) => r.n)).toEqual(['01', '02', '03', '04']);
    expect(column.rows[3].right).toBe('14:46:40');
  });

  it('reads the verdict row as pending while the round is still out', () => {
    const rows = fixtureEvidence.rows.map((r) =>
      r.n === 4 ? { ...r, ts: '', state: 'pending' } : r
    );
    const column = railEvidence({
      ...fixture,
      evidence: { ...fixtureEvidence, rows, attested_count: 3 }
    })!;
    expect(column.count).toBe('3 / 4 attested');
    expect(column.rows[3].right).toBe('pending');
  });

  it("offers the artboard's magenta call to action", () => {
    const column = railAttention(fixture)!;
    expect(column.title).toBe('Merge decision');
    expect(column.cta).toEqual({
      label: 'Open PR #2059 to merge',
      href: 'https://github.com/thossullivan/kew/pull/2059',
      external: true
    });
  });

  it('makes the failed merge verify the move, ahead of the sentence (design review c9 D10, D14)', () => {
    // #2055's merge verify failed at its head: reading the failure is the
    // safest move, and the PR follows it, quiet. Both are Do this, which the
    // rail's compact block draws before the sentence, so the window's foot
    // cannot cut them off.
    const block = railAttention(fixture)!.block!;
    expect(block.command).toBe('');
    expect(block.moves?.map((l) => [l.label, l.move])).toEqual([
      ['See the failed merge verify', true],
      ['Open PR #2059 to merge', false]
    ]);
    expect(block.links).toEqual([]);
    expect(block.external).toEqual([]);
    const html = render(SelectedRail, { props: { input: fixture } }).body;
    const moves = html.indexOf('data-needs-you-moves');
    const summary = html.indexOf('data-needs-you-summary');
    expect(moves).toBeGreaterThan(-1);
    expect(summary).toBeGreaterThan(moves);
    expect(html.match(/<a [^>]*data-needs-you-move[^>]*>/)?.[0]).toContain('#merge-verify');
  });
});
