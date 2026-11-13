"""#2217 — a dispatch-refusal hold must not outlive the dispatch that succeeds.

Incident (merge-path drive #2204, 2026-09-29): #2201 and #2202 were each
refused on their first dispatch attempt and held under
``credential_issuer_refused: issuer unavailable``. A later dispatch of the same
nodes SUCCEEDED, opened PRs, and the reviewer returned NEEDS_CHANGES. The
refusal rows survived all of it, and the remediator refuses to run while any
hold stands — so for two hours both nodes read ``remediating``, every tick
printed ``signal=progressing``, and ``kew epic watch`` emitted nothing.

Two contracts, one per defect:

* the dispatch that disproves a refusal CLEARS its hold, so remediation has a
  hold-free path (:func:`test_a_successful_dispatch_clears_the_stale_refusal_hold`
  and the end-to-end composition below);
* a REMEDIATING node whose remediation a standing hold blocks reads HELD with
  that reason, and the attention stream `kew epic watch` reads emits it.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from support.driver_fakes import EPIC, NOW, FakeFactsReader, FakeSource, _node

import kew.run_store as run_store_module
import kew.run_store_review_rounds as run_rounds_store
from kew import credential_issuer
from kew.config import DispatchConfig, EpicConfig, LoggingConfig, ReviewConfig
from kew.epic_holds import (
    CI_PENDING_HOLD_REASON,
    CREDENTIAL_ISSUER_REFUSED_HOLD_PREFIX,
    PREEXISTING_WORKTREE_REFUSED_HOLD_PREFIX,
)
from kew.graph.build import build_graph
from kew.graph.domain import Graph, Node, NodeStatus
from kew.graph.driver import tick as tick_mod
from kew.graph.driver.attention import (
    UNBUCKETED_HELD_HINT,
    AttentionKind,
    attention_events,
    bucket_for,
)
from kew.graph.driver.facts import NodeFacts, ReviewPressure
from kew.graph.driver.overlay import DriverStatus, ReviewOverlay, overlay_status
from kew.graph.driver.result import TerminalSignal
from kew.graph.driver.review_stage import RemediationContext, drive_review_stage
from kew.run_store import REVIEW_REMEDIATION_RUN_KIND, SqliteRunStore
from kew.service import verbs

ALPHA = "alpha"
ITEM = "42"
HEAD = "c0ffee11"
REFUSAL_REASON = f"credential_issuer_refused: {credential_issuer.IssuerRefused.reason}"
WORKTREE_REASON = "preexisting_worktree_refused: dirty worktree already on disk"
#: The two actions STALL_BUCKETS owns for those reasons (see the round-2
#: section at the foot of this module).
CREDENTIAL_ISSUER_HINT = "re-check the issuer its reason names, then kew epic unhold"
PREEXISTING_WORKTREE_HINT = (
    "re-check the worktree its reason names, then kew epic unhold"
)


@pytest.fixture
def store():
    with SqliteRunStore(":memory:") as opened:
        yield opened


def _graph() -> Graph:
    return Graph(
        [Node(id=ITEM, title="refused node", status=NodeStatus.TODO, epic_ref=ALPHA)],
        [],
    )


def _dispatch(store: Any, *, outcome: Any) -> tuple[Any, dict[str, DriverStatus]]:
    """One frontier pass over the single node, with the reservation scripted.

    ``outcome`` is either a refusal to raise or the string one reservation
    returns.
    """
    graph = _graph()
    statuses = {ITEM: DriverStatus.TODO}
    kwargs = (
        {"side_effect": outcome}
        if isinstance(outcome, BaseException)
        else {"return_value": outcome}
    )
    config = DispatchConfig()
    with (
        patch.object(tick_mod, "reserve_and_dispatch", **kwargs),
        patch.object(tick_mod, "remote_branch_exists", return_value=False),
    ):
        return tick_mod._dispatch_frontier(
            graph,
            graph,
            {ITEM: NodeStatus.TODO},
            statuses,
            provider="linear",
            reserved_item_ids=set(),
            already_stuck_ids=set(),
            name=ALPHA,
            store=store,
            dispatch_sink=MagicMock(),
            config=config,
            max_parallel=4,
            fleet_cap=4,
            branch_prefix="kew/",
            pressure=ReviewPressure(delivery_repo="owner/repo"),
            dependency_effects={},
            liveness=None,
        ), statuses


def _review_config(tmp_path: Path) -> DispatchConfig:
    return DispatchConfig(
        review=ReviewConfig(adversarial=True, blocking=True, max_rounds=3),
        logging=LoggingConfig(dir=str(tmp_path / "logs")),
    )


def _facts(*, head_oid: str = HEAD) -> NodeFacts:
    return NodeFacts(
        issue_num=int(ITEM),
        state="OPEN",
        state_reason=None,
        labels=frozenset(),
        pr_number=7,
        pr_url=None,
        review_decision=None,
        merge_state_status=None,
        head_oid=head_oid,
        pr_pushed_at=None,
    )


def _needs_changes_overlay(*, reserved_kind: str | None = None) -> ReviewOverlay:
    return ReviewOverlay(
        enabled=True,
        reserved_kind=reserved_kind,
        round_count=1,
        latest_verdict="NEEDS_CHANGES",
        latest_head_sha=HEAD,
        current_head_sha=HEAD,
    )


def _frozen(moment: datetime, module: Any) -> Any:
    """Freeze one store module's clock, so a write lands at *moment*.

    The order of ``epic_holds.held_at`` against ``review_rounds.recorded_at``
    is the whole question below — whether the node reached the review
    lifecycle before or after the refusal — so the tests state it outright
    instead of leaning on two wall-clock reads landing microseconds apart.
    """
    clock = MagicMock()
    clock.now.return_value = moment
    return patch.object(module, "datetime", clock)


# ---------------------------------------------------------------------------
# Defect 1: the hold must not outlive the dispatch that disproves it
# ---------------------------------------------------------------------------


def test_a_successful_dispatch_clears_the_stale_refusal_hold(store: Any) -> None:
    """The refusal is a property of ONE attempt, not of the node: the attempt
    that succeeds is what disproves it."""
    _dispatch(store, outcome=credential_issuer.IssuerRefused())
    assert [h.reason for h in store.list_epic_holds(ALPHA)] == [REFUSAL_REASON]

    outcome, _ = _dispatch(store, outcome="dispatched")

    assert outcome.dispatched == outcome.in_flight == [ITEM]
    assert store.list_epic_holds(ALPHA) == [], (
        "the dispatch that succeeded must clear the refusal it disproved"
    )


@pytest.mark.parametrize(
    "refusal",
    [
        credential_issuer.IssuerRefused(),
        tick_mod._PreexistingWorktreeRefusal("dirty worktree already on disk"),
    ],
)
def test_every_frontier_refusal_prefix_clears_on_a_later_success(
    store: Any, refusal: BaseException
) -> None:
    """Both reasons the frontier refusal path writes clear the same way."""
    _dispatch(store, outcome=refusal)
    assert store.list_epic_holds(ALPHA) != []

    _dispatch(store, outcome="dispatched")

    assert store.list_epic_holds(ALPHA) == []


@pytest.mark.parametrize(
    "reason",
    [
        "ci_pending",
        "review_rounds_exhausted",
        "admission_refused: no mapped image — un-hold with `kew epic unhold`",
        "sandbox_containment: no mapped image",
        "budget_preflight: $5.00 over the $1.00 cap",
    ],
)
def test_a_dispatch_never_clears_a_hold_somebody_else_owns(
    store: Any, reason: str
) -> None:
    """Only the driver's OWN pre-dispatch refusal clears on a success. Every
    other hold is honored by the overlay (so no dispatch should reach the node
    at all) and clears on its own terms — a dispatch that raced one must not
    un-hold it."""
    store.hold_epic_node_item(ALPHA, ITEM, reason)

    _dispatch(store, outcome="dispatched")

    assert [h.reason for h in store.list_epic_holds(ALPHA)] == [reason]


def test_the_release_re_reads_the_reason_inside_the_delete(store: Any) -> None:
    """The mandatory check-then-act guard. The detached child THIS reservation
    starts can upsert an ``admission_refused`` hold over the refusal row, so
    the reason the release matches on has to be read inside the store's delete
    transaction — a release that matched on an earlier read would un-hold a
    refusal nothing disproved."""
    store.hold_epic_node_item(ALPHA, ITEM, REFUSAL_REASON)
    child_hold = "admission_refused: no credential for codex — un-hold to retry"

    def dispatch_then_refuse_in_the_child(*_args: Any, **_kwargs: Any) -> str:
        # Exactly the window: the reservation is away and the child has already
        # written its own hold by the time the parent looks.
        store.hold_epic_node_item(ALPHA, ITEM, child_hold)
        return "dispatched"

    with (
        patch.object(
            tick_mod, "reserve_and_dispatch", dispatch_then_refuse_in_the_child
        ),
        patch.object(tick_mod, "remote_branch_exists", return_value=False),
    ):
        tick_mod._dispatch_frontier(
            _graph(),
            _graph(),
            {ITEM: NodeStatus.TODO},
            {ITEM: DriverStatus.TODO},
            provider="linear",
            reserved_item_ids=set(),
            already_stuck_ids=set(),
            name=ALPHA,
            store=store,
            dispatch_sink=MagicMock(),
            config=DispatchConfig(),
            max_parallel=4,
            fleet_cap=4,
            branch_prefix="kew/",
            pressure=ReviewPressure(delivery_repo="owner/repo"),
            dependency_effects={},
            liveness=None,
        )

    assert [h.reason for h in store.list_epic_holds(ALPHA)] == [child_hold]


def test_a_cleared_refusal_lets_the_next_remediation_run(tmp_path: Path) -> None:
    """The incident end to end: refused, dispatched, reviewed NEEDS_CHANGES —
    and the next tick starts the remediation instead of sitting on the hold."""
    calls: list[RemediationContext] = []
    config = _review_config(tmp_path)
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _dispatch(store, outcome=credential_issuer.IssuerRefused())
        _dispatch(store, outcome="dispatched")
        store.record_review_round(
            ALPHA, ITEM, head_sha=HEAD, verdict="NEEDS_CHANGES", sidecar_digest="d"
        )
        facts = _facts()
        status = overlay_status(
            facts,
            [],
            reserved=False,
            held_reason=next((h.reason for h in store.list_epic_holds(ALPHA)), None),
            review=_needs_changes_overlay(),
        )
        assert status is DriverStatus.REMEDIATING

        outcome = drive_review_stage(
            {ITEM: status},
            {ITEM: facts},
            epic_ref=ALPHA,
            config=config,
            store=store,
            repo="o/r",
            repo_root=tmp_path,
            base="main",
            reviewer_launcher=None,
            remediator=calls.append,
            now=NOW,
        )

    assert [c.item_id for c in calls] == [ITEM]
    assert outcome.remediating == [ITEM]


# ---------------------------------------------------------------------------
# Defect 2: a hold that blocks remediation must not be silent
# ---------------------------------------------------------------------------


def test_remediation_blocked_by_a_standing_hold_reads_held() -> None:
    """``remediating`` with no live run and no hold-free path is the silent
    stall #2153/#2107 set out to remove: report the hold."""
    status = overlay_status(
        _facts(),
        [],
        reserved=False,
        held_reason=REFUSAL_REASON,
        review=_needs_changes_overlay(),
    )

    assert status is DriverStatus.HELD


