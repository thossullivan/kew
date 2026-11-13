"""Production-path wiring tests for the governed adversarial-review stage (#991).

The stage's LOGIC is proven by ``test_driver_review_stage.py`` (which calls
``drive_review_stage`` directly). These tests close the completeness gap that
adversarial review flagged on the first cut: they drive the REAL production
``advance()`` tick with only the reviewer's *container* faked, so they prove the
connective tissue is actually wired —

    advance() -> _run_tick -> drive_review_stage -> reviewer_launcher
      -> dispatch_reviewer (real git clone at head) -> sidecar -> parse
      -> record_review_round -> emit_review_verdict -> node transition

and prove the concrete production launcher (``sandbox_reviewer_launcher``) runs
its orchestration with the container faked at the shared ``_launch_container`` /
``_stream_sandbox_log`` seam (#1023). A test that stubbed
``drive_review_stage`` or the launcher itself would prove nothing; here the only
fakes are the container process and the dispatch sink, exactly the seams a
reviewer subprocess and the merge broker occupy in production — the git clone
that materializes the reviewer's checkout is real.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from dispatch_owner_fixture import admission_owner as admission_owner
from support import driver_fakes as _fx
from support.container_launch import ContainerLaunchCapture
from support.driver_fakes import (
    EPIC,
    REPO,
    FakeFactsReader,
    FakeSink,
    FakeSource,
    _node,
)
from support.finished_then_raises import FinishedThenRaisesLauncher
from support.gh_fakes import install_placeholder_forge
from support.review_stream import (
    FakeReviewerContainer,
    draining_stream,
    reviewer_stream,
    stream_verdict,
)
from support.unanswered_docker import UnansweredCreateDocker

from kew.audit_chain import verify_chain
from kew.config import DispatchConfig
from kew.graph.driver.facts import NodeFacts
from kew.graph.driver.result import TerminalSignal
from kew.graph.driver.review_launcher import sandbox_reviewer_launcher
from kew.graph.driver.tick import advance
from kew.ids import branch_slug
from kew.review_contract import CollectedSidecar
from kew.review_dispatch import ReviewerLaunchContext
from kew.run_store import ADVERSARIAL_REVIEW_RUN_KIND, SqliteRunStore
from kew.subprocess_utils import TOOL_PATHS, run

# ``store`` is a real SqliteRunStore fixture; ``_isolate_tool_paths`` (autouse)
# sandboxes the subprocess allowlist. We deliberately do NOT import
# ``_no_real_git`` — the review stage materializes the reviewer's checkout with
# a REAL ``git clone``, which these tests exercise end-to-end.
_isolate_tool_paths = _fx._isolate_tool_paths

NOW = datetime(2026, 7, 23, 12, 0, 0, tzinfo=UTC)
ITEM = "1"


@pytest.fixture(autouse=True)
def _placeholder_forge(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ``gh`` call reaches GitHub: the forge answers as ``o/r`` does (#3069)."""
    install_placeholder_forge(monkeypatch)


@pytest.fixture
def store(tmp_path: Path) -> Any:
    with SqliteRunStore(str(tmp_path / "run.db"), project_root=tmp_path) as bound:
        yield bound


@pytest.fixture(autouse=True)
def _reviewer_image_resolves(monkeypatch):
    """The reviewer now launches by the resolved immutable image ID (#1552);
    without a runtime these tests would refuse at resolution, so resolve to a
    stable fake ID."""
    monkeypatch.setattr(
        "kew.dispatch_sandbox._resolve_launch_image_id",
        lambda config, image, **_: "sha256:" + "f" * 64,
    )


@pytest.fixture(autouse=True)
def _register_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """Put the real git on the subprocess allowlist so ``dispatch_reviewer``
    can clone the reviewer's checkout (mirrors test_driver_review_stage.py)."""
    git = shutil.which("git")
    assert git is not None, "git required for production-wiring tests"
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


def _review_config(tmp_path: Path, *, blocking: bool) -> DispatchConfig:
    c = _fx._config()
    c.review.adversarial = True
    c.review.blocking = blocking
    c.review.max_rounds = 2
    c.logging.dir = str(tmp_path / "logs")
    return c


def _pr_open_facts(
    head: str, *, approved: bool = False, ci_clean: bool = False
) -> NodeFacts:
    """Facts for a node whose PR is open — the precondition that makes
    ``advance()`` derive ``ADVERSARIAL_REVIEW`` when review is enabled."""
    return NodeFacts(
        issue_num=int(ITEM),
        state="OPEN",
        state_reason=None,
        labels=frozenset(),
        pr_number=7,
        pr_url="https://github.com/x/y/pull/7",
        review_decision="APPROVED" if approved else None,
        merge_state_status="CLEAN" if ci_clean else None,
        head_oid=head,
        pr_pushed_at=None,
    )


def _pr_facts(head: str, *, approved: bool, ci_clean: bool) -> dict[str, Any]:
    d: dict[str, Any] = {"number": 7, "headRefOid": head}
    if approved:
        d["reviewDecision"] = "APPROVED"
        d["latestReviews"] = {
            "nodes": [
                {
                    "author": {"login": "human"},
                    "state": "APPROVED",
                    "submittedAt": "2026-07-23T13:00:00Z",
                }
            ]
        }
    if ci_clean:
        d["mergeStateStatus"] = "CLEAN"
    return d


_READY = {
    "verdict": "READY",
    "findings": [],
    "acceptance_evidence": [{"command": "pytest", "output_digest": "sha256:z"}],
}


def _spy_ready_launcher():
    calls: list[str] = []

    def _launch(ctx: ReviewerLaunchContext) -> CollectedSidecar:
        calls.append(ctx.item_id)
        return stream_verdict(
            ctx,
            {
                "verdict": "READY",
                "findings": [],
                "acceptance_evidence": [
                    {"command": "pytest -q", "output_digest": "sha256:x"}
                ],
            },
        )

    _launch.calls = calls  # type: ignore[attr-defined]
    return _launch


def _advance_one(
    *,
    config: DispatchConfig,
    store: SqliteRunStore,
    facts: FakeFactsReader,
    sink: FakeSink,
    launcher: Any,
    tmp_path: Path,
) -> Any:
    return advance(
        EPIC,
        config=config,
        store=store,
        deps=FakeSource(nodes=[_node(ITEM)], edges=[]),
        facts=facts,
        dispatch_sink=sink,
        max_parallel=3,
        now=NOW,
        lock_path=tmp_path / "epic.lock",
        approvers=["human"],
        repo=REPO,
        reviewer_launcher=launcher,
    )


