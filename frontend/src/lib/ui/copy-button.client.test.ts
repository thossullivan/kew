/**
 * #3459 L7 — a Copy that could not reach the clipboard says so.
 *
 * The clipboard is the browser's boundary, so it is the one thing replaced
 * here: absent (an insecure origin has no `navigator.clipboard`), refusing,
 * and taking the text.
 */
import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, describe, expect, it } from 'vitest';
import CopyButton from './CopyButton.svelte';

let open: ReturnType<typeof mount> | null = null;
const real = Object.getOwnPropertyDescriptor(navigator, 'clipboard');

afterEach(() => {
  if (open) unmount(open);
  open = null;
  document.body.innerHTML = '';
  if (real) Object.defineProperty(navigator, 'clipboard', real);
  else Reflect.deleteProperty(navigator, 'clipboard');
});

function clipboard(value: unknown) {
  Object.defineProperty(navigator, 'clipboard', { value, configurable: true });
}

async function press(): Promise<HTMLButtonElement> {
  open = mount(CopyButton, { target: document.body, props: { text: 'kew finalize r1', label: 'Copy the command' } });
  flushSync();
  const button = document.querySelector<HTMLButtonElement>('button');
  if (!button) throw new Error('no button drawn');
  button.click();
  // writeText settles, then the component's state, then the DOM.
  await Promise.resolve();
  await tick();
  flushSync();
  return button;
}

const failure = () => document.querySelector('[data-copy-failed]');

describe('CopyButton', () => {
  it('says the copy failed when the page has no clipboard at all', async () => {
    clipboard(undefined);
    const button = await press();
    expect(failure()?.textContent).toBe('copy failed: select the command and copy it by hand');
    expect(failure()?.getAttribute('role')).toBe('status');
    expect(button.textContent).toBe('Copy');
  });

  it('says the copy failed when the clipboard refuses the write', async () => {
    clipboard({ writeText: () => Promise.reject(new DOMException('denied', 'NotAllowedError')) });
    await press();
    expect(failure()).not.toBeNull();
  });

  it('flashes Copied, and says nothing of failure, once the clipboard took the text', async () => {
    const taken: string[] = [];
    clipboard({ writeText: async (text: string) => void taken.push(text) });
    const button = await press();
    expect(taken).toEqual(['kew finalize r1']);
    expect(button.textContent).toBe('Copied');
    expect(button.getAttribute('aria-label')).toBe('Copy the command');
    expect(failure()).toBeNull();
  });
});
