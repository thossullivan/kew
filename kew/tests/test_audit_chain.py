# tests/test_audit_chain.py
import dataclasses
import hashlib
import json
from types import SimpleNamespace

import pytest

from kew.audit_chain import (
    CHAIN_FORMAT_ITEM_KEYS,
    CHAIN_FORMAT_LEGACY,
    GENESIS_PREV,
    Anchor,
    AnchorChainResult,
    AnchorReport,
    ChainResult,
    ChainRow,
    anchor_hash,
    classify_anchors,
    entry_hash,
    format_anchor,
    parse_anchors,
    verify_anchor_chain,
    verify_chain,
)


def _row(
    id,
    prev_hash,
    *,
    event="dispatch_started",
    payload=None,
    timestamp="2026-06-25T00:00:00+00:00",
):
    return ChainRow(
        id=id,
        timestamp=timestamp,
        event=event,
        run_id=None,
        issue_num=None,
        repo=None,
        actor_type="human",
        actor_id="tom",
        endpoint=None,
        policy_hash=None,
        payload=payload,
        prev_hash=prev_hash,
    )


def test_genesis_prev_is_64_zeros():
    assert GENESIS_PREV == "0" * 64


def test_entry_hash_is_deterministic():
    r = _row(1, GENESIS_PREV)
    assert entry_hash(r) == entry_hash(r)
    assert len(entry_hash(r)) == 64


def test_entry_hash_sensitive_to_every_field():
    base = _row(1, GENESIS_PREV)
    h = entry_hash(base)
    for field, value in [
        ("id", 2),
        ("event", "other"),
        ("actor_id", "someone"),
        ("payload", '{"x":1}'),
        ("prev_hash", "f" * 64),
        ("timestamp", "2026-06-26T00:00:00+00:00"),
    ]:
        assert entry_hash(dataclasses.replace(base, **{field: value})) != h, field


def test_entry_hash_hashes_payload_text_verbatim_not_reparsed():
    # Two byte-different but JSON-equivalent payloads must hash differently,
    # proving we hash the stored TEXT, not a re-serialized dict.
    a = _row(1, GENESIS_PREV, payload='{"a":1,"b":2}')
    b = _row(1, GENESIS_PREV, payload='{"b":2,"a":1}')
    assert entry_hash(a) != entry_hash(b)


def test_historical_verdict_payload_with_effort_still_verifies():
    """A04 deleted ``effort`` from the verdict policy block. The chain hashes
    the stored payload TEXT, not a schema, so pre-deletion rows that carry the
    key keep verifying — a payload-shape change is never a chain break.
    """
    legacy = _row(
        1,
        GENESIS_PREV,
        event="adversarial_review_verdict",
        payload='{"policy":{"blocking":true,"effort":"","max_rounds":2}}',
    )
    current = _row(
        2,
        entry_hash(legacy),
        event="adversarial_review_verdict",
        payload='{"policy":{"blocking":true,"max_rounds":2}}',
    )
    res = verify_chain([legacy, current])
    assert res.ok and res.broken_at_id is None and res.count == 2


def test_verify_chain_empty_is_vacuously_ok():
    r = verify_chain([])
    assert r == ChainResult(ok=True, broken_at_id=None, head_hash=None, count=0)


def test_verify_chain_intact():
    r1 = _row(1, GENESIS_PREV)
    r2 = _row(2, entry_hash(r1))
    r3 = _row(3, entry_hash(r2))
    res = verify_chain([r1, r2, r3])
    assert res.ok and res.broken_at_id is None and res.count == 3
    assert res.head_hash == entry_hash(r3)


def test_verify_chain_detects_edited_row():
    r1 = _row(1, GENESIS_PREV)
    r2 = _row(2, entry_hash(r1))
    tampered = dataclasses.replace(r1, actor_id="mallory")  # r2.prev_hash now dangling
    res = verify_chain([tampered, r2])
    assert not res.ok and res.broken_at_id == 2


def _format_two(row: ChainRow, *, item_id="SFD-1", dedup_key="once:1") -> ChainRow:
    return dataclasses.replace(
        row, chain_format=CHAIN_FORMAT_ITEM_KEYS, item_id=item_id, dedup_key=dedup_key
    )


