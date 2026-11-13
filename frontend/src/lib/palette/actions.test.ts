/**
 * #2517 — what the palette can do: an item's offered actions sent against its
 * revision, the server's dispatch command copied without a write, and the
 * fleet stop held behind a reason and a confirm.
 *
 * `fetch` is injected and records every call, so "no POST" and "no request at
 * all" are asserted on what was actually sent rather than on what was meant.
 */
import { describe, expect, it, vi } from "vitest";
import planJson from "../../../fixtures/factory/dispatch-resolve.json";
import viewJson from "../../../fixtures/factory/verbs-work-2055.json";
import type { DispatchPlan, WorkAction, WorkView } from "$lib/api";
import {
  STOP_FLEET,
  commandEntries,
  copyCommands,
  needsConfirm,
  needsReason,
  offeredCommands,
  offersDispatch,
  openItemOf,
  paletteCommands,
  runCommand,
  viewFor,
  type PaletteCommand,
} from "./actions";
import { paletteKey, rankEntries } from "./entries";

const FINALIZE: WorkAction = {
  action: "finalize_publication",
  intent: "finish the publication kew already started",
  target_kind: "publication_job",
  target_ref: "job-7",
  safest: true,
  requires_reason: false,
};

function viewWith(actions: WorkAction[], revision = "rev-41"): WorkView {
  const base = structuredClone(viewJson) as unknown as WorkView;
  return { ...base, actions, view_revision: revision };
}

const plan = planJson as unknown as DispatchPlan;

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

const offered = (cmds: PaletteCommand[]) =>
  cmds.filter(
    (c): c is Extract<PaletteCommand, { kind: "offered" }> =>
      c.kind === "offered",
  );

describe("an offered action", () => {
  it("is listed for an item that offers finalize_publication", () => {
    const cmds = paletteCommands({
      itemId: "2055",
      view: viewWith([FINALIZE]),
      plan: null,
    });
    expect(offered(cmds).map((c) => c.action)).toEqual([
      "finalize_publication",
    ]);
    const entries = commandEntries(cmds);
    const entry = rankEntries(entries, "finalize")[0];
    expect(entry.command).toMatchObject({
      kind: "offered",
      action: "finalize_publication",
    });
    // Enter on it runs the command (through its confirm step), never a navigation.
    expect(paletteKey("Enter", 0, [entry])).toEqual({
      kind: "command",
      command: entry.command,
    });
  });

  it("sends the item's expected_revision when it is called", async () => {
    const [cmd] = offeredCommands("2055", viewWith([FINALIZE], "rev-41"));
    const after = viewWith([], "rev-42");
    const { f, calls } = recorder([
      {
        status: 200,
        body: {
          action: "finalize_publication",
          target_kind: "publication_job",
          target_ref: "job-7",
          applied: true,
          idempotent: false,
          detail: "",
          expected_revision: "rev-41",
          view: after,
        },
      },
    ]);
    const out = await runCommand(cmd, { confirm: true }, { fetch: f });
    expect(calls).toEqual([
      {
        url: "/api/verbs/work/2055/action",
        method: "POST",
        body: {
          action: "finalize_publication",
          expected_revision: "rev-41",
          reason: "",
        },
      },
    ]);
    expect(out).toMatchObject({ chip: "applied", error: null, reread: false });
    expect(out.view?.view_revision).toBe("rev-42");
  });

  it("is not sent without the confirm step", async () => {
    const [cmd] = offeredCommands("2055", viewWith([FINALIZE]));
    expect(needsConfirm(cmd)).toBe(true);
    const { f, calls } = recorder([]);
    const out = await runCommand(cmd, {}, { fetch: f });
    expect(calls).toEqual([]);
    expect(out.error).toMatch(/confirm/);
  });

  it("that must be explained is not sent without a reason", async () => {
    const [cmd] = offeredCommands(
      "2055",
      viewWith([{ ...FINALIZE, requires_reason: true }]),
    );
    expect(needsReason(cmd)).toBe(true);
    const { f, calls } = recorder([]);
    const out = await runCommand(
      cmd,
      { confirm: true, reason: "   " },
      { fetch: f },
    );
    expect(calls).toEqual([]);
    expect(out.error).toMatch(/reason/);
  });

  it("re-reads the item on a 409 and says so", async () => {
    const [cmd] = offeredCommands("2055", viewWith([FINALIZE], "rev-41"));
    const reread = viewWith([], "rev-43");
    const { f, calls } = recorder([
      { status: 409, body: { detail: "the view moved to rev-43" } },
      { status: 200, body: reread },
    ]);
    const out = await runCommand(cmd, { confirm: true }, { fetch: f });
    expect(calls.map((c) => `${c.method} ${c.url}`)).toEqual([
      "POST /api/verbs/work/2055/action",
      "GET /api/verbs/work/2055",
    ]);
    expect(out.reread).toBe(true);
    expect(out.view?.view_revision).toBe("rev-43");
    expect(out.error).toContain("the view moved to rev-43");
    expect(out.error).toContain("re-read");
  });

  it("is refused by the server's approver rule, said in the console's words", async () => {
    const [cmd] = offeredCommands("2055", viewWith([FINALIZE]));
    const { f } = recorder([
      { status: 403, body: { detail: "actor not in approvers" } },
    ]);
    const out = await runCommand(cmd, { confirm: true }, { fetch: f });
    expect(out.error).toMatch(/not an approver/);
    expect(out.view).toBeNull();
  });
});

