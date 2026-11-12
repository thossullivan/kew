"""One motion vocabulary: only what kew is executing moves (#2305).

The table itself is held to the frontend's copy by ``test_motion_parity.py``.
This module is about the readings the table alone cannot make — a pending run
whose owner is gone, a pursuing loop nothing has advanced — and about the DTOs
never serving a motion their status contradicts.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from kew.motion import (
    FALLBACK_MOTION,
    LOOP_STALE_AFTER_SECS,
    RUN_MOTION_OF,
    is_alive,
    loop_motion,
    motion_of,
    run_motion,
)
from kew.service.loops import _loop_to_dto
from kew.service.models import EpicNodeDTO, LoopDTO, RunDTO


@pytest.mark.parametrize(
    ("status", "motion"),
    [
        # The three node statuses where kew is running code.
        ("in_progress", "executing"),
        ("adversarial_review", "executing"),
        ("remediating", "executing"),
        # The two the console used to get backwards: a published pull request
        # waits on a PERSON, and completed work waits on kew.
        ("in_review", "waiting_human"),
        ("awaiting_publication", "waiting_kew"),
        ("todo", "waiting_kew"),
        ("queued", "waiting_human"),
        ("held", "waiting_human"),
        ("done", "settled"),
        ("cancelled", "settled"),
    ],
)
def test_node_statuses_read_their_motion(status: str, motion: str) -> None:
    assert motion_of(status) == motion


def test_a_status_word_is_read_however_it_is_spelled() -> None:
    """`in-progress`, `In Progress` and `in_progress` are one status."""
    assert motion_of("In Progress") == "executing"
    assert motion_of("in-progress") == "executing"
    assert motion_of("  IN_PROGRESS  ") == "executing"


def test_an_unknown_status_neither_moves_nor_claims_to_be_over() -> None:
    """The forward-compatible degradation, in both directions.

    A status a later kew adds must not earn a halo by default, and must not be
    reported as history either — `alive` has to stay true for work that may
    well still be in flight.
    """
    assert motion_of("teleporting") == FALLBACK_MOTION
    assert motion_of("") == FALLBACK_MOTION
    assert motion_of(None) == FALLBACK_MOTION
    assert is_alive(FALLBACK_MOTION)
    assert FALLBACK_MOTION != "executing"


def test_prototype_members_are_not_statuses() -> None:
    """A status word off the wire cannot reach a dict's own attributes."""
    assert motion_of("keys") == FALLBACK_MOTION
    assert motion_of("__class__") == FALLBACK_MOTION


def test_alive_is_everything_but_settled() -> None:
    assert is_alive("executing")
    assert is_alive("waiting_kew")
    assert is_alive("waiting_human")
    assert not is_alive("settled")


def test_a_queued_run_is_kews_own_turn_and_a_queued_node_is_a_persons() -> None:
    """The one word the two vocabularies spell alike and read oppositely.

    An epic node is `queued` while nobody has armed it — the Linear adapter
    reports it precisely when the marker a PERSON adds is absent (#2209). A run
    is `queued` only as the console's own display word for an untouched
    `pending` row (`displayStatus` in kpi.ts), which the dispatcher picks up on
    its own turn. Read in the wrong vocabulary, every queued run would be
    reported as somebody's wait.
    """
    assert motion_of("queued") == "waiting_human"
    assert motion_of("queued", vocabulary="epic") == "waiting_human"
    assert motion_of("queued", vocabulary="run") == "waiting_kew"
    assert run_motion("queued", owner_alive=None) == "waiting_kew"


def test_the_run_vocabulary_refines_that_word_and_no_other() -> None:
    """Every other word reads the same whichever vocabulary served it.

    A second collision is a decision, not an accident: it has to be added to
    `RUN_MOTION_OF` (and to `motion.ts`'s copy, which
    `test_motion_parity.py` holds it to) rather than appearing here.
    """
    assert set(RUN_MOTION_OF) == {"queued"}
    assert run_motion("in_progress", owner_alive=True) == "executing"
    assert run_motion("teleporting", owner_alive=None) == FALLBACK_MOTION


def test_a_pending_run_with_a_dead_owner_is_not_executing() -> None:
    """An orphan is work kew owes a reap, not work in flight.

    The rule, for a caller that already holds a liveness reading. The console's
    read path does NOT take one — establishing it means reading the host
    process table, and a Flight read may never shell out (see
    `_record_to_run_dto`) — so a pending row is served `executing` there until
    the driver's own tick reaps it and records `killed`.
    """
    assert run_motion("pending", owner_alive=True) == "executing"
    assert run_motion("pending", owner_alive=False) == "waiting_kew"


def test_an_undeterminable_owner_leaves_a_pending_run_executing() -> None:
    """Act on False, never on None — the reaper's own asymmetry.

    A run with no recorded owner identity, or one inside a container the cheap
    probe cannot ask about, must not be declared stopped by something that
    merely could not see it.
    """
    assert run_motion("pending", owner_alive=None) == "executing"


def test_liveness_cannot_revive_a_terminal_run() -> None:
    """Only the executing reading is refined; a settled row stays settled."""
    assert run_motion("success", owner_alive=True) == "settled"
    assert run_motion("killed", owner_alive=None) == "settled"


def test_a_stale_pursuing_loop_stops_moving() -> None:
    fresh = LOOP_STALE_AFTER_SECS - 1
    stale = LOOP_STALE_AFTER_SECS + 1
    assert loop_motion("pursuing", age_secs=fresh) == "executing"
    assert loop_motion("pursuing", age_secs=stale) == "waiting_kew"


