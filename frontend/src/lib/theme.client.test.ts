/**
 * The theme survives blocked storage (design review c9 X4).
 *
 * With site data blocked, touching `localStorage` throws. The theme is read in
 * the root layout's effect, so an unguarded read left every page blank.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('$app/environment', () => ({ browser: true }));

const { theme } = await import('./theme.svelte');

/** Storage as a browser with site data blocked serves it: every touch throws. */
function blockStorage(): void {
  Object.defineProperty(window, 'localStorage', {
    configurable: true,
    get() {
      throw new DOMException('The operation is insecure.', 'SecurityError');
    }
  });
}

const saved = Object.getOwnPropertyDescriptor(window, 'localStorage');

afterEach(() => {
  if (saved) Object.defineProperty(window, 'localStorage', saved);
  document.documentElement.removeAttribute('data-theme');
});

describe('the theme with storage blocked', () => {
  it('renders the default theme rather than throwing', () => {
    blockStorage();
    expect(() => theme.init()).not.toThrow();
    expect(theme.current).toBe('dark');
    expect(document.documentElement.getAttribute('data-theme')).toBe('dark');
  });

  it('still toggles, for this page', () => {
    blockStorage();
    theme.init();
    expect(() => theme.toggle()).not.toThrow();
    expect(document.documentElement.getAttribute('data-theme')).toBe('light');
    theme.toggle();
  });
});

describe('the theme with storage', () => {
  it('reads and keeps the saved choice', () => {
    // An in-memory store: this jsdom serves no `localStorage` of its own.
    const items = new Map<string, string>([['kew-theme', 'light']]);
    Object.defineProperty(window, 'localStorage', {
      configurable: true,
      value: {
        getItem: (key: string) => items.get(key) ?? null,
        setItem: (key: string, value: string) => void items.set(key, value)
      }
    });
    theme.init();
    expect(document.documentElement.getAttribute('data-theme')).toBe('light');
    theme.toggle();
    expect(items.get('kew-theme')).toBe('dark');
  });
});
