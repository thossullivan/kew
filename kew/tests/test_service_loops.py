"""Loop reads + serialization (service layer, store injected as a fake)."""

from datetime import UTC, datetime, timedelta

import kew.service.loops as loops_mod
from kew.service.loops import _drive_mode_for, get_loop, list_loop_audit, list_loops


class FakeLoop:
    """Structural stand-in for run_store.LoopRecord (service never imports it)."""

    def __init__(self, **kw: object) -> None:
        self.loop_id = "L1"
        self.source = "issue"
        self.issue_num = 281
        self.repo = "thossullivan/kew"
        self.goal_text = "make CI green"
        self.runner_name = "claude-code"
        self.model = "claude-sonnet-4-6"
        self.status = "pursuing"
        self.status_reason = ""
        self.budget_usd: float | None = 5.0
        self.spent_usd = 1.25
        self.wall_clock_secs = 1800
        self.max_steps = 200
        self.step_count = 7
        self.created_at = datetime(2026, 6, 23, tzinfo=UTC)
        self.updated_at: datetime | None = None
        self.finished_at: datetime | None = None
        self.__dict__.update(kw)


class FakeStore:
    def __init__(self, loops: list[object]) -> None:
        self._loops = loops
        # loop_id -> newest recorded iteration stamp, as the store's grouped
        # read returns it. Empty = no loop has recorded an iteration.
        self.last_iterations: dict[str, str] = {}

    def list_loops(self, status: str | None = None) -> list[object]:
        if status is None:
            return list(self._loops)
        return [loop for loop in self._loops if loop.status == status]  # type: ignore[union-attr]

    def last_iteration_at_by_loop(self) -> dict[str, str]:
        return dict(self.last_iterations)


def test_drive_mode_delegated_for_native_runner():
    # claude-code declares loop_style "native" (Plan 1) -> delegated.
    assert _drive_mode_for("claude-code") == "delegated"


def test_drive_mode_unknown_for_unresolvable_runner():
    assert _drive_mode_for("not-a-runner") == "unknown"


def test_drive_mode_unknown_when_runner_lacks_loop_style(monkeypatch):
    # A registered runner whose instance has no loop_style attribute must not
    # 500 — the getattr(..., None) fallback returns "unknown".
    class _NoStyleRunner:
        pass  # deliberately no loop_style attribute

    monkeypatch.setattr(
        loops_mod, "_get_runner_factories", lambda: {"styleless": _NoStyleRunner}
    )
    assert _drive_mode_for("styleless") == "unknown"


def test_list_loops_serializes_record_to_dto():
    store = FakeStore([FakeLoop()])
    dtos = list_loops(store)
    assert len(dtos) == 1
    dto = dtos[0]
    assert dto.loop_id == "L1"
    assert dto.drive_mode == "delegated"
    assert dto.needs_human is False
    assert dto.budget_usd == 5.0
    assert dto.spent_usd == 1.25
    assert dto.created_at == "2026-06-23T00:00:00+00:00"
    assert dto.updated_at is None


def test_list_loops_surfaces_the_loops_durable_model():
    """Both halves of the route reach the API (#1111).

    runner_name alone cannot answer "what is this loop actually running on?" —
    a complexity-routed loop and the [agent] default can name the same runner
    and different models.
    """
    store = FakeStore([FakeLoop(model="openrouter/deepseek/deepseek-v4-flash")])
    assert list_loops(store)[0].model == "openrouter/deepseek/deepseek-v4-flash"


def test_list_loops_model_is_blank_for_a_pre_v35_loop():
    """A record predating the durable model column serializes, not 500s."""

    class _NoModelLoop(FakeLoop):
        def __init__(self) -> None:
            super().__init__()
            del self.model

    assert list_loops(FakeStore([_NoModelLoop()]))[0].model == ""


def test_list_loops_flags_needs_human():
    store = FakeStore([FakeLoop(status="needs_human")])
    dto = list_loops(store)[0]
    assert dto.status == "needs_human"
    assert dto.needs_human is True


