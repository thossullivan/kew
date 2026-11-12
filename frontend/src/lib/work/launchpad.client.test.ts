/**
 * #2302, AC2 — the launchpad in a DOM: a complexity change re-reads, a moved
 * selection re-reads, and a stale answer is dropped.
 *
 * jsdom, because every one of those is about reads in flight and none is
 * visible from a payload or a pure reading. The stale one is the defect the
 * monotonic request id exists to prevent and the one a green pure suite cannot
 * catch: two reads out at once, the FIRST answering last. Without the guard the
 * screen settles on whatever the network delivered last, which is a route the
 * operator did not ask for presented as the route that would run.
 */
import { flushSync, mount, unmount } from 'svelte';
import { createClassComponent } from 'svelte/legacy';
import { afterEach, describe, expect, it, vi } from 'vitest';
import planJson from '../../../fixtures/factory/dispatch-resolve.json';
import type { DispatchPlan } from '$lib/api';
import { claimReserved, registerShortcuts, shortcutsSuspended } from '$lib/keys/registry';

/**
 * The committed fixture with one field swapped — the same base payload
 * `launchpad.test.ts` reads and the fixture API serves. Built here rather than
 * imported from that file: importing a `.test.ts` would run the whole pure
 * suite a second time, in jsdom.
 */
const plan = (over: Partial<DispatchPlan> = {}): DispatchPlan => ({
  ...(planJson as DispatchPlan),
  ...over
});

const { default: DispatchLaunchpad } = await import('./DispatchLaunchpad.svelte');

type Query = { item?: string | null; complexity?: string | null; lane?: string };

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

/** Mount the panel with a read under the test's control. */
function render(read: (q: Query) => Promise<DispatchPlan>) {
  const target = document.createElement('div');
  document.body.append(target);
  const app = mount(DispatchLaunchpad, {
    target,
    props: { itemId: '2302', read: read as never }
  });
  mounted.push(app);
  // The mount read is `onMount`'s, which runs on the first flush.
  flushSync();
  return target;
}

/**
 * The same mount, with a handle that moves the SELECTION — what the work page
 * does when the operator picks another row.
 *
 * `createClassComponent` rather than `mount`, because `mount`'s props object is
 * only reactive when it is a `$state` proxy and runes exist only inside a
 * compiled module; `$set` is the one way a plain `.ts` test can push a new prop
 * into a mounted component.
 */
function renderSelected(read: (q: Query) => Promise<DispatchPlan>, itemId: string) {
  const target = document.createElement('div');
  document.body.append(target);
  const app = createClassComponent({
    component: DispatchLaunchpad as never,
    target,
    props: { itemId, read: read as never }
  });
  selectable.push(app);
  flushSync();
  return {
    target,
    select(next: string): void {
      app.$set({ itemId: next });
      flushSync();
    }
  };
}

const text = (target: HTMLElement): string => target.textContent ?? '';

function press(target: HTMLElement, label: string): void {
  const button = [...target.querySelectorAll('button')].find((b) =>
    (b.textContent ?? '').includes(label)
  );
  if (button === undefined) throw new Error(`no "${label}" button on screen`);
  button.click();
  flushSync();
}

/**
 * Open the drawer (#2535). With `all`, once the plan has landed, unfold the set
 * and default rows too — the rows most of these tests read are the route's own,
 * which the opened plan folds away under "N more fields ▸".
 */
async function openPlan(target: HTMLElement, { all = false } = {}): Promise<void> {
  press(target, 'Details');
  if (!all) return;
  await vi.waitFor(() => expect(text(target)).toContain('more fields ▸'));
  press(target, 'more fields ▸');
}

function choose(target: HTMLElement, id: string, value: string): void {
  const select = target.querySelector(`#${id}`) as HTMLSelectElement;
  select.value = value;
  select.dispatchEvent(new Event('change', { bubbles: true }));
  flushSync();
}

