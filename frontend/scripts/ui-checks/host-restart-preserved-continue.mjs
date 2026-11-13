/**
 * A preserved host-restart continuation offers continue on every surface
 * (#2988).
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 *
 * A node whose preserved continuation dies with its host a second time is
 * failed with its work still in the checkout (#2960). `kew epic retry` would
 * dispatch past those bytes, so the backend refuses it there and offers
 * `kew epic continue` instead. Until this check the only evidence of that in
 * the console was a backend test handing a node dict to `_epic_wait_row`, so a
 * page that hid the command, or printed and copied `retry`, passed everything.
 *
 * Nothing here is written by hand. `scenarios/host-restart-preserved-continue.json`
 * is the needs-you row the real `GET /api/dashboard` served and the node the
 * real `GET /api/epics/{ref}` served over the ledger the driver wrote, and
 * `kew/tests/test_host_restart_preserved_continuation.py` serves both again
 * and fails if a field drifts. This check adds them to the committed set and
 * reads what the BROWSER drew on Work, Monitor and the Epic page, at the
 * design width and on a phone: one needs-you action for the node on each, its
 * command `kew epic continue` drawn whole inside the viewport, `retry` nowhere
 * on the page, and a real click on that action's Copy putting exactly the
 * continue command on the clipboard. "Drawn" is `assertPainted`'s reading, not
 * a box: a transparent, faded, clipped or covered command fails it, and so
 * does a Copy control that is not painted before the click or whose Copied is
 * not painted after it. The Epic page draws the action on two surfaces, the
 * node popover and the selected-node rail; each carries it exactly once, and
 * nothing else on that page offers a command for the node. On the Epic
 * page the node is also reached the way a person reaches it, without `?node`:
 * its card wears the magenta needs-you mark, and a click on it opens the
 * popover that carries the command.
 */
// ui-check-owns: fixtures/scenarios/host-restart-preserved-continue.json
// ui-check-owns: src/routes/(factory)/work/+page.svelte
// ui-check-owns: src/lib/needs-you/NeedsYouBlock.svelte
// ui-check-owns: src/lib/monitor/Holds.svelte
// ui-check-owns: src/lib/epic/NodePopover.svelte
// ui-check-owns: src/lib/epic/SelectedRail.svelte
import { readFileSync } from "node:fs";
import { FRONTEND, SETTLE_MS, assertPainted } from "../ui-check-lib.mjs";

/** The served row and node, and the epic both belong to. */
const SERVED = JSON.parse(
  readFileSync(
    new URL("fixtures/scenarios/host-restart-preserved-continue.json", FRONTEND),
    "utf8",
  ),
);
const EPIC = SERVED.epic_ref;
const ITEM = SERVED.row.item_id;
const COMMAND = `kew epic continue ${EPIC} ${ITEM}`;
/** The command the backend refuses for this node: never on screen. */
const RETRY = `kew epic retry ${EPIC} ${ITEM}`;

const SIZES = [
  [1440, 960],
  [390, 844],
];

/** Serve the committed set with the row and the node added to it. */
async function routeServed(page) {
  await page.route(
    (url) => url.pathname === "/api/dashboard",
    async (route) => {
      const response = await route.fetch();
      const payload = await response.json();
      const byEpic = payload.needs_you_by_epic ?? {};
      await route.fulfill({
        response,
        json: {
          ...payload,
          rows: [...(payload.rows ?? []), SERVED.row],
          needs_you_by_epic: { ...byEpic, [EPIC]: (byEpic[EPIC] ?? 0) + 1 },
        },
      });
    },
  );
  await page.route(
    (url) => url.pathname === `/api/epics/${EPIC}`,
    async (route) => {
      const response = await route.fetch();
      const payload = await response.json();
      const { snapshot } = payload;
      const counts = snapshot.counts ?? {};
      await route.fulfill({
        response,
        json: {
          ...payload,
          snapshot: {
            ...snapshot,
            nodes: [...snapshot.nodes, SERVED.node],
            counts: { ...counts, failed: (counts.failed ?? 0) + 1 },
          },
        },
      });
    },
  );
}

