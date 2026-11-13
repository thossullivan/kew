// Whether the tab is in front, as reactive state (#2789, K7).
//
// A poll can ask `document.hidden` at each beat, but a channel that stays OPEN
// — the epic page's EventSource — has no beat to ask at: it has to be closed
// when the tab goes to the background and opened again when it comes back, and
// an `$effect` can only do that if visibility is something it can depend on.
// One listener for the whole app, installed on first import in the browser.
import { browser } from '$app/environment';

class PageVisibility {
  visible = $state(!browser || !document.hidden);

  constructor() {
    if (!browser) return;
    document.addEventListener('visibilitychange', () => {
      this.visible = !document.hidden;
    });
  }
}

export const pageVisibility = new PageVisibility();