def test_list_loops_passes_status_filter_to_store():
    store = FakeStore(
        [
            FakeLoop(loop_id="L1", status="pursuing"),
            FakeLoop(loop_id="L2", status="achieved"),
        ]
    )
    ids = [d.loop_id for d in list_loops(store, status="achieved")]
    assert ids == ["L2"]


# ---------------------------------------------------------------------------
# Task 2: detail + audit edge-case coverage
# ---------------------------------------------------------------------------


class FakeIteration:
    def __init__(self, iteration_id: str, idx: int, **kw: object) -> None:
        self.iteration_id = iteration_id
        self.loop_id = "L1"
        self.idx = idx
        self.run_id = "run-abc"
        self.kind = "assistant_turn"
        self.status = "pursuing"
        self.cost_usd = 0.42
        self.tokens = 900
        self.started_at = datetime(2026, 6, 23, 0, 1, tzinfo=UTC)
        self.finished_at = datetime(2026, 6, 23, 0, 2, tzinfo=UTC)
        self.__dict__.update(kw)


class FakeAudit:
    def __init__(self, id: int, event: str, payload: dict[str, object] | None) -> None:
        self.id = id
        self.timestamp = "2026-06-23T00:01:00+00:00"
        self.event = event
        self.actor_type = "policy"
        self.actor_id = None
        self.payload = payload


class DetailStore:
    def __init__(
        self,
        loop: object | None,
        iterations: list[object],
        audit: list[object] | None = None,
    ) -> None:
        self._loop = loop
        self._iterations = iterations
        self._audit = audit or []

    def list_loops(self, status: str | None = None) -> list[object]:
        return [self._loop] if self._loop else []

    def get_loop(self, loop_id: str) -> object | None:
        return self._loop

    def list_iterations(self, loop_id: str) -> list[object]:
        return list(self._iterations)

    def list_audit_events(
        self,
        limit: int | None = 100,
        *,
        event: str | None = None,
        event_prefix: str | None = None,
        since: object | None = None,
        loop_id: str | None = None,
    ) -> list[object]:
        events = list(self._audit)
        if event is not None:
            events = [ev for ev in events if getattr(ev, "event", None) == event]
        if event_prefix is not None:
            events = [
                ev
                for ev in events
                if str(getattr(ev, "event", "")).startswith(event_prefix)
            ]
        return events if limit is None else events[:limit]


def test_get_loop_returns_none_when_absent():
    assert get_loop(DetailStore(None, []), "nope") is None


def test_list_loop_audit_filters_on_payload_loop_id():
    audit = [
        FakeAudit(
            2, "verification_checked", {"loop_id": "L1", "summary": "gate failed"}
        ),
        FakeAudit(1, "loop_started", {"loop_id": "other"}),
        FakeAudit(
            0, "loop_started", None
        ),  # malformed payload must be skipped, not crash
    ]
    dtos = list_loop_audit(DetailStore(FakeLoop(), [], audit), "L1")
    assert [d.event for d in dtos] == ["verification_checked"]
    assert dtos[0].loop_id == "L1"
    assert dtos[0].summary == "gate failed"


def test_get_loop_includes_iterations_in_store_order():
    its = [FakeIteration("I0", 0), FakeIteration("I1", 1)]
    detail = get_loop(DetailStore(FakeLoop(), its), "L1")
    assert detail is not None
    assert [i.iteration_id for i in detail.iterations] == ["I0", "I1"]
    assert detail.goal_text == "make CI green"
    assert detail.drive_mode == "delegated"


def test_get_loop_carries_status_reason_evidence():
    loop = FakeLoop(status="needs_human", status_reason="gate_failed: 2 tests red")
    detail = get_loop(DetailStore(loop, []), "L1")
    assert detail is not None
    assert detail.needs_human is True
    assert detail.status_reason == "gate_failed: 2 tests red"
    assert detail.iterations == []


# --- #2297: loops have no heartbeat, so staleness is derived on read --------


