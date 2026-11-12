/**
 * #2536 — `why ▸` opens the text it names, in a real DOM.
 *
 * The server-rendered tests hold the closed state (the text is in the document,
 * `hidden`, named by `aria-controls`). This holds the click: the fold opens on
 * the first press and closes on the second, on the Needs-you block and on the
 * Health page — whose folds bind into a keyed record that starts empty, the
 * shape Svelte refuses at mount if the binding has no fallback of its own.
 */
import { flushSync, mount, unmount, type Component } from 'svelte';
import { afterEach, describe, expect, it } from 'vitest';
import type { EffectiveConfig, Health, HealthEnv } from '$lib/api';
import type { NeedsYouBlock } from '$lib/needs-you/block';
import healthJson from '../../../fixtures/factory/health.json';
import envJson from '../../../fixtures/factory/health-env.json';
import configJson from '../../../fixtures/factory/config-effective.json';
import NeedsYouBlockView from '$lib/needs-you/NeedsYouBlock.svelte';
import HealthPage from '../../routes/(factory)/health/+page.svelte';

let open: ReturnType<typeof mount> | null = null;

afterEach(() => {
  if (open) unmount(open);
  open = null;
  document.body.innerHTML = '';
});

// Generic over the component's own props: Svelte 5 types refuse a component
// with required props where `Record<string, unknown>` is asked for.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
function draw<P extends Record<string, any>>(component: Component<P>, props: P): HTMLElement {
  const target = document.createElement('div');
  document.body.append(target);
  open = mount(component, { target, props });
  flushSync();
  return target;
}

/** Press the toggle that controls `id`, and report whether its text shows. */
function press(target: HTMLElement, id: string): boolean {
  const toggle = target.querySelector<HTMLButtonElement>(`button[aria-controls="${id}"]`)!;
  toggle.click();
  flushSync();
  const shown = !target.querySelector<HTMLElement>(`#${CSS.escape(id)}`)!.hidden;
  expect(toggle.getAttribute('aria-expanded')).toBe(String(shown));
  return shown;
}

describe('why ▸', () => {
  it('opens and closes the Needs-you block`s why, since and note', () => {
    const block: NeedsYouBlock = {
      label: 'Held',
      summary: 'held for a decision',
      why: 'the review round cap was reached',
      since: 'held 2h13m',
      command: 'kew retry 2055',
      note: 'starts a fresh attempt',
      links: [],
      external: []
    } as NeedsYouBlock;
    const target = draw(NeedsYouBlockView, { block });
    const fold = target.querySelector<HTMLElement>('dl[id^="needs-you-why-"]')!;
    expect(fold.hidden).toBe(true);
    expect(fold.textContent).toContain('the review round cap was reached');
    expect(press(target, fold.id)).toBe(true);
    expect(press(target, fold.id)).toBe(false);
  });

  it('opens a Health posture row`s refusal, and the missing review policy`s reason', () => {
    const health = structuredClone(healthJson) as unknown as Health;
    health.posture[0].refusal = 'image_unmapped';
    const config = { ...(configJson as unknown as EffectiveConfig), human_review_unavailable: 'no store' };
    const target = draw(HealthPage, { data: { health, env: envJson as unknown as HealthEnv, config } });
    const lane = `posture-why-${health.posture[0].lane}`;
    expect(target.querySelector(`#${lane}`)!.textContent).toContain('image_unmapped');
    // A clean lane has nothing to fold, so it draws no toggle.
    expect(target.querySelector(`button[aria-controls="posture-why-${health.posture[1].lane}"]`)).toBeNull();
    expect(press(target, lane)).toBe(true);
    expect(press(target, lane)).toBe(false);
    expect(press(target, 'human-review-why')).toBe(true);
  });
});
