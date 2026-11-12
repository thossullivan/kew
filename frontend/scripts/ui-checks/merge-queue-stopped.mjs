/**
 * A stopped merge queue is a needs-you row until it is resumed (#2883).
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 *
 * The queue says it stopped once, as an audit event and a notification; what
 * keeps it in front of a person is the needs-you row the ledger's suspect row
 * serves. `scenarios/dashboard-merge-queue-stopped.json` is that row as
 * `assemble` serves it — `kew/tests/test_merge_queue_needs_you.py` fails if a
 * field drifts — so this reads what the BROWSER drew from it at each width: the
 * header counts it, Work draws exactly one row titled by the stopped queue
 * whose state mark says it needs you, selecting it draws the resume command in
 * full inside the viewport and Copy hands over exactly that command, and once
 * the queue is resumed (the ledger row gone) the row leaves and the header
 * count drops by one.
 *
 * And when the ledger read FAILED, so the dashboard serves no row but names
 * `merge_queue` in `unavailable`: Monitor's holds column says that reading is
 * missing rather than claiming nothing is held.
 */
// ui-check-owns: src/lib/work/list.ts
// ui-check-owns: src/lib/shell/header-state.ts
// ui-check-owns: src/lib/monitor/holds.ts
import { readFileSync } from "node:fs";
import { FRONTEND, SETTLE_MS } from "../ui-check-lib.mjs";

const ROW = JSON.parse(
  readFileSync(
    new URL("fixtures/scenarios/dashboard-merge-queue-stopped.json", FRONTEND),
    "utf8",
  ),
);

/**
 * Serve `/api/dashboard` with the stopped-queue row while `state.stopped`, or
 * with the merge-queue source fail-softed while `state.unread`.
 */
async function routeQueue(page, state) {
  await page.route(
    (url) => url.pathname === "/api/dashboard",
    async (route) => {
      const response = await route.fetch();
      const payload = await response.json();
      if (state.unread) {
        await route.fulfill({
          response,
          json: {
            ...payload,
            unavailable: [...(payload.unavailable ?? []), "merge_queue"],
          },
        });
        return;
      }
      if (!state.stopped) {
        await route.fulfill({ response, json: payload });
        return;
      }
      await route.fulfill({
        response,
        json: {
          ...payload,
          rows: [...(payload.rows ?? []), ROW],
          needs_you_no_epic: (payload.needs_you_no_epic ?? 0) + 1,
        },
      });
    },
  );
}

/** Record what the page hands the clipboard, instead of a real clipboard. */
async function spyClipboard(page) {
  await page.addInitScript(() => {
    window.__copied = [];
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: async (text) => void window.__copied.push(text) },
    });
  });
  return () => page.evaluate(() => window.__copied.slice());
}

/** What `composeHolds` says when the merge-queue source did not answer. */
const UNREAD_NOTICE = "the merge queue unavailable";

/** The header's needs-you number, or null when no counter is drawn. */
async function headerCount(page) {
  const el = page.locator("header [data-needs-you-count]");
  if ((await el.count()) === 0) return null;
  return Number(((await el.first().textContent()) ?? "").trim());
}

