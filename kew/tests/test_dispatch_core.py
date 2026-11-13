"""Tests for dispatch_core -- the shared dispatch execution logic."""

import io
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import ANY, MagicMock, patch

import pytest
from dispatch_owner_fixture import admission_owner as admission_owner
from support.agent_report import quoted
from support.checkout_generations import (
    fixture_checkout_generation,
    fixture_claim_evidence,
)
from support.host_stream import run_host_stream
from support.narrow_checkout import assert_narrow_checkout, make_narrow_checkout_source
from test_liveness_owner import BIRTH
from test_liveness_owner import process_table as process_table

from kew.budget import BudgetExceeded, BudgetStatus
from kew.config import (
    DispatchConfig,
    InferenceTransportConfig,
    RetryConfig,
    SandboxConfig,
    SwitchyardConfig,
    load_config,
)
from kew.inference_transport import RunnerTransportCapability
from kew.sandbox import (
    CHECKOUT_QUOTA_BYTES,
    DEFAULT_IMAGE_RUNNER,
    ImageIdStatus,
    ImageResolution,
    ResourceCapCode,
)
from kew.switchyard import SwitchyardProbeResult
from kew.tracking import DispatchResult
from kew.transcript_metrics import read_derived_evidence, write_derived_evidence

# The image the runner-derived sandbox decision resolves for the default
# claude-code route — what these tests would get from `SandboxDecision.image`
# in production (#1144) — and the runner it was resolved from, which rides down
# with it so the container args match the image (#1145).
SANDBOX_IMAGE = SandboxConfig().image
SANDBOX_RUNNER = DEFAULT_IMAGE_RUNNER


def _backoff_sleeps(mock_sleep: MagicMock) -> list[float]:
    """The retry-ladder waits recorded on a patched ``time.sleep``.

    Patching ``kew.dispatch_core.time.sleep`` patches the ``time`` module, so
    the run store's sub-second SQLite busy retries and the stdlib's
    Popen.wait(timeout) poll (0.0005 s doubling to 0.05 s) land on the same
    mock, and their count varies with host load (65 and 1402 of them, under
    xdist plus CI on one host). The retry ladder waits whole seconds; count
    only those.
    """
    return [
        call.args[0]
        for call in mock_sleep.call_args_list
        if call.args and call.args[0] >= 1.0
    ]


@pytest.fixture(autouse=True)
def _release_checkout_admission():
    """Unit launch calls bypass the dispatcher's run finalizer."""
    from kew.worktree import release_checkout_claims

    yield
    release_checkout_claims()


@pytest.fixture(autouse=True)
def _launch_image_resolves(monkeypatch):
    """Sandboxed-dispatch tests run with detect_runtime_cached forced to None
    (conftest), which the fail-closed image gate (#1550 F1) would refuse.
    Resolve to a stable fake ID by default; gate tests re-patch to raise."""
    from kew.dispatch_core import ContainedImageUnresolvedError as _unused  # noqa: F401

    monkeypatch.setattr(
        "kew.dispatch_sandbox._resolve_launch_image_id",
        lambda config, image, **_: "sha256:" + "f" * 64,
    )
    monkeypatch.setattr(
        "kew.dispatch_sandbox._contained_auth_path",
        lambda _runner: "workload-identity",
    )


@pytest.fixture(autouse=True)
def _mock_resolved_models_are_not_openrouter_routes(monkeypatch):
    """A patched resolve_runner yields a MagicMock model, which names no route.

    Governed review round 2 (2026-09-21): a direct OpenRouter route without a
    provisioning key is refused on the host too, so the flow tests here that
    never name a model must not read as OpenRouter routes.
    """
    from kew import run_credentials

    real = run_credentials.direct_openrouter_route

    def classify(model, credential_names, *, direct):
        return isinstance(model, str) and real(model, credential_names, direct=direct)

    monkeypatch.setattr(run_credentials, "direct_openrouter_route", classify)


@pytest.fixture(autouse=True)
def _register_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """Register ``git`` in the subprocess allowlist for sandbox tests that
    now call ``_checkout_has_no_changes`` / ``has_uncommitted_changes``
    (#958). Module-wide so all test classes in this file are covered."""
    import shutil

    from kew.subprocess_utils import TOOL_PATHS

    git = shutil.which("git")
    assert git is not None
    monkeypatch.setitem(TOOL_PATHS, "git", git)


@pytest.fixture(autouse=True)
def _no_unstubbed_origin_branch_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail loudly instead of asking the real origin whether a branch exists.

    ``_dispatch_dry_run`` reads the duplicate-branch guard through
    ``kew.cli.remote_branch_exists``, so a preview test that forgets to stub
    that seam silently runs an untimed ``git ls-remote --heads origin`` against
    the operator's remote and then passes or fails on whichever agent branches
    happen to exist there (PR 2191 review). Every other ``git`` call still
    reaches the real ``run``.
    """
    from kew import worktree

    real_run = worktree.run

    def guarded(cmd, *args, **kwargs):
        argv = list(cmd)
        if "ls-remote" in argv and "origin" in argv:
            raise AssertionError(
                f"test reached the real remote-branch read ({argv}) — "
                "stub kew.cli.remote_branch_exists instead"
            )
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(worktree, "run", guarded)


@pytest.fixture(autouse=True)
def _allow_runner_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    """Existing dispatch tests use synthetic runners without host CLIs."""
    monkeypatch.setattr(
        "kew.runner_preflight.runner_preflight_failure",
        lambda _config, *, runner_name=None: None,
    )


@pytest.fixture
def _canonical_existing_pr_published(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Synthetic orchestration checkouts have no tree to publish.

    Mock only the existing-PR publication seam for tests that opt in.
    Real publication and cleanup ordering live in test_publication_success_routing.
    """
    publisher = MagicMock()
    monkeypatch.setattr("kew.dispatch_core._publish_canonical_pr_update", publisher)
    return publisher


def _make_issue(
    num: int = 42,
    title: str = "Fix the widget",
    body: str = "It is broken",
    labels: list[dict[str, str]] | None = None,
) -> dict:
    return {
        "number": num,
        "title": title,
        "body": body,
        "labels": labels or [{"name": "ready"}],
    }


def _mock_store() -> MagicMock:
    """Mock RunStore for execute_dispatch tests. live_run_count must return a
    real int — the worker-cap preflight (#721) compares it against
    parallel.max_workers, and a bare MagicMock raises on `>=`."""
    store = MagicMock()
    store.live_run_count.return_value = 0
    # The atomic worker-slot admission (#745) admits by default in mock-based
    # dispatch tests; a bare MagicMock is truthy but this is explicit.
    store.try_admit_run.return_value = True
    # An empty reservation list keeps the budget-refusal row's reservation
    # lookup (#891) iterable — a bare MagicMock is not iterable.
    store.list_epic_reservations.return_value = []
    store.list_route_profiles.return_value = []
    store.continuation_origins.return_value = []
    return store


def _ok_result(issue_num: int = 42, **kw) -> DispatchResult:
    defaults = dict(
        exit_code=0,
        issue_num=issue_num,
        log_file=Path("/tmp/log"),
        session_id="sess-ok",
        error_category="",
    )
    defaults.update(kw)
    return DispatchResult(**defaults)


def _fail_result(issue_num: int = 42, **kw) -> DispatchResult:
    defaults = dict(
        exit_code=1,
        issue_num=issue_num,
        log_file=Path("/tmp/log"),
        session_id="sess-fail",
        error_category="transient",
    )
    defaults.update(kw)
    return DispatchResult(**defaults)


def test_root_checkout_snapshot_detects_base_ref_movement(tmp_path: Path):
    from kew.dispatch_core import (
        _capture_root_checkout_state,
        _root_checkout_state_changed,
    )

    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("initial\n")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=tmp_path, check=True)

    before = _capture_root_checkout_state(tmp_path, base_ref="main")
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "stray"], cwd=tmp_path, check=True
    )
    after = _capture_root_checkout_state(tmp_path, base_ref="main")

    assert before is not None
    assert after is not None
    assert _root_checkout_state_changed(before, after)


def test_root_checkout_integrity_violation_is_audited(monkeypatch):
    import kew.dispatch_core as dispatch_core

    ctx = MagicMock(spec=dispatch_core._DispatchContext)
    ctx.checkout_generation = ""
    ctx.root_checkout_before = ("head-before", "main", "ref-before")
    ctx.config = DispatchConfig()
    ctx.config.branches.base = "main"
    ctx.config.project.repo = "octo/widgets"
    ctx.issue_num = 42
    ctx.item_id = "42"
    ctx.store = MagicMock()

    monkeypatch.setattr(
        dispatch_core,
        "_capture_root_checkout_state",
        lambda root, *, base_ref: ("head-after", "main", "ref-after"),
    )
    emit = MagicMock()
    monkeypatch.setattr(dispatch_core.audit, "emit", emit)

    assert dispatch_core._root_checkout_integrity_check(ctx, run_id="run-42")
    emit.assert_called_once()
    assert emit.call_args.args[1] == "root_checkout_integrity_violation"
    assert emit.call_args.kwargs["run_id"] == "run-42"
    assert emit.call_args.kwargs["payload"]["before"]["head"] == "head-before"
    assert emit.call_args.kwargs["payload"]["after"]["ref"] == "ref-after"


def test_root_checkout_integrity_check_fails_closed_when_after_snapshot_missing(
    monkeypatch,
):
    import kew.dispatch_core as dispatch_core

    ctx = MagicMock(spec=dispatch_core._DispatchContext)
    ctx.checkout_generation = ""
    ctx.root_checkout_before = ("head-before", "main", "ref-before")
    ctx.config = DispatchConfig()
    ctx.config.branches.base = "main"
    ctx.config.project.repo = "octo/widgets"
    ctx.issue_num = 42
    ctx.item_id = "42"
    ctx.store = MagicMock()

    monkeypatch.setattr(
        dispatch_core, "_capture_root_checkout_state", lambda root, *, base_ref: None
    )
    emit = MagicMock()
    monkeypatch.setattr(dispatch_core.audit, "emit", emit)
    error = MagicMock()
    monkeypatch.setattr(dispatch_core, "_error", error)

    assert dispatch_core._root_checkout_integrity_check(ctx, run_id="run-42")
    emit.assert_called_once()
    assert emit.call_args.args[1] == "root_checkout_integrity_violation"
    assert emit.call_args.kwargs["payload"]["after"] is None
    # The reason must name the capture failure, not checkout movement — the two
    # violations look identical from the event type alone.
    expected_reason = (
        "could not capture operator checkout HEAD/ref state after dispatch"
    )
    assert emit.call_args.kwargs["payload"]["reason"] == expected_reason
    error.assert_called_once()
    assert expected_reason in error.call_args.args[0]


@pytest.mark.parametrize(
    ("after_state", "expected_reason"),
    [
        (
            ("head-after", "main", "ref-after"),
            "operator checkout HEAD/ref state moved during dispatch",
        ),
        (None, "could not capture operator checkout HEAD/ref state after dispatch"),
    ],
)
def test_collect_result_publishes_the_reason_it_attested(
    monkeypatch, tmp_path: Path, after_state, expected_reason
):
    """The terminal failure an operator reads must match the audited incident.

    A lost post-dispatch snapshot and a moved ref both abort collection, but
    reporting the unreadable case as "state moved" sends the operator hunting a
    rewrite that never happened."""
    import kew.dispatch_core as dispatch_core

    ctx = MagicMock(spec=dispatch_core._DispatchContext)
    ctx.checkout_generation = ""
    ctx.root_checkout_before = ("head-before", "main", "ref-before")
    ctx.config = DispatchConfig()
    ctx.config.branches.base = "main"
    ctx.config.project.repo = "octo/widgets"
    ctx.issue_num = 42
    ctx.item_id = "42"
    ctx.store = MagicMock()
    ctx.terminal_failure = MagicMock()

    monkeypatch.setattr(
        dispatch_core,
        "_capture_root_checkout_state",
        lambda root, *, base_ref: after_state,
    )
    monkeypatch.setattr(dispatch_core.audit, "emit", MagicMock())

    outcome = dispatch_core._collect_result(
        ctx,
        result=_fail_result(run_id="run-42"),
        worktree_dir=tmp_path / "wt",
        run_sandboxed_path=False,
    )

    assert outcome.exit_code == 1
    assert outcome.changed_files == []
    ctx.terminal_failure.assert_called_once_with(
        "root_checkout_integrity_violation", expected_reason
    )


def _integrity_ctx(dispatch_core, **overrides):
    """A dispatch context whose operator checkout is about to be found moved."""
    ctx = MagicMock(spec=dispatch_core._DispatchContext)
    ctx.checkout_generation = ""
    ctx.root_checkout_before = ("head-before", "main", "ref-before")
    ctx.config = DispatchConfig()
    ctx.config.branches.base = "main"
    ctx.config.project.repo = "octo/widgets"
    ctx.issue_num = 42
    ctx.item_id = "42"
    ctx.branch = "agent/issue-42"
    ctx.store = MagicMock()
    ctx.terminal_failure = MagicMock()
    for name, value in overrides.items():
        setattr(ctx, name, value)
    return ctx


def _arm_integrity_violation(monkeypatch, dispatch_core, pr_lookup):
    """Force a moved operator checkout and capture the gh argv it produces.

    Returns ``(emit, argv_calls, collect_success)`` so a test can assert on the
    audit event, the tracker side effects, and whether the normal terminal
    ladder was allowed to run.
    """
    monkeypatch.setattr(
        dispatch_core,
        "_capture_root_checkout_state",
        lambda root, *, base_ref: ("head-after", "main", "ref-after"),
    )
    emit = MagicMock()
    monkeypatch.setattr(dispatch_core.audit, "emit", emit)
    monkeypatch.setattr(dispatch_core, "check_pr_exists", pr_lookup)
    argv_calls: list[list[str]] = []

    def _fake_run(argv, **kwargs):
        argv_calls.append(list(argv))
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(dispatch_core, "run", _fake_run)
    collect_success = MagicMock(
        return_value=dispatch_core.DispatchOutcome(
            exit_code=0, changed_files=["a.py"], run_id="run-42"
        )
    )
    monkeypatch.setattr(dispatch_core, "_collect_success", collect_success)
    return emit, argv_calls, collect_success


def test_integrity_violation_keeps_a_published_run_and_only_comments(
    monkeypatch, tmp_path: Path
):
    """A moved operator checkout must not rewrite an outcome already on the remote.

    The violation happens in the *operator's* checkout; the PR the run published
    is reviewable work that exists regardless. Relabelling it ``agent-failed``
    hid a finished node from ``kew epic status`` and asked a human to redo work
    that was already waiting for review (#1660)."""
    import kew.dispatch_core as dispatch_core

    ctx = _integrity_ctx(dispatch_core)
    emit, argv_calls, collect_success = _arm_integrity_violation(
        monkeypatch, dispatch_core, lambda branch, config: 7
    )

    outcome = dispatch_core._collect_result(
        ctx,
        result=_ok_result(run_id="run-42"),
        worktree_dir=tmp_path / "wt",
        run_sandboxed_path=False,
    )

    # The published outcome stands, produced by the normal terminal ladder.
    collect_success.assert_called_once()
    assert outcome.exit_code == 0
    assert outcome.changed_files == ["a.py"]
    # No relabel: the tracker hook never fires on a published run.
    ctx.terminal_failure.assert_not_called()
    # The incident is still audited...
    emit.assert_called_once()
    assert emit.call_args.args[1] == "root_checkout_integrity_violation"
    # ...and reported as a plain issue comment naming the published PR.
    assert len(argv_calls) == 1
    argv = argv_calls[0]
    assert argv[:5] == ["gh", "issue", "comment", "42", "--repo"]
    assert "--add-label" not in argv and "--remove-label" not in argv
    body = argv[argv.index("--body") + 1]
    assert "#7" in body
    assert "operator checkout HEAD/ref state moved during dispatch" in body


def test_integrity_violation_without_a_published_pr_still_fails_closed(
    monkeypatch, tmp_path: Path
):
    """The fail-closed branch is unchanged for a run that published nothing."""
    import kew.dispatch_core as dispatch_core

    ctx = _integrity_ctx(dispatch_core)
    emit, argv_calls, collect_success = _arm_integrity_violation(
        monkeypatch, dispatch_core, lambda branch, config: None
    )

    outcome = dispatch_core._collect_result(
        ctx,
        result=_ok_result(run_id="run-42"),
        worktree_dir=tmp_path / "wt",
        run_sandboxed_path=False,
    )

    assert outcome.exit_code == 1
    assert outcome.changed_files == []
    collect_success.assert_not_called()
    ctx.terminal_failure.assert_called_once_with(
        "root_checkout_integrity_violation",
        "operator checkout HEAD/ref state moved during dispatch",
    )
    emit.assert_called_once()
    # The terminal-failure publisher owns the comment on this branch; the
    # incident reporter must not double-post.
    assert argv_calls == []


def test_integrity_violation_fails_closed_when_the_pr_lookup_is_unreadable(
    monkeypatch, tmp_path: Path
):
    """Only positive evidence of a published PR may soften the incident.

    An unreadable remote is not evidence of publication -- reading it as one
    would let a GitHub outage disarm the integrity check entirely."""
    import kew.dispatch_core as dispatch_core

    def _boom(branch, config):
        raise RuntimeError("GitHub REST PR lookup failed (exit 1)")

    ctx = _integrity_ctx(dispatch_core)
    _emit, argv_calls, collect_success = _arm_integrity_violation(
        monkeypatch, dispatch_core, _boom
    )

    outcome = dispatch_core._collect_result(
        ctx,
        result=_ok_result(run_id="run-42"),
        worktree_dir=tmp_path / "wt",
        run_sandboxed_path=False,
    )

    assert outcome.exit_code == 1
    collect_success.assert_not_called()
    ctx.terminal_failure.assert_called_once_with(
        "root_checkout_integrity_violation",
        "operator checkout HEAD/ref state moved during dispatch",
    )
    assert argv_calls == []


def test_integrity_incident_on_a_non_github_item_is_audited_not_commented(
    monkeypatch, tmp_path: Path
):
    """A Linear item has no GitHub issue to comment on; the audit event is the
    durable record and the published outcome still stands."""
    import kew.dispatch_core as dispatch_core

    ctx = _integrity_ctx(dispatch_core, item_id="SFD-12")
    emit, argv_calls, collect_success = _arm_integrity_violation(
        monkeypatch, dispatch_core, lambda branch, config: 7
    )

    outcome = dispatch_core._collect_result(
        ctx,
        result=_ok_result(run_id="run-42"),
        worktree_dir=tmp_path / "wt",
        run_sandboxed_path=False,
    )

    assert outcome.exit_code == 0
    collect_success.assert_called_once()
    ctx.terminal_failure.assert_not_called()
    emit.assert_called_once()
    assert argv_calls == []


def test_integrity_incident_comment_never_sinks_a_published_run(
    monkeypatch, tmp_path: Path
):
    """The work is already on the remote: a tracker that refuses the comment
    must not turn the published outcome into a failure."""
    import kew.dispatch_core as dispatch_core

    ctx = _integrity_ctx(dispatch_core)
    _emit, _argv, collect_success = _arm_integrity_violation(
        monkeypatch, dispatch_core, lambda branch, config: 7
    )

    def _explode(argv, **kwargs):
        raise OSError("gh is not installed")

    monkeypatch.setattr(dispatch_core, "run", _explode)

    outcome = dispatch_core._collect_result(
        ctx,
        result=_ok_result(run_id="run-42"),
        worktree_dir=tmp_path / "wt",
        run_sandboxed_path=False,
    )

    assert outcome.exit_code == 0
    collect_success.assert_called_once()
    ctx.terminal_failure.assert_not_called()


# ---------- Patch target prefix for dispatch_core ----------
_P = "kew.dispatch_core"


class TestContinuationDispatchBoundary:
    """Continuation identity is enforced before any runner-side side effect."""

    @staticmethod
    def _claim(store, tmp_path: Path) -> Path:
        worktree = tmp_path / "preserved"
        store.record_continuation(
            "epic:x",
            "42",
            "original-run",
            "agent/issue-42",
            worktree,
            checkout_generation=fixture_checkout_generation(worktree),
        )
        assert (
            store.claim_continuation_under_cap(
                "epic:x",
                "42",
                "original-run",
                "agent/issue-42",
                worktree,
                "continuation-run",
                1,
                claim_evidence=fixture_claim_evidence(
                    store,
                    "epic:x",
                    "42",
                    "original-run",
                    "agent/issue-42",
                    worktree,
                    "continuation-run",
                ),
            )
            == "claimed"
        )
        return worktree

    def test_execute_dispatch_rejects_partial_continuation_state(
        self, tmp_path: Path
    ) -> None:
        from kew.dispatch_core import execute_dispatch
        from kew.run_store import SqliteRunStore

        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            with pytest.raises(
                ValueError, match="requires worktree, branch, and run id"
            ):
                execute_dispatch(
                    allow_unrecorded=True,
                    issue=_make_issue(),
                    config=DispatchConfig(),
                    store=store,
                    epic_ref="epic:x",
                    worktree_dir=tmp_path / "preserved",
                )

            assert store.list_all() == []

    def test_execute_dispatch_rejects_mismatched_claimed_branch(
        self, tmp_path: Path
    ) -> None:
        from kew.dispatch_core import execute_dispatch
        from kew.run_store import SqliteRunStore

        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            worktree = self._claim(store, tmp_path)
            with pytest.raises(ValueError, match="does not match"):
                execute_dispatch(
                    allow_unrecorded=True,
                    issue=_make_issue(),
                    config=DispatchConfig(),
                    store=store,
                    epic_ref="epic:x",
                    worktree_dir=worktree,
                    branch="agent/issue-other",
                    run_id="continuation-run",
                )

            assert store.get("continuation-run") is None

    def test_run_tracked_agent_rejects_unclaimed_run_id(self, tmp_path: Path) -> None:
        from kew.dispatch_core import run_tracked_agent
        from kew.run_store import SqliteRunStore

        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            worktree = self._claim(store, tmp_path)
            with pytest.raises(ValueError, match="does not match"):
                run_tracked_agent(
                    allow_unrecorded=True,
                    prompt="continue",
                    worktree_dir=worktree,
                    config=DispatchConfig(),
                    issue_num=42,
                    item_id="42",
                    store=store,
                    branch="agent/issue-other",
                    run_id="continuation-run",
                    epic_ref="epic:x",
                )


class TestWorkerCapPreflight:
    """Fleet-wide worker cap enforced at dispatch preflight (#721)."""

    @staticmethod
    def _pending(store, issue_num: int, run_id: str) -> None:
        from datetime import UTC, datetime

        from kew.run_store import RunRecord

        store.save(
            RunRecord(
                run_id=run_id,
                issue_num=issue_num,
                branch=f"agent/issue-{issue_num}",
                status="pending",
                exit_code=-1,
                started_at=datetime.now(UTC),
            )
        )

    def test_worker_cap_reached_helper(self, tmp_path):
        from kew.dispatch_core import _worker_cap_reached
        from kew.run_store import SqliteRunStore

        config = DispatchConfig()
        config.parallel.max_workers = 2
        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            assert _worker_cap_reached(store, config, 5) == (False, 0, 2)
            self._pending(store, 1, "r-1")
            assert _worker_cap_reached(store, config, 5) == (False, 1, 2)
            self._pending(store, 2, "r-2")
            assert _worker_cap_reached(store, config, 5) == (True, 2, 2)
            # another run of the issue being dispatched still holds a slot (#2628)
            assert _worker_cap_reached(store, config, 1) == (True, 2, 2)
            # but its own reservation never counts against it
            store.reserve_epic_node("epic-x", 5, "r-5")
            assert _worker_cap_reached(store, config, 6) == (True, 3, 2)
            assert _worker_cap_reached(store, config, 5) == (True, 2, 2)

    @staticmethod
    def _pending_dead(store, issue_num: int, run_id: str) -> None:
        """A pending row whose owning process group is provably gone.

        pgid 1 is init/launchd: it exists but is not ours, so ``killpg(1, 0)``
        raises PermissionError and reads as alive. A pgid that no process holds
        raises ProcessLookupError, which is the "provably dead" verdict the
        probe reaps on. 2**31 - 1 is above any real pid.
        """
        from datetime import UTC, datetime

        from kew.run_store import RunRecord

        store.save(
            RunRecord(
                run_id=run_id,
                issue_num=issue_num,
                branch=f"agent/issue-{issue_num}",
                status="pending",
                exit_code=-1,
                started_at=datetime.now(UTC),
                pgid=2**31 - 1,
                owner_pid=2**31 - 1,
                owner_start_time=BIRTH,
            )
        )

    def test_worker_cap_ignores_provably_dead_runs(self, tmp_path, process_table):
        """A crashed run must not hold a worker slot forever (#1132).

        ``live_run_count`` grew a liveness probe in #1059 so one hard-killed
        run could never permanently shrink the fleet, but the dispatch
        preflight called the unprobed form: a single orphaned ``pending`` row
        cost a slot until someone ran ``kew cleanup`` by hand.
        """
        from kew.dispatch_core import _worker_cap_reached
        from kew.run_store import SqliteRunStore

        config = DispatchConfig()
        config.parallel.max_workers = 2
        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            self._pending(store, 1, "r-1")
            self._pending_dead(store, 2, "r-2-dead")
            # Two pending rows, but only one live agent: the cap is not reached.
            assert _worker_cap_reached(store, config, 5) == (False, 1, 2)

    def test_worker_cap_counts_undeterminable_runs(self, tmp_path):
        """No pgid means undeterminable, and undeterminable is never reaped.

        The probe is three-state on purpose: only a positive "dead" reclaims a
        slot, so a run mid-startup that has not recorded its pgid yet still
        counts.
        """
        from kew.dispatch_core import _worker_cap_reached
        from kew.run_store import SqliteRunStore

        config = DispatchConfig()
        config.parallel.max_workers = 2
        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            self._pending(store, 1, "r-1")
            self._pending(store, 2, "r-2")  # no pgid recorded
            assert _worker_cap_reached(store, config, 5) == (True, 2, 2)

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.setup_worktree")
    @patch(f"{_P}.run_tracked_agent")
    @patch(f"{_P}.reset_issue_to_ready")
    def test_over_cap_blocks_and_requeues_classic(
        self, mock_reset, mock_run, mock_worktree, mock_conn, tmp_path
    ):
        from kew.dispatch_core import execute_dispatch
        from kew.run_store import SqliteRunStore

        config = DispatchConfig()
        config.parallel.max_workers = 2
        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            self._pending(store, 201, "r-201")
            self._pending(store, 202, "r-202")
            outcome = execute_dispatch(
                allow_unrecorded=True,
                issue=_make_issue(num=203),
                config=config,
                store=store,
            )

        assert outcome.exit_code == 1
        mock_reset.assert_called_once_with(203, config)
        mock_run.assert_not_called()
        mock_worktree.assert_not_called()
        # The preflight bails before the GitHub connectivity check even runs.
        mock_conn.assert_not_called()

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.setup_worktree")
    @patch(f"{_P}.run_tracked_agent")
    @patch(f"{_P}.reset_issue_to_ready")
    def test_over_cap_epic_node_releases_reservation_not_labels(
        self, mock_reset, mock_run, mock_worktree, mock_conn, tmp_path
    ):
        from kew.dispatch_core import execute_dispatch
        from kew.run_store import SqliteRunStore

        config = DispatchConfig()
        config.parallel.max_workers = 2
        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            self._pending(store, 201, "r-201")
            self._pending(store, 202, "r-202")
            store.reserve_epic_node("epic-x", 203, "r-203")
            outcome = execute_dispatch(
                allow_unrecorded=True,
                issue=_make_issue(num=203),
                config=config,
                store=store,
                epic_ref="epic-x",
                expected_reservation_run_id="r-203",
                expected_reservation_generation="r-203",
            )
            reservations = store.list_epic_reservations("epic-x")

        assert outcome.exit_code == 1
        # Driver-owned node: reservation released for re-admission, labels
        # untouched (the #661 contract, keyed on epic_ref not labels).
        assert reservations == []
        mock_reset.assert_not_called()
        mock_run.assert_not_called()

    def test_requeue_epic_node_releases_reservation(self):
        # epic_ref (the driver-owned signal, #661 contract) decides — even
        # when labels are absent or malformed. Release keys on the durable
        # item id: a GitHub node's item id is str(issue_num).
        from kew.dispatch_core import _requeue_over_cap

        store = _mock_store()
        config = DispatchConfig()
        _requeue_over_cap(42, "epic-x", store, config, expected_reservation_run_id="A")
        store.release_epic_node_item.assert_called_once_with("42", run_id="A")

    def test_requeue_linear_node_releases_reservation_by_item_id(self):
        # A Linear node rides issue_num=0 but reserves on its durable item id.
        # Releasing on the 0 sentinel would leave "SFD-12" to dangle until grace
        # expiry — the release must name the item, not the sentinel (A3).
        from kew.dispatch_core import _requeue_over_cap

        store = _mock_store()
        config = DispatchConfig()
        _requeue_over_cap(
            0, "epic-x", store, config, "SFD-12", expected_reservation_run_id="A"
        )
        store.release_epic_node_item.assert_called_once_with("SFD-12", run_id="A")

    def test_worker_cap_reached_excludes_self_by_item_id(self, tmp_path):
        # Two Linear reservations both ride issue_num=0; excluding by issue_num
        # would drop neither (self never excluded) and the item counts itself.
        # Keyed on item_id, dispatching SFD-1 sees only SFD-2 live (#SFD-143).
        from kew.dispatch_core import _worker_cap_reached
        from kew.run_store import SqliteRunStore

        config = DispatchConfig()
        config.parallel.max_workers = 2
        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            store.reserve_epic_node_item("epic-x", "SFD-1", "r-1")
            store.reserve_epic_node_item("epic-x", "SFD-2", "r-2")
            # SFD-1 excludes its own reservation -> 1 other live < cap 2.
            assert _worker_cap_reached(store, config, 0, "SFD-1") == (False, 1, 2)

    @patch(f"{_P}.reset_issue_to_ready")
    def test_requeue_classic_node_resets_to_ready(self, mock_reset):
        from kew.dispatch_core import _requeue_over_cap

        store = _mock_store()
        config = DispatchConfig()
        _requeue_over_cap(7, None, store, config)
        mock_reset.assert_called_once_with(7, config)
        store.release_epic_node.assert_not_called()

    @patch(f"{_P}.reset_issue_to_ready")
    def test_requeue_epic_labelled_but_undriven_resets_labels(self, mock_reset):
        # An epic-LABELLED issue dispatched outside the driver (epic_ref None)
        # must NOT release a reservation it does not own — labels are not the
        # signal, epic_ref is.
        from kew.dispatch_core import _requeue_over_cap

        store = _mock_store()
        config = DispatchConfig()
        _requeue_over_cap(9, None, store, config)
        mock_reset.assert_called_once_with(9, config)
        store.release_epic_node.assert_not_called()


class TestWorkerCapAtomicAdmission:
    """Atomic worker-slot admission at the pending-row insert (#745): the row
    IS the slot, count+insert are one transaction, and a race-loser (passed
    preflight, lost the insert) is requeued exactly like the preflight path."""

    @staticmethod
    def _pending(store, issue_num: int, run_id: str) -> None:
        from datetime import UTC, datetime

        from kew.run_store import RunRecord

        store.save(
            RunRecord(
                run_id=run_id,
                issue_num=issue_num,
                branch=f"agent/issue-{issue_num}",
                status="pending",
                exit_code=-1,
                started_at=datetime.now(UTC),
            )
        )

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.generate_repo_map", return_value="")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    @patch(f"{_P}.setup_worktree", return_value=Path("/tmp/wt-203"))
    @patch(f"{_P}.resolve_runner")
    @patch(f"{_P}.run_tracked_agent")
    @patch(f"{_P}._cleanup_dispatch_dir")
    @patch(f"{_P}.mark_failed")
    @patch(f"{_P}.reset_issue_to_ready")
    def test_admission_race_loser_requeues_and_cleans_up(
        self,
        mock_reset,
        mock_failed,
        mock_cleanup,
        mock_run,
        mock_resolve,
        mock_worktree,
        mock_prompt,
        mock_repomap,
        mock_conn,
        tmp_path,
    ):
        from kew.dispatch_core import WorkerCapReached, execute_dispatch
        from kew.run_store import SqliteRunStore

        # The atomic insert lost the race after preflight passed.
        mock_run.side_effect = WorkerCapReached(203)
        config = DispatchConfig()
        config.parallel.max_workers = 5  # preflight sees 0 live < 5, proceeds
        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            outcome = execute_dispatch(
                allow_unrecorded=True,
                issue=_make_issue(num=203),
                config=config,
                store=store,
                model_override="claude-sonnet-4-6",  # skip the cache governor
            )

        assert outcome.exit_code == 1
        # Requeued (classic node → back to ready), NOT marked failed.
        mock_reset.assert_called_once_with(203, config)
        mock_failed.assert_not_called()
        # The unused worktree is torn down (host path → sandboxed=False).
        mock_cleanup.assert_called_once()
        assert mock_cleanup.call_args.kwargs.get("sandboxed") is False

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.generate_repo_map", return_value="")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    @patch(f"{_P}.setup_worktree", return_value=Path("/tmp/wt-203"))
    @patch(f"{_P}.resolve_runner")
    @patch(f"{_P}.run_tracked_agent")
    def test_execute_dispatch_forwards_cap_to_run_tracked_agent(
        self,
        mock_run,
        mock_resolve,
        mock_worktree,
        mock_prompt,
        mock_repomap,
        mock_conn,
        tmp_path,
    ):
        from kew.dispatch_core import execute_dispatch
        from kew.run_store import SqliteRunStore

        mock_run.return_value = _ok_result(issue_num=203)
        config = DispatchConfig()
        config.parallel.max_workers = 4
        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            execute_dispatch(
                allow_unrecorded=True,
                issue=_make_issue(num=203),
                config=config,
                store=store,
                model_override="claude-sonnet-4-6",
            )

        kwargs = mock_run.call_args.kwargs
        assert kwargs["worker_cap"] == 4
        assert kwargs["cap_exclude_issue"] == 203

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.generate_repo_map", return_value="")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    @patch(f"{_P}.setup_worktree", return_value=Path("/tmp/wt-1108"))
    @patch(f"{_P}.resolve_runner")
    @patch(f"{_P}.run_tracked_agent")
    def test_host_non_claude_runner_wires_budget_watchdog(
        self,
        mock_run,
        mock_resolve,
        mock_worktree,
        mock_prompt,
        mock_repomap,
        mock_conn,
        tmp_path,
    ):
        """#1108: opencode/pi/hermes have no native ``--max-budget-usd`` flag, so
        the host path must pass a mid-run ``budget_check`` watchdog. Without it
        ``max_budget_usd`` is decorative on direct dispatch and only the
        admission-time pools bound a runaway run (drill 1a: $0.0142 under a
        $0.01 cap ran to success)."""
        from kew.dispatch_core import execute_dispatch
        from kew.run_store import SqliteRunStore

        mock_run.return_value = _ok_result(issue_num=210)
        # opencode runner (no native --max-budget-usd flag); the model string is
        # orthogonal to the wiring — use a priced one to pass the preflight.
        mock_resolve.return_value.runner.name = "opencode"
        mock_resolve.return_value.model = "claude-sonnet-4-6"
        config = DispatchConfig()
        config.agent.max_budget_usd = 0.01
        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            execute_dispatch(
                allow_unrecorded=True,
                issue=_make_issue(num=210),
                config=config,
                store=store,
                model_override="claude-sonnet-4-6",  # skip the cache governor
            )

        bc = mock_run.call_args.kwargs["budget_check"]
        assert bc is not None
        # Over the run cap → trip; at/under → allow.
        assert bc(0.0142) is True
        assert bc(0.01) is False
        assert bc(0.005) is False

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.generate_repo_map", return_value="")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    @patch(f"{_P}.setup_worktree", return_value=Path("/tmp/wt-1108"))
    @patch(f"{_P}.resolve_runner")
    @patch(f"{_P}.run_tracked_agent")
    def test_host_claude_code_leaves_budget_watchdog_to_native_flag(
        self,
        mock_run,
        mock_resolve,
        mock_worktree,
        mock_prompt,
        mock_repomap,
        mock_conn,
        tmp_path,
    ):
        """claude-code enforces ``max_budget_usd`` natively via its
        ``--max-budget-usd`` argv flag, so the host path leaves ``budget_check``
        unset for it — enforcement isn't doubled (#1108)."""
        from kew.dispatch_core import execute_dispatch
        from kew.run_store import SqliteRunStore

        mock_run.return_value = _ok_result(issue_num=211)
        mock_resolve.return_value.runner.name = "claude-code"
        mock_resolve.return_value.model = "claude-sonnet-4-6"
        config = DispatchConfig()
        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            execute_dispatch(
                allow_unrecorded=True,
                issue=_make_issue(num=211),
                config=config,
                store=store,
                model_override="claude-sonnet-4-6",
            )

        assert mock_run.call_args.kwargs["budget_check"] is None

    def test_run_tracked_agent_raises_when_slot_admission_rejected(self, tmp_path):
        """run_tracked_agent turns a rejected atomic admission into
        WorkerCapReached before the agent ever runs — no run row written."""
        from kew.dispatch_core import WorkerCapReached, run_tracked_agent
        from kew.run_store import SqliteRunStore

        config = DispatchConfig()
        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            # Another issue already holds the single slot.
            self._pending(store, 1, "r-1")
            with pytest.raises(WorkerCapReached):
                run_tracked_agent(
                    allow_unrecorded=True,
                    prompt="p",
                    worktree_dir=tmp_path,
                    config=config,
                    issue_num=2,
                    store=store,
                    branch="agent/issue-2",
                    worker_cap=1,
                    cap_exclude_issue=2,
                )
            # Rejected admission wrote no row for issue 2.
            assert store.list_by_issue(2) == []

    @patch(f"{_P}.run_agent")
    def test_linear_fanout_admits_both_at_cap(self, mock_agent, tmp_path):
        """The headline #SFD-143 red test: a 2-node parallel fan-out of Linear
        items on an epic already running one item admits BOTH at cap 3.

        Every Linear item rides issue_num=0, so the pre-fix admission — keyed on
        ``exclude_issue=0`` — could not drop an item's OWN reservation: each of
        the two children counted itself (its reservation) plus the sibling plus
        the one live run = 3 == cap, and the atomic slot insert rejected both.
        Keyed on item_id (A3), each child excludes only itself and sees 2 < 3.
        """
        from kew.dispatch_core import WorkerCapReached, run_tracked_agent
        from kew.run_store import SqliteRunStore

        mock_agent.side_effect = lambda *a, **k: _ok_result(issue_num=0)
        runner = MagicMock()
        runner.name = "claude-code"
        runner.supports_resume.return_value = False

        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        config.parallel.max_workers = 3
        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            # One unrelated GitHub run already live; the driver reserved both
            # Linear fan-out nodes before spawning either child.
            self._pending(store, 999, "r-999")
            store.reserve_epic_node_item("epic-x", "SFD-1", "r-1")
            store.reserve_epic_node_item("epic-x", "SFD-2", "r-2")

            admitted = []
            for item in ("SFD-1", "SFD-2"):
                try:
                    run_tracked_agent(
                        allow_unrecorded=True,
                        prompt="p",
                        worktree_dir=tmp_path,
                        config=config,
                        issue_num=0,
                        item_id=item,
                        store=store,
                        branch=f"agent/{item.lower()}",
                        runner=runner,
                        model="claude-sonnet-4-6",
                        source_tracker="linear",
                        worker_cap=3,
                        cap_exclude_issue=0,
                    )
                    admitted.append(item)
                except WorkerCapReached:
                    pass

        assert admitted == ["SFD-1", "SFD-2"]


def test_normalize_reported_model():
    from kew.dispatch_core import _normalize_reported_model

    assert _normalize_reported_model("anthropic/claude-sonnet-4-6") == (
        "anthropic/claude-sonnet-4-6"
    )
    assert _normalize_reported_model("") is None
    assert _normalize_reported_model("   ") is None
    assert _normalize_reported_model(None) is None
    # control chars / newlines stripped
    assert _normalize_reported_model("a\nb\tc\x00") == "abc"
    # truncated to 256 chars
    assert len(_normalize_reported_model("x" * 500)) == 256


def test_early_exit_payload_carries_reported_model_none(tmp_path):
    # #251: every dispatch_completed payload carries reported_model (None on the
    # run-less early-exit path) so consumers never KeyError on the key.
    from kew.dispatch_core import _emit_early_exit
    from kew.run_store import SqliteRunStore

    store = SqliteRunStore(str(tmp_path / "runs.db"))
    _emit_early_exit(
        store,
        issue_num=1,
        complexity="medium",
        runner="claude-code",
        model="claude-sonnet-4-6",
        exit_code=1,
        duration_seconds=0.0,
        error_category="no_run",
        repo="o/r",
    )
    events = store.list_audit_events(event="dispatch_completed")
    assert len(events) == 1
    assert "reported_model" in events[0].payload
    assert events[0].payload["reported_model"] is None


def test_adaptive_admission_refusal_is_attested_as_additive_payload_fact(tmp_path):
    from kew.dispatch_core import _emit_early_exit
    from kew.run_store import SqliteRunStore

    store = SqliteRunStore(str(tmp_path / "runs.db"))
    _emit_early_exit(
        store,
        issue_num=1331,
        complexity="medium",
        runner="codex",
        model="switchyard/llm_classifier",
        exit_code=1,
        duration_seconds=0.0,
        error_category="transport_admission_refused",
        repo="o/r",
        admission_reason="adaptive_routing_disabled",
    )

    event = store.list_audit_events(event="dispatch_completed")[0]
    assert event.payload["error_category"] == "transport_admission_refused"
    assert event.payload["transport_admission_reason"] == "adaptive_routing_disabled"


def _swap_decision(initial, governed, *, applied: bool = True):
    """A real governor decision recommending *governed* over *initial*."""
    from kew.runners.governor import GovernorDecision

    return GovernorDecision(
        proposed_model=initial.model,
        proposed_runner=initial.runner.name,
        proposed_provider="proposed-provider",
        recommended_model=governed.model,
        recommended_runner=governed.runner.name,
        recommended_provider="governed-provider",
        candidates=[],
        warm_set=["pk-warm"],
        est_savings_usd=0.5,
        reason="warm_stronger_beats_cold" if applied else "proposed_already_cheapest",
        applied=applied,
    )


def _routing_decisions(store):
    return store.list_audit_events(event="routing_decision", limit=None)


@pytest.mark.parametrize("epic_ref", [None, "dag-1"])
def test_governor_runner_preflight_refusal_records_final_route_reason(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    epic_ref: str | None,
) -> None:
    """Also for an epic child (epic_ref set): the refusal must be printed on
    stderr there too, because that is where the child's hold text comes from
    (round-7 review: a guard that skipped the print for epic children would
    have survived a direct-dispatch-only test). The governed runner shares the
    initial runner's posture, so the swap is admitted (#1803)."""
    from kew.config import RouteEntry, RoutingConfig
    from kew.dispatch_core import execute_dispatch
    from kew.run_store import SqliteRunStore
    from kew.runners.claude_code import ClaudeCodeRunner
    from kew.runners.opencode import OpenCodeRunner
    from kew.runners.registry import ResolvedRoute

    config = DispatchConfig(
        routing=RoutingConfig(
            default=RouteEntry(runner="claude-code", model="claude-sonnet-4-6"),
            cache_aware=True,
        ),
        sandbox=SandboxConfig(enabled=False, dangerously_skip_containment=True),
    )
    config.project.repo = "owner/repo"
    initial = ResolvedRoute(ClaudeCodeRunner(), "claude-sonnet-4-6")
    governed = ResolvedRoute(OpenCodeRunner(), "z-ai-model/v1")
    refusal = "Runner 'opencode' is not logged in. Authenticate with: opencode"
    preflighted: list[str | None] = []
    launch = MagicMock()

    monkeypatch.setattr(
        "kew.dispatch_core._check_github_connectivity", lambda _config: True
    )
    monkeypatch.setattr("kew.dispatch_core.resolve_runner", lambda *a, **k: initial)
    monkeypatch.setattr(
        "kew.dispatch_core._run_governor",
        lambda **_kwargs: (governed, _swap_decision(initial, governed)),
    )
    monkeypatch.setattr("kew.dispatch_core.setup_worktree", lambda **_kwargs: tmp_path)
    monkeypatch.setattr("kew.dispatch_core.build_prompt", lambda **_kwargs: "prompt")
    monkeypatch.setattr("kew.dispatch_core.generate_repo_map", lambda *a, **k: "")
    monkeypatch.setattr("kew.dispatch_core.run_tracked_agent", launch)
    failed_with: list[str] = []
    monkeypatch.setattr(
        "kew.dispatch_core.mark_failed",
        lambda _issue, _config, message: failed_with.append(message),
    )
    monkeypatch.setattr("kew.dispatch_core._cleanup_dispatch_dir", lambda *a, **k: None)
    import kew.dispatch_core as dispatch_core

    recorded: list[dict[str, object]] = []
    real_emit = dispatch_core._emit_early_exit
    monkeypatch.setattr(
        dispatch_core,
        "_emit_early_exit",
        lambda *a, **k: (recorded.append(dict(k)), real_emit(*a, **k))[1],
    )
    monkeypatch.setattr(
        "kew.runner_preflight.runner_preflight_failure",
        lambda _config, *, runner_name=None: (
            preflighted.append(runner_name)
            or (refusal if runner_name == "opencode" else None)
        ),
    )

    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        outcome = execute_dispatch(
            allow_unrecorded=True,
            issue=_make_issue(),
            config=config,
            store=store,
            epic_ref=epic_ref,
        )
        event = store.list_audit_events(event="dispatch_completed")[0]
        posture = store.list_audit_events(event="sandbox_resolved", limit=1)[0]

    assert outcome.exit_code == 1
    assert preflighted == ["opencode"]
    launch.assert_not_called()
    assert event.payload["runner"] == "opencode"
    # The admitted swap is re-attested: the latest posture names the runner
    # that the final route launches, under the same posture (#1803).
    assert (posture.payload["runner"], posture.payload["posture"]) == (
        "opencode",
        "uncontained",
    )
    assert event.payload["transport_admission_reason"] == refusal
    # The refusal is a RUNNER fact, not a Switchyard one: the operator must
    # be sent to the runner's login, not to the adaptive route (round-4 review).
    assert recorded and recorded[-1]["error_category"] == "runner_preflight_refused"
    # The PERSISTED event, not only the argument the wrapper saw: a rewrite
    # inside the recorder would otherwise pass (round-5 review).
    assert event.payload["error_category"] == "runner_preflight_refused"
    assert failed_with == [f"Runner preflight refused: {refusal}"]
    # And on stderr: the epic child harvests its hold reason from the
    # captured stderr tail, so a message that only reached mark_failed
    # produced a hold without the login remedy (round-6 review).
    assert f"Runner preflight refused: {refusal}" in capsys.readouterr().err


@pytest.mark.parametrize("skip_containment", [True, False])
def test_governor_swap_to_a_runner_with_another_posture_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, skip_containment: bool
) -> None:
    """#1803: containment is attested for the initial route before the
    governor runs. An initial codex route (native) governed onto a warm
    claude-code prefix would launch on the host uncontained (skip flag) or
    not be containable at all (no runtime) while the ledger says native.
    The swap is refused: codex launches, under the posture attested for it.
    Mutation caught: taking the governed runner without re-resolving.
    """
    from kew.config import RouteEntry, RoutingConfig
    from kew.dispatch_core import DispatchOutcome, execute_dispatch
    from kew.run_store import SqliteRunStore
    from kew.runners.claude_code import ClaudeCodeRunner
    from kew.runners.codex import CodexRunner
    from kew.runners.registry import ResolvedRoute

    sandbox = (
        SandboxConfig(enabled=False, dangerously_skip_containment=True)
        if skip_containment
        else SandboxConfig()
    )
    config = DispatchConfig(
        routing=RoutingConfig(
            default=RouteEntry(runner="codex", model="gpt-5.6-sol"),
            cache_aware=True,
        ),
        sandbox=sandbox,
    )
    config.project.repo = "owner/repo"
    initial = ResolvedRoute(CodexRunner(), "gpt-5.6-sol")
    governed = ResolvedRoute(ClaudeCodeRunner(), "claude-sonnet-4-6")
    launch = MagicMock(return_value=_ok_result())

    monkeypatch.setattr("kew.sandbox.detect_runtime_cached", lambda _config: None)
    monkeypatch.setattr(
        "kew.dispatch_core._check_github_connectivity", lambda _config: True
    )
    monkeypatch.setattr("kew.dispatch_core.resolve_runner", lambda *a, **k: initial)
    # The real _run_governor runs; only the governor's pick is fixed.
    monkeypatch.setattr(
        "kew.dispatch_core.cache_governor",
        lambda **_kwargs: (governed, _swap_decision(initial, governed)),
    )
    monkeypatch.setattr("kew.dispatch_core.setup_worktree", lambda **_kwargs: tmp_path)
    monkeypatch.setattr("kew.dispatch_core.build_prompt", lambda **_kwargs: "prompt")
    monkeypatch.setattr("kew.dispatch_core.generate_repo_map", lambda *a, **k: "")
    monkeypatch.setattr("kew.dispatch_core.run_tracked_agent", launch)
    monkeypatch.setattr(
        "kew.dispatch_core._collect_result",
        lambda *a, **k: DispatchOutcome(exit_code=0, changed_files=[]),
    )
    monkeypatch.setattr(
        "kew.runner_preflight.runner_preflight_failure",
        lambda _config, *, runner_name=None: None,
    )

    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        outcome = execute_dispatch(
            allow_unrecorded=True,
            issue=_make_issue(),
            config=config,
            store=store,
        )
        postures = store.list_audit_events(event="sandbox_resolved", limit=None)
        routing = _routing_decisions(store)

    # Exactly one routing_decision, attesting the route that launched: the
    # refused swap is not applied, and the refusal cause is in the ledger,
    # not only on stderr (PR 2034 review round 1).
    assert len(routing) == 1
    payload = routing[0].payload
    assert payload["applied"] is False
    assert payload["reason"] == "containment_refused"
    assert payload["proposed_runner"] == "codex"
    assert (payload["recommended_runner"], payload["recommended_model"]) == (
        "codex",
        "gpt-5.6-sol",
    )
    assert payload["est_savings_usd"] == 0.0
    assert payload["containment_refusal"] == (
        "claude-code would launch with posture 'uncontained', not the attested 'native'"
    )
    assert outcome.exit_code == 0
    launch.assert_called_once()
    assert launch.call_args.kwargs["runner"] is initial.runner
    assert launch.call_args.kwargs["model"] == "gpt-5.6-sol"
    assert launch.call_args.kwargs["sandbox_posture"] == "native"
    assert postures
    assert {(e.payload["runner"], e.payload["posture"]) for e in postures} == {
        ("codex", "native")
    }


def _governed_dispatch(monkeypatch, tmp_path, initial, governor, model_override=None):
    """Run a host dispatch with the real _run_governor around *governor*.

    Returns the launch mock and every audit event, oldest first.
    """
    from kew.config import RouteEntry, RoutingConfig
    from kew.dispatch_core import DispatchOutcome, execute_dispatch
    from kew.run_store import SqliteRunStore

    config = DispatchConfig(
        routing=RoutingConfig(
            default=RouteEntry(runner=initial.runner.name, model=initial.model),
            cache_aware=True,
        ),
        sandbox=SandboxConfig(enabled=False, dangerously_skip_containment=True),
    )
    config.project.repo = "owner/repo"
    launch = MagicMock(return_value=_ok_result())
    monkeypatch.setattr(
        "kew.dispatch_core._check_github_connectivity", lambda _config: True
    )
    monkeypatch.setattr("kew.dispatch_core.resolve_runner", lambda *a, **k: initial)
    monkeypatch.setattr("kew.dispatch_core.cache_governor", governor)
    monkeypatch.setattr("kew.dispatch_core.setup_worktree", lambda **_kwargs: tmp_path)
    monkeypatch.setattr("kew.dispatch_core.build_prompt", lambda **_kwargs: "prompt")
    monkeypatch.setattr("kew.dispatch_core.generate_repo_map", lambda *a, **k: "")
    monkeypatch.setattr("kew.dispatch_core.run_tracked_agent", launch)
    monkeypatch.setattr(
        "kew.dispatch_core._collect_result",
        lambda *a, **k: DispatchOutcome(exit_code=0, changed_files=[]),
    )
    monkeypatch.setattr(
        "kew.runner_preflight.runner_preflight_failure",
        lambda _config, *, runner_name=None: None,
    )
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        outcome = execute_dispatch(
            allow_unrecorded=True,
            issue=_make_issue(),
            config=config,
            store=store,
            model_override=model_override,
        )
        events = sorted(store.list_audit_events(limit=None), key=lambda e: e.id)
    assert outcome.exit_code == 0
    return launch, events


@pytest.mark.parametrize(
    ("source_env", "source"),
    [
        ("console launchpad", "console launchpad"),
        (None, "kew dispatch --model"),
        # Only a source the ledger names is attested by name.
        ("anything else", "kew dispatch --model"),
    ],
)
def test_a_model_override_is_recorded_on_routing_decision(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    source_env: str | None,
    source: str,
) -> None:
    """#2741 — a named model bypasses the governor, but the route that launched
    is still attested once: the configured route as proposed, the override as
    what ran, and where the override was chosen."""
    from kew.run_requests import MODEL_SOURCE_ENV
    from kew.runners.claude_code import ClaudeCodeRunner
    from kew.runners.registry import ResolvedRoute

    if source_env is None:
        monkeypatch.delenv(MODEL_SOURCE_ENV, raising=False)
    else:
        monkeypatch.setenv(MODEL_SOURCE_ENV, source_env)
    initial = ResolvedRoute(ClaudeCodeRunner(), "claude-opus-5")

    def _no_governor(**_kwargs):
        raise AssertionError("an overridden model is not governed")

    _launch, events = _governed_dispatch(
        monkeypatch,
        tmp_path,
        initial,
        _no_governor,
        model_override="claude-sonnet-5-5",
    )

    routing = [e for e in events if e.event == "routing_decision"]
    assert len(routing) == 1
    payload = routing[0].payload
    assert payload["reason"] == "model_override"
    assert payload["applied"] is True
    assert payload["proposed_model"] == "claude-opus-5"
    assert payload["recommended_model"] == "claude-sonnet-5-5"
    assert payload["recommended_runner"] == "claude-code"
    assert payload["override"] == {"model": "claude-sonnet-5-5", "source": source}
    assert routing[0].run_id in {
        e.run_id for e in events if e.event != "routing_decision"
    }


def test_a_model_override_launches_and_is_attested_on_a_contained_dispatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """#2741 — the contained path (the default under sandbox.enabled='auto')
    launches the operator-named model, not the configured one, and attests the
    override on one routing_decision tied to the run, as the host path does."""
    from kew.dispatch_core import execute_dispatch
    from kew.run_requests import MODEL_SOURCE_ENV
    from kew.run_store import SqliteRunStore
    from kew.runners.claude_code import ClaudeCodeRunner
    from kew.runners.registry import ResolvedRoute
    from kew.sandbox import SandboxDecision

    monkeypatch.setenv(MODEL_SOURCE_ENV, "console launchpad")
    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    initial = ResolvedRoute(ClaudeCodeRunner(), "claude-opus-5")
    with (
        SqliteRunStore(str(tmp_path / "runs.db")) as store,
        patch(f"{_P}._check_github_connectivity", return_value=True),
        patch(f"{_P}.resolve_runner", return_value=initial),
        patch(
            f"{_P}.resolve_effective_sandbox",
            return_value=SandboxDecision(
                sandboxed=True, runtime="docker", posture="sandboxed", notice=None
            ),
        ),
        patch(f"{_P}._materialize_checkout", return_value=checkout),
        patch(f"{_P}.check_host_capacity"),
        patch(f"{_P}._run_sandboxed", return_value=_ok_result(0)) as mock_sandbox,
    ):
        outcome = execute_dispatch(
            allow_unrecorded=True,
            issue=_make_issue(),
            config=config,
            store=store,
            model_override="claude-sonnet-5-5",
        )
        events = sorted(store.list_audit_events(limit=None), key=lambda e: e.id)

    assert outcome.exit_code == 0
    assert mock_sandbox.call_args.kwargs["model"] == "claude-sonnet-5-5"
    routing = [e for e in events if e.event == "routing_decision"]
    assert len(routing) == 1
    payload = routing[0].payload
    assert payload["reason"] == "model_override"
    assert payload["proposed_model"] == "claude-opus-5"
    assert payload["recommended_model"] == "claude-sonnet-5-5"
    assert payload["override"] == {
        "model": "claude-sonnet-5-5",
        "source": "console launchpad",
    }
    assert routing[0].run_id == mock_sandbox.call_args.kwargs["run_id"]


def test_admitted_governor_swap_attests_one_applied_routing_decision(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An admitted cross-runner swap is attested once, applied, for the
    governed runner, and only after containment re-attested that runner: the
    decision is emitted after the posture check, not inside the governor
    (PR 2034 review round 1)."""
    from kew.runners.claude_code import ClaudeCodeRunner
    from kew.runners.opencode import OpenCodeRunner
    from kew.runners.registry import ResolvedRoute

    initial = ResolvedRoute(ClaudeCodeRunner(), "claude-sonnet-4-6")
    governed = ResolvedRoute(OpenCodeRunner(), "z-ai-model/v1")
    decision = _swap_decision(initial, governed)

    launch, events = _governed_dispatch(
        monkeypatch, tmp_path, initial, lambda **_kwargs: (governed, decision)
    )

    assert launch.call_args.kwargs["runner"] is governed.runner
    routing = [e for e in events if e.event == "routing_decision"]
    assert len(routing) == 1
    assert routing[0].payload == decision.to_audit_payload()
    assert routing[0].payload["applied"] is True
    assert routing[0].payload["recommended_runner"] == "opencode"
    assert "containment_refusal" not in routing[0].payload
    governed_posture = [
        e
        for e in events
        if e.event == "sandbox_resolved" and e.payload["runner"] == "opencode"
    ]
    assert len(governed_posture) == 1
    assert routing[0].id > governed_posture[0].id


def test_the_routing_decision_names_the_run_it_routed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """C3/#2300 — the attested route carries its run id.

    The run page reads one run's events off the ``audit_events.run_id`` index.
    An issue number cannot tell one attempt from the next, so without this the
    routing decision was the one event of a run the run page could not find.
    """
    from kew.runners.codex import CodexRunner
    from kew.runners.registry import ResolvedRoute

    initial = ResolvedRoute(CodexRunner(), "gpt-5.6-sol")
    decision = _swap_decision(initial, initial, applied=False)

    _launch, events = _governed_dispatch(
        monkeypatch, tmp_path, initial, lambda **_kwargs: (initial, decision)
    )

    routing = [e for e in events if e.event == "routing_decision"]
    assert len(routing) == 1
    # The same run the rest of this dispatch's events are attributed to, so a
    # run page's one indexed read picks the route up with everything else.
    attributed = {e.run_id for e in events if e.run_id}
    assert attributed, [e.event for e in events]
    assert routing[0].run_id in attributed


def test_the_dry_run_preview_writes_no_run_attributed_ledger_row(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A preview launches nothing, so no ledger row may name a run (#2302).

    The sibling test pins the ``routing_decision`` row specifically; this one
    is the wider invariant the preview has to keep. Before #2302 the preview
    wrote a route row with ``run_id`` None — a launch-shaped event attributable
    to no run — and the fix was to write nothing at all, across every event,
    not to keep writing one row with an honest null in it.
    """
    from kew.cli import _dispatch_dry_run
    from kew.run_store import SqliteRunStore
    from kew.runners.codex import CodexRunner
    from kew.runners.registry import ResolvedRoute

    initial = ResolvedRoute(CodexRunner(), "gpt-5.6-sol")
    decision = _swap_decision(initial, initial, applied=False)
    config = DispatchConfig()
    config.prompt.include_repo_map = False
    monkeypatch.setattr("kew.cli.resolve_runner", lambda *a, **k: initial)
    monkeypatch.setattr("kew.cli.remote_branch_exists", lambda _branch: False)
    monkeypatch.setattr(
        "kew.dispatch_core.cache_governor", lambda **_kwargs: (initial, decision)
    )

    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        _dispatch_dry_run(
            config,
            issue_num=42,
            issue_title="Add a widget",
            issue_body="",
            issue_labels=[{"name": "ready"}],
            complexity="medium",
            store=store,
        )
        events = store.list_audit_events(limit=None)

    assert [(e.event, e.run_id) for e in events] == []


def test_no_swap_governor_decision_is_attested_once_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from kew.runners.codex import CodexRunner
    from kew.runners.registry import ResolvedRoute

    initial = ResolvedRoute(CodexRunner(), "gpt-5.6-sol")
    decision = _swap_decision(initial, initial, applied=False)

    launch, events = _governed_dispatch(
        monkeypatch, tmp_path, initial, lambda **_kwargs: (initial, decision)
    )

    assert launch.call_args.kwargs["runner"] is initial.runner
    routing = [e for e in events if e.event == "routing_decision"]
    assert len(routing) == 1
    assert routing[0].payload == decision.to_audit_payload()
    assert "containment_refusal" not in routing[0].payload


def test_dry_run_preview_writes_no_routing_decision(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A dry run writes NOTHING to the ledger — no routing_decision (#2302).

    The audit chain attests what kew DID. This preview launches no run, opens no
    PR and spends nothing, so the row it used to write named a route for a
    dispatch that never happened, and the governance log read as a dispatch
    history with launches in it that no run row, PR or charge corresponds to.
    The governor is still evaluated — its reasoning is the whole point of the
    preview — and the decision is printed rather than stored.
    """
    from kew.cli import _dispatch_dry_run
    from kew.run_store import SqliteRunStore
    from kew.runners.codex import CodexRunner
    from kew.runners.registry import ResolvedRoute

    initial = ResolvedRoute(CodexRunner(), "gpt-5.6-sol")
    decision = _swap_decision(initial, initial, applied=False)
    config = DispatchConfig()
    config.prompt.include_repo_map = False
    monkeypatch.setattr("kew.cli.resolve_runner", lambda *a, **k: initial)
    monkeypatch.setattr("kew.cli.remote_branch_exists", lambda _branch: False)
    monkeypatch.setattr(
        "kew.dispatch_core.cache_governor", lambda **_kwargs: (initial, decision)
    )

    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        _dispatch_dry_run(
            config,
            issue_num=42,
            issue_title="Add a widget",
            issue_body="",
            issue_labels=[{"name": "ready"}],
            complexity="medium",
            store=store,
        )
        routing = _routing_decisions(store)

    assert routing == []


def _raise_governor(**_kwargs):
    raise RuntimeError("governor down")


def test_governor_error_is_attested_once_not_applied(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A governor that raised is attested (docs/configuration.md: every governor
    decision is attested, including its reason), and the run launches on the
    proposed route (PR 2034 review round 2)."""
    from kew.runners.codex import CodexRunner
    from kew.runners.registry import ResolvedRoute

    initial = ResolvedRoute(CodexRunner(), "gpt-5.6-sol")

    launch, events = _governed_dispatch(monkeypatch, tmp_path, initial, _raise_governor)

    assert launch.call_args.kwargs["runner"] is initial.runner
    routing = [e for e in events if e.event == "routing_decision"]
    assert len(routing) == 1
    assert routing[0].payload["reason"] == "governor_error"
    assert routing[0].payload["applied"] is False
    assert routing[0].payload["recommended_model"] == "gpt-5.6-sol"


def test_dry_run_preview_writes_nothing_when_the_governor_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Including the failure path: a governor that raised is still no dispatch."""
    from kew.cli import _dispatch_dry_run
    from kew.run_store import SqliteRunStore
    from kew.runners.codex import CodexRunner
    from kew.runners.registry import ResolvedRoute

    initial = ResolvedRoute(CodexRunner(), "gpt-5.6-sol")
    config = DispatchConfig()
    config.prompt.include_repo_map = False
    monkeypatch.setattr("kew.cli.resolve_runner", lambda *a, **k: initial)
    monkeypatch.setattr("kew.cli.remote_branch_exists", lambda _branch: False)
    monkeypatch.setattr("kew.dispatch_core.cache_governor", _raise_governor)

    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        _dispatch_dry_run(
            config,
            issue_num=42,
            issue_title="Add a widget",
            issue_body="",
            issue_labels=[{"name": "ready"}],
            complexity="medium",
            store=store,
        )
        routing = _routing_decisions(store)

    assert routing == []


def test_an_unstubbed_origin_branch_read_fails_instead_of_asking_origin() -> None:
    """Pin the ``_no_unstubbed_origin_branch_read`` guard.

    Both dry-run preview tests above reached the real ``git ls-remote --heads
    origin`` once ``_dispatch_dry_run`` grew its duplicate-branch guard, so
    their verdict depended on which agent branches the operator's remote
    happened to carry (PR 2191 review). A forgotten stub must fail the test.
    """
    from kew.worktree import remote_branch_exists

    with pytest.raises(AssertionError, match="real remote-branch read"):
        remote_branch_exists("agent/issue-42")


def test_direct_dispatch_launches_the_preflighted_final_runner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from kew.dispatch_core import DispatchOutcome, execute_dispatch
    from kew.run_store import SqliteRunStore
    from kew.runners.codex import CodexRunner
    from kew.runners.registry import ResolvedRoute

    config = DispatchConfig(
        sandbox=SandboxConfig(enabled=False, dangerously_skip_containment=True)
    )
    resolved = ResolvedRoute(CodexRunner(), "gpt-5.6-sol")
    preflighted: list[str | None] = []
    launch = MagicMock(return_value=_ok_result())

    monkeypatch.setattr(
        "kew.dispatch_core._check_github_connectivity", lambda _config: True
    )
    monkeypatch.setattr("kew.dispatch_core.resolve_runner", lambda *a, **k: resolved)
    monkeypatch.setattr("kew.dispatch_core.setup_worktree", lambda **_kwargs: tmp_path)
    monkeypatch.setattr("kew.dispatch_core.build_prompt", lambda **_kwargs: "prompt")
    monkeypatch.setattr("kew.dispatch_core.generate_repo_map", lambda *a, **k: "")
    monkeypatch.setattr("kew.dispatch_core.run_tracked_agent", launch)
    monkeypatch.setattr(
        "kew.dispatch_core._collect_result",
        lambda *a, **k: DispatchOutcome(exit_code=0, changed_files=[]),
    )
    monkeypatch.setattr(
        "kew.runner_preflight.runner_preflight_failure",
        lambda _config, *, runner_name=None: preflighted.append(runner_name) or None,
    )

    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        outcome = execute_dispatch(
            allow_unrecorded=True,
            issue=_make_issue(),
            config=config,
            store=store,
            model_override="gpt-5.6-sol",
        )

    assert outcome.exit_code == 0
    assert preflighted == ["codex"]
    assert launch.call_args.kwargs["runner"] is resolved.runner


def test_contained_dispatch_does_not_preflight_the_host_binary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A contained launch runs the CLI inside the image; the host binary is
    irrelevant. A host without `claude` on PATH is a valid contained launch,
    and a host-CLI preflight on the sandbox path refused it (round-4 review,
    Medium 2). Mutation caught: preflighting on the sandbox path.
    """
    from kew.dispatch_core import execute_dispatch
    from kew.run_store import SqliteRunStore
    from kew.sandbox import SandboxDecision

    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    preflighted: list[str | None] = []

    def _no_host_binary(_config: DispatchConfig, *, runner_name=None) -> str | None:
        preflighted.append(runner_name)
        return "Runner 'claude-code' needs the 'claude' CLI, which was not found."

    monkeypatch.setattr(
        "kew.runner_preflight.runner_preflight_failure", _no_host_binary
    )
    with (
        SqliteRunStore(str(tmp_path / "runs.db")) as store,
        patch(f"{_P}._check_github_connectivity", return_value=True),
        patch(
            f"{_P}.resolve_effective_sandbox",
            return_value=SandboxDecision(
                sandboxed=True, runtime="docker", posture="sandboxed", notice=None
            ),
        ),
        patch(f"{_P}._materialize_checkout", return_value=checkout),
        patch(f"{_P}._run_sandboxed", return_value=_ok_result(0)) as mock_sandbox,
    ):
        outcome = execute_dispatch(
            allow_unrecorded=True,
            issue=_make_issue(),
            config=config,
            store=store,
        )

    assert outcome.exit_code == 0
    mock_sandbox.assert_called_once()
    assert preflighted == [], "the host preflight must not run for a contained launch"


@pytest.mark.usefixtures("_canonical_existing_pr_published")
class TestLinearProviderThreading:
    """#814: the caller's ``provider`` must reach every downstream helper that
    re-validates ``item_id``. A Linear id ("SFD-12") re-validated under the
    ``provider="github"`` default raises ``InvalidItemIdError`` at prompt build
    (SFD-133 died here, twice). Exception paths must also record ``item_id`` and
    unwind the out-of-band claim."""

    @staticmethod
    def _sandbox_on():
        from kew.sandbox import SandboxDecision

        return SandboxDecision(
            sandboxed=True, runtime="docker", posture="sandboxed", notice=None
        )

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.resolve_runner")
    def test_sandboxed_linear_dispatch_builds_prompt_without_invalid_item_id(
        self, mock_resolve_runner, mock_conn, tmp_path
    ):
        # Red on current main: build_prompt at dispatch_core.py:2439 omits
        # provider, so validate_item_id("SFD-12", "github") raises and the
        # generic except swallows it -> _run_sandboxed is never reached.
        from kew.dispatch_core import execute_dispatch
        from kew.run_store import SqliteRunStore

        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        mock_resolve_runner.return_value.runner.name = "claude-code"
        mock_resolve_runner.return_value.model = "claude-sonnet-4-6"
        checkout = tmp_path / "checkout"
        checkout.mkdir()

        with (
            SqliteRunStore(str(tmp_path / "runs.db")) as store,
            patch(f"{_P}.resolve_effective_sandbox", return_value=self._sandbox_on()),
            patch(f"{_P}._materialize_checkout", return_value=checkout),
            patch(f"{_P}._run_sandboxed", return_value=_ok_result(0)) as mock_sandbox,
        ):
            outcome = execute_dispatch(
                allow_unrecorded=True,
                issue=_make_issue(num=0, title="Linear item"),
                config=config,
                store=store,
                item_id="SFD-12",
                provider="linear",
            )

        assert outcome.exit_code == 0
        mock_sandbox.assert_called_once()

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.generate_repo_map", return_value="")
    @patch(f"{_P}.setup_worktree", return_value=Path("/tmp/wt-linear"))
    @patch(f"{_P}.resolve_runner")
    @patch(f"{_P}.run_tracked_agent")
    @patch(f"{_P}.check_pr_exists", new=lambda branch, config: 99)
    @patch(f"{_P}._mark_success_pr", new=lambda *args: None)
    def test_host_linear_dispatch_threads_provider_to_worktree_and_prompt(
        self,
        mock_run,
        mock_resolve_runner,
        mock_worktree,
        mock_repomap,
        mock_conn,
        tmp_path,
    ):
        from kew.config import SandboxConfig
        from kew.dispatch_core import execute_dispatch
        from kew.run_store import SqliteRunStore

        config = DispatchConfig(
            sandbox=SandboxConfig(enabled=False, dangerously_skip_containment=True)
        )
        config.logging.dir = str(tmp_path / "logs")
        mock_resolve_runner.return_value.runner.name = "claude-code"
        mock_resolve_runner.return_value.model = "claude-sonnet-4-6"
        mock_run.return_value = _ok_result(0)

        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            outcome = execute_dispatch(
                allow_unrecorded=True,
                issue=_make_issue(num=0, title="Linear item"),
                config=config,
                store=store,
                item_id="SFD-12",
                provider="linear",
                model_override="claude-sonnet-4-6",  # skip the cache governor
            )

        assert outcome.exit_code == 0
        # setup_worktree must receive the caller's provider so _item_slug
        # validates "SFD-12" as a Linear id, not a GitHub one.
        assert mock_worktree.call_args.kwargs.get("provider") == "linear"
        # Prompt built + agent reached (no InvalidItemIdError swallowed).
        mock_run.assert_called_once()

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.mark_failed")
    @patch(f"{_P}.generate_repo_map", return_value="")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    @patch(f"{_P}.setup_worktree", return_value=Path("/tmp/wt-linear"))
    @patch(f"{_P}.resolve_runner")
    @patch(f"{_P}.run_tracked_agent")
    def test_exception_path_records_item_id_and_rolls_back_claim(
        self,
        mock_run,
        mock_resolve_runner,
        mock_worktree,
        mock_prompt,
        mock_repomap,
        mock_failed,
        mock_conn,
        tmp_path,
    ):
        # An exception mid-dispatch equally strands the Linear item In
        # Progress/assigned with no agent, and the early-exit row must be
        # findable by item id.
        from kew.config import SandboxConfig
        from kew.dispatch_core import execute_dispatch
        from kew.run_store import SqliteRunStore

        config = DispatchConfig(
            sandbox=SandboxConfig(enabled=False, dangerously_skip_containment=True)
        )
        config.logging.dir = str(tmp_path / "logs")
        config.project.repo = "octo/widgets"
        mock_resolve_runner.return_value.runner.name = "claude-code"
        mock_resolve_runner.return_value.model = "claude-sonnet-4-6"
        mock_run.side_effect = RuntimeError("boom during dispatch")
        rollback = MagicMock()

        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            outcome = execute_dispatch(
                allow_unrecorded=True,
                issue=_make_issue(num=0, title="Linear item"),
                config=config,
                store=store,
                item_id="SFD-12",
                provider="linear",
                model_override="claude-sonnet-4-6",
                on_refuse=rollback,
            )
            events = store.list_audit_events(event="dispatch_completed")

        assert outcome.exit_code == 1
        # (a) the early-exit row carries the item id so it is findable by item.
        assert len(events) == 1
        assert events[0].payload.get("item_id") == "SFD-12"
        # (b) the claim-rollback hook fires on exception, exactly as on refusal.
        rollback.assert_called_once()

    @patch(f"{_P}._check_github_connectivity")
    def test_github_auth_failure_rolls_back_claim(self, mock_conn, tmp_path):
        # A pre-spawn GitHub auth failure equally strands the claimed Linear
        # item In Progress/assigned — the rollback hook must fire, exactly as on
        # every other pre-spawn refusal.
        from kew.dispatch_core import GitHubAuthError, execute_dispatch
        from kew.run_store import SqliteRunStore

        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        mock_conn.side_effect = GitHubAuthError("gh not authenticated")
        rollback = MagicMock()

        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            outcome = execute_dispatch(
                allow_unrecorded=True,
                issue=_make_issue(num=0, title="Linear item"),
                config=config,
                store=store,
                item_id="SFD-12",
                provider="linear",
                on_refuse=rollback,
            )
            events = store.list_audit_events(event="dispatch_completed")

        assert outcome.exit_code == 3
        assert outcome.launched is False  # decided before any agent started
        assert events[0].payload.get("item_id") == "SFD-12"
        rollback.assert_called_once()

    @patch(f"{_P}._check_github_connectivity", return_value=False)
    def test_github_unreachable_rolls_back_claim(self, mock_conn, tmp_path):
        # An unreachable GitHub API pre-spawn strands the claim the same way.
        from kew.dispatch_core import execute_dispatch
        from kew.run_store import SqliteRunStore

        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        rollback = MagicMock()

        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            outcome = execute_dispatch(
                allow_unrecorded=True,
                issue=_make_issue(num=0, title="Linear item"),
                config=config,
                store=store,
                item_id="SFD-12",
                provider="linear",
                on_refuse=rollback,
            )
            events = store.list_audit_events(event="dispatch_completed")

        assert outcome.exit_code == 3
        assert outcome.launched is False  # decided before any agent started
        assert events[0].payload.get("item_id") == "SFD-12"
        rollback.assert_called_once()

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    def test_invalid_item_id_rolls_back_claim_and_reraises(self, mock_conn, tmp_path):
        # validate_item_id runs before the dispatch try; a malformed Linear id
        # (letters after the dash -> not canonical) must still release the
        # out-of-band claim (rollback fires) while preserving the raise so the
        # caller still sees the failure.
        from kew.dispatch_core import execute_dispatch
        from kew.ids import InvalidItemIdError
        from kew.run_store import SqliteRunStore

        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        rollback = MagicMock()

        with (
            SqliteRunStore(str(tmp_path / "runs.db")) as store,
            pytest.raises(InvalidItemIdError),
        ):
            execute_dispatch(
                allow_unrecorded=True,
                issue=_make_issue(num=0, title="Linear item"),
                config=config,
                store=store,
                item_id="SFD-nope",
                provider="linear",
                on_refuse=rollback,
            )

        rollback.assert_called_once()

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.mark_failed", side_effect=RuntimeError("relabel failed"))
    @patch(f"{_P}.generate_repo_map", return_value="")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    @patch(f"{_P}.setup_worktree", return_value=Path("/tmp/wt-linear"))
    @patch(f"{_P}.resolve_runner")
    @patch(f"{_P}.run_tracked_agent", side_effect=RuntimeError("boom during dispatch"))
    def test_mark_failed_raising_still_rolls_back_claim(
        self,
        mock_run,
        mock_resolve_runner,
        mock_worktree,
        mock_prompt,
        mock_repomap,
        mock_failed,
        mock_conn,
        tmp_path,
    ):
        # If failure bookkeeping (mark_failed relabelling) itself raises inside
        # the exception handler, the guaranteed rollback must still release the
        # out-of-band Linear claim instead of exiting early and stranding it.
        from kew.config import SandboxConfig
        from kew.dispatch_core import execute_dispatch
        from kew.run_store import SqliteRunStore

        config = DispatchConfig(
            sandbox=SandboxConfig(enabled=False, dangerously_skip_containment=True)
        )
        config.logging.dir = str(tmp_path / "logs")
        mock_resolve_runner.return_value.runner.name = "claude-code"
        mock_resolve_runner.return_value.model = "claude-sonnet-4-6"
        rollback = MagicMock()

        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            outcome = execute_dispatch(
                allow_unrecorded=True,
                issue=_make_issue(num=0, title="Linear item"),
                config=config,
                store=store,
                item_id="SFD-12",
                provider="linear",
                model_override="claude-sonnet-4-6",
                on_refuse=rollback,
            )

        assert outcome.exit_code == 1
        rollback.assert_called_once()

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.mark_failed")
    @patch(f"{_P}.check_budget")
    def test_budget_refusal_records_item_id(
        self, mock_check_budget, mock_failed, mock_conn, tmp_path
    ):
        # The budget refusal handler (_refuse_budget) is shared by the cheap
        # preflight and the atomic admit-time gate; both now thread the validated
        # item id into the early-exit row so a budget-refused Linear item stays
        # findable by item, exactly like the other early-exit paths.
        from kew.dispatch_core import execute_dispatch
        from kew.run_store import SqliteRunStore

        status = BudgetStatus(
            issue_spent_usd=20.0,
            issue_limit_usd=15.0,
            daily_spent_usd=0,
            daily_limit_usd=50.0,
            weekly_spent_usd=0,
            weekly_limit_usd=200.0,
        )
        mock_check_budget.side_effect = BudgetExceeded("per_issue", 15.0, 20.0, status)
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        rollback = MagicMock()

        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            outcome = execute_dispatch(
                allow_unrecorded=True,
                issue=_make_issue(num=0, title="Linear item"),
                config=config,
                store=store,
                item_id="SFD-12",
                provider="linear",
                on_refuse=rollback,
            )
            ev = store.list_audit_events(event="dispatch_completed")[0]

        assert getattr(outcome, "launched", None) is False
        assert outcome.exit_code == 1
        assert ev.payload["error_category"] == "budget_exceeded"
        assert ev.payload["item_id"] == "SFD-12"
        rollback.assert_called_once()

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.mark_failed")
    @patch(f"{_P}.check_budget")
    def test_epic_budget_refusal_writes_refusal_row_keyed_to_reservation(
        self, mock_check_budget, mock_failed, mock_conn, tmp_path
    ):
        # O2 (#891): an epic node the budget preflight refuses must leave a
        # terminal *refusal* run row keyed to the driver's reservation — not a
        # silent corpse — so the dead-child reconciler never misdiagnoses it.
        from kew.dispatch_core import execute_dispatch
        from kew.run_store import SqliteRunStore

        status = BudgetStatus(
            issue_spent_usd=20.0,
            issue_limit_usd=15.0,
            daily_spent_usd=0,
            daily_limit_usd=50.0,
            weekly_spent_usd=0,
            weekly_limit_usd=200.0,
        )
        mock_check_budget.side_effect = BudgetExceeded("per_issue", 15.0, 20.0, status)
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")

        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            # The driver reserved this node before spawning the child.
            assert store.reserve_epic_node("epic/dag-1", 647, "run-647") is True
            outcome = execute_dispatch(
                allow_unrecorded=True,
                issue=_make_issue(num=647),
                config=config,
                store=store,
                epic_ref="epic/dag-1",
                expected_reservation_run_id="run-647",
                expected_reservation_generation="run-647",
            )
            rows = store.list_by_item("647")

        assert outcome.exit_code == 1
        # A hold is non-destructive; a terminal refusal row records the refusal.
        mock_failed.assert_not_called()
        assert len(rows) == 1
        row = rows[0]
        assert row.run_id == "run-647"  # keyed to the reservation, not a fresh id
        assert row.status == "refused"  # distinct from "failure"
        assert (
            row.error_category == "budget_refused"
        )  # distinct from dispatch_child_died


@pytest.mark.usefixtures("_canonical_existing_pr_published")
class TestRepoMapDeliveryParity:
    """Task 5 / spec D5 (R3-7): all three dispatch call sites route the repo map
    through the shared ``build_repo_map`` seam. The sandbox path previously built
    prompts with no map at all; the host path is a regression guard here (its
    hint + args are unchanged, so its output stays byte-identical)."""

    _SENTINEL = "SYMBOL_MAP_SENTINEL"

    @staticmethod
    def _sandbox_on():
        from kew.sandbox import SandboxDecision

        return SandboxDecision(
            sandboxed=True, runtime="docker", posture="sandboxed", notice=None
        )

    def _dispatch_sandboxed(self, config, tmp_path) -> str:
        """Run one sandboxed dispatch, return the prompt handed to the container."""
        from kew.dispatch_core import execute_dispatch
        from kew.run_store import SqliteRunStore

        checkout = tmp_path / "checkout"
        checkout.mkdir(exist_ok=True)
        captured: dict = {}

        def _capture(**kwargs):
            captured.update(kwargs)
            return _ok_result(0)

        with (
            SqliteRunStore(str(tmp_path / "runs.db")) as store,
            patch(f"{_P}._check_github_connectivity", return_value=True),
            patch(f"{_P}.resolve_runner") as mrr,
            patch(f"{_P}.resolve_effective_sandbox", return_value=self._sandbox_on()),
            patch(f"{_P}._materialize_checkout", return_value=checkout),
            patch(f"{_P}._run_sandboxed", side_effect=_capture),
        ):
            mrr.return_value.runner.name = "claude-code"
            mrr.return_value.model = "claude-sonnet-4-6"
            outcome = execute_dispatch(
                allow_unrecorded=True,
                issue=_make_issue(num=7, title="Fix widget", body="broken"),
                config=config,
                store=store,
            )
        assert outcome.exit_code == 0
        return captured["prompt"]

    @patch(f"{_P}.generate_repo_map", return_value=_SENTINEL)
    def test_sandbox_prompt_includes_repo_map(self, _mrm, tmp_path):
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        assert config.prompt.include_repo_map  # default on
        prompt = self._dispatch_sandboxed(config, tmp_path)
        assert self._SENTINEL in prompt
        assert "## Repo Map" in prompt

    @patch(f"{_P}.generate_repo_map", return_value=_SENTINEL)
    def test_sandbox_prompt_omits_repo_map_when_disabled(self, _mrm, tmp_path):
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        config.prompt.include_repo_map = False
        prompt = self._dispatch_sandboxed(config, tmp_path)
        assert self._SENTINEL not in prompt
        assert "## Repo Map" not in prompt

    @patch(f"{_P}.generate_repo_map", return_value=_SENTINEL)
    @patch(f"{_P}.setup_worktree")
    @patch(f"{_P}.run_tracked_agent")
    @patch(f"{_P}.check_pr_exists", new=lambda branch, config: 99)
    @patch(f"{_P}._mark_success_pr", new=lambda *args: None)
    def test_host_prompt_still_includes_repo_map(self, mrun, mwt, _mrm, tmp_path):
        """Regression: the host path still delivers the map (unchanged seam)."""
        from kew.config import SandboxConfig
        from kew.dispatch_core import execute_dispatch
        from kew.run_store import SqliteRunStore

        # Non-existent path so post-run worktree cleanup short-circuits before
        # shelling out to git (not on the test allowlist); the map is built via
        # the patched generate_repo_map, so the dir need not exist.
        mwt.return_value = tmp_path / "wt-missing"
        captured: dict = {}

        def _capture(**kwargs):
            captured.update(kwargs)
            return _ok_result(0)

        mrun.side_effect = _capture
        config = DispatchConfig(
            sandbox=SandboxConfig(enabled=False, dangerously_skip_containment=True)
        )
        config.logging.dir = str(tmp_path / "logs")
        with (
            SqliteRunStore(str(tmp_path / "runs.db")) as store,
            patch(f"{_P}._check_github_connectivity", return_value=True),
            patch(f"{_P}.resolve_runner") as mrr,
        ):
            mrr.return_value.runner.name = "claude-code"
            mrr.return_value.model = "claude-sonnet-4-6"
            outcome = execute_dispatch(
                allow_unrecorded=True,
                issue=_make_issue(num=7),
                config=config,
                store=store,
                model_override="claude-sonnet-4-6",  # skip the cache governor
            )
        assert outcome.exit_code == 0
        assert self._SENTINEL in captured["prompt"]


@pytest.mark.usefixtures("_canonical_existing_pr_published")
class TestPricingPreflight:
    """#934: governed dispatch hard-errors on an unpriced model rather than
    silently metering it at $0 (which corrupts budgets, the breaker baseline,
    and the cost-per-merged-PR headline)."""

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.generate_repo_map", return_value="")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    @patch(f"{_P}.setup_worktree", return_value=Path("/tmp/wt-price"))
    @patch(f"{_P}.resolve_runner")
    @patch(f"{_P}.run_tracked_agent")
    def test_unpriced_model_aborts_before_launch(
        self,
        mock_run,
        mock_resolve_runner,
        mock_worktree,
        mock_prompt,
        mock_repomap,
        mock_conn,
        tmp_path,
    ):
        from kew.config import SandboxConfig
        from kew.dispatch_core import execute_dispatch
        from kew.run_store import SqliteRunStore

        config = DispatchConfig(
            sandbox=SandboxConfig(enabled=False, dangerously_skip_containment=True)
        )
        config.logging.dir = str(tmp_path / "logs")
        mock_resolve_runner.return_value.runner.name = "claude-code"
        mock_resolve_runner.return_value.model = "claude-sonnet-4-6"

        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            outcome = execute_dispatch(
                allow_unrecorded=True,
                issue=_make_issue(num=0, title="Unpriced"),
                config=config,
                store=store,
                # A model with no MODEL_PRICING entry and no override.
                model_override="claude-nonexistent-9",
            )

        # Fails closed: dispatch refuses and never launches the agent.
        assert outcome.exit_code == 1
        mock_run.assert_not_called()

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.generate_repo_map", return_value="")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    @patch(f"{_P}.setup_worktree", return_value=Path("/tmp/wt-price"))
    @patch(f"{_P}.resolve_runner")
    @patch(f"{_P}.run_tracked_agent")
    @patch(f"{_P}.check_pr_exists", new=lambda branch, config: 99)
    @patch(f"{_P}._mark_success_pr", new=lambda *args: None)
    def test_config_override_priced_model_dispatches(
        self,
        mock_run,
        mock_resolve_runner,
        mock_worktree,
        mock_prompt,
        mock_repomap,
        mock_conn,
        tmp_path,
    ):
        from kew.config import PricingConfig, SandboxConfig
        from kew.dispatch_core import execute_dispatch
        from kew.run_store import SqliteRunStore

        # An operator-priced model is not "unpriced": the [pricing] override
        # merges over the shipped table so the preflight lets it through.
        config = DispatchConfig(
            sandbox=SandboxConfig(enabled=False, dangerously_skip_containment=True),
            pricing=PricingConfig(overrides={"my-local-model": [1.0, 2.0]}),
        )
        config.logging.dir = str(tmp_path / "logs")
        mock_resolve_runner.return_value.runner.name = "claude-code"
        mock_resolve_runner.return_value.model = "claude-sonnet-4-6"
        mock_run.return_value = _ok_result(0)

        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            outcome = execute_dispatch(
                allow_unrecorded=True,
                issue=_make_issue(num=0, title="Priced override"),
                config=config,
                store=store,
                model_override="my-local-model",
            )

        assert outcome.exit_code == 0
        mock_run.assert_called_once()


class TestMalformedIssueData:
    """execute_dispatch rejects malformed issue data without calling run_agent."""

    @patch(f"{_P}.run_agent")
    def test_missing_number_returns_1(self, mock_run_agent: MagicMock):
        from kew.dispatch_core import execute_dispatch

        rc = execute_dispatch(
            allow_unrecorded=True, issue={"title": "Test"}, config=DispatchConfig()
        )
        assert rc.exit_code == 1
        mock_run_agent.assert_not_called()

    @patch(f"{_P}.run_agent")
    def test_wrong_number_type_returns_1(self, mock_run_agent: MagicMock):
        from kew.dispatch_core import execute_dispatch

        rc = execute_dispatch(
            allow_unrecorded=True,
            issue={"number": "not-an-int", "title": "Test"},
            config=DispatchConfig(),
        )
        assert rc.exit_code == 1
        mock_run_agent.assert_not_called()


def test_execute_dispatch_refuses_checkout_publication_before_launch(tmp_path: Path):
    from kew.dispatch_core import _LaunchResult, execute_dispatch
    from kew.overlap import DispatchOutcome
    from kew.run_store import SqliteRunStore

    config = DispatchConfig()
    config.publication.publish = "checkout"
    rollback = MagicMock()
    with (
        patch(f"{_P}._check_github_connectivity", return_value=True),
        patch(
            f"{_P}._launch_runner",
            return_value=_LaunchResult(
                early=DispatchOutcome(exit_code=0, changed_files=[])
            ),
        ) as launch,
        patch(f"{_P}._defer_publication") as defer,
        patch(f"{_P}.mark_failed") as mark_failed,
        SqliteRunStore(str(tmp_path / "dispatch.db")) as store,
    ):
        outcome = execute_dispatch(
            issue=_make_issue(),
            config=config,
            store=store,
            on_refuse=rollback,
        )
        events = store.list_audit_events(event="dispatch_completed")

    assert outcome.exit_code == 1
    launch.assert_not_called()
    defer.assert_not_called()
    rollback.assert_called_once()
    mark_failed.assert_called_once()
    assert "publication configuration refused" in mark_failed.call_args.args[2]
    assert len(events) == 1
    assert events[0].payload is not None
    assert events[0].payload["error_category"] == "publication_configuration_refused"


@pytest.mark.usefixtures("_canonical_existing_pr_published")
class TestExecuteDispatchSuccess:
    """1. Successful dispatch with PR created -> returns 0, marks completed."""

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.cleanup_worktree")
    @patch(f"{_P}.check_pr_exists", return_value=99)
    @patch(f"{_P}.update_pr_labels")
    @patch(f"{_P}.mark_completed")
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    @patch(f"{_P}.setup_worktree", return_value=Path("/tmp/wt"))
    @patch(f"{_P}.resolve_runner")
    def test_success_with_pr(
        self,
        mock_resolve_runner: MagicMock,
        mock_worktree: MagicMock,
        mock_prompt: MagicMock,
        mock_run_agent: MagicMock,
        mock_completed: MagicMock,
        mock_pr_labels: MagicMock,
        mock_pr_exists: MagicMock,
        mock_cleanup: MagicMock,
        mock_connectivity: MagicMock,
    ):
        from kew.dispatch_core import execute_dispatch

        config = DispatchConfig()
        mock_run_agent.return_value = _ok_result()

        rc = execute_dispatch(allow_unrecorded=True, issue=_make_issue(), config=config)

        assert rc.exit_code == 0
        mock_completed.assert_called_once_with(42, config)
        mock_pr_labels.assert_called_once_with(
            99, config, add=[config.labels.pr_pending_review]
        )
        mock_cleanup.assert_called_once()


@pytest.mark.usefixtures("_canonical_existing_pr_published")
class TestExecuteDispatchLabelingFailure:
    """1b. PR created but labeling fails -> does NOT mark completed."""

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.cleanup_worktree")
    @patch(f"{_P}.check_pr_exists", return_value=99)
    @patch(f"{_P}.update_pr_labels", return_value=False)
    @patch(f"{_P}.mark_completed")
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    @patch(f"{_P}.setup_worktree", return_value=Path("/tmp/wt"))
    @patch(f"{_P}.resolve_runner")
    def test_labeling_failure_skips_mark_completed(
        self,
        mock_resolve_runner: MagicMock,
        mock_worktree: MagicMock,
        mock_prompt: MagicMock,
        mock_run_agent: MagicMock,
        mock_completed: MagicMock,
        mock_pr_labels: MagicMock,
        mock_pr_exists: MagicMock,
        mock_cleanup: MagicMock,
        mock_connectivity: MagicMock,
    ):
        from kew.dispatch_core import execute_dispatch

        config = DispatchConfig()
        mock_run_agent.return_value = _ok_result()

        rc = execute_dispatch(allow_unrecorded=True, issue=_make_issue(), config=config)

        assert rc.exit_code == 0
        mock_pr_labels.assert_called_once()
        mock_completed.assert_not_called()
        mock_cleanup.assert_called_once()


class TestExecuteDispatchPRLookupFailure:
    """A failed lookup and publication preserve work and defer completion."""

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.cleanup_worktree")
    @patch(f"{_P}.check_pr_exists", side_effect=RuntimeError("gh api timeout"))
    @patch(f"{_P}.update_pr_labels")
    @patch(f"{_P}.mark_completed")
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    @patch(f"{_P}.setup_worktree", return_value=Path("/tmp/wt"))
    @patch(f"{_P}.resolve_runner")
    def test_pr_lookup_failure_skips_mark_completed(
        self,
        mock_resolve_runner: MagicMock,
        mock_worktree: MagicMock,
        mock_prompt: MagicMock,
        mock_run_agent: MagicMock,
        mock_completed: MagicMock,
        mock_pr_labels: MagicMock,
        mock_pr_exists: MagicMock,
        mock_cleanup: MagicMock,
        mock_connectivity: MagicMock,
        tmp_path: Path,
    ):
        from kew.dispatch_core import _defer_publication, execute_dispatch
        from kew.publication import PublicationError

        config = DispatchConfig()
        mock_run_agent.return_value = _ok_result()
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        (checkout / "work.txt").write_text("unpublished work\n")
        mock_worktree.return_value = checkout

        with (
            patch(
                f"{_P}._broker_agent_work",
                side_effect=PublicationError("push", "publication unavailable"),
            ) as broker,
            patch(f"{_P}._defer_publication", wraps=_defer_publication) as defer,
        ):
            rc = execute_dispatch(
                allow_unrecorded=True, issue=_make_issue(), config=config
            )

        assert rc.exit_code == 1
        broker.assert_called_once()
        defer.assert_called_once()
        assert "publication unavailable" in str(defer.call_args.args[3])
        mock_pr_labels.assert_not_called()
        mock_completed.assert_not_called()
        mock_cleanup.assert_not_called()
        assert (checkout / "work.txt").read_text() == "unpublished work\n"


class TestExecuteDispatchNoChanges:
    """2. No changes -> returns 0 and closes NOTHING (I10B).

    The zero-change path records a durable no-change candidate and emits zero
    provider writes; ``_complete_no_changes`` no longer labels, closes, or
    auto-unblocks anything.
    """

    @patch(f"{_P}._broker_agent_work", return_value=False)
    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.cleanup_worktree")
    @patch(f"{_P}._auto_unblock_dependents")
    @patch(f"{_P}.check_pr_exists", return_value=None)
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    @patch(f"{_P}.setup_worktree", return_value=Path("/tmp/wt"))
    @patch(f"{_P}.resolve_runner")
    def test_no_changes(
        self,
        mock_resolve_runner: MagicMock,
        mock_worktree: MagicMock,
        mock_prompt: MagicMock,
        mock_run_agent: MagicMock,
        mock_pr_exists: MagicMock,
        mock_unblock: MagicMock,
        mock_cleanup: MagicMock,
        mock_connectivity: MagicMock,
        mock_broker: MagicMock,
    ):
        from kew.dispatch_core import execute_dispatch

        config = DispatchConfig()
        mock_run_agent.return_value = _ok_result()

        rc = execute_dispatch(allow_unrecorded=True, issue=_make_issue(), config=config)

        assert rc.exit_code == 0
        # An absence never moves a dependent: no unblock, no issue close.
        mock_unblock.assert_not_called()
        mock_cleanup.assert_called_once()
        # The broker is consulted exactly once, with the dispatch context.
        mock_broker.assert_called_once()
        broker_args = mock_broker.call_args.args
        assert broker_args[0] == Path("/tmp/wt")  # worktree_dir from setup_worktree
        assert broker_args[-1] is config  # config threaded through


class TestExecuteDispatchFailed:
    """3. Failed dispatch -> returns exit code, marks failed, preserves worktree."""

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.cleanup_worktree")
    @patch(f"{_P}.mark_failed")
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    @patch(f"{_P}.setup_worktree", return_value=Path("/tmp/wt"))
    @patch(f"{_P}.resolve_runner")
    def test_failure_preserves_worktree(
        self,
        mock_resolve_runner: MagicMock,
        mock_worktree: MagicMock,
        mock_prompt: MagicMock,
        mock_run_agent: MagicMock,
        mock_failed: MagicMock,
        mock_cleanup: MagicMock,
        mock_connectivity: MagicMock,
    ):
        from kew.dispatch_core import execute_dispatch

        config = DispatchConfig(retry=RetryConfig(max_attempts=1))
        mock_run_agent.return_value = _fail_result(error_category="permanent")

        rc = execute_dispatch(allow_unrecorded=True, issue=_make_issue(), config=config)

        assert rc.exit_code == 1
        mock_failed.assert_called_once()
        mock_cleanup.assert_not_called()


class TestExecuteDispatchRescue:
    """3b. Agent-failure branch defers to the rescue path (#518) before hard-failing."""

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.cleanup_worktree")
    @patch(f"{_P}.mark_failed")
    @patch(f"{_P}._rescue_incomplete_run")
    @patch(f"{_P}._qualifies_for_rescue", return_value=True)
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    @patch(f"{_P}.setup_worktree", return_value=Path("/tmp/wt"))
    @patch(f"{_P}.resolve_runner")
    def test_aborted_with_work_takes_rescue_path(
        self,
        mock_resolve_runner: MagicMock,
        mock_worktree: MagicMock,
        mock_prompt: MagicMock,
        mock_run_agent: MagicMock,
        mock_qualifies: MagicMock,
        mock_rescue: MagicMock,
        mock_failed: MagicMock,
        mock_cleanup: MagicMock,
        mock_connectivity: MagicMock,
    ):
        from kew.dispatch_core import execute_dispatch

        config = DispatchConfig(retry=RetryConfig(max_attempts=1))
        mock_run_agent.return_value = _fail_result(
            error_category="permanent", stop_reason="aborted"
        )

        outcome = execute_dispatch(
            allow_unrecorded=True, issue=_make_issue(), config=config
        )

        mock_rescue.assert_called_once()
        mock_failed.assert_not_called()
        assert outcome.exit_code != 0

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.cleanup_worktree")
    @patch(f"{_P}.mark_failed")
    @patch(f"{_P}._rescue_incomplete_run", side_effect=RuntimeError("boom"))
    @patch(f"{_P}._qualifies_for_rescue", return_value=True)
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    @patch(f"{_P}.setup_worktree", return_value=Path("/tmp/wt"))
    @patch(f"{_P}.resolve_runner")
    def test_rescue_exception_falls_back_to_hard_fail(
        self,
        mock_resolve_runner: MagicMock,
        mock_worktree: MagicMock,
        mock_prompt: MagicMock,
        mock_run_agent: MagicMock,
        mock_qualifies: MagicMock,
        mock_rescue: MagicMock,
        mock_failed: MagicMock,
        mock_cleanup: MagicMock,
        mock_connectivity: MagicMock,
    ):
        from kew.dispatch_core import execute_dispatch

        config = DispatchConfig(retry=RetryConfig(max_attempts=1))
        mock_run_agent.return_value = _fail_result(
            error_category="permanent", stop_reason="aborted"
        )

        outcome = execute_dispatch(
            allow_unrecorded=True, issue=_make_issue(), config=config
        )

        mock_failed.assert_called_once()
        assert outcome.exit_code != 0

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.cleanup_worktree")
    @patch(f"{_P}.mark_failed")
    @patch(f"{_P}._rescue_incomplete_run")
    @patch(f"{_P}._qualifies_for_rescue", return_value=False)
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    @patch(f"{_P}.setup_worktree", return_value=Path("/tmp/wt"))
    @patch(f"{_P}.resolve_runner")
    def test_non_qualifying_failure_hard_fails_as_before(
        self,
        mock_resolve_runner: MagicMock,
        mock_worktree: MagicMock,
        mock_prompt: MagicMock,
        mock_run_agent: MagicMock,
        mock_qualifies: MagicMock,
        mock_rescue: MagicMock,
        mock_failed: MagicMock,
        mock_cleanup: MagicMock,
        mock_connectivity: MagicMock,
    ):
        from kew.dispatch_core import execute_dispatch

        config = DispatchConfig(retry=RetryConfig(max_attempts=1))
        mock_run_agent.return_value = _fail_result(error_category="permanent")

        outcome = execute_dispatch(
            allow_unrecorded=True, issue=_make_issue(), config=config
        )

        mock_rescue.assert_not_called()
        mock_failed.assert_called_once()
        assert outcome.exit_code != 0

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.cleanup_worktree")
    @patch(f"{_P}.mark_failed")
    @patch(f"{_P}._rescue_incomplete_run")
    @patch(f"{_P}._qualifies_for_rescue", side_effect=RuntimeError("qual boom"))
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    @patch(f"{_P}.setup_worktree", return_value=Path("/tmp/wt"))
    @patch(f"{_P}.resolve_runner")
    def test_qualification_error_falls_back_to_hard_fail(
        self,
        mock_resolve_runner: MagicMock,
        mock_worktree: MagicMock,
        mock_prompt: MagicMock,
        mock_run_agent: MagicMock,
        mock_qualifies: MagicMock,
        mock_rescue: MagicMock,
        mock_failed: MagicMock,
        mock_cleanup: MagicMock,
        mock_connectivity: MagicMock,
    ):
        from kew.dispatch_core import execute_dispatch

        config = DispatchConfig(retry=RetryConfig(max_attempts=1))
        mock_run_agent.return_value = _fail_result(
            error_category="permanent", stop_reason="aborted"
        )

        outcome = execute_dispatch(
            allow_unrecorded=True, issue=_make_issue(), config=config
        )

        # A raise from qualification must not crash the dispatch; it degrades
        # to the existing hard-fail without ever attempting the rescue.
        mock_rescue.assert_not_called()
        mock_failed.assert_called_once()
        assert outcome.exit_code != 0

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.cleanup_worktree")
    @patch(f"{_P}.mark_failed")
    @patch(f"{_P}._get_changed_files", side_effect=RuntimeError("diff boom"))
    @patch(f"{_P}._rescue_incomplete_run")
    @patch(f"{_P}._qualifies_for_rescue", return_value=True)
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    @patch(f"{_P}.setup_worktree", return_value=Path("/tmp/wt"))
    @patch(f"{_P}.resolve_runner")
    def test_rescue_survives_post_rescue_bookkeeping_error(
        self,
        mock_resolve_runner: MagicMock,
        mock_worktree: MagicMock,
        mock_prompt: MagicMock,
        mock_run_agent: MagicMock,
        mock_qualifies: MagicMock,
        mock_rescue: MagicMock,
        mock_changed: MagicMock,
        mock_failed: MagicMock,
        mock_cleanup: MagicMock,
        mock_connectivity: MagicMock,
    ):
        from kew.dispatch_core import execute_dispatch

        config = DispatchConfig(retry=RetryConfig(max_attempts=1))
        mock_run_agent.return_value = _fail_result(
            error_category="permanent", stop_reason="aborted"
        )

        outcome = execute_dispatch(
            allow_unrecorded=True, issue=_make_issue(), config=config
        )

        # A confirmed rescue is never downgraded to a hard-fail by a later
        # bookkeeping error (the changed-files diff); the rescue outcome stands.
        mock_rescue.assert_called_once()
        mock_failed.assert_not_called()
        assert outcome.exit_code != 0
        assert outcome.changed_files == []


class TestBudgetExceededUncommitted:
    """4. Budget exceeded + uncommitted -> returns 1, marks failed."""

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}._checkout_has_work", return_value=True)
    @patch(f"{_P}.mark_failed")
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    @patch(f"{_P}.setup_worktree", return_value=Path("/tmp/wt"))
    @patch(f"{_P}.resolve_runner")
    def test_budget_exceeded_uncommitted(
        self,
        mock_resolve_runner: MagicMock,
        mock_worktree: MagicMock,
        mock_prompt: MagicMock,
        mock_run_agent: MagicMock,
        mock_failed: MagicMock,
        mock_uncommitted: MagicMock,
        mock_connectivity: MagicMock,
    ):
        from kew.dispatch_core import execute_dispatch

        config = DispatchConfig()
        result = _ok_result(error_category="budget_exceeded")
        mock_run_agent.return_value = result

        rc = execute_dispatch(allow_unrecorded=True, issue=_make_issue(), config=config)

        assert rc.exit_code == 1
        mock_failed.assert_called_once()
        # Check that budget_exceeded label was included
        call_kwargs = mock_failed.call_args
        assert config.labels.budget_exceeded in call_kwargs[1]["extra_labels"]


class TestBudgetExceededCleanWorktree:
    """5. Budget exceeded with clean worktree -> returns 0, marks completed."""

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.cleanup_worktree")
    @patch(f"{_P}._checkout_has_work", return_value=False)
    @patch(f"{_P}.mark_completed")
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    @patch(f"{_P}.setup_worktree", return_value=Path("/tmp/wt"))
    @patch(f"{_P}.resolve_runner")
    def test_budget_exceeded_clean(
        self,
        mock_resolve_runner: MagicMock,
        mock_worktree: MagicMock,
        mock_prompt: MagicMock,
        mock_run_agent: MagicMock,
        mock_completed: MagicMock,
        mock_uncommitted: MagicMock,
        mock_cleanup: MagicMock,
        mock_connectivity: MagicMock,
    ):
        from kew.dispatch_core import execute_dispatch

        config = DispatchConfig()
        # The escape hatch: under canonical publication a clean budget stop is
        # a no-change candidate, not a completion (review round 11 of #1440;
        # covered in tests/test_publication_round3.py).
        config.publication.publish = "checkout"
        config.sandbox.dangerously_skip_containment = True
        result = _ok_result(error_category="budget_exceeded")
        mock_run_agent.return_value = result

        rc = execute_dispatch(allow_unrecorded=True, issue=_make_issue(), config=config)

        assert rc.exit_code == 0
        mock_completed.assert_called_once()
        mock_cleanup.assert_called_once()


class TestBudgetPreCheckFails:
    """6. Budget check fails pre-dispatch -> returns 1, marks failed."""

    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.mark_failed")
    @patch(f"{_P}.check_budget")
    def test_budget_pre_check_blocks(
        self,
        mock_check_budget: MagicMock,
        mock_failed: MagicMock,
        mock_run_agent: MagicMock,
    ):
        from kew.dispatch_core import execute_dispatch

        config = DispatchConfig()
        store = _mock_store()

        status = BudgetStatus(
            issue_spent_usd=20.0,
            issue_limit_usd=15.0,
            daily_spent_usd=0,
            daily_limit_usd=50.0,
            weekly_spent_usd=0,
            weekly_limit_usd=200.0,
        )
        mock_check_budget.side_effect = BudgetExceeded("per_issue", 15.0, 20.0, status)

        rc = execute_dispatch(
            allow_unrecorded=True, issue=_make_issue(), config=config, store=store
        )

        assert rc.exit_code == 1
        mock_failed.assert_called_once()
        mock_check_budget.assert_called_once()
        mock_run_agent.assert_not_called()

    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.mark_failed")
    @patch(f"{_P}.check_budget")
    def test_classic_refusal_names_numbers_and_knob(
        self,
        mock_check_budget: MagicMock,
        mock_failed: MagicMock,
        mock_run_agent: MagicMock,
    ):
        """Classic-queue preflight refusal keeps mark_failed but the reason
        it records names the observed spend, the cap, and the knob (#661)."""
        from kew.dispatch_core import execute_dispatch

        config = DispatchConfig()
        store = _mock_store()
        status = BudgetStatus(
            issue_spent_usd=17.08,
            issue_limit_usd=15.0,
            daily_spent_usd=0,
            daily_limit_usd=50.0,
            weekly_spent_usd=0,
            weekly_limit_usd=200.0,
        )
        mock_check_budget.side_effect = BudgetExceeded("per_issue", 15.0, 17.08, status)

        with patch("kew.dispatch_preflight._warn") as mock_warn:
            rc = execute_dispatch(
                allow_unrecorded=True, issue=_make_issue(), config=config, store=store
            )

        assert rc.exit_code == 1
        mock_failed.assert_called_once()
        # The recorded failure reason names the numbers; the knob is warned.
        reason_arg = mock_failed.call_args[0][2]
        assert "17.08" in reason_arg and "15.00" in reason_arg
        warning = mock_warn.call_args.args[0]
        assert "raise budget.per_issue_usd in kew.toml" in warning
        assert "kew dispatch" not in warning
        assert "--force" not in warning
        # Epic hold plumbing is NOT touched on the classic path.
        store.hold_epic_node.assert_not_called()


class TestBudgetPreCheckEpicHold:
    """#661: an epic-driven preflight refusal HOLDs the node instead of
    relabelling it agent-failed (which made retried nodes unretryable)."""

    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.mark_failed")
    @patch(f"{_P}.check_budget")
    def test_epic_context_holds_and_does_not_relabel(
        self,
        mock_check_budget: MagicMock,
        mock_failed: MagicMock,
        mock_run_agent: MagicMock,
    ):
        from kew.dispatch_core import execute_dispatch

        config = DispatchConfig()
        store = _mock_store()
        status = BudgetStatus(
            issue_spent_usd=17.08,
            issue_limit_usd=15.0,
            daily_spent_usd=0,
            daily_limit_usd=50.0,
            weekly_spent_usd=0,
            weekly_limit_usd=200.0,
        )
        mock_check_budget.side_effect = BudgetExceeded("per_issue", 15.0, 17.08, status)

        rc = execute_dispatch(
            allow_unrecorded=True,
            issue=_make_issue(num=647),
            config=config,
            store=store,
            epic_ref="epic/dag-1",
        )

        assert rc.exit_code == 1
        # Non-destructive: a HOLD is recorded and NO agent-failed relabel runs.
        mock_failed.assert_not_called()
        mock_run_agent.assert_not_called()
        store.hold_epic_node.assert_called_once()
        called = store.hold_epic_node.call_args[0]
        assert called[0] == "epic/dag-1"
        # Held on the durable item id (a GitHub number rides as its str).
        assert called[1] == "647"
        reason = called[2]
        # Overlay routes budget_preflight-prefixed holds to HELD.
        assert reason.startswith("budget_preflight")
        # The reason carries observed/cap numbers + the real config knob.
        assert "17.08" in reason and "15.00" in reason
        assert "raise budget.per_issue_usd in kew.toml" in reason
        assert "kew dispatch" not in reason
        assert "--force" not in reason

    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.mark_failed")
    @patch(f"{_P}.check_budget")
    def test_linear_hold_keyed_by_item_id_without_bogus_command(
        self,
        mock_check_budget: MagicMock,
        mock_failed: MagicMock,
        mock_run_agent: MagicMock,
    ):
        # #809: a non-GitHub node rides ``issue_num=0`` but the hold must use
        # the durable item id. The remedy must not invent a dispatch command
        # that accepts neither Linear IDs nor the documented force flag.
        from kew.dispatch_core import execute_dispatch

        config = DispatchConfig()
        store = _mock_store()
        status = BudgetStatus(
            issue_spent_usd=17.08,
            issue_limit_usd=15.0,
            daily_spent_usd=0,
            daily_limit_usd=50.0,
            weekly_spent_usd=0,
            weekly_limit_usd=200.0,
        )
        mock_check_budget.side_effect = BudgetExceeded("per_issue", 15.0, 17.08, status)

        rc = execute_dispatch(
            allow_unrecorded=True,
            issue=_make_issue(num=0, title="Linear item"),
            config=config,
            store=store,
            epic_ref="epic/dag-1",
            item_id="SFD-138",
            provider="linear",
        )

        assert rc.exit_code == 1
        store.hold_epic_node.assert_called_once()
        called = store.hold_epic_node.call_args[0]
        # Held on the durable Linear id, not the ``0`` sentinel.
        assert called[1] == "SFD-138"
        reason = called[2]
        assert "raise budget.per_issue_usd in kew.toml" in reason
        assert "kew dispatch" not in reason
        assert "--force" not in reason
        assert "dispatch 0" not in reason
        # check_budget was threaded the item id so the gate keyed per item.
        assert mock_check_budget.call_args.kwargs.get("item_id") == "SFD-138"


@pytest.mark.usefixtures("_canonical_existing_pr_published")
class TestRetryTransientThenSucceed:
    """7. Retry on transient error then succeed -> returns 0."""

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.time.sleep")
    @patch(f"{_P}.cleanup_worktree")
    @patch(f"{_P}.update_pr_labels")
    @patch(f"{_P}.check_pr_exists")
    @patch(f"{_P}.mark_completed")
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    @patch(f"{_P}.setup_worktree", return_value=Path("/tmp/wt"))
    @patch(f"{_P}.resolve_runner")
    def test_retry_then_succeed(
        self,
        mock_resolve_runner: MagicMock,
        mock_worktree: MagicMock,
        mock_prompt: MagicMock,
        mock_run_agent: MagicMock,
        mock_completed: MagicMock,
        mock_pr_exists: MagicMock,
        mock_pr_labels: MagicMock,
        mock_cleanup: MagicMock,
        mock_sleep: MagicMock,
        mock_connectivity: MagicMock,
    ):
        from kew.dispatch_core import execute_dispatch

        config = DispatchConfig(
            retry=RetryConfig(max_attempts=3, max_resume_attempts=2)
        )
        mock_resolve_runner.return_value.supports_resume.return_value = True
        mock_run_agent.side_effect = [
            _fail_result(error_category="transient", session_id="sess-1"),
            _ok_result(),
        ]
        # None: retry-loop guard (attempt=1) should not short-circuit
        # 99: post-loop success path (PR exists -> mark_completed)
        mock_pr_exists.side_effect = [None, 99]

        rc = execute_dispatch(allow_unrecorded=True, issue=_make_issue(), config=config)

        assert rc.exit_code == 0
        assert mock_run_agent.call_count == 2
        mock_completed.assert_called_once()
        assert len(_backoff_sleeps(mock_sleep)) == 1


@pytest.mark.usefixtures("_canonical_existing_pr_published")
class TestResumeStrategyOnFirstRetry:
    """8. Resume strategy used on first retry -> uses session_id."""

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.time.sleep")
    @patch(f"{_P}.cleanup_worktree")
    @patch(f"{_P}.update_pr_labels")
    @patch(f"{_P}.check_pr_exists")
    @patch(f"{_P}.mark_completed")
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    @patch(f"{_P}.setup_worktree", return_value=Path("/tmp/wt"))
    @patch(f"{_P}.resolve_runner")
    def test_resume_session_id_passed(
        self,
        mock_resolve_runner: MagicMock,
        mock_worktree: MagicMock,
        mock_prompt: MagicMock,
        mock_run_agent: MagicMock,
        mock_completed: MagicMock,
        mock_pr_exists: MagicMock,
        mock_pr_labels: MagicMock,
        mock_cleanup: MagicMock,
        mock_sleep: MagicMock,
        mock_connectivity: MagicMock,
    ):
        from kew.dispatch_core import execute_dispatch

        config = DispatchConfig(
            retry=RetryConfig(max_attempts=3, max_resume_attempts=2)
        )
        mock_resolve_runner.return_value.supports_resume.return_value = True
        mock_run_agent.side_effect = [
            _fail_result(error_category="transient", session_id="sess-abc"),
            _ok_result(),
        ]
        # None: retry-loop guard (attempt=1) should not short-circuit
        # 99: post-loop success path (PR exists -> mark_completed)
        mock_pr_exists.side_effect = [None, 99]

        rc = execute_dispatch(allow_unrecorded=True, issue=_make_issue(), config=config)

        assert rc.exit_code == 0
        # The second run_agent call should have resume_session_id="sess-abc"
        second_call = mock_run_agent.call_args_list[1]
        assert second_call[1]["resume_session_id"] == "sess-abc"


class TestPermanentErrorNoRetry:
    """9. Permanent error -> no retry."""

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.mark_failed")
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    @patch(f"{_P}.setup_worktree", return_value=Path("/tmp/wt"))
    @patch(f"{_P}.resolve_runner")
    def test_permanent_no_retry(
        self,
        mock_resolve_runner: MagicMock,
        mock_worktree: MagicMock,
        mock_prompt: MagicMock,
        mock_run_agent: MagicMock,
        mock_failed: MagicMock,
        mock_connectivity: MagicMock,
    ):
        from kew.dispatch_core import execute_dispatch

        config = DispatchConfig(retry=RetryConfig(max_attempts=3))
        mock_run_agent.return_value = _fail_result(error_category="permanent")

        rc = execute_dispatch(allow_unrecorded=True, issue=_make_issue(), config=config)

        assert rc.exit_code == 1
        assert mock_run_agent.call_count == 1
        mock_failed.assert_called_once()


@pytest.mark.usefixtures("_canonical_existing_pr_published")
class TestCostAwareBudgetGuard:
    """#642: a budget kill that escapes subtype/exit classification must not
    be retried. Before retrying ANY failure the ladder compares accumulated
    cost against the effective runtime cap; cost >= cap*0.9 -> budget_stopped.
    Reproduces wordly run #5 (cap $0.15, final cost $0.53, exit 1 / SIGTERM-143
    with no budget subtype -> the old ladder re-ran it, tripling spend).
    """

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.time.sleep")
    @patch(f"{_P}.mark_failed")
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    @patch(f"{_P}.setup_worktree", return_value=Path("/tmp/wt"))
    @patch(f"{_P}.resolve_runner")
    def test_exit1_cost_at_cap_no_subtype_stops_retry(
        self,
        mock_resolve_runner: MagicMock,
        mock_worktree: MagicMock,
        mock_prompt: MagicMock,
        mock_run_agent: MagicMock,
        mock_failed: MagicMock,
        mock_sleep: MagicMock,
        mock_connectivity: MagicMock,
    ):
        from kew.dispatch_core import execute_dispatch

        config = DispatchConfig(retry=RetryConfig(max_attempts=3))
        config.agent.max_budget_usd = 0.15
        # Misclassified as transient (the escape): exit 1, cost over cap,
        # no budget flag. Guard must catch it via cost alone.
        mock_run_agent.return_value = _fail_result(
            error_category="transient",
            reported_cost_usd=0.53,
        )

        rc = execute_dispatch(allow_unrecorded=True, issue=_make_issue(), config=config)

        assert rc.exit_code == 1
        assert mock_run_agent.call_count == 1  # not retried
        assert _backoff_sleeps(mock_sleep) == []
        label = mock_failed.call_args[1]["extra_labels"]
        assert config.labels.budget_stopped in label

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.time.sleep")
    @patch(f"{_P}.mark_failed")
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    @patch(f"{_P}.setup_worktree", return_value=Path("/tmp/wt"))
    @patch(f"{_P}.resolve_runner")
    def test_sigterm143_with_cost_stops_retry(
        self,
        mock_resolve_runner: MagicMock,
        mock_worktree: MagicMock,
        mock_prompt: MagicMock,
        mock_run_agent: MagicMock,
        mock_failed: MagicMock,
        mock_sleep: MagicMock,
        mock_connectivity: MagicMock,
    ):
        from kew.dispatch_core import execute_dispatch

        config = DispatchConfig(retry=RetryConfig(max_attempts=3))
        config.agent.max_budget_usd = 0.15
        # SIGTERM-143 shape: runner labelled it watchdog_kill (retryable), but
        # the accumulated cost is over cap -> guard overrides and stops.
        mock_run_agent.return_value = _fail_result(
            exit_code=143,
            error_category="watchdog_kill",
            reported_cost_usd=0.53,
        )

        execute_dispatch(allow_unrecorded=True, issue=_make_issue(), config=config)

        assert mock_run_agent.call_count == 1
        assert _backoff_sleeps(mock_sleep) == []
        label = mock_failed.call_args[1]["extra_labels"]
        assert config.labels.budget_stopped in label

    @patch(f"{_P}.mark_completed")
    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.time.sleep")
    @patch(f"{_P}.cleanup_worktree")
    @patch(f"{_P}.update_pr_labels")
    @patch(f"{_P}.check_pr_exists")
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    @patch(f"{_P}.setup_worktree", return_value=Path("/tmp/wt"))
    @patch(f"{_P}.resolve_runner")
    def test_cost_below_cap_still_retries(
        self,
        mock_resolve_runner: MagicMock,
        mock_worktree: MagicMock,
        mock_prompt: MagicMock,
        mock_run_agent: MagicMock,
        mock_pr_exists: MagicMock,
        mock_pr_labels: MagicMock,
        mock_cleanup: MagicMock,
        mock_sleep: MagicMock,
        mock_connectivity: MagicMock,
        mock_completed: MagicMock,
    ):
        from kew.dispatch_core import execute_dispatch

        config = DispatchConfig(
            retry=RetryConfig(max_attempts=3, max_resume_attempts=2)
        )
        config.agent.max_budget_usd = 0.15
        mock_resolve_runner.return_value.supports_resume.return_value = True
        # A cheap transient failure well under cap must still retry normally.
        mock_run_agent.side_effect = [
            _fail_result(error_category="transient", reported_cost_usd=0.01),
            _ok_result(),
        ]
        mock_pr_exists.side_effect = [None, 99]

        rc = execute_dispatch(allow_unrecorded=True, issue=_make_issue(), config=config)

        assert rc.exit_code == 0
        assert mock_run_agent.call_count == 2  # guard did not over-fire


class TestFailureSummaryHelpers:
    """Group 4 (CS-8): the hard-fail summary must name the reason + attempt
    count, and a category-tailored 'Next:' block must give exact commands.
    Pure-function coverage (the rendering is exercised end-to-end below)."""

    def test_summary_names_budget_reason_and_attempts(self):
        from kew.dispatch_core import _failure_summary_line

        line = _failure_summary_line(
            issue_num=5,
            error_category="budget_stopped",
            exit_code=1,
            total_attempts=1,
            budget_cap_usd=0.15,
            cost_usd=0.53,
        )
        assert "#5" in line
        assert "budget cap hit ($0.15)" in line
        assert "1 attempt" in line
        assert "1 attempts" not in line  # singular
        assert "(exit 1)" not in line  # category known -> never bare exit

    def test_summary_names_generic_reason_and_attempts(self):
        from kew.dispatch_core import _failure_summary_line

        line = _failure_summary_line(
            issue_num=5,
            error_category="transient",
            exit_code=1,
            total_attempts=3,
        )
        assert "agent error (exit 1)" in line
        assert "3 attempts" in line

    def test_summary_falls_back_to_bare_exit_without_category(self):
        from kew.dispatch_core import _failure_summary_line

        line = _failure_summary_line(
            issue_num=7,
            error_category="",
            exit_code=2,
            total_attempts=1,
        )
        assert "(exit 2)" in line
        assert "1 attempt" in line

    def test_next_step_block_budget_variant(self):
        from kew.dispatch_core import _next_step_lines

        lines = _next_step_lines(
            issue_num=5,
            error_category="budget_stopped",
            log_file="/tmp/d.log",
        )
        assert lines[0] == "Next:"
        assert len(lines) <= 3
        joined = "\n".join(lines)
        assert "agent.max_budget_usd" in joined
        assert "kew dispatch 5" in joined
        assert "/tmp/d.log" in joined

    def test_next_step_block_generic_variant(self):
        from kew.dispatch_core import _next_step_lines

        lines = _next_step_lines(
            issue_num=9,
            error_category="transient",
            log_file="/tmp/d.log",
        )
        joined = "\n".join(lines)
        assert "kew dispatch 9" in joined
        assert "agent.max_budget_usd" not in joined  # generic, not budget
        assert "/tmp/d.log" in joined


class TestFailureSummaryOutput:
    """Group 4 (CS-8): end-to-end rendering of the reason line, next-step
    block, and foreground attempt visibility."""

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.time.sleep")
    @patch(f"{_P}.mark_failed")
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    @patch(f"{_P}.setup_worktree", return_value=Path("/tmp/wt"))
    @patch(f"{_P}.resolve_runner")
    def test_budget_stop_prints_reason_and_next_step(
        self,
        mock_resolve_runner: MagicMock,
        mock_worktree: MagicMock,
        mock_prompt: MagicMock,
        mock_run_agent: MagicMock,
        mock_failed: MagicMock,
        mock_sleep: MagicMock,
        mock_connectivity: MagicMock,
        capsys: pytest.CaptureFixture[str],
    ):
        from kew.dispatch_core import execute_dispatch

        config = DispatchConfig(retry=RetryConfig(max_attempts=3))
        config.agent.max_budget_usd = 0.15
        # Misclassified transient over cap -> the #642 guard routes it to
        # budget_stopped on the first attempt.
        mock_run_agent.return_value = _fail_result(
            error_category="transient", reported_cost_usd=0.53
        )

        execute_dispatch(allow_unrecorded=True, issue=_make_issue(num=5), config=config)

        err = capsys.readouterr().err
        assert "budget cap hit ($0.15)" in err
        assert "1 attempt" in err
        assert "Next:" in err
        assert "agent.max_budget_usd" in err
        assert "kew dispatch 5" in err

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.mark_failed")
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    @patch(f"{_P}.setup_worktree", return_value=Path("/tmp/wt"))
    @patch(f"{_P}.resolve_runner")
    def test_generic_hard_fail_prints_reason_and_next_step(
        self,
        mock_resolve_runner: MagicMock,
        mock_worktree: MagicMock,
        mock_prompt: MagicMock,
        mock_run_agent: MagicMock,
        mock_failed: MagicMock,
        mock_connectivity: MagicMock,
        capsys: pytest.CaptureFixture[str],
    ):
        from kew.dispatch_core import execute_dispatch

        config = DispatchConfig(retry=RetryConfig(max_attempts=1))
        mock_run_agent.return_value = _fail_result(error_category="permanent")

        execute_dispatch(allow_unrecorded=True, issue=_make_issue(num=8), config=config)

        err = capsys.readouterr().err
        assert "agent error (exit 1)" in err
        assert "1 attempt" in err
        assert "Next:" in err
        assert "kew dispatch 8" in err
        assert "agent.max_budget_usd" not in err  # generic, not budget

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.check_pr_exists", return_value=None)
    @patch(f"{_P}.time.sleep")
    @patch(f"{_P}.mark_failed")
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    @patch(f"{_P}.setup_worktree", return_value=Path("/tmp/wt"))
    @patch(f"{_P}.resolve_runner")
    def test_retry_warn_shows_ladder_position_and_strategy(
        self,
        mock_resolve_runner: MagicMock,
        mock_worktree: MagicMock,
        mock_prompt: MagicMock,
        mock_run_agent: MagicMock,
        mock_failed: MagicMock,
        mock_sleep: MagicMock,
        mock_pr_exists: MagicMock,
        mock_connectivity: MagicMock,
        capsys: pytest.CaptureFixture[str],
    ):
        from kew.dispatch_core import execute_dispatch

        config = DispatchConfig(
            retry=RetryConfig(max_attempts=3, max_resume_attempts=2)
        )
        mock_resolve_runner.return_value.supports_resume.return_value = True
        mock_run_agent.return_value = _fail_result(
            error_category="transient", session_id="s1"
        )

        execute_dispatch(allow_unrecorded=True, issue=_make_issue(), config=config)

        err = capsys.readouterr().err
        assert "Attempt 1/3 failed (transient)" in err
        assert "retrying resume" in err


class TestExceptionDuringDispatch:
    """10. Exception during dispatch -> marks failed, returns 1."""

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.mark_failed")
    @patch(f"{_P}.setup_worktree")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    def test_exception_marks_failed(
        self,
        mock_prompt: MagicMock,
        mock_worktree: MagicMock,
        mock_failed: MagicMock,
        mock_connectivity: MagicMock,
    ):
        from kew.dispatch_core import execute_dispatch

        config = DispatchConfig()
        mock_worktree.side_effect = RuntimeError("worktree exploded")

        rc = execute_dispatch(allow_unrecorded=True, issue=_make_issue(), config=config)

        assert rc.exit_code == 1
        mock_failed.assert_called_once()
        # Check the reason is the generic message (not the raw exception text)
        reason_arg = mock_failed.call_args[0][2]
        assert reason_arg == "internal error during dispatch"

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.mark_failed")
    @patch(f"{_P}.setup_worktree")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    def test_exception_message_not_leaked_to_github(
        self,
        mock_prompt: MagicMock,
        mock_worktree: MagicMock,
        mock_failed: MagicMock,
        mock_connectivity: MagicMock,
    ):
        """Exception details must NOT appear in the mark_failed GitHub comment.

        The raw exception text may contain sensitive info (file paths, API keys,
        internal state). Only a generic message should be posted to GitHub; the
        detailed error is retained in the local log via _error().
        """
        from kew.dispatch_core import execute_dispatch

        config = DispatchConfig()
        sensitive_detail = "secret-api-key=/home/user/.config/token.txt"
        mock_worktree.side_effect = RuntimeError(sensitive_detail)

        rc = execute_dispatch(allow_unrecorded=True, issue=_make_issue(), config=config)

        assert rc.exit_code == 1
        mock_failed.assert_called_once()
        reason_arg = mock_failed.call_args[0][2]
        # The exception text must NOT appear in the GitHub comment body
        assert sensitive_detail not in reason_arg, (
            f"Exception detail leaked to GitHub comment: {reason_arg!r}"
        )


@pytest.mark.usefixtures("_canonical_existing_pr_published")
class TestCostAccumulatedAcrossRetries:
    """11. Cost accumulated across retries."""

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.time.sleep")
    @patch(f"{_P}.cleanup_worktree")
    @patch(f"{_P}.update_pr_labels")
    @patch(f"{_P}.check_pr_exists")
    @patch(f"{_P}.mark_completed")
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    @patch(f"{_P}.setup_worktree", return_value=Path("/tmp/wt"))
    @patch(f"{_P}.resolve_runner")
    def test_cost_accumulation(
        self,
        mock_resolve_runner: MagicMock,
        mock_worktree: MagicMock,
        mock_prompt: MagicMock,
        mock_run_agent: MagicMock,
        mock_completed: MagicMock,
        mock_pr_exists: MagicMock,
        mock_pr_labels: MagicMock,
        mock_cleanup: MagicMock,
        mock_sleep: MagicMock,
        mock_connectivity: MagicMock,
    ):
        from kew.dispatch_core import execute_dispatch

        config = DispatchConfig(
            retry=RetryConfig(max_attempts=3, max_resume_attempts=2)
        )

        fail_r = _fail_result(error_category="transient")
        fail_r.input_tokens = 1000
        fail_r.output_tokens = 500

        ok_r = _ok_result()
        ok_r.input_tokens = 2000
        ok_r.output_tokens = 800

        mock_resolve_runner.return_value.supports_resume.return_value = True
        mock_run_agent.side_effect = [fail_r, ok_r]
        # None: retry-loop guard (attempt=1) should not short-circuit
        # 99: post-loop success path (PR exists -> mark_completed)
        mock_pr_exists.side_effect = [None, 99]

        store = _mock_store()
        store.get.return_value = MagicMock()

        rc = execute_dispatch(
            allow_unrecorded=True, issue=_make_issue(), config=config, store=store
        )

        assert rc.exit_code == 0
        # After merge, ok_r should have accumulated tokens from fail_r
        # 2000 + 1000 = 3000 input, 800 + 500 = 1300 output
        assert ok_r.input_tokens == 3000
        assert ok_r.output_tokens == 1300
        assert ok_r.total_tokens == 4300


@pytest.mark.usefixtures("_canonical_existing_pr_published")
class TestNotificationSentOnSuccess:
    """12a. Notification sent on success."""

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.cleanup_worktree")
    @patch(f"{_P}.check_pr_exists", return_value=99)
    @patch(f"{_P}.update_pr_labels")
    @patch(f"{_P}.mark_completed")
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    @patch(f"{_P}.setup_worktree", return_value=Path("/tmp/wt"))
    @patch(f"{_P}.resolve_runner")
    def test_notifier_called_on_success(
        self,
        mock_resolve_runner: MagicMock,
        mock_worktree: MagicMock,
        mock_prompt: MagicMock,
        mock_run_agent: MagicMock,
        mock_completed: MagicMock,
        mock_pr_labels: MagicMock,
        mock_pr_exists: MagicMock,
        mock_cleanup: MagicMock,
        mock_connectivity: MagicMock,
    ):
        from kew.config import AlertingConfig
        from kew.dispatch_core import execute_dispatch

        config = DispatchConfig(
            alerting=AlertingConfig(
                notify_on_success=True,
            )
        )
        mock_run_agent.return_value = _ok_result()
        notifier = MagicMock()

        rc = execute_dispatch(
            allow_unrecorded=True, issue=_make_issue(), config=config, notifier=notifier
        )

        assert rc.exit_code == 0
        notifier.notify.assert_called_once()
        event = notifier.notify.call_args[0][0]
        assert event.event_type == "dispatch_succeeded"


class TestNotificationSentOnFailure:
    """12b. Notification sent on failure."""

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.mark_failed")
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    @patch(f"{_P}.setup_worktree", return_value=Path("/tmp/wt"))
    @patch(f"{_P}.resolve_runner")
    def test_notifier_called_on_failure(
        self,
        mock_resolve_runner: MagicMock,
        mock_worktree: MagicMock,
        mock_prompt: MagicMock,
        mock_run_agent: MagicMock,
        mock_failed: MagicMock,
        mock_connectivity: MagicMock,
    ):
        from kew.config import AlertingConfig
        from kew.dispatch_core import execute_dispatch

        config = DispatchConfig(
            retry=RetryConfig(max_attempts=1),
            alerting=AlertingConfig(
                notify_on_failure=True,
            ),
        )
        mock_run_agent.return_value = _fail_result(error_category="permanent")
        notifier = MagicMock()

        rc = execute_dispatch(
            allow_unrecorded=True, issue=_make_issue(), config=config, notifier=notifier
        )

        assert rc.exit_code == 1
        notifier.notify.assert_called_once()
        event = notifier.notify.call_args[0][0]
        assert event.event_type == "dispatch_failed"


class TestCheckGithubConnectivity:
    """Tests for _check_github_connectivity helper."""

    @patch(f"{_P}.run")
    def test_returns_true_when_gh_succeeds(self, mock_run: MagicMock):
        from kew.config import DispatchConfig
        from kew.dispatch_core import _check_github_connectivity

        mock_run.return_value = MagicMock(returncode=0)

        result = _check_github_connectivity(DispatchConfig())

        assert result is True
        mock_run.assert_called_once_with(
            ["gh", "api", "rate_limit"], check=False, timeout=10
        )

    @patch(f"{_P}.run")
    def test_returns_false_when_gh_fails(self, mock_run: MagicMock):
        from kew.config import DispatchConfig
        from kew.dispatch_core import _check_github_connectivity

        mock_run.return_value = MagicMock(returncode=1)

        result = _check_github_connectivity(DispatchConfig())

        assert result is False

    @patch(f"{_P}.run")
    def test_returns_false_on_nonzero_exit_code(self, mock_run: MagicMock):
        from kew.config import DispatchConfig
        from kew.dispatch_core import _check_github_connectivity

        mock_run.return_value = MagicMock(returncode=127)

        result = _check_github_connectivity(DispatchConfig())

        assert result is False

    @patch(f"{_P}.run")
    def test_raises_github_auth_error_on_401(self, mock_run: MagicMock):
        from kew.config import DispatchConfig
        from kew.dispatch_core import GitHubAuthError, _check_github_connectivity

        mock_run.return_value = MagicMock(
            returncode=1,
            stderr="HTTP 401: Bad credentials (https://api.github.com/rate_limit)",
        )

        with pytest.raises(GitHubAuthError):
            _check_github_connectivity(DispatchConfig())

    @patch(f"{_P}.run")
    def test_raises_github_auth_error_on_unauthorized(self, mock_run: MagicMock):
        from kew.config import DispatchConfig
        from kew.dispatch_core import GitHubAuthError, _check_github_connectivity

        mock_run.return_value = MagicMock(
            returncode=1,
            stderr="error: Unauthorized - token expired",
        )

        with pytest.raises(GitHubAuthError):
            _check_github_connectivity(DispatchConfig())

    @patch(f"{_P}.run")
    def test_auth_error_network_failure_returns_false(self, mock_run: MagicMock):
        """Non-auth failures (e.g. network unreachable) still return False."""
        from kew.config import DispatchConfig
        from kew.dispatch_core import _check_github_connectivity

        mock_run.return_value = MagicMock(returncode=1, stderr="connection refused")

        result = _check_github_connectivity(DispatchConfig())

        assert result is False


class TestCheckGithubConnectivityErrorMessages:
    """_check_github_connectivity emits specific error messages per failure type."""

    @patch(f"{_P}.run")
    @patch(f"{_P}.time")
    def test_raises_auth_error_when_auth_fails(
        self, mock_time: MagicMock, mock_run: MagicMock
    ):
        from kew.config import DispatchConfig
        from kew.dispatch_core import GitHubAuthError, _check_github_connectivity

        mock_run.return_value = MagicMock(
            returncode=1, stderr="HTTP 401: credentials required", stdout=""
        )

        with pytest.raises(GitHubAuthError):
            _check_github_connectivity(DispatchConfig())

    @patch(f"{_P}.run")
    @patch(f"{_P}.time")
    def test_logs_rate_limit_message_when_rate_limited(
        self, mock_time: MagicMock, mock_run: MagicMock
    ):
        from kew.config import DispatchConfig
        from kew.dispatch_core import _check_github_connectivity

        mock_run.return_value = MagicMock(
            returncode=1, stderr="API rate limit exceeded", stdout=""
        )

        result = _check_github_connectivity(DispatchConfig())

        assert result is False

    @patch(f"{_P}.run")
    def test_returns_false_on_exception(self, mock_run: MagicMock):
        from kew.config import DispatchConfig
        from kew.dispatch_core import _check_github_connectivity

        mock_run.side_effect = OSError("network unreachable")

        result = _check_github_connectivity(DispatchConfig())

        assert result is False

    @patch(f"{_P}.run")
    @patch(f"{_P}.time")
    def test_retries_on_rate_limit_before_giving_up(
        self, mock_time: MagicMock, mock_run: MagicMock
    ):
        from kew.config import DispatchConfig
        from kew.dispatch_core import _check_github_connectivity

        rate_limited = MagicMock(returncode=1, stderr="rate limit exceeded", stdout="")
        success = MagicMock(returncode=0, stderr="", stdout="{}")
        mock_run.side_effect = [rate_limited, success]

        result = _check_github_connectivity(DispatchConfig())

        assert result is True
        assert mock_run.call_count == 2


@pytest.mark.usefixtures("_canonical_existing_pr_published")
class TestConnectivityCheckInExecuteDispatch:
    """execute_dispatch returns exit code 3 when GitHub is unreachable."""

    @patch(f"{_P}._check_github_connectivity", return_value=False)
    @patch(f"{_P}.setup_worktree")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    def test_returns_3_when_unreachable(
        self,
        mock_prompt: MagicMock,
        mock_worktree: MagicMock,
        mock_connectivity: MagicMock,
    ):
        from kew.dispatch_core import execute_dispatch

        config = DispatchConfig()

        rc = execute_dispatch(allow_unrecorded=True, issue=_make_issue(), config=config)

        assert rc.exit_code == 3
        mock_worktree.assert_not_called()

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.cleanup_worktree")
    @patch(f"{_P}.check_pr_exists", return_value=99)
    @patch(f"{_P}.update_pr_labels")
    @patch(f"{_P}.mark_completed")
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    @patch(f"{_P}.setup_worktree", return_value=Path("/tmp/wt"))
    @patch(f"{_P}.resolve_runner")
    def test_proceeds_when_reachable(
        self,
        mock_resolve_runner: MagicMock,
        mock_worktree: MagicMock,
        mock_prompt: MagicMock,
        mock_run_agent: MagicMock,
        mock_completed: MagicMock,
        mock_pr_labels: MagicMock,
        mock_pr_exists: MagicMock,
        mock_cleanup: MagicMock,
        mock_connectivity: MagicMock,
    ):
        from kew.dispatch_core import execute_dispatch

        config = DispatchConfig()
        mock_run_agent.return_value = _ok_result()

        rc = execute_dispatch(allow_unrecorded=True, issue=_make_issue(), config=config)

        assert rc.exit_code == 0
        mock_worktree.assert_called_once()

    @patch(f"{_P}.setup_worktree")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    @patch(f"{_P}._check_github_connectivity")
    def test_returns_3_when_auth_fails(
        self,
        mock_connectivity: MagicMock,
        mock_prompt: MagicMock,
        mock_worktree: MagicMock,
    ):
        from kew.dispatch_core import GitHubAuthError, execute_dispatch

        mock_connectivity.side_effect = GitHubAuthError("GitHub authentication failed")
        config = DispatchConfig()
        rc = execute_dispatch(allow_unrecorded=True, issue=_make_issue(), config=config)
        assert rc.exit_code == 3
        mock_worktree.assert_not_called()


@pytest.mark.usefixtures("_canonical_existing_pr_published")
class TestPRExistsGuardOnRetry:
    """13. PR-exists check at top of retry loop prevents duplicate PRs.

    Scenario: attempt 0 fails (transient), but a PR was created before failure.
    On attempt 1, check_pr_exists returns True -> skip execute, return 0.
    """

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.time.sleep")
    @patch(f"{_P}.cleanup_worktree")
    @patch(f"{_P}.update_pr_labels")
    @patch(f"{_P}.mark_completed")
    @patch(f"{_P}.check_pr_exists")
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    @patch(f"{_P}.setup_worktree", return_value=Path("/tmp/wt"))
    @patch(f"{_P}.resolve_runner")
    def test_pr_exists_on_retry_skips_execute(
        self,
        mock_resolve_runner: MagicMock,
        mock_worktree: MagicMock,
        mock_prompt: MagicMock,
        mock_run_agent: MagicMock,
        mock_pr_exists: MagicMock,
        mock_completed: MagicMock,
        mock_pr_labels: MagicMock,
        mock_cleanup: MagicMock,
        mock_sleep: MagicMock,
        mock_connectivity: MagicMock,
    ):
        from kew.dispatch_core import execute_dispatch

        config = DispatchConfig(
            retry=RetryConfig(max_attempts=3, max_resume_attempts=2)
        )
        # First attempt fails (transient), then on retry the PR-exists guard fires.
        # check_pr_exists returns the PR number on attempt=1, causing early break.
        mock_resolve_runner.return_value.supports_resume.return_value = True
        mock_run_agent.side_effect = [
            _fail_result(error_category="transient", session_id="sess-1"),
        ]
        mock_pr_exists.return_value = 99

        rc = execute_dispatch(allow_unrecorded=True, issue=_make_issue(), config=config)

        assert rc.exit_code == 0
        # run_agent called only once (first attempt); retry was skipped
        assert mock_run_agent.call_count == 1
        mock_completed.assert_called_once_with(42, config)
        # update_pr_labels called in both the retry guard and the post-loop
        assert mock_pr_labels.call_count == 2
        mock_pr_labels.assert_called_with(
            99, config, add=[config.labels.pr_pending_review]
        )
        mock_cleanup.assert_called_once()


class TestExtractLabelNames:
    """Unit tests for extract_label_names helper."""

    def test_normal_labels(self):
        from kew.dispatch_core import extract_label_names

        issue = {"labels": [{"name": "bug"}, {"name": "ready"}]}
        assert extract_label_names(issue) == ["bug", "ready"]

    def test_empty_labels(self):
        from kew.dispatch_core import extract_label_names

        assert extract_label_names({"labels": []}) == []

    def test_missing_labels_key(self):
        from kew.dispatch_core import extract_label_names

        assert extract_label_names({}) == []

    def test_non_list_labels(self):
        from kew.dispatch_core import extract_label_names

        assert extract_label_names({"labels": "not-a-list"}) == []


class TestExceptionUpdatesStore:
    """Exception during dispatch records failure in store."""

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.mark_failed")
    @patch(f"{_P}.setup_worktree")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    def test_pre_execution_exception_no_store_record(
        self,
        mock_prompt: MagicMock,
        mock_worktree: MagicMock,
        mock_failed: MagicMock,
        mock_connectivity: MagicMock,
    ):
        """Exception before run_tracked_agent -- no RunRecord to update."""
        from kew.dispatch_core import execute_dispatch

        config = DispatchConfig()
        mock_worktree.side_effect = RuntimeError("boom")

        store = _mock_store()

        rc = execute_dispatch(
            allow_unrecorded=True, issue=_make_issue(), config=config, store=store
        )

        assert rc.exit_code == 1
        mock_failed.assert_called_once()
        # No RunRecord was created (exception before run_tracked_agent)
        store.save.assert_not_called()


@pytest.mark.usefixtures("_canonical_existing_pr_published")
class TestRetryBackoffJitter:
    """H2.12: Jitter applied to retry backoff to prevent thundering herd."""

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.random.uniform", return_value=1.1)
    @patch(f"{_P}.time.sleep")
    @patch(f"{_P}.cleanup_worktree")
    @patch(f"{_P}.update_pr_labels")
    @patch(f"{_P}.check_pr_exists")
    @patch(f"{_P}.mark_completed")
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    @patch(f"{_P}.setup_worktree", return_value=Path("/tmp/wt"))
    @patch(f"{_P}.resolve_runner")
    def test_backoff_sleep_includes_jitter(
        self,
        mock_resolve_runner: MagicMock,
        mock_worktree: MagicMock,
        mock_prompt: MagicMock,
        mock_run_agent: MagicMock,
        mock_completed: MagicMock,
        mock_pr_exists: MagicMock,
        mock_pr_labels: MagicMock,
        mock_cleanup: MagicMock,
        mock_sleep: MagicMock,
        mock_uniform: MagicMock,
        mock_connectivity: MagicMock,
    ):
        from kew.dispatch_core import execute_dispatch

        config = DispatchConfig(
            retry=RetryConfig(
                max_attempts=3,
                max_resume_attempts=2,
                backoff_base_seconds=10.0,
                backoff_multiplier=2.0,
            )
        )
        mock_resolve_runner.return_value.supports_resume.return_value = True
        mock_run_agent.side_effect = [
            _fail_result(error_category="transient", session_id="sess-1"),
            _ok_result(),
        ]
        # None: retry-loop guard (attempt=1) should not short-circuit
        # 99: post-loop success path (PR exists -> mark_completed)
        mock_pr_exists.side_effect = [None, 99]

        rc = execute_dispatch(allow_unrecorded=True, issue=_make_issue(), config=config)

        assert rc.exit_code == 0
        # attempt=0: wait = 10.0 * (2.0**0) * 1.1 = 10.0 * 1.0 * 1.1 = 11.0
        assert _backoff_sleeps(mock_sleep) == [11.0]
        mock_uniform.assert_called_once_with(0.75, 1.25)


@pytest.mark.usefixtures("_canonical_existing_pr_published")
class TestForceBudgetSkipsPreCheck:
    """force_budget=True bypasses the budget pre-check."""

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.cleanup_worktree")
    @patch(f"{_P}.check_pr_exists", return_value=99)
    @patch(f"{_P}.update_pr_labels")
    @patch(f"{_P}.mark_completed")
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    @patch(f"{_P}.setup_worktree", return_value=Path("/tmp/wt"))
    @patch(f"{_P}.check_budget")
    @patch(f"{_P}.resolve_runner")
    def test_force_budget_skips_check(
        self,
        mock_resolve_runner: MagicMock,
        mock_check_budget: MagicMock,
        mock_worktree: MagicMock,
        mock_prompt: MagicMock,
        mock_run_agent: MagicMock,
        mock_completed: MagicMock,
        mock_pr_labels: MagicMock,
        mock_pr_exists: MagicMock,
        mock_cleanup: MagicMock,
        mock_connectivity: MagicMock,
    ):
        from kew.dispatch_core import execute_dispatch

        config = DispatchConfig()
        store = _mock_store()
        mock_run_agent.return_value = _ok_result()

        rc = execute_dispatch(
            allow_unrecorded=True,
            issue=_make_issue(),
            config=config,
            store=store,
            force_budget=True,
        )

        assert rc.exit_code == 0
        mock_check_budget.assert_not_called()


@pytest.mark.usefixtures("_canonical_existing_pr_published")
class TestRoutingIntegration:
    """Tests that routing flows through dispatch_core correctly."""

    def test_simple_issue_routes_to_configured_runner(self):
        """An issue with 'simple' label routes to the simple runner."""
        from kew.config import DispatchConfig, RouteEntry, RoutingConfig
        from kew.runners.registry import resolve_runner

        config = DispatchConfig(
            routing=RoutingConfig(
                default=RouteEntry(runner="claude-code", model="claude-sonnet-4"),
                simple=RouteEntry(runner="opencode", model="ollama/deepseek-coder"),
            ),
        )
        route = resolve_runner(config, complexity="simple")
        assert route.runner.name == "opencode"
        assert route.model == "ollama/deepseek-coder"

    def test_complex_issue_routes_to_configured_runner(self):
        from kew.config import DispatchConfig, RouteEntry, RoutingConfig
        from kew.runners.registry import resolve_runner

        config = DispatchConfig(
            routing=RoutingConfig(
                default=RouteEntry(runner="claude-code", model="claude-sonnet-4"),
                complex=RouteEntry(runner="claude-code", model="claude-opus-4"),
            ),
        )
        route = resolve_runner(config, complexity="complex")
        assert route.runner.name == "claude-code"
        assert route.model == "claude-opus-4"

    def test_unlabeled_issue_falls_through_to_default(self):
        """Medium (default complexity) with no medium route uses default."""
        from kew.config import DispatchConfig, RouteEntry, RoutingConfig
        from kew.runners.registry import resolve_runner

        config = DispatchConfig(
            routing=RoutingConfig(
                default=RouteEntry(runner="claude-code", model="claude-sonnet-4"),
                simple=RouteEntry(runner="opencode", model="ollama/deepseek-coder"),
            ),
        )
        route = resolve_runner(config, complexity="medium")
        assert route.runner.name == "claude-code"
        assert route.model == "claude-sonnet-4"

    def test_no_routing_preserves_phase2_behavior(self):
        from kew.config import AgentConfig, DispatchConfig
        from kew.runners.registry import resolve_runner

        config = DispatchConfig(
            agent=AgentConfig(runner="claude-code", model="claude-sonnet-4")
        )
        route = resolve_runner(config)
        assert route.runner.name == "claude-code"
        assert route.model == "claude-sonnet-4"

    def test_route_model_used_for_cost_computation(self):
        """Verify the routed model (not config default) would be used for pricing."""
        from kew.config import AgentConfig, DispatchConfig, RouteEntry, RoutingConfig
        from kew.runners.registry import resolve_runner

        config = DispatchConfig(
            agent=AgentConfig(model="claude-sonnet-4"),
            routing=RoutingConfig(
                default=RouteEntry(runner="claude-code", model="claude-sonnet-4"),
                complex=RouteEntry(runner="claude-code", model="claude-opus-4"),
            ),
        )
        route = resolve_runner(config, complexity="complex")
        assert route.model == "claude-opus-4"
        assert route.model != config.agent.model

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.cleanup_worktree")
    @patch(f"{_P}.check_pr_exists", return_value=99)
    @patch(f"{_P}.update_pr_labels")
    @patch(f"{_P}.mark_completed")
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    @patch(f"{_P}.setup_worktree", return_value=Path("/tmp/wt"))
    def test_execute_dispatch_passes_routed_model_to_run_agent(
        self,
        mock_worktree: MagicMock,
        mock_prompt: MagicMock,
        mock_run_agent: MagicMock,
        mock_completed: MagicMock,
        mock_pr_labels: MagicMock,
        mock_pr_exists: MagicMock,
        mock_cleanup: MagicMock,
        mock_connectivity: MagicMock,
    ):
        """execute_dispatch threads the routed model into run_agent."""
        from kew.config import (
            AgentConfig,
            DispatchConfig,
            RouteEntry,
            RoutingConfig,
        )
        from kew.dispatch_core import execute_dispatch

        config = DispatchConfig(
            agent=AgentConfig(model="claude-sonnet-4"),
            routing=RoutingConfig(
                default=RouteEntry(runner="claude-code", model="claude-sonnet-4"),
                complex=RouteEntry(runner="claude-code", model="claude-opus-4"),
            ),
        )
        mock_run_agent.return_value = _ok_result()

        issue = _make_issue(labels=[{"name": "complex"}])
        rc = execute_dispatch(allow_unrecorded=True, issue=issue, config=config)

        assert rc.exit_code == 0
        call_kwargs = mock_run_agent.call_args.kwargs
        assert call_kwargs["model"] == "claude-opus-4"

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.cleanup_worktree")
    @patch(f"{_P}.check_pr_exists", return_value=99)
    @patch(f"{_P}.update_pr_labels")
    @patch(f"{_P}.mark_completed")
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    @patch(f"{_P}.setup_worktree", return_value=Path("/tmp/wt"))
    def test_run_record_includes_routed_model(
        self,
        mock_worktree: MagicMock,
        mock_prompt: MagicMock,
        mock_run_agent: MagicMock,
        mock_completed: MagicMock,
        mock_pr_labels: MagicMock,
        mock_pr_exists: MagicMock,
        mock_cleanup: MagicMock,
        mock_connectivity: MagicMock,
    ):
        """execute_dispatch saves a RunRecord with model=resolved.model."""
        from kew.config import (
            AgentConfig,
            DispatchConfig,
            RouteEntry,
            RoutingConfig,
        )
        from kew.dispatch_core import execute_dispatch

        config = DispatchConfig(
            agent=AgentConfig(model="claude-sonnet-4"),
            routing=RoutingConfig(
                default=RouteEntry(runner="claude-code", model="claude-sonnet-4"),
                complex=RouteEntry(runner="claude-code", model="claude-opus-4"),
            ),
        )
        mock_run_agent.return_value = _ok_result()
        store = _mock_store()
        store.get.return_value = MagicMock()

        issue = _make_issue(labels=[{"name": "complex"}])
        rc = execute_dispatch(
            allow_unrecorded=True, issue=issue, config=config, store=store
        )

        assert rc.exit_code == 0
        # The initial pending row is the atomic worker slot (#745) AND the
        # anchor for the immutable admission identity (I01), so it lands
        # through admit_run_with_identity rather than save/try_admit_run.
        admitted_record = store.admit_run_with_identity.call_args_list[0][0][0]
        assert admitted_record.model == "claude-opus-4"


@pytest.mark.usefixtures("_canonical_existing_pr_published")
class TestEmitEventsAndLogFilePath:
    """run_agent is called with emit_events=True and a stable log_file_path."""

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.cleanup_worktree")
    @patch(f"{_P}.check_pr_exists", return_value=99)
    @patch(f"{_P}.update_pr_labels")
    @patch(f"{_P}.mark_completed")
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    @patch(f"{_P}.setup_worktree", return_value=Path("/tmp/wt"))
    @patch(f"{_P}.resolve_runner")
    def test_emit_events_true(
        self,
        mock_resolve_runner: MagicMock,
        mock_worktree: MagicMock,
        mock_prompt: MagicMock,
        mock_run_agent: MagicMock,
        mock_completed: MagicMock,
        mock_pr_labels: MagicMock,
        mock_pr_exists: MagicMock,
        mock_cleanup: MagicMock,
        mock_connectivity: MagicMock,
        tmp_path,
    ):
        """run_agent is always called with emit_events=True."""
        from kew.config import DispatchConfig, LoggingConfig
        from kew.dispatch_core import execute_dispatch

        config = DispatchConfig(logging=LoggingConfig(dir=str(tmp_path)))
        mock_resolve_runner.return_value.supports_resume.return_value = True
        mock_run_agent.return_value = _ok_result()

        rc = execute_dispatch(allow_unrecorded=True, issue=_make_issue(), config=config)

        assert rc.exit_code == 0
        mock_run_agent.assert_called_once()
        call_kwargs = mock_run_agent.call_args[1]
        assert call_kwargs["emit_events"] is True

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.cleanup_worktree")
    @patch(f"{_P}.check_pr_exists", return_value=99)
    @patch(f"{_P}.update_pr_labels")
    @patch(f"{_P}.mark_completed")
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    @patch(f"{_P}.setup_worktree", return_value=Path("/tmp/wt"))
    @patch(f"{_P}.resolve_runner")
    def test_log_file_path_matches_item_id_pattern(
        self,
        mock_resolve_runner: MagicMock,
        mock_worktree: MagicMock,
        mock_prompt: MagicMock,
        mock_run_agent: MagicMock,
        mock_completed: MagicMock,
        mock_pr_labels: MagicMock,
        mock_pr_exists: MagicMock,
        mock_cleanup: MagicMock,
        mock_connectivity: MagicMock,
        tmp_path,
    ):
        """Log names use the durable item id, including non-GitHub ids."""
        import re

        from kew.config import DispatchConfig, LoggingConfig
        from kew.dispatch_core import execute_dispatch

        issue_num = 42
        config = DispatchConfig(logging=LoggingConfig(dir=str(tmp_path)))
        mock_resolve_runner.return_value.supports_resume.return_value = True
        mock_run_agent.return_value = _ok_result(issue_num=issue_num)

        rc = execute_dispatch(
            allow_unrecorded=True, issue=_make_issue(num=issue_num), config=config
        )

        assert rc.exit_code == 0
        mock_run_agent.assert_called_once()
        call_kwargs = mock_run_agent.call_args[1]
        log_file_path = call_kwargs["log_file_path"]
        assert isinstance(log_file_path, Path)
        assert re.match(rf"dispatch-{issue_num}-.+\.log", log_file_path.name)

        mock_run_agent.reset_mock()
        mock_run_agent.return_value = _ok_result(issue_num=0)
        rc = execute_dispatch(
            allow_unrecorded=True,
            issue=_make_issue(num=0),
            config=config,
            item_id="SFD-199",
            provider="linear",
        )

        assert rc.exit_code == 0
        linear_log = mock_run_agent.call_args.kwargs["log_file_path"]
        assert re.match(r"dispatch-SFD-199-.+\.log", linear_log.name)

        from kew.dispatch_core import _dispatch_log_item_id

        assert "/" not in _dispatch_log_item_id(0, "plan:../../escape")

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.time.sleep")
    @patch(f"{_P}.cleanup_worktree")
    @patch(f"{_P}.update_pr_labels")
    @patch(f"{_P}.check_pr_exists")
    @patch(f"{_P}.mark_completed")
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    @patch(f"{_P}.setup_worktree", return_value=Path("/tmp/wt"))
    @patch(f"{_P}.resolve_runner")
    def test_log_file_path_consistent_across_retries(
        self,
        mock_resolve_runner: MagicMock,
        mock_worktree: MagicMock,
        mock_prompt: MagicMock,
        mock_run_agent: MagicMock,
        mock_completed: MagicMock,
        mock_pr_exists: MagicMock,
        mock_pr_labels: MagicMock,
        mock_cleanup: MagicMock,
        mock_sleep: MagicMock,
        mock_connectivity: MagicMock,
        tmp_path,
    ):
        """The same log_file_path is passed on both the initial attempt and retry."""
        from kew.config import DispatchConfig, LoggingConfig
        from kew.dispatch_core import execute_dispatch

        config = DispatchConfig(
            logging=LoggingConfig(dir=str(tmp_path)),
            retry=RetryConfig(max_attempts=2),
        )
        mock_resolve_runner.return_value.supports_resume.return_value = True
        mock_run_agent.side_effect = [
            _fail_result(error_category="transient", session_id="sess-1"),
            _ok_result(),
        ]
        # None: retry-loop guard (attempt=1) should not short-circuit
        # 99: post-loop success path (PR exists -> mark_completed)
        mock_pr_exists.side_effect = [None, 99]

        rc = execute_dispatch(allow_unrecorded=True, issue=_make_issue(), config=config)

        assert rc.exit_code == 0
        assert mock_run_agent.call_count == 2
        first_path = mock_run_agent.call_args_list[0][1]["log_file_path"]
        second_path = mock_run_agent.call_args_list[1][1]["log_file_path"]
        assert first_path == second_path


class TestRunTrackedAgent:
    """Tests for run_tracked_agent -- the shared execution + tracking core."""

    @patch(f"{_P}.check_pr_exists", return_value=None)
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.resolve_runner")
    def test_creates_pending_record_and_updates_on_success(
        self,
        mock_resolve_runner: MagicMock,
        mock_run_agent: MagicMock,
        mock_pr_exists: MagicMock,
        tmp_path,
    ):
        """RunRecord created as pending, updated to success after agent succeeds."""
        from kew.dispatch_core import run_tracked_agent
        from kew.run_store import SqliteRunStore

        store = SqliteRunStore(str(tmp_path / "runs.db"))
        config = DispatchConfig()
        mock_resolve_runner.return_value.runner.name = "claude-code"
        mock_resolve_runner.return_value.model = "claude-sonnet-4"
        mock_run_agent.return_value = _ok_result()

        result = run_tracked_agent(
            allow_unrecorded=True,
            prompt="fix the bug",
            worktree_dir=tmp_path,
            config=config,
            issue_num=42,
            store=store,
            title="Fix the widget",
            branch="agent/issue-42",
        )

        assert result.exit_code == 0

        # Verify a record exists and was updated to success
        records = store.list_by_issue(42)
        assert len(records) == 1
        rec = records[0]
        assert rec.status == "success"
        assert rec.exit_code == 0
        assert rec.finished_at is not None
        assert rec.title == "Fix the widget"

    @patch(f"{_P}.check_pr_exists", return_value=None)
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.resolve_runner")
    def test_persists_item_id_on_run_record(
        self,
        mock_resolve_runner: MagicMock,
        mock_run_agent: MagicMock,
        mock_pr_exists: MagicMock,
        tmp_path,
    ):
        """A5a: run_tracked_agent threads item_id onto the RunRecord."""
        from kew.dispatch_core import run_tracked_agent
        from kew.run_store import SqliteRunStore

        store = SqliteRunStore(str(tmp_path / "runs.db"))
        config = DispatchConfig()
        mock_resolve_runner.return_value.runner.name = "claude-code"
        mock_resolve_runner.return_value.model = "claude-sonnet-4"
        mock_run_agent.return_value = _ok_result()

        run_tracked_agent(
            allow_unrecorded=True,
            prompt="fix the bug",
            worktree_dir=tmp_path,
            config=config,
            issue_num=42,
            item_id="42",
            store=store,
            title="Fix the widget",
            branch="agent/issue-42",
        )

        records = store.list_by_item("42")
        assert len(records) == 1
        assert records[0].item_id == "42"

    @patch(f"{_P}.check_pr_exists", return_value=None)
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.resolve_runner")
    def test_updates_record_on_failure(
        self,
        mock_resolve_runner: MagicMock,
        mock_run_agent: MagicMock,
        mock_pr_exists: MagicMock,
        tmp_path,
    ):
        """RunRecord updated to failure when agent fails."""
        from kew.dispatch_core import run_tracked_agent
        from kew.run_store import SqliteRunStore

        store = SqliteRunStore(str(tmp_path / "runs.db"))
        config = DispatchConfig(retry=RetryConfig(max_attempts=1))
        mock_resolve_runner.return_value.runner.name = "claude-code"
        mock_resolve_runner.return_value.model = "claude-sonnet-4"
        mock_run_agent.return_value = _fail_result(error_category="permanent")

        result = run_tracked_agent(
            allow_unrecorded=True,
            prompt="fix the bug",
            worktree_dir=tmp_path,
            config=config,
            issue_num=42,
            store=store,
            title="Fix the widget",
            branch="agent/issue-42",
        )

        assert result.exit_code == 1

        records = store.list_by_issue(42)
        assert len(records) == 1
        rec = records[0]
        assert rec.status == "failure"
        assert rec.exit_code == 1
        assert rec.finished_at is not None

    @patch(f"{_P}.check_pr_exists", return_value=None)
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.resolve_runner")
    def test_breaker_anomaly_maps_to_killed_and_emits_event(
        self,
        mock_resolve_runner: MagicMock,
        mock_run_agent: MagicMock,
        mock_pr_exists: MagicMock,
        tmp_path,
    ):
        """A cost_anomaly watchdog kill persists status='killed' and emits a
        circuit_breaker audit event (actor=policy)."""
        from kew.dispatch_core import run_tracked_agent
        from kew.run_store import SqliteRunStore
        from kew.tracking import DispatchResult

        store = SqliteRunStore(str(tmp_path / "runs.db"))
        config = DispatchConfig(retry=RetryConfig(max_attempts=1))
        mock_resolve_runner.return_value.runner.name = "claude-code"
        mock_resolve_runner.return_value.model = "claude-sonnet-4-6"

        breaker_result = DispatchResult(
            exit_code=1, issue_num=42, log_file=tmp_path / "l"
        )
        breaker_result.breaker_rule = "cost_anomaly"
        breaker_result.error_category = "circuit_breaker"
        breaker_result.subtype = "cost_anomaly"
        breaker_result.estimated_cost_usd = 12.0
        mock_run_agent.return_value = breaker_result

        run_tracked_agent(
            allow_unrecorded=True,
            prompt="fix the bug",
            worktree_dir=tmp_path,
            config=config,
            issue_num=42,
            store=store,
            title="Runaway",
            branch="agent/issue-42",
            model="claude-sonnet-4-6",
            complexity="medium",
        )

        records = store.list_by_issue(42)
        assert records and records[-1].status == "killed"
        events = store.list_audit_events(event="circuit_breaker")
        assert len(events) == 1
        assert events[0].actor_type == "policy"
        assert events[0].payload["rule"] == "cost_anomaly"
        assert events[0].payload["scope"] == "run"

    @patch(f"{_P}.check_pr_exists", return_value=None)
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.resolve_runner")
    def test_works_without_store(
        self,
        mock_resolve_runner: MagicMock,
        mock_run_agent: MagicMock,
        mock_pr_exists: MagicMock,
        tmp_path,
    ):
        """run_tracked_agent works fine when store=None (no RunRecord)."""
        from kew.dispatch_core import run_tracked_agent

        config = DispatchConfig()
        mock_run_agent.return_value = _ok_result()

        result = run_tracked_agent(
            allow_unrecorded=True,
            prompt="fix the bug",
            worktree_dir=tmp_path,
            config=config,
            issue_num=42,
        )

        assert result.exit_code == 0
        mock_run_agent.assert_called_once()

    @patch(f"{_P}.check_pr_exists", return_value=None)
    @patch(f"{_P}.run_agent")
    def test_transport_is_preflighted_once_persisted_and_passed_to_runner(
        self,
        mock_run_agent: MagicMock,
        mock_pr_exists: MagicMock,
        tmp_path: Path,
    ) -> None:
        from kew.dispatch_core import run_tracked_agent
        from kew.run_store import SqliteRunStore

        runner = MagicMock()
        runner.name = "transport-runner"
        runner.transport_capability = RunnerTransportCapability(
            incoming_api_formats=("openai-responses",),
            endpoint_override=True,
            model_override=True,
        )
        runner.supports_resume.return_value = False
        config = DispatchConfig(
            retry=RetryConfig(max_attempts=1),
            inference_transport=InferenceTransportConfig(
                mode="switchyard",
                switchyard=SwitchyardConfig(
                    endpoint="https://switchyard.example",
                    route="exact-route",
                    expected_version="pinned",
                ),
            ),
        )
        preflight = MagicMock(
            return_value=SwitchyardProbeResult(
                admitted=True,
                reason="ready",
                endpoint="https://switchyard.example",
                route="exact-route",
                observed_version="pinned",
            )
        )
        mock_run_agent.return_value = _ok_result()
        store = SqliteRunStore(str(tmp_path / "runs.db"))

        run_tracked_agent(
            allow_unrecorded=True,
            prompt="fix",
            worktree_dir=tmp_path,
            config=config,
            issue_num=42,
            store=store,
            runner=runner,
            model="original-model",
            transport_preflight=preflight,
        )

        preflight.assert_called_once()
        admitted = mock_run_agent.call_args.kwargs["inference_transport"]
        assert admitted.model == "exact-route"
        assert mock_run_agent.call_args.kwargs["model"] == "exact-route"
        record = store.list_by_issue(42)[0]
        assert record.transport_mode == "switchyard"
        assert record.transport_endpoint == "https://switchyard.example"
        assert record.transport_route == "exact-route"
        assert record.transport_observed_version == "pinned"
        assert record.transport_preflight_verdict == "ready"
        event = store.list_audit_events(event="dispatch_completed")[0]
        assert event.payload["transport_route"] == "exact-route"
        assert event.payload["transport_preflight_verdict"] == "ready"

    @patch(f"{_P}.check_pr_exists", return_value=None)
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.probe_switchyard_endpoint")
    def test_external_transport_uses_production_preflight(
        self,
        probe: MagicMock,
        mock_run_agent: MagicMock,
        mock_pr_exists: MagicMock,
        tmp_path: Path,
    ) -> None:
        from kew.dispatch_core import run_tracked_agent

        runner = MagicMock()
        runner.name = "transport-runner"
        runner.transport_capability = RunnerTransportCapability(
            incoming_api_formats=("openai-responses",),
            endpoint_override=True,
            model_override=True,
        )
        runner.supports_resume.return_value = False
        config = DispatchConfig(
            retry=RetryConfig(max_attempts=1),
            inference_transport={
                "mode": "switchyard",
                "switchyard": {
                    "endpoint": "https://switchyard.example",
                    "route": "exact-route",
                    "expected_version": "pinned",
                },
            },
        )
        probe.return_value = SwitchyardProbeResult(
            admitted=True,
            reason="ready",
            endpoint="https://switchyard.example",
            route="exact-route",
        )
        mock_run_agent.return_value = _ok_result()

        run_tracked_agent(
            allow_unrecorded=True,
            prompt="fix",
            worktree_dir=tmp_path,
            config=config,
            issue_num=42,
            runner=runner,
            model="original-model",
        )

        probe.assert_called_once_with(
            endpoint="https://switchyard.example",
            route="exact-route",
            timeout_seconds=20.0,
        )

    @patch(f"{_P}.check_pr_exists", return_value=None)
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.managed_switchyard_context")
    def test_managed_transport_persists_supervisor_evidence(
        self,
        managed_context: MagicMock,
        mock_run_agent: MagicMock,
        mock_pr_exists: MagicMock,
        tmp_path: Path,
    ) -> None:
        from kew.dispatch_core import run_tracked_agent
        from kew.run_store import SqliteRunStore

        binary = tmp_path / "switchyard-server"
        binary.write_text("binary")
        route_config = tmp_path / "routes.toml"
        route_config.write_text("schema_version = 1")
        runner = MagicMock()
        runner.name = "transport-runner"
        runner.transport_capability = RunnerTransportCapability(
            incoming_api_formats=("openai-responses",),
            endpoint_override=True,
            model_override=True,
        )
        runner.supports_resume.return_value = False
        config = DispatchConfig(
            retry=RetryConfig(
                max_attempts=2,
                max_resume_attempts=0,
                backoff_base_seconds=0,
            ),
            inference_transport={
                "mode": "switchyard",
                "switchyard": {
                    "endpoint": "http://127.0.0.1:4000",
                    "route": "exact-route",
                    "expected_version": "0.1.0",
                    "managed": True,
                    "config_path": route_config,
                    "binary": binary,
                    "binary_sha256": "a" * 64,
                },
            },
        )

        supervisor = MagicMock()
        supervisor.observed_version = "0.1.0"
        supervisor.config_sha256 = "b" * 64
        managed_context.return_value.__enter__.return_value = supervisor

        launches = 0

        def launch(**kwargs):
            nonlocal launches
            launches += 1
            assert kwargs["managed_switchyard"] is supervisor
            if launches == 1:
                return _ok_result(exit_code=1, error_category="transient")
            return _ok_result()

        mock_run_agent.side_effect = launch
        store = SqliteRunStore(str(tmp_path / "runs.db"))

        run_tracked_agent(
            allow_unrecorded=True,
            prompt="fix",
            worktree_dir=tmp_path,
            config=config,
            issue_num=42,
            store=store,
            runner=runner,
            model="original-model",
        )

        record = store.list_by_issue(42)[0]
        assert record.transport_observed_version == "0.1.0"
        assert record.transport_config_sha256 == "b" * 64
        assert record.transport_preflight_verdict == "ready"
        event = store.list_audit_events(event="dispatch_completed")[0]
        assert event.payload["transport_observed_version"] == "0.1.0"
        assert event.payload["transport_config_sha256"] == "b" * 64
        assert event.payload["transport_preflight_verdict"] == "ready"
        assert launches == 2
        managed_context.return_value.__enter__.assert_called_once()
        managed_context.return_value.__exit__.assert_called_once()


class TestExecuteDispatchModelOverride:
    """execute_dispatch uses model_override when provided instead of resolved model."""

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.cleanup_worktree")
    @patch(f"{_P}.check_pr_exists", return_value=99)
    @patch(f"{_P}.update_pr_labels")
    @patch(f"{_P}.mark_completed")
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    @patch(f"{_P}.setup_worktree", return_value=Path("/tmp/wt"))
    @patch(f"{_P}.resolve_runner")
    def test_model_override_passed_to_run_tracked_agent(
        self,
        mock_resolve_runner: MagicMock,
        mock_worktree: MagicMock,
        mock_prompt: MagicMock,
        mock_run_agent: MagicMock,
        mock_completed: MagicMock,
        mock_pr_labels: MagicMock,
        mock_pr_exists: MagicMock,
        mock_cleanup: MagicMock,
        mock_connectivity: MagicMock,
    ):
        from kew.dispatch_core import execute_dispatch

        config = DispatchConfig()
        mock_resolve_runner.return_value.model = "claude-sonnet-4-6"
        mock_run_agent.return_value = _ok_result()

        execute_dispatch(
            allow_unrecorded=True,
            issue=_make_issue(),
            config=config,
            model_override="claude-opus-4-6",
        )

        # run_agent must be called with the override model, not the resolved model
        call_kwargs = mock_run_agent.call_args[1]
        assert call_kwargs["model"] == "claude-opus-4-6"


class TestDispatchAuditEvents:
    """dispatch_completed audit events replace dispatch-log.jsonl (#246).

    Parity: every field the legacy JSONL entry carried lives in the new
    event's payload; the envelope (actor/repo/run_id) is new.
    """

    _LEGACY_PAYLOAD_FIELDS = (
        "complexity",
        "runner",
        "model",
        "exit_code",
        "duration_seconds",
        "cost_usd",
        "tokens",
        "attempt_number",
        "error_category",
    )

    def _store(self, tmp_path):
        from kew.run_store import SqliteRunStore

        return SqliteRunStore(str(tmp_path / "t.db"))

    def _config(self, tmp_path):
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        config.project.repo = "octo/widgets"
        return config

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.cleanup_worktree")
    @patch(f"{_P}.check_pr_exists", return_value=99)
    @patch(f"{_P}.update_pr_labels", return_value=True)
    @patch(f"{_P}.mark_completed")
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    @patch(f"{_P}.setup_worktree", return_value=Path("/tmp/wt"))
    @patch(f"{_P}.resolve_runner")
    def test_success_emits_dispatch_completed_with_envelope(
        self,
        mock_resolve_runner: MagicMock,
        mock_worktree: MagicMock,
        mock_prompt: MagicMock,
        mock_run_agent: MagicMock,
        mock_completed: MagicMock,
        mock_pr_labels: MagicMock,
        mock_pr_exists: MagicMock,
        mock_cleanup: MagicMock,
        mock_connectivity: MagicMock,
        tmp_path: Path,
        monkeypatch,
    ):
        from kew.dispatch_core import execute_dispatch

        monkeypatch.setenv("KEW_ACTOR", "alice")
        provenance = {
            "orchestrator_version": "0.123.1",
            "orchestrator_commit": "abc123",
            "orchestrator_dirty": True,
        }
        config = self._config(tmp_path)
        mock_resolve_runner.return_value.runner.name = "claude-code"
        mock_resolve_runner.return_value.model = "claude-sonnet-4-6"
        mock_run_agent.return_value = _ok_result(
            exit_code=0, total_tokens=500, input_tokens=400, output_tokens=100
        )
        mock_run_agent.return_value.estimated_cost_usd = 0.05

        with (
            patch(f"{_P}._orchestrator_provenance", return_value=provenance),
            self._store(tmp_path) as store,
        ):
            execute_dispatch(
                allow_unrecorded=True,
                issue=_make_issue(labels=[{"name": "ready"}, {"name": "medium"}]),
                config=config,
                store=store,
            )
            events = store.list_audit_events(event="dispatch_completed")
            assert len(events) == 1
            ev = events[0]
            # Envelope — all new vs the legacy JSONL
            assert ev.run_id is not None  # run_id now present (#246)
            assert ev.issue_num == 42
            assert ev.repo == "octo/widgets"
            assert ev.actor_type == "human"
            assert ev.actor_id == "alice"
            assert ev.endpoint is not None
            # Payload parity with the legacy entry
            for field in self._LEGACY_PAYLOAD_FIELDS:
                assert field in ev.payload, f"legacy field {field} missing"
            assert ev.payload["complexity"] == "medium"
            assert ev.payload["exit_code"] == 0
            assert ev.payload["tokens"] == 500
            from kew.pricing import estimate_cost

            assert ev.payload["cost_usd"] == pytest.approx(
                estimate_cost("claude-sonnet-4-6", input_tokens=400, output_tokens=100)
            )
            assert ev.payload["attempt_number"] == 1
            assert ev.payload["error_category"] == ""
            assert ev.payload["orchestrator_version"] == "0.123.1"
            assert ev.payload["orchestrator_commit"] == "abc123"
            assert ev.payload["orchestrator_dirty"] is True
            # run row carries the same envelope (acceptance #1)
            record = store.get(ev.run_id)
            assert record is not None
            assert record.actor_id == "alice"
            assert record.repo == "octo/widgets"
            assert record.endpoint == ev.endpoint
        # The legacy file is no longer written
        assert not (tmp_path / "logs" / "dispatch-log.jsonl").exists()

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.cleanup_worktree")
    @patch(f"{_P}.check_pr_exists", return_value=99)
    @patch(f"{_P}.update_pr_labels", return_value=True)
    @patch(f"{_P}.mark_completed")
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    @patch(f"{_P}.setup_worktree", return_value=Path("/tmp/wt"))
    @patch(f"{_P}.resolve_runner")
    def test_reported_model_persisted_and_chained(
        self,
        mock_resolve_runner: MagicMock,
        mock_worktree: MagicMock,
        mock_prompt: MagicMock,
        mock_run_agent: MagicMock,
        mock_completed: MagicMock,
        mock_pr_labels: MagicMock,
        mock_pr_exists: MagicMock,
        mock_cleanup: MagicMock,
        mock_connectivity: MagicMock,
        tmp_path: Path,
        monkeypatch,
    ):
        from kew.dispatch_core import execute_dispatch

        monkeypatch.setenv("KEW_ACTOR", "alice")
        mock_resolve_runner.return_value.runner.name = "claude-code"
        mock_resolve_runner.return_value.model = "claude-sonnet-4-6"
        mock_run_agent.return_value = _ok_result(exit_code=0, total_tokens=10)
        mock_run_agent.return_value.resolved_model = "anthropic/claude-sonnet-4-6"

        with self._store(tmp_path) as store:
            execute_dispatch(
                allow_unrecorded=True,
                issue=_make_issue(labels=[{"name": "ready"}, {"name": "medium"}]),
                config=self._config(tmp_path),
                store=store,
            )
            events = store.list_audit_events(event="dispatch_completed")
            assert events[0].payload["reported_model"] == "anthropic/claude-sonnet-4-6"
            record = store.get(events[0].run_id)
            assert record.reported_model == "anthropic/claude-sonnet-4-6"

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.mark_failed")
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    @patch(f"{_P}.setup_worktree", return_value=Path("/tmp/wt"))
    @patch(f"{_P}.resolve_runner")
    def test_failure_emits_event_with_error_category(
        self,
        mock_resolve_runner: MagicMock,
        mock_worktree: MagicMock,
        mock_prompt: MagicMock,
        mock_run_agent: MagicMock,
        mock_failed: MagicMock,
        mock_connectivity: MagicMock,
        tmp_path: Path,
    ):
        from kew.dispatch_core import execute_dispatch

        config = self._config(tmp_path)
        config.retry.max_attempts = 1
        mock_resolve_runner.return_value.runner.name = "claude-code"
        mock_resolve_runner.return_value.model = "claude-sonnet-4-6"
        mock_run_agent.return_value = _ok_result(exit_code=1)
        mock_run_agent.return_value.error_category = "permanent"

        with self._store(tmp_path) as store:
            execute_dispatch(
                allow_unrecorded=True, issue=_make_issue(), config=config, store=store
            )
            ev = store.list_audit_events(event="dispatch_completed")[0]
            assert ev.payload["exit_code"] == 1
            assert ev.payload["error_category"] == "permanent"
            assert ev.run_id is not None

    @patch(f"{_P}.mark_failed")
    @patch(f"{_P}.check_budget")
    def test_budget_precheck_failure_emits_early_exit_event(
        self,
        mock_check_budget: MagicMock,
        mock_failed: MagicMock,
        tmp_path: Path,
    ):
        from kew.dispatch_core import execute_dispatch

        status = BudgetStatus(
            issue_spent_usd=20.0,
            issue_limit_usd=15.0,
            daily_spent_usd=0,
            daily_limit_usd=50.0,
            weekly_spent_usd=0,
            weekly_limit_usd=200.0,
        )
        mock_check_budget.side_effect = BudgetExceeded("per_issue", 15.0, 20.0, status)

        config = self._config(tmp_path)
        with self._store(tmp_path) as store:
            execute_dispatch(
                allow_unrecorded=True, issue=_make_issue(), config=config, store=store
            )
            ev = store.list_audit_events(event="dispatch_completed")[0]
            assert ev.payload["error_category"] == "budget_exceeded"
            assert ev.payload["attempt_number"] == 0
            assert ev.run_id is None  # no run was created
            assert ev.actor_type == "human"
            assert ev.repo == "octo/widgets"
        assert not (tmp_path / "logs" / "dispatch-log.jsonl").exists()

    @patch(f"{_P}._check_github_connectivity", return_value=False)
    def test_github_unreachable_emits_early_exit_event(
        self,
        mock_connectivity: MagicMock,
        tmp_path: Path,
    ):
        from kew.dispatch_core import execute_dispatch

        config = self._config(tmp_path)
        with self._store(tmp_path) as store:
            execute_dispatch(
                allow_unrecorded=True, issue=_make_issue(), config=config, store=store
            )
            ev = store.list_audit_events(event="dispatch_completed")[0]
            assert ev.run_id is None
            assert ev.payload["error_category"] == "github_unreachable"
            assert ev.payload["exit_code"] == 3

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.mark_failed")
    @patch(f"{_P}.setup_worktree", side_effect=RuntimeError("boom"))
    @patch(f"{_P}.resolve_runner")
    def test_dispatch_exception_emits_early_exit_event(
        self,
        mock_resolve_runner: MagicMock,
        mock_worktree: MagicMock,
        mock_failed: MagicMock,
        mock_connectivity: MagicMock,
        tmp_path: Path,
    ):
        from kew.dispatch_core import execute_dispatch

        config = self._config(tmp_path)
        mock_resolve_runner.return_value.runner.name = "claude-code"
        mock_resolve_runner.return_value.model = "claude-sonnet-4-6"
        with self._store(tmp_path) as store:
            execute_dispatch(
                allow_unrecorded=True, issue=_make_issue(), config=config, store=store
            )
            ev = store.list_audit_events(event="dispatch_completed")[0]
            assert ev.payload["error_category"] == "exception"
            assert ev.payload["runner"] == "claude-code"
            posture = store.list_audit_events(event="sandbox_resolved")[0]
            assert posture.run_id is not None
            assert posture.payload["posture"] == "uncontained"

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.mark_failed")
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    @patch(f"{_P}.setup_worktree", return_value=Path("/tmp/wt"))
    @patch(f"{_P}.resolve_runner")
    def test_no_store_drops_event_without_crashing(
        self,
        mock_resolve_runner: MagicMock,
        mock_worktree: MagicMock,
        mock_prompt: MagicMock,
        mock_run_agent: MagicMock,
        mock_failed: MagicMock,
        mock_connectivity: MagicMock,
        tmp_path: Path,
    ):
        """store=None: audit degrades to a warning; dispatch outcome unaffected."""
        from kew.dispatch_core import execute_dispatch

        config = self._config(tmp_path)
        config.retry.max_attempts = 1
        mock_resolve_runner.return_value.runner.name = "claude-code"
        mock_resolve_runner.return_value.model = "claude-sonnet-4-6"
        mock_run_agent.return_value = _ok_result(exit_code=1)
        mock_run_agent.return_value.error_category = "permanent"

        outcome = execute_dispatch(
            allow_unrecorded=True, issue=_make_issue(), config=config, store=None
        )
        assert outcome.exit_code == 1
        assert not (tmp_path / "logs" / "dispatch-log.jsonl").exists()

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.mark_failed")
    @patch(f"{_P}.run_agent", side_effect=RuntimeError("agent blew up"))
    @patch(f"{_P}.build_prompt", return_value="prompt")
    @patch(f"{_P}.setup_worktree", return_value=Path("/tmp/wt"))
    @patch(f"{_P}.resolve_runner")
    def test_run_agent_exception_emits_event_with_run_id(
        self,
        mock_resolve_runner: MagicMock,
        mock_worktree: MagicMock,
        mock_prompt: MagicMock,
        mock_run_agent: MagicMock,
        mock_failed: MagicMock,
        mock_connectivity: MagicMock,
        tmp_path: Path,
    ):
        """run_tracked_agent's internal exception path emits a terminal row."""
        from kew.dispatch_core import execute_dispatch

        config = self._config(tmp_path)
        config.retry.max_attempts = 1
        mock_resolve_runner.return_value.runner.name = "claude-code"
        mock_resolve_runner.return_value.model = "claude-sonnet-4-6"

        with self._store(tmp_path) as store:
            execute_dispatch(
                allow_unrecorded=True, issue=_make_issue(), config=config, store=store
            )
            events = store.list_audit_events(event="dispatch_completed")
            assert len(events) == 1
            ev = events[0]
            assert ev.run_id is not None  # a run row exists for this failure
            assert ev.payload["error_category"] == "exception"
            assert ev.payload["exit_code"] == 1
            record = store.get(ev.run_id)
            assert record is not None
            assert record.status == "failure"

    @patch(f"{_P}._check_github_connectivity")
    def test_github_auth_error_emits_early_exit_event(
        self,
        mock_connectivity: MagicMock,
        tmp_path: Path,
    ):
        from kew.dispatch_core import GitHubAuthError, execute_dispatch

        mock_connectivity.side_effect = GitHubAuthError("bad token")
        config = self._config(tmp_path)
        with self._store(tmp_path) as store:
            outcome = execute_dispatch(
                allow_unrecorded=True, issue=_make_issue(), config=config, store=store
            )
            assert outcome.exit_code == 3
            ev = store.list_audit_events(event="dispatch_completed")[0]
            assert ev.payload["error_category"] == "github_auth"
            assert ev.payload["exit_code"] == 3
            assert ev.run_id is None  # no run was created


class TestMergeContextPassthrough:
    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.resolve_runner")
    @patch(f"{_P}.setup_worktree", return_value=Path("/tmp/wt"))
    @patch(f"{_P}.build_prompt")
    @patch(f"{_P}.run_tracked_agent")
    @patch(f"{_P}.check_pr_exists", return_value=99)
    @patch(f"{_P}.mark_completed")
    @patch(f"{_P}.update_pr_labels", return_value=True)
    @patch(f"{_P}.cleanup_worktree")
    @patch(f"{_P}.run")
    def test_merge_context_passed_to_build_prompt(
        self,
        mock_run,
        mock_cleanup,
        mock_update_pr,
        mock_completed,
        mock_check_pr,
        mock_tracked,
        mock_prompt,
        mock_worktree,
        mock_runner,
        mock_gh,
    ):
        from kew.dispatch_core import execute_dispatch
        from kew.runners.claude_code import ClaudeCodeRunner

        mock_runner.return_value = MagicMock(
            runner=ClaudeCodeRunner(), model="claude-sonnet-4-6"
        )
        mock_tracked.return_value = _ok_result()
        mock_prompt.return_value = "prompt"
        mock_run.return_value = MagicMock(returncode=0, stdout="")

        merge_ctx = "## Recently Merged\n\nPR #10 modified scheduler.py"
        execute_dispatch(
            allow_unrecorded=True,
            issue=_make_issue(),
            config=DispatchConfig(),
            merge_context=merge_ctx,
        )

        mock_prompt.assert_called_once()
        call_kwargs = mock_prompt.call_args.kwargs
        assert call_kwargs["recently_merged_context"] == merge_ctx


class TestGetComplexity:
    """get_complexity extracts complexity from issue labels."""

    def test_get_complexity_label_mapping(self):
        from kew.selector import get_complexity

        labels = [{"name": "complex"}]
        assert get_complexity(labels) == "complex"

        labels_simple = [{"name": "simple"}]
        assert get_complexity(labels_simple) == "simple"

        labels_none = [{"name": "bug"}]
        assert get_complexity(labels_none) == "medium"  # default


@pytest.mark.usefixtures("_canonical_existing_pr_published")
class TestAutoUnblockOnCompletion:
    """After mark_completed, dependents with all deps resolved are unblocked."""

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.cleanup_worktree")
    @patch(f"{_P}.check_pr_exists", return_value=99)
    @patch(f"{_P}.update_pr_labels")
    @patch(f"{_P}.mark_completed")
    @patch(f"{_P}.unblock_issue")
    @patch(f"{_P}._fetch_issue_labels", return_value=set())
    @patch(f"{_P}.find_blocked_dependents", return_value=[72, 73])
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    @patch(f"{_P}.setup_worktree", return_value=Path("/tmp/wt"))
    @patch(f"{_P}.resolve_runner")
    def test_unblocks_dependents_after_success(
        self,
        mock_resolve_runner: MagicMock,
        mock_worktree: MagicMock,
        mock_prompt: MagicMock,
        mock_run_agent: MagicMock,
        mock_find: MagicMock,
        mock_fetch_labels: MagicMock,
        mock_unblock: MagicMock,
        mock_completed: MagicMock,
        mock_pr_labels: MagicMock,
        mock_pr_exists: MagicMock,
        mock_cleanup: MagicMock,
        mock_connectivity: MagicMock,
    ):
        from kew.dispatch_core import execute_dispatch

        config = DispatchConfig()
        mock_run_agent.return_value = _ok_result()

        rc = execute_dispatch(allow_unrecorded=True, issue=_make_issue(), config=config)

        assert rc.exit_code == 0
        mock_completed.assert_called_once()
        mock_find.assert_called_once_with(42, config)
        assert mock_unblock.call_count == 2
        mock_unblock.assert_any_call(72, config)
        mock_unblock.assert_any_call(73, config)

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.cleanup_worktree")
    @patch(f"{_P}.check_pr_exists", return_value=99)
    @patch(f"{_P}.update_pr_labels")
    @patch(f"{_P}.mark_completed")
    @patch(f"{_P}.unblock_issue")
    @patch(f"{_P}._fetch_issue_labels", return_value=set())
    @patch(f"{_P}.find_blocked_dependents", return_value=[])
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    @patch(f"{_P}.setup_worktree", return_value=Path("/tmp/wt"))
    @patch(f"{_P}.resolve_runner")
    def test_no_dependents_to_unblock(
        self,
        mock_resolve_runner: MagicMock,
        mock_worktree: MagicMock,
        mock_prompt: MagicMock,
        mock_run_agent: MagicMock,
        mock_find: MagicMock,
        mock_fetch_labels: MagicMock,
        mock_unblock: MagicMock,
        mock_completed: MagicMock,
        mock_pr_labels: MagicMock,
        mock_pr_exists: MagicMock,
        mock_cleanup: MagicMock,
        mock_connectivity: MagicMock,
    ):
        from kew.dispatch_core import execute_dispatch

        config = DispatchConfig()
        mock_run_agent.return_value = _ok_result()

        rc = execute_dispatch(allow_unrecorded=True, issue=_make_issue(), config=config)

        assert rc.exit_code == 0
        mock_find.assert_called_once()
        mock_unblock.assert_not_called()

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.cleanup_worktree")
    @patch(f"{_P}.check_pr_exists", return_value=99)
    @patch(f"{_P}.update_pr_labels")
    @patch(f"{_P}.mark_completed")
    @patch(f"{_P}.unblock_issue", side_effect=RuntimeError("gh fail"))
    @patch(f"{_P}._fetch_issue_labels", return_value=set())
    @patch(f"{_P}.find_blocked_dependents", return_value=[72])
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    @patch(f"{_P}.setup_worktree", return_value=Path("/tmp/wt"))
    @patch(f"{_P}.resolve_runner")
    def test_unblock_failure_does_not_break_dispatch(
        self,
        mock_resolve_runner: MagicMock,
        mock_worktree: MagicMock,
        mock_prompt: MagicMock,
        mock_run_agent: MagicMock,
        mock_find: MagicMock,
        mock_fetch_labels: MagicMock,
        mock_unblock: MagicMock,
        mock_completed: MagicMock,
        mock_pr_labels: MagicMock,
        mock_pr_exists: MagicMock,
        mock_cleanup: MagicMock,
        mock_connectivity: MagicMock,
    ):
        from kew.dispatch_core import execute_dispatch

        config = DispatchConfig()
        mock_run_agent.return_value = _ok_result()

        rc = execute_dispatch(allow_unrecorded=True, issue=_make_issue(), config=config)

        # Dispatch still succeeds even if unblock fails
        assert rc.exit_code == 0
        mock_completed.assert_called_once()
        mock_unblock.assert_called_once_with(72, config)


class TestPgidRecording:
    """The dispatch owns liveness; attempt groups remain available for kills."""

    @pytest.fixture(autouse=True)
    def registered_birth(self, monkeypatch):
        """The mock runner supplies a process birth for its synthetic PIDs."""

        def birth(pid):
            return "Tue Sep 15 10:00:00 2026"

        monkeypatch.setattr("kew.run_store.process_start_time", birth)
        monkeypatch.setattr("kew.process_groups.process_start_time", birth)
        monkeypatch.setattr("kew.run_store.attempt_descends_from", lambda *a: True)

    def test_retry_backoff_keeps_worker_cap_full(self, tmp_path):
        from kew.dispatch_core import _worker_cap_reached
        from kew.run_store import RunRecord, SqliteRunStore

        config = DispatchConfig()
        config.parallel.max_workers = 1
        with SqliteRunStore(str(tmp_path / "retry.db")) as store:
            store.save(
                RunRecord(
                    run_id="retry",
                    issue_num=42,
                    branch="agent/issue-42",
                    status="pending",
                    exit_code=-1,
                    started_at=datetime.now(UTC),
                    pgid=os.getpgrp(),
                )
            )
            store.set_attempt_pgid("retry", 2**31 - 1)
            assert _worker_cap_reached(store, config, 43) == (True, 1, 1)

    @patch("kew.dispatch_core.run_agent")
    def test_owner_pgid_is_saved_before_attempt_spawn(
        self, mock_run_agent, tmp_path, process_table
    ):
        from kew.dispatch_core import run_tracked_agent
        from kew.run_store import SqliteRunStore

        # Attempt registration authenticates the attempt leader's ancestry
        # against the host snapshot (#1936 round 13), so the fake attempt pid
        # must appear as a live descendant of this dispatch process.
        owner = os.getpid()
        process_table.members[owner] = (owner, "S", "python3", "python3 -m pytest")
        process_table.parents[owner] = 1
        process_table.members[12345] = (12345, "S", "claude", "claude -p")
        process_table.parents[12345] = owner

        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        config.retry.max_attempts = 1
        snapshots = []
        with SqliteRunStore(str(tmp_path / "owner.db")) as store:

            def spawn(**kwargs):
                snapshots.append(store.list_pending()[0].pgid)
                kwargs["on_spawn"](12345)
                snapshots.append(store.list_pending()[0].pgid)
                return DispatchResult(
                    exit_code=0, issue_num=42, log_file=tmp_path / "x.log"
                )

            mock_run_agent.side_effect = spawn
            runner = MagicMock()
            runner.name = "fake"
            runner.supports_resume.return_value = False
            run_tracked_agent(
                allow_unrecorded=True,
                prompt="p",
                worktree_dir=tmp_path,
                config=config,
                issue_num=42,
                store=store,
                branch="agent/issue-42",
                model="claude-sonnet-4-6",
                runner=runner,
            )
        assert snapshots == [os.getpgrp(), os.getpgrp()]

    @patch("kew.dispatch_core.run_agent")
    def test_on_spawn_writes_pgid_to_store(self, mock_run_agent, tmp_path):
        from kew.dispatch_core import run_tracked_agent
        from kew.run_store import SqliteRunStore
        from kew.tracking import DispatchResult

        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        config.retry.max_attempts = 1

        def _fake_run_agent(**kwargs):
            on_spawn = kwargs.get("on_spawn")
            assert on_spawn is not None, "on_spawn must be passed when store is set"
            on_spawn(55555)
            return DispatchResult(
                exit_code=0, issue_num=42, log_file=tmp_path / "x.log"
            )

        mock_run_agent.side_effect = _fake_run_agent
        mock_runner = MagicMock()
        mock_runner.name = "fake"
        mock_runner.supports_resume.return_value = False

        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            run_tracked_agent(
                allow_unrecorded=True,
                prompt="p",
                worktree_dir=tmp_path,
                config=config,
                issue_num=42,
                store=store,
                branch="agent/issue-42",
                model="claude-sonnet-4-6",
                runner=mock_runner,
            )
            records = store.list_by_issue(42)
            assert len(records) == 1
            assert records[0].pgid == os.getpgrp()
            assert records[0].attempt_pgid == 55555

    @patch("kew.dispatch_core.run_agent")
    def test_no_store_still_tracks_spawn(self, mock_run_agent, tmp_path):
        from kew.dispatch_core import run_tracked_agent
        from kew.tracking import DispatchResult

        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        config.retry.max_attempts = 1
        mock_run_agent.return_value = DispatchResult(
            exit_code=0, issue_num=42, log_file=tmp_path / "x.log"
        )
        mock_runner = MagicMock()
        mock_runner.name = "fake"
        mock_runner.supports_resume.return_value = False

        run_tracked_agent(
            allow_unrecorded=True,
            prompt="p",
            worktree_dir=tmp_path,
            config=config,
            issue_num=42,
            store=None,
            branch="agent/issue-42",
            model="claude-sonnet-4-6",
            runner=mock_runner,
        )
        on_spawn = mock_run_agent.call_args.kwargs["on_spawn"]
        assert callable(on_spawn)
        on_spawn(55555)
        assert on_spawn.args[0].launched is True

    @patch("kew.dispatch_core._record_retry_pgid")
    @patch("kew.dispatch_core.run_agent")
    def test_no_store_skips_attempt_registration(
        self, mock_run_agent, register_attempt, tmp_path
    ):
        from kew.dispatch_core import run_tracked_agent
        from kew.tracking import DispatchResult

        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        config.retry.max_attempts = 1
        mock_run_agent.return_value = DispatchResult(
            exit_code=0, issue_num=42, log_file=tmp_path / "x.log"
        )
        mock_runner = MagicMock()
        mock_runner.name = "fake"
        mock_runner.supports_resume.return_value = False

        run_tracked_agent(
            allow_unrecorded=True,
            prompt="p",
            worktree_dir=tmp_path,
            config=config,
            issue_num=42,
            store=None,
            branch="agent/issue-42",
            model="claude-sonnet-4-6",
            runner=mock_runner,
        )
        on_spawn = mock_run_agent.call_args.kwargs["on_spawn"]
        assert on_spawn(55555) is None
        register_attempt.assert_not_called()


class TestWorkerKilledBehavior:
    """A worker whose run was marked killed skips retries and keeps the status."""

    def _config(self, tmp_path):
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        config.retry.max_attempts = 3
        config.retry.backoff_base_seconds = 0
        return config

    def _runner(self):
        mock_runner = MagicMock()
        mock_runner.name = "fake"
        mock_runner.supports_resume.return_value = False
        return mock_runner

    @patch("kew.dispatch_core.time.sleep")
    @patch("kew.dispatch_core.run_agent")
    def test_killed_run_skips_retry_loop(self, mock_run_agent, _sleep, tmp_path):
        from kew.dispatch_core import run_tracked_agent
        from kew.run_store import SqliteRunStore
        from kew.tracking import DispatchResult

        config = self._config(tmp_path)

        with SqliteRunStore(str(tmp_path / "t.db")) as store:

            def _agent_killed(**kwargs):
                pending = store.list_pending()
                assert len(pending) == 1
                record = pending[0]
                record.status = "killed"
                record.error_category = "killed"
                store.save(record)
                return DispatchResult(
                    exit_code=-15,
                    issue_num=42,
                    log_file=tmp_path / "x.log",
                    error_category="transient",
                )

            mock_run_agent.side_effect = _agent_killed
            result = run_tracked_agent(
                allow_unrecorded=True,
                prompt="p",
                worktree_dir=tmp_path,
                config=config,
                issue_num=42,
                store=store,
                branch="agent/issue-42",
                model="claude-sonnet-4-6",
                runner=self._runner(),
            )

            assert mock_run_agent.call_count == 1  # no retries
            assert result.error_category == "killed"
            records = store.list_by_issue(42)
            assert records[0].status == "killed"  # not overwritten to failure
            assert records[0].error_category == "killed"
            ev = store.list_audit_events(event="dispatch_completed")[0]
            assert ev.payload["error_category"] == "killed"

    @patch("kew.dispatch_core.run_agent")
    def test_normal_failure_still_marked_failure(self, mock_run_agent, tmp_path):
        from kew.dispatch_core import run_tracked_agent
        from kew.run_store import SqliteRunStore
        from kew.tracking import DispatchResult

        config = self._config(tmp_path)
        config.retry.max_attempts = 1
        mock_run_agent.return_value = DispatchResult(
            exit_code=1,
            issue_num=42,
            log_file=tmp_path / "x.log",
            error_category="permanent",
        )

        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            run_tracked_agent(
                allow_unrecorded=True,
                prompt="p",
                worktree_dir=tmp_path,
                config=config,
                issue_num=42,
                store=store,
                branch="agent/issue-42",
                model="claude-sonnet-4-6",
                runner=self._runner(),
            )
            records = store.list_by_issue(42)
            assert records[0].status == "failure"

    @patch("kew.dispatch_core.run_agent")
    def test_exception_after_kill_preserves_killed_status(
        self, mock_run_agent, tmp_path
    ):
        from kew.dispatch_core import run_tracked_agent
        from kew.run_store import SqliteRunStore

        config = self._config(tmp_path)
        config.retry.max_attempts = 1

        with SqliteRunStore(str(tmp_path / "t.db")) as store:

            def _kill_then_raise(**kwargs):
                # `kew stop` marks the run killed, then the worker hits an
                # exception during teardown — must NOT become a failure.
                record = store.list_pending()[0]
                record.status = "killed"
                record.error_category = "killed"
                store.save(record)
                raise RuntimeError("boom during teardown")

            mock_run_agent.side_effect = _kill_then_raise
            result = run_tracked_agent(
                allow_unrecorded=True,
                prompt="p",
                worktree_dir=tmp_path,
                config=config,
                issue_num=42,
                store=store,
                branch="agent/issue-42",
                model="claude-sonnet-4-6",
                runner=self._runner(),
            )

            assert result.error_category == "killed"
            records = store.list_by_issue(42)
            assert records[0].status == "killed"  # not overwritten to failure


class TestExecuteDispatchFleetLatch:
    """execute_dispatch refuses while the fleet-stop latch is engaged."""

    def _issue(self):
        return {
            "number": 42,
            "title": "Test",
            "body": "",
            "labels": [{"name": "ready"}],
        }

    def _config(self, tmp_path):
        config = DispatchConfig()
        config.project.repo = "owner/repo"
        config.logging.dir = str(tmp_path / "logs")
        return config

    @patch("kew.dispatch_core.reset_issue_to_ready")
    @patch("kew.dispatch_core._check_github_connectivity")
    def test_refuses_when_latched(self, mock_conn, mock_reset, tmp_path):
        from kew.dispatch_core import execute_dispatch
        from kew.run_store import SqliteRunStore

        config = self._config(tmp_path)
        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            store.set_fleet_stopped("incident")
            outcome = execute_dispatch(
                allow_unrecorded=True, issue=self._issue(), config=config, store=store
            )

        assert outcome.exit_code == 1
        assert outcome.launched is False  # the boundary reports could-not-run
        mock_conn.assert_not_called()  # refused before any dispatch work
        mock_reset.assert_called_once_with(42, config)

    @patch("kew.dispatch_core.reset_issue_to_ready")
    @patch("kew.dispatch_core.run_tracked_agent")
    @patch("kew.dispatch_core.setup_worktree")
    @patch("kew.dispatch_core.resolve_runner")
    @patch("kew.dispatch_core._check_github_connectivity", return_value=True)
    def test_killed_result_resets_labels_and_preserves_worktree(
        self,
        mock_conn,
        mock_resolve,
        mock_worktree,
        mock_run_tracked,
        mock_reset,
        tmp_path,
    ):
        from kew.dispatch_core import execute_dispatch
        from kew.run_store import SqliteRunStore
        from kew.tracking import DispatchResult

        config = self._config(tmp_path)
        worktree = tmp_path / "wt"
        worktree.mkdir()
        mock_worktree.return_value = worktree
        mock_resolve.return_value = MagicMock(
            runner=MagicMock(name="fake"), model="claude-sonnet-4-6"
        )
        mock_run_tracked.return_value = DispatchResult(
            exit_code=-15,
            issue_num=42,
            log_file=tmp_path / "x.log",
            error_category="killed",
        )

        with (
            patch("kew.dispatch_core.cleanup_worktree") as mock_cleanup,
            patch("kew.dispatch_core.mark_failed") as mock_failed,
            SqliteRunStore(str(tmp_path / "t.db")) as store,
        ):
            outcome = execute_dispatch(
                allow_unrecorded=True, issue=self._issue(), config=config, store=store
            )

        assert outcome.exit_code != 0
        mock_failed.assert_not_called()  # operator stop is not a failure
        mock_cleanup.assert_not_called()  # worktree preserved
        mock_reset.assert_called_once_with(42, config)


@pytest.mark.usefixtures("_canonical_existing_pr_published")
class TestApprovalGateNonInteractive:
    """Non-interactive matrix: {feature on/off} × {above/below} × {--approve}.

    ``--approve`` is a ``kew dispatch <issue>`` option (W4.4, #2001); the hold
    message names that spelling, never the removed ``kew run --issue``.
    """

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.cleanup_worktree")
    @patch(f"{_P}.check_pr_exists", return_value=99)
    @patch(f"{_P}.update_pr_labels", return_value=True)
    @patch(f"{_P}.mark_completed")
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    @patch(f"{_P}.setup_worktree", return_value=Path("/tmp/wt"))
    @patch(f"{_P}.resolve_runner")
    @patch(f"{_P}.estimate_dispatch_cost")
    def test_feature_off_never_estimates(
        self,
        mock_estimate: MagicMock,
        mock_resolve_runner: MagicMock,
        mock_worktree: MagicMock,
        mock_prompt: MagicMock,
        mock_run_agent: MagicMock,
        mock_completed: MagicMock,
        mock_pr_labels: MagicMock,
        mock_pr_exists: MagicMock,
        mock_cleanup: MagicMock,
        mock_connectivity: MagicMock,
    ):
        """threshold_usd unset -> zero behavior change, estimator never runs."""
        from kew.dispatch_core import execute_dispatch

        config = DispatchConfig()
        mock_run_agent.return_value = _ok_result()

        rc = execute_dispatch(allow_unrecorded=True, issue=_make_issue(), config=config)

        assert rc.exit_code == 0
        mock_estimate.assert_not_called()
        mock_run_agent.assert_called_once()

    @patch(f"{_P}.log_approval_event")
    @patch(f"{_P}.mark_needs_approval")
    @patch(f"{_P}.estimate_dispatch_cost")
    @patch(f"{_P}.resolve_runner")
    @patch(f"{_P}.run_agent")
    def test_above_threshold_holds_issue(
        self,
        mock_run_agent: MagicMock,
        mock_resolve_runner: MagicMock,
        mock_estimate: MagicMock,
        mock_hold: MagicMock,
        mock_log_event: MagicMock,
        tmp_path: Path,
    ):
        from kew.approval import Estimate
        from kew.dispatch_core import execute_dispatch

        config = DispatchConfig()
        config.approval.threshold_usd = 3.0
        config.logging.dir = str(tmp_path / "logs")
        mock_resolve_runner.return_value.model = "claude-sonnet-4-6"
        mock_estimate.return_value = Estimate(
            usd=5.0, basis="based on 3 similar runs (median $5.00)"
        )

        with patch(f"{_P}._warn") as warn:
            rc = execute_dispatch(
                allow_unrecorded=True, issue=_make_issue(), config=config
            )

        assert rc.exit_code == 0
        mock_hold.assert_called_once_with(42, config)
        mock_run_agent.assert_not_called()
        assert mock_log_event.call_args[1]["event"] == "approval_hold"
        assert mock_log_event.call_args[1]["issue_num"] == 42
        assert mock_log_event.call_args[1]["threshold_usd"] == 3.0
        held = " ".join(str(c.args[0]) for c in warn.call_args_list)
        assert "release with `kew dispatch 42 --approve`" in held
        assert "kew run" not in held

    @patch(f"{_P}.log_approval_event")
    @patch(f"{_P}.mark_needs_approval")
    @patch(f"{_P}.estimate_dispatch_cost")
    @patch(f"{_P}.resolve_runner")
    @patch(f"{_P}.run_agent")
    def test_at_threshold_holds_issue(
        self,
        mock_run_agent: MagicMock,
        mock_resolve_runner: MagicMock,
        mock_estimate: MagicMock,
        mock_hold: MagicMock,
        mock_log_event: MagicMock,
        tmp_path: Path,
    ):
        """estimate >= threshold triggers the gate (inclusive)."""
        from kew.approval import Estimate
        from kew.dispatch_core import execute_dispatch

        config = DispatchConfig()
        config.approval.threshold_usd = 5.0
        config.logging.dir = str(tmp_path / "logs")
        mock_resolve_runner.return_value.model = "claude-sonnet-4-6"
        mock_estimate.return_value = Estimate(usd=5.0, basis="x")

        rc = execute_dispatch(allow_unrecorded=True, issue=_make_issue(), config=config)

        assert rc.exit_code == 0
        mock_hold.assert_called_once()
        mock_run_agent.assert_not_called()

    @patch(f"{_P}.notify_approval_hold")
    @patch(f"{_P}.log_approval_event")
    @patch(f"{_P}.mark_needs_approval")
    @patch(f"{_P}.estimate_dispatch_cost")
    @patch(f"{_P}.resolve_runner")
    @patch(f"{_P}.run_agent")
    def test_hold_fires_notification(
        self,
        mock_run_agent: MagicMock,
        mock_resolve_runner: MagicMock,
        mock_estimate: MagicMock,
        mock_mark: MagicMock,
        mock_log: MagicMock,
        mock_notify: MagicMock,
        tmp_path: Path,
    ):
        from kew.approval import Estimate
        from kew.dispatch_core import execute_dispatch

        config = DispatchConfig()
        config.approval.threshold_usd = 5.0
        config.logging.dir = str(tmp_path / "logs")
        mock_resolve_runner.return_value.model = "claude-sonnet-4-6"
        mock_estimate.return_value = Estimate(
            usd=6.10, basis="median of 8 similar runs"
        )

        rc = execute_dispatch(allow_unrecorded=True, issue=_make_issue(), config=config)

        assert rc.exit_code == 0
        mock_run_agent.assert_not_called()
        mock_notify.assert_called_once()
        kw = mock_notify.call_args.kwargs
        assert kw["issue_num"] == 42
        assert kw["estimate_usd"] == 6.10
        assert kw["threshold_usd"] == 5.0

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.cleanup_worktree")
    @patch(f"{_P}.check_pr_exists", return_value=99)
    @patch(f"{_P}.update_pr_labels", return_value=True)
    @patch(f"{_P}.mark_completed")
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    @patch(f"{_P}.setup_worktree", return_value=Path("/tmp/wt"))
    @patch(f"{_P}.resolve_runner")
    @patch(f"{_P}.log_approval_event")
    @patch(f"{_P}.estimate_dispatch_cost")
    def test_below_threshold_proceeds_silently(
        self,
        mock_estimate: MagicMock,
        mock_log_event: MagicMock,
        mock_resolve_runner: MagicMock,
        mock_worktree: MagicMock,
        mock_prompt: MagicMock,
        mock_run_agent: MagicMock,
        mock_completed: MagicMock,
        mock_pr_labels: MagicMock,
        mock_pr_exists: MagicMock,
        mock_cleanup: MagicMock,
        mock_connectivity: MagicMock,
        tmp_path: Path,
    ):
        from kew.approval import Estimate
        from kew.dispatch_core import execute_dispatch

        config = DispatchConfig()
        config.approval.threshold_usd = 5.0
        config.logging.dir = str(tmp_path / "logs")
        mock_resolve_runner.return_value.model = "claude-sonnet-4-6"
        mock_estimate.return_value = Estimate(usd=1.0, basis="x")
        mock_run_agent.return_value = _ok_result()

        rc = execute_dispatch(allow_unrecorded=True, issue=_make_issue(), config=config)

        assert rc.exit_code == 0
        mock_run_agent.assert_called_once()
        mock_log_event.assert_not_called()

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.cleanup_worktree")
    @patch(f"{_P}.check_pr_exists", return_value=99)
    @patch(f"{_P}.update_pr_labels", return_value=True)
    @patch(f"{_P}.mark_completed")
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    @patch(f"{_P}.setup_worktree", return_value=Path("/tmp/wt"))
    @patch(f"{_P}.resolve_runner")
    @patch(f"{_P}.mark_needs_approval")
    @patch(f"{_P}.log_approval_event")
    @patch(f"{_P}.estimate_dispatch_cost")
    def test_approved_above_threshold_proceeds_and_logs_granted(
        self,
        mock_estimate: MagicMock,
        mock_log_event: MagicMock,
        mock_hold: MagicMock,
        mock_resolve_runner: MagicMock,
        mock_worktree: MagicMock,
        mock_prompt: MagicMock,
        mock_run_agent: MagicMock,
        mock_completed: MagicMock,
        mock_pr_labels: MagicMock,
        mock_pr_exists: MagicMock,
        mock_cleanup: MagicMock,
        mock_connectivity: MagicMock,
        tmp_path: Path,
    ):
        from kew.approval import Estimate
        from kew.dispatch_core import execute_dispatch

        config = DispatchConfig()
        config.approval.threshold_usd = 3.0
        config.logging.dir = str(tmp_path / "logs")
        mock_resolve_runner.return_value.model = "claude-sonnet-4-6"
        mock_estimate.return_value = Estimate(usd=5.0, basis="x")
        mock_run_agent.return_value = _ok_result()

        rc = execute_dispatch(
            allow_unrecorded=True, issue=_make_issue(), config=config, approved=True
        )

        assert rc.exit_code == 0
        mock_run_agent.assert_called_once()
        mock_hold.assert_not_called()
        assert mock_log_event.call_args[1]["event"] == "approval_granted"

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.cleanup_worktree")
    @patch(f"{_P}.check_pr_exists", return_value=99)
    @patch(f"{_P}.update_pr_labels", return_value=True)
    @patch(f"{_P}.mark_completed")
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    @patch(f"{_P}.setup_worktree", return_value=Path("/tmp/wt"))
    @patch(f"{_P}.resolve_runner")
    @patch(f"{_P}.log_approval_event")
    @patch(f"{_P}.estimate_dispatch_cost")
    def test_approved_with_feature_off_is_noop(
        self,
        mock_estimate: MagicMock,
        mock_log_event: MagicMock,
        mock_resolve_runner: MagicMock,
        mock_worktree: MagicMock,
        mock_prompt: MagicMock,
        mock_run_agent: MagicMock,
        mock_completed: MagicMock,
        mock_pr_labels: MagicMock,
        mock_pr_exists: MagicMock,
        mock_cleanup: MagicMock,
        mock_connectivity: MagicMock,
    ):
        """--approve without [approval] configured: accepted no-op."""
        from kew.dispatch_core import execute_dispatch

        config = DispatchConfig()  # threshold None
        mock_run_agent.return_value = _ok_result()

        rc = execute_dispatch(
            allow_unrecorded=True, issue=_make_issue(), config=config, approved=True
        )

        assert rc.exit_code == 0
        mock_run_agent.assert_called_once()
        mock_estimate.assert_not_called()
        mock_log_event.assert_not_called()

    @patch(f"{_P}.check_budget")
    @patch(f"{_P}.log_approval_event")
    @patch(f"{_P}.mark_needs_approval")
    @patch(f"{_P}.estimate_dispatch_cost")
    @patch(f"{_P}.resolve_runner")
    @patch(f"{_P}.run_agent")
    def test_force_budget_does_not_bypass_gate(
        self,
        mock_run_agent: MagicMock,
        mock_resolve_runner: MagicMock,
        mock_estimate: MagicMock,
        mock_hold: MagicMock,
        mock_log_event: MagicMock,
        mock_check_budget: MagicMock,
        tmp_path: Path,
    ):
        """--force skips the budget check but the gate still holds."""
        from kew.approval import Estimate
        from kew.dispatch_core import execute_dispatch

        config = DispatchConfig()
        config.approval.threshold_usd = 3.0
        config.logging.dir = str(tmp_path / "logs")
        mock_resolve_runner.return_value.model = "claude-sonnet-4-6"
        mock_estimate.return_value = Estimate(usd=5.0, basis="x")
        store = _mock_store()

        rc = execute_dispatch(
            allow_unrecorded=True,
            issue=_make_issue(),
            config=config,
            store=store,
            force_budget=True,
        )

        assert rc.exit_code == 0
        mock_check_budget.assert_not_called()
        mock_hold.assert_called_once()
        mock_run_agent.assert_not_called()

    @patch(f"{_P}.mark_needs_approval")
    @patch(f"{_P}.resolve_runner")
    @patch(f"{_P}.run_agent")
    def test_hold_writes_no_run_record_and_logs_real_event(
        self,
        mock_run_agent: MagicMock,
        mock_resolve_runner: MagicMock,
        mock_hold: MagicMock,
        tmp_path: Path,
    ):
        """Acceptance #1 end-to-end: seeded history median above threshold
        -> hold, approval_hold event with honest basis, no run record."""
        from datetime import UTC, datetime, timedelta

        from kew.dispatch_core import execute_dispatch
        from kew.run_store import RunRecord, SqliteRunStore

        config = DispatchConfig()
        config.approval.threshold_usd = 3.0
        config.logging.dir = str(tmp_path / "logs")
        mock_resolve_runner.return_value.model = "claude-sonnet-4-6"

        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            now = datetime.now(UTC)
            for i in range(3):
                store.save(
                    RunRecord(
                        run_id=f"hist{i}",
                        issue_num=100 + i,
                        branch=f"agent/issue-{100 + i}",
                        status="success",
                        exit_code=0,
                        started_at=now - timedelta(minutes=10 - i),
                        estimated_cost_usd=5.0,
                        model="claude-sonnet-4-6",
                        complexity="medium",
                    )
                )

            rc = execute_dispatch(
                allow_unrecorded=True,
                issue=_make_issue(labels=[{"name": "ready"}, {"name": "medium"}]),
                config=config,
                store=store,
            )

            assert rc.exit_code == 0
            mock_hold.assert_called_once_with(42, config)
            mock_run_agent.assert_not_called()
            # No run record for the held dispatch — only the 3 seeded ones
            assert {r.run_id for r in store.list_all()} == {
                "hist0",
                "hist1",
                "hist2",
            }

            # approval_hold event is in audit_events, not dispatch-log.jsonl
            events = store.list_audit_events(event="approval_hold")
            assert len(events) == 1
            ev = events[0]
            assert ev.issue_num == 42
            assert ev.payload["estimate_usd"] == pytest.approx(5.0)
            assert ev.payload["basis"] == "based on 3 similar runs (median $5.00)"
            assert ev.payload["threshold_usd"] == pytest.approx(3.0)

        assert not (tmp_path / "logs" / "dispatch-log.jsonl").exists()


@pytest.mark.usefixtures("_canonical_existing_pr_published")
class TestApprovalGateInteractive:
    """Interactive (TTY) confirm/decline, and non-TTY hold-not-hang."""

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.cleanup_worktree")
    @patch(f"{_P}.check_pr_exists", return_value=99)
    @patch(f"{_P}.update_pr_labels", return_value=True)
    @patch(f"{_P}.mark_completed")
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    @patch(f"{_P}.setup_worktree", return_value=Path("/tmp/wt"))
    @patch(f"{_P}.resolve_runner")
    @patch(f"{_P}.log_approval_event")
    @patch(f"{_P}.confirm_dispatch", return_value=True)
    @patch(f"{_P}.estimate_dispatch_cost")
    @patch(f"{_P}.sys.stdin")
    def test_confirm_yes_proceeds_and_logs_confirmed(
        self,
        mock_stdin: MagicMock,
        mock_estimate: MagicMock,
        mock_confirm: MagicMock,
        mock_log_event: MagicMock,
        mock_resolve_runner: MagicMock,
        mock_worktree: MagicMock,
        mock_prompt: MagicMock,
        mock_run_agent: MagicMock,
        mock_completed: MagicMock,
        mock_pr_labels: MagicMock,
        mock_pr_exists: MagicMock,
        mock_cleanup: MagicMock,
        mock_connectivity: MagicMock,
        tmp_path: Path,
    ):
        from kew.approval import Estimate
        from kew.dispatch_core import execute_dispatch

        mock_stdin.isatty.return_value = True
        config = DispatchConfig()
        config.approval.threshold_usd = 3.0
        config.logging.dir = str(tmp_path / "logs")
        mock_resolve_runner.return_value.model = "claude-sonnet-4-6"
        est = Estimate(usd=5.0, basis="based on 3 similar runs (median $5.00)")
        mock_estimate.return_value = est
        mock_run_agent.return_value = _ok_result()

        rc = execute_dispatch(
            allow_unrecorded=True, issue=_make_issue(), config=config, interactive=True
        )

        assert rc.exit_code == 0
        mock_confirm.assert_called_once_with(
            issue_num=42, estimate=est, threshold_usd=3.0
        )
        mock_run_agent.assert_called_once()
        assert mock_log_event.call_args[1]["event"] == "approval_confirmed"

    @patch(f"{_P}.log_approval_event")
    @patch(f"{_P}.mark_ready_from_approval")
    @patch(f"{_P}.confirm_dispatch", return_value=False)
    @patch(f"{_P}.estimate_dispatch_cost")
    @patch(f"{_P}.resolve_runner")
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.sys.stdin")
    def test_decline_returns_issue_to_ready(
        self,
        mock_stdin: MagicMock,
        mock_run_agent: MagicMock,
        mock_resolve_runner: MagicMock,
        mock_estimate: MagicMock,
        mock_confirm: MagicMock,
        mock_mark_ready: MagicMock,
        mock_log_event: MagicMock,
        tmp_path: Path,
    ):
        from kew.approval import Estimate
        from kew.dispatch_core import execute_dispatch

        mock_stdin.isatty.return_value = True
        config = DispatchConfig()
        config.approval.threshold_usd = 3.0
        config.logging.dir = str(tmp_path / "logs")
        mock_resolve_runner.return_value.model = "claude-sonnet-4-6"
        mock_estimate.return_value = Estimate(usd=5.0, basis="x")

        rc = execute_dispatch(
            allow_unrecorded=True, issue=_make_issue(), config=config, interactive=True
        )

        assert rc.exit_code == 0
        mock_mark_ready.assert_called_once_with(42, config)
        mock_run_agent.assert_not_called()
        assert mock_log_event.call_args[1]["event"] == "approval_declined"

    @patch(f"{_P}.log_approval_event")
    @patch(f"{_P}.mark_needs_approval")
    @patch(f"{_P}.confirm_dispatch")
    @patch(f"{_P}.estimate_dispatch_cost")
    @patch(f"{_P}.resolve_runner")
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.sys.stdin")
    def test_interactive_without_tty_holds_instead_of_hanging(
        self,
        mock_stdin: MagicMock,
        mock_run_agent: MagicMock,
        mock_resolve_runner: MagicMock,
        mock_estimate: MagicMock,
        mock_confirm: MagicMock,
        mock_hold: MagicMock,
        mock_log_event: MagicMock,
        tmp_path: Path,
    ):
        """TTY detection: interactive caller without a TTY must hold, not prompt."""
        from kew.approval import Estimate
        from kew.dispatch_core import execute_dispatch

        mock_stdin.isatty.return_value = False
        config = DispatchConfig()
        config.approval.threshold_usd = 3.0
        config.logging.dir = str(tmp_path / "logs")
        mock_resolve_runner.return_value.model = "claude-sonnet-4-6"
        mock_estimate.return_value = Estimate(usd=5.0, basis="x")

        rc = execute_dispatch(
            allow_unrecorded=True, issue=_make_issue(), config=config, interactive=True
        )

        assert rc.exit_code == 0
        mock_confirm.assert_not_called()
        mock_hold.assert_called_once()
        assert mock_log_event.call_args[1]["event"] == "approval_hold"


class TestRunRowEnvelope:
    """The pending-save stamps who dispatched, which repo, which endpoint."""

    def _run(
        self,
        store,
        config,
        monkeypatch,
        runner_name="claude-code",
        origin_slug=None,
    ):
        monkeypatch.setenv("KEW_ACTOR", "dispatcher-dan")
        monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
        from kew.dispatch_core import run_tracked_agent

        runner = MagicMock()
        runner.name = runner_name
        runner.supports_resume.return_value = False
        with (
            patch(f"{_P}.run_agent", return_value=_ok_result(exit_code=0)),
            patch(f"{_P}.check_pr_exists", return_value=None),
            patch(f"{_P}._repo_slug_from_origin", return_value=origin_slug),
        ):
            run_tracked_agent(
                allow_unrecorded=True,
                prompt="p",
                worktree_dir=Path("/tmp/wt"),
                config=config,
                issue_num=42,
                store=store,
                branch="agent/issue-42",
                model="claude-sonnet-4-6",
                runner=runner,
                complexity="medium",
            )

    def test_pending_save_carries_envelope(self, tmp_path, monkeypatch):
        from kew.run_store import SqliteRunStore

        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        config.project.repo = "octo/widgets"
        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            self._run(store, config, monkeypatch)
            runs = store.list_all()
            assert len(runs) == 1
            record = runs[0]
            assert record.actor_type == "human"
            assert record.actor_id == "dispatcher-dan"
            assert record.repo == "octo/widgets"
            assert record.endpoint == "anthropic:default"
            assert record.policy_hash is None  # no policy bound → None (#250)

    def test_envelope_survives_completion_save(self, tmp_path, monkeypatch):
        from kew.run_store import SqliteRunStore

        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        config.project.repo = "octo/widgets"
        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            self._run(store, config, monkeypatch)
            record = store.list_all()[0]
            assert record.status == "success"  # completion save happened
            assert record.actor_id == "dispatcher-dan"  # envelope intact
            assert record.endpoint == "anthropic:default"

    def test_empty_repo_stored_as_null(self, tmp_path, monkeypatch):
        from kew.run_store import SqliteRunStore

        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        config.project.repo = ""  # undetected repo, undetectable origin
        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            self._run(store, config, monkeypatch, origin_slug=None)
            assert store.list_all()[0].repo is None

    def test_unset_repo_falls_back_to_the_project_origin(self, tmp_path, monkeypatch):
        """The dispatching parent stamps the slug detached publication needs.

        With ``project.repo`` unset, a canonical publication never reads the
        worktree origin, so the run row is the only binding it has (#2097).
        """
        from kew.run_store import SqliteRunStore

        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        config.project.repo = ""  # "auto-detected from gh"
        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            self._run(store, config, monkeypatch, origin_slug="octo/widgets")
            assert store.list_all()[0].repo == "octo/widgets"


def test_dispatch_populates_policy_hash_and_retains_snapshot(tmp_path, monkeypatch):
    import kew.audit as audit
    from kew.run_store import SqliteRunStore

    audit.set_audit_context(repo="o/r", policy_hash="pol-xyz", snapshot_json='{"v":1}')
    try:
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        config.project.repo = "o/r"
        monkeypatch.setenv("KEW_ACTOR", "dispatcher-dan")
        monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
        from kew.dispatch_core import run_tracked_agent

        runner = MagicMock()
        runner.name = "claude-code"
        runner.supports_resume.return_value = False
        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            with (
                patch(f"{_P}.run_agent", return_value=_ok_result(exit_code=0)),
                patch(f"{_P}.check_pr_exists", return_value=None),
            ):
                run_tracked_agent(
                    allow_unrecorded=True,
                    prompt="p",
                    worktree_dir=Path("/tmp/wt"),
                    config=config,
                    issue_num=42,
                    store=store,
                    branch="agent/issue-42",
                    model="claude-sonnet-4-6",
                    runner=runner,
                    complexity="medium",
                )
            runs = store.list_all()
            assert runs and runs[0].policy_hash == "pol-xyz"
            row = store.get_policy_snapshot("pol-xyz")
            assert row is not None and row[0] == '{"v":1}'
    finally:
        audit.set_audit_context(repo=None)  # never leak the binding to later tests


class TestRunTrackedAgentPause:
    """run_tracked_agent threads should_pause and propagates paused."""

    @patch("kew.dispatch_core.run_agent")
    def test_run_tracked_agent_propagates_paused(self, mock_run_agent, tmp_path):
        from kew.dispatch_core import run_tracked_agent
        from kew.tracking import DispatchResult

        seen: dict = {}

        def _fake_run_agent(*args, should_pause=None, **kwargs):
            seen["should_pause"] = should_pause
            return DispatchResult(
                exit_code=0,
                issue_num=1,
                log_file=tmp_path / "x.log",
                paused=True,
            )

        mock_run_agent.side_effect = _fake_run_agent
        mock_runner = MagicMock()
        mock_runner.name = "fake"
        mock_runner.supports_resume.return_value = False

        def sp() -> bool:
            return False

        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        config.retry.max_attempts = 1

        result = run_tracked_agent(
            allow_unrecorded=True,
            prompt="p",
            worktree_dir=tmp_path,
            config=config,
            issue_num=1,
            model="claude-sonnet-4-6",
            runner=mock_runner,
            should_pause=sp,
        )

        assert result.paused is True
        assert seen["should_pause"] is sp


@pytest.mark.usefixtures("_canonical_existing_pr_published")
class TestDispatchOutcomeRunIdThreaded:
    """execute_dispatch must propagate run_id into DispatchOutcome.

    Regression test: before the fix, DispatchOutcome.run_id was always None,
    so the post-dispatch capture hook in cli._dispatch_one never fired.
    """

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.cleanup_worktree")
    @patch(f"{_P}.check_pr_exists", return_value=99)
    @patch(f"{_P}.update_pr_labels", return_value=True)
    @patch(f"{_P}.mark_completed")
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    @patch(f"{_P}.setup_worktree", return_value=Path("/tmp/wt"))
    @patch(f"{_P}.resolve_runner")
    def test_outcome_run_id_populated_on_success(
        self,
        mock_resolve_runner: MagicMock,
        mock_worktree: MagicMock,
        mock_prompt: MagicMock,
        mock_run_agent: MagicMock,
        mock_completed: MagicMock,
        mock_pr_labels: MagicMock,
        mock_pr_exists: MagicMock,
        mock_cleanup: MagicMock,
        mock_connectivity: MagicMock,
        tmp_path,
    ):
        """DispatchOutcome.run_id must be a non-empty string on success."""
        from kew.dispatch_core import execute_dispatch

        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        mock_run_agent.return_value = _ok_result()

        outcome = execute_dispatch(
            allow_unrecorded=True, issue=_make_issue(), config=config
        )

        assert outcome.exit_code == 0
        # run_id must be a non-empty truthy string — this is what gates the
        # post-dispatch capture hook in cli._dispatch_one.
        assert outcome.run_id is not None, "DispatchOutcome.run_id must not be None"
        assert outcome.run_id != "", "DispatchOutcome.run_id must not be empty"

    def test_outcome_run_id_populated_on_killed(self, tmp_path):
        """The operator-kill early return must also carry run_id."""
        from kew.dispatch_core import execute_dispatch

        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        with patch.multiple(
            _P,
            _check_github_connectivity=MagicMock(return_value=True),
            resolve_runner=MagicMock(),
            setup_worktree=MagicMock(return_value=Path("/tmp/wt")),
            build_prompt=MagicMock(return_value="prompt"),
            run_agent=MagicMock(
                return_value=_ok_result(error_category="killed", exit_code=1)
            ),
            reset_issue_to_ready=MagicMock(),
        ):
            outcome = execute_dispatch(
                allow_unrecorded=True, issue=_make_issue(), config=config
            )
        assert outcome.run_id and outcome.run_id != ""

    def test_outcome_run_id_populated_on_budget_exceeded(self, tmp_path):
        """The budget-exceeded (clean worktree) early return must carry run_id."""
        from kew.dispatch_core import execute_dispatch

        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        # The escape hatch: canonical mode records a no-change candidate here
        # (review round 11 of #1440; tests/test_publication_round3.py).
        config.publication.publish = "checkout"
        config.sandbox.dangerously_skip_containment = True
        with patch.multiple(
            _P,
            _check_github_connectivity=MagicMock(return_value=True),
            resolve_runner=MagicMock(),
            setup_worktree=MagicMock(return_value=Path("/tmp/wt")),
            build_prompt=MagicMock(return_value="prompt"),
            run_agent=MagicMock(
                return_value=_ok_result(error_category="budget_exceeded")
            ),
            _checkout_has_work=MagicMock(return_value=False),
            mark_completed=MagicMock(),
            _auto_unblock_dependents=MagicMock(),
            _get_changed_files=MagicMock(return_value=[]),
            cleanup_worktree=MagicMock(),
        ):
            outcome = execute_dispatch(
                allow_unrecorded=True, issue=_make_issue(), config=config
            )
        assert outcome.run_id and outcome.run_id != ""


class TestBudgetStopped:
    """Live watchdog over-budget → graceful budget_stop label, not auto-resumed."""

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.mark_failed")
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    @patch(f"{_P}.setup_worktree", return_value=Path("/tmp/wt"))
    @patch(f"{_P}.resolve_runner")
    def test_budget_stopped_marks_label_and_preserves(
        self,
        mock_resolve_runner: MagicMock,
        mock_worktree: MagicMock,
        mock_prompt: MagicMock,
        mock_run_agent: MagicMock,
        mock_failed: MagicMock,
        mock_connectivity: MagicMock,
    ):
        from kew.dispatch_core import execute_dispatch

        config = DispatchConfig()
        # budget_exceeded=True is the live watchdog trip: run_tracked_agent
        # relabels it "budget_stop" (graceful, #859), not "budget_stopped".
        result = _ok_result(
            exit_code=143, error_category="budget_exceeded", budget_exceeded=True
        )
        mock_run_agent.return_value = result

        rc = execute_dispatch(allow_unrecorded=True, issue=_make_issue(), config=config)

        assert rc.exit_code != 0
        mock_failed.assert_called_once()
        call_kwargs = mock_failed.call_args
        assert config.labels.budget_stop in call_kwargs[1]["extra_labels"]

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}._get_changed_files", return_value=[])
    @patch(f"{_P}.mark_failed")
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    @patch(f"{_P}.setup_worktree")
    @patch(f"{_P}.resolve_runner")
    def test_budget_stop_commits_wip_to_branch(
        self,
        mock_resolve_runner: MagicMock,
        mock_worktree: MagicMock,
        mock_prompt: MagicMock,
        mock_run_agent: MagicMock,
        mock_failed: MagicMock,
        mock_changed: MagicMock,
        mock_connectivity: MagicMock,
        tmp_path: Path,
    ):
        """Graceful budget stop commits leftover WIP to the branch (#859)."""
        from kew.dispatch_core import execute_dispatch

        worktree = tmp_path / "wt"
        worktree.mkdir()

        def git(*args: str) -> str:
            return subprocess.run(
                ["git", *args], cwd=worktree, check=True, capture_output=True, text=True
            ).stdout.strip()

        git("init", "-b", "kew/issue-42")
        git("config", "user.email", "test@example.com")
        git("config", "user.name", "Test")
        (worktree / "README.md").write_text("initial\n")
        git("add", "README.md")
        git("commit", "-m", "initial")
        base = git("rev-parse", "HEAD")
        # The agent's leftover work: one edit, one untracked file.
        (worktree / "README.md").write_text("edited\n")
        (worktree / "wip.py").write_text("x = 1\n")
        mock_worktree.return_value = worktree

        config = DispatchConfig()
        # Committing in the checkout is the checkout escape hatch; canonical
        # mode leaves WIP where it is and says so (round 9 of #1440).
        config.publication.publish = "checkout"
        config.sandbox.dangerously_skip_containment = True
        result = _ok_result(
            exit_code=143, error_category="budget_exceeded", budget_exceeded=True
        )
        mock_run_agent.return_value = result

        execute_dispatch(allow_unrecorded=True, issue=_make_issue(), config=config)

        # The WIP commit is the non-destructive guarantee: the branch tip moved
        # one commit past base and that commit carries the leftover work.
        assert git("rev-parse", "HEAD~1") == base
        assert git("show", "HEAD:README.md") == "edited"
        assert git("show", "HEAD:wip.py") == "x = 1"
        assert git("status", "--porcelain") == ""


@pytest.mark.usefixtures("_canonical_existing_pr_published")
class TestWatchdogKillAutoResume:
    """exit 143 + session_id + non-operator → auto-resume via retry loop."""

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.time.sleep")
    @patch(f"{_P}.cleanup_worktree")
    @patch(f"{_P}.update_pr_labels")
    @patch(f"{_P}.check_pr_exists")
    @patch(f"{_P}.mark_completed")
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    @patch(f"{_P}.setup_worktree", return_value=Path("/tmp/wt"))
    @patch(f"{_P}.resolve_runner")
    def test_watchdog_kill_resumes_with_session(
        self,
        mock_resolve_runner: MagicMock,
        mock_worktree: MagicMock,
        mock_prompt: MagicMock,
        mock_run_agent: MagicMock,
        mock_completed: MagicMock,
        mock_pr_exists: MagicMock,
        mock_pr_labels: MagicMock,
        mock_cleanup: MagicMock,
        mock_sleep: MagicMock,
        mock_connectivity: MagicMock,
    ):
        from kew.dispatch_core import execute_dispatch

        config = DispatchConfig(
            retry=RetryConfig(max_attempts=3, max_resume_attempts=2)
        )
        mock_resolve_runner.return_value.supports_resume.return_value = True
        mock_run_agent.side_effect = [
            _fail_result(
                exit_code=143,
                error_category="watchdog_kill",
                session_id="sess-wdg",
            ),
            _ok_result(),
        ]
        mock_pr_exists.side_effect = [None, 99]

        rc = execute_dispatch(allow_unrecorded=True, issue=_make_issue(), config=config)

        assert rc.exit_code == 0
        second_call = mock_run_agent.call_args_list[1]
        assert second_call[1]["resume_session_id"] == "sess-wdg"


class TestOperatorStopNotResumed:
    """Operator stop (kew stop) stays killed — NOT auto-resumed.

    Uses run_tracked_agent to test the full retry-loop behavior: the
    operator stop (stored status=killed) must break the retry loop
    even when exit code is 143 (which would otherwise be retried).
    """

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.reset_issue_to_ready")
    @patch(f"{_P}.run_tracked_agent")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    @patch(f"{_P}.setup_worktree", return_value=Path("/tmp/wt"))
    @patch(f"{_P}.resolve_runner")
    def test_operator_stop_stays_killed(
        self,
        mock_resolve_runner: MagicMock,
        mock_worktree: MagicMock,
        mock_prompt: MagicMock,
        mock_run_tracked: MagicMock,
        mock_reset: MagicMock,
        mock_connectivity: MagicMock,
    ):
        from kew.dispatch_core import execute_dispatch

        config = DispatchConfig()
        result = _ok_result(exit_code=143, error_category="killed")
        mock_run_tracked.return_value = result

        rc = execute_dispatch(allow_unrecorded=True, issue=_make_issue(), config=config)

        assert rc.exit_code != 0
        mock_run_tracked.assert_called_once()
        mock_reset.assert_called_once()


class TestMaterializeCheckoutFreshBase:
    """#749: the agent branch must be cut from the TRUE upstream base — the
    stitches wave dispatched four agents onto a 54-commit-stale local main."""

    @pytest.mark.parametrize("packed", [False, True], ids=["loose", "bitmap-pack"])
    def test_checkout_excludes_unrelated_and_unreachable_objects(
        self, tmp_path, monkeypatch, packed: bool
    ):
        import shutil

        from kew.dispatch_core import _materialize_checkout

        source = make_narrow_checkout_source(tmp_path, packed=packed)
        monkeypatch.chdir(source.repo)
        checkout = _materialize_checkout(
            "security", "agent/security-test", DispatchConfig()
        )
        try:
            assert_narrow_checkout(
                source,
                checkout,
                required_refs=("main",),
                expected_refs={
                    "refs/heads/agent/security-test",
                    "refs/remotes/origin/main",
                },
            )
        finally:
            shutil.rmtree(checkout, ignore_errors=True)

    def test_failure_removes_claimed_checkout(self, tmp_path, monkeypatch):
        from kew import dispatch_core

        root = tmp_path / "work-root"
        monkeypatch.setenv("KEW_WORK_ROOT", str(root))

        with (
            patch.object(dispatch_core, "run") as mock_run,
            patch.object(
                dispatch_core.worktree,
                "clone_local_checkout",
                side_effect=RuntimeError("clone failed"),
            ),
        ):
            mock_run.return_value = MagicMock(stdout=str(tmp_path))
            with pytest.raises(RuntimeError, match="clone failed"):
                dispatch_core._materialize_checkout(
                    "5", "agent/issue-5", DispatchConfig()
                )

        # Neither the checkout nor its claim's lock file is left behind in the
        # scope it was made in (no ledger is registered here, so unscoped).
        from kew import work_dirs

        scope = root / work_dirs.UNSCOPED
        assert sorted(entry.name for entry in scope.iterdir()) == []

    def test_tempdir_prefix_uses_branch_slug_for_linear_id(self, tmp_path, monkeypatch):
        # A5b: a non-numeric (Linear) work-item id reaches the checkout tempdir
        # as its branch-safe slug (lowercased), never the raw id.
        from kew import dispatch_core
        from kew.config import DispatchConfig

        with (
            patch.object(dispatch_core, "run") as mock_run,
            # Clone + fetch now live in the shared worktree primitive (Wave 2),
            # so stub them there rather than through dispatch_core.run. The
            # stand-in clone leaves the one thing a clone must: a .git.
            patch.object(
                dispatch_core.worktree,
                "clone_local_checkout",
                side_effect=lambda _repo, checkout, _base: (checkout / ".git").mkdir(),
            ),
            patch.object(dispatch_core.worktree, "fetch_ref"),
            patch.object(dispatch_core, "_agent_branch_start_point", return_value="x"),
            patch.object(dispatch_core, "_write_prompt_exclude"),
        ):
            monkeypatch.setenv("KEW_WORK_ROOT", str(tmp_path / "root"))
            mock_run.return_value = MagicMock(stdout="repo\n")
            checkout = dispatch_core._materialize_checkout(
                "SFD-12", "agent/sfd-12", DispatchConfig()
            )

        from kew import work_dirs

        assert checkout.parent == tmp_path / "root" / work_dirs.UNSCOPED
        assert checkout.name.startswith("kew-sandbox-sfd-12-")

    def test_branches_from_upstream_tip_not_stale_local_main(
        self, tmp_path, monkeypatch
    ):
        import shutil as _shutil
        import subprocess as _sp

        from kew.dispatch_core import _materialize_checkout
        from kew.subprocess_utils import TOOL_PATHS

        git = _shutil.which("git")
        assert git is not None
        monkeypatch.setitem(TOOL_PATHS, "git", git)

        def g(cwd: Path, *args: str) -> None:
            _sp.run(
                [git, "-c", "user.name=t", "-c", "user.email=t@t", *args],
                cwd=cwd,
                check=True,
                capture_output=True,
            )

        # Upstream repo with commit A; operator clone taken at A; upstream
        # then advances to B — the operator never pulls (the stale shape).
        upstream = tmp_path / "upstream"
        upstream.mkdir()
        g(upstream, "init", "-b", "main")
        (upstream / "base.txt").write_text("a\n")
        g(upstream, "add", "-A")
        g(upstream, "commit", "-m", "A")
        local = tmp_path / "local"
        g(tmp_path, "clone", str(upstream), str(local))
        (upstream / "new.txt").write_text("b\n")
        g(upstream, "add", "-A")
        g(upstream, "commit", "-m", "B")

        monkeypatch.chdir(local)
        config = DispatchConfig()
        checkout = _materialize_checkout("5", "agent/issue-5", config)
        try:
            # The agent branch must carry upstream's tip (B), which the
            # operator's local main has never seen.
            assert (checkout / "new.txt").exists()
        finally:
            import shutil as _rm

            _rm.rmtree(checkout, ignore_errors=True)

    def test_materialize_writes_prompt_exclude(self, tmp_path, monkeypatch):
        """#725: checkout materialization writes a worktree-local exclude so a
        `git add -A` never commits the mounted prompt handoff into a PR branch."""
        import shutil as _shutil
        import subprocess as _sp

        from kew.dispatch_core import _materialize_checkout
        from kew.sandbox import PROMPT_FILENAME
        from kew.subprocess_utils import TOOL_PATHS

        git = _shutil.which("git")
        assert git is not None
        monkeypatch.setitem(TOOL_PATHS, "git", git)

        def g(cwd: Path, *args: str) -> str:
            return _sp.run(
                [git, "-c", "user.name=t", "-c", "user.email=t@t", *args],
                cwd=cwd,
                check=True,
                capture_output=True,
                text=True,
            ).stdout

        upstream = tmp_path / "upstream"
        upstream.mkdir()
        g(upstream, "init", "-b", "main")
        (upstream / "base.txt").write_text("a\n")
        g(upstream, "add", "-A")
        g(upstream, "commit", "-m", "A")
        local = tmp_path / "local"
        g(tmp_path, "clone", str(upstream), str(local))

        monkeypatch.chdir(local)
        checkout = _materialize_checkout("6", "agent/issue-6", DispatchConfig())
        try:
            # The exclude is worktree-local (.git/info/exclude), NOT the
            # tracked .gitignore — so it also covers arbitrary user repos.
            exclude = (checkout / ".git" / "info" / "exclude").read_text()
            assert PROMPT_FILENAME in exclude
            assert not (checkout / ".gitignore").exists()

            # git itself must treat the prompt file as ignored: create it,
            # `git add -A`, and confirm it never enters the index.
            (checkout / PROMPT_FILENAME).write_text("secret prompt\n")
            g(checkout, "add", "-A")
            staged = g(checkout, "diff", "--cached", "--name-only")
            assert PROMPT_FILENAME not in staged
        finally:
            import shutil as _rm

            _rm.rmtree(checkout, ignore_errors=True)

    def test_dispatch_checkout_prompt_not_trackable(self, tmp_path, monkeypatch):
        """#883 B2: a dispatch checkout must never show .kew-prompt.txt as
        trackable — `git status` must not report it as untracked (??) even when
        the file exists in the working tree.  The .git/info/exclude written at
        materialization tells git to treat it as ignored on all git code-paths,
        not just `git add -A`."""
        import shutil as _shutil
        import subprocess as _sp

        from kew.dispatch_core import _materialize_checkout
        from kew.sandbox import PROMPT_FILENAME
        from kew.subprocess_utils import TOOL_PATHS

        git = _shutil.which("git")
        assert git is not None
        monkeypatch.setitem(TOOL_PATHS, "git", git)

        def g(cwd: Path, *args: str) -> str:
            return _sp.run(
                [git, "-c", "user.name=t", "-c", "user.email=t@t", *args],
                cwd=cwd,
                check=True,
                capture_output=True,
                text=True,
            ).stdout

        upstream = tmp_path / "upstream"
        upstream.mkdir()
        g(upstream, "init", "-b", "main")
        (upstream / "base.txt").write_text("a\n")
        g(upstream, "add", "-A")
        g(upstream, "commit", "-m", "init")
        local = tmp_path / "local"
        g(tmp_path, "clone", str(upstream), str(local))

        monkeypatch.chdir(local)
        checkout = _materialize_checkout("7", "agent/issue-7", DispatchConfig())
        try:
            # Simulate the host writing the prompt file just before container launch.
            (checkout / PROMPT_FILENAME).write_text("top-secret prompt\n")

            # git status --porcelain must NOT show '?? .kew-prompt.txt':
            # an excluded/ignored file is invisible to git's tracking machinery.
            status = g(checkout, "status", "--porcelain")
            assert PROMPT_FILENAME not in status, (
                f"{PROMPT_FILENAME!r} shown in git status (trackable); got: {status!r}"
            )

            # ls-files --others --exclude-standard also must not list it:
            # confirms the exclude is respected on all git enumeration paths.
            others = g(checkout, "ls-files", "--others", "--exclude-standard")
            assert PROMPT_FILENAME not in others, (
                f"{PROMPT_FILENAME!r} listed as untracked candidate; got: {others!r}"
            )
        finally:
            import shutil as _rm

            _rm.rmtree(checkout, ignore_errors=True)

    def test_zero_commit_stale_branch_resets_to_current_base(
        self, tmp_path, monkeypatch
    ):
        """#743: an existing remote agent branch that a prior (failed) attempt
        cut from a now-stale base, with zero commits ahead, must NOT be reused
        as-is — it is reset onto the freshly fetched origin/<base> tip so the
        retry builds on current main, not the stale checkout."""
        import shutil as _shutil
        import subprocess as _sp

        from kew.dispatch_core import _materialize_checkout
        from kew.subprocess_utils import TOOL_PATHS

        git = _shutil.which("git")
        assert git is not None
        monkeypatch.setitem(TOOL_PATHS, "git", git)

        def g(cwd: Path, *args: str) -> None:
            _sp.run(
                [git, "-c", "user.name=t", "-c", "user.email=t@t", *args],
                cwd=cwd,
                check=True,
                capture_output=True,
            )

        # Upstream at A; a prior attempt pushed agent/issue-7 cut from A with
        # NO commits of its own; upstream main then advanced to B.
        upstream = tmp_path / "upstream"
        upstream.mkdir()
        g(upstream, "init", "-b", "main")
        (upstream / "base.txt").write_text("a\n")
        g(upstream, "add", "-A")
        g(upstream, "commit", "-m", "A")
        g(upstream, "branch", "agent/issue-7")  # zero-commit stale branch
        local = tmp_path / "local"
        g(tmp_path, "clone", str(upstream), str(local))
        (upstream / "new.txt").write_text("b\n")
        g(upstream, "add", "-A")
        g(upstream, "commit", "-m", "B")

        monkeypatch.chdir(local)
        checkout = _materialize_checkout("7", "agent/issue-7", DispatchConfig())
        try:
            # Reset onto current base: upstream's tip (B) is present, and the
            # branch is NOT wedged at the stale A.
            assert (checkout / "new.txt").exists()
        finally:
            import shutil as _rm

            _rm.rmtree(checkout, ignore_errors=True)

    def test_branch_with_ahead_commits_is_reused_unchanged(self, tmp_path, monkeypatch):
        """#743: an existing remote agent branch carrying real work (commits
        ahead of base) keeps today's reuse behavior — it is checked out as-is,
        NOT reset onto the base."""
        import shutil as _shutil
        import subprocess as _sp

        from kew.dispatch_core import _materialize_checkout
        from kew.subprocess_utils import TOOL_PATHS

        git = _shutil.which("git")
        assert git is not None
        monkeypatch.setitem(TOOL_PATHS, "git", git)

        def g(cwd: Path, *args: str) -> None:
            _sp.run(
                [git, "-c", "user.name=t", "-c", "user.email=t@t", *args],
                cwd=cwd,
                check=True,
                capture_output=True,
            )

        # Upstream at A; a prior attempt pushed agent/issue-8 with its own
        # commit W (work.txt); upstream main then advanced to B (new.txt).
        upstream = tmp_path / "upstream"
        upstream.mkdir()
        g(upstream, "init", "-b", "main")
        (upstream / "base.txt").write_text("a\n")
        g(upstream, "add", "-A")
        g(upstream, "commit", "-m", "A")
        g(upstream, "checkout", "-b", "agent/issue-8")
        (upstream / "work.txt").write_text("w\n")
        g(upstream, "add", "-A")
        g(upstream, "commit", "-m", "W")
        g(upstream, "checkout", "main")
        (upstream / "new.txt").write_text("b\n")
        g(upstream, "add", "-A")
        g(upstream, "commit", "-m", "B")
        local = tmp_path / "local"
        g(tmp_path, "clone", str(upstream), str(local))

        monkeypatch.chdir(local)
        checkout = _materialize_checkout("8", "agent/issue-8", DispatchConfig())
        try:
            # Reused as-is: the prior attempt's work is present, and it was NOT
            # reset onto base (so the base-only new.txt is absent).
            assert (checkout / "work.txt").exists()
            assert not (checkout / "new.txt").exists()
        finally:
            import shutil as _rm

            _rm.rmtree(checkout, ignore_errors=True)


class TestSandboxLiveTelemetry:
    """#SFD-155: contained runs must surface live token/cost telemetry while
    running, not false zeros until completion. The host already tails the
    container stream for the per-run log — parse usage from stream-json
    assistant events incrementally and update the run row periodically."""

    @staticmethod
    def _pending_row(store, run_id="live-1"):
        from datetime import UTC, datetime

        from kew.run_store import RunRecord

        store.save(
            RunRecord(
                run_id=run_id,
                issue_num=7,
                branch="agent/issue-7",
                status="pending",
                exit_code=-1,
                started_at=datetime.now(UTC),
                model="claude-sonnet-4-6",
            )
        )
        return run_id

    @staticmethod
    def _claude_parser():
        from kew.runners import get_runner

        return get_runner("claude-code")

    def test_accumulate_stream_line_reads_assistant_usage(self):
        from kew.dispatch_core import _accumulate_stream_line, _SandboxTelemetry

        live = _SandboxTelemetry()
        parser = self._claude_parser()
        # The init event is where a claude run names its model (#1147: the
        # runner's own parser reports it, rather than the sandbox re-reading
        # the assistant message's `model` field itself).
        assert (
            _accumulate_stream_line(
                live,
                json.dumps(
                    {
                        "type": "system",
                        "subtype": "init",
                        "model": "claude-sonnet-4-6",
                    }
                ),
                parser,
            )
            is False
        )
        line = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "model": "claude-sonnet-4-6",
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 40,
                        "cache_creation_input_tokens": 10,
                        "cache_read_input_tokens": 5,
                    },
                },
            }
        )
        assert _accumulate_stream_line(live, line, parser) is True
        assert live.input_tokens == 100
        assert live.output_tokens == 40
        assert live.cache_creation_tokens == 10
        assert live.cache_read_tokens == 5
        assert live.reported_model == "claude-sonnet-4-6"

        # A second assistant turn accumulates on top of the first.
        assert _accumulate_stream_line(live, line, parser) is True
        assert live.input_tokens == 200
        assert live.output_tokens == 80

    def test_accumulate_stream_line_ignores_non_usage_lines(self):
        from kew.dispatch_core import _accumulate_stream_line, _SandboxTelemetry

        live = _SandboxTelemetry()
        parser = self._claude_parser()
        for line in (
            "not json at all",
            json.dumps({"type": "system", "subtype": "init"}),
            json.dumps({"type": "assistant", "message": {"content": []}}),
            json.dumps(["not", "a", "dict"]),
        ):
            assert _accumulate_stream_line(live, line, parser) is False
        assert live.input_tokens == 0
        assert live.output_tokens == 0

    def test_stream_sandbox_log_updates_row_before_completion(self, tmp_path):
        from kew.dispatch_core import _stream_sandbox_log
        from kew.run_store import SqliteRunStore

        log_file = tmp_path / "sandbox-7.log"
        lines = [
            # A real claude init event names the run's model; that is where the
            # live row now learns it (#1147), rather than from a per-turn
            # assistant message — which on a sub-agent turn names the SUB-agent.
            json.dumps(
                {"type": "system", "subtype": "init", "model": "claude-sonnet-4-6"}
            )
            + "\n",
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "model": "claude-sonnet-4-6",
                        "usage": {"input_tokens": 1000, "output_tokens": 250},
                    },
                }
            )
            + "\n",
        ]
        handle = MagicMock()
        handle.__iter__ = MagicMock(return_value=iter(lines))
        handle.wait.return_value = 0

        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            run_id = self._pending_row(store)
            # Monotonic clock advances well past the flush interval each call so
            # the first usage-bearing line flushes immediately.
            clock = iter([0.0, 100.0, 200.0, 300.0, 400.0])
            exit_code = _stream_sandbox_log(
                handle,
                log_file,
                store,
                run_id,
                model="claude-sonnet-4-6",
                overrides={},
                runner="claude-code",
                _now=lambda: next(clock),
            )
            assert exit_code == 0
            # _stream_sandbox_log does NOT finalize — a non-zero row here proves
            # the update landed mid-stream, not at completion.
            stored = store.get(run_id)
            assert stored.status == "pending"
            assert stored.input_tokens == 1000
            assert stored.output_tokens == 250
            assert stored.total_tokens == 1250
            assert stored.estimated_cost_usd > 0.0
            assert stored.reported_model == "claude-sonnet-4-6"

        # I06T: the per-run log is SANITIZED — the streamed provider lines are
        # derived and dropped, never written back out.
        written = log_file.read_text(encoding="utf-8")
        assert "input_tokens" not in written
        assert '"type"' not in written
        assert written.splitlines()[0].startswith("# kew sanitized dispatch log")
        assert "step 1 tokens=1250" in written
        # The facts survive the bytes: the sidecar carries the same totals.
        evidence = read_derived_evidence(log_file)
        assert evidence is not None
        assert evidence.steps == 1
        assert evidence.totals.input_tokens == 1000
        assert evidence.totals.output_tokens == 250
        assert evidence.totals.reported_model == "claude-sonnet-4-6"

    def test_stream_sandbox_log_tolerates_missing_store(self, tmp_path):
        from kew.dispatch_core import _stream_sandbox_log

        log_file = tmp_path / "sandbox-7.log"
        handle = MagicMock()
        handle.__iter__ = MagicMock(
            return_value=iter([json.dumps({"type": "assistant"}) + "\n"])
        )
        handle.wait.return_value = 0
        exit_code = _stream_sandbox_log(
            handle,
            log_file,
            None,
            "live-1",
            model="",
            overrides=None,
            runner="claude-code",
        )
        assert exit_code == 0


def test_host_worktree_interrupt_still_attests_posture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kew import dispatch_core

    interrupt = KeyboardInterrupt()
    emitted: list[Path | None] = []
    monkeypatch.setattr(
        dispatch_core,
        "setup_worktree",
        MagicMock(side_effect=interrupt),
    )
    monkeypatch.setattr(
        dispatch_core,
        "_emit_host_setup_posture",
        lambda _ctx, _runner, _decision, checkout: emitted.append(checkout),
    )

    with pytest.raises(KeyboardInterrupt) as raised:
        dispatch_core._setup_host_worktree(
            MagicMock(epic_ref=None, store=None),
            DispatchConfig(),
            "claude-code",
            MagicMock(),
        )

    assert raised.value is interrupt
    assert emitted == [None]


@pytest.mark.usefixtures("_canonical_existing_pr_published")
class TestSandboxedDispatch:
    """S4: sandbox.enabled path materializes checkout, brokers push+PR from host."""

    def test_sandboxed_dispatch_brokers_push_and_pr(self, tmp_path):
        """Container success routes publication through the canonical broker."""
        from kew.config import SandboxConfig
        from kew.dispatch_core import _run_sandboxed

        config = DispatchConfig(sandbox=SandboxConfig(enabled=True))
        config.project.repo = "o/r"
        checkout_dir = tmp_path / "checkout"
        checkout_dir.mkdir()

        fake_handle = MagicMock()
        fake_handle.__iter__ = MagicMock(return_value=iter([]))
        fake_handle.wait.return_value = 0

        run_calls: list[list[str]] = []

        def _fake_run(cmd, **kw):
            run_calls.append(list(cmd))
            result = MagicMock()
            result.stdout = (
                '{"number":42,"html_url":"https://github.com/o/r/pull/42"}'
                if cmd[:4] == ["gh", "api", "--method", "POST"]
                and cmd[4].endswith("/pulls")
                else ""
            )
            result.returncode = 0
            return result

        with (
            patch(f"{_P}._detect_sandbox_runtime", return_value="/usr/bin/docker"),
            patch(
                f"{_P}._build_sandbox_argv",
                return_value=["docker", "run", "--rm", "--name", "kew-42", "kew-agent"],
            ) as mock_build,
            patch(f"{_P}._launch_container", return_value=fake_handle) as mock_launch,
            patch(f"{_P}._git_push_branch", return_value=MagicMock()) as publish,
            patch(f"{_P}.run", side_effect=_fake_run),
        ):
            from kew.run_store import SqliteRunStore

            _store = SqliteRunStore(":memory:")
            result = _run_sandboxed(
                prompt="prompt text",
                checkout_dir=checkout_dir,
                config=config,
                store=_store,
                image=SANDBOX_IMAGE,
                runner=SANDBOX_RUNNER,
                issue_num=42,
                item_id="42",
                branch="agent/issue-42",
                title="Fix the widget",
                publication_base_sha="a" * 40,
            )

        assert result.exit_code == 0
        mock_launch.assert_called_once()
        # argv[0] must be the BARE allowlisted runtime name even though the
        # probe returned a path (2026-07-16 incident, second resolution site).
        assert mock_build.call_args.kwargs["runtime"] == "docker"

        assert publish.call_args.kwargs["publish"] == "canonical"
        assert publish.call_args.kwargs["base_sha"] == "a" * 40
        assert publish.call_args.kwargs["runtime"] == "docker"
        assert publish.call_args.kwargs["image"] == "sha256:" + "f" * 64
        assert not any(c[:2] == ["git", "push"] for c in run_calls)

        pr_calls = [c for c in run_calls if len(c) >= 5 and c[4].endswith("/pulls")]
        assert pr_calls, "host must create the PR through GitHub REST"

        # run_agent must NOT be invoked
        assert not any(c[:1] == ["claude"] for c in run_calls), (
            "agent-side push must not happen"
        )

    def test_sandboxed_dispatch_template_omits_agent_push(self):
        """build_prompt(sandboxed=True) omits git push / gh pr create instructions."""
        from kew.prompt import build_prompt

        config = DispatchConfig()
        rendered = build_prompt(
            issue_num=1,
            issue_title="Test",
            issue_body="Body",
            complexity="simple",
            config=config,
            has_coderabbit=False,
            sandboxed=True,
        )

        assert "git push" not in rendered, "sandboxed prompt must omit git push"
        assert "gh pr create" not in rendered, "sandboxed prompt must omit gh pr create"
        assert "git commit" in rendered, (
            "sandboxed prompt must still mention git commit"
        )

    def test_sandboxed_dispatch_template_documents_gh_absence(self):
        """#762: sandboxed prompt tells the agent gh is deliberately absent so
        it skips gh-dependent tests instead of trying to install gh into /work."""
        from kew.prompt import build_prompt

        rendered = build_prompt(
            issue_num=1,
            issue_title="Test",
            issue_body="Body",
            complexity="simple",
            config=DispatchConfig(),
            has_coderabbit=False,
            sandboxed=True,
        )
        assert "deliberately absent" in rendered
        assert "uv" in rendered  # preinstalled tooling is called out

    def test_host_dispatch_template_omits_container_tooling_note(self):
        """The gh/tooling note is sandbox-only — a host run must not carry it."""
        from kew.prompt import build_prompt

        rendered = build_prompt(
            issue_num=1,
            issue_title="Test",
            issue_body="Body",
            complexity="simple",
            config=DispatchConfig(),
            has_coderabbit=False,
            sandboxed=False,
        )
        assert "deliberately absent" not in rendered

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.cleanup_worktree")
    @patch(f"{_P}.check_pr_exists", return_value=99)
    @patch(f"{_P}.update_pr_labels", return_value=True)
    @patch(f"{_P}.mark_completed")
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    @patch(f"{_P}.setup_worktree", return_value=Path("/tmp/wt"))
    @patch(f"{_P}.resolve_runner")
    def test_host_path_unchanged_when_sandbox_disabled(
        self,
        mock_resolve_runner: MagicMock,
        mock_worktree: MagicMock,
        mock_prompt: MagicMock,
        mock_run_agent: MagicMock,
        mock_completed: MagicMock,
        mock_pr_labels: MagicMock,
        mock_pr_exists: MagicMock,
        mock_cleanup: MagicMock,
        mock_connectivity: MagicMock,
    ):
        """An explicit opt-out takes the host path; no container is launched.

        `enabled = False` alone no longer reaches here — it is uncontained
        execution and refuses without `dangerously_skip_containment`, so the
        test declares both rather than letting a fixture supply the second.
        """
        from kew.config import SandboxConfig
        from kew.dispatch_core import execute_dispatch

        config = DispatchConfig(
            sandbox=SandboxConfig(enabled=False, dangerously_skip_containment=True)
        )
        assert config.sandbox.enabled is False

        mock_run_agent.return_value = _ok_result()

        with patch(f"{_P}._launch_container") as mock_launch:
            rc = execute_dispatch(
                allow_unrecorded=True, issue=_make_issue(), config=config
            )

        assert rc.exit_code == 0
        mock_run_agent.assert_called_once()
        mock_launch.assert_not_called()

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.cleanup_worktree")
    @patch(f"{_P}.check_pr_exists", return_value=99)
    @patch(f"{_P}.update_pr_labels", return_value=True)
    @patch(f"{_P}.mark_completed")
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    @patch(f"{_P}.setup_worktree", return_value=Path("/tmp/wt"))
    @patch(f"{_P}.resolve_runner")
    def test_explicit_skip_takes_the_host_path_and_attests_uncontained(
        self,
        mock_resolve_runner: MagicMock,
        mock_worktree: MagicMock,
        mock_prompt: MagicMock,
        mock_run_agent: MagicMock,
        mock_completed: MagicMock,
        mock_pr_labels: MagicMock,
        mock_pr_exists: MagicMock,
        mock_cleanup: MagicMock,
        mock_connectivity: MagicMock,
        tmp_path,
    ):
        """An opted-out dispatch runs on the host and SAYS SO on the chain.

        The ledger token is the point. `guard-only` and `disabled` both used to
        mean "no container", so a query could not separate a machine that
        quietly lacked Docker from an operator who accepted the risk. Now one
        token answers "which runs had no boundary", and it only ever appears
        when someone asked for it.
        """
        from kew.config import SandboxConfig
        from kew.dispatch_core import execute_dispatch
        from kew.run_store import SqliteRunStore

        # autouse conftest fixture already forces detect_runtime_cached -> None
        config = DispatchConfig(
            sandbox=SandboxConfig(enabled="auto", dangerously_skip_containment=True)
        )
        config.logging.dir = str(tmp_path / "logs")
        config.project.repo = "octo/widgets"
        store = SqliteRunStore(str(tmp_path / "runs.db"))
        checkout = tmp_path / "wt"
        hook = checkout / ".claude" / "hooks" / "security-guard.sh"
        hook.parent.mkdir(parents=True)
        trusted_hook = (
            Path(__file__).resolve().parents[2]
            / ".claude"
            / "hooks"
            / "security-guard.sh"
        )
        hook.write_bytes(trusted_hook.read_bytes())
        hook.chmod(0o755)
        (checkout / ".claude" / "settings.json").write_text(
            json.dumps(
                {
                    "hooks": {
                        "PreToolUse": [
                            {
                                "matcher": matcher,
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": ".claude/hooks/security-guard.sh",
                                    }
                                ],
                            }
                            for matcher in ("Bash", "Write", "Edit")
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )
        mock_worktree.return_value = checkout
        mock_resolve_runner.return_value.runner.name = "claude-code"
        mock_resolve_runner.return_value.model = "claude-sonnet-4-6"
        mock_run_agent.return_value = _ok_result()

        with patch(f"{_P}._launch_container") as mock_launch:
            rc = execute_dispatch(
                allow_unrecorded=True, issue=_make_issue(), config=config, store=store
            )

        assert rc.exit_code == 0
        mock_run_agent.assert_called_once()
        mock_launch.assert_not_called()

        events = store.list_audit_events(event="sandbox_resolved")
        assert len(events) == 1
        assert events[0].payload["posture"] == "uncontained"
        assert events[0].payload["enabled"] == "auto"
        assert events[0].payload["command_guard"] == "loaded"

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.cleanup_worktree")
    @patch(f"{_P}.check_pr_exists", return_value=99)
    @patch(f"{_P}.update_pr_labels", return_value=True)
    @patch(f"{_P}.mark_completed")
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.build_prompt", return_value="prompt")
    @patch(f"{_P}.setup_worktree", return_value=Path("/tmp/wt"))
    @patch(f"{_P}.resolve_runner")
    def test_codex_with_runtime_uses_host_path_and_attests_native_posture(
        self,
        mock_resolve_runner: MagicMock,
        mock_worktree: MagicMock,
        mock_prompt: MagicMock,
        mock_run_agent: MagicMock,
        mock_completed: MagicMock,
        mock_pr_labels: MagicMock,
        mock_pr_exists: MagicMock,
        mock_cleanup: MagicMock,
        mock_connectivity: MagicMock,
        tmp_path,
    ):
        from kew.config import SandboxConfig
        from kew.dispatch_core import execute_dispatch
        from kew.run_store import SqliteRunStore

        config = DispatchConfig(sandbox=SandboxConfig(enabled="auto"))
        config.logging.dir = str(tmp_path / "logs")
        config.project.repo = "octo/widgets"
        store = SqliteRunStore(str(tmp_path / "runs.db"))
        mock_resolve_runner.return_value.runner.name = "codex"
        mock_resolve_runner.return_value.runner.publication_style = "host"
        mock_resolve_runner.return_value.model = ""
        mock_run_agent.return_value = _ok_result()

        with (
            patch.dict(
                os.environ,
                {
                    # /bin carries ps: admission proves group exclusivity with it.
                    "PATH": "/usr/bin:/bin",
                    "UNDECLARED_CREDENTIAL": "credential-value",
                    "KEW_ACTOR": "orchestrator-only",
                },
                clear=True,
            ),
            # Docker IS on this host, which is the point of the test name: the
            # host path is taken because the RUNNER holds the boundary, not
            # because nothing else could. Resolution does consult detection
            # before asking codex its posture — the answer decides whether kew
            # has a boundary for it to defer to at all (`_offered_runtime`) —
            # and on this runtime the declaration does not move either way.
            patch(
                "kew.sandbox.detect_runtime_cached", return_value="/usr/bin/docker"
            ) as detect,
            patch(f"{_P}._launch_container") as mock_launch,
        ):
            result = execute_dispatch(
                allow_unrecorded=True, issue=_make_issue(), config=config, store=store
            )

        assert result.exit_code == 0
        mock_run_agent.assert_called_once()
        mock_launch.assert_not_called()
        assert detect.call_args.args[0].runtime == "docker"

        events = store.list_audit_events(event="sandbox_resolved")
        assert len(events) == 1
        assert events[0].payload == {
            "attempt_number": 1,
            "posture_id": events[0].payload["posture_id"],
            "posture": "native",
            "runtime": None,
            "enabled": "auto",
            "runner": "codex",
            "image": None,
            "reason": None,
            "egress_verification": "unavailable",
            "filesystem_boundary": "runner-native-workspace-write",
            "network_boundary": "not-applicable",
            "credential_names": ["PATH", "UNDECLARED_CREDENTIAL"],
            "proxy_environment": "none",
            "proxy_variables": {"inherited": [], "blanked": []},
            "command_guard": "not-applicable",
            "admission": "policy",
            # Both keys on every posture, so one payload schema covers them
            # all — but a native runner launches no kew container, so there is
            # no cap to attest and the value is the None runtime and image
            # already carry here (#2360).
            "max_memory": None,
            "max_cpus": None,
        }

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.mark_failed")
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.setup_worktree", return_value=Path("/tmp/wt"))
    @patch(f"{_P}.resolve_runner")
    def test_true_without_runtime_refuses_to_dispatch(
        self,
        mock_resolve_runner: MagicMock,
        mock_worktree: MagicMock,
        mock_run_agent: MagicMock,
        mock_failed: MagicMock,
        mock_connectivity: MagicMock,
    ):
        """enabled=true + no runtime: fail closed — run_agent never invoked."""
        from kew.config import SandboxConfig
        from kew.dispatch_core import execute_dispatch

        # autouse conftest fixture forces detect_runtime_cached -> None
        config = DispatchConfig(sandbox=SandboxConfig(enabled=True))

        rc = execute_dispatch(allow_unrecorded=True, issue=_make_issue(), config=config)

        assert rc.exit_code != 0
        mock_run_agent.assert_not_called()
        mock_failed.assert_called_once()

    def test_auto_with_runtime_takes_sandboxed_path_and_records_posture(
        self, tmp_path, capsys
    ):
        """auto + runtime present: sandboxed path taken, sandboxed posture logged."""
        from kew.config import SandboxConfig
        from kew.dispatch_core import execute_dispatch
        from kew.run_store import SqliteRunStore

        config = DispatchConfig(sandbox=SandboxConfig(enabled="auto"))
        store = SqliteRunStore(str(tmp_path / "runs.db"))

        with (
            patch("kew.sandbox.detect_runtime_cached", return_value="/usr/bin/docker"),
            # Runtime alone no longer suffices (#732): sandboxed posture also
            # requires an agent credential in the env.
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}),
            patch("kew.sandbox.allowlist_network_exists", return_value=True),
            patch(f"{_P}.resolve_runner") as mock_resolve_runner,
            patch(f"{_P}._materialize_checkout", return_value=tmp_path / "co"),
            patch(f"{_P}.build_prompt", return_value="prompt"),
            patch(f"{_P}._run_sandboxed", return_value=_ok_result()) as mock_sb,
            patch(f"{_P}.check_pr_exists", return_value=99),
            patch(f"{_P}.update_pr_labels", return_value=True),
            patch(f"{_P}.mark_completed"),
            patch(f"{_P}._get_changed_files", return_value=[]),
            patch(f"{_P}._cleanup_dispatch_dir"),
            patch(f"{_P}._check_github_connectivity", return_value=True),
        ):
            mock_resolve_runner.return_value.runner.name = "claude-code"
            mock_resolve_runner.return_value.model = "claude-sonnet-4-6"
            rc = execute_dispatch(
                allow_unrecorded=True, issue=_make_issue(), config=config, store=store
            )

        assert rc.exit_code == 0
        mock_sb.assert_called_once()
        # The container runs the image the RUNNER resolved, not whatever
        # config.sandbox.image happens to say (#1144).
        assert mock_sb.call_args.kwargs["image"] == SANDBOX_IMAGE

        events = store.list_audit_events(event="sandbox_resolved")
        assert len(events) == 1
        assert events[0].payload["posture"] == "sandboxed"
        # Bare allowlisted name in the attested payload, never the which()
        # path (the 2026-07-16 argv/allowlist incident).
        assert events[0].payload["runtime"] == "docker"
        # The ledger names the runner and the image it was contained in — the
        # attested facts a mixed-runner epic needs to audit after the fact.
        assert events[0].payload["runner"] == "claude-code"
        assert events[0].payload["image"] == SANDBOX_IMAGE
        assert (
            events[0].payload.get("egress_verification")
            == "operator-managed-unverified"
        )
        assert (
            "Egress verification: operator-managed-unverified"
            in capsys.readouterr().err
        )

    def test_sandbox_attestation_names_the_complete_boundary_without_values(
        self, tmp_path: Path
    ) -> None:
        """The recorded posture is complete, run-bound, and names-only."""
        from kew.dispatch_core import _DispatchContext, _emit_sandbox_resolved
        from kew.run_store import SqliteRunStore

        store = SqliteRunStore(str(tmp_path / "runs.db"))
        ctx = MagicMock(spec=_DispatchContext)
        ctx.checkout_generation = ""
        ctx.config = DispatchConfig()
        ctx.config.project.repo = "octo/widgets"
        ctx.store = store
        ctx.run_id = "run-posture-42"
        ctx.issue_num = 42
        ctx.item_id = "42"
        secret = "sk-ant-value-must-not-be-recorded"

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": secret}, clear=True):
            _emit_sandbox_resolved(
                ctx,
                "claude-code",
                posture="sandboxed",
                runtime="docker",
                image="kew-agent:latest",
                egress_verification="sidecar-certified",
                egress_route="sidecar",
            )

        event = store.list_audit_events(event="sandbox_resolved")[0]
        assert event.run_id == "run-posture-42"
        assert event.payload == {
            "attempt_number": 1,
            "posture_id": event.payload["posture_id"],
            "posture": "sandboxed",
            "runtime": "docker",
            "enabled": "auto",
            "runner": "claude-code",
            "image": "kew-agent:latest",
            "reason": None,
            "egress_verification": "sidecar-certified",
            "egress_verification_basis": "sidecar-admission",
            "filesystem_boundary": "container-mount",
            "network_boundary": "sidecar-certified",
            "credential_names": ["ANTHROPIC_API_KEY"],
            "proxy_environment": "none",
            "proxy_variables": {
                "inherited": [],
                "blanked": [
                    "HTTP_PROXY",
                    "HTTPS_PROXY",
                    "NO_PROXY",
                    "http_proxy",
                    "https_proxy",
                    "no_proxy",
                    "FTP_PROXY",
                    "ALL_PROXY",
                    "ftp_proxy",
                    "all_proxy",
                ],
            },
            "command_guard": "absent",
            "admission": "policy",
            "max_memory": "8g",
            "max_cpus": 4,
        }
        assert secret not in json.dumps(event.payload, sort_keys=True)

    def test_unmapped_runner_is_never_dispatched_into_the_claude_image(self, tmp_path):
        """The #1104 incident, pinned at the dispatch layer (#1144).

        Driver env carries CLAUDE_CODE_OAUTH_TOKEN for sibling claude nodes; a
        routed opencode node must NOT ride that credential into the claude-only
        image. With no [sandbox] images.opencode and a runtime present, the
        dispatch is REFUSED — not quietly handed to the uncontained host path,
        which is the posture the committed kew.toml's `simple` tier would
        otherwise have silently acquired. The refusal is attested.
        """
        from kew.config import PricingConfig, SandboxConfig
        from kew.dispatch_core import execute_dispatch
        from kew.run_store import SqliteRunStore

        config = DispatchConfig(
            sandbox=SandboxConfig(enabled="auto"),
            pricing=PricingConfig(
                overrides={"openrouter/deepseek-v4-flash": [0.1, 0.2]}
            ),
        )
        store = SqliteRunStore(str(tmp_path / "runs.db"))

        with (
            patch("kew.sandbox.detect_runtime_cached", return_value="/usr/bin/docker"),
            patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "tok-sibling-node"}),
            patch(f"{_P}.resolve_runner") as mock_resolve_runner,
            patch(f"{_P}._materialize_checkout", return_value=tmp_path / "co"),
            patch(f"{_P}.build_prompt", return_value="prompt"),
            patch(f"{_P}._run_sandboxed", return_value=_ok_result()) as mock_sb,
            patch(f"{_P}.setup_worktree", return_value=tmp_path / "wt"),
            patch(f"{_P}._launch_host", return_value=_ok_result()) as mock_host,
            patch(f"{_P}.check_pr_exists", return_value=99),
            patch(f"{_P}.update_pr_labels", return_value=True),
            patch(f"{_P}.mark_completed"),
            patch(f"{_P}._get_changed_files", return_value=[]),
            patch(f"{_P}._cleanup_dispatch_dir"),
            patch(f"{_P}._check_github_connectivity", return_value=True),
            patch(f"{_P}.mark_failed") as mock_failed,
        ):
            mock_resolve_runner.return_value.runner.name = "opencode"
            mock_resolve_runner.return_value.model = "openrouter/deepseek-v4-flash"
            result = execute_dispatch(
                allow_unrecorded=True, issue=_make_issue(), config=config, store=store
            )

        # Neither containment path ran: not the claude image, and not the host.
        assert result.exit_code != 0
        mock_sb.assert_not_called()
        mock_host.assert_not_called()
        # A clean refusal on the classic queue, named as containment — never
        # the generic "internal error during dispatch" of the catch-all path.
        assert "containment refused" in mock_failed.call_args.args[2]
        events = store.list_audit_events(event="sandbox_resolved")
        assert len(events) == 1
        assert events[0].payload["posture"] == "refused"
        assert events[0].payload["runner"] == "opencode"
        assert events[0].payload["image"] is None
        # The refusal attests WHY, and that a runtime WAS present — the
        # opposite of the no-runtime refusal it would otherwise be
        # byte-identical to.
        assert events[0].payload["reason"] == "image_unmapped"
        assert events[0].payload["runtime"] == "docker"
        assert events[0].payload["filesystem_boundary"] == "not-applicable"

    def test_open_egress_warning_fires_without_a_resolved_image(self, tmp_path, capsys):
        """#935's credential-exfiltration warning is keyed on containment, not
        on the image.

        The warning says "you opted into open egress with a credential in the
        env" — an image fact has nothing to do with it, so a sandboxed decision
        that (for any reason) carries no image must still emit it. Folding the
        warning behind the image made a security warning silently optional.
        """
        from kew.config import SandboxConfig
        from kew.dispatch_core import _DispatchContext, _resolve_and_apply_sandbox
        from kew.sandbox import SandboxDecision

        config = DispatchConfig(sandbox=SandboxConfig(enabled="auto", network="open"))
        ctx = MagicMock(spec=_DispatchContext)
        ctx.checkout_generation = ""
        ctx.config = config
        ctx.store = None
        ctx.issue_num = 42
        ctx.item_id = "42"

        with (
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}),
            patch(
                f"{_P}.resolve_effective_sandbox",
                return_value=SandboxDecision(
                    sandboxed=True,
                    runtime="docker",
                    posture="sandboxed",
                    notice=None,
                    image=None,
                ),
            ),
        ):
            _resolve_and_apply_sandbox(ctx, "claude-code")

        assert "egress" in capsys.readouterr().err.lower()


def test_existing_continuation_refuses_a_required_sandbox_posture():
    from kew.dispatch_core import _DispatchContext, _resolve_and_apply_sandbox
    from kew.sandbox import SandboxDecision, SandboxRuntimeUnavailable

    config = DispatchConfig()
    ctx = MagicMock(spec=_DispatchContext)
    ctx.checkout_generation = ""
    ctx.config = config
    ctx.store = None
    ctx.issue_num = 42
    ctx.item_id = "42"
    decision = SandboxDecision(
        sandboxed=True,
        runtime="docker",
        posture="sandboxed",
        notice=None,
        image="kew-agent",
        egress_verification="operator-managed-unverified",
    )

    with (
        patch(f"{_P}.resolve_effective_sandbox", return_value=decision),
        patch(
            f"{_P}._resolve_egress_verification",
            return_value="operator-managed-unverified",
        ),
        patch(f"{_P}._emit_sandbox_resolved") as emit,
        patch(f"{_P}._warn"),
        pytest.raises(SandboxRuntimeUnavailable) as raised,
    ):
        _resolve_and_apply_sandbox(ctx, "claude-code", host_only=True)

    assert raised.value.reason == "continuation_host_only"
    assert raised.value.runtime == "docker"
    assert raised.value.image == "kew-agent"
    emit.assert_called_once()
    assert emit.call_args.kwargs["posture"] == "refused"
    assert emit.call_args.kwargs["runtime"] == "docker"
    assert emit.call_args.kwargs["image"] == "kew-agent"
    assert emit.call_args.kwargs["egress_verification"] == "unavailable"


def test_a_continuation_carries_its_shape_into_the_containment_resolution():
    """The flag reaches the RESOLVER, not only the guard that runs after it.

    `_resolve_and_apply_sandbox` is the one caller that knows a dispatch is a
    continuation, so teaching `kew.sandbox` about the shape while this seam
    keeps passing the dispatch default changes nothing: a runner holding its own
    boundary would still defer to a container this launch cannot have and still
    die at `_ensure_host_only_sandbox_posture` (PR #2461 review round 3).

    Pinned on both entry points, because a continuation whose route is a sidecar
    takes the other one.
    """
    from kew.dispatch_core import _DispatchContext, _resolve_and_apply_sandbox
    from kew.sandbox import SandboxDecision

    native = SandboxDecision(
        sandboxed=False, runtime=None, posture="native", notice=None, image=None
    )
    ctx = MagicMock(spec=_DispatchContext)
    ctx.config = DispatchConfig()
    ctx.store = None
    sidecar_transport = SimpleNamespace(
        sidecar=True, mode="sidecar", worker_credential_env_name="KEW_WORKER_KEY"
    )

    for seam, transport in (
        ("resolve_effective_sandbox", None),
        ("resolve_effective_sidecar_sandbox", sidecar_transport),
    ):
        for host_only in (True, False):
            with (
                patch(f"{_P}.{seam}", return_value=native) as resolve,
                patch(f"{_P}._emit_sandbox_resolved"),
            ):
                _resolve_and_apply_sandbox(
                    ctx,
                    "codex",
                    host_only=host_only,
                    inference_transport=transport,
                )
            assert resolve.call_args.kwargs["host_only"] is host_only, seam


def test_sidecar_egress_is_unavailable_before_packet_admission() -> None:
    from kew.dispatch_core import _resolve_egress_verification
    from kew.sandbox import SandboxDecision

    decision = SandboxDecision(
        sandboxed=True,
        runtime="docker",
        posture="sandboxed",
        notice=None,
        image="kew-agent",
    )
    transport = MagicMock(sidecar=True)

    with patch("kew.dispatch_core.sandbox_network.allowlist_network_exists") as probe:
        verification = _resolve_egress_verification(decision, transport)

    assert verification == "unavailable"
    probe.assert_not_called()


def test_sidecar_sandbox_attestation_follows_ready_callback(tmp_path: Path) -> None:
    from kew.dispatch_core import _EarlyRunInfo, _launch_sandboxed
    from kew.sandbox import SandboxDecision

    ctx = MagicMock()
    ctx.config = DispatchConfig()
    ctx.issue_num = 42
    ctx.item_id = "42"
    ctx.provider = "github"
    ctx.issue_title = "title"
    ctx.issue_body = "body"
    ctx.complexity = "medium"
    ctx.merge_context = None
    ctx.branch = "agent/issue-42"
    ctx.store = MagicMock()
    ctx.run_id = "run-42"
    ctx.publication_base_sha = "a" * 40
    ctx.epic_ref = None
    ctx.allow_unrecorded = True
    early = _EarlyRunInfo(runner="claude-code", model="claude-sonnet-4-6")
    decision = SandboxDecision(
        sandboxed=True,
        runtime="docker",
        posture="sandboxed",
        notice=None,
        image="kew-agent",
        egress_verification="unavailable",
    )
    transport = MagicMock(sidecar=True)

    def run_after_packet_gate(**kwargs: Any) -> DispatchResult:
        assert emit.call_count == 1
        assert emit.call_args.kwargs["egress_verification"] == "unavailable"
        kwargs["sidecar_ready"]()
        assert emit.call_count == 2
        return _ok_result()

    with (
        patch(f"{_P}._build_repo_map", return_value=""),
        patch(f"{_P}.build_prompt", return_value="prompt"),
        patch(f"{_P}._run_sandboxed", side_effect=run_after_packet_gate),
        patch(f"{_P}._emit_sandbox_resolved") as emit,
    ):
        result = _launch_sandboxed(
            ctx,
            tmp_path,
            early,
            None,
            image="kew-agent",
            sandbox_decision=decision,
            inference_transport=transport,
        )

    assert result.exit_code == 0
    assert emit.call_args.kwargs["egress_verification"] == "sidecar-certified"


def test_sidecar_packet_refusal_never_attests_certified_egress(
    tmp_path: Path,
) -> None:
    from kew.dispatch_core import _EarlyRunInfo, _launch_sandboxed
    from kew.sandbox import SandboxDecision
    from kew.switchyard_supervisor import SwitchyardSupervisorError

    ctx = MagicMock()
    ctx.config = DispatchConfig()
    ctx.issue_num = 42
    ctx.item_id = "42"
    ctx.provider = "github"
    ctx.issue_title = "title"
    ctx.issue_body = "body"
    ctx.complexity = "medium"
    ctx.merge_context = None
    ctx.branch = "agent/issue-42"
    ctx.store = MagicMock()
    ctx.run_id = "run-42"
    ctx.publication_base_sha = "a" * 40
    ctx.epic_ref = None
    ctx.allow_unrecorded = True
    early = _EarlyRunInfo(runner="claude-code", model="claude-sonnet-4-6")
    decision = SandboxDecision(
        sandboxed=True,
        runtime="docker",
        posture="sandboxed",
        notice=None,
        image="kew-agent",
        egress_verification="unavailable",
    )
    transport = MagicMock(sidecar=True)

    with (
        patch(f"{_P}._build_repo_map", return_value=""),
        patch(f"{_P}.build_prompt", return_value="prompt"),
        patch(
            f"{_P}._run_sandboxed",
            side_effect=SwitchyardSupervisorError(
                "sidecar packet evidence is unavailable"
            ),
        ),
        patch(f"{_P}._emit_sandbox_resolved") as emit,
        pytest.raises(SwitchyardSupervisorError, match="packet evidence"),
    ):
        _launch_sandboxed(
            ctx,
            tmp_path,
            early,
            None,
            image="kew-agent",
            sandbox_decision=decision,
            inference_transport=transport,
        )

    emit.assert_called_once()
    assert emit.call_args.kwargs["egress_verification"] == "unavailable"


class TestContainmentRefusalReleasesTheSlot:
    """#1144 review: a containment refusal must not park a fleet worker slot.

    The refusal reaches dispatch AFTER the driver reserved a slot for the node.
    Falling through to the generic mid-dispatch handler writes no run row, so
    the reservation dangles for the driver's full reconcile grace at $0 — on a
    config-level cause that is every affected node, every tick.
    """

    def _refuse(self, store, *, epic_ref: str | None, tmp_path):
        from kew.config import PricingConfig, SandboxConfig
        from kew.dispatch_core import execute_dispatch

        config = DispatchConfig(
            sandbox=SandboxConfig(enabled="auto"),
            pricing=PricingConfig(
                overrides={"openrouter/deepseek-v4-flash": [0.1, 0.2]}
            ),
        )
        with (
            patch("kew.sandbox.detect_runtime_cached", return_value="/usr/bin/docker"),
            patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "tok-sibling-node"}),
            patch(f"{_P}.resolve_runner") as mock_resolve_runner,
            patch(f"{_P}.mark_failed") as mock_failed,
            patch(f"{_P}.run_agent") as mock_run_agent,
            patch(f"{_P}._check_github_connectivity", return_value=True),
        ):
            mock_resolve_runner.return_value.runner.name = "opencode"
            mock_resolve_runner.return_value.model = "openrouter/deepseek-v4-flash"
            rc = execute_dispatch(
                allow_unrecorded=True,
                issue=_make_issue(num=647),
                config=config,
                store=store,
                epic_ref=epic_ref,
            )
        return rc, mock_failed, mock_run_agent

    def test_epic_node_is_held_and_its_reservation_reconciled(self, tmp_path):
        store = _mock_store()
        rc, mock_failed, mock_run_agent = self._refuse(
            store, epic_ref="epic/containment-1", tmp_path=tmp_path
        )

        assert getattr(rc, "launched", None) is False
        assert rc.exit_code == 1
        mock_run_agent.assert_not_called()
        # Non-destructive, exactly like the budget hold: HELD, never relabelled.
        mock_failed.assert_not_called()
        store.hold_epic_node.assert_called_once()
        epic, item, reason = store.hold_epic_node.call_args[0]
        assert (epic, item) == ("epic/containment-1", "647")
        # The prefix the overlay routes to HELD, and a reason that names both
        # the knob to set and how to release the node afterwards.
        assert reason.startswith("sandbox_containment")
        assert "images.opencode" in reason
        assert "kew epic unhold" in reason

        # The terminal refusal row keyed to the reservation is what frees the
        # slot NOW instead of at grace expiry.
        store.save.assert_called_once()
        row = store.save.call_args[0][0]
        assert row.status == "refused"
        assert row.error_category == "containment_refused"
        assert row.item_id == "647"
        assert row.finished_at is not None

    def test_classic_queue_marks_failed_naming_containment(self, tmp_path):
        store = _mock_store()
        rc, mock_failed, mock_run_agent = self._refuse(
            store, epic_ref=None, tmp_path=tmp_path
        )

        assert getattr(rc, "launched", None) is False
        assert rc.exit_code == 1
        mock_run_agent.assert_not_called()
        # No epic reservation to reconcile off the classic queue — just a clean,
        # named failure instead of "internal error during dispatch".
        store.hold_epic_node.assert_not_called()
        store.save.assert_not_called()
        assert "containment refused" in mock_failed.call_args[0][2]

    def test_refusal_rolls_back_the_out_of_band_claim(self, tmp_path):
        """The Linear shape: the caller claimed the item before dispatching, so
        a refusal that skips ``on_refuse`` strands it In Progress with no agent.
        """
        from kew.config import PricingConfig, SandboxConfig
        from kew.dispatch_core import execute_dispatch

        config = DispatchConfig(
            sandbox=SandboxConfig(enabled="auto"),
            pricing=PricingConfig(
                overrides={"openrouter/deepseek-v4-flash": [0.1, 0.2]}
            ),
        )
        rollback = MagicMock()
        with (
            patch("kew.sandbox.detect_runtime_cached", return_value="/usr/bin/docker"),
            patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "tok-sibling-node"}),
            patch(f"{_P}.resolve_runner") as mock_resolve_runner,
            patch(f"{_P}.mark_failed"),
            patch(f"{_P}._check_github_connectivity", return_value=True),
        ):
            mock_resolve_runner.return_value.runner.name = "opencode"
            mock_resolve_runner.return_value.model = "openrouter/deepseek-v4-flash"
            execute_dispatch(
                allow_unrecorded=True,
                issue=_make_issue(num=647),
                config=config,
                store=_mock_store(),
                epic_ref="epic/containment-1",
                on_refuse=rollback,
            )

        rollback.assert_called_once()

    def _capacity_refusal(self, tmp_path, *, over_quota: bool):
        from kew.config import SandboxConfig
        from kew.dispatch_core import execute_dispatch

        config = DispatchConfig(sandbox=SandboxConfig(enabled="auto"))
        store = _mock_store()
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        disk_usage = MagicMock(free=(1 << 60) if over_quota else 0)
        checkout_usage = (CHECKOUT_QUOTA_BYTES + 1, 1) if over_quota else (0, 0)
        with (
            patch("kew.sandbox.detect_runtime_cached", return_value="/usr/bin/docker"),
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}),
            patch(f"{_P}.resolve_runner") as resolve,
            patch(f"{_P}.check_runtime_memory_capacity"),
            patch(f"{_P}._materialize_checkout", return_value=checkout) as materialize,
            patch("kew.sandbox.shutil.disk_usage", return_value=disk_usage),
            patch("kew.sandbox.checkout_usage", return_value=checkout_usage) as walk,
            patch(f"{_P}._run_sandboxed") as run_sandboxed,
            patch(f"{_P}._cleanup_dispatch_dir") as cleanup,
            patch(f"{_P}.mark_failed") as mark_failed,
            patch(f"{_P}._check_github_connectivity", return_value=True),
        ):
            resolve.return_value.runner.name = "claude-code"
            resolve.return_value.model = ""
            result = execute_dispatch(
                allow_unrecorded=True,
                issue=_make_issue(num=647),
                config=config,
                store=store,
                epic_ref="epic/capacity-1",
            )
        events = [
            call.kwargs
            for call in store.append_audit_event.call_args_list
            if call.kwargs.get("event") == "sandbox_resolved"
        ]
        return (
            result,
            store,
            events,
            materialize,
            walk,
            run_sandboxed,
            cleanup,
            mark_failed,
        )

    def test_host_free_space_refusal_is_attested_and_releases_the_slot(self, tmp_path):
        result, store, events, materialize, walk, run, cleanup, failed = (
            self._capacity_refusal(tmp_path, over_quota=False)
        )
        assert result.exit_code == 1
        assert len(events) == 1  # attested exactly once
        assert events[0]["payload"]["posture"] == "refused"
        assert events[0]["payload"]["reason"] == "host_free_space"
        assert store.save.call_args.args[0].status == "refused"
        materialize.assert_not_called()
        walk.assert_not_called()
        run.assert_not_called()
        cleanup.assert_not_called()
        failed.assert_not_called()

    def test_materialized_checkout_quota_refusal_is_attested_and_cleans_up(
        self, tmp_path
    ):
        result, store, events, materialize, walk, run, cleanup, failed = (
            self._capacity_refusal(tmp_path, over_quota=True)
        )
        assert result.exit_code == 1
        assert len(events) == 1  # attested exactly once
        assert events[0]["payload"]["posture"] == "refused"
        assert events[0]["payload"]["reason"] == "checkout_over_quota"
        assert store.save.call_args.args[0].status == "refused"
        materialize.assert_called_once()
        walk.assert_called_once()
        run.assert_not_called()
        cleanup.assert_called_once_with(
            tmp_path / "checkout", sandboxed=True, branch="agent/issue-647"
        )
        failed.assert_not_called()

    def test_unparseable_inherited_proxy_refusal_is_attested(
        self, tmp_path, monkeypatch
    ):
        """PR #1984 review round 4: the proxy refusal reaches the ledger as a
        refused posture with its own cause, and no sandboxed posture is recorded.
        """
        from kew.config import SandboxConfig
        from kew.dispatch_core import execute_dispatch
        from kew.sandbox import CONTROLLED_PROXY_NAMES

        for name in CONTROLLED_PROXY_NAMES:
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv(
            "HTTPS_PROXY", "http://alice:placeholder-pw-1984@proxy.corp:99999"
        )
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        config = DispatchConfig(
            sandbox=SandboxConfig(enabled="auto", proxy_environment="inherit")
        )
        store = _mock_store()
        with (
            patch("kew.sandbox.detect_runtime_cached", return_value="/usr/bin/docker"),
            patch(f"{_P}.resolve_runner") as resolve,
            patch(f"{_P}._materialize_checkout") as materialize,
            patch(f"{_P}._run_sandboxed") as run_sandboxed,
            patch(f"{_P}.mark_failed"),
            patch(f"{_P}._check_github_connectivity", return_value=True),
        ):
            resolve.return_value.runner.name = "claude-code"
            resolve.return_value.model = ""
            result = execute_dispatch(
                allow_unrecorded=True,
                issue=_make_issue(num=647),
                config=config,
                store=store,
                epic_ref="epic/proxy-1",
            )
        events = [
            call.kwargs
            for call in store.append_audit_event.call_args_list
            if call.kwargs.get("event") == "sandbox_resolved"
        ]
        assert result.exit_code == 1
        assert [event["payload"]["posture"] for event in events] == ["refused"]
        assert events[0]["payload"]["reason"] == "proxy_unparseable"
        assert events[0]["payload"]["runtime"] == "docker"
        assert events[0]["payload"]["image"]
        assert "placeholder-pw-1984" not in repr(store.mock_calls)
        assert store.save.call_args.args[0].status == "refused"
        materialize.assert_not_called()
        run_sandboxed.assert_not_called()

    def test_containment_hold_prefix_matches_the_overlay(self):
        """The hold reason is written here and matched there — a drift makes
        every containment hold derive back to TODO and re-dispatch forever."""
        from kew.dispatch_preflight import _CONTAINMENT_HOLD_PREFIX
        from kew.epic_holds import (
            CONTAINMENT_HOLD_PREFIX as _OVERLAY_PREFIX,
        )
        from kew.epic_holds import holds_node_out_of_frontier

        assert _CONTAINMENT_HOLD_PREFIX == _OVERLAY_PREFIX
        assert holds_node_out_of_frontier(f"{_CONTAINMENT_HOLD_PREFIX}: no image")


@pytest.mark.usefixtures("_canonical_existing_pr_published")
class TestBuildPromptSkillsPointer:
    """I1 regression: real dispatch call sites (not just --show-prompt) must
    pass has_repo_skills so the rendered prompt points dispatched agents at
    .claude/skills. Gated on the resolved runner being claude-code -- v1
    scope, per the template's 'load automatically' claim which only holds
    for Claude Code."""

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.cleanup_worktree")
    @patch(f"{_P}.check_pr_exists", return_value=99)
    @patch(f"{_P}.update_pr_labels", return_value=True)
    @patch(f"{_P}.mark_completed")
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.resolve_runner")
    def test_codex_dispatch_renders_host_publication_prompt(
        self,
        mock_resolve_runner: MagicMock,
        mock_run_agent: MagicMock,
        mock_completed: MagicMock,
        mock_pr_labels: MagicMock,
        mock_pr_exists: MagicMock,
        mock_cleanup: MagicMock,
        mock_connectivity: MagicMock,
        tmp_path,
    ):
        from kew.dispatch_core import execute_dispatch
        from kew.runners.codex import CodexRunner
        from kew.runners.registry import ResolvedRoute

        config = DispatchConfig()
        config.agent.runner = "codex"
        config.agent.model = "gpt-5.6-sol"
        mock_resolve_runner.return_value = ResolvedRoute(
            runner=CodexRunner(), model="gpt-5.6-sol"
        )
        mock_run_agent.return_value = _ok_result()

        with patch(f"{_P}.setup_worktree", return_value=tmp_path):
            result = execute_dispatch(
                allow_unrecorded=True, issue=_make_issue(), config=config
            )

        assert result.exit_code == 0
        prompt = mock_run_agent.call_args.kwargs["prompt"]
        assert "Only edit files and run verification" in prompt
        assert "git push -u" not in prompt
        assert "gh pr create" not in prompt

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.cleanup_worktree")
    @patch(f"{_P}.check_pr_exists", return_value=99)
    @patch(f"{_P}.update_pr_labels", return_value=True)
    @patch(f"{_P}.mark_completed")
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.resolve_runner")
    def test_worktree_path_includes_pointer_for_claude_code_with_skills_dir(
        self,
        mock_resolve_runner: MagicMock,
        mock_run_agent: MagicMock,
        mock_completed: MagicMock,
        mock_pr_labels: MagicMock,
        mock_pr_exists: MagicMock,
        mock_cleanup: MagicMock,
        mock_connectivity: MagicMock,
        tmp_path,
    ):
        from kew.dispatch_core import execute_dispatch

        (tmp_path / ".claude" / "skills").mkdir(parents=True)
        mock_resolve_runner.return_value.runner.name = "claude-code"
        mock_resolve_runner.return_value.model = "claude-sonnet-4-6"
        mock_run_agent.return_value = _ok_result()

        with patch(f"{_P}.setup_worktree", return_value=tmp_path):
            rc = execute_dispatch(
                allow_unrecorded=True, issue=_make_issue(), config=DispatchConfig()
            )

        assert rc.exit_code == 0
        prompt = mock_run_agent.call_args.kwargs["prompt"]
        assert ".claude/skills" in prompt

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.cleanup_worktree")
    @patch(f"{_P}.check_pr_exists", return_value=99)
    @patch(f"{_P}.update_pr_labels", return_value=True)
    @patch(f"{_P}.mark_completed")
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.resolve_runner")
    def test_worktree_path_omits_pointer_without_skills_dir(
        self,
        mock_resolve_runner: MagicMock,
        mock_run_agent: MagicMock,
        mock_completed: MagicMock,
        mock_pr_labels: MagicMock,
        mock_pr_exists: MagicMock,
        mock_cleanup: MagicMock,
        mock_connectivity: MagicMock,
        tmp_path,
    ):
        from kew.dispatch_core import execute_dispatch

        # No .claude/skills dir created under tmp_path.
        mock_resolve_runner.return_value.runner.name = "claude-code"
        mock_resolve_runner.return_value.model = "claude-sonnet-4-6"
        mock_run_agent.return_value = _ok_result()

        with patch(f"{_P}.setup_worktree", return_value=tmp_path):
            rc = execute_dispatch(
                allow_unrecorded=True, issue=_make_issue(), config=DispatchConfig()
            )

        assert rc.exit_code == 0
        prompt = mock_run_agent.call_args.kwargs["prompt"]
        assert ".claude/skills" not in prompt

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch(f"{_P}.cleanup_worktree")
    @patch(f"{_P}.check_pr_exists", return_value=99)
    @patch(f"{_P}.update_pr_labels", return_value=True)
    @patch(f"{_P}.mark_completed")
    @patch(f"{_P}.run_agent")
    @patch(f"{_P}.resolve_runner")
    def test_worktree_path_omits_pointer_for_non_claude_code_runner(
        self,
        mock_resolve_runner: MagicMock,
        mock_run_agent: MagicMock,
        mock_completed: MagicMock,
        mock_pr_labels: MagicMock,
        mock_pr_exists: MagicMock,
        mock_cleanup: MagicMock,
        mock_connectivity: MagicMock,
        tmp_path,
    ):
        """v1 scope: skills auto-load only for claude-code -- other runners
        must not get a pointer that doesn't apply to them."""
        from kew.config import PricingConfig
        from kew.dispatch_core import execute_dispatch

        (tmp_path / ".claude" / "skills").mkdir(parents=True)
        mock_resolve_runner.return_value.runner.name = "opencode"
        mock_resolve_runner.return_value.model = "gpt-5"
        mock_run_agent.return_value = _ok_result()

        # gpt-5 is not in the shipped table; a non-Anthropic route is priced via
        # a [pricing] override (the escape hatch the #934 preflight honors).
        config = DispatchConfig(
            pricing=PricingConfig(overrides={"gpt-5": [1.25, 10.0]})
        )
        with patch(f"{_P}.setup_worktree", return_value=tmp_path):
            rc = execute_dispatch(
                allow_unrecorded=True, issue=_make_issue(), config=config
            )

        assert rc.exit_code == 0
        prompt = mock_run_agent.call_args.kwargs["prompt"]
        assert ".claude/skills" not in prompt

    @patch(f"{_P}._check_github_connectivity", return_value=True)
    @patch("kew.sandbox.detect_runtime_cached", return_value="/usr/bin/docker")
    @patch(f"{_P}._run_sandboxed")
    @patch(f"{_P}._materialize_checkout")
    @patch(f"{_P}.resolve_runner")
    def test_sandboxed_path_includes_pointer_for_claude_code_with_skills_dir(
        self,
        mock_resolve_runner: MagicMock,
        mock_materialize: MagicMock,
        mock_run_sandboxed: MagicMock,
        mock_detect_runtime: MagicMock,
        mock_connectivity: MagicMock,
        tmp_path,
    ):
        from kew.config import SandboxConfig
        from kew.dispatch_core import execute_dispatch

        checkout_dir = tmp_path / "checkout"
        (checkout_dir / ".claude" / "skills").mkdir(parents=True)
        mock_materialize.return_value = checkout_dir
        mock_resolve_runner.return_value.runner.name = "claude-code"
        mock_resolve_runner.return_value.model = "claude-sonnet-4-6"
        mock_run_sandboxed.return_value = _ok_result()

        config = DispatchConfig(sandbox=SandboxConfig(enabled=True))
        # Sandboxed posture also requires an env credential (#732).
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}):
            rc = execute_dispatch(
                allow_unrecorded=True, issue=_make_issue(), config=config
            )

        assert rc.exit_code == 0
        prompt = mock_run_sandboxed.call_args.kwargs["prompt"]
        assert ".claude/skills" in prompt


class TestSandboxAgentReport:
    """#758: the host broker publishes the sandboxed agent's OWN Dispatch
    Report (its final stream message) — before this, every contained run fell
    through to the #664 machine fallback."""

    _REPORT = "## Dispatch Report\n\n**Issue:** #5\n\n### What was done\n- x"

    def _log(self, tmp_path: Path, lines: list[str]) -> Path:
        """A finished run: sanitized log + derived-evidence sidecar.

        I06T moved report capture from a post-run replay of the raw transcript
        to a fold performed while the stream is read, so the fixture streams
        the same lines through the same derivation the container tail uses —
        and leaves behind exactly what a real run leaves behind: no transcript.
        """
        from kew.transcript_metrics import StreamDerivation

        derivation = StreamDerivation()
        p = tmp_path / "sandbox-5.log"
        p.write_text(
            "".join(
                f"{safe}\n"
                for safe in (derivation.observe(line) for line in lines)
                if safe is not None
            ),
            encoding="utf-8",
        )
        write_derived_evidence(p, derivation.evidence())
        return p

    def test_a_legacy_raw_transcript_yields_no_report(self, tmp_path):
        """A pre-I06T run left a raw log and no sidecar. The report is plainly
        sitting in that file, and kew refuses to read it back: re-ingesting a
        provider transcript as free text is how private reasoning reaches a PR
        comment. Unavailable takes the #664 machine fallback instead."""
        import json as _json

        from kew.dispatch_core import _extract_sandbox_report

        legacy = tmp_path / "sandbox-legacy.log"
        legacy.write_text(
            _json.dumps({"type": "result", "result": self._REPORT}) + "\n",
            encoding="utf-8",
        )
        assert "What was done" in legacy.read_text(encoding="utf-8")
        assert _extract_sandbox_report(legacy) is None

    def test_sanitized_log_keeps_no_report_text(self, tmp_path):
        import json as _json

        from kew.dispatch_core import _extract_sandbox_report

        log = self._log(
            tmp_path, [_json.dumps({"type": "result", "result": self._REPORT})]
        )
        assert "What was done" not in log.read_text(encoding="utf-8")
        assert _extract_sandbox_report(log) == self._REPORT

    def test_extracts_report_from_last_result_event(self, tmp_path):
        import json as _json

        from kew.dispatch_core import _extract_sandbox_report

        log = self._log(
            tmp_path,
            [
                '{"type":"assistant","message":{}}',
                "not json {",
                _json.dumps({"type": "result", "result": "API Error: stalled"}),
                _json.dumps({"type": "result", "result": self._REPORT}),
            ],
        )
        assert _extract_sandbox_report(log) == self._REPORT

    def test_extracts_report_from_codex_agent_message(self, tmp_path):
        import json as _json

        from kew.dispatch_core import _extract_sandbox_report

        log = self._log(
            tmp_path,
            [
                _json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "agent_message",
                            "text": self._REPORT,
                        },
                    }
                ),
                _json.dumps({"type": "turn.completed", "usage": {}}),
            ],
        )
        assert _extract_sandbox_report(log) == self._REPORT

    def test_none_when_run_ended_without_report(self, tmp_path):
        import json as _json

        from kew.dispatch_core import _extract_sandbox_report

        log = self._log(
            tmp_path,
            [_json.dumps({"type": "result", "result": "just some prose"})],
        )
        assert _extract_sandbox_report(log) is None

    def test_none_when_log_missing(self, tmp_path):
        from kew.dispatch_core import _extract_sandbox_report

        assert _extract_sandbox_report(tmp_path / "nope.log") is None

    def test_strips_preamble_and_wrapping_code_fence(self, tmp_path):
        """The agent ended its run with prose + the report wrapped in a ```
        fence (PR #1020 incident). Posted verbatim, GitHub renders the whole
        report as a literal code block. The extractor must return the bare
        report markdown — no preamble, no wrapping fence — so it renders
        natively.
        """
        import json as _json

        from kew.dispatch_core import _extract_sandbox_report

        wrapped = (
            "Committed. Here is my dispatch report.\n\n```\n" + self._REPORT + "\n```"
        )
        log = self._log(tmp_path, [_json.dumps({"type": "result", "result": wrapped})])
        assert _extract_sandbox_report(log) == self._REPORT

    def test_preserves_an_internal_code_fence(self, tmp_path):
        """A report that legitimately contains a fenced code block (balanced
        pair) must survive intact — only a DANGLING wrapper fence is stripped.
        """
        import json as _json

        from kew.dispatch_core import _extract_sandbox_report

        report = self._REPORT + "\n\n### Testing\n```bash\npytest -q\n```"
        log = self._log(tmp_path, [_json.dumps({"type": "result", "result": report})])
        assert _extract_sandbox_report(log) == report

    def test_strips_wrapper_but_keeps_internal_fence(self, tmp_path):
        """Both at once: a wrapped report that also contains a real code block.
        The internal ```bash pair stays; the outer wrapper fence goes.
        """
        import json as _json

        from kew.dispatch_core import _extract_sandbox_report

        report = self._REPORT + "\n\n### Testing\n```bash\npytest -q\n```"
        wrapped = "Here is the report:\n\n```markdown\n" + report + "\n```"
        log = self._log(tmp_path, [_json.dumps({"type": "result", "result": wrapped})])
        assert _extract_sandbox_report(log) == report

    def test_posts_agent_report_on_pr(self, tmp_path):
        import json as _json

        from kew.dispatch_core import _post_agent_report

        log = self._log(
            tmp_path, [_json.dumps({"type": "result", "result": self._REPORT})]
        )
        config = DispatchConfig()
        config.project.repo = "octo/widgets"
        captured: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            captured.append(list(cmd))
            if cmd[:4] == ["gh", "api", "--method", "POST"] and cmd[4].endswith(
                "/pulls"
            ):
                return MagicMock(
                    returncode=0,
                    stdout='{"number":12,"html_url":"https://github.com/o/r/pull/12"}',
                    stderr="",
                )
            return MagicMock(returncode=0, stdout="", stderr="")

        with (
            patch(f"{_P}.run", side_effect=fake_run),
            patch(f"{_P}._git_push_branch", return_value=MagicMock()),
        ):
            _post_agent_report("https://github.com/octo/widgets/pull/9", log, config)

        [cmd] = captured
        assert cmd[:3] == ["gh", "pr", "comment"]
        assert "https://github.com/octo/widgets/pull/9" in cmd
        body = cmd[cmd.index("--body") + 1]
        assert body.startswith("**Agent-authored report**")
        assert f"\n\n{quoted(self._REPORT)}\n\n" in body
        assert body.endswith("<!-- kew-dispatch-report run= -->\n")

    def test_no_post_when_no_report_or_no_pr(self, tmp_path):
        import json as _json

        from kew.dispatch_core import _post_agent_report

        config = DispatchConfig()
        config.project.repo = "octo/widgets"
        no_report = self._log(
            tmp_path, [_json.dumps({"type": "result", "result": "prose"})]
        )
        with patch(f"{_P}.run") as mock_run:
            _post_agent_report("https://github.com/x/y/pull/9", no_report, config)
            _post_agent_report(None, no_report, config)
        mock_run.assert_not_called()

    def test_gh_failure_is_fail_soft(self, tmp_path):
        # Nonzero gh exit must warn, never raise — reporting cannot derail a
        # completed dispatch.
        import json as _json

        from kew.dispatch_core import _post_agent_report

        log = self._log(
            tmp_path, [_json.dumps({"type": "result", "result": self._REPORT})]
        )
        config = DispatchConfig()
        config.project.repo = "octo/widgets"
        failed = MagicMock(returncode=1, stdout="", stderr="boom")
        with patch(f"{_P}.run", return_value=failed):
            _post_agent_report("https://github.com/x/y/pull/9", log, config)

    def test_run_exception_is_fail_soft(self, tmp_path):
        # run() raising (allowlist, timeout, OSError) must be swallowed too.
        import json as _json

        from kew.dispatch_core import _post_agent_report

        log = self._log(
            tmp_path, [_json.dumps({"type": "result", "result": self._REPORT})]
        )
        config = DispatchConfig()
        config.project.repo = "octo/widgets"
        with patch(f"{_P}.run", side_effect=OSError("gh vanished")):
            _post_agent_report("https://github.com/x/y/pull/9", log, config)


class TestSandboxedRunRecord:
    """#739: the sandboxed path must write the run-row liveness signal the
    epic driver polls — a reservation with no run row past the startup grace
    is failed with a `dispatch_child_died` diagnostic, so a slow-but-alive
    container that never wrote this row would be wrongly declared dead."""

    def _run(
        self,
        tmp_path,
        store,
        *,
        wait_rc=0,
        launch_error=None,
        stream_lines=None,
        model="",
        auth_path="workload-identity",
    ):
        from kew import dispatch_core
        from kew.config import DispatchConfig

        checkout = tmp_path / "checkout"
        checkout.mkdir(exist_ok=True)
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")

        statuses_at_launch: list[str] = []
        self.models_at_launch: list[str] = []
        self.argv_at_launch: list[str] = []

        def _fake_launch(
            argv: list[str],
            *,
            admission,
            on_spawn,
            prepare,
            env,
            credential,
            separate_stderr=False,
        ):
            # Capture what the driver would see WHILE the container runs.
            self.argv_at_launch = list(argv)
            rows = store.list_by_issue(5)
            statuses_at_launch.extend(r.status for r in rows)
            self.models_at_launch.extend(r.model for r in rows)
            if launch_error is not None:
                raise launch_error
            handle = MagicMock()
            handle.__iter__.return_value = iter(stream_lines or [])
            handle.wait.return_value = wait_rc
            return handle

        with (
            patch.object(
                dispatch_core, "_detect_sandbox_runtime", return_value="/usr/bin/docker"
            ),
            patch.object(dispatch_core, "_launch_container", side_effect=_fake_launch),
            patch.object(dispatch_core, "_push_and_open_pr"),
            patch("kew.dispatch_sandbox._contained_auth_path", return_value=auth_path),
        ):
            self.result = dispatch_core._run_sandboxed(
                prompt="do the thing",
                checkout_dir=checkout,
                config=config,
                image=SANDBOX_IMAGE,
                runner=SANDBOX_RUNNER,
                issue_num=5,
                item_id="5",
                branch="agent/issue-5",
                title="t",
                model=model,
                store=store,
            )
        return statuses_at_launch

    def _store(self, tmp_path):
        from kew.run_store import SqliteRunStore

        return SqliteRunStore(str(tmp_path / "runs.db"))

    def test_pending_row_exists_while_container_runs(self, tmp_path):
        store = self._store(tmp_path)
        statuses = self._run(tmp_path, store)
        assert statuses == ["pending"]

    def test_reusable_credential_launch_is_admitted_for_a_scanned_push(
        self, tmp_path, monkeypatch
    ):
        from kew.sandbox import runner_credential_env_vars

        names = runner_credential_env_vars(SANDBOX_RUNNER)
        assert names
        monkeypatch.setenv(names[0], "synthetic-provider-credential")
        store = self._store(tmp_path)

        statuses = self._run(tmp_path, store, auth_path=f"env:{names[0]}")

        # #1969: the launch proceeds; its push is scanned before publication.
        assert statuses == ["pending"]
        assert self.result.error_category != "credential_publication_refused"

    def test_missing_auth_evidence_refuses_before_container_launch(
        self, tmp_path, monkeypatch
    ):
        from kew.sandbox import runner_credential_env_vars

        for name in runner_credential_env_vars(SANDBOX_RUNNER):
            monkeypatch.setenv(name, "synthetic-provider-credential")
        store = self._store(tmp_path)

        statuses = self._run(tmp_path, store, auth_path=None)

        assert statuses == []
        assert self.result.error_category == "credential_publication_refused"
        assert self.result.exit_code == 1
        [row] = store.list_by_issue(5)
        assert row.status == "refused"
        assert row.error_category == "credential_publication_refused"
        events = store.list_audit_events(
            limit=None, event="credential_publication_refused"
        )
        assert len(events) == 1
        assert "synthetic-provider-credential" not in str(events[0].payload)

    def test_success_finalizes_row(self, tmp_path):
        store = self._store(tmp_path)
        self._run(tmp_path, store, wait_rc=0)
        [row] = store.list_by_issue(5)
        assert row.status == "success"
        assert row.exit_code == 0
        assert row.finished_at is not None
        assert row.container_name == "kew-5"
        assert row.runner_name == "claude-code"

    def test_failure_finalizes_row(self, tmp_path):
        store = self._store(tmp_path)
        self._run(tmp_path, store, wait_rc=3)
        [row] = store.list_by_issue(5)
        assert row.status == "failure"
        assert row.exit_code == 3

    def test_launch_exception_finalizes_and_reraises(self, tmp_path):
        store = self._store(tmp_path)
        with pytest.raises(OSError):
            self._run(tmp_path, store, launch_error=OSError("no docker socket"))
        [row] = store.list_by_issue(5)
        assert row.status == "failure"
        assert row.error_category == "sandbox_launch_error"

    def test_stream_exception_finalizes_with_stream_category(self, tmp_path):
        # Launch succeeds, container stdout breaks mid-stream: the run history
        # must name the streaming layer, not blame the launch.
        from kew import dispatch_core
        from kew.config import DispatchConfig

        store = self._store(tmp_path)
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")

        def _broken_iter():
            raise OSError("container stdout gone")
            yield  # pragma: no cover

        handle = MagicMock()
        handle.__iter__.return_value = _broken_iter()
        handle.observe_workload_start.return_value = True
        with (
            patch.object(
                dispatch_core, "_detect_sandbox_runtime", return_value="/usr/bin/docker"
            ),
            patch.object(dispatch_core, "_launch_container", return_value=handle),
            patch(
                "kew.dispatch_sandbox._contained_auth_path",
                return_value="workload-identity",
            ),
            pytest.raises(OSError),
        ):
            dispatch_core._run_sandboxed(
                prompt="p",
                checkout_dir=checkout,
                config=config,
                image=SANDBOX_IMAGE,
                runner=SANDBOX_RUNNER,
                issue_num=5,
                item_id="5",
                branch="agent/issue-5",
                store=store,
            )
        [row] = store.list_by_issue(5)
        assert row.status == "failure"
        assert row.error_category == "sandbox_stream_error"

    def test_no_store_is_refused(self, tmp_path):
        from kew import dispatch_core
        from kew.config import DispatchConfig

        checkout = tmp_path / "checkout"
        checkout.mkdir()
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        handle = MagicMock()
        handle.__iter__.return_value = iter([])
        handle.wait.return_value = 0
        with (
            patch.object(
                dispatch_core, "_detect_sandbox_runtime", return_value="/usr/bin/docker"
            ),
            patch.object(dispatch_core, "_launch_container", return_value=handle),
            patch.object(dispatch_core, "_push_and_open_pr"),
            # A paid contained run with no store would leave no realized
            # route row at all — refused by design (#1550 review F5/F-6).
            pytest.raises(ValueError, match="requires a store"),
        ):
            dispatch_core._run_sandboxed(
                prompt="p",
                checkout_dir=checkout,
                config=config,
                image=SANDBOX_IMAGE,
                runner=SANDBOX_RUNNER,
                issue_num=5,
                item_id="5",
                branch="agent/issue-5",
            )

    def _result_event_line(self):
        # A real stream-json result event: modelUsage carries per-model tokens
        # and costUSD; the busiest model names the run (#761).
        return (
            json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "total_cost_usd": 0.42,
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 20,
                        "cache_creation_input_tokens": 30,
                        "cache_read_input_tokens": 40,
                    },
                    "modelUsage": {
                        "claude-sonnet-4-6": {
                            "inputTokens": 10,
                            "outputTokens": 20,
                            "cacheCreationInputTokens": 30,
                            "cacheReadInputTokens": 40,
                            "costUSD": 0.42,
                        }
                    },
                }
            )
            + "\n"
        )

    def test_result_event_populates_cost_tokens_and_model(self, tmp_path):
        # #761: the terminal row carries cost/tokens/reported_model parsed from
        # the streamed result event, not zeros.
        store = self._store(tmp_path)
        self._run(
            tmp_path,
            store,
            stream_lines=[self._result_event_line()],
            model="claude-sonnet-4-6",
        )
        [row] = store.list_by_issue(5)
        assert row.estimated_cost_usd == pytest.approx(0.42)
        assert row.input_tokens == 10
        assert row.output_tokens == 20
        assert row.cache_creation_tokens == 30
        assert row.cache_read_tokens == 40
        assert row.total_tokens == 100
        assert row.reported_model == "claude-sonnet-4-6"

    def test_sums_cost_and_tokens_across_multiple_models(self, tmp_path):
        # modelUsage may span models (e.g. a sonnet worker + a haiku sub-agent):
        # cost/tokens sum; the higher-cost model names the run.
        store = self._store(tmp_path)
        line = (
            json.dumps(
                {
                    "type": "result",
                    "modelUsage": {
                        "claude-haiku-4-5": {
                            "inputTokens": 1,
                            "outputTokens": 2,
                            "costUSD": 0.01,
                        },
                        "claude-sonnet-4-6": {
                            "inputTokens": 10,
                            "outputTokens": 20,
                            "costUSD": 0.40,
                        },
                    },
                }
            )
            + "\n"
        )
        self._run(tmp_path, store, stream_lines=[line])
        [row] = store.list_by_issue(5)
        assert row.estimated_cost_usd == pytest.approx(0.41)
        assert row.input_tokens == 11
        assert row.output_tokens == 22
        assert row.reported_model == "claude-sonnet-4-6"

    def test_missing_result_event_records_zeros(self, tmp_path):
        # No result event → today's behavior: zeros, no raise, no model.
        store = self._store(tmp_path)
        self._run(
            tmp_path,
            store,
            stream_lines=["not json\n", '{"type": "assistant"}\n'],
        )
        [row] = store.list_by_issue(5)
        assert row.estimated_cost_usd == 0.0
        assert row.total_tokens == 0
        assert row.reported_model is None
        assert row.status == "success"

    def test_malformed_result_event_does_not_raise(self, tmp_path):
        # A truncated/garbage result line must not blow up finalization.
        store = self._store(tmp_path)
        self._run(
            tmp_path,
            store,
            stream_lines=['{"type": "result", "modelUsage": "oops"}\n'],
        )
        [row] = store.list_by_issue(5)
        assert row.estimated_cost_usd == 0.0
        assert row.total_tokens == 0

    def test_routed_model_recorded_at_start(self, tmp_path):
        # #761: the pending row stamps the routed effective model, not the
        # (empty) config default — captured WHILE the container runs.
        store = self._store(tmp_path)
        self._run(tmp_path, store, model="claude-sonnet-4-6")
        assert self.models_at_launch == ["claude-sonnet-4-6"]
        [row] = store.list_by_issue(5)
        assert row.model == "claude-sonnet-4-6"

    def test_routed_model_reaches_container_argv(self, tmp_path):
        # #761: routing must actually reach the container — the built argv
        # carries `--model <routed>`, not just the run row. Without this the
        # row records the routed model while the container runs the config
        # default (a mislabeled ledger, routing with zero effect).
        store = self._store(tmp_path)
        self._run(tmp_path, store, model="claude-sonnet-4-6")
        assert "--model" in self.argv_at_launch
        idx = self.argv_at_launch.index("--model")
        assert self.argv_at_launch[idx + 1] == "claude-sonnet-4-6"


class TestSandboxItemIdThreading:
    """Group A5b: a non-numeric (Linear) work-item id threads through the
    sandbox dispatch path — slugged container name, log file, and RunRecord
    ``item_id`` — while ``issue_num`` stays the ``0`` sentinel (A3)."""

    def test_linear_item_id_slugs_container_and_row(self, tmp_path):
        from kew import dispatch_core
        from kew.config import DispatchConfig
        from kew.run_store import SqliteRunStore

        store = SqliteRunStore(str(tmp_path / "runs.db"))
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")

        captured: dict[str, list[str]] = {}

        def _fake_launch(
            argv: list[str],
            *,
            admission,
            on_spawn,
            prepare,
            env,
            credential,
            separate_stderr=False,
        ):
            captured["argv"] = list(argv)
            handle = MagicMock()
            handle.__iter__.return_value = iter([])
            handle.wait.return_value = 0
            return handle

        with (
            patch.object(
                dispatch_core, "_detect_sandbox_runtime", return_value="/usr/bin/docker"
            ),
            patch.object(dispatch_core, "_launch_container", side_effect=_fake_launch),
            patch.object(dispatch_core, "_push_and_open_pr") as mock_push,
        ):
            dispatch_core._run_sandboxed(
                prompt="p",
                checkout_dir=checkout,
                config=config,
                image=SANDBOX_IMAGE,
                runner=SANDBOX_RUNNER,
                issue_num=0,  # 0 sentinel for a non-numeric tracker id
                item_id="SFD-12",
                branch="agent/sfd-12",
                title="t",
                store=store,
                tracker="linear",
            )

        # RunRecord carries the durable id; issue_num stays the sentinel.
        [row] = store.list_by_item("SFD-12")
        assert row.item_id == "SFD-12"
        assert row.issue_num == 0
        # Container name + log file use the branch-safe slug (docker charset).
        assert row.container_name == "kew-sfd-12"
        name = captured["argv"][captured["argv"].index("--name") + 1]
        assert name == "kew-sfd-12"
        assert str(row.log_file).endswith("sandbox-sfd-12.log")
        # Host brokered push/PR keyed on the item id, never issue_num.
        assert mock_push.call_args.args[2] == "SFD-12"


class TestPushAndOpenPrDraft:
    """_push_and_open_pr extends with draft mode for rescue incomplete-run path."""

    def test_linear_body_has_item_id_and_no_closes_marker(self):
        # Linear PRs carry the bare item id; never a `Closes` marker GitHub
        # can't resolve (resolution comes from the branch lookup, A6).
        captured: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            captured.append(list(cmd))
            if cmd[:4] == ["gh", "api", "--method", "POST"] and cmd[4].endswith(
                "/pulls"
            ):
                return MagicMock(
                    returncode=0,
                    stdout='{"number":9,"html_url":"https://github.com/o/r/pull/9"}',
                    stderr="",
                )
            return MagicMock(returncode=0, stdout="", stderr="")

        config = DispatchConfig()
        config.project.repo = "o/r"
        with (
            patch(f"{_P}.run", side_effect=fake_run),
            patch(f"{_P}._git_push_branch", return_value=MagicMock()),
        ):
            from kew.dispatch_core import _push_and_open_pr

            _push_and_open_pr(Path("/tmp/wt"), "agent/sfd-12", "SFD-12", "", config)

        create_cmd = next(c for c in captured if c[-1].startswith("draft="))
        body = create_cmd[create_cmd.index("body=SFD-12")]
        title = create_cmd[create_cmd.index("title=Fix SFD-12")]
        assert "Closes" not in body
        assert body == "body=SFD-12"
        assert title == "title=Fix SFD-12"

    def test_draft_passes_flag_body_and_extra_labels(self):
        """When draft=True, pass --draft flag, custom body, and extra_labels."""
        captured: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            captured.append(list(cmd))
            if cmd[:4] == ["gh", "api", "--method", "POST"] and cmd[4].endswith(
                "/pulls"
            ):
                return MagicMock(
                    returncode=0,
                    stdout='{"number":9,"html_url":"https://github.com/o/r/pull/9"}',
                    stderr="",
                )
            return MagicMock(returncode=0, stdout="", stderr="")

        config = DispatchConfig()
        config.project.repo = "o/r"
        with (
            patch(f"{_P}.run", side_effect=fake_run),
            patch(f"{_P}._git_push_branch", return_value=MagicMock()),
        ):
            from kew.dispatch_core import _push_and_open_pr

            _push_and_open_pr(
                Path("/tmp/wt"),
                "agent/issue-9",
                "9",
                "Add glossary",
                config,
                draft=True,
                body="⚠️ incomplete run",
                extra_labels=("incomplete-run",),
            )

        create_cmd = next(c for c in captured if c[-1] == "draft=true")
        assert "body=⚠️ incomplete run" in create_cmd
        labels_cmd = next(c for c in captured if c[4].endswith("/issues/9/labels"))
        assert "labels[]=incomplete-run" in labels_cmd

    def test_non_draft_unchanged(self):
        """Existing callers unchanged: no --draft, default body."""
        captured: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            captured.append(list(cmd))
            if cmd[:4] == ["gh", "api", "--method", "POST"] and cmd[4].endswith(
                "/pulls"
            ):
                return MagicMock(
                    returncode=0,
                    stdout='{"number":9,"html_url":"https://github.com/o/r/pull/9"}',
                    stderr="",
                )
            return MagicMock(returncode=0, stdout="", stderr="")

        config = DispatchConfig()
        config.project.repo = "o/r"
        with (
            patch(f"{_P}.run", side_effect=fake_run),
            patch(f"{_P}._git_push_branch", return_value=MagicMock()),
        ):
            from kew.dispatch_core import _push_and_open_pr

            _push_and_open_pr(Path("/tmp/wt"), "agent/issue-9", "9", "Fix it", config)

        create_cmd = next(c for c in captured if c[-1] == "draft=false")
        assert "body=Closes #9" in create_cmd


class TestGitPushBranchRetry:
    """_git_push_branch retries transient push failures with bounded backoff."""

    @pytest.fixture
    def publication_checkout(self, tmp_path: Path) -> Path:
        """Retry-wrapper tests still require a real checkout generation."""
        from kew.worktree import write_checkout_generation

        checkout = tmp_path / "checkout"
        checkout.mkdir()
        (checkout / ".git").mkdir(exist_ok=True)
        write_checkout_generation(checkout)
        return checkout

    def test_checkout_escape_refuses_before_publication_without_admission(self):
        from kew.dispatch_core import _push_and_open_pr
        from kew.publication import PublicationError

        config = DispatchConfig()
        config.publication.publish = "checkout"
        with (
            patch(f"{_P}.run") as run,
            pytest.raises(
                PublicationError,
                match=r"#1218.*uncontained-publication",
            ),
        ):
            _push_and_open_pr(Path("/tmp/wt"), "agent/issue-9", "9", "Fix", config)

        run.assert_not_called()

    def test_first_push_fails_second_succeeds(self, publication_checkout):
        """The retry policy reaches one immutable publication intent."""
        from kew.dispatch_core import _git_push_branch

        published = MagicMock()

        slept: list[float] = []
        with (
            patch(f"{_P}.publication_remote", return_value="/host/origin.git"),
            patch(f"{_P}.remote_branch_sha", return_value=""),
            patch(f"{_P}.publish_checkout", return_value=published) as publish,
        ):
            result = _git_push_branch(
                "agent/issue-1",
                publication_checkout,
                base_sha="a" * 40,
                run_id="retry-run",
                _sleep=slept.append,
            )

        assert result is published
        publish.assert_called_once()
        assert publish.call_args.kwargs["push_attempts"] == 3
        assert publish.call_args.kwargs["push_delays"] == (20.0, 40.0)
        assert publish.call_args.kwargs["_sleep"] == slept.append

    def test_all_attempts_fail_raises_with_checkout_path(self, publication_checkout):
        """When all retries are exhausted, error message names the checkout path."""
        from kew.dispatch_core import _git_push_branch
        from kew.publication import PublicationError

        with (
            pytest.raises(RuntimeError) as exc_info,
            patch(f"{_P}.publication_remote", return_value="/host/origin.git"),
            patch(f"{_P}.remote_branch_sha", return_value=""),
            patch(
                f"{_P}.publish_checkout",
                side_effect=PublicationError("push", "SSH error"),
            ),
        ):
            _git_push_branch(
                "agent/issue-1",
                publication_checkout,
                base_sha="a" * 40,
                run_id="retry-run",
                max_attempts=3,
            )

        msg = str(exc_info.value)
        assert isinstance(exc_info.value, PublicationError)
        assert exc_info.value.step == "push"
        assert "3 attempts" in msg
        assert str(publication_checkout) in msg  # checkout path for manual salvage

    def test_immediate_success_no_sleep(self, publication_checkout):
        """No backoff sleep when push succeeds on first attempt."""
        from kew.dispatch_core import _git_push_branch

        slept: list[float] = []
        with (
            patch(f"{_P}.publication_remote", return_value="/host/origin.git"),
            patch(f"{_P}.remote_branch_sha", return_value=""),
            patch(f"{_P}.publish_checkout", return_value=MagicMock()) as publish,
        ):
            _git_push_branch(
                "agent/issue-1",
                publication_checkout,
                base_sha="a" * 40,
                run_id="retry-run",
                _sleep=slept.append,
            )

        assert slept == []
        publish.assert_called_once()

    def test_known_contained_legacy_output_refuses_before_publication_calls(
        self, publication_checkout, tmp_path
    ):
        from kew.dispatch_core import _git_push_branch
        from kew.publication_credentials import CredentialPublicationRefused
        from kew.run_store import RunRecord, SqliteRunStore

        store = SqliteRunStore(str(tmp_path / "publication.db"))
        store.save(
            RunRecord(
                run_id="legacy-contained",
                issue_num=1,
                item_id="1",
                branch="agent/issue-1",
                status="awaiting_publication",
                exit_code=0,
                started_at=datetime(2026, 9, 5, tzinfo=UTC),
                container_name="kew-1",
            )
        )
        with (
            patch(f"{_P}.publication_remote") as remote,
            patch(f"{_P}.publish_checkout") as publish,
            pytest.raises(CredentialPublicationRefused),
        ):
            _git_push_branch(
                "agent/issue-1",
                publication_checkout,
                base_sha="a" * 40,
                store=store,
                run_id="legacy-contained",
            )

        remote.assert_not_called()
        publish.assert_not_called()

    def test_continuation_publication_keeps_original_exposure(
        self, publication_checkout, tmp_path
    ):
        from kew.audit import Actor, emit_continuation_evidence
        from kew.dispatch_core import _git_push_branch
        from kew.publication_credentials import CredentialPublicationRefused
        from kew.run_store import RunRecord, SqliteRunStore

        store = SqliteRunStore(str(tmp_path / "continuation-publication.db"))
        for run_id, container_name in (
            ("native-producer", None),
            ("exposed-continuation", "kew-1"),
            ("native-continuation", None),
        ):
            store.save(
                RunRecord(
                    run_id=run_id,
                    issue_num=1,
                    item_id="1",
                    branch="agent/issue-1",
                    status="awaiting_publication",
                    exit_code=0,
                    started_at=datetime(2026, 9, 5, tzinfo=UTC),
                    container_name=container_name,
                )
            )
        preserved = tmp_path / "preserved"
        store.record_continuation(
            "epic:1",
            "1",
            "native-producer",
            "agent/issue-1",
            preserved,
            checkout_generation=fixture_checkout_generation(preserved),
        )
        assert (
            store.claim_continuation_under_cap(
                "epic:1",
                "1",
                "native-producer",
                "agent/issue-1",
                preserved,
                "exposed-continuation",
                0,
                claim_evidence=fixture_claim_evidence(
                    store,
                    "epic:1",
                    "1",
                    "native-producer",
                    "agent/issue-1",
                    preserved,
                    "exposed-continuation",
                ),
            )
            == "claimed"
        )
        first = store.get_continuation(
            "epic:1", "1", "native-producer", "agent/issue-1", preserved
        )
        assert first is not None
        emit_continuation_evidence(
            store,
            "continuation_claimed",
            actor=Actor("policy", "test"),
            phase="claim",
            epic_ref=first.epic_ref,
            item_id=first.item_id,
            original_run_id=first.original_run_id,
            continuation_run_id="exposed-continuation",
            branch=first.branch,
            worktree_identity=first.worktree_identity,
            generation=first.generation,
            state=first.state,
            outcome="continuing",
            run_id="exposed-continuation",
            issue_num=1,
        )
        assert store.mark_continuation_awaiting(
            "epic:1",
            "1",
            "native-producer",
            "agent/issue-1",
            preserved,
            1,
            "exposed-continuation",
        )
        assert (
            store.claim_continuation_under_cap(
                "epic:1",
                "1",
                "native-producer",
                "agent/issue-1",
                preserved,
                "native-continuation",
                0,
                claim_evidence=fixture_claim_evidence(
                    store,
                    "epic:1",
                    "1",
                    "native-producer",
                    "agent/issue-1",
                    preserved,
                    "native-continuation",
                ),
            )
            == "claimed"
        )
        second = store.get_continuation(
            "epic:1", "1", "native-producer", "agent/issue-1", preserved
        )
        assert second is not None
        emit_continuation_evidence(
            store,
            "continuation_claimed",
            actor=Actor("policy", "test"),
            phase="claim",
            epic_ref=second.epic_ref,
            item_id=second.item_id,
            original_run_id=second.original_run_id,
            continuation_run_id="native-continuation",
            branch=second.branch,
            worktree_identity=second.worktree_identity,
            generation=second.generation,
            state=second.state,
            outcome="continuing",
            run_id="native-continuation",
            issue_num=1,
        )

        with (
            patch(f"{_P}.publication_remote") as remote,
            patch(f"{_P}.publish_checkout") as publish,
            pytest.raises(CredentialPublicationRefused),
        ):
            _git_push_branch(
                "agent/issue-1",
                publication_checkout,
                base_sha="a" * 40,
                store=store,
                run_id="native-continuation",
            )

        remote.assert_not_called()
        publish.assert_not_called()

    def test_checkout_escape_uses_captured_token_for_git_and_gh(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Publication survives a child env with no interactive credential surface."""
        from kew.dispatch_core import _push_and_open_pr

        monkeypatch.setattr(
            "kew.publication.os.environ",
            {"PATH": "/usr/bin", "KEW_DETACHED_PUBLICATION_TOKEN": "gho_secret"},
        )
        calls: list[tuple[list[str], dict[str, Any]]] = []

        def fake_run(cmd, **kwargs):
            calls.append((list(cmd), kwargs))
            stdout = (
                '{"number":9,"html_url":"https://github.com/o/r/pull/9"}'
                if cmd[:4] == ["gh", "api", "--method", "POST"]
                and cmd[4].endswith("/pulls")
                else ""
            )
            return MagicMock(returncode=0, stdout=stdout, stderr="")

        config = DispatchConfig()
        config.project.repo = "o/r"
        config.publication.publish = "checkout"
        config.sandbox.dangerously_skip_containment = True
        with (
            patch(f"{_P}.run", side_effect=fake_run),
            patch(f"{_P}._guard_checkout_output", autospec=True) as scan,
        ):
            _push_and_open_pr(
                Path("/tmp/wt"), "agent/issue-9", "9", "Fix", config, base_sha="a" * 40
            )
        scan.assert_called_once_with(
            Path("/tmp/wt"),
            "agent/issue-9",
            repo="o/r",
            base_sha="a" * 40,
            base_branch="",
            store=None,
            run_id=None,
        )

        push_cmd, push_kwargs = next(
            call for call in calls if call[0][:2] == ["git", "push"]
        )
        create_cmd, create_kwargs = next(
            call
            for call in calls
            if call[0][:4] == ["gh", "api", "--method", "POST"]
            and call[0][4].endswith("/pulls")
        )
        assert "https://github.com/o/r.git" in push_cmd
        assert push_kwargs["env"]["GH_TOKEN"] == "gho_secret"
        # The push reads objects exactly as the pre-push scan did.
        assert push_kwargs["env"]["GIT_NO_REPLACE_OBJECTS"] == "1"
        assert push_kwargs["env"]["GIT_GRAFT_FILE"] == os.devnull
        pushed_config = {
            push_kwargs["env"][f"GIT_CONFIG_KEY_{index}"]: push_kwargs["env"][
                f"GIT_CONFIG_VALUE_{index}"
            ]
            for index in range(int(push_kwargs["env"]["GIT_CONFIG_COUNT"]))
        }
        assert pushed_config["core.useReplaceRefs"] == "false"
        assert pushed_config["core.hooksPath"] == os.devnull
        assert "--no-follow-tags" in push_cmd
        assert create_kwargs["env"]["GH_TOKEN"] == "gho_secret"
        assert any(
            key.startswith("GIT_CONFIG_VALUE_")
            and value.startswith("Authorization: Basic ")
            for key, value in push_kwargs["env"].items()
        )
        assert "gho_secret" not in " ".join(push_cmd + create_cmd)

    def test_checkout_escape_resolves_repo_from_origin_when_config_is_unset(self):
        """The one-release checkout escape keeps its legacy origin fallback."""
        from kew.dispatch_core import _push_and_open_pr

        calls: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            if cmd == ["git", "remote", "get-url", "origin"]:
                return MagicMock(
                    returncode=0,
                    stdout="git@github.com:owner/repo.git\n",
                    stderr="",
                )
            stdout = (
                '{"number":9,"html_url":"https://github.com/owner/repo/pull/9"}'
                if cmd[:4] == ["gh", "api", "--method", "POST"]
                and cmd[4].endswith("/pulls")
                else ""
            )
            return MagicMock(returncode=0, stdout=stdout, stderr="")

        config = DispatchConfig()
        config.publication.publish = "checkout"
        config.sandbox.dangerously_skip_containment = True
        with (
            patch(f"{_P}.run", side_effect=fake_run),
            patch(f"{_P}._guard_checkout_output", autospec=True) as scan,
        ):
            _push_and_open_pr(
                Path("/tmp/wt"), "agent/issue-9", "9", "Fix", config, base_sha="a" * 40
            )
        scan.assert_called_once_with(
            Path("/tmp/wt"),
            "agent/issue-9",
            repo=None,
            base_sha="a" * 40,
            base_branch="",
            store=None,
            run_id=None,
        )

        create_cmd = next(
            c for c in calls if c[:4] == ["gh", "api", "--method", "POST"]
        )
        assert create_cmd[4] == "repos/owner/repo/pulls"

    def test_publication_rejects_missing_repo_before_rest_create(self):
        """A missing repo is a typed publication failure, never repos/None."""
        from kew.dispatch_core import _push_and_open_pr
        from kew.publication import PublicationError

        calls: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            if cmd == ["git", "remote", "get-url", "origin"]:
                return MagicMock(returncode=1, stdout="", stderr="no origin")
            return MagicMock(returncode=0, stdout="", stderr="")

        config = DispatchConfig()
        config.publication.publish = "checkout"
        config.sandbox.dangerously_skip_containment = True
        with (
            patch(f"{_P}.run", side_effect=fake_run),
            patch(f"{_P}._guard_checkout_output", autospec=True),
            pytest.raises(
                PublicationError,
                match="GitHub repository slug unavailable",
            ) as exc_info,
        ):
            _push_and_open_pr(
                Path("/tmp/wt"), "agent/issue-9", "9", "Fix", config, base_sha="a" * 40
            )

        assert exc_info.value.step == "pr_create"
        assert not any(c[:4] == ["gh", "api", "--method", "POST"] for c in calls)


class TestRequirePublicationRepo:
    """The slug publication demands, and an error naming what it actually tried."""

    def _require(self, mode, *, config=None, store=None, run_id=None, origin=None):
        from kew.dispatch_core import _require_publication_repo

        with patch(f"{_P}._repo_slug_from_origin", return_value=origin):
            return _require_publication_repo(
                Path("/tmp/wt"), config or DispatchConfig(), store, run_id, mode
            )

    def test_canonical_error_names_only_the_sources_consulted(self):
        """Canonical never reads the worktree origin, so it cannot blame it (#2097)."""
        from kew.publication import PublicationError

        with pytest.raises(PublicationError) as exc_info:
            self._require("canonical", origin="octo/widgets")

        detail = str(exc_info.value)
        assert exc_info.value.step == "pr_create"
        assert "project.repo" in detail
        assert "run row" in detail
        assert "origin" not in detail

    def test_checkout_error_names_the_worktree_origin_it_tried(self):
        from kew.publication import PublicationError

        with pytest.raises(PublicationError) as exc_info:
            self._require("checkout", origin=None)

        detail = str(exc_info.value)
        assert "project.repo" in detail
        assert "worktree origin" in detail
        assert "run row" in detail

    def test_canonical_uses_the_slug_the_parent_persisted(self):
        store = MagicMock()
        store.get.return_value = SimpleNamespace(repo=" octo/widgets ")

        assert (
            self._require("canonical", store=store, run_id="r1", origin=None)
            == "octo/widgets"
        )

    def test_configured_repo_outranks_every_fallback(self):
        config = DispatchConfig()
        config.project.repo = "octo/configured"
        store = MagicMock()
        store.get.return_value = SimpleNamespace(repo="octo/stored")

        assert (
            self._require(
                "checkout",
                config=config,
                store=store,
                run_id="r1",
                origin="octo/origin",
            )
            == "octo/configured"
        )


class TestCommitAllIfDirty:
    def test_checkout_escape_reads_changed_files_through_checkout_git(
        self, tmp_path: Path
    ) -> None:
        from kew.dispatch_core import _get_changed_files

        work = tmp_path / "work"
        work.mkdir()
        commands = (
            ("init", "-q", "-b", "main", "."),
            ("config", "user.name", "Test"),
            ("config", "user.email", "test@example.invalid"),
        )
        for command in commands:
            subprocess.run(["git", *command], cwd=work, check=True)
        (work / "base.txt").write_text("base\n")
        subprocess.run(["git", "add", "-A"], cwd=work, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=work, check=True)
        base = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=work,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        subprocess.run(
            ["git", "update-ref", "refs/remotes/origin/main", base],
            cwd=work,
            check=True,
        )
        (work / "changed.txt").write_text("changed\n")
        subprocess.run(["git", "add", "-A"], cwd=work, check=True)
        subprocess.run(["git", "commit", "-qm", "change"], cwd=work, check=True)

        assert _get_changed_files(
            work,
            "main",
            publish="checkout",
            dangerously_skip_containment=True,
        ) == ["changed.txt"]

    def test_canonical_detects_work_without_checkout_git(self):
        import kew.dispatch_core as dc
        from kew.config import DispatchConfig

        with (
            patch(f"{_P}._get_changed_files", return_value=["src/new.py"]),
            patch(f"{_P}.run") as mock_run,
        ):
            committed = dc._commit_all_if_dirty(Path("/tmp/wt"), "9", DispatchConfig())

        assert committed is True
        mock_run.assert_not_called()

    def test_canonical_noop_when_clean(self):
        import kew.dispatch_core as dc
        from kew.config import DispatchConfig

        with (
            patch(f"{_P}._get_changed_files", return_value=[]),
            patch(f"{_P}.run") as mock_run,
        ):
            committed = dc._commit_all_if_dirty(Path("/tmp/wt"), "9", DispatchConfig())

        assert committed is False
        mock_run.assert_not_called()


class TestBrokerAgentWork:
    """kew brokers the git ceremony when a (weaker) agent writes code but skips
    commit/push/PR — the #518 pattern seen with small local models."""

    def test_brokers_canonical_publication_without_checkout_git(self):
        config = DispatchConfig()
        config.project.repo = "o/r"
        with (
            patch(
                f"{_P}._push_and_open_pr",
                return_value="https://github.com/o/r/pull/9",
            ) as publish,
            patch(f"{_P}.run") as mock_run,
        ):
            from kew.dispatch_core import _broker_agent_work

            brokered = _broker_agent_work(
                Path("/wt"),
                "agent/issue-9",
                "9",
                "Add x",
                config,
                base_sha="a" * 40,
            )

        assert brokered is True
        assert publish.call_args.kwargs["base_sha"] == "a" * 40
        mock_run.assert_not_called()

    def test_brokers_canonical_tree_when_agent_committed_first(self):
        config = DispatchConfig()
        config.project.repo = "o/r"
        with (
            patch(
                f"{_P}._push_and_open_pr",
                return_value="https://github.com/o/r/pull/9",
            ) as publish,
            patch(f"{_P}._commit_all_if_dirty") as commit,
        ):
            from kew.dispatch_core import _broker_agent_work

            brokered = _broker_agent_work(
                Path("/wt"), "agent/issue-9", "9", "T", config
            )

        assert brokered is True
        publish.assert_called_once()
        commit.assert_not_called()

    def test_noop_when_canonical_tree_is_unchanged(self):
        with (
            patch(f"{_P}._push_and_open_pr", return_value=None) as publish,
            patch(f"{_P}.run") as mock_run,
        ):
            from kew.dispatch_core import _broker_agent_work

            brokered = _broker_agent_work(Path("/wt"), "b", "9", "T", DispatchConfig())

        assert brokered is False
        publish.assert_called_once()
        mock_run.assert_not_called()


class TestBrokerFailureHandling:
    """The broker path fails safely: it never falsely completes or loses work."""

    def test_broker_raises_when_gh_pr_create_fails(self):
        def fake_run(cmd, **kwargs):
            rc = (
                1
                if cmd[:4] == ["gh", "api", "--method", "POST"]
                and cmd[4].endswith("/pulls")
                else 0
            )
            return MagicMock(returncode=rc, stdout="", stderr="boom")

        config = DispatchConfig()
        config.project.repo = "o/r"
        with (
            patch(f"{_P}._git_push_branch", return_value=MagicMock()),
            patch(f"{_P}.run", side_effect=fake_run),
        ):
            from kew.dispatch_core import _broker_agent_work
            from kew.publication import PublicationError

            with pytest.raises(
                PublicationError, match="GitHub REST PR create failed"
            ) as exc_info:
                _broker_agent_work(Path("/wt"), "b", "9", "T", config)

        assert exc_info.value.step == "pr_create"

    def test_retry_opens_pr_when_intended_tree_already_exists_remotely(
        self, tmp_path: Path
    ) -> None:
        """A failed PR request does not turn its pushed tree into no-change."""
        from kew.dispatch_core import _push_and_open_pr
        from kew.worktree import write_checkout_generation

        origin = tmp_path / "origin.git"
        checkout = tmp_path / "checkout"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], check=True)
        subprocess.run(
            ["git", "clone", str(origin), str(checkout)],
            check=True,
            capture_output=True,
        )
        for key, value in (
            ("user.name", "dispatch test"),
            ("user.email", "dispatch@example.invalid"),
        ):
            subprocess.run(["git", "config", key, value], cwd=checkout, check=True)
        (checkout / "base.txt").write_text("base\n")
        subprocess.run(["git", "add", "-A"], cwd=checkout, check=True)
        subprocess.run(
            ["git", "commit", "-m", "base"],
            cwd=checkout,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "push", "origin", "main"],
            cwd=checkout,
            check=True,
            capture_output=True,
        )
        base_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=checkout,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        subprocess.run(
            ["git", "checkout", "-b", "agent/issue-9"],
            cwd=checkout,
            check=True,
            capture_output=True,
        )
        (checkout / "feature.txt").write_text("feature\n")
        write_checkout_generation(checkout)

        from kew.run_store import SqliteRunStore

        config = DispatchConfig()
        config.project.repo = str(origin)
        no_changes: list[object] = []
        # The retry may resume only on a head a durable publication intent of
        # this item names (review round 3), so the store is real.
        store = SqliteRunStore(str(tmp_path / "runs.db"))
        create = MagicMock(
            side_effect=[
                RuntimeError("PR request failed"),
                ("https://example.invalid/pull/9", 9),
            ]
        )
        with (
            patch(f"{_P}._create_pull_request", create),
            patch(f"{_P}._apply_created_pr_labels"),
            pytest.raises(RuntimeError, match="PR request failed"),
        ):
            _push_and_open_pr(
                checkout,
                "agent/issue-9",
                "9",
                "Feature",
                config,
                base_sha=base_sha,
                on_no_change=no_changes.append,
                store=store,
                run_id="run-9",
            )

        first_remote = subprocess.run(
            ["git", "-C", str(origin), "rev-parse", "agent/issue-9"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        with (
            patch(f"{_P}._create_pull_request", create),
            patch(f"{_P}._apply_created_pr_labels"),
        ):
            url = _push_and_open_pr(
                checkout,
                "agent/issue-9",
                "9",
                "Feature",
                config,
                base_sha=base_sha,
                on_no_change=no_changes.append,
                store=store,
                run_id="run-9",
            )

        assert url == "https://example.invalid/pull/9"
        assert create.call_count == 2
        assert no_changes == []
        assert (
            subprocess.run(
                ["git", "-C", str(origin), "rev-parse", "agent/issue-9"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            == first_remote
        )
        store.close()

    def test_broker_propagates_canonical_publication_failure(self):
        from kew.dispatch_core import _broker_agent_work
        from kew.publication import PublicationError

        with (
            patch(
                f"{_P}._push_and_open_pr",
                side_effect=PublicationError("push", "canonical unavailable"),
            ),
            pytest.raises(PublicationError) as exc_info,
        ):
            _broker_agent_work(Path("/wt"), "b", "9", "T", DispatchConfig())

        assert exc_info.value.step == "push"
        assert "canonical unavailable" in exc_info.value.detail

    def test_broker_uses_publication_result_not_a_checkout_diff(self):
        config = DispatchConfig()
        config.project.repo = "o/r"
        with (
            patch(
                f"{_P}._push_and_open_pr",
                return_value="https://github.com/o/r/pull/9",
            ),
            patch(f"{_P}._get_changed_files") as changed,
        ):
            from kew.dispatch_core import _broker_agent_work

            brokered = _broker_agent_work(Path("/wt"), "b", "9", "T", config)

        assert brokered is True
        changed.assert_not_called()

    def test_finalize_skips_completion_when_pr_missing(self):
        result = _ok_result()
        with (
            patch(f"{_P}.check_pr_exists", return_value=None),
            patch(f"{_P}.update_pr_labels") as mock_labels,
            patch(f"{_P}.mark_completed") as mock_completed,
            patch(f"{_P}._auto_unblock_dependents") as mock_unblock,
            patch(f"{_P}._post_dispatch_report_fallback") as mock_report,
        ):
            from kew.dispatch_core import _finalize_brokered_pr

            _finalize_brokered_pr(
                9,
                "b",
                DispatchConfig(),
                item_id="9",
                complexity="medium",
                result=result,
            )

        mock_labels.assert_not_called()
        mock_completed.assert_not_called()
        mock_unblock.assert_not_called()
        mock_report.assert_not_called()

    def test_finalize_skips_completion_when_label_fails(self):
        result = _ok_result()
        with (
            patch(f"{_P}.check_pr_exists", return_value=42),
            patch(f"{_P}.update_pr_labels", return_value=False),
            patch(f"{_P}.mark_completed") as mock_completed,
            patch(f"{_P}._auto_unblock_dependents") as mock_unblock,
            patch(f"{_P}._post_dispatch_report_fallback") as mock_report,
        ):
            from kew.dispatch_core import _finalize_brokered_pr

            _finalize_brokered_pr(
                9,
                "b",
                DispatchConfig(),
                item_id="9",
                complexity="medium",
                result=result,
            )

        mock_completed.assert_not_called()
        mock_unblock.assert_not_called()
        mock_report.assert_not_called()

    def test_finalize_posts_report_after_label_and_completion(self):
        result = _ok_result()
        calls: list[str] = []
        with (
            patch(f"{_P}.check_pr_exists", return_value=42),
            patch(f"{_P}.update_pr_labels", return_value=True),
            patch(f"{_P}.mark_completed", side_effect=lambda *_: calls.append("mark")),
            patch(
                f"{_P}._auto_unblock_dependents",
                side_effect=lambda *_: calls.append("unblock"),
            ),
            patch(
                f"{_P}._post_dispatch_report_fallback",
                side_effect=lambda *_, **__: calls.append("report"),
            ) as mock_report,
        ):
            from kew.dispatch_core import _finalize_brokered_pr

            _finalize_brokered_pr(
                9,
                "b",
                DispatchConfig(),
                item_id="9",
                complexity="medium",
                result=result,
            )
        assert calls == ["mark", "unblock", "report"]
        assert mock_report.call_args.args == (42, "9", "medium", result, ANY)
        assert mock_report.call_args.kwargs == {"selected_model": None}

    def test_finalize_publishes_agent_report_and_selected_model(self):
        result = _ok_result(resolved_model="")
        result.run_id = "run-1"
        store = MagicMock()
        store.get.return_value.model = "gpt-5.6-sol"
        with (
            patch(f"{_P}.check_pr_exists", return_value=42),
            patch(f"{_P}.update_pr_labels", return_value=True),
            patch(f"{_P}.mark_completed"),
            patch(f"{_P}._auto_unblock_dependents"),
            patch(f"{_P}._post_agent_report", return_value=False) as post_agent,
            patch(f"{_P}._post_dispatch_report_fallback") as post_fallback,
        ):
            from kew.dispatch_core import _finalize_brokered_pr

            _finalize_brokered_pr(
                0,
                "agent/issue-sfd-158",
                DispatchConfig(),
                item_id="SFD-158",
                complexity="medium",
                result=result,
                store=store,
            )

        post_agent.assert_called_once_with("42", result.log_file, ANY, result=result)
        assert post_fallback.call_args.kwargs["selected_model"] == "gpt-5.6-sol"


class TestQualifiesForRescue:
    """Test the _qualifies_for_rescue predicate (max-steps trigger)."""

    def _result(self, stop_reason):
        from kew.tracking import DispatchResult

        r = DispatchResult(exit_code=0, issue_num=1, log_file=Path("/dev/null"))
        r.stop_reason = stop_reason
        return r

    def test_aborted_with_canonical_tree_changes_qualifies(self):
        import kew.dispatch_core as dc
        from kew.config import DispatchConfig

        with patch(f"{_P}._get_changed_files", return_value=["src/new.py"]):
            assert dc._qualifies_for_rescue(
                self._result("aborted"), Path("/tmp/wt"), DispatchConfig()
            )

    def test_aborted_with_deleted_path_qualifies(self):
        import kew.dispatch_core as dc
        from kew.config import DispatchConfig

        with patch(f"{_P}._get_changed_files", return_value=["docs/x.md"]):
            assert dc._qualifies_for_rescue(
                self._result("aborted"), Path("/tmp/wt"), DispatchConfig()
            )

    def test_aborted_but_clean_does_not_qualify(self):
        import kew.dispatch_core as dc
        from kew.config import DispatchConfig

        with patch(f"{_P}._get_changed_files", return_value=[]):
            assert not dc._qualifies_for_rescue(
                self._result("aborted"), Path("/tmp/wt"), DispatchConfig()
            )

    def test_non_aborted_stop_reasons_never_qualify(self):
        import kew.dispatch_core as dc
        from kew.config import DispatchConfig

        # even with changes present, only "aborted" (max-steps) qualifies
        with patch(f"{_P}._get_changed_files", return_value=["docs/x.md"]):
            for reason in ("error", "", "timeout", "stop", "tool_use"):
                assert not dc._qualifies_for_rescue(
                    self._result(reason), Path("/tmp/wt"), DispatchConfig()
                )


class TestRescueIncompleteRun:
    """Rescue incomplete-but-coherent runs into flagged draft PRs (#518)."""

    def test_linear_rescue_body_has_banner_and_bare_id_no_closes(self):
        import kew.dispatch_core as dc
        from kew.config import DispatchConfig

        config = DispatchConfig()
        with (
            patch(f"{_P}._commit_all_if_dirty", return_value=True),
            patch(f"{_P}._push_and_open_pr") as mock_push,
            patch(f"{_P}.check_pr_exists", return_value=77),
            patch(f"{_P}.update_pr_labels"),
            patch(f"{_P}.mark_needs_review_incomplete"),
        ):
            dc._rescue_incomplete_run(
                Path("/tmp/wt"), "agent/sfd-12", 0, "t", config, item_id="SFD-12"
            )

        body = mock_push.call_args[1].get("body", "")
        assert "max_steps" in body  # banner retained
        assert "Closes" not in body  # no GitHub marker for a Linear item
        assert body.rstrip().endswith("SFD-12")

    def test_opens_draft_pr_flags_issue_and_never_completes(self):
        import kew.dispatch_core as dc
        from kew.config import DispatchConfig

        config = DispatchConfig()
        with (
            patch(f"{_P}._commit_all_if_dirty", return_value=True) as mock_commit,
            patch(f"{_P}._push_and_open_pr") as mock_push,
            patch(f"{_P}.check_pr_exists", return_value=77) as mock_check_pr,
            patch(f"{_P}.update_pr_labels") as mock_pr_labels,
            patch(f"{_P}.mark_needs_review_incomplete") as mock_needs,
            patch(f"{_P}.mark_completed") as mock_completed,
            patch(f"{_P}._auto_unblock_dependents") as mock_unblock,
        ):
            dc._rescue_incomplete_run(
                Path("/tmp/wt"), "agent/issue-9", 9, "Fix #9", config, item_id="9"
            )

            # Commit called
            mock_commit.assert_called_once()

            # PR opened as draft with incomplete-run label
            mock_push.assert_called_once()
            push_kwargs = mock_push.call_args[1]
            assert push_kwargs.get("draft") is True
            assert "incomplete-run" in push_kwargs.get("extra_labels", ())

            # Body contains the banner text
            assert "max_steps" in push_kwargs.get("body", "")
            assert "Closes #9" in push_kwargs.get("body", "")

            # Real PR number resolved before re-applying the label
            mock_check_pr.assert_called_once_with("agent/issue-9", config)

            # Labels re-applied (belt-and-suspenders) against the resolved
            # PR number, NOT the issue number
            mock_pr_labels.assert_called_once_with(
                77, config, add=[config.labels.incomplete_run]
            )

            # Issue flagged needs-review
            mock_needs.assert_called_once_with(9, ANY)

            # Never marks completed or unblocks
            mock_completed.assert_not_called()
            mock_unblock.assert_not_called()

    def test_protected_output_refuses_before_rescue_commit(self, tmp_path):
        import kew.dispatch_core as dc
        from kew.config import DispatchConfig
        from kew.publication_credentials import CredentialPublicationRefused
        from kew.run_store import RunRecord, SqliteRunStore

        store = SqliteRunStore(str(tmp_path / "rescue.db"))
        store.save(
            RunRecord(
                run_id="legacy-contained",
                issue_num=9,
                item_id="9",
                branch="agent/issue-9",
                status="failure",
                exit_code=1,
                started_at=datetime(2026, 9, 5, tzinfo=UTC),
                container_name="kew-9",
            )
        )
        with (
            patch(f"{_P}._commit_all_if_dirty") as commit,
            patch(f"{_P}._push_and_open_pr") as publish,
            pytest.raises(CredentialPublicationRefused),
        ):
            dc._rescue_incomplete_run(
                tmp_path / "checkout",
                "agent/issue-9",
                9,
                "Fix #9",
                DispatchConfig(),
                item_id="9",
                store=store,
                run_id="legacy-contained",
            )

        commit.assert_not_called()
        publish.assert_not_called()

    def test_push_failure_raises_without_flagging(self):
        import kew.dispatch_core as dc
        from kew.config import DispatchConfig

        config = DispatchConfig()
        with (
            patch(f"{_P}._commit_all_if_dirty"),
            patch(
                f"{_P}._push_and_open_pr",
                side_effect=RuntimeError("push failed"),
            ),
            patch(f"{_P}.mark_needs_review_incomplete") as mock_needs,
        ):
            with pytest.raises(RuntimeError, match="push failed"):
                dc._rescue_incomplete_run(
                    Path("/tmp/wt"), "agent/issue-9", 9, "Fix #9", config, item_id="9"
                )

            # Issue NOT flagged needs-review if PR never opened
            mock_needs.assert_not_called()

    def test_post_pr_labeling_failure_lets_draft_stand(self):
        """A gh hiccup after the draft PR is open must not propagate (#518).

        Once the draft PR is confirmed open, any failure in the post-PR
        labeling steps (check_pr_exists, update_pr_labels,
        mark_needs_review_incomplete) is log-only: the draft stands rather
        than the caller falling through to mark_failed(agent-failed) and
        stranding an open draft PR under a mislabeled issue.
        """
        import kew.dispatch_core as dc
        from kew.config import DispatchConfig

        config = DispatchConfig()
        with (
            patch(f"{_P}._commit_all_if_dirty", return_value=True),
            patch(f"{_P}._push_and_open_pr"),
            patch(f"{_P}.check_pr_exists", return_value=77),
            patch(f"{_P}.update_pr_labels", return_value=True),
            patch(
                f"{_P}.mark_needs_review_incomplete",
                side_effect=RuntimeError("gh hiccup"),
            ),
        ):
            # Must NOT raise -- the draft PR is already open, so the
            # labeling failure is logged and swallowed.
            dc._rescue_incomplete_run(
                Path("/tmp/wt"), "agent/issue-9", 9, "Fix #9", config, item_id="9"
            )


class TestPrUrlCaptureAtCreation:
    """#648: gh pr create URL is captured + persisted to the run record."""

    def test_extract_pr_url_takes_last_url_line(self):
        from kew.dispatch_core import _extract_pr_url

        out = "Warning: 1 uncommitted change\nhttps://github.com/o/r/pull/42\n"
        assert _extract_pr_url(out) == "https://github.com/o/r/pull/42"
        assert _extract_pr_url("no url here") is None
        assert _extract_pr_url("") is None

    def test_push_and_open_pr_persists_url_when_store_given(self):
        from datetime import UTC, datetime

        from kew.dispatch_core import _push_and_open_pr
        from kew.run_store import RunRecord, SqliteRunStore

        store = SqliteRunStore(":memory:")
        store.save(
            RunRecord(
                run_id="r1",
                issue_num=9,
                branch="agent/issue-9",
                status="pending",
                exit_code=-1,
                started_at=datetime.now(UTC),
            )
        )

        def fake_run(cmd, **kwargs):
            if cmd[:4] == ["gh", "api", "--method", "POST"] and cmd[4].endswith(
                "/pulls"
            ):
                return MagicMock(
                    returncode=0,
                    stdout='{"number":42,"html_url":"https://github.com/o/r/pull/42"}',
                    stderr="",
                )
            return MagicMock(returncode=0, stdout="", stderr="")

        config = DispatchConfig()
        config.project.repo = "o/r"
        with (
            patch(f"{_P}.run", side_effect=fake_run),
            patch(f"{_P}._git_push_branch", return_value=MagicMock()),
        ):
            url = _push_and_open_pr(
                Path("/tmp/wt"),
                "agent/issue-9",
                "9",
                "Add glossary",
                config,
                store=store,
                run_id="r1",
            )

        assert url == "https://github.com/o/r/pull/42"
        assert store.get("r1").pr_url == "https://github.com/o/r/pull/42"

    def test_push_and_open_pr_returns_url_without_store(self):
        from kew.dispatch_core import _push_and_open_pr

        def fake_run(cmd, **kwargs):
            if cmd[:4] == ["gh", "api", "--method", "POST"] and cmd[4].endswith(
                "/pulls"
            ):
                return MagicMock(
                    returncode=0,
                    stdout='{"number":7,"html_url":"https://github.com/o/r/pull/7"}',
                    stderr="",
                )
            return MagicMock(returncode=0, stdout="", stderr="")

        config = DispatchConfig()
        config.project.repo = "o/r"
        with (
            patch(f"{_P}.run", side_effect=fake_run),
            patch(f"{_P}._git_push_branch", return_value=MagicMock()),
        ):
            url = _push_and_open_pr(Path("/tmp/wt"), "agent/issue-7", "7", "t", config)
        assert url == "https://github.com/o/r/pull/7"

    def test_checkout_escape_records_uncontained_publication_posture(self):
        from datetime import UTC, datetime

        from kew.dispatch_core import _push_and_open_pr
        from kew.run_store import RunRecord, SqliteRunStore

        store = SqliteRunStore(":memory:")
        store.save(
            RunRecord(
                run_id="r1",
                issue_num=9,
                branch="agent/issue-9",
                status="pending",
                exit_code=-1,
                started_at=datetime.now(UTC),
            )
        )
        config = DispatchConfig()
        config.project.repo = "o/r"
        config.publication.publish = "checkout"
        config.sandbox.dangerously_skip_containment = True

        def fake_run(cmd, **kwargs):
            stdout = (
                '{"number":9,"html_url":"https://github.com/o/r/pull/9"}'
                if cmd[:4] == ["gh", "api", "--method", "POST"]
                and cmd[4].endswith("/pulls")
                else ""
            )
            return MagicMock(returncode=0, stdout=stdout, stderr="")

        with (
            patch(f"{_P}.run", side_effect=fake_run),
            patch(f"{_P}._guard_checkout_output", autospec=True) as scan,
        ):
            _push_and_open_pr(
                Path("/tmp/wt"),
                "agent/issue-9",
                "9",
                "Fix",
                config,
                store=store,
                run_id="r1",
            )
        scan.assert_called_once_with(
            Path("/tmp/wt"),
            "agent/issue-9",
            repo="o/r",
            base_sha=None,
            base_branch="",
            store=store,
            run_id="r1",
        )

        [event] = store.list_audit_events(event="publication_posture")
        assert event.payload["posture"] == "uncontained-publication"

    def test_persist_pr_url_is_failsoft_and_noop_on_none(self):
        from datetime import UTC, datetime

        from kew.dispatch_core import _persist_pr_url
        from kew.run_store import RunRecord, SqliteRunStore

        store = SqliteRunStore(":memory:")
        store.save(
            RunRecord(
                run_id="r1",
                issue_num=1,
                branch="agent/issue-1",
                status="pending",
                exit_code=-1,
                started_at=datetime.now(UTC),
            )
        )
        # No-ops: missing store / run_id / url must not raise.
        _persist_pr_url(None, "r1", "https://x/pull/1")
        _persist_pr_url(store, None, "https://x/pull/1")
        _persist_pr_url(store, "r1", None)
        assert store.get("r1").pr_url is None
        # Happy path still writes.
        _persist_pr_url(store, "r1", "https://github.com/o/r/pull/1")
        assert store.get("r1").pr_url == "https://github.com/o/r/pull/1"

    def test_retry_guard_persists_discovered_pr_url(self, tmp_path):
        from kew.dispatch_core import run_tracked_agent
        from kew.run_store import SqliteRunStore

        store = SqliteRunStore(":memory:")
        config = DispatchConfig()
        config.project.repo = "o/r"
        config.retry.max_attempts = 2
        config.retry.backoff_base_seconds = 0
        config.breaker.enabled = False
        config.logging.dir = str(tmp_path / "logs")
        runner = MagicMock()
        runner.name = "claude-code"

        with (
            patch(f"{_P}.run_agent", return_value=_fail_result(issue_num=9)),
            patch(f"{_P}.check_pr_exists", return_value=99),
            patch(f"{_P}.update_pr_labels"),
        ):
            result = run_tracked_agent(
                allow_unrecorded=True,
                prompt="p",
                worktree_dir=tmp_path,
                config=config,
                issue_num=9,
                store=store,
                branch="agent/issue-9",
                runner=runner,
                model="claude-sonnet-4-6",
            )

        rec = store.get(result.run_id)
        assert rec is not None
        assert rec.pr_url == "https://github.com/o/r/pull/99"


# ── #649: cap-conflict warning + per-issue over-cap completion note ─────


class TestBudgetCapWarnings:
    """dispatch_core wiring of the CS-7 effective-budget warnings."""

    def _store(self, tmp_path):
        from kew.run_store import SqliteRunStore

        return SqliteRunStore(str(tmp_path / "runs.db"))

    def _record(self, issue_num: int, cost: float):
        from datetime import UTC, datetime

        from kew.run_store import RunRecord

        return RunRecord(
            run_id=f"r-{issue_num}-{cost}",
            issue_num=issue_num,
            branch=f"agent/issue-{issue_num}",
            status="success",
            exit_code=0,
            started_at=datetime.now(UTC),
            estimated_cost_usd=cost,
        )

    def test_cap_conflict_warns_on_first_run_under_low_cap(self, tmp_path):
        from kew.dispatch_core import _warn_if_cap_conflict

        store = self._store(tmp_path)
        config = DispatchConfig(
            agent={"max_budget_usd": 25.0}, budget={"per_issue_usd": 0.40}
        )
        with patch(f"{_P}._warn") as mock_warn:
            _warn_if_cap_conflict(config, store, issue_num=42)
        mock_warn.assert_called_once()
        assert "per_issue_usd" in mock_warn.call_args.args[0]
        store.close()

    def test_cap_conflict_silent_when_caps_aligned(self, tmp_path):
        from kew.dispatch_core import _warn_if_cap_conflict

        store = self._store(tmp_path)
        config = DispatchConfig(
            agent={"max_budget_usd": 0.20}, budget={"per_issue_usd": 15.0}
        )
        with patch(f"{_P}._warn") as mock_warn:
            _warn_if_cap_conflict(config, store, issue_num=42)
        mock_warn.assert_not_called()
        store.close()

    def test_cap_conflict_fail_soft_on_broken_store(self):
        from kew.dispatch_core import _warn_if_cap_conflict

        broken = MagicMock()
        broken.get_config_override.return_value = None
        broken.list_by_issue.side_effect = RuntimeError("boom")
        # per_issue explicitly set so the gate is passed and the broken store hit.
        config = DispatchConfig(budget={"per_issue_usd": 0.40})
        with patch(f"{_P}._warn") as mock_warn:
            _warn_if_cap_conflict(config, broken, issue_num=42)  # must not raise
        mock_warn.assert_not_called()

    @pytest.mark.parametrize("fleet_worker", [True, False])
    def test_fleet_worker_defers_cap_warning_to_scheduler_parent(
        self, fleet_worker: bool
    ) -> None:
        """A fleet worker never warns itself: the scheduler parent owns it."""
        from types import SimpleNamespace

        from kew.dispatch_core import _warn_if_scheduler_does_not_own_cap_warning

        ctx = SimpleNamespace(
            fleet_worker=fleet_worker,
            config=DispatchConfig(),
            store=MagicMock(),
            issue_num=42,
            item_id="42",
        )
        with patch(f"{_P}._warn_if_cap_conflict") as cap_warning:
            _warn_if_scheduler_does_not_own_cap_warning(ctx)  # type: ignore[arg-type]

        if fleet_worker:
            cap_warning.assert_not_called()
        else:
            cap_warning.assert_called_once_with(ctx.config, ctx.store, 42, "42")

    def test_over_cap_note_fires_after_run_exceeds_per_issue(self, tmp_path):
        from kew.dispatch_core import _note_if_over_per_issue

        store = self._store(tmp_path)
        store.save(self._record(42, 0.41))  # spend already over the cap
        config = DispatchConfig(budget={"per_issue_usd": 0.40})
        with patch(f"{_P}._warn") as mock_warn:
            _note_if_over_per_issue(config, store, issue_num=42)
        mock_warn.assert_called_once()
        msg = mock_warn.call_args.args[0]
        assert "#42" in msg and "next dispatch" in msg.lower()
        store.close()

    def test_over_cap_note_silent_when_within_cap(self, tmp_path):
        from kew.dispatch_core import _note_if_over_per_issue

        store = self._store(tmp_path)
        store.save(self._record(42, 0.10))
        config = DispatchConfig(budget={"per_issue_usd": 0.40})
        with patch(f"{_P}._warn") as mock_warn:
            _note_if_over_per_issue(config, store, issue_num=42)
        mock_warn.assert_not_called()
        store.close()


class TestBudgetBannerRender:
    """The four cli banner sites share one compact effective-budget block."""

    def test_banner_renders_shared_block(self, tmp_path):
        from kew.cli import _log_budget_banner
        from kew.run_store import SqliteRunStore

        store = SqliteRunStore(str(tmp_path / "runs.db"))
        config = DispatchConfig(
            agent={"max_budget_usd": 25.0},
            budget={"per_issue_usd": 0.40, "daily_usd": 100.0},
        )
        with patch("kew.cli._log") as mock_log:
            _log_budget_banner(config, store=store, issue=42)
        line = mock_log.call_args.args[0]
        assert line.startswith("Budget: run cap $25 (kew.toml)")
        assert "issue $0.00/$0.40" in line
        assert "today $0.00/$100" in line
        store.close()


class TestProjectBannerRender:
    """Every driver/dispatch command prints project=/tracker=/db= before acting (F1)."""

    def test_banner_reports_project_tracker_and_db(self, tmp_path, monkeypatch):
        from kew.cli import _log_project_banner

        project = tmp_path / "svc"
        project.mkdir()
        (project / "kew.toml").write_bytes(
            b'[tracker]\ntype = "github"\n[logging]\ndir = "logs/dispatch"\n'
        )
        monkeypatch.setenv("KEW_PROJECT", str(project))
        config = load_config()

        with patch("kew.cli._log") as mock_log:
            _log_project_banner(config)
        line = mock_log.call_args.args[0]
        assert line.startswith(f"project={project}")
        assert "tracker=github" in line
        assert f"db={(project / 'logs' / 'dispatch' / 'dispatch.db')}" in line

    def test_banner_db_matches_project_from_a_subdirectory(self, tmp_path, monkeypatch):
        """#1156: `db=` and `project=` are two halves of the same root."""
        from kew.cli import _log_project_banner

        project = (tmp_path / "svc").resolve()
        (project / ".git").mkdir(parents=True)
        (project / "kew.toml").write_bytes(
            b'[tracker]\ntype = "github"\n[logging]\ndir = "logs/dispatch"\n'
        )
        (project / "pkg").mkdir()
        monkeypatch.chdir(project / "pkg")
        config = load_config()

        with patch("kew.cli._log") as mock_log:
            _log_project_banner(config)
        line = mock_log.call_args.args[0]
        assert line.startswith(f"project={project} ")
        assert f"db={project / 'logs' / 'dispatch' / 'dispatch.db'}" in line

    def test_banner_never_raises(self, tmp_path):
        from kew.cli import _log_project_banner

        # A config whose logging.dir cannot resolve must not break the command.
        config = DispatchConfig(tracker={"type": "linear", "linear": {"team": "T"}})
        with patch("kew.cli.resolved_config_path", side_effect=RuntimeError("boom")):
            _log_project_banner(config)  # must not raise


class TestFallbackDispatchReport:
    """kew posts a machine-generated Dispatch Report when the agent didn't (#664).

    A headless run can finalize `success` while the agent's own wrap-up (its
    Dispatch Report comment) silently never ran (#663). After the PR is
    confirmed, kew fills the gap fail-soft.
    """

    def test_posts_when_no_agent_report_exists(self):
        from kew.dispatch_core import _post_dispatch_report_fallback

        calls: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            # gh pr view -> comment bodies (no report present)
            if "view" in cmd:
                return MagicMock(
                    returncode=0, stdout="just a chat comment\n", stderr=""
                )
            return MagicMock(returncode=0, stdout="", stderr="")

        with (
            patch(f"{_P}.run", side_effect=fake_run),
            patch(f"{_P}._log") as mock_log,
        ):
            _post_dispatch_report_fallback(
                7, "42", "medium", _ok_result(estimated_cost_usd=1.23), DispatchConfig()
            )

        comment_cmd = next(c for c in calls if c[:3] == ["gh", "pr", "comment"])
        body = comment_cmd[comment_cmd.index("--body") + 1]
        assert "## Dispatch Report" in body
        assert "#42" in body
        # The success-logging branch runs inside the fail-soft except, so a
        # broken logger there would be silently swallowed — assert it fired.
        assert any(
            "Posted fallback Dispatch Report" in str(c.args[0])
            for c in mock_log.call_args_list
        )

    def test_uses_selected_model_when_runner_did_not_report_one(self):
        from kew.dispatch_core import _fallback_report_body

        body = _fallback_report_body(
            "SFD-157",
            "simple",
            _ok_result(resolved_model=""),
            selected_model="gpt-5.6-sol",
        )

        assert "- Model: `gpt-5.6-sol`" in body

    def test_host_success_publishes_agent_report_before_fallback(self, tmp_path):
        from kew.dispatch_core import _mark_success_pr

        config = DispatchConfig()
        ctx = MagicMock(
            config=config,
            issue_num=0,
            item_id="SFD-157",
            complexity="simple",
        )
        ctx.store.get.return_value.model = "gpt-5.6-sol"
        result = _ok_result(log_file=tmp_path / "codex.jsonl")
        result.run_id = "run-1"

        with (
            patch(f"{_P}.update_pr_labels", return_value=True),
            patch(f"{_P}.mark_completed"),
            patch(f"{_P}._auto_unblock_dependents"),
            patch(f"{_P}._post_agent_report", return_value=False) as post_agent,
            patch(f"{_P}._post_dispatch_report_fallback") as post_fallback,
        ):
            _mark_success_pr(ctx, result, 7, False)

        post_agent.assert_called_once_with(
            "7",
            result.log_file,
            config,
            result=result,
        )
        assert post_fallback.call_args.kwargs["selected_model"] == "gpt-5.6-sol"

    def test_skips_when_agent_already_posted(self):
        from kew.dispatch_core import _post_dispatch_report_fallback

        calls: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            if "view" in cmd:
                return MagicMock(
                    returncode=0,
                    stdout='"## Dispatch Report\\n\\nagent wrote this"\n',
                    stderr="",
                )
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch(f"{_P}.run", side_effect=fake_run):
            _post_dispatch_report_fallback(
                7, "42", "medium", _ok_result(), DispatchConfig()
            )

        assert not any(c[:3] == ["gh", "pr", "comment"] for c in calls)

    def test_gh_errors_are_swallowed(self):
        from kew.dispatch_core import _post_dispatch_report_fallback

        def boom(cmd, **kwargs):
            raise RuntimeError("gh exploded")

        with patch(f"{_P}.run", side_effect=boom):
            # Must not raise — reporting is best-effort, never fatal to the run.
            _post_dispatch_report_fallback(
                7, "42", "medium", _ok_result(), DispatchConfig()
            )

    def test_comment_failure_warns_not_raises(self):
        from kew.dispatch_core import _post_dispatch_report_fallback

        def fake_run(cmd, **kwargs):
            if "view" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            return MagicMock(returncode=1, stdout="", stderr="no auth")

        with (
            patch(f"{_P}.run", side_effect=fake_run),
            patch(f"{_P}._warn") as mock_warn,
        ):
            _post_dispatch_report_fallback(
                7, "42", "medium", _ok_result(), DispatchConfig()
            )
        assert mock_warn.called


class TestResolveIssueNumber:
    """Store-first PR->item resolution with gh fallback (A6)."""

    def test_store_first_wins_over_body(self):
        from kew.dispatch_core import resolve_issue_number

        store = MagicMock()
        record = MagicMock()
        record.issue_num = 42
        store.find_run_by_branch.return_value = record
        # Body names a different issue; the store is canonical.
        assert resolve_issue_number("Closes #99", "agent/issue-7", store=store) == 42
        store.find_run_by_branch.assert_called_once_with("agent/issue-7")

    def test_falls_back_to_gh_when_no_run(self):
        from kew.dispatch_core import resolve_issue_number

        store = MagicMock()
        store.find_run_by_branch.return_value = None
        assert resolve_issue_number("Closes #99", "some-branch", store=store) == 99

    def test_falls_back_when_no_store(self):
        from kew.dispatch_core import resolve_issue_number

        assert resolve_issue_number("Closes #55", "b") == 55

    def test_zero_sentinel_run_falls_back(self):
        from kew.dispatch_core import resolve_issue_number

        # A Linear run carries issue_num=0 (sentinel) — not a GitHub number.
        store = MagicMock()
        record = MagicMock()
        record.issue_num = 0
        store.find_run_by_branch.return_value = record
        assert resolve_issue_number("Fixes #13", "agent/SFD-12", store=store) == 13

    def test_no_branch_skips_store(self):
        from kew.dispatch_core import resolve_issue_number

        store = MagicMock()
        assert resolve_issue_number("Closes #8", "", store=store) == 8
        store.find_run_by_branch.assert_not_called()

    def test_returns_none_when_nothing_resolves(self):
        from kew.dispatch_core import resolve_issue_number

        store = MagicMock()
        store.find_run_by_branch.return_value = None
        assert resolve_issue_number("no marker", "no-digits", store=store) is None


class TestSandboxFinalizeOrdering:
    """Terminal 'success' is recorded only after the host broker lands the PR
    (#SFD-152): a push/PR failure must leave a failure row, never a 'success'
    row with no PR."""

    def _config(self, tmp_path: Path) -> DispatchConfig:
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        return config

    @patch("kew.dispatch_core._post_agent_report")
    @patch("kew.dispatch_core._push_and_open_pr")
    @patch("kew.dispatch_core._stream_sandbox_log")
    @patch("kew.dispatch_core._launch_container")
    def test_publication_hold_returns_a_successful_implementation_result(
        self, _mock_launch, mock_stream, mock_push, mock_report, tmp_path
    ):
        from kew.dispatch_core import _run_sandboxed_container
        from kew.publication_canonical import PublicationHeld
        from kew.run_store import SqliteRunStore

        mock_stream.return_value = 0
        mock_push.side_effect = PublicationHeld(("workflow.yml: executable config",))
        store = SqliteRunStore(":memory:")

        result = _run_sandboxed_container(
            prompt="compiled prompt",
            checkout_dir=tmp_path,
            config=self._config(tmp_path),
            image=SANDBOX_IMAGE,
            runner=SANDBOX_RUNNER,
            issue_num=7,
            item_id="7",
            branch="agent/issue-7",
            title="Fix it",
            model="claude-sonnet-4-6",
            store=store,
        )

        assert result.exit_code == 0
        assert result.error_category == "publication_held"
        assert "workflow.yml" in result.subtype
        assert store.list_by_issue(7)[0].status == "awaiting_publication"
        assert store.list_by_issue(7)[0].error_category == "publication_review_required"
        mock_report.assert_not_called()

    @patch("kew.dispatch_core._post_agent_report")
    @patch("kew.dispatch_core._push_and_open_pr")
    @patch("kew.dispatch_core._stream_sandbox_log")
    @patch("kew.dispatch_core._launch_container")
    def test_contained_row_carries_the_origin_resolved_repo(
        self, _mock_launch, mock_stream, _mock_push, _mock_report, tmp_path
    ):
        """The contained path stamps the binding the host path stamps (#2097).

        A detached canonical publication never reads its worktree origin, so
        ``runs.repo`` is the only slug it has; a contained dispatch that left
        the column NULL also made the two paths disagree on the column
        stop-label supersede matches run ownership on.
        """
        from kew.dispatch_core import _run_sandboxed_container
        from kew.run_store import SqliteRunStore

        mock_stream.return_value = 0
        config = self._config(tmp_path)
        config.project.repo = ""  # "auto-detected from gh"
        store = SqliteRunStore(":memory:")

        with patch(f"{_P}._repo_slug_from_origin", return_value="octo/widgets"):
            _run_sandboxed_container(
                prompt="compiled prompt",
                checkout_dir=tmp_path,
                config=config,
                image=SANDBOX_IMAGE,
                runner=SANDBOX_RUNNER,
                issue_num=7,
                item_id="7",
                branch="agent/issue-7",
                title="Fix it",
                model="claude-sonnet-4-6",
                store=store,
            )

        [row] = store.list_by_issue(7)
        assert row.repo == "octo/widgets"
        identity = store.get_admission_identity(row.run_id)
        assert identity.delivery_repo == "octo/widgets"

    def test_publication_hold_category_routes_to_deferral(self, tmp_path):
        import kew.dispatch_core as dispatch_core

        result = DispatchResult(
            exit_code=0,
            issue_num=7,
            log_file=tmp_path / "run.log",
            error_category="publication_held",
            subtype="workflow.yml: executable config",
        )
        ctx = MagicMock(spec=dispatch_core._DispatchContext)
        ctx.checkout_generation = ""
        deferred = MagicMock()
        with patch(f"{_P}._defer_publication", return_value=deferred) as defer:
            outcome = dispatch_core._collect_special_category(
                ctx, result, tmp_path, True
            )

        assert outcome is deferred
        held = defer.call_args.args[3]
        assert held.flags == ("workflow.yml: executable config",)

    def test_credential_refusal_cannot_enter_failure_rescue(self, tmp_path):
        import kew.dispatch_core as dispatch_core
        from kew.publication_credentials import CREDENTIAL_PUBLICATION_REFUSAL

        result = DispatchResult(
            exit_code=1,
            issue_num=7,
            log_file=tmp_path / "run.log",
            run_id="run-7",
            error_category=CREDENTIAL_PUBLICATION_REFUSAL,
        )
        ctx = MagicMock(spec=dispatch_core._DispatchContext)
        ctx.checkout_generation = ""
        with (
            patch(f"{_P}._root_checkout_integrity_check", return_value=None),
            patch(f"{_P}._collect_hard_fail_or_rescue") as rescue,
        ):
            outcome = dispatch_core._collect_result(
                ctx,
                result=result,
                worktree_dir=tmp_path / "preserved",
                run_sandboxed_path=True,
            )

        assert outcome.exit_code == 1
        assert outcome.changed_files == []
        assert outcome.run_id == "run-7"
        rescue.assert_not_called()

    @patch("kew.dispatch_core._post_agent_report")
    @patch("kew.dispatch_core._push_and_open_pr")
    @patch("kew.dispatch_core._stream_sandbox_log")
    @patch("kew.dispatch_core._launch_container")
    def test_shadow_event_is_correlated_to_the_real_run(
        self, _mock_launch, mock_stream, mock_push, _mock_report, tmp_path
    ):
        from kew.dispatch_core import _run_sandboxed_container
        from kew.publication_shadow import STATUS_MATCH, ShadowOutcome
        from kew.run_store import SqliteRunStore

        mock_stream.return_value = 0
        mock_push.return_value = "https://github.com/x/y/pull/1"
        config = self._config(tmp_path)
        config.project.repo = "x/y"
        config.publication.publish = "checkout"
        config.sandbox.dangerously_skip_containment = True
        config.publication.shadow = True
        store = SqliteRunStore(":memory:")
        with (
            patch(
                "kew.publication_shadow_hook.resolve_shadow_inputs",
                return_value=("a" * 40, "b" * 40),
            ),
            patch(
                "kew.publication_shadow_hook.compare_publication",
                return_value=ShadowOutcome(status=STATUS_MATCH),
            ),
            patch(
                "kew.dispatch_sandbox.orchestrator_provenance",
                return_value={
                    "orchestrator_version": "test",
                    "orchestrator_commit": None,
                    "orchestrator_dirty": None,
                },
            ),
        ):
            _run_sandboxed_container(
                prompt="compiled prompt",
                checkout_dir=tmp_path,
                config=config,
                image=SANDBOX_IMAGE,
                runner=SANDBOX_RUNNER,
                issue_num=7,
                item_id="SFD-7",
                branch="agent/SFD-7",
                title="Fix it",
                model="claude-sonnet-4-6",
                store=store,
                tracker="linear",
            )

        run = store.list_by_issue(7)[0]
        event = store.list_audit_events(event="publication_shadow_outcome")[0]
        assert event.run_id == run.run_id
        assert event.issue_num == 7
        assert event.item_id == "SFD-7"
        assert event.repo == "x/y"
        assert event.payload["branch"] == "agent/SFD-7"
        assert event.payload["runner"] == SANDBOX_RUNNER

    @patch("kew.dispatch_core._post_agent_report")
    @patch("kew.dispatch_core._push_and_open_pr")
    @patch("kew.dispatch_core._stream_sandbox_log")
    @patch("kew.dispatch_core._launch_container")
    def test_shadow_audit_failure_does_not_stop_publication(
        self, _mock_launch, mock_stream, mock_push, mock_report, tmp_path
    ):
        from kew.dispatch_core import _run_sandboxed_container
        from kew.publication_shadow import STATUS_MATCH, ShadowOutcome
        from kew.run_store import SqliteRunStore

        mock_stream.return_value = 0
        mock_push.return_value = "https://github.com/x/y/pull/1"
        config = self._config(tmp_path)
        config.project.repo = "x/y"
        config.publication.shadow = True
        store = SqliteRunStore(":memory:")
        with (
            patch.object(
                store,
                "append_audit_event_once",
                side_effect=OSError("ledger unavailable"),
            ),
            patch(
                "kew.publication_shadow_hook.resolve_shadow_inputs",
                return_value=("a" * 40, "b" * 40),
            ),
            patch(
                "kew.publication_shadow_hook.compare_publication",
                return_value=ShadowOutcome(status=STATUS_MATCH),
            ),
        ):
            result = _run_sandboxed_container(
                prompt="compiled prompt",
                checkout_dir=tmp_path,
                config=config,
                image=SANDBOX_IMAGE,
                runner=SANDBOX_RUNNER,
                issue_num=7,
                item_id="7",
                branch="agent/issue-7",
                title="Fix it",
                model="claude-sonnet-4-6",
                store=store,
            )

        mock_push.assert_called_once()
        mock_report.assert_called_once()
        assert result.exit_code == 0

    @patch("kew.dispatch_core._post_agent_report")
    @patch("kew.dispatch_core._push_and_open_pr")
    @patch("kew.dispatch_core._stream_sandbox_log")
    @patch("kew.dispatch_core._launch_container")
    def test_push_failure_finalizes_failure_not_success(
        self, _mock_launch, mock_stream, mock_push, mock_report, tmp_path
    ):
        from kew.dispatch_core import _run_sandboxed_container
        from kew.run_store import SqliteRunStore

        mock_stream.return_value = 0  # agent exited 0
        mock_push.side_effect = RuntimeError("gh pr create failed")

        store = SqliteRunStore(":memory:")
        # The implementation finished; the publication failure rides on the
        # result so the host defers it and preserves the work instead of
        # raising past the collector (review round 16 of #1440).
        result = _run_sandboxed_container(
            prompt="compiled prompt",
            checkout_dir=tmp_path,
            config=self._config(tmp_path),
            image=SANDBOX_IMAGE,
            runner=SANDBOX_RUNNER,
            issue_num=7,
            item_id="7",
            branch="agent/issue-7",
            title="Fix it",
            model="claude-sonnet-4-6",
            store=store,
        )
        assert result.error_category == "sandbox_push_failed"
        assert result.subtype.startswith("pr_create\n")

        records = store.list_by_issue(7)
        assert len(records) == 1
        rec = records[0]
        # Parked durably before the sandbox returns (review round 17).
        assert rec.status == "awaiting_publication"
        assert rec.finished_at is not None
        # The report is never published when the broker failed.
        mock_report.assert_not_called()

    @patch("kew.dispatch_core._post_agent_report")
    @patch("kew.dispatch_core._push_and_open_pr")
    @patch("kew.dispatch_core._stream_sandbox_log")
    @patch("kew.dispatch_core._launch_container")
    def test_final_publication_guard_returns_a_dedicated_refusal(
        self, _mock_launch, mock_stream, mock_push, mock_report, tmp_path
    ):
        from kew.dispatch_core import _run_sandboxed_container
        from kew.publication_credentials import (
            CREDENTIAL_PUBLICATION_REFUSAL,
            CredentialPublicationRefused,
        )
        from kew.run_store import SqliteRunStore

        mock_stream.return_value = 0
        mock_push.side_effect = CredentialPublicationRefused("credential_in_output")
        store = SqliteRunStore(":memory:")

        result = _run_sandboxed_container(
            prompt="compiled prompt",
            checkout_dir=tmp_path,
            config=self._config(tmp_path),
            image=SANDBOX_IMAGE,
            runner=SANDBOX_RUNNER,
            issue_num=7,
            item_id="7",
            branch="agent/issue-7",
            title="Fix it",
            model="claude-sonnet-4-6",
            store=store,
        )

        record = store.list_by_issue(7)[0]
        assert result.exit_code == 1
        assert result.error_category == CREDENTIAL_PUBLICATION_REFUSAL
        assert record.status == "refused"
        assert record.error_category == CREDENTIAL_PUBLICATION_REFUSAL
        mock_push.assert_called_once()
        mock_report.assert_not_called()

    @patch("kew.dispatch_core._post_agent_report")
    @patch("kew.dispatch_core._push_and_open_pr")
    @patch("kew.dispatch_core._stream_sandbox_log")
    @patch("kew.dispatch_core._launch_container")
    def test_success_finalized_only_after_broker(
        self, _mock_launch, mock_stream, mock_push, mock_report, tmp_path
    ):
        from kew.dispatch_core import _run_sandboxed_container
        from kew.run_store import SqliteRunStore

        mock_stream.return_value = 0
        mock_push.return_value = "https://github.com/x/y/pull/1"

        # The row must still be 'pending' at the moment the broker is invoked —
        # success is recorded only after the push+PR succeeds.
        observed: dict[str, str | None] = {}

        def _capture_status(*_a, **_k):
            recs = store.list_by_issue(7)
            observed["status"] = recs[0].status if recs else None
            return "https://github.com/x/y/pull/1"

        mock_push.side_effect = _capture_status

        store = SqliteRunStore(":memory:")
        result = _run_sandboxed_container(
            prompt="compiled prompt",
            checkout_dir=tmp_path,
            config=self._config(tmp_path),
            image=SANDBOX_IMAGE,
            runner=SANDBOX_RUNNER,
            issue_num=7,
            item_id="7",
            branch="agent/issue-7",
            title="Fix it",
            model="claude-sonnet-4-6",
            store=store,
        )

        assert observed["status"] == "pending"
        records = store.list_by_issue(7)
        assert records[0].status == "success"
        assert result.exit_code == 0
        mock_report.assert_called_once()

    @patch("kew.dispatch_sandbox.run_shadow_comparison")
    @patch("kew.dispatch_core._post_agent_report")
    @patch("kew.dispatch_core._push_and_open_pr")
    @patch("kew.dispatch_core._stream_sandbox_log")
    @patch("kew.dispatch_core._launch_container")
    def test_shadow_runs_before_the_push_and_observes_it(
        self, _mock_launch, mock_stream, mock_push, _mock_report, mock_shadow, tmp_path
    ):
        """Shadow mode must observe the publication that is ABOUT to happen.

        Run it after the push and it describes something already done; run it
        before the zero-change check and there may be nothing to compare. The
        order is the whole point, so it is asserted rather than assumed.
        """
        from kew.dispatch_core import _run_sandboxed_container
        from kew.run_store import SqliteRunStore

        mock_stream.return_value = 0
        order: list[str] = []
        mock_shadow.side_effect = lambda *a, **k: order.append("shadow")
        mock_push.side_effect = lambda *a, **k: (
            order.append("push"),
            "https://github.com/x/y/pull/1",
        )[1]

        _run_sandboxed_container(
            prompt="compiled prompt",
            checkout_dir=tmp_path,
            config=self._config(tmp_path),
            image=SANDBOX_IMAGE,
            runner=SANDBOX_RUNNER,
            issue_num=7,
            item_id="7",
            branch="agent/issue-7",
            title="Fix it",
            model="claude-sonnet-4-6",
            store=SqliteRunStore(":memory:"),
        )

        assert order == ["shadow", "push"]

    @patch("kew.dispatch_sandbox.run_shadow_comparison")
    @patch("kew.dispatch_core._post_agent_report")
    @patch("kew.dispatch_core._push_and_open_pr")
    @patch("kew.dispatch_core._stream_sandbox_log")
    @patch("kew.dispatch_core._launch_container")
    def test_a_shadow_that_explodes_does_not_stop_the_publication(
        self, _mock_launch, mock_stream, mock_push, mock_report, mock_shadow, tmp_path
    ):
        """The property the whole feature rests on.

        ``run_shadow_comparison`` is contracted never to raise, and its own
        tests prove that. This asserts the dispatch survives even if that
        contract is ever broken — because the cost of being wrong is a run that
        did the work and lost the PR, and "an observability feature ate my
        publication" is the one outcome that would justify never turning it on.
        """
        from kew.dispatch_core import _run_sandboxed_container
        from kew.run_store import SqliteRunStore

        mock_stream.return_value = 0
        mock_push.return_value = "https://github.com/x/y/pull/1"
        mock_shadow.side_effect = RuntimeError("shadow blew up")

        store = SqliteRunStore(":memory:")
        result = _run_sandboxed_container(
            prompt="compiled prompt",
            checkout_dir=tmp_path,
            config=self._config(tmp_path),
            image=SANDBOX_IMAGE,
            runner=SANDBOX_RUNNER,
            issue_num=7,
            item_id="7",
            branch="agent/issue-7",
            title="Fix it",
            model="claude-sonnet-4-6",
            store=store,
        )

        mock_push.assert_called_once()
        mock_report.assert_called_once()
        assert result.exit_code == 0
        assert store.list_by_issue(7)[0].status == "success"

    @patch("kew.dispatch_core._post_agent_report")
    @patch("kew.dispatch_core._push_and_open_pr")
    @patch("kew.dispatch_core._stream_sandbox_log")
    @patch("kew.dispatch_core._launch_container")
    def test_zero_change_exits_clean_not_push_failure(
        self, _mock_launch, mock_stream, mock_push, mock_report, tmp_path
    ):
        """A canonical no-op resolves as ``sandbox_no_change_needed``."""
        from kew.dispatch_core import _run_sandboxed_container
        from kew.run_store import SqliteRunStore

        mock_stream.return_value = 0  # agent exited 0
        mock_push.return_value = None

        store = SqliteRunStore(":memory:")
        result = _run_sandboxed_container(
            prompt="compiled prompt",
            checkout_dir=tmp_path,
            config=self._config(tmp_path),
            image=SANDBOX_IMAGE,
            runner=SANDBOX_RUNNER,
            issue_num=7,
            item_id="7",
            branch="agent/issue-7",
            title="Fix it",
            model="claude-sonnet-4-6",
            store=store,
        )

        records = store.list_by_issue(7)
        assert len(records) == 1
        rec = records[0]
        # Exit 0 — clean, not a failure
        assert result.exit_code == 0
        assert rec.status == "success"
        assert rec.error_category == "sandbox_no_change_needed"
        assert rec.finished_at is not None
        # Canonical publication proves that the extracted tree is unchanged.
        mock_push.assert_called_once()
        # The agent report is NOT published when there is no PR to comment on
        mock_report.assert_not_called()

    @patch("kew.dispatch_core._post_agent_report")
    @patch("kew.dispatch_core._push_and_open_pr")
    @patch("kew.dispatch_core._stream_sandbox_log")
    @patch("kew.dispatch_core._launch_container")
    def test_needs_human_stop_is_never_published(
        self, _mock_launch, mock_stream, mock_push, mock_report, tmp_path
    ):
        """#2601: a contained agent that ends ``KEW_NEEDS_HUMAN: <reason>``
        exits 0. The contained fold never read the marker, so the change it
        refused was pushed. The real fold now records the stop, and the run
        finalizes as needs_human before publication is reached."""
        from kew.dispatch_core import _collect_result, _run_sandboxed_container
        from kew.dispatch_sandbox import sandbox_log_fold
        from kew.run_store import SqliteRunStore

        stop = "KEW_NEEDS_HUMAN: policy gate failed: tests/test_x.py::test_y"
        lines = [
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [{"type": "tool_use", "name": "Bash", "input": {}}]
                    },
                }
            ),
            json.dumps(
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": stop}]},
                }
            ),
            json.dumps({"type": "result", "subtype": "success", "result": stop}),
        ]

        def real_fold(_stream, log_file, store, run_id, **kwargs):
            kwargs["runner"] = "claude-code"
            with sandbox_log_fold(log_file, store, run_id, **kwargs) as fold:
                for line in lines:
                    fold(line)
            return 0

        mock_stream.side_effect = real_fold
        mock_push.return_value = "https://github.com/x/y/pull/1"

        store = SqliteRunStore(":memory:")
        result = _run_sandboxed_container(
            prompt="compiled prompt",
            checkout_dir=tmp_path,
            config=self._config(tmp_path),
            image=SANDBOX_IMAGE,
            runner=SANDBOX_RUNNER,
            issue_num=7,
            item_id="7",
            branch="agent/issue-7",
            title="Fix it",
            model="claude-sonnet-4-6",
            store=store,
        )

        mock_push.assert_not_called()
        mock_report.assert_not_called()
        assert result.error_category == "needs_human"
        assert result.subtype == "policy gate failed: tests/test_x.py::test_y"
        rec = store.list_by_issue(7)[0]
        assert rec.status == "failure"
        assert rec.error_category == "needs_human"

        with (
            patch(
                "kew.dispatch_core._root_checkout_integrity_check", return_value=None
            ),
            patch("kew.dispatch_core._collect_needs_human") as human,
            patch("kew.dispatch_core._collect_success") as publish,
        ):
            ctx = MagicMock()
            ctx.checkout_generation = ""
            _collect_result(
                ctx, result=result, worktree_dir=tmp_path, run_sandboxed_path=True
            )
        publish.assert_not_called()
        human.assert_called_once()


class TestRepoMapStabilityGate:
    """The shared seam enforces ``repo_map_mode``: "stable" zeroes the caller's
    hint at the ONE gate all three call sites share — the exact behavior the
    #1042/#1045 semantic merge broke (F821 ``ctx`` inside the seam)."""

    def _repo(self, tmp_path):
        (tmp_path / "alpha_module.py").write_text("def alpha_fn():\n    return 1\n")
        (tmp_path / "zeta_module.py").write_text("def zeta_fn():\n    return 2\n")
        return tmp_path

    def test_stable_mode_ignores_caller_hint(self, tmp_path):
        from kew.dispatch_core import build_repo_map
        from kew.repomap import generate_repo_map

        repo = self._repo(tmp_path)
        config = DispatchConfig()
        config.prompt.repo_map_mode = "stable"
        # Tight budget so a forwarded hint WOULD reorder selection — only a
        # truly-zeroed hint yields identical output for opposing hints, so the
        # equality below proves the gate drops the hint rather than the budget
        # being loose enough to fit both files regardless.
        config.prompt.repo_map_tokens = 30
        a = build_repo_map(config, repo, hint="alpha alpha_fn")
        b = build_repo_map(config, repo, hint="zeta zeta_fn")
        assert a == b
        assert a != ""
        # Pin the equality to hint-zeroing: at this budget the empty-hint
        # generator matches, and a hinted generator diverges.
        assert a == generate_repo_map(repo, max_tokens=30, hint="")
        assert a != generate_repo_map(repo, max_tokens=30, hint="zeta zeta_fn")

    def test_hint_mode_passes_caller_hint_through(self, tmp_path):
        from kew.dispatch_core import build_repo_map
        from kew.repomap import generate_repo_map

        repo = self._repo(tmp_path)
        config = DispatchConfig()
        assert config.prompt.repo_map_mode == "hint"
        out = build_repo_map(config, repo, hint="zeta zeta_fn")
        assert out == generate_repo_map(
            repo, max_tokens=config.prompt.repo_map_tokens, hint="zeta zeta_fn"
        )


def test_tracked_codex_preflight_failure_terminalizes_without_spawn(tmp_path):
    from unittest.mock import patch

    from kew.config import DispatchConfig
    from kew.dispatch_core import run_tracked_agent
    from kew.run_store import SqliteRunStore
    from kew.runners.codex import CodexRunner

    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text("forbidden = true")
    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")
    config.retry.max_attempts = 1
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        with (
            patch("kew.orchestrator.popen") as mock_popen,
            patch("kew.orchestrator.find_timeout_cmd", return_value=None),
        ):
            result = run_tracked_agent(
                allow_unrecorded=True,
                prompt="fix",
                worktree_dir=tmp_path,
                config=config,
                issue_num=120,
                store=store,
                title="Synthetic Codex preflight",
                branch="agent/issue-120",
                runner=CodexRunner(),
                model="gpt-5.6-sol",
            )
        mock_popen.assert_not_called()
        records = store.list_by_issue(120)
        assert len(records) == 1
        assert records[0].status == "failure"
        assert records[0].error_category == "exception"
        assert result.exit_code == 1
        assert result.error_category == "exception"


@pytest.mark.usefixtures("admission_owner")
class TestDispatchReservationBinding:
    @pytest.mark.parametrize("sandboxed", [False, True], ids=["host", "contained"])
    @pytest.mark.parametrize(
        "category", ["checkout_identity_changed", "credential_publication_refused"]
    )
    def test_owned_dispatch_refusal_reconciles_immediately(
        self, tmp_path, monkeypatch, sandboxed, category
    ):
        from datetime import timedelta

        from kew import dispatch_core, dispatch_sandbox, worktree
        from kew.audit_chain import verify_chain
        from kew.cli.epic import _report_child_refusal
        from kew.delegate.run import _mark_item_gone
        from kew.graph.driver.tick import (
            reconcile_epic_reservations,
            resolve_reservation_run,
        )
        from kew.publication_credentials import CredentialPublicationRefused
        from kew.run_store import RunRecord, SqliteRunStore
        from kew.runners import get_runner

        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        config.retry.max_attempts = 1
        # Setup only: the branch's checkout generation binds to a contained
        # .git directory, so the fixture checkout needs one before minting.
        (tmp_path / ".git").mkdir()
        generation = worktree.write_checkout_generation(tmp_path)
        started = datetime.now(UTC)
        launched = MagicMock(side_effect=AssertionError("paid launch must not run"))
        monkeypatch.setattr(dispatch_core, "run_agent", launched)
        monkeypatch.setattr(dispatch_sandbox, "run_container_and_stream", launched)
        monkeypatch.setattr(
            dispatch_core, "_check_existing_pr", lambda *_: (False, None)
        )

        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            assert store.reserve_epic_node_item("alpha", "42", "reservation")
            [reservation] = store.list_epic_reservations("alpha")
            admit = store.admit_run_with_identity
            pending_ids = []

            def admit_and_race(record, *args, **kwargs):
                admitted = admit(record, *args, **kwargs)
                assert admitted
                if record.status != "pending":
                    return admitted
                pending_ids.append(record.run_id)
                assert record.run_id != reservation.run_id
                assert store.get(record.run_id).status == "pending"
                for kind in ("resolve", "review"):
                    store.save(
                        RunRecord(
                            run_id=f"unowned-{kind}",
                            issue_num=42,
                            item_id="42",
                            branch=record.branch,
                            run_kind=kind,
                            status="refused",
                            error_category="credential_publication_refused",
                            exit_code=1,
                            started_at=datetime.now(UTC),
                            finished_at=datetime.now(UTC),
                        )
                    )
                _mark_item_gone(store, "42")
                assert (
                    resolve_reservation_run(store, reservation).run_id == record.run_id
                )
                assert reconcile_epic_reservations(
                    "alpha",
                    store,
                    config,
                    now=reservation.reserved_at + timedelta(seconds=1),
                ) == {"42"}
                assert not store.reserve_epic_node_item("alpha", "42", "second")
                return admitted

            monkeypatch.setattr(store, "admit_run_with_identity", admit_and_race)

            def refuse_host_launch():
                if category == "checkout_identity_changed":
                    dispatch_core._require_checkout_generation(
                        tmp_path, "replaced-generation", boundary="test paid launch"
                    )
                raise CredentialPublicationRefused("contained_profile_missing")

            def launch():
                if sandboxed:
                    if category == "credential_publication_refused":
                        # A contained launch with no recorded auth path is
                        # refused at admission (contained_auth_evidence_missing).
                        monkeypatch.setenv(
                            "ANTHROPIC_API_KEY", "synthetic-provider-key"
                        )
                        monkeypatch.setattr(
                            dispatch_sandbox,
                            "_contained_auth_path",
                            lambda runner_name: None,
                        )
                    return dispatch_sandbox._run_sandboxed_container(
                        prompt="p",
                        checkout_dir=tmp_path,
                        config=config,
                        image=SANDBOX_IMAGE,
                        runner=SANDBOX_RUNNER,
                        issue_num=42,
                        item_id="42",
                        branch="agent/issue-42",
                        title="t",
                        model="claude-sonnet-4-6",
                        store=store,
                        epic_ref="alpha",
                        checkout_generation=(
                            "replaced-generation"
                            if category == "checkout_identity_changed"
                            else generation
                        ),
                    )
                return dispatch_core.run_tracked_agent(
                    prompt="p",
                    worktree_dir=tmp_path,
                    config=config,
                    issue_num=42,
                    item_id="42",
                    store=store,
                    branch="agent/issue-42",
                    runner=get_runner(SANDBOX_RUNNER),
                    model="claude-sonnet-4-6",
                    epic_ref="alpha",
                    paid_launch_check=refuse_host_launch,
                )

            if category == "checkout_identity_changed":
                with pytest.raises(dispatch_core._CheckoutIdentityRefused):
                    launch()
            else:
                result = launch()
                assert result.error_category == category
            [child_id] = pending_ids
            child = store.get(child_id)
            assert child.status == "refused"
            assert child.error_category == category
            _report_child_refusal(
                store,
                config,
                epic_ref="alpha",
                item_id="42",
                detail=category,
                exit_code=1,
                since=started,
            )
            assert store.get(reservation.run_id) is None
            assert resolve_reservation_run(store, reservation) == child
            assert (
                reconcile_epic_reservations(
                    "alpha",
                    store,
                    config,
                    now=reservation.reserved_at + timedelta(seconds=2),
                )
                == set()
            )
            assert store.list_epic_reservations("alpha") == []
            assert (
                reconcile_epic_reservations(
                    "alpha",
                    store,
                    config,
                    now=reservation.reserved_at + timedelta(seconds=601),
                )
                == set()
            )
            assert store.get(reservation.run_id) is None
            assert store.get(child_id) == child
            assert all(
                r.error_category != "dispatch_child_died"
                for r in store.list_by_item("42")
            )
            [binding] = store.list_audit_events(
                event="dispatch_reservation_bound", run_id=child_id
            )
            assert binding.payload == {
                "reservation_run_id": "reservation",
                "pinned": False,
                "epic_ref": "alpha",
                "item_id": "42",
            }
            assert verify_chain(store.read_chain_rows()).ok
        launched.assert_not_called()

    @pytest.mark.parametrize("failure", ["ledger", "replaced_generation"])
    def test_dispatch_binding_failure_rolls_back_admission(
        self, tmp_path, monkeypatch, failure
    ):
        from kew import dispatch_core
        from kew.run_store import SqliteRunStore
        from kew.run_store_work import AdmissionIdentityConflictError
        from kew.runners import get_runner

        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        retry = MagicMock(
            return_value=dispatch_core._RetryOutcome(early_return=_ok_result())
        )
        monkeypatch.setattr(dispatch_core, "_run_retry_loop", retry)
        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            assert store.reserve_epic_node_item("alpha", "42", "owner")
            if failure == "ledger":
                append = store._chained_insert_in_transaction

                def fail_binding(values, **kwargs):
                    if values[1] == "dispatch_reservation_bound":
                        raise RuntimeError("binding ledger unavailable")
                    return append(values, **kwargs)

                monkeypatch.setattr(
                    store, "_chained_insert_in_transaction", fail_binding
                )
                error, reason = RuntimeError, "binding ledger unavailable"
            else:
                build = store.build_admission_identity

                def replace_reservation(*args, **kwargs):
                    store.release_epic_node_item("42", run_id="owner")
                    assert store.reserve_epic_node_item("alpha", "42", "replacement")
                    return build(*args, **kwargs)

                monkeypatch.setattr(
                    store, "build_admission_identity", replace_reservation
                )
                error, reason = (
                    AdmissionIdentityConflictError,
                    "dispatch reservation changed",
                )
            with pytest.raises(error, match=reason):
                dispatch_core.run_tracked_agent(
                    prompt="p",
                    worktree_dir=tmp_path,
                    config=config,
                    issue_num=42,
                    item_id="42",
                    store=store,
                    branch="agent/issue-42",
                    runner=get_runner(SANDBOX_RUNNER),
                    model="claude-sonnet-4-6",
                    epic_ref="alpha",
                )
            assert store.list_by_item("42") == []
            assert store.list_audit_events(event="dispatch_reservation_bound") == []
            assert (
                store._conn.execute(
                    "SELECT COUNT(*) FROM run_admission_identity"
                ).fetchone()[0]
                == 0
            )
            assert (
                store._conn.execute(
                    "SELECT COUNT(*) FROM run_route_profile"
                ).fetchone()[0]
                == 0
            )
            retry.assert_not_called()


class TestHostAdmissionIdentity:
    """I01: the host pending-row writer persists the immutable admission
    identity in the same transaction as the run row."""

    def _runner(self):
        runner = MagicMock()
        runner.name = "claude-code"
        runner.supports_resume.return_value = False
        return runner

    def _config(self, tmp_path):
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        config.project.repo = "acme/widgets"
        return config

    @patch(f"{_P}.run_agent")
    def test_github_dispatch_records_tracker_item_and_repo(self, mock_agent, tmp_path):
        from kew.dispatch_core import run_tracked_agent
        from kew.run_store import SqliteRunStore

        mock_agent.side_effect = lambda *a, **k: _ok_result(issue_num=42)
        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            run_tracked_agent(
                allow_unrecorded=True,
                prompt="p",
                worktree_dir=tmp_path,
                config=self._config(tmp_path),
                issue_num=42,
                store=store,
                branch="agent/issue-42",
                runner=self._runner(),
                model="claude-sonnet-4-6",
                provider="anthropic",
            )
            [row] = store.list_by_issue(42)
            identity = store.get_admission_identity(row.run_id)

        assert identity is not None
        assert identity.source_tracker == "github"
        # The issue number IS the GitHub item id — a rename, not a shim.
        assert identity.item_id == "42"
        assert identity.delivery_repo == "acme/widgets"
        assert identity.attempt_number == 1

    @patch(f"{_P}.run_agent")
    def test_inference_provider_never_becomes_the_source_tracker(
        self, mock_agent, tmp_path
    ):
        """``provider`` on the run row names the inference provider; the
        identity's tracker is a separate, separately-sourced field."""
        from kew.dispatch_core import run_tracked_agent
        from kew.run_store import SqliteRunStore

        mock_agent.side_effect = lambda *a, **k: _ok_result(issue_num=0)
        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            run_tracked_agent(
                allow_unrecorded=True,
                prompt="p",
                worktree_dir=tmp_path,
                config=self._config(tmp_path),
                issue_num=0,
                item_id="SFD-9",
                store=store,
                branch="agent/sfd-9",
                runner=self._runner(),
                model="claude-sonnet-4-6",
                provider="openrouter",
                source_tracker="linear",
            )
            [row] = store.list_by_item("SFD-9")
            identity = store.get_admission_identity(row.run_id)

        assert row.provider == "openrouter"
        assert identity.source_tracker == "linear"
        assert identity.item_id == "SFD-9"

    @patch(f"{_P}.run_agent")
    def test_planning_bucket_stays_out_of_admission_identity(
        self, mock_agent, tmp_path
    ):
        from kew.dispatch_core import run_tracked_agent
        from kew.run_store import SqliteRunStore

        bucket = "epic-plan:alpha-20260818"
        mock_agent.side_effect = lambda *a, **k: _ok_result(issue_num=0)
        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            run_tracked_agent(
                allow_unrecorded=True,
                prompt="plan",
                worktree_dir=tmp_path,
                config=self._config(tmp_path),
                issue_num=0,
                item_id=bucket,
                store=store,
                runner=self._runner(),
                model="claude-sonnet-4-6",
                run_kind="planning",
            )
            [row] = store.list_by_item(bucket)
            identity = store.get_admission_identity(row.run_id)

        assert row.item_id == bucket
        assert identity is not None
        assert identity.is_synthetic
        assert identity.source_tracker is None
        assert identity.item_id is None

    @patch(f"{_P}.run_agent")
    def test_standalone_review_pr_number_is_not_admission_item(
        self, mock_agent, tmp_path
    ):
        from kew.dispatch_core import run_tracked_agent
        from kew.run_store import SqliteRunStore

        mock_agent.side_effect = lambda *a, **k: _ok_result(issue_num=87)
        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            run_tracked_agent(
                allow_unrecorded=True,
                prompt="review",
                worktree_dir=tmp_path,
                config=self._config(tmp_path),
                issue_num=87,
                store=store,
                runner=self._runner(),
                model="claude-sonnet-4-6",
                run_kind="review",
            )
            [row] = store.list_by_issue(87)
            identity = store.get_admission_identity(row.run_id)

        assert identity is not None
        assert identity.is_synthetic
        assert identity.source_tracker is None
        assert identity.item_id is None

    @patch(f"{_P}.run_agent")
    def test_two_attempt_retry_persists_latest_admission_identity(
        self, mock_agent, tmp_path
    ):
        from kew.dispatch_core import run_tracked_agent
        from kew.run_store import SqliteRunStore

        config = self._config(tmp_path)
        config.retry.max_attempts = 2
        config.retry.backoff_base_seconds = 0
        mock_agent.side_effect = [
            _fail_result(issue_num=42),
            _ok_result(issue_num=42),
        ]
        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            run_tracked_agent(
                allow_unrecorded=True,
                prompt="p",
                worktree_dir=tmp_path,
                config=config,
                issue_num=42,
                store=store,
                runner=self._runner(),
                model="claude-sonnet-4-6",
            )
            [row] = store.list_by_issue(42)
            identity = store.get_admission_identity(row.run_id)

        assert mock_agent.call_count == 2
        assert row.attempt_number == 2
        assert identity is not None
        assert identity.attempt_number == 2
        assert identity.item_id == "42"

    @patch(f"{_P}.run_agent")
    def test_epic_node_identity_comes_from_the_reservation(self, mock_agent, tmp_path):
        from kew.dispatch_core import run_tracked_agent
        from kew.run_store import SqliteRunStore

        mock_agent.side_effect = lambda *a, **k: _ok_result(issue_num=0)
        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            store.reserve_epic_node_item("epic-x", "SFD-11", "reserver")
            run_tracked_agent(
                allow_unrecorded=True,
                prompt="p",
                worktree_dir=tmp_path,
                config=self._config(tmp_path),
                issue_num=0,
                item_id="SFD-11",
                store=store,
                branch="agent/sfd-11",
                runner=self._runner(),
                model="claude-sonnet-4-6",
                source_tracker="linear",
            )
            [row] = store.list_by_item("SFD-11")
            identity = store.get_admission_identity(row.run_id)

        assert identity.epic_ref == "epic-x"
        assert identity.node_id == "SFD-11"

    def test_rejected_cap_admission_writes_neither_row_nor_identity(self, tmp_path):
        from datetime import UTC, datetime

        from kew.dispatch_core import WorkerCapReached, run_tracked_agent
        from kew.run_store import RunRecord, SqliteRunStore

        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            store.save(
                RunRecord(
                    run_id="r-live",
                    issue_num=1,
                    branch="agent/issue-1",
                    status="pending",
                    exit_code=-1,
                    started_at=datetime.now(UTC),
                    item_id="1",
                )
            )
            with pytest.raises(WorkerCapReached):
                run_tracked_agent(
                    allow_unrecorded=True,
                    prompt="p",
                    worktree_dir=tmp_path,
                    config=self._config(tmp_path),
                    issue_num=2,
                    store=store,
                    branch="agent/issue-2",
                    runner=self._runner(),
                    model="claude-sonnet-4-6",
                    worker_cap=1,
                    cap_exclude_issue=2,
                )
            assert store.list_by_issue(2) == []
            assert store.find_admissions_for_item("github", "2") == []


def test_linear_governed_remediation_forwards_tracker_to_admission(tmp_path):
    from kew.dispatch_core import run_tracked_agent
    from kew.review import dispatch_review
    from kew.run_store import SqliteRunStore

    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")
    config.project.repo = "acme/widgets"
    config.tracker.type = "linear"
    config.retry.max_attempts = 1

    runner = MagicMock()
    runner.name = "claude-code"
    runner.publication_style = "agent"
    runner.supports_resume.return_value = False
    resolved = MagicMock(runner=runner, model="claude-sonnet-4-6")
    checkout = MagicMock()
    checkout.path = tmp_path / "review-checkout"
    checkout.conflict = None
    checkout.detached = False
    checkout.head_sha = "a" * 40
    containment = MagicMock(
        posture="host", image=None, reason="test host path", contained=False
    )
    meta = {
        "headRefName": "agent/sfd-12",
        "baseRefName": "main",
        "body": "",
        "title": "Fix SFD-12",
    }

    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        with (
            patch("kew.review._get_pr_metadata", return_value=meta),
            patch("kew.review.fetch_review_comments", return_value=[MagicMock()]),
            patch("kew.review._check_github_connectivity", return_value=True),
            patch("kew.review.materialize_review_checkout", return_value=checkout),
            patch(
                "kew.review.build_review_prompt", return_value="fix"
            ) as build_prompt_mock,
            patch("kew.review.resolve_runner", return_value=resolved),
            patch(
                "kew.review.resolve_remediation_containment",
                return_value=containment,
            ),
            patch("kew.review.attest_flow_posture"),
            patch("kew.review.record_review_outcome"),
            patch("kew.review.run_tracked_agent", side_effect=run_tracked_agent),
            patch(
                f"{_P}.run_agent",
                return_value=_fail_result(issue_num=87, error_category="permanent"),
            ),
        ):
            dispatch_review(
                pr_number=87,
                config=config,
                store=store,
                item_id="SFD-12",
            )
        [row] = store.list_by_item("SFD-12")
        identity = store.get_admission_identity(row.run_id)

    assert identity is not None
    assert identity.source_tracker == "linear"
    assert identity.item_id == "SFD-12"
    # A governed round gets the targeted-test brief, not the full suite (#2109).
    assert build_prompt_mock.call_args.kwargs["governed"] is True


# --------------------------------------------------------------------------
# I03C — realized route-profile capture at admission (host path)
# --------------------------------------------------------------------------


class TestCapacityAnchors:
    def test_the_project_and_the_tempdir_devices_are_both_probed(
        self, tmp_path, monkeypatch
    ):
        from kew.dispatch_core import _capacity_anchors

        monkeypatch.chdir(tmp_path)
        root = tmp_path / "work-root"
        monkeypatch.setenv("KEW_WORK_ROOT", str(root))
        anchors = _capacity_anchors()
        assert anchors == [Path(tmp_path).resolve(), root] or anchors == [
            Path.cwd(),
            root,
        ]

    def test_a_shared_device_is_probed_once(self, tmp_path, monkeypatch):
        from kew.dispatch_core import _capacity_anchors

        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("KEW_WORK_ROOT", str(Path.cwd()))
        assert len(_capacity_anchors()) == 1


class TestBreakerEventForHardKills:
    """A 137 hard kill emits a circuit_breaker event; a host cap names itself."""

    @staticmethod
    def _emit(store, tmp_path, result):
        from kew.dispatch_core import _maybe_emit_breaker

        _maybe_emit_breaker(_profile_run(store, tmp_path), result)
        return store.list_audit_events(event="circuit_breaker")

    def test_a_classified_hard_stop_is_audited_under_its_own_rule(self, tmp_path):
        from kew.run_store import SqliteRunStore

        for kind in ("wall_clock", "hard_kill"):
            store = SqliteRunStore(str(tmp_path / f"{kind}.db"))
            result = DispatchResult(
                exit_code=137, issue_num=42, log_file=tmp_path / "l"
            )
            result.hard_stop = kind
            events = self._emit(store, tmp_path, result)
            assert [e.payload["rule"] for e in events] == [kind]

    def test_an_unclassified_137_emits_nothing(self, tmp_path):
        # Only the orchestrator can tell an OOM from a timeout; a bare 137
        # from another path must not be audited as a wall-clock breaker.
        from kew.run_store import SqliteRunStore

        store = SqliteRunStore(str(tmp_path / "runs.db"))
        result = DispatchResult(exit_code=137, issue_num=42, log_file=tmp_path / "l")
        assert self._emit(store, tmp_path, result) == []

    def test_a_host_cap_kill_names_the_cap(self, tmp_path):
        from kew.run_store import SqliteRunStore

        store = SqliteRunStore(str(tmp_path / "runs.db"))
        result = DispatchResult(exit_code=137, issue_num=42, log_file=tmp_path / "l")
        result.resource_cap_reason = "kew-42: checkout exceeded 1024 bytes"
        result.hard_stop = "resource_cap"
        events = self._emit(store, tmp_path, result)
        assert [e.payload["rule"] for e in events] == ["resource_cap"]
        assert events[0].payload["reason"] == result.resource_cap_reason

    def test_an_unavailable_oom_probe_is_durable_in_the_audit_payload(self, tmp_path):
        from kew.run_store import SqliteRunStore

        store = SqliteRunStore(str(tmp_path / "runs.db"))
        result = DispatchResult(exit_code=137, issue_num=42, log_file=tmp_path / "l")
        result.hard_stop = "hard_kill"
        result.oom_probe_status = "unavailable"
        result.oom_probe_diagnostic = "OSError: daemon refused the query"
        [event] = self._emit(store, tmp_path, result)
        assert event.payload["oom_probe_status"] == "unavailable"
        assert event.payload["oom_probe_diagnostic"] == result.oom_probe_diagnostic


class TestHostCapKillStopsTheRetryLadder:
    """Stable cap codes select terminal or retriable policy."""

    @staticmethod
    def _terminal(store, tmp_path, result):
        from kew.dispatch_core import _classify_attempt

        return _classify_attempt(
            result, _profile_run(store, tmp_path), store, None, None
        )

    @staticmethod
    def _capped(tmp_path, code):
        result = DispatchResult(exit_code=137, issue_num=42, log_file=tmp_path / "l")
        result.hard_stop = "resource_cap"
        result.resource_cap_code = code.value
        result.resource_cap_reason = f"kew-42: observed {code.value}"
        result.duration_seconds = 12.0
        return result

    def _assert_policy(self, tmp_path, code, *, terminal):
        from kew.run_store import SqliteRunStore

        store = SqliteRunStore(str(tmp_path / f"runs-{code.value}.db"))
        assert self._terminal(store, tmp_path, self._capped(tmp_path, code)) is terminal

    def test_checkout_bytes_is_terminal(self, tmp_path):
        self._assert_policy(tmp_path, ResourceCapCode.CHECKOUT_BYTES, terminal=True)

    def test_checkout_entries_is_terminal(self, tmp_path):
        self._assert_policy(tmp_path, ResourceCapCode.CHECKOUT_ENTRIES, terminal=True)

    def test_confirmed_oom_is_terminal(self, tmp_path):
        self._assert_policy(tmp_path, ResourceCapCode.OOM, terminal=True)

    def test_host_free_space_is_retriable(self, tmp_path):
        self._assert_policy(tmp_path, ResourceCapCode.HOST_FREE_SPACE, terminal=False)

    def test_transcript_cap_is_retriable(self, tmp_path):
        self._assert_policy(tmp_path, ResourceCapCode.TRANSCRIPT, terminal=False)

    @pytest.mark.parametrize("kind", ["wall_clock", "hard_kill", ""])
    def test_every_other_137_keeps_its_retry(self, tmp_path, kind):
        # A timeout or an unattributed hard kill can genuinely differ next
        # attempt; only a cap that the retry cannot change is terminal.
        from kew.run_store import SqliteRunStore

        store = SqliteRunStore(str(tmp_path / f"runs-{kind or 'bare'}.db"))
        result = DispatchResult(exit_code=137, issue_num=42, log_file=tmp_path / "l")
        result.hard_stop = kind
        result.duration_seconds = 12.0
        assert self._terminal(store, tmp_path, result) is False


def test_transcript_detector_emits_retriable_policy_code(tmp_path) -> None:
    from kew.dispatch_core import _resource_cap_stops_retry
    from kew.sandbox import _StreamGuard

    guard = _StreamGuard(
        checkout_dir=None,
        wall_clock_seconds=60,
        transcript_cap=1,
        checkout_quota=100,
        now=lambda: 0,
    )
    observation = guard.admit("too large")
    assert observation is not None
    assert observation.code is ResourceCapCode.TRANSCRIPT

    result = DispatchResult(exit_code=137, issue_num=1, log_file=tmp_path / "l")
    result.hard_stop = "resource_cap"
    result.resource_cap_code = observation.code.value
    result.resource_cap_reason = observation.reason
    assert _resource_cap_stops_retry(result) is False


def test_checkout_entry_detector_emits_terminal_policy_code(tmp_path) -> None:
    from kew.dispatch_core import _resource_cap_stops_retry
    from kew.sandbox import _StreamGuard

    guard = _StreamGuard(
        checkout_dir=tmp_path,
        wall_clock_seconds=60,
        transcript_cap=100,
        checkout_quota=100,
        checkout_entry_quota=1,
        host_free_floor=0,
        now=lambda: 0,
    )
    with patch("kew.sandbox.checkout_usage", return_value=(0, 2)):
        observation = guard.timed_breach()
    assert observation is not None
    assert observation.code is ResourceCapCode.CHECKOUT_ENTRIES

    result = DispatchResult(exit_code=137, issue_num=1, log_file=tmp_path / "l")
    result.hard_stop = "resource_cap"
    result.resource_cap_code = observation.code.value
    result.resource_cap_reason = observation.reason
    assert _resource_cap_stops_retry(result) is True


def test_sandboxed_execute_dispatch_classifies_oom_on_real_run_path(tmp_path) -> None:
    """The direct sandbox launch classifies, audits, and returns one OOM."""
    from kew import dispatch_core
    from kew.dispatch_core import DispatchOutcome, execute_dispatch
    from kew.inference_transport import ResolvedInferenceTransport
    from kew.run_store import SqliteRunStore
    from kew.runners import get_runner
    from kew.sandbox import ContainerHandle, SandboxDecision

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")
    resolved = MagicMock()
    resolved.runner.name = "claude-code"
    resolved.model = "claude-sonnet-4-6"
    transport = ResolvedInferenceTransport(
        mode="direct",
        requested_model=resolved.model,
        model=resolved.model,
        config_sha256="cfg",
        endpoint="https://api.anthropic.com",
    )
    proc = MagicMock()
    proc.stdout = io.StringIO("")
    proc.wait.return_value = 137
    cidfile = tmp_path / "dispatch.cid"
    cidfile.write_text("a" * 64, encoding="utf-8")
    handle = ContainerHandle(proc, "kew-1767", cidfile=cidfile)
    captured: dict[str, DispatchResult] = {}

    def collect(ctx, *, result, worktree_dir, run_sandboxed_path):
        captured["result"] = result
        return DispatchOutcome(
            exit_code=result.exit_code,
            changed_files=[],
            run_id=result.run_id,
        )

    def daemon(argv, **kwargs):
        if argv[:3] == ["ps", "-o", "lstart="]:
            return MagicMock(returncode=0, stdout="Sun Sep 20 02:00:00 2026\n")
        if argv[:3] == ["docker", "network", "inspect"]:
            return MagicMock(returncode=0, stdout="[]", stderr="")
        assert argv[:2] == ["docker", "events"]
        assert "event=oom" in argv
        return MagicMock(returncode=0, stdout="container oom current-id\n")

    decision = SandboxDecision(
        sandboxed=True,
        runtime="docker",
        posture="sandboxed",
        notice=None,
        image=SANDBOX_IMAGE,
    )
    with (
        patch("kew.sandbox.ContainerHandle.observe_workload_start", return_value=True),
        SqliteRunStore(str(tmp_path / "runs.db")) as store,
        patch(f"{_P}.resolve_runner", return_value=resolved),
        patch(f"{_P}.resolve_effective_sandbox", return_value=decision),
        patch(f"{_P}._check_github_connectivity", return_value=True),
        patch(f"{_P}._materialize_checkout", return_value=checkout),
        patch(f"{_P}._build_repo_map", return_value=""),
        patch(f"{_P}.check_runtime_memory_capacity"),
        patch(f"{_P}._check_initial_host_capacity"),
        patch(f"{_P}.check_agent_image_staleness", return_value=None),
        patch(f"{_P}._detect_sandbox_runtime", return_value="/usr/bin/docker"),
        patch(
            "kew.dispatch_sandbox._resolve_launch_inputs",
            return_value=(
                "sha256:" + "a" * 64,
                transport,
                resolved.model,
                None,
                get_runner("claude-code"),
            ),
        ),
        patch("kew.dispatch_sandbox._capture_launch_evidence_or_finalize"),
        patch.object(dispatch_core, "_launch_container", return_value=handle) as launch,
        patch("kew.subprocess_utils.run", side_effect=daemon),
        patch(f"{_P}._collect_result", side_effect=collect),
    ):
        outcome = execute_dispatch(
            allow_unrecorded=True,
            issue=_make_issue(num=1767, title="Classify direct sandbox OOM"),
            config=config,
            store=store,
        )
        events = store.list_audit_events(event="circuit_breaker")

    result = captured["result"]
    assert outcome.exit_code == 137
    assert launch.call_count == 1
    assert result.hard_stop == "resource_cap"
    assert result.resource_cap_code == ResourceCapCode.OOM.value
    assert "OOM-killed" in result.resource_cap_reason
    assert len(events) == 1
    assert events[0].payload["rule"] == "resource_cap"
    assert events[0].payload["oom_probe_status"] == "seen"
    evidence = read_derived_evidence(result.log_file)
    assert evidence is not None and evidence.oom_probe_status == "seen"


def test_empty_and_unavailable_oom_probes_are_distinct_in_durable_evidence(
    tmp_path,
) -> None:
    from kew import dispatch_core
    from kew.dispatch_sandbox import run_container_and_stream
    from kew.sandbox import ContainerHandle

    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")

    def run_case(label, probe_outcome):
        log_file = tmp_path / f"{label}.log"
        proc = MagicMock()
        proc.stdout = io.StringIO("")
        proc.wait.return_value = 137
        cidfile = tmp_path / f"{label}.cid"
        cidfile.write_text("a" * 64, encoding="utf-8")
        handle = ContainerHandle(proc, f"kew-{label}", cidfile=cidfile)
        result = DispatchResult(exit_code=-1, issue_num=1767, log_file=log_file)

        def probe(argv, **kwargs):
            assert argv[:2] == ["docker", "events"]
            assert f"container={'a' * 64}" in argv
            assert "event=oom" in argv
            if isinstance(probe_outcome, BaseException):
                raise probe_outcome
            return probe_outcome

        with (
            patch(
                "kew.sandbox.ContainerHandle.observe_workload_start", return_value=True
            ),
            patch.object(dispatch_core, "_launch_container", return_value=handle),
            patch("kew.subprocess_utils.run", side_effect=probe),
        ):
            run_container_and_stream(
                config,
                None,
                image=SANDBOX_IMAGE,
                runner=SANDBOX_RUNNER,
                checkout_dir=tmp_path,
                slug=label,
                run_id=label,
                model="claude-sonnet-4-6",
                log_file=log_file,
                run_kind="dispatch",
                result=result,
            )
        evidence = read_derived_evidence(log_file)
        assert evidence is not None
        return result, evidence

    absent_result, absent = run_case(
        "absent",
        MagicMock(returncode=0, stdout=""),
    )
    unavailable_result, unavailable = run_case(
        "unavailable",
        OSError("daemon unavailable " + "x" * 1000),
    )
    assert absent_result.oom_probe_status == absent.oom_probe_status == "absent"
    assert absent.oom_probe_diagnostic is None
    assert (
        unavailable_result.oom_probe_status
        == unavailable.oom_probe_status
        == "unavailable"
    )
    assert "daemon unavailable" in (unavailable.oom_probe_diagnostic or "")
    assert len(unavailable.oom_probe_diagnostic or "") == 500


def _profile_run(store, tmp_path, **overrides):
    """A minimal host ``_TrackedRun`` for launch-boundary capture tests."""
    from kew.audit import resolve_actor
    from kew.dispatch_core import _TrackedRun
    from kew.inference_transport import (
        AdmittedInferenceTransport,
        ResolvedInferenceTransport,
    )
    from kew.runners import get_runner

    config = DispatchConfig()
    fields = {
        "config": config,
        "store": store,
        "run_id": "run-profile",
        "issue_num": 42,
        "item_id": "42",
        "branch": "agent/issue-42",
        "title": "Fix it",
        "complexity": "medium",
        "run_kind": "dispatch",
        "loop_id": None,
        "prefix_key": None,
        "provider": "anthropic",
        "runner": get_runner("claude-code"),
        "effective_model": "claude-opus-5",
        "actor": resolve_actor(),
        "endpoint": None,
        "repo": "x/y",
        "log_file": tmp_path / "run.log",
        "dispatch_start": 0.0,
        "transport": AdmittedInferenceTransport(
            resolved=ResolvedInferenceTransport(
                mode="direct",
                requested_model="claude-opus-5",
                model="claude-opus-5",
                config_sha256="cfg",
                endpoint="https://api.anthropic.com",
            ),
            observed_version=None,
            preflight_verdict="ready",
        ),
    }
    fields.update(overrides)
    return _TrackedRun(**fields)


class TestRealizedRouteProfileCapture:
    """Every paid attempt retains the boundary it actually launched across."""

    def _store(self, tmp_path):
        from kew.run_store import SqliteRunStore

        return SqliteRunStore(str(tmp_path / "runs.db"))

    def test_admission_persists_the_host_launch_profile(self, tmp_path):
        from kew.dispatch_core import _save_pending_run
        from kew.readiness import Provenance, RealizedRouteProfile

        store = self._store(tmp_path)
        run = _profile_run(store, tmp_path)
        _save_pending_run(
            run, worker_cap=0, cap_exclude_issue=None, admit_budget_check=None
        )

        row = store.require_route_profile("run-profile", 1)
        assert row.containment == "host"
        assert row.source_provider == "github"
        assert row.inference_provider == "anthropic"
        assert row.endpoint == "https://api.anthropic.com"
        assert row.requested_model == "claude-opus-5"
        # A host attempt never claims a container identity.
        assert row.image_id is None
        profile = RealizedRouteProfile.restore(row.profile_json, "run-profile", 1)
        assert profile.containment.provenance is Provenance.ENFORCED
        assert profile.image_id.provenance is Provenance.UNAVAILABLE
        assert store.get("run-profile") is not None

    def test_the_pending_row_records_the_prompts_self_check(self, tmp_path):
        """#2454: the host path's half of the self_check fact. The pending row
        is where it lands, so the run report can read it after completion —
        without it every uncontained dispatch would report no self-check."""
        from kew.dispatch_core import _save_pending_run

        store = self._store(tmp_path)
        run = _profile_run(store, tmp_path, self_check=True)
        _save_pending_run(
            run, worker_cap=0, cap_exclude_issue=None, admit_budget_check=None
        )

        stored = store.get("run-profile")
        assert stored is not None
        assert stored.self_check is True

    def test_tracker_identity_is_never_the_inference_provider(self, tmp_path):
        from kew.dispatch_core import _launch_boundary

        store = self._store(tmp_path)
        run = _profile_run(store, tmp_path, source_tracker="linear")
        boundary = _launch_boundary(run)

        assert boundary.source_provider == "linear"
        assert boundary.inference_provider == "anthropic"

    def test_a_contained_attempt_records_the_resolved_image_id(self, tmp_path):
        from kew.dispatch_core import _launch_boundary
        from kew.readiness import Containment

        store = self._store(tmp_path)
        run = _profile_run(store, tmp_path, contained_image="kew-agent:latest")
        with (
            patch(f"{_P}._detect_sandbox_runtime", return_value="docker"),
            patch(
                "kew.sandbox.inspect_image_id",
                return_value=ImageResolution(
                    ImageIdStatus.RESOLVED, image_id="sha256:" + "a" * 64
                ),
            ),
        ):
            boundary = _launch_boundary(run)

        assert boundary.containment is Containment.CONTAINED
        assert boundary.image_id == "sha256:" + "a" * 64
        assert boundary.image_ref == "kew-agent:latest"
        assert boundary.runner_digest is None

    def test_a_mutable_tag_alone_refuses_the_attempt(self, tmp_path):
        """Fail closed (#1550 review F1): an attempt whose boundary cannot be
        identified is refused before any charge — never launched under an
        UNKNOWN containment claim."""
        from kew.dispatch_core import (
            ContainedImageUnresolvedError,
            _launch_boundary,
        )

        store = self._store(tmp_path)
        run = _profile_run(store, tmp_path, contained_image="kew-agent:latest")
        with (
            patch(f"{_P}._detect_sandbox_runtime", return_value=None),
            pytest.raises(ContainedImageUnresolvedError),
        ):
            _launch_boundary(run)

    def test_the_boundary_pins_the_launch_to_the_resolved_id(self, tmp_path):
        """What the profile captures is what the argv launches (#1550 F1)."""
        from kew.dispatch_core import _launch_boundary

        store = self._store(tmp_path)
        run = _profile_run(store, tmp_path, contained_image="kew-agent:latest")
        with (
            patch(f"{_P}._detect_sandbox_runtime", return_value="docker"),
            patch(
                "kew.sandbox.inspect_image_id",
                return_value=ImageResolution(
                    ImageIdStatus.RESOLVED, image_id="sha256:" + "a" * 64
                ),
            ),
        ):
            boundary = _launch_boundary(run)

        assert boundary.image_id == "sha256:" + "a" * 64
        assert run.resolved_image_id == "sha256:" + "a" * 64

    def test_a_moved_tag_changes_the_attempt_fingerprint(self, tmp_path):
        from kew.dispatch_core import _attempt_route_profile

        store = self._store(tmp_path)
        run = _profile_run(store, tmp_path, contained_image="kew-agent:latest")
        with patch(f"{_P}._detect_sandbox_runtime", return_value="docker"):
            with patch(
                "kew.sandbox.inspect_image_id",
                return_value=ImageResolution(
                    ImageIdStatus.RESOLVED, image_id="sha256:" + "a" * 64
                ),
            ):
                before = _attempt_route_profile(run, 1)
            with patch(
                "kew.sandbox.inspect_image_id",
                return_value=ImageResolution(
                    ImageIdStatus.RESOLVED, image_id="sha256:" + "b" * 64
                ),
            ):
                after = _attempt_route_profile(run, 1)

        assert before is not None and after is not None
        assert before.fingerprint != after.fingerprint

    def test_attempt_two_records_its_own_profile_and_cannot_borrow_one(self, tmp_path):
        from kew.dispatch_core import _admit_attempt_route_profile, _save_pending_run
        from kew.run_store_readiness import RouteProfileMissingError

        store = self._store(tmp_path)
        run = _profile_run(store, tmp_path)
        _save_pending_run(
            run, worker_cap=0, cap_exclude_issue=None, admit_budget_check=None
        )

        with pytest.raises(RouteProfileMissingError):
            store.require_route_profile("run-profile", 2)

        # Attempt 2 re-resolves the boundary: the same run moved into a
        # container between attempts, and its profile must say so.
        run.contained_image = "kew-agent:latest"
        with (
            patch(f"{_P}._detect_sandbox_runtime", return_value="docker"),
            patch(f"{_P}._auth_path_identifier", return_value="workload-identity"),
            patch(
                "kew.sandbox.inspect_image_id",
                return_value=ImageResolution(
                    ImageIdStatus.RESOLVED, image_id="sha256:" + "c" * 64
                ),
            ),
        ):
            _admit_attempt_route_profile(run, 2)

        first = store.require_route_profile("run-profile", 1)
        second = store.require_route_profile("run-profile", 2)
        assert first.containment == "host"
        assert second.containment == "contained"
        assert second.image_id == "sha256:" + "c" * 64
        assert first.fingerprint != second.fingerprint

    def test_an_unprofiled_paid_attempt_refuses_to_launch(self, tmp_path):
        from kew.dispatch_core import _admit_attempt_route_profile
        from kew.run_store_readiness import RouteProfileMissingError

        store = self._store(tmp_path)
        run = _profile_run(store, tmp_path)

        # Attempt 1's profile is written by admission; this run never admitted,
        # so the pre-launch gate must refuse rather than infer a boundary.
        with pytest.raises(RouteProfileMissingError):
            _admit_attempt_route_profile(run, 1)

    @pytest.mark.parametrize("credential", ["held", "absent"])
    def test_contained_attempt_admission_needs_auth_evidence_only(
        self, tmp_path, monkeypatch, credential
    ):
        from kew.dispatch_core import _admit_attempt_route_profile, _save_pending_run
        from kew.publication_credentials import CredentialPublicationRefused
        from kew.sandbox import runner_credential_env_vars

        store = self._store(tmp_path)
        run = _profile_run(store, tmp_path, contained_image="kew-agent:latest")
        names = runner_credential_env_vars("claude-code")
        assert names
        for name in names:
            monkeypatch.delenv(name, raising=False)
        if credential == "held":
            monkeypatch.setenv(names[0], "synthetic-provider-credential")
        with (
            patch(f"{_P}._detect_sandbox_runtime", return_value="docker"),
            patch(
                "kew.sandbox.inspect_image_id",
                return_value=ImageResolution(
                    ImageIdStatus.RESOLVED, image_id="sha256:" + "d" * 64
                ),
            ),
        ):
            _save_pending_run(
                run, worker_cap=0, cap_exclude_issue=None, admit_budget_check=None
            )
            if credential == "held":
                # #1969: a reusable credential is admitted; the push is scanned.
                _admit_attempt_route_profile(run, 1)
                return
            with pytest.raises(CredentialPublicationRefused) as raised:
                _admit_attempt_route_profile(run, 1)
            assert raised.value.reason == "contained_auth_evidence_missing"

    def test_a_legacy_run_exposes_unknowns_not_guesses(self, tmp_path):
        from kew.readiness import Provenance, RealizedRouteProfile

        store = self._store(tmp_path)
        payload = store.route_profile_json("never-profiled", 1)
        unknown = RealizedRouteProfile.restore(payload, "never-profiled", 1)

        assert payload is None
        assert unknown.schema_version == 0
        assert all(
            getattr(unknown, name).provenance is Provenance.UNAVAILABLE
            for name in unknown.REALIZED_FACT_FIELDS
        )


class TestRemediatedCaptureContracts:
    """The #1550 review findings, each pinned by the behavior that closes it."""

    def _store(self, tmp_path):
        from kew.run_store import SqliteRunStore

        return SqliteRunStore(str(tmp_path / "runs.db"))

    def test_a_paid_run_with_no_store_is_refused(self, tmp_path):
        """F5: store-less paid execution must be refused, not silently
        unprofiled."""
        from kew.dispatch_core import run_tracked_agent

        with pytest.raises(ValueError, match="requires a store"):
            run_tracked_agent(
                prompt="p",
                worktree_dir=tmp_path,
                config=DispatchConfig(),
                issue_num=1,
                store=None,
            )

    def test_native_and_dangerous_postures_are_not_host(self, tmp_path):
        """F6: a self-containing runner and an explicit dangerously-uncontained
        launch are distinct recorded facts."""
        from kew.dispatch_core import _launch_boundary
        from kew.readiness import Containment

        store = self._store(tmp_path)
        native = _profile_run(store, tmp_path, sandbox_posture="native")
        assert _launch_boundary(native).containment is Containment.NATIVE
        dangerous = _profile_run(store, tmp_path, sandbox_posture="uncontained")
        assert _launch_boundary(dangerous).containment is Containment.UNCONTAINED
        plain = _profile_run(store, tmp_path)
        assert _launch_boundary(plain).containment is Containment.HOST

    def test_identical_material_shares_one_fingerprint_across_attempts(self, tmp_path):
        """F7: run/attempt identity is row identity, never material."""
        from kew.dispatch_core import _attempt_route_profile

        store = self._store(tmp_path)
        run = _profile_run(store, tmp_path)
        first = _attempt_route_profile(run, 1)
        second = _attempt_route_profile(run, 2)
        other = _profile_run(store, tmp_path, run_id="run-profile-2")
        third = _attempt_route_profile(other, 1)
        assert first is not None and second is not None and third is not None
        assert first.fingerprint == second.fingerprint == third.fingerprint

    def test_host_launches_probe_the_runner_version(self, tmp_path):
        """F8: Task 10 requires the native executable version, probed bounded."""
        from kew.dispatch_core import _launch_boundary
        from kew.subprocess_utils import RunnerExecutable

        store = self._store(tmp_path)
        run = _profile_run(store, tmp_path)
        with patch(
            f"{_P}.observe_runner_executable",
            return_value=RunnerExecutable(
                path="/opt/claude", version="1.2.3", sha256="ab" * 32
            ),
        ) as observe:
            boundary = _launch_boundary(run)
        assert observe.call_args.kwargs.get("probe_version") is True
        assert boundary.runner_version == "1.2.3"

    def test_synthetic_kinds_record_no_source_provider(self, tmp_path):
        """F9: a planning bucket or review pass has no source work item, so it
        has no source provider; the inference provider is the route's."""
        from kew.dispatch_core import _launch_boundary

        store = self._store(tmp_path)
        planning = _profile_run(store, tmp_path, run_kind="planning", provider=None)
        boundary = _launch_boundary(planning)
        assert boundary.source_provider is None
        assert boundary.inference_provider == "anthropic"
        review = _profile_run(store, tmp_path, run_kind="review", provider=None)
        assert _launch_boundary(review).source_provider is None

    def test_cli_login_is_a_nameable_auth_path(self, tmp_path, monkeypatch):
        """F10: a host launch with no populated credential env authenticates
        through the runner's CLI login — an honest descriptor, not None."""
        from kew.dispatch_core import _auth_path_identifier
        from kew.sandbox import runner_credential_env_vars

        store = self._store(tmp_path)
        run = _profile_run(store, tmp_path)
        for name in runner_credential_env_vars("claude-code"):
            monkeypatch.delenv(name, raising=False)
        assert _auth_path_identifier(run, "claude-code") == "cli-login"
        names = runner_credential_env_vars("claude-code")
        if names:
            monkeypatch.setenv(names[0], "sentinel")
            assert _auth_path_identifier(run, "claude-code") == f"env:{names[0]}"

    def test_a_retry_binds_identity_and_profile_in_one_transaction(self, tmp_path):
        """F4: after attempt 2's capture — before its provider call — both the
        profile AND the attempt-2 admission identity exist."""
        from kew.dispatch_core import _admit_attempt_route_profile, _save_pending_run

        store = self._store(tmp_path)
        run = _profile_run(store, tmp_path)
        _save_pending_run(
            run, worker_cap=0, cap_exclude_issue=None, admit_budget_check=None
        )
        _admit_attempt_route_profile(run, 2)
        assert store.get_route_profile(run.run_id, 2) is not None
        identity = store.get_admission_identity(run.run_id)
        assert identity is not None
        assert identity.attempt_number == 2

    def test_observation_reads_the_allowlist_path_the_launch_execs(
        self, tmp_path, monkeypatch
    ):
        """F2: the profile must hash the executable the child will exec (the
        TOOL_PATHS entry), never a fresh PATH lookup that can diverge."""
        from kew import subprocess_utils

        real = tmp_path / "claude"
        real.write_text("#!/bin/sh\nexit 0\n")
        real.chmod(0o755)
        decoy = tmp_path / "decoy" / "claude"
        decoy.parent.mkdir()
        decoy.write_text("#!/bin/sh\nexit 1\n")
        decoy.chmod(0o755)
        monkeypatch.setitem(subprocess_utils.TOOL_PATHS, "claude", str(real))
        monkeypatch.setenv("PATH", str(decoy.parent))

        observed = subprocess_utils.observe_runner_executable(
            "claude-code", probe_version=False
        )
        assert observed is not None
        assert observed.path == str(real)


def _tracked_run_stub(store, log_file):
    """A minimal ``_TrackedRun`` for the evidence-import unit tests."""
    from kew.audit import Actor
    from kew.dispatch_core import _TrackedRun

    run = MagicMock(spec=_TrackedRun)
    run.store = store
    run.run_id = "run-7"
    run.issue_num = 7
    run.item_id = "7"
    run.repo = "octo/widgets"
    run.endpoint = None
    run.actor = Actor("policy", "test")
    run.runner = MagicMock()
    run.runner.name = "claude-code"
    run.log_file = log_file
    return run


class TestContainedLaunchEvidence:
    """I06T activates I06's protected prompt and records the trusted base
    BEFORE the provider runs — the two facts that stop being knowable once an
    agent-writable checkout is handed to a container."""

    def _config(self, tmp_path: Path) -> DispatchConfig:
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        config.project.repo = "octo/widgets"
        return config

    def _launch(self, tmp_path, store, checkout=None):
        from kew.dispatch_core import _run_sandboxed_container

        with (
            patch("kew.dispatch_core._launch_container"),
            patch("kew.dispatch_core._stream_sandbox_log", return_value=1),
        ):
            _run_sandboxed_container(
                prompt="the exact compiled prompt",
                checkout_dir=checkout or tmp_path,
                config=self._config(tmp_path),
                image=SANDBOX_IMAGE,
                runner=SANDBOX_RUNNER,
                issue_num=7,
                item_id="7",
                branch="agent/issue-7",
                title="Fix it",
                model="claude-sonnet-4-6",
                store=store,
            )

    @staticmethod
    def _facts(store, kind):
        from kew.journal_replay import replay_host_sequence

        return [
            dict(record.facts)
            for record in replay_host_sequence(store).records
            if record.kind == kind
        ]

    def test_prompt_bytes_are_retained_outside_the_database(self, tmp_path):
        from kew.dispatch_sandbox import protected_prompt_root
        from kew.run_store import SqliteRunStore

        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            from kew.evidence import EvidenceKind

            self._launch(tmp_path, store)
            [artifact] = self._facts(store, EvidenceKind.ARTIFACT)

        assert artifact["artifact_kind"] == "compiled_prompt"
        assert artifact["digest"].startswith("sha256:")
        # The ledger names the bytes by digest and never carries them.
        assert "the exact compiled prompt" not in json.dumps(artifact)
        root = protected_prompt_root(self._config(tmp_path))
        [retained] = list(root.glob("*.prompt"))
        assert retained.read_text(encoding="utf-8") == "the exact compiled prompt"
        assert (retained.stat().st_mode & 0o777) == 0o600

    def test_trusted_base_is_recorded_from_the_host_checkout(self, tmp_path):
        import subprocess as sp

        from kew.run_store import SqliteRunStore
        from kew.subprocess_utils import TOOL_PATHS

        checkout = tmp_path / "checkout"
        checkout.mkdir()
        git = TOOL_PATHS["git"]
        for args in (
            ("init",),
            ("config", "user.name", "t"),
            ("config", "user.email", "t@t"),
            ("commit", "--allow-empty", "-m", "base"),
        ):
            sp.run([git, *args], cwd=checkout, capture_output=True, check=True)
        head = sp.run(
            [git, "rev-parse", "HEAD"], cwd=checkout, capture_output=True, text=True
        ).stdout.strip()

        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            from kew.evidence import EvidenceKind

            self._launch(tmp_path, store, checkout=checkout)
            facts = self._facts(store, EvidenceKind.RECEIPT_FACT)

        [base] = [f for f in facts if f["fact"] == "trusted_base_sha"]
        assert base["value"] == head
        assert base["source"] == "host_checkout"

    def test_an_unobservable_base_is_explicit_never_silent(self, tmp_path, monkeypatch):
        """Rule 4: a base kew could not read is recorded as an explicit
        unavailable observation, so I06P/I07 refuse on a typed gap instead of
        binding an artifact tree to a plausible-looking guess."""
        from kew.run_store import SqliteRunStore

        # Git ignores an empty .git directory; explicitly bound upward discovery.
        monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))

        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            from kew.evidence import EvidenceKind

            # An unborn repository has no base and cannot discover a parent HEAD.
            subprocess.run(
                ["git", "init", str(tmp_path)], check=True, capture_output=True
            )
            self._launch(tmp_path, store)
            facts = self._facts(store, EvidenceKind.RECEIPT_FACT)

        [base] = [f for f in facts if f["fact"] == "trusted_base_sha"]
        assert base["value"] == "unavailable"
        assert base["source"] == "unobservable"

    def test_a_store_that_cannot_index_the_prompt_refuses_the_launch(self, tmp_path):
        """Fail-closed: an attempt whose prompt no receipt can name must not
        run. The admitted row is finalized first, so the refusal never leaves a
        pending row pinning the node's reservation."""
        from kew.dispatch_sandbox import LaunchEvidenceRefused
        from kew.run_store import SqliteRunStore

        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            with (
                patch.object(type(store), "record_artifact", None, create=True),
                pytest.raises(LaunchEvidenceRefused),
            ):
                self._launch(tmp_path, store)
            [row] = store.list_by_issue(7)

        assert row.status == "failure"
        assert row.error_category == "launch_evidence_refused"


class TestRunnerEvidenceImport:
    """A runner's account of its own run enters the ledger stamped as a runner
    claim — never as a host attestation, and never as free text."""

    def test_derived_stream_facts_land_as_runner_evidence(self, tmp_path):
        from kew.evidence import EvidenceKind, ProducerKind
        from kew.journal_replay import replay_host_sequence
        from kew.run_store import SqliteRunStore
        from kew.transcript_metrics import StreamDerivation

        log_file = tmp_path / "dispatch-7.log"
        derivation = StreamDerivation()
        for line in [
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [{"type": "tool_use", "name": "Edit"}],
                        "usage": {"input_tokens": 10, "output_tokens": 5},
                    },
                }
            ),
            json.dumps({"type": "result", "result": "## Dispatch Report\n\n- x"}),
        ]:
            derivation.observe(line)
        write_derived_evidence(log_file, derivation.evidence())

        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            run = _tracked_run_stub(store, log_file)
            from kew.dispatch_core import _import_runner_evidence

            _import_runner_evidence(
                run, DispatchResult(exit_code=0, issue_num=7, log_file=log_file)
            )
            replay = replay_host_sequence(store)

        kinds = {r.kind for r in replay.records}
        assert EvidenceKind.RUNNER_USAGE in kinds
        assert EvidenceKind.RUNNER_PROGRESS in kinds
        # Imported facts are runner-derived forever.
        assert {r.producer.kind for r in replay.records} == {ProducerKind.RUNNER}
        metrics = {
            r.facts["metric"]: r.facts["value"]
            for r in replay.records
            if r.kind == EvidenceKind.RUNNER_USAGE
        }
        assert metrics["steps_to_first_edit"] == 1
        assert metrics["tokens_to_first_edit"] == 15
        assert metrics["tool_uses"] == 1
        # No report body, no transcript, anywhere in the ledger.
        assert "Dispatch Report" not in json.dumps(
            [dict(r.facts) for r in replay.records]
        )

    def test_a_contained_run_imports_its_evidence_too(self, tmp_path):
        """A receipt that could describe a host run but not the same run in a
        container is the #761 containment blind spot in the evidence layer."""
        from datetime import UTC, datetime

        from kew.dispatch_sandbox import _finalize_sandbox_run
        from kew.evidence import EvidenceKind, ProducerKind
        from kew.journal_replay import replay_host_sequence
        from kew.run_store import RunRecord, SqliteRunStore
        from kew.transcript_metrics import StreamDerivation

        log_file = tmp_path / "sandbox-7.log"
        derivation = StreamDerivation()
        derivation.observe(json.dumps({"type": "result", "result": "done"}))
        write_derived_evidence(log_file, derivation.evidence())

        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            store.save(
                RunRecord(
                    run_id="c-1",
                    issue_num=7,
                    branch="agent/issue-7",
                    status="pending",
                    exit_code=-1,
                    started_at=datetime.now(UTC),
                    log_file=log_file,
                )
            )
            _finalize_sandbox_run(store, "c-1", exit_code=0, runner="opencode")
            replay = replay_host_sequence(store)

        [record] = replay.records
        assert record.kind == EvidenceKind.RUNNER_PROGRESS
        assert record.producer.kind is ProducerKind.RUNNER
        assert record.producer.name == "opencode"
        assert record.facts["phase"] == "result"

    def test_a_run_with_no_derived_evidence_says_so(self, tmp_path):
        """Rule 4: unavailable telemetry is recorded as unavailable, not as a
        run that did nothing."""
        from kew.evidence import EvidenceKind
        from kew.journal_replay import replay_host_sequence
        from kew.run_store import SqliteRunStore

        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            legacy = tmp_path / "legacy.log"
            run = _tracked_run_stub(store, legacy)
            from kew.dispatch_core import _import_runner_evidence

            _import_runner_evidence(
                run, DispatchResult(exit_code=0, issue_num=7, log_file=legacy)
            )
            replay = replay_host_sequence(store)

        [record] = replay.records
        assert record.kind == EvidenceKind.RUNNER_PROGRESS
        assert record.facts["phase"] == "evidence_unavailable"


# ── I14: human-review capacity at the dispatch boundary ────────────────────


class TestDirectDispatchIsWarnedNotStopped:
    """A person dispatching directly is told the review queue is full.

    Told, not stopped: this cap governs *publication*, and a direct dispatch is
    a decision somebody made. The E5 publication that run produces still has to
    win the atomic reservation before anything leaves the host, so warning here
    costs nothing and hides nothing.
    """

    _BASE = "b" * 40
    _TREE = "c" * 40
    _NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)

    def _store(self, tmp_path):
        from kew.run_store import SqliteRunStore

        return SqliteRunStore(str(tmp_path / "capacity.db"))

    def _config(self, limit: int = 1):
        config = DispatchConfig()
        config.project.repo = "o/r"
        config.human_review.max_pending_reviews = limit
        return config

    def _hold(self, store, *, repo="o/r", run_id="run-a", limit=1):
        from kew.human_review import (
            PathChange,
            ReviewPolicy,
            decide_publication,
            measure_risk,
        )
        from kew.publication_job import PublicationIntent
        from kew.publication_repository import VERIFICATION_EXACT_TREE
        from kew.run_store import ArtifactLineage

        policy = ReviewPolicy(delivery_repo=repo, max_pending_reviews=limit)
        receipt = store.seal_artifact_receipt(
            lineage=ArtifactLineage(
                source_kind="github",
                source_id=repo,
                run_id=run_id,
                attempt_number=1,
                base_sha=self._BASE,
                package_digest="sha256:" + "a" * 64,
                tree_sha=self._TREE,
                verification=VERIFICATION_EXACT_TREE,
                locator="packages/" + "a" * 64,
            ),
            sealed_at=self._NOW,
        )
        job = store.open_publication_job(
            intent=PublicationIntent(
                source_kind="github",
                source_id=repo,
                run_id=run_id,
                attempt_number=1,
                branch=f"agent/{run_id}",
                base_sha=self._BASE,
                tree_sha=self._TREE,
                receipt_digest=receipt.receipt_digest,
                message="feat: the change",
            ),
            now=self._NOW,
        )
        risk = measure_risk(
            base_sha=self._BASE,
            tree_sha=self._TREE,
            changes=[PathChange("src/a.py", added=1)],
        )
        decision = decide_publication(policy=policy, risk=risk, pending_reviews=0)
        store.record_publication_review(
            job_id=job.job_id,
            policy=policy,
            risk=risk,
            decision=decision,
            now=self._NOW,
        )
        from kew.publication_job import (
            REVIEW_REMOTE_AVAILABLE,
            RemoteReviewObservation,
        )

        store.record_review_observation(
            RemoteReviewObservation(
                delivery_repo=repo,
                availability=REVIEW_REMOTE_AVAILABLE,
                observed_at=self._NOW.isoformat(),
                generation=0,
            )
        )
        store.reserve_publication_capacity(
            job_id=job.job_id,
            system_policy=policy,
            expected_generation=0,
            actor="ada",
            now=self._NOW,
        )
        return job

    def test_a_saturated_repository_warns_a_direct_dispatcher(self, tmp_path):
        from kew.dispatch_core import _warn_if_review_capacity_full

        store = self._store(tmp_path)
        self._hold(store)
        with patch(f"{_P}._warn") as mock_warn:
            _warn_if_review_capacity_full(self._config(), store)
        mock_warn.assert_called_once()
        assert "human reviews" in mock_warn.call_args.args[0]
        store.close()

    def test_a_supervised_driver_is_not_warned_it_is_back_pressured(self, tmp_path):
        from kew.dispatch_core import _warn_if_review_capacity_full

        store = self._store(tmp_path)
        self._hold(store)
        with patch(f"{_P}._warn") as mock_warn:
            _warn_if_review_capacity_full(self._config(), store, "epic:x")
        mock_warn.assert_not_called()
        store.close()

    def test_room_in_the_queue_says_nothing(self, tmp_path):
        from kew.dispatch_core import _warn_if_review_capacity_full

        store = self._store(tmp_path)
        self._hold(store, limit=3)
        with patch(f"{_P}._warn") as mock_warn:
            _warn_if_review_capacity_full(self._config(limit=3), store)
        mock_warn.assert_not_called()
        store.close()

    def test_another_repositorys_backlog_does_not_warn_this_one(self, tmp_path):
        from kew.dispatch_core import _warn_if_review_capacity_full

        store = self._store(tmp_path)
        self._hold(store, repo="other/repo")
        with patch(f"{_P}._warn") as mock_warn:
            _warn_if_review_capacity_full(self._config(), store)
        mock_warn.assert_not_called()
        store.close()

    def test_the_warning_never_sinks_a_dispatch(self):
        from kew.dispatch_core import _warn_if_review_capacity_full

        broken = MagicMock()
        broken.pending_review_count.side_effect = RuntimeError("boom")
        with patch(f"{_P}._warn") as mock_warn:
            _warn_if_review_capacity_full(self._config(), broken)
        mock_warn.assert_not_called()

    def test_a_project_with_no_delivery_repository_is_silent(self, tmp_path):
        from kew.dispatch_core import _warn_if_review_capacity_full

        store = self._store(tmp_path)
        config = self._config()
        config.project.repo = ""
        with patch(f"{_P}._warn") as mock_warn:
            _warn_if_review_capacity_full(config, store)
        mock_warn.assert_not_called()
        store.close()


class TestTheDeferralWriterIsCutOverToTheNormalizedJob:
    """A run whose publication the job lifecycle owns stops overloading status.

    ``awaiting_publication`` said one thing — "something about publication went
    wrong" — and could not say whether the push landed. Where a normalized job
    exists, the deferral is written there instead, as an attempt plus the
    ``ambiguous`` state that remembers what it interrupted. Legacy runs, which
    is every run that exists today, keep the encoding they were written under.
    """

    _BASE = "b" * 40
    _TREE = "c" * 40
    _NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)

    @pytest.mark.parametrize("scheme", ["", "socks5://"])
    def test_deferred_proxy_failure_redacts_payload_and_log(
        self, tmp_path, monkeypatch, scheme
    ):
        import kew.dispatch_core as dispatch_core
        from kew.publication import PublicationError
        from kew.run_store import SqliteRunStore

        secret = f"{scheme}user:s3cret@proxy.invalid:3128"
        monkeypatch.setenv("HTTP_PROXY", secret)
        with SqliteRunStore(str(tmp_path / "defer.db")) as store:
            self._run_and_job(store, with_job=False)
            self._defer(
                dispatch_core,
                store,
                tmp_path,
                error=PublicationError("push", f"push failed: {secret}"),
            )
            [event] = store.list_audit_events(event="publication_deferred")
            payload = json.dumps(event.payload)
        log = (tmp_path / "run.log").read_text()
        assert "s3cret" not in payload
        assert "s3cret" not in log
        assert "proxy.invalid" not in payload + log
        assert "<HTTP_PROXY redacted>" in payload
        assert "<HTTP_PROXY redacted>" in log

    def _ctx(self, dispatch_core, store, tmp_path):
        ctx = MagicMock(spec=dispatch_core._DispatchContext)
        ctx.checkout_generation = ""
        ctx.config = DispatchConfig()
        ctx.config.project.repo = "o/r"
        ctx.store = store
        ctx.issue_num = 42
        ctx.item_id = "42"
        ctx.branch = "agent/run-pub"
        ctx.epic_ref = None
        return ctx

    def _run_and_job(self, store, *, with_job: bool):
        from kew.publication_job import PublicationIntent
        from kew.publication_repository import VERIFICATION_EXACT_TREE
        from kew.run_store import ArtifactLineage, RunRecord

        store.save(
            RunRecord(
                run_id="run-pub",
                issue_num=42,
                item_id="42",
                branch="agent/run-pub",
                status="success",
                exit_code=0,
                started_at=self._NOW,
                finished_at=self._NOW,
            )
        )
        if not with_job:
            return None
        receipt = store.seal_artifact_receipt(
            lineage=ArtifactLineage(
                source_kind="github",
                source_id="o/r",
                run_id="run-pub",
                attempt_number=1,
                base_sha=self._BASE,
                package_digest="sha256:" + "a" * 64,
                tree_sha=self._TREE,
                verification=VERIFICATION_EXACT_TREE,
                locator="packages/" + "a" * 64,
            ),
            sealed_at=self._NOW,
        )
        return store.open_publication_job(
            intent=PublicationIntent(
                source_kind="github",
                source_id="o/r",
                run_id="run-pub",
                attempt_number=1,
                branch="agent/run-pub",
                base_sha=self._BASE,
                tree_sha=self._TREE,
                receipt_digest=receipt.receipt_digest,
                message="feat: the change",
            ),
            now=self._NOW,
        )

    def _defer(self, dispatch_core, store, tmp_path, *, error=None):
        from kew.publication import PublicationError

        result = DispatchResult(
            issue_num=42,
            log_file=tmp_path / "run.log",
            exit_code=0,
            run_id="run-pub",
        )
        with patch(f"{_P}._record_publication_continuation"):
            return dispatch_core._defer_publication(
                self._ctx(dispatch_core, store, tmp_path),
                result,
                tmp_path / "worktree",
                error or PublicationError("push", "the remote hung up"),
            )

    def test_credential_refusal_is_terminal_and_not_a_deferral(self, tmp_path):
        import kew.dispatch_core as dispatch_core
        from kew.publication_credentials import CredentialPublicationRefused
        from kew.run_store import SqliteRunStore

        with SqliteRunStore(str(tmp_path / "refused.db")) as store:
            job = self._run_and_job(store, with_job=True)
            record = store.get("run-pub")
            assert record is not None
            record.container_name = "kew-42"
            store.save(record)

            outcome = self._defer(
                dispatch_core,
                store,
                tmp_path,
                error=CredentialPublicationRefused("contained_profile_missing"),
            )

            refused = store.get("run-pub")
            settled = store.get_publication_job(job.job_id)
            events = store.list_audit_events(event="credential_publication_refused")

        assert outcome.exit_code == 1
        assert refused is not None and refused.status == "refused"
        assert refused.error_category == "credential_publication_refused"
        assert settled is not None and settled.state == "abandoned"
        assert len(events) == 1

    def test_a_job_backed_deferral_leaves_the_run_status_alone(self, tmp_path):
        import kew.dispatch_core as dispatch_core
        from kew.run_store import SqliteRunStore

        with SqliteRunStore(str(tmp_path / "defer.db")) as store:
            job = self._run_and_job(store, with_job=True)

            outcome = self._defer(dispatch_core, store, tmp_path)

            assert outcome.exit_code == 1
            assert store.get("run-pub").status == "success"
            settled = store.get_publication_job(job.job_id)
            assert settled.state == "ambiguous"
            assert settled.resume_state == "pending"
            assert settled.attempt_count == 1
            event = store.list_audit_events(event="publication_deferred")
            assert event[0].payload["job_id"] == job.job_id

    def test_a_run_with_no_job_keeps_the_legacy_awaiting_publication_encoding(
        self, tmp_path
    ):
        import kew.dispatch_core as dispatch_core
        from kew.run_store import SqliteRunStore

        with SqliteRunStore(str(tmp_path / "legacy.db")) as store:
            self._run_and_job(store, with_job=False)

            self._defer(dispatch_core, store, tmp_path)

            assert store.get("run-pub").status == "awaiting_publication"
            event = store.list_audit_events(event="publication_deferred")
            assert "job_id" not in event[0].payload

    def test_review_hold_records_flags_and_awaiting_publication(self, tmp_path):
        import kew.dispatch_core as dispatch_core
        from kew.publication_canonical import PublicationHeld
        from kew.run_store import SqliteRunStore

        with SqliteRunStore(str(tmp_path / "held.db")) as store:
            self._run_and_job(store, with_job=False)
            result = DispatchResult(
                issue_num=42,
                log_file=tmp_path / "run.log",
                exit_code=0,
                run_id="run-pub",
            )
            with patch(f"{_P}._record_publication_continuation"):
                outcome = dispatch_core._defer_publication(
                    self._ctx(dispatch_core, store, tmp_path),
                    result,
                    tmp_path / "worktree",
                    PublicationHeld(("workflow.yml: executes in CI",)),
                )

            assert outcome.exit_code == 1
            assert store.get("run-pub").status == "awaiting_publication"
            [event] = store.list_audit_events(event="publication_deferred")
            assert event.payload["category"] == "publication_review_required"
            assert "workflow.yml" in event.payload["error"]


class TestInitialCanonicalCheckoutBinding:
    """A fresh checkout is durably bound before either paid runner boundary."""

    @staticmethod
    def _context(dispatch_core, config, store, *, run_id: str):
        return dispatch_core._DispatchContext(
            config=config,
            store=store,
            notifier=None,
            force_budget=False,
            approved=True,
            interactive=False,
            model_override=None,
            merge_context=None,
            epic_ref="alpha",
            provider="github",
            issue_num=42,
            issue_title="Bind checkout",
            issue_body="",
            complexity="simple",
            item_id="42",
            branch="agent/issue-42",
            start_monotonic=0.0,
            refuse_rollback=lambda _reason: None,
            terminal_failure=None,
            run_kind="dispatch",
            run_id=run_id,
        )

    @staticmethod
    def _decision(*, sandboxed: bool):
        decision = MagicMock()
        decision.sandboxed = sandboxed
        decision.posture = "contained" if sandboxed else "host"
        decision.runtime = "docker" if sandboxed else None
        decision.image = SANDBOX_IMAGE if sandboxed else None
        return decision

    @staticmethod
    def _stub_native_paid_boundary(monkeypatch, dispatch_core, paid_boundary):
        """Keep the real host launch ordering while replacing the paid runner."""
        monkeypatch.setattr(dispatch_core, "_build_repo_map", lambda *_a: "")
        monkeypatch.setattr(
            dispatch_core, "build_prompt", lambda **_kwargs: "compiled prompt"
        )
        monkeypatch.setattr(
            dispatch_core,
            "_run_governor",
            lambda **kwargs: (
                kwargs["resolved"],
                _swap_decision(kwargs["resolved"], kwargs["resolved"], applied=False),
            ),
        )
        monkeypatch.setattr(
            dispatch_core, "_require_final_runner_preflight", lambda *_a: None
        )
        monkeypatch.setattr(dispatch_core, "run_tracked_agent", paid_boundary)

    @staticmethod
    def _save_checkout_producer(
        store,
        run_id: str,
        *,
        status: str = "failure",
        run_kind: str = "dispatch",
        item_id: str = "42",
        branch: str = "agent/issue-42",
    ) -> None:
        from kew.run_store import RunRecord

        store.save(
            RunRecord(
                run_id=run_id,
                issue_num=42,
                item_id=item_id,
                branch=branch,
                status=status,
                exit_code=0 if status == "success" else 1,
                started_at=datetime.now(UTC),
                run_kind=run_kind,
            )
        )

    def _claim_checkout_continuation(
        self,
        dispatch_core,
        config,
        store,
        checkout: Path,
        *,
        original_run_id: str = "producer-a",
    ):
        record = store.record_continuation(
            "alpha",
            "42",
            original_run_id,
            "agent/issue-42",
            checkout,
            checkout_generation=fixture_checkout_generation(checkout),
        )
        assert (
            store.claim_continuation_under_cap(
                record.epic_ref,
                record.item_id,
                record.original_run_id,
                record.branch,
                record.worktree_identity,
                "continuation-run",
                5,
                claim_evidence=fixture_claim_evidence(
                    store,
                    record.epic_ref,
                    record.item_id,
                    record.original_run_id,
                    record.branch,
                    record.worktree_identity,
                    "continuation-run",
                ),
            )
            == "claimed"
        )
        dispatch_core._validate_continuation_dispatch_identity(
            store=store,
            epic_ref="alpha",
            item_id="42",
            worktree_dir=checkout,
            branch="agent/issue-42",
            run_id="continuation-run",
        )
        ctx = self._context(dispatch_core, config, store, run_id="continuation-run")
        ctx.worktree_dir = checkout
        return ctx

    @pytest.mark.parametrize("sandboxed", [False, True], ids=["native", "contained"])
    @pytest.mark.parametrize(
        "replace_checkout", [False, True], ids=["stable", "replaced"]
    )
    def test_initial_generation_survives_actual_admission_handoff(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        sandboxed: bool,
        replace_checkout: bool,
    ) -> None:
        """Admission cannot exchange the checkout before the paid boundary."""
        from types import SimpleNamespace

        from kew import dispatch_core, dispatch_sandbox, worktree
        from kew.audit import Actor
        from kew.publication import PublicationError
        from kew.run_store import SqliteRunStore
        from kew.runners import get_runner

        class PaidBoundaryReached(BaseException):
            pass

        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        config.run_reports.enabled = False
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        checkout.joinpath("candidate.txt").write_text("original bytes\n")
        (checkout / ".git").mkdir(exist_ok=True)
        original_generation = worktree.write_checkout_generation(checkout)
        worktree.claim_checkout(checkout)
        preserved = tmp_path / "preserved"
        run_id = "admission-contained" if sandboxed else "admission-native"
        runner = get_runner(SANDBOX_RUNNER)
        model = "claude-sonnet-4-6"
        resolved = SimpleNamespace(runner=runner, model=model)
        decision = SimpleNamespace(
            sandboxed=sandboxed,
            posture="contained" if sandboxed else "native",
            runtime="docker" if sandboxed else None,
            image=config.sandbox.image if sandboxed else None,
            egress_verification="unavailable",
        )
        for name in (
            "_emit_sandbox_resolved",
            "check_host_capacity",
            "_require_final_runner_preflight",
        ):
            monkeypatch.setattr(dispatch_core, name, lambda *_a, **_k: None)
        monkeypatch.setattr(
            dispatch_core, "_resolve_and_apply_sandbox", lambda *_a, **_k: decision
        )
        monkeypatch.setattr(
            dispatch_core, "_trusted_dispatch_base", lambda *_a: "a" * 40
        )
        monkeypatch.setattr(dispatch_core, "setup_worktree", lambda **_k: checkout)
        monkeypatch.setattr(
            dispatch_core, "_materialize_checkout", lambda *_a, **_k: checkout
        )
        monkeypatch.setattr(dispatch_core, "_build_repo_map", lambda *_a: "")
        monkeypatch.setattr(
            dispatch_core, "build_prompt", lambda **_k: "synthetic prompt"
        )
        monkeypatch.setattr(
            dispatch_core,
            "_compute_prefix_routing",
            lambda *_a: ("anthropic", ""),
        )
        monkeypatch.setattr(
            dispatch_core, "observe_runner_executable", lambda *_a, **_k: None
        )
        monkeypatch.setattr(
            dispatch_core,
            "resolve_actor",
            lambda: Actor("policy", "admission-test"),
        )
        monkeypatch.setattr(dispatch_core, "configured_endpoint", lambda *_a: None)
        monkeypatch.setattr(
            dispatch_core, "_check_existing_pr", lambda *_a: (False, None)
        )
        monkeypatch.setattr(
            dispatch_core,
            "_compute_breaker_baseline",
            lambda *_a: (None, 0.0),
        )
        monkeypatch.setattr(
            dispatch_core,
            "_detect_sandbox_runtime",
            lambda *_a: Path("/synthetic/docker"),
        )

        paid_generations: list[str] = []
        admission_calls: list[bool] = []
        evidence_generations: list[str] = []
        refusal: PublicationError | None = None
        refused_status = ""
        refused_category = ""
        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            ctx = self._context(dispatch_core, config, store, run_id=run_id)
            ctx.epic_ref = None

            def admission_boundary() -> None:
                admission_calls.append(True)
                [event] = store.list_audit_events(event="publication_base")
                assert event.payload["checkout_id"] == original_generation
                if replace_checkout:
                    checkout.rename(preserved)
                    checkout.mkdir()
                    checkout.joinpath("candidate.txt").write_text("replacement bytes\n")
                    (checkout / ".git").mkdir(exist_ok=True)
                    worktree.write_checkout_generation(checkout)

            def capture_boundary(*_a, **_k) -> None:
                evidence_generations.append(worktree.read_checkout_generation(checkout))

            def paid_boundary(*_a, **_k):
                assert store.get(run_id) is not None
                assert store.require_route_profile(run_id, 1) is not None
                paid_generations.append(worktree.read_checkout_generation(checkout))
                raise PaidBoundaryReached()

            monkeypatch.setattr(
                dispatch_sandbox,
                "_capture_launch_evidence_or_finalize",
                capture_boundary,
            )
            monkeypatch.setattr(dispatch_core, "run_agent", paid_boundary)
            monkeypatch.setattr(
                dispatch_sandbox, "run_container_and_stream", paid_boundary
            )
            try:
                dispatch_core._launch_workspace(
                    ctx,
                    dispatch_core._LaunchSetup(),
                    config=config,
                    resolved=resolved,
                    runner=runner,
                    early=dispatch_core._EarlyRunInfo(runner=runner.name, model=model),
                    admit_budget_check=admission_boundary,
                )
            except PaidBoundaryReached:
                pass
            except PublicationError as exc:
                refusal = exc
            finally:
                row = store.get(run_id)
                refused_status = row.status if row is not None else ""
                refused_category = row.error_category if row is not None else ""
                worktree.release_checkout_claim(checkout)

        assert admission_calls == [True]
        if replace_checkout:
            assert preserved.joinpath("candidate.txt").read_text() == "original bytes\n"
            assert (
                checkout.joinpath("candidate.txt").read_text() == "replacement bytes\n"
            )
            assert paid_generations == []
            assert evidence_generations == []
            assert refusal is not None
            assert refused_status == "refused"
            assert refused_category == "checkout_identity_changed"
        else:
            assert paid_generations == [original_generation]
            assert evidence_generations == ([original_generation] if sandboxed else [])

    @pytest.mark.parametrize("sandboxed", [False, True])
    @pytest.mark.parametrize("supplied", [True, False])
    def test_initial_canonical_dispatch_records_base_before_runner(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        sandboxed: bool,
        supplied: bool,
    ) -> None:
        from kew import dispatch_core, worktree
        from kew.run_store import SqliteRunStore

        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        (checkout / ".git").mkdir(exist_ok=True)
        worktree.write_checkout_generation(checkout)
        route = "contained" if sandboxed else "native"
        run_id = f"{'supplied' if supplied else 'preallocated'}-{route}"
        allocated = MagicMock()
        allocated.__str__.return_value = run_id
        allocate = MagicMock(return_value=allocated)
        monkeypatch.setattr(dispatch_core, "uuid", SimpleNamespace(uuid4=allocate))
        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            ctx = self._context(
                dispatch_core,
                config,
                store,
                run_id=run_id if supplied else None,
            )
            monkeypatch.setattr(
                dispatch_core,
                "_resolve_and_apply_sandbox",
                lambda *_a, **_k: self._decision(sandboxed=sandboxed),
            )
            monkeypatch.setattr(
                dispatch_core, "_trusted_dispatch_base", lambda _c: "a" * 40
            )
            monkeypatch.setattr(
                dispatch_core, "_emit_sandbox_resolved", lambda *_a, **_k: None
            )
            monkeypatch.setattr(
                dispatch_core, "check_host_capacity", lambda *_a, **_k: None
            )
            monkeypatch.setattr(dispatch_core, "setup_worktree", lambda **_k: checkout)
            monkeypatch.setattr(
                dispatch_core, "_materialize_checkout", lambda *_a, **_k: checkout
            )

            def paid_boundary(*_a, **kwargs):
                assert kwargs.get("run_id", run_id) == run_id
                assert (
                    dispatch_core._recorded_publication_base(
                        store,
                        run_id=run_id,
                        item_id="42",
                        branch="agent/issue-42",
                        checkout=checkout,
                    )
                    == "a" * 40
                )
                return _ok_result(run_id=run_id)

            if sandboxed:
                monkeypatch.setattr(dispatch_core, "_launch_sandboxed", paid_boundary)
            else:
                self._stub_native_paid_boundary(
                    monkeypatch, dispatch_core, paid_boundary
                )
            runner = MagicMock()
            runner.name = SANDBOX_RUNNER
            runner.publication_style = "host"
            resolved = MagicMock(runner=runner, model="claude-sonnet-4-6")

            result = dispatch_core._launch_workspace(
                ctx,
                dispatch_core._LaunchSetup(),
                config=config,
                resolved=resolved,
                runner=runner,
                early=dispatch_core._EarlyRunInfo(runner=SANDBOX_RUNNER),
                admit_budget_check=None,
            )

        assert result.run_id == run_id
        assert allocate.call_count == (0 if supplied else 1)

    def test_initial_canonical_sandbox_dispatch_records_base_before_runner(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self.test_initial_canonical_dispatch_records_base_before_runner(
            tmp_path, monkeypatch, True, True
        )

    @pytest.mark.parametrize("sandboxed", [False, True])
    @pytest.mark.parametrize("failure", ["write", "drop", "mismatch"])
    def test_initial_canonical_dispatch_refuses_unrecorded_base_before_runner(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        sandboxed: bool,
        failure: str,
    ) -> None:
        from kew import dispatch_core, worktree
        from kew.publication import PublicationError
        from kew.run_store import SqliteRunStore

        config = DispatchConfig()
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        (checkout / ".git").mkdir(exist_ok=True)
        worktree.write_checkout_generation(checkout)
        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            ctx = self._context(dispatch_core, config, store, run_id="refused-run")
            monkeypatch.setattr(
                dispatch_core,
                "_resolve_and_apply_sandbox",
                lambda *_a, **_k: self._decision(sandboxed=sandboxed),
            )
            monkeypatch.setattr(
                dispatch_core, "_trusted_dispatch_base", lambda _c: "a" * 40
            )
            monkeypatch.setattr(
                dispatch_core, "_emit_sandbox_resolved", lambda *_a, **_k: None
            )
            monkeypatch.setattr(
                dispatch_core, "check_host_capacity", lambda *_a, **_k: None
            )
            monkeypatch.setattr(dispatch_core, "setup_worktree", lambda **_k: checkout)
            monkeypatch.setattr(
                dispatch_core, "_materialize_checkout", lambda *_a, **_k: checkout
            )
            if failure == "write":
                monkeypatch.setattr(
                    store,
                    "append_audit_event",
                    MagicMock(side_effect=OSError("ledger unavailable")),
                )
            elif failure == "drop":
                monkeypatch.setattr(
                    store, "append_audit_event", MagicMock(return_value=None)
                )
            else:
                monkeypatch.setattr(
                    dispatch_core,
                    "_recorded_publication_base",
                    lambda *_a, **_k: "b" * 40,
                )
            native = MagicMock()
            contained = MagicMock()
            if sandboxed:
                monkeypatch.setattr(dispatch_core, "_launch_sandboxed", contained)
            else:
                self._stub_native_paid_boundary(monkeypatch, dispatch_core, native)
            runner = MagicMock()
            runner.name = SANDBOX_RUNNER
            runner.publication_style = "host"
            resolved = MagicMock(runner=runner, model="claude-sonnet-4-6")

            with pytest.raises(PublicationError, match="not durably recorded"):
                dispatch_core._launch_workspace(
                    ctx,
                    dispatch_core._LaunchSetup(),
                    config=config,
                    resolved=resolved,
                    runner=runner,
                    early=dispatch_core._EarlyRunInfo(runner=SANDBOX_RUNNER),
                    admit_budget_check=None,
                )

        native.assert_not_called()
        contained.assert_not_called()

    def test_initial_canonical_dispatch_refuses_missing_or_mismatched_base_readback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self.test_initial_canonical_dispatch_refuses_unrecorded_base_before_runner(
            tmp_path, monkeypatch, False, "mismatch"
        )

    def test_initial_canonical_dispatch_reuses_supplied_run_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kew import dispatch_core

        allocated = MagicMock()
        monkeypatch.setattr(dispatch_core.uuid, "uuid4", allocated)
        monkeypatch.setattr(
            dispatch_core, "_capture_root_checkout_state", lambda *_a, **_k: None
        )
        ctx = dispatch_core._prepare_dispatch_context(
            issue=_make_issue(),
            config=DispatchConfig(),
            store=None,
            notifier=None,
            force_budget=False,
            approved=True,
            interactive=False,
            model_override=None,
            merge_context=None,
            epic_ref=None,
            item_id=None,
            provider="github",
            on_refuse=None,
            on_terminal_failure=None,
            run_kind="dispatch",
            worktree_dir=None,
            run_id="caller-run",
            branch_override=None,
            allow_unrecorded=True,
        )

        monkeypatch.setattr(
            dispatch_core,
            "_resolve_and_apply_sandbox",
            MagicMock(side_effect=RuntimeError("stop after allocation")),
        )
        with pytest.raises(RuntimeError, match="stop after allocation"):
            dispatch_core._launch_workspace(
                ctx,
                dispatch_core._LaunchSetup(),
                config=ctx.config,
                resolved=MagicMock(),
                runner=MagicMock(),
                early=dispatch_core._EarlyRunInfo(),
                admit_budget_check=None,
            )
        assert ctx.run_id == "caller-run"
        allocated.assert_not_called()

    def test_initial_canonical_dispatch_preallocates_one_run_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kew import dispatch_core

        generated = MagicMock()
        generated.__str__.return_value = "generated-run"
        allocate = MagicMock(return_value=generated)
        monkeypatch.setattr(dispatch_core, "uuid", SimpleNamespace(uuid4=allocate))
        monkeypatch.setattr(
            dispatch_core, "_capture_root_checkout_state", lambda *_a, **_k: None
        )
        ctx = dispatch_core._prepare_dispatch_context(
            issue=_make_issue(),
            config=DispatchConfig(),
            store=None,
            notifier=None,
            force_budget=False,
            approved=True,
            interactive=False,
            model_override=None,
            merge_context=None,
            epic_ref=None,
            item_id=None,
            provider="github",
            on_refuse=None,
            on_terminal_failure=None,
            run_kind="dispatch",
            worktree_dir=None,
            run_id=None,
            branch_override=None,
            allow_unrecorded=True,
        )

        assert ctx.run_id is None
        monkeypatch.setattr(
            dispatch_core,
            "_resolve_and_apply_sandbox",
            MagicMock(side_effect=RuntimeError("stop after allocation")),
        )
        with pytest.raises(RuntimeError, match="stop after allocation"):
            dispatch_core._launch_workspace(
                ctx,
                dispatch_core._LaunchSetup(),
                config=ctx.config,
                resolved=MagicMock(),
                runner=MagicMock(),
                early=dispatch_core._EarlyRunInfo(),
                admit_budget_check=None,
            )
        assert ctx.run_id == "generated-run"
        allocate.assert_called_once_with()

    @pytest.mark.parametrize("supplied", [True, False])
    def test_initial_canonical_sandbox_dispatch_propagates_one_run_id_through_admission(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        supplied: bool,
    ) -> None:
        from types import SimpleNamespace

        from kew import dispatch_core, worktree
        from kew.run_store import SqliteRunStore

        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        (checkout / ".git").mkdir(exist_ok=True)
        worktree.write_checkout_generation(checkout)
        run_id = "supplied-contained-run" if supplied else "preallocated-contained-run"
        allocated = MagicMock()
        allocated.__str__.return_value = run_id
        allocate = MagicMock(return_value=allocated)
        monkeypatch.setattr(dispatch_core, "uuid", SimpleNamespace(uuid4=allocate))
        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            ctx = self._context(
                dispatch_core,
                config,
                store,
                run_id=run_id if supplied else None,
            )
            ctx.epic_ref = None
            monkeypatch.setattr(
                dispatch_core,
                "_resolve_and_apply_sandbox",
                lambda *_a, **_k: self._decision(sandboxed=True),
            )
            monkeypatch.setattr(
                dispatch_core, "_trusted_dispatch_base", lambda _c: "a" * 40
            )
            monkeypatch.setattr(
                dispatch_core, "_materialize_checkout", lambda *_a, **_k: checkout
            )
            monkeypatch.setattr(
                dispatch_core, "_emit_sandbox_resolved", lambda *_a, **_k: None
            )
            monkeypatch.setattr(
                dispatch_core, "check_host_capacity", lambda *_a, **_k: None
            )
            monkeypatch.setattr(dispatch_core, "_build_repo_map", lambda *_a: "")
            monkeypatch.setattr(
                dispatch_core, "build_prompt", lambda **_kwargs: "compiled prompt"
            )
            monkeypatch.setattr(
                dispatch_core,
                "_detect_sandbox_runtime",
                lambda _config: Path("/usr/bin/docker"),
            )
            monkeypatch.setattr(
                "kew.dispatch_sandbox._capture_launch_evidence_or_finalize",
                lambda *_a, **_k: None,
            )

            def paid_boundary(_config, observed_store, **kwargs):
                assert observed_store is store
                assert kwargs["run_id"] == run_id
                assert store.get(run_id) is not None
                assert store.require_route_profile(run_id, 1) is not None
                return 1

            monkeypatch.setattr(
                "kew.dispatch_sandbox.run_container_and_stream", paid_boundary
            )
            result = dispatch_core._launch_workspace(
                ctx,
                dispatch_core._LaunchSetup(),
                config=config,
                resolved=MagicMock(
                    runner=SimpleNamespace(name=SANDBOX_RUNNER),
                    model="claude-sonnet-4-6",
                ),
                runner=SimpleNamespace(name=SANDBOX_RUNNER),
                early=dispatch_core._EarlyRunInfo(
                    runner=SANDBOX_RUNNER, model="claude-sonnet-4-6"
                ),
                admit_budget_check=None,
            )

            [event] = store.list_audit_events(event="publication_base")

        assert result.run_id == run_id
        assert event.run_id == run_id
        assert allocate.call_count == (0 if supplied else 1)

    @pytest.mark.parametrize(
        ("producer_state", "run_kind", "bind_original"),
        [
            ("missing", "dispatch", True),
            ("pending", "dispatch", True),
            ("failure", "adversarial_review", True),
            ("failure", "ci_fix", True),
            ("failure", "dispatch", False),
        ],
        ids=["missing", "pending", "review", "ci-fix", "wrong"],
    )
    def test_existing_continuation_rejects_invalid_original_producer(
        self,
        tmp_path: Path,
        producer_state: str,
        run_kind: str,
        bind_original: bool,
    ) -> None:
        from kew import dispatch_core, worktree
        from kew.publication import PublicationError
        from kew.run_store import SqliteRunStore

        config = DispatchConfig()
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        (checkout / ".git").mkdir(exist_ok=True)
        worktree.write_checkout_generation(checkout)
        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            if producer_state != "missing":
                self._save_checkout_producer(
                    store,
                    "producer-a",
                    status=producer_state,
                    run_kind=run_kind,
                )
            bound_run = "producer-a" if bind_original else "producer-b"
            if not bind_original:
                self._save_checkout_producer(store, bound_run)
            dispatch_core._bind_publication_base(
                store,
                publish="canonical",
                base="a" * 40,
                run_id=bound_run,
                issue_num=42,
                item_id="42",
                branch="agent/issue-42",
                checkout=checkout,
            )
            ctx = self._claim_checkout_continuation(
                dispatch_core, config, store, checkout
            )

            with pytest.raises(PublicationError):
                dispatch_core._continuation_base(ctx)

    def test_existing_continuation_rejects_multiple_immutable_producers(
        self, tmp_path: Path
    ) -> None:
        from kew import dispatch_core, worktree
        from kew.publication import PublicationError
        from kew.run_store import SqliteRunStore

        config = DispatchConfig()
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        (checkout / ".git").mkdir(exist_ok=True)
        worktree.write_checkout_generation(checkout)
        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            for run_id in ("producer-a", "producer-b"):
                self._save_checkout_producer(store, run_id)
                dispatch_core._bind_publication_base(
                    store,
                    publish="canonical",
                    base="a" * 40,
                    run_id=run_id,
                    issue_num=42,
                    item_id="42",
                    branch="agent/issue-42",
                    checkout=checkout,
                )
            ctx = self._claim_checkout_continuation(
                dispatch_core, config, store, checkout
            )

            with pytest.raises(PublicationError, match="multiple"):
                dispatch_core._continuation_base(ctx)

    def test_existing_continuation_accepts_repeated_base_and_checkout_alias(
        self, tmp_path: Path
    ) -> None:
        from kew import dispatch_core, worktree
        from kew.run_store import SqliteRunStore

        config = DispatchConfig()
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        alias = tmp_path / "checkout-alias"
        alias.symlink_to(checkout, target_is_directory=True)
        (checkout / ".git").mkdir(exist_ok=True)
        worktree.write_checkout_generation(checkout)
        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            self._save_checkout_producer(store, "producer-a")
            for recorded_path in (checkout, alias):
                dispatch_core._bind_publication_base(
                    store,
                    publish="canonical",
                    base="a" * 40,
                    run_id="producer-a",
                    issue_num=42,
                    item_id="42",
                    branch="agent/issue-42",
                    checkout=recorded_path,
                )
            ctx = self._claim_checkout_continuation(dispatch_core, config, store, alias)

            assert dispatch_core._continuation_base(ctx) == "a" * 40

    def test_checkout_continuation_needs_generation_without_canonical_base(
        self, tmp_path: Path
    ) -> None:
        from kew import dispatch_core, worktree
        from kew.run_store import SqliteRunStore

        config = DispatchConfig()
        config.publication.publish = "checkout"
        config.sandbox.dangerously_skip_containment = True
        checkout = tmp_path / "checkout"
        (checkout / ".git").mkdir(parents=True)
        worktree.write_checkout_generation(checkout)
        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            self._save_checkout_producer(store, "producer-a")
            ctx = self._claim_checkout_continuation(
                dispatch_core, config, store, checkout
            )

            assert dispatch_core._continuation_base(ctx) == ""

    def test_existing_continuation_base_rejects_ambiguous_producers(
        self, tmp_path: Path
    ) -> None:
        from kew import dispatch_core, worktree
        from kew.publication import PublicationError
        from kew.run_store import SqliteRunStore

        config = DispatchConfig()
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        (checkout / ".git").mkdir(exist_ok=True)
        worktree.write_checkout_generation(checkout)
        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            dispatch_core._bind_publication_base(
                store,
                publish="canonical",
                base="a" * 40,
                run_id="producer-a",
                issue_num=42,
                item_id="42",
                branch="agent/issue-42",
                checkout=checkout,
            )
            first = store.record_continuation(
                "alpha",
                "42",
                "producer-a",
                "agent/issue-42",
                checkout,
                checkout_generation=fixture_checkout_generation(checkout),
            )
            assert (
                store.claim_continuation_under_cap(
                    first.epic_ref,
                    first.item_id,
                    first.original_run_id,
                    first.branch,
                    first.worktree_identity,
                    "continuation-run",
                    5,
                    claim_evidence=fixture_claim_evidence(
                        store,
                        first.epic_ref,
                        first.item_id,
                        first.original_run_id,
                        first.branch,
                        first.worktree_identity,
                        "continuation-run",
                    ),
                )
                == "claimed"
            )
            store.record_continuation(
                "alpha",
                "42",
                "producer-b",
                "agent/issue-42",
                checkout,
                checkout_generation=fixture_checkout_generation(checkout),
            )
            ctx = self._context(dispatch_core, config, store, run_id="continuation-run")
            ctx.worktree_dir = checkout

            with pytest.raises(PublicationError, match="ambiguous continuation"):
                dispatch_core._continuation_base(ctx)


class TestBothZeroChangePathsRecordOneCandidate:
    """Host and container reach the SAME no-change seam (I10B / Task 45).

    The contained path returns from the container with the run identity the
    sandbox recorded; the host path carries its own. Either way the outcome is
    one durable candidate bound to that attempt — never a provider write.
    """

    def _ctx(self, dispatch_core, store, tmp_path: Path):
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        return dispatch_core._DispatchContext(
            config=config,
            store=store,
            notifier=None,
            force_budget=False,
            approved=True,
            interactive=False,
            model_override=None,
            merge_context=None,
            epic_ref="alpha",
            provider="github",
            issue_num=42,
            issue_title="Nothing to do",
            issue_body="",
            complexity="simple",
            item_id="42",
            branch="agent/issue-42",
            publication_base_sha="c" * 40,
            start_monotonic=0.0,
            refuse_rollback=lambda _reason: None,
            terminal_failure=None,
            run_kind="dispatch",
        )

    def test_the_contained_no_change_path_records_the_containers_attempt(
        self, tmp_path: Path
    ):
        from kew import dispatch_core
        from kew.run_store import SqliteRunStore

        with SqliteRunStore(str(tmp_path / "d.db")) as store:
            ctx = self._ctx(dispatch_core, store, tmp_path)
            result = DispatchResult(
                exit_code=0,
                issue_num=42,
                log_file=tmp_path / "log",
                run_id="run-contained",
                error_category="sandbox_no_change_needed",
            )
            # A contained no-change carries the walker's evidence; without it
            # canonical mode records nothing (review round 10 of #1440).
            from kew.publication_canonical import CanonicalNoChange

            result.canonical_no_change = CanonicalNoChange(
                artifact=MagicMock(), base_sha="c" * 40, tree_sha="d" * 40
            )
            with patch.object(dispatch_core, "_complete_no_changes") as seam:
                dispatch_core._broker_or_complete(ctx, result, tmp_path, True)

        assert seam.call_args.kwargs["run_id"] == "run-contained"
        assert seam.call_args.kwargs["item_id"] == "42"
        assert seam.call_args.kwargs["provider"] == "github"

    def test_the_host_no_change_path_records_the_brokered_attempt(self, tmp_path: Path):
        from kew import dispatch_core
        from kew.run_store import SqliteRunStore

        with SqliteRunStore(str(tmp_path / "d.db")) as store:
            ctx = self._ctx(dispatch_core, store, tmp_path)
            result = DispatchResult(
                exit_code=0,
                issue_num=42,
                log_file=tmp_path / "log",
                run_id="run-host",
            )
            with (
                patch.object(dispatch_core, "_broker_agent_work", return_value=False),
                patch.object(dispatch_core, "_complete_no_changes") as seam,
            ):
                dispatch_core._broker_or_complete(ctx, result, tmp_path, False)

        assert seam.call_args.kwargs["run_id"] == "run-host"
        assert seam.call_args.kwargs["store"] is store


def _sidecar_test_run(tmp_path: Path):
    from kew.dispatch_core import _TrackedRun
    from kew.inference_transport import (
        AdmittedInferenceTransport,
        ResolvedInferenceTransport,
    )
    from kew.runners import get_runner

    route_config = tmp_path / "routes.toml"
    route_config.write_text("[routes]\n", encoding="utf-8")
    config = DispatchConfig.model_validate(
        {
            "sandbox": {"runtime": "docker"},
            "inference_transport": {
                "mode": "switchyard",
                "switchyard": {
                    "route": "switchyard/passthrough",
                    "expected_version": "0.1.0",
                    "config_path": str(route_config),
                    "sidecar": {
                        "enabled": True,
                        "image": "switchyard:pinned",
                        "egress_network": "approved-egress",
                    },
                },
            },
        }
    )
    resolved = ResolvedInferenceTransport(
        mode="switchyard",
        requested_model="claude-sonnet-4-6",
        model="switchyard/passthrough",
        config_sha256="pending",
        route="switchyard/passthrough",
        api_format="anthropic-messages",
        sidecar=True,
        sidecar_image="switchyard:pinned",
        sidecar_egress_network="approved-egress",
        worker_credential_env_name="ANTHROPIC_API_KEY",
    )
    return _TrackedRun(
        config=config,
        store=None,
        run_id="run-sidecar",
        issue_num=42,
        item_id="42",
        branch="agent/issue-42",
        title="Sidecar",
        complexity="medium",
        run_kind="dispatch",
        loop_id=None,
        prefix_key=None,
        provider="anthropic",
        runner=get_runner("claude-code"),
        effective_model="switchyard/passthrough",
        actor=MagicMock(),
        endpoint=None,
        repo="x/y",
        log_file=tmp_path / "run.log",
        dispatch_start=0.0,
        transport=AdmittedInferenceTransport(
            resolved=resolved,
            observed_version=None,
            preflight_verdict="sidecar_pending",
        ),
        contained_image="worker:pinned",
    )


def _sidecar_packet_evidence():
    from kew.readiness import (
        SIDECAR_PACKET_DENIALS,
        PacketProbeStatus,
        SidecarPacketEvidence,
        evaluate_topology_denial,
    )

    return SidecarPacketEvidence(
        proxy_health=PacketProbeStatus.REACHABLE,
        proxy_route=PacketProbeStatus.REACHABLE,
        denials=tuple(
            evaluate_topology_denial(
                name=name,
                positive_control=PacketProbeStatus.REACHABLE,
                worker=PacketProbeStatus.DENIED,
            )
            for name in SIDECAR_PACKET_DENIALS
        ),
    )


@pytest.mark.parametrize(
    ("mode", "host_credential", "admitted"),
    [
        ("sidecar", False, True),
        ("direct", False, False),
        ("direct", True, True),
    ],
)
def test_sandbox_admission_uses_the_selected_authentication_path(
    tmp_path: Path,
    mode: str,
    host_credential: bool,
    admitted: bool,
) -> None:
    from kew.dispatch_core import (
        _DispatchContext,
        _require_dispatch_transport_admission,
        _resolve_and_apply_sandbox,
    )
    from kew.runners import get_runner
    from kew.sandbox import SandboxRuntimeUnavailable

    config = DispatchConfig(sandbox=SandboxConfig(enabled=True, runtime="docker"))
    if mode == "sidecar":
        route_config = tmp_path / "synthetic-routes.toml"
        route_config.write_text("[routes]\n", encoding="utf-8")
        config.inference_transport = InferenceTransportConfig.model_validate(
            {
                "mode": "switchyard",
                "switchyard": {
                    "route": "switchyard/passthrough",
                    "config_path": str(route_config),
                    "expected_version": "0.1.0",
                    "credential_env": ["OPENAI_API_KEY"],
                    "sidecar": {
                        "enabled": True,
                        "image": "switchyard:pinned",
                        "egress_network": "approved-egress",
                    },
                },
            }
        )

    runner = get_runner("claude-code")
    selected = _require_dispatch_transport_admission(
        config, runner, "claude-sonnet-4-6"
    )
    ctx = MagicMock(spec=_DispatchContext)
    ctx.checkout_generation = ""
    ctx.config = config
    ctx.store = None
    ctx.issue_num = 42
    ctx.item_id = "42"
    environment = {"OPENAI_API_KEY": "synthetic-upstream"}
    if host_credential:
        environment["ANTHROPIC_API_KEY"] = "synthetic-direct"

    with (
        patch.dict(os.environ, environment, clear=True),
        patch("kew.sandbox.detect_runtime_cached", return_value="/usr/bin/docker"),
        patch(f"{_P}.check_runtime_memory_capacity"),
        patch(f"{_P}._check_initial_host_capacity"),
        patch(f"{_P}.check_agent_image_staleness", return_value=None),
    ):
        if not admitted:
            with pytest.raises(SandboxRuntimeUnavailable):
                _resolve_and_apply_sandbox(
                    ctx,
                    "claude-code",
                    inference_transport=selected,
                    defer_attestation=True,
                )
            return
        decision = _resolve_and_apply_sandbox(
            ctx,
            "claude-code",
            inference_transport=selected,
            defer_attestation=True,
        )

    assert decision.sandboxed is True


def test_sidecar_worker_omits_reusable_provider_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    from kew.audit import Actor, append_sidecar_packet_certification
    from kew.dispatch_sandbox import (
        WorkerNetnsFilterEvidence,
        prepare_sidecar_worker,
    )
    from kew.readiness import SidecarPacketCertification
    from kew.run_store import SqliteRunStore

    run = _sidecar_test_run(tmp_path)
    resolved = replace(
        run.transport.resolved,
        endpoint="http://172.30.0.2:4000",
    )
    supervisor = MagicMock()
    supervisor.network_name = "attempt-internal"
    supervisor.worker_name = "attempt-worker"
    supervisor.owner = "0" * 32
    supervisor.proxy_image_id = "sha256:" + "d" * 64
    supervisor.worker_environment.return_value = {
        "ANTHROPIC_API_KEY": "kew-synthetic-client"
    }
    supervisor.create_worker.return_value = "a" * 64
    monkeypatch.setenv("ANTHROPIC_API_KEY", "reusable-anthropic-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "reusable-openai-secret")

    with SqliteRunStore(str(tmp_path / "packet.db")) as store:
        append_sidecar_packet_certification(
            store,
            SidecarPacketCertification(
                schema_version=1,
                worker_image_id="sha256:" + "c" * 64,
                proxy_image_id="sha256:" + "d" * 64,
                packet=_sidecar_packet_evidence(),
            ),
            actor=Actor("policy", "sidecar-packet-gate"),
        )
        with patch(
            "kew.dispatch_sandbox.install_worker_netns_filter",
            return_value=WorkerNetnsFilterEvidence(
                worker_id="a" * 64,
                helper_id="b" * 64,
                image_id="sha256:" + "c" * 64,
                table="kew_sc0_" + "0" * 32,
                readback="{}",
            ),
        ):
            execution = prepare_sidecar_worker(
                run.config,
                supervisor=supervisor,
                image_id="sha256:" + "c" * 64,
                runner="claude-code",
                checkout_dir=tmp_path,
                slug="42",
                model="switchyard/passthrough",
                inference_transport=resolved,
                store=store,
            )

    worker_argv = supervisor.create_worker.call_args.args[0]
    joined = " ".join(worker_argv)
    assert worker_argv[:2] == ["docker", "create"]
    assert "reusable-anthropic-secret" not in joined
    assert "reusable-openai-secret" not in joined
    assert "ANTHROPIC_API_KEY=kew-synthetic-client" in worker_argv
    assert "--cap-add" not in worker_argv
    assert "/var/run/docker.sock" not in worker_argv
    assert execution.argv[:3] == ("docker", "exec", "-e")
    assert execution.argv[3].startswith("KEW_AGENT_START_NONCE=")
    assert execution.argv[4] == "a" * 64


def test_sidecar_lifecycle_persists_admission_then_terminal_evidence(
    tmp_path: Path,
) -> None:
    from dataclasses import replace

    from kew.audit import SIDECAR_EVIDENCE_EVENT
    from kew.dispatch_core import (
        _admit_attempt_route_profile,
        _record_terminal_sidecar_evidence,
    )
    from kew.publication_credentials import (
        CredentialPublicationRefused,
        require_publication_credentials,
    )
    from kew.readiness import SidecarTopologyEvidence
    from kew.run_store import RunRecord, SqliteRunStore

    run = _sidecar_test_run(tmp_path)
    worker_image_id = "sha256:" + "a" * 64
    run.resolved_image_id = worker_image_id
    run.transport = replace(
        run.transport,
        resolved=replace(
            run.transport.resolved,
            endpoint="http://192.0.2.10:4000",
            config_sha256="b" * 64,
        ),
        observed_version="0.1.0",
        preflight_verdict="ready",
    )

    class Supervisor:
        def topology_evidence(
            self,
            *,
            run_id: str,
            attempt_number: int,
            profile_fingerprint: str,
            stage: str,
        ) -> SidecarTopologyEvidence:
            return SidecarTopologyEvidence(
                schema_version=1,
                stage=stage,
                run_id=run_id,
                attempt_number=attempt_number,
                profile_fingerprint=profile_fingerprint,
                route="switchyard/passthrough",
                config_sha256="b" * 64,
                owner="c" * 32,
                worker_image_id=worker_image_id,
                proxy_image_id="sha256:" + "d" * 64,
                network_id="e" * 64,
                egress_network_id="f" * 64,
                proxy_id="1" * 64,
                worker_id="2" * 64,
                config_helper_id="3" * 64,
                filter_helper_id="4" * 64,
                config_storage="kew-switchyard-test-config",
                endpoint="http://192.0.2.10:4000",
                observed_version="0.1.0",
                packet=_sidecar_packet_evidence(),
                filter_verdict="passed",
                cleanup_verdict="passed" if stage == "terminal" else "pending",
            )

    with SqliteRunStore(str(tmp_path / "lifecycle.db")) as store:
        run.store = store
        store.save(
            RunRecord(
                run_id=run.run_id,
                issue_num=run.issue_num,
                item_id=run.item_id,
                branch=run.branch,
                status="pending",
                exit_code=-1,
                started_at=datetime.now(UTC),
                runner_name="claude-code",
                model=run.effective_model,
                container_name="sidecar-worker",
            )
        )
        supervisor = Supervisor()
        _admit_attempt_route_profile(run, 1, sidecar_supervisor=supervisor)

        with pytest.raises(CredentialPublicationRefused) as raised:
            require_publication_credentials(store, run_id=run.run_id)
        assert raised.value.reason == "sidecar_terminal_evidence_missing"

        _record_terminal_sidecar_evidence(run, supervisor, 1)
        require_publication_credentials(store, run_id=run.run_id)
        events = store.list_audit_events(event=SIDECAR_EVIDENCE_EVENT)

    assert {event.payload["stage"] for event in events} == {
        "admission",
        "terminal",
    }


def test_sidecar_failure_prevents_paid_worker_launch(tmp_path: Path) -> None:
    from kew.dispatch_core import _sidecar_attempt_context
    from kew.switchyard_supervisor import SwitchyardSupervisorError

    run = _sidecar_test_run(tmp_path)
    paid_launch = MagicMock()

    class FailingContext:
        def __enter__(self):
            raise SwitchyardSupervisorError("sidecar startup failed")

        def __exit__(self, *exc_info: object) -> None:
            raise AssertionError("an unentered context cannot exit")

    with (
        patch(
            "kew.dispatch_core._require_contained_image_id",
            return_value="sha256:" + "a" * 64,
        ),
        patch(
            "kew.dispatch_core.sidecar_switchyard_context",
            return_value=FailingContext(),
        ),
        pytest.raises(SwitchyardSupervisorError, match="startup"),
        _sidecar_attempt_context(run),
    ):
        paid_launch()

    paid_launch.assert_not_called()


def test_retry_uses_new_sidecar_topology(tmp_path: Path) -> None:
    from kew.dispatch_core import _sidecar_attempt_context

    run = _sidecar_test_run(tmp_path)
    supervisors = [
        MagicMock(endpoint="http://172.30.0.2:4000", config_sha256="a" * 64),
        MagicMock(endpoint="http://172.31.0.2:4000", config_sha256="b" * 64),
    ]

    class AttemptContext:
        def __init__(self, supervisor: MagicMock) -> None:
            self.supervisor = supervisor

        def __enter__(self) -> MagicMock:
            return self.supervisor

        def __exit__(self, *exc_info: object) -> None:
            return None

    contexts = [AttemptContext(supervisor) for supervisor in supervisors]
    with (
        patch(
            "kew.dispatch_core._require_contained_image_id",
            side_effect=["sha256:" + "c" * 64, "sha256:" + "d" * 64],
        ),
        patch(
            "kew.dispatch_core.sidecar_switchyard_context",
            side_effect=contexts,
        ) as context_factory,
    ):
        endpoints = []
        for _ in range(2):
            with _sidecar_attempt_context(run):
                endpoints.append(run.transport.resolved.endpoint)

    assert endpoints == ["http://172.30.0.2:4000", "http://172.31.0.2:4000"]
    assert context_factory.call_count == 2
    worker_ids = [
        call.kwargs["worker_image_id"] for call in context_factory.call_args_list
    ]
    assert worker_ids == ["sha256:" + "c" * 64, "sha256:" + "d" * 64]


def test_sidecar_cleanup_failure_preserves_completed_attempt_accounting(
    tmp_path: Path,
) -> None:
    from contextlib import nullcontext

    from kew.audit import resolve_actor
    from kew.dispatch_core import _run_retry_loop
    from kew.run_store import RunRecord, SqliteRunStore
    from kew.switchyard_supervisor import SwitchyardSupervisorError

    completed = DispatchResult(
        exit_code=0,
        issue_num=42,
        log_file=tmp_path / "run.log",
        total_tokens=350,
        input_tokens=200,
        output_tokens=100,
        cache_creation_tokens=40,
        cache_read_tokens=10,
        tool_uses=7,
        estimated_cost_usd=12.50,
        reported_cost_usd=12.50,
        planned_cost_usd=13.00,
    )
    run = _sidecar_test_run(tmp_path)
    run.actor = resolve_actor()
    # The planned bound must be backed by the same pricing used at finalization.
    run.config.pricing.overrides = {"switchyard/passthrough": [0.0, 130_000.0]}

    class CleanupFailure:
        def __enter__(self) -> MagicMock:
            return MagicMock()

        def __exit__(self, *exc_info: object) -> None:
            raise SwitchyardSupervisorError("sidecar cleanup or absence proof failed")

    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        run.store = store
        store.save(
            RunRecord(
                run_id=run.run_id,
                issue_num=run.issue_num,
                item_id=run.item_id,
                branch=run.branch,
                status="pending",
                exit_code=0,
                started_at=datetime.now(UTC),
            )
        )
        with (
            patch(f"{_P}._check_existing_pr", return_value=(False, None)),
            patch(f"{_P}._sidecar_attempt_context", return_value=CleanupFailure()),
            patch(f"{_P}._contained_execution_for", return_value=MagicMock()),
            patch(f"{_P}._admit_attempt_route_profile"),
            patch(f"{_P}.managed_switchyard_context", return_value=nullcontext(None)),
            patch(f"{_P}.run_agent", return_value=completed) as launch,
        ):
            outcome = _run_retry_loop(
                run,
                prompt="synthetic prompt",
                worktree_dir=tmp_path,
                budget_override_usd=None,
                timeout_override_minutes=None,
                should_pause=None,
                budget_check=None,
                anomaly_threshold_usd=None,
                anomaly_compaction_allowance_usd=0.0,
            )

        failed = outcome.early_return
        assert failed is completed
        assert failed.exit_code == 1
        assert failed.error_category == "exception"
        assert failed.total_tokens == 350
        assert failed.input_tokens == 200
        assert failed.output_tokens == 100
        assert failed.cache_creation_tokens == 40
        assert failed.cache_read_tokens == 10
        assert failed.tool_uses == 7
        assert failed.estimated_cost_usd == 12.50
        assert failed.reported_cost_usd == 12.50
        assert failed.planned_cost_usd == 13.00
        persisted = store.get(run.run_id)
        assert persisted is not None
        assert persisted.status == "failure"
        assert persisted.exit_code == 1
        assert persisted.total_tokens == 350
        assert persisted.input_tokens == 200
        assert persisted.output_tokens == 100
        assert persisted.cache_creation_tokens == 40
        assert persisted.cache_read_tokens == 10
        assert persisted.estimated_cost_usd == 12.50
        assert persisted.planned_cost_usd == 13.00
        [event] = store.list_audit_events(event="dispatch_completed")
        assert event.payload["exit_code"] == 1
        assert event.payload["cost_usd"] == 12.50
        assert event.payload["tokens"] == 350

    assert launch.call_count == 1


@pytest.mark.parametrize("reported_cost", [12.50, 0.0, None])
@pytest.mark.parametrize("classifier", [False, True])
@pytest.mark.parametrize(
    "failure", ["cleanup", "killed_cleanup", "retry_cleanup", "next_startup"]
)
def test_sidecar_failure_finalizes_authoritative_accounting(
    tmp_path: Path, reported_cost: float | None, classifier: bool, failure: str
) -> None:
    from contextlib import nullcontext

    from kew.audit import resolve_actor
    from kew.dispatch_core import _run_retry_loop
    from kew.run_store import RunRecord, SqliteRunStore
    from kew.switchyard_supervisor import SwitchyardSupervisorError

    run = _sidecar_test_run(tmp_path)
    run.actor = resolve_actor()
    run.config.retry.max_attempts = 3
    run.config.retry.backoff_base_seconds = 0
    run.config.agent.max_budget_usd = 100
    run.config.pricing.overrides = {
        "switchyard/passthrough": [10_000.0, 20_000.0],
        "synthetic-classifier": [1_000.0, 2_000.0],
    }

    class CleanupFailure:
        def __enter__(self) -> MagicMock:
            return MagicMock()

        def __exit__(self, *exc_info: object) -> None:
            if failure == "killed_cleanup":
                # The stop lands after the attempt, before cleanup reports failure.
                stopped = store.get(run.run_id)
                assert stopped is not None
                stopped.status = "killed"
                stopped.exit_code = -15
                stopped.error_category = "killed"
                stopped.finished_at = stopped_at
                store.save(stopped)
            raise SwitchyardSupervisorError("sidecar cleanup or absence proof failed")

    class StartupFailure:
        def __enter__(self) -> None:
            raise SwitchyardSupervisorError("sidecar startup unavailable")

        def __exit__(self, *exc_info: object) -> None:
            raise AssertionError("unentered sidecar context")

    costs = [reported_cost]
    if failure == "retry_cleanup":
        costs.append(2.50 if reported_cost else reported_cost)
    results = [
        DispatchResult(
            exit_code=0,
            issue_num=42,
            log_file=tmp_path / "synthetic.log",
            input_tokens=200,
            output_tokens=100,
            cache_creation_tokens=40,
            cache_read_tokens=10,
            total_tokens=350,
            tool_uses=7,
            reported_cost_usd=cost,
            classifier_overhead_model="synthetic-classifier" if classifier else "",
            classifier_overhead_input_tokens=30 if classifier else 0,
            classifier_overhead_output_tokens=20 if classifier else 0,
            session_id=f"synthetic-session-{index}",
        )
        for index, cost in enumerate(costs)
    ]
    contexts: list[Any] = [CleanupFailure()]
    if failure in {"retry_cleanup", "next_startup"}:
        results[0].exit_code = 1
        results[0].error_category = "transient"
        contexts = [
            nullcontext(MagicMock()),
            StartupFailure() if failure == "next_startup" else CleanupFailure(),
        ]

    killed = failure == "killed_cleanup"
    stopped_at = datetime.now(UTC) if killed else None
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        run.store = store
        store.save(
            RunRecord(
                run_id=run.run_id,
                issue_num=run.issue_num,
                item_id=run.item_id,
                branch=run.branch,
                status="pending",
                exit_code=0,
                started_at=datetime.now(UTC),
            )
        )
        with (
            patch(f"{_P}._check_existing_pr", return_value=(False, None)),
            patch(f"{_P}._sidecar_attempt_context", side_effect=contexts) as sidecar,
            patch(f"{_P}._contained_execution_for", return_value=MagicMock()),
            patch(f"{_P}._admit_attempt_route_profile"),
            patch(f"{_P}.managed_switchyard_context", return_value=nullcontext(None)),
            patch(f"{_P}.run_agent", side_effect=results) as launch,
        ):
            outcome = _run_retry_loop(
                run,
                prompt="synthetic prompt",
                worktree_dir=tmp_path,
                budget_override_usd=None,
                timeout_override_minutes=None,
                should_pause=None,
                budget_check=None,
                anomaly_threshold_usd=None,
                anomaly_compaction_allowance_usd=0.0,
            )

        failed = outcome.early_return
        assert failed is results[-1]
        assert outcome.result is None
        assert outcome.pr_confirmed is False
        assert launch.call_count == len(results)
        assert sidecar.call_count == len(contexts)
        persisted = store.get(run.run_id)
        assert persisted is not None
        assert persisted.status == ("killed" if killed else "failure")
        if killed:
            assert persisted.finished_at == stopped_at
        [event] = store.list_audit_events(event="dispatch_completed")
        expected_exit = -15 if killed else 1
        expected_category = "killed" if killed else "exception"
        assert failed.exit_code == persisted.exit_code == expected_exit
        assert event.payload["exit_code"] == expected_exit
        assert failed.error_category == persisted.error_category == expected_category
        assert event.payload["error_category"] == expected_category
        # Startup attempts retain their existing lifecycle count, without usage.
        assert failed.attempt_number == persisted.attempt_number == len(contexts)
        assert event.payload["attempt_number"] == len(contexts)
        for field, per_attempt in (
            ("input_tokens", 200),
            ("output_tokens", 100),
            ("cache_creation_tokens", 40),
            ("cache_read_tokens", 10),
            ("total_tokens", 350),
        ):
            expected = per_attempt * len(results)
            assert getattr(failed, field) == getattr(persisted, field) == expected
        assert event.payload["tokens"] == 350 * len(results)
        assert failed.tool_uses == 7 * len(results)
        assert persisted.session_id == failed.session_id
        assert persisted.log_file == failed.log_file
        overhead = 0.07 if classifier else 0.0
        token_bound = 4.50 * len(results)
        native_cost = sum(cost for cost in costs if cost is not None)
        expected_cost = (
            token_bound if reported_cost is None else native_cost
        ) + overhead
        assert failed.reported_cost_usd == (
            None if reported_cost is None else native_cost
        )
        assert failed.estimated_cost_usd == pytest.approx(expected_cost)
        assert persisted.estimated_cost_usd == pytest.approx(expected_cost)
        assert event.payload["cost_usd"] == pytest.approx(expected_cost)
        assert failed.planned_cost_usd == pytest.approx(token_bound + overhead)
        assert persisted.planned_cost_usd == pytest.approx(token_bound + overhead)
        for measured in (failed, persisted):
            assert measured.classifier_overhead_model == (
                "synthetic-classifier"
                if classifier
                else ("" if measured is failed else None)
            )
            assert measured.classifier_overhead_input_tokens == (
                30 if classifier else 0
            )
            assert measured.classifier_overhead_output_tokens == (
                20 if classifier else 0
            )
            assert measured.classifier_overhead_cost_usd == pytest.approx(overhead)


def test_sidecar_startup_failure_keeps_existing_killed_accounting(
    tmp_path: Path,
) -> None:
    from kew.audit import resolve_actor
    from kew.dispatch_core import _retry_exception_outcome
    from kew.run_store import RunRecord, SqliteRunStore
    from kew.switchyard_supervisor import SwitchyardSupervisorError

    run = _sidecar_test_run(tmp_path)
    run.actor = resolve_actor()
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        run.store = store
        stopped = RunRecord(
            run_id=run.run_id,
            issue_num=run.issue_num,
            item_id=run.item_id,
            branch=run.branch,
            status="killed",
            exit_code=-15,
            error_category="killed",
            estimated_cost_usd=12.50,
            input_tokens=200,
            output_tokens=100,
            total_tokens=300,
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
        )
        store.save(stopped)
        outcome = _retry_exception_outcome(
            run, SwitchyardSupervisorError("sidecar startup unavailable"), 1
        )
        assert outcome.early_return is not None
        assert outcome.early_return.exit_code == -15
        assert store.get(run.run_id) == stopped


@pytest.mark.parametrize("terminal", [None, RuntimeError("worker failed")])
def test_terminal_paths_prove_sidecar_and_network_absent(
    tmp_path: Path, terminal: RuntimeError | None
) -> None:
    from kew.dispatch_core import _sidecar_attempt_context

    run = _sidecar_test_run(tmp_path)
    exits: list[type[BaseException] | None] = []
    supervisor = MagicMock(endpoint="http://172.30.0.2:4000", config_sha256="a" * 64)

    class AttemptContext:
        def __enter__(self) -> MagicMock:
            return supervisor

        def __exit__(self, *exc_info: object) -> None:
            exits.append(exc_info[0] if isinstance(exc_info[0], type) else None)

    with (
        patch(
            "kew.dispatch_core._require_contained_image_id",
            return_value="sha256:" + "c" * 64,
        ),
        patch(
            "kew.dispatch_core.sidecar_switchyard_context",
            return_value=AttemptContext(),
        ),
    ):
        if terminal is None:
            with _sidecar_attempt_context(run):
                pass
        else:
            with (
                pytest.raises(RuntimeError, match="worker failed"),
                _sidecar_attempt_context(run),
            ):
                raise terminal

    assert exits == ([None] if terminal is None else [RuntimeError])


def test_direct_sandbox_sidecar_cleans_after_stream(tmp_path: Path) -> None:
    from kew import dispatch_core
    from kew.dispatch_sandbox import run_container_and_stream
    from kew.sandbox import ContainedExecution

    run = _sidecar_test_run(tmp_path)
    supervisor = MagicMock(
        endpoint="http://172.30.0.2:4000",
        config_sha256="a" * 64,
    )
    events: list[str] = []

    class AttemptContext:
        def __enter__(self) -> MagicMock:
            events.append("topology")
            return supervisor

        def __exit__(self, *exc_info: object) -> None:
            events.append("cleanup")

    execution = ContainedExecution(
        argv=("docker", "exec", "b" * 64, "agent"),
        name="attempt-worker",
        runtime="docker",
        checkout_dir=tmp_path,
        container_id="b" * 64,
        prepared=True,
    )
    handle = MagicMock()
    handle.cleanup_identity.side_effect = lambda: events.append("worker-streamed")
    result = DispatchResult(
        exit_code=-1,
        issue_num=42,
        log_file=tmp_path / "sidecar.log",
        run_id="sidecar-direct",
    )
    ready = []
    with (
        patch(
            "kew.dispatch_sandbox.sidecar_switchyard_context",
            return_value=AttemptContext(),
        ),
        patch(
            "kew.dispatch_sandbox.prepare_sidecar_worker",
            return_value=execution,
        ),
        patch.object(dispatch_core, "_launch_container", return_value=handle) as launch,
        patch.object(dispatch_core, "_stream_sandbox_log", return_value=0),
        patch("kew.dispatch_sandbox.finalize_container_hard_stop"),
        patch("kew.dispatch_sandbox.write_container_evidence"),
    ):
        exit_code = run_container_and_stream(
            run.config,
            None,
            image="sha256:" + "c" * 64,
            runner="claude-code",
            checkout_dir=tmp_path,
            slug="42",
            run_id="sidecar-direct",
            model="switchyard/passthrough",
            log_file=result.log_file,
            run_kind="dispatch",
            result=result,
            inference_transport=run.transport.resolved,
            sidecar_ready=lambda transport, supervisor: ready.append(transport),
        )

    assert exit_code == 0
    assert ready[0].endpoint == "http://172.30.0.2:4000"
    launch.assert_called_once_with(
        list(execution.argv),
        admission=ANY,
        env=ANY,
        credential=None,
        container_name=execution.name,
        container_id=execution.container_id,
        checkout_dir=execution.checkout_dir,
        on_spawn=ANY,
        separate_stderr=False,
    )
    assert events == ["topology", "worker-streamed", "cleanup"]


class TestExistingPrGuardSkipsPrSubjectRuns:
    """The duplicate-PR retry guard applies to runs that open PRs, not to runs
    whose subject already is one (review, remediation) — those now name the PR
    branch on their row so the producer lookup can bind their heads."""

    @staticmethod
    def _run(kind: str):
        from types import SimpleNamespace

        return SimpleNamespace(
            run_kind=kind,
            branch="agent/issue-42",
            config=DispatchConfig(),
            run_id="run-1",
            issue_num=42,
        )

    def test_pr_subject_kinds_never_consult_the_lookup(self, monkeypatch):
        from kew import dispatch_core
        from kew.run_store import PR_SUBJECT_RUN_KINDS

        # The guard swallows lookup failures, so a raising stub proves nothing;
        # record the calls instead, and spell the kinds out so an omitted kind
        # cannot hide behind the production constant.
        looked_up: list[str] = []
        monkeypatch.setattr(
            dispatch_core,
            "check_pr_exists",
            lambda branch, config: looked_up.append(branch) or 99,
        )
        kinds = (
            "review",
            "review_remediation",
            "adversarial_review",
            "review_stage",
            "ci_fix",
            "resolve",
        )
        assert set(kinds) == set(PR_SUBJECT_RUN_KINDS)
        for kind in kinds:
            assert dispatch_core._check_existing_pr(
                self._run(kind), 1, "prev", "current", None
            ) == (False, "current")
        assert looked_up == []

    def test_a_dispatch_still_stops_on_an_existing_pr(self, monkeypatch):
        from kew import dispatch_core

        monkeypatch.setattr(dispatch_core, "check_pr_exists", lambda b, c: 99)
        monkeypatch.setattr(dispatch_core, "update_pr_labels", lambda *a, **k: None)
        assert dispatch_core._check_existing_pr(
            self._run("dispatch"), 1, "prev", "current", None
        ) == (True, "prev")


class TestContinuationRefusesPrSubjectKinds:
    """A continuation resumes an implementation. The run store exempts
    PR-subject rows from its retained-claim check, so admission must refuse
    such a kind before any store lookup (#1923 round 3)."""

    @pytest.mark.parametrize(
        "kind", ["review", "review_remediation", "adversarial_review"]
    )
    def test_prepared_continuation_refuses_the_kind_first(self, tmp_path, kind):
        from kew import dispatch_core

        with pytest.raises(ValueError, match="PR-subject run kind"):
            dispatch_core._validate_prepared_run_identity(
                store=None,
                epic_ref="alpha",
                item_id="42",
                worktree_dir=tmp_path,
                branch="agent/issue-42",
                branch_override="agent/issue-42",
                run_id="continued",
                run_kind=kind,
            )

    def test_a_dispatch_continuation_reaches_the_claim_check(self, tmp_path):
        from kew import dispatch_core

        with pytest.raises(ValueError, match="requires a store"):
            dispatch_core._validate_prepared_run_identity(
                store=None,
                epic_ref="alpha",
                item_id="42",
                worktree_dir=tmp_path,
                branch="agent/issue-42",
                branch_override="agent/issue-42",
                run_id="continued",
                run_kind="dispatch",
            )

    def test_tracked_continuation_refuses_the_kind_first(self, tmp_path):
        from kew import dispatch_core

        with pytest.raises(ValueError, match="PR-subject run kind"):
            dispatch_core._validate_tracked_run_identity(
                validate=True,
                store=None,
                epic_ref="alpha",
                item_id="42",
                worktree_dir=tmp_path,
                branch="agent/issue-42",
                run_id="continued",
                run_kind="review_remediation",
            )

    def test_a_fresh_tracked_run_is_not_a_continuation(self, tmp_path):
        from kew import dispatch_core

        dispatch_core._validate_tracked_run_identity(
            validate=True,
            store=None,
            epic_ref=None,
            item_id="42",
            worktree_dir=tmp_path,
            branch="agent/issue-42",
            run_id=None,
            run_kind="review_remediation",
        )


class TestSidecarAttestationBeforeLaunch:
    """A sidecar dispatch defers its attestation to the launch; a refusal between
    the direct attestation point and the launch must still leave exactly one
    ``sandbox_resolved`` record, as the direct transport always did (round 3)."""

    @pytest.mark.parametrize("sidecar", [True, False], ids=["sidecar", "direct"])
    def test_checkout_binding_refusal_attests_exactly_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sidecar: bool
    ) -> None:
        from kew import dispatch_core, worktree
        from kew.run_store import SqliteRunStore

        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        (checkout / ".git").mkdir(exist_ok=True)
        worktree.write_checkout_generation(checkout)
        decision = TestInitialCanonicalCheckoutBinding._decision(sandboxed=True)
        decision.egress_verification = "unavailable"
        emitted: list[dict[str, Any]] = []
        monkeypatch.setattr(
            dispatch_core, "_resolve_and_apply_sandbox", lambda *_a, **_k: decision
        )
        monkeypatch.setattr(
            dispatch_core, "_trusted_dispatch_base", lambda _c: "a" * 40
        )
        monkeypatch.setattr(
            dispatch_core, "_emit_sandbox_resolved", lambda *_a, **k: emitted.append(k)
        )
        monkeypatch.setattr(
            dispatch_core, "check_host_capacity", lambda *_a, **_k: None
        )
        monkeypatch.setattr(dispatch_core, "setup_worktree", lambda **_k: checkout)
        monkeypatch.setattr(
            dispatch_core, "_materialize_checkout", lambda *_a, **_k: checkout
        )

        def replaced(*_a, **_k):
            raise RuntimeError("checkout replaced concurrently")

        monkeypatch.setattr(dispatch_core, "_bind_initial_checkout", replaced)
        monkeypatch.setattr(
            dispatch_core,
            "_launch_sandboxed",
            lambda *_a, **_k: pytest.fail("the launch must not run"),
        )
        runner = MagicMock()
        runner.name = SANDBOX_RUNNER
        runner.publication_style = "host"
        resolved = MagicMock(runner=runner, model="claude-sonnet-4-6")
        with SqliteRunStore(str(tmp_path / "runs.db")) as store:
            ctx = TestInitialCanonicalCheckoutBinding._context(
                dispatch_core, config, store, run_id="run-42"
            )
            with pytest.raises(RuntimeError, match="replaced concurrently"):
                dispatch_core._launch_workspace(
                    ctx,
                    dispatch_core._LaunchSetup(),
                    config=config,
                    resolved=resolved,
                    runner=runner,
                    early=dispatch_core._EarlyRunInfo(runner=SANDBOX_RUNNER),
                    admit_budget_check=None,
                    inference_transport=MagicMock(sidecar=sidecar),
                )

        assert [e["egress_verification"] for e in emitted] == ["unavailable"]
        assert emitted[0]["posture"] == decision.posture


class TestTerminalOutcomeKeepsRunnerCategory:
    """A finished child names its runner category, never 'failed - exit 1'."""

    def _result(self, **overrides: Any) -> Any:
        from kew.tracking import DispatchResult

        fields: dict[str, Any] = {
            "exit_code": 1,
            "issue_num": 42,
            "log_file": Path("/logs/run-42.log"),
            "error_category": "killed",
            "attempt_number": 2,
            "run_id": "run-42",
        }
        fields.update(overrides)
        return DispatchResult(**fields)

    def test_outcome_names_category_attempts_and_log(self) -> None:
        from kew.dispatch_core import _terminal_outcome

        outcome = _terminal_outcome(
            self._result(), exit_code=1, detail="worktree preserved for debugging"
        )

        assert outcome.exit_code == 1
        assert outcome.category == "killed"
        assert outcome.run_id == "run-42"
        assert outcome.reason == (
            "run ended as killed after 2 attempt(s); worktree preserved for "
            "debugging; log: /logs/run-42.log"
        )

    def test_explicit_category_wins_and_missing_category_is_failed(self) -> None:
        from kew.dispatch_core import _terminal_outcome

        explicit = _terminal_outcome(
            self._result(), exit_code=1, category="rescued_incomplete"
        )
        assert explicit.category == "rescued_incomplete"
        assert explicit.reason.startswith("run ended as rescued_incomplete after 2")

        blank = _terminal_outcome(
            self._result(error_category="", attempt_number=0), exit_code=0
        )
        assert blank.exit_code == 1
        assert blank.category == "failed"
        assert blank.reason.startswith("run ended as failed after 1 attempt(s)")

    def test_hard_fail_outcome_carries_runner_category(self) -> None:
        from types import SimpleNamespace

        from kew.dispatch_core import _collect_hard_fail_or_rescue

        ctx = SimpleNamespace(
            config=DispatchConfig(), issue_num=42, epic_ref=None, item_id="42"
        )
        result = self._result(error_category="timeout", attempt_number=3)
        with (
            patch(f"{_P}._try_rescue_incomplete", return_value=False),
            patch(f"{_P}._publish_terminal_failure"),
            patch(f"{_P}._print_failure_next_steps"),
            patch(f"{_P}._notify_dispatch_failure"),
            patch(f"{_P}._error"),
            patch(f"{_P}._warn"),
        ):
            outcome = _collect_hard_fail_or_rescue(ctx, result, Path("/wt"))  # type: ignore[arg-type]

        assert outcome.category == "timeout"
        assert "after 3 attempt(s)" in outcome.reason
        assert outcome.reason != "dispatch ended with exit code 1"


@pytest.mark.parametrize(
    "diagnostic", ["reason", "oom_probe_diagnostic", "resource_cap_observations"]
)
def test_breaker_audit_redacts_inherited_proxy(tmp_path, monkeypatch, diagnostic):
    from kew.run_store import SqliteRunStore

    secret = "http://user:s3cret@proxy.invalid:3128"
    monkeypatch.setenv("HTTP_PROXY", secret)
    result = DispatchResult(exit_code=137, issue_num=42, log_file=tmp_path / "l")
    result.hard_stop = "resource_cap"
    result.resource_cap_reason = secret
    result.oom_probe_diagnostic = secret
    result.resource_cap_observations = [{"code": "oom", "reason": secret}]
    with SqliteRunStore(str(tmp_path / "proxy.db")) as store:
        [event] = TestBreakerEventForHardKills._emit(store, tmp_path, result)
    assert secret not in json.dumps(event.payload[diagnostic])
    assert "redacted" in json.dumps(event.payload[diagnostic])


def test_container_exception_output_is_redacted(monkeypatch):
    from kew import dispatch_core

    secret = "http://user:s3cret@proxy.invalid:3128"
    monkeypatch.setenv("HTTP_PROXY", secret)
    errors = []
    monkeypatch.setattr(dispatch_core, "_error", errors.append)
    dispatch_core._classify_retry_exception(RuntimeError(f"container failed: {secret}"))
    assert len(errors) == 1
    assert secret not in errors[0]
    assert "container failed: <HTTP_PROXY redacted>" in errors[0]


@pytest.mark.parametrize("field", ["resolved_model", "reported_service_tier"])
def test_fallback_report_redacts_inherited_proxy(tmp_path, monkeypatch, field):
    from kew.dispatch_core import _fallback_report_body

    secret = "http://user:s3cret@proxy.invalid:3128"
    monkeypatch.setenv("HTTP_PROXY", secret)
    result = DispatchResult(exit_code=0, issue_num=1933, log_file=tmp_path / "l")
    setattr(result, field, secret)
    report = _fallback_report_body("1933", "simple", result)
    assert secret not in report
    assert "<HTTP_PROXY redacted>" in report


@pytest.mark.parametrize(
    ("field", "surface"),
    [
        (field, surface)
        for field in ("model", "service_tier")
        for surface in ("record", "audit", "report")
    ]
    + [("model", "sidecar")],
)
def test_codex_proxy_identity_never_reaches_retained_surfaces(
    tmp_path, monkeypatch, field, surface
):
    from dataclasses import asdict
    from io import StringIO

    from kew.dispatch_core import _finalize_result, _RetryOutcome, _save_pending_run
    from kew.dispatch_sandbox import (
        _accumulate_stream_line,
        _runner_totals,
        _SandboxTelemetry,
    )
    from kew.orchestrator import _process_line
    from kew.report import _render_switchyard_accounting
    from kew.run_store import SqliteRunStore
    from kew.runners.codex import CodexRunner
    from kew.transcript_metrics import StreamDerivation, write_derived_evidence

    password = "s3cret" * (60 if field == "model" else 1)
    secret = f"http://user:{password}@proxy.invalid:3128"
    monkeypatch.setenv("HTTP_PROXY", secret)
    line = json.dumps(
        {
            "type": "thread.started",
            "thread_id": "12345678-1234-5678-9abc-123456789abc",
            field: secret,
        }
    )
    with SqliteRunStore(str(tmp_path / "identity.db")) as store:
        run = _profile_run(store, tmp_path, runner=CodexRunner())
        _save_pending_run(
            run, worker_cap=0, cap_exclude_issue=None, admit_budget_check=None
        )
        result = DispatchResult(exit_code=0, issue_num=42, log_file=run.log_file)
        derivation = StreamDerivation()
        _process_line(
            line, StringIO(), None, run.runner, result, "", None, None, derivation
        )
        live = _SandboxTelemetry()
        _accumulate_stream_line(live, line, CodexRunner())
        derivation.record_totals(_runner_totals(live))
        sidecar = write_derived_evidence(run.log_file, derivation.evidence())
        assert sidecar is not None
        _finalize_result(
            run, _RetryOutcome(result=result, last_exit_code=0, total_attempts=1)
        )
        record = store.get(run.run_id)
        assert record is not None
        [event] = store.list_audit_events(event="dispatch_completed")
        record.transport_mode = "switchyard"
        record.transport_route = "switchyard/test"
        report = "\n".join(_render_switchyard_accounting([record]))
        assert "Reported service tier" in report
        surfaces = {
            "record": json.dumps(asdict(record), default=str),
            "audit": json.dumps(event.payload),
            "report": report,
            "sidecar": sidecar.read_text(),
        }
        assert "s3cret" not in surfaces[surface]
        assert "s3cret" not in "\n".join(store._conn.iterdump())
        if field == "service_tier":
            assert record.reported_service_tier == "invalid"


def test_completion_without_runner_discards_unvalidated_session(tmp_path):
    from kew.dispatch_core import _persist_completion, _RetryOutcome
    from kew.run_store import RunRecord, SqliteRunStore

    with SqliteRunStore(str(tmp_path / "missing-runner.db")) as store:
        run = _profile_run(store, tmp_path, runner=None)
        store.save(
            RunRecord(
                run_id=run.run_id,
                issue_num=42,
                branch=run.branch,
                status="running",
                exit_code=0,
                started_at=datetime.now(UTC),
            )
        )
        result = DispatchResult(
            exit_code=0,
            issue_num=42,
            log_file=run.log_file,
            session_id="12345678-1234-5678-9abc-123456789abc",
        )
        _persist_completion(
            run, result, _RetryOutcome(last_exit_code=0, total_attempts=1), None
        )
        assert store.get(run.run_id).session_id is None
        assert result.session_id is None
        assert (
            result.session_id_rejection == "runner unavailable for session validation"
        )


@pytest.mark.parametrize("source", ["init", "completion"])
def test_proxy_session_never_reaches_attempt_storage_or_resume(
    tmp_path, monkeypatch, source
):
    from io import StringIO

    from kew.dispatch_core import (
        _persist_completion,
        _resume_or_fresh,
        _RetryOutcome,
        _save_pending_run,
    )
    from kew.orchestrator import _process_line
    from kew.run_store import SqliteRunStore
    from kew.transcript_metrics import StreamDerivation

    secret = "http://user:s3cret@proxy.invalid:3128"
    monkeypatch.setenv("HTTP_PROXY", secret)
    with SqliteRunStore(str(tmp_path / "sessions.db")) as store:
        run = _profile_run(store, tmp_path)
        _save_pending_run(
            run, worker_cap=0, cap_exclude_issue=None, admit_budget_check=None
        )
        result = DispatchResult(exit_code=0, issue_num=42, log_file=run.log_file)
        if source == "init":
            line = json.dumps(
                {"type": "system", "subtype": "init", "session_id": secret}
            )
            _process_line(
                line,
                StringIO(),
                None,
                run.runner,
                result,
                "",
                None,
                None,
                StreamDerivation(),
            )
        else:
            result.session_id = secret
        _persist_completion(
            run, result, _RetryOutcome(last_exit_code=0, total_attempts=1), None
        )

        assert store.get(run.run_id).session_id is None
        assert _resume_or_fresh(0, result, run) == (None, "fresh")
        assert result.session_id is None
        [rejection] = store.list_audit_events(event="session_id_rejected")
        assert rejection.payload["reason"] == "credential-bearing session identifier"
        # The complete database includes the run, immutable attempt identity, and audit.
        assert "s3cret" not in "\n".join(store._conn.iterdump())


@pytest.mark.parametrize("publication", ["canonical", "checkout"])
@pytest.mark.parametrize("boundary", ["host", "contained"])
def test_fresh_checkout_binding_attests_replacement(
    tmp_path, monkeypatch, publication, boundary
):
    from kew import dispatch_core, worktree
    from kew.config import DispatchConfig
    from kew.run_store import SqliteRunStore

    config = DispatchConfig()
    config.publication.publish = publication
    checkout = tmp_path / "fresh"
    checkout.mkdir()
    (checkout / ".git").mkdir(exist_ok=True)
    generation = worktree.write_checkout_generation(checkout)
    # The base writer is independent; retain the real generation validation.
    monkeypatch.setattr(
        dispatch_core, "_record_publication_base", lambda *args, **kwargs: None
    )
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        ctx = TestInitialCanonicalCheckoutBinding._context(
            dispatch_core, config, store, run_id="fresh"
        )
        if boundary == "host":
            dispatch_core._bind_host_checkout_before_runner(
                ctx,
                checkout,
                continuation_dispatch=False,
                checkout_generation="",
            )
        else:
            dispatch_core._bind_contained_checkout(
                ctx,
                checkout,
                runner="codex",
                decision=TestInitialCanonicalCheckoutBinding._decision(sandboxed=True),
                inference_transport=None,
            )
        events = store.list_audit_events(event="checkout_replaced", run_id="fresh")
        assert len(events) == 1
        assert events[0].payload == {
            "epic_ref": "alpha",
            "item_id": "42",
            "branch": "agent/issue-42",
            "checkout": str(checkout.resolve()),
            "checkout_id": generation,
        }
        # A continuation handoff must never publish fresh-checkout evidence.
        dispatch_core._bind_host_checkout_before_runner(
            ctx,
            checkout,
            continuation_dispatch=True,
            checkout_generation=generation,
        )
        assert (
            store.list_audit_events(event="checkout_replaced", run_id="fresh") == events
        )

        # A lost attestation must stop launch before new worker edits can exist.
        monkeypatch.setattr(dispatch_core.audit, "emit", lambda *args, **kwargs: None)
        ctx.run_id = "unattested"
        with pytest.raises(
            dispatch_core.PublicationError, match="not durably recorded"
        ):
            dispatch_core._bind_host_checkout_before_runner(
                ctx, checkout, continuation_dispatch=False, checkout_generation=""
            )


@pytest.mark.parametrize("gate", ["managed_credentials", "launch_credentials"])
def test_round_four_real_retry_gates_never_report_agent_execution(
    gate, tmp_path, monkeypatch
):
    from support.dispatch_identity import install_dispatch_identity

    install_dispatch_identity(monkeypatch)
    from dataclasses import replace

    from test_switchyard_supervisor import _settings

    import kew.cli as cli
    from kew.dispatch_core import (
        _collect_special_category,
        _run_retry_loop,
        _save_pending_run,
        _terminal_outcome,
    )
    from kew.inference_transport import resolve_inference_transport
    from kew.run_store import SqliteRunStore
    from kew.runners.codex import CodexRunner

    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        run = _profile_run(store, tmp_path)
        if gate == "managed_credentials":
            settings = _settings(
                tmp_path,
                credential_env=("OPENAI_API_KEY",),
                route_credential_env="OPENAI_API_KEY",
            )
            monkeypatch.delenv("OPENAI_API_KEY", raising=False)
            run.config.inference_transport = InferenceTransportConfig(
                mode="switchyard", switchyard=settings
            )
            run.runner = CodexRunner()
            run.transport = replace(
                run.transport,
                resolved=resolve_inference_transport(
                    run.config, run.runner, "original-model"
                ),
            )
        else:
            from kew.sandbox import runner_credential_env_vars

            # A contained launch with no auth evidence is the real launch gate.
            run.contained_image = "kew-agent:latest"
            for name in runner_credential_env_vars("claude-code"):
                monkeypatch.delenv(name, raising=False)
        with (
            patch(f"{_P}._detect_sandbox_runtime", return_value="docker"),
            patch(
                "kew.sandbox.inspect_image_id",
                return_value=ImageResolution(
                    ImageIdStatus.RESOLVED, image_id="sha256:" + "d" * 64
                ),
            ),
            patch(f"{_P}.run_agent") as launch,
            patch("kew.switchyard_supervisor.subprocess.Popen") as proxy_launch,
        ):
            _save_pending_run(
                run, worker_cap=0, cap_exclude_issue=None, admit_budget_check=None
            )
            retry = _run_retry_loop(
                run,
                prompt="fix",
                worktree_dir=tmp_path,
                budget_override_usd=None,
                timeout_override_minutes=None,
                should_pause=None,
                budget_check=None,
                anomaly_threshold_usd=None,
                anomaly_compaction_allowance_usd=0.0,
            )
        result = retry.early_return
        assert result is not None
        if gate == "managed_credentials":
            outcome = _terminal_outcome(result, exit_code=result.exit_code)
        else:
            outcome = _collect_special_category(MagicMock(), result, tmp_path, True)
        with patch("kew.cli.execute_dispatch", return_value=outcome):
            command = cli._execute_and_capture_outcome(
                issue=_make_issue(),
                config=run.config,
                store=None,
                notifier=None,
                force_budget=False,
                approve=False,
                model=None,
            )
    assert command.kind == "not_launched"
    assert cli._dispatch_exit_code(command) == 2
    assert result.launched is False
    launch.assert_not_called()
    assert not any(
        "switchyard" in str(c.args[0][0]) for c in proxy_launch.call_args_list
    )
    expected = (
        "exception"
        if gate == "managed_credentials"
        else "credential_publication_refused"
    )
    assert result.error_category == expected


def test_round_four_container_launch_marks_execution(tmp_path):
    from kew.dispatch_sandbox import run_container_and_stream

    result = DispatchResult(1, 42, tmp_path / "run.log")
    handle = MagicMock()
    with (
        patch.object(handle, "observe_workload_start", return_value=True),
        patch("kew.dispatch_sandbox._launch_streaming_container", return_value=handle),
        patch("kew.dispatch_core._stream_sandbox_log", return_value=137),
        patch("kew.dispatch_sandbox.finalize_container_hard_stop"),
        patch("kew.dispatch_sandbox.write_container_evidence"),
    ):
        code = run_container_and_stream(
            DispatchConfig(),
            MagicMock(),
            image="image",
            runner="claude-code",
            checkout_dir=tmp_path,
            slug="42",
            run_id="run-42",
            model="claude-opus-5",
            log_file=result.log_file,
            run_kind="dispatch",
            result=result,
        )
    assert getattr(result, "launched", False) is True
    assert code == 137


@pytest.mark.parametrize("with_store", [False, True])
def test_round_five_stream_exception_retains_launch(tmp_path, with_store):
    from contextlib import nullcontext
    from types import SimpleNamespace

    from test_orchestrator import FakeRunner, TestRunAgent

    import kew.cli as cli
    from kew.dispatch_core import _run_retry_loop, _terminal_outcome
    from kew.run_store import SqliteRunStore

    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        run = _profile_run(store if with_store else None, tmp_path)
        run.runner = FakeRunner()
        launch_state = SimpleNamespace(launched=False)
        run.launch_state = launch_state
        proc = TestRunAgent()._mock_popen(exit_code=1)
        proc.pid = 1234
        with (
            patch(f"{_P}._check_existing_pr", return_value=(False, None)),
            patch(f"{_P}._sidecar_attempt_context", return_value=nullcontext(None)),
            patch(f"{_P}._admit_attempt_route_profile"),
            patch(f"{_P}._contained_execution_for", return_value=None),
            patch(f"{_P}.managed_switchyard_context", return_value=nullcontext(None)),
            patch("kew.orchestrator.popen", return_value=proc) as spawn,
            patch("kew.orchestrator.find_timeout_cmd", return_value=None),
            patch(
                "kew.orchestrator._stream_output",
                side_effect=OSError(28, "No space left on device"),
            ),
        ):
            retry = _run_retry_loop(
                run,
                prompt="fix",
                worktree_dir=tmp_path,
                budget_override_usd=None,
                timeout_override_minutes=None,
                should_pause=None,
                budget_check=None,
                anomaly_threshold_usd=None,
                anomaly_compaction_allowance_usd=0.0,
            )
        result = retry.early_return
        assert result is not None
        spawn.assert_called_once()
        assert result.launched is True
        assert launch_state.launched is True
        outcome = _terminal_outcome(result, exit_code=result.exit_code)
        with patch("kew.cli.execute_dispatch", return_value=outcome):
            command = cli._execute_and_capture_outcome(
                issue=_make_issue(),
                config=run.config,
                store=None,
                notifier=None,
                force_budget=False,
                approve=False,
                model=None,
            )
        assert command.kind == "executed"
        assert cli._dispatch_exit_code(command) == 1


def test_round_six_container_exceptions_preserve_partial_result(tmp_path):
    from types import SimpleNamespace

    from kew.claim_retention import RefusalDelivery
    from kew.cli import _dispatch_exit_code
    from kew.dispatch_core import _launch_runner, _terminal_outcome
    from kew.dispatch_sandbox import run_container_and_stream

    for started in (True, False):
        config = DispatchConfig()
        # The caller's refusal callback is delivered once, as in production.
        on_refuse = MagicMock()
        ctx = SimpleNamespace(
            config=config,
            complexity="medium",
            model_override=None,
            issue_num=42,
            store=None,
            item_id="42",
            run_id="run-42",
            start_monotonic=0,
            epic_ref=None,
            refuse_rollback=RefusalDelivery(on_refuse),
            expected_reservation_run_id=None,
            expected_reservation_generation=None,
            admitted_run_id=None,
            on_admitted=None,
            claim_launch_started=False,
            container_cleanup_pending=False,
        )
        partial = DispatchResult(
            1,
            42,
            tmp_path / "run.log",
            run_id="run-42",
            attempt_number=2,
            total_tokens=17,
        )
        handle = MagicMock(container_id="a" * 64)

        def workspace(_ctx, setup, *, config=config, partial=partial, **kwargs):
            setup.worktree_dir = tmp_path
            setup.run_sandboxed = True
            return run_container_and_stream(
                config,
                None,
                image="image",
                runner="claude-code",
                checkout_dir=tmp_path,
                slug="42",
                run_id="run-42",
                model="claude-opus-5",
                log_file=partial.log_file,
                run_kind="dispatch",
                result=partial,
            )

        with (
            patch.object(handle, "observe_workload_start", return_value=True),
            patch(f"{_P}.resolve_runner"),
            patch(f"{_P}._preflight_pricing"),
            patch(f"{_P}._require_dispatch_transport_admission"),
            patch(f"{_P}._launch_workspace", side_effect=workspace),
            patch(f"{_P}.mark_failed"),
            patch(f"{_P}._emit_early_exit"),
            patch(
                "kew.dispatch_sandbox._launch_streaming_container",
                return_value=handle,
                side_effect=None if started else RuntimeError("start refused"),
            ),
            patch(
                f"{_P}._stream_sandbox_log",
                side_effect=OSError(28, "No space left on device"),
            ),
            patch("kew.dispatch_sandbox._finalize_sandbox_run"),
        ):
            launched = _launch_runner(ctx, None)
        outcome = launched.early or _terminal_outcome(launched.result, exit_code=1)
        assert outcome.launched is started
        command = SimpleNamespace(
            kind="executed" if outcome.launched else "not_launched",
            exit_code=outcome.exit_code,
        )
        assert _dispatch_exit_code(command) == (1 if started else 2)
        if started:
            assert launched.result is partial
            assert partial.container_id == "a" * 64
            assert partial.attempt_number == 2
            assert partial.total_tokens == 17
            assert partial.error_category == "sandbox_stream_error"
            assert launched.worktree_dir == tmp_path
            on_refuse.assert_not_called()
        else:
            on_refuse.assert_called_once_with("start refused")


@pytest.mark.parametrize(
    "terminal", ["success", "failure", "killed", "cost_anomaly", "timeout", "exception"]
)
@pytest.mark.parametrize("run_kind, cap", [("dispatch", None), ("remediation", 7.0)])
def test_bounded_tracked_route_owns_key_until_terminal(
    tmp_path, monkeypatch, caplog, terminal, run_kind, cap, process_table
):
    from kew import credential_issuer
    from kew.dispatch_core import run_tracked_agent
    from kew.run_store import SqliteRunStore
    from kew.runners import get_runner

    owner = os.getpid()
    process_table.members[owner] = (owner, "S", "python3", "kew dispatch")
    monkeypatch.setattr("kew.process_groups.exclusive_owner_pid", lambda: owner)
    monkeypatch.setenv("OPENROUTER_PROVISIONING_KEY", "management-secret-1944")
    monkeypatch.setenv("OPENROUTER_API_KEY", "host-secret-1944")
    issued_at = datetime.now(UTC).replace(microsecond=0)

    class IssuanceClock(datetime):
        @classmethod
        def now(cls, tz=None):
            assert tz == UTC
            return issued_at

    # The key's expiry is read off this clock; a loaded host must not skew it.
    monkeypatch.setattr("kew.run_credentials.datetime", IssuanceClock)

    from functools import partial

    mint_mock, revoke = MagicMock(), MagicMock()
    monkeypatch.setattr(credential_issuer, "mint_bounded_run_key", mint_mock)
    monkeypatch.setattr(credential_issuer, "revoke_run_key", revoke)
    config = DispatchConfig(retry=RetryConfig(max_attempts=1))
    launch = MagicMock(
        return_value=DispatchResult(
            exit_code=0 if terminal == "success" else 1,
            issue_num=1944,
            log_file=tmp_path / "run.log",
            error_category="" if terminal == "success" else terminal,
            breaker_rule="cost_anomaly" if terminal == "cost_anomaly" else "",
        )
    )
    if terminal == "exception":
        launch.side_effect = RuntimeError("launch failed")
    monkeypatch.setattr("kew.dispatch_core.run_agent", launch)
    monkeypatch.setattr("kew.dispatch_core.check_pr_exists", lambda *a, **k: None)
    with SqliteRunStore(str(tmp_path / "issued.db"), project_root=tmp_path) as store:
        mint_mock.side_effect = partial(_bounded_tracked_mint, store)
        run_tracked_agent(
            prompt="test",
            worktree_dir=tmp_path,
            config=config,
            issue_num=1944,
            item_id="1944",
            branch="agent/1944",
            store=store,
            run_id="bounded-tracked",
            runner=get_runner("opencode"),
            model="openrouter/test",
            budget_override_usd=cap,
            timeout_override_minutes=2,
            run_kind=run_kind,
            validate_continuation=False,
        )
        revoke.assert_called_once_with("a" * 64, issued_key="run-secret-1944")
        assert mint_mock.call_args.args[1] == (25.0 if cap is None else cap)
        # One 2-minute attempt plus the 300-second expiry grace.
        assert mint_mock.call_args.args[2] == issued_at + timedelta(seconds=420)
        key = launch.call_args.kwargs["credential"]
        assert key.secret == "run-secret-1944"
        profile = store.require_route_profile("bounded-tracked", 1)
        assert profile.auth_path == "issued:openrouter:" + "a" * 64
        assert (
            json.loads(profile.profile_json)["credential_issuer"]["value"]
            == key.identity
        )
        events = store.list_audit_events(event="sandbox_resolved", limit=100)
        assert any(
            e.payload.get("credential_issuer", {}).get("revoked") is True
            for e in events
        )
        evidence = (
            str(events)
            + profile.profile_json
            + str(store.get("bounded-tracked"))
            + caplog.text
        )
        assert "run-secret-1944" not in evidence
        assert "management-secret-1944" not in evidence
        assert os.environ["OPENROUTER_API_KEY"] == "host-secret-1944"


@pytest.mark.parametrize("run_kind", ["ci_fix", "resolve"])
def test_trackerless_publication_keeps_admission_synthetic(run_kind):
    from types import SimpleNamespace

    from kew.dispatch_core import _admission_item_id

    run = SimpleNamespace(
        run_kind=run_kind, item_id="", issue_num=99, source_tracker="github"
    )
    assert _admission_item_id(run) is None
    run.item_id = "SFD-258"
    run.source_tracker = "linear"
    assert _admission_item_id(run) == "SFD-258"


@pytest.mark.parametrize("started", [False, True])
def test_container_registration_preserves_permit_without_claiming_startup(
    tmp_path, started
):
    from kew.dispatch_core import _exception_result, _record_retry_spawn
    from kew.tracking import DispatchResult, LaunchState

    store = MagicMock()
    run = _profile_run(
        store, tmp_path, contained_image="image", launch_state=LaunchState()
    )
    permit = MagicMock()
    with patch("kew.dispatch_core._record_retry_pgid", return_value=permit) as register:
        final_permit = _record_retry_spawn(run, 12345)
    register.assert_called_once_with(store, run.run_id, 12345)
    # The registration's permit is verified by the returned final permit.
    assert final_permit is not None
    permit.assert_not_called()
    final_permit()
    permit.assert_called_once_with()
    assert run.launched is False
    assert run.launch_state.launched is False
    result = DispatchResult(1, 42, run.log_file, launched=started)
    assert _exception_result(run, 1, result).launched is started
    assert run.launch_state.launched is started


@pytest.mark.parametrize("timeout_minutes", [None, 2])
@pytest.mark.parametrize("delay_before_attempt", [None, 1, 2])
def test_bounded_retry_schedule_keeps_deadlines_and_shared_spend(
    tmp_path, monkeypatch, timeout_minutes, delay_before_attempt, process_table
):
    from datetime import timedelta

    from kew import credential_issuer, dispatch_core, run_credentials
    from kew.run_store import SqliteRunStore
    from kew.runners import get_runner

    class Clock(datetime):
        current = datetime(2026, 9, 15, tzinfo=UTC)

        @classmethod
        def now(cls, tz=None):
            return cls.current

    def advance(seconds):
        Clock.current += timedelta(seconds=seconds)

    config = DispatchConfig()
    timeout = (timeout_minutes or config.agent.timeout_minutes) * 60
    start = Clock.current
    launches = []

    def launch(**kwargs):
        key = kwargs["credential"]
        launches.append((key, Clock.current + timedelta(seconds=timeout)))
        advance(timeout)
        return DispatchResult(
            exit_code=124,
            issue_num=1944,
            log_file=tmp_path / "run.log",
            error_category="timeout",
        )

    admissions = 0

    def admit():
        nonlocal admissions
        admissions += 1
        if admissions == delay_before_attempt:
            # Setup consumes the entire grace allowance before this launch.
            advance(301)

    owner = os.getpid()
    process_table.members[owner] = (owner, "S", "python3", "kew dispatch")
    monkeypatch.setattr("kew.process_groups.exclusive_owner_pid", lambda: owner)
    monkeypatch.setenv("OPENROUTER_PROVISIONING_KEY", "management-secret-1944")
    monkeypatch.setattr(run_credentials, "datetime", Clock)
    mint_mock, revoke = MagicMock(side_effect=_bounded_retry_mint), MagicMock()
    monkeypatch.setattr(credential_issuer, "mint_bounded_run_key", mint_mock)
    monkeypatch.setattr(credential_issuer, "revoke_run_key", revoke)
    monkeypatch.setattr(dispatch_core, "run_agent", launch)
    monkeypatch.setattr(dispatch_core, "check_pr_exists", lambda *a, **k: None)
    monkeypatch.setattr(dispatch_core.time, "sleep", advance)
    monkeypatch.setattr(dispatch_core.random, "uniform", lambda low, high: high)
    with SqliteRunStore(str(tmp_path / "retry.db"), project_root=tmp_path) as store:
        result = dispatch_core.run_tracked_agent(
            prompt="test",
            worktree_dir=tmp_path,
            config=config,
            issue_num=1944,
            item_id="1944",
            branch="agent/1944",
            store=store,
            run_id="retry-bound",
            runner=get_runner("opencode"),
            model="openrouter/test",
            budget_override_usd=7.0,
            timeout_override_minutes=timeout_minutes,
            run_kind="remediation",
            validate_continuation=False,
            paid_launch_check=admit,
        )
        if delay_before_attempt is not None:
            assert len(launches) == delay_before_attempt - 1
            assert result.error_category == "credential_lifetime_refused"
            assert store.get("retry-bound").status == "refused"
        else:
            assert len(launches) == 3
            expiry = datetime.fromisoformat(launches[-1][0].expires_at)
            assert expiry > launches[-1][1]
            assert (expiry - start).total_seconds() == 3 * timeout + 1.25 * (
                30 + 60
            ) + 300
        keys = [key for key, _ in launches]
        assert all(key is keys[0] and key.limit_usd == 7.0 for key in keys)
        mint_mock.assert_called_once()
        assert mint_mock.call_args.args[1] == 7.0
        revoke.assert_called_once_with("a" * 64, issued_key="run-secret-1944")


def _bounded_tracked_mint(store, run_id, limit, expiry, *, ledger_id, on_malformed):
    from kew import credential_issuer

    assert ledger_id == store.read_ledger_id()
    [posture] = store.list_audit_events(event="sandbox_resolved", run_id=run_id)
    assert posture.payload["posture"] == "host"
    assert posture.payload["attempt_number"] == 1
    return credential_issuer.BoundedRunKey(
        "run-secret-1944", "a" * 64, limit, expiry.isoformat().replace("+00:00", "Z")
    )


def _bounded_retry_mint(run_id, limit, expiry, *, ledger_id, on_malformed):
    from kew import credential_issuer

    return credential_issuer.BoundedRunKey(
        "run-secret-1944", "a" * 64, limit, expiry.isoformat()
    )


def test_the_spawn_permit_asks_each_runtime_for_the_identity_a_stop_can_use(
    tmp_path: Path,
):
    """A gateway sandbox has no container id, and demanding one refused it.

    Shown to fail: `openshell_launch._record_identity` records the sandbox
    NAME and leaves `container_id` empty on purpose — `sbx_01HN4Q8ZJ2WME5T` is
    not a 64-hex container identity — so the docker-shaped check refused every
    store-backed OpenShell launch at its own spawn registration, and the launch
    that swallowed the refusal then blocked in `proc.wait()` instead of
    returning a streaming handle (PR #2401 review).

    The docker-compatible rows are the control: a container name with no id
    still means the cidfile read-back never landed, and still refuses. So does
    a row whose runtime this build does not recognize.
    """
    from kew import dispatch_core
    from kew.run_store import RunRecord, SqliteRunStore
    from kew.runtime_names import OPENSHELL_RUNTIME

    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        row = RunRecord(
            "run-2320", 2320, "agent/2320", "pending", -1, datetime.now(UTC)
        )
        row.container_name = "kew-2320-openshell"
        row.container_runtime = OPENSHELL_RUNTIME
        store.save(row)
        assert store.get(row.run_id).container_id is None

        dispatch_core._check_spawn_permit(store, row.run_id)

        for runtime in ("docker", "podman", None, "containerd"):
            saved = store.get(row.run_id)
            saved.container_runtime = runtime
            store.save(saved, update_ownership=True)
            with pytest.raises(RuntimeError, match="container identity unavailable"):
                dispatch_core._check_spawn_permit(store, row.run_id)


_SONNET = "claude-sonnet-4-6"


def _seed_breaker_bucket(store: Any, *, count: int, cost: float) -> None:
    """Prior successes in the (claude-sonnet-4-6, medium, dispatch) bucket."""
    from kew.run_store import RunRecord

    for i in range(count):
        store.save(
            RunRecord(
                run_id=f"prior-{i}",
                issue_num=1,
                branch="agent/issue-1",
                status="success",
                exit_code=0,
                started_at=datetime(2026, 9, 1, tzinfo=UTC),
                model=_SONNET,
                complexity="medium",
                run_kind="dispatch",
                estimated_cost_usd=cost,
            )
        )


def _sonnet_usd(input_tokens: int) -> float:
    from kew.pricing import compute_cost_from_tokens

    return compute_cost_from_tokens(
        model=_SONNET, input_tokens=input_tokens, output_tokens=0
    )


def _claude_turn(input_tokens: int = 250_000) -> str:
    message = {
        "model": _SONNET,
        "content": [{"type": "text", "text": "working"}],
        "usage": {"input_tokens": input_tokens, "output_tokens": 0},
    }
    return json.dumps({"type": "assistant", "message": message}) + "\n"


def _codex_closing_line(input_tokens: int) -> str:
    """codex's ``turn.completed``: the run's last usage and its terminal event."""
    usage = {"input_tokens": input_tokens, "cached_input_tokens": 0}
    return json.dumps({"type": "turn.completed", "usage": usage}) + "\n"


def _opencode_closing_line(input_tokens: int, cost: float) -> str:
    """opencode's terminal ``step_finish``: usage, a native cost, and the end."""
    part = {
        "type": "step-finish",
        "reason": "stop",
        "cost": cost,
        "tokens": {"input": input_tokens, "output": 0, "cache": {}},
    }
    return json.dumps({"type": "step_finish", "sessionID": "s", "part": part}) + "\n"


def _streamed_launch(
    tmp_path: Path,
    lines: list[str],
    *,
    parser: str,
    launches: list[dict[str, Any]],
) -> Any:
    """A ``run_agent`` stand-in whose agent writes *lines*, read by kew's real
    host stream loop at the ceilings the caller armed."""

    def launch(**kw: Any) -> DispatchResult:
        launches.append(kw)
        return run_host_stream(
            tmp_path,
            list(lines),
            parser=parser,
            config=kw["config"],
            worktree_dir=kw["worktree_dir"],
            issue_num=kw["issue_num"],
            model=kw["model"],
            log_file_path=kw["log_file_path"],
            budget_check=kw["budget_check"],
            anomaly_threshold_usd=kw["anomaly_threshold_usd"],
            anomaly_compaction_allowance_usd=kw["anomaly_compaction_allowance_usd"],
        ).result

    return launch


def _tracked_stream_run(
    tmp_path: Path,
    store: Any,
    lines: list[str],
    *,
    parser: str = "claude-code",
    budget_check: Any = None,
) -> tuple[DispatchResult, list[dict[str, Any]]]:
    """One tracked dispatch whose agent writes *lines*, read by kew's real host
    stream loop with the ceilings ``run_tracked_agent`` armed. Three attempts
    are allowed, so a retry would show in the launch count."""
    from kew.dispatch_core import run_tracked_agent

    launches: list[dict[str, Any]] = []
    config = DispatchConfig(retry=RetryConfig(max_attempts=3))
    config.logging.dir = str(tmp_path / "logs")
    launch = _streamed_launch(tmp_path, lines, parser=parser, launches=launches)
    with patch(f"{_P}.run_agent", side_effect=launch):
        result = run_tracked_agent(
            allow_unrecorded=True,
            prompt="fix the bug",
            worktree_dir=tmp_path,
            config=config,
            issue_num=42,
            store=store,
            title="Runaway",
            branch="agent/issue-42",
            model=_SONNET,
            complexity="medium",
            budget_check=budget_check,
        )
    return result, launches


def test_breaker_event_records_threshold_median_samples_and_source(
    tmp_path: Path,
) -> None:
    """#1798: the ledger says why a cost was anomalous — the threshold, the
    median it came from, how many runs that median covers and which term won."""
    from kew.run_store import SqliteRunStore

    store = SqliteRunStore(str(tmp_path / "runs.db"))
    _seed_breaker_bucket(store, count=6, cost=2.25)
    # Ten $0.75 turns: the tenth passes the 3 x $2.25 = $6.75 ceiling.
    _, launches = _tracked_stream_run(tmp_path, store, [_claude_turn()] * 12)

    assert len(launches) == 1  # a breaker kill is never retried
    assert launches[0]["anomaly_threshold_usd"] == pytest.approx(6.75)
    [event] = store.list_audit_events(event="circuit_breaker")
    payload = event.payload
    assert payload["rule"] == "cost_anomaly"
    assert payload["threshold_usd"] == 6.75
    assert payload["median_usd"] == 2.25
    assert payload["sample_count"] == 6
    assert payload["threshold_source"] == "median_factor"
    assert payload["terminal_observed"] is False
    assert payload["observed_usd"] == pytest.approx(_sonnet_usd(2_500_000))
    assert store.list_by_issue(42)[-1].status == "killed"


def test_a_crossing_on_the_closing_line_flags_the_run_and_keeps_its_work(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#1798: codex's usage and its terminal event share one line. A run whose
    cost crossed the bar on that line had finished: it is flagged, not killed
    and not retried, and returns as a success, which is the result the
    publication path takes. The flag is on the row, the ledger, the dispatch
    output and the fallback Dispatch Report."""
    from kew.dispatch_core import _fallback_report_body
    from kew.run_store import SqliteRunStore

    store = SqliteRunStore(str(tmp_path / "runs.db"))
    _seed_breaker_bucket(store, count=6, cost=2.25)
    result, launches = _tracked_stream_run(
        tmp_path, store, [_codex_closing_line(3_000_000)], parser="codex"
    )

    assert len(launches) == 1
    assert result.exit_code == 0
    assert result.error_category == ""
    assert result.breaker_stopped is False
    row = store.list_by_issue(42)[-1]
    assert row.status == "success"
    assert row.error_category == "cost_anomaly_flagged"
    [event] = store.list_audit_events(event="circuit_breaker")
    assert event.payload["rule"] == "cost_anomaly"
    assert event.payload["terminal_observed"] is True
    err = capsys.readouterr().err
    assert "flagged" in err
    assert "$9.00" in err and "$6.75" in err
    report = _fallback_report_body("42", "medium", result)
    assert "$6.75 cost-anomaly threshold" in report


def test_a_flagged_run_takes_the_publication_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1798, end to end: a dispatch whose codex ``turn.completed`` crosses the
    bar is flagged, not killed, and its checkout goes to the host broker that
    pushes it and opens the PR. GitHub is faked behind ``gh``."""
    from support.gh_fakes import GhProcessBoundary

    from kew import subprocess_utils
    from kew.dispatch_core import execute_dispatch
    from kew.run_store import SqliteRunStore

    gh_calls: list[list[str]] = []

    def forge(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        gh_calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setitem(subprocess_utils.TOOL_PATHS, "gh", "gh")
    monkeypatch.setattr(
        subprocess_utils,
        "subprocess",
        GhProcessBoundary(forge, tool_paths=subprocess_utils.TOOL_PATHS),
    )
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    config = DispatchConfig(retry=RetryConfig(max_attempts=3))
    config.project.repo = "o/r"
    config.agent.model = _SONNET
    config.logging.dir = str(tmp_path / "logs")
    launches: list[dict[str, Any]] = []
    launch = _streamed_launch(
        tmp_path, [_codex_closing_line(3_000_000)], parser="codex", launches=launches
    )
    with (
        SqliteRunStore(str(tmp_path / "runs.db")) as store,
        patch(f"{_P}.setup_worktree", return_value=checkout),
        patch(f"{_P}.build_prompt", return_value="prompt"),
        patch(f"{_P}.generate_repo_map", return_value=""),
        patch(f"{_P}.run_agent", side_effect=launch),
        patch(f"{_P}._broker_agent_work", return_value=True) as broker,
    ):
        _seed_breaker_bucket(store, count=6, cost=2.25)
        outcome = execute_dispatch(
            allow_unrecorded=True, issue=_make_issue(), config=config, store=store
        )
        row = store.list_by_issue(42)[-1]

    assert launches[0]["anomaly_threshold_usd"] == pytest.approx(6.75)
    assert len(launches) == 1
    assert outcome.exit_code == 0
    assert broker.call_args.args[0] == checkout  # the work went to publication
    assert row.status == "success"
    assert row.error_category == "cost_anomaly_flagged"


def test_a_flagged_run_does_not_move_the_next_threshold(tmp_path: Path) -> None:
    """A flagged run finished as a success, but its cost was anomalous: it is
    kept out of the median the next run's ceiling is drawn from."""
    from kew.run_store import SqliteRunStore

    store = SqliteRunStore(str(tmp_path / "runs.db"))
    _seed_breaker_bucket(store, count=6, cost=2.25)
    _tracked_stream_run(
        tmp_path, store, [_codex_closing_line(30_000_000)], parser="codex"
    )

    assert store.median_cost_for_bucket(_SONNET, "medium", "dispatch") == (2.25, 6)


def test_a_closing_line_cannot_lower_a_flagged_run_below_its_trip_cost(
    tmp_path: Path,
) -> None:
    """The closing line that flagged the run also claims the whole run cost
    $0.0001. The row keeps the cost the run crossed the bar at."""
    from kew.run_store import SqliteRunStore

    store = SqliteRunStore(str(tmp_path / "runs.db"))
    _seed_breaker_bucket(store, count=6, cost=2.25)
    _tracked_stream_run(
        tmp_path,
        store,
        [_opencode_closing_line(3_000_000, cost=0.0001)],
        parser="opencode",
    )

    [event] = store.list_audit_events(event="circuit_breaker")
    assert event.payload["terminal_observed"] is True
    row = store.list_by_issue(42)[-1]
    assert row.estimated_cost_usd >= _sonnet_usd(3_000_000)


def test_a_budget_stop_after_a_flag_names_no_breaker(tmp_path: Path) -> None:
    """The first closing line crossed the anomaly bar and flagged the run; a
    second one spent past the budget cap and stopped it. The budget made the
    stop, so the ledger records no breaker trip for it."""
    from kew.run_store import SqliteRunStore

    store = SqliteRunStore(str(tmp_path / "runs.db"))
    _seed_breaker_bucket(store, count=6, cost=0.5)  # $1.50 ceiling
    result, _ = _tracked_stream_run(
        tmp_path,
        store,
        [_codex_closing_line(1_000_000)] * 2,  # $3.00 a line
        parser="codex",
        budget_check=lambda cost: cost > 4.5,
    )

    assert result.breaker_rule == ""
    assert store.list_audit_events(event="circuit_breaker") == []
    assert store.list_by_issue(42)[-1].status == "budget_stop"


def test_breaker_kill_names_the_rule_cost_and_bar() -> None:
    """#1798: a breaker kill used to print "Dispatch failed for #N (exit -15)".
    The dispatch line and the issue's failure comment name the rule, the cost
    and the bar."""
    from kew.dispatch_core import _failure_summary_line, _terminal_failure_reason

    line = _failure_summary_line(
        issue_num=7,
        error_category="circuit_breaker",
        exit_code=-15,
        total_attempts=1,
        cost_usd=7.66,
        breaker_rule="cost_anomaly",
        breaker_threshold_usd=6.75,
    )
    killed = _fail_result(issue_num=7, exit_code=-15, error_category="circuit_breaker")
    killed.breaker_rule = "cost_anomaly"
    killed.breaker_threshold_usd = 6.75
    killed.estimated_cost_usd = 7.66

    reason = "circuit breaker cost_anomaly ($7.66 over the $6.75 threshold)"
    assert line == f"Dispatch failed for #7 — {reason}, 1 attempt"
    assert _terminal_failure_reason(killed) == reason
