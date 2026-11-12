"""Tests for kew.service.work — the canonical customer work projection (I02).

Every assertion here is about ONE question: does this view tell the truth about
one work item, at one instant, to every surface at once? The three failure
modes it guards are the ones the product-coherence charter names:

* a torn view — half of one world and half of the next, because two reads
  straddled a concurrent reconciliation;
* a fabricated empty — an unavailable remote read rendered as "nothing there";
* an invented action — each surface deriving its own idea of what to do next,
  most dangerously "redispatch" over work that is already preserved.

Seeds a real ``SqliteRunStore`` (no driver import) and passes remote facts in
explicitly, because remote facts are the caller's observation to make — the
composer is pure and never reaches out.
"""

from __future__ import annotations

import shutil
import subprocess
from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from kew import subprocess_utils
from kew.audit_chain import Anchor
from kew.certificate_ref import (
    KIND_CERTIFICATE,
    KIND_READY,
    CertificateEnvelope,
    append_certificate,
    resolve_certificate,
)
from kew.config import DispatchConfig
from kew.human_review import REVIEW_PENDING
from kew.run_store import (
    DEFAULT_NODE_PR_FACTS_LIMIT,
    AdmissionIdentity,
    RunRecord,
    SqliteRunStore,
)
from kew.service.attention import work_projection
from kew.service.models import SafestActionDTO, WorkExceptionDTO
from kew.service.work import (
    JOURNALED_MERGE_STATE_MAX_AGE_S,
    CertificateFact,
    NoChangeFact,
    PublicationFact,
    PullRequestFact,
    ReadinessFact,
    RemoteObservation,
    ReviewPressureFact,
    TrackerItemFact,
    WorkFacts,
    cached_pull_request,
    collect_work_facts,
    compose_work_projection,
    compose_work_view,
    get_work_view,
    offered_actions,
)
from kew.waits import HOST_RESTART_REASON

FIXED_NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
#: A merge state read a minute before ``FIXED_NOW`` — the ordinary case, one
#: driver tick back.
JUST_READ = (FIXED_NOW - timedelta(minutes=1)).isoformat()
OBSERVED_AT = datetime(2026, 8, 18, 11, 59, tzinfo=UTC)


@pytest.fixture
def store(tmp_path: Path) -> Any:
    s = SqliteRunStore(str(tmp_path / "dispatch.db"))
    yield s
    s.close()


def _admit(
    store: Any,
    *,
    run_id: str = "r1",
    item_id: str = "42",
    tracker: str = "github",
    status: str = "success",
    issue_num: int = 42,
    attempt_number: int = 1,
    **overrides: Any,
) -> None:
    """Admit one run through the single identity-bearing admission path."""
    fields: dict[str, Any] = {
        "run_id": run_id,
        "issue_num": issue_num,
        "item_id": item_id,
        "branch": f"agent/issue-{item_id.lower()}",
        "status": status,
        "exit_code": 0,
        "started_at": datetime(2026, 8, 18, 10, 0, tzinfo=UTC),
        "finished_at": datetime(2026, 8, 18, 10, 30, tzinfo=UTC),
        "runner_name": "claude-code",
        "model": "claude-opus-5",
        "attempt_number": attempt_number,
    }
    fields.update(overrides)
    store.admit_run_with_identity(
        RunRecord(**fields),
        AdmissionIdentity(
            run_id=run_id,
            attempt_number=attempt_number,
            source_tracker=tracker,
            item_id=item_id,
            delivery_repo="acme/widgets",
        ),
    )


def _pr(state: str, *, url: str = "https://example.invalid/pr/7") -> PullRequestFact:
    return PullRequestFact(
        observation=RemoteObservation(
            dimension="pull_request",
            availability="available",
            provenance="observed",
            observed_at=OBSERVED_AT,
        ),
        url=url,
        state=state,
        merged_at=OBSERVED_AT if state == "merged" else None,
    )


def _item(state: str) -> TrackerItemFact:
    return TrackerItemFact(
        observation=RemoteObservation(
            dimension="tracker_item",
            availability="available",
            provenance="observed",
            observed_at=OBSERVED_AT,
        ),
        state=state,
    )


def _view(store: Any, item_id: str = "42", **kwargs: Any) -> Any:
    kwargs.setdefault("composed_at", FIXED_NOW)
    return get_work_view(store, item_id, **kwargs)


# --- identity: opaque ids survive, numeric shims never appear ---------------


def test_work_view_identity_keeps_the_opaque_linear_id(store: Any) -> None:
    """SFD-12 never degrades to #0 — the identity is read from admission, not
    from the ``issue_num`` sentinel the row had to carry."""
    _admit(store, run_id="r-sfd", item_id="SFD-12", tracker="linear", issue_num=0)

    view = _view(store, "SFD-12")

    assert view.identity.item_id == "SFD-12"
    assert view.identity.item_ref == "SFD-12"  # bare, not "#0" and not "#SFD-12"
    assert view.identity.source_tracker == "linear"
    assert view.identity.delivery_repo == "acme/widgets"
    assert view.identity.run_id == "r-sfd"


def test_work_view_identity_renders_github_numeric_ids_as_hash_refs(
    store: Any,
) -> None:
    _admit(store, item_id="42", issue_num=42)

    view = _view(store)

    assert view.identity.item_ref == "#42"
    assert view.identity.source_tracker == "github"


def test_work_view_identity_joins_the_epic_node_without_parsing_a_branch(
    store: Any,
) -> None:
    store.reserve_epic_node_item("epic-1", "SFD-12", "r-epic")
    identity = store.build_admission_identity(
        "r-epic", tracker="linear", item_id="SFD-12", repo="acme/widgets"
    )
    store.admit_run_with_identity(
        RunRecord(
            run_id="r-epic",
            issue_num=0,
            item_id="SFD-12",
            branch="agent/issue-sfd-12",
            status="pending",
            exit_code=-1,
            started_at=datetime(2026, 8, 18, 10, 0, tzinfo=UTC),
        ),
        identity,
    )

    view = _view(store, "SFD-12")

    assert view.identity.epic_ref == "epic-1"
    assert view.identity.node_id == "SFD-12"


def test_work_view_legacy_run_without_an_admission_identity_is_honestly_partial(
    store: Any,
) -> None:
    """A pre-I01 row has no identity table entry. The view must say "unknown
    tracker", not invent one from the inference provider or the issue number."""
    store.save(
        RunRecord(
            run_id="r-legacy",
            issue_num=7,
            item_id="7",
            branch="agent/issue-7",
            status="success",
            exit_code=0,
            started_at=datetime(2026, 8, 18, 10, 0, tzinfo=UTC),
            provider="anthropic",
        )
    )

    view = _view(store, "7")

    assert view.identity.item_id == "7"
    assert view.identity.source_tracker is None
    assert view.identity.delivery_repo is None
    assert view.source.tracker is None


# --- execution: RunStatus stays execution state ----------------------------


@pytest.mark.parametrize(
    ("status", "expected_state"),
    [
        ("pending", "in_flight"),
        ("awaiting_publication", "completed"),
        ("success", "completed"),
        ("failure", "terminal"),
        ("refused", "terminal"),
        ("budget_exceeded", "terminal"),
        ("budget_stopped", "terminal"),
        ("budget_stop", "terminal"),
        ("killed", "terminal"),
    ],
)
def test_work_view_every_current_run_status_projects_to_an_execution_state(
    store: Any, status: str, expected_state: str
) -> None:
    _admit(store, status=status)

    view = _view(store)

    assert view.execution.state == expected_state
    assert view.execution.run_status == status  # raw status kept, never lost


def test_work_view_no_attempt_yet_is_not_started_and_recommends_dispatch(
    store: Any,
) -> None:
    view = _view(store, "999", tracker_item=_item("open"))

    assert view.execution.state == "not_started"
    assert view.execution.run_status is None
    assert view.execution.attempts == 0
    assert view.safest_action.action == "dispatch"


def test_work_view_in_flight_run_recommends_waiting_not_a_second_dispatch(
    store: Any,
) -> None:
    _admit(store, status="pending", finished_at=None)

    view = _view(store)

    assert view.safest_action.action == "wait"
    assert view.outcome.state == "in_progress"


def test_work_view_a_future_dated_reaped_run_is_not_the_current_attempt(
    store: Any,
) -> None:
    """#2969: the run a host-restart marker binds is superseded whatever the
    clocks say. Its clock reading ahead of the replacement must not make it the
    current attempt and offer a dispatch while the replacement is in flight."""
    assert store.reserve_epic_node_item("epic/dag", "42", "reservation-1")
    [reservation] = store.list_epic_reservations("epic/dag")
    _admit(
        store,
        run_id="r-reaped",
        status="pending",
        started_at=datetime(2026, 8, 18, 11, 30, tzinfo=UTC),
        finished_at=None,
    )
    assert store.finalize_orphaned_pending(
        "r-reaped",
        error_category="host_restart_reaped",
        finished_at=datetime(2026, 8, 18, 10, 0, tzinfo=UTC),
        reservation=reservation,
        retry_reason=HOST_RESTART_REASON,
        retry_repo="acme/widgets",
    )
    _admit(
        store,
        run_id="r-replacement",
        status="pending",
        attempt_number=2,
        started_at=datetime(2026, 8, 18, 10, 5, tzinfo=UTC),
        finished_at=None,
    )

    view = _view(store)

    assert view.identity.run_id == "r-replacement"
    assert view.execution.attempts == 2
    assert view.safest_action.action == "wait"
    assert "dispatch" not in {a.action for a in view.actions}


def test_work_view_a_pending_replacement_outranks_every_cleared_attempt(
    store: Any,
) -> None:
    """#2969: after an operator retry and a host-restart reap, the cleared
    failure is superseded by name too. A replacement whose clock was set back
    before that failure is still the current attempt, so Work offers no
    second dispatch while it runs."""
    _admit(
        store,
        run_id="r-cleared",
        status="failure",
        started_at=datetime(2026, 8, 18, 11, 0, tzinfo=UTC),
        finished_at=datetime(2026, 8, 18, 11, 10, tzinfo=UTC),
    )
    store.mark_epic_retry_item("epic/dag", "42")
    assert store.reserve_epic_node_item("epic/dag", "42", "reservation-1")
    [reservation] = store.list_epic_reservations("epic/dag")
    _admit(
        store,
        run_id="r-reaped",
        status="pending",
        attempt_number=2,
        started_at=datetime(2026, 8, 18, 11, 30, tzinfo=UTC),
        finished_at=None,
    )
    assert store.finalize_orphaned_pending(
        "r-reaped",
        error_category="host_restart_reaped",
        finished_at=datetime(2026, 8, 18, 11, 40, tzinfo=UTC),
        reservation=reservation,
        retry_reason=HOST_RESTART_REASON,
        retry_repo="acme/widgets",
    )
    _admit(
        store,
        run_id="r-replacement",
        status="pending",
        attempt_number=3,
        started_at=datetime(2026, 8, 18, 10, 5, tzinfo=UTC),
        finished_at=None,
    )

    view = _view(store)

    assert view.identity.run_id == "r-replacement"
    assert view.execution.attempts == 3
    assert view.safest_action.action == "wait"
    assert "dispatch" not in {a.action for a in view.actions}


