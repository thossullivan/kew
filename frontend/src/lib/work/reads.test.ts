/**
 * FUI-31 — the Work page's description reads, and the one sequence that gets
 * them wrong: A -> B -> A.
 *
 * Re-selecting an item the page is already reading puts two reads of it in
 * flight at once, and everything that decides what the panel shows was keyed on
 * the item ID — which cannot tell those two apart. The earlier read's
 * completion handed the in-flight flag back while the later one was still out,
 * the shell's tick then took a read the endpoint refuses for the very item it
 * is reading, and that refusal landed on top of the body the owning read had
 * just brought back: a reason with no body and no link, under an item whose
 * description had in fact been read.
 *
 * So the ledger is driven here the way the page drives it — the page itself
 * cannot be mounted (there is no component-render infra in this suite; see
 * `list-geometry.test.ts`), so the last test reads `+page.svelte` and holds it
 * to this same wiring.
 */
import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import type { ItemDescription } from '$lib/api';
import { DESCRIPTION_RETRIES } from './evidence';
import { DescriptionReads, NewestReads, SingleRead } from './reads';

/** A read that came back with a body: an answer, never taken again. */
const BODY: ItemDescription = {
  item_id: '2055',
  availability: 'available',
  provenance: 'github',
  observed_at: '2026-09-24T18:11:58+00:00',
  title: 'The list is not the dashboard',
  url: 'https://github.com/thossullivan/kew/issues/2055',
  description: 'The list and the dashboard disagree about the ready queue.',
  expected: '',
  actual: '',
  reason: '',
  composed_at: '2026-09-24T18:12:04+00:00'
};

/**
 * The refusal this file is about, worded as `kew/service/description.py` words
 * it: a read of an item whose read is already out, turned away before the
 * request leaves the box. It is the one refusal the page can PROVOKE, which is
 * why a duplicate read is the failure and not merely waste.
 */
const REFUSED: ItemDescription = {
  item_id: '2055',
  availability: 'unavailable',
  provenance: 'error',
  observed_at: null,
  title: '',
  url: null,
  description: '',
  expected: '',
  actual: '',
  reason: 'a read for this item is already in flight; this one was not attempted',
  composed_at: BODY.composed_at
};

/**
 * The page's own `readDescription` and tick retry, over answers the test hands
 * over one at a time — `answers[n]` settles the nth read taken.
 */
function screen() {
  const reads = new DescriptionReads();
  const answers: Array<(next: ItemDescription) => void> = [];
  const state = { description: null as ItemDescription | null };
  function read(): Promise<void> {
    const ticket = reads.begin();
    return new Promise<ItemDescription>((resolve) => answers.push(resolve))
      .then((next) => {
        if (!reads.owns(ticket)) return;
        state.description = next;
      })
      .finally(() => reads.end(ticket));
  }
  /** One tick of the shell's pulse, as far as the description is concerned. */
  function tick(): Promise<void> | null {
    return reads.claimReread(state.description) ? read() : null;
  }
  return { reads, answers, state, read, tick };
}

