/**
 * #2516 — the palette, driven: the reserved chord opens it, typing an item
 * number and pressing Enter navigates there, Esc closes it and puts focus
 * back, and opening it starts no timer.
 *
 * jsdom, because every one of those is an effect or a key event.
 */
import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import dashboardJson from '../../../fixtures/factory/dashboard.json';
import projectionJson from '../../../fixtures/factory/dashboard-work.json';
import epicsJson from '../../../fixtures/factory/epics.json';
import runsJson from '../../../fixtures/factory/runs.json';

const goto = vi.fn(async () => {});
const fetchPaletteSources = vi.fn();
const pulse = { dashboard: dashboardJson, runs: runsJson };

vi.mock('$app/environment', () => ({ browser: true }));
vi.mock('$app/navigation', () => ({ goto }));
const fetchDispatchPlan = vi.fn(async () => null);
const fetchMeta = vi.fn();
const askPaletteModel = vi.fn();
class PaletteAskRefused extends Error {
  constructor(readonly sentence: string) {
    super(sentence);
  }
}
vi.mock('$lib/api', () => ({
  PaletteAskRefused,
  fetchPaletteSources,
  fetchDispatchPlan,
  fetchMeta,
  askPaletteModel,
  fetchWorkView: vi.fn(),
  performWorkAction: vi.fn()
}));
vi.mock('$lib/shell/pulse.svelte', () => ({ factoryPulse: pulse }));

const { default: Palette } = await import('./Palette.svelte');
const { resetShortcuts } = await import('$lib/keys/registry');

let component: Record<string, unknown> | null = null;
let opener: HTMLButtonElement;

beforeEach(() => {
  fetchPaletteSources.mockResolvedValue({ epics: epicsJson, projection: projectionJson });
  fetchMeta.mockResolvedValue({ palette_ask: false });
  opener = document.createElement('button');
  opener.textContent = 'opener';
  document.body.append(opener);
  component = mount(Palette, { target: document.body });
  flushSync();
});

afterEach(() => {
  if (component) unmount(component);
  component = null;
  resetShortcuts();
  document.body.innerHTML = '';
  vi.restoreAllMocks();
  goto.mockClear();
  fetchPaletteSources.mockReset();
  fetchMeta.mockReset();
  askPaletteModel.mockReset();
});

const settle = async () => {
  for (let i = 0; i < 5; i += 1) {
    await tick();
    await Promise.resolve();
  }
  flushSync();
};

function press(target: EventTarget, key: string, init: KeyboardEventInit = {}): void {
  target.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true, ...init }));
  flushSync();
}

async function openPalette(): Promise<HTMLInputElement> {
  opener.focus();
  press(opener, 'k', { ctrlKey: true });
  await settle();
  const field = document.querySelector<HTMLInputElement>('[role="dialog"] input');
  expect(field).not.toBeNull();
  return field!;
}

function type(field: HTMLInputElement, text: string): void {
  field.value = text;
  field.dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();
}

describe('the palette', () => {
  it('opens on Ctrl+K and on Meta+K, with the field focused', async () => {
    expect(document.querySelector('[role="dialog"]')).toBeNull();
    const field = await openPalette();
    expect(document.activeElement).toBe(field);
    press(field, 'Escape');
    opener.focus();
    press(opener, 'K', { metaKey: true });
    await settle();
    expect(document.querySelector('[role="dialog"]')).not.toBeNull();
  });

  it('ranks a typed item number first and navigates there on Enter', async () => {
    const field = await openPalette();
    type(field, '2055');
    const first = document.querySelector('[role="option"]');
    expect(first?.getAttribute('aria-selected')).toBe('true');
    expect(first?.textContent).toContain('item');
    expect(first?.textContent).toContain('2055');
    press(field, 'Enter');
    await settle();
    expect(goto).toHaveBeenCalledWith('/work?item=2055');
    expect(document.querySelector('[role="dialog"]')).toBeNull();
  });

  it('moves with the arrows', async () => {
    const field = await openPalette();
    press(field, 'ArrowDown');
    const options = [...document.querySelectorAll('[role="option"]')];
    expect(options[1].getAttribute('aria-selected')).toBe('true');
    press(field, 'Enter');
    await settle();
    expect(goto).toHaveBeenCalledWith('/monitor');
  });

  it('closes on Esc and gives focus back to where it was', async () => {
    const field = await openPalette();
    const seen = vi.fn();
    window.addEventListener('keydown', seen);
    press(field, 'Escape');
    window.removeEventListener('keydown', seen);
    expect(document.querySelector('[role="dialog"]')).toBeNull();
    expect(document.activeElement).toBe(opener);
    // Stopped at the palette: a surface underneath that answers Esc from a
    // field must not close itself too.
    expect(seen).not.toHaveBeenCalled();
  });
});

describe('polling', () => {
  it('opening the palette starts no interval timer', async () => {
    const setIntervalSpy = vi.spyOn(globalThis, 'setInterval');
    const windowSpy = vi.spyOn(window, 'setInterval');
    const field = await openPalette();
    type(field, 'work');
    await settle();
    expect(setIntervalSpy).not.toHaveBeenCalled();
    expect(windowSpy).not.toHaveBeenCalled();
  });

  it('reads once on open, not again until the pulse lands a reading', async () => {
    await openPalette();
    expect(fetchPaletteSources).toHaveBeenCalledTimes(1);
  });
});

