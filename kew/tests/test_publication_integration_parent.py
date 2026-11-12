"""#2525: a remediation must publish kew's own base integration as a parent.

The root cause behind every "copy-sync" of this drive (#2304, #2302, #2463,
#2469, #2324; #2457 fixed the symptom). kew materializes a remediation checkout
by merging ``origin/<base>`` into the PR branch (#2362), the agent edits on top,
and publication then committed the whole tree with ONE parent. The integration
merge was flattened, so the commit carried the base's newer files as if the
agent had authored them: PR #2461's commit 16a8fe13 was 62 files / 5303
insertions, four of them byte copies of ``main``, and when the base moved again
all four conflicted and the node was held ``base_integration_conflict``.

A publication that records the integrated base commit as a SECOND parent says
the true thing: the PR's diff against its merge base is the branch's own work
again, and a later base move merges cleanly. The second parent is the host's own
``integrated_base_sha``, never a value read back out of the agent's checkout —
which the checkout may still VETO, because a record of a merge is not a promise
the agent kept it — in git state (the ``abandoned`` tests) or in the published
tree, which is a separate hole because a revert of the merge leaves the history
saying yes while the content is gone (#2525 review).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from kew import subprocess_utils, worktree
from kew.publication_canonical import CanonicalPublication, PublicationHeld
from kew.worktree import PublicationError, _publish_existing_pr_branch_canonical

BRANCH = "agent/issue-2525"
#: The base's own file. Only ``main`` ever writes it, so every version of it in
#: a publication came from the base one way or another.
SHARED = "src/shared.py"
#: The node's own file: its earlier work, and the remediation edit on top.
OWNED = "src/owned.py"
#: A file both sides edit, where the branch already carries the base's change.
LONG = "src/long.py"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _merge(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), "merge", "--no-edit", *args],
        check=False,
        capture_output=True,
        text=True,
    )


def _identify(repo: Path) -> None:
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.invalid")


@pytest.fixture(autouse=True)
def _git_on_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    git = shutil.which("git")
    assert git is not None
    monkeypatch.setitem(subprocess_utils.TOOL_PATHS, "git", git)


@pytest.fixture
def scene(tmp_path: Path) -> dict[str, Any]:
    """The #2461 shape: a branch, a base that moved, and the host's own merge.

    ``head`` is the PR head the remediation publishes onto. ``integrated`` is
    the base commit kew's checkout integration merged in before the agent ran —
    a REAL merge in the checkout, which is what makes the second parent true.
    The agent's own edit is written by each test.
    """
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-q", "-b", "main", str(origin)], check=True
    )
    seed = tmp_path / "seed"
    _git(tmp_path, "clone", "-q", str(origin), str(seed))
    _identify(seed)
    for path, body in ((SHARED, "shared = 1\n"), (OWNED, "owned = 1\n")):
        target = seed / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
    _git(seed, "add", "-A")
    _git(seed, "commit", "-qm", "base")
    _git(seed, "push", "-q", "origin", "main")

    _git(seed, "checkout", "-q", "-b", BRANCH)
    (seed / OWNED).write_text("owned = 2\n")
    _git(seed, "commit", "-qam", "the node's own change")
    head = _git(seed, "rev-parse", "HEAD")
    _git(seed, "push", "-q", "origin", BRANCH)

    _git(seed, "checkout", "-q", "main")
    (seed / SHARED).write_text("shared = 2\n")
    _git(seed, "commit", "-qam", "the base moves")
    _git(seed, "push", "-q", "origin", "main")
    integrated = _git(seed, "rev-parse", "HEAD")

    checkout = tmp_path / "agent" / "checkout"
    checkout.parent.mkdir()
    _git(tmp_path, "clone", "-q", "-b", BRANCH, str(origin), str(checkout))
    _identify(checkout)
    # kew's own base integration (#2362), as a real merge commit.
    assert _merge(checkout, "origin/main").returncode == 0
    worktree.write_checkout_generation(checkout)
    return {
        "origin": origin,
        "seed": seed,
        "checkout": checkout,
        "head": head,
        "integrated": integrated,
    }


def _advance_base(scene: dict[str, Any], body: str) -> str:
    """Move ``origin/main`` on, after the host integrated it."""
    seed = Path(scene["seed"])
    _git(seed, "checkout", "-q", "main")
    (seed / SHARED).write_text(body)
    _git(seed, "commit", "-qam", "the base moves again")
    _git(seed, "push", "-q", "origin", "main")
    return _git(seed, "rev-parse", "HEAD")


def _publish(scene: dict[str, Any], **overrides: Any) -> Any:
    facts: dict[str, Any] = {
        "commit_message": "fix: address review feedback on PR #2461",
        "expected_remote_sha": str(scene["head"]),
        "repo": str(scene["origin"]),
        "merged_base_branch": "main",
        "integrated_base_sha": str(scene["integrated"]),
    }
    facts.update(overrides)
    return _publish_existing_pr_branch_canonical(
        Path(scene["checkout"]), BRANCH, **facts
    )


# ---------------------------------------------------------------------------
# The merge the host actually made is the merge it publishes
# ---------------------------------------------------------------------------


def test_a_host_integrated_checkout_publishes_a_real_merge(
    scene: dict[str, Any],
) -> None:
    """AC. Parents are (PR head, the integrated base), and the published diff
    against that base is the branch's own work — not the base's own files."""
    (Path(scene["checkout"]) / OWNED).write_text("owned = 3\n")

    published = _publish(scene)

    assert isinstance(published, CanonicalPublication)
    origin = Path(scene["origin"])
    integrated = str(scene["integrated"])
    assert published.parent_shas == (str(scene["head"]), integrated)
    assert published.commit_sha == _git(origin, "rev-parse", f"refs/heads/{BRANCH}")
    # The PR's diff is computed against its merge base, and that is now the
    # commit the host integrated rather than where the branch was cut.
    assert _git(origin, "merge-base", integrated, published.commit_sha) == integrated
    assert (
        _git(origin, "diff", "--name-only", integrated, published.commit_sha) == OWNED
    )
    assert _git(origin, "show", f"{published.commit_sha}:{OWNED}") == "owned = 3"
    assert _git(origin, "show", f"{published.commit_sha}:{SHARED}") == "shared = 2"


def test_a_later_base_move_merges_without_conflict(
    scene: dict[str, Any], tmp_path: Path
) -> None:
    """The #2461 failure itself: the base moves again after the remediation and
    the next round's base integration has to merge cleanly. Flattened, the
    branch looked like the author of ``shared.py``'s second version and the
    base's third version conflicted with it."""
    (Path(scene["checkout"]) / OWNED).write_text("owned = 3\n")

    _publish(scene)
    _advance_base(scene, "shared = 3\n")

    next_round = tmp_path / "next-round"
    _git(tmp_path, "clone", "-q", "-b", BRANCH, str(scene["origin"]), str(next_round))
    _identify(next_round)
    integration = _merge(next_round, "origin/main")

    assert integration.returncode == 0, integration.stdout + integration.stderr
    assert (next_round / SHARED).read_text() == "shared = 3\n"
    assert (next_round / OWNED).read_text() == "owned = 3\n"


