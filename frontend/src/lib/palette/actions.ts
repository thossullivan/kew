/**
 * #2517 — what the Cmd-K palette can DO, beyond going somewhere.
 *
 * Three kinds of command, each from a source the console already trusts:
 *
 * * **offered** — an item's `WorkView.actions`, exactly as the projection
 *   offered them, and nothing else. The palette names no action of its own:
 *   a verb the server never offered has no path into this list. Each is sent
 *   through `performWorkAction` against the `view_revision` it was offered at,
 *   and a 409 re-reads the item rather than retrying blind.
 * * **copy** — a CLI-only action (dispatch), as the command string
 *   `GET /api/dispatch/resolve` composed. Copied verbatim, never assembled
 *   here, and it writes nothing.
 * * **stop the fleet** — always on offer. It needs a written reason and an
 *   explicit confirm before `POST /api/fleet/stop` is sent (`stopFleetVerb`).
 *
 * The server holds the approver rule for every write; a 403 is shown as the
 * same friendly "not an approver" the console's rows show.
 */
import {
  fetchWorkView,
  performWorkAction,
  type DispatchPlan,
  type WorkView,
} from "$lib/api";
import {
  applyWorkActionResult,
  offeredActions,
  workActionLabel,
} from "$lib/dashboard";
import { itemRef } from "$lib/utils";
import { dispatchIsNext } from "$lib/work/list";
import { stopFleetChip, stopFleetVerb } from "$lib/verbs";
import type { PaletteEntry } from "./entries";

export type PaletteCommand =
  | {
      kind: "offered";
      key: string;
      label: string;
      intent: string;
      itemId: string;
      action: string;
      /** The revision the action was offered at — sent as `expected_revision`. */
      revision: string;
      requiresReason: boolean;
    }
  | {
      kind: "copy";
      key: string;
      label: string;
      intent: string;
      itemId: string;
      text: string;
    }
  | { kind: "stop_fleet"; key: string; label: string; intent: string };

export const STOP_FLEET: PaletteCommand = {
  kind: "stop_fleet",
  key: "command:stop-fleet",
  label: "Stop the fleet",
  intent: "kill every running agent and refuse new dispatches until resumed",
};

/** The item's offered actions, in the order the projection offered them. */
export function offeredCommands(
  itemId: string,
  view: WorkView | null,
): PaletteCommand[] {
  // No view, or a view with no revision, has nothing it can be decided against.
  if (view === null || view.view_revision === "") return [];
  return offeredActions(view).map((a) => ({
    kind: "offered",
    key: `command:${itemId}:${a.action}`,
    label: workActionLabel(a.action),
    intent: a.intent,
    itemId,
    action: a.action,
    revision: view.view_revision,
    requiresReason: a.requires_reason,
  }));
}

/**
 * The copy-command entries for one item: the dispatch command the server
 * composed for it, or nothing. A plan read for another item is not this one's.
 */
export function copyCommands(
  itemId: string,
  plan: DispatchPlan | null,
): PaletteCommand[] {
  const text = plan?.command ?? "";
  if (plan === null || text === "" || plan.item !== itemId) return [];
  return [
    {
      kind: "copy",
      key: `command:${itemId}:copy-dispatch`,
      label: "Copy dispatch command",
      intent: text,
      itemId,
      text,
    },
  ];
}

export type CommandInput = {
  /** The item the commands are for — the selected or open one — or none. */
  itemId: string | null;
  view: WorkView | null;
  plan: DispatchPlan | null;
  /** Whether the ready queue holds the item (#2785). */
  queued?: boolean;
};

/**
 * Whether the palette reads and offers a dispatch for this item (#2785): only
 * where dispatch is its next move, the same rule the Work page's "Plan a
 * dispatch" is mounted on. An item in review has another move, and the key of
 * a needs-you entry row (`stalled_epic:go-live`) is no item at all.
 */
export function offersDispatch({
  itemId,
  view,
  queued = false,
}: Omit<CommandInput, "plan">): boolean {
  return itemId !== null && dispatchIsNext(view, queued);
}

/**
 * The item's offered actions, then its copy commands — those only where
 * dispatch is its next move; none without an item.
 */
