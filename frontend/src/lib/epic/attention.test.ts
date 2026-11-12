import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import {
  attentionUnavailable,
  attentionNotice,
  nodeAttentionUnavailable,
  nodeAttentionNotice,
  UNREADABLE_ARCHIVE,
  UNREADABLE_EPIC_HOLDS,
  epicNeedsYou,
  EPIC_BUCKET_SOURCES,
  EPIC_PLACEMENT_SOURCES,
  keepAttentionInGraph,
  markStatus,
  needsYou,
  needsYouReading,
  needsYouSetNotice,
  offGraphNeedsYou,
  offGraphNotice,
  noEpicNeedsYou,
  stalledWaitsLink,
  SEP,
  statusLineText,
  statusSegments,
  type NodeFacts
} from './attention';
import { labelTone } from './render';
import { JUDGMENT_ROW_SOURCES } from '$lib/shell/header-state';
import { splitStandalone } from './metro';

import { statusShape } from '$lib/factory/status';
import { ARM_LABEL, ARM_WAIT_REASON } from './waiting';
import type { Dashboard, EpicDetail } from '$lib/api';
import dashboardJson from '../../../fixtures/factory/dashboard.json';
import epicJson from '../../../fixtures/factory/epics-launch-fixes.json';
import goLiveJson from '../../../fixtures/factory/epics-go-live-2026-09.json';

const MAGENTA = 'var(--accent)';
const CYAN = 'var(--accent-2)';
const LAVENDER = 'var(--status-blocked)';

function node(facts: Partial<NodeFacts> & { status: string }): NodeFacts {
  return { id: '2047', attention: null, blocked_reason: null, stuck: null, ...facts };
}

/** DEpic's #2055: an in-review node whose PR is waiting on a person. */
const NEEDS_YOU = node({
  status: 'in_review',
  attention: { kind: 'merge_decision', reason: 'merge PR #2059', url: 'https://x/pull/2059' }
});

/** DEpic's #2053: blocked, with the reason the driver recorded. */
const BLOCKED = node({ status: 'blocked', blocked_reason: 'needs a design decision' });

