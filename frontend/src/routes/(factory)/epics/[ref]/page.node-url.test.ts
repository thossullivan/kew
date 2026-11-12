/**
 * #2250 — the epic page's selection is the URL, not component state.
 *
 * It was `let selected = $state<string | null>(null)`: a link could not land
 * on a node and Back could not undo a selection (P7 of the UX brief). Four
 * claims about the replacement, each of which is a way URL selection goes
 * wrong:
 *
 * 1. **The URL is read, not mirrored.** A second copy in component state is
 *    two readings of where the reader is, and they drift the moment one is
 *    written without the other.
 * 2. **An unknown id selects nothing.** A stale or hand-typed `?node=` must
 *    not put the rail on a node this epic does not have — and must not take
 *    the four per-selection reads for one.
 * 3. **Selecting is `replaceState`.** Walking the graph with the arrow keys
 *    would otherwise push one history entry per node, so Back would have to
 *    unwind every node the reader passed. This is `work/+page.svelte`'s rule
 *    for the same move.
 * 4. **Esc is the page's, and it is the reducer's.** The popover closes with
 *    the selection intact, then the selection goes — and the two steps come
 *    from `escape` in `$lib/epic/node-popover`, not from a second copy of the
 *    rule written in the markup.
 *
 * Asserted against the component's source, the way `page.rail.test.ts` and
 * `page.strip.test.ts` are: there is no DOM in this suite to mount.
 */
import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { escape, NO_FOCUS } from '$lib/epic/node-popover';

const page = readFileSync(new URL('./+page.svelte', import.meta.url), 'utf8');

