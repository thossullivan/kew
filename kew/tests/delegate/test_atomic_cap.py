"""Atomic fleet-cap admission on the delegation reserve path (N10, #959).

``delegate/run.py`` read ``live_run_count()`` and THEN reserved — a TOCTOU the
budget cap already closed with an atomic reserve-and-check (#933/#939). Two
concurrent surfaces (watcher + driver, or two watchers) could each read the same
sub-cap count and each reserve, pushing the fleet past ``[parallel]
max_workers``. These pin the folded count-and-insert transaction.
"""

from __future__ import annotations

import threading

from kew.delegate.claim import DELEGATION_EPIC_REF
from kew.run_store import SqliteRunStore

EPIC_REF = DELEGATION_EPIC_REF


def test_reserve_under_cap_admits_below_and_refuses_at_cap(tmp_path):
    """Reserve succeeds below the cap and returns ``fleet_full`` at the cap."""
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        assert (
            store.reserve_epic_node_item_under_cap(EPIC_REF, "SFD-1", "r1", 2)
            == "reserved"
        )
        assert (
            store.reserve_epic_node_item_under_cap(EPIC_REF, "SFD-2", "r2", 2)
            == "reserved"
        )
        # The fleet now holds 2 live slots == cap: the next admit is refused.
        assert (
            store.reserve_epic_node_item_under_cap(EPIC_REF, "SFD-3", "r3", 2)
            == "fleet_full"
        )
        assert {r.item_id for r in store.list_epic_reservations(EPIC_REF)} == {
            "SFD-1",
            "SFD-2",
        }


def test_reserve_under_cap_lost_claim_on_duplicate(tmp_path):
    """A second reserve of the same item is a clean ``lost_claim`` no-op."""
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        assert (
            store.reserve_epic_node_item_under_cap(EPIC_REF, "SFD-1", "r1", 5)
            == "reserved"
        )
        assert (
            store.reserve_epic_node_item_under_cap(EPIC_REF, "SFD-1", "r2", 5)
            == "lost_claim"
        )


def test_cap_zero_disables_the_check(tmp_path):
    """A cap of 0 always reserves (unless the item is already taken)."""
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        for i in range(5):
            assert (
                store.reserve_epic_node_item_under_cap(EPIC_REF, f"SFD-{i}", f"r{i}", 0)
                == "reserved"
            )
        assert len(store.list_epic_reservations(EPIC_REF)) == 5


def test_concurrent_admits_never_exceed_cap(tmp_path):
    """N racing reserves of DISTINCT items against a 1-slot cap → exactly one wins.

    Each thread on its own connection (the production per-worker pattern) reserves
    a distinct item under a cap of 1. Because the count and the insert are one
    ``BEGIN IMMEDIATE`` transaction the contenders serialize: the first inserts
    its row and admits, and every later contender sees the fleet already at the
    cap and is refused. Total reservations must never exceed the cap.
    """
    n_workers = 5
    cap = 1
    db_path = str(tmp_path / "concurrent.db")
    SqliteRunStore(db_path).close()  # create the schema up front

    results: list[str | Exception] = []
    barrier = threading.Barrier(n_workers)
    lock = threading.Lock()

    def worker(i: int) -> None:
        store = SqliteRunStore(db_path)
        try:
            barrier.wait(timeout=5)
        except threading.BrokenBarrierError:
            with lock:
                results.append(RuntimeError("barrier timeout"))
            return
        try:
            outcome = store.reserve_epic_node_item_under_cap(
                EPIC_REF, f"SFD-{i}", f"r{i}", cap
            )
            with lock:
                results.append(outcome)
        finally:
            store.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert results.count("reserved") == 1
    assert results.count("fleet_full") == n_workers - 1
    with SqliteRunStore(db_path) as store:
        assert len(store.list_epic_reservations(EPIC_REF)) == cap
        assert store.live_run_count() == cap