def test_a_live_remediation_run_outranks_the_hold() -> None:
    """A remediator actually in flight is real work, not a stall — the status
    must keep naming it."""
    status = overlay_status(
        _facts(),
        [],
        reserved=False,
        held_reason=REFUSAL_REASON,
        review=_needs_changes_overlay(reserved_kind=REVIEW_REMEDIATION_RUN_KIND),
    )

    assert status is DriverStatus.REMEDIATING


def test_a_held_remediation_node_owes_no_review_action(tmp_path: Path) -> None:
    """The stage must not dispatch a remediation the remediator will refuse."""
    calls: list[RemediationContext] = []
    config = _review_config(tmp_path)
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        store.hold_epic_node_item(ALPHA, ITEM, REFUSAL_REASON)
        store.record_review_round(
            ALPHA, ITEM, head_sha=HEAD, verdict="NEEDS_CHANGES", sidecar_digest="d"
        )
        facts = _facts()
        status = overlay_status(
            facts,
            [],
            reserved=False,
            held_reason=REFUSAL_REASON,
            review=_needs_changes_overlay(),
        )
        outcome = drive_review_stage(
            {ITEM: status},
            {ITEM: facts},
            epic_ref=ALPHA,
            config=config,
            store=store,
            repo="o/r",
            repo_root=tmp_path,
            base="main",
            reviewer_launcher=None,
            remediator=calls.append,
            now=NOW,
        )

    assert calls == []
    assert outcome.remediating == []