# ---------------------------------------------------------------------------
# The copy-sync hold (#2457) still judges what the integration cannot excuse
# ---------------------------------------------------------------------------


def test_a_copy_of_a_never_integrated_base_head_is_still_held(
    scene: dict[str, Any],
) -> None:
    """The second parent accounts for the commit the host integrated and
    nothing else, so content that is the base head's OWN — which neither the
    branch nor that integration holds — is still a copy, and still held."""
    _advance_base(scene, "shared = 3\n")
    checkout = Path(scene["checkout"])
    (checkout / OWNED).write_text("owned = 3\n")
    (checkout / SHARED).write_text("shared = 3\n")

    with pytest.raises(PublicationError) as raised:
        _publish(scene)

    held = raised.value.__cause__
    assert isinstance(held, PublicationHeld)
    assert any(SHARED in flag for flag in held.flags)
    assert all(OWNED not in flag for flag in held.flags)
    assert _git(Path(scene["origin"]), "rev-parse", f"refs/heads/{BRANCH}") == str(
        scene["head"]
    )


def test_the_content_the_host_integrated_still_rides_along(
    scene: dict[str, Any],
) -> None:
    """The control for the hold above, with the rule armed the same way: the
    base content kew's own merge brought in publishes, as #2362 requires."""
    _advance_base(scene, "shared = 3\n")
    (Path(scene["checkout"]) / OWNED).write_text("owned = 3\n")

    published = _publish(scene)

    assert isinstance(published, CanonicalPublication)
    assert published.parent_shas == (str(scene["head"]), str(scene["integrated"]))
    origin = Path(scene["origin"])
    assert _git(origin, "show", f"{published.commit_sha}:{SHARED}") == "shared = 2"


