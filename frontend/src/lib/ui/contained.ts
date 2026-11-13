/**
 * #2285 (C14) — a modal that keeps the focus it claims to have.
 *
 * The Config overrides dialog says `aria-modal="true"`, which is a promise to
 * assistive technology that nothing outside the box is reachable. It was not
 * kept: the dialog is a plain `<div>` over the page (no `<dialog>`, no library,
 * matching `dismissable`'s minimalism), so Tab walked straight out of the last
 * field and on through the Config page behind it — a keyboard reader editing an
 * override ended up pressing controls they could not see.
 *
 * One action, `use:contained`, applied to the box that carries the
 * `role="dialog"` (`routes/(factory)/config/+page.svelte`; the modal tests read
 * the tree so a third dialog cannot ship without it):
 *
 * * Focus moves into the box on mount, so the reader is not left on the opener.
 * * Tab and Shift+Tab cycle within the box: past the last stop is the first,
 *   before the first is the last.
 * * Focus that is OUTSIDE the box when Tab is pressed is pulled back in, which
 *   is why the listener is on the document in the capture phase rather than on
 *   the element — a press on the dialog's own padding leaves focus on `body`,
 *   where an element-bound handler would never see the next Tab.
 *
 * Escape is deliberately NOT here: it is the page's one window-level handler,
 * which is the other half of C14 (it used to be bound twice, at the window and
 * on the backdrop, so one press ran two dismissals).
 *
 * #2841 (Q1) — dialogs stack: the palette or help opens over the launchpad
 * drawer. Every box's keeper listens on the document, and the one beneath was
 * registered first, so it ran first and pulled every Tab back into a box the
 * reader could no longer see. The open boxes are one stack: only the top one
 * keeps Tab (`onTop`; a box's own Escape handler asks the same, so a press
 * left on `body` closes only the top box), and
 * closing it hands focus back to the one beneath when nothing else took it.
 */

/** The open boxes, oldest first: the last is the one the reader is in. */
const stack: HTMLElement[] = [];

/** The top open box — the one that keeps Tab and Escape — or null. */
export function topBox(): HTMLElement | null {
  return stack.length > 0 ? stack[stack.length - 1] : null;
}

/** Whether `node` is the top open box. */
export function onTop(node: Element): boolean {
  return topBox() === node;
}

/**
 * The stops a Tab can land on, in document order. No layout is consulted —
 * `offsetParent` is always `null` under jsdom, so a visibility test would make
 * this untestable and would drop every stop in the suite. `[hidden]` and a
 * negative `tabIndex` are the two exclusions the dialogs actually need.
 */
const TABBABLE = [
  'a[href]',
  'button',
  'input',
  'select',
  'textarea',
  '[tabindex]'
]
  .map((s) => `${s}:not([disabled]):not([hidden])`)
  .join(',');

/**
 * Whether `a` comes before `b` in document order. An ancestor counts as before
 * its descendants, which is what makes the box's own focus holder read as
 * standing in front of every stop inside it.
 */
function before(a: Node, b: Node): boolean {
  return (a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0;
}

export function contained(node: HTMLElement): { destroy(): void } {
  function stops(): HTMLElement[] {
    return [...node.querySelectorAll<HTMLElement>(TABBABLE)].filter((el) => el.tabIndex >= 0);
  }

  function keep(event: KeyboardEvent): void {
    if (event.key !== 'Tab' || !onTop(node)) return;
    const reachable = stops();
    // A box with nothing to move to still holds the press: leaving it would
    // land the reader behind a modal they cannot see.
    if (reachable.length === 0) {
      event.preventDefault();
      node.focus();
      return;
    }
    const back = event.shiftKey;
    const active = document.activeElement;
    // The press is left to the browser exactly when its own move stays in the
    // box — when there is a stop on the side the press is heading for. Identity
    // against the first/last stop is not enough to decide that: the box itself
    // holds focus before the first Tab (it is `tabindex="-1"`, so it is not one
    // of the stops), and from there FORWARD has the whole list ahead of it and
    // moves inward on its own, while BACKWARD has nothing in front of it and
    // would walk straight out of an `aria-modal` dialog into the page behind.
    if (active instanceof Node && node.contains(active)) {
      const stays = reachable.some((el) => (back ? before(el, active) : before(active, el)));
      if (stays) return;
    }
    event.preventDefault();
    (back ? reachable[reachable.length - 1] : reachable[0]).focus();
  }

  document.addEventListener('keydown', keep, true);
  stack.push(node);
  node.focus();

  return {
    destroy(): void {
      document.removeEventListener('keydown', keep, true);
      const at = stack.indexOf(node);
      if (at === -1) return;
      stack.splice(at, 1);
      if (at !== stack.length) return;
      // The top box closed. Its owner may put focus back on its opener (help
      // does, a microtask on); if that is not inside the box now on top, the
      // reader would be left behind a modal, so the box beneath takes it.
      queueMicrotask(() => {
        const top = stack[stack.length - 1];
        const active = document.activeElement;
        if (top === undefined || (active instanceof Node && top.contains(active))) return;
        top.focus();
      });
    }
  };
}
