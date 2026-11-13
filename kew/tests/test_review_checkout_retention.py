"""Each PR keeps at most one preserved review checkout (#2961, #3186).

A failed or refused review used to leave its checkout for the next launch to
rename aside, with no cap and no prune: two refusal loops filled 33 GB. The
checkouts here are real clones of a real origin; only the forge and the agent
launch are faked.

The prune never costs a person's unpublished work, and never keeps more than
it must for it: each deferred publication keeps exactly the one copy its
evidence names, while its run is in a deferred state and that copy is on
disk, whatever the PR head. Every other copy keeps the normal rule. When no
copy can be told apart from the deferred one — a deferred run's checkout is
unknown — every copy is kept; when the ledger cannot be read, the checkout is
not moved at all.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kew import subprocess_utils
from kew.config import DispatchConfig
from kew.dispatch_core import WorkerCapReached
from kew.epic_holds import deferred_publication_key
from kew.graph.driver.facts import NodeFacts
from kew.graph.driver.tick import _reconcile_github_links
from kew.review import dispatch_review
from kew.review_checkout import (
    ReviewCheckoutError,
    _copy_is_gone,
    forget_review_checkouts,
    materialize_review_checkout,
    review_checkout_path,
)
from kew.review_contract import Finding
from kew.run_store import RunRecord, SqliteRunStore
from kew.tracking import DispatchResult

PR = 178
PR_BRANCH = "agent/issue-42"


@pytest.fixture(autouse=True)
def _git_allowed():
    saved = subprocess_utils.TOOL_PATHS.copy()
    subprocess_utils._register_optional_tool("git")
    yield
    subprocess_utils.TOOL_PATHS.clear()
    subprocess_utils.TOOL_PATHS.update(saved)


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A bare origin with the PR branch pushed; cwd is a detached clone."""
    origin = tmp_path / "origin.git"
    _git("init", "--bare", "-q", str(origin), cwd=tmp_path)
    work = tmp_path / "work"
    _git("clone", "-q", str(origin), str(work), cwd=tmp_path)
    _git("config", "user.email", "t@t.t", cwd=work)
    _git("config", "user.name", "t", cwd=work)
    (work / "f.txt").write_text("base")
    _git("add", ".", cwd=work)
    _git("commit", "-qm", "base", cwd=work)
    _git("branch", "-M", "main", cwd=work)
    _git("push", "-q", "origin", "main", cwd=work)
    _git("checkout", "-qb", PR_BRANCH, cwd=work)
    (work / "pr.txt").write_text("pr work")
    _git("add", ".", cwd=work)
    _git("commit", "-qm", "pr work", cwd=work)
    _git("push", "-q", "origin", PR_BRANCH, cwd=work)
    _git("checkout", "-q", "--detach", cwd=work)
    monkeypatch.chdir(work)
    return work


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SqliteRunStore]:
    with SqliteRunStore(str(tmp_path / "d.db")) as bound:
        yield bound


def _cfg(tmp_path: Path) -> DispatchConfig:
    cfg = DispatchConfig()
    cfg.branches.worktree_base = str(tmp_path / "wt")
    assert cfg.publication.publish == "canonical"
    return cfg


def _preserved(cfg: DispatchConfig) -> list[Path]:
    review_dir = review_checkout_path(cfg, PR)
    return sorted(review_dir.parent.glob(f"{review_dir.name}.preserved-*"))


def _failed_review(
    cfg: DispatchConfig,
    marker: str,
    store: SqliteRunStore | None,
    *,
    head: str | None = None,
) -> None:
    """A review whose agent ran and failed: its checkout is left in place.
    *head* is the PR head the caller saw (GitHub's ``headRefOid``)."""
    checkout = materialize_review_checkout(
        pr_number=PR,
        branch_name=PR_BRANCH,
        config=cfg,
        store=store,
        expected_head_sha=head,
    )
    (checkout.path / "marker.txt").write_text(marker)
    checkout.release()


