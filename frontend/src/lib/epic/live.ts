// Live-mode pure state reducer (#454 Task C5). Extracted from
// routes/(factory)/epics/[ref]/+page.svelte so the SSE frame-application
// semantics (dedup by tick id, ticker cap, economics/heartbeat replace) are
// unit-testable without component-render infra.
import type {
  EpicAttention,
  EpicEconomics,
  EpicFrame,
  EpicHeartbeat,
  EpicRecovery,
  EpicSnapshot,
  EpicTick
} from '$lib/api';
import { tickerLines, type TickerLine } from './ticker';

export type LiveState = {
  snapshot: EpicSnapshot | null;
  economics: EpicEconomics | null;
  heartbeat: EpicHeartbeat | null;
  ticker: TickerLine[];
  lastTickId: number;
};

/** Ticker keeps at most this many lines, newest first (spec §Ticker). */
export const TICKER_CAP = 100;

export function initialLiveState(init: {
  snapshot?: EpicSnapshot | null;
  economics?: EpicEconomics | null;
  heartbeat?: EpicHeartbeat | null;
}): LiveState {
  return {
    snapshot: init.snapshot ?? null,
    economics: init.economics ?? null,
    heartbeat: init.heartbeat ?? null,
    ticker: [],
    lastTickId: init.snapshot?.tick_id ?? 0
  };
}

/** Apply one SSE frame to live state. Pure — no fetch, no mutation of the
 * input. Reconnect/replay honesty: a 'snapshot' frame whose tick id is not
 * strictly newer than the last one we applied is dropped whole (snapshot AND
 * ticker untouched) — live never re-animates history it already showed, and
 * a reconnect that skips ticks silently skips them (spec Decision: "live
 * never replays history"). A frame that IS new still has its ticker lines
 * checked against what is on screen (`unshown`), because the journal seed
 * puts lines there without moving the id watermark. */
export function applyFrame(state: LiveState, frame: EpicFrame): LiveState {
  switch (frame.type) {
    case 'snapshot': {
      const tick = frame.data;
      if (tick.id <= state.lastTickId) return state;
      return {
        ...state,
        snapshot: tick.snapshot,
        lastTickId: tick.id,
        ticker: [...unshown(state.ticker, tickerLines(tick)), ...state.ticker].slice(0, TICKER_CAP)
      };
    }
    case 'attention':
      return applyAttention(state, frame.data);
    case 'recovery':
      return applyRecovery(state, frame.data);
    case 'economics':
      return { ...state, economics: frame.data };
    case 'heartbeat':
      return { ...state, heartbeat: frame.data };
    default:
      return state;
  }
}

/** Re-annotate the snapshot already on screen with a fresh needs-you join.
 * Deliberately NOT tick-gated: attention changes with no journal row behind
 * it (a PR merged by serve's own sweep, an approval hold opening), and the
 * whole point of the frame is to refresh the diamond between ticks. Nothing
 * else moves — no ticker line, no lastTickId — so live still never replays
 * history. A node absent from `by_node` has nobody waiting on it, which is
 * trustworthy exactly while `unavailable` is empty. With no snapshot yet
 * there is nothing to annotate; the next snapshot frame carries the join
 * itself. */
function applyAttention(state: LiveState, attention: EpicAttention): LiveState {
  if (!state.snapshot) return state;
  return {
    ...state,
    snapshot: {
      ...state.snapshot,
      nodes: state.snapshot.nodes.map((n) => ({
        ...n,
        attention: attention.by_node[n.id] ?? null
      })),
      unavailable: attention.unavailable
    }
  };
}

/** Re-read each node's stuck reason and recovery command off a fresh
 * recovery frame. Not tick-gated, for the same reason as attention: a
 * continuation claimed or abandoned elsewhere changes the command with no
 * journal row behind it (#2960), and a mounted page must stop printing the
 * one it loaded with. Status, ticker and lastTickId stay where they are; a
 * node the frame does not name keeps what it has. */
