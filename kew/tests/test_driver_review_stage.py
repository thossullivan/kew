"""#991 — the governed-review drive pass wired end-to-end.

The guard the epic was missing: a real (fake-tracker) PR-opened node drives the
FULL loop — reviewer dispatched (real materialization + head-ref masking) →
sidecar produced → verdict parsed → round recorded → verdict attested on the
chain → node transitions — with the audit chain verify green. This is the test
whose absence let five correct-in-isolation components look like a finished
stage while nothing fed the merge gate.

Plus the transition drivers #988 defined (READY → in_review, NEEDS_CHANGES →
remediating → round cap → HELD) and the mandatory `concurrent-state-change`
false-positive: a live reviewer mid-startup survives the reconcile sweep.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

import pytest
from support.gate_path_repo import add_pr_commit, make_pr_repo
from support.gh_fakes import FakeGh, GhProcessBoundary
from support.review_stream import (
    result_line,
    reviewer_stream,
    stream_lines,
    stream_rate_limited,
    stream_verdict,
    verdict_message,
)
from test_remediation_flow_gate import review_retry_scene as review_retry_scene

from kew.audit_chain import verify_chain
from kew.config import DispatchConfig, LoggingConfig, ReviewConfig
from kew.epic_holds import holds_node_out_of_frontier, safe_hold_reason
from kew.graph.driver import review_stage
from kew.graph.driver.facts import NodeFacts
from kew.graph.driver.merge_queue import MERGE_VERIFY_REVIEW_MODE
from kew.graph.driver.overlay import DriverStatus, ReviewOverlay, overlay_status
from kew.graph.driver.review_stage import RemediationContext, drive_review_stage
from kew.graph.driver.tick import _reconcile_reservations
from kew.pseudo_epics import REVIEW_EPIC_REF
from kew.publication import PUBLICATION_CONFIGURATION_REFUSAL
from kew.review_audit import REVIEW_VERDICT_EVENT, VERDICT_DEGRADED
from kew.review_contract import CollectedSidecar, parse_verdict_sidecar
from kew.review_dispatch import ReviewerLaunchContext
from kew.review_outcome import (
    STAGE_PRE_LAUNCH,
    STATUS_FAILED,
    ReviewOutcome,
    record_review_outcome,
)
from kew.run_store import ADVERSARIAL_REVIEW_RUN_KIND, RunRecord, SqliteRunStore
from kew.sandbox import detect_runtime_cached
from kew.subprocess_utils import run
from kew.waits import classify_wait

EPIC = "epic:x"
ITEM = "42"
NOW = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)


def _drive_retry_scene(scene, tmp_path, *, remediator=None):
    from kew.graph.driver.review_launcher import sandbox_remediator

    holds = scene.store.list_epic_holds(EPIC)
    facts = _facts(scene.checkout.head_sha)
    statuses = {
        ITEM: overlay_status(
            facts,
            scene.store.list_runs_by_branch(scene.checkout.branch),
            reserved=False,
            held_reason=holds[0].reason if holds else None,
            review=ReviewOverlay(
                enabled=True,
                round_count=1,
                latest_verdict="NEEDS_CHANGES",
                latest_head_sha=scene.checkout.head_sha,
                current_head_sha=scene.checkout.head_sha,
            ),
        )
    }
    return drive_review_stage(
        statuses,
        {ITEM: facts},
        epic_ref=EPIC,
        config=scene.config,
        store=scene.store,
        repo="o/r",
        repo_root=tmp_path,
        base="main",
        reviewer_launcher=None,
        remediator=remediator
        or sandbox_remediator(scene.config, scene.store, epic_ref=EPIC),
        now=NOW,
    )


def _retry_findings(scene, tmp_path):
    sidecar = tmp_path / "findings.json"
    sidecar.write_text(
        json.dumps(
            {
                "verdict": "NEEDS_CHANGES",
                "findings": [
                    {
                        "severity": "high",
                        "location": "code.py:1",
                        "summary": "unchecked path",
                        "evidence": _evidence(),
                    }
                ],
            }
        )
    )
    scene.store.record_review_round(
        EPIC,
        ITEM,
        head_sha=scene.checkout.head_sha,
        verdict="NEEDS_CHANGES",
        sidecar_digest="d",
        sidecar_path=str(sidecar),
    )


def test_governed_refusal_recovers_after_repair_and_unhold(
    review_retry_scene, tmp_path
):
    scene = review_retry_scene
    _retry_findings(scene, tmp_path)
    first = _drive_retry_scene(scene, tmp_path)
    assert first.held == [ITEM]
    scene.materialize.assert_not_called()
    scene.config.sandbox.allow_uncontained_flows = True
    # This is the store operation used by `kew epic unhold`.
    assert scene.store.remove_epic_hold(EPIC, ITEM) is not None
    second = _drive_retry_scene(scene, tmp_path)
    scene.publish.assert_called_once()
    assert scene.publish.call_args.kwargs["producer_run_id"] is None
    assert second.remediating == [ITEM]
    assert scene.store.list_epic_holds(EPIC) == []
    assert scene.store.list_epic_reservations(REVIEW_EPIC_REF) == []


def _mixed_branch_run(
    scene, run_id: str, item_id: str, run_kind: str, hour: int, *, profile: bool
) -> None:
    """One row on the scene's branch, contained with a profile unless told not."""
    from kew.readiness import (
        Containment,
        RealizedLaunchBoundary,
        compose_realized_profile,
        compose_route_profile,
    )

    scene.store.save(
        RunRecord(
            run_id=run_id,
            issue_num=int(item_id or ITEM),
            item_id=item_id,
            branch=scene.checkout.branch,
            status="success",
            exit_code=0,
            started_at=datetime(2026, 7, 20, hour, tzinfo=UTC),
            finished_at=datetime(2026, 7, 20, hour, 30, tzinfo=UTC),
            attempt_number=1,
            container_name=f"kew-{run_id}" if profile else None,
            runner_name="claude-code",
            model="claude-opus-5",
            run_kind=run_kind,
            error_category="" if profile else "base_integration_step",
        )
    )
    if profile:
        scene.store.record_route_profile(
            compose_realized_profile(
                run_id=run_id,
                attempt_number=1,
                route=compose_route_profile(
                    runner="claude-code", requested_model="claude-opus-5"
                ),
                boundary=RealizedLaunchBoundary(
                    containment=Containment.CONTAINED,
                    image_ref="kew-agent:latest",
                    image_id="sha256:" + "a" * 64,
                    auth_path="env:SYNTHETIC_PROVIDER_KEY",
                ),
            )
        )


def test_drive_remediates_a_merge_main_head_on_a_branch_with_mixed_items(
    review_retry_scene, tmp_path
):
    """#3067 end to end: the drive's own remediation launches on that head.

    The branch's runs name the tracker item, the PR number (a hand-started
    ``kew review 7``) and ``''`` (the merge-main resolve run that pushed the
    head and claimed it), all contained, and the newest row is the merge-main
    marker with no route profile. Every launch on the node used to be refused
    before it started as missing ancestor evidence; the claim now binds the
    head to the resolve run and the remediation publishes from it.
    """
    from kew.audit import Actor, emit
    from kew.publication_credentials import publication_producer_for_head
    from kew.review_outcome import list_review_outcomes

    scene = review_retry_scene
    head, branch = scene.checkout.head_sha, scene.checkout.branch
    _mixed_branch_run(scene, "dispatch", ITEM, "dispatch", 1, profile=True)
    _mixed_branch_run(scene, "pr-review", "7", "review", 2, profile=True)
    _mixed_branch_run(scene, "resolve", "", "resolve", 4, profile=True)
    emit(
        scene.store,
        "publication_intent",
        actor=Actor("policy", "host-publication"),
        run_id="resolve",
        issue_num=7,
        item_id=branch,
        payload={
            "branch": branch,
            "scope": "branch",
            "commit_sha": head,
            "tree_sha": "d" * 40,
            "parent_sha": "c" * 40,
        },
    )
    _mixed_branch_run(scene, "marker", ITEM, "resolve", 5, profile=False)
    _retry_findings(scene, tmp_path)
    # The scene has no container runtime; this test is about the producer.
    scene.config.sandbox.allow_uncontained_flows = True

    assert (
        publication_producer_for_head(scene.store, branch=branch, head_sha=head)
        == "resolve"
    )
    outcome = _drive_retry_scene(scene, tmp_path)

    assert outcome.remediating == [ITEM]
    assert scene.store.list_epic_holds(EPIC) == []
    assert [
        o.classification
        for o in list_review_outcomes(scene.store, pr_number=7)
        if o.status == STATUS_FAILED
    ] == []
    scene.launch.assert_called_once()
    assert scene.publish.call_args.kwargs["producer_run_id"] == "resolve"


def test_containment_hold_survives_exhausted_cleanup_retries(
    review_retry_scene, tmp_path, caplog, capsys
):
    import sqlite3

    from kew.run_store import _BUSY_MAX_RETRIES

    scene = review_retry_scene
    _retry_findings(scene, tmp_path)
    execute = scene.store._execute
    deletions = []

    def busy_delete(sql, parameters=()):
        if sql.startswith("DELETE FROM epic_reservations"):
            deletions.append(parameters)
            raise sqlite3.OperationalError("database is locked")
        return execute(sql, parameters)

    with (
        patch.object(scene.store, "_execute", side_effect=busy_delete),
        patch.object(scene.store, "busy_sleep"),
    ):
        first = _drive_retry_scene(scene, tmp_path)

    [reservation] = scene.store.list_epic_reservations(REVIEW_EPIC_REF)
    assert deletions == [(ITEM, reservation.run_id)] * (_BUSY_MAX_RETRIES + 1)
    assert first.held == [ITEM]
    [hold] = scene.store.list_epic_holds(EPIC)
    assert hold.reason.startswith("admission_refused: ")
    [event] = scene.store.list_audit_events(event="review.outcome")
    assert event.payload["classification"] == "containment_refused"
    [row] = scene.store.list_by_item(ITEM)
    assert row.status == "refused"
    assert row.error_category == "containment_refused"
    [completed] = scene.store.list_audit_events(event="dispatch_completed")
    assert completed.run_id == row.run_id
    diagnostics = caplog.text + capsys.readouterr().err
    assert "Failed to release review reservation" in diagnostics
    assert reservation.run_id in diagnostics
    assert ITEM in diagnostics
    assert "database is locked" in diagnostics

    assert (
        _reconcile_reservations(
            REVIEW_EPIC_REF, scene.store, scene.config, now=datetime.now(UTC)
        )
        == set()
    )
    assert scene.store.list_epic_reservations(REVIEW_EPIC_REF) == []
    # Repaired admission must not bypass the operator's explicit unhold.
    scene.config.sandbox.allow_uncontained_flows = True
    second = _drive_retry_scene(scene, tmp_path)
    assert second.held == second.remediating == []
    assert scene.store.list_epic_holds(EPIC) == [hold]
    assert len(scene.store.list_by_item(ITEM)) == 1
    scene.materialize.assert_not_called()
    scene.launch.assert_not_called()


def test_runtime_install_and_unhold_repairs_running_driver(
    review_retry_scene, tmp_path, monkeypatch
):
    from kew.graph.driver.review_launcher import sandbox_remediator

    scene = review_retry_scene
    _retry_findings(scene, tmp_path)
    monkeypatch.setattr("kew.sandbox.detect_runtime_cached", detect_runtime_cached)
    monkeypatch.setattr(
        "kew.remediation_containment.detect_runtime_cached", detect_runtime_cached
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-credential")
    remediator = sandbox_remediator(scene.config, scene.store, epic_ref=EPIC)
    with patch("kew.sandbox.detect_runtime", return_value=None) as detector:
        first = _drive_retry_scene(scene, tmp_path, remediator=remediator)
        assert first.held == [ITEM]
        scene.materialize.assert_not_called()
        assert len(scene.store.list_by_item(ITEM)) == 1
        # This is the store operation used by `kew epic unhold`.
        assert scene.store.remove_epic_hold(EPIC, ITEM) is not None
        detector.return_value = "/installed/docker"
        detector.reset_mock()
        second = _drive_retry_scene(scene, tmp_path, remediator=remediator)
        assert second.held == []
        assert second.remediating == [ITEM]
        assert scene.config.sandbox.uncontained_flows_allowed is False
        assert scene.store.list_epic_holds(EPIC) == []
        scene.materialize.assert_called_once()
        scene.launch.assert_called_once()
        assert scene.launch.call_args.kwargs["sandbox_posture"] == "sandboxed"
        scene.publish.assert_called_once()
        assert len(scene.store.list_by_item(ITEM)) == 1
        assert scene.store.list_epic_reservations(REVIEW_EPIC_REF) == []
        # Admission and later lookups reuse the repaired positive observation.
        assert detect_runtime_cached(scene.config.sandbox) == "/installed/docker"
        detector.assert_called_once_with(scene.config.sandbox)


@pytest.mark.parametrize("failure", ["raise", "unconfirmed"])
def test_containment_hold_survives_missing_completion_events(
    review_retry_scene, tmp_path, monkeypatch, caplog, capsys, failure
):
    scene = review_retry_scene
    _retry_findings(scene, tmp_path)
    append = scene.store.append_audit_event

    def fail_completion(**kwargs):
        if kwargs["event"] in {"review.outcome", "dispatch_completed"}:
            if failure == "raise":
                raise OSError("injected completion persistence failure")
            return None
        return append(**kwargs)

    monkeypatch.setattr(scene.store, "append_audit_event", fail_completion)
    first = _drive_retry_scene(scene, tmp_path)
    second = _drive_retry_scene(scene, tmp_path)
    rows = scene.store.list_runs_by_branch(scene.checkout.branch)
    assert len(rows) == 1
    assert rows[0].status == "refused"
    [posture] = scene.store.list_audit_events(event="sandbox_resolved")
    assert posture.run_id == rows[0].run_id
    assert scene.store.list_audit_events(event="dispatch_completed") == []
    assert scene.store.list_audit_events(event="review.outcome") == []
    [hold] = scene.store.list_epic_holds(EPIC)
    assert hold.reason.startswith("admission_refused: ")
    assert "allow_uncontained_flows = true" in hold.reason
    assert first.held == [ITEM]
    assert second.held == second.remediating == []
    scene.materialize.assert_not_called()
    scene.launch.assert_not_called()
    assert scene.store.list_epic_reservations(REVIEW_EPIC_REF) == []
    diagnostics = caplog.text + capsys.readouterr().err
    assert "review.outcome" in diagnostics
    assert "Failed to record containment refusal" in diagnostics
    assert (
        "injected completion persistence failure"
        if failure == "raise"
        else "persistence was not confirmed"
    ) in diagnostics


@pytest.fixture(autouse=True)
def _register_git(monkeypatch):
    from kew.subprocess_utils import TOOL_PATHS

    git = shutil.which("git")
    assert git is not None, "git required for review-stage tests"
    monkeypatch.setitem(TOOL_PATHS, "git", git)


def _make_repo(tmp_path: Path) -> tuple[Path, str]:
    """A one-commit repo on ``main``; returns ``(repo_root, head_sha)``."""
    repo = tmp_path / "repo"
    repo.mkdir()
    run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    run(["git", "config", "user.email", "t@t.t"], cwd=repo, check=True)
    run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "code.py").write_text("x = 1\n")
    run(["git", "add", "-A"], cwd=repo, check=True)
    run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)
    head = run(["git", "rev-parse", "HEAD"], cwd=repo, check=True).stdout.strip()
    return repo, head


def _config(tmp_path: Path, *, blocking: bool, max_rounds: int = 2) -> DispatchConfig:
    return DispatchConfig(
        review=ReviewConfig(adversarial=True, blocking=blocking, max_rounds=max_rounds),
        logging=LoggingConfig(dir=str(tmp_path / "logs")),
    )


def _facts(head_oid: str, *, pr_number: int | None = 7) -> NodeFacts:
    return NodeFacts(
        issue_num=int(ITEM),
        state="OPEN",
        state_reason=None,
        labels=frozenset(),
        pr_number=pr_number,
        pr_url=None,
        review_decision=None,
        merge_state_status=None,
        head_oid=head_oid,
        pr_pushed_at=None,
    )


def _evidence() -> dict[str, str]:
    return {"command": "uv run pytest -q", "output_digest": "sha256:deadbeef"}


def _ready_launcher(ctx: ReviewerLaunchContext) -> CollectedSidecar:
    """A fake reviewer: ends with an evidenced READY verdict block in its final
    result event, exactly as a real container reviewer would, and the host
    collects it from the stream (#2021) — the real parse/digest path runs."""
    sidecar = {"verdict": "READY", "findings": [], "acceptance_evidence": [_evidence()]}
    return stream_verdict(ctx, sidecar)


def _needs_changes_launcher(ctx: ReviewerLaunchContext) -> CollectedSidecar:
    finding = {
        "severity": "high",
        "location": "code.py:1",
        "summary": "unchecked path",
        "evidence": {"command": "uv run pytest -q", "output_digest": "sha256:cafe"},
    }
    sidecar = {"verdict": "NEEDS_CHANGES", "findings": [finding]}
    return stream_verdict(ctx, sidecar)


def _crash_launcher(ctx: ReviewerLaunchContext) -> CollectedSidecar:
    """The reviewer ends without a result event → the crash path."""
    return stream_verdict(ctx, None)


# --- THE guard: the full loop, chain green ---


def test_advisory_full_loop_ready_verdict_chain_green(tmp_path: Path):
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path, blocking=False)
    facts = _facts(head)
    statuses = {ITEM: DriverStatus.ADVERSARIAL_REVIEW}

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        out = drive_review_stage(
            statuses,
            {ITEM: facts},
            epic_ref=EPIC,
            config=config,
            store=store,
            repo="o/r",
            repo_root=repo,
            base="main",
            reviewer_launcher=_ready_launcher,
            now=NOW,
        )
        # Round recorded, bound to the reviewed sha.
        latest = store.latest_review_round(ITEM)
        assert latest is not None
        assert latest.verdict == "READY"
        assert latest.head_sha == head
        assert store.review_round_count(ITEM) == 1
        # Verdict attested — and the chain verifies.
        events = [
            e
            for e in store.list_audit_events(item_id=ITEM)
            if e.event == REVIEW_VERDICT_EVENT
        ]
        assert len(events) == 1
        assert events[0].payload["verdict"] == "READY"
        assert events[0].payload["head_sha"] == head
        assert verify_chain(store.read_chain_rows()).ok
        # Reservation released on the terminal path; worktree cleaned.
        assert store.list_epic_reservations(REVIEW_EPIC_REF) == []
        assert out.verdicts == {ITEM: "READY"}
        # Node transitions: the next overlay reads READY bound to the live head
        # → IN_REVIEW (the approval surface).
        ds = overlay_status(
            facts,
            [],
            reserved=False,
            held_reason=None,
            review=ReviewOverlay(
                enabled=True,
                round_count=1,
                latest_verdict="READY",
                latest_head_sha=head,
                current_head_sha=head,
            ),
        )
        assert ds == DriverStatus.IN_REVIEW


def test_needs_changes_records_and_transitions_to_remediating(tmp_path: Path):
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path, blocking=True)
    facts = _facts(head)
    statuses = {ITEM: DriverStatus.ADVERSARIAL_REVIEW}

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        drive_review_stage(
            statuses,
            {ITEM: facts},
            epic_ref=EPIC,
            config=config,
            store=store,
            repo="o/r",
            repo_root=repo,
            base="main",
            reviewer_launcher=_needs_changes_launcher,
            now=NOW,
        )
        latest = store.latest_review_round(ITEM)
        assert latest is not None and latest.verdict == "NEEDS_CHANGES"
        assert verify_chain(store.read_chain_rows()).ok
        # Overlay reads NEEDS_CHANGES → REMEDIATING (findings stand).
        ds = overlay_status(
            facts,
            [],
            reserved=False,
            held_reason=None,
            review=ReviewOverlay(
                enabled=True,
                round_count=1,
                latest_verdict="NEEDS_CHANGES",
                latest_head_sha=head,
                current_head_sha=head,
            ),
        )
        assert ds == DriverStatus.REMEDIATING


