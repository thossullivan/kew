/**
 * #2285 (C14) — `use:contained`: the half of `aria-modal` that was missing.
 *
 * The Config overrides dialog declared `aria-modal="true"` and then let Tab
 * walk out of its last field and on through the page behind it. These cases are
 * the wrap in both directions, the pull-back when focus has escaped the box
 * entirely, and the pass-through in the middle — because a trap that eats every
 * Tab is the same bug wearing the other coat.
 *
 * jsdom performs no default Tab movement, so what is asserted is what the
 * action decides: whether it took the press (`preventDefault` + the element it
 * moved to) or left it to the browser.
 */
import { afterEach, describe, expect, it } from 'vitest';
import { contained, onTop, topBox } from './contained';

let box: HTMLElement;
let action: { destroy(): void } | null = null;

/** A dialog box with the stops named, as the Config dialog's are. */
function open(inner: string): HTMLElement {
  box = document.createElement('div');
  box.setAttribute('role', 'dialog');
  box.setAttribute('aria-modal', 'true');
  box.tabIndex = -1;
  box.innerHTML = inner;
  document.body.appendChild(box);
  action = contained(box);
  return box;
}

/** Press Tab (or Shift+Tab) wherever focus currently is. */
function tab(shiftKey = false): KeyboardEvent {
  const event = new KeyboardEvent('keydown', {
    key: 'Tab',
    shiftKey,
    bubbles: true,
    cancelable: true
  });
  (document.activeElement ?? document.body).dispatchEvent(event);
  return event;
}

/**
 * Let focus go nowhere — what a click on the dialog's own padding does in a
 * real browser. `body.focus()` is a no-op under jsdom, so the current element
 * is blurred instead; either way `activeElement` ends up outside the box.
 */
function blur(): void {
  (document.activeElement as HTMLElement | null)?.blur();
}

const id = (): string | undefined => (document.activeElement as HTMLElement | null)?.id;

afterEach(() => {
  action?.destroy();
  action = null;
  box?.remove();
  document.body.innerHTML = '';
});

const THREE = `
  <input id="first" />
  <input id="middle" />
  <button id="last">Confirm</button>
`;

describe('opening the box', () => {
  it('moves focus into it, so the reader is not left on the opener', () => {
    const opener = document.createElement('button');
    document.body.appendChild(opener);
    opener.focus();

    const dialog = open(THREE);

    expect(document.activeElement).toBe(dialog);
  });
});

describe('Tab', () => {
  it('wraps from the last stop to the first', () => {
    open(THREE);
    document.getElementById('last')!.focus();

    const event = tab();

    expect(event.defaultPrevented).toBe(true);
    expect(id()).toBe('first');
  });

  it('wraps backwards from the first stop to the last', () => {
    open(THREE);
    document.getElementById('first')!.focus();

    const event = tab(true);

    expect(event.defaultPrevented).toBe(true);
    expect(id()).toBe('last');
  });

  it('leaves a press in the middle of the box to the browser', () => {
    open(THREE);
    document.getElementById('middle')!.focus();

    const event = tab();

    expect(event.defaultPrevented).toBe(false);
    expect(id()).toBe('middle');
  });

  it('leaves the first press to the browser, which moves inward on its own', () => {
    const dialog = open(THREE);
    expect(document.activeElement).toBe(dialog);

    expect(tab().defaultPrevented).toBe(false);
  });

  it('wraps the FIRST press backwards to the last stop, not out of the dialog', () => {
    // The position `contained` itself puts focus in on mount: the box holds it,
    // and the box is not one of the stops. Forward from here the browser moves
    // inward on its own (above), but backward it would move to whatever
    // precedes the dialog — the Config page behind an `aria-modal` box.
    const dialog = open(THREE);
    expect(document.activeElement).toBe(dialog);

    const event = tab(true);

    expect(event.defaultPrevented).toBe(true);
    expect(id()).toBe('last');
  });

  it('wraps backwards from a non-stop inside the box ahead of the first stop', () => {
    // A `tabindex="-1"` heading the dialog focuses for a screen reader: the
    // browser's backward move from there leaves the box just as the box's own
    // holder does, so the action has to take that press too.
    open(`
      <h2 id="title" tabindex="-1">Overrides</h2>
      ${THREE}
    `);
    document.getElementById('title')!.focus();

    const event = tab(true);

    expect(event.defaultPrevented).toBe(true);
    expect(id()).toBe('last');
  });

  it('pulls focus back in when it is outside the box', () => {
    // The press that puts focus here: a click on the dialog's own padding,
    // which leaves `activeElement` on `body` in a real browser — the reason
    // this listener is on the document rather than on the element.
    open(THREE);
    blur();
    expect(box.contains(document.activeElement)).toBe(false);

    const event = tab();

    expect(event.defaultPrevented).toBe(true);
    expect(id()).toBe('first');
  });

  it('pulls focus back to the LAST stop on a Shift+Tab from outside', () => {
    open(THREE);
    blur();

    expect(tab(true).defaultPrevented).toBe(true);
    expect(id()).toBe('last');
  });

  it('skips a disabled control, so a saving dialog cannot be tabbed out of', () => {
    open(`
      <input id="first" disabled />
      <input id="only" />
      <button id="last" disabled>Confirm</button>
    `);
    document.getElementById('only')!.focus();

    const event = tab();

    expect(event.defaultPrevented).toBe(true);
    expect(id()).toBe('only');
  });

  it('holds the press in a box with nothing to move to', () => {
    const dialog = open('<p>Reading…</p>');
    blur();

    const event = tab();

    expect(event.defaultPrevented).toBe(true);
    expect(document.activeElement).toBe(dialog);
  });
});

