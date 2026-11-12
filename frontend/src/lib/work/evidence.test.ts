/**
 * FUI-21 — the needs-you band, the description panel and the evidence ledger:
 * what each of them is allowed to claim.
 *
 * Three claims, three ways to get them wrong, one suite per way:
 *
 * 1. **The band spends the rationed colour.** Solid magenta means "a person is
 *    the next move", so the band appears for exactly two reasons and no others:
 *    the projection's judgment says a decision is outstanding, or its one safest
 *    action is one kew cannot take. Every action in the vocabulary is classified
 *    here, and `kew/tests/test_factory_exception_parity.py` holds that table to
 *    `derive_verdict` itself — the frontend cannot be the only copy, because a
 *    new action missing from BOTH would silently stop raising a band.
 * 2. **A green check means kew vouched for the fact.** An unverified chain
 *    downgrades every row, so `unverified` never draws a check however the
 *    payload was composed — and the rail segment follows the NEXT row's state,
 *    which is the artboard's solid record dashing into what has not happened.
 * 3. **A description is a remote read.** The body may be drawn only under a
 *    reading the server stands behind; everything else is the reason, plus the
 *    "Open on GitHub" link wherever the payload named a URL — never an empty
 *    panel that reads as a short issue. Which answers name a URL is a fact
 *    about FUI-31's payload, so the failure cases are built from what that
 *    endpoint really composes (`failed()`), not by re-labelling the available
 *    fixture: a spread that kept its `url` would test a link the panel cannot
 *    draw in the app.
 *
 * As in `header.test.ts` and `pipeline.test.ts` the components are SERVER-
 * rendered, so the pixels are read off the markup the sections really produce
 * rather than off a model that claims to describe them — the standing check on
 * DWork y ≈522–598 and y ≈624–928 until FUI-02's screenshot harness can shoot
 * the artboard. The artboard is read below too, so its own numbers are the ones
 * asserted rather than remembered.
 */
import { readFileSync } from 'node:fs';
import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import type { ItemDescription, ItemEvidence, EvidenceRow, WorkView } from '$lib/api';
import { statusShape } from '$lib/factory/status';
import Description from './Description.svelte';
import EvidenceLedger from './EvidenceLedger.svelte';
import NeedsYouBlock from '$lib/needs-you/NeedsYouBlock.svelte';
import { needsYouBlock } from '$lib/needs-you/block';
import type { WorkRow } from './list';
import {
  actionNeedsPerson,
  AWAITING_DECISION,
  bandCta,
  DESCRIPTION_EMPTY,
  DESCRIPTION_LINK,
  DESCRIPTION_RETRIES,
  DESCRIPTION_UNATTEMPTED,
  DESCRIPTION_UNSYNCED,
  descriptionPanel,
  descriptionUnread,
  evidenceLedger,
  evidenceState,
  LEDGER_ABSENT,
  ledgerIndex,
  ledgerRows,
  NEEDS_YOU_TITLE,
  needsYouBand,
  readFailure,
  rereadsDescription,
  SAFEST_ACTION_ACTOR,
  segmentInto
} from './evidence';
import { PULSE_INTERVAL_MS } from '$lib/shell/pulse.svelte';
import evidenceJson from '../../../fixtures/factory/work-2055-evidence.json';
import descriptionJson from '../../../fixtures/factory/work-2055-description.json';
import workViewJson from '../../../fixtures/factory/verbs-work-2055.json';

const DWORK = readFileSync(
  new URL('../../../design/factory-ui/DWork.html', import.meta.url),
  'utf8'
);

/** The UX artboard the needs-you block is drawn from (`.ny`). */
const UXWORK = readFileSync(
  new URL('../../../design/factory-ui/UXWork.html', import.meta.url),
  'utf8'
);

/**
 * A row with no needs-you entry behind it, so the block is the projection's
 * alone — the case the band answered before #2280 merged the two.
 */
const BARE_ROW: WorkRow = {
  itemId: '2055',
  number: '2055',
  ref: '#2055',
  title: 'Epic driver loses a tick',
  state: 'in review',
  needsYou: false,
  view: null,
  tier: null,
  entry: null,
  attention: null,
  href: '/work?item=2055',
  external: false
};

/** The committed FUI-12 payload, which is the DWork ledger. */
const EVIDENCE: ItemEvidence = evidenceJson;
/** The committed FUI-31 payload, which is the DWork description panel. */
const DESCRIPTION: ItemDescription = descriptionJson;
/** The committed work view for #2055 — awaiting a merge decision. */
const VIEW: WorkView = workViewJson;

const view = (over: Partial<WorkView> = {}): WorkView => ({ ...VIEW, ...over });