/** The resolved runner/model each row prints, keyed by field. */
function rowFor(target: HTMLElement, field: string): string {
  const row = [...target.querySelectorAll('div')].find((div) =>
    (div.firstElementChild?.textContent ?? '').trim() === field
  );
  return (row?.textContent ?? '').replace(/\s+/g, ' ').trim();
}

describe('opening the panel', () => {
  it('reads once for the closed line, and opening the drawer reads nothing more', async () => {
    const read = vi.fn(async () => plan());
    const target = render(read);
    expect(read).toHaveBeenCalledTimes(1);
    await vi.waitFor(() => expect(text(target)).toContain('codex / GPT-5.6 Sol'));
    await openPlan(target);
    expect(read).toHaveBeenCalledTimes(1);
    await vi.waitFor(() => expect(text(target)).toContain('kew dispatch 2302'));
  });

  it('sends no complexity at all before one is chosen', async () => {
    const read = vi.fn(async (_query: Query) => plan());
    render(read);
    expect(read.mock.calls[0][0]).toEqual({
      item: '2302',
      complexity: null,
      lane: 'implement'
    });
  });
});

describe('changing complexity updates the rows', () => {
  it('re-reads with the chosen tier and repaints every row', async () => {
    const answers: Record<string, DispatchPlan> = {
      '': plan({
        complexity: { value: 'unknown', source: 'no complexity given', known: false },
        runner: { value: 'claude-code', source: 'kew.toml [routing].default' },
        model: { value: 'claude-opus-4-8', source: 'kew.toml [routing].default' }
      }),
      complex: plan({
        complexity: { value: 'complex', source: 'the caller', known: true },
        runner: { value: 'claude-code', source: 'kew.toml [routing].complex' },
        model: { value: 'claude-opus-4-8', source: 'kew.toml [routing].complex' }
      })
    };
    const read = vi.fn(async (q: Query) => answers[q.complexity ?? '']);
    const target = render(read);

    await openPlan(target, { all: true });
    await vi.waitFor(() => expect(rowFor(target, 'runner')).toContain('[routing].default'));
    // #2785 (D12): a tier nobody named reads as the default lane's.
    expect(rowFor(target, 'complexity')).toContain('default');

    choose(target, 'plan-complexity', 'complex');
    expect(read).toHaveBeenCalledTimes(2);
    expect(read.mock.calls[1][0]).toMatchObject({ complexity: 'complex' });
    await vi.waitFor(() => expect(rowFor(target, 'runner')).toContain('[routing].complex'));
    expect(rowFor(target, 'complexity')).toContain('complex');
  });

  it('re-reads when the lane changes too', async () => {
    const read = vi.fn(async (q: Query) =>
      plan({ lane: { value: q.lane ?? 'implement', source: 'the caller' } })
    );
    const target = render(read);
    await openPlan(target);
    choose(target, 'plan-lane', 'review');
    expect(read.mock.calls[1][0]).toMatchObject({ lane: 'review' });
    await vi.waitFor(() =>
      expect(text(target)).toContain("epic driver's review stage")
    );
    // A reviewer lane has no hand command, so none is printed.
    expect(text(target)).not.toContain('kew dispatch 2302');
  });
});