describe("an action that is not offered", () => {
  it("never appears", () => {
    const view = viewWith([FINALIZE]);
    const cmds = paletteCommands({ itemId: "2055", view, plan });
    const actions = offered(cmds).map((c) => c.action);
    expect(actions).toEqual(["finalize_publication"]);
    for (const absent of [
      "retry",
      "refuse_no_change",
      "cancel_run",
      "redispatch",
    ]) {
      expect(actions).not.toContain(absent);
      expect(
        rankEntries(commandEntries(cmds), absent).some(
          (e) => e.command?.kind === "offered" && e.command.action === absent,
        ),
      ).toBe(false);
    }
  });

  it("none appear when the view offers nothing, or has no revision to send", () => {
    expect(offeredCommands("2055", viewWith([]))).toEqual([]);
    expect(offeredCommands("2055", viewWith([FINALIZE], ""))).toEqual([]);
    expect(offeredCommands("2055", null)).toEqual([]);
  });

  it("none appear without an item; the fleet stop always does", () => {
    expect(
      paletteCommands({ itemId: null, view: viewWith([FINALIZE]), plan }),
    ).toEqual([STOP_FLEET]);
  });
});

describe("a dispatch entry", () => {
  it("copies the backend-computed command and makes no POST", async () => {
    const [cmd] = copyCommands("2302", plan);
    expect(cmd).toMatchObject({ kind: "copy", text: plan.command });
    expect(needsConfirm(cmd)).toBe(false);
    const writeText = vi.fn(async () => {});
    const { f, calls } = recorder([]);
    const out = await runCommand(
      cmd,
      {},
      { fetch: f, clipboard: { writeText } },
    );
    expect(writeText).toHaveBeenCalledWith("kew dispatch 2302");
    expect(calls).toEqual([]);
    expect(out).toMatchObject({ chip: "copied", error: null });
  });

  it("is whatever the server said, never assembled from the item number", () => {
    const [cmd] = copyCommands("2302", {
      ...plan,
      command: "kew dispatch 2302 --from-server",
    });
    expect(cmd).toMatchObject({ text: "kew dispatch 2302 --from-server" });
  });

  it("is absent when the server composed none, or the plan is for another item", () => {
    expect(copyCommands("2302", { ...plan, command: null })).toEqual([]);
    expect(copyCommands("2302", { ...plan, command: undefined })).toEqual([]);
    expect(copyCommands("2055", plan)).toEqual([]);
    expect(copyCommands("2302", null)).toEqual([]);
  });

  it("says so when the clipboard refuses", async () => {
    const [cmd] = copyCommands("2302", plan);
    const writeText = vi.fn(async () => {
      throw new Error("denied");
    });
    const out = await runCommand(cmd, {}, { clipboard: { writeText } });
    expect(out.error).toMatch(/copy it by hand/);
  });
});