/**
 * Every command block on the page, the body's text, where *code* is drawn
 * against the viewport, and which of its text's line rectangles fall outside
 * its own box.
 */
function readPage(code) {
  return code.evaluate((el) => {
    const box = el.getBoundingClientRect();
    const doc = document.documentElement;
    const range = document.createRange();
    range.selectNodeContents(el);
    const spill = [...range.getClientRects()]
      .filter((line) => line.width > 0 && line.height > 0)
      .filter(
        (line) =>
          line.left < box.left - 0.5 ||
          line.top < box.top - 0.5 ||
          line.right > box.right + 0.5 ||
          line.bottom > box.bottom + 0.5,
      )
      .map((line) => [line.left, line.top, line.right, line.bottom].map(Math.round));
    return {
      text: (el.textContent ?? "").trim(),
      left: box.left,
      right: box.right,
      top: box.top,
      bottom: box.bottom,
      spill,
      viewport: doc.clientWidth,
      viewportHeight: doc.clientHeight,
      scrollWidth: doc.scrollWidth,
      body: document.body.innerText,
      commands: [...document.querySelectorAll("[data-command]")].map((c) =>
        (c.textContent ?? "").trim(),
      ),
    };
  });
}

/**
 * Bring *locator* into view through whichever box scrolls it. Any sideways
 * scroll that takes is not hidden: `assertPainted` fails a window or ancestor
 * left scrolled sideways, so a command laid out past the right edge cannot be
 * carried back on screen and pass.
 */
function scrollIntoView(locator) {
  return locator.evaluate((el) => el.scrollIntoView({ block: "center", inline: "nearest" }));
}

/** Whether *node*'s card lies wholly inside the canvas it is drawn on. */
function onCanvas(page, node) {
  return node.evaluate((el) => {
    const pane = el.closest(".svelte-flow")?.getBoundingClientRect();
    const box = el.getBoundingClientRect();
    return (
      pane !== undefined &&
      box.left >= pane.left - 1 &&
      box.top >= pane.top - 1 &&
      box.right <= pane.right + 1 &&
      box.bottom <= pane.bottom + 1
    );
  });
}

/**
 * Node *id*'s card on the Epic canvas: whether it is the needs-you card
 * (DESIGN.md dag-node-card-needs-you) — the `needs` class, a magenta border, a
 * magenta diamond and NEEDS YOU in its status row — read as computed colours
 * against the page's own `--accent`.
 */
function readCard(node) {
  return node.evaluate((el) => {
    const probe = document.createElement("span");
    probe.style.color = "var(--accent)";
    document.body.append(probe);
    const accent = getComputedStyle(probe).color;
    probe.remove();
    const card = el.querySelector(".card");
    const diamond = [...el.querySelectorAll(".mk svg rect")].filter(
      (r) => (r.getAttribute("transform") ?? "").startsWith("rotate(45"),
    );
    const box = el.getBoundingClientRect();
    return {
      accent,
      needs: card?.classList.contains("needs") ?? false,
      border: card ? getComputedStyle(card).borderTopColor : null,
      diamond: diamond.map((r) => getComputedStyle(r).fill),
      status: (card?.textContent ?? "").replace(/\s+/g, " ").trim(),
      drawn: box.width > 0 && box.height > 0,
    };
  });
}

