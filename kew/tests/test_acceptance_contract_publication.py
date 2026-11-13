"""#2729: publication keeps a node's acceptance contract byte-identical.

A node's acceptance contract is its acceptance test file, recorded in the
ledger by path and sha256 before the implementer starts. A publication whose
tree would not hold that file byte-identical is held for a person with reason
``acceptance-test-changed``, and nothing reaches the remote.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from kew import dispatch_core, resolve, review, subprocess_utils, worktree
from kew.publication_canonical import (
    ACCEPTANCE_TEST_CHANGED,
    AcceptanceContract,
    CanonicalIdentity,
    PublicationError,
    PublicationHeld,
    publish_checkout,
)
from kew.run_store import RESOLVE_RUN_KIND, RunRecord, SqliteRunStore

BRANCH = "agent/issue-7"
ACCEPTANCE = "kew/tests/acceptance/test_issue_7.py"
AUTHORED = b"def test_ac1():\n    assert True\n"
_IDENTITY = CanonicalIdentity(name="kew", email="kew@example.invalid")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture(autouse=True)
def _git_on_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    git = shutil.which("git")
    assert git is not None
    monkeypatch.setitem(subprocess_utils.TOOL_PATHS, "git", git)


@pytest.fixture
def scene(tmp_path: Path) -> dict[str, Any]:
    """A branch carrying the authored contract, and the implementer's checkout."""
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-q", "-b", "main", str(origin)], check=True
    )
    seed = tmp_path / "seed"
    _git(tmp_path, "clone", "-q", str(origin), str(seed))
    _git(seed, "config", "user.name", "Test")
    _git(seed, "config", "user.email", "test@example.invalid")
    (seed / "src").mkdir()
    (seed / "src" / "feature.py").write_text("feature = 1\n")
    acceptance = seed / ACCEPTANCE
    acceptance.parent.mkdir(parents=True)
    acceptance.write_bytes(AUTHORED)
    _git(seed, "add", "-A")
    _git(seed, "commit", "-qm", "acceptance tests for #7")
    _git(seed, "push", "-q", "origin", "main")
    head = _git(seed, "rev-parse", "HEAD")

    checkout = tmp_path / "agent" / "checkout"
    checkout.parent.mkdir()
    _git(tmp_path, "clone", "-q", str(origin), str(checkout))
    worktree.write_checkout_generation(checkout)
    (checkout / "src" / "feature.py").write_text("feature = 2\n")
    return {"origin": origin, "checkout": checkout, "head": head}


def _contract() -> AcceptanceContract:
    return AcceptanceContract(
        path=ACCEPTANCE, sha256=hashlib.sha256(AUTHORED).hexdigest()
    )


def _publish(scene: dict[str, Any], scratch: Path) -> Any:
    head = str(scene["head"])
    return publish_checkout(
        Path(scene["checkout"]),
        remote=str(scene["origin"]),
        branch=BRANCH,
        parents=(head,),
        base_sha=head,
        message="feat: #7",
        identity=_IDENTITY,
        force_with_lease=f"{BRANCH}:",
        acceptance=(_contract(),),
        output_guard=None,
        scratch=scratch,
    )


def _remote_branches(origin: Path) -> str:
    return _git(origin, "for-each-ref", "--format=%(refname)", "refs/heads/")


def test_an_edited_acceptance_file_is_held_and_nothing_is_pushed(
    scene: dict[str, Any], tmp_path: Path
) -> None:
    """AC2: the edit is named with reason acceptance-test-changed."""
    (Path(scene["checkout"]) / ACCEPTANCE).write_text("def test_ac1():\n    pass\n")

    with pytest.raises(PublicationHeld) as raised:
        _publish(scene, tmp_path / "scratch")

    assert raised.value.flags[0] == f"{ACCEPTANCE}: {ACCEPTANCE_TEST_CHANGED}"
    assert ACCEPTANCE_TEST_CHANGED == "acceptance-test-changed"
    assert _remote_branches(Path(scene["origin"])) == "refs/heads/main"


def test_a_deleted_acceptance_file_is_held_and_nothing_is_pushed(
    scene: dict[str, Any], tmp_path: Path
) -> None:
    """AC2: deleting the contract changes it as surely as editing it."""
    (Path(scene["checkout"]) / ACCEPTANCE).unlink()

    with pytest.raises(PublicationHeld) as raised:
        _publish(scene, tmp_path / "scratch")

    assert raised.value.flags == (f"{ACCEPTANCE}: {ACCEPTANCE_TEST_CHANGED}",)
    assert _remote_branches(Path(scene["origin"])) == "refs/heads/main"