describe('every other key', () => {
  it('is left alone — Escape is the page’s one window handler, not ours', () => {
    open(THREE);
    document.getElementById('last')!.focus();

    const escape = new KeyboardEvent('keydown', {
      key: 'Escape',
      bubbles: true,
      cancelable: true
    });
    document.activeElement!.dispatchEvent(escape);

    expect(escape.defaultPrevented).toBe(false);
    expect(id()).toBe('last');
  });
});

describe('closing the box', () => {
  it('stops containing anything', () => {
    open(THREE);
    document.getElementById('last')!.focus();
    action!.destroy();
    action = null;

    expect(tab().defaultPrevented).toBe(false);
  });
});

describe('a box opened over another (#2841)', () => {
  /** A second box over the first — the palette over the launchpad drawer. */
  function openOver(inner: string): { node: HTMLElement; action: { destroy(): void } } {
    const node = document.createElement('div');
    node.setAttribute('role', 'dialog');
    node.tabIndex = -1;
    node.innerHTML = inner;
    document.body.appendChild(node);
    return { node, action: contained(node) };
  }

  it('keeps Tab in the top box: the one beneath does not pull it back', () => {
    open(THREE);
    const top = openOver('<input id="query" /><button id="cancel">Cancel</button>');
    document.getElementById('query')!.focus();

    // Forward from the input has Cancel ahead of it, so the press is the
    // browser's; the box beneath used to take it and focus its own first stop.
    expect(tab().defaultPrevented).toBe(false);
    expect(id()).toBe('query');

    document.getElementById('cancel')!.focus();
    expect(tab().defaultPrevented).toBe(true);
    expect(id()).toBe('query');
    top.action.destroy();
  });

  it('hands focus back to the box beneath when the top one closes', async () => {
    open(THREE);
    const top = openOver('<button id="cancel">Cancel</button>');
    expect(document.activeElement).toBe(top.node);

    top.action.destroy();
    top.node.remove();
    await Promise.resolve();

    expect(document.activeElement).toBe(box);
    document.getElementById('last')!.focus();
    expect(tab().defaultPrevented).toBe(true);
    expect(id()).toBe('first');
  });

  it('names the top box, and the one beneath once the top one closes', () => {
    open(THREE);
    expect(topBox()).toBe(box);
    const top = openOver('<button id="cancel">Cancel</button>');
    expect(topBox()).toBe(top.node);
    expect(onTop(box)).toBe(false);

    top.action.destroy();

    expect(topBox()).toBe(box);
    action!.destroy();
    action = null;
    expect(topBox()).toBeNull();
  });

  it('leaves focus where the top box’s owner put it inside the box beneath', async () => {
    open(THREE);
    document.getElementById('middle')!.focus();
    const top = openOver('<button id="cancel">Cancel</button>');

    top.action.destroy();
    top.node.remove();
    document.getElementById('middle')!.focus();
    await Promise.resolve();

    expect(id()).toBe('middle');
  });
});