describe('statusSegments — the status line, word by coloured word', () => {
  it('draws DEpic #2055 exactly: cyan status, quiet separator, magenta needs-you', () => {
    expect(statusSegments(NEEDS_YOU)).toEqual([
      { text: 'in review', color: CYAN },
      { text: SEP, color: 'var(--text-5)' },
      { text: 'needs you · merge PR #2059', color: MAGENTA }
    ]);
  });

  it('prints the server`s own reason verbatim — no template of our own', () => {
    const reason = 'PR open for #2055 — awaiting review';
    const segs = statusSegments(node({ status: 'in_review', attention: { kind: 'x', reason, url: null } }));
    expect(segs.at(-1)).toEqual({ text: `needs you${SEP}${reason}`, color: MAGENTA });
    expect(statusLineText(segs)).toBe(`in review${SEP}needs you${SEP}${reason}`);
  });

  it('a reasonless attention is still magenta needs-you, with no dangling separator', () => {
    for (const reason of ['', '   ']) {
      const segs = statusSegments(node({ status: 'in_review', attention: { kind: 'approval', reason, url: null } }));
      expect(segs.at(-1)).toEqual({ text: 'needs you', color: MAGENTA });
      expect(statusLineText(segs)).toBe(`in review${SEP}needs you`);
    }
  });

  it('draws DEpic #2053 as one lavender line carrying blocked_reason', () => {
    expect(statusSegments(BLOCKED)).toEqual([
      { text: 'blocked · needs a design decision', color: LAVENDER }
    ]);
  });

  it('a null blocked_reason prints only the status — never an invented reason', () => {
    for (const reason of [null, undefined, '', '  ']) {
      expect(statusSegments(node({ status: 'blocked', blocked_reason: reason }))).toEqual([
        { text: 'blocked', color: LAVENDER }
      ]);
    }
  });

  it('an ordinary node keeps its one labelTone-coloured word', () => {
    for (const status of ['done', 'todo', 'in_progress', 'failed', 'held']) {
      const segs = statusSegments(node({ status }));
      expect(segs).toHaveLength(1);
      expect(segs[0]).toEqual({
        text: statusShape(status).word,
        color: labelTone(status).status
      });
    }
  });

  it('a recorded reason rides the status`s own colour, whatever the status', () => {
    // The DTO documents blocked_reason as a blocked node's fact, but a fact the
    // driver recorded is printed wherever it arrives rather than dropped.
    const segs = statusSegments(node({ status: 'held', blocked_reason: 'budget cap reached' }));
    expect(segs).toEqual([
      { text: `held${SEP}budget cap reached`, color: labelTone('held').status }
    ]);
  });

  it('a blocked node that also needs a person says both, in both colours', () => {
    const segs = statusSegments(
      node({
        status: 'blocked',
        blocked_reason: 'needs a design decision',
        attention: { kind: 'approval', reason: 'awaiting approval — spend', url: null }
      })
    );
    expect(segs.map((s) => s.color)).toEqual([LAVENDER, 'var(--text-5)', MAGENTA]);
  });

  it('an unarmed node carries the action that arms it, in its own quiet tone', () => {
    // #2209 took `queued` off the magenta needs-you diamond, which is only
    // honest if the node still says what the person has to do. It said it
    // nowhere: the only surface printing `waitAction` was the epic page's
    // STALLED notice, which needs `heartbeat.last_signal === 'stalled'`, so on
    // a progressing epic an unarmed node drew a quiet dashed ring and the bare
    // word "queued" — indistinguishable from a step that arrives by itself.
    const segs = statusSegments(node({ id: 'SFD-223', status: 'queued', stuck: ARM_WAIT_REASON }));
    expect(segs).toEqual([
      {
        text: `queued${SEP}arm it: add the ${ARM_LABEL} label to SFD-223 in Linear`,
        color: labelTone('queued').status
      }
    ]);
    // Quiet, not magenta: the rationing #2209 was about in the first place.
    expect(segs[0].color).not.toBe(MAGENTA);
  });

  it('repeats no other waiting reason — the driver already words those', () => {
    // Every other `_FIXED_WAITING_REASONS` line leads with its own status word
    // and names its own command, so folding it in here would print the status
    // twice and paraphrase the driver (waiting.ts).
    const others = [
      ['held', 'held — budget_preflight exceeds the per-issue cap'],
      ['remediating', 'remediating — acting on review findings'],
      ['blocked', 'blocked (blocked label)'],
      ['awaiting_continuation', 'awaiting_continuation — preserved dirty work; use `kew epic continue`']
    ];
    for (const [status, stuck] of others) {
      const segs = statusSegments(node({ status, stuck }));
      expect(statusLineText(segs), status).toBe(statusShape(status).word);
    }
  });
});

describe('markStatus — attention takes the mark', () => {
  it('an attentive node is drawn as the 18px magenta diamond, whatever its status', () => {
    expect(markStatus(NEEDS_YOU)).toBe('needs_you');
    const mark = statusShape(markStatus(NEEDS_YOU));
    expect(mark.shape).toBe('diamond-halo');
    expect(mark.color).toBe(MAGENTA);
  });

  it('every other node keeps its own mark — blocked stays a lavender hatch', () => {
    expect(markStatus(BLOCKED)).toBe('blocked');
    expect(statusShape(markStatus(BLOCKED)).shape).toBe('hatch-square');
    expect(markStatus(node({ status: 'done' }))).toBe('done');
  });

  it('needsYou is the one test for the treatment', () => {
    expect(needsYou(NEEDS_YOU)).toBe(true);
    expect(needsYou(BLOCKED)).toBe(false);
  });
});

describe('needsYouReading — the header strip`s count', () => {
  const nodes = [NEEDS_YOU, BLOCKED, node({ status: 'done' })];

  it('counts the nodes with attention', () => {
    expect(needsYouReading(nodes, [])).toEqual({ count: 1, known: true, label: '1' });
    expect(needsYouReading([BLOCKED], [])).toEqual({ count: 0, known: true, label: '0' });
  });

  it('is unknown, not zero, when the join could not be composed', () => {
    const reading = needsYouReading([BLOCKED], ['attention']);
    expect(reading.known).toBe(false);
    expect(reading.label).toBe('?');
  });
});