def test_a_pursuing_loop_that_has_not_iterated_reads_stale() -> None:
    # The real shape this came from: loops sitting at `pursuing` for months.
    loop = FakeLoop(updated_at=datetime(2026, 7, 15, tzinfo=UTC))
    [dto] = list_loops(FakeStore([loop]), now=datetime(2026, 10, 6, tzinfo=UTC))
    assert dto.stale is True
    assert dto.status == "pursuing"  # the row is NOT rewritten, only read


def test_a_loop_inside_two_cadences_is_not_stale() -> None:
    now = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
    loop = FakeLoop(updated_at=datetime(2026, 10, 6, 11, 45, tzinfo=UTC))
    [dto] = list_loops(FakeStore([loop]), now=now)
    assert dto.stale is False


def test_a_terminal_loop_is_never_stale() -> None:
    loop = FakeLoop(status="achieved", updated_at=datetime(2026, 7, 15, tzinfo=UTC))
    [dto] = list_loops(FakeStore([loop]), now=datetime(2026, 10, 6, tzinfo=UTC))
    assert dto.stale is False


def test_an_unreadable_timestamp_is_not_evidence_of_staleness() -> None:
    loop = FakeLoop(updated_at="not a timestamp")
    [dto] = list_loops(FakeStore([loop]), now=datetime(2026, 10, 6, tzinfo=UTC))
    assert dto.stale is False


def test_a_loop_inside_its_wall_clock_budget_is_not_stale() -> None:
    # The regression: `updated_at` is written at session start, resume and
    # terminal — never by add_iteration — so a loop working normally through its
    # default 2-hour budget read stale after 61 minutes.
    now = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
    loop = FakeLoop(
        wall_clock_secs=7200, updated_at=datetime(2026, 10, 6, 10, 30, tzinfo=UTC)
    )
    [dto] = list_loops(FakeStore([loop]), now=now)
    assert dto.stale is False


def test_a_loop_quiet_past_its_wall_clock_budget_is_stale() -> None:
    # Past the budget the run is killed and the loop finalized, so continued
    # silence at `pursuing` means nothing is driving it.
    now = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
    loop = FakeLoop(
        wall_clock_secs=7200, updated_at=datetime(2026, 10, 6, 9, 0, tzinfo=UTC)
    )
    [dto] = list_loops(FakeStore([loop]), now=now)
    assert dto.stale is True


def test_a_recorded_iteration_is_the_freshest_evidence_of_movement() -> None:
    # add_iteration writes no loops.updated_at, so the iteration stamp is the
    # only thing proving a long-running loop is still moving.
    now = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
    loop = FakeLoop(updated_at=datetime(2026, 10, 6, 6, 0, tzinfo=UTC))
    store = FakeStore([loop])
    assert list_loops(store, now=now)[0].stale is True

    store.last_iterations = {"L1": "2026-10-06T11:55:00+00:00"}
    assert list_loops(store, now=now)[0].stale is False


def test_get_loop_reads_staleness_from_the_iterations_it_already_fetched() -> None:
    now = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
    loop = FakeLoop(updated_at=datetime(2026, 10, 6, 6, 0, tzinfo=UTC))
    its = [
        FakeIteration(
            "I0",
            0,
            started_at=datetime(2026, 10, 6, 6, 5, tzinfo=UTC),
            finished_at=datetime(2026, 10, 6, 6, 6, tzinfo=UTC),
        ),
        FakeIteration(
            "I1",
            1,
            started_at=datetime(2026, 10, 6, 11, 50, tzinfo=UTC),
            finished_at=datetime(2026, 10, 6, 11, 51, tzinfo=UTC),
        ),
    ]
    detail = get_loop(DetailStore(loop, its), "L1", now=now)
    assert detail is not None
    assert detail.stale is False
    assert len(detail.iterations) == 2


def test_a_store_without_the_iteration_read_still_serves_the_page() -> None:
    # The read degrades to the updated_at stamps rather than raising: a store
    # double (or an older reader) that has no grouped iteration query must not
    # break the loops page.
    now = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
    loop = FakeLoop(updated_at=datetime(2026, 10, 6, 11, 45, tzinfo=UTC))
    [dto] = list_loops(DetailStore(loop, []), now=now)
    assert dto.stale is False


