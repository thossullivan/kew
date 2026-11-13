/**
 * #2542, AC3 in a DOM — the /config Agent model override is the catalog
 * listbox, and choosing Opus 5.5 writes `claude-opus-5-5`.
 *
 * `scripts/ui-checks/config-model-picker.mjs` makes the same reading in a
 * real Chromium; this one runs wherever vitest does, so the write is checked
 * even on a machine with no browser.
 */
import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import overridableJson from '../../fixtures/factory/config-overridable.json';
import modelsJson from '../../fixtures/factory/models.json';
import type { CatalogModel, OverridableMap } from '$lib/api';
import ConfigPage from '../routes/(factory)/config/+page.svelte';

let open: ReturnType<typeof mount> | null = null;

afterEach(() => {
  if (open) unmount(open);
  open = null;
  document.body.innerHTML = '';
  vi.unstubAllGlobals();
});

const settle = async () => {
  for (let i = 0; i < 5; i++) {
    await tick();
    await Promise.resolve();
  }
  flushSync();
};

function render(models: CatalogModel[] | null) {
  const posts: Array<{ url: string; body: { value: unknown } }> = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === 'POST') {
        const body = JSON.parse(String(init.body));
        posts.push({ url, body });
        return new Response(JSON.stringify({ field: 'agent.model', effective: body.value }));
      }
      return new Response('{}', { status: 404 });
    })
  );
  const target = document.createElement('div');
  document.body.appendChild(target);
  open = mount(ConfigPage, {
    target,
    props: { data: { overridable: overridableJson as OverridableMap, models } }
  });
  flushSync();
  return { target, posts };
}

function openAgentModel(target: HTMLElement, label = 'Agent model') {
  const row = [...target.querySelectorAll('li')].find((li) => li.textContent?.includes(label))!;
  [...row.querySelectorAll('button')].find((b) => b.textContent?.includes('Override'))!.click();
  flushSync();
  return { row, dialog: document.querySelector<HTMLElement>('[role="dialog"]')! };
}

describe('/config model control', () => {
  it('is a listbox, and choosing Opus 5.5 writes claude-opus-5-5', async () => {
    const { target, posts } = render((modelsJson as { models: CatalogModel[] }).models);
    const { row, dialog } = openAgentModel(target);
    const listbox = dialog.querySelector('[role="listbox"]');
    expect(listbox).not.toBeNull();
    // Only the reason is still typed.
    expect(dialog.querySelectorAll('input[type="text"]').length).toBe(1);

    const opus = [...listbox!.querySelectorAll<HTMLElement>('[role="option"]')].filter((o) =>
      [...o.querySelectorAll('span')].some((s) => s.textContent === 'Opus 5.5')
    );
    expect(opus.length).toBe(1);
    opus[0].click();
    flushSync();
    [...dialog.querySelectorAll('button')]
      .find((b) => b.textContent?.includes('Confirm override'))!
      .click();
    await settle();

    expect(posts).toEqual([
      { url: '/api/config/agent.model', body: { value: 'claude-opus-5-5', reason: '' } }
    ]);
    expect(row.textContent).toContain('claude-opus-5-5');
  });

  it('writes nothing when the catalog was not read', async () => {
    const { target, posts } = render(null);
    const { dialog } = openAgentModel(target);
    expect(dialog.querySelector('[role="listbox"]')).toBeNull();
    expect(dialog.textContent).toContain('Model catalog not read');
    [...dialog.querySelectorAll('button')]
      .find((b) => b.textContent?.includes('Confirm override'))!
      .click();
    await settle();
    expect(posts).toEqual([]);
    expect(dialog.textContent).toContain('Pick a model from the catalog.');
  });

  it('a routing row picks its model from the listbox and takes that model’s runner', async () => {
    const { target, posts } = render((modelsJson as { models: CatalogModel[] }).models);
    const { dialog } = openAgentModel(target, 'Simple route');
    const listbox = dialog.querySelector('[role="listbox"]')!;
    listbox.querySelector<HTMLElement>('[data-model-id="claude-opus-5-5"]')!.click();
    flushSync();
    [...dialog.querySelectorAll('button')]
      .find((b) => b.textContent?.includes('Confirm override'))!
      .click();
    await settle();
    expect(posts.map((p) => [p.url, p.body.value])).toEqual([
      ['/api/config/routing.simple', { runner: 'claude-code', model: 'claude-opus-5-5' }]
    ]);
  });

  it('a routing row shows the picked model’s runner read-only, not as free text', () => {
    const { target } = render((modelsJson as { models: CatalogModel[] }).models);
    const { dialog } = openAgentModel(target, 'Simple route');
    const runner = () => dialog.querySelector('[data-route-runner]')?.textContent ?? null;
    // Prefilled codex · gpt-5.6-luna: the runner comes from the model row.
    expect(runner()).toContain('codex');
    expect(dialog.querySelectorAll('input[type="text"]').length).toBe(1);

    dialog.querySelector<HTMLElement>('[data-model-id="claude-opus-5-5"]')!.click();
    flushSync();
    expect(runner()).toContain('claude-code');
    expect(dialog.querySelectorAll('input[type="text"]').length).toBe(1);

    // Clearing the model is the way back to a typed, runner-only route.
    [...dialog.querySelectorAll('button')]
      .find((b) => b.textContent?.includes('Clear model'))!
      .click();
    flushSync();
    expect(runner()).toBeNull();
    expect(dialog.querySelectorAll('input[type="text"]').length).toBe(2);
  });
});