describe('attentionNotice — silence on failure is not silence', () => {
  it('says nothing when every source was read', () => {
    expect(attentionNotice([])).toBeNull();
    expect(attentionNotice(['economics'])).toBeNull();
  });

  it('names the attention sources that failed, and only those', () => {
    const unavailable = ['economics', 'attention', 'attention:approval_holds'];
    expect(attentionUnavailable(unavailable)).toEqual(['attention', 'attention:approval_holds']);
    const notice = attentionNotice(unavailable);
    expect(notice).toContain('attention:approval_holds');
    expect(notice).not.toContain('economics');
    expect(notice?.toLowerCase()).toContain('needs you');
  });
});

describe('nodeAttentionNotice — one node`s share of the failure', () => {
  // Every entry but `attention:<item>` is epic-wide; that one is about its item
  // alone (service/attention.py:unreadable_item).
  it('keeps the epic-wide entries, which cloud every node', () => {
    expect(nodeAttentionUnavailable(['attention'], '2055')).toEqual(['attention']);
    expect(nodeAttentionUnavailable(['attention:approval_holds'], '2055')).toEqual([
      'attention:approval_holds'
    ]);
    expect(nodeAttentionNotice(['attention'], '2055')).toContain('attention');
  });

  // #2207 regression: the epic-hold read is the third epic-wide marker, and the
  // per-node whitelist admitted only the first two — so a failed hold read left
  // every node silently clean and `railAttention` hid the "Needs you" column
  // outright, which is the "nobody is waiting" claim an unread join cannot make.
  it('keeps the epic-hold read`s failure, which clouds every node too', () => {
    expect(nodeAttentionUnavailable([UNREADABLE_EPIC_HOLDS], '2055')).toEqual([
      UNREADABLE_EPIC_HOLDS
    ]);
    expect(nodeAttentionNotice([UNREADABLE_EPIC_HOLDS], '2055')).toContain('epic_holds');
    // And the epic-wide reading has it too, so the header reads "?" not "0".
    expect(needsYouReading([], [UNREADABLE_EPIC_HOLDS]).label).toBe('?');
  });

  // #2207 regression, the same bug a fourth marker wide: the archive read
  // decides whether this epic is retired, and when it fails the server drops
  // EVERY journaled wait in the epic. Left out of `EPIC_WIDE`, that dropped
  // wait read as a clean "needs nobody" on every node and the rail hid the
  // "Needs you" column outright.
  it('keeps the archive read`s failure, which drops every node`s journaled wait', () => {
    expect(nodeAttentionUnavailable([UNREADABLE_ARCHIVE], '2055')).toEqual([
      UNREADABLE_ARCHIVE
    ]);
    expect(nodeAttentionNotice([UNREADABLE_ARCHIVE], '2055')).toContain('epic_archive');
    // And the epic-wide reading has it too, so the header reads "?" not "0".
    expect(needsYouReading([], [UNREADABLE_ARCHIVE]).label).toBe('?');
  });

  // The server's own vocabulary, read off the module that defines it: a marker
  // added there and left out of `EPIC_WIDE` is the bug above, again.
  it('admits every epic-wide marker the server can send', () => {
    const py = readFileSync(
      new URL('../../../../kew/src/kew/service/attention.py', import.meta.url),
      'utf8'
    );
    const markers = [...py.matchAll(/^UNREADABLE_\w+ = "([^"]+)"$/gm)].map((m) => m[1]);
    expect(markers.length).toBeGreaterThanOrEqual(4);
    for (const marker of markers) {
      expect(nodeAttentionUnavailable([marker], '2055')).toEqual([marker]);
    }
  });

  it('drops another item`s entry, and keeps this item`s', () => {
    const unavailable = ['economics', 'attention:2047', 'attention:2055'];
    expect(nodeAttentionUnavailable(unavailable, '2055')).toEqual(['attention:2055']);
    expect(nodeAttentionNotice(unavailable, '2055')).toContain('attention:2055');
    expect(nodeAttentionNotice(unavailable, '2055')).not.toContain('attention:2047');
    expect(nodeAttentionNotice(['attention:2047'], '2055')).toBeNull();
  });

  it('says nothing when nothing failed', () => {
    expect(nodeAttentionNotice([], '2055')).toBeNull();
    expect(nodeAttentionNotice(['economics'], '2055')).toBeNull();
  });

  // The header's count is over every node, so it keeps the wider reading:
  // one unread item still makes the epic total a "?" rather than a number.
  it('leaves the header`s epic-wide reading alone', () => {
    expect(needsYouReading([], ['attention:2047']).label).toBe('?');
    expect(attentionNotice(['attention:2047'])).toContain('attention:2047');
  });
});

