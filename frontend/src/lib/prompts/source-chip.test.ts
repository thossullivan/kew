import { describe, expect, it } from 'vitest';
import type { PromptRole, PromptSource } from '$lib/api';
import prompts from '../../../fixtures/factory/prompts.json';
import { changeLine, sourceChip } from './source-chip';

const SHA = '3f2a1c0d9e8b7a6f5e4d3c2b1a0f9e8d7c6b5a4f3e2d1c0b9a8f7e6d5c4b3a29';

function role(source: Partial<PromptSource>, promptConfig = false): PromptRole {
  return {
    role: 'r',
    run_kinds: ['r'],
    source: { kind: 'code', name: 'n', version: null, sha256: null, pin_mismatch: false, ...source },
    prompt_config_applies: promptConfig,
    profile_applies: false
  };
}

describe('sourceChip', () => {
  it('names the pinned reviewer by version and short sha', () => {
    const chip = sourceChip(role({ kind: 'pinned-artifact', name: 'adversarial-reviewer', version: 9, sha256: SHA }));
    expect(chip).toBe('pinned v9 · 3f2a1c0');
  });

  it('says so when the pinned bytes are not the pin', () => {
    const chip = sourceChip(
      role({ kind: 'pinned-artifact', name: 'adversarial-reviewer', version: 9, sha256: SHA, pin_mismatch: true })
    );
    expect(chip).toBe('pinned v9 · 3f2a1c0 · not the pin');
  });

  it("names a project's deployed copy by short sha, with no version", () => {
    const chip = sourceChip(role({ kind: 'deployed-artifact', name: '.kew/reviewer.md', sha256: SHA }));
    expect(chip).toBe('deployed copy · 3f2a1c0');
  });

  it("calls a template kew.toml's [prompt] configures kew.toml's", () => {
    expect(sourceChip(role({ kind: 'config-template', name: 'prompts/custom.md.j2' }, true))).toBe(
      'kew.toml template'
    );
  });

  it('calls a packaged template built in even where [prompt] could replace it', () => {
    expect(sourceChip(role({ kind: 'packaged-template', name: 'dispatch.md.j2' }, true))).toBe('built-in template');
  });

  it('calls every other packaged template built in', () => {
    expect(sourceChip(role({ kind: 'packaged-template', name: 'triage.md.j2' }))).toBe('built-in template');
  });

  it('calls a prompt a function builds built-in code', () => {
    expect(sourceChip(role({ kind: 'code', name: 'probe_route.PROBE_PROMPT' }))).toBe('built-in code');
  });

  it('reads the committed fixture, served with no [prompt] template: the reviewer pinned, the rest built in', () => {
    const chips = Object.fromEntries((prompts as PromptRole[]).map((r) => [r.role, sourceChip(r)]));
    expect(chips.adversarial_review).toMatch(/^pinned v\d+ · [0-9a-f]{7}$/);
    expect(chips.dispatch).toBe('built-in template');
    expect(chips.triage).toBe('built-in template');
    expect(chips.readiness_probe).toBe('built-in code');
  });
});

describe('changeLine', () => {
  const text = (r: PromptRole) => {
    const line = changeLine(r);
    return `${line.before}\`${line.code}\`${line.after}`;
  };

  it('sends the reviewer to its artifact, through a PR', () => {
    const r = role({ kind: 'pinned-artifact', name: 'adversarial-reviewer', version: 9, sha256: SHA });
    expect(text(r)).toBe('Edit `_skills/adversarial-reviewer/PROMPT.md` through a PR.');
  });

  it('sends dispatch to [prompt] in kew.toml', () => {
    expect(text(role({ kind: 'packaged-template', name: 'dispatch.md.j2' }, true))).toBe(
      'Set `[prompt]` in kew.toml.'
    );
  });

  it('sends a configured template to [prompt] in kew.toml', () => {
    expect(text(role({ kind: 'config-template', name: 'prompts/custom.md.j2' }, true))).toBe(
      'Set `[prompt]` in kew.toml.'
    );
  });

  it('sends every other template to its file, through a PR', () => {
    expect(text(role({ kind: 'packaged-template', name: 'triage.md.j2' }))).toBe(
      'Edit `_templates/triage.md.j2` through a PR.'
    );
  });

  it('sends a built-in builder to its function, through a PR', () => {
    expect(text(role({ kind: 'code', name: 'probe_route.PROBE_PROMPT' }))).toBe(
      'Change `probe_route.PROBE_PROMPT` in kew through a PR.'
    );
  });

  it("sends a deployed copy to the project's own file", () => {
    expect(text(role({ kind: 'deployed-artifact', name: '.kew/reviewer.md', sha256: SHA }))).toBe(
      'Edit `.kew/reviewer.md` in this project through a PR.'
    );
  });
});