describe('the description read ledger', () => {
  it('hands each read the selection it was taken for', () => {
    const reads = new DescriptionReads();
    const first = reads.begin();
    expect(reads.owns(first)).toBe(true);
    reads.select();
    // The selection moved: the read still out answers for the item just left.
    expect(reads.owns(first)).toBe(false);
    const second = reads.begin();
    expect(reads.owns(second)).toBe(true);
  });

  it('keeps the in-flight flag with the newest read of a re-selected item', () => {
    // The regression (#2155 review, round 4). Two reads of A are out — the one
    // from the first selection of A and the one from the second — and the
    // earlier one comes back first. Keyed on the item id, its completion freed
    // the flag its successor holds.
    const reads = new DescriptionReads();
    reads.select();
    const firstA = reads.begin();
    reads.select();
    reads.begin();
    reads.select();
    const secondA = reads.begin();
    expect(reads.owns(firstA)).toBe(false);
    expect(reads.owns(secondA)).toBe(true);

    reads.end(firstA);
    // Still out, so the tick takes no read: the endpoint would refuse it for
    // the item it is reading, and the retry would be spent on that refusal.
    expect(reads.claimReread(REFUSED)).toBe(false);
    reads.end(secondA);
    expect(reads.claimReread(REFUSED)).toBe(true);
  });

  it('never answers a re-selected item with a refusal of its own making', async () => {
    const s = screen();
    s.reads.select(); // A
    const firstA = s.read();
    s.reads.select(); // B
    const b = s.read();
    s.reads.select(); // A again
    const secondA = s.read();

    // The first A read lands. It is nobody's answer now — the selection has
    // moved past it twice — and it frees no flag either.
    s.answers[0](REFUSED);
    await firstA;
    expect(s.state.description).toBeNull();
    expect(s.tick()).toBeNull();

    // The read that does own the screen brings back the body.
    s.answers[2](BODY);
    await secondA;
    expect(s.state.description).toBe(BODY);

    // B's read, superseded in the same way, writes nothing over it; and a body
    // on screen is not re-read at all.
    s.answers[1](REFUSED);
    await b;
    expect(s.state.description).toBe(BODY);
    expect(s.tick()).toBeNull();
  });

  it('takes the read again once the refusal it owns has landed', async () => {
    const s = screen();
    s.reads.select();
    const first = s.read();
    // Nothing while it is out, however many ticks pass.
    expect(s.tick()).toBeNull();
    expect(s.tick()).toBeNull();
    s.answers[0](REFUSED);
    await first;
    expect(s.state.description).toBe(REFUSED);

    const retry = s.tick();
    expect(retry).not.toBeNull();
    s.answers[1](BODY);
    await retry;
    expect(s.state.description).toBe(BODY);
  });

  it('spends the retry bound on the ticks that fire, then stops', async () => {
    const s = screen();
    s.reads.select();
    for (let taken = 0; taken < DESCRIPTION_RETRIES; taken += 1) {
      const retry = s.tick();
      expect(retry, `retry ${taken}`).not.toBeNull();
      s.answers[taken](REFUSED);
      await retry;
    }
    expect(s.tick()).toBeNull();
    // A new selection reads with its own budget: the last item's spend is not
    // charged to this one.
    s.reads.select();
    expect(s.tick()).not.toBeNull();
  });

  it('is how the page takes the read', () => {
    // The page cannot be rendered here, so the wiring is asserted against its
    // source: a hand-rolled flag beside the ledger is the way this regression
    // comes back.
    const page = readFileSync(
      new URL('../../routes/(factory)/work/+page.svelte', import.meta.url),
      'utf8'
    );
    expect(page).toContain('const ticket = descriptionReads.begin()');
    expect(page).toContain('if (!descriptionReads.owns(ticket)) return;');
    expect(page).toContain('descriptionReads.end(ticket);');
    expect(page).toContain('descriptionReads.claimReread(description)');
    expect(page).toContain('descriptionReads.select();');
    // Never keyed on the item id again — that is the flag two reads of one item
    // would share.
    expect(page).not.toContain('descriptionReading');
    expect(page).not.toContain('descriptionTries');
  });
});

/**
 * #2933 — the pulse's re-reads of the list, the pipeline and the evidence can
 * answer out of order. An answer older than the one on screen is not drawn.
 */
describe('NewestReads', () => {
  it('drops a slow answer that lands after a newer one was drawn', () => {
    const reads = new NewestReads();
    const slow = reads.begin();
    const fast = reads.begin();
    expect(reads.take(fast)).toBe(true);
    expect(reads.take(slow)).toBe(false);
  });

  it('draws answers that land in the order they were sent', () => {
    const reads = new NewestReads();
    const first = reads.begin();
    const second = reads.begin();
    expect(reads.take(first)).toBe(true);
    expect(reads.take(second)).toBe(true);
  });

  it('draws a lone answer, and an answer to a read sent after it was drawn', () => {
    const reads = new NewestReads();
    expect(reads.take(reads.begin())).toBe(true);
    const old = reads.begin();
    const next = reads.begin();
    expect(reads.take(next)).toBe(true);
    expect(reads.take(old)).toBe(false);
    expect(reads.take(reads.begin())).toBe(true);
  });

  it('drops an earlier selection’s answer after A -> B -> A, even with nothing drawn since', () => {
    const reads = new NewestReads();
    const firstA = reads.begin();
    reads.select();
    reads.begin(); // B's read, left before it answered: never drawn
    reads.select();
    const secondA = reads.begin();
    expect(reads.take(firstA)).toBe(false);
    expect(reads.take(secondA)).toBe(true);
  });

  it('draws in order within one selection after the selection moved', () => {
    const reads = new NewestReads();
    reads.begin();
    reads.select();
    const first = reads.begin();
    const second = reads.begin();
    expect(reads.take(first)).toBe(true);
    expect(reads.take(second)).toBe(true);
  });
});

describe('SingleRead (#3055)', () => {
  it('sends no second read while one is out, however many pulses ask', () => {
    const reads = new SingleRead();
    const first = reads.begin();
    expect(first).not.toBeNull();
    for (let pulse = 0; pulse < 3; pulse += 1) expect(reads.begin()).toBeNull();
    reads.end(first!);
    expect(reads.begin()).not.toBeNull();
  });

  it('aborts the read out, and lets the next be sent', () => {
    const reads = new SingleRead();
    const out = reads.begin()!;
    reads.abort();
    expect(out.aborted).toBe(true);
    const next = reads.begin();
    expect(next).not.toBeNull();
    // The aborted read settling late does not free the slot its successor holds.
    reads.end(out);
    expect(reads.begin()).toBeNull();
    expect(next!.aborted).toBe(false);
  });
});
