/**
 * #2845 (P5) — fixture mode answers a dispatch plan per item.
 *
 * One committed `dispatch-resolve.json` answered every item, so the drawer for
 * item 2060 printed `kew dispatch 2302`. Each ready-queue item now has its own
 * plan under `fixtures/factory/dispatch-resolve/`, and a read for an item with
 * none 404s rather than borrowing another item's.
 */
import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import type { Dashboard, DispatchPlan } from './api';
import { fixtureMiddleware, keyedFixtureFileFor } from '../../fixtures/plugin';
import dashboardJson from '../../fixtures/factory/dashboard.json';

const dashboard: Dashboard = dashboardJson as Dashboard;

/** What the middleware answered `url` with: its status and parsed body. */
function serve(url: string): { status: number; body: Record<string, unknown> } {
  let status = 200;
  let text = '';
  const req = { url, method: 'GET', on: () => req };
  const res = {
    set statusCode(code: number) {
      status = code;
    },
    get statusCode() {
      return status;
    },
    setHeader: () => res,
    end(chunk?: string) {
      text += chunk ?? '';
      return res;
    }
  };
  fixtureMiddleware(
    req as unknown as Parameters<typeof fixtureMiddleware>[0],
    res as unknown as Parameters<typeof fixtureMiddleware>[1],
    () => {
      throw new Error(`${url} was passed on, not answered`);
    }
  );
  return { status, body: JSON.parse(text) };
}

describe('the dispatch plan fixture is per item', () => {
  it('names a per-item file only for an item the read names', () => {
    expect(keyedFixtureFileFor('/api/dispatch/resolve?item=2060&lane=implement')).toBe(
      'dispatch-resolve/2060.json'
    );
    expect(keyedFixtureFileFor('/api/dispatch/resolve')).toBeNull();
    expect(keyedFixtureFileFor('/api/dispatch/resolve?item=')).toBeNull();
    expect(keyedFixtureFileFor('/api/dispatch/resolve?item=..%2Fmeta')).toBeNull();
    expect(keyedFixtureFileFor('/api/health?item=2060')).toBeNull();
  });

  it('answers every ready-queue item with a plan for that item', () => {
    const queue = dashboard.glance.ready_queue.map((q) => q.item_id ?? String(q.issue_num));
    expect(queue.length).toBeGreaterThan(1);
    for (const item of queue) {
      const { status, body } = serve(`/api/dispatch/resolve?item=${item}&lane=implement`);
      expect(status, item).toBe(200);
      const plan = body as unknown as DispatchPlan;
      expect(plan.item, item).toBe(item);
      expect(plan.issue_num, item).toBe(Number(item));
      expect(body.command, item).toBe(`kew dispatch ${item}`);
    }
  });

  it("404s an item with no plan of its own instead of serving another item's", () => {
    const { status, body } = serve('/api/dispatch/resolve?item=9999');
    expect(status).toBe(404);
    expect(body.expected).toBe('frontend/fixtures/factory/dispatch-resolve/9999.json');
  });

  it('keeps every per-item plan the shape of a DispatchPlan', () => {
    const base = JSON.parse(
      readFileSync(new URL('../../fixtures/factory/dispatch-resolve.json', import.meta.url), 'utf8')
    ) as Record<string, unknown>;
    for (const item of ['2060', '2058', '2054', '2050']) {
      const plan = serve(`/api/dispatch/resolve?item=${item}`).body;
      expect(Object.keys(plan).sort(), item).toEqual(Object.keys(base).sort());
    }
  });
});