# ---------------------------------------------------------------------------
# When there is no integration to record, nothing changes
# ---------------------------------------------------------------------------


def test_a_checkout_with_no_recorded_integration_publishes_one_parent(
    scene: dict[str, Any],
) -> None:
    """A checkout from before #2362 records no integrated base, so there is no
    host merge to declare and the publication is exactly as it was."""
    (Path(scene["checkout"]) / OWNED).write_text("owned = 3\n")

    published = _publish(scene, integrated_base_sha=None)

    assert isinstance(published, CanonicalPublication)
    assert published.parent_shas == (str(scene["head"]),)


def test_an_integration_already_in_the_branch_history_adds_no_parent(
    scene: dict[str, Any],
) -> None:
    """A branch that already contains the base commit got "Already up to date"
    from the integration: it brought nothing in, so declaring it as a parent
    would record a merge that never happened."""
    checkout = Path(scene["checkout"])
    # The branch itself now carries the integration, so the next round's
    # checkout integration has nothing to bring in.
    _git(checkout, "push", "-q", "origin", BRANCH)
    merged_head = _git(checkout, "rev-parse", "HEAD")
    (checkout / OWNED).write_text("owned = 3\n")

    published = _publish(scene, expected_remote_sha=merged_head)

    assert isinstance(published, CanonicalPublication)
    assert published.parent_shas == (merged_head,)


def _without_copy_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    """Leave the copy-sync rule unarmed, so only the parent choice is judged.

    An integration commit nobody can read is also no evidence for that rule,
    and it HOLDS on it (#2633, ``test_publication_copy_sync``). These tests are
    about what such a commit does to the parents, so they publish unarmed.
    """
    monkeypatch.setattr(worktree, "_merged_base_head", lambda *_a, **_k: None)


