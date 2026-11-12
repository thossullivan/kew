/**
 * The Prompts page says each fact once and in its own colour (#3093): the
 * history's active state word computes to the cyan token, never the grey a
 * later class would win with; a proposed row reads "v2 proposed by alice
 * <time>", the verb once and its time as the other rows write theirs, while a
 * decided row keeps its "decided by"; and the dispatch source chip names the
 * source in effect — "built-in template" when kew.toml's `[prompt]` sets no
 * template, "kew.toml template" only when it configures one.
 *
 * The answers are the committed fixtures and `fixtures/scenarios/prompts-versions.json`;
 * each check routes the reads it changes.
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it.
 */
// ui-check-owns: src/routes/(factory)/prompts/+page.svelte
// ui-check-owns: src/lib/prompts/ProfileEditor.svelte
// ui-check-owns: src/lib/prompts/source-chip.ts
// ui-check-owns: fixtures/scenarios/prompts-versions.json
import { readFileSync } from "node:fs";
import { FRONTEND } from "../ui-check-lib.mjs";

const PHONE = { width: 390, height: 844 };
const ROLE = "dispatch";
const fixture = (path) =>
  JSON.parse(readFileSync(new URL(`fixtures/${path}`, FRONTEND), "utf8"));
const SCENARIO = fixture("scenarios/prompts-versions.json");
const ROLES = fixture("factory/prompts.json");
const DISPATCH_READ = fixture(`factory/prompts-${ROLE}.json`);
const isPath = (path) => (url) => url.pathname === path;
const text = async (locator) =>
  (await locator.innerText()).replace(/\s+/g, " ").trim();

/** v2 proposed by alice over v1, active, that bob decided. */
const PROPOSED = {
  ...SCENARIO.proposed.versions[0],
  version: 2,
  body: "Version 2: name the file you touched.",
  body_sha256: "a".repeat(64),
  proposed_by: "alice",
  proposed_at: "2026-10-19T08:05:00+00:00",
};
const ACTIVE = { ...SCENARIO.approved.versions[0], decided_by: "bob" };
const HISTORY = { ...SCENARIO.approved, versions: [PROPOSED, ACTIVE] };

/** A configured `[prompt] template`, as `GET /api/prompts` describes it. */
const CONFIGURED = {
  kind: "config-template",
  name: "prompts/dispatch.md.j2",
  version: null,
  sha256: null,
  pin_mismatch: false,
};