describe('the standalone-done strip (DEpic y 669–742)', () => {
  const nodes = [
    { id: 'a', status: 'done' },
    { id: 'b', status: 'done' },
    { id: 'c', status: 'done' },
    { id: 'd', status: 'todo' },
    { id: 'e', status: 'blocked' }
  ];

  it('holds only the done nodes with no edge at either end', () => {
    const { graph, strip } = splitStandalone(nodes, [['a', 'd']]);
    expect(strip.map((n) => n.id)).toEqual(['b', 'c']);
    expect(graph.map((n) => n.id)).toEqual(['a', 'd', 'e']);
  });

  it('keeps an unfinished node in the graph even with nothing depending on it', () => {
    const { strip } = splitStandalone(nodes, []);
    expect(strip.map((n) => n.id)).toEqual(['a', 'b', 'c']);
  });
});

describe('keepAttentionInGraph — the count and the treatment cover the same nodes', () => {
  const waited = {
    id: 'b',
    status: 'done',
    attention: { kind: 'approval', reason: 'approve the merge', url: null }
  };
  const nodes = [
    { id: 'a', status: 'done', attention: null },
    waited,
    { id: 'c', status: 'todo', attention: null }
  ];

  it('takes a waited-on done node back out of the strip', () => {
    // Regression: the strip draws a plain done row — no plate, no diamond, no
    // reason — so a node it holds could be counted in the header and shown
    // nowhere.
    const split = splitStandalone(nodes, []);
    expect(split.strip.map((n) => n.id)).toEqual(['a', 'b']);
    const kept = keepAttentionInGraph(split);
    expect(kept.strip.map((n) => n.id)).toEqual(['a']);
    expect(kept.graph.map((n) => n.id)).toEqual(['c', 'b']);
  });

  it('every node the header counts is a node the graph draws', () => {
    const { graph, strip } = keepAttentionInGraph(splitStandalone(nodes, []));
    expect(graph.filter(needsYou)).toHaveLength(needsYouReading(nodes, []).count);
    expect(strip.some(needsYou)).toBe(false);
  });

  it('leaves a strip nobody is waiting on exactly as it was', () => {
    const quiet = splitStandalone([nodes[0], nodes[2]], []);
    expect(keepAttentionInGraph(quiet)).toBe(quiet);
  });
});


describe('the fixture epic, read the way the view reads it', () => {
  // The same payload `npm run dev:fixtures` serves the visual check, so what
  // this asserts is what the screenshot of DEpic shows.
  const snapshot = (epicJson as unknown as EpicDetail).snapshot!;
  const nodeOf = (id: string) => snapshot.nodes.find((n) => n.id === id)!;

  it('draws #2055 as the one needs-you row, with the server`s own sentence', () => {
    const n = nodeOf('2055');
    expect(needsYou(n)).toBe(true);
    expect(snapshot.nodes.filter(needsYou)).toHaveLength(1);
    expect(markStatus(n)).toBe('needs_you');
    expect(statusSegments(n)).toEqual([
      { text: 'in review', color: CYAN },
      { text: SEP, color: 'var(--text-5)' },
      // Its merge verify failed at its head, so the sentence leads with that (#3460).
      {
        text: `needs you${SEP}Merge verify failed at 3670e43f: 2 of main's tests fail on this PR — the merge decision is yours`,
        color: MAGENTA
      }
    ]);
  });

  it('draws #2053 as one lavender blocked line, and nothing magenta', () => {
    const n = nodeOf('2053');
    expect(needsYou(n)).toBe(false);
    expect(statusSegments(n)).toEqual([
      { text: `blocked${SEP}${n.blocked_reason}`, color: LAVENDER }
    ]);
  });

  it('counts one needs-you on a composed join, and "?" on a journaled tick', () => {
    expect(needsYouReading(snapshot.nodes, snapshot.unavailable).label).toBe('1');
    // `/ticks` history composes no join (fixtures.test.ts pins the payload).
    expect(needsYouReading(snapshot.nodes, ['attention']).label).toBe('?');
    expect(attentionNotice(snapshot.unavailable)).toBeNull();
  });
});

