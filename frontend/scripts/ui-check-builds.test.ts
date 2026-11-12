/**
 * A scoped run kew's merge verify makes keeps both builds whole (#3213):
 * `--builds` runs `npm run build` and `npm run build:demo` beside the modules a
 * change owns, and a bare `--builds` runs the builds and no module.
 */
import { describe, expect, it } from 'vitest';
import { buildsToRun, checkModules, runScope, scopedModules } from './ui-check-lib.mjs';

describe('--builds', () => {
  it('runs both builds beside the named modules', () => {
    const scope = runScope(['--builds', 'work-rows']);
    expect(scope).toEqual({ only: ['work-rows'], builds: true });
    expect(buildsToRun(scope)).toEqual({ build: true, demo: true });
    expect(scopedModules(checkModules(), scope)).toEqual(['work-rows.mjs']);
  });

  it('runs the builds and no module when it names none', () => {
    const scope = runScope(['--builds']);
    expect(scopedModules(checkModules(), scope)).toEqual([]);
    expect(buildsToRun(scope)).toEqual({ build: true, demo: true });
  });

  it('leaves a run without it as it was', () => {
    expect(buildsToRun(runScope([]))).toEqual({ build: true, demo: true });
    expect(buildsToRun(runScope(['work-rows']))).toEqual({ build: false, demo: false });
    expect(buildsToRun(runScope(['demo-static']))).toEqual({ build: false, demo: true });
    expect(scopedModules(checkModules(), runScope([]))).toEqual(checkModules());
  });
});
