/**
 * #2278 (C15) — a `<details>` menu that closes the way a menu closes.
 *
 * `<details>` is the app's menu primitive: no library, no focus trap, and the
 * disclosure state is the element's own. What it does not give is a way out.
 * The browser closes one on a second press of its summary and on nothing else,
 * so the header's More menu and the Work filter both stayed open through a
 * click anywhere else on the page, and through Escape — the key a reader
 * reaches for. On Work, Escape did worse than nothing: the page's window
 * handler read it as "clear the selection" and threw the reader's item away
 * while the menu they were trying to dismiss stayed up.
 *
 * Both are one action, applied with `use:dismissable`
 * (`routes/(factory)/menus.test.ts` reads the tree so a third menu cannot ship
 * without it):
 *
 * * A press outside the element closes it. `pointerdown`, not `click`, so the
 *   menu is gone before the press lands — a reader pressing a control behind an
 *   open menu gets that control, not a dismissal they have to repeat.
 * * Escape closes it, and the press stops there. `stopPropagation` is what puts
 *   the menu ahead of the selection on Work, and the listener is in the
 *   document's CAPTURE phase so it runs before anything bound to the window —
 *   `svelte:window onkeydown` binds in the bubble phase, which is last of all.
 *   With no menu open the listener declines the press, so the next Escape is
 *   the page's and clears the selection as it always did.
 *
 * The listeners are on the document rather than the element because an outside
 * press is by definition not on the element, and because an Escape has to be
 * answered with focus anywhere — including nowhere, the state after a page load.
 */
export function dismissable(node: HTMLDetailsElement): { destroy(): void } {
  function closeFromOutside(event: Event): void {
    if (!node.open) return;
    const target = event.target;
    if (target instanceof Node && node.contains(target)) return;
    node.open = false;
  }

  function closeOnEscape(event: KeyboardEvent): void {
    if (event.key !== 'Escape' || !node.open) return;
    node.open = false;
    event.stopPropagation();
    // Focus goes back to the control that opened the menu, not to whatever the
    // menu held — that element is about to be hidden, and focus on a hidden
    // element is focus nowhere.
    if (node.contains(document.activeElement)) node.querySelector('summary')?.focus();
  }

  document.addEventListener('pointerdown', closeFromOutside, true);
  document.addEventListener('keydown', closeOnEscape, true);

  return {
    destroy(): void {
      document.removeEventListener('pointerdown', closeFromOutside, true);
      document.removeEventListener('keydown', closeOnEscape, true);
    }
  };
}