def test_work_view_latest_attempt_wins_and_attempts_are_counted(store: Any) -> None:
    _admit(store, run_id="r-a", status="failure", attempt_number=1)
    _admit(
        store,
        run_id="r-b",
        status="success",
        attempt_number=2,
        started_at=datetime(2026, 8, 18, 11, 0, tzinfo=UTC),
        finished_at=datetime(2026, 8, 18, 11, 30, tzinfo=UTC),
    )

    view = _view(store)

    assert view.identity.run_id == "r-b"
    assert view.identity.attempt_number == 2
    assert view.execution.attempts == 2
    assert view.execution.state == "completed"


# --- legacy awaiting_publication: preserved work, never a retry -------------


def test_work_view_awaiting_publication_decodes_as_completed_plus_pending_publication(
    store: Any,
) -> None:
    """The compatibility encoding, read honestly: execution finished and the
    branch is preserved; only publication is outstanding."""
    _admit(store, status="awaiting_publication")

    view = _view(store)

    assert view.execution.state == "completed"
    assert view.artifact.state == "preserved"
    assert view.artifact.publication == "pending"
    assert view.artifact.branch == "agent/issue-42"


def test_work_view_awaiting_publication_recommends_finalizing_the_same_artifact(
    store: Any,
) -> None:
    """The guardrail with teeth: redispatching would destroy preserved work."""
    _admit(store, status="awaiting_publication")

    action = _view(store).safest_action

    assert action.action == "finalize_publication"
    assert action.action != "dispatch"
    assert "agent/issue-42" in action.reason


def test_work_view_awaiting_publication_still_finalizes_when_the_remote_is_unavailable(
    store: Any,
) -> None:
    """Preserved work outranks a missing remote read: the artifact is local
    fact, so "finalize it" needs no network to be the safest thing to do."""
    _admit(store, status="awaiting_publication")

    view = _view(
        store,
        pull_request=PullRequestFact(
            observation=RemoteObservation.unavailable(
                "pull_request", detail="github api unreachable"
            )
        ),
    )

    assert view.safest_action.action == "finalize_publication"


# --- artifact, judgment, outcome -------------------------------------------


def test_work_view_published_artifact_awaiting_review_is_a_human_decision(
    store: Any,
) -> None:
    _admit(store, pr_url="https://example.invalid/pr/7")

    view = _view(store, pull_request=_pr("open"), tracker_item=_item("open"))

    assert view.artifact.state == "published"
    assert view.artifact.publication == "published"
    assert view.judgment.state == "awaiting_human_decision"
    assert view.outcome.state == "in_progress"
    assert view.safest_action.action == "await_human_decision"


def test_work_view_merged_pr_with_a_closed_item_is_delivered_and_needs_nothing(
    store: Any,
) -> None:
    _admit(store, pr_url="https://example.invalid/pr/7")

    view = _view(store, pull_request=_pr("merged"), tracker_item=_item("closed"))

    assert view.judgment.state == "accepted"
    assert view.outcome.state == "delivered"
    assert view.source.synchronization == "synchronized"
    assert view.safest_action.action == "none"


def test_work_view_merged_pr_with_an_open_item_is_tracker_sync_pending(
    store: Any,
) -> None:
    _admit(store, pr_url="https://example.invalid/pr/7")

    view = _view(store, pull_request=_pr("merged"), tracker_item=_item("open"))

    assert view.outcome.state == "delivered"
    assert view.source.synchronization == "pending"
    assert view.safest_action.action == "synchronize_tracker"


def test_work_view_closed_unmerged_pr_is_declined_and_never_auto_redispatched(
    store: Any,
) -> None:
    _admit(store, pr_url="https://example.invalid/pr/7")

    view = _view(store, pull_request=_pr("closed"), tracker_item=_item("open"))

    assert view.judgment.state == "declined"
    assert view.outcome.state == "abandoned"
    assert view.safest_action.action == "inspect"


def test_work_view_completed_run_with_no_artifact_is_absent_not_published(
    store: Any,
) -> None:
    _admit(store)

    view = _view(store)

    assert view.artifact.state == "absent"
    assert view.artifact.pr_url is None
    assert view.judgment.state == "not_applicable"
    assert view.safest_action.action == "inspect"


def test_work_view_terminal_failure_with_no_artifact_recommends_dispatch(
    store: Any,
) -> None:
    _admit(store, status="failure", error_category="timeout")

    view = _view(store, tracker_item=_item("open"))

    assert view.execution.state == "terminal"
    assert view.execution.error_category == "timeout"
    assert view.outcome.state == "failed"
    assert view.safest_action.action == "dispatch"


@pytest.mark.parametrize("execution", ["not_started", "retryable_failure"])
@pytest.mark.parametrize(
    ("source", "expected_action"),
    [
        ("open", "dispatch"),
        ("closed", "none"),
        ("unavailable", "observe_remote"),
    ],
)
def test_work_view_dispatch_requires_an_open_available_source(
    store: Any, execution: str, source: str, expected_action: str
) -> None:
    if execution == "retryable_failure":
        _admit(store, status="failure")
    tracker_item = (
        TrackerItemFact(observation=RemoteObservation.unavailable("tracker_item"))
        if source == "unavailable"
        else _item(source)
    )

    action = _view(store, tracker_item=tracker_item).safest_action

    assert action.action == expected_action
    assert (action.action == "dispatch") is (source == "open")
    if source == "unavailable":
        assert action.blocked_by == "tracker_item"


def test_work_view_refusal_is_terminal_but_never_auto_redispatched(store: Any) -> None:
    _admit(store, status="refused")

    assert _view(store).safest_action.action == "inspect"


@pytest.mark.parametrize("status", ["budget_exceeded", "budget_stopped", "budget_stop"])
def test_work_view_budget_terminals_ask_for_inspection_not_another_spend(
    store: Any, status: str
) -> None:
    _admit(store, status=status)

    assert _view(store).safest_action.action == "inspect"


def test_work_view_orphan_pr_blocks_dispatch_over_an_artifact_that_already_exists(
    store: Any,
) -> None:
    """An open agent PR with no successful run behind it. The artifact is real,
    so dispatching on top of it would duplicate or clobber work."""
    _admit(store, status="failure", branch="agent/issue-42")
    store.sync_orphan_prs(
        [(7, "https://example.invalid/pr/7", "agent/issue-42", "failed-run")]
    )

    view = _view(store, pull_request=_pr("open"))

    assert view.artifact.orphan is True
    assert view.artifact.state == "published"
    assert view.safest_action.action == "inspect"
    assert view.safest_action.action != "dispatch"


# --- remote availability: unknown is never a successful empty ---------------


def test_work_view_unread_remote_is_unavailable_not_a_successful_empty(
    store: Any,
) -> None:
    """No remote fact was passed at all. The dimensions resting on it stay
    unknown; nothing is reported as "found nothing"."""
    _admit(store, pr_url="https://example.invalid/pr/7")

    view = _view(store)

    assert view.judgment.state == "unknown"
    assert view.judgment.observation.availability == "unavailable"
    assert view.judgment.observation.provenance == "unobserved"
    assert view.source.item_state == "unknown"
    assert view.outcome.state == "unknown"


def test_work_view_unavailable_remote_makes_observing_the_safest_action(
    store: Any,
) -> None:
    _admit(store, pr_url="https://example.invalid/pr/7")

    view = _view(
        store,
        pull_request=PullRequestFact(
            observation=RemoteObservation.unavailable(
                "pull_request", detail="github api 503"
            )
        ),
    )

    assert view.safest_action.action == "observe_remote"
    assert view.safest_action.blocked_by == "pull_request"
    assert "503" in (view.judgment.observation.detail or "")


def test_work_view_unavailable_tracker_read_blocks_a_synchronization_claim(
    store: Any,
) -> None:
    """The PR merged, but we could not read the item. "Synchronized" would be a
    guess and "pending" would be a different guess — so: go look."""
    _admit(store, pr_url="https://example.invalid/pr/7")

    view = _view(
        store,
        pull_request=_pr("merged"),
        tracker_item=TrackerItemFact(
            observation=RemoteObservation.unavailable("tracker_item")
        ),
    )

    assert view.source.synchronization == "unknown"
    assert view.safest_action.action == "observe_remote"
    assert view.safest_action.blocked_by == "tracker_item"


def test_work_view_stale_remote_reading_is_usable_and_says_it_is_stale(
    store: Any,
) -> None:
    _admit(store, pr_url="https://example.invalid/pr/7")
    stale = PullRequestFact(
        observation=RemoteObservation(
            dimension="pull_request",
            availability="stale",
            provenance="cached",
            observed_at=datetime(2026, 8, 17, 0, 0, tzinfo=UTC),
        ),
        url="https://example.invalid/pr/7",
        state="open",
    )

    view = _view(store, pull_request=stale, tracker_item=_item("open"))

    assert view.judgment.state == "awaiting_human_decision"
    assert view.judgment.observation.availability == "stale"
    assert view.judgment.observation.observed_at == "2026-08-17T00:00:00+00:00"
    assert view.safest_action.action == "await_human_decision"


def test_work_view_every_remote_observation_is_listed_with_provenance(
    store: Any,
) -> None:
    _admit(store)

    view = _view(store, pull_request=_pr("open"), tracker_item=_item("open"))

    by_dimension = {o.dimension: o for o in view.observations}
    # Every dimension that rests on a read someone had to make is listed —
    # including the ones nobody made, which is the point of listing them.
    assert set(by_dimension) == {
        "pull_request",
        "tracker_item",
        "route_boundary",
        "review_capacity",
    }
    for name in ("pull_request", "tracker_item"):
        assert by_dimension[name].availability == "available"
        assert by_dimension[name].provenance == "observed"
        assert by_dimension[name].observed_at == "2026-08-18T11:59:00+00:00"
    for name in ("route_boundary", "review_capacity"):
        assert by_dimension[name].availability == "unavailable"
        assert by_dimension[name].provenance == "unobserved"


# --- revision and snapshot binding -----------------------------------------


def test_work_view_view_revision_is_stable_across_recomposition_of_the_same_facts(
    store: Any,
) -> None:
    """Two surfaces at the same facts hold the same revision — that is what
    makes a cross-surface comparison meaningful instead of an undefined
    instant."""
    _admit(store, pr_url="https://example.invalid/pr/7")

    first = _view(store, pull_request=_pr("open"), tracker_item=_item("open"))
    second = get_work_view(
        store,
        "42",
        pull_request=_pr("open"),
        tracker_item=_item("open"),
        composed_at=datetime(2026, 8, 18, 23, 59, tzinfo=UTC),
    )

    assert first.view_revision == second.view_revision
    assert first.composed_at != second.composed_at  # time moves; the facts did not


def test_work_view_view_revision_changes_when_a_remote_observation_changes(
    store: Any,
) -> None:
    _admit(store, pr_url="https://example.invalid/pr/7")

    open_pr = _view(store, pull_request=_pr("open"), tracker_item=_item("open"))
    merged = _view(store, pull_request=_pr("merged"), tracker_item=_item("open"))

    assert open_pr.view_revision != merged.view_revision


def test_work_view_view_revision_changes_when_execution_advances(store: Any) -> None:
    _admit(store, status="pending", finished_at=None)
    before = _view(store).view_revision

    _admit(store, status="success")

    assert _view(store).view_revision != before


