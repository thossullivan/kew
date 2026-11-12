/**
 * #2512 — the console's one key layer.
 *
 * Every surface used to own its own `keydown` handler (the Work list, the
 * Epic DAG, the timeline and the epic page's switch), each restating the same
 * two refusals and none of them knowing about the others. A palette chord, a
 * `?` overlay and a global jump each need ONE place that knows every shortcut,
 * so this is it:
 *
 * * a surface REGISTERS its shortcuts — key, description, handler, scope — and
 *   gets back the function that takes them away again, which is exactly the
 *   shape a Svelte `$effect` cleanup wants;
 * * ONE window listener dispatches every press, installed with the first
 *   registration and removed with the last, so a page mounted on its own (a
 *   client test, a route outside the factory shell) is answered as well;
 * * the legends read the same definitions the surfaces register (`legendOf`).
 *
 * Five rules are the registry's rather than any surface's, so no surface can
 * forget one:
 *
 * * **A press while the reader is typing belongs to the field.** Nothing fires
 *   from an input, a textarea, a `<select>` or anything `contenteditable` — `j`
 *   goes into the search box rather than moving the selection out from under
 *   it. A shortcut may say otherwise (`inFields`): the epic page's Esc still
 *   closes from the replay scrubber, as it did before this layer. The palette
 *   chord is the one reserved press a field does not keep (#2781): it types
 *   nothing, and from the palette's own field it is how the palette closes.
 * * **Nothing reaches the page behind an open modal** (#2781). A box with
 *   `aria-modal` — the palette, the help overlay, the launchpad drawer, a
 *   Config dialog — holds `suspendShortcuts()` while it is open, and every
 *   page shortcut is dropped until the last one lets go. The reserved presses
 *   still go to their claimants, so ⌘K still closes the palette.
 * * **A platform modifier belongs to the browser.** ⌘R, ^W and ⌥← are never
 *   offered to a surface. Shift is not one of them: it is how `T` and `?` are
 *   typed at all.
 * * **Two presses are reserved.** `Meta+K` / `Ctrl+K` is the palette and `?`
 *   is help. They are the only chords, and registering either from a surface
 *   throws in development, so a page cannot quietly take them.
 * * **Tab is never trapped** (DESIGN.md: "focus always leaves a widget"). The
 *   registry never calls `preventDefault` itself; a handler CLAIMS a press by
 *   preventing its default, and a handler that leaves a press alone leaves it
 *   to the next surface and then to the browser. A Tab nothing claims moves
 *   the focus, as it always does.
 */

/** A surface's name for its own shortcuts: `work`, `dag`, `timeline`, `epic`. */
export type Scope = string;

/** One shortcut as a legend reads it — no handler, so it can live in a table. */
export type KeyDef = {
  /** `KeyboardEvent.key`, exactly: `j`, `Enter`, `ArrowLeft`. A shifted
   * letter is a different key, so a surface that answers both registers both. */
  readonly key: string;
  /** What the press does, in the legend's words. Keys that share one are
   * drawn as one hint (`j k  move`). */
  readonly description: string;
  /** Whether the legend draws it. `'wide'` draws it only where there is room. */
  readonly legend?: boolean | 'wide';
  /** Whether the press is offered even from a field. Only for a key a field
   * has no use for — Esc dismisses rather than types — and the handler still
   * reads the target for anything it must leave to the field. */
  readonly inFields?: boolean;
  /** Where the press answers, when that is not the whole page — the `?`
   * overlay adds it to the description (`on the event track`, #2841). */
  readonly where?: string;
};

/** One registered shortcut. */
export type Shortcut = KeyDef & {
  readonly scope: Scope;
  /** Called with the press. It claims the press by calling `preventDefault`;
   * one that does not leaves the press to the next surface and the browser. */
  readonly handler: (event: KeyboardEvent) => void;
};

/** The two reserved presses. */
export type Reserved = 'palette' | 'help';

/** The part of a `KeyboardEvent` the dispatcher reads. */
export type KeyPress = {
  readonly key: string;
  readonly metaKey: boolean;
  readonly ctrlKey: boolean;
  readonly altKey: boolean;
  readonly target: unknown;
};

/** A chord spelled the way an author would write one: `Meta+K`, `Ctrl+Shift+P`. */
const CHORD = /^(?:Meta|Cmd|Ctrl|Control|Alt|Option)\+./i;

