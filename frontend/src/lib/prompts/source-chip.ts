/**
 * The Prompts page's words for where a role's base prompt comes from (#3019).
 *
 * `GET /api/prompts` (#3018, `kew.service.prompts`) serves each role's source
 * as a kind and a name; this turns that into the chip on the role's row and the
 * one line above its preview that says how the base is changed. Pure, so the
 * chip text for every source kind is pinned by a unit test rather than read off
 * a screenshot.
 */
import type { PromptRole, PromptSourceKind } from '$lib/api';

/** How many hex digits of a sha256 the chip shows, as `git` abbreviates. */
export const SHORT_SHA = 7;

const short = (sha: string | null): string | null => (sha ? sha.slice(0, SHORT_SHA) : null);

/**
 * The source chip: `pinned v9 · 3f2a1c0` for the pinned reviewer artifact,
 * `deployed copy · 3f2a1c0` when the project deploys its own, `kew.toml
 * template` only when kew.toml's `[prompt]` configures a template in effect,
 * `built-in template` for every packaged template — `[prompt]` without a
 * template leaves the packaged one in effect — and `built-in code` for a
 * prompt a function builds.
 */
export function sourceChip(role: Pick<PromptRole, 'source' | 'prompt_config_applies'>): string {
  const { source } = role;
  const sha = short(source.sha256);
  switch (source.kind) {
    case 'pinned-artifact': {
      const parts = [source.version == null ? 'pinned' : `pinned v${source.version}`];
      if (sha) parts.push(sha);
      if (source.pin_mismatch) parts.push('not the pin');
      return parts.join(' · ');
    }
    case 'deployed-artifact':
      return sha ? `deployed copy · ${sha}` : 'deployed copy';
    case 'config-template':
      return 'kew.toml template';
    case 'packaged-template':
      return 'built-in template';
    case 'code':
      return 'built-in code';
  }
}

/** Where the packaged files live under `kew/src/kew/`, as a PR edits them. */
const PACKAGED_DIR: Partial<Record<PromptSourceKind, string>> = {
  'pinned-artifact': '_skills',
  'packaged-template': '_templates'
};

/**
 * One line on how the role's base is changed. `code` is the path to put in
 * backticks; the sentence is `before` + code + `after`.
 */
export type ChangeLine = { before: string; code: string; after: string };

export function changeLine(role: Pick<PromptRole, 'source' | 'prompt_config_applies'>): ChangeLine {
  const { source } = role;
  if (role.prompt_config_applies) return { before: 'Set ', code: '[prompt]', after: ' in kew.toml.' };
  switch (source.kind) {
    case 'pinned-artifact':
      return { before: 'Edit ', code: `${PACKAGED_DIR[source.kind]}/${source.name}/PROMPT.md`, after: ' through a PR.' };
    case 'deployed-artifact':
      return { before: 'Edit ', code: source.name, after: ' in this project through a PR.' };
    case 'config-template':
      return { before: 'Edit ', code: source.name, after: ' in this project.' };
    case 'packaged-template':
      return { before: 'Edit ', code: `${PACKAGED_DIR[source.kind]}/${source.name}`, after: ' through a PR.' };
    case 'code':
      return { before: 'Change ', code: source.name, after: ' in kew through a PR.' };
  }
}
