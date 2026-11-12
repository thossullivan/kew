/**
 * FUI-14 — what the epic page's header strip may claim, and when it may not.
 *
 * The strip is the one part of this page that renders before (and without) a
 * snapshot, which is exactly why it can lie: every number on it is a reading of
 * something — the heartbeat, the snapshot, the journal — and a reading printed
 * when nothing was read is worse than a blank. Two claims are pinned here
 * because both were shipped wrong once:
 *
 * 1. An unreachable `kew serve` must not produce driver readings. `data.detail`
 *    is null, so `staleness(null, now)` is 'unknown' and `lastTick` is an em
 *    dash — which the markup would otherwise print as "last tick —" beside "no
 *    heartbeat yet", two confident statements about a driver nobody reached.
 * 2. The replay/live toggle is a mode switch, not a progress indicator. The
 *    control must stay pressable while the journal paginates (up to
 *    MAX_TICK_PAGES requests), and leaving replay must abandon that fetch
 *    rather than let it land in a mode the operator has left.
 *
 * Both live in the component, which this suite has no DOM to mount — so they
 * are asserted against its source, the same way `run-links.test.ts` holds the
 * surfaces to the one link table.
 */
import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

const page = readFileSync(new URL('./+page.svelte', import.meta.url), 'utf8');

/**
 * The markup, with its comments stripped: a `{#if}` written inside a `<!-- -->`
 * (or inside the script's prose) guards nothing, and counting it would pair the
 * wrong braces.
 */
const markup = page.slice(page.indexOf('</script>')).replace(/<!--[\s\S]*?-->/g, '');

/**
 * The span of the Svelte block opened at `open`, from `{#...}` to its matching
 * `{/...}`, counting opens against closes — so a guard's span is everything it
 * actually governs, nested blocks and all.
 */