def _wedged_summary(store: Any, *, reason: str) -> Any:
    """`kew epic status`'s read-only snapshot of ONE hold-wedged node.

    The shape the incident left behind: an open PR whose reviewer returned
    NEEDS_CHANGES, and *reason* standing on the node so nothing can remediate
    it. `summarize` is the path every read-only surface takes — `kew epic
    status`, `kew epic watch` — so both round-2 and round-3 contracts below
    read this one snapshot.
    """
    deps = FakeSource(nodes=[_node("1")], edges=[])
    graph = build_graph(deps, EPIC)
    config = DispatchConfig(
        review=ReviewConfig(adversarial=True, blocking=True, max_rounds=3),
        epic=EpicConfig(stall_days=3),
    )
    store.hold_epic_node_item(EPIC, "1", reason)
    store.record_review_round(
        EPIC, "1", head_sha=HEAD, verdict="NEEDS_CHANGES", sidecar_digest="d"
    )
    facts = FakeFactsReader(
        issue_facts_by_num={
            1: NodeFacts(
                issue_num=1,
                state="OPEN",
                state_reason=None,
                labels=frozenset(),
                pr_number=7,
                pr_url=None,
                review_decision=None,
                merge_state_status=None,
                head_oid=HEAD,
                pr_pushed_at=None,
            )
        }
    )
    return tick_mod.summarize(
        EPIC, graph, config=config, store=store, facts=facts, now=NOW
    )


