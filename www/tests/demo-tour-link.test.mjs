import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

import { DEMO_TOUR_URL, DEMO_URL, TOUR_DESKTOP } from '../src/lib/data/demo.js';

/**
 * The page's demo addresses checked against the demo's own sources, not
 * against themselves: the browser check imports DEMO_TOUR_URL and
 * TOUR_DESKTOP from demo.js, so a wrong tour id or breakpoint there would
 * pass it.
 */
const repo = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const TOURS = resolve(repo, 'frontend', 'demo', 'tours');
const CONTROLLER = resolve(repo, 'frontend', 'src', 'lib', 'demo', 'tours', 'controller.ts');

test("the tour link names a bundled tour, and its step sits on the link's route", async () => {
  const url = new URL(DEMO_TOUR_URL);
  assert.equal(url.origin, new URL(DEMO_URL).origin, 'the tour link leaves the demo');
  const id = url.searchParams.get('tour');
  assert.match(id ?? '', /^[a-z0-9-]+$/, `the tour id is "${id}"`);
  const raw = url.searchParams.get('step');
  const step = Number(raw);
  // Steps count from 1 (frontend/src/lib/demo/tours/deep-link.ts).
  assert.ok(Number.isInteger(step) && step >= 1, `the step is "${raw}"`);

  let tour;
  try {
    tour = JSON.parse(await readFile(resolve(TOURS, `${id}.json`), 'utf8'));
  } catch (error) {
    assert.fail(`no tour "${id}" in frontend/demo/tours: ${error.message}`);
  }
  assert.equal(tour.id, id);
  assert.ok(step <= tour.steps.length, `tour "${id}" has ${tour.steps.length} steps, not ${step}`);
  assert.equal(tour.steps[step - 1].route, url.pathname);
});

test('the page opens the tour at the width the tour controller does', async () => {
  const source = await readFile(CONTROLLER, 'utf8');
  const found = /export const DESKTOP = '([^']+)';/.exec(source);
  assert.ok(found, 'controller.ts no longer declares DESKTOP as a string');
  assert.equal(TOUR_DESKTOP, found[1]);
});
