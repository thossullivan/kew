/**
 * #2741 — the launchpad's per-dispatch model, in a DOM.
 *
 * The control is a listbox of the catalog limited to the runner the plan
 * resolved; a pick reaches the Run request as `model` and the copied command
 * as `--model`; an untouched control sends no `model` key at all.
 */
import { flushSync, mount, unmount } from 'svelte';
import { createClassComponent } from 'svelte/legacy';
import { afterEach, describe, expect, it, vi } from 'vitest';
import planJson from '../../../fixtures/factory/dispatch-resolve.json';
import modelsJson from '../../../fixtures/factory/models.json';
import type { CatalogModel, DispatchPlan } from '$lib/api';

const { default: DispatchLaunchpad } = await import('./DispatchLaunchpad.svelte');

const plan = (over: Partial<DispatchPlan> = {}): DispatchPlan => ({
  ...(planJson as DispatchPlan),
  ...over
});
const catalog = (modelsJson as { models: CatalogModel[] }).models;

let mounted: Array<Record<string, unknown>> = [];
let selectable: Array<{ $destroy: () => void }> = [];

afterEach(() => {
  for (const app of mounted) unmount(app);
  for (const app of selectable) app.$destroy();
  mounted = [];
  selectable = [];
  document.body.innerHTML = '';
  vi.unstubAllGlobals();
});

/** Every POST the page makes to `/api/run-requests`, by body. */
function stubRunRequests(): Array<Record<string, unknown>> {
  const posted: Array<Record<string, unknown>> = [];
  const request = {
    id: 'r1',
    command: 'kew dispatch 2302',
    operator: 'tom',
    requested_at: '2026-10-11T00:00:00+00:00',
    source: 'web',
    state: 'queued',
    item_id: '2302',
    drive: null,
    host: null,
    pid: null,
    log: null,
    started_at: null,
    finished_at: null,
    exit_code: null,
    detail: null
  };
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === 'POST') {
        const body = JSON.parse(String(init.body)) as Record<string, unknown>;
        posted.push(body);
        return Response.json({ request: { ...request, model: body.model ?? null }, created: true });
      }
      if (url.startsWith('/api/run-requests?')) return Response.json({ requests: [] });
      return Response.json(request);
    })
  );
  return posted;
}

/** The request a held POST answers with, once it is let go. */
const heldRequest = {
  id: 'held',
  command: 'kew dispatch 2302',
  operator: 'tom',
  requested_at: '2026-10-11T00:00:00+00:00',
  source: 'web',
  state: 'queued',
  item_id: '2302',
  drive: null,
  host: null,
  pid: null,
  log: null,
  started_at: null,
  finished_at: null,
  exit_code: null,
  detail: null,
  model: null
};

async function render(
  over: Partial<DispatchPlan> = {},
  models: CatalogModel[] = catalog,
  shown = 'codex / GPT-5.6 Sol'
) {
  const target = document.createElement('div');
  document.body.append(target);
  const app = mount(DispatchLaunchpad, {
    target,
    props: {
      itemId: '2302',
      read: (async () => plan(over)) as never,
      readModels: async () => models
    }
  });
  mounted.push(app);
  flushSync();
  await vi.waitFor(() => expect(target.textContent).toContain(shown));
  return target;
}

function press(target: HTMLElement, label: string): void {
  const button = [...target.querySelectorAll('button')].find((b) =>
    (b.textContent ?? '').includes(label)
  );
  if (button === undefined) throw new Error(`no "${label}" button on screen`);
  button.click();
  flushSync();
}

/** Run takes a press only once its ledger read for the pair on screen has
 * landed (#2975): a new pick starts a new read. */
async function runReady(target: HTMLElement): Promise<void> {
  await vi.waitFor(() =>
    expect((target.querySelector('button[data-run-button]') as HTMLButtonElement).disabled).toBe(
      false
    )
  );
}

/** Every write the page makes to the clipboard. */
function stubClipboard(): string[] {
  const written: string[] = [];
  vi.stubGlobal('navigator', {
    ...navigator,
    clipboard: { writeText: async (text: string) => void written.push(text) }
  });
  return written;
}