describe('the selection moving is a choice too', () => {
  it("re-reads for the new item instead of keeping the last one's plan", async () => {
    const read = vi.fn(async (q: Query) =>
      plan({ item: q.item ?? null, issue_num: Number(q.item) })
    );
    const { target, select } = renderSelected(read, '2302');

    await openPlan(target);
    await vi.waitFor(() => expect(text(target)).toContain('kew dispatch 2302'));

    select('2410');
    // The previous item's command and route are gone the moment the selection
    // moves — not left on screen under a header that now says 2410.
    expect(text(target)).not.toContain('kew dispatch 2302');
    expect(read).toHaveBeenCalledTimes(2);
    expect(read.mock.calls[1][0]).toMatchObject({ item: '2410' });
    await vi.waitFor(() => expect(text(target)).toContain('kew dispatch 2410'));
    expect(text(target)).toContain('item 2410');
  });

  it('drops the answer to a read the operator has already moved past', async () => {
    const pending: Array<{ q: Query; settle: (p: DispatchPlan) => void }> = [];
    const read = (q: Query) =>
      new Promise<DispatchPlan>((resolve) => pending.push({ q, settle: resolve }));
    const { target, select } = renderSelected(read, '2302');

    await openPlan(target);
    select('2410');
    expect(pending).toHaveLength(2);

    // 2302's read lands after the operator has left it. It is not this item's
    // route, so it is not painted.
    pending[0].settle(plan({ item: '2302', issue_num: 2302 }));
    await Promise.resolve();
    flushSync();
    expect(text(target)).not.toContain('kew dispatch 2302');

    pending[1].settle(plan({ item: '2410', issue_num: 2410 }));
    await vi.waitFor(() => expect(text(target)).toContain('kew dispatch 2410'));
  });

  it('re-reads for the new item with the drawer closed — the line is about it', async () => {
    const read = vi.fn(async (q: Query) =>
      plan({
        item: q.item ?? null,
        issue_num: Number(q.item),
        runner: { value: q.item === '2410' ? 'claude-code' : 'codex', source: 's' }
      })
    );
    const { target, select } = renderSelected(read, '2302');
    await vi.waitFor(() => expect(text(target)).toContain('codex / GPT-5.6 Sol'));
    select('2410');
    // The last item's route is gone the moment the selection moves.
    expect(text(target)).not.toContain('codex / GPT-5.6 Sol');
    expect(read).toHaveBeenCalledTimes(2);
    expect(read.mock.calls[1][0]).toMatchObject({ item: '2410' });
    await vi.waitFor(() => expect(text(target)).toContain('claude-code / GPT-5.6 Sol'));
    expect(document.querySelector('[role="dialog"]')).toBeNull();
  });
  it("resets the tier on a move, so the untouched option names the new item's", async () => {
    // #2895 (C5): a tier picked on one item is not the next item's choice.
    const tiers: Record<string, string> = { '2060': 'medium', '2410': 'complex' };
    const read = vi.fn(async (q: Query) =>
      plan({
        item: q.item ?? null,
        issue_num: Number(q.item),
        complexity: q.complexity
          ? { value: q.complexity, source: 'the caller', known: true }
          : { value: tiers[q.item ?? ''], source: "the ready queue's label", known: true }
      })
    );
    const { target, select } = renderSelected(read, '2060');
    await openPlan(target);
    const tier = () => target.querySelector('#plan-complexity') as HTMLSelectElement;
    await vi.waitFor(() => expect(tier().selectedOptions[0]?.textContent).toBe('from item: medium'));
    choose(target, 'plan-complexity', 'complex');
    await vi.waitFor(() => expect(read).toHaveBeenCalledTimes(2));
    expect(tier().value).toBe('complex');

    select('2410');
    expect(read.mock.calls.at(-1)?.[0]).toMatchObject({ item: '2410', complexity: null });
    await vi.waitFor(() => expect(tier().selectedOptions[0]?.textContent).toBe('from item: complex'));
    expect(tier().value).toBe('');
  });
});