describe('the go-live epic, whose heartbeat is never stalled', () => {
  // The fixture the #2209 review is reconstructed from. Its heartbeat says
  // `progressing`, so the epic page's stalled notice — the only other place the
  // arm action is printed — renders nothing at all on it. The node has to carry
  // the cue itself, or the whole epic shows a dashed ring and no reason.
  const go = goLiveJson as unknown as EpicDetail;

  it('says what to do about its unarmed node anyway', () => {
    expect(go.heartbeat?.last_signal).toBe('progressing');
    const unarmed = go.snapshot!.nodes.filter((n) => n.status === 'queued');
    expect(unarmed.map((n) => n.id)).toEqual(['SFD-223']);
    for (const n of unarmed) {
      // Not the magenta diamond (#2209) — and so it owes the reader the action.
      expect(markStatus(n), n.id).not.toBe('needs_you');
      expect(statusLineText(statusSegments(n)), n.id).toBe(
        `queued${SEP}arm it: add the ${ARM_LABEL} label to ${n.id} in Linear`
      );
    }
  });
});

describe('epicNeedsYou — the served set, filtered by epic (#2279/D6)', () => {
  const served = {
    needs_you_by_epic: { 'launch-fixes': 1, 'go-live': 2 },
    unavailable: [] as string[]
  };

  it('reads each epic its own bucket, and 0 for an epic with none', () => {
    expect(epicNeedsYou(served, 'launch-fixes')).toEqual({
      count: 1,
      known: true,
      label: '1'
    });
    expect(epicNeedsYou(served, 'go-live')).toEqual({ count: 2, known: true, label: '2' });
    // Absent from the map IS zero: the server omits an empty bucket rather
    // than serving one row per epic it walked.
    expect(epicNeedsYou(served, 'drain-2026-07')).toEqual({
      count: 0,
      known: true,
      label: '0'
    });
  });

  it('sums to the header count the same payload serves', () => {
    // The acceptance, client-side: the header counts `rows`, and every epic's
    // legend and list row read a bucket of this map, so the three numbers are
    // arithmetic on one set. kew/tests/test_needs_you_one_set.py holds the
    // server to composing it that way.
    const counts = Object.values(served.needs_you_by_epic);
    expect(counts.reduce((a, b) => a + b, 0)).toBe(
      epicNeedsYou(served, 'launch-fixes').count + epicNeedsYou(served, 'go-live').count
    );
  });

  it('says "?" rather than 0 when the set confessed a failed source', () => {
    const partial = { ...served, unavailable: ['work_rows'] };
    expect(epicNeedsYou(partial, 'launch-fixes')).toEqual({
      count: 1,
      known: false,
      label: '?'
    });
    expect(needsYouSetNotice(partial)).toBe('needs you unknown — could not read work_rows');
  });

  it('says "?" for a dashboard nobody has loaded yet, and names itself', () => {
    expect(epicNeedsYou(null, 'launch-fixes')).toEqual({
      count: 0,
      known: false,
      label: '?'
    });
    expect(needsYouSetNotice(null)).toBe(
      'needs you unknown — could not read the needs-you set'
    );
    expect(needsYouSetNotice(served)).toBeNull();
  });

  it('tolerates a kew serve that predates the field', () => {
    // Optional on the wire: an older server serves no map, and a legend that
    // printed 0 over one would be claiming nobody is waiting.
    expect(epicNeedsYou({ unavailable: [] }, 'launch-fixes')).toEqual({
      count: 0,
      known: true,
      label: '0'
    });
  });

  it('reads the fixture payload the gallery and the Playwright harness see', () => {
    // Real data, so `npm run dev:fixtures` shows the legend and the Epics list
    // row the same number the header counts.
    const dashboard = dashboardJson as unknown as Dashboard;
    // Each epic's bucket is that epic's own slice of the rows the header
    // counts — the arithmetic #2279 exists to keep, now that the fixture set
    // spans two epics rather than one.
    for (const ref of ['launch-fixes', 'go-live-2026-09']) {
      expect(epicNeedsYou(dashboard, ref).count, ref).toBe(
        dashboard.rows.filter((r) => r.epic_ref === ref).length
      );
    }
    expect(
      Object.values(dashboard.needs_you_by_epic ?? {}).reduce((a, b) => a + b, 0)
    ).toBe(dashboard.rows.filter((r) => r.epic_ref).length);
  });
});

