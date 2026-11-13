/**
 * The hidden-tab check's own test (#2851): it fails when it should, without a
 * browser. A page here is a clock and its polls — each asks for its path every
 * `every` ms while visible, and the check's `sleep` is what moves the clock —
 * so a page whose poll is gone, one that keeps asking while hidden, one that
 * asks the moment it is hidden, and one that never comes back are each a line,
 * and each must fail the check.
 */
import { mkdirSync, mkdtempSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, relative, sep } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { afterEach, describe, expect, it } from 'vitest';
import {
  EPIC_OFFLINE,
  HEALTH,
  HEALTH_HIDDEN_MS,
  HIDDEN_MS,
  POLLS,
  ROUTES_DIR,
  epicComesBack,
  factoryRoutes,
  healthRound,
  hiddenTabRound,
  inPool,
  recordRequests
} from './hidden-tab.mjs';

type Poll = { what: string; poll: RegExp; every: number };
type Request = { url: () => string; method: () => string };

/**
 * One poll of a fake tab. `path` is what it requests; `whileHidden` keeps it
 * asking in the background; `comesBack` false leaves it stopped once the tab
 * is shown again.
 */
type Ask = { path: string; every: number; whileHidden?: boolean; comesBack?: boolean };

/**
 * How an offline page treats its reconnect read once shown, when the server
 * answers it: `takesIt` false drops the answer and stays offline; `disarms`
 * false comes back but keeps asking.
 */
type Reconnect = { path: string; takesIt?: boolean; disarms?: boolean };

/**
 * A tab with these polls. `onHide` is a path the hide event itself asks for;
 * `reconnect` makes it an offline page that a shown read can bring back. A
 * page probe (`evaluate` with no visibility to set) reads whether it is back.
 */
function fakePage(
  asks: Ask[],
  { onHide, reconnect }: { onHide?: string; reconnect?: Reconnect } = {}
) {
  const listeners: ((request: Request) => void)[] = [];
  let hidden = false;
  let wasHidden = false;
  let back = false;
  const since = asks.map(() => 0);
  const request = (path: string) => {
    for (const listener of listeners) {
      listener({ url: () => `http://127.0.0.1${path}`, method: () => 'GET' });
    }
    if (reconnect?.path === path && wasHidden && !hidden && reconnect.takesIt !== false) back = true;
  };
  const disarmed = (path: string) =>
    back && reconnect?.path === path && reconnect.disarms !== false;
  const page = {
    request,
    on(event: string, listener: (request: Request) => void) {
      if (event === 'request') listeners.push(listener);
    },
    async evaluate(_fn: unknown, next: unknown) {
      if (typeof next !== 'boolean') return back;
      hidden = next;
      wasHidden ||= next;
      if (next && onHide) request(onHide);
    },
    async sleep(ms: number) {
      asks.forEach((ask, i) => {
        since[i] += ms;
        while (since[i] >= ask.every) {
          since[i] -= ask.every;
          const stopped = hidden
            ? !ask.whileHidden
            : (wasHidden && ask.comesBack === false) || disarmed(ask.path);
          if (!stopped) request(ask.path);
        }
      });
    }
  };
  return page;
}

function fail(ok: boolean, message: string) {
  if (!ok) throw new Error(message);
}

type After = (
  page: unknown,
  log: unknown,
  opts: { sleep: (ms: number) => Promise<void>; expect: typeof fail }
) => Promise<void>;

function round(page: ReturnType<typeof fakePage>, polls: Poll[], after?: After) {
  return hiddenTabRound(page, polls, {
    sleep: (ms: number) => page.sleep(ms),
    expect: fail,
    after
  });
}

