/**
 * #2518 — ask mode: the model ranks entries the palette already holds, and a
 * ranked result is that entry. Selecting it goes through `paletteKey` and the
 * command's own confirm step exactly as selecting it directly does.
 *
 * `fetch` is injected and records every call, so what was sent — and that
 * nothing was sent — is asserted on the wire, not on intent.
 */
import { describe, expect, it, vi } from "vitest";
import {
  STOP_FLEET,
  commandEntries,
  needsConfirm,
  runCommand,
} from "./actions";
import {
  ASK_MAX_ENTRIES,
  askLabel,
  askPalette,
  askPayload,
  askedEntries,
  asksOn,
  isAskQuery,
} from "./ask";
import { pageEntries, paletteKey, type PaletteEntry } from "./entries";

type Call = { url: string; method: string; body: unknown };

function recorder(responses: Array<{ status: number; body: unknown }>) {
  const calls: Call[] = [];
  const queue = [...responses];
  const f = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    calls.push({
      url: String(url),
      method: init?.method ?? "GET",
      body: init?.body ? JSON.parse(String(init.body)) : undefined,
    });
    const next = queue.shift() ?? { status: 500, body: {} };
    return new Response(JSON.stringify(next.body), {
      status: next.status,
      headers: { "Content-Type": "application/json" },
    });
  }) as unknown as typeof fetch;
  return { f, calls };
}

const ITEM: PaletteEntry = {
  kind: "item",
  key: "item:gh:2055",
  id: "2055",
  title: "Fix the stuck epic",
  aliases: ["gh:2055"],
  href: "/work?item=gh%3A2055",
  itemId: "gh:2055",
};

function listed(): PaletteEntry[] {
  return [...pageEntries(), ITEM, ...commandEntries([STOP_FLEET])];
}

describe("an ask result", () => {
  it("is the entry itself, and Enter on it does what Enter on the entry does", async () => {
    const entries = listed();
    const { f, calls } = recorder([
      {
        status: 200,
        body: {
          results: [
            { id: STOP_FLEET.key, reason: "halts everything" },
            { id: ITEM.key, reason: "the stuck one" },
          ],
          cost_usd: 0.001,
        },
      },
    ]);
    const out = await askPalette("what stops it all?", entries, f);
    expect(out.error).toBeNull();
    const answer = out.answer!;
    const direct = entries.findIndex((e) => e.key === STOP_FLEET.key);

    // The very object the palette listed, not a copy with its own behaviour.
    expect(answer.entries[0]).toBe(entries[direct]);
    expect(answer.entries[1]).toBe(ITEM);
    expect(answer.reasons[STOP_FLEET.key]).toBe("halts everything");

    // Same key → same action, whichever list the cursor is in.
    const viaAsk = paletteKey("Enter", 0, answer.entries);
    expect(viaAsk).toEqual(paletteKey("Enter", direct, entries));
    expect(viaAsk).toEqual({ kind: "command", command: STOP_FLEET });
    expect(paletteKey("Enter", 1, answer.entries)).toEqual({
      kind: "go",
      href: ITEM.href,
    });
    expect(paletteKey("ArrowRight", 1, answer.entries)).toEqual({
      kind: "actions",
      itemId: "gh:2055",
    });

    // ...including the confirm step: the stop is held behind it, and the only
    // request made so far is the ask itself.
    if (viaAsk?.kind !== "command") throw new Error("expected a command");
    expect(needsConfirm(viaAsk.command)).toBe(true);
    const refused = await runCommand(viaAsk.command, {}, { fetch: f });
    expect(refused.error).not.toBeNull();
    expect(calls.map((c) => c.url)).toEqual(["/api/palette/ask"]);
  });

  it("never names an entry the palette did not list", () => {
    const entries = listed();
    const answer = askedEntries("q?", entries, [
      { id: "command:resume-fleet", reason: "made up" },
      { id: ITEM.key, reason: "real" },
      { id: ITEM.key, reason: "twice" },
      { id: "https://evil.example/", reason: "a link" },
    ]);
    expect(answer.entries).toEqual([ITEM]);
    expect(answer.reasons).toEqual({ [ITEM.key]: "real" });
  });

  it("over budget is an empty answer carrying the server's sentence", async () => {
    const sentence = "Today's spend ($5.00) has reached the daily budget of $5.00.";
    const { f } = recorder([{ status: 429, body: { detail: sentence } }]);
    const out = await askPalette("q?", listed(), f);
    expect(out.error).toBeNull();
    expect(out.answer).toEqual({
      query: "q?",
      entries: [],
      reasons: {},
      refusal: sentence,
    });
    // The query counts as asked: Enter does not ask it again.
    expect(asksOn("Enter", "q?", { enabled: true, asked: "q?" })).toBe(false);
  });

  it("shows a refused ask's own sentence, as the demo build refuses it", async () => {
    // #3139: the demo answers every write 405 with what it would have done.
    const sentence = "Demo: this would ask the model which command answers your question.";
    const { f } = recorder([{ status: 405, body: { detail: sentence } }]);
    const out = await askPalette("q?", listed(), f);
    expect(out).toEqual({
      answer: { query: "q?", entries: [], reasons: {}, refusal: sentence },
      error: null,
    });
  });

  it("words a 4xx with no sentence itself, and a network failure too", async () => {
    const { f } = recorder([{ status: 405, body: {} }]);
    expect(await askPalette("q?", listed(), f)).toEqual({
      answer: null,
      error: "The ask could not be answered just now.",
    });
    const offline = (async () => {
      throw new TypeError("Failed to fetch");
    }) as unknown as typeof fetch;
    expect(await askPalette("q?", listed(), offline)).toEqual({
      answer: null,
      error: "The ask could not be answered just now.",
    });
  });

  it("words an auth, CSRF or ask-off failure itself, so Enter can ask again", async () => {
    // A 401/403/404 sentence is about the request, not the ask: it is not a
    // refusal to show in place of results, and the query is not counted asked.
    for (const [status, detail] of [
      [401, "authentication required"],
      [403, "invalid or missing CSRF token"],
      [404, "not found"],
    ] as const) {
      const { f } = recorder([{ status, body: { detail } }]);
      expect(await askPalette("q?", listed(), f)).toEqual({
        answer: null,
        error: "The ask could not be answered just now.",
      });
    }
  });

  it("is an error line, not a throw, when the server refuses", async () => {
    const { f } = recorder([{ status: 503, body: { detail: "down" } }]);
    const out = await askPalette("q?", listed(), f);
    expect(out.answer).toBeNull();
    expect(out.error).toMatch(/could not be answered/);
  });
});