def test_needs_changes_dispatches_remediator(tmp_path: Path):
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path, blocking=True, max_rounds=2)
    facts = _facts(head)
    statuses = {ITEM: DriverStatus.REMEDIATING}
    calls: list[RemediationContext] = []

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        # A standing NEEDS_CHANGES round against the current head → remediate.
        store.record_review_round(
            EPIC, ITEM, head_sha=head, verdict="NEEDS_CHANGES", sidecar_digest="d"
        )
        out = drive_review_stage(
            statuses,
            {ITEM: facts},
            epic_ref=EPIC,
            config=config,
            store=store,
            repo="o/r",
            repo_root=repo,
            base="main",
            reviewer_launcher=_ready_launcher,
            remediator=calls.append,
            now=NOW,
        )
        assert len(calls) == 1
        assert calls[0].review_round == 1
        assert calls[0].head_sha == head
        assert out.remediating == [ITEM]
        # No second reviewer round was dispatched (remediation must land first).
        assert store.review_round_count(ITEM) == 1


def test_publication_configuration_refusal_holds_remediation_until_unheld(
    tmp_path: Path,
) -> None:
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path, blocking=True, max_rounds=2)
    facts = _facts(head)
    statuses = {ITEM: DriverStatus.REMEDIATING}
    attempts: list[RemediationContext] = []

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        store.record_review_round(
            EPIC, ITEM, head_sha=head, verdict="NEEDS_CHANGES", sidecar_digest="d"
        )

        def refuse(ctx: RemediationContext) -> None:
            attempts.append(ctx)
            refusal = ReviewOutcome(
                pr_number=ctx.pr_number or 0,
                status=STATUS_FAILED,
                stage=STAGE_PRE_LAUNCH,
                classification=PUBLICATION_CONFIGURATION_REFUSAL,
                detail="checkout publication requires explicit host-risk admission",
                head_sha=ctx.head_sha,
                item_id=ctx.item_id,
            )
            record_review_outcome(
                refusal,
                store=store,
                config=config,
                notify=False,
            )

        first = drive_review_stage(
            statuses,
            {ITEM: facts},
            epic_ref=EPIC,
            config=config,
            store=store,
            repo="o/r",
            repo_root=repo,
            base="main",
            reviewer_launcher=_ready_launcher,
            remediator=refuse,
            now=NOW,
        )

        assert first.held == [ITEM]
        assert first.remediating == []
        assert statuses[ITEM] == DriverStatus.HELD
        [hold] = store.list_epic_holds(EPIC)
        assert hold.reason.startswith("admission_refused: ")
        assert "checkout publication requires explicit host-risk admission" in (
            hold.reason
        )

        next_status = overlay_status(
            facts,
            [],
            reserved=False,
            held_reason=hold.reason,
            review=ReviewOverlay(
                enabled=True,
                round_count=1,
                latest_verdict="NEEDS_CHANGES",
                latest_head_sha=head,
                current_head_sha=head,
            ),
        )
        assert next_status == DriverStatus.HELD
        second = drive_review_stage(
            {ITEM: next_status},
            {ITEM: facts},
            epic_ref=EPIC,
            config=config,
            store=store,
            repo="o/r",
            repo_root=repo,
            base="main",
            reviewer_launcher=_ready_launcher,
            remediator=refuse,
            now=NOW,
        )

    assert len(attempts) == 1
    assert second.remediating == []


def test_an_unrecorded_deferred_checkout_move_holds_remediation(
    tmp_path: Path,
) -> None:
    """#3186: ``kew review`` refused to move a checkout holding deferred work
    whose move it could not record. Every tick would refuse the same way, so
    the node holds with the reason, instead of re-dispatching each tick."""
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path, blocking=True, max_rounds=2)
    statuses = {ITEM: DriverStatus.REMEDIATING}
    detail = (
        "/wt/review/pr-7 holds a deferred publication's work and its move could "
        "not be re-recorded (database is locked)"
    )

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        store.record_review_round(
            EPIC, ITEM, head_sha=head, verdict="NEEDS_CHANGES", sidecar_digest="d"
        )

        def refuse(ctx: RemediationContext) -> None:
            record_review_outcome(
                ReviewOutcome(
                    pr_number=ctx.pr_number or 0,
                    status=STATUS_FAILED,
                    stage=STAGE_PRE_LAUNCH,
                    classification="deferred_checkout_unrecorded",
                    detail=detail,
                    head_sha=ctx.head_sha,
                    item_id=ctx.item_id,
                ),
                store=store,
                config=config,
                notify=False,
            )

        out = drive_review_stage(
            statuses,
            {ITEM: _facts(head)},
            epic_ref=EPIC,
            config=config,
            store=store,
            repo="o/r",
            repo_root=repo,
            base="main",
            reviewer_launcher=_ready_launcher,
            remediator=refuse,
            now=NOW,
        )

        assert out.held == [ITEM]
        assert statuses[ITEM] == DriverStatus.HELD
        [hold] = store.list_epic_holds(EPIC)
        assert hold.reason.startswith("admission_refused: ")
        assert "deferred_checkout_unrecorded" in hold.reason
        assert "could not be re-recorded" in hold.reason


def test_a_github_auth_refusal_holds_remediation(tmp_path: Path) -> None:
    """A remediation refused because gh cannot authenticate refuses the same
    way every tick until a person logs in or fixes the token, so the node
    holds with the reason instead of re-dispatching (and notifying) each tick."""
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path, blocking=True, max_rounds=2)
    statuses = {ITEM: DriverStatus.REMEDIATING}
    detail = "GitHub CLI is not logged in — run `gh auth login`"
    attempts: list[str] = []

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        store.record_review_round(
            EPIC, ITEM, head_sha=head, verdict="NEEDS_CHANGES", sidecar_digest="d"
        )

        def refuse(ctx: RemediationContext) -> None:
            attempts.append(ctx.item_id)
            record_review_outcome(
                ReviewOutcome(
                    pr_number=ctx.pr_number or 0,
                    status=STATUS_FAILED,
                    stage=STAGE_PRE_LAUNCH,
                    classification="github_auth",
                    detail=detail,
                    head_sha=ctx.head_sha,
                    item_id=ctx.item_id,
                ),
                store=store,
                config=config,
                notify=False,
            )

        out = drive_review_stage(
            statuses,
            {ITEM: _facts(head)},
            epic_ref=EPIC,
            config=config,
            store=store,
            repo="o/r",
            repo_root=repo,
            base="main",
            reviewer_launcher=_ready_launcher,
            remediator=refuse,
            now=NOW,
        )

        assert out.held == [ITEM]
        assert out.remediating == []
        assert statuses[ITEM] == DriverStatus.HELD
        [hold] = store.list_epic_holds(EPIC)
        assert hold.reason.startswith("admission_refused: ")
        assert "gh auth login" in hold.reason

    assert attempts == [ITEM]


@pytest.mark.parametrize("malformed_settings", [False, True])
def test_containment_refusal_holds_production_remediation_across_ticks(
    tmp_path, malformed_settings
):
    from kew.graph.driver.review_launcher import sandbox_remediator
    from kew.review import dispatch_review
    from kew.review_checkout import ReviewCheckout, review_checkout_path
    from kew.runners.claude_code import ClaudeCodeRunner
    from kew.runners.registry import ResolvedRoute
    from kew.sandbox import command_guard_state

    head = "a" * 40
    config = _config(tmp_path, blocking=True)
    config.sandbox.enabled = True
    config.branches.worktree_base = str(tmp_path / "checkouts")
    preserved = review_checkout_path(config, 7) / ".claude" / "settings.json"
    if malformed_settings:
        preserved.parent.mkdir(parents=True)
        preserved.write_bytes(b"[" * 20_000 + b"]" * 20_000)
    sidecar = tmp_path / "findings.json"
    sidecar.write_text(
        json.dumps(
            {
                "verdict": "NEEDS_CHANGES",
                "findings": [
                    {
                        "severity": "high",
                        "location": "code.py:1",
                        "summary": "unchecked path",
                        "evidence": _evidence(),
                    }
                ],
            }
        )
    )
    checkout = ReviewCheckout(
        tmp_path / "checkout", head, "agent/issue-42", False, None, None
    )
    with (
        SqliteRunStore(tmp_path / "d.db") as store,
        patch(
            "kew.review._get_pr_metadata",
            return_value={
                "headRefName": checkout.branch,
                "headRefOid": head,
                "baseRefName": "main",
                "body": "Closes #42",
            },
        ),
        patch("kew.review._check_github_connectivity", return_value=True),
        patch(
            "kew.runners.resolve_runner",
            return_value=ResolvedRoute(ClaudeCodeRunner(), "claude-opus-5"),
        ),
        patch(
            "kew.review.resolve_runner",
            return_value=ResolvedRoute(ClaudeCodeRunner(), "claude-opus-5"),
        ),
        patch(
            "kew.runner_preflight.runner_preflight_failure", return_value=None
        ) as credential_preflight,
        patch("kew.sandbox.detect_runtime_cached", return_value=None),
        patch(
            "kew.review.materialize_review_checkout", return_value=checkout
        ) as materialize,
        patch("kew.review.run_tracked_agent") as launch,
        patch("kew.review.dispatch_review", wraps=dispatch_review) as dispatch,
        patch("kew.sandbox.command_guard_state", wraps=command_guard_state) as guard,
    ):
        store.record_review_round(
            EPIC,
            ITEM,
            head_sha=head,
            verdict="NEEDS_CHANGES",
            sidecar_digest="d",
            sidecar_path=str(sidecar),
        )
        remediator = Mock(wraps=sandbox_remediator(config, store, epic_ref=EPIC))
        facts = _facts(head)
        ticks = []
        for _ in range(2):
            holds = store.list_epic_holds(EPIC)
            statuses = {
                ITEM: overlay_status(
                    facts,
                    store.list_runs_by_branch(checkout.branch),
                    reserved=False,
                    held_reason=holds[0].reason if holds else None,
                    review=ReviewOverlay(
                        enabled=True,
                        round_count=1,
                        latest_verdict="NEEDS_CHANGES",
                        latest_head_sha=head,
                        current_head_sha=head,
                    ),
                )
            }
            ticks.append(
                drive_review_stage(
                    statuses,
                    {ITEM: facts},
                    epic_ref=EPIC,
                    config=config,
                    store=store,
                    repo="o/r",
                    repo_root=tmp_path,
                    base="main",
                    reviewer_launcher=None,
                    remediator=remediator,
                    now=NOW,
                )
            )

        if malformed_settings:
            guard.assert_not_called()
            assert preserved.read_bytes() == b"[" * 20_000 + b"]" * 20_000
        refusals = store.list_runs_by_branch(checkout.branch)
        assert len(refusals) == 1
        assert refusals[0].status == "refused"
        assert refusals[0].error_category == "containment_refused"
        assert refusals[0].exit_code == 2
        [event] = store.list_audit_events(event="sandbox_resolved")
        [completed] = store.list_audit_events(event="dispatch_completed")
        assert event.run_id == completed.run_id == refusals[0].run_id
        assert event.payload["reason"] == "runtime_unavailable"
        [hold] = store.list_epic_holds(EPIC)
        assert hold.reason.startswith("admission_refused: ")
        assert "runtime" in hold.reason
        assert "not found" in hold.reason
        assert "allow_uncontained_flows = true" in hold.reason
        assert "kew epic unhold" in hold.reason
        assert statuses[ITEM] == DriverStatus.HELD
        assert ticks[0].held == [ITEM]
        assert all(tick.remediating == [] for tick in ticks)
        remediator.assert_called_once()
        dispatch.assert_called_once()
        materialize.assert_not_called()
        credential_preflight.assert_not_called()
        launch.assert_not_called()
        assert store.list_epic_reservations(REVIEW_EPIC_REF) == []

        # The CLI uses this store operation to release an operator hold.
        assert store.remove_epic_hold(EPIC, ITEM) is not None
        statuses[ITEM] = DriverStatus.REMEDIATING
        drive_review_stage(
            statuses,
            {ITEM: facts},
            epic_ref=EPIC,
            config=config,
            store=store,
            repo="o/r",
            repo_root=tmp_path,
            base="main",
            reviewer_launcher=None,
            remediator=remediator,
            now=NOW,
        )
        assert dispatch.call_count == 2
        assert len(store.list_runs_by_branch(checkout.branch)) == 2
        assert statuses[ITEM] == DriverStatus.HELD
        materialize.assert_not_called()


def test_refused_remediation_profile_holds_production_remediation_across_ticks(
    tmp_path: Path,
) -> None:
    """An active remediation profile the launch refuses (#3023) holds the node
    on that category with the launch's reason, and the next tick starts no
    second remediation."""
    from support.prompt_profiles import activate_unchecked

    from kew.graph.driver.review_launcher import sandbox_remediator
    from kew.prompt_addendum import PROMPT_CONFIGURATION_REFUSAL
    from kew.remediation_containment import RemediationContainment
    from kew.review_checkout import ReviewCheckout
    from kew.runners.claude_code import ClaudeCodeRunner
    from kew.runners.registry import ResolvedRoute

    head = "a" * 40
    config = _config(tmp_path, blocking=True)
    config.branches.worktree_base = str(tmp_path / "checkouts")
    sidecar = tmp_path / "findings.json"
    sidecar.write_text(
        json.dumps(
            {
                "verdict": "NEEDS_CHANGES",
                "findings": [
                    {
                        "severity": "high",
                        "location": "code.py:1",
                        "summary": "unchecked path",
                        "evidence": _evidence(),
                    }
                ],
            }
        )
    )
    checkout = ReviewCheckout(
        tmp_path / "checkout", head, "agent/issue-42", False, None, None
    )
    route = ResolvedRoute(ClaudeCodeRunner(), "claude-opus-5")
    pr_view = json.dumps(
        {
            "number": 7,
            "headRefName": checkout.branch,
            "headRefOid": head,
            "baseRefName": "main",
            "body": "Closes #42",
            "title": "Fix it",
        }
    )

    def gh(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        """The ``gh`` process boundary: the PR the remediation is cut from."""
        out = pr_view if cmd[:3] == ["gh", "pr", "view"] else ""
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    with (
        SqliteRunStore(tmp_path / "d.db") as store,
        patch("kew.review.subprocess_run", side_effect=gh),
        patch("kew.review._check_github_connectivity", return_value=True),
        patch("kew.runners.resolve_runner", return_value=route),
        patch("kew.review.resolve_runner", return_value=route),
        patch(
            "kew.review.resolve_remediation_containment",
            return_value=RemediationContainment(posture="sandboxed", image="kew-agent"),
        ),
        patch("kew.review.attest_flow_posture"),
        patch("kew.review.materialize_review_checkout", return_value=checkout),
        patch("kew.review.run_tracked_agent") as launch,
    ):
        activate_unchecked(
            store, "Keep it small.", "review_remediation", body_sha256="0" * 64
        )
        store.record_review_round(
            EPIC,
            ITEM,
            head_sha=head,
            verdict="NEEDS_CHANGES",
            sidecar_digest="d",
            sidecar_path=str(sidecar),
        )
        remediator = Mock(wraps=sandbox_remediator(config, store, epic_ref=EPIC))
        facts = _facts(head)
        ticks = []
        for _ in range(2):
            holds = store.list_epic_holds(EPIC)
            statuses = {
                ITEM: overlay_status(
                    facts,
                    store.list_runs_by_branch(checkout.branch),
                    reserved=False,
                    held_reason=holds[0].reason if holds else None,
                    review=ReviewOverlay(
                        enabled=True,
                        round_count=1,
                        latest_verdict="NEEDS_CHANGES",
                        latest_head_sha=head,
                        current_head_sha=head,
                    ),
                )
            }
            ticks.append(
                drive_review_stage(
                    statuses,
                    {ITEM: facts},
                    epic_ref=EPIC,
                    config=config,
                    store=store,
                    repo="o/r",
                    repo_root=tmp_path,
                    base="main",
                    reviewer_launcher=None,
                    remediator=remediator,
                    now=NOW,
                )
            )

        [refusal] = store.list_runs_by_branch(checkout.branch)
        assert refusal.status == "refused"
        assert refusal.error_category == PROMPT_CONFIGURATION_REFUSAL
        [hold] = store.list_epic_holds(EPIC)
        assert hold.reason.startswith("admission_refused: ")
        assert PROMPT_CONFIGURATION_REFUSAL in hold.reason
        assert "does not match the approved digest" in hold.reason
        assert statuses[ITEM] == DriverStatus.HELD
        assert ticks[0].held == [ITEM]
        assert all(tick.remediating == [] for tick in ticks)
        remediator.assert_called_once()
        launch.assert_not_called()


def test_other_remediation_failures_remain_retryable(tmp_path: Path) -> None:
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path, blocking=True, max_rounds=2)
    facts = _facts(head)
    statuses = {ITEM: DriverStatus.REMEDIATING}

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        store.record_review_round(
            EPIC, ITEM, head_sha=head, verdict="NEEDS_CHANGES", sidecar_digest="d"
        )

        def fail(ctx: RemediationContext) -> None:
            record_review_outcome(
                ReviewOutcome(
                    pr_number=ctx.pr_number or 0,
                    status=STATUS_FAILED,
                    stage=STAGE_PRE_LAUNCH,
                    classification="runner_preflight",
                    detail="runner unavailable",
                    head_sha=ctx.head_sha,
                    item_id=ctx.item_id,
                ),
                store=store,
                config=config,
                notify=False,
            )

        result = drive_review_stage(
            statuses,
            {ITEM: facts},
            epic_ref=EPIC,
            config=config,
            store=store,
            repo="o/r",
            repo_root=repo,
            base="main",
            reviewer_launcher=_ready_launcher,
            remediator=fail,
            now=NOW,
        )

        assert store.list_epic_holds(EPIC) == []

    assert result.held == []
    assert result.remediating == [ITEM]
    assert statuses[ITEM] == DriverStatus.REMEDIATING


def test_needs_changes_records_sidecar_path_and_feeds_it_to_remediator(
    tmp_path: Path,
):
    """#1030: the round records WHERE its sidecar is stored, and the remediation
    leg receives that path so it can feed the host-stored findings (not PR
    comments) as input. The sidecar file really holds the evidenced finding."""
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path, blocking=True, max_rounds=2)
    facts = _facts(head)

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        # Round 1: a real reviewer round records the NEEDS_CHANGES verdict AND the
        # host path of its collected sidecar.
        drive_review_stage(
            {ITEM: DriverStatus.ADVERSARIAL_REVIEW},
            {ITEM: facts},
            epic_ref=EPIC,
            config=config,
            store=store,
            repo="o/r",
            repo_root=repo,
            base="main",
            reviewer_launcher=_needs_changes_launcher,
            now=NOW,
        )
        latest = store.latest_review_round(ITEM)
        assert latest is not None and latest.verdict == "NEEDS_CHANGES"
        assert latest.sidecar_path is not None
        sidecar_file = Path(latest.sidecar_path)
        assert sidecar_file.is_file()
        parsed = parse_verdict_sidecar(sidecar_file.read_bytes())
        assert parsed.findings and parsed.findings[0].location == "code.py:1"

        # The node is now REMEDIATING: the remediator receives the recorded path.
        calls: list[RemediationContext] = []
        drive_review_stage(
            {ITEM: DriverStatus.REMEDIATING},
            {ITEM: facts},
            epic_ref=EPIC,
            config=config,
            store=store,
            repo="o/r",
            repo_root=repo,
            base="main",
            reviewer_launcher=_ready_launcher,
            remediator=calls.append,
            now=NOW,
        )
        assert len(calls) == 1
        assert calls[0].sidecar_path == latest.sidecar_path
        assert calls[0].pr_number == 7
        assert calls[0].head_sha == head


def test_remediation_push_re_reviews_at_round_two_on_new_head(tmp_path: Path):
    """#1030 Done-When #2: once remediation lands a new commit, the driver detects
    the moved head and re-enters adversarial_review at round N+1, recording the
    new head — the leg the manual bridge had to perform by hand."""
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path, blocking=True, max_rounds=3)

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        # Round 1 at the original head: NEEDS_CHANGES.
        drive_review_stage(
            {ITEM: DriverStatus.ADVERSARIAL_REVIEW},
            {ITEM: _facts(head)},
            epic_ref=EPIC,
            config=config,
            store=store,
            repo="o/r",
            repo_root=repo,
            base="main",
            reviewer_launcher=_needs_changes_launcher,
            now=NOW,
        )
        assert store.review_round_count(ITEM) == 1

        # Remediation lands a new commit → the PR head moves.
        (repo / "code.py").write_text("x = 2\n")
        run(["git", "add", "-A"], cwd=repo, check=True)
        run(["git", "commit", "-q", "-m", "remediate"], cwd=repo, check=True)
        new_head = run(
            ["git", "rev-parse", "HEAD"], cwd=repo, check=True
        ).stdout.strip()
        assert new_head != head

        # At the moved head the standing NEEDS_CHANGES no longer binds the current
        # head, so a fresh reviewer round is due — round 2, bound to the new head.
        drive_review_stage(
            {ITEM: DriverStatus.REMEDIATING},
            {ITEM: _facts(new_head)},
            epic_ref=EPIC,
            config=config,
            store=store,
            repo="o/r",
            repo_root=repo,
            base="main",
            reviewer_launcher=_ready_launcher,
            remediator=lambda ctx: None,
            now=NOW,
        )
        assert store.review_round_count(ITEM) == 2
        latest = store.latest_review_round(ITEM)
        assert latest is not None
        assert latest.round == 2
        assert latest.head_sha == new_head
        assert latest.verdict == "READY"