describe("where a dispatch is offered (#2785)", () => {
  const copies = (cmds: PaletteCommand[]) =>
    cmds.filter((c) => c.kind === "copy").map((c) => c.key);
  const planFor = (item: string): DispatchPlan => ({ ...plan, item });
  const next = (action: string): WorkView => ({
    ...viewWith([]),
    safest_action: { action, reason: "r", blocked_by: null },
  });

  it("is offered where dispatch is the projection's safest action", () => {
    const input = { itemId: "2302", view: next("dispatch"), plan: planFor("2302") };
    expect(offersDispatch(input)).toBe(true);
    expect(copies(paletteCommands(input))).toEqual(["command:2302:copy-dispatch"]);
  });

  it("is offered on an item the ready queue holds, view or none", () => {
    const input = { itemId: "2060", view: null, plan: planFor("2060"), queued: true };
    expect(copies(paletteCommands(input))).toEqual(["command:2060:copy-dispatch"]);
  });

  it("is not offered on an item in review, whose next move is not dispatch", () => {
    // The 2055 fixture is the in-review item: its safest action is `wait`.
    const view = structuredClone(viewJson) as unknown as WorkView;
    expect(view.safest_action.action).not.toBe("dispatch");
    const input = { itemId: "2055", view, plan: planFor("2055") };
    expect(offersDispatch(input)).toBe(false);
    expect(copies(paletteCommands(input))).toEqual([]);
  });

  it("is not offered on a needs-you entry key, which is no item", () => {
    const itemId = "stalled_epic:go-live-2026-09";
    const input = { itemId, view: null, plan: planFor(itemId) };
    expect(offersDispatch(input)).toBe(false);
    expect(copies(paletteCommands(input))).toEqual([]);
  });
});

describe("stopping the fleet", () => {
  const stopped = { stopped: true, already_stopped: false, runs_killed: [] };

  it("needs a non-empty reason before POST /api/fleet/stop", async () => {
    expect(needsReason(STOP_FLEET)).toBe(true);
    for (const reason of ["", "   "]) {
      const { f, calls } = recorder([]);
      const out = await runCommand(
        STOP_FLEET,
        { reason, confirm: true },
        { fetch: f },
      );
      expect(calls).toEqual([]);
      expect(out.error).toMatch(/reason/);
    }
  });

  it("needs a confirm before POST /api/fleet/stop", async () => {
    expect(needsConfirm(STOP_FLEET)).toBe(true);
    const { f, calls } = recorder([]);
    const out = await runCommand(
      STOP_FLEET,
      { reason: "runaway spend" },
      { fetch: f },
    );
    expect(calls).toEqual([]);
    expect(out.error).toMatch(/confirm/);
  });

  it("sends the reason once both are given", async () => {
    const { f, calls } = recorder([{ status: 200, body: stopped }]);
    const out = await runCommand(
      STOP_FLEET,
      { reason: " runaway spend ", confirm: true },
      { fetch: f },
    );
    expect(calls).toEqual([
      {
        url: "/api/fleet/stop",
        method: "POST",
        body: { reason: "runaway spend" },
      },
    ]);
    expect(out).toMatchObject({ chip: "stopped", error: null });
  });

  it("is always listed", () => {
    expect(
      paletteCommands({ itemId: "2055", view: viewWith([FINALIZE]), plan }).at(
        -1,
      ),
    ).toBe(STOP_FLEET);
    expect(rankEntries(commandEntries([STOP_FLEET]), "stop")[0].command).toBe(
      STOP_FLEET,
    );
  });
});

describe("the subject item", () => {
  it("is the one /work?item= has open, opaque", () => {
    expect(openItemOf(new URL("http://x/work?item=SFD-12"))).toBe("SFD-12");
    expect(openItemOf(new URL("http://x/work"))).toBeNull();
    expect(openItemOf(new URL("http://x/monitor?item=2055"))).toBeNull();
  });

  it("reads a re-read view over the projection one", () => {
    const old = viewWith([FINALIZE], "rev-1");
    const newer = viewWith([], "rev-2");
    expect(viewFor("2055", [old])).toBe(old);
    expect(viewFor("2055", [old], { "2055": newer })).toBe(newer);
    expect(viewFor(null, [old])).toBeNull();
  });

  it("→ on an item entry lists its actions", () => {
    const entry = {
      kind: "item" as const,
      key: "item:2055",
      id: "2055",
      title: "",
      aliases: [],
      href: "/work?item=2055",
      itemId: "2055",
    };
    expect(paletteKey("ArrowRight", 0, [entry])).toEqual({
      kind: "actions",
      itemId: "2055",
    });
    expect(
      paletteKey("ArrowRight", 0, [{ ...entry, itemId: undefined }]),
    ).toBeNull();
  });
});