def test_a_byte_identical_acceptance_file_is_not_held(
    scene: dict[str, Any], tmp_path: Path
) -> None:
    """AC3: the implementer's own change publishes with the contract intact."""
    published = _publish(scene, tmp_path / "scratch")

    origin = Path(scene["origin"])
    assert published.commit_sha == _git(origin, "rev-parse", f"refs/heads/{BRANCH}")
    assert published.changed_paths == ("src/feature.py",)
    assert _git(origin, "show", f"{BRANCH}:{ACCEPTANCE}") == AUTHORED.decode().strip()


def test_a_rewrite_back_to_the_same_bytes_is_not_held(
    scene: dict[str, Any], tmp_path: Path
) -> None:
    """AC3: the rule compares bytes, not whether the file was touched."""
    (Path(scene["checkout"]) / ACCEPTANCE).write_bytes(AUTHORED)

    published = _publish(scene, tmp_path / "scratch")

    assert published.changed_paths == ("src/feature.py",)


def _dispatch_push(scene: dict[str, Any], store: SqliteRunStore) -> Any:
    return dispatch_core._canonical_push_branch(
        BRANCH,
        Path(scene["checkout"]),
        repo=str(scene["origin"]),
        base_sha=str(scene["head"]),
        message="feat: #7",
        runtime=None,
        image=None,
        proxy_environment="none",
        max_attempts=1,
        delays=(),
        sleep=lambda _seconds: None,
        on_no_change=None,
        store=store,
        run_id="implementer-run",
        item_id="7",
    )


def _store_with_contract(scene: dict[str, Any]) -> SqliteRunStore:
    store = SqliteRunStore(":memory:")
    store.record_acceptance_contract(
        "github:thossullivan/kew#2477",
        "7",
        path=ACCEPTANCE,
        sha256=_contract().sha256,
        author_run_id="author-run",
        commit=str(scene["head"]),
    )
    return store


def test_dispatch_publication_holds_on_the_ledgers_contract(
    scene: dict[str, Any],
) -> None:
    """AC2 end to end: the dispatch push reads the contract from the ledger."""
    (Path(scene["checkout"]) / ACCEPTANCE).write_text("def test_ac1():\n    pass\n")
    store = _store_with_contract(scene)

    with pytest.raises(PublicationHeld) as raised:
        _dispatch_push(scene, store)

    assert raised.value.flags[0] == f"{ACCEPTANCE}: {ACCEPTANCE_TEST_CHANGED}"
    assert dispatch_core._publication_error_category(raised.value) == (
        "publication_review_required"
    )
    assert _remote_branches(Path(scene["origin"])) == "refs/heads/main"


def test_dispatch_publication_keeping_the_contract_publishes(
    scene: dict[str, Any],
) -> None:
    """AC3 end to end: an intact contract does not hold the dispatch push."""
    store = _store_with_contract(scene)

    published = _dispatch_push(scene, store)

    origin = Path(scene["origin"])
    assert published.commit_sha == _git(origin, "rev-parse", f"refs/heads/{BRANCH}")


# -- remediation publications read the same contract (review, republish, resolve) --

PR_NUMBER = 2757
EDITED = "def test_ac1():\n    pass\n"


def _open_pr_branch(scene: dict[str, Any]) -> None:
    """Put the PR branch on the remote at the contract's commit."""
    checkout = Path(scene["checkout"])
    _git(checkout, "push", "-q", "origin", f"HEAD:refs/heads/{BRANCH}")


def _branch_head(scene: dict[str, Any]) -> str:
    return _git(Path(scene["origin"]), "rev-parse", f"refs/heads/{BRANCH}")


