import { fetchPrompts, type PromptRole } from '$lib/api';
import { readFailure } from '$lib/work/evidence';

export const ssr = false;

export type PromptsPageData = {
  roles: PromptRole[] | null;
  /** Why `roles` is null, as a sentence the page prints in place of the list. */
  failure: string | null;
};

/**
 * The role list `GET /api/prompts` serves (#3019). A failed read is a sentence,
 * never a blank page: the page prints `failure` where the list and the pane
 * would be. The selected role's preview is read by the page, not here, so a
 * change of role is a shallow `replaceState` that never re-runs this.
 */
export async function load({ fetch }: { fetch: typeof globalThis.fetch }): Promise<PromptsPageData> {
  try {
    return { roles: await fetchPrompts(fetch), failure: null };
  } catch (err) {
    return { roles: null, failure: readFailure(err, 'The prompt catalog') };
  }
}
