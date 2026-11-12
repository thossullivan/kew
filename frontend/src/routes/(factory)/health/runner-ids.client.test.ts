/**
 * #2952 — Health sets every runner id in the Runners and Images rows in the id
 * role (data-13 mono, `--text-3`) and never cuts it.
 *
 * The browser check (`scripts/ui-checks/health-runner-ids.mjs`) reads where
 * the ink lies at 390, 640 and 1440. This holds the markup without a browser:
 * the id is its own element, in the id role, and nothing between it and its
 * row truncates.
 */
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, describe, expect, it } from 'vitest';
import type { EffectiveConfig, Health, HealthEnv } from '$lib/api';
import healthJson from '../../../../fixtures/factory/health.json';
import envJson from '../../../../fixtures/factory/health-env.json';
import configJson from '../../../../fixtures/factory/config-effective.json';
import HealthPage from './+page.svelte';

const LONG = 'Claude-Code-Enterprise-Sandboxed-For-Restricted-Production';

let open: ReturnType<typeof mount> | null = null;

afterEach(() => {
  if (open) unmount(open);
  open = null;
  document.body.innerHTML = '';
});

function drawHealth(health: Health): HTMLElement {
  const target = document.createElement('div');
  document.body.append(target);
  const data = {
    health,
    env: envJson as unknown as HealthEnv,
    config: configJson as unknown as EffectiveConfig
  };
  open = mount(HealthPage, { target, props: { data } });
  flushSync();
  return target;
}

/** The leaf element in `section#<section>` whose whole text is `id`. */
function idElement(target: HTMLElement, section: string, id: string): HTMLElement | undefined {
  return [...target.querySelectorAll<HTMLElement>(`section#${section} *`)].find(
    (el) => el.childElementCount === 0 && el.textContent?.trim() === id
  );
}

describe('Health runner ids', () => {
  it('sets each served runner and image id whole, in mono --text-3, with no truncating ancestor', () => {
    const health = structuredClone(healthJson) as unknown as Health;
    health.runners = health.runners.map((r, n) => ({ ...r, name: `${LONG}-Runner${n + 1}` }));
    health.images = health.images.map((i, n) => ({ ...i, runner: `${LONG}-Image${n + 1}` }));
    expect(health.runners.length).toBeGreaterThan(0);
    expect(health.images.length).toBeGreaterThan(0);
    const target = drawHealth(health);
    const served: [string, string[]][] = [
      ['runners', health.runners.map((r) => r.name)],
      ['images', health.images.map((i) => i.runner)]
    ];
    for (const [section, ids] of served) {
      const root = target.querySelector(`section#${section}`)!;
      for (const id of ids) {
        const el = idElement(target, section, id);
        expect(el, `${section}: "${id}" is not its own element`).toBeDefined();
        expect([...el!.classList]).toEqual(expect.arrayContaining(['font-mono', 'text-13', 'text-text-3']));
        expect(el!.classList.contains('uppercase')).toBe(false);
        for (let up: HTMLElement | null = el!; up !== null && up !== root; up = up.parentElement) {
          expect(up.classList.contains('truncate'), `${section}: "${id}" sits in a truncating <${up.tagName}>`).toBe(
            false
          );
          expect(up.classList.contains('h-[50px]'), `${section}: "${id}"'s row has a fixed height`).toBe(false);
        }
      }
    }
  });
});