# --------------------------------------------------------------------------- #
# A. Production-path E2E through advance() — the completeness-critic requirement #
# --------------------------------------------------------------------------- #
def test_advance_drives_the_full_review_loop_and_attests(
    tmp_path: Path, store: SqliteRunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The REAL advance() tick clones the reviewer checkout, invokes the
    launcher, ingests its sidecar, records the round, and attests the verdict —
    the chain stays verifiable. Only the launcher (container) is faked."""
    repo, head = _make_repo(tmp_path)
    monkeypatch.chdir(repo)  # cwd is the git root; advance() clones it at head
    config = _review_config(tmp_path, blocking=False)
    facts = FakeFactsReader(
        issue_facts_by_num={int(ITEM): _pr_open_facts(head)},
        pr_facts_by_num={7: _pr_facts(head, approved=False, ci_clean=False)},
    )
    launcher = _spy_ready_launcher()

    _advance_one(
        config=config,
        store=store,
        facts=facts,
        sink=FakeSink(),
        launcher=launcher,
        tmp_path=tmp_path,
    )

    assert launcher.calls == [ITEM]  # production tick really invoked it
    latest = store.latest_review_round(ITEM)
    assert latest is not None and latest.verdict == "READY"
    assert latest.head_sha == head  # verdict bound to the reviewed head
    assert verify_chain(store.read_chain_rows()).ok  # attested, chain still verifies


def test_advance_needs_changes_is_recorded_through_the_production_tick(
    tmp_path: Path, store: SqliteRunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, head = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    config = _review_config(tmp_path, blocking=False)
    facts = FakeFactsReader(
        issue_facts_by_num={int(ITEM): _pr_open_facts(head)},
        pr_facts_by_num={7: _pr_facts(head, approved=False, ci_clean=False)},
    )

    def _needs_changes(ctx: ReviewerLaunchContext) -> CollectedSidecar:
        return stream_verdict(
            ctx,
            {
                "verdict": "NEEDS_CHANGES",
                "findings": [
                    {
                        "severity": "high",
                        "location": "code.py:1",
                        "summary": "unchecked path",
                        "evidence": {
                            "command": "uv run pytest -q",
                            "output_digest": "sha256:y",
                        },
                    }
                ],
            },
        )

    _advance_one(
        config=config,
        store=store,
        facts=facts,
        sink=FakeSink(),
        launcher=_needs_changes,
        tmp_path=tmp_path,
    )

    latest = store.latest_review_round(ITEM)
    assert latest is not None and latest.verdict == "NEEDS_CHANGES"
    assert verify_chain(store.read_chain_rows()).ok


# --------------------------------------------------------------------------- #
# B. Blocking-mode happy path — the deadlock #991 exists to prevent            #
# --------------------------------------------------------------------------- #
def test_blocking_mode_dispatches_reviewer_and_produces_a_bound_verdict(
    tmp_path: Path, store: SqliteRunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In blocking mode the reviewer is still dispatched and a READY verdict is
    produced, bound to the current head — the precondition the merge-pass
    invariant (#988) consumes to let the node merge. Before #991 NOTHING
    produced a verdict in production, so a blocking node blocked forever; this
    proves the dispatch path is live under blocking, closing that deadlock. (The
    merge-pass invariant itself is exercised by test_driver_tick's merge tests.)
    """
    repo, head = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    config = _review_config(tmp_path, blocking=True)
    facts = FakeFactsReader(
        issue_facts_by_num={int(ITEM): _pr_open_facts(head)},
        pr_facts_by_num={7: _pr_facts(head, approved=False, ci_clean=False)},
    )
    launcher = _spy_ready_launcher()

    _advance_one(
        config=config,
        store=store,
        facts=facts,
        sink=FakeSink(),
        launcher=launcher,
        tmp_path=tmp_path,
    )

    assert launcher.calls == [ITEM]  # blocking mode did NOT deadlock at dispatch
    latest = store.latest_review_round(ITEM)
    assert latest is not None and latest.verdict == "READY"
    assert latest.head_sha == head  # bound to current head → invariant satisfiable
    assert verify_chain(store.read_chain_rows()).ok


# --------------------------------------------------------------------------- #
# B2. #1002 regressions — the conditions the first advisory drive found that   #
# the tests above structurally masked (cwd == git root; launcher always fake). #
# --------------------------------------------------------------------------- #
def test_advance_from_a_package_subdir_clones_the_reviewer_from_the_git_root(
    tmp_path: Path, store: SqliteRunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1002 Bug A: kew runs from the ``kew/`` package SUBDIR, but the reviewer
    worktree is cloned from ``repo_root`` — which must be the git toplevel, not
    ``Path.cwd()``. With cwd a subdir of the repo, the drive must still resolve
    the git root and clone successfully (before the fix, ``git clone <subdir>``
    exited 128 and crashed the tick). Other advance()-path tests chdir to the
    git ROOT, which is exactly what masked this."""
    repo, head = _make_repo(tmp_path)
    subdir = repo / "pkg"
    subdir.mkdir()
    monkeypatch.chdir(subdir)  # cwd is a package subdir, NOT the git root
    config = _review_config(tmp_path, blocking=False)
    facts = FakeFactsReader(
        issue_facts_by_num={int(ITEM): _pr_open_facts(head)},
        pr_facts_by_num={7: _pr_facts(head, approved=False, ci_clean=False)},
    )
    launcher = _spy_ready_launcher()

    _advance_one(
        config=config,
        store=store,
        facts=facts,
        sink=FakeSink(),
        launcher=launcher,
        tmp_path=tmp_path,
    )

    # The reviewer clone (from the git root, resolved from the subdir cwd) worked
    # → the launcher ran and a verdict was recorded. No tick crash.
    assert launcher.calls == [ITEM]
    latest = store.latest_review_round(ITEM)
    assert latest is not None and latest.verdict == "READY"


def _make_monorepo(tmp_path: Path) -> tuple[Path, str, str]:
    """A one-commit repo whose CODE lives in a SUBDIR — the mini-monorepo /
    nested-package shape kew's target market actually has. Returns
    ``(repo_root, head_sha, code_subdir)``; the git root is still the mount
    root, but the code (and thus any agent's natural cwd) is ``packages/api``.
    """
    repo = tmp_path / "monorepo"
    repo.mkdir()
    run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    run(["git", "config", "user.email", "t@t.t"], cwd=repo, check=True)
    run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    pkg = repo / "packages" / "api"
    pkg.mkdir(parents=True)
    (pkg / "code.py").write_text("x = 1\n")
    run(["git", "add", "-A"], cwd=repo, check=True)
    run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)
    head = run(["git", "rev-parse", "HEAD"], cwd=repo, check=True).stdout.strip()
    return repo, head, "packages/api"


def _spy_subdir_planting_launcher(code_subdir: str):
    """A reviewer run in which the code under review planted a READY verdict
    file, with the recorded head SHA, in the CODE SUBDIR of a monorepo — the
    shape #1026's subdir recovery used to collect — while the reviewer itself
    ended without a verdict block. Routes through the REAL stream collection."""
    calls: list[str] = []

    def _launch(ctx: ReviewerLaunchContext) -> CollectedSidecar:
        calls.append(ctx.item_id)
        code_dir = ctx.worktree / code_subdir
        code_dir.mkdir(parents=True, exist_ok=True)
        (code_dir / ".kew-review-verdict.json").write_text(
            json.dumps({**_READY, "head_sha": ctx.head_sha})
        )
        return stream_verdict(ctx, None)

    _launch.calls = calls  # type: ignore[attr-defined]
    return _launch


def test_advance_never_collects_a_verdict_file_planted_in_a_monorepo_subdir(
    tmp_path: Path, store: SqliteRunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2021 guard at the production ``advance()`` tick: on a target repo whose
    CODE lives in a SUBDIR (a mini-monorepo), a READY verdict file the reviewed
    code planted in that subdir is never collected. The reviewer ended without
    a verdict block, so the round is the crash path — HELD in blocking mode —
    never READY. Before #2021 the subdir recovery attested it as READY."""
    repo, head, code_subdir = _make_monorepo(tmp_path)
    monkeypatch.chdir(repo / code_subdir)  # cwd is the code subdir, not the root
    config = _review_config(tmp_path, blocking=True)  # blocking → false-HELD if lost
    facts = FakeFactsReader(
        issue_facts_by_num={int(ITEM): _pr_open_facts(head)},
        pr_facts_by_num={7: _pr_facts(head, approved=False, ci_clean=False)},
    )
    launcher = _spy_subdir_planting_launcher(code_subdir)

    _advance_one(
        config=config,
        store=store,
        facts=facts,
        sink=FakeSink(),
        launcher=launcher,
        tmp_path=tmp_path,
    )

    assert launcher.calls == [ITEM]  # production tick really invoked it
    latest = store.latest_review_round(ITEM)
    assert latest is None or latest.verdict != "READY"
    assert any("review_unparseable" in h.reason for h in store.list_epic_holds(EPIC))
    assert verify_chain(store.read_chain_rows()).ok  # attested, chain still verifies


def _refused_launcher():
    """The sandbox refuses the reviewer BEFORE launch — the #2147 incident: the
    pinned image was momentarily absent from the local container runtime, so no
    container ran and no review happened ($0 spent)."""
    from kew.review_contract import infrastructure_failure_sidecar

    calls: list[str] = []

    def _launch(ctx: ReviewerLaunchContext) -> CollectedSidecar:
        calls.append(ctx.item_id)
        return infrastructure_failure_sidecar(
            ctx.log_dir,
            ctx.run_id,
            failure_layer="image",
            error_category="sandbox_refused",
            reason=(
                "image kew-agent:docker-d148816b388f is not present in the "
                "local container runtime"
            ),
        )

    _launch.calls = calls  # type: ignore[attr-defined]
    return _launch


def test_pre_launch_refusal_keeps_the_tick_progressing_and_names_the_refusal(
    tmp_path: Path, store: SqliteRunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2147 at the production tick: a review the sandbox refused before launch
    leaves the node ready for re-review and the tick names the refusal. The
    incident recorded it as ``review_unparseable``, HELD the node, and reported
    ``AWAITING_MERGES — 0 PRs awaiting a human merge`` while four downstream
    nodes waited for an operator to run `kew epic retry`."""
    repo, head = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    config = _review_config(tmp_path, blocking=True)  # the incident's mode
    facts = FakeFactsReader(
        issue_facts_by_num={int(ITEM): _pr_open_facts(head)},
        pr_facts_by_num={7: _pr_facts(head, approved=False, ci_clean=False)},
    )
    launcher = _refused_launcher()

    result = _advance_one(
        config=config,
        store=store,
        facts=facts,
        sink=FakeSink(),
        launcher=launcher,
        tmp_path=tmp_path,
    )

    assert launcher.calls == [ITEM]  # the production tick really invoked it
    # Ready for re-review: no hold, no round, no guessed verdict.
    assert store.list_epic_holds(EPIC) == []
    assert store.latest_review_round(ITEM) is None
    # The tick names the refusal on the node's own waiting line, and reads as
    # progressing — the next tick re-reviews it.
    waiting = {w.node_id: w.reason for w in result.waiting}
    assert "review_sandbox_refused" in waiting.get(ITEM, "")
    assert "unparseable" not in waiting.get(ITEM, "")
    assert result.signal is TerminalSignal.PROGRESSING
    # The refusal is durable on the run row, at $0 — the retry bound's evidence.
    refusals = [
        r
        for r in store.list_by_item(ITEM)
        if r.run_kind == ADVERSARIAL_REVIEW_RUN_KIND
        and r.error_category == "sandbox_refused"
    ]
    assert len(refusals) == 1
    assert refusals[0].estimated_cost_usd == 0.0
    assert verify_chain(store.read_chain_rows()).ok


def test_a_refused_review_is_re_reviewed_on_the_next_tick(
    tmp_path: Path, store: SqliteRunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2147's proof: the node the sandbox refused is still due a reviewer, so
    the very next tick reviews it — the image was present again a minute later.
    No operator `kew epic retry` in between."""
    repo, head = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    config = _review_config(tmp_path, blocking=True)
    facts = FakeFactsReader(
        issue_facts_by_num={int(ITEM): _pr_open_facts(head)},
        pr_facts_by_num={7: _pr_facts(head, approved=False, ci_clean=False)},
    )
    refuse = _refused_launcher()
    ready = _spy_ready_launcher()

    _advance_one(
        config=config,
        store=store,
        facts=facts,
        sink=FakeSink(),
        launcher=refuse,
        tmp_path=tmp_path,
    )
    _advance_one(
        config=config,
        store=store,
        facts=facts,
        sink=FakeSink(),
        launcher=ready,
        tmp_path=tmp_path,
    )

    assert refuse.calls == [ITEM] and ready.calls == [ITEM]
    latest = store.latest_review_round(ITEM)
    assert latest is not None and latest.verdict == "READY"
    assert latest.round == 1  # the refused round never consumed the allowance
    assert store.list_epic_holds(EPIC) == []
    assert verify_chain(store.read_chain_rows()).ok


def test_reviewer_infra_failure_degrades_and_never_crashes_the_tick(
    tmp_path: Path, store: SqliteRunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1002 Bug B: a reviewer INFRA failure (a launcher/materialize raise) must
    fail closed — DEGRADED attested (advisory) — and NEVER propagate out of the
    tick. Before the fix, a raise crashed the whole epic drive (exit 1)."""
    repo, head = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    config = _review_config(tmp_path, blocking=False)  # advisory → DEGRADED
    facts = FakeFactsReader(
        issue_facts_by_num={int(ITEM): _pr_open_facts(head)},
        pr_facts_by_num={7: _pr_facts(head, approved=False, ci_clean=False)},
    )

    def _exploding_launcher(ctx: ReviewerLaunchContext) -> CollectedSidecar:
        raise RuntimeError("simulated reviewer infra failure (clone/container)")

    # Must NOT raise — the tick survives an infra failure.
    _advance_one(
        config=config,
        store=store,
        facts=facts,
        sink=FakeSink(),
        launcher=_exploding_launcher,
        tmp_path=tmp_path,
    )

    # Advisory: the crash path attests DEGRADED and proceeds (never a READY).
    latest = store.latest_review_round(ITEM)
    assert latest is not None and latest.verdict == "DEGRADED"
    verdict = next(
        event
        for event in store.list_audit_events(item_id=ITEM)
        if event.event == "adversarial_review_verdict"
    )
    assert verdict.payload is not None
    assert verdict.payload["failure_layer"] == "launch"
    assert verdict.payload["error_category"] == "launch_failed"
    run = store.get(verdict.run_id or "")
    assert run is not None
    assert run.error_category == "launch_failed"
    assert run.status == "failure"
    assert verify_chain(store.read_chain_rows()).ok
    # The review reservation was released (no leak on the failure path).
    from kew.pseudo_epics import REVIEW_EPIC_REF

    assert [r for r in store.list_epic_reservations(REVIEW_EPIC_REF)] == []


def test_blocking_infra_failure_attests_its_launch_evidence_while_holding(
    tmp_path: Path, store: SqliteRunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1393: a blocking-mode launch failure HELDs the node — and still attests
    the crash. Before this, the blocking branch held and returned before any
    emit, so the failing layer + error category existed only on the run row and
    a dark lane was invisible to the verdict stream (and to `kew doctor`)."""
    repo, head = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    config = _review_config(tmp_path, blocking=True)  # blocking → HELD, not DEGRADED
    facts = FakeFactsReader(
        issue_facts_by_num={int(ITEM): _pr_open_facts(head)},
        pr_facts_by_num={7: _pr_facts(head, approved=False, ci_clean=False)},
    )

    def _exploding_launcher(ctx: ReviewerLaunchContext) -> CollectedSidecar:
        raise RuntimeError("simulated reviewer infra failure (clone/container)")

    _advance_one(
        config=config,
        store=store,
        facts=facts,
        sink=FakeSink(),
        launcher=_exploding_launcher,
        tmp_path=tmp_path,
    )

    # Held needs-human, exactly as before — the fix adds evidence, not a merge.
    # The reason is the launch failure's own (#2043): the reviewer never started,
    # so there is no verdict — and no verdict channel — for a human to look at.
    (hold,) = store.list_epic_holds(EPIC)
    assert hold.reason.startswith("review_launch_failed")
    assert "unparseable" not in hold.reason
    verdict = next(
        event
        for event in store.list_audit_events(item_id=ITEM)
        if event.event == "adversarial_review_verdict"
    )
    assert verdict.payload is not None
    assert verdict.payload["verdict"] == "UNPARSEABLE"
    assert verdict.payload["failure_layer"] == "launch"
    assert verdict.payload["error_category"] == "launch_failed"
    assert verdict.payload["policy"]["blocking"] is True
    # The verdict points at the terminal run row the failed launch left behind.
    run_row = store.get(verdict.run_id or "")
    assert run_row is not None
    assert run_row.status == "failure"
    assert run_row.error_category == "launch_failed"
    assert verify_chain(store.read_chain_rows()).ok


def test_a_reviewer_that_ran_is_not_published_as_one_that_never_started(
    tmp_path: Path, store: SqliteRunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2043 re-review: the review STAGE's own infra-failure handler is the second
    producer of ``launch_failed``, and it hard-coded the category for ANY raise
    escaping ``dispatch_reviewer``. The launcher's post-run bookkeeping sits
    outside its except guard, so a reviewer that started, streamed and exited 0
    landed there and was held — and published to the PR — as one that never
    started, with its finished run row rewritten to a synthetic $0 failure.

    The stage now reads the run row that raise left behind: a row exists, so the
    launch cannot be disproven, and the round earns the crash path's honest
    ``review_unparseable`` under a ``host`` layer."""
    repo, head = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    rec = _record_review_status(monkeypatch)
    config = _review_config(tmp_path, blocking=True)
    facts = FakeFactsReader(
        issue_facts_by_num={int(ITEM): _pr_open_facts(head)},
        pr_facts_by_num={7: _pr_facts(head, approved=False, ci_clean=False)},
    )

    # Fail-closed as ever: the tick survives the raise.
    _advance_one(
        config=config,
        store=store,
        facts=facts,
        sink=FakeSink(),
        launcher=FinishedThenRaisesLauncher(store, config),
        tmp_path=tmp_path,
    )

    # The node is still held — no verdict was collected — but under the reason
    # that is true, and the PR is never told the reviewer never started.
    (hold,) = store.list_epic_holds(EPIC)
    assert hold.reason.startswith("review_unparseable")
    assert "launch_failed" not in hold.reason
    assert rec["failure"] == [(head, "unparseable verdict")]

    verdict = next(
        event
        for event in store.list_audit_events(item_id=ITEM)
        if event.event == "adversarial_review_verdict"
    )
    assert verdict.payload is not None
    assert verdict.payload["error_category"] == "review_host_failed"
    assert verdict.payload["failure_layer"] == "host"

    # ...and the reviewer's own spend still bounds the review pool: the stage
    # stamps the row it found, it does not replace it with a synthetic $0 one.
    row = store.get(verdict.run_id or "")
    assert row is not None
    assert row.estimated_cost_usd == 1.25
    assert row.total_tokens == 4242
    assert row.error_category == "review_host_failed"
    assert row.status == "failure"
    assert verify_chain(store.read_chain_rows()).ok


def test_a_reviewer_create_the_runtime_never_answers_holds_as_never_started(
    tmp_path: Path, store: SqliteRunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#3186: every ``create`` of the reviewer's container timed out — the host
    was too slow and no reviewer ever ran. The node holds as a pre-launch
    failure naming the runtime timeout: no review round, and never
    ``review_unparseable``. The ``docker`` is a real executable that never
    answers ``create``; kew's own small ceiling kills it. It is the only
    ``docker`` the process can find, so the host's real runtime is never
    reached, and the wait between attempts is a recorded no-op."""
    scene = _unanswered_create_scene(tmp_path, monkeypatch)
    config = _review_config(tmp_path, blocking=True)
    _earlier_timed_out_launches(store)

    _advance_one(
        config=config,
        store=store,
        facts=_review_facts(scene.head),
        sink=FakeSink(),
        launcher=sandbox_reviewer_launcher(config, store),
        tmp_path=tmp_path,
    )

    assert scene.docker.creates() == 3
    assert scene.slept == [2.0, 4.0]
    _assert_held_as_runtime_timeout(store)


def test_create_retries_stop_at_the_container_attempts_ceiling(
    tmp_path: Path, store: SqliteRunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#3186: the create retries stop at the container attempt's ceiling
    (``WALL_CLOCK_LIMIT_SECONDS`` from its launch). The clock is pinned and
    moves only by the backoff waits; a backoff of half the ceiling leaves room
    for one retry but not a second, although the attempt count would allow it.
    The attempt ends inside its ceiling, held as never started."""
    from kew.sandbox import WALL_CLOCK_LIMIT_SECONDS

    scene = _unanswered_create_scene(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "kew.dispatch_sandbox.CREATE_TIMEOUT_BACKOFF_SECONDS",
        WALL_CLOCK_LIMIT_SECONDS / 2,
    )
    config = _review_config(tmp_path, blocking=True)
    _earlier_timed_out_launches(store)

    _advance_one(
        config=config,
        store=store,
        facts=_review_facts(scene.head),
        sink=FakeSink(),
        launcher=sandbox_reviewer_launcher(config, store),
        tmp_path=tmp_path,
    )

    assert scene.docker.creates() == 2
    assert scene.slept == [WALL_CLOCK_LIMIT_SECONDS / 2]
    # The run started at 0 on the pinned clock; its last create ended by its
    # own timeout after the one wait, inside the bound.
    assert scene.now + _CREATE_TIMEOUT <= WALL_CLOCK_LIMIT_SECONDS
    _assert_held_as_runtime_timeout(store)


#: The create ceiling the unanswered-create scenes run under: kew's own
#: timeout, not a sleep, decides how long each attempt takes.
_CREATE_TIMEOUT = 0.5


class _PinnedLaunchClock:
    """The launch clock and wait, pinned: time moves only when kew waits."""

    def __init__(self, docker: UnansweredCreateDocker, head: str) -> None:
        self.docker, self.head = docker, head
        self.now = 0.0
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.now

    def wait(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def _unanswered_create_scene(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> _PinnedLaunchClock:
    from kew import sandbox

    repo, head = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setattr(sandbox, "RUNTIME_CONTROL_TIMEOUT_SECONDS", _CREATE_TIMEOUT)
    docker = UnansweredCreateDocker(tmp_path / "runtime")
    docker.install(monkeypatch)
    pinned = _PinnedLaunchClock(docker, head)
    monkeypatch.setattr("kew.dispatch_sandbox.launch_clock", pinned.clock)
    monkeypatch.setattr("kew.dispatch_sandbox.launch_wait", pinned.wait)
    return pinned


def _review_facts(head: str) -> FakeFactsReader:
    return FakeFactsReader(
        issue_facts_by_num={int(ITEM): _pr_open_facts(head)},
        pr_facts_by_num={7: _pr_facts(head, approved=False, ci_clean=False)},
    )


def _earlier_timed_out_launches(store: SqliteRunStore) -> None:
    """Two earlier reviewer launches of the node, an hour back, that a runtime
    timeout ended: the scene's launch is the third in a row, the one the
    stage holds on (#3246), and the back-off after the second is long over."""
    from kew.run_store import RunRecord

    for minutes in (70, 60):
        at = NOW - timedelta(minutes=minutes)
        store.save(
            RunRecord(
                run_id=f"review-timed-out-{minutes}",
                issue_num=int(ITEM),
                item_id=ITEM,
                branch=f"kew/{ITEM}",
                status="failure",
                exit_code=-1,
                started_at=at,
                finished_at=at,
                error_category="runtime_timeout",
                run_kind=ADVERSARIAL_REVIEW_RUN_KIND,
            )
        )


def _assert_held_as_runtime_timeout(store: SqliteRunStore) -> None:
    (hold,) = store.list_epic_holds(EPIC)
    assert hold.reason.startswith("review_launch_failed")
    assert "the reviewer never started" in hold.reason
    assert "runtime_timeout" in hold.reason
    assert "unparseable" not in hold.reason
    assert store.latest_review_round(ITEM) is None
    verdict = next(
        event
        for event in store.list_audit_events(item_id=ITEM)
        if event.event == "adversarial_review_verdict"
    )
    assert verdict.payload is not None
    assert verdict.payload["error_category"] == "runtime_timeout"
    assert verdict.payload["failure_layer"] == "container"
    assert verify_chain(store.read_chain_rows()).ok


def test_a_dispatch_that_raised_before_any_run_row_still_reports_launch_failed(
    tmp_path: Path, store: SqliteRunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of the stage's pair: a raise with NO reviewer row behind it
    landed before the launcher admitted anything — before any container — so the
    #2043 hold that says the reviewer never started is exactly right."""
    repo, head = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    rec = _record_review_status(monkeypatch)
    config = _review_config(tmp_path, blocking=True)
    facts = FakeFactsReader(
        issue_facts_by_num={int(ITEM): _pr_open_facts(head)},
        pr_facts_by_num={7: _pr_facts(head, approved=False, ci_clean=False)},
    )

    def _explodes_before_admission(ctx: ReviewerLaunchContext) -> CollectedSidecar:
        raise RuntimeError("the reviewer checkout could not be materialized")

    _advance_one(
        config=config,
        store=store,
        facts=facts,
        sink=FakeSink(),
        launcher=_explodes_before_admission,
        tmp_path=tmp_path,
    )

    (hold,) = store.list_epic_holds(EPIC)
    assert hold.reason.startswith("review_launch_failed")
    assert rec["failure"] == [(head, "the reviewer never started")]
    verdict = next(
        event
        for event in store.list_audit_events(item_id=ITEM)
        if event.event == "adversarial_review_verdict"
    )
    assert verdict.payload is not None
    assert verdict.payload["error_category"] == "launch_failed"
    assert verdict.payload["failure_layer"] == "launch"
    assert verify_chain(store.read_chain_rows()).ok


# --------------------------------------------------------------------------- #
# C. The concrete production launcher, container faked                         #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("runner", ["", "claude-code"])
def test_sandbox_reviewer_launcher_runs_orchestration_with_faked_container(
    tmp_path: Path, store: SqliteRunStore, runner: str
) -> None:
    """sandbox_reviewer_launcher writes the pending review-kind run row, runs
    the shared container primitive, finalizes, and collects the host-stored
    verdict its result event carried into a parsed READY verdict — all without
    a real container runtime.

    The container is faked at the shared seam (``dispatch_core._launch_container``
    / ``_stream_sandbox_log``) both callers now route through (#1023). Both
    ``review.runner = ""`` and the explicit ``"claude-code"`` take this path
    unchanged (#1069)."""
    config = _fx._config()
    config.review.adversarial = True
    config.review.runner = runner
    config.logging.dir = str(tmp_path / "logs")
    Path(config.logging.dir).mkdir(parents=True, exist_ok=True)

    worktree = tmp_path / "wt"
    worktree.mkdir()
    seen: dict[str, Any] = {}
    container = FakeReviewerContainer(reviewer_stream(_READY, "a" * 40))

    def _fake_stream(handle, log_file, st, run_id, *, model, overrides, runner) -> int:
        pending = [r for r in st.list_by_item(ITEM) if r.run_id == run_id]
        seen["pending_kinds"] = [r.run_kind for r in pending]
        # #1059: the pending reviewer row must carry the container liveness
        # signal so the driver's orphan reconciler can reap a hard-killed
        # reviewer (breaker process-group kill / crash) that never runs its
        # in-process ``finally`` — without it the row pins a fleet slot forever.
        seen["pending_container"] = [r.container_name for r in pending]
        return draining_stream()(
            handle,
            log_file,
            st,
            run_id,
            model=model,
            overrides=overrides,
            runner=runner,
        )

    launcher = sandbox_reviewer_launcher(config, store)
    ctx = ReviewerLaunchContext(
        item_id=ITEM,
        run_id="rev-run-1",
        prompt="review this",
        prompt_artifact_sha="sha256:prompt",
        head_sha="a" * 40,
        worktree=worktree,
        log_dir=Path(config.logging.dir).resolve(),
    )

    import kew.dispatch_core as _dc
    import kew.graph.driver.review_launcher as _rl

    monkeypatch_runtime(_rl, "docker")
    try:
        with (
            patch.object(_dc, "_launch_container", return_value=container),
            patch.object(_dc, "_stream_sandbox_log", side_effect=_fake_stream),
        ):
            collected = launcher(ctx)
    finally:
        _rl.detect_runtime_cached = _rl_orig  # type: ignore[name-defined]

    assert seen["pending_kinds"] == [ADVERSARIAL_REVIEW_RUN_KIND]
    # The reviewer container is named ``kew-<slug>`` (sandbox.build_run_argv);
    # the pending row records exactly that so ``default_run_liveness`` can prove
    # a dead reviewer's container gone and reap the orphan (#1059).
    assert seen["pending_container"] == [f"kew-{branch_slug(ITEM)}"]
    finalized = [r for r in store.list_by_item(ITEM) if r.run_id == "rev-run-1"]
    assert finalized and finalized[0].status != "pending"
    assert collected.parsed.verdict == "READY"


def test_the_production_launcher_excuses_findings_for_declared_tests(
    tmp_path: Path, store: SqliteRunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tests the prompt listed as declared reach the parse (#3516): a
    NEEDS_CHANGES whose only finding asks for a declared test comes back READY."""
    import kew.dispatch_core as _dc
    import kew.graph.driver.review_launcher as _rl

    node = "tests/test_widget.py::test_widget"
    verdict = {
        **_READY,
        "verdict": "NEEDS_CHANGES",
        "findings": [
            {
                "severity": "medium",
                "location": "tests/test_widget.py:1",
                "summary": "no Behavior changes line names this edited test",
                "evidence": {"command": "pytest", "output_digest": "1 failed"},
                "kind": "declaration",
                "node_ids": [node],
            }
        ],
    }
    config = _fx._config()
    config.review.adversarial = True
    config.logging.dir = str(tmp_path / "logs")
    Path(config.logging.dir).mkdir(parents=True, exist_ok=True)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    ctx = ReviewerLaunchContext(
        item_id=ITEM,
        run_id="rev-declared",
        prompt="review this",
        prompt_artifact_sha="sha256:prompt",
        head_sha="a" * 40,
        worktree=worktree,
        log_dir=Path(config.logging.dir).resolve(),
        declared_tests=(node,),
    )
    monkeypatch.setattr(_rl, "detect_runtime_cached", lambda _cfg: "docker")
    with (
        patch.object(
            _dc,
            "_launch_container",
            return_value=FakeReviewerContainer(reviewer_stream(verdict, "a" * 40)),
        ),
        patch.object(_dc, "_stream_sandbox_log", side_effect=draining_stream()),
    ):
        collected = sandbox_reviewer_launcher(config, store)(ctx)

    assert collected.parsed.verdict == "READY"
    assert collected.parsed.excused_tests == (node,)


def test_reviewer_oom_records_cap_sidecar_and_breaker_audit(
    admission_owner, tmp_path: Path, store: SqliteRunStore
) -> None:
    from dataclasses import replace

    from kew.sandbox import ContainerHandle, ResourceCapCode
    from kew.transcript_metrics import read_derived_evidence

    config = _fx._config()
    config.review.adversarial = True
    config.logging.dir = str(tmp_path / "logs")
    worktree = tmp_path / "wt"
    worktree.mkdir()
    cidfile = tmp_path / "review.cid"
    cidfile.write_text("a" * 64, encoding="utf-8")
    handle = ContainerHandle(MagicMock(), "kew-1", cidfile=cidfile)
    ctx = ReviewerLaunchContext(
        item_id=ITEM,
        run_id="rev-oom",
        prompt="review this",
        prompt_artifact_sha="sha256:prompt",
        head_sha="b" * 40,
        worktree=worktree,
        log_dir=Path(config.logging.dir).resolve(),
    )

    def daemon(argv, **kwargs):
        if argv[1] == "network":
            # The reviewer's own posture record reads the attachment network
            # the launch would use, exactly as a dispatch's does (epic node
            # N12). Not an assertion target here; the posture tests own it.
            return MagicMock(returncode=1, stdout="")
        if argv[1] == "inspect":
            # The real handle observes startup before probing this OOM exit.
            assert argv == [
                "docker",
                "inspect",
                "--format",
                "{{json .State}}",
                "a" * 64,
            ]
            return MagicMock(
                returncode=0,
                stdout=json.dumps(
                    {"Running": False, "StartedAt": "2026-09-15T01:00:00Z"}
                ),
            )
        assert argv[:2] == ["docker", "events"]
        assert f"container={'a' * 64}" in argv
        return MagicMock(returncode=0, stdout="container oom current-id\n")

    import kew.dispatch_core as _dc
    import kew.graph.driver.review_launcher as _rl

    monkeypatch_runtime(_rl, "docker")
    try:
        with (
            patch.object(_dc, "_launch_container", return_value=handle),
            patch.object(_dc, "_stream_sandbox_log", return_value=137),
            patch(
                "kew.process_groups.process_identity",
                return_value=replace(admission_owner, birth="review-owner-start"),
            ),
            patch("kew.subprocess_utils.run", side_effect=daemon),
        ):
            sandbox_reviewer_launcher(config, store)(ctx)
    finally:
        _rl.detect_runtime_cached = _rl_orig  # type: ignore[name-defined]

    [event] = store.list_audit_events(event="circuit_breaker")
    assert event.run_id == ctx.run_id
    assert event.payload["rule"] == "resource_cap"
    assert event.payload["resource_cap_code"] == ResourceCapCode.OOM.value
    assert "OOM-killed" in event.payload["reason"]
    assert store.get(ctx.run_id).owner_start_time == "review-owner-start"
    evidence = read_derived_evidence(Path(config.logging.dir) / "review-rev-oom.log")
    assert evidence is not None
    assert evidence.oom_probe_status == "seen"
    assert evidence.resource_cap_observations[-1][0] == ResourceCapCode.OOM.value


def test_reviewer_prompt_names_the_stream_verdict_channel(
    tmp_path: Path, store: SqliteRunStore
) -> None:
    """#2021: the host-appended prompt tells the reviewer to end with one
    delimited verdict block naming the recorded head SHA, and names no verdict
    file — a file in the checkout is writable by the code under review.
    """
    from kew.review_stream import VERDICT_BEGIN, VERDICT_END
    from kew.sandbox import PROMPT_FILENAME

    config = _fx._config()
    config.review.adversarial = True
    config.logging.dir = str(tmp_path / "logs")
    Path(config.logging.dir).mkdir(parents=True, exist_ok=True)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    seen: dict[str, str] = {}

    def _fake_stream(handle, log_file, st, run_id, *, model, overrides, runner) -> int:
        # The launcher writes the prompt file before launching and unlinks it
        # after; capture it here, mid-run, while it still exists.
        seen["prompt"] = (worktree / PROMPT_FILENAME).read_text(encoding="utf-8")
        return 0

    ctx = ReviewerLaunchContext(
        item_id=ITEM,
        run_id="rev-prompt",
        prompt="review this",
        prompt_artifact_sha="sha256:prompt",
        head_sha="a" * 40,
        worktree=worktree,
        log_dir=Path(config.logging.dir).resolve(),
    )

    import kew.dispatch_core as _dc
    import kew.graph.driver.review_launcher as _rl

    monkeypatch_runtime(_rl, "docker")
    try:
        with (
            patch.object(_dc, "_launch_container", return_value=object()),
            patch.object(_dc, "_stream_sandbox_log", side_effect=_fake_stream),
        ):
            sandbox_reviewer_launcher(config, store)(ctx)
    finally:
        _rl.detect_runtime_cached = _rl_orig  # type: ignore[name-defined]

    assert seen["prompt"].count(VERDICT_BEGIN) == 1
    assert seen["prompt"].count(VERDICT_END) == 1
    assert f'"head_sha" to exactly {"a" * 40}' in seen["prompt"]
    assert ".kew-review-verdict.json" not in seen["prompt"]
    assert "Stage scratch files under /work" in seen["prompt"]


def test_sandbox_reviewer_launcher_fail_closed_when_no_runtime(
    tmp_path: Path, store: SqliteRunStore
) -> None:
    """No sandbox runtime → the reviewer cannot run → a crash-path (no-verdict)
    sidecar, never a raise that would crash the driver tick."""
    config = _fx._config()
    config.review.adversarial = True
    config.logging.dir = str(tmp_path / "logs")
    worktree = tmp_path / "wt"
    worktree.mkdir()
    ctx = ReviewerLaunchContext(
        item_id=ITEM,
        run_id="rev-2",
        prompt="p",
        prompt_artifact_sha="sha256:p",
        head_sha="a" * 40,
        worktree=worktree,
        log_dir=tmp_path,
    )

    def _boom(*_a, **_k) -> int:  # pragma: no cover - must never run
        raise AssertionError("container must not launch when runtime is absent")

    import kew.dispatch_core as _dc
    import kew.graph.driver.review_launcher as _rl

    monkeypatch_runtime(_rl, None)
    try:
        with patch.object(_dc, "_launch_container", side_effect=_boom):
            collected = sandbox_reviewer_launcher(config, store)(ctx)
    finally:
        _rl.detect_runtime_cached = _rl_orig  # type: ignore[name-defined]

    assert collected.parsed.unparseable  # no verdict written == crash path
    row = store.get("rev-2")
    assert row is not None
    assert row.status == "failure"
    assert row.error_category == "launch_failed"
    assert row.run_kind == ADVERSARIAL_REVIEW_RUN_KIND


class _StartedReviewerContainer:
    """A container handle the host OBSERVES starting its workload.

    ``observe_workload_start`` is the only evidence ``capture_container_launch``
    accepts as proof the reviewer ran, so a double that omits it (the default
    ``FakeReviewerContainer``) cannot exercise the started-then-died lane.
    """

    def observe_workload_start(self) -> bool:
        return True

    def __iter__(self) -> Any:  # a stream that dies before any line
        return iter(())

    def wait(self, *_a: Any, **_k: Any) -> int:
        return -1

    def kill(self, *_a: Any, **_k: Any) -> Any:
        return None


def test_a_reviewer_that_died_mid_stream_is_not_reported_as_never_started(
    tmp_path: Path, store: SqliteRunStore
) -> None:
    """#2043 re-review: ``launch_failed`` is the category the review stage holds
    under "the reviewer never started" and publishes that sentence to the PR, so
    the launcher may claim it only when the launch evidence PROVES the container
    never ran.

    A reviewer whose workload start WAS observed and whose stream then died is
    the counter-case: it keeps its own stream-layer category and lands on the
    crash path's ``review_unparseable``, which was accurate all along."""
    from kew.graph.driver.review_stage import _crash_hold_reason

    config = _fx._config()
    config.review.adversarial = True
    config.logging.dir = str(tmp_path / "logs")
    worktree = tmp_path / "wt"
    worktree.mkdir()
    ctx = ReviewerLaunchContext(
        item_id=ITEM,
        run_id="rev-midstream",
        prompt="p",
        prompt_artifact_sha="sha256:p",
        head_sha="a" * 40,
        worktree=worktree,
        log_dir=tmp_path,
    )

    def _dies_mid_stream(*_a: Any, **_k: Any) -> int:
        raise RuntimeError("reviewer stream died after the container started")

    import kew.dispatch_core as _dc
    import kew.graph.driver.review_launcher as _rl

    monkeypatch_runtime(_rl, "docker")
    try:
        with (
            patch.object(
                _dc, "_launch_container", return_value=_StartedReviewerContainer()
            ),
            patch.object(_dc, "_stream_sandbox_log", side_effect=_dies_mid_stream),
        ):
            collected = sandbox_reviewer_launcher(config, store)(ctx)
    finally:
        _rl.detect_runtime_cached = _rl_orig  # type: ignore[name-defined]

    # Fail-closed as ever: a stream that died collected no verdict.
    assert collected.parsed.unparseable
    # But the lane it names is the one that failed, not a launch that never was.
    assert collected.error_category != "launch_failed"
    assert collected.failure_layer == "stream"
    assert _crash_hold_reason(collected.error_category) == "review_unparseable"
    row = store.get("rev-midstream")
    assert row is not None
    assert row.status == "failure"
    assert row.error_category == collected.error_category


def test_a_reviewer_the_silence_watchdog_stopped_reports_a_silent_stream(
    tmp_path: Path, store: SqliteRunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2240: the container primitive returns 137 with the run named
    ``stream_silent`` — the watchdog stopped a reviewer whose model call went
    quiet. The launcher hands the stage that category on the stream lane, the
    one the stage retries without spending a round, never a bare unparseable
    round."""
    from kew.graph.driver.review_stage import _INFRA_RETRIES
    from kew.tracking import STREAM_SILENT_CATEGORY, DispatchResult

    config = _fx._config()
    config.review.adversarial = True
    config.logging.dir = str(tmp_path / "logs")
    worktree = tmp_path / "wt"
    worktree.mkdir()
    ctx = ReviewerLaunchContext(
        item_id=ITEM,
        run_id="rev-silent",
        prompt="p",
        prompt_artifact_sha="sha256:p",
        head_sha="a" * 40,
        worktree=worktree,
        log_dir=tmp_path,
    )

    def _stopped_for_silence(*_a: Any, result: DispatchResult, **_k: Any) -> int:
        result.launched = True
        result.exit_code = 137
        result.error_category = STREAM_SILENT_CATEGORY
        return 137

    import kew.graph.driver.review_launcher as _rl

    monkeypatch.setattr(_rl, "detect_runtime_cached", lambda _cfg: "docker")
    monkeypatch.setattr(
        "kew.dispatch_sandbox.run_container_and_stream", _stopped_for_silence
    )

    collected = sandbox_reviewer_launcher(config, store)(ctx)

    assert collected.parsed.unparseable
    assert collected.failure_layer == "stream"
    assert collected.error_category == STREAM_SILENT_CATEGORY
    assert STREAM_SILENT_CATEGORY in _INFRA_RETRIES
    row = store.get("rev-silent")
    assert row is not None
    assert row.status == "failure"


def test_a_usage_limit_the_stream_reported_outranks_a_later_silence_stop(
    tmp_path: Path, store: SqliteRunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reviewer whose stream carried rejected usage-limit notices and then
    went quiet until the watchdog stopped it is a rate-limited round, never a
    silent one: the stage must wait out the limit and record the round's
    credential (#3330), not retry at once."""
    import json as _json

    from support.review_stream import capture_of, rate_limited_stream

    import kew.graph.driver.review_launcher as _rl
    from kew.review_contract import RATE_LIMITED_CATEGORY
    from kew.review_stream import capture_path_for
    from kew.tracking import STREAM_SILENT_CATEGORY, DispatchResult

    config = _fx._config()
    config.review.adversarial = True
    config.logging.dir = str(tmp_path / "logs")
    worktree = tmp_path / "wt"
    worktree.mkdir()
    ctx = ReviewerLaunchContext(
        item_id=ITEM,
        run_id="rev-limited-then-silent",
        prompt="p",
        prompt_artifact_sha="sha256:p",
        head_sha="a" * 40,
        worktree=worktree,
        log_dir=tmp_path,
    )
    log_file = (tmp_path / "logs").resolve() / f"review-{ctx.run_id}.log"

    def _limited_then_stopped(*_a: Any, result: DispatchResult, **_k: Any) -> int:
        capture = capture_of(rate_limited_stream(killed=True))
        capture_path_for(log_file).write_text(_json.dumps(capture))
        result.launched = True
        result.exit_code = 137
        result.error_category = STREAM_SILENT_CATEGORY
        return 137

    monkeypatch.setattr(_rl, "detect_runtime_cached", lambda _cfg: "docker")
    monkeypatch.setattr(
        "kew.dispatch_sandbox.run_container_and_stream", _limited_then_stopped
    )

    collected = sandbox_reviewer_launcher(config, store)(ctx)

    assert collected.error_category == RATE_LIMITED_CATEGORY


def test_a_reviewer_whose_container_never_launched_still_reports_launch_failed(
    tmp_path: Path, store: SqliteRunStore
) -> None:
    """The other half of the pair: with no launch evidence at all the failure IS
    a reviewer that never started, and keeps the #2043 hold that says so."""
    from kew.graph.driver.review_stage import _crash_hold_reason

    config = _fx._config()
    config.review.adversarial = True
    config.logging.dir = str(tmp_path / "logs")
    worktree = tmp_path / "wt"
    worktree.mkdir()
    ctx = ReviewerLaunchContext(
        item_id=ITEM,
        run_id="rev-nolaunch",
        prompt="p",
        prompt_artifact_sha="sha256:p",
        head_sha="a" * 40,
        worktree=worktree,
        log_dir=tmp_path,
    )

    def _never_launches(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("the runtime refused to create the container")

    import kew.dispatch_core as _dc
    import kew.graph.driver.review_launcher as _rl

    monkeypatch_runtime(_rl, "docker")
    try:
        with patch.object(_dc, "_launch_container", side_effect=_never_launches):
            collected = sandbox_reviewer_launcher(config, store)(ctx)
    finally:
        _rl.detect_runtime_cached = _rl_orig  # type: ignore[name-defined]

    assert collected.parsed.unparseable
    assert collected.error_category == "launch_failed"
    assert collected.failure_layer == "launch"
    assert _crash_hold_reason(collected.error_category) == "review_launch_failed"
    row = store.get("rev-nolaunch")
    assert row is not None
    assert row.error_category == "launch_failed"


class _StartupRefusedContainer:
    """A container that refused its agent's startup and exited: the runtime
    RETURNS, with a refusal and no observed workload start."""

    startup_refusal_reason = "ERROR: git preparation failed before agent startup"

    def __iter__(self) -> Any:
        return iter(())

    def wait(self, *_a: Any, **_k: Any) -> int:
        return 78

    def kill(self, *_a: Any, **_k: Any) -> Any:
        return None


def test_a_reviewer_whose_container_refused_startup_never_started(
    tmp_path: Path, store: SqliteRunStore
) -> None:
    """#3186: a container that refused its agent's startup returns normally
    with ``launched=False``. Read by its exit code alone it was a failed run's
    missing verdict; it is a reviewer that never started."""
    from kew.graph.driver.review_stage import _crash_hold_reason

    config = _fx._config()
    config.review.adversarial = True
    config.logging.dir = str(tmp_path / "logs")
    worktree = tmp_path / "wt"
    worktree.mkdir()
    ctx = ReviewerLaunchContext(
        item_id=ITEM,
        run_id="rev-startup-refused",
        prompt="p",
        prompt_artifact_sha="sha256:p",
        head_sha="a" * 40,
        worktree=worktree,
        log_dir=tmp_path,
    )

    import kew.dispatch_core as _dc
    import kew.graph.driver.review_launcher as _rl

    monkeypatch_runtime(_rl, "docker")
    try:
        with (
            patch.object(
                _dc, "_launch_container", return_value=_StartupRefusedContainer()
            ),
            patch.object(_dc, "_stream_sandbox_log", side_effect=draining_stream()),
        ):
            collected = sandbox_reviewer_launcher(config, store)(ctx)
    finally:
        _rl.detect_runtime_cached = _rl_orig  # type: ignore[name-defined]

    assert collected.parsed.unparseable
    assert collected.error_category == "sandbox_startup_refused"
    assert collected.failure_layer == "container"
    assert _crash_hold_reason(collected.error_category) == "review_launch_failed"
    row = store.get("rev-startup-refused")
    assert row is not None
    assert row.error_category == "sandbox_startup_refused"


def test_review_runner_without_a_verdict_channel_fails_closed_before_any_container(
    tmp_path: Path,
    store: SqliteRunStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``review.runner`` pinned to a runner without a typed final result →
    fail closed (#2021) before the image map is even consulted: no container is
    launched and the crash-path record is returned — NEVER a silent
    review-with-claude under a config that pinned a different runner (#1069)."""
    config = _fx._config()
    config.review.adversarial = True
    config.review.runner = "opencode"  # deliberately unmapped
    config.sandbox.images = {}
    config.logging.dir = str(tmp_path / "logs")
    worktree = tmp_path / "wt"
    worktree.mkdir()
    ctx = ReviewerLaunchContext(
        item_id=ITEM,
        run_id="rev-3",
        prompt="p",
        prompt_artifact_sha="sha256:p",
        head_sha="a" * 40,
        worktree=worktree,
        log_dir=tmp_path,
    )

    def _boom(*_a, **_k) -> int:  # pragma: no cover - must never run
        raise AssertionError("container must not launch for an unmapped runner")

    import kew.dispatch_core as _dc
    import kew.graph.driver.review_launcher as _rl

    # Runtime IS available — the missing image mapping alone must fail closed.
    monkeypatch_runtime(_rl, "docker")
    try:
        with (
            caplog.at_level("ERROR", logger="kew.graph.driver.review_launcher"),
            patch.object(_dc, "_launch_container", side_effect=_boom),
        ):
            collected = sandbox_reviewer_launcher(config, store)(ctx)
    finally:
        _rl.detect_runtime_cached = _rl_orig  # type: ignore[name-defined]

    assert collected.parsed.unparseable  # no verdict written == crash path
    # The refusal names the missing verdict channel (the real reason).
    assert any(
        "opencode" in r.getMessage() and "no typed final result" in r.getMessage()
        for r in caplog.records
    )
    assert "no verdict channel" in (collected.parsed.unparseable_reason or "")
    row = store.get("rev-3")
    assert row is not None
    assert row.error_category == "sandbox_refused"
    assert row.status == "failure"


def test_mapped_opencode_reviewer_is_refused_before_credentials_or_container(
    tmp_path: Path, store: SqliteRunStore
) -> None:
    """A mapped opencode reviewer (#1163) has no typed final result record kew
    can bind a verdict to (#2021), so it is refused before any credential is
    minted or container launched, even though its image is mapped."""
    config = _fx._config()
    config.review.adversarial = True
    config.review.runner = "opencode"
    config.review.model = "openrouter/moonshotai/kimi-k2.7-code"
    config.sandbox.runtime = "docker"
    config.sandbox.images = {"opencode": "ghcr.io/example/kew-opencode:1"}
    config.logging.dir = str(tmp_path / "logs")
    worktree = tmp_path / "wt"
    worktree.mkdir()
    ctx = ReviewerLaunchContext(
        item_id=ITEM,
        run_id="rev-kimi",
        prompt="review this",
        prompt_artifact_sha="sha256:prompt",
        head_sha="a" * 40,
        worktree=worktree,
        log_dir=Path(config.logging.dir).resolve(),
    )

    with (
        patch(
            "kew.graph.driver.review_launcher.detect_runtime_cached"
        ) as detect_runtime,
        patch("kew.dispatch_sandbox.run_container_and_stream") as launch,
    ):
        collected = sandbox_reviewer_launcher(config, store)(ctx)

    detect_runtime.assert_not_called()
    launch.assert_not_called()
    assert collected.parsed.unparseable
    assert collected.failure_layer == "runner"
    row = store.get("rev-kimi")
    assert row is not None
    assert row.runner_name == "opencode"
    assert row.error_category == "sandbox_refused"


def test_mapped_codex_reviewer_refuses_before_sidecar_or_container(
    tmp_path: Path, store: SqliteRunStore
) -> None:
    config = _fx._config()
    config.review.adversarial = True
    config.review.runner = "codex"
    config.review.model = "gpt-5.6-sol"
    config.sandbox.images = {"codex": "ghcr.io/example/kew-codex:1"}
    config.logging.dir = str(tmp_path / "logs")
    worktree = tmp_path / "wt"
    worktree.mkdir()
    source = worktree / ".kew-review-verdict.json"
    source.write_text(
        json.dumps(
            {
                "verdict": "READY",
                "acceptance_evidence": [
                    {"command": "pytest", "output_digest": "sha256:z"}
                ],
            }
        )
    )
    ctx = ReviewerLaunchContext(
        item_id=ITEM,
        run_id="rev-codex",
        prompt="review",
        prompt_artifact_sha="sha256:p",
        head_sha="a" * 40,
        worktree=worktree,
        log_dir=tmp_path / "logs",
    )

    with (
        patch(
            "kew.graph.driver.review_launcher.detect_runtime_cached"
        ) as detect_runtime,
        patch("kew.graph.driver.review_launcher.collect_stream_verdict") as collect,
        patch("kew.dispatch_sandbox.run_container_and_stream") as launch,
    ):
        result = sandbox_reviewer_launcher(config, store)(ctx)

    detect_runtime.assert_not_called()
    collect.assert_not_called()
    launch.assert_not_called()
    assert result.parsed.unparseable
    assert "no approved container credential" in (
        result.parsed.unparseable_reason or ""
    )
    row = store.get("rev-codex")
    assert row is not None
    assert row.error_category == "credential_missing"
    assert row.status == "failure"
    assert source.exists()


def test_reviewer_argv0_is_the_bare_runtime_name_not_the_resolved_path(
    tmp_path: Path, store: SqliteRunStore
) -> None:
    """Bug D: detect_runtime_cached returns the shutil.which() PATH
    (``/usr/local/bin/docker`` in production), but argv[0] goes through the
    subprocess allowlist, which matches bare NAMES only. If the launcher passes
    the resolved path to build_run_argv, the container launch dies with
    ``Unknown executable '/usr/local/bin/docker' — not in allowlist``.

    The other launcher tests monkeypatch detect_runtime_cached to return the
    bare ``"docker"``, which structurally masks this — argv[0] happens to be a
    name either way. Here we return a realistic PATH and assert the captured
    argv[0] survives the REAL resolve_cmd allowlist (the exact production gate).
    The argv is captured at the shared ``_launch_container`` seam the reviewer
    now routes through via ``run_container_and_stream`` (#1023).
    """
    config = _fx._config()
    config.review.adversarial = True
    config.sandbox.runtime = "docker"
    config.logging.dir = str(tmp_path / "logs")
    Path(config.logging.dir).mkdir(parents=True, exist_ok=True)

    worktree = tmp_path / "wt"
    worktree.mkdir()
    launch = ContainerLaunchCapture()

    def _fake_stream(handle, log_file, st, run_id, *, model, overrides, runner) -> int:
        return 0

    launcher = sandbox_reviewer_launcher(config, store)
    ctx = ReviewerLaunchContext(
        item_id=ITEM,
        run_id="rev-argv0",
        prompt="review this",
        prompt_artifact_sha="sha256:prompt",
        head_sha="a" * 40,
        worktree=worktree,
        log_dir=Path(config.logging.dir).resolve(),
    )

    import kew.dispatch_core as _dc
    import kew.graph.driver.review_launcher as _rl

    # Production reality: detect_runtime_cached returns the resolved path.
    monkeypatch_runtime(_rl, "/usr/local/bin/docker")
    try:
        with (
            patch.object(_dc, "_launch_container", side_effect=launch),
            patch.object(_dc, "_stream_sandbox_log", side_effect=_fake_stream),
        ):
            launcher(ctx)
    finally:
        _rl.detect_runtime_cached = _rl_orig  # type: ignore[name-defined]

    assert launch.admissions[0].store is store
    argv = launch.argvs[0]
    # The allowlist (subprocess_utils.resolve_cmd) matches bare names only, so
    # the invariant it enforces — env-independently — is that argv[0] has no
    # path separator. With the bug, argv[0] is "/usr/local/bin/docker".
    assert argv[0] == config.sandbox.runtime, (
        f"argv[0] must be the bare runtime name, got {argv[0]!r}"
    )
    assert "/" not in argv[0], (
        f"argv[0] must be a bare name (no path) to pass the allowlist, got {argv[0]!r}"
    )


# --------------------------------------------------------------------------- #
# D. The concrete production remediator (#1030) — dispatch_review faked          #
# --------------------------------------------------------------------------- #
def _needs_changes_sidecar(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "verdict": "NEEDS_CHANGES",
                "findings": [
                    {
                        "severity": "high",
                        "location": "code.py:1",
                        "summary": "unchecked path",
                        "evidence": {
                            "command": "uv run pytest -q",
                            "output_digest": "sha256:y",
                        },
                    }
                ],
            }
        )
    )


def _run_remediator(
    config: DispatchConfig,
    store: SqliteRunStore,
    ctx_kwargs: dict[str, Any],
) -> dict[str, Any]:
    """Run ``sandbox_remediator`` with ``dispatch_review`` faked; return the
    kwargs it was (or was not) called with under key ``called``."""
    from kew.graph.driver.review_launcher import sandbox_remediator
    from kew.graph.driver.review_stage import RemediationContext

    captured: dict[str, Any] = {"called": None}

    def _fake_dispatch(**kwargs: Any) -> int:
        captured["called"] = kwargs
        return 0

    import kew.review as _rv

    with (
        patch.object(_rv, "dispatch_review", _fake_dispatch),
        patch("kew.runner_preflight.runner_preflight_failure", return_value=None),
    ):
        sandbox_remediator(config, store, epic_ref=EPIC)(
            RemediationContext(**ctx_kwargs)
        )
    return captured


def test_sandbox_remediator_feeds_host_stored_findings_to_dispatch_review(
    tmp_path: Path, store: SqliteRunStore
) -> None:
    """The production remediator loads the evidenced findings from the HOST-stored
    sidecar and hands them to the #987 governed remediation path (item_id set),
    never fetching PR comments (F10)."""
    config = _fx._config()
    config.review.adversarial = True
    config.logging.dir = str(tmp_path / "logs")
    sidecar = tmp_path / "review-sidecar-x.json"
    _needs_changes_sidecar(sidecar)

    captured = _run_remediator(
        config,
        store,
        {
            "item_id": ITEM,
            "issue_num": 1,
            "pr_number": 7,
            "head_sha": "a" * 40,
            "review_round": 1,
            "sidecar_path": str(sidecar),
        },
    )

    called = captured["called"]
    assert called is not None
    assert called["item_id"] == ITEM  # governed mode: review pool + reservation
    assert called["pr_number"] == 7
    assert [f.summary for f in called["findings"]] == ["unchecked path"]


def test_sandbox_remediator_runner_preflight_refusal_does_not_launch(
    tmp_path: Path, store: SqliteRunStore, caplog: pytest.LogCaptureFixture
) -> None:
    import kew.review as review_module
    from kew.graph.driver.review_launcher import sandbox_remediator
    from kew.graph.driver.review_stage import RemediationContext

    config = _fx._config()
    config.logging.dir = str(tmp_path / "logs")
    sidecar = tmp_path / "review-sidecar-x.json"
    _needs_changes_sidecar(sidecar)
    refusal = "Runner 'codex' is not logged in. Authenticate with: codex login"
    context = RemediationContext(
        item_id=ITEM,
        issue_num=1,
        pr_number=7,
        head_sha="a" * 40,
        review_round=1,
        sidecar_path=str(sidecar),
    )

    with (
        patch("kew.runner_preflight.runner_preflight_failure", return_value=refusal),
        patch.object(review_module, "dispatch_review") as dispatch,
        caplog.at_level("ERROR", logger="kew.graph.driver.review_launcher"),
    ):
        remediator = sandbox_remediator(config, store, epic_ref=EPIC)
        result = remediator(context)
        first_log_count = caplog.text.count(refusal)
        second_result = remediator(context)

    assert result is None
    assert second_result is None
    dispatch.assert_not_called()
    assert refusal in caplog.text
    assert first_log_count == 1
    assert caplog.text.count(refusal) == 1
    holds = store.list_epic_holds(EPIC)
    assert [hold.item_id for hold in holds] == [ITEM]
    assert refusal in holds[0].reason
    assert "codex login" in holds[0].reason
    assert "kew epic unhold" in holds[0].reason
    assert "node held for operator" in caplog.text


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda kw: kw.update(sidecar_path=None), id="no-sidecar-path"),
        pytest.param(lambda kw: kw.update(pr_number=None), id="no-pr-number"),
        pytest.param(
            lambda kw: kw.update(sidecar_path="/nonexistent/sidecar.json"),
            id="missing-sidecar-file",
        ),
    ],
)
def test_sandbox_remediator_no_ops_when_nothing_to_act_on(
    tmp_path: Path, store: SqliteRunStore, mutate: Any
) -> None:
    """Fail-closed: with no locatable findings or no PR, the remediator logs and
    NO-OPS — it never dispatches an empty remediation, and never raises."""
    config = _fx._config()
    config.review.adversarial = True
    config.logging.dir = str(tmp_path / "logs")
    kwargs: dict[str, Any] = {
        "item_id": ITEM,
        "issue_num": 1,
        "pr_number": 7,
        "head_sha": "a" * 40,
        "review_round": 1,
        "sidecar_path": str(tmp_path / "s.json"),
    }
    _needs_changes_sidecar(tmp_path / "s.json")
    mutate(kwargs)

    captured = _run_remediator(config, store, kwargs)
    assert captured["called"] is None  # dispatch_review was NOT invoked


# Minimal monkeypatch helper for detect_runtime_cached (module-global swap so
# the launcher's ``config.sandbox`` lookup is bypassed deterministically).
_rl_orig = None


def monkeypatch_runtime(mod: Any, value: Any) -> None:
    global _rl_orig
    _rl_orig = mod.detect_runtime_cached
    mod.detect_runtime_cached = lambda _cfg: value


# --------------------------------------------------------------------------- #
# D. `kew-review` GitHub status check (#1056) — the human-merge-race gate       #
# --------------------------------------------------------------------------- #
def _record_review_status(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    """Record the `kew-review` status posts the production tick emits.

    Patches the ``review_status`` module the review stage calls into, so the tick
    drives the REAL publish call sites — only the ``gh`` POST itself is captured.
    """
    from kew import review_status

    # gh is absent in this sandbox; the status posts are captured below, and the
    # merge pass never shells gh for an unapproved PR — so the required-tool
    # preflight (which would SystemExit on missing gh) is neutralized here.
    monkeypatch.setattr("kew.graph.driver.tick.preflight_tools", lambda: None)
    rec: dict[str, list[Any]] = {"pending": [], "verdict": [], "failure": []}
    monkeypatch.setattr(
        review_status, "publish_pending", lambda repo, sha: rec["pending"].append(sha)
    )
    monkeypatch.setattr(
        review_status,
        "publish_verdict",
        lambda repo, sha, verdict: rec["verdict"].append((sha, verdict)),
    )
    monkeypatch.setattr(
        review_status,
        "publish_failure",
        lambda repo, sha, reason: rec["failure"].append((sha, reason)),
    )
    return rec


def test_opening_a_review_pr_posts_pending_then_flips_to_success_on_ready(
    tmp_path: Path, store: SqliteRunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A review-enabled PR posts `pending` on dispatch; a READY on the current
    head flips the `kew-review` check to success — keyed to the reviewed SHA."""
    repo, head = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    rec = _record_review_status(monkeypatch)
    config = _review_config(tmp_path, blocking=True)
    facts = FakeFactsReader(
        issue_facts_by_num={int(ITEM): _pr_open_facts(head)},
        pr_facts_by_num={7: _pr_facts(head, approved=False, ci_clean=False)},
    )

    _advance_one(
        config=config,
        store=store,
        facts=facts,
        sink=FakeSink(),
        launcher=_spy_ready_launcher(),
        tmp_path=tmp_path,
    )

    assert rec["pending"] == [head]  # gate went pending the moment review dispatched
    assert rec["verdict"] == [(head, "READY")]  # ...and flipped to success on READY


def test_needs_changes_holds_the_check_at_failure(
    tmp_path: Path, store: SqliteRunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A NEEDS_CHANGES verdict on the current head fails the `kew-review` check."""
    repo, head = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    rec = _record_review_status(monkeypatch)
    config = _review_config(tmp_path, blocking=True)
    facts = FakeFactsReader(
        issue_facts_by_num={int(ITEM): _pr_open_facts(head)},
        pr_facts_by_num={7: _pr_facts(head, approved=False, ci_clean=False)},
    )

    def _needs_changes(ctx: ReviewerLaunchContext) -> CollectedSidecar:
        return stream_verdict(
            ctx,
            {
                "verdict": "NEEDS_CHANGES",
                "findings": [
                    {
                        "severity": "high",
                        "location": "code.py:1",
                        "summary": "unchecked path",
                        "evidence": {
                            "command": "uv run pytest -q",
                            "output_digest": "sha256:y",
                        },
                    }
                ],
            },
        )

    _advance_one(
        config=config,
        store=store,
        facts=facts,
        sink=FakeSink(),
        launcher=_needs_changes,
        tmp_path=tmp_path,
    )

    assert rec["pending"] == [head]
    assert rec["verdict"] == [(head, "NEEDS_CHANGES")]  # required check stays red


def test_a_post_verdict_force_push_returns_the_check_to_pending(
    tmp_path: Path, store: SqliteRunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A verdict recorded on head1, then a force-push to head2: the next tick
    re-dispatches on the MOVED head and re-posts `pending` on head2 — the gate
    follows the code, never satisfied by a stale review of head1 (mirrors F1)."""
    repo, head1 = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    # Force-push: a new commit moves the head the verdict no longer attests.
    (repo / "code.py").write_text("x = 2\n")
    run(["git", "add", "-A"], cwd=repo, check=True)
    run(["git", "commit", "-q", "-m", "force-push"], cwd=repo, check=True)
    head2 = run(["git", "rev-parse", "HEAD"], cwd=repo, check=True).stdout.strip()

    config = _review_config(tmp_path, blocking=True)
    # A READY verdict already recorded against the OLD head.
    store.record_review_round(
        EPIC, ITEM, head_sha=head1, verdict="READY", sidecar_digest="sha256:z"
    )
    rec = _record_review_status(monkeypatch)
    facts = FakeFactsReader(
        issue_facts_by_num={int(ITEM): _pr_open_facts(head2)},
        pr_facts_by_num={7: _pr_facts(head2, approved=False, ci_clean=False)},
    )

    _advance_one(
        config=config,
        store=store,
        facts=facts,
        sink=FakeSink(),
        launcher=_spy_ready_launcher(),
        tmp_path=tmp_path,
    )

    assert head2 in rec["pending"]  # moved head → back to pending on the new SHA
    # The stale head1 READY never posts success on head2 without a fresh round.
    assert (head1, "READY") not in rec["verdict"]


def test_reviewer_launch_provisions_the_environment_before_the_container(
    tmp_path: Path, store: SqliteRunStore
) -> None:
    """The host installs the checkout's locked dependencies in the review
    image before the reviewer container starts, so acceptance commands can run
    offline; the install gets the same checkout, image, and runner."""
    config = _fx._config()
    config.review.adversarial = True
    config.sandbox.runtime = "docker"
    config.logging.dir = str(tmp_path / "logs")
    Path(config.logging.dir).mkdir(parents=True, exist_ok=True)

    worktree = tmp_path / "wt"
    worktree.mkdir()
    order: list[str] = []
    seen: dict[str, Any] = {}

    def _fake_provision(cfg, *, image, runner, checkout_dir, slug, **_k) -> bool:
        order.append("provision")
        seen.update(image=image, runner=runner, checkout_dir=checkout_dir, slug=slug)
        return True

    launch = ContainerLaunchCapture(order=order)

    def _fake_stream(handle, log_file, st, run_id, *, model, overrides, runner) -> int:
        return 0

    launcher = sandbox_reviewer_launcher(config, store)
    ctx = ReviewerLaunchContext(
        item_id=ITEM,
        run_id="rev-env",
        prompt="review this",
        prompt_artifact_sha="sha256:prompt",
        head_sha="a" * 40,
        worktree=worktree,
        log_dir=Path(config.logging.dir).resolve(),
    )

    import kew.dispatch_core as _dc
    import kew.graph.driver.review_launcher as _rl
    import kew.review_environment as _re

    monkeypatch_runtime(_rl, "docker")
    try:
        with (
            patch.object(
                _re, "provision_review_environment", side_effect=_fake_provision
            ),
            patch.object(_dc, "_launch_container", side_effect=launch),
            patch.object(_dc, "_stream_sandbox_log", side_effect=_fake_stream),
        ):
            launcher(ctx)
    finally:
        _rl.detect_runtime_cached = _rl_orig  # type: ignore[name-defined]

    assert order == ["provision", "launch"]
    assert seen["checkout_dir"] == worktree
    assert seen["runner"] == "claude-code"
    assert seen["slug"] == branch_slug(ITEM)
    # The install and the reviewer use the very same resolved image.
    assert launch.admissions[0].store is store
    assert seen["image"] in launch.argvs[0], (seen["image"], launch.argvs[0])


def test_reviewer_launch_binds_the_base_lock_node_modules_read_only(
    tmp_path: Path, store: SqliteRunStore
) -> None:
    """#2559: the launched reviewer argv carries the cached frontend tree,
    keyed by the base lockfile hash the dispatch recorded, as a ``:ro`` bind."""
    config = _fx._config()
    config.review.adversarial = True
    config.sandbox.runtime = "docker"
    config.logging.dir = str(tmp_path / "logs")
    Path(config.logging.dir).mkdir(parents=True, exist_ok=True)
    worktree = tmp_path / "wt"
    (worktree / "frontend").mkdir(parents=True)
    lock_sha = "b" * 64
    cache = tmp_path / "cache"
    (cache / lock_sha).mkdir(parents=True)
    launch = ContainerLaunchCapture()

    def _fake_stream(handle, log_file, st, run_id, *, model, overrides, runner) -> int:
        return 0

    launcher = sandbox_reviewer_launcher(config, store)
    ctx = ReviewerLaunchContext(
        item_id=ITEM,
        run_id="rev-node-modules",
        prompt="review this",
        prompt_artifact_sha="sha256:prompt",
        head_sha="a" * 40,
        worktree=worktree,
        log_dir=Path(config.logging.dir).resolve(),
        frontend_lock_sha256=lock_sha,
    )

    import kew.dispatch_core as _dc
    import kew.graph.driver.review_launcher as _rl
    import kew.review_environment as _re
    import kew.review_frontend as _rf

    monkeypatch_runtime(_rl, "docker")
    try:
        with (
            patch.object(_re, "provision_review_environment", return_value=True),
            patch.object(_rf, "default_cache_root", return_value=cache),
            patch.object(_dc, "_launch_container", side_effect=launch),
            patch.object(_dc, "_stream_sandbox_log", side_effect=_fake_stream),
        ):
            launcher(ctx)
    finally:
        _rl.detect_runtime_cached = _rl_orig  # type: ignore[name-defined]

    bind = f"{(cache / lock_sha).absolute()}:/work/frontend/node_modules:ro"
    argv = launch.argvs[0]
    assert bind in argv, argv
    assert argv[argv.index(bind) - 1] == "-v"
    # The mounted reviewer keeps the agent's one pids cap (#2927).
    pids = [argv[i + 1] for i, arg in enumerate(argv) if arg == "--pids-limit"]
    assert pids == ["1024"], argv
    assert not [arg for arg in argv if arg.startswith("--pids-limit=")]


@pytest.mark.parametrize(
    ("detail", "timed_out", "category", "layer"),
    [
        ("could not be confirmed gone", False, "launch_failed", "launch"),
        # #3246: the ``rm -f`` timed out — the category the stage retries
        # after a back-off, not a bare launch failure.
        ("could not be removed (timed out)", True, "runtime_timeout", "container"),
    ],
)
def test_an_unconfirmed_provisioning_container_fails_the_review_closed(
    tmp_path: Path,
    store: SqliteRunStore,
    detail: str,
    timed_out: bool,
    category: str,
    layer: str,
) -> None:
    """An install container that cannot be proven gone must not share the
    host with the reviewer: no reviewer launch, a failed run row, no verdict.
    The run says why: a removal the daemon never answered is a
    ``runtime_timeout``."""
    from kew.review_environment import ProvisioningUnconfirmed

    config = _fx._config()
    config.review.adversarial = True
    config.sandbox.runtime = "docker"
    config.logging.dir = str(tmp_path / "logs")
    Path(config.logging.dir).mkdir(parents=True, exist_ok=True)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    launcher = sandbox_reviewer_launcher(config, store)
    ctx = ReviewerLaunchContext(
        item_id=ITEM,
        run_id="rev-env-stuck",
        prompt="review this",
        prompt_artifact_sha="sha256:prompt",
        head_sha="a" * 40,
        worktree=worktree,
        log_dir=Path(config.logging.dir).resolve(),
    )

    import kew.dispatch_core as _dc
    import kew.graph.driver.review_launcher as _rl
    import kew.review_environment as _re

    def _stuck(*_a, **_k):
        raise ProvisioningUnconfirmed(
            f"provisioning container 'kew-x-env' {detail}", timed_out=timed_out
        )

    monkeypatch_runtime(_rl, "docker")
    try:
        with (
            patch.object(_re, "provision_review_environment", side_effect=_stuck),
            patch.object(_dc, "_launch_container") as launch,
        ):
            result = launcher(ctx)
    finally:
        _rl.detect_runtime_cached = _rl_orig  # type: ignore[name-defined]

    launch.assert_not_called()
    assert result.parsed.unparseable
    assert (result.error_category, result.failure_layer) == (category, layer)
    row = store.get("rev-env-stuck")
    assert row is not None and row.status == "failure"
    assert row.error_category == category
