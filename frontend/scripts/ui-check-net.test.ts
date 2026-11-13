/**
 * A route handler that throws fails its own check, and the run goes on (#3477).
 *
 * Playwright calls a `page.route` handler from an event listener and never
 * catches its promise, so a `route.fetch()` the dev server reset was an
 * unhandled rejection, and Node's default ended the whole `npm run ui-check`
 * with no summary. The recorder tests run in a child `node`: vitest installs
 * its own rejection handler, so only a real process shows whether Node exits.
 */
import { spawnSync } from 'node:child_process';
import { describe, expect, it } from 'vitest';
import { checksDirFrom, CHECKS_DIR, fetchThrough, settleThrownRoutes } from './ui-check-lib.mjs';

const LIB = new URL('./ui-check-lib.mjs', import.meta.url).href;

type Result = { name: string; ok: boolean };

/** Run `body` in a child node with `recorder` built from the lib; it prints the results as JSON. */
function child(
  body: string,
  options = '{ log: () => {} }'
): { status: number | null; results: Result[]; stdout: string; stderr: string } {
  const script = [
    `const { checkRecorder } = await import(${JSON.stringify(LIB)});`,
    `const recorder = checkRecorder(${options});`,
    'const tick = () => new Promise((resolve) => setTimeout(resolve, 20));',
    body,
    'console.log(JSON.stringify(recorder.results));'
  ].join('\n');
  const run = spawnSync(process.execPath, ['--input-type=module', '-e', script], { encoding: 'utf8' });
  const last = run.stdout.trim().split('\n').at(-1) || '[]';
  return { status: run.status, results: JSON.parse(last) as Result[], stdout: run.stdout, stderr: run.stderr };
}

describe('the recorder turns a rejection no one awaits into a failing result', () => {
  it('a rejection no one awaits fails the check it happened in, and the next check still runs', () => {
    const run = child(`
      await recorder.check('first', async () => {
        Promise.reject(new Error('read ECONNRESET'));
        await tick();
      });
      await recorder.check('second', async () => {});
    `);
    expect(run.stderr).toBe('');
    expect(run.status).toBe(0);
    expect(run.results.map((result) => [result.name, result.ok])).toEqual([
      ['first', false],
      ['second', true]
    ]);
    expect((run.results[0] as Result & { message?: string }).message).toContain(
      'a route or page handler threw: read ECONNRESET'
    );
  }, 30_000);

  it('a rejection between checks is a failing result, not an exit', () => {
    const run = child(`
      await recorder.check('first', async () => {});
      Promise.reject(new Error('read ECONNRESET'));
      await tick();
      await recorder.check('second', async () => {});
    `);
    expect(run.status).toBe(0);
    expect(run.results.map((result) => [result.name, result.ok])).toEqual([
      ['first', true],
      ['ui-check: an error outside any check', false],
      ['second', true]
    ]);
  }, 30_000);

  it('a rejection while overlapping checks run fails every running check, never one that has ended', () => {
    // A and B start, A ends, C starts, B ends, then the rejection lands during C.
    const run = child(`
      const gate = () => { let open; const shut = new Promise((resolve) => { open = resolve; }); return { shut, open }; };
      const a = gate(), b = gate(), c = gate();
      const pool = [
        recorder.check('A', () => a.shut),
        recorder.check('B', () => b.shut)
      ];
      a.open(); await pool[0];
      pool.push(recorder.check('C', () => c.shut));
      b.open(); await pool[1];
      Promise.reject(new Error('read ECONNRESET'));
      await tick();
      c.open(); await pool[2];
    `);
    expect(run.status).toBe(0);
    expect(run.results.map((result) => [result.name, result.ok])).toEqual([
      ['A', true],
      ['B', true],
      ['C', false]
    ]);
  }, 30_000);

  it('a rejection while two checks run fails both and names the other', () => {
    const run = child(`
      let open;
      const shut = new Promise((resolve) => { open = resolve; });
      const pool = [recorder.check('A', () => shut), recorder.check('B', () => shut)];
      Promise.reject(new Error('read ECONNRESET'));
      await tick();
      open(); await Promise.all(pool);
    `);
    expect(run.status).toBe(0);
    const results = run.results as Array<Result & { message?: string }>;
    expect(results.map((result) => [result.name, result.ok])).toEqual([
      ['A', false],
      ['B', false]
    ]);
    expect(results[0].message).toContain('read ECONNRESET (while B also ran)');
    expect(results[1].message).toContain('read ECONNRESET (while A also ran)');
  }, 30_000);
});

