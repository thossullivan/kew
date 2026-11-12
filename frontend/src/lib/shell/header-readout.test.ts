/**
 * The header's readings before its first read lands (design review c4, D35).
 *
 * The desktop header printed a bare `—` for the repo and "• — working" — the
 * cyan working mark over an em dash — on every page load, before the pulse's
 * first read had come back. An em dash is the header's word for "could not be
 * read", so the first frame claimed a failure nobody had had. Until the first
 * read settles the working reading says `reading…` with no mark, and the repo
 * shows nothing; after it, a read that failed still says `—`.
 *
 * Server-rendered, as `pipeline.test.ts` draws its component: the pulse is a
 * module singleton, so the test sets its state and renders the markup.
 */
import { render } from 'svelte/server';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { Dashboard, Run } from '$lib/api';

vi.mock('$app/state', () => ({ page: { url: new URL('http://localhost/work') } }));

const { factoryPulse } = await import('./pulse.svelte');
const { READING, stillReading, UNREADABLE } = await import('./header-state');
const { default: FactoryHeader } = await import('./FactoryHeader.svelte');

const REPO = 'acme/widgets';
const dashboard = {
  banner: {},
  rows: [],
  glance: { pressure: { delivery_repo: REPO } }
} as unknown as Dashboard;
const working = { status: 'running' } as unknown as Run;

/** The rendered header as text, tags dropped and whitespace folded. */
function drawn(): { html: string; text: string } {
  const { body } = render(FactoryHeader);
  return { html: body, text: body.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ') };
}

afterEach(() => {
  factoryPulse.dashboard = null;
  factoryPulse.runs = null;
  factoryPulse.hasRead = false;
});

describe('the header readout before the first read', () => {
  it('says reading… and draws no working mark or bare dash', () => {
    factoryPulse.hasRead = false;
    const { html, text } = drawn();
    expect(READING).toBe('reading…');
    expect(text).toContain(READING);
    expect(text).not.toMatch(new RegExp(`${UNREADABLE} working`));
    expect(html).not.toContain('aria-label="working"');
    // The repo slot holds its place but shows nothing.
    expect(html).toMatch(/class="[^"]*\binvisible\b[^"]*"[^>]*>—</);
  });

  it('prints the readings once the first read lands', () => {
    factoryPulse.dashboard = dashboard;
    factoryPulse.runs = [working, working];
    factoryPulse.hasRead = true;
    const { html, text } = drawn();
    expect(text).not.toContain(READING);
    expect(text).toContain('2 working');
    expect(html).toContain('aria-label="working"');
    expect(text).toContain(REPO);
    expect(html).not.toContain('invisible');
  });

  it('keeps the em dash for a read that settled and failed', () => {
    factoryPulse.hasRead = true;
    const { text } = drawn();
    expect(text).not.toContain(READING);
    expect(text).toContain(`${UNREADABLE} working`);
  });
});

describe('stillReading', () => {
  it('is true only for an unread value before any read has settled', () => {
    expect(stillReading(null, false)).toBe(true);
    expect(stillReading(null, true)).toBe(false);
    expect(stillReading(0, false)).toBe(false);
    expect(stillReading(REPO, false)).toBe(false);
  });
});