export async function checks({ browser, base, check, expect, sleep }) {
  await check("the served row and node offer continue, not retry", async () => {
    const offered = [
      SERVED.row.command,
      SERVED.node.attention?.command,
      SERVED.node.recovery_command,
    ];
    expect(
      offered.every((command) => command === COMMAND),
      `the scenario offers ${JSON.stringify(offered)}, want ${COMMAND} on each`,
    );
  });

  /** Open *path* at *width*, run *body* on the page, close it. */
  async function opened(path, [width, height], body) {
    const ctx = await browser.newContext({
      viewport: { width, height },
      permissions: ["clipboard-read", "clipboard-write"],
    });
    const page = await ctx.newPage();
    try {
      await routeServed(page);
      await page.goto(`${base}${path}`, { waitUntil: "load" });
      await body(page);
    } finally {
      await page.close();
      await ctx.close();
    }
  }

  /**
   * *code* is the continue command, painted whole inside the viewport and its
   * own box; retry is nowhere.
   */
  async function drawsContinue(page, code, width, surface) {
    await scrollIntoView(code);
    const read = await readPage(code);
    expect(
      read.text === COMMAND,
      `${width}px ${surface}: the action prints ${JSON.stringify(read.text)}, want ${COMMAND}`,
    );
    await assertPainted(page, code, `${width}px ${surface}: the command`);
    expect(
      read.left >= 0 &&
        read.right <= read.viewport + 0.5 &&
        read.top >= 0 &&
        read.bottom <= read.viewportHeight + 0.5,
      `${width}px ${surface}: the command spans ${read.left}..${read.right} x ${read.top}..${read.bottom}, ` +
        `outside the ${read.viewport}x${read.viewportHeight} viewport`,
    );
    expect(
      read.spill.length === 0,
      `${width}px ${surface}: the command's text lines ${JSON.stringify(read.spill)} run past its box`,
    );
    expect(
      read.scrollWidth <= read.viewport,
      `${width}px ${surface}: the page scrolls sideways (${read.scrollWidth}px over ${read.viewport}px)`,
    );
    expect(
      !read.body.includes(RETRY) &&
        !read.commands.some((c) => c.startsWith("kew epic retry") && c.endsWith(` ${ITEM}`)),
      `${width}px ${surface}: the page offers ${RETRY}`,
    );
    const mine = read.commands.filter((c) => c.endsWith(` ${ITEM}`));
    expect(
      mine.every((c) => c === COMMAND),
      `${width}px ${surface}: #${ITEM}'s commands read ${JSON.stringify(mine)}, want only ${COMMAND}`,
    );
  }

  /**
   * *button* is a painted Copy control, a real click on it paints Copied, and
   * puts exactly the continue command on the clipboard.
   */
  async function copiesContinue(page, button, width, surface) {
    await scrollIntoView(button);
    expect(
      ((await button.textContent()) ?? "").trim() === "Copy",
      `${width}px ${surface}: the control reads ${JSON.stringify(await button.textContent())}, want Copy`,
    );
    await assertPainted(page, button, `${width}px ${surface}: the Copy control`);
    await button.click();
    await button
      .filter({ hasText: "Copied" })
      .waitFor({ state: "visible", timeout: 5_000 });
    await assertPainted(page, button, `${width}px ${surface}: the Copied feedback`);
    const clipboard = await page.evaluate(() => navigator.clipboard.readText());
    expect(
      clipboard === COMMAND,
      `${width}px ${surface}: Copy put ${JSON.stringify(clipboard)} on the clipboard, want ${COMMAND}`,
    );
  }

  /**
   * The Epic page draws #1010's action on two surfaces, the node popover and
   * the selected-node rail, and each carries it exactly once; no other part of
   * the page offers a command for it.
   */
  async function oneEachSurface(popover, width, surface) {
    const read = await popover.evaluate((dialog, item) => {
      const mine = (root) =>
        [...root.querySelectorAll("[data-command]")]
          .filter((c) => (c.textContent ?? "").trim().endsWith(` ${item}`))
          .map((c) => ({ c, text: (c.textContent ?? "").trim() }));
      const rails = [...document.querySelectorAll('section[aria-label^="Selected node"]')];
      const inside = ({ c }) => dialog.contains(c) || rails.some((r) => r.contains(c));
      const texts = (found) => found.map(({ text }) => text);
      return {
        rails: rails.length,
        popover: texts(mine(dialog)),
        rail: texts(rails.flatMap(mine)),
        elsewhere: texts(mine(document).filter((found) => !inside(found))),
      };
    }, ITEM);
    expect(
      read.popover.length === 1 && read.popover[0] === COMMAND,
      `${width}px ${surface}: #${ITEM}'s popover offers ${JSON.stringify(read.popover)}, want exactly ${COMMAND}`,
    );
    expect(
      read.rails === 1 && read.rail.length === 1 && read.rail[0] === COMMAND,
      `${width}px ${surface}: ${read.rails} selected-node rail(s) offer ${JSON.stringify(read.rail)}, want exactly ${COMMAND}`,
    );
    expect(
      read.elsewhere.length === 0,
      `${width}px ${surface}: outside the popover and the rail the page offers ${JSON.stringify(read.elsewhere)} for #${ITEM}`,
    );
  }

  for (const size of SIZES) {
    const [width] = size;

    await check(`Work offers and copies one continue for #${ITEM} at ${width}px`, () =>
      opened("/work", size, async (page) => {
        const rows = page.locator('aside[aria-label="Issues"] a[data-item]', {
          has: page.locator("[data-row-reason]", { hasText: SERVED.row.label }),
        });
        await rows.first().waitFor({ state: "visible", timeout: 15_000 });
        await sleep(SETTLE_MS);
        const drawn = await rows.evaluateAll((els) =>
          els.map((el) => ({
            item: el.getAttribute("data-item"),
            member: el.hasAttribute("data-needs-you"),
            diamond: el.querySelector("[data-needs-you-diamond]") !== null,
          })),
        );
        expect(
          drawn.length === 1 && drawn[0].item === ITEM,
          `${width}px Work: ${JSON.stringify(drawn)} read "${SERVED.row.label}", want #${ITEM} once`,
        );
        expect(
          drawn[0].member && drawn[0].diamond,
          `${width}px Work: #${ITEM}'s row is not marked as needing you`,
        );
        await rows.first().click();
        await page.waitForURL(/[?&]item=/, { timeout: 5_000 });
        const commands = page.locator("code[data-command]");
        await commands
          .filter({ hasText: COMMAND })
          .first()
          .waitFor({ state: "visible", timeout: 15_000 });
        await sleep(SETTLE_MS);
        expect(
          (await commands.count()) === 1,
          `${width}px Work: the selected row draws ${JSON.stringify(await commands.allTextContents())}, want one action`,
        );
        await drawsContinue(page, commands.first(), width, "Work");
        await copiesContinue(
          page,
          page.getByRole("button", { name: "Copy command (c)" }).first(),
          width,
          "Work",
        );
      }),
    );

    await check(`Monitor holds one continue for #${ITEM} and copies it at ${width}px`, () =>
      opened("/monitor", size, async (page) => {
        const holds = page.locator('section[aria-label="Holds and blocks"]');
        const row = holds.locator(`li[data-hold-item="${ITEM}"]`);
        await row.first().waitFor({ state: "visible", timeout: 15_000 });
        await sleep(SETTLE_MS);
        const naming = await holds
          .locator("li")
          .evaluateAll(
            (els, ref) => els.filter((el) => (el.textContent ?? "").includes(ref)).length,
            `#${ITEM}`,
          );
        expect(
          (await row.count()) === 1 && naming === 1,
          `${width}px Monitor: ${await row.count()} hold row(s) for #${ITEM}, ${naming} naming it, want one`,
        );
        const commands = row.locator("code[data-command]");
        expect(
          (await commands.count()) === 1,
          `${width}px Monitor: #${ITEM}'s hold draws ${JSON.stringify(await commands.allTextContents())}, want one action`,
        );
        await drawsContinue(page, commands.first(), width, "Monitor");
        await copiesContinue(
          page,
          row.locator('button[aria-label^="Copy command"]'),
          width,
          "Monitor",
        );
      }),
    );

    await check(`the Epic page offers one continue for #${ITEM} and copies it at ${width}px`, () =>
      opened(`/epics/${EPIC}?node=${ITEM}`, size, async (page) => {
        const popover = page.locator('[role="dialog"]').filter({
          has: page.locator("code[data-command]", { hasText: COMMAND }),
        });
        await popover.first().waitFor({ state: "visible", timeout: 15_000 });
        await sleep(SETTLE_MS);
        const commands = popover.first().locator("code[data-command]");
        expect(
          (await commands.count()) === 1,
          `${width}px Epic: #${ITEM}'s popover draws ${JSON.stringify(await commands.allTextContents())}, want one action`,
        );
        await drawsContinue(page, commands.first(), width, "Epic");
        await oneEachSurface(popover.first(), width, "Epic");
        await copiesContinue(
          page,
          popover.first().getByRole("button", { name: "Copy command" }),
          width,
          "Epic",
        );
      }),
    );

    await check(`the Epic canvas marks #${ITEM} as needing you and its card opens continue at ${width}px`, () =>
      opened(`/epics/${EPIC}`, size, async (page) => {
        const node = page.locator(`.dag .svelte-flow__node[data-id="${ITEM}"]`);
        await node.first().waitFor({ state: "visible", timeout: 15_000 });
        await sleep(SETTLE_MS);
        // The first frame is a crop centred on one node; where it leaves
        // #1010 off the canvas (the phone), a person reaches it with "Fit
        // all", so the check does too.
        if (!(await onCanvas(page, node.first()))) {
          await page.getByRole("button", { name: "Fit view" }).click();
          for (let i = 0; i < 30 && !(await onCanvas(page, node.first())); i++) await sleep(100);
          await sleep(SETTLE_MS);
        }
        expect(
          await onCanvas(page, node.first()),
          `${width}px Epic: #${ITEM}'s card is off the canvas even after Fit all`,
        );
        const dialogs = page.locator('[role="dialog"]');
        expect(
          (await dialogs.count()) === 0,
          `${width}px Epic: a popover is open before #${ITEM}'s card is clicked`,
        );
        const card = await readCard(node.first());
        expect(card.drawn, `${width}px Epic: #${ITEM}'s card is not drawn`);
        expect(
          card.needs && card.border === card.accent,
          `${width}px Epic: #${ITEM}'s card is not the needs-you card ` +
            `(needs=${card.needs}, border ${card.border}, accent ${card.accent})`,
        );
        expect(
          card.diamond.length > 0 && card.diamond.every((fill) => fill === card.accent),
          `${width}px Epic: #${ITEM}'s card wears ${JSON.stringify(card.diamond)}, want the ${card.accent} diamond`,
        );
        expect(
          /needs you/i.test(card.status),
          `${width}px Epic: #${ITEM}'s card reads "${card.status}", want NEEDS YOU in its status row`,
        );
        await node.first().click();
        const popover = dialogs.filter({
          has: page.locator("code[data-command]", { hasText: COMMAND }),
        });
        const opens = await popover
          .first()
          .waitFor({ state: "visible", timeout: 5_000 })
          .then(() => true)
          .catch(() => false);
        expect(opens, `${width}px Epic: a click on #${ITEM}'s card opens no popover carrying ${COMMAND}`);
        await sleep(SETTLE_MS);
        const commands = popover.first().locator("code[data-command]");
        expect(
          (await commands.count()) === 1,
          `${width}px Epic card: #${ITEM}'s popover draws ${JSON.stringify(await commands.allTextContents())}, want one action`,
        );
        await drawsContinue(page, commands.first(), width, "Epic card");
        await oneEachSurface(popover.first(), width, "Epic card");
        await copiesContinue(
          page,
          popover.first().getByRole("button", { name: "Copy command" }),
          width,
          "Epic card",
        );
      }),
    );
  }
}