describe('a check that never settles fails at its deadline', () => {
  it('a body that never settles fails its check at the deadline, and the next check still runs', () => {
    const run = child(
      `
      await recorder.check('waits forever', () => new Promise(() => {}));
      await recorder.check('second', async () => {});
    `,
      '{ log: () => {}, deadlineMs: 200 }'
    );
    expect(run.stderr).toBe('');
    expect(run.status).toBe(0);
    const results = run.results as Array<Result & { message?: string }>;
    expect(results.map((result) => [result.name, result.ok])).toEqual([
      ['waits forever', false],
      ['second', true]
    ]);
    expect(results[0].message).toBe('the check did not finish within 200 ms');
  }, 30_000);
});

/** A stand-in for Playwright's `route`: `fetch` answers from `answers` in order. */
function fakeRoute(answers: Array<Error | { status: number }>) {
  const route = {
    tries: 0,
    aborted: false,
    request: () => ({ url: () => 'http://127.0.0.1:5173/api/runs/f5dcb6a5' }),
    fetch: async () => {
      const answer = answers[Math.min(route.tries, answers.length - 1)];
      route.tries += 1;
      if (answer instanceof Error) throw answer;
      return answer;
    },
    abort: async () => {
      route.aborted = true;
    }
  };
  return route;
}

describe('fetchThrough', () => {
  it('fetchThrough tries a reset fetch again and returns the answer', async () => {
    const answer = { status: 200 };
    const route = fakeRoute([new Error('read ECONNRESET'), answer]);
    expect(await fetchThrough(route)).toBe(answer);
    expect(route.tries).toBe(2);
    expect(route.aborted).toBe(false);
  });

  it('fetchThrough names the URL and the tries when every try fails', async () => {
    const route = fakeRoute([new Error('read ECONNRESET')]);
    await expect(fetchThrough(route)).rejects.toThrow(
      'route.fetch http://127.0.0.1:5173/api/runs/f5dcb6a5 failed after 3 tries: read ECONNRESET'
    );
    expect(route.tries).toBe(3);
    expect(route.aborted).toBe(true);
  });

  it('fetchThrough does not try again an error that is not a reset', async () => {
    const route = fakeRoute([new Error('Target page, context or browser has been closed')]);
    await expect(fetchThrough(route)).rejects.toThrow(/failed after 1 try: Target page/);
    expect(route.tries).toBe(1);
    expect(route.aborted).toBe(true);
  });
});

/** A stand-in for a Playwright page: `route()` keeps its handlers so a test can call one. */
class FakePage {
  handlers = new Map<string, (route: unknown, request: unknown) => unknown>();
  route(url: string, handler: (route: unknown, request: unknown) => unknown) {
    this.handlers.set(url, handler);
  }
  unroute(url: string, handler: unknown) {
    if (this.handlers.get(url) === handler) this.handlers.delete(url);
  }
}

describe('settleThrownRoutes', () => {
  settleThrownRoutes(new FakePage());

  it('a handler that throws aborts its route and throws on', async () => {
    const page = new FakePage();
    page.route('**/api/x', () => {
      throw new Error('the handler threw');
    });
    const route = fakeRoute([{ status: 200 }]);
    await expect(Promise.resolve(page.handlers.get('**/api/x')?.(route, route.request()))).rejects.toThrow(
      'the handler threw'
    );
    expect(route.aborted).toBe(true);
  });

  it('a handler that answers is left alone', async () => {
    const page = new FakePage();
    page.route('**/api/x', async () => 'fulfilled');
    const route = fakeRoute([{ status: 200 }]);
    expect(await page.handlers.get('**/api/x')?.(route, route.request())).toBe('fulfilled');
    expect(route.aborted).toBe(false);
  });

  it('unroute by the handler a module passed still removes the route', () => {
    const page = new FakePage();
    const handler = () => undefined;
    page.route('**/api/x', handler);
    page.unroute('**/api/x', handler);
    expect(page.handlers.size).toBe(0);
  });
});

describe('checksDirFrom', () => {
  it('is the ui-checks folder unless a test names another', () => {
    expect(checksDirFrom({})).toBe(CHECKS_DIR);
    expect(checksDirFrom({ KEW_UI_CHECK_DIR: '/tmp/fixture' }).href).toBe('file:///tmp/fixture/');
  });
});