def test_work_view_composition_is_bound_to_one_untorn_snapshot(
    store: Any, tmp_path: Path
) -> None:
    """A concurrent writer commits while the facts are being collected. The
    collected facts must be entirely from one image of the database — never a
    run row from before the write and an identity row from after it."""
    _admit(store, run_id="r-1", status="pending", finished_at=None)
    writer = SqliteRunStore(str(tmp_path / "dispatch.db"))
    try:
        with store.read_snapshot():
            facts = collect_work_facts(store, "42")
            _admit(writer, run_id="r-2", status="success", attempt_number=2)
            again = collect_work_facts(store, "42")
    finally:
        writer.close()

    assert len(facts.runs) == len(again.runs) == 1
    assert compose_work_view(facts, composed_at=FIXED_NOW) == compose_work_view(
        again, composed_at=FIXED_NOW
    )
    # ...and after the snapshot closes, the new attempt is visible.
    assert len(collect_work_facts(store, "42").runs) == 2


def test_compose_work_view_is_pure(store: Any) -> None:
    """Same facts in, byte-identical view out — no clock, no store, no network
    inside the composer."""
    _admit(store, pr_url="https://example.invalid/pr/7")
    facts = collect_work_facts(
        store, "42", pull_request=_pr("open"), tracker_item=_item("open")
    )

    assert compose_work_view(facts) == compose_work_view(facts)


def test_work_view_composed_at_is_recorded_in_utc_iso(store: Any) -> None:
    _admit(store)

    assert _view(store).composed_at == "2026-08-18T12:00:00+00:00"


# --- route dimension --------------------------------------------------------


def test_work_view_route_dimension_reports_the_lane_as_declared_not_enforced(
    store: Any,
) -> None:
    _admit(store, runner_name="claude-code", model="claude-opus-5")

    view = _view(store)

    assert view.route.runner == "claude-code"
    assert view.route.model == "claude-opus-5"
    assert view.route.containment == "host"
    assert view.route.provenance == "declared"


def test_work_view_contained_run_reports_containment(store: Any) -> None:
    _admit(store, container_name="kew-42")

    assert _view(store).route.containment == "contained"


# --- Task 6: the three properties this projection exists to guarantee -------


def test_work_view_concurrent_publication_reconciliation_cannot_tear_the_view(
    store: Any, tmp_path: Path
) -> None:
    """The real race: a reconciliation finalizes an ``awaiting_publication``
    run from another connection *while* the view is being composed.

    The view must be entirely pre-finalize or entirely post-finalize. A torn
    one — say, a preserved artifact next to an already-published outcome —
    would recommend finalizing work that is already published.
    """
    _admit(store, run_id="r-pub", status="awaiting_publication")
    reconciler = SqliteRunStore(str(tmp_path / "dispatch.db"))
    try:
        with store.read_snapshot():
            before = compose_work_view(
                collect_work_facts(store, "42"), composed_at=FIXED_NOW
            )
            reconciler.finalize_awaiting_publication(
                "r-pub", pr_url="https://example.invalid/pr/7"
            )
            after = compose_work_view(
                collect_work_facts(store, "42"), composed_at=FIXED_NOW
            )
    finally:
        reconciler.close()

    assert before == after
    assert before.view_revision == after.view_revision
    assert before.artifact.publication == "pending"

    # Outside the snapshot the finalized world is visible — and it is a
    # different revision, which is exactly how a stale surface finds out.
    now = _view(store)
    assert now.artifact.publication == "published"
    assert now.view_revision != before.view_revision


@pytest.mark.parametrize(
    ("pr_state", "item_state"),
    [("open", "open"), ("merged", "open"), ("merged", "closed"), ("closed", "open")],
)
def test_work_view_unavailable_remote_never_reads_as_a_successful_empty(
    store: Any, pr_state: str, item_state: str
) -> None:
    """Whatever the remote *would* have said, an unavailable read yields
    ``unknown`` — never the absent/empty answer a successful read produces."""
    _admit(store, pr_url="https://example.invalid/pr/7")

    known = _view(store, pull_request=_pr(pr_state), tracker_item=_item(item_state))
    blind = _view(
        store,
        pull_request=PullRequestFact(
            observation=RemoteObservation.unavailable("pull_request")
        ),
        tracker_item=TrackerItemFact(
            observation=RemoteObservation.unavailable("tracker_item")
        ),
    )

    assert known.judgment.state != "unknown"  # the read succeeded: a real answer
    assert blind.judgment.state == "unknown"
    assert blind.source.item_state == "unknown"
    assert blind.source.synchronization == "unknown"
    assert blind.outcome.state == "unknown"
    assert blind.safest_action.action == "observe_remote"
    assert all(o.availability == "unavailable" for o in blind.observations)


@pytest.mark.parametrize(
    ("pull_request", "tracker_item"),
    [
        (None, None),
        (_pr("open"), _item("open")),
        (_pr("merged"), _item("closed")),
        (
            PullRequestFact(observation=RemoteObservation.unavailable("pull_request")),
            None,
        ),
    ],
)
def test_work_view_awaiting_publication_never_recommends_a_retry(
    store: Any, pull_request: Any, tracker_item: Any
) -> None:
    """Under every remote condition, preserved work is finalized, not re-run."""
    _admit(store, status="awaiting_publication")

    action = _view(
        store, pull_request=pull_request, tracker_item=tracker_item
    ).safest_action

    assert action.action == "finalize_publication"
    assert action.action not in {"dispatch", "inspect", "observe_remote"}


# ===========================================================================
# I19 — the composed exception, pressure and availability projection.
#
# Every test below asks the same question the six I02 dimensions ask, of the
# four dimensions later slices made durable: does ONE view tell the truth
# about integration health, boundary readiness, the zero-diff judgment, and
# the human review a delivery repository is holding — and does it collapse all
# of that into exactly ONE exception name and ONE safest action?
# ===========================================================================


def _observation(
    dimension: str, availability: str = "available", provenance: str = "observed"
) -> RemoteObservation:
    return RemoteObservation(
        dimension=dimension,
        availability=availability,
        provenance=provenance,
        observed_at=OBSERVED_AT,
    )


def _facts(store: Any, item_id: str = "42", **overrides: Any) -> Any:
    """One item's facts, read at ``FIXED_NOW``.

    The moment is pinned because the rules have AGES in them: a pending row is
    a run in flight only while it is young (``PENDING_RUN_MAX_AGE_S``), so facts
    read at the wall clock against a fixture started in August compose a view
    that changes meaning as the calendar moves.
    """
    facts = collect_work_facts(
        store,
        item_id,
        pull_request=overrides.pop("pull_request", None),
        tracker_item=overrides.pop("tracker_item", None),
        now=FIXED_NOW,
    )
    return replace(facts, **overrides) if overrides else facts


def _composed(store: Any, item_id: str = "42", **overrides: Any) -> Any:
    return compose_work_view(_facts(store, item_id, **overrides), composed_at=FIXED_NOW)


# --- integration: approval and landability are different facts -------------


def test_changes_requested_is_a_decision_not_an_absent_one(store: Any) -> None:
    """An open PR with changes requested is a human decision already made —
    reporting it as "awaiting decision" would hide the ball that is in our
    court."""
    _admit(store, pr_url="https://example.invalid/pr/7")

    view = _composed(
        store,
        pull_request=replace(_pr("open"), review_decision="changes_requested"),
        tracker_item=_item("open"),
    )

    assert view.judgment.state == "changes_requested"
    assert view.integration.review_decision == "changes_requested"
    assert view.exception.state == "changes_requested"
    assert view.exception.blocking is True
    assert view.safest_action.action == "address_review_feedback"


def test_failing_checks_outrank_an_approval(store: Any) -> None:
    """Approved and red is not landable. The exception names the build."""
    _admit(store, pr_url="https://example.invalid/pr/7")

    view = _composed(
        store,
        pull_request=replace(_pr("open"), review_decision="approved", checks="failing"),
        tracker_item=_item("open"),
    )

    assert view.integration.checks == "failing"
    assert view.exception.state == "checks_failing"
    assert view.safest_action.action == "fix_checks"


def test_conflicted_tree_outranks_failing_checks(store: Any) -> None:
    """A conflict must be resolved before a build result means anything."""
    _admit(store, pr_url="https://example.invalid/pr/7")

    view = _composed(
        store,
        pull_request=replace(_pr("open"), checks="failing", mergeability="conflicted"),
        tracker_item=_item("open"),
    )

    assert view.integration.mergeability == "conflicted"
    assert view.exception.state == "merge_conflict"
    assert view.safest_action.action == "resolve_conflict"


def test_a_cached_pr_carries_the_drivers_journaled_merge_state(store: Any) -> None:
    """#2303 — ``merge_conflict`` could never fire in serve before this.

    ``cached_pull_request`` is how every serve-composed view gets its artifact,
    and it left ``mergeability`` unset, so the dimension was ``unknown`` on every
    real request and the blocking conflict verdict (and its PCJ-3 conformance
    claim) was reachable only from hand-built facts. The driver reads
    ``mergeStateStatus`` every tick and now journals it, so the fact is in hand.
    """
    _admit(store, pr_url="https://example.invalid/pr/7", pr_state="OPEN")
    record = next(r for r in store.list_by_item("42") if r.pr_url)

    conflicted = cached_pull_request(
        record, merge_state="DIRTY", merge_state_at=JUST_READ, now=FIXED_NOW
    )
    view = compose_work_view(
        replace(
            _facts(store, "42", tracker_item=_item("open")),
            pull_request=conflicted,
        ),
        composed_at=FIXED_NOW,
    )

    assert conflicted.mergeability == "conflicted"
    assert view.integration.mergeability == "conflicted"
    assert view.exception.state == "merge_conflict"
    assert view.safest_action.action == "resolve_conflict"


@pytest.mark.parametrize(
    ("merge_state", "expected"),
    [
        ("CLEAN", "clean"),
        ("HAS_HOOKS", "clean"),
        ("UNSTABLE", "clean"),
        ("BEHIND", "clean"),
        ("DIRTY", "conflicted"),
        # Neither of these proves a tree: a rule or a draft stops the merge for
        # a reason that is not the tree, and UNKNOWN is GitHub saying it has not
        # computed one. Both must stay ``unknown`` rather than read clean.
        ("BLOCKED", None),
        ("DRAFT", None),
        ("UNKNOWN", None),
        ("", None),
        (None, None),
    ],
)
def test_the_merge_state_maps_only_to_what_it_proves(
    store: Any, merge_state: str | None, expected: str | None
) -> None:
    _admit(store, pr_url="https://example.invalid/pr/7", pr_state="OPEN")
    record = next(r for r in store.list_by_item("42") if r.pr_url)

    fact = cached_pull_request(
        record, merge_state=merge_state, merge_state_at=JUST_READ, now=FIXED_NOW
    )

    assert fact.mergeability == expected


# --------------------------------------------------------------------------- #
# The journaled merge state carries its AGE, or it is not acted on (#2303)
# --------------------------------------------------------------------------- #


