# src/kew/audit_chain.py
"""Pure hash-chain logic for the audit log (#249).

No I/O: callers pass raw row values in, get hashes/verdicts out. This is the
single source of truth for how an audit row hashes, so the writer (run_store)
and the verifier (audit CLI) cannot drift.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass

GENESIS_PREV = "0" * 64

#: The entry-hash preimage of rows written before the chain-format step (v77):
#: twelve slots, ``item_id`` and ``dedup_key`` left out. Frozen — the
#: published anchors cover rows hashed this way.
CHAIN_FORMAT_LEGACY = 1
#: The preimage every writer uses from v77 on (#2029): the literal ``2``, the
#: twelve legacy slots, then ``item_id`` and ``dedup_key``.
CHAIN_FORMAT_ITEM_KEYS = 2
CHAIN_FORMATS = (CHAIN_FORMAT_LEGACY, CHAIN_FORMAT_ITEM_KEYS)


@dataclass(frozen=True)
class ChainRow:
    """One audit row's RAW stored column values (payload is verbatim TEXT)."""

    id: int
    timestamp: str
    event: str
    run_id: str | None
    issue_num: int | None
    repo: str | None
    actor_type: str | None
    actor_id: str | None
    endpoint: str | None
    policy_hash: str | None
    payload: str | None
    prev_hash: str | None
    item_id: str | None = None
    dedup_key: str | None = None
    #: Which preimage the row hashes under. A row written before the
    #: chain-format column existed reads as the legacy format.
    chain_format: int = CHAIN_FORMAT_LEGACY