const safest = (action: string, reason = 'because the projection said so'): Partial<WorkView> => ({
  safest_action: { action, reason, blocked_by: null }
});

const evidenceRow = (over: Partial<EvidenceRow> = {}): EvidenceRow => ({
  n: 1,
  label: 'Sandboxed',
  event: 'sandbox_resolved',
  summary: 'sandboxed',
  ts: '2026-09-24T15:24:15+00:00',
  state: 'attested',
  ...over
});

const chain = (rows: EvidenceRow[], over: Partial<ItemEvidence> = {}): ItemEvidence => ({
  item_id: '2055',
  rows,
  attested_count: rows.filter((r) => r.state === 'attested').length,
  total: rows.length,
  chain_verified: true,
  reason: '',
  unavailable: [],
  composed_at: '2026-09-24T18:12:04+00:00',
  ...over
});

/**
 * The ledger the DWork artboard draws: the committed one read mid-review, its
 * verdict row not yet recorded. The committed payload itself is composed after
 * round 1 returned READY (#3461), so all four of its rows are attested; the
 * artboard's dash into a pending row is pinned on this one instead.
 */
const ARTBOARD: ItemEvidence = chain(
  EVIDENCE.rows.map((r) =>
    r.n === 4 ? { ...r, summary: 'round 1 · reviewing', ts: '', state: 'pending' } : r
  )
);

const body = (over: Partial<ItemDescription> = {}): ItemDescription => ({
  ...DESCRIPTION,
  ...over
});

/**
 * A payload as FUI-31 composes a read that FAILED, field for field: the `url`,
 * the title and the body all come from the READING, so an answer that took no
 * reading carries none of them (`kew/service/description.py`, `_dto` with
 * `reading=None`). Spreading the available fixture into an `unavailable`
 * availability keeps a URL the endpoint cannot send, which is exactly how the
 * fallback link came to look covered while never rendering in the app.
 */
const failed = (over: Partial<ItemDescription> = {}): ItemDescription => ({
  item_id: DESCRIPTION.item_id,
  availability: 'unavailable',
  provenance: 'error',
  observed_at: null,
  title: '',
  url: null,
  description: '',
  expected: '',
  actual: '',
  reason: 'the tracker could not be read',
  composed_at: DESCRIPTION.composed_at,
  ...over
});

/**
 * The three reasons FUI-31 gives for a read it refused WITHOUT making it, worded
 * as `kew/service/description.py` words them (`SAME_ITEM_REASON`, `BUSY_REASON`,
 * `BUDGET_REASON` with this build's limits interpolated). Copies, and known to
 * be copies: `kew/tests/test_description_retry_parity.py` is what holds them to
 * the module, because these three are the only answers the tick takes again.
 */
const UNATTEMPTED = [
  'a read for this item is already in flight; this one was not attempted',
  '4 tracker reads are already in flight; this one was not attempted',
  'the tracker read budget (30 per 60s) is spent; this one was not attempted'
];

/** Reasons from a read that REACHED the tracker, and so already spent one. */
const ANSWERED = [
  'gh could not read the issue: non-zero exit',
  'the Linear read failed: HTTP 503 from the Linear API',
  'the tracker returned a response this read could not parse: Expecting value'
];

/** Rendered markup, comments stripped and whitespace between tags gone. */
function markup(body: string): string {
  return body.replace(/<!--[\s\S]*?-->/g, '').replace(/>\s+</g, '><');
}

/**
 * The band as the page draws it: one renderer over `needsYouBlock`'s model
 * (#2280 D9), given a row the needs-you SET does not name — which is the case
 * the band alone answers.
 */
const drawBand = (props: { view: WorkView | null }): string =>
  markup(
    render(NeedsYouBlock, {
      props: { block: needsYouBlock({ ...BARE_ROW, view: props.view }) }
    }).body
  );

const drawLedger = (props: { evidence: ItemEvidence | null; notice?: string }): string =>
  markup(render(EvidenceLedger, { props }).body);

const drawDescription = (props: { description: ItemDescription | null; notice?: string }): string =>
  markup(render(Description, { props }).body);