describe('which failures make a per-epic count unknowable', () => {
  const served = {
    needs_you_by_epic: { 'launch-fixes': 1 },
    unavailable: [] as string[]
  };

  it('watches the judgment rows and the walk that places them, and nothing else', () => {
    // The set's own sources, in two languages. The Python side holds this to
    // `needs_you_set` itself (kew/tests/test_factory_header_parity.py); what
    // is asserted here is the composition the server cannot see — the epic's
    // list IS the header's list plus placement, so one cannot be extended
    // without the other.
    // #2784 adds the executing-run read: it removes entries rather than
    // producing any, and a failed read leaves a running item in a bucket.
    expect(EPIC_PLACEMENT_SOURCES).toEqual(['node_epics', 'executing_runs']);
    for (const name of JUDGMENT_ROW_SOURCES) expect(EPIC_BUCKET_SOURCES).toContain(name);
    expect(EPIC_BUCKET_SOURCES).toContain('node_epics');
    expect(EPIC_BUCKET_SOURCES).not.toContain('ready_queue');
  });

  it('keeps the count a count when a GLANCE source failed', () => {
    // The bug: `known` was read off the whole payload's `unavailable`, so the
    // gh-backed ready queue timing out (it goes unavailable on a 20s timeout
    // and backs off for 30s) printed "?" on every epic legend and every
    // Epics-list row while the header above them printed an exact count from
    // the same payload. A reading that was taken correctly, reported as
    // unknown, is the null-versus-zero mistake pointing the other way.
    const glanceDown = {
      ...served,
      unavailable: ['ready_queue', 'queue_candidates', 'fleet', 'caps', 'banner']
    };
    expect(epicNeedsYou(glanceDown, 'launch-fixes')).toEqual({
      count: 1,
      known: true,
      label: '1'
    });
    expect(epicNeedsYou(glanceDown, 'nothing-waiting')).toEqual({
      count: 0,
      known: true,
      label: '0'
    });
    // And nothing to confess: a notice naming the ready queue under a count
    // the ready queue cannot touch reads as an excuse for a number that is fine.
    expect(needsYouSetNotice(glanceDown)).toBeNull();
  });

  it('still says "?" when a source the BUCKET rests on failed', () => {
    for (const name of EPIC_BUCKET_SOURCES) {
      const down = { ...served, unavailable: ['ready_queue', name] };
      expect(epicNeedsYou(down, 'launch-fixes').label, name).toBe('?');
      // The confession names what actually failed, and only it.
      expect(needsYouSetNotice(down), name).toBe(`needs you unknown — could not read ${name}`);
    }
  });

  it('says so for the walk in particular, which the header deliberately ignores', () => {
    // `node_epics` loses no row — the header's count stays exact, which is why
    // JUDGMENT_ROW_SOURCES leaves it out — but every entry whose own reader
    // knew no epic then lands in no bucket, so these numbers ARE short by it.
    const unplaced = { ...served, unavailable: ['node_epics'] };
    expect(JUDGMENT_ROW_SOURCES).not.toContain('node_epics');
    expect(epicNeedsYou(unplaced, 'launch-fixes').known).toBe(false);
  });
});

