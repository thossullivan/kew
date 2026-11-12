/**
 * THE evidence pack, as the page uses it.
 *
 * The figures, and the one string each renders as, live in pack-figures.js,
 * plain JavaScript so the build's claims check (scripts/page-claims.mjs)
 * reads the same values the page shows. They are re-exported here, beside
 * the anchor records and the honest limits.
 *
 * Anchors are from the repo-root `audit-anchors.jsonl`.
 */

export {
  PACK,
  RUN_KINDS,
  SCORECARD,
  SHOWN,
  SPEND_LABEL,
  kindLabel,
  type ScoreRow
} from './pack-figures';

/**
 * Real anchor records from audit-anchors.jsonl.
 *
 * anchor_hash = sha256 over a canonical JSON array of exactly these five
 * fields in this order, `ensure_ascii=True, separators=(",",":")`
 * (kew/src/kew/audit_chain.py:102). Every value is ASCII, so
 * `JSON.stringify([...])` reproduces Python's bytes exactly. Cross-checked:
 * Node and Python agree on all three hashes below.
 */
export type Anchor = {
  id: number;
  entry_hash: string;
  count: number;
  timestamp: string;
  prev: string | null;
  /**
   * This anchor's own hash as the record holds it: the `prev` of the anchor
   * after it in audit-anchors.jsonl. Not part of the canonical record. It is
   * what lets the page check the LAST anchor shown, which no shown link binds.
   */
  recorded: string;
};

export const ANCHORS: Anchor[] = [
  {
    id: 147,
    entry_hash: 'd097580f0778c46aef07b1086016ad0d1efe3a362d858759cb09e28b9eefc161',
    count: 147,
    timestamp: '2026-07-06T05:44:37.922021+00:00',
    prev: null,
    recorded: '64c9f3d58e1c2f0c5a73901a581820addffa76b67a502fa2ebbcf607376b23f1'
  },
  {
    id: 1471,
    entry_hash: '507c7dce777d04e88099a953d34b6797409a3c6ee6a578004f45f0989a82713e',
    count: 1471,
    timestamp: '2026-07-24T05:26:08.261545+00:00',
    prev: '64c9f3d58e1c2f0c5a73901a581820addffa76b67a502fa2ebbcf607376b23f1',
    recorded: '0b30eb5143d2e39fb5c25300599fad81f23dc3dd8b442cf01015252d275b59c8'
  },
  {
    id: 1731,
    entry_hash: 'b4f05a1fc884a78805035eb2fb94ec5662e8399988abd0ed2244380a20be249d',
    count: 1731,
    timestamp: '2026-07-25T22:57:25.130705+00:00',
    prev: '0b30eb5143d2e39fb5c25300599fad81f23dc3dd8b442cf01015252d275b59c8',
    // The prev of anchor #2019, the next record in audit-anchors.jsonl.
    recorded: '30e33c1e117a6cdc9468ac4041370296fd7917542170510f7d50f0bcceed2153'
  }
];

/** Exactly the canonicalisation in audit_chain.py, in the browser. */
export function canonical(a: Anchor): string {
  return JSON.stringify([a.id, a.entry_hash, a.count, a.timestamp, a.prev]);
}

/** SHA-256 as hex, with Web Crypto: the same call in Node at build time and
 *  in the browser. */
export async function sha256Hex(input: string): Promise<string> {
  const buf = await globalThis.crypto.subtle.digest('SHA-256', new TextEncoder().encode(input));
  return Array.from(new Uint8Array(buf))
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('');
}

export async function anchorHash(a: Anchor): Promise<string> {
  return sha256Hex(canonical(a));
}

/** One anchor as the evidence section draws it: the record, its computed
 *  hash, whether its prev binds the previous record (null when it has no
 *  prev), and whether the reader altered it. */
export type VerifiedAnchor = Anchor & {
  computed: string;
  /** The computed hash equals the hash the record holds for this anchor. */
  hashOk: boolean;
  linkOk: boolean | null;
  edited: boolean;
};

/**
 * Recompute every anchor's hash, check it against the hash the record holds
 * for that anchor (so altering the last anchor shown breaks too), and check
 * each prev against the previous record's hash. `tampered` is the id of an anchor whose count the reader
 * bumped by one, or null. Runs at build time (src/routes/+page.server.ts), so
 * the prerendered page already holds the computed rows, and again in the
 * browser on mount and on every alteration.
 */
export async function verifyAnchors(
  anchors: Anchor[],
  tampered: number | null
): Promise<VerifiedAnchor[]> {
  const out: VerifiedAnchor[] = [];
  let expectedPrev: string | null = null;
  for (const a of anchors) {
    const src: Anchor = tampered === a.id ? { ...a, count: a.count + 1 } : { ...a };
    const computed = await anchorHash(src);
    const linkOk = src.prev === null ? null : src.prev === expectedPrev;
    const hashOk = computed === src.recorded;
    out.push({ ...src, computed, hashOk, linkOk, edited: tampered === a.id });
    expectedPrev = computed;
  }
  return out;
}

/** What a reader with only this page can and cannot check for themselves. */
export const LIMITS = {
  can: [
    'Recompute every anchor hash from the record shown, with the same algorithm kew uses.',
    'Check that each anchor’s prev binds the whole previous anchor record.',
    'Watch a single altered value break the link that binds it.',
    'Cross-foot the pack’s arithmetic: total spend against cost per merged PR, and the run kinds’ spend against the total.'
  ],
  cannot: [
    'Recompute the chain head from this page. The pack ships the head hash and the event count, not the events, so there is no preimage here.',
    'Independently corroborate the costs or merge states. They come from kew’s own run store.'
  ]
};
