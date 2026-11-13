// Event ticker: journal `actions` -> display lines (spec §Visual grammar).
// Pure projection — no state, no fetch. Order is merged -> closed ->
// dispatched -> held -> stuck (successes read first).
//
// `closed` is the out-of-band-merged set (#2304): the PR merged and the item
// closed without kew doing it, and the tick's snapshot already paints those
// nodes DONE. This is the only reader of `actions`, so leaving it out is what
// made a node go DONE on a tick whose ticker said the tick did nothing. It
// gets its own line rather than joining `merged` because kew did not do the
// merge — a surface that cannot tell them apart cannot say so.
import type { EpicTick } from '$lib/api';

export type TickerLine = { icon: string; text: string; ts: string };

export function tickerLines(tick: EpicTick): TickerLine[] {
  const ts = tick.tick_ts;
  const lines: TickerLine[] = [];
  for (const m of tick.actions.merged ?? []) {
    lines.push({
      icon: '✓',
      text: m.approved_by ? `merged #${m.id} · approved by @${m.approved_by}` : `merged #${m.id}`,
      ts
    });
  }
  for (const id of tick.actions.closed ?? []) {
    lines.push({ icon: '✓', text: `closed #${id} · merged out of band`, ts });
  }
  for (const id of tick.actions.dispatched ?? []) {
    lines.push({ icon: '▸', text: `dispatched #${id}`, ts });
  }
  for (const id of tick.actions.held ?? []) {
    lines.push({ icon: '⏸', text: `held #${id}`, ts });
  }
  for (const s of tick.actions.stuck ?? []) {
    lines.push({ icon: '✗', text: `stuck #${s.id} · ${s.reason}`, ts });
  }
  return lines;
}
