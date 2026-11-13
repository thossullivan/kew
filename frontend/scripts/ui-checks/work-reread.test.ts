/**
 * The Work re-read check's own test (#2916): it fails when it should, without a
 * browser. A page here is a clock, the three routes the check serves, and a
 * screen: it asks for all three reads on load and again on every pulse after
 * the first, and it draws an answer only where it is told to take it. So a
 * page that sends every re-read and drops what comes back — the one
 * `hidden-tab.mjs` cannot tell from a working page — is a line, and must fail.
 */
import { describe, expect, it } from 'vitest';
import {
  HIDINGS,
  ITEM,
  READS,
  REREAD_MS,
  VISITS,
  hiddenFails,
  pulseApplied,
  serveVersions,
  versions
} from './work-reread.mjs';

type Key = 'work' | 'pipeline' | 'evidence';
type Body = Record<string, unknown>;
type Handler = (route: { fulfill: (r: { body: string }) => Promise<void> }) => Promise<void>;

const PULSE_MS = 7_000;
const KEYS: Key[] = ['work', 'pipeline', 'evidence'];

/** Which read a glob the check routes is for. */
const keyOf = (glob: string): Key =>
  glob.endsWith('/dashboard/work') ? 'work' : glob.endsWith('/pipeline') ? 'pipeline' : 'evidence';

/** What the real page draws from each body in the three places' own elements:
 * the row's state word, the merge stage's label, the verdict's summary. */
function draw({ work, pipeline, evidence }: Record<Key, Body>) {
  const view = (work.views as { identity: { item_id: string }; exception: { state: string } }[]).find(
    (v) => v.identity.item_id === ITEM
  );
  const stages = (pipeline.lanes as { stages: { stage: string; label: string }[] }[]).flatMap(
    (lane) => lane.stages
  );
  const rows = evidence.rows as { event: string; summary: string }[];
  const verdict = rows.find((r) => r.event === 'adversarial_review_verdict');
  return {
    row: view?.exception.state === 'none' ? 'done' : 'in review',
    stage: stages.find((s) => s.stage === 'merge')?.label ?? '',
    line: verdict ? `${verdict.event} · ${verdict.summary}` : ''
  };
}

/** What the check passes the in-page reader (`look`). */
type Place = keyof typeof READS;
type Look = {
  item?: string;
  hide?: { place: Place; value: string; name: string; css: string };
  unhide?: boolean;
};

/**
 * A Work page. `takes` names the reads whose re-read answers it draws (all of
 * them by default); `reloads` reloads it a second after the server moves.
 * `misses` names the `HIDINGS` its reading does not notice: under one of them
 * the value still reads, as it would to a reader of a container's
 * `textContent`, of every node regardless of colour or clip, or of the place's
 * siblings. Every other hiding blanks the place, as the real reader sees it.
 */
function fakePage({
  takes = KEYS,
  reloads = false,
  misses = []
}: { takes?: Key[]; reloads?: boolean; misses?: string[] } = {}) {
  const handlers = new Map<Key, Handler>();
  const drawn = {} as Record<Key, Body>;
  const asked = { work: 0, pipeline: 0, evidence: 0 };
  let kept = false;
  let clock = 0;
  let pulses = 0;
  let flippedAt: number | null = null;
  let hidden: Place | null = null;

  const ask = async (key: Key): Promise<Body> => {
    asked[key] += 1;
    let body = '';
    await handlers.get(key)!({ fulfill: async (r) => void (body = r.body) });
    return JSON.parse(body) as Body;
  };
  const load = async () => {
    kept = false;
    for (const key of KEYS) drawn[key] = await ask(key);
  };
  let loaded: Promise<void> | null = null;

  const page = {
    asked,
    async route(glob: string, handler: Handler) {
      handlers.set(keyOf(glob), handler);
      if (handlers.size === KEYS.length) loaded = load();
    },
    async evaluate(_fn: unknown, look?: Look) {
      await loaded;
      if (look?.item !== ITEM) {
        kept = true;
        flippedAt = clock;
        return undefined;
      }
      if (look.unhide) hidden = null;
      if (look.hide && !misses.includes(look.hide.name)) hidden = look.hide.place;
      const screen = { ...draw(drawn), kept, hid: look.hide ? 'span' : '' };
      if (hidden) screen[hidden] = '';
      return screen;
    },
    async sleep(ms: number) {
      const end = clock + ms;
      while (clock + (PULSE_MS - (clock % PULSE_MS)) <= end) {
        clock += PULSE_MS - (clock % PULSE_MS);
        pulses += 1;
        // The first pulse is the reading `load()` already took.
        if (pulses === 1) continue;
        for (const key of KEYS) {
          const body = await ask(key);
          if (takes.includes(key)) drawn[key] = body;
        }
      }
      clock = end;
      if (reloads && flippedAt !== null && clock - flippedAt >= 1_000 && kept) await load();
    }
  };
  return page;
}

