/**
 * FUI-03 — the Factory UI token set in `app.css`.
 *
 * `app.css` is the system of record for kew's palette: `kew/tui/palette.py`
 * transcribes it for the terminal and `www/scripts/sync-tokens.mjs` lifts its
 * dark block into the marketing site. So the factory tokens have to be purely
 * additive — new names in both theme blocks, every pre-existing value left
 * exactly where it was. These tests pin that, the contrast the ramp promises,
 * and the colours the DMonitor artboard actually paints.
 */
import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

const CSS = readFileSync(new URL('../app.css', import.meta.url), 'utf8');
const DMONITOR = readFileSync(
  new URL('../../design/factory-ui/DMonitor.html', import.meta.url),
  'utf8'
);

/** Split app.css the way the TUI parity test does: dark, light, `@theme inline`. */
function blocks(): { dark: string; light: string; theme: string } {
  const lightAt = CSS.indexOf("[data-theme='light']");
  const themeAt = CSS.indexOf('@theme inline', lightAt);
  expect(lightAt, "[data-theme='light'] block").toBeGreaterThan(-1);
  expect(themeAt, '@theme inline block').toBeGreaterThan(-1);
  const themeEnd = CSS.indexOf('\n}', themeAt);
  return {
    dark: CSS.slice(0, lightAt),
    light: CSS.slice(lightAt, themeAt),
    theme: CSS.slice(themeAt, themeEnd)
  };
}

/** Every `--name: value` declaration in a block, comments stripped, lowercased. */
function declarations(block: string): Map<string, string> {
  const body = block.replace(/\/\*[\s\S]*?\*\//g, '');
  const found = new Map<string, string>();
  for (const [, name, value] of body.matchAll(/--([a-z0-9-]+):\s*([^;]+);/g)) {
    found.set(name, value.trim().toLowerCase().replace(/\s+/g, ' '));
  }
  return found;
}

const { dark, light, theme } = blocks();
const DARK = declarations(dark);
const LIGHT = declarations(light);
const THEME = declarations(theme);

/** The factory ground, hairlines, text ramp, edges and halos (direction D). */
const FACTORY_TOKENS: Record<string, string> = {
  ink: '#0a0714',
  'ink-raised': '#0d0a18',
  'ink-selected': '#151025',
  'ink-track': '#1d1730',
  hair: '#221c33',
  'hair-quiet': '#1a1528',
  'hair-strong': '#342a4a',
  'hair-box': '#2a2240',
  guide: '#16111f',
  'text-1': '#ebe6f2',
  'text-2': '#b5acc8',
  'text-3': '#8d84a3',
  'text-4': '#6a6082',
  'text-5': '#5a5070',
  'edge-done': '#3a3052',
  'halo-cyan': 'rgba(5, 217, 232, 0.16)',
  'halo-magenta': 'rgba(255, 42, 109, 0.16)'
};

/**
 * Every token that existed before FUI-03, frozen. The TUI parity test would
 * catch a changed value too, but only when the Python suite runs; this keeps
 * the "additive" promise inside the frontend gate.
 */
const PRE_EXISTING_DARK: Record<string, string> = {
  bg: '#0d0221',
  'bg-glow': '#1a0b2e',
  surface: '#1a0b2e',
  'surface-2': '#2d1b4e',
  'surface-3': '#0d0221',
  border: '#2d1b4e',
  'border-strong': '#533a73',
  fg: '#e0d8ea',
  'fg-dim': '#b8aad0',
  muted: '#8b7fa8',
  faint: '#6c5b7b',
  accent: '#ff2a6d',
  'accent-bright': '#ff5c8f',
  'accent-dim': '#9c2148',
  'accent-fg': '#0d0221',
  'accent-2': '#05d9e8',
  'accent-2-fg': '#0d0221',
  success: '#2ee6a6',
  warn: '#ffc94a',
  danger: '#ff4365',
  info: '#05d9e8',
  'status-blocked': '#b48ade',
  'status-cancelled': '#6c5b7b'
};

const PRE_EXISTING_LIGHT: Record<string, string> = {
  bg: '#f4f0fb',
  'bg-glow': '#fbe6f1',
  surface: '#fcfaff',
  'surface-2': '#f1ebf9',
  'surface-3': '#ece3f7',
  border: '#e2d7f0',
  'border-strong': '#c9b8e0',
  fg: '#1a0b2e',
  'fg-dim': '#4a3768',
  muted: '#6c5b7b',
  faint: '#9a8db5',
  accent: '#c81e5a',
  'accent-bright': '#e0246a',
  'accent-dim': '#e59ab6',
  'accent-fg': '#fff5f9',
  'accent-2': '#0892a5',
  'accent-2-fg': '#f2fdff',
  success: '#128a5e',
  warn: '#b5790e',
  danger: '#c81e3c',
  info: '#0892a5',
  'status-blocked': '#7a4fa3',
  'status-cancelled': '#9a8db5'
};

function channel(component: number): number {
  const c = component / 255;
  return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
}

function luminance(hex: string): number {
  const n = Number.parseInt(hex.slice(1), 16);
  return (
    0.2126 * channel((n >> 16) & 255) +
    0.7152 * channel((n >> 8) & 255) +
    0.0722 * channel(n & 255)
  );
}

/** WCAG 2.x relative-contrast ratio, 1:1 to 21:1. */
function contrast(a: string, b: string): number {
  const [x, y] = [luminance(a), luminance(b)];
  return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05);
}

