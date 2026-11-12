/**
 * #2542, AC2 — the picker in a DOM: whatever is clicked or pressed, the only
 * ids that reach `onpick` are ones its own list holds.
 */
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import modelsJson from '../../fixtures/factory/models.json';
import type { CatalogModel } from '$lib/api';

const { default: ModelPicker } = await import('./ModelPicker.svelte');

const catalog = (modelsJson as { models: CatalogModel[] }).models;
let mounted: Array<Record<string, unknown>> = [];

afterEach(() => {
  for (const app of mounted) unmount(app);
  mounted = [];
  document.body.innerHTML = '';
});

function render(props: {
  models: CatalogModel[];
  value?: string;
  current?: string;
  disabled?: boolean;
}) {
  const onpick = vi.fn();
  const target = document.createElement('div');
  document.body.appendChild(target);
  mounted.push(mount(ModelPicker, { target, props: { ...props, onpick } }));
  flushSync();
  const listbox = target.querySelector<HTMLElement>('[role="listbox"]');
  return { onpick, target, listbox };
}

const key = (el: HTMLElement, k: string) => {
  el.dispatchEvent(new KeyboardEvent('keydown', { key: k, bubbles: true }));
  flushSync();
};

describe('ModelPicker', () => {
  it('is a listbox of the catalog, grouped by runner', () => {
    const { listbox } = render({ models: catalog });
    expect(listbox).not.toBeNull();
    const options = [...listbox!.querySelectorAll<HTMLElement>('[role="option"]')];
    expect(options.map((o) => o.dataset.modelId)).toEqual(
      [...new Set(catalog.map((m) => m.runner))].flatMap((r) =>
        catalog.filter((m) => m.runner === r).map((m) => m.id)
      )
    );
    expect(listbox!.querySelectorAll('[role="group"]').length).toBe(
      new Set(catalog.map((m) => m.runner)).size
    );
  });

  it('emits only listed ids, by click and by keyboard', () => {
    const { onpick, listbox } = render({ models: catalog, value: 'claude-sonnet-5-5' });
    const opus = listbox!.querySelector<HTMLElement>('[data-model-id="claude-opus-5-5"]')!;
    opus.click();
    flushSync();
    for (const k of ['ArrowDown', 'ArrowDown', 'Enter', 'End', ' ', 'Home', 'Enter', 'ArrowUp', 'Enter']) {
      key(listbox!, k);
    }
    const ids = new Set(catalog.map((m) => m.id));
    expect(onpick.mock.calls.length).toBeGreaterThan(3);
    for (const [id, model] of onpick.mock.calls) {
      expect(ids.has(id), id).toBe(true);
      expect(model.id).toBe(id);
    }
    expect(onpick.mock.calls[0][0]).toBe('claude-opus-5-5');
  });

  it('cannot emit an id planted in the DOM that is not in its list', () => {
    const { onpick, listbox } = render({ models: catalog });
    const opus = listbox!.querySelector<HTMLElement>('[data-model-id="claude-opus-5-5"]')!;
    opus.dataset.modelId = 'evil-model';
    opus.id = 'evil-model';
    opus.click();
    key(listbox!, 'Enter');
    for (const [id] of onpick.mock.calls) expect(id).not.toBe('evil-model');
  });

  it('hides a retired model unless it is the configured value, and flags it then', () => {
    const retired: CatalogModel = {
      ...catalog[0],
      id: 'claude-opus-3',
      display_name: 'Opus 4',
      lifecycle: { status: 'retired', shutdown: '2026-01-05', replacement: 'claude-opus-5-5' },
      configured: true
    };
    const hidden = render({ models: [...catalog, retired], value: 'claude-opus-5-5' });
    expect(hidden.listbox!.querySelector('[data-model-id="claude-opus-3"]')).toBeNull();
    const shown = render({ models: [...catalog, retired], value: 'claude-opus-3' });
    const row = shown.listbox!.querySelector<HTMLElement>('[data-model-id="claude-opus-3"]');
    expect(row?.textContent).toContain('retired — still configured');
    expect(row?.getAttribute('aria-selected')).toBe('true');
  });

  it('marks the model in force, and only it, whatever is picked since (#2846)', () => {
    const { listbox } = render({
      models: catalog,
      value: 'claude-opus-5-5',
      current: 'claude-sonnet-5-5'
    });
    const marked = [...listbox!.querySelectorAll<HTMLElement>('[data-current]')];
    expect(marked.map((o) => o.dataset.modelId)).toEqual(['claude-sonnet-5-5']);
    expect(marked[0].textContent).toContain('✓ current');
    expect(listbox!.textContent!.match(/current/g)).toHaveLength(1);
    const picked = listbox!.querySelector('[data-model-id="claude-opus-5-5"]');
    expect(picked?.getAttribute('aria-selected')).toBe('true');
    expect(picked?.textContent).not.toContain('current');
  });

  it('marks nothing when no model is in force', () => {
    const { listbox } = render({ models: catalog, value: 'claude-opus-5-5' });
    expect(listbox!.querySelectorAll('[data-current]')).toHaveLength(0);
  });

  it('emits nothing while disabled', () => {
    const { onpick, listbox } = render({ models: catalog, disabled: true });
    listbox!.querySelector<HTMLElement>('[role="option"]')!.click();
    key(listbox!, 'Enter');
    expect(onpick).not.toHaveBeenCalled();
  });
});