describe('stopping the fleet (#2517)', () => {
  it('opens a confirm step, and posts only after a reason and the confirm', async () => {
    const sent = vi.fn(
      async () =>
        new Response(JSON.stringify({ stopped: true, already_stopped: false, runs_killed: [] }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' }
        })
    );
    vi.stubGlobal('fetch', sent);
    try {
      const field = await openPalette();
      type(field, 'stop the fleet');
      press(field, 'Enter');
      await settle();
      const form = document.querySelector<HTMLFormElement>('[role="dialog"] form');
      expect(form?.getAttribute('aria-label')).toBe('Confirm Stop the fleet');
      const confirm = document.querySelector<HTMLButtonElement>('#palette-confirm');
      expect(confirm?.disabled).toBe(true);
      // Submitting without a reason sends nothing.
      form!.requestSubmit();
      await settle();
      expect(sent).not.toHaveBeenCalled();
      const reason = document.querySelector<HTMLInputElement>('#palette-reason')!;
      expect(document.activeElement).toBe(reason);
      type(reason, 'runaway spend');
      expect(confirm?.disabled).toBe(false);
      confirm!.click();
      await settle();
      expect(sent).toHaveBeenCalledTimes(1);
      const [url, init] = sent.mock.calls[0] as unknown as [string, RequestInit];
      expect(url).toBe('/api/fleet/stop');
      expect(init.method).toBe('POST');
      expect(JSON.parse(String(init.body))).toEqual({ reason: 'runaway spend' });
      expect(document.querySelector('[role="status"]')?.textContent).toContain('stopped');
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it('Esc leaves the confirm step without writing', async () => {
    const sent = vi.fn();
    vi.stubGlobal('fetch', sent);
    try {
      const field = await openPalette();
      type(field, 'stop the fleet');
      press(field, 'Enter');
      await settle();
      press(document.querySelector('#palette-reason')!, 'Escape');
      await settle();
      expect(document.querySelector('[role="dialog"] form')).toBeNull();
      expect(document.querySelector('[role="dialog"] input[role="combobox"]')).not.toBeNull();
      expect(sent).not.toHaveBeenCalled();
    } finally {
      vi.unstubAllGlobals();
    }
  });
});

describe('ask mode (#2518)', () => {
  it('is disabled by default: no hint, and a ? query never asks', async () => {
    const field = await openPalette();
    expect(field.placeholder).not.toContain('ask');
    type(field, 'show me stuck epics?');
    expect(document.querySelector('[role="dialog"]')?.textContent).not.toContain('Enter to ask');
    press(field, 'Enter');
    press(field, 'Tab');
    await settle();
    expect(askPaletteModel).not.toHaveBeenCalled();
  });

  it('asks on Enter, and a ranked result selects through its own confirm step', async () => {
    fetchMeta.mockResolvedValue({ palette_ask: true });
    askPaletteModel.mockResolvedValue({
      results: [
        { id: 'command:stop-fleet', reason: 'halts every agent' },
        { id: 'command:not-offered', reason: 'made up' }
      ],
      cost_usd: 0
    });
    const sent = vi.fn();
    vi.stubGlobal('fetch', sent);
    try {
      const field = await openPalette();
      expect(field.placeholder).toContain('? to ask');
      type(field, 'how do I halt everything?');
      expect(document.querySelector('[role="dialog"]')?.textContent).toContain('Enter to ask');
      press(field, 'Enter');
      await settle();
      expect(askPaletteModel).toHaveBeenCalledTimes(1);
      const [body] = askPaletteModel.mock.calls[0] as [{ query: string; entries: unknown[] }];
      expect(body.query).toBe('how do I halt everything?');
      const options = [...document.querySelectorAll('[role="option"]')];
      expect(options).toHaveLength(1);
      expect(options[0].textContent).toContain('Stop the fleet');
      expect(options[0].textContent).toContain('halts every agent');
      // Selecting the result is selecting the entry: the confirm step opens,
      // and nothing is sent without it.
      press(field, 'Enter');
      await settle();
      expect(askPaletteModel).toHaveBeenCalledTimes(1);
      const form = document.querySelector<HTMLFormElement>('[role="dialog"] form');
      expect(form?.getAttribute('aria-label')).toBe('Confirm Stop the fleet');
      expect(sent).not.toHaveBeenCalled();
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it('over the daily budget shows the refusal in place of results, and Enter does nothing', async () => {
    const sentence = "Today's spend ($5.00) has reached the daily budget of $5.00 ([budget] daily_usd), so nothing was asked.";
    fetchMeta.mockResolvedValue({ palette_ask: true });
    askPaletteModel.mockRejectedValue(new PaletteAskRefused(sentence));
    const sent = vi.fn();
    vi.stubGlobal('fetch', sent);
    try {
      const field = await openPalette();
      type(field, 'how do I halt everything?');
      press(field, 'Enter');
      await settle();
      expect(askPaletteModel).toHaveBeenCalledTimes(1);
      const list = document.querySelector('#palette-results');
      expect(document.querySelectorAll('[role="option"]')).toHaveLength(0);
      expect(list?.textContent?.trim()).toBe(sentence);
      // Enter neither asks again nor selects: nothing is listed to select.
      press(field, 'Enter');
      await settle();
      expect(askPaletteModel).toHaveBeenCalledTimes(1);
      expect(goto).not.toHaveBeenCalled();
      expect(document.querySelector('[role="dialog"] form')).toBeNull();
      expect(sent).not.toHaveBeenCalled();
    } finally {
      vi.unstubAllGlobals();
    }
  });
});