def test_watch_emits_a_held_event_for_a_hold_blocked_remediation(store: Any) -> None:
    """The watch surface, end to end: `kew epic watch` reads the read-only
    snapshot through ``attention_events``, and it emitted NOTHING for two hours
    on #2201/#2202. It now emits one `held` event naming the reason."""
    summary = _wedged_summary(store, reason=REFUSAL_REASON)

    assert summary.held == ("1",)
    events = attention_events(EPIC, summary)
    assert [(e.kind, e.item) for e in events] == [(AttentionKind.HELD, "1")]
    assert "credential_issuer_refused" in events[0].reason
    assert events[0].action == CREDENTIAL_ISSUER_HINT, (
        "the event has to carry the refusal's OWN action, not the fallback: "
        "both read 'kew epic unhold', so only the exact text tells them apart"
    )


# ---------------------------------------------------------------------------
# Review round 2: the refusal vocabulary, and retry once the wedge exists
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reason", "prefix", "label", "hint"),
    [
        (
            REFUSAL_REASON,
            CREDENTIAL_ISSUER_REFUSED_HOLD_PREFIX,
            "held on a credential-issuer refusal",
            CREDENTIAL_ISSUER_HINT,
        ),
        (
            WORKTREE_REASON,
            PREEXISTING_WORKTREE_REFUSED_HOLD_PREFIX,
            "held on a pre-existing-worktree refusal",
            PREEXISTING_WORKTREE_HINT,
        ),
    ],
)
def test_each_refusal_reason_owns_a_bucket_label_and_action(
    reason: str, prefix: str, label: str, hint: str
) -> None:
    """Pin the vocabulary, not "an unhold is mentioned somewhere".

    Both reasons already reached the event stream through
    ``UNBUCKETED_HELD_HINT``, whose text also contains ``kew epic unhold`` — so
    an assertion on the wording alone passes with the buckets deleted, and the
    thing the buckets add (a LABEL for `kew epic status`'s dominant-cause stall
    line, which has none for an unbucketed reason) goes untested. Both hints
    say re-check rather than naming a repair: one prefix serves a live refusal
    and a refusal gone stale under an open PR, and only the first has anything
    to fix.
    """
    bucket = bucket_for(reason)

    assert bucket is not None, reason
    assert (bucket.prefix, bucket.label, bucket.hint, bucket.kind) == (
        prefix,
        label,
        hint,
        AttentionKind.HELD,
    )
    assert bucket.hint != UNBUCKETED_HELD_HINT