def test_an_unresolvable_integration_commit_holds_the_publication(
    scene: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2632. A recorded commit the remote cannot answer for is neither handed
    to the commit as a parent nor silently dropped: dropping it flattened the
    host's merge again — the #2525 defect — on any remote error. The
    publication is held for a person and nothing is pushed.

    The commit named here is one the CHECKOUT holds and the remote does not —
    the host's own integration merge, which is never pushed — so it passes the
    checkout's veto and reaches the ancestry query that cannot answer for it.
    """
    checkout = Path(scene["checkout"])
    (checkout / OWNED).write_text("owned = 3\n")
    unpushed = _git(checkout, "rev-parse", "HEAD")
    _without_copy_sync(monkeypatch)

    with pytest.raises(PublicationError) as raised:
        _publish(scene, integrated_base_sha=unpushed)

    held = raised.value.__cause__
    assert isinstance(held, PublicationHeld)
    assert any(unpushed[:12] in flag for flag in held.flags)
    assert _git(Path(scene["origin"]), "rev-parse", f"refs/heads/{BRANCH}") == str(
        scene["head"]
    )


def test_a_transient_ancestry_failure_holds_rather_than_flattens(
    scene: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2632. The integration is real and held by the tree; only the remote
    query failed. That proves nothing false about the merge, so the parent may
    not be dropped for it."""
    (Path(scene["checkout"]) / OWNED).write_text("owned = 3\n")

    def unreadable(*_args: Any, **_kwargs: Any) -> bool:
        raise OSError("connection reset")

    monkeypatch.setattr(worktree, "remote_commit_is_ancestor", unreadable)

    with pytest.raises(PublicationError) as raised:
        _publish(scene)

    assert isinstance(raised.value.__cause__, PublicationHeld)
    assert _git(Path(scene["origin"]), "rev-parse", f"refs/heads/{BRANCH}") == str(
        scene["head"]
    )


def test_a_commit_the_checkout_never_held_publishes_one_parent(
    scene: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recorded sha the checkout cannot resolve at all proves no merge."""
    _without_copy_sync(monkeypatch)
    (Path(scene["checkout"]) / OWNED).write_text("owned = 3\n")

    published = _publish(scene, integrated_base_sha="d" * 40)

    assert isinstance(published, CanonicalPublication)
    assert published.parent_shas == (str(scene["head"]),)


# ---------------------------------------------------------------------------
# An integration the published tree does not hold is not published as a merge
# ---------------------------------------------------------------------------


@pytest.fixture
def abandoned(scene: dict[str, Any], tmp_path: Path) -> dict[str, Any]:
    """A checkout whose base integration CONFLICTED, left for the agent.

    The base now edits ``OWNED`` as well — the node's own file — so the merge
    kew runs before the agent cannot finish on its own, which is the state that
    reaches an agent at all. The checkout is a fresh one with that conflicted
    merge in progress; each test decides what the agent did with it.
    """
    seed = Path(scene["seed"])
    _git(seed, "checkout", "-q", "main")
    (seed / OWNED).write_text("owned = 1b\n")
    _git(seed, "commit", "-qam", "the base edits the node's own file too")
    _git(seed, "push", "-q", "origin", "main")
    integrated = _git(seed, "rev-parse", "HEAD")

    checkout = tmp_path / "conflicted" / "checkout"
    checkout.parent.mkdir()
    _git(tmp_path, "clone", "-q", "-b", BRANCH, str(scene["origin"]), str(checkout))
    _identify(checkout)
    integration = _merge(checkout, "origin/main")
    assert integration.returncode != 0, "the integration has to conflict"
    assert _git(checkout, "diff", "--name-only", "--diff-filter=U") == OWNED
    worktree.write_checkout_generation(checkout)
    return {**scene, "checkout": checkout, "integrated": integrated}


def test_an_abandoned_integration_publishes_no_merge_parent(
    abandoned: dict[str, Any],
) -> None:
    """The host's record says it merged; it does not say the merge survived.
    An agent that answers the conflict with ``git merge --abort`` leaves a tree
    holding none of the base's content, and a parent declared from the record
    alone would be a merge the commit's own tree contradicts."""
    checkout = Path(abandoned["checkout"])
    _git(checkout, "merge", "--abort")
    (checkout / OWNED).write_text("owned = 3\n")

    published = _publish(abandoned)

    assert isinstance(published, CanonicalPublication)
    assert published.parent_shas == (str(abandoned["head"]),)
    origin = Path(abandoned["origin"])
    integrated = str(abandoned["integrated"])
    assert _git(origin, "show", f"{published.commit_sha}:{SHARED}") == "shared = 1"
    # The commit the integration never finished is still outside the branch, so
    # the next round integrates it for real instead of being told it is in.
    assert _git(origin, "merge-base", integrated, published.commit_sha) != integrated


def test_an_abandoned_integration_cannot_revert_the_base(
    abandoned: dict[str, Any], tmp_path: Path
) -> None:
    """The whole cost of that false claim. With the abandoned merge declared,
    the base commit becomes the PR's merge base, every file the branch never
    touched reads as deliberately reverted, and merging the PR takes the
    branch's older tree wholesale — no conflict, the base's own commit gone."""
    checkout = Path(abandoned["checkout"])
    _git(checkout, "merge", "--abort")
    (checkout / OWNED).write_text("owned = 3\n")

    _publish(abandoned)

    landing = tmp_path / "landing"
    _git(tmp_path, "clone", "-q", "-b", "main", str(abandoned["origin"]), str(landing))
    _identify(landing)
    landed = _merge(landing, f"origin/{BRANCH}")

    assert landed.returncode != 0, "the unresolved conflict has to surface"
    assert _git(landing, "diff", "--name-only", "--diff-filter=U") == OWNED
    assert (landing / SHARED).read_text() == "shared = 2\n"


def test_a_resolution_that_reverts_the_base_on_a_shared_path_adds_no_parent(
    abandoned: dict[str, Any], tmp_path: Path
) -> None:
    """#2632. The branch had already changed ``OWNED`` itself, so a path-only
    check reads "the published tree changed it since the fork" and calls the
    base's change held — even when the resolution put back the PR head's own
    bytes and dropped the base's ``owned = 1b`` entirely. Declared, that
    parent makes landing the PR revert the base's change without a conflict."""
    checkout = Path(abandoned["checkout"])
    _git(checkout, "checkout", "--ours", OWNED)
    _git(checkout, "add", OWNED)
    assert (checkout / OWNED).read_text() == "owned = 2\n"
    assert (checkout / SHARED).read_text() == "shared = 2\n"

    published = _publish(abandoned)

    assert isinstance(published, CanonicalPublication)
    assert published.parent_shas == (str(abandoned["head"]),)
    landing = tmp_path / "landing"
    _git(tmp_path, "clone", "-q", "-b", "main", str(abandoned["origin"]), str(landing))
    _identify(landing)
    landed = _merge(landing, f"origin/{BRANCH}")
    assert landed.returncode != 0, "the dropped base change has to surface"
    assert _git(landing, "diff", "--name-only", "--diff-filter=U") == OWNED


def test_a_resolution_left_uncommitted_still_records_the_parent(
    abandoned: dict[str, Any],
) -> None:
    """An agent that resolves the conflict is told to leave it staged and
    uncommitted, so the merge the published tree carries is ``MERGE_HEAD``'s,
    not ``HEAD``'s. That tree does hold the base's content, and the parent that
    records it is true — the veto above must not take this one away."""
    checkout = Path(abandoned["checkout"])
    (checkout / OWNED).write_text("owned = 3\n")
    _git(checkout, "add", OWNED)

    published = _publish(abandoned)

    assert isinstance(published, CanonicalPublication)
    assert published.parent_shas == (
        str(abandoned["head"]),
        str(abandoned["integrated"]),
    )
    origin = Path(abandoned["origin"])
    assert _git(origin, "show", f"{published.commit_sha}:{SHARED}") == "shared = 2"


# ---------------------------------------------------------------------------
# History is not content: a merge the tree undid is not a merge either
# ---------------------------------------------------------------------------


def test_a_reverted_integration_publishes_no_merge_parent(
    scene: dict[str, Any],
) -> None:
    """#2525 review. ``git revert -m 1 HEAD`` leaves the host's merge in the
    checkout's history while undoing every byte it brought in. An ancestry
    answer alone still says yes, so the veto has to read the published TREE:
    the base's content is gone, and no parent may claim it."""
    checkout = Path(scene["checkout"])
    _git(checkout, "revert", "--no-edit", "-m", "1", "HEAD")
    (checkout / OWNED).write_text("owned = 3\n")
    assert (checkout / SHARED).read_text() == "shared = 1\n"

    published = _publish(scene)

    assert isinstance(published, CanonicalPublication)
    assert published.parent_shas == (str(scene["head"]),)
    origin = Path(scene["origin"])
    integrated = str(scene["integrated"])
    assert _git(origin, "merge-base", integrated, published.commit_sha) != integrated


def test_a_checkout_of_the_pr_head_over_the_merge_publishes_no_merge_parent(
    scene: dict[str, Any],
) -> None:
    """The same undo by the other route an agent reaches for: ``git checkout
    <pr-head> -- .`` puts the pre-integration content back without touching
    history at all, so ``MERGE_HEAD``/``HEAD`` ancestry still answers yes."""
    checkout = Path(scene["checkout"])
    _git(checkout, "checkout", str(scene["head"]), "--", ".")
    (checkout / OWNED).write_text("owned = 3\n")
    assert (checkout / SHARED).read_text() == "shared = 1\n"

    published = _publish(scene)

    assert isinstance(published, CanonicalPublication)
    assert published.parent_shas == (str(scene["head"]),)


def test_a_reverted_integration_cannot_revert_the_base(
    scene: dict[str, Any], tmp_path: Path
) -> None:
    """The cost the veto buys off. Declared, the undone merge makes the base
    commit the PR's merge base, the base's own newer file reads as deliberately
    reverted, and landing the PR drops ``shared = 2`` cleanly and silently."""
    checkout = Path(scene["checkout"])
    _git(checkout, "revert", "--no-edit", "-m", "1", "HEAD")
    (checkout / OWNED).write_text("owned = 3\n")

    _publish(scene)

    landing = tmp_path / "landing"
    _git(tmp_path, "clone", "-q", "-b", "main", str(scene["origin"]), str(landing))
    _identify(landing)
    landed = _merge(landing, f"origin/{BRANCH}")

    assert landed.returncode == 0, landed.stdout + landed.stderr
    assert (landing / SHARED).read_text() == "shared = 2\n"
    assert (landing / OWNED).read_text() == "owned = 3\n"


def test_an_edit_on_top_of_the_integrated_content_still_records_the_parent(
    scene: dict[str, Any],
) -> None:
    """The control for the tree veto. An agent is allowed to edit the very file
    the integration brought in — that is remediation work, not an undo — and
    the parent that records the merge must survive it."""
    checkout = Path(scene["checkout"])
    (checkout / SHARED).write_text("shared = 2\nreviewed = True\n")
    (checkout / OWNED).write_text("owned = 3\n")

    published = _publish(scene)

    assert isinstance(published, CanonicalPublication)
    assert published.parent_shas == (str(scene["head"]), str(scene["integrated"]))


def _both_sides_edit(tmp_path: Path, *, head_carries: bool) -> dict[str, Any]:
    """A branch and a base that both edit ``LONG``, merged cleanly by the host.

    The base changes ``LONG``'s first line; the branch changes its last line
    and, when *head_carries*, had already picked up the base's first-line
    change on its own before the host integrated.
    """
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-q", "-b", "main", str(origin)], check=True
    )
    seed = tmp_path / "seed"
    _git(tmp_path, "clone", "-q", str(origin), str(seed))
    _identify(seed)
    pad = "pad\n" * 5
    for path, body in ((LONG, f"top = 1\n{pad}bottom = 1\n"), (OWNED, "owned = 1\n")):
        target = seed / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
    _git(seed, "add", "-A")
    _git(seed, "commit", "-qm", "base")
    _git(seed, "push", "-q", "origin", "main")

    _git(seed, "checkout", "-q", "-b", BRANCH)
    top = "top = 2" if head_carries else "top = 1"
    (seed / LONG).write_text(f"{top}\n{pad}bottom = 2\n")
    (seed / OWNED).write_text("owned = 2\n")
    _git(seed, "commit", "-qam", "the node's own change")
    head = _git(seed, "rev-parse", "HEAD")
    _git(seed, "push", "-q", "origin", BRANCH)

    _git(seed, "checkout", "-q", "main")
    (seed / LONG).write_text(f"top = 2\n{pad}bottom = 1\n")
    _git(seed, "commit", "-qam", "the base moves")
    _git(seed, "push", "-q", "origin", "main")
    integrated = _git(seed, "rev-parse", "HEAD")

    checkout = tmp_path / "agent" / "checkout"
    checkout.parent.mkdir()
    _git(tmp_path, "clone", "-q", "-b", BRANCH, str(origin), str(checkout))
    _identify(checkout)
    assert _merge(checkout, "origin/main").returncode == 0
    assert (checkout / LONG).read_text() == f"top = 2\n{pad}bottom = 2\n"
    worktree.write_checkout_generation(checkout)
    return {
        "origin": origin,
        "seed": seed,
        "checkout": checkout,
        "head": head,
        "integrated": integrated,
    }


def test_a_change_the_branch_already_carried_still_records_the_parent(
    tmp_path: Path,
) -> None:
    """#2632 review. The base advanced ``LONG`` and the PR head differs from the
    base there, yet the clean merge left the head's bytes in place because the
    head already held the base's change. Reading that as "put back to its
    pre-integration content" dropped the parent of an untouched kew merge and
    flattened it again (#2525)."""
    carried = _both_sides_edit(tmp_path, head_carries=True)
    (Path(carried["checkout"]) / OWNED).write_text("owned = 3\n")

    published = _publish(carried)

    assert isinstance(published, CanonicalPublication)
    assert published.parent_shas == (str(carried["head"]), str(carried["integrated"]))


def test_a_clean_merge_put_back_on_a_shared_path_adds_no_parent(
    tmp_path: Path,
) -> None:
    """The control: both sides edited ``LONG`` and the merge brought the base's
    first line in cleanly. Putting the PR head's bytes back there undoes it,
    and that still may not be declared."""
    scene = _both_sides_edit(tmp_path, head_carries=False)
    checkout = Path(scene["checkout"])
    _git(checkout, "checkout", str(scene["head"]), "--", LONG)
    (checkout / OWNED).write_text("owned = 3\n")

    published = _publish(scene)

    assert isinstance(published, CanonicalPublication)
    assert published.parent_shas == (str(scene["head"]),)
