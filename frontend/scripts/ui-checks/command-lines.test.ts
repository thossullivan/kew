/**
 * The command-line reading's own test (#3052 N4): it fails when it should,
 * without a browser. A command drawn on one line, or broken only at its
 * spaces, passes; one broken after an id's hyphen fails, as does a block that
 * drew nothing.
 */
import { describe, expect, it } from 'vitest';
import { commandLineFault } from './command-lines.mjs';

const COMMAND = 'kew epic unhold supervised-daemon 2814';

describe('commandLineFault', () => {
  it('passes a command on one line, or broken at its own spaces', () => {
    expect(commandLineFault({ text: COMMAND, lines: [COMMAND] })).toBeNull();
    expect(commandLineFault({ text: COMMAND, lines: ['kew epic unhold', 'supervised-daemon 2814'] })).toBeNull();
  });

  it('passes a line that ends in a word the command itself ends in a hyphen', () => {
    expect(commandLineFault({ text: 'kew run --', lines: ['kew run', '--'] })).toBeNull();
  });

  it('fails a command broken after an id hyphen', () => {
    expect(
      commandLineFault({ text: COMMAND, lines: ['kew epic unhold', 'supervised-', 'daemon 2814'] })
    ).toMatch(/join to/);
    expect(
      commandLineFault({
        text: 'kew epic unhold go-live-2026-09 SFD-229',
        lines: ['kew epic unhold go-live-2026-09 SFD-', '229']
      })
    ).toMatch(/join to/);
  });

  it('passes a break inside a word only when that word is wider than the block', () => {
    const text = 'kew epic unhold go-live-2026-09-supervised-daemon SFD-2814';
    const lines = ['kew epic unhold', 'go-live-2026-09-', 'supervised-daemon', 'SFD-2814'];
    expect(commandLineFault({ text, lines, wide: ['go-live-2026-09-supervised-daemon'] })).toBeNull();
    // The same break with no word too wide for the block is the N4 fault.
    expect(commandLineFault({ text, lines, wide: [] })).toMatch(/join to/);
    // A wide word elsewhere excuses no break in a word that fits.
    expect(
      commandLineFault({
        text,
        lines: ['kew epic unhold', 'go-live-2026-09-supervised-daemon', 'SFD-', '2814'],
        wide: ['go-live-2026-09-supervised-daemon']
      })
    ).toMatch(/join to/);
  });

  it('fails lines that drop or reorder the command even with a wide word', () => {
    const text = 'kew epic unhold go-live-2026-09-supervised-daemon';
    const wide = ['go-live-2026-09-supervised-daemon'];
    expect(commandLineFault({ text, lines: ['kew epic unhold', 'go-live-2026-09-'], wide })).toMatch(/join to/);
    expect(
      commandLineFault({ text, lines: ['kew epic', 'go-live-2026-09-', 'supervised-daemon'], wide })
    ).toMatch(/join to/);
  });

  it('fails a block that drew no line', () => {
    expect(commandLineFault({ text: COMMAND, lines: [] })).toMatch(/drew no line/);
  });
});