def test_an_old_journaled_merge_state_raises_no_blocking_conflict(store: Any) -> None:
    """A dead driver's last reading must not hand a person a resolved conflict.

    ``merge_conflict`` is BLOCKING and suppresses every other verdict, and the
    journal keeps its last merge state indefinitely — a driver that stopped
    ticking (or an epic nobody drives) would otherwise go on reporting a
    conflict that may have been rebased away hours ago.
    """
    _admit(store, pr_url="https://example.invalid/pr/7", pr_state="OPEN")
    record = next(r for r in store.list_by_item("42") if r.pr_url)
    stale_read = FIXED_NOW - timedelta(seconds=JOURNALED_MERGE_STATE_MAX_AGE_S + 1)

    fact = cached_pull_request(
        record,
        merge_state="DIRTY",
        merge_state_at=stale_read.isoformat(),
        now=FIXED_NOW,
    )

    assert fact.mergeability is None
    view = compose_work_view(
        replace(_facts(store, "42", tracker_item=_item("open")), pull_request=fact),
        composed_at=FIXED_NOW,
    )
    assert view.integration.mergeability == "unknown"
    assert view.exception.state != "merge_conflict"


def test_a_merge_state_inside_the_bound_is_still_acted_on(store: Any) -> None:
    """The bound is ~60 missed ticks, not a twitch: an ordinary gap still reads."""
    _admit(store, pr_url="https://example.invalid/pr/7", pr_state="OPEN")
    record = next(r for r in store.list_by_item("42") if r.pr_url)
    read_at = FIXED_NOW - timedelta(seconds=JOURNALED_MERGE_STATE_MAX_AGE_S - 1)

    fact = cached_pull_request(
        record, merge_state="DIRTY", merge_state_at=read_at.isoformat(), now=FIXED_NOW
    )

    assert fact.mergeability == "conflicted"


@pytest.mark.parametrize("read_at", [None, "", "not-a-timestamp", "2026-08-18T12:00"])
def test_a_merge_state_nobody_can_age_is_not_acted_on(
    store: Any, read_at: str | None
) -> None:
    """An absent, unparseable or NAIVE instant establishes no age at all.

    Including the naive one: ``2026-08-18T12:00`` in an unknown zone could be
    half a day either side of now, and guessing a zone to raise a blocking
    verdict is worse than reporting the dimension unknown.
    """
    _admit(store, pr_url="https://example.invalid/pr/7", pr_state="OPEN")
    record = next(r for r in store.list_by_item("42") if r.pr_url)

    fact = cached_pull_request(
        record, merge_state="DIRTY", merge_state_at=read_at, now=FIXED_NOW
    )

    assert fact.mergeability is None


def test_the_observation_is_stamped_with_the_older_of_the_two_readings(
    store: Any,
) -> None:
    """A composite fact may not read as fresher than its stalest part.

    The run row's ``finished_at`` says when the PR's URL/state were reconciled;
    it says nothing about the tree. Stamping the composite with it would let a
    surface age a merge state read an hour ago as if it were read on the
    reconciliation — in either direction, which is why the OLDER wins and the
    detail names the merge read's own instant.
    """
    _admit(store, pr_url="https://example.invalid/pr/7", pr_state="OPEN")
    record = next(r for r in store.list_by_item("42") if r.pr_url)
    older = FIXED_NOW - timedelta(minutes=30)

    fact = cached_pull_request(
        record, merge_state="DIRTY", merge_state_at=older.isoformat(), now=FIXED_NOW
    )

    assert fact.observation.observed_at is not None
    assert fact.observation.observed_at <= older
    assert older.isoformat() in (fact.observation.detail or "")


def test_without_a_merge_state_the_stamp_is_the_reconciliation_as_before(
    store: Any,
) -> None:
    """The reading this function has always reported, unchanged."""
    _admit(store, pr_url="https://example.invalid/pr/7", pr_state="OPEN")
    record = next(r for r in store.list_by_item("42") if r.pr_url)

    fact = cached_pull_request(record)

    assert fact.observation.observed_at == record.finished_at
    assert fact.observation.detail == (
        "the last persisted reconciliation of this pull request"
    )


def test_unreadable_remote_leaves_integration_unknown_not_green(store: Any) -> None:
    """A forge nobody could read is not a green build and not a clean tree."""
    _admit(store, pr_url="https://example.invalid/pr/7")

    view = _composed(store, tracker_item=_item("open"))

    assert view.integration.checks == "unknown"
    assert view.integration.mergeability == "unknown"
    assert view.integration.review_decision == "unknown"
    assert view.integration.observation.availability == "unavailable"
    assert view.exception.state == "remote_unavailable"
    assert view.safest_action.action == "observe_remote"


def test_stale_remote_still_reads_but_is_marked_stale(store: Any) -> None:
    """A cached reading is an old answer, not an absent one — the view says so
    rather than pretending it never looked."""
    _admit(store, pr_url="https://example.invalid/pr/7")
    cached = PullRequestFact(
        observation=_observation("pull_request", "stale", "cached"),
        url="https://example.invalid/pr/7",
        state="open",
        checks="passing",
        mergeability="clean",
        review_decision="review_required",
    )

    view = _composed(store, pull_request=cached, tracker_item=_item("open"))

    assert view.integration.checks == "passing"
    assert view.integration.observation.availability == "stale"
    assert view.exception.state == "awaiting_human_decision"
    assert view.safest_action.action == "await_human_decision"


# --- readiness: evidence, never optimism -----------------------------------


@pytest.mark.parametrize(
    ("state", "expected_exception"),
    [
        ("proven", "none"),
        ("refuted", "readiness_refuted"),
        ("expired", "none"),
        ("unproven", "none"),
        ("unknown", "none"),
    ],
)
def test_readiness_only_refuted_is_an_exception(
    store: Any, state: str, expected_exception: str
) -> None:
    """A boundary nobody probed is not broken; a probed-and-broken one is."""
    _admit(store, status="success")

    view = _composed(
        store,
        readiness=ReadinessFact(
            observation=_observation("route_boundary"), state=state
        ),
        tracker_item=_item("open"),
    )

    assert view.readiness.state == state
    if expected_exception != "none":
        assert view.exception.state == expected_exception


def test_readiness_participates_in_view_revision(store: Any) -> None:
    """Two views that differ only in boundary evidence must not compare equal."""
    _admit(store)
    base = ReadinessFact(observation=_observation("route_boundary"), state="proven")

    proven = _composed(store, readiness=base)
    refuted = _composed(store, readiness=replace(base, state="refuted"))

    assert proven.view_revision != refuted.view_revision


def test_readiness_defaults_to_unobserved_not_ready(store: Any) -> None:
    _admit(store)

    view = _composed(store)

    assert view.readiness.state == "unknown"
    assert view.readiness.observation.availability == "unavailable"


# --- the zero-diff judgment and what it still owes -------------------------


def test_no_change_pending_confirmation_needs_a_person(store: Any) -> None:
    _admit(store, status="success")

    view = _composed(
        store,
        no_change=NoChangeFact(
            state="pending_confirmation",
            policy="confirm",
            reasons=("no attested gaps",),
        ),
        tracker_item=_item("open"),
    )

    assert view.no_change.state == "pending"
    assert view.no_change.judgment_state == "pending_confirmation"
    assert view.no_change.reasons == ["no attested gaps"]
    assert view.exception.state == "no_change_pending"
    assert view.exception.blocking is True
    assert view.safest_action.action == "confirm_no_change"


def test_no_change_authorized_owes_a_source_write(store: Any) -> None:
    _admit(store, status="success")

    view = _composed(
        store,
        no_change=NoChangeFact(
            state="sync_pending", policy="close_if_host_verified", sync_outstanding=1
        ),
        tracker_item=_item("open"),
    )

    assert view.no_change.state == "synchronizing"
    assert view.no_change.sync_outstanding == 1
    assert view.exception.state == "no_change_authorized"
    assert view.safest_action.action == "synchronize_source"


def test_no_change_sync_failure_is_blocking_and_named(store: Any) -> None:
    """An abandoned sync means the item was authorized closed and never was."""
    _admit(store, status="success")

    view = _composed(
        store,
        no_change=NoChangeFact(state="sync_pending", sync_failed=1),
        tracker_item=_item("open"),
    )

    assert view.no_change.state == "sync_failed"
    assert view.exception.state == "no_change_sync_failed"
    assert view.exception.blocking is True
    assert view.safest_action.action == "synchronize_source"


def test_no_change_never_recommends_a_redispatch(store: Any) -> None:
    """A judged zero-diff is a decision; running it again re-asks a settled
    question and can only produce the same empty diff."""
    _admit(store, status="success")

    for state in ("candidate", "attested", "pending_confirmation", "authorized"):
        view = _composed(
            store, no_change=NoChangeFact(state=state), tracker_item=_item("open")
        )
        assert view.safest_action.action != "dispatch"


def test_completed_no_change_is_not_an_exception(store: Any) -> None:
    _admit(store, status="success")

    view = _composed(
        store,
        no_change=NoChangeFact(state="completed"),
        tracker_item=_item("closed"),
    )

    assert view.no_change.state == "completed"
    assert view.exception.state == "none"


# --- publication risk / capacity hold --------------------------------------


def test_risk_hold_is_a_capacity_exception_not_a_publication_failure(
    store: Any,
) -> None:
    """Held for a person is not stuck: the exception says who is holding it."""
    _admit(store, status="awaiting_publication")

    view = _composed(
        store,
        publication=PublicationFact(
            state="pending",
            review_state="pending",
            risk_reasons=("diff_lines", "review_flag"),
            reserved_at="2026-08-18T09:00:00+00:00",
        ),
        tracker_item=_item("open"),
    )

    assert view.exception.state == "publication_risk_hold"
    assert view.exception.blocking is True
    assert "diff_lines" in view.exception.detail
    assert view.safest_action.action == "confirm_publication_risk"


def test_preserved_artifact_without_a_hold_is_still_finalized(store: Any) -> None:
    _admit(store, status="awaiting_publication")

    view = _composed(store, tracker_item=_item("open"))

    assert view.exception.state == "awaiting_publication"
    assert view.safest_action.action == "finalize_publication"


def test_risk_hold_never_becomes_a_retry(store: Any) -> None:
    _admit(store, status="awaiting_publication")

    view = _composed(
        store,
        publication=PublicationFact(state="pending", review_state="pending"),
    )

    assert view.safest_action.action not in {"dispatch", "finalize_publication"}


# --- review pressure: used / total / held / oldest / burden / enforcement ---


def _pressure(**overrides: Any) -> Any:
    base: dict[str, Any] = {
        "delivery_repo": "acme/widgets",
        "engaged": True,
        "countable": True,
        "used": 2,
        "total": 3,
        "held": 2,
        "oldest_hold_age_seconds": 7200.0,
        "diff_burden_lines": 480,
        "policy_generation": 4,
        "observation": RemoteObservation(
            dimension="review_capacity",
            availability="available",
            provenance="observed",
            observed_at=OBSERVED_AT,
        ),
    }
    base.update(overrides)
    return ReviewPressureFact(**base)


def test_pressure_reports_every_number_a_person_needs(store: Any) -> None:
    _admit(store)

    pressure = _composed(store, pressure=_pressure()).review_pressure

    assert (pressure.used, pressure.total) == (2, 3)
    assert pressure.held == 2
    assert pressure.oldest_hold_age_seconds == 7200.0
    assert pressure.diff_burden_lines == 480
    assert pressure.saturated is False
    assert pressure.policy_generation == 4
    assert pressure.observation.availability == "available"