/** Both Copy surfaces: the summary's, and the open drawer's. */
const copies = (target: HTMLElement) =>
  [...target.querySelectorAll('button')].filter(
    (b) => b.getAttribute('aria-label') === 'Copy command'
  ) as HTMLButtonElement[];

const picker = (target: HTMLElement) =>
  target.querySelector('section[aria-label="Model for this dispatch"]') as HTMLElement;

async function openModels(target: HTMLElement): Promise<HTMLElement> {
  press(target, 'Details');
  await vi.waitFor(() => expect(picker(target)?.querySelector('[role="listbox"]')).toBeTruthy());
  return picker(target);
}

function pick(section: HTMLElement, id: string): void {
  const option = section.querySelector(`[role="option"][data-model-id="${id}"]`) as HTMLElement;
  option.click();
  flushSync();
}

describe('the launchpad model control (#2741)', () => {
  it("is a listbox of the resolved runner's routable models only", async () => {
    stubRunRequests();
    const section = await openModels(await render());
    const ids = [...section.querySelectorAll('[role="option"]')].map((o) =>
      o.getAttribute('data-model-id')
    );
    expect(ids).toEqual(['gpt-5.6-luna', 'gpt-5.6-sol']);
    // Untouched: nothing selected, and it says no override is sent.
    expect(section.querySelector('[aria-selected="true"]')).toBeNull();
    expect(section.textContent).toContain('no override is sent');
  });

  it('marks only the model that runs, and moves the mark to a pick (#2895)', async () => {
    stubRunRequests();
    const section = await openModels(await render());
    const marked = () =>
      [...section.querySelectorAll('[role="option"]')]
        .filter((o) => o.hasAttribute('data-current') || o.classList.contains('bg-white/5'))
        .map((o) => [o.getAttribute('data-model-id'), o.textContent?.includes('✓ runs now')]);
    // Untouched, the route's model (MODEL: gpt-5.6-sol) runs — not the first row.
    expect(marked()).toEqual([['gpt-5.6-sol', true]]);
    pick(section, 'gpt-5.6-luna');
    expect(marked()).toEqual([['gpt-5.6-luna', true]]);
    press(section, "Use the route's model");
    expect(marked()).toEqual([['gpt-5.6-sol', true]]);
  });

  it('keeps the shade on the model that runs while the arrows move (#2895)', async () => {
    stubRunRequests();
    const section = await openModels(await render());
    const listbox = section.querySelector('[role="listbox"]') as HTMLElement;
    const shaded = () =>
      [...section.querySelectorAll('[role="option"].bg-white\\/5')].map((o) =>
        o.getAttribute('data-model-id')
      );
    const cursor = () =>
      document.getElementById(listbox.getAttribute('aria-activedescendant') ?? '')?.dataset
        .modelId;
    const key = (k: string) => {
      listbox.dispatchEvent(new KeyboardEvent('keydown', { key: k, bubbles: true }));
      flushSync();
    };
    expect(cursor()).toBe('gpt-5.6-sol');
    key('ArrowUp');
    expect(cursor()).toBe('gpt-5.6-luna');
    expect(shaded()).toEqual(['gpt-5.6-sol']);
    expect(section.querySelectorAll('[data-active]')).toHaveLength(1);
    expect(section.querySelector('[data-active]')?.getAttribute('data-model-id')).toBe(
      'gpt-5.6-luna'
    );
    key('ArrowDown');
    expect(cursor()).toBe('gpt-5.6-sol');
    expect(shaded()).toEqual(['gpt-5.6-sol']);
    expect(section.querySelector('[aria-selected="true"]')).toBeNull();
  });

  it('marks a route model the catalog does not route, and sends no override for it (#2895)', async () => {
    const posted = stubRunRequests();
    const route = {
      runner: { value: 'claude-code', source: 'kew.toml [routing].simple' },
      model: { value: 'claude-opus-4-7', source: 'kew.toml [routing].simple' }
    } as Partial<DispatchPlan>;
    const target = await render(route, catalog, 'claude-code / Opus 4.7');
    const section = await openModels(target);
    const row = section.querySelector('[data-model-id="claude-opus-4-7"]') as HTMLElement;
    expect(row.hasAttribute('data-current')).toBe(true);
    expect(row.textContent).toContain('✓ runs now');
    expect(row.classList.contains('bg-white/5')).toBe(true);
    expect(section.querySelectorAll('[data-current]')).toHaveLength(1);
    // Picking it is the untouched route: Run sends no model.
    pick(section, 'claude-opus-4-7');
    expect(section.textContent).toContain('no override is sent');
    await runReady(target);
    press(target, 'Run');
    await vi.waitFor(() => expect(posted).toHaveLength(1));
    expect(posted[0]).not.toHaveProperty('model');
  });

  it('marks the catalog row a MODEL alias names (#2895)', async () => {
    stubRunRequests();
    const aliased = catalog.map((m) =>
      m.id === 'gpt-5.6-sol' ? { ...m, aliases: ['sol-latest'] } : m
    );
    const route = {
      model: { value: 'sol-latest', source: 'kew.toml [routing].simple' }
    } as Partial<DispatchPlan>;
    const section = await openModels(await render(route, aliased, 'codex / sol-latest'));
    const marked = [...section.querySelectorAll('[data-current]')].map((o) =>
      o.getAttribute('data-model-id')
    );
    expect(marked).toEqual(['gpt-5.6-sol']);
  });

  it('sends the pick with Run and carries it in the copied command', async () => {
    const posted = stubRunRequests();
    const target = await render();
    const section = await openModels(target);
    pick(section, 'gpt-5.6-luna');

    expect(target.querySelector('code')?.textContent).toBe(
      'kew dispatch 2302 --model gpt-5.6-luna'
    );
    expect(target.querySelector('[data-plan-summary]')?.textContent).toContain(
      'codex / GPT-5.6 Luna (override)'
    );
    press(target, 'Close');
    await runReady(target);
    press(target, 'Run');
    await vi.waitFor(() => expect(posted).toHaveLength(1));
    expect(posted[0]).toEqual({
      command: 'kew dispatch 2302',
      item_id: '2302',
      model: 'gpt-5.6-luna',
      key: expect.stringMatching(/^[0-9a-f]{32}$/)
    });
  });

  it('sends no model key when the control is left untouched', async () => {
    const posted = stubRunRequests();
    const target = await render();
    await openModels(target);
    press(target, 'Close');
    await runReady(target);
    press(target, 'Run');
    await vi.waitFor(() => expect(posted).toHaveLength(1));
    expect(posted[0]).toEqual({
      command: 'kew dispatch 2302',
      item_id: '2302',
      key: expect.stringMatching(/^[0-9a-f]{32}$/)
    });
  });

  it("presses the one Run from the drawer, with the pick, and closes the drawer (#3055)", async () => {
    const posted = stubRunRequests();
    const target = await render();
    const section = await openModels(target);
    pick(section, 'gpt-5.6-luna');
    const drawerRun = () => target.querySelector('button[data-drawer-run]') as HTMLButtonElement;
    await vi.waitFor(() => expect(drawerRun().disabled).toBe(false));
    expect(drawerRun().textContent?.trim()).toBe('Run');
    expect(drawerRun().hasAttribute('aria-describedby')).toBe(false);
    expect(target.querySelector('[data-run-off]')).toBeNull();
    drawerRun().click();
    flushSync();
    await vi.waitFor(() => expect(posted).toHaveLength(1));
    expect(posted[0]).toMatchObject({ command: 'kew dispatch 2302', model: 'gpt-5.6-luna' });
    expect(target.querySelector('[role="dialog"]')).toBeNull();
    // One Run: the closed line's follows the request the drawer's sent.
    await vi.waitFor(() =>
      expect(target.querySelector('button[data-run-button]')?.textContent).toBe('Queued…')
    );
  });

  it('drops the pick on "Use the route\'s model"', async () => {
    stubRunRequests();
    const target = await render();
    const section = await openModels(target);
    pick(section, 'gpt-5.6-luna');
    press(target, "Use the route's model");
    expect(target.querySelector('code')?.textContent).toBe('kew dispatch 2302');
  });

  it('holds the lane, the tier and the model while Run is sending (#2975)', async () => {
    // The POST is held: a lane change now would re-resolve Run away from a
    // request the server can still commit.
    let answer: (r: Response) => void = () => {};
    const reads: string[] = [];
    const posted: unknown[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string, init?: RequestInit) => {
        if (init?.method === 'POST') {
          posted.push(JSON.parse(String(init.body)));
          return new Promise<Response>((resolve) => (answer = resolve));
        }
        if (url.startsWith('/api/run-requests?')) return Response.json({ requests: [] });
        return Response.json(heldRequest);
      })
    );
    const target = document.createElement('div');
    document.body.append(target);
    mounted.push(
      mount(DispatchLaunchpad, {
        target,
        props: {
          itemId: '2302',
          read: (async (q: { lane: string }) => {
            reads.push(q.lane);
            return plan();
          }) as never,
          readModels: async () => catalog
        }
      })
    );
    flushSync();
    await vi.waitFor(() => expect(target.textContent).toContain('codex / GPT-5.6 Sol'));

    press(target, 'Run');
    const section = await openModels(target);
    const note = () => section.querySelector('[data-model-note]')?.textContent ?? '';
    expect(note()).toContain('held while Run is sending');
    const lane = target.querySelector('#plan-lane') as HTMLSelectElement;
    expect(lane.disabled).toBe(true);
    expect((target.querySelector('#plan-complexity') as HTMLSelectElement).disabled).toBe(true);

    // A change event that reaches the select anyway re-reads nothing.
    lane.value = 'remediation';
    lane.dispatchEvent(new Event('change', { bubbles: true }));
    flushSync();
    expect(reads).toEqual(['implement']);

    // The answer lands: the button shows the real request, the hold lifts.
    answer(Response.json({ request: heldRequest, created: true }));
    await vi.waitFor(() =>
      expect(target.querySelector('button[data-run-button]')?.textContent).toBe('Queued…')
    );
    expect(lane.disabled).toBe(false);
    expect(note()).not.toContain('held while Run is sending');
    expect(posted).toHaveLength(1);
  });

  it('holds Run while a route change is resolving, so a stale route is never sent (#2975)', async () => {
    // A lane change takes a plan read; until it lands, the Run on screen is the
    // old route's, and a POST of it would be hidden when the new plan arrives.
    const posted = stubRunRequests();
    let release: () => void = () => {};
    const reads: string[] = [];
    const target = document.createElement('div');
    document.body.append(target);
    mounted.push(
      mount(DispatchLaunchpad, {
        target,
        props: {
          itemId: '2302',
          read: (async (q: { lane: string }) => {
            reads.push(q.lane);
            if (reads.length === 1) return plan();
            await new Promise<void>((resolve) => (release = resolve));
            return plan({ lane: { ...plan().lane, value: q.lane } });
          }) as never,
          readModels: async () => catalog
        }
      })
    );
    flushSync();
    await vi.waitFor(() => expect(target.textContent).toContain('codex / GPT-5.6 Sol'));
    const run = () => target.querySelector('button[data-run-button]') as HTMLButtonElement;
    expect(run().disabled).toBe(false);

    press(target, 'Details');
    const lane = target.querySelector('#plan-lane') as HTMLSelectElement;
    lane.value = 'remediation';
    lane.dispatchEvent(new Event('change', { bubbles: true }));
    flushSync();
    expect(reads).toEqual(['implement', 'remediation']);
    press(target, 'Close');

    expect(run().disabled).toBe(true);
    run().click();
    flushSync();
    await new Promise((r) => setTimeout(r, 5));
    expect(posted).toHaveLength(0);

    // The new route lands: it is the remediation lane's, which no dispatch
    // command launches, so the implement Run goes and nothing was sent.
    release();
    await vi.waitFor(() => expect(target.querySelector('button[data-run-button]')).toBeNull());
    expect(target.textContent).toContain('codex / GPT-5.6 Sol');
    expect(posted).toHaveLength(0);
  });

  it('keeps a stale Run unavailable when a changed lane fails to resolve (#2975)', async () => {
    // The review read fails: the implement plan in hand answers a lane the
    // operator moved off, and its Run would launch the wrong lane.
    const posted = stubRunRequests();
    const reads: string[] = [];
    const target = document.createElement('div');
    document.body.append(target);
    mounted.push(
      mount(DispatchLaunchpad, {
        target,
        props: {
          itemId: '2302',
          read: (async (q: { lane: string }) => {
            reads.push(q.lane);
            if (q.lane !== 'implement') throw new Error('serve is gone');
            return plan();
          }) as never,
          readModels: async () => catalog
        }
      })
    );
    flushSync();
    await vi.waitFor(() => expect(target.textContent).toContain('codex / GPT-5.6 Sol'));
    expect(target.querySelector('button[data-run-button]')).not.toBeNull();

    press(target, 'Details');
    const lane = target.querySelector('#plan-lane') as HTMLSelectElement;
    lane.value = 'review';
    lane.dispatchEvent(new Event('change', { bubbles: true }));
    flushSync();
    press(target, 'Close');
    await vi.waitFor(() =>
      expect(target.textContent).toContain('the route could not be resolved')
    );
    expect(reads).toEqual(['implement', 'review']);
    expect(target.querySelector('button[data-run-button]')).toBeNull();
    expect(posted).toHaveLength(0);
  });

  it('offers no Copy of the last lane while a changed lane is held or failed (#2975)', async () => {
    // The drawer stays open: both Copy surfaces are pressed while the review
    // read is held, then after it fails.
    const written: string[] = [];
    vi.stubGlobal('navigator', {
      ...navigator,
      clipboard: { writeText: async (text: string) => void written.push(text) }
    });
    // The ledger holds nothing of the implement command, so its Copy is free.
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => Response.json({ requests: [] }))
    );
    let fail: (e: Error) => void = () => {};
    const target = document.createElement('div');
    document.body.append(target);
    mounted.push(
      mount(DispatchLaunchpad, {
        target,
        props: {
          itemId: '2302',
          read: (async (q: { lane: string }) => {
            if (q.lane === 'implement') return plan();
            return new Promise((_resolve, reject) => (fail = reject));
          }) as never,
          readModels: async () => catalog
        }
      })
    );
    flushSync();
    await vi.waitFor(() => expect(target.textContent).toContain('codex / GPT-5.6 Sol'));
    const copies = () =>
      [...target.querySelectorAll('button')].filter(
        (b) => b.getAttribute('aria-label') === 'Copy command'
      ) as HTMLButtonElement[];

    press(target, 'Details');
    await vi.waitFor(() => expect(copies().every((b) => !b.disabled)).toBe(true));
    const lane = target.querySelector('#plan-lane') as HTMLSelectElement;
    lane.value = 'review';
    lane.dispatchEvent(new Event('change', { bubbles: true }));
    flushSync();

    expect(copies()).toHaveLength(2);
    expect(copies().every((b) => b.disabled)).toBe(true);
    for (const copy of copies()) copy.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    await new Promise((r) => setTimeout(r, 5));
    expect(written).toEqual([]);

    fail(new Error('serve is gone'));
    await vi.waitFor(() => expect(target.textContent).toContain('the route could not be resolved'));
    expect(copies()).toHaveLength(0);
    expect(target.querySelector('[data-command-stale]')?.textContent).toContain(
      'implement command is not'
    );
    expect(target.querySelector('code[data-command]')).toBeNull();
    expect(written).toEqual([]);
  });

  it("scopes a held Run's line to the item that sent it (#2975)", async () => {
    let answer: (r: Response) => void = () => {};
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string, init?: RequestInit) => {
        if (init?.method === 'POST') return new Promise<Response>((resolve) => (answer = resolve));
        if (url.startsWith('/api/run-requests?')) return Response.json({ requests: [] });
        return Response.json(heldRequest);
      })
    );
    // SFD-7 is a tracker item: its plan has no issue number, so no Run mounts
    // there to replace the line.
    const reads: string[] = [];
    const read = async (q: { item: string }) => {
      reads.push(q.item);
      return q.item === '2302' ? plan() : plan({ item: q.item, issue_num: null });
    };
    const target = document.createElement('div');
    document.body.append(target);
    const app = createClassComponent({
      component: DispatchLaunchpad as never,
      target,
      props: { itemId: '2302', read: read as never, readModels: async () => catalog }
    });
    selectable.push(app);
    flushSync();
    await vi.waitFor(() => expect(target.textContent).toContain('codex / GPT-5.6 Sol'));
    press(target, 'Run');
    const line = () => target.querySelector('[data-run-line]')?.textContent?.trim() ?? null;
    expect(line()).toBe('sending to the drive…');

    app.$set({ itemId: 'SFD-7' });
    flushSync();
    await vi.waitFor(() => expect(target.textContent).toContain('codex / GPT-5.6 Sol'));
    expect(reads).toEqual(['2302', 'SFD-7']);
    expect(target.querySelector('button[data-run-button]')).toBeNull();
    expect(line()).toBeNull();

    // The answer lands while the operator is on SFD-7: it stays off that
    // panel, and is shown when 2302 is back.
    answer(Response.json({ request: heldRequest, created: true }));
    await new Promise((r) => setTimeout(r, 5));
    expect(line()).toBeNull();
    app.$set({ itemId: '2302' });
    flushSync();
    await vi.waitFor(() =>
      expect(target.querySelector('button[data-run-button]')?.textContent).toBe('Queued…')
    );
    expect(line()).toContain('queued — the drive picks it up within seconds');
  });

  it('shows a held Run as Sending… in a launchpad mounted after the first went away (#2975)', async () => {
    let answer: (r: Response) => void = () => {};
    const posted: unknown[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string, init?: RequestInit) => {
        if (init?.method === 'POST') {
          posted.push(JSON.parse(String(init.body)));
          return new Promise<Response>((resolve) => (answer = resolve));
        }
        if (url.startsWith('/api/run-requests?')) return Response.json({ requests: [] });
        return Response.json(heldRequest);
      })
    );
    const first = await render();
    press(first, 'Run');
    expect(first.querySelector('button[data-run-button]')?.textContent).toBe('Sending…');

    // The operator navigates away: the panel and its button unmount mid-POST.
    unmount(mounted.pop() as Record<string, unknown>);
    const second = await render();
    const run = () => second.querySelector('button[data-run-button]') as HTMLButtonElement;
    expect(run().textContent).toBe('Sending…');
    expect(run().disabled).toBe(true);
    run().click();
    flushSync();
    expect(posted).toHaveLength(1);

    answer(Response.json({ request: heldRequest, created: true }));
    await vi.waitFor(() => expect(run().textContent).toBe('Queued…'));
    expect(second.textContent).toContain('queued — the drive picks it up within seconds');
    expect(posted).toHaveLength(1);
  });

  it('keeps Run shut in a launchpad remounted after its Run was accepted until the ledger read lands (#2975)', async () => {
    const posted: unknown[] = [];
    const lookups: Array<(r: Response) => void> = [];
    let holdLookups = false;
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string, init?: RequestInit) => {
        if (init?.method === 'POST') {
          posted.push(JSON.parse(String(init.body)));
          return Response.json({ request: heldRequest, created: true });
        }
        if (url.startsWith('/api/run-requests?')) {
          if (!holdLookups) return Response.json({ requests: [] });
          return new Promise<Response>((resolve) => lookups.push(resolve));
        }
        return Response.json(heldRequest);
      })
    );
    const first = await render();
    await runReady(first);
    press(first, 'Run');
    // Accepted while its button listened: the answer was delivered, and the
    // ledger, not the store, is the record from here.
    await vi.waitFor(() =>
      expect(first.querySelector('button[data-run-button]')?.textContent).toBe('Queued…')
    );

    unmount(mounted.pop() as Record<string, unknown>);
    holdLookups = true;
    const second = await render();
    const run = () => second.querySelector('button[data-run-button]') as HTMLButtonElement;
    await vi.waitFor(() => expect(lookups).toHaveLength(1));
    expect(run().disabled).toBe(true);
    run().click();
    flushSync();
    expect(posted).toHaveLength(1);

    lookups[0](Response.json({ requests: [heldRequest] }));
    await vi.waitFor(() => expect(run().textContent).toBe('Queued…'));
    expect(run().disabled).toBe(true);
    expect(posted).toHaveLength(1);
  });

  it('holds both Copies and the clipboard while Run is sending (#2975)', async () => {
    const written = stubClipboard();
    let answer: (r: Response) => void = () => {};
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string, init?: RequestInit) => {
        if (init?.method === 'POST') return new Promise<Response>((resolve) => (answer = resolve));
        if (url.startsWith('/api/run-requests?')) return Response.json({ requests: [] });
        return Response.json(heldRequest);
      })
    );
    const target = await render();
    await runReady(target);

    press(target, 'Run');
    press(target, 'Details');
    expect(copies(target)).toHaveLength(2);
    expect(copies(target).every((b) => b.disabled)).toBe(true);
    for (const copy of copies(target)) copy.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    await new Promise((r) => setTimeout(r, 5));
    expect(written).toEqual([]);

    // The drive settles the request: nothing of the pair is unsettled, so the
    // command may be handed over again.
    answer(Response.json({ request: { ...heldRequest, state: 'done', exit_code: 0 }, created: true }));
    await vi.waitFor(() => expect(copies(target).every((b) => !b.disabled)).toBe(true));
    copies(target)[0].click();
    await vi.waitFor(() => expect(written).toEqual(['kew dispatch 2302']));
  });

  it('holds Run and both Copies while the ledger lookup is out (#2975)', async () => {
    const written = stubClipboard();
    const posted: unknown[] = [];
    const lookups: Array<(r: Response) => void> = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string, init?: RequestInit) => {
        if (init?.method === 'POST') {
          posted.push(JSON.parse(String(init.body)));
          return Response.json({ request: heldRequest, created: true });
        }
        if (url.startsWith('/api/run-requests?'))
          return new Promise<Response>((resolve) => lookups.push(resolve));
        return Response.json(heldRequest);
      })
    );
    const target = await render();
    press(target, 'Details');
    const run = target.querySelector('button[data-run-button]') as HTMLButtonElement;

    // One lookup for the pair, shared by Run and the launchpad's Copies.
    await vi.waitFor(() => expect(lookups).toHaveLength(1));
    expect(run.disabled).toBe(true);
    expect(copies(target)).toHaveLength(2);
    expect(copies(target).every((b) => b.disabled)).toBe(true);
    run.click();
    for (const copy of copies(target)) copy.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    await new Promise((r) => setTimeout(r, 5));
    expect([posted, written]).toEqual([[], []]);

    lookups[0](Response.json({ requests: [] }));
    await vi.waitFor(() => expect(run.disabled).toBe(false));
    expect(copies(target).every((b) => !b.disabled)).toBe(true);
  });

  it('keeps Run and both Copies shut through a failed ledger lookup, and asks again (#2975)', async () => {
    const written = stubClipboard();
    const posted: unknown[] = [];
    const answers = [
      () => new Response('serve is restarting', { status: 503 }),
      () => Response.json({ requests: [] })
    ];
    let lookups = 0;
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string, init?: RequestInit) => {
        if (init?.method === 'POST') {
          posted.push(JSON.parse(String(init.body)));
          return Response.json({ request: heldRequest, created: true });
        }
        if (url.startsWith('/api/run-requests?')) return answers[Math.min(lookups++, 1)]();
        return Response.json(heldRequest);
      })
    );
    const target = await render();
    press(target, 'Details');
    const run = target.querySelector('button[data-run-button]') as HTMLButtonElement;

    // The failed lookup is not an empty ledger: nothing is offered meanwhile.
    await vi.waitFor(() => expect(lookups).toBe(1));
    await new Promise((r) => setTimeout(r, 20));
    expect(run.disabled).toBe(true);
    expect(copies(target).every((b) => b.disabled)).toBe(true);
    run.click();
    for (const copy of copies(target)) copy.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    await new Promise((r) => setTimeout(r, 5));
    expect([posted, written]).toEqual([[], []]);

    // Asked again on the follow's beat, the ledger answers empty.
    await vi.waitFor(() => expect(run.disabled).toBe(false), { timeout: 5000 });
    expect(lookups).toBe(2);
    expect(copies(target).every((b) => !b.disabled)).toBe(true);
  });
});