describe('factory tokens', () => {
  it.each(Object.entries(FACTORY_TOKENS))(
    '--%s carries the artboard value in dark',
    (name, value) => {
      expect(DARK.get(name)).toBe(value);
    }
  );

  it.each(Object.keys(FACTORY_TOKENS))('--%s has a light value too', (name) => {
    const value = LIGHT.get(name);
    expect(value, `--${name} missing from the light block`).toBeDefined();
    expect(value).toMatch(/^(#[0-9a-f]{6}|rgba\()/);
  });

  it.each(Object.keys(FACTORY_TOKENS))('--%s is exposed to Tailwind', (name) => {
    expect(THEME.get(`color-${name}`)).toBe(`var(--${name})`);
  });
});

describe('the token set is additive', () => {
  it('leaves every pre-existing dark value alone', () => {
    for (const [name, value] of Object.entries(PRE_EXISTING_DARK)) {
      expect(DARK.get(name), `--${name}`).toBe(value);
    }
  });

  it('leaves every pre-existing light value alone', () => {
    for (const [name, value] of Object.entries(PRE_EXISTING_LIGHT)) {
      expect(LIGHT.get(name), `--${name}`).toBe(value);
    }
  });

  it('adds nothing to a theme block that the other one lacks', () => {
    expect([...DARK.keys()].filter((k) => !LIGHT.has(k) && k !== 'color-scheme')).toEqual([]);
    expect([...LIGHT.keys()].filter((k) => !DARK.has(k) && k !== 'color-scheme')).toEqual([]);
  });
});

describe('the text ramp is legible on its ground', () => {
  const themes = [
    ['dark', DARK],
    ['light', LIGHT]
  ] as const;

  it.each(
    themes.flatMap(([label, tokens]) =>
      (['text-1', 'text-2', 'text-3'] as const).map((name) => [label, tokens, name] as const)
    )
  )('%s --%s clears AA on --ink', (_label, tokens, name) => {
    expect(contrast(tokens.get(name)!, tokens.get('ink')!)).toBeGreaterThanOrEqual(4.5);
  });

  it.each(themes)('%s --text-4 is flagged non-text because it fails AA', (_label, tokens) => {
    expect(contrast(tokens.get('text-4')!, tokens.get('ink')!)).toBeLessThan(4.5);
  });

  it('says so in app.css, right where --text-4 is declared', () => {
    const preamble = dark.slice(0, dark.indexOf('--text-4:'));
    const comment = preamble.slice(preamble.lastIndexOf('/*'));
    expect(comment).toMatch(/non-text/i);
  });

  it('orders the ramp from loudest to quietest', () => {
    const ratios = (['text-1', 'text-2', 'text-3', 'text-4', 'text-5'] as const).map((name) =>
      contrast(DARK.get(name)!, DARK.get('ink')!)
    );
    expect(ratios).toEqual([...ratios].sort((a, b) => b - a));
  });
});

describe('the DMonitor readouts specimen', () => {
  /** The readout strip, x 40–1400 / y 88–258 in the 1440×960 artboard. */
  const readouts = DMONITOR.slice(
    DMONITOR.indexOf('<section aria-label="Readouts"'),
    DMONITOR.indexOf('<section aria-label="Live runs"')
  );

  it('is a real region of the artboard', () => {
    expect(readouts).toContain('Needs you');
    expect(readouts).toContain('prior cap $600');
  });

  it('paints only colours the token set defines', () => {
    const known = new Set(
      [...Object.values(FACTORY_TOKENS), ...Object.values(PRE_EXISTING_DARK)].map((v) =>
        v.toLowerCase()
      )
    );
    const used = new Set([...readouts.matchAll(/#[0-9a-f]{6}/gi)].map((m) => m[0].toLowerCase()));
    expect([...used].filter((hex) => !known.has(hex))).toEqual([]);
  });
});