def test_format_two_entry_hash_covers_item_id_and_dedup_key():
    row = _format_two(_row(1, GENESIS_PREV))
    assert entry_hash(dataclasses.replace(row, item_id="SFD-2")) != entry_hash(row)
    assert entry_hash(dataclasses.replace(row, item_id=None)) != entry_hash(row)
    assert entry_hash(dataclasses.replace(row, dedup_key="once:2")) != entry_hash(row)
    assert entry_hash(dataclasses.replace(row, dedup_key=None)) != entry_hash(row)
    # The format is in the preimage: relabelling the row legacy is an edit too.
    legacy = dataclasses.replace(row, chain_format=CHAIN_FORMAT_LEGACY)
    assert entry_hash(legacy) != entry_hash(row)


def test_format_two_preimage_is_the_literal_two_then_twelve_slots_then_item_keys():
    row = _format_two(_row(1, GENESIS_PREV, payload='{"a": 1}'))
    preimage = json.dumps(
        [
            2,
            1,
            "2026-06-25T00:00:00+00:00",
            "dispatch_started",
            None,
            None,
            None,
            "human",
            "tom",
            None,
            None,
            '{"a": 1}',
            GENESIS_PREV,
            "SFD-1",
            "once:1",
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    assert entry_hash(row) == hashlib.sha256(preimage.encode("utf-8")).hexdigest()


def test_format_one_row_hash_is_unchanged():
    """Rows written before the format step keep the hash the anchors cover.

    The pinned value is what main computed before the format step existed.
    """
    row = _row(1, GENESIS_PREV, payload='{"a": 1}')
    assert row.chain_format == CHAIN_FORMAT_LEGACY
    assert (
        entry_hash(row)
        == "1f5a64356e3ada8811d0571f86eea57b82662ac4dd82e382e268c71c2022e554"
    )
    # item_id and dedup_key stay out of a legacy row's hash.
    tagged = dataclasses.replace(row, item_id="SFD-1", dedup_key="once:1")
    assert entry_hash(tagged) == entry_hash(row)


def test_verify_chain_accepts_format_one_rows_then_format_two_rows():
    r1 = _row(1, GENESIS_PREV)
    r2 = _format_two(_row(2, entry_hash(r1)))
    r3 = _format_two(_row(3, entry_hash(r2)))
    res = verify_chain([r1, r2, r3])
    assert res.ok and res.count == 3 and res.head_hash == entry_hash(r3)


def test_verify_chain_refuses_a_format_one_row_after_a_format_two_row():
    r1 = _format_two(_row(1, GENESIS_PREV))
    r2 = _row(2, entry_hash(r1))  # linked correctly, but downgraded
    r3 = _format_two(_row(3, entry_hash(r2)))
    res = verify_chain([r1, r2, r3])
    assert not res.ok and res.broken_at_id == 2


def test_verify_chain_refuses_an_unknown_chain_format():
    r1 = _row(1, GENESIS_PREV)
    r2 = dataclasses.replace(_format_two(_row(2, entry_hash(r1))), chain_format=3)
    res = verify_chain([r1, r2])
    assert not res.ok and res.broken_at_id == 2


def test_verify_chain_reports_a_non_integer_chain_format_as_a_break():
    r1 = _format_two(_row(1, GENESIS_PREV))
    r2 = dataclasses.replace(_format_two(_row(2, entry_hash(r1))), chain_format="2")
    r3 = _format_two(_row(3, entry_hash(r2)))
    res = verify_chain([r1, r2, r3])
    assert not res.ok and res.broken_at_id == 2


def test_verify_chain_detects_edited_item_id_on_a_format_two_row():
    r1 = _format_two(_row(1, GENESIS_PREV))
    r2 = _format_two(_row(2, entry_hash(r1)))
    res = verify_chain([dataclasses.replace(r1, item_id="SFD-9"), r2])
    assert not res.ok and res.broken_at_id == 2


def test_verify_chain_detects_deleted_middle_row():
    r1 = _row(1, GENESIS_PREV)
    r2 = _row(2, entry_hash(r1))
    r3 = _row(3, entry_hash(r2))
    res = verify_chain([r1, r3])  # r2 deleted; r3.prev_hash dangles
    assert not res.ok and res.broken_at_id == 3


def test_verify_chain_does_not_assume_contiguous_ids():
    # A gap (id 2 rolled back) is fine as long as links hold.
    r1 = _row(1, GENESIS_PREV)
    r3 = _row(3, entry_hash(r1))
    assert verify_chain([r1, r3]).ok


def test_anchor_round_trip():
    a = Anchor(
        id=7, entry_hash="a" * 64, count=7, timestamp="2026-06-25T00:00:00+00:00"
    )
    line = format_anchor(a)
    assert "\n" not in line
    assert parse_anchors(line + "\n") == [a]


def test_legacy_anchor_hash_and_format_are_frozen():
    anchor = Anchor(
        id=7,
        entry_hash="a" * 64,
        count=7,
        timestamp="2026-06-25T00:00:00+00:00",
        prev="b" * 64,
    )
    assert (
        anchor_hash(anchor)
        == "1fe70ed7d8b193c031c0e6a745d31bc19fe9efb47e32b9a8c94a10c4ff86882e"
    )
    assert format_anchor(anchor) == (
        '{"id":7,"entry_hash":"'
        + "a" * 64
        + '","count":7,"timestamp":"2026-06-25T00:00:00+00:00","prev":"'
        + "b" * 64
        + '"}'
    )


def test_v2_anchor_round_trip_and_hash_preimage():
    ledger_id = "12345678-1234-5678-9234-567812345678"
    anchor = Anchor(
        id=7,
        entry_hash="a" * 64,
        count=7,
        timestamp="2026-06-25T00:00:00+00:00",
        prev="b" * 64,
        version=2,
        ledger_id=ledger_id,
    )
    assert parse_anchors(format_anchor(anchor)) == [anchor]
    assert json.loads(format_anchor(anchor)) == {
        "version": 2,
        "ledger_id": ledger_id,
        "id": 7,
        "entry_hash": "a" * 64,
        "count": 7,
        "timestamp": "2026-06-25T00:00:00+00:00",
        "prev": "b" * 64,
    }
    preimage = json.dumps(
        [2, ledger_id, 7, "a" * 64, 7, "2026-06-25T00:00:00+00:00", "b" * 64],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    assert anchor_hash(anchor) == hashlib.sha256(preimage.encode()).hexdigest()


def test_anchor_history_allows_only_one_legacy_to_v2_transition():
    ledger_id = "12345678-1234-5678-9234-567812345678"
    legacy = _anchor(1, 1)
    v2 = dataclasses.replace(_anchor(2, 2), version=2, ledger_id=ledger_id)
    assert parse_anchors("\n".join((format_anchor(legacy), format_anchor(v2)))) == [
        legacy,
        v2,
    ]
    # Anchored regex: the message is the operator's whole diagnosis, so a
    # mutation that pads or rewrites it must fail (#1175).
    with pytest.raises(ValueError, match=r"^legacy anchor after v2 transition$"):
        parse_anchors("\n".join((format_anchor(v2), format_anchor(legacy))))


def test_anchor_history_rejects_inconsistent_v2_ledger_ids():
    first = dataclasses.replace(
        _anchor(1, 1),
        version=2,
        ledger_id="12345678-1234-5678-9234-567812345678",
    )
    second = dataclasses.replace(
        _anchor(2, 2),
        version=2,
        ledger_id="87654321-4321-5678-9234-567812345678",
    )
    with pytest.raises(ValueError, match=r"^inconsistent ledger_id in anchor history$"):
        parse_anchors("\n".join((format_anchor(first), format_anchor(second))))


def test_identity_mismatch_precedes_count_classification():
    ledger_id = "12345678-1234-5678-9234-567812345678"
    anchor = dataclasses.replace(_anchor(99, 99), version=2, ledger_id=ledger_id)
    report = classify_anchors(
        [], [anchor], ledger_id="87654321-4321-5678-9234-567812345678"
    )
    assert not report.ok
    assert report.wrong_ledger
    assert not report.count_shrink


def test_v2_anchor_without_live_ledger_id_uses_normal_classification():
    ledger_id = "12345678-1234-5678-9234-567812345678"
    row = _row(1, GENESIS_PREV)
    anchor = dataclasses.replace(
        _anchor(1, 1),
        entry_hash=entry_hash(row),
        version=2,
        ledger_id=ledger_id,
    )

    report = classify_anchors([row], [anchor])

    assert report.ok
    assert not report.wrong_ledger
    assert report.matched == 1


def test_parse_anchors_skips_blank_lines():
    a = Anchor(
        id=1, entry_hash="b" * 64, count=1, timestamp="2026-06-25T00:00:00+00:00"
    )
    assert parse_anchors("\n" + format_anchor(a) + "\n\n") == [a]


def test_classify_anchors_all_match():
    r1 = _row(1, GENESIS_PREV)
    r2 = _row(2, entry_hash(r1))
    rows = [r1, r2]
    anchors = [Anchor(id=2, entry_hash=entry_hash(r2), count=2, timestamp="t")]
    rep = classify_anchors(rows, anchors)
    assert rep == AnchorReport(ok=True, matched=1, mismatched=[], absent=[], lagging=[])


def test_classify_anchors_mismatch_is_tamper():
    r1 = _row(1, GENESIS_PREV)
    anchors = [Anchor(id=1, entry_hash="f" * 64, count=1, timestamp="t")]
    rep = classify_anchors([r1], anchors)
    assert not rep.ok and rep.mismatched == [1]


def test_classify_anchors_absent_in_range_is_tamper():
    r1 = _row(1, GENESIS_PREV)
    r3 = _row(3, entry_hash(r1))
    # anchor for id 2 which is <= head (3) but absent -> tamper
    anchors = [Anchor(id=2, entry_hash="c" * 64, count=2, timestamp="t")]
    rep = classify_anchors([r1, r3], anchors)
    assert not rep.ok and rep.absent == [2]


def test_classify_anchors_lagging_beyond_window_is_shrink():
    # #912: a lagging anchor (id/count beyond the local head) is no longer
    # automatically benign — with the default zero window it is count-shrink
    # (the live chain trails the latest anchor), so ok fails closed.
    r1 = _row(1, GENESIS_PREV)
    anchors = [Anchor(id=5, entry_hash="d" * 64, count=5, timestamp="t")]
    rep = classify_anchors([r1], anchors)
    assert not rep.ok and rep.count_shrink and rep.lagging == [5]


def test_classify_anchors_lagging_within_window_is_tolerated():
    # #912: a lag within the configured staleness window is tolerated (ok), so a
    # clone that trails the anchor by a bounded amount still verifies.
    r1 = _row(1, GENESIS_PREV)
    anchors = [Anchor(id=5, entry_hash="d" * 64, count=5, timestamp="t")]
    rep = classify_anchors([r1], anchors, staleness_window=4)
    assert rep.ok and not rep.count_shrink and rep.lagging == [5]


def test_count_shrink_among_anchors_classifies_tamper():
    # A later non-lagging anchor with count < an earlier anchor's count means
    # rows were removed after the earlier anchor was taken — tamper (A3).
    r1 = _row(1, GENESIS_PREV)
    r2 = _row(2, entry_hash(r1))
    r3 = _row(3, entry_hash(r2))
    rows = [r1, r2, r3]
    anchors = [
        Anchor(id=2, entry_hash=entry_hash(r2), count=5, timestamp="t"),
        Anchor(id=3, entry_hash=entry_hash(r3), count=3, timestamp="t"),  # count shrank
    ]
    # A generous window would still not excuse a non-monotonic anchor FILE.
    rep = classify_anchors(rows, anchors, staleness_window=1000)
    assert not rep.ok and rep.count_shrink


def test_count_shrink_relative_to_latest_anchor_is_tamper():
    # #912: the live chain lost rows below the latest anchor's count — the
    # exact field a DB-level adversary would demote to advisory. Fails closed.
    r1 = _row(1, GENESIS_PREV)
    r2 = _row(2, entry_hash(r1))
    rows = [r1, r2]  # live count 2
    anchors = [Anchor(id=2, entry_hash=entry_hash(r2), count=9, timestamp="t")]
    rep = classify_anchors(rows, anchors)
    assert not rep.ok and rep.count_shrink


def test_live_count_at_or_ahead_of_latest_anchor_is_ok():
    # Honest operation: the live DB has grown past the latest anchor. No shrink.
    r1 = _row(1, GENESIS_PREV)
    r2 = _row(2, entry_hash(r1))
    r3 = _row(3, entry_hash(r2))
    rows = [r1, r2, r3]  # live count 3
    anchors = [Anchor(id=2, entry_hash=entry_hash(r2), count=2, timestamp="t")]
    rep = classify_anchors(rows, anchors)
    assert rep.ok and not rep.count_shrink


# ---------------------------------------------------------------------------
# Anchor chain: each new anchor binds the previous anchor's hash (#912)
# ---------------------------------------------------------------------------


def _anchor(id, count, *, prev=None, eh=None):
    return Anchor(
        id=id, entry_hash=(eh or ("a" * 64)), count=count, timestamp="t", prev=prev
    )


def test_anchor_prev_defaults_none_and_round_trips():
    a = _anchor(3, 3, prev="b" * 64)
    assert parse_anchors(format_anchor(a) + "\n") == [a]


def test_parse_anchors_reads_legacy_line_without_prev():
    # Pre-#912 anchor files have no "prev" key — parse must default it to None.
    legacy = '{"id":1,"entry_hash":"' + "a" * 64 + '","count":1,"timestamp":"t"}'
    assert parse_anchors(legacy + "\n") == [_anchor(1, 1, prev=None)]


def test_anchor_hash_binds_every_field():
    base = _anchor(1, 1, prev=GENESIS_PREV)
    h = anchor_hash(base)
    for field, value in [
        ("id", 2),
        ("entry_hash", "f" * 64),
        ("count", 9),
        ("timestamp", "later"),
        ("prev", "c" * 64),
    ]:
        assert anchor_hash(dataclasses.replace(base, **{field: value})) != h, field


def test_verify_anchor_chain_intact():
    a1 = _anchor(1, 1, prev=GENESIS_PREV)
    a2 = _anchor(2, 2, prev=anchor_hash(a1))
    a3 = _anchor(3, 3, prev=anchor_hash(a2))
    res = verify_anchor_chain([a1, a2, a3])
    assert res.ok and res.broken_at_index is None and res.checked == 3
    assert res.head_hash == anchor_hash(a3)


def test_verify_anchor_chain_detects_rechained_history():
    # An adversary who rewrites the audit log and re-anchors it cannot reproduce
    # the git-tracked prev links: the forged anchor's prev no longer binds a1.
    a1 = _anchor(1, 1, prev=GENESIS_PREV)
    a2_forged = _anchor(2, 2, prev="d" * 64)  # not anchor_hash(a1)
    res = verify_anchor_chain([a1, a2_forged])
    assert not res.ok and res.broken_at_index == 1


def test_verify_anchor_chain_skips_legacy_unlinked_prev():
    # Legacy anchors (prev=None) can't be link-checked but still form the
    # preimage for the next anchor's prev.
    legacy = _anchor(1, 1, prev=None)
    following = _anchor(2, 2, prev=anchor_hash(legacy))
    res = verify_anchor_chain([legacy, following])
    assert res.ok and res.checked == 1


def test_verify_anchor_chain_skips_a_whole_legacy_prefix():
    # Every anchor written before #912 lacks prev; the unlinked prefix is
    # tolerated wholesale, and link checking starts at the first linked anchor.
    l1 = _anchor(1, 1, prev=None)
    l2 = _anchor(2, 2, prev=None)
    linked = _anchor(3, 3, prev=anchor_hash(l2))
    res = verify_anchor_chain([l1, l2, linked])
    assert res.ok and res.checked == 1 and res.total == 3


def test_verify_anchor_chain_missing_prev_after_a_linked_anchor_is_a_break():
    # #1122: dropping "prev" must not opt an anchor out of the re-chain defence.
    # Once the chain carries links, a prev-less anchor is forged history.
    a1 = _anchor(1, 1, prev=GENESIS_PREV)
    a2_stripped = _anchor(2, 2, prev=None)
    res = verify_anchor_chain([a1, a2_stripped])
    assert not res.ok and res.broken_at_index == 1 and res.checked == 1


def test_verify_anchor_chain_reports_first_missing_prev_after_linking():
    a1 = _anchor(1, 1, prev=GENESIS_PREV)
    a2 = _anchor(2, 2, prev=anchor_hash(a1))
    stripped = _anchor(3, 3, prev=None)
    following = _anchor(4, 4, prev=anchor_hash(stripped))
    res = verify_anchor_chain([a1, a2, stripped, following])
    assert not res.ok and res.broken_at_index == 2


def test_verify_anchor_chain_totals_every_anchor_walked():
    legacy = [_anchor(i, i, prev=None) for i in (1, 2)]
    res = verify_anchor_chain(legacy)
    assert res.ok and res.checked == 0 and res.total == 2


def test_verify_anchor_chain_first_prev_must_be_genesis():
    a1_bad = _anchor(1, 1, prev="e" * 64)  # first anchor not bound to genesis
    res = verify_anchor_chain([a1_bad])
    assert not res.ok and res.broken_at_index == 0


# ---------------------------------------------------------------------------
# Mutation-derived assertions (#1175, Task 83)
#
# Each test below kills at least one mutant that survived `mutmut run` over
# src/kew/audit_chain.py. The mutants they answer are named in the docstrings so
# a later reader can tell a load-bearing assertion from a decorative one.
#
# Documented EQUIVALENT survivors — behaviour-preserving, deliberately not
# asserted (a test for them would pin an implementation detail, not a promise):
#   * entry_hash 10 / anchor_hash 14 — `separators=(",", "XX:XX")`. Both dump a
#     JSON *array*, which never emits a key separator, so the second element of
#     the tuple is unreachable. format_anchor dumps an object, and there the same
#     mutation IS killed (test_legacy_anchor_hash_and_format_are_frozen).
#   * entry_hash 14 / anchor_hash 18 — `.encode("utf-8")` -> `.encode("UTF-8")`.
#     Python normalises codec names case-insensitively; identical bytes.
#   * parse_anchors 3, verify_anchor_chain 4, _partition_anchors 6 — a `False`
#     accumulator initialised to `None`. Each is only ever read for truthiness
#     inside its own function and is folded into a bool (`or`, `and`) before it
#     can escape, so no caller can observe the difference.
# ---------------------------------------------------------------------------


def test_entry_hash_canonical_preimage_is_frozen():
    """entry_hash 3/4/7/8/9: the canonical JSON options had no assertion.

    Every anchor ever published commits to this exact byte string. Whitespace
    from a dropped `separators`, or `sort_keys`, silently invalidates the
    entire anchor history — and the existing tests only compared entry_hash to
    itself, so any of those changes passed. The expected preimage is spelled
    out by hand on purpose: it is the wire format, not a re-derivation of the
    code under test.

    Not a kill: entry_hash 6 drops `ensure_ascii=True`, which is json.dumps's
    default — equivalent by construction (confirmed by a mutmut re-run under
    review). This test still pins the ASCII wire form for the day the default
    changes.
    """
    row = _row(1, GENESIS_PREV, payload='{"note":"café"}')
    canonical = (
        '[1,"2026-06-25T00:00:00+00:00","dispatch_started",null,null,null,'
        '"human","tom",null,null,'
        '"{\\"note\\":\\"caf\\u00e9\\"}",'
        '"' + GENESIS_PREV + '"]'
    )
    assert canonical.isascii()
    assert entry_hash(row) == hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def test_anchor_canonical_form_escapes_non_ascii():
    """anchor_hash 7/12 and format_anchor 52/57: `ensure_ascii` flipped to False.

    Anchor lines are committed to `audit-anchors.jsonl` and travel through git,
    diffs and mail. `ensure_ascii=True` keeps that file pure ASCII whatever a
    field holds; the frozen-format test only ever used ASCII inputs, so flipping
    it changed nothing there.

    Not kills: anchor_hash 10 and format_anchor 55 DROP `ensure_ascii=True`,
    which is json.dumps's default — equivalent by construction (mutmut re-run
    under review). This test pins the ASCII form regardless.
    """
    smuggled = dataclasses.replace(
        _anchor(1, 1, prev=GENESIS_PREV), timestamp="2026-06-25T00:00:00+00:00 café"
    )
    line = format_anchor(smuggled)
    assert line.isascii(), line
    assert "caf\\u00e9" in line
    preimage = (
        '[1,"'
        + "a" * 64
        + '",1,"2026-06-25T00:00:00+00:00 caf\\u00e9","'
        + GENESIS_PREV
        + '"]'
    )
    assert anchor_hash(smuggled) == hashlib.sha256(preimage.encode("utf-8")).hexdigest()


def test_format_anchor_tolerates_duck_typed_anchors():
    """format_anchor 5/20/48: the `getattr` defaults had no assertion.

    The source comment says these keep duck-typed callers working; nothing
    proved it, so deleting the default (turning a missing attribute into an
    AttributeError) survived.
    """
    legacy = SimpleNamespace(id=1, entry_hash="a" * 64, count=1, timestamp="t")
    assert json.loads(format_anchor(legacy)) == {
        "id": 1,
        "entry_hash": "a" * 64,
        "count": 1,
        "timestamp": "t",
        "prev": None,
    }
    v2 = SimpleNamespace(
        version=2, id=1, entry_hash="a" * 64, count=1, timestamp="t", prev=None
    )
    assert json.loads(format_anchor(v2))["ledger_id"] is None


def test_anchor_line_must_be_a_json_object():
    """_parse_anchor_line 4/5/6/7: the TypeError message had no assertion."""
    with pytest.raises(TypeError, match=r"^anchor line must be a JSON object$"):
        parse_anchors("[1,2]\n")


def test_legacy_anchor_cannot_carry_a_ledger_id():
    """_validated_anchor_identity 6/7/8: an unversioned line naming a ledger."""
    line = json.dumps(
        {
            "id": 1,
            "entry_hash": "a" * 64,
            "count": 1,
            "timestamp": "t",
            "ledger_id": "12345678-1234-5678-9234-567812345678",
        }
    )
    with pytest.raises(ValueError, match=r"^legacy anchor cannot carry ledger_id$"):
        parse_anchors(line + "\n")


def test_unknown_anchor_version_is_rejected_by_number():
    """_validated_anchor_identity 11: the version in the message is the clue."""
    line = json.dumps(
        {"version": 3, "id": 1, "entry_hash": "a" * 64, "count": 1, "timestamp": "t"}
    )
    with pytest.raises(ValueError, match=r"^unsupported anchor version: 3$"):
        parse_anchors(line + "\n")


def test_v2_anchor_without_a_ledger_id_is_rejected():
    """_validated_anchor_identity 13/14/15."""
    line = json.dumps(
        {"version": 2, "id": 1, "entry_hash": "a" * 64, "count": 1, "timestamp": "t"}
    )
    with pytest.raises(ValueError, match=r"^v2 anchor requires ledger_id$"):
        parse_anchors(line + "\n")


def test_v2_ledger_id_must_be_a_uuid():
    """_validated_anchor_identity 18/19/20/21."""
    line = json.dumps(
        {
            "version": 2,
            "id": 1,
            "entry_hash": "a" * 64,
            "count": 1,
            "timestamp": "t",
            "ledger_id": "not-a-uuid",
        }
    )
    with pytest.raises(ValueError, match=r"^v2 anchor ledger_id is not a UUID$"):
        parse_anchors(line + "\n")


def test_v2_ledger_id_must_be_in_canonical_form():
    """_validated_anchor_identity 24/25/26.

    `uuid.UUID` happily parses a braced or upper-cased spelling, so identity
    comparison would depend on formatting unless the canonical form is required.
    """
    line = json.dumps(
        {
            "version": 2,
            "id": 1,
            "entry_hash": "a" * 64,
            "count": 1,
            "timestamp": "t",
            "ledger_id": "{12345678-1234-5678-9234-567812345678}",
        }
    )
    with pytest.raises(ValueError, match=r"^v2 anchor ledger_id must be canonical$"):
        parse_anchors(line + "\n")


def test_verify_anchor_chain_empty_has_no_head():
    """verify_anchor_chain 7: `head_hash` starts as None, not "".

    A caller continues the chain from `head_hash`; an empty string is a value,
    and would be written as one anchor's `prev`.
    """
    assert verify_anchor_chain([]) == AnchorChainResult(
        ok=True, broken_at_index=None, checked=0, head_hash=None, total=0
    )


def test_identity_unanchored_report_is_fully_specified():
    """_anchor_identity_report 7-21: the whole branch was unexercised.

    A ledger that knows its own id, with an anchor file that names none, is not
    verified — it is unanchored, and every other field of the verdict must say
    "nothing was checked" rather than carry arithmetic from a chain nobody
    compared.
    """
    row = _row(1, GENESIS_PREV)
    legacy = dataclasses.replace(_anchor(1, 1), entry_hash=entry_hash(row))
    rep = classify_anchors(
        [row], [legacy], ledger_id="12345678-1234-5678-9234-567812345678"
    )
    assert rep == AnchorReport(
        ok=False,
        matched=0,
        mismatched=[],
        absent=[],
        lagging=[],
        identity_unanchored=True,
    )


def test_wrong_ledger_report_is_fully_specified():
    """_anchor_identity_report 25-29/38: the branch was exercised, not asserted."""
    anchor = dataclasses.replace(
        _anchor(99, 99), version=2, ledger_id="12345678-1234-5678-9234-567812345678"
    )
    rep = classify_anchors(
        [], [anchor], ledger_id="87654321-4321-5678-9234-567812345678"
    )
    assert rep == AnchorReport(
        ok=False,
        matched=0,
        mismatched=[],
        absent=[],
        lagging=[],
        wrong_ledger=True,
    )


def test_classify_anchors_with_nothing_to_compare_is_vacuously_ok():
    """classify_anchors 23/25/26: `max(..., default=0)` over no anchors.

    A fresh install has no anchors at all; the high-water mark is 0, not 1 and
    not an error.
    """
    assert classify_anchors([], []) == AnchorReport(
        ok=True, matched=0, mismatched=[], absent=[], lagging=[]
    )


def test_classify_anchors_with_no_rows_treats_every_anchor_as_lagging():
    """classify_anchors 12: the empty-chain head id is 0, so id 1 is ahead."""
    rep = classify_anchors([], [_anchor(1, 1)], staleness_window=1)
    assert rep.ok and rep.lagging == [1] and rep.absent == []


def test_default_staleness_window_tolerates_no_lag_at_all():
    """classify_anchors 1: the default window is 0, and that is the promise.

    Every existing test either passed a window explicitly or lagged by more than
    one, so a default of 1 — one silently tolerated missing event — passed them.
    Hand-applying the mutant fails this test; mutmut 3 cannot see it (its
    trampoline keeps the original signature and forwards `staleness_window`
    explicitly), so the survivor is a tooling artefact, recorded as such.
    """
    row = _row(1, GENESIS_PREV)
    anchors = [Anchor(id=1, entry_hash=entry_hash(row), count=2, timestamp="t")]
    rep = classify_anchors([row], anchors)
    assert not rep.ok and rep.count_shrink


def test_re_anchoring_at_the_same_height_is_not_shrink():
    """_partition_anchors 13 (`<` not `<=`) and 21 (`matched += 1`).

    Anchoring twice with no events in between is routine — the workflow runs on
    a schedule. Equal counts are monotonic, and both anchors count as matched.
    """
    r1 = _row(1, GENESIS_PREV)
    r2 = _row(2, entry_hash(r1))
    anchor = Anchor(id=2, entry_hash=entry_hash(r2), count=2, timestamp="t")
    rep = classify_anchors([r1, r2], [anchor, anchor])
    assert rep.ok and not rep.count_shrink and rep.matched == 2


def test_classify_anchors_does_not_assume_anchor_file_order():
    """_partition_anchors 12: a lagging anchor is skipped, not a stop signal.

    Nothing sorts the anchor file, so turning the `continue` into a `break`
    would silently stop checking at the first anchor ahead of the local head.
    """
    row = _row(1, GENESIS_PREV)
    ahead = Anchor(id=9, entry_hash="d" * 64, count=1, timestamp="t")
    behind = Anchor(id=1, entry_hash=entry_hash(row), count=1, timestamp="t")
    rep = classify_anchors([row], [ahead, behind], staleness_window=1)
    assert rep.matched == 1 and rep.lagging == [9]


def test_an_anchor_taken_before_any_event_is_not_nonmonotonic():
    """_partition_anchors 9: the count high-water mark starts at 0."""
    row = _row(1, GENESIS_PREV)
    anchors = [Anchor(id=1, entry_hash=entry_hash(row), count=0, timestamp="t")]
    rep = classify_anchors([row], anchors)
    assert rep.ok and not rep.count_shrink and rep.matched == 1