def _pr_head(work: Path) -> str:
    _git("fetch", "-q", "origin", cwd=work)
    return subprocess.run(
        ["git", "rev-parse", f"origin/{PR_BRANCH}"],
        cwd=work,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _push_new_head(work: Path) -> str:
    """Move the PR head on origin, as publishing a deferral would."""
    _git("checkout", "-q", PR_BRANCH, cwd=work)
    (work / "published.txt").write_text("published")
    _git("add", ".", cwd=work)
    _git("commit", "-qm", "published", cwd=work)
    _git("push", "-q", "origin", PR_BRANCH, cwd=work)
    _git("checkout", "-q", "--detach", cwd=work)
    return _pr_head(work)


def _remediation_run(
    store: SqliteRunStore,
    *,
    status: str = "awaiting_publication",
    category: str = "publication_held",
    run_id: str = "deferred-1",
) -> None:
    store.save(
        RunRecord(
            run_id=run_id,
            issue_num=PR,
            item_id="42",
            branch=PR_BRANCH,
            status=status,  # type: ignore[arg-type]
            exit_code=0,
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
            error_category=category,
            run_kind="review_remediation",
        )
    )


def _defer(
    store: SqliteRunStore,
    checkout: Path,
    head: str,
    *,
    run_id: str = "deferred-1",
    **row: str,
) -> None:
    """A remediation of the PR whose publication was deferred at *head*, its
    work left in *checkout* — the run row and the evidence ``kew review``
    records for it."""
    _remediation_run(store, run_id=run_id, **row)
    store.set_meta(
        deferred_publication_key(run_id),
        json.dumps({"head": head, "checkout": str(checkout), "pr": PR}),
    )


def _deferred_checkout(store: SqliteRunStore, run_id: str = "deferred-1") -> Path:
    raw = store.get_meta(deferred_publication_key(run_id))
    assert raw is not None
    return Path(json.loads(raw)["checkout"])


def test_failed_reviews_keep_only_the_newest_preserved_copy(
    tmp_path: Path, work: Path, store: SqliteRunStore
) -> None:
    cfg = _cfg(tmp_path)

    _failed_review(cfg, "review-1", store)
    _failed_review(cfg, "review-2", store)
    assert len(_preserved(cfg)) == 1

    _failed_review(cfg, "review-3", store)

    (kept,) = _preserved(cfg)
    assert (kept / "marker.txt").read_text() == "review-2"
    assert (review_checkout_path(cfg, PR) / "marker.txt").read_text() == "review-3"


def test_another_prs_copies_are_never_pruned(
    tmp_path: Path, work: Path, store: SqliteRunStore
) -> None:
    cfg = _cfg(tmp_path)
    other = review_checkout_path(cfg, PR * 10)
    sibling = other.with_name(f"{other.name}.preserved-20260101T000000000000Z")
    sibling.mkdir(parents=True)

    for n in range(3):
        _failed_review(cfg, f"review-{n}", store)

    assert sibling.is_dir()


@pytest.mark.parametrize(
    "deferral",
    [
        pytest.param({}, id="awaiting-publication-row"),
        pytest.param(
            {"status": "failure", "category": "publication_review_required"},
            id="publication-review-required-row",
        ),
        pytest.param(
            {"status": "failure", "category": "held_publication"},
            id="held-publication-row",
        ),
    ],
)
def test_a_deferrals_copy_survives_the_prune(
    tmp_path: Path, work: Path, store: SqliteRunStore, deferral: dict[str, str]
) -> None:
    """#3186: a deferral keeps the one copy it names while its run is deferred
    and the copy is on disk. The next review moves its checkout aside; the
    evidence follows the move, that copy is never pruned, and the rest keep
    the normal rule."""
    cfg = _cfg(tmp_path)
    head = _pr_head(work)
    _failed_review(cfg, "deferred-work", store, head=head)
    _defer(store, review_checkout_path(cfg, PR), head, **deferral)

    for n in range(3):
        _failed_review(cfg, f"review-{n}", store, head=head)

    kept = _deferred_checkout(store)
    assert kept in _preserved(cfg)
    assert (kept / "marker.txt").read_text() == "deferred-work"
    others = [p for p in _preserved(cfg) if p != kept]
    assert [(p / "marker.txt").read_text() for p in others] == ["review-1"]


def _merge_main_into_pr(work: Path) -> str:
    """Move the PR head the way kew's merge-main step does: merge main in."""
    _git("checkout", "-q", "main", cwd=work)
    (work / "main2.txt").write_text("main moved")
    _git("add", ".", cwd=work)
    _git("commit", "-qm", "main moved", cwd=work)
    _git("push", "-q", "origin", "main", cwd=work)
    _git("checkout", "-q", PR_BRANCH, cwd=work)
    _git("merge", "-q", "--no-edit", "main", cwd=work)
    _git("push", "-q", "origin", PR_BRANCH, cwd=work)
    _git("checkout", "-q", "--detach", cwd=work)
    return _pr_head(work)


def _markers(cfg: DispatchConfig) -> list[str]:
    return [(p / "marker.txt").read_text() for p in _preserved(cfg)]


@pytest.mark.parametrize("move_head", [_merge_main_into_pr, _push_new_head])
def test_a_deferral_keeps_its_copy_whatever_the_pr_head(
    tmp_path: Path, work: Path, store: SqliteRunStore, move_head: Any
) -> None:
    """#3186: kew's merge-main step (or "Update branch", a rebase, a push)
    moves the PR head without publishing the deferred work, so the head says
    nothing about it. The deferred copy survives any number of later reviews;
    the other copies are still pruned to the newest one."""
    cfg = _cfg(tmp_path)
    head = _pr_head(work)
    _failed_review(cfg, "deferred-work", store, head=head)
    _defer(store, review_checkout_path(cfg, PR), head)

    moved = move_head(work)
    for n in range(4):
        _failed_review(cfg, f"review-{n}", store, head=moved)

    kept = _deferred_checkout(store)
    assert (kept / "marker.txt").read_text() == "deferred-work"
    assert sorted(_markers(cfg)) == ["deferred-work", "review-2"]
    assert (review_checkout_path(cfg, PR) / "marker.txt").read_text() == "review-3"


def test_a_review_that_does_not_know_the_head_keeps_the_deferred_copy(
    tmp_path: Path, work: Path, store: SqliteRunStore
) -> None:
    cfg = _cfg(tmp_path)
    head = _pr_head(work)
    _failed_review(cfg, "deferred-work", store, head=head)
    _defer(store, review_checkout_path(cfg, PR), head)

    for n in range(3):
        _failed_review(cfg, f"review-{n}", store, head=None)

    assert "deferred-work" in _markers(cfg)
    assert len(_preserved(cfg)) == 2


def test_two_deferrals_each_keep_their_own_copy(
    tmp_path: Path, work: Path, store: SqliteRunStore
) -> None:
    cfg = _cfg(tmp_path)
    head = _pr_head(work)
    _failed_review(cfg, "deferred-1", store, head=head)
    _defer(store, review_checkout_path(cfg, PR), head, run_id="deferred-1")
    _failed_review(cfg, "deferred-2", store, head=head)
    _defer(store, review_checkout_path(cfg, PR), head, run_id="deferred-2")

    for n in range(3):
        _failed_review(cfg, f"review-{n}", store, head=head)

    assert sorted(_markers(cfg)) == ["deferred-1", "deferred-2", "review-1"]
    assert (_deferred_checkout(store, "deferred-1") / "marker.txt").read_text() == (
        "deferred-1"
    )
    assert (_deferred_checkout(store, "deferred-2") / "marker.txt").read_text() == (
        "deferred-2"
    )


def test_a_move_whose_record_cannot_be_written_leaves_the_work_in_place(
    tmp_path: Path, work: Path, store: SqliteRunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#3186: moving the deferred checkout aside without re-recording where it
    went would leave the evidence naming ``review/pr-N``, where the next
    launch clones afresh — and that clone would read as the deferred work. The
    launch is refused instead, the directory stays put, and the next launch
    that can record the move keeps the work."""
    cfg = _cfg(tmp_path)
    head = _pr_head(work)
    review_dir = review_checkout_path(cfg, PR)
    _failed_review(cfg, "deferred-work", store, head=head)
    _defer(store, review_dir, head)
    real = store.set_meta

    def _ledger_locked(key: str, value: str) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "set_meta", _ledger_locked)
    with pytest.raises(ReviewCheckoutError) as refused:
        _failed_review(cfg, "review-1", store, head=head)
    monkeypatch.setattr(store, "set_meta", real)

    assert refused.value.classification == "deferred_checkout_unrecorded"
    assert "could not be re-recorded" in str(refused.value)
    assert (review_dir / "marker.txt").read_text() == "deferred-work"
    assert _deferred_checkout(store) == review_dir
    assert _preserved(cfg) == []

    for n in range(2, 4):
        _failed_review(cfg, f"review-{n}", store, head=head)

    kept = _deferred_checkout(store)
    assert kept in _preserved(cfg)
    assert (kept / "marker.txt").read_text() == "deferred-work"
    assert sorted(_markers(cfg)) == ["deferred-work", "review-2"]


@pytest.mark.parametrize(
    "evidence",
    [
        pytest.param(None, id="no-evidence"),
        pytest.param("{not json", id="unreadable-evidence"),
        pytest.param(json.dumps({"head": "a" * 40, "pr": PR}), id="no-checkout"),
        pytest.param(
            json.dumps({"head": "a" * 40, "checkout": "review/pr-178", "pr": PR}),
            id="relative-checkout",
        ),
    ],
)
def test_a_deferral_whose_checkout_is_unknown_keeps_every_copy(
    tmp_path: Path, work: Path, store: SqliteRunStore, evidence: str | None
) -> None:
    """#3186: when the deferral's hold would read its checkout as unknown, no
    copy can be told apart from the one holding the work, so none is pruned."""
    cfg = _cfg(tmp_path)
    _remediation_run(store)
    if evidence is not None:
        store.set_meta(deferred_publication_key("deferred-1"), evidence)

    for n in range(3):
        _failed_review(cfg, f"review-{n}", store)

    assert _markers(cfg) == ["review-0", "review-1"]


def test_an_unknown_deferral_stops_the_prune_but_not_a_known_ones_record(
    tmp_path: Path, work: Path, store: SqliteRunStore
) -> None:
    """#3186: a deferred run whose copy is unknown keeps every copy, but a
    known deferral's record still follows its checkout when the next review
    moves it aside — or the next fresh clone at ``review/pr-N`` would read as
    its work. Once the unknown run settles, the prune keeps the known copy."""
    cfg = _cfg(tmp_path)
    head = _pr_head(work)
    review_dir = review_checkout_path(cfg, PR)
    _failed_review(cfg, "deferred-work", store, head=head)
    _defer(store, review_dir, head)
    _remediation_run(store, run_id="no-evidence")

    _failed_review(cfg, "review-1", store, head=head)
    _failed_review(cfg, "review-2", store, head=head)

    kept = _deferred_checkout(store)
    assert kept != review_dir
    assert kept in _preserved(cfg)
    assert (kept / "marker.txt").read_text() == "deferred-work"
    assert sorted(_markers(cfg)) == ["deferred-work", "review-1"]

    _remediation_run(
        store, run_id="no-evidence", status="failure", category="runner_failed"
    )
    for n in range(3, 5):
        _failed_review(cfg, f"review-{n}", store, head=head)

    assert _deferred_checkout(store) == kept
    assert sorted(_markers(cfg)) == ["deferred-work", "review-3"]


def test_a_copy_whose_presence_cannot_be_checked_counts_as_present(
    tmp_path: Path,
) -> None:
    """The review stage's own reading (``_checkout_is_gone``): only definite
    absence is a discard."""
    locked = tmp_path / "locked"
    copy = locked / "copy"
    copy.mkdir(parents=True)
    locked.chmod(0)
    try:
        assert _copy_is_gone(copy) is False
        assert _copy_is_gone(tmp_path / "absent") is True
    finally:
        locked.chmod(0o755)


def test_a_deferral_whose_copy_is_gone_protects_nothing(
    tmp_path: Path, work: Path, store: SqliteRunStore
) -> None:
    """A discarded deferral leaves its row ``awaiting_publication`` for good;
    the absent copy is what says it was discarded."""
    cfg = _cfg(tmp_path)
    head = _pr_head(work)
    _defer(store, tmp_path / "discarded", head)

    for n in range(3):
        _failed_review(cfg, f"review-{n}", store, head=head)

    assert len(_preserved(cfg)) == 1


def test_a_settled_run_of_the_pr_does_not_stop_the_prune(
    tmp_path: Path, work: Path, store: SqliteRunStore
) -> None:
    cfg = _cfg(tmp_path)
    head = _pr_head(work)
    _failed_review(cfg, "review-0", store, head=head)
    _defer(
        store,
        review_checkout_path(cfg, PR),
        head,
        status="failure",
        category="runner_failed",
    )

    for n in range(1, 4):
        _failed_review(cfg, f"review-{n}", store, head=head)

    assert len(_preserved(cfg)) == 1


def test_without_a_ledger_every_copy_is_kept(tmp_path: Path, work: Path) -> None:
    cfg = _cfg(tmp_path)

    for n in range(3):
        _failed_review(cfg, f"review-{n}", None)

    assert _markers(cfg) == ["review-0", "review-1"]


def test_an_unreadable_ledger_leaves_the_checkout_in_place(
    tmp_path: Path, work: Path, store: SqliteRunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#3186: a ledger that cannot be read cannot say which deferral the
    checkout holds, so its move could not be recorded. Moved anyway, the
    deferral's evidence would name the fresh clone at ``review/pr-N`` and its
    work would become an unprotected copy the next readable prune deletes. The
    launch is refused instead, and the next launch that can read the ledger
    moves the work with its record."""
    cfg = _cfg(tmp_path)
    head = _pr_head(work)
    review_dir = review_checkout_path(cfg, PR)
    _failed_review(cfg, "deferred-work", store, head=head)
    _defer(store, review_dir, head)
    real = store.list_by_issue

    def _ledger_locked(issue_num: int) -> list[RunRecord]:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "list_by_issue", _ledger_locked)
    with pytest.raises(ReviewCheckoutError) as refused:
        _failed_review(cfg, "review-1", store, head=head)
    monkeypatch.setattr(store, "list_by_issue", real)

    assert refused.value.classification == "deferred_checkout_unrecorded"
    assert (review_dir / "marker.txt").read_text() == "deferred-work"
    assert _deferred_checkout(store) == review_dir
    assert _preserved(cfg) == []

    for n in range(2, 4):
        _failed_review(cfg, f"review-{n}", store, head=head)

    kept = _deferred_checkout(store)
    assert kept in _preserved(cfg)
    assert (kept / "marker.txt").read_text() == "deferred-work"
    assert sorted(_markers(cfg)) == ["deferred-work", "review-2"]


def _forge(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    """Answer ``gh pr view`` with the PR; every other argv runs for real."""
    if cmd[:3] == ["gh", "pr", "view"]:
        meta = {"number": PR, "headRefName": PR_BRANCH, "body": "Closes #42"}
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(meta), stderr="")
    return subprocess_utils.run(cmd, **kwargs)


def _review_with(cfg: DispatchConfig, agent: Any) -> object:
    """``kew review`` whose agent launch is *agent* (a ``run_tracked_agent``
    double), with the forge answered and everything else real."""
    finding = Finding(
        severity="high",
        location="f.txt:1",
        summary="fix it",
        evidence_command="pytest",
        evidence_output_digest="sha256:cafe",
    )
    with (
        patch("kew.review._check_github_connectivity", return_value=True),
        patch("kew.review.subprocess_run", side_effect=_forge),
        patch("kew.review.detect_base_branch", return_value="main"),
        patch("kew.review.resolve_runner", return_value=MagicMock()),
        patch("kew.review.run_tracked_agent", side_effect=agent),
    ):
        return dispatch_review(pr_number=PR, config=cfg, findings=[finding])


def test_launches_refused_before_start_leave_no_review_checkout(
    tmp_path: Path, work: Path
) -> None:
    cfg = _cfg(tmp_path)
    cfg.sandbox.dangerously_skip_containment = True

    for _ in range(3):
        outcome = _review_with(cfg, WorkerCapReached(42))
        assert getattr(outcome, "exit_code", None) == 1

    assert _preserved(cfg) == []
    assert not review_checkout_path(cfg, PR).exists()


@pytest.mark.parametrize("category", ["sandbox_startup_refused", "runtime_timeout"])
@pytest.mark.parametrize("first_attempt_ran", [True, False])
def test_a_startup_refusal_after_an_attempt_that_ran_keeps_its_work(
    tmp_path: Path, work: Path, first_attempt_ran: bool, category: str
) -> None:
    """#3186: launch evidence is cumulative across attempts. A second attempt
    refused at startup does not undo the first attempt's edits, so the
    checkout they are in is kept; a review no attempt of which ran is
    discarded. A create the runtime never answered is that startup refusal
    too: even on a zero exit, nothing is published."""
    cfg = _cfg(tmp_path)
    cfg.sandbox.dangerously_skip_containment = True

    def _two_attempts(**kwargs: Any) -> DispatchResult:
        worktree: Path = kwargs["worktree_dir"]
        if first_attempt_ran:
            (worktree / "attempt-1.txt").write_text("first attempt work")
            kwargs["launch_state"].observe(
                DispatchResult(1, PR, worktree / "run.log", launched=True)
            )
        return DispatchResult(
            0,
            PR,
            worktree / "run.log",
            launched=False,
            error_category=category,
            stop_reason="ERROR: the agent never started",
        )

    with patch("kew.review.record_review_outcome") as record:
        outcome = _review_with(cfg, _two_attempts)

    assert getattr(outcome, "exit_code", 0) != 0
    (recorded,) = [call.args[0] for call in record.call_args_list]
    assert (recorded.stage, recorded.classification) == ("pre_launch", category)
    review_dir = review_checkout_path(cfg, PR)
    if first_attempt_ran:
        assert (review_dir / "attempt-1.txt").read_text() == "first attempt work"
    else:
        assert not review_dir.exists()


def _merged(pr_state: str, head: str | None) -> NodeFacts:
    return NodeFacts(
        issue_num=42,
        state="CLOSED",
        state_reason="COMPLETED",
        labels=frozenset(),
        pr_number=PR,
        pr_url=f"https://github.com/o/r/pull/{PR}",
        review_decision=None,
        merge_state_status=None,
        head_oid=head,
        pr_pushed_at=None,
        pr_state=pr_state,
    )


def _reconcile(
    store: SqliteRunStore,
    cfg: DispatchConfig,
    pr_state: str,
    head: str | None = None,
) -> None:
    _reconcile_github_links(
        "epic:x",
        {"42": _merged(pr_state, head)},
        provider="github",
        facts=MagicMock(),
        store=store,
        config=cfg,
    )


@pytest.mark.parametrize("pr_state", ["MERGED", "CLOSED"])
def test_a_finished_pr_leaves_no_review_checkout(
    tmp_path: Path, work: Path, store: SqliteRunStore, pr_state: str
) -> None:
    cfg = _cfg(tmp_path)
    for n in range(3):
        _failed_review(cfg, f"review-{n}", store)
    assert _preserved(cfg)

    _reconcile(store, cfg, pr_state)

    assert _preserved(cfg) == []
    assert not review_checkout_path(cfg, PR).exists()


def test_a_pr_closed_after_its_head_moved_keeps_the_deferred_copy(
    tmp_path: Path, work: Path, store: SqliteRunStore
) -> None:
    """Closed after kew merged main into it: the deferred work was never
    published, and the copy it is in (here the live checkout) stays."""
    cfg = _cfg(tmp_path)
    head = _pr_head(work)
    _failed_review(cfg, "deferred-work", store, head=head)
    _defer(store, review_checkout_path(cfg, PR), head)
    moved = _merge_main_into_pr(work)

    _reconcile(store, cfg, "CLOSED", moved)

    review_dir = review_checkout_path(cfg, PR)
    assert (review_dir / "marker.txt").read_text() == "deferred-work"
    assert _preserved(cfg) == []


def test_a_pr_closed_unmerged_keeps_only_its_deferrals_copy(
    tmp_path: Path, work: Path, store: SqliteRunStore
) -> None:
    """#3186: closed without merging, the PR's unpublished deferred work may
    still be wanted, so the copy a deferral names stays; every other copy
    goes."""
    cfg = _cfg(tmp_path)
    head = _pr_head(work)
    _failed_review(cfg, "deferred-work", store, head=head)
    _defer(store, review_checkout_path(cfg, PR), head)
    for n in range(2):
        _failed_review(cfg, f"review-{n}", store, head=head)
    kept = _deferred_checkout(store)

    _reconcile(store, cfg, "CLOSED", head)

    assert _preserved(cfg) == [kept]
    assert (kept / "marker.txt").read_text() == "deferred-work"
    assert not review_checkout_path(cfg, PR).exists()


def test_a_merged_pr_drops_even_a_deferrals_copy(
    tmp_path: Path, work: Path, store: SqliteRunStore
) -> None:
    cfg = _cfg(tmp_path)
    head = _pr_head(work)
    _failed_review(cfg, "deferred-work", store, head=head)
    _defer(store, review_checkout_path(cfg, PR), head)
    _failed_review(cfg, "review-0", store, head=head)

    _reconcile(store, cfg, "MERGED", head)

    assert _preserved(cfg) == []
    assert not review_checkout_path(cfg, PR).exists()


def test_a_removal_whose_git_times_out_never_fails_the_tick(
    tmp_path: Path, work: Path, store: SqliteRunStore
) -> None:
    """``discard_checkout`` runs bounded git subprocesses; one that times out
    is logged and the next sighting tries again (#3186)."""
    cfg = _cfg(tmp_path)
    _failed_review(cfg, "review-1", store)

    def _git_times_out(cmd: list[str], **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(cmd, 30)

    with patch("kew.worktree.run", side_effect=_git_times_out):
        _reconcile(store, cfg, "MERGED")

    assert review_checkout_path(cfg, PR).is_dir()


def test_a_claimed_checkout_of_a_finished_pr_is_left_whole(
    tmp_path: Path,
    work: Path,
    store: SqliteRunStore,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A claimed checkout is kept, and the keep says why — never a silent
    "nothing to remove"."""
    cfg = _cfg(tmp_path)
    _failed_review(cfg, "review-1", store)
    _failed_review(cfg, "review-2", store)
    live = materialize_review_checkout(
        pr_number=PR, branch_name=PR_BRANCH, config=cfg, store=store
    )
    capsys.readouterr()
    try:
        assert forget_review_checkouts(cfg, PR, store=store, merged=True) == []
        assert live.path.is_dir()
        assert len(_preserved(cfg)) == 1
        assert f"Keeping the review checkouts of PR #{PR}" in capsys.readouterr().err
    finally:
        live.release()