/** The `<script>` with its comments stripped: prose is not behaviour. */
const script = page
  .slice(0, page.indexOf('</script>'))
  .replace(/\/\*[\s\S]*?\*\//g, '')
  .replace(/^\s*\/\/.*$/gm, '');

describe('the selection lives in ?node=', () => {
  it('reads it off the URL rather than holding a copy', () => {
    expect(script).toContain("page.url.searchParams.get('node')");
    // `page.state.node` first: it is what `replaceState` updates, and
    // `page.url` is not (shallow routing).
    expect(script).toMatch(
      /page\.state\.node !== undefined \? page\.state\.node : page\.url\.searchParams\.get\('node'\)/
    );
    expect(script).toMatch(/const selected = \$derived</);
    // The old state. A second copy of the selection is a second reading of it.
    expect(script).not.toMatch(/let selected = \$state/);
  });

  it('selects nothing for an id the snapshot does not have', () => {
    // The guard is against the SNAPSHOT, so the rail and the four reads are
    // never pointed at a node this epic has no facts for. BOTH candidates go
    // through it: the URL's id, and the latched opening pick, which is an id
    // off the FIRST snapshot and so can name a node a later frame (or a
    // replay seek) no longer carries (review r9).
    expect(script).toContain(
      'const selected = $derived<string | null>(onScreen(wanted) ?? onScreen(opening));'
    );
    expect(script).toMatch(
      /function onScreen\(id: string \| null\): string \| null \{\s*if \(id == null\) return null;\s*return \(currentSnapshot\?\.nodes \?\? \[\]\)\.some\(\(n\) => n\.id === id\) \? id : null;/
    );
  });

  it('writes it with replaceState, and deletes it rather than writing empty', () => {
    // One write path for both halves of this page's URL state since #2271
    // (the Graph / Timeline switch): `onselect` hands `writeUrl` the node and
    // `onview` hands it the view, and `writeUrl` is where the rules below
    // live.
    expect(script).toMatch(
      /function onselect\(id: string \| null\): void \{\s*picked = true;\s*writeUrl\(\{ node: id \}\);/
    );
    const fn = script.slice(script.indexOf('function writeUrl('));
    const body = fn.slice(0, fn.indexOf('\n  }'));
    // The selection rides `page.state` as well as the address bar: shallow
    // routing does not update `page.url`, so a page reading the URL alone
    // wrote `?node=` on every click and never moved the rail (#2250, seen in
    // the browser).
    expect(body).toMatch(
      /replaceState\(url, \{\s*\.\.\.page\.state,\s*node: id,\s*epicView: to,\s*select: sel \|\| null,\s*epicMode: replaying \? 'replay' : 'live',\s*replayT: t,\s*replaySpeed: speed\s*\}\)/
    );
    // #2544: the lineage selection rides the same write, and is read back
    // through `page.state` first like the other two.
    expect(body).toContain("url.searchParams.delete('select')");
    expect(script).toMatch(
      /page\.state\.select !== undefined \? page\.state\.select : page\.url\.searchParams\.get\('select'\)/
    );
    expect(body).toContain("url.searchParams.delete('node')");
    expect(body).toContain("url.searchParams.set('node', id)");
    // `?node=` with nothing after it is not "no selection"; it is a selection
    // of the empty id, which `selected` would then have to special-case.
    expect(body).not.toMatch(/set\('node', ''\)/);
  });

  it('opens on the node that needs a person until the reader picks one (D20)', () => {
    // The rail used to hold 195px open for one sentence beside a graph that
    // already knew which node was waiting (design review C2, D20). The
    // default is NOT written to the URL: it is not a place anybody navigated
    // to, and a link that names no node must keep meaning "the epic".
    expect(script).toContain(
      'const opening = $derived<string | null>(picked ? null : openingPick);'
    );
    expect(script).toContain('onScreen(wanted) ?? onScreen(opening)');
    // Decided ONCE, by the latch, and not re-derived over the snapshot on
    // screen: an SSE frame that changed which node was first-needs-you moved
    // the selection under the reader (review r8, and
    // `page.opening.client.test.ts` is the behavioural regression).
    expect(script).toMatch(
      /\$effect\(\(\) => \{\s*const nodes = currentSnapshot\?\.nodes \?\? \[\];/
    );
    expect(script).toMatch(/if \(openingDecided \|\| nodes\.length === 0\) return;/);
    expect(script).toMatch(
      /openingDecided = true;\s*openingPick = nodes\.find\(\(n\) => needsYou\(n\)\)\?\.id \?\? null;/
    );
    expect(script).not.toMatch(/const opening = \$derived[^;]*currentSnapshot/);
    // And decided again for the next epic, from that epic's own snapshot.
    expect(script).toMatch(/openingPick = null;\s*openingDecided = false;/);
    // And the reader's own Esc is final: `onselect(null)` records the
    // decision, so the default is not handed straight back.
    expect(script).toMatch(/let picked = \$state\(/);
    // A view switch writes `node: null` as well, which is why the decision is
    // state rather than a reading of `page.state.node !== undefined`. Seeded
    // from the URL twice over: at init for the first render of a `?node=`
    // link, and on every later ref.
    expect(script).toContain("let picked = $state(page.url.searchParams.get('node') !== null)");
    expect(script).toContain("picked = untrack(() => page.url.searchParams.get('node') !== null)");
  });

  it('tells the canvas whose selection it is drawing, so a default opens no popover', () => {
    // A popover nobody asked for is the covered card D5 and D19 are about.
    expect(page).toContain('linked={picked}');
  });

  it('carries the view through a selection, and the selection through a switch', () => {
    // `replaceState` REPLACES the page state. A write naming one of the two
    // readings drops the other — and `page.state` is the reading that wins,
    // so the shared `?node=` the two views are meant to hand each other would
    // vanish on the way across.
    const fn = script.slice(script.indexOf('function writeUrl('));
    const body = fn.slice(0, fn.indexOf('\n  }'));
    // The URL's own reading, not the snapshot-validated `selected`: with no
    // snapshot loaded yet, `selected` is null and a switch erased ?node=.
    expect(body).toContain('const id = next.node !== undefined ? next.node : wanted;');
    expect(body).toContain('const to = next.view !== undefined ? next.view : view;');
    expect(script).toMatch(/function onview\(to: CanvasView\): void \{\s*writeUrl\(\{ view: to \}\);/);
  });
});

describe('Esc is the reducer, once, for the whole page', () => {
  it('is on the key layer\'s window listener, so Esc works from the rail as well as the graph', () => {
    // #2512: the console's one key layer listens on the window; the page
    // registers its Esc and `T` there rather than binding a listener of its own.
    expect(page).toContain("registerShortcuts(bindShortcuts('epic', EPIC_KEYS, onPageKey))");
    expect(page).not.toMatch(/<svelte:window onkeydown/);
  });

  it('delegates both steps to `escape` instead of restating them', () => {
    const fn = script.slice(script.indexOf('function onPageKey('));
    const body = fn.slice(0, fn.indexOf('\n  }'));
    expect(body).toContain('escapeFocus({ selected, popover })');
    // The handler itself decides nothing: it applies what the reducer returned.
    expect(body).toContain('popover = next.popover');
    expect(body).not.toMatch(/popover\s*=\s*(true|false)/);
  });

  it('and the reducer is the brief`s two steps', () => {
    // Pinned here as well as in `node-popover.test.ts` because this is the
    // page that spends it: the first press must leave the rail where it is.
    expect(escape({ selected: '2219', popover: true })).toEqual({
      selected: '2219',
      popover: false
    });
    expect(escape({ selected: '2219', popover: false })).toEqual(NO_FOCUS);
  });

  it('hands the canvas the popover half rather than letting it own one', () => {
    expect(page).toMatch(/<DagView[\s\S]*?\{popover\}[\s\S]*?\{onpopover\}/);
    expect(script).toMatch(/let popover = \$state\(false\)/);
    // A per-epic reset clears the open box; the SELECTION is the URL's, so it
    // is deliberately not reset here.
    expect(script).toMatch(/popover = false;\s*clearNodeReads\(\);/);
  });
});