function fail(ok: boolean, message: string) {
  if (!ok) throw new Error(message);
}

async function check(page: ReturnType<typeof fakePage>, places?: string[]) {
  const served = await serveVersions(page);
  return pulseApplied(page, served, { sleep: (ms: number) => page.sleep(ms), expect: fail, places });
}

describe('version B', () => {
  it('moves every place the check reads, and nothing reads B before it', () => {
    const { a, b } = versions();
    const before = draw(a);
    const after = draw(b);
    for (const [key, { a: was, b: now }] of Object.entries(READS)) {
      const place = key as keyof typeof before;
      expect(was, key).not.toBe(now);
      expect(before[place], key).toBe(was);
      expect(after[place], key).toBe(now);
    }
  });
});

describe('pulseApplied', () => {
  it('passes a page that draws what its pulse re-reads return', async () => {
    await expect(check(fakePage())).resolves.toBeUndefined();
  });

  it('fails a page that sends every re-read and drops every result (negative control)', async () => {
    const page = fakePage({ takes: [] });
    await expect(check(page)).rejects.toThrow(/the re-reads ran and were dropped/);
    // Every read was asked for again after load: the requests alone prove nothing.
    for (const key of KEYS) expect(page.asked[key], key).toBeGreaterThan(1);
  });

  for (const [key, place] of [
    ['work', 'row'],
    ['pipeline', 'stage'],
    ['evidence', 'line']
  ] as const) {
    it(`fails a page that drops only the ${key} re-read, and names ${READS[place].what}`, async () => {
      const page = fakePage({ takes: KEYS.filter((k) => k !== key) });
      const error = await check(page).then(
        () => null,
        (e: Error) => e
      );
      expect(error?.message).toContain(`${READS[place].what} still reads`);
      for (const other of Object.keys(READS).filter((p) => p !== place)) {
        expect(error?.message).not.toContain(`${READS[other as typeof place].what} still reads`);
      }
    });
  }

  it('fails a page that dropped the row, when only the row is checked (the phone list)', async () => {
    await expect(check(fakePage({ takes: ['pipeline', 'evidence'] }), ['row'])).rejects.toThrow(
      /the Work row still reads "in review"/
    );
    await expect(check(fakePage(), ['row'])).resolves.toBeUndefined();
  });

  it('fails a page whose version B arrived by a reload, not a re-read', async () => {
    await expect(check(fakePage({ takes: [], reloads: true }))).rejects.toThrow(
      /reloaded after the flip/
    );
  });
});

describe('hiddenFails', () => {
  async function applied(page: ReturnType<typeof fakePage>) {
    await check(page);
    return hiddenFails(page, { expect: fail });
  }

  it('passes when a value hidden with CSS stops reading, and reads again once shown', async () => {
    await expect(applied(fakePage())).resolves.toBeUndefined();
  });

  for (const { name } of HIDINGS) {
    it(`fails a reading that counts a value under ${name} (negative control)`, async () => {
      await expect(applied(fakePage({ misses: [name] }))).rejects.toThrow(
        `control: the Work row still reads "done" under ${name} (span): ` +
          'the check counts text the place does not draw'
      );
    });
  }

  it('hides each way a page can leave a value in the DOM and off its place', () => {
    const names = HIDINGS.map((h) => h.name);
    for (const name of [
      'display: none',
      'visibility: hidden',
      'opacity: 0',
      'font-size: 0',
      'color: transparent',
      'an ancestor clip',
      'only a sibling'
    ]) {
      expect(names, name).toContain(name);
    }
    expect(HIDINGS.find((h) => h.name === 'an ancestor clip')).toMatchObject({ on: 'ancestor' });
    expect(HIDINGS.find((h) => h.name === 'only a sibling')).toMatchObject({ twin: true });
  });
});

describe('the visits', () => {
  it('read all three places at each width', () => {
    const places = Object.keys(READS).sort();
    for (const visits of [VISITS.wide, VISITS.phone]) {
      expect([...new Set(visits.flatMap((v) => v.places))].sort()).toEqual(places);
    }
    expect(REREAD_MS).toBeGreaterThan(2 * PULSE_MS);
  });
});
