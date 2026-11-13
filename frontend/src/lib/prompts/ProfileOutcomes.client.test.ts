/**
 * The outcomes table beside the versions the editor shows (#3090): it is
 * drawn only from the generation of that history, and a table from another
 * generation is refused and read again, once per answer. Mounted with `fetch`
 * stubbed at the network boundary.
 */
import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import scenarioJson from '../../../fixtures/scenarios/prompts-outcomes.json';
import type { PromptOutcomes } from '$lib/api';
import { OUTCOMES_REREAD } from './outcomes';

const { default: ProfileOutcomes } = await import('./ProfileOutcomes.svelte');

/** The dispatch table after v1's approval: generation 1. */
const AFTER = (scenarioJson as unknown as { approval: { after: PromptOutcomes } }).approval.after;

let mounted: Array<Record<string, unknown>> = [];

afterEach(() => {
  for (const app of mounted) unmount(app);
  mounted = [];
  document.body.innerHTML = '';
  vi.unstubAllGlobals();
});

async function render(answer: unknown, historyGeneration: number | null | undefined) {
  const fetch = vi.fn(async () => new Response(JSON.stringify(answer), { status: 200 }));
  vi.stubGlobal('fetch', fetch);
  const onstale = vi.fn();
  const target = document.createElement('div');
  document.body.appendChild(target);
  mounted.push(mount(ProfileOutcomes, { target, props: { role: 'dispatch', historyGeneration, onstale } }));
  flushSync();
  // The read lands, then the effects it wakes run.
  await vi.waitFor(() => expect(fetch).toHaveBeenCalled());
  for (let i = 0; i < 5; i++) {
    await new Promise((resolve) => setTimeout(resolve, 0));
    await tick();
  }
  flushSync();
  const meta = target.querySelector('[data-profile-outcomes] > :first-child')?.textContent ?? '';
  return { target, onstale, fetch, meta: meta.replace(/\s+/g, ' ').trim() };
}

describe('ProfileOutcomes beside the versions read', () => {
  it('draws the table when it names the history generation', async () => {
    const { target, onstale, meta } = await render(AFTER, 1);
    const rows = [...target.querySelectorAll('[data-outcome-row]')].map((r) => r.getAttribute('data-outcome-row'));
    expect(rows).toEqual(AFTER.versions.map((v) => v.label));
    expect(meta).toContain(String(AFTER.versions.length));
    expect(onstale).not.toHaveBeenCalled();
  });

  it('refuses a table a generation ahead of the history and asks for one re-read', async () => {
    // The versions read landed at 0 (v1 proposed); the outcomes read at 1.
    const { target, onstale, meta } = await render(AFTER, 0);
    expect(target.querySelector('[data-outcomes-table]')).toBeNull();
    expect(target.querySelector('[data-outcomes-reread]')?.textContent?.trim()).toBe(OUTCOMES_REREAD);
    expect(meta).not.toMatch(/\d/);
    expect(onstale).toHaveBeenCalledTimes(1);
  });

  it('waits, drawing nothing, while the history is still being read', async () => {
    const { target, onstale } = await render(AFTER, undefined);
    expect(target.querySelector('[data-outcomes-table]')).toBeNull();
    expect(onstale).not.toHaveBeenCalled();
  });

  it('shows no version count beside a table no generation names', async () => {
    const { target, meta } = await render({ ...AFTER, generation: null }, 1);
    expect(target.querySelector('[data-outcomes-failure]')).not.toBeNull();
    expect(target.querySelector('[data-outcomes-table]')).toBeNull();
    expect(meta).not.toMatch(/\d/);
  });
});