@pytest.mark.parametrize(
    ("refusal", "prefix"),
    [
        (credential_issuer.IssuerRefused(), CREDENTIAL_ISSUER_REFUSED_HOLD_PREFIX),
        (
            tick_mod._PreexistingWorktreeRefusal("dirty worktree already on disk"),
            PREEXISTING_WORKTREE_REFUSED_HOLD_PREFIX,
        ),
    ],
)
def test_a_live_frontier_refusal_reports_a_bucketed_reason(
    store: Any, refusal: BaseException, prefix: str
) -> None:
    """The refusal the frontier has just hit is the case the buckets describe,
    and it was the one case they could not label: the hold row written from the
    refusal carried the prefix, while the stuck row every operator surface
    reads carried the bare detail and fell through to the fallback."""
    outcome, statuses = _dispatch(store, outcome=refusal)

    (stuck,) = outcome.stuck
    bucket = bucket_for(stuck.reason)
    assert bucket is not None and bucket.prefix == prefix, stuck.reason
    assert statuses[ITEM] is DriverStatus.HELD
    assert [h.reason for h in store.list_epic_holds(ALPHA)] == [stuck.reason], (
        "the hold row and the row an operator reads must say the same thing"
    )


@pytest.mark.parametrize("reason", [REFUSAL_REASON, WORKTREE_REASON])
def test_retry_stays_available_while_the_node_still_awaits_dispatch(
    store: Any, reason: str
) -> None:
    """The #1957/#2154 contract, unchanged: a refusal hold leaves the node
    FAILED and dispatchable, the next dispatch clears it, and retry is the
    documented way to ask for that dispatch."""
    config = DispatchConfig()
    config.project.repo = "o/r"
    store.hold_epic_node_item(ALPHA, ITEM, reason)

    with patch("kew.labels.run") as gh:
        gh.return_value = MagicMock(returncode=0, stdout="", stderr="")
        verbs.epic_retry(store, epic_ref=ALPHA, item_id=ITEM, config=config)

    assert [row.item_id for row in store.list_epic_retries(ALPHA)] == [ITEM]


@pytest.mark.parametrize("reason", [REFUSAL_REASON, WORKTREE_REASON])
def test_retry_refuses_once_the_refusal_hold_has_wedged_the_review(
    store: Any, reason: str
) -> None:
    """Precedence 8a took the recovery retry was protecting away.

    A reviewer round recorded AFTER this hold means the node went past
    dispatch carrying it: the PR is open and nothing will dispatch the node
    again — so no dispatch will disprove the refusal, the overlay reads the
    node HELD, and a retry would emit its audit event and write its marker
    while the node stayed held. The marker would also reset the review cycle
    out from under the open PR. That is exactly the #2154 drift
    `kew.epic_holds` forbids, so retry refuses and names the one command that
    moves the node.
    """
    config = DispatchConfig()
    config.project.repo = "o/r"
    with _frozen(NOW, run_store_module):
        store.hold_epic_node_item(ALPHA, ITEM, reason)
    with _frozen(NOW + timedelta(hours=1), run_rounds_store):
        store.record_review_round(
            ALPHA, ITEM, head_sha=HEAD, verdict="NEEDS_CHANGES", sidecar_digest="d"
        )
    assert (
        overlay_status(
            _facts(),
            [],
            reserved=False,
            held_reason=reason,
            review=_needs_changes_overlay(),
        )
        is DriverStatus.HELD
    ), "precondition: this is the node precedence 8a holds"

    with pytest.raises(verbs.EpicRetryHeld) as exc:
        verbs.epic_retry(store, epic_ref=ALPHA, item_id=ITEM, config=config)

    assert "kew epic unhold" in str(exc.value)
    assert store.list_epic_retries(ALPHA) == [], "no marker"
    assert store.list_audit_events(event="epic_node_retry") == [], "no audit event"
    assert store.latest_review_round(ITEM) is not None, "and the cycle survives"