def test_saturated_pressure_explains_itself_with_the_burden(store: Any) -> None:
    _admit(store)

    pressure = _composed(store, pressure=_pressure(used=3)).review_pressure

    assert pressure.saturated is True
    assert pressure.reasons
    joined = " ".join(pressure.reasons)
    assert "3 of 3" in joined
    assert "480" in joined
    assert "2.0h" in joined


def test_uncountable_pressure_is_saturated_never_a_healthy_zero(store: Any) -> None:
    """The forge nobody could read is the queue most likely to be full."""
    _admit(store)

    pressure = _composed(
        store,
        pressure=_pressure(
            countable=False,
            used=0,
            held=0,
            diff_burden_lines=0,
            oldest_hold_age_seconds=None,
            observation=RemoteObservation.unavailable(
                "review_capacity", detail="gh call failed"
            ),
        ),
    ).review_pressure

    assert pressure.countable is False
    assert pressure.saturated is True
    assert pressure.reasons
    assert "not an empty one" in " ".join(pressure.reasons)


def test_unengaged_pressure_invents_nothing_from_silence(store: Any) -> None:
    _admit(store)

    pressure = _composed(store).review_pressure

    assert pressure.engaged is False
    assert pressure.saturated is False
    assert pressure.reasons == []


def test_configured_prevention_reports_inactive_before_the_broker(
    store: Any,
) -> None:
    """Pre-promotion the policy is configured and reported — and enforces
    nothing. Claiming otherwise would be a guarantee the code cannot keep."""
    _admit(store)

    pressure = _composed(store, pressure=_pressure()).review_pressure

    assert pressure.enforcement == "inactive"
    assert "not active" in pressure.enforcement_detail


def test_pressure_saturation_blocks_a_fresh_dispatch(store: Any) -> None:
    """No attempt yet, and the reviewer queue is full: starting more work adds
    to the pile a person already cannot clear."""
    view = compose_work_view(
        replace(
            collect_work_facts(store, "42", tracker_item=_item("open")),
            pressure=_pressure(used=3),
        ),
        composed_at=FIXED_NOW,
    )

    assert view.exception.state == "review_capacity_saturated"
    assert view.safest_action.action == "wait"
    assert view.safest_action.blocked_by == "review_capacity"


# --- route claim -----------------------------------------------------------


@pytest.mark.parametrize(
    ("reported", "claim"),
    [
        ("claude-opus-5", "honored"),
        ("claude-haiku-4-5", "substituted"),
        (None, "unverified"),
    ],
)
def test_route_claim_separates_declared_from_served(
    store: Any, reported: str | None, claim: str
) -> None:
    _admit(store, reported_model=reported)

    view = _composed(store)

    assert view.route.model == "claude-opus-5"
    assert view.route.reported_model == reported
    assert view.route.claim == claim


def test_substituted_route_is_an_exception_worth_naming(store: Any) -> None:
    _admit(store, reported_model="claude-haiku-4-5", status="success")

    view = _composed(store, tracker_item=_item("closed"))

    assert view.exception.state == "route_substituted"
    assert "claude-haiku-4-5" in view.exception.detail


# --- one exception, one action: precedence is total and deterministic ------


def test_exception_precedence_prefers_the_locally_provable_one(store: Any) -> None:
    """Preserved work plus an unreadable forge: the preserved artifact wins,
    because finalizing it needs no network and a retry would destroy it."""
    _admit(store, status="awaiting_publication")

    view = _composed(store, pressure=_pressure(countable=False))

    assert view.exception.state == "awaiting_publication"
    assert view.safest_action.action == "finalize_publication"


def test_in_flight_work_is_an_exception_nobody_should_act_on(store: Any) -> None:
    _admit(store, status="pending", finished_at=None)

    view = _composed(store)

    assert view.exception.state == "in_flight"
    assert view.exception.blocking is False
    assert view.safest_action.action == "wait"


def test_terminal_failure_and_refusal_are_named_apart(store: Any) -> None:
    _admit(store, status="failure")
    failed = _composed(store, tracker_item=_item("open"))

    _admit(store, run_id="r2", status="refused", attempt_number=2)
    refused = _composed(store, tracker_item=_item("open"))

    assert failed.exception.state == "terminal_failure"
    assert refused.exception.state == "terminal_refused"
    assert refused.safest_action.action == "inspect"


def test_orphan_artifact_is_its_own_exception(store: Any) -> None:
    _admit(store, status="failure", pr_url=None)
    store.sync_orphan_prs(
        [(99, "https://example.invalid/pr/99", "agent/issue-42", "failed-run")]
    )

    view = _composed(store, pull_request=_pr("open"), tracker_item=_item("open"))

    assert view.artifact.orphan is True
    assert view.exception.state == "orphan_artifact"
    assert view.safest_action.action == "inspect"


def test_healthy_delivered_work_has_no_exception(store: Any) -> None:
    _admit(store, pr_url="https://example.invalid/pr/7")

    view = _composed(
        store,
        pull_request=replace(
            _pr("merged"), checks="passing", mergeability="not_applicable"
        ),
        tracker_item=_item("closed"),
        readiness=ReadinessFact(
            observation=_observation("route_boundary"), state="proven"
        ),
        pressure=_pressure(),
    )

    assert view.exception.state == "none"
    assert view.exception.blocking is False
    assert view.safest_action.action == "none"


def test_every_composed_view_carries_all_ten_dimensions(store: Any) -> None:
    """Orthogonality is structural: no dimension may be dropped or merged."""
    _admit(store)

    view = _composed(store)

    for name in (
        "identity",
        "source",
        "route",
        "execution",
        "artifact",
        "judgment",
        "outcome",
        "integration",
        "readiness",
        "no_change",
        "review_pressure",
        "exception",
        "safest_action",
    ):
        assert getattr(view, name) is not None


# --- the aggregate projection: one snapshot, one revision ------------------


def test_projection_binds_every_item_to_one_snapshot(store: Any) -> None:
    _admit(store, run_id="a", item_id="41", issue_num=41)
    _admit(store, run_id="b", item_id="42", issue_num=42)

    projection = compose_work_projection(
        store, ["41", "42"], composed_at=FIXED_NOW, delivery_repo="acme/widgets"
    )

    assert [v.identity.item_id for v in projection.views] == ["41", "42"]
    assert projection.projection_revision.startswith("wp1:")
    assert projection.composed_at == FIXED_NOW.isoformat()
    assert projection.unreadable == []


def test_projection_revision_is_fact_derived_not_time_derived(store: Any) -> None:
    _admit(store)

    first = compose_work_projection(store, ["42"], composed_at=FIXED_NOW)
    later = compose_work_projection(
        store, ["42"], composed_at=datetime(2027, 1, 1, tzinfo=UTC)
    )

    assert first.projection_revision == later.projection_revision


def test_projection_revision_moves_when_any_member_moves(store: Any) -> None:
    _admit(store, run_id="a", item_id="41", issue_num=41)
    _admit(store, run_id="b", item_id="42", issue_num=42)
    before = compose_work_projection(store, ["41", "42"], composed_at=FIXED_NOW)

    store.sync_orphan_prs(
        [(7, "https://example.invalid/pr/7", "agent/issue-42", "failed-run")]
    )
    after = compose_work_projection(store, ["41", "42"], composed_at=FIXED_NOW)

    assert before.projection_revision != after.projection_revision


def test_projection_reports_an_unreadable_item_without_sinking_the_rest(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One item that cannot be read is named, not silently dropped and not
    allowed to turn the whole projection into an empty, healthy-looking one."""
    _admit(store, run_id="a", item_id="41", issue_num=41)
    _admit(store, run_id="b", item_id="42", issue_num=42)
    real = store.list_by_item

    def flaky(item_id: str) -> Any:
        if item_id == "41":
            raise RuntimeError("boom")
        return real(item_id)

    monkeypatch.setattr(store, "list_by_item", flaky)

    projection = compose_work_projection(store, ["41", "42"], composed_at=FIXED_NOW)

    assert projection.unreadable == ["41"]
    assert [v.identity.item_id for v in projection.views] == ["42"]


def test_projection_pressure_is_read_once_for_the_whole_set(store: Any) -> None:
    _admit(store, run_id="a", item_id="41", issue_num=41)
    _admit(store, run_id="b", item_id="42", issue_num=42)

    projection = compose_work_projection(
        store, ["41", "42"], composed_at=FIXED_NOW, delivery_repo="acme/widgets"
    )

    assert projection.pressure.delivery_repo == "acme/widgets"
    assert all(
        v.review_pressure.delivery_repo == "acme/widgets" for v in projection.views
    )


def test_projection_is_not_torn_by_a_concurrent_writer(
    tmp_path: Path, store: Any
) -> None:
    """A write landing while the projection is being composed must not appear
    in half of it: the snapshot is held for the whole set."""
    _admit(store, run_id="a", item_id="41", issue_num=41)
    _admit(store, run_id="b", item_id="42", issue_num=42)
    writer = SqliteRunStore(str(tmp_path / "dispatch.db"))
    real = store.list_by_item
    seen: list[str] = []

    def writing(item_id: str) -> Any:
        seen.append(item_id)
        if len(seen) == 1:
            writer.sync_orphan_prs(
                [(7, "https://example.invalid/pr/7", "agent/issue-42", "failed-run")]
            )
        return real(item_id)

    store.list_by_item = writing  # type: ignore[method-assign]
    try:
        projection = compose_work_projection(store, ["41", "42"], composed_at=FIXED_NOW)
    finally:
        store.list_by_item = real  # type: ignore[method-assign]
        writer.close()

    assert seen == ["41", "42"]
    second = next(v for v in projection.views if v.identity.item_id == "42")
    assert second.artifact.orphan is False
    assert second.artifact.state == "absent"


# --- readiness and no-change facts really come from the store --------------


def test_collect_reads_no_change_judgment_from_the_store(store: Any) -> None:
    from kew.no_change import NoChangeIntent

    _admit(store, status="success")
    store.open_no_change_judgment(
        intent=NoChangeIntent(
            source_kind="github",
            source_id="42",
            run_id="r1",
            attempt_number=1,
            base_sha="a" * 40,
            item_id="42",
        ),
        now=FIXED_NOW,
    )

    facts = collect_work_facts(store, "42")

    assert facts.no_change.state == "candidate"
    assert facts.no_change.policy == "confirm"


def test_collect_reads_readiness_from_the_recorded_route_profile(store: Any) -> None:
    _admit(store)

    facts = collect_work_facts(store, "42")

    # No realized profile was recorded for this attempt: unknown, not ready.
    assert facts.readiness.state == "unknown"
    assert facts.readiness.observation.availability == "unavailable"


def test_collect_survives_a_store_without_the_later_slices(store: Any) -> None:
    """A duck-typed store missing the I10/I13 readers degrades to "nothing
    recorded" — never to a fabricated judgment or an invented pressure."""

    class Minimal:
        def read_snapshot(self) -> Any:
            return nullcontext()

        def list_by_item(self, item_id: str) -> list[Any]:
            return []

        def get_admission_identity(self, run_id: str) -> Any:
            return None

        def list_orphan_prs(self, include_acked: bool = False) -> list[Any]:
            return []

    facts = collect_work_facts(Minimal(), "42")

    assert facts.no_change.state is None
    assert facts.readiness.state == "unknown"
    assert facts.pressure.engaged is False


# --- Task 75: the three claims this projection is verified against ---------


def test_concurrent_writers_never_produce_a_torn_view(
    tmp_path: Path, store: Any
) -> None:
    """Two threads composing while a third writes: every view a composer
    returns must be internally consistent with ONE image of the database, and
    two composers seeing the same revision must have seen the same facts."""
    import threading

    _admit(store, run_id="a", item_id="41", issue_num=41)
    _admit(store, run_id="b", item_id="42", issue_num=42)
    store.close()
    # The real multi-surface case: one connection shared across threads (the
    # TUI's), and a second connection writing underneath it.
    shared = SqliteRunStore(str(tmp_path / "dispatch.db"), check_same_thread=False)
    writer = SqliteRunStore(str(tmp_path / "dispatch.db"))
    revisions: list[tuple[str, bool]] = []
    lock = threading.Lock()
    stop = threading.Event()

    def compose() -> None:
        while not stop.is_set():
            projection = compose_work_projection(
                shared, ["41", "42"], composed_at=FIXED_NOW
            )
            orphaned = any(v.artifact.orphan for v in projection.views)
            with lock:
                revisions.append((projection.projection_revision, orphaned))

    threads = [threading.Thread(target=compose) for _ in range(2)]
    for t in threads:
        t.start()
    try:
        for pr_number in range(1, 12):
            writer.sync_orphan_prs(
                [(pr_number, f"http://pr/{pr_number}", "agent/issue-42", "failed-run")]
            )
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=10)
        writer.close()
        shared.close()

    # One revision never describes two different worlds — the digest is over
    # the fact material, so a torn read would show up as the same revision
    # carrying different orphan truth.
    by_revision: dict[str, set[bool]] = {}
    for revision, orphaned in revisions:
        by_revision.setdefault(revision, set()).add(orphaned)
    assert all(len(v) == 1 for v in by_revision.values())


