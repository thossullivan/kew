/**
 * #3052 N4 — a command is never broken at a hyphen.
 *
 * The browser breaks a line after a hyphen, so a wrapped command read
 * "supervised- / daemon". jsdom does not lay out lines, so this holds what
 * decides them: each word is its own inline-block, capped at the block's
 * width, between the command's own spaces, and nothing scrolls sideways; its
 * text is the command exactly. `command-lines.mjs` reads the lines a real
 * browser draws.
 */
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, describe, expect, it } from 'vitest';
import Command from './Command.svelte';

let open: ReturnType<typeof mount> | null = null;

afterEach(() => {
  if (open) unmount(open);
  open = null;
  document.body.innerHTML = '';
});

function draw(command: string, className = ''): HTMLElement {
  open = mount(Command, { target: document.body, props: { command, class: className } });
  flushSync();
  const code = document.querySelector<HTMLElement>('code[data-command]');
  if (!code) throw new Error('no code[data-command] drawn');
  return code;
}

describe('Command', () => {
  it('wraps only between its words, and never scrolls inside its block', () => {
    const code = draw('kew epic unhold supervised-daemon 2814');
    const classes = code.className.split(' ');
    // Spaces kept as typed, and a line may end at one of them.
    expect(classes).toContain('whitespace-pre-wrap');
    expect(classes).not.toContain('overflow-x-auto');
    expect(classes).not.toContain('whitespace-pre');
    // Each word whole on its line: an inline-block no line can end inside,
    // capped at the block's width, breaking within itself only when wider.
    const words = [...code.children];
    expect(words.map((word) => word.textContent)).toEqual([
      'kew',
      'epic',
      'unhold',
      'supervised-daemon',
      '2814'
    ]);
    for (const word of words) {
      expect(word.tagName).toBe('SPAN');
      expect(word.className.split(' ')).toEqual(
        expect.arrayContaining(['inline-block', 'max-w-full', '[overflow-wrap:anywhere]'])
      );
    }
  });

  it('keeps the spaces between words as typed, in text between the word spans', () => {
    const code = draw('kew epic retry go-live-2026-09 SFD-229  --force');
    const between = [...code.childNodes]
      .filter((node) => node.nodeType === Node.TEXT_NODE)
      .map((node) => node.textContent)
      // Svelte's empty anchor text nodes hold no character, so none of the command.
      .filter((text) => text !== '');
    expect(between).toEqual([' ', ' ', ' ', ' ', '  ']);
  });

  it('is the command exactly, as text, so a select-and-copy is too', () => {
    const command = 'kew epic retry go-live-2026-09 SFD-229  --force';
    expect(draw(command).textContent).toBe(command);
  });

  it('keeps the caller classes beside its own', () => {
    expect(draw('kew status', 'font-mono text-13').className).toContain('font-mono text-13');
  });
});