describe('who acts next', () => {
  it('classifies every safest action as a person`s move or kew`s', () => {
    // The parity anchor is the Python one; this only pins that the two tables
    // here agree — a title for an action nobody calls a person's is dead, and a
    // person's action with no title would head the band with a raw key.
    for (const [action, actor] of Object.entries(SAFEST_ACTION_ACTOR)) {
      expect(['person', 'kew'], action).toContain(actor);
      expect(Object.hasOwn(NEEDS_YOU_TITLE, action), action).toBe(actor === 'person');
    }
    for (const action of Object.keys(NEEDS_YOU_TITLE)) {
      expect(SAFEST_ACTION_ACTOR[action], action).toBe('person');
    }
  });

  it('reads an action it has never heard of as kew`s, not as a person`s', () => {
    // The loud direction would paint magenta for every action kew grows, which
    // is exactly the rationing leak this UI exists to avoid. The quiet direction
    // is caught by the Python parity test instead of by a false alarm here.
    expect(actionNeedsPerson('teleport_the_artifact')).toBe(false);
    expect(actionNeedsPerson('')).toBe(false);
    // `Object.hasOwn`, not `in`: a status word off the wire reaches this table.
    expect(actionNeedsPerson('constructor')).toBe(false);
    expect(actionNeedsPerson('__proto__')).toBe(false);
  });

  it('names kew`s own actions as kew`s, so no band appears over a button', () => {
    for (const action of ['dispatch', 'finalize_publication', 'wait', 'observe_remote', 'none'])
      expect(actionNeedsPerson(action), action).toBe(false);
  });

  it('names the decisions and the edits kew cannot perform', () => {
    for (const action of [
      'await_human_decision',
      'confirm_no_change',
      'confirm_publication_risk',
      'resolve_conflict',
      'fix_checks',
      'address_review_feedback',
      'resume_fleet',
      'inspect'
    ])
      expect(actionNeedsPerson(action), action).toBe(true);
  });
});

describe('the needs-you band', () => {
  it('is absent when nothing waits on a person', () => {
    expect(needsYouBand(null)).toBeNull();
    expect(
      needsYouBand(
        view({ judgment: { ...VIEW.judgment, state: 'accepted' }, ...safest('dispatch') })
      )
    ).toBeNull();
  });

  it('appears on the judgment alone, whatever the safest action is', () => {
    // The committed view is exactly this case: `awaiting_human_decision` with a
    // safest action of `wait`, because kew performs no merge.
    expect(VIEW.judgment.state).toBe(AWAITING_DECISION);
    expect(VIEW.safest_action.action).toBe('wait');
    const band = needsYouBand(VIEW)!;
    expect(band.key).toBe('await_human_decision');
    expect(band.title).toBe('Merge decision');
  });

  it('appears on a person`s safest action even with no judgment outstanding', () => {
    const band = needsYouBand(
      view({
        judgment: { ...VIEW.judgment, state: 'not_applicable' },
        ...safest('resolve_conflict', 'the artifact cannot merge until the conflict is resolved')
      })
    )!;
    expect(band.title).toBe('Merge conflict');
    expect(band.prose).toBe('the artifact cannot merge until the conflict is resolved');
  });

  it('names the move the projection ranked first, not the decision behind it', () => {
    // Awaiting a decision AND red: the safest action is the one to say out loud.
    const band = needsYouBand(view(safest('fix_checks', 'the artifact is published but red')))!;
    expect(band.key).toBe('fix_checks');
    expect(band.title).toBe('Checks failing');
  });

  it('words an unrecognised action rather than dropping the band', () => {
    // Only reachable through the judgment door today, but the title table is a
    // transcription: a band with a raw `snake_case` key in it is worse than one
    // with a spaced word, and no band at all is worse than either.
    const band = needsYouBand(view({ ...safest('wait'), judgment: VIEW.judgment }))!;
    expect(band.title).toBe('Merge decision');
  });

  it('prints the projection`s sentence and writes none of its own', () => {
    const band = needsYouBand(VIEW)!;
    expect(band.prose).toBe(VIEW.safest_action.reason);
    // Falls back to the exception's sentence, never to an invented one.
    const quiet = needsYouBand(view(safest('await_human_decision', '')))!;
    expect(quiet.prose).toBe(VIEW.exception.detail);
    const silent = needsYouBand(
      view({
        ...safest('await_human_decision', ''),
        exception: { ...VIEW.exception, detail: '' }
      })
    )!;
    expect(silent.prose).toBe('');
  });

  it('sends the reader to the pull request, and nowhere else', () => {
    expect(bandCta('await_human_decision', VIEW.artifact.pr_url)).toEqual({
      label: 'Open PR #2059 to merge',
      href: VIEW.artifact.pr_url,
      external: true
    });
    // The artboard's own words for the one call to action.
    expect(DWORK).toContain('Open PR #2059 to merge');
    // Another kind opens the same artifact without claiming a merge.
    expect(bandCta('fix_checks', VIEW.artifact.pr_url)?.label).toBe('Open PR #2059');
    // No artifact, no link: kew merges nothing, and there is nowhere to send
    // anyone for a stopped fleet.
    expect(bandCta('await_human_decision', null)).toBeNull();
    expect(bandCta('resume_fleet', '')).toBeNull();
    expect(needsYouBand(view({ artifact: { ...VIEW.artifact, pr_url: null } }))!.cta).toBeNull();
  });

  it('draws the block the artboard draws, and nothing when there is none', () => {
    const html = drawBand({ view: VIEW });
    expect(html).toContain('Needs you');
    expect(html).toContain('Merge decision');
    expect(html).toContain(VIEW.safest_action.reason);
    expect(html).toContain('Open PR #2059 to merge');
    expect(html).toContain(`href="${VIEW.artifact.pr_url}"`);
    expect(html).toContain('rel="noreferrer"');
    // The artboard's magenta left edge on the card, and its diamond.
    expect(html).toContain('border-l-accent');
    expect(UXWORK).toContain('border-left:3px solid var(--accent)');
    expect(html).toContain('text-accent');
    expect(statusShape('needs_you').shape).toBe('diamond-halo');
    // An item that needs nobody renders NO section at all.
    expect(
      drawBand({
        view: view({ judgment: { ...VIEW.judgment, state: 'accepted' }, ...safest('wait') })
      }).trim()
    ).toBe('');
    expect(drawBand({ view: null }).trim()).toBe('');
  });
});