def test_a_remote_failure_never_renders_as_empty_or_healthy(store: Any) -> None:
    """Every remote-derived dimension goes unknown, the exception says the
    remote is unavailable, and nothing reads as delivered or clear."""
    _admit(store, pr_url="https://example.invalid/pr/7")

    view = _composed(
        store,
        pressure=_pressure(
            countable=False,
            used=0,
            observation=RemoteObservation.unavailable("review_capacity"),
        ),
    )

    assert view.judgment.state == "unknown"
    assert view.source.item_state == "unknown"
    assert view.integration.checks == "unknown"
    assert view.outcome.state == "unknown"
    assert view.exception.state != "none"
    assert view.review_pressure.saturated is True
    assert view.safest_action.action not in {"none", "dispatch"}


@pytest.mark.parametrize(
    "publication",
    [PublicationFact(), PublicationFact(state="pending", review_state="pending")],
)
def test_awaiting_publication_never_becomes_a_retry_under_any_pressure(
    store: Any, publication: Any
) -> None:
    """Held or not, preserved work is published or confirmed — never re-run."""
    _admit(store, status="awaiting_publication")

    for pressure in (_pressure(), _pressure(used=3), _pressure(countable=False)):
        action = _composed(
            store, publication=publication, pressure=pressure
        ).safest_action
        assert action.action != "dispatch"
        assert action.action in {"finalize_publication", "confirm_publication_risk"}


def test_pre_broker_surfaces_say_configured_but_not_active(store: Any) -> None:
    """The E5 gate is shut. Reporting a configured policy as enforced would be
    a guarantee the code cannot keep."""
    from kew.publication_job import publication_jobs_active

    _admit(store)

    pressure = _composed(store, pressure=_pressure()).review_pressure

    assert publication_jobs_active() is False
    assert pressure.enforcement == "inactive"
    assert pressure.enforcement_detail.endswith("broker is promoted")


# --- the store-backed reads behind the later-slice dimensions --------------
#
# Every reader below is duck-typed by design (a store that predates a slice
# must degrade, never fabricate), so these exercise the mapping against the
# shapes those stores return rather than against a seeded SQLite file.


class _Slices:
    """A store carrying exactly the later-slice rows each test needs."""

    def __init__(self, base: Any, **rows: Any) -> None:
        self._base = base
        self._rows = rows

    def __getattr__(self, name: str) -> Any:
        if name in self._rows:
            return self._rows[name]
        return getattr(self._base, name)


def _probe(state: str, *, detail: str = "") -> Any:
    return SimpleNamespace(
        state=state,
        run_id="probe-run",
        detail=detail,
        settled_at=OBSERVED_AT,
    )


def _probe_store(store: Any, verdict: str, probe: Any) -> Any:
    return _Slices(
        store,
        get_route_profile=lambda run_id, attempt: SimpleNamespace(fingerprint="fp-1"),
        lookup_readiness_probe=lambda fingerprint, now=None: SimpleNamespace(
            verdict=verdict, probe=probe
        ),
    )


def test_a_succeeded_probe_on_this_boundary_reads_as_proven(store: Any) -> None:
    _admit(store)

    facts = collect_work_facts(_probe_store(store, "hit", _probe("succeeded")), "42")

    assert facts.readiness.state == "proven"
    assert facts.readiness.fingerprint == "fp-1"
    assert facts.readiness.evidence_ref == "probe-run"
    assert facts.readiness.observation.availability == "available"


def test_a_failed_probe_reads_as_refuted_not_as_missing_evidence(store: Any) -> None:
    """Proof that the boundary is broken is evidence, not an absence of it."""
    _admit(store)
    probed = _probe_store(store, "hit", _probe("failed", detail="auth rejected"))

    view = compose_work_view(
        collect_work_facts(probed, "42", tracker_item=_item("open")),
        composed_at=FIXED_NOW,
    )

    assert view.readiness.state == "refuted"
    assert view.exception.state == "readiness_refuted"
    assert view.safest_action.action == "inspect"


def test_an_expired_probe_is_stale_evidence_not_fresh_proof(store: Any) -> None:
    _admit(store)

    facts = collect_work_facts(
        _probe_store(store, "expired", _probe("succeeded")), "42"
    )

    assert facts.readiness.state == "expired"
    assert facts.readiness.observation.availability == "stale"
    assert facts.readiness.observation.provenance == "cached"


def test_a_never_probed_boundary_is_unproven_not_broken(store: Any) -> None:
    _admit(store)

    facts = collect_work_facts(_probe_store(store, "miss", None), "42")

    assert facts.readiness.state == "unproven"
    assert facts.readiness.fingerprint == "fp-1"


def test_publication_job_and_its_review_row_become_the_hold(store: Any) -> None:
    _admit(store, status="awaiting_publication")
    held = _Slices(
        store,
        active_publication_job=lambda lineage: SimpleNamespace(
            job_id="job-1", state="pending"
        ),
        publication_review=lambda job_id: SimpleNamespace(
            review_state="pending",
            risk_reasons=("diff_lines",),
            reserved_at="2026-08-18T09:00:00+00:00",
            delivery_repo="acme/widgets",
            occupies_slot=True,
            risk_added_lines=300,
            risk_deleted_lines=180,
        ),
    )

    view = compose_work_view(
        collect_work_facts(held, "42", tracker_item=_item("open")),
        composed_at=FIXED_NOW,
    )

    assert view.exception.state == "publication_risk_hold"
    assert view.safest_action.action == "confirm_publication_risk"


def test_a_publication_job_with_no_review_row_is_not_a_hold(store: Any) -> None:
    _admit(store, status="awaiting_publication")
    unheld = _Slices(
        store,
        active_publication_job=lambda lineage: SimpleNamespace(
            job_id="job-1", state="pending"
        ),
        publication_review=lambda job_id: None,
    )

    facts = collect_work_facts(unheld, "42")

    assert facts.publication.state == "pending"
    assert facts.publication.review_state is None


def test_local_hold_detail_explains_the_capacity_number(store: Any) -> None:
    """held / oldest / diff burden come from the repository's live holds."""
    from kew.publication_job import RemoteReviewObservation

    _admit(store)
    review = SimpleNamespace(
        delivery_repo="acme/widgets",
        occupies_slot=True,
        reserved_at=datetime(2026, 8, 18, 10, 0, tzinfo=UTC).isoformat(),
        risk_added_lines=300,
        risk_deleted_lines=180,
    )
    engaged = _Slices(
        store,
        review_observation=lambda repo: RemoteReviewObservation(
            delivery_repo=repo,
            availability="available",
            observed_at=FIXED_NOW.isoformat(),
            generation=0,
        ),
        review_capacity_occupancy=lambda *, delivery_repo, observation: 1,
        get_human_review_policy=lambda repo: None,
        list_publication_jobs=lambda: [SimpleNamespace(job_id="job-1")],
        publication_review=lambda job_id: review,
    )

    pressure = collect_work_facts(
        engaged, "42", delivery_repo="acme/widgets", now=FIXED_NOW
    ).pressure

    assert (pressure.used, pressure.total) == (1, 3)
    assert pressure.held == 1
    assert pressure.oldest_hold_age_seconds == 7200.0
    assert pressure.diff_burden_lines == 480
    assert pressure.countable is True


def test_a_capacity_read_that_blows_up_brakes_instead_of_crashing(store: Any) -> None:
    """Engaged and blind. Never a crash, and never a countable zero."""
    _admit(store)

    def boom(repo: str) -> Any:
        raise RuntimeError("the meta table is unreadable")

    pressure = collect_work_facts(
        _Slices(store, review_observation=boom),
        "42",
        delivery_repo="acme/widgets",
        now=FIXED_NOW,
    ).pressure

    assert pressure.engaged is True
    assert pressure.countable is False
    assert pressure.saturated is True
    assert "unreadable" in " ".join(pressure.reasons())


def test_a_downstream_capacity_read_failure_also_brakes(store: Any) -> None:
    from kew.publication_job import RemoteReviewObservation

    _admit(store)

    def boom(**_kwargs: Any) -> int:
        raise RuntimeError("occupancy query failed")

    pressure = collect_work_facts(
        _Slices(
            store,
            review_observation=lambda repo: RemoteReviewObservation(
                delivery_repo=repo,
                availability="available",
                observed_at=FIXED_NOW.isoformat(),
                generation=0,
            ),
            review_capacity_occupancy=boom,
        ),
        "42",
        delivery_repo="acme/widgets",
        now=FIXED_NOW,
    ).pressure

    assert pressure.saturated is True
    assert pressure.used == 0
    assert pressure.countable is False


def test_an_unavailable_observation_is_uncountable_not_empty(store: Any) -> None:
    from kew.publication_job import RemoteReviewObservation

    _admit(store)

    pressure = collect_work_facts(
        _Slices(
            store,
            review_observation=lambda repo: RemoteReviewObservation(
                delivery_repo=repo,
                availability="unavailable",
                observed_at=FIXED_NOW.isoformat(),
                generation=0,
                detail="gh could not be reached",
            ),
            get_human_review_policy=lambda repo: None,
            list_publication_jobs=lambda: [],
        ),
        "42",
        delivery_repo="acme/widgets",
        now=FIXED_NOW,
    ).pressure

    assert pressure.engaged is True
    assert pressure.countable is False
    assert pressure.saturated is True
    assert pressure.observation.availability == "stale"