describe('a stale response is dropped', () => {
  it('keeps the newest answer when an older read lands after it', async () => {
    const pending: Array<{ q: Query; settle: (p: DispatchPlan) => void }> = [];
    const read = (q: Query) =>
      new Promise<DispatchPlan>((resolve) => pending.push({ q, settle: resolve }));
    const target = render(read);

    await openPlan(target);
    choose(target, 'plan-complexity', 'simple');
    choose(target, 'plan-complexity', 'complex');
    expect(pending).toHaveLength(3);

    // The newest read answers first…
    pending[2].settle(
      plan({
        complexity: { value: 'complex', source: 'the caller', known: true },
        runner: { value: 'claude-code', source: 'kew.toml [routing].complex' }
      })
    );
    await vi.waitFor(() => expect(text(target)).toContain('more fields ▸'));
    press(target, 'more fields ▸');
    await vi.waitFor(() => expect(rowFor(target, 'runner')).toContain('[routing].complex'));

    // …and then the two it superseded arrive. Neither may be believed.
    pending[1].settle(
      plan({
        complexity: { value: 'simple', source: 'the caller', known: true },
        runner: { value: 'codex', source: 'kew.toml [routing].simple' }
      })
    );
    pending[0].settle(
      plan({
        complexity: { value: 'unknown', source: 'no complexity given', known: false },
        runner: { value: 'opencode', source: 'kew.toml [routing].default' }
      })
    );
    await Promise.resolve();
    flushSync();

    expect(rowFor(target, 'runner')).toContain('[routing].complex');
    expect(rowFor(target, 'runner')).not.toContain('codex');
    expect(rowFor(target, 'runner')).not.toContain('opencode');
    expect(rowFor(target, 'complexity')).toContain('complex');
  });

  it('drops a stale FAILURE too, rather than wiping a good plan', async () => {
    const pending: Array<{
      resolve: (p: DispatchPlan) => void;
      reject: (e: Error) => void;
    }> = [];
    const read = () =>
      new Promise<DispatchPlan>((resolve, reject) => pending.push({ resolve, reject }));
    const target = render(read);

    await openPlan(target);
    choose(target, 'plan-complexity', 'simple');
    pending[1].resolve(plan({ runner: { value: 'codex', source: 'kew.toml [routing].simple' } }));
    await vi.waitFor(() => expect(text(target)).toContain('more fields ▸'));
    press(target, 'more fields ▸');
    await vi.waitFor(() => expect(rowFor(target, 'runner')).toContain('[routing].simple'));

    pending[0].reject(new Error('offline'));
    await Promise.resolve();
    flushSync();
    expect(text(target)).not.toContain('could not be resolved');
    expect(rowFor(target, 'runner')).toContain('codex');
  });

  it('says a failed read out loud, in place, keeping the last good plan', async () => {
    let fail = false;
    const read = async () => {
      if (fail) throw new Error('offline');
      return plan();
    };
    const target = render(read);
    await openPlan(target, { all: true });
    await vi.waitFor(() => expect(rowFor(target, 'runner')).toContain('codex'));
    fail = true;
    choose(target, 'plan-complexity', 'medium');
    // No toast: the sentence sits in the drawer, and the rows that were read
    // stay on screen.
    await vi.waitFor(() =>
      expect(document.querySelector('[role="dialog"]')?.textContent).toContain(
        'could not be resolved'
      )
    );
    expect(rowFor(target, 'runner')).toContain('codex');
  });
});

describe('the launch control', () => {
  it('is Run, the verb its note uses, and names why it is off (#3055)', async () => {
    const target = render(async () =>
      plan({
        admission: {
          fleet_stopped: true,
          fleet_stopped_reason: 'deploy',
          open_hold: false,
          headroom: { usd: 10, binds: 'daily', source: 's' },
          source: 's'
        }
      })
    );
    await openPlan(target);
    await vi.waitFor(() => expect(text(target)).toContain('kew dispatch 2302'));
    const run = target.querySelector('button[data-drawer-run]') as HTMLButtonElement;
    expect(run.textContent?.trim()).toBe('Run');
    expect(run.disabled).toBe(true);
    expect(target.querySelector('[data-launch-note]')?.textContent).toContain(
      'Run hands the command to the drive'
    );
    const reason = document.getElementById(run.getAttribute('aria-describedby') ?? '');
    expect(reason?.textContent).toBe('Run is off: fleet stopped.');
    expect([...target.querySelectorAll('button')].some((b) => b.textContent?.trim() === 'Launch')).toBe(
      false
    );
  });

  it('names the fleet latch above that rule', async () => {
    const target = render(async () =>
      plan({
        admission: {
          fleet_stopped: true,
          fleet_stopped_reason: 'deploy',
          open_hold: false,
          headroom: { usd: 10, binds: 'daily', source: 's' },
          source: 's'
        }
      })
    );
    await openPlan(target);
    await vi.waitFor(() => expect(text(target)).toContain('the fleet is stopped — deploy'));
  });
});