def test_a_merge_queue_round_does_not_spend_the_remediation_budget(tmp_path: Path):
    """#2148: the merge queue records its red-combined-tree refusal as a round.

    With the default ``max_rounds = 2`` the reviewer's READY round plus that
    refusal exhausted the budget, so the node converged to
    ``review_rounds_exhausted`` instead of ever remediating the failure the queue
    recorded the round to trigger. ``max_rounds`` caps remediation cycles, and a
    merge-queue round is not one: no reviewer ran, and the round IS the finding
    remediation must answer.
    """
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path, blocking=True, max_rounds=2)
    statuses = {ITEM: DriverStatus.REMEDIATING}
    calls: list[RemediationContext] = []

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        store.record_review_round(
            EPIC, ITEM, head_sha=head, verdict="READY", review_mode="full"
        )
        store.record_review_round(
            EPIC,
            ITEM,
            head_sha=head,
            verdict="NEEDS_CHANGES",
            sidecar_digest="d",
            review_mode=MERGE_VERIFY_REVIEW_MODE,
        )
        out = drive_review_stage(
            statuses,
            {ITEM: _facts(head)},
            epic_ref=EPIC,
            config=config,
            store=store,
            repo="o/r",
            repo_root=repo,
            base="main",
            reviewer_launcher=_ready_launcher,
            remediator=calls.append,
            now=NOW,
        )

        assert store.review_round_count(ITEM) == 2  # both rounds are on record
        assert out.remediating == [ITEM]
        assert out.held == []
        assert len(calls) == 1
        assert store.list_epic_holds(EPIC) == []

    # One real remediation cycle still exhausts the budget of 2 — the cap is
    # narrowed, not removed.
    with SqliteRunStore(str(tmp_path / "e.db")) as store:
        for mode in ("full", "delta"):
            store.record_review_round(
                EPIC, ITEM, head_sha=head, verdict="NEEDS_CHANGES", review_mode=mode
            )
        store.record_review_round(
            EPIC,
            ITEM,
            head_sha=head,
            verdict="NEEDS_CHANGES",
            sidecar_digest="d",
            review_mode=MERGE_VERIFY_REVIEW_MODE,
        )
        out = drive_review_stage(
            {ITEM: DriverStatus.REMEDIATING},
            {ITEM: _facts(head)},
            epic_ref=EPIC,
            config=config,
            store=store,
            repo="o/r",
            repo_root=repo,
            base="main",
            reviewer_launcher=_ready_launcher,
            remediator=calls.append,
            now=NOW,
        )
        assert out.held == [ITEM]
        assert [h.reason for h in store.list_epic_holds(EPIC)] == [
            "review_rounds_exhausted"
        ]


def test_round_cap_holds_needs_human_in_blocking(tmp_path: Path):
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path, blocking=True, max_rounds=1)
    facts = _facts(head)
    statuses = {ITEM: DriverStatus.REMEDIATING}

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        store.record_review_round(
            EPIC, ITEM, head_sha=head, verdict="NEEDS_CHANGES", sidecar_digest="d"
        )
        out = drive_review_stage(
            statuses,
            {ITEM: facts},
            epic_ref=EPIC,
            config=config,
            store=store,
            repo="o/r",
            repo_root=repo,
            base="main",
            reviewer_launcher=_ready_launcher,
            remediator=lambda ctx: None,
            now=NOW,
        )
        # At the cap: HELD (never a silent remediation loop), and the merge pass
        # sees it as HELD this very tick (statuses mutated in place).
        assert out.held == [ITEM]
        assert statuses[ITEM] == DriverStatus.HELD


def test_round_cap_release_gets_a_fresh_reviewer_not_the_same_hold(tmp_path: Path):
    """#2154: `kew epic unhold` is the whole recovery from a round-cap hold.

    Retry refuses a held node, so the marker that restarts round numbering can
    only be written by the release. Without that, this second pass finds the
    same standing NEEDS_CHANGES verdict against the same head with the count
    still at the cap and re-HOLDs — the node's only way out would be landing a
    retry inside the window between the unhold and this pass.
    """
    from kew.epic_holds import REVIEW_CYCLE_RESET_HOLD_REASONS

    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path, blocking=True, max_rounds=1)
    statuses = {ITEM: DriverStatus.REMEDIATING}

    def _drive(store):
        return drive_review_stage(
            statuses,
            {ITEM: _facts(head)},
            epic_ref=EPIC,
            config=config,
            store=store,
            repo="o/r",
            repo_root=repo,
            base="main",
            reviewer_launcher=_ready_launcher,
            remediator=lambda ctx: None,
            now=NOW,
        )

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        store.record_review_round(
            EPIC, ITEM, head_sha=head, verdict="NEEDS_CHANGES", sidecar_digest="d"
        )
        assert _drive(store).held == [ITEM]

        # This is the store operation `kew epic unhold` performs.
        removed = store.remove_epic_hold(
            EPIC, ITEM, fresh_review_cycle_reasons=REVIEW_CYCLE_RESET_HOLD_REASONS
        )
        assert removed is not None and removed.reason == "review_rounds_exhausted"
        statuses[ITEM] = DriverStatus.ADVERSARIAL_REVIEW

        out = _drive(store)

        assert out.held == [], "the release must not be re-held on the old count"
        assert store.list_epic_holds(EPIC) == []
        # A real reviewer round ran on the fresh allowance, numbered from 1.
        latest = store.latest_review_round(ITEM)
        assert latest is not None
        assert (latest.round, latest.verdict) == (1, "READY")
        assert [r.round for r in store.list_review_rounds(ITEM)] == [1, 1]


def test_round_cap_advisory_attests_degraded_as_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path, blocking=False, max_rounds=1)
    statuses = {ITEM: DriverStatus.REMEDIATING}
    published: list[tuple[str, str]] = []
    monkeypatch.setattr(
        review_stage.review_status,
        "publish_advisory_degraded",
        lambda published_repo, published_head: published.append(
            (published_repo, published_head)
        ),
    )

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        store.record_review_round(
            EPIC, ITEM, head_sha=head, verdict="NEEDS_CHANGES", sidecar_digest="d"
        )
        out = drive_review_stage(
            statuses,
            {ITEM: _facts(head)},
            epic_ref=EPIC,
            config=config,
            store=store,
            repo="o/r",
            repo_root=repo,
            base="main",
            reviewer_launcher=_ready_launcher,
            remediator=lambda ctx: None,
            now=NOW,
        )

        assert out.verdicts == {ITEM: VERDICT_DEGRADED}
        assert out.held == []
        assert store.list_epic_holds(EPIC) == []
        latest = store.latest_review_round(ITEM)
        assert latest is not None and latest.verdict == VERDICT_DEGRADED
        assert verify_chain(store.read_chain_rows()).ok
        assert published == [("o/r", head)]


def test_unparseable_blocking_holds_never_ready(tmp_path: Path):
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path, blocking=True)
    facts = _facts(head)
    statuses = {ITEM: DriverStatus.ADVERSARIAL_REVIEW}

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        out = drive_review_stage(
            statuses,
            {ITEM: facts},
            epic_ref=EPIC,
            config=config,
            store=store,
            repo="o/r",
            repo_root=repo,
            base="main",
            reviewer_launcher=_crash_launcher,
            now=NOW,
        )
        assert out.held == [ITEM]
        assert statuses[ITEM] == DriverStatus.HELD
        # No verdict was guessed into a round.
        assert store.latest_review_round(ITEM) is None
        assert store.list_epic_reservations(REVIEW_EPIC_REF) == []


def _refused_launcher(ctx: ReviewerLaunchContext) -> CollectedSidecar:
    """The sandbox refused the reviewer BEFORE launch (#2147): the pinned image
    was momentarily absent from the local container runtime, so no container
    ever ran and no review happened."""
    from kew.review_contract import infrastructure_failure_sidecar

    return infrastructure_failure_sidecar(
        ctx.log_dir,
        ctx.run_id,
        failure_layer="image",
        error_category="sandbox_refused",
        reason=(
            "image kew-agent:docker-d148816b388f is not present in the local "
            "container runtime"
        ),
    )


def _drive_refused(statuses, store, config, repo, head):
    return drive_review_stage(
        statuses,
        {ITEM: _facts(head)},
        epic_ref=EPIC,
        config=config,
        store=store,
        repo="o/r",
        repo_root=repo,
        base="main",
        reviewer_launcher=_refused_launcher,
        now=NOW,
    )


def test_pre_launch_sandbox_refusal_leaves_the_node_ready_for_re_review(
    tmp_path: Path,
) -> None:
    """#2147: a review the sandbox refused before launch is a transient infra
    failure, not a verdict. It must NOT hold the node as review_unparseable —
    the incident held #2085 over an image that was present again a minute later
    and stalled four downstream nodes until an operator ran `kew epic retry`."""
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path, blocking=True)  # blocking → the incident's mode
    statuses = {ITEM: DriverStatus.ADVERSARIAL_REVIEW}

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        out = _drive_refused(statuses, store, config, repo, head)

        # Not held, no verdict guessed, node still due a reviewer next tick.
        assert out.held == []
        assert store.list_epic_holds(EPIC) == []
        assert store.latest_review_round(ITEM) is None
        assert statuses[ITEM] == DriverStatus.ADVERSARIAL_REVIEW
        # Its own waiting reason names the refusal — never "unparseable".
        assert ITEM in out.refused
        # A wait that names the refusal and no person (#3186): the retry is
        # kew's own, so it raises no attention until its bound is spent.
        assert out.refused[ITEM].startswith("review_retrying — review_sandbox_refused")
        assert classify_wait(out.refused[ITEM], held=False) is None
        assert "unparseable" not in out.refused[ITEM]
        # The reservation is released, so the next tick can reserve the node.
        assert store.list_epic_reservations(REVIEW_EPIC_REF) == []
        # The refusal left a terminal review row — the bound's only evidence.
        refusals = [
            r for r in store.list_by_item(ITEM) if r.error_category == "sandbox_refused"
        ]
        assert len(refusals) == 1


def _drive(statuses, store, config, repo, head, launcher, *, now=NOW):
    return drive_review_stage(
        statuses,
        {ITEM: _facts(head)},
        epic_ref=EPIC,
        config=config,
        store=store,
        repo="o/r",
        repo_root=repo,
        base="main",
        reviewer_launcher=launcher,
        now=now,
    )


def test_a_rate_limited_reviewer_retries_instead_of_holding(tmp_path: Path) -> None:
    """#2219: a provider usage limit ended the reviewer 49 s in — three
    ``rate_limit_event`` notices, then an error result with no verdict block.
    Recorded as UNPARSEABLE it held the incident's node needs-human for 30 h over
    a limit that reset by itself. It is infrastructure: no hold, no round, and
    the round runs again once the limit has had time to reset."""
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path, blocking=True)  # blocking → the incident's mode
    statuses = {ITEM: DriverStatus.ADVERSARIAL_REVIEW}
    launched: list[str] = []
    # The wait times from the run row's own ``finished_at``, which the store
    # stamps with the real clock, so these ticks run on real-clock times.
    start = datetime.now(UTC)

    def _limited(ctx: ReviewerLaunchContext) -> CollectedSidecar:
        launched.append(ctx.run_id)
        return stream_rate_limited(ctx)

    def _ready(ctx: ReviewerLaunchContext) -> CollectedSidecar:
        launched.append(ctx.run_id)
        return _ready_launcher(ctx)

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        out = _drive(statuses, store, config, repo, head, _limited, now=start)

        # Not held, no verdict guessed, no round spent against the cycle.
        assert out.held == []
        assert store.list_epic_holds(EPIC) == []
        assert store.latest_review_round(ITEM) is None
        assert store.review_round_count(ITEM) == 0
        assert statuses[ITEM] == DriverStatus.ADVERSARIAL_REVIEW
        # The wait names the limit — never "unparseable".
        assert out.refused[ITEM].startswith("review_rate_limited")
        assert "unparseable" not in out.refused[ITEM]
        # The terminal row carries the category the wait times from.
        limited_runs = [
            r for r in store.list_by_item(ITEM) if r.error_category == "rate_limited"
        ]
        assert len(limited_runs) == 1
        # The reservation is released, so a later tick can reserve the node.
        assert store.list_epic_reservations(REVIEW_EPIC_REF) == []
        assert verify_chain(store.read_chain_rows()).ok
        assert len(launched) == 1

        # Still inside the wait: no second container, still only the wait.
        out = _drive(
            {ITEM: DriverStatus.ADVERSARIAL_REVIEW},
            store,
            config,
            repo,
            head,
            _limited,
            now=start + timedelta(minutes=5),
        )
        assert len(launched) == 1, "a live usage limit must not be re-launched into"
        assert out.held == []
        assert out.refused[ITEM].startswith("review_rate_limited")

        # The limit has reset: the node is re-reviewed and the round lands.
        statuses = {ITEM: DriverStatus.ADVERSARIAL_REVIEW}
        out = _drive(
            statuses,
            store,
            config,
            repo,
            head,
            _ready,
            now=start + timedelta(minutes=20),
        )
        assert len(launched) == 2, "the retry must run the round the limit ended"
        assert out.held == []
        assert out.refused == {}
        latest = store.latest_review_round(ITEM)
        assert latest is not None and latest.verdict == "READY"
        assert verify_chain(store.read_chain_rows()).ok


def test_a_clean_reviewer_finish_after_a_rate_limit_notice_still_holds(
    tmp_path: Path,
) -> None:
    """#2219 boundary: the retry needs BOTH halves — a limit notice AND an error
    result. A reviewer that survived a limit warning and then finished cleanly
    without a verdict block wrote a bad verdict, which is a human's to read."""
    from support.review_stream import capture_of, rate_limited_stream, result_line

    from kew.review_contract import collect_stream_verdict

    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path, blocking=True)
    statuses = {ITEM: DriverStatus.ADVERSARIAL_REVIEW}

    def _warned_then_clean(ctx: ReviewerLaunchContext) -> CollectedSidecar:
        lines = [*rate_limited_stream()[:-1], result_line("no verdict here")]
        return collect_stream_verdict(
            capture_of(lines),
            head_sha=ctx.head_sha,
            exit_code=0,
            dest_dir=ctx.log_dir,
            run_id=ctx.run_id,
        )

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        out = _drive(statuses, store, config, repo, head, _warned_then_clean)

        assert out.held == [ITEM]
        assert statuses[ITEM] == DriverStatus.HELD
        (hold,) = store.list_epic_holds(EPIC)
        # The hold names what was wrong with the verdict (#3186).
        assert hold.reason == (
            "review_unparseable: the reviewer's final result carries no verdict block"
        )
        assert out.refused == {}
        assert store.latest_review_round(ITEM) is None


def test_a_round_that_only_saw_a_benign_limit_notice_still_holds(
    tmp_path: Path,
) -> None:
    """#2219 boundary: the runner emits ``rate_limit_event`` whenever its
    rate-limit info CHANGES, so ``allowed``/``allowed_warning`` notices are
    routine traffic of a run nowhere near a limit. Counting those turned every
    reviewer round that failed for its own reasons into a provider usage limit:
    the ``review_unparseable`` hold a human reads became 6 h of silent
    15-minute relaunches ending in ``review_rate_limit_exhausted``, which tells
    the operator to raise a limit that was never exceeded."""
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path, blocking=True)
    statuses = {ITEM: DriverStatus.ADVERSARIAL_REVIEW}
    launched: list[str] = []

    def _warned_then_failed(ctx: ReviewerLaunchContext) -> CollectedSidecar:
        launched.append(ctx.run_id)
        return stream_rate_limited(ctx, status="allowed_warning")

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        out = _drive(statuses, store, config, repo, head, _warned_then_failed)

        assert out.held == [ITEM]
        assert statuses[ITEM] == DriverStatus.HELD
        (hold,) = store.list_epic_holds(EPIC)
        # A failed run holds under its own name, with its exit (#3174).
        assert hold.reason.startswith("review_failed_no_verdict: reviewer exited")
        assert out.refused == {}
        assert [
            r for r in store.list_by_item(ITEM) if r.error_category == "rate_limited"
        ] == []
        assert len(launched) == 1


def test_a_rate_limit_that_killed_the_run_mid_stream_retries_too(
    tmp_path: Path,
) -> None:
    """#2219: the limit does not always get to write a result event. A reviewer
    the limit killed mid-stream leaves limit notices and NOTHING else, so the
    capture never recorded ``is_error``; read as a clean finish, that absence
    landed on the ``review_unparseable`` needs-human hold this path removes."""
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path, blocking=True)
    statuses = {ITEM: DriverStatus.ADVERSARIAL_REVIEW}

    def _killed(ctx: ReviewerLaunchContext) -> CollectedSidecar:
        return stream_rate_limited(ctx, killed=True)

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        out = _drive(
            statuses, store, config, repo, head, _killed, now=datetime.now(UTC)
        )

        assert out.held == []
        assert store.list_epic_holds(EPIC) == []
        assert statuses[ITEM] == DriverStatus.ADVERSARIAL_REVIEW
        assert store.review_round_count(ITEM) == 0
        assert out.refused[ITEM].startswith("review_rate_limited")
        assert "unparseable" not in out.refused[ITEM]


def test_the_rate_limit_wait_reason_never_changes_during_one_wait(
    tmp_path: Path,
) -> None:
    """#2219: the waiting reason is CONSTANT for the life of one wait.

    The reason is part of what makes an operator-attention event a distinct
    state (``AttentionEvent.state_key``), so a reason that counted the minutes
    down made every poll a new state: ``kew epic watch --follow`` emitted one
    held event per minute of a single benign 15-minute wait.
    """
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path, blocking=True)
    start = datetime.now(UTC)

    def _limited(ctx: ReviewerLaunchContext) -> CollectedSidecar:
        return stream_rate_limited(ctx)

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        first = _drive(
            {ITEM: DriverStatus.ADVERSARIAL_REVIEW},
            store,
            config,
            repo,
            head,
            _limited,
            now=start,
        ).refused[ITEM]
        polled = {
            _drive(
                {ITEM: DriverStatus.ADVERSARIAL_REVIEW},
                store,
                config,
                repo,
                head,
                _limited,
                now=start + timedelta(minutes=minute),
            ).refused[ITEM]
            for minute in range(1, 15)
        }

        assert polled == {first}, "the wait must read as ONE state, not fifteen"


def _backdate_rate_limited_runs(store, item_id: str, *, by: timedelta) -> None:
    """Age every usage-limited row of *item_id* by *by*.

    The bound is measured from the node's own run rows, so this is how a test
    reaches a six-hour limit without six hours of ticks.
    """
    for record in store.list_by_item(item_id):
        if record.error_category != "rate_limited":
            continue
        record.started_at -= by
        if record.finished_at is not None:
            record.finished_at -= by
        store.save(record)


def _seed_rate_limited_retries(
    store, *, since: datetime, until: datetime, every: timedelta | None = None
) -> None:
    """Fill ``since``..``until`` with limited rounds, one ``every`` interval apart.

    The bound is a wall-clock span over the node's limited rows, and a limit that
    really is not resetting leaves ~24 of them one retry wait apart. Two rows
    seven hours apart are a DIFFERENT fact — a driver that was not running in
    between — so a test that only aged one row could not tell the bound from the
    gap ``_RATE_LIMIT_STREAK_GAP`` splits an incident on.
    """
    step = every or review_stage._RATE_LIMIT_RETRY_WAIT
    at = since
    index = 0
    while at <= until:
        store.save(
            RunRecord(
                run_id=f"rev-limited-{index}",
                issue_num=int(ITEM),
                item_id=ITEM,
                branch="kew/42",
                status="failed",
                exit_code=1,
                started_at=at,
                finished_at=at + timedelta(minutes=1),
                run_kind=ADVERSARIAL_REVIEW_RUN_KIND,
                error_category="rate_limited",
            )
        )
        at += step
        index += 1


