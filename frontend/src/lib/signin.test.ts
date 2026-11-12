import { describe, expect, it } from 'vitest';
import { SIGNIN_COMMANDS, SIGNIN_HEADING, SIGNIN_BODY } from './signin';

describe('sign-in teaching copy', () => {
  it('teaches how to get the link and how to rotate it', () => {
    const cmds = SIGNIN_COMMANDS.map((c) => c.cmd);
    expect(cmds).toContain('kew serve');
    expect(cmds).toContain('kew serve --new-token');
  });

  it('gives every command a description', () => {
    for (const c of SIGNIN_COMMANDS) {
      expect(c.cmd.length).toBeGreaterThan(0);
      expect(c.desc.length).toBeGreaterThan(0);
    }
  });

  it('keeps the "not signed in" wording and a non-empty body', () => {
    expect(SIGNIN_HEADING.toLowerCase()).toContain('signed in');
    expect(SIGNIN_BODY.length).toBeGreaterThan(0);
  });
});