describe('an invalid row carries what fixes it', () => {
  it('reads an unknown tier as the default lane, with nothing to fix (#2785)', async () => {
    const target = render(async () =>
      plan({ complexity: { value: 'unknown', source: 'no complexity given', known: false } })
    );
    await openPlan(target, { all: true });
    await vi.waitFor(() => expect(rowFor(target, 'complexity')).toContain('default'));
    expect(rowFor(target, 'complexity')).not.toContain('unknown');
    expect(text(target)).not.toContain('choose a complexity');
    expect(target.querySelector('section[aria-label="What needs fixing"]')).toBeNull();
    const select = target.querySelector('#plan-complexity') as HTMLSelectElement;
    expect(select.selectedOptions[0]?.textContent).toBe('default');
  });

  it('starts the tier control at the tier the plan resolved', async () => {
    const read = vi.fn(async (q: Query) =>
      plan({
        complexity: q.complexity
          ? { value: q.complexity, source: 'the caller', known: true }
          : { value: 'complex', source: "the ready queue's `complex` label", known: true }
      })
    );
    const target = render(read);
    await openPlan(target, { all: true });
    const select = () => target.querySelector('#plan-complexity') as HTMLSelectElement;
    // #2845 (Q5): the untouched option names the item's tier and is the one
    // selected, so picking it back stays on it instead of snapping away.
    await vi.waitFor(() => expect(select().selectedOptions[0]?.textContent).toBe('from item: complex'));
    expect(select().value).toBe('');
    choose(target, 'plan-complexity', 'simple');
    await vi.waitFor(() => expect(rowFor(target, 'complexity')).toContain('simple'));
    choose(target, 'plan-complexity', '');
    await vi.waitFor(() => expect(rowFor(target, 'complexity')).toContain('complex'));
    expect(read.mock.calls.at(-1)?.[0]).toMatchObject({ complexity: null });
    expect(select().value).toBe('');
    expect(select().selectedOptions[0]?.textContent).toBe('from item: complex');
  });

  it('puts the fix on the row too, not only in the band above it', async () => {
    const target = render(async () =>
      plan({ containment: { ...plan().containment, image_present: false } })
    );
    await openPlan(target, { all: true });
    await vi.waitFor(() => expect(text(target)).toContain('NOT built on this host'));
    // The row the reader is looking at carries it: two links to the same
    // place, one in the summary band and one on the row.
    expect(target.querySelectorAll('a[href="/health#images"]')).toHaveLength(2);
  });

  it('links a missing image to the section of Health that explains it', async () => {
    const target = render(async () =>
      plan({
        containment: { ...plan().containment, image_present: false }
      })
    );
    await openPlan(target);
    await vi.waitFor(() => expect(text(target)).toContain('NOT built on this host'));
    const link = target.querySelector('a[href="/health#images"]');
    expect(link).not.toBeNull();
  });
});