# --- #2305: motion and stale are two statements about the same stamp --------


def test_a_loop_whose_only_recent_write_is_an_iteration_still_animates() -> None:
    """A DTO must not say "waiting on kew" and "not stale" about one loop.

    `motion` was aged off `updated_at` alone, which `add_iteration` never
    touches, so a loop iterating past LOOP_STALE_AFTER_SECS was served
    motion="waiting_kew" while the same DTO's stale=False said it was moving.
    """
    now = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
    # Session start was 5 hours ago — past the 3h motion threshold — but the
    # loop recorded an iteration a minute ago, so it IS executing.
    loop = FakeLoop(
        wall_clock_secs=7200, updated_at=datetime(2026, 10, 6, 7, 0, tzinfo=UTC)
    )
    store = FakeStore([loop])
    store.last_iterations = {"L1": "2026-10-06T11:59:00+00:00"}
    [dto] = list_loops(store, now=now)
    assert dto.stale is False
    assert dto.motion == "executing"


def test_a_loop_nothing_has_advanced_stops_moving_on_screen() -> None:
    # The other side of the same reading: no iteration, updated_at hours old.
    now = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
    loop = FakeLoop(updated_at=datetime(2026, 10, 6, 4, 0, tzinfo=UTC))
    [dto] = list_loops(FakeStore([loop]), now=now)
    assert dto.stale is True
    assert dto.motion == "waiting_kew"


def test_get_loop_motion_reads_the_iterations_it_already_fetched() -> None:
    # The detail route derives the quiet window from the iterations it read,
    # so the card and the list cannot disagree about one loop's motion.
    now = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
    loop = FakeLoop(
        wall_clock_secs=7200, updated_at=datetime(2026, 10, 6, 7, 0, tzinfo=UTC)
    )
    its = [
        FakeIteration(
            "I0",
            0,
            started_at=datetime(2026, 10, 6, 11, 50, tzinfo=UTC),
            finished_at=datetime(2026, 10, 6, 11, 59, tzinfo=UTC),
        )
    ]
    detail = get_loop(DetailStore(loop, its), "L1", now=now)
    assert detail is not None
    assert detail.stale is False
    assert detail.motion == "executing"


def test_an_unreadable_stamp_leaves_a_pursuing_loop_moving() -> None:
    # Fail-safe both ways: an unreadable stamp proves no silence, so it may
    # neither mark the loop stale nor stop its halo.
    loop = FakeLoop(updated_at="not a timestamp")
    [dto] = list_loops(FakeStore([loop]), now=datetime(2026, 10, 6, tzinfo=UTC))
    assert dto.stale is False
    assert dto.motion == "executing"


def test_motion_and_stale_never_contradict_each_other() -> None:
    """One DTO cannot say a loop is both moving and stopped.

    The two words were read against two windows — `stale` against the loop's
    own budget, `motion` against a global 3 hours — so a loop with a 4-hour
    budget, quiet for three and a half, was served stale=False beside
    motion="waiting_kew".
    """
    now = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
    loop = FakeLoop(
        wall_clock_secs=4 * 3600, updated_at=datetime(2026, 10, 6, 8, 30, tzinfo=UTC)
    )
    [dto] = list_loops(FakeStore([loop]), now=now)
    assert dto.stale is False
    assert dto.motion == "executing"


def test_a_pursuing_loop_reads_moving_exactly_while_it_reads_fresh() -> None:
    # The invariant, swept across the whole quiet range of one loop: for a
    # `pursuing` row, motion == "executing" is the negation of stale.
    now = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
    for quiet_h in (0, 1, 2, 3, 4, 5, 9):
        loop = FakeLoop(
            wall_clock_secs=4 * 3600,
            updated_at=now - timedelta(hours=quiet_h),
        )
        [dto] = list_loops(FakeStore([loop]), now=now)
        assert (dto.motion == "executing") is not dto.stale, quiet_h