# ---------------------------------------------------------------------------
# Review round 3: the refusal must only outlive a retry once the node is past
# dispatch, and the wedge it does survive into is not progress
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reason", [REFUSAL_REASON, WORKTREE_REASON])
def test_retry_stays_available_when_the_refusal_followed_the_rounds(
    store: Any, reason: str
) -> None:
    """Rounds OLDER than the hold are an abandoned PR, not a wedge.

    The node's PR is gone, so the overlay honors nothing for it (precedence 8a
    needs a `remediating` node) and it reads `todo` — dispatchable, which is
    the state the frontier refusal that wrote this row was hit in. Refusing
    retry there takes away the documented recovery AND strands the abandoned
    PR's rounds: `kew epic unhold`, the command the refusal names, is not a
    writer of the ``epic_retries`` marker, so only a retry restarts the cycle
    they count against (``review.max_rounds``).
    """
    config = DispatchConfig()
    config.project.repo = "o/r"
    with _frozen(NOW, run_rounds_store):
        store.record_review_round(
            ALPHA, ITEM, head_sha=HEAD, verdict="NEEDS_CHANGES", sidecar_digest="d"
        )
    with _frozen(NOW + timedelta(hours=1), run_store_module):
        store.hold_epic_node_item(ALPHA, ITEM, reason)

    with patch("kew.labels.run") as gh:
        gh.return_value = MagicMock(returncode=0, stdout="", stderr="")
        verbs.epic_retry(store, epic_ref=ALPHA, item_id=ITEM, config=config)

    assert [row.item_id for row in store.list_epic_retries(ALPHA)] == [ITEM]
    assert store.review_round_count(ITEM) == 0, (
        "and the retry is what restarts the cycle those rounds were counted in"
    )


def test_a_hold_wedged_remediation_never_reads_progressing(store: Any) -> None:
    """The second #2217 symptom: every tick printed ``signal=progressing``.

    `_held_signal` counted any hold inside its stall clock as a live one, so
    the wedged node bought the epic a PROGRESSING signal for `epic.stall_days`
    — and PROGRESSING means something moved, or a `ci_pending` gate is still
    inside its clock (`TerminalSignal`). Nothing is waiting on anything here:
    the row is the driver's own superseded bookkeeping and no pass will clear
    it. STALLED from the first tick, which is also what makes `kew epic run`
    exit non-zero on it (#2107).
    """
    summary = _wedged_summary(store, reason=REFUSAL_REASON)

    assert summary.signal is TerminalSignal.STALLED
    assert summary.held == ("1",), "still a hold an operator clears, not a failure"
    assert [n.reason for n in summary.waiting] == [REFUSAL_REASON], (
        "and the reason still reaches every surface that reads the snapshot"
    )


def test_an_honored_hold_inside_its_stall_clock_still_reads_progressing(
    store: Any,
) -> None:
    """The narrowness of the fix above. A `ci_pending` gate is the hold
    PROGRESSING was written for — approved PR, checks running, nobody waiting
    — and it keeps the signal it had."""
    summary = _wedged_summary(store, reason=CI_PENDING_HOLD_REASON)

    assert summary.signal is TerminalSignal.PROGRESSING
