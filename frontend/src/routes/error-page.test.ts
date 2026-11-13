/**
 * The root error page says where to go, and never sends a reader back into
 * the page that failed (design review c9 X7).
 */
import { render } from 'svelte/server';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const state = vi.hoisted(() => ({
  page: { status: 404, url: new URL('http://kew.test/nope'), error: null as { message: string } | null }
}));

vi.mock('$app/state', () => ({
  get page() {
    return state.page;
  }
}));

const { default: ErrorPage } = await import('./+error.svelte');

const main = (html: string) => html.slice(html.indexOf('data-error-page'));

beforeEach(() => {
  state.page = { status: 404, url: new URL('http://kew.test/nope'), error: null };
});

describe('the root error page', () => {
  it('sends an unknown address to Work', () => {
    const html = main(render(ErrorPage).body);
    expect(html).toContain('That page does not exist.');
    expect(html).toMatch(/<a href="\/work"[^>]*>Go to Work\.<\/a>/);
    expect(html).not.toContain('data-error-retry');
  });

  it('offers Try again, not Go to Work, when Work itself failed', () => {
    state.page = { status: 500, url: new URL('http://kew.test/work?item=7'), error: { message: 'the read failed' } };
    const html = main(render(ErrorPage).body);
    expect(html).toContain('This page could not be drawn.');
    expect(html).toContain('data-error-retry');
    expect(html).toContain('Try again.');
    expect(html).not.toContain('Go to Work.');
    expect(html).toContain('the read failed');
  });

  it('sends any other failed page to Work', () => {
    state.page = { status: 500, url: new URL('http://kew.test/monitor'), error: { message: 'x' } };
    const html = main(render(ErrorPage).body);
    expect(html).toContain('Go to Work.');
    expect(html).not.toContain('data-error-retry');
  });
});