function blockSpan(source: string, open: number): [number, number] {
  const token = /\{[#/]/g;
  token.lastIndex = open;
  let depth = 0;
  for (let m = token.exec(source); m; m = token.exec(source)) {
    depth += source[m.index + 1] === '#' ? 1 : -1;
    if (depth === 0) return [open, m.index];
  }
  throw new Error(`unclosed block at ${open}`);
}

const GUARD = '{#if connected}';

/** Every span the markup puts behind `{#if connected}`. */
function guards(): Array<[number, number]> {
  const spans: Array<[number, number]> = [];
  for (let i = markup.indexOf(GUARD); i !== -1; i = markup.indexOf(GUARD, i + 1))
    spans.push(blockSpan(markup, i));
  return spans;
}

const GUARDED = guards();

/**
 * Where `needle` sits in the markup — and that it sits in exactly one place, so
 * a moved or duplicated line fails loudly instead of testing the wrong span.
 * Throws rather than asserting: it is called from describe bodies too.
 */
function at(needle: string): number {
  const i = markup.indexOf(needle);
  if (i === -1) throw new Error(`the markup no longer contains ${needle}`);
  if (markup.indexOf(needle, i + 1) !== -1) throw new Error(`${needle} appears more than once`);
  return i;
}

function isGuarded(needle: string): boolean {
  const i = at(needle);
  return GUARDED.some(([start, end]) => i > start && i < end);
}

describe('the header strip when kew serve never answered', () => {
  it('guards something at all — the whole suite is vacuous otherwise', () => {
    expect(GUARDED.length).toBeGreaterThan(0);
  });

  it('still names the epic and its repo: those are facts with the server down', () => {
    expect(isGuarded('<h1 class="text-26 text-text-1">{ref}</h1>')).toBe(false);
    expect(isGuarded('{#if repo}')).toBe(false);
  });

  it('prints no "last tick" reading, because no heartbeat was read', () => {
    expect(isGuarded('last tick <span class="text-text-2">{lastTick}</span>')).toBe(true);
  });

  it('makes no staleness claim — "no heartbeat yet" is a claim about a driver', () => {
    expect(isGuarded('no heartbeat yet')).toBe(true);
    expect(isGuarded('driver last seen')).toBe(true);
    expect(isGuarded("{#if st.kind === 'fresh'}")).toBe(true);
  });

  it('offers neither replay nor the ticker, which read the journal', () => {
    expect(isGuarded('{#if canReplay}')).toBe(true);
    expect(isGuarded('aria-expanded={tickerOpen}')).toBe(true);
  });

  it('says the one true thing in the body instead', () => {
    expect(markup).toContain('start kew serve to see this epic.');
  });
});

describe('the replay/live toggle', () => {
  /** The toggle's own markup: the `{#if canReplay}` block. */
  const [start, end] = blockSpan(markup, at('{#if canReplay}'));
  const toggle = markup.slice(start, end);

  it('switches on the mode, in both directions', () => {
    expect(toggle).toContain("onclick={mode === 'replay' ? exitReplay : enterReplay}");
    expect(toggle).toContain("{mode === 'replay' ? 'live' : 'replay'}");
  });

  it('stays pressable while the journal paginates, so replay is never a trap', () => {
    // `disabled={replayLoading}` held the control for every page of
    // fetchAllTicks — up to MAX_TICK_PAGES round trips with no way back to live.
    expect(toggle).not.toMatch(/\bdisabled\b/);
  });

  it('never words a loading state it cannot be in', () => {
    // enterReplay sets mode='replay' BEFORE replayLoading=true, so a
    // `replayLoading ? 'loading…'` arm of this label is unreachable text.
    expect(toggle).not.toContain('loading');
  });
});

describe('leaving replay', () => {
  /** `exitReplay`'s body, from the script. */
  const body = (() => {
    const open = page.indexOf('function exitReplay(): void {');
    if (open === -1) throw new Error('exitReplay is gone');
    return page.slice(open, page.indexOf('\n  }', open));
  })();

  it('abandons the journal fetch entering it started', () => {
    // The epoch bump is what makes an in-flight fetchAllTicks drop its pages
    // instead of refilling replayFrames for a mode the operator has left.
    expect(body).toContain('replayGen += 1');
  });

  it('clears the loading flag itself, since the bump stops the fetch from doing it', () => {
    // enterReplay's `finally` only clears replayLoading for its own epoch; a
    // flag left true here would make every later press of `replay` a no-op
    // (enterReplay returns early on replayLoading).
    expect(body).toContain('replayLoading = false');
    expect(page).toContain('if (!canReplay || replayLoading) return;');
  });

  it('drops the frames and the error with it', () => {
    expect(body).toContain("mode = 'live'");
    expect(body).toContain('replayFrames = []');
    expect(body).toContain('replayError = false');
  });
});

/**
 * #2205 — every time on this page reads in one zone, with its UTC instant
 * underneath.
 *
 * The header printed `last tick 16:26:16` beside a Monitor whose rows read
 * 14:26 for the same moment. The zone itself is `$lib/time`'s business and is
 * tested there; what has to hold *here* is that this page formats nothing of
 * its own and that each rendered time still carries its title — a `title=`
 * dropped in a later edit is invisible until somebody in another zone asks
 * what "16:26:16" means.
 */
describe('one zone', () => {
  it('formats through the shared formatter and nothing else', () => {
    expect(page).toContain("import { fmtTime, utcTitle } from '$lib/time';");
    expect(page).not.toContain('fmtTimeUTC');
    expect(page).not.toContain('toLocaleTimeString');
  });

  it('titles every time it draws with the UTC instant behind it', () => {
    // The header's own reading, plus the three the driver-state lines and the
    // ticker rows draw. `lastTickTs` is held separately from `lastTick` so a
    // driver that has not ticked gets no tooltip on its "no tick recorded".
    expect(markup).toContain('title={utcTitle(lastTickTs)}');
    expect(markup).toContain('title={utcTitle(st.lastSeen)}');
    expect(markup).toContain('title={utcTitle(st.finishedAt)}');
    expect(markup).toContain('title={utcTitle(line.ts)}');
    expect(page).toContain(
      "const lastTick = $derived(lastTickTs ? fmtTime(lastTickTs) : 'no tick recorded');"
    );
  });

  it('names no zone beside a reading, the replay clock included', () => {
    // The scrubber's wall clock printed `${wallClockStr} UTC` from when that
    // reading WAS rendered in UTC; once it moved to the viewer's zone the label
    // became a confidently wrong instant for every viewer outside UTC, with no
    // title to resolve it. The frame's own stamp carries the UTC claim now.
    expect(markup).toContain('title={utcTitle(replayFrame?.tick_ts)}');
    expect(markup).not.toContain('UTC');
  });
});

/**
 * D22 (design review C2) — the Linear epic's `/ticks` read 404s in fixture
 * mode, the page swallowed it, and "last tick —" over an empty ticker read as
 * a driver that had never run. A failed read is a sentence now (DESIGN.md:
 * "Say what an empty or failed read means, in a sentence").
 */
describe('the journal tail the ticker seeds itself from (D22)', () => {
  const script = page.slice(0, page.indexOf('</script>'));

  it('keeps the failure instead of swallowing it', () => {
    expect(script).toContain('let tickerError = $state(false)');
    // The old silent catch, which is what made the 404 invisible.
    expect(script).not.toMatch(/\.catch\(\(\) => \{\s*\/\* best-effort/);
    // An expired session still goes to sign-in, like every other read here.
    expect(script).toMatch(/if \(isUnauthorized\(e\)\) \{\s*window\.location\.href = '\/inbox';/);
  });

  it('says it on the strip, where the readings it contradicts are', () => {
    expect(markup).toContain('{#if connected && tickerError}');
    expect(markup).toContain('tick history could not be read');
    // Beside the attention notice, in the same hairline treatment — not a
    // filled panel over the graph. An unread value is text-3, never amber,
    // which is money and held (#2787 N10).
    const i = markup.indexOf('{#if connected && tickerError}');
    expect(markup.slice(i, i + 260)).toContain('border-l border-hair-strong pl-3 font-mono text-13 text-text-3');
    expect(markup.slice(i, i + 260)).not.toContain('var(--warn)');
  });

  it('is reset per epic, so one epic`s failure is not another`s', () => {
    const fn = script.slice(script.indexOf('void ref; '));
    expect(fn.slice(0, fn.indexOf('\n  });'))).toContain('tickerError = false;');
  });

  it('leaves the empty ticker honest about which emptiness it is', () => {
    expect(markup).toContain(
      "{tickerError ? 'tick history could not be read' : 'no events yet.'}"
    );
  });
});

/**
 * D23 — the graph rings every node on the driver's ready frontier, and the
 * ring was in no legend. DESIGN.md: "New states get a new shape, and a legend
 * entry."
 */
describe('the ready frontier has a legend entry (D23)', () => {
  it('counts it off the snapshot`s own frontier, over nodes it carries', () => {
    const script = page.slice(0, page.indexOf('</script>'));
    expect(script).toContain('const nextUp = $derived(');
    expect(script).toContain("(currentSnapshot?.frontier ?? []).filter((id) =>");
  });

  it('shows it only when it is a reading: a zero is noise in a legend', () => {
    expect(markup).toContain('{#if nextUp > 0}');
    expect(markup).toContain('next up');
  });

  it('draws the card`s own ring, from the one component both use (D36)', () => {
    const i = markup.indexOf('{#if nextUp > 0}');
    const entry = markup.slice(i, markup.indexOf('{/if}', i));
    expect(entry).toContain('<ReadyRing size={12} />');
    expect(entry).not.toContain('var(--accent-2)');
  });
});

describe('the strip’s needs-you count (#2279/D6)', () => {
  it('reads the served set live, and the frame’s own diamonds in replay', () => {
    // The header counts `rows` and this legend counts a bucket of the same
    // payload, so the two cannot disagree the way they did — 1 in the header
    // over legends saying 1 and 2. Replay keeps `needsYouReading`: the set is
    // always NOW, and the honest number over a scrubbed frame from the past is
    // what that frame drew.
    expect(page).toContain("? epicNeedsYou(factoryPulse.dashboard, ref)");
    expect(page).toContain(
      ': needsYouReading(currentSnapshot?.nodes ?? [], currentSnapshot?.unavailable ?? [])'
    );
    expect(page).toContain("mode === 'live'");
  });

  it('confesses a count that is not a count, whichever reading failed', () => {
    // A legend printing "?" with nothing saying why is the half-answer FUI-16
    // refused for the node join; the served reading owes the same confession.
    expect(page).toContain('needsYouSetNotice(factoryPulse.dashboard)');
    expect(page).toContain('attentionNotice(currentSnapshot?.unavailable ?? [])');
  });

  it('still spends magenta only on a count that IS somebody waiting', () => {
    // DESIGN.md rations it. `waiting` is the gate, and it needs both halves:
    // a known reading AND something in it.
    expect(page).toContain('attention.known && attention.count > 0');
    expect(markup).toContain("style:color={waiting ? 'var(--accent)' : 'var(--text-3)'}");
  });
});

describe('the strip’s needs-you count (#2279/D6), round 2', () => {
  it('names the part of the count no diamond on the graph can carry', () => {
    // The legend's number is the epic's BUCKET of the one set, and the set
    // holds waits that are about the epic rather than about one of its nodes
    // — a stalled driver has no node to put a diamond on. Sitting in a legend
    // of node statuses, that number could read higher than the diamonds below
    // it with nothing on the page to account for the difference, which is the
    // defect `keepAttentionInGraph` already fixed for the entries that ARE
    // nodes: a count is honest only when every unit of it has something to
    // point at.
    expect(page).toContain(
      "? offGraphNeedsYou(factoryPulse.dashboard?.rows, ref, currentSnapshot?.nodes ?? [])"
    );
    expect(page).toContain('const offGraphText = $derived(offGraphNotice(offGraph));');
    expect(markup).toContain('{#if offGraphText}');
    expect(markup).toContain('{row.reason}');
  });

  it('leaves replay alone, where the count is the frame’s own diamonds', () => {
    // `needsYouReading` counts what the frame drew, so there is nothing left
    // over by construction and a line about one would be a line about nothing.
    const open = page.indexOf('const offGraph = $derived(');
    expect(open).toBeGreaterThan(-1);
    expect(page.slice(open, page.indexOf(');', open))).toContain("mode === 'live'");
  });
});

/**
 * #2303 — the freshness read's own failure is on the strip, not swallowed.
 *
 * A read that threw left `freshness` null, and `freshnessNotice(null)` is null
 * by design (the poll is in flight on first paint). So a persistently failing
 * `/api/prs/freshness` drew no staleness badge on any node AND no confession
 * beside them — which is, pixel for pixel, an epic where nothing is stale.
 * `failedReading` is what puts the failure into the same `unavailable` channel
 * the server-reported reads use, and this holds the wiring to it.
 */
describe('a freshness read that threw', () => {
  /** The poll's `catch` arm, from the script. */
  const arm = (() => {
    const open = page.indexOf('const read = async (): Promise<void> => {');
    if (open === -1) throw new Error('the freshness poll is gone');
    const caught = page.indexOf('} catch (e) {', open);
    return page.slice(caught, page.indexOf('} finally {', caught));
  })();

  it('records the failure as a reading rather than leaving null', () => {
    expect(arm).toContain('freshness = failedReading(freshness, e)');
    expect(page).toContain('failedReading,');
  });

  it('writes it only for the newest read, like the success path', () => {
    expect(arm).toContain('if (generation === freshnessGen)');
  });

  it('still leaves the expired session to the redirect, with nothing else said', () => {
    // An unauthorized read is the session, not the endpoint: the page is being
    // replaced, so a warning on the strip it is leaving behind says nothing.
    expect(arm).toContain("window.location.href = '/inbox'");
    expect(arm.indexOf('return;')).toBeLessThan(arm.indexOf('failedReading'));
  });

  it('draws the notice the reading produces', () => {
    expect(page).toContain('const freshNotice = $derived(freshnessNotice(freshness));');
    expect(markup).toContain('{freshNotice}');
  });
});

/**
 * #2304 — what redraws the full tick strip.
 *
 * The strip is read once per epic and then on a live frame, and the first
 * version named `live.heartbeat?.tick_count` as that signal. It is the wrong
 * reading: `tick_count` is written by the driver's `begin_epic_tick`, which
 * `advance` reaches only past the single-driver lock, the tool preflight and
 * the non-empty-graph guard — and a tick turned away by one of those moves
 * none of the stream's four frame hashes either, so the live channel emitted
 * nothing at all and the strip sat frozen on its last reading while the driver
 * failed every tick. Those attempts are the entire reason the strip exists
 * (reviewer finding, round 3). `tick_attempt_stamp` moves whenever the driver
 * opens or closes an attempt row, and `_heartbeat_tuple` server-side carries
 * it so the frame is emitted.
 */
describe('the signal the full tick strip is redrawn on', () => {
  const script = page.slice(0, page.indexOf('</script>'));

  /** The strip effect's body, from its ref capture to the teardown. */
  const effect = (() => {
    const open = script.indexOf('fetchEpicTickStrip(epicRef, FULL_STRIP_LIMIT)');
    if (open === -1) throw new Error('the tick strip read is gone');
    const start = script.lastIndexOf('$effect(() => {', open);
    return script.slice(start, script.indexOf('\n  });', open));
  })();

  /** The key the effect waits on (#2789, K7): a derived, so the strip is
   * re-read when the key moves and not on every frame that replaces `live`. */
  const key = (() => {
    const open = script.indexOf('const tickStripKey = $derived(');
    if (open === -1) throw new Error('the tick strip key is gone');
    return script.slice(open, script.indexOf(');', open));
  })();

  it('depends on the attempt stamp, not only on the tick count', () => {
    expect(key).toContain('live.heartbeat?.tick_attempt_stamp');
    expect(effect).toContain('void tickStripKey;');
    // Read through the derived only: a read of `live` inside the effect is a
    // dependency on every frame, which is the refetch-per-heartbeat (K7).
    expect(effect).not.toContain('live.heartbeat');
  });

  it('keeps the tick count behind it, for a server that predates the stamp', () => {
    expect(key).toContain('live.heartbeat?.tick_count');
  });

  it('is still a stamp and not a timer — no interval of its own', () => {
    // A poll per second would redraw a frozen strip, at the cost the roster's
    // whole read budget was designed around (C3/#2296).
    expect(effect).not.toContain('setInterval');
    expect(effect).not.toContain('setTimeout');
  });

  it('is dropped per epic, so one epic`s attempts are not another`s', () => {
    // Reviewer finding, round 4: the reset re-initialized eighteen per-epic
    // readings and not these two, so a client-side ref -> ref navigation drew
    // the PREVIOUS epic's attempts under the new epic's aria-label until the
    // new read landed — and carried the previous epic's read failure with it.
    const reset = script.slice(script.indexOf('void ref; '));
    const body = reset.slice(0, reset.indexOf('\n  });'));
    expect(body).toContain('tickStrip = [];');
    expect(body).toContain('tickStripError = false;');
    // Which is only reachable because the state is declared ABOVE the reset.
    expect(script.indexOf('let tickStrip = $state<EpicTickStrip[]>([]);')).toBeLessThan(
      script.indexOf('void ref; ')
    );
  });
});

/**
 * #2473 — the full strip's width at phone width. The strip drew at its design
 * 760px inside a box this page clips at the viewport (the column's
 * `overflow-x-hidden`), so at 390px roughly half of it was cut off — and the
 * strip places its NEWEST attempt last, so the half the clip took was the
 * recent ticks. The rules are `fitStripWidth` and `stripLimit` — the width the
 * strip draws at and how many attempts that width can draw apart, both
 * unit-tested in `factory/tick-strip.test.ts`. What has to be pinned HERE is
 * that the page measures its box and hands BOTH readings over off that one
 * measurement, because a strip drawn at the default is the bug whatever the
 * rules say, and so is a fitted strip still loaded with the design width's
 * worth of attempts.
 */
describe('the full tick strip inside a box narrower than it', () => {
  const script = page.slice(0, page.indexOf('</script>'));

  it('measures the box the strip is in, rather than folding at a breakpoint', () => {
    // A breakpoint cannot know the number: the box is the window less this
    // section's gutters and whatever the scroller takes.
    expect(markup).toContain('bind:clientWidth={stripBox}');
    expect(script).toContain('fitStripWidth(stripBox)');
  });

  it('fits the width through the tested rule, not with arithmetic of its own', () => {
    expect(script).toContain("from '$lib/factory/tick-strip'");
    expect(script).toContain('fitStripWidth');
    // No hand-rolled second opinion about the design width on this page.
    expect(script).not.toContain('FULL_STRIP_WIDTH');
  });

  it('hands the fitted width to the strip, which otherwise draws at 760px', () => {
    const open = markup.indexOf('<TickTimeline');
    expect(open).toBeGreaterThan(-1);
    const tag = markup.slice(open, markup.indexOf('/>', open));
    expect(tag).toContain('width={stripWidth}');
  });

  it('hands it no more attempts than that same width can draw apart', () => {
    // Reviewer finding, round 1: the WIDTH was fitted and the attempt count was
    // not, so a 310px lane was handed the 189 attempts the 760px lane is sized
    // for — and the batcher's 4px floor merged every one of them into a single
    // bar, which is the degeneration the limit exists to prevent. The rule is
    // `stripLimit` (unit-tested next to `fitStripWidth`); what is pinned HERE is
    // that the drawn set comes off the same measured width the strip draws at.
    expect(script).toContain('stripLimit(stripWidth)');
    const open = markup.indexOf('<TickTimeline');
    const tag = markup.slice(open, markup.indexOf('/>', open));
    expect(tag).toContain('ticks={stripShown}');
    // Not the whole read: that is the bug.
    expect(tag).not.toContain('ticks={tickStrip}');
  });

  it('still READS the design width\'s worth, so widening needs no fetch', () => {
    // The trim is a drawing decision, not a fetch one — a rotation back to
    // landscape re-adds the older attempts from rows already in hand, and the
    // read stays keyed on the heartbeat's attempt stamp rather than on a width
    // that changes with every pixel of a drag.
    expect(script).toContain('fetchEpicTickStrip(epicRef, FULL_STRIP_LIMIT)');
  });
});

/**
 * #2303 — a freshness reading belongs to the epic it was asked for.
 *
 * The poll reaches `ref` only from inside an async callback, which a hidden tab
 * never runs — so the effect could carry no dependency on the ref at all, and a
 * ref -> ref navigation would leave the poll asking for the epic the reader has
 * left. The reset effect nulled `freshness` and bumped `replayGen` but not
 * `freshnessGen`, so a read already in flight for epic A still matched the
 * generation guard and landed its rows — and its strip notice — under epic B
 * (reviewer finding, #2303). Both halves are pinned here, against the source,
 * like every other claim in this file.
 */
describe('a freshness reading under the wrong epic', () => {
  const script = page.slice(0, page.indexOf('</script>'));

  /** The poll effect's body, from its ref capture to the interval teardown. */
  const poll = (() => {
    const open = script.indexOf("if (!connected || view !== 'graph' || mode !== 'live') return;");
    if (open === -1) throw new Error('the freshness poll is gone');
    return script.slice(open, script.indexOf('\n  });', open));
  })();

  it('captures the ref synchronously, so the effect re-arms for a new one', () => {
    // `fetchPrFreshness(..., ref)` sits behind two async hops; a tab that was
    // hidden on the effect's run reaches none of them, and the dependency a
    // Svelte effect does not read it does not have.
    expect(poll).toContain('const epicRef = ref;');
    expect(poll).toContain('fetchPrFreshness(FRESHNESS_PAGE, cursor, epicRef)');
    expect(poll).not.toContain('fetchPrFreshness(FRESHNESS_PAGE, cursor, ref)');
  });

  it('orphans an in-flight read from the reset itself, beside the tick fetches', () => {
    const reset = script.slice(script.indexOf('void ref; '));
    const body = reset.slice(0, reset.indexOf('\n  });'));
    expect(body).toContain('replayGen += 1;');
    expect(body).toContain('freshnessGen += 1;');
    expect(body).toContain('freshness = null;');
  });

  it('writes a reading only for the newest epoch, on both paths', () => {
    expect(poll).toContain('const generation = ++freshnessGen;');
    expect(poll).toContain('if (generation === freshnessGen) freshness = next;');
    expect(poll).toContain(
      'if (generation === freshnessGen) freshness = failedReading(freshness, e);'
    );
  });
});