export async function checks({
  browser,
  base,
  check,
  expect,
  sleep,
  sizes: { WIDTH, HEIGHT },
}) {
  for (const [width, height] of [
    [WIDTH, HEIGHT],
    [390, 844],
  ]) {
    await check(
      `a stopped merge queue is a counted Work row offering ${ROW.command} until resumed at ${width}px`,
      async () => {
        const ctx = await browser.newContext({ viewport: { width, height } });
        const page = await ctx.newPage();
        const state = { stopped: true };
        try {
          const copied = await spyClipboard(page);
          await routeQueue(page, state);
          await page.goto(`${base}/work`, { waitUntil: "load" });
          const list = page.locator('aside[aria-label="Issues"]');
          const rows = list.locator("a[data-item]", { hasText: ROW.title });
          await rows.first().waitFor({ state: "visible", timeout: 15_000 });
          await sleep(SETTLE_MS);
          const stoppedCount = await headerCount(page);
          // The row's own state mark, the last one in its State cell: an
          // entry row of kind `merge_queue_suspect` is `manual`, whose mark already
          // IS needs-you, so the auxiliary diamond is (rightly) not drawn.
          const drawn = await rows.evaluateAll((els) =>
            els.map((el) => {
              const words = el.querySelectorAll("[data-status-word]");
              const word = words[words.length - 1];
              return {
                member: el.hasAttribute("data-needs-you"),
                mark: (word?.textContent ?? "").trim().toLowerCase(),
              };
            }),
          );
          expect(
            drawn.length === 1,
            `${width}px: ${drawn.length} Work row(s) read "${ROW.title}", want one`,
          );
          expect(
            drawn[0].member && drawn[0].mark === "needs you",
            `${width}px: the stopped-queue row reads ${JSON.stringify(drawn[0].mark)} and member=${drawn[0].member}, want a counted "needs you" row`,
          );

          await rows.first().click();
          await page.waitForURL(/[?&]item=/, { timeout: 5_000 });
          const code = page
            .locator("code[data-command]")
            .filter({ hasText: ROW.command });
          await code.first().waitFor({ state: "visible", timeout: 15_000 });
          await sleep(SETTLE_MS);
          const read = await code.first().evaluate((el) => {
            const box = el.getBoundingClientRect();
            return {
              text: (el.textContent ?? "").trim(),
              left: box.left,
              right: box.right,
              viewport: document.documentElement.clientWidth,
              scrollWidth: document.documentElement.scrollWidth,
            };
          });
          expect(
            read.text === ROW.command,
            `${width}px: the row prints ${JSON.stringify(read.text)}, want ${ROW.command}`,
          );
          expect(
            read.left >= 0 && read.right <= read.viewport + 0.5,
            `${width}px: the command spans ${read.left}..${read.right}, past the ${read.viewport}px viewport`,
          );
          expect(
            read.scrollWidth <= read.viewport,
            `${width}px: the page scrolls sideways (${read.scrollWidth}px over ${read.viewport}px)`,
          );

          const block = page
            .locator('section[aria-label^="Needs you"]')
            .filter({ has: code.first() });
          const copy = block
            .first()
            .getByRole("button", { name: "Copy command (c)" });
          await copy.waitFor({ state: "visible", timeout: 15_000 });
          await page.waitForFunction(
            (el) => el !== null && !el.disabled,
            await copy.elementHandle(),
            { timeout: 15_000 },
          );
          await copy.click();
          await sleep(SETTLE_MS);
          const handed = await copied();
          expect(
            handed.length === 1 && handed[0] === ROW.command,
            `${width}px: Copy handed over ${JSON.stringify(handed)}, want [${JSON.stringify(ROW.command)}]`,
          );

          // Resumed: a pass verified main green and the ledger row is gone.
          state.stopped = false;
          await page.goto(`${base}/work`, { waitUntil: "load" });
          await list.waitFor({ state: "visible", timeout: 15_000 });
          await rows.first().waitFor({ state: "detached", timeout: 15_000 });
          await sleep(SETTLE_MS);
          const resumedCount = await headerCount(page);
          expect(
            stoppedCount !== null && stoppedCount === (resumedCount ?? 0) + 1,
            `${width}px: the header read ${stoppedCount} stopped and ${resumedCount} resumed, want one fewer`,
          );
        } finally {
          await page.close();
          await ctx.close();
        }
      },
    );

    await check(
      `Monitor names an unread merge queue rather than an empty one at ${width}px`,
      async () => {
        const ctx = await browser.newContext({ viewport: { width, height } });
        const page = await ctx.newPage();
        try {
          await routeQueue(page, { unread: true });
          await page.goto(`${base}/monitor`, { waitUntil: "load" });
          const holds = page.locator('section[aria-label="Holds and blocks"]');
          await holds
            .getByText(UNREAD_NOTICE, { exact: true })
            .waitFor({ state: "visible", timeout: 15_000 });
          await sleep(SETTLE_MS);
          const read = await holds.evaluate((el) => ({
            text: el.textContent ?? "",
            viewport: document.documentElement.clientWidth,
            scrollWidth: document.documentElement.scrollWidth,
          }));
          expect(
            !read.text.includes("none active") &&
              !read.text.includes("nothing held or blocked"),
            `${width}px: the holds column calls an unread merge queue complete:\n${read.text}`,
          );
          expect(
            read.scrollWidth <= read.viewport,
            `${width}px: the page scrolls sideways (${read.scrollWidth}px over ${read.viewport}px)`,
          );
        } finally {
          await page.close();
          await ctx.close();
        }
      },
    );
  }
}