describe('where the fix link sits', () => {
  /**
   * #2522 — where each printing of the fix link SITS. Both of these are a
   * structure a browser then lays out, and the layout is asserted by
   * `scripts/ui-check.mjs` ("the launchpad keeps its fix links in their own
   * box"), which a machine with no Chromium skips. These two hold the
   * structure that reading depends on, so the overprint cannot come back
   * silently on a CI run with no browser:
   *
   * * on the ROW, the link is a grid item of the row — its own line, starting
   *   in the value column — and not a wrapping item of the value cell, where
   *   it was drawn below a fixed 50px row and over the next row's value;
   * * in the BAND, the link is inside the remedy sentence, so it follows the
   *   last word instead of taking a ~60px column of its own beside it.
   */
  it('hangs the row fix off the row, as its own line in the value column', async () => {
    const target = render(async () =>
      plan({
        containment: {
          ...plan().containment,
          posture: 'refused',
          refusal_reason: 'no container runtime is available — this dispatch is refused'
        }
      })
    );
    await openPlan(target);
    await vi.waitFor(() => expect(rowFor(target, 'posture')).toContain('refused'));

    // The row, found the way `rowFor` finds one: the grid whose first cell is
    // the field name.
    const row = [...target.querySelectorAll('div')].find(
      (div) => (div.firstElementChild?.textContent ?? '').trim() === 'posture'
    );
    expect(row).toBeDefined();
    const link = row?.querySelector('a[href="/health#posture"]');
    expect(link).not.toBeNull();
    // A child of the ROW, not of the value cell: the cell is one wrapping
    // line, and a third item on it is the line the row had no height for.
    expect(link?.parentElement).toBe(row);
    // Placed in the value column, under the source, and never broken
    // mid-label: `truncate` is nowrap plus a clip, so a label that outgrew its
    // line is cut inside the row rather than spilling off it.
    expect(link?.className).toContain('col-start-2');
    expect(link?.className).toContain('truncate');
    // The row grows by that line rather than holding the artboard's 50px: a
    // jsdom has no layout, so the reading is the class that permits the growth.
    const classes = (row?.className ?? '').split(/\s+/);
    expect(classes).toContain('min-h-[50px]');
    expect(classes).not.toContain('h-[50px]');
  });

  it('puts the band fix inside the remedy sentence, not beside it', async () => {
    const target = render(async () =>
      plan({
        containment: {
          ...plan().containment,
          posture: 'refused',
          refusal_reason: 'no container runtime is available — this dispatch is refused'
        }
      })
    );
    await openPlan(target);
    await vi.waitFor(() => expect(text(target)).toContain('no container runtime is available'));

    const band = target.querySelector('section[aria-label="What needs fixing"]');
    const link = band?.querySelector('a[href="/health#posture"]');
    expect(link).not.toBeNull();
    // Inside the sentence, which is what makes it flow after the remedy: its
    // parent carries both the remedy's words and the link.
    const sentence = link?.parentElement;
    expect(sentence?.textContent).toContain('posture — refused');
    expect(sentence?.textContent).toContain('Health · posture by lane');
    // A space between the remedy and the link, so the two are not one word.
    expect(sentence?.textContent).toContain('refused Health · posture by lane');
    expect(link?.className).toContain('whitespace-nowrap');
  });
});

describe('the command block', () => {
  it('is a real code element, so it can be copied by hand', async () => {
    const target = render(async () => plan());
    await openPlan(target);
    await vi.waitFor(() => expect(text(target)).toContain('kew dispatch 2302'));
    const code = target.querySelector('code');
    expect(code?.textContent).toBe('kew dispatch 2302');
  });

  it('copies the command and says so', async () => {
    const writeText = vi.fn(async () => {});
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
    // Copy waits for the ledger to answer that nothing of the command is
    // queued (#2975).
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) =>
        url.startsWith('/api/run-requests?')
          ? Response.json({ requests: [] })
          : new Response('not in this test', { status: 404 })
      )
    );
    const target = render(async () => plan());
    await openPlan(target);
    await vi.waitFor(() => expect(text(target)).toContain('kew dispatch 2302'));
    await vi.waitFor(() =>
      expect(
        [...target.querySelectorAll<HTMLButtonElement>('button[aria-label="Copy command"]')].every(
          (b) => !b.disabled
        )
      ).toBe(true)
    );
    press(target, 'Copy');
    expect(writeText).toHaveBeenCalledWith('kew dispatch 2302');
    await vi.waitFor(() => expect(text(target)).toContain('Copied'));
  });
});