def test_a_usage_limit_that_never_resets_is_held_for_a_person(
    tmp_path: Path,
) -> None:
    """#2219: the retry has to be BOUNDED, and this is the only thing that
    bounds it.

    A node waiting in ADVERSARIAL_REVIEW carries no hold row, and every stall
    clock runs off a hold row (``tick._held_signal``) or an open IN_REVIEW PR
    (``tick._stale_review_stuck``) — so nothing could ever reach an unbounded
    wait: a persistent or planted usage limit relaunched a reviewer container
    every 15 minutes forever while the epic read PROGRESSING off the node's own
    review status and ``kew epic run`` never exited. Past
    ``_MAX_RATE_LIMIT_WAIT`` the node is HELD under its own reason, which is a
    hold row with a stall clock and an operator action on it.
    """
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path, blocking=True)
    start = datetime.now(UTC)
    launched: list[str] = []

    def _limited(ctx: ReviewerLaunchContext) -> CollectedSidecar:
        launched.append(ctx.run_id)
        return stream_rate_limited(ctx)

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        out = _drive(
            {ITEM: DriverStatus.ADVERSARIAL_REVIEW},
            store,
            config,
            repo,
            head,
            _limited,
            now=start,
        )
        assert out.held == [], "the first limited round is still a plain wait"

        # That round, and every retry kew has made since, has been ended by the
        # limit at the retry cadence for longer than any provider window takes
        # to reset — the history a limit that is NOT resetting leaves behind.
        aged = review_stage._MAX_RATE_LIMIT_WAIT + timedelta(hours=1)
        _backdate_rate_limited_runs(store, ITEM, by=aged)
        _seed_rate_limited_retries(
            store, since=start - aged, until=start - timedelta(minutes=20)
        )
        statuses = {ITEM: DriverStatus.ADVERSARIAL_REVIEW}
        out = _drive(statuses, store, config, repo, head, _limited, now=start)

        assert len(launched) == 2
        assert out.held == [ITEM]
        assert out.refused == {}
        assert statuses[ITEM] == DriverStatus.HELD
        (hold,) = store.list_epic_holds(EPIC)
        # Its own reason, never review_unparseable, and an honored hold — which
        # is what gives it a stall clock and keeps the node out of the frontier.
        assert hold.reason.startswith("review_rate_limit_exhausted")
        assert "unparseable" not in hold.reason
        assert holds_node_out_of_frontier(hold.reason)
        assert hold.reason == safe_hold_reason(hold.reason), "fits an echoed line"
        # Still no verdict guessed and no round spent against the cycle.
        assert store.latest_review_round(ITEM) is None
        assert store.review_round_count(ITEM) == 0
        assert verify_chain(store.read_chain_rows()).ok


def test_a_review_that_ran_resets_the_rate_limit_bound(tmp_path: Path) -> None:
    """#2219: the bound spends per limit incident, not once per node lifetime —
    a round that got past the provider ends the streak, so a later unrelated
    limit is waited out again instead of holding immediately. Without this the
    bound would eventually reach the incident the issue is about: a limit that
    DOES reset, just not before some ancient limited row aged out."""
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path, blocking=True)
    start = datetime.now(UTC)

    def _limited(ctx: ReviewerLaunchContext) -> CollectedSidecar:
        return stream_rate_limited(ctx)

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        # An incident that ran past the bound, at the retry cadence, and ended
        # less than one streak gap ago — so the ONLY thing that can split it off
        # the fresh limit below is the round that reached the provider.
        _seed_rate_limited_retries(
            store,
            since=start - review_stage._MAX_RATE_LIMIT_WAIT - timedelta(hours=1),
            until=start - timedelta(minutes=30),
        )
        store.save(
            RunRecord(
                run_id="rev-ran",
                issue_num=int(ITEM),
                item_id=ITEM,
                branch="kew/42",
                status="success",
                exit_code=0,
                started_at=start - timedelta(minutes=25),
                finished_at=start - timedelta(minutes=25),
                run_kind=ADVERSARIAL_REVIEW_RUN_KIND,
            )
        )

        # A fresh limit incident, one round long: still a wait, never a hold.
        statuses = {ITEM: DriverStatus.ADVERSARIAL_REVIEW}
        out = _drive(statuses, store, config, repo, head, _limited, now=start)

        assert out.held == []
        assert store.list_epic_holds(EPIC) == []
        assert statuses[ITEM] == DriverStatus.ADVERSARIAL_REVIEW
        assert out.refused[ITEM].startswith("review_rate_limited")


def test_driver_downtime_between_two_limits_does_not_hold_the_second(
    tmp_path: Path,
) -> None:
    """#2219 regression: the six-hour bound is a span over the node's limited
    rows, and nothing writes a row while the driver is not running — so an
    operator who stopped the host after one limited round and started it again
    the next morning had the FIRST round of a fresh transient limit read as a
    seven-hour incident. The node was HELD needs-human under
    ``review_rate_limit_exhausted``, whose remedy is to raise a provider limit
    that had never once been waited out. A gap no retry cadence could leave ends
    the incident, exactly as a round that reached the provider does.
    """
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path, blocking=True)
    start = datetime.now(UTC)

    def _limited(ctx: ReviewerLaunchContext) -> CollectedSidecar:
        return stream_rate_limited(ctx)

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        out = _drive(
            {ITEM: DriverStatus.ADVERSARIAL_REVIEW},
            store,
            config,
            repo,
            head,
            _limited,
            now=start,
        )
        assert out.refused[ITEM].startswith("review_rate_limited")

        # The host was then off for longer than the bound: that one round is the
        # whole history, with no round of any kind after it.
        _backdate_rate_limited_runs(
            store, ITEM, by=review_stage._MAX_RATE_LIMIT_WAIT + timedelta(hours=1)
        )
        statuses = {ITEM: DriverStatus.ADVERSARIAL_REVIEW}
        out = _drive(statuses, store, config, repo, head, _limited, now=start)

        # A fresh limit, one round old: waited out, never held.
        assert out.held == []
        assert store.list_epic_holds(EPIC) == []
        assert statuses[ITEM] == DriverStatus.ADVERSARIAL_REVIEW
        assert out.refused[ITEM].startswith("review_rate_limited")
        assert "exhausted" not in out.refused[ITEM]
        assert store.review_round_count(ITEM) == 0


def test_the_rate_limit_bound_survives_a_late_tick_within_one_incident(
    tmp_path: Path,
) -> None:
    """#2219 boundary on the gap above: the gap must forgive a driver that was
    NOT running, never a slow one. Rounds spaced by the retry wait plus a late
    tick are one incident, so a limit rejecting rounds on that cadence for six
    hours still reaches the hold that makes the wait visible to a stall clock.
    """
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path, blocking=True)
    start = datetime.now(UTC)
    late = review_stage._RATE_LIMIT_RETRY_WAIT + timedelta(minutes=30)
    assert late < review_stage._RATE_LIMIT_STREAK_GAP, "a late tick, not downtime"

    def _limited(ctx: ReviewerLaunchContext) -> CollectedSidecar:
        return stream_rate_limited(ctx)

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _seed_rate_limited_retries(
            store,
            since=start - review_stage._MAX_RATE_LIMIT_WAIT - timedelta(hours=1),
            until=start - timedelta(minutes=20),
            every=late,
        )

        statuses = {ITEM: DriverStatus.ADVERSARIAL_REVIEW}
        out = _drive(statuses, store, config, repo, head, _limited, now=start)

        assert out.held == [ITEM]
        (hold,) = store.list_epic_holds(EPIC)
        assert hold.reason.startswith("review_rate_limit_exhausted")


def _seed_slow_cadence_limits(store, *, rounds: int, now: datetime) -> None:
    """Seed *rounds* limited rounds at a cadence WIDER than one streak gap.

    The spacing is what `kew epic advance` from cron (or `kew epic run
    --interval 7200`) leaves behind: every pair of rounds is further apart than
    any live retry cadence could be, so each one reads as its own incident. The
    newest round is older than the retry wait, so the next tick reviews rather
    than defers.
    """
    _seed_rate_limited_retries(
        store,
        since=now - timedelta(minutes=30) - (rounds - 1) * timedelta(hours=2),
        until=now - timedelta(minutes=30),
        every=timedelta(hours=2),
    )


def test_a_slow_driver_cadence_still_reaches_the_rate_limit_bound(
    tmp_path: Path,
) -> None:
    """#2231 review: the bound must be reachable at ANY supported cadence.

    ``_RATE_LIMIT_STREAK_GAP`` is a fixed hour, derived from the default 60s
    poll. A host driven from cron, or with ``kew epic run --interval`` above it,
    spaces every pair of limited rounds wider than that gap — so the wall-clock
    span read off the streak is a permanent zero and
    ``_MAX_RATE_LIMIT_WAIT``, the ONLY bound that ever reaches a person for a
    node waiting in ADVERSARIAL_REVIEW, could never be spent: the unbounded
    relaunch loop #2219 exists to stop, with no hold at the end of it. The round
    COUNT is spent instead, and no cadence can dilute a count.
    """
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path, blocking=True)
    start = datetime.now(UTC)

    def _limited(ctx: ReviewerLaunchContext) -> CollectedSidecar:
        return stream_rate_limited(ctx)

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _seed_slow_cadence_limits(
            store, rounds=review_stage._MAX_RATE_LIMIT_ROUNDS - 1, now=start
        )
        statuses = {ITEM: DriverStatus.ADVERSARIAL_REVIEW}
        out = _drive(statuses, store, config, repo, head, _limited, now=start)

        # The round just run spends the count — while the span bound still sees
        # only this round and the one before it, which is the whole point of
        # this regression: 25 rejections, and never six hours of them to read.
        streak = review_stage._rate_limited_streak(store, ITEM)
        assert len(streak) <= 2, "a cadence this slow splits every pair of rounds"
        assert (
            review_stage._rate_limited_span(streak) < review_stage._MAX_RATE_LIMIT_WAIT
        ), "the span bound cannot fire at this cadence"

        assert out.held == [ITEM]
        assert out.refused == {}
        assert statuses[ITEM] == DriverStatus.HELD
        (hold,) = store.list_epic_holds(EPIC)
        assert hold.reason.startswith("review_rate_limit_exhausted")
        assert holds_node_out_of_frontier(hold.reason)
        # Still no verdict guessed and no round spent against the cycle.
        assert store.latest_review_round(ITEM) is None
        assert store.review_round_count(ITEM) == 0
        assert verify_chain(store.read_chain_rows()).ok


def test_a_slow_cadence_under_the_round_bound_is_still_only_a_wait(
    tmp_path: Path,
) -> None:
    """#2231 review, other side: the count must not become a second way to hold
    a limit that was never waited out. One round short of the bound the node is
    waiting, exactly as it is on a fast host — the count only ever fires on
    rounds kew itself relaunched and the provider itself rejected.
    """
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path, blocking=True)
    start = datetime.now(UTC)

    def _limited(ctx: ReviewerLaunchContext) -> CollectedSidecar:
        return stream_rate_limited(ctx)

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _seed_slow_cadence_limits(
            store, rounds=review_stage._MAX_RATE_LIMIT_ROUNDS - 2, now=start
        )
        statuses = {ITEM: DriverStatus.ADVERSARIAL_REVIEW}
        out = _drive(statuses, store, config, repo, head, _limited, now=start)

        assert (
            len(review_stage._rate_limited_rounds(store, ITEM))
            == review_stage._MAX_RATE_LIMIT_ROUNDS - 1
        )
        assert out.held == []
        assert store.list_epic_holds(EPIC) == []
        assert statuses[ITEM] == DriverStatus.ADVERSARIAL_REVIEW
        assert out.refused[ITEM].startswith("review_rate_limited")
        assert "exhausted" not in out.refused[ITEM]
        assert store.review_round_count(ITEM) == 0


def test_repeated_pre_launch_refusals_hold_under_their_own_reason(
    tmp_path: Path,
) -> None:
    """#2147 bound: the retry is not unlimited. A refusal that keeps repeating
    is no longer transient, so the node is held — under its own reason, never
    as an unparseable verdict."""
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path, blocking=True)

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        for attempt in (1, 2):
            out = _drive_refused(
                {ITEM: DriverStatus.ADVERSARIAL_REVIEW}, store, config, repo, head
            )
            assert out.held == [], f"attempt {attempt} must retry, not hold"
            assert store.list_epic_holds(EPIC) == []

        statuses = {ITEM: DriverStatus.ADVERSARIAL_REVIEW}
        out = _drive_refused(statuses, store, config, repo, head)

        assert out.held == [ITEM]
        assert statuses[ITEM] == DriverStatus.HELD
        (hold,) = store.list_epic_holds(EPIC)
        assert hold.reason.startswith("review_sandbox_refused")
        assert "unparseable" not in hold.reason
        assert out.refused == {}
        # Still no guessed verdict, and the reservation is released.
        assert store.latest_review_round(ITEM) is None
        assert store.list_epic_reservations(REVIEW_EPIC_REF) == []
        assert verify_chain(store.read_chain_rows()).ok


def test_a_review_that_ran_resets_the_pre_launch_refusal_budget(
    tmp_path: Path,
) -> None:
    """#2147: the bound spends per incident, not once per node lifetime — a
    review that got past the sandbox ends the refusal streak, so a later
    unrelated refusal is retried again instead of holding immediately."""
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path, blocking=True)

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        for _ in (1, 2):
            _drive_refused(
                {ITEM: DriverStatus.ADVERSARIAL_REVIEW}, store, config, repo, head
            )
        # A review round that actually launched ends the streak.
        store.save(
            RunRecord(
                run_id="rev-ran",
                issue_num=int(ITEM),
                item_id=ITEM,
                branch="kew/42",
                status="success",
                exit_code=0,
                started_at=datetime.now(UTC),
                finished_at=datetime.now(UTC),
                run_kind=ADVERSARIAL_REVIEW_RUN_KIND,
            )
        )

        out = _drive_refused(
            {ITEM: DriverStatus.ADVERSARIAL_REVIEW}, store, config, repo, head
        )

        assert out.held == []
        assert store.list_epic_holds(EPIC) == []
        assert ITEM in out.refused


def _never_launched_launcher(*, error_category: str, failure_layer: str, reason: str):
    """A reviewer lane that failed BEFORE any container ran (#2043): admission
    or launch refused, so there is no reviewer and no verdict to parse."""

    def _launch(ctx: ReviewerLaunchContext) -> CollectedSidecar:
        from kew.review_contract import infrastructure_failure_sidecar

        return infrastructure_failure_sidecar(
            ctx.log_dir,
            ctx.run_id,
            failure_layer=failure_layer,
            error_category=error_category,
            reason=reason,
        )

    return _launch


@pytest.mark.parametrize(
    ("error_category", "failure_layer", "names"),
    [
        ("launch_failed", "runtime", "container runtime"),
        ("credential_missing", "credential", "[review] runner"),
        ("container_start_failed", "container", "container kew-42 could not"),
    ],
)
def test_a_reviewer_that_never_started_is_not_held_as_unparseable(
    tmp_path: Path, error_category: str, failure_layer: str, names: str
) -> None:
    """#2043: an admission or launch failure before the reviewer starts is held
    under its OWN reason. Held as ``review_unparseable`` it sent the incident's
    operator looking for a reviewer verdict — and a verdict channel to fix —
    that never existed, while the real fault was one layer below."""
    from kew.epic_holds import (
        HOLD_REASON_MAX_CHARS,
        holds_node_out_of_frontier,
        safe_hold_reason,
    )

    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path, blocking=True)
    statuses = {ITEM: DriverStatus.ADVERSARIAL_REVIEW}

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        out = drive_review_stage(
            statuses,
            {ITEM: _facts(head)},
            epic_ref=EPIC,
            config=config,
            store=store,
            repo="o/r",
            repo_root=repo,
            base="main",
            reviewer_launcher=_never_launched_launcher(
                error_category=error_category,
                failure_layer=failure_layer,
                reason="the lane refused before the reviewer ran",
            ),
            now=NOW,
        )

        assert out.held == [ITEM]
        assert statuses[ITEM] == DriverStatus.HELD
        (hold,) = store.list_epic_holds(EPIC)
        assert hold.reason.startswith("review_launch_failed")
        assert "unparseable" not in hold.reason
        # It says the reviewer never started, and names the fix for the layer
        # that refused — not a verdict-channel repair.
        assert "the reviewer never started" in hold.reason
        assert names in hold.reason
        # The hold still takes the node out of the frontier, and an operator
        # surface echoes it WHOLE — a remedy truncated away is no remedy.
        assert holds_node_out_of_frontier(hold.reason)
        assert len(hold.reason) <= HOLD_REASON_MAX_CHARS
        assert safe_hold_reason(hold.reason) == hold.reason
        # No verdict was guessed into a round, and the chain still verifies.
        assert store.latest_review_round(ITEM) is None
        assert verify_chain(store.read_chain_rows()).ok


def test_a_reviewer_that_died_after_starting_is_held_as_unparseable(
    tmp_path: Path,
) -> None:
    """#2043 re-review: the "never started" hold belongs ONLY to a lane that
    failed before the container ran. A reviewer whose container started and then
    died mid-stream reports the stream layer, and takes the
    ``review_unparseable`` hold — the accurate reason for a reviewer that ran
    and left no admissible verdict — naming what its stream lacked."""
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path, blocking=True)
    statuses = {ITEM: DriverStatus.ADVERSARIAL_REVIEW}

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        out = drive_review_stage(
            statuses,
            {ITEM: _facts(head)},
            epic_ref=EPIC,
            config=config,
            store=store,
            repo="o/r",
            repo_root=repo,
            base="main",
            reviewer_launcher=_never_launched_launcher(
                error_category="sandbox_stream_error",
                failure_layer="stream",
                reason="the reviewer stream died after the container started",
            ),
            now=NOW,
        )

        assert out.held == [ITEM]
        (hold,) = store.list_epic_holds(EPIC)
        # Unparseable, naming what the stream lacked (#3186).
        assert hold.reason == (
            "review_unparseable: the reviewer stream died after the container started"
        )
        assert "never started" not in hold.reason
        assert store.latest_review_round(ITEM) is None
        assert verify_chain(store.read_chain_rows()).ok


def test_the_pre_launch_refusal_hold_names_the_build_command(tmp_path: Path) -> None:
    """#2043: the incident's cause was an absent agent image, whose fix is
    `kew sandbox build --runner claude-code`. The hold an operator reads has to
    name it — the sandbox layer's own refusal line scrolls past with the tick."""
    from kew.epic_holds import HOLD_REASON_MAX_CHARS, safe_hold_reason

    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path, blocking=True)

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        for _ in range(3):  # the bounded retry (#2147) is spent, then it holds
            out = _drive_refused(
                {ITEM: DriverStatus.ADVERSARIAL_REVIEW}, store, config, repo, head
            )

        assert out.held == [ITEM]
        (hold,) = store.list_epic_holds(EPIC)
        assert hold.reason.startswith("review_sandbox_refused")
        assert "the reviewer never started" in hold.reason
        assert "kew sandbox build --runner claude-code" in hold.reason
        assert len(hold.reason) <= HOLD_REASON_MAX_CHARS
        assert safe_hold_reason(hold.reason) == hold.reason


def test_a_deferred_refusal_keeps_the_node_due_for_re_review(tmp_path: Path) -> None:
    """#2147: the deferral is only safe if the node stays in the lifecycle. An
    approval landing between the refusal and the retry must not grandfather the
    node to IN_REVIEW — the merge gate refuses to merge it unreviewed, so a
    grandfathered status would drop it out of `_due_nodes` and wedge it."""
    from dataclasses import replace as _replace

    from kew.graph.domain import NodeStatus
    from kew.graph.driver.tick import _review_overlay_for

    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path, blocking=True)

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _drive_refused(
            {ITEM: DriverStatus.ADVERSARIAL_REVIEW}, store, config, repo, head
        )

        facts = _replace(_facts(head), review_decision="APPROVED")
        runs = store.list_by_item(ITEM)
        overlay = _review_overlay_for(
            ITEM,
            facts,
            observed_status=NodeStatus.READY,  # no `pr-pending-review` label
            config=config,
            store=store,
            review_res_kinds={},
            runs=runs,
        )
        assert overlay is not None and overlay.attempted
        assert (
            overlay_status(
                facts, runs, reserved=False, held_reason=None, review=overlay
            )
            is DriverStatus.ADVERSARIAL_REVIEW
        )


