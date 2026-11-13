/**
 * #2278 (C15) — the two `<details>` menus close the way a menu closes.
 *
 * jsdom, because the whole of `dismissable` is listeners on a document and the
 * order two of them run in: a press outside the menu, and an Escape that must
 * be spent on the menu rather than on the page behind it. The action is called
 * directly rather than through `use:` — it is plain DOM, and mounting a
 * component would only put a Svelte binding between the press and the answer.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { dismissable } from './dismissable';

let open: Array<{ destroy(): void }> = [];

function menu(): { details: HTMLDetailsElement; outside: HTMLElement } {
  document.body.innerHTML = `
    <details open>
      <summary>More</summary>
      <div><a href="/runs" id="entry">Runs</a></div>
    </details>
    <main id="outside">the page</main>`;
  const details = document.querySelector('details') as HTMLDetailsElement;
  open.push(dismissable(details));
  return { details, outside: document.getElementById('outside') as HTMLElement };
}

function press(on: EventTarget, key: string): KeyboardEvent {
  const event = new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true });
  on.dispatchEvent(event);
  return event;
}

function point(on: EventTarget): void {
  on.dispatchEvent(new MouseEvent('pointerdown', { bubbles: true }));
}

afterEach(() => {
  for (const handle of open) handle.destroy();
  open = [];
  document.body.innerHTML = '';
});

describe('dismissable', () => {
  it('closes an open menu on a press anywhere outside it', () => {
    const { details, outside } = menu();
    point(outside);
    expect(details.open).toBe(false);
  });

  it('leaves a press inside the menu to the menu — its own entries still click', () => {
    const { details } = menu();
    point(document.getElementById('entry') as HTMLElement);
    expect(details.open).toBe(true);
  });

  it('closes on Escape', () => {
    const { details } = menu();
    press(document.getElementById('entry') as HTMLElement, 'Escape');
    expect(details.open).toBe(false);
  });

  it('spends the Escape on the menu, so the page behind it keeps its selection', () => {
    const { details } = menu();
    const clearSelection = vi.fn();
    window.addEventListener('keydown', clearSelection);
    try {
      press(document.getElementById('entry') as HTMLElement, 'Escape');
      expect(details.open).toBe(false);
      expect(clearSelection).not.toHaveBeenCalled();
    } finally {
      window.removeEventListener('keydown', clearSelection);
    }
  });

  it('lets Escape through once no menu is open — the second press clears', () => {
    const { details } = menu();
    details.open = false;
    const clearSelection = vi.fn();
    window.addEventListener('keydown', clearSelection);
    try {
      press(document.body, 'Escape');
      expect(clearSelection).toHaveBeenCalledOnce();
    } finally {
      window.removeEventListener('keydown', clearSelection);
    }
  });

  it('takes focus back to the summary, not to whatever the closed menu held', () => {
    const { details } = menu();
    const entry = document.getElementById('entry') as HTMLElement;
    entry.focus();
    press(entry, 'Escape');
    expect(document.activeElement).toBe(details.querySelector('summary'));
  });

  it('claims no other key', () => {
    const { details } = menu();
    press(document.body, 'ArrowDown');
    expect(details.open).toBe(true);
  });

  it('unbinds on destroy, so an unmounted menu stops eating presses', () => {
    const { details, outside } = menu();
    for (const handle of open) handle.destroy();
    open = [];
    point(outside);
    const event = press(document.body, 'Escape');
    expect(details.open).toBe(true);
    expect(event.defaultPrevented).toBe(false);
  });
});
