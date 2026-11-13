/**
 * #2513 — the `?` overlay as the reader drives it: `?` opens it over the Work
 * page, it lists the keys the page registered, and Esc closes it and hands the
 * focus back — without the Esc also reaching the page's own Esc behind it.
 *
 * Mounted in jsdom because the claim on `?`, the capture listener and the
 * focus return are all `$effect`s, which an SSR render never runs.
 */
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('$app/state', () => ({ page: { url: new URL('http://localhost/work') } }));

const { default: HelpOverlay } = await import('./HelpOverlay.svelte');
const { keyGlyph, registerShortcuts, resetShortcuts } = await import('$lib/keys/registry');
const { WORK_KEYS, workShortcuts } = await import('$lib/work/keys');

let target: HTMLElement;
let component: Record<string, unknown> | null = null;
let opener: HTMLButtonElement;
const intents: string[] = [];

function press(key: string, on: EventTarget = document.activeElement ?? document.body): KeyboardEvent {
  const event = new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true });
  on.dispatchEvent(event);
  flushSync();
  return event;
}

const dialog = (): HTMLElement | null => document.querySelector('[role="dialog"][data-help-overlay]');

beforeEach(() => {
  intents.length = 0;
  registerShortcuts(
    workShortcuts((intent, event) => {
      intents.push(intent);
      event.preventDefault();
    })
  );
  opener = document.createElement('button');
  opener.textContent = 'a Work row';
  document.body.appendChild(opener);
  target = document.createElement('div');
  document.body.appendChild(target);
  component = mount(HelpOverlay, { target });
  flushSync();
});

afterEach(() => {
  if (component) unmount(component);
  component = null;
  resetShortcuts();
  document.body.innerHTML = '';
});

describe('HelpOverlay', () => {
  it('opens on ? with the surface sentence and docs link', () => {
    opener.focus();
    expect(dialog()).toBeNull();
    press('?');
    const box = dialog();
    expect(box).not.toBeNull();
    expect(box!.querySelector('[data-help-answers]')?.textContent).toContain('What is mine to do');
    expect(box!.querySelector('a')?.getAttribute('href')).toBe('https://kew.dev/docs/web/#cockpit-shell');
  });

  it('on Work lists exactly the keys the registry holds for Work', () => {
    opener.focus();
    press('?');
    const keys = [...dialog()!.querySelectorAll('[data-help-key]')].map((k) => k.textContent);
    expect(keys.sort()).toEqual(WORK_KEYS.map((k) => keyGlyph(k.key)).sort());
  });

  it('closes on Esc, returns the focus, and the page never sees the Esc', () => {
    opener.focus();
    press('?');
    expect(document.activeElement).not.toBe(opener);
    const esc = press('Escape');
    expect(esc.defaultPrevented).toBe(true);
    expect(dialog()).toBeNull();
    expect(intents, 'the Esc also cleared the Work selection behind the overlay').toEqual([]);
    return Promise.resolve().then(() => expect(document.activeElement).toBe(opener));
  });

  it('holds the page keys while open, and gives them back when closed', () => {
    opener.focus();
    press('?');
    press('j');
    expect(intents).toEqual([]);
    press('?');
    expect(dialog()).toBeNull();
    press('j', document.body);
    expect(intents).toEqual(['down']);
  });
});