describe('the part of an epic count the graph cannot draw', () => {
  const stalled = {
    type: 'stalled_epic',
    item_id: null,
    epic_ref: 'launch-fixes',
    reason: 'driver gone — launch-fixes — no tick in 6d 8h'
  };
  const merge = {
    type: 'pr_review',
    item_id: '2055',
    epic_ref: 'launch-fixes',
    reason: 'merge PR #2059'
  };
  const elsewhere = { type: 'pr_review', item_id: '3001', epic_ref: 'go-live', reason: 'x' };
  // #2055 drawn waiting, #2040 drawn settled: one diamond on the graph.
  const drawn = [{ ...NEEDS_YOU, id: '2055' }, node({ id: '2040', status: 'done' })];

  it('leaves out the entries a diamond on this graph already accounts for', () => {
    // #2055 is drawn waiting (NEEDS_YOU carries the same id), so its row has
    // somewhere to point and belongs to nobody's leftover.
    expect(offGraphNeedsYou([merge], 'launch-fixes', drawn)).toEqual([]);
    expect(offGraphNotice([])).toBeNull();
  });

  it('keeps a wait that is about the EPIC, which has no node to mark', () => {
    // A stopped driver is the case: `_stalled_rows` names the epic and no
    // item, so the bucket counts it and the DAG cannot. Unsaid, the legend
    // simply read one higher than the diamonds under it.
    expect(offGraphNeedsYou([stalled, merge], 'launch-fixes', drawn)).toEqual([stalled]);
    expect(offGraphNotice([stalled])).toBe('one of them is not a node on the graph below');
  });

  it('keeps an entry whose node this graph is not drawing waiting', () => {
    const ghost = { ...merge, item_id: '9999' };
    expect(offGraphNeedsYou([ghost], 'launch-fixes', drawn)).toEqual([ghost]);
    expect(offGraphNotice([stalled, ghost])).toBe('2 of them are not a node on the graph below');
  });

  it('reads only this epic’s bucket, which is what the legend counts', () => {
    expect(offGraphNeedsYou([elsewhere], 'launch-fixes', drawn)).toEqual([]);
    expect(offGraphNeedsYou([elsewhere], 'go-live', [])).toEqual([elsewhere]);
    expect(offGraphNeedsYou(undefined, 'launch-fixes', drawn)).toEqual([]);
  });

  it('accounts for every unit of the served count, on the fixture payload', () => {
    // The arithmetic the page rests on: diamonds drawn + entries named = the
    // number in the legend. Anything else is a count with nothing to point at.
    const dashboard = dashboardJson as unknown as Dashboard;
    const snapshot = (epicJson as unknown as EpicDetail).snapshot!;
    const nodes = snapshot.nodes as unknown as NodeFacts[];
    const count = epicNeedsYou(dashboard, 'launch-fixes').count;
    expect(
      nodes.filter(needsYou).length +
        offGraphNeedsYou(dashboard.rows, 'launch-fixes', nodes).length
    ).toBe(count);
  });
});

describe('stalledWaitsLink (#2538, #2784)', () => {
  it("counts the epic's bucket — the legend's reading — as waits on Work", () => {
    const served = {
      needs_you_by_epic: { 'go-live-2026-09': 4, 'launch-fixes': 1 },
      unavailable: []
    };
    expect(stalledWaitsLink(epicNeedsYou(served, 'go-live-2026-09'))).toBe('4 waits on Work →');
    expect(stalledWaitsLink(epicNeedsYou(served, 'launch-fixes'))).toBe('1 wait on Work →');
  });

  it('links nothing when nobody waits, or when the bucket could not be read', () => {
    expect(stalledWaitsLink(epicNeedsYou({ needs_you_by_epic: {} }, 'go-live-2026-09'))).toBeNull();
    expect(
      stalledWaitsLink(
        epicNeedsYou({ needs_you_by_epic: { x: 2 }, unavailable: ['epic_waits'] }, 'x')
      )
    ).toBeNull();
    expect(stalledWaitsLink(epicNeedsYou(null, 'x'))).toBeNull();
  });
});

describe('noEpicNeedsYou (#2784)', () => {
  it('reads the served no-epic bucket, so the column sums to the header', () => {
    const served = {
      rows: [{ epic_ref: 'a' }, { epic_ref: null }, { epic_ref: '' }],
      needs_you_by_epic: { a: 1 },
      needs_you_no_epic: 2,
      unavailable: []
    };
    const reading = noEpicNeedsYou(served);
    expect(reading).toEqual({ count: 2, known: true, label: '2' });
    expect(Object.values(served.needs_you_by_epic).reduce((x, y) => x + y, 0) + reading.count).toBe(
      served.rows.length
    );
  });

  it('counts the rows naming no epic for a server that predates the field', () => {
    expect(noEpicNeedsYou({ rows: [{ epic_ref: 'a' }, {}] }).count).toBe(1);
  });

  it('is unknown when the placement walk failed, and before any load', () => {
    expect(noEpicNeedsYou({ rows: [], unavailable: ['node_epics'] }).label).toBe('?');
    expect(noEpicNeedsYou(null).known).toBe(false);
  });
});