describe('the evidence ledger', () => {
  it('zero-pads the index the way both artboards draw it', () => {
    expect([ledgerIndex(1), ledgerIndex(4), ledgerIndex(12)]).toEqual(['01', '04', '12']);
    expect(DWORK).toContain('>01<');
  });

  it('downgrades every attested row on a chain that does not verify', () => {
    const rows = EVIDENCE.rows;
    for (const row of rows) {
      expect(evidenceState(EVIDENCE, row)).toBe(row.state);
      expect(evidenceState(chain([...rows], { chain_verified: false }), row)).toBe(
        row.state === 'attested' ? 'unverified' : row.state
      );
    }
  });

  it('never draws a check on a row that is not attested', () => {
    const states = ['attested', 'unverified', 'pending', 'missing', 'a_word_nobody_serves'];
    const rows = ledgerRows(
      chain(states.map((state, i) => evidenceRow({ n: i + 1, state, ts: '' })))
    );
    expect(rows.map((r) => r.check)).toEqual([true, false, false, false, false]);
    // And on an unverified chain, not even the attested rows.
    const unverified = ledgerRows(
      chain(
        states.map((state, i) => evidenceRow({ n: i + 1, state, ts: '' })),
        { chain_verified: false }
      )
    );
    expect(unverified.some((r) => r.check)).toBe(false);
    expect(unverified[0].word).toBe(statusShape('unverified').word);
  });

  it('follows the next row`s state with the rail segment', () => {
    expect(segmentInto('pending')).toBe('dashed');
    for (const state of ['attested', 'unverified', 'missing', 'anything_else'])
      expect(segmentInto(state), state).toBe('solid');
    const rows = ledgerRows(ARTBOARD);
    // The artboard: three solid links, then row 03 dashing into a pending 04.
    expect(rows.map((r) => r.above)).toEqual([null, 'solid', 'solid', 'dashed']);
    expect(rows.map((r) => r.below)).toEqual(['solid', 'solid', 'dashed', null]);
    // The rail between two rows is ONE decision, read the same way from above
    // and below it: a row whose neighbour disagreed would draw a broken line.
    for (let i = 0; i < rows.length - 1; i += 1)
      expect(rows[i].below, `${i}`).toBe(rows[i + 1].above);
    // The committed ledger, its READY verdict recorded (#3461): solid throughout.
    expect(ledgerRows(EVIDENCE).map((r) => r.above)).toEqual([null, 'solid', 'solid', 'solid']);
  });

  it('reads the recorded time, and never borrows another row`s', () => {
    const rows = ledgerRows(ARTBOARD);
    expect(rows.map((r) => r.time)).toEqual(['15:24:15', '15:46:50', '15:46:54', '—']);
    expect(rows.map((r) => r.timed)).toEqual([true, true, true, false]);
    // The artboard's own times, and its em dash on the row with no fact yet.
    for (const time of ['15:24:15', '15:46:50', '15:46:54']) expect(DWORK).toContain(time);
    // The committed verdict row reads its own recorded instant, not row 03's.
    const verdict = ledgerRows(EVIDENCE)[3];
    expect([verdict.time, verdict.timed]).toEqual(['14:46:40', true]);
  });

  it('prints the payload`s own words for the event and the summary', () => {
    const rows = ledgerRows(EVIDENCE);
    expect(rows[0].summary).toBe('sandbox_resolved · sandboxed');
    expect(rows[2].summary).toBe('publication_output_scan · clean · 3670e43f');
    // Either half alone when the payload sent only one of them.
    expect(ledgerRows(chain([evidenceRow({ summary: '' })]))[0].summary).toBe('sandbox_resolved');
    expect(ledgerRows(chain([evidenceRow({ event: '' })]))[0].summary).toBe('sandboxed');
  });

  it('counts what the server counted, zeroed by the server`s own verdict', () => {
    const model = evidenceLedger(ARTBOARD);
    expect([model.attested, model.total]).toEqual([3, 4]);
    expect(model.count).toBe('3 / 4 attested');
    expect(DWORK).toContain('/ 4 attested');
    // The committed ledger counts the READY verdict its pipeline also reads (#3461).
    expect(evidenceLedger(EVIDENCE).count).toBe('4 / 4 attested');
    const broken = evidenceLedger(chain([...EVIDENCE.rows], { chain_verified: false }));
    expect(broken.attested).toBe(0);
    expect(broken.count).toBe('0 / 4 attested');
  });

  it('says why when it has no rows, and never counts to zero over nothing', () => {
    const unread = evidenceLedger(null, 'the evidence chain could not be read (500)');
    expect(unread.empty).toBe(true);
    expect(unread.count).toBe('—');
    expect(unread.reason).toBe('the evidence chain could not be read (500)');
    // A payload that answered with no rows is a different thing from a failure.
    expect(evidenceLedger(chain([])).reason).toBe(LEDGER_ABSENT);
    // The server's sentence rides along only when there are rows it explains.
    const reasoned = evidenceLedger(chain([...EVIDENCE.rows], { reason: 'no run recorded' }));
    expect(reasoned.notice).toBe('no run recorded');
    expect(evidenceLedger(chain([], { reason: 'no run recorded' })).notice).toBe('');
  });

  it('claims nothing about the chain while the read is still out', () => {
    // The regression (#2155 review): no payload AND no failure is a read this
    // page took a moment ago, not an item whose chain is bare — and
    // `LEDGER_ABSENT` is a positive claim about the operator's own item. The
    // sibling Pipeline section draws nothing at all in the same state.
    const model = evidenceLedger(null);
    expect(model.pending).toBe(true);
    expect(model.reason).toBe('');
    const html = drawLedger({ evidence: null, notice: '' });
    expect(html).not.toContain(LEDGER_ABSENT);
    expect(html).not.toContain('<ol');
    // No empty paragraph left behind where the claim used to be.
    expect(html).not.toContain('<p');
    // A payload that ANSWERED with no rows is the claim, and still makes it.
    expect(evidenceLedger(chain([])).pending).toBe(false);
    expect(evidenceLedger(chain([])).reason).toBe(LEDGER_ABSENT);
    // So is a read that failed, in the words the page gave it.
    const failedRead = evidenceLedger(null, 'the evidence chain could not be read (500)');
    expect(failedRead.pending).toBe(false);
    expect(failedRead.reason).toBe('the evidence chain could not be read (500)');
  });

  it('draws the artboard`s 50px rows, the rail and the one check', () => {
    const html = drawLedger({ evidence: ARTBOARD, notice: '' });
    expect(html).toContain('Evidence ledger');
    expect(html).toContain('grid-cols-[30px_24px_minmax(0,1fr)_auto]');
    expect(html).toContain('h-[50px]');
    expect(DWORK).toContain('grid-template-columns: 30px 24px minmax(0, 1fr) auto');
    expect(DWORK).toContain('height: 50px');
    // The head greens the numerator alone.
    expect(html).toContain('<span class="text-success">3</span> / 4 attested');
    // Three checks for three attested rows, and the cyan dash into the pending
    // one: the working colour, at the point the record stops.
    expect(html.match(/M5 12\.5 10 17 19 7/g)).toHaveLength(3);
    expect(html).toContain('border-accent-2');
    expect(html).toContain('border-hair-strong');
    for (const row of EVIDENCE.rows) expect(html).toContain(row.label);
  });

  it('draws no check at all for a chain kew has not vouched for', () => {
    const html = drawLedger({
      evidence: chain([...EVIDENCE.rows], { chain_verified: false, reason: 'the chain is broken' }),
      notice: ''
    });
    expect(html).not.toContain('M5 12.5 10 17 19 7');
    expect(html).toContain('<span class="text-success">0</span> / 4 attested');
    expect(html).toContain('the chain is broken');
    expect(html).toContain(statusShape('unverified').word);
  });

  it('says a failed read out loud instead of drawing an empty ledger', () => {
    const html = drawLedger({ evidence: null, notice: 'the evidence chain could not be read' });
    expect(html).toContain('the evidence chain could not be read');
    expect(html).not.toContain('<ol');
  });

  it('words a failed read with the status the fetch reported', () => {
    expect(readFailure(new Error('/api/work/2055/evidence -> 500'), 'the evidence chain')).toBe(
      'the evidence chain could not be read (500)'
    );
    expect(readFailure(new TypeError('network down'), 'the description')).toBe(
      'the description could not be read'
    );
  });
});