def entry_hash(row: ChainRow) -> str:
    """SHA-256 hex over a canonical fixed-order JSON array of raw values.

    A legacy-format row hashes the twelve original slots, exactly as before
    the format step. Any other format prepends the format number and appends
    ``item_id`` and ``dedup_key``, so the format itself is in the preimage:
    flipping a row's format changes its hash like any other edit.
    """
    slots: list[object] = [
        row.id,
        row.timestamp,
        row.event,
        row.run_id,
        row.issue_num,
        row.repo,
        row.actor_type,
        row.actor_id,
        row.endpoint,
        row.policy_hash,
        row.payload,
        row.prev_hash,
    ]
    if row.chain_format != CHAIN_FORMAT_LEGACY:
        slots = [row.chain_format, *slots, row.item_id, row.dedup_key]
    canonical = json.dumps(
        slots,
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ChainResult:
    ok: bool
    broken_at_id: int | None
    head_hash: str | None
    count: int


def verify_chain(rows: list[ChainRow]) -> ChainResult:
    """Walk ascending; each row.prev_hash must equal the prior row's hash.

    Does not assume contiguous ids. head_hash is the last row's hash even
    when the chain is broken (so anchoring can still record a head).

    The chain format only moves forward: a row in an unknown format, or a
    legacy-format row after an item-keys row, is a break at that row — a
    downgrade would drop ``item_id`` and ``dedup_key`` out of the hash.
    """
    expected_prev = GENESIS_PREV
    floor = CHAIN_FORMAT_LEGACY
    broken_at_id: int | None = None
    head_hash: str | None = None
    for row in rows:
        linked = row.prev_hash == expected_prev
        known = row.chain_format in CHAIN_FORMATS and row.chain_format >= floor
        if broken_at_id is None and not (linked and known):
            broken_at_id = row.id
        if row.chain_format in CHAIN_FORMATS:
            floor = max(floor, row.chain_format)
        head_hash = entry_hash(row)
        expected_prev = head_hash
    return ChainResult(
        ok=broken_at_id is None,
        broken_at_id=broken_at_id,
        head_hash=head_hash,
        count=len(rows),
    )


@dataclass(frozen=True)
class Anchor:
    id: int
    entry_hash: str
    count: int
    timestamp: str
    #: Hash binding the PREVIOUS anchor record (the anchor chain, T1/#912).
    #: ``GENESIS_PREV`` for the first anchor; ``None`` for legacy anchors
    #: written before the anchor chain existed (their link is unverifiable but
    #: they still form the preimage for the next anchor's ``prev``).
    prev: str | None = None
    #: Explicitly absent for legacy anchors. Identity-aware anchors are v2.
    version: int | None = None
    ledger_id: str | None = None


def anchor_hash(anchor: Anchor) -> str:
    """SHA-256 hex over a canonical fixed-order JSON array of the anchor's fields.

    Binds ``id``, ``entry_hash``, ``count``, ``timestamp`` and ``prev`` so that
    each new anchor's ``prev`` commits to the ENTIRE previous anchor record.
    Wholesale re-chaining of the audit log therefore requires rewriting the
    anchor history too — which lives out-of-band in git (the anchor workflow),
    making a silent re-chain detectable by construction.
    """
    fields = (
        [
            2,
            anchor.ledger_id,
            anchor.id,
            anchor.entry_hash,
            anchor.count,
            anchor.timestamp,
            anchor.prev,
        ]
        if anchor.version == 2
        else [
            anchor.id,
            anchor.entry_hash,
            anchor.count,
            anchor.timestamp,
            anchor.prev,
        ]
    )
    canonical = json.dumps(
        fields,
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def format_anchor(anchor: Anchor) -> str:
    if getattr(anchor, "version", None) == 2:
        fields = {
            "version": 2,
            "ledger_id": getattr(anchor, "ledger_id", None),
            "id": anchor.id,
            "entry_hash": anchor.entry_hash,
            "count": anchor.count,
            "timestamp": anchor.timestamp,
            "prev": anchor.prev,
        }
    else:
        fields = {
            "id": anchor.id,
            "entry_hash": anchor.entry_hash,
            "count": anchor.count,
            "timestamp": anchor.timestamp,
            # getattr keeps duck-typed callers (SimpleNamespace in tests) working.
            "prev": getattr(anchor, "prev", None),
        }
    return json.dumps(
        fields,
        ensure_ascii=True,
        separators=(",", ":"),
    )


def _validated_anchor_identity(
    version: object,
    ledger_id: object,
    *,
    identity: str | None,
    identity_aware: bool,
) -> tuple[str | None, bool]:
    if version is None:
        if identity_aware:
            raise ValueError("legacy anchor after v2 transition")
        if ledger_id is not None:
            raise ValueError("legacy anchor cannot carry ledger_id")
        return identity, identity_aware
    if version != 2:
        raise ValueError(f"unsupported anchor version: {version!r}")
    if not isinstance(ledger_id, str):
        raise ValueError("v2 anchor requires ledger_id")
    try:
        parsed_id = uuid.UUID(ledger_id)
    except ValueError as exc:
        raise ValueError("v2 anchor ledger_id is not a UUID") from exc
    if str(parsed_id) != ledger_id:
        raise ValueError("v2 anchor ledger_id must be canonical")
    if identity is not None and ledger_id != identity:
        raise ValueError("inconsistent ledger_id in anchor history")
    return ledger_id, True


def _parse_anchor_line(
    line: str, *, identity: str | None, identity_aware: bool
) -> tuple[Anchor, str | None, bool]:
    obj = json.loads(line)
    if not isinstance(obj, dict):
        raise TypeError("anchor line must be a JSON object")
    version = obj.get("version")
    ledger_id = obj.get("ledger_id")
    identity, identity_aware = _validated_anchor_identity(
        version, ledger_id, identity=identity, identity_aware=identity_aware
    )
    return (
        Anchor(
            id=obj["id"],
            entry_hash=obj["entry_hash"],
            count=obj["count"],
            timestamp=obj["timestamp"],
            # Absent in legacy anchor files (pre-#912) — treated as unlinked.
            prev=obj.get("prev"),
            version=version,
            ledger_id=ledger_id,
        ),
        identity,
        identity_aware,
    )


def parse_anchors(text: str) -> list[Anchor]:
    anchors: list[Anchor] = []
    identity: str | None = None
    identity_aware = False
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        anchor, identity, identity_aware = _parse_anchor_line(
            line, identity=identity, identity_aware=identity_aware
        )
        anchors.append(anchor)
    return anchors


@dataclass(frozen=True)
class AnchorChainResult:
    """Verdict for the anchor-chain continuity walk (each anchor binds the prior)."""

    ok: bool
    broken_at_index: int | None
    checked: int
    head_hash: str | None
    #: How many anchors were walked, so a caller can tell a vacuously-clean
    #: verdict (``checked == 0`` over many anchors) from a real one (#1122).
    total: int = 0


def verify_anchor_chain(anchors: list[Anchor]) -> AnchorChainResult:
    """Walk anchors in file order; each ``prev`` must bind the previous anchor.

    The first anchor's ``prev`` must be ``GENESIS_PREV``. Legacy anchors whose
    ``prev`` is ``None`` predate the anchor chain (#912): their link cannot be
    checked, but they still contribute their ``anchor_hash`` as the preimage the
    next anchor's ``prev`` must match. That tolerance is confined to the LEADING
    prefix — once any anchor carries a ``prev``, a later anchor without one is a
    break, not a skip, so an adversary cannot opt out of the defence by deleting
    the field (#1122). A PRESENT ``prev`` that does not equal the previous
    anchor's ``anchor_hash`` is likewise a break — evidence the anchor history
    was rewritten (re-chaining). ``head_hash`` is the last anchor's
    ``anchor_hash`` so a caller can continue the chain.
    """
    broken_at_index: int | None = None
    checked = 0
    linked = False
    expected_prev = GENESIS_PREV
    head_hash: str | None = None
    for i, a in enumerate(anchors):
        if a.prev is None:
            # Tolerated only while no anchor has claimed a link yet.
            if linked and broken_at_index is None:
                broken_at_index = i
        else:
            linked = True
            if a.prev == expected_prev:
                checked += 1
            elif broken_at_index is None:
                broken_at_index = i
        expected_prev = anchor_hash(a)
        head_hash = expected_prev
    return AnchorChainResult(
        ok=broken_at_index is None,
        broken_at_index=broken_at_index,
        checked=checked,
        head_hash=head_hash,
        total=len(anchors),
    )


@dataclass(frozen=True)
class AnchorReport:
    ok: bool
    matched: int
    mismatched: list[int]
    absent: list[int]
    lagging: list[int]
    #: True when the anchored event count went backwards — either the anchor
    #: file itself is non-monotonic, or the LIVE chain has fewer events than the
    #: highest anchored count by more than ``staleness_window`` (truncation /
    #: re-chain-and-shrink, or a stale/lagging anchor beyond tolerance). #912.
    count_shrink: bool = False
    #: Identity-aware anchors name a different ledger. This verdict takes
    #: precedence over count/hash arithmetic against an unrelated chain.
    wrong_ledger: bool = False
    identity_unanchored: bool = False


@dataclass(frozen=True)
class _AnchorPartition:
    """Intermediate per-anchor classification, before count-shrink is folded in."""

    matched: int
    mismatched: list[int]
    absent: list[int]
    lagging: list[int]
    anchor_nonmonotonic: bool


def _partition_anchors(
    anchors: list[Anchor], by_id: dict[int, ChainRow], head_id: int
) -> _AnchorPartition:
    """Sort each anchor into lag / matched / mismatched / absent and track
    count monotonicity. Pure per-anchor classification; no count-shrink here."""
    matched = 0
    mismatched: list[int] = []
    absent: list[int] = []
    lagging: list[int] = []
    anchor_nonmonotonic = False
    max_count = 0
    for a in anchors:
        if a.id > head_id:
            lagging.append(a.id)
            continue
        if a.count < max_count:
            anchor_nonmonotonic = True
        else:
            max_count = a.count
        if a.id not in by_id:
            absent.append(a.id)
        elif entry_hash(by_id[a.id]) == a.entry_hash:
            matched += 1
        else:
            mismatched.append(a.id)
    return _AnchorPartition(
        matched=matched,
        mismatched=mismatched,
        absent=absent,
        lagging=lagging,
        anchor_nonmonotonic=anchor_nonmonotonic,
    )


def _anchor_identity_report(
    anchors: list[Anchor], ledger_id: str | None
) -> AnchorReport | None:
    anchor_ledger_ids = {a.ledger_id for a in anchors if a.version == 2}
    if ledger_id is not None and not anchor_ledger_ids:
        return AnchorReport(
            ok=False,
            matched=0,
            mismatched=[],
            absent=[],
            lagging=[],
            identity_unanchored=True,
        )
    if ledger_id is not None and anchor_ledger_ids != {ledger_id}:
        return AnchorReport(
            ok=False,
            matched=0,
            mismatched=[],
            absent=[],
            lagging=[],
            wrong_ledger=True,
        )
    return None


def classify_anchors(
    rows: list[ChainRow],
    anchors: list[Anchor],
    *,
    staleness_window: int = 0,
    ledger_id: str | None = None,
) -> AnchorReport:
    """Compare anchors to the local chain, distinguishing tamper from lag.

    Assumes ``rows`` is sorted ascending by id (same contract as verify_chain);
    uses ``rows[-1].id`` as the local head.

    id > local head        -> lag (a clone/anchor ahead of the local head)
    id <= head, present     -> recompute hash; match or mismatch (tamper)
    id <= head, absent      -> tamper (a gap where a row should exist)

    Count-shrink (#912, fail-closed): counts must be monotonically
    non-decreasing across non-lagging anchors, AND the live chain must not trail
    the highest anchored count by more than ``staleness_window``. In honest
    operation the live DB is always at or ahead of every anchor (anchors trail
    the head), so a live count below the anchored high-water mark means rows
    were truncated after anchoring — or the anchor is stale/lagging beyond the
    tolerated window. ``staleness_window=0`` demands the live chain be at least
    as long as the latest anchor.
    """
    identity_report = _anchor_identity_report(anchors, ledger_id)
    if identity_report is not None:
        return identity_report

    by_id = {r.id: r for r in rows}
    head_id = rows[-1].id if rows else 0
    live_count = len(rows)
    part = _partition_anchors(anchors, by_id, head_id)
    # Count-shrink relative to the LATEST (highest-count) anchor — the field a
    # DB-level adversary would demote to advisory. Includes lagging anchors, so
    # a truncation that pushes the anchored head beyond the (shrunken) local
    # head still fails once the gap exceeds the staleness window.
    anchored_high = max((a.count for a in anchors), default=0)
    live_shrink = anchored_high - live_count > staleness_window
    count_shrink = part.anchor_nonmonotonic or live_shrink
    return AnchorReport(
        ok=not part.mismatched and not part.absent and not count_shrink,
        matched=part.matched,
        mismatched=part.mismatched,
        absent=part.absent,
        lagging=part.lagging,
        count_shrink=count_shrink,
    )