def test_an_expired_policy_override_resolves_back_to_the_system_bounds(
    store: Any,
) -> None:
    """The override is resolved by the shared rule, on a READ — the projection
    never opens the writing resolver inside a held snapshot."""
    from kew.human_review import ReviewPolicy
    from kew.publication_job import RemoteReviewObservation

    _admit(store)
    override = ReviewPolicy(
        delivery_repo="acme/widgets",
        max_pending_reviews=9,
        generation=1,
        expires_at=datetime(2026, 8, 17, tzinfo=UTC).isoformat(),
    )

    pressure = collect_work_facts(
        _Slices(
            store,
            review_observation=lambda repo: RemoteReviewObservation(
                delivery_repo=repo,
                availability="available",
                observed_at=FIXED_NOW.isoformat(),
                generation=0,
            ),
            review_capacity_occupancy=lambda *, delivery_repo, observation: 0,
            get_human_review_policy=lambda repo: override,
            list_publication_jobs=lambda: [],
        ),
        "42",
        delivery_repo="acme/widgets",
        now=FIXED_NOW,
    ).pressure

    assert pressure.total == 3  # the system bound, not the expired 9
    assert pressure.policy_generation == 2


def test_an_unrecognized_tracker_state_is_inspected_never_dispatched(
    store: Any,
) -> None:
    """A read that succeeded but returned a word we do not model is not
    "open" — guessing either way starts or skips work on a coin flip."""
    view = compose_work_view(
        collect_work_facts(
            store,
            "42",
            tracker_item=TrackerItemFact(
                observation=_observation("tracker_item"), state="triage"
            ),
        ),
        composed_at=FIXED_NOW,
    )

    assert view.exception.state == "source_state_unknown"
    assert "triage" in view.exception.detail
    assert view.safest_action.action == "inspect"


def test_an_unrecognized_pr_state_leaves_the_judgment_unknown(store: Any) -> None:
    _admit(store, pr_url="https://example.invalid/pr/7")

    view = _composed(
        store,
        pull_request=replace(_pr("open"), state="draft"),
        tracker_item=_item("open"),
    )

    assert view.judgment.state == "unknown"
    assert view.exception.state == "judgment_unknown"
    assert view.safest_action.action == "inspect"


# --- I20: the offered actions ----------------------------------------------
#
# One question per test: can a surface be handed a button the projection does
# not stand behind? Every "no" here is a redispatch that cannot be requested.


def _actions(view: Any) -> list[str]:
    return [action.action for action in view.actions]


def test_work_actions_offer_only_finalizing_the_preserved_artifact(store: Any) -> None:
    """Preserved work offers publication of THAT artifact and nothing else.

    There is no dispatch on offer here at any layer — not because a caller is
    trusted to avoid it, but because no such action is ever produced.
    """
    _admit(store, status="awaiting_publication")

    view = _view(store)

    assert _actions(view) == ["finalize_publication"]
    offer = view.actions[0]
    assert (offer.target_kind, offer.target_ref) == ("run", "r1")
    assert offer.safest is True
    assert offer.requires_reason is False


def test_work_actions_finalize_the_same_artifact_with_the_forge_unreadable(
    store: Any,
) -> None:
    """A local artifact needs no network to be publishable, so the offer stands
    even when nothing about the remote could be read."""
    _admit(store, status="awaiting_publication")

    view = _view(store)

    assert view.observations[0].availability == "unavailable"
    assert _actions(view) == ["finalize_publication"]


def test_work_actions_offer_dispatch_for_an_open_unstarted_github_item(
    store: Any,
) -> None:
    view = _view(store, "42", tracker_item=_item("open"))

    assert view.safest_action.action == "dispatch"
    offer = view.actions[0]
    assert (offer.action, offer.target_kind, offer.target_ref) == (
        "dispatch",
        "item",
        "42",
    )


def test_work_actions_withhold_dispatch_while_the_source_cannot_be_read(
    store: Any,
) -> None:
    """The unread-source case fails closed: the safest move is to look."""
    view = _view(store, "42")

    assert view.safest_action.action == "observe_remote"
    assert view.safest_action.blocked_by == "tracker_item"
    assert view.actions == []


def test_work_actions_never_shim_an_opaque_id_into_a_queueable_number(
    store: Any,
) -> None:
    """SFD-12 has no GitHub label to set, so no dispatch is offered for it.

    Withholding the offer is the honest answer. Degrading the opaque id to a
    numeric shim so that a queue call would typecheck is exactly the lie this
    projection exists to end.
    """
    view = _view(store, "SFD-12", tracker_item=_item("open"))

    assert view.safest_action.action == "dispatch"
    assert view.actions == []


def test_work_actions_carry_the_opaque_identity_through_to_the_target(
    store: Any,
) -> None:
    _admit(
        store,
        run_id="r-sfd",
        item_id="SFD-12",
        tracker="linear",
        issue_num=0,
        status="awaiting_publication",
    )

    view = _view(store, "SFD-12")

    assert view.identity.item_id == "SFD-12"
    assert view.identity.item_ref == "SFD-12"
    assert [(a.action, a.target_ref) for a in view.actions] == [
        ("finalize_publication", "r-sfd")
    ]


def test_work_actions_offer_both_no_change_decisions_safest_first(store: Any) -> None:
    """Confirming is the derived safest move; refusing is the same person's
    other entitlement, and it must say why."""
    _admit(store, status="success")

    view = _composed(
        store,
        no_change=NoChangeFact(state="pending_confirmation", judgment_id="nc-1"),
        tracker_item=_item("open"),
    )

    assert _actions(view) == ["confirm_no_change", "reject_no_change"]
    confirm, reject = view.actions
    assert confirm.safest is True and confirm.requires_reason is False
    assert reject.safest is False and reject.requires_reason is True
    assert {a.target_ref for a in view.actions} == {"nc-1"}
    assert {a.target_kind for a in view.actions} == {"no_change_judgment"}


def test_work_actions_offer_a_resync_for_an_authorized_no_change(store: Any) -> None:
    _admit(store, status="success")

    view = _composed(
        store,
        no_change=NoChangeFact(
            state="sync_pending", sync_outstanding=1, judgment_id="nc-1"
        ),
        tracker_item=_item("open"),
    )

    assert _actions(view) == ["retry_no_change_sync"]
    assert view.actions[0].target_ref == "nc-1"


def test_work_actions_withhold_a_no_change_decision_with_no_recorded_judgment(
    store: Any,
) -> None:
    """No canonical id, no offer. A caller can never supply the missing one."""
    _admit(store, status="success")

    view = _composed(
        store,
        no_change=NoChangeFact(state="pending_confirmation"),
        tracker_item=_item("open"),
    )

    assert view.safest_action.action == "confirm_no_change"
    assert view.actions == []


def test_work_actions_offer_confirming_a_publication_risk_hold(store: Any) -> None:
    _admit(store, status="awaiting_publication")

    view = _composed(
        store,
        publication=PublicationFact(
            state="reserved",
            review_state=REVIEW_PENDING,
            risk_reasons=("diff is 900 lines",),
            job_id="job-7",
        ),
    )

    assert view.exception.state == "publication_risk_hold"
    assert [(a.action, a.target_kind, a.target_ref) for a in view.actions] == [
        ("confirm_publication_risk", "publication_job", "job-7")
    ]


@pytest.mark.parametrize(
    ("checks", "mergeability", "expected_action"),
    [
        ("passing", "conflicted", "resolve_conflict"),
        ("failing", "clean", "fix_checks"),
    ],
)
def test_work_actions_offer_nothing_kew_cannot_do_itself(
    store: Any, checks: str, mergeability: str, expected_action: str
) -> None:
    """A conflict and a red build are real next steps that belong to a person.

    Offering an executable action here would mean offering the only one kew
    has — a dispatch — over a published artifact somebody is mid-review on.
    """
    _admit(store, status="success", pr_url="https://example.invalid/pr/7")
    pr = replace(_pr("open"), checks=checks, mergeability=mergeability)

    view = _view(store, pull_request=pr, tracker_item=_item("open"))

    assert view.safest_action.action == expected_action
    assert view.actions == []


def test_work_actions_offer_nothing_while_a_run_is_in_flight(store: Any) -> None:
    _admit(store, status="pending")

    view = _view(store, tracker_item=_item("open"))

    assert view.safest_action.action == "wait"
    assert view.actions == []


def test_work_actions_offer_acknowledging_an_orphan_artifact(store: Any) -> None:
    _admit(store, status="failure", branch="agent/issue-42")
    store.sync_orphan_prs(
        [(7, "https://example.invalid/pr/7", "agent/issue-42", "no-successful-run")]
    )

    view = _view(store, pull_request=_pr("open"), tracker_item=_item("open"))

    assert view.exception.state == "orphan_artifact"
    assert [(a.action, a.target_kind, a.target_ref) for a in view.actions] == [
        ("acknowledge_orphan", "orphan_pr", "7")
    ]


def test_work_actions_replace_dispatch_with_resume_while_the_fleet_is_stopped(
    store: Any,
) -> None:
    """A stopped fleet dispatches nothing, so recommending one would name a
    step that silently does not happen."""
    _admit(store, status="failure")
    store.set_fleet_stopped(reason="budget breach")

    view = _view(store, tracker_item=_item("open"))

    assert view.exception.state == "fleet_stopped"
    assert "budget breach" in view.exception.detail
    assert [(a.action, a.target_kind) for a in view.actions] == [
        ("resume_fleet", "fleet")
    ]


def test_work_actions_return_to_dispatch_once_the_fleet_resumes(store: Any) -> None:
    _admit(store, status="failure")
    store.set_fleet_stopped(reason="budget breach")
    store.clear_fleet_stopped()

    view = _view(store, tracker_item=_item("open"))

    assert _actions(view) == ["dispatch"]


def test_work_actions_are_digested_into_the_view_revision(store: Any) -> None:
    """A caller's expected revision pins the ACTIONS it saw, not only the
    facts — otherwise two views could agree on a revision and disagree about
    what may be requested against it."""
    _admit(store, status="failure")
    open_source = _view(store, tracker_item=_item("open"))
    stopped = replace(
        collect_work_facts(store, "42", tracker_item=_item("open"), now=FIXED_NOW),
        fleet_stopped=True,
        fleet_stop_reason="budget breach",
    )

    recomposed = compose_work_view(stopped, composed_at=FIXED_NOW)

    assert _actions(open_source) != _actions(recomposed)
    assert open_source.view_revision != recomposed.view_revision


def test_work_actions_fail_closed_on_a_reading_that_is_merely_stale() -> None:
    """The resolver's own guard, asserted directly: a verdict derived from a
    dimension nobody could read *currently* offers nothing, whatever the
    verdict's action word happens to be."""
    stale = RemoteObservation(
        dimension="tracker_item", availability="stale", provenance="cached"
    )
    facts = WorkFacts(
        item_id="42",
        identity=None,
        runs=(),
        orphan_prs=(),
        pull_request=PullRequestFact(
            observation=RemoteObservation(dimension="pull_request")
        ),
        tracker_item=TrackerItemFact(observation=stale, state="open"),
    )
    exception = WorkExceptionDTO(state="not_started", dimension="source")
    safest = SafestActionDTO(action="dispatch", reason="")

    assert offered_actions(facts, exception, safest) == []


