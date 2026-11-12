/**
 * A declaration hold names every node id it asks for, whole (#2884).
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 *
 * The `test_declarations_missing` reason is capped at 160 characters and ends
 * `+N more`, so with long node ids the console showed one id and a count, and
 * the operator could not see what to declare. The server now serves the full
 * list as `node_ids`; this injects such a hold — three ids long enough that the
 * reason can name only the first — and reads what the BROWSER drew on Work, at
 * each width: each id in full, nothing wider than the viewport, and a real
 * click on Copy ids putting every id on the clipboard, one per line.
 */
// ui-check-owns: src/lib/needs-you/NeedsYouBlock.svelte
// ui-check-owns: src/routes/(factory)/work/+page.*
import { SETTLE_MS } from "../ui-check-lib.mjs";

const ITEM = "2811";
const EPIC = "go-live-2026-09";
const IDS = [
  "tests/test_hosts_providers.py::TestGcloud::test_every_gcloud_call_runs_in_that_environment",
  "tests/test_hosts_providers.py::TestGcloud::test_subprocess_runs_ceiling_returns_but_leaves_the_child",
  "tests/test_serve_api_dashboard.py::TestDashboardReadsUnderLoad::test_a_slow_epic_reader_names_its_source_and_still_serves_the_rest",
];

/** The hold as `/api/dashboard` serves it (`needs_you._budget_rows`). */
const HOLD = {
  type: "budget_hold",
  consequence: "money",
  age: "2026-10-15T09:00:00+00:00",
  reason: `test_declarations_missing: 3 undeclared: ${IDS[0]} +2 more`,
  primary_action: "approve",
  issue_num: Number(ITEM),
  item_id: ITEM,
  estimate_usd: null,
  epic_ref: EPIC,
  url: null,
  pr_number: null,
  attention_kind: "held",
  label: "",
  command: `kew epic unhold ${EPIC} ${ITEM}`,
  sentence: "",
  title: "Declaration hold fixture",
  node_ids: IDS,
};

/** Serve `/api/dashboard` (exactly that path) with the hold added. */
async function routeDeclarationHold(page) {
  await page.route(
    (url) => url.pathname === "/api/dashboard",
    async (route) => {
      const response = await route.fetch();
      const payload = await response.json();
      await route.fulfill({
        response,
        json: { ...payload, rows: [...(payload.rows ?? []), HOLD] },
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
  for (const [width, height] of [
    [WIDTH, HEIGHT],
    [1280, 800],
    [390, 844],
  ]) {
    await check(
      `a declaration hold draws every node id in full at ${width}px`,
      async () => {
        const ctx = await browser.newContext({
          viewport: { width, height },
          permissions: ["clipboard-read", "clipboard-write"],
        });
        const page = await ctx.newPage();
        try {
          await routeDeclarationHold(page);
          await page.goto(`${base}/work?item=${ITEM}`, { waitUntil: "load" });
          const section = page.locator("[data-declare-ids]");
          await section.first().waitFor({ state: "visible", timeout: 15_000 });
          await sleep(SETTLE_MS);
          const read = await page.evaluate(() => {
            const viewport = document.documentElement.clientWidth;
            const ids = [
              ...document.querySelectorAll("[data-declare-ids] [data-node-id]"),
            ].map((el) => {
              const box = el.getBoundingClientRect();
              const style = getComputedStyle(el);
              return {
                text: (el.textContent ?? "").trim(),
                visible:
                  box.width > 0 &&
                  box.height > 0 &&
                  style.visibility !== "hidden",
                left: box.left,
                right: box.right,
              };
            });
            const copy = [
              ...document.querySelectorAll("[data-declare-ids] button"),
            ].map((b) => (b.textContent ?? "").trim());
            return {
              viewport,
              scrollWidth: document.documentElement.scrollWidth,
              ids,
              copy,
            };
          });
          const drawn = read.ids.map((id) => id.text);
          for (const id of IDS) {
            expect(
              drawn.includes(id),
              `${width}px: ${id} is not drawn in full (drawn: ${drawn.join(" | ")})`,
            );
          }
          expect(
            drawn.length === IDS.length,
            `${width}px: ${drawn.length} ids drawn, want ${IDS.length}`,
          );
          for (const id of read.ids) {
            expect(id.visible, `${width}px: ${id.text} is not visible`);
            expect(
              id.left >= 0 && id.right <= read.viewport + 0.5,
              `${width}px: ${id.text} spans ${id.left}..${id.right}, past the ${read.viewport}px viewport`,
            );
          }
          expect(
            read.scrollWidth <= read.viewport,
            `${width}px: the page scrolls sideways (${read.scrollWidth}px over ${read.viewport}px)`,
          );
          expect(
            read.copy.includes("Copy ids"),
            `${width}px: no copy action beside the ids (${read.copy.join(", ")})`,
          );
          // By text: the button's aria-label counts the ids instead.
          await section
            .first()
            .locator("button", { hasText: "Copy ids" })
            .click();
          await section
            .first()
            .locator("button", { hasText: "Copied" })
            .waitFor({ state: "visible", timeout: 5_000 });
          const clipboard = await page.evaluate(() =>
            navigator.clipboard.readText(),
          );
          expect(
            clipboard === IDS.join("\n"),
            `${width}px: Copy ids put ${JSON.stringify(clipboard)} on the clipboard, want every id one per line`,
          );
        } finally {
          await page.close();
          await ctx.close();
        }
      },
    );
  }
}
