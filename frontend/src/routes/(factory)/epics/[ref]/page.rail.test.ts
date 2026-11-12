/**
 * FUI-17 — the epic page keeps the selected-node rail on ONE moment.
 *
 * The rail draws from two clocks. Its Selected and Needs-you columns read the
 * node off the snapshot on screen, which the SSE tail moves under the page on
 * every driver tick and which replay rewinds wholesale. Its other three
 * columns are fed by four reads the page takes of `kew serve` as it is *now*.
 *
 * Keyed on the selection alone — which is how this shipped — those clocks
 * drift the moment anything happens: a merge lands, the node's attention
 * clears, the Needs-you column disappears, and beside the gap sits a pipeline
 * read from before the merge with its merge stage still magenta. The rail is
 * then asserting two moments at once, which is the one thing `rail.ts` exists
 * to prevent.
 *
 * `rail.test.ts` pins the model's half (`nodeMoment`, and what `replay` dashes).
 * This suite pins the page's half — the two dependencies that make the reads
 * follow the graph — and it does it against the component's source, the way
 * `page.strip.test.ts` does, because there is no DOM in this suite to mount.
 */
import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

const page = readFileSync(new URL('./+page.svelte', import.meta.url), 'utf8');

/** The `<script>` with its comments stripped: prose is not a dependency. */
const script = page
  .slice(0, page.indexOf('</script>'))
  .replace(/\/\*[\s\S]*?\*\//g, '')
  .replace(/^\s*\/\/.*$/gm, '');

/**
 * The body of the `$effect` that takes the four per-selection reads, found by
 * the first read it makes rather than by position — so it is still the right
 * effect after the file is reordered.
 */
const reads = (() => {
  const anchor = script.indexOf('take(fetchEpicNode(');
  if (anchor === -1) throw new Error('the page no longer takes the four node reads');
  const open = script.lastIndexOf('$effect(() => {', anchor);
  if (open === -1) throw new Error('the node reads are no longer inside an $effect');
  return script.slice(open, anchor);
})();

describe('the four per-selection reads', () => {
  it('are retaken when the node moves, not only when the selection changes', () => {
    // Without this dependency the reads are taken once and then held for as
    // long as the node stays selected, however far the graph moves on.
    expect(reads).toContain('selectionMoment');
  });

  it('key on the node\'s facts, not on the snapshot object', () => {
    // `selectedNode` is rebuilt by every SSE frame, so depending on it would
    // re-read four endpoints per driver tick; `nodeMoment` compares by value.
    expect(script).toContain('const selectionMoment = $derived(nodeMoment(selectedNode));');
    expect(reads).not.toMatch(/\bselectedNode\b/);
  });

  it('are not taken at all under replay', () => {
    // They are reads of NOW. There is no "pipeline as of tick 41" endpoint,
    // so under replay the page takes none and clears the ones it holds.
    expect(reads).toMatch(/mode === 'replay'/);
    expect(reads).toContain('clearNodeReads();');
  });
});

describe('what the page hands the rail', () => {
  it('tells it which clock the graph is on', () => {
    // Cleared reads alone would read "not read yet", which is a claim about
    // the server; `replay` is what makes the columns say the honest thing.
    expect(script).toContain("replay: mode === 'replay',");
  });

  it('still hands it the frame\'s own facts, which replay does not blank', () => {
    expect(script).toContain('node: selectedNode,');
    expect(script).toContain('edges: currentSnapshot?.edges ?? [],');
    expect(script).toContain('unavailable: currentSnapshot?.unavailable ?? [],');
  });
});