describe('the description panel', () => {
  it('draws the body only for a reading the server stands behind', () => {
    expect(descriptionPanel(DESCRIPTION).blocks).toHaveLength(1);
    expect(descriptionPanel(body({ availability: 'stale' })).blocks).toHaveLength(1);
    for (const availability of ['unavailable', 'error', '', 'AVAILABLE']) {
      // A payload holding a body AND a url under an availability this file does
      // not stand behind: the prose is refused whatever else rode along, and
      // the link follows the url the payload named — not the availability.
      const panel = descriptionPanel(body({ availability, reason: 'the tracker timed out' }));
      expect(panel.blocks, availability).toEqual([]);
      expect(panel.sections, availability).toEqual([]);
      expect(panel.notice, availability).toBe('the tracker timed out');
      expect(panel.link?.href, availability).toBe(DESCRIPTION.url);
    }
  });

  it('offers no link for a failed read, because the server named none', () => {
    // The regression (#2155 review): `url` is the READING's, so every
    // `unavailable` answer carries `url: null` — and this panel will not
    // invent a tracker URL from the item's id, so that answer is its reason
    // by itself. The link renders for a payload that named a URL and had
    // nothing to draw, which is the empty-body read below.
    const panel = descriptionPanel(failed());
    expect(panel.link).toBeNull();
    expect(panel.notice).toBe('the tracker could not be read');
    expect(panel.empty).toBe(true);
    // Every other urlless failure the endpoint composes answers the same way.
    for (const reason of ['no delivery repository is configured (project.repo)', '']) {
      expect(descriptionPanel(failed({ reason })).link, reason).toBeNull();
    }
    expect(descriptionPanel(failed({ reason: '' })).notice).toBe(DESCRIPTION_UNSYNCED);
  });

  it.each(['javascript:alert(1)', '/work?item=1', 'data:text/html,x'])(
    'offers no link for a served URL safeHref refuses (%j)',
    (url) => {
      expect(descriptionPanel(failed({ url })).link).toBeNull();
      expect(bandCta('await_human_decision', url)).toBeNull();
    }
  );

  it('labels a stale reading rather than passing it off as current', () => {
    const panel = descriptionPanel(body({ availability: 'stale', reason: 'the tracker timed out' }));
    expect(panel.stale).toBe('older reading; the tracker timed out');
    expect(descriptionPanel(DESCRIPTION).stale).toBe('');
  });

  it('says why with a plain note when there is no reading at all', () => {
    const panel = descriptionPanel(null, 'the description could not be read (500)');
    expect(panel.empty).toBe(true);
    expect(panel.notice).toBe('the description could not be read (500)');
    // No payload at all names no URL, so there is not even a link to offer.
    expect(panel.link).toBeNull();
    // The plain note belongs to a read that ARRIVED saying nothing; a read
    // still out says nothing at all (the pending test below).
    expect(descriptionPanel(null).notice).toBe('');
  });

  it('names what a closed panel holds: its size in words, or why there is none (#2786)', () => {
    const words = [DESCRIPTION.description, DESCRIPTION.expected, DESCRIPTION.actual]
      .join(' ')
      .split(/\s+/)
      .filter(Boolean).length;
    expect(words).toBeGreaterThan(1);
    expect(descriptionPanel(DESCRIPTION).contents).toBe(String(words) + ' words');
    expect(descriptionPanel(body({ availability: 'stale' })).contents).toMatch(
      / words · older reading$/
    );
    expect(descriptionPanel(failed({ reason: 'the tracker timed out' })).contents).toBe(
      'the tracker timed out'
    );
    expect(descriptionPanel(null).contents).toBe('');
  });

  it('claims nothing about the description while the read is still out', () => {
    // The regression (#2155 review): "the description is not synced into kew"
    // is a positive claim about the operator's own item, and the instant after
    // a row is selected is when this page knows least about it.
    const model = descriptionPanel(null);
    expect(model.pending).toBe(true);
    expect(model.notice).toBe('');
    expect(model.link).toBeNull();
    const html = drawDescription({ description: null, notice: '' });
    expect(html).not.toContain(DESCRIPTION_UNSYNCED);
    expect(html).not.toContain('<p');
    expect(html).not.toContain('<a');
    // A payload that landed with no reason of its own is the note's own case,
    // and so is a read the page reported as failed.
    expect(descriptionPanel(failed({ reason: '' })).pending).toBe(false);
    expect(descriptionPanel(failed({ reason: '' })).notice).toBe(DESCRIPTION_UNSYNCED);
    expect(descriptionPanel(null, 'the description could not be read (500)').pending).toBe(
      false
    );
  });

  it('re-reads the refusals the tracker never heard', () => {
    // The other half of the #2155 review finding: FUI-31 refuses a read while
    // the same item's read is out, while the process-wide bound is full or
    // while the minute's budget is spent — refusals that clear on their own.
    // The description is not on the poll, so without this the panel would hold
    // one forever: a reason with no body, and no URL to leave by either.
    for (const reason of UNATTEMPTED) {
      expect(reason, reason).toContain(DESCRIPTION_UNATTEMPTED);
      expect(rereadsDescription(failed({ reason }), 0), reason).toBe(true);
    }
    // No payload at all is the one case decided without a reason: the read is
    // still out (which the page checks for separately), or this console's own
    // read of the endpoint threw.
    expect(rereadsDescription(null, 0)).toBe(true);
    // A body on screen is left alone: the tracker is not on this clock, and a
    // stale reading is still a reading.
    expect(rereadsDescription(DESCRIPTION, 0)).toBe(false);
    expect(rereadsDescription(body({ availability: 'stale' }), 0)).toBe(false);
    // It is the panel's own question, asked once.
    expect(descriptionUnread(DESCRIPTION)).toBe(false);
    expect(descriptionUnread(failed())).toBe(true);
  });

  it('never re-reads an answer the tracker was actually asked for', () => {
    // The regression (#2155 review, round 3): re-reading on availability alone
    // took a reading that DID reach GitHub as freely as the three cost-free
    // gate refusals above, so ONE unreadable item spent 11 of the 30 reads a
    // minute the whole process shares with the dispatch driver — rationing away
    // every other item's and every other operator's cold read, to be told the
    // same thing eleven times.
    for (const reason of [
      ...ANSWERED,
      // Free like the gates, but settled: nothing a tick can change.
      'no delivery repository is configured (project.repo)',
      'FUI-31 is not a GitHub issue number',
      "tracker.type is 'linear' but the LINEAR_API_KEY environment variable is not set",
      "tracker 'jira' has no description read",
      // A payload that arrived with no reason of its own says nothing a retry
      // would learn either; the panel has its own note for that one.
      ''
    ]) {
      expect(reason, reason).not.toContain(DESCRIPTION_UNATTEMPTED);
      expect(rereadsDescription(failed({ reason }), 0), reason).toBe(false);
    }
    // The availability it arrived under decides nothing here — an unknown one
    // is still an answer, and it is the reason that says what the read cost.
    for (const availability of ['unavailable', 'error', '', 'AVAILABLE'])
      expect(
        rereadsDescription(failed({ availability, reason: ANSWERED[0] }), 0),
        availability
      ).toBe(false);
  });

  it('stops re-reading, so a tracker that cannot be read is not polled', () => {
    expect(rereadsDescription(null, DESCRIPTION_RETRIES - 1)).toBe(true);
    expect(rereadsDescription(null, DESCRIPTION_RETRIES)).toBe(false);
    // The bound holds for the refusals that are re-read, which is the path the
    // budget window is counted against.
    const spent = failed({ reason: UNATTEMPTED[2] });
    expect(rereadsDescription(spent, DESCRIPTION_RETRIES - 1)).toBe(true);
    expect(rereadsDescription(spent, DESCRIPTION_RETRIES)).toBe(false);
    // Long enough to outlast the longest window FUI-31 refuses a read for —
    // the 60s read budget — at the shell's own tick.
    expect(DESCRIPTION_RETRIES * PULSE_INTERVAL_MS).toBeGreaterThan(60_000);
  });

  it('keeps an empty body under a read that worked apart from a failed one', () => {
    const panel = descriptionPanel(body({ description: '', expected: '', actual: '' }));
    expect(panel.notice).toBe(DESCRIPTION_EMPTY);
    expect(panel.link?.label).toBe(DESCRIPTION_LINK);
    expect(panel.empty).toBe(true);
    // The same three empty fields under a failed read are not that answer: it
    // says why the read failed, in the server's words, and offers no link.
    const unread = descriptionPanel(failed());
    expect(unread.notice).not.toBe(DESCRIPTION_EMPTY);
    expect(unread.notice).toBe('the tracker could not be read');
    expect(unread.link).toBeNull();
  });

  it('reads the body as the markdown subset, blank lines and all', () => {
    // The blocks themselves are `markdown.test.ts`; what is pinned here is that
    // the panel reads a body through that tokenizer at all, rather than
    // printing it raw as it did before Design C1.
    expect(descriptionPanel(body({ description: '## head\n\ntext' })).blocks).toEqual([
      { kind: 'heading', level: 2, inline: [{ kind: 'text', text: 'head' }] },
      { kind: 'paragraph', inline: [{ kind: 'text', text: 'text' }] }
    ]);
    expect(descriptionPanel(body({ description: '  ' })).blocks).toEqual([]);
  });

  it('draws the sections the body named for itself, and no others', () => {
    expect(descriptionPanel(DESCRIPTION).sections).toEqual([
      { key: 'expected', label: 'Expected', text: DESCRIPTION.expected }
    ]);
    const both = descriptionPanel(body({ actual: 'the counter never fires' }));
    expect(both.sections.map((s) => s.key)).toEqual(['expected', 'actual']);
  });

  it('draws the artboard`s panel, and the fallback when it cannot', () => {
    const html = drawDescription({ description: DESCRIPTION, notice: '' });
    expect(html).toContain('Description');
    expect(html).toContain('the glue attack is caught by unparsed lines');
    expect(html).toContain('Expected');
    expect(html).toContain(DESCRIPTION.expected);
    expect(DWORK).toContain('the glue attack is caught by unparsed lines');
    // The artboard's inline 11px label, not a heading block.
    expect(html).toContain('class="mr-2 text-11 text-text-1">Expected</span>');
    // A body that is shown needs no link out of the panel.
    expect(html).not.toContain(DESCRIPTION_LINK);

    // The fallback link, drawn for the answer that really carries a URL with
    // nothing to put above it: a read that worked and found an empty body.
    const fallback = drawDescription({
      description: body({ description: '', expected: '', actual: '' }),
      notice: ''
    });
    expect(fallback).toContain(DESCRIPTION_LINK);
    expect(fallback).toContain(DESCRIPTION_EMPTY);
    expect(fallback).toContain(`href="${DESCRIPTION.url}"`);
    expect(fallback).toContain('rel="noreferrer"');
    expect(fallback).not.toContain('the glue attack is caught by unparsed lines');

    // A FAILED read names no URL, so the panel is the server's reason and no
    // link at all — never a dead "Open on GitHub" with nowhere to go.
    const unread = drawDescription({ description: failed(), notice: '' });
    expect(unread).toContain('the tracker could not be read');
    expect(unread).not.toContain(DESCRIPTION_LINK);
    expect(unread).not.toContain('<a');
    expect(unread).not.toContain('the glue attack is caught by unparsed lines');
  });
});

