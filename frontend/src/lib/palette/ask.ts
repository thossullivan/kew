/**
 * #2518 — ask mode: a model ranks the entries the palette already offers.
 *
 * The browser cannot call a model (the CSP forbids it), so a query ending in
 * `?` — or `Tab` on any query — is sent to `POST /api/palette/ask` with the id
 * and label of every entry the palette holds, and nothing else: no href, no
 * item body, no token. The server drops any id it was not sent; this side
 * drops one too, and maps every id it keeps back to the SAME entry object it
 * sent. So a ranked result is not a new kind of row: it is the entry itself,
 * and selecting it is the palette's ordinary press — `paletteKey`, `choose`,
 * the confirm step — never a path of its own.
 *
 * Off unless the server says `palette_ask` in `/api/meta`; while off the
 * palette shows no hint, a `?` query is just text, and `Tab` is the browser's.
 *
 * Over the day's budget the server answers 429 with one sentence naming the
 * cap (#2676), and the demo build answers 405 with its "Demo: this would…"
 * sentence (#3139). Either comes back as an answer with no entries and the
 * sentence as its `refusal`: the list shows the sentence in place of results, the query
 * counts as asked (so Enter does not ask again), and with nothing listed Enter
 * has nothing to select.
 */
import {
  askPaletteModel,
  PaletteAskRefused,
  type PaletteAskEntry,
} from "$lib/api";
import { KIND_LABEL, type PaletteEntry } from "./entries";

/** The most entries one ask carries — the server's own bound. */
export const ASK_MAX_ENTRIES = 500;

/** A query the palette asks about on Enter: some words, then a `?`. */
export function isAskQuery(query: string): boolean {
  const q = query.trim();
  return q.length > 1 && q.endsWith("?");
}

/**
 * Whether a key press asks, given whether the server allows it. Enter asks on
 * a `?` query, Tab on any query; a query already asked does not ask again
 * (each ask is a model call the operator pays for) — Enter then selects.
 */
export function asksOn(
  key: string,
  query: string,
  { enabled, asked }: { enabled: boolean; asked: string | null },
): boolean {
  if (!enabled || query.trim() === "" || asked === query) return false;
  if (key === "Tab") return true;
  return key === "Enter" && isAskQuery(query);
}

/** What the model reads for one entry: its kind, id and title — no more. */
export function askLabel(entry: PaletteEntry): string {
  return [KIND_LABEL[entry.kind], entry.id, entry.title]
    .filter((s) => s !== "")
    .join(" ");
}

/** The request body: the query and each entry's key and label. */
export function askPayload(
  query: string,
  entries: readonly PaletteEntry[],
): { query: string; entries: PaletteAskEntry[] } {
  return {
    query: query.trim(),
    entries: entries
      .slice(0, ASK_MAX_ENTRIES)
      .map((e) => ({ id: e.key, label: askLabel(e) })),
  };
}

/**
 * A ranked answer: the entries themselves, best first, and why each — or, when
 * the server refused over budget, no entries and its sentence as `refusal`.
 */
export type AskAnswer = {
  query: string;
  entries: PaletteEntry[];
  reasons: Record<string, string>;
  refusal?: string;
};

/**
 * Map the server's ranked ids back onto the entries that were sent. An id the
 * request did not carry is dropped here as well as on the server, and each
 * kept id yields the very entry object it names — so whatever the palette does
 * with it is what it does with that entry.
 */
export function askedEntries(
  query: string,
  sent: readonly PaletteEntry[],
  results: ReadonlyArray<{ id: string; reason: string }>,
): AskAnswer {
  const byKey = new Map(sent.slice(0, ASK_MAX_ENTRIES).map((e) => [e.key, e]));
  const entries: PaletteEntry[] = [];
  const reasons: Record<string, string> = {};
  for (const r of results) {
    const entry = byKey.get(r.id);
    if (entry === undefined || entry.key in reasons) continue;
    entries.push(entry);
    reasons[entry.key] = r.reason;
  }
  return { query, entries, reasons };
}

/** Ask, and map the answer back. A failure comes back as a line to show. */
export async function askPalette(
  query: string,
  entries: readonly PaletteEntry[],
  f: typeof fetch = fetch,
): Promise<
  { answer: AskAnswer; error: null } | { answer: null; error: string }
> {
  try {
    const out = await askPaletteModel(askPayload(query, entries), f);
    return { answer: askedEntries(query, entries, out.results), error: null };
  } catch (e) {
    if (e instanceof PaletteAskRefused) {
      return {
        answer: { query, entries: [], reasons: {}, refusal: e.sentence },
        error: null,
      };
    }
    return { answer: null, error: "The ask could not be answered just now." };
  }
}