describe("the ask request", () => {
  it("carries the query and each entry's key and label — no href, no item id", async () => {
    const { f, calls } = recorder([
      { status: 200, body: { results: [], cost_usd: 0 } },
    ]);
    await askPalette("  what needs me?  ", [ITEM], f);
    expect(calls).toEqual([
      {
        url: "/api/palette/ask",
        method: "POST",
        body: {
          query: "what needs me?",
          entries: [{ id: "item:gh:2055", label: "item 2055 Fix the stuck epic" }],
        },
      },
    ]);
    expect(askLabel(commandEntries([STOP_FLEET])[0])).toBe(
      `do Stop the fleet ${STOP_FLEET.intent}`,
    );
  });

  it("is bounded to what the server accepts", () => {
    const many = Array.from({ length: ASK_MAX_ENTRIES + 20 }, (_, n) => ({
      ...ITEM,
      key: `item:${n}`,
    }));
    expect(askPayload("q?", many).entries).toHaveLength(ASK_MAX_ENTRIES);
  });
});

describe("when ask mode is disabled", () => {
  it("no key asks, so the palette offers no ask", () => {
    const off = { enabled: false, asked: null };
    expect(asksOn("Enter", "show me stuck epics?", off)).toBe(false);
    expect(asksOn("Tab", "stuck", off)).toBe(false);
  });
});

describe("when ask mode is enabled", () => {
  const on = { enabled: true, asked: null };

  it("Enter asks on a query ending in ?, Tab on any query", () => {
    expect(isAskQuery("show me stuck epics?")).toBe(true);
    expect(isAskQuery("?")).toBe(false);
    expect(isAskQuery("epics")).toBe(false);
    expect(asksOn("Enter", "show me stuck epics?", on)).toBe(true);
    expect(asksOn("Enter", "epics", on)).toBe(false);
    expect(asksOn("Tab", "epics", on)).toBe(true);
    expect(asksOn("Tab", "  ", on)).toBe(false);
    expect(asksOn("ArrowDown", "epics?", on)).toBe(false);
  });

  it("does not ask the same query twice: Enter then selects", () => {
    const q = "show me stuck epics?";
    expect(asksOn("Enter", q, { enabled: true, asked: q })).toBe(false);
    expect(asksOn("Tab", q, { enabled: true, asked: q })).toBe(false);
  });
});