describe('the lower grid', () => {
  it('is the artboard`s 5fr / 7fr split', () => {
    const page = readFileSync(
      new URL('../../routes/(factory)/work/+page.svelte', import.meta.url),
      'utf8'
    );
    expect(page).toContain('grid-cols-[5fr_7fr]');
    expect(DWORK).toContain('grid-template-columns: 5fr 7fr');
  });

  it('drops all three per-item reads when the selection moves', () => {
    // A guarantee about the page, not about a model: another item's evidence
    // under this item's header is the one failure none of the models above can
    // catch, because each of them is handed exactly what the page kept.
    const page = readFileSync(
      new URL('../../routes/(factory)/work/+page.svelte', import.meta.url),
      'utf8'
    );
    const effect = page.slice(page.indexOf('if (id === detailItem) return;'));
    const reset = effect.slice(0, effect.indexOf('}'));
    for (const cleared of [
      'pipeline = null',
      'evidence = null',
      'description = null',
      // The retry budget is the new item's too, not what the last one spent,
      // and every read still out is superseded (`$lib/work/reads`).
      'descriptionReads.select()'
    ])
      expect(reset, cleared).toContain(cleared);
  });

  it('takes the description read again on the tick while it has no body', () => {
    const page = readFileSync(
      new URL('../../routes/(factory)/work/+page.svelte', import.meta.url),
      'utf8'
    );
    const tick = page.slice(page.indexOf('const firstTick'), page.indexOf('async function reread'));
    // `rereadsDescription` decides which answers are worth taking again; the
    // ledger decides whether a tick may take one at all, and counts the retry.
    // Never a second read of an item whose read is already out: the endpoint
    // refuses that one, and the refusal would spend a retry on itself —
    // `reads.test.ts` is where that is driven through the sequence that broke.
    expect(tick).toContain('descriptionReads.claimReread(description)');
    expect(tick).toContain('void readDescription(detailItem)');
  });
});