export function itemCommands(input: CommandInput): PaletteCommand[] {
  const { itemId, view, plan } = input;
  if (itemId === null) return [];
  const copies = offersDispatch(input) ? copyCommands(itemId, plan) : [];
  return [...offeredCommands(itemId, view), ...copies];
}

/** Everything the palette can do: the item's commands, then the fleet stop. */
export function paletteCommands(input: CommandInput): PaletteCommand[] {
  return [...itemCommands(input), STOP_FLEET];
}

/** The item's view: one re-read after a write or a 409 wins over the projection's. */
export function viewFor(
  itemId: string | null,
  views: readonly WorkView[] | null,
  fresh: Readonly<Record<string, WorkView>> = {},
): WorkView | null {
  if (itemId === null) return null;
  return (
    fresh[itemId] ?? views?.find((v) => v.identity.item_id === itemId) ?? null
  );
}

/** The item a `/work?item=` URL has open, or none. The id stays opaque. */
export function openItemOf(url: URL): string | null {
  if (url.pathname !== "/work") return null;
  const item = url.searchParams.get("item");
  return item === null || item === "" ? null : item;
}

/** As palette entries, so they rank alongside the places. */
export function commandEntries(
  commands: readonly PaletteCommand[],
): PaletteEntry[] {
  return commands.map((command) => ({
    kind: "action",
    key: command.key,
    id: command.label,
    title:
      command.kind === "offered"
        ? `${itemRef(command.itemId, null)} — ${command.intent}`
        : command.intent,
    aliases: command.kind === "offered" ? [command.action] : [],
    href: "",
    command,
  }));
}

/** A write needs the confirm step; a copy does not. */
export function needsConfirm(command: PaletteCommand): boolean {
  return command.kind !== "copy";
}

/** Whether the confirm step must collect a non-empty reason first. */
export function needsReason(command: PaletteCommand): boolean {
  return (
    command.kind === "stop_fleet" ||
    (command.kind === "offered" && command.requiresReason)
  );
}

export type CommandOutcome = {
  chip: string | null;
  error: string | null;
  /** The item's view after the write, or after the 409 re-read. */
  view: WorkView | null;
  /** The view was re-read because the revision went stale. */
  reread: boolean;
};

export type CommandDeps = {
  fetch?: typeof fetch;
  clipboard?: Pick<Clipboard, "writeText">;
};

const refused = (error: string): CommandOutcome => ({
  chip: null,
  error,
  view: null,
  reread: false,
});

/**
 * Run one command. A write is refused here, before any request, unless it was
 * confirmed and — where it must be explained — given a reason.
 */
export async function runCommand(
  command: PaletteCommand,
  { reason = "", confirm = false }: { reason?: string; confirm?: boolean },
  deps: CommandDeps = {},
): Promise<CommandOutcome> {
  const f = deps.fetch ?? fetch;
  if (command.kind === "copy") {
    const clipboard = deps.clipboard ?? globalThis.navigator?.clipboard;
    if (!clipboard)
      return refused("The clipboard is not available here — copy it by hand.");
    try {
      await clipboard.writeText(command.text);
    } catch {
      return refused("The clipboard refused the copy — copy it by hand.");
    }
    return { chip: "copied", error: null, view: null, reread: false };
  }
  if (command.kind === "stop_fleet") {
    const res = await stopFleetVerb(reason, confirm, f);
    if (!res.ok) return refused(res.message);
    return {
      chip: stopFleetChip(res.data),
      error: null,
      view: null,
      reread: false,
    };
  }
  if (confirm !== true)
    return refused(`${command.label} needs an explicit confirm.`);
  if (command.requiresReason && reason.trim() === "")
    return refused(`${command.label} needs a reason.`);
  const out = applyWorkActionResult(
    await performWorkAction(
      command.itemId,
      command.action,
      command.revision,
      reason.trim(),
      f,
    ),
  );
  if (!out.reread)
    return { chip: out.chip, error: out.error, view: out.view, reread: false };
  // The world moved: re-read, so the next decision is made against the facts
  // as they are now, and say so — a silent re-read would look as if it landed.
  let view: WorkView | null = null;
  try {
    view = await fetchWorkView(command.itemId, f);
  } catch {
    view = null;
  }
  const said =
    view === null
      ? "it could not be re-read"
      : "it has been re-read — choose again";
  return { chip: null, error: `${out.error} (${said})`, view, reread: true };
}