def test_a_deferred_refusal_still_blocks_the_merge_gate(tmp_path: Path) -> None:
    """#2147 regression: deferring a ROUND-1 refusal writes neither a round nor
    a ``review_*`` hold, which is exactly the shape the merge pass grandfathers
    as PRE_POLICY. Without the review-run disqualifier a human approval + green
    CI landing before the refusal bound HELDs the node auto-merges and closes a
    blocking-review node with ZERO review verdicts — the invariant the review
    stage runs before the merge pass to protect."""
    from kew.graph.driver.merge import REVIEW_SKIP_NOT_READY, ApprovalDecision
    from kew.graph.driver.merge import _review_gate as review_gate

    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path, blocking=True)

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        out = _drive_refused(
            {ITEM: DriverStatus.ADVERSARIAL_REVIEW}, store, config, repo, head
        )
        # The deferral's premise: nothing held, no round credited.
        assert out.held == []
        assert store.list_epic_holds(EPIC) == []
        assert store.review_round_count(ITEM) == 0

        blocked = review_gate(
            ITEM,
            item_id=ITEM,
            decision=ApprovalDecision(
                review={"submittedAt": "2026-07-23T13:00:00Z"},
                reviewer="alice",
                actor_type="User",
                permission="write",
            ),
            head_oid=head,
            pr_number=7,
            store=store,
            repo="o/r",
            epic_ref=EPIC,
        )

    assert blocked is not None, "a deferred refusal must not be grandfathered"
    assert blocked.action == "skip"
    assert blocked.reason == REVIEW_SKIP_NOT_READY


def test_unparseable_advisory_degrades_and_proceeds(tmp_path: Path):
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path, blocking=False)
    facts = _facts(head)
    statuses = {ITEM: DriverStatus.ADVERSARIAL_REVIEW}

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        out = drive_review_stage(
            statuses,
            {ITEM: facts},
            epic_ref=EPIC,
            config=config,
            store=store,
            repo="o/r",
            repo_root=repo,
            base="main",
            reviewer_launcher=_crash_launcher,
            now=NOW,
        )
        assert out.verdicts == {ITEM: VERDICT_DEGRADED}
        latest = store.latest_review_round(ITEM)
        assert latest is not None and latest.verdict == VERDICT_DEGRADED
        assert verify_chain(store.read_chain_rows()).ok


def test_disabled_stage_is_a_noop(tmp_path: Path):
    repo, head = _make_repo(tmp_path)
    config = DispatchConfig()  # review.adversarial defaults False
    facts = _facts(head)
    statuses = {ITEM: DriverStatus.IN_REVIEW}

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        out = drive_review_stage(
            statuses,
            {ITEM: facts},
            epic_ref=EPIC,
            config=config,
            store=store,
            repo="o/r",
            repo_root=repo,
            base="main",
            reviewer_launcher=_ready_launcher,
            now=NOW,
        )
        assert out.dispatched == []
        assert store.latest_review_round(ITEM) is None


def test_no_launcher_wired_is_loud_noop_never_silent_accept(tmp_path: Path):
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path, blocking=True)
    facts = _facts(head)
    statuses = {ITEM: DriverStatus.ADVERSARIAL_REVIEW}

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        out = drive_review_stage(
            statuses,
            {ITEM: facts},
            epic_ref=EPIC,
            config=config,
            store=store,
            repo="o/r",
            repo_root=repo,
            base="main",
            reviewer_launcher=None,
            now=NOW,
        )
        # No verdict produced, no reservation leaked, node stays under review —
        # the gate is unfed but NEVER silently cleared.
        assert out.unwired == [ITEM]
        assert store.latest_review_round(ITEM) is None
        assert store.list_epic_reservations(REVIEW_EPIC_REF) == []


# --- concurrent-state-change: reconcile false-positive (F-4) ---


def _seed_live_reviewer(store: SqliteRunStore, *, with_run_row: bool) -> str:
    """A live reviewer reservation under the review pseudo-epic; optionally with a
    ``pending`` run row (post-startup) or none (mid-startup, within grace)."""
    run_id = "rv-live-1"
    assert store.reserve_epic_node_item(REVIEW_EPIC_REF, ITEM, run_id)
    if with_run_row:
        store.save(
            RunRecord(
                run_id=run_id,
                issue_num=int(ITEM),
                item_id=ITEM,
                branch="review/42",
                status="pending",
                exit_code=0,
                started_at=NOW,
                finished_at=None,
                run_kind=ADVERSARIAL_REVIEW_RUN_KIND,
            )
        )
    return run_id


@pytest.mark.parametrize("with_run_row", [True, False])
def test_live_reviewer_survives_reconcile_sweep(tmp_path: Path, with_run_row: bool):
    """The mandatory false-positive guard: a live reviewer — mid-startup (no run
    row yet, reserved within the grace) OR with a pending run row — must survive
    every reconcile pass. Reclaiming it would strand a live reviewer (#975 class)."""
    config = _config(tmp_path, blocking=True)
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _seed_live_reviewer(store, with_run_row=with_run_row)
        still = _reconcile_reservations(REVIEW_EPIC_REF, store, config, now=NOW)
        assert still == {ITEM}
        assert [r.run_id for r in store.list_epic_reservations(REVIEW_EPIC_REF)] == [
            "rv-live-1"
        ]


def test_terminated_ci_fix_cannot_release_live_review_reservation(tmp_path: Path):
    """A CI-fix row cannot stand in for a governed review generation."""
    config = _config(tmp_path, blocking=True)
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _seed_live_reviewer(store, with_run_row=False)
        reserved_at = store.list_epic_reservations(REVIEW_EPIC_REF)[0].reserved_at
        store.save(
            RunRecord(
                run_id="ci-fix-1",
                issue_num=int(ITEM),
                item_id=ITEM,
                branch="agent/issue-42",
                status="failed",
                exit_code=1,
                started_at=reserved_at + timedelta(seconds=1),
                finished_at=reserved_at + timedelta(seconds=2),
                run_kind="ci_fix",
            )
        )

        still = _reconcile_reservations(
            REVIEW_EPIC_REF,
            store,
            config,
            now=reserved_at + timedelta(seconds=3),
        )

        assert still == {ITEM}
        assert [r.run_id for r in store.list_epic_reservations(REVIEW_EPIC_REF)] == [
            "rv-live-1"
        ]


# --- #2153: an exhausted remediation pool HOLDs the node, never drops it ------


def _spend_the_remediation_pool(store, cost: float) -> None:
    """Record a prior remediation round that ate the node's review pool."""
    store.save(
        RunRecord(
            run_id=f"rem-{cost}",
            issue_num=int(ITEM),
            item_id=ITEM,
            # NOT the PR's own head branch: a review row on that branch would be
            # read as the head's publication producer and derail the dispatch
            # this test is about.
            branch="agent/review-42",
            status="success",
            exit_code=0,
            started_at=NOW,
            finished_at=NOW,
            estimated_cost_usd=cost,
            run_kind="review_remediation",
        )
    )


def test_exhausted_remediation_pool_holds_the_node_with_spend_and_key(
    review_retry_scene, tmp_path
):
    """#2153: the refusal HOLDs under `review_budget` instead of leaving the node
    `remediating` — which read as progressing while every tick re-refused it."""
    scene = review_retry_scene
    _retry_findings(scene, tmp_path)
    scene.config.review.max_remediation_usd = 40.0
    _spend_the_remediation_pool(scene.store, 43.97)

    result = _drive_retry_scene(scene, tmp_path)

    assert result.held == [ITEM]
    assert result.remediating == []
    scene.launch.assert_not_called()
    [hold] = scene.store.list_epic_holds(EPIC)
    assert hold.reason.startswith("review_budget: ")
    # Names the pool, what it allows, what the node spent, and the key to raise.
    assert "remediation pool $40.00 exhausted (spent $43.97)" in hold.reason
    assert "raise [review].max_remediation_usd" in hold.reason
    # No reservation is left pinned under the review pseudo-epic.
    assert scene.store.list_epic_reservations(REVIEW_EPIC_REF) == []


def test_exhausted_pool_hold_routes_the_node_to_held(review_retry_scene, tmp_path):
    """The detailed reason must route through the overlay: matched bare-only it
    routed nowhere and the node kept reading REMEDIATING (#2153)."""
    scene = review_retry_scene
    _retry_findings(scene, tmp_path)
    scene.config.review.max_remediation_usd = 1.0
    _spend_the_remediation_pool(scene.store, 2.0)

    _drive_retry_scene(scene, tmp_path)

    [hold] = scene.store.list_epic_holds(EPIC)
    assert (
        overlay_status(
            _facts(scene.checkout.head_sha),
            [],
            reserved=False,
            held_reason=hold.reason,
        )
        == DriverStatus.HELD
    )


def test_raising_the_remediation_cap_resumes_the_held_node(
    review_retry_scene, tmp_path
):
    """Proof of the operator's one-knob fix: the driver's own reconcile pass
    clears the hold, and the next drive remediates (#2153)."""
    from kew.graph.driver.tick import _reconcile_review_budget_holds

    scene = review_retry_scene
    _retry_findings(scene, tmp_path)
    scene.config.review.max_remediation_usd = 40.0
    _spend_the_remediation_pool(scene.store, 43.97)
    assert _drive_retry_scene(scene, tmp_path).held == [ITEM]

    # Still exhausted: a tick that changes nothing leaves the hold in place.
    _reconcile_review_budget_holds(EPIC, scene.store, scene.config, now=NOW)
    assert [h.item_id for h in scene.store.list_epic_holds(EPIC)] == [ITEM]

    scene.config.review.max_remediation_usd = 60.0
    _reconcile_review_budget_holds(EPIC, scene.store, scene.config, now=NOW)
    assert scene.store.list_epic_holds(EPIC) == []

    # The raised cap is the ONLY thing the operator changed about the budget;
    # containment is this fixture's separate (already-covered) refusal.
    scene.config.sandbox.allow_uncontained_flows = True
    second = _drive_retry_scene(scene, tmp_path)
    assert second.remediating == [ITEM]
    scene.publish.assert_called_once()
    assert scene.store.list_epic_holds(EPIC) == []


def test_a_reviewer_pool_hold_is_never_cleared_by_the_remediation_cap(
    review_retry_scene,
):
    """Both pools sum the same rows, so the reconciler must re-check the hold
    against the cap its OWN reason names (#2153)."""
    from kew.budget import REVIEW_POOL_KEY, review_budget_hold_reason
    from kew.graph.driver.tick import _reconcile_review_budget_holds

    scene = review_retry_scene
    _spend_the_remediation_pool(scene.store, 12.0)
    scene.config.review.max_review_usd = 10.0
    scene.config.review.max_remediation_usd = 40.0
    scene.store.hold_epic_node(
        EPIC,
        ITEM,
        review_budget_hold_reason(
            limit_usd=10.0, spent_usd=12.0, config_key=REVIEW_POOL_KEY
        ),
    )

    _reconcile_review_budget_holds(EPIC, scene.store, scene.config, now=NOW)
    assert [h.item_id for h in scene.store.list_epic_holds(EPIC)] == [ITEM]

    scene.config.review.max_review_usd = 25.0
    _reconcile_review_budget_holds(EPIC, scene.store, scene.config, now=NOW)
    assert scene.store.list_epic_holds(EPIC) == []


# --- #2291: the round records the plan it was judged against ---


def _criteria_launcher(ctx: ReviewerLaunchContext) -> CollectedSidecar:
    """A reviewer that judges every recorded criterion the prompt named."""
    sidecar = {
        "verdict": "READY",
        "findings": [],
        "acceptance_evidence": [_evidence()],
        "criteria_results": [
            {"id": cid, "result": "pass", "evidence": _evidence(), "reason": "ran it"}
            for cid in ctx.criterion_ids
        ],
    }
    return stream_verdict(ctx, sidecar)


def _record_two_criteria(store, *, body: str = "body") -> str:
    from kew.triage.plan import canonical_plan_json, plan_sha
    from kew.triage.schema import AcceptanceCriterion, ItemPlan

    plan = ItemPlan(
        acceptance_criteria=[
            AcceptanceCriterion(id="AC1", statement="the round records the plan sha"),
            AcceptanceCriterion(id="AC2", statement="the comment shows the table"),
        ]
    )
    sha = plan_sha(plan)
    store.record_item_plan(
        ITEM,
        body_digest=hashlib.sha256(body.encode()).hexdigest(),
        source="extracted",
        plan_sha=sha,
        plan_json=canonical_plan_json(plan),
        criteria_count=2,
    )
    return sha


def test_the_round_row_records_the_plan_sha_and_criteria_it_was_judged_against(
    tmp_path: Path,
) -> None:
    """#2291: ``plan_sha`` + ``criteria_json`` land on the ``review_rounds`` row.

    Stored beside each other rather than resolved through the plan sha: a later
    body edit supersedes the ``item_plans`` row, and what THIS round received
    must stay readable afterwards.
    """
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path, blocking=False)
    statuses = {ITEM: DriverStatus.ADVERSARIAL_REVIEW}

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        sha = _record_two_criteria(store)
        out = _drive(statuses, store, config, repo, head, _criteria_launcher)

        assert out.verdicts == {ITEM: "READY"}
        latest = store.latest_review_round(ITEM)
        assert latest is not None
        assert latest.verdict == "READY"
        assert latest.plan_sha == sha
        assert [c["id"] for c in json.loads(latest.criteria_json or "[]")] == [
            "AC1",
            "AC2",
        ]
        assert verify_chain(store.read_chain_rows()).ok


def test_a_round_with_no_recorded_plan_records_no_plan_sha(tmp_path: Path) -> None:
    """Behaviour is unchanged for an item with nothing recorded: a reader must
    read None as "judged against nothing stated", never as "judged against the
    current plan"."""
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path, blocking=False)

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _drive(
            {ITEM: DriverStatus.ADVERSARIAL_REVIEW},
            store,
            config,
            repo,
            head,
            _ready_launcher,
        )

        latest = store.latest_review_round(ITEM)
        assert latest is not None
        assert (latest.verdict, latest.plan_sha, latest.criteria_json) == (
            "READY",
            None,
            None,
        )


def test_a_reviewer_silent_about_a_recorded_criterion_fails_the_round(
    tmp_path: Path,
) -> None:
    """The round is the crash path, not a READY: a reviewer that was handed the
    criteria and judged none of them has not judged the item's intent, and the
    merge gate must not be fed from it."""
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path, blocking=True)
    statuses = {ITEM: DriverStatus.ADVERSARIAL_REVIEW}

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _record_two_criteria(store)
        out = _drive(statuses, store, config, repo, head, _ready_launcher)

        assert out.held == [ITEM]
        assert statuses[ITEM] == DriverStatus.HELD
        (hold,) = store.list_epic_holds(EPIC)
        # The hold names the criteria the verdict left unjudged (#3186).
        assert hold.reason.startswith("review_unparseable: ")
        assert "AC1, AC2" in hold.reason
        assert store.latest_review_round(ITEM) is None


def test_repeated_credential_refusal_holds_remediation_with_attention(
    tmp_path: Path,
) -> None:
    """The same pre-launch credential refusal, tick after tick, holds the node.

    A refused producer lookup does not clear by itself: the drive retried one
    for five hours and posted 253 error comments (#3067). The first refusals
    still retry, since unreadable evidence can be transient; a run of identical
    ones holds the node under ``admission_refused``, which asks for a person.
    """
    from support.driver_fakes import FakeFactsReader, FakeSource

    from kew.graph.build import build_graph
    from kew.graph.domain import Node, NodeStatus
    from kew.graph.driver.attention import AttentionKind, attention_events
    from kew.graph.driver.review_stage import ReviewStageOutcome
    from kew.graph.driver.tick import summarize
    from kew.publication_credentials import CREDENTIAL_PUBLICATION_REFUSAL

    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path, blocking=True, max_rounds=2)
    facts = _facts(head)
    attempts: list[RemediationContext] = []

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        store.record_review_round(
            EPIC, ITEM, head_sha=head, verdict="NEEDS_CHANGES", sidecar_digest="d"
        )

        def refuse(ctx: RemediationContext) -> None:
            attempts.append(ctx)
            record_review_outcome(
                ReviewOutcome(
                    pr_number=ctx.pr_number or 0,
                    status=STATUS_FAILED,
                    stage=STAGE_PRE_LAUNCH,
                    classification=CREDENTIAL_PUBLICATION_REFUSAL,
                    detail="publication ancestor evidence is missing",
                    head_sha=ctx.head_sha,
                    item_id=ctx.item_id,
                ),
                store=store,
                config=config,
                notify=False,
            )

        def tick() -> ReviewStageOutcome:
            hold = store.list_epic_holds(EPIC)
            status = overlay_status(
                facts,
                [],
                reserved=False,
                held_reason=hold[0].reason if hold else None,
                review=ReviewOverlay(
                    enabled=True,
                    round_count=1,
                    latest_verdict="NEEDS_CHANGES",
                    latest_head_sha=head,
                    current_head_sha=head,
                ),
            )
            return drive_review_stage(
                {ITEM: status},
                {ITEM: facts},
                epic_ref=EPIC,
                config=config,
                store=store,
                repo="o/r",
                repo_root=repo,
                base="main",
                reviewer_launcher=_ready_launcher,
                remediator=refuse,
                now=NOW,
            )

        outcomes = [tick() for _ in range(5)]

        [hold] = store.list_epic_holds(EPIC)
        # What `kew epic watch` reads: the read-only snapshot, then its events.
        summary = summarize(
            EPIC,
            build_graph(
                FakeSource(
                    nodes=[
                        Node(id=ITEM, title="t", status=NodeStatus.TODO, epic_ref=EPIC)
                    ],
                    edges=[],
                ),
                EPIC,
            ),
            config=config,
            store=store,
            facts=FakeFactsReader(issue_facts_by_num={int(ITEM): facts}),
            now=NOW,
        )
        events = attention_events(EPIC, summary)

    assert [o.remediating for o in outcomes[:2]] == [[ITEM], [ITEM]]
    assert outcomes[2].held == [ITEM]
    assert len(attempts) == 3
    # The hold names the refused head, so a moved head releases it (#3193).
    assert hold.reason.startswith(
        f"admission_refused@{head[:12]}: credential_publication_refused"
    )
    assert "publication ancestor evidence is missing" in hold.reason
    assert outcomes[4].remediating == []
    assert [(e.kind, e.item) for e in events] == [(AttentionKind.HELD, ITEM)]
    assert "credential_publication_refused" in events[0].reason
    # A head-scoped hold clears itself on a pushed fix; unhold is the other way.
    assert events[0].action == (
        f"push a fix over head {head[:12]} (the hold clears when the PR head "
        "moves), or kew epic unhold"
    )


# --- #3186: holds say whether the reviewer ran, and what its stream lacked ---


def _answered_twice(
    launched: list[str], *, differ: bool = False, exit_code: int = 0
) -> Any:
    """A reviewer whose runner wrote a second ``result`` after its first: the
    same verdict again, or (*differ*) a different one."""
    ready = {"verdict": "READY", "findings": [], "acceptance_evidence": [_evidence()]}
    needs = {
        "verdict": "NEEDS_CHANGES",
        "findings": [
            {
                "severity": "high",
                "location": "code.py:1",
                "summary": "unchecked path",
                "evidence": {"command": "pytest", "output_digest": "sha256:cafe"},
            }
        ],
    }

    def _launch(ctx: ReviewerLaunchContext) -> CollectedSidecar:
        launched.append(ctx.run_id)
        genuine = reviewer_stream(ready, ctx.head_sha)
        second = (
            result_line(verdict_message(needs, ctx.head_sha)) if differ else genuine[-1]
        )
        return stream_lines(ctx, [*genuine, second], exit_code=exit_code)

    return _launch


def _verdict_payloads(store: SqliteRunStore) -> list[dict[str, Any]]:
    return [
        event.payload
        for event in store.list_audit_events(None, event=REVIEW_VERDICT_EVENT)
        if isinstance(event.payload, dict)
    ]


def test_a_reviewer_that_answered_twice_is_retried_once_then_held(
    tmp_path: Path,
) -> None:
    """#3174: Claude Code started a second turn after its first ``result``. The
    first such round is re-reviewed instead of held; the second holds, and the
    hold and the verdict audit both say "2 result events"."""
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path, blocking=True)
    launched: list[str] = []
    launcher = _answered_twice(launched)

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        first = _drive(
            {ITEM: DriverStatus.ADVERSARIAL_REVIEW}, store, config, repo, head, launcher
        )

        assert first.held == []
        assert store.list_epic_holds(EPIC) == []
        assert "2 result events" in first.refused[ITEM]
        # The retry is a wait that names no person (#3186), not a hold.
        assert first.refused[ITEM].startswith("review_retrying — ")
        assert classify_wait(first.refused[ITEM], held=False) is None
        assert store.latest_review_round(ITEM) is None
        assert _verdict_payloads(store) == []

        statuses = {ITEM: DriverStatus.ADVERSARIAL_REVIEW}
        second = _drive(statuses, store, config, repo, head, launcher)

        assert len(launched) == 2
        assert second.held == [ITEM]
        assert statuses[ITEM] == DriverStatus.HELD
        (hold,) = store.list_epic_holds(EPIC)
        assert hold.reason.startswith("review_multiple_results: ")
        assert "2 result events" in hold.reason
        assert holds_node_out_of_frontier(hold.reason)
        assert safe_hold_reason(hold.reason) == hold.reason
        assert store.latest_review_round(ITEM) is None
        (payload,) = _verdict_payloads(store)
        assert payload["verdict"] == "UNPARSEABLE"
        assert payload["error_category"] == "review_multiple_results"
        assert "2 result events" in payload["refusal"]
        assert verify_chain(store.read_chain_rows()).ok


