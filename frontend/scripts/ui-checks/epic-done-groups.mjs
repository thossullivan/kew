/**
 * The epic graph's done group cards are never under the toolbar after a fit
 * (#2782, D25).
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 */
// ui-check-owns: src/routes/(factory)/epics/[ref]/**
// ui-check-owns: fixtures/factory/epics-go-live-2026-09*.json

/** The go-live fixture: SFD-204 and SFD-201 fold to the "2 done" card the
 * review found half under the toolbar. */
const GO_LIVE = "/epics/go-live-2026-09";

export async function checks({
  check,
  visit,
  expect,
  sleep,
  sizes: { WIDTH, HEIGHT },
}) {
  for (const size of [
    { width: WIDTH, height: HEIGHT },
    { width: 390, height: 844 },
  ]) {
    const at = `${size.width}x${size.height}`;
    await check(
      `after "Done · collapsed" every group card is clear of the toolbar at ${at}`,
      () =>
        visit(
          GO_LIVE,
          async (page) => {
            const groups = page.locator(
              '.dag .svelte-flow__node[data-id^="done:"]',
            );
            const toggle = page.locator(".dag button.done-toggle");
            await groups
              .first()
              .waitFor({ state: "attached", timeout: 10_000 });
            const read = async () => (await toggle.textContent())?.trim() ?? "";
            expect(
              (await read()).startsWith("Done · collapsed"),
              `the toggle reads "${await read()}"`,
            );

            // Show every done node, then fold them again: the second click is
            // the "Done · collapsed" fit the review read the card under.
            await toggle.click();
            await sleep(600);
            expect(
              (await read()) === "Done · shown",
              `the toggle reads "${await read()}"`,
            );
            await toggle.click();
            // The refit is a 260ms camera move.
            await sleep(600);
            expect(
              (await read()).startsWith("Done · collapsed"),
              `the toggle reads "${await read()}"`,
            );

            const toolbar = await page
              .locator('.dag [role="toolbar"]')
              .boundingBox();
            const floor = await page.locator(".dag .floor").boundingBox();
            expect(
              toolbar !== null && floor !== null,
              "the toolbar or the node area is not laid out",
            );
            const count = await groups.count();
            expect(
              count > 0,
              'no group card is drawn after "Done · collapsed"',
            );
            for (let i = 0; i < count; i += 1) {
              const card = groups.nth(i);
              const id = await card.getAttribute("data-id");
              const box = await card.boundingBox();
              expect(box !== null, `group card ${id} is not laid out`);
              const said = `${id} ${JSON.stringify(box)}`;
              // The toolbar is in the rail at the node area's foot, so "not
              // under it" is the card's whole box ending above the toolbar's.
              const overlaps =
                box.x < toolbar.x + toolbar.width &&
                box.x + box.width > toolbar.x &&
                box.y < toolbar.y + toolbar.height &&
                box.y + box.height > toolbar.y;
              expect(
                !overlaps,
                `group card ${said} overlaps the toolbar ${JSON.stringify(toolbar)}`,
              );
              expect(
                box.y + box.height <= toolbar.y + 1,
                `group card ${said} reaches the toolbar's row ${JSON.stringify(toolbar)}`,
              );
              // And it is wholly in the node area, which clips at its own foot:
              // a card half past it is the one D25 saw cut off by the rail.
              const inside =
                box.x >= floor.x - 1 &&
                box.y >= floor.y - 1 &&
                box.x + box.width <= floor.x + floor.width + 1 &&
                box.y + box.height <= floor.y + floor.height + 1;
              expect(
                inside,
                `group card ${said} is cut off by the node area ${JSON.stringify(floor)}`,
              );
            }
          },
          size,
        ),
    );
  }
}