/** A path the poll matches: the pattern with its anchors taken off. */
function pathOf(poll: RegExp): string {
  return poll.source.replace(/^\^/, '').replace(/\$$/, '').replace(/\\\//g, '/');
}

/** A page that answers every one of `polls` on its own interval. */
const asking = (polls: Poll[]): Ask[] => polls.map(({ poll, every }) => ({ path: pathOf(poll), every }));

describe('hiddenTabRound', () => {
  const epics = { what: 'the roster read', poll: /^\/api\/epics$/, every: 5_000 };
  const pulse = { what: 'the shell pulse', poll: /^\/api\/dashboard$/, every: 7_000 };

  it('passes a page whose polls run while visible, stop while hidden, and run again', async () => {
    const page = fakePage(asking([pulse, epics]));
    await expect(round(page, [pulse, epics])).resolves.toBeUndefined();
  });

  it('fails a page whose poll is gone: zero requests while hidden proves nothing', async () => {
    await expect(round(fakePage([]), [epics])).rejects.toThrow(/while visible: the poll never ran/);
  });

  it('fails a page that keeps the pulse but drops the route’s own poll', async () => {
    await expect(round(fakePage(asking([pulse])), [pulse, epics])).rejects.toThrow(
      /no the roster read .* while visible: the poll never ran/
    );
  });

  it('fails a page that polls something else, not the route’s poll', async () => {
    await expect(round(fakePage([{ path: '/api/loops', every: 5_000 }]), [epics])).rejects.toThrow(
      /the poll never ran/
    );
  });

  it('fails a page that keeps asking while hidden', async () => {
    const page = fakePage([{ path: '/api/epics', every: 5_000, whileHidden: true }]);
    await expect(round(page, [epics])).rejects.toThrow(
      new RegExp(`${HIDDEN_MS / 5_000} request\\(s\\) while hidden: /api/epics`)
    );
  });

  it('fails a page whose hide event itself asks the server', async () => {
    const page = fakePage(asking([epics]), { onHide: '/api/epics' });
    await expect(round(page, [epics])).rejects.toThrow(/1 request\(s\) while hidden: \/api\/epics/);
  });

  it('fails a page whose poll does not come back when the tab is shown', async () => {
    const page = fakePage([{ path: '/api/epics', every: 5_000, comesBack: false }]);
    await expect(round(page, [epics])).rejects.toThrow(/after the tab came back/);
  });

  it('fails a page where one of two polls does not come back', async () => {
    const page = fakePage([
      { path: '/api/dashboard', every: 7_000 },
      { path: '/api/epics', every: 5_000, comesBack: false }
    ]);
    await expect(round(page, [pulse, epics])).rejects.toThrow(
      /no the roster read .* after the tab came back/
    );
  });
});

describe('every poll a route names, removed, fails the check (negative control)', () => {
  for (const [route, visits] of Object.entries(POLLS)) {
    for (const { visit, polls } of visits) {
      it(`${route} at ${visit}: passes with all its polls, fails without any one`, async () => {
        for (const { poll } of polls) expect(poll.test(pathOf(poll))).toBe(true);
        await expect(round(fakePage(asking(polls)), polls)).resolves.toBeUndefined();
        for (const dropped of polls) {
          const page = fakePage(asking(polls.filter((poll) => poll !== dropped)));
          await expect(round(page, polls)).rejects.toThrow(/the poll never ran/);
        }
      });
    }
  }

  it('a route with a poll of its own names it next to the pulse', () => {
    for (const route of ['/epics', '/epics/[ref]', '/loops', '/monitor', '/runs/[id]', '/work']) {
      for (const { polls } of POLLS[route as keyof typeof POLLS]) {
        expect(polls.length, `${route} names only the pulse`).toBeGreaterThan(1);
      }
    }
  });
});

describe('the reads a page arms off another reading are named too', () => {
  const named = (route: keyof typeof POLLS, path: string) =>
    POLLS[route].filter((spec) => spec.polls.some(({ poll }) => poll.test(path)));

  /** A page that asks for every poll the visit names except `path`. */
  async function withoutIt(route: keyof typeof POLLS, path: string) {
    const [spec] = named(route, path);
    const kept = spec.polls.filter(({ poll }) => !poll.test(path));
    await expect(round(fakePage(asking(kept)), spec.polls)).rejects.toThrow(/the poll never ran/);
  }

  it('an offline epic page fails without its reconnect read, and one that asks while hidden fails', async () => {
    const [spec] = named('/epics/[ref]', '/api/epics/launch-fixes');
    // The fixture server always answers, so the visit must serve the failure.
    expect(spec?.prepare).toBeTypeOf('function');
    await withoutIt('/epics/[ref]', '/api/epics/launch-fixes');
    const unguarded = asking(spec.polls).map((ask) =>
      ask.path === '/api/epics/launch-fixes' ? { ...ask, whileHidden: true } : ask
    );
    await expect(round(fakePage(unguarded), spec.polls)).rejects.toThrow(
      /while hidden: \/api\/epics\/launch-fixes/
    );
  });

  it('an offline epic page must come back once its reconnect read is answered', async () => {
    const [spec] = named('/epics/[ref]', '/api/epics/launch-fixes');
    expect(spec.after).toBe(epicComesBack);
    const path = '/api/epics/launch-fixes';
    const offline = (reconnect: Omit<Reconnect, 'path'>) =>
      round(fakePage(asking(spec.polls), { reconnect: { path, ...reconnect } }), spec.polls, spec.after);
    await expect(offline({})).resolves.toBeUndefined();
    // The retry succeeds, the page drops the answer: every request the round
    // expects is made, and the page still says it is offline.
    await expect(offline({ takesIt: false })).rejects.toThrow(
      new RegExp(`still says "${EPIC_OFFLINE}"`)
    );
    await expect(offline({ disarms: false })).rejects.toThrow(
      /reconnect read\(s\) after the epic came back: the retry never disarmed/
    );
  });

  it('a Work page that stops re-reading on the pulse fails', async () => {
    for (const path of [
      '/api/dashboard/work',
      '/api/work/SFD-229/pipeline',
      '/api/work/SFD-229/evidence'
    ]) {
      expect(named('/work', path), path).toHaveLength(1);
      await withoutIt('/work', path);
    }
  });
});

describe('the health round', () => {
  /** The shell's first health read lands at navigation, before the beat. */
  function healthPage(ask: Partial<Ask> = {}) {
    const page = fakePage([{ path: '/api/health', every: HEALTH.every, ...ask }]);
    const log = recordRequests(page);
    page.request('/api/health');
    return { page, log };
  }
  const run = ({ page, log }: ReturnType<typeof healthPage>) =>
    healthRound(page, log, { sleep: (ms: number) => page.sleep(ms), expect: fail });

  it('stays hidden past the 60s beat', () => {
    expect(HEALTH.every).toBe(60_000);
    expect(HEALTH_HIDDEN_MS).toBeGreaterThan(HEALTH.every);
  });

  it('passes a health beat that waits while hidden and runs again once shown', async () => {
    await expect(run(healthPage())).resolves.toBeUndefined();
  });

  it('fails a health beat that ignores the hidden tab, which a 20s round never sees', async () => {
    await expect(
      round(fakePage([{ path: '/api/health', every: HEALTH.every, whileHidden: true }]), [])
    ).resolves.toBeUndefined();
    await expect(run(healthPage({ whileHidden: true }))).rejects.toThrow(/while hidden: \/api\/health/);
  });

  it('fails a health beat whose timer is gone: the first read alone is not the beat', async () => {
    await expect(run(healthPage({ comesBack: false }))).rejects.toThrow(/after the tab came back/);
  });
});

describe('inPool', () => {
  it('runs every task, never more than the pool at once', async () => {
    let running = 0;
    let most = 0;
    const done: number[] = [];
    const tasks = Array.from({ length: 9 }, (_, i) => async () => {
      running += 1;
      most = Math.max(most, running);
      await new Promise((resolve) => setTimeout(resolve, 1));
      running -= 1;
      done.push(i);
    });
    await inPool(tasks, 4);
    expect(done.sort((a, b) => a - b)).toEqual([0, 1, 2, 3, 4, 5, 6, 7, 8]);
    expect(most).toBe(4);
  });
});

const temps: string[] = [];
afterEach(() => {
  for (const dir of temps.splice(0)) rmSync(dir, { recursive: true, force: true });
});

/** Every `+page.svelte` under the folder, by a walk of its own. */
function pagesOnDisk(dir: string): string[] {
  return (readdirSync(dir, { recursive: true }) as string[])
    .filter((file) => file.split(sep).at(-1) === '+page.svelte')
    .map((file) => relative(dir, join(dir, file, '..')).split(sep).filter(Boolean).join('/'))
    .map((folder) => `/${folder}`)
    .sort();
}

describe('the route list is the routes folder', () => {
  it('names one route per +page.svelte under src/routes/(factory)', () => {
    const onDisk = pagesOnDisk(fileURLToPath(ROUTES_DIR));
    expect(onDisk).toContain('/epics');
    expect(onDisk).toContain('/scorecard');
    expect(factoryRoutes()).toEqual(onDisk);
  });

  it('the poll table names exactly those routes', () => {
    expect(Object.keys(POLLS).sort()).toEqual(factoryRoutes());
  });

  it('picks up a page added to the folder, and a (group) adds no segment', () => {
    const dir = mkdtempSync(join(tmpdir(), 'factory-routes-'));
    temps.push(dir);
    for (const folder of ['work', 'epics/[ref]', '(inner)/later', 'lib-only']) {
      mkdirSync(join(dir, folder), { recursive: true });
    }
    for (const page of ['work', 'epics/[ref]', '(inner)/later']) {
      writeFileSync(join(dir, page, '+page.svelte'), '');
    }
    writeFileSync(join(dir, 'lib-only', 'reads.ts'), '');
    expect(factoryRoutes(pathToFileURL(`${dir}/`))).toEqual(['/epics/[ref]', '/later', '/work']);
  });
});