def test_a_reviewer_whose_results_disagree_is_held_without_a_retry(
    tmp_path: Path,
) -> None:
    """Two DIFFERENT verdict blocks are refused as they always were: one may be
    forged into the agent's stdout, so the stream buys no retry."""
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path, blocking=True)
    launched: list[str] = []

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        out = _drive(
            {ITEM: DriverStatus.ADVERSARIAL_REVIEW},
            store,
            config,
            repo,
            head,
            _answered_twice(launched, differ=True),
        )

        assert launched and len(launched) == 1
        assert out.refused == {}
        assert out.held == [ITEM]
        (hold,) = store.list_epic_holds(EPIC)
        # The hold itself names the refusal, not only the audit (#3186).
        assert hold.reason.startswith("review_unparseable: ")
        assert "2 result events" in hold.reason
        assert holds_node_out_of_frontier(hold.reason)
        assert store.latest_review_round(ITEM) is None
        (payload,) = _verdict_payloads(store)
        assert "2 result events" in payload["refusal"]


class _RetryAppendFails(SqliteRunStore):
    """A ledger whose append of the retry row fails, as a full disk would."""

    def append_audit_event(self, **kwargs: Any) -> int:
        if kwargs.get("event") == "adversarial_review_retried":
            raise sqlite3.OperationalError("database or disk is full")
        return super().append_audit_event(**kwargs)


def test_a_retry_that_cannot_be_recorded_holds_instead_of_retrying(
    tmp_path: Path,
) -> None:
    """#3186: the retry bound is read back from the retry rows, so a retry
    whose row did not land would leave the count at zero and launch another
    paid run every tick. It holds at once instead."""
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path, blocking=True)
    launched: list[str] = []

    with _RetryAppendFails(str(tmp_path / "d.db")) as store:
        statuses = {ITEM: DriverStatus.ADVERSARIAL_REVIEW}
        out = _drive(statuses, store, config, repo, head, _answered_twice(launched))

        assert len(launched) == 1
        assert out.refused == {}
        assert out.held == [ITEM]
        assert statuses[ITEM] == DriverStatus.HELD
        (hold,) = store.list_epic_holds(EPIC)
        assert hold.reason.startswith("review_multiple_results: ")
        assert "2 result events" in hold.reason
        assert store.list_audit_events(None, event="adversarial_review_retried") == []


def test_a_review_that_raised_after_its_run_was_recorded_names_the_raise(
    tmp_path: Path,
) -> None:
    """#3186: a reviewer that ran, and whose dispatch then raised, holds as
    unparseable — and the hold says what raised, not a bare reason."""
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path, blocking=True)

    with SqliteRunStore(str(tmp_path / "d.db")) as store:

        def _ran_then_raised(ctx: ReviewerLaunchContext) -> CollectedSidecar:
            store.save(
                RunRecord(
                    run_id=ctx.run_id,
                    issue_num=int(ITEM),
                    item_id=ITEM,
                    branch="kew/42",
                    status="failure",
                    exit_code=0,
                    started_at=datetime.now(UTC),
                    finished_at=datetime.now(UTC),
                    error_category="sandbox_stream_error",
                    run_kind=ADVERSARIAL_REVIEW_RUN_KIND,
                )
            )
            raise RuntimeError("post-run bookkeeping failed")

        out = _drive(
            {ITEM: DriverStatus.ADVERSARIAL_REVIEW},
            store,
            config,
            repo,
            head,
            _ran_then_raised,
        )

        assert out.held == [ITEM]
        (hold,) = store.list_epic_holds(EPIC)
        assert hold.reason.startswith("review_unparseable: ")
        assert "sandbox_stream_error" in hold.reason
        assert "post-run bookkeeping failed" in hold.reason
        assert "never started" not in hold.reason
        (payload,) = _verdict_payloads(store)
        assert "post-run bookkeeping failed" in payload["refusal"]


def test_a_failed_review_run_holds_under_its_own_name_and_is_not_retried(
    tmp_path: Path,
) -> None:
    """#3174: a review run that failed left an empty sidecar. It holds under its
    own classification, naming the exit and what the stream lacked — never a
    bare unparseable hold — and no retry is scheduled."""
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path, blocking=True)
    launched: list[str] = []

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        out = _drive(
            {ITEM: DriverStatus.ADVERSARIAL_REVIEW},
            store,
            config,
            repo,
            head,
            _answered_twice(launched, exit_code=1),
        )

        assert len(launched) == 1
        assert out.refused == {}
        assert out.held == [ITEM]
        (hold,) = store.list_epic_holds(EPIC)
        assert hold.reason.startswith("review_failed_no_verdict: reviewer exited 1")
        assert "2 result events" in hold.reason
        assert "unparseable" not in hold.reason
        assert holds_node_out_of_frontier(hold.reason)
        (payload,) = _verdict_payloads(store)
        assert payload["error_category"] == "review_failed_no_verdict"
        assert "reviewer exited 1" in payload["refusal"]
        (sidecar,) = tmp_path.rglob(f"review-sidecar-{launched[0]}.json")
        assert sidecar.read_bytes() == b""
        assert store.latest_review_round(ITEM) is None


def test_a_raised_review_whose_row_says_runtime_timeout_never_started(
    tmp_path: Path,
) -> None:
    """#3186: a raise that escaped the dispatch after the run row recorded
    ``runtime_timeout`` is a reviewer that never started — the create was never
    answered — so it holds as a launch failure naming the timeout, never as
    ``review_unparseable``."""
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path, blocking=True)

    with SqliteRunStore(str(tmp_path / "d.db")) as store:

        def _timed_out(ctx: ReviewerLaunchContext) -> CollectedSidecar:
            store.save(
                RunRecord(
                    run_id=ctx.run_id,
                    issue_num=int(ITEM),
                    item_id=ITEM,
                    branch="kew/42",
                    status="failure",
                    exit_code=-1,
                    started_at=datetime.now(UTC),
                    finished_at=datetime.now(UTC),
                    error_category="runtime_timeout",
                    run_kind=ADVERSARIAL_REVIEW_RUN_KIND,
                )
            )
            raise RuntimeError("post-run bookkeeping failed")

        out = _drive(
            {ITEM: DriverStatus.ADVERSARIAL_REVIEW},
            store,
            config,
            repo,
            head,
            _timed_out,
        )

        assert out.held == [ITEM]
        (hold,) = store.list_epic_holds(EPIC)
        assert hold.reason.startswith("review_launch_failed")
        assert "runtime_timeout" in hold.reason
        assert "unparseable" not in hold.reason
        assert store.latest_review_round(ITEM) is None


# --- #3205: the daily and weekly caps stop reviews and remediations too ------


class _BudgetClock(datetime):
    """The budget module's clock, pinned to ``NOW`` (a clock-boundary fake)."""

    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        return NOW if tz is None else NOW.astimezone(tz)


def _pin_budget_clock(monkeypatch) -> None:
    monkeypatch.setattr("kew.budget.datetime", _BudgetClock)


def _spend_in_window(
    store, cost: float, *, at: datetime = NOW, run_id: str = ""
) -> None:
    """Record settled spend on ANOTHER item, so only the windows see it."""
    store.save(
        RunRecord(
            run_id=run_id or f"spend-{cost}-{at.isoformat()}",
            issue_num=99,
            item_id="99",
            branch="agent/issue-99",
            status="success",
            exit_code=0,
            started_at=at,
            finished_at=at,
            estimated_cost_usd=cost,
            run_kind="dispatch",
        )
    )


def _verdict_events(store) -> list[Any]:
    return [
        e
        for e in store.list_audit_events(item_id=ITEM)
        if e.event == REVIEW_VERDICT_EVENT
    ]


def _held_status(head: str, reason: str) -> DriverStatus:
    """What the next tick's overlay reads for the held node."""
    return overlay_status(
        _facts(head),
        [],
        reserved=False,
        held_reason=reason,
        review=ReviewOverlay(
            enabled=True,
            round_count=0,
            latest_verdict=None,
            latest_head_sha=None,
            current_head_sha=head,
        ),
    )


def test_a_review_past_the_daily_cap_holds_once_and_never_calls_the_reviewer(
    tmp_path: Path,
) -> None:
    from kew.graph.driver.tick import _reconcile_window_budget_holds
    from kew.waits import AttentionKind

    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path, blocking=True)
    config.budget.daily_usd = 10.0
    reviewer = Mock(side_effect=_ready_launcher)
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _spend_in_window(store, 12.5)

        out = _drive(
            {ITEM: DriverStatus.ADVERSARIAL_REVIEW}, store, config, repo, head, reviewer
        )

        reviewer.assert_not_called()
        assert out.held == [ITEM]
        [hold] = store.list_epic_holds(EPIC)
        assert hold.reason == "budget: daily cap $10.00 reached ($12.50 spent)"
        attention = classify_wait(hold.reason, held=True)
        assert attention is not None
        assert attention.kind is AttentionKind.BUDGET_EXHAUSTED
        assert holds_node_out_of_frontier(hold.reason)
        # No verdict, no round, and the reservation is not left pinned.
        assert _verdict_events(store) == []
        assert store.review_round_count(ITEM) == 0
        assert store.list_epic_reservations(REVIEW_EPIC_REF) == []

        # Two more ticks: the reconcile keeps the hold (still past the cap),
        # the overlay reads HELD, and the hold row — what the attention event
        # is read off — is untouched, so no second event.
        for _ in range(2):
            _reconcile_window_budget_holds(EPIC, store, config, now=NOW)
            status = _held_status(head, hold.reason)
            assert status == DriverStatus.HELD
            _drive({ITEM: status}, store, config, repo, head, reviewer)

        reviewer.assert_not_called()
        assert store.list_epic_holds(EPIC) == [hold]


def test_a_review_past_the_weekly_cap_names_the_weekly_window(tmp_path: Path) -> None:
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path, blocking=True)
    config.budget.daily_usd = 1000.0
    config.budget.weekly_usd = 30.0
    reviewer = Mock(side_effect=_ready_launcher)
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        # Spent three days ago: out of today's window, inside the week's.
        _spend_in_window(store, 35.0, at=NOW - timedelta(days=3))

        out = _drive(
            {ITEM: DriverStatus.ADVERSARIAL_REVIEW}, store, config, repo, head, reviewer
        )

        reviewer.assert_not_called()
        assert out.held == [ITEM]
        [hold] = store.list_epic_holds(EPIC)
        assert hold.reason == "budget: weekly cap $30.00 reached ($35.00 spent)"
        assert _verdict_events(store) == []
        assert store.review_round_count(ITEM) == 0


def test_reservations_plus_the_reviewers_ceiling_past_the_cap_refuse_the_launch(
    tmp_path: Path,
) -> None:
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path, blocking=True)
    config.budget.daily_usd = 20.0
    config.review.max_review_usd = 10.0
    reviewer = Mock(side_effect=_ready_launcher)
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        # $5 settled is under the $20 cap, but one run is still in flight:
        # $5 + its $10 reservation + this reviewer's $10 ceiling passes it.
        _spend_in_window(store, 5.0)
        store.save(
            RunRecord(
                run_id="in-flight",
                issue_num=98,
                item_id="98",
                branch="agent/issue-98",
                status="pending",
                exit_code=-1,
                started_at=NOW,
                run_kind="dispatch",
            )
        )

        out = _drive(
            {ITEM: DriverStatus.ADVERSARIAL_REVIEW}, store, config, repo, head, reviewer
        )

        reviewer.assert_not_called()
        assert out.held == [ITEM]
        [hold] = store.list_epic_holds(EPIC)
        # Recorded spend and the other run's reservation, named apart; the
        # refused reviewer's own $10 ceiling is in neither number.
        assert hold.reason == (
            "budget: daily cap $20.00 reached "
            "($5.00 spent, $10.00 reserved by running or starting runs)"
        )


@pytest.mark.parametrize("recovery", ["utc_midnight", "cap_raised"])
def test_the_cap_hold_clears_itself_and_the_next_tick_reviews_once(
    tmp_path: Path, recovery: str
) -> None:
    from kew.graph.driver.tick import _reconcile_window_budget_holds

    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path, blocking=True)
    config.budget.daily_usd = 10.0
    reviewer = Mock(side_effect=_ready_launcher)
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _spend_in_window(store, 12.5)
        _drive(
            {ITEM: DriverStatus.ADVERSARIAL_REVIEW}, store, config, repo, head, reviewer
        )
        assert [h.item_id for h in store.list_epic_holds(EPIC)] == [ITEM]

        if recovery == "utc_midnight":
            later = datetime(2026, 7, 24, 0, 0, 1, tzinfo=UTC)
        else:
            later = NOW
            config.budget.daily_usd = 100.0
        _reconcile_window_budget_holds(EPIC, store, config, now=later)
        assert store.list_epic_holds(EPIC) == []

        out = _drive(
            {ITEM: DriverStatus.ADVERSARIAL_REVIEW},
            store,
            config,
            repo,
            head,
            reviewer,
            now=later,
        )

        reviewer.assert_called_once()
        assert out.verdicts == {ITEM: "READY"}
        assert store.review_round_count(ITEM) == 1


def test_a_daily_cap_hold_still_past_the_weekly_cap_after_midnight_names_the_week(
    tmp_path: Path,
) -> None:
    from kew.graph.driver.tick import _reconcile_window_budget_holds
    from kew.waits import AttentionKind

    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path, blocking=True)
    config.budget.daily_usd = 10.0
    config.budget.weekly_usd = 30.0
    reviewer = Mock(side_effect=_ready_launcher)
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _spend_in_window(store, 12.5)
        _spend_in_window(store, 25.0, at=NOW - timedelta(days=2))
        _drive(
            {ITEM: DriverStatus.ADVERSARIAL_REVIEW}, store, config, repo, head, reviewer
        )
        [daily] = store.list_epic_holds(EPIC)
        assert daily.reason == "budget: daily cap $10.00 reached ($12.50 spent)"

        after_midnight = datetime(2026, 7, 24, 0, 0, 1, tzinfo=UTC)
        _reconcile_window_budget_holds(EPIC, store, config, now=after_midnight)

        [weekly] = store.list_epic_holds(EPIC)
        assert weekly.reason == "budget: weekly cap $30.00 reached ($37.50 spent)"
        assert weekly.held_at == daily.held_at
        assert holds_node_out_of_frontier(weekly.reason)
        attention = classify_wait(weekly.reason, held=True)
        assert attention is not None
        assert attention.kind is AttentionKind.BUDGET_EXHAUSTED

        # More spend in the same window rewrites nothing: the reason, which an
        # attention event is keyed on, changed once, for the change of window.
        _spend_in_window(store, 1.0, at=after_midnight)
        _reconcile_window_budget_holds(EPIC, store, config, now=after_midnight)
        assert store.list_epic_holds(EPIC) == [weekly]

        # Neither window binds once the weekly cap is raised: the hold clears.
        config.budget.weekly_usd = 100.0
        _reconcile_window_budget_holds(EPIC, store, config, now=after_midnight)
        assert store.list_epic_holds(EPIC) == []
        reviewer.assert_not_called()


def test_a_reviewer_already_running_when_the_cap_passes_finishes_with_its_verdict(
    tmp_path: Path,
) -> None:
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path, blocking=True)
    config.budget.daily_usd = 10.0
    with SqliteRunStore(str(tmp_path / "d.db")) as store:

        def _cap_passes_mid_run(ctx: ReviewerLaunchContext) -> CollectedSidecar:
            # Another run settles while this reviewer is running.
            _spend_in_window(store, 50.0)
            return _ready_launcher(ctx)

        out = _drive(
            {ITEM: DriverStatus.ADVERSARIAL_REVIEW},
            store,
            config,
            repo,
            head,
            _cap_passes_mid_run,
        )

        assert out.verdicts == {ITEM: "READY"}
        assert [e.payload["verdict"] for e in _verdict_events(store)] == ["READY"]
        assert store.list_epic_holds(EPIC) == []


def _remediation_rows(store) -> list[RunRecord]:
    return [r for r in store.list_by_item(ITEM) if r.run_kind == "review_remediation"]


def test_a_remediation_past_the_daily_cap_holds_and_never_launches(
    review_retry_scene, tmp_path, monkeypatch
):
    _pin_budget_clock(monkeypatch)
    scene = review_retry_scene
    scene.config.sandbox.allow_uncontained_flows = True
    scene.config.budget.daily_usd = 10.0
    _retry_findings(scene, tmp_path)
    _spend_in_window(scene.store, 12.5)
    rounds_before = scene.store.review_round_count(ITEM)

    result = _drive_retry_scene(scene, tmp_path)

    scene.launch.assert_not_called()
    assert result.held == [ITEM]
    assert result.remediating == []
    [hold] = scene.store.list_epic_holds(EPIC)
    assert hold.reason == "budget: daily cap $10.00 reached ($12.50 spent)"
    # Not a remediation attempt and not a review round.
    assert _remediation_rows(scene.store) == []
    assert scene.store.review_round_count(ITEM) == rounds_before
    assert scene.store.list_epic_reservations(REVIEW_EPIC_REF) == []


def test_a_remediation_past_the_weekly_cap_names_the_weekly_window(
    review_retry_scene, tmp_path, monkeypatch
):
    _pin_budget_clock(monkeypatch)
    scene = review_retry_scene
    scene.config.sandbox.allow_uncontained_flows = True
    scene.config.budget.daily_usd = 1000.0
    scene.config.budget.weekly_usd = 30.0
    _retry_findings(scene, tmp_path)
    _spend_in_window(scene.store, 35.0, at=NOW - timedelta(days=2))

    result = _drive_retry_scene(scene, tmp_path)

    scene.launch.assert_not_called()
    assert result.held == [ITEM]
    [hold] = scene.store.list_epic_holds(EPIC)
    assert hold.reason == "budget: weekly cap $30.00 reached ($35.00 spent)"
    assert _remediation_rows(scene.store) == []


def test_a_cleared_remediation_cap_hold_remediates_on_the_next_tick(
    review_retry_scene, tmp_path, monkeypatch
):
    from kew.graph.driver.tick import _reconcile_window_budget_holds

    _pin_budget_clock(monkeypatch)
    scene = review_retry_scene
    scene.config.sandbox.allow_uncontained_flows = True
    scene.config.budget.daily_usd = 10.0
    _retry_findings(scene, tmp_path)
    _spend_in_window(scene.store, 12.5)
    assert _drive_retry_scene(scene, tmp_path).held == [ITEM]

    scene.config.budget.daily_usd = 100.0
    _reconcile_window_budget_holds(EPIC, scene.store, scene.config, now=NOW)
    assert scene.store.list_epic_holds(EPIC) == []

    second = _drive_retry_scene(scene, tmp_path)
    assert second.remediating == [ITEM]
    scene.launch.assert_called_once()


@pytest.mark.parametrize("force_budget", [False, True])
def test_kew_review_force_budget_still_launches_past_the_cap(
    review_retry_scene, monkeypatch, force_budget
):
    from kew import review
    from kew.cli import _command_exit_code
    from kew.review_contract import Finding

    _pin_budget_clock(monkeypatch)
    scene = review_retry_scene
    scene.config.sandbox.allow_uncontained_flows = True
    scene.config.budget.daily_usd = 10.0
    _spend_in_window(scene.store, 12.5)

    code = _command_exit_code(
        review.dispatch_review(
            pr_number=7,
            config=scene.config,
            store=scene.store,
            findings=[Finding("high", "a:1", "gap", "pytest", "sha256:a")],
            force_budget=force_budget,
        )
    )

    if force_budget:
        scene.launch.assert_called_once()
    else:
        scene.launch.assert_not_called()
        assert code != 0


# --- #3330: the reviewer's usage-limit wait and hold name the agent token -----

#: Stand-in reviewer credential values. Not real; only fingerprints may surface.
_REVIEW_TOKEN_A = "fixture-review-credential-a-0123"
_REVIEW_TOKEN_B = "fixture-review-credential-b-9876"


