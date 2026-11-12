/**
 * Every small button computes 13px (#3094): `buttonClasses({ size: 'sm' })`
 * was Tailwind's 12px `text-xs`, off DESIGN.md's 11/13/15/26/44 scale, and the
 * Prompts page's Approve and Decline put themselves back on it one at a time
 * while Propose beside them stayed at 12. The size now carries 13px at the
 * source, and the `button-sm` class it emits is how this finds every one.
 *
 * The Prompts page is served a pending v2 over an active v1, so Propose,
 * Approve and Decline are all drawn and the check has something to fail on;
 * every other route asserts on whatever small buttons its fixtures draw.
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it.
 */
// ui-check-owns: src/lib/factory/classes.ts
// ui-check-owns: src/lib/ui/Button.svelte
// ui-check-owns: src/lib/prompts/ProfileEditor.svelte
import { readFileSync } from 'node:fs';
import { FRONTEND } from '../ui-check-lib.mjs';

const PHONE = { width: 390, height: 844 };
const ROLE = 'dispatch';
const SCENARIO = JSON.parse(readFileSync(new URL('fixtures/scenarios/prompts-versions.json', FRONTEND), 'utf8'));
const isPath = (path) => (url) => url.pathname === path;

/** The routes whose fixtures may draw a small button. */
const ROUTES = ['/work', '/monitor', '/epics', '/epics/launch-fixes', '/runs/f5dcb6a5'];

/** Every visible small button, as `label: font-size`. */
function smallButtons() {
  return [...document.querySelectorAll('.button-sm')]
    .filter((el) => el.checkVisibility())
    .map((el) => ({ label: el.textContent.replace(/\s+/g, ' ').trim(), size: getComputedStyle(el).fontSize }));
}

const offScale = (buttons) => buttons.filter((b) => b.size !== '13px').map((b) => `"${b.label}" ${b.size}`);

export async function checks({ check, visit, expect, sizes: { WIDTH, HEIGHT } }) {
  for (const size of [{ width: WIDTH, height: HEIGHT }, PHONE]) {
    await check(`/prompts draws Propose, Approve and Decline at 13px at ${size.width}px`, () => {
      const v1 = SCENARIO.approved.versions[0];
      const v2 = { ...SCENARIO.proposed.versions[0], version: 2, audit_event_id: 3 };
      const prepare = async (page) => {
        await page.route(isPath(`/api/prompts/${ROLE}/versions`), (route) =>
          route.fulfill({ json: { ...SCENARIO.approved, versions: [v2, v1] } })
        );
        await page.route(isPath(`/api/prompts/${ROLE}`), (route) => route.fulfill({ json: SCENARIO.preview_approved }));
      };
      return visit(
        `/prompts?role=${ROLE}`,
        async (page) => {
          await page.locator('[data-pending="2"]').waitFor({ state: 'visible', timeout: 15_000 });
          const marked = await page
            .locator('[data-profile-editor] :is([data-propose], [data-approve], [data-decline])')
            .evaluateAll((els) => els.filter((el) => !el.classList.contains('button-sm')).map((el) => el.textContent.trim()));
          expect(marked.length === 0, `not drawn as small buttons: ${marked.join(', ')}`);
          const buttons = await page.evaluate(smallButtons);
          expect(buttons.length >= 3, `found ${buttons.length} small buttons; Propose, Approve and Decline make 3`);
          const off = offScale(buttons);
          expect(off.length === 0, `small buttons off the 13px step: ${off.join(', ')}`);
        },
        size,
        prepare
      );
    });

    for (const route of ROUTES) {
      await check(`${route} draws every small button at 13px at ${size.width}px`, () =>
        visit(
          route,
          async (page) => {
            const off = offScale(await page.evaluate(smallButtons));
            expect(off.length === 0, `small buttons off the 13px step: ${off.join(', ')}`);
          },
          size
        )
      );
    }
  }
}
