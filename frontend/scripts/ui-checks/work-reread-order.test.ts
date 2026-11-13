/**
 * The out-of-order Work re-read checks' own test (#2933): they fail when they
 * should, without a browser. A page here is a clock, the routes a check
 * serves, a selection and a screen: it reads all three on load and again on
 * every pulse after the first, without waiting for the last pulse's reads to
 * answer. A `guarded` read draws an answer only if no newer answer was drawn
 * before it; an unguarded one draws every answer as it lands — the page #2933
 * found. A selection's epoch is guarded apart, so each check's negative
 * control removes only its own guard. A selection also reads the item's
 * description, which the fixture server answers out of the committed file or
 * with a 404 the browser logs as a console error.
 */
import { existsSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { NewestReads } from '../../src/lib/work/reads';
import { ITEM, versions } from './work-reread.mjs';
import {
  OTHER,
  earlierSelectionDropped,
  lateAnswerDropped,
  serveOutOfOrder,
  serveSelections
} from './work-reread-order.mjs';

type Key = 'work' | 'pipeline' | 'evidence';
type Body = Record<string, unknown>;
type Request = { key: Key | 'description'; item: string };
type Route = { fulfill: (r: { body: string }) => Promise<void>; request: () => Request };
type Handler = (route: Route) => unknown;

const PULSE_MS = 7_000;
const KEYS: Key[] = ['work', 'pipeline', 'evidence'];

/** The glob a read of `key` for `item` is routed by. */
const globOf = (key: Key, item: string) =>
  key === 'work' ? '**/api/dashboard/work' : `**/api/work/${item}/${key}`;

/** Whether the fixture server has a description of `item` to answer with. */
const describedByFixture = (item: string) =>
  existsSync(new URL(`../../fixtures/factory/work-${item}-description.json`, import.meta.url));

/** What the real page draws from each body in the three places' own elements. */
function draw(drawn: Partial<Record<Key, Body>>) {
  const { work, pipeline, evidence } = drawn;
  const view = (
    (work?.views ?? []) as { identity: { item_id: string }; exception: { state: string } }[]
  ).find((v) => v.identity.item_id === ITEM);
  const stages = ((pipeline?.lanes ?? []) as { stages: { stage: string; label: string }[] }[]).flatMap(
    (lane) => lane.stages
  );
  const verdict = ((evidence?.rows ?? []) as { event: string; summary: string }[]).find(
    (r) => r.event === 'adversarial_review_verdict'
  );
  return {
    row: view === undefined ? '' : view.exception.state === 'none' ? 'done' : 'in review',
    stage: stages.find((s) => s.stage === 'merge')?.label ?? '',
    line: verdict ? `${verdict.event} · ${verdict.summary}` : ''
  };
}

/**
 * A Work page selecting `ITEM`. Its pulse reads of `guarded` keep the newest
 * answer drawn, and its page load's list read is in the same ledger; with
 * `epochs` a selection starts a new epoch of the detail ledgers. A read
 * nothing routes answers the committed fixture at once, as the fixture server
 * does.
 */
function fakePage({
  guarded = KEYS,
  epochs = true
}: { guarded?: Key[]; epochs?: boolean } = {}) {
  const handlers = new Map<string, Handler>();
  /** The console errors the page has logged, as the browser's `problems`. */
  const problems: string[] = [];
  const fixture = versions().a as Record<Key, Body>;
  const drawn: Partial<Record<Key, Body>> = {};
  const reads = { work: new NewestReads(), pipeline: new NewestReads(), evidence: new NewestReads() };
  const finished = new Map<Request, () => void>();
  const landed = new Set<Request>();
  const sent: Request[] = [];
  let selected = ITEM;
  let started = false;
  let clock = 0;
  let pulses = 0;

  const ask = (key: Key, guard: boolean): Promise<void> => {
    const generation = reads[key].begin();
    const request: Request = { key, item: selected };
    sent.push(request);
    const answered = new Promise<string>((resolve) => {
      const fulfill = async ({ body }: { body: string }) => {
        resolve(body);
        landed.add(request);
        finished.get(request)?.();
      };
      const handler = handlers.get(globOf(key, request.item));
      if (handler) void handler({ request: () => request, fulfill });
      else void fulfill({ body: JSON.stringify(fixture[key]) });
    });
    return answered.then((body) => {
      // The detail pane drops an answer for an item it is no longer showing.
      if (key !== 'work' && request.item !== selected) return;
      if (guard && !reads[key].take(generation)) return;
      drawn[key] = JSON.parse(body) as Body;
    });
  };
  const readDetail = () => {
    for (const key of ['pipeline', 'evidence'] as const) void ask(key, guarded.includes(key));
  };
  /** A selection's description read: routed, or the fixture server's file or 404. */
  const readDescription = () => {
    const glob = `**/api/work/${selected}/description`;
    const handler = handlers.get(glob);
    const request: Request = { key: 'description', item: selected };
    if (handler) void handler({ request: () => request, fulfill: async () => {} });
    else if (!describedByFixture(selected))
      problems.push(`console error: ${glob} answered 404 (Not Found)`);
  };
  /** The page loads on its first look: the loader's list read and the selection's reads. */
  const start = () => {
    if (started) return;
    started = true;
    void ask('work', true);
    readDetail();
  };
  /** Let every answer that has landed be drawn. */
  const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

  return {
    problems,
    async route(glob: string, handler: Handler) {
      handlers.set(glob, handler);
    },
    async evaluate() {
      start();
      await flush();
      return { ...draw(drawn), kept: true, hid: '' };
    },
    /** Playwright's `requestfinished`: resolves once the request's answer lands. */
    waitForEvent(_event: string, { predicate }: { predicate: (r: Request) => boolean }) {
      const request = sent.find(predicate);
      if (!request) throw new Error('waited on a request the page never sent');
      return new Promise<void>((resolve) =>
        landed.has(request) ? resolve() : finished.set(request, resolve)
      );
    },
    /** Select `item` in place: the pane is cleared and its reads are taken. */
    async select(item: string) {
      start();
      selected = item;
      delete drawn.pipeline;
      delete drawn.evidence;
      if (epochs) for (const key of ['pipeline', 'evidence'] as const) reads[key].select();
      readDetail();
      readDescription();
      await flush();
    },
    async sleep(ms: number) {
      start();
      const end = clock + ms;
      while (clock + (PULSE_MS - (clock % PULSE_MS)) <= end) {
        clock += PULSE_MS - (clock % PULSE_MS);
        pulses += 1;
        // The first pulse is the reading `load()` already took.
        if (pulses > 1) {
          void ask('work', guarded.includes('work'));
          readDetail();
        }
      }
      clock = end;
      await flush();
    }
  };
}

function fail(ok: boolean, message: string) {
  if (!ok) throw new Error(message);
}

async function check(page: ReturnType<typeof fakePage>, places?: string[]) {
  const served = await serveOutOfOrder(page);
  try {
    await lateAnswerDropped(page, served, { sleep: (ms: number) => page.sleep(ms), expect: fail, places });
  } finally {
    await served.releaseRest();
  }
}

const LATE = /the older answer was drawn over the newer/;

describe('lateAnswerDropped', () => {
  it('passes a page that keeps the newest answer drawn', async () => {
    await expect(check(fakePage())).resolves.toBeUndefined();
  });

  it('fails a page that draws every answer as it lands (negative control)', async () => {
    await expect(check(fakePage({ guarded: [] }))).rejects.toThrow(LATE);
  });

  for (const [key, place, what] of [
    ['work', 'row', 'the Work row'],
    ['pipeline', 'stage', 'the pipeline’s merge stage'],
    ['evidence', 'line', 'the evidence ledger’s verdict line']
  ] as const) {
    it(`fails a page with no guard on the ${key} read alone, and names ${what}`, async () => {
      const page = fakePage({ guarded: KEYS.filter((k) => k !== key) });
      const error = await check(page).then(
        () => null,
        (e: Error) => e
      );
      expect(error?.message).toMatch(LATE);
      expect(error?.message.split('\n').filter((l) => l.startsWith('the '))).toEqual([
        expect.stringContaining(`${what} reads`)
      ]);
      // Checked alone, as the phone visits check it, the place still fails.
      await expect(check(fakePage({ guarded: KEYS.filter((k) => k !== key) }), [place])).rejects.toThrow(
        LATE
      );
    });
  }
});

async function checkSelections(page: ReturnType<typeof fakePage>) {
  const served = await serveSelections(page);
  try {
    await earlierSelectionDropped(page, served, {
      sleep: (ms: number) => page.sleep(ms),
      expect: fail,
      select: (item: string) => page.select(item),
      reopen: async () => {}
    });
    fail(page.problems.length === 0, page.problems.join('\n'));
  } finally {
    await served.releaseRest();
  }
}

describe('earlierSelectionDropped', () => {
  it('passes a page whose detail ledgers start an epoch per selection', async () => {
    await expect(checkSelections(fakePage())).resolves.toBeUndefined();
  });

  it('fails a page whose detail ledgers keep no selection epoch (negative control)', async () => {
    const error = await checkSelections(fakePage({ epochs: false })).then(
      () => null,
      (e: Error) => e
    );
    expect(error?.message).toMatch(new RegExp(`an answer from before A -> ${OTHER} -> A was drawn`));
    expect(error?.message).toContain('the pipeline’s merge stage reads');
    expect(error?.message).toContain('the evidence ledger’s verdict line reads');
  });

  it(`fails when selecting ${OTHER} reads a description the fixture server 404s (negative control)`, async () => {
    // The committed fixtures still lack it, so the route is what keeps the visit clean.
    expect(describedByFixture(OTHER)).toBe(false);
    const page = fakePage();
    const unrouted = {
      ...page,
      route: (glob: string, handler: Handler) =>
        glob.endsWith('/description') ? Promise.resolve() : page.route(glob, handler)
    };
    await expect(checkSelections(unrouted)).rejects.toThrow(
      new RegExp(`/api/work/${OTHER}/description answered 404`)
    );
  });
});