def test_an_unrecorded_loop_age_establishes_no_staleness() -> None:
    assert loop_motion("pursuing", age_secs=None) == "executing"


def test_staleness_cannot_move_a_resumable_or_terminal_loop() -> None:
    old = LOOP_STALE_AFTER_SECS * 10
    assert loop_motion("needs_human", age_secs=old) == "waiting_human"
    assert loop_motion("achieved", age_secs=old) == "settled"


def test_a_node_dto_derives_its_motion_from_its_status() -> None:
    """The builder passes no motion, so the DTO cannot contradict the status."""
    node = EpicNodeDTO(id="2305", status="adversarial_review")
    assert (node.motion, node.alive) == ("executing", True)
    settled = EpicNodeDTO(id="2305", status="done")
    assert (settled.motion, settled.alive) == ("settled", False)


def test_a_dto_motion_the_builder_passes_wins() -> None:
    """The refined reading is the point: it must survive the derivation."""
    run = RunDTO(
        run_id="r1",
        issue_num=1,
        title="t",
        runner="claude-code",
        model="claude-opus-5",
        status="pending",
        cost_usd=0.0,
        motion="waiting_kew",
    )
    assert (run.motion, run.alive) == ("waiting_kew", True)


def test_a_run_dto_reads_its_status_in_the_run_vocabulary() -> None:
    """The DTO that can be handed a display word reads it as one.

    `queued` is the collision: on a node it means a person has to arm the work,
    on a run it is the console's own word for a `pending` row the dispatcher
    starts by itself. The DTO says which vocabulary it is spelled in so no
    builder has to remember (`motion_vocabulary`).
    """
    run = RunDTO(
        run_id="r1",
        issue_num=1,
        title="t",
        runner="claude-code",
        model="claude-opus-5",
        status="queued",
        cost_usd=0.0,
    )
    assert (run.motion, run.alive) == ("waiting_kew", True)
    assert EpicNodeDTO(id="2305", status="queued").motion == "waiting_human"


def test_an_unknown_dto_status_degrades_rather_than_raising() -> None:
    node = EpicNodeDTO(id="2305", status="teleporting")
    assert (node.motion, node.alive) == (FALLBACK_MOTION, True)


class _FakeLoop:
    """Structural stand-in for a LoopRecord (the service never imports one)."""

    def __init__(self, **kw: Any) -> None:
        self.loop_id = "L1"
        self.source = "issue"
        self.issue_num = 2305
        self.repo = "thossullivan/kew"
        self.goal_text = "make CI green"
        self.runner_name = "claude-code"
        self.model = "claude-opus-5"
        self.status = "pursuing"
        self.status_reason = ""
        self.budget_usd: float | None = 5.0
        self.spent_usd = 1.25
        self.wall_clock_secs = 60
        self.max_steps = 20
        self.step_count = 2
        self.created_at = datetime(2026, 10, 6, tzinfo=UTC)
        self.updated_at: datetime | None = None
        self.finished_at: datetime | None = None
        self.__dict__.update(kw)


def _loop_dto(updated_at: Any, now: datetime) -> LoopDTO:
    return _loop_to_dto(_FakeLoop(updated_at=updated_at), now=now)


def test_a_loop_dto_ages_its_pursuing_status() -> None:
    now = datetime(2026, 10, 6, 12, tzinfo=UTC)
    assert _loop_dto(now - timedelta(minutes=5), now).motion == "executing"
    assert _loop_dto(now - timedelta(days=1), now).motion == "waiting_kew"


def test_a_naive_loop_timestamp_is_read_as_utc() -> None:
    """A row written before the store was timezone-aware still ages honestly.

    Read as local time in a zone behind UTC it would age NEGATIVELY, and a loop
    stopped a week ago would animate forever.
    """
    now = datetime(2026, 10, 6, 12, tzinfo=UTC)
    naive = datetime(2026, 10, 1, 12)
    assert _loop_dto(naive, now).motion == "waiting_kew"


def test_an_unreadable_loop_timestamp_leaves_the_status_alone() -> None:
    now = datetime(2026, 10, 6, 12, tzinfo=UTC)
    assert _loop_dto("not a date", now).motion == "executing"


def test_a_loop_with_no_stamp_at_all_leaves_the_status_alone() -> None:
    """Nothing to age against establishes no silence, so the halo stays."""
    now = datetime(2026, 10, 6, 12, tzinfo=UTC)
    loop = _FakeLoop(updated_at=None, created_at=None)
    assert _loop_to_dto(loop, now=now).motion == "executing"


def test_a_loop_with_no_updated_at_ages_off_its_creation() -> None:
    """The age is the quiet window `stale` reads, not `updated_at` alone.

    A `pursuing` row that has never been updated and never iterated is a loop
    nothing is driving; it must not animate while `stale` calls it stuck.
    """
    now = datetime(2026, 10, 6, 12, tzinfo=UTC)
    dto = _loop_dto(None, now)  # created_at is midnight, 12 hours back
    assert dto.motion == "waiting_kew"
    assert dto.stale is True


def test_a_loop_whose_iteration_is_the_only_fresh_write_keeps_its_halo() -> None:
    """`add_iteration` never bumps `updated_at` (#2305).

    Aged off `updated_at` alone, a loop iterating past LOOP_STALE_AFTER_SECS was
    served motion="waiting_kew" beside stale=False on the same DTO.
    """
    now = datetime(2026, 10, 6, 12, tzinfo=UTC)
    loop = _FakeLoop(updated_at=now - timedelta(hours=5))
    dto = _loop_to_dto(loop, now=now, last_iteration_at=now - timedelta(minutes=1))
    assert dto.motion == "executing"
    assert dto.stale is False