def _use_reviewer_token(monkeypatch: pytest.MonkeyPatch, value: str | None) -> str:
    """Put *value* in the environment as the reviewer's token (None: no token
    at all); its fingerprint."""
    from kew.runners.registry import runner_credential_fingerprint

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    if value is not None:
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", value)
    return runner_credential_fingerprint("claude-code")


def _oauth_token(fingerprint: str) -> str:
    return f"agent token CLAUDE_CODE_OAUTH_TOKEN (fingerprint {fingerprint})"


def test_the_reviewer_usage_limit_wait_names_the_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wait names the token, and reads the same on every tick under it:
    the reason is part of the attention event's state key."""
    fingerprint = _use_reviewer_token(monkeypatch, _REVIEW_TOKEN_A)
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path, blocking=True)
    start = datetime.now(UTC)
    launched: list[str] = []

    def _limited(ctx: ReviewerLaunchContext) -> CollectedSidecar:
        launched.append(ctx.run_id)
        return stream_rate_limited(ctx)

    statuses = {ITEM: DriverStatus.ADVERSARIAL_REVIEW}
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        first = _drive(statuses, store, config, repo, head, _limited, now=start)
        second = _drive(
            {ITEM: DriverStatus.ADVERSARIAL_REVIEW},
            store,
            config,
            repo,
            head,
            _limited,
            now=start + timedelta(minutes=5),
        )

    reason = first.refused[ITEM]
    assert reason.startswith("review_rate_limited")
    assert "claude-code" in reason
    assert reason.endswith(_oauth_token(fingerprint))
    assert _REVIEW_TOKEN_A not in reason
    assert second.refused[ITEM] == reason, "one token, one waiting state"
    assert len(launched) == 1, "the same token waits out the limit"


def test_the_reviewer_usage_limit_wait_names_the_token_the_round_ran_under(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wait names the token the provider refused, not whatever the
    environment holds at a later tick: a driver restarted without the token
    still says which one hit the limit, and still waits — kew cannot tell
    that the account changed."""
    refused = _use_reviewer_token(monkeypatch, _REVIEW_TOKEN_A)
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path, blocking=True)
    start = datetime.now(UTC)
    launched: list[str] = []

    def _limited(ctx: ReviewerLaunchContext) -> CollectedSidecar:
        launched.append(ctx.run_id)
        return stream_rate_limited(ctx)

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        first = _drive(
            {ITEM: DriverStatus.ADVERSARIAL_REVIEW},
            store,
            config,
            repo,
            head,
            _limited,
            now=start,
        ).refused[ITEM]
        _use_reviewer_token(monkeypatch, None)
        later = _drive(
            {ITEM: DriverStatus.ADVERSARIAL_REVIEW},
            store,
            config,
            repo,
            head,
            _limited,
            now=start + timedelta(minutes=5),
        ).refused[ITEM]

    assert later == first
    assert later.endswith(_oauth_token(refused))
    assert len(launched) == 1


def test_a_replaced_reviewer_token_ends_the_usage_limit_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A different token is a different account window, as for a runner-quota
    hold (``released_by_credential``): the next tick re-reviews at once, and
    a limit that ends that round too names the new token."""
    refused = _use_reviewer_token(monkeypatch, _REVIEW_TOKEN_A)
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path, blocking=True)
    start = datetime.now(UTC)
    launched: list[str] = []

    def _limited(ctx: ReviewerLaunchContext) -> CollectedSidecar:
        launched.append(ctx.run_id)
        return stream_rate_limited(ctx)

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        first = _drive(
            {ITEM: DriverStatus.ADVERSARIAL_REVIEW},
            store,
            config,
            repo,
            head,
            _limited,
            now=start,
        ).refused[ITEM]
        replaced = _use_reviewer_token(monkeypatch, _REVIEW_TOKEN_B)
        later = _drive(
            {ITEM: DriverStatus.ADVERSARIAL_REVIEW},
            store,
            config,
            repo,
            head,
            _limited,
            now=start + timedelta(minutes=5),
        ).refused[ITEM]

    assert len(launched) == 2, "the new token re-reviews without waiting"
    assert first.endswith(_oauth_token(refused))
    assert later.endswith(_oauth_token(replaced))


def test_the_reviewer_usage_limit_hold_names_the_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fingerprint = _use_reviewer_token(monkeypatch, _REVIEW_TOKEN_A)
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path, blocking=True)
    start = datetime.now(UTC)

    def _limited(ctx: ReviewerLaunchContext) -> CollectedSidecar:
        return stream_rate_limited(ctx)

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _drive(
            {ITEM: DriverStatus.ADVERSARIAL_REVIEW},
            store,
            config,
            repo,
            head,
            _limited,
            now=start,
        )
        aged = review_stage._MAX_RATE_LIMIT_WAIT + timedelta(hours=1)
        _backdate_rate_limited_runs(store, ITEM, by=aged)
        _seed_rate_limited_retries(
            store, since=start - aged, until=start - timedelta(minutes=20)
        )
        out = _drive(
            {ITEM: DriverStatus.ADVERSARIAL_REVIEW},
            store,
            config,
            repo,
            head,
            _limited,
            now=start,
        )
        (hold,) = store.list_epic_holds(EPIC)

    assert out.held == [ITEM]
    assert holds_node_out_of_frontier(hold.reason)
    assert hold.reason == (
        "review_rate_limit_exhausted: no claude-code reviewer round got past a usage "
        f"limit for 6 h: {_oauth_token(fingerprint)}"
    )
    assert hold.reason == safe_hold_reason(hold.reason)
    assert _REVIEW_TOKEN_A not in hold.reason


#: Every env var a registered runner authenticates with, each set: the longest
#: token name kew can print (claude-code reads two).
_EVERY_CREDENTIAL = {
    "ANTHROPIC_API_KEY": _REVIEW_TOKEN_A,
    "CLAUDE_CODE_OAUTH_TOKEN": _REVIEW_TOKEN_B,
    "OPENROUTER_API_KEY": "fixture-review-credential-c-5555",
}


def _credentialed_runners() -> list[str]:
    from kew.runners.registry import KNOWN_RUNNER_NAMES, runner_required_credentials

    return [
        name for name in sorted(KNOWN_RUNNER_NAMES) if runner_required_credentials(name)
    ]


@pytest.mark.parametrize("runner", _credentialed_runners())
@pytest.mark.parametrize(
    "render",
    [
        review_stage._rate_limit_wait_reason,
        review_stage._rate_limit_exhausted_hold_reason,
    ],
    ids=["wait", "exhausted-hold"],
)
def test_the_echo_cap_never_cuts_the_token_out_of_a_reviewer_usage_limit(
    runner: str, render: Any
) -> None:
    """Every operator surface flattens a reason to ``HOLD_REASON_MAX_CHARS``:
    the token is what the operator acts on, so even the longest one a
    registered runner can carry is never what the cap cuts."""
    from kew.runners.registry import run_credential

    credential = run_credential(runner, _EVERY_CREDENTIAL)

    reason = render(credential)
    capped = safe_hold_reason(reason)

    assert capped == reason
    assert capped.endswith(
        f"agent token {'/'.join(credential.names)} "
        f"(fingerprint {credential.fingerprint})"
    )
    assert not any(value in capped for value in _EVERY_CREDENTIAL.values())


# --- #3246: a Docker call that times out at review launch is retried ---------


def _provisioning_rm_timed_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, launched: list[str]
):
    """A reviewer launch whose provisioning container's ``rm -f`` the daemon
    never answers. The ``docker`` is a real executable that hangs on ``rm``;
    the real absence check times it out, and the launch reports what the
    production launcher reports for that: ``runtime_timeout``, no container."""
    from support.fake_docker import LaunchDocker

    from kew import review_environment, subprocess_utils
    from kew.review_contract import infrastructure_failure_sidecar
    from kew.review_environment import ProvisioningUnconfirmed

    fake = LaunchDocker(tmp_path / "runtime", rm_hangs=True)
    leftover = fake.add("kew-42-provision", {}, status="exited")
    monkeypatch.setitem(subprocess_utils.TOOL_PATHS, "docker", str(fake.path))
    monkeypatch.setattr(review_environment, "_CONTAINER_REMOVE_TIMEOUT_SECONDS", 0.5)

    def _launch(ctx: ReviewerLaunchContext) -> CollectedSidecar:
        launched.append(ctx.run_id)
        with pytest.raises(ProvisioningUnconfirmed) as raised:
            review_environment._require_container_gone(
                "docker", "kew-42-provision", leftover
            )
        assert raised.value.timed_out
        return infrastructure_failure_sidecar(
            ctx.log_dir,
            ctx.run_id,
            failure_layer="container",
            error_category="runtime_timeout",
            reason=str(raised.value),
        )

    return _launch


def test_a_timed_out_docker_call_at_review_launch_waits_and_retries_before_holding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A loaded host's daemon did not answer the provisioning container's
    ``rm -f``: no reviewer ran. Held ``review_launch_failed`` on the first
    one, the node needed a person for a host that was only slow. It waits
    instead, backs off, and a later tick launches the review."""
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path, blocking=True)
    launched: list[str] = []
    timed_out = _provisioning_rm_timed_out(tmp_path, monkeypatch, launched)

    def _ready(ctx: ReviewerLaunchContext) -> CollectedSidecar:
        launched.append(ctx.run_id)
        return _ready_launcher(ctx)

    # The back-off times from the run row's own ``finished_at``, stamped with
    # the real clock, so these ticks run on real-clock times.
    start = datetime.now(UTC)
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        statuses = {ITEM: DriverStatus.ADVERSARIAL_REVIEW}
        out = _drive(statuses, store, config, repo, head, timed_out, now=start)

        assert out.held == []
        assert store.list_epic_holds(EPIC) == []
        assert statuses[ITEM] == DriverStatus.ADVERSARIAL_REVIEW
        assert out.refused[ITEM].startswith("review_retrying")
        assert "runtime_timeout" in out.refused[ITEM]
        assert store.latest_review_round(ITEM) is None
        assert store.list_epic_reservations(REVIEW_EPIC_REF) == []
        assert len(launched) == 1

        # Inside the back-off: nothing is launched into the same slow daemon.
        out = _drive(
            {ITEM: DriverStatus.ADVERSARIAL_REVIEW},
            store,
            config,
            repo,
            head,
            timed_out,
            now=start + timedelta(minutes=1),
        )
        assert len(launched) == 1
        assert out.held == []
        assert out.refused[ITEM].startswith("review_retrying")

        # After it, a later tick launches the review and the round lands.
        statuses = {ITEM: DriverStatus.ADVERSARIAL_REVIEW}
        out = _drive(
            statuses,
            store,
            config,
            repo,
            head,
            _ready,
            now=start + timedelta(minutes=5),
        )
        assert len(launched) == 2
        assert out.held == []
        assert out.refused == {}
        latest = store.latest_review_round(ITEM)
        assert latest is not None and latest.verdict == "READY"
        assert verify_chain(store.read_chain_rows()).ok


def test_the_launch_hold_comes_only_after_the_retry_bound(tmp_path: Path) -> None:
    """Every launch times out: the first two wait out a growing back-off, and
    only the third holds ``review_launch_failed``, naming the timeout."""
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path, blocking=True)
    launched: list[str] = []
    timed_out = _never_launched_launcher(
        error_category="runtime_timeout",
        failure_layer="container",
        reason="the docker create timed out",
    )

    def _launch(ctx: ReviewerLaunchContext) -> CollectedSidecar:
        launched.append(ctx.run_id)
        return timed_out(ctx)

    start = datetime.now(UTC)
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        held: list[list[str]] = []
        # Each row's finish is stamped on the real clock, moments after
        # ``start``: the first back-off (2 min) is over at 3 min, the second
        # (4 min) not yet at 3.5 min, so that tick launches nothing.
        for minutes in (0, 3, 3.5, 10):
            out = _drive(
                {ITEM: DriverStatus.ADVERSARIAL_REVIEW},
                store,
                config,
                repo,
                head,
                _launch,
                now=start + timedelta(minutes=minutes),
            )
            held.append(out.held)
            if not out.held:
                assert out.refused[ITEM].startswith("review_retrying")

        assert len(launched) == 3
        assert held == [[], [], [], [ITEM]]
        (hold,) = store.list_epic_holds(EPIC)
        assert hold.reason.startswith("review_launch_failed")
        assert "the reviewer never started" in hold.reason
        assert "timed out" in hold.reason
        assert "runtime_timeout" in hold.reason
        assert holds_node_out_of_frontier(hold.reason)
        assert safe_hold_reason(hold.reason) == hold.reason
        assert store.latest_review_round(ITEM) is None


def _silent_launcher(ctx: ReviewerLaunchContext) -> CollectedSidecar:
    """The reviewer wrote its identity event and then nothing until the
    no-output watchdog stopped its container (#2240): no verdict exists."""
    from kew.review_contract import infrastructure_failure_sidecar
    from kew.tracking import STREAM_SILENT_CATEGORY

    return infrastructure_failure_sidecar(
        ctx.log_dir,
        ctx.run_id,
        failure_layer="stream",
        error_category=STREAM_SILENT_CATEGORY,
        reason="reviewer exited 137 with no result event",
    )


def test_a_silent_reviewer_round_is_retried_and_not_counted(tmp_path: Path) -> None:
    """#2240: a reviewer stopped for a silent stream is a stalled provider, not
    a verdict — no hold, no round spent, and the next tick re-runs it."""
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path, blocking=True)
    statuses = {ITEM: DriverStatus.ADVERSARIAL_REVIEW}

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        out = _drive(statuses, store, config, repo, head, _silent_launcher)

        assert out.held == []
        assert store.list_epic_holds(EPIC) == []
        assert store.latest_review_round(ITEM) is None
        assert store.review_round_count(ITEM) == 0
        assert statuses[ITEM] == DriverStatus.ADVERSARIAL_REVIEW
        assert out.refused[ITEM].startswith("review_retrying — ")
        assert "stream_silent" in out.refused[ITEM]
        assert classify_wait(out.refused[ITEM], held=False) is None
        assert store.list_epic_reservations(REVIEW_EPIC_REF) == []
        silent = [
            r for r in store.list_by_item(ITEM) if r.error_category == "stream_silent"
        ]
        assert len(silent) == 1

        # The next tick runs the round again, and this time it lands.
        statuses = {ITEM: DriverStatus.ADVERSARIAL_REVIEW}
        out = _drive(statuses, store, config, repo, head, _ready_launcher)

        assert out.held == []
        assert out.refused == {}
        assert store.review_round_count(ITEM) == 1
        latest = store.latest_review_round(ITEM)
        assert latest is not None and latest.verdict == "READY"


def test_a_reviewer_silent_past_the_bound_is_held_naming_the_silence(
    tmp_path: Path,
) -> None:
    """The silent-round retry is bounded: once ``_MAX_SILENT_ATTEMPTS`` rounds
    in a row went silent the node is held, and the hold names the cause."""
    repo, head = _make_repo(tmp_path)
    config = _config(tmp_path, blocking=True)

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        for _ in range(review_stage._MAX_SILENT_ATTEMPTS - 1):
            statuses = {ITEM: DriverStatus.ADVERSARIAL_REVIEW}
            out = _drive(statuses, store, config, repo, head, _silent_launcher)
            assert out.held == []
            assert store.list_epic_holds(EPIC) == []

        statuses = {ITEM: DriverStatus.ADVERSARIAL_REVIEW}
        out = _drive(statuses, store, config, repo, head, _silent_launcher)

        assert out.held == [ITEM]
        assert statuses[ITEM] == DriverStatus.HELD
        holds = store.list_epic_holds(EPIC)
        assert len(holds) == 1
        assert "stream_silent" in holds[0].reason
        assert "went silent" in holds[0].reason
        assert store.latest_review_round(ITEM) is None


# --- a READY on a merge-gate-path head needs a second full READY (#3422) ---

_GATE_REASON = (
    "this PR changes merge gate paths, so kew runs an independent full review"
)


class _Prompts:
    """A reviewer double that keeps every prompt it was launched with."""

    def __init__(self, launch) -> None:
        self.launch = launch
        self.prompts: list[str] = []

    def __call__(self, ctx: ReviewerLaunchContext) -> CollectedSidecar:
        self.prompts.append(ctx.prompt)
        return self.launch(ctx)


def test_a_gate_path_ready_runs_one_confirming_full_round(tmp_path: Path):
    from kew.review_delta import full_fallback_section, remediation_rounds_spent

    repo, head = make_pr_repo(tmp_path, "scripts/verify.sh")
    config = _config(tmp_path, blocking=True, max_rounds=1)
    reviewer = _Prompts(_ready_launcher)
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        statuses = {ITEM: DriverStatus.ADVERSARIAL_REVIEW}
        out = _drive(statuses, store, config, repo, head, reviewer)
        first = store.latest_review_round(ITEM)
        assert first is not None
        assert (first.verdict, first.review_mode) == ("NEEDS_FULL", "full")
        assert out.verdicts == {ITEM: "NEEDS_FULL"}
        assert out.held == []

        statuses = {ITEM: DriverStatus.ADVERSARIAL_REVIEW}
        out = _drive(statuses, store, config, repo, head, reviewer)
        second = store.latest_review_round(ITEM)
        assert second is not None and second.round == 2
        assert (second.verdict, second.review_mode) == ("READY", "full")
        assert second.head_sha == head
        assert out.verdicts == {ITEM: "READY"}
        # The confirming round spent no remediation round: max_rounds = 1 is
        # still unspent by the two READYs.
        assert remediation_rounds_spent(store.review_rounds_in_cycle(ITEM)) == 1
        assert out.held == []

        # The READY stands: no third round is dispatched.
        statuses = {ITEM: DriverStatus.IN_REVIEW}
        _drive(statuses, store, config, repo, head, reviewer)
        assert store.review_round_count(ITEM) == 2
        assert len(reviewer.prompts) == 2

    # The confirming prompt is the first one plus the reason it runs, which
    # names no earlier round and no verdict.
    section = full_fallback_section(_GATE_REASON)
    assert section in reviewer.prompts[1]
    assert reviewer.prompts[1].replace(section, "", 1) == reviewer.prompts[0]
    assert "NEEDS_FULL" not in section and "round" not in section.split(":", 1)[1]


def test_a_ready_that_touches_no_gate_path_stands_after_one_round(tmp_path: Path):
    repo, head = make_pr_repo(tmp_path, "src/app.py")
    config = _config(tmp_path, blocking=True)
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        statuses = {ITEM: DriverStatus.ADVERSARIAL_REVIEW}
        out = _drive(statuses, store, config, repo, head, _ready_launcher)
        latest = store.latest_review_round(ITEM)
        assert latest is not None and latest.verdict == "READY"
        assert out.verdicts == {ITEM: "READY"}
        assert store.review_round_count(ITEM) == 1


