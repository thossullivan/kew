/**
 * Mount points for chrome that is not the console's own (#3139).
 *
 * Three places in the factory shell draw whatever component is set here, and
 * nothing while it is null: a banner above the header, a slot at the top of
 * the header's More menu (the row itself has no width to spare), and a slot
 * at the foot of the page. The demo build fills
 * them (its banner here; its Tours entry in the header slot, #3145; its page
 * foot, #3144) from `src/lib/demo/`, so the shell never imports demo code and
 * the normal build, which leaves all three null, draws nothing extra.
 */
import type { Component } from 'svelte';

export type ChromeSlots = {
  banner: Component | null;
  header: Component | null;
  foot: Component | null;
};

export const chromeSlots: ChromeSlots = $state({ banner: null, header: null, foot: null });