def test_review_remediation_holds_on_the_contract_recorded_for_its_node(
    scene: dict[str, Any],
) -> None:
    """AC2: a governed review remediation reads its node's contract, not the PR's.

    The contract is recorded under the node's item; the PR number keys none.
    """
    _open_pr_branch(scene)
    (Path(scene["checkout"]) / ACCEPTANCE).write_text(EDITED)
    store = _store_with_contract(scene)

    with pytest.raises(PublicationError) as raised:
        review._canonical_review_publication(
            worktree_dir=Path(scene["checkout"]),
            branch_name=BRANCH,
            expected_head_sha=str(scene["head"]),
            pr_number=PR_NUMBER,
            repo=str(scene["origin"]),
            host_publishes=True,
            store=store,
            item_id="7",
        )

    flags = review.deferred_publication_flags(raised.value)
    assert flags is not None
    assert flags[0] == f"{ACCEPTANCE}: {ACCEPTANCE_TEST_CHANGED}"
    assert _branch_head(scene) == scene["head"]


def test_review_remediation_keeping_the_contract_publishes(
    scene: dict[str, Any],
) -> None:
    """AC3: the node's intact contract does not hold the review publication."""
    _open_pr_branch(scene)
    store = _store_with_contract(scene)

    published, _already = review._canonical_review_publication(
        worktree_dir=Path(scene["checkout"]),
        branch_name=BRANCH,
        expected_head_sha=str(scene["head"]),
        pr_number=PR_NUMBER,
        repo=str(scene["origin"]),
        host_publishes=True,
        store=store,
        item_id="7",
    )

    assert published == _branch_head(scene) != scene["head"]


def test_republishing_an_agent_head_that_edits_the_contract_is_held(
    scene: dict[str, Any],
) -> None:
    """AC2: the host adopts no agent-pushed tree that changes the contract."""
    _open_pr_branch(scene)
    checkout = Path(scene["checkout"])
    _git(checkout, "config", "user.name", "Agent")
    _git(checkout, "config", "user.email", "agent@example.invalid")
    (checkout / ACCEPTANCE).write_text(EDITED)
    _git(checkout, "commit", "-qam", "fix: address review")
    _git(checkout, "push", "-q", "origin", f"HEAD:refs/heads/{BRANCH}")
    observed = _branch_head(scene)
    store = _store_with_contract(scene)

    with pytest.raises(PublicationHeld) as raised:
        worktree.republish_agent_head(
            checkout,
            BRANCH,
            expected_sha=str(scene["head"]),
            observed_sha=observed,
            commit_message=f"fix: address review feedback on PR #{PR_NUMBER}",
            repo=str(scene["origin"]),
            store=store,
            item_id="7",
        )

    assert raised.value.flags[0] == f"{ACCEPTANCE}: {ACCEPTANCE_TEST_CHANGED}"
    # The host built and pushed nothing over the agent's head.
    assert _branch_head(scene) == observed


def test_a_resolve_integration_that_edits_the_contract_is_held(
    scene: dict[str, Any],
) -> None:
    """AC2: kew resolve's merge reads the contract of the item its run names."""
    _open_pr_branch(scene)
    origin = Path(scene["origin"])
    checkout = Path(scene["checkout"])
    _git(checkout, "config", "user.name", "Agent")
    _git(checkout, "config", "user.email", "agent@example.invalid")
    _git(checkout, "stash", "-q")
    (checkout / "base.txt").write_text("base moved\n")
    _git(checkout, "add", "base.txt")
    _git(checkout, "commit", "-qm", "base moves on")
    _git(checkout, "push", "-q", "origin", "HEAD:refs/heads/main")
    base_sha = _git(checkout, "rev-parse", "HEAD")
    (checkout / ACCEPTANCE).write_text(EDITED)
    store = _store_with_contract(scene)
    store.save(
        RunRecord(
            run_id="resolve-run",
            issue_num=7,
            item_id="7",
            branch=BRANCH,
            status="running",
            exit_code=0,
            started_at=datetime(2026, 10, 12, tzinfo=UTC),
            run_kind=RESOLVE_RUN_KIND,
        )
    )

    with pytest.raises(PublicationError) as raised:
        resolve._push_clean_integration_canonical(
            worktree_dir=checkout,
            branch_name=BRANCH,
            base_branch="main",
            expected_head_sha=str(scene["head"]),
            base_sha=base_sha,
            repo=str(origin),
            store=store,
            run_id="resolve-run",
        )

    flags = review.deferred_publication_flags(raised.value)
    assert flags is not None
    assert flags[0] == f"{ACCEPTANCE}: {ACCEPTANCE_TEST_CHANGED}"
    assert _branch_head(scene) == scene["head"]