/**
 * Whether the press is one of the two reserved ones, and which.
 *
 * `Meta+K` and `Ctrl+K` are the palette whichever case the `k` arrives in (a
 * held Shift or Caps Lock still means the palette), but only without Alt —
 * ⌥⌘K is the browser's. `?` is help, and only bare: with a modifier it is
 * somebody else's chord.
 */
export function reservedPress(press: Omit<KeyPress, 'target'>): Reserved | null {
  if ((press.metaKey || press.ctrlKey) && !press.altKey && press.key.toLowerCase() === 'k') {
    return 'palette';
  }
  if (press.key === '?' && !press.metaKey && !press.ctrlKey && !press.altKey) return 'help';
  return null;
}

/**
 * Whether the press landed in something the reader is typing into: an input,
 * a textarea, anything `contenteditable`, or a `<select>` — whose letters jump
 * to an option and whose arrows walk them (#2781).
 */
export function isTyping(target: unknown): boolean {
  if (typeof HTMLElement === 'undefined' || !(target instanceof HTMLElement)) return false;
  return (
    target instanceof HTMLInputElement ||
    target instanceof HTMLTextAreaElement ||
    target instanceof HTMLSelectElement ||
    target.isContentEditable === true
  );
}

/** Why a definition cannot be registered, or `null` when it can. */
function refusal(def: KeyDef): string | null {
  if (CHORD.test(def.key)) {
    return `"${def.key}" is a chord; Meta+K / Ctrl+K (the palette) is the only one, and it is reserved`;
  }
  if (def.key === '?') return '"?" is reserved for help';
  if (def.key === '') return 'a shortcut needs a key';
  return null;
}

/** Throw in development, where the author can fix it; say so in production,
 * where throwing would take the page down with it. */
function refuse(message: string): void {
  if (import.meta.env.DEV) throw new Error(`keys: ${message}`);
  console.error(`keys: ${message}`);
}

let registered: Shortcut[] = [];
const reservedHandlers = new Map<Reserved, (event: KeyboardEvent) => void>();
let listening: { target: Window; listener: (event: KeyboardEvent) => void } | null = null;
/** The open modals holding the page's shortcuts off, one token each. */
const suspensions = new Set<symbol>();

/** Install the one window listener if anything needs it, remove it if not. */
function sync(): void {
  const wanted = registered.length > 0 || reservedHandlers.size > 0;
  if (wanted && listening === null && typeof window !== 'undefined') {
    const listener = (event: KeyboardEvent): void => dispatchKey(event);
    window.addEventListener('keydown', listener);
    listening = { target: window, listener };
  } else if (!wanted && listening !== null) {
    listening.target.removeEventListener('keydown', listening.listener);
    listening = null;
  }
}

/**
 * Register a surface's shortcuts; returns the function that unregisters them.
 *
 * Refuses — throwing in development — a reserved press, any chord, an empty
 * key, and a key its own scope already holds (two handlers for one key in one
 * surface is a table with a typo in it). Two SCOPES may share a key: the DAG
 * and the timeline both answer `F`, and the one whose handler claims the press
 * is the one it was aimed at.
 */
export function registerShortcuts(shortcuts: readonly Shortcut[]): () => void {
  const accepted: Shortcut[] = [];
  for (const s of shortcuts) {
    const why =
      refusal(s) ??
      ([...registered, ...accepted].some((r) => r.scope === s.scope && r.key === s.key)
        ? `"${s.key}" is registered twice in scope "${s.scope}"`
        : null);
    if (why !== null) {
      refuse(why);
      continue;
    }
    accepted.push(s);
  }
  registered = [...registered, ...accepted];
  sync();
  return () => {
    registered = registered.filter((s) => !accepted.includes(s));
    sync();
  };
}

/**
 * Take one of the two reserved presses. For the shell's own palette and help
 * overlay, never a surface: a second claim on the same press is refused.
 */
export function claimReserved(which: Reserved, handler: (event: KeyboardEvent) => void): () => void {
  if (reservedHandlers.has(which)) {
    refuse(`"${which}" is already claimed`);
    return () => {};
  }
  reservedHandlers.set(which, handler);
  sync();
  return () => {
    if (reservedHandlers.get(which) === handler) reservedHandlers.delete(which);
    sync();
  };
}

