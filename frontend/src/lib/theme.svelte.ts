import { browser } from '$app/environment';

type Theme = 'dark' | 'light';

const KEY = 'kew-theme';

/**
 * The saved theme, or the default. Every storage touch is guarded (design
 * review c9 X4): with site data blocked, `localStorage` throws on access, and
 * this read runs in the root layout's effect — unguarded, the throw left the
 * page blank. `app.html`'s pre-paint script already guards the same read.
 */
function read(): Theme {
  if (!browser) return 'dark';
  try {
    return localStorage.getItem(KEY) === 'light' ? 'light' : 'dark';
  } catch {
    return 'dark';
  }
}

/** Remember the choice where storage allows; the toggle works either way. */
function write(theme: Theme): void {
  if (!browser) return;
  try {
    localStorage.setItem(KEY, theme);
  } catch {
    // Storage blocked or full: the choice lasts for this page only.
  }
}

class ThemeStore {
  current = $state<Theme>('dark');

  init() {
    this.current = read();
    this.apply();
  }

  private apply() {
    if (!browser) return;
    document.documentElement.setAttribute('data-theme', this.current);
  }

  toggle() {
    this.current = this.current === 'dark' ? 'light' : 'dark';
    write(this.current);
    this.apply();
  }
}

export const theme = new ThemeStore();