# --- certificate: the third truth, backed by the real resolver (P1.1) ------
#
# These compose against :mod:`kew.certificate_ref` itself — a real evidence
# ref, a real audit binding, a real anchor — rather than a hand-built fact.
# The frontend already renders these states (#1541/#1602) from a mock; the
# point of this block is that the same states now come out of the resolver
# that will actually feed it.


@pytest.fixture
def _git_allowlisted(monkeypatch: pytest.MonkeyPatch) -> None:
    resolved = shutil.which("git")
    if resolved is None:  # pragma: no cover - the image always has git
        pytest.skip("the certificate carrier is a git ref")
    monkeypatch.setitem(subprocess_utils.TOOL_PATHS, "git", resolved)


CERT_SUBJECT = "c" * 40


class _Ledger:
    """The audit rows the resolver binds a certificate append to."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def append_audit_event(self, **kwargs: Any) -> int:
        row_id = len(self.rows) + 1
        self.rows.append({"id": row_id, **kwargs})
        return row_id


@pytest.fixture
def carrier(tmp_path: Path, _git_allowlisted: None) -> Any:
    path = tmp_path / "carrier"
    path.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    return SimpleNamespace(path=path, ledger=_Ledger())


def _envelope(kind: str, status: str, issued_at: str) -> CertificateEnvelope:
    return CertificateEnvelope(
        kind=kind,
        subject=CERT_SUBJECT,
        status=status,
        lane="claude-code/github",
        fingerprint="fp-1",
        issued_at=issued_at,
        actor="release",
    )


def _resolved(carrier: Any, **kwargs: Any) -> CertificateFact:
    """Read the REAL carrier and adapt it exactly as a caller would."""
    anchors = [
        Anchor(
            id=len(carrier.ledger.rows),
            entry_hash="e" * 64,
            count=len(carrier.ledger.rows),
            timestamp=OBSERVED_AT.isoformat(),
            prev="0" * 64,
        )
    ]
    params: dict[str, Any] = {
        "audit_rows": carrier.ledger.rows,
        "anchors": anchors,
        "fetched_at": OBSERVED_AT,
        "now": FIXED_NOW,
    }
    params.update(kwargs)
    reading = resolve_certificate(carrier.path, CERT_SUBJECT, **params)
    return CertificateFact.from_reading(reading, observed_at=params["fetched_at"])


def _seed_other_subject(carrier: Any) -> None:
    """Make the carrier real but silent about THIS subject, so a resolution of
    ``uncertified`` is a genuine read rather than an absent ref."""
    append_certificate(
        carrier.path,
        replace(
            _envelope(KIND_CERTIFICATE, "certified", "2026-08-16T09:00:00+00:00"),
            subject="d" * 40,
        ),
        ledger=carrier.ledger,
        now=OBSERVED_AT,
    )


def _certify(carrier: Any) -> None:
    append_certificate(
        carrier.path,
        _envelope(KIND_CERTIFICATE, "certified", "2026-08-17T09:00:00+00:00"),
        ledger=carrier.ledger,
        now=OBSERVED_AT,
    )


def test_work_view_certificate_defaults_to_unavailable_not_uncertified(
    store: Any,
) -> None:
    """Nobody fetched the evidence ref. That is not "no certificate"."""
    _admit(store, status="success")

    view = _view(store)

    assert view.certificate_status.state == "unknown"
    assert view.certificate_status.observation.availability == "unavailable"
    assert view.certificate_status.observation.provenance == "unobserved"


def test_work_view_an_anchored_certificate_reads_as_certified(
    store: Any, carrier: Any
) -> None:
    _admit(store, status="success")
    _certify(carrier)

    view = _composed(store, certificate=_resolved(carrier))

    assert view.certificate_status.state == "certified"
    assert view.certificate_status.certified_at == "2026-08-17T09:00:00+00:00"
    assert view.certificate_status.lane == "claude-code/github"
    assert view.certificate_status.observation.availability == "available"


def test_work_view_a_passing_probe_never_implies_a_certificate(
    store: Any, carrier: Any
) -> None:
    """Truth one and truth three are independent: a proven boundary and a
    successful run leave certification exactly where the carrier left it."""
    _admit(store, status="success")
    _seed_other_subject(carrier)

    view = _composed(
        store,
        readiness=ReadinessFact(
            observation=_observation("route_boundary"), state="proven"
        ),
        certificate=_resolved(carrier),
        tracker_item=_item("open"),
    )

    assert view.readiness.state == "proven"
    assert view.execution.state == "completed"
    assert view.certificate_status.state == "uncertified"
    assert view.certificate_status.observation.availability == "available"


def test_work_view_a_certificate_never_implies_current_reachability(
    store: Any, carrier: Any
) -> None:
    """And the reverse: a standing certificate cannot repair a refuted probe."""
    _admit(store, status="success")
    _certify(carrier)

    view = _composed(
        store,
        readiness=ReadinessFact(
            observation=_observation("route_boundary"), state="refuted"
        ),
        certificate=_resolved(carrier),
    )

    assert view.certificate_status.state == "certified"
    assert view.readiness.state == "refuted"
    assert view.exception.dimension == "readiness"


def test_work_view_certification_ready_is_not_certified(
    store: Any, carrier: Any
) -> None:
    """A build status and a journey certificate are different envelopes and
    stay different answers all the way out to the view."""
    _admit(store, status="success")
    append_certificate(
        carrier.path,
        _envelope(KIND_READY, "build_accepted", "2026-08-17T08:00:00+00:00"),
        ledger=carrier.ledger,
        now=OBSERVED_AT,
    )

    view = _composed(store, certificate=_resolved(carrier))

    assert view.certificate_status.state == "uncertified"
    assert "certification_ready" in view.certificate_status.detail


def test_work_view_unreadable_certificate_evidence_is_never_uncertified(
    store: Any, carrier: Any
) -> None:
    """The honesty gate: unreadable evidence is unknown, in both directions —
    it neither erases a certificate nor invents an absence of one."""
    _admit(store, status="success")
    _certify(carrier)

    view = _composed(store, certificate=_resolved(carrier, audit_rows=[]))

    assert view.certificate_status.state == "unknown"
    assert view.certificate_status.observation.availability == "unavailable"
    assert view.certificate_status.observation.provenance == "error"


def test_work_view_a_stale_certificate_reading_is_not_a_current_claim(
    store: Any, carrier: Any
) -> None:
    _admit(store, status="success")
    _certify(carrier)

    view = _composed(
        store, certificate=_resolved(carrier, fetched_at=FIXED_NOW - timedelta(days=5))
    )

    assert view.certificate_status.state == "unknown"
    assert view.certificate_status.observation.availability == "stale"


def test_work_view_the_certificate_never_moves_the_safest_action(
    store: Any, carrier: Any
) -> None:
    """Certification is evidence about a build, never a reason to act. The
    verdict and the offered set must be identical with and without one."""
    _admit(store, status="success")
    _certify(carrier)
    without = _composed(store, tracker_item=_item("open"))

    with_certificate = _composed(
        store, certificate=_resolved(carrier), tracker_item=_item("open")
    )

    assert with_certificate.exception == without.exception
    assert with_certificate.safest_action == without.safest_action
    assert with_certificate.actions == without.actions


def test_work_view_the_certificate_participates_in_the_view_revision(
    store: Any, carrier: Any
) -> None:
    """It is a fact in the view, so two views that differ in it must differ."""
    _admit(store, status="success")
    before = _composed(store, certificate=_resolved(carrier))
    _certify(carrier)

    after = _composed(store, certificate=_resolved(carrier))

    assert before.view_revision != after.view_revision


def test_work_view_certificate_evidence_is_not_an_operational_health_reading(
    store: Any, carrier: Any
) -> None:
    """The certificate's own observation rides on its dimension and STAYS
    there. The aggregate list gates the health verdict, the TUI evidence word
    and the offered set — none of which journey certification may touch, or
    an unfetched evidence ref would render every item in the fleet unhealthy.
    """
    _admit(store, status="success")

    view = _composed(store, certificate=_resolved(carrier))

    assert view.certificate_status.observation.dimension == "certificate_evidence"
    assert "certificate_evidence" not in {o.dimension for o in view.observations}


# --------------------------------------------------------------------------- #
# The journaled merge state is read PAST THE FIRST PAGE (reviewer finding)
# --------------------------------------------------------------------------- #


def _journal_node(item_id: str, merge_state: str, read_at: str) -> dict[str, Any]:
    """One snapshot node as the driver journals it (``journal._node_entry``)."""
    return {
        "id": item_id,
        "status": "in_review",
        "pr_url": f"https://example.invalid/pr/{item_id}",
        "head_oid": "9f8e7d1",
        "merge_state": merge_state,
        "merge_state_since": read_at,
        "merge_state_at": read_at,
        "external": False,
    }


def test_a_conflict_past_the_journals_first_page_still_blocks(store: Any) -> None:
    """The merge-state read is PAGED, or truncation reads as a clean tree.

    ``latest_node_pr_facts`` is a bounded, cursored query whose default page is
    ``DEFAULT_NODE_PR_FACTS_LIMIT`` rows; the projection called it with no limit
    and no cursor, so on a host past that many open linked artifacts a
    conflicted tree was simply ABSENT from the Work projection — the blocking
    ``merge_conflict`` silently off, which is the shape of the defect this
    reading is supposed to remove (#2303).
    """
    read_at = (FIXED_NOW - timedelta(minutes=1)).isoformat()
    # The conflicted item sorts LAST by (epic_ref, item_id), behind a full
    # default page of clean ones.
    nodes = [
        _journal_node(str(100 + n), "CLEAN", read_at)
        for n in range(DEFAULT_NODE_PR_FACTS_LIMIT)
    ]
    nodes.append(_journal_node("9001", "DIRTY", read_at))
    store.append_epic_tick(
        epic_ref="an-epic",
        tick_ts=read_at,
        snapshot={"schema": 1, "epic_ref": "an-epic", "nodes": nodes},
        actions={},
        snapshot_hash="h1",
    )
    _admit(
        store,
        item_id="9001",
        issue_num=9001,
        pr_url="https://example.invalid/pr/9001",
        pr_state="OPEN",
    )

    projection = work_projection(
        store, DispatchConfig(), item_ids=["9001"], now=FIXED_NOW
    )

    view = next(v for v in projection.views if v.identity.item_id == "9001")
    assert view.integration.mergeability == "conflicted"
    assert view.exception.state == "merge_conflict"


def test_a_store_with_no_journal_reader_says_the_tree_was_unread(store: Any) -> None:
    """An ``unknown`` mergeability has two causes and they are different worlds:
    no tick ever recorded a tree state, or nobody could look. The detail is the
    only place a reader of this fact could tell them apart."""

    class _NoJournal:
        """The store, minus the journaled-fact reader."""

        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def __getattr__(self, name: str) -> Any:
            if name == "latest_node_pr_facts":
                raise AttributeError(name)
            return getattr(self._inner, name)

    _admit(
        store,
        item_id="9001",
        issue_num=9001,
        pr_url="https://example.invalid/pr/9001",
        pr_state="OPEN",
    )

    projection = work_projection(
        _NoJournal(store), DispatchConfig(), item_ids=["9001"], now=FIXED_NOW
    )

    view = next(v for v in projection.views if v.identity.item_id == "9001")
    assert view.integration.mergeability == "unknown"
    assert "could not be read" in (view.integration.observation.detail or "")