/**
 * Hold every page shortcut off while a modal is open; returns the function
 * that lets go — once, however often it is called, so a Svelte `$effect`
 * cleanup can be it. Two modals open at once (the palette over the launchpad
 * drawer) each hold their own, and the page answers again only when both have
 * let go. The reserved presses are never suspended: they are the modals' own.
 */
export function suspendShortcuts(): () => void {
  const token = Symbol('suspension');
  suspensions.add(token);
  return () => {
    suspensions.delete(token);
  };
}

/** Whether an open modal is holding the page's shortcuts off. */
export function shortcutsSuspended(): boolean {
  return suspensions.size > 0;
}

/**
 * Offer one press to whoever registered it.
 *
 * In order: a reserved press goes to its claimant (and is claimed for it, so
 * ⌘K does not also open the browser's search bar) — the palette chord from
 * anywhere, `?` never from a field, where it is a character; any other
 * modifier press is the browser's; while a modal holds the registry
 * suspended, nothing else is offered; a press in a field is the field's, bar
 * the shortcuts that asked for it (`inFields`); then the shortcuts
 * for that key are offered newest first — the surface mounted last is the one
 * on top — until one claims it.
 */
export function dispatchKey(event: KeyboardEvent): void {
  const typing = isTyping(event.target);
  const reserved = reservedPress(event);
  if (reserved !== null) {
    if (typing && reserved === 'help') return;
    const handler = reservedHandlers.get(reserved);
    if (handler) {
      event.preventDefault();
      handler(event);
    }
    return;
  }
  if (event.metaKey || event.ctrlKey || event.altKey) return;
  if (suspensions.size > 0) return;
  const candidates = registered
    .filter((s) => s.key === event.key && (!typing || s.inFields === true))
    .reverse();
  for (const s of candidates) {
    s.handler(event);
    if (event.defaultPrevented) return;
  }
}

/** What is registered now, in registration order — every scope, or one. */
export function registeredShortcuts(scope?: Scope): readonly Shortcut[] {
  return scope === undefined ? [...registered] : registered.filter((s) => s.scope === scope);
}

/** One hint in a legend: the keys that share a description, and it. */
export type KeyHint = { keys: readonly string[]; label: string; wide?: boolean };

/**
 * A legend from the definitions a surface registers: one hint per
 * description, in the order the descriptions first appear, carrying every
 * drawn key that shares it — with the `'wide'` hints last, since they are the
 * ones a narrow legend drops off its end. So the keys a reader is told about
 * and the keys the surface answers to are one list.
 */
export function legendOf(defs: readonly KeyDef[]): KeyHint[] {
  const hints: KeyHint[] = [];
  for (const def of defs) {
    if (!def.legend) continue;
    const hint = hints.find((h) => h.label === def.description);
    if (hint) {
      hint.keys = [...hint.keys, def.key];
      continue;
    }
    hints.push(
      def.legend === 'wide'
        ? { keys: [def.key], label: def.description, wide: true }
        : { keys: [def.key], label: def.description }
    );
  }
  return [...hints.filter((h) => !h.wide), ...hints.filter((h) => h.wide)];
}

/** Bind a table of definitions to one handler, for `registerShortcuts`. */
export function bindShortcuts(
  scope: Scope,
  defs: readonly KeyDef[],
  handler: (event: KeyboardEvent) => void
): Shortcut[] {
  return defs.map((def) => ({ ...def, scope, handler }));
}

/** How a legend draws a key: `↵` and `esc` read better than their names. */
export const KEY_GLYPH: Readonly<Record<string, string>> = {
  Enter: '↵',
  Escape: 'esc',
  Tab: 'tab',
  ArrowUp: '↑',
  ArrowDown: '↓',
  ArrowLeft: '←',
  ArrowRight: '→'
};

/** The glyph for a key, or the key itself. Own-property lookup, so a key
 * called `constructor` is drawn as itself. */
export function keyGlyph(key: string): string {
  return Object.hasOwn(KEY_GLYPH, key) ? KEY_GLYPH[key] : key;
}

/** Drop every registration and claim — for tests, so one cannot leak a
 * handler into the next. */
export function resetShortcuts(): void {
  registered = [];
  reservedHandlers.clear();
  suspensions.clear();
  sync();
}
