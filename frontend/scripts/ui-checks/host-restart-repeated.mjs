/**
 * The repeated-restart wait offers the command that matches the checkout (#2964).
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 *
 * The second time the host restarts under an epic node, the epic asks a
 * person. A clean node failed, so its row offers `kew epic retry`; a node
 * whose checkout still holds the work kept it preserved, so its row offers
 * `kew epic continue` — a retry there would start over beside the work.
 *
 * Nothing here is written by hand. `scenarios/dashboard-host-restart-repeated.json`
 * is what the real `GET /api/dashboard` served over the ledger the driver
 * wrote for each death, and `kew/tests/test_host_restart_reap.py` serves it
 * again and fails if a field drifts — so a backend that offered the wrong
 * command fails there, and this check, which asserts the row's OWN command,
 * reads what the BROWSER drew from it at each width. It starts where a person
 * does, on the Work list: exactly one row reads the repeated-restart label and
 * wears the needs-you mark, and selecting THAT row draws its command — the
 * right verb, never the opposite one — in full inside the viewport, and a real
 * click on Copy puts exactly that command on the clipboard.
 */
// ui-check-owns: fixtures/scenarios/dashboard-host-restart-repeated.json
// ui-check-owns: src/routes/(factory)/work/+page.svelte
// ui-check-owns: src/lib/work/list.ts
// ui-check-owns: src/lib/needs-you/NeedsYouBlock.svelte
import { readFileSync } from "node:fs";
import { FRONTEND, SETTLE_MS } from "../ui-check-lib.mjs";

/** The served row for each death, by checkout: `clean` and `dirty`. */
const SERVED = JSON.parse(
  readFileSync(
    new URL(
      "fixtures/scenarios/dashboard-host-restart-repeated.json",
      FRONTEND,
    ),
    "utf8",
  ),
);

/** The first three words of *command*: its verb, `kew epic continue`. */
const verbOf = (command) => command.split(" ").slice(0, 3).join(" ");

const CASES = Object.entries(SERVED).map(([name, row]) => ({
  name,
  command: row.command,
  row,
}));

/** Serve `/api/dashboard` (exactly that path) with *row* as one more wait. */
async function routeServedRow(page, row) {
  await page.route(
    (url) => url.pathname === "/api/dashboard",
    async (route) => {
      const response = await route.fetch();
      const payload = await response.json();
      await route.fulfill({
        response,
        json: { ...payload, rows: [...(payload.rows ?? []), row] },
      });
    },
  );
}

export async function checks({
  browser,
  base,
  check,
  expect,
  sleep,
  sizes: { WIDTH, HEIGHT },
}) {
  await check(
    "the served repeated-restart rows offer continue and retry",
    async () => {
      const verbs = CASES.map((c) => verbOf(c.command)).sort();
      expect(
        verbs.join() === "kew epic continue,kew epic retry",
        `the served rows offer ${JSON.stringify(verbs)}`,
      );
    },
  );
  const VERBS = CASES.map((c) => verbOf(c.command));
  for (const { name, command, row } of CASES) {
    const opposite = VERBS.filter((v) => v !== verbOf(command));
    for (const [width, height] of [
      [WIDTH, HEIGHT],
      [1280, 800],
      [390, 844],
    ]) {
      await check(
        `a ${name} repeated host restart is a Work row that offers and copies ${verbOf(command)} at ${width}px`,
        async () => {
          const ctx = await browser.newContext({
            viewport: { width, height },
            permissions: ["clipboard-read", "clipboard-write"],
          });
          const page = await ctx.newPage();
          try {
            await routeServedRow(page, row);
            await page.goto(`${base}/work`, { waitUntil: "load" });
            const list = page.locator('aside[aria-label="Issues"]');
            const rows = list.locator("a[data-item]", {
              has: page.locator("[data-row-reason]", { hasText: row.label }),
            });
            await rows.first().waitFor({ state: "visible", timeout: 15_000 });
            await sleep(SETTLE_MS);
            // The needs-you mark is the magenta diamond wherever the row draws
            // it (#2971): beside a held ring (`dirty`), or AS the state mark of
            // a failed node nothing holds (`clean`, state `manual`), which
            // `needsYouDiamond` deliberately does not draw twice. Read off the
            // painting — StatusMark's diamond is its rect rotated 45°, filled
            // `--accent` resolved from the token itself.
            const drawn = await rows.evaluateAll((els) => {
              const probe = document.createElement("span");
              probe.style.color = "var(--accent)";
              document.body.append(probe);
              const accent = getComputedStyle(probe).color;
              probe.remove();
              return els.map((el) => ({
                member: el.hasAttribute("data-needs-you"),
                diamond: [
                  ...el.querySelectorAll('svg rect[transform^="rotate(45"]'),
                ].some((rect) => getComputedStyle(rect).fill === accent),
              }));
            });
            expect(
              drawn.length === 1,
              `${width}px: ${drawn.length} Work row(s) read "${row.label}", want one`,
            );
            expect(
              drawn[0].member && drawn[0].diamond,
              `${width}px: the repeated-restart row is not marked as needing you`,
            );
            await rows.first().click();
            await page.waitForURL(/[?&]item=/, { timeout: 5_000 });
            const commands = page.locator("code[data-command]");
            const code = commands.filter({ hasText: command });
            await code.first().waitFor({ state: "visible", timeout: 15_000 });
            await sleep(SETTLE_MS);
            const offered = await commands.evaluateAll((els) =>
              els.map((el) => (el.textContent ?? "").trim()),
            );
            expect(
              offered.filter((text) => text === command).length === 1,
              `${width}px: the selected row draws ${JSON.stringify(offered)}, want one ${command}`,
            );
            for (const verb of opposite)
              expect(
                !offered.some((text) => text.startsWith(verb)),
                `${width}px: the ${name} row also offers ${verb}: ${JSON.stringify(offered)}`,
              );
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
              read.text === command,
              `${width}px: the row prints ${JSON.stringify(read.text)}, want ${command}`,
            );
            expect(
              read.left >= 0 && read.right <= read.viewport + 0.5,
              `${width}px: the command spans ${read.left}..${read.right}, past the ${read.viewport}px viewport`,
            );
            expect(
              read.scrollWidth <= read.viewport,
              `${width}px: the page scrolls sideways (${read.scrollWidth}px over ${read.viewport}px)`,
            );
            await page
              .getByRole("button", { name: "Copy command (c)" })
              .first()
              .click();
            await page
              .getByRole("button", { name: "Copy command (c)" })
              .first()
              .filter({ hasText: "Copied" })
              .waitFor({ state: "visible", timeout: 5_000 });
            const clipboard = await page.evaluate(() =>
              navigator.clipboard.readText(),
            );
            expect(
              clipboard === command,
              `${width}px: Copy put ${JSON.stringify(clipboard)} on the clipboard, want ${command}`,
            );
          } finally {
            await page.close();
            await ctx.close();
          }
        },
      );
    }
  }
}