def test_a_confirming_round_with_only_low_findings_stands_as_residuals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The stop rule decides the confirming round as it decides any other: an
    all-low NEEDS_CHANGES it accepts is READY-with-residuals, not a remediation."""
    from kew import subprocess_utils
    from kew.review_residuals import READY_WITH_RESIDUALS

    gh = FakeGh()
    monkeypatch.setitem(subprocess_utils.TOOL_PATHS, "gh", "gh")
    monkeypatch.setattr(
        subprocess_utils,
        "subprocess",
        GhProcessBoundary(gh, tool_paths=subprocess_utils.TOOL_PATHS),
    )

    def _low(ctx: ReviewerLaunchContext) -> CollectedSidecar:
        finding = {
            "severity": "low",
            "location": "scripts/verify.sh:1",
            "summary": "nit",
            "evidence": {"command": "uv run pytest -q", "output_digest": "sha256:c"},
        }
        return stream_verdict(ctx, {"verdict": "NEEDS_CHANGES", "findings": [finding]})

    repo, head = make_pr_repo(tmp_path, "scripts/verify.sh")
    config = DispatchConfig(
        review=ReviewConfig(
            adversarial=True,
            blocking=True,
            max_rounds=4,
            stop_rule_after_rounds=1,
            publish_comment=False,
        ),
        logging=LoggingConfig(dir=str(tmp_path / "logs")),
    )
    remediations: list[Any] = []
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        statuses = {ITEM: DriverStatus.ADVERSARIAL_REVIEW}
        _drive(statuses, store, config, repo, head, _ready_launcher)
        statuses = {ITEM: DriverStatus.ADVERSARIAL_REVIEW}
        out = drive_review_stage(
            statuses,
            {ITEM: _facts(head)},
            epic_ref=EPIC,
            config=config,
            store=store,
            repo="o/r",
            repo_root=repo,
            base="main",
            reviewer_launcher=_low,
            remediator=remediations.append,
            now=NOW,
        )
        latest = store.latest_review_round(ITEM)
        assert latest is not None and latest.round == 2
        assert latest.verdict == "NEEDS_CHANGES"
        assert out.verdicts == {ITEM: READY_WITH_RESIDUALS}
        assert remediations == []
        assert len(gh.creates()) == 1


# --- the stop rule on a merge-gate-path head (#3422) ---
#
# Rounds 1-2 reviewed an older head A; round 3 is on the head B under review
# now. A round 4 of lows past ``stop_rule_after_rounds = 3`` is residual-shaped,
# but on a gate-path head the stop rule may accept it only when it is the full
# round that confirms an earlier full READY on B.

_OLD_HEAD = "a" * 40


def _gh_boundary(monkeypatch: pytest.MonkeyPatch) -> FakeGh:
    from kew import subprocess_utils

    gh = FakeGh()
    monkeypatch.setitem(subprocess_utils.TOOL_PATHS, "gh", "gh")
    monkeypatch.setattr(
        subprocess_utils,
        "subprocess",
        GhProcessBoundary(gh, tool_paths=subprocess_utils.TOOL_PATHS),
    )
    return gh


def _low_finding() -> dict[str, Any]:
    return {
        "severity": "low",
        "location": "code.py:1",
        "summary": "nit",
        "evidence": {"command": "uv run pytest -q", "output_digest": "sha256:c"},
    }


def _lows_launcher(ctx: ReviewerLaunchContext) -> CollectedSidecar:
    sidecar = {"verdict": "NEEDS_CHANGES", "findings": [_low_finding()]}
    return stream_verdict(ctx, sidecar)


def _stop_rule_config(tmp_path: Path) -> DispatchConfig:
    return DispatchConfig(
        review=ReviewConfig(
            adversarial=True,
            blocking=True,
            max_rounds=10,
            stop_rule_after_rounds=3,
            stop_rule_max_severity="low",
            publish_comment=False,
        ),
        logging=LoggingConfig(dir=str(tmp_path / "logs")),
    )


def _rounds_before_the_lows(store: Any, head: str, round3_mode: str) -> None:
    """Round 1 a full READY on A awaiting confirmation, round 2 a full
    NEEDS_CHANGES (medium) on A that remediation answered with head B, round 3
    a READY on B recorded NEEDS_FULL in *round3_mode*."""
    store.record_review_round(
        EPIC, ITEM, head_sha=_OLD_HEAD, verdict="NEEDS_FULL", review_mode="full"
    )
    store.record_review_round(
        EPIC, ITEM, head_sha=_OLD_HEAD, verdict="NEEDS_CHANGES", review_mode="full"
    )
    store.record_review_round(
        EPIC, ITEM, head_sha=head, verdict="NEEDS_FULL", review_mode=round3_mode
    )


_LOWS_CASES = [
    # A delta round 3 never confirms: round 4 is the first full review of B.
    pytest.param(("scripts/verify.sh",), "delta", False, id="gate-head-unconfirmed"),
    # The same history on a head that touches no gate path: the rule accepts.
    pytest.param(("src/app.py",), "delta", True, id="no-gate-head"),
    # Round 4 is the full round confirming round 3's full READY on B.
    pytest.param(("scripts/verify.sh",), "full", True, id="gate-head-confirming"),
]


@pytest.mark.parametrize(("changed", "round3_mode", "accepted"), _LOWS_CASES)
def test_the_stop_rule_accepts_a_gate_path_head_only_on_a_confirming_full_round(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changed: tuple[str, ...],
    round3_mode: str,
    accepted: bool,
):
    from kew.review_residuals import READY_WITH_RESIDUALS

    gh = _gh_boundary(monkeypatch)
    repo, head = make_pr_repo(tmp_path, *changed)
    config = _stop_rule_config(tmp_path)
    remediations: list[Any] = []
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _rounds_before_the_lows(store, head, round3_mode)
        statuses = {ITEM: DriverStatus.ADVERSARIAL_REVIEW}
        out = drive_review_stage(
            statuses,
            {ITEM: _facts(head)},
            epic_ref=EPIC,
            config=config,
            store=store,
            repo="o/r",
            repo_root=repo,
            base="main",
            reviewer_launcher=_lows_launcher,
            remediator=remediations.append,
            now=NOW,
        )
        latest = store.latest_review_round(ITEM)
        assert latest is not None and latest.round == 4
        assert latest.review_mode == "full"
        if accepted:
            assert latest.verdict == "NEEDS_CHANGES"
            assert out.verdicts == {ITEM: READY_WITH_RESIDUALS}
            assert latest.accepted_verdict == READY_WITH_RESIDUALS
            assert len(gh.creates()) == 1
        else:
            # Residual-only but unconfirmed: recorded like a first READY, so
            # a confirming full round runs next instead of a remediation.
            assert latest.verdict == "NEEDS_FULL"
            assert out.verdicts == {ITEM: "NEEDS_FULL"}
            assert latest.accepted_verdict is None
            assert gh.creates() == []
            assert store.merge_ready_review_rounds(ITEM) == []
            assert remediations == []


@pytest.mark.parametrize(("changed", "round3_mode", "accepted"), _LOWS_CASES)
def test_a_standing_round_of_lows_on_a_gate_path_head_is_remediated_unless_confirming(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changed: tuple[str, ...],
    round3_mode: str,
    accepted: bool,
):
    """The standing-round path decides on a later tick, when no reviewer ran:
    kew reads the head's gate paths itself."""
    from kew.review_residuals import READY_WITH_RESIDUALS

    gh = _gh_boundary(monkeypatch)
    repo, head = make_pr_repo(tmp_path, *changed)
    config = _stop_rule_config(tmp_path)
    data = json.dumps({"verdict": "NEEDS_CHANGES", "findings": [_low_finding()]})
    sidecar = tmp_path / "round4.json"
    sidecar.write_text(data)
    remediations: list[Any] = []
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _rounds_before_the_lows(store, head, round3_mode)
        store.record_review_round(
            EPIC,
            ITEM,
            head_sha=head,
            verdict="NEEDS_CHANGES",
            sidecar_digest=hashlib.sha256(data.encode()).hexdigest(),
            sidecar_path=str(sidecar),
            review_mode="full",
        )
        statuses = {ITEM: DriverStatus.REMEDIATING}
        out = drive_review_stage(
            statuses,
            {ITEM: _facts(head)},
            epic_ref=EPIC,
            config=config,
            store=store,
            repo="o/r",
            repo_root=repo,
            base="main",
            reviewer_launcher=_lows_launcher,
            remediator=remediations.append,
            now=NOW,
        )
        latest = store.latest_review_round(ITEM)
        assert latest is not None and latest.round == 4
        if accepted:
            assert out.verdicts == {ITEM: READY_WITH_RESIDUALS}
            assert latest.accepted_verdict == READY_WITH_RESIDUALS
            assert len(gh.creates()) == 1
            assert remediations == []
        else:
            assert out.verdicts.get(ITEM) != READY_WITH_RESIDUALS
            assert latest.accepted_verdict is None
            assert gh.creates() == []
            assert len(remediations) == 1


def test_a_standing_round_whose_gate_paths_cannot_be_read_waits_a_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """An unread diff decides nothing: it is not "touches no gate path"
    (accept), and not "touches one" either (remediate a PR that may touch
    none). The round stands as it is and the next tick reads again."""
    gh = _gh_boundary(monkeypatch)
    repo, _ = make_pr_repo(tmp_path, "src/app.py")
    unreadable = "b" * 40  # in no repository
    config = _stop_rule_config(tmp_path)
    data = json.dumps({"verdict": "NEEDS_CHANGES", "findings": [_low_finding()]})
    sidecar = tmp_path / "round4.json"
    sidecar.write_text(data)
    remediations: list[Any] = []
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _rounds_before_the_lows(store, unreadable, "delta")
        store.record_review_round(
            EPIC,
            ITEM,
            head_sha=unreadable,
            verdict="NEEDS_CHANGES",
            sidecar_digest=hashlib.sha256(data.encode()).hexdigest(),
            sidecar_path=str(sidecar),
            review_mode="full",
        )
        out = drive_review_stage(
            {ITEM: DriverStatus.REMEDIATING},
            {ITEM: _facts(unreadable)},
            epic_ref=EPIC,
            config=config,
            store=store,
            repo="o/r",
            repo_root=repo,
            base="main",
            reviewer_launcher=_lows_launcher,
            remediator=remediations.append,
            now=NOW,
        )
        latest = store.latest_review_round(ITEM)
        assert latest is not None and latest.round == 4
        assert latest.accepted_verdict is None
        assert gh.creates() == []
        assert remediations == []
        assert out.held == [] and store.list_epic_holds(EPIC) == []


# --- kew's own [review] settings on a gate-path head (#3422) ---


def _kew_toml_review(tmp_path: Path) -> DispatchConfig:
    """The ``[review]`` values of kew's own kew.toml: the stop rule fires from
    round 2, and two remediation rounds are the cap."""
    return DispatchConfig(
        review=ReviewConfig(
            adversarial=True,
            blocking=True,
            max_rounds=2,
            stop_rule_after_rounds=1,
            stop_rule_max_severity="low",
            publish_comment=False,
        ),
        logging=LoggingConfig(dir=str(tmp_path / "logs")),
    )


def _medium_launcher(ctx: ReviewerLaunchContext) -> CollectedSidecar:
    finding = {**_low_finding(), "severity": "medium", "summary": "real gap"}
    return stream_verdict(ctx, {"verdict": "NEEDS_CHANGES", "findings": [finding]})


def _tick(store, config, repo, head, launcher, remediations, *, status):
    return drive_review_stage(
        {ITEM: status},
        {ITEM: _facts(head)},
        epic_ref=EPIC,
        config=config,
        store=store,
        repo="o/r",
        repo_root=repo,
        base="main",
        reviewer_launcher=launcher,
        remediator=remediations.append,
        now=NOW,
    )


@pytest.mark.parametrize(
    ("confirming", "expected"),
    [
        pytest.param(_lows_launcher, "READY_WITH_RESIDUALS", id="lows-again"),
        pytest.param(_ready_launcher, "READY", id="ready"),
    ],
)
def test_residuals_after_a_remediation_on_a_gate_path_head_are_confirmed_not_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, confirming, expected: str
):
    """R1 finds a medium and remediation pushes H2; R2, full on H2, finds only
    lows. Under kew.toml's values R1 + R2 are both rounds of the cap, so
    remediating R2 held every such PR ``review_rounds_exhausted``. R2 is
    recorded like a first READY instead, and R3 confirms it."""
    from kew.review_delta import remediation_rounds_spent

    gh = _gh_boundary(monkeypatch)
    repo, first = make_pr_repo(tmp_path, "scripts/verify.sh")
    config = _kew_toml_review(tmp_path)
    remediations: list[Any] = []
    due = DriverStatus.ADVERSARIAL_REVIEW
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _tick(store, config, repo, first, _medium_launcher, remediations, status=due)
        r1 = store.latest_review_round(ITEM)
        assert r1 is not None and r1.verdict == "NEEDS_CHANGES"
        second = add_pr_commit(repo)
        remediated = len(remediations)

        out = _tick(
            store, config, repo, second, _lows_launcher, remediations, status=due
        )
        r2 = store.latest_review_round(ITEM)
        assert r2 is not None and r2.round == 2 and r2.head_sha == second
        # One review never accepts a gate-path head, residuals or not.
        assert (r2.verdict, r2.review_mode) == ("NEEDS_FULL", "full")
        assert r2.accepted_verdict is None
        assert out.verdicts == {ITEM: "NEEDS_FULL"}
        assert gh.creates() == [] and store.merge_ready_review_rounds(ITEM) == []
        assert remediation_rounds_spent(store.review_rounds_in_cycle(ITEM)) == 1

        out = _tick(store, config, repo, second, confirming, remediations, status=due)
        r3 = store.latest_review_round(ITEM)
        assert r3 is not None and r3.round == 3 and r3.head_sha == second
        assert r3.review_mode == "full"
        assert out.verdicts == {ITEM: expected}
        assert out.held == [] and store.list_epic_holds(EPIC) == []
        assert len(remediations) == remediated
        [ready] = store.merge_ready_review_rounds(ITEM)
        assert ready.round == 3


def test_a_standing_round_decides_on_the_gate_paths_its_review_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The gate paths a reviewer dispatch read for a head serve every later
    decision on that head: the standing round needs no checkout of its own,
    so it decides even with the repository gone."""
    from kew.review_residuals import READY_WITH_RESIDUALS

    gh = _gh_boundary(monkeypatch)
    repo, head = make_pr_repo(tmp_path, "src/app.py")
    config = _kew_toml_review(tmp_path)
    remediations: list[Any] = []
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _tick(
            store,
            config,
            repo,
            head,
            _medium_launcher,
            remediations,
            status=DriverStatus.ADVERSARIAL_REVIEW,
        )
        data = json.dumps({"verdict": "NEEDS_CHANGES", "findings": [_low_finding()]})
        sidecar = tmp_path / "round2.json"
        sidecar.write_text(data)
        store.record_review_round(
            EPIC,
            ITEM,
            head_sha=head,
            verdict="NEEDS_CHANGES",
            sidecar_digest=hashlib.sha256(data.encode()).hexdigest(),
            sidecar_path=str(sidecar),
            review_mode="full",
        )
        shutil.rmtree(repo)
        remediated = len(remediations)

        out = _tick(
            store,
            config,
            repo,
            head,
            _lows_launcher,
            remediations,
            status=DriverStatus.REMEDIATING,
        )
        latest = store.latest_review_round(ITEM)
        assert latest is not None and latest.round == 2
        assert latest.accepted_verdict == READY_WITH_RESIDUALS
        assert out.verdicts == {ITEM: READY_WITH_RESIDUALS}
        assert len(gh.creates()) == 1
        assert len(remediations) == remediated


# --- the recorded gate paths answer only for their own head and patterns ---


def _standing_lows(store: Any, tmp_path: Path, head: str) -> None:
    """A full NEEDS_CHANGES round of lows on *head*, with its stored sidecar."""
    data = json.dumps({"verdict": "NEEDS_CHANGES", "findings": [_low_finding()]})
    sidecar = tmp_path / f"standing-{store.review_round_count(ITEM) + 1}.json"
    sidecar.write_text(data)
    store.record_review_round(
        EPIC,
        ITEM,
        head_sha=head,
        verdict="NEEDS_CHANGES",
        sidecar_digest=hashlib.sha256(data.encode()).hexdigest(),
        sidecar_path=str(sidecar),
        review_mode="full",
    )


def _roomy_kew_toml_review(tmp_path: Path) -> DispatchConfig:
    """kew.toml's stop rule (from round 2, lows only) with room to remediate."""
    config = _kew_toml_review(tmp_path)
    config.review.max_rounds = 10
    return config


def _assert_not_accepted(out: Any, store: Any, gh: FakeGh) -> None:
    from kew.review_residuals import READY_WITH_RESIDUALS

    latest = store.latest_review_round(ITEM)
    assert latest is not None and latest.round == 2
    assert latest.accepted_verdict is None
    assert out.verdicts.get(ITEM) != READY_WITH_RESIDUALS
    assert gh.creates() == []


@pytest.mark.parametrize("readable", [True, False], ids=["read", "unread"])
def test_gate_paths_recorded_for_another_head_never_answer_for_this_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, readable: bool
):
    """The first head's review recorded "no gate paths"; remediation then
    pushed a gate-path change. The standing round on the new head reads its
    own paths: remediated when the read works, nothing decided when it fails,
    never accepted on the old head's record."""
    gh = _gh_boundary(monkeypatch)
    repo, first = make_pr_repo(tmp_path, "src/app.py")
    config = _roomy_kew_toml_review(tmp_path)
    remediations: list[Any] = []
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _tick(
            store,
            config,
            repo,
            first,
            _medium_launcher,
            remediations,
            status=DriverStatus.ADVERSARIAL_REVIEW,
        )
        second = add_pr_commit(repo, "scripts/verify.sh")
        _standing_lows(store, tmp_path, second)
        if not readable:
            shutil.rmtree(repo)
        before = len(remediations)
        out = _tick(
            store,
            config,
            repo,
            second,
            _lows_launcher,
            remediations,
            status=DriverStatus.REMEDIATING,
        )
        _assert_not_accepted(out, store, gh)
        assert len(remediations) == before + (1 if readable else 0)
        assert out.held == []


@pytest.mark.parametrize("readable", [True, False], ids=["read", "unread"])
def test_gate_paths_recorded_under_other_patterns_are_read_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, readable: bool
):
    """The head's review recorded its gate paths under a pattern list that
    named none of them; the operator has since restored the defaults. The
    record no longer answers: the head is read again."""
    gh = _gh_boundary(monkeypatch)
    repo, head = make_pr_repo(tmp_path, "scripts/verify.sh")
    config = _roomy_kew_toml_review(tmp_path)
    defaults = list(config.epic.merge_gate_paths)
    config.epic.merge_gate_paths = ["**/nothing/"]
    remediations: list[Any] = []
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _tick(
            store,
            config,
            repo,
            head,
            _medium_launcher,
            remediations,
            status=DriverStatus.ADVERSARIAL_REVIEW,
        )
        config.epic.merge_gate_paths = defaults
        _standing_lows(store, tmp_path, head)
        if not readable:
            shutil.rmtree(repo)
        before = len(remediations)
        out = _tick(
            store,
            config,
            repo,
            head,
            _lows_launcher,
            remediations,
            status=DriverStatus.REMEDIATING,
        )
        _assert_not_accepted(out, store, gh)
        assert len(remediations) == before + (1 if readable else 0)
        assert out.held == []


# --- a head whose gate paths never read is held, not left waiting ---


def _unread_ticks(store, config, repo, head, remediations, count: int) -> list[Any]:
    return [
        _tick(
            store,
            config,
            repo,
            head,
            _lows_launcher,
            remediations,
            status=DriverStatus.REMEDIATING,
        )
        for _ in range(count)
    ]


def _gate_head_with_standing_lows(store: Any, tmp_path: Path, head: str) -> None:
    store.record_review_round(
        EPIC, ITEM, head_sha=head, verdict="NEEDS_CHANGES", review_mode="full"
    )
    _standing_lows(store, tmp_path, head)


def _assert_held_on_the_read(out: Any, store: Any, head: str) -> None:
    assert out.held == [ITEM]
    [hold] = [h for h in store.list_epic_holds(EPIC) if h.item_id == ITEM]
    assert hold.reason.startswith("review_gate_paths_unread: ")
    assert head[:12] in hold.reason
    assert holds_node_out_of_frontier(hold.reason)


def test_a_head_whose_gate_paths_never_read_is_held_naming_the_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    gh = _gh_boundary(monkeypatch)
    repo, head = make_pr_repo(tmp_path, "scripts/verify.sh")
    config = _roomy_kew_toml_review(tmp_path)
    remediations: list[Any] = []
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _gate_head_with_standing_lows(store, tmp_path, head)
        shutil.rmtree(repo)
        for out in _unread_ticks(store, config, repo, head, remediations, 4):
            assert out.held == []
        assert store.list_epic_holds(EPIC) == []
        [out] = _unread_ticks(store, config, repo, head, remediations, 1)
        _assert_held_on_the_read(out, store, head)
        assert remediations == [] and gh.creates() == []


def test_a_gate_path_read_that_succeeds_starts_the_unread_count_over(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _gh_boundary(monkeypatch)
    repo, head = make_pr_repo(tmp_path, "scripts/verify.sh")
    away = tmp_path / "away"
    config = _roomy_kew_toml_review(tmp_path)
    remediations: list[Any] = []
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _gate_head_with_standing_lows(store, tmp_path, head)
        repo.rename(away)
        for out in _unread_ticks(store, config, repo, head, remediations, 4):
            assert out.held == []
        away.rename(repo)
        [out] = _unread_ticks(store, config, repo, head, remediations, 1)
        assert out.held == [] and len(remediations) == 1  # read: a gate head
        # A changed pattern list voids the record, so the head is read again.
        config.epic.merge_gate_paths = [*config.epic.merge_gate_paths, "**/extra/"]
        repo.rename(away)
        for out in _unread_ticks(store, config, repo, head, remediations, 4):
            assert out.held == []
        assert store.list_epic_holds(EPIC) == []
        [out] = _unread_ticks(store, config, repo, head, remediations, 1)
        _assert_held_on_the_read(out, store, head)