function applyRecovery(state: LiveState, recovery: EpicRecovery): LiveState {
  if (!state.snapshot) return state;
  return {
    ...state,
    snapshot: {
      ...state.snapshot,
      nodes: state.snapshot.nodes.map((n) => {
        const now = recovery.by_node[n.id];
        return now
          ? {
              ...n,
              stuck: now.stuck,
              blocked_reason: now.blocked_reason,
              recovery_command: now.recovery_command
            }
          : n;
      })
    }
  };
}

/** Replay-mode ticker: ACCUMULATED lines from frames 0..uptoIndex (newest
 * frame first, each frame's lines in their natural tickerLines order),
 * capped at TICKER_CAP like live. Pure and index-windowed, so seeking
 * BACKWARD honestly shows only what the journal had recorded by that frame
 * — the demo narrative gets a building log, not a per-frame flicker. */
export function accumulatedTicker(frames: EpicTick[], uptoIndex: number): TickerLine[] {
  const lines: TickerLine[] = [];
  const end = Math.min(uptoIndex, frames.length - 1);
  for (let i = end; i >= 0 && lines.length < TICKER_CAP; i--) {
    lines.push(...tickerLines(frames[i]));
  }
  return lines.slice(0, TICKER_CAP);
}

/** One line's identity for the seed merge below. `tickerLines` is a pure
 * projection of a tick, so the same tick yields a byte-identical line whether
 * it arrived over SSE or came back in the journal drain — which makes the
 * triple the only key we need, and needs no tick id carried alongside. */
function lineKey(line: TickerLine): string {
  return `${line.ts}\u0000${line.icon}\u0000${line.text}`;
}

/** The lines of an arriving tick that the ticker is not already showing.
 * `applyFrame`'s id gate cannot see the journal seed: the seed is display-only
 * and deliberately leaves `lastTickId` alone, while the SSE hub polls the
 * journal on its own clock (POLL_S = 1s, `kew/serve/watch.py`) and the seed
 * reads the store directly. A tick committed between that poll and the seed's
 * read is therefore seeded from the drain AND delivered as a frame whose id is
 * genuinely new — which printed it twice, the second copy stacked above older
 * lines. Identity is the same triple `seededTicker` merges on, so both halves
 * agree on what counts as the same line and whichever copy landed first is the
 * one kept. A line the cap dropped is not on screen, so it is simply
 * prepended. */
function unshown(ticker: TickerLine[], lines: TickerLine[]): TickerLine[] {
  const seen = new Set(ticker.map(lineKey));
  return lines.filter((line) => !seen.has(lineKey(line)));
}

/** The journal seed merged UNDER the lines SSE has already shown (both lists
 * are newest-first). The two OVERLAP: the drain is taken after the channel
 * opened, so every tick SSE delivered before the drain read it is in both, and
 * a blind concat would print it twice. Seeding used to be skipped altogether
 * whenever anything was on screen, which on a live epic meant one SSE line
 * arriving first cost the reader the whole journal tail — the seed is one-shot,
 * so nothing came back for it. It only ever ADDS history below: a line SSE
 * showed that the drain missed (a tick recorded while it was still paging)
 * stays on top rather than being dropped. */
export function seededTicker(shown: TickerLine[], journal: EpicTick[]): TickerLine[] {
  const seen = new Set(shown.map(lineKey));
  const older = accumulatedTicker(journal, journal.length - 1).filter(
    (line) => !seen.has(lineKey(line))
  );
  return [...shown, ...older].slice(0, TICKER_CAP);
}

/** Matches the `${path} -> ${status}` shape api.ts's internal get() throws
 * (see src/lib/api.ts) — the only signal we have of the HTTP status since
 * fetch helpers don't expose it separately. Used to trigger the honesty
 * rule: any 401 sends the user to /inbox to pick up a session, never a
 * token in the URL. */
export function isUnauthorized(err: unknown): boolean {
  return err instanceof Error && /-> 401$/.test(err.message);
}
