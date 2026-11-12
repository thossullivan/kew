"""Governed review round 7 (2026-09-22), PR #1952.

Round 5 gave every kew-made checkout one removal path, so the claim lock next
to the tree goes with it. The reviewer stage did not use that path: it paired
``shutil.rmtree`` with ``release_checkout_claim``, which drops the in-process
claim and leaves the lock file behind. Every governed review left one in the
system temp directory, which is the leak round 5 says it closed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from kew.graph.driver import review_stage
from kew.review_dispatch import ReviewerDispatch
from kew.worktree import claim_checkout


def test_round7_reviewer_teardown_removes_the_claim_lock(tmp_path):
    checkout = tmp_path / "kew-review-42"
    checkout.mkdir()
    (checkout / "file.txt").write_text("reviewed\n")
    lock = tmp_path / "kew-review-42.lock"

    claim_checkout(checkout)
    assert lock.exists(), "the claim writes a lock beside the checkout"

    result = ReviewerDispatch(
        outcome="dispatched",
        run_id="review-run",
        item_id="42",
        head_sha="a" * 40,
        worktree=checkout,
    )
    store = Mock()
    # No earlier review runs: the rate-limit wait reads the node's history.
    store.list_by_item.return_value = []
    config = SimpleNamespace(
        review=SimpleNamespace(
            adversarial=True,
            blocking=True,
            max_review_usd=10.0,
            publish_status=False,
        )
    )

    with (
        patch.object(review_stage, "dispatch_reviewer", return_value=result),
        patch.object(review_stage, "_process_result") as processed,
    ):
        review_stage._dispatch_and_collect(
            "node",
            "42",
            SimpleNamespace(issue_num=42, pr_number=None),
            "a" * 40,
            statuses={},
            outcome=SimpleNamespace(held=[], unwired=[]),
            epic_ref="epic",
            config=config,
            store=store,
            repo="owner/repo",
            repo_root=Path(tmp_path),
            base="main",
            reviewer_launcher=Mock(),
            worker_cap=1,
            now=datetime.now(UTC),
        )

    processed.assert_called_once()
    released = store.release_epic_node_item.call_args
    assert released.args == ("42",), "the reservation is released once"
    assert not checkout.exists(), "the reviewer worktree is removed"
    assert not lock.exists(), "and so is the lock that sat beside it"


def test_round7_reviewer_teardown_runs_when_the_verdict_raises(tmp_path):
    """Teardown is on every terminal path, so a crash cannot leak the lock."""
    checkout = tmp_path / "kew-review-43"
    checkout.mkdir()
    lock = tmp_path / "kew-review-43.lock"
    claim_checkout(checkout)

    result = ReviewerDispatch(
        outcome="dispatched",
        run_id="review-run",
        item_id="43",
        head_sha="b" * 40,
        worktree=checkout,
    )
    config = SimpleNamespace(
        review=SimpleNamespace(
            adversarial=True,
            blocking=True,
            max_review_usd=10.0,
            publish_status=False,
        )
    )

    raised = False
    try:
        with (
            patch.object(review_stage, "dispatch_reviewer", return_value=result),
            patch.object(
                review_stage, "_process_result", side_effect=RuntimeError("verdict")
            ),
        ):
            review_stage._dispatch_and_collect(
                "node",
                "43",
                SimpleNamespace(issue_num=43, pr_number=None),
                "b" * 40,
                statuses={},
                outcome=SimpleNamespace(held=[], unwired=[]),
                epic_ref="epic",
                config=config,
                store=Mock(list_by_item=Mock(return_value=[])),
                repo="owner/repo",
                repo_root=Path(tmp_path),
                base="main",
                reviewer_launcher=Mock(),
                worker_cap=1,
                now=datetime.now(UTC),
            )
    except RuntimeError:
        raised = True

    assert raised, "the verdict error still reaches the caller"
    assert not checkout.exists()
    assert not lock.exists()