/** `iso` as `fmtDateTime` writes it in the page's zone: ISO date, 24-hour time. */
function localStamp(iso) {
  const d = new Date(iso);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

/** The computed colour of `token` (a CSS custom property) in this page. */
function tokenColour(token) {
  const probe = document.createElement("span");
  probe.style.color = `var(${token})`;
  document.body.append(probe);
  const colour = getComputedStyle(probe).color;
  probe.remove();
  return colour;
}

export async function checks({
  check,
  visit,
  expect,
  sizes: { WIDTH, HEIGHT },
}) {
  for (const size of [{ width: WIDTH, height: HEIGHT }, PHONE]) {
    await check(
      `/prompts history: the active word is cyan and a proposed row says "proposed" once at ${size.width}px`,
      () =>
        visit(
          `/prompts?role=${ROLE}`,
          async (page) => {
            const proposed = page.locator('[data-history-version="2"]');
            await proposed.waitFor({ state: "visible", timeout: 15_000 });

            const word = page.locator(
              '[data-history-version="1"] [data-status-word]',
            );
            const colour = await word.evaluate(
              (el) => getComputedStyle(el).color,
            );
            const cyan = await page.evaluate(tokenColour, "--accent-2");
            expect(
              colour === cyan,
              `the active word computes to ${colour}, not the cyan ${cyan}`,
            );

            const stamp = await page.evaluate(localStamp, PROPOSED.proposed_at);
            const said = await text(proposed);
            const want = `v2 proposed by alice ${stamp} ${PROPOSED.body_sha256.slice(0, 7)}`;
            expect(
              said === want,
              `the proposed row reads "${said}", not "${want}"`,
            );
            const proposedWord = await proposed
              .locator("[data-status-word]")
              .evaluate((el) => getComputedStyle(el).color);
            expect(
              proposedWord !== cyan,
              "the proposed word is cyan as if it were active",
            );

            const decided = await text(
              page.locator('[data-history-version="1"]'),
            );
            expect(
              /^v1 active proposed by alice, decided by bob \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} /.test(
                decided,
              ),
              `the decided row reads "${decided}"`,
            );
          },
          size,
          async (page) => {
            await page.route(isPath(`/api/prompts/${ROLE}/versions`), (route) =>
              route.fulfill({ json: HISTORY }),
            );
            await page.route(isPath(`/api/prompts/${ROLE}`), (route) =>
              route.fulfill({ json: SCENARIO.preview_approved }),
            );
          },
        ),
    );

    for (const [configured, want] of [
      [false, "built-in template"],
      [true, "kew.toml template"],
    ]) {
      const label = configured
        ? "with a [prompt] template"
        : "without a [prompt] template";
      await check(
        `/prompts names the dispatch source in effect ${label} at ${size.width}px`,
        () =>
          visit(
            `/prompts?role=${ROLE}`,
            async (page) => {
              await page
                .locator("[data-preview]")
                .waitFor({ state: "visible", timeout: 15_000 });
              const row = await text(
                page.locator(`[data-role="${ROLE}"] [data-source-chip]`),
              );
              expect(
                row === want,
                `the dispatch row's chip reads "${row}", not "${want}"`,
              );
              const pane = await text(page.locator("[data-pane-source]"));
              expect(
                pane === want,
                `the pane's chip reads "${pane}", not "${want}"`,
              );
            },
            size,
            async (page) => {
              if (!configured) {
                const kind = ROLES.find((r) => r.role === ROLE)?.source.kind;
                expect(
                  kind === "packaged-template",
                  `the catalog fixture's dispatch source is ${kind}`,
                );
                return;
              }
              const roles = ROLES.map((r) =>
                r.role === ROLE ? { ...r, source: CONFIGURED } : r,
              );
              await page.route(isPath("/api/prompts"), (route) =>
                route.fulfill({ json: roles }),
              );
              await page.route(isPath(`/api/prompts/${ROLE}`), (route) =>
                route.fulfill({
                  json: { ...DISPATCH_READ, source: CONFIGURED },
                }),
              );
            },
          ),
      );
    }
  }

  /**
   * D43 (#3470) — the preview caption ends in a sentence, not in a raw
   * key=value line: "sha256 1dde372 · runner claude-code · posture contained …"
   * is behind a closed `details ▸` disclosure on that line. And a role whose one
   * run kind is its own name does not repeat that name as its subtitle.
   */
  for (const size of [PHONE, { width: 768, height: 1024 }, { width: WIDTH, height: HEIGHT }]) {
    await check(
      `/prompts folds its key=value line and repeats no role name at ${size.width}px`,
      () =>
        visit(
          `/prompts?role=${ROLE}`,
          async (page) => {
            const caption = page.locator("[data-preview-caption]");
            await caption.waitFor({ state: "visible", timeout: 15_000 });
            const shown = await text(caption);
            expect(
              shown.startsWith("Rendered over a sample item"),
              `the caption reads "${shown}"`,
            );
            expect(
              !/sha256|=|\w+ (true|false)\b|runner claude-code/.test(shown),
              `the caption still prints its facts: "${shown}"`,
            );
            const toggle = caption.getByRole("button", { name: /details/ });
            expect(
              (await toggle.getAttribute("aria-expanded")) === "false",
              "the details disclosure is not closed on arrival",
            );
            if (size.width < 640) {
              // DESIGN.md: a control on a phone is at least 32px to the press.
              const height = (await toggle.boundingBox())?.height ?? 0;
              expect(height >= 32, `the details toggle is ${height}px tall at ${size.width}px`);
            }
            const facts = page.locator("[data-preview-facts]");
            expect(!(await facts.isVisible()), "the facts line shows before it is asked for");
            await toggle.click();
            await facts.waitFor({ state: "visible", timeout: 5_000 });
            const opened = await text(facts);
            expect(
              opened.startsWith("sha256 ") && opened.includes("runner claude-code"),
              `the opened facts read "${opened}"`,
            );

            const rows = await page.evaluate(() =>
              [...document.querySelectorAll("[data-role-kinds]")].map((el) => ({
                role: (el.previousElementSibling?.textContent ?? "").trim(),
                kinds: (el.textContent ?? "").trim(),
              })),
            );
            expect(rows.length >= 5, `only ${rows.length} role rows drew`);
            for (const row of rows) {
              expect(
                row.kinds !== row.role,
                `the ${row.role} row repeats its name as its subtitle`,
              );
            }
            const dispatch = rows.find((r) => r.role === "dispatch");
            expect(
              dispatch?.kinds === "dispatch · loop · delegation",
              `the dispatch row's run kinds read "${dispatch?.kinds}"`,
            );
            const box = await page.evaluate(() => ({
              scroll: document.documentElement.scrollWidth,
              client: document.documentElement.clientWidth,
            }));
            expect(box.scroll <= box.client, `/prompts scrolls sideways at ${size.width}px`);
          },
          size,
        ),
    );
  }

  // DESIGN.md: a control on a phone is at least 32px to the press, the
  // refused launch's `details ▸` as much as the caption's.
  await check("/prompts refused launch: its details toggle is at least 32px at 390px", () =>
    visit(
      `/prompts?role=${ROLE}`,
      async (page) => {
        await page
          .locator("[data-preview-refused]")
          .waitFor({ state: "visible", timeout: 15_000 });
        const toggle = page.locator('button[aria-controls="preview-facts"]');
        const height = (await toggle.boundingBox())?.height ?? 0;
        expect(height >= 32, `the refused launch's details toggle is ${height}px tall`);
      },
      PHONE,
      async (page) => {
        await page.route(isPath(`/api/prompts/${ROLE}`), (route) =>
          route.fulfill({
            json: {
              ...DISPATCH_READ,
              preview: "",
              variant: {
                ...DISPATCH_READ.variant,
                posture: "refused",
                refusal: "The sandbox is off, so a dispatch is refused.",
              },
            },
          }),
        );
      },
    ),
  );
}
