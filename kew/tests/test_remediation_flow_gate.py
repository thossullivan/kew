"""Keep remediation policy exceptions outside ordinary dispatch admission."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from dispatch_owner_fixture import admission_owner as admission_owner

from kew.cli import _command_exit_code
from kew.config import DispatchConfig, SandboxConfig
from kew.sandbox import SandboxRuntimeUnavailable, resolve_effective_sandbox


@pytest.fixture
def review_retry_scene(tmp_path):
    """Real admission and publication policy with mocked external effects."""
    from kew import review
    from kew.review_checkout import ReviewCheckout
    from kew.review_contract import Finding
    from kew.run_store import SqliteRunStore
    from kew.runners.claude_code import ClaudeCodeRunner
    from kew.runners.registry import ResolvedRoute
    from kew.tracking import DispatchResult

    config = DispatchConfig(sandbox=SandboxConfig(enabled=True))
    config.project.repo = "o/r"
    config.review.adversarial = True
    checkout = ReviewCheckout(
        tmp_path / "checkout", "a" * 40, "agent/issue-42", True, None, None
    )
    with (
        SqliteRunStore(tmp_path / "runs.db") as store,
        patch(
            "kew.review._get_pr_metadata",
            return_value={
                "headRefName": checkout.branch,
                "headRefOid": checkout.head_sha,
                "baseRefName": "main",
                "body": "Closes #42",
            },
        ),
        patch("kew.review._check_github_connectivity", return_value=True),
        patch(
            "kew.review.resolve_runner",
            return_value=ResolvedRoute(ClaudeCodeRunner(), "claude-opus-5"),
        ),
        patch(
            "kew.runners.resolve_runner",
            return_value=ResolvedRoute(ClaudeCodeRunner(), "claude-opus-5"),
        ),
        patch("kew.runner_preflight.runner_preflight_failure", return_value=None),
        patch("kew.sandbox.detect_runtime_cached", return_value=None),
        patch("kew.remediation_containment.detect_runtime_cached", return_value=None),
        patch(
            "kew.review.materialize_review_checkout", return_value=checkout
        ) as materialize,
        patch(
            "kew.review.run_tracked_agent",
            return_value=DispatchResult(
                exit_code=0, issue_num=7, log_file=tmp_path / "run.log"
            ),
        ) as launch,
        patch(
            "kew.review._canonical_review_publication",
            return_value=("b" * 40, False),
        ) as publish,
        patch("kew.review.discard_checkout"),
        patch("kew.review_outcome._notify"),
    ):
        yield SimpleNamespace(
            config=config,
            store=store,
            checkout=checkout,
            materialize=materialize,
            launch=launch,
            publish=publish,
            # Public exit code: the CLI maps the review's CommandOutcome
            # (2 = could not run, 1 = ran and failed, 0 = success).
            dispatch=lambda item_id=None, on_outcome=None: _command_exit_code(
                review.dispatch_review(
                    pr_number=7,
                    config=config,
                    store=store,
                    item_id=item_id,
                    findings=[Finding("high", "a:1", "gap", "pytest", "sha256:a")],
                    on_outcome=on_outcome,
                )
            ),
        )


@pytest.mark.parametrize("path", ["normal", "cleanup_failure", "after_launch"])
def test_terminal_outcome_delivered_once_before_cleanup(review_retry_scene, path):
    import sqlite3

    from kew.review_outcome import latest_review_outcome, record_review_outcome

    scene = review_retry_scene
    scene.config.sandbox.allow_uncontained_flows = True
    steps = []
    outcomes = []
    release = scene.store.release_epic_node_item
    generations = []

    def record(outcome, **kwargs):
        steps.append("record")
        record_review_outcome(outcome, **kwargs)

    def callback(outcome):
        steps.append("callback")
        outcomes.append(outcome)

    def cleanup(item_id, *, run_id):
        steps.append("cleanup")
        [reservation] = scene.store.list_epic_reservations("review")
        generations.append((item_id, run_id, reservation.run_id))
        if path == "cleanup_failure":
            raise sqlite3.OperationalError("database is locked")
        release(item_id, run_id=run_id)

    if path == "after_launch":

        def launched_then_failed(**kwargs):
            # The agent launched before the failure: an executed failure (1).
            kwargs["launch_state"].launched = True
            raise RuntimeError("injected failure after launch")

        scene.launch.side_effect = launched_then_failed
    with (
        patch("kew.review.record_review_outcome", side_effect=record),
        patch.object(scene.store, "release_epic_node_item", side_effect=cleanup),
    ):
        if path == "cleanup_failure":
            with pytest.raises(sqlite3.OperationalError, match="database is locked"):
                scene.dispatch("42", callback)
        else:
            assert scene.dispatch("42", callback) == (
                1 if path == "after_launch" else 0
            )

    assert len(outcomes) == 1
    assert steps == ["record", "callback", "cleanup"]
    assert latest_review_outcome(scene.store, 7) == outcomes[0]
    assert outcomes[0].classification == (
        "dispatch_error" if path == "after_launch" else "completed"
    )
    [(item_id, released_generation, owned_generation)] = generations
    assert item_id == "42"
    assert released_generation == owned_generation


def test_callback_failure_does_not_redeliver_terminal_outcome(review_retry_scene):
    from kew.review_outcome import list_review_outcomes

    scene = review_retry_scene
    scene.config.sandbox.allow_uncontained_flows = True
    outcomes = []

    def callback(outcome):
        outcomes.append(outcome)
        if len(outcomes) == 1:
            raise RuntimeError("injected outcome callback failure")

    with patch(
        "kew.runner_preflight.runner_preflight_failure", return_value="runner absent"
    ):
        # A runner preflight refusal never launched: public exit 2 (could not run).
        assert scene.dispatch("42", callback) == 2

    assert len(outcomes) == 1
    assert outcomes[0].classification == "runner_preflight"
    assert list_review_outcomes(scene.store, pr_number=7) == outcomes
    assert scene.store.list_epic_reservations("review") == []
    scene.launch.assert_not_called()


def _seed_retry_producer(scene, *, intent):
    from datetime import UTC, datetime

    from kew.audit import Actor, emit
    from kew.readiness import (
        Containment,
        RealizedLaunchBoundary,
        compose_realized_profile,
        compose_route_profile,
    )
    from kew.run_store import RunRecord

    scene.store.save(
        RunRecord(
            run_id="producer",
            issue_num=42,
            item_id="42",
            branch=scene.checkout.branch,
            status="success",
            exit_code=0,
            started_at=datetime(2026, 9, 1, tzinfo=UTC),
            runner_name="claude-code",
            model="claude-opus-5",
        )
    )
    scene.store.record_route_profile(
        compose_realized_profile(
            run_id="producer",
            attempt_number=1,
            route=compose_route_profile(
                runner="claude-code", requested_model="claude-opus-5"
            ),
            boundary=RealizedLaunchBoundary(
                containment=Containment.NATIVE, auth_path="cli-login"
            ),
        )
    )
    if intent:
        emit(
            scene.store,
            "publication_intent",
            actor=Actor("policy", "host-publication"),
            run_id="producer",
            issue_num=42,
            item_id="42",
            payload={
                "branch": scene.checkout.branch,
                "commit_sha": scene.checkout.head_sha,
            },
        )


def test_standalone_refusal_records_explicit_no_item(review_retry_scene):
    scene = review_retry_scene
    assert scene.dispatch() == 2
    [row] = scene.store.list_runs_by_branch(scene.checkout.branch)
    assert row.item_id == ""
    assert row.issue_num == 7


def _reserve_live_remediation(store):
    from datetime import UTC, datetime

    from kew.run_store import RunRecord

    assert store.reserve_epic_node_item("review", "42", "active-reservation")
    store.save(
        RunRecord(
            run_id="active-remediation",
            issue_num=42,
            item_id="42",
            branch="agent/issue-42",
            status="pending",
            exit_code=0,
            started_at=datetime.now(UTC),
            run_kind="review_remediation",
        )
    )


def _assert_retained_refusal(store):
    [posture] = store.list_audit_events(event="sandbox_resolved")
    [completed] = store.list_audit_events(event="dispatch_completed")
    row = store.get(posture.run_id)
    assert row is not None
    assert (row.status, row.error_category, row.exit_code) == (
        "refused",
        "containment_refused",
        2,
    )
    assert row.finished_at is not None
    assert completed.run_id == row.run_id
    assert posture.payload["posture"] == "refused"
    assert posture.payload["reason"] == "runtime_unavailable"
    assert completed.payload["reason"] == "runtime_unavailable"
    assert completed.payload["exit_code"] == 2
    return row


@pytest.mark.parametrize("flow", ["resolve", "review"])
def test_concurrent_refusal_preserves_active_reservation(review_retry_scene, flow):
    from datetime import UTC, datetime

    from kew import resolve, review
    from kew.graph.driver.tick import reconcile_epic_reservations

    scene = review_retry_scene
    if flow == "resolve":
        _reserve_live_remediation(scene.store)
        with (
            patch(
                "kew.resolve._get_pr_metadata",
                return_value={
                    "headRefName": scene.checkout.branch,
                    "headRefOid": scene.checkout.head_sha,
                    "baseRefName": "main",
                    "body": "Closes #42",
                    "state": "OPEN",
                    "mergeable": "CONFLICTING",
                },
            ),
            patch("kew.resolve._check_github_connectivity", return_value=True),
            patch("kew.resolve.checkout_existing_worktree") as checkout,
            patch("kew.resolve.run_tracked_agent") as launch,
        ):
            assert (
                _command_exit_code(
                    resolve.dispatch_resolve(
                        pr_number=7, config=scene.config, store=scene.store
                    )
                )
                == 2
            )
        checkout.assert_not_called()
        launch.assert_not_called()
    else:
        record = review.record_remediation_refusal

        def replace_owner_before_write(*args, **kwargs):
            # Another actor releases the old generation before its refusal saves.
            [old] = scene.store.list_epic_reservations("review")
            scene.store.release_epic_node_item("42", run_id=old.run_id)
            _reserve_live_remediation(scene.store)
            record(*args, **kwargs)

        with patch(
            "kew.review.record_remediation_refusal",
            side_effect=replace_owner_before_write,
        ):
            assert scene.dispatch("42") == 2

    refusal = _assert_retained_refusal(scene.store)
    assert refusal.item_id == "42"
    scene.materialize.assert_not_called()
    scene.launch.assert_not_called()
    assert reconcile_epic_reservations(
        "review", scene.store, scene.config, now=datetime.now(UTC)
    ) == {"42"}
    [reservation] = scene.store.list_epic_reservations("review")
    assert reservation.run_id == "active-reservation"
    assert scene.store.get("active-remediation").status == "pending"
    assert not scene.store.reserve_epic_node_item("review", "42", "second")
    assert scene.store.get(refusal.run_id) == refusal


@pytest.mark.parametrize("item_id", ["42", "SFD-42"])
def test_owned_review_refusal_binds_reservation_and_reconciles(
    review_retry_scene, item_id
):
    from datetime import UTC, datetime

    from kew.graph.driver.tick import reconcile_epic_reservations

    scene = review_retry_scene
    # Leave cleanup to reconciliation, as after a crash following persistence.
    with patch.object(scene.store, "release_epic_node_item"):
        assert scene.dispatch(item_id) == 2
    [reservation] = scene.store.list_epic_reservations("review")
    refusal = _assert_retained_refusal(scene.store)
    assert refusal.run_id == reservation.run_id
    assert refusal.item_id == item_id
    assert (
        reconcile_epic_reservations(
            "review", scene.store, scene.config, now=datetime.now(UTC)
        )
        == set()
    )
    assert scene.store.reserve_epic_node_item("review", item_id, "next")
    assert scene.store.get(refusal.run_id) == refusal


@pytest.mark.parametrize("refusal_count", [32, 33])
@pytest.mark.parametrize("item_id", [None, "42"], ids=["standalone", "governed"])
@pytest.mark.parametrize("intent", [False, True], ids=["legacy", "intent"])
def test_refusal_history_does_not_block_review_retry(
    review_retry_scene, refusal_count, item_id, intent
):
    scene = review_retry_scene
    for _ in range(refusal_count):
        assert scene.dispatch() == 2
    scene.materialize.assert_not_called()
    scene.launch.assert_not_called()
    # Insert the older producer after recording refusals to isolate retry admission.
    _seed_retry_producer(scene, intent=intent)

    scene.config.sandbox.allow_uncontained_flows = True
    assert scene.dispatch(item_id) == 0

    scene.materialize.assert_called_once()
    scene.launch.assert_called_once()
    # A hand-started review resolves the PR's owning item from the head branch,
    # so its row names the branch exactly as a governed remediation's does and
    # its publication records an intent the next round can bind to (#2446). The
    # row still carries only the item it was handed: `item_id` is the key the
    # fleet counts the run by, never the branch owner it publishes under.
    assert scene.launch.call_args.kwargs["branch"] == scene.checkout.branch
    assert scene.launch.call_args.kwargs["item_id"] == item_id
    assert scene.publish.call_args.kwargs["producer_run_id"] == "producer"
    assert scene.store.list_epic_reservations("review") == []


@pytest.mark.parametrize("refusal_count", [32, 33])
def test_refusal_history_does_not_fill_governed_branch_window(
    review_retry_scene, refusal_count
):
    from kew.review import _remediation_row_branch

    scene = review_retry_scene
    for _ in range(refusal_count):
        assert scene.dispatch() == 2
    scene.config.sandbox.allow_uncontained_flows = True

    assert (
        _remediation_row_branch(scene.store, "42", scene.checkout.branch)
        == scene.checkout.branch
    )
    assert scene.dispatch("42") == 0
    scene.materialize.assert_called_once()
    scene.launch.assert_called_once()


@pytest.mark.parametrize("intent", [False, True], ids=["legacy", "intent"])
@pytest.mark.parametrize("prior_item", [None, "7", "43"])
def test_refuse_then_enable_exception_recovers_producer(
    review_retry_scene, intent, prior_item
):
    from kew.publication_credentials import (
        _attested_head_producer,
        publication_producer_for_head,
    )

    scene = review_retry_scene
    _seed_retry_producer(scene, intent=intent)
    assert scene.dispatch() == 2
    scene.materialize.assert_not_called()
    if prior_item is not None:
        # Recover existing rows from the buggy recorder and other governed items.
        row = scene.store.list_runs_by_branch(scene.checkout.branch)[0]
        row.item_id = prior_item
        scene.store.save(row)
    scene.config.sandbox.allow_uncontained_flows = True
    assert scene.dispatch("42") == 0
    assert (
        publication_producer_for_head(
            scene.store, branch=scene.checkout.branch, head_sha=scene.checkout.head_sha
        )
        == "producer"
    )
    if intent:
        assert (
            _attested_head_producer(
                scene.store,
                branch=scene.checkout.branch,
                head_sha=scene.checkout.head_sha,
            )
            == "producer"
        )
    scene.publish.assert_called_once()
    assert scene.publish.call_args.kwargs["producer_run_id"] == "producer"
    assert scene.launch.call_args.kwargs["branch"] == scene.checkout.branch
    assert scene.store.list_epic_reservations("review") == []


@pytest.mark.parametrize(
    ("runner", "runtime", "reason"),
    [
        ("claude-code", None, "runtime_unavailable"),
        ("pi", "docker", "image_unmapped"),
        ("claude-code", "docker", "credential_missing"),
    ],
)
def test_dispatch_does_not_use_remediation_exception(runner, runtime, reason):
    config = SandboxConfig(enabled=True, allow_uncontained_flows=True)
    with (
        patch("kew.sandbox.detect_runtime_cached", return_value=runtime),
        patch.dict("os.environ", {}, clear=True),
        pytest.raises(SandboxRuntimeUnavailable) as refused,
    ):
        resolve_effective_sandbox(config, runner=runner)
    assert refused.value.reason == reason


@pytest.mark.parametrize("flow", ["review", "resolve"])
@pytest.mark.parametrize("checkout_raises", [False, True])
@pytest.mark.parametrize("malformed_settings", [False, True])
def test_required_containment_precedes_checkout(
    tmp_path, flow, checkout_raises, malformed_settings
):
    from kew import resolve, review
    from kew.review_checkout import ReviewCheckout, review_checkout_path
    from kew.review_contract import Finding
    from kew.run_store import SqliteRunStore
    from kew.runners.claude_code import ClaudeCodeRunner
    from kew.runners.registry import ResolvedRoute
    from kew.sandbox import command_guard_state
    from kew.worktree import ConflictInfo, dispatch_checkout_path

    config = DispatchConfig(sandbox=SandboxConfig(enabled=True))
    config.branches.worktree_base = str(tmp_path / "checkouts")
    checkout_dir = (
        review_checkout_path(config, 7)
        if flow == "review"
        else dispatch_checkout_path(config, issue_num=42)
    )
    (checkout_dir / ".git").mkdir(parents=True)
    (checkout_dir / ".git" / "config").write_bytes(b"[core]\n\tbare = false\n")
    (checkout_dir / "dirty.bin").write_bytes(bytes(range(256)))
    if malformed_settings:
        (checkout_dir / ".claude").mkdir()
        (checkout_dir / ".claude" / "settings.json").write_bytes(
            b"[" * 20_000 + b"]" * 20_000
        )

    def snapshot() -> dict[Path, bytes | None]:
        return {
            path.relative_to(checkout_dir.parent): (
                path.read_bytes() if path.is_file() else None
            )
            for path in checkout_dir.parent.rglob("*")
        }

    before = snapshot()
    conflict = ConflictInfo("merge", ["dirty.bin"], "main")
    checkout = (
        ReviewCheckout(checkout_dir, "a" * 40, "agent/issue-42", False, None, conflict)
        if flow == "review"
        else (checkout_dir, conflict)
    )
    materializer = (
        "materialize_review_checkout"
        if flow == "review"
        else "checkout_existing_worktree"
    )
    metadata = {
        "headRefName": "agent/issue-42",
        "headRefOid": "a" * 40,
        "baseRefName": "main",
        "body": "Closes #42",
        "state": "OPEN",
        "mergeable": "CONFLICTING",
    }
    with (
        SqliteRunStore(tmp_path / "runs.db") as store,
        patch(f"kew.{flow}._get_pr_metadata", return_value=metadata),
        patch(f"kew.{flow}._check_github_connectivity", return_value=True),
        patch(
            f"kew.{flow}.resolve_runner",
            return_value=ResolvedRoute(ClaudeCodeRunner(), "claude-opus-5"),
        ),
        patch("kew.sandbox.detect_runtime_cached", return_value=None),
        patch(
            f"kew.{flow}.{materializer}",
            return_value=checkout,
            side_effect=RuntimeError("checkout failed") if checkout_raises else None,
        ) as materialize,
        patch("kew.runner_preflight.runner_preflight_failure") as credential_preflight,
        # This branch calls ensure_publication_credentials, which wraps the
        # same requirement with revocation recovery and retries it, so the
        # preflight seam moved with the merge.
        patch(f"kew.{flow}.ensure_publication_credentials") as publication_preflight,
        patch(f"kew.{flow}.run_tracked_agent") as launch,
        patch("kew.sandbox.command_guard_state", wraps=command_guard_state) as guard,
    ):
        try:
            if flow == "review":
                rc = _command_exit_code(
                    review.dispatch_review(
                        pr_number=7,
                        config=config,
                        store=store,
                        item_id="42",
                        findings=[
                            Finding(
                                "high",
                                "dirty.bin:1",
                                "unchecked path",
                                "pytest",
                                "sha256:a",
                            )
                        ],
                    )
                )
            else:
                rc = _command_exit_code(
                    resolve.dispatch_resolve(pr_number=7, config=config, store=store)
                )
        finally:
            if malformed_settings:
                guard.assert_not_called()

        materialize.assert_not_called()
        credential_preflight.assert_not_called()
        publication_preflight.assert_not_called()
        launch.assert_not_called()
        _assert_containment_gate_result(store, rc, snapshot(), before, flow, metadata)


def _assert_containment_gate_result(store, rc, after, before, flow, metadata):
    from kew.review_outcome import latest_review_outcome

    assert after == before
    assert rc == 2
    retained = _assert_retained_refusal(store)
    [event] = store.list_audit_events(event="sandbox_resolved")
    assert event.payload["command_guard"] == "not-applicable"
    [row] = store.list_runs_by_branch("agent/issue-42")
    assert row == retained
    if flow == "review":
        outcome = latest_review_outcome(store, 7)
        assert outcome.classification == "containment_refused"
        assert outcome.exit_code == 2
        assert outcome.head_sha == metadata["headRefOid"]
        assert store.list_epic_reservations("review") == []


@pytest.mark.parametrize("flow", ["review", "resolve"])
@pytest.mark.parametrize(
    "failed_write",
    [
        "save",
        "sandbox_resolved",
        "dispatch_completed",
        "recorder",
        "unconfirmed_sandbox_resolved",
        "unconfirmed_dispatch_completed",
    ],
)
def test_refusal_recording_failure_preserves_terminal_result(
    tmp_path, flow, failed_write
):
    from kew import resolve, review
    from kew.review_contract import Finding
    from kew.run_store import SqliteRunStore
    from kew.runners.claude_code import ClaudeCodeRunner
    from kew.runners.registry import ResolvedRoute

    config = DispatchConfig(sandbox=SandboxConfig(enabled=True))
    metadata = {
        "headRefName": "agent/issue-42",
        "headRefOid": "a" * 40,
        "baseRefName": "main",
        "body": "Closes #42",
        "state": "OPEN",
        "mergeable": "CONFLICTING",
    }
    with (
        SqliteRunStore(tmp_path / "runs.db") as store,
        patch(f"kew.{flow}._get_pr_metadata", return_value=metadata),
        patch(f"kew.{flow}._check_github_connectivity", return_value=True),
        patch(
            f"kew.{flow}.resolve_runner",
            return_value=ResolvedRoute(ClaudeCodeRunner(), "claude-opus-5"),
        ),
        patch("kew.sandbox.detect_runtime_cached", return_value=None),
        patch(f"kew.{flow}._error") as error,
        patch(f"kew.{flow}.run_tracked_agent") as launch,
    ):
        append = store.append_audit_event

        def fail_event(**kwargs):
            if kwargs["event"] == failed_write:
                raise OSError("injected refusal write failure")
            if failed_write == f"unconfirmed_{kwargs['event']}":
                return None
            return append(**kwargs)

        target = (
            f"kew.{flow}.record_remediation_refusal"
            if failed_write == "recorder"
            else None
        )
        failing_write = (
            patch(target, side_effect=OSError("injected refusal write failure"))
            if target
            else patch.object(
                store,
                "save" if failed_write == "save" else "append_audit_event",
                side_effect=(
                    OSError("injected refusal write failure")
                    if failed_write == "save"
                    else fail_event
                ),
            )
        )
        with failing_write:
            try:
                rc = _command_exit_code(
                    review.dispatch_review(
                        pr_number=7,
                        config=config,
                        store=store,
                        item_id="42",
                        findings=[Finding("high", "a:1", "gap", "pytest", "sha256:a")],
                    )
                    if flow == "review"
                    else resolve.dispatch_resolve(
                        pr_number=7, config=config, store=store
                    )
                )
            except OSError:
                rc = 1

        _assert_recording_failure_result(
            rc, failed_write, error, launch, store, flow, metadata
        )


def _assert_recording_failure_result(
    rc, failed_write, error, launch, store, flow, metadata
):
    from kew.review_outcome import latest_review_outcome

    assert rc == 2
    diagnostic = (
        "persistence was not confirmed"
        if failed_write.startswith("unconfirmed_")
        else "injected refusal write failure"
    )
    assert any(
        "Failed to record containment refusal" in call.args[0]
        and diagnostic in call.args[0]
        for call in error.call_args_list
    )
    launch.assert_not_called()
    assert store.list_epic_reservations("review") == []
    if flow == "review":
        outcome = latest_review_outcome(store, 7)
        assert (
            outcome.classification,
            outcome.stage,
            outcome.exit_code,
            outcome.head_sha,
        ) == (
            "containment_refused",
            "pre_launch",
            2,
            metadata["headRefOid"],
        )


@pytest.fixture
def pinned_dispatch_scene(admission_owner, tmp_path, monkeypatch):
    """Real dispatch admission and bindings; replace only external execution seams."""
    from kew import dispatch_core as core
    from kew import dispatch_sandbox as sandbox
    from kew.dispatch_admission import PendingAdmission
    from kew.overlap import DispatchOutcome
    from kew.run_store import SqliteRunStore
    from kew.runners import get_runner
    from kew.runners.registry import ResolvedRoute
    from kew.sandbox import SandboxDecision

    class ChildRunning(BaseException):
        pass

    class LiveChildAdmission(PendingAdmission):
        """``ChildRunning`` unwinds a live child, not an interrupted admission."""

        def __exit__(self, exc_type, exc, traceback):
            if isinstance(exc, ChildRunning):
                return False
            return super().__exit__(exc_type, exc, traceback)

    monkeypatch.setattr(core, "PendingAdmission", LiveChildAdmission)
    monkeypatch.setattr(sandbox, "PendingAdmission", LiveChildAdmission)

    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")
    agent = get_runner("claude-code")
    route = ResolvedRoute(agent, "claude-sonnet-4-6")
    launches = []
    path = {"sandboxed": False}
    retry_loop = core._run_retry_loop
    container_stream = sandbox.run_container_and_stream

    def running(*args, **kwargs):
        launches.append(True)
        raise ChildRunning

    def workspace(ctx, setup, **kwargs):
        setup.worktree_dir = tmp_path
        setup.run_sandboxed = path["sandboxed"]
        if setup.run_sandboxed:
            return core._launch_sandboxed(
                ctx,
                tmp_path,
                kwargs["early"],
                None,
                image=config.sandbox.image,
                inference_transport=None,
                sandbox_decision=SandboxDecision(
                    sandboxed=True,
                    posture="sandboxed",
                    image=config.sandbox.image,
                    runtime="docker",
                    notice=None,
                ),
            )
        return core._launch_host(ctx, tmp_path, route, agent, kwargs["early"], None)

    monkeypatch.setattr(core, "_run_admission_gates", lambda _: (None, None))
    monkeypatch.setattr(core, "_run_approval_gate", lambda _: None)
    monkeypatch.setattr(core, "_check_connectivity_gate", lambda _: None)
    monkeypatch.setattr(core, "_capture_root_checkout_state", lambda *_a, **_k: None)
    monkeypatch.setattr(core, "_check_existing_pr", lambda *_: (False, None))
    monkeypatch.setattr(core, "resolve_runner", lambda *_a, **_k: route)
    monkeypatch.setattr(core, "_preflight_pricing", lambda *_: None)
    monkeypatch.setattr(core, "_require_dispatch_transport_admission", lambda *_: None)
    monkeypatch.setattr(core, "_require_final_runner_preflight", lambda *_: None)
    monkeypatch.setattr(
        core, "_bind_host_checkout_before_runner", lambda *_a, **_k: "g"
    )
    monkeypatch.setattr(core, "_build_repo_map", lambda *_: "")
    monkeypatch.setattr(core, "build_prompt", lambda **_: "prompt")
    monkeypatch.setattr(core, "_launch_workspace", workspace)
    monkeypatch.setattr(core, "_run_retry_loop", running)
    monkeypatch.setattr(core, "_detect_sandbox_runtime", lambda _: "/mock/docker")
    monkeypatch.setattr(
        sandbox, "_resolve_launch_image_id", lambda *_a, **_k: "sha256:" + "f" * 64
    )
    monkeypatch.setattr(sandbox, "_contained_auth_path", lambda _: "workload-identity")
    monkeypatch.setattr(
        sandbox, "_capture_launch_evidence_or_finalize", lambda *_a, **_k: None
    )
    monkeypatch.setattr(sandbox, "run_container_and_stream", running)

    with SqliteRunStore(tmp_path / "runs.db") as store:

        def dispatch(expected_id, generation=None):
            import inspect

            # Keep the baseline executable so its failure is a behavior assertion.
            identity = {
                "expected_reservation_run_id": expected_id,
                "expected_reservation_generation": generation or expected_id,
            }
            supported = inspect.signature(core.execute_dispatch).parameters
            identity = {
                key: value for key, value in identity.items() if key in supported
            }
            try:
                return core.execute_dispatch(
                    issue={"number": 42, "title": "t"},
                    config=config,
                    store=store,
                    epic_ref="alpha",
                    approved=True,
                    model_override=route.model,
                    **identity,
                )
            except ChildRunning:
                return DispatchOutcome(
                    exit_code=0, changed_files=[], category="launched"
                )

        yield SimpleNamespace(
            store=store,
            config=config,
            dispatch=dispatch,
            launches=launches,
            path=path,
            retry_loop=retry_loop,
            container_stream=container_stream,
        )


@pytest.mark.parametrize("sandboxed", [False, True])
@pytest.mark.parametrize("replace_at", ["before_admission", "admission_transaction"])
def test_delayed_child_cannot_adopt_replacement(
    pinned_dispatch_scene, monkeypatch, sandboxed, replace_at
):
    from datetime import UTC, datetime, timedelta

    from kew.graph.driver.tick import (
        reconcile_epic_reservations,
        resolve_reservation_run,
    )

    scene = pinned_dispatch_scene
    scene.path["sandboxed"] = sandboxed
    store = scene.store
    assert store.reserve_epic_node_item("alpha", "42", "A")
    store._execute(
        "UPDATE epic_reservations SET reserved_at = ? WHERE run_id = ?",
        ((datetime.now(UTC) - timedelta(seconds=602)).isoformat(), "A"),
    )
    store._conn.commit()
    [a] = store.list_epic_reservations("alpha")

    def replace():
        assert (
            reconcile_epic_reservations(
                "alpha",
                store,
                scene.config,
                now=a.reserved_at + timedelta(seconds=601),
            )
            == set()
        )
        assert store.reserve_epic_node_item("alpha", "42", "B")

    if replace_at == "before_admission":
        replace()
    else:
        original = store.build_admission_identity

        def replace_during_admission(*args, **kwargs):
            replace()
            monkeypatch.setattr(store, "build_admission_identity", original)
            return original(*args, **kwargs)

        monkeypatch.setattr(store, "build_admission_identity", replace_during_admission)

    outcome = scene.dispatch("A")
    assert (outcome.category, outcome.reason, outcome.exit_code) == (
        "reservation_replaced",
        "reservation_replaced",
        2,
    )
    [b] = store.list_epic_reservations("alpha")
    assert (scene.launches, b.run_id) == ([], "B")
    assert store.list_audit_events(event="dispatch_reservation_bound") == []
    [refusal] = [row for row in store.list_by_item("42") if row.status == "refused"]
    assert (refusal.run_id not in {"A", "B"}, refusal.error_category) == (
        True,
        "reservation_replaced",
    )
    assert resolve_reservation_run(store, b) is None
    _assert_replacement_child_admits(scene, b)


def _assert_replacement_child_admits(scene, b):
    from datetime import timedelta

    from kew.audit_chain import verify_chain
    from kew.graph.driver.tick import (
        reconcile_epic_reservations,
        resolve_reservation_run,
    )

    store = scene.store
    assert reconcile_epic_reservations(
        "alpha",
        store,
        scene.config,
        now=b.reserved_at + timedelta(seconds=1),
    ) == {"42"}
    assert scene.dispatch("B").category == "launched"
    assert scene.launches == [True]
    child = resolve_reservation_run(store, b)
    assert child.status == "pending"
    [binding] = store.list_audit_events(event="dispatch_reservation_bound")
    assert binding.run_id == child.run_id
    assert binding.payload == {
        "reservation_run_id": "B",
        "epic_ref": "alpha",
        "item_id": "42",
        "pinned": True,
    }
    assert verify_chain(store.read_chain_rows()).ok


def test_replaced_reservation_refusal_releases_and_publishes_nothing(
    pinned_dispatch_scene,
):
    from kew import dispatch_core as core
    from kew import dispatch_sandbox as sandbox

    scene = pinned_dispatch_scene
    assert scene.store.reserve_epic_node_item("alpha", "42", "B")
    with (
        patch.object(scene.store, "release_epic_node_item") as release,
        patch.object(core, "_publish_terminal_failure") as report_failure,
        patch.object(core, "_collect_result") as collect,
        patch.object(sandbox, "_publish_completed_sandbox") as publish,
        patch.object(core, "reset_issue_to_ready") as reset,
    ):
        outcome = scene.dispatch("A")
    assert outcome.category == "reservation_replaced"
    release.assert_not_called()
    report_failure.assert_not_called()
    collect.assert_not_called()
    publish.assert_not_called()
    reset.assert_not_called()
    assert scene.launches == []
    assert scene.store.list_audit_events(event="publication_intent") == []


@pytest.mark.parametrize("gate", ["worker_cap", "budget", "containment", "transport"])
@pytest.mark.parametrize("held", [False, True])
@pytest.mark.parametrize("replace_at", ["gate", "transaction"])
@pytest.mark.parametrize("through_child", [False, True])
def test_round16_pre_admission_replacement_preserves_owner(
    tmp_path, monkeypatch, gate, held, replace_at, through_child
):
    from unittest.mock import Mock

    from kew import dispatch_core as core
    from kew.budget import BudgetExceeded, BudgetStatus
    from kew.run_store import SqliteRunStore
    from kew.runners import get_runner
    from kew.runners.registry import ResolvedRoute

    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")
    route = ResolvedRoute(get_runner("claude-code"), "claude-sonnet-4-6")
    rollback = Mock()
    monkeypatch.setattr(core, "_capture_root_checkout_state", lambda *_a, **_k: None)
    monkeypatch.setattr(
        core, "_warn_if_scheduler_does_not_own_cap_warning", lambda _: None
    )
    monkeypatch.setattr(core, "_check_github_connectivity", lambda _: True)
    monkeypatch.setattr(core, "resolve_runner", lambda *_a, **_k: route)
    monkeypatch.setattr(core, "_preflight_pricing", lambda *_: None)
    monkeypatch.setattr(core, "_require_dispatch_transport_admission", lambda *_: None)
    failed = Mock()
    monkeypatch.setattr(core, "mark_failed", failed)

    with SqliteRunStore(tmp_path / "runs.db") as store:
        assert store.reserve_epic_node_item("alpha", "SFD-42", "A")
        replacement = []
        holds = []

        def replace():
            store.release_epic_node_item("SFD-42", run_id="A")
            assert store.reserve_epic_node_item("alpha", "SFD-42", "B")
            if held:
                store.hold_epic_node("alpha", "SFD-42", "B's existing hold")
            replacement.extend(store.list_epic_reservations("alpha"))
            holds.extend(store.list_epic_holds("alpha"))

        def refuse(*_args, **_kwargs):
            if replace_at == "gate":
                replace()
            if gate == "worker_cap":
                return True, 1, 1
            if gate == "budget":
                raise BudgetExceeded("daily", 1, 2, BudgetStatus(0, 1, 2, 1, 2, 7))
            if gate == "containment":
                raise SandboxRuntimeUnavailable("no runtime")
            raise core.TransportAdmissionError("route refused", reason="unavailable")

        seam = {
            "worker_cap": "_worker_cap_reached",
            "budget": "check_budget",
            "containment": "_resolve_and_apply_sandbox",
            "transport": "_require_dispatch_transport_admission",
        }[gate]
        monkeypatch.setattr(core, seam, refuse)
        if replace_at == "transaction":
            original = store.try_admit_run

            def replace_before_transaction(*args, **kwargs):
                replace()
                monkeypatch.setattr(store, "try_admit_run", original)
                return original(*args, **kwargs)

            monkeypatch.setattr(store, "try_admit_run", replace_before_transaction)

        outcomes = []

        def dispatch(*_args, **_kwargs):
            outcome = core.execute_dispatch(
                issue={"number": 0, "title": "t"},
                config=config,
                store=store,
                epic_ref="alpha",
                item_id="SFD-42",
                provider="linear",
                approved=True,
                on_refuse=rollback,
                expected_reservation_run_id="A",
                expected_reservation_generation="A",
            )
            outcomes.append(outcome)
            return outcome

        if through_child:
            import kew.cli.epic as epic

            monkeypatch.setattr(epic, "_dispatch_child_node", dispatch)
            with pytest.raises(epic.typer.Exit) as exc:
                epic._run_dispatch_child(
                    store,
                    config,
                    None,
                    epic_ref="alpha",
                    item_id="SFD-42",
                    provider="linear",
                    run_kind="dispatch",
                    expected_reservation_run_id="A",
                    expected_reservation_generation="A",
                )
            assert exc.value.exit_code == 2
        else:
            dispatch()
        [outcome] = outcomes
        _assert_round16_preserved_owner(
            store, replacement, holds, rollback, failed, outcome
        )


def _assert_round16_preserved_owner(
    store, replacement, holds, rollback, failed, outcome
):
    from kew.graph.driver.tick import resolve_reservation_run

    assert replacement, "refusal must validate ownership in a write transaction"
    assert (
        store.list_epic_reservations("alpha"),
        store.get("B"),
        store.live_run_count(),
        store.list_epic_holds("alpha"),
    ) == (replacement, None, 1, holds)
    rollback.assert_not_called()
    failed.assert_not_called()
    assert (outcome.category, outcome.exit_code) == ("reservation_replaced", 2)
    [row] = store.list_by_item("SFD-42")
    assert row.run_id not in {"A", "B"}
    assert (row.status, row.error_category, row.exit_code) == (
        "refused",
        "reservation_replaced",
        2,
    )
    assert store.list_audit_events(event="dispatch_reservation_bound") == []
    assert resolve_reservation_run(store, replacement[0]) is None


def _round17_linear_child(scene, monkeypatch, tracker):
    import kew.cli.epic as epic
    from kew import dispatch_core as core

    monkeypatch.setenv("LINEAR_API_KEY", "test-key")
    monkeypatch.setattr("kew.linear.client.LinearClient", lambda *_: object())
    monkeypatch.setattr("kew.linear.tracker.LinearTracker", lambda *_: tracker)
    tracker.claim_state.return_value = {"id": "started", "name": "In Progress"}
    tracker.claim_assignee.return_value = "viewer"
    monkeypatch.setattr(epic, "_require_dispatch_child_runner", lambda *_: None)
    monkeypatch.setattr(epic._c, "execute_dispatch", core.execute_dispatch)
    return lambda: epic._dispatch_linear_node(
        scene.store,
        scene.config,
        None,
        epic_ref="alpha",
        item_id="SFD-42",
        provider="linear",
        expected_reservation_run_id="A",
        expected_reservation_generation="A",
    )


def test_round17_linear_replaced_during_fetch_never_claims(
    pinned_dispatch_scene, monkeypatch
):
    from unittest.mock import Mock

    scene = pinned_dispatch_scene
    assert scene.store.reserve_epic_node_item("alpha", "SFD-42", "A")
    tracker = Mock()

    def fetch(_item):
        scene.store.release_epic_node_item("SFD-42", run_id="A")
        assert scene.store.reserve_epic_node_item("alpha", "SFD-42", "B")
        return {"number": 0, "title": "t"}

    tracker.fetch_issue.side_effect = fetch
    outcome = _round17_linear_child(scene, monkeypatch, tracker)()
    tracker.claim.assert_not_called()
    tracker.reset_to_ready.assert_not_called()
    assert (outcome.category, outcome.exit_code) == ("reservation_replaced", 2)
    assert scene.store.get("B") is None
    assert [row.run_id for row in scene.store.list_epic_reservations("alpha")] == ["B"]
    assert scene.store.list_audit_events(event="dispatch_reservation_bound") == []
    assert scene.launches == []


@pytest.mark.parametrize("sandboxed", [False, True])
@pytest.mark.parametrize("failure", ["admission", "replaced", "partial_claim"])
def test_round17_linear_admission_failure_retains_and_records_claim(
    pinned_dispatch_scene, monkeypatch, sandboxed, failure
):
    from unittest.mock import Mock

    from kew import dispatch_core as core
    from kew import dispatch_sandbox as sandbox
    from kew.linear.client import LinearApiError
    from kew.run_store import SqliteRunStore

    scene = pinned_dispatch_scene
    scene.path["sandboxed"] = sandboxed
    assert scene.store.reserve_epic_node_item("alpha", "SFD-42", "A")
    tracker = Mock()
    tracker.fetch_issue.return_value = {"number": 0, "title": "t"}
    snapshot = {
        "id": "issue-uuid",
        "identifier": "SFD-42",
        "updatedAt": "claim-A",
        "state": {"id": "started", "type": "started"},
        "assignee": {"id": "viewer"},
    }
    observed = []

    def claim(_item):
        # A separate connection proves the pending row and binding COMMITTED.
        with SqliteRunStore(scene.store._db_path, read_only=True) as reader:
            rows = reader.list_by_item("SFD-42")
            assert len(rows) == 1, "claim requires committed admission"
            [row] = rows
            assert row.status == "pending"
            [binding] = reader.list_audit_events(event="dispatch_reservation_bound")
            assert binding.run_id == row.run_id
            assert binding.payload["reservation_run_id"] == "A"
            observed.append(row.run_id)
        if failure == "replaced":
            scene.store.release_epic_node_item("SFD-42", run_id="A")
            assert scene.store.reserve_epic_node_item("alpha", "SFD-42", "B")
        if failure == "partial_claim":
            raise LinearApiError("mutation response lost")
        return {"issue": snapshot}

    tracker.claim.side_effect = claim

    def refuse(*_a, **_k):
        raise core.TransportAdmissionError(
            "late admission refusal", reason="unavailable"
        )

    if failure == "admission":
        if sandboxed:
            monkeypatch.setattr(
                sandbox, "_require_initial_sandbox_publication_credentials", refuse
            )
        else:
            monkeypatch.setattr(core, "_compute_breaker_baseline", refuse)
    monkeypatch.setattr(core, "mark_failed", Mock())
    outcome = _round17_linear_child(scene, monkeypatch, tracker)()
    _round17_assert_retained(scene, tracker, observed, outcome, failure)


def _round17_assert_retained(scene, tracker, observed, outcome, failure):
    assert len(observed) == 1, "claim requires committed admission"
    tracker.claim.assert_called_once_with("SFD-42")
    tracker.reset_to_ready.assert_not_called()
    retained_events = scene.store.list_audit_events(event="linear_claim_retained")
    if failure == "partial_claim":
        assert retained_events == []
        assert scene.store.list_audit_events(event="linear_claim_completed") == []
    else:
        [retained] = retained_events
        assert retained.run_id == observed[0]
        assert retained.payload["updatedAt"] == "claim-A"
    assert scene.store.get(observed[0]).status == "failure"
    assert scene.launches == []
    assert outcome.exit_code == (2 if failure == "replaced" else 1)
    if failure == "replaced":
        assert scene.store.get("B") is None
        assert [row.run_id for row in scene.store.list_epic_reservations("alpha")] == [
            "B"
        ]


@pytest.mark.parametrize("sandboxed", [False, True])
def test_round17_linear_spawn_retains_admitted_claim(
    pinned_dispatch_scene, monkeypatch, sandboxed
):
    from unittest.mock import Mock

    from kew import dispatch_core as core
    from kew import dispatch_sandbox as sandbox

    class Spawned(BaseException):
        pass

    scene = pinned_dispatch_scene
    scene.path["sandboxed"] = sandboxed
    assert scene.store.reserve_epic_node_item("alpha", "SFD-42", "A")
    tracker = Mock()
    tracker.fetch_issue.return_value = {"number": 0, "title": "t"}

    def claim(_item):
        rows = scene.store.list_by_item("SFD-42")
        assert len(rows) == 1, "claim requires admission"
        assert rows[0].status == "pending", "claim requires admission"
        return {
            "issue": {
                "identifier": "SFD-42",
                "updatedAt": "claimed",
                "state": {"id": "started", "name": "In Progress"},
                "assignee": {"id": "viewer"},
            }
        }

    tracker.claim.side_effect = claim

    def spawned(*_a, **_k):
        raise Spawned

    def native(**kwargs):
        permit = kwargs["on_spawn"](123)
        permit()
        raise Spawned

    if sandboxed:
        monkeypatch.setattr(sandbox, "run_container_and_stream", scene.container_stream)
        monkeypatch.setattr(
            sandbox, "_launch_streaming_container", lambda *_a, **_k: object()
        )
        monkeypatch.setattr(core, "_stream_sandbox_log", spawned)
    else:
        monkeypatch.setattr(
            "kew.run_store.process_start_time", Mock(return_value="birth")
        )
        monkeypatch.setattr(
            "kew.run_store.process_start_token", Mock(return_value="admitted-birth")
        )
        monkeypatch.setattr(
            "kew.run_store._attempt_owner_verified", Mock(return_value=True)
        )
        monkeypatch.setattr(core, "_run_retry_loop", scene.retry_loop)
        # PR #1952 replaced _require_canonical_checkout_generation with the
        # context-bound _require_bound_checkout_generation at the paid boundary.
        monkeypatch.setattr(
            core, "_require_bound_checkout_generation", lambda *_a, **_k: None
        )
        monkeypatch.setattr(core, "run_agent", native)
    with pytest.raises(Spawned):
        _round17_linear_child(scene, monkeypatch, tracker)()
    tracker.claim.assert_called_once_with("SFD-42")
    tracker.reset_to_ready.assert_not_called()
    assert scene.store.list_audit_events(event="linear_claim_rollback") == []