describe('progressive disclosure (#2535)', () => {
  const refusedPlan = () =>
    plan({
      containment: {
        ...plan().containment,
        posture: 'refused',
        refusal_reason:
          'no container runtime is available — this dispatch is refused. Install Docker/OrbStack.'
      }
    });

  /** The closed line: route, verdict and the primary action, and nothing more. */
  const summary = (target: HTMLElement): HTMLElement =>
    target.querySelector('[data-plan-summary]') as HTMLElement;

  it('closes to one line: route, verdict, and the copy', async () => {
    const target = render(async () => plan());
    await vi.waitFor(() => expect(text(summary(target))).toContain('codex / GPT-5.6 Sol'));
    const line = summary(target);
    expect(text(line)).toContain('ready');
    expect(line.querySelector('button[aria-label="Copy command"]')).not.toBeNull();
    // Held to a line by the layout, not by luck: nothing in it may wrap.
    expect(line.className).toContain('whitespace-nowrap');
    // And the rest is NOT on the page until asked for.
    expect(document.querySelector('[role="dialog"]')).toBeNull();
    expect(target.querySelector('div[style*="grid-template-columns"]')).toBeNull();
    expect(text(target)).not.toContain('the console never dispatches');
  });

  it('says refused, with the one phrase — not the remedy paragraph', async () => {
    const target = render(async () => refusedPlan());
    await vi.waitFor(() => expect(text(summary(target))).toMatch(/refused\s*— posture refused/));
    expect(text(summary(target))).not.toContain('Install Docker');
    // A refused plan's primary action is what is in the way, not the copy.
    expect(summary(target).querySelector('button[aria-label="Copy command"]')).toBeNull();
    expect(text(summary(target))).toContain('Details');
  });

  it('names the model in the line, with the id as its title and in the drawer', async () => {
    const target = render(async () =>
      plan({
        runner: { value: 'claude-code', source: 'kew.toml [routing].complex' },
        model: { value: 'claude-opus-5-5', source: 'kew.toml [routing].complex' }
      })
    );
    // #2785 (D8): the closed line reads the model's name; the id the resolver
    // sent, verbatim, is the hover.
    await vi.waitFor(() => expect(text(summary(target))).toContain('claude-code / Opus 5.5'));
    const route = summary(target).querySelector('[title]');
    expect(route?.getAttribute('title')).toBe('claude-code / claude-opus-5-5');
    await openPlan(target);
    const model = target.querySelector('[data-route="model"]');
    expect((model?.textContent ?? '').trim()).toBe('claude-opus-5-5');
    // Wrapped when it must be, never cut: a cut `claude-opus-5-5` is another id.
    expect(model?.className).toContain('break-all');
    expect(model?.className).not.toContain('truncate');
  });

  it('opens a drawer with the inputs first, then the route, then the table', async () => {
    const target = render(async () => plan());
    await vi.waitFor(() => expect(text(summary(target))).toContain('ready'));
    await openPlan(target);
    const dialog = document.querySelector('[role="dialog"]') as HTMLElement;
    expect(dialog.getAttribute('aria-modal')).toBe('true');
    const order = [
      dialog.querySelector('#plan-complexity'),
      dialog.querySelector('#plan-lane'),
      dialog.querySelector('[data-route="runner"]'),
      dialog.querySelector('code')
    ];
    for (let i = 1; i < order.length; i++) {
      expect(order[i - 1]!.compareDocumentPosition(order[i]!)).toBe(
        Node.DOCUMENT_POSITION_FOLLOWING
      );
    }
  });

  it('folds the set and default rows behind "N more fields"', async () => {
    const target = render(async () =>
      plan({ transport: { value: 'proxy', source: 'DB override (inference_transport.mode)' } })
    );
    await openPlan(target);
    await vi.waitFor(() => expect(rowFor(target, 'transport')).toContain('proxy'));
    // Only the overridden row is drawn; the route's set rows are folded.
    expect(rowFor(target, 'runner')).toBe('');
    expect(rowFor(target, 'spend so far')).toBe('');
    press(target, 'more fields ▸');
    expect(rowFor(target, 'runner')).toContain('codex');
    expect(rowFor(target, 'spend so far')).not.toBe('');
    press(target, 'fewer fields ▴');
    expect(rowFor(target, 'runner')).toBe('');
  });

  it('keeps a remedy to one line, with the whole reason behind details', async () => {
    const target = render(async () => refusedPlan());
    await openPlan(target);
    const band = await vi.waitFor(() => {
      const found = target.querySelector('section[aria-label="What needs fixing"]');
      expect(found).not.toBeNull();
      return found as HTMLElement;
    });
    expect(text(band)).not.toContain('Install Docker');
    const details = [...band.querySelectorAll('button')].find(
      (b) => (b.textContent ?? '').trim() === 'details'
    )!;
    expect(details.getAttribute('aria-expanded')).toBe('false');
    details.click();
    flushSync();
    expect(text(band)).toContain('Install Docker/OrbStack.');
    // And the Launch reasons name the refusal rather than print it a third time.
    const dialog = document.querySelector('[role="dialog"]') as HTMLElement;
    const blockers = [...dialog.querySelectorAll('li')].map((li) => li.textContent ?? '');
    expect(blockers.join(' | ')).toContain('the posture is refused');
    expect(blockers.join(' | ')).not.toContain('Install Docker');
  });

  it('closes on Escape without the press reaching the page behind it', async () => {
    const pageKeys = vi.fn();
    window.addEventListener('keydown', pageKeys);
    try {
      const target = render(async () => plan());
      await vi.waitFor(() => expect(text(summary(target))).toContain('ready'));
      await openPlan(target);
      const dialog = document.querySelector('[role="dialog"]') as HTMLElement;
      // A page shortcut from inside the drawer is the drawer's, not the page's.
      dialog.dispatchEvent(new KeyboardEvent('keydown', { key: 'j', bubbles: true }));
      dialog.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
      flushSync();
      expect(pageKeys).not.toHaveBeenCalled();
      expect(document.querySelector('[role="dialog"]')).toBeNull();
      // Focus goes back to the control that opened it.
      expect((document.activeElement?.textContent ?? '').trim()).toBe('Details');
    } finally {
      window.removeEventListener('keydown', pageKeys);
    }
  });

  it('lets ⌘K and ? through to the key layer, and suspends the page shortcuts', async () => {
    const palette = vi.fn();
    const help = vi.fn();
    const pageJ = vi.fn((e: KeyboardEvent) => e.preventDefault());
    const releases = [
      claimReserved('palette', palette),
      claimReserved('help', help),
      registerShortcuts([{ key: 'j', description: 'move', scope: 'work', handler: pageJ }])
    ];
    try {
      const target = render(async () => plan());
      await vi.waitFor(() => expect(text(summary(target))).toContain('ready'));
      await openPlan(target);
      const dialog = document.querySelector('[role="dialog"]') as HTMLElement;
      const press = (key: string, init: KeyboardEventInit = {}): KeyboardEvent => {
        const event = new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true, ...init });
        dialog.dispatchEvent(event);
        return event;
      };
      expect(press('k', { metaKey: true }).defaultPrevented).toBe(true);
      expect(press('?', { shiftKey: true }).defaultPrevented).toBe(true);
      press('j');
      expect(palette).toHaveBeenCalledOnce();
      expect(help).toHaveBeenCalledOnce();
      expect(pageJ).not.toHaveBeenCalled();
      expect(shortcutsSuspended()).toBe(true);
      press('Escape');
      flushSync();
      expect(shortcutsSuspended()).toBe(false);
    } finally {
      for (const release of releases) release();
    }
  });

  it('closes on a press of the scrim, and on Close', async () => {
    const target = render(async () => plan());
    await openPlan(target);
    const dialog = document.querySelector('[role="dialog"]') as HTMLElement;
    dialog.click();
    flushSync();
    expect(document.querySelector('[role="dialog"]')).not.toBeNull();
    (dialog.parentElement as HTMLElement).click();
    flushSync();
    expect(document.querySelector('[role="dialog"]')).toBeNull();
    await openPlan(target);
    press(target, 'Close');
    expect(document.querySelector('[role="dialog"]')).toBeNull();
  });
});

describe('a plan for another item is refused (#2845 P5)', () => {
  it("never paints another item's command under this item", async () => {
    const read = vi.fn(async () => plan({ item: '2302', issue_num: 2302 }));
    const { target } = renderSelected(read, '2410');
    await openPlan(target);
    await vi.waitFor(() =>
      expect(text(target)).toContain('the plan read answered for item 2302, not item 2410')
    );
    expect(text(target)).not.toContain('kew dispatch 2302');
    expect(target.querySelector('[data-command]')).toBeNull();
  });
});
